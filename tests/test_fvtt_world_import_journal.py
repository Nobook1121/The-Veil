"""fvtt-world-import 第四刀（M1 收官）：journal/folders → 剧本场景文本 + 世界树。

覆盖（两包真实 zip 对账 + AI 回调注入与降级 + 门控）：

- 纯转换层（convert_journal）：heroes 2 条目 7 页全部产出场景文本模块
  （DC 检定散文逐字保留）、ownership → public/hidden/kp_only 三态、章节
  （folders）线性 + 场景挂点拓扑、AI 切块/摘要/边推断的注入与逐项降级
  （回调抛错 / 非逐字切块 / 悬空边）；
- ``POST /api/plugins/fvtt-world-import/rooms/<id>/journal/run``：heroes
  7 模块并入房间绑定剧本（kp 材料不进玩家侧）+ 图谱合并进 narrative.json
  + 升版重钉 + 幂等重导；sixian journal 空库 → 0 条 + 警告（不报错、
  剧本不动）；集合缺席的包同样优雅返回；
- inspect 报告补 journal 计数（条目 / 页 / 可转换页 / 章节）；
- 门控：房管权限、非 dnd5e 房间、无绑定剧本（有产物时才要求）。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
FVTT_ENTRY = PLUGIN_DIR / "entry.py"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fvtt_world_import import world_package as wp  # noqa: E402
from fvtt_world_import.convert_journal import (  # noqa: E402
    convert_world_journal,
    page_plain_text,
    page_visibility,
)

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fvtt = _load_module(FVTT_ENTRY, "fvtt_world_import_entry_journal_test")


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


@pytest.fixture()
def plugins():
    """装载插件运行时（与 actors 刀同一装置；journal 落库不依赖 veil-map）。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


# -- 装置 ------------------------------------------------------------------


def _write_room(rooms: Path, room_id: str, *, ruleset_id: str = "dnd5e", owner_id: int = 1, scenario_id: int | None = 101) -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "id": room_id,
        "name": "世界包 journal 导入测试",
        "room_code": room_id.upper(),
        "creator_id": str(owner_id),
        "ruleset_id": ruleset_id,
        "members": [
            {
                "user_id": str(owner_id),
                "username": "alice" if owner_id == 1 else "bob",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Alice"},
            },
            {
                "user_id": "2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Bob"},
            },
        ],
    }
    if scenario_id is not None:
        info["scenario_id"] = str(scenario_id)
        info["scenario_version"] = "1.0.0"
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def _write_scenario(scenarios: Path, scenario_id: int = 101) -> None:
    sdir = scenarios / f"scenario-{scenario_id}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(
        json.dumps(
            {
                "id": scenario_id,
                "title": "测试剧本",
                "author": "alice",
                "ruleset_id": "dnd5e",
                "scenario_version": "1.0.0",
                "modules": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _make_app(tmp_path: Path, *, ruleset_id: str = "dnd5e", scenario: bool = True, owner_id: int = 1):
    """dnd5e 房间 + 绑定剧本的测试应用工厂（AI 平台目录缺失 → 语义增强降级）。"""
    rooms = tmp_path / "rooms"
    _write_room(rooms, "room1", ruleset_id=ruleset_id, owner_id=owner_id, scenario_id=101 if scenario else None)
    scenarios = tmp_path / "scenarios"
    if scenario:
        _write_scenario(scenarios, 101)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=rooms,
        CONFIG_DIR=str(config),
        SCENARIOS_DIR=scenarios,
    )
    app.register_blueprint(fvtt.bp)
    return app.test_client(), rooms, scenarios, tmp_path / "config"


def _login(client, user_id: int, username: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=username, role="USER")


def _upload(client, zip_path: Path, *, user_id: int = 1, username: str = "alice"):
    _login(client, user_id, username)
    with open(zip_path, "rb") as handle:
        return client.post(
            "/api/plugins/fvtt-world-import/rooms/room1/journal/run",
            data={"file": (handle, zip_path.name)},
            content_type="multipart/form-data",
        )


def _scenario_modules(scenarios: Path) -> list[dict[str, Any]]:
    payload = json.loads(
        (scenarios / "scenario-101" / "scenario.json").read_text(encoding="utf-8")
    )
    return list(payload.get("modules") or [])


def _room_info(rooms: Path) -> dict[str, Any]:
    return json.loads((rooms / "room1" / "info.json").read_text(encoding="utf-8"))


def _narrative(scenarios: Path) -> dict[str, Any]:
    return json.loads(
        (scenarios / "scenario-101" / "narrative.json").read_text(encoding="utf-8")
    )


def _tiny_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "world.json",
            json.dumps({"id": "demo", "title": "演示世界", "system": "dnd5e", "coreVersion": "13.351"}),
        )
    return buffer.getvalue()


