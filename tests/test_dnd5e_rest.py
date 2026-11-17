"""G2：D&D 5e 短休/长休、生命骰、装备 AC 与调谐。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server import room_rest, room_rules
from trpg_server.combat.settings import resolve_settings
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

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


def _settings(**rules):
    return resolve_settings({"ruleset_id": "dnd5e", "house_rules": dict(rules)})


def _card(equipment=None, level: int = 3, hp: dict | None = None) -> dict:
    return _plugin().schema.pack(
        {
            "id": "rest-card",
            "name": "Mira",
            "level": level,
            "class": "Wizard",
            "abilities": {"str": 10, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": hp or {"current": 10, "max": 30},
            "equipment": list(equipment or []),
        }
    )


def _room(card: dict, ruleset_id: str = "dnd5e") -> tuple[dict, dict]:
    member = {
        "user_id": 1,
        "username": "Mira",
        "character_card": card,
        "character_state": {"current_hp": 10, "max_hp": 30},
        "is_active": True,
    }
    info = {
        "id": "room-rest",
        "ruleset_id": ruleset_id,
        "house_rules": {},
        "members": [member],
    }
    return info, member


# ---------------------------------------------------------------------------
# 插件：生命骰与休息
# ---------------------------------------------------------------------------
def test_short_rest_spends_hit_dice_and_heals(plugins):
    card = _card()
    holder = {
        "name": "Mira",
        "hp": 10,
        "max_hp": 30,
        "spell_slots": {"1": {"max": 4, "used": 0}},
        "effects": [],
    }
    result = _plugin().rest.short_rest(
        holder, card, hit_dice=2, rng=lambda sides: 4, settings=_settings()
    )
    assert holder["hit_dice"] == {"die": 6, "max": 3, "used": 2}
    assert result["hit_dice_spent"] == 2
    # 每颗 d6 掷 4 + CON 调整值 1 = 5，两颗共 10。
    assert result["healed"] == 10 and holder["hp"] == 20
    assert result["hit_dice_remaining"] == 1


def test_long_rest_restores_hp_half_hit_dice_slots_and_exhaustion(plugins):
    from dnd5e_plugin.effects import build_condition

    card = _card()
    exhaustion = build_condition("exhaustion", levels=2)
    holder = {
        "name": "Mira",
        "hp": 0,
        "max_hp": 30,
        "spell_slots": {"1": {"max": 4, "used": 4}, "2": {"max": 2, "used": 1}},
        "spell_points": {"max": 14, "used": 14},
        "effects": [exhaustion],
        "concentration": {"spell": "bless", "name": "Bless"},
        "hit_dice": {"die": 6, "max": 3, "used": 3},
    }
    result = _plugin().rest.long_rest(holder, card, rng=lambda sides: 1, settings=_settings())
    assert holder["hp"] == 30
    assert result["healed"] == 30
    assert holder["hit_dice"] == {"die": 6, "max": 3, "used": 2}
    assert result["hit_dice_regained"] == 1
    assert all(row["used"] == 0 for row in holder["spell_slots"].values())
    assert "spell_points" not in holder
    assert "concentration" not in holder
    assert result["exhaustion_level"] == 1


# ---------------------------------------------------------------------------
# 核心：房间级入口与 CoC 不可见
# ---------------------------------------------------------------------------
def test_room_rest_applies_and_persists(plugins, tmp_path):
    card = _card(hp={"current": 10, "max": 30})
    info, member = _room(card)
    room_dir = tmp_path
    room_rules.save_room_info(room_dir, info)
    result = room_rest.take_rest(
        room_dir, info, kind="short", hit_dice=1, rng=lambda sides: 6
    )
    row = result["members"][0]
    assert row["healed"] == 7  # d6 掷 6 + CON 1
    assert member["character_state"]["current_hp"] == 17
    assert member["character_card"]["sheet"]["hit_dice"]["used"] == 1
    assert result["profile"]["short_rest_minutes"] == 60


def test_coc7_has_no_rest_entrypoints(plugins, tmp_path):
    card = _card()
    info, _member = _room(card, ruleset_id="coc7")
    room_rules.save_room_info(tmp_path, info)
    with pytest.raises(room_rules.RuleOperationError) as exc:
        room_rest.take_rest(tmp_path, info, kind="short")
    assert exc.value.code == "no_rest"
    assert room_rest.rest_available(info) is False

    from trpg_server.agents.profiles import build_kp_tool_names

    names = set(build_kp_tool_names("coc7"))
    assert "rest.take" not in names and "equipment.attune" not in names
    assert "rest.take" in set(build_kp_tool_names("dnd5e"))


# ---------------------------------------------------------------------------
# 装备 AC、力量需求与穿戴状态
# ---------------------------------------------------------------------------
def test_armor_class_follows_equipment_categories(plugins):
    from dnd5e_plugin.combat import armor_class_from_equipment

    assert armor_class_from_equipment(_card()) == 12  # 无甲 10 + DEX 2
    leather = _card(equipment=[{"id": "leather", "equipped": True}])
    assert armor_class_from_equipment(leather) == 13  # 11 + DEX 2
    chain_shirt = _card(equipment=["chain-shirt"])
    assert armor_class_from_equipment(chain_shirt) == 15  # 13 + min(DEX,2)
    chain_mail = _card(equipment=["chain-mail"])
    assert armor_class_from_equipment(chain_mail) == 16  # 重甲不加 DEX
    shielded = _card(equipment=["chain-mail", "shield"])
    assert armor_class_from_equipment(shielded) == 18


def test_equipped_state_controls_armor_and_weapons(plugins):
    card = _card(
        equipment=[
            {"id": "chain-mail", "equipped": False},
            {"id": "longsword", "equipped": False},
        ]
    )
    combatant = {"name": "Mira", "is_player": True, "side": "player"}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=_settings())
    # 未穿戴的重甲不提供 AC；未持握的武器不进入攻击列表。
    assert combatant["armorClass"] == 12
    assert combatant["attacks"][0]["id"] == "unarmed"


def test_armor_strength_requirement_reduces_speed(plugins):
    card = _card(equipment=["chain-mail"])  # STR 10 < 需求 13
    combatant = {"name": "Mira", "is_player": True, "side": "player"}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=_settings())
    assert combatant["armor_strength_penalty"] == 13
    assert combatant["speed_ft"] == 20

    strong = _card(equipment=["chain-mail"])
    strong["sheet"]["abilities"]["str"] = 14
    ok = {"name": "Mira", "is_player": True, "side": "player"}
    _plugin().combat.prepare_combatant(ok, card=strong, settings=_settings())
    assert "armor_strength_penalty" not in ok and ok["speed_ft"] == 30


# ---------------------------------------------------------------------------
# 调谐
# ---------------------------------------------------------------------------
def test_attunement_requires_rest_and_respects_limit(plugins, tmp_path):
    from dnd5e_plugin.effects import build_condition  # noqa: F401 - 保持插件已加载

    card = _card(
        equipment=[
            {"id": "cloak", "name": "隐身斗篷", "attunement": True},
            {"id": "ring", "name": "防护戒指", "attunement": True},
            {"id": "boots", "name": "精灵靴", "attunement": True},
            {"id": "amulet", "name": "健康护符", "attunement": True},
        ]
    )
    info, member = _room(card)
    room_rules.save_room_info(tmp_path, info)

    with pytest.raises(room_rules.RuleOperationError) as exc:
        room_rest.attune(tmp_path, info, member, "cloak")
    assert exc.value.code == "attunement_requires_rest"

    room_rest.attune(tmp_path, info, member, "cloak", during_rest=True)
    room_rest.attune(tmp_path, info, member, "ring", during_rest=True)
    room_rest.attune(tmp_path, info, member, "boots", during_rest=True)
    with pytest.raises(room_rules.RuleOperationError) as exc:
        room_rest.attune(tmp_path, info, member, "amulet", during_rest=True)
    assert exc.value.code == "attunement_limit"

    removed = room_rest.unattune(tmp_path, info, member, "ring")
    assert removed["attuned"] == ["cloak", "boots"]


def test_item_without_attunement_is_rejected(plugins, tmp_path):
    card = _card(equipment=["longsword"])
    info, member = _room(card)
    room_rules.save_room_info(tmp_path, info)
    with pytest.raises(room_rules.RuleOperationError) as exc:
        room_rest.attune(tmp_path, info, member, "longsword", during_rest=True)
    assert exc.value.code == "attunement_not_required"
