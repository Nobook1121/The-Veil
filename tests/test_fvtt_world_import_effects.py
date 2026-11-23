"""!actors.items.effects! → 品种效果（transfer buff）→ 卡面 effect_changes。

这一层是**数据通路**：世界包里的条目级 Active Effect 必须真的进产物，
否则引擎再准也只是空转（此前它们只被计数、被丢弃）。

覆盖：

- heroes 包 41 条条目级效果全部收进 ``ActorIndex.item_effects``（三层复合键
  ``<actorId>.<itemId>.<effectId>``，``_id`` 写回，与 items 同款做法）；
- 41 条全部挂到对应物品/角色卡，汇总计数如实（57 条 change / 24 条 unsupported）；
- unsupported 全是真没有消费方的字段族（flags/ATL/activities/…），并且
  **导入器的「支持」口径 = 引擎真能结算的口径**（两边不许漂移）；
- 「战斗风格：防御」这类真能生效的 transfer buff 真的改派生值（AC 12 → 13）；
- sixian 包 14 条同样全挂载（11 条 unsupported）；
- kiwee 深库补强只在**同名**物品上生效（防护戒指 → AC+1/豁免+1），
  模糊命中的魔法物品不会把加值算到凡品身上。

样例包缺失或深库未建时 pytest.skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FVTT_PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
DND5E_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e"
for _path in (FVTT_PLUGIN_DIR, DND5E_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fvtt_world_import import convert_actors as ca  # noqa: E402
from fvtt_world_import import world_package as wp  # noqa: E402

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"

#: 两包实测的条目级效果账目（发现数 / 挂载数 / change 数 / unsupported 数）。
HEROES_EFFECTS = {"documents": 41, "changes": 57, "unsupported": 24, "transfer": 8}
SIXIAN_EFFECTS = {"documents": 14, "changes": 16, "unsupported": 11, "transfer": 0}


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


class _Package:
    """解包 → 读 actors → 转换；三样东西一起给测试用。"""

    def __init__(self, zip_path: Path, workdir: Path) -> None:
        self.pkg = wp.extract_package(_require_zip(zip_path).read_bytes(), workdir)
        self.index = wp.read_actors(self.pkg)
        # 补强单独测（避免样例包的凡品被深库影响），这里关掉。
        self.converted = ca.convert_world_actors(self.index, item_lookup=None, kiwee_enrich=False)

    def close(self) -> None:
        self.pkg._archive.close()

    def sheets(self) -> list[dict]:
        """所有产物里带效果的「卡面」字典（角色 sheet / NPC statblock）。"""
        rows: list[dict] = []
        for row in self.converted["characters"]:
            rows.append(row["card"]["sheet"])
        for row in self.converted["npcs"]:
            rows.append(row["module"]["statblock"])
        return rows

    def effects_of(self) -> list[dict]:
        """产物里所有挂上的效果条目（角色级 + 装备行）。"""
        found: list[dict] = []
        for sheet in self.sheets():
            found.extend(sheet.get("effect_changes") or [])
            for row in sheet.get("equipment") or []:
                found.extend(row.get("effects") or [])
        return found


@pytest.fixture(scope="module")
def heroes(tmp_path_factory: pytest.TempPathFactory):
    package = _Package(HEROES_ZIP, tmp_path_factory.mktemp("effects-heroes"))
    yield package
    package.close()


@pytest.fixture(scope="module")
def sixian(tmp_path_factory: pytest.TempPathFactory):
    package = _Package(SIXIAN_ZIP, tmp_path_factory.mktemp("effects-sixian"))
    yield package
    package.close()


# -- 数据通路：条目级效果真的收进索引 --------------------------------------


def test_item_effects_are_collected_with_three_part_ids(heroes) -> None:
    index = heroes.index
    assert index.item_effects_total == HEROES_EFFECTS["documents"]
    assert sum(len(rows) for rows in index.item_effects.values()) == HEROES_EFFECTS["documents"]
    assert index.item_effects_dropped == 0
    assert index.orphan_effects == 0
    for owner, rows in index.item_effects.items():
        assert owner in index.documents
        for doc in rows:
            parts = str(doc["_id"]).split(".")
            assert len(parts) == 3
            assert parts[0] == owner
            # 复合键的 item 段必须真能在 items 里找到（否则就是挂空）。
            assert ".".join(parts[:2]) in {item["_id"] for item in index.items[owner]}


def test_item_effects_are_not_mixed_into_actor_level_effects(heroes) -> None:
    for owner, rows in heroes.index.item_effects.items():
        actor_ids = {str(doc["_id"]) for doc in heroes.index.effects.get(owner, [])}
        assert not (actor_ids & {str(doc["_id"]) for doc in rows})


# -- 41 条全通 --------------------------------------------------------------


def test_heroes_all_item_effects_are_mounted(heroes) -> None:
    index = heroes.index
    converted = heroes.converted
    assert converted["totals"]["effects"]["documents"] == HEROES_EFFECTS["documents"]
    assert converted["totals"]["effects"]["changes"] == HEROES_EFFECTS["changes"]
    assert converted["totals"]["effects"]["transfer"] == HEROES_EFFECTS["transfer"]
    # 挂载数 = 索引里的条数：一条不丢（丢弃才是 bug，unsupported 由引擎计数）。
    mounted = heroes.effects_of()
    assert len(mounted) == HEROES_EFFECTS["documents"]
    # 每条都能追回自己的父物品（导入器写回 origin.item_id）。
    by_actor = {
        str(actor_id): {str(doc["_id"]) for doc in rows}
        for actor_id, rows in index.item_effects.items()
    }
    for entry in mounted:
        origin = entry.get("origin") or {}
        item_id = str(origin.get("item_id") or "")
        assert item_id and "." in item_id
        actor_id, _sep, _item = item_id.partition(".")
        assert any(effect_id.startswith(f"{item_id}.") for effect_id in by_actor[actor_id])


def test_heroes_unsupported_count_is_honest(heroes) -> None:
    converted = heroes.converted
    assert converted["totals"]["effects"]["unsupported"] == HEROES_EFFECTS["unsupported"]
    # 少部分 change 没有消费方是**如实行为**（模块/前端字段），但一条支持路径
    # 都不许被误判成 unsupported。
    from dnd5e_plugin.effect_changes import classify_path

    changes = [
        change
        for entry in heroes.effects_of()
        for change in entry.get("changes") or []
    ]
    assert len(changes) == HEROES_EFFECTS["changes"]
    drifted = [
        change["key"]
        for change in changes
        if (classify_path(change["key"])[0] != "unsupported")
        != ca.effect_key_supported(change["key"])
    ]
    assert drifted == []


def test_heroes_effect_degrades_are_reported_not_silent(heroes) -> None:
    # unsupported 出告警（前 5 个 key + 出处），但不是错误。
    converted = heroes.converted
    texts = [text for text in converted["warnings"] if "效果" in text]
    assert texts
    assert converted["degradations"]["effects.unsupported"] >= 1


# -- 真生效的那几条 --------------------------------------------------------


def _derived(sheet: dict) -> dict:
    """卡面 → 派生镜像（AC/先攻/被动察觉），与运行时同一套口径。"""
    import copy

    from dnd5e_plugin.schema import Dnd5eCharacterSchema

    return Dnd5eCharacterSchema().derive(copy.deepcopy(sheet))


def _character(converted: dict, name_fragment: str) -> dict:
    for row in converted["characters"]:
        if name_fragment in row["name"]:
            return row
    raise AssertionError(f"找不到角色：{name_fragment}")


def test_transfer_buff_on_feat_changes_derived_armor_class(heroes) -> None:
    import copy

    from dnd5e_plugin.effect_changes import effective_sheet
    from dnd5e_plugin.schema import Dnd5eCharacterSchema

    row = _character(heroes.converted, "图伊勒")
    sheet = row["card"]["sheet"]
    names = [entry["name"] for entry in sheet.get("effect_changes") or []]
    assert "战斗风格：防御" in names

    with_effect = Dnd5eCharacterSchema().derive(copy.deepcopy(sheet))["armorClass"]
    # 把效果摘掉再算：差值就是这条 transfer buff 的贡献。
    stripped = copy.deepcopy(sheet)
    stripped.pop("effect_changes", None)
    without_effect = Dnd5eCharacterSchema().derive(stripped)["armorClass"]
    assert (without_effect, with_effect) == (12, 13)
    assert effective_sheet(sheet).ac_bonus == 1


def test_disabled_spell_effects_stay_inert(heroes) -> None:
    from dnd5e_plugin.effect_changes import effective_sheet

    # 卓尔法师挂着 Mage Armor / Shield 两条效果，但源包里 disabled=true：
    # 导入照实转写，激活判定交给引擎（不生效，AC 保持 12）。
    sheet = _character(heroes.converted, "卓尔法师")["card"]["sheet"]
    names = [entry["name"] for entry in sheet.get("effect_changes") or []]
    assert "Shield" in names
    assert all(entry.get("disabled") for entry in sheet["effect_changes"] if entry["name"] == "Shield")
    assert effective_sheet(sheet).empty
    assert _derived(sheet)["armorClass"] == 12


def test_alert_feat_shifts_initiative(heroes) -> None:
    from dnd5e_plugin.effect_changes import effective_sheet

    sheet = _character(heroes.converted, "提夫林盗贼")["card"]["sheet"]
    view = effective_sheet(sheet)
    assert view.initiative_bonus == 5  # 警觉 Alert
    # 敏捷 +3 + 5 = 8
    assert _derived(sheet)["initiative"] == 8


# -- sixian 包 --------------------------------------------------------------


def test_sixian_item_effects_are_mounted_too(sixian) -> None:
    index = sixian.index
    assert index.item_effects_total == SIXIAN_EFFECTS["documents"]
    assert sum(len(rows) for rows in index.item_effects.values()) == SIXIAN_EFFECTS["documents"]
    converted = sixian.converted
    assert converted["totals"]["effects"]["documents"] == SIXIAN_EFFECTS["documents"]
    assert converted["totals"]["effects"]["changes"] == SIXIAN_EFFECTS["changes"]
    assert converted["totals"]["effects"]["unsupported"] == SIXIAN_EFFECTS["unsupported"]
    assert len(sixian.effects_of()) == SIXIAN_EFFECTS["documents"]


# -- 审计报告：双计数 ------------------------------------------------------


def test_audit_reports_imported_and_unsupported_counts(heroes) -> None:
    report = wp.audit(heroes.pkg)
    assert report["actor_item_effects"] == HEROES_EFFECTS["documents"]
    assert report["actor_item_effects_imported"] == HEROES_EFFECTS["documents"]
    assert report["actor_item_effects_unsupported"] == HEROES_EFFECTS["unsupported"]
    notes = [text for text in report["notes"] if "条目级效果" in text]
    assert notes and f"已导入 {HEROES_EFFECTS['documents']} 条" in notes[0]
    assert f"{HEROES_EFFECTS['unsupported']} 条改动路径没有消费方" in notes[0]
    # 这类字段本来就不进数值：不该长得像错误。
    assert not [text for text in report["warnings"] if "条目级效果" in text]


def test_audit_keeps_legacy_key_for_discovered_count(sixian) -> None:
    report = wp.audit(sixian.pkg)
    assert report["actor_item_effects"] == SIXIAN_EFFECTS["documents"]
    assert report["actor_item_effects_imported"] == SIXIAN_EFFECTS["documents"]
    assert report["actor_item_effects_unsupported"] == SIXIAN_EFFECTS["unsupported"]


# -- kiwee 深库补强 --------------------------------------------------------


def _kiwee_lookup():
    lookup = ca.default_kiwee_item_lookup()
    if lookup is None:
        pytest.skip("kiwee 深库未建（data/plugins/dnd5e/kiwee-data/kiwee.sqlite3 缺失）")
    return lookup


def test_kiwee_enriches_magic_ring_but_never_a_plain_dagger() -> None:
    lookup = _kiwee_lookup()
    ring = lookup("防护戒指")
    assert isinstance(ring, dict) and ring.get("bonusAc") == "+1"
    # 模糊命中会把「真冰碎片（匕首）」的 +3 AC 算到凡品匕首上——必须拒绝。
    assert lookup("匕首") is None
    assert lookup("皮甲") is None


def test_kiwee_bonus_ac_becomes_a_typed_change() -> None:
    lookup = _kiwee_lookup()
    found = lookup("防护戒指")
    changes = ca._kiwee_changes(found, set())
    keys = {(change["key"], change["mode"], change["value"]) for change in changes}
    assert ("system.attributes.ac.bonus", ca.EFFECT_MODE_NAMES and 2, 1) in keys
    assert ("system.bonuses.abilities.save", 2, 1) in keys


def _ring_index() -> "wp.ActorIndex":
    """一个最小角色 + 一件「防护戒指」的合成索引（不依赖样例包）。"""
    index = wp.ActorIndex()
    actor_id, item_id = "ACTOR0000000001", "ITEM000000000001"
    index.documents[actor_id] = {
        "_id": actor_id,
        "name": "测试角色",
        "type": "character",
        "items": [item_id],
        "system": {
            "abilities": {
                key: {"value": 10}
                for key in ("str", "dex", "con", "int", "wis", "cha")
            },
            "attributes": {"ac": {"flat": 10}, "hp": {"value": 10, "max": 10}},
            "details": {"level": 1},
        },
    }
    index.items[actor_id] = [
        {
            "_id": f"{actor_id}.{item_id}",
            "name": "防护戒指",
            "type": "equipment",
            "system": {
                "equipped": True,
                "quantity": 1,
                "weight": {"value": 0},
                "type": {"value": "ring"},
                "armor": {"value": None},
            },
        }
    ]
    return index


def test_kiwee_enrichment_lands_on_the_equipment_row() -> None:
    lookup = _kiwee_lookup()
    converted = ca.convert_world_actors(_ring_index(), item_lookup=lookup)
    assert converted["totals"]["effects"]["kiwee_enriched"] == 1
    sheet = converted["characters"][0]["card"]["sheet"]
    ring = next(row for row in sheet["equipment"] if row["name"] == "防护戒指")
    entries = ring.get("effects") or []
    assert len(entries) == 1
    assert entries[0]["origin"]["source"].startswith("kiwee:")
    keys = {change["key"] for change in entries[0]["changes"]}
    assert {"system.attributes.ac.bonus", "system.bonuses.abilities.save"} <= keys
    # 补强出来的 AC 真进派生值：10 + 1。
    assert _derived(sheet)["armorClass"] == 11


def test_kiwee_enrichment_skips_plain_gear() -> None:
    lookup = _kiwee_lookup()
    index = _ring_index()
    item = index.items["ACTOR0000000001"][0]
    item["name"] = "匕首"
    converted = ca.convert_world_actors(index, item_lookup=lookup)
    assert converted["totals"]["effects"]["kiwee_enriched"] == 0
    sheet = converted["characters"][0]["card"]["sheet"]
    assert not (sheet.get("equipment") or [{}])[0].get("effects")
    assert _derived(sheet)["armorClass"] == 10
