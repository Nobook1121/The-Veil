"""导入地图图片的校验工具。

只读取文件头判断格式与尺寸，不把整张图解码进内存，也不依赖 Pillow。
所有错误都带可读中文消息与稳定 code，由插件转换为写操作回执。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 40 * 1024 * 1024
MIN_IMAGE_DIMENSION = 64
MAX_IMAGE_DIMENSION = 16384
MAX_IMAGE_PIXELS = 120_000_000
SUPPORTED_IMAGE_SUFFIXES = ("png", "jpg", "jpeg", "webp", "bmp")


class ImageError(Exception):
    """图片校验失败；``code`` 供前端区分错误类型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def image_suffix(filename: Any) -> str:
    return Path(str(filename or "")).suffix.lstrip(".").lower()


def _png_size(raw: bytes) -> tuple[int, int] | None:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        return None
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    if not raw.startswith(b"\xff\xd8"):
        return None
    index = 2
    total = len(raw)
    while index + 9 < total:
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(raw[index + 2 : index + 4], "big")
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(raw[index + 5 : index + 7], "big")
            width = int.from_bytes(raw[index + 7 : index + 9], "big")
            return width, height
        index += 2 + length
    return None


def _webp_size(raw: bytes) -> tuple[int, int] | None:
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP" or len(raw) < 30:
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L" and len(raw) >= 25:
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(raw[26:28], "little") & 0x3FFF
        height = int.from_bytes(raw[28:30], "little") & 0x3FFF
        return width, height
    return None


def _bmp_size(raw: bytes) -> tuple[int, int] | None:
    if raw[:2] != b"BM" or len(raw) < 30:
        return None
    header = int.from_bytes(raw[14:18], "little")
    if header not in (12, 40, 52, 56, 108, 124):
        return None
    if header == 12:
        width = int.from_bytes(raw[18:20], "little")
        height = int.from_bytes(raw[20:22], "little")
    else:
        width = int.from_bytes(raw[18:22], "little", signed=True)
        height = int.from_bytes(raw[22:26], "little", signed=True)
    return abs(width), abs(height)


_READERS = {
    "png": _png_size,
    "jpg": _jpeg_size,
    "jpeg": _jpeg_size,
    "webp": _webp_size,
    "bmp": _bmp_size,
}


def inspect_image(raw: bytes, filename: Any) -> dict[str, Any]:
    """校验上传图片，返回 ``{suffix, width, height, size}``；失败抛 ImageError。"""
    suffix = image_suffix(filename)
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise ImageError(
            "map.image_unsupported",
            "只支持 PNG / JPG / WEBP / BMP 格式的地图图片。",
        )
    if not raw:
        raise ImageError("map.image_empty", "上传的图片是空文件，请重新选择。")
    if len(raw) > MAX_IMAGE_BYTES:
        limit = MAX_IMAGE_BYTES // (1024 * 1024)
        raise ImageError(
            "map.image_too_large",
            f"地图图片不能超过 {limit} MB，请压缩或裁切后再导入。",
        )
    reader = _READERS.get(suffix)
    size = reader(raw) if reader else None
    if not size:
        raise ImageError(
            "map.image_invalid",
            "图片内容无法识别，请确认文件没有损坏、且扩展名与实际格式一致。",
        )
    width, height = size
    if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
        raise ImageError(
            "map.image_tiny",
            f"图片尺寸过小（{width}×{height}），至少需要 {MIN_IMAGE_DIMENSION}×{MIN_IMAGE_DIMENSION} 像素。",
        )
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ImageError(
            "map.image_dimension",
            f"图片尺寸异常（{width}×{height}），单边不能超过 {MAX_IMAGE_DIMENSION} 像素。",
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageError(
            "map.image_pixels",
            f"图片像素总量过大（{width}×{height}），请先缩小到 1.2 亿像素以内。",
        )
    return {"suffix": suffix, "width": width, "height": height, "size": len(raw)}
