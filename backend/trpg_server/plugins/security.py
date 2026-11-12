"""插件目录的安全边界。

插件是「管理员显式安装的本地目录」，不是沙箱。这里能做、也必须做的是：

- 所有路径都经 :func:`safe_plugin_path`（内部复用 ``security.safe_join``），
  拒绝 ``..`` 与绝对路径；
- 拒绝符号链接：插件目录本身、目录内任意一级目录/文件只要是 symlink 就拒绝，
  避免 ``data/plugins/x -> C:\\Windows`` 这类逃逸；
- 资源有上限（文件数、单文件、总量），避免一个大插件把磁盘吃满。

代码执行本身的隔离不在这里做——见 ``contracts.py`` 的信任边界说明。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from trpg_server.config.paths import DATA_DIR
from trpg_server.security import safe_join

from .contracts import (
    ENABLED_STATE_FILENAME,
    MAX_PLUGIN_FILE_BYTES,
    MAX_PLUGIN_FILES,
    MAX_PLUGIN_MANIFEST_BYTES,
    MAX_PLUGIN_TOTAL_BYTES,
    ManifestError,
    ResourceLimitError,
    UnsafePluginPath,
)


def default_plugin_root() -> Path:
    """插件根目录：``data/plugins``（可用 ``PLUGIN_DIR`` 配置覆盖）。"""
    return Path(DATA_DIR) / "plugins"


def enabled_state_path(root: str | Path | None = None) -> Path:
    return Path(root or default_plugin_root()) / ENABLED_STATE_FILENAME


def assert_plugin_root(root: str | Path) -> Path:
    """返回解析后的插件根目录；根目录本身是符号链接时拒绝。"""
    path = Path(root)
    if path.is_symlink():
        raise UnsafePluginPath(f"插件根目录不允许是符号链接：{path}")
    return path.resolve()


def safe_plugin_path(base: str | Path, *parts: str | os.PathLike[str]) -> Path:
    """在插件目录内解析路径；越界或经过符号链接时抛 :class:`UnsafePluginPath`。"""
    base_path = Path(base)
    if base_path.is_symlink():
        raise UnsafePluginPath(f"插件目录不允许是符号链接：{base_path.name}")
    resolved_base = base_path.resolve()
    try:
        target = safe_join(resolved_base, *[str(part) for part in parts])
    except ValueError as exc:
        raise UnsafePluginPath(f"插件路径越界：{'/'.join(str(part) for part in parts)}") from exc

    current = resolved_base
    for part in parts:
        current = current / str(part)
        if current.is_symlink():
            raise UnsafePluginPath(f"插件内不允许符号链接：{current.name}")
    return target


def ensure_plugin_dir(root: str | Path, plugin_id: str) -> Path:
    """校验 ``<root>/<plugin_id>`` 存在且是一个真实目录。"""
    base = assert_plugin_root(root)
    directory = safe_plugin_path(base, plugin_id)
    if not directory.is_dir():
        raise ManifestError(f"插件目录不存在：{plugin_id}")
    return directory


def read_text_limited(path: Path, limit: int, label: str) -> str:
    """读取文本并做体积上限检查（先看 stat，避免先读进内存）。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(f"{label} 无法读取") from exc
    if size > limit:
        raise ResourceLimitError(f"{label} 超过大小上限（{limit // 1024} KB）")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{label} 无法按 UTF-8 读取") from exc


def assert_plugin_resources(root: str | Path) -> None:
    """遍历插件目录做安全审计：拒绝符号链接、限制文件数与总体积。

    只做元数据检查，不读取文件内容；纯数据插件与代码插件都适用。
    """
    base = Path(root)
    if base.is_symlink():
        raise UnsafePluginPath("插件目录不允许是符号链接")
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in {"__pycache__", ".git"}]
        for name in dirnames + filenames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                raise UnsafePluginPath(f"插件内不允许符号链接：{entry.relative_to(base)}")
        for name in filenames:
            entry = Path(dirpath) / name
            try:
                info = entry.stat()
            except OSError as exc:
                raise ManifestError(f"插件文件无法读取：{entry.name}") from exc
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePluginPath(f"插件内只允许普通文件：{entry.name}")
            files += 1
            if files > MAX_PLUGIN_FILES:
                raise ResourceLimitError(f"插件文件数超过上限（{MAX_PLUGIN_FILES}）")
            total += info.st_size
            if total > MAX_PLUGIN_TOTAL_BYTES:
                raise ResourceLimitError(
                    f"插件总体积超过上限（{MAX_PLUGIN_TOTAL_BYTES // (1024 * 1024)} MB）"
                )


def assert_file_size(path: Path, limit: int, label: str) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(f"{label} 无法读取") from exc
    if size > limit:
        raise ResourceLimitError(f"{label} 超过大小上限（{limit // 1024} KB）")


__all__ = [
    "assert_file_size",
    "assert_plugin_resources",
    "assert_plugin_root",
    "default_plugin_root",
    "enabled_state_path",
    "ensure_plugin_dir",
    "read_text_limited",
    "safe_plugin_path",
    "MAX_PLUGIN_MANIFEST_BYTES",
    "MAX_PLUGIN_FILE_BYTES",
]
