"""边界收敛 B1：战斗掷骰 / 成功等级按「战斗会话规则集」解析（R1 验收）。

R1 缺陷回顾：进程默认规则集配置成非 coc7（本文件注入 dnd5e）时，战斗工具的
掷骰策略（``RollPolicy``）、成功等级带（``success_level``）、等级排序
（``level_rank``）与插件内 CON 检定都静默跟随**进程默认**——coc7 房间的战斗
因此跑在 dnd5e 的中性二值带上（hard/extreme/critical 全部塌缩成 regular）。
B1 修复方式：调用方显式携带战斗 / 房间规则集 id（``combat["ruleset"]`` →
``settings.ruleset_id``），插件内包装（``combat/rulesets/coc7.py`` 的 CON 检定）
固定声明自己的规则集 id。

本文件用真插件装配 + ``TRPG_RULESET_DEFAULT=dnd5e`` 复现缺陷环境，验收：

1. ``dice._active_roll_policy("coc7")`` 解析到 coc7 插件实例的 roll_policy
   （``is not`` 核心默认策略）；
2. coc7 房间先攻检定 25/60 → ``hard``（initiative=2060；中性带应为 1060）；
3. 反向：dnd5e 房间同一骰值 → ``regular``（中性带，1040/1060 一类）；
4. spy 记录掷骰调用点传出的 ruleset_id——coc7 房间全链路逐点为 ``coc7``，
   进程默认 dnd5e 不再渗入任何调用点；
5. 伤害结算 CON 检定等级带按 coc7 解析（roll 25 / CON 50 → ``hard``；
   修复前在 dnd5e 默认进程里静默塌缩为 ``regular``）；
6. 房规开关按房间规则集分派：coc7 房间开关生效、dnd5e 房间全关并显式拒绝
   （毒剂 / 击晕 / 部位命中的「房规未开启」分支）。

D1（dnd5e 房间 shoot / treat 仍跑 CoC 流程）的行为证据由本文件的
``test_dnd5e_room_shoot_and_treat_probe`` 固化——它是探针不是修复：B1 不动
这两条链路，D1 落地时应把该用例改写为 dnd5e 语义断言。

D3 已兑现该教义（2026-11-20）：探针改写为
``test_dnd5e_room_shoot_and_treat_dispatch``——dnd5e 房间的 shoot / treat
按规则集能力分派后显式拒绝（射击指向 combat.attack 的 d20 分派；急救无
对应能力），不再静默跑 CoC 流程；coc7 房间的 shoot / treat 全流程回归
（掩体 / 闪避 / 弹丸 / 卡壳 / 急救回血）锚定在
``tests/test_ruleset_d3_dispatch.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.tools import combat, dice
from trpg_server.dice_policy import DEFAULT_ROLL_POLICY
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, default_ruleset_id, reset_builtin_registry

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

    ``TRPG_RULESET_DEFAULT`` 是运维临时覆盖（优先级高于配置文件），与
    ``tests/test_ruleset_default_id.py`` 的注入手法同源；重置注册表后下一次
    ``builtin_registry()`` 以 dnd5e 为进程默认重建。
    """
    registry = _load_real_plugins()
    monkeypatch.setenv(HINT_ENV_KEY, "dnd5e")
    reset_builtin_registry()
    assert default_ruleset_id() == "dnd5e"
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class FakeContext:
    """战斗工具的最小房间上下文；``ruleset_id`` 决定房间规则集。"""

    def __init__(self, tmp_path: Path, ruleset_id: str, house_rules: dict | None = None):
        self.room_dir = tmp_path
        self.room_id = f"b1-{ruleset_id}"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._ruleset_id = ruleset_id
        self._house_rules = house_rules or {}

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": self._ruleset_id,
            "house_rules": self._house_rules,
            "members": [],
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


RIFLE = {
    "name": "Rifle",
    "skill": "Firearms",
    "damage": "1D10",
    "range": "100码",
    "attacks": "1",
    "ammo": "5",
}


