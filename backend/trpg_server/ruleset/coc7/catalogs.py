"""CoC7 的技能 / 武器 / 职业目录。

优先读 ``data/rulesets/coc7/catalogs/``；不存在时回退到项目原有位置，
保证不搬迁数据也能正常工作。路由层的 app.config 覆盖仍然生效（测试会注入临时路径）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server import weapon_catalog
from trpg_server.json_store import read_json
from trpg_server.settings import (
    CHARACTER_SKILLS_FILE,
    DATA_DIR,
    OCCUPATIONS_DIR,
    WEAPONS_DIR,
)

logger = logging.getLogger(__name__)

RULESET_CATALOG_DIR = DATA_DIR / "rulesets" / "coc7" / "catalogs"


def _app_override(key: str, fallback: Path) -> Path:
    """优先使用 Flask app 配置里的路径（测试注入），无上下文时回落默认。"""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return Path(current_app.config.get(key, fallback))
    except Exception:  # pragma: no cover - Flask 未安装/无上下文时走默认
        pass
    return Path(fallback)


class Coc7Catalogs:
    """目录提供者；返回结构与现有前端渲染完全一致。"""

    ruleset_id = "coc7"

    def __init__(self, catalogs_dir: Path | None = None) -> None:
        self.catalogs_dir = Path(catalogs_dir) if catalogs_dir else RULESET_CATALOG_DIR

    # -- 路径解析 ---------------------------------------------------------
    def _skills_path(self) -> Path:
        plugin_path = self.catalogs_dir / "skills.json"
        if plugin_path.exists():
            return plugin_path
        return _app_override("CHARACTER_SKILLS_FILE", CHARACTER_SKILLS_FILE)

    def _weapons_dir(self) -> Path:
        plugin_path = self.catalogs_dir / "weapons"
        if plugin_path.exists():
            return plugin_path
        return _app_override("WEAPONS_DIR", WEAPONS_DIR)

    def _occupations_dir(self) -> Path:
        plugin_path = self.catalogs_dir / "occupations"
        if plugin_path.exists():
            return plugin_path
        return _app_override("OCCUPATIONS_DIR", OCCUPATIONS_DIR)

    # -- 目录 -------------------------------------------------------------
    def skills(self) -> dict[str, Any]:
        catalog = read_json(self._skills_path(), default={})
        if not isinstance(catalog, dict):
            catalog = {}
        if not isinstance(catalog.get("skills"), list):
            catalog["skills"] = []
        if not isinstance(catalog.get("locales"), dict):
            catalog["locales"] = {}
        catalog.setdefault("version", 1)
        catalog.setdefault("defaultLocale", "zh-CN")
        return catalog

    def weapons(self) -> list[dict[str, Any]]:
        try:
            return weapon_catalog.load_preset_weapons(self._weapons_dir())
        except OSError:
            logger.exception("Failed to load CoC7 weapon catalog")
            return []

    def occupations(self) -> list[dict[str, Any]]:
        directory = self._occupations_dir() / "builtin"
        if not directory.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            occupation = read_json(path, default=None)
            if isinstance(occupation, dict):
                items.append(occupation)
        items.sort(key=lambda item: str(item.get("id") or item.get("nameKey") or ""))
        return items

    def items(self) -> list[dict[str, Any]]:
        # CoC7 物品目前是角色卡上的自由文本（equipment），暂无独立目录。
        catalog = read_json(self.catalogs_dir / "items.json", default=[])
        return catalog if isinstance(catalog, list) else []
