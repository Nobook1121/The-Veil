"""插件扩展体系测试（``docs/plugin-development.md``）。

覆盖：清单校验、版本区间、默认不加载、启用/禁用/卸载、依赖与冲突、
失败隔离、资源上限、符号链接、模板接入与示例插件端到端。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from trpg_server.agents.tools import default_tool_registry
from trpg_server.app_factory import create_app
from trpg_server.config import paths as config_paths
from trpg_server.plugins import manifest as plugin_manifest
from trpg_server.plugins import runtime as plugin_runtime
from trpg_server.plugins import security as plugin_security
from trpg_server.plugins.contracts import (
    REASON_API_VERSION,
    REASON_APP_VERSION,
    REASON_CONFLICT,
    REASON_DEPENDENCY_CYCLE,
    REASON_DEPENDENCY_MISSING,
    REASON_DEPENDENCY_VERSION,
    REASON_MANIFEST_INVALID,
    REASON_RESOURCE_LIMIT,
    REASON_UNSAFE_PATH,
    STATE_DISABLED,
    STATE_FAILED,
    STATE_LOADED,
)
from trpg_server.plugins.registry import PluginRegistry, reset_plugin_registry
from trpg_server.props import registry as props_registry
from trpg_server.props.render import render_prop

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PLUGIN = REPO_ROOT / "examples" / "plugins" / "hello-prop"

_TEMPLATE_MANIFEST = {
    "id": "plugin-note",
    "name": "插件便条",
    "fields": [{"key": "body", "label": "正文", "type": "textarea", "max": 200, "required": True}],
}


# ---------------------------------------------------------------- 工具


def _enable(root: Path, plugin_ids: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "enabled.json").write_text(
        json.dumps({"version": 1, "enabled": plugin_ids}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_template(directory: Path, template_id: str = "plugin-note", page_html: str | None = None) -> None:
    root = directory / template_id
    (root / "assets").mkdir(parents=True)
    payload = {**_TEMPLATE_MANIFEST, "id": template_id}
    (root / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (root / "page.html").write_text(page_html or "<div><p>{{body}}</p></div>", encoding="utf-8")
    (root / "style.css").write_text("p { font-size: 12pt; }", encoding="utf-8")


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    manifest: dict | None = None,
    entry: str | None = None,
    with_template: bool = False,
    template_page: str | None = None,
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
    if with_template:
        payload["capabilities"]["prop_templates"] = {"dirs": ["templates"]}
        _write_template(directory / "templates", page_html=template_page)
    if entry is not None:
        payload["code"] = {"entry": "entry.py"}
        directory.joinpath("entry.py").write_text(entry, encoding="utf-8")
    if manifest:
        payload.update(manifest)
    (directory / "plugin.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return directory


def _load(root: Path) -> PluginRegistry:
    registry = PluginRegistry(root)
    registry.load()
    return registry


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    props_registry.reset_template_sources()
    props_registry.rescan()
    plugin_runtime.clear_plugin_runtime()
    reset_plugin_registry()


# ---------------------------------------------------------------- 清单规范


def test_manifest_schema_file_is_in_sync():
    """文档里的 JSON Schema 必须与实现一致，避免第三方开发者拿到过期规范。"""
    path = REPO_ROOT / "docs" / "schemas" / "plugin-manifest.schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == plugin_manifest.manifest_json_schema()


def test_capability_module_is_optional_and_defaults_to_code_entry():
    """cordis-paradigm 阶段 5：能力段 module 可选，未填时默认复用 code.entry。"""
    data = {
        "schema_version": 1,
        "id": "demo",
        "name": "demo",
        "version": "1.0.0",
        "api_version": 1,
        "code": {"entry": "entry.py"},
        "capabilities": {
            "rulesets": {"ids": ["my-rules"]},
            "agent_tools": {"tools": ["demo.ping"]},
            "http_blueprints": {"blueprints": ["bp"]},
        },
    }
    manifest = plugin_manifest.parse_manifest(data, Path("demo"))
    for name in ("rulesets", "agent_tools", "http_blueprints"):
        assert manifest.capabilities[name]["module"] == "entry.py"

    # 显式填写的 module 保持作者原值，不被 code.entry 覆盖。
    data["capabilities"]["agent_tools"] = {"module": "custom.py", "tools": ["demo.ping"]}
    manifest = plugin_manifest.parse_manifest(data, Path("demo"))
    assert manifest.capabilities["agent_tools"]["module"] == "custom.py"
    assert manifest.capabilities["rulesets"]["module"] == "entry.py"


def test_capability_module_without_code_entry_still_rejected(tmp_path):
    """module 降级为可选不等于 code.entry 降级：三个代码能力段仍必须给 code.entry。"""
    base = {
        "schema_version": 1,
        "id": "demo",
        "name": "demo",
        "version": "1.0.0",
        "api_version": 1,
    }
    # 旧形态（module 填了但没 code.entry）与新增形态（module 也没填）同样拒绝。
    with_module = {**base, "capabilities": {"agent_tools": {"module": "entry.py", "tools": ["demo.ping"]}}}
    without_module = {**base, "capabilities": {"agent_tools": {"tools": ["demo.ping"]}}}
    for data in (with_module, without_module):
        with pytest.raises(plugin_manifest.ManifestError) as excinfo:
            plugin_manifest.parse_manifest(data, tmp_path / "demo")
        assert "code.entry" in str(excinfo.value)


def test_manifest_schema_makes_capability_module_optional():
    """schema 视图同步：三个能力段的 required 不再包含 module。"""
    caps = plugin_manifest.manifest_json_schema()["properties"]["capabilities"]["properties"]
    assert "required" not in caps["rulesets"]
    assert caps["agent_tools"]["required"] == ["tools"]
    assert caps["http_blueprints"]["required"] == ["blueprints"]


def test_version_range_satisfies():
    assert plugin_manifest.satisfies_version("1.0.4", "")
    assert plugin_manifest.satisfies_version("1.0.4", ">=1.0.0 <2.0.0")
    assert not plugin_manifest.satisfies_version("2.0.0", ">=1.0.0 <2.0.0")
    assert not plugin_manifest.satisfies_version("1.0.4", ">=2.0.0")
    assert not plugin_manifest.satisfies_version("1.0.4", "not-a-range")
    assert plugin_manifest.satisfies_version("1.2.3", "==1.2.3,!=9.9.9")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda data: data.pop("id"), "id"),
        (lambda data: data.update(id="Bad_ID"), "id"),
        (lambda data: data.update(version="1.0"), "version"),
        (lambda data: data.update(schema_version=99), "schema_version"),
        (lambda data: data.update(capabilities={"nope": {}}), "未知能力"),
        (lambda data: data.update(capabilities={"prop_templates": {"dirs": ["../x"]}}), "不允许包含"),
        (
            lambda data: data.update(
                capabilities={"agent_tools": {"module": "entry.py", "tools": ["other.tool"]}}
            ),
            "命名空间",
        ),
        # 多级命名空间：mangle 后 _resolve_tool 只还原首个 _，必然解析错（阶段 5 强校验）。
        (
            lambda data: data.update(
                capabilities={"agent_tools": {"module": "entry.py", "tools": ["demo.sub.tool"]}}
            ),
            "命名空间",
        ),
        # 空段：demo. 点后为空。
        (
            lambda data: data.update(
                capabilities={"agent_tools": {"module": "entry.py", "tools": ["demo."]}}
            ),
            "命名空间",
        ),
    ],
)
def test_manifest_validation_rejects_bad_input(mutate, message):
    data = {
        "schema_version": 1,
        "id": "demo",
        "name": "demo",
        "version": "1.0.0",
        "api_version": 1,
        "capabilities": {},
    }
    mutate(data)
    with pytest.raises(plugin_manifest.ManifestError) as excinfo:
        plugin_manifest.parse_manifest(data, Path("demo"))
    assert message in str(excinfo.value)


# ---------------------------------------------------------------- 生命周期


def test_plugins_are_not_loaded_unless_enabled(tmp_path):
    sentinel = tmp_path / "imported.marker"
    _write_plugin(
        tmp_path / "plugins",
        "quiet",
        entry=f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('x')\n",
    )
    registry = _load(tmp_path / "plugins")

    record = registry.get("quiet")
    assert record is not None
    assert record.state == STATE_DISABLED
    assert record.to_dict()["reason_code"] == "not_enabled"
    assert not sentinel.exists()


def test_enable_loads_plugin_and_template(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "note-pack", with_template=True)
    _enable(root, ["note-pack"])

    registry = _load(root)
    record = registry.get("note-pack")
    assert record is not None and record.state == STATE_LOADED
    assert "prop_templates" in record.loaded_capabilities

    manifests = {item["id"]: item for item in props_registry.list_templates()}
    assert manifests["plugin-note"]["source"] == "plugin:note-pack"

    output = tmp_path / "note.pdf"
    result = render_prop(
        {
            "schema_version": 1,
            "id": "prop-1",
            "room_id": "",
            "template_id": "plugin-note",
            "title": "插件便条",
            "fields": {"body": "插件模板渲染成功。"},
        },
        output,
    )
    assert result["pages"] >= 1
    assert output.stat().st_size > 0


def test_summary_reports_source_from_discovery_location(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "note-pack", with_template=True)
    _enable(root, ["note-pack"])

    plugin = _load(root).summary()["plugins"][0]
    assert plugin["source"] == "plugin_dir"
    # has_code 保留，但前端不再用它推断来源。
    assert plugin["manifest"]["has_code"] is False


def test_disable_and_uninstall_lifecycle(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "note-pack", with_template=True)
    _enable(root, ["note-pack"])
    registry = _load(root)
    assert "plugin-note" in props_registry.available_ids()

    result = registry.disable("note-pack")
    assert result["restart_required"] is False
    assert registry.get("note-pack").state == STATE_DISABLED
    assert "plugin-note" not in props_registry.available_ids()

    with pytest.raises(Exception):
        registry.uninstall("note-pack", confirm="wrong-id")
    registry.uninstall("note-pack", confirm="note-pack")
    assert not (root / "note-pack").exists()
    assert registry.get("note-pack") is None


def test_corrupt_enabled_state_degrades_to_empty(tmp_path):
    """enabled.json 损坏时按空列表降级，不能把全部插件拖成不可用。"""
    root = tmp_path / "plugins"
    _write_plugin(root, "note-pack", with_template=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "enabled.json").write_text("{not valid json", encoding="utf-8")

    registry = _load(root)  # 不抛异常
    record = registry.get("note-pack")
    assert record is not None
    assert record.state != STATE_LOADED
    assert registry.loaded_ids() == []
    assert "plugin-note" not in props_registry.available_ids()


def test_reserved_plugin_id_is_rejected(tmp_path):
    """插件 id 不能与核心 Agent 工具命名空间同名，否则会顶掉核心工具。"""
    payload = {
        "schema_version": 1,
        "id": "props",
        "name": "伪造道具工具",
        "version": "1.0.0",
        "api_version": 1,
        "description": "shadow core tools",
        "author": "tester",
    }
    with pytest.raises(plugin_manifest.ManifestError):
        plugin_manifest.parse_manifest(payload, tmp_path / "props")


def test_disabled_code_plugin_prunes_module_without_restart_flag(tmp_path):
    """禁用代码插件后模块要从 sys.modules 移除；无蓝图则不再要求重启。

    生命周期 v2（cordis-paradigm 阶段 2）：agent_tools 等内存注册类能力运行期
    精确撤销，``restart_required`` 收窄为仅含 ``http_blueprints`` 的插件为 true。
    """
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "toolbox",
        entry=(
            "from trpg_server.agents.tools.base import AgentTool\n"
            "def handler(args, ctx):\n"
            "    return {'ok': True}\n"
            "def create_tools():\n"
            "    return [AgentTool(name='toolbox.ping', description='demo', parameters={}, handler=handler)]\n"
        ),
        manifest={
            "capabilities": {
                "agent_tools": {"module": "entry.py", "tools": ["toolbox.ping"]}
            }
        },
    )
    _enable(root, ["toolbox"])
    registry = _load(root)
    module_name = "trpg_plugin_toolbox_entry"
    assert module_name in sys.modules
    assert default_tool_registry().get("toolbox.ping") is not None

    result = registry.disable("toolbox")
    # 无 http_blueprints：不需要重启，能力已即时撤销。
    assert result["restart_required"] is False
    assert result["plugin"]["pending_restart"] is False
    assert module_name not in sys.modules
    assert registry.get("toolbox").pending_restart is False
    assert registry.get("toolbox").state == STATE_DISABLED
    assert default_tool_registry().get("toolbox.ping") is None

    # 重新加载（例如刷新设置页触发的 rescan）后语义一致：不背重启标记。
    registry.load()
    assert registry.get("toolbox").pending_restart is False


def test_disabled_all_skips_enabled_plugins(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "note-pack", with_template=True)
    _enable(root, ["note-pack"])

    registry = PluginRegistry(root, disabled_all=True)
    registry.load()
    record = registry.get("note-pack")
    assert record is not None
    assert record.state == STATE_DISABLED
    assert "plugin-note" not in props_registry.available_ids()


# ---------------------------------------------------------------- 依赖 / 冲突


def test_missing_dependency_marks_failed(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "needs-ghost", manifest={"dependencies": ["ghost"]})
    _enable(root, ["needs-ghost"])

    record = _load(root).get("needs-ghost")
    assert record.state == STATE_FAILED
    assert record.reason_code == REASON_DEPENDENCY_MISSING


def test_dependency_version_mismatch_marks_failed(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "base", with_template=False)
    _write_plugin(
        root,
        "client",
        manifest={"requires": {"plugins": {"base": ">=2.0.0"}}},
    )
    _enable(root, ["base", "client"])

    records = _load(root)
    assert records.get("base").state == STATE_LOADED
    assert records.get("client").reason_code == REASON_DEPENDENCY_VERSION


def test_dependency_cycle_marks_all_failed(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha", manifest={"dependencies": ["beta"]})
    _write_plugin(root, "beta", manifest={"dependencies": ["alpha"]})
    _enable(root, ["alpha", "beta"])

    records = _load(root)
    assert records.get("alpha").reason_code == REASON_DEPENDENCY_CYCLE
    assert records.get("beta").reason_code == REASON_DEPENDENCY_CYCLE


def test_conflict_marks_both_failed(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "first", manifest={"conflicts": ["second"]})
    _write_plugin(root, "second")
    _enable(root, ["first", "second"])

    records = _load(root)
    assert records.get("first").reason_code == REASON_CONFLICT
    assert records.get("second").reason_code == REASON_CONFLICT


def test_version_gates(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "future-api", manifest={"api_version": 99})
    _write_plugin(root, "future-app", manifest={"requires": {"app": ">=9.0.0"}})
    _enable(root, ["future-api", "future-app"])

    records = _load(root)
    assert records.get("future-api").reason_code == REASON_API_VERSION
    assert records.get("future-app").reason_code == REASON_APP_VERSION


# ---------------------------------------------------------------- 失败隔离 / 安全


def test_broken_code_is_isolated(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "boom", entry="raise RuntimeError('插件内部炸了')\n")
    _write_plugin(root, "good", with_template=True)
    _enable(root, ["boom", "good"])

    registry = _load(root)
    broken = registry.get("boom")
    assert broken.state == STATE_FAILED
    assert "插件内部炸了" in broken.reason
    assert registry.get("good").state == STATE_LOADED
    assert "plugin-note" in props_registry.available_ids()


def test_unsafe_template_is_isolated_to_template(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "evil-template",
        with_template=True,
        template_page="<div><script>alert(1)</script><p>{{body}}</p></div>",
    )
    _enable(root, ["evil-template"])

    registry = _load(root)
    assert registry.get("evil-template").state == STATE_LOADED
    assert "plugin-note" not in props_registry.available_ids()
    reasons = [item["reason"] for item in props_registry.list_unavailable()]
    assert any("<script>" in reason for reason in reasons)


def test_symlinked_plugin_dir_is_rejected(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir(parents=True)
    real = tmp_path / "outside"
    real.mkdir()
    (real / "plugin.json").write_text(
        json.dumps({"schema_version": 1, "id": "evil", "name": "evil", "version": "1.0.0"}),
        encoding="utf-8",
    )
    link = root / "evil"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    record = _load(root).get("evil")
    assert record is not None
    assert record.reason_code == REASON_UNSAFE_PATH


def test_safe_plugin_path_rejects_traversal(tmp_path):
    base = tmp_path / "plugins" / "demo"
    base.mkdir(parents=True)
    with pytest.raises(plugin_security.UnsafePluginPath):
        plugin_security.safe_plugin_path(base, "..", "other")
    with pytest.raises(plugin_security.UnsafePluginPath):
        plugin_security.safe_plugin_path(base, "C:/Windows/system32")


def test_resource_limit_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_security, "MAX_PLUGIN_FILES", 2)
    root = tmp_path / "plugins"
    directory = _write_plugin(root, "heavy", with_template=True)
    (directory / "extra.bin").write_bytes(b"x")
    _enable(root, ["heavy"])

    record = _load(root).get("heavy")
    assert record.state == STATE_FAILED
    assert record.reason_code == REASON_RESOURCE_LIMIT


# ---------------------------------------------------------------- 示例插件端到端


class _FakeManager:
    """会话校验替身：避免测试碰真实用户库。"""

    def __init__(self, role: str = "ADMIN") -> None:
        self.role = role

    def is_session_current(self, _user_id, _token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


def test_example_plugin_end_to_end(tmp_path):
    root = tmp_path / "plugins"
    import shutil

    shutil.copytree(EXAMPLE_PLUGIN, root / "hello-prop")
    _enable(root, ["hello-prop"])

    manager = _FakeManager()
    app = create_app(
        {
            "TESTING": True,
            "PLUGIN_DIR": str(root),
            "USER_MANAGER": manager,
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
            "ROOM_LOG_EXPORT_DIR": tmp_path / "exports",
        }
    )
    registry = app.extensions["plugin_registry"]
    assert registry.get("hello-prop").state == STATE_LOADED

    client = app.test_client()
    ping = client.get("/api/plugins/hello-prop/ping")
    assert ping.status_code == 200
    assert ping.get_json()["data"]["pong"] is True

    # 普通用户看不到插件清单；管理员可以。
    denied = client.get("/api/plugins")
    assert denied.status_code == 401
    with client.session_transaction() as session:
        session.update(user_id=1, username="u", role="USER", session_token="t", csrf_token="c")
    manager.role = "USER"
    assert client.get("/api/plugins").status_code == 403
    manager.role = "ADMIN"
    payload = client.get("/api/plugins").get_json()["data"]
    assert payload["plugins"][0]["id"] == "hello-prop"
    assert payload["counts"]["loaded"] == 1

    # 模板进入道具工坊列表，并且真的能渲染。
    assert "hello-note" in props_registry.available_ids()
    output = tmp_path / "hello.pdf"
    render_prop(
        {
            "schema_version": 1,
            "id": "prop-hello",
            "room_id": "",
            "template_id": "hello-note",
            "title": "来自插件的便条",
            "fields": {"title": "你好", "body": "这是一张插件模板。"},
        },
        output,
    )
    assert output.stat().st_size > 0


def test_agent_tool_hook_registers_plugin_tools(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "toolbox",
        entry=(
            "from trpg_server.agents.tools.base import AgentTool\n"
            "def handler(args, ctx):\n"
            "    return {'ok': True}\n"
            "def create_tools():\n"
            "    return [AgentTool(name='toolbox.ping', description='demo', parameters={}, handler=handler)]\n"
        ),
        manifest={
            "capabilities": {
                "agent_tools": {"module": "entry.py", "tools": ["toolbox.ping"]}
            }
        },
    )
    _enable(root, ["toolbox"])
    _load(root)
    assert default_tool_registry().get("toolbox.ping") is not None


def test_agent_tools_activate_without_module_field(tmp_path):
    """module 缺省的清单可正常激活：能力默认复用 code.entry，工具照常注册。"""
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "toolbox",
        entry=(
            "from trpg_server.agents.tools.base import AgentTool\n"
            "def handler(args, ctx):\n"
            "    return {'ok': True}\n"
            "def create_tools():\n"
            "    return [AgentTool(name='toolbox.ping', description='demo', parameters={}, handler=handler)]\n"
        ),
        manifest={"capabilities": {"agent_tools": {"tools": ["toolbox.ping"]}}},
    )
    _enable(root, ["toolbox"])
    registry = _load(root)
    record = registry.get("toolbox")
    assert record.state == STATE_LOADED
    assert "agent_tools" in record.loaded_capabilities
    assert record.manifest.capabilities["agent_tools"]["module"] == "entry.py"
    assert default_tool_registry().get("toolbox.ping") is not None


def test_multi_level_tool_name_is_rejected_before_activation(tmp_path):
    """多级工具命名必须在加载期（清单解析）拒绝，不允许带病激活。

    ``_resolve_tool`` 只把模型侧名称的首个 ``_`` 还原成 ``.``，多级命名 mangle
    后无法还原；清单解析是能拒绝的最早位置——插件代码一行都不该被执行。
    """
    root = tmp_path / "plugins"
    sentinel = tmp_path / "imported.marker"
    _write_plugin(
        root,
        "mangled",
        entry=(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('x')\n"
            "from trpg_server.agents.tools.base import AgentTool\n"
            "def handler(args, ctx):\n"
            "    return {'ok': True}\n"
            "def create_tools():\n"
            "    return [AgentTool(name='mangled.deep.tool', description='demo', parameters={}, handler=handler)]\n"
        ),
        manifest={
            "capabilities": {
                "agent_tools": {"module": "entry.py", "tools": ["mangled.deep.tool"]}
            }
        },
    )
    _enable(root, ["mangled"])
    registry = _load(root)
    record = registry.get("mangled")
    assert record.state == STATE_FAILED
    assert record.reason_code == REASON_MANIFEST_INVALID
    assert "命名空间" in record.reason
    # 清单在发现期即被判死：入口模块从未被 import，工具也从未注册。
    assert not sentinel.exists()
    assert default_tool_registry().get("mangled.deep.tool") is None


def test_cli_no_plugins_flag():
    args = config_paths.parse_cli_args(["server.py", "--no-plugins", "8080"])
    assert args.no_plugins is True
    assert args.port == 8080
    assert config_paths.parse_cli_args(["server.py"]).no_plugins is False
