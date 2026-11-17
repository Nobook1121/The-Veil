"""角色卡结构描述。

规则集插件通过 ``schema`` 声明自己的卡面字段；本模块只做「原样转述」，
不解释字段语义，也不假设任何具体规则集。核心与前端拿到的是一份可选的
字段清单，缺失时按空清单处理。

v2 在旧的平铺键之外增加 ``sections/fields/status_groups`` 与
``resources/conditions/progression/ui``；旧键继续保留，现有前端读取不受影响。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.content import CARD_DESCRIPTOR_VERSION
from trpg_server.ruleset.content import character_card_descriptor

# 描述格式版本：前端据此判断能否理解该结构。
SCHEMA_DESCRIPTOR_VERSION = CARD_DESCRIPTOR_VERSION


def descriptor_of(plugin: Any) -> dict[str, Any]:
    """把插件的 schema 声明转成前端可消费的描述；未声明时返回空描述。"""
    return character_card_descriptor(plugin).to_dict()


def dice_of(plugin: Any) -> dict[str, Any]:
    """把插件的骰型声明原样转述给前端；未声明时返回空对象。

    核心不判断 d20 / d100，也不决定优势劣势的语义；前端只按这里给出的
    ``type`` / ``expression`` / ``critical`` / ``fumble`` 渲染骰桌。
    """
    raw = getattr(plugin, "dice", None)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("type", "expression"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value
    for key in ("sides",):
        try:
            sides = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            sides = 0
        if sides > 0:
            result[key] = sides
    modes = raw.get("modes")
    if isinstance(modes, (list, tuple)):
        result["modes"] = [str(item) for item in modes if str(item or "").strip()]
    for key in ("critical", "fumble"):
        values = raw.get(key)
        if isinstance(values, (list, tuple)):
            faces: list[int] = []
            for item in values:
                try:
                    faces.append(int(item))
                except (TypeError, ValueError):
                    continue
            if faces:
                result[key] = faces
    return result


__all__ = ["SCHEMA_DESCRIPTOR_VERSION", "descriptor_of", "dice_of"]
