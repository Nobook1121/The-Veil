"""输出侧提示门控：房规未开启「行动建议」时，清掉模型自行写进正文的提示与选项列表。

提示功能有三层门控，缺一层就会出现「明明关了却还在提示」的回归：

1. 工具层：``room.suggest_actions`` 在关闭时从工具清单里摘除，工具内部也会拒绝调用；
2. 输入层：系统提示每轮重新注入常驻约束（禁止主动提示、禁止「你要做什么：1…2…」式列表）；
3. 输出层（本模块）：模型仍可能无视提示词，直接在叙事正文里写
   「**你们要做什么？**」+ Markdown 选项列表。提示词只能降低概率、不能保证，
   所以这里做最后一道确定性兜底——关闭时把这段引导问句与选项列表删掉。

只处理「引导问句 + 紧跟其后的选项列表」这种固定形态，普通叙事与 NPC 台词（引号内）
不动，避免误删正常内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["HintReport", "strip_action_hints"]


@dataclass(frozen=True)
class HintReport:
    """清洗结果：``text`` 是清洗后的正文，计数用于写房间日志。"""

    text: str
    prompts: int = 0
    options: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.prompts or self.options)


# 引导问句：只认「问玩家下一步做什么」的固定说法，避免把剧情里的普通疑问句一起删掉。
_HINT_QUESTIONS = (
    "你们要做什么",
    "你要做什么",
    "你们要干什么",
    "你要干什么",
    "你们打算做什么",
    "你打算做什么",
    "你们打算怎么做",
    "你打算怎么做",
    "你们准备做什么",
    "你准备做什么",
    "你们准备怎么做",
    "你准备怎么做",
    "你们想做什么",
    "你想做什么",
    "你们想怎么做",
    "你想怎么做",
    "你们要怎么做",
    "你要怎么做",
    "你们要如何行动",
    "你要如何行动",
    "你们下一步要做什么",
    "你下一步要做什么",
    "接下来你们要做什么",
    "接下来你要做什么",
    "你们接下来要做什么",
    "你接下来要做什么",
    "你们的选择是",
    "你的选择是",
    "请选择你们的行动",
    "请选择你的行动",
    "请从下方选择你们的行动",
    "请从下方选择你的行动",
    "请从以上选项中选择",
)

# 选项块小标题：整行只有这类文字（可带「：」与强调符号）时，视为选项列表的一部分。
_OPTION_HEADERS = (
    "可选行动",
    "可选的行动",
    "可选操作",
    "可选项",
    "选项如下",
    "行动选项",
    "行动建议",
    "可能的行动",
    "你可以选择",
    "你们可以选择",
    "你可以做",
    "你们可以做",
    "以下是你可能的行动",
    "以下是你们可能的行动",
)

# 列表项：``- xxx`` / ``* xxx`` / ``1. xxx`` / ``1、xxx`` / ``（1）xxx`` / ``① xxx``。
# 排除 ``**加粗**`` 行（``*`` 后面紧跟 ``*``），否则会把加粗的叙事段落当成列表项。
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:\*(?!\*)|[-+•·]|\d{1,2}[.、)．]|[（(]\d{1,2}[)）]|[①-⑳])[\s\u3000]*\S"
)

_EMPHASIS_RE = re.compile(r"[*_`~]+")

# 引号开符：问句紧跟在这些符号后面时说明是台词，不能动。
_QUOTE_OPENERS = "「『“‘\"'（(《【["

_QUESTION_TAIL = r"[*_`\s]*[？?。.!！…~～]*[*_`\s]*$"


def _flexible(phrase: str) -> str:
    """把固定短语编译成允许中间夹杂 Markdown 强调符/空白的正则。"""
    return r"[*_`\s]*".join(re.escape(char) for char in phrase)


# 长短语优先，保证「你们要做什么」不会被「你要做什么」抢先匹配掉一部分。
_HINT_RE = re.compile(
    "(?:"
    + "|".join(_flexible(item) for item in sorted(_HINT_QUESTIONS, key=len, reverse=True))
    + ")"
    + _QUESTION_TAIL
)


def _plain(line: str) -> str:
    return _EMPHASIS_RE.sub("", line).strip()


def _is_option_header(line: str) -> bool:
    text = _plain(line).rstrip("：: 　")
    return bool(text) and text in _OPTION_HEADERS


def _has_text(line: str) -> bool:
    return bool(_plain(line))


def _hint_kind(line: str) -> tuple[str, str] | None:
    """判断这一行是不是引导行。

    返回 ``(kind, prefix)``：``kind`` 为 ``"prompt"``（引导问句）或 ``"header"``
    （选项块小标题）；``prefix`` 是需要保留的行内前缀（整行都是引导内容时为空串）。
    不是引导行返回 ``None``。
    """
    if _is_option_header(line):
        return ("header", "")
    match = _HINT_RE.search(line)
    if not match:
        return None
    prefix = line[: match.start()].rstrip()
    if prefix and prefix[-1] in _QUOTE_OPENERS:
        # 「他说：「你们要做什么？」」——台词，不动。
        return None
    return ("prompt", prefix if _has_text(prefix) else "")


def _collect_block(lines: list[str], start: int) -> tuple[int, int]:
    """从 ``start``（引导行的下一行）开始收集选项块。

    返回 ``(end, items)``：``end`` 是块结束后的行下标（不含），``items`` 是列表项数量。
    块内允许出现空行与「可选行动：」这类小标题。
    """
    total = len(lines)
    index = start
    items = 0
    end = start
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _is_option_header(line):
            index += 1
            continue
        if _LIST_ITEM_RE.match(line):
            items += 1
            index += 1
            end = index
            continue
        break
    return (end if items else start), items


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def strip_action_hints(text: str) -> HintReport:
    """删除正文里「引导问句 + 选项列表」段落，保留其余叙事。

    没有可删内容时原样返回，计数为 0。清洗后为空说明整段回复都是提示，
    这时不返回空串（调用方会当成平台无响应），保留原文交给人工判断。
    """
    if not isinstance(text, str) or not text.strip():
        return HintReport(text=text or "", prompts=0, options=0)

    lines = text.split("\n")
    kept: list[str] = []
    prompts = 0
    options = 0
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        hint = _hint_kind(line)
        if hint is None:
            kept.append(line)
            index += 1
            continue
        kind, prefix = hint
        end, items = _collect_block(lines, index + 1)
        if kind == "header" and not items:
            # 只有小标题、后面没有列表：不属于选项块，保持原样。
            kept.append(line)
            index += 1
            continue
        if prefix:
            kept.append(prefix)
        if kind == "prompt":
            prompts += 1
        if items:
            options += items
        index = end if items else index + 1

    if not prompts and not options:
        return HintReport(text=text, prompts=0, options=0)

    cleaned = _collapse_blank_lines("\n".join(kept)).strip()
    if not _has_text(cleaned):
        # 整条回复都是提示：删空会让玩家看到「平台无响应」，保留原文。
        return HintReport(text=text, prompts=0, options=0)
    return HintReport(text=cleaned, prompts=prompts, options=options)
