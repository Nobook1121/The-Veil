"""边界收敛 D1：CoC 战斗语义迁入插件（六钩子 + SAN 槽位驱动 + 伤势软化）。

D1 迁移内容：``agents/tools/combat.py`` 里的 CoC 战斗常量与数学——徒手武器
条目（``UNARMED_WEAPON``）、极限成功的伤害合成（满骰面 / 贯穿追加）、全自动
命中配比、射击卡壳判定、先攻检定的数值合成、NPC 缺省理智（SAN 起始 = POW）
——整块下沉到 ``combat/rulesets/coc7.py`` 的 ``Coc7Ruleset``；核心只保留
``combat/registry`` 的中性取用点，规则集缺席钩子时按中性语义降级。战斗 SAN
读写同时改按规则集 ``san`` 资源槽位驱动：未声明理智资源的规则集（dnd5e 只有
hp 槽位）战斗者不出现 ``san`` 字段，持久化也不写 ``current_san``。伤势状态
（``DamageOutcome``）的 ``major_wound / dying / unconscious / dead`` 软化为
``bool | None``：插件没有该概念时置 ``None``，唯一消费方按 ``False`` 呈现。

本文件用真插件装配 + ``TRPG_RULESET_DEFAULT=dnd5e`` 复现最严苛环境（与 B1
``test_ruleset_b1_combat_context.py`` 同源手法），验收：

1. coc7 六钩子逐值锚定：``unarmed_weapon()`` 逐字等于迁移后的真源常量；两条
   极限伤害文案逐字（非贯穿「极限成功·满伤害（…）」与贯穿「极限成功·贯穿
   满伤害并追加一次武器伤害骰 {terms}」）；全自动常规半数命中、极难全中且
   前半贯穿；卡壳「骰值 ≥ 故障值」，None/0/N/A 恒不卡；先攻 hard/DEX60 →
   2060（与 ``test_combat_golden`` 金样口径一致）；缺省理智 = POW。
2. 缺席中性降级：未声明钩子的规则集（dnd5e 真插件）与空注册表两种缺席形态
   下，``combat/registry`` 各取用函数全部退中性——无贯穿追加、恒不卡壳、
   中性徒手条目、先攻走核心默认等级表、缺省理智 0。
3. SAN 槽位驱动：coc7 房间战斗者带 ``san`` 且伤害结算后持久化写
   ``current_san`` 与卡面镜像；dnd5e 房间战斗者没有 ``san`` 字段、持久化
   不写理智键（真插件装配 + FakeContext 驱动 start_combat / damage）。
4. ``DamageOutcome`` 伤势四字段 ``None`` 判空：字段语义 + 走 ``_apply_damage``
   的判空呈现分支，不崩、按 ``False`` 呈现。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from trpg_server.agents.tools import combat
from trpg_server.combat.contracts import (
    DEFAULT_LEVEL_RANK,
    DEFAULT_UNARMED_WEAPON,
    DamageOutcome,
)
from trpg_server.combat.registry import (
    CombatRulesetUnavailable,
    auto_fire_composition,
    extreme_damage,
    get_ruleset,
    initiative_score,
    initial_sanity,
    unarmed_weapon,
    weapon_malfunction,
)
from trpg_server.combat.rulesets.coc7 import UNARMED_WEAPON as COC7_UNARMED_WEAPON
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import (
    builtin_registry,
    default_ruleset_id,
    reset_builtin_registry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
HINT_ENV_KEY = "TRPG_RULESET_DEFAULT"


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def dnd5e_default_process(monkeypatch):
    """真插件装配 + 进程默认规则集注入 dnd5e（复现 R1 缺陷环境）。

    与 B1 同源：``TRPG_RULESET_DEFAULT`` 是运维临时覆盖（优先级高于配置
    文件），重置注册表后下一次 ``builtin_registry()`` 以 dnd5e 为进程默认
    重建。D1 的钩子与 SAN 槽位都按**战斗会话规则集**显式解析，本环境保证
    任何跟随进程默认的路径都会露馅。
    """
    registry = _load_real_plugins()
    monkeypatch.setenv(HINT_ENV_KEY, "dnd5e")
    reset_builtin_registry()
    assert default_ruleset_id() == "dnd5e"
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture
def empty_ruleset_registry(monkeypatch):
    """空插件注册表（P0 手法：插件模块清单显式置空 + 清第三方运行时）。

    空注册表是「规则集缺席」的最彻底形态：``get_ruleset`` 抛
    ``CombatRulesetUnavailable``，``combat/registry`` 的取用函数必须全部退
    中性而不是抛错或回落 coc7。
    """
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    yield builtin_registry()
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class FakeContext:
    """战斗工具的最小房间上下文；``ruleset_id`` 决定房间规则集。"""

    def __init__(self, tmp_path: Path, ruleset_id: str, members: list | None = None):
        self.room_dir = tmp_path
        self.room_id = f"d1-{ruleset_id}"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._ruleset_id = ruleset_id
        self._members = members or []

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": self._ruleset_id,
            "house_rules": {},
            "members": self._members,
        }


def _spec(name: str, **overrides) -> dict:
    spec = {
        "name": name,
        "is_npc": True,
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 10,
        "max_hp": 10,
        "skills": {"Fighting (Brawl)": 50},
        "weapons": [
            {"name": "Knife", "skill": "Fighting (Brawl)", "damage": "1D4+2", "attacks": "1"},
        ],
    }
    spec.update(overrides)
    return spec


class SeqRng:
    """按固定序列返回骰值的确定性随机源（超出骰面时夹紧）。"""

    def __init__(self, values: list[int]):
        self.values = list(values)
        self.calls: list[tuple[int, int]] = []

    def __call__(self, sides: int) -> int:
        value = self.values[len(self.calls) % len(self.values)]
        self.calls.append((sides, value))
        return max(1, min(int(sides), int(value)))


# -- 徒手武器条目（Slice 1）-------------------------------------------------


def test_coc7_unarmed_weapon_entry_matches_legacy(dnd5e_default_process):
    """coc7 徒手条目逐字等于真源常量；CoC 专精语义完整；每次返回独立副本。"""
    entry = unarmed_weapon("coc7")
    assert entry == COC7_UNARMED_WEAPON
    # CoC 语义锚定：格斗（斗殴）专精、1D3、无贯穿 / 卡壳语义。
    assert entry["name"] == "徒手"
    assert entry["skill"] == "格斗（斗殴）"
    assert entry["skillKey"] == "fighting"
    assert entry["specialtyKey"] == "brawl"
    assert entry["damage"] == "1D3"
    assert entry["range"] == "接触"
    assert entry["impale"] is False
    assert entry["malfunction"] == "N/A"
    # 副本独立性：调用方改条目不污染规则集真源。
    entry["name"] = "被改坏的条目"
    assert unarmed_weapon("coc7") == COC7_UNARMED_WEAPON
    assert COC7_UNARMED_WEAPON["name"] == "徒手"


def test_unarmed_weapon_falls_back_to_neutral_without_hook(dnd5e_default_process):
    """dnd5e（真插件、未声明钩子）退中性徒手：无专精键、无贯穿 / 卡壳语义。"""
    entry = unarmed_weapon("dnd5e")
    assert entry == DEFAULT_UNARMED_WEAPON
    assert entry["skillKey"] == ""
    assert entry["specialtyKey"] == ""
    assert entry["impale"] is False
    assert entry["malfunction"] == "N/A"
    # 稳定性：重复取用逐位一致（攻击 / 反击链路每次都查规则集）。
    assert unarmed_weapon("dnd5e") == DEFAULT_UNARMED_WEAPON


# -- 极限成功的伤害合成 -----------------------------------------------------


def test_coc7_extreme_damage_full_face_without_impale(dnd5e_default_process):
    """非贯穿：满骰面 + DB 满骰；文案「极限成功·满伤害（…）」逐字。"""
    calls: list[list] = []

    def roll_damage(terms):
        calls.append(list(terms))
        return 99, []  # 若被误调用会把数值带崩，由断言捕获

    # 1D4+2 + DB(1D4)：4 + 2 + 4 = 10。
    total, detail = extreme_damage(
        [(1, 4)], 2, [(1, 4)],
        raw="1D4+2", impale=False, allow_impale=True,
        roll_damage=roll_damage, ruleset_id="coc7",
    )
    assert (total, detail) == (10, "极限成功·满伤害（1D4+2 +DB）")
    # 无 DB 时文案无「+DB」后缀；非贯穿恒不追加伤害骰。
    total, detail = extreme_damage(
        [(1, 6)], 0, [],
        raw="1D6", impale=False, allow_impale=True,
        roll_damage=roll_damage, ruleset_id="coc7",
    )
    assert (total, detail) == (6, "极限成功·满伤害（1D6）")
    assert calls == []


def test_coc7_extreme_damage_impale_adds_weapon_dice(dnd5e_default_process):
    """贯穿：满骰面 + 追加一次武器伤害骰；文案逐字；骰项原样转交。"""
    calls: list[list] = []

    def roll_damage(terms):
        calls.append(list(terms))
        return 3, []  # 固定追加骰 3

    # 1D10+1 + DB(1D4)：10 + 1 + 3（追加） + 4 = 18。
    total, detail = extreme_damage(
        [(1, 10)], 1, [(1, 4)],
        raw="1D10+1", impale=True, allow_impale=True,
        roll_damage=roll_damage, ruleset_id="coc7",
    )
    assert total == 18
    assert detail == "极限成功·贯穿满伤害并追加一次武器伤害骰 [(1, 10)]"
    # 追加骰按武器骰项掷，不含 DB。
    assert calls == [[(1, 10)]]
    # 贯穿门由调用方控制：allow_impale=False 时退非贯穿文案，不追加。
    calls.clear()
    total, detail = extreme_damage(
        [(1, 10)], 1, [],
        raw="1D10+1", impale=True, allow_impale=False,
        roll_damage=roll_damage, ruleset_id="coc7",
    )
    assert (total, detail) == (11, "极限成功·满伤害（1D10+1）")
    assert calls == []


def test_extreme_damage_falls_back_to_neutral_without_hook(dnd5e_default_process):
    """缺席钩子（dnd5e）：极难也只取满骰面，无贯穿追加。"""
    calls: list[list] = []

    def roll_damage(terms):
        calls.append(list(terms))
        return 99, []

    total, detail = extreme_damage(
        [(1, 10)], 1, [(1, 4)],
        raw="1D10+1", impale=True, allow_impale=True,
        roll_damage=roll_damage, ruleset_id="dnd5e",
    )
    assert (total, detail) == (15, "极限成功·满伤害（1D10+1 +DB）")
    assert calls == []


# -- 全自动的命中 / 贯穿配比 -------------------------------------------------


def test_coc7_auto_fire_composition_regular_and_extreme(dnd5e_default_process):
    """全自动配比：常规半数命中；极难全中且前半贯穿（p97-98）。"""
    # 常规成功：半数命中（整除），不贯穿。
    assert auto_fire_composition(10, False, 0, "coc7") == (5, 0)
    # 单发也保底命中 1。
    assert auto_fire_composition(1, False, 0, "coc7") == (1, 0)
    # 常规 / 困难难度带（rank 0/1）极难成功：全中且前半贯穿。
    assert auto_fire_composition(10, True, 0, "coc7") == (10, 5)
    assert auto_fire_composition(10, True, 1, "coc7") == (10, 5)
    # 极难难度带（rank 2）下极难成功：全中但不贯穿（迁移实现逐位）。
    assert auto_fire_composition(10, True, 2, "coc7") == (10, 0)


def test_auto_fire_composition_falls_back_to_neutral(dnd5e_default_process):
    """缺席钩子：全中且不贯穿（极难成功也不给贯穿配比）；弹药 0 保底 1。"""
    assert auto_fire_composition(10, False, 1, "dnd5e") == (10, 0)
    assert auto_fire_composition(10, True, 1, "dnd5e") == (10, 0)
    assert auto_fire_composition(0, True, 1, "dnd5e") == (1, 0)


# -- 射击卡壳 ---------------------------------------------------------------


def test_coc7_weapon_malfunction_threshold_semantics(dnd5e_default_process):
    """卡壳判定：骰值 ≥ 故障值即卡（含等于）；None / 0 / N/A 恒不卡。"""
    assert weapon_malfunction(96, 96, "coc7") is True
    assert weapon_malfunction(100, 96, "coc7") is True
    assert weapon_malfunction(95, 96, "coc7") is False
    # 故障值缺失（目录 N/A 解析为 None）或为 0：恒不卡壳。
    assert weapon_malfunction(100, None, "coc7") is False
    assert weapon_malfunction(100, 0, "coc7") is False
    assert weapon_malfunction(100, "N/A", "coc7") is False


def test_weapon_malfunction_falls_back_to_never_jam(dnd5e_default_process):
    """缺席钩子：即使故障值 1、骰值 100 也不卡壳（中性 = 恒不卡）。"""
    assert weapon_malfunction(100, 1, "dnd5e") is False
    assert weapon_malfunction(100, 96, "dnd5e") is False


# -- 先攻检定的数值合成 -----------------------------------------------------


def test_coc7_initiative_score_matches_combat_golden_band(dnd5e_default_process):
    """先攻数值 = 等级 ×1000 + DEX；hard/60 → 2060（B1 用例与金样同口径）。"""
    assert initiative_score("hard", 60, "coc7") == 2060
    # 等级表全档位（coc7 LEVEL_RANK）。
    assert initiative_score("regular", 40, "coc7") == 1040
    assert initiative_score("extreme", 40, "coc7") == 3040
    assert initiative_score("critical", 40, "coc7") == 4040
    assert initiative_score("fumble", 40, "coc7") == -960
    # 未知等级按 0 档；DEX 缺省按 0。
    assert initiative_score("unknown", 40, "coc7") == 40
    assert initiative_score("hard", None, "coc7") == 2000


def test_initiative_score_falls_back_to_neutral_band(dnd5e_default_process):
    """缺席钩子：走核心默认等级表（数值合成口径不变，等级来源是默认表）。"""
    assert initiative_score("hard", 60, "dnd5e") == DEFAULT_LEVEL_RANK["hard"] * 1000 + 60
    assert initiative_score("critical", 99, "dnd5e") == 4099
    # 未知等级在默认表同样按 0 档。
    assert initiative_score("unknown", 99, "dnd5e") == 99


# -- NPC 缺省理智 -----------------------------------------------------------


def test_coc7_initial_sanity_defaults_to_pow(dnd5e_default_process):
    """缺省理智 = POW；空值与负值钳 0。"""
    assert initial_sanity(77, "coc7") == 77
    assert initial_sanity(0, "coc7") == 0
    assert initial_sanity(None, "coc7") == 0
    assert initial_sanity(-5, "coc7") == 0


def test_initial_sanity_falls_back_to_zero(dnd5e_default_process):
    """缺席钩子：缺省理智 0（该钩子只在声明了 san 槽位的规则集才会被调用）。"""
    assert initial_sanity(90, "dnd5e") == 0


# -- 空注册表：全部取用函数退中性 -------------------------------------------


def test_registry_neutral_fallbacks_with_empty_registry(empty_ruleset_registry):
    """空注册表（最彻底的缺席）：六个取用函数全部中性，不抛错、不回落 coc7。"""
    registry = empty_ruleset_registry
    assert registry.ids() == []
    # 战斗实现同样不可用（P0 明确错误语义，不回落任何默认）。
    with pytest.raises(CombatRulesetUnavailable):
        get_ruleset("coc7")

    # 徒手：中性条目（显式 id 缺席与无 id 同路）。
    assert unarmed_weapon(None) == DEFAULT_UNARMED_WEAPON
    assert unarmed_weapon("coc7") == DEFAULT_UNARMED_WEAPON

    # 极限伤害：满骰面、无贯穿追加（impale=True 也不追加）。
    calls: list[list] = []

    def roll_damage(terms):
        calls.append(list(terms))
        return 99, []

    total, detail = extreme_damage(
        [(1, 8)], 0, [],
        raw="1D8", impale=True, allow_impale=True,
        roll_damage=roll_damage, ruleset_id=None,
    )
    assert (total, detail) == (8, "极限成功·满伤害（1D8）")
    assert calls == []

    # 卡壳：恒不卡；全自动：全中不贯穿；先攻：默认等级表；理智：0。
    assert weapon_malfunction(100, 1, None) is False
    assert auto_fire_composition(10, True, 1, None) == (10, 0)
    assert initiative_score("hard", 60, None) == DEFAULT_LEVEL_RANK["hard"] * 1000 + 60
    assert initiative_score("critical", 99, "coc7") == 4099
    assert initial_sanity(90, None) == 0


# -- SAN 槽位驱动（真插件装配 + start_combat 战斗快照）----------------------

COC7_MEMBER = {
    "user_id": "u-wendy",
    "username": "player-1",
    "character_card": {
        "name": "Wendy",
        "attributes": {"STR": 50, "CON": 70, "SIZ": 60, "DEX": 55, "POW": 60, "LUCK": 50},
        "currentHp": 13,
        "maxHp": 13,
        "currentSan": 60,
    },
}

DND5E_MEMBER = {
    "user_id": "u-hero",
    "username": "player-2",
    "character_card": {
        "name": "Hero",
        "rulesetId": "dnd5e",
        "level": 3,
        "abilities": {"str": 10, "dex": 14, "con": 12, "int": 13, "wis": 11, "cha": 12},
        "proficiencies": {},
        "currentHp": 15,
        "maxHp": 15,
    },
}


def test_coc7_room_combatants_carry_san_and_persist_current_san(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """coc7 房间：战斗者带 san；伤害结算后持久化写 current_san 与卡面镜像。"""
    context = FakeContext(tmp_path, "coc7", members=[deepcopy(COC7_MEMBER)])
    monkeypatch.setattr(combat, "_roller", SeqRng([2]))
    started = combat.start_combat(
        {"participants": ["Wendy", _spec("Cultist", pow=70)]}, context
    )
    assert "error" not in started
    state = combat._load_combat(context)
    # 玩家战斗者：理智按 san 槽位读取（房间状态优先，缺省回落卡面镜像 60）。
    assert state["combatants"]["Wendy"]["san"] == 60
    # NPC 战斗者：未显式给出理智时按规则集约定起始（coc7：SAN 起始 = POW）。
    assert state["combatants"]["Cultist"]["san"] == 70

    # 伤害结算触发玩家状态回写（_apply_damage → _persist_player_state → info.json）。
    damaged = combat.damage_combatant({"target": "Wendy", "dice": "1D3"}, context)
    assert "error" not in damaged
    info = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    member = info["members"][0]
    saved = member["character_state"]
    assert saved["current_san"] == 60
    card = member["character_card"]
    # 卡面镜像按槽位声明写主镜像与历史别名。
    assert card["currentSan"] == 60
    assert card["current_san"] == 60
    # 理智首次进入房间快照（None → 60）时并入统一待写回队列，type = 槽位 kind。
    san_changes = [
        row for row in saved.get("pending_changes", []) if row.get("type") == "san"
    ]
    assert san_changes and san_changes[0]["value"] == 60


def test_dnd5e_room_combatants_have_no_san_and_skip_persistence(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """dnd5e 房间（未声明 san 槽位）：战斗者没有 san 字段，持久化不写理智键。"""
    context = FakeContext(tmp_path, "dnd5e", members=[deepcopy(DND5E_MEMBER)])
    # 先攻掷 d20（两名战斗者各一颗），伤害骰 1D3 夹紧到 3。
    monkeypatch.setattr(combat, "_roller", SeqRng([15, 2]))
    started = combat.start_combat(
        {"participants": ["Hero", _spec("Grunt", pow=70)]}, context
    )
    assert "error" not in started
    state = combat._load_combat(context)
    # 战斗者快照：玩家与 NPC 都不出现 CoC 的 san 字段。
    assert "san" not in state["combatants"]["Hero"]
    assert "san" not in state["combatants"]["Grunt"]

    damaged = combat.damage_combatant({"target": "Hero", "dice": "1D3"}, context)
    assert "error" not in damaged
    info = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    member = info["members"][0]
    saved = member["character_state"]
    # 房间状态与卡面都不写 CoC 的理智键。
    assert "current_san" not in saved
    card = member["character_card"]
    assert "currentSan" not in card
    assert "current_san" not in card
    # 也不进统一待写回队列（理智分支整体跳过）。
    san_changes = [
        row for row in saved.get("pending_changes", []) if row.get("type") == "san"
    ]
    assert san_changes == []


# -- DamageOutcome 伤势软化（bool | None 判空） ------------------------------


class _BareOutcomeRuleset:
    """只回报事实、没有伤势概念的占位伤害模型（供判空分支验收）。"""

    ruleset_id = "bare-outcome"

    def resolve_damage(
        self,
        combatant,
        amount,
        *,
        armor,
        ignore_armor,
        roll_d100,
        thresholds,
        settings,
    ):
        before = int(combatant.get("hp") or 0)
        return DamageOutcome(
            damage=int(amount),
            armor=int(armor),
            effective=int(amount),
            hp_before=before,
            hp_after=before - int(amount),
            major_wound=None,
            dying=None,
            unconscious=None,
            dead=None,
        )


def test_damage_outcome_none_status_fields_render_as_false(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """伤势四字段软化 None：字段语义 None；_apply_damage 判空按 False 呈现。"""
    # 字段语义：软化后默认 None——不强制没有伤势概念的伤害模型塞假布尔。
    outcome = DamageOutcome()
    for field in ("major_wound", "dying", "unconscious", "dead"):
        assert getattr(outcome, field) is None
        assert bool(getattr(outcome, field)) is False

    # 走唯一消费方 _apply_damage 的判空分支：None → False，键恒存在、不崩。
    monkeypatch.setattr(combat, "get_ruleset", lambda _ruleset_id: _BareOutcomeRuleset())
    context = FakeContext(tmp_path, "coc7")
    combatant = {
        "name": "Ghost",
        "is_player": False,
        "ruleset": "bare-outcome",
        "hp": 10,
        "max_hp": 10,
        "armor": 0,
    }
    result = combat._apply_damage(context, combatant, 4)
    assert result["effective"] == 4
    assert result["hp_before"] == 10 and result["hp_after"] == 6
    assert result["major_wound"] is False
    assert result["dying"] is False
    assert result["unconscious"] is False
    assert result["dead"] is False
