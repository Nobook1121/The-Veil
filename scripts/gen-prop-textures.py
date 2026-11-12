#!/usr/bin/env python3
"""程序化生成道具纹理 PNG（设计稿 §4.5）。

硬约束 C1：不引入 Pillow / cairosvg / reportlab，只用 pymupdf 的绘图 API。
随机种子固定，产物可复现，生成后作为静态资源提交进
``backend/trpg_server/props/assets/``。

用法::

    python scripts/gen-prop-textures.py

产物：paper-ivory / paper-aged / stain / fold / burn / tear-edge / stamp / wax-seal /
binding-holes / perforation / gold-band
"""
from __future__ import annotations

import os
import random
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "backend" / "trpg_server" / "props" / "assets"
SEED = 20261110
DPI = 150


def _new_page(width_px: int, height_px: int):
    doc = pymupdf.open()
    page = doc.new_page(width=width_px * 72 / DPI, height=height_px * 72 / DPI)
    return doc, page


def _save(doc, name: str) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / name
    # PyMuPDF 的 PNG 导出不会覆盖已存在文件：先写临时文件再原子替换。
    temp = OUTPUT / f".{name}.tmp.png"
    if temp.exists():
        temp.unlink()
    doc[0].get_pixmap(alpha=True, dpi=DPI).save(str(temp))
    os.replace(temp, path)
    doc.close()
    print(f"{name}: {path.stat().st_size} bytes")


def _fill(page, color: tuple[float, float, float]) -> None:
    shape = page.new_shape()
    shape.draw_rect(page.rect)
    shape.finish(color=None, fill=color)
    shape.commit()


def _specks(page, rng: random.Random, count: int, *, opacity: tuple[float, float]) -> None:
    width, height = page.rect.width, page.rect.height
    for _ in range(count):
        x = rng.uniform(0, width)
        y = rng.uniform(0, height)
        radius = rng.uniform(0.2, 0.75)
        tone = rng.uniform(0.62, 0.9)
        shape = page.new_shape()
        shape.draw_circle((x, y), radius)
        shape.finish(
            color=None,
            fill=(tone, tone * 0.96, tone * 0.86),
            fill_opacity=rng.uniform(*opacity),
        )
        shape.commit()


def _blotches(page, rng: random.Random, count: int) -> None:
    width, height = page.rect.width, page.rect.height
    for _ in range(count):
        center = (rng.uniform(0, width), rng.uniform(0, height))
        radius = rng.uniform(6, 34)
        shape = page.new_shape()
        shape.draw_circle(center, radius)
        shape.finish(
            color=None,
            fill=(0.62, 0.50, 0.30),
            fill_opacity=rng.uniform(0.03, 0.07),
        )
        shape.commit()


def paper_ivory() -> None:
    rng = random.Random(SEED + 1)
    doc, page = _new_page(874, 1240)
    _fill(page, (0.965, 0.945, 0.885))
    _specks(page, rng, 900, opacity=(0.05, 0.16))
    _save(doc, "paper-ivory.png")


def paper_aged() -> None:
    rng = random.Random(SEED + 2)
    doc, page = _new_page(874, 1240)
    _fill(page, (0.905, 0.860, 0.745))
    _specks(page, rng, 900, opacity=(0.06, 0.18))
    _blotches(page, rng, 12)
    _save(doc, "paper-aged.png")


def stain() -> None:
    rng = random.Random(SEED + 3)
    doc, page = _new_page(480, 480)
    width, height = page.rect.width, page.rect.height
    for _ in range(5):
        center = (rng.uniform(width * 0.25, width * 0.7), rng.uniform(height * 0.25, height * 0.7))
        radius = rng.uniform(width * 0.12, width * 0.24)
        shape = page.new_shape()
        shape.draw_circle(center, radius)
        shape.finish(
            color=None,
            fill=(0.56, 0.42, 0.24),
            fill_opacity=rng.uniform(0.06, 0.12),
        )
        shape.commit()
    for _ in range(3):
        center = (rng.uniform(width * 0.3, width * 0.7), rng.uniform(height * 0.3, height * 0.7))
        shape = page.new_shape()
        shape.draw_circle(center, rng.uniform(width * 0.1, width * 0.18))
        shape.finish(color=(0.45, 0.32, 0.18), width=0.8, fill_opacity=0.0, stroke_opacity=0.12)
        shape.commit()
    _save(doc, "stain.png")