class SeqRng:
    """按固定序列返回骰值的确定性随机源（超出骰面时夹紧）。"""

    def __init__(self, values: list[int]):
        self.values = list(values)
        self.calls: list[tuple[int, int]] = []

    def __call__(self, sides: int) -> int:
        value = self.values[len(self.calls) % len(self.values)]
        self.calls.append((sides, value))
        return max(1, min(int(sides), int(value)))


# -- 骰子策略按显式规则集 id 解析 ------------------------------------------


def test_active_roll_policy_resolves_by_explicit_ruleset_id(dnd5e_default_process):
    """进程默认 dnd5e 时：coc7 显式解析到插件策略，dnd5e/缺省用核心默认。"""
    coc7_plugin = builtin_registry().get("coc7")
    policy = dice._active_roll_policy("coc7")
    assert policy is coc7_plugin.roll_policy
    assert policy is not DEFAULT_ROLL_POLICY
    # dnd5e 未声明 roll_policy，落回核心默认百分骰；缺省（进程默认）同路。
    assert dice._active_roll_policy("dnd5e") is DEFAULT_ROLL_POLICY
    assert dice._active_roll_policy(None) is DEFAULT_ROLL_POLICY


# -- 先攻检定：等级带按房间规则集 ------------------------------------------


def test_coc7_room_initiative_check_uses_coc7_band(tmp_path, dnd5e_default_process, monkeypatch):
    """coc7 房间先攻检定 25/60 → hard → initiative=2060（中性带会是 1060）。"""
    context = FakeContext(tmp_path, "coc7")
    rng = SeqRng([25, 10])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)], "initiative_check": True},
        context,
    )
    assert "error" not in started
    assert started["ruleset"] == "coc7"
    by_name = {row["name"]: row for row in started["order"]}
    # 25 ≤ 60/2=30 → hard；10 ≤ 40/2=20 → hard：2×1000 + DEX。
    assert by_name["Alpha"]["initiative"] == 2060
    assert by_name["Beta"]["initiative"] == 2040
    state = combat._load_combat(context)
    assert state["ruleset"] == "coc7"
    assert state["combatants"]["Alpha"]["initiative_roll"] == 25
    assert state["combatants"]["Beta"]["initiative_roll"] == 10
    # 骰面序列：两次先攻检定各耗一颗 d100（coc7 策略无修正时单颗百分骰）。
    assert rng.calls == [(100, 25), (100, 10)]


def test_dnd5e_room_initiative_check_falls_back_to_neutral_band(tmp_path, dnd5e_default_process, monkeypatch):
    """反向：dnd5e 房间同一骰值走中性二值带 → regular → 1×1000 + DEX。

    dnd5e 未声明 success_level 能力，中性带只有 regular/failure；先攻掷骰
    本身是 d20（先攻数值与可选检定各走各的骰）。
    """
    context = FakeContext(tmp_path, "dnd5e")
    rng = SeqRng([25, 10])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)], "initiative_check": True},
        context,
    )
    assert "error" not in started
    assert started["ruleset"] == "dnd5e"
    by_name = {row["name"]: row for row in started["order"]}
    # 25 ≤ 60 → regular（中性带）；10 ≤ 40 → regular：1×1000 + DEX。
    assert by_name["Alpha"]["initiative"] == 1060
    assert by_name["Beta"]["initiative"] == 1040
    # 序列：每名战斗者先掷 d20 先攻，再做百分骰先攻检定。
    assert rng.calls == [(20, 25), (20, 10), (100, 25), (100, 10)]


# -- 掷骰调用点逐点携带战斗规则集 id ---------------------------------------


