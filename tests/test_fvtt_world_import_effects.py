"""!actors.items.effects! → 品种效果（transfer buff）→ 卡面 effect_changes。

这一层是**数据通路**：世界包里的条目级 Active Effect 必须真的进产物，
否则引擎再准也只是空转（此前它们只被计数、被丢弃）。

覆盖：

- heroes 包 41 条条目级效果全部收进 ``ActorIndex.item_effects``（三层复合键
  ``<actorId>.<itemId>.<effectId>``，``_id`` 写回，与 items 同款做法）；
- 41 条全部挂到对应物品/角色卡，汇总计数如实（57 条 change：31 条接入数值 /
  26 条已记录但没有消费方 / 0 条在镜像模型之外）；
- flags/ATL/activities 这类模块/前端字段**不再丢弃**：引擎镜像模型照样
  记下，只是分级为 stored（「已记录·暂无消费方」）；真正的外来命名空间才
  算 unsupported，并且**导入器的分级口径 = 引擎镜像 schema 的口径**
  （两边不许漂移）；
- 「战斗风格：防御」这类真能生效的 transfer buff 真的改派生值（AC 12 → 13）；
- sixian 包 14 条同样全挂载（16 条 change：3 条接入数值 / 13 条已记录）；
- kiwee 深库补强只在**同名**物品上生效（防护戒指 → AC+1/豁免+1），
  模糊命中的魔法物品不会把加值算到凡品身上。

样例包缺失或深库未建时 pytest.skip。
"""

from __future__ import annotations

import json
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

#: 两包实测的条目级效果账目（发现数 / change 数 / 三档分级 / transfer 数）。
#: 三档相加 == changes：``full`` 真进数值、``stored`` 已记录但没消费方、
#: ``unsupported`` 连镜像模型都没有（heroes / sixian 实测都是 0）。
HEROES_EFFECTS = {
    "documents": 41,
    "changes": 57,
    "full": 36,
    "stored": 21,
    "unsupported": 0,
    "transfer": 8,
}
SIXIAN_EFFECTS = {
    "documents": 14,
    "changes": 16,
    "full": 4,
    "stored": 12,
    "unsupported": 0,
    "transfer": 0,
}


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


def test_heroes_three_tier_counts_are_honest(heroes) -> None:
    """41 条效果 / 57 条 change 的三档分级必须逐条对得上账。"""
    converted = heroes.converted
    totals = converted["totals"]["effects"]
    assert totals["changes"] == HEROES_EFFECTS["changes"]
    assert totals["full"] == HEROES_EFFECTS["full"]
    assert totals["stored"] == HEROES_EFFECTS["stored"]
    assert totals["unsupported"] == HEROES_EFFECTS["unsupported"]
    assert totals["full"] + totals["stored"] + totals["unsupported"] == totals["changes"]
    # 导入器的分级口径 == 引擎镜像 schema 的口径（两边不许漂移）。
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
        if classify_path(change["key"])[2] != ca.effect_key_tier(change["key"])
    ]
    assert drifted == []


def test_heroes_stored_effects_are_kept_not_dropped(heroes) -> None:
    """flags / ATL / activities / 分速 … 这些没有消费方的路径照样在卡面上。"""
    from dnd5e_plugin.effect_changes import TIER_STORED, effective_sheet

    sheet = _character(heroes.converted, "半身人诗人")["card"]["sheet"]
    view = effective_sheet(sheet)
    stored_keys = {record["key"] for record in view.stored}
    assert "flags.dnd5e.halflingLucky" in stored_keys
    assert "flags.midi-qol.ActivityOverTime" in stored_keys
    assert "ATL.light.bright" in stored_keys
    assert all(record["tier"] == TIER_STORED for record in view.stored)
    # 值真的写进了镜像（「已记录」有内容），只是没有数值消费方。
    assert view.value("flags.dnd5e.halflingLucky") is not None


def test_heroes_effect_warnings_only_for_alien_paths(heroes) -> None:
    """没有「镜像模型之外」的路径就不该出效果告警（stored 进 notes）。"""
    converted = heroes.converted
    assert not [text for text in converted["warnings"] if "效果" in text]
    assert converted["degradations"].get("effects.unsupported", 0) == 0


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
    totals = converted["totals"]["effects"]
    assert totals["full"] == SIXIAN_EFFECTS["full"]
    assert totals["stored"] == SIXIAN_EFFECTS["stored"]
    assert totals["unsupported"] == SIXIAN_EFFECTS["unsupported"]
    assert len(sixian.effects_of()) == SIXIAN_EFFECTS["documents"]


# -- 审计报告：双计数 ------------------------------------------------------


