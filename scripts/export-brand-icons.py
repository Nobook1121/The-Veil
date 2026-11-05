"""导出「帷幕 The Veil」品牌图标的多尺寸 PNG / ICO / Apple Touch 图标。

用法： python scripts/export-brand-icons.py [--skip-preview]

依赖：playwright（仅用本机已安装的 Microsoft Edge 作为 Chromium 内核）。
不引入 Pillow：PNG 解码与 ICO(BMP) 打包均为标准库实现，保证发布工具零新增依赖。
"""

from __future__ import annotations

import base64
import struct
import sys
import zlib
from pathlib import Path
from xml.dom import minidom

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "frontend" / "src" / "assets" / "brand"
SHOTS = ROOT / ".screenshots" / "brand"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

# (源 SVG, 目标像素, 输出文件名)
RENDERS = [
    ("icon.svg", 512, "icon-512.png"),
    ("icon.svg", 256, "icon-256.png"),
    ("icon.svg", 128, "icon-128.png"),
    ("icon.svg", 64, "icon-64.png"),
    ("icon.svg", 48, "icon-48.png"),
    ("icon-simple.svg", 32, "icon-32.png"),
    ("icon-16.svg", 16, "icon-16.png"),
    ("icon-maskable.svg", 512, "icon-maskable-512.png"),
    ("icon-mono.svg", 256, "icon-mono-256.png"),
    ("icon-maskable.svg", 180, "apple-touch-icon.png"),
]


def read_png(path: Path) -> tuple[int, int, bytearray]:
    """把 8bit RGBA 非隔行 PNG 解成宽度优先的 RGBA 字节流。"""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name}: 不是 PNG"
    pos = 8
    width = height = 0
    bit_depth = color_type = 0
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            assert interlace == 0, f"{path.name}: 不支持隔行 PNG"
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    assert bit_depth == 8, f"{path.name}: 需要 8bit PNG"
    assert color_type in (2, 6), f"{path.name}: 需要 RGB/RGBA（得到色彩类型 {color_type}）"
    raw = zlib.decompress(bytes(idat))
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    pixels = bytearray()
    previous = bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(stride):
                line[i] = (line[i] + (line[i - bpp] if i >= bpp else 0)) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                up_left = previous[i - bpp] if i >= bpp else 0
                estimate = left + up - up_left
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
                predictor = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                line[i] = (line[i] + predictor) & 0xFF
        else:
            raise ValueError(f"{path.name}: 未知 PNG 过滤类型 {filter_type}")
        if color_type == 2:
            for i in range(0, stride, 3):
                pixels += bytes((line[i], line[i + 1], line[i + 2], 255))
        else:
            pixels += line
        previous = line
    return width, height, pixels


def bmp_image(pixels: bytearray, width: int, height: int) -> bytes:
    """构造 ICO 内嵌的 32bpp DIB：XOR 位图（BGRA，自下而上）+ 1bpp AND 掩码。"""
    pixel_bytes = bytearray()
    for y in range(height - 1, -1, -1):
        row = pixels[y * width * 4 : (y + 1) * width * 4]
        for x in range(width):
            r, g, b, a = row[x * 4 : x * 4 + 4]
            pixel_bytes += bytes((b, g, r, a))

    mask_row_bytes = ((width + 31) // 32) * 4
    mask = bytearray()
    for y in range(height - 1, -1, -1):
        row = pixels[y * width * 4 : (y + 1) * width * 4]
        bits = bytearray(mask_row_bytes)
        for x in range(width):
            if row[x * 4 + 3] < 128:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits

    header = struct.pack(
        "<IiiHHIIiiII",
        40,               # biSize
        width,            # biWidth
        height * 2,       # biHeight：XOR + AND
        1,                # biPlanes
        32,               # biBitCount
        0,                # biCompression = BI_RGB
        len(pixel_bytes) + len(mask),
        0, 0, 0, 0,
    )
    return header + pixel_bytes + mask


def write_ico(target: Path, entries: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = bytearray()
    payload = bytearray()
    offset = 6 + 16 * len(entries)
    for size, image in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0, 0, 1, 32, len(image), offset + len(payload),
        )
        payload += image
    target.write_bytes(header + directory + payload)


def coverage(pixels: bytearray, width: int, height: int) -> tuple[float, str]:
    opaque = 0
    for y in range(height):
        for x in range(width):
            if pixels[(y * width + x) * 4 + 3] > 0:
                opaque += 1
    corners = [
        pixels[(0 * width + 0) * 4 + 3],
        pixels[(0 * width + width - 1) * 4 + 3],
        pixels[((height - 1) * width + 0) * 4 + 3],
        pixels[((height - 1) * width + width - 1) * 4 + 3],
    ]
    return opaque / (width * height), "/".join(str(value) for value in corners)


def main() -> int:
    skip_preview = "--skip-preview" in sys.argv
    for svg in ("icon.svg", "icon-simple.svg", "icon-16.svg", "icon-maskable.svg", "icon-mono.svg"):
        minidom.parse(str(BRAND / svg))
        print(f"[xml] {svg} 解析通过")

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
        page = browser.new_page(device_scale_factor=1)

        def render(svg: str, size: int, target: Path) -> None:
            source = (BRAND / svg).read_text(encoding="utf-8")
            encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(
                "<style>html,body{margin:0;background:transparent}"
                f"img{{display:block;width:{size}px;height:{size}px}}</style>"
                f'<img id="mark" src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            page.wait_for_function("() => document.getElementById('mark').complete")
            page.screenshot(
                path=str(target),
                omit_background=True,
                clip={"x": 0, "y": 0, "width": size, "height": size},
            )

        for svg, size, name in RENDERS:
            render(svg, size, BRAND / name)

        if not skip_preview:
            preview = page.goto((BRAND / "preview.html").as_uri(), wait_until="networkidle")
            assert preview is not None
            page.set_viewport_size({"width": 1120, "height": 900})
            page.wait_for_timeout(300)
            for panel in ("panel-dark", "panel-light"):
                page.locator(f"#{panel}").screenshot(path=str(SHOTS / f"preview-{panel}.png"))
            page.screenshot(path=str(SHOTS / "preview-full.png"), full_page=True)
            print(f"[shot] 预览截图写入 {SHOTS}")

        browser.close()

    entries = []
    for size in (16, 32, 48):
        source = BRAND / f"icon-{size}.png"
        width, height, pixels = read_png(source)
        assert width == height == size, f"{source.name}: {width}×{height}"
        entries.append((size, bmp_image(pixels, width, height)))
    write_ico(BRAND / "favicon.ico", entries)

    print("\n[尺寸检查]")
    for _, size, name in RENDERS:
        path = BRAND / name
        width, height, pixels = read_png(path)
        ratio, corners = coverage(pixels, width, height)
        print(
            f"  {name:24s} {width:4d}×{height:<4d} 覆盖率 {ratio:6.1%}  "
            f"角落 alpha {corners}"
        )
    ico = BRAND / "favicon.ico"
    data = ico.read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    print(f"  favicon.ico 条目 {count} 个：", end="")
    for index in range(count):
        entry = data[6 + index * 16 : 6 + (index + 1) * 16]
        size = entry[0] or 256
        print(f"{size}px({struct.unpack('<I', entry[8:12])[0]}B)", end="  ")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
