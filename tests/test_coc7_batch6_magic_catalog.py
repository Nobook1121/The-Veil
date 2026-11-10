"""批次 6 法术 / 典籍内容表：数据完整性、按需查询、开关与控制台接线。"""

from __future__ import annotations

from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools import magic as magic_tools
from trpg_server.room_rule_commands import handle_room_rule_command
from trpg_server.room_rules import load_room_info, save_room_info
from trpg_server.ruleset.coc7 import magic_content
from trpg_server.ruleset.coc7.options import resolve_options


def _info(house_rules=None, *, pow_value=50, mythos=0, san=50, mp=10, hp=10):
    return {
        "room_id": "magic-batch6",
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


class FakeContext:
    def __init__(self, tmp_path, house_rules=None):
        self.room_dir = tmp_path
        self.room_id = "magic-batch6"
        self.config_dir = tmp_path / "config"
        self.username = "房主"
        self.user_id = "owner-id"

    def room_info(self):
        return load_room_info(self.room_dir) or {}


def _run(words, tmp_path):
    return handle_room_rule_command(
        words,
        room_id="magic-batch6",
        room_dir=tmp_path,
        actor="房主",
        actor_id="owner-id",
        actor_rank=3,
    )


# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------
def test_catalog_files_are_present_and_structured():
    stats = magic_content.catalog_stats()
    assert stats["spells"] > 90
    assert stats["grimoires"] > 90

    spells = magic_content.spells()
    for entry in spells:
        assert entry.get("name")
        assert isinstance(entry.get("page"), int) and entry["page"] > 0
        assert isinstance(entry.get("cost_by_unit"), (dict, type(None)))
        assert isinstance(entry.get("check"), dict)

    # 只有章节标题条目没有独立消耗，其余法术都能解析出消耗原文。
    with_cost = [entry for entry in spells if magic_content.resolve_cost(entry)["raw"]]
    assert len(with_cost) >= len(spells) - 5

    for book in magic_content.grimoires():
        assert book.get("title")
        assert isinstance(book.get("page"), int) and book["page"] > 0
        assert book.get("editions")
        assert magic_content.compact_grimoire(book, detail=True)["summary"]
        for edition in book["editions"]:
            assert edition.get("language")
            assert "san_loss" in edition


def test_spell_lookup_exact_alias_and_keyword():
    exact = magic_content.get_spell("深渊之息")
    assert exact and exact["name"] == "深渊之息"
    costs = magic_content.spell_costs("深渊之息")
    assert costs["mp"] == 8
    assert costs["san"] is None
    assert costs["units"]["san"] == "1D6"

    # 别名可命中同一法术（规则书给出的旧称）。
    by_alias = magic_content.get_spell("盐卤之吻")
    assert by_alias and by_alias["name"] == "深渊之息"

    names = [item["name"] for item in magic_content.find_spells("联络术", limit=12)]
    assert "深潜者联络术" in names
    assert len(names) > 3


def test_spell_detail_and_filters():
    detail = magic_content.describe_spell("深渊之息")
    assert detail["page"] == 210
    assert "1D6" in detail["cost"]
    assert detail["check"] == "需要检定"
    assert detail["check_detail"]["kinds"] == ["pow_opposed"]
    assert detail["summary"]

    cheap = magic_content.find_spells("", max_mp=2, limit=12)
    assert cheap
    assert all(item["cost"] for item in cheap)

    cheap_san = magic_content.find_spells("", max_san=1, limit=12)
    assert cheap_san
    assert all(item["cost"] for item in cheap_san)


def test_grimoire_lookup_and_language():
    book = magic_content.get_grimoire("伊波恩之书")
    assert book and book["title"] == "伊波恩之书"
    stats = magic_content.grimoire_stats("伊波恩之书", "英文")
    assert stats["language"] == "英文"
    assert stats["cmi"] == 3 and stats["cmf"] == 8 and stats["rating"] == 33

    detail = magic_content.describe_grimoire("伊波恩之书")
    assert detail["page"] == 191
    assert len(detail["editions"]) >= 2
    assert detail["summary"]

    names = [item["title"] for item in magic_content.find_grimoires("死灵", limit=6)]
    assert "死灵之书" in names


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------
def test_catalog_switch_defaults_off_and_lookup_tool_disabled(tmp_path):
    assert resolve_options(None).magic_catalog_enabled is False
    assert (
        resolve_options({"house_rules": {"coc7_magic_catalog_enabled": True}}).magic_catalog_enabled
        is True
    )
    save_room_info(tmp_path, _info())
    result = magic_tools.execute_magic_lookup({"query": "深渊之息"}, FakeContext(tmp_path))
    assert result["ok"] is False
    assert "catalog" in result["error"]

    assert "magic.lookup" in magic_tools.MAGIC_CATALOG_TOOL_NAMES
    assert default_tool_registry().get("magic.lookup") is not None


def test_lookup_tool_returns_compact_results(tmp_path):
    save_room_info(
        tmp_path, _info(house_rules={"coc7_magic_catalog_enabled": True})
    )
    context = FakeContext(tmp_path)
    listed = magic_tools.execute_magic_lookup(
        {"query": "联络术", "kind": "spell", "limit": 5}, context
    )
    assert listed["ok"] is True
    assert listed["count"] == 5
    assert all("cost" in item for item in listed["results"])

    detail = magic_tools.execute_magic_lookup(
        {"query": "深渊之息", "kind": "spell", "detail": True}, context
    )
    assert detail["ok"] is True
    assert detail["spell"]["page"] == 210

    books = magic_tools.execute_magic_lookup(
        {"query": "伊波恩之书", "kind": "grimoire", "detail": True}, context
    )
    assert books["ok"] is True
    assert books["grimoire"]["editions"][0]["cmi"] == 3


# ---------------------------------------------------------------------------
# 与施法 / 研读接通
# ---------------------------------------------------------------------------
def test_cast_use_catalog_fills_only_missing_values(tmp_path):
    save_room_info(
        tmp_path,
        _info(house_rules={"coc7_magic_enabled": True, "coc7_magic_catalog_enabled": True}),
    )
    context = FakeContext(tmp_path)
    magic_tools.execute_magic_learn(
        {"player": "哈维", "spell": "深渊之息", "weeks": 2}, context
    )

    # 显式 mp 优先，内容表只补 SAN（骰值保留给 KP）。
    explicit = magic_tools.execute_magic_cast(
        {
            "player": "哈维",
            "spell": "深渊之息",
            "mp_cost": 2,
            "use_catalog": True,
            "result": "regular",
        },
        context,
    )
    assert explicit["ok"] is True, explicit
    assert "mp" not in explicit["catalog"]["filled"]
    assert explicit["catalog"]["pending"]["san"] == "1D6"
    assert explicit["cast"]["mp_spent"] == 2

    filled = magic_tools.execute_magic_cast(
        {"player": "哈维", "spell": "深渊之息", "use_catalog": True}, context
    )
    assert filled["ok"] is True, filled
    assert filled["catalog"]["filled"]["mp"] == 8
    assert filled["cast"]["mp_spent"] == 8


def test_grimoire_use_catalog_fills_mythos_stats(tmp_path):
    save_room_info(
        tmp_path,
        _info(house_rules={"coc7_magic_enabled": True, "coc7_magic_catalog_enabled": True}),
    )
    context = FakeContext(tmp_path)
    result = magic_tools.execute_magic_grimoire(
        {
            "player": "哈维",
            "title": "伊波恩之书",
            "mode": "study",
            "weeks": 32,
            "san_loss": 4,
            "language": "英文",
            "use_catalog": True,
        },
        context,
    )
    assert result["ok"] is True, result
    assert result["catalog"]["filled"]["rating"] == 33
    assert "spells" in result["catalog"]["filled"]
    assert result["grimoire"]["mythos_gain"] == 8


# ---------------------------------------------------------------------------
# 控制台
# ---------------------------------------------------------------------------
def test_console_find_show_require_catalog_switch(tmp_path):
    save_room_info(tmp_path, _info())
    ok, text = _run(["spell", "find", "深渊"], tmp_path)
    assert ok is False
    assert "未知的规则命令：rule spell" in text


def test_console_find_and_show_with_catalog_only(tmp_path):
    save_room_info(
        tmp_path, _info(house_rules={"coc7_magic_catalog_enabled": True})
    )
    ok, text = _run(["spell", "find", "深渊之息"], tmp_path)
    assert ok is True, text
    assert "深渊之息" in text

    ok, text = _run(["spell", "show", "深渊之息"], tmp_path)
    assert ok is True, text
    assert "1D6" in text
    assert "POW对抗" in text or "需要检定" in text

    # 只开内容表时不能施法，命令按未知命令处理。
    ok, text = _run(["spell", "cast", "哈维", "深渊之息"], tmp_path)
    assert ok is False
    assert "未知的规则命令：rule spell cast" in text


def test_console_cast_uses_catalog_flag(tmp_path):
    save_room_info(
        tmp_path,
        _info(house_rules={"coc7_magic_enabled": True, "coc7_magic_catalog_enabled": True}),
    )
    ok, text = _run(["spell", "learn", "哈维", "深渊之息", "weeks", "2"], tmp_path)
    assert ok is True, text

    ok, text = _run(
        ["spell", "cast", "哈维", "深渊之息", "catalog", "result", "regular"], tmp_path
    )
    assert ok is True, text
    assert "8点魔法值" in text
    assert load_room_info(tmp_path)["members"][0]["character_state"]["current_mp"] == 2
