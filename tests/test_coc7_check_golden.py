"""CoC7 检定行为金样（规则集边界搬迁基线）。

覆盖：技能 / 属性取值、检定名解析、难度归一、成功等级、奖励 / 惩罚骰、
房间检定、SAN 检定、疯狂判定与 KP 工具输出文字 / 结构。

搬迁到插件后必须逐字一致；本文件不允许为迁就实现而修改期望值。
新增路径若未被覆盖，应在阶段报告中点名。

更新金样：``UPDATE_GOLDEN=1 pytest tests/test_coc7_check_golden.py``
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "coc7_check_golden.json"

NAMES = [
    "STR",
    "str",
    "力量",
    "敏捷",
    "LUC",
    "幸运",
    "luck",
    "AGE",
    "年龄",
    "侦察",
    "侦查",
    "观察",
    "聆听",
    "图书馆",
    "图书馆使用",
    "克苏鲁神话",
    "cthulhumythos",
    "spotHidden",
    "SpotHidden",
    "射击（手枪）",
    "射击(手枪)",
    "firearmsHandgun",
    "不存在的技能",
    "",
]

RESOLVE_CASES = [
    {"skill": "侦察"},
    {"name": "力量"},
    {"skill": "不存在", "name": "聆听"},
    {"name": "STR", "check_name": "敏捷"},
    {},
]

DIFFICULTY_VALUES = [None, "", "regular", "hard", "extreme", "普通", "困难", "极难", "HARD", "困难检定", "abc"]

ATTRIBUTE_VALUES = ["STR", "str", "力量", "敏捷", "luck", "LUC", "AGE", "年龄", "DEX检定", "不存在", ""]


class SeqRng:
    """按固定序列返回骰值的确定性随机源（超出骰面时夹紧）。"""

    def __init__(self, values: list[int]):
        self.values = list(values)
        self.calls = 0

    def __call__(self, sides: int) -> int:
        value = self.values[self.calls % len(self.values)]
        self.calls += 1
        return max(1, min(int(sides), int(value)))


def _card() -> dict:
    return {
        "id": "golden-card",
        "name": "测试调查员",
        "attributes": {
            "STR": 50,
            "DEX": 60,
            "CON": 55,
            "APP": 40,
            "POW": 65,
            "SIZ": 60,
            "EDU": 70,
            "INT": 75,
            "LUC": 45,
        },
        "skills": [
            {"name": "侦察", "skillKey": "spotHidden", "value": 65},
            {"name": "聆听", "skillKey": "listen", "value": 55},
            {"name": "克苏鲁神话", "skillKey": "cthulhumythos", "value": 12},
            {"name": "射击", "skillKey": "firearmsHandgun", "value": 70},
            {"name": "图书馆使用", "skillKey": "libraryUse", "value": 60},
            {"name": "信用评级", "skillKey": "creditRating", "value": 50},
        ],
        "currentHp": 11,
        "maxHp": 11,
        "currentSan": 60,
        "maxSan": 99,
        "initialSan": 65,
        "currentMp": 13,
        "maxMp": 13,
    }


def _member() -> dict:
    return {
        "user_id": "u-golden",
        "username": "alice",
        "character_name": "测试调查员",
        "character_card": _card(),
        "character_state": {
            "max_hp": 11,
            "current_hp": 11,
            "max_san": 99,
            "current_san": 60,
            "initial_san": 65,
            "max_mp": 13,
            "current_mp": 13,
        },
        "is_active": True,
    }


def _context(member: dict):
    from trpg_server.room_rules import RoomRuleContext

    return RoomRuleContext(None, {"ruleset_id": "coc7", "game_date": "1925-01-01", "members": [member]})


def _success_level_rows(dice) -> dict:
    rows = {}
    for target in (1, 20, 49, 50, 51, 65, 99, 100):
        for critical, fumble in ((1, 96), (5, 95), (0, 100)):
            for roll in range(1, 101):
                rows[f"{target}/{critical}/{fumble}/{roll}"] = dice._success_level(roll, target, critical, fumble)
    return rows


def _build_snapshot() -> dict:
    from trpg_server import room_rules
    from trpg_server.agents.tools import dice
    from trpg_server.ruleset.coc7 import checks as coc7_checks

    card = _card()
    member = _member()
    context = _context(member)

    snapshot: dict = {}
    snapshot["lookup"] = {name: dice._lookup_check_value(card, name) for name in NAMES}
    snapshot["known"] = {name: dice._is_known_check_name(name) for name in NAMES}
    snapshot["resolve"] = {str(index): dice._resolve_check_name(case) for index, case in enumerate(RESOLVE_CASES)}
    snapshot["localize"] = {name: dice._localize_check_label(name) for name in NAMES}
    snapshot["display"] = {name: dice._resolve_check_display_name(card, name) for name in NAMES}
    snapshot["attribute_key"] = {value: room_rules._normalize_attribute_key(value) for value in ATTRIBUTE_VALUES}
    snapshot["normalize_difficulty"] = {
        str(value): dice._normalize_difficulty(value) for value in DIFFICULTY_VALUES
    }
    snapshot["difficulty_labels"] = {key: dice.DIFFICULTY_LABELS[key] for key in ("regular", "hard", "extreme")}
    snapshot["level_labels"] = {
        key: dice.LEVEL_LABELS[key]
        for key in ("critical", "extreme", "hard", "regular", "failure", "fumble")
    }
    snapshot["default_thresholds"] = list(dice._context_thresholds(context))
    snapshot["threshold"] = {
        f"{target}/{difficulty}": dice._threshold(target, difficulty)
        for target in (1, 5, 10, 25, 49, 50, 65, 99, 100)
        for difficulty in ("regular", "hard", "extreme")
    }
    snapshot["success_level"] = _success_level_rows(dice)
    snapshot["bonus_penalty"] = {
        f"{bonus}/{penalty}": list(dice._normalize_bonus_penalty(bonus, penalty))
        for bonus in range(0, 5)
        for penalty in range(0, 5)
    }
    snapshot["level_suffix"] = {level: dice._level_suffix(level) for level in ("critical", "extreme", "hard", "regular", "failure", "fumble")}
    snapshot["room_summary"] = {
        "regular": dice._format_room_check_summary("侦察", "regular", 42, 65, True),
        "hard_failure": dice._format_room_check_summary("力量", "hard", 90, 25, False),
        "extreme_critical": dice._format_room_check_summary("聆听", "extreme", 3, 11, True),
    }
    snapshot["percentile_faces"] = {
        f"{faces}/{bonus}/{penalty}": list(dice._percentile_from_faces(faces, bonus, penalty))
        for faces in ([3, 7], [0, 0], [9, 5], [2, 4, 8], [6, 1, 3, 9])
        for bonus, penalty in ((0, 0), (1, 0), (0, 1), (2, 0), (0, 2), (1, 1), (3, 0))
    }
    snapshot["coc_check"] = {
        "regular": dice.roll_coc_check({"target": 65, "difficulty": "regular", "name": "侦察"}, faces=[3, 7]),
        "hard_bonus": dice.roll_coc_check(
            {"target": 65, "difficulty": "困难", "name": "侦察", "bonus_dice": 1, "reason": "调查线索"},
            faces=[3, 7, 8],
        ),
        "extreme_penalty": dice.roll_coc_check(
            {"target": 99, "difficulty": "extreme", "penalty": 2, "adjustment": "+10"},
            faces=[9, 5, 2, 1],
        ),
        "invalid_target": dice.roll_coc_check({"target": 0, "difficulty": "regular"}),
        "invalid_difficulty": dice.roll_coc_check({"target": 50, "difficulty": "abc"}),
    }
    snapshot["room_check"] = {
        "skill": dice.roll_room_check(
            {"player_name": "alice", "name": "侦察", "difficulty": "regular"}, _context(_member()), faces=[3, 7]
        ),
        "attribute_hard": dice.roll_room_check(
            {"player_name": "alice", "name": "力量", "difficulty": "hard", "bonus_dice": 1},
            _context(_member()),
            faces=[2, 4, 8],
        ),
        "unknown_member": dice.roll_room_check({"player_name": "bob", "name": "侦察"}, _context(_member())),
        "unknown_skill": dice.roll_room_check(
            {"player_name": "alice", "name": "不存在的技能", "difficulty": "regular"}, _context(_member())
        ),
    }
    snapshot["sanity_check"] = {
        "failure": dice.roll_sanity_check(
            {"player_name": "alice", "san_change": "1/1d6", "game_date": "1925-01-01"},
            _context(_member()),
            rng=SeqRng([3]),
            faces=[9, 9],
        ),
        "success": dice.roll_sanity_check(
            {"player_name": "alice", "san_change": "1d4/1d6", "game_date": "1925-01-01"},
            _context(_member()),
            rng=SeqRng([2, 4]),
            faces=[1, 2],
        ),
        "bonus": dice.roll_sanity_check(
            {"player_name": "alice", "san_change": "1/1d6", "game_date": "1925-01-01", "bonus_dice": 1},
            _context(_member()),
            rng=SeqRng([5]),
            faces=[6, 8, 2],
        ),
    }
    snapshot["madness"] = {
        "skipped": dice.roll_madness(
            {"player_name": "alice", "loss": 1, "game_date": "1925-01-01"}, _context(_member()), rng=SeqRng([50])
        ),
        "triggered": dice.roll_madness(
            {"player_name": "alice", "loss": 8, "force": True, "game_date": "1925-01-01"},
            _context(_member()),
            rng=SeqRng([50, 3, 7]),
        ),
        "int_roll": dice.roll_madness(
            {"player_name": "alice", "loss": 8, "force": True, "game_date": "1925-01-01", "bonus_dice": 1},
            _context(_member()),
            rng=SeqRng([90, 4, 2, 9]),
        ),
    }
    checks = coc7_checks.Coc7Checks()
    snapshot["contact_plan"] = {
        "local": checks.contact_plan(
            _card(), "信用评级", in_hometown=True, remote=False, same_profession=True
        ),
        "remote": checks.contact_plan(
            _card(), "信用评级", in_hometown=False, remote=True, same_profession=False
        ),
    }
    snapshot["contact_outcome"] = {
        "success": checks.contact_outcome(True, pushed=False),
        "failure": checks.contact_outcome(False, pushed=False),
        "pushed": checks.contact_outcome(False, pushed=True),
    }
    snapshot["social_difficulty"] = {
        f"{skill}/{attitude}/{leverage}": checks.social_difficulty(
            _card(), target_skill=skill, attitude=attitude, leverage=leverage
        )
        for skill in (50, 70, 90)
        for attitude in ("hostile", "opposed", "neutral", "supportive")
        for leverage in (False, True)
    }
    snapshot["combined_check"] = checks.combined_check(
        _card(), ["侦察", "聆听"], mode="all", difficulty="hard", bonus=1, faces=[2, 4, 8]
    )
    snapshot["group_luck"] = checks.group_luck(
        [_member(), {**_member(), "user_id": "u-2", "username": "bob", "character_card": {**_card(), "attributes": {**_card()["attributes"], "LUC": 30}}}],
        faces=[4, 2],
    )
    return snapshot


def _assert_same(expected, actual, path: str = "") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        assert set(actual) == set(expected), f"{path}: key mismatch {sorted(set(actual) ^ set(expected))}"
        for key in expected:
            _assert_same(expected[key], actual[key], f"{path}.{key}")
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_coc7_check_golden_matches_baseline():
    actual = json.loads(json.dumps(_build_snapshot(), ensure_ascii=False))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        pytest.skip("golden baseline updated")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    _assert_same(expected, actual)
