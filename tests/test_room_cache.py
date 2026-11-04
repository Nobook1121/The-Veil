"""房间级配图缓存与 NPC 复用测试。

覆盖：
- key + 平台/模型/尺寸/画幅元数据校验，元数据不一致不串图；
- 图片与索引落在房间目录内，清理不越界；
- 单图大小、单房间配额与 LRU/拒绝策略；
- 同一房间内场景/NPC 二次生成命中缓存，不重复出图；
- 剧本作者开关默认关闭且可显式开启。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trpg_server.immersive import illustration, room_cache, scenario_cache
from trpg_server.immersive.illustration import scenario_accepts_room_images

SETTINGS = {
    "illustration_enabled": True,
    "illustration_auto": True,
    "illustration_provider": "procedural",
    "illustration_size": "1280x720",
    "illustration_npc_enabled": True,
    "illustration_npc_size": "768x768",
    "illustration_room_cache_max_entries": 40,
    "illustration_room_cache_quota_mb": 64,
    "illustration_room_cache_max_image_mb": 8,
    "illustration_room_cache_overflow": "lru",
}


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _store(room: Path, key: str, *, content: bytes = b"<svg/>", **overrides):
    params = {
        "fingerprint": "scene:大厅",
        "platform": "procedural",
        "model": "svg-v1",
        "size": "1280x720",
        "category": "scene",
        "content": content,
        "suffix": "svg",
        "scene": "大厅",
        "settings": SETTINGS,
    }
    params.update(overrides)
    return room_cache.store(room, key, **params)


def test_room_cache_roundtrip_metadata_and_stats(tmp_path: Path) -> None:
    room = tmp_path / "room1"
    room.mkdir()
    key = _key("scene:大厅")
    ok, reason, entry = _store(room, key)
    assert ok, reason
    assert entry is not None

    cached = room / "immersive_cache" / f"{key}.svg"
    assert cached.is_file()
    assert (room / "immersive_cache" / "index.json").is_file()

    hit = room_cache.lookup(
        room,
        key,
        fingerprint="scene:大厅",
        platform="procedural",
        model="svg-v1",
        size="1280x720",
        category="scene",
    )
    assert hit is not None
    assert Path(hit["path"]).read_bytes() == b"<svg/>"

    miss = room_cache.lookup(
        room,
        key,
        fingerprint="scene:大厅",
        platform="openai_compatible",
        model="svg-v1",
        size="1280x720",
        category="scene",
    )
    assert miss is None

    stats = room_cache.stats(room)
    assert stats["entries"] == 1
    assert stats["files"] == 1
    assert stats["hits"] == 1
    assert str(room.resolve()) in stats["directory"]


def test_room_cache_rejects_bad_key_and_suffix(tmp_path: Path) -> None:
    room = tmp_path / "room1"
    room.mkdir()
    ok, reason, _ = _store(room, "../escape")
    assert not ok and reason == "invalid_key"
    ok, reason, _ = _store(room, _key("x"), suffix="html")
    assert not ok and reason == "invalid_suffix"
    assert not (tmp_path / "escape.svg").exists()


def test_room_cache_lru_and_reject(tmp_path: Path) -> None:
    room = tmp_path / "room1"
    room.mkdir()
    settings = dict(SETTINGS, illustration_room_cache_quota_mb=1, illustration_room_cache_overflow="lru")
    keys = [_key(f"scene:{index}") for index in range(3)]
    for key in keys:
        ok, reason, _ = _store(room, key, content=b"x" * 600_000, settings=settings)
        assert ok, reason
    stats = room_cache.stats(room)
    assert stats["entries"] == 1
    assert stats["bytes"] <= 1024 * 1024

    reject_room = tmp_path / "room2"
    reject_room.mkdir()
    reject = dict(settings, illustration_room_cache_overflow="reject", illustration_room_cache_max_entries=1)
    assert _store(reject_room, keys[0], content=b"x" * 600_000, settings=reject)[0]
    ok, reason, _ = _store(reject_room, keys[1], content=b"x" * 600_000, settings=reject)
    assert not ok and reason == "quota_exceeded"


def test_room_cache_clear_stays_inside_room(tmp_path: Path) -> None:
    room = tmp_path / "room1"
    room.mkdir()
    (room / "room.json").write_text("{}", encoding="utf-8")
    _store(room, _key("scene:大厅"))
    files, size = room_cache.clear(room)
    assert files == 1 and size > 0
    assert (room / "room.json").is_file()
    assert not list((room / "immersive_cache").glob("*.svg"))


def test_generate_now_reuses_room_scene_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(illustration, "IMAGE_CACHE_DIR", runtime)
    room = tmp_path / "room"
    room.mkdir()

    first = illustration.generate_now(
        room_dir=room,
        room_id="room-a",
        scene="大厅",
        profile="daily",
        intensity="medium",
        seed="seed-1",
        turn_index=1,
        settings=SETTINGS,
        fingerprint="scene:大厅",
    )
    assert first["cache_level"] == "none"
    assert Path(first["filename"]).suffix == ".svg"

    second = illustration.generate_now(
        room_dir=room,
        room_id="room-a",
        scene="大厅",
        profile="daily",
        intensity="medium",
        seed="seed-2",
        turn_index=2,
        settings=SETTINGS,
        fingerprint="scene:大厅",
    )
    assert second["cache_hit"] is True
    assert second["cache_level"] == "room"
    assert second["source_room"] == "room-a"
    assert second["url"].endswith(".svg")


def test_generate_now_reuses_npc_portrait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(illustration, "IMAGE_CACHE_DIR", runtime)
    room = tmp_path / "room"
    room.mkdir()

    kwargs = {
        "room_dir": room,
        "room_id": "room-a",
        "scene": "老周",
        "profile": "daily",
        "intensity": "medium",
        "settings": SETTINGS,
        "category": "portrait",
        "fingerprint": "npc:老周",
        "npc_name": "老周",
        "size_override": "768x768",
    }
    first = illustration.generate_now(seed="p1", turn_index=1, **kwargs)
    second = illustration.generate_now(seed="p2", turn_index=2, **kwargs)
    assert first["cache_level"] == "none"
    assert second["cache_hit"] is True
    assert second["cache_level"] == "room"
    assert second["width"] == 768
    assert second["url"] != ""


def test_cross_room_hit_requires_author_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(illustration, "IMAGE_CACHE_DIR", runtime)
    scenarios = tmp_path / "scenarios"
    (scenarios / "scenario-7").mkdir(parents=True)
    (scenarios / "scenario-7" / "scenario.json").write_text(
        json.dumps({"id": 7, "title": "测试剧本", "accept_room_images": False}),
        encoding="utf-8",
    )
    settings = dict(
        SETTINGS,
        illustration_scenario_record=False,
        illustration_scenario_prefer=True,
    )
    scenario_cache.store(
        "7",
        fingerprint="scene:大厅",
        platform="procedural",
        model="svg-v1",
        size="1280x720",
        content=b"<svg/>",
        suffix="svg",
        scene="大厅",
        profile="daily",
        intensity="medium",
        settings=settings,
        origin="room",
        source_room="room-a",
        scenarios_dir=scenarios,
    )

    room_b = tmp_path / "room-b"
    room_b.mkdir()
    blocked = illustration.generate_now(
        room_dir=room_b,
        room_id="room-b",
        scene="大厅",
        profile="daily",
        intensity="medium",
        seed="b",
        turn_index=1,
        settings=settings,
        scenario_id="7",
        scenarios_dir=scenarios,
        fingerprint="scene:大厅",
        allow_cross_room=False,
    )
    assert blocked.get("cache_hit") is not True

    (scenarios / "scenario-7" / "scenario.json").write_text(
        json.dumps({"id": 7, "title": "测试剧本", "accept_room_images": True}),
        encoding="utf-8",
    )
    room_c = tmp_path / "room-c"
    room_c.mkdir()
    allowed = illustration.generate_now(
        room_dir=room_c,
        room_id="room-c",
        scene="大厅",
        profile="daily",
        intensity="medium",
        seed="c",
        turn_index=2,
        settings=settings,
        scenario_id="7",
        scenarios_dir=scenarios,
        fingerprint="scene:大厅",
        allow_cross_room=True,
    )
    assert allowed["cache_hit"] is True
    assert allowed["cache_level"] == "cross-room"
    assert allowed["source_room"] == "room-a"


def test_scenario_author_accept_rooms_default_off(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "scenario-7"
    scenario_dir.mkdir()
    (scenario_dir / "scenario.json").write_text(
        json.dumps({"id": 7, "title": "测试剧本"}), encoding="utf-8"
    )
    assert scenario_accepts_room_images("7", scenarios_dir=tmp_path) is False

    (scenario_dir / "scenario.json").write_text(
        json.dumps({"id": 7, "title": "测试剧本", "accept_room_images": True}), encoding="utf-8"
    )
    assert scenario_accepts_room_images("7", scenarios_dir=tmp_path) is True
