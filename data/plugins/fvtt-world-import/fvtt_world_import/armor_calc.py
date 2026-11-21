"""AC 装备重算小引擎（FVTT dnd5e 的 AC 字段是「算法选择」，不是数值）。

决策树（映射表 §2.6，两包真实样本对过账）：

1. ``ac.flat`` 是数字 → 直接用（``calc`` 不再关心；作者手填意图优先，不回退
   不告警——BAR flat=15 与其无甲防御推导 16 不一致，仍按 15 走）；
2. ``ac.flat`` 为空 → 从已装备护甲重算：

   - 取 ``items[type=equipment]`` 中 ``system.equipped == true`` 的护甲
     （``type.value ∈ {light, medium, heavy}``）与盾牌（``shield``，每面
     ``armor.value``（缺省 2）相加）；
   - 护甲 AC = ``armor.value + magicalBonus + 敏捷加值``，敏捷加值按类型
     （light 不限 / medium 上限 +2 / heavy 敏捷完全不参与——PHB 重甲规则，
     链甲+盾的力量 8 圣武士仍是 18，不是 17）；``armor.dex`` 为数字表示
     显式上限（塞巴斯蒂安的板条甲 +1 是 ``{value:17, dex:0,
     magicalBonus:"1"}``，秘银甲 ``{value:17, dex:null}`` + heavy → AC 17）；
   - 多件护甲取算出值最高者（样本无多件实例，防御分支）；
   - 无任何已装备护甲 → ``10 + 敏捷调整值`` + 警告（按无甲推导）。

3. 样本外取值（``formula`` 非空等）→ 落到 2 的无甲分支并出警告。

``magicalBonus`` 可能是字符串（"1"）；非数字公式串按 0 计并出警告，不引入
FVTT 公式求值。
"""

from __future__ import annotations

from typing import Any, Mapping

ARMOR_TYPES = ("light", "medium", "heavy")
#: 类型 → 敏捷加值上限；``armor.dex`` 为 null 时按此表，数字则为显式上限。
DEX_CAPS: dict[str, int | None] = {"light": None, "medium": 2, "heavy": 0}
DEFAULT_SHIELD_BONUS = 2

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")


def ability_modifier(score: Any) -> int:
    """属性值 → 调整值（缺省按 10）。"""
    try:
        value = int(score)
    except (TypeError, ValueError):
        value = 10
    return (max(1, min(30, value)) - 10) // 2


def actor_dexterity(actor: Mapping[str, Any]) -> int:
    system = actor.get("system") if isinstance(actor.get("system"), Mapping) else {}
    abilities = system.get("abilities") if isinstance(system.get("abilities"), Mapping) else {}
    dex = abilities.get("dex") if isinstance(abilities.get("dex"), Mapping) else {}
    try:
        return int(dex.get("value"))
    except (TypeError, ValueError):
        return 10


def _equipped_armors(items: list[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """已装备的护甲/盾牌条目：yield (item_doc, system)。"""
    found: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for item in items or []:
        if not isinstance(item, Mapping) or item.get("type") != "equipment":
            continue
        system = item.get("system") if isinstance(item.get("system"), Mapping) else {}
        if not system.get("equipped"):
            continue
        item_type = system.get("type") if isinstance(system.get("type"), Mapping) else {}
        kind = str(item_type.get("value") or "").strip().lower()
        if kind in ARMOR_TYPES or kind == "shield":
            found.append((item, system))
    return found


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def armor_class(
    actor: Mapping[str, Any],
    items: list[Mapping[str, Any]],
) -> tuple[int, str, list[tuple[str, str]]]:
    """返回 ``(AC, 来源说明, [(warning_code, text), ...])``。

    纯函数：只消费 actor 文档与已聚合的内嵌条目，不落任何数据。
    """
    warnings: list[tuple[str, str]] = []
    system = actor.get("system") if isinstance(actor.get("system"), Mapping) else {}
    attributes = system.get("attributes") if isinstance(system.get("attributes"), Mapping) else {}
    ac = attributes.get("ac") if isinstance(attributes.get("ac"), Mapping) else {}
    calc = str(ac.get("calc") or "").strip().lower()
    formula = str(ac.get("formula") or "").strip()
    if formula:
        warnings.append(
            (
                "ac.formula_ignored",
                f"AC 计算式「{formula[:60]}」无法求值，按护甲重算处理。",
            )
        )
    elif calc not in {"default", "natural"} and calc:
        warnings.append(("ac.calc_unsupported", f"AC 算法「{calc}」不在支持列表，按护甲重算处理。"))

    flat = _int_or_none(ac.get("flat"))
    if flat is not None:
        return flat, "卡面直接填写", warnings

    dex_mod = ability_modifier(actor_dexterity(actor))
    best: tuple[int, str] | None = None
    shield_total = 0
    shield_names: list[str] = []
    for item, item_system in _equipped_armors(items):
        armor = item_system.get("armor") if isinstance(item_system.get("armor"), Mapping) else {}
        item_type = item_system.get("type") if isinstance(item_system.get("type"), Mapping) else {}
        kind = str(item_type.get("value") or "").strip().lower()
        name = str(item.get("name") or "护甲")
        base = _int_or_none(armor.get("value"))
        bonus = _int_or_none(armor.get("magicalBonus"))
        if armor.get("magicalBonus") not in (None, "") and bonus is None:
            bonus = 0
            warnings.append(
                (
                    "ac.magical_bonus_formula",
                    f"「{name}」的魔法加值「{armor.get('magicalBonus')}」不是数字，按 0 处理。",
                )
            )
        if kind == "shield":
            value = base if base is not None else DEFAULT_SHIELD_BONUS
            shield_total += value
            shield_names.append(name)
            continue
        if base is None:
            warnings.append(("ac.armor_value_missing", f"「{name}」没有护甲值，跳过。"))
            continue
        cap = _int_or_none(armor.get("dex"))
        if cap is None:
            cap = DEX_CAPS.get(kind)
        if cap is None:
            # light 护甲（或类型表未声明上限）：敏捷调整值全量参与，负值同样计入。
            dex_part = dex_mod
        elif cap <= 0:
            # heavy（显式 0 或类型表默认）：敏捷完全不参与（PHB；PAL 链甲 dex8 → 18）。
            dex_part = 0
        else:
            # medium 等正上限：RAW 只封顶加值，负敏捷调整值仍照常计入。
            dex_part = min(dex_mod, cap)
        total = base + (bonus or 0) + dex_part
        note = f"{name}（{kind} {base}" + (f"+{bonus}" if bonus else "") + f"，敏捷{dex_part:+d}）"
        if best is None or total > best[0]:
            best = (total, note)
    if best is not None:
        total = best[0] + shield_total
        parts = [best[1]]
        if shield_names:
            parts.append(f"盾牌 +{shield_total}（{'、'.join(shield_names)}）")
        return total, "，".join(parts), warnings

    warnings.append(
        (
            "ac.unarmored",
            "没有已装备护甲，AC 按无甲（10 + 敏捷调整值）推导。",
        )
    )
    return 10 + dex_mod, "无甲推导（10 + 敏捷调整值）", warnings


__all__ = ["armor_class", "ability_modifier", "actor_dexterity", "DEX_CAPS"]