def test_audit_reports_imported_and_three_tier_counts(heroes) -> None:
    report = wp.audit(heroes.pkg)
    assert report["actor_item_effects"] == HEROES_EFFECTS["documents"]
    assert report["actor_item_effects_imported"] == HEROES_EFFECTS["documents"]
    assert report["actor_item_effects_full"] == HEROES_EFFECTS["full"]
    assert report["actor_item_effects_stored"] == HEROES_EFFECTS["stored"]
    assert report["actor_item_effects_unsupported"] == HEROES_EFFECTS["unsupported"]
    notes = [text for text in report["notes"] if "条目级效果" in text]
    assert notes and f"已导入 {HEROES_EFFECTS['documents']} 条" in notes[0]
    assert f"{HEROES_EFFECTS['full']} 条接入数值" in notes[0]
    assert f"{HEROES_EFFECTS['stored']} 条已记录但当前没有消费方" in notes[0]
    # 这类字段本来就不进数值：不该长得像错误。
    assert not [text for text in report["warnings"] if "条目级效果" in text]


def test_audit_keeps_legacy_keys_for_discovered_and_unsupported(sixian) -> None:
    report = wp.audit(sixian.pkg)
    assert report["actor_item_effects"] == SIXIAN_EFFECTS["documents"]
    assert report["actor_item_effects_imported"] == SIXIAN_EFFECTS["documents"]
    assert report["actor_item_effects_full"] == SIXIAN_EFFECTS["full"]
    assert report["actor_item_effects_stored"] == SIXIAN_EFFECTS["stored"]
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


# ---------------------------------------------------------------------------
# FVTT 物品文档（item document）保真
# ---------------------------------------------------------------------------


def test_equipment_rows_keep_full_item_documents(heroes):
    """装备行带规范化文档：type/system/activities/flags 都在，不只扁平行。"""
    sheet = _character(heroes.converted, "图伊勒")["card"]["sheet"]
    weapons = [row for row in sheet["equipment"] if (row.get("item") or {}).get("type") == "weapon"]
    assert weapons, "图伊勒应有武器行"
    doc = weapons[0]["item"]
    assert doc["id"] and doc["name"]
    system = doc["system"]
    assert isinstance(system, dict) and "damage" in system
    assert doc["activities"], "武器文档应保留 activities（攻击/伤害参数）"
    assert isinstance(doc.get("flags"), dict)
    # 非装备条目（职业/种族/子职/法术/专长）进 item_documents。
    documents = sheet.get("item_documents") or {}
    assert isinstance(documents, dict) and documents
    assert all(isinstance(value, dict) and value.get("type") for value in documents.values())


def test_item_document_activities_drive_weapon_row():
    """文档优先于扁平字段：activities 的攻击加值、伤害骰、属性、射程都生效。"""
    from dnd5e_plugin.effect_changes import weapon_row

    row = {
        "id": "test-sword",
        "name": "测试剑",
        "properties": [],
        "damage": {"dice": "1d4", "type": "slashing"},
        "item": {
            "id": "test-sword",
            "type": "weapon",
            "system": {
                "properties": ["finesse"],
                "damage": {"base": {"number": 2, "denomination": 6, "types": ["fire"]}},
                "range": {"value": 20, "long": 60},
                "activities": {
                    "dnd5eactivity000": {
                        "attack": {"bonus": 3, "ability": "finesse"},
                        "damage": {"parts": [{"formula": "3d8"}]},
                    }
                },
            },
        },
    }
    merged = weapon_row(row)
    assert merged["attack_bonus"] == 3
    assert merged["attack_ability"] == "finesse"
    assert "finesse" in merged["properties"]
    assert merged["damage"]["dice"] == "3d8"  # activities 覆盖文档伤害
    assert merged["range"]["normal_ft"] == 20
    assert merged["range"]["long_ft"] == 60


def test_legacy_card_without_item_document_is_unchanged():
    """没有 item / 没有效果视图时，weapon_row 返回原行副本（逐字节一致）。"""
    from dnd5e_plugin.effect_changes import weapon_row

    row = {
        "id": "old",
        "name": "旧剑",
        "properties": ["versatile"],
        "damage": {"dice": "1d8", "type": "slashing"},
    }
    merged = weapon_row(dict(row))
    assert merged == row
    assert merged is not row


def test_item_document_truncation_is_reported():
    """超限文档按层截断并留下可读标记，不静默丢数据。"""
    from fvtt_world_import import convert_actors as ca

    item = {
        "_id": "oversized",
        "name": "超大物品",
        "type": "loot",
        "system": {
            "description": {"value": "说" * 80000},
            "weight": 1,
            "price": {"value": 1},
        },
        "flags": {"huge": "x" * 40000},
    }
    doc = ca._item_document(item)
    assert doc["name"] == "超大物品"
    assert doc.get("_truncated"), "超限文档应标记截断层"
    size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    assert size <= ca.ITEM_DOC_MAX_BYTES + 2048
