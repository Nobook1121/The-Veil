"""fvtt-world-import 转换与登记台端点：两包真实 zip 的对账测试。

覆盖（第二刀交付）：

- 纯转换层（convert_scenes）：两包逐场景 grid/偏移/墙/光/棋子计数对账、
  像素坐标直供（墙折线、token 位置）、六边形与网格出窗的拒绝、伴生 JSON
  兜底路径；
- 审计端点 ``POST /api/plugins/fvtt-world-import/inspect``：报告字段、
  world.json 校验（system/coreVersion/结构）、体积上限与登录门；
- 落库端点 ``POST /api/plugins/fvtt-world-import/rooms/<id>/run``：走
  Flask test client + 真实 veil-map ``map_service``，落盘后逐场景核对
  grid_size/grid_offset/calibration、墙/光/棋子计数、
  ``token 格坐标 = 像素 ÷ grid.size 向下取整``（对 grid 整数倍尺寸的场
  景直接对账；奇数尺寸场景按世界像素 + 同余对账）、背景资产与激活态。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from trpg_server.plugins import runtime as plugin_runtime

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
VEIL_ENTRY = REPO_ROOT / "data" / "plugins" / "veil-map" / "entry.py"
FVTT_ENTRY = PLUGIN_DIR / "entry.py"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fvtt_world_import import convert_scenes as cs  # noqa: E402
from fvtt_world_import import world_package as wp  # noqa: E402

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


veil_map = _load_module(VEIL_ENTRY, "veil_map_entry_fvtt")
fvtt = _load_module(FVTT_ENTRY, "fvtt_world_import_entry_test")


def _require_zip(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"样例包缺失：{path}")
    return path


# -- 装置：两包全量转换（模块级缓存，zip 缺失则跳过） ----------------------


class _IndexSnapshot:
    """read_scenes 结果的纯数据快照（临时目录删除后仍可用）。"""

    def __init__(self, index) -> None:
        self.documents = dict(index.documents)
        self.walls = {k: list(v) for k, v in index.walls.items()}
        self.lights = {k: list(v) for k, v in index.lights.items()}
        self.tokens = {k: list(v) for k, v in index.tokens.items()}
        self.orphans = list(index.orphans)

    def scene_id_of_background(self, fragment: str) -> str:
        for scene_id, doc in self.documents.items():
            if fragment in ((doc.get("background") or {}).get("src") or ""):
                return scene_id
        raise AssertionError(f"找不到背景含 {fragment!r} 的场景")


def _convert_package(zip_path: Path, tmp_path_factory: pytest.TempPathFactory):
    """全流程纯函数转换：解包 → 读 LevelDB → 转换；返回 (converted, skipped, warnings, 快照)。"""
    raw = _require_zip(zip_path).read_bytes()
    workdir = tmp_path_factory.mktemp(zip_path.stem)
    pkg = wp.extract_package(raw, workdir)
    try:
        index = wp.read_scenes(pkg)

        def resolve_background(scene_doc: dict[str, Any]):
            rel = pkg.resolve_background(cs.scene_background_src(scene_doc))
            if rel is None:
                return None
            return {"path": rel, "name": Path(rel).name, "bytes": pkg.asset_size(rel)}

        def companion_for(scene_doc: dict[str, Any]):
            base = Path(cs.scene_background_src(scene_doc)).name
            candidate = f"assets/scenes/{Path(base).stem}.json"
            if not pkg.has_asset(candidate):
                return None
            try:
                data = json.loads(pkg.read_asset(candidate).decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                return None
            return data if isinstance(data, dict) else None

        converted, skipped, warnings = cs.convert_world_scenes(
            index,
            resolve_background=resolve_background,
            companion_for=companion_for,
        )
        return converted, skipped, warnings, _IndexSnapshot(index)
    finally:
        pkg._archive.close()


@pytest.fixture(scope="module")
def heroes_convert(tmp_path_factory: pytest.TempPathFactory):
    return _convert_package(HEROES_ZIP, tmp_path_factory)


@pytest.fixture(scope="module")
def sixian_convert(tmp_path_factory: pytest.TempPathFactory):
    return _convert_package(SIXIAN_ZIP, tmp_path_factory)


# -- 纯转换层：heroes ------------------------------------------------------


def test_heroes_scene_grid_sizes_and_offsets(heroes_convert):
    converted, _skipped, _warnings, _index = heroes_convert
    by_bg = {item["background"]["path"]: item for item in converted}
    # 4 个场景：150×2、192、282。282（宅邸二层）落在 16–512 窗口内，全部转换。
    assert sorted(item["grid"]["size"] for item in converted) == [150, 150, 192, 282]
    mansion_1f = by_bg["assets/mansion-1f.jpg"]
    assert mansion_1f["grid"] == {"size": 192, "origin_x": 24.0, "origin_y": 92.0}
    mansion_2f = by_bg["assets/mansion-2f.jpg"]
    # 282 场景的原点直供：background.offsetX/offsetY = (-58, -50)。
    assert mansion_2f["grid"] == {"size": 282, "origin_x": -58.0, "origin_y": -50.0}
    orphanage = by_bg["assets/orphanage-v1.jpg"]
    assert orphanage["grid"]["origin_x"] == 0.0 and orphanage["grid"]["origin_y"] == 0.0
    rain = by_bg["assets/rain-ambush-v1.jpg"]
    assert rain["grid"]["size"] == 150


def test_heroes_accepts_282px_scene_without_skipping(heroes_convert):
    """282px/格的大格场景不再被 grid_range 跳过（旧上限 256 会整场丢弃）。"""
    converted, skipped, warnings, _index = heroes_convert
    assert len(converted) == 4
    assert skipped == []
    assert not any("grid_range" in str(item) for item in skipped)
    assert not any("不在" in text and "范围内" in text for text in warnings)
    large = next(item for item in converted if item["grid"]["size"] == 282)
    assert large["background"]["path"] == "assets/mansion-2f.jpg"
    assert len(large["tokens"]) == 3  # 宅邸二层的 3 个棋子一并带入


def test_heroes_counts_match_source(heroes_convert):
    converted, skipped, _warnings, index = heroes_convert
    assert len(converted) + len(skipped) == len(index.documents)
    assert sum(len(item["walls"]) for item in converted) == 23  # 全部属于孤儿院场景
    assert sum(len(item["lights"]) for item in converted) == 2
    assert sum(len(item["tokens"]) for item in converted) == 17  # 4 场景全量
    for item in converted:
        source_id = item["source_scene_id"]
        assert item["source_counts"] == {
            "walls": len(index.walls.get(source_id) or []),
            "lights": len(index.lights.get(source_id) or []),
            "tokens": len(index.tokens.get(source_id) or []),
        }


def test_heroes_walls_keep_image_pixels_and_door_kinds(heroes_convert):
    converted, _skipped, _warnings, index = heroes_convert
    orphanage = next(item for item in converted if "orphanage" in item["background"]["path"])
    source_walls = index.walls[orphanage["source_scene_id"]]
    assert len(orphanage["walls"]) == 23
    doors = [w for w in orphanage["walls"] if w["kind"] == "door"]
    windows = [w for w in orphanage["walls"] if w["kind"] == "window"]
    walls = [w for w in orphanage["walls"] if w["kind"] == "wall"]
    assert len(doors) == 3 and len(windows) == 1 and len(walls) == 19
    assert all(w["state"] == "closed" for w in doors)  # 源包 ds=0
    # 墙折线原样保持图片像素坐标（不做第二套几何换算）。
    first = orphanage["walls"][0]
    source_first = next(w for w in source_walls if (w.get("_id") or "").endswith(first["id"]))
    assert [first["points"][0]["x"], first["points"][0]["y"]] == [
        int(source_first["c"][0]),
        int(source_first["c"][1]),
    ]
    assert [first["points"][1]["x"], first["points"][1]["y"]] == [
        int(source_first["c"][2]),
        int(source_first["c"][3]),
    ]


def test_heroes_tokens_keep_pixels_and_disposition_colors(heroes_convert):
    converted, _skipped, _warnings, index = heroes_convert
    rain = next(item for item in converted if "rain-ambush" in item["background"]["path"])
    assert len(rain["tokens"]) == 10
    # 子文档内嵌自己的 _id（单段），转换器原样保留为棋子 id。
    source = {t["_id"]: t for t in index.tokens[rain["source_scene_id"]]}
    for token in rain["tokens"]:
        doc = source[token["id"]]
        assert token["x"] == int(doc["x"]) and token["y"] == int(doc["y"])
        expected = cs.DISPOSITION_COLORS.get(int(doc["disposition"]), cs.DISPOSITION_COLORS[0])
        assert token["color"] == expected
        assert token["kind"] == "npc"
        assert token["w"] == int(doc["width"]) and token["h"] == int(doc["height"])
    # 隐藏棋子照常导入（计数对账），并给出可读警告。
    assert any("隐藏" in text for item in converted for text in item["warnings"])


def test_heroes_lights_radius_in_cells(heroes_convert):
    converted, _skipped, _warnings, _index = heroes_convert
    rain = next(item for item in converted if "rain-ambush" in item["background"]["path"])
    (light,) = rain["lights"]
    # dim 60ft / bright 30ft，每格 5ft → 12 格 / 6 格。
    assert light["range_cells"] == 12 and light["bright_cells"] == 6
    assert light["intensity"] == pytest.approx(0.4)
    assert light["angle"] == 360


def test_heroes_ambient_from_environment(heroes_convert):
    converted, _skipped, _warnings, _index = heroes_convert
    rain = next(item for item in converted if "rain-ambush" in item["background"]["path"])
    orphanage = next(item for item in converted if "orphanage" in item["background"]["path"])
    assert rain["ambient"]["darkness"] == pytest.approx(0.6)
    assert rain["ambient"]["enabled"] is False
    assert orphanage["ambient"]["enabled"] is True
    assert orphanage["ambient"]["darkness"] == pytest.approx(0.0)


def test_heroes_active_scene_marked(heroes_convert):
    converted, _skipped, _warnings, index = heroes_convert
    active = [item for item in converted if item["active"]]
    assert len(active) == 1
    source_active = [sid for sid, doc in index.documents.items() if doc.get("active")]
    assert active[0]["source_scene_id"] == source_active[0]


# -- 纯转换层：sixian ------------------------------------------------------


def test_sixian_all_scenes_convertible(sixian_convert):
    converted, skipped, warnings, _index = sixian_convert
    assert len(converted) == 6 and not skipped and not warnings
    assert all(item["grid"]["size"] == 150 for item in converted)
    assert all(item["grid"]["origin_x"] == 0.0 and item["grid"]["origin_y"] == 0.0 for item in converted)
    assert all(item["ambient"]["enabled"] for item in converted)


def test_sixian_counts_match_source(sixian_convert):
    converted, _skipped, _warnings, index = sixian_convert
    assert sum(len(item["walls"]) for item in converted) == 482
    assert sum(len(item["lights"]) for item in converted) == 200
    assert sum(len(item["tokens"]) for item in converted) == 0
    for item in converted:
        source_id = item["source_scene_id"]
        assert len(item["walls"]) == len(index.walls[source_id])
        assert len(item["lights"]) == len(index.lights[source_id])
    per_scene = {item["background"]["path"]: len(item["walls"]) for item in converted}
    assert per_scene["assets/scenes/elegant-chess-chamber.jpg"] == 56
    assert per_scene["assets/scenes/crypt-valhalla.jpg"] == 128
    assert per_scene["assets/scenes/manor-floor-1.jpg"] == 118
    assert per_scene["assets/scenes/manor-floor-2.jpg"] == 111
    assert per_scene["assets/scenes/valhalla-tavern.jpg"] == 51
    assert per_scene["assets/scenes/wedding-day-mansion.jpg"] == 18


def test_sixian_lights_radius_reconciles(sixian_convert):
    converted, _skipped, _warnings, index = sixian_convert
    tavern = next(item for item in converted if "valhalla-tavern" in item["background"]["path"])
    by_pos = {(int(l["x"]), int(l["y"])): l for l in tavern["lights"]}
    for doc in index.lights[tavern["source_scene_id"]]:
        light = by_pos[(int(doc["x"]), int(doc["y"]))]
        dim = float(doc["config"]["dim"])
        bright = float(doc["config"]["bright"])
        assert light["range_cells"] == max(1, int(round(dim / 5)))
        assert light["bright_cells"] == min(light["range_cells"], int(round(bright / 5)))


# -- 纯转换层：拒绝与伴生 JSON 兜底 ----------------------------------------


def _scene_doc(**overrides):
    doc = {
        "name": "测试场景",
        "width": 3000,
        "height": 1500,
        "active": False,
        "grid": {"size": 150, "type": 1, "distance": 5, "units": "ft"},
        "background": {"src": "worlds/demo/assets/map.jpg", "offsetX": 0, "offsetY": 0},
    }
    doc.update(overrides)
    return doc


def test_convert_scene_rejects_hex_grid():
    with pytest.raises(cs.SceneSkip) as excinfo:
        cs.convert_scene(
            _scene_doc(grid={"size": 150, "type": 5, "distance": 5, "units": "ft"}),
            scene_id="hex1",
            background={"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
        )
    assert excinfo.value.code == "fvtt.grid_hex"


@pytest.mark.parametrize("grid_size", [16, 256, 257, 282, 512])
def test_convert_scene_accepts_grid_within_window(grid_size):
    """16–512 全部放行：256 是旧上限，282 是 heroes「宅邸（二层）」的实际值。"""
    scene = cs.convert_scene(
        _scene_doc(grid={"size": grid_size, "type": 1, "distance": 5, "units": "ft"}),
        scene_id=f"grid{grid_size}",
        background={"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
    )
    assert scene["grid"]["size"] == grid_size


@pytest.mark.parametrize("grid_size", [0, 15, 513, 1024])
def test_convert_scene_rejects_grid_out_of_window(grid_size):
    with pytest.raises(cs.SceneSkip) as excinfo:
        cs.convert_scene(
            _scene_doc(grid={"size": grid_size, "type": 1, "distance": 5, "units": "ft"}),
            scene_id="big",
            background={"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
        )
    assert excinfo.value.code == "fvtt.grid_range"


def test_convert_scene_rejects_missing_background():
    with pytest.raises(cs.SceneSkip) as excinfo:
        cs.convert_scene(_scene_doc(), scene_id="nobg", background=None)
    assert excinfo.value.code == "fvtt.background_missing"


def test_convert_scene_rejects_non_feet_units():
    with pytest.raises(cs.SceneSkip) as excinfo:
        cs.convert_scene(
            _scene_doc(grid={"size": 100, "type": 1, "distance": 1.5, "units": "m"}),
            scene_id="metric",
            background={"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
        )
    assert excinfo.value.code == "fvtt.grid_units"


def test_convert_world_scenes_collects_hex_skip_with_warning():
    class _Index:
        documents = {"hexscene": _scene_doc(grid={"size": 150, "type": 5, "distance": 5, "units": "ft"})}
        walls: dict[str, list] = {}
        lights: dict[str, list] = {}
        tokens: dict[str, list] = {}
        orphans: list[str] = []

    converted, skipped, warnings = cs.convert_world_scenes(
        _Index(),
        resolve_background=lambda doc: {"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
    )
    assert not converted
    assert skipped[0]["code"] == "fvtt.grid_hex"
    assert any("六边形" in text for text in warnings)


def test_convert_scene_uses_companion_fallback():
    """LevelDB 子集合缺位时用伴生 JSON（sixianyuchanmian 形态）兜底，shift 直供偏移。"""
    companion = {
        "name": "伴生场景",
        "grid": 150,
        "gridDistance": 5.0,
        "gridUnits": "ft",
        "shiftX": 30,
        "shiftY": -45,
        "width": 3000,
        "height": 1500,
        "darkness": 0.3,
        "globalLight": False,
        "walls": [
            {"c": [100, 200, 400, 200], "move": 1, "sense": 2, "sound": 1, "door": 0},
            {"c": [400, 200, 400, 500], "move": 1, "sense": 1, "sound": 1, "door": 1},
        ],
        "lights": [
            {"x": 500, "y": 600, "dim": 25.0, "bright": 12.5, "tintColor": "#FF9800", "tintAlpha": 0.5}
        ],
    }
    payload = cs.convert_scene(
        _scene_doc(name="主文档名"),
        scene_id="withcompanion",
        walls=None,
        lights=None,
        tokens=[],
        companion=companion,
        background={"path": "assets/map.jpg", "name": "map.jpg", "bytes": 10},
    )
    assert payload["grid"] == {"size": 150, "origin_x": 30.0, "origin_y": -45.0}
    assert len(payload["walls"]) == 2
    # sense=2（受限）按阻挡；sense=1（透明）挡移动 → 窗；门走 door 字段。
    assert payload["walls"][0]["blocks_sight"] is True
    assert payload["walls"][1]["kind"] == "door"
    (light,) = payload["lights"]
    # dim 25ft → 5 格；bright 12.5ft → round(2.5) = 2（与 veil-map 导入器同款取整）。
    assert light["range_cells"] == 5 and light["bright_cells"] == 2
    assert light["intensity"] == pytest.approx(0.5)
    assert payload["ambient"]["darkness"] == pytest.approx(0.3)


def test_safe_scene_id_shape():
    assert cs.safe_scene_id("Ab12Xy99") == "fvtt-Ab12Xy99"
    assert cs.safe_scene_id("weird id!!") == "fvtt-weirdid"
    assert cs.safe_scene_id("").startswith("fvtt-")


# -- HTTP 端点装置 ---------------------------------------------------------


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event, payload, room=None, **_kwargs):
        self.events.append({"event": event, "payload": payload, "room": room})


def _write_room(rooms: Path, room_id: str, *, ruleset_id: str = "dnd5e") -> None:
    room = rooms / room_id
    room.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "世界包导入测试",
        "room_code": room_id.upper(),
        "creator_id": "u1",
        "ruleset_id": ruleset_id,
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Alice"},
            },
            {
                "user_id": "u2",
                "username": "bob",
                "room_role": "member",
                "status": "active",
                "is_active": True,
                "character_card": {"name": "Bob"},
            },
        ],
    }
    (room / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def app_env(tmp_path):
    """dnd5e 房间 + 已注册真实 map_service 的测试应用工厂。"""

    def _make(*, ruleset_id: str = "dnd5e", with_service: bool = True):
        rooms = tmp_path / "rooms"
        _write_room(rooms, "room1", ruleset_id=ruleset_id)
        app = Flask(__name__)
        app.secret_key = "test"
        app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
        socket = _FakeSocket()
        app.extensions["socketio"] = socket
        app.register_blueprint(fvtt.bp)
        if with_service:
            plugin_runtime.register_service("veil-map", "map_service", veil_map._MAP_SERVICE)
        return app.test_client(), app, rooms, socket

    yield _make
    plugin_runtime.clear_plugin_runtime()


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session_data:
        session_data.update(user_id=user_id, username=user_id, role="USER")


def _upload(client, url: str, zip_path: Path, *, user_id: str | None = "u1", form: dict | None = None):
    if user_id:
        _login(client, user_id)
    data = dict(form or {})
    with open(zip_path, "rb") as handle:
        data["file"] = (handle, zip_path.name)
        return client.post(url, data=data, content_type="multipart/form-data")


def _tiny_world_zip(world: dict[str, Any] | str | None = None) -> bytes:
    payload = world if world is not None else {
        "id": "demo",
        "title": "演示世界",
        "system": "dnd5e",
        "coreVersion": "13.351",
        "systemVersion": "5.3.3",
        "version": "1.0.0",
        "compatibility": {"minimum": "13", "verified": "13.351"},
    }
    if isinstance(payload, str):
        content = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("world.json", content)
    return buffer.getvalue()


# -- inspect 端点 ---------------------------------------------------------


def test_inspect_reports_heroes_plan(app_env):
    client, _app, _rooms, _socket = app_env()
    rv = _upload(client, "/api/plugins/fvtt-world-import/inspect", HEROES_ZIP)
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["system"] == "dnd5e" and data["core_version"] == "13.351"
    assert data["version"] == "0.1.6"
    assert data["scenes"] == 4 and data["convertible_scenes"] == 4
    assert data["tokens"] == 17 and data["walls"] == 23 and data["lights"] == 2
    assert data["collections"]["actors"] is True and data["collections"]["journal"] is True
    # 282 网格场景（宅邸二层）现在可转换，不再进「网格像素不在范围内」警告。
    assert not any("网格像素" in text for text in data["warnings"])
    assert not any("282" in text for text in data["warnings"])
    active = [item for item in data["scenes_detail"] if item["active"]]
    assert len(active) == 1 and active[0]["convertible"] is True


def test_inspect_reports_sixian_plan(app_env):
    client, _app, _rooms, _socket = app_env()
    rv = _upload(client, "/api/plugins/fvtt-world-import/inspect", SIXIAN_ZIP)
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["scenes"] == 6 and data["convertible_scenes"] == 6
    assert data["tokens"] == 0 and data["walls"] == 482 and data["lights"] == 200
    # actors 审计如实上报：14 个条目级效果按降级策略丢弃（仅计数）——**信息级
    # notes**，不再混进 warnings（正常降级不该长得像错误）；
    # journal 第四刀：sixian 的 journal 是空库，仍如实警告（0 条可转换）。
    assert data["warnings"] == ["journal 集合为空库，没有可转换的 journal 条目。"]
    assert data["notes"] == [
        "已忽略 14 个条目级效果（transfer buff）——新版本此为正常降级："
        "条目自身字段（AC/伤害/重量等）已按文档折算进角色卡，"
        "这类「装备后自动加值」的附加效果不进产物、不在本系统运行时生效。"
    ]
    assert data["actor_item_effects"] == 14


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"this is not a zip at all", "fvtt.package_not_zip"),
        (_tiny_world_zip(), "fvtt.scenes_missing"),
        (_tiny_world_zip({"system": "coc7", "coreVersion": "13.351"}), "fvtt.system_unsupported"),
        (_tiny_world_zip({"system": "dnd5e", "coreVersion": "11.315"}), "fvtt.core_version_unsupported"),
        (_tiny_world_zip({"system": "dnd5e", "coreVersion": "13.351", "compatibility": {}}), "fvtt.scenes_missing"),
    ],
)
def test_inspect_rejects_invalid_packages(app_env, raw, code):
    client, _app, _rooms, _socket = app_env()
    _login(client, "u1")
    rv = client.post(
        "/api/plugins/fvtt-world-import/inspect",
        data={"file": (io.BytesIO(raw), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == code


def test_inspect_rejects_zip_without_world_json(app_env):
    client, _app, _rooms, _socket = app_env()
    _login(client, "u1")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "no world here")
    rv = client.post(
        "/api/plugins/fvtt-world-import/inspect",
        data={"file": (io.BytesIO(buffer.getvalue()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.world_json_missing"


def test_inspect_rejects_broken_world_json(app_env):
    client, _app, _rooms, _socket = app_env()
    _login(client, "u1")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("world.json", "{ not valid json")
    rv = client.post(
        "/api/plugins/fvtt-world-import/inspect",
        data={"file": (io.BytesIO(buffer.getvalue()), "world.zip")},
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.world_json_invalid"


def test_inspect_requires_login_and_file(app_env):
    client, _app, _rooms, _socket = app_env()
    rv = client.post("/api/plugins/fvtt-world-import/inspect", data={})
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.login_required"
    _login(client, "u1")
    rv = client.post("/api/plugins/fvtt-world-import/inspect", data={})
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "fvtt.package_missing"


def test_inspect_enforces_size_limit(app_env, monkeypatch):
    client, _app, _rooms, _socket = app_env()
    monkeypatch.setattr(wp, "MAX_PACKAGE_BYTES", 1024)
    rv = _upload(client, "/api/plugins/fvtt-world-import/inspect", HEROES_ZIP)
    assert rv.status_code == 413
    assert rv.get_json()["error"] == "fvtt.package_too_large"


# -- run 端点 -------------------------------------------------------------


def _wrap_offset(value: float, grid: int) -> int:
    """veil-map 的格线偏移折回公式（import_map/calibrate_map 同款）。"""
    return (int(round(value)) + grid // 2) % grid - grid // 2


def _run(client, zip_path: Path, *, form: dict | None = None):
    return _upload(client, "/api/plugins/fvtt-world-import/rooms/room1/run", zip_path, form=form)


def test_run_imports_heroes_scenes_into_room(app_env):
    client, _app, rooms, socket = app_env()
    rv = _run(client, HEROES_ZIP)
    assert rv.status_code == 200, rv.get_data(as_text=True)
    data = rv.get_json()["data"]
    assert data["world"]["version"] == "0.1.6"
    # 4 个场景全部入库：282px/格的「宅邸（二层）」不再被网格窗口跳过。
    assert len(data["scenes"]) == 4
    assert data["skipped"] == []
    assert data["totals"] == {"scenes": 4, "skipped": 0, "walls": 23, "lights": 2, "tokens": 17}
    large = next(item for item in data["scenes"] if item["grid_size"] == 282)
    assert large["background"]["width"] == 7500
    assert large["background"]["height"] == 2570

    room_dir = rooms / "room1"
    scene_ids = {item["scene_id"] for item in data["scenes"]}
    on_disk = {
        path.name
        for path in (room_dir / "plugins" / "veil-map" / "scenes").iterdir()
        if path.is_dir()
    }
    assert scene_ids <= on_disk

    # 激活的是 FVTT 标记 active 的场景（雨中伏击），并按观察者广播给房间成员。
    active_id = data["active_scene_id"]
    assert active_id == next(item["scene_id"] for item in data["scenes"] if item["active"])
    assert veil_map.scene_store.active_scene_id(room_dir) == active_id
    assert socket.events
    assert all(event["event"] == "room_map_changed" for event in socket.events)
    assert all(event["payload"]["room_id"] == "room1" for event in socket.events)

    for item in data["scenes"]:
        raw = veil_map.scene_store.load_scene(room_dir, item["scene_id"])
        assert raw is not None, f"场景 {item['scene_id']} 未落盘"
        state = veil_map._normalize_map(raw)
        assert state["title"] == item["title"]
        assert state["grid_size"] == item["grid_size"]
        assert len(state["walls"]) == item["walls"]
        assert len(state["lights"]) == item["lights"]
        assert len(state["tokens"]) == item["tokens"]
        assert state["revision"] == item["revision"]
        # 背景资产落在房间资产区，URL 指向 veil-map 的服务路由。
        assert state["background"].startswith(f"/api/plugins/veil-map/rooms/room1/assets/maps/")
        asset = (
            room_dir / "plugins" / "veil-map" / "assets" / "maps" / state["background"].rsplit("/", 1)[1]
        )
        assert asset.is_file() and asset.stat().st_size > 0
        assert state["background_meta"]["width"] > 0


def test_run_reconciles_grid_offset_calibration_and_token_cells(app_env, heroes_convert):
    """落库后逐场景对账：grid_offset/calibration 来自直供原点，token 格坐标 = 像素÷grid。"""
    converted, _skipped, _warnings, _index = heroes_convert
    client, _app, rooms, _socket = app_env()
    rv = _run(client, HEROES_ZIP)
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    room_dir = rooms / "room1"
    payloads = {item["scene_id"]: item for item in converted}

    for summary in data["scenes"]:
        payload = payloads[summary["scene_id"]]
        state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, summary["scene_id"]))
        grid = payload["grid"]["size"]
        origin_x, origin_y = payload["grid"]["origin_x"], payload["grid"]["origin_y"]
        width = state["background_meta"]["width"]
        height = state["background_meta"]["height"]
        # 格线偏移 = (图内原点 − 图片中心) 折回一格周期；calibration 保存图内原点取模。
        assert state["grid_offset"]["x"] == _wrap_offset(origin_x - width / 2.0, grid)
        assert state["grid_offset"]["y"] == _wrap_offset(origin_y - height / 2.0, grid)
        assert state["calibration"]["offset_x"] == int(round(origin_x)) % grid
        assert state["calibration"]["offset_y"] == int(round(origin_y)) % grid
        # 墙折线 = 图片像素 − 图片中心（整数换算）。
        saved_walls = {w["id"]: w for w in state["walls"]}
        for wall in payload["walls"]:
            saved = saved_walls[wall["id"]]
            assert saved["points"][0]["x"] == int(round(wall["points"][0]["x"] - width / 2.0))
            assert saved["points"][0]["y"] == int(round(wall["points"][0]["y"] - height / 2.0))
            assert saved["points"][1]["x"] == int(round(wall["points"][1]["x"] - width / 2.0))
            assert saved["points"][1]["y"] == int(round(wall["points"][1]["y"] - height / 2.0))
        # 世界像素保全：token 落在世界坐标 = 图片像素 − 图片中心（四舍五入）。
        saved_tokens = {t["id"]: t for t in state["tokens"]}
        for token in payload["tokens"]:
            saved = saved_tokens[token["id"]]
            assert saved["x"] == int(round(token["x"] - width / 2.0))
            assert saved["y"] == int(round(token["y"] - height / 2.0))

    # token 落位对账：格编号相对「折回后的格线原点」计数（veil-map 把大偏移折回
    # 一格周期，格号与图内编号相差整数格 —— 物理位置不变）。所以这里对账三件套：
    # ① 世界像素 = 图片像素 − 图片中心；② 格线偏移 ≡ (图内原点 − 图片中心) mod grid；
    # ③ token 世界像素 = 格线原点 + (cell + 格内小数) × grid（自洽）。
    rain = next(item for item in converted if "rain-ambush" in item["background"]["path"])
    state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, rain["scene_id"]))
    saved_tokens = {t["id"]: t for t in state["tokens"]}
    assert len(saved_tokens) == 10
    for token in rain["tokens"]:
        saved = saved_tokens[token["id"]]
        assert saved["cell_x"] == math.floor((saved["x"] - state["grid_offset"]["x"]) / 150)
        assert saved["cell_y"] == math.floor((saved["y"] - state["grid_offset"]["y"]) / 150)

    # 192 网格 + 非零偏移（mansion-1f，奇数高度）同样自洽。
    mansion = next(item for item in converted if "mansion-1f" in item["background"]["path"])
    state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, mansion["scene_id"]))
    offset_x, offset_y = state["grid_offset"]["x"], state["grid_offset"]["y"]
    saved_tokens = {t["id"]: t for t in state["tokens"]}
    assert len(saved_tokens) == 4
    for token in mansion["tokens"]:
        saved = saved_tokens[token["id"]]
        assert saved["x"] == int(round((saved["cell_x"] + saved["offset_x"]) * 192)) + offset_x
        assert saved["y"] == int(round((saved["cell_y"] + saved["offset_y"]) * 192)) + offset_y
        assert saved["cell_x"] == math.floor((saved["x"] - offset_x) / 192)
        assert saved["cell_y"] == math.floor((saved["y"] - offset_y) / 192)

    # 282 网格（宅邸二层）：格坐标换算仍是「像素 ÷ grid 向下取整」，无 256 假设。
    big = next(item for item in converted if "mansion-2f" in item["background"]["path"])
    state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, big["scene_id"]))
    assert state["grid_size"] == 282
    offset_x, offset_y = state["grid_offset"]["x"], state["grid_offset"]["y"]
    saved_tokens = {t["id"]: t for t in state["tokens"]}
    assert len(saved_tokens) == 3
    for token in big["tokens"]:
        saved = saved_tokens[token["id"]]
        assert saved["cell_x"] == math.floor((saved["x"] - offset_x) / 282)
        assert saved["cell_y"] == math.floor((saved["y"] - offset_y) / 282)
        assert saved["x"] == int(round((saved["cell_x"] + saved["offset_x"]) * 282)) + offset_x
        assert saved["y"] == int(round((saved["cell_y"] + saved["offset_y"]) * 282)) + offset_y


def _png_bytes(size: int) -> bytes:
    """最小灰度 PNG（无 Pillow 依赖），供服务级直接公式测试。"""
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes([200]) * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def test_map_service_token_cell_direct_formula(app_env):
    """token 格坐标 = 像素 ÷ grid.size 向下取整（折回零平移的小图直接对账）。

    64×64 底图、grid 64、图内原点 (0,0)：格线偏移折回后不动，落库格坐标
    与图内公式完全一致。
    """
    client, _app, rooms, _socket = app_env()
    room_dir = rooms / "room1"
    info = json.loads((room_dir / "info.json").read_text(encoding="utf-8"))
    payload = {
        "scene_id": "fvtt-direct-formula",
        "title": "直接公式校验",
        "grid": {"size": 64, "origin_x": 0.0, "origin_y": 0.0},
        "image": {"name": "tiny.png", "bytes": _png_bytes(64)},
        "walls": [
            {"id": "w1", "kind": "wall", "points": [{"x": 0, "y": 0}, {"x": 64, "y": 64}]}
        ],
        "lights": [],
        "tokens": [
            {"id": "t1", "name": "甲", "x": 10, "y": 10, "w": 1, "h": 1, "kind": "npc", "color": "#c8a35a"},
            {"id": "t2", "name": "乙", "x": 70, "y": 130, "w": 1, "h": 1, "kind": "npc", "color": "#e06666"},
        ],
        "ambient": {"enabled": False, "color": "#ffffff", "intensity": 1.0, "darkness": 0.0, "baked": False},
    }
    service = plugin_runtime.get_service("veil-map", "map_service")
    assert service is not None
    outcome = service.import_scene(room_dir, info, payload, activate=False)
    assert outcome["ok"], outcome
    state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, outcome["scene_id"]))
    tokens = {t["id"]: t for t in state["tokens"]}
    assert tokens["t1"]["cell_x"] == math.floor(10 / 64) == 0
    assert tokens["t1"]["cell_y"] == math.floor(10 / 64) == 0
    assert tokens["t2"]["cell_x"] == math.floor(70 / 64) == 1
    assert tokens["t2"]["cell_y"] == math.floor(130 / 64) == 2


def test_run_imports_sixian_scenes(app_env):
    client, _app, rooms, _socket = app_env()
    rv = _run(client, SIXIAN_ZIP)
    assert rv.status_code == 200, rv.get_data(as_text=True)
    data = rv.get_json()["data"]
    assert len(data["scenes"]) == 6 and data["skipped"] == []
    assert data["totals"] == {"scenes": 6, "skipped": 0, "walls": 482, "lights": 200, "tokens": 0}
    room_dir = rooms / "room1"
    tavern = next(item for item in data["scenes"] if item["walls"] == 51)
    state = veil_map._normalize_map(veil_map.scene_store.load_scene(room_dir, tavern["scene_id"]))
    assert state["grid_size"] == 150
    assert state["grid_offset"] == {"x": 0, "y": 0}
    assert state["ambient_light"]["enabled"] is True
    assert len(state["walls"]) == 51 and len(state["lights"]) == 31
    assert state["calibration"]["offset_x"] == 0 and state["calibration"]["offset_y"] == 0


def test_run_activate_false_keeps_room_scene_pointer_absent(app_env):
    client, _app, rooms, _socket = app_env()
    rv = _run(client, HEROES_ZIP, form={"activate": "false"})
    assert rv.status_code == 200
    data = rv.get_json()["data"]
    assert data["activate"] is False
    assert data["active_scene_id"] == ""
    # 不切换房间当前场景：房间此前没有场景指针，保持为空。
    root_pointer = rooms / "room1" / "plugins" / "veil-map" / "scenes" / "active.json"
    assert not root_pointer.exists()


def test_run_requires_room_membership(app_env):
    client, _app, _rooms, _socket = app_env()
    rv = _upload(client, "/api/plugins/fvtt-world-import/rooms/room1/run", HEROES_ZIP, user_id="u9")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "room.access_denied"


def test_run_requires_manager(app_env):
    client, _app, _rooms, _socket = app_env()
    rv = _upload(client, "/api/plugins/fvtt-world-import/rooms/room1/run", HEROES_ZIP, user_id="u2")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "fvtt.manager_only"


def test_run_without_map_service_returns_unavailable(tmp_path):
    rooms = tmp_path / "rooms"
    _write_room(rooms, "room1")
    app = Flask(__name__)
    app.secret_key = "test"
    app.config.update(ROOMS_DIR=str(rooms), CONFIG_DIR=str(tmp_path / "config"))
    app.extensions["socketio"] = _FakeSocket()
    app.register_blueprint(fvtt.bp)
    client = app.test_client()
    rv = _upload(client, "/api/plugins/fvtt-world-import/rooms/room1/run", HEROES_ZIP)
    assert rv.status_code == 503
    assert rv.get_json()["error"] == "fvtt.map_unavailable"


def test_run_rejects_non_map_ruleset(app_env):
    client, _app, _rooms, _socket = app_env(ruleset_id="coc7")
    rv = _run(client, HEROES_ZIP)
    assert rv.status_code == 400
    assert rv.get_json()["error"] == "map.ruleset_disabled"


def test_run_reimport_updates_scenes_in_place(app_env):
    """同一世界重复导入：场景 id 稳定、revision 递增而不是重复建目录。"""
    client, _app, rooms, _socket = app_env()
    first = _run(client, HEROES_ZIP)
    assert first.status_code == 200
    first_data = first.get_json()["data"]
    second = _run(client, HEROES_ZIP)
    assert second.status_code == 200
    second_data = second.get_json()["data"]
    assert {item["scene_id"] for item in first_data["scenes"]} == {
        item["scene_id"] for item in second_data["scenes"]
    }
    for before, after in zip(
        sorted(first_data["scenes"], key=lambda item: item["scene_id"]),
        sorted(second_data["scenes"], key=lambda item: item["scene_id"]),
    ):
        assert before["created"] is True and after["created"] is False
        assert after["revision"] > before["revision"]
    scenes_root = rooms / "room1" / "plugins" / "veil-map" / "scenes"
    ids = {path.name for path in scenes_root.iterdir() if path.is_dir()}
    assert ids == {item["scene_id"] for item in second_data["scenes"]}
