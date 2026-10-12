"""提示词 token 成本归因。

上游 usage 只给出一个总的 ``prompt_tokens``，看不出输入 token 到底花在静态前缀、
知识库检索还是历史对话上。这里用轻量估算把一次请求的提示词按「分段」拆开，让
管理员在仪表盘与控制台里看到成本结构，从而判断该优化哪一块。

估算只用于归因，不是计费口径：真实 token 以上游返回的 usage 为准，分段比例用于
横向比较各部分谁更「贵」。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# 每条约 4 token 的消息封装开销（role、分隔符、协议字段等），把不可见开销也计入分段。
MESSAGE_OVERHEAD_TOKENS = 4

# 分段键 → 中文标签，供控制台与前端展示；顺序即展示顺序。
SEGMENT_LABELS: dict[str, str] = {
    "rules": "系统规则",
    "scenario_static": "剧本静态核心",
    "scene_static": "场景静态卡",
    "knowledge": "知识库检索",
    "room_state": "房间动态状态",
    "history": "历史对话",
    "user_input": "本轮输入",
    "unknown": "未分类",
}


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F  # CJK 标点
        or 0x3040 <= code <= 0x30FF  # 假名
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 统一表意
        or 0xAC00 <= code <= 0xD7AF  # 谚文
        or 0xF900 <= code <= 0xFAFF  # 兼容表意
        or 0xFF00 <= code <= 0xFFEF  # 全角
    )


def estimate_tokens(text: Any) -> int:
    """粗略估算文本 token 数：CJK 约 1 token/字，其余字符约 1 token/4 字符。"""
    content = str(text or "")
    if not content:
        return 0
    cjk = 0
    other = 0
    for char in content:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    # 纯 CJK 时 other 为 0，向上取整会得到 1，必须单独判断避免多加一个 token。
    other_tokens = (other + 3) // 4 if other else 0
    return cjk + other_tokens


def attribute_prompt_segments(
    messages: Iterable[Mapping[str, Any]],
    segments: Iterable[str] | None = None,
) -> dict[str, int]:
    """按分段统计提示词各部分的估算 token 数。

    ``segments`` 与 ``messages`` 一一对应；缺省或越界的条目归入 ``unknown``。
    """
    segment_list = list(segments) if segments is not None else []
    totals: dict[str, int] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        name = "unknown"
        if index < len(segment_list):
            name = str(segment_list[index] or "unknown")
        tokens = estimate_tokens(message.get("content")) + MESSAGE_OVERHEAD_TOKENS
        totals[name] = totals.get(name, 0) + tokens
    return totals


def merge_segment_totals(target: dict[str, int], source: Mapping[str, Any] | None) -> None:
    """把一次请求的分段统计累加进汇总字典（供 telemetry 聚合复用）。"""
    if not isinstance(source, Mapping):
        return
    for key, value in source.items():
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount:
            target[str(key)] = target.get(str(key), 0) + amount


def format_segment_summary(totals: Mapping[str, Any] | None, limit: int | None = None) -> str:
    """把分段 token 汇总渲染成一行紧凑文本，供控制台打印。"""
    if not isinstance(totals, Mapping) or not totals:
        return "（无分段数据）"
    ordered = sorted(totals.items(), key=lambda item: int(item[1] or 0), reverse=True)
    if limit is not None:
        ordered = ordered[:limit]
    return "，".join(
        f"{SEGMENT_LABELS.get(str(key), str(key))}={int(value or 0)}" for key, value in ordered
    )