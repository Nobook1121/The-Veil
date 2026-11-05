"""导出提灯三变体（E1a/E1b/E1c）的多尺寸 PNG，并截取对比页。

用法： python scripts/export-brand-lantern.py [--skip-preview]

产物只写进 frontend/src/assets/brand/concepts/lantern/，不触碰正式 icon.svg 等文件。
PNG 解码 / 覆盖率 / 连通块统计复用既有的导出脚本，不新增依赖。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from xml.dom import minidom

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "frontend" / "src" / "assets" / "brand" / "concepts" / "lantern"
SHOTS = ROOT / ".screenshots" / "brand" / "concepts" / "lantern"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

NAMES = ("e1a", "e1b", "e1c")
SIZES = (16, 24, 32, 48, 64, 128, 256)
LIGHT_SIZES = (128, 256)
SIL_SIZES = (16, 32, 256)
MONO_COLORS = {"white": "#FFFFFF", "black": "#000000"}
BONE = "#E8E0CD"


def load_helpers():
    spec = importlib.util.spec_from_file_location(
        "export_brand_icons", ROOT / "scripts" / "export-brand-icons.py"
    )
    icons = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(icons)

    spec2 = importlib.util.spec_from_file_location(
        "export_brand_concepts", ROOT / "scripts" / "export-brand-concepts.py"
    )
    concepts = importlib.util.module_from_spec(spec2)
    assert spec2.loader is not None
    spec2.loader.exec_module(concepts)
    return icons, concepts.blobs


def pick(name: str, size: int, dark: bool = False, mono: bool = False) -> str:
    """<=16 用 16 档重画，<=32 用 32 档重画，其余用母版；32/16 各有单色版本。"""
    if size <= 16:
        return f"{name}-mono-16.svg" if mono else f"{name}-16.svg"
    if size <= 32:
        return f"{name}-mono-32.svg" if mono else f"{name}-32.svg"
    if mono:
        return f"{name}-mono.svg"
    return f"{name}-dark.svg" if dark else f"{name}.svg"


def ink_stats(pixels: bytearray, width: int, height: int):
    """返回不透明像素的包围盒与 alpha 加权重心（像素坐标）。"""
    min_x, min_y, max_x, max_y = width, height, -1, -1
    sum_x = sum_y = total = 0.0
    for y in range(height):
        row = y * width
        for x in range(width):
            alpha = pixels[(row + x) * 4 + 3]
            if alpha == 0:
                continue
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            sum_x += x * alpha
            sum_y += y * alpha
            total += alpha
    if total == 0:
        return None
    return (min_x, min_y, max_x, max_y, sum_x / total, sum_y / total)


def main() -> int:
    skip_preview = "--skip-preview" in sys.argv
    icons, blobs = load_helpers()

    svg_files = sorted(DIR.glob("*.svg"))
    assert len(svg_files) == 24, f"期望 24 个 SVG，实际 {len(svg_files)}"
    for svg in svg_files:
        minidom.parse(str(svg))
    print(f"[xml] {len(svg_files)} 个 SVG 解析通过")

    SHOTS.mkdir(parents=True, exist_ok=True)
    written = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
        page = browser.new_page(device_scale_factor=1)

        def render(svg_text: str, size: int, target: Path, bg: str | None = None) -> None:
            encoded = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
            page.set_viewport_size({"width": size, "height": size})
            background = "transparent" if bg is None else bg
            page.set_content(
                "<style>html,body{margin:0;background:" + background + "}"
                f"img{{display:block;width:{size}px;height:{size}px}}</style>"
                f'<img id="mark" src="data:image/svg+xml;base64,{encoded}">',
                wait_until="load",
            )
            page.wait_for_function("() => document.getElementById('mark').complete")
            page.wait_for_timeout(40)
            page.screenshot(
                path=str(target),
                omit_background=bg is None,
                clip={"x": 0, "y": 0, "width": size, "height": size},
            )

        cache: dict[str, str] = {}

        def source(file_name: str) -> str:
            if file_name not in cache:
                cache[file_name] = (DIR / file_name).read_text(encoding="utf-8")
            return cache[file_name]

        for name in NAMES:
            for size in SIZES:
                render(source(pick(name, size)), size, DIR / f"{name}-{size}.png")
                render(source(pick(name, size, dark=True)), size, DIR / f"{name}-dark-{size}.png")
                mono = source(pick(name, size, mono=True))
                for key, hex_value in MONO_COLORS.items():
                    render(
                        mono.replace(BONE, hex_value),
                        size,
                        DIR / f"{name}-mono-{key}-{size}.png",
                    )
                written += 4

            for size in LIGHT_SIZES:
                render(source(f"{name}-light.svg"), size, DIR / f"{name}-light-{size}.png")
                written += 1

            mono_master = source(f"{name}-mono.svg")
            for size in SIL_SIZES:
                file_name = pick(name, size, mono=True)
                render(
                    source(file_name).replace(BONE, "#000000"),
                    size,
                    DIR / f"{name}-silhouette-{size}.png",
                    bg="#FFFFFF",
                )
                written += 1

        if not skip_preview:
            preview = DIR / "preview.html"
            assert preview.is_file(), "缺少 preview.html"
            page.set_viewport_size({"width": 1520, "height": 1400})
            page.goto(preview.as_uri(), wait_until="networkidle")
            page.wait_for_timeout(600)
            for panel in ("sheet-dark", "sheet-light", "silhouette-panel", "zoom-panel"):
                page.locator(f"#{panel}").screenshot(path=str(SHOTS / f"{panel}.png"))
            page.screenshot(path=str(SHOTS / "preview-full.png"), full_page=True)
            print(f"[shot] 对比页截图写入 {SHOTS}")

        browser.close()

    print("\n[几何 / 覆盖率 / 连通块]  256px 上 1px = 4 母版单位")
    for name in NAMES:
        width, height, pixels = icons.read_png(DIR / f"{name}-mono-white-256.png")
        min_x, min_y, max_x, max_y, cx, cy = ink_stats(pixels, width, height)
        print(
            f"  {name} 剪影包围盒 x {min_x}–{max_x} y {min_y}–{max_y} px"
            f"（中心 {(min_x + max_x) / 2:.1f},{(min_y + max_y) / 2:.1f} → 母版"
            f" {((min_x + max_x) / 2 + 0.5) * 4:.0f},{((min_y + max_y) / 2 + 0.5) * 4:.0f}）"
        )
        print(
            f"            留白 上 {min_y} / 下 {255 - max_y} px = 母版 {min_y * 4} / {(255 - max_y) * 4}；"
            f"alpha 重心 ({cx:.2f},{cy:.2f}) px → 母版 ({(cx + 0.5) * 4:.0f},{(cy + 0.5) * 4:.0f})"
        )
        parts256 = blobs(pixels, width, height)
        print(f"            单色 256 连通块 {len(parts256)} 块，最大 {parts256[0]}px")
        for size in SIZES:
            for kind in ("color", "dark"):
                file = DIR / f"{name}{'-dark' if kind == 'dark' else ''}-{size}.png"
                width, height, pixels = icons.read_png(file)
                assert (width, height) == (size, size), f"{file.name}: {width}×{height}"
                ratio, corners = icons.coverage(pixels, width, height)
                if size <= 32:
                    parts = blobs(pixels, width, height)
                    print(
                        f"  {file.name:24s} {width:3d}px 覆盖 {ratio:5.1%} 块 {len(parts)} "
                        f"最大块 {parts[0] if parts else 0}px 角落 {corners}"
                    )
            if size <= 32:
                mono = icons.read_png(DIR / f"{name}-mono-white-{size}.png")
                parts = blobs(mono[2], size, size)
                print(
                    f"  {name}-mono-{size}px 剪影块 {len(parts)} 最大块 "
                    f"{parts[0] if parts else 0}px"
                )
    print(f"\n共写出 {written} 张 PNG。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
