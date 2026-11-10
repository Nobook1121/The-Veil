"""房间状态并发回归：info.json 整份写回、锁忙等 / ABA、毒剂队列读改写。"""
from __future__ import annotations

import copy
import os
import threading
import time

import pytest

from trpg_server import room_clock, room_poison, room_rules


def _info(**overrides):
    info = {
        "id": "room-1",
        "name": "并发测试房",
        "room_code": "ABC123",
        "house_rules": {"coc7_game_clock": True},
        "members": [],
    }
    info.update(overrides)
    return info


def test_stale_full_write_does_not_roll_clock_back(tmp_path):
    room_rules.save_room_info(tmp_path, _info())
    room_clock.advance_clock(tmp_path, hours=1)

    stale = room_rules.load_room_info(tmp_path)
    room_clock.advance_clock(tmp_path, hours=1)

    # 模拟加入房间 / 写角色卡：请求开始时读到旧 info，推进后才整份写回。
    stale["members"] = [{"user_id": "u1", "role": "player"}]
    room_rules.save_room_info(tmp_path, stale)

    clock = room_clock.read_stored_clock(room_rules.load_room_info(tmp_path))
    assert clock is not None
    assert clock["elapsed_seconds"] == 2 * room_clock.SECONDS_PER_HOUR


def test_interleaved_join_writes_and_clock_advances_keep_time(tmp_path):
    room_rules.save_room_info(tmp_path, _info())
    room_clock.advance_clock(tmp_path, seconds=1)  # 先建立时钟

    stale = copy.deepcopy(room_rules.load_room_info(tmp_path))
    rounds = 15
    errors: list[BaseException] = []

    def writer():
        try:
            for index in range(rounds):
                snapshot = copy.deepcopy(stale)
                snapshot["members"] = [{"user_id": f"u{index}", "role": "player"}]
                room_rules.save_room_info(tmp_path, snapshot)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常需带回主线程
            errors.append(exc)

    def clocker():
        try:
            for _ in range(rounds):
                room_clock.advance_clock(tmp_path, seconds=10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=clocker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    clock = room_clock.read_stored_clock(room_rules.load_room_info(tmp_path))
    assert clock is not None
    # 每次推进都必须累加，不能被旧快照覆盖或回退。
    assert clock["elapsed_seconds"] == 1 + rounds * 10


def test_file_lock_polls_with_deadline_when_stat_keeps_failing(tmp_path, monkeypatch):
    room_rules.save_room_info(tmp_path, _info())
    lock_path = tmp_path / room_clock.LOCK_FILENAME
    lock_path.write_text("other-holder", encoding="utf-8")
    monkeypatch.setattr(room_clock, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(room_clock, "LOCK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(room_clock, "_lock_age", lambda _path: None)

    started = time.monotonic()
    with pytest.raises(room_clock.ClockError) as raised:
        room_clock.advance_clock(tmp_path, hours=1)
    elapsed = time.monotonic() - started

    assert raised.value.code == "clock_busy"
    # 必须按 deadline 退出并 sleep 轮询，而不是空转或永久等待。
    assert elapsed >= 0.04
    assert elapsed < 2.0


def test_lock_release_only_removes_own_token(tmp_path):
    lock_path = tmp_path / room_clock.LOCK_FILENAME

    lock_path.write_text("A-token", encoding="utf-8")
    # A 结束前锁已被 B 重建：不能按旧身份删除 B 的锁。
    assert room_clock._remove_lock_if_owner(lock_path, "B-token") is False
    assert lock_path.exists()
    assert room_clock._remove_lock_if_owner(lock_path, "A-token") is True
    assert not lock_path.exists()


def test_room_lock_writes_and_cleans_unique_token(tmp_path):
    lock_path = tmp_path / room_clock.LOCK_FILENAME
    with room_clock.room_lock(tmp_path):
        token = room_clock._read_lock_token(lock_path)
        assert token and token.startswith(str(os.getpid()))
    assert not lock_path.exists()


def test_poison_save_deletes_settled_entries_by_id(tmp_path):
    past = "2026-01-01 00:00:00"
    future = "2027-01-01 00:00:00"
    due_entry = room_poison.register(tmp_path, target="甲", due_at=past)
    snapshot = room_poison.load_queue(tmp_path)
    due, remaining = room_poison.take_due(snapshot, now="2026-06-01 00:00:00")
    assert [item["id"] for item in due] == [due_entry["id"]]
    assert remaining.settled_ids == {due_entry["id"]}

    # 结算途中新登记的条目不能被旧快照写回覆盖。
    fresh_entry = room_poison.register(tmp_path, target="乙", due_at=future)
    room_poison.save_queue(tmp_path, remaining)

    queue = room_poison.load_queue(tmp_path)
    assert [item["id"] for item in queue] == [fresh_entry["id"]]

    # 重复结算同一份剩余队列不会把已结算项加回来。
    room_poison.save_queue(tmp_path, remaining)
    assert [item["id"] for item in room_poison.load_queue(tmp_path)] == [
        fresh_entry["id"]
    ]


def test_poison_registration_during_settlement_is_not_lost(tmp_path):
    future = "2027-01-01 00:00:00"
    rounds = 10
    errors: list[BaseException] = []

    def registrar():
        try:
            for index in range(rounds):
                room_poison.register(tmp_path, target=f"t{index}", due_at=future)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def settler():
        try:
            for _ in range(rounds):
                queue = room_poison.load_queue(tmp_path)
                _, remaining = room_poison.take_due(queue, now="2026-06-01 00:00:00")
                room_poison.save_queue(tmp_path, remaining)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=registrar), threading.Thread(target=settler)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    # 所有条目都未到期，串行化的读改写不应丢失任何登记。
    assert len(room_poison.load_queue(tmp_path)) == rounds
