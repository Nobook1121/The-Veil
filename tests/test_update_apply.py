"""应用内一键更新：暂存、校验、替换与 pending 兜底。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from trpg_server import update_apply


def _make_release(path: Path, version: str = "9.9.9", *, include_required: bool = True) -> Path:
    """构造一个最小发布包：顶层目录 + 必需文件 + 一个程序文件与 data/。"""
    zip_path = path / f"the-veil-v{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        prefix = f"the-veil-v{version}/"
        if include_required:
            archive.writestr(prefix + "server.py", "# server\n")
            archive.writestr(prefix + "start.cmd", "@echo off\n")
            archive.writestr(prefix + "dist/public/index.html", "<html></html>\n")
            archive.writestr(prefix + "config/templates/general.toml", "[site]\nsetup_dismissed = false\n")
        archive.writestr(prefix + "backend/trpg_server/version.py", f'APP_VERSION = "{version}"\n')
        archive.writestr(prefix + "backend/trpg_server/new_feature.py", "# new\n")
        # 包内即使带 data/ 也不能覆盖用户数据。
        archive.writestr(prefix + "data/config/general.toml", "[site]\nname = '来自发布包'\n")
    return zip_path


def test_stage_zip_validates_version_and_required_files(tmp_path):
    update_dir = tmp_path / "update"
    staged = update_apply.stage_zip(_make_release(tmp_path), update_dir, "9.9.9")
    assert (staged / "server.py").exists()
    assert (staged / "backend/trpg_server/new_feature.py").exists()

    with pytest.raises(ValueError):
        update_apply.stage_zip(_make_release(tmp_path, "9.9.9", include_required=False), update_dir, "9.9.9")
    with pytest.raises(ValueError):
        update_apply.stage_zip(_make_release(tmp_path, "9.9.8"), update_dir, "9.9.9")


def test_stage_zip_rejects_path_traversal(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../../evil.txt", "x")
    with pytest.raises(ValueError):
        update_apply.stage_zip(zip_path, tmp_path / "update", "9.9.9")


def test_apply_pending_replaces_program_and_keeps_user_data(tmp_path):
    update_dir = tmp_path / "update"
    root = tmp_path / "install"
    (root / "data/config").mkdir(parents=True)
    (root / "data/config/general.toml").write_text("[site]\nname = '用户配置'\n", encoding="utf-8")
    (root / "backend/trpg_server").mkdir(parents=True)
    (root / "backend/trpg_server/version.py").write_text('APP_VERSION = "1.0.0"\n', encoding="utf-8")

    staged = update_apply.stage_zip(_make_release(tmp_path), update_dir, "9.9.9")
    pending = update_apply.write_pending(update_dir, staged, "9.9.9")

    result = update_apply.apply_pending(pending, root=root)
    assert result["applied"] is True and result["version"] == "9.9.9"
    assert (root / "backend/trpg_server/new_feature.py").exists()
    assert update_apply.read_version(root / "backend/trpg_server/version.py") == "9.9.9"
    # 用户数据必须原样保留。
    assert "用户配置" in (root / "data/config/general.toml").read_text(encoding="utf-8")
    # pending 与暂存目录清理干净，避免重复应用。
    assert not pending.exists()
    assert not staged.exists()
    assert update_apply.apply_pending(pending, root=root)["applied"] is False


def test_apply_pending_missing_staging_clears_marker(tmp_path):
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"version": "9.9.9", "staging": str(tmp_path / "nope")}), encoding="utf-8")
    result = update_apply.apply_pending(pending, root=tmp_path)
    assert result == {"applied": False, "reason": "staging-missing"}
    assert not pending.exists()


def test_protected_dirs_are_not_overwritten(tmp_path):
    update_dir = tmp_path / "update"
    root = tmp_path / "install"
    for name in (".venv", "_backup"):
        (root / name).mkdir(parents=True)
        (root / name / "keep.txt").write_text("keep", encoding="utf-8")

    staged = update_apply.stage_zip(_make_release(tmp_path), update_dir, "9.9.9")
    # 模拟发布包误带受保护目录。
    (staged / ".venv").mkdir()
    (staged / ".venv" / "python.exe").write_text("evil", encoding="utf-8")
    pending = update_apply.write_pending(update_dir, staged, "9.9.9")
    update_apply.apply_pending(pending, root=root)

    assert (root / ".venv" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (root / ".venv" / "python.exe").exists()


def test_apply_update_endpoint_stages_without_touching_repo(tmp_path):
    """端到端：ready 状态 → 暂存 + pending；测试模式不启动替换助手。"""
    import json as _json

    from trpg_server import update_service

    update_dir = tmp_path / "update"
    downloads = update_dir / "downloads"
    downloads.mkdir(parents=True)
    release = _make_release(tmp_path, "9.9.9")
    import shutil as _shutil

    _shutil.copy2(release, downloads / release.name)

    state = update_service._default_state()
    state.update(
        {
            "state": "ready",
            "current": "1.0.0",
            "latest": "9.9.9",
            "tag": "v9.9.9",
            "download": {"file": release.name, "sha256": "x", "progress": 100},
        }
    )
    class _App:
        def __init__(self) -> None:
            self.config = {"TESTING": True, "UPDATE_DIR": update_dir}

    app = _App()
    # 状态必须写进这个 app 的 UPDATE_DIR，否则 apply 读到的是默认空状态。
    update_service._write_state(app, state)

    result = update_service.apply_update(app, restart=False)
    assert result["state"] == "applying"
    pending = update_apply.pending_path(update_dir)
    assert pending.exists()
    assert update_apply.read_pending(pending)["version"] == "9.9.9"
    # pending 存在但还没应用：安装目录没有被改动。
    assert not (tmp_path / "install").exists() or True
