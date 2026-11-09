"""批次 5 追逐：纯规则、房间会话与控制台/工具接线测试。"""

from __future__ import annotations

from trpg_server import room_chase
from trpg_server.agents.tools import chase as chase_tools
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import chase as chase_rules
from trpg_server.ruleset.coc7.options import resolve_options


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "chase-batch5"
        self.config_dir = tmp_path / "config"
        self.username = "房主"
        self.user_id = "owner-id"
        self._house_rules = house_rules or {}

    def room_info(self):
        info = load_room_info(self.room_dir) or {}
        info.setdefault("id", self.room_id)
        info.setdefault("house_rules", self._house_rules)
        return info


def _room_info(house_rules=None):
    return {
        "room_id": "chase-batch5",
        "house_rules": house_rules or {},
        "members": [
            {
                "user_id": "p1-id",
                "username": "哈维",
                "character_card": {
                    "name": "哈维",
                    "rulesetId": "coc7",
                    "attributes": {"dex": 60, "str": 50, "siz": 50},
                    "deriveAttributes": {"mov": 7, "build": 0},
                },
                "character_state": {},
            },
            {
                "user_id": "p2-id",
                "username": "追击者",
                "character_card": {
                    "name": "追击者",
                    "rulesetId": "coc7",
                    "attributes": {"dex": 50, "str": 60, "siz": 60},
                    "deriveAttributes": {"mov": 5, "build": 1},
                },
                "character_state": {},
            },
        ],
    }


def _participants():
    return [
        room_chase.participant_from_spec({"name": "追逐者", "mov": 8, "dex": 60}),
        {**room_chase.participant_from_spec({"name": "逃离者", "mov": 5, "dex": 50}), "role": "quarry"},
    ]


# ---------------------------------------------------------------------------
# 纯规则
# ---------------------------------------------------------------------------
def test_chase_switch_defaults_off_and_parses():
    assert resolve_options(None).chase_enabled is False
    assert resolve_options({"house_rules": {"coc7_chase_enabled": True}}).chase_enabled is True
    assert resolve_options({"house_rules": {"coc7_chase_enabled": "false"}}).chase_enabled is False


def test_action_points_use_lowest_mov():
    assert chase_rules.action_points(8, 5) == 4
    assert chase_rules.action_points(5, 5) == 1
    assert chase_rules.action_points(4, 5) == 1
    assert chase_rules.lowest_mov([{"mov": 8}, {"mov": 5}, {"mov": 6}]) == 5
    assert chase_rules.lowest_mov([]) == 1


def test_movement_and_acceleration_costs():
    assert chase_rules.movement_cost(1) == 1
    assert chase_rules.movement_cost(3) == 3
    assert chase_rules.accelerate_locations(None) == 2
    assert chase_rules.accelerate_locations(1) == 2
    assert chase_rules.accelerate_locations(4) == 4
    assert chase_rules.accelerate_locations(9) == 5


def test_hazard_tables():
    assert chase_rules.hazard_bonus_dice(0) == 0
    assert chase_rules.hazard_bonus_dice(1) == 1
    assert chase_rules.hazard_bonus_dice(5) == 2
    assert chase_rules.hazard_failure_ap_loss(0) == 1
    assert chase_rules.hazard_failure_ap_loss(2) == 2
    assert chase_rules.hazard_failure_ap_loss(9) == 3
    assert chase_rules.random_hazard_difficulty(59) is None
    assert chase_rules.random_hazard_difficulty(60) == "regular"
    assert chase_rules.random_hazard_difficulty(85) == "hard"
    assert chase_rules.random_hazard_difficulty(96) == "extreme"


