"""注释保留的 TOML 写入。

基于 ``tomlkit`` 往返解析：读入文档后仅更新指定 section 的键，注释、键序、
空白原样保留；不存在的 section 自动追加到文件末尾。本模块只负责 diff 后的
定点更新、进程内锁与原子替换；注释保留由 tomlkit 保证。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

import tomlkit

from trpg_server.config import merge

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()


def _normalize_quoted_dotted_tables(doc: Any) -> None:
    """把根级带引号的点分表名（``["ai.small_models"]``）折叠为嵌套表。

    历史脏数据里段名以引号整体出现；若不做归一，写入 ``ai.small_models`` 会在
    文档里同时存在 ``["ai.small_models"]`` 与 ``[ai.small_models]`` 两种形态，
    序列化后即非法 TOML。合法文件不可能两种形态并存（解析期就会冲突报错），
    因此折叠后不会与既有嵌套表冲突。
    """
    for key in list(doc.keys()):
        if "." not in key:
            continue
        value = doc[key]
        if not isinstance(value, tomlkit.items.Table):
            continue
        del doc[key]
        target: Any = doc
        parts = key.split(".")
        for part in parts[:-1]:
            existing = target.get(part)
            if not isinstance(existing, tomlkit.items.Table):
                existing = tomlkit.table()
                target[part] = existing
            target = existing
        target[parts[-1]] = value


def update_sections(path: Path, changes: Mapping[str, Mapping[str, Any]]) -> Path:
    """把 ``changes``（section → {key: value}）定点写入 ``path`` 并原子替换。

    只更新列出的键，文件其余内容（含注释与未修改键）保持不变。
    """
    with _write_lock:
        text = ""
        if path.exists():
            text = path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text) if text.strip() else tomlkit.document()
        _normalize_quoted_dotted_tables(doc)

        for section, values in changes.items():
            if not values:
                continue
            table: Any = doc
            for part in section.split("."):
                existing = table.get(part)
                if not isinstance(existing, (dict, tomlkit.items.Table)):
                    existing = tomlkit.table()
                    table[part] = existing
                table = existing
            for key, value in values.items():
                table[key] = value

        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(temp_path, path)

    merge.invalidate_settings_cache()
    return path
