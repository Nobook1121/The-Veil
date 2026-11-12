"""插件注册表：发现 → 校验 → 依赖/冲突检查 → 激活 → 启用/禁用 → 卸载。

失败隔离是硬要求：任何一个插件在发现、校验、导入、注册能力的任何一步抛错，
都只会把**该插件**（以及依赖它的插件）标成 ``failed`` 并写日志，主程序照常启动。

插件根目录默认 ``data/plugins``：

```
data/plugins/
  enabled.json          # {"version":1,"enabled":["hello-prop"],"updated_at":"..."}
  hello-prop/
    plugin.json
    entry.py            # 可选；只在被启用后才会被 import
    templates/<id>/...
```

``enabled.json`` 是唯一的启用来源；没有它（或列表为空）时，任何第三方插件都
不会被激活，只在 ``/api/plugins`` 里显示为 ``disabled``。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping

from trpg_server.json_store import read_json, remove_tree, write_json_atomic
from trpg_server.props import registry as props_registry
from trpg_server.ruleset.contracts import RULESET_API_VERSION
from trpg_server.version import APP_VERSION

from . import runtime
from .contracts import (
    ENABLED_STATE_FILENAME,
    MAX_PLUGIN_FILE_BYTES,
    PLUGIN_API_VERSION,
    REASON_API_VERSION,
    REASON_APP_VERSION,
    REASON_CAPABILITY,
    REASON_CODE_LOAD,
    REASON_CONFLICT,
    REASON_DEPENDENCY_CYCLE,
    REASON_DEPENDENCY_FAILED,
    REASON_DEPENDENCY_MISSING,
    REASON_DEPENDENCY_VERSION,
    REASON_DISABLED_ALL,
    REASON_MANIFEST_INVALID,
    REASON_MISSING,
    REASON_NOT_ENABLED,
    REASON_UNSAFE_PATH,
    STATE_DISABLED,
    STATE_FAILED,
    STATE_FILE_VERSION,
    STATE_LOADED,
    ManifestError,
    PluginError,
    PluginManifest,
    PluginRecord,
)
from .manifest import load_manifest, satisfies_version
from .security import (
    assert_file_size,
    assert_plugin_resources,
    assert_plugin_root,
    default_plugin_root,
    enabled_state_path,
    safe_plugin_path,
)

logger = logging.getLogger(__name__)


class PluginNotFound(KeyError):
    """请求的插件不在仓库里。"""


class PluginStateError(ValueError):
    """启用/禁用/卸载的前置条件不满足（例如未禁用就卸载）。"""


class PluginStateStore:
    """``enabled.json`` 的读写；损坏时按空列表处理并告警，不阻断启动。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, default=None)
        if not isinstance(data, Mapping):
            return {"version": STATE_FILE_VERSION, "enabled": [], "updated_at": ""}
        enabled = data.get("enabled")
        ids = []
        if isinstance(enabled, list):
            for item in enabled:
                text = str(item or "").strip()
                if text and text not in ids:
                    ids.append(text)
        return {
            "version": STATE_FILE_VERSION,
            "enabled": ids,
            "updated_at": str(data.get("updated_at") or ""),
        }

    def enabled_ids(self) -> list[str]:
        return list(self.load()["enabled"])

    def set_enabled(self, ids: Iterable[str]) -> dict[str, Any]:
        unique = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        payload = {
            "version": STATE_FILE_VERSION,
            "enabled": unique,
            "updated_at": _now_text(),
        }
        write_json_atomic(self.path, payload)
        return payload


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dependency_ids(manifest: PluginManifest) -> list[str]:
    """``dependencies`` 与 ``requires.plugins`` 的并集（保持稳定顺序）。"""
    return list(dict.fromkeys([*manifest.dependencies, *manifest.requires_plugins.keys()]))


