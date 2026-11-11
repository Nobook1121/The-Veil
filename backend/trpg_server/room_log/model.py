"""把房间消息归约为与渲染器无关的团录文档（设计稿 §3）。

本模块只做纯数据变换：读取 ``info.json`` / ``messages.json``（只读，不写回），
按固定规则切章、映射条目类型、过滤 OOC 与图片，并产出可直接序列化为 JSON
的中间文档。渲染细节全部在 :mod:`trpg_server.room_log.render`。

纯函数化是为了让归约规则可以独立单测：这里不依赖 Flask 上下文，路径与配置
由调用方注入。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from trpg_server.json_store import read_json
from trpg_server.security import safe_join
from trpg_server.settings import DATA_DIR

from .markdown import to_html

SCHEMA_VERSION = 1
MAX_ENTRIES_PER_CHAPTER = 250
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
CJK_OPENING_QUOTES = ("“", "『", "「")
OOC_PREFIXES = ("((", "（（")
_SUCCESS_LABELS = ("大成功", "大失败", "极难成功", "困难成功", "成功", "失败")
_LEVEL_LABELS = {
    "critical": "大成功",
    "fumble": "大失败",
    "extreme": "极难成功",
    "hard": "困难成功",
    "regular": "成功",
    "success": "成功",
    "failure": "失败",
}
_IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_NUMBER_AFTER_EQUALS_RE = re.compile(r"=\s*(-?\d+)")
_BRACKET_NUMBER_RE = re.compile(r"\[(-?\d+)\]")

DEFAULT_OPTIONS: dict[str, Any] = {
    "template": "archive",
    "include_dice": True,
    "include_ooc": True,
    "include_images": True,
    "include_kp_only": False,
    "date_from": None,
    "date_to": None,
    "max_messages": None,
}


def normalize_options(options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """合并默认值并收敛类型，保证渲染/接口拿到同一种形状。"""
    merged = dict(DEFAULT_OPTIONS)
    for key, value in (options or {}).items():
        if key not in merged:
            continue
        merged[key] = value
    merged["template"] = str(merged.get("template") or "archive")
    for key in ("include_dice", "include_ooc", "include_images", "include_kp_only"):
        merged[key] = bool(merged.get(key))
    for key in ("date_from", "date_to"):
        value = merged.get(key)
        merged[key] = str(value).strip() if value else None
    max_messages = merged.get("max_messages")
    if max_messages in (None, "", 0):
        merged["max_messages"] = None
    else:
        try:
            merged["max_messages"] = max(0, int(max_messages))
        except (TypeError, ValueError):
            merged["max_messages"] = None
    return merged


def build_document(
    room_dir: str | Path,
    options: Mapping[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """归约一份房间团录文档（只读）。

    ``data_dir`` 仅在测试或非常规部署时注入；默认使用项目 ``data/``。
    """
    room_path = Path(room_dir)
    info = read_json(room_path / "info.json", default={})
    if not isinstance(info, Mapping):
        info = {}
    messages = read_json(room_path / "messages.json", default=[])
    if not isinstance(messages, list):
        messages = []

    resolved = normalize_options(options)
    data_root = Path(data_dir) if data_dir else DATA_DIR
    cast = _build_cast(info)
    cast_by_user = {str(item["user_id"]): item for item in cast if item.get("user_id") is not None}

    ordered = _ordered_messages(messages)
    filtered = [msg for msg in ordered if _message_passes(msg, resolved)]
    stats: dict[str, Any] = {
        "messages_total": len(messages),
        "messages_exported": 0,
        "messages_truncated": 0,
        "chapters": 0,
        "dice_rolls": 0,
        "images": 0,
        "images_missing": 0,
        "duration_days": _duration_days(ordered),
    }
    max_messages = resolved.get("max_messages")
    if max_messages is not None and len(filtered) > max_messages:
        stats["messages_truncated"] = len(filtered) - max_messages
        filtered = filtered[:max_messages]

    seed_entries = []
    for message in filtered:
        entry = _build_entry(message, cast_by_user, resolved, room_path, data_root, stats)
        if entry is not None:
            seed_entries.append((message, entry))

    chapters = _build_chapters(seed_entries, info)
    stats["messages_exported"] = sum(len(chapter["entries"]) for chapter in chapters)
    stats["chapters"] = len(chapters)
    stats["estimated_pages"] = _estimate_pages(chapters)

    return {
        "schema_version": SCHEMA_VERSION,
        "room": _room_meta(info, room_path),
        "options": resolved,
        "stats": stats,
        "chapters": chapters,
        "cast": cast,
    }


def _ordered_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """按 ``created_at`` 升序；缺失时间戳的条目保持原始相对顺序（稳定排序）。"""
    items = [dict(item) for item in messages if isinstance(item, Mapping)]
    return sorted(items, key=lambda item: str(item.get("created_at") or ""))


def _message_passes(message: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    if options.get("include_kp_only") and str(message.get("type")) not in {"kp", "dice"}:
        return False
    if not options.get("include_dice") and str(message.get("type")) == "dice":
        return False
    if not options.get("include_ooc") and is_ooc(message):
        return False
    date = _date_of(message)
    date_from = options.get("date_from")
    date_to = options.get("date_to")
    if date_from:
        if not date or date < date_from:
            return False
    if date_to:
        if not date or date > date_to:
            return False
    return True


def is_ooc(message: Mapping[str, Any]) -> bool:
    metadata = message.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("ooc") is True:
        return True
    content = str(message.get("content") or "").strip()
    if not content:
        return False
    starts = content.startswith(OOC_PREFIXES)
    ends = content.endswith(("))", "））"))
    return starts and ends


def _date_of(message: Mapping[str, Any]) -> str:
    raw = str(message.get("created_at") or "").strip()
    return raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else ""


def _display_time(message: Mapping[str, Any]) -> str:
    value = str(message.get("time") or "").strip()
    if value:
        return value
    raw = str(message.get("created_at") or "").strip()
    return raw[11:16] if len(raw) >= 16 else ""


def _duration_days(messages: list[Mapping[str, Any]]) -> int:
    dates = sorted({_date_of(item) for item in messages if _date_of(item)})
    if not dates:
        return 0
    try:
        start = datetime.strptime(dates[0], "%Y-%m-%d")
        end = datetime.strptime(dates[-1], "%Y-%m-%d")
    except ValueError:
        return len(dates)
    return max(1, (end - start).days + 1)


def _build_cast(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    cast: list[dict[str, Any]] = []
    for member in info.get("members") or []:
        if not isinstance(member, Mapping):
            continue
        if member.get("is_active", True) is False:
            continue
        card = _parse_character_card(member.get("character_card"))
        cast.append(
            {
                "name": str(member.get("username") or ""),
                "user_id": member.get("user_id"),
                "character": str(card.get("name") or ""),
                "avatar": str(member.get("avatar") or ""),
                "role": str(member.get("room_role") or member.get("role") or "member"),
            }
        )
    return cast


def _parse_character_card(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _room_meta(info: Mapping[str, Any], room_path: Path) -> dict[str, Any]:
    members = [item for item in (info.get("members") or []) if isinstance(item, Mapping)]
    active = [item for item in members if item.get("is_active", True) is not False]
    return {
        "id": str(info.get("id") or room_path.name),
        "name": str(info.get("name") or "未命名房间"),
        "code": str(info.get("room_code") or ""),
        "scenario_title": str(info.get("scenario_title") or ""),
        "system": str(info.get("ruleset_id") or "COC7"),
        "created_at": str(info.get("created_at") or ""),
        "creator_name": str(info.get("creator_name") or ""),
        "player_count": len(active),
    }


def _build_entry(
    message: Mapping[str, Any],
    cast_by_user: Mapping[str, Mapping[str, Any]],
    options: Mapping[str, Any],
    room_dir: Path,
    data_dir: Path,
    stats: dict[str, Any],
) -> dict[str, Any] | None:
    msg_type = str(message.get("type") or "")
    content = str(message.get("content") or "")
    time_text = _display_time(message)

    if msg_type == "kp":
        kind = "dialogue" if _is_dialogue(content) else "narration"
        return {
            "kind": kind,
            "speaker": "KP" if kind == "dialogue" else None,
            "time": time_text,
            "html": to_html(content),
        }

    if msg_type == "player":
        member = cast_by_user.get(str(message.get("sender_id")))
        return {
            "kind": "speaker",
            "speaker": str(message.get("sender_name") or (member or {}).get("name") or "玩家"),
            "character": str((member or {}).get("character") or ""),
            "time": time_text,
            "html": to_html(content),
        }

    if msg_type == "dice":
        entry = {
            "kind": "dice",
            "speaker": str(message.get("sender_name") or "骰娘"),
            "time": time_text,
            "html": to_html(content),
            "roll": _dice_roll(message, content),
        }
        stats["dice_rolls"] += 1
        return entry

    if msg_type == "trigger":
        image = _image_target(message, content)
        if image and options.get("include_images"):
            asset_url, caption = image
            path = resolve_image_path(asset_url, room_dir=room_dir, data_dir=data_dir)
            if path is None:
                stats["images_missing"] += 1
                return _system_entry(time_text, f"[图片缺失] {caption or asset_url}")
            stats["images"] += 1
            return {
                "kind": "image",
                "speaker": str(message.get("sender_name") or "KP"),
                "time": time_text,
                "url": asset_url,
                "caption": caption,
                "available": True,
            }
        if image:
            # 用户关闭了图片：与「缺失」区分，直接丢弃，不计入缺图统计。
            return None
        fallback = content.strip() or str((message.get("metadata") or {}).get("asset_url") or "")
        return _system_entry(time_text, fallback or "[附件]")

    return _system_entry(time_text, content)


def _is_dialogue(content: str) -> bool:
    stripped = content.strip()
    if not stripped or not stripped.startswith(CJK_OPENING_QUOTES):
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    return 0 < len(lines) <= 2


def _system_entry(time_text: str, text: str) -> dict[str, Any]:
    return {"kind": "system", "speaker": None, "time": time_text, "html": to_html(text)}


def _dice_roll(message: Mapping[str, Any], content: str) -> dict[str, Any]:
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    expr = (
        metadata.get("expr")
        or metadata.get("expression")
        or metadata.get("dice")
        or ("d%" if "d%" in content else "1d100")
    )
    total = _first_int(metadata.get("total"), metadata.get("result"), metadata.get("roll"), metadata.get("value"))
    if total is None:
        match = _NUMBER_AFTER_EQUALS_RE.search(content) or _BRACKET_NUMBER_RE.search(content)
        total = int(match.group(1)) if match else None
    judgement = _judgement(metadata, content)
    roll: dict[str, Any] = {"expr": str(expr), "total": total}
    if judgement:
        roll["judgement"] = judgement
    label = metadata.get("name") or metadata.get("label")
    if label:
        roll["label"] = str(label)
    player = metadata.get("player_name")
    if player:
        roll["player"] = str(player)
    return roll


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            return int(value.strip())
    return None


def _judgement(metadata: Mapping[str, Any], content: str) -> str:
    for key in ("judgement", "judgment", "success_label", "result_label"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    level = metadata.get("success_level") or metadata.get("level")
    if isinstance(level, str) and level.strip().lower() in _LEVEL_LABELS:
        return _LEVEL_LABELS[level.strip().lower()]
    success = metadata.get("success")
    if isinstance(success, bool):
        return "成功" if success else "失败"
    for label in _SUCCESS_LABELS:
        if label in content:
            return label
    return ""


def _image_target(message: Mapping[str, Any], content: str) -> tuple[str, str] | None:
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    url = str(metadata.get("asset_url") or metadata.get("assetUrl") or "").strip()
    caption = str(metadata.get("asset_name") or metadata.get("caption") or "").strip()
    if not url:
        match = _IMAGE_MARKDOWN_RE.search(content)
        if match:
            caption = caption or match.group(1).strip()
            url = match.group(2).strip()
    if not url:
        return None
    if Path(url.split("?", 1)[0]).suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return url, caption


def resolve_image_path(
    url: str,
    *,
    room_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> Path | None:
    """把消息里的图片 URL/路径解析为磁盘文件；越界或不存在时返回 ``None``。

    白名单：仅允许 ``data/assets/`` 与本房间目录下的文件。任何 ``..``、盘符
    或其它绝对路径都会被 :func:`trpg_server.security.safe_join` 拒绝。
    """
    raw = str(url or "").strip().split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    if not raw:
        return None
    data_root = Path(data_dir).resolve() if data_dir else Path(DATA_DIR).resolve()
    room_root = Path(room_dir).resolve() if room_dir else None
    assets_root = data_root / "assets"

    candidates: list[tuple[Path, str]] = []
    if raw.startswith("/assets/"):
        candidates.append((assets_root, raw[len("/assets/") :]))
    elif raw.startswith("assets/"):
        candidates.append((assets_root, raw[len("assets/") :]))
    elif raw.startswith("/data/assets/"):
        candidates.append((assets_root, raw[len("/data/assets/") :]))
    elif raw.startswith("data/assets/"):
        candidates.append((assets_root, raw[len("data/assets/") :]))
    elif room_root is not None:
        prefixes = (
            f"/runtime/rooms/{room_root.name}/",
            f"/data/runtime/rooms/{room_root.name}/",
            "/runtime/rooms/",
            "/data/runtime/rooms/",
        )
        for prefix in prefixes:
            if raw.startswith(prefix):
                remainder = raw[len(prefix) :]
                if prefix in ("/runtime/rooms/", "/data/runtime/rooms/"):
                    # 只允许本房间：去掉 room_id 前缀后必须与当前房间同名。
                    head, _, tail = remainder.partition("/")
                    if head == room_root.name and tail:
                        candidates.append((room_root, tail))
                else:
                    candidates.append((room_root, remainder))
                break
        else:
            if not raw.startswith("/"):
                candidates.append((room_root, raw))

    for base, relative in candidates:
        path = _safe_file(base, relative)
        if path is not None and path.is_file():
            return path
    return None


def _safe_file(base: Path, relative: str) -> Path | None:
    relative = relative.lstrip("/")
    if not relative or re.match(r"^[A-Za-z]:", relative):
        return None
    try:
        path = safe_join(base, *[part for part in relative.split("/") if part])
    except (ValueError, TypeError, OSError):
        return None
    base_resolved = base.resolve()
    try:
        path.relative_to(base_resolved)
    except ValueError:
        return None
    return path if path.is_file() else None


def _scene_marker(message: Mapping[str, Any]) -> str:
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    for key in ("scene_id", "chapter"):
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _scene_title(message: Mapping[str, Any]) -> str:
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    for key in ("scene_title", "chapter_title"):
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


def _build_chapters(
    seed_entries: list[tuple[Mapping[str, Any], dict[str, Any]]],
    info: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not seed_entries:
        return []

    use_scene = any(_scene_marker(message) for message, _entry in seed_entries)
    groups: list[list[tuple[Mapping[str, Any], dict[str, Any]]]] = []
    group_keys: list[tuple[str, str]] = []
    for message, entry in seed_entries:
        marker = _scene_marker(message) if use_scene else ""
        key = ("scene", marker) if marker else ("date", _date_of(message))
        if not groups or group_keys[-1] != key:
            groups.append([])
            group_keys.append(key)
        groups[-1].append((message, entry))

    chapters: list[dict[str, Any]] = []
    scene_counter = 0
    for index, (key, items) in enumerate(zip(group_keys, groups)):
        if key[0] == "scene":
            scene_counter += 1
            title = f"第 {scene_counter} 章"
        else:
            title = key[1] or "未知日期"
        subtitle = _scene_title(items[0][0])
        base = {
            "title": title,
            "subtitle": subtitle,
            "started_at": str(items[0][0].get("created_at") or ""),
        }
        if len(items) > MAX_ENTRIES_PER_CHAPTER:
            chunks = [
                items[start : start + MAX_ENTRIES_PER_CHAPTER]
                for start in range(0, len(items), MAX_ENTRIES_PER_CHAPTER)
            ]
            for chunk_index, chunk in enumerate(chunks, start=1):
                chapter = dict(base)
                chapter["index"] = len(chapters) + 1
                chapter["title"] = f"{title}（{_chinese_number(chunk_index)}）"
                chapter["entries"] = [entry for _message, entry in chunk]
                chapters.append(chapter)
        else:
            chapter = dict(base)
            chapter["index"] = len(chapters) + 1
            chapter["entries"] = [entry for _message, entry in items]
            chapters.append(chapter)
    return chapters


def _plain_length(markup: str) -> int:
    return len(re.sub(r"<[^>]+>", "", str(markup or "")))


def _estimate_pages(chapters: list[Mapping[str, Any]]) -> int:
    """粗略估算成品页数（封面 + 目录 + 正文），供前端预览展示。

    正文按每页约 560 个汉字折算，骰子/系统/图片块按等效行数加权。允许有偏差，
    但量级必须接近实际渲染结果（实测 180 条约 45 页，估算约 50 页）。
    """
    if not chapters:
        return 0
    weight = 0
    for chapter in chapters:
        weight += 120  # 章节头占位
        for entry in chapter.get("entries") or []:
            kind = entry.get("kind")
            if kind == "image":
                weight += 1500
            elif kind == "dice":
                weight += 60 + _plain_length(entry.get("html") or "")
            else:
                weight += _plain_length(entry.get("html") or "")
    text_pages = max(1, math.ceil(weight / 560))
    toc_pages = max(1, math.ceil(len(chapters) / 28))
    return 1 + toc_pages + text_pages


def _chinese_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value <= 10:
        return "十" if value == 10 else digits[value]
    if value < 20:
        return "十" + digits[value - 10]
    tens, ones = divmod(value, 10)
    return digits[tens] + "十" + (digits[ones] if ones else "")


__all__ = [
    "DEFAULT_OPTIONS",
    "MAX_ENTRIES_PER_CHAPTER",
    "build_document",
    "is_ooc",
    "normalize_options",
    "resolve_image_path",
]
