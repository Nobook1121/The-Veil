"""战斗日志「数值链」：一次结算是否在聊天里单卡闭环。

三件事被钉住：

1. 命中：攻击式 → total vs AC → 命中 → 伤害 → HP a→b，字段齐全可直接渲染；
2. 未命中：链止于 ``hit: false``，不给伤害与 HP 假数据；
3. 豁免类（圣火）：豁免式 → total vs DC → 成功/失败 →（失败时）伤害 → HP。

同时钉住兼容性：``metadata.dice_roll`` 保持原样（骰桌动画链路依赖它），
无法识别结算时**不产出** ``combat_log``（旧消息按老样式渲染，不会崩）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.tools import combat
from trpg_server.agents.tools import spells as spell_tools
from trpg_server.agents.tools.combat_chain import attack_chain, save_chain
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"

# 渲染层依赖的字段：少一个都会让卡面缺段或崩。
CHAIN_KEYS = {
    "version",
    "kind",
    "attacker",
    "target",
    "expression",
    "roll",
    "total",
    "compare",
    "hit",
    "outcome",
    "damage_roll",
    "damage",
    "hp_before",
    "hp_after",
    "hp_delta",
    "notes",
}


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


def _fighter_card() -> dict:
    return _plugin().schema.pack(
        {
            "id": "fighter-chain",
            "name": "Aria",
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics"], "weapons": ["martial"]},
            "hp": {"current": 24, "max": 24},
            "equipment": ["longsword", "mace", "chain-mail", "shield"],
        }
    )


def _wizard_card() -> dict:
    return _plugin().schema.pack(
        {
            "id": "wizard-chain",
            "name": "Mira",
            "level": 3,
            "class": "Wizard",
            "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
            "hp": {"current": 20, "max": 20},
            "spellcasting": {"known": ["sacred-flame"]},
            "equipment": [],
        }
    )


class FakeContext:
    """够用的房间上下文：一张角色卡 + 一个可选的施法者。"""

    def __init__(self, tmp_path: Path, cards: list[dict] | None = None):
        self.room_dir = tmp_path
        self.room_id = "dnd5e-chain"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._cards = cards or []

    def room_info(self) -> dict:
        members = [
            {
                "user_id": index + 1,
                "username": card.get("name"),
                "is_active": True,
                "character_card": card,
                "character_state": {},
            }
            for index, card in enumerate(self._cards)
        ]
        return {
            "id": self.room_id,
            "ruleset_id": "dnd5e",
            "house_rules": {},
            "members": members,
        }


def _start_combat(context: FakeContext, extra: list[dict] | None = None) -> dict:
    participants: list[dict] = list(context._cards and [card.get("name") for card in context._cards])
    for monster_id in extra or ["goblin"]:
        participants.append(
            {"name": monster_id.title(), "monster_id": monster_id, "is_npc": True, "side": "enemy"}
        )
    started = combat.start_combat({"participants": participants}, context)
    assert "error" not in started, started
    return started


def _message(result: dict) -> dict:
    message = result.get("visible_message")
    assert isinstance(message, dict), result
    return message


def _metadata(result: dict) -> dict:
    return _message(result).get("metadata") or {}


def _chain(result: dict) -> dict:
    chain = _metadata(result).get("combat_log")
    assert isinstance(chain, dict), _metadata(result)
    return chain


# ---------------------------------------------------------------------------
# 1. 命中：攻击式 → vs AC → 命中 → 伤害 → HP
# ---------------------------------------------------------------------------
def test_attack_hit_chain_carries_ac_and_hp_delta(tmp_path, plugins, monkeypatch):
    monkeypatch.setattr(combat, "_roller", lambda sides: 12 if sides == 20 else 4)
    context = FakeContext(tmp_path, [_fighter_card()])
    _start_combat(context)

    result = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 18, "request_id": "chain-hit"},
        context,
    )
    assert result.get("success") is True, result

    chain = _chain(result)
    assert CHAIN_KEYS.issubset(chain), sorted(CHAIN_KEYS - set(chain))
    assert chain["kind"] == "attack"
    assert chain["attacker"] == "Aria"
    assert chain["target"] == "Goblin"
    assert chain["expression"] == "1d20"
    assert chain["roll"] == 18
    assert chain["modifier"] == result["attack_bonus"]
    assert chain["total"] == 18 + result["attack_bonus"]
    assert chain["compare"] == {"label": "AC", "value": result["target_ac"]}
    # 攻击段的别名：前端/测试都按这两个名字读，避免嵌套判空。
    assert chain["attack_total"] == chain["total"]
    assert chain["target_ac"] == result["target_ac"]
    assert chain["hit"] is True
    assert chain["outcome"] == "hit"
    # 伤害与 HP 段：单卡闭环必须能画出 a→b。
    assert chain["damage"] == result["damage"]["effective"]
    assert chain["damage_type"] == "slashing"
    assert chain["damage_roll"]
    assert chain["hp_before"] == result["damage"]["hp_before"]
    assert chain["hp_after"] == result["damage"]["hp_after"]
    assert chain["hp_delta"] == chain["hp_after"] - chain["hp_before"]
    assert isinstance(chain["notes"], list)

    # 骰桌动画链路：dice_roll 原样保留，且与 combat_log 并存于同一条消息。
    dice = _metadata(result)["dice_roll"]
    assert dice["mode"] == "d20"
    assert dice["roll"] == 18
    assert dice["target"] == result["target_ac"]


# ---------------------------------------------------------------------------
# 2. 未命中：链止于 hit:false
# ---------------------------------------------------------------------------
def test_attack_miss_chain_stops_at_hit_false(tmp_path, plugins, monkeypatch):
    monkeypatch.setattr(combat, "_roller", lambda sides: 12 if sides == 20 else 4)
    context = FakeContext(tmp_path, [_fighter_card()])
    _start_combat(context)

    result = combat.attack_combatant(
        {"attacker": "Aria", "target": "Goblin", "attack_roll": 2, "request_id": "chain-miss"},
        context,
    )
    assert result.get("success") is False, result

    chain = _chain(result)
    assert CHAIN_KEYS.issubset(chain)
    assert chain["hit"] is False
    assert chain["outcome"] == "miss"
    # 没有伤害就没有伤害/HP 段：宁可留空，也不写 0 骗人。
    assert chain["damage"] is None
    assert chain["damage_roll"] is None
    assert chain["hp_before"] is None
    assert chain["hp_after"] is None
    assert chain["hp_delta"] is None
    # 对比段仍然完整：玩家要看的是「差多少」。
    assert chain["total"] < chain["target_ac"]


# ---------------------------------------------------------------------------
# 3. 豁免：success 与 failure 两种链形态
# ---------------------------------------------------------------------------
def _cast_sacred_flame(tmp_path: Path, faces: dict[int, int]) -> dict:
    """掷骰面可控地施放一次圣火（DEX 豁免）。"""
    original = combat._roller
    original_spell = spell_tools._roller
    roller = lambda sides: faces.get(sides, 1)  # noqa: E731
    combat._roller = roller
    spell_tools._roller = roller
    try:
        context = FakeContext(tmp_path, [_wizard_card()])
        _start_combat(context)
        return spell_tools.cast_spell(
            {
                "caster": "Mira",
                "spell": "sacred-flame",
                "targets": ["Goblin"],
                "request_id": "chain-save",
            },
            context,
        )
    finally:
        combat._roller = original
        spell_tools._roller = original_spell


def _save_message(result: dict) -> dict:
    messages = [item for item in (result.get("direct_messages") or []) if isinstance(item, dict)]
    saves = [
        item
        for item in messages
        if (item.get("metadata") or {}).get("check_type") == "spell_save"
    ]
    assert saves, messages
    return saves[0]


def test_save_failed_chain_reaches_hp(tmp_path, plugins):
    # d20=1 → 豁免必失败 → 命中并落伤害。
    result = _cast_sacred_flame(tmp_path, {20: 1})
    assert not result.get("error"), result
    message = _save_message(result)
    chain = message["metadata"]["combat_log"]

    assert CHAIN_KEYS.issubset(chain), sorted(CHAIN_KEYS - set(chain))
    assert chain["kind"] == "save"
    assert "圣火" in chain["spell"]
    assert chain["target"] == "Goblin"
    assert chain["compare"]["label"] == "DC"
    assert chain["dc"] == chain["compare"]["value"]
    assert chain["save_ability"] == "dex"
    assert chain["success"] is False
    assert chain["hit"] is True
    assert chain["outcome"] == "save_failed"
    assert chain["roll"] == 1
    assert chain["damage"] is not None and chain["damage"] > 0
    assert chain["hp_before"] is not None
    assert chain["hp_after"] == chain["hp_before"] - chain["damage"]
    assert chain["hp_delta"] == -chain["damage"]
    # 豁免消息的骰桌元数据不受影响。
    assert message["metadata"]["dice_roll"]["mode"] == "d20"


def test_save_succeeded_chain_has_no_damage(tmp_path, plugins):
    # d20=20 → 豁免成功；圣火成功豁免不受伤 → 链止于豁免成功。
    result = _cast_sacred_flame(tmp_path, {20: 20})
    assert not result.get("error"), result
    message = _save_message(result)
    chain = message["metadata"]["combat_log"]

    assert CHAIN_KEYS.issubset(chain)
    assert chain["success"] is True
    assert chain["hit"] is False
    assert chain["outcome"] == "save_succeeded"
    assert chain["roll"] == 20
    assert chain["total"] is not None and chain["total"] >= chain["dc"]
    assert chain["damage"] is None
    assert chain["hp_before"] is None
    assert chain["hp_after"] is None


# ---------------------------------------------------------------------------
# 兼容：识别不了的结算不产出 combat_log；纯函数不因脏输入抛错
# ---------------------------------------------------------------------------
def test_unrecognised_result_yields_no_chain():
    assert attack_chain({"error": "boom"}) is None
    assert attack_chain({}) is None
    assert attack_chain(None) is None
    assert save_chain(None) is None
    assert save_chain({}) is None


def test_chain_builder_is_pure_and_tolerates_partial_damage():
    """伤害缺 HP 字段时链仍成立，只把缺的段留空。"""
    chain = attack_chain(
        {
            "attack_mode": "d20",
            "attacker": "A",
            "target": "B",
            "weapon": "剑",
            "expression": "1d20",
            "rolls": [14],
            "natural": 14,
            "attack_bonus": 3,
            "total": 17,
            "target_ac": 12,
            "success": True,
            "damage": {"damage": 5, "effective": 5, "damage_type": "piercing"},
        }
    )
    assert chain["hit"] is True
    assert chain["damage"] == 5
    assert chain["hp_before"] is None
    assert chain["hp_delta"] is None
