"""本地自动配乐：不依赖大模型，把叙事分析直接翻译成前端可演奏的乐谱。

设计目标：
- 确定性：同一 (profile, intensity, section, seed) 永远生成同一份乐谱；
- 可演奏：输出严格符合 ``director_schema.validate_score`` 的字段；
- 有结构：四段（铺垫/上升/高潮/余韵），由前端再补留白与尾段；
- 有色彩：按档位选择半减七、减七、三全音、那不勒斯 bII、音簇等和弦，
  避免恐怖段落出现明亮大三和弦；
- 可回退：AI 导演不可用时仍有一套完整配乐，而不是只剩「和弦垫」。
"""

from __future__ import annotations

import hashlib
from typing import Any

from trpg_server.immersive.director_schema import CHORD_QUALITIES

# 档位默认调式；locrian 专门给恐怖/深渊场景。
PROFILE_MODES: dict[str, str] = {
    "daily": "mixolydian",
    "suspense": "dorian",
    "unease": "phrygian",
    "horror": "locrian",
    "urgency": "harmonic_minor",
    "sorrow": "minor",
    "awe": "lydian",
    "aftermath": "dorian",
}

# 基准 tempo（中等强度）；低/高强度在前端与 plan 中再缩放。
PROFILE_TEMPO: dict[str, int] = {
    "daily": 92,
    "suspense": 68,
    "unease": 58,
    "horror": 48,
    "urgency": 118,
    "sorrow": 62,
    "awe": 72,
    "aftermath": 54,
}

# 和弦：相对主音的半音偏移 + 色彩。按档位选配，不追求功能解决。
PROFILE_CHORDS: dict[str, list[tuple[int, str]]] = {
    "daily": [(0, "major"), (4, "minor"), (5, "minor"), (7, "major"), (9, "minor"), (4, "sus2"), (2, "minor"), (5, "sus4")],
    "suspense": [(0, "minor"), (6, "half_dim"), (1, "major"), (4, "minor"), (0, "min_maj7"), (5, "min7"), (3, "major"), (6, "tritone")],
    "unease": [(0, "minor"), (1, "major"), (6, "half_dim"), (5, "dim7"), (0, "min_maj7"), (7, "tritone"), (3, "cluster"), (1, "major")],
    "horror": [(0, "half_dim"), (6, "dim7"), (1, "major"), (7, "tritone"), (0, "min_maj7"), (5, "half_dim"), (3, "cluster"), (6, "dim7")],
    "urgency": [(0, "minor"), (4, "minor"), (7, "tritone"), (0, "minor"), (5, "half_dim"), (4, "minor"), (6, "dim7"), (0, "minor")],
    "sorrow": [(0, "minor"), (4, "minor"), (6, "half_dim"), (5, "minor"), (0, "min7"), (3, "major"), (6, "minor"), (4, "sus2")],
    "awe": [(0, "major"), (5, "major"), (7, "major"), (9, "minor"), (0, "maj7"), (4, "minor"), (2, "minor"), (7, "sus4")],
    "aftermath": [(0, "minor"), (5, "minor"), (4, "minor"), (6, "half_dim"), (0, "min7"), (3, "major"), (6, "minor"), (4, "sus2")],
}

PROFILE_INSTRUMENTS: dict[str, list[str]] = {
    "daily": ["pad", "pluck", "bass", "bells", "strings", "ostinato"],
    "suspense": ["pad", "strings", "drone", "bass", "bells", "ostinato"],
    "unease": ["strings", "organ", "drone", "bass", "noise", "bells"],
    "horror": ["drone", "organ", "strings", "timpani", "bass", "bells"],
    "urgency": ["strings", "ostinato", "bass", "timpani", "brass", "noise"],
    "sorrow": ["pad", "strings", "choir", "bass", "bells", "organ"],
    "awe": ["choir", "organ", "strings", "bells", "bass", "brass"],
    "aftermath": ["pad", "strings", "drone", "bass", "bells", "choir"],
}

