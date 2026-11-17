"""房间级锁：地图移动与战斗伤害必须串行，不能互相覆盖。"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from trpg_server.agents.room_state import load_room_state, save_room_state
from trpg_server.agents.tools import combat as combat_tools
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.room_clock import room_lock

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
ENTRY = PLUGIN_ROOT / "veil-map" / "entry.py"


@pytest.fixture
def plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    PluginRegistry(PLUGIN_ROOT).load()
    yield
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _load_plugin():
    spec = importlib.util.spec_from_file_location("veil_map_entry_lock_test", ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


veil_map = _load_plugin()


class _Context:
    def __init__(self, room_dir: Path):
        self.room_dir = room_dir
        self.room_id = "room1"
        self.config_dir = room_dir / "config"
        self.user_id = "u1"
        self.agent_id = "kp"

    def room_info(self) -> dict:
        return json.loads((self.room_dir / "info.json").read_text(encoding="utf-8"))


def _write_room(tmp_path: Path) -> Path:
    room_dir = tmp_path / "room1"
    room_dir.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "锁测试",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "members": [
            {
                "user_id": "u1",
                "username": "Bob",
                "room_role": "owner",
                "is_active": True,
                "status": "active",
                "character_card": {"name": "Bob", "hp": {"current": 20, "max": 20}},
                "character_state": {},
            }
        ],
    }
    (room_dir / "info.json").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    save_room_state(
        room_dir,
        {
            "combat": {
                "id": "combat-1",
                "active": True,
                "round": 1,
                "turn": 0,
                "order": ["Bob"],
                "surprised": [],
                "combatants": {
                    "Bob": {
                        "name": "Bob",
                        "hp": 20,
                        "max_hp": 20,
                        "armor": 0,
                        "ruleset": "dnd5e",
                        "budget": {"movement": 30, "action": 1, "reaction": 1},
                    }
                },
            }
        },
    )
    return room_dir


def test_map_move_and_combat_damage_share_one_room_lock(tmp_path, plugins, monkeypatch):
    room_dir = _write_room(tmp_path)
    context = _Context(room_dir)
    token = {"id": "bob", "name": "Bob", "cell_x": 0, "cell_y": 0}
    map_state = {"grid_size": 64}

    real_load = veil_map.load_room_state
    read_done = threading.Event()
    release = threading.Event()
    results: dict[str, object] = {}

    def gated_load(path):
        state = real_load(path)
        if threading.current_thread().name == "map-mover":
            read_done.set()
            # 模拟「地图已读到旧 combat、还没写回」的窗口。
            release.wait(timeout=5)
        return state

    monkeypatch.setattr(veil_map, "load_room_state", gated_load)

    def map_move():
        # 与真实的 map move 入口一致：整段读改写都在房间锁内。
        with room_lock(room_dir):
            results["move"] = veil_map._apply_combat_cost(
                "room1", room_dir, map_state, token, 4, 0
            )

    damager_started = threading.Event()

    def deal_damage():
        damager_started.set()
        results["damage"] = combat_tools.damage_combatant(
            {"target": "Bob", "amount": 7}, context
        )

    mover = threading.Thread(target=map_move, name="map-mover")
    damager = threading.Thread(target=deal_damage, name="damager")
    mover.start()
    assert read_done.wait(timeout=5)
    damager.start()
    assert damager_started.wait(timeout=5)
    # 伤害线程应被房间锁挡住；留出一点时间让它走到加锁处。
    threading.Event().wait(0.15)
    release.set()
    mover.join(timeout=5)
    damager.join(timeout=5)

    assert results["move"][0] is True
    combat = (load_room_state(room_dir) or {}).get("combat") or {}
    combatant = combat["combatants"]["Bob"]
    # 两项修改都必须保留：移动力扣 20 英尺（4 格），HP 扣 7。
    assert combatant["budget"]["movement"] == 10
    assert combatant["hp"] == 13
