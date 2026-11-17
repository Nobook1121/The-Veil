"""E4：D&D 5e 攻击、伤害、先攻与动作经济。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server.agents.tools import combat
from trpg_server.combat.settings import resolve_settings
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.session import empty_budget, turn_model_of

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


class FakeContext:
    def __init__(self, tmp_path: Path, card: dict | None = None):
        self.room_dir = tmp_path
        self.room_id = "dnd5e-combat"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._card = card or {}

    def room_info(self) -> dict:
        members = []
        if self._card:
            members.append(
                {
                    "user_id": 1,
                    "username": "Aria",
                    "character_card": self._card,
                    "character_state": {},
                }
            )
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "house_rules": {},
            "members": members,
        }


def _card() -> dict:
    return builtin_registry().resolve("dnd5e").schema.pack(
        {
            "id": "dnd-card-1",
            "name": "Aria",
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics"], "weapons": ["martial"]},
            "hp": {"current": 24, "max": 24},
            "equipment": ["longsword", "mace", "chain-mail", "shield"],
        }
    )


def _rules():
    return builtin_registry().resolve("dnd5e").combat


def _prepared_player(plugin) -> dict:
    combatant = {"name": "Aria", "is_player": True, "side": "player"}
    _rules().prepare_combatant(combatant, card=_card(), settings=None)
    return combatant


def _prepared_monster(monster_id: str) -> dict:
    combatant = {"name": monster_id, "is_player": False, "side": "enemy", "monster_id": monster_id}
    _rules().prepare_combatant(combatant, card=None, settings=None)
    return combatant


# ---------------------------------------------------------------------------
# 声明与数据包
# ---------------------------------------------------------------------------
def test_describe_exposes_combat_and_turn_model(plugins):
    item = next(row for row in builtin_registry().describe() if row["id"] == "dnd5e")
    assert item["combat"]["available"] is True
    assert item["combat"]["attack_mode"] == "d20"
    assert set(item["combat"]["turn"]["budget"][0]) == {
        "key",
        "per_turn",
        "per_round",
        "refresh",
        "label_i18n",
        "detail",
    }
    assert item["combat"]["turn"]["budget"] == [
        {
            "key": "action",
            "per_turn": 1,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "动作",
            "detail": {},
        },
        {
            "key": "bonus_action",
            "per_turn": 1,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "附赠动作",
            "detail": {},
        },
        {
            "key": "reaction",
            "per_turn": None,
            "per_round": 1,
            "refresh": "round",
            "label_i18n": "反应",
            "detail": {},
        },
        {
            "key": "movement",
            "per_turn": 30,
            "per_round": None,
            "refresh": "turn",
            "label_i18n": "移动",
            "detail": {"unit": "ft"},
        },
    ]


def test_combat_settings_follow_room_ruleset(plugins):
    assert resolve_settings({"ruleset_id": "dnd5e"}).ruleset_id == "dnd5e"
    assert resolve_settings({"ruleset_id": "coc7"}).ruleset_id == "coc7"


def test_monster_and_armor_data_come_from_pack(plugins):
    goblin = _prepared_monster("goblin")
    assert goblin["armorClass"] == 15
    assert (goblin["hp"], goblin["max_hp"]) == (7, 7)
    assert goblin["speed_ft"] == 30
    assert goblin["attacks"][0]["name"] == "Scimitar"
    assert goblin["attacks"][0]["attack_bonus"] == 4

    player = _prepared_player(plugins)
    assert player["armorClass"] == 18  # 链甲 16 + 盾牌 2
    assert player["attacks"][0]["name"] == "长剑"
    assert player["attacks"][0]["attack_bonus"] == 5  # STR +3 + 熟练 +2
    assert player["attacks"][0]["damage_dice"] == "1d8"


def test_initiative_is_d20_plus_dex(plugins):
    goblin = _prepared_monster("goblin")
    assert _rules().roll_initiative(goblin, lambda sides: 10, None) == 12
    assert _rules().initiative_tiebreak(goblin) == 2


# ---------------------------------------------------------------------------
# 攻击与伤害数学
# ---------------------------------------------------------------------------
def test_attack_hit_crit_miss_and_vulnerability(plugins):
    player = _prepared_player(plugins)
    goblin = _prepared_monster("goblin")

    hit = _rules().resolve_attack_action(
        player, goblin, {"weapon": "长剑", "attack_roll": 18}, rng=lambda sides: 4
    )
    assert hit["success"] is True and hit["critical"] is False
    assert hit["total"] == 23 and hit["target_ac"] == 15
    assert hit["damage"]["effective"] == 7
    assert goblin["hp"] == 0 and goblin["dying"] is True

    fresh = _prepared_monster("goblin")
    crit = _rules().resolve_attack_action(
        player, fresh, {"weapon": "长剑", "attack_roll": 20}, rng=lambda sides: 3
    )
    assert crit["critical"] is True and crit["success"] is True
    assert crit["damage"]["effective"] == 9  # 2d8 + 3
    assert fresh["dying"] is True

    miss = _rules().resolve_attack_action(
        player, _prepared_monster("goblin"), {"weapon": "长剑", "attack_roll": 2}, rng=lambda sides: 4
    )
    assert miss["success"] is False and miss["damage"] is None

    skeleton = _prepared_monster("skeleton")
    vulnerable = _rules().resolve_attack_action(
        player, skeleton, {"weapon": "钉头锤", "attack_roll": 15}, rng=lambda sides: 2
    )
    # 钉头锤 1d6 + 3 = 5，骷髅对钝击易伤 → 10。
    assert vulnerable["damage"]["vulnerability"] is True
    assert vulnerable["damage"]["effective"] == 10
    assert skeleton["hp"] == 3


def test_death_saves_at_turn_start(plugins):
    skeleton = _prepared_monster("skeleton")
    skeleton.update(hp=0, dying=True, unconscious=True, status="dying")
    skeleton["death_saves"] = {"successes": 0, "failures": 2, "stable": False}
    notes = _rules().start_turn(skeleton, combat={"round": 1, "surprised": []}, rng=lambda sides: 9)
    assert skeleton["death_saves"]["failures"] == 3
    assert skeleton["dead"] is True
    assert any("死亡" in note for note in notes)


# ---------------------------------------------------------------------------
# 端到端工具与动作经济
# ---------------------------------------------------------------------------
def test_start_attack_and_action_economy(tmp_path, plugins, monkeypatch):
    context = FakeContext(tmp_path, _card())
    monkeypatch.setattr(combat, "_roller", lambda sides: 10 if sides == 20 else 3)
    started = combat.start_combat(
        {
            "participants": [
                "Aria",
                {"name": "Goblin", "monster_id": "goblin", "is_npc": True, "side": "enemy"},
            ]
        },
        context,
    )
    assert "error" not in started
    state = combat._load_combat(context)
    assert state["order"][0] == "Aria"
    assert state["combatants"]["Aria"]["budget"] == {
        "action": 1,
        "bonus_action": 1,
        "reaction": 1,
        "movement": 30,
    }

    result = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18}, context
    )
    assert result["success"] is True
    state = combat._load_combat(context)
    assert state["combatants"]["Aria"]["budget"]["action"] == 0
    assert state["combatants"]["Goblin"]["hp"] == 1

    blocked = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18}, context
    )
    assert "额度" in blocked["error"]

    spent = combat.spend_combat_action({"actor": "Aria", "slot": "movement", "amount": 10}, context)
    assert spent["summary"].startswith("Aria 剩余 movement：20")
    assert combat.spend_combat_action({"actor": "Aria", "slot": "movement", "amount": 99}, context)[
        "error"
    ]

    combat.next_turn({}, context)
    state = combat._load_combat(context)
    assert state["order"][state["turn"]] == "Goblin"
    assert state["combatants"]["Goblin"]["budget"]["action"] == 1
    combat.next_turn({}, context)
    state = combat._load_combat(context)
    assert state["order"][state["turn"]] == "Aria"
    assert state["combatants"]["Aria"]["budget"]["action"] == 1
    assert state["combatants"]["Aria"]["budget"]["movement"] == 30


def test_turn_model_budget_shape(plugins):
    model = turn_model_of(builtin_registry().resolve("dnd5e"))
    assert empty_budget(model) == {"action": 1, "bonus_action": 1, "reaction": 1, "movement": 30}


# ---------------------------------------------------------------------------
# 规则集切换拦截
# ---------------------------------------------------------------------------
def test_switch_is_blocked_while_combat_active(tmp_path, plugins):
    from trpg_server.routes.rooms_lifecycle import RulesetSwitchBlocked, _apply_ruleset_switch

    info = {"ruleset_id": "dnd5e", "house_rules": {"by_ruleset": {"dnd5e": {}, "coc7": {}}}}
    (tmp_path / "state.json").write_text(
        json.dumps({"combat": {"active": True, "round": 1}}), encoding="utf-8"
    )
    with pytest.raises(RulesetSwitchBlocked):
        _apply_ruleset_switch(info, "coc7", tmp_path)

    # 同一规则集不受影响（例如版本迁移/保存）。
    _apply_ruleset_switch(info, "dnd5e", tmp_path)

    (tmp_path / "state.json").write_text(json.dumps({"combat": None}), encoding="utf-8")
    old, new, _paused = _apply_ruleset_switch(info, "coc7", tmp_path)
    assert (old, new) == ("dnd5e", "coc7")
