"""应用内一键更新：暂存、校验、替换与重启的底层实现。

设计约束（对应 update_service 模块头注释里「不自动替换」的旧结论）：

- **不在请求线程里覆盖正在运行的程序文件**。下载完成后只做「解压 + 校验 +
  写 pending」，真正的替换交给进程退出后的独立助手（``scripts/apply_update.py``），
  或下一次启动时的早鸟钩子。
- 替换只写程序文件：``data/``、``.venv/``、``_backup/``、``.git/`` 一律不动。
- 任何一步失败都不留下半更新状态：暂存目录可重试，pending 文件最后才写。
- 本模块只依赖标准库，``server.py`` 可以在导入 Flask 之前调用它。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 更新时永远不覆盖的顶层目录（用户数据、虚拟环境、备份、版本库与缓存）。
PROTECTED_DIRS = {"data", ".venv", "venv", "_backup", ".git", "node_modules", "__pycache__"}

#: 解压后必须存在的文件（少一个就判定发布包不完整）。
REQUIRED_FILES = (
    "server.py",
    "start.cmd",
    "backend/trpg_server/version.py",
    "dist/public/index.html",
    "config/templates/general.toml",
)

_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*"([^"]+)"')


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pending_path(update_dir: Path) -> Path:
    return Path(update_dir) / "pending.json"


def staging_path(update_dir: Path, version: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(version or "")) or "latest"
    return Path(update_dir) / "staging" / safe


def read_version(version_file: Path) -> str:
    text = Path(version_file).read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    """拒绝绝对路径与 ``..``，防止发布包写出目标目录。"""
    root = target.resolve()
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"发布包含有非法路径：{member.filename}")
    archive.extractall(target)


def stage_zip(zip_path: Path, update_dir: Path, expected_version: str) -> Path:
    """把发布包解压到暂存目录并校验版本/结构；返回暂存根目录。"""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"更新包不存在：{zip_path}")
    target = staging_path(update_dir, expected_version)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract(archive, target)
    entries = [item for item in target.iterdir() if item.is_dir()]
    staged = entries[0] if len(entries) == 1 and not (target / "server.py").exists() else target
    version = read_version(staged / "backend/trpg_server/version.py")
    if expected_version and version and version != expected_version:
        raise ValueError(f"发布包版本 {version} 与目标版本 {expected_version} 不一致")
    missing = [name for name in REQUIRED_FILES if not (staged / name).exists()]
    if missing:
        raise ValueError("发布包缺少必需文件：" + "、".join(missing))
    return staged


def write_pending(update_dir: Path, staged: Path, version: str, *, notes: str = "") -> Path:
    payload: dict[str, Any] = {
        "version": version,
        "staging": str(staged),
        "created_at": _now(),
        "notes": notes,
    }
    path = pending_path(update_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_pending(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("staging"):
        return None
    return data


def _copy_tree(source: Path, target: Path) -> int:
    """把暂存目录的程序文件覆盖到安装目录；返回复制的文件数。"""
    copied = 0
    source = Path(source)
    target = Path(target)
    for item in source.iterdir():
        if item.name in PROTECTED_DIRS:
            continue
        destination = target / item.name
        if item.is_dir():
            for current_root, dirs, files in os.walk(item):
                dirs[:] = [name for name in dirs if name not in PROTECTED_DIRS]
                relative = Path(current_root).relative_to(source)
                (target / relative).mkdir(parents=True, exist_ok=True)
                for name in files:
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    src = Path(current_root) / name
                    dst = target / relative / name
                    shutil.copy2(src, dst)
                    copied += 1
        else:
            shutil.copy2(item, destination)
            copied += 1
    return copied


def apply_pending(path: Path, *, root: Path, cleanup: bool = True) -> dict[str, Any]:
    """执行 pending 里的替换；成功后清掉 pending 与暂存目录。"""
    data = read_pending(path)
    if not data:
        return {"applied": False, "reason": "no-pending"}
    staged = Path(str(data.get("staging")))
    if not staged.exists():
        Path(path).unlink(missing_ok=True)
        return {"applied": False, "reason": "staging-missing"}
    expected = str(data.get("version") or "")
    actual = read_version(staged / "backend/trpg_server/version.py")
    if expected and actual and expected != actual:
        raise ValueError(f"暂存版本 {actual} 与 pending 版本 {expected} 不一致")
    copied = _copy_tree(staged, root)
    # 更新完成后写入构建标记，关于页/日志能看出当前跑的是哪个版本。
    try:
        manifest = root / "backend/trpg_server/build_manifest.json"
        if not manifest.exists():
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {"version": actual, "mode": "updated", "updated_at": _now()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    except OSError:
        pass
    if cleanup:
        shutil.rmtree(staged.parent, ignore_errors=True)
        Path(path).unlink(missing_ok=True)
    return {"applied": True, "version": actual, "files": copied}
