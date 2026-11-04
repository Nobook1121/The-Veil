"""确定性程序化场景图（SVG）。

不调用扩散模型：同一组（档位、强度、场景、种子）永远生成同一张图，离线可用、
零版权风险、体积小、移动端渲染开销低。外部生图作为可选增强，失败时回落到这里。
"""
from __future__ import annotations

import hashlib
import html
import math
import random

WIDTH = 1280
HEIGHT = 720

_PALETTES: dict[str, dict[str, str]] = {
    "daily": {"sky": "#33465c", "sky2": "#7c8fa3", "glow": "#f2d6a2", "ground": "#1c242e", "ridge": "#2c3a47", "particle": "#e8f0ff"},
    "suspense": {"sky": "#22343a", "sky2": "#5d7f78", "glow": "#bfe3cf", "ground": "#121b1d", "ridge": "#233437", "particle": "#a9d6c9"},
    "unease": {"sky": "#3b3226", "sky2": "#8a7a52", "glow": "#e0c37a", "ground": "#1d1912", "ridge": "#332c1f", "particle": "#d9c48c"},
    "horror": {"sky": "#2a0d12", "sky2": "#6d1d24", "glow": "#ff6b57", "ground": "#0b0507", "ridge": "#1d0a0d", "particle": "#ff8a6a"},
    "urgency": {"sky": "#36170b", "sky2": "#a3421c", "glow": "#ffb15c", "ground": "#120705", "ridge": "#291008", "particle": "#ffcf87"},
    "sorrow": {"sky": "#2b3446", "sky2": "#6a7c96", "glow": "#c9d8ee", "ground": "#141922", "ridge": "#232c3a", "particle": "#cdddf2"},
    "awe": {"sky": "#131834", "sky2": "#3b4a86", "glow": "#ffe4a3", "ground": "#0a0c1a", "ridge": "#1b2140", "particle": "#ffe9b8"},
    "aftermath": {"sky": "#2c2b2a", "sky2": "#6b6458", "glow": "#e0a568", "ground": "#121110", "ridge": "#26231f", "particle": "#d8b184"},
}
_ACCENT: dict[str, str] = {
    "daily": "#c9d6e5", "suspense": "#7fd0b3", "unease": "#e2c37a", "horror": "#ff6b57",
    "urgency": "#ffa04d", "sorrow": "#9fb8d8", "awe": "#ffe4a3", "aftermath": "#d9a672",
}


def _seed_number(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _ridge_path(rng: random.Random, baseline: int, amplitude: int, steps: int = 8) -> str:
    points = [(0, baseline)]
    for index in range(1, steps):
        x = WIDTH * index / steps
        y = baseline - rng.randint(0, amplitude) - (amplitude // 3 if index % 2 else 0)
        points.append((int(x), int(y)))
    points.append((WIDTH, baseline))
    path = f"M0,{HEIGHT} L0,{baseline}"
    for x, y in points[1:]:
        path += f" L{x},{y}"
    path += f" L{WIDTH},{HEIGHT} Z"
    return path


def _stars(rng: random.Random, count: int, color: str) -> str:
    items = []
    for _ in range(count):
        x = rng.uniform(0, WIDTH)
        y = rng.uniform(0, HEIGHT * 0.55)
        radius = rng.uniform(0.6, 2.0)
        opacity = rng.uniform(0.35, 0.95)
        items.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.2f}" fill="{color}" opacity="{opacity:.2f}"/>'
        )
    return "".join(items)


def _rain(rng: random.Random, count: int, color: str) -> str:
    items = []
    for _ in range(count):
        x = rng.uniform(0, WIDTH + 200)
        y = rng.uniform(0, HEIGHT)
        length = rng.uniform(18, 60)
        opacity = rng.uniform(0.12, 0.42)
        items.append(
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x - length * 0.22:.1f}" y2="{y + length:.1f}" '
            f'stroke="{color}" stroke-width="1.2" opacity="{opacity:.2f}"/>'
        )
    return "".join(items)


