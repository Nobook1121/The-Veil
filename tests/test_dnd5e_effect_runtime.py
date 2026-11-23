"""声明式 workflow 钩子（midi-qol / DAE / ItemMacro 的安全等价物）。

覆盖：

- **DSL 校验**：白名单 action、参数类型/范围、单次 20 个 action、嵌套 3 层；
  危险动作名（``eval`` / ``macro.execute`` / ``flags.set`` …）一律拒绝；
- **条件表达式**：``target.hp_pct < 0.5`` / ``self.has_condition('raging')`` /
  ``weapon.properties has 'finesse'`` 及 ``and or not`` 组合；未知上下文根、
  ``__class__`` 这类反射名字、语法错都报错（**绝不当成成立**）；
  ``formula.py`` 兼容桥只借"条件求值"函数，不把公式求值器的 dict 结果当布尔；
- **执行器**：触发点过滤、顺序执行、状态跳过、跨钩子共享 action 预算、
  rng 注入可重放、不改 context / hooks；
- **导入期分诊**：``parse_workflow`` 对真实样本（heroes / sixian 包）的结论，
  heroes 4 个 ItemMacro + 4 段 DAE JS 全部 needs_review 且原文不丢；
  sixian 的「死云术 Cloudkill」转出 5d8 毒素（豁免半伤）damage.add；
- **导入落点**：装备行的钩子写 ``equipment[].item.workflow_hooks``（``item``
  缺席时退到行级），非装备条目（专长/法术）汇总到 ``sheet.workflow_hooks``，
  怪物走 ``statblock.workflow_hooks``，audit 计数
  ``workflow_hooks_converted`` / ``workflow_hooks_needs_review`` 对账。
"""

from __future__ import annotations

