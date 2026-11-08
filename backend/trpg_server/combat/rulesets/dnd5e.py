"""D&D 5e 参考插件。

存在的目的不是把 5e 做全，而是验证核心抽象是否只依赖“先攻 / 行动经济 / 判定 /
伤害 / 轮末钩子”，而不是 CoC7 的字段名。房间战斗面板暂时只启用 CoC7，
5e 插件由契约测试钉住；接入角色卡后再替换 ruleset 即可。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..contracts import DamageOutcome, RoundEndOutcome


def _ability_mod(score: int) -> int:
    return (int(score) - 10) // 2


class Dnd5eRuleset:
    ruleset_id = "dnd5e"
    display_name = "Dungeons & Dragons 5e"

    def roll_initiative(self, combatant: Mapping[str, Any], rng: Any, settings: Any) -> int:
        dex = int(
            combatant.get("dex")
            or (combatant.get("abilities") or {}).get("dex")
            or 10
        )
        return int(rng.randint(1, 20)) + _ability_mod(dex)

    def initiative_tiebreak(self, combatant: Mapping[str, Any]) -> int:
        return int(combatant.get("dex") or (combatant.get("abilities") or {}).get("dex") or 10)

    def actions_per_round(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def reaction_limit(self, combatant: Mapping[str, Any], settings: Any) -> int:
        return 1

    def action_budget(self, combatant: Mapping[str, Any], settings: Any) -> dict[str, int]:
        return {
            "action": 1,
            "bonus_action": 1,
            "reaction": 1,
            "movement": int(combatant.get("speed") or 30),
        }

    def is_incapacitated(self, combatant: Mapping[str, Any]) -> bool:
        if str(combatant.get("status") or "") in {"dead", "unconscious", "incapacitated"}:
            return True
        return int(combatant.get("hp") or 0) <= 0

    def resolve_attack(self, attack_total: int, target_ac: int) -> str:
        """5e 是 d20+加值 对 AC，不是成功等级对抗。"""

        return "hit" if int(attack_total) >= int(target_ac) else "miss"

    def resolve_melee(
        self, attack_level: str, defense_level: str, defense_kind: str
    ) -> str:
        # 适配契约：5e 没有“反击”作为默认反应，攻击要么命中要么失手。
        return "hit" if attack_level in {"regular", "hard", "extreme", "critical"} else "miss"

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
        before = int(combatant.get("hp") or 0)
        max_hp = max(1, int(combatant.get("max_hp") or 1))
        after = before - damage
        combatant["hp"] = after
        outcome = DamageOutcome(
            damage=damage,
            armor=0,
            effective=damage,
            hp_before=before,
            hp_after=max(0, after),
        )
        if after <= 0:
            combatant["hp"] = 0
            # 溢出伤害 >= 最大耐久 → 即死；否则倒地并开始死亡豁免。
            if -after >= max_hp:
                combatant.update({"dead": True, "status": "dead"})
                outcome.dead = True
            else:
                combatant.update({"unconscious": True, "dying": True, "status": "unconscious"})
                combatant.setdefault("death_saves", {"successes": 0, "failures": 0})
                outcome.dying = True
                outcome.unconscious = True
        return outcome

    def on_round_end(
        self,
        combatant: dict[str, Any],
        *,
        roll_d100,
        thresholds: tuple[int, int],
        settings: Any,
    ) -> RoundEndOutcome:
        if not combatant.get("dying") or combatant.get("dead"):
            return RoundEndOutcome()
        saves = combatant.setdefault("death_saves", {"successes": 0, "failures": 0})
        # 5e 用 d20，而不是 d100；这里直接复用注入的随机源。
        roll = int(roll_d100()) if callable(roll_d100) else 0
        if roll >= 10:
            saves["successes"] = int(saves.get("successes", 0)) + 1
        else:
            saves["failures"] = int(saves.get("failures", 0)) + 1
        if int(saves["failures"]) >= 3:
            combatant.update({"dead": True, "dying": False, "status": "dead"})
            return RoundEndOutcome(dead=True, note="死亡豁免失败三次")
        if int(saves["successes"]) >= 3:
            combatant.update({"dying": False, "unconscious": True, "status": "unconscious"})
            return RoundEndOutcome(note="死亡豁免成功三次，伤势稳定")
        return RoundEndOutcome(note=f"死亡豁免 {roll}（成功 {saves['successes']}/3，失败 {saves['failures']}/3）")

    def spell_timing(self, cast_time: str, dex: int) -> dict[str, int]:
        return {"round_offset": 0, "initiative_bonus": 0, "dex": dex}

    def spend_resource(
        self,
        combatant: dict[str, Any],
        resource: str,
        amount: int,
        *,
        roll_d100: Any = None,
    ) -> dict[str, int]:
        # 5e 的资源是法术位/职业资源，统一按“同名字段”扣除；溢出由核心决定。
        cost = max(0, int(amount))
        key = str(resource or "").strip().lower()
        current = max(0, int(combatant.get(key) or 0))
        spent = min(current, cost)
        combatant[key] = current - spent
        return {"spent": spent, "overflow": cost - spent}
