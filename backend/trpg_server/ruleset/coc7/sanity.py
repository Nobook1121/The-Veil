"""CoC7 理智值与疯狂规则。

核心只负责定位成员、掷骰、广播与留痕；本模块承载全部 CoC7 语义：
理智上限（99 − 克苏鲁神话）、成功/失败损失表达式、掷出 100 取最大损失、
单次与单日 1/5 阈值、INT 检定与疯狂表现表。
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable

from trpg_server import dice_policy, room_clock
from trpg_server.ruleset.coc7 import rules as coc7_rules
from trpg_server.ruleset.coc7 import sanity_state, traits
from trpg_server.ruleset.coc7.options import resolve_options

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


def _loss_expression(expression: Any) -> str:
    """结构化 SAN 模式下：未带符号的表达式按「损失」理解。

    控制台的 ``normalize_change`` 早就采用同一约定；旧卡与开关关闭时仍按
    原样传给 ``_roll_value``，因此历史行为完全不变。
    """
    text = str(expression or "0").strip()
    if text[:1] in {"+", "-"}:
        return text
    return "-" + text


def _bout_mode(value: Any) -> str:
    """发作呈现方式：即时症状（1D10 轮）或总结症状（1D10 小时）。"""
    text = str(value or "").strip().lower()
    if text in {"immediate", "即时", "即时症状", "round", "rounds", "轮"}:
        return "immediate"
    if text in {"summary", "总结", "总结症状", "hour", "hours", "小时"}:
        return "summary"
    return "immediate"


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

    # ------------------------------------------------------------ 结构化精神状态

    def mental_block(self, card: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """取（必要时创建）结构化精神状态，并同步卡面扩展与房间状态。

        旧卡没有 ``extensions.mental`` 时按默认值补齐；不删除任何既有字段。
        """
        block = state.get("mental")
        if not isinstance(block, dict):
            extensions = card.get("extensions") if isinstance(card.get("extensions"), dict) else {}
            candidate = extensions.get("mental") if isinstance(extensions, dict) else None
            block = candidate if isinstance(candidate, dict) else {}
        block = sanity_state.normalize(block)
        state["mental"] = block
        extensions = card.get("extensions")
        if not isinstance(extensions, dict):
            extensions = {}
            card["extensions"] = extensions
        extensions["mental"] = block
        return block

    def mental_snapshot(
        self,
        card: dict[str, Any],
        state: dict[str, Any],
        room_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """只读快照：不创建字段，旧卡直接得到默认结构。

        发作的「进行中」判断与写入时使用同一个房间时钟口径（``room_clock.san_now``），
        旧数据按墙钟记录在快照副本中折算到当前口径，不回写。
        """
        options = resolve_options(room_info)
        block = state.get("mental")
        if not isinstance(block, dict):
            extensions = card.get("extensions") if isinstance(card.get("extensions"), dict) else {}
            block = extensions.get("mental") if isinstance(extensions, dict) else None
        data = sanity_state.normalize(block)
        now_ts, _, clock_kind = room_clock.san_now(room_info)
        sanity_state.rebase_timers(
            data,
            now=now_ts,
            target_clock=clock_kind,
            safety_minutes=options.sanity_bout_safety_minutes,
        )
        data["enabled"] = bool(options.sanity_state)
        data["activeBout"] = bool(
            options.sanity_state
            and sanity_state.has_active_bout(
                data, now_ts, safety_minutes=options.sanity_bout_safety_minutes
            )
        )
        return data

    def active_bout(
        self,
        card: dict[str, Any],
        state: dict[str, Any],
        room_info: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """当前仍在持续的疯狂发作；开关关闭或已过期时返回 ``None``。"""
        options = resolve_options(room_info)
        if not options.sanity_state:
            return None
        snapshot = self.mental_snapshot(card, state, room_info)
        bout = snapshot.get("bout")
        return dict(bout) if isinstance(bout, dict) and snapshot.get("activeBout") else None

    def fear_penalty(
        self,
        card: dict[str, Any],
        state: dict[str, Any],
        source: Any,
        room_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """恐惧 / 躁狂暴露：命中来源时返回 1 颗惩罚骰（开关关闭时为 0）。"""
        options = resolve_options(room_info)
        if not options.sanity_state or not options.sanity_traits:
            return {"penalty": 0, "matched": "", "kind": ""}
        snapshot = self.mental_snapshot(card, state)
        return sanity_state.fear_penalty(snapshot, str(source or ""))

    def traits_query(self, kind: Any = None, keyword: Any = None, *, limit: int = 10) -> dict[str, Any]:
        """按需查询恐惧症 / 躁狂症表；不整表注入上下文。"""
        return traits.query(kind, keyword, limit=limit)

    def _mental_pending(self, block: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "type": "extensions",
            "value": {"mental": dict(block)},
            "source": "ai_tool",
            "reason": reason,
        }

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
        room_info: dict[str, Any] | None = None,
        source: Any = None,
        illusion: bool = False,
    ) -> dict[str, Any]:
        """按成功 / 失败结算一次理智检定。

        ``room_info`` 决定批次 3 房规是否生效；全部关闭时与旧行为逐字一致。
        规则书第八章：
        - 疯狂发作期间免疫进一步的理智损失（p.138“笔记：关于幻觉和理智损失”）；
        - 现实认知成功后的免疫只针对**幻觉**，持续到下一次失去理智值（p.135）；
        - 习惯恐惧按来源累计，达到该存在单次遭遇的最大损失后不再损失（附录 p.386）。
        """
        options = resolve_options(room_info)
        if options.sanity_state:
            # 结构化模式把未带符号的「1d6」视为损失，与房间控制台保持一致。
            success_expr = _loss_expression(success_expr)
            failure_expr = _loss_expression(failure_expr)
        selected_expr = success_expr if success else failure_expr
        if roll == 100 and not success:
            # 7 版：理智检定掷出 100 时取该次判定的最大损失。
            delta, detail_rolls = -int(self.max_loss(failure_expr)), []
        else:
            delta, detail_rolls = _roll_value(selected_expr, roller)
        new_san = max(0, min(maximum, current + delta))
        loss = max(0, current - new_san)
        pending: list[dict[str, Any]] = []
        mental: dict[str, Any] | None = None
        habituation: dict[str, Any] | None = None
        waived = ""
        if options.sanity_state and loss > 0:
            mental = self.mental_block(card, state)
            # 发作判断必须和写入 started_at 使用同一个时钟口径：游戏时钟优先，
            # 旧墙钟记录先折算到当前口径，避免 time correct 后永远不到期。
            now_ts, now_text, clock_kind = room_clock.san_now(room_info)
            if options.sanity_bout:
                sanity_state.rebase_timers(
                    mental,
                    now=now_ts,
                    target_clock=clock_kind,
                    safety_minutes=options.sanity_bout_safety_minutes,
                )
            if options.sanity_bout and sanity_state.has_active_bout(
                mental, now_ts, safety_minutes=options.sanity_bout_safety_minutes
            ):
                # 规则书 p.138：“当调查员疯狂发作时，他将免疫进一步的理智值损失。”
                waived = "bout"
                delta = 0
                new_san = current
                loss = 0
            elif options.sanity_reality and illusion and mental.get("realityImmunity"):
                # 规则书第八章「疯狂的副作用2：幻想与现实的界限」（p.135）：
                # “成功：调查员将会看穿任何的自身产生的幻觉……并暂时获得对幻觉的
                #  抗性，直到调查员下一次失去理智值为止。（这也是为了防止守秘人会
                #  不停的给予调查员新的幻觉来抵消现实认知检定的效果。）”
                # 因此抗性只对 illusion=True 的损失生效；被它抵消的幻觉损失并没有
                # 真正失去理智值，不消耗抗性（这正是原文括号的用意，不是漏清标记）。
                waived = "reality"
                delta = 0
                new_san = current
                loss = 0
            if loss > 0 and options.sanity_habituation and str(source or "").strip():
                habituation = sanity_state.note_habituation(
                    mental,
                    str(source or ""),
                    loss,
                    max_loss=self.max_loss(failure_expr),
                )
                applied = int(habituation["applied"])
                if applied != loss:
                    loss = applied
                    delta = -applied
                    new_san = max(0, current - applied)
            # 同页原文只把“下一次失去理智值”作为终止条件：被抗性抵消、或完全被
            # 习惯恐惧吸收的损失都没有真正扣 SAN，所以不在这里清除抗性。
            if loss > 0:
                mental["realityImmunity"] = False
                mental["lastLossAt"] = now_text
            pending.append(self._mental_pending(mental, "理智检定"))
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
        if waived == "bout":
            summary += " 疯狂发作期间免疫进一步的理智损失，本次不再损失。"
        elif waived == "reality":
            summary += " 现实认知免疫抵消了来自幻觉的理智损失。"
        if habituation and habituation["immune"]:
            summary += (
                f" 习惯恐惧：对「{habituation['source']}」的累计损失已达上限 "
                f"{habituation['previous_total']}/{habituation['cap']}，本次不再损失。"
            )
        elif habituation and habituation["applied"] != habituation["loss"]:
            summary += (
                f" 习惯恐惧：对「{habituation['source']}」累计 {habituation['previous_total']}/"
                f"{habituation['cap']}，本次只损失 {habituation['applied']} 点。"
            )
        elif habituation:
            summary += (
                f" 习惯恐惧：记录对「{habituation['source']}」的累计理智损失 "
                f"{habituation['previous_total'] + habituation['applied']}/{habituation['cap'] or '?'}。"
            )
        if outcome["madness_required"]:
            summary += " 一次损失≥5点：请继续调用 san.roll_madness 做智力检定并抽取疯狂表现。"
        if daily.get("threshold_met"):
            summary += " 同一游戏日累计理智损失已达当日起始理智的 1/5，判定不定性疯狂。"
        result = {
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
        if mental is not None:
            result["mental"] = mental
            result["pending_changes"] = pending
            result["reality_immunity"] = bool(mental.get("realityImmunity"))
        if waived == "reality":
            result["reality_waived"] = True
        if waived == "bout":
            result["bout_immunity"] = True
        if habituation is not None:
            result["habituation"] = habituation
        return result

    # ------------------------------------------------------------ 疯狂

    def _gain_mythos(
        self,
        card: dict[str, Any],
        state: dict[str, Any],
        block: dict[str, Any],
        current_san: int,
    ) -> dict[str, Any]:
        """目击神话：首次 +5，之后每次 +1；同时压低理智上限。"""
        count = int(block.get("mythosMadnessCount") or 0)
        gain = 5 if count == 0 else 1
        block["mythosMadnessCount"] = count + 1

        skills = card.get("skills") if isinstance(card.get("skills"), list) else []
        row = next(
            (
                item
                for item in skills
                if isinstance(item, dict)
                and str(item.get("name") or "").strip() in {"克苏鲁神话", "Cthulhu Mythos"}
            ),
            None,
        )
        before = _as_int(row.get("value") if isinstance(row, dict) else 0, 0)
        after = max(0, min(99, before + gain))
        if row is None:
            row = {"name": "克苏鲁神话", "value": after, "base": 0}
            skills.append(row)
            card["skills"] = skills
        else:
            row["value"] = after

        maximum = max(0, 99 - after)
        state["max_san"] = maximum
        card["maxSan"] = maximum
        clamped = min(int(current_san or 0), maximum)
        state["current_san"] = clamped
        card["currentSan"] = card["current_san"] = clamped
        return {
            "gain": gain,
            "before": before,
            "after": after,
            "max_san": maximum,
            "san_clamped": clamped < int(current_san or 0),
            "san": clamped,
        }

    def _record_structured_madness(
        self,
        *,
        card: dict[str, Any],
        state: dict[str, Any],
        options: Any,
        madness_type: str,
        loss: int,
        source: str,
        bout_roll: int,
        bout: dict[str, Any],
        trait: str,
        trait_entry: dict[str, Any],
        duration_hours: int,
        duration_rounds: int,
        current_san: int,
        bout_mode: str = "immediate",
        started_at: str = "",
        clock: str = "wall",
    ) -> dict[str, Any]:
        """把疯狂结果写入结构化精神状态，并返回需要持久化的变更。

        ``started_at`` / ``clock`` 由调用方按房间统一时间口径给出，保证写入与
        到期判断使用同一个时钟。
        """
        block = self.mental_block(card, state)
        pending: list[dict[str, Any]] = []
        notes: list[str] = []
        trait_row: dict[str, Any] = {}
        if trait:
            kind = "phobia" if bout_roll == 9 else "mania"
            trait_row = sanity_state.add_trait(
                block,
                kind=kind,
                name=str(trait_entry.get("name") or trait.split("：", 1)[0]),
                tag=str(trait_entry.get("tag") or ""),
                en=str(trait_entry.get("en") or ""),
                source=source,
            )
        bout_record: dict[str, Any] = {}
        if options.sanity_bout:
            bout_record = sanity_state.start_bout(
                block,
                madness_type=madness_type,
                name=str(bout.get("name") or ""),
                description=str(bout.get("description") or ""),
                mode=bout_mode,
                duration_rounds=duration_rounds,
                duration_hours=duration_hours,
                trait=trait,
                source=source,
                san_loss=loss,
                bout_roll=bout_roll,
                started_at=started_at or None,
                clock=clock,
            )
            if bout_record["mode"] == "summary":
                notes.append(
                    f"发作（总结症状）：1D10={bout_record['duration_hours']} 小时，"
                    f"预计 {bout_record['expires_at']} 结束。"
                )
            else:
                notes.append(f"发作（即时症状）：1D10={bout_record['duration_rounds']} 个战斗轮。")
            if bout_record.get("symptoms"):
                notes.append("汇总症状：" + "、".join(bout_record["symptoms"]) + "。")
        # 疯狂阶段：临时性 1D10 小时；不定性持续到治愈；永久不结束。
        if options.sanity_state:
            insanity_kind = madness_type if madness_type in ("temporary", "indefinite", "permanent") else ""
            if insanity_kind:
                record = sanity_state.start_insanity(
                    block,
                    insanity_kind,
                    duration_hours=duration_hours,
                    source=source,
                    started_at=started_at or None,
                    clock=clock,
                )
                if record.get("endsAt"):
                    notes.append(f"临时性疯狂持续 {record['durationHours']} 小时，预计 {record['endsAt']} 恢复。")
                elif insanity_kind == "indefinite":
                    notes.append("不定性疯狂持续到被治愈（系统治疗需 1D6 个月）。")
                elif insanity_kind == "permanent":
                    notes.append("永久性疯狂不会自行恢复。")
        mythos: dict[str, Any] = {}
        if options.sanity_mythos:
            mythos = self._gain_mythos(card, state, block, current_san)
            notes.append(f"克苏鲁神话 +{mythos['gain']}（{mythos['before']}%→{mythos['after']}%）。")
            pending.append(
                {
                    "type": "skill",
                    "name": "克苏鲁神话",
                    "value": mythos["after"],
                    "source": "ai_tool",
                    "reason": "目击神话",
                }
            )
            if mythos["san_clamped"]:
                pending.append(
                    {"type": "san", "value": mythos["san"], "source": "ai_tool", "reason": "理智上限下降"}
                )
                notes.append(f"理智上限降至 {mythos['max_san']}。")
        pending.append(self._mental_pending(block, "疯狂发作"))
        return {
            "mental": block,
            "bout": bout_record,
            "trait_row": trait_row,
            "mythos": mythos,
            "pending_changes": pending,
            "notes": notes,
        }

    def begin_bout(
        self,
        *,
        card: dict[str, Any],
        state: dict[str, Any],
        room_info: dict[str, Any] | None = None,
        madness_type: str = "indefinite",
        source: str = "",
        mode: str = "immediate",
        roller: Callable[[int], int] | None = None,
    ) -> dict[str, Any]:
        """外部裁定触发的发作（现实认知失败等）。

        即时症状持续 1D10 个战斗轮；总结症状持续 1D10 小时。
        """
        options = resolve_options(room_info)
        if not options.sanity_state or not options.sanity_bout:
            return {}
        pick = roller or (lambda sides: random.randint(1, sides))
        bout_roll = max(1, min(10, int(pick(10))))
        bout = dict(MADNESS_BOUTS[bout_roll - 1])
        rounds = max(1, min(10, int(pick(10))))
        hours = max(1, min(10, int(pick(10))))
        block = self.mental_block(card, state)
        _, now_text, clock_kind = room_clock.san_now(room_info)
        record = sanity_state.start_bout(
            block,
            madness_type=madness_type,
            name=str(bout.get("name") or ""),
            description=str(bout.get("description") or ""),
            mode=mode,
            duration_rounds=rounds,
            duration_hours=hours,
            source=source,
            bout_roll=bout_roll,
            started_at=now_text,
            clock=clock_kind,
        )
        if record.get("mode") == "summary":
            timing = f"1D10={record.get('duration_hours')} 小时，预计 {record.get('expires_at')} 结束"
        else:
            timing = f"1D10={record.get('duration_rounds')} 个战斗轮"
        return {
            "mental": block,
            "bout": record,
            "pending_changes": [self._mental_pending(block, "发作")],
            "summary": f"进入发作「{record.get('name')}」（{timing}）。",
        }

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
        room_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CoC7 临时 / 不定 / 永久疯狂结算。"""
        options = resolve_options(room_info)
        # 疯狂发作 / 临时性疯狂的写入与后续到期判断共用同一时钟口径。
        _, now_text, clock_kind = room_clock.san_now(room_info)
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
        bout_mode = _bout_mode(arguments.get("bout_mode"))
        bout_roll = max(1, min(10, int(roller(10))))
        bout = dict(MADNESS_BOUTS[bout_roll - 1])
        duration_hours = max(1, min(10, int(roller(10))))
        duration_rounds = max(1, min(10, int(roller(10))))
        trait = ""
        trait_entry: dict[str, Any] = {}
        if insane and bout_roll in (9, 10):
            kind = "phobia" if bout_roll == 9 else "mania"
            if options.sanity_state and options.sanity_traits:
                trait_entry = traits.random_entry(kind, roller)
                trait = traits.format_entry(trait_entry)
            else:
                pool = SAMPLE_PHOBIAS if bout_roll == 9 else SAMPLE_MANIAS
                trait = pool[max(1, min(10, int(roller(10)))) - 1]
            bout["trait"] = trait

        flags = state.setdefault("mental_status", {})
        if not isinstance(flags, dict):
            flags = {}
            state["mental_status"] = flags
        structured: dict[str, Any] = {}
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
            if options.sanity_state:
                structured = self._record_structured_madness(
                    card=card,
                    state=state,
                    options=options,
                    madness_type=madness_type,
                    loss=loss,
                    source=str(arguments.get("cause") or arguments.get("source") or "").strip(),
                    bout_roll=bout_roll,
                    bout=bout,
                    trait=trait,
                    trait_entry=trait_entry,
                    duration_hours=duration_hours,
                    duration_rounds=duration_rounds,
                    current_san=current_san,
                    bout_mode=bout_mode,
                    started_at=now_text,
                    clock=clock_kind,
                )
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

        if structured.get("notes"):
            summary += " " + " ".join(structured["notes"])

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
        pending_changes.extend(structured.get("pending_changes") or [])

        result = {
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
        if structured:
            result["mental"] = structured["mental"]
            result["bout_record"] = structured["bout"]
            result["trait_record"] = structured["trait_row"]
            result["bout_mode"] = bout_mode
            if structured["mythos"]:
                result["mythos"] = structured["mythos"]
        return result


SANITY = Coc7Sanity()
