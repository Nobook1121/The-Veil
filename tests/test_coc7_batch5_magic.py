"""批次 5 法术与典籍：纯规则、卡面扩展、施法结算与控制台/工具接线测试。"""

from __future__ import annotations

from trpg_server import room_magic
from trpg_server.agents.tools import magic as magic_tools
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import magic as magic_rules
from trpg_server.ruleset.coc7.options import resolve_options


def _info(house_rules=None, *, pow_value=50, mythos=0, san=50, mp=10, hp=10):
    return {
        "room_id": "magic-batch5",
        "house_rules": house_rules or {},
        "members": [
            {
                "user_id": "p1-id",
                "username": "哈维",
                "character_card": {
                    "name": "哈维",
                    "rulesetId": "coc7",
                    "attributes": {"dex": 60, "str": 50, "siz": 50, "con": 50, "pow": pow_value, "int": 60},
                    "skills": [{"name": "克苏鲁神话", "value": mythos}],
                },
                "character_state": {
                    "current_hp": hp,
                    "max_hp": hp,
                    "current_mp": mp,
                    "max_mp": pow_value // 5,
                    "current_san": san,
                    "max_san": 99 - mythos,
                },
            }
        ],
    }


def _member(tmp_path):
    info = load_room_info(tmp_path)
    return info, info["members"][0]


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "magic-batch5"
        self.config_dir = tmp_path / "config"
        self.username = "房主"
        self.user_id = "owner-id"
        self._house_rules = house_rules or {}

    def room_info(self):
        info = load_room_info(self.room_dir) or {}
        info.setdefault("house_rules", self._house_rules)
        return info


def _run(words, tmp_path):
    return handle_room_rule_command(
        words,
        room_id="magic-batch5",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )


# ---------------------------------------------------------------------------
# 纯规则
# ---------------------------------------------------------------------------
def test_magic_switch_defaults_off():
    assert resolve_options(None).magic_enabled is False
    assert resolve_options({"house_rules": {"coc7_magic_enabled": True}}).magic_enabled is True


def test_mp_math_and_recovery():
    assert magic_rules.max_mp(50) == 10
    assert magic_rules.max_mp(0) == 0
    assert magic_rules.mp_recovery(5) == 5
    assert magic_rules.mp_recovery(5, 100) == 10
    split = magic_rules.mp_cost_split(4, 1, 10)
    assert split == {"mp": 1, "hp": 3, "unpaid": 0}
    assert magic_rules.mp_cost_split(4, 0, 2)["unpaid"] == 2


def test_first_cast_and_push_math():
    assert magic_rules.first_cast_target(55) == 27
    assert magic_rules.push_multiplier(0) == 1
    assert magic_rules.push_multiplier(4) == 4
    assert magic_rules.push_multiplier(9) == 6
    assert magic_rules.pushed_cost(3, 4) == 12
    assert magic_rules.push_side_effect(1)
    assert magic_rules.push_side_effect(8, strong=True)


def test_learning_and_grimoire_math():
    assert magic_rules.learning_weeks(7) == 7
    assert magic_rules.learning_weeks(99) == 12
    assert magic_rules.grimoire_mythos_gain(mythos=5, rating=10, cmi=1, cmf=3) == 3
    assert magic_rules.grimoire_mythos_gain(mythos=10, rating=10, cmi=1, cmf=3) == 1
    assert magic_rules.grimoire_weeks(4, 0) == 4
    assert magic_rules.grimoire_weeks(4, 2) == 16
    assert magic_rules.grimoire_lookup_success(20, 30) is True
    assert magic_rules.grimoire_lookup_success(31, 30) is False
    assert magic_rules.belief_san_loss(12) == 12


