"""fvtt-world-import 第五刀（M5 第一段）：登记台自动建剧本 + 一键开团指引。

覆盖（两包真实 zip 对账 + 幂等 + 门闸 + 孤儿防御）：

- ``POST /api/plugins/fvtt-world-import/scenario/import``（登录即可）：
  heroes → 直接产出可开团的新剧本条目（标题/描述/封面取 world.json，封面
  内容寻址落剧本 ``assets/``；journal 场景文本模块 + 图谱 ``narrative.json``
  写入新剧本；版本取 world.json version；inspect 的审计计数进响应）；
- 幂等：同 world id 重导 → 溯源标记定位既有剧本原位升版（0.1.6 → 0.1.7），
  journal 产物整体替换不翻倍、不生成重复剧本，NPC 模块（actors 刀）保留；
- sixian（journal 空库）→ 剧本条目照常产出（标题/描述/封面仍在，
  场景列表如实为空），narrative.json 不写（第四刀语义）；
- inspect 报告补 ``scenario_would_create`` 摘要（标题/版本/模块数预估）；
- 门闸：未登录 403；没有 scenes 集合的包被审计拒绝（可读错误）；
- 孤儿防御：图谱写失败 → 新建路径整目录清理；升版路径回滚剧本记录。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
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

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fvtt = _load_module(FVTT_ENTRY, "fvtt_world_import_entry_scenario_test")


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


@pytest.fixture()
def plugins():
    """装载插件运行时（与 journal/actors 刀同一装置；本刀不依赖 veil-map）。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


# -- 装置 ------------------------------------------------------------------


def _make_app(tmp_path: Path):
    """空剧本库的测试应用工厂（登记台不需要房间，只写 SCENARIOS_DIR）。"""
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "general.toml").write_text("", encoding="utf-8")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(
        ROOMS_DIR=tmp_path / "rooms",
        CONFIG_DIR=str(config),
        SCENARIOS_DIR=scenarios,
    )
    app.register_blueprint(fvtt.bp)
    return app.test_client(), scenarios


def _login(client, user_id: int = 1, username: str = "alice") -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=username, role="USER")


def _import(client, zip_path: Path, *, user_id: int = 1, username: str = "alice"):
    _login(client, user_id, username)
    with open(zip_path, "rb") as handle:
        return client.post(
            "/api/plugins/fvtt-world-import/scenario/import",
            data={"file": (handle, zip_path.name)},
            content_type="multipart/form-data",
        )


def _scenario_dirs(scenarios: Path) -> list[Path]:
    return sorted(path for path in scenarios.iterdir() if path.is_dir())


