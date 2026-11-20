"""CoC7 预设武器目录读取器（规则集边界收敛 D2）。

CoC 规则书武器表（表 XVII）的目录布局与分类排序归本插件所有；行结构规范
（``normalize_weapon``）是跨规则集的前端行契约，复用核心
``weapon_catalog`` 的实现，不再让核心反向持有 CoC 目录读取器。

数据位置：优先 ``data/rulesets/coc7/catalogs/weapons/builtin/<分类>.json``
（路径解析在 ``catalogs.Coc7Catalogs._weapons_dir``）；老安装仍可用旧位置
``data/weapons/``（兼容回退，清理期限见 ``catalogs.py``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json
from trpg_server.weapon_catalog import (
    DEFAULT_CATEGORY,
    category_order,
    normalize_weapon,
)


def _as_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip()


def load_preset_weapons(weapons_dir: Path) -> list[dict[str, Any]]:
    """读取全部预设武器，旧版“单武器一个文件”的格式也兼容。"""
    builtin_dir = weapons_dir / "builtin"
    if not builtin_dir.exists():
        return []
    weapons: list[dict[str, Any]] = []
    for path in sorted(builtin_dir.glob("*.json")):
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            continue
        category = _as_text(payload.get("category"), DEFAULT_CATEGORY) or DEFAULT_CATEGORY
        order = payload.get("order")
        try:
            order_value = int(order) if order is not None else category_order(category)
        except (TypeError, ValueError):
            order_value = category_order(category)
        entries = payload.get("weapons")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    weapons.append(normalize_weapon(entry, category=category, order=order_value))
            continue
        # 旧格式：文件本身就是一把武器
        fallback_id = path.stem
        weapons.append(normalize_weapon(payload, category=category, weapon_id=fallback_id, order=order_value))
    weapons.sort(key=lambda item: (int(item.get("categoryOrder") or 0), str(item.get("name") or "")))
    return weapons
