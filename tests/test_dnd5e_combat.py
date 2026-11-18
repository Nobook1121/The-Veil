"""E4：D&D 5e 攻击、伤害、先攻与动作经济。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.agents.tools import combat
from trpg_server.combat.settings import resolve_settings
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.session import empty_budget, turn_model_of

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class FakeContext:
    def __init__(self, tmp_path: Path, card: dict | None = None):
        self.room_dir = tmp_path
        self.room_id = "dnd5e-combat"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._card = card or {}

    def room_info(self) -> dict:
        members = []
        if self._card:
            members.append(
                {
                    "user_id": 1,
                    "username": "Aria",
                    "character_card": self._card,
                    "character_state": {},
                }
            )
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "house_rules": {},
            "members": members,
        }


def _card() -> dict:
    return builtin_registry().resolve("dnd5e").schema.pack(
        {
            "id": "dnd-card-1",
            "name": "Aria",
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics"], "weapons": ["martial"]},
            "hp": {"current": 24, "max": 24},
            "equipment": ["longsword", "mace", "chain-mail", "shield"],
        }
    )


def _rules():
    return builtin_registry().resolve("dnd5e").combat


def _prepared_player(plugin) -> dict:
    combatant = {"name": "Aria", "is_player": True, "side": "player"}
    _rules().prepare_combatant(combatant, card=_card(), settings=None)
    return combatant


def _prepared_monster(monster_id: str) -> dict:
    combatant = {"name": monster_id, "is_player": False, "side": "enemy", "monster_id": monster_id}
    _rules().prepare_combatant(combatant, card=None, settings=None)
    return combatant


# ---------------------------------------------------------------------------
# 声明与数据包
# ---------------------------------------------------------------------------
def test_describe_exposes_combat_and_turn_model(plugins):
    item = next(row for row in builtin_registry().describe() if row["id"] == "dnd5e")
    assert item["combat"]["available"] is True
    assert item["combat"]["attack_mode"] == "d20"
    assert set(item["combat"]["turn"]["budget"][0]) == {
        "key",
        "per_turn",
        "per_round",
        "refresh",
        "label_i18n",
        "detail",
    }
    assert item["combat"]["turn"]["budget"] == [
        {
            "key": "action",
            "per_turn": 1,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "动作",
            "detail": {},
        },
        {
            "key": "bonus_action",
            "per_turn": 1,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "附赠动作",
            "detail": {},
        },
        {
            "key": "reaction",
            "per_turn": None,
            "per_round": 1,
            "refresh": "round",
            "label_i18n": "反应",
            "detail": {},
        },
        {
            "key": "movement",
            "per_turn": 30,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "移动",
            "detail": {"unit": "ft"},
        },
    ]


def test_combat_settings_follow_room_ruleset(plugins):
    assert resolve_settings({"ruleset_id": "dnd5e"}).ruleset_id == "dnd5e"
    assert resolve_settings({"ruleset_id": "coc7"}).ruleset_id == "coc7"


def test_monster_and_armor_data_come_from_pack(plugins):
    goblin = _prepared_monster("goblin")
    assert goblin["armorClass"] == 15
    assert (goblin["hp"], goblin["max_hp"]) == (7, 7)
    assert goblin["speed_ft"] == 30
    assert goblin["attacks"][0]["name"] == "Scimitar"
    assert goblin["attacks"][0]["attack_bonus"] == 4

    player = _prepared_player(plugins)
    assert player["armorClass"] == 18  # 链甲 16 + 盾牌 2
    assert player["attacks"][0]["name"] == "长剑"
    assert player["attacks"][0]["attack_bonus"] == 5  # STR +3 + 熟练 +2
    assert player["attacks"][0]["damage_dice"] == "1d8"


def test_initiative_is_d20_plus_dex(plugins):
    goblin = _prepared_monster("goblin")
    assert _rules().roll_initiative(goblin, lambda sides: 10, None) == 12
    assert _rules().initiative_tiebreak(goblin) == 2


# ---------------------------------------------------------------------------
# 攻击与伤害数学
# ---------------------------------------------------------------------------
def test_attack_hit_crit_miss_and_vulnerability(plugins):
    player = _prepared_player(plugins)
    goblin = _prepared_monster("goblin")

    hit = _rules().resolve_attack_action(
        player, goblin, {"weapon": "长剑", "attack_roll": 18}, rng=lambda sides: 4
    )
    assert hit["success"] is True and hit["critical"] is False
    assert hit["total"] == 23 and hit["target_ac"] == 15
    assert hit["damage"]["effective"] == 7
    assert goblin["hp"] == 0 and goblin["dying"] is True

    fresh = _prepared_monster("goblin")
    crit = _rules().resolve_attack_action(
        player, fresh, {"weapon": "长剑", "attack_roll": 20}, rng=lambda sides: 3
    )
    assert crit["critical"] is True and crit["success"] is True
    assert crit["damage"]["effective"] == 9  # 2d8 + 3
    assert fresh["dying"] is True

    miss = _rules().resolve_attack_action(
        player, _prepared_monster("goblin"), {"weapon": "长剑", "attack_roll": 2}, rng=lambda sides: 4
    )
    assert miss["success"] is False and miss["damage"] is None

    skeleton = _prepared_monster("skeleton")
    vulnerable = _rules().resolve_attack_action(
        player, skeleton, {"weapon": "钉头锤", "attack_roll": 15}, rng=lambda sides: 2
    )
    # 钉头锤 1d6 + 3 = 5，骷髅对钝击易伤 → 10。
    assert vulnerable["damage"]["vulnerability"] is True
    assert vulnerable["damage"]["effective"] == 10
    assert skeleton["hp"] == 3


def test_death_saves_at_turn_start(plugins):
    skeleton = _prepared_monster("skeleton")
    skeleton.update(hp=0, dying=True, unconscious=True, status="dying")
    skeleton["death_saves"] = {"successes": 0, "failures": 2, "stable": False}
    notes = _rules().start_turn(skeleton, combat={"round": 1, "surprised": []}, rng=lambda sides: 9)
    assert skeleton["death_saves"]["failures"] == 3
    assert skeleton["dead"] is True
    assert any("死亡" in note for note in notes)


# ---------------------------------------------------------------------------
# 端到端工具与动作经济
# ---------------------------------------------------------------------------
def test_start_attack_and_action_economy(tmp_path, plugins, monkeypatch):
    context = FakeContext(tmp_path, _card())
    monkeypatch.setattr(combat, "_roller", lambda sides: 10 if sides == 20 else 3)
    started = combat.start_combat(
        {
            "participants": [
                "Aria",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started
    state = combat._load_combat(context)
    assert state["order"][0] == "Aria"
    assert state["combatants"]["Aria"]["budget"] == {
        "action": 1,
        "bonus_action": 1,
        "reaction": 1,
        "movement": 30,
    }

    result = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18}, context
    )
    assert result["success"] is True
    state = combat._load_combat(context)
    assert state["combatants"]["Aria"]["budget"]["action"] == 0
    assert state["combatants"]["Goblin"]["hp"] == 1

    blocked = combat.attack_combatant(
        {
            "attacker": "Aria",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-budget-2",
        },
        context,
    )
    assert "额度" in blocked["error"]

    spent = combat.spend_combat_action({"actor": "Aria", "slot": "movement", "amount": 10}, context)
    assert spent["summary"].startswith("Aria 剩余 movement：20")
    assert combat.spend_combat_action({"actor": "Aria", "slot": "movement", "amount": 99}, context)[
        "error"
    ]

    combat.next_turn({}, context)
    state = combat._load_combat(context)
    assert state["order"][state["turn"]] == "Goblin"
    assert state["combatants"]["Goblin"]["budget"]["action"] == 1
    combat.next_turn({}, context)
    state = combat._load_combat(context)
    assert state["order"][state["turn"]] == "Aria"
    assert state["combatants"]["Aria"]["budget"]["action"] == 1
    assert state["combatants"]["Aria"]["budget"]["movement"] == 30


def test_attack_request_id_is_idempotent(tmp_path, plugins, monkeypatch):
    """同一 request_id 重试只结算一次：不二次扣预算、不二次伤害。"""
    from trpg_server.workflows.actions import STEP_ATTACK, prior_flow
    from trpg_server.workflows.engine import WorkflowEngine

    context = FakeContext(tmp_path, _card())
    monkeypatch.setattr(combat, "_roller", lambda sides: 10 if sides == 20 else 3)
    started = combat.start_combat(
        {
            "participants": [
                "Aria",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started
    assert "request_id" in combat.COMBAT_ATTACK_TOOL.parameters["properties"]

    first = combat.attack_combatant(
        {
            "attacker": "Aria",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-idem-1",
        },
        context,
    )
    assert first.get("success") is True, first
    state_after_first = combat._load_combat(context)
    hp_after_first = state_after_first["combatants"]["Goblin"]["hp"]
    assert state_after_first["combatants"]["Aria"]["budget"]["action"] == 0

    second = combat.attack_combatant(
        {
            "attacker": "Aria",
            "target": "Goblin",
            "attack_roll": 18,
            "request_id": "atk-idem-1",
        },
        context,
    )
    assert second["workflow"]["duplicate"] is True
    assert "额度" not in str(second.get("error") or "")
    state_after_second = combat._load_combat(context)
    assert state_after_second["combatants"]["Goblin"]["hp"] == hp_after_first
    assert state_after_second["combatants"]["Aria"]["budget"]["action"] == 0

    flow = WorkflowEngine(tmp_path).get(first["workflow"]["id"])
    assert flow is not None
    assert flow["request_key"] == "atk-idem-1"
    assert any(step["id"] == STEP_ATTACK for step in flow["steps"])
    existing = prior_flow(tmp_path, "atk-idem-1", STEP_ATTACK)
    assert existing is not None


def test_attack_derives_stable_key_when_request_id_missing(tmp_path, plugins, monkeypatch):
    """未传 request_id 时，同一回合同参数攻击重试命中同一流程。"""
    context = FakeContext(tmp_path, _card())
    monkeypatch.setattr(combat, "_roller", lambda sides: 10 if sides == 20 else 3)
    combat.start_combat(
        {
            "participants": [
                "Aria",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    first = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18},
        context,
    )
    hp_after_first = combat._load_combat(context)["combatants"]["Goblin"]["hp"]
    second = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18},
        context,
    )
    assert second["workflow"]["duplicate"] is True
    assert combat._load_combat(context)["combatants"]["Goblin"]["hp"] == hp_after_first


def test_turn_model_budget_shape(plugins):
    model = turn_model_of(builtin_registry().resolve("dnd5e"))
    assert empty_budget(model) == {"action": 1, "bonus_action": 1, "reaction": 1, "movement": 30}


# ---------------------------------------------------------------------------
# 规则集切换拦截
# ---------------------------------------------------------------------------
def test_switch_clears_combat_workflows_and_areas(tmp_path, plugins):
    from trpg_server.workflows import areas as area_store
    from trpg_server.workflows.engine import WorkflowEngine
    from trpg_server.workflows.model import FLOW_CANCELLED, make_step

    from trpg_server.routes.rooms_lifecycle import _apply_ruleset_switch

    info = {"ruleset_id": "dnd5e", "house_rules": {"by_ruleset": {"dnd5e": {}, "coc7": {}}}}
    (tmp_path / "state.json").write_text(
        json.dumps({"combat": {"active": True, "round": 3, "combatants": {"Aria": {"hp": 12}}}}),
        encoding="utf-8",
    )
    flow, _error, _created = WorkflowEngine(tmp_path).open(
        kind="cast",
        initiator="Aria",
        initiator_type="player",
        steps=[make_step("resolve", "法术结算", policy="manual", executor="player")],
        ttl=600,
    )
    area = area_store.create_area(
        tmp_path,
        {
            "name": "Spike Growth",
            "shape": "sphere",
            "size_ft": 20,
            "origin": {"cell_x": 0, "cell_y": 0},
            "duration": {"unit": "round", "rounds": 10},
        },
    )["area"]

    old, new, _paused, cleanup = _apply_ruleset_switch(info, "coc7", tmp_path)
    assert (old, new) == ("dnd5e", "coc7")
    assert cleanup == {
        "combat_archived": True,
        "cancelled_workflows": 1,
        "ended_areas": 1,
    }

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["combat"]["active"] is False
    assert state["combat"]["ended_reason"] == "ruleset_switch"
    archive = json.loads((tmp_path / "combat_archive.json").read_text(encoding="utf-8"))
    assert archive["archived"][0]["combat"]["round"] == 3
    assert WorkflowEngine(tmp_path).get(flow["id"], expire=False)["status"] == FLOW_CANCELLED
    assert area_store.list_areas(tmp_path) == []
    assert area["id"] not in {item["id"] for item in area_store.list_areas(tmp_path, active_only=False)}

    # 同一规则集不受影响（例如版本迁移/保存），不会再次归档。
    _old, _new, _paused, noop = _apply_ruleset_switch(info, "coc7", tmp_path)
    assert noop == {"combat_archived": False, "cancelled_workflows": 0, "ended_areas": 0}
    archive_again = json.loads((tmp_path / "combat_archive.json").read_text(encoding="utf-8"))
    assert len(archive_again["archived"]) == 1


# ---------------------------------------------------------------------------
# 房规字段的消费点：改配置必须改变结算
# ---------------------------------------------------------------------------
def _settings(**rules):
    return resolve_settings({"ruleset_id": "dnd5e", "house_rules": dict(rules)})


def _target(ac: int = 10) -> dict:
    return {"name": "木桩", "hp": 100, "max_hp": 100, "armorClass": ac}


def _attack(player, target, settings, **action):
    payload = {"weapon": "长剑", "attack_roll": 20, **action}
    return _rules().resolve_attack_action(
        player, target, payload, rng=lambda sides: 1, settings=settings
    )


def test_critical_rule_changes_damage(plugins):
    player = _prepared_player(plugins)
    standard = _attack(player, _target(), _settings(dnd5e_critical_rule="standard"))
    assert standard["critical"] is True
    assert standard["damage"]["damage"] == 5  # 2d8(各 1) + 3

    maximized = _attack(player, _target(), _settings(dnd5e_critical_rule="max_dice"))
    assert maximized["critical"] is True
    assert maximized["damage"]["damage"] == 11  # 1d8 取 8 + 3

    none = _attack(player, _target(), _settings(dnd5e_critical_rule="none"))
    assert none["critical"] is False
    assert none["damage"]["damage"] == 4  # 1d8(1) + 3

    # 确认骰失败：按普通命中结算；确认成功：保留暴击。
    failed = _rules().resolve_attack_action(
        player,
        _target(),
        {"weapon": "长剑", "attack_roll": 20},
        rng=lambda sides: 1,
        settings=_settings(dnd5e_critical_rule="confirm"),
    )
    assert failed["critical"] is False and failed["damage"]["damage"] == 4
    confirmed = _rules().resolve_attack_action(
        player,
        _target(),
        {"weapon": "长剑", "attack_roll": 20},
        rng=lambda sides: 20 if sides == 20 else 1,
        settings=_settings(dnd5e_critical_rule="confirm"),
    )
    assert confirmed["critical"] is True and confirmed["damage"]["damage"] == 5


def test_initiative_mode_changes_roll(plugins):
    combatant = {"name": "Aria", "dex": 16}

    def sequence(*values):
        iterator = iter(values)
        return lambda sides: next(iterator)

    assert _rules().roll_initiative(combatant, sequence(3), _settings()) == 6
    assert (
        _rules().roll_initiative(combatant, sequence(), _settings(dnd5e_initiative_mode="fixed"))
        == 13
    )
    assert (
        _rules().roll_initiative(
            combatant, sequence(3, 18), _settings(dnd5e_initiative_mode="advantage")
        )
        == 21
    )


def test_surprise_rule_controls_first_turn(plugins):
    def budget():
        return {"action": 1, "bonus_action": 1, "reaction": 1, "movement": 30}

    combat = {"round": 1, "surprised": ["Aria"]}
    standard = {"name": "Aria", "budget": budget()}
    _rules().start_turn(standard, combat=combat, settings=_settings())
    assert standard["budget"]["action"] == 0

    ignored = {"name": "Aria", "budget": budget()}
    _rules().start_turn(ignored, combat=combat, settings=_settings(dnd5e_surprise_rule="none"))
    assert ignored["budget"]["action"] == 1

    # 先攻劣势：掷两次取低；预算不再清零。
    initiative = {"name": "Aria", "dex": 16, "budget": budget()}
    initiative["surprised"] = True
    values = iter([18, 4])
    roll = _rules().roll_initiative(
        initiative,
        lambda sides: next(values),
        _settings(dnd5e_surprise_rule="initiative_disadvantage"),
    )
    assert roll == 7  # 取低 4 + DEX 调整值 3
    _rules().start_turn(
        initiative, combat=combat, settings=_settings(dnd5e_surprise_rule="initiative_disadvantage")
    )
    assert initiative["budget"]["action"] == 1


def test_flanking_rule_grants_advantage(plugins):
    player = _prepared_player(plugins)
    # AC 20：单骰 12 + 5 = 17 未命中；夹击取第二次 15 → 20 命中。
    target = {"name": "木桩", "hp": 100, "max_hp": 100, "armorClass": 20}
    off = _rules().resolve_attack_action(
        player,
        target,
        {"weapon": "长剑", "attack_roll": 12, "flanking": True},
        rng=lambda sides: 15,
        settings=_settings(dnd5e_flanking=False),
    )
    assert off["advantage"] is False and off["success"] is False
    on = _rules().resolve_attack_action(
        player,
        {"name": "木桩", "hp": 100, "max_hp": 100, "armorClass": 20},
        {"weapon": "长剑", "attack_roll": 12, "flanking": True},
        rng=lambda sides: 15,
        settings=_settings(dnd5e_flanking=True),
    )
    assert on["advantage"] is True and on["success"] is True


def test_cover_rule_toggles_ac_bonus(plugins):
    player = _prepared_player(plugins)
    # 10 + 5 = 15；半掩体 +2 后未命中，关闭掩体则命中。
    standard = _rules().resolve_attack_action(
        player,
        _target(15),
        {"weapon": "长剑", "attack_roll": 10, "cover": "half"},
        rng=lambda sides: 1,
        settings=_settings(),
    )
    assert standard["target_ac"] == 17 and standard["success"] is False
    ignored = _rules().resolve_attack_action(
        player,
        _target(15),
        {"weapon": "长剑", "attack_roll": 10, "cover": "half"},
        rng=lambda sides: 1,
        settings=_settings(dnd5e_cover_rule="none"),
    )
    assert ignored["target_ac"] == 15 and ignored["success"] is True


def test_variant_encumbrance_reduces_speed(plugins):
    card = builtin_registry().resolve("dnd5e").schema.pack(
        {
            "id": "heavy-1",
            "name": "Aria",
            "level": 1,
            "class": "Fighter",
            "abilities": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
            "hp": {"current": 10, "max": 10},
            # 两件中甲合计 65 磅，超过力量 10 的负重阈值 50，且都没有力量需求。
            "equipment": ["chain-shirt", "scale-mail"],
        }
    )
    rules = _rules()
    off = {"name": "Aria", "is_player": True, "side": "player"}
    rules.prepare_combatant(off, card=card, settings=_settings())
    assert rules.action_budget(off, _settings())["movement"] == 30

    on = {"name": "Aria", "is_player": True, "side": "player"}
    settings = _settings(dnd5e_variant_encumbrance=True)
    rules.prepare_combatant(on, card=card, settings=settings)
    assert on["encumbrance"] == "light"
    assert rules.action_budget(on, settings)["movement"] == 20


def test_rest_variant_changes_rest_length(plugins):
    from dnd5e_plugin.rest import rest_profile

    assert rest_profile(_settings())["short_rest_minutes"] == 60
    assert rest_profile(_settings(dnd5e_rest_variant="gritty"))["long_rest_minutes"] == 10080
    assert rest_profile(_settings(dnd5e_rest_variant="heroic"))["short_rest_minutes"] == 5

