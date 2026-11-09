"""角色卡结构描述。

规则集插件通过 ``schema`` 声明自己的卡面字段；本模块只做「原样转述」，
不解释字段语义，也不假设任何具体规则集。核心与前端拿到的是一份可选的
字段清单，缺失时按空清单处理。
"""

from __future__ import annotations

from typing import Any

# 描述格式版本：前端据此判断能否理解该结构。
SCHEMA_DESCRIPTOR_VERSION = 1


def _read(schema: Any, name: str) -> Any:
    value = getattr(schema, name, None)
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001 - 插件声明异常时按空处理
            return None
    return value


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value]


def _slots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    slots: list[dict[str, Any]] = []
    for slot in value:
        if isinstance(slot, dict):
            slots.append(dict(slot))
    return slots


def descriptor_of(plugin: Any) -> dict[str, Any]:
    """把插件的 schema 声明转成前端可消费的描述；未声明时返回空描述。"""
    schema = getattr(plugin, "schema", None)
    ruleset_id = str(getattr(plugin, "id", "") or "")
    if schema is None:
        return {
            "descriptor_version": SCHEMA_DESCRIPTOR_VERSION,
            "ruleset_id": ruleset_id,
            "schema_version": int(getattr(plugin, "schema_version", 0) or 0),
            "envelope_fields": [],
            "sheet_fields": [],
            "attribute_keys": [],
            "resource_keys": [],
            "derived_keys": [],
            "status_body_keys": [],
            "status_mental_keys": [],
            "derived_card_keys": [],
            "resource_slots": [],
        }
    return {
        "descriptor_version": SCHEMA_DESCRIPTOR_VERSION,
        "ruleset_id": str(_read(schema, "ruleset_id") or ruleset_id),
        "schema_version": int(_read(schema, "schema_version") or 0),
        "envelope_fields": _strings(_read(schema, "envelope_fields")),
        "sheet_fields": _strings(_read(schema, "sheet_fields")),
        "attribute_keys": _strings(_read(schema, "attribute_keys")),
        "resource_keys": _strings(_read(schema, "resource_keys")),
        "derived_keys": _strings(_read(schema, "derived_keys")),
        "status_body_keys": _strings(_read(schema, "status_body_keys")),
        "status_mental_keys": _strings(_read(schema, "status_mental_keys")),
        "derived_card_keys": _strings(_read(schema, "derived_card_keys")),
        "resource_slots": _slots(_read(schema, "resource_slots")),
    }