# 三族动机：音级（相对主音）、节奏（拍）、角色。每族 4~6 音，避免长旋律。
PROFILE_MOTIFS: dict[str, list[dict[str, Any]]] = {
    "daily": [
        {"degrees": [0, 4, 7, 4], "rhythm": [1, 1, 1, 1], "role": "statement"},
        {"degrees": [0, 2, 5, 7, 5], "rhythm": [1, 0.5, 1, 1, 0.5], "role": "answer"},
        {"degrees": [0, -3, 2, 7], "rhythm": [1, 1.5, 1, 0.5], "role": "fragment"},
    ],
    "suspense": [
        {"degrees": [0, -1, 3, 2], "rhythm": [1.5, 1, 1, 0.5], "role": "statement"},
        {"degrees": [0, 3, -1, 2], "rhythm": [1, 1, 1.5, 1.5], "role": "answer"},
        {"degrees": [0, -1, -3], "rhythm": [1, 1, 2], "role": "fragment"},
    ],
    "unease": [
        {"degrees": [0, -2, -5, -1], "rhythm": [1, 1, 1.5, 1.5], "role": "statement"},
        {"degrees": [0, 1, -2, 5], "rhythm": [0.5, 1, 1, 1.5], "role": "answer"},
        {"degrees": [0, 1, 6, -2], "rhythm": [1, 0.5, 1, 2], "role": "climax"},
    ],
    "horror": [
        {"degrees": [0, -1, -3, -6], "rhythm": [1.5, 1.5, 1, 1], "role": "statement"},
        {"degrees": [0, 6, 3, 1], "rhythm": [1, 1, 1.5, 1.5], "role": "answer"},
        {"degrees": [0, -2, -5, -7], "rhythm": [1, 0.5, 1, 2], "role": "fragment"},
    ],
    "urgency": [
        {"degrees": [0, 3, 5, 3, 0], "rhythm": [0.5, 0.5, 1, 0.5, 0.5], "role": "statement"},
        {"degrees": [0, -2, 4, -2], "rhythm": [1, 0.5, 1, 0.5], "role": "answer"},
        {"degrees": [0, 4, 7, 12], "rhythm": [1, 0.5, 0.5, 1], "role": "climax"},
    ],
    "sorrow": [
        {"degrees": [0, -2, -4, -7], "rhythm": [1.5, 1, 1.5, 1], "role": "statement"},
        {"degrees": [0, 2, -2, 4], "rhythm": [1, 1.5, 1, 1.5], "role": "answer"},
        {"degrees": [0, -2, -4], "rhythm": [2, 1, 1], "role": "fragment"},
    ],
    "awe": [
        {"degrees": [0, 4, 7, 11], "rhythm": [1.5, 1, 1.5, 1], "role": "statement"},
        {"degrees": [0, 5, 9, 5], "rhythm": [1, 1, 1.5, 1.5], "role": "answer"},
        {"degrees": [0, 7, 4, 12], "rhythm": [1, 1.5, 1, 1], "role": "climax"},
    ],
    "aftermath": [
        {"degrees": [0, 3, 6, 3], "rhythm": [2, 1, 1.5, 1.5], "role": "statement"},
        {"degrees": [0, -3, 2, -5], "rhythm": [1, 1.5, 1, 1.5], "role": "answer"},
        {"degrees": [0, -3, 2], "rhythm": [2, 1, 1], "role": "fragment"},
    ],
}