import copy
import random
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FVTT_PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
DND5E_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e"
for _path in (FVTT_PLUGIN_DIR, DND5E_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from dnd5e_plugin import effect_runtime as er  # noqa: E402
from fvtt_world_import import convert_actors as ca  # noqa: E402
from fvtt_world_import import world_package as wp  # noqa: E402

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"

#: 真实样本的钩子账目（flag 名 / 状态）。两包都是实测值，改口径必须同步改这里。
HEROES_HOOKS = {"items": 4, "converted": 0, "needs_review": 8}
SIXIAN_HOOKS = {"items": 1, "converted": 1, "needs_review": 2}


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


def _package(zip_path: Path, workdir: Path):
    pkg = wp.extract_package(_require_zip(zip_path).read_bytes(), workdir)
    index = wp.read_actors(pkg)
    return ca.convert_world_actors(index, item_lookup=None, kiwee_enrich=False)


def _cloudkill_script() -> str:
    """真实样本里那段 ActiveAuras 模板宏（只作为"原文"使用，绝不执行）。"""
    return (
        'const arg = Array.isArray(args) ? args[0] : args;\n'
        'if (arg?.macroPass !== "preActiveEffects") return;\n'
        'const activeAuras = game.modules.get("ActiveAuras");\n'
        'await activeAuras.api.AAHelpers.applyTemplate({...arg});\n'
    )


def _cloudkill_doc() -> dict:
    return {
        "name": "死云术 Cloudkill",
        "type": "spell",
        "flags": {
            "midi-qol": {"onUseMacroName": "[preActiveEffects]ItemMacro"},
            "dae": {"macro": {"name": "死云术 Cloudkill", "type": "script", "scope": "global", "command": _cloudkill_script()}},
        },
        "system": {
            "activities": {
                "21d3d24d6581cae8": {
                    "type": "save",
                    "name": "cloudkill: zone pulse",
                    "save": {"ability": ["con"], "dc": {"calculation": "spellcasting"}},
                    "damage": {
                        "onSave": "half",
                        "parts": [{"number": 5, "denomination": 8, "types": ["poison"]}],
                    },
                }
            }
        },
    }


# -- DSL 校验 --------------------------------------------------------------


def test_dsl_round_trip_fills_defaults_and_keeps_order() -> None:
    dsl = {
        "trigger": "onUse",
        "condition": "target.hp_pct < 0.5",
        "actions": [
            {"type": "damage.add", "dice": "1d6", "damage_type": "fire", "target": "hit"},
            {"type": "attack.advantage"},
            {"type": "save.disadvantage"},
            {"type": "condition.add", "condition": "prone", "duration": 1},
            {"type": "heal", "amount": "2d4"},
            {"type": "temp_hp", "amount": 5},
            {"type": "resource.spend", "key": "uses", "amount": 1},
            {"type": "effect.apply", "name": "Absorb Elements", "duration_rounds": 3},
        ],
    }
    validated = er.validate_dsl(dsl)
    assert validated["trigger"] == "onUse"
    assert validated["condition"] == "target.hp_pct < 0.5"
    assert [action["type"] for action in validated["actions"]] == [action["type"] for action in dsl["actions"]]
    # 缺省参数被补齐（调用方不必自己猜 target/on_save 的默认值）。
    assert validated["actions"][0]["on_save"] == "full"
    assert validated["actions"][1]["target"] == "self"
    assert validated["actions"][4]["target"] == "self"


def test_dsl_rejects_unknown_trigger_and_field() -> None:
    with pytest.raises(er.HookError) as trigger_error:
        er.validate_dsl({"trigger": "onRoll", "actions": []})
    assert trigger_error.value.code == "trigger.unknown"
    with pytest.raises(er.HookError) as field_error:
        er.validate_dsl({"trigger": "onUse", "actions": [], "script": "eval(1)"})
    assert field_error.value.code == "dsl.unknown_field"


@pytest.mark.parametrize(
    "action",
    [
        {"type": "eval", "code": "1"},
        {"type": "exec", "code": "1"},
        {"type": "script", "src": "x"},
        {"type": "macro.execute", "name": "ItemMacro"},
        {"type": "item.update", "path": "system.uses.value", "value": 0},
        {"type": "flags.set", "key": "x"},
        {"type": "game.exec"},
    ],
)
def test_dangerous_action_names_are_refused(action: dict) -> None:
    """越权动作名：拒绝且**不降级执行**（不给"部分执行"的口子）。"""
    with pytest.raises(er.HookError) as excinfo:
        er.validate_action(action)
    assert excinfo.value.code in ("action.forbidden", "action.unknown")


@pytest.mark.parametrize(
    "action, code",
    [
        ({"type": "damage.add", "dice": "21d6", "damage_type": "fire"}, "dice.count"),
        ({"type": "damage.add", "dice": "1d7", "damage_type": "fire"}, "dice.sides"),
        ({"type": "damage.add", "dice": "1d6+500", "damage_type": "fire"}, "dice.modifier"),
        ({"type": "damage.add", "dice": "1d6+eval(1)", "damage_type": "fire"}, "dice.modifier"),
        ({"type": "damage.add", "dice": "1d6", "damage_type": "holy"}, "param.choice"),
        ({"type": "damage.add", "dice": "1d6", "damage_type": "fire", "target": "moon"}, "param.choice"),
        ({"type": "damage.add", "dice": "1d6", "damage_type": "fire", "bonus": 3}, "param.unknown"),
        ({"type": "temp_hp", "amount": 501}, "param.range"),
        ({"type": "temp_hp", "amount": True}, "param.type"),
        ({"type": "condition.add", "condition": "俯卧", "duration": 1}, "param.charset"),
        ({"type": "condition.add", "condition": "prone", "duration": -1}, "param.range"),
        ({"type": "resource.spend", "key": "uses", "amount": 0}, "param.range"),
        ({"type": "resource.spend", "key": "x" * 40}, "param.length"),
        ({"type": "effect.apply", "name": "x" * 200}, "param.length"),
        ({"type": "effect.apply", "name": "a\nb"}, "param.charset"),
        ({"type": "heal"}, "param.missing"),
        ({"type": "heal", "amount": "2d4", "target": ("hit",)}, "param.choice"),
    ],
)
def test_action_parameters_are_range_and_type_checked(action: dict, code: str) -> None:
    with pytest.raises(er.HookError) as excinfo:
        er.validate_action(action)
    assert excinfo.value.code == code


def test_action_budget_and_nesting_depth() -> None:
    ok = {"type": "effect.apply", "name": "x", "then": [{"type": "effect.apply", "name": "y", "then": [{"type": "effect.apply", "name": "z"}]}]}
    er.validate_dsl({"trigger": "onUse", "actions": [ok]})

    too_deep = {
        "type": "effect.apply",
        "name": "x",
        "then": [{"type": "effect.apply", "name": "y", "then": [{"type": "effect.apply", "name": "z", "then": [{"type": "effect.apply", "name": "w"}]}]}],
    }
    with pytest.raises(er.HookError) as depth_error:
        er.validate_dsl({"trigger": "onUse", "actions": [too_deep]})
    assert depth_error.value.code == "nesting.depth"

    with pytest.raises(er.HookError) as count_error:
        er.validate_dsl({"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 1}] * 21})
    assert count_error.value.code == "actions.limit"


# -- 条件表达式 ------------------------------------------------------------


@pytest.mark.parametrize(
    "expression, context, expected",
    [
        ("target.hp_pct < 0.5", {"target": {"hp": 10, "hp_max": 40}}, True),
        ("target.hp_pct < 0.5", {"target": {"hp": 30, "hp_max": 40}}, False),
        ("target.hp_pct < 0.5", {"target": {"hp": 30, "hp_max": 40}}, False),
        ("self.hp <= 10", {"self": {"hp": 10}}, True),
        ("self.has_condition('raging')", {"self": {"conditions": ["Raging"]}}, True),
        ("self.has_condition('raging')", {"self": {"conditions": ["prone"]}}, False),
        ("weapon.properties has 'finesse'", {"weapon": {"properties": ["light", "finesse"]}}, True),
        ("weapon.properties has 'finesse'", {"weapon": {"properties": ["heavy"]}}, False),
        ("'fire' in target.damage_types", {"target": {"damage_types": ["fire", "cold"]}}, True),
        (
            "target.hp_pct < 0.5 and self.has_condition('raging')",
            {"target": {"hp": 4, "hp_max": 40}, "self": {"conditions": ["raging"]}},
            True,
        ),
        (
            "not (self.hp <= 5 or target.hp_pct > 0.9)",
            {"self": {"hp": 20}, "target": {"hp": 1, "hp_max": 10}},
            True,
        ),
        ("event.critical", {"event": {"critical": True}}, True),
    ],
)
def test_condition_expressions(expression: str, context: dict, expected: bool) -> None:
    assert er.eval_condition(expression, context) is expected


@pytest.mark.parametrize(
    "expression, context",
    [
        ("target.unknown_field > 1", {"target": {"hp": 1}}),
        ("mystery.hp > 1", {"target": {"hp": 1}}),
        ("self.__class__", {"self": {}}),
        ("self.hp >", {"self": {"hp": 1}}),
        ("self.hp + 1", {"self": {"hp": 1}}),
        ("__import__('os')", {}),
        ("open('/etc/passwd')", {}),
        ("self.eval('1')", {"self": {}}),
    ],
)
def test_condition_errors_are_fail_closed(expression: str, context: dict) -> None:
    with pytest.raises((er.ConditionError, er.HookError)):
        er.eval_condition(expression, context)


def test_empty_condition_means_no_condition() -> None:
    """没有条件 = 恒真（这是 DSL 的显式语义，不是"解析失败当成立"）。

    空白串这类脏数据由**校验期**挡住（:func:`validate_dsl` 会拒），执行期把它
    当"无条件"处理——两侧都不允许"坏了却执行"。
    """
    assert er.eval_condition("", {}) is True
    assert er.eval_condition(None, {}) is True
    with pytest.raises(er.HookError):
        er.validate_dsl({"trigger": "onUse", "condition": "   ", "actions": []})


def test_condition_bridge_ignores_formula_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    """``formula.py`` 的 ``evaluate`` 是公式求值器，不能被当成条件求值器。

    这是一个真实踩过的坑：``evaluate`` 返回 ``{"ok": True, ...}``，直接 ``bool()``
    会把「算不出来」读成「条件成立」。桥只认 ``eval_condition`` /
    ``evaluate_condition``。
    """
    import dnd5e_plugin

    fake = types.ModuleType("dnd5e_plugin.formula")
    fake.evaluate = lambda formula, context=None, **kw: {"ok": True, "value": 1}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dnd5e_plugin.formula", fake)
    # ``from dnd5e_plugin import formula`` 走包属性，所以两处都要换。
    monkeypatch.setattr(dnd5e_plugin, "formula", fake, raising=False)
    assert er._formula_condition("target.hp_pct < 0.5", {"target": {"hp": 30, "hp_max": 40}}) is er._MISSING
    assert er.eval_condition("target.hp_pct < 0.5", {"target": {"hp": 30, "hp_max": 40}}) is False

    # 真的提供条件求值函数时才借它（这里是"永远不成立"）。
    fake.eval_condition = lambda expression, context: False  # type: ignore[attr-defined]
    assert er.eval_condition("target.hp_pct < 0.5", {"target": {"hp": 1, "hp_max": 40}}) is False


# -- 执行器 ----------------------------------------------------------------


def test_run_is_sequential_and_replayable() -> None:
    dsl = {
        "trigger": "onUse",
        "actions": [
            {"type": "damage.add", "dice": "2d6", "damage_type": "fire", "target": "hit"},
            {"type": "condition.add", "condition": "prone", "duration": 1},
            {"type": "temp_hp", "amount": 5, "target": "self"},
        ],
    }
    context = {"self": {"hp": 12}, "target": {"hp": 20}}
    first = er.run("onUse", context, [dsl], rng=random.Random(2024))
    second = er.run("onUse", context, [dsl], rng=random.Random(2024))
    assert first == second, "同 seed 必须逐字节可重放"
    assert [action["type"] for action in first["actions"]] == [
        "damage.add",
        "condition.add",
        "temp_hp",
    ]
    assert [event["kind"] for event in first["events"]] == [action["type"] for action in first["actions"]]
    assert first["actions"][0]["amount"] == sum(first["actions"][0]["rolls"])
    assert len(first["actions"][0]["rolls"]) == 2
    # 另一个 seed 只要骰子不同就说明随机源真的被注入了。
    other = er.run("onUse", context, [dsl], rng=random.Random(7))
    assert other["actions"][0]["rolls"] != first["actions"][0]["rolls"] or len(first["actions"][0]["rolls"]) != 2


def test_run_does_not_mutate_inputs() -> None:
    hooks = [
        {
            "source": "flags.midi-qol.onUseMacroName",
            "status": "converted",
            "dsl": {"trigger": "onUse", "actions": [{"type": "heal", "amount": "1d4"}]},
        }
    ]
    context = {"self": {"hp": 3, "conditions": ["raging"]}}
    hooks_before = copy.deepcopy(hooks)
    context_before = copy.deepcopy(context)
    er.run("onUse", context, hooks, rng=random.Random(1))
    assert hooks == hooks_before
    assert context == context_before


def test_run_filters_trigger_and_skips_unreviewed_hooks() -> None:
    hooks = [
        {"source": "a", "status": "needs_review", "dsl": None},
        {"source": "b", "status": "native", "dsl": {"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 1}]}},
        {"source": "c", "status": "ignored", "dsl": {"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 1}]}},
        {"source": "d", "status": "converted", "dsl": {"trigger": "onHit", "actions": [{"type": "temp_hp", "amount": 2}]}},
        {"source": "e", "status": "converted", "dsl": {"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 3}]}},
    ]
    result = er.run("onUse", {}, hooks, rng=random.Random(3))
    assert [action["source"] for action in result["actions"]] == ["e"]
    assert len(result["notes"]) == 3  # needs_review / native / ignored 各一条说明


def test_run_rejects_bad_dsl_instead_of_partially_executing() -> None:
    hooks = [
        {
            "source": "poisoned",
            "status": "converted",
            "dsl": {
                "trigger": "onUse",
                "actions": [
                    {"type": "temp_hp", "amount": 1},
                    {"type": "script", "src": "game.modules"},
                ],
            },
        }
    ]
    result = er.run("onUse", {}, hooks)
    assert result["actions"] == []
    assert result["events"] == []
    assert "校验失败" in result["notes"][0]


def test_run_budget_is_shared_across_hooks() -> None:
    one = {"source": "h1", "status": "converted", "dsl": {"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 1}] * 15}}
    two = {"source": "h2", "status": "converted", "dsl": {"trigger": "onUse", "actions": [{"type": "temp_hp", "amount": 2}] * 15}}
    result = er.run("onUse", {}, [one, two], rng=random.Random(1))
    assert len(result["actions"]) == er.MAX_ACTIONS_PER_TRIGGER
    assert any("上限" in note for note in result["notes"])


def test_run_condition_failure_skips_and_notes() -> None:
    hooks = [
        {
            "source": "h",
            "status": "converted",
            "dsl": {"trigger": "onUse", "condition": "target.mystery > 1", "actions": [{"type": "temp_hp", "amount": 1}]},
        }
    ]
    result = er.run("onUse", {"target": {"hp": 3}}, hooks)
    assert result["actions"] == []
    assert "条件求值失败" in result["notes"][0]


def test_run_rejects_unknown_trigger() -> None:
    with pytest.raises(er.HookError):
        er.run("onExplode", {}, [])


def test_run_works_with_plain_dsl_list() -> None:
    result = er.run("onUse", {}, [{"trigger": "onUse", "actions": [{"type": "effect.apply", "name": "Bless", "duration_rounds": 10}]}])
    assert result["actions"][0]["name"] == "Bless"
    assert result["actions"][0]["duration_rounds"] == 10


# -- 导入期分诊 ------------------------------------------------------------


def test_parse_workflow_converts_item_macro_with_damage() -> None:
    entries = er.parse_workflow(_cloudkill_doc())
    statuses = er.hook_stats(entries)
    assert statuses["converted"] == 1
    assert statuses["needs_review"] == 2  # 宏的其余副作用 + DAE 正文
    converted = next(entry for entry in entries if entry["status"] == "converted")
    assert converted["source"] == "flags.midi-qol.onUseMacroName"
    assert converted["trigger"] == "onUse"
    assert converted["macro"] == "ItemMacro"
    assert converted["macro_pass"] == "preActiveEffects"
    assert converted["dsl"] == {
        "trigger": "onUse",
        "actions": [
            {
                "type": "damage.add",
                "dice": "5d8",
                "damage_type": "poison",
                "target": "hit",
                "on_save": "half",
            }
        ],
    }
    # 原文不丢（宏体照抄保存，绝不执行）。
    dae_entry = next(entry for entry in entries if entry["source"] == "flags.dae.macro.command")
    assert dae_entry["status"] == "needs_review"
    assert "ActiveAuras" in dae_entry["raw"]


def test_parse_workflow_marks_unknown_macro_and_flags_for_review() -> None:
    doc = {
        "name": "Mystery",
        "type": "feat",
        "flags": {
            "midi-qol": {
                "onHitMacroName": "[onHit]TotallyUnknownMacro",
                "brandNewSwitch": {"nested": 3},
                "itemCondition": 'actor.flags.x === "y"',
            }
        },
    }
    entries = er.parse_workflow(doc)
    by_source = {entry["source"]: entry for entry in entries}
    assert by_source["flags.midi-qol.onHitMacroName"]["status"] == "needs_review"
    assert by_source["flags.midi-qol.onHitMacroName"]["raw"] == "[onHit]TotallyUnknownMacro"
    assert by_source["flags.midi-qol.onHitMacroName"]["trigger"] == "onHit"
    assert by_source["flags.midi-qol.brandNewSwitch.nested"]["status"] == "needs_review"
    assert by_source["flags.midi-qol.itemCondition"]["status"] == "needs_review"
    assert by_source["flags.midi-qol.itemCondition"]["raw"] == 'actor.flags.x === "y"'


def test_parse_workflow_marks_native_and_ignored() -> None:
    doc = {
        "name": "Rage",
        "type": "feat",
        "flags": {
            "midi-qol": {
                "advantage.attack.all": True,
                "disadvantage.ability.save.dex": True,
                "critical.all": True,
                "criticalThreshold": 19,
                "rollAttackPerTarget": "always",
                "itemCondition": "",
            },
            "midiProperties": {"confirmTargets": "always", "autoFailFriendly": False},
            "babele": {"originalPayload": {"description": "<p>desc</p>"}},
        },
    }
    entries = er.parse_workflow(doc)
    by_source = {entry["source"]: entry for entry in entries}
    for key in (
        "flags.midi-qol.advantage.attack.all",
        "flags.midi-qol.disadvantage.ability.save.dex",
        "flags.midi-qol.critical.all",
        "flags.midi-qol.criticalThreshold",
    ):
        assert by_source[key]["status"] == "native", key
    assert by_source["flags.midi-qol.rollAttackPerTarget"]["status"] == "ignored"
    assert by_source["flags.midi-qol.itemCondition"]["status"] == "ignored"
    assert by_source["flags.midiProperties.confirmTargets"]["status"] == "ignored"
    # description 里含 "script"，但按分段匹配不该被误判成钩子。
    assert "flags.babele" in by_source
    assert by_source["flags.babele"]["status"] == "ignored"
    assert all(entry["status"] != "needs_review" for entry in entries)


def test_parse_workflow_reads_activity_macro_and_itemacro() -> None:
    doc = {
        "name": "Sneak Attack",
        "type": "feat",
        "flags": {"itemacro": {"macro": {"command": "item.use();", "name": "sneak"}}},
        "system": {"activities": {"dnd5eactivity000": {"macroData": {"command": "actor.rollDamage();", "name": "sv"}}}},
    }
    entries = er.parse_workflow(doc)
    sources = {entry["source"] for entry in entries if entry["status"] == "needs_review"}
    assert sources == {"flags.itemacro.macro.command", "system.activities.dnd5eactivity000.macroData.command"}


def test_parse_workflow_on_clean_item_returns_nothing() -> None:
    assert er.parse_workflow({"name": "Longsword", "type": "weapon", "flags": {"core": {}}}) == []


def test_kept_hooks_drops_ignored_and_compacts_raw() -> None:
    entries = [
        {"source": "a", "status": "converted", "trigger": "onUse", "dsl": {"trigger": "onUse", "actions": []}, "raw": "x", "reason": "r"},
        {"source": "b", "status": "needs_review", "trigger": "", "dsl": None, "raw": "y" * 900, "reason": "r2"},
        {"source": "c", "status": "ignored", "trigger": "", "dsl": None, "raw": "z", "reason": "r3"},
        {"source": "d", "status": "native", "trigger": "", "dsl": None, "raw": "w", "reason": "r4"},
    ]
    kept = er.kept_hooks(entries)
    assert [row["source"] for row in kept] == ["a", "b"]
    assert len(kept[1]["raw"]) < 300
    assert "900" in kept[1]["raw"]


# -- 真实样本 --------------------------------------------------------------


def test_real_packages_hook_triage(tmp_path: Path) -> None:
    """heroes 4 条 ItemMacro + 4 段 DAE JS 全 needs_review；sixian 那条能转。"""
    heroes = _package(HEROES_ZIP, tmp_path / "heroes")
    heroes_totals = heroes["totals"]["workflow_hooks"]
    assert heroes_totals["items"] == HEROES_HOOKS["items"]
    assert heroes_totals["converted"] == HEROES_HOOKS["converted"]
    assert heroes_totals["needs_review"] == HEROES_HOOKS["needs_review"]
    assert heroes_totals["errors"] == 0
    assert heroes["totals"]["workflow_hooks_converted"] == 0
    assert heroes["totals"]["workflow_hooks_needs_review"] == 8

    sixian = _package(SIXIAN_ZIP, tmp_path / "sixian")
    sixian_totals = sixian["totals"]["workflow_hooks"]
    assert sixian_totals["items"] == SIXIAN_HOOKS["items"]
    assert sixian_totals["converted"] == SIXIAN_HOOKS["converted"]
    assert sixian_totals["needs_review"] == SIXIAN_HOOKS["needs_review"]
    assert sixian["totals"]["workflow_hooks_converted"] == 1

    # 非装备条目（专长/法术）的钩子汇总到卡级，否则 heroes 的 4 条会被丢掉。
    cards = {row["name"]: row["card"] for row in heroes["characters"]}
    hooked = [name for name, card in cards.items() if card["sheet"].get("workflow_hooks")]
    assert len(hooked) == 2
    for name in hooked:
        for hook in cards[name]["sheet"]["workflow_hooks"]:
            assert hook["status"] in ("converted", "needs_review")
            # 卡级条目带来源条目名与类型（导入器用 _main_name 只留主名）。
            assert hook["item"]
            assert hook["item_type"] in ("feat", "spell")

    # sixian 的死云术挂在 NPC 的 statblock 上（怪物没有装备清单行）。
    npc_hooks = [
        hook
        for row in sixian["npcs"]
        for hook in (row["module"]["statblock"].get("workflow_hooks") or [])
    ]
    assert npc_hooks, "sixian 包的死云术钩子必须落在 statblock.workflow_hooks"
    converted = [hook for hook in npc_hooks if hook["status"] == "converted"]
    assert converted[0]["dsl"]["actions"][0]["dice"] == "5d8"
    assert converted[0]["dsl"]["actions"][0]["damage_type"] == "poison"
    assert converted[0]["dsl"]["actions"][0]["on_save"] == "half"
    # 转出来的 DSL 必须能直接被执行器跑（含中文化事件流水）。
    result = er.run("onUse", {"target": {"hp": 30, "hp_max": 30}}, converted, rng=random.Random(5))
    assert result["actions"][0]["amount"] >= 5
    assert "poison" in result["events"][0]["text"]


def test_equipment_row_placement(tmp_path: Path) -> None:
    """装备行的钩子写进 ``equipment[].item.workflow_hooks``；``item`` 缺席时退到行级。"""
    item = {
        "name": "Flame Tongue",
        "type": "weapon",
        "flags": {"midi-qol": {"onHitMacroName": "[onHit]ItemMacro"}},
        "system": {"damage": {"parts": [["1d6", "fire"]]}, "equipped": True},
    }
    rows = ca._equipment_rows({}, [item], ca._Warn())
    hooks, stats = ca._workflow_hook_report([item], rows)
    assert hooks == []  # 装备行的钩子不重复进卡级
    assert stats["placement_item"] == 1
    assert stats["converted"] == 1
    # 一条 onHit 宏按设计拆两条：伤害部分 converted + 其余副作用 needs_review。
    assert [hook["status"] for hook in rows[0]["item"]["workflow_hooks"]] == [
        "converted",
        "needs_review",
    ]
    assert rows[0]["item"]["workflow_hooks"][0]["dsl"]["actions"][0]["dice"] == "1d6"
    assert rows[0]["item"]["workflow_hooks"][0]["trigger"] == "onHit"

    row: dict = {"name": "Flame Tongue"}
    assert ca._attach_workflow_hooks(row, [{"status": "needs_review"}]) == "row"
    assert row["workflow_hooks"] == [{"status": "needs_review"}]
    nested: dict = {"name": "Flame Tongue", "item": {"id": "x"}}
    assert ca._attach_workflow_hooks(nested, [{"status": "converted"}]) == "item"
    assert nested["item"]["workflow_hooks"] == [{"status": "converted"}]


def test_workflow_engine_missing_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """dnd5e 插件缺席（导入器不硬依赖它）时钩子分诊整体跳过，导入不受影响。"""
    monkeypatch.setattr(ca, "_WORKFLOW_MODULE", None)
    monkeypatch.setattr(ca, "_WORKFLOW_LOOKED_UP", True)
    hooks, stats = ca._workflow_hook_report([{"name": "x", "type": "spell"}], [])
    assert hooks == []
    assert set(stats) == set(ca._WORKFLOW_STAT_KEYS)
    assert all(value == 0 for value in stats.values())
