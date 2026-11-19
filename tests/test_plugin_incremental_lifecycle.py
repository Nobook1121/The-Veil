"""插件增量启停（cordis-paradigm-adoption-plan 阶段 2）测试。

覆盖：

- **激活 = 注册即副作用**：宿主中介的 runtime 注册经 ``ctx.effect`` 包装，
  插件代码在工厂调用里直写核心全局槽位（行动 guard、位置/场景提供方、
  ``register_service``）由激活窗口对拍兜进同一条撤销链；
- **100 次启停往返对拍**：每轮后 runtime / props / areas / guards 快照与
  初始一致（零残留），工具可解析性随启停切换；
- **依赖连带回滚**：disable 依赖提供方 → 消费方运行期回滚并显式 WAITING
  （点名在等谁），enable 提供方 → 自动恢复；
- **HTTP 契约（阶段 3 接线后）**：``http_blueprints`` 经代理路由层挂摘，
  disable 即摘表 404、enable 即恢复；``restart_required`` 与
  ``pending_restart`` 字段保留但恒为 False。

测试纪律对齐 ``tests/test_plugins.py``：全部走临时目录 + autouse 清理，
不碰真实用户数据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from flask import Flask

from trpg_server.agents.tools import default_tool_registry
from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins.contracts import (
    REASON_DEPENDENCY_MISSING,
    STATE_DISABLED,
    STATE_FAILED,
    STATE_LOADED,
)
from trpg_server.plugins.registry import PluginRegistry, reset_plugin_registry
from trpg_server.plugins.route_proxy import bp as route_proxy_blueprint, route_proxy
from trpg_server.props import registry as props_registry
from trpg_server.workflows import areas, guards

_TEMPLATE_MANIFEST = {
    "id": "alpha-note",
    "name": "Alpha 便条",
    "fields": [{"key": "body", "label": "正文", "type": "textarea", "max": 200, "required": True}],
}

ALPHA_ENTRY = '''\
from trpg_server.agents.tools.base import AgentTool
from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.workflows import areas
from trpg_server.workflows.guards import register_action_guard


def _guard(report):
    return ()


def _position(room_dir, character_id):
    return None


def _scene(room_dir):
    return ""


def _handler(args, ctx):
    return {"ok": True}


def create_tools():
    # 插件代码在工厂调用里直写核心全局槽位：宿主靠激活窗口对拍兜进撤销链，
    # 插件作者不写 disposer 也获得可卸载性（对拍 → 差集 → 定点摘除）。
    register_action_guard(_guard)
    areas.register_position_provider(_position)
    areas.register_scene_id_provider(_scene)
    plugin_runtime.register_service("alpha-tools", "map", {"zone": "alpha"})
    return [
        AgentTool(name="alpha-tools.ping", description="alpha ping", parameters={}, handler=_handler)
    ]
'''

BETA_ENTRY = '''\
from trpg_server.agents.tools.base import AgentTool
from trpg_server.workflows.guards import register_action_guard


def _beta_guard(report):
    return ()


def _handler(args, ctx):
    return {"ok": True}


def create_tools():
    register_action_guard(_beta_guard)
    return [
        AgentTool(name="beta-addon.echo", description="beta echo", parameters={}, handler=_handler)
    ]
'''

GAMMA_ENTRY = '''\
from trpg_server.agents.tools.base import AgentTool


def _handler(args, ctx):
    return {"ok": True}


def create_tools():
    return [
        AgentTool(name="gamma-chain.tap", description="gamma tap", parameters={}, handler=_handler)
    ]
'''

ROUTE_ENTRY = '''\
from flask import Blueprint
from trpg_server.agents.tools.base import AgentTool

bp = Blueprint("route_addon_routes", __name__, url_prefix="/api/plugins/route-addon")


@bp.route("/ping")
def _ping():
    return {"pong": True}


def _handler(args, ctx):
    return {"ok": True}


def create_tools():
    return [
        AgentTool(name="route-addon.ping", description="route ping", parameters={}, handler=_handler)
    ]
'''


# ---------------------------------------------------------------- 工具


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    props_registry.reset_template_sources()
    props_registry.rescan()
    plugin_runtime.clear_plugin_runtime()
    guards.clear_action_guards()
    areas.clear_position_provider()
    areas.clear_scene_id_provider()
    for name in [item for item in sys.modules if item.startswith("trpg_plugin_")]:
        sys.modules.pop(name, None)
    reset_plugin_registry()


def _enable(root: Path, plugin_ids: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "enabled.json").write_text(
        json.dumps({"version": 1, "enabled": plugin_ids}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_template(directory: Path, template_id: str = "alpha-note") -> None:
    root = directory / template_id
    (root / "assets").mkdir(parents=True)
    payload = {**_TEMPLATE_MANIFEST, "id": template_id}
    (root / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (root / "page.html").write_text("<div><p>{{body}}</p></div>", encoding="utf-8")
    (root / "style.css").write_text("p { font-size: 12pt; }", encoding="utf-8")


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    manifest: dict | None = None,
    entry: str | None = None,
) -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "id": plugin_id,
        "name": plugin_id,
        "version": "1.0.0",
        "api_version": 1,
        "capabilities": {},
    }
    if entry is not None:
        payload["code"] = {"entry": "entry.py"}
        directory.joinpath("entry.py").write_text(entry, encoding="utf-8")
    if manifest:
        payload.update(manifest)
    (directory / "plugin.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return directory


def _write_alpha(root: Path) -> None:
    """P1：agent_tools + content_packs + prop_templates + 工厂内直写全局槽位。"""
    directory = _write_plugin(
        root,
        "alpha-tools",
        entry=ALPHA_ENTRY,
        manifest={
            "capabilities": {
                "prop_templates": {"dirs": ["templates"]},
                "content_packs": {"dirs": ["packs/alpha-pack"], "contract_version": 1},
                "agent_tools": {"module": "entry.py", "tools": ["alpha-tools.ping"]},
            }
        },
    )
    _write_template(directory / "templates")
    pack = directory / "packs" / "alpha-pack"
    pack.mkdir(parents=True)
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "pack_id": "alpha-pack",
                "pack_version": "1.0.0",
                "ruleset_id": "demo",
                "content_version": "1.0.0",
                "contract_version": 1,
                "source": {
                    "system": "demo",
                    "document": "demo document",
                    "license": "CC0-1.0",
                    "attribution": "incremental lifecycle test",
                },
                "files": [{"path": "spells.json", "type": "spells"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pack / "spells.json").write_text(
        json.dumps([{"id": "spark", "name": "火花"}], ensure_ascii=False),
        encoding="utf-8",
    )


def _write_beta(root: Path) -> None:
    """P2：依赖 P1 的 Agent 工具插件。"""
    _write_plugin(
        root,
        "beta-addon",
        entry=BETA_ENTRY,
        manifest={
            "dependencies": ["alpha-tools"],
            "capabilities": {
                "agent_tools": {"module": "entry.py", "tools": ["beta-addon.echo"]}
            },
        },
    )


def _write_gamma(root: Path) -> None:
    """P3：依赖 P2（传递依赖 P1），验证连带回滚的传递性。"""
    _write_plugin(
        root,
        "gamma-chain",
        entry=GAMMA_ENTRY,
        manifest={
            "dependencies": ["beta-addon"],
            "capabilities": {
                "agent_tools": {"module": "entry.py", "tools": ["gamma-chain.tap"]}
            },
        },
    )


def _write_route(root: Path) -> None:
    """含 http_blueprints 的插件：disable 后路由进程内不可逆。"""
    _write_plugin(
        root,
        "route-addon",
        entry=ROUTE_ENTRY,
        manifest={
            "capabilities": {
                "http_blueprints": {"module": "entry.py", "blueprints": ["bp"]},
                "agent_tools": {"module": "entry.py", "tools": ["route-addon.ping"]},
            }
        },
    )


def _snapshot(registry: PluginRegistry) -> dict:
    """对拍快照：核心全局槽位 + 插件条目生命周期视图，要求逐轮零残留。"""
    records: dict[str, tuple] = {}
    for entry in registry.summary()["plugins"]:
        records[entry["id"]] = (
            entry["state"],
            entry["enabled"],
            entry.get("fiber_state"),
            tuple(entry.get("fiber_waiting_for", ())),
            tuple(entry["capabilities"]),
        )
    return {
        "runtime": plugin_runtime.runtime_snapshot(),
        "services": plugin_runtime.services_snapshot(),
        "packs": plugin_runtime.content_pack_ids(),
        "guards": len(guards.registered_guards()),
        "position_provider": areas.current_position_provider() is not None,
        "scene_provider": areas.current_scene_id_provider() is not None,
        "props_sources": sorted(props_registry.registered_sources()),
        "props_available": sorted(props_registry.available_ids()),
        "records": records,
    }


def _summary_by_id(registry: PluginRegistry) -> dict[str, dict]:
    return {entry["id"]: entry for entry in registry.summary()["plugins"]}


# ---------------------------------------------------------------- 往返对拍


def test_incremental_roundtrip_100_cycles(tmp_path):
    """100 次启停往返：每轮零残留、工具可解析性随启停切换。"""
    root = tmp_path / "plugins"
    _write_alpha(root)
    _write_beta(root)
    _write_gamma(root)
    _enable(root, ["alpha-tools", "beta-addon", "gamma-chain"])

    registry = PluginRegistry(root)
    registry.load()
    baseline = _snapshot(registry)
    assert baseline["records"]["alpha-tools"][0] == STATE_LOADED
    assert baseline["records"]["alpha-tools"][2] == "active"
    assert default_tool_registry().get("alpha-tools.ping") is not None
    assert plugin_runtime.get_service("alpha-tools", "map") == {"zone": "alpha"}

    for index in range(100):
        disabled = registry.disable("alpha-tools")
        assert disabled["restart_required"] is False
        # 中间态：全链连带回滚（依赖方先撤），没有任何静默。
        assert default_tool_registry().get("alpha-tools.ping") is None
        assert default_tool_registry().get("beta-addon.echo") is None
        assert default_tool_registry().get("gamma-chain.tap") is None
        assert plugin_runtime.get_service("alpha-tools", "map") is None
        assert guards.registered_guards() == ()
        assert areas.current_position_provider() is None
        assert areas.current_scene_id_provider() is None
        assert plugin_runtime.content_pack_ids() == []
        assert "plugin:alpha-tools" not in props_registry.registered_sources()
        assert "alpha-note" not in props_registry.available_ids()
        # 目标模块从缓存与 sys.modules 摘除；等待中的依赖方保留模块缓存。
        assert "trpg_plugin_alpha_tools_entry" not in sys.modules
        by_id = _summary_by_id(registry)
        assert by_id["alpha-tools"]["state"] == STATE_DISABLED
        assert by_id["beta-addon"]["fiber_state"] == "waiting"
        assert by_id["beta-addon"]["fiber_waiting_for"] == ["alpha-tools"]
        assert by_id["gamma-chain"]["fiber_state"] == "waiting"
        assert by_id["gamma-chain"]["fiber_waiting_for"] == ["beta-addon"]

        enabled = registry.enable("alpha-tools")
        assert enabled["restart_required"] is False
        assert _snapshot(registry) == baseline, f"第 {index + 1} 轮往返后出现残留"
        assert default_tool_registry().get("alpha-tools.ping") is not None
        assert default_tool_registry().get("gamma-chain.tap") is not None
        assert plugin_runtime.get_service("alpha-tools", "map") == {"zone": "alpha"}

    # 全部往返完成后：三条效果链都在、模块缓存与 sys.modules 干净对齐。
    assert "trpg_plugin_alpha_tools_entry" in sys.modules
    assert len(guards.registered_guards()) == 2  # alpha 与 beta 各注册一个 guard

    # 已加载时重复 enable：幂等确认，不重导模块、不重复注册。
    again = registry.enable("alpha-tools")
    assert again["restart_required"] is False
    assert _snapshot(registry) == baseline


# ---------------------------------------------------------------- 依赖连带


def test_disable_provider_rolls_back_dependents_waiting_then_enable_restores(tmp_path):
    """disable P1 → P2 连带回滚且 summary 显式标 waiting；enable P1 → 恢复。"""
    root = tmp_path / "plugins"
    _write_alpha(root)
    _write_beta(root)
    _write_gamma(root)
    _enable(root, ["alpha-tools", "beta-addon", "gamma-chain"])
    registry = PluginRegistry(root)
    registry.load()

    registry.disable("alpha-tools")
    by_id = _summary_by_id(registry)
    # 提供方：普通禁用，不背失败码。
    assert by_id["alpha-tools"]["state"] == STATE_DISABLED
    assert by_id["alpha-tools"]["reason_code"] == "not_enabled"
    # 直接依赖方：运行期回滚 + 显式 WAITING（点名在等谁，禁止静默）。
    assert by_id["beta-addon"]["state"] == STATE_FAILED
    assert by_id["beta-addon"]["reason_code"] == REASON_DEPENDENCY_MISSING
    assert "运行期回滚" in by_id["beta-addon"]["reason"]
    assert by_id["beta-addon"]["fiber_state"] == "waiting"
    assert by_id["beta-addon"]["fiber_waiting_for"] == ["alpha-tools"]
    # 传递依赖方同样连带。
    assert by_id["gamma-chain"]["fiber_state"] == "waiting"
    assert by_id["gamma-chain"]["fiber_waiting_for"] == ["beta-addon"]

    # 依赖没回来时单独启用 P2：必须仍是显式等待，不能装作激活成功。
    registry.enable("beta-addon")
    still = _summary_by_id(registry)["beta-addon"]
    assert still["fiber_state"] == "waiting"
    assert still["fiber_waiting_for"] == ["alpha-tools"]
    assert registry.get("beta-addon").state == STATE_FAILED

    # 全量 rescan 的等待语义与增量路径一致。
    registry.load()
    reloaded = _summary_by_id(registry)
    assert reloaded["beta-addon"]["state"] == STATE_FAILED
    assert reloaded["beta-addon"]["fiber_state"] == "waiting"
    assert reloaded["beta-addon"]["fiber_waiting_for"] == ["alpha-tools"]

    # 依赖恢复 → 消费方自动重新激活。
    registry.enable("alpha-tools")
    restored = _summary_by_id(registry)
    assert restored["alpha-tools"]["state"] == STATE_LOADED
    assert restored["beta-addon"]["state"] == STATE_LOADED
    assert restored["beta-addon"]["fiber_state"] == "active"
    assert "fiber_waiting_for" not in restored["beta-addon"]
    assert restored["gamma-chain"]["fiber_state"] == "active"
    assert default_tool_registry().get("beta-addon.echo") is not None


def test_enable_missing_dependency_marks_waiting_without_activation(tmp_path):
    """启用一个依赖未就绪的插件：不激活、显式 WAITING，不产生半途注册。"""
    root = tmp_path / "plugins"
    _write_beta(root)
    _enable(root, ["beta-addon"])
    registry = PluginRegistry(root)
    registry.load()
    assert registry.get("beta-addon").state == STATE_FAILED

    registry.enable("beta-addon")
    target = _summary_by_id(registry)["beta-addon"]
    assert target["state"] == STATE_FAILED
    assert target["fiber_state"] == "waiting"
    assert target["fiber_waiting_for"] == ["alpha-tools"]
    # 没有任何半途注册泄漏到全局槽位。
    assert plugin_runtime.runtime_snapshot()["agent_tools"] == {}
    assert guards.registered_guards() == ()


# ---------------------------------------------------------------- HTTP 契约


def test_blueprint_plugin_disable_revokes_routes_immediately(tmp_path):
    """含 http_blueprints 的插件 disable：路由立即 404，restart_required 恒 False。"""
    root = tmp_path / "plugins"
    _write_route(root)
    _enable(root, ["route-addon"])

    app = Flask(__name__)
    registry = PluginRegistry(root)
    registry.load(app)
    # 模拟 app_factory 的注册顺序：核心蓝图（此处为空）之后才注册插件代理总蓝图。
    app.register_blueprint(route_proxy_blueprint)
    client = app.test_client()
    assert client.get("/api/plugins/route-addon/ping").status_code == 200
    assert default_tool_registry().get("route-addon.ping") is not None

    result = registry.disable("route-addon")
    # 阶段 3 契约：代理层摘表，蓝图路由即时消失，重启要求成为历史。
    assert result["restart_required"] is False
    assert result["plugin"]["pending_restart"] is False
    # 全部能力即时撤销：工具摘除、模块出缓存、路由 404。
    assert default_tool_registry().get("route-addon.ping") is None
    assert "trpg_plugin_route_addon_entry" not in sys.modules
    assert registry.get("route-addon").state == STATE_DISABLED
    assert registry.get("route-addon").pending_restart is False
    assert client.get("/api/plugins/route-addon/ping").status_code == 404

    # 重新启用：mount 即时生效，无需任何「补挂 Flask 蓝图」步骤。
    enabled = registry.enable("route-addon")
    assert enabled["restart_required"] is False
    assert default_tool_registry().get("route-addon.ping") is not None
    assert client.get("/api/plugins/route-addon/ping").status_code == 200
    # 新宿主实例只注册总蓝图一次即可接管同一张模块级路由表。
    fresh_app = Flask(__name__)
    fresh_app.register_blueprint(route_proxy_blueprint)
    assert fresh_app.test_client().get("/api/plugins/route-addon/ping").status_code == 200
    # 管理面示例：总蓝图是 catch-all，路径相同也不会吞掉宿主静态路由——
    # 这里用「未挂任何插件的路径」验证代理兜底 404 而不是 5xx。
    assert fresh_app.test_client().get("/api/plugins/never-mounted/ping").status_code == 404