# 四段结构模板：慢燃 / 标准 / 突变。section 只用来轮换模板，不影响随机复现。
PROFILE_ARCS: list[list[dict[str, Any]]] = [
    [
        {"label": "铺垫", "bars": 7, "dynamic": "low", "level": 0.34},
        {"label": "上升", "bars": 6, "dynamic": "medium", "level": 0.66},
        {"label": "高潮", "bars": 5, "dynamic": "high", "level": 0.92},
        {"label": "余韵", "bars": 5, "dynamic": "low", "level": 0.24},
    ],
    [
        {"label": "慢燃", "bars": 9, "dynamic": "low", "level": 0.3},
        {"label": "渐强", "bars": 8, "dynamic": "medium", "level": 0.7},
        {"label": "爆发", "bars": 6, "dynamic": "high", "level": 0.95},
        {"label": "消散", "bars": 7, "dynamic": "low", "level": 0.2},
    ],
    [
        {"label": "骤起", "bars": 3, "dynamic": "low", "level": 0.38},
        {"label": "推进", "bars": 8, "dynamic": "medium", "level": 0.74},
        {"label": "高压", "bars": 5, "dynamic": "high", "level": 0.96},
        {"label": "抽离", "bars": 3, "dynamic": "low", "level": 0.18},
    ],
]


def _stable_seed(profile: str, section: int, seed: str) -> int:
    """曲子身份只由 (种子, 档位, 段落) 决定。

    强度是「演奏力度」，不是「换一首曲子」；如果把它也放进哈希，
    叙事分析每回合轻微波动一次强度，前端就会听到同一首 cue 从头重放。
    """
    digest = hashlib.sha256(f"{seed}|{profile}|{section}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _tempo(profile: str, intensity: str) -> int:
    base = PROFILE_TEMPO.get(profile, 72)
    scale = {"low": 0.92, "medium": 1.0, "high": 1.07}.get(intensity, 1.0)
    return max(30, min(200, int(round(base * scale))))


def _chords(profile: str, base: int, serial: int) -> list[dict[str, Any]]:
    plan = PROFILE_CHORDS.get(profile) or PROFILE_CHORDS["suspense"]
    # 每次调用轻微轮换序位，但不打乱和声色彩的逻辑。
    shift = (serial // 3) % len(plan)
    rotated = plan[shift:] + plan[:shift]
    chords: list[dict[str, Any]] = []
    for offset, quality in rotated[:8]:
        intervals = CHORD_QUALITIES.get(quality) or CHORD_QUALITIES["minor"]
        chords.append({
            "root": (base + offset) % 12,
            "quality": quality,
            "intervals": list(intervals),
        })
    return chords


def build_auto_score(
    *,
    profile: str,
    intensity: str,
    section: int,
    scene: str = "",
    seed: str = "",
) -> dict[str, Any]:
    """生成一份无需 LLM 的完整乐谱。输出可直接交给前端合成器。"""
    safe_profile = profile if profile in PROFILE_MODES else "daily"
    safe_intensity = intensity if intensity in {"low", "medium", "high"} else "medium"
    serial = _stable_seed(safe_profile, int(section), seed)
    base = [0, 2, 5, 7, 9, 3, 6, 10][serial % 8]
    mode = PROFILE_MODES[safe_profile]
    arc = PROFILE_ARCS[(int(section) + serial // 7) % len(PROFILE_ARCS)]
    motifs = PROFILE_MOTIFS.get(safe_profile) or PROFILE_MOTIFS["suspense"]
    pedal = base if safe_profile in {"horror", "suspense", "unease", "awe"} else None

    score: dict[str, Any] = {
        "scene": str(scene or "自动配乐")[:80],
        "mood": safe_profile,
        "confidence": 80,
        "mode": mode,
        "tempo": _tempo(safe_profile, safe_intensity),
        "chords": _chords(safe_profile, base, serial),
        "instrumentation": list(PROFILE_INSTRUMENTS.get(safe_profile, PROFILE_INSTRUMENTS["suspense"])),
        "motif": [int(value) for value in motifs[0]["degrees"]],
        "motif_variants": [
            {
                "degrees": [int(value) for value in item["degrees"]],
                "rhythm": [float(value) for value in item["rhythm"]],
                "role": str(item["role"]),
            }
            for item in motifs
        ],
        "sections": [
            {"label": item["label"], "dynamic": item["dynamic"], "bars": int(item["bars"])}
            for item in arc
        ],
        "dynamics": [round(float(item["level"]), 3) for item in arc],
    }
    if pedal is not None:
        score["pedal"] = int(pedal)
    return score
