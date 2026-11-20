"""边界收敛 D3：combat.shoot / combat.treat 按规则集分派。

D3 收口内容：核心的射击流程（百分骰 vs 阈值、射程难度带、掩体闪避、弹药、
卡壳）与急救 / 医学流程（1 小时窗口、回 1 HP / 1D3、濒死稳定）是 CoC7 的
战斗约定，不是可中性降级的通用流程。两条工具入口按
``combat["ruleset"] → settings.ruleset_id`` 解析本场战斗规则集，经
``combat/registry.flow_mode`` 探测 ``<flow>_mode`` 声明（与 D1
``attack_mode="d20"`` 同款属性声明惯例）：coc7 声明
``shoot_mode="percentile"`` / ``treat_mode="first_aid"`` 全流程放行；未声明
规则集显式拒绝（对齐 D2 毒剂 / 击晕的拒绝先例），不再静默套用 CoC 流程 +
中性二值带（B1 探针固化的 R2 事实就此收口）。

分派形态判定（普查事实，先结论后动刀）：

- dnd5e 的 ``resolve_attack_action`` 天然覆盖远程攻击（武器行按 ammunition
  属性 / 怪物动作推导 ranged、``attack_modifiers(ranged=True)`` 施加近身
  5 射尺劣势、cover 半掩体 AC +2 / 四分之三 +5 / 全掩体禁攻）——dnd5e
  房间射击的正确入口就是 ``combat.attack``（D1 的 d20 分派已就位），且
  dnd5e KP 工具清单早已 deny ``combat.shoot`` / ``combat.treat``
  （``DND5E_KP_TOOL_DENY``）。把 ``combat.shoot`` 转发给它需要发明
  百分骰→d20、射程难度带→无、burst / 弹药 / 卡壳→无的参数翻译，属
  「发明新 dnd5e 语义」，不做 → **显式拒绝**（错误指向 combat.attack）。
- dnd5e 没有战斗内急救 / 医学能力（死亡豁免在 ``start_turn`` 自动结算、
  休息 / 生命骰恢复走 rest 工具面、SRD Medicine 只是通用 d20 技能检定，
  无「回 1 HP / first_aid_used / 濒死稳定」语义）→ **显式拒绝**。

本文件用真插件装配 + ``TRPG_RULESET_DEFAULT=dnd5e`` 复现最严苛环境（与
B1 / D1 / D2 同源手法），验收：

1. registry 取用点：coc7 显式解析 ``percentile`` / ``first_aid``（进程默认
   dnd5e 不影响显式 id）；dnd5e 真插件与空注册表两种缺席形态退空串。
2. coc7 房间全流程回归（金样三条链路不含 shoot / treat，本文件补位）：
   射击命中（技能 50 / roll 10 → regular、1D10 伤害、弹药 5→4）与
   「骰值 ≥ 故障值即卡」的卡壳路径；急救（roll 30/30 → regular 回 1 HP、
   ``first_aid_used`` 置位、二次急救仍被 CoC 规则拒绝）；「Knife 不是
   远程武器」报错路径仍可达（原 B1 探针 tripwire 段的 coc7 归位）。
3. dnd5e 房间：shoot / treat 显式拒绝、拒绝先于武器与战斗者解析、零骰子
   消耗（CoC 流程一步未跑）。
4. 空注册表（会话中途规则集被摘除的最彻底缺席形态）：同样显式拒绝，
   不抛 ``CombatRulesetUnavailable``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.tools import combat
from trpg_server.combat.registry import CombatRulesetUnavailable, flow_mode, get_ruleset
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

RIFLE = {
    "name": "Rifle",
    "skill": "Firearms",
    "damage": "1D10",
    "range": "100码",
    "attacks": "1",
    "ammo": "5",
    "malfunction": "98",
}

SHOOT_REJECT = {"error": "当前规则集不支持射击检定，请使用 combat.attack 处理远程攻击"}
TREAT_REJECT = {"error": "当前规则集不支持急救或医学治疗"}


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def dnd5e_default_process(monkeypatch):
    """真插件装配 + 进程默认规则集注入 dnd5e（复现 R1 缺陷环境，B1/D1/D2 同源）。"""
    registry = _load_real_plugins()
    monkeypatch.setenv(HINT_ENV_KEY, "dnd5e")
    reset_builtin_registry()
    assert default_ruleset_id() == "dnd5e"
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture
def empty_ruleset_registry(monkeypatch):
    """空插件注册表：任何规则集缺席形态下取用函数必须退中性而不是抛错。"""
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    yield builtin_registry()
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class FakeContext:
    """战斗工具的最小房间上下文；``ruleset_id`` 决定房间规则集。"""

    def __init__(self, tmp_path: Path, ruleset_id: str | None):
        self.room_dir = tmp_path
        self.room_id = f"d3-{ruleset_id or 'legacy'}"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._ruleset_id = ruleset_id

    def room_info(self) -> dict:
        info = {"id": self.room_id, "house_rules": {}, "members": []}
        if self._ruleset_id is not None:
            info["ruleset_id"] = self._ruleset_id
        return info


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


# -- 1. registry 取用点：流程模式声明与缺席形态 ------------------------------


def test_flow_mode_resolves_coc7_declarations_by_explicit_id(dnd5e_default_process):
    """coc7 显式 id 解析两条流程模式；进程默认 dnd5e 不影响显式解析。"""
    assert flow_mode("shoot", "coc7") == "percentile"
    assert flow_mode("treat", "coc7") == "first_aid"
    # 流程名归一（大小写 / 空白不敏感，与 skill_names 的 kind 口径一致）。
    assert flow_mode("  Shoot ", "coc7") == "percentile"
    # dnd5e 真插件未声明 → 空串（显式拒绝的判据）。
    assert flow_mode("shoot", "dnd5e") == ""
    assert flow_mode("treat", "dnd5e") == ""
    # 进程默认（dnd5e）同样没有声明：未指定规则集不得静默落到 CoC 流程。
    assert flow_mode("shoot", None) == ""
    assert flow_mode("treat", None) == ""
    # 未声明模式的流程名退空串（未来新增流程按同一惯例声明）。
    assert flow_mode("grapple", "coc7") == ""


def test_flow_mode_empty_registry_returns_blank(empty_ruleset_registry):
    """空注册表（最彻底缺席）：任何流程 / 任何 id 退空串，不抛错。

    战斗实现整体不可用（``CombatRulesetUnavailable``）与未声明模式同一判据：
    闸门拿空串 → 显式拒绝，不击穿调用方。
    """
    with pytest.raises(CombatRulesetUnavailable):
        get_ruleset("coc7")
    assert flow_mode("shoot", "coc7") == ""
    assert flow_mode("treat", "coc7") == ""
    assert flow_mode("shoot", None) == ""
    assert flow_mode("treat", None) == ""


# -- 2. coc7 房间全流程回归（金样未覆盖 shoot / treat，本节补位） -------------


def test_coc7_room_shoot_hits_with_ammo_and_damage(tmp_path, dnd5e_default_process, monkeypatch):
    """coc7 房间射击全流程：命中 / 1D10 伤害 / 弹药递减，逐位保留。

    进程默认是 dnd5e——本用例证明闸门按**战斗会话规则集**放行 coc7 房间，
    完整 CoC 流程（技能名表命中 firearms → 50、普通难度阈值即技能值、
    弹药 5→4）不受进程默认渗入。
    """
    context = FakeContext(tmp_path, "coc7")
    rng = SeqRng([30, 4])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {
            "participants": [
                _spec("Shooter", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]),
                _spec("Prey", dex=40),
            ]
        },
        context,
    )
    assert "error" not in started
    shot = combat.shoot_combatant(
        {"attacker": "Shooter", "target": "Prey", "weapon": "Rifle"}, context
    )
    assert "error" not in shot, shot
    assert shot["roll"] == 30
    assert shot["success"] is True
    assert shot["success_level"] == "regular"
    assert shot["difficulty"] == "regular"
    assert shot["threshold"] == 50
    assert shot["total_damage"] == 4
    assert shot["malfunction"] is False
    first = shot["shots"][0]
    assert first["hit"] is True
    assert first["hp_before"] == 10
    assert first["hp_after"] == 6
    assert "d100=30" in shot["summary"]
    assert "伤害 4（1D10=[4]），HP 10→6" in shot["summary"]
    # 弹药跟踪：装弹量 5 → 一发后 4。
    state = combat._load_combat(context)
    assert state["combatants"]["Shooter"]["ammo"]["Rifle"] == 4
    assert state["combatants"]["Prey"]["hp"] == 6
    # 序列：coc7 先攻免检（DEX 直接定序），射击骰 + 1D10 伤害骰各一颗。
    assert rng.calls == [(100, 30), (10, 4)]


def test_coc7_room_shoot_malfunction_follows_coc7_threshold(tmp_path, dnd5e_default_process, monkeypatch):
    """coc7 房间卡壳路径：骰值 98 ≥ 故障值 98 即卡（D2 武器表语义原样接线）。"""
    context = FakeContext(tmp_path, "coc7")
    rng = SeqRng([98])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {
            "participants": [
                _spec("Gunner", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]),
                _spec("Prey", dex=40),
            ]
        },
        context,
    )
    assert "error" not in started
    shot = combat.shoot_combatant(
        {"attacker": "Gunner", "target": "Prey", "weapon": "Rifle"}, context
    )
    assert "error" not in shot, shot
    assert shot["roll"] == 98
    # CoC7 派生规则：所需值 ≥50 时只有 100 算大失败，98 是普通失败；
    # 卡壳判定独立于等级带，照常触发（98 ≥ 故障值 98）。
    assert shot["success_level"] == "failure"
    assert shot["success"] is False
    assert shot["malfunction"] is True
    assert shot["total_damage"] == 0
    assert "d100=98 ≥ 故障值 98，武器故障" in shot["summary"]
    assert "武器已故障，需要维修或更换" in shot["summary"]
    # 卡壳同样消耗一发弹药。
    state = combat._load_combat(context)
    assert state["combatants"]["Gunner"]["ammo"]["Rifle"] == 4
    # 序列：只有射击骰；卡壳短路，不掷伤害骰。
    assert rng.calls == [(100, 98)]


def test_coc7_room_treat_heals_and_rejects_repeat(tmp_path, dnd5e_default_process, monkeypatch):
    """coc7 房间急救全流程：roll 30/30 → regular 回 1 HP；二次急救仍被拒。"""
    context = FakeContext(tmp_path, "coc7")
    rng = SeqRng([30])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {"participants": [_spec("Healer", dex=60), _spec("Hurt", dex=40, hp=3)]},
        context,
    )
    assert "error" not in started
    treated = combat.treat_combatant({"target": "Hurt", "method": "first_aid"}, context)
    assert "error" not in treated, treated
    assert treated["roll"] == 30
    assert treated["success"] is True
    assert treated["success_level"] == "regular"
    assert treated["healed"] == 1
    assert treated["hp"] == 4
    assert "d100=30 / 30" in treated["summary"]
    state = combat._load_combat(context)
    assert state["combatants"]["Hurt"]["first_aid_used"] is True
    # CoC 规则分支原样：每次伤势只能急救一次。
    repeat = combat.treat_combatant({"target": "Hurt", "method": "first_aid"}, context)
    assert repeat == {"error": "该目标已经接受过急救，本次伤势不能再次急救"}
    assert rng.calls == [(100, 30)]


def test_coc7_room_shoot_without_ranged_weapon_error_still_reachable(
    tmp_path, dnd5e_default_process, monkeypatch
):
    """「Knife 不是远程武器」报错路径在 coc7 房间仍可达（原 B1 探针 tripwire 归位）。

    B1 探针曾在 dnd5e 房间固化这条报错；D3 后 dnd5e 房间在闸门处拒绝，
    该报错只属于 coc7 房间的 CoC 流程内部，语义锚随迁。
    """
    context = FakeContext(tmp_path, "coc7")
    monkeypatch.setattr(combat, "_roller", SeqRng([30]))
    started = combat.start_combat(
        {"participants": [_spec("Punchy", dex=60), _spec("Prey", dex=40)]},
        context,
    )
    assert "error" not in started
    shot = combat.shoot_combatant({"attacker": "Punchy", "target": "Prey", "weapon": "Knife"}, context)
    assert shot == {"error": "Knife 不是远程武器"}


# -- 3. dnd5e 房间：显式拒绝（分派形态判定的落地语义） ------------------------


def test_dnd5e_room_rejects_shoot_and_treat(tmp_path, dnd5e_default_process, monkeypatch):
    """dnd5e 房间：shoot / treat 显式拒绝，CoC 流程一步未跑。

    射击指向 ``combat.attack``（D1 的 d20 分派已覆盖远程攻击）；急救无
    对应能力（死亡豁免走 ``start_turn``、恢复走 rest 工具面）。
    """
    context = FakeContext(tmp_path, "dnd5e")
    rng = SeqRng([30, 6])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {
            "participants": [
                _spec("Shooter", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]),
                _spec("Target", dex=40, hp=3),
            ]
        },
        context,
    )
    assert "error" not in started

    shot = combat.shoot_combatant(
        {"attacker": "Shooter", "target": "Target", "weapon": "Rifle"}, context
    )
    assert shot == SHOOT_REJECT
    treated = combat.treat_combatant({"target": "Target", "method": "first_aid"}, context)
    assert treated == TREAT_REJECT

    # 拒绝先于战斗者解析：未入场的名字同样拿到规则集拒绝，而不是
    # 「不在当前战斗中」——工具对本规则集整体不可用，换参数重试无意义。
    unknown = combat.shoot_combatant(
        {"attacker": "Ghost", "target": "Target", "weapon": "Rifle"}, context
    )
    assert unknown == SHOOT_REJECT

    # 战斗状态未被触碰：HP 原样、弹药跟踪器开战时通用初始化为空且未被
    # 拒绝的工具触碰，全程只有开战的两颗 d20 先攻。
    state = combat._load_combat(context)
    assert state["combatants"]["Target"]["hp"] == 3
    assert state["combatants"]["Shooter"]["ammo"] == {}
    assert rng.calls == [(20, 30), (20, 6)]


# -- 4. 空注册表：会话中途摘除规则集的拒绝路径 -------------------------------


def test_mid_session_registry_wipe_rejects_shoot_and_treat(tmp_path, monkeypatch):
    """空注册表（会话中途规则集被摘除）：同样显式拒绝，不抛错。

    旧房间形态（room_info 无 ruleset_id）先以真插件开一场 coc7 战斗，
    再摘除全部规则集——``flow_mode`` 经既有 ``_capability`` 探测把
    ``CombatRulesetUnavailable`` 降为空串，闸门照常拒绝。
    """
    _load_real_plugins()
    legacy = FakeContext(tmp_path, None)
    rng = SeqRng([10, 4])
    monkeypatch.setattr(combat, "_roller", rng)
    started = combat.start_combat(
        {
            "participants": [
                _spec("Shooter", dex=60, skills={"Firearms": 50}, weapons=[RIFLE]),
                _spec("Prey", dex=40),
            ]
        },
        legacy,
    )
    assert "error" not in started
    assert started["ruleset"] == "coc7"

    # 中途摘除全部规则集（运维停用 / 装配出错的最彻底缺席形态）。
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    try:
        shot = combat.shoot_combatant(
            {"attacker": "Shooter", "target": "Prey", "weapon": "Rifle"}, legacy
        )
        assert shot == SHOOT_REJECT
        treated = combat.treat_combatant({"target": "Prey", "method": "first_aid"}, legacy)
        assert treated == TREAT_REJECT
        # 拒绝路径零骰子消耗（coc7 开战 DEX 直接定序，全程不掷任何骰）。
        assert rng.calls == []
    finally:
        runtime.clear_plugin_runtime()
        reset_builtin_registry()
