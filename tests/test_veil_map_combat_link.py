"""M10：剧本 NPC → 5e 战斗者，以及战斗与地图棋子同步。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
VEIL_ENTRY = PLUGIN_ROOT / "veil-map" / "entry.py"


def _load_veil_map():
    spec = importlib.util.spec_from_file_location("veil_map_combat_link_test", VEIL_ENTRY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


veil_map = _load_veil_map()


@pytest.fixture()
def plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _room_dir(tmp_path: Path) -> Path:
    room = tmp_path / "rooms" / "room1"
    room.mkdir(parents=True)
    info = {
        "id": "room1",
        "name": "战斗联动测试",
        "room_code": "ROOM1",
        "creator_id": "u1",
        "ruleset_id": "dnd5e",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Alice"},
            }
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room


def _scene(room_dir: Path, *, pc_cell=(0, 0)) -> None:
    state = veil_map._normalize_map({})
    state["scene_id"] = "default"
    state["revision"] = 1
    state["tokens"] = [
        {
            "id": "pc-alice",
            "name": "Alice",
            "kind": "pc",
            "owner_user_id": "u1",
            "combatant": "Alice",
            "cell_x": pc_cell[0],
            "cell_y": pc_cell[1],
            "x": 0,
            "y": 0,
            "w": 1,
            "h": 1,
            "color": "#c8a35a",
        }
    ]
    veil_map._save_map(
        room_dir,
        state,
        {"request_id": "init-scene", "action": "token.add", "ok": True, "revision": 1},
    )


def _combat(*, active: bool = True, turn: int = 1, hp: int = 27) -> dict:
    return {
        "id": "combat-1",
        "active": active,
        "round": 1,
        "turn": turn,
        "ruleset": "dnd5e",
        "order": ["Alice", "克拉格"],
        "combatants": {
            "Alice": {
                "name": "Alice",
                "is_player": True,
                "user_id": "u1",
                "hp": 10,
                "max_hp": 10,
                "initiative": 15,
                "budget": {"movement": 30},
            },
            "克拉格": {
                "name": "克拉格",
                "display_name": "克拉格",
                "is_player": False,
                "is_npc": True,
                "combatant_id": "klarg",
                "hp": hp,
                "max_hp": 27,
                "initiative": 12,
                "side": "enemy",
                "budget": {"movement": 30},
            },
        },
        "log": [],
    }


def _tokens(room_dir: Path) -> list[dict]:
    state, _source = veil_map.scene_store.load(room_dir)
    return state.get("tokens") or []


def _sync(room_dir: Path, combat: dict) -> dict:
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    return veil_map._MapService().sync_combat_tokens(room_dir, info, combat)


def test_combat_start_places_bound_npc_token(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    result = _sync(room_dir, _combat())

    assert result["status"] == "synced"
    assert result["changed"] is True
    assert len(result["placed"]) == 1
    token = next(t for t in _tokens(room_dir) if t["kind"] == "npc")
    assert token["combatant"] == "npc:klarg"
    assert token["combatant_name"] == "克拉格"
    assert token["combat"]["combat_id"] == "combat-1"
    assert token["combat"]["hp"] == 27
    assert token["combat"]["initiative"] == 12
    assert token["combat"]["side"] == "enemy"
    assert token["combat"]["current"] is True
    assert (token["cell_x"], token["cell_y"]) != (0, 0)


def test_hp_and_turn_sync_to_existing_token(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    result = _sync(room_dir, _combat(hp=9, turn=1))

    assert result["updated"] >= 1
    token = next(t for t in _tokens(room_dir) if t["kind"] == "npc")
    assert token["combat"]["hp"] == 9
    assert token["combat"]["current"] is True
    alice = next(t for t in _tokens(room_dir) if t["kind"] == "pc")
    assert alice["combat"]["hp"] == 10


def test_combat_position_writes_back_to_token(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    combat = _combat()
    combat["combatants"]["克拉格"]["position"] = {
        "scene_id": "default",
        "cell": {"x": 3, "y": 4},
    }
    result = _sync(room_dir, combat)

    assert result["changed"] is True
    token = next(t for t in _tokens(room_dir) if t["kind"] == "npc")
    assert (token["cell_x"], token["cell_y"]) == (3, 4)


def test_map_move_writes_position_into_combat(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    combat = _combat(turn=0)
    combat["order"] = ["克拉格", "Alice"]
    (room_dir / "state.json").write_text(
        json.dumps({"combat": combat}, ensure_ascii=False), encoding="utf-8"
    )
    state, _source = veil_map.scene_store.load(room_dir)
    token = next(t for t in state["tokens"] if t["kind"] == "npc")

    ok, code, _message = veil_map._apply_combat_cost(
        "room1", room_dir, state, token, 2, 2
    )
    assert ok is True
    assert code == ""
    saved = json.loads((room_dir / "state.json").read_text(encoding="utf-8"))
    position = saved["combat"]["combatants"]["克拉格"]["position"]
    assert position["cell"] == {"x": 2, "y": 2}
    assert position["scene_id"] == "default"


def test_retreat_status_removes_token(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    combat = _combat()
    combat["combatants"]["克拉格"]["status"] = "retreated"
    result = _sync(room_dir, combat)

    assert result["removed"] == 1
    assert all(token["kind"] == "pc" for token in _tokens(room_dir))



def test_combat_end_removes_npc_token_and_clears_pc_snapshot(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    result = _sync(room_dir, _combat(active=False))

    assert result["removed"] == 1
    tokens = _tokens(room_dir)
    assert all(token["kind"] == "pc" for token in tokens)
    alice = tokens[0]
    assert not alice.get("combat")
    assert alice["name"] == "Alice"


def test_removed_combatant_drops_its_token(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())
    combat = _combat()
    combat["order"] = ["Alice"]
    combat["combatants"].pop("克拉格")
    result = _sync(room_dir, combat)

    assert result["removed"] == 1
    assert all(token["kind"] == "pc" for token in _tokens(room_dir))


def test_no_scene_degrades_without_creating_map(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    result = _sync(room_dir, _combat())

    assert result["ok"] is True
    assert result["status"] == "skipped"
    assert result["reason"] == "no_scene"
    assert not (room_dir / "plugins" / "veil-map" / "scenes").exists()


def test_start_combat_places_token_through_ruleset_hook(tmp_path, plugins) -> None:
    """真实链路：core start_combat → dnd5e 模块桥接 → 地图服务放置棋子。"""
    from trpg_server.agents.tools import combat

    scenes = tmp_path / "scenarios" / "s1"
    scenes.mkdir(parents=True)
    (scenes / "scenario.json").write_text(
        json.dumps(
            {
                "id": "s1",
                "modules": [
                    {
                        "id": "npc_klarg",
                        "title": "NPC：克拉格",
                        "module_type": "npc",
                        "monster_id": "bugbear",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    context = SimpleNamespace(
        room_dir=room_dir,
        room_id="room1",
        config_dir=tmp_path / "config",
        scenarios_dir=tmp_path / "scenarios",
        characters_dir=tmp_path / "characters",
        room_info=lambda: {
            "id": "room1",
            "ruleset_id": "dnd5e",
            "scenario_id": "s1",
            "house_rules": {},
            "members": [
                {
                    "user_id": "u1",
                    "username": "alice",
                    "room_role": "owner",
                    "status": "active",
                    "is_active": True,
                    "character_card": {"name": "Alice"},
                }
            ],
        },
    )

    started = combat.start_combat(
        {
            "participants": [
                {
                    "module_id": "npc_klarg",
                    "name": "克拉格",
                    "is_npc": True,
                    "side": "enemy",
                }
            ]
        },
        context,
    )
    assert "error" not in started

    state = combat._load_combat(context)
    klarg = state["combatants"]["克拉格"]
    npc_token = next(t for t in _tokens(room_dir) if t["kind"] == "npc")
    assert npc_token["combatant"] == f"npc:{klarg['combatant_id']}"
    assert npc_token["combat"]["combat_id"] == state["id"]
    assert npc_token["combat"]["hp"] == 27
    assert npc_token["combat"]["initiative"] == klarg["initiative"]


def test_combat_end_cleans_other_scenes(tmp_path) -> None:
    room_dir = _room_dir(tmp_path)
    _scene(room_dir)
    _sync(room_dir, _combat())

    # 把 NPC 棋子挪到另一个场景，模拟战斗中切图；结束战斗后必须清掉孤儿。
    scene_state, _source = veil_map.scene_store.load(room_dir)
    npc = next(t for t in scene_state["tokens"] if t["kind"] == "npc")
    scene_state["tokens"] = [t for t in scene_state["tokens"] if t["kind"] == "pc"]
    scene_state["scene_id"] = "default"
    veil_map._save_map(
        room_dir,
        scene_state,
        {"request_id": "remove-active", "action": "token.delete", "ok": True, "revision": scene_state["revision"]},
    )
    other = veil_map._normalize_map({})
    other["scene_id"] = "other"
    other["revision"] = 1
    other["tokens"] = [npc]
    veil_map.scene_store.save(room_dir, other, activate=False)

    result = _sync(room_dir, _combat(active=False))
    assert result["status"] == "synced"
    assert result["removed"] >= 1
    reloaded = veil_map.scene_store.load_scene(room_dir, "other")
    assert reloaded is not None
    assert reloaded.get("tokens") == []
