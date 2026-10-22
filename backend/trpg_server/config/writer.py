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


def update_sections(path: Path, changes: Mapping[str, Mapping[str, Any]]) -> Path:
    """把 ``changes``（section → {key: value}）定点写入 ``path`` 并原子替换。

    只更新列出的键，文件其余内容（含注释与未修改键）保持不变。
    """
    with _write_lock:
        text = ""
        if path.exists():
            text = path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text) if text.strip() else tomlkit.document()

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
