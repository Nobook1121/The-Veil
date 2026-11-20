"""CoC7 规则书插件。

只把 CoC7 的“判定/伤害/濒死/行动经济”语义收在这里；武器表、法术表、怪物表
仍然由内容层提供，插件不硬编码具体条目。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..contracts import DamageOutcome, RoundEndOutcome

LEVEL_RANK = {"fumble": -1, "failure": 0, "regular": 1, "hard": 2, "extreme": 3, "critical": 4}
SKIPPED_STATUS = {"unconscious", "dying", "dead"}
# 徒手武器目录条目（格斗（斗殴）专精、1D3、无贯穿/卡壳）。
# 核心攻击/反击链路只向 ``unarmed_weapon()`` 查询该条目，不再自持 CoC 武器表。
UNARMED_WEAPON = {
    "name": "徒手",
    "skill": "格斗（斗殴）",
    "skillKey": "fighting",
    "specialtyKey": "brawl",
    "damage": "1D3",
    "range": "接触",
    "impale": False,
    "attacks": "1",
    "ammo": "N/A",
    "malfunction": "N/A",
}
# 战斗技能名回退表：角色卡技能行缺失或未声明专精键时，按这些 CoC 技能名兜底
# 取值（D2：原硬编码在 ``agents/tools/combat.py`` 的三张表，逐字迁移）。
# 核心链路只经 ``combat/registry.skill_names`` 按本场战斗规则集查询，缺席退空表。
MELEE_SKILL_NAMES = ("格斗（斗殴）", "格斗（斗殴技能）", "斗殴", "格斗", "fencing", "brawl", "fighting")
RANGED_SKILL_NAMES = ("射击（手枪）", "射击（步枪）", "射击（霰弹枪）", "射击", "枪械", "firearms")
DODGE_SKILL_NAMES = ("闪避", "dodge")


def _as_text(value: Any, fallback: str = "") -> str:
    """与核心武器行层同语义的文本清洗（插件自持，不 import 核心私有助手）。"""
    if value is None:
        return fallback
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def parse_malfunction(value: Any) -> int | None:
    """把 CoC 武器表的故障值解析成整数（``N/A`` 返回 ``None``，p97-98）。

    故障值是 CoC 规则书武器表（表 XVII）的专有列；其他规则集的武器行没有
    「骰值达到阈值即卡壳」的数据语义，解析归本插件所有。
    """
    text = _as_text(value).upper()
    if not text or text in {"N/A", "NA", "-", "—", "NONE"}:
        return None
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _max_roll(terms) -> int:
    return sum(max(0, int(count)) * max(1, int(sides)) for count, sides in terms)


def _success_level(roll: int, target: int, critical: int = 1, fumble: int = 96) -> str:
    """CoC7 成功等级：统一复用骰子模块的判定数学，避免出现第三份阈值口径。

    所需值 ≥50 且未显式收紧阈值（默认 96）时，只有 100 算大失败。
    本实现就是 coc7 的战斗数学，显式声明自己的规则集 id（B1）：进程默认规则集
    被配置成其他规则集时，本规则集内部的等级带仍按 coc7 解析，不跟随进程默认。
    """
    from trpg_server.agents.tools import dice

    return dice._success_level(
        int(roll), int(target), int(critical), int(fumble), ruleset_id="coc7"
    )


def build_and_damage_bonus(str_value: int, siz_value: int) -> tuple[int, str, list[tuple[int, int]]]:
    """按 COC7 体格表由 STR+SIZ 推导（体格, 伤害加值文本, DB 骰点）。"""

    total = max(0, str_value) + max(0, siz_value)
    if total <= 64:
        return -2, "-2", []
    if total <= 84:
        return -1, "-1", []
    if total <= 124:
        return 0, "0", []
    if total <= 164:
        return 1, "+1D4", [(1, 4)]
    if total <= 204:
        return 2, "+1D6", [(1, 6)]
    extra = max(0, (total - 205) // 80)
    return 3 + extra, f"+{2 + extra}D6", [(2 + extra, 6)]


class Coc7Ruleset:
    ruleset_id = "coc7"
    display_name = "克苏鲁的呼唤 7e"
    # 核心战斗流程的归属声明（D3，与 dnd5e 的 ``attack_mode="d20"`` 同款惯例）：
    # 核心的射击流程（百分骰 vs 阈值、射程难度带、掩体闪避、弹药、卡壳）与
    # 急救 / 医学流程（1 小时窗口、回 1 HP / 1D3、濒死稳定）是 CoC7 的战斗
    # 约定；声明了模式才对核心实现开放，未声明规则集由入口显式拒绝。
    shoot_mode = "percentile"
    treat_mode = "first_aid"

    # -- 核心能力映射（供与规则无关的战斗工具调用）------------------------
    def level_rank(self) -> dict[str, int]:
        """成功等级排序：核心只做大小比较，不关心等级叫什么。"""
        return LEVEL_RANK

    def skipped_statuses(self) -> frozenset[str]:
        """失去行动能力的状态集合。"""
        return SKIPPED_STATUS

    def build_and_damage_bonus(self, str_value: Any, siz_value: Any):
        """伤害加值/体格：这是本规则集的伤害模型，由插件提供。"""
        return build_and_damage_bonus(str_value, siz_value)

    def unarmed_weapon(self) -> dict[str, Any]:
        """徒手攻击/反击的武器目录条目（格斗（斗殴）、1D3）。"""
        return dict(UNARMED_WEAPON)

    def extreme_damage(
        self,
        terms,
        fixed: int,
        db,
        *,
        raw: str = "",
        impale: bool = False,
        allow_impale: bool = True,
        roll_damage=None,
    ) -> tuple[int, str]:
        """极限成功的伤害合成：满骰面；贯穿武器追加一次武器伤害骰（p97）。"""
        db_terms = [tuple(term) for term in db or []]
        if not (impale and allow_impale):
            total = _max_roll(terms) + int(fixed) + _max_roll(db_terms)
            return max(0, total), f"极限成功·满伤害（{raw}{' +DB' if db_terms else ''}）"
        extra, _ = roll_damage(terms)
        total = _max_roll(terms) + int(fixed) + extra + _max_roll(db_terms)
        return max(0, total), f"极限成功·贯穿满伤害并追加一次武器伤害骰 {terms}"

    def weapon_malfunction(self, roll: Any, threshold: Any) -> bool:
        """射击卡壳判定：骰值不低于武器故障值即故障（p97-98）。

        故障值本身是武器表数据（由核心转交），「达到阈值即卡壳」是本规则集
        的判定；``threshold`` 缺省（N/A 解析为 ``None``）恒不卡壳。
        """
        try:
            value = int(threshold)
        except (TypeError, ValueError):
            return False
        return bool(value) and int(roll) >= value

    def weapon_field_rules(self, weapon: Any) -> dict[str, Any]:
        """CoC 武器行的规则集数据语义：故障值整数化、贯穿标记取行内 impale。

        「故障值 95 表示骰到 95+ 卡壳」「impale=True 的武器极限成功追加一次
        伤害骰」都是 CoC7 的表数据语义；其他规则集的武器行没有这两列的含义，
        由本钩子集中解读，核心只转交结果，缺席退中性（不卡壳、不贯穿）。
        """
        row = weapon if isinstance(weapon, dict) else {}
        return {
            "malfunction": parse_malfunction(row.get("malfunction")),
            "impale": _as_bool(row.get("impale")),
        }

    def melee_skill_names(self) -> tuple[str, ...]:
        """近战技能名回退表（格斗系）；核心链路经 ``skill_names`` 取用。"""
        return MELEE_SKILL_NAMES

    def ranged_skill_names(self) -> tuple[str, ...]:
        """射击技能名回退表；核心链路经 ``skill_names`` 取用。"""
        return RANGED_SKILL_NAMES

    def dodge_skill_names(self) -> tuple[str, ...]:
        """闪避技能名回退表；核心链路经 ``skill_names`` 取用。"""
        return DODGE_SKILL_NAMES

    def auto_fire_composition(
        self, bullets: Any, extreme: Any, difficulty_rank: Any
    ) -> tuple[int, int]:
        """全自动配比：常规成功一半命中；极难全中且前半贯穿（p97-98）。"""
        usable = max(0, int(bullets))
        hits = usable if extreme else max(1, usable // 2)
        impale_count = usable // 2 if extreme and int(difficulty_rank) < 2 else 0
        return hits, impale_count

    def initiative_score(self, level: str, dex: Any) -> int:
        """可选先攻检定的排序键：成功等级 ×1000 + DEX（p105）。"""
        return LEVEL_RANK.get(level, 0) * 1000 + int(dex or 0)

    def initial_sanity(self, power: Any) -> int:
        """无卡参与者未显式给出理智时的起始值：SAN 起始 = POW。"""
        return max(0, int(power or 0))

    # -- 先攻与行动经济 ---------------------------------------------------
    def roll_initiative(
        self, combatant: Mapping[str, Any], rng: Any, settings: Any
    ) -> int:
        dex = max(0, int(combatant.get("dex") or 0))
        # 已准备武器：DEX+50 决定射击时机（p96）。这是状态，不是额外行动。
        return dex + 50 if combatant.get("ready_weapon") else dex

    def initiative_tiebreak(self, combatant: Mapping[str, Any]) -> int:
        # DEX 同值时战斗技能高者先（p86）。
        return max(0, int(combatant.get("fighting") or combatant.get("melee_skill") or 0))

    def actions_per_round(self, combatant: Mapping[str, Any], settings: Any) -> int:
        base = max(1, int(combatant.get("actions_base") or 1))
        # 房规开关，默认关闭；核心规则没有 DEX 阈值额外行动（p86）。
        threshold = getattr(settings, "dex_extra_action_threshold", None)
        if threshold is not None and int(combatant.get("dex") or 0) >= int(threshold):
            base += 1
        return base

    def reaction_limit(self, combatant: Mapping[str, Any], settings: Any) -> int:
        # 多攻击防御者需要先耗尽等量反应，才吃寡不敌众奖励骰（p91）。
        return max(1, self.actions_per_round(combatant, settings))

    def action_budget(self, combatant: Mapping[str, Any], settings: Any) -> dict[str, int]:
        return {"action": self.actions_per_round(combatant, settings)}

    def is_incapacitated(self, combatant: Mapping[str, Any]) -> bool:
        return str(combatant.get("status") or "active") in SKIPPED_STATUS

    # -- 对抗判定 ---------------------------------------------------------
    def resolve_melee(
        self, attack_level: str, defense_level: str, defense_kind: str
    ) -> str:
        attack_rank = LEVEL_RANK.get(attack_level, 0)
        defense_rank = LEVEL_RANK.get(defense_level, 0)
        if defense_kind == "none":
            return "hit" if attack_rank > 0 else "miss"
        if attack_rank > defense_rank:
            return "hit"
        if attack_rank < defense_rank:
            # 反击成功才反伤；闪避成功只是免伤（p86-87）。
            return "counter_hit" if defense_kind == "counter" and defense_rank > 0 else "miss"
        # 平手：闪避胜；反击时攻击者胜（p86-87）。
        return "hit" if defense_kind == "counter" else "miss"

    # -- 伤害与死亡 -------------------------------------------------------
    def resolve_damage(
        self,
        combatant: dict[str, Any],
        amount: int,
        *,
        armor: int,
        ignore_armor: bool,
        roll_d100,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> DamageOutcome:
        damage = max(0, int(amount))
        armor_value = max(0, int(armor))
        effective = damage if ignore_armor else max(0, damage - armor_value)
        before = int(combatant.get("hp") or 0)
        max_hp = max(1, int(combatant.get("max_hp") or 1))
        outcome = DamageOutcome(
            damage=damage,
            armor=0 if ignore_armor else armor_value,
            effective=effective,
            hp_before=before,
            hp_after=before,
            major_wound=bool(combatant.get("major_wound")),
        )
        if combatant.get("dead"):
            combatant["hp"] = 0
            outcome.hp_after = 0
            outcome.dead = True
            outcome.dying = bool(combatant.get("dying"))
            outcome.unconscious = True
            outcome.note = "目标已经死亡，伤害不再改变状态"
            return outcome

        # 单次伤害严格大于最大耐久才即死；等于最大耐久是重伤 + 濒死（p100）。
        if effective > max_hp:
            combatant.update({"hp": 0, "dead": True, "dying": False, "unconscious": True})
            outcome.hp_after = 0
            outcome.dead = True
            outcome.dying = False
            outcome.unconscious = True
            outcome.major_wound = True
            outcome.note = "单次伤害超过最大生命值，目标死亡"
            return outcome

        after = max(0, before - effective)
        combatant["hp"] = after
        combatant["damage_taken_this_round"] = int(
            combatant.get("damage_taken_this_round") or 0
        ) + effective
        major = effective >= max_hp / 2 and effective > 0
        if major:
            combatant["major_wound"] = True
        dying = unconscious = False
        con_roll = con_level = None
        if after <= 0:
            # 濒死必须同时满足：本次/已有重伤 + 耐久归零（p100）。
            if major or combatant.get("major_wound"):
                dying = True
            else:
                unconscious = True
        elif major:
            con_value = max(1, min(99, int(combatant.get("con") or 50)))
            if callable(roll_d100):
                con_roll = int(roll_d100())
                con_level = _success_level(
                    con_roll, con_value, thresholds[0], thresholds[1]
                )
                if LEVEL_RANK.get(con_level, 0) <= 0:
                    unconscious = True
        if dying:
            combatant["dying"] = True
            combatant["unconscious"] = True
            combatant["status"] = "dying"
        elif unconscious:
            combatant["unconscious"] = True
            combatant["status"] = "unconscious"
        elif combatant.get("status") == "dying":
            combatant["status"] = "active"
        elif combatant.get("status") == "unconscious":
            combatant["status"] = "active"

        outcome.hp_after = after
        outcome.major_wound = bool(combatant.get("major_wound"))
        outcome.dying = bool(combatant.get("dying"))
        outcome.unconscious = bool(combatant.get("unconscious"))
        outcome.dead = bool(combatant.get("dead"))
        if con_roll is not None:
            outcome.extra["con_check"] = {
                "roll": con_roll,
                "value": combatant.get("con"),
                "level": con_level,
            }
        return outcome

    def on_round_end(
        self,
        combatant: dict[str, Any],
        *,
        roll_d100,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> RoundEndOutcome:
        if not getattr(settings, "dying_check", True):
            return RoundEndOutcome()
        if not combatant.get("dying") or combatant.get("dead"):
            return RoundEndOutcome()
        if not callable(roll_d100):
            return RoundEndOutcome(needs_check=True)
        con_value = max(1, min(99, int(combatant.get("con") or 50)))
        roll = int(roll_d100())
        level = _success_level(roll, con_value, thresholds[0], thresholds[1])
        if LEVEL_RANK.get(level, 0) <= 0:
            combatant.update({"dead": True, "dying": False, "unconscious": True})
            combatant["status"] = "dead"
            return RoundEndOutcome(
                dead=True,
                needs_check=True,
                check=None,
                note=f"濒死体质检定失败（d100={roll}/{con_value}，{level}）",
            )
        return RoundEndOutcome(
            needs_check=True,
            note=f"濒死体质检定成功（d100={roll}/{con_value}，{level}）",
        )

    # -- 法术时序 ---------------------------------------------------------
    def spell_timing(self, cast_time: str, dex: int) -> dict[str, int]:
        # 即时法术 DEX+50；1 轮法术本轮 DEX；2 轮法术次轮 DEX（p206）。
        if cast_time == "instant":
            return {"round_offset": 0, "initiative_bonus": 50, "dex": dex + 50}
        if cast_time == "two_rounds":
            return {"round_offset": 1, "initiative_bonus": 0, "dex": dex}
        return {"round_offset": 0, "initiative_bonus": 0, "dex": dex}

    def spend_resource(
        self,
        combatant: dict[str, Any],
        resource: str,
        amount: int,
        *,
        roll_d100=None,
    ) -> dict[str, int]:
        cost = max(0, int(amount))
        key = str(resource or "").strip().lower()
        current = max(0, int(combatant.get(key) or 0))
        spent = min(current, cost)
        combatant[key] = current - spent
        return {"spent": spent, "overflow": cost - spent}