def _record(scenarios: Path, scenario_id: int) -> dict[str, Any]:
    payload = json.loads(
        (scenarios / f"scenario-{scenario_id}" / "scenario.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def _narrative(scenarios: Path, scenario_id: int) -> dict[str, Any]:
    return json.loads(
        (scenarios / f"scenario-{scenario_id}" / "narrative.json").read_text(encoding="utf-8")
    )


def _tiny_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "world.json",
            json.dumps({"id": "demo", "title": "演示世界", "system": "dnd5e", "coreVersion": "13.351"}),
        )
    return buffer.getvalue()


# -- heroes：登记台产出可开团的剧本条目 ------------------------------------


def test_import_heroes_creates_openable_scenario(tmp_path, plugins):
    client, scenarios = _make_app(tmp_path)
    rv = _import(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]

    # 响应契约：scenario_id / title / version / counts / warnings / 开团指引。
    scenario_id = int(data["scenario_id"])
    assert scenario_id > 0
    assert data["title"] == "英雄向远山而行"
    assert data["version"] == "0.1.6"
    assert data["created"] is True
    counts = data["counts"]
    assert counts["modules"] == 7 and counts["journals"] == 2 and counts["pages"] == 7
    assert counts["nodes"] == 14 and counts["edges"] > 0
    assert counts["scenes"] == 4 and counts["characters"] == 10
    # heroes 的 4 个场景全部可转换（282px/格的宅邸二层在 16–512 窗口内）。
    assert counts["npcs"] == 12 and counts["convertible_scenes"] == 4
    # AI 语义增强**默认关闭** → 纯确定性切块（不读 AI 配置、不发网络请求）；
    # 「用没用 AI」是信息级说明（notes），不进 warnings。
    assert data["ai"]["enabled"] is False
    assert not any("AI 语义增强" in text for text in data["warnings"])
    assert any("确定性" in text for text in data["notes"])
    # 开团指引：剧本 id + 现有开房流程（POST /api/rooms scenario_id 直选）。
    open_room = data["open_room"]
    assert open_room["scenario_id"] == scenario_id
    assert "POST /api/rooms" in open_room["hint"]
    assert any("/rooms/<room_id>/run" in step for step in open_room["room_steps"])
    assert any("actors/run" in step for step in open_room["room_steps"])

    # 剧本记录：剧本库出现条目（storage/scenario.json + 版本文件）。
    record = _record(scenarios, scenario_id)
    assert record["title"] == "英雄向远山而行"
    assert record["scenario_version"] == "0.1.6"
    assert record["ruleset_id"] == "dnd5e"
    assert str(record["owner_id"]) == "1"
    assert record["creator_username"] == "alice"
    assert re.fullmatch(r"[A-Za-z0-9]{6}", str(record["public_id"]))
    assert record["import_mode"] == "fvtt_world"
    # 描述来自 world.json（HTML 剥除、实体反转义）。
    assert "D&D 5e" in record["description"] and "4-6" in record["description"]
    assert "<" not in record["description"]
    # 溯源标记：幂等定位键。
    marker = record["fvtt_world_import"]
    assert marker["world_id"] == "heroes-to-yuanshan"
    assert marker["world_version"] == "0.1.6"

    # 封面：内容寻址落剧本 assets/，URL 走 /assets/scenarios 静态路由。
    cover_url = str(record["cover"])
    assert cover_url.startswith(f"/assets/scenarios/scenario-{scenario_id}/assets/")
    assert re.search(r"assets/[0-9a-f]{16}\.jpg$", cover_url)
    cover_file = scenarios / cover_url.removeprefix("/assets/scenarios/")
    assert cover_file.is_file() and cover_file.stat().st_size > 0
    assert data["cover_url"] == cover_url

    # journal 产物直接写入新剧本：7 条场景文本（kp 材料不进玩家侧）。
    modules = list(record["modules"])
    journal_modules = [m for m in modules if m.get("fvtt_page_id")]
    assert len(journal_modules) == 7
    kp_titles = {m["title"] for m in journal_modules if m["visibility"] == "kp"}
    assert kp_titles == {"塞巴斯蒂安好感度规则", "渡鸦女神与丹山市背景", "渡鸦女神的庆典恩赐"}
    riddle = next(m for m in journal_modules if m["title"] == "龙首谜语（玩家版）")
    assert "敏捷豁免 DC 14" in riddle["content"]
    assert (scenarios / f"scenario-{scenario_id}" / "versions" / "0.1.6.json").is_file()

    # 图谱：narrative.json 合并进剧本目录（第四刀语义），可被核心原样加载。
    narrative = _narrative(scenarios, scenario_id)
    assert len(narrative["nodes"]) == 14  # 1 act + 2 area + 7 页 + 4 场景锚点
    assert narrative["meta"]["journal"]["world_title"] == "英雄向远山而行"
    assert narrative["scenario_id"] == str(scenario_id)
    assert narrative["scenario_version"] == "0.1.6"
    from trpg_server.agents.narrative.model import NarrativeBundle

    bundle = NarrativeBundle.from_dict(narrative)
    assert len(bundle.nodes) == 14
    assert data["narrative"] == {"written": True, "nodes": 14, "edges": len(bundle.edges)}

    # 场景与角色不进剧本条目：响应里给出房间维度的计数与指引，不落任何地图。
    assert not any(
        str(m.get("module_type")) == "monster" for m in modules
    )  # actors 刀产物只走房间维度


def test_import_heroes_is_idempotent_upgrades_in_place(tmp_path, plugins):
    client, scenarios = _make_app(tmp_path)
    first = _import(client, _require_zip(HEROES_ZIP))
    assert first.status_code == 200
    scenario_id = int(first.get_json()["data"]["scenario_id"])
    assets_before = {
        path.name for path in (scenarios / f"scenario-{scenario_id}" / "assets").iterdir()
    }
    # 第六刀后 assets/ 除封面外还有 stash 场景背景（heroes 4 个可转换场景）。
    assert len(assets_before) == 5

    second = _import(client, HEROES_ZIP)
    assert second.status_code == 200
    data = second.get_json()["data"]

    # 同 world id 重导：不生成重复剧本，原位升版覆盖本插件产物。
    assert data["scenario_id"] == scenario_id
    assert data["created"] is False
    assert data["version"] == "0.1.7"
    assert len(_scenario_dirs(scenarios)) == 1

    record = _record(scenarios, scenario_id)
    journal_modules = [m for m in record["modules"] if m.get("fvtt_page_id")]
    assert len(journal_modules) == 7  # 整体替换，不翻倍
    assert record["scenario_version"] == "0.1.7"
    assert record["fvtt_world_import"]["world_version"] == "0.1.6"
    assert (scenarios / f"scenario-{scenario_id}" / "versions" / "0.1.7.json").is_file()
    # 图谱节点同样不翻倍；封面与 stash 场景背景全部内容寻址——同图重导
    # 零增长（资产集合与首次导入完全一致，而不是只有封面一个文件）。
    assert len(_narrative(scenarios, scenario_id)["nodes"]) == 14
    assets_after = {
        path.name for path in (scenarios / f"scenario-{scenario_id}" / "assets").iterdir()
    }
    assert assets_after == assets_before


def test_import_upgrade_preserves_non_journal_modules(tmp_path, plugins):
    """升版只替换 journal 产物：actors 刀的 NPC 模块 / 作者手写模块原样保留。"""
    client, scenarios = _make_app(tmp_path)
    first = _import(client, _require_zip(HEROES_ZIP))
    scenario_id = int(first.get_json()["data"]["scenario_id"])

    # 模拟 actors 刀并入的 NPC statblock 模块（带 fvtt_actor_id 溯源标记）。
    record_path = scenarios / f"scenario-{scenario_id}" / "scenario.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["modules"].append(
        {
            "id": "npc-demo",
            "module_type": "monster",
            "title": "保留下来的 NPC",
            "content": "statblock",
            "visibility": "kp",
            "send_to_ai": True,
            "fvtt_actor_id": "actor-demo",
        }
    )
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    second = _import(client, HEROES_ZIP)
    assert second.status_code == 200
    updated = _record(scenarios, scenario_id)
    modules = updated["modules"]
    journal_modules = [m for m in modules if m.get("fvtt_page_id")]
    assert len(journal_modules) == 7  # journal 产物整体替换
    assert any(m.get("id") == "npc-demo" for m in modules)  # NPC 模块保留


