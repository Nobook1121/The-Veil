"""配置查看与遗留文件迁移（控制台专用）。

复用 :mod:`trpg_server.config` 的分层加载与注释保留写入，不依赖 Flask 请求上下文；
配置目录等依赖由调用方注入（缺省走 paths 解析，含 ``--config-dir`` 覆盖）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server.config import load_settings, update_sections
from trpg_server.config.paths import (
    TEMPLATE_DIR,
    general_config_path,
    resolve_config_dir,
)
from trpg_server.config.sources import (
    LEGACY_JSON_FILES,
    read_legacy_json_layer,
    read_toml_file,
)

logger = logging.getLogger(__name__)


def config_path_info(config_dir: Any = None) -> dict[str, str]:
    """返回配置目录、模板目录与用户配置文件的实际路径。"""
    resolved = resolve_config_dir(config_dir)
    return {
        "config_dir": str(resolved),
        "template_dir": str(TEMPLATE_DIR),
        "general_config": str(general_config_path(resolved)),
    }


def format_settings(section_filter: str | None = None, config_dir: Any = None) -> str:
    """格式化生效配置：逐段列出键值；被部署层（env/CLI）锁定的键标注来源。"""
    settings = load_settings(config_dir)
    lines: list[str] = []
    for section in sorted(settings.data):
        if section_filter and section != section_filter:
            continue
        lines.append(f"[{section}]")
        values = settings.data[section]
        for key in sorted(values):
            lock = settings.locked.get(f"{section}.{key}")
            marker = f"  (locked: {lock})" if lock else ""
            lines.append(f"  {key} = {values[key]!r}{marker}")
    if section_filter and not lines:
        existing = ", ".join(sorted(settings.data))
        return f"未知段：{section_filter}（现有段：{existing}）"
    return "\n".join(lines) if lines else "(无配置)"


def legacy_migration_pending(config_dir: Any = None) -> dict[str, dict[str, Any]]:
    """遗留 JSON 中会被合并进 general.toml 的键（即 TOML 里尚不存在的键）。

    TOML 已有的键运行时本就胜出，无需迁移；只写缺失键保证迁移前后生效配置一致。
    """
    resolved = resolve_config_dir(config_dir)
    user_layer = read_toml_file(general_config_path(resolved))
    pending: dict[str, dict[str, Any]] = {}
    for section, values in read_legacy_json_layer(resolved).items():
        for key, value in values.items():
            if key not in user_layer.get(section, {}):
                pending.setdefault(section, {})[key] = value
    return pending


def legacy_files_present(config_dir: Any = None) -> list[Path]:
    resolved = resolve_config_dir(config_dir)
    return [resolved / filename for filename, _base in LEGACY_JSON_FILES if (resolved / filename).exists()]


def format_migration_preview(config_dir: Any = None) -> str:
    pending = legacy_migration_pending(config_dir)
    files = legacy_files_present(config_dir)
    if not files:
        return "没有遗留 JSON 配置文件，无需迁移。"
    lines = ["将执行以下迁移："]
    if pending:
        for section in sorted(pending):
            for key in sorted(pending[section]):
                lines.append(f"  写入 [{section}] {key} = {pending[section][key]!r}")
    else:
        lines.append("  （没有需要写入 general.toml 的新键）")
    for path in files:
        lines.append(f"  改名 {path.name} -> {path.name}.legacy")
    return "\n".join(lines)


def migrate_legacy_config(config_dir: Any = None) -> list[str]:
    """把遗留 JSON 的缺失键写入 general.toml，并把遗留文件改名为 ``*.legacy``。"""
    resolved = resolve_config_dir(config_dir)
    actions: list[str] = []
    pending = legacy_migration_pending(resolved)
    if pending:
        update_sections(general_config_path(resolved), pending)
        actions.append(f"已写入 {sum(len(v) for v in pending.values())} 个键到 general.toml")
    for path in legacy_files_present(resolved):
        target = path.with_name(path.name + ".legacy")
        path.replace(target)
        actions.append(f"已改名 {path.name} -> {target.name}")
    logger.info("config_legacy_migrated actions=%d", len(actions))
    return actions