def test_roll_callsites_carry_combat_ruleset_id(tmp_path, dnd5e_default_process, monkeypatch):
    """coc7 房间全链路：每个掷骰调用点传出的 ruleset_id 都是 coc7。

    spy 包在 ``dice._active_roll_policy`` / ``dice._checks_capability`` 上
    （战斗工具经 ``_percentile_roll`` / ``_success_level`` 必经的两个解析
    点）。进程默认是 dnd5e——任何调用点漏传 ruleset_id 都会以 ``None`` /
    ``"dnd5e"`` 出现在记录里，本断言直接红。
    """
    roll_ids: list[str | None] = []
    check_ids: list[str | None] = []
    real_policy = dice._active_roll_policy
    real_checks = dice._checks_capability

    def spy_policy(ruleset_id=None):
        roll_ids.append(ruleset_id)
        return real_policy(ruleset_id)

    def spy_checks(ruleset_id=None):
        check_ids.append(ruleset_id)
        return real_checks(ruleset_id)

    monkeypatch.setattr(dice, "_active_roll_policy", spy_policy)
    monkeypatch.setattr(dice, "_checks_capability", spy_checks)

    # 链路一：开战（先攻检定）→ 近战攻击（对抗 + 伤害 CON 检定）→ 伤害结算。
    room_a = FakeContext(tmp_path / "a", "coc7")
    (tmp_path / "a").mkdir()
    monkeypatch.setattr(combat, "_roller", SeqRng([25, 10, 15, 40, 3, 20, 5, 4]))
    started = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)], "initiative_check": True},
        room_a,
    )
    assert "error" not in started
    attacked = combat.attack_combatant(
        {"attacker": "Alpha", "target": "Beta", "weapon": "Knife", "defense": "dodge"}, room_a
    )
    assert "error" not in attacked
    damaged = combat.damage_combatant({"target": "Beta", "dice": "1D6"}, room_a)
    assert "error" not in damaged

    # 链路二：射击（射击骰）与急救（检定骰）。
    room_b = FakeContext(tmp_path / "b", "coc7")
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(combat, "_roller", SeqRng([30, 6, 20, 30]))
    started_b = combat.start_combat(
        {"participants": [_spec("Shooter", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]), _spec("Prey", dex=40)]},
        room_b,
    )
    assert "error" not in started_b
    shot = combat.shoot_combatant({"attacker": "Shooter", "target": "Prey", "weapon": "Rifle"}, room_b)
    assert "error" not in shot
    treated = combat.treat_combatant({"target": "Prey", "method": "first_aid"}, room_b)
    assert "error" not in treated

    # 逐点断言：先攻检定 ×2、攻击 / 防御 / 重伤 CON ×3、伤害 CON ×1、
    # 射击 ×1、急救 ×1 —— 全部显式 coc7，无一处回落进程默认 dnd5e。
    assert roll_ids and set(roll_ids) == {"coc7"}, roll_ids
    assert check_ids and set(check_ids) == {"coc7"}, check_ids
    assert len(roll_ids) >= 8
    assert len(check_ids) >= 8


# -- CON 检定等级带（插件内包装显式声明 coc7） -----------------------------


def test_con_check_band_resolves_as_coc7_in_dnd5e_default_process(tmp_path, dnd5e_default_process, monkeypatch):
    """dnd5e 默认进程里，coc7 房间重伤 CON 检定按 coc7 等级带判定。

    修复前：``combat/rulesets/coc7.py`` 的 CON 检定包装不声明规则集，
    ``dice._success_level`` 按进程默认（dnd5e）解析 → Dnd5eChecks 没有
    success_level → 中性二值带：25/50 只能得出 regular。修复后 25 ≤ 25
    （hard 阈值 = 50/2）→ ``hard``。
    """
    from trpg_server.combat.rulesets import coc7 as coc7_combat_impl

    assert coc7_combat_impl._success_level(25, 50, 1, 96) == "hard"
    assert coc7_combat_impl._success_level(4, 50, 1, 96) == "extreme"

    context = FakeContext(tmp_path, "coc7")
    monkeypatch.setattr(combat, "_roller", SeqRng([15, 40, 3, 25]))
    started = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)]}, context
    )
    assert "error" not in started
    attacked = combat.attack_combatant(
        {"attacker": "Alpha", "target": "Beta", "weapon": "Knife", "defense": "dodge"}, context
    )
    assert "error" not in attacked
    con_check = attacked["damage"]["con_check"]
    assert con_check == {"roll": 25, "value": 50, "level": "hard"}