# ---------------------------------------------------------------------------
# 卡面扩展与施法
# ---------------------------------------------------------------------------
def test_learn_spell_creates_extensions_on_old_card(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    assert "extensions" not in member["character_card"]
    payload = room_magic.learn_spell(tmp_path, info, member, "折磨术", weeks=7)
    assert payload["spells"] == ["折磨术"]
    saved = load_room_info(tmp_path)["members"][0]["character_card"]
    assert saved["extensions"]["magic"]["spells"][0]["learned_weeks"] == 7
    assert saved["extensions"]["magic"]["spells"][0]["first_cast_done"] is False


def test_first_cast_failure_has_no_effect_no_cost(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    room_magic.learn_spell(tmp_path, info, member, "折磨术", weeks=2)
    payload = room_magic.cast_spell(
        tmp_path, info, member, name="折磨术", mp_cost=3, success_level="failure"
    )
    assert payload["effects"] is False
    assert payload["mp_spent"] == 0
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_mp"] == 10
    assert saved["character_card"]["extensions"]["magic"]["spells"][0]["first_cast_done"] is False


def test_first_cast_success_spends_and_marks_done(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    room_magic.learn_spell(tmp_path, info, member, "折磨术", weeks=2)
    payload = room_magic.cast_spell(
        tmp_path, info, member, name="折磨术", mp_cost=3, san_cost=2, success_level="regular"
    )
    assert payload["effects"] is True
    assert payload["mp_spent"] == 3
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_mp"] == 7
    assert saved["character_state"]["current_san"] == 48
    assert saved["character_card"]["extensions"]["magic"]["spells"][0]["first_cast_done"] is True
    # 第二次施法不再需要 POW 检定。
    second = room_magic.cast_spell(tmp_path, info, member, name="折磨术", mp_cost=1)
    assert second["first_cast_check"] is False
    assert second["effects"] is True


def test_mp_overflow_costs_hp(tmp_path):
    save_room_info(tmp_path, _info(mp=1))
    info, member = _member(tmp_path)
    room_magic.learn_spell(tmp_path, info, member, "折磨术", weeks=2)
    payload = room_magic.cast_spell(
        tmp_path, info, member, name="折磨术", mp_cost=4, success_level="regular"
    )
    assert payload["mp_spent"] == 1
    assert payload["hp_spent"] == 3
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_mp"] == 0
    assert saved["character_state"]["current_hp"] == 7


def test_pushed_failure_still_works_with_extra_cost(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    room_magic.learn_spell(tmp_path, info, member, "折磨术", weeks=2)
    payload = room_magic.cast_spell(
        tmp_path,
        info,
        member,
        name="折磨术",
        mp_cost=2,
        success_level="failure",
        pushed=True,
        multiplier=3,
    )
    assert payload["effects"] is True
    assert payload["extra_cost"] == 6
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_mp"] == 2
    assert payload["side_effect"]


# ---------------------------------------------------------------------------
# 典籍
# ---------------------------------------------------------------------------
def test_study_gains_cmf_below_rating_and_reduces_san_cap(tmp_path):
    save_room_info(tmp_path, _info(mythos=5, san=50))
    info, member = _member(tmp_path)
    payload = room_magic.read_grimoire(
        tmp_path, info, member, title="《蠕虫之秘》", mode="study",
        weeks=4, san_loss=4, cmi=1, cmf=3, rating=10,
    )
    assert payload["mythos_gain"] == 3
    saved = load_room_info(tmp_path)["members"][0]
    mythos = next(item for item in saved["character_card"]["skills"] if item["name"] == "克苏鲁神话")
    assert mythos["value"] == 8
    assert saved["character_state"]["max_san"] == 91
    assert saved["character_state"]["current_san"] == 46
    assert saved["character_card"]["extensions"]["magic"]["grimoires"]["《蠕虫之秘》"]["reads"] == 1


def test_study_gains_cmi_at_or_above_rating(tmp_path):
    save_room_info(tmp_path, _info(mythos=10, san=50))
    info, member = _member(tmp_path)
    payload = room_magic.read_grimoire(
        tmp_path, info, member, title="《无名祭祀书》", mode="study",
        weeks=4, san_loss=0, cmi=1, cmf=5, rating=10,
    )
    assert payload["mythos_gain"] == 1


def test_skim_failure_grants_nothing(tmp_path):
    save_room_info(tmp_path, _info(mythos=2))
    info, member = _member(tmp_path)
    payload = room_magic.read_grimoire(
        tmp_path, info, member, title="《死灵之书》", mode="skim", cmi=4, san_loss=2,
        success_level="failure",
    )
    assert payload["success"] is False
    assert payload["mythos_gain"] == 0
    saved = load_room_info(tmp_path)["members"][0]
    assert saved["character_state"]["current_san"] == 50


def test_grimoire_can_register_learned_spells(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    room_magic.read_grimoire(
        tmp_path, info, member, title="《死灵之书》", mode="study",
        weeks=8, cmi=2, cmf=4, rating=0, spells=["召唤术", "束缚术"],
    )
    saved = load_room_info(tmp_path)["members"][0]["character_card"]
    names = [item["name"] for item in saved["extensions"]["magic"]["spells"]]
    assert names == ["召唤术", "束缚术"]


def test_magic_requires_learned_spell(tmp_path):
    save_room_info(tmp_path, _info())
    info, member = _member(tmp_path)
    try:
        room_magic.cast_spell(tmp_path, info, member, name="未学法术", mp_cost=1)
    except Exception as exc:  # noqa: BLE001
        assert "尚未学会" in str(exc)
    else:
        raise AssertionError("未登记的法术不应能施放")


# ---------------------------------------------------------------------------
# 控制台与工具
# ---------------------------------------------------------------------------
def test_spell_command_requires_switch(tmp_path):
    save_room_info(tmp_path, _info())
    ok, text = _run(["spell", "哈维"], tmp_path)
    assert ok is False
    assert "rule spell" in text


def test_spell_command_learn_and_cast(tmp_path):
    save_room_info(tmp_path, _info(house_rules={"coc7_magic_enabled": True}))
    ok, text = _run(["spell", "learn", "哈维", "折磨术", "weeks", "7"], tmp_path)
    assert ok is True, text
    assert "学会" in text
    ok, text = _run(
        ["spell", "cast", "哈维", "折磨术", "mp", "3", "result", "failure"], tmp_path
    )
    assert ok is True, text
    assert "没有效果" in text
    ok, text = _run(
        ["spell", "cast", "哈维", "折磨术", "mp", "3", "result", "regular"], tmp_path
    )
    assert ok is True, text
    assert "生效" in text
    saved = load_room_info(tmp_path)["members"][0]["character_state"]
    assert saved["current_mp"] == 7


def test_spell_command_grimoire(tmp_path):
    save_room_info(tmp_path, _info(house_rules={"coc7_magic_enabled": True}))
    ok, text = _run(
        ["spell", "grimoire", "哈维", "《蠕虫之秘》", "study", "weeks", "4",
         "san", "3", "cmi", "1", "cmf", "2", "rating", "10", "spells", "召唤术"],
        tmp_path,
    )
    assert ok is True, text
    assert "克苏鲁神话 +2" in text
    saved = load_room_info(tmp_path)["members"][0]["character_card"]
    assert "召唤术" in [item["name"] for item in saved["extensions"]["magic"]["spells"]]


def test_magic_tools_respect_switch(tmp_path):
    save_room_info(tmp_path, _info())
    context = FakeContext(tmp_path)
    result = magic_tools.execute_magic_status({"player": "哈维"}, context)
    assert result["ok"] is False
    assert "disabled" in result["error"]

    save_room_info(tmp_path, _info(house_rules={"coc7_magic_enabled": True}))
    learned = magic_tools.execute_magic_learn(
        {"player": "哈维", "spell": "折磨术", "weeks": 2}, context
    )
    assert learned["ok"] is True
    cast = magic_tools.execute_magic_cast(
        {"player": "哈维", "spell": "折磨术", "mp_cost": 2, "result": "regular"}, context
    )
    assert cast["ok"] is True
    assert cast["cast"]["effects"] is True
