"""D&D 5e 检定与豁免：d20 + 属性调整值 + 熟练加值。

结果统一交给 ``CheckOutcome``；成功等级（自然 1/20、普通成功/失败）只是
插件自定的字符串，核心不比较、不解释。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Mapping

from dnd5e_plugin.effect_changes import effective_sheet
from dnd5e_plugin.rules import (
    ABILITY_KEYS,
    ABILITY_LABELS,
    SKILLS,
    ability_key,
    check_bonus,
    check_label,
    parse_check,
    sheet_of,
)
from trpg_server.ruleset.check_outcome import CheckOutcome
from trpg_server.ruleset.contracts import CheckRequest


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _fmt_modifier(value: int) -> str:
    return f"+ {value}" if value >= 0 else f"- {abs(value)}"


class Dnd5eChecks:
    ruleset_id = "dnd5e"

    def target_bounds(self) -> tuple[int | None, int | None]:
        """DC 的合理范围；核心只用于输入校验。"""
        return 0, 40

    def is_known_check_name(self, check_name: str) -> bool:
        return parse_check(check_name) is not None

    def lookup_target(self, card: Mapping[str, Any], name: str) -> int | None:
        """旧百分制入口不适用；5e 的目标值是 DC，由调用方显式传入。"""
        return None

    def list_check_names(self, card: Mapping[str, Any]) -> list[str]:
        names = [f"{ABILITY_LABELS[key]}检定" for key in ABILITY_KEYS]
        names += [f"{ABILITY_LABELS[key]}豁免" for key in ABILITY_KEYS]
        names += [f"{SKILLS[key][1]}检定" for key in SKILLS]
        return names

    def display_name(self, card: Mapping[str, Any], check_name: str) -> str:
        parsed = parse_check(check_name)
        return check_label(parsed) if parsed else str(check_name or "")

    def player_name_check_message(self, check_name: str) -> str:
        return f"“{check_name}”是玩家或角色名；请填写检定名（如 力量/察觉/敏捷豁免）。"

    def unknown_check_message(self, check_name: str) -> str:
        return f"“{check_name}”不是 5e 认可的检定或豁免名。"

    def roll_outcome(
        self,
        card: Mapping[str, Any],
        request: CheckRequest,
        rng: Callable[[int], int] | None = None,
    ) -> CheckOutcome:
        kind = str(request.kind or "").strip().lower()
        if kind == "attack":
            return self._roll_attack(card, request, rng)
        parsed = parse_check(request.name)
        if parsed is None and kind in {"save", "saving_throw"}:
            ability = ability_key((request.extra or {}).get("ability") or request.name)
            if ability:
                parsed = {"kind": "save", "ability": ability}
        if parsed is None:
            raise ValueError(f"未知的检定名：{request.name}")
        sheet = sheet_of(card)
        extra = dict(request.extra or {})
        dc = _as_int(request.target if request.target is not None else extra.get("dc"), 10)
        # 效果引擎：路径式加值与 d20 模式（无效果时 view 为空，下面两行不产生任何变化）。
        effects = effective_sheet(sheet)
        effect_modifier = 0
        effect_mode = 0
        if not effects.empty:
            effect_modifier = effects.check_bonus(parsed.get("ability"), parsed.get("skill"))
            kind_of_check = "save" if str(parsed.get("kind")) == "save" else "check"
            effect_mode = effects.roll_mode(parsed.get("ability"), "save" if kind_of_check == "save" else "check")
            if kind_of_check == "save":
                effect_modifier += effects.save_bonus(parsed.get("ability"))
        advantage = bool(extra.get("advantage") or extra.get("adv")) or effect_mode > 0
        disadvantage = bool(extra.get("disadvantage") or extra.get("dis")) or effect_mode < 0
        if advantage and disadvantage:
            advantage = disadvantage = False

        roller = rng or (lambda sides: random.randint(1, sides))
        dice = [int(roller(20))]
        if advantage or disadvantage:
            dice.append(int(roller(20)))
        natural = max(dice) if advantage else min(dice) if disadvantage else dice[0]

        modifier, detail = check_bonus(
            sheet,
            parsed,
            extra_modifier=effect_modifier + _as_int(request.modifier, 0),
        )
        if effect_modifier:
            # 只在真有时附加键：无效果时 detail 与旧实现逐字节一致。
            detail["effect_modifier"] = effect_modifier
        total = natural + modifier
        auto_fail = bool(extra.get("auto_fail"))
        if auto_fail:
            degree = "auto_failure"
            success = False
        elif natural == 20:
            degree = "critical_success"
            success = True
        elif natural == 1:
            degree = "critical_failure"
            success = False
        else:
            success = total >= dc
            degree = "success" if success else "failure"

        label = check_label(parsed)
        result_text = {
            "critical_success": "大成功",
            "success": "成功",
            "failure": "失败",
            "critical_failure": "大失败",
            "auto_failure": "自动失败",
        }[degree]
        summary = f"{label}：d20 [{natural}] {_fmt_modifier(modifier)} = {total}，DC {dc}，{result_text}。"
        detail.update(
            {
                "natural": natural,
                "dc": dc,
                "dice": list(dice),
                "kept": natural,
                "advantage": advantage,
                "disadvantage": disadvantage,
                "modifier": modifier,
                "label": label,
                "auto_fail": auto_fail,
            }
        )
        return CheckOutcome(
            ruleset_id=self.ruleset_id,
            kind=str(parsed.get("kind") or request.kind or "check"),
            dice=tuple(dice),
            total=total,
            target=dc,
            margin=total - dc,
            degree=degree,
            success=success,
            detail=detail,
            presentation={
                "mode": "d20",
                "label": label,
                "rolls_text": ", ".join(str(item) for item in dice),
                "summary": summary,
                "dc": dc,
                "degree": degree,
            },
        )

    def _roll_attack(
        self,
        card: Mapping[str, Any],
        request: CheckRequest,
        rng: Callable[[int], int] | None = None,
    ) -> CheckOutcome:
        """法术/远程攻击：d20 + 加值 vs AC；核心只拿到 ``CheckOutcome``。"""
        extra = dict(request.extra or {})
        ac = _as_int(request.target if request.target is not None else extra.get("ac"), 10)
        modifier = _as_int(request.modifier, 0)
        advantage = bool(extra.get("advantage") or extra.get("adv"))
        disadvantage = bool(extra.get("disadvantage") or extra.get("dis"))
        if advantage and disadvantage:
            advantage = disadvantage = False
        roller = rng or (lambda sides: random.randint(1, sides))
        dice = [int(roller(20))]
        if advantage or disadvantage:
            dice.append(int(roller(20)))
        natural = max(dice) if advantage else min(dice) if disadvantage else dice[0]
        total = natural + modifier
        if natural == 20:
            degree = "critical_success"
            success = True
        elif natural == 1:
            degree = "critical_failure"
            success = False
        else:
            success = total >= ac
            degree = "hit" if success else "miss"
        label = str(extra.get("label") or "法术攻击")
        result_text = "暴击" if natural == 20 else ("命中" if success else "未命中")
        summary = (
            f"{label}：d20 [{natural}] {_fmt_modifier(modifier)} = {total}，AC {ac}，{result_text}。"
        )
        detail = {
            "natural": natural,
            "ac": ac,
            "dice": list(dice),
            "kept": natural,
            "modifier": modifier,
            "advantage": advantage,
            "disadvantage": disadvantage,
            "label": label,
        }
        return CheckOutcome(
            ruleset_id=self.ruleset_id,
            kind="attack",
            dice=tuple(dice),
            total=total,
            target=ac,
            margin=total - ac,
            degree=degree,
            success=success,
            detail=detail,
            presentation={
                "mode": "d20",
                "label": label,
                "rolls_text": ", ".join(str(item) for item in dice),
                "summary": summary,
                "ac": ac,
                "degree": degree,
            },
        )
