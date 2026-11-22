"""建卡向导计算（dnd5e_plugin.builder）：购点/掷骰/标准数组/种族加值/HP 与派生一致性。

这一份是建卡规则的**权威口径**：前端向导的实时预览只是同一套数字的镜像，
保存后的卡面派生走 ``Dnd5eCharacterSchema.derive``，两者必须逐项相等。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
if str(PLUGIN_ROOT / "dnd5e") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "dnd5e"))

from dnd5e_plugin import builder  # noqa: E402  (需先准备插件运行时)
from dnd5e_plugin.schema import Dnd5eCharacterSchema  # noqa: E402


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture()
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class _FixedRng:
    """固定骰面的 rng：``randint`` 依次吐预设值，用于验证 4d6 去最低。"""

    def __init__(self, values: list[int]) -> None:
        self._values = list(values)

    def randint(self, low: int, high: int) -> int:  # noqa: ARG002 - 契约要两个入参
        value = self._values.pop(0)
        assert low <= value <= high, f"预设骰面 {value} 超出 d{high}"
        return value


# --- 购点 -------------------------------------------------------------------


def test_point_buy_cost_table_matches_srd():
    assert builder.POINT_BUY_COST == {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}
    assert builder.POINT_BUY_BUDGET == 27
    assert [builder.point_buy_cost(score) for score in range(8, 16)] == [0, 1, 2, 3, 4, 5, 7, 9]


def test_point_buy_clamps_out_of_range():
    assert builder.clamp_point_buy(3) == 8
    assert builder.clamp_point_buy(18) == 15
    assert builder.point_buy_cost(20) == builder.point_buy_cost(15) == 9
    assert builder.point_buy_cost("bad") == 0


def test_point_buy_budget_accounting():
    floor = {key: 8 for key in builder.ABILITY_KEYS}
    assert builder.point_buy_spent(floor) == 0
    assert builder.point_buy_remaining(floor) == 27
    # 15/15/15/8/8/8 恰好花掉 27 分，是购点模式的上限组合。
    maxed = {"str": 15, "dex": 15, "con": 15, "int": 8, "wis": 8, "cha": 8}
    assert builder.point_buy_spent(maxed) == 27
    assert builder.point_buy_remaining(maxed) == 0
    # 超支可以是负余额，UI 需要它来标红。
    overspent = {key: 15 for key in builder.ABILITY_KEYS}
    assert builder.point_buy_remaining(overspent) < 0


def test_point_buy_affordable_blocks_over_budget_and_cap():
    maxed = {"str": 15, "dex": 15, "con": 15, "int": 8, "wis": 8, "cha": 8}
    assert builder.point_buy_affordable(maxed, "int") is False  # 预算已花完
    assert builder.point_buy_affordable(maxed, "str") is False  # 已到 15 上限
    spare = {"str": 10, "dex": 10, "con": 10, "int": 8, "wis": 8, "cha": 8}
    assert builder.point_buy_affordable(spare, "cha") is True


# --- 标准数组 ---------------------------------------------------------------


def test_standard_array_values():
    assert builder.STANDARD_ARRAY == (15, 14, 13, 12, 10, 8)
    assert [builder.standard_array(index) for index in range(6)] == [15, 14, 13, 12, 10, 8]
    assert builder.standard_array(99) == 8
    assert builder.standard_array(-5) == 15
    # 标准数组不走购点：它比 27 分买到的组合更强，这是 5e 两条独立路径。
    bought = {"str": 15, "dex": 13, "con": 12, "int": 10, "wis": 8, "cha": 8}
    assert builder.point_buy_spent(bought) <= builder.POINT_BUY_BUDGET


# --- 掷骰 -------------------------------------------------------------------


def test_roll_4d6_drop_lowest_drops_the_lowest_die():
    assert builder.roll_4d6_drop_lowest([6, 6, 6, 1]) == 18
    assert builder.roll_4d6_drop_lowest([1, 2, 3, 4]) == 9
    assert builder.roll_4d6_drop_lowest([1, 1, 1, 1]) == 3
    assert builder.roll_4d6_drop_lowest([6, 1, 6, 1]) == 13


def test_roll_pool_returns_six_groups():
    pool = builder.roll_pool(_FixedRng([6, 6, 6, 1] * 6))
    assert pool == [18] * 6
    assert builder.ROLL_POOL_SIZE == 6
    assert len(builder.roll_pool(random.Random(7))) == 6
    # 每组成绩都在 3d6 的合法区间内。
    assert all(3 <= value <= 18 for value in builder.roll_pool(random.Random(11)))


# --- 种族加值 ---------------------------------------------------------------


def test_race_bonuses_stack_on_base_scores():
    base = {"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8}
    totals = builder.with_race_bonuses(base, {"str": 2, "cha": 1})
    assert totals == {"str": 17, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 9}
    # 加值不写进制上：原字典保持不变，UI 要能同时展示「基础 / 总值」。
    assert base["str"] == 15


def test_race_bonuses_ignore_unknown_keys_and_cap_at_thirty():
    totals = builder.with_race_bonuses({"str": 19, "dex": 10}, {"str": 5, "luck": 3, "wis": "bad"})
    assert totals["str"] == 24
    assert totals["dex"] == 10
    assert "luck" not in totals
    assert builder.with_race_bonuses({"str": 29}, {"str": 5})["str"] == 30


def test_normalize_bonuses_rejects_junk():
    assert builder.normalize_bonuses({"STR": 2, "dex": "1", "cha": 0, "x": 2}) == {"str": 2, "dex": 1}
    assert builder.normalize_bonuses(None) == {}


# --- 1 级 HP / 派生 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("hit_die", "con_mod", "expected"),
    [(12, 2, 14), (10, 0, 10), (6, -1, 5), (8, 3, 11), (6, -4, 2)],
)
def test_level_one_hit_points(hit_die, con_mod, expected):
    assert builder.level_one_hit_points(hit_die, con_mod) == expected


def test_passive_perception_and_armor_class():
    scores = {"str": 10, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8}
    assert builder.passive_perception(scores, proficient=False) == 11
    assert builder.passive_perception(scores, proficient=True) == 13
    assert builder.passive_perception(scores, expertise=True) == 15
    assert builder.armor_class_from_dex(scores["dex"]) == 12


def test_spellcasting_preview_only_for_casters():
    scores = {"str": 8, "dex": 12, "con": 14, "int": 16, "wis": 10, "cha": 13}
    preview = builder.spellcasting_preview(scores, "int")
    assert preview == {"spellSaveDC": 8 + 2 + 3, "spellAttackBonus": 2 + 3}
    assert builder.spellcasting_preview(scores, "") is None
    assert builder.spellcasting_preview(scores, "luck") is None
    assert builder.spell_slots_at_level_one(caster=True) == {"1": {"max": 2, "used": 0}}
    assert builder.spell_slots_at_level_one(caster=True, pact_caster=True) == {"1": {"max": 1, "used": 0}}
    assert builder.spell_slots_at_level_one(caster=False) == {}


# --- 产出 sheet 与卡面派生一致 ----------------------------------------------


def _wizard_sheet(**overrides):
    payload = dict(
        race="矮人",
        class_name="战士",
        background="士兵",
        abilities={"str": 17, "dex": 14, "con": 15, "int": 10, "wis": 12, "cha": 8},
        hit_die=10,
        skills=["Athletics", "perception", "历史"],
        saves=["str", "con"],
        armor=["light", "medium", "heavy", "shield"],
        weapons=["simple", "martial"],
        equipment=[{"name": "链甲", "quantity": 1, "weight": 55}],
        speed=25,
    )
    payload.update(overrides)
    return builder.build_sheet(**payload)


def test_build_sheet_writes_english_ids_and_level_one_hp(plugins):
    sheet = _wizard_sheet()
    assert sheet["proficiencies"]["skills"] == ["athletics", "perception", "history"]
    assert sheet["proficiencies"]["saves"] == ["str", "con"]
    assert sheet["level"] == 1
    assert sheet["xp"] == 0
    # d10 + 体质 +2 = 12；卡面命中的是同一份数值。
    assert sheet["hp"] == {"max": 12, "current": 12, "temp": 0}
    assert sheet["hitDice"] == "1d10"
    assert sheet["hit_dice"] == {"die": 10, "max": 1, "used": 0}
    assert sheet["speed"] == 25
    assert sheet["equipment"] == [{"name": "链甲", "quantity": 1, "weight": 55, "equipped": False}]
    # 无甲 AC 交给 schema 派生，向导不写死，避免改了敏捷 AC 不动。
    assert "armorClass" not in sheet
    assert sheet["spellcasting"]["ability"] == ""
    assert sheet["spellcasting"]["slots"] == {}


def test_preview_matches_schema_derive_for_all_three_modes(plugins):
    """预警：预览与保存后派生必须逐项一致——三条分配路径各建一张卡对照。"""
    schema = Dnd5eCharacterSchema()
    point_buy = _wizard_sheet(
        abilities={"str": 15, "dex": 14, "con": 13, "int": 12, "wis": 10, "cha": 8},
    )
    standard = _wizard_sheet(
        abilities={"str": 13, "dex": 15, "con": 14, "int": 10, "wis": 12, "cha": 8},
    )
    rolled = _wizard_sheet(
        abilities={"str": 18, "dex": 16, "con": 15, "int": 9, "wis": 13, "cha": 11},
    )
    for sheet in (point_buy, standard, rolled):
        quick = builder.preview(sheet)
        derived = schema.derive(dict(sheet))
        assert quick["maxHp"] == derived["maxHp"]
        assert quick["armorClass"] == derived["armorClass"]
        assert quick["proficiencyBonus"] == derived["proficiencyBonus"]
        assert quick["initiative"] == derived["initiative"]
        assert quick["passivePerception"] == derived["passivePerception"]
        assert quick["spellSaveDC"] == derived.get("spellSaveDC")
        assert quick["spellAttackBonus"] == derived.get("spellAttackBonus")


def test_preview_matches_schema_derive_for_caster(plugins):
    schema = Dnd5eCharacterSchema()
    sheet = _wizard_sheet(
        class_name="法师",
        hit_die=6,
        abilities={"str": 8, "dex": 14, "con": 13, "int": 16, "wis": 12, "cha": 10},
        skills=["arcana", "investigation"],
        saves=["int", "wis"],
        spellcasting_ability="int",
        caster=True,
        speed=30,
    )
    assert sheet["spellcasting"]["slots"] == {"1": {"max": 2, "used": 0}}
    quick = builder.preview(sheet)
    derived = schema.derive(dict(sheet))
    assert quick["maxHp"] == 7  # d6 + 体质 +1
    assert quick["spellSaveDC"] == derived["spellSaveDC"] == 13  # 8 + 2 + 3
    assert quick["spellAttackBonus"] == derived["spellAttackBonus"] == 5
    assert quick["passivePerception"] == derived["passivePerception"] == 11
    # 打包后卡面才会有显式 AC，值仍是 10 + 敏捷调整值。
    packed = schema.pack({"name": "向导法师", "sheet": dict(sheet)})
    assert packed["sheet"]["armorClass"] == quick["armorClass"] == 12
    assert packed["sheet"]["spellcasting"]["slots"] == {"1": {"max": 2, "used": 0}}
    assert packed["maxHp"] == quick["maxHp"]
    assert packed["spellSaveDC"] == quick["spellSaveDC"]


def test_build_sheet_drops_unknown_skill_and_keeps_background_skills(plugins):
    sheet = _wizard_sheet(skills=["athletics", "不存在的技能", "animal handling", "animal handling"])
    assert sheet["proficiencies"]["skills"] == ["athletics", "animal-handling"]


def test_normalize_skill_id_accepts_old_chinese_and_spaced_forms():
    assert builder.normalize_skill_id("Animal Handling") == "animal-handling"
    assert builder.normalize_skill_id("sleight_of_hand") == "sleight-of-hand"
    assert builder.normalize_skill_id("察觉") == "perception"
    assert builder.normalize_skill_id("") == ""