def fold() -> None:
    rng = random.Random(SEED + 4)
    doc, page = _new_page(220, 1240)
    width, height = page.rect.width, page.rect.height
    center = width / 2
    for offset, opacity in ((-2.2, 0.06), (-0.9, 0.10), (0.4, 0.14), (1.6, 0.08), (3.0, 0.05)):
        shape = page.new_shape()
        shape.draw_line((center + offset, 0), (center + offset + rng.uniform(-1.5, 1.5), height))
        shape.finish(color=(0.35, 0.30, 0.24), width=rng.uniform(0.5, 1.4), stroke_opacity=opacity)
        shape.commit()
    for _ in range(40):
        y = rng.uniform(0, height)
        shape = page.new_shape()
        shape.draw_line((center - 3, y), (center + 3, y + rng.uniform(-4, 4)))
        shape.finish(color=(0.30, 0.26, 0.20), width=0.5, stroke_opacity=rng.uniform(0.04, 0.10))
        shape.commit()
    _save(doc, "fold.png")


def burn() -> None:
    rng = random.Random(SEED + 5)
    doc, page = _new_page(480, 480)
    width, height = page.rect.width, page.rect.height
    # 从右上角向内烧蚀的不规则块
    for scale, tone, opacity in ((1.0, (0.16, 0.11, 0.06), 0.55), (0.72, (0.08, 0.06, 0.04), 0.6)):
        edge_x = width * (1 - 0.42 * scale)
        edge_y = height * (0.42 * scale)
        shape = page.new_shape()
        shape.draw_bezier(
            (width, edge_y),
            (width - 60 * scale, edge_y + rng.uniform(-30, 30)),
            (edge_x + rng.uniform(0, 40), height * 0.12 * scale),
            (edge_x, 0),
        )
        shape.draw_line((edge_x, 0), (width, 0))
        shape.draw_line((width, 0), (width, edge_y))
        shape.finish(color=None, fill=tone, fill_opacity=opacity)
        shape.commit()
    for _ in range(60):
        x = rng.uniform(width * 0.62, width)
        y = rng.uniform(0, height * 0.42)
        shape = page.new_shape()
        shape.draw_circle((x, y), rng.uniform(0.6, 2.4))
        shape.finish(color=None, fill=(0.10, 0.07, 0.05), fill_opacity=rng.uniform(0.15, 0.45))
        shape.commit()
    _save(doc, "burn.png")


def tear_edge() -> None:
    rng = random.Random(SEED + 6)
    doc, page = _new_page(220, 1240)
    width, height = page.rect.width, page.rect.height
    steps = 46
    previous = (width * 0.62, 0)
    for index in range(1, steps + 1):
        point = (
            width * rng.uniform(0.52, 0.72),
            height * index / steps,
        )
        shape = page.new_shape()
        shape.draw_line(previous, point)
        shape.finish(color=(0.42, 0.35, 0.26), width=rng.uniform(0.6, 1.6), stroke_opacity=0.28)
        shape.commit()
        shape = page.new_shape()
        shape.draw_line((point[0] + 2.5, point[1] - height / steps), (point[0] + 2.5, point[1]))
        shape.finish(color=(0.95, 0.92, 0.84), width=2.0, stroke_opacity=0.16)
        shape.commit()
        previous = point
    _save(doc, "tear-edge.png")


def stamp() -> None:
    rng = random.Random(SEED + 7)
    doc, page = _new_page(400, 400)
    width, height = page.rect.width, page.rect.height
    center = (width / 2, height / 2)
    for radius, width_pt, opacity in ((width * 0.40, 2.4, 0.45), (width * 0.30, 1.2, 0.35)):
        shape = page.new_shape()
        shape.draw_circle(center, radius)
        shape.finish(color=(0.62, 0.10, 0.12), width=width_pt, fill_opacity=0.0, stroke_opacity=opacity)
        shape.commit()
    for index in range(24):
        angle = index * 15
        radians = angle * 3.141592653589793 / 180
        inner = width * 0.34
        outer = width * 0.38 if index % 2 == 0 else width * 0.365
        shape = page.new_shape()
        shape.draw_line(
            (center[0] + inner * _cos(radians), center[1] + inner * _sin(radians)),
            (center[0] + outer * _cos(radians), center[1] + outer * _sin(radians)),
        )
        shape.finish(color=(0.62, 0.10, 0.12), width=1.2, stroke_opacity=0.4)
        shape.commit()
    for _ in range(80):
        x = center[0] + rng.uniform(-width * 0.36, width * 0.36)
        y = center[1] + rng.uniform(-height * 0.36, height * 0.36)
        shape = page.new_shape()
        shape.draw_circle((x, y), rng.uniform(0.4, 1.4))
        shape.finish(color=None, fill=(0.62, 0.10, 0.12), fill_opacity=rng.uniform(0.10, 0.30))
        shape.commit()
    _save(doc, "stamp.png")


