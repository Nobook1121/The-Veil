"""导出骰子意象四候选（D1/D2/D3/E1）的多尺寸 PNG，并截取对比页。

用法： python scripts/export-brand-dice.py [--skip-preview]

依赖：playwright（用本机 Microsoft Edge 作 Chromium 内核）。PNG 解码、覆盖率与
连通块统计复用 export-brand-icons.py / export-brand-concepts.py，不新增依赖。
产物只写进 frontend/src/assets/brand/concepts/dice/，不触碰正式 icon.svg 等文件。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from xml.dom import minidom

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DICE = ROOT / "frontend" / "src" / "assets" / "brand" / "concepts" / "dice"
SHOTS = ROOT / ".screenshots" / "brand" / "concepts" / "dice"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

NAMES = ("d1", "d2", "d3", "e1")
SIZES = (16, 24, 32, 48, 64, 128, 256)
MONO_COLORS = {"white": "#FFFFFF", "black": "#000000"}


def load_helpers():
    """复用正式导出脚本的 PNG 解码 / 覆盖率，以及概念脚本的连通块统计。"""
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
    """<=16 用 16 档重画，<=32 用 32 档重画，其余用母版；32/16 档各有单色版本。"""
    if size <= 16:
        return f"{name}-mono-16.svg" if mono else f"{name}-16.svg"
    if size <= 32:
        return f"{name}-mono-32.svg" if mono else f"{name}-32.svg"
    if mono:
        return f"{name}-mono.svg"
    return f"{name}-dark.svg" if dark else f"{name}.svg"


def main() -> int:
    skip_preview = "--skip-preview" in sys.argv
    icons, blobs = load_helpers()

    svg_files = sorted(DICE.glob("*.svg"))
    assert len(svg_files) == 28, f"期望 28 个 SVG，实际 {len(svg_files)}"
    for svg in svg_files:
        minidom.parse(str(svg))
    print(f"[xml] {len(svg_files)} 个 SVG 解析通过")

    SHOTS.mkdir(parents=True, exist_ok=True)
    written = 0

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
            page.wait_for_timeout(40)
            page.screenshot(
                path=str(target),
                omit_background=True,
                clip={"x": 0, "y": 0, "width": size, "height": size},
            )

        cache: dict[str, str] = {}

        def source(file_name: str) -> str:
            if file_name not in cache:
                cache[file_name] = (DICE / file_name).read_text(encoding="utf-8")
            return cache[file_name]

        for name in NAMES:
            for size in SIZES:
                render(source(pick(name, size)), size, DICE / f"{name}-{size}.png")
                render(source(pick(name, size, dark=True)), size, DICE / f"{name}-dark-{size}.png")
                mono = source(pick(name, size, mono=True))
                for key, hex_value in MONO_COLORS.items():
                    render(
                        mono.replace("#E8E0CD", hex_value),
                        size,
                        DICE / f"{name}-mono-{key}-{size}.png",
                    )
                written += 4

        if not skip_preview:
            preview = DICE / "preview.html"
            assert preview.is_file(), "缺少 preview.html"
            page.set_viewport_size({"width": 1480, "height": 1200})
            page.goto(preview.as_uri(), wait_until="networkidle")
            page.wait_for_timeout(500)
            for panel in ("sheet-dark", "sheet-light"):
                page.locator(f"#{panel}").screenshot(path=str(SHOTS / f"preview-{panel}.png"))
            page.screenshot(path=str(SHOTS / "preview-full.png"), full_page=True)
            print(f"[shot] 对比页截图写入 {SHOTS}")

        browser.close()

    print("\n[尺寸 / 覆盖率 / 剪影检查]")
    for name in NAMES:
        for size in SIZES:
            for kind in ("color", "dark"):
                file = DICE / f"{name}{'-dark' if kind == 'dark' else ''}-{size}.png"
                width, height, pixels = icons.read_png(file)
                assert (width, height) == (size, size), f"{file.name}: {width}×{height}"
                ratio, corners = icons.coverage(pixels, width, height)
                if size <= 32:
                    parts = blobs(pixels, width, height)
                    print(
                        f"  {file.name:26s} {width:3d}px 覆盖 {ratio:5.1%} 块 {len(parts)} "
                        f"最大块 {parts[0] if parts else 0}px 角落 {corners}"
                    )
            mono = icons.read_png(DICE / f"{name}-mono-white-{size}.png")
            assert mono[0] == size == mono[1]
            if size <= 32:
                parts = blobs(mono[2], size, size)
                print(
                    f"  {name}-mono-white-{size}px 剪影块 {len(parts)} 最大块 "
                    f"{parts[0] if parts else 0}px"
                )
    print(f"\n共写出 {written} 张 PNG。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
