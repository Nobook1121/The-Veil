"""反应与豁免的待决中断队列。

反应不是另一个平行系统，而是施法/攻击流程中的一个 ``reaction:<角色>`` 步骤：
- 需要当事人决定时，流程停在 waiting，跨请求可查；
- 提交决定或 TTL 到期后，流程通过续跑器继续结算；
- 被反制的施法由续跑器扣除施法者法术位（不退还）但不产生任何效果。

豁免复用同一套待决队列：策略为 ask/manual 时，每个需要豁免的目标对应一个
``save:<角色>`` 步骤，流程停在 waiting；auto 的目标仍由规则集自动掷骰。

目前接入的是反制法术（counter-spell）、护盾术（shield）与需要玩家决定的豁免。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import (
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTOR_AI,
    EXECUTOR_KP,
    EXECUTOR_PLAYER,
    FLOW_WAITING,
    POLICY_ASK,
    POLICY_MANUAL,
    make_step,
    resolve_policy,
)

REACTION_PREFIX = "reaction:"
SAVE_PREFIX = "save:"


def reaction_step_id(candidate: str) -> str:
    return f"{REACTION_PREFIX}{str(candidate or '').strip()}"


def candidate_of(step_id: str) -> str:
    text = str(step_id or "")
    return text[len(REACTION_PREFIX):] if text.startswith(REACTION_PREFIX) else ""


def save_step_id(target: str) -> str:
    return f"{SAVE_PREFIX}{str(target or '').strip()}"


def save_target_of(step_id: str) -> str:
    text = str(step_id or "")
    return text[len(SAVE_PREFIX):] if text.startswith(SAVE_PREFIX) else ""


def save_steps(
    candidates: Iterable[Mapping[str, Any]],
    *,
    policy_overrides: Mapping[str, Any] | None = None,
    label: str = "豁免",
) -> list[dict[str, Any]]:
    """生成豁免待决步骤；只有策略为 ask/manual 的目标会进入队列。

    auto 的目标不生成步骤，交给规则集在结算时自动掷骰，行为与之前一致。
    """
    steps: list[dict[str, Any]] = []
    for row in candidates:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if str(row.get("agent_id") or "").strip():
            executor = EXECUTOR_AI
        else:
            executor = EXECUTOR_PLAYER if row.get("is_player") else EXECUTOR_KP
        policy = resolve_policy("save", executor, policy_overrides)
        if policy not in {POLICY_ASK, POLICY_MANUAL}:
            continue
        steps.append(
            make_step(
                save_step_id(name),
                f"{name} {label}",
                policy=policy,
                executor=executor,
                default={"decision": "fail", "reason": "save_timeout"},
            )
        )
    return steps


def _reaction_steps(
    reactors: Iterable[Mapping[str, Any]],
    *,
    policy_overrides: Mapping[str, Any] | None = None,
    label: str = "是否反应",
) -> list[dict[str, Any]]:
    """生成反应待决步骤；反制与护盾共用同一套策略与默认值。"""
    steps: list[dict[str, Any]] = []
    for reactor in reactors:
        name = str(reactor.get("name") or "").strip()
        if not name:
            continue
        executor = EXECUTOR_PLAYER if reactor.get("is_player") else EXECUTOR_KP
        steps.append(
            make_step(
                reaction_step_id(name),
                f"{name} {label}",
                policy=resolve_policy("reaction", executor, policy_overrides),
                executor=executor,
                default={"decision": "none", "reason": "reaction_timeout"},
            )
        )
    return steps


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
    save_steps: Iterable[Mapping[str, Any]] = (),
    ttl: int = DEFAULT_TIMEOUT_SECONDS,
    request_key: str = "",
    policy_overrides: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """为一个等待反制或豁免的施法打开流程；返回 ``{flow, error?}``。"""
    engine = WorkflowEngine(room_dir, room_id=room_id)
    reactor_rows = [dict(item) for item in reactors if isinstance(item, Mapping)]
    save_rows = [dict(item) for item in save_steps if isinstance(item, Mapping)]
    if not reactor_rows and not save_rows:
        return {"error": "没有可反应的候选者", "code": "reaction.no_candidates"}
    steps: list[dict[str, Any]] = [
        make_step("validate", "校验射程与目标", policy="auto"),
        *_reaction_steps(reactor_rows, policy_overrides=policy_overrides, label="是否反制"),
        *save_rows,
    ]
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
            "save_targets": [
                save_target_of(str(step.get("id") or "")) for step in save_rows
            ],
        },
        request_key=request_key,
        lock_character=True,
    )
    if error:
        return {"flow": None, **error}
    updated = engine.run_auto(str(flow["id"]), {})
    return {"flow": updated or flow}


def open_attack_window(
    room_dir: Any,
    *,
    room_id: str,
    attacker: str,
    attacker_type: str,
    target: str,
    reactor: Mapping[str, Any],
    action: Mapping[str, Any],
    attack_roll: int,
    attack_rolls: Iterable[Any],
    slot: str = "action",
    ttl: int = DEFAULT_TIMEOUT_SECONDS,
    request_key: str = "",
    policy_overrides: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """命中后、伤害前的护盾术反应窗口。

    复用反制的 ``reaction:<角色>`` 步骤：流程停在 waiting，提交决定或 TTL
    到期后由 ``dnd5e.attack_after_reaction`` 续跑。攻击掷骰在开窗前已冻结，
    无论接受还是拒绝都基于同一颗骰子重算，不会二次掷骰。
    """
    engine = WorkflowEngine(room_dir, room_id=room_id)
    steps: list[dict[str, Any]] = [
        make_step("validate", "校验行动预算", policy="auto"),
        *_reaction_steps([reactor], policy_overrides=policy_overrides, label="是否使用护盾术"),
        make_step("attack", "攻击检定", policy="auto"),
        make_step("damage", "伤害结算", policy="auto"),
    ]
    flow, error, _created = engine.open(
        kind="attack",
        initiator=str(attacker or ""),
        initiator_type=str(attacker_type or EXECUTOR_KP),
        steps=steps,
        ttl=ttl,
        metadata={
            **dict(metadata or {}),
            "continuation": "dnd5e.attack_after_reaction",
            "attacker": str(attacker or ""),
            "target": str(target or ""),
            "reaction": "shield",
            "reaction_slot_level": int(reactor.get("slot_level") or 1),
            "attack_roll": int(attack_roll or 0),
            "attack_rolls": [int(item) for item in (attack_rolls or [])],
            "slot": str(slot or "action"),
            "action": dict(action),
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
    actor_user_id: Any = "",
    agent_id: Any = "",
    is_manager: bool = False,
) -> dict[str, Any]:
    engine = WorkflowEngine(room_dir)
    return engine.submit(
        flow_id,
        reaction_step_id(candidate),
        decision=decision,
        idempotency_key=idempotency_key,
        actor=actor or candidate,
        actor_user_id=actor_user_id,
        agent_id=agent_id,
        is_manager=is_manager,
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
    "SAVE_PREFIX",
    "candidate_of",
    "counter_used",
    "decide",
    "open_attack_window",
    "open_cast_window",
    "pending_for",
    "reaction_step_id",
    "save_step_id",
    "save_steps",
    "save_target_of",
]
