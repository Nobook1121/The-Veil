"""PyMuPDF 渲染器：封面 / 目录 / 章节目录页码 / 正文 / 页脚（设计稿 §4、§5.4）。

渲染器只消费 :mod:`trpg_server.room_log.model` 产出的文档字典，不读消息原文。
每章单独用 ``Story`` 渲染成 PDF，再依次合并 —— 这是稳定拿到「每章起始页」
的唯一办法（Story 不暴露内容落在第几页）。

视觉层次只用 Story 实测支持的子集：块级元素、table、border、background-color。
禁止 flex / grid / absolute / float / box-shadow / border-radius / transform /
filter / SVG / column-count。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import pymupdf

from trpg_server.settings import DATA_DIR, ROOMS_DIR

from .fonts import build_archive, font_css, resolve_font
from .markdown import escape_html
from .model import build_document, resolve_image_path

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MM = 72.0 / 25.4
DEFAULT_TEMPLATE = "archive"
TOC_ENTRIES_PER_PAGE = 28
FOOTER_FONT_SIZE = 8

_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


class RenderError(RuntimeError):
    """渲染失败（模板缺失、字体缺失、内容为空等可读错误）。"""


def template_root(template_id: str | None = None) -> Path:
    return TEMPLATES_DIR / (template_id or DEFAULT_TEMPLATE)


def load_template(template_id: str | None = None) -> dict[str, Any]:
    """读取版式目录的 template.json，并校验必需文件齐全。"""
    root = template_root(template_id)
    meta_path = root / "template.json"
    if not meta_path.is_file():
        raise RenderError(f"template not found: {template_id}")
    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("id", root.name)
    for required in ("print.css", "chapter.html"):
        if not (root / required).is_file():
            raise RenderError(f"template {root.name} missing {required}")
    return meta


def render_pdf(
    document: Mapping[str, Any],
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    template: str | None = None,
    room_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """把团录文档渲染为 PDF，返回 ``{path, pages, bytes, chapters}``。"""
    if not document.get("chapters"):
        raise RenderError("log.empty")
    template_id = str(template or (document.get("options") or {}).get("template") or DEFAULT_TEMPLATE)
    meta = load_template(template_id)
    root = template_root(template_id)
    css = font_css(font_path or resolve_font()[1]) + "\n" + (root / "print.css").read_text(encoding="utf-8")
    resolved_font_path = str(font_path) if font_path else resolve_font()[1]

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="roomlog-render-"))
    _report(progress, 5)
    try:
        build_dir = temp_root / "assets"
        (build_dir / "images").mkdir(parents=True, exist_ok=True)
        archive = build_archive(resolved_font_path, extra_dirs=[build_dir])
        image_map = _copy_images(document, build_dir, room_dir=room_dir, data_dir=data_dir)

        cover_pages = _render_cover(meta, root, document, css, archive, temp_root)
        _report(progress, 15)

        chapter_files: list[Path] = []
        chapter_pages: list[int] = []
        total = max(1, len(document["chapters"]))
        for position, chapter in enumerate(document["chapters"], start=1):
            html = _chapter_html(root, document, chapter, image_map, position)
            path = temp_root / f"chapter-{position:03d}.pdf"
            theme = meta.get("options") or {}
            chapter_css = css + _chapter_extra_css(theme)
            _render_story(html, chapter_css, archive, path)
            chapter_files.append(path)
            chapter_pages.append(_page_count(path))
            _report(progress, 15 + int(60 * position / total))

        toc_pages = 1 if meta.get("toc", True) else 0
        start_pages: list[int] = []
        toc_path: Path | None = None
        if toc_pages:
            for _attempt in range(3):
                start_pages = _chapter_start_pages(cover_pages, toc_pages, chapter_pages)
                toc_path = temp_root / "toc.pdf"
                _render_story(
                    _toc_html(document, start_pages),
                    css + _toc_extra_css(meta.get("options") or {}),
                    archive,
                    toc_path,
                )
                actual = _page_count(toc_path)
                if actual == toc_pages:
                    break
                toc_pages = actual
        else:
            start_pages = _chapter_start_pages(cover_pages, 0, chapter_pages)

        _report(progress, 82)
        final_doc = pymupdf.open()
        cover_file = temp_root / "cover.pdf"
        if meta.get("cover", True) and cover_file.is_file():
            final_doc.insert_pdf(_open_quiet(cover_file))
        if toc_path is not None:
            final_doc.insert_pdf(_open_quiet(toc_path))
        for path in chapter_files:
            final_doc.insert_pdf(_open_quiet(path))

        _write_footer(final_doc, resolved_font_path, skip_pages=cover_pages)
        final_doc.set_metadata(
            {
                "title": f"{document['room'].get('name') or '房间'} · 团录",
                "author": str(document["room"].get("creator_name") or ""),
                "subject": str(document["room"].get("scenario_title") or "团录"),
                "keywords": "TRPG, 团录, room log",
                "creator": "The Veil",
                "producer": "PyMuPDF",
            }
        )
        _report(progress, 92)
        try:
            final_doc.subset_fonts()
        except Exception:  # noqa: BLE001 - 子集化失败只影响体积，不阻断导出
            logger.warning("room_log subset_fonts failed", exc_info=True)

        temp_output = output.with_name(f"{output.name}.{uuid.uuid4().hex[:8]}.tmp")
        final_doc.save(str(temp_output), garbage=4, deflate=True, clean=True)
        page_count = final_doc.page_count
        final_doc.close()
        os.replace(temp_output, output)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    _report(progress, 100)
    return {
        "path": str(output),
        "pages": page_count,
        "bytes": output.stat().st_size,
        "chapters": len(document["chapters"]),
    }


def export_room(
    room_dir: str | Path,
    output_path: str | Path,
    options: Mapping[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
    font_path: str | Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """一站式导出：归约 + 渲染。"""
    document = build_document(room_dir, options, data_dir=data_dir)
    return render_pdf(
        document,
        output_path,
        font_path=font_path,
        template=(options or {}).get("template") or document["options"].get("template"),
        room_dir=room_dir,
        data_dir=data_dir,
        progress=progress,
    )


# ---------------------------------------------------------------- 章节与条目 HTML


def _render_story(html: str, css: str, archive, path: Path) -> None:
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    writer = pymupdf.DocumentWriter(str(path))
    page = pymupdf.paper_rect("a4")
    where = page + (16 * MM, 18 * MM, -16 * MM, -18 * MM)
    more = 1
    while more:
        device = writer.begin_page(page)
        more, _filled = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()


def _render_cover(meta, root: Path, document, css: str, archive, temp_root: Path) -> int:
    """封面：先渲染透明文字层，再叠到填充主题色的页面上。"""
    cover_file = temp_root / "cover.pdf"
    if not meta.get("cover", True):
        return 0
    template_path = root / "cover.html"
    if not template_path.is_file():
        return 0
    values = _cover_values(document)
    html = _fill(template_path.read_text(encoding="utf-8"), values)
    overlay = temp_root / "cover-overlay.pdf"
    _render_story(html, css, archive, overlay)
    theme = meta.get("options") or {}
    fill = _hex_to_rgb(str(theme.get("theme_color") or "#0d1f1a"))
    pages: int
    page_rect = pymupdf.paper_rect("a4")
    out = pymupdf.open()
    source = pymupdf.open(str(overlay))
    for index in range(source.page_count):
        page = out.new_page(width=page_rect.width, height=page_rect.height)
        page.draw_rect(page.rect, color=None, fill=fill)
        page.show_pdf_page(page.rect, source, index, overlay=True)
    source.close()
    out.save(str(cover_file))
    pages = out.page_count
    out.close()
    return pages


def _cover_values(document: Mapping[str, Any]) -> dict[str, str]:
    room = document.get("room") or {}
    stats = document.get("stats") or {}
    chapters = document.get("chapters") or []
    first = _date_part(chapters[0].get("started_at")) if chapters else ""
    last = _date_part(chapters[-1].get("started_at")) if chapters else ""
    date_range = f"{first} — {last}" if first and last and first != last else (first or "—")
    stats_line = (
        f"本记录共 {stats.get('chapters', 0)} 章 / {stats.get('messages_exported', 0)} 条"
    )
    if stats.get("messages_truncated"):
        stats_line += f"（已截断 {stats['messages_truncated']} 条）"
    return {
        "room_name": escape_html(str(room.get("name") or "未命名房间")),
        "scenario_title": escape_html(str(room.get("scenario_title") or "")),
        "date_range": escape_html(date_range),
        "stats_line": escape_html(stats_line),
        "creator_name": escape_html(str(room.get("creator_name") or "")),
    }


def _chapter_html(
    root: Path,
    document: Mapping[str, Any],
    chapter: Mapping[str, Any],
    image_map: Mapping[tuple[int, int], str],
    position: int,
) -> str:
    body_parts: list[str] = []
    for entry_index, entry in enumerate(chapter.get("entries") or []):
        body_parts.append(_entry_html(entry, image_map.get((position - 1, entry_index))))
    body = "\n".join(part for part in body_parts if part)
    values = {
        "chapter_index": f"{position:02d}",
        "chapter_title": escape_html(str(chapter.get("title") or "")),
        "chapter_subtitle": escape_html(str(chapter.get("subtitle") or "")),
        "chapter_body": body,
        "room_name": escape_html(str((document.get("room") or {}).get("name") or "")),
    }
    template = (root / "chapter.html").read_text(encoding="utf-8")
    return _wrap_document(_fill(template, values))


def _entry_html(entry: Mapping[str, Any], image_name: str | None) -> str:
    kind = str(entry.get("kind") or "system")
    html = str(entry.get("html") or "")
    time_text = escape_html(str(entry.get("time") or ""))
    speaker = escape_html(str(entry.get("speaker") or ""))
    if kind == "narration":
        return f'<div class="narr">{html}</div>'
    if kind == "dialogue":
        return f'<div class="dial">{html}</div>'
    if kind == "speaker":
        character = escape_html(str(entry.get("character") or ""))
        who = f"<strong>{speaker}</strong>"
        if character:
            who += f'<span class="character"> · {character}</span>'
        return (
            '<table class="speaker" width="100%"><tr>'
            f'<td class="who">{who}</td><td class="when">{time_text}</td>'
            f'</tr></table><div class="speech">{html}</div>'
        )
    if kind == "dice":
        roll = entry.get("roll") or {}
        roll_text = _roll_text(roll)
        judgement = escape_html(str(roll.get("judgement") or ""))
        judge_cell = f'<tr><td colspan="2" class="dice-judge">{judgement}</td></tr>' if judgement else ""
        return (
            '<table class="dice" width="100%"><tr>'
            f'<td class="dice-left">{speaker} · {time_text}</td>'
            f'<td class="dice-right">{roll_text}</td></tr>{judge_cell}</table>'
        )
    if kind == "image":
        if not image_name:
            caption = escape_html(str(entry.get("caption") or ""))
            return f'<p class="system">[图片缺失] {caption}</p>'
        caption = escape_html(str(entry.get("caption") or ""))
        caption_row = f'<tr><td class="figure-caption">{caption}</td></tr>' if caption else ""
        return (
            '<table class="figure" width="100%"><tr><td class="figure-cell">'
            f'<img src="{escape_html(image_name)}" width="420"/></td></tr>{caption_row}</table>'
        )
    return f'<p class="system">{html}</p>'


def _roll_text(roll: Mapping[str, Any]) -> str:
    expr = escape_html(str(roll.get("expr") or ""))
    total = roll.get("total")
    label = escape_html(str(roll.get("label") or ""))
    player = escape_html(str(roll.get("player") or ""))
    prefix = " · ".join(part for part in (label, player) if part)
    core = f"{expr} = {total}" if total is not None else expr
    return f"{prefix}　{core}" if prefix else core


def _toc_html(document: Mapping[str, Any], start_pages: list[int]) -> str:
    rows = []
    for chapter, page in zip(document.get("chapters") or [], start_pages):
        title = escape_html(str(chapter.get("title") or ""))
        subtitle = escape_html(str(chapter.get("subtitle") or ""))
        name = f"{title}　{subtitle}" if subtitle else title
        rows.append(
            '<tr>'
            f'<td class="toc-no">{int(chapter.get("index") or 0):02d}</td>'
            f'<td class="toc-name">{name}</td>'
            f'<td class="toc-page">{page}</td>'
            "</tr>"
        )
    body = "".join(rows) or '<tr><td class="toc-name">（暂无章节）</td></tr>'
    return _wrap_document(
        '<h1 class="toc-title">目录</h1>'
        f'<table class="toc" width="100%">{body}</table>'
    )


def _wrap_document(body: str) -> str:
    return f"<html><body>{body}</body></html>"


def _fill(template: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return str(values.get(match.group(1), ""))

    return _PLACEHOLDER_RE.sub(replace, template)


def _chapter_extra_css(theme: Mapping[str, Any]) -> str:
    accent = str(theme.get("accent") or "#b9a06a")
    return f"\n.chapter-rule {{ border-top-color: {accent}; }}" f"\nh1.chapter-title {{ color: #0d1f1a; }}" f"\ndiv.narr {{ border-left-color: {accent}; }}"


def _toc_extra_css(theme: Mapping[str, Any]) -> str:
    theme_color = str(theme.get("theme_color") or "#0d1f1a")
    return f"\nh1.toc-title {{ color: {theme_color}; }}"


# ---------------------------------------------------------------- 图片与页码


def _copy_images(
    document: Mapping[str, Any],
    build_dir: Path,
    *,
    room_dir: str | Path | None,
    data_dir: str | Path | None,
) -> dict[tuple[int, int], str]:
    mapping: dict[tuple[int, int], str] = {}
    counter = 0
    for chapter_index, chapter in enumerate(document.get("chapters") or []):
        for entry_index, entry in enumerate(chapter.get("entries") or []):
            if entry.get("kind") != "image":
                continue
            source = resolve_image_path(
                str(entry.get("url") or ""), room_dir=room_dir, data_dir=data_dir
            )
            if source is None:
                continue
            counter += 1
            suffix = source.suffix.lower() or ".png"
            name = f"images/img-{counter:04d}{suffix}"
            target = build_dir / name
            try:
                shutil.copyfile(source, target)
            except OSError:
                logger.warning("room_log image copy failed url=%s", entry.get("url"), exc_info=True)
                continue
            mapping[(chapter_index, entry_index)] = name
    return mapping


def _chapter_start_pages(cover_pages: int, toc_pages: int, chapter_pages: list[int]) -> list[int]:
    starts: list[int] = []
    page = cover_pages + toc_pages + 1
    for count in chapter_pages:
        starts.append(page)
        page += count
    return starts


def _write_footer(doc, font_path: str, *, skip_pages: int) -> None:
    total = doc.page_count
    font = pymupdf.Font(fontfile=font_path)
    for index in range(total):
        if index < skip_pages:
            continue
        page = doc[index]
        text = f"第 {index + 1} 页 / 共 {total} 页"
        width = font.text_length(text, fontsize=FOOTER_FONT_SIZE)
        x = page.rect.width - 16 * MM - width
        y = page.rect.height - 10 * MM
        page.insert_font(fontname="pgnum", fontfile=font_path)
        page.insert_text((x, y), text, fontname="pgnum", fontsize=FOOTER_FONT_SIZE, color=(0.42, 0.42, 0.42))


def _page_count(path: Path) -> int:
    doc = pymupdf.open(str(path))
    try:
        return doc.page_count
    finally:
        doc.close()


def _open_quiet(path: Path):
    return pymupdf.open(str(path))


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    try:
        number = int(raw, 16)
    except ValueError:
        return (0.05, 0.12, 0.10)
    return ((number >> 16) & 255) / 255.0, ((number >> 8) & 255) / 255.0, (number & 255) / 255.0


def _date_part(value: Any) -> str:
    return str(value or "")[:10]


def _report(progress: Callable[[int], None] | None, value: int) -> None:
    if progress is None:
        return
    try:
        progress(max(0, min(100, int(value))))
    except Exception:  # noqa: BLE001 - 进度回调失败不影响导出
        logger.debug("room_log progress callback failed", exc_info=True)


# ---------------------------------------------------------------- CLI（P1 验收入口）


def _locate_room(room_id: str, rooms_dir: str | Path | None = None) -> Path:
    root = Path(rooms_dir) if rooms_dir else ROOMS_DIR
    direct = root / str(room_id)
    if (direct / "info.json").is_file():
        return direct
    import json

    for candidate in sorted(root.iterdir()) if root.is_dir() else []:
        info_file = candidate / "info.json"
        if not info_file.is_file():
            continue
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(info.get("id")) == str(room_id):
            return candidate
    raise SystemExit(f"room not found: {room_id}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="导出房间团录 PDF")
    parser.add_argument("room_id")
    parser.add_argument("output")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--include-ooc", action="store_true", default=True)
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args(argv)

    room_dir = _locate_room(args.room_id)
    options = {
        "template": args.template,
        "include_images": not args.no_images,
    }
    document = build_document(room_dir, options, data_dir=DATA_DIR)
    result = render_pdf(
        document,
        args.output,
        template=args.template,
        room_dir=room_dir,
        data_dir=DATA_DIR,
    )
    print(json.dumps({"room": document["room"], "stats": document["stats"], "file": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TEMPLATE",
    "RenderError",
    "export_room",
    "load_template",
    "main",
    "render_pdf",
    "template_root",
]