# -- 房规开关按房间规则集分派 ----------------------------------------------


def test_room_rule_options_dispatch_by_room_ruleset(tmp_path, dnd5e_default_process):
    """coc7 房间开关生效；dnd5e 房间（未声明 options 能力）全关。"""
    rules_on = {
        "coc7_combat_poison": True,
        "coc7_combat_knockout": True,
        "coc7_combat_hit_locations": True,
    }
    coc7_options = combat._room_rule_options(FakeContext(tmp_path, "coc7", rules_on))
    dnd5e_options = combat._room_rule_options(FakeContext(tmp_path, "dnd5e", rules_on))
    assert coc7_options.combat_poison is True
    assert coc7_options.combat_knockout is True
    assert coc7_options.combat_hit_locations is True
    assert dnd5e_options.combat_poison is False
    assert dnd5e_options.combat_knockout is False
    assert dnd5e_options.combat_hit_locations is False


def test_dnd5e_room_rejects_coc7_room_rule_tools(tmp_path, dnd5e_default_process, monkeypatch):
    """dnd5e 房间毒剂 / 击晕 / 部位命中：按房规解析后显式拒绝，不静默跑 CoC。"""
    context = FakeContext(tmp_path, "dnd5e")
    monkeypatch.setattr(combat, "_roller", SeqRng([30]))
    started = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)]}, context
    )
    assert "error" not in started

    poison = combat.poison_combatant(
        {"target": "Beta", "doses": 1, "potency": 1, "damage": "1D6"}, context
    )
    assert poison == {"error": "毒剂房规未开启，请先在房规中开启「毒剂」"}
    knockout = combat.knockout_combatant({"actor": "Alpha", "target": "Beta"}, context)
    assert knockout == {"error": "击晕房规未开启，请先在房规中开启「击晕」"}
    hit_location = combat.hit_location_check({"target": "Beta"}, context)
    assert hit_location == {"error": "部位命中房规未开启，请先在房规中开启「部位命中」"}


# -- R2 兑现：shoot / treat 按规则集分派（D3 落地，探针改写为语义断言） ----