class PluginRegistry:
    """进程级插件注册表；构造后通过 :meth:`load` 完成一次完整生命周期。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        app_version: str | None = None,
        disabled_all: bool = False,
        state_store: PluginStateStore | None = None,
    ) -> None:
        self.root = Path(root or default_plugin_root())
        self._app_version = str(app_version or APP_VERSION)
        self._disabled_all = bool(disabled_all)
        self._store = state_store or PluginStateStore(enabled_state_path(self.root))
        self._records: dict[str, PluginRecord] = {}
        self._order: list[str] = []
        self._modules: dict[tuple[str, str], tuple[tuple[int, int], ModuleType]] = {}
        self._blueprints: list[tuple[str, Any]] = []
        self._app: Any = None
        self._lock = threading.RLock()
        self._loaded_at = ""

    # -- 只读视图 ---------------------------------------------------------
    @property
    def disabled_all(self) -> bool:
        return self._disabled_all

    def get(self, plugin_id: str) -> PluginRecord | None:
        return self._records.get(str(plugin_id))

    def records(self) -> list[PluginRecord]:
        return [self._records[key] for key in sorted(self._records)]

    def loaded_ids(self) -> list[str]:
        return [record.id for record in self.records() if record.loaded]

    def summary(self) -> dict[str, Any]:
        records = [record.to_dict() for record in self.records()]
        counts: dict[str, int] = {}
        for record in records:
            counts[record["state"]] = counts.get(record["state"], 0) + 1
        return {
            "root": str(self.root),
            "app_version": self._app_version,
            "api_version": PLUGIN_API_VERSION,
            "disabled_all": self._disabled_all,
            "loaded_at": self._loaded_at,
            "counts": counts,
            "plugins": records,
        }

    # -- 生命周期 ---------------------------------------------------------
    def load(self, app: Any = None) -> "PluginRegistry":
        """完整跑一遍生命周期；任何单插件异常都不会向外抛。"""
        with self._lock:
            if app is not None:
                self._app = app
            enabled_ids = [] if self._disabled_all else self._store.enabled_ids()
            self._records = {}
            self._order = []
            self._blueprints = []
            runtime.clear_plugin_runtime()
            props_registry.reset_template_sources()

            self._discover(enabled_ids)
            self._apply_enabled_state(enabled_ids)
            candidates = self._check_compatibility(enabled_ids)
            order = self._resolve_order(candidates)
            for plugin_id in order:
                self._activate(self._records[plugin_id])
            # 依赖了失败插件的插件一并标记失败（在 _resolve_order 之后才失败的情况）
            self._mark_failed_dependents()

            if self._records:
                props_registry.rescan()
            # 规则集注册表是懒构建的单例；插件启用/禁用后让它下次重建，
            # 避免启用新规则集却仍读到旧缓存。
            try:
                from trpg_server.ruleset.registry import reset_builtin_registry

                reset_builtin_registry()
            except Exception:  # noqa: BLE001 - 重置失败不影响插件状态
                logger.debug("ruleset_registry_reset_failed", exc_info=True)
            self._loaded_at = _now_text()
            logger.info(
                "plugins_loaded total=%s loaded=%s disabled=%s failed=%s disabled_all=%s",
                len(self._records),
                sum(1 for record in self._records.values() if record.loaded),
                sum(1 for record in self._records.values() if record.state == STATE_DISABLED),
                sum(1 for record in self._records.values() if record.state == STATE_FAILED),
                self._disabled_all,
            )
            return self

    def reload(self, app: Any = None) -> "PluginRegistry":
        return self.load(app)

    def attach_app(self, app: Any) -> None:
        """记录宿主 app；之后 :meth:`enable` 触发的重载才可能补注册蓝图。"""
        self._app = app

    def register_blueprints(self, app: Any) -> list[str]:
        """把已加载插件的蓝图注册进 Flask（同名蓝图只注册一次）。"""
        registered: list[str] = []
        for plugin_id, blueprint in self._blueprints:
            name = str(getattr(blueprint, "name", "") or "")
            if not name or name in app.blueprints:
                continue
            try:
                app.register_blueprint(blueprint)
                registered.append(name)
            except Exception:  # noqa: BLE001 - 单个插件蓝图失败不影响其它
                logger.warning(
                    "plugin_blueprint_failed plugin=%s blueprint=%s", plugin_id, name, exc_info=True
                )
        return registered

    def enable(self, plugin_id: str) -> dict[str, Any]:
        key = str(plugin_id or "").strip()
        record = self._records.get(key)
        if record is None:
            raise PluginNotFound(key)
        if record.manifest is None:
            raise PluginStateError(record.reason or "插件清单不可用，无法启用")
        ids = self._store.enabled_ids()
        if key not in ids:
            ids.append(key)
            self._store.set_enabled(ids)
        self.load()
        if self._app is not None:
            # 运行期启用的插件可能带蓝图；核心蓝图已注册，插件只能补自己的命名空间。
            self.register_blueprints(self._app)
        record = self._records.get(key)
        return {
            "plugin": record.to_dict() if record else None,
            "restart_required": False,
            "enabled_ids": self._store.enabled_ids(),
        }

    def disable(self, plugin_id: str) -> dict[str, Any]:
        key = str(plugin_id or "").strip()
        if key not in self._records:
            raise PluginNotFound(key)
        ids = [item for item in self._store.enabled_ids() if item != key]
        self._store.set_enabled(ids)
        has_code = bool(self._records[key].manifest and self._records[key].manifest.has_code)
        self.load()
        record = self._records.get(key)
        return {
            "plugin": record.to_dict() if record else None,
            "restart_required": has_code,
            "enabled_ids": self._store.enabled_ids(),
        }

    def uninstall(self, plugin_id: str, *, confirm: str = "") -> dict[str, Any]:
        """卸载：要求先禁用，且请求里回填插件 id 作为确认。"""
        key = str(plugin_id or "").strip()
        record = self._records.get(key)
        if record is None:
            raise PluginNotFound(key)
        if str(confirm) != key:
            raise PluginStateError("卸载需要确认：confirm 必须等于插件 id")
        if record.enabled:
            raise PluginStateError("请先禁用该插件再卸载")
        if record.plugin_dir.is_symlink():
            # 符号链接插件本就不可能被加载；卸载时只删链接本身，绝不跟进目标。
            try:
                record.plugin_dir.unlink()
                removed = True
            except OSError:
                removed = False
        else:
            directory = safe_plugin_path(assert_plugin_root(self.root), key)
            removed = remove_tree(directory)
        ids = [item for item in self._store.enabled_ids() if item != key]
        self._store.set_enabled(ids)
        self.load()
        return {"id": key, "removed": removed, "enabled_ids": ids}

    # -- 内部阶段 ---------------------------------------------------------
    def _discover(self, enabled_ids: list[str]) -> None:
        if not self.root.is_dir():
            return
        for entry in sorted(self.root.iterdir()):
            if entry.name == ENABLED_STATE_FILENAME:
                continue
            if entry.is_symlink():
                self._failed(
                    PluginRecord(id=entry.name, plugin_dir=entry),
                    REASON_UNSAFE_PATH,
                    "插件目录不允许是符号链接",
                )
                continue
            if not entry.is_dir():
                continue
            record = PluginRecord(id=entry.name, plugin_dir=entry, enabled=entry.name in enabled_ids)
            try:
                record.manifest = load_manifest(entry, expected_id=entry.name)
            except PluginError as exc:
                self._failed(record, exc.reason_code, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - 单插件发现失败不影响其它
                logger.warning("plugin_discover_crashed id=%s", entry.name, exc_info=True)
                self._failed(record, REASON_MANIFEST_INVALID, f"清单校验异常：{exc}")
                continue
            self._records[record.id] = record

    def _apply_enabled_state(self, enabled_ids: list[str]) -> None:
        for plugin_id, record in list(self._records.items()):
            if record.state == STATE_FAILED:
                continue
            if plugin_id in enabled_ids:
                continue
            record.state = STATE_DISABLED
            if self._disabled_all and record.enabled:
                record.reason_code = REASON_DISABLED_ALL
                record.reason = "管理员已禁用全部第三方插件，重启前不生效"
            else:
                record.reason_code = REASON_NOT_ENABLED
                record.reason = "未在 enabled.json 的启用列表中"
            record.enabled = False
        for plugin_id in enabled_ids:
            if plugin_id in self._records:
                continue
            record = PluginRecord(
                id=plugin_id,
                plugin_dir=self.root / plugin_id,
                enabled=True,
            )
            self._failed(record, REASON_MISSING, "启用列表里的插件目录不存在")

    def _check_compatibility(self, enabled_ids: list[str]) -> list[str]:
        candidates = [
            plugin_id
            for plugin_id in enabled_ids
            if plugin_id in self._records and self._records[plugin_id].state != STATE_FAILED
        ]
        for plugin_id in candidates:
            record = self._records[plugin_id]
            manifest = record.manifest
            assert manifest is not None
            if manifest.api_version != PLUGIN_API_VERSION:
                self._failed(
                    record,
                    REASON_API_VERSION,
                    f"插件 api_version={manifest.api_version}，宿主为 {PLUGIN_API_VERSION}，已禁用",
                )
                continue
            if not satisfies_version(self._app_version, manifest.requires_app):
                self._failed(
                    record,
                    REASON_APP_VERSION,
                    f"要求 App 版本 {manifest.requires_app}，当前 {self._app_version}",
                )
                continue
            missing = [dep for dep in _dependency_ids(manifest) if dep not in candidates]
            if missing:
                self._failed(
                    record,
                    REASON_DEPENDENCY_MISSING,
                    "依赖未启用或不存在：" + ", ".join(sorted(missing)),
                )
                continue
            bad_versions = []
            for dep_id, spec in manifest.requires_plugins.items():
                if dep_id not in candidates:
                    bad_versions.append(f"{dep_id}（未启用）")
                    continue
                dep = self._records[dep_id].manifest
                if dep is None or not satisfies_version(dep.version, spec):
                    bad_versions.append(f"{dep_id}{spec}")
            if bad_versions:
                self._failed(record, REASON_DEPENDENCY_VERSION, "依赖版本不满足：" + ", ".join(bad_versions))

        # 冲突：双方都禁用，并各自写明对方；避免「谁先加载谁赢」的隐式行为。
        active = [plugin_id for plugin_id in candidates if self._records[plugin_id].state != STATE_FAILED]
        for plugin_id in list(active):
            manifest = self._records[plugin_id].manifest
            assert manifest is not None
            clashes = [other for other in manifest.conflicts if other in active]
            clashes += [
                other
                for other in active
                if other != plugin_id
                and self._records[other].manifest is not None
                and plugin_id in self._records[other].manifest.conflicts
            ]
            if clashes:
                self._failed(
                    self._records[plugin_id],
                    REASON_CONFLICT,
                    "与插件冲突：" + ", ".join(sorted(dict.fromkeys(clashes))),
                )
        return [plugin_id for plugin_id in candidates if self._records[plugin_id].state != STATE_FAILED]

    def _resolve_order(self, candidates: list[str]) -> list[str]:
        """按依赖拓扑排序；环里的插件全部标记失败。"""
        remaining = set(candidates)
        ordered: list[str] = []

        def _ready(plugin_id: str, pending: set[str]) -> bool:
            manifest = self._records[plugin_id].manifest
            if manifest is None:
                return False
            return not (set(_dependency_ids(manifest)) & pending)

        while True:
            ready = sorted(plugin_id for plugin_id in remaining if _ready(plugin_id, remaining))
            if not ready:
                break
            for plugin_id in ready:
                ordered.append(plugin_id)
                remaining.discard(plugin_id)
        for plugin_id in sorted(remaining):
            self._failed(
                self._records[plugin_id],
                REASON_DEPENDENCY_CYCLE,
                "依赖存在环：" + ", ".join(sorted(remaining)),
            )
        return ordered

    def _activate(self, record: PluginRecord) -> None:
        manifest = record.manifest
        if manifest is None:
            return
        loaded_capabilities: list[str] = []
        try:
            assert_plugin_resources(record.plugin_dir)
        except PluginError as exc:
            self._failed(record, exc.reason_code, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin_resource_check_crashed id=%s", record.id, exc_info=True)
            self._failed(record, REASON_CAPABILITY, f"资源检查异常：{exc}")
            return

        try:
            if "prop_templates" in manifest.capabilities:
                self._activate_templates(record)
                loaded_capabilities.append("prop_templates")

            module: ModuleType | None = None
            if manifest.has_code and manifest.code_entry:
                module = self._import_module(record, manifest.code_entry)

            if "http_blueprints" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["http_blueprints"]["module"])
                )
                for name in manifest.capabilities["http_blueprints"]["blueprints"]:
                    blueprint = getattr(module, name, None)
                    if blueprint is None:
                        raise ManifestError(f"入口模块没有蓝图：{name}")
                    self._blueprints.append((record.id, blueprint))
                loaded_capabilities.append("http_blueprints")

            if "agent_tools" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["agent_tools"]["module"])
                )
                self._activate_tools(record, module)
                loaded_capabilities.append("agent_tools")

            if "rulesets" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["rulesets"]["module"])
                )
                self._activate_ruleset(record, module)
                loaded_capabilities.append("rulesets")

            if "settings_sections" in manifest.capabilities:
                loaded_capabilities.append("settings_sections")

            if "frontend_assets" in manifest.capabilities:
                self._check_frontend_assets(record)
                loaded_capabilities.append("frontend_assets")
        except PluginError as exc:
            self._rollback(record)
            self._failed(record, exc.reason_code, str(exc))
            return
        except KeyboardInterrupt:  # 允许运维中断启动
            raise
        except BaseException as exc:  # noqa: BLE001 - 插件代码异常（含 SystemExit）只影响自己
            logger.warning("plugin_activate_crashed id=%s", record.id, exc_info=True)
            self._rollback(record)
            self._failed(record, REASON_CODE_LOAD, f"加载失败：{exc}")
            return

        record.state = STATE_LOADED
        record.reason = ""
        record.reason_code = ""
        record.enabled = True
        record.loaded_capabilities = tuple(loaded_capabilities)
        logger.info("plugin_loaded id=%s version=%s capabilities=%s", record.id, manifest.version, ",".join(loaded_capabilities))

    def _activate_templates(self, record: PluginRecord) -> None:
        manifest = record.manifest
        assert manifest is not None
        dirs: list[Path] = []
        for relative in manifest.capabilities["prop_templates"]["dirs"]:
            path = safe_plugin_path(record.plugin_dir, relative)
            if not path.is_dir():
                raise ManifestError(f"模板目录不存在：{relative}")
            dirs.append(path)
        props_registry.register_template_source(f"plugin:{record.id}", dirs)

    def _activate_tools(self, record: PluginRecord, module: ModuleType) -> None:
        declared = record.manifest.capabilities["agent_tools"]["tools"] if record.manifest else []
        factory = getattr(module, "create_tools", None)
        tools = None
        if callable(factory):
            tools = factory()
        else:
            tools = getattr(module, "TOOLS", None)
        if not isinstance(tools, (list, tuple)):
            raise ManifestError("Agent 工具插件必须提供 create_tools() 或 TOOLS 列表")
        allowed = set(declared)
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            if name not in allowed:
                raise ManifestError(f"未在清单中声明的 Agent 工具：{name or '<unnamed>'}")
            if not name.startswith(f"{record.id}."):
                raise ManifestError(f"Agent 工具名必须以插件 id 为命名空间：{name}")
        runtime.register_agent_tools(record.id, list(tools))

    def _activate_ruleset(self, record: PluginRecord, module: ModuleType) -> None:
        manifest = record.manifest
        assert manifest is not None
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            factory = getattr(module, "create_plugin", None)
            plugin = factory() if callable(factory) else None
        if plugin is None:
            raise ManifestError("规则集插件必须导出 PLUGIN 或 create_plugin()")
        api_version = int(getattr(plugin, "api_version", 0) or 0)
        if api_version != RULESET_API_VERSION:
            raise ManifestError(
                f"规则集 api_version={api_version}，宿主为 {RULESET_API_VERSION}"
            )
        declared = set(manifest.capabilities["rulesets"].get("ids") or [])
        ruleset_id = str(getattr(plugin, "id", "") or "")
        if declared and ruleset_id not in declared:
            raise ManifestError(f"规则集 id {ruleset_id} 未在清单中声明")
        runtime.register_ruleset_plugin(record.id, plugin)

    def _check_frontend_assets(self, record: PluginRecord) -> None:
        manifest = record.manifest
        assert manifest is not None
        for asset in manifest.capabilities["frontend_assets"]:
            path = safe_plugin_path(record.plugin_dir, str(asset["path"]))
            if not path.is_file():
                raise ManifestError(f"前端资源不存在：{asset['path']}")
            assert_file_size(path, MAX_PLUGIN_FILE_BYTES, str(asset["path"]))

    def _import_module(self, record: PluginRecord, relative: str) -> ModuleType:
        path = safe_plugin_path(record.plugin_dir, relative)
        if not path.is_file():
            raise ManifestError(f"入口模块不存在：{relative}")
        assert_file_size(path, MAX_PLUGIN_FILE_BYTES, relative)
        try:
            stat = path.stat()
        except OSError as exc:
            raise ManifestError(f"入口模块无法读取：{relative}") from exc
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = (record.id, relative)
        cached = self._modules.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]

        module_name = f"trpg_plugin_{record.id.replace('-', '_')}_{Path(relative).stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ManifestError(f"入口模块无法加载：{relative}")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        path_entries = list(sys.path)
        sys.path.insert(0, str(path.parent))
        try:
            spec.loader.exec_module(module)
        except BaseException:
            if previous is not None:
                sys.modules[module_name] = previous
            else:
                sys.modules.pop(module_name, None)
            raise
        finally:
            sys.path[:] = path_entries
        self._modules[cache_key] = (signature, module)
        return module

    def _rollback(self, record: PluginRecord) -> None:
        """回滚该插件已产生的一半注册（模板来源、扩展钩子、模块缓存）。"""
        props_registry.unregister_template_source(f"plugin:{record.id}")
        self._blueprints = [item for item in self._blueprints if item[0] != record.id]
        for key in [key for key in self._modules if key[0] == record.id]:
            cached = self._modules.pop(key, None)
            if cached is not None:
                sys.modules.pop(cached[1].__name__, None)
        runtime.unregister_plugin(record.id)

    def _mark_failed_dependents(self) -> None:
        changed = True
        while changed:
            changed = False
            failed_ids = {
                plugin_id for plugin_id, record in self._records.items() if record.state == STATE_FAILED
            }
            for plugin_id, record in self._records.items():
                if record.state != STATE_LOADED or record.manifest is None:
                    continue
                broken = [dep for dep in _dependency_ids(record.manifest) if dep in failed_ids]
                if broken:
                    props_registry.unregister_template_source(f"plugin:{plugin_id}")
                    runtime.unregister_plugin(plugin_id)
                    self._failed(
                        record,
                        REASON_DEPENDENCY_FAILED,
                        "依赖加载失败：" + ", ".join(sorted(broken)),
                    )
                    changed = True

    def _failed(self, record: PluginRecord, reason_code: str, reason: str) -> None:
        record.state = STATE_FAILED
        record.reason = reason
        record.reason_code = reason_code
        record.loaded_capabilities = ()
        self._records[record.id] = record
        logger.warning("plugin_load_failed id=%s code=%s reason=%s", record.id, reason_code, reason)


# ---------------------------------------------------------------- 单例


_REGISTRY: PluginRegistry | None = None
_REGISTRY_LOCK = threading.RLock()


def plugin_registry() -> PluginRegistry | None:
    return _REGISTRY


def set_plugin_registry(registry: PluginRegistry | None) -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = registry


def reset_plugin_registry() -> None:
    set_plugin_registry(None)


__all__ = [
    "PluginNotFound",
    "PluginRegistry",
    "PluginStateError",
    "PluginStateStore",
    "plugin_registry",
    "reset_plugin_registry",
    "set_plugin_registry",
]
