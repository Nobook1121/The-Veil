"""中文字体探测与 Story 字体归档（设计稿 §5.3 / C5）。

导出 PDF 必须保证中文可显示：这里只接受**能真正打开且覆盖常用汉字**的字体
文件，任何候选都不可用时抛 :class:`FontMissing`，由接口回 503，绝不静默
生成方块字 PDF。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pymupdf

from trpg_server.settings import DATA_DIR

logger = logging.getLogger(__name__)

FONT_FAMILY = "VeilBody"
# 常用探测顺序：黑体 → 微软雅黑 → 宋体；最后再找随包附带的 data/assets/fonts。
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
    ("MSYaHei", r"C:\Windows\Fonts\msyh.ttc"),
    ("SimSun", r"C:\Windows\Fonts\simsun.ttc"),
)
# 用来验证字体确实覆盖汉字的最小集合。「中」「文」足以排除只有拉丁字形的字体。
_PROBE_TEXT = "中文团录"


class FontMissing(RuntimeError):
    """本机找不到可嵌入的中文字体。"""


def resolve_font(override: str = "", *, data_dir: str | Path | None = None) -> tuple[str, str]:
    """返回 ``(family_name, abs_path)``；找不到时抛 :class:`FontMissing`。

    ``override`` 来自 ``[room_log] font_file``，一旦提供就只认它（但仍会验证
    字体可打开且含汉字），避免管理员指定了字体却被悄悄忽略。
    """
    if override:
        candidate = Path(str(override)).expanduser()
        if _is_usable_font(candidate):
            return FONT_FAMILY, str(candidate.resolve())
        raise FontMissing(f"configured font is not usable: {override}")

    for _name, raw_path in CANDIDATES:
        # 用 os.path.exists 而不是 Path.exists，便于测试用 monkeypatch 模拟「无字体环境」。
        if not os.path.exists(raw_path):
            continue
        path = Path(raw_path)
        if _is_usable_font(path):
            return FONT_FAMILY, str(path.resolve())
        logger.warning("room_log font candidate rejected path=%s", raw_path)

    bundled = _bundled_font(data_dir)
    if bundled is not None:
        return FONT_FAMILY, str(bundled)
    raise FontMissing("no CJK font found; set [room_log] font_file")


def _bundled_font(data_dir: str | Path | None) -> Path | None:
    root = Path(data_dir) if data_dir else DATA_DIR
    directory = root / "assets" / "fonts"
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
            continue
        if _is_usable_font(path):
            return path.resolve()
    return None


def _is_usable_font(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        font = pymupdf.Font(fontfile=str(path))
    except Exception:  # noqa: BLE001 - 损坏/不支持的字体一律跳过
        return False
    try:
        return all(font.has_glyph(ord(char)) for char in _PROBE_TEXT)
    except Exception:  # noqa: BLE001
        return False


def font_css(font_path: str | Path, family: str = FONT_FAMILY) -> str:
    """生成 Story 可用的 ``@font-face``；``url`` 只写文件名，由 Archive 解析。"""
    return (
        f"@font-face {{ font-family: {family}; "
        f"src: url({Path(font_path).name}); }}"
    )


def build_archive(font_path: str | Path, extra_dirs: list[str | Path] | tuple[str | Path, ...] = ()):
    """构造 Story 用的 Archive：字体所在目录 + 额外资源目录（图片临时目录）。"""
    archive = pymupdf.Archive(str(Path(font_path).resolve().parent))
    for directory in extra_dirs:
        archive.add(str(Path(directory).resolve()))
    return archive


__all__ = [
    "CANDIDATES",
    "FONT_FAMILY",
    "FontMissing",
    "build_archive",
    "font_css",
    "resolve_font",
]
