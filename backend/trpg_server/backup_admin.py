"""数据备份（控制台专用）：把关键数据目录打包为 zip。

只打包配置与数据（config / scenarios / rooms / 知识库 / 用户库 / 日志），
不触碰向量库等可重建产物，避免备份体积失控。
"""
from __future__ import annotations

import logging
import time
import zipfile
from pathlib import Path
from typing import Any

from trpg_server.settings import DATA_DIR, LOGS_DIR, ROOMS_DIR, SCENARIOS_DIR

logger = logging.getLogger(__name__)

# 备份内容：目录名 -> 实际路径。目录缺失时跳过。
_BACKUP_SOURCES = {
    "config": DATA_DIR / "config",
    "scenarios": SCENARIOS_DIR,
    "rooms": ROOMS_DIR,
    "knowledge-bases": DATA_DIR / "runtime" / "knowledge-bases",
    "users": DATA_DIR / "runtime" / "users",
    "logs": LOGS_DIR,
}


def create_backup(out_dir: Any = None) -> dict[str, Any]:
    """打包关键数据目录，返回 ``{path, bytes, entries}``。"""

    destination = Path(out_dir) if out_dir else Path(DATA_DIR) / "backups"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"

    entries = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for label, source in _BACKUP_SOURCES.items():
            if not source.exists():
                continue
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                try:
                    bundle.write(item, arcname=f"{label}/{item.relative_to(source)}")
                    entries += 1
                except OSError:
                    logger.debug("Skipped unreadable file during backup: %s", item, exc_info=True)
    return {"path": str(archive), "bytes": archive.stat().st_size, "entries": entries}