def _embers(rng: random.Random, count: int, color: str) -> str:
    items = []
    for _ in range(count):
        x = rng.uniform(0, WIDTH)
        y = rng.uniform(HEIGHT * 0.3, HEIGHT)
        radius = rng.uniform(1.0, 3.4)
        opacity = rng.uniform(0.2, 0.7)
        items.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.2f}" fill="{color}" opacity="{opacity:.2f}"/>'
        )
    return "".join(items)


def _structures(profile: str, rng: random.Random, accent: str) -> str:
    """按氛围给出极简剪影：只做暗示，不画具体角色，避免剧透与形象争议。"""
    parts: list[str] = []
    if profile in {"horror", "urgency"}:
        for index in range(5):
            base_x = 140 + index * 240 + rng.randint(-40, 40)
            height = rng.randint(120, 260)
            skew = rng.randint(-50, 50)
            parts.append(
                f'<path d="M{base_x},640 L{base_x + skew},{640 - height} L{base_x + skew + 26},{640} Z" '
                f'fill="#070405" opacity="0.75"/>'
            )
    elif profile in {"awe"}:
        for index in range(4):
            x = 220 + index * 280
            parts.append(
                f'<rect x="{x}" y="360" width="46" height="280" fill="#0b0e1c" opacity="0.8"/>'
                f'<rect x="{x - 12}" y="344" width="70" height="18" fill="#0b0e1c" opacity="0.85"/>'
            )
        parts.append(f'<ellipse cx="640" cy="330" rx="220" ry="120" fill="none" stroke="{accent}" stroke-width="3" opacity="0.22"/>')
    elif profile in {"suspense", "unease"}:
        x = 860 + rng.randint(-60, 60)
        parts.append(f'<rect x="{x}" y="430" width="150" height="210" fill="#06070a" opacity="0.8"/>')
        parts.append(f'<rect x="{x + 22}" y="454" width="106" height="186" fill="none" stroke="{accent}" stroke-width="2" opacity="0.28"/>')
        parts.append(f'<circle cx="{x + 75}" cy="548" r="4" fill="{accent}" opacity="0.8"/>')
    elif profile in {"aftermath"}:
        for index in range(6):
            x = 180 + index * 180 + rng.randint(-30, 30)
            height = rng.randint(40, 130)
            parts.append(f'<rect x="{x}" y="{640 - height}" width="34" height="{height}" fill="#0a0908" opacity="0.8" transform="rotate({rng.randint(-12, 12)} {x} 640)"/>')
    elif profile in {"daily", "sorrow"}:
        for index in range(7):
            x = 120 + index * 170 + rng.randint(-40, 40)
            height = rng.randint(60, 140)
            parts.append(f'<rect x="{x}" y="{640 - height}" width="8" height="{height}" fill="#0b0f14" opacity="0.75"/>')
            parts.append(f'<circle cx="{x + 4}" cy="{640 - height - 12}" r="{rng.randint(14, 26)}" fill="#0b0f14" opacity="0.7"/>')
    return "".join(parts)


