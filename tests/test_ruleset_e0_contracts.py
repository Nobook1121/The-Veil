"""E0 平台契约回归：判定 / 资源 / 会话 / 内容 / UI 五类壳。

这些用例只验证契约与 CoC7 适配层，不改变现有房间业务流；CoC7 的逐字行为由
``test_coc7_check_golden.py`` 继续锁定。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.ruleset import CheckRequest, RulesetRegistry, RulesetSelectionError, builtin_registry
from trpg_server.ruleset.capabilities import (
    card_descriptor_of,
    check_outcome_of,
    resource_slots,
    target_bounds_of,
    turn_model_of,
    workspace_of,
)
from trpg_server.ruleset.content import (
    HOUSE_RULES_BY_RULESET,
    HOUSE_RULES_CORE,
    HouseRuleField,
    HouseRuleSchema,
    HouseRuleSection,
    ProgressionEvent,
    active_house_rules,
)
from trpg_server.ruleset.resources import (
    EffectInstance,
    ResourceDelta,
    apply_delta,
    normalize_effect,
    normalize_slot,
    threshold_events,
    tick_effects,
)
from trpg_server.ruleset.session import ActionBudgetSlot, TurnModel, empty_budget
from trpg_server.ruleset.ui import (
    UiDeclarationError,
    default_workspace,
    parse_ui,
    select_workspace,
)


def _card() -> dict:
    return {
        "name": "测试调查员",
        "attributes": {"STR": 50, "DEX": 60},
        "skills": [{"name": "侦察", "skillKey": "spotHidden", "value": 65}],
    }


# ---------------------------------------------------------------- 判定壳


def test_check_outcome_wraps_coc7_without_changing_legacy_result():
    plugin = builtin_registry().resolve("coc7")
    request = CheckRequest(kind="skill", name="侦察", difficulty="regular", extra={"faces": [3, 7]})
    outcome = check_outcome_of(plugin, _card(), request)
    assert outcome.ruleset_id == "coc7"
    assert outcome.kind == "skill"
    assert len(outcome.dice) == 1
    assert outcome.total == outcome.dice[0]
    assert outcome.target == 65
    assert outcome.degree in {"critical", "extreme", "hard", "regular", "failure", "fumble"}
    assert isinstance(outcome.success, bool)
    assert outcome.detail["base_target"] == 65
    assert target_bounds_of(plugin) == (0, 100)


def test_check_service_uses_plugin_target_bounds(monkeypatch):
    """核心不再写死 100：范围由插件声明，CoC7 仍保持 0–100。"""
    from trpg_server import check_service
    from trpg_server.ruleset.contracts import CheckResolution

    class _Checks:
        def lookup_target(self, card, name):
            return 150

        def target_bounds(self):
            return 0, 100

        def roll(self, card, request, rng):  # pragma: no cover - 不应被调用
            raise AssertionError("out-of-range target must fail before rolling")

    class _Plugin:
        id = "bounded"
        checks = _Checks()

    monkeypatch.setattr(check_service, "get_plugin", lambda ruleset_id=None: _Plugin())
    member = {"username": "tester", "character_card": _card()}
    with pytest.raises(check_service.CheckServiceError) as exc_info:
        check_service.run_room_check(member, name="力量", ruleset_id="coc7")
    assert "between 0 and 100" in str(exc_info.value)

    class _WideChecks(_Checks):
        def lookup_target(self, card, name):
            return 150

        def target_bounds(self):
            return 0, 200

        def roll(self, card, request, rng):
            return CheckResolution(roll=151, target=150, level="success", success=True)

    class _WidePlugin:
        id = "wide"
        checks = _WideChecks()

    monkeypatch.setattr(check_service, "get_plugin", lambda ruleset_id=None: _WidePlugin())
    result = check_service.run_room_check(member, name="力量", ruleset_id="coc7")
    assert result["roll"] == 151
    assert result["success"] is True


# ---------------------------------------------------------------- 资源 / 效果壳


def test_legacy_slot_is_normalized_with_v2_defaults():
    slot = normalize_slot({"kind": "hp", "state": "current_hp", "cap_state": "max_hp"})
    assert slot["scope"] == "character"
    assert slot["shape"] == "gauge"
    assert slot["visibility"] == "public"
    assert slot["thresholds"] == ()
    assert slot["slot_version"] == 2
    # 旧字段仍然存在，CoC7 现有读写路径不需要改。
    assert slot["state"] == "current_hp"
    assert slot["cap_state"] == "max_hp"


def test_resource_delta_clamps_and_reports_threshold_crossing():
    slot = normalize_slot(
        {
            "kind": "hp",
            "state": "current_hp",
            "cap_state": "max_hp",
            "cap_min": 0,
            "cap_max": 10,
            "thresholds": [{"at": 0, "when": "below", "event": "dying"}],
        }
    )
    change = apply_delta(slot, 7, ResourceDelta(kind="hp", delta=-3))
    assert (change.before, change.after, change.applied) == (7, 4, -3)
    assert change.clamped is False
    assert change.events == ()

    lethal = apply_delta(slot, 2, ResourceDelta(kind="hp", delta=-5))
    assert lethal.after == 0
    assert lethal.clamped is True
    assert [event.event for event in lethal.events] == ["dying"]

    assert threshold_events(slot, 0, 0) == ()


def test_effect_instance_tick_respects_persistence_and_unit():
    effect = normalize_effect(
        {
            "id": "e1",
            "kind": "prone",
            "duration": {"unit": "turn", "remaining": 2},
            "persistence": "removable",
        }
    )
    assert isinstance(effect, EffectInstance)
    remaining, expired = tick_effects([effect.to_dict()], phase="turn")
    assert len(remaining) == 1 and not expired
    remaining, expired = tick_effects(remaining, phase="turn")
    assert remaining == [] and len(expired) == 1

    permanent = EffectInstance(id="e2", kind="curse", persistence="irreversible").to_dict()
    remaining, expired = tick_effects([permanent], phase="round")
    assert len(remaining) == 1 and expired == []


def test_coc7_resource_slots_keep_existing_kinds_and_gain_v2_fields():
    plugin = builtin_registry().resolve("coc7")
    slots = resource_slots(plugin)
    assert {slot["kind"] for slot in slots} == {"hp", "san", "mp"}
    assert all(slot["scope"] == "character" and slot["slot_version"] == 2 for slot in slots)


# ---------------------------------------------------------------- 会话壳


def test_turn_model_is_declared_by_plugin_and_combat_is_optional():
    plugin = builtin_registry().resolve("coc7")
    model = turn_model_of(plugin)
    assert model.mode == "round"
    assert "action" in model.budget_keys
    assert empty_budget(model) == {"action": 1}

    class _NoCombat:
        id = "narrative"
        combat = None
        turn = TurnModel(mode="none")

    bare = turn_model_of(_NoCombat())
    assert bare.has_turns is False
    assert empty_budget(bare) == {}

    class _RoundOnly:
        id = "round"
        combat = object()

    inferred = turn_model_of(_RoundOnly())
    assert inferred.mode == "round"
    assert inferred.budget == (ActionBudgetSlot(key="action", per_round=1),)


# ---------------------------------------------------------------- 内容壳


def test_card_descriptor_v2_exposes_sections_fields_and_status_groups():
    plugin = builtin_registry().resolve("coc7")
    descriptor = card_descriptor_of(plugin)
    assert descriptor["descriptor_version"] == 2
    assert descriptor["sections"]
    assert descriptor["fields"]
    assert {item["key"] for item in descriptor["fields"]} >= {"attributes", "skillGroups"}
    assert "status_body_keys" not in descriptor
    assert {group["key"] for group in descriptor["status_groups"]} == {"body", "mental"}


def test_house_rule_schema_validates_types_and_namespace_compatibility():
    schema = HouseRuleSchema(
        ruleset_id="demo",
        sections=(
            HouseRuleSection(
                key="combat",
                fields=(
                    HouseRuleField(key="critical", type="enum", values=("double", "max"), default="double"),
                    HouseRuleField(key="threshold", type="integer", min=0, max=10, default=5),
                ),
            ),
        ),
    )
    assert schema.defaults() == {"critical": "double", "threshold": 5}
    assert schema.validate({"critical": "max", "threshold": 3}) == []
    codes = {issue["path"]: issue["code"] for issue in schema.validate({"critical": "x", "threshold": 99, "oops": 1})}
    assert codes == {"critical": "invalid_value", "threshold": "out_of_range", "oops": "unknown_field"}

    # 旧扁平 CoC 房规原样读出；新结构按规则集命名空间读取。
    flat = {"coc7_sanity_check": True}
    assert active_house_rules(flat, "coc7") == flat
    namespaced = {
        HOUSE_RULES_CORE: {"bgm": True},
        HOUSE_RULES_BY_RULESET: {"coc7": {"coc7_sanity_check": True}, "dnd5e": {"critical": "max"}},
    }
    assert active_house_rules(namespaced, "coc7") == {"coc7_sanity_check": True}
    assert active_house_rules(namespaced, "dnd5e") == {"critical": "max"}


def test_progression_event_supports_non_xp_growth():
    event = ProgressionEvent(kind="point_spend", amount=3, currency="reward", source="main_god")
    assert event.to_dict()["kind"] == "point_spend"
    assert event.to_dict()["currency"] == "reward"


# ---------------------------------------------------------------- UI 壳


def test_workspace_declaration_is_clamped_and_conversation_is_default():
    declaration = parse_ui(
        {
            "api_version": 1,
            "workspace": {
                "layout_id": "dnd5e-canvas",
                "rulesets": ["dnd5e"],
                "primary": "canvas",
                "primary_min_width": 720,
                "sidebar": {"position": "right", "width": 380, "min_width": 360, "max_width": 420},
                "topbar": {"height": 48, "slots": ["scene", "players", "turn", "status", "settings"]},
                "left_rail": {"width": 48, "visible": True},
                "panels": {"chat": "sidebar", "character": "drawer", "combat": "topbar"},
                "priority": 100,
            },
        }
    )
    assert declaration is not None and declaration.workspace is not None
    workspace = declaration.workspace
    assert workspace.primary == "canvas"
    assert workspace.sidebar.width == 380
    assert workspace.topbar.height == 48

    overwide = parse_ui({"api_version": 1, "workspace": {"primary": "canvas", "sidebar": {"width": 9999}}})
    assert overwide is not None and overwide.workspace is not None
    assert overwide.workspace.sidebar.width == overwide.workspace.sidebar.max_width

    fallback = default_workspace()
    assert fallback.primary == "conversation"
    assert fallback.sidebar.width == 260
    assert fallback.topbar.height == 0

    coc7 = workspace_of(builtin_registry().resolve("coc7"))
    assert coc7.primary == "conversation"
    assert coc7.sidebar.width == 260

    class _Bare:
        id = "bare"

    assert workspace_of(_Bare()).layout_id == "conversation"


def test_ui_declaration_rejects_unknown_slots_and_forces_sandbox_off():
    with pytest.raises(UiDeclarationError):
        parse_ui({"api_version": 1, "slots": [{"id": "x", "slot": "room.nowhere"}]})

    declaration = parse_ui(
        {
            "api_version": 1,
            "sandbox": {"enabled": True, "mode": "iframe", "position": "room.stage.primary"},
        }
    )
    assert declaration is not None and declaration.sandbox is not None
    assert declaration.sandbox.enabled is False
    assert declaration.sandbox.mode == "iframe"
    assert any("enabled" in warning for warning in declaration.sandbox.warnings)


def test_select_workspace_matches_ruleset_and_falls_back():
    class _Dnd:
        id = "dnd5e"
        workspace = parse_ui(
            {
                "api_version": 1,
                "workspace": {"layout_id": "dnd", "rulesets": ["dnd5e"], "primary": "canvas", "priority": 10},
            }
        ).workspace

    class _Other:
        id = "other"
        workspace = parse_ui(
            {
                "api_version": 1,
                "workspace": {"layout_id": "other", "rulesets": ["other"], "primary": "canvas", "priority": 99},
            }
        ).workspace

    selected = select_workspace([_Other(), _Dnd()], "dnd5e")
    assert selected.layout_id == "dnd"
    assert select_workspace([_Other()], "coc7").layout_id == "conversation"


# ---------------------------------------------------------------- 清单声明


def test_manifest_accepts_ui_capability_and_validates_it():
    from trpg_server.plugins.contracts import ManifestError
    from trpg_server.plugins.manifest import parse_manifest

    manifest = parse_manifest(
        {
            "id": "demo",
            "version": "1.0.0",
            "name": "Demo",
            "capabilities": {
                "ui": {
                    "api_version": 1,
                    "workspace": {"primary": "canvas", "rulesets": ["demo"]},
                    "slots": [{"id": "stage", "slot": "room.stage.primary", "requires": ["veil-map"]}],
                }
            },
        },
        Path("."),
    )
    ui = manifest.capability("ui")
    assert ui["workspace"]["primary"] == "canvas"
    assert ui["slots"][0]["slot"] == "room.stage.primary"

    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "id": "bad",
                "version": "1.0.0",
                "name": "Bad",
                "capabilities": {"ui": {"api_version": 1, "workspace": {"primary": "wrong"}}},
            },
            Path("."),
        )


# ---------------------------------------------------------------- 显式解析


def test_registry_exposes_structured_resolution_for_all_states():
    from trpg_server.ruleset.registry import DeclaredRuleset

    registry = builtin_registry()
    assert registry.resolve_explicit("coc7").status == "available"
    assert registry.resolve_explicit("coc7").plugin is not None

    declared = RulesetRegistry(
        [],
        declared=[DeclaredRuleset(id="coming-soon", display_name="Coming Soon")],
        default_id="",
    )
    assert declared.resolve_explicit("coming-soon").status == "development"
    assert declared.resolve_explicit("coming-soon").reason_code == "ruleset_in_development"
    with pytest.raises(RulesetSelectionError):
        declared.resolve_explicit("coming-soon").require()

    incompatible = RulesetRegistry([], default_id="")
    assert incompatible.resolve_explicit("x").reason_code == "ruleset_unknown"
