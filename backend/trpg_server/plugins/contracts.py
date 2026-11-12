"""插件体系的公共契约（``docs/plugin-development.md``）。

边界（硬约定）：

- **默认不启用任何第三方插件**：只有出现在 ``data/plugins/enabled.json`` 的
  ``enabled`` 列表里的插件才会被激活；未启用的插件只读取 ``plugin.json``
  元数据用于列表展示，不会读取 HTML/CSS/资源，更不会导入 Python。
- 插件代码以**宿主进程权限**运行；信任边界是「站点管理员显式安装并启用」。
- 全部路径经 :func:`trpg_server.plugins.security.safe_plugin_path`；禁止符号
  链接逃逸，资源有体积/数量上限。
- 插件清单是纯数据，加载清单阶段**不得**产生任何副作用（无网络、无磁盘写入）。

本模块只放常量与数据结构，不 import 任何具体插件，也不 import Flask。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# 清单结构版本：字段发生不兼容变化时递增。
PLUGIN_MANIFEST_VERSION = 1
# 插件 API 版本：能力接口发生不兼容变化时递增；与清单 api_version 不一致即拒绝加载。
PLUGIN_API_VERSION = 1

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")

# 能力名 → 中文说明，供 /api/plugins 与文档共用。
CAPABILITY_LABELS: dict[str, str] = {
    "prop_templates": "道具模板",
    "rulesets": "规则集",
    "agent_tools": "Agent 工具",
    "http_blueprints": "HTTP 蓝图",
    "settings_sections": "设置页配置段",
    "frontend_assets": "前端资源",
}
CAPABILITY_NAMES: tuple[str, ...] = tuple(CAPABILITY_LABELS)

# 生命周期状态。``available`` 表示「在磁盘上发现了，但不在启用列表里」。
STATE_AVAILABLE = "available"
STATE_LOADED = "loaded"
STATE_DISABLED = "disabled"
STATE_FAILED = "failed"
STATE_PENDING_RESTART = "pending_restart"

# 失败原因码：写进清单接口，前端/开发者按码判断，按 reason 展示。
REASON_MANIFEST_INVALID = "manifest_invalid"
REASON_API_VERSION = "api_version_mismatch"
REASON_APP_VERSION = "app_version_incompatible"
REASON_DEPENDENCY_MISSING = "dependency_missing"
REASON_DEPENDENCY_VERSION = "dependency_version"
REASON_DEPENDENCY_FAILED = "dependency_failed"
REASON_DEPENDENCY_CYCLE = "dependency_cycle"
REASON_CONFLICT = "conflict"
REASON_NOT_ENABLED = "not_enabled"
REASON_DISABLED_ALL = "plugins_disabled_by_operator"
REASON_RESOURCE_LIMIT = "resource_limit"
REASON_UNSAFE_PATH = "unsafe_path"
REASON_CODE_LOAD = "code_load_failed"
REASON_CAPABILITY = "capability_failed"
REASON_MISSING = "plugin_dir_missing"
REASON_RESTART = "restart_required"

# 资源上限：插件目录整体（含代码/模板/资源）。
MAX_PLUGIN_FILES = 512
MAX_PLUGIN_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PLUGIN_FILE_BYTES = 16 * 1024 * 1024
MAX_PLUGIN_MANIFEST_BYTES = 128 * 1024
# 纯数据（模板/前端资源）单文件上限，与 props/template_validation.py 保持一致。
MAX_DATA_FILE_BYTES = 20 * 1024 * 1024

ENABLED_STATE_FILENAME = "enabled.json"
MANIFEST_FILENAME = "plugin.json"

# 状态文件的版本；不匹配时按空启用列表处理并告警（不阻断启动）。
STATE_FILE_VERSION = 1


class PluginError(RuntimeError):
    """插件体系的基础异常。"""

    reason_code = REASON_MANIFEST_INVALID


class ManifestError(PluginError):
    """``plugin.json`` 不合法（缺字段、类型不对、路径越界等）。"""


class UnsafePluginPath(PluginError):
    """插件目录/资源路径越界或经过符号链接。"""

    reason_code = REASON_UNSAFE_PATH


class ResourceLimitError(PluginError):
    """插件文件数量/体积超过上限。"""

    reason_code = REASON_RESOURCE_LIMIT


@dataclass(frozen=True)
class PluginManifest:
    """校验并归一化后的插件清单（只读）。"""

    id: str
    name: str
    version: str
    description: str
    author: str
    manifest_version: int
    api_version: int
    # 目标 App 版本区间，例如 ``">=1.0.0 <2.0.0"``；空串表示不限制。
    requires_app: str
    requires_plugins: Mapping[str, str] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    # 相对插件根目录的 Python 入口；``None`` 表示纯数据插件。
    code_entry: str | None = None
    plugin_dir: Path = Path(".")

    @property
    def has_code(self) -> bool:
        return bool(self.code_entry)

    def capability(self, name: str) -> Any:
        return self.capabilities.get(name)

    def capability_names(self) -> tuple[str, ...]:
        return tuple(name for name in CAPABILITY_NAMES if name in self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        """可序列化视图；刻意不含绝对路径，避免通过接口泄露本机目录。"""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "manifest_version": self.manifest_version,
            "api_version": self.api_version,
            "requires_app": self.requires_app,
            "requires_plugins": dict(self.requires_plugins),
            "dependencies": list(self.dependencies),
            "conflicts": list(self.conflicts),
            "capabilities": {
                name: self.capabilities[name] for name in self.capability_names()
            },
            "has_code": self.has_code,
        }


@dataclass
class PluginRecord:
    """一个插件的运行期状态（可序列化给接口）。"""

    id: str
    plugin_dir: Path
    manifest: PluginManifest | None = None
    state: str = STATE_AVAILABLE
    reason: str = ""
    reason_code: str = ""
    enabled: bool = False
    # 已成功激活的能力名（只有 loaded 才有意义）。
    loaded_capabilities: tuple[str, ...] = ()
    module: Any = None

    @property
    def loaded(self) -> bool:
        return self.state == STATE_LOADED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "enabled": self.enabled,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "dir": str(self.plugin_dir),
            "capabilities": list(self.loaded_capabilities),
            "manifest": self.manifest.to_dict() if self.manifest else None,
        }


__all__ = [
    "CAPABILITY_LABELS",
    "CAPABILITY_NAMES",
    "ENABLED_STATE_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_DATA_FILE_BYTES",
    "MAX_PLUGIN_FILE_BYTES",
    "MAX_PLUGIN_FILES",
    "MAX_PLUGIN_MANIFEST_BYTES",
    "MAX_PLUGIN_TOTAL_BYTES",
    "ManifestError",
    "PLUGIN_API_VERSION",
    "PLUGIN_ID_RE",
    "PLUGIN_MANIFEST_VERSION",
    "PluginError",
    "PluginManifest",
    "PluginRecord",
    "REASON_API_VERSION",
    "REASON_APP_VERSION",
    "REASON_CAPABILITY",
    "REASON_CODE_LOAD",
    "REASON_CONFLICT",
    "REASON_DEPENDENCY_CYCLE",
    "REASON_DEPENDENCY_FAILED",
    "REASON_DEPENDENCY_MISSING",
    "REASON_DEPENDENCY_VERSION",
    "REASON_DISABLED_ALL",
    "REASON_MANIFEST_INVALID",
    "REASON_MISSING",
    "REASON_NOT_ENABLED",
    "REASON_RESOURCE_LIMIT",
    "REASON_RESTART",
    "REASON_UNSAFE_PATH",
    "ResourceLimitError",
    "STATE_AVAILABLE",
    "STATE_DISABLED",
    "STATE_FAILED",
    "STATE_FILE_VERSION",
    "STATE_LOADED",
    "STATE_PENDING_RESTART",
    "UnsafePluginPath",
    "VERSION_RE",
]
