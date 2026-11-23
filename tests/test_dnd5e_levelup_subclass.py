"""升级向导的子职候选：SRD 优先 + kiwee 扩展库去重、深库缺席降级、落库校验。

覆盖面（**全部离线**：手写 5etools raw → 现场建 tiny sqlite）：

- 候选顺序：SRD 永远排第一，其后才是 kiwee 扩展子职；
- 去重：与 SRD 同名（中文名/英文名）的扩展条目被吃掉，扩展库内部同名只留一条
  （2014 版优先）；只收「属于本职业」的扩展子职（别的职业的不许混进来）；
- 降级：深库没建（服务在册但库文件缺席）或服务干脆不在册 → 静默只剩 SRD；
- 上限：候选最多 ``MAX_SUBCLASS_CANDIDATES`` 条；
- 落库：``apply_plan`` 接受 kiwee 子职名（中文/英文都认），两者都查不到的名字
  仍然拒绝且不占用计划、不写盘。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
if str(PLUGIN_ROOT / "dnd5e") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "dnd5e"))

from dnd5e_plugin import kiwee, progression  # noqa: E402  (需先准备插件运行时)
from dnd5e_plugin.kiwee import db as kiwee_db  # noqa: E402
from trpg_server.plugins import runtime  # noqa: E402
from trpg_server.plugins.registry import PluginRegistry  # noqa: E402
from trpg_server.ruleset import reset_builtin_registry  # noqa: E402

WIZARD = "法师"
CLERIC = "牧师"


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def plugins():
    """真实数据包 + 插件运行时（与 test_dnd5e_levelup.py 同款）。"""
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _wizard_sheet(level: int = 1) -> dict:
    return {
        "level": level,
        "class": "wizard",
        "xp": 0,
        "abilities": {"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 10, "cha": 10},
        "hp": {"current": 7, "max": 7},
        "hit_dice": {"die": 6, "max": level, "used": 0},
        "features": [],
        "spellcasting": {"known": [], "prepared": [], "slots": {"1": {"max": 2}}},
    }


def _subclass_row(
    name: str,
    eng: str,
    *,
    source: str = "PHB",
    class_name: str = WIZARD,
    class_source: str = "PHB",
    edition: str = "classic",
    page: int = 100,
    features: list[tuple[int, str]] | None = None,
) -> dict:
    """一条 5etools 风格的子职 raw（字段口径＝kiwee cn2.0 快照）。"""
    grants = features or [(2, f"{name}特性"), (6, f"{name}强化")]
    return {
        "name": name,
        "ENG_name": eng,
        "source": source,
        "page": page,
        "className": class_name,
        "classSource": class_source,
        "shortName": name,
        "edition": edition,
        "subclassFeatures": [
            f"{feature}|{class_name}|{class_source}|{name}|{source}|{level}|{source}"
            for level, feature in grants
        ],
    }


SUBCLASS_ROWS = [
    # 与 SRD 的 "School of Evocation（塑能学派）" 同名 → 必须被去重掉
    _subclass_row("塑能学派", "School of Evocation", source="PHB", page=117),
    # 扩展库独有：应当出现在候选里
    _subclass_row("战争魔法", "War Magic", source="XGE", page=59),
    _subclass_row("时间魔法", "Chronurgy Magic", source="EGW", page=184),
    # 同名两条（2014 TCE / 2024 FRHoF）：只留 2014 那条
    _subclass_row("剑咏者", "Bladesinging", source="TCE", page=76),
    _subclass_row("剑咏者", "Bladesinger", source="FRHoF", page=26, edition="one"),
    # 别的职业的子职：不许混进法师候选
    _subclass_row("和平领域", "Peace Domain", source="TCE", class_name=CLERIC, page=32),
]


def _write_raw(raw_dir: Path, rows: list[dict]) -> Path:
    class_dir = raw_dir / "class"
    class_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"subclass": rows}, ensure_ascii=False)
    (class_dir / "class-test.json").write_text(payload, encoding="utf-8")
    return raw_dir


@pytest.fixture()
def kiwee_service(tmp_path):
    """tiny 深库 + 挂进服务表（teardown 摘掉，避免污染其它用例）。"""
    db_path = tmp_path / "kiwee.sqlite3"
    stats = kiwee_db.build_kiwee_db(_write_raw(tmp_path / "raw", SUBCLASS_ROWS), db_path)
    assert stats["status"] == "ok"
    assert stats["categories"]["subclass"] == len(SUBCLASS_ROWS)
    service = kiwee.KiweeDbService(db_path=db_path)
    runtime.register_service("dnd5e", kiwee.SERVICE_NAME, service)
    try:
        yield service
    finally:
        runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)


def _subclass_slot(plan: dict) -> dict:
    return next(slot for slot in plan["slots"] if slot["kind"] == "subclass")


def _candidates(plan: dict) -> list[dict]:
    return _subclass_slot(plan)["candidates"]


def _plan() -> dict:
    return progression.create_plan(
        {"sheet": _wizard_sheet(1)}, 2, owner_key="character:w1", class_advancement=True
    )


def _choices(subclass: str) -> list[dict]:
    return [
        {"slot_id": "level-2:subclass", "values": [subclass]},
        {"slot_id": "level-2:hp", "values": ["average"]},
        {"slot_id": "level-2:spell_known", "values": ["shield", "detect-magic"]},
    ]


# ---------------------------------------------------------------- 候选


def test_srd_candidate_first_then_kiwee(plugins, kiwee_service):
    """SRD 候选排第一，扩展库子职按「2014 优先 → 来源 → 名称」排在其后。"""
    plan = _plan()
    candidates = _candidates(plan)

    assert candidates[0]["value"] == "evocation"
    assert candidates[0]["source"] == "srd"
    assert candidates[0]["name_zh"] == "塑能学派"

    kiwee_values = [item["value"] for item in candidates if item["source"] == "kiwee"]
    assert kiwee_values == ["时间魔法", "剑咏者", "战争魔法"]
    assert plan["subclass"]["counts"] == {"srd": 1, "kiwee": 3}
    assert _subclass_slot(plan)["detail"]["sources"] == {"srd": 1, "kiwee": 3}
    # plan.subclass.options 与槽位候选同源（同一份列表，不是两套口径）
    assert [item["value"] for item in plan["subclass"]["options"]] == [
        item["value"] for item in candidates
    ]


def test_kiwee_candidate_shape_and_summary(plugins, kiwee_service):
    """扩展候选带来源/中文名/简介（无正文时＝等级＋特性名清单）。"""
    item = next(c for c in _candidates(_plan()) if c["value"] == "战争魔法")
    assert item["label"] == "战争魔法（War Magic）"
    assert item["name_zh"] == "战争魔法"
    assert item["source"] == "kiwee"
    assert item["summary"] == "2 级 战争魔法特性 · 6 级 战争魔法强化"
    assert item["detail"]["source_code"] == "XGE"
    assert item["detail"]["page_hint"] == "XGE 59"
    assert item["detail"]["source"] == "kiwee · XGE 59"
    assert item["detail"]["deep_link"].startswith(kiwee.KIWEE_SITE)


def test_kiwee_candidates_deduped_against_srd_and_each_other(plugins, kiwee_service):
    """与 SRD 同名的扩展条目被吃掉；扩展库内部同名只留 2014 那一版。"""
    candidates = _candidates(_plan())
    values = [item["value"] for item in candidates]
    # 塑能学派：SRD 已占位（value 是 srd id），扩展条目不得再出现
    assert "塑能学派" not in values
    assert sum(1 for value in values if value == "剑咏者") == 1
    assert len(values) == len(set(values))
    bladedancer = next(item for item in candidates if item["value"] == "剑咏者")
    assert bladedancer["detail"]["source_code"] == "TCE"  # 2014 版优先
    # 别的职业的子职不会混进法师候选
    assert "和平领域" not in values


def test_candidates_capped(plugins, tmp_path):
    """扩展候选很多时截到上限，且 SRD 那条仍在最前。"""
    rows = [
        _subclass_row(f"测试子职{i:02d}", f"Test Subclass {i:02d}", source="PHB", page=200 + i)
        for i in range(progression.MAX_SUBCLASS_CANDIDATES + 10)
    ]
    db_path = tmp_path / "kiwee-many.sqlite3"
    stats = kiwee_db.build_kiwee_db(_write_raw(tmp_path / "raw-many", rows), db_path)
    assert stats["categories"]["subclass"] == len(rows)
    runtime.register_service("dnd5e", kiwee.SERVICE_NAME, kiwee.KiweeDbService(db_path=db_path))
    try:
        candidates = _candidates(_plan())
    finally:
        runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)
    assert len(candidates) == progression.MAX_SUBCLASS_CANDIDATES
    assert candidates[0]["value"] == "evocation"
    assert sum(1 for item in candidates if item["source"] == "kiwee") == (
        progression.MAX_SUBCLASS_CANDIDATES - 1
    )


# ---------------------------------------------------------------- 降级


def test_missing_kiwee_db_keeps_srd_only(plugins, tmp_path):
    """服务在册但库文件缺席（深库未建）→ 静默只剩 SRD，计划照常生成。"""
    runtime.register_service(
        "dnd5e", kiwee.SERVICE_NAME, kiwee.KiweeDbService(db_path=tmp_path / "absent.sqlite3")
    )
    plan = _plan()
    assert [item["value"] for item in _candidates(plan)] == ["evocation"]
    assert plan["subclass"]["counts"] == {"srd": 1, "kiwee": 0}
    # 深库缺席时手填只能落在 SRD 名单上
    with pytest.raises(progression.ProgressionError) as missing:
        progression.apply_plan(plan["plan_id"], _choices("战争魔法"), {"sheet": _wizard_sheet(1)})
    assert missing.value.code == "progression.subclass_unknown"


def test_kiwee_service_absent_keeps_srd_only(plugins):
    """服务表里根本没有 kiwee（插件未装/旧核心）→ 同样只剩 SRD，不报错。"""
    runtime.unregister_service("dnd5e", kiwee.SERVICE_NAME)
    plan = _plan()
    assert [item["value"] for item in _candidates(plan)] == ["evocation"]
    assert plan["subclass"]["level_due"] is True


# ---------------------------------------------------------------- 落库校验


def test_apply_accepts_kiwee_subclass_name(plugins, kiwee_service):
    """kiwee 子职名可以落库（候选值与手填中文名两条路都通）。"""
    plan = _plan()
    result = progression.apply_plan(
        plan["plan_id"], _choices("战争魔法"), {"sheet": _wizard_sheet(1)}
    )
    assert result["sheet"]["subclass"] == "战争魔法"
    assert result["sheet"]["level"] == 2
    assert "subclass" in {item["field"] for item in result["changes"]}


def test_apply_accepts_kiwee_subclass_english_name(plugins, kiwee_service):
    """英文名也认（深库 eng_name 命中），落库仍是用户提交的名字。"""
    plan = _plan()
    result = progression.apply_plan(
        plan["plan_id"], _choices("Bladesinging"), {"sheet": _wizard_sheet(1)}
    )
    assert result["sheet"]["subclass"] == "Bladesinging"


def test_apply_rejects_unknown_subclass_without_claiming_plan(plugins, kiwee_service):
    """SRD 与深库都没有的名字 → 可读错误，且计划不被占用、卡面不变。"""
    plan = _plan()
    with pytest.raises(progression.ProgressionError) as rejected:
        progression.apply_plan(
            plan["plan_id"], _choices("瞎编的学派"), {"sheet": _wizard_sheet(1)}
        )
    assert rejected.value.code == "progression.subclass_unknown"
    assert "瞎编的学派" in rejected.value.message
    assert "SRD" in rejected.value.message
    assert progression._PLANS[plan["plan_id"]].get("claimed") is None

    # 计划还能用：换成合法子职立刻通过
    result = progression.apply_plan(
        plan["plan_id"], _choices("时间魔法"), {"sheet": _wizard_sheet(1)}
    )
    assert result["sheet"]["subclass"] == "时间魔法"


def test_apply_rejects_overlong_free_text(plugins, kiwee_service):
    """长度上限仍在（先于深库查询的廉价校验）。"""
    plan = _plan()
    overlong = "长" * (progression.MAX_SUBCLASS_NAME + 1)
    with pytest.raises(progression.ProgressionError) as rejected:
        progression.apply_plan(plan["plan_id"], _choices(overlong), {"sheet": _wizard_sheet(1)})
    assert rejected.value.code == "progression.invalid_option"
    assert str(progression.MAX_SUBCLASS_NAME) in rejected.value.message
