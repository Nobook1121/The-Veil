"""CoC7 法术 / 典籍内容表：惰性加载与按需查询。

数据来自《克苏鲁的呼唤》第七版守秘人规则书：
- ``content/spells.json``  第十二章「法术」（书页 209–230）；
- ``content/grimoires.json`` 第十一章「可怖传说书籍」正文 + 表Ⅺ（书页 190–203）。

设计约定：
- 只有本插件认识这些内容，核心与房间状态层不引用；
- 整张表**不进**系统提示或工具描述，调用方按名称 / 关键词查询单条；
- 返回结构保持精简（摘要 + 关键数值），``detail=True`` 才带正文节选；
- 文件改动后按 mtime 自动重载，便于 KP 自行订正数值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

CONTENT_DIR = Path(__file__).resolve().parent / "content"
SPELL_FILE = "spells.json"
GRIMOIRE_FILE = "grimoires.json"
SOURCE_SPELL = "守秘人规则书 第十二章"
SOURCE_GRIMOIRE = "守秘人规则书 第十一章 / 表Ⅺ"

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _load(filename: str) -> list[dict[str, Any]]:
    path = CONTENT_DIR / filename
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    cached = _cache.get(filename)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data if isinstance(data, list) else []
    _cache[filename] = (stamp, entries)
    return entries


def spells() -> list[dict[str, Any]]:
    return _load(SPELL_FILE)


def grimoires() -> list[dict[str, Any]]:
    return _load(GRIMOIRE_FILE)


def catalog_stats() -> dict[str, int]:
    return {"spells": len(spells()), "grimoires": len(grimoires())}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


_FIELD_LABEL_RE = re.compile(
    r"^\s*(?:消耗|施法用时|别名|理智损失|克苏鲁神话|神话等级|学时|建议法术|神秘学|复本)"
    r"\s*[：:]\s*"
)


def _strip_label(value: Any) -> str:
    """去掉字段名前缀，展示层只保留值本身。"""
    return _FIELD_LABEL_RE.sub("", str(value or "")).strip()


def _score(query: str, *fields: Any) -> int:
    """名称匹配打分：完全一致 > 前缀 > 包含；返回 0 表示不匹配。"""
    if not query:
        return 1
    best = 0
    for field in fields:
        name = _norm(field)
        if not name:
            continue
        if name == query:
            best = max(best, 100)
        elif name.startswith(query):
            best = max(best, 80)
        elif query in name:
            best = max(best, 60)
    return best


DICE_RE = re.compile(r"(\d+)D(\d+)", re.IGNORECASE)


def dice_ceiling(text: Any) -> int | None:
    """把 "2D6" / "1D4+3" / "8" / "无" 折算成最大可能值；无法判断返回 None。"""
    raw = _norm(text)
    if not raw:
        return None
    if raw in {"无", "0", "none"}:
        return 0
    if "可变" in raw or "视" in raw:
        return None
    total = 0
    matched = False
    for part in re.split(r"[+＋]", raw):
        dice = DICE_RE.search(part)
        if dice:
            total += int(dice.group(1)) * int(dice.group(2))
            matched = True
            continue
        number = re.search(r"\d+", part)
        if number:
            total += int(number.group(0))
            matched = True
    return total if matched else None


def _plain_int(value: Any) -> int | None:
    text = _norm(value)
    if re.fullmatch(r"\d+", text):
        return int(text)
    if re.fullmatch(r"\d+\+", text):
        return int(text[:-1])
    return None


def _group_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    group = entry.get("group")
    if not group:
        return None
    return next((item for item in spells() if item.get("name") == group), None)


def resolve_cost(entry: dict[str, Any]) -> dict[str, Any]:
    """取该法术的消耗；子条目（如各联络术）缺省时回落到所属大类。"""
    cost = entry.get("cost") or ""
    units = entry.get("cost_by_unit") or {}
    origin = entry.get("name", "")
    if not cost:
        parent = _group_entry(entry)
        if parent and parent.get("cost"):
            return resolve_cost(parent)
        return {"mp": None, "san": None, "pow": None, "hp": None, "raw": "", "origin": origin}
    return {
        "mp": units.get("mp", {}).get("value"),
        "san": units.get("san", {}).get("value"),
        "pow": units.get("pow", {}).get("value"),
        "hp": units.get("hp", {}).get("value"),
        "raw": _strip_label(cost),
        "origin": origin,
    }


def compact_spell(entry: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    check = entry.get("check") or {}
    cost = resolve_cost(entry)
    data: dict[str, Any] = {
        "name": entry.get("name", ""),
        "page": entry.get("page"),
        "cost": cost["raw"],
        "casting_time": _strip_label(entry.get("casting_time")),
        "check": "需要检定" if check.get("required") else "无需检定",
        "source": SOURCE_SPELL,
    }
    if entry.get("group"):
        data["group"] = entry["group"]
    if check.get("required"):
        data["check_detail"] = {
            "kinds": check.get("kinds", []),
            "skills": check.get("skills", []),
            "note": check.get("note", ""),
        }
    if not detail:
        return data
    data["aliases"] = entry.get("aliases", [])
    data["cost_by_unit"] = {
        unit: value.get("value") for unit, value in (entry.get("cost_by_unit") or {}).items()
    }
    data["san_note"] = entry.get("san_note", "")
    data["summary"] = entry.get("summary", "")
    text = str(entry.get("text") or "")
    data["text"] = text[:900]
    if len(text) > 900:
        data["text_truncated"] = True
    return data


def find_spells(
    query: Any = "",
    *,
    group: Any = None,
    max_san: Any = None,
    max_mp: Any = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """按名称 / 别名 / 关键词检索法术；可再按大类与消耗上限过滤。"""
    q = _norm(query)
    group_key = _norm(group)
    san_cap = _plain_int(max_san) if max_san is not None else None
    mp_cap = _plain_int(max_mp) if max_mp is not None else None
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in spells():
        if group_key and group_key not in _norm(entry.get("group")) and group_key not in _norm(entry.get("name")):
            continue
        if san_cap is not None:
            ceiling = dice_ceiling((entry.get("cost_by_unit") or {}).get("san", {}).get("value"))
            if ceiling is None or ceiling > san_cap:
                continue
        if mp_cap is not None:
            ceiling = dice_ceiling((entry.get("cost_by_unit") or {}).get("mp", {}).get("value"))
            if ceiling is None or ceiling > mp_cap:
                continue
        score = _score(
            q,
            entry.get("name"),
            entry.get("group"),
            entry.get("summary"),
            " ".join(entry.get("aliases") or []),
        )
        if not score:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("page") or 0))
    return [compact_spell(entry) for _score_value, entry in scored[: max(1, min(limit, 12))]]


def get_spell(name: Any) -> dict[str, Any] | None:
    q = _norm(name)
    if not q:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for entry in spells():
        score = _score(q, entry.get("name"), " ".join(entry.get("aliases") or []))
        if score > best_score:
            best_score = score
            best = entry
    return best


def describe_spell(name: Any) -> dict[str, Any] | None:
    entry = get_spell(name)
    if entry is None:
        return None
    data = compact_spell(entry, detail=True)
    if not entry.get("cost"):
        parent = _group_entry(entry)
        if parent:
            data["cost_note"] = f"该法术无独立消耗，按「{parent.get('name')}」结算：{_strip_label(parent.get('cost'))}"
    return data


def spell_costs(name: Any) -> dict[str, Any] | None:
    """给施法工具用的确定性消耗；骰值类（如 2D10）返回 None 交给 KP 掷骰。"""
    entry = get_spell(name)
    if entry is None:
        return None
    cost = resolve_cost(entry)
    units = {
        key: str(value)
        for key, value in {
            "mp": cost.get("mp"),
            "san": cost.get("san"),
            "pow": cost.get("pow"),
            "hp": cost.get("hp"),
        }.items()
        if value
    }
    return {
        "name": entry.get("name", ""),
        "mp": _plain_int(cost.get("mp")),
        "san": _plain_int(cost.get("san")),
        "pow": _plain_int(cost.get("pow")),
        "hp": _plain_int(cost.get("hp")),
        "units": units,
        "raw": cost.get("raw", ""),
        "origin": cost.get("origin", ""),
    }


def _book_summary(book: dict[str, Any]) -> str:
    """正文条目用原文摘要；只在表Ⅺ出现的典籍由表格字段拼一句结构摘要。"""
    summary = str(book.get("summary") or "").strip()
    if summary:
        return summary
    editions = book.get("editions") or []
    if not editions:
        return ""
    edition = editions[0]
    bits = []
    if edition.get("language"):
        bits.append(str(edition["language"]))
    if edition.get("year"):
        bits.append(str(edition["year"]))
    if edition.get("author"):
        bits.append(f"作者 {edition['author']}")
    if edition.get("weeks"):
        bits.append(f"学时 {edition['weeks']} 周")
    if edition.get("san_loss"):
        bits.append(f"理智 {edition['san_loss']}")
    if edition.get("cmi") is not None:
        bits.append(f"CMI +{edition['cmi']}")
    if edition.get("cmf") is not None:
        bits.append(f"CMF +{edition['cmf']}")
    if edition.get("rating") is not None:
        bits.append(f"MR {edition['rating']}")
    return "；".join(bits)


def compact_grimoire(book: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    editions = book.get("editions") or []
    shown: Iterable[dict[str, Any]] = editions if detail else editions[:1]
    items = []
    for edition in shown:
        item = {
            "language": edition.get("language", ""),
            "san_loss": edition.get("san_loss", ""),
            "cmi": edition.get("cmi"),
            "cmf": edition.get("cmf"),
            "rating": edition.get("rating"),
            "weeks": edition.get("weeks", ""),
        }
        if edition.get("author"):
            item["author"] = edition["author"]
        if edition.get("year"):
            item["year"] = edition["year"]
        if edition.get("spells"):
            item["spells"] = edition["spells"]
        if edition.get("occult"):
            item["occult"] = edition["occult"]
        items.append(item)
    data: dict[str, Any] = {
        "title": book.get("title", ""),
        "page": book.get("page"),
        "editions": items,
        "source": SOURCE_GRIMOIRE,
    }
    if len(editions) > 1:
        data["edition_count"] = len(editions)
    if detail:
        data["summary"] = _book_summary(book)
        text = str(book.get("text") or "")
        data["text"] = text[:900]
        if len(text) > 900:
            data["text_truncated"] = True
    return data


def find_grimoires(
    query: Any = "",
    *,
    language: Any = None,
    max_san: Any = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    q = _norm(query)
    lang_key = _norm(language)
    san_cap = _plain_int(max_san) if max_san is not None else None
    scored: list[tuple[int, dict[str, Any]]] = []
    for book in grimoires():
        editions = book.get("editions") or []
        if lang_key and not any(lang_key in _norm(edition.get("language")) for edition in editions):
            continue
        if san_cap is not None:
            ceilings = [dice_ceiling(edition.get("san_loss")) for edition in editions]
            ceilings = [value for value in ceilings if value is not None]
            if not ceilings or min(ceilings) > san_cap:
                continue
        haystack = " ".join(
            [book.get("title", "")]
            + [edition.get("author", "") for edition in editions]
            + [edition.get("language", "") for edition in editions]
        )
        score = _score(q, book.get("title"), haystack, book.get("summary"))
        if not score:
            continue
        scored.append((score, book))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("page") or 0))
    return [compact_grimoire(book) for _score_value, book in scored[: max(1, min(limit, 12))]]


def get_grimoire(title: Any) -> dict[str, Any] | None:
    q = _norm(title)
    if not q:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for book in grimoires():
        score = _score(q, book.get("title"))
        if score > best_score:
            best_score = score
            best = book
    return best


def describe_grimoire(title: Any) -> dict[str, Any] | None:
    book = get_grimoire(title)
    return compact_grimoire(book, detail=True) if book else None


def grimoire_stats(title: Any, language: Any = None) -> dict[str, Any] | None:
    """给研读工具用的确定性数值（神话等级 / CMI / CMF / 可用法术）。"""
    book = get_grimoire(title)
    if book is None:
        return None
    editions = book.get("editions") or []
    lang_key = _norm(language)
    chosen = None
    if lang_key:
        chosen = next((item for item in editions if lang_key in _norm(item.get("language"))), None)
    chosen = chosen or (editions[0] if editions else None)
    if chosen is None:
        return None
    return {
        "title": book.get("title", ""),
        "language": chosen.get("language", ""),
        "san_loss": chosen.get("san_loss", ""),
        "cmi": chosen.get("cmi"),
        "cmf": chosen.get("cmf"),
        "rating": chosen.get("rating"),
        "weeks": chosen.get("weeks", ""),
        "spells": chosen.get("spells", []),
        "occult": chosen.get("occult", ""),
    }


def format_spell_line(entry: dict[str, Any]) -> str:
    parts = [f"{entry.get('name')}（p{entry.get('page')}）"]
    if entry.get("cost"):
        parts.append(str(entry["cost"]))
    if entry.get("casting_time"):
        parts.append(str(entry["casting_time"]))
    if entry.get("check_detail", {}).get("kinds"):
        parts.append("/".join(entry["check_detail"]["kinds"]))
    return " · ".join(parts)


def format_grimoire_line(book: dict[str, Any]) -> str:
    first = (book.get("editions") or [{}])[0]
    bits = [f"《{book.get('title')}》（p{book.get('page')}）"]
    if first.get("language"):
        bits.append(str(first["language"]))
    if first.get("cmi") is not None or first.get("cmf") is not None:
        bits.append(f"CMI/CMF {first.get('cmi')}/{first.get('cmf')}")
    if first.get("rating") is not None:
        bits.append(f"MR {first['rating']}")
    if first.get("san_loss"):
        bits.append(f"SAN {first['san_loss']}")
    if first.get("weeks"):
        bits.append(f"{first['weeks']} 周")
    if book.get("edition_count"):
        bits.append(f"共 {book['edition_count']} 版")
    return " · ".join(bits)
