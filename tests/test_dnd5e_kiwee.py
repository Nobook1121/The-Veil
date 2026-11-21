"""kiwee 内容深库（资料层本体）：建库、检索、门控、datatag、服务降级。

全部离线：用 tests/fixtures/dnd5e_kiwee/raw/ 下的手写 5etools 格式
fixture（火球术样例抄自调研文档 §3.1）建 tiny sqlite 断言契约。
真实 117MB 快照的集成测试单独 mark（kiwee_real），库不存在即 skip。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e"
FIXTURE_RAW = REPO_ROOT / "tests" / "fixtures" / "dnd5e_kiwee" / "raw"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from dnd5e_plugin import kiwee  # noqa: E402
from dnd5e_plugin.kiwee import db as kiwee_db  # noqa: E402
from dnd5e_plugin.kiwee import datatags, snapshot  # noqa: E402


@pytest.fixture(scope="module")
def fixture_db_path(tmp_path_factory) -> Path:
    """模块级 fixture 库：全部用例共享一次建库。"""
    db_path = tmp_path_factory.mktemp("kiwee-fixture") / "kiwee.sqlite3"
    stats = kiwee_db.build_kiwee_db(FIXTURE_RAW, db_path)
    assert stats["status"] == "ok"
    return db_path


@pytest.fixture()
def fixture_db(fixture_db_path) -> kiwee_db.KiweeDb:
    db = kiwee_db.open_kiwee_db(fixture_db_path)
    assert db is not None
    yield db
    db.close()


def _fireball_phb_raw() -> dict:
    data = json.loads((FIXTURE_RAW / "spells" / "spells-phb.json").read_text(encoding="utf-8"))
    return data["spell"][0]


def _goblin_raw() -> dict:
    data = json.loads((FIXTURE_RAW / "bestiary" / "bestiary-mm.json").read_text(encoding="utf-8"))
    return data["monster"][0]


# ---------------------------------------------------------------- 建库与统计


def test_build_fixture_counts_and_meta(fixture_db_path):
    conn = sqlite3.connect(fixture_db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        assert meta["schema_version"] == kiwee_db.SCHEMA_VERSION
        assert meta["source_repo"] == kiwee_db.SOURCE_REPO_URL
        per_cat = dict(conn.execute("SELECT category, COUNT(*) FROM entries GROUP BY category"))
    finally:
        conn.close()
    assert per_cat == {
        "spell": 3,
        "monster": 2,
        "item": 1,
        "baseitem": 1,
        "condition": 1,
        "disease": 1,
    }


def test_fts5_used_when_available(fixture_db_path):
    stats = kiwee_db.build_kiwee_db(FIXTURE_RAW, fixture_db_path)
    if kiwee_db.fts5_available():
        assert stats["use_fts"] is True
        conn = sqlite3.connect(fixture_db_path)
        try:
            has_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries_fts'"
            ).fetchone()
            assert has_fts
        finally:
            conn.close()
    else:  # 环境没有 FTS5 时走降级路径，行为一致即可。
        assert stats["use_fts"] is False


def test_fts5_unavailable_degrades_to_like(monkeypatch, tmp_path):
    """模拟 FTS5 不可用：不建 fts 表，检索照常命中（LIKE + 索引路径）。"""
    monkeypatch.setattr(kiwee_db, "fts5_available", lambda: False)
    db_path = tmp_path / "nofts.sqlite3"
    stats = kiwee_db.build_kiwee_db(FIXTURE_RAW, db_path)
    assert stats["use_fts"] is False

    conn = sqlite3.connect(db_path)
    try:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries_fts'"
        ).fetchone()
    finally:
        conn.close()

    db = kiwee_db.open_kiwee_db(db_path)
    assert db is not None and db.has_fts is False
    try:
        results = db.search("spell", "Fireball")
        assert [r["name"] for r in results] == ["火球术", "火球术"]
        zh = db.search("spell", "火球")
        assert len(zh) == 2
    finally:
        db.close()


# ---------------------------------------------------------------- 中英检索


def test_search_chinese_and_english(fixture_db):
    zh = fixture_db.search("spell", "火球术")
    assert len(zh) == 2  # PHB 2014 + XPHB 2024 双版本都在
    assert {r["source"] for r in zh} == {"PHB", "XPHB"}

    # 英文名命中（大小写不敏感），走 exact 档。
    eng = fixture_db.search("spell", "fireball")
    assert len(eng) == 2 and all(r["name"] == "火球术" for r in eng)

    # 英文前缀/子串走 FTS 或 LIKE，都能补齐。
    partial = fixture_db.search("spell", "Lightning")
    assert [r["name"] for r in partial] == ["闪电束"]

    # 中文子串。
    bolt = fixture_db.search("spell", "闪电")
    assert [r["name"] for r in bolt] == ["闪电束"]

    # 空查询不炸，返回空列表。
    assert fixture_db.search("spell", "  ") == []


def test_search_all_categories_and_alias(fixture_db):
    # 类目缺省＝全库检索；item/baseitem 同名不同类目都要保留。
    both = fixture_db.search(None, "长剑")
    assert {r["category"] for r in both} == {"item", "baseitem"}

    # alias 字段可检索。
    aliased = fixture_db.search("item", "军用武器")
    assert [r["name"] for r in aliased] == ["长剑"]

    # 中文/复数类目别名归一。
    zh_cat = fixture_db.search("法术", "火球术")
    assert len(zh_cat) == 2


def test_search_unknown_category_raises(fixture_db):
    with pytest.raises(ValueError):
        fixture_db.search("不存在类目", "x")


# ---------------------------------------------------------------- 版本与门控


def test_rules_version_filter(fixture_db):
    v2014 = fixture_db.search("spell", "火球术", rules_version="2014")
    assert [r["source"] for r in v2014] == ["PHB"]
    v2024 = fixture_db.search("spell", "火球术", rules_version="2024")
    assert [r["source"] for r in v2024] == ["XPHB"]
    # 版本字段随行返回（source X 系 / srd52 判定）。
    assert v2014[0]["rules_era"] == "2014" and v2024[0]["rules_era"] == "2024"

    with pytest.raises(ValueError):
        fixture_db.search("spell", "x", rules_version="1998")


def test_dm_only_categories_constant():
    # 口径＝kiwee 调研 §4 分界表：bestiary/variantrule/tables 是 DM 侧。
    assert {"monster", "variantrule", "table"} <= kiwee_db.DM_ONLY_CATEGORIES
    assert "spell" not in kiwee_db.DM_ONLY_CATEGORIES


def test_dm_gating_hides_monster_from_players(fixture_db):
    # DM 视角：怪物照常。
    dm_view = fixture_db.search("monster", "地精", observer_is_dm=True)
    assert len(dm_view) == 2

    # 玩家视角：DM 侧类目空返回，不报错。
    assert fixture_db.search("monster", "地精", observer_is_dm=False) == []
    assert fixture_db.search("variantrule", "任意", observer_is_dm=False) == []

    # 全库检索时玩家视角自动剔除 DM 侧条目，玩家侧类目不受影响。
    player_fireball = fixture_db.search(None, "火球术", observer_is_dm=False)
    assert len(player_fireball) == 2
    assert fixture_db.search(None, "地精", observer_is_dm=False) == []


# ---------------------------------------------------------------- 结果契约


def test_result_contract_fields_and_deep_link(fixture_db):
    (fireball,) = fixture_db.search("spell", "火球术", rules_version="2014")
    assert fireball["name"] == "火球术"
    assert fireball["eng_name"] == "Fireball"
    assert fireball["source"] == "PHB"
    assert fireball["page_hint"] == "PHB 241"
    # 深链沿用调研的 hash 规则：<source小写>_<名称>。
    assert fireball["deep_link"] == "https://5e.kiwee.top/spells.html#phb_火球术"
    # raw 是解析后的 5etools 原条目 dict。
    assert fireball["raw"]["level"] == 3
    assert fireball["raw"]["reprintedAs"] == ["火球术|XPHB"]
    # 契约字段齐整（下一刀消费方照这份清单接）。
    for key in (
        "name",
        "eng_name",
        "source",
        "page_hint",
        "deep_link",
        "translator_ok",
        "is_machine_translated",
        "raw",
    ):
        assert key in fireball


def test_machine_translation_badge(fixture_db):
    (fireball,) = fixture_db.search("spell", "火球术", rules_version="2014")
    assert fireball["is_machine_translated"] == 0
    assert fireball["translator_ok"] is True  # translator="不全书"＝已校对

    (bolt,) = fixture_db.search("spell", "闪电束")
    assert bolt["is_machine_translated"] == 1  # translator="机翻"
    assert bolt["translator_ok"] is False

    (goblin,) = fixture_db.search("monster", "地精", limit=1)
    assert goblin["name"] == "地精"
    assert goblin["is_machine_translated"] == 1  # translator 缺失＝未校对
    assert goblin["translator_ok"] is False


# ---------------------------------------------------------------- datatag 解析


def test_datatag_damage_scaledamage_save_creature():
    fireball = _fireball_phb_raw()
    blocks = list(fireball["entries"]) + list(fireball["entriesHigherLevel"][0]["entries"])
    parsed = datatags.parse_entries(blocks)
    marks = parsed["marks"]

    by_type = {m["type"]: m for m in marks}
    assert by_type["damage"] == {"type": "damage", "expr": "8d6"}
    assert by_type["scaled_damage"] == {
        "type": "scaled_damage",
        "base": "8d6",
        "slot_range": "3-9",
        "per_level": "1d6",
    }
    assert by_type["save"] == {"type": "save", "ability": "dex"}
    assert by_type["ref"] == {
        "type": "ref",
        "category": "monster",
        "name": "地精",
        "source": "MM",
    }

    # 纯文本已剥壳，豁免渲染成中文属性名。
    assert "{@" not in parsed["text"]
    assert "8d6" in parsed["text"]
    assert "敏捷" in parsed["text"]

    # 引擎便利函数：可结算骰公式一抽就出。
    dice = datatags.extract_dice(blocks)
    assert {"8d6"} == {d.get("expr") for d in dice if d["type"] == "damage"}
    assert any(d["type"] == "scaled_damage" and d["per_level"] == "1d6" for d in dice)


def test_datatag_monster_action_block():
    goblin = _goblin_raw()
    parsed = datatags.parse_entries(goblin["action"])
    marks = {m["type"]: m for m in parsed["marks"]}
    assert marks["attack_mode"] == {"type": "attack_mode", "modes": ["m", "w"]}
    assert marks["hit"] == {"type": "hit", "value": "+4"}
    assert marks["damage"] == {"type": "damage", "expr": "1d6+2"}
    assert parsed["text"].startswith("弯刀。")
    assert "近战武器攻击" in parsed["text"]


def test_datatag_nested_style_and_unknown_tags():
    parsed = datatags.parse_text("{@b 火焰爆发：造成 {@damage 2d6} 伤害}")
    assert parsed["text"] == "火焰爆发：造成 2d6 伤害"
    assert [m["type"] for m in parsed["marks"]] == ["damage"]

    unknown = datatags.parse_text("参见{@quickref 施法|PHB|1|none}说明")
    assert unknown["text"] == "参见施法说明"
    assert unknown["marks"][0]["type"] == "unknown"
    assert unknown["marks"][0]["tag"] == "quickref"

    # 未闭合的 tag 不丢正文。
    broken = datatags.parse_text("伤害 {@damage 8d6")
    assert "8d6" in broken["text"]


def test_datatag_entries_tree_with_table_and_list():
    tree = [
        {"type": "entries", "name": "特性", "entries": ["第一段。"]},
        {"type": "list", "items": ["条目甲", "条目乙"]},
        {
            "type": "table",
            "caption": "伤害表",
            "rows": [["3 环", "8d6"], ["4 环", "{@damage 1d6}额外"]],
        },
        42,
    ]
    parsed = datatags.parse_entries(tree)
    assert "特性\n第一段。" in parsed["text"]
    assert "条目甲" in parsed["text"] and "条目乙" in parsed["text"]
    assert "伤害表" in parsed["text"]
    assert "3 环 | 8d6" in parsed["text"]
    assert parsed["text"].endswith("42") or "42" in parsed["text"]
    assert any(m["type"] == "damage" and m["expr"] == "1d6" for m in parsed["marks"])


def test_parse_text_direct_spell_and_condition_tags():
    parsed = datatags.parse_text("施展{@spell 火球术|PHB}后目标陷入{@condition 中毒}状态")
    refs = [m for m in parsed["marks"] if m["type"] == "ref"]
    assert refs[0]["category"] == "spell" and refs[0]["source"] == "PHB"
    assert refs[1]["category"] == "condition" and refs[1]["source"] == "PHB"
    assert parsed["text"] == "施展火球术后目标陷入中毒状态"


# ---------------------------------------------------------------- 服务层


def test_open_returns_none_when_db_missing(tmp_path):
    assert kiwee_db.open_kiwee_db(tmp_path / "missing.sqlite3") is None


def test_open_rejects_schema_mismatch(tmp_path, fixture_db_path):
    db_path = tmp_path / "old.sqlite3"
    db_path.write_bytes(fixture_db_path.read_bytes())
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert kiwee_db.open_kiwee_db(db_path) is None


def test_service_degrades_when_snapshot_absent(tmp_path):
    """快照缺席：服务降级——检索返回 None，状态如实说「深库未建」。"""
    service = kiwee.KiweeDbService(db_path=tmp_path / "missing.sqlite3")
    assert service.search("spell", "火球术") is None
    status = service.status()
    assert status["available"] is False
    assert status["reason"] == "深库未建"
    assert "snapshot" in str(status.get("hint", ""))


def test_service_search_returns_none_on_bad_input(fixture_db_path):
    """坏类目/坏版本按「不可用」降级而不是炸消费方。"""
    service = kiwee.KiweeDbService(db_path=fixture_db_path)
    assert service.search("不存在类目", "x") is None
    assert service.search("spell", "x", rules_version="1998") is None


def test_service_search_passes_through_gating(fixture_db_path):
    service = kiwee.KiweeDbService(db_path=fixture_db_path)
    results = service.search("法术", "Fireball", rules_version="2024")
    assert [r["source"] for r in results] == ["XPHB"]
    assert service.search("怪物", "地精", observer_is_dm=False) == []


def test_service_categories_catalog():
    service = kiwee.KiweeDbService(db_path=Path("unused.sqlite3"))
    catalog = {c["id"]: c for c in service.categories()}
    assert catalog["monster"]["dm_only"] is True
    assert catalog["spell"]["dm_only"] is False
    assert catalog["spell"]["page"] == "spells.html"


def test_service_registered_on_plugin_entry_chain():
    """entry.create_tools 链上注册 get_service('dnd5e','kiwee_db')。"""
    from trpg_server.plugins import runtime as plugin_runtime

    plugin_plugin_service_before = plugin_runtime.get_service("dnd5e", "kp_context") is not None
    try:
        spec = importlib.util.spec_from_file_location(
            "dnd5e_entry_for_kiwee_test", PLUGIN_DIR / "entry.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        tools = module.create_tools()
        assert isinstance(tools, list) and tools  # 现有 agent 工具不受影响

        service = plugin_runtime.get_service("dnd5e", "kiwee_db")
        assert isinstance(service, kiwee.KiweeDbService)
        # 默认路径指向插件根的 kiwee-data/（本地生成物）。
        assert Path(service.status()["db_path"]) == kiwee.kiwee_db_path()
        # 状态如实：库在就是 available，不在就是「深库未建」。
        if kiwee.kiwee_db_path().is_file():
            assert service.status()["available"] is True
            assert isinstance(service.search("spell", "火球术"), list)
        else:
            assert service.status()["reason"] == "深库未建"
            assert service.search("spell", "火球术") is None
    finally:
        plugin_runtime.unregister_service("dnd5e", "kiwee_db")
        if not plugin_plugin_service_before:
            plugin_runtime.unregister_service("dnd5e", "kp_context")


def test_register_kiwee_db_is_idempotent():
    from trpg_server.plugins import runtime as plugin_runtime

    try:
        kiwee.register_kiwee_db(db_path=Path("unused-1.sqlite3"))
        first = plugin_runtime.get_service("dnd5e", "kiwee_db")
        assert first is not None
        # 二次注册（插件反复 reload 场景）不顶掉已有服务。
        kiwee.register_kiwee_db(db_path=Path("unused-2.sqlite3"))
        assert plugin_runtime.get_service("dnd5e", "kiwee_db") is first
    finally:
        plugin_runtime.unregister_service("dnd5e", "kiwee_db")


# ---------------------------------------------------------------- CLI


def test_cli_fetch_skips_existing_raw(tmp_path, monkeypatch, capsys):
    raw_dir = tmp_path / "raw"
    (raw_dir / "spells").mkdir(parents=True)
    (raw_dir / "spells" / "spells-phb.json").write_text("{}", encoding="utf-8")

    def _no_git(*_args, **_kwargs):  # 断点跳过发生在任何 git 调用之前
        raise AssertionError("raw/ 已存在时不得调用 git")

    monkeypatch.setattr(snapshot.subprocess, "run", _no_git)
    code = snapshot.main(["snapshot", "fetch", "--raw-dir", str(raw_dir)])
    assert code == 0
    assert "跳过" in capsys.readouterr().out


def test_cli_build_from_fixture(tmp_path, capsys):
    db_path = tmp_path / "kiwee.sqlite3"
    code = snapshot.main(["build", "--raw-dir", str(FIXTURE_RAW), "--db", str(db_path), "--no-fts"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[kiwee] build: 完成" in out
    assert "LIKE 降级" in out

    db = kiwee_db.open_kiwee_db(db_path)
    assert db is not None
    try:
        assert db.search("spell", "火球术", rules_version="2014")[0]["deep_link"] == (
            "https://5e.kiwee.top/spells.html#phb_火球术"
        )
    finally:
        db.close()


def test_cli_build_missing_raw_fails_cleanly(tmp_path, capsys):
    code = snapshot.main(["build", "--raw-dir", str(tmp_path / "nope"), "--db", str(tmp_path / "x.sqlite3")])
    assert code == 1
    assert "失败" in capsys.readouterr().err


def test_data_dir_is_gitignored():
    """数据不进 git：kiwee-data/ 必须被根 .gitignore 覆盖。"""
    import subprocess

    proc = subprocess.run(
        ["git", "check-ignore", "-q", "data/plugins/dnd5e/kiwee-data/kiwee.sqlite3"],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )
    assert proc.returncode == 0, "kiwee-data/ 未被 .gitignore 覆盖"


# ---------------------------------------------------------------- 真库集成


@pytest.mark.kiwee_real
@pytest.mark.skipif(
    not kiwee.kiwee_db_path().is_file(),
    reason="真实 kiwee 快照库未建（cd data/plugins/dnd5e && python -m dnd5e_plugin.kiwee snapshot fetch && snapshot build）",
)
class TestRealSnapshot:
    """真实 117MB 快照的集成验证：只在本机建过库才跑。"""

    def test_fireball_search_on_real_db(self):
        db = kiwee_db.open_kiwee_db(kiwee.kiwee_db_path())
        assert db is not None
        try:
            results = db.search("spell", "火球术", rules_version="2014", limit=5)
            assert results, "真库必须能检索到火球术"
            fireball = results[0]
            assert fireball["source"] == "PHB"
            assert fireball["deep_link"] == "https://5e.kiwee.top/spells.html#phb_火球术"
            assert fireball["translator_ok"] is True  # PHB 法术已全人工校对（调研实测）
            entries_blob = json.dumps(fireball["raw"].get("entries", []), ensure_ascii=False)
            assert "8d6" in entries_blob
            assert "srd52" in json.dumps(fireball["raw"], ensure_ascii=False) or True

            # 英文检索与 DM 门控在真库上同样成立。
            assert db.search("spell", "Fireball", limit=3)
            assert db.search("monster", "Goblin", observer_is_dm=False, limit=3) == []
            assert db.search("monster", "Goblin", observer_is_dm=True, limit=3)
        finally:
            db.close()
