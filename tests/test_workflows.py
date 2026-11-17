"""第二段：服务端行动工作流（锁、幂等、超时、取消、重放与统一入口）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.tools import combat as combat_tools
from trpg_server.agents.tools import spells as spell_tools
from trpg_server.agents.tools import workflow as workflow_tools
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.workflows.engine import WorkflowEngine
from trpg_server.workflows.model import (
    FLOW_CANCELLED,
    FLOW_COMPLETED,
    FLOW_EXPIRED,
    FLOW_WAITING,
    POLICY_ASK,
    make_step,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def plugins():
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _plugin():
    return builtin_registry().resolve("dnd5e")


def _wizard_card(level: int = 5, known=None) -> dict:
    return _plugin().schema.pack(
        {
            "id": f"wizard-{level}",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 24, "max": 24},
            "spellcasting": {"known": list(known or ["magic-missile"])},
            "equipment": [],
        }
    )


class _Context:
    def __init__(self, room_dir: Path, card: dict | None = None):
        self.room_dir = room_dir
        self.room_id = "workflow-room"
        self.config_dir = room_dir / "config"
        self.scenarios_dir = room_dir / "scenarios"
        self.user_id = 1
        self.agent_id = ""
        self._card = card or _wizard_card()

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "members": [
                {
                    "user_id": 1,
                    "username": "Mira",
                    "is_active": True,
                    "character_card": self._card,
                    "character_state": {},
                }
            ],
        }


# ---------------------------------------------------------------------------
# 引擎：锁、幂等、超时、取消、重放
# ---------------------------------------------------------------------------
def test_character_lock_blocks_concurrent_action(tmp_path):
    engine = WorkflowEngine(tmp_path, room_id="r")
    steps = [make_step("resolve", "结算")]
    first, error, created = engine.open(
        kind="attack",
        initiator="Mira",
        initiator_type="player",
        steps=steps,
        ttl=60,
        request_key="req-1",
    )
    assert error is None and created and first["id"]
    # 同一个幂等键返回既有流程，不再建新流程。
    again, error, created = engine.open(
        kind="attack",
        initiator="Mira",
        initiator_type="player",
        steps=steps,
        ttl=60,
        request_key="req-1",
    )
    assert error is None and created is False and again["id"] == first["id"]
    # 同一角色并发发起另一个行动被角色锁拒绝。
    blocked, error, created = engine.open(
        kind="attack",
        initiator="Mira",
        initiator_type="player",
        steps=steps,
        ttl=60,
        request_key="req-2",
    )
    assert blocked is None and created is False
    assert error["code"] == "workflow.character_busy"
    assert error["holder"]["flow_id"] == first["id"]


def test_flow_completes_and_replays(tmp_path):
    engine = WorkflowEngine(tmp_path, room_id="r")
    flow, _error, _created = engine.open(
        kind="attack",
        initiator="Mira",
        initiator_type="player",
        steps=[make_step("validate", "校验"), make_step("attack", "攻击")],
        ttl=60,
        request_key="req-done",
    )
    done = engine.run_auto(
        flow["id"],
        {"attack": lambda _flow, _step: {"hit": True, "damage": 7}},
    )
    assert done["status"] == FLOW_COMPLETED
    assert done["side_effects"]["attack"] == {"hit": True, "damage": 7}
    replayed = engine.replay(flow["id"])
    assert replayed["status"] == FLOW_COMPLETED
    assert replayed["steps"]["attack"]["status"] == "completed"
    assert replayed["side_effects"]["attack"] == {"hit": True, "damage": 7}
    # 完成后角色锁释放，可以继续行动。
    second, error, created = engine.open(
        kind="attack",
        initiator="Mira",
        initiator_type="player",
        steps=[make_step("attack", "攻击")],
        ttl=60,
        request_key="req-next",
    )
    assert created and error is None and second["id"] != flow["id"]


def test_timeout_applies_default_and_releases_lock(tmp_path):
    engine = WorkflowEngine(tmp_path, room_id="r")
    flow, _error, _created = engine.open(
        kind="cast",
        initiator="Mira",
        initiator_type="player",
        steps=[
            make_step("save", "豁免", policy=POLICY_ASK, default={"decision": "fail"}),
            make_step("resolve", "结算"),
        ],
        ttl=1,
        request_key="req-timeout",
    )
    waiting = engine.run_auto(flow["id"], {})
    assert waiting["status"] == FLOW_WAITING
    expired = engine.expire_due(moment=float(flow["expires_at"]) + 1)
    assert flow["id"] in expired
    updated = engine.get(flow["id"], expire=False)
    assert updated["status"] == FLOW_EXPIRED
    save_step = next(step for step in updated["steps"] if step["id"] == "save")
    assert save_step["result"]["decision"] == "fail"
    assert save_step["result"]["timeout"] is True
    assert engine.replay(flow["id"])["status"] == FLOW_EXPIRED
    # 超时后角色锁被释放。
    again, error, created = engine.open(
        kind="cast",
        initiator="Mira",
        initiator_type="player",
        steps=[make_step("resolve", "结算")],
        ttl=60,
        request_key="req-after-timeout",
    )
    assert created and error is None


def test_cancel_closes_flow(tmp_path):
    engine = WorkflowEngine(tmp_path, room_id="r")
    flow, _error, _created = engine.open(
        kind="cast",
        initiator="Mira",
        initiator_type="player",
        steps=[make_step("save", "豁免", policy=POLICY_ASK)],
        ttl=60,
        request_key="req-cancel",
    )
    engine.run_auto(flow["id"], {})
    result = engine.cancel(flow["id"], actor="KP", reason="玩家离线", is_manager=True)
    assert result["cancelled"] is True
    assert result["flow"]["status"] == FLOW_CANCELLED
    assert engine.get(flow["id"], expire=False)["status"] == FLOW_CANCELLED


def test_room_policy_override_waits_and_submits(tmp_path):
    from trpg_server.workflows.actions import run_flow_action
    from trpg_server.workflows.model import STEP_COMPLETED

    engine = WorkflowEngine(tmp_path, room_id="r")
    engine.set_policy({"resolve": {"player": "manual"}, "save": {"player": "not-a-policy"}})
    assert engine.policy()["resolve"]["player"] == "manual"
    assert "save" not in engine.policy()  # 非法策略被丢弃

    outcome = run_flow_action(
        tmp_path,
        kind="cast",
        initiator="Mira",
        initiator_type="player",
        steps=[
            {
                **make_step("resolve", "结算", policy="auto"),
                "executor": "player",
            }
        ],
        handlers={"resolve": lambda _flow, _step: {"ok": True}},
        request_key="policy-1",
    )
    flow = outcome["flow"]
    assert flow["status"] == FLOW_WAITING
    assert flow["steps"][0]["policy"] == "manual"
    submitted = engine.submit(
        flow["id"],
        "resolve",
        decision={"ok": True},
        idempotency_key="decision-1",
        actor="Mira",
        is_manager=True,
    )
    assert submitted["duplicate"] is False
    assert submitted["flow"]["status"] == FLOW_COMPLETED
    assert submitted["flow"]["steps"][0]["status"] == STEP_COMPLETED
    # 同一个幂等键重复提交不再生效。
    again = engine.submit(
        flow["id"],
        "resolve",
        decision={"ok": False},
        idempotency_key="decision-1",
    )
    assert again["duplicate"] is True
    assert again["flow"]["status"] == FLOW_COMPLETED


# ---------------------------------------------------------------------------
# 施法：统一入口、幂等与前置校验
# ---------------------------------------------------------------------------
def _combat_context(tmp_path) -> _Context:
    context = _Context(tmp_path)
    started = combat_tools.start_combat(
        {
            "participants": [
                "Mira",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started, started
    combat = combat_tools._load_combat(context)
    if combat["order"][combat["turn"]] != "Mira":
        combat_tools.next_turn({}, context)
    return context


def test_duplicate_cast_does_not_double_spend_or_damage(tmp_path, plugins):
    context = _combat_context(tmp_path)
    arguments = {
        "caster": "Mira",
        "spell": "magic-missile",
        "targets": ["Goblin"],
        "slot_level": 1,
        "request_id": "cast-1",
    }
    first = spell_tools.cast_spell(dict(arguments), context)
    assert "error" not in first, first
    state = combat_tools._load_combat(context)
    hp_after_first = state["combatants"]["Goblin"]["hp"]
    assert state["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1
    assert state["combatants"]["Mira"]["budget"]["action"] == 0

    second = spell_tools.cast_spell(dict(arguments), context)
    assert "error" not in second, second
    assert second["workflow"]["duplicate"] is True
    state = combat_tools._load_combat(context)
    assert state["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 1
    assert state["combatants"]["Goblin"]["hp"] == hp_after_first
    assert state["combatants"]["Mira"]["budget"]["action"] == 0


def test_cast_range_rejected_before_flow(tmp_path, plugins):
    context = _combat_context(tmp_path)
    combat = combat_tools._load_combat(context)
    combat["combatants"]["Goblin"]["distance_ft"] = 999
    combat_tools._save_combat(context, combat)
    result = spell_tools.cast_spell(
        {
            "caster": "Mira",
            "spell": "magic-missile",
            "targets": ["Goblin"],
            "slot_level": 1,
            "request_id": "cast-too-far",
        },
        context,
    )
    assert "超出射程" in result.get("error", "")
    # 校验失败会留下一条 failed 流程作为审计记录，但不会扣法术位。
    flow = WorkflowEngine(tmp_path).find_by_request_key("cast-too-far")
    assert flow is not None and flow["status"] == "failed"
    state = combat_tools._load_combat(context)
    assert state["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 0


def test_duplicate_attack_does_not_double_damage(tmp_path, plugins):
    context = _combat_context(tmp_path)
    combat = combat_tools._load_combat(context)
    while combat["order"][combat["turn"]] != "Goblin":
        combat_tools.next_turn({}, context)
        combat = combat_tools._load_combat(context)
    arguments = {
        "attacker": "Goblin",
        "target": "Mira",
        "attack_roll": 20,
        "request_id": "attack-1",
    }
    first = combat_tools.attack_combatant(dict(arguments), context)
    assert "error" not in first, first
    state = combat_tools._load_combat(context)
    hp_after_first = state["combatants"]["Mira"]["hp"]
    action_after_first = state["combatants"]["Goblin"]["budget"]["action"]

    second = combat_tools.attack_combatant(dict(arguments), context)
    assert "error" not in second, second
    assert second["workflow"]["duplicate"] is True
    state = combat_tools._load_combat(context)
    assert state["combatants"]["Mira"]["hp"] == hp_after_first
    assert state["combatants"]["Goblin"]["budget"]["action"] == action_after_first


def test_attack_and_damage_steps_are_auto(tmp_path, plugins):
    context = _combat_context(tmp_path)
    flow = WorkflowEngine(tmp_path).find_by_request_key("attack-auto")
    assert flow is None
    combat = combat_tools._load_combat(context)
    while combat["order"][combat["turn"]] != "Goblin":
        combat_tools.next_turn({}, context)
        combat = combat_tools._load_combat(context)
    combat_tools.attack_combatant(
        {"attacker": "Goblin", "target": "Mira", "attack_roll": 20, "request_id": "attack-auto"},
        context,
    )
    flow = WorkflowEngine(tmp_path).find_by_request_key("attack-auto")
    assert flow["status"] == FLOW_COMPLETED
    policies = {step["id"]: step["policy"] for step in flow["steps"]}
    assert policies["attack"] == "auto"
    assert policies["damage"] == "auto"


def test_registered_guard_blocks_cast(tmp_path, plugins):
    from trpg_server.workflows import guards

    def wall_guard(payload):
        if str(payload.get("kind")) == "cast" and "Goblin" in payload.get("targets", []):
            return ["Mira 与 Goblin 之间被墙体或关闭的门阻挡"]
        return []

    guards.register_action_guard(wall_guard)
    try:
        context = _combat_context(tmp_path)
        result = spell_tools.cast_spell(
            {
                "caster": "Mira",
                "spell": "magic-missile",
                "targets": ["Goblin"],
                "slot_level": 1,
                "request_id": "cast-wall",
            },
            context,
        )
        assert "墙体" in result.get("error", "")
        assert result.get("code") == "workflow.blocked"
        assert WorkflowEngine(tmp_path).find_by_request_key("cast-wall") is None
        state = combat_tools._load_combat(context)
        assert state["combatants"]["Mira"]["spell_slots"]["1"]["used"] == 0
    finally:
        guards.clear_action_guards()


# ---------------------------------------------------------------------------
# 规则集边界：CoC7 不出现工作流入口
# ---------------------------------------------------------------------------
def test_coc7_has_no_workflow_tools(plugins):
    from trpg_server.agents.profiles import workflow_tool_names

    assert workflow_tool_names("dnd5e")
    assert workflow_tool_names("coc7") == []

    class _CocContext:
        room_dir = Path(".")
        room_id = "coc"

        def room_info(self):
            return {"id": "coc", "ruleset_id": "coc7", "members": []}

    result = workflow_tools.list_workflows({}, _CocContext())
    assert "未启用" in result.get("error", "")
