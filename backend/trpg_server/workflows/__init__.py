"""规则集无关的服务端行动工作流。

一次施法或一次攻击不再由工具调用直接改状态，而是一个带 id、发起者、步骤、
状态与 TTL 的流程实体：跨请求可查、可取消、可超时。工作流只负责编排与
持久化，伤害、豁免、法术位等规则数学仍由规则集插件完成。

- :mod:`trpg_server.workflows.model`：流程/步骤结构与默认策略。
- :mod:`trpg_server.workflows.store`：房间级 JSON 存储、房间写锁与角色锁。
- :mod:`trpg_server.workflows.engine`：打开/查询/取消/超时/幂等/重放。
- :mod:`trpg_server.workflows.actions`：施法与攻击的统一入口。
"""

from trpg_server.workflows.actions import (
    STEP_ATTACK,
    STEP_CAST,
    STEP_EFFECT,
    STEP_RESOLVE,
    STEP_VALIDATE,
    action_steps,
    run_flow_action,
)
from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import (
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTOR_TYPES,
    FLOW_CANCELLED,
    FLOW_COMPLETED,
    FLOW_EXPIRED,
    FLOW_FAILED,
    FLOW_RUNNING,
    FLOW_WAITING,
    POLICY_ASK,
    POLICY_AUTO,
    POLICY_MANUAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_WAITING,
    make_step,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EXECUTOR_TYPES",
    "FLOW_CANCELLED",
    "FLOW_COMPLETED",
    "FLOW_EXPIRED",
    "FLOW_FAILED",
    "FLOW_RUNNING",
    "FLOW_WAITING",
    "POLICY_ASK",
    "POLICY_AUTO",
    "POLICY_MANUAL",
    "STEP_ATTACK",
    "STEP_CAST",
    "STEP_COMPLETED",
    "STEP_EFFECT",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_RESOLVE",
    "STEP_RUNNING",
    "STEP_VALIDATE",
    "STEP_WAITING",
    "WorkflowEngine",
    "action_steps",
    "make_step",
    "run_flow_action",
]
