"""道具存储的房间锁淘汰策略（B-lock）。

覆盖：配额满时只淘汰空闲条目、正在持有的锁不会被换掉、以及唯一配额被长期
持有时新房间等待而不是制造第二把锁。
"""
from __future__ import annotations

import threading
import time

from trpg_server.props import store


def test_room_lock_eviction_skips_held_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_LOCKS", {})
    monkeypatch.setattr(store, "_LOCKS_GUARD", threading.Lock())
    monkeypatch.setattr(store, "_MAX_ROOM_LOCKS", 2)
    held, idle, fresh = tmp_path / "held", tmp_path / "idle", tmp_path / "fresh"

    with store._room_lock(held) as held_lock:
        with store._room_lock(idle):
            pass
        # 配额已满，且 held 正被持有：只能淘汰 idle，不能换掉 held 的锁。
        with store._room_lock(fresh) as fresh_lock:
            assert store._LOCKS[str(held.resolve())].lock is held_lock
            assert str(idle.resolve()) not in store._LOCKS
            assert store._LOCKS[str(fresh.resolve())].lock is fresh_lock


def test_room_lock_never_creates_two_locks_for_same_room(tmp_path, monkeypatch):
    """唯一配额被长期持有时，新房间要等待而不是淘汰在用的锁。"""
    monkeypatch.setattr(store, "_LOCKS", {})
    monkeypatch.setattr(store, "_LOCKS_GUARD", threading.Lock())
    monkeypatch.setattr(store, "_MAX_ROOM_LOCKS", 1)
    monkeypatch.setattr(store, "_LOCK_RETRY_SECONDS", 0.002)

    room, blocker = tmp_path / "room", tmp_path / "blocker"
    acquired = threading.Event()
    release = threading.Event()

    def hold_blocker():
        with store._room_lock(blocker):
            acquired.set()
            release.wait(2)

    holder = threading.Thread(target=hold_blocker)
    holder.start()
    assert acquired.wait(2)

    stats = {"active": 0, "max": 0}
    lock_ids: set[int] = set()
    stats_guard = threading.Lock()
    errors: list[BaseException] = []

    def worker():
        try:
            with store._room_lock(room) as lock:
                with stats_guard:
                    stats["active"] += 1
                    stats["max"] = max(stats["max"], stats["active"])
                    lock_ids.add(id(lock))
                time.sleep(0.01)
                with stats_guard:
                    stats["active"] -= 1
        except BaseException as exc:  # noqa: BLE001 - 收集线程异常给断言用
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for thread in workers:
        thread.start()
    time.sleep(0.05)  # 让所有 worker 进入「没有可淘汰条目」的等待路径
    release.set()
    for thread in workers:
        thread.join(5)
    holder.join(2)

    assert not errors
    assert stats["max"] == 1
    assert len(lock_ids) == 1
    assert len(store._LOCKS) == 1
