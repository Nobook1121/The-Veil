"""导出三套极简图标概念的多尺寸 PNG（主色 + 单色纯白/纯黑），并截取对比页。

用法： python scripts/export-brand-concepts.py [--skip-preview]

依赖：playwright（用本机 Microsoft Edge 作 Chromium 内核）。PNG 解码与覆盖率检查
复用 export-brand-icons.py，不引入 Pillow 等新依赖。
产物只写进 frontend/src/assets/brand/concepts/，不触碰正式 icon.svg 等文件。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from xml.dom import minidom

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CONCEPTS = ROOT / "frontend" / "src" / "assets" / "brand" / "concepts"
SHOTS = ROOT / ".screenshots" / "brand" / "concepts"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

NAMES = ("a1", "a2", "a3")
PAGES = ("preview-variants.html",)
SIZES = (16, 32, 64, 256)
MONO_COLORS = {"white": "#FFFFFF", "black": "#000000"}


def load_icon_helpers():
    """复用正式图标导出脚本里的 PNG 解码函数。"""
    spec = importlib.util.spec_from_file_location(
        "export_brand_icons", ROOT / "scripts" / "export-brand-icons.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def blobs(pixels: bytearray, width: int, height: int, threshold: int = 64) -> list[int]:
    """按 4 连通统计不透明块，用来判断 16px 下剪影是否被碎成噪点。"""
    taken = [False] * (width * height)
    areas: list[int] = []
    for index in range(width * height):
        if taken[index] or pixels[index * 4 + 3] < threshold:
            continue
        stack = [index]
        taken[index] = True
        area = 0
        while stack:
            current = stack.pop()
            area += 1
            x, y = current % width, current // width
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    nxt = ny * width + nx
                    if not taken[nxt] and pixels[nxt * 4 + 3] >= threshold:
                        taken[nxt] = True
                        stack.append(nxt)
        areas.append(area)
    return sorted(areas, reverse=True)


def main() -> int:
    skip_preview = "--skip-preview" in sys.argv
    icons = load_icon_helpers()

    for name in NAMES:
        for suffix in (".svg", "-mono.svg"):
            minidom.parse(str(CONCEPTS / f"{name}{suffix}"))
            print(f"[xml] {name}{suffix} 解析通过")

    SHOTS.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
        page = browser.new_page(device_scale_factor=1)

        def render(svg_text: str, size: int, target: Path) -> None:
            encoded = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(
                "<style>html,body{margin:0;background:transparent}"
                f"img{{display:block;width:{size}px;height:{size}px}}</style>"
                f'<img id="mark" src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            page.wait_for_function("() => document.getElementById('mark').complete")
            page.wait_for_timeout(60)
            page.screenshot(
                path=str(target),
                omit_background=True,
                clip={"x": 0, "y": 0, "width": size, "height": size},
            )
            rendered.append(target)

        for name in NAMES:
            color_svg = (CONCEPTS / f"{name}.svg").read_text(encoding="utf-8")
            mono_svg = (CONCEPTS / f"{name}-mono.svg").read_text(encoding="utf-8")
            for size in SIZES:
                render(color_svg, size, CONCEPTS / f"{name}-{size}.png")
                for key, hex_value in MONO_COLORS.items():
                    variant = mono_svg.replace("#E8E0CD", hex_value)
                    render(variant, size, CONCEPTS / f"{name}-mono-{key}-{size}.png")

        if not skip_preview:
            for name in PAGES:
                preview = CONCEPTS / name
                if not preview.is_file():
                    continue
                page.set_viewport_size({"width": 1400, "height": 1200})
                page.goto(preview.as_uri(), wait_until="networkidle")
                page.wait_for_timeout(400)
                stem = preview.stem
                for panel in ("sheet-dark", "sheet-light"):
                    page.locator(f"#{panel}").screenshot(
                        path=str(SHOTS / f"{stem}-{panel}.png")
                    )
                page.screenshot(path=str(SHOTS / f"{stem}-full.png"), full_page=True)
                print(f"[shot] 对比页截图写入 {SHOTS}")

        browser.close()

    print("\n[尺寸与剪影检查]")
    for name in NAMES:
        for size in SIZES:
            for kind in ("color", "mono-white", "mono-black"):
                file = CONCEPTS / (
                    f"{name}-{size}.png"
                    if kind == "color"
                    else f"{name}-mono-{kind.split('-')[1]}-{size}.png"
                )
                width, height, pixels = icons.read_png(file)
                assert (width, height) == (size, size), f"{file.name}: {width}×{height}"
                if size <= 32:
                    parts = blobs(pixels, width, height)
                    ratio, _ = icons.coverage(pixels, width, height)
                    print(
                        f"  {file.name:28s} {width:3d}×{height:<3d} 覆盖 {ratio:5.1%} "
                        f"块 {len(parts)} 最大块 {parts[0] if parts else 0}px"
                    )
    print(f"\n共写出 {len(rendered)} 张 PNG。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
