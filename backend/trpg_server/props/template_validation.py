"""用户模板的安全校验（设计稿 §4.2 子集 + 资源限制）。

只允许文档流标签，禁止脚本、事件属性、`javascript:` 与外链 `url(http...)`；
资源只收 png/jpg，并有单文件/总量上限。任何一项失败只抛
:class:`TemplateError` 的一个具体原因，由 registry 把该模板标记为不可用，
不影响其他模板。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

from trpg_server.security import safe_join

from .registry import TemplateError

ALLOWED_TAGS = frozenset(
    {"div", "p", "span", "h1", "h2", "h3", "h4", "table", "tr", "td", "ul", "ol", "li", "hr", "img", "strong", "em"}
)
ALLOWED_ATTRS = frozenset({"class", "style", "src", "width", "height", "colspan", "rowspan", "align", "alt"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
ALLOWED_PAGE_SIZES = frozenset({"a3", "a4", "a5", "a6", "letter", "legal"})
WEAR_NAMES = frozenset({"stain", "fold", "burn", "tear"})
MAX_HTML_BYTES = 512 * 1024
MAX_CSS_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_ASSETS_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ASSET_COUNT = 24
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.IGNORECASE)
_DANGEROUS_CSS_RE = re.compile(r"(@import|javascript:|expression\s*\(|-moz-binding)", re.IGNORECASE)
_SAFE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")


def _fail(message: str) -> None:
    raise TemplateError(message)


def _read_text(path: Path, limit: int, label: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        _fail(f"{label} 无法读取")
        raise TemplateError(label) from exc  # pragma: no cover
    if size > limit:
        _fail(f"{label} 超过大小上限（{limit // 1024} KB）")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"{label} 无法读取")
        raise TemplateError(label) from exc  # pragma: no cover


def _check_css_urls(text: str, label: str) -> None:
    if _DANGEROUS_CSS_RE.search(text):
        _fail(f"{label} 包含禁止的 CSS 写法（@import/javascript:/expression）")
    for match in _URL_RE.finditer(text):
        target = match.group(1).strip().strip("'\"")
        if not _SAFE_FILENAME_RE.match(target) or not target.lower().endswith(tuple(IMAGE_SUFFIXES)):
            _fail(f"{label} 的 url() 只允许引用 assets 内的 png/jpg 文件名")


class _HtmlAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.error = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._audit(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._audit(tag, attrs)

    def _audit(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.error:
            return
        name = str(tag or "").lower()
        if name in {"script", "iframe"}:
            self.error = f"page.html 禁止使用 <{name}>"
            return
        if name not in ALLOWED_TAGS:
            self.error = f"page.html 包含不允许的标签 <{name}>"
            return
        for raw_key, raw_value in attrs:
            key = str(raw_key or "").lower()
            value = "" if raw_value is None else str(raw_value)
            if key.startswith("on"):
                self.error = f"page.html 禁止事件属性 {key}"
                return
            if key not in ALLOWED_ATTRS:
                self.error = f"page.html 包含不允许的属性 {key}"
                return
            lowered = value.lower()
            if "javascript:" in lowered:
                self.error = f"page.html 的属性 {key} 包含 javascript:"
                return
            if key == "src":
                if not _SAFE_FILENAME_RE.match(value) or not value.lower().endswith(tuple(IMAGE_SUFFIXES)):
                    self.error = "img src 只允许 assets 内的 png/jpg 文件名"
                    return
            if key == "style":
                if _DANGEROUS_CSS_RE.search(value):
                    self.error = "style 属性包含禁止的写法"
                    return
                for match in _URL_RE.finditer(value):
                    target = match.group(1).strip().strip("'\"")
                    if not _SAFE_FILENAME_RE.match(target) or not target.lower().endswith(tuple(IMAGE_SUFFIXES)):
                        self.error = "style 中的 url() 只允许 assets 内的 png/jpg 文件名"
                        return


def _validate_manifest_extras(manifest: Mapping[str, Any]) -> None:
    template_id = str(manifest.get("id") or "")
    styles = manifest.get("styles")
    if styles is not None:
        if not isinstance(styles, Mapping):
            _fail(f"模板 {template_id} 的 styles 必须是对象")
        for key in ("paper", "ink", "accent", "font"):
            values = styles.get(key)
            if values is None:
                continue
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                _fail(f"模板 {template_id} 的 styles.{key} 必须是数组")
            for value in values:
                text = str(value or "").strip()
                if key in {"ink", "accent"}:
                    if not _SAFE_COLOR_RE.match(text):
                        _fail(f"模板 {template_id} 的 styles.{key} 只允许 #hex 颜色")
                elif not _SAFE_NAME_RE.match(text):
                    _fail(f"模板 {template_id} 的 styles.{key} 包含不安全字符")

    wear = manifest.get("wear_supported")
    if wear is not None:
        if not isinstance(wear, list) or any(str(item) not in WEAR_NAMES for item in wear):
            _fail(f"模板 {template_id} 的 wear_supported 只能包含 stain/fold/burn/tear")

    page = manifest.get("page")
    if page is not None:
        if not isinstance(page, Mapping):
            _fail(f"模板 {template_id} 的 page 必须是对象")
        size = str(page.get("size") or "a4").lower()
        if size not in ALLOWED_PAGE_SIZES:
            _fail(f"模板 {template_id} 的 page.size 不支持：{size}")
        margin = page.get("margin")
        if margin is not None:
            if not isinstance(margin, list) or not (1 <= len(margin) <= 4):
                _fail(f"模板 {template_id} 的 page.margin 必须是 1–4 个数字")
            for value in margin:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    _fail(f"模板 {template_id} 的 page.margin 必须是数字")
                    return
                if not 0 <= number <= 100:
                    _fail(f"模板 {template_id} 的 page.margin 超出 0–100")

    for field in manifest.get("fields") or []:
        key = str(field.get("key") or "")
        if not _FIELD_KEY_RE.match(key):
            _fail(f"模板 {template_id} 的字段 key 不合法：{key}")
        field_type = str(field.get("type") or "text")
        if field_type not in {"text", "textarea"}:
            _fail(f"模板 {template_id} 的字段 {key} type 只能是 text/textarea")
        maximum = field.get("max")
        if maximum is not None:
            try:
                number = int(maximum)
            except (TypeError, ValueError):
                _fail(f"模板 {template_id} 的字段 {key} max 必须是正整数")
                return
            if number <= 0 or number > 20000:
                _fail(f"模板 {template_id} 的字段 {key} max 必须在 1–20000")


def _validate_assets(root: Path) -> None:
    try:
        assets_dir = safe_join(root, "assets")
    except ValueError:
        _fail("assets 路径不合法")
        return
    if not assets_dir.exists():
        return
    if not assets_dir.is_dir():
        _fail("assets 必须是目录")
    entries = sorted(assets_dir.iterdir())
    files = [entry for entry in entries if entry.is_file()]
    if len(files) != len(entries):
        _fail("assets 只能包含平铺文件，不允许子目录")
    if len(files) > MAX_ASSET_COUNT:
        _fail(f"assets 文件数超过上限（{MAX_ASSET_COUNT}）")
    total = 0
    for entry in files:
        try:
            path = safe_join(assets_dir, entry.name)
        except ValueError:
            _fail("assets 包含不安全文件名")
            return
        if path.is_symlink():
            _fail(f"assets 不允许符号链接：{entry.name}")
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            _fail(f"assets 只允许 png/jpg：{entry.name}")
        size = path.stat().st_size
        if size > MAX_ASSET_BYTES:
            _fail(f"assets 单文件超过 5 MB：{entry.name}")
        total += size
        if total > MAX_ASSETS_TOTAL_BYTES:
            _fail("assets 总体积超过 20 MB")
        header = path.read_bytes()[:8]
        if not (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")):
            _fail(f"assets 文件内容不是有效的 png/jpg：{entry.name}")


def validate_user_template(root: Path, manifest: Mapping[str, Any]) -> None:
    """用户模板的额外校验；失败抛 :class:`TemplateError`（带可读原因）。"""
    try:
        manifest_path = safe_join(root, "manifest.json")
        page_path = safe_join(root, "page.html")
        style_path = safe_join(root, "style.css")
    except ValueError:
        _fail("模板路径不合法")
        return
    if not page_path.is_file():
        _fail("缺少 page.html")
    if not style_path.is_file():
        _fail("缺少 style.css")
    _read_text(manifest_path, MAX_MANIFEST_BYTES, "manifest.json")
    html = _read_text(page_path, MAX_HTML_BYTES, "page.html")
    css = _read_text(style_path, MAX_CSS_BYTES, "style.css")

    audit = _HtmlAudit()
    try:
        audit.feed(html)
        audit.close()
    except Exception as exc:  # noqa: BLE001 - HTML 解析失败按不可用处理
        _fail(f"page.html 无法解析：{exc}")
    if audit.error:
        _fail(audit.error)
    _check_css_urls(css, "style.css")
    _validate_manifest_extras(manifest)
    _validate_assets(root)


__all__ = [
    "ALLOWED_ATTRS",
    "ALLOWED_PAGE_SIZES",
    "ALLOWED_TAGS",
    "IMAGE_SUFFIXES",
    "MAX_ASSET_BYTES",
    "MAX_ASSETS_TOTAL_BYTES",
    "MAX_ASSET_COUNT",
    "validate_user_template",
]
