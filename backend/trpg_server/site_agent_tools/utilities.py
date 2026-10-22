"""通用小工具：骰子、随机选择、日期计算。

这些工具没有站点写权限，也不返回任何界面动画数据（Agent 窗口只展示文本结果）。
"""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta
from typing import Any

from trpg_server.agents.tools.dice import roll_dice as _roll_dice_expression
from trpg_server.site_agent_tools.base import ToolContext, ToolResult, ToolSpec, schema

_DICE_PATTERN = re.compile(r"^\d{1,3}d\d{1,4}$", re.I)


def roll_dice(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    expression = str(arguments.get("expression") or "1d100").strip().lower()
    if not _DICE_PATTERN.match(expression):
        return ToolResult.failure("骰子表达式必须形如 1d6 或 3d100")
    result = _roll_dice_expression({"expression": expression})
    if isinstance(result, dict) and result.get("error"):
        return ToolResult.failure(str(result["error"]))
    payload = {
        "expression": result.get("expression"),
        "rolls": result.get("rolls"),
        "total": result.get("total"),
    }
    return ToolResult.success(payload, summary=str(result.get("summary") or ""))


def random_choice(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return ToolResult.failure("items 必须是非空数组")
    items = [str(item)[:120] for item in raw_items[:50]]
    count = max(1, min(int(arguments.get("count") or 1), len(items)))
    picks = random.sample(items, count)
    return ToolResult.success(
        {"picks": picks, "count": count, "total": len(items)},
        summary="随机选择：" + "、".join(picks),
    )


def date_math(actor: Any, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    base_text = str(arguments.get("base_date") or "").strip()
    try:
        base = datetime.strptime(base_text, "%Y-%m-%d").date() if base_text else date.today()
    except ValueError:
        return ToolResult.failure("base_date 必须使用 YYYY-MM-DD 格式")
    days = int(arguments.get("days") or 0)
    weeks = int(arguments.get("weeks") or 0)
    target = base + timedelta(days=days + weeks * 7)
    weekdays = "一二三四五六日"
    return ToolResult.success(
        {
            "base_date": base.isoformat(),
            "date": target.isoformat(),
            "weekday": f"星期{weekdays[target.weekday()]}",
            "delta_days": (target - base).days,
        },
        summary=f"{base.isoformat()} 加 {days + weeks * 7} 天是 {target.isoformat()}",
    )


UTILITY_TOOLS = [
    ToolSpec(
        name="utilities.dice_roll",
        description="投掷骰子，表达式形如 1d100、3d6；仅返回文本结果。",
        parameters=schema({"expression": {"type": "string", "maxLength": 20}}, ["expression"]),
        risk="read",
        handler=roll_dice,
    ),
    ToolSpec(
        name="utilities.random_choice",
        description="从给定选项中随机抽取若干项（不放回）。",
        parameters=schema(
            {
                "items": {"type": "array", "maxItems": 50},
                "count": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["items"],
        ),
        risk="read",
        handler=random_choice,
    ),
    ToolSpec(
        name="utilities.date_math",
        description="在指定基准日期上加减天/周，返回日期与星期。",
        parameters=schema(
            {
                "base_date": {"type": "string", "maxLength": 10},
                "days": {"type": "integer", "minimum": -36500, "maximum": 36500},
                "weeks": {"type": "integer", "minimum": -5200, "maximum": 5200},
            }
        ),
        risk="read",
        handler=date_math,
    ),
]