def wax_seal() -> None:
    rng = random.Random(SEED + 8)
    doc, page = _new_page(280, 280)
    width, height = page.rect.width, page.rect.height
    center = (width / 2, height / 2)
    points: list[tuple[float, float]] = []
    for index in range(28):
        radians = index * 2 * 3.141592653589793 / 28
        radius = width * rng.uniform(0.28, 0.36)
        points.append((center[0] + radius * _cos(radians), center[1] + radius * _sin(radians)))
    shape = page.new_shape()
    shape.draw_polyline(points + [points[0]])
    shape.finish(color=(0.35, 0.05, 0.06), width=0.8, fill=(0.52, 0.08, 0.09), fill_opacity=0.85)
    shape.commit()
    shape = page.new_shape()
    shape.draw_circle(center, width * 0.18)
    shape.finish(color=(0.30, 0.04, 0.05), width=1.2, fill_opacity=0.0, stroke_opacity=0.5)
    shape.commit()
    for _ in range(40):
        x = center[0] + rng.uniform(-width * 0.14, width * 0.14)
        y = center[1] + rng.uniform(-height * 0.14, height * 0.14)
        shape = page.new_shape()
        shape.draw_circle((x, y), rng.uniform(0.5, 1.6))
        shape.finish(color=None, fill=(0.86, 0.72, 0.55), fill_opacity=rng.uniform(0.08, 0.22))
        shape.commit()
    _save(doc, "wax-seal.png")


def binding_holes() -> None:
    """卷宗左侧装订孔：纵向一排深色圆孔，透明底。"""
    rng = random.Random(SEED + 9)
    doc, page = _new_page(200, 1240)
    width, height = page.rect.width, page.rect.height
    for index in range(5):
        center = (width * 0.45, height * (0.14 + index * 0.18))
        for radius, opacity in ((17, 0.55), (12, 0.85)):
            shape = page.new_shape()
            shape.draw_circle(center, radius)
            shape.finish(color=None, fill=(0.18, 0.14, 0.10), fill_opacity=opacity)
            shape.commit()
        for _ in range(10):
            shape = page.new_shape()
            shape.draw_circle(
                (center[0] + rng.uniform(-20, 20), center[1] + rng.uniform(-20, 20)),
                rng.uniform(0.6, 2.0),
            )
            shape.finish(color=None, fill=(0.12, 0.10, 0.08), fill_opacity=rng.uniform(0.05, 0.16))
            shape.commit()
    _save(doc, "binding-holes.png")


def perforation() -> None:
    """票据撕口：横向虚线 + 半圆缺口，透明底。"""
    rng = random.Random(SEED + 10)
    doc, page = _new_page(1240, 48)
    width, height = page.rect.width, page.rect.height
    y = height / 2
    for x in range(0, int(width), 22):
        shape = page.new_shape()
        shape.draw_line((x, y), (min(width, x + 12), y))
        shape.finish(color=(0.42, 0.35, 0.26), width=1.2, stroke_opacity=0.45)
        shape.commit()
    for x in range(10, int(width), 60):
        shape = page.new_shape()
        shape.draw_circle((x, y), rng.uniform(4.5, 6.5))
        shape.finish(color=(0.80, 0.74, 0.62), width=1.0, fill=(0.97, 0.95, 0.90), fill_opacity=0.9)
        shape.commit()
    _save(doc, "perforation.png")


def gold_band() -> None:
    """请柬烫金花边：横向双线 + 菱形花纹，透明底。"""
    rng = random.Random(SEED + 11)
    doc, page = _new_page(1200, 96)
    width, height = page.rect.width, page.rect.height
    gold = (0.72, 0.58, 0.28)
    for offset, line_width, opacity in ((14, 2.2, 0.85), (22, 0.9, 0.55)):
        shape = page.new_shape()
        shape.draw_line((0, offset), (width, offset))
        shape.draw_line((0, height - offset), (width, height - offset))
        shape.finish(color=gold, width=line_width, stroke_opacity=opacity)
        shape.commit()
    center_y = height / 2
    for x in range(40, int(width), 80):
        radius = rng.uniform(3.0, 5.0)
        shape = page.new_shape()
        shape.draw_polyline(
            [(x, center_y - radius), (x + radius, center_y), (x, center_y + radius), (x - radius, center_y), (x, center_y - radius)]
        )
        shape.finish(color=gold, width=1.0, fill_opacity=0.0, stroke_opacity=0.7)
        shape.commit()
    _save(doc, "gold-band.png")


def _cos(value: float) -> float:
    import math

    return math.cos(value)


def _sin(value: float) -> float:
    import math

    return math.sin(value)


def main() -> int:
    paper_ivory()
    paper_aged()
    stain()
    fold()
    burn()
    tear_edge()
    stamp()
    wax_seal()
    binding_holes()
    perforation()
    gold_band()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
