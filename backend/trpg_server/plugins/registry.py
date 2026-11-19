"""插件注册表：发现 → 校验 → 依赖/冲突检查 → 激活 → 启用/禁用 → 卸载。

失败隔离是硬要求：任何一个插件在发现、校验、导入、注册能力的任何一步抛错，
都只会把**该插件**（以及依赖它的插件）标成 ``failed`` 并写日志，主程序照常启动。

阶段 2（cordis-paradigm-adoption-plan）注册语义：

- 激活 = 在插件自己的 :class:`PluginContext` 上登记**可撤销效果**（注册带回执），
  宿主中介的 ``runtime.register_*`` 全部经 ``ctx.effect`` 包装；
- 插件代码在导入/工厂调用期间直接写入核心全局槽位（行动 guard、位置/场景
  提供方、``register_service``）不经宿主入口，用激活窗口前后对拍
  （:class:`_SideEffectCapture`）补进同一条撤销链；
- ``enable`` / ``disable`` 是增量操作：只重建目标插件 fiber 及其依赖子图，
  不重跑全量 ``load``；``rescan`` 仍走全量语义（管理员显式全刷新）；
- 蓝图挂载进 Flask 后不可撤销（阶段三前的平台事实）：蓝图记录本身仍走
  效果链（摘出宿主的挂载清单），真正不可逆的「已注册路由」用
  ``pending_restart`` 表达；HTTP 契约的 ``restart_required`` 收窄为仅含
  ``http_blueprints`` 的插件为 true。

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
from typing import Any, Callable, Iterable, Mapping

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
    REASON_RESTART,
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
from .kernel import FIBER_FAILED, FIBER_WAITING, Fiber, PluginContext
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
        try:
            data = read_json(self.path, default=None)
        except (ValueError, OSError):
            # 损坏的 enabled.json 不能阻断启动，也不能把全部插件拖下水：
            # 记录告警后按空启用列表处理。
            logger.warning("plugin_state_corrupt path=%s, fallback to empty", self.path, exc_info=True)
            data = None
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


class _SideEffectCapture:
    """插件代码激活窗口内的全局注册捕获（对拍 → 差集 → 撤销回执）。

    行动 guard（``workflows.guards``）、位置/场景提供方（``workflows.areas``）
    与 ``runtime.register_service`` 由插件代码在导入或工厂调用期间直接写进
    核心全局槽位，不经宿主的 ``register_*`` 入口，无法逐调用包装。宿主在激活
    窗口前后各拍一次快照，把差集冻结成撤销清单并挂到插件 ctx 上——这是
    「注册即副作用」对既有插件形态（见 veil-map ``create_tools``）的最小侵入
    落地，插件作者不需要改一行代码就获得可卸载性。

    窗口中途抛异常时 ``dispose`` 会先补拍再撤销，半途注册不残留。幂等；
    单项撤销失败只记 WARNING，不中断其余撤销。
    """

    __slots__ = (
        "plugin_id",
        "_guards",
        "_areas",
        "_before_guards",
        "_new_guards",
        "_before_position",
        "_new_position",
        "_before_scene",
        "_new_scene",
        "_before_services",
        "_new_services",
        "_closed",
        "_disposed",
    )

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = str(plugin_id)
        guards = areas = None
        try:  # 核心未装载工作流模块时（精简部署）捕获降级为 no-op
            from trpg_server.workflows import areas as _areas, guards as _guards

            guards, areas = _guards, _areas
        except Exception:  # noqa: BLE001
            logger.debug("side_effect_capture_unavailable plugin=%s", self.plugin_id)
        self._guards = guards
        self._areas = areas
        self._before_guards: frozenset[int] = frozenset()
        self._new_guards: list[Any] = []
        self._before_position: Any = None
        self._new_position: Any = None
        self._before_scene: Any = None
        self._new_scene: Any = None
        self._before_services: dict[str, frozenset[str]] = {}
        self._new_services: list[tuple[str, str]] = []
        self._closed = False
        self._disposed = False

    def open(self) -> None:
        """激活窗口开始：记录全局槽位基线。"""
        if self._guards is not None:
            self._before_guards = frozenset(id(guard) for guard in self._guards.registered_guards())
        if self._areas is not None:
            self._before_position = self._areas.current_position_provider()
            self._before_scene = self._areas.current_scene_id_provider()
        self._before_services = {
            plugin_id: frozenset(names) for plugin_id, names in runtime.services_snapshot().items()
        }

    def close(self) -> None:
        """激活窗口结束：把差集冻结成撤销清单（幂等）。"""
        if self._closed:
            return
        self._closed = True
        if self._guards is not None:
            for guard in self._guards.registered_guards():
                if id(guard) not in self._before_guards:
                    self._new_guards.append(guard)
        if self._areas is not None:
            position = self._areas.current_position_provider()
            self._new_position = position if position is not self._before_position else None
            scene = self._areas.current_scene_id_provider()
            self._new_scene = scene if scene is not self._before_scene else None
        for plugin_id, names in runtime.services_snapshot().items():
            before = self._before_services.get(plugin_id, frozenset())
            self._new_services.extend(
                (plugin_id, name) for name in sorted(names) if name not in before
            )

    def dispose(self) -> None:
        """按冻结清单撤销（幂等）；未 close 时先补拍，保证半途注册不残留。"""
        if self._disposed:
            return
        if not self._closed:
            self.close()
        self._disposed = True
        if self._guards is not None:
            for guard in self._new_guards:
                try:
                    self._guards.unregister_action_guard(guard)
                except Exception:  # noqa: BLE001 - 单项撤销失败不中断兄弟撤销
                    logger.warning("guard_undo_failed plugin=%s", self.plugin_id, exc_info=True)
        if self._areas is not None:
            if self._new_position is not None and self._areas.current_position_provider() is self._new_position:
                # 只有当前槽位还是自己装上的才恢复：后来者覆盖时不许误伤。
                if self._before_position is None:
                    self._areas.clear_position_provider()
                else:
                    self._areas.register_position_provider(self._before_position)
            if self._new_scene is not None and self._areas.current_scene_id_provider() is self._new_scene:
                if self._before_scene is None:
                    self._areas.clear_scene_id_provider()
                else:
                    self._areas.register_scene_id_provider(self._before_scene)
        for plugin_id, name in self._new_services:
            try:
                runtime.unregister_service(plugin_id, name)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "service_undo_failed plugin=%s owner=%s name=%s",
                    self.plugin_id,
                    plugin_id,
                    name,
                    exc_info=True,
                )


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
        # 每插件一条效果链（阶段 2）：激活窗口内的一切注册带回执挂在这里，
        # ctx.dispose() 即该插件的全部回滚；键随激活建立、随回滚/全量重载消亡。
        self._contexts: dict[str, PluginContext] = {}
        self._blueprints: list[tuple[str, Any]] = []
        self._app: Any = None
        self._lock = threading.RLock()
        self._loaded_at = ""
        # 代码插件禁用后其蓝图仍存活到进程重启；记录在条目的 pending_restart 上。
        self._pending_restart: set[str] = set()

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
            # 全量语义（rescan / 启动）：先按 LIFO 撤销全部运行期效果链，再重建
            # 世界；增量路径（enable/disable）不走这里。
            self._dispose_all_contexts()
            self._prune_modules(enabled_ids)
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
            for plugin_id in self._pending_restart:
                record = self._records.get(plugin_id)
                if record is not None and not record.loaded:
                    record.pending_restart = True
                    record.reason_code = REASON_RESTART
                    record.reason = "已禁用，但已注册的 HTTP 路由仍在当前进程中，需重启服务才能完全回滚"

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
        """增量启用：只激活目标 + 因缺它而回滚/落失败的依赖方。

        全量重跑只留给 :meth:`load`（rescan）；模块重导、清单变更也走 rescan。
        运行期启用的蓝图经 :meth:`register_blueprints` 补挂，因此响应的
        ``restart_required`` 恒为 False（HTTP 契约不变）。
        """
        key = str(plugin_id or "").strip()
        record = self._records.get(key)
        if record is None:
            raise PluginNotFound(key)
        if record.manifest is None:
            raise PluginStateError(record.reason or "插件清单不可用，无法启用")
        with self._lock:
            ids = self._store.enabled_ids()
            if key not in ids:
                ids.append(key)
                self._store.set_enabled(ids)
            if self._disabled_all:
                # 与全量 load 的降级保持一致：管理员禁用一切时激活路径整体关闭。
                record.state = STATE_DISABLED
                record.enabled = False
                record.reason_code = REASON_NOT_ENABLED
                record.reason = "未在 enabled.json 的启用列表中"
                return {
                    "plugin": record.to_dict(),
                    "restart_required": False,
                    "enabled_ids": self._store.enabled_ids(),
                }
            record.enabled = True
            self._pending_restart.discard(key)
            record.pending_restart = False
            if record.loaded:
                # 已在运行：幂等确认，不重导模块（文件变更请走 rescan）。
                return {
                    "plugin": record.to_dict(),
                    "restart_required": False,
                    "enabled_ids": self._store.enabled_ids(),
                }
            # 依赖先就绪：目标 + 因缺依赖而等待的启用中插件，按拓扑序逐个激活。
            plan = self._incremental_activation_plan(key)
            loaded_now = {pid for pid, item in self._records.items() if item.loaded}
            activated: dict[str, tuple[str, ...]] = {}
            for pid in plan:
                target = self._records.get(pid)
                if target is None or target.manifest is None or target.loaded:
                    continue
                missing = [dep for dep in self._dependency_names(target) if dep not in loaded_now]
                if missing:
                    self._mark_waiting(target, missing)
                    self._failed(
                        target,
                        REASON_DEPENDENCY_MISSING,
                        "依赖未启用或不存在：" + ", ".join(sorted(missing)),
                    )
                    continue
                blocked = self._gate_activation(target, loaded_now)
                if blocked is not None:
                    self._failed(target, blocked[0], blocked[1])
                    self._fail_fiber(target, blocked[1])
                    continue
                self._activate(target)
                if target.loaded:
                    loaded_now.add(pid)
                    activated[pid] = tuple(target.loaded_capabilities)
            self._invalidate_global_caches(activated)
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
        """增量禁用：只回滚目标 + 连带依赖它的已加载插件（依赖方先撤）。

        内存注册类能力精确撤销、即时生效；``restart_required`` 仅对含
        ``http_blueprints``（进程内不可逆）的插件为 true（HTTP 契约不变，
        语义收窄见 docs/plans/cordis-paradigm-adoption-plan.md 阶段 2）。
        """
        key = str(plugin_id or "").strip()
        record = self._records.get(key)
        if record is None:
            raise PluginNotFound(key)
        with self._lock:
            ids = [item for item in self._store.enabled_ids() if item != key]
            self._store.set_enabled(ids)
            record.enabled = False
            rollback = self._runtime_dependent_closure(key)
            closure = set(rollback)
            touched: dict[str, tuple[str, ...]] = {}
            for pid in rollback:
                target = self._records.get(pid)
                if target is None:
                    continue
                was_loaded = target.loaded
                if was_loaded:
                    touched[pid] = tuple(target.loaded_capabilities)
                    self._deactivate(target, forget_module=(pid == key))
                if pid == key:
                    target.state = STATE_DISABLED
                    target.reason_code = REASON_NOT_ENABLED
                    target.reason = "未在 enabled.json 的启用列表中"
                    target.loaded_capabilities = ()
                    target.fiber = None
                    if "http_blueprints" in touched.get(pid, ()):
                        # 蓝图已挂进 Flask、进程内不可逆：显式标记等待重启。
                        target.pending_restart = True
                        self._pending_restart.add(pid)
                elif was_loaded:
                    missing = [
                        dep
                        for dep in self._dependency_names(target)
                        if dep in closure or self._records.get(dep) is None or not self._records[dep].loaded
                    ]
                    self._mark_waiting(target, missing)
                    self._failed(
                        target,
                        REASON_DEPENDENCY_MISSING,
                        "依赖已禁用，已运行期回滚：" + ", ".join(sorted(missing)),
                    )
                    if "http_blueprints" in touched.get(pid, ()):
                        target.pending_restart = True
                        self._pending_restart.add(pid)
            self._invalidate_global_caches(touched)
        return {
            "plugin": record.to_dict(),
            "restart_required": "http_blueprints" in touched.get(key, ()),
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
        self._pending_restart.discard(key)
        self.load()
        return {"id": key, "removed": removed, "enabled_ids": ids}

    # -- 内部阶段 ---------------------------------------------------------
    def _prune_modules(self, enabled_ids: list[str]) -> None:
        """丢弃不再启用的插件模块，避免目录已删但模块/线程还挂在进程里。

        启用列表里的插件保留模块缓存（同签名可复用）；被禁用/卸载的插件
        从 ``_modules`` 与 ``sys.modules`` 一起移除，下次启用会重新导入。
        """
        keep = {str(item) for item in enabled_ids}
        for cache_key in [key for key in self._modules if key[0] not in keep]:
            cached = self._modules.pop(cache_key, None)
            if cached is not None:
                sys.modules.pop(str(getattr(cached[1], "__name__", "")), None)

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
                # 依赖缺失不是插件自己的错：fiber 落 WAITING 点名在等谁，依赖
                # 启用后由增量 enable 自动恢复；静默等待是调试一号疑难，禁止。
                self._mark_waiting(record, missing)
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

        # 激活窗口（cordis-paradigm-adoption-plan 阶段 2）：宿主中介的注册全部经
        # ``ctx.effect``（注册带回执）；插件代码在导入/工厂调用期间直写核心全局
        # 槽位（行动 guard、位置/场景提供方、register_service）的部分由
        # :class:`_SideEffectCapture` 对拍差集兜进同一条撤销链——``ctx.dispose()``
        # 即该插件的全部回滚。
        stale_ctx = self._contexts.pop(record.id, None)
        if stale_ctx is not None:
            stale_ctx.dispose()  # 重复激活兜底：先撤旧链再挂新链
        ctx = PluginContext(record.id)
        self._contexts[record.id] = ctx
        record.fiber = Fiber(record.id)
        capture = _SideEffectCapture(record.id)
        capture.open()
        ctx.effect(lambda: capture.dispose)  # 链上第一环：LIFO 时最后执行

        try:
            if "prop_templates" in manifest.capabilities:
                self._activate_templates(record, ctx)
                loaded_capabilities.append("prop_templates")

            if "content_packs" in manifest.capabilities:
                self._activate_content_packs(record, ctx)
                loaded_capabilities.append("content_packs")

            module: ModuleType | None = None
            if manifest.has_code and manifest.code_entry:
                module = self._import_module(record, manifest.code_entry)

            if "http_blueprints" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["http_blueprints"]["module"])
                )
                blueprints: list[Any] = []
                for name in manifest.capabilities["http_blueprints"]["blueprints"]:
                    blueprint = getattr(module, name, None)
                    if blueprint is None:
                        raise ManifestError(f"入口模块没有蓝图：{name}")
                    blueprints.append(blueprint)
                self._register_blueprint_entries(record, blueprints, ctx)
                loaded_capabilities.append("http_blueprints")

            if "agent_tools" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["agent_tools"]["module"])
                )
                self._activate_tools(record, module, ctx)
                loaded_capabilities.append("agent_tools")

            if "rulesets" in manifest.capabilities:
                module = module or self._import_module(
                    record, str(manifest.capabilities["rulesets"]["module"])
                )
                self._activate_ruleset(record, module, ctx)
                loaded_capabilities.append("rulesets")

            if "settings_sections" in manifest.capabilities:
                loaded_capabilities.append("settings_sections")

            if "frontend_assets" in manifest.capabilities:
                self._check_frontend_assets(record)
                loaded_capabilities.append("frontend_assets")
        except PluginError as exc:
            self._rollback(record)
            self._failed(record, exc.reason_code, str(exc))
            self._fail_fiber(record, str(exc))
            return
        except KeyboardInterrupt:  # 允许运维中断启动
            raise
        except BaseException as exc:  # noqa: BLE001 - 插件代码异常（含 SystemExit）只影响自己
            logger.warning("plugin_activate_crashed id=%s", record.id, exc_info=True)
            self._rollback(record)
            self._failed(record, REASON_CODE_LOAD, f"加载失败：{exc}")
            self._fail_fiber(record, f"加载失败：{exc}")
            return

        capture.close()  # 冻结窗口差集：之后的注册不属于本插件
        record.fiber.activate()
        record.state = STATE_LOADED
        record.reason = ""
        record.reason_code = ""
        record.enabled = True
        record.loaded_capabilities = tuple(loaded_capabilities)
        logger.info("plugin_loaded id=%s version=%s capabilities=%s", record.id, manifest.version, ",".join(loaded_capabilities))

    def _activate_templates(self, record: PluginRecord, ctx: PluginContext) -> None:
        manifest = record.manifest
        assert manifest is not None
        dirs: list[Path] = []
        for relative in manifest.capabilities["prop_templates"]["dirs"]:
            path = safe_plugin_path(record.plugin_dir, relative)
            if not path.is_dir():
                raise ManifestError(f"模板目录不存在：{relative}")
            dirs.append(path)
        source_name = f"plugin:{record.id}"

        def _register() -> Callable[[], None]:
            props_registry.register_template_source(source_name, dirs)
            return lambda: props_registry.unregister_template_source(source_name)

        ctx.effect(_register)

    def _activate_content_packs(self, record: PluginRecord, ctx: PluginContext) -> None:
        """装载只读内容数据包；启动阶段只校验 pack.json 与文件清单。"""
        from .content_packs import ContentPackLibrary

        manifest = record.manifest
        assert manifest is not None
        library = ContentPackLibrary.from_manifest(manifest)

        def _register() -> Callable[[], None]:
            runtime.register_content_packs(record.id, library)
            return lambda: runtime.unregister_content_packs(record.id)

        ctx.effect(_register)

    def _register_blueprint_entries(
        self, record: PluginRecord, blueprints: list[Any], ctx: PluginContext
    ) -> None:
        """蓝图记录入册（挂进 Flask 的动作本身进程内不可逆，由 pending_restart 表达）。"""
        entries = tuple((record.id, blueprint) for blueprint in blueprints)

        def _register() -> Callable[[], None]:
            self._blueprints.extend(entries)

            def _dispose() -> None:
                self._blueprints = [item for item in self._blueprints if item not in entries]

            return _dispose

        ctx.effect(_register)

    def _activate_tools(self, record: PluginRecord, module: ModuleType, ctx: PluginContext) -> None:
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
        # 清单层已禁止与核心命名空间同名的插件 id；这里再与真实注册表比对，
        # 防止核心新增工具而保留名单没同步时被插件顶掉。
        from trpg_server.agents.tools import core_agent_tool_names

        core_names = core_agent_tool_names()
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            if name not in allowed:
                raise ManifestError(f"未在清单中声明的 Agent 工具：{name or '<unnamed>'}")
            if not name.startswith(f"{record.id}."):
                raise ManifestError(f"Agent 工具名必须以插件 id 为命名空间：{name}")
            if name in core_names:
                raise ManifestError(f"Agent 工具与核心工具重名，拒绝覆盖：{name}")
        tools_list = list(tools)

        def _register() -> Callable[[], None]:
            runtime.register_agent_tools(record.id, tools_list)
            return lambda: runtime.unregister_agent_tools(record.id)

        ctx.effect(_register)

    def _activate_ruleset(self, record: PluginRecord, module: ModuleType, ctx: PluginContext) -> None:
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

        def _register() -> Callable[[], None]:
            runtime.register_ruleset_plugin(record.id, plugin)
            return lambda: runtime.unregister_ruleset_plugin(record.id)

        ctx.effect(_register)

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
        """激活半途失败的整体回滚：dispose 效果链（LIFO）+ 半坏模块不缓存。"""
        self._deactivate(record, forget_module=True)

    def _deactivate(self, record: PluginRecord, *, forget_module: bool) -> None:
        """运行期整体撤销：dispose 效果链 + ctx 覆盖不到的部分兜底。

        蓝图记录、runtime 注册、插件直写全局槽位的差集都在 ctx 的撤销链上；
        ctx 覆盖不到的只有模块缓存——``forget_module`` 为真时半坏/已禁用的
        插件模块从 ``_modules`` 与 ``sys.modules`` 一起移除，下次重新导入。
        等待依赖的插件（仍在启用列表里）保留模块缓存，恢复激活零重导。
        """
        ctx = self._contexts.pop(record.id, None)
        if ctx is not None:
            ctx.dispose()
        else:
            # 没有效果链（异常/历史路径）：沿用旧式手工清理，保证幂等。
            props_registry.unregister_template_source(f"plugin:{record.id}")
            self._blueprints = [item for item in self._blueprints if item[0] != record.id]
            runtime.unregister_plugin(record.id)
        if forget_module:
            for cache_key in [key for key in self._modules if key[0] == record.id]:
                cached = self._modules.pop(cache_key, None)
                if cached is not None:
                    sys.modules.pop(str(getattr(cached[1], "__name__", "")), None)

    def _dispose_all_contexts(self) -> None:
        """全量重载前撤销全部效果链（LIFO）；dispose 自身不抛异常。"""
        for plugin_id in list(self._contexts):
            ctx = self._contexts.pop(plugin_id, None)
            if ctx is not None:
                ctx.dispose()

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
                    # 依赖方整体回滚（效果链 LIFO），落 WAITING 点名在等谁：
                    # 依赖修复并重新启用后由增量 enable 自动恢复。
                    self._deactivate(record, forget_module=False)
                    self._mark_waiting(record, broken)
                    self._failed(
                        record,
                        REASON_DEPENDENCY_FAILED,
                        "依赖加载失败：" + ", ".join(sorted(broken)),
                    )
                    changed = True

    # -- 增量启停辅助（cordis-paradigm-adoption-plan 阶段 2）----------------

    def _dependency_names(self, record: PluginRecord) -> list[str]:
        if record.manifest is None:
            return []
        return _dependency_ids(record.manifest)

    def _mark_waiting(self, record: PluginRecord, missing: Iterable[str]) -> None:
        """运行期等待态：fiber 点名在等谁（静默 WAITING 是调试一号疑难，禁止）。"""
        names = list(dict.fromkeys(str(item) for item in missing if str(item).strip()))
        fiber = record.fiber
        if fiber is None or fiber.state == FIBER_FAILED:
            fiber = Fiber(record.id)
            record.fiber = fiber
        fiber.mark_waiting(names)

    def _fail_fiber(self, record: PluginRecord, reason: str) -> None:
        fiber = record.fiber
        if fiber is None:
            fiber = Fiber(record.id)
            record.fiber = fiber
        if fiber.state != FIBER_FAILED:
            fiber.fail(str(reason or "unknown"))

    def _runtime_dependent_closure(self, target_id: str) -> list[str]:
        """运行期回滚集合：目标 + 传递依赖它的已加载插件。

        排序保证「依赖方先撤、提供方最后撤」（各自的对拍快照因此保持准确）；
        同层插件互不依赖，按 id 稳定排序即可。
        """
        key = str(target_id or "").strip()
        loaded = {plugin_id for plugin_id, record in self._records.items() if record.loaded}
        if key not in loaded:
            return [key]
        dependents: dict[str, list[str]] = {}
        for plugin_id in loaded:
            manifest = self._records[plugin_id].manifest
            if manifest is None:
                continue
            for dep in _dependency_ids(manifest):
                if dep in loaded and dep != plugin_id:
                    dependents.setdefault(dep, []).append(plugin_id)
        closure = {key}
        queue = [key]
        while queue:
            for dependent in dependents.get(queue.pop(), ()):
                if dependent not in closure:
                    closure.add(dependent)
                    queue.append(dependent)
        depth = {plugin_id: 0 for plugin_id in closure}
        for _ in range(len(closure)):
            for plugin_id in closure:
                if plugin_id == key:
                    continue
                manifest = self._records[plugin_id].manifest
                inside = [
                    dep
                    for dep in (_dependency_ids(manifest) if manifest else ())
                    if dep in closure
                ]
                value = 1 + max([depth.get(dep, 0) for dep in inside] or [0])
                if value > depth.get(plugin_id, 0):
                    depth[plugin_id] = value
        return sorted(closure, key=lambda plugin_id: (-depth[plugin_id], plugin_id))

    def _incremental_activation_plan(self, seed: str) -> list[str]:
        """增量启用要重建的插件集合：目标 + 因缺依赖而等待的启用中插件。

        覆盖两类等待方：运行期被连带回滚的（fiber WAITING），以及启动时就
        因依赖缺失/依赖失败而没激活的（reason_code 点名）；其余启动期失败
        （清单/版本/冲突）不自动重试，交给 rescan。
        """
        candidates = {seed}
        for plugin_id, record in self._records.items():
            if plugin_id == seed or record.loaded or not record.enabled:
                continue
            waiting = record.fiber is not None and record.fiber.state == FIBER_WAITING
            dep_blocked = record.reason_code in (REASON_DEPENDENCY_MISSING, REASON_DEPENDENCY_FAILED)
            if waiting or dep_blocked:
                candidates.add(plugin_id)
        return self._dependency_order(candidates)

    def _dependency_order(self, candidates: Iterable[str]) -> list[str]:
        """候选集的依赖拓扑序；环依赖放行，交给逐项 missing 检查显式标记。"""
        pending = {plugin_id for plugin_id in candidates if plugin_id in self._records}
        ordered: list[str] = []
        while pending:
            ready = sorted(
                plugin_id
                for plugin_id in pending
                if not (set(self._dependency_names(self._records[plugin_id])) & pending)
            )
            if not ready:
                ready = sorted(pending)
            for plugin_id in ready:
                ordered.append(plugin_id)
                pending.discard(plugin_id)
        return ordered

    def _gate_activation(self, record: PluginRecord, active_ids: set[str]) -> tuple[str, str] | None:
        """增量启用前的兼容性闸门（与全量 load 同一套失败码）。

        冲突语义与 load 期的差别：运行中的冲突方不动，被启用方落 FAIL
        （增量操作不该连坐正在运行的插件）。
        """
        manifest = record.manifest
        assert manifest is not None
        if manifest.api_version != PLUGIN_API_VERSION:
            return (
                REASON_API_VERSION,
                f"插件 api_version={manifest.api_version}，宿主为 {PLUGIN_API_VERSION}，已禁用",
            )
        if not satisfies_version(self._app_version, manifest.requires_app):
            return (
                REASON_APP_VERSION,
                f"要求 App 版本 {manifest.requires_app}，当前 {self._app_version}",
            )
        bad_versions = []
        for dep_id, spec in manifest.requires_plugins.items():
            dep = self._records.get(dep_id)
            dep_manifest = dep.manifest if dep is not None else None
            if dep_manifest is None or not satisfies_version(dep_manifest.version, spec):
                bad_versions.append(f"{dep_id}{spec}")
        if bad_versions:
            return (REASON_DEPENDENCY_VERSION, "依赖版本不满足：" + ", ".join(bad_versions))
        clashes = [
            other
            for other in active_ids
            if other != record.id
            and (
                other in manifest.conflicts
                or (
                    self._records.get(other) is not None
                    and self._records[other].manifest is not None
                    and record.id in self._records[other].manifest.conflicts
                )
            )
        ]
        if clashes:
            return (REASON_CONFLICT, "与插件冲突：" + ", ".join(sorted(dict.fromkeys(clashes))))
        return None

    def _invalidate_global_caches(self, capabilities: Mapping[str, Iterable[str]]) -> None:
        """启停增量后的针对性缓存失效：只重建受影响的注册表，不重导其他插件模块。"""
        touched: set[str] = set()
        for caps in capabilities.values():
            touched.update(str(item) for item in (caps or ()))
        if not touched:
            return
        if "prop_templates" in touched:
            props_registry.rescan()
        if "rulesets" in touched:
            try:
                from trpg_server.ruleset.registry import reset_builtin_registry

                reset_builtin_registry()
            except Exception:  # noqa: BLE001 - 重置失败不影响插件状态
                logger.debug("ruleset_registry_reset_failed", exc_info=True)

    def _failed(self, record: PluginRecord, reason_code: str, reason: str) -> None:
        record.state = STATE_FAILED
        record.reason = reason
        record.reason_code = reason_code
        record.loaded_capabilities = ()
        if record.enabled and record.fiber is None:
            # 启用中插件的失败必须可观测：fiber 落 FAILED；依赖类失败由调用方
            # 先 ``_mark_waiting`` 覆盖为 WAITING（点名在等谁，禁止静默）。
            fiber = Fiber(record.id)
            fiber.fail(reason)
            record.fiber = fiber
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
