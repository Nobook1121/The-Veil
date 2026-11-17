"""5e 内容检索的消歧层：复用核心内容包查找，补充 5e 区分字段。

核心只做通用名称/id/别名匹配与来源排序；本模块负责把 5e 的环阶、CR、
装备类别等展示信息附加到候选项上，避免核心出现 5e 词表。
"""

from __future__ import annotations

from typing import Any

from trpg_server.plugins import runtime

TYPE_ALIASES = {
    "spell": "spell",
    "spells": "spell",
    "法术": "spell",
    "monster": "monster",
    "monsters": "monster",
    "怪物": "monster",
    "npc": "monster",
    "equipment": "equipment",
    "item": "equipment",
    "items": "equipment",
    "装备": "equipment",
    "物品": "equipment",
}
TYPE_LABELS = {
    "spell": "法术",
    "monster": "怪物",
    "equipment": "装备",
}
SCHOOL_LABELS = {
    "abjuration": "防护",
    "conjuration": "咒法",
    "divination": "预言",
    "enchantment": "附魔",
    "evocation": "塑能",
    "illusion": "幻术",
    "necromancy": "死灵",
    "transmutation": "变化",
}
SIZE_LABELS = {
    "tiny": "微型",
    "small": "小型",
    "medium": "中型",
    "large": "大型",
    "huge": "巨型",
    "gargantuan": "超巨型",
}
CATEGORY_LABELS = {
    "weapon": "武器",
    "armor": "护甲",
    "shield": "盾牌",
    "adventuring_gear": "冒险装备",
    "tools": "工具",
    "mount": "坐骑",
    "vehicle": "载具",
    "trade_good": "贸易品",
}
WEAPON_CATEGORY_LABELS = {
    "simple": "简易武器",
    "martial": "军用武器",
}


def _localized(entry: dict[str, Any]) -> str:
    locales = entry.get("locales") if isinstance(entry.get("locales"), dict) else {}
    zh = locales.get("zh-CN") if isinstance(locales.get("zh-CN"), dict) else {}
    return str(zh.get("name") or "").strip()


def _label(entry: dict[str, Any]) -> str:
    primary = str(entry.get("name") or entry.get("id") or "")
    zh = _localized(entry)
    if zh and zh.casefold() != primary.casefold():
        return f"{primary}（{zh}）"
    return primary


def _spell_detail(entry: dict[str, Any]) -> dict[str, str]:
    level = int(entry.get("level") or 0)
    school = str(entry.get("school") or "")
    classes = ", ".join(str(item) for item in entry.get("classes") or [])
    detail = {
        "环阶": "戏法" if level <= 0 else f"{level} 环",
        "学派": SCHOOL_LABELS.get(school, school),
    }
    if classes:
        detail["职业"] = classes
    return {key: value for key, value in detail.items() if value}


def _monster_detail(entry: dict[str, Any]) -> dict[str, str]:
    monster_type = str(entry.get("type") or "")
    subtype = str(entry.get("subtype") or "")
    size = str(entry.get("size") or "")
    return {
        "CR": str(entry.get("cr") or ""),
        "类型": f"{monster_type}（{subtype}）" if subtype else monster_type,
        "体型": SIZE_LABELS.get(size, size),
    }


def _equipment_detail(entry: dict[str, Any]) -> dict[str, str]:
    category = str(entry.get("category") or "")
    weapon_category = str(entry.get("weapon_category") or "")
    damage = entry.get("damage") if isinstance(entry.get("damage"), dict) else {}
    dice = str(damage.get("dice") or "")
    dtype = str(damage.get("type") or "")
    detail = {
        "类别": CATEGORY_LABELS.get(category, category),
        "武器类别": WEAPON_CATEGORY_LABELS.get(weapon_category, weapon_category),
        "伤害": f"{dice} {dtype}".strip(),
        "类型": str(entry.get("kind") or ""),
    }
    return {key: value for key, value in detail.items() if value}


_DETAIL_BUILDERS = {
    "spell": _spell_detail,
    "monster": _monster_detail,
    "equipment": _equipment_detail,
}


class Dnd5eContentSearch:
    """5e 内容检索能力：供核心 ``content.search`` 工具与插件 HTTP 接口使用。"""

    def types(self) -> list[str]:
        return list(TYPE_LABELS)

    def resolve_type(self, type_name: Any) -> str:
        key = TYPE_ALIASES.get(str(type_name or "").strip().lower())
        if key is None:
            raise ValueError(
                "不支持的内容类型；可用类型：" + "、".join(TYPE_LABELS.values()) + "。"
            )
        return key

    def search(self, type_name: Any, query: Any, *, limit: int = 10) -> dict[str, Any]:
        key = self.resolve_type(type_name)
        text = str(query or "").strip()
        if not text:
            raise ValueError("检索内容不能为空。")
        library = runtime.content_packs_for("dnd5e")
        result = library.lookup("dnd5e", key, text, limit=limit)
        builder = _DETAIL_BUILDERS[key]
        for match in result.get("matches") or []:
            entry = library.get("dnd5e", key, match.get("id")) or {}
            if isinstance(entry, dict) and entry:
                match["label"] = _label(entry)
                match["distinguish"] = builder(entry)
                match["source_text"] = str(entry.get("source") or "")
            else:
                match["label"] = str(match.get("name") or match.get("id") or "")
                match["distinguish"] = {}
                match["source_text"] = str((match.get("pack") or {}).get("edition") or "")
        result["type_label"] = TYPE_LABELS.get(key, key)
        result["types"] = self.types()
        return result
