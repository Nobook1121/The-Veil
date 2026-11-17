"""工作流引擎：打开、推进、查询、取消、超时与事件重放。

引擎只做编排；每一步的实际副作用由调用方传入的 handler 执行，结果按步骤
写入 ``side_effects`` 并成为幂等缓存。重复提交（同一个 request_key）会直接
返回既有流程，不会再次扣血或再次消耗法术位。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from trpg_server.workflows import access as flow_access
from trpg_server.workflows import store as flow_store
from trpg_server.workflows.model import (
    DEFAULT_TIMEOUT_SECONDS,
    FLOW_CANCELLED,
    FLOW_COMPLETED,
    FLOW_EXPIRED,
    FLOW_FAILED,
    FLOW_RUNNING,
    FLOW_WAITING,
    POLICY_ASK,
    POLICY_MANUAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_WAITING,
    TERMINAL_STATUSES,
    all_steps_settled,
    default_decision,
    make_step,
    new_flow,
    normalize_flow,
    now,
    stamp,
)

StepHandler = Callable[[dict[str, Any], dict[str, Any]], Any]


class WorkflowEngine:
    """单个房间的工作流门面；所有写操作都在房间写锁内完成。"""

    def __init__(self, room_dir: Any, *, room_id: str = "") -> None:
        self.room_dir = room_dir
        self.room_id = str(room_id or "")

    # -- 读 ---------------------------------------------------------------
    def get(self, flow_id: str, *, expire: bool = True) -> dict[str, Any] | None:
        flow_id = str(flow_id or "").strip()
        if not flow_id:
            return None
        if expire:
            self.expire_due()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
        return data["flows"].get(flow_id)

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.expire_due()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
        rows = list(data["flows"].values())
        if status:
            rows = [row for row in rows if str(row.get("status")) == str(status)]
        rows.sort(key=lambda row: float(row.get("created_epoch") or 0), reverse=True)
        return rows[: max(1, int(limit or 50))]

    def find_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        key = str(request_key or "").strip()
        if not key:
            return None
        self.expire_due()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
        for flow in data["flows"].values():
            if str(flow.get("request_key") or "") == key:
                return flow
        return None

    def policy(self) -> dict[str, Any]:
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
        return dict(data.get("policy") or {})

    def set_policy(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        """合并房间级策略覆盖；只接受 auto/ask/manual 与合法执行者。"""
        cleaned: dict[str, dict[str, str]] = {}
        for step, rule in (overrides or {}).items():
            if not isinstance(rule, Mapping):
                continue
            entry = {
                str(executor): str(policy).strip().lower()
                for executor, policy in rule.items()
                if str(executor).strip().lower() in {"kp", "player", "ai"}
                and str(policy).strip().lower() in {"auto", "ask", "manual"}
            }
            if entry:
                cleaned[str(step)] = entry
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            merged = {**data.get("policy", {})}
            for step, rule in cleaned.items():
                merged[step] = {**merged.get(step, {}), **rule}
            data["policy"] = merged
            flow_store.save(self.room_dir, data)
        return merged

    # -- 打开 -------------------------------------------------------------
    def open(
        self,
        *,
        kind: str,
        initiator: str,
        initiator_type: str,
        steps: list[dict[str, Any]],
        ttl: int,
        metadata: Mapping[str, Any] | None = None,
        request_key: str = "",
        lock_character: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        """新建流程并占用角色锁；返回 ``(flow, error, created)``。"""
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            # 先给到期流程做收尾（结算默认决策、释放角色锁），再抢锁：
            # 避免旧流程的角色锁刚被 prune 掉、新流程立刻抢入造成重复结算。
            self._expire_stale(data)
            flow_store.prune_locks(data)
            request_key = str(request_key or "").strip()
            if request_key:
                for existing in data["flows"].values():
                    if str(existing.get("request_key") or "") == request_key:
                        return existing, None, False
            flow = new_flow(
                kind=kind,
                room_id=self.room_id,
                initiator=initiator,
                initiator_type=initiator_type,
                steps=steps,
                ttl=ttl,
                metadata=metadata,
                request_key=request_key,
            )
            if lock_character:
                ok, holder = flow_store.acquire_character_lock(
                    data,
                    flow["initiator"],
                    flow_id=flow["id"],
                    ttl=ttl,
                    owner=str(initiator_type),
                )
                if not ok:
                    return None, {
                        "error": f"{flow['initiator']} 正在结算另一个行动，请等待它完成或超时。",
                        "code": "workflow.character_busy",
                        "holder": holder,
                    }, False
            self._append_event(flow, "flow.created", actor=str(initiator), payload={"kind": kind})
            data["flows"][flow["id"]] = flow
            flow_store.save(self.room_dir, data)
        if lock_character:
            self._schedule_expiry(flow["id"], int(ttl))
        return flow, None, True

    # -- 推进 -------------------------------------------------------------
    def run_auto(
        self,
        flow_id: str,
        handlers: Mapping[str, StepHandler] | None = None,
    ) -> dict[str, Any] | None:
        """顺序执行 auto 步骤；遇到 ask/manual 停在等待态。"""
        handlers = handlers or {}
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(str(flow_id))
            if flow is None:
                return None
            if str(flow.get("status")) in TERMINAL_STATUSES:
                return flow
            for step in flow.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                status = str(step.get("status") or STEP_PENDING)
                if status != STEP_PENDING:
                    continue
                policy = str(step.get("policy") or "auto")
                if policy in {POLICY_ASK, POLICY_MANUAL}:
                    step["status"] = STEP_WAITING
                    step["started_at"] = step.get("started_at") or stamp()
                    flow["status"] = FLOW_WAITING
                    self._append_event(
                        flow,
                        "step.waiting",
                        actor=str(flow.get("initiator") or ""),
                        payload={"step": step.get("id"), "policy": policy},
                    )
                    break
                step_id = str(step.get("id") or "")
                cached = flow["side_effects"].get(step_id)
                if cached is not None:
                    step["result"] = cached
                    step["status"] = STEP_COMPLETED
                    step["finished_at"] = stamp()
                    continue
                handler = handlers.get(step_id)
                step["status"] = STEP_RUNNING
                step["started_at"] = stamp()
                self._append_event(
                    flow,
                    "step.started",
                    actor=str(flow.get("initiator") or ""),
                    payload={"step": step_id},
                )
                try:
                    result = handler(flow, step) if callable(handler) else {"ok": True}
                except Exception as exc:  # noqa: BLE001 - 单步失败不能留下半截流程
                    step["status"] = STEP_FAILED
                    step["error"] = str(exc)
                    step["finished_at"] = stamp()
                    flow["status"] = FLOW_FAILED
                    self._append_event(
                        flow,
                        "step.failed",
                        actor=str(flow.get("initiator") or ""),
                        payload={"step": step_id, "error": str(exc)},
                    )
                    break
                if isinstance(result, Mapping) and result.get("error"):
                    step["status"] = STEP_FAILED
                    step["error"] = str(result.get("error"))
                    step["result"] = dict(result)
                    step["finished_at"] = stamp()
                    flow["status"] = FLOW_FAILED
                    self._append_event(
                        flow,
                        "step.failed",
                        actor=str(flow.get("initiator") or ""),
                        payload={"step": step_id, "error": step["error"]},
                    )
                    break
                flow["side_effects"][step_id] = result
                step["result"] = result
                step["status"] = STEP_COMPLETED
                step["finished_at"] = stamp()
                self._append_event(
                    flow,
                    "step.completed",
                    actor=str(flow.get("initiator") or ""),
                    payload={"step": step_id},
                )
                self._append_event(
                    flow,
                    "side_effect.applied",
                    actor=str(flow.get("initiator") or ""),
                    payload={"key": step_id, "result": result},
                )
            self._settle(flow, data)
            flow_store.save(self.room_dir, data)
        return flow

    def submit(
        self,
        flow_id: str,
        step_id: str,
        *,
        decision: Any = None,
        idempotency_key: str = "",
        actor: str = "",
        actor_user_id: Any = "",
        agent_id: Any = "",
        is_manager: bool = False,
    ) -> dict[str, Any]:
        """提交一个待决步骤的决定；同一幂等键只生效一次。

        ``actor_user_id`` / ``agent_id`` / ``is_manager`` 用于服务端鉴权：
        步骤只能由其目标角色的控制者、对应 AI 执行者或 KP / 房管提交。
        """
        flow_id = str(flow_id or "").strip()
        step_id = str(step_id or "").strip()
        key = str(idempotency_key or "").strip() or f"{flow_id}:{step_id}"
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(flow_id)
            if flow is None:
                return {"error": "工作流不存在", "code": "workflow.not_found"}
            if key in flow["submissions"]:
                return {
                    "flow": flow,
                    "duplicate": True,
                    "result": flow["submissions"][key],
                }
            step = next(
                (item for item in flow.get("steps") or [] if str(item.get("id")) == step_id),
                None,
            )
            if step is None:
                return {"error": f"工作流没有步骤“{step_id}”", "code": "workflow.unknown_step"}
            # 过期流程先按默认决策收尾再拒绝：角色锁可能已被清理，不能留下
            # 可被再次提交的半截流程造成重复结算。
            try:
                expires_at = float(flow.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if (
                str(flow.get("status")) not in TERMINAL_STATUSES
                and expires_at > 0
                and expires_at <= now()
            ):
                self._expire(flow, data)
                flow_store.save(self.room_dir, data)
                return {
                    "error": "工作流已超时结束，不能再提交决定",
                    "code": "workflow.expired",
                    "flow": flow,
                }
            if str(flow.get("status")) in TERMINAL_STATUSES:
                return {"error": "工作流已经结束", "code": "workflow.closed", "flow": flow}
            allowed, code, message = flow_access.authorize_step(
                flow,
                step,
                user_id=actor_user_id,
                agent_id=agent_id,
                manager=is_manager,
                info=flow_access.load_room_info(self.room_dir),
            )
            if not allowed:
                return {"error": message, "code": code}
            if str(step.get("status")) not in {STEP_WAITING, STEP_PENDING}:
                return {"error": f"步骤“{step_id}”当前不可提交", "code": "workflow.step_not_waiting"}
            step["status"] = STEP_COMPLETED
            step["result"] = decision
            step["finished_at"] = stamp()
            flow["submissions"][key] = decision
            self._append_event(
                flow,
                "step.decided",
                actor=str(actor or ""),
                payload={"step": step_id, "idempotency_key": key},
            )
            self._run_continuation(flow, data)
            self._settle(flow, data)
            flow_store.save(self.room_dir, data)
            return {"flow": flow, "duplicate": False, "result": decision}

    def cancel(
        self,
        flow_id: str,
        *,
        actor: str = "",
        reason: str = "",
        actor_user_id: Any = "",
        agent_id: Any = "",
        is_manager: bool = False,
    ) -> dict[str, Any]:
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(str(flow_id or ""))
            if flow is None:
                return {"error": "工作流不存在", "code": "workflow.not_found"}
            if str(flow.get("status")) in TERMINAL_STATUSES:
                return {"flow": flow, "cancelled": False}
            allowed, code, message = flow_access.authorize_control(
                flow,
                user_id=actor_user_id,
                agent_id=agent_id,
                manager=is_manager,
                info=flow_access.load_room_info(self.room_dir),
            )
            if not allowed:
                return {"error": message, "code": code}
            for step in flow.get("steps") or []:
                if str(step.get("status")) in {STEP_PENDING, STEP_WAITING, STEP_RUNNING}:
                    step["status"] = STEP_SKIPPED
                    step["finished_at"] = stamp()
            flow["status"] = FLOW_CANCELLED
            flow["updated_at"] = stamp()
            self._append_event(
                flow,
                "flow.cancelled",
                actor=str(actor or ""),
                payload={"reason": str(reason or "")},
            )
            self._release_lock(flow, data)
            flow_store.save(self.room_dir, data)
        return {"flow": flow, "cancelled": True}

    # -- 超时 -------------------------------------------------------------
    def expire_due(self, moment: float | None = None) -> list[str]:
        """把过期流程按预设默认决策收尾；返回本次过期的流程 id。"""
        moment = now() if moment is None else float(moment)
        expired: list[str] = []
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            # 顺序很重要：先结算到期流程并释放各自角色锁，再清理剩余过期锁。
            # 否则锁先被清掉、流程还没收尾，另一条流程可能抢入造成重复结算。
            self._expire_stale(data, moment, expired)
            released = flow_store.prune_locks(data, moment)
            if expired or released:
                flow_store.save(self.room_dir, data)
        return expired

    def expire(
        self,
        flow_id: str,
        *,
        actor: str = "",
        actor_user_id: Any = "",
        agent_id: Any = "",
        is_manager: bool = False,
    ) -> dict[str, Any]:
        flow_id = str(flow_id or "").strip()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(flow_id)
            if flow is None:
                return {"error": "工作流不存在", "code": "workflow.not_found"}
            if str(flow.get("status")) in TERMINAL_STATUSES:
                return {"flow": flow, "expired": False}
            allowed, code, message = flow_access.authorize_control(
                flow,
                user_id=actor_user_id,
                agent_id=agent_id,
                manager=is_manager,
                info=flow_access.load_room_info(self.room_dir),
            )
            if not allowed:
                return {"error": message, "code": code}
            self._expire(flow, data)
            flow_store.save(self.room_dir, data)
        return {"flow": flow, "expired": True}

    def _expire_stale(
        self,
        data: dict[str, Any],
        moment: float | None = None,
        expired: list[str] | None = None,
    ) -> list[str]:
        """把已到期且未结束的流程收尾；返回本次收尾的流程 id。"""
        moment = now() if moment is None else float(moment)
        rows = expired if expired is not None else []
        for flow in list(data.get("flows", {}).values()):
            if str(flow.get("status")) in TERMINAL_STATUSES:
                continue
            try:
                expires_at = float(flow.get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            if expires_at > moment:
                continue
            self._expire(flow, data)
            rows.append(str(flow.get("id")))
        return rows

    def _expire(self, flow: dict[str, Any], data: dict[str, Any]) -> None:
        continuation = str((flow.get("metadata") or {}).get("continuation") or "").strip()
        registered = False
        if continuation:
            from trpg_server.workflows import continuations

            registered = continuations.has(continuation)
        for step in flow.get("steps") or []:
            status = str(step.get("status"))
            if status not in {STEP_PENDING, STEP_WAITING, STEP_RUNNING}:
                continue
            if registered and status == STEP_PENDING:
                # 续跑器会自己补齐后续步骤；这里只把等待中的决定按默认值收尾。
                continue
            step["status"] = STEP_COMPLETED
            step["result"] = step.get("default") or default_decision(str(step.get("id") or ""))
            step["result"] = {**step["result"], "timeout": True}
            step["finished_at"] = stamp()
        flow["timed_out"] = True
        flow["updated_at"] = stamp()
        self._append_event(
            flow,
            "flow.timeout",
            actor="system",
            payload={
                "defaults": {
                    str(step.get("id")): step.get("default")
                    for step in flow.get("steps") or []
                    if isinstance(step, dict)
                }
            },
        )
        if registered:
            flow["status"] = FLOW_RUNNING
            self._run_continuation(flow, data)
            self._settle(flow, data)
            if str(flow.get("status")) == FLOW_RUNNING and not any(
                str(step.get("status")) in {STEP_PENDING, STEP_WAITING, STEP_RUNNING}
                for step in flow.get("steps") or []
            ):
                flow["status"] = FLOW_COMPLETED
                self._release_lock(flow, data)
            return
        flow["status"] = FLOW_EXPIRED
        self._release_lock(flow, data)

    def _run_continuation(self, flow: dict[str, Any], data: dict[str, Any]) -> None:
        """流程收束后执行注册的续跑器；失败则终止流程。"""
        metadata = flow.get("metadata") if isinstance(flow.get("metadata"), Mapping) else {}
        name = str(metadata.get("continuation") or "").strip()
        if not name or metadata.get("continuation_done"):
            return
        if any(
            str(step.get("status")) == STEP_WAITING for step in flow.get("steps") or []
        ):
            return
        from trpg_server.workflows import continuations

        if not continuations.has(name):
            return
        try:
            result = continuations.run(name, self.room_dir, flow, data)
        except Exception as exc:  # noqa: BLE001 - 续跑失败不能留下半截流程
            flow["status"] = FLOW_FAILED
            flow["continuation_error"] = str(exc)
            flow["updated_at"] = stamp()
            self._append_event(
                flow,
                "flow.failed",
                actor="system",
                payload={"continuation": name, "error": str(exc)},
            )
            self._release_lock(flow, data)
            return
        metadata = dict(metadata)
        flow["continuation_result"] = result
        self._append_event(
            flow,
            "flow.continued",
            actor="system",
            payload={"continuation": name},
        )
        if any(
            str(step.get("status")) == STEP_WAITING for step in flow.get("steps") or []
        ):
            # 续跑器可能把后续步骤（例如反制之后的豁免）挂起：保留续跑资格，
            # 重新按 TTL 计时，等下一次决定到达再继续。
            ttl = max(1, int(flow.get("ttl") or DEFAULT_TIMEOUT_SECONDS))
            flow["status"] = FLOW_WAITING
            flow["updated_at"] = stamp()
            flow["expires_at"] = now() + ttl
            self._schedule_expiry(str(flow.get("id")), ttl)
            return
        metadata["continuation_done"] = True
        flow["metadata"] = metadata
        if all_steps_settled(flow):
            flow["status"] = FLOW_COMPLETED
            self._release_lock(flow, data)

    def _schedule_expiry(self, flow_id: str, ttl: int) -> None:
        delay = max(1, int(ttl)) + 0.05
        timer = threading.Timer(delay, self._expire_safely, args=(flow_id,))
        timer.daemon = True
        timer.start()

    def _expire_safely(self, flow_id: str) -> None:
        try:
            with flow_store.room_write_lock(self.room_dir):
                data = flow_store.load(self.room_dir)
                flow = data["flows"].get(str(flow_id))
                if flow is None or str(flow.get("status")) in TERMINAL_STATUSES:
                    return
                try:
                    expires_at = float(flow.get("expires_at") or 0)
                except (TypeError, ValueError):
                    expires_at = 0.0
                if expires_at > now():
                    return
                self._expire(flow, data)
                flow_store.save(self.room_dir, data)
        except Exception:  # noqa: BLE001 - 定时器异常不能影响请求线程
            return

    # -- 幂等副作用 -------------------------------------------------------
    def apply_once(self, flow_id: str, key: str, fn: Callable[[], Any]) -> tuple[Any, bool]:
        """执行一次副作用；同一个 ``key`` 再次调用直接返回缓存。"""
        flow_id = str(flow_id or "").strip()
        cache_key = str(key or "").strip()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(flow_id)
            if flow is None:
                raise ValueError("workflow not found")
            if cache_key in flow["side_effects"]:
                return flow["side_effects"][cache_key], True
        result = fn()
        with flow_store.room_write_lock(self.room_dir):
            data = flow_store.load(self.room_dir)
            flow = data["flows"].get(flow_id)
            if flow is not None:
                flow["side_effects"][cache_key] = result
                flow["updated_at"] = stamp()
                self._append_event(
                    flow,
                    "side_effect.applied",
                    actor=str(flow.get("initiator") or ""),
                    payload={"key": cache_key, "result": result},
                )
                flow_store.save(self.room_dir, data)
        return result, False

    # -- 重放 -------------------------------------------------------------
    def replay(self, flow_id: str) -> dict[str, Any] | None:
        """从事件日志重建流程快照，用于排查与验证幂等/取消/超时。"""
        flow = self.get(flow_id, expire=False)
        if flow is None:
            return None
        state: dict[str, Any] = {
            "id": flow.get("id"),
            "kind": flow.get("kind"),
            "status": FLOW_RUNNING,
            "initiator": flow.get("initiator"),
            "steps": {
                str(step.get("id")): {"status": STEP_PENDING, "result": None}
                for step in flow.get("steps") or []
                if isinstance(step, dict)
            },
            "side_effects": {},
            "submissions": {},
        }
        for event in flow.get("events") or []:
            kind = str(event.get("type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            step_id = str(payload.get("step") or "")
            if kind == "step.started" and step_id in state["steps"]:
                state["steps"][step_id]["status"] = STEP_RUNNING
            elif kind == "step.waiting" and step_id in state["steps"]:
                state["steps"][step_id]["status"] = STEP_WAITING
            elif kind == "step.completed" and step_id in state["steps"]:
                state["steps"][step_id]["status"] = STEP_COMPLETED
            elif kind == "step.decided" and step_id in state["steps"]:
                state["steps"][step_id]["status"] = STEP_COMPLETED
                state["submissions"][str(payload.get("idempotency_key") or "")] = True
            elif kind == "step.failed" and step_id in state["steps"]:
                state["steps"][step_id]["status"] = STEP_FAILED
            elif kind == "side_effect.applied":
                state["side_effects"][str(payload.get("key") or "")] = payload.get("result")
            elif kind == "flow.timeout":
                state["status"] = FLOW_EXPIRED
            elif kind == "flow.cancelled":
                state["status"] = FLOW_CANCELLED
            elif kind == "flow.completed":
                state["status"] = FLOW_COMPLETED
            elif kind == "flow.failed":
                state["status"] = FLOW_FAILED
        if state["status"] == FLOW_RUNNING and flow.get("status") == FLOW_WAITING:
            state["status"] = FLOW_WAITING
        return state

    # -- 内部 -------------------------------------------------------------
    def _settle(self, flow: dict[str, Any], data: dict[str, Any]) -> None:
        waiting = any(
            str(step.get("status")) == STEP_WAITING for step in flow.get("steps") or []
        )
        failed = any(str(step.get("status")) == STEP_FAILED for step in flow.get("steps") or [])
        if failed:
            flow["status"] = FLOW_FAILED
            self._append_event(flow, "flow.failed", actor="system", payload={})
            self._release_lock(flow, data)
        elif waiting:
            flow["status"] = FLOW_WAITING
        elif all_steps_settled(flow):
            flow["status"] = FLOW_COMPLETED
            self._append_event(flow, "flow.completed", actor="system", payload={})
            self._release_lock(flow, data)
        else:
            flow["status"] = FLOW_RUNNING
        flow["updated_at"] = stamp()

    def _release_lock(self, flow: Mapping[str, Any], data: dict[str, Any]) -> None:
        flow_store.release_character_lock(
            data,
            str(flow.get("initiator") or ""),
            flow_id=str(flow.get("id") or ""),
        )

    @staticmethod
    def _append_event(
        flow: dict[str, Any],
        event_type: str,
        *,
        actor: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        events = flow.setdefault("events", [])
        events.append(
            {
                "seq": len(events) + 1,
                "at": stamp(),
                "type": str(event_type),
                "actor": str(actor or ""),
                "payload": dict(payload or {}),
            }
        )
        if len(events) > 200:
            del events[:-200]


__all__ = ["StepHandler", "WorkflowEngine"]