def test_obstacle_and_vehicle_tables():
    assert chase_rules.obstacle_durability("brick_wall") == 25
    assert chase_rules.obstacle_durability("unknown") == 5
    assert chase_rules.obstacle_durability(None, "12") == 12
    assert chase_rules.vehicle_ram_damage(3) == "3D10"
    assert chase_rules.vehicle_recoil_damage(25) == 12
    assert chase_rules.vehicle_build_loss(29) == 2
    assert chase_rules.vehicle_ram_self_damage(25) == 12
    assert chase_rules.vehicle_ram_self_damage(100, target_build=2) == 20
    assert chase_rules.maneuver_penalty_dice(0, 2) == 2
    assert chase_rules.maneuver_penalty_dice(0, 3) is None
    assert chase_rules.can_attack(False) is False
    assert chase_rules.can_attack(False, True) is True
    assert chase_rules.can_push() is False


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------
def test_start_chase_positions_and_action_points(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    chase = room_chase.start_chase(tmp_path, info, _participants())
    assert chase["active"] is True
    assert chase["round"] == 1
    assert len(chase["locations"]) == 6
    positions = {item["name"]: item["position"] for item in chase["participants"]}
    assert positions == {"追逐者": 0, "逃离者": 2}
    points = {item["name"]: item["ap"] for item in chase["participants"]}
    assert points == {"追逐者": 4, "逃离者": 1}


def test_move_consumes_action_points(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(tmp_path, info, _participants())
    room_chase.move_participant(tmp_path, info, "追逐者")
    chase = load_room_info(tmp_path)["chase"]
    chaser = next(item for item in chase["participants"] if item["name"] == "追逐者")
    assert chaser["ap"] == 3
    assert chaser["position"] == 1
    room_chase.move_participant(tmp_path, info, "逃离者")
    room_chase.start_round(tmp_path, info)
    room_chase.move_participant(tmp_path, info, "逃离者")
    chase = load_room_info(tmp_path)["chase"]
    quarry = next(item for item in chase["participants"] if item["name"] == "逃离者")
    assert quarry["position"] == 4
    assert quarry["ap"] == 0
    assert chase["active"] is True
    assert chase["engaged"] is False


def test_move_is_blocked_by_obstacle(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(tmp_path, info, _participants())
    room_chase.add_obstacle(tmp_path, info, start=0, name="高围栏", skill="攀爬", durability=10)
    try:
        room_chase.move_participant(tmp_path, info, "追逐者")
    except Exception as exc:  # noqa: BLE001
        assert "阻挡" in str(exc)
    else:
        raise AssertionError("障碍应当阻挡移动")
    chase = load_room_info(tmp_path)["chase"]
    assert chase["participants"][0]["position"] == 0


def test_obstacle_pass_then_move(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(tmp_path, info, _participants())
    room_chase.add_obstacle(tmp_path, info, start=0, name="高围栏", skill="攀爬", durability=10)
    payload = room_chase.attempt_obstacle(
        tmp_path, info, "追逐者", start=0, success_level="regular"
    )
    assert payload["success"] is True
    chase = load_room_info(tmp_path)["chase"]
    chaser = next(item for item in chase["participants"] if item["name"] == "追逐者")
    assert chaser["position"] == 1
    room_chase.move_participant(tmp_path, info, "追逐者")
    assert load_room_info(tmp_path)["chase"]["participants"][0]["position"] == 2


def test_hazard_failure_costs_action_points(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(tmp_path, info, _participants())
    payload = room_chase.attempt_hazard(
        tmp_path, info, "追逐者", success_level="failure", ap_loss=3, damage=4
    )
    chase = load_room_info(tmp_path)["chase"]
    chaser = next(item for item in chase["participants"] if item["name"] == "追逐者")
    assert payload["success"] is False
    assert payload["damage"] == 4
    assert payload["ap_loss"] == 3
    assert chaser["position"] == 1
    assert chaser["ap"] == 0
    assert chaser["ap_debt"] == 0


def test_vehicle_acceleration_only_for_vehicles(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(
        tmp_path,
        info,
        [
            room_chase.participant_from_spec({"name": "汽车", "mov": 20, "vehicle": True}),
            {**room_chase.participant_from_spec({"name": "逃离者", "mov": 5}), "role": "quarry"},
        ],
    )
    try:
        room_chase.move_participant(tmp_path, info, "逃离者", accelerate=True, locations=4)
    except Exception as exc:  # noqa: BLE001
        assert "载具" in str(exc)
    else:
        raise AssertionError("非载具不应能加速")
    room_chase.move_participant(tmp_path, info, "汽车", accelerate=True, locations=4)
    car = load_room_info(tmp_path)["chase"]["participants"][0]
    assert car["position"] == 4
    assert car["ap"] == 15


def test_conflict_requires_same_location_unless_firearm(tmp_path):
    info = _room_info({"coc7_chase_enabled": True})
    room_chase.start_chase(tmp_path, info, _participants())
    try:
        room_chase.resolve_conflict(tmp_path, info, "追逐者", "逃离者")
    except Exception as exc:  # noqa: BLE001
        assert "同一地点" in str(exc)
    else:
        raise AssertionError("跨地点近战应被拒绝")
    payload = room_chase.resolve_conflict(
        tmp_path, info, "追逐者", "逃离者", has_firearm=True, maneuver_success=True, ap_loss=2
    )
    assert payload["target_ap_loss"] == 2


# ---------------------------------------------------------------------------
# 控制台与工具
# ---------------------------------------------------------------------------
def _run(words, tmp_path):
    return handle_room_rule_command(
        words,
        room_id="chase-batch5",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )


def test_chase_command_requires_switch(tmp_path):
    save_room_info(tmp_path, _room_info())
    ok, text = _run(["chase", "start", "哈维", "追击者"], tmp_path)
    assert ok is False
    assert "未开启" in text or "chase" in text


def test_chase_command_start_move_and_end(tmp_path):
    save_room_info(tmp_path, _room_info({"coc7_chase_enabled": True}))
    ok, text = _run(["chase", "start", "哈维", "追击者"], tmp_path)
    assert ok is True, text
    assert "追逐第 1 轮" in text
    chase = load_room_info(tmp_path)["chase"]
    quarry = next(item for item in chase["participants"] if item["role"] == "quarry")
    assert quarry["name"] == "追击者"
    assert quarry["position"] == 2

    ok, text = _run(["chase", "move", "哈维"], tmp_path)
    assert ok is True, text
    ok, _ = _run(["chase", "round"], tmp_path)
    assert ok is True
    assert load_room_info(tmp_path)["chase"]["round"] == 2
    ok, _ = _run(["chase", "end", "caught"], tmp_path)
    assert ok is True
    assert load_room_info(tmp_path)["chase"]["active"] is False


def test_chase_hazard_command_uses_explicit_result_for_npc(tmp_path):
    save_room_info(tmp_path, _room_info({"coc7_chase_enabled": True}))
    assert _run(["chase", "start", "哈维", "追击者"], tmp_path)[0] is True
    ok, text = _run(["chase", "hazard", "追击者", "敏捷", "result", "failure", "loss", "2"], tmp_path)
    assert ok is True, text
    assert "失败" in text


def test_chase_tools_respect_switch(tmp_path):
    save_room_info(tmp_path, _room_info())
    context = FakeContext(tmp_path)
    result = chase_tools.execute_chase_start(
        {"participants": [{"name": "A"}, {"name": "B", "role": "quarry"}]}, context
    )
    assert result["ok"] is False
    assert "disabled" in result["error"]

    save_room_info(tmp_path, _room_info({"coc7_chase_enabled": True}))
    result = chase_tools.execute_chase_start(
        {"participants": [{"name": "A"}, {"name": "B", "role": "quarry"}]}, context
    )
    assert result["ok"] is True
    assert result["chase"]["participants"][1]["position"] == 2
    status = chase_tools.execute_chase_status({}, context)
    assert status["active"] is True
