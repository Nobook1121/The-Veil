"""工作流实体结构、状态常量与默认策略矩阵。

流程 JSON 直接落盘，因此这里只做纯函数的结构归一化，不 import 任何规则集或
Flask 对象。策略按「步骤 × 执行者类型」解析：执行者是谁决定这一步该自动、
询问还是手动。默认矩阵只覆盖两类行为——攻击与伤害自动，需要玩家拍板的
豁免/反应走询问。
"""

from __future__ import annotations

import time
from typing import Any, Mapping
from uuid import uuid4

SCHEMA_VERSION = 1

FLOW_RUNNING = "running"
FLOW_WAITING = "waiting"
FLOW_COMPLETED = "completed"
FLOW_CANCELLED = "cancelled"
FLOW_EXPIRED = "expired"
FLOW_FAILED = "failed"
TERMINAL_STATUSES = frozenset({FLOW_COMPLETED, FLOW_CANCELLED, FLOW_EXPIRED, FLOW_FAILED})

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_WAITING = "waiting"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
TERMINAL_STEP_STATUSES = frozenset({STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED})

POLICY_AUTO = "auto"
POLICY_ASK = "ask"
POLICY_MANUAL = "manual"
POLICIES = (POLICY_AUTO, POLICY_ASK, POLICY_MANUAL)

EXECUTOR_KP = "kp"
EXECUTOR_PLAYER = "player"
EXECUTOR_AI = "ai"
EXECUTOR_TYPES = (EXECUTOR_KP, EXECUTOR_PLAYER, EXECUTOR_AI)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_FLOWS = 100
MAX_EVENTS = 200

# 步骤 → 执行者类型 → 策略。规则集插入新步骤时若未声明，按 auto 处理。
DEFAULT_POLICY: dict[str, dict[str, str]] = {
    "validate": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    "attack": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    "damage": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    "resolve": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    "effect": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    "concentration": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_AUTO, EXECUTOR_AI: POLICY_AUTO},
    # 豁免由目标自己掷；AI/真人玩家都先问一声，KP 控制的目标直接结算。
    "save": {EXECUTOR_KP: POLICY_AUTO, EXECUTOR_PLAYER: POLICY_ASK, EXECUTOR_AI: POLICY_ASK},
    # 反应永远需要当事人确认（护盾术、反制法术等）。
    "reaction": {EXECUTOR_KP: POLICY_ASK, EXECUTOR_PLAYER: POLICY_ASK, EXECUTOR_AI: POLICY_ASK},
}

# 超时未响应时的预设默认决策：反应=不反应，豁免=不通过（继续结算），其余=继续。
DEFAULT_STEP_DEFAULTS: dict[str, Any] = {
    "reaction": {"decision": "none", "reason": "reaction_timeout"},
    "save": {"decision": "fail", "reason": "save_timeout"},
}


def now() -> float:
    return time.time()


def stamp(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch if epoch is not None else now()))


def new_flow_id() -> str:
    return f"flow-{uuid4().hex}"


def resolve_policy(step_id: str, executor_type: str, overrides: Mapping[str, Any] | None = None) -> str:
    """解析步骤策略：房间覆盖 > 默认矩阵 > auto。"""
    step = str(step_id or "").strip()
    executor = str(executor_type or "").strip().lower()
    if executor not in EXECUTOR_TYPES:
        executor = EXECUTOR_KP
    override = (overrides or {}).get(step)
    if isinstance(override, Mapping):
        chosen = str(override.get(executor) or "").strip().lower()
        if chosen in POLICIES:
            return chosen
    defaults = DEFAULT_POLICY.get(step) or {}
    chosen = str(defaults.get(executor) or "").strip().lower()
    return chosen if chosen in POLICIES else POLICY_AUTO


def default_decision(step_id: str) -> dict[str, Any]:
    preset = DEFAULT_STEP_DEFAULTS.get(str(step_id or ""))
    if isinstance(preset, Mapping):
        return dict(preset)
    return {"decision": "continue", "reason": "timeout"}


def make_step(
    step_id: str,
    label: str,
    *,
    policy: str = POLICY_AUTO,
    executor: str = "system",
    default: Any = None,
) -> dict[str, Any]:
    return {
        "id": str(step_id),
        "label": str(label or step_id),
        "policy": policy if policy in POLICIES else POLICY_AUTO,
        "executor": str(executor or "system"),
        "status": STEP_PENDING,
        "default": default if default is not None else default_decision(step_id),
        "result": None,
        "error": "",
        "started_at": "",
        "finished_at": "",
    }


