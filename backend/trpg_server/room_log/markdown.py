"""Markdown 子集 → HTML（设计稿附录 C），并做一次标签白名单过滤。

聊天内容里的 Markdown 只覆盖标题、粗体、斜体、分隔线、引用、列表、行内代码。
实现顺序刻意是「先转义、再做替换」：任何用户输入的 ``<`` / ``>`` / ``&`` 在
进入标签生成前就已经变成实体，因此不会出现 HTML 注入；最后再用白名单过滤
一次，作为纵深防御。
"""
from __future__ import annotations

import html
import re

ALLOWED_TAGS = frozenset(
    {"p", "strong", "em", "h1", "h2", "h3", "ul", "ol", "li", "blockquote", "hr", "code", "br", "a"}
)
VOID_TAGS = frozenset({"hr", "br"})

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)(?:\s[^<>]*)?/?>")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)")


def escape_html(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def to_html(text: str) -> str:
    """把 Markdown 子集渲染成一段安全的 HTML（可能是多个块级元素）。"""
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    list_tag = ""
    list_items: list[str] = []
    quote_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(''.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_tag and list_items:
            items = "".join(f"<li>{_inline(item)}</li>" for item in list_items)
            blocks.append(f"<{list_tag}>{items}</{list_tag}>")
        list_tag = ""
        list_items.clear()

    def flush_quote() -> None:
        if quote_lines:
            inner = "<br/>".join(_inline(escape_html(line)) for line in quote_lines)
            blocks.append(f"<blockquote><p>{inner}</p></blockquote>")
            quote_lines.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            flush_all()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_all()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(escape_html(heading.group(2).strip()))}</h{level}>")
            continue

        if _HR_RE.match(line):
            flush_all()
            blocks.append("<hr/>")
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            flush_paragraph()
            flush_list()
            quote_lines.append(quote.group(1).strip())
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            flush_quote()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(escape_html(bullet.group(1).strip()))
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush_paragraph()
            flush_quote()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(escape_html(ordered.group(1).strip()))
            continue

        flush_list()
        flush_quote()
        paragraph.append(escape_html(line))

    flush_all()
    return sanitize_html("\n".join(blocks))


def _inline(escaped_line: str) -> str:
    """行内标记替换；入参必须是已经转义过的文本。"""
    value = _CODE_RE.sub(r"<code>\1</code>", escaped_line)
    value = _BOLD_RE.sub(r"<strong>\1</strong>", value)
    value = _ITALIC_RE.sub(r"<em>\1</em>", value)
    return value


def sanitize_html(markup: str) -> str:
    """只保留白名单标签并去掉全部属性。"""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if name not in ALLOWED_TAGS:
            return ""
        if match.group(0).startswith("</"):
            return f"</{name}>"
        if name in VOID_TAGS:
            return f"<{name}/>"
        return f"<{name}>"

    return _TAG_RE.sub(replace, str(markup or ""))


__all__ = ["ALLOWED_TAGS", "escape_html", "sanitize_html", "to_html"]
