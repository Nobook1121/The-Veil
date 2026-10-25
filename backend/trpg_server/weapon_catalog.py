"""预设武器目录与角色卡自定义武器库的读写工具。

预设武器按分类存放在 ``data/weapons/builtin/<分类>.json``。每个文件包含
``category``/``order`` 元数据以及 ``weapons`` 数组；角色卡的自定义武器则
存放在该角色卡自己的目录里（``<角色卡目录>/weapons.json``）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic

CATEGORY_ORDER = {
    "常规": 10,
    "手枪": 20,
    "半自动步枪": 30,
    "全自动步枪": 40,
    "霰弹枪": 50,
    "冲锋枪": 60,
    "狙击步枪": 70,
    "机枪": 80,
    "其它": 90,
}
CUSTOM_CATEGORY = "自定义"
DEFAULT_CATEGORY = "常规"
MAX_CUSTOM_WEAPONS = 200
_WEAPON_ID_PATTERN = re.compile(r"[^0-9a-zA-Z_-]+")


def _as_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def category_order(category: str) -> int:
    if category == CUSTOM_CATEGORY:
        return 999
    return CATEGORY_ORDER.get(category, CATEGORY_ORDER[DEFAULT_CATEGORY])


def normalize_skill(raw: Any) -> dict[str, str]:
    skill = raw if isinstance(raw, dict) else {}
    label = _as_text(raw) if isinstance(raw, str) else _as_text(skill.get("label"))
    return {
        "skillKey": _as_text(skill.get("skillKey") or skill.get("skill") or skill.get("key")),
        "specialtyKey": _as_text(skill.get("specialtyKey") or skill.get("specialty")),
        "label": label,
    }


def normalize_weapon(raw: Any, *, category: str = DEFAULT_CATEGORY, weapon_id: str = "", order: int | None = None) -> dict[str, Any]:
    """把预设或自定义武器规范成前端目录使用的统一结构。"""
    weapon = raw if isinstance(raw, dict) else {}
    resolved_category = _as_text(weapon.get("category"), category) or category
    resolved_id = _as_text(weapon.get("id"), weapon_id) or weapon_id
    return {
        "id": resolved_id,
        "name": _as_text(weapon.get("name"), "未命名武器")[:60] or "未命名武器",
        "category": resolved_category,
        "categoryOrder": category_order(resolved_category) if order is None else order,
        "skill": normalize_skill(weapon.get("skill")),
        "damage": _as_text(weapon.get("damage"), "1D3") or "1D3",
        "attacks": _as_text(weapon.get("attacks"), "1") or "1",
        "impale": _as_bool(weapon.get("impale")),
        "range": _as_text(weapon.get("range"), "接触") or "接触",
        "ammo": _as_text(weapon.get("ammo"), "N/A") or "N/A",
        "malfunction": _as_text(weapon.get("malfunction"), "N/A") or "N/A",
        "eras": [_as_text(era) for era in _as_list(weapon.get("eras")) if _as_text(era)],
        "price": _as_text(weapon.get("price"), "N/A") or "N/A",
    }


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


def find_preset_weapon(weapons: list[dict[str, Any]], weapon_id_or_name: str) -> dict[str, Any] | None:
    key = _as_text(weapon_id_or_name)
    if not key:
        return None
    for weapon in weapons:
        if _as_text(weapon.get("id")) == key:
            return weapon
    normalized = key.lower()
    for weapon in weapons:
        if _as_text(weapon.get("name")).lower() == normalized:
            return weapon
    return None


def preset_to_character_weapon(preset: dict[str, Any]) -> dict[str, Any]:
    """把预设武器转换成角色卡武器行。"""
    skill = preset.get("skill") if isinstance(preset.get("skill"), dict) else {}
    return {
        "name": _as_text(preset.get("name")),
        "skill": _as_text(skill.get("label")),
        "skillKey": _as_text(skill.get("skillKey")),
        "specialtyKey": _as_text(skill.get("specialtyKey")),
        "damage": _as_text(preset.get("damage")),
        "range": _as_text(preset.get("range")),
        "impale": bool(preset.get("impale")),
        "attacks": _as_text(preset.get("attacks")),
        "ammo": _as_text(preset.get("ammo")),
        "malfunction": _as_text(preset.get("malfunction")),
        "weight": "",
        "note": "",
    }


def merge_character_weapon_row(row: Any, preset: dict[str, Any]) -> dict[str, Any]:
    """以预设为底，保留角色卡行里显式给出的覆盖值。"""
    base = preset_to_character_weapon(preset)
    if not isinstance(row, dict):
        return base
    aliases = {
        "skill": ("skill",),
        "skillKey": ("skillKey",),
        "specialtyKey": ("specialtyKey",),
        "damage": ("damage",),
        "range": ("range",),
        "attacks": ("attacks", "round"),
        "ammo": ("ammo", "num"),
        "malfunction": ("malfunction", "err"),
        "weight": ("weight",),
        "note": ("note",),
    }
    for field, keys in aliases.items():
        for key in keys:
            value = row.get(key)
            if value is not None and _as_text(value):
                base[field] = _as_text(value)
                break
    if isinstance(row.get("impale"), bool):
        base["impale"] = row["impale"]
    return base


def custom_weapons_path(character_dir: Path) -> Path:
    return character_dir / "weapons.json"


def read_custom_weapons(character_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(custom_weapons_path(character_dir), default=None)
    entries = payload.get("weapons") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return []
    weapons = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        weapon = normalize_weapon(entry, category=CUSTOM_CATEGORY)
        if not weapon["id"]:
            weapon["id"] = custom_weapon_id(weapon["name"], index)
        weapons.append(weapon)
    return weapons


def write_custom_weapons(character_dir: Path, weapons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    character_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    seen: set[str] = set()
    for index, entry in enumerate(weapons):
        if not isinstance(entry, dict):
            continue
        weapon = normalize_weapon(entry, category=CUSTOM_CATEGORY)
        base_id = weapon["id"] or custom_weapon_id(weapon["name"], index)
        weapon_id = base_id
        suffix = 2
        while weapon_id in seen:
            weapon_id = f"{base_id}-{suffix}"
            suffix += 1
        seen.add(weapon_id)
        weapon["id"] = weapon_id
        normalized.append(weapon)
        if len(normalized) >= MAX_CUSTOM_WEAPONS:
            break
    write_json_atomic(custom_weapons_path(character_dir), {"version": 1, "weapons": normalized})
    return normalized


def custom_weapon_id(name: str, index: int = 0) -> str:
    slug = _WEAPON_ID_PATTERN.sub("-", _as_text(name).lower()).strip("-")
    return f"custom-{slug or index}-{index}" if not slug else f"custom-{slug}"


def upsert_custom_weapons(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 id 或名称合并自定义武器，返回完整库。"""
    merged = [dict(item) for item in existing]
    for entry in additions:
        if not isinstance(entry, dict):
            continue
        weapon = normalize_weapon(entry, category=CUSTOM_CATEGORY)
        if not weapon["name"] or weapon["name"] == "未命名武器":
            continue
        match = None
        if weapon["id"]:
            match = next((item for item in merged if _as_text(item.get("id")) == weapon["id"]), None)
        if match is None:
            match = next((item for item in merged if _as_text(item.get("name")) == weapon["name"]), None)
        if match is None:
            weapon["id"] = weapon["id"] or custom_weapon_id(weapon["name"], len(merged))
            merged.append(weapon)
        else:
            merged[merged.index(match)] = {**match, **weapon, "id": match.get("id") or weapon["id"]}
    return merged