def _read_journal_from_zip(zip_path: Path):
    """真实 zip → (JournalIndex, world_title)；供纯转换层测试直接用。"""
    from fvtt_world_import.world_package import extract_package

    with open(zip_path, "rb") as handle:
        raw = handle.read()
    with tempfile.TemporaryDirectory(prefix="fvtt-journal-test-") as workdir:
        pkg = extract_package(raw, workdir)
        index = wp.read_journal(pkg)
        title = pkg.summary().get("title") or ""
    return index, title


def _fake_index(entries: list[dict[str, Any]], pages: list[dict[str, Any]]) -> types.SimpleNamespace:
    """合成 JournalIndex（ownership / 空正文等形态的定向用例）。"""
    pages_by_entry: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        pages_by_entry.setdefault(str(page.get("journal_id")), []).append(page)
    return types.SimpleNamespace(
        entries={str(entry["_id"]): entry for entry in entries},
        pages=pages_by_entry,
        folders={},
        scene_names={},
        scene_active=set(),
        orphan_pages=0,
    )


# -- inspect 报告：journal 计数 --------------------------------------------


def test_inspect_reports_journal_counts(tmp_path, plugins):
    client, _rooms, _scenarios, _config = _make_app(tmp_path)
    _login(client, 1, "alice")
    with open(_require_zip(HEROES_ZIP), "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/inspect",
            data={"file": (handle, HEROES_ZIP.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["journals"] == 2
    assert data["journal_pages"] == 7
    assert data["journal_convertible"] == 7
    assert data["journal_folders"] == 1
    assert {row["title"] for row in data["journals_detail"]} == {"规则卡（DM 专用）", "玩家 Handouts"}

    # sixian：journal 空库——计数如实为 0，且给出空库警告，不是报错。
    with open(_require_zip(SIXIAN_ZIP), "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/inspect",
            data={"file": (handle, SIXIAN_ZIP.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert (data["journals"], data["journal_pages"], data["journal_convertible"]) == (0, 0, 0)
    assert any("空库" in text for text in data["warnings"])


# -- 纯转换层：heroes 全量对账 ----------------------------------------------


def test_convert_heroes_modules_visibility_and_dc_prose():
    index, _title = _read_journal_from_zip(_require_zip(HEROES_ZIP))
    result = convert_world_journal(index, world_title="英雄向远山而行")
    totals = result["totals"]
    assert totals["journals"] == 2 and totals["pages"] == 7 and totals["modules"] == 7
    assert totals["scene_anchors"] == 4
    assert not result["warnings"]

    modules = {row["title"]: row for row in result["modules"]}
    # 规则卡（DM 专用，ownership.default=0 LIMITED）→ kp：不进玩家侧。
    for title in ("塞巴斯蒂安好感度规则", "渡鸦女神与丹山市背景", "渡鸦女神的庆典恩赐"):
        assert modules[title]["visibility"] == "kp"
    # 玩家 Handouts（ownership.default=2 OWNER）→ public。
    for title in ("剧本（上）：哈特和海莉的分歧", "剧本（下）：哈特和海莉的分歧", "龙首谜语（玩家版）", "渡鸦女神餐前祷文"):
        assert modules[title]["visibility"] == "public"

    # DC 检定散文逐字保留（§5.2：检定线索不丢失）。
    assert "敏捷豁免 DC 14，失败受到 5（2d4）点火焰伤害，成功减半" in modules["龙首谜语（玩家版）"]["content"]
    assert "（宗教 DC10）" in modules["渡鸦女神与丹山市背景"]["content"]
    assert "（历史 DC15）" in modules["渡鸦女神与丹山市背景"]["content"]
    # 模块溯源标记：幂等重导的去重键。
    assert all(row.get("fvtt_page_id") for row in result["modules"])

    bundle = result["bundle"]
    nodes = {node["id"]: node for node in bundle["nodes"]}
    # 章节树：folder act + 条目 area + 页节点；DM 页三态 hidden。
    assert any(node["kind"] == "act" for node in bundle["nodes"])
    areas = [node for node in bundle["nodes"] if node.get("fvtt_kind") == "journal_entry"]
    assert {node["title"] for node in areas} == {"规则卡（DM 专用）", "玩家 Handouts"}
    dm_pages = [
        node for node in bundle["nodes"]
        if node.get("fvtt_page_id", "").startswith("PPBsxqWbcvd0eRSH")
    ]
    assert len(dm_pages) == 3 and all(node["visibility"] == "hidden" for node in dm_pages)
    player_pages = [
        node for node in bundle["nodes"]
        if node.get("fvtt_page_id", "").startswith("pK2aoP1feATz3tA9")
    ]
    assert len(player_pages) == 4 and all(node["visibility"] == "public" for node in player_pages)
    # 检定散文页 → clue 节点；纯叙事页 → concept。
    kinds = {node["title"]: node["kind"] for node in bundle["nodes"]}
    assert kinds["龙首谜语（玩家版）"] == "clue"
    assert kinds["渡鸦女神餐前祷文"] == "concept"

    edges = bundle["edges"]
    edge_keys = {(row["type"], row["from"], row["to"]) for row in edges}
    assert any(row["type"] == "contains" for row in edges)
    assert any(row["type"] == "next" for row in edges)
    # 场景挂点：剧本（上）/（下）文本提到「孤儿院」→ mentions 边到场景锚点。
    mentions = [row for row in edges if row["type"] == "mentions"]
    orphan_anchor = next(
        node_id for node_id, node in nodes.items()
        if node.get("fvtt_kind") == "scene_anchor" and node["title"] == "孤儿院"
    )
    assert any(row["to"] == orphan_anchor for row in mentions)
    assert all(row["to"] in nodes and row["from"] in nodes for row in edges)


# -- 纯转换层：ownership 三态的定向用例（合成 index）------------------------


def test_page_visibility_mapping_three_states():
    entry = {"ownership": {"default": 2}}
    assert page_visibility(entry, {"ownership": {"default": -1}})[0] == "public"  # 页未配置 → 继承条目 OWNER
    assert page_visibility({"ownership": {"default": 0}}, {})[0] == "hidden"  # LIMITED：只见标题
    assert page_visibility({"ownership": {"default": -1}}, {})[0] == "kp_only"  # NONE：玩家侧不存在
    assert page_visibility({"ownership": {"default": 2}}, {"ownership": {"default": 0}})[0] == "hidden"  # 页显式收紧
    assert page_visibility({}, {})[0] == "hidden"  # 条目缺省按 FVTT 默认 LIMITED


def test_convert_synthetic_kp_only_entry_stays_out_of_player_side():
    index = _fake_index(
        entries=[
            {
                "_id": "dmE",
                "name": "KP 真相",
                "folder": "",
                "sort": 0,
                "ownership": {"default": -1},
            }
        ],
        pages=[
            {
                "_id": "dmE.p1",
                "journal_id": "dmE",
                "name": "幕后真相",
                "type": "text",
                "sort": 0,
                "ownership": {"default": -1},
                "text": {"content": "<p>凶手是园丁（隐藏 DC 12 察觉）。</p>"},
            }
        ],
    )
    result = convert_world_journal(index)
    assert result["totals"]["modules"] == 1
    module = result["modules"][0]
    assert module["visibility"] == "kp"  # 玩家侧拿不到
    node = next(node for node in result["bundle"]["nodes"] if node.get("fvtt_kind") == "journal_page")
    assert node["visibility"] == "kp_only"
    assert node["kp_notes"] and "园丁" in node["body"]


# -- AI 回调注入与降级 ------------------------------------------------------


def test_convert_with_injected_chunk_and_summary_callbacks():
    index, _title = _read_journal_from_zip(_require_zip(HEROES_ZIP))

    calls = {"chunk": 0, "summary": 0}

    def chunk_page(page):
        calls["chunk"] += 1
        text = str(page.get("text") or "")
        if not text:
            return [{"kind": "scene", "text": text}]
        # 真实语义切块：对半切两块，文本逐字摘录原文（子串校验可过）。
        half = len(text) // 2
        cut = text.find("\n", half)
        cut = cut if cut > 0 else half
        return [
            {"kind": "clue", "title": "前半", "text": text[:cut]},
            {"kind": "kp_note", "title": "DM 注", "text": text[cut:]},
        ]

    def summarize(title, text):
        calls["summary"] += 1
        return f"【AI】{title}"

    result = convert_world_journal(index, chunk_page=chunk_page, summarize=summarize)
    assert calls["chunk"] == 7
    # 切块后每页 2 节点（前半 + DM 私注）。
    assert result["totals"]["chunks"] == 14
    assert not result["degradations"]

    # DM 私注块：visibility 收紧为 kp_only、正文进 kp_notes、body 留空。
    kp_note_nodes = [
        node for node in result["bundle"]["nodes"]
        if node.get("chunk_kind") == "kp_note"
    ]
    assert len(kp_note_nodes) == 7
    assert all(node["visibility"] == "kp_only" and not node["body"] and node["kp_notes"] for node in kp_note_nodes)
    # 玩家侧节点仍按页级可见性（Handouts 页的前半块 public）。
    handout_chunk = next(
        node for node in result["bundle"]["nodes"]
        if node.get("fvtt_page_id") == "pK2aoP1feATz3tA9.FjXaPR3dJymGXnXG"
        and node.get("chunk_kind") == "clue"
    )
    assert handout_chunk["visibility"] == "public"
    # 摘要 marker 走注入回调。
    assert handout_chunk["summary"].startswith("【AI】")
    # 模块仍是整页原文（场景文本条目不因切块丢字）。
    modules = {row["title"]: row for row in result["modules"]}
    assert "敏捷豁免 DC 14" in modules["龙首谜语（玩家版）"]["content"]


def test_convert_ai_callback_failures_degrade_deterministically():
    index, _title = _read_journal_from_zip(_require_zip(HEROES_ZIP))

    def bad_chunk(page):
        raise RuntimeError("模型超时")

    def bad_summary(title, text):
        raise RuntimeError("摘要服务不可用")

    def bad_edges(node_rows):
        raise RuntimeError("边推断超限")

    result = convert_world_journal(index, chunk_page=bad_chunk, summarize=bad_summary, infer_edges=bad_edges)
    # 降级不阻塞：产物齐全，且逐项给出稳定 code 告警。
    assert result["totals"]["modules"] == 7
    assert result["totals"]["chunks"] == 7  # 整页确定性单块
    assert result["degradations"]["journal.ai_chunk_failed"] == 7
    assert result["degradations"]["journal.ai_summary_failed"] >= 1
    assert result["degradations"]["journal.ai_edges_failed"] == 1
    # 确定性降级仍保住 DC 原文与可见性。
    modules = {row["title"]: row for row in result["modules"]}
    assert "敏捷豁免 DC 14" in modules["龙首谜语（玩家版）"]["content"]
    assert modules["塞巴斯蒂安好感度规则"]["visibility"] == "kp"
    assert all(node["summary"] for node in result["bundle"]["nodes"])


def test_convert_rejects_non_verbatim_chunks_and_dangling_edges():
    index, _title = _read_journal_from_zip(_require_zip(HEROES_ZIP))

    def paraphrase_chunk(page):
        # 一块逐字子串（保住 kept 非空）+ 一块改写（子串校验不过 → 丢块）；
        # 覆盖不足 60% → 整页确定性降级。
        text = str(page.get("text") or "")
        return [
            {"kind": "scene", "text": text[:40]},
            {"kind": "scene", "text": "这是模型改写过的概括，不是原文。"},
        ]

    def dangling_edges(node_rows):
        return [
            {"from": "不存在的节点", "to": node_rows[0]["id"], "type": "leads_to"},
            {"from": node_rows[0]["id"], "to": node_rows[0]["id"], "type": "alias"},
            {"from": node_rows[0]["id"], "to": node_rows[-1]["id"], "type": "提到"},  # 非法类型 → mentions
        ]

    result = convert_world_journal(index, chunk_page=paraphrase_chunk, infer_edges=dangling_edges)
    assert result["totals"]["chunks"] == 7  # 全部整页降级
    assert result["degradations"]["journal.ai_chunk_not_verbatim"] == 7
    assert result["degradations"]["journal.ai_chunk_coverage"] == 7
    edges = result["bundle"]["edges"]
    # 悬空端点与自环被丢弃；非法类型收紧为 mentions；合法推断边保留 1 条。
    assert not any(row["from"] == "不存在的节点" for row in edges)
    assert not any(row["from"] == row["to"] for row in edges)
    ai_edges = [row for row in edges if row.get("source") == "ai"]
    assert len(ai_edges) == 1 and ai_edges[0]["type"] == "mentions" and ai_edges[0]["confidence"] < 1


def test_convert_empty_journal_returns_zero_with_warning():
    index, _title = _read_journal_from_zip(_require_zip(SIXIAN_ZIP))
    result = convert_world_journal(index)
    assert result["totals"] == {
        "journals": 0, "pages": 0, "modules": 0, "nodes": 0, "edges": 0,
        "chunks": 0, "scene_anchors": 0, "warnings": 1,
    }
    assert any("空库" in text for text in result["warnings"])
    # 空库不建场景锚点（锚点是 journal 驱动的挂点，不是场景刀的职责）。
    assert result["bundle"]["nodes"] == []


# -- 端点：heroes 落库 + 图谱 + 幂等 ---------------------------------------


def test_heroes_run_imports_modules_and_narrative_graph(tmp_path, plugins):
    client, rooms, scenarios, _config = _make_app(tmp_path)
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]

    # 测试应用没有 AI 平台配置 → 语义增强降级为确定性路径（告警如实上报）。
    assert any("AI 语义增强不可用" in text for text in data["warnings"])
    assert data["totals"]["modules"] == 7
    assert len(data["journals"]) == 7
    assert data["scenario"]["scenario_version"] == "1.0.1"

    # 场景文本条目：按剧本存储格式（scenario.json modules）落盘。
    modules = _scenario_modules(scenarios)
    assert len(modules) == 7
    journal_modules = [m for m in modules if m.get("fvtt_page_id")]
    assert len(journal_modules) == 7
    kp_titles = {m["title"] for m in journal_modules if m["visibility"] == "kp"}
    assert kp_titles == {"塞巴斯蒂安好感度规则", "渡鸦女神与丹山市背景", "渡鸦女神的庆典恩赐"}
    riddle = next(m for m in journal_modules if m["title"] == "龙首谜语（玩家版）")
    assert "敏捷豁免 DC 14" in riddle["content"]
    assert (scenarios / "scenario-101" / "versions" / "1.0.1.json").exists()
    assert _room_info(rooms)["scenario_version"] == "1.0.1"

    # 世界树图谱：narrative.json（剧本存储原生支持的独立图谱文件）。
    narrative = _narrative(scenarios)
    assert narrative["schema_version"] == 1
    nodes = {node["id"]: node for node in narrative["nodes"]}
    assert len(nodes) == 14  # 1 act + 2 area + 7 页 + 4 场景锚点
    dm_node = nodes["fvtt-journal-PPBsxqWbcvd0eRSH_x48IioaSzQBSWdhd"]
    assert dm_node["visibility"] == "hidden"
    handout = nodes["fvtt-journal-pK2aoP1feATz3tA9_FjXaPR3dJymGXnXG"]
    assert handout["visibility"] == "public"
    assert any(node.get("fvtt_kind") == "scene_anchor" for node in narrative["nodes"])
    assert narrative["meta"]["journal"]["world_title"] == "英雄向远山而行"
    # 边的端点全部存在（from_dict 的悬空边裁剪在保存前兜了一层）。
    assert all(row["to"] in nodes and row["from"] in nodes for row in narrative["edges"])
    # narrative.json 可被核心运行时原样加载（round-trip 校验）。
    from trpg_server.agents.narrative.model import NarrativeBundle

    bundle = NarrativeBundle.from_dict(narrative)
    assert len(bundle.nodes) == 14 and bundle.node(dm_node["id"]) is not None


def test_heroes_run_is_idempotent(tmp_path, plugins):
    client, rooms, scenarios, _config = _make_app(tmp_path)
    assert _upload(client, _require_zip(HEROES_ZIP)).status_code == 200
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200

    # 同 fvtt_page_id 模块先移除再追加：数量不翻倍；图谱节点同样不翻倍。
    assert len(_scenario_modules(scenarios)) == 7
    narrative = _narrative(scenarios)
    assert len(narrative["nodes"]) == 14
    assert _room_info(rooms)["scenario_version"] == "1.0.2"
    assert (scenarios / "scenario-101" / "versions" / "1.0.2.json").exists()


def test_heroes_run_preserves_author_nodes_in_narrative(tmp_path, plugins):
    """图谱合并：作者手写节点原样保留，只有 journal 产物被替换。"""
    client, _rooms, scenarios, _config = _make_app(tmp_path)
    author_node = {
        "id": "hall_of_raven",
        "kind": "location",
        "title": "渡鸦大厅（作者手写）",
        "summary": "作者自己写的地点",
        "body": "手写正文",
        "visibility": "public",
    }
    storage = scenarios / "scenario-101"
    (storage / "narrative.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario_id": "101",
                "scenario_version": "1.0.0",
                "topology": "sandbox",
                "topology_source": "author",
                "nodes": [author_node],
                "edges": [],
                "warnings": [],
                "meta": {"author_note": "保留我"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    narrative = _narrative(scenarios)
    nodes = {node["id"]: node for node in narrative["nodes"]}
    assert "hall_of_raven" in nodes and nodes["hall_of_raven"]["title"] == "渡鸦大厅（作者手写）"
    assert narrative["topology"] == "sandbox" and narrative["topology_source"] == "author"
    assert narrative["meta"]["author_note"] == "保留我"
    assert narrative["meta"]["journal"]["world_title"] == "英雄向远山而行"
    assert len(nodes) == 15  # 作者 1 + journal 14


def test_heroes_run_with_injected_ai_callbacks(tmp_path, plugins, monkeypatch):
    """端点注入假 AI 回调：切块/摘要生效且产物落盘（不发起任何网络请求）。"""
    client, _rooms, scenarios, _config = _make_app(tmp_path)

    def summarize(title, text):
        return f"AI 摘要：{title}"

    monkeypatch.setattr(
        fvtt,
        "make_journal_ai_callbacks",
        lambda: {
            "chunk_page": lambda page: [{"kind": "clue", "text": str(page.get("text") or "")}],
            "summarize": summarize,
            "infer_edges": lambda rows: [],
            "warning": None,
        },
    )
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    # 假回调整页单块（逐字子串校验通过）→ 7 页 7 块。
    assert data["totals"]["chunks"] == 7
    narrative = _narrative(scenarios)
    nodes = [node for node in narrative["nodes"] if node.get("fvtt_kind") == "journal_page"]
    assert all(node["summary"].startswith("AI 摘要：") for node in nodes)
    assert narrative["meta"]["journal"]["ai_summarized"] is True
    assert narrative["meta"]["journal"]["ai_chunked"] is True


# -- 端点：sixian 空库与集合缺席 -------------------------------------------


def test_sixian_run_returns_zero_with_warning(tmp_path, plugins):
    client, rooms, scenarios, _config = _make_app(tmp_path)
    rv = _upload(client, _require_zip(SIXIAN_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["totals"]["journals"] == 0 and data["totals"]["modules"] == 0
    assert data["journals"] == []
    assert any("空库" in text for text in data["warnings"])
    # 空库：剧本不动（不升版、不写模块、不写图谱）。
    assert _scenario_modules(scenarios) == []
    assert _room_info(rooms)["scenario_version"] == "1.0.0"
    assert not (scenarios / "scenario-101" / "narrative.json").exists()


def test_run_accepts_package_without_journal_collection(tmp_path, plugins):
    client, _rooms, scenarios, _config = _make_app(tmp_path)
    _login(client, 1, "alice")
    rv = client.post(
        "/api/plugins/fvtt-world-import/rooms/room1/journal/run",
        data={"file": (io.BytesIO(_tiny_zip()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["totals"]["modules"] == 0
    assert any("journal" in text for text in data["warnings"])
    assert not (scenarios / "scenario-101" / "narrative.json").exists()


# -- 门控 ------------------------------------------------------------------


def test_run_requires_room_manager(tmp_path, plugins):
    client, _rooms, _scenarios, _config = _make_app(tmp_path)
    rv = _upload(client, _require_zip(HEROES_ZIP), user_id=2, username="bob")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.manager_only"


def test_run_rejects_non_dnd5e_room(tmp_path, plugins):
    client, _rooms, _scenarios, _config = _make_app(tmp_path, ruleset_id="coc7")
    _login(client, 1, "alice")
    rv = client.post(
        "/api/plugins/fvtt-world-import/rooms/room1/journal/run",
        data={"file": (io.BytesIO(_tiny_zip()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.ruleset_unsupported"


def test_run_rejects_room_without_scenario_when_pages_exist(tmp_path, plugins):
    # 包里有 journal 产物而房间没有绑定剧本：整体拒绝，不落半份数据。
    client, _rooms, scenarios, _config = _make_app(tmp_path, scenario=False)
    rv = _upload(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.scenario_missing"
    assert not (scenarios / "scenario-101").exists()


def test_page_plain_text_converts_html_structure():
    page = {
        "type": "text",
        "text": {
            "content": (
                "<h2>标题</h2><p>第一段。</p><blockquote><p>引用内容</p></blockquote>"
                "<ul><li>要点一</li></ul><p>宗教 <strong>DC 15</strong>（检定）。</p>"
            )
        },
    }
    text = page_plain_text(page)
    assert "标题" in text and "第一段。" in text
    assert "> 引用内容" in text
    assert "- 要点一" in text
    assert "宗教 DC 15（检定）。" in text  # 行内标签剔除、实体反转义、正文不改写
    assert "<" not in text
    # 非文本页（image/pdf）正文按空处理。
    assert page_plain_text({"type": "image", "src": "a.png", "text": {"content": "x"}}) == ""
