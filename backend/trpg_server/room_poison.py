"""战斗状态之外的毒剂队列：只做登记与到期调度，不含任何规则数学。

存储位置是房间 ``state.json`` 的 ``poison_queue``，与战斗内的
``combat.pending_poisons`` 语义一致，但不依赖战斗状态是否存在：

- ``due_round``：按战斗回合到期；条目带 ``combat_id`` 记录所属战斗，
  只在该战斗的轮次里到期，战斗开始 / 结束时由战斗侧清理；
- ``due_at``：按游戏时间到期（ISO 8601，取房间游戏时钟的当前值比较），
  与战斗无关，可在任何时刻结算。

CON 检定、伤害减半、剂量等 CoC7 数学全部留在 ``ruleset/coc7``，由
:mod:`trpg_server.agents.tools.combat` 在结算时调用，本模块只搬运数据。

队列的读改写与游戏时钟共用房间锁（:func:`room_clock.room_lock`）；结算保存时
按到期条目的 ``id`` 删除，而不是把调用方手里的旧快照整表写回。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from trpg_server import room_clock
from trpg_server.agents.room_state import load_room_state, save_room_state
from trpg_server.room_clock import room_lock

QUEUE_KEY = "poison_queue"
MAX_QUEUE = 50


class QueueRemainder(list):
    """``take_due`` 的剩余队列，附带本次到期条目的 id。

    ``save_queue`` 依据 ``settled_ids`` 在房间锁内按 id 删除已结算项，而不是把
    调用方手里的旧快照整表写回；期间新登记的条目会保留下来。
    """

    settled_ids: set[str]


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_queue(room_dir: Any) -> list[dict[str, Any]]:
    queue = load_room_state(Path(room_dir)).get(QUEUE_KEY)
    if not isinstance(queue, list):
        return []
    return [dict(item) for item in queue if isinstance(item, dict)]


def _save_queue(room_dir: Any, queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = [dict(item) for item in queue if isinstance(item, dict)][-MAX_QUEUE:]
    state = load_room_state(Path(room_dir))
    state[QUEUE_KEY] = entries
    save_room_state(Path(room_dir), state)
    return entries


def load_queue(room_dir: Any) -> list[dict[str, Any]]:
    with room_lock(room_dir):
        return _load_queue(room_dir)


def save_queue(room_dir: Any, queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """写回队列；带 ``settled_ids`` 时按 id 删除到期项，其余保持磁盘现状。"""
    settled_ids = getattr(queue, "settled_ids", None)
    with room_lock(room_dir):
        if settled_ids is not None:
            current = _load_queue(room_dir)
            entries = [
                entry
                for entry in current
                if str(entry.get("id") or "") not in settled_ids
            ]
        else:
            entries = [dict(item) for item in queue if isinstance(item, dict)]
        return _save_queue(room_dir, entries)


def register(
    room_dir: Any,
    *,
    target: Any,
    damage: Any = "1D6",
    potency: Any = None,
    doses: Any = 1,
    due_round: Any = None,
    due_at: Any = None,
    combat_id: Any = None,
    source: Any = "narrative",
) -> dict[str, Any]:
    """登记一条延迟毒剂；``due_round`` / ``due_at`` 至少给一个才可能到期。"""
    entry = {
        "id": uuid4().hex,
        "target": str(target or "").strip(),
        "target_name": str(target or "").strip(),
        "damage": str(damage or "1D6").strip() or "1D6",
        "potency": potency,
        "doses": max(1, _as_int(doses, 1)),
        "due_round": _as_int(due_round, 0) if due_round not in (None, "") else None,
        "due_at": str(due_at or "") or None,
        "combat_id": str(combat_id or "") or None,
        "registered_at": _timestamp(),
        "source": str(source or "narrative"),
    }
    with room_lock(room_dir):
        queue = _load_queue(room_dir)
        queue.append(entry)
        _save_queue(room_dir, queue)
    return entry


def drop_round_entries(room_dir: Any) -> int:
    """清掉所有按回合到期的条目（新战斗开始时调用，避免旧毒误触发）。"""
    with room_lock(room_dir):
        queue = _load_queue(room_dir)
        kept = [entry for entry in queue if entry.get("due_round") in (None, "")]
        dropped = len(queue) - len(kept)
        if dropped:
            _save_queue(room_dir, kept)
    return dropped


def take_due(
    queue: list[dict[str, Any]],
    *,
    round_no: Any = None,
    now: Any = None,
    combat_id: Any = None,
    force_rounds: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把队列拆成 (到期, 剩余)。

    - 回合制条目只在 ``combat_id`` 匹配（未记录的旧条目视为当前战斗）时判定，
      防止新战斗打到相同轮数误触发上一场战斗的毒剂；
    - ``force_rounds`` 用于战斗结束时一次性结清本场剩余回合制条目；
    - ``now`` 是当前游戏时间（datetime 或 ISO 串），只影响时间制条目。

    返回的剩余队列是 :class:`QueueRemainder`，保存时会按到期 id 删除。
    """
    moment = room_clock.parse_moment(now) if now is not None else None
    current_round = _as_int(round_no, 0) if round_no is not None else None
    scope = str(combat_id or "") or None

    due: list[dict[str, Any]] = []
    remaining: QueueRemainder = QueueRemainder()
    settled_ids: set[str] = set()
    for entry in queue:
        if not isinstance(entry, dict):
            continue
        due_round = entry.get("due_round")
        if due_round not in (None, ""):
            entry_scope = str(entry.get("combat_id") or "") or None
            if scope and entry_scope and entry_scope != scope:
                remaining.append(entry)
                continue
            if current_round is not None and (
                force_rounds or _as_int(due_round, 0) <= current_round
            ):
                due.append(entry)
                entry_id = str(entry.get("id") or "")
                if entry_id:
                    settled_ids.add(entry_id)
                continue
        due_at = room_clock.parse_moment(entry.get("due_at"))
        if due_at is not None and moment is not None and due_at <= moment:
            due.append(entry)
            entry_id = str(entry.get("id") or "")
            if entry_id:
                settled_ids.add(entry_id)
            continue
        remaining.append(entry)
    remaining.settled_ids = settled_ids
    return due, remaining


def describe(queue: list[dict[str, Any]]) -> str:
    """给控制台 / 工具用的简短列表（只读）。"""
    if not queue:
        return "战斗外毒剂队列为空。"
    lines = []
    for entry in queue:
        due = (
            f"第 {entry.get('due_round')} 轮"
            if entry.get("due_round") not in (None, "")
            else f"{room_clock.format_display(entry.get('due_at'))}"
        )
        lines.append(
            f"- {entry.get('target_name') or entry.get('target') or '?'}："
            f"{entry.get('damage')}（毒性 {entry.get('potency') or 0}，"
            f"{entry.get('doses') or 1} 剂），到期 {due}"
        )
    return "\n".join(lines)
