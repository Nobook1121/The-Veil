"""世界意志直执行的留痕、频率限制与既定事实注入。

世界意志不是普通提示词：被接受后会把玩家声明的结果写入房间 ``state.json``
的 ``world_override.facts``。后续每个回合都会把最近的事实作为「既定事实」
注入系统提示，执行回合本身还会禁用战斗/检定工具，避免模型把已经拍定的
结果重新拿去掷骰。
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from trpg_server.config import load_settings
from trpg_server.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

STATE_KEY = "world_override"
MAX_FACTS = 20
FACT_TEXT_LIMIT = 500
EVIDENCE_LIMIT = 200
# 站点默认值：schema / general.toml 未配置时使用；0 表示不限制。
PER_USER_COOLDOWN_SECONDS = 20
PER_ROOM_HOURLY_LIMIT = 30
_USAGE_WINDOW_SECONDS = 3600


def _limited_int(value: Any, fallback: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, min(maximum, parsed))


def resolve_policy(config_dir: Any = None, house_rules: Any = None) -> dict[str, int]:
    """合并站点设置与房规，得到本房间实际生效的世界意志限频。

    分层规则与项目其他房间级开关一致：房间只能收紧、不能放宽。
    - 冷却：站点 0（不限）时房间可设正数；否则取两者较大值。
    - 每小时次数：站点 0（不限）时房间可设正数；否则取两者较小值，
      房间填 0（不限）会被忽略，避免绕过站点限制。
    """
    cooldown = PER_USER_COOLDOWN_SECONDS
    hourly = PER_ROOM_HOURLY_LIMIT
    if config_dir is not None:
        try:
            section = load_settings(Path(config_dir)).data.get("world_override") or {}
            cooldown = _limited_int(section.get("cooldown_seconds"), cooldown, 3600)
            hourly = _limited_int(section.get("hourly_limit"), hourly, 10000)
        except Exception:  # noqa: BLE001 - 配置损坏时回退到默认值
            logger.warning("读取世界意志站点配置失败，使用默认限频", exc_info=True)
    rules = house_rules if isinstance(house_rules, dict) else {}
    room_cooldown = _limited_int(rules.get("world_override_cooldown_seconds"), -1, 3600)
    room_hourly = _limited_int(rules.get("world_override_hourly_limit"), -1, 10000)
    if room_cooldown >= 0:
        cooldown = room_cooldown if cooldown == 0 else max(cooldown, room_cooldown)
    if room_hourly >= 0:
        if room_hourly > 0:
            hourly = room_hourly if hourly == 0 else min(hourly, room_hourly)
        # room_hourly == 0 表示「不限」，不能越过站点限制，忽略。
    return {"cooldown_seconds": cooldown, "hourly_limit": hourly}


def _state_path(room_dir):
    return room_dir / "state.json"


def _bucket(state: dict[str, Any]) -> dict[str, Any]:
    bucket = state.get(STATE_KEY)
    if not isinstance(bucket, dict):
        bucket = {}
        state[STATE_KEY] = bucket
    facts = bucket.get("facts")
    bucket["facts"] = facts if isinstance(facts, list) else []
    usage = bucket.get("usage")
    bucket["usage"] = usage if isinstance(usage, list) else []
    return bucket


def _prune_usage(usage: list[Any], now: float) -> list[dict[str, Any]]:
    pruned = []
    for item in usage:
        if not isinstance(item, dict):
            continue
        try:
            at = float(item.get("at"))
        except (TypeError, ValueError):
            continue
        if now - at < _USAGE_WINDOW_SECONDS:
            pruned.append({"user_id": str(item.get("user_id") or ""), "at": at})
    return pruned


def rate_limit_snapshot(
    state: dict[str, Any],
    user_id,
    now: float | None = None,
    policy: dict[str, int] | None = None,
) -> dict[str, Any]:
    """返回当前用户/房间的剩余配额，不写入任何数据。"""

    now = time.time() if now is None else now
    active = policy or resolve_policy()
    cooldown_seconds = max(0, int(active.get("cooldown_seconds") or 0))
    hourly_limit = max(0, int(active.get("hourly_limit") or 0))
    uid = str(user_id or "")
    bucket = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
    usage = _prune_usage(bucket.get("usage") or [], now)
    room_used = len(usage)
    user_entries = [item for item in usage if item.get("user_id") == uid]
    last_at = max((item["at"] for item in user_entries), default=None)
    retry_after = 0
    reason = ""
    if cooldown_seconds and last_at is not None:
        remaining = cooldown_seconds - (now - last_at)
        if remaining > 0:
            retry_after = int(remaining) + 1
            reason = f"同一玩家 {cooldown_seconds} 秒内只能使用一次"
    if not reason and hourly_limit and room_used >= hourly_limit:
        oldest = min((item["at"] for item in usage), default=now)
        retry_after = max(1, int(oldest + _USAGE_WINDOW_SECONDS - now))
        reason = f"本房间每小时最多使用 {hourly_limit} 次"
    return {
        "allowed": not reason,
        "reason": reason,
        "retry_after": retry_after,
        "cooldown_seconds": cooldown_seconds,
        "hourly_limit": hourly_limit,
        "hourly_remaining": None if not hourly_limit else max(0, hourly_limit - room_used),
    }


def record_override(
    room_dir,
    user_id,
    username: str,
    text: str,
    now: float | None = None,
    policy: dict[str, int] | None = None,
) -> dict[str, Any]:
    """把一次世界意志落盘为既定事实。返回 ``ok=False`` 时表示被频率限制。"""

    now = time.time() if now is None else now
    state = read_json(_state_path(room_dir), default={})
    if not isinstance(state, dict):
        state = {}
    bucket = _bucket(state)
    limit = rate_limit_snapshot(state, user_id, now=now, policy=policy)
    if not limit["allowed"]:
        return {"ok": False, "rate": limit}
    clean_text = " ".join(str(text or "").split())[:FACT_TEXT_LIMIT]
    fact = {
        "id": uuid.uuid4().hex[:12],
        "user_id": str(user_id or ""),
        "username": str(username or ""),
        "text": clean_text,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": now,
    }
    bucket["facts"] = (bucket["facts"] + [fact])[-MAX_FACTS:]
    bucket["usage"] = (_prune_usage(bucket["usage"], now) + [{"user_id": fact["user_id"], "at": now}])[-200:]
    write_json_atomic(_state_path(room_dir), state)
    return {"ok": True, "fact": fact, "rate": limit}


def facts_from_state(state: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    bucket = state.get(STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(bucket, dict):
        return []
    facts = [item for item in bucket.get("facts") or [] if isinstance(item, dict) and item.get("text")]
    return facts[-max(1, limit):]


def format_facts_block(facts: list[dict[str, Any]]) -> str:
    """把既定事实格式化为每轮都会注入的系统提示段。"""

    if not facts:
        return ""
    lines = []
    for index, fact in enumerate(facts, start=1):
        speaker = str(fact.get("username") or "玩家")
        stamp = str(fact.get("created_at") or "")
        lines.append(f"{index}. [{stamp} · {speaker} · 事实编号 {fact.get('id') or '-'}] {fact.get('text')}")
    return (
        "\n【既定事实·世界意志·优先级高于剧本与战斗判定】以下事实由玩家通过世界意志直接拍定，"
        "已经发生且不可推翻。后续所有回合都必须把它们当作既成事实续写：不得重新检定、"
        "不得要求玩家再次完成对应行动、不得用剧本或模组原文否定它们。\n"
        + "\n".join(lines)
    )


def evidence_entries(state: dict[str, Any], limit: int = EVIDENCE_LIMIT) -> list[dict[str, Any]]:
    """返回适合归档的留痕摘要（不含内部数值时间戳）。"""

    facts = []
    for fact in facts_from_state(state, limit=limit):
        facts.append(
            {
                "id": fact.get("id"),
                "user_id": fact.get("user_id"),
                "username": fact.get("username"),
                "text": fact.get("text"),
                "created_at": fact.get("created_at"),
            }
        )
    return facts