def new_flow(
    *,
    kind: str,
    room_id: str,
    initiator: str,
    initiator_type: str,
    steps: list[dict[str, Any]],
    ttl: int = DEFAULT_TIMEOUT_SECONDS,
    metadata: Mapping[str, Any] | None = None,
    request_key: str = "",
) -> dict[str, Any]:
    created = now()
    clean_steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").strip()
        if not step_id or step_id in seen:
            continue
        seen.add(step_id)
        clean_steps.append(
            {
                **_normalize_step(step),
                "id": step_id,
            }
        )
    timeout = max(1, int(ttl or DEFAULT_TIMEOUT_SECONDS))
    flow = {
        "schema": SCHEMA_VERSION,
        "id": new_flow_id(),
        "kind": str(kind or "action"),
        "room_id": str(room_id or ""),
        "initiator": str(initiator or ""),
        "initiator_type": str(initiator_type or EXECUTOR_KP).lower(),
        "status": FLOW_RUNNING,
        "created_at": stamp(created),
        "updated_at": stamp(created),
        "created_epoch": created,
        "expires_at": created + timeout,
        "ttl": timeout,
        "metadata": dict(metadata or {}),
        "request_key": str(request_key or ""),
        "steps": clean_steps,
        "side_effects": {},
        "submissions": {},
        "events": [],
        "timed_out": False,
        "continuation_result": None,
        "continuation_error": "",
    }
    return flow


def _normalize_step(raw: Mapping[str, Any]) -> dict[str, Any]:
    step_id = str(raw.get("id") or "")
    policy = str(raw.get("policy") or POLICY_AUTO).strip().lower()
    return {
        "id": step_id,
        "label": str(raw.get("label") or step_id),
        "policy": policy if policy in POLICIES else POLICY_AUTO,
        "executor": str(raw.get("executor") or "system"),
        "status": str(raw.get("status") or STEP_PENDING),
        "default": raw.get("default") if isinstance(raw.get("default"), Mapping) else default_decision(step_id),
        "result": raw.get("result"),
        "error": str(raw.get("error") or ""),
        "started_at": str(raw.get("started_at") or ""),
        "finished_at": str(raw.get("finished_at") or ""),
    }


def normalize_flow(raw: Any) -> dict[str, Any] | None:
    """读取时兜底修复；结构不可识别返回 ``None``。"""
    if not isinstance(raw, Mapping):
        return None
    flow_id = str(raw.get("id") or "").strip()
    if not flow_id:
        return None
    steps = [
        _normalize_step(step)
        for step in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])
        if isinstance(step, Mapping)
    ]
    try:
        expires_at = float(raw.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    try:
        ttl = max(1, int(raw.get("ttl") or DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TIMEOUT_SECONDS
    flow = {
        "schema": int(raw.get("schema") or SCHEMA_VERSION),
        "id": flow_id,
        "kind": str(raw.get("kind") or "action"),
        "room_id": str(raw.get("room_id") or ""),
        "initiator": str(raw.get("initiator") or ""),
        "initiator_type": str(raw.get("initiator_type") or EXECUTOR_KP),
        "status": str(raw.get("status") or FLOW_RUNNING),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "created_epoch": float(raw.get("created_epoch") or 0),
        "expires_at": expires_at,
        "ttl": ttl,
        "metadata": dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else {},
        "request_key": str(raw.get("request_key") or ""),
        "steps": steps,
        "side_effects": dict(raw.get("side_effects")) if isinstance(raw.get("side_effects"), Mapping) else {},
        "submissions": dict(raw.get("submissions")) if isinstance(raw.get("submissions"), Mapping) else {},
        "events": [item for item in (raw.get("events") or []) if isinstance(item, Mapping)][-MAX_EVENTS:],
        "timed_out": bool(raw.get("timed_out")),
        "continuation_result": raw.get("continuation_result"),
        "continuation_error": str(raw.get("continuation_error") or ""),
    }
    return flow


def all_steps_settled(flow: Mapping[str, Any]) -> bool:
    steps = flow.get("steps") if isinstance(flow.get("steps"), list) else []
    return bool(steps) and all(str(step.get("status")) in TERMINAL_STEP_STATUSES for step in steps)


def next_pending_step(flow: Mapping[str, Any]) -> dict[str, Any] | None:
    for step in flow.get("steps") or []:
        if isinstance(step, dict) and str(step.get("status")) == STEP_PENDING:
            return step
    return None


__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_TIMEOUT_SECONDS",
    "EXECUTOR_AI",
    "EXECUTOR_KP",
    "EXECUTOR_PLAYER",
    "EXECUTOR_TYPES",
    "FLOW_CANCELLED",
    "FLOW_COMPLETED",
    "FLOW_EXPIRED",
    "FLOW_FAILED",
    "FLOW_RUNNING",
    "FLOW_WAITING",
    "MAX_EVENTS",
    "MAX_FLOWS",
    "POLICIES",
    "POLICY_ASK",
    "POLICY_AUTO",
    "POLICY_MANUAL",
    "SCHEMA_VERSION",
    "STEP_COMPLETED",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_RUNNING",
    "STEP_SKIPPED",
    "STEP_WAITING",
    "all_steps_settled",
    "default_decision",
    "make_step",
    "new_flow",
    "new_flow_id",
    "next_pending_step",
    "normalize_flow",
    "now",
    "resolve_policy",
    "stamp",
]