def test_import_sixian_creates_scenario_without_journal(tmp_path, plugins):
    client, scenarios = _make_app(tmp_path)
    rv = _import(client, _require_zip(SIXIAN_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]

    scenario_id = int(data["scenario_id"])
    assert data["title"] == "丝线与缠绵"
    assert data["version"] == "1.0.1"
    assert data["counts"]["modules"] == 0 and data["counts"]["journals"] == 0
    assert any("空库" in text for text in data["warnings"])
    assert data["narrative"]["written"] is False

    # 剧本条目照常产出：标题/描述/封面仍在，场景列表如实为空。
    record = _record(scenarios, scenario_id)
    assert record["title"] == "丝线与缠绵"
    assert "D&D 5e" in record["description"]
    assert record["modules"] == [] and record["scenes"] == []
    cover_url = str(record["cover"])
    assert re.search(r"assets/[0-9a-f]{16}\.png$", cover_url)
    cover_file = scenarios / cover_url.removeprefix("/assets/scenarios/")
    assert cover_file.is_file() and cover_file.stat().st_size > 0  # 6.5MB 封面如实落盘

    # journal 为空库 → narrative.json 不写（第四刀语义）。
    assert not (scenarios / f"scenario-{scenario_id}" / "narrative.json").exists()
    # 剧本条目可被核心存储原样加载（合法剧本形态）。
    from trpg_server.scenario_store import load_scenario_by_id

    descriptor, loaded = load_scenario_by_id(scenarios, scenario_id)
    assert descriptor is not None and loaded is not None
    assert loaded["modules"] == []


def test_import_sixian_then_heroes_keeps_both_scenarios(tmp_path, plugins):
    """两个不同的世界包各建一个剧本条目，互不干扰。"""
    client, scenarios = _make_app(tmp_path)
    assert _import(client, _require_zip(SIXIAN_ZIP)).status_code == 200
    assert _import(client, _require_zip(HEROES_ZIP)).status_code == 200
    assert len(_scenario_dirs(scenarios)) == 2


# -- inspect：scenario_would_create 摘要 -----------------------------------


def test_inspect_reports_scenario_would_create(tmp_path, plugins):
    client, _scenarios = _make_app(tmp_path)
    _login(client)
    with open(_require_zip(HEROES_ZIP), "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/inspect",
            data={"file": (handle, HEROES_ZIP.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["scenario_would_create"] == {
        "title": "英雄向远山而行",
        "version": "0.1.6",
        "modules": 7,
    }

    with open(_require_zip(SIXIAN_ZIP), "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/inspect",
            data={"file": (handle, SIXIAN_ZIP.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["scenario_would_create"]["title"] == "丝线与缠绵"
    assert data["scenario_would_create"]["version"] == "1.0.1"
    assert data["scenario_would_create"]["modules"] == 0  # journal 空库如实预估


# -- 门闸与孤儿防御 ---------------------------------------------------------


def test_import_requires_login(tmp_path, plugins):
    client, _scenarios = _make_app(tmp_path)
    with open(_require_zip(HEROES_ZIP), "rb") as handle:
        rv = client.post(
            "/api/plugins/fvtt-world-import/scenario/import",
            data={"file": (handle, HEROES_ZIP.name)},
            content_type="multipart/form-data",
        )
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.login_required"


def test_import_rejects_package_without_scenes_collection(tmp_path, plugins):
    """审计复用 inspect：包形态不完整（无 scenes 集合）整包拒绝、零写入。"""
    client, scenarios = _make_app(tmp_path)
    _login(client)
    rv = client.post(
        "/api/plugins/fvtt-world-import/scenario/import",
        data={"file": (io.BytesIO(_tiny_zip()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.scenes_missing"
    assert _scenario_dirs(scenarios) == []


def test_import_cleans_up_half_written_scenario_on_narrative_failure(
    tmp_path, plugins, monkeypatch
):
    """孤儿防御（新建路径）：图谱写失败 → 整目录清理，库回到导入前状态。"""
    from trpg_server.agents.narrative.model import NarrativeBundle
    from trpg_server.agents.narrative.store import NarrativeStoreError

    def broken_save_bundle(storage_dir, bundle, **kwargs):
        raise NarrativeStoreError("磁盘拒绝写入")

    monkeypatch.setattr("trpg_server.agents.narrative.store.save_bundle", broken_save_bundle)
    client, scenarios = _make_app(tmp_path)
    rv = _import(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 500
    body = rv.get_json()
    assert body["error"] == "fvtt.narrative_write_failed"
    assert "剧本图谱写入失败" in body["message"]
    # 半成品清理：剧本目录整体移除，库里没有残留条目。
    assert _scenario_dirs(scenarios) == []
    # NarrativeBundle 只是引用（防御性 import 无副作用）。
    assert NarrativeBundle is not None


def test_import_rolls_back_upgraded_record_on_narrative_failure(
    tmp_path, plugins, monkeypatch
):
    """孤儿防御（升版路径）：图谱写失败 → 回滚剧本记录到导入前版本。"""
    from trpg_server.agents.narrative.store import NarrativeStoreError

    client, scenarios = _make_app(tmp_path)
    assert _import(client, _require_zip(HEROES_ZIP)).status_code == 200
    scenario_id = int(_record_from_dirs(scenarios)["id"])
    before = _record(scenarios, scenario_id)

    def broken_save_bundle(storage_dir, bundle, **kwargs):
        raise NarrativeStoreError("磁盘拒绝写入")

    monkeypatch.setattr("trpg_server.agents.narrative.store.save_bundle", broken_save_bundle)
    rv = _import(client, HEROES_ZIP)
    assert rv.status_code == 500
    body = rv.get_json()
    assert body["error"] == "fvtt.narrative_write_failed"
    assert "已回滚剧本记录" in body["message"]

    # 回滚后：版本与模块保持导入前状态，失败升版文件不留。
    after = _record(scenarios, scenario_id)
    assert after["scenario_version"] == "0.1.6"
    assert len([m for m in after["modules"] if m.get("fvtt_page_id")]) == 7
    assert not (scenarios / f"scenario-{scenario_id}" / "versions" / "0.1.7.json").exists()
    assert len(_scenario_dirs(scenarios)) == 1


def _record_from_dirs(scenarios: Path) -> dict[str, Any]:
    dirs = _scenario_dirs(scenarios)
    assert len(dirs) == 1
    return json.loads((dirs[0] / "scenario.json").read_text(encoding="utf-8"))


# -- 不阻塞导入 + 降噪（journal AI 默认确定性 / transfer buff 降为说明） ------


def _deterministic_journal_counts(zip_path: Path) -> dict[str, int]:
    """同一份包在「无 AI 回调」下的确定性转换结果（纯转换层，零端点）。"""
    from fvtt_world_import.convert_journal import convert_world_journal
    from fvtt_world_import.world_package import extract_package, read_journal

    import tempfile as _tempfile

    with open(zip_path, "rb") as handle:
        raw = handle.read()
    with _tempfile.TemporaryDirectory(prefix="fvtt-parity-") as workdir:
        pkg = extract_package(raw, workdir)
        index = read_journal(pkg)
        converted = convert_world_journal(index, world_title=str(pkg.summary().get("title") or ""))
    return dict(converted["totals"])


def test_import_quick_path_matches_deterministic_conversion(tmp_path, plugins):
    """快速导入（默认无 AI）= 确定性切块，产物与纯转换层逐项一致。"""
    client, _scenarios = _make_app(tmp_path)
    rv = _import(client, _require_zip(HEROES_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    expected = _deterministic_journal_counts(_require_zip(HEROES_ZIP))

    counts = data["counts"]
    assert data["ai"]["enabled"] is False
    assert counts["modules"] == expected["modules"] == 7
    assert counts["chunks"] == expected["chunks"]  # 确定性整页切块：每页一块
    assert counts["nodes"] == expected["nodes"]
    assert counts["edges"] == expected["edges"]
    assert data["degradations"] == {}
    # 「用没用 AI」是说明，不是警告。
    assert not any("AI 语义增强" in text for text in data["warnings"])
    assert any("确定性" in text for text in data["notes"])


def test_import_item_effect_notes_are_not_warnings(tmp_path, plugins):
    """条目级 transfer buff 计数进 notes（正常降级），不出现在 warnings。"""
    client, _scenarios = _make_app(tmp_path)
    rv = _import(client, _require_zip(SIXIAN_ZIP))
    assert rv.status_code == 200
    data = rv.get_json()["data"]

    assert not any("条目级效果" in text for text in data["warnings"])
    assert not any("transfer buff" in text for text in data["warnings"])
    notes = [text for text in data["notes"] if "条目级效果" in text]
    assert notes and "14 个" in notes[0]
    assert "正常降级" in notes[0]
    assert data["counts"]["notes"] == len(data["notes"])
