"""标签映射、打分与选曲。

设计目标不是「找一个标签完全匹配的曲子」，而是「按氛围挑一首不出戏、能长时间
循环、且不抢叙事」的曲子，因此打分侧重：

- 许可与署名（硬门槛，交给 ``license`` 模块）；
- 器乐优先（有歌词的曲子会与 KP 旁白抢注意力）；
- 时长要够（>=75s 才不频繁换曲，>=120s 加分）；
- 标签/标题与目标氛围的重合度；
- 最近播放过的曲目降权，避免同一房间连续三段都是同一首。

分数只用于排序，不做「必须超过某阈值才播」——曲库本身就是氛围近似检索，
过严会让外链模式长期空转；是否可播仍由许可与署名硬门槛决定。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

# 中文场景词 → 英文氛围标签。曲库元数据以英文为主，这一步决定检索命中率。
_SCENE_HINTS: dict[str, tuple[str, ...]] = {
    "雨": ("rain", "rainy", "storm"),
    "夜": ("night", "nocturnal"),
    "森林": ("forest", "woods", "nature"),
    "海": ("sea", "ocean", "waves"),
    "海岸": ("coast", "ocean", "waves"),
    "船": ("ship", "sea", "sailing"),
    "山": ("mountain", "nature", "epic"),
    "雪": ("snow", "winter", "cold"),
    "风": ("wind", "storm"),
    "城": ("city", "urban"),
    "街": ("street", "urban", "city"),
    "酒吧": ("bar", "jazz", "noir"),
    "酒馆": ("tavern", "folk", "medieval"),
    "教堂": ("church", "organ", "sacred"),
    "废墟": ("ruins", "desolate", "dark ambient"),
    "洞穴": ("cave", "dark ambient", "drone"),
    "地牢": ("dungeon", "dark ambient"),
    "实验室": ("laboratory", "eerie", "industrial"),
    "医院": ("hospital", "eerie", "dark ambient"),
    "学校": ("school", "nostalgic", "piano"),
    "图书馆": ("library", "quiet", "piano"),
    "火车": ("train", "travel", "rhythmic"),
    "战斗": ("battle", "action", "percussion"),
    "追逐": ("chase", "action", "tense"),
    "仪式": ("ritual", "sacred", "drone"),
    "梦": ("dream", "ethereal", "ambient"),
    "回忆": ("memory", "nostalgic", "piano"),
}

_PROFILE_QUERIES: dict[str, tuple[str, ...]] = {
    "daily": ("calm ambient", "acoustic", "peaceful"),
    "suspense": ("suspense", "mystery", "dark ambient"),
    "unease": ("eerie ambient", "drone", "unsettling"),
    "horror": ("horror ambient", "dark drone", "creepy"),
    "urgency": ("tense action", "driving percussion", "chase"),
    "sorrow": ("melancholy piano", "sad strings", "grief"),
    "awe": ("epic cinematic", "orchestral", "wonder"),
    "aftermath": ("reflective ambient", "aftermath", "sparse piano"),
}

_PROFILE_TAGS: dict[str, tuple[str, ...]] = {
    "daily": ("ambient", "calm", "acoustic", "peaceful"),
    "suspense": ("suspense", "mystery", "dark-ambient", "tension"),
    "unease": ("dark-ambient", "drone", "eerie", "atmospheric"),
    "horror": ("horror", "dark-ambient", "drone", "creepy"),
    "urgency": ("action", "tense", "percussion", "driving"),
    "sorrow": ("melancholy", "sad", "piano", "strings"),
    "awe": ("epic", "orchestral", "cinematic", "wonder"),
    "aftermath": ("ambient", "reflective", "piano", "sparse"),
}

_INTENSITY_TAGS: dict[str, tuple[str, ...]] = {
    "low": ("minimal", "slow", "sparse"),
    "medium": ("ambient",),
    "high": ("intense", "driving", "dramatic"),
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,24}")


def profile_query(profile: str) -> str:
    return " ".join(_PROFILE_QUERIES.get(str(profile or "").strip().lower(), _PROFILE_QUERIES["daily"]))


def profile_tags(profile: str) -> list[str]:
    return list(_PROFILE_TAGS.get(str(profile or "").strip().lower(), _PROFILE_TAGS["daily"]))


def intensity_tags(intensity: str) -> list[str]:
    return list(_INTENSITY_TAGS.get(str(intensity or "").strip().lower(), ("ambient",)))


def scene_tags(scene: str) -> list[str]:
    """从中英文场景文本里抽英文检索标签；中文按关键词表映射。"""
    text = str(scene or "")
    found: list[str] = []
    for word, hints in _SCENE_HINTS.items():
        if word in text:
            for hint in hints:
                if hint not in found:
                    found.append(hint)
    for token in _TOKEN_RE.findall(text.lower()):
        if token not in found:
            found.append(token)
    return found[:10]


def build_tags(profile: str, intensity: str, scene: str, extra: Iterable[str] | None = None) -> list[str]:
    tags = profile_tags(profile) + intensity_tags(intensity) + scene_tags(scene)
    for item in extra or []:
        text = str(item or "").strip().lower()
        if text and text not in tags:
            tags.append(text)
    return tags[:16]


def _track_tokens(track: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for tag in track.get("tags") or []:
        tokens.add(str(tag or "").strip().lower())
    for field in ("title", "artist"):
        for token in _TOKEN_RE.findall(str(track.get(field) or "").lower()):
            tokens.add(token)
    return tokens


def _stable_jitter(track: dict[str, Any]) -> float:
    raw = f"{track.get('provider')}:{track.get('track_id')}".encode("utf-8")
    return (int(hashlib.sha256(raw).hexdigest()[:6], 16) % 1000) / 1000.0 * 0.12


def score_track(
    track: dict[str, Any],
    *,
    tags: Iterable[str],
    recent_ids: Iterable[str] | None = None,
    instrumental_only: bool = True,
) -> float:
    desired = {str(tag or "").strip().lower() for tag in tags if str(tag or "").strip()}
    tokens = _track_tokens(track)
    overlap = len(desired & tokens)
    score = overlap * 0.55

    duration = int(track.get("duration_ms") or 0) / 1000.0
    if duration >= 120:
        score += 0.5
    elif duration >= 75:
        score += 0.3
    elif duration < 30:
        score -= 0.8
    elif duration < 45:
        score -= 0.35

    instrumental = track.get("instrumental")
    if instrumental is True:
        score += 0.35
    elif instrumental is False:
        score -= 0.9 if instrumental_only else 0.2

    provider = str(track.get("provider") or "").lower()
    score += {"jamendo": 0.2, "openverse": 0.0}.get(provider, 0.0)

    key = str(track.get("track_key") or f"{provider}:{track.get('track_id')}")
    if recent_ids and key in set(recent_ids):
        score -= 1.5
    return round(score + _stable_jitter(track), 4)


def dedupe(tracks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一首曲子可能同时出现在两个源；按「标题 + 作者」归一化去重。

    保留先出现的那条（Jamendo 优先），避免同一旋律在交叉淡化里叠两次。
    """
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for track in tracks:
        title = re.sub(r"[^a-z0-9]+", "", str(track.get("title") or "").lower())
        artist = re.sub(r"[^a-z0-9]+", "", str(track.get("artist") or "").lower())
        fingerprint = f"{title}|{artist}"
        if not title or fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(track)
    return result


def rank(
    tracks: Iterable[dict[str, Any]],
    *,
    tags: Iterable[str],
    recent_ids: Iterable[str] | None = None,
    instrumental_only: bool = True,
    limit: int = 10,
) -> list[dict[str, Any]]:
    scored = [
        (score_track(track, tags=tags, recent_ids=recent_ids, instrumental_only=instrumental_only), track)
        for track in tracks
    ]
    scored.sort(key=lambda item: (-item[0], str(item[1].get("track_id") or "")))
    return [track for _, track in scored[: max(1, int(limit))]]
