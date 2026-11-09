"""规则集可选能力的访问层。

核心不假设插件一定提供 ``schema`` / ``catalogs`` / ``checks``：这里统一做
"能力存在就调用、不存在就用中性值"的降级，避免删除某个插件或换成只有元数据的
占位插件时出现 ``AttributeError``。

降级原则：

- 角色卡：无法打包/派生时按原样返回，读路径不丢数据、写路径不破坏结构。
- 目录：无法提供时返回空列表，调用方自行展示"该规则集暂无目录"。
- 状态槽位：插件可声明自己的资源键；未声明时使用核心默认槽位。
"""

from __future__ import annotations

from typing import Any, Mapping

# 默认派生键：插件未声明时按历史 CoC 口径处理，保证旧卡行为不变。
DEFAULT_DERIVED_CARD_KEYS: tuple[str, ...] = (
    "maxHp",
    "maxSan",
    "initialSan",
    "maxMp",
    "mov",
    "build",
    "damageBonus",
    "armor",
)
# 默认运行时资源槽位：kind → 快照键 / 卡面镜像键 / 上限来源。
DEFAULT_RESOURCE_SLOTS: tuple[dict[str, Any], ...] = (
    {
        "kind": "hp",
        "state": "current_hp",
        "mirror": "currentHp",
        "cap_state": "max_hp",
        "cap_derived": "maxHp",
        "cap_min": 1,
    },
    {
        "kind": "san",
        "state": "current_san",
        "mirror": "currentSan",
        "cap_state": "max_san",
        "cap_derived": "maxSan",
        "cap_min": 0,
    },
    {
        "kind": "mp",
        "state": "current_mp",
        "mirror": "currentMp",
        "cap_state": "max_mp",
        "cap_derived": "maxMp",
        "cap_min": 0,
    },
)


def schema_of(plugin: Any) -> Any | None:
    return getattr(plugin, "schema", None)


def checks_of(plugin: Any) -> Any | None:
    return getattr(plugin, "checks", None)


def sanity_of(plugin: Any) -> Any | None:
    """插件的理智 / 疯狂能力；未声明时返回 None。"""
    return getattr(plugin, "sanity", None)


def catalogs_of(plugin: Any) -> Any | None:
    return getattr(plugin, "catalogs", None)


def tools_of(plugin: Any) -> Any | None:
    return getattr(plugin, "tools", None)


def pack_card(plugin: Any, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """按插件提供的 schema 打包；无 schema 时原样返回。"""
    pack = getattr(schema_of(plugin), "pack", None)
    if callable(pack):
        return dict(pack(dict(card or {})) or {})
    return dict(card or {})


def derive_card(plugin: Any, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """按插件提供的 schema 派生；无 schema 时原样返回。"""
    derive = getattr(schema_of(plugin), "derive", None)
    if callable(derive):
        return dict(derive(dict(card or {})) or {})
    return dict(card or {})


def catalog_value(plugin: Any, name: str, default: Any = None) -> Any:
    """读取插件目录原始值；缺失时返回 ``default``（目录可能是列表或字典）。"""
    provider = getattr(catalogs_of(plugin), name, None)
    if not callable(provider):
        return default
    return provider()


def catalog_items(plugin: Any, name: str) -> list[Any]:
    """读取插件目录项（skills / weapons / occupations / items）；缺失时为空。"""
    items = catalog_value(plugin, name, [])
    if isinstance(items, dict):
        return []
    return list(items or [])


def derived_card_keys(plugin: Any) -> tuple[str, ...]:
    declared = getattr(schema_of(plugin), "derived_card_keys", None)
    if callable(declared):
        keys = tuple(str(key) for key in declared())
        if keys:
            return keys
    return DEFAULT_DERIVED_CARD_KEYS


def resource_slots(plugin: Any) -> tuple[dict[str, Any], ...]:
    declared = getattr(schema_of(plugin), "resource_slots", None)
    if callable(declared):
        slots = tuple(dict(slot) for slot in declared())
        if slots:
            return slots
    return DEFAULT_RESOURCE_SLOTS
