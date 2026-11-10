"""CoC7 恐惧症 / 躁狂症表：按需读取的内容数据，不进入提示词。

表存放在 ``data/rulesets/coc7/catalogs/{phobias,manias}.json``，每项含
``id / name / en / tag / description``。核心与 KP 只在真正需要时调用
:func:`query` 或 :func:`random_entry`，避免把 200 条内容整表注入上下文。

文件缺失或损坏时回落到内置的最小样本表，保证疯狂结算永不因内容缺失中断。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from trpg_server.config.paths import DATA_DIR

CATALOG_DIR = DATA_DIR / "rulesets" / "coc7" / "catalogs"
KIND_FILES = {"phobia": "phobias.json", "mania": "manias.json"}
KIND_LABELS = {"phobia": "恐惧症", "mania": "躁狂症"}

# 内置兜底：只用于数据文件不可用的极端情况。
FALLBACK: dict[str, tuple[dict[str, Any], ...]] = {
    "phobia": (
        {"id": 1, "name": "幽闭恐惧", "en": "claustrophobia", "tag": "enclosed", "description": "害怕封闭狭小空间"},
        {"id": 2, "name": "黑暗恐惧", "en": "nyctophobia", "tag": "darkness", "description": "害怕没有光线的环境"},
        {"id": 3, "name": "蜘蛛恐惧", "en": "arachnophobia", "tag": "spider", "description": "害怕蜘蛛与蛛形生物"},
        {"id": 4, "name": "高处恐惧", "en": "acrophobia", "tag": "heights", "description": "害怕高处与坠落"},
        {"id": 5, "name": "血液恐惧", "en": "hemophobia", "tag": "blood", "description": "害怕血液与伤口"},
        {"id": 6, "name": "尸体恐惧", "en": "necrophobia", "tag": "corpse", "description": "害怕尸体与死亡之物"},
        {"id": 7, "name": "海洋恐惧", "en": "thalassophobia", "tag": "sea", "description": "害怕大海与深水"},
        {"id": 8, "name": "镜子恐惧", "en": "eisoptrophobia", "tag": "mirror", "description": "害怕镜子与倒影"},
        {"id": 9, "name": "昆虫恐惧", "en": "entomophobia", "tag": "insect", "description": "害怕昆虫与节肢动物"},
        {"id": 10, "name": "超自然恐惧", "en": "supernatural phobia", "tag": "supernatural", "description": "害怕鬼魂与超自然现象"},
    ),
    "mania": (
        {"id": 1, "name": "洁癖", "en": "ablutomania", "tag": "cleanliness", "description": "反复清洗，无法忍受污渍"},
        {"id": 2, "name": "纵火癖", "en": "pyromania", "tag": "fire", "description": "无法抑制地点火的冲动"},
        {"id": 3, "name": "偷窃癖", "en": "kleptomania", "tag": "theft", "description": "无法抑制地偷取物品"},
        {"id": 4, "name": "谎言癖", "en": "pseudomania", "tag": "lying", "description": "习惯性编造并相信自己的谎言"},
        {"id": 5, "name": "藏书癖", "en": "bibliomania", "tag": "books", "description": "痴迷收集书籍与手稿"},
        {"id": 6, "name": "赌博癖", "en": "gambling mania", "tag": "gambling", "description": "无法停止下注与冒险"},
        {"id": 7, "name": "疼痛迷恋", "en": "algomania", "tag": "pain", "description": "痴迷于疼痛与自我伤害"},
        {"id": 8, "name": "大笑狂", "en": "geliomania", "tag": "laughter", "description": "不合时宜地狂笑不止"},
        {"id": 9, "name": "呼喊狂", "en": "klazomania", "tag": "shouting", "description": "无法控制地大声呼喊"},
        {"id": 10, "name": "仪式癖", "en": "ritual mania", "tag": "ritual", "description": "必须完成固定仪式才安心"},
    ),
}

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _normalize_kind(kind: Any) -> str:
    text = str(kind or "").strip().lower()
    if text.startswith("phob") or text.startswith("恐惧"):
        return "phobia"
    if text.startswith("man") or text.startswith("躁"):
        return "mania"
    return ""


def _coerce_entry(raw: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    try:
        entry_id = int(raw.get("id") or index)
    except (TypeError, ValueError):
        entry_id = index
    return {
        "id": entry_id,
        "name": name[:60],
        "en": str(raw.get("en") or "").strip()[:60],
        "tag": str(raw.get("tag") or "").strip()[:40],
        "description": str(raw.get("description") or "").strip()[:200],
    }


def _read_file(kind: str) -> list[dict[str, Any]]:
    path = CATALOG_DIR / KIND_FILES.get(kind, "")
    if not path.is_file():
        return [dict(item) for item in FALLBACK.get(kind, ())]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _CACHE.get(kind)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [dict(item) for item in FALLBACK.get(kind, ())]
    rows = payload.get("entries") if isinstance(payload, Mapping) else payload
    entries: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for index, raw in enumerate(rows, start=1):
            if isinstance(raw, Mapping):
                entry = _coerce_entry(raw, index)
                if entry:
                    entries.append(entry)
    if not entries:
        entries = [dict(item) for item in FALLBACK.get(kind, ())]
    _CACHE[kind] = (mtime, entries)
    return entries


def entries(kind: Any) -> list[dict[str, Any]]:
    """整表（只给内部随机 / 计数用；对外请用 :func:`query`）。"""
    key = _normalize_kind(kind)
    return _read_file(key) if key else []


def count(kind: Any) -> int:
    return len(entries(kind))


def search(kind: Any, keyword: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    """按关键词（名称 / 英文 / 标签 / 描述）检索；空关键词返回前 ``limit`` 条。"""
    rows = entries(kind)
    needle = " ".join(str(keyword or "").split()).casefold()
    if needle:
        rows = [
            row
            for row in rows
            if needle in str(row.get("name") or "").casefold()
            or needle in str(row.get("en") or "").casefold()
            or needle in str(row.get("tag") or "").casefold()
            or needle in str(row.get("description") or "").casefold()
        ]
    return rows[: max(1, min(100, int(limit or 10)))]


def query(kind: Any = None, keyword: Any = None, *, limit: int = 10) -> dict[str, Any]:
    """按需查询入口：返回条数与命中项，避免整表进入上下文。"""
    key = _normalize_kind(kind)
    kinds = [key] if key else ["phobia", "mania"]
    results = []
    for item in kinds:
        hits = search(item, keyword, limit=limit)
        results.append(
            {
                "kind": item,
                "label": KIND_LABELS[item],
                "total": count(item),
                "entries": hits,
            }
        )
    payload: dict[str, Any] = {"query": str(keyword or "").strip(), "results": results}
    if key:
        payload["kind"] = key
        payload["entries"] = results[0]["entries"]
        payload["total"] = results[0]["total"]
    return payload


def random_entry(kind: Any, roller: Callable[[int], int]) -> dict[str, Any]:
    """等概率抽取一条；表为空时返回兜底条目。"""
    key = _normalize_kind(kind) or "phobia"
    rows = entries(key)
    if not rows:
        rows = [dict(item) for item in FALLBACK.get(key, ())]
    index = max(1, min(len(rows), int(roller(len(rows)))))
    return dict(rows[index - 1])


def format_entry(entry: Mapping[str, Any]) -> str:
    """``名称（英文）：描述`` 的展示格式。"""
    name = str(entry.get("name") or "").strip()
    en = str(entry.get("en") or "").strip()
    desc = str(entry.get("description") or "").strip()
    label = f"{name}（{en}）" if en else name
    return f"{label}：{desc}" if desc else label


def reload() -> None:
    """清空文件缓存（测试 / 热更新用）。"""
    _CACHE.clear()
    for kind in KIND_FILES:
        _read_file(kind)