def render_scene_svg(
    profile: str,
    intensity: str,
    scene_label: str,
    seed: str,
) -> str:
    """生成一张 1280×720 的场景 SVG（确定性）。"""
    palette = _PALETTES.get(profile, _PALETTES["daily"])
    accent = _ACCENT.get(profile, "#c9d6e5")
    rank = {"low": 0, "medium": 1, "high": 2}.get(intensity, 1)
    rng = random.Random(_seed_number(profile, intensity, scene_label, seed))
    label = html.escape((scene_label or "未知场景")[:48])

    horizon = 470 + rng.randint(-30, 30)
    ridges = "".join(
        f'<path d="{_ridge_path(rng, horizon + index * 70, 90 + index * 40)}" '
        f'fill="{palette["ridge"]}" opacity="{0.55 + index * 0.15:.2f}"/>'
        for index in range(3)
    )
    stars = _stars(rng, 40 + rank * 30, "#f5efe0") if profile in {"awe", "sorrow", "daily", "suspense"} else ""
    rain = _rain(rng, 40 + rank * 90, palette["particle"]) if profile in {"sorrow", "suspense", "unease"} else ""
    embers = _embers(rng, 20 + rank * 50, palette["particle"]) if profile in {"horror", "urgency", "aftermath"} else ""
    if profile in {"daily", "awe"}:
        embers = _embers(rng, 12 + rank * 16, palette["particle"])

    glow_radius = 90 + rank * 30
    celestial = (
        f'<circle cx="{WIDTH * 0.72:.0f}" cy="{horizon - 190 - rank * 10}" r="{glow_radius}" '
        f'fill="{palette["glow"]}" opacity="0.18"/>'
        f'<circle cx="{WIDTH * 0.72:.0f}" cy="{horizon - 190 - rank * 10}" r="{glow_radius * 0.42:.0f}" '
        f'fill="{palette["glow"]}" opacity="0.55"/>'
    )

    fog = "".join(
        f'<ellipse cx="{300 + index * 320}" cy="{horizon + 60 + index * 26}" '
        f'rx="{260 + index * 40}" ry="{34 + rank * 8}" fill="{palette["sky2"]}" opacity="{0.10 + rank * 0.03:.2f}"/>'
        for index in range(3)
    )

    grain_freq = 0.7 + rng.random() * 0.3
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{label}">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{palette["sky"]}"/>
      <stop offset="62%" stop-color="{palette["sky2"]}"/>
      <stop offset="100%" stop-color="{palette["sky"]}"/>
    </linearGradient>
    <linearGradient id="ground" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{palette["ground"]}"/>
      <stop offset="100%" stop-color="#040405"/>
    </linearGradient>
    <radialGradient id="vignette" cx="50%" cy="45%" r="72%">
      <stop offset="55%" stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.62"/>
    </radialGradient>
    <filter id="grain">
      <feTurbulence type="fractalNoise" baseFrequency="{grain_freq:.2f}" numOctaves="2" seed="{_seed_number(seed, profile) % 97}"/>
      <feColorMatrix type="saturate" values="0"/>
      <feComponentTransfer><feFuncA type="linear" slope="0.10"/></feComponentTransfer>
    </filter>
  </defs>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#sky)"/>
  {stars}
  {celestial}
  {ridges}
  <rect y="{horizon}" width="{WIDTH}" height="{HEIGHT - horizon}" fill="url(#ground)"/>
  {_structures(profile, rng, accent)}
  {fog}
  {rain}
  {embers}
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#vignette)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" filter="url(#grain)" opacity="0.5"/>
  <g opacity="0.82">
    <text x="48" y="{HEIGHT - 52}" font-family="Georgia, 'Noto Serif SC', serif" font-size="26" fill="#f2ede2" opacity="0.9">{label}</text>
    <text x="50" y="{HEIGHT - 26}" font-family="'Share Tech Mono', monospace" font-size="13" fill="{accent}" opacity="0.75">SCENE / {profile.upper()} / {intensity.upper()}</text>
  </g>
  <rect x="18" y="18" width="{WIDTH - 36}" height="{HEIGHT - 36}" fill="none" stroke="{accent}" stroke-width="1.2" opacity="0.22"/>
