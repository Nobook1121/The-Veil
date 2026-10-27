from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float
    hits: int = 0


class _TTLCache:
    def __init__(self, default_ttl: int = 300):
        self.default_ttl = max(1, int(default_ttl))
        self._entries: dict[str, _Entry] = {}

    def _get(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if not entry:
            return None
        if entry.expires_at <= time.time():
            self._entries.pop(key, None)
            return None
        entry.hits += 1
        return entry

    def get(self, key: str) -> Any:
        entry = self._get(str(key))
        return entry.value if entry else None

    def set(self, key: str, value: Any, ttl: int | None = None) -> Any:
        self._entries[str(key)] = _Entry(value=value, expires_at=time.time() + max(1, int(ttl or self.default_ttl)))
        return value

    def stats(self, key: str) -> dict[str, Any]:
        entry = self._entries.get(str(key))
        return {"hits": entry.hits if entry else 0, "present": bool(self._get(str(key)))}

    def clear(self) -> int:
        """清空全部条目，返回被清除的条目数（含已过期但尚未回收的）。"""

        count = len(self._entries)
        self._entries.clear()
        return count

    def snapshot(self) -> dict[str, Any]:
        """返回当前存活条目数与累计命中数，用于控制台查看缓存状态。"""

        now = time.time()
        live = [entry for entry in self._entries.values() if entry.expires_at > now]
        return {"entries": len(live), "hits": sum(entry.hits for entry in live)}


class ProviderPrefixCache(_TTLCache):
    """Tracks stable provider prefixes; provider remains the source of truth for billing hits."""

    def lookup(self, cache_key: str, ttl: int | None = None) -> bool:
        if self._get(cache_key):
            return True
        self.set(cache_key, True, ttl=ttl)
        return False


class ExactResponseCache(_TTLCache):
    pass


class SemanticCache(_TTLCache):
    @staticmethod
    def _key(intent: str, state: dict[str, Any] | None) -> str:
        fingerprint = hashlib.sha256(
            json.dumps(state or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"{str(intent).strip().casefold()}:{fingerprint}"

    def get(self, intent: str, state: dict[str, Any] | None) -> Any:
        return super().get(self._key(intent, state))

    def set(self, intent: str, state: dict[str, Any] | None, value: Any, ttl: int | None = None) -> Any:
        return super().set(self._key(intent, state), value, ttl=ttl)


# 缓存指纹里必须剔除的易变字段：事件日志/触发记录的随机 id 与时间戳每轮都会变，
# 但它们并不改变「相同输入是否应复用同一回复」的判断。保留它们会让指纹几乎永不重复、
# 本地缓存形同虚设。剔除后只保留真正代表剧情推进的内容字段。
VOLATILE_STATE_KEYS = frozenset({"id", "created_at", "updated_at", "timestamp"})


def stable_state_for_cache(value: Any) -> Any:
    """递归剔除易变字段，得到跨请求稳定的状态投影，专用于生成缓存指纹。

    只作用于缓存键，不改变实际返回给模型/前端的原始状态，因此不会影响 KP 判断。
    """
    if isinstance(value, dict):
        return {
            str(key): stable_state_for_cache(item)
            for key, item in value.items()
            if str(key) not in VOLATILE_STATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [stable_state_for_cache(item) for item in value]
    return value


def build_exact_response_key(
    scenario_id: Any,
    scenario_version: Any,
    scene_id: Any,
    state: dict[str, Any] | None,
    user_input: str,
    variant: str = "",
) -> str:
    state_hash = hashlib.sha256(
        json.dumps(state or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    input_hash = hashlib.sha256(str(user_input or "").encode("utf-8")).hexdigest()[:16]
    key = f"scenario:{scenario_id}@{scenario_version}:scene:{scene_id}:state:{state_hash}:input:{input_hash}"
    return f"{key}:latex:{variant}" if variant else key
