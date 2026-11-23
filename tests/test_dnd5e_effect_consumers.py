"""效果引擎消费方全量补全（第二刀）：8 类路径都有真实读值点。

覆盖：
- ``abilities.*.value`` → 属性值合并（AC/先攻/命中）
- ``attributes.prof`` → 熟练加值（攻击/法术 DC）
- ``attributes.movement.{fly,swim,…}`` → 分速模型（卡面/战斗者）
- ``traits.dm`` → 减伤（免疫 → 抗性/易伤 → 减伤顺序）
- ``attributes.hp.{max,value}`` → HP 派生与夹取
- ``traits.languages`` → 语言清单
- ``activities[…]`` → 物品活动覆盖攻击/伤害/射程
- ``flags.midi-qol.*`` → 攻击优势与暴击阈值
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _effect(changes, *, transfer: bool = True, disabled: bool = False) -> dict:
    return {"name": "测试效果", "changes": list(changes), "transfer": transfer, "disabled": disabled}


def _change(key: str, mode: int, value) -> dict:
    return {"key": key, "mode": mode, "value": value}


def _base_sheet(**overrides) -> dict:
    sheet = {
        "id": "dnd-effect-consumers",
        "name": "Aria",
        "level": 3,
        "class": "Fighter",
        "abilities": {"str": 16, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8},
        "proficiencies": {"saves": ["con"], "skills": ["athletics"], "weapons": ["martial"]},
        "hp": {"current": 24, "max": 24},
        "speed": 30,
        "equipment": ["longsword", "chain-mail", "shield"],
    }
    sheet.update(overrides)
    return sheet


def _card(effects=None, **overrides) -> dict:
    sheet = _base_sheet(**overrides)
    if effects is not None:
        sheet["effect_changes"] = effects
    return _plugin().schema.pack(sheet)


def _combatant(card: dict | None, *, name: str = "Aria") -> dict:
    combatant = {"name": name, "is_player": True, "side": "player"}
    _plugin().combat.prepare_combatant(combatant, card=card, settings=None)
    return combatant


def _monster(monster_id: str) -> dict:
    combatant = {
        "name": monster_id,
        "is_player": False,
        "side": "enemy",
        "monster_id": monster_id,
    }
    _plugin().combat.prepare_combatant(combatant, card=None, settings=None)
    return combatant


# ---------------------------------------------------------------------------
# 1) abilities.*.value → 属性值合并
# ---------------------------------------------------------------------------


def test_ability_value_merges_before_derivation(plugins):
    """DEX 16 → +4（mode 2 add）= 20：无甲 AC 13→15、先攻 +3→+5。"""
    plain = _plugin().schema.derive(_base_sheet(equipment=[]))
    assert plain["armorClass"] == 13 and plain["initiative"] == 3

    boosted = _plugin().schema.derive(
        _base_sheet(
            equipment=[],
            effect_changes=[_effect([_change("system.abilities.dex.value", 2, 4)])],
        )
    )
    assert boosted["armorClass"] == 15
    assert boosted["initiative"] == 5
    # 原始值保留，卡面可回退。
    assert boosted["base_abilities"]["dex"] == 16


def test_ability_value_caps_at_thirty(plugins):
    sheet = _base_sheet(
        abilities={"str": 16, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8},
        equipment=[],
        effect_changes=[_effect([_change("system.abilities.str.value", 2, 40)])],
    )
    derived = _plugin().schema.derive(sheet)
    assert derived["abilities"]["str"] == 30


# ---------------------------------------------------------------------------
# 2) attributes.prof → 熟练加值
# ---------------------------------------------------------------------------


def test_proficiency_override_feeds_attack_bonus(plugins):
    baseline = _combatant(_card())
    assert baseline["attacks"][0]["attack_bonus"] == 5  # STR +3 + 熟练 2

    buffed = _combatant(
        _card([_effect([_change("system.attributes.prof", 2, 1)])])
    )
    assert buffed["proficiency_bonus"] == 3
    assert buffed["attacks"][0]["attack_bonus"] == 6


# ---------------------------------------------------------------------------
# 3) attributes.movement.* → 分速模型
# ---------------------------------------------------------------------------


def test_movement_modes_feed_speeds_model(plugins):
    sheet = _base_sheet(
        effect_changes=[
            _effect(
                [
                    _change("system.attributes.movement.fly", 2, 60),
                    _change("system.attributes.movement.walk", 2, 10),
                ]
            )
        ]
    )
    packed = _plugin().schema.pack(sheet)
    assert packed["sheet"]["speed"] == 40
    assert packed["sheet"]["speeds"]["walk"] == 40
    assert packed["sheet"]["speeds"]["fly"] == 60

    combatant = _combatant(_card(
        [
            _effect(
                [
                    _change("system.attributes.movement.fly", 2, 60),
                    _change("system.attributes.movement.walk", 2, 10),
                ]
            )
        ]
    ))
    assert combatant["speed_ft"] == 40
    assert combatant["speeds"]["fly"] == 60


def test_no_extra_speed_keeps_sheet_clean(plugins):
    packed = _plugin().schema.pack(_base_sheet(effect_changes=[]))
    assert "speeds" not in packed["sheet"]


# ---------------------------------------------------------------------------
# 4) traits.dm → 减伤阶段
# ---------------------------------------------------------------------------


def test_damage_reduction_applies_after_resistance(plugins):
    """减伤在抗性/易伤之后：1d6+2 打抗性目标 → 6//2=3，再减 2 → 1。"""
    player = _combatant(_card([_effect([_change("system.traits.dm.amount", 2, 2)])]))
    goblin = _monster("goblin")

    player["damage_resistances"] = ["slashing"]
    result = _plugin().combat.resolve_attack_action(
        goblin,
        player,
        {"weapon": "Scimitar", "attack_roll": 18},
        rng=lambda sides: 4,
    )
    assert result["success"] is True
    assert result["damage"]["damage"] == 6
    assert result["damage"]["effective"] == 1  # 6 // 2 = 3，再 -2
    assert result["damage"]["damage_reduction"] == 2