</svg>"""


def render_portrait_svg(
    profile: str,
    intensity: str,
    npc_name: str,
    seed: str,
    size: int = 768,
) -> str:
    """生成 NPC 头像级 SVG（头肩构图、方形、确定性、无文字）。"""
    palette = _PALETTES.get(profile, _PALETTES["daily"])
    accent = _ACCENT.get(profile, "#c9d6e5")
    rank = {"low": 0, "medium": 1, "high": 2}.get(intensity, 1)
    rng = random.Random(_seed_number("portrait", profile, intensity, npc_name, seed))
    center = size / 2
    shoulder_y = size * 0.78
    overall = 0.62 + rank * 0.08
    hair_tilt = rng.uniform(-8, 8)
    hair_width = size * rng.uniform(0.20, 0.26)
    hair_height = size * rng.uniform(0.28, 0.36)
    head_r = size * rng.uniform(0.13, 0.16)
    neck_w = size * rng.uniform(0.07, 0.09)
    shoulder_w = size * rng.uniform(0.42, 0.52)
    rim = accent
    motes = "".join(
        f'<circle cx="{rng.uniform(size * 0.1, size * 0.9):.1f}" cy="{rng.uniform(size * 0.08, size * 0.9):.1f}" '
        f'r="{rng.uniform(1.2, 3.4):.1f}" fill="{palette["particle"]}" opacity="{rng.uniform(0.18, 0.55):.2f}"/>'
        for _ in range(18 + rank * 18)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img">
  <defs>
    <radialGradient id="bg" cx="50%" cy="36%" r="78%">
      <stop offset="0%" stop-color="{palette["sky2"]}"/>
      <stop offset="58%" stop-color="{palette["sky"]}"/>
      <stop offset="100%" stop-color="#06060a"/>
    </radialGradient>
    <radialGradient id="lamp" cx="68%" cy="24%" r="40%">
      <stop offset="0%" stop-color="{palette["glow"]}" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="{palette["glow"]}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="vig" cx="50%" cy="46%" r="66%">
      <stop offset="52%" stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.68"/>
    </radialGradient>
    <linearGradient id="cloth" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{palette["ridge"]}"/>
      <stop offset="100%" stop-color="#07070b"/>
    </linearGradient>
  </defs>
  <rect width="{size}" height="{size}" fill="url(#bg)"/>
  <rect width="{size}" height="{size}" fill="url(#lamp)"/>
  {motes}
  <g opacity="{overall:.2f}">
    <path d="M {center - shoulder_w / 2:.1f} {size} L {center - shoulder_w * 0.38:.1f} {shoulder_y + 18:.1f}
      L {center - neck_w:.1f} {shoulder_y - size * 0.06:.1f} L {center + neck_w:.1f} {shoulder_y - size * 0.06:.1f}
      L {center + shoulder_w * 0.38:.1f} {shoulder_y + 18:.1f} L {center + shoulder_w / 2:.1f} {size} Z"
      fill="url(#cloth)"/>
    <rect x="{center - neck_w:.1f}" y="{shoulder_y - size * 0.18:.1f}" width="{neck_w * 2:.1f}" height="{size * 0.16:.1f}" rx="{neck_w * 0.5:.1f}" fill="{palette["ridge"]}"/>
    <ellipse cx="{center:.1f}" cy="{shoulder_y - size * 0.24:.1f}" rx="{head_r:.1f}" ry="{head_r * 1.12:.1f}" fill="#0b0b10"/>
    <ellipse cx="{center - head_r * 0.24:.1f}" cy="{shoulder_y - size * 0.25:.1f}" rx="{head_r * 0.52:.1f}" ry="{head_r * 0.72:.1f}" fill="{palette["ground"]}" opacity="0.55"/>
    <path d="M {center - hair_width:.1f} {shoulder_y - size * 0.24:.1f}
      C {center - hair_width * 0.7:.1f} {shoulder_y - size * 0.24 - hair_height:.1f}
        {center + hair_width * 0.7:.1f} {shoulder_y - size * 0.24 - hair_height:.1f}
        {center + hair_width:.1f} {shoulder_y - size * 0.24:.1f}
      L {center + hair_width * 0.72:.1f} {shoulder_y - size * 0.10:.1f}
      L {center - hair_width * 0.72:.1f} {shoulder_y - size * 0.10:.1f} Z"
      fill="#0a0a0f" transform="rotate({hair_tilt:.1f} {center:.1f} {shoulder_y - size * 0.24:.1f})"/>
    <path d="M {center + head_r * 0.62:.1f} {shoulder_y - size * 0.34:.1f}
      C {center + head_r * 0.95:.1f} {shoulder_y - size * 0.27:.1f}
        {center + head_r * 0.92:.1f} {shoulder_y - size * 0.17:.1f}
        {center + head_r * 0.66:.1f} {shoulder_y - size * 0.13:.1f}"
      fill="none" stroke="{rim}" stroke-width="{2 + rank}" opacity="0.55"/>
    <ellipse cx="{center - head_r * 0.28:.1f}" cy="{shoulder_y - size * 0.27:.1f}" rx="{head_r * 0.10:.1f}" ry="{head_r * 0.045:.1f}" fill="{rim}" opacity="0.35"/>
  </g>
  <rect width="{size}" height="{size}" fill="url(#vig)"/>
  <rect x="14" y="14" width="{size - 28}" height="{size - 28}" fill="none" stroke="{rim}" stroke-width="1.2" opacity="0.18"/>
</svg>"""
