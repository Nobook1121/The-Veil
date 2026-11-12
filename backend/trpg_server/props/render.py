"""PropSpec → Story HTML → PDF / PNG（设计稿 §4.2 / §5.1）。

只使用 PyMuPDF ``Story`` 实测支持的 HTML/CSS 子集：块级流 + table + img +
``background-image``；禁止 flex / grid / absolute / float / SVG / box-shadow /
transform / column-count。中文靠 ``room_log/fonts.py`` 的探测与归档，字体缺失
直接抛 :class:`FontMissing`，绝不静默产出方块字。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import pymupdf

from trpg_server.room_log.fonts import FONT_FAMILY, FontMissing, build_archive, font_css, resolve_font
from trpg_server.room_log.markdown import escape_html, to_html

from . import registry, store

logger = logging.getLogger(__name__)

PROPS_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
TEMPLATES_DIR = registry.TEMPLATES_DIR
MM = 72.0 / 25.4
DEFAULT_PAGE = "a4"
MAX_PAGES = 20

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_PAPER_FILES = {
    "ivory": "paper-ivory.png",
    "aged": "paper-aged.png",
    "white": "",
}
_DEFAULT_INK = "#2b2b2b"
_DEFAULT_ACCENT = "#7a5c3e"
# (文件名, 最小像素宽, 最大像素宽)
_WEAR_FILES = {
    "stain": ("stain.png", 90, 240),
    "fold": ("fold.png", 140, 320),
    "burn": ("burn.png", 70, 190),
    "tear": ("tear-edge.png", 90, 260),
}


class RenderError(RuntimeError):
    """渲染失败（模板缺失、页面无法落版等可读错误）。"""


# ---------------------------------------------------------------- HTML 填充


def fill_page_html(spec: Mapping[str, Any], template: str | Mapping[str, Any]) -> str:
    """把字段与样式填进模板 ``page.html``；未知占位符替换为空串。"""
    manifest = registry.get_template(template) if isinstance(template, str) else dict(template)
    template_id = str(manifest.get("id") or "")
    root = registry.template_dir(template_id)
    page_path = root / "page.html"
    if not page_path.is_file():
        raise RenderError(f"模板 {template_id} 缺少 page.html")
    raw = page_path.read_text(encoding="utf-8")

    fields = spec.get("fields") if isinstance(spec.get("fields"), Mapping) else {}
    values: dict[str, str] = {}
    for key, value in fields.items():
        text = "" if value is None else str(value)
        values[str(key)] = escape_html(text)
        values[f"{key}_html"] = to_html(text)
        # 收据条目、契约条款这类「一行一条」的字段需要保留原始换行；
        # to_html 会把单个换行并成同一段，这里额外提供 <br/> 版本。
        values[f"{key}_lines"] = "<br/>".join(escape_html(line) for line in text.splitlines())

    style = spec.get("style") if isinstance(spec.get("style"), Mapping) else {}
    paper = str(style.get("paper") or "ivory").strip().lower()
    paper_file = _PAPER_FILES.get(paper, _PAPER_FILES["ivory"])
    values["paper"] = paper_file
    values["paper_style"] = f"background-image:url({paper_file});" if paper_file else ""
    values["ink"] = str(style.get("ink") or _DEFAULT_INK)
    values["accent"] = str(style.get("accent") or _DEFAULT_ACCENT)

    wear = spec.get("wear") if isinstance(spec.get("wear"), Mapping) else {}
    for name, (filename, min_width, max_width) in _WEAR_FILES.items():
        values[f"wear_{name}"] = _wear_html(name, filename, wear.get(name), min_width, max_width)

    # 照片类道具：资产目录已经挂进 Archive，模板只需引用文件名；没有图时
    # 保持空串，模板的表格单元格就是「空白照片框」的降级形态。
    assets = spec.get("assets") if isinstance(spec.get("assets"), Mapping) else {}
    photo_name = Path(str(assets.get("photo") or "")).name
    if photo_name and re.match(r"^[A-Za-z0-9_.-]+$", photo_name):
        values["photo"] = escape_html(photo_name)
        values["photo_img"] = f'<img src="{escape_html(photo_name)}" alt=""/>'
    else:
        values["photo"] = ""
        values["photo_img"] = ""

    def replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), "")

    body = _PLACEHOLDER_RE.sub(replace, raw)
    return f"<html><body>{body}</body></html>"


def _wear_html(name: str, filename: str, raw_strength: Any, min_width: int, max_width: int) -> str:
    try:
        strength = float(raw_strength)
    except (TypeError, ValueError):
        return ""
    strength = max(0.0, min(1.0, strength))
    if strength <= 0.01:
        return ""
    width = int(round(min_width + (max_width - min_width) * strength))
    return (
        f'<div class="wear wear-{name}">'
        f'<img src="{escape_html(filename)}" width="{width}" alt=""/></div>'
    )


# ---------------------------------------------------------------- 渲染


def spec_hash(spec: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(spec or {}), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def render_prop(
    spec: Mapping[str, Any],
    output_path: str | Path,
    *,
    template_id: str | None = None,
    font_path: str | Path | None = None,
    room_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    fmt: str | None = None,
) -> dict[str, Any]:
    """把一件道具渲染成 PDF 或 PNG，返回 ``{path, pages, bytes, template_id, format}``。"""
    manifest = registry.get_template(template_id or str(spec.get("template_id") or ""))
    template_key = str(manifest["id"])
    root = registry.template_dir(template_key)

    output = Path(output_path)
    suffix = str(fmt or output.suffix.lstrip(".") or "pdf").strip().lower()
    if suffix not in {"pdf", "png"}:
        raise RenderError(f"不支持的导出格式：{suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)

    html = fill_page_html(spec, manifest)
    style_path = root / "style.css"
    if not style_path.is_file():
        raise RenderError(f"模板 {template_key} 缺少 style.css")
    family, resolved_font = resolve_font(str(font_path or ""), data_dir=data_dir)
    css = font_css(resolved_font, family) + "\n" + style_path.read_text(encoding="utf-8")

    page_rect, margins = _page_geometry(manifest)
    archive = _build_archive(resolved_font, root, room_dir, spec)

    # 中间产物全部留在内存里：Story 的 DocumentWriter 支持写 BytesIO，PDF 后处理
    # 与 PNG 转换都能直接从字节流读。这样既不需要临时目录，也不需要在 finally
    # 里删文件——删除失败（Windows 句柄未释放、受限环境拦截删除）不会再留残留。
    pages, payload = _finalize_pdf(
        _story_to_pdf_bytes(html, css, archive, page_rect, margins), spec
    )
    if suffix == "pdf":
        output.write_bytes(payload)
    else:
        _pdf_to_png(payload, output)

    return {
        "path": str(output),
        "pages": pages,
        "bytes": output.stat().st_size,
        "template_id": template_key,
        "format": suffix,
    }


def render_cached(
    room_dir: str | Path,
    spec: Mapping[str, Any],
    fmt: str = "pdf",
    *,
    settings: Mapping[str, Any] | None = None,
    font_path: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """命中 ``renders/<id>@<sha1(spec)>.<ext>`` 直接复用，否则渲染后落盘。"""
    prop_id = str(spec.get("id") or "")
    digest = spec_hash(spec)
    target = store.render_path(room_dir, prop_id, digest, fmt)
    if target.is_file():
        return {
            "path": str(target),
            "pages": _page_count(target) if target.suffix.lower() == ".pdf" else 1,
            "bytes": target.stat().st_size,
            "template_id": str(spec.get("template_id") or ""),
            "format": str(fmt).lower(),
            "cached": True,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    result = render_prop(
        spec,
        target,
        font_path=font_path,
        room_dir=room_dir,
        data_dir=data_dir,
        fmt=fmt,
    )
    result["cached"] = False
    if settings is not None:
        store.prune_renders(room_dir, int(settings.get("render_cache_days") or 0))
    return result


def _build_archive(font_path: str, root: Path, room_dir: str | Path | None, spec: Mapping[str, Any]):
    extra: list[Path] = [root]
    template_assets = root / "assets"
    if template_assets.is_dir():
        extra.append(template_assets)
    if PROPS_ASSETS_DIR.is_dir():
        extra.append(PROPS_ASSETS_DIR)
    if room_dir is not None:
        prop_id = str(spec.get("id") or "")
        if store.is_valid_prop_id(prop_id):
            asset_dir = store.assets_dir(room_dir, prop_id)
            if asset_dir.is_dir():
                extra.append(asset_dir)
    return build_archive(font_path, extra_dirs=extra)


def _page_geometry(manifest: Mapping[str, Any]) -> tuple["pymupdf.Rect", tuple[float, float, float, float]]:
    page_meta = manifest.get("page") if isinstance(manifest.get("page"), Mapping) else {}
    size = str(page_meta.get("size") or DEFAULT_PAGE).strip().lower()
    try:
        rect = pymupdf.paper_rect(size)
    except Exception:  # noqa: BLE001 - 未知纸张回落到 a4
        rect = pymupdf.paper_rect(DEFAULT_PAGE)
    raw_margin = page_meta.get("margin")
    if isinstance(raw_margin, (list, tuple)) and len(raw_margin) == 4:
        try:
            top, right, bottom, left = (float(value) * MM for value in raw_margin)
        except (TypeError, ValueError):
            top = right = bottom = left = 14 * MM
    else:
        top = right = bottom = left = 14 * MM
    # 负 margin 不合法（会让 where 反向），统一钳到 0。
    top, right, bottom, left = (max(0.0, value) for value in (top, right, bottom, left))
    return rect, (top, right, bottom, left)


def _story_to_pdf_bytes(html: str, css: str, archive, page_rect, margins) -> bytes:
    top, right, bottom, left = margins
    where = page_rect + (left, top, -right, -bottom)
    if where.is_empty or where.width <= 0 or where.height <= 0:
        raise RenderError("页面边距大于纸张尺寸")
    buffer = io.BytesIO()
    # Windows 上 DocumentWriter 直到对象被回收才真正释放文件句柄；写 BytesIO
    # 没有句柄问题，调用方拿到字节后即可丢弃 PDF 中间态。
    writer = pymupdf.DocumentWriter(buffer)
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    more = 1
    pages = 0
    try:
        while more:
            pages += 1
            if pages > MAX_PAGES:
                raise RenderError(f"内容超过 {MAX_PAGES} 页，疑似模板或字段异常")
            device = writer.begin_page(page_rect)
            more, _filled = story.place(where)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()
    return buffer.getvalue()


def _finalize_pdf(source: bytes, spec: Mapping[str, Any]) -> tuple[int, bytes]:
    """裁掉尾部纯装饰页，铺纸纹与做旧，补元数据 + 字体子集化，返回 (页数, 字节)。"""
    doc = None
    try:
        doc = pymupdf.open(stream=source, filetype="pdf")
        _trim_trailing_decoration(doc)
        _apply_dressing(doc, spec)
        doc.set_metadata(
            {
                "title": "道具",
                "creator": "The Veil",
                "producer": "PyMuPDF",
                "keywords": "TRPG, prop",
            }
        )
        try:
            doc.subset_fonts()
        except Exception:  # noqa: BLE001 - 子集化失败只影响体积
            logger.warning("props subset_fonts failed", exc_info=True)
        return doc.page_count, doc.tobytes(garbage=4, deflate=True, clean=True)
    except Exception:  # noqa: BLE001 - 元数据/子集化失败时保留原始 PDF
        logger.warning("props pdf postprocess failed", exc_info=True)
        fallback = pymupdf.open(stream=source, filetype="pdf")
        try:
            return fallback.page_count, source
        finally:
            fallback.close()
    finally:
        if doc is not None:
            doc.close()


def _trim_trailing_decoration(doc) -> None:
    """删除尾部「只有图、没有文字」的页面。

    Story 会把放不下的做旧纹理（水渍/撕口）整体推到下一页，产生一页只有装饰图
    的空白页。最后一页确实还有正文时不会触发；只有多页文档才会裁剪。
    """
    while doc.page_count > 1 and not doc[-1].get_text().strip():
        previous = doc[-2]
        if not previous.get_text().strip():
            break
        doc.delete_page(doc.page_count - 1)


def _apply_dressing(doc, spec: Mapping[str, Any]) -> None:
    """在渲染结果上铺纸纹与做旧纹理。

    实测当前 PyMuPDF ``Story`` 不绘制 ``background-image``；纸纹改由渲染后
    ``insert_image(overlay=False)`` 垫到内容下方，做旧纹理用带透明通道的 PNG
    叠在页面上（alpha 已内置，不盖住正文）。
    """
    style = spec.get("style") if isinstance(spec.get("style"), Mapping) else {}
    paper = str(style.get("paper") or "ivory").strip().lower()
    paper_file = _PAPER_FILES.get(paper, _PAPER_FILES["ivory"])
    paper_path = PROPS_ASSETS_DIR / paper_file if paper_file else None
    wear = spec.get("wear") if isinstance(spec.get("wear"), Mapping) else {}
    for index, page in enumerate(doc):
        rect = page.rect
        if paper_path is not None and paper_path.is_file():
            page.insert_image(rect, filename=str(paper_path), overlay=False, keep_proportion=False)
        if index:
            continue
        for name, raw_strength in wear.items():
            strength = _strength(raw_strength)
            if strength <= 0.01:
                continue
            texture = _WEAR_FILES.get(str(name))
            if texture is None:
                continue
            wear_rect = _wear_rect(str(name), rect, strength)
            if wear_rect is None:
                continue
            path = PROPS_ASSETS_DIR / texture[0]
            if path.is_file():
                page.insert_image(wear_rect, filename=str(path), overlay=True, keep_proportion=False)


def _strength(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _wear_rect(name: str, rect: "pymupdf.Rect", strength: float) -> "pymupdf.Rect | None":
    width, height = rect.width, rect.height
    base = min(width, height)
    if name == "fold":
        half = width * (0.018 + 0.028 * strength)
        return pymupdf.Rect(width * 0.5 - half, 0, width * 0.5 + half, height) + (rect.x0, rect.y0, rect.x0, rect.y0)
    if name == "stain":
        size = base * (0.26 + 0.24 * strength)
        return pymupdf.Rect(width * 0.48, height * 0.56, width * 0.48 + size, height * 0.56 + size) + (
            rect.x0, rect.y0, rect.x0, rect.y0
        )
    if name == "burn":
        size = base * (0.16 + 0.20 * strength)
        return pymupdf.Rect(width - size, 0, width, size) + (rect.x0, rect.y0, rect.x0, rect.y0)
    if name == "tear":
        span = width * (0.06 + 0.06 * strength)
        return pymupdf.Rect(width - span, 0, width, height) + (rect.x0, rect.y0, rect.x0, rect.y0)
    return None


def _pdf_to_png(pdf_data: bytes, output: Path) -> None:
    doc = pymupdf.open(stream=pdf_data, filetype="pdf")
    try:
        if doc.page_count < 1:
            raise RenderError("渲染结果为空")
        pixmap = doc[0].get_pixmap(dpi=144)
        pixmap.save(str(output))
    finally:
        doc.close()


def _page_count(path: Path) -> int:
    try:
        doc = pymupdf.open(str(path))
    except Exception:  # noqa: BLE001 - 缓存文件损坏时交给调用方重新渲染
        return 0
    try:
        return doc.page_count
    finally:
        doc.close()


# ---------------------------------------------------------------- CLI（P0 验收入口）


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="把 PropSpec 渲染成 PDF/PNG")
    parser.add_argument("spec")
    parser.add_argument("output")
    parser.add_argument("--format", choices=("pdf", "png"), default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--font", default=None)
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    try:
        result = render_prop(
            spec,
            args.output,
            template_id=args.template,
            font_path=args.font,
            fmt=args.format,
        )
    except FontMissing as error:
        print(json.dumps({"error": "prop.font_missing", "detail": str(error)}, ensure_ascii=False))
        return 3
    except RenderError as error:
        print(json.dumps({"error": "prop.render_failed", "detail": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGES",
    "PROPS_ASSETS_DIR",
    "RenderError",
    "TEMPLATES_DIR",
    "fill_page_html",
    "main",
    "render_cached",
    "render_prop",
    "spec_hash",
]