def test_damage_reduction_respects_type(plugins):
    player = _combatant(
        _card(
            [
                _effect(
                    [
                        _change("system.traits.dm.amount", 2, 5),
                        _change("system.traits.dm.type", 5, "fire"),
                    ]
                )
            ]
        )
    )
    # 挥砍不匹配 fire：减伤 0。
    goblin = _monster("goblin")
    result = _plugin().combat.resolve_attack_action(
        goblin,
        player,
        {"weapon": "Scimitar", "attack_roll": 18},
        rng=lambda sides: 4,
    )
    assert result["damage"]["damage_reduction"] == 0
    assert result["damage"]["effective"] == 6


# ---------------------------------------------------------------------------
# 5) attributes.hp.{max,value} → HP 派生与夹取
# ---------------------------------------------------------------------------


def test_hp_max_override_feeds_derived_and_current(plugins):
    sheet = _base_sheet(
        hp={"current": 30, "max": 24},
        effect_changes=[_effect([_change("system.attributes.hp.max", 5, 40)])],
    )
    packed = _plugin().schema.pack(sheet)
    assert packed["maxHp"] == 40
    assert packed["currentHp"] == 30

    combatant = _combatant(packed)
    assert combatant["max_hp"] == 40
    assert combatant["hp"] == 30


def test_hp_max_lower_clamps_current_hp(plugins):
    sheet = _base_sheet(
        hp={"current": 24, "max": 24},
        effect_changes=[_effect([_change("system.attributes.hp.max", 5, 12)])],
    )
    packed = _plugin().schema.pack(sheet)
    assert packed["maxHp"] == 12
    assert packed["currentHp"] == 12


# ---------------------------------------------------------------------------
# 6) traits.languages → 语言清单
# ---------------------------------------------------------------------------


def test_languages_merge_into_proficiencies(plugins):
    packed = _plugin().schema.pack(
        _base_sheet(
            proficiencies={
                "saves": ["con"],
                "skills": ["athletics"],
                "weapons": ["martial"],
                "languages": ["common"],
            },
            effect_changes=[_effect([_change("system.traits.languages.value", 2, "druidic")])],
        )
    )
    languages = packed["sheet"]["proficiencies"]["languages"]
    assert "druidic" in languages and "common" in languages


# ---------------------------------------------------------------------------
# 7) activities[…] → 物品活动覆盖攻击/伤害/射程
# ---------------------------------------------------------------------------


def test_item_activity_overrides_weapon_row(plugins):
    sheet = _base_sheet(
        equipment=[
            {
                "id": "longsword",
                "name": "长剑",
                "category": "weapon",
                "kind": "melee",
                "equipped": True,
                "properties": ["versatile"],
                "damage": {"dice": "1d8", "type": "slashing"},
                "effects": [
                    _effect(
                        [
                            _change("activities[attack].attack.bonus", 2, 2),
                            _change("activities[attack].attack.ability", 5, "finesse"),
                            _change("activities[attack].damage.parts", 5, ["2d6"]),
                            _change("activities[attack].range.value", 2, 20),
                        ]
                    )
                ],
            }
        ]
    )
    from dnd5e_plugin.effect_changes import effective_sheet

    view = effective_sheet(sheet)
    row = sheet["equipment"][0]
    merged = view.weapon_row(row)
    assert merged["attack_bonus"] == 2
    assert merged["attack_ability"] == "finesse"
    assert merged["damage"]["dice"] == "2d6"
    assert merged["range"]["normal_ft"] == 20

    combatant = _combatant(_plugin().schema.pack(sheet))
    attack = combatant["attacks"][0]
    # finesse = dex：DEX 16 → +3；熟练 2；活动 +2 → 7。
    assert attack["ability"] == "dex"
    assert attack["attack_bonus"] == 7
    assert attack["damage_dice"] == "2d6"


# ---------------------------------------------------------------------------
# 8) flags.midi-qol.* → 攻击优势与暴击阈值
# ---------------------------------------------------------------------------


def test_midi_qol_advantage_rolls_twice(plugins):
    player = _combatant(
        _card([_effect([_change("flags.midi-qol.advantage.attack.all", 5, True)])])
    )
    goblin = _monster("goblin")
    dice = iter([5, 18])

    def rng(sides: int) -> int:
        if sides == 20:
            return next(dice)
        return 4

    result = _plugin().combat.resolve_attack_action(
        player, goblin, {"weapon": "长剑"}, rng=rng
    )
    # 两骰取高：18 + 5 = 23 ≥ AC 15。
    assert result["natural"] == 18
    assert result["success"] is True


def test_midi_qol_critical_threshold_expands_crit_range(plugins):
    player = _combatant(_card([_effect([_change("flags.midi-qol.critical.all", 5, True)])]))
    goblin = _monster("goblin")
    result = _plugin().combat.resolve_attack_action(
        player, goblin, {"weapon": "长剑", "attack_roll": 19}, rng=lambda sides: 2
    )
    assert result["natural"] == 19
    assert result["critical"] is True
