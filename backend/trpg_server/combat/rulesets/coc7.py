"""CoC7 规则书插件。

只把 CoC7 的“判定/伤害/濒死/行动经济”语义收在这里；武器表、法术表、怪物表
仍然由内容层提供，插件不硬编码具体条目。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..contracts import DamageOutcome, RoundEndOutcome

LEVEL_RANK = {"fumble": -1, "failure": 0, "regular": 1, "hard": 2, "extreme": 3, "critical": 4}
SKIPPED_STATUS = {"unconscious", "dying", "dead"}


def _success_level(roll: int, target: int, critical: int = 1, fumble: int = 96) -> str:
    """CoC7 成功等级，和 dice 模块保持同一套阈值语义。"""
    if roll <= critical:
        return "critical"
    if roll <= target // 5:
        return "extreme"
    if roll <= target // 2:
        return "hard"
    if roll <= target:
        return "regular"
    if roll >= fumble:
        return "fumble"
    return "failure"


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
