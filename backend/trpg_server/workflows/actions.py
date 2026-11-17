"""施法与攻击的统一工作流入口。

工具调用、房间级施法、战斗 REST 都从这里创建一个流程，再交给
:class:`WorkflowEngine` 推进。步骤策略由 :func:`workflow_action_steps` 按
「步骤 × 执行者类型」生成；攻击与伤害默认自动，豁免/反应默认询问。

调用方先完成所有前置校验（射程、目标限制、行动预算、墙体/视线），把真正
改状态的闭包作为 handler 传进来。这样 AI 与真人共用同一条校验链，任何一侧
都无法绕过。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import (
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTOR_AI,
    EXECUTOR_KP,
    EXECUTOR_PLAYER,
    make_step,
    resolve_policy,
)
STEP_VALIDATE = "validate"
STEP_CAST = "cast"
STEP_RESOLVE = "resolve"
STEP_ATTACK = "attack"
STEP_DAMAGE = "damage"
STEP_EFFECT = "effect"
STEP_CONCENTRATION = "concentration"
STEP_SAVE = "save"
STEP_REACTION = "reaction"

# 流程类型 → 步骤 id 与显示名。
ACTION_STEPS: dict[str, tuple[tuple[str, str], ...]] = {
    "cast": (
        (STEP_VALIDATE, "校验射程与目标"),
        (STEP_RESOLVE, "法术结算"),
        (STEP_EFFECT, "附加效果"),
        (STEP_CONCENTRATION, "专注登记"),
    ),
    "attack": (
        (STEP_VALIDATE, "校验行动预算"),
        (STEP_ATTACK, "攻击检定"),
        (STEP_DAMAGE, "伤害结算"),
    ),
}


def executor_type_for(*, is_player: bool = False, agent_id: str = "") -> str:
    """执行者类型：真人玩家 / AI 玩家 / KP 或系统。"""
    if is_player:
        return EXECUTOR_AI if str(agent_id or "").strip() else EXECUTOR_PLAYER
    return EXECUTOR_KP


def action_steps(
    kind: str,
    *,
    executor_type: str,
    overrides: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按默认矩阵生成步骤；``overrides`` 来自房间自动化配置。"""
    definitions = ACTION_STEPS.get(str(kind or "").strip().lower())
    if not definitions:
        return []
    steps: list[dict[str, Any]] = []
    for step_id, label in definitions:
        steps.append(
            make_step(
                step_id,
                label,
                policy=resolve_policy(step_id, executor_type, overrides),
                executor=executor_type if step_id in {STEP_SAVE, STEP_REACTION} else "system",
            )
        )
    return steps


def extra_step(
    step_id: str,
    label: str,
    *,
    executor_type: str,
    overrides: Mapping[str, Any] | None = None,
    default: Any = None,
) -> dict[str, Any]:
    """给流程追加一个目标级步骤（如每个目标一次豁免）。"""
    return make_step(
        step_id,
        label,
        policy=resolve_policy(step_id, executor_type, overrides),
        executor=executor_type,
        default=default,
    )


def run_flow_action(
    room_dir: Any,
    *,
    kind: str,
    initiator: str,
    initiator_type: str,
    steps: list[dict[str, Any]],
    handlers: Mapping[str, Callable[[dict[str, Any], dict[str, Any]], Any]],
    room_id: str = "",
    request_key: str = "",
    ttl: int = DEFAULT_TIMEOUT_SECONDS,
    metadata: Mapping[str, Any] | None = None,
    lock_character: bool = True,
) -> dict[str, Any]:
    """打开并推进一个流程；返回 ``{flow, duplicate, error?}``。"""
    engine = WorkflowEngine(room_dir, room_id=room_id)
    overrides = engine.policy()
    resolved_steps: list[dict[str, Any]] = []
    for step in steps:
        executor = str(step.get("executor") or "").strip().lower()
        if executor not in {EXECUTOR_KP, EXECUTOR_PLAYER, EXECUTOR_AI}:
            executor = initiator_type
        resolved_steps.append(
            {
                **step,
                "policy": resolve_policy(str(step.get("id") or ""), executor, overrides),
            }
        )
    flow, error, created = engine.open(
        kind=kind,
        initiator=initiator,
        initiator_type=initiator_type,
        steps=resolved_steps,
        ttl=ttl,
        metadata=metadata,
        request_key=request_key,
        lock_character=lock_character,
    )
    if error:
        return {"flow": None, **error}
    if flow is None:
        return {"flow": None, "error": "工作流创建失败"}
    if not created:
        return {"flow": flow, "duplicate": True}
    updated = engine.run_auto(str(flow.get("id")), handlers)
    return {"flow": updated or flow, "duplicate": False}


def flow_result(flow: Mapping[str, Any] | None, step_id: str) -> Any:
    """从流程里取某一步的结果；不存在返回 ``None``。"""
    if not isinstance(flow, Mapping):
        return None
    for step in flow.get("steps") or []:
        if isinstance(step, Mapping) and str(step.get("id")) == str(step_id):
            return step.get("result")
    return None


def prior_flow(
    room_dir: Any,
    request_key: str,
    step_id: str,
) -> tuple[dict[str, Any], Any] | None:
    """按幂等键取既有流程；重复提交在预算校验之前就要命中。"""
    key = str(request_key or "").strip()
    if not room_dir or not key:
        return None
    flow = WorkflowEngine(room_dir).find_by_request_key(key)
    if flow is None:
        return None
    return flow, flow_result(flow, step_id)


__all__ = [
    "ACTION_STEPS",
    "STEP_ATTACK",
    "STEP_CAST",
    "STEP_CONCENTRATION",
    "STEP_DAMAGE",
    "STEP_EFFECT",
    "STEP_REACTION",
    "STEP_RESOLVE",
    "STEP_SAVE",
    "STEP_VALIDATE",
    "action_steps",
    "executor_type_for",
    "extra_step",
    "flow_result",
    "prior_flow",
    "run_flow_action",
]
