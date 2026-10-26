"""触发消息组装与发布（§5.8.2 / §5.8.4）。

不直接写 messages.json：工具返回的 `direct_messages` 由既有聊天链路持久化，
与旧 `direct_message` 共用同一套前端渲染与去重入口。
"""
from __future__ import annotations

from typing import Any, Mapping

from trpg_server.agents.narrative.resolver import TriggerHit

MAX_MESSAGES_PER_TURN = 3


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _triggered_by(binding: Mapping[str, Any] | None) -> dict[str, Any]:
    binding = binding if isinstance(binding, Mapping) else {}
    result: dict[str, Any] = {}
    for key in (
        "location_id",
        "object_id",
        "interaction_id",
        "check_id",
        "skill",
        "difficulty",
        "result",
        "event_id",
        "clue_id",
        "npc_id",
    ):
        value = _text(binding.get(key))
        if value:
            result[key] = value
    return result


def _message_content(specs: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for spec in specs:
        content = _text(spec.get("content"))
        if content and content not in parts:
            parts.append(content)
    for attachment in attachments:
        caption = _text(attachment.get("caption"))
        if caption and caption not in parts:
            parts.append(caption)
    if not parts:
        for spec in specs:
            label = _text(spec.get("label"))
            if label and label not in parts:
                parts.append(label)
    return "\n\n".join(parts)


def _message(
    specs: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    *,
    binding: Mapping[str, Any] | None,
    source: str,
    audience: str,
) -> dict[str, Any]:
    ordered = sorted(attachments, key=lambda item: _int(item.get("order"), 0))
    for index, attachment in enumerate(ordered):
        attachment["order"] = index
    first = specs[0]
    trigger_ids = [spec["id"] for spec in specs]
    spoiler = max((_int(spec.get("spoiler_level"), 0) for spec in specs), default=0)
    return {
        "type": "trigger",
        "sender_id": None,
        "sender_name": "KP",
        "avatar": "",
        "content": _message_content(specs, ordered) or "你注意到了一些资料。",
        "metadata": {
            "trigger_id": first["id"],
            "trigger_name": first.get("label") or first["id"],
            "trigger_group": _text(first.get("group")),
            "trigger_ids": trigger_ids,
            "visibility": _text(first.get("visibility")) or "player_visible",
            "spoiler_level": spoiler,
            "reveal_source": source,
            "audience": audience,
            "triggered_by": _triggered_by(binding),
            "attachments": ordered,
        },
    }


def publish_hits(
    hits: list[TriggerHit],
    *,
    binding: Mapping[str, Any] | None = None,
    source: str = "auto",
    audience: str = "player",
    max_messages: int = MAX_MESSAGES_PER_TURN,
) -> list[dict[str, Any]]:
    """按 priority 排序、同 group 合并，并折叠超过上限的资料卡。"""
    if not hits:
        return []
    ordered = sorted(hits, key=lambda hit: (-hit.priority, hit.trigger_id))
    groups: list[list[TriggerHit]] = []
    by_group: dict[str, list[TriggerHit]] = {}
    for hit in ordered:
        group = hit.group
        if not group:
            groups.append([hit])
            continue
        if group not in by_group:
            by_group[group] = []
            groups.append(by_group[group])
        by_group[group].append(hit)

    messages: list[dict[str, Any]] = []
    for group_hits in groups:
        specs = [hit.spec for hit in group_hits]
        attachments: list[dict[str, Any]] = []
        for hit in group_hits:
            attachments.extend(dict(item) for item in hit.attachments)
        messages.append(
            _message(
                specs,
                attachments,
                binding=binding,
                source=source,
                audience=audience,
            )
        )

    limit = max(1, _int(max_messages, MAX_MESSAGES_PER_TURN))
    if len(messages) <= limit:
        return messages
    collapsed = messages[limit:]
    kept = messages[:limit]
    remaining = sum(len((item.get("metadata") or {}).get("trigger_ids") or []) for item in collapsed)
    kept.append({
        "type": "trigger",
        "sender_id": None,
        "sender_name": "KP",
        "avatar": "",
        "content": f"还有 {max(1, remaining)} 份资料。",
        "metadata": {
            "trigger_id": "",
            "trigger_name": "更多资料",
            "trigger_group": "",
            "trigger_ids": [
                trigger_id
                for item in collapsed
                for trigger_id in (item.get("metadata") or {}).get("trigger_ids") or []
            ],
            "visibility": "player_visible",
            "spoiler_level": 0,
            "reveal_source": source,
            "audience": audience,
            "collapsed_count": max(1, remaining),
            "attachments": [],
        },
    })
    return kept


__all__ = ["MAX_MESSAGES_PER_TURN", "publish_hits"]
