"""LLM 音乐导演的乐谱 schema 与严格校验。

导演只输出「乐谱参数」而不是音频：前端 Web Audio 引擎按谱合成，因此：

- 所有字段都必须可枚举/可数值化，任何越界、缺字段或类型错误都判为非法；
- 非法或低置信输出一律回退规则分析器，绝不把半截 JSON 送进播放链路；
- 校验后只保留白名单字段，避免模型夹带的未知键影响前端渲染。

字段含义：
``mood`` 影响前端音色与滤波器性格，``mode`` / ``chords`` / ``motif`` 决定旋律与和声，
``tempo`` 决定小节时长，``instrumentation`` 是配器白名单，``sections`` 描述段落推进
与动态起伏，``dynamics`` 是逐段响度曲线（0~1）。
"""
from __future__ import annotations

from typing import Any

DIRECTOR_MODES: tuple[str, ...] = (
    "major",
    "minor",
    "dorian",
    "phrygian",
    "lydian",
    "mixolydian",
    "harmonic_minor",
    "whole_tone",
    "chromatic",
)

CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "major": (0, 4, 7),
    "minor": (0, 3, 7),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "power": (0, 7),
}

INSTRUMENTS: tuple[str, ...] = (
    "pad",
    "bass",
    "arp",
    "pulse",
    "bells",
    "drone",
    "strings",
    "choir",
    "noise",
)

DIRECTOR_INTENSITIES: tuple[str, ...] = ("low", "medium", "high")
MOODS: tuple[str, ...] = (
    "daily", "suspense", "unease", "horror",
    "urgency", "sorrow", "awe", "aftermath",
)

MIN_SECTIONS = 1
MAX_SECTIONS = 4
MIN_CHORDS = 2
MAX_CHORDS = 8
MIN_MOTIF = 2
MAX_MOTIF = 8
MIN_DYNAMICS = 2
MAX_DYNAMICS = 8


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _valid_int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum <= value <= maximum:
        return value
    return None


def _valid_float(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if minimum <= number <= maximum:
        return number
    return None


def validate_score(raw: Any, *, min_confidence: int = 0) -> tuple[dict[str, Any] | None, list[str], int]:
    """校验并归一化导演输出。

    返回 ``(score, errors, confidence)``：``score`` 为 ``None`` 时调用方必须回退。
    ``min_confidence`` 由站点设置下发；置信度低于阈值同样视为不可用（但不置位
    ``errors``，让调用方把「低置信」与「schema 非法」分别记录）。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["输出不是 JSON 对象"], 0

    scene = _text(raw.get("scene"), 80)
    if not scene:
        errors.append("scene 不能为空")

    mood = str(raw.get("mood") or "").strip().lower()
    if mood not in MOODS:
        errors.append(f"mood 必须是 {list(MOODS)}")

    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in DIRECTOR_MODES:
        errors.append(f"mode 必须是 {list(DIRECTOR_MODES)}")

    tempo = _valid_int(raw.get("tempo"), 30, 200)
    if tempo is None:
        errors.append("tempo 必须是 30-200 的整数")

    chords: list[dict[str, Any]] = []
    raw_chords = raw.get("chords")
    if not isinstance(raw_chords, list) or not (MIN_CHORDS <= len(raw_chords) <= MAX_CHORDS):
        errors.append(f"chords 必须是 {MIN_CHORDS}-{MAX_CHORDS} 个和弦的数组")
    else:
        for index, item in enumerate(raw_chords):
            if not isinstance(item, dict):
                errors.append(f"chords[{index}] 必须是对象")
                continue
            root = _valid_int(item.get("root"), 0, 11)
            quality = str(item.get("quality") or "").strip().lower()
            if root is None:
                errors.append(f"chords[{index}].root 必须是 0-11 的整数")
            if quality not in CHORD_QUALITIES:
                errors.append(f"chords[{index}].quality 必须是 {list(CHORD_QUALITIES)}")
            if root is not None and quality in CHORD_QUALITIES:
                chords.append({
                    "root": root,
                    "quality": quality,
                    "intervals": list(CHORD_QUALITIES[quality]),
                })

    instrumentation: list[str] = []
    raw_instruments = raw.get("instrumentation")
    if not isinstance(raw_instruments, list) or not (1 <= len(raw_instruments) <= 6):
        errors.append("instrumentation 必须是 1-6 个乐器的数组")
    else:
        for item in raw_instruments:
            name = str(item or "").strip().lower()
            if name not in INSTRUMENTS:
                errors.append(f"instrumentation 含未知乐器 {name!r}")
            elif name not in instrumentation:
                instrumentation.append(name)

    motif: list[int] = []
    raw_motif = raw.get("motif")
    if not isinstance(raw_motif, list) or not (MIN_MOTIF <= len(raw_motif) <= MAX_MOTIF):
        errors.append(f"motif 必须是 {MIN_MOTIF}-{MAX_MOTIF} 个音级的数组")
    else:
        for index, item in enumerate(raw_motif):
            note = _valid_int(item, -12, 24)
            if note is None:
                errors.append(f"motif[{index}] 必须是 -12~24 的整数")
            else:
                motif.append(note)

    sections: list[dict[str, Any]] = []
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not (MIN_SECTIONS <= len(raw_sections) <= MAX_SECTIONS):
        errors.append(f"sections 必须是 {MIN_SECTIONS}-{MAX_SECTIONS} 个段落的数组")
    else:
        for index, item in enumerate(raw_sections):
            if not isinstance(item, dict):
                errors.append(f"sections[{index}] 必须是对象")
                continue
            label = _text(item.get("label") or f"段落{index + 1}", 32)
            dynamic = str(item.get("dynamic") or "").strip().lower()
            bars = _valid_int(item.get("bars"), 2, 16)
            if dynamic not in DIRECTOR_INTENSITIES:
                errors.append(f"sections[{index}].dynamic 必须是 {list(DIRECTOR_INTENSITIES)}")
            if bars is None:
                errors.append(f"sections[{index}].bars 必须是 2-16 的整数")
            if dynamic in DIRECTOR_INTENSITIES and bars is not None:
                sections.append({"label": label, "dynamic": dynamic, "bars": bars})

    dynamics: list[float] = []
    raw_dynamics = raw.get("dynamics")
    if not isinstance(raw_dynamics, list) or not (MIN_DYNAMICS <= len(raw_dynamics) <= MAX_DYNAMICS):
        errors.append(f"dynamics 必须是 {MIN_DYNAMICS}-{MAX_DYNAMICS} 个 0~1 数值的数组")
    else:
        for index, item in enumerate(raw_dynamics):
            level = _valid_float(item, 0.0, 1.0)
            if level is None:
                errors.append(f"dynamics[{index}] 必须是 0~1 的数值")
            else:
                dynamics.append(round(level, 3))

    confidence_raw = raw.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        errors.append("confidence 必须是 0-100 的整数")
        confidence = 0
    else:
        confidence = max(0, min(100, int(confidence_raw)))

    if errors:
        return None, errors, confidence

    score = {
        "scene": scene,
        "mood": mood,
        "mode": mode,
        "tempo": tempo,
        "chords": chords,
        "instrumentation": instrumentation,
        "motif": motif,
        "sections": sections,
        "dynamics": dynamics,
        "confidence": confidence,
    }
    if confidence < max(0, min(100, int(min_confidence))):
        return None, ["low_confidence"], confidence
    return score, [], confidence


def fallback_payload(error: str, *, confidence: int = 0) -> dict[str, Any]:
    """统一的回退描述，供日志与接口返回使用。"""
    return {"accepted": False, "error": str(error or "")[:180], "confidence": confidence}
