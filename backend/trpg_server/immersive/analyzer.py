"""叙事氛围分析：把 KP 的回复确定性映射成配乐档位与配图场景。

设计原则：
- 纯函数、无随机、无网络：同一段文本永远得到同一个结果，便于复现与调试；
- 先看结构化输出（场景/地点），再看关键词与标点强度，最后回落到默认档位；
- 只输出「导演指令」，不生成音频/图片本身，避免把重活放进请求线程。
"""
from __future__ import annotations

import re
from typing import Any

from trpg_server.immersive.config import INTENSITIES, PROFILES

# 关键词权重：命中即累加。中文为主，补充少量英文，覆盖中英混合叙事。
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "horror": (
        "尸体", "血肉", "触手", "尖啸", "嘶吼", "腐", "畸形", "深渊", "邪祟", "不可名状",
        "腥", "内脏", "蠕动", "恐怖", "疯狂", "呓语", "血池", "骸骨", "祭祀", "非人",
        "eldritch", "tentacle", "corpse", "madness", "abyss",
    ),
    "urgency": (
        "逃", "追", "冲", "扑", "枪", "爆炸", "倒计时", "来不及", "立刻", "危险", "警报",
        "崩", "塌", "撞击", "撕开", "奔跑", "失火", "袭击", "断裂", "坠落",
        "run", "chase", "explosion", "alarm", "collapse", "escape",
    ),
    "suspense": (
        "脚步", "阴影", "窥视", "低语", "门缝", "沉默", "屏息", "跟踪", "隐藏", "陌生",
        "窥探", "回音", "不对劲", "屏住", "黑暗中", "缓慢", "试探", "耳语",
        "shadow", "whisper", "footstep", "silence", "lurk",
    ),
    "unease": (
        "寒意", "颤抖", "恶心", "不安", "发冷", "毛骨", "刺痛", "模糊", "失真", "扭曲",
        "压抑", "窒息", "湿冷", "霉味", "锈", "裂痕",
        "unease", "dread", "chill", "wrong",
    ),
    "sorrow": (
        "哭", "泪", "墓", "葬礼", "失去", "告别", "遗憾", "哀", "悼", "遗物", "诀别",
        "沉默地", "空荡", "旧照片", "叹息", "祭奠",
        "grief", "funeral", "farewell", "mourn",
    ),
    "awe": (
        "星空", "穹顶", "巨大", "遗迹", "神殿", "庄严", "宏伟", "敬畏", "光柱", "银河",
        "亘古", "肃穆", "神圣", "浩瀚", "奇观", "光晕", "钟声",
        "cathedral", "cosmic", "majestic", "ancient", "starlight",
    ),
    "aftermath": (
        "废墟", "余烬", "血迹", "残骸", "幸存", "清点", "收拾", "结束", "平息", "烟尘",
        "瓦砾", "断壁", "残局", "善后", "寂静",
        "aftermath", "ruins", "ashes", "survivor", "wreckage",
    ),
}

_DEFAULT_PROFILE = "daily"
_INTENSITY_RANK = {name: index for index, name in enumerate(INTENSITIES)}


def _clamp_text(text: str, limit: int = 2000) -> str:
    # 只看最近一段，避免长历史把当前场景淹没；同时限制正则开销。
    return (text or "")[-limit:]


def _scene_label(text: str, structured: dict[str, Any] | None, state: dict[str, Any] | None) -> str:
    """优先结构化场景/地点，其次状态机里的活动场景，最后取首句。"""
    for source in (structured, state):
        if not isinstance(source, dict):
            continue
        for key in ("next_scene", "scene", "location", "active_scene_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:48]
        if isinstance(source.get("next_scene"), dict):
            label = source["next_scene"].get("title") or source["next_scene"].get("id")
            if isinstance(label, str) and label.strip():
                return label.strip()[:48]
    match = re.search(r"[^。！？!?\n]{2,48}", text)
    return match.group(0).strip() if match else "未知场景"


def _score_profiles(lowered: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    for profile, words in _KEYWORDS.items():
        hits = sum(lowered.count(word) for word in words)
        if hits:
            scores[profile] = hits
    return scores


def _intensity(text: str, profile: str, scores: dict[str, int]) -> str:
    exclamations = text.count("！") + text.count("!") * 2
    ellipses = text.count("…") + text.count("...")
    danger = scores.get("urgency", 0) + scores.get("horror", 0)
    # 短句 + 感叹号 = 紧凑、压迫；长句 + 省略号 = 缓慢、低沉。
    if danger >= 6 or exclamations >= 8:
        return "high"
    if danger >= 3 or exclamations >= 4 or profile in {"horror", "urgency"} and ellipses < 3:
        return "medium"
    if profile in {"daily", "aftermath", "sorrow"} and ellipses >= 3:
        return "low"
    return "medium" if scores.get(profile, 0) >= 3 else "low"


def analyze_narrative(
    text: str,
    *,
    structured: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    default_profile: str = _DEFAULT_PROFILE,
    default_intensity: str = "medium",
) -> dict[str, Any]:
    """返回导演指令：profile / intensity / scene / confidence / reason。"""
    body = _clamp_text(text)
    lowered = body.lower()
    scores = _score_profiles(lowered)
    profile = max(scores, key=scores.get) if scores else default_profile
    if profile not in PROFILES:
        profile = _DEFAULT_PROFILE
    intensity = _intensity(body, profile, scores)
    top = scores.get(profile, 0)
    confidence = "high" if top >= 5 else ("medium" if top >= 2 else "low")
    # 低置信度时回落到默认档位，避免一句无关台词触发恐怖配乐。
    if confidence == "low" and default_profile in PROFILES:
        profile = default_profile
        intensity = default_intensity if default_intensity in INTENSITIES else intensity
    scene = _scene_label(body, structured, state)
    reasons = [f"{profile}:{top}"] if top else ["default"]
    if scores:
        reasons.append("scores=" + ",".join(f"{key}:{value}" for key, value in sorted(scores.items(), key=lambda item: -item[1])))
    return {
        "profile": profile,
        "intensity": intensity,
        "intensity_rank": _INTENSITY_RANK.get(intensity, 1),
        "scene": scene,
        "confidence": confidence,
        "scores": scores,
        "reason": "；".join(reasons),
        "text_length": len(body),
    }
