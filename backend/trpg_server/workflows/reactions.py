"""反应待决中断队列。

反应不是另一个平行系统，而是施法/攻击流程中的一个 ``reaction:<角色>`` 步骤：
- 需要当事人决定时，流程停在 waiting，跨请求可查；
- 提交决定或 TTL 到期后，流程通过续跑器继续结算；
- 被反制的施法由续跑器扣除施法者法术位（不退还）但不产生任何效果。

目前接入的是反制法术（counter-spell）。护盾术等其它反应复用同一队列，
只需在触发点插入同名步骤。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import (
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTOR_KP,
    EXECUTOR_PLAYER,
    FLOW_WAITING,
    make_step,
    resolve_policy,
)

REACTION_PREFIX = "reaction:"


def reaction_step_id(candidate: str) -> str:
    return f"{REACTION_PREFIX}{str(candidate or '').strip()}"


def candidate_of(step_id: str) -> str:
    text = str(step_id or "")
    return text[len(REACTION_PREFIX):] if text.startswith(REACTION_PREFIX) else ""


def open_cast_window(
    room_dir: Any,
    *,
    room_id: str,
    caster: str,
    caster_type: str,
    spell: str,
    slot_level: int,
    targets: Iterable[str],
    reactors: Iterable[Mapping[str, Any]],
    ttl: int = DEFAULT_TIMEOUT_SECONDS,
    request_key: str = "",
    policy_overrides: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """为一个等待反制的施法打开流程；返回 ``{flow, error?}``。"""
    engine = WorkflowEngine(room_dir, room_id=room_id)
    reactor_rows = [dict(item) for item in reactors if isinstance(item, Mapping)]
    if not reactor_rows:
        return {"error": "没有可反应的候选者", "code": "reaction.no_candidates"}
    steps: list[dict[str, Any]] = [
        make_step("validate", "校验射程与目标", policy="auto"),
    ]
    for reactor in reactor_rows:
        name = str(reactor.get("name") or "").strip()
        if not name:
            continue
        executor = EXECUTOR_PLAYER if reactor.get("is_player") else EXECUTOR_KP
        steps.append(
            make_step(
                reaction_step_id(name),
                f"{name} 是否反制",
                policy=resolve_policy("reaction", executor, policy_overrides),
                executor=executor,
                default={"decision": "none", "reason": "reaction_timeout"},
            )
        )
    for step_id, label in (
        ("resolve", "法术结算"),
        ("effect", "附加效果"),
        ("concentration", "专注登记"),
    ):
        steps.append(make_step(step_id, label, policy="auto"))
    flow, error, _created = engine.open(
        kind="cast",
        initiator=caster,
        initiator_type=caster_type,
        steps=steps,
        ttl=ttl,
        metadata={
            **dict(metadata or {}),
            "continuation": "dnd5e.cast_after_reaction",
            "caster": caster,
            "spell": spell,
            "slot_level": int(slot_level),
            "targets": [str(name) for name in targets if str(name or "").strip()],
            "reactors": [str(item.get("name") or "") for item in reactor_rows],
        },
        request_key=request_key,
        lock_character=True,
    )
    if error:
        return {"flow": None, **error}
    updated = engine.run_auto(str(flow["id"]), {})
    return {"flow": updated or flow}


def pending_for(room_dir: Any, candidate: str) -> list[dict[str, Any]]:
    """列出某个角色当前需要响应的反应窗口。"""
    engine = WorkflowEngine(room_dir)
    waiting = engine.list(status=FLOW_WAITING, limit=100)
    step_id = reaction_step_id(candidate)
    rows: list[dict[str, Any]] = []
    for flow in waiting:
        for step in flow.get("steps") or []:
            if str(step.get("id")) == step_id and str(step.get("status")) == "waiting":
                rows.append(flow)
                break
    return rows


def decide(
    room_dir: Any,
    flow_id: str,
    candidate: str,
    decision: Any,
    *,
    idempotency_key: str = "",
    actor: str = "",
) -> dict[str, Any]:
    engine = WorkflowEngine(room_dir)
    return engine.submit(
        flow_id,
        reaction_step_id(candidate),
        decision=decision,
        idempotency_key=idempotency_key,
        actor=actor or candidate,
    )


def counter_used(flow: Mapping[str, Any]) -> str:
    """返回选择反制的角色名；没有则返回空串。"""
    for step in flow.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        if not str(step.get("id") or "").startswith(REACTION_PREFIX):
            continue
        result = step.get("result")
        if isinstance(result, Mapping) and str(result.get("decision") or "").lower() == "use":
            return candidate_of(str(step.get("id")))
    return ""


__all__ = [
    "REACTION_PREFIX",
    "candidate_of",
    "counter_used",
    "decide",
    "open_cast_window",
    "pending_for",
    "reaction_step_id",
]