def test_dnd5e_room_shoot_and_treat_dispatch(tmp_path, dnd5e_default_process, monkeypatch):
    """D3 兑现：dnd5e 房间的 shoot / treat 显式拒绝，不再静默跑 CoC 流程。

    本用例前身 ``test_dnd5e_room_shoot_and_treat_probe`` 的教义写明「分派
    落地时改写为语义断言」——D3 按普查事实落地后兑现：

    - dnd5e 的 ``resolve_attack_action`` 天然覆盖远程攻击（武器行按
      ammunition 属性推导 ranged、掩体 AC +2/+5、近身 5 射尺劣势），
      射击的正确入口就是 ``combat.attack``（D1 的 ``attack_mode="d20"``
      分派）；把 ``combat.shoot`` 转发给它需要发明百分骰→d20、射程难度
      带→无、burst / 弹药 / 卡壳→无的参数翻译，属「发明新 dnd5e 语义」，
      不做——dnd5e KP 工具清单也早已 deny ``combat.shoot`` / ``combat.treat``。
    - dnd5e 没有战斗内急救 / 医学能力（死亡豁免在 ``start_turn`` 自动
      结算、休息恢复走 rest 工具面、Medicine 只是通用 d20 技能检定）。
    - 因此两者显式拒绝（对齐 D2 毒剂 / 击晕的拒绝先例）：shoot →
      「当前规则集不支持射击检定，请使用 combat.attack 处理远程攻击」；
      treat → 「当前规则集不支持急救或医学治疗」。
    - 闸门先于武器与战斗者解析：角色卡带着 Rifle（远程武器在场）也拒绝，
      不再落入「Knife 不是远程武器」一类 CoC 流程内部报错（该报错仍是
      coc7 房间的行为锚，回归锚定在 ``tests/test_ruleset_d3_dispatch.py``）；
      拒绝路径零骰子消耗（全程只有开战的两颗 d20 先攻）。
    """
    context = FakeContext(tmp_path, "dnd5e")
    rng = SeqRng([30, 6])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {
            "participants": [
                _spec("Shooter", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]),
                _spec("Target", dex=40),
            ]
        },
        context,
    )
    assert "error" not in started

    # 射击：规则集拒绝先于武器解析——Rifle 在卡上也不进 CoC 射击流程。
    shot = combat.shoot_combatant(
        {"attacker": "Shooter", "target": "Target", "weapon": "Rifle"}, context
    )
    assert shot == {"error": "当前规则集不支持射击检定，请使用 combat.attack 处理远程攻击"}
    # 闸门也先于战斗者解析：未入场的 attacker 同样拿到规则集拒绝，而不是
    # 「不在当前战斗中」——工具对本规则集整体不可用，换参数重试无意义。
    unknown = combat.shoot_combatant(
        {"attacker": "Ghost", "target": "Target", "weapon": "Rifle"}, context
    )
    assert unknown == {"error": "当前规则集不支持射击检定，请使用 combat.attack 处理远程攻击"}

    # 急救：同样显式拒绝（dnd5e 无战斗内急救 / 医学能力）。
    hurt = FakeContext(tmp_path / "hurt", "dnd5e")
    (tmp_path / "hurt").mkdir()
    monkeypatch.setattr(combat, "_roller", SeqRng([30]))
    started_hurt = combat.start_combat(
        {"participants": [_spec("Healer", dex=60), _spec("Hurt", dex=40, hp=3)]}, hurt
    )
    assert "error" not in started_hurt
    treated = combat.treat_combatant({"target": "Hurt", "method": "first_aid"}, hurt)
    assert treated == {"error": "当前规则集不支持急救或医学治疗"}

    # 全程只掷了开战的 d20 先攻：拒绝路径零骰子消耗，CoC 流程一步未跑。
    assert rng.calls == [(20, 30), (20, 6)]


# -- NPC 体格 / 伤害加值按战斗规则集解析（B1 收尾补充） ----------------------


def test_coc7_room_npc_build_and_damage_bonus_follow_room_ruleset(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """R1 尾洞：NPC 体格 / 伤害加值不再跟随进程默认。

    修复前 ``_combatant_from_spec`` 不带规则集 id，dnd5e 默认进程里 coc7 房间
    NPC 的 build/damage_bonus 退化为中性值（金样 Hunter.damage_bonus 出现
    '' != '0'）；修复后按房间规则集解析：STR80/SIZ80 → build 1、+1D4、
    db_terms [(1,4)]（中性值是 build 0、''、[]）。
    """
    context = FakeContext(tmp_path, "coc7")
    rng = SeqRng([50])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {"participants": [_spec("Brute", str=80, siz=80)]},
        context,
    )
    assert "error" not in started
    state = combat._load_combat(context)
    brute = state["combatants"]["Brute"]
    assert brute["build"] == 1
    assert brute["damage_bonus"] == "+1D4"
    assert [tuple(term) for term in brute["db_terms"]] == [(1, 4)]


def test_dnd5e_room_npc_build_and_damage_bonus_stay_neutral(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """反向：dnd5e 房间（进程默认同向）NPC 体格走中性值，语义一致。"""
    context = FakeContext(tmp_path, "dnd5e")
    rng = SeqRng([50])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {"participants": [_spec("Grunt", str=80, siz=80)]},
        context,
    )
    assert "error" not in started
    state = combat._load_combat(context)
    grunt = state["combatants"]["Grunt"]
    assert grunt["build"] == 0
    assert grunt["damage_bonus"] == ""
    assert grunt["db_terms"] == []
