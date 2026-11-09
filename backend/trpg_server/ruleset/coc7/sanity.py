"""CoC7 理智值与疯狂规则。

核心只负责定位成员、掷骰、广播与留痕；本模块承载全部 CoC7 语义：
理智上限（99 − 克苏鲁神话）、成功/失败损失表达式、掷出 100 取最大损失、
单次与单日 1/5 阈值、INT 检定与疯狂表现表。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from trpg_server import dice_policy
from trpg_server.ruleset.coc7 import rules as coc7_rules

# 一次理智损失达到该值时进入临时疯狂判定。
MADNESS_LOSS_THRESHOLD = 5
# 单次 / 单日累计损失达到当前理智的 1/5 时判定不定性疯狂。
DAILY_LOSS_DIVISOR = 5

# CoC7 7th Edition「Bouts of Madness」表（1D10，Keeper 也可直接选择）。
MADNESS_BOUTS: tuple[dict[str, Any], ...] = (
    {"roll": 1, "name": "失忆", "description": "调查员不记得自上次处于安全地点以来发生的事，持续 1D10 轮。"},
    {"roll": 2, "name": "心身障碍", "description": "调查员出现心身性失明、失聪或失去某肢体功能，持续 1D10 轮。"},
    {"roll": 3, "name": "暴力", "description": "调查员陷入狂暴，对周围所有人无差别地施暴与破坏，持续 1D10 轮。"},
    {"roll": 4, "name": "偏执", "description": "调查员陷入严重偏执，认为所有人都在害他、监视他、背叛他，持续 1D10 轮。"},
    {"roll": 5, "name": "重要之人", "description": "调查员把场景中的某人误认为自己的重要之人，并按其关系行事，持续 1D10 轮。"},
    {"roll": 6, "name": "昏厥", "description": "调查员昏倒，1D10 轮后恢复。"},
    {"roll": 7, "name": "惊恐逃离", "description": "调查员不顾一切地逃离现场，甚至抢走唯一载具丢下同伴，持续 1D10 轮。"},
    {"roll": 8, "name": "歇斯底里", "description": "调查员无法控制地大笑、哭泣或尖叫，持续 1D10 轮。"},
    {"roll": 9, "name": "恐惧症", "description": "调查员获得新的恐惧症；即使源头不在场也会幻想它在附近，持续 1D10 轮，期间所有行动承受惩罚骰。"},
    {"roll": 10, "name": "躁狂症", "description": "调查员获得新的躁狂症；在疯狂期间会沉溺于该冲动，持续 1D10 轮，期间所有行动承受惩罚骰。"},
)
SAMPLE_PHOBIAS = (
    "幽闭恐惧（claustrophobia）", "恶魔恐惧（demonophobia）", "蟑螂恐惧（katsaridaphobia）",
    "黑暗恐惧", "血液恐惧", "镜子恐惧", "蜘蛛恐惧", "高处恐惧", "电话恐惧", "陌生人恐惧",
)
SAMPLE_MANIAS = (
    "洁癖（ablutomania）", "谎言癖（pseudomania）", "偷窃癖（kleptomania）", "疼痛迷恋（algomania）",
    "纵火癖", "大笑狂（geliomania）", "呼喊狂（klazomania）", "藏书癖（bibliokleptomania）",
    "正义狂（dikemania）", "病痛妄想（nosomania）",
)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _nullable_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _roll_value(expression: Any, roller: Callable[[int], int]) -> tuple[int, list[int]]:
    """计算一次 SAN 变化：支持 ``NdM`` 与整数，带正负号。"""
    text = str(expression or "0").strip()
    sign = -1 if text.startswith("-") else 1
    if text[:1] in {"+", "-"}:
        text = text[1:]
    match = re.fullmatch(r"(\d+)d(\d+)", text.lower())
    if match:
        count, sides = int(match.group(1)), int(match.group(2))
        if count < 1 or count > 100 or sides < 2 or sides > 1000:
            raise ValueError("SAN dice expression is out of range")
        rolls = [int(roller(sides)) for _ in range(count)]
        return sign * sum(rolls), rolls
    if not re.fullmatch(r"\d+", text):
        raise ValueError("SAN change must be a number or dice expression such as 1d6")
    return sign * int(text), [int(text)]


class Coc7Sanity:
    """CoC7 的理智 / 疯狂能力。"""

    ruleset_id = "coc7"
    loss_threshold = MADNESS_LOSS_THRESHOLD
    # 核心的骰桌展示与工具输出只读取这两个名字，不再硬编码 CoC 文案。
    check_name = "理智"
    madness_check_name = "疯狂表现"

    def __init__(self) -> None:
        self.policy = dice_policy.PercentileRollPolicy()

    # ------------------------------------------------------------ 读数

    def snapshot(self, card: dict[str, Any], derived: dict[str, Any], state: dict[str, Any], *, write: bool = False) -> dict[str, int]:
        """理智上限 = 99 − 克苏鲁神话值；当前值优先取房间状态，其次持久卡派生值。"""
        maximum = _as_int(
            derived.get("maxSan"),
            _as_int(state.get("max_san"), _as_int(card.get("maxSan", card.get("max_san")), 0)),
        )
        maximum = max(0, min(999, maximum))
        fallback = _as_int(
            derived.get("currentSan"),
            _as_int(
                card.get("currentSan", card.get("current_san")),
                min(maximum, _as_int(derived.get("initialSan"), maximum)),
            ),
        )
        current = max(0, min(maximum, _as_int(state.get("current_san"), fallback)))
        if write:
            state["max_san"] = maximum
            state["current_san"] = current
            card["maxSan"] = maximum
            card["currentSan"] = current
            card["current_san"] = current
        return {"current": current, "max": maximum}

    # ------------------------------------------------------------ 输入与上限

    def normalize_change(self, value: Any) -> str:
        """玩家命令默认把「1d4/1d10」视为损失；显式带 ``+`` 才表示恢复。"""
        text = str(value or "").strip()
        if "/" not in text:
            raise ValueError("理智值变化必须使用「成功/失败」格式，例如 1d4/1d10")
        parts = []
        for part in text.split("/", 1):
            segment = part.strip()
            if segment and segment[0] not in "+-":
                segment = "-" + segment
            parts.append(segment)
        return "/".join(parts)

    def max_loss(self, expression: Any) -> int:
        """一次 SAN 变化的最大损失；用于「掷出 100 取最大值」。"""
        return coc7_rules.max_of_expression(expression)

    def madness_required(self, loss: int) -> bool:
        return loss >= MADNESS_LOSS_THRESHOLD

    # ------------------------------------------------------------ 结算

    def resolve_loss(
        self,
        *,
        card: dict[str, Any],
        state: dict[str, Any],
        before: int,
        new_san: int,
        maximum: int,
        loss: int,
        day: str,
        record_last: bool = True,
        write_card: bool = True,
    ) -> dict[str, Any]:
        """应用一次理智损失：累计当日损失、判定不定性 / 永久疯狂并写镜像。"""
        daily: dict[str, Any] = {}
        if loss > 0:
            daily = coc7_rules.record_daily_loss(state, day=day, before=before, loss=loss)
        flags = state.setdefault("mental_status", {})
        if not isinstance(flags, dict):
            flags = {}
            state["mental_status"] = flags
        indefinite = loss > 0 and (loss * DAILY_LOSS_DIVISOR >= before or bool(daily.get("threshold_met")))
        if indefinite:
            flags["indefiniteInsanity"] = flags["indefinite_insanity"] = True
        permanent = new_san <= 0
        if permanent:
            flags["permanentInsanity"] = flags["permanent_insanity"] = True

        state["current_san"] = new_san
        state["max_san"] = maximum
        if record_last:
            state["last_san_loss"] = loss
            state["last_san_before"] = before
        if write_card:
            card["currentSan"] = new_san
            card["current_san"] = new_san
            card["maxSan"] = maximum
            card["mental_status"] = flags
            card["status"] = flags
            card["temporary_insanity"] = bool(flags.get("temporaryInsanity"))
            card["indefinite_insanity"] = bool(flags.get("indefiniteInsanity"))
            card["permanent_insanity"] = bool(flags.get("permanentInsanity"))
        return {
            "daily": daily,
            "flags": flags,
            "madness_required": self.madness_required(loss),
            "indefinite": indefinite,
            "permanent": permanent,
        }

    def resolve_check(
        self,
        *,
        player_name: str,
        card: dict[str, Any],
        state: dict[str, Any],
        current: int,
        maximum: int,
        roll: int,
        success: bool,
        success_expr: str,
        failure_expr: str,
        roller: Callable[[int], int],
        day: str,
    ) -> dict[str, Any]:
        """按成功 / 失败结算一次理智检定。"""
        selected_expr = success_expr if success else failure_expr
        if roll == 100 and not success:
            # 7 版：理智检定掷出 100 时取该次判定的最大损失。
            delta, detail_rolls = -int(self.max_loss(failure_expr)), []
        else:
            delta, detail_rolls = _roll_value(selected_expr, roller)
        new_san = max(0, min(maximum, current + delta))
        loss = max(0, current - new_san)
        outcome = self.resolve_loss(
            card=card,
            state=state,
            before=current,
            new_san=new_san,
            maximum=maximum,
            loss=loss,
            day=day,
        )
        daily = outcome["daily"]
        flags = outcome["flags"]
        summary = (
            f"{player_name} 理智检定：1d100=[{roll}] / 当前 SAN {current}，{'成功' if success else '失败'}；"
            f"理智变化 {selected_expr} = {delta:+d}，SAN {current}→{new_san}。"
        )
        if roll == 100 and not success:
            summary += " 掷出 100，本次按最大损失结算。"
        if outcome["madness_required"]:
            summary += " 一次损失≥5点：请继续调用 san.roll_madness 做智力检定并抽取疯狂表现。"
        if daily.get("threshold_met"):
            summary += " 同一游戏日累计理智损失已达当日起始理智的 1/5，判定不定性疯狂。"
        return {
            "delta": delta,
            "new_san": new_san,
            "detail_rolls": detail_rolls,
            "daily": daily,
            "flags": flags,
            "madness_required": outcome["madness_required"],
            "madness_note": "一次损失≥5点，请调用 san.roll_madness 进行智力检定并抽取疯狂表现"
            if outcome["madness_required"]
            else "",
            "summary": summary,
        }

    # ------------------------------------------------------------ 疯狂

    def resolve_madness(
        self,
        *,
        player_name: str,
        card: dict[str, Any],
        state: dict[str, Any],
        arguments: dict[str, Any],
        roller: Callable[[int], int],
        bonus: int,
        penalty: int,
        critical_threshold: int,
        fumble_threshold: int,
        success_level: Callable[[int, int, int, int], str],
    ) -> dict[str, Any]:
        """CoC7 临时 / 不定 / 永久疯狂结算。"""
        loss = _nullable_int(arguments.get("loss"))
        if loss is None:
            loss = _nullable_int(state.get("last_san_loss"))
        loss = max(0, _as_int(loss, 0))
        current_san = _as_int(state.get("current_san", card.get("currentSan", card.get("current_san", 0))), 0)
        force = bool(arguments.get("force"))
        if loss < MADNESS_LOSS_THRESHOLD and current_san > 0 and not force:
            return {
                "player_name": player_name,
                "loss": loss,
                "skipped": True,
                "summary": f"{player_name} 本次理智损失 {loss} 点（<5），未触发 CoC7 临时疯狂判定。",
            }

        before = _as_int(state.get("last_san_before", current_san + loss), current_san + loss)
        daily = state.get("san_daily") if isinstance(state.get("san_daily"), dict) else {}
        indefinite = (before > 0 and loss * DAILY_LOSS_DIVISOR >= before) or bool(daily.get("threshold_met")) or bool(
            (state.get("mental_status") or {}).get("indefiniteInsanity")
            if isinstance(state.get("mental_status"), dict)
            else False
        )
        permanent = current_san <= 0

        attrs = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}
        int_target = _as_int(attrs.get("INT", card.get("int")), 0)
        int_roll = dice_policy.roll_with_policy(self.policy, roller, bonus, penalty)[0]
        int_roll = max(1, min(100, int(int_roll)))
        int_level = success_level(int_roll, int_target, critical_threshold, fumble_threshold)
        int_success = int_target > 0 and int_roll <= int_target

        insane = permanent or indefinite or (loss >= MADNESS_LOSS_THRESHOLD and int_success)
        bout_roll = max(1, min(10, int(roller(10))))
        bout = dict(MADNESS_BOUTS[bout_roll - 1])
        duration_hours = max(1, min(10, int(roller(10))))
        duration_rounds = max(1, min(10, int(roller(10))))
        trait = ""
        if insane and bout_roll in (9, 10):
            pool = SAMPLE_PHOBIAS if bout_roll == 9 else SAMPLE_MANIAS
            trait = pool[max(1, min(10, int(roller(10)))) - 1]
            bout["trait"] = trait

        flags = state.setdefault("mental_status", {})
        if not isinstance(flags, dict):
            flags = {}
            state["mental_status"] = flags
        madness_record: dict[str, Any] | None = None
        if insane:
            madness_type = "permanent" if permanent else "indefinite" if indefinite else "temporary"
            flags["temporaryInsanity"] = flags["temporary_insanity"] = madness_type == "temporary"
            flags["indefiniteInsanity"] = flags["indefinite_insanity"] = madness_type == "indefinite"
            flags["permanentInsanity"] = flags["permanent_insanity"] = madness_type == "permanent"
            madness_record = {
                "type": madness_type,
                "loss": loss,
                "cause": str(arguments.get("cause") or arguments.get("source") or "").strip(),
                "int_roll": int_roll,
                "int_target": int_target,
                "int_success": int_success,
                "int_level": int_level,
                "bout_roll": bout_roll,
                "bout_name": bout["name"],
                "bout_description": bout["description"],
                "duration_hours": duration_hours if madness_type == "temporary" else None,
                "duration_rounds": duration_rounds,
                "trait": trait,
            }
            card["madness"] = madness_record
            if trait:
                background = card.setdefault("background", {})
                if not isinstance(background, dict):
                    background = {}
                    card["background"] = background
                existing = str(background.get("phobiasManias") or "").strip()
                background["phobiasManias"] = f"{existing}；{trait}".strip("；") if existing else trait
            card["mental_status"] = flags
            card["status"] = flags
            card["temporary_insanity"] = madness_type == "temporary"
            card["indefinite_insanity"] = madness_type == "indefinite"
            card["permanent_insanity"] = madness_type == "permanent"
        else:
            # INT 检定失败：记忆被压抑，不进入疯狂，也不写入新的疯狂记录。
            flags.setdefault("temporaryInsanity", flags.get("temporary_insanity", False))
            flags.setdefault("temporary_insanity", flags.get("temporaryInsanity", False))
            card["mental_status"] = flags
            card["status"] = flags

        state["last_san_loss"] = 0

        if insane:
            if permanent:
                summary = f"{player_name} 永久性疯狂：SAN 归零。"
            elif indefinite:
                daily_total = _as_int(daily.get("loss"), loss)
                summary = (
                    f"{player_name} 不定性疯狂：本游戏日累计理智损失 {daily_total} 点"
                    "（达到当日起始理智的 1/5），需接受治疗才能恢复。"
                )
            else:
                summary = (
                    f"{player_name} 触发临时性疯狂：智力检定 1d100={int_roll} ≤ INT {int_target}，"
                    f"持续 1d10={duration_hours} 小时。"
                )
            summary += f" 疯狂表现（1d10={bout_roll}）：{bout['name']}——{bout['description']}"
            if trait:
                summary += f" 新增：{trait}。"
        else:
            summary = (
                f"{player_name} 一次损失 {loss} 点理智，但智力检定 1d100={int_roll} > INT {int_target}，"
                "记忆被压抑，未进入临时性疯狂。"
            )

        pending_changes: list[dict[str, Any]] = []
        if insane:
            pending_changes.append(
                {"type": "status", "value": dict(flags), "source": "ai_tool", "reason": "疯狂表现"}
            )
            background = card.get("background")
            if isinstance(background, dict):
                pending_changes.append(
                    {"type": "background", "value": dict(background), "source": "ai_tool", "reason": "疯狂表现"}
                )

        return {
            "player_name": player_name,
            "roll": int_roll,
            "success": int_success,
            "success_level": int_level,
            "loss": loss,
            "int_roll": int_roll,
            "int_target": int_target,
            "int_success": int_success,
            "insane": insane,
            "indefinite": indefinite and insane,
            "permanent": permanent,
            "bout_roll": bout_roll,
            "bout": bout if insane else None,
            "duration_hours": duration_hours if insane and not permanent and not indefinite else None,
            "duration_rounds": duration_rounds if insane else None,
            "trait": trait,
            "madness": madness_record,
            "summary": summary,
            "pending_changes": pending_changes,
            "persist": insane,
        }


SANITY = Coc7Sanity()
