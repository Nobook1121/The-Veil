"""效果引擎（FVTT transfer buff）：按路径改数值 + 接入派生值。

覆盖范围（每一项都是真数行为，不是「跑过就行」）：

- 五种模式逐个：2 加 / 1 乘 / 3 取低 / 4 取高 / 5 覆盖，0（custom）只计数；
- 同一个 key 上多条 change 的结算顺序（乘→加→取低→取高→覆盖最后），
  结果与效果挂载顺序无关；
- 镜像 schema 判定：命中 schema 的路径（含 ``flags.*`` / ``ATL.*`` /
  ``activities[...]``）记进镜像并按三档分级（full / stored / unsupported），
  真正的外来命名空间才进 ``unsupported``；骰式值（``+1d4``）进 ``deferred``
  不猜数；
- 列表型 key（``traits.dr`` / ``traits.ci``）的 add / override；
- ``disabled`` 的效果、``transfer`` 但装备行未 ``equipped`` 的效果都不生效；
- 非 transfer 的角色级效果只有 ``applied: true`` 才生效；
- **回归铁律**：卡上一条效果都没有时，:func:`effective_sheet` 返回空视图，
  ``derive`` / ``pack`` 的输出与旧实现（效果引擎合入前的 schema）逐字节一致
  ——快照写死在 :data:`DERIVE_SNAPSHOT` / :data:`PACK_MIRRORS`；
- 接入点：checks（加值与 d20 模式）、combat（AC 与抗性）、statblock（NPC AC）、
  schema（SHEET_FIELDS 声明 ``effect_changes``、二次打包不重复叠加）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.contracts import CheckRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
DND5E_DIR = PLUGIN_ROOT / "dnd5e"
if str(DND5E_DIR) not in sys.path:
    sys.path.insert(0, str(DND5E_DIR))

from dnd5e_plugin import effect_changes as ec  # noqa: E402
from dnd5e_plugin.schema import SHEET_FIELDS, Dnd5eCharacterSchema  # noqa: E402


@pytest.fixture()
def plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _effect(
    changes: list[dict],
    *,
    name: str = "测试效果",
    transfer: bool = True,
    disabled: bool = False,
    applied: bool | None = None,
    origin: dict | None = None,
) -> dict:
    entry = {
        "id": f"eff-{name}",
        "name": name,
        "transfer": transfer,
        "disabled": disabled,
        "changes": changes,
    }
    if applied is not None:
        entry["applied"] = applied
    if origin is not None:
        entry["origin"] = origin
    return entry


def _ac_change(mode: int, value) -> dict:
    return {"key": "system.attributes.ac.bonus", "mode": mode, "value": value}


def _sheet(effects: list[dict], equipment: list[dict] | None = None) -> dict:
    sheet: dict = {"effect_changes": effects}
    if equipment is not None:
        sheet["equipment"] = equipment
    return sheet


def _gear(effects: list[dict], *, equipped: bool) -> dict:
    row = {"name": "防护戒指", "quantity": 1, "weight": 0.0, "notes": "首饰", "effects": effects}
    if equipped:
        row["equipped"] = True
    return row


# --- 五种模式 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        (ec.MODE_ADD, "+2", 2),
        # 没有原值时 multiply 按 1 起乘（Foundry 口径，不能乘成 0）
        (ec.MODE_MULTIPLY, 3, 3),
        (ec.MODE_DOWNGRADE, 4, 4),
        (ec.MODE_UPGRADE, 4, 4),
        (ec.MODE_OVERRIDE, 7, 7),
    ],
)
def test_each_mode_applies_on_empty_document(mode, value, expected) -> None:
    view = ec.effective_sheet(_sheet([_effect([_ac_change(mode, value)])]))
    assert not view.empty
    assert view.ac_bonus == expected
    assert view.stats["applied"] == 1
    assert view.stats["unsupported"] == 0


def test_mode_custom_is_counted_but_never_applied() -> None:
    view = ec.effective_sheet(_sheet([_effect([_ac_change(ec.MODE_CUSTOM, 5)])]))
    assert view.empty
    assert view.stats["custom"] == 1
    assert view.stats["applied"] == 0
    assert view.report()["custom"] == ["system.attributes.ac.bonus"]


def test_multiply_downgrade_upgrade_use_existing_value() -> None:
    # 同 key 多条：先乘 2（现 3 → 6），再取低 1（→ 1），最后取高 5（→ 5）。
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [
                        _ac_change(ec.MODE_ADD, 3),
                        _ac_change(ec.MODE_MULTIPLY, 2),
                        _ac_change(ec.MODE_DOWNGRADE, 1),
                        _ac_change(ec.MODE_UPGRADE, 5),
                    ]
                )
            ]
        )
    )
    assert view.ac_bonus == 5


def test_same_key_order_is_mode_fixed_and_source_order_independent() -> None:
    changes = [
        _ac_change(ec.MODE_OVERRIDE, 4),
        _ac_change(ec.MODE_ADD, 1),
        _ac_change(ec.MODE_MULTIPLY, 3),
        _ac_change(ec.MODE_DOWNGRADE, 2),
        _ac_change(ec.MODE_UPGRADE, 5),
    ]
    forward = ec.effective_sheet(_sheet([_effect(list(changes))])).ac_bonus
    backward = ec.effective_sheet(_sheet([_effect(list(reversed(changes)))])).ac_bonus
    # 覆盖永远最后：0 →(乘)3 →(加)4 →(取低)2 →(取高)5 →(覆盖)4。
    assert forward == 4
    assert backward == 4


def test_separate_effects_keep_fixed_order_too() -> None:
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [{"key": "system.bonuses.mwak.attack", "mode": ec.MODE_OVERRIDE, "value": 3}],
                    name="覆盖",
                ),
                _effect(
                    [{"key": "system.bonuses.mwak.attack", "mode": ec.MODE_ADD, "value": 2}],
                    name="加值",
                ),
            ]
        )
    )
    assert view.attack_bonus("mwak") == 3


# --- 未知路径 / 骰式值 ------------------------------------------------------


def test_module_namespaces_are_mirrored_not_thrown_away() -> None:
    """模块/前端命名空间（flags / ATL / activities）也命中镜像：记下来、分级 stored。"""
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [
                        {"key": "flags.midi-qol.grants.advantage.attack.all", "mode": 2, "value": 1},
                        {"key": "ATL.light.bright", "mode": 5, "value": 30},
                        {"key": "activities[attack].attack.ability", "mode": 5, "value": "wis"},
                        _ac_change(ec.MODE_ADD, 1),
                    ]
                )
            ]
        )
    )
    assert view.ac_bonus == 1
    # 三条都在镜像模型里 → 没有一条是 unsupported。
    assert view.stats["unsupported"] == 0
    assert view.stats["stored"] == 3
    keys = {record["key"] for record in view.stored}
    assert keys == {
        "flags.midi-qol.grants.advantage.attack.all",
        "ATL.light.bright",
        "activities[attack].attack.ability",
    }
    assert all(record["reason"] == "no-consumer" for record in view.stored)
    # 值真的进了镜像（「记录」不是空话）。
    assert ec.resolve_path(view.doc, "flags.midi-qol.grants.advantage.attack.all") == 1
    assert ec.resolve_path(view.doc, "ATL.light.bright") == 30
    assert ec.unsupported_hint("flags.midi-qol.x").startswith("模块/系统标志")


def test_truly_alien_namespace_is_unsupported() -> None:
    """镜像 schema 都没有的路径才归 unsupported（外来系统/拼写错误）。"""
    view = ec.effective_sheet(
        _sheet([_effect([{"key": "system.dwarvish.forge.temperature", "mode": 2, "value": 1}])])
    )
    assert view.stats["unsupported"] == 1
    assert view.stats["stored"] == 0
    assert view.report()["unsupported"] == ["system.dwarvish.forge.temperature"]
    assert ec.classify_path("system.dwarvish.forge.temperature")[2] == ec.TIER_UNSUPPORTED


@pytest.mark.parametrize(
    "sheet",
    [
        None,
        {},
        {"effect_changes": "not-a-list"},
        {"effect_changes": [None, 1, "x", {"changes": "x"}]},
        {"equipment": [None, {"effects": [None]}]},
    ],
)
def test_dirty_input_degrades_to_empty_view(sheet) -> None:
    view = ec.effective_sheet(sheet)
    assert view.empty
    assert view.report()["stats"]["applied"] == 0


def test_dice_formula_is_deferred_not_guessed() -> None:
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [{"key": "system.bonuses.mwak.attack", "mode": 2, "value": "1d4"}],
                    name="祝福",
                )
            ]
        )
    )
    # 骰式加值不进派生整数，单独记下来给 UI/日志。
    assert view.empty
    assert view.stats["deferred"] == 1
    assert ec.effect_dice_bonus(view, "bonuses.mwak.attack") == "1d4"


# --- 列表型路径 ------------------------------------------------------------


def test_list_paths_add_dedupe_and_override() -> None:
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [
                        {"key": "system.traits.dr.value", "mode": 2, "value": ["fire", "cold"]},
                        {"key": "system.traits.dr.value", "mode": 2, "value": ["fire", "poison"]},
                        # 逗号串也算列表（FVTT 手填常见形态）
                        {"key": "system.traits.ci.value", "mode": 5, "value": "charmed, frightened"},
                    ],
                    name="护符",
                )
            ]
        )
    )
    assert view.traits("dr") == ["fire", "cold", "poison"]
    assert view.traits("ci") == ["charmed", "frightened"]
    # 合并到卡面既有值：保序、去重、卡面在前。
    assert view.extend_traits(["cold", "acid"], "dr") == ["cold", "acid", "fire", "poison"]
    assert view.extend_traits(None, "dv") == []


def test_list_path_ignores_unsupported_modes() -> None:
    view = ec.effective_sheet(
        _sheet(
            [{"effect_changes": []} and _effect([{"key": "system.traits.dr.value", "mode": 1, "value": 2}])]
        )
    )
    assert view.traits("dr") == []
    # 列表 key 只认 add / override：别的模式如实记进 stored（原因写明模式）。
    assert view.stats["deferred"] == 0
    assert view.stats["stored"] == 1
    assert view.stored[0]["reason"] == "mode-1"


# --- 激活判定 --------------------------------------------------------------


def test_disabled_effect_is_inert() -> None:
    view = ec.effective_sheet(
        _sheet([_effect([_ac_change(ec.MODE_ADD, 1)], disabled=True)])
    )
    assert view.empty
    assert view.stats["documents"] == 1 and view.stats["active"] == 0


def test_equipment_effect_needs_transfer_and_equipped() -> None:
    transfer_unequipped = ec.effective_sheet(
        _sheet([], [_gear([_effect([_ac_change(ec.MODE_ADD, 1)])], equipped=False)])
    )
    non_transfer_equipped = ec.effective_sheet(
        _sheet([], [_gear([_effect([_ac_change(ec.MODE_ADD, 1)], transfer=False)], equipped=True)])
    )
    explicit_off = ec.effective_sheet(
        _sheet(
            [],
            [
                _gear(
                    [
                        _effect(
                            [_ac_change(ec.MODE_ADD, 1)],
                            origin={"item_id": "ring", "source": "equipment", "equipped": False},
                        )
                    ],
                    equipped=True,
                )
            ],
        )
    )
    active = ec.effective_sheet(
        _sheet([], [_gear([_effect([_ac_change(ec.MODE_ADD, 1)])], equipped=True)])
    )
    assert transfer_unequipped.empty
    assert non_transfer_equipped.empty
    assert explicit_off.empty
    assert active.ac_bonus == 1
    assert active.stats["active"] == 1


def test_actor_level_effect_needs_transfer_or_applied() -> None:
    passive = ec.effective_sheet(_sheet([_effect([_ac_change(ec.MODE_ADD, 1)], transfer=True)]))
    manual_without_flag = ec.effective_sheet(
        _sheet([_effect([_ac_change(ec.MODE_ADD, 1)], transfer=False)])
    )
    manual_applied = ec.effective_sheet(
        _sheet([_effect([_ac_change(ec.MODE_ADD, 1)], transfer=False, applied=True, name="法师护甲")])
    )
    assert passive.ac_bonus == 1
    assert manual_without_flag.empty
    assert manual_applied.ac_bonus == 1


# --- 路径白名单 ------------------------------------------------------------


def test_supported_paths_have_consumers_and_unknown_ones_do_not() -> None:
    supported = [
        "system.attributes.ac.bonus",
        "system.attributes.init.bonus",
        "system.bonuses.abilities.save",
        "system.bonuses.abilities.check",
        "system.bonuses.abilities.saving_throw",
        "system.bonuses.abilities.ability_check",
        "system.bonuses.mwak.attack",
        "system.bonuses.rwak.damage",
        "system.bonuses.msak.attack",
        "system.bonuses.rsak.attack",
        "system.bonuses.spell.dc",
        "system.abilities.dex.check.roll.mode",
        "system.abilities.con.save.roll.mode",
        "system.bonuses.abilities.save.wis",
        "system.bonuses.abilities.check.str",
        "system.bonuses.skills.stealth.check",
        "system.traits.dr.value",
        "system.traits.ci.value",
        "system.attributes.ac.calc",
        # 第二刀补全的消费方：属性值合并 / 熟练 / 分速 / 语言 / 减伤 / HP。
        "system.abilities.str.value",
        "system.attributes.prof",
        "system.attributes.movement.fly",
        "system.traits.languages.value",
        "system.traits.dm.amount",
        "system.attributes.hp.max",
        # midi-qol 子集（优势/暴击阈值）有真实消费方。
        "flags.midi-qol.advantage.attack.all",
        "flags.midi-qol.critical.all",
    ]
    assert all(ec.classify_path(key)[2] == ec.TIER_FULL for key in supported)
    # 命中镜像但没有数值消费方的路径：照实记成 stored（不是 unsupported，
    # 也不是假装生效）。
    for key in (
        "flags.dnd5e.halflingLucky",
        "ATL.light.dim",
        "system.attributes.movement.hover",
        "system.damage.base.number",
        "system.properties",
    ):
        kind, path, tier, _note, _scope = ec.classify_path(key)
        assert kind != "unsupported", key
        assert tier == ec.TIER_STORED, key
        assert path, key
    # 只有镜像 schema 里根本没有的路径才是 unsupported。
    for key in ("system.dwarvish.forge", "system.traits.nonsense", "system.attributes.nonsense.value"):
        assert ec.classify_path(key)[2] == ec.TIER_UNSUPPORTED, key


# --- 回归铁律：无效果时与旧实现逐字节一致 ---------------------------------

FIXTURE_SHEET = {
    "level": 5,
    "class": "游荡者",
    "subclass": "刺客",
    "race": "半身人",
    "background": "罪犯",
    "alignment": "混乱中立",
    "abilities": {"str": 10, "dex": 18, "con": 12, "int": 14, "wis": 11, "cha": 8},
    "proficiencies": {
        "saves": ["dex", "int"],
        "skills": ["stealth", "perception", "acrobatics"],
        "expertise": ["stealth"],
        "armor": ["light"],
        "weapons": ["simple", "martial"],
        "tools": ["thieves-tools"],
        "languages": ["common", "halfling"],
    },
    "hp": {"current": 27, "max": 27, "temp": 3},
    "armorClass": 15,
    "speed": 25,
    "initiative": 4,
    "hitDice": "5d8",
    "equipment": [
        {"name": "短剑", "quantity": 2, "weight": 2.0, "notes": "武器", "equipped": True},
        {"name": "皮甲", "quantity": 1, "weight": 10.0, "notes": "轻甲（11）", "equipped": True},
    ],
    "features": ["偷袭", "灵巧动作"],
    "spellcasting": {"ability": "int", "slots": {"1": {"max": 2, "used": 0}}},
    "notes": "",
    "xp": 6500,
}

#: 效果引擎合入**之前**的 ``derive`` 输出（由 ``git archive HEAD`` 的旧插件
#: 实跑得出，原样抄写）。任何人改派生口径都会让这条断言失败——这是铁律。
DERIVE_SNAPSHOT = {
    "abilities": {"str": 10, "dex": 18, "con": 12, "int": 14, "wis": 11, "cha": 8},
    "alignment": "混乱中立",
    "armorClass": 15,
    "attributeCap": 30,
    "background": "罪犯",
    "class": "游荡者",
    "equipment": [
        {"equipped": True, "name": "短剑", "notes": "武器", "quantity": 2, "weight": 2.0},
        {"equipped": True, "name": "皮甲", "notes": "轻甲（11）", "quantity": 1, "weight": 10.0},
    ],
    "features": ["偷袭", "灵巧动作"],
    "hitDice": "5d8",
    "hp": {"current": 27, "max": 27, "temp": 3},
    "initiative": 4,
    "level": 5,
    "maxHp": 27,
    "notes": "",
    "passivePerception": 13,
    "proficiencies": {
        "armor": ["light"],
        "expertise": ["stealth"],
        "languages": ["common", "halfling"],
        "saves": ["dex", "int"],
        "skills": ["stealth", "perception", "acrobatics"],
        "tools": ["thieves-tools"],
        "weapons": ["simple", "martial"],
    },
    "proficiencyBonus": 3,
    "race": "半身人",
    "speed": 25,
    "spellAttackBonus": 5,
    "spellSaveDC": 13,
    "spellcasting": {"ability": "int", "slots": {"1": {"max": 2, "used": 0}}},
    "subclass": "刺客",
    "xp": 6500,
}

PACK_MIRRORS = {
    "level": 5,
    "class": "游荡者",
    "race": "半身人",
    "maxHp": 27,
    "currentHp": 27,
    "tempHp": 3,
    "armorClass": 15,
    "proficiencyBonus": 3,
    "initiative": 4,
    "passivePerception": 13,
    "spellSaveDC": 13,
    "spellAttackBonus": 5,
}


def test_effective_sheet_is_empty_without_any_effect() -> None:
    view = ec.effective_sheet(dict(FIXTURE_SHEET))
    assert view.empty
    assert view.report()["stats"] == {
        "documents": 0,
        "active": 0,
        "changes": 0,
        "applied": 0,
        "stored": 0,
        "unsupported": 0,
        "deferred": 0,
        "custom": 0,
    }


def test_derive_matches_legacy_snapshot_without_effects() -> None:
    import copy

    sheet = copy.deepcopy(FIXTURE_SHEET)
    assert Dnd5eCharacterSchema().derive(sheet) == DERIVE_SNAPSHOT
    # 一次空列表也不许改变任何数（导入器给没有效果的卡写 [] 是常态）；
    # derive 会把输入原样带出，所以只比派生值本身。
    sheet["effect_changes"] = []
    with_empty_list = Dnd5eCharacterSchema().derive(sheet)
    assert with_empty_list["effect_changes"] == []
    assert {
        key: value for key, value in with_empty_list.items() if key != "effect_changes"
    } == DERIVE_SNAPSHOT


def test_pack_mirrors_match_legacy_snapshot_without_effects() -> None:
    import copy

    card = Dnd5eCharacterSchema().pack(
        {"id": "rouge-5", "name": "皮普", "sheet": copy.deepcopy(FIXTURE_SHEET)}
    )
    assert {key: card[key] for key in PACK_MIRRORS} == PACK_MIRRORS
    assert card["sheet"]["armorClass"] == 15


# --- schema 接入 -----------------------------------------------------------


def test_sheet_fields_declare_effect_changes(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    assert "effect_changes" in SHEET_FIELDS
    assert "effect_changes" in plugin.schema.sheet_fields
    descriptor = plugin.site_agent.descriptor() if hasattr(plugin, "site_agent") else None
    if isinstance(descriptor, dict):
        assert "effect_changes" in descriptor["sheet_fields"]


def test_pack_applies_effect_and_never_double_counts(plugins) -> None:
    import copy

    ring = _effect([_ac_change(ec.MODE_ADD, 1)], name="防护戒指", origin={"source": "equipment"})
    sheet = copy.deepcopy(FIXTURE_SHEET)
    sheet["equipment"] = [*sheet["equipment"], _gear([ring], equipped=True)]
    card = Dnd5eCharacterSchema().pack({"id": "rouge-5", "name": "皮普", "sheet": sheet})
    # 镜像只存基础值，效果加值只进顶层派生镜像……
    assert card["sheet"]["armorClass"] == 15
    assert card["armorClass"] == 16
    # ……所以再打包一次也不会把 +1 当基础值叠第二遍。
    again = Dnd5eCharacterSchema().pack(card)
    assert again["sheet"]["armorClass"] == 15
    assert again["armorClass"] == 16


def test_effect_bonus_reaches_derived_initiative_and_spell_dc(plugins) -> None:
    import copy

    sheet = copy.deepcopy(FIXTURE_SHEET)
    sheet["effect_changes"] = [
        _effect([{"key": "system.attributes.init.bonus", "mode": ec.MODE_ADD, "value": 5}], name="警觉"),
        _effect([{"key": "system.bonuses.spell.dc", "mode": ec.MODE_ADD, "value": 1}], name="法器"),
    ]
    derived = Dnd5eCharacterSchema().derive(sheet)
    assert derived["initiative"] == 9  # 敏捷 +4 + 5
    assert derived["spellSaveDC"] == 14  # 13 + 1
    assert derived["spellAttackBonus"] == 5  # DC 加值不碰攻击加值


def test_effect_bonus_reaches_passive_perception(plugins) -> None:
    import copy

    sheet = copy.deepcopy(FIXTURE_SHEET)
    sheet["effect_changes"] = [
        _effect(
            [{"key": "system.bonuses.skills.perception.check", "mode": ec.MODE_ADD, "value": 2}],
            name="鹰眼",
        )
    ]
    assert Dnd5eCharacterSchema().derive(sheet)["passivePerception"] == 15


# --- checks 接入 -----------------------------------------------------------


def _card_with(plugin, effects: list[dict], *, abilities: dict | None = None) -> dict:
    return plugin.schema.pack(
        {
            "id": "eff-check",
            "name": "皮普",
            "sheet": {
                "level": 5,
                "class": "战士",
                "abilities": abilities
                or {"str": 14, "dex": 12, "con": 12, "int": 10, "wis": 10, "cha": 10},
                "hp": {"current": 30, "max": 30},
                "effect_changes": effects,
            },
        }
    )


def test_check_adds_effect_bonus_and_reports_it(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [_effect([{"key": "system.bonuses.abilities.check", "mode": ec.MODE_ADD, "value": 2}])],
    )
    outcome = plugin.checks.roll_outcome(
        card,
        CheckRequest(kind="check", name="力量检定", target=15),
        rng=lambda sides: 10,
    )
    # 力量 14 → +2，效果再 +2 → 10 + 4 = 14
    assert outcome.total == 14
    assert outcome.detail["effect_modifier"] == 2


def test_check_without_effects_keeps_legacy_detail(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(plugin, [])
    outcome = plugin.checks.roll_outcome(
        card,
        CheckRequest(kind="check", name="力量检定", target=15),
        rng=lambda sides: 10,
    )
    assert outcome.total == 12
    assert "effect_modifier" not in outcome.detail


def test_check_roll_mode_grants_advantage_and_disadvantage(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    advantage_card = _card_with(
        plugin,
        [_effect([{"key": "system.abilities.str.check.roll.mode", "mode": ec.MODE_OVERRIDE, "value": 1}])],
    )
    disadvantage_card = _card_with(
        plugin,
        [_effect([{"key": "system.abilities.str.check.roll.mode", "mode": ec.MODE_OVERRIDE, "value": -1}])],
    )
    rolls = iter([20, 1])
    advantage = plugin.checks.roll_outcome(
        advantage_card,
        CheckRequest(kind="check", name="力量检定", target=15),
        rng=lambda sides: next(rolls),
    )
    assert len(advantage.dice) == 2 and advantage.detail["kept"] == 20
    rolls = iter([20, 1])
    disadvantage = plugin.checks.roll_outcome(
        disadvantage_card,
        CheckRequest(kind="check", name="力量检定", target=15),
        rng=lambda sides: next(rolls),
    )
    assert len(disadvantage.dice) == 2 and disadvantage.detail["kept"] == 1


def test_save_uses_effect_save_bonus(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [_effect([{"key": "system.bonuses.abilities.save", "mode": ec.MODE_ADD, "value": 1}])],
    )
    outcome = plugin.checks.roll_outcome(
        card,
        CheckRequest(kind="save", name="敏捷豁免", target=15),
        rng=lambda sides: 10,
    )
    # 敏捷 12 → +1，效果 +1 → 12
    assert outcome.total == 12
    assert outcome.detail["effect_modifier"] == 1


# --- combat 接入 -----------------------------------------------------------


def test_combatant_gets_effect_ac_and_resistances(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    ring = _effect([_ac_change(ec.MODE_ADD, 1)], name="防护戒指", origin={"source": "equipment"})
    cloak = _effect(
        [{"key": "system.traits.dr.value", "mode": ec.MODE_ADD, "value": ["fire"]}],
        name="耐火斗篷",
        origin={"source": "equipment"},
    )
    card = _card_with(plugin, [])
    card["sheet"]["armorClass"] = 15
    card["sheet"]["equipment"] = [
        _gear([ring], equipped=True),
        {"name": "耐火斗篷", "quantity": 1, "weight": 1.0, "notes": "披风", "equipped": True, "effects": [cloak]},
    ]
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert combatant["armorClass"] == 16
    assert "fire" in combatant["damage_resistances"]


def test_combatant_ac_untouched_without_effects(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(plugin, [])
    card["sheet"]["armorClass"] = 15
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert combatant["armorClass"] == 15


# --- statblock 接入 --------------------------------------------------------


def test_statblock_npc_applies_effect_ac(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    module = {
        "id": "npc_stone_guard",
        "title": "NPC：石肤守卫",
        "module_type": "npc",
        "card_type": "npc",
        "statblock": {
            "abilities": {"str": 16, "dex": 10, "con": 14, "int": 8, "wis": 10, "cha": 8},
            "armorClass": 14,
            "maxHp": 30,
            "attacks": [
                {
                    "name": "石拳",
                    "kind": "melee",
                    "attack_bonus": 5,
                    "damage_dice": "1d8+3",
                    "damage_type": "bludgeoning",
                }
            ],
            "effect_changes": [
                _effect([_ac_change(ec.MODE_ADD, 2)], name="石肤护符"),
                _effect(
                    [{"key": "system.traits.di.value", "mode": ec.MODE_ADD, "value": ["poison"]}],
                    name="石肤护符",
                ),
            ],
        },
    }
    spec = combatant_from_module(module)
    assert spec["armorClass"] == 16
    assert "poison" in spec["damage_immunities"]


def test_statblock_without_effects_keeps_pack_values(plugins) -> None:
    from dnd5e_plugin.statblock import combatant_from_module

    module = {
        "id": "npc_bugbear_plain",
        "title": "NPC：熊地精",
        "module_type": "npc",
        "card_type": "npc",
        "monster_id": "bugbear",
    }
    spec = combatant_from_module(module)
    assert spec["armorClass"] == 16
    assert spec["attacks"]


# --- 镜像 schema：路径 → 镜像值 --------------------------------------------
#
# 「完整镜像 + 通用路径求值」的账本：每条路径写进镜像的哪一格、什么类型、
# 落在哪一档。表里没有的路径族（真正的外来命名空间）才归 unsupported。

MIRROR_PATH_TABLE = [
    # (key, mode, value, 镜像路径, 期望镜像值, 分级)
    ("system.attributes.ac.bonus", ec.MODE_ADD, "+1", "system.attributes.ac.bonus", 1, ec.TIER_FULL),
    ("system.attributes.init.bonus", ec.MODE_ADD, 2, "system.attributes.init.bonus", 2, ec.TIER_FULL),
    ("system.attributes.ac.calc", ec.MODE_OVERRIDE, "mage", "system.attributes.ac.calc", "mage", ec.TIER_FULL),
    ("system.attributes.hp.bonus", ec.MODE_ADD, 5, "system.attributes.hp.bonus", 5, ec.TIER_FULL),
    ("system.attributes.movement.walk", ec.MODE_ADD, 10, "system.attributes.movement.walk", 10, ec.TIER_FULL),
    ("system.attributes.movement.fly", ec.MODE_ADD, 60, "system.attributes.movement.fly", 60, ec.TIER_FULL),
    ("system.attributes.movement.units", ec.MODE_OVERRIDE, "ft", "system.attributes.movement.units", "ft", ec.TIER_STORED),
    ("system.bonuses.mwak.attack", ec.MODE_ADD, 1, "system.bonuses.mwak.attack", 1, ec.TIER_FULL),
    ("system.bonuses.spell.dc", ec.MODE_ADD, 1, "system.bonuses.spell.dc", 1, ec.TIER_FULL),
    ("system.bonuses.abilities.save.dex", ec.MODE_ADD, 1, "system.bonuses.abilities.save.dex", 1, ec.TIER_FULL),
    ("system.bonuses.skills.stealth.check", ec.MODE_ADD, 2, "system.bonuses.skills.stealth.check", 2, ec.TIER_FULL),
    ("system.abilities.dex.check.roll.mode", ec.MODE_ADD, 1, "system.abilities.dex.check.roll.mode", 1, ec.TIER_FULL),
    ("system.abilities.wis.save.roll.mode", ec.MODE_ADD, -1, "system.abilities.wis.save.roll.mode", -1, ec.TIER_FULL),
    ("system.abilities.str.value", ec.MODE_ADD, 2, "system.abilities.str.value", 2, ec.TIER_FULL),
    ("system.attributes.prof", ec.MODE_ADD, 1, "system.attributes.prof", 1, ec.TIER_FULL),
    ("system.attributes.exhaustion", ec.MODE_ADD, 1, "system.attributes.exhaustion", 1, ec.TIER_STORED),
    ("system.traits.dr.value", ec.MODE_ADD, "fire", "system.traits.dr.value", ["fire"], ec.TIER_FULL),
    ("system.traits.ci.value", ec.MODE_ADD, "charmed", "system.traits.ci.value", ["charmed"], ec.TIER_FULL),
    ("system.traits.languages.value", ec.MODE_ADD, "druidic", "system.traits.languages.value", ["druidic"], ec.TIER_FULL),
    ("system.traits.size", ec.MODE_OVERRIDE, "lg", "system.traits.size", "lg", ec.TIER_STORED),
    ("system.details.biography.value", ec.MODE_OVERRIDE, "无名", "system.details.biography.value", "无名", ec.TIER_STORED),
    ("system.currency.gp", ec.MODE_ADD, 5, "system.currency.gp", 5, ec.TIER_STORED),
    ("system.resources.legact.max", ec.MODE_ADD, 1, "system.resources.legact.max", 1, ec.TIER_STORED),
    ("flags.midi-qol.someFlag", ec.MODE_OVERRIDE, True, "flags.midi-qol.someFlag", True, ec.TIER_STORED),
    ("flags.dnd5e.halflingLucky", ec.MODE_OVERRIDE, "true", "flags.dnd5e.halflingLucky", "true", ec.TIER_STORED),
    ("ATL.light.bright", ec.MODE_UPGRADE, 0, "ATL.light.bright", 0, ec.TIER_STORED),
    ("name", ec.MODE_OVERRIDE, "改过的名字", "name", "改过的名字", ec.TIER_STORED),
]


@pytest.mark.parametrize(("key", "mode", "value", "path", "expected", "tier"), MIRROR_PATH_TABLE)
def test_mirror_schema_writes_each_path(key, mode, value, path, expected, tier) -> None:
    view = ec.effective_sheet(_sheet([_effect([{"key": key, "mode": mode, "value": value}])]))
    reachable = [record for record in [*view.applied, *view.stored] if record["key"] == key]
    assert reachable, f"{key} 没有被镜像接住"
    assert reachable[0]["tier"] == tier, key
    assert reachable[0]["path"] == path, key
    assert ec.resolve_path(view.doc, path) == expected, key


@pytest.mark.parametrize(
    "key",
    [
        "system.dwarvish.forge.temperature",
        "system.attributes.nonsense",
        "system.traits.not-a-trait.value",
        "system.bonuses.mwak.not-a-bonus",
        "activities",
    ],
)
def test_paths_outside_the_mirror_are_unsupported(key) -> None:
    view = ec.effective_sheet(_sheet([_effect([{"key": key, "mode": ec.MODE_ADD, "value": 1}])]))
    assert view.stats["unsupported"] == 1
    assert view.stats["applied"] == 0 and view.stats["stored"] == 0
    assert ec.classify_path(key)[2] == ec.TIER_UNSUPPORTED


def test_five_modes_settle_on_the_mirror_in_fixed_order() -> None:
    """乘 → 加 → 取低 → 取高 → 覆盖：结果与挂载顺序无关。"""
    changes = [
        {"key": "system.attributes.hp.bonus", "mode": ec.MODE_MULTIPLY, "value": 2},
        {"key": "system.attributes.hp.bonus", "mode": ec.MODE_ADD, "value": 3},
        {"key": "system.attributes.hp.bonus", "mode": ec.MODE_DOWNGRADE, "value": 7},
        {"key": "system.attributes.hp.bonus", "mode": ec.MODE_UPGRADE, "value": 9},
        {"key": "system.attributes.hp.bonus", "mode": ec.MODE_OVERRIDE, "value": 11},
    ]
    forward = ec.effective_sheet(_sheet([_effect(list(changes))]))
    backward = ec.effective_sheet(_sheet([_effect(list(reversed(changes)))]))
    # override 最后应用 → 两条路径都是 11。
    assert forward.number("attributes.hp.bonus") == 11
    assert backward.number("attributes.hp.bonus") == 11


def test_mirror_records_stored_paths_without_touching_derived_values() -> None:
    """stored 只写镜像，不进任何派生值（empty 仍为真）。"""
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [
                        {"key": "system.attributes.movement.hover", "mode": ec.MODE_OVERRIDE, "value": True},
                        {"key": "ATL.light.dim", "mode": ec.MODE_ADD, "value": 10},
                        {"key": "flags.midi-qol.x", "mode": ec.MODE_OVERRIDE, "value": 1},
                    ]
                )
            ]
        )
    )
    assert view.empty  # 没有 full 级 change
    assert view.stats["stored"] == 3
    assert view.ac_bonus == 0 and view.initiative_bonus == 0
    # hover 是标志位：布尔文本/数字都可能，只断言被镜像收下且不是空视图。
    assert view.value("attributes.movement.hover") in (True, 1, "true")
    assert view.value("ATL.light.dim") == 10


# --- 新接的消费方：速度 / 生命上限 / 武器属性 / 条件免疫 --------------------


def test_movement_walk_feeds_the_single_speed_model(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    fast = _effect(
        [{"key": "system.attributes.movement.walk", "mode": ec.MODE_ADD, "value": 10}],
        name="脚程护符",
        origin={"source": "equipment"},
    )
    flying = _effect(
        [{"key": "system.attributes.movement.fly", "mode": ec.MODE_ADD, "value": 60}],
        name="飞行药水",
        origin={"source": "equipment"},
    )
    card = _card_with(plugin, [])
    card["sheet"]["speed"] = 30
    card["sheet"]["equipment"] = [
        _gear([fast], equipped=True),
        {"name": "飞行药水", "quantity": 1, "weight": 0.0, "notes": "药水", "equipped": True, "effects": [flying]},
    ]
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    # walk 进了单一速度模型；fly 只记录，不改 speed_ft（分速模型未建）。
    assert combatant["speed_ft"] == 40
    view = ec.effective_sheet(card["sheet"])
    assert view.number("attributes.movement.fly") == 60
    assert all(record["tier"] == ec.TIER_STORED for record in view.stored if "fly" in record["key"])


def test_speed_override_replaces_instead_of_adding(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [
            _effect(
                [{"key": "system.attributes.movement.walk", "mode": ec.MODE_OVERRIDE, "value": 15}],
                name="缓慢诅咒",
            )
        ],
    )
    card["sheet"]["speed"] = 30
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert combatant["speed_ft"] == 15


def test_hp_bonus_raises_max_hp_everywhere(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [_effect([{"key": "system.attributes.hp.bonus", "mode": ec.MODE_ADD, "value": 5}], name="坚韧")],
    )
    sheet = card["sheet"]
    base = dict(sheet)
    base.pop("effect_changes", None)
    # 卡面 hp.max 是基础值（不会被效果写脏），派生与战斗各自叠加。
    assert sheet["hp"]["max"] == 30
    assert plugin.schema.derive(sheet)["maxHp"] == 35
    assert plugin.schema.derive(base)["maxHp"] == 30
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert combatant["max_hp"] == 35


def test_item_scope_properties_and_damage_reach_the_weapon(plugins) -> None:
    """Shillelagh 形态：非 transfer 的物品效果改的是那件武器本身。"""
    plugin = builtin_registry().resolve("dnd5e")
    shillelagh = {
        "id": "shillelagh",
        "name": "Shillelagh",
        "transfer": False,
        "changes": [
            {"key": "system.properties", "mode": ec.MODE_ADD, "value": "finesse"},
            {"key": "system.damage.base.number", "mode": ec.MODE_OVERRIDE, "value": "1"},
            {"key": "system.damage.base.denomination", "mode": ec.MODE_OVERRIDE, "value": "8"},
        ],
        "origin": {"source": "spell", "equipped": True},
    }
    card = _card_with(plugin, [], abilities={"str": 14, "dex": 12, "con": 12, "int": 10, "wis": 10, "cha": 10})
    card["sheet"]["equipment"] = [
        {"name": "Mace", "quantity": 1, "weight": 4.0, "notes": "", "equipped": True, "effects": [shillelagh]}
    ]
    view = ec.effective_sheet(card["sheet"])
    assert view.stats["applied"] == 3 and view.stats["stored"] == 0
    weapon = view.weapon_row({"name": "Mace", "category": "weapon"})
    assert "finesse" in weapon["properties"]
    assert weapon["damage"]["dice"] == "1d8"

    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    row = combatant["attacks"][0]
    # 灵巧 → 用敏捷（+1）而不是力量（+2）；伤害骰被改成 1d8。
    assert row["ability"] == "dex"
    assert row["damage_dice"] == "1d8"


def test_item_scope_paths_on_an_actor_effect_are_stored_not_faked() -> None:
    """角色级（法术）效果带物品路径时没有目标武器：如实 stored。"""
    view = ec.effective_sheet(
        _sheet(
            [
                _effect(
                    [
                        {"key": "system.properties", "mode": ec.MODE_ADD, "value": "mgc"},
                        {"key": "system.damage.base.denomination", "mode": ec.MODE_OVERRIDE, "value": 8},
                    ],
                    name="Shillelagh",
                    transfer=True,
                )
            ]
        )
    )
    assert view.empty
    assert view.stats["stored"] == 2
    assert all(record["reason"] == "no-consumer" for record in view.stored)


def test_condition_immunity_blocks_the_condition(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [
            _effect(
                [{"key": "system.traits.ci.value", "mode": ec.MODE_ADD, "value": ["charmed", "frightened"]}],
                name="防护善恶",
            )
        ],
    )
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert set(combatant["condition_immunities"]) == {"charmed", "frightened"}
    blocked = plugin.effects.apply(combatant, {"condition": "charmed"})
    assert blocked.get("immune") is True
    assert blocked.get("effect") is None
    assert "免疫" in blocked["notes"][0]
    # 没有免疫的条件照常施加。
    allowed = plugin.effects.apply(combatant, {"condition": "poisoned"})
    assert allowed.get("error") is None and allowed.get("effect")


def test_condition_immunity_is_inert_without_effects(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(plugin, [])
    combatant = {"is_player": True, "name": "皮普"}
    plugin.combat.prepare_combatant(combatant, card=card)
    assert "condition_immunities" not in combatant
    assert plugin.effects.apply(combatant, {"condition": "charmed"}).get("effect")


# --- 卡面分组数据形状（效果页签） ------------------------------------------


def test_annotate_sheet_marks_every_change_for_the_ui(plugins) -> None:
    plugin = builtin_registry().resolve("dnd5e")
    card = _card_with(
        plugin,
        [
            _effect([_ac_change(ec.MODE_ADD, 1)], name="战斗风格：防御"),
            _effect(
                [
                    {"key": "flags.midi-qol.someFlag", "mode": ec.MODE_OVERRIDE, "value": True},
                    {"key": "system.dwarvish.forge", "mode": ec.MODE_ADD, "value": 1},
                ],
                name="模块残留",
            ),
        ],
    )
    changes = [change for entry in card["sheet"]["effect_changes"] for change in entry["changes"]]
    by_key = {change["key"]: change for change in changes}
    assert by_key["system.attributes.ac.bonus"]["applied"] == ec.TIER_FULL
    assert by_key["system.attributes.ac.bonus"]["active"] is True
    assert by_key["flags.midi-qol.someFlag"]["applied"] == ec.TIER_STORED
    assert by_key["flags.midi-qol.someFlag"]["reason"] == "no-consumer"
    assert by_key["system.dwarvish.forge"]["applied"] == ec.TIER_UNSUPPORTED
    # 装备行上的效果同样带分级（未装备 → active=False）。
    gear = _gear([_effect([_ac_change(ec.MODE_ADD, 1)], name="戒指")], equipped=False)
    packed = plugin.schema.pack(
        {
            "sheet": {
                "level": 1,
                "abilities": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
                "hp": {"current": 5, "max": 5},
                "equipment": [gear],
            }
        }
    )["sheet"]
    change = packed["equipment"][0]["effects"][0]["changes"][0]
    # 没装备 → 记录在案（stored），active=False，原因写明「未激活」。
    assert change["applied"] == ec.TIER_STORED and change["active"] is False
    assert change["reason"] == "inactive"


def test_annotate_sheet_leaves_effect_free_sheets_byte_identical() -> None:
    sheet = {"level": 5, "class": "游荡者", "hp": {"current": 10, "max": 10}, "equipment": []}
    before = repr(sheet)
    annotated = ec.annotate_sheet(sheet)
    assert "effect_changes" not in annotated
    assert repr(sheet) == before  # 不就地改写调用方的 dict
