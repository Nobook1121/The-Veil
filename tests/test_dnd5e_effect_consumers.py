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


# ---------------------------------------------------------------------------
# 9) workflow 钩子（effect_runtime DSL）→ combat 执行
# ---------------------------------------------------------------------------


def test_workflow_hook_on_hit_adds_damage(plugins):
    """converted 钩子在命中后真的加骰伤害，并挂进战斗结果。"""
    card = _card(
        [
            _effect([]),
        ]
    )
    card["sheet"]["workflow_hooks"] = [
        {
            "status": "converted",
            "source": "midi-qol.onUseMacroName",
            "dsl": {
                "trigger": "onHit",
                "actions": [
                    {"type": "damage.add", "dice": "1d6", "damage_type": "fire"}
                ],
            },
        }
    ]
    player = _combatant(card)
    goblin = _monster("goblin")
    result = _plugin().combat.resolve_attack_action(
        player, goblin, {"weapon": "长剑", "attack_roll": 18}, rng=lambda sides: 3
    )
    assert result["success"] is True
    workflow = result.get("workflow")
    assert workflow and workflow["trigger"] == "onHit"
    assert workflow["actions"] and workflow["actions"][0]["type"] == "damage.add"
    assert 1 <= workflow["actions"][0]["amount"] <= 6


def test_workflow_hook_condition_add_and_miss_trigger(plugins):
    """onMiss 钩子只在未命中触发；condition.add 写进目标状态。"""
    card = _card([_effect([])])
    card["sheet"]["workflow_hooks"] = [
        {
            "status": "converted",
            "dsl": {
                "trigger": "onMiss",
                "actions": [{"type": "condition.add", "condition": "off_balance"}],
            },
        }
    ]
    player = _combatant(card)
    goblin = _monster("goblin")
    result = _plugin().combat.resolve_attack_action(
        player, goblin, {"weapon": "长剑", "attack_roll": 2}, rng=lambda sides: 3
    )
    assert result["success"] is False
    assert result["workflow"]["trigger"] == "onMiss"
    assert "off_balance" in goblin.get("conditions", [])


# ---------------------------------------------------------------------------
# 10) 目标侧 grants / 半身人幸运 / 物品改名
# ---------------------------------------------------------------------------


def _target_with_grants(grants: dict) -> dict:
    goblin = _monster("goblin")
    goblin["grants_attack"] = grants
    goblin["creature_type"] = "fiend"
    return goblin


def test_grants_advantage_from_target_gives_attacker_advantage(plugins):
    """Faerie Fire 形态：目标给「所有攻击者」优势 → 攻击掷两骰取高。"""
    card = _card()
    player = _combatant(card)
    target = _target_with_grants({"advantage": True})
    dice = iter([4, 17])

    def rng(sides: int) -> int:
        return next(dice) if sides == 20 else 3

    result = _plugin().combat.resolve_attack_action(
        player, target, {"weapon": "长剑"}, rng=rng
    )
    assert result["advantage"] is True
    assert result["natural"] == 17


def test_grants_disadvantage_filters_by_attacker_type(plugins):
    """Protection from Evil：只有名单里的攻击者类型吃劣势。"""
    card = _card()
    player = _combatant(card)
    player["creature_type"] = "undead"
    target = _target_with_grants(
        {"disadvantage": ["aberration", "celestial", "elemental", "fey", "fiend", "undead"]}
    )
    dice = iter([12, 3])

    def rng(sides: int) -> int:
        return next(dice) if sides == 20 else 3

    result = _plugin().combat.resolve_attack_action(
        player, target, {"weapon": "长剑"}, rng=rng
    )
    assert result["disadvantage"] is True
    assert result["natural"] == 3

    # 非名单类型（humanoid）不吃劣势。
    human = _combatant(_card())
    human["creature_type"] = "humanoid"
    plain = _plugin().combat.resolve_attack_action(
        human, _target_with_grants({"disadvantage": ["undead"]}), {"weapon": "长剑", "attack_roll": 12},
        rng=lambda sides: 3,
    )
    assert plain["disadvantage"] is False


def test_halfling_lucky_rerolls_natural_one(plugins):
    """半身人幸运：d20 掷出 1 自动重掷一次。"""
    card = _card([_effect([_change("flags.dnd5e.halflingLucky", 5, True)])])
    player = _combatant(card)
    assert player.get("halfling_lucky") is True
    dice = iter([1, 15])

    def rng(sides: int) -> int:
        return next(dice) if sides == 20 else 4

    result = _plugin().combat.resolve_attack_action(
        player, _monster("goblin"), {"weapon": "长剑"}, rng=rng
    )
    assert result["natural"] == 15
    assert result["rolls"] == [1, 15]


def test_item_rename_change_reaches_weapon_row(plugins):
    """``name`` 效果（Shillelagh）把物品改名带到攻击行，模板 {} 换成原名。"""
    from dnd5e_plugin.effect_changes import weapon_row

    sheet = _base_sheet(
        equipment=[
            {
                "id": "club",
                "name": "木棒",
                "category": "weapon",
                "equipped": True,
                "damage": {"dice": "1d4", "type": "bludgeoning"},
                "effects": [
                    _effect(
                        [
                            _change("name", 5, "{}, Shillelagh"),
                            _change("system.damage.base.denomination", 5, 8),
                        ]
                    )
                ],
            }
        ]
    )
    from dnd5e_plugin.effect_changes import effective_sheet

    merged = weapon_row(sheet["equipment"][0], effective_sheet(sheet))
    assert merged["name"].startswith("木棒")
    assert "Shillelagh" in merged["name"]
    assert merged["damage"]["dice"] == "1d8"
