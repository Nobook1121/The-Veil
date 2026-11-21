"""M5 第二段：dnd5e 房规档案按房间剧本形态自动适配。

覆盖（夹具照 tests/test_room_house_rules.py：真 create_app + 真插件装载 +
真房间目录；地图服务照 tests/test_dnd5e_vtt_agent_tools.py 的
``_map_service`` monkeypatch 手法降级伪造）：

- 档案声明：两档案合法（键在 schema 内、值在枚举内）；theater-of-mind
  与 schema 全默认逐键相等（现状兼容）；grid-combat 差异仅在位置/对角键，
  夹击默认关；新键经核心房规 GET/PUT 通用校验可改（核心零改动）；
- 推荐链：挂活动场景 → grid-combat（自动应用一次、幂等不重写）；
  无 veil-map/无场景 → theater-of-mind（应用后值仍=默认，记忆照落）；
- 三态：手动设置（推荐前/推荐后改）→ manual、不再自动推荐、绝不回写；
  自动应用后房主手动改 → 翻 manual；站点配置关闭 → disabled、零写入；
- 玩家只读：普通成员调用不触发房间写入；
- 剧本形态升级：纯文本房先自动 tom、后挂地图 → 窗口内自动升档 grid；
- KP 提示词：``("dnd5e", "kp_context")`` 服务行含格子/剧场口径，
  ``build_prompt_layers`` 动态段可见（turn_pipeline 同一通道）；
  非 dnd5e 房间 / 无房间上下文零注入；
- 判定降级：veil-map 缺席 / 服务抛错 / 目录读失败 → 纯文本形态不报错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from trpg_server.app_factory import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PACKAGE_DIR = REPO_ROOT / "data" / "plugins" / "dnd5e"


class FakeManager:
    """按用户返回角色：房主（user 1）是站点 ADMIN，其余成员是普通 USER。

    会话中间件会用 user manager 同步 session 角色，固定单角色的桩会把
    「玩家视角」也同步成 ADMIN，破坏 _rank 的房主判定。
    """

    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        resolved = "ADMIN" if str(user_id) == "1" else "USER"
        return {"id": user_id, "username": "tester", "role": resolved}


def _app(tmp_path, role="ADMIN"):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(role),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
        }
    )


def _login(client, role="ADMIN", user_id=1):
    with client.session_transaction() as session:
        session.update(
            user_id=user_id, username="tester", role=role, session_token="token", csrf_token="csrf"
        )


def _write_room(tmp_path, room_id="r-1", **extra):
    room_dir = tmp_path / "rooms" / room_id
    room_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "id": room_id,
        "name": "房规档案测试房",
        "creator_id": 1,
        "ruleset_id": "dnd5e",
        "members": [
            {
                "user_id": 1,
                "username": "tester",
                "role": "ADMIN",
                "status": "active",
                "is_active": True,
                "room_role": "owner",
            },
            {
                "user_id": 2,
                "username": "player",
                "role": "USER",
                "status": "active",
                "is_active": True,
                "room_role": "member",
            },
        ],
    }
    info.update(extra)
    (room_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return room_dir, info


def _read_room_info(tmp_path, room_id="r-1"):
    return json.loads(
        (tmp_path / "rooms" / room_id / "info.json").read_text(encoding="utf-8")
    )


def _stored_ruleset(tmp_path, room_id="r-1"):
    stored = _read_room_info(tmp_path, room_id).get("house_rules") or {}
    return (stored.get("by_ruleset") or {}).get("dnd5e") or {}


def _marker(tmp_path, room_id="r-1"):
    path = tmp_path / "rooms" / room_id / "plugins" / "dnd5e" / "house-rule-profile.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _profiles_module():
    """取 dnd5e 插件的房规档案模块（create_app 装载后为同一缓存实例）。"""
    if "dnd5e_plugin.house_rule_profiles" not in sys.modules:
        sys.path.insert(0, str(PLUGIN_PACKAGE_DIR))
    from dnd5e_plugin import house_rule_profiles

    return house_rule_profiles


class _FakeMapService:
    def __init__(self, scenes):
        self.scenes = scenes

    def list_scenes(self, room_dir):
        active = next((item.get("scene_id") for item in self.scenes if item.get("active")), "")
        return {"ok": True, "scenes": self.scenes, "active_scene_id": active}


def _active_scene_service():
    return _FakeMapService(
        [
            {
                "scene_id": "cave",
                "title": "银矿洞口",
                "background": "/assets/scenes/cave.jpg",
                "tokens": 6,
                "active": True,
            }
        ]
    )


def _patch_map(monkeypatch, service):
    module = _profiles_module()
    monkeypatch.setattr(module, "_map_service", lambda: service)


def _get_recommendation(client, room_id="r-1"):
    response = client.get(f"/api/plugins/dnd5e/rooms/{room_id}/rules/recommendation")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


# ---------------------------------------------------------------- 档案声明


def test_profiles_declared_against_schema(tmp_path):
    app = _app(tmp_path)
    profiles = _profiles_module()
    from dnd5e_plugin.house_rules import house_rules

    schema = house_rules()
    assert set(profiles.profile_ids()) == {"theater-of-mind", "grid-combat"}
    defaults = {key: spec["default"] for key, spec in schema.items()}

    # theater-of-mind = 现状兼容档：与 schema 全默认逐键相等。
    tom = profiles.profile_values("theater-of-mind")
    assert tom == defaults

    # grid-combat：键全在 schema 内、值全在枚举/布尔内；差异仅位置/对角键。
    grid = profiles.profile_values("grid-combat")
    assert set(grid) == set(schema)
    for key, value in grid.items():
        spec = schema[key]
        if spec["type"] == "boolean":
            assert isinstance(value, bool)
        else:
            assert value in spec["values"], key
    assert grid["dnd5e_positioning"] == "grid"
    assert grid["dnd5e_diagonal_rule"] == "5-5-5"
    assert grid["dnd5e_flanking"] is False
    diff = {key for key in grid if grid[key] != defaults[key]}
    # 对角线档 5-5-5 与 schema 默认同值（自文档化声明）；唯一差异是位置口径。
    assert diff == {"dnd5e_positioning"}

    # 预留键在 schema 里明确标注「尚未生效」，不给假行为背书。
    assert "尚未生效" in schema["dnd5e_diagonal_rule"]["description_i18n"]["zh-CN"]
    assert "尚未生效" in schema["dnd5e_milestone_leveling"]["description_i18n"]["zh-CN"]

    # 档案目录可展示：名称/描述/完整取值齐备。
    catalog = profiles.profile_catalog()
    assert set(catalog) == {"theater-of-mind", "grid-combat"}
    for entry in catalog.values():
        assert entry["label_i18n"]["zh-CN"] and entry["description_i18n"]["zh-CN"]
        assert set(entry["values"]) == set(schema)

    # 新键经核心房规通用端点可读可改（核心零改动验证）。
    _write_room(tmp_path)
    client = app.test_client()
    _login(client)
    rules = client.get("/api/rooms/r-1/house-rules", headers={"X-CSRF-Token": "csrf"})
    assert rules.status_code == 200
    definitions = rules.get_json()["data"]["definitions"]
    assert definitions["dnd5e_positioning"]["values"] == ["theater_of_mind", "grid"]
    assert definitions["dnd5e_diagonal_rule"]["values"] == ["5-5-5", "5-10-5"]
    assert definitions["dnd5e_milestone_leveling"]["type"] == "boolean"

    updated = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dnd5e_positioning": "grid", "dnd5e_milestone_leveling": True}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert updated.status_code == 200, updated.get_json()
    scoped = _stored_ruleset(tmp_path)
    assert scoped["dnd5e_positioning"] == "grid"
    assert scoped["dnd5e_milestone_leveling"] is True

    bogus = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dnd5e_positioning": "hex"}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert bogus.status_code == 400


# ---------------------------------------------------------------- 推荐链


def test_mapped_room_recommends_grid_and_auto_applies_once(tmp_path, monkeypatch):
    room_dir, _info = _write_room(tmp_path)
    _patch_map(monkeypatch, _active_scene_service())
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = _get_recommendation(client)
    assert data["scenario_form"] == "mapped"
    assert data["recommended_profile"] == "grid-combat"
    assert data["recommendation_active"] is True
    assert data["auto_applied"] is True
    assert data["current_profile"] == "grid-combat"
    assert data["current_source"] == "auto"
    assert "银矿洞口" in "".join(data["reasons"])

    # 落盘：dnd5e 命名空间 = grid 档案值；夹击保持关（DMG 可选默认关）。
    scoped = _stored_ruleset(tmp_path)
    assert scoped["dnd5e_positioning"] == "grid"
    assert scoped["dnd5e_diagonal_rule"] == "5-5-5"
    assert scoped["dnd5e_flanking"] is False
    marker = _marker(tmp_path)
    assert marker["source"] == "auto"
    assert marker["profile"] == "grid-combat"
    assert marker["snapshot"]["dnd5e_positioning"] == "grid"

    # 幂等：窗口内再次调用不重复写房规，也不再报 auto_applied。
    before = (room_dir / "info.json").read_text(encoding="utf-8")
    again = _get_recommendation(client)
    assert again["auto_applied"] is False
    assert again["current_source"] == "auto"
    after = (room_dir / "info.json").read_text(encoding="utf-8")
    assert before == after


def test_text_room_recommends_theater_of_mind_without_veil_map(tmp_path, monkeypatch):
    room_dir, _info = _write_room(tmp_path)
    _patch_map(monkeypatch, None)  # veil-map 缺席
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    data = _get_recommendation(client)
    assert data["scenario_form"] == "text"
    assert data["recommended_profile"] == "theater-of-mind"
    assert data["auto_applied"] is True
    assert data["current_profile"] == "theater-of-mind"
    assert data["current_source"] == "auto"

    # tom 档 = schema 默认：应用后 dnd5e 值仍等于默认，但记忆已落盘。
    scoped = _stored_ruleset(tmp_path)
    assert scoped["dnd5e_positioning"] == "theater_of_mind"
    marker = _marker(tmp_path)
    assert marker["source"] == "auto"
    assert marker["profile"] == "theater-of-mind"

    # 普通成员只读：看到推荐但不触发任何房间写入。
    _login(client, role="USER", user_id=2)
    before = (room_dir / "info.json").read_text(encoding="utf-8")
    player_view = _get_recommendation(client)
    assert player_view["can_apply"] is False
    assert player_view["auto_applied"] is False
    assert (room_dir / "info.json").read_text(encoding="utf-8") == before


def test_scenario_form_upgrade_within_auto_window(tmp_path, monkeypatch):
    """纯文本房先自动 tom，后挂地图 → 窗口内自动升档 grid（无人工设置时）。"""
    _write_room(tmp_path)
    _patch_map(monkeypatch, None)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    first = _get_recommendation(client)
    assert first["recommended_profile"] == "theater-of-mind"
    assert first["auto_applied"] is True

    # 剧本后来带上了地图（如世界包导入挂出活动场景）。
    _patch_map(monkeypatch, _active_scene_service())
    second = _get_recommendation(client)
    assert second["recommended_profile"] == "grid-combat"
    assert second["auto_applied"] is True
    assert second["current_profile"] == "grid-combat"
    assert _stored_ruleset(tmp_path)["dnd5e_positioning"] == "grid"
    assert _marker(tmp_path)["profile"] == "grid-combat"


# ---------------------------------------------------------------- 三态：手动 / 站点开关


def test_manual_house_rules_stop_auto_recommendation(tmp_path, monkeypatch):
    room_dir, _info = _write_room(tmp_path)
    _patch_map(monkeypatch, _active_scene_service())
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    # 路径 a：推荐前房主就手动设置过 → 首次调用即 manual，绝不自动应用。
    manual = client.put(
        "/api/rooms/r-1/house-rules",
        json={"house_rules": {"dnd5e_critical_rule": "max_dice"}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert manual.status_code == 200, manual.get_json()
    before = (room_dir / "info.json").read_text(encoding="utf-8")

    data = _get_recommendation(client)
    assert data["status"] == "manual"
    assert data["current_source"] == "manual"
    assert data["recommendation_active"] is False
    assert data["auto_applied"] is False
    assert _marker(tmp_path) == {}
    assert (room_dir / "info.json").read_text(encoding="utf-8") == before
    assert _stored_ruleset(tmp_path)["dnd5e_critical_rule"] == "max_dice"

    # 路径 b：自动应用 grid 后房主手动改任一键 → 翻 manual，房规保持手动值。
    _write_room(tmp_path, room_id="r-2")
    first = _get_recommendation(client, "r-2")
    assert first["auto_applied"] is True
    assert first["current_profile"] == "grid-combat"

    changed = client.put(
        "/api/rooms/r-2/house-rules",
        json={"house_rules": {"dnd5e_flanking": True}},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert changed.status_code == 200, changed.get_json()
    second = _get_recommendation(client, "r-2")
    assert second["status"] == "manual"
    assert second["recommendation_active"] is False
    assert second["auto_applied"] is False
    # 手动值不被回写覆盖：位置口径仍是自动应用的 grid，夹击是房主的选择。
    scoped = _stored_ruleset(tmp_path, "r-2")
    assert scoped["dnd5e_positioning"] == "grid"
    assert scoped["dnd5e_flanking"] is True
    assert _marker(tmp_path, "r-2")["source"] == "auto"  # 历史记忆保留供核对


def test_site_config_disables_recommendation_entirely(tmp_path, monkeypatch):
    room_dir, _info = _write_room(tmp_path)
    _patch_map(monkeypatch, _active_scene_service())
    app = _app(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "dnd5e-house-rules.json").write_text(
        json.dumps({"auto_profile_recommendation": False}), encoding="utf-8"
    )
    client = app.test_client()
    _login(client)

    before = (room_dir / "info.json").read_text(encoding="utf-8")
    data = _get_recommendation(client)
    assert data["site_enabled"] is False
    assert data["status"] == "disabled"
    assert data["recommended_profile"] == ""
    assert data["recommendation_active"] is False
    assert data["auto_applied"] is False
    assert (room_dir / "info.json").read_text(encoding="utf-8") == before
    assert _marker(tmp_path) == {}


def test_endpoint_guards_non_member_and_foreign_ruleset(tmp_path, monkeypatch):
    _write_room(tmp_path)
    _patch_map(monkeypatch, None)
    app = _app(tmp_path)
    client = app.test_client()

    # 非成员（甚至未登录）拿不到推荐。
    _login(client, role="USER", user_id=99)
    denied = client.get("/api/plugins/dnd5e/rooms/r-1/rules/recommendation")
    assert denied.status_code == 403

    # 非 dnd5e 房间：规则集不匹配，可读拒绝。
    _write_room(tmp_path, room_id="r-coc", ruleset_id="coc7")
    _login(client)
    mismatch = client.get("/api/plugins/dnd5e/rooms/r-coc/rules/recommendation")
    assert mismatch.status_code == 400
    assert mismatch.get_json()["error"] == "dnd5e.ruleset_mismatch"


# ---------------------------------------------------------------- KP 提示词联动


def test_kp_context_fragment_carries_distance_conventions(tmp_path, monkeypatch):
    from trpg_server.agents.prompt_builder import build_prompt_layers
    from trpg_server.plugins import runtime as plugin_runtime

    room_dir, info = _write_room(tmp_path)
    _patch_map(monkeypatch, None)
    app = _app(tmp_path)
    module = _profiles_module()
    with app.app_context():
        service = plugin_runtime.get_service("dnd5e", "kp_context")
        assert service is not None, "dnd5e 插件应已在 agent_tools 激活期注册 kp_context 服务"

        # 方格房：一行说清格距口径（引擎现状 5-5-5），AI DM 跟档案叙述。
        grid_info = dict(info, house_rules={"dnd5e_positioning": "grid"})
        grid_frags = service.kp_prompt_fragments(
            SimpleNamespace(room_dir=room_dir, room_info=lambda: grid_info, scenarios_dir=None)
        )
        assert len(grid_frags) == 1
        grid_line = grid_frags[0]
        assert grid_line.startswith("【房规口径】")
        assert "方格战斗（grid-combat）" in grid_line
        assert "1 格 = 5 尺" in grid_line
        assert "5-5-5" in grid_line
        assert "夹击（flanking）：关闭" in grid_line

        # 剧场房：档位口径，不引导按格结算。
        tom_frags = service.kp_prompt_fragments(
            SimpleNamespace(
                room_dir=room_dir, room_info=lambda: dict(info, house_rules={}), scenarios_dir=None
            )
        )
        tom_line = tom_frags[0]
        assert "剧场式（theater-of-mind）" in tom_line
        assert "近战（约 5 尺内）" in tom_line
        assert "不要以格数或精确英尺结算" in tom_line

        # 自定义混合档：按实际键值如实描述。
        custom_info = dict(info, house_rules={"dnd5e_positioning": "grid", "dnd5e_flanking": True})
        custom_line = service.kp_prompt_fragments(
            SimpleNamespace(room_dir=room_dir, room_info=lambda: custom_info, scenarios_dir=None)
        )[0]
        assert "房主自定义混合档" in custom_line
        assert "夹击（flanking）：开启" in custom_line

        # 与 turn_pipeline 同一通道：片段进 build_prompt_layers 的规则集动态段。
        layers = build_prompt_layers(
            "全局规则", None, None, None, [], "玩家输入", ruleset_fragments=grid_frags
        )
        ruleset_message = next(
            message for message in layers.messages if "规则集判定口径" in message["content"]
        )
        assert "【房规口径】" in ruleset_message["content"]

        # 非 dnd5e 房间 / 无房间上下文：零注入不抛错。
        coc_frags = service.kp_prompt_fragments(
            SimpleNamespace(
                room_dir=room_dir, room_info=lambda: {"ruleset_id": "coc7"}, scenarios_dir=None
            )
        )
        assert coc_frags == []
        assert (
            service.kp_prompt_fragments(SimpleNamespace(room_dir=None, room_info=lambda: {})) == []
        )
        # 预留未生效键不进提示词：即便存了 5-10-5，也不声称引擎已按其结算。
        reserved_info = dict(
            info, house_rules={"dnd5e_positioning": "grid", "dnd5e_diagonal_rule": "5-10-5"}
        )
        reserved_line = module.profile_prompt_line(module.current_values(reserved_info))
        assert "5-5-5" in reserved_line
        assert "5-10-5" not in reserved_line


# ---------------------------------------------------------------- 判定链降级


def test_detection_degrades_gracefully_without_map_service(tmp_path, monkeypatch):
    app = _app(tmp_path)
    module = _profiles_module()
    room_dir = tmp_path / "rooms" / "any"

    # veil-map 缺席：纯文本形态，不报错。
    monkeypatch.setattr(module, "_map_service", lambda: None)
    form = module.detect_scenario_form(room_dir)
    assert form["form"] == "text"
    assert any("veil-map" in reason for reason in form["reasons"])

    # 服务抛错：降级纯文本。
    class _Broken:
        def list_scenes(self, room_dir):
            raise RuntimeError("boom")

    monkeypatch.setattr(module, "_map_service", lambda: _Broken())
    broken = module.detect_scenario_form(room_dir)
    assert broken["form"] == "text"
    assert broken["reasons"] == ["地图场景目录读取失败"]

    # 目录读失败（服务自查 ok=False）：空场景清单如实降级。
    class _NoScenes:
        def list_scenes(self, room_dir):
            return {"ok": False, "code": "map.scene_unavailable", "scenes": []}

    monkeypatch.setattr(module, "_map_service", lambda: _NoScenes())
    empty = module.detect_scenario_form(room_dir)
    assert empty["form"] == "text"
    assert empty["reasons"] == ["房间未挂地图"]

    # 有配底图但未激活（如导入时 activate=false）：仍视为带地图。
    monkeypatch.setattr(
        module,
        "_map_service",
        lambda: _FakeMapService(
            [{"scene_id": "s", "title": "图", "background": "/a.jpg", "tokens": 0, "active": False}]
        ),
    )
    inactive = module.detect_scenario_form(room_dir)
    assert inactive["form"] == "mapped"
    assert any("未激活" in reason for reason in inactive["reasons"])

    # 只有未配底图的空场景：按纯文本推荐。
    monkeypatch.setattr(
        module,
        "_map_service",
        lambda: _FakeMapService([{"scene_id": "s", "title": "空", "background": "", "tokens": 0, "active": False}]),
    )
    blank = module.detect_scenario_form(room_dir)
    assert blank["form"] == "text"
    assert blank["reasons"] == ["仅有未配底图的空场景"]

    # 站点开关：文件缺失视为开启，显式 false 才关闭。
    config_dir = tmp_path / "fresh-config"
    assert module.site_recommendation_enabled(config_dir) is True
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "dnd5e-house-rules.json").write_text(
        json.dumps({"auto_profile_recommendation": False}), encoding="utf-8"
    )
    assert module.site_recommendation_enabled(config_dir) is False
