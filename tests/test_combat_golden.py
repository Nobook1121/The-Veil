"""coc7 房间战斗链路金样（规则集边界收敛 B1 护栏）。

三条确定性链路（真战斗工具 + SeqRng 固定 ``combat._roller``，coc7 房间）：

1. **对抗先攻** ``[25, 10]``：``combat.start`` 携带 ``initiative_check``，
   两名战斗者各掷一颗 d100；coc7 等级带给出 hard（中性带会是 regular）。
2. **攻击命中** ``[15, 40, 3, 20]``：近战对抗（攻击骰 / 闪避骰 / 1D4 伤害 /
   重伤 CON 检定各一颗），含 DamageOutcome 全字段与战斗面板日志文字。
3. **伤害结算** ``[5, 4]``：``combat.damage`` 的 1D6 伤害骰 + 重伤 CON 检定。

每条链路录制「工具返回结果 + 战斗快照」，快照中的战斗 uuid 与时间戳归一化
后与 ``fixtures/combat_golden.json`` 逐字比对。B1 修复（战斗掷骰 / 成功
等级按战斗会话规则集显式解析）不得改变 coc7 房间的任何输出字节；等级带、
阈值、文案漂移都会在本护栏上直接红。

更新金样：``UPDATE_GOLDEN=1 pytest tests/test_combat_golden.py``
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trpg_server.agents.tools import combat

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "combat_golden.json"


class SeqRng:
    """按固定序列返回骰值的确定性随机源（超出骰面时夹紧）。"""

    def __init__(self, values: list[int]):
        self.values = list(values)
        self.calls: list[tuple[int, int]] = []

    def __call__(self, sides: int) -> int:
        value = self.values[len(self.calls) % len(self.values)]
        self.calls.append((sides, value))
        return max(1, min(int(sides), int(value)))


class FakeContext:
    """战斗工具的最小房间上下文；本金样固定 coc7 房间。"""

    def __init__(self, tmp_path: Path):
        self.room_dir = tmp_path
        self.room_id = "combat-golden"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": "coc7",
            "house_rules": {},
            "members": [],
        }


def _spec(name: str, **overrides) -> dict:
    spec = {
        "name": name,
        "is_npc": True,
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 10,
        "max_hp": 10,
        "skills": {"Fighting (Brawl)": 50},
        "weapons": [
            {"name": "Knife", "skill": "Fighting (Brawl)", "damage": "1D4+2", "attacks": "1"},
        ],
    }
    spec.update(overrides)
    return spec


def _normalize_state(state: dict) -> dict:
    """归一化战斗快照里的 uuid 与时间戳；其余字段逐字进入金样。"""
    normalized = json.loads(json.dumps(state, ensure_ascii=False))
    if normalized.get("id"):
        normalized["id"] = "<combat-id>"
    if normalized.get("started_at"):
        normalized["started_at"] = "<timestamp>"
    for entry in normalized.get("log") or []:
        if isinstance(entry, dict) and entry.get("time"):
            entry["time"] = "<timestamp>"
    return normalized


def _chain_initiative(tmp_path: Path) -> dict:
    context = FakeContext(tmp_path / "initiative")
    (tmp_path / "initiative").mkdir()
    rng = SeqRng([25, 10])
    combat._roller = rng
    result = combat.start_combat(
        {"participants": [_spec("Alpha", dex=60), _spec("Beta", dex=40)], "initiative_check": True},
        context,
    )
    assert "error" not in result, result
    assert rng.calls == [(100, 25), (100, 10)]
    return {"result": result, "state": _normalize_state(combat._load_combat(context))}


def _chain_attack(tmp_path: Path) -> dict:
    context = FakeContext(tmp_path / "attack")
    (tmp_path / "attack").mkdir()
    rng = SeqRng([15, 40, 3, 20])
    combat._roller = rng
    started = combat.start_combat(
        {"participants": [_spec("Hunter", dex=60), _spec("Prey", dex=40)]}, context
    )
    assert "error" not in started, started
    result = combat.attack_combatant(
        {"attacker": "Hunter", "target": "Prey", "weapon": "Knife", "defense": "dodge"},
        context,
    )
    assert "error" not in result, result
    assert rng.calls == [(100, 15), (100, 40), (4, 3), (100, 20)]
    return {"result": result, "state": _normalize_state(combat._load_combat(context))}


def _chain_damage(tmp_path: Path) -> dict:
    context = FakeContext(tmp_path / "damage")
    (tmp_path / "damage").mkdir()
    rng = SeqRng([5, 4])
    combat._roller = rng
    started = combat.start_combat(
        {"participants": [_spec("Assailant", dex=60), _spec("Victim", dex=40)]}, context
    )
    assert "error" not in started, started
    result = combat.damage_combatant({"target": "Victim", "dice": "1D6"}, context)
    assert "error" not in result, result
    assert rng.calls == [(6, 5), (100, 4)]
    return {"result": result, "state": _normalize_state(combat._load_combat(context))}


def _build_snapshot(tmp_path: Path) -> dict:
    return {
        "initiative": _chain_initiative(tmp_path),
        "attack": _chain_attack(tmp_path),
        "damage": _chain_damage(tmp_path),
    }


def _assert_same(expected, actual, path: str = "") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        assert set(actual) == set(expected), f"{path}: key mismatch {sorted(set(actual) ^ set(expected))}"
        for key in expected:
            _assert_same(expected[key], actual[key], f"{path}.{key}")
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


@pytest.fixture(autouse=True)
def _restore_roller():
    """金样用例替换 ``combat._roller``；结束后恢复真随机源。"""
    original = combat._roller
    yield
    combat._roller = original


def test_combat_golden_matches_baseline(tmp_path):
    actual = json.loads(json.dumps(_build_snapshot(tmp_path), ensure_ascii=False))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        pytest.skip("golden baseline updated")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    _assert_same(expected, actual)
