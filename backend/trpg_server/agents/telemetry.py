from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from trpg_server.agents.token_accounting import merge_segment_totals


USAGE_FILENAME = "ai_usage.jsonl"


def calculate_cache_hit_rate(prompt_tokens: int | float | None, cached_tokens: int | float | None) -> float:
    prompt = max(0.0, float(prompt_tokens or 0))
    cached = max(0.0, min(prompt, float(cached_tokens or 0)))
    return round((cached / prompt) * 100, 2) if prompt else 0.0


def build_provider_cache_key(
    scenario_id: Any,
    scenario_version: Any,
    scene_id: Any,
    scene_version: Any,
    rules_version: Any,
) -> str:
    return (
        f"scenario:{scenario_id}@{scenario_version}:"
        f"scene:{scene_id}@{scene_version}:rules:{rules_version}"
    )


def _usage_path(log_dir: Path) -> Path:
    return Path(log_dir) / USAGE_FILENAME


def record_ai_usage(log_dir: Path, usage: dict[str, Any]) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    item = {str(key): value for key, value in usage.items()}
    item.setdefault("timestamp", now)
    item.setdefault("day", item["timestamp"][:10])
    item.setdefault("agent_id", "unknown")
    item.setdefault("prompt_tokens", 0)
    item.setdefault("completion_tokens", 0)
    item.setdefault("total_tokens", int(item["prompt_tokens"] or 0) + int(item["completion_tokens"] or 0))
    item.setdefault("cached_tokens", 0)
    # 本地（语义/精确）缓存命中没有调用上游，prompt/cached token 都是 0；调用方会显式
    # 传入 cache_hit_rate=100。此时不能再用 0/0 覆盖成 0，否则缓存命中会被记成未命中。
    if "cache_hit_rate" not in item:
        item["cache_hit_rate"] = calculate_cache_hit_rate(item["prompt_tokens"], item["cached_tokens"])
    path = _usage_path(Path(log_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    _record_room_usage(item)
    return item


def _record_room_usage(item: dict[str, Any]) -> None:
    """把带 room_id 的 AI 用量累计到房间用量文件；统计失败不影响主流程。"""
    room_id = item.get("room_id")
    if room_id in (None, ""):
        return
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return
        from trpg_server.room_usage import record_room_usage
        from trpg_server.settings import CONFIG_DIR, ROOMS_DIR

        rooms_dir = Path(current_app.config.get("ROOMS_DIR", ROOMS_DIR))
        config_dir = Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
        record_room_usage(rooms_dir / str(room_id), item, config_dir=config_dir)
    except Exception:
        pass


def load_daily_ai_usage(log_dir: Path, day: str | None = None) -> dict[str, Any]:
    target_day = day or date.today().isoformat()
    roles: dict[str, dict[str, Any]] = {}
    scenario_distribution: dict[str, int] = {}
    prefix_hits = 0
    prefix_requests = 0
    room_costs: dict[str, dict[str, int]] = {}
    ruleset_distribution: dict[str, int] = {}
    retrieval_topics: dict[str, int] = {}
    retrieval_chunks = 0
    retrieval_latency_total = 0.0
    segment_totals: dict[str, int] = {}
    totals: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    # 命中率按「每次请求的命中率取平均」统计：本地缓存命中记为 100%，上游请求按厂商
    # 上报的 cached/prompt 计算。若沿用「总 cached / 总 prompt」，本地命中（token 全为
    # 0）会白白拉低平均，甚至让整体命中率永远显示 0。
    rate_sum = 0.0
    rate_count = 0
    role_rate_sum: dict[str, float] = {}
    path = _usage_path(Path(log_dir))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or str(item.get("day") or str(item.get("timestamp", ""))[:10]) != target_day:
                continue
            agent_id = str(item.get("agent_id") or "unknown")
            bucket = roles.setdefault(
                agent_id,
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "request_count": 0, "prompt_segments": {}},
            )
            for key in totals:
                bucket[key] += int(item.get(key) or 0)
                totals[key] += int(item.get(key) or 0)
            bucket["request_count"] += 1
            # 提示词分段估算：把输入 token 拆到静态前缀/知识库/历史等分段，便于判断
            # 成本结构。汇总到角色桶与全局，两者都由 token_accounting 统一累加。
            merge_segment_totals(bucket["prompt_segments"], item.get("prompt_segments"))
            merge_segment_totals(segment_totals, item.get("prompt_segments"))
            request_rate = float(item.get("cache_hit_rate") or 0)
            rate_sum += request_rate
            rate_count += 1
            role_rate_sum[agent_id] = role_rate_sum.get(agent_id, 0.0) + request_rate
            if item.get("prefix_cache_hit"):
                bucket["prefix_cache_hits"] = bucket.get("prefix_cache_hits", 0) + 1
                prefix_hits += 1
            prefix_requests += 1
            scenario_key = item.get("scenario_id")
            if scenario_key not in (None, ""):
                scenario_distribution[str(scenario_key)] = scenario_distribution.get(str(scenario_key), 0) + 1
            room_id = item.get("room_id")
            if room_id not in (None, ""):
                room_bucket = room_costs.setdefault(str(room_id), {"request_count": 0, "total_tokens": 0})
                room_bucket["request_count"] += 1
                room_bucket["total_tokens"] += int(item.get("total_tokens") or 0)
            for ruleset_id in item.get("ruleset_ids") or []:
                ruleset_distribution[str(ruleset_id)] = ruleset_distribution.get(str(ruleset_id), 0) + 1
            for topic in item.get("retrieval_topics") or []:
                retrieval_topics[str(topic)] = retrieval_topics.get(str(topic), 0) + 1
            retrieval_chunks += int(item.get("retrieval_chunk_count") or 0)
            retrieval_latency_total += float(item.get("retrieval_latency_ms") or 0)
    for agent_id, bucket in roles.items():
        count = bucket["request_count"]
        bucket["cache_hit_rate"] = round(role_rate_sum.get(agent_id, 0.0) / count, 2) if count else 0.0
        bucket["prefix_cache_hit_rate"] = round((bucket.get("prefix_cache_hits", 0) / count) * 100, 2) if count else 0.0
    totals["cache_hit_rate"] = round(rate_sum / rate_count, 2) if rate_count else 0.0
    totals["prefix_cache_hit_rate"] = round((prefix_hits / prefix_requests) * 100, 2) if prefix_requests else 0.0
    return {"day": target_day, **totals, "roles": roles, "scenario_distribution": scenario_distribution, "room_costs": room_costs,
            "ruleset_distribution": ruleset_distribution, "retrieval_topics": retrieval_topics,
            "retrieval_chunk_count": retrieval_chunks, "retrieval_latency_ms": round(retrieval_latency_total, 2),
            "prompt_segments": segment_totals}
