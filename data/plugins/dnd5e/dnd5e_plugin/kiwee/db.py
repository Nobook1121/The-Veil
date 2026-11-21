"""kiwee 深库：SQLite 建库与查询（5etools 中文镜像数据，无 pip 依赖）。

数据来源 ``tjliqy/5etools-cn``（cn2.0 分支，中文数据 CC BY-NC-SA 4.0，
声明见插件根 ``NOTICE-KIWEE.md``）。本模块只做三件事：

1. :func:`build_kiwee_db` —— 把 ``kiwee-data/raw/`` 快照（见 snapshot.py）
   建成 ``kiwee.sqlite3``：条目表 + FTS5 全文索引（FTS5 不可用时降级为
   LIKE + 普通索引，行为一致）。
2. :func:`open_kiwee_db` —— 只读打开；库文件缺席返回 ``None``（深库未建
   的降级语义，消费方应回落 dnd5e-srd）。
3. :func:`KiweeDb.search` —— 中英文名/别名检索，DM/玩家类目门控与
   2014/2024 规则版本过滤。

快照与库都是**本地生成物，不进 git**（.gitignore 覆盖 kiwee-data/）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

# --- 来源与许可标识 ---------------------------------------------------------

KIWEE_SITE = "https://5e.kiwee.top"
SOURCE_REPO_URL = "https://github.com/tjliqy/5etools-cn"
SOURCE_REF_DEFAULT = "cn2.0"
SCHEMA_VERSION = "1"

# 机翻判定口径＝kiwee 站点自身渲染逻辑（js/render.js）：
# translator 字段缺失/为空，或恰好等于"机翻"，都算未校对机翻。
MACHINE_TRANSLATOR_MARK = "机翻"

# 2014/2024 双版本分界：X 系来源码（XPHB=2024 PHB；MMX/XDMG=2025 MM/DMG）
# 与 srd52 标记（调研实测 spells-xphb.json 全部 srd52: true）。
ERA_2024_SOURCES = frozenset({"XPHB", "MMX", "XDMG", "XMM", "DMGX"})
KNOWN_ERAS = ("2014", "2024")

# --- 类目口径 ---------------------------------------------------------------

# DM/玩家分界，口径＝kiwee 调研文档 §4 的分界表（写死在此常量）：
# 玩家侧＝种族/职业/专长/背景/物品/法术/状态；DM 侧＝怪物图鉴（bestiary）、
# DMG 掷表（tables.json）、变体规则（含 DMG 章节，保守归 DM）。
# 站点本身无访问控制，这条分界在本插件里变成真正的检索门控：
# observer_is_dm=False 时这些类目一律不返回。
DM_ONLY_CATEGORIES = frozenset({"monster", "variantrule", "table"})

# 每类目：raw/ 下的数据文件 glob（posix 风格）→ 顶层 JSON 数组键 + 深链页面。
# v1 收录口径（范围修订 2026-11-23，docs/plans/dnd5e-vtt-refactor-plan.md §4）：
# spells/、bestiary/、items.json、feats.json、races.json、class/、
# backgrounds.json、conditionsdiseases.json、tables.json、variantrules.json。
# **不收** adventure/ 与 book/（模组全文 48MB+23MB，留待后续刀）。
# 注：races.json 的子种族嵌在 race.subraces 里，v1 只收顶层种族。
CATEGORY_SPECS: dict[str, dict[str, Any]] = {
    "spell": {"files": ("spells/spells-*.json",), "keys": ("spell",), "page": "spells.html"},
    "monster": {"files": ("bestiary/bestiary-*.json",), "keys": ("monster",), "page": "bestiary.html"},
    "item": {"files": ("items.json",), "keys": ("item",), "page": "items.html"},
    "baseitem": {"files": ("items.json",), "keys": ("baseitem",), "page": "items.html"},
    "feat": {"files": ("feats.json",), "keys": ("feat",), "page": "feats.html"},
    "race": {"files": ("races.json",), "keys": ("race",), "page": "races.html"},
    "class": {"files": ("class/*.json",), "keys": ("class",), "page": "classes.html"},
    "subclass": {"files": ("class/*.json",), "keys": ("subclass",), "page": "classes.html"},
    "background": {"files": ("backgrounds.json",), "keys": ("background",), "page": "backgrounds.html"},
    "condition": {"files": ("conditionsdiseases.json",), "keys": ("condition",), "page": "conditionsdiseases.html"},
    "disease": {"files": ("conditionsdiseases.json",), "keys": ("disease",), "page": "conditionsdiseases.html"},
    "table": {"files": ("tables.json",), "keys": ("table",), "page": "tables.html"},
    "variantrule": {"files": ("variantrules.json",), "keys": ("variantrule",), "page": "variantrules.html"},
}
CATEGORIES = tuple(sorted(CATEGORY_SPECS))

# 检索入参的宽松别名（复数/中文），normalize 后仍不在类目表则 ValueError。
CATEGORY_ALIASES: dict[str, str] = {
    "spells": "spell",
    "monsters": "monster",
    "items": "item",
    "baseitems": "baseitem",
    "feats": "feat",
    "races": "race",
    "classes": "class",
    "subclasses": "subclass",
    "backgrounds": "background",
    "conditions": "condition",
    "diseases": "disease",
    "tables": "table",
    "variantrules": "variantrule",
    "法术": "spell",
    "怪物": "monster",
    "物品": "item",
    "装备": "item",
    "基础物品": "baseitem",
    "专长": "feat",
    "种族": "race",
    "职业": "class",
    "子职业": "subclass",
    "背景": "background",
    "状态": "condition",
    "异常状态": "condition",
    "疾病": "disease",
    "掷表": "table",
    "表格": "table",
    "变体规则": "variantrule",
    "术语": "variantrule",
}

# 每档 rank 的 SQL 截取上限：LIKE '%x%' 在真库可能命中数千行，
# 逐档限量再合并去重，最坏情况有界。
_PER_RANK_LIMIT = 500
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200

_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    eng_name TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    page_hint TEXT NOT NULL DEFAULT '',
    rules_era TEXT NOT NULL DEFAULT '2014',
    translator TEXT NOT NULL DEFAULT '',
    is_machine_translated INTEGER NOT NULL DEFAULT 0,
    deep_link TEXT NOT NULL,
    raw TEXT NOT NULL,
    UNIQUE (category, uid)
);
CREATE INDEX idx_entries_category ON entries(category);
CREATE INDEX idx_entries_source ON entries(source);
CREATE INDEX idx_entries_era ON entries(rules_era);
CREATE INDEX idx_entries_name ON entries(name);
CREATE INDEX idx_entries_eng_name ON entries(eng_name);
"""

_FTS_DDL = "CREATE VIRTUAL TABLE entries_fts USING fts5(name, eng_name, aliases);"

_ENTRY_COLUMNS = (
    "id, uid, category, name, eng_name, aliases, source, page_hint, "
    "rules_era, translator, is_machine_translated, deep_link, raw"
)
# FTS join 用：entries 与 entries_fts 有同名列（name/eng_name/aliases），必须限定。
_ENTRY_COLUMNS_E = "e." + _ENTRY_COLUMNS.replace(", ", ", e.")


# --- 小工具 -----------------------------------------------------------------


def fts5_available() -> bool:
    """探测当前 stdlib sqlite3 是否带 FTS5（编译期开关，Windows 自带版通常有）。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def build_deep_link(page: str, source: str, name: str) -> str:
    """kiwee 深链：``<page>#<source小写>_<名称>``（调研 §2 的 hash 规则）。

    如 https://5e.kiwee.top/spells.html#phb_火球术 —— 真实浏览器可正常
    渲染定位（headless 抓取不行，与数据无关）。
    """
    return f"{KIWEE_SITE}/{page}#{str(source).strip().lower()}_{str(name).strip()}"


def category_id(value: Any) -> str:
    """类目入参归一：精确 id / 复数 / 中文别名均可；未知类目 ValueError。"""
    if value is None:
        return ""
    text = str(value or "").strip()
    if not text or text.lower() in {"all", "*", "全部", "any"}:
        return ""
    norm = CATEGORY_ALIASES.get(text, text)
    norm = CATEGORY_ALIASES.get(text.lower(), norm)
    if norm not in CATEGORY_SPECS:
        raise ValueError(f"未知 kiwee 类目：{text!r}（可选：{', '.join(CATEGORIES)} 或 all）")
    return norm


def normalize_rules_version(value: Any) -> str | None:
    """规则版本入参归一：None/空 → 不过滤；只接受 2014/2024。"""
    if value is None:
        return None
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"2014", "5e"}:
        return "2014"
    if text in {"2024", "5r"}:
        return "2024"
    raise ValueError(f"未知规则版本：{value!r}（可选：2014 / 2024）")


def _page_hint(entry: dict[str, Any]) -> str:
    """来源页码提示，如 ``PHB 241``；无页码则只留来源码。"""
    source = str(entry.get("source") or "").strip()
    page = entry.get("page")
    if page in (None, "", 0):
        return source
    return f"{source} {page}"


def _rules_era(entry: dict[str, Any]) -> str:
    source = str(entry.get("source") or "").strip()
    if source in ERA_2024_SOURCES:
        return "2024"
    if entry.get("srd52"):
        return "2024"
    return "2014"


def _is_machine_translated(entry: dict[str, Any]) -> int:
    translator = str(entry.get("translator") or "").strip()
    if not translator or translator == MACHINE_TRANSLATOR_MARK:
        return 1
    return 0


def _aliases_text(entry: dict[str, Any]) -> str:
    alias = entry.get("alias")
    if isinstance(alias, str):
        alias = [alias]
    if not isinstance(alias, (list, tuple)):
        return ""
    return " ".join(str(a).strip() for a in alias if str(a or "").strip())


def _like_escape(text: str) -> str:
    return text.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")


def _fts_match_string(query: str) -> str:
    """把查询词转成 FTS5 MATCH 串：每 token 加前缀通配（火球* / fire*）。"""
    terms: list[str] = []
    for token in re.split(r"\s+", query.strip()):
        token = token.strip().strip('"')
        if not token:
            continue
        terms.append(f'"{token}"*')
    return " ".join(terms)


# --- 建库 -------------------------------------------------------------------


def build_kiwee_db(
    raw_dir: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    *,
    use_fts: bool | None = None,
) -> dict[str, Any]:
    """把 raw/ 快照建成 SQLite 深库（原子替换，可重复执行）。

    ``use_fts=None`` 先探 FTS5 可用性；不可用自动降级 LIKE + 索引并如实
    记录在 stats 与 meta 里。返回统计 dict 供 CLI 与测试断言。
    """
    raw_path = Path(raw_dir)
    db_file = Path(db_path)
    if not raw_path.is_dir():
        raise FileNotFoundError(f"raw 快照目录不存在：{raw_path}（先跑 snapshot fetch）")

    fts_requested = fts5_available() if use_fts is None else bool(use_fts)
    fts_used = fts_requested and fts5_available()
    started = time.monotonic()

    provenance: dict[str, Any] = {}
    prov_file = raw_path / ".provenance.json"
    if prov_file.is_file():
        try:
            provenance = json.loads(prov_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            provenance = {}

    db_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = db_file.with_name(db_file.name + ".tmp")
    if tmp_file.exists():
        tmp_file.unlink()

    conn = sqlite3.connect(tmp_file)
    counts: dict[str, int] = {}
    duplicates = 0
    skipped = 0
    machine = 0
    files_parsed = 0
    try:
        conn.executescript(_SCHEMA_SQL)
        if fts_used:
            conn.execute(_FTS_DDL)

        for category, spec in CATEGORY_SPECS.items():
            cat_count = 0
            for pattern in spec["files"]:
                for data_file in sorted(raw_path.glob(pattern)):
                    try:
                        data = json.loads(data_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        skipped += 1
                        continue
                    files_parsed += 1
                    for key in spec["keys"]:
                        rows = data.get(key)
                        if not isinstance(rows, list):
                            continue
                        for entry in rows:
                            made = _insert_entry(conn, entry, category, spec["page"], fts_used)
                            if made is None:
                                continue
                            if made == "dup":
                                duplicates += 1
                            else:
                                cat_count += 1
                                if made == "new:mt":
                                    machine += 1
            counts[category] = cat_count

        total = sum(counts.values())
        meta_rows = [
            ("schema_version", SCHEMA_VERSION),
            ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            ("fts", "fts5" if fts_used else "like"),
            ("entries", str(total)),
            ("source_repo", str(provenance.get("repo") or SOURCE_REPO_URL)),
            ("source_ref", str(provenance.get("ref") or "")),
            ("source_commit", str(provenance.get("commit") or "")),
        ]
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta_rows)
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_file, db_file)
    return {
        "status": "ok",
        "db_path": str(db_file),
        "db_bytes": db_file.stat().st_size,
        "use_fts": fts_used,
        "fts_requested": fts_requested,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "entries": sum(counts.values()),
        "categories": counts,
        "machine_translated": machine,
        "duplicates": duplicates,
        "skipped_invalid": skipped,
        "files": files_parsed,
    }


def _insert_entry(
    conn: sqlite3.Connection, entry: Any, category: str, page: str, fts_used: bool
) -> str | None:
    """单条目入库；返回 'new' / 'new:machine' / 'dup' / None(无效)。"""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    source = str(entry.get("source") or "").strip()
    if not name or not source:
        return None
    uid = f"{name}|{source}".lower()
    eng_name = str(entry.get("ENG_name") or "").strip()
    aliases = _aliases_text(entry)
    era = _rules_era(entry)
    translator = str(entry.get("translator") or "").strip()
    machine = _is_machine_translated(entry)
    page_hint = _page_hint(entry)
    deep_link = build_deep_link(page, source, name)
    raw_json = json.dumps(entry, ensure_ascii=False, sort_keys=True)

    cur = conn.execute(
        "INSERT OR IGNORE INTO entries (uid, category, name, eng_name, aliases, source, "
        "page_hint, rules_era, translator, is_machine_translated, deep_link, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, category, name, eng_name, aliases, source, page_hint, era, translator, machine, deep_link, raw_json),
    )
    if cur.rowcount <= 0:
        return "dup"
    if fts_used:
        conn.execute(
            "INSERT INTO entries_fts (rowid, name, eng_name, aliases) VALUES (?, ?, ?, ?)",
            (cur.lastrowid, name, eng_name, aliases),
        )
    return "new:mt" if machine else "new"


# --- 查询 -------------------------------------------------------------------


class KiweeDb:
    """只读深库句柄；由 :func:`open_kiwee_db` 构造。"""

    def __init__(self, conn: sqlite3.Connection, path: Path, has_fts: bool, meta: dict[str, str]):
        self._conn = conn
        self.path = path
        self.has_fts = has_fts
        self.meta = meta

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "KiweeDb | None":
        """只读打开；文件缺席或 schema 不匹配返回 ``None``（降级语义）。"""
        db_file = Path(path)
        if not db_file.is_file():
            return None
        uri = f"file:{db_file.resolve().as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            meta = {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            if meta.get("schema_version") != SCHEMA_VERSION:
                conn.close()
                return None
            has_fts = bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries_fts'").fetchone()
            )
        except sqlite3.Error:
            return None
        return cls(conn, db_file, has_fts, meta)

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "KiweeDb":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    # -- 查询 --------------------------------------------------------------

    def search(
        self,
        category: Any = None,
        query: Any = "",
        *,
        rules_version: Any = None,
        observer_is_dm: bool = True,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """中英文名/别名检索，返回结果 dict 列表（契约字段见 _row_to_result）。

        - ``category``：类目 id/复数/中文别名，None 或 "all" 检索全部。
        - ``rules_version``：None 不过滤，"2014"/"2024" 按 source/srd52 分界。
        - ``observer_is_dm=False``：DM_ONLY_CATEGORIES（bestiary/variantrule/
          tables 等调研分界表的 DM 侧类目）一律不返回——空列表而非报错。
        - 三档排序：精确名 > FTS 前缀（有索引时）> LIKE 子串；同档按名称长度。
        """
        cat = category_id(category)
        era = normalize_rules_version(rules_version)
        text = str(query or "").strip()
        if not text:
            return []
        try:
            limit = max(1, min(int(limit), _MAX_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT

        where, params = self._filters(cat, era, observer_is_dm)
        # 去重键＝(category, uid)：item/baseitem 这类跨类目同名条目（uid 同、
        # 类目不同）都要保留，与表级 UNIQUE(category, uid) 一致。
        found: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}

        def _absorb(rank: int, rows: list[tuple]) -> None:
            for row in rows:
                result = _row_to_result(row)
                key = (result["category"], result["uid"])
                if key not in found:
                    found[key] = (rank, result)

        # 档 0：精确名（中文名或英文名，忽略大小写）。
        _absorb(
            0,
            self._conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE (name = :q COLLATE NOCASE "
                f"OR eng_name = :q COLLATE NOCASE) {where} LIMIT {_PER_RANK_LIMIT}",
                {**params, "q": text},
            ).fetchall(),
        )
        # 档 1：FTS5 前缀（含中文整名前缀与英文 token 前缀）。
        if self.has_fts:
            match = _fts_match_string(text)
            if match:
                _absorb(
                    1,
                    self._conn.execute(
                        f"SELECT {_ENTRY_COLUMNS_E} FROM entries e JOIN entries_fts "
                        f"ON entries_fts.rowid = e.id WHERE entries_fts MATCH :m {where} "
                        f"LIMIT {_PER_RANK_LIMIT}",
                        {**params, "m": match},
                    ).fetchall(),
                )
        # 档 2：LIKE 子串（名/英文名/别名；FTS 降级时的主检索路径）。
        # SQLite LIKE 对 ASCII 默认大小写不敏感，中文无大小写，直接用。
        like = f"%{_like_escape(text)}%"
        _absorb(
            2,
            self._conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE (name LIKE :like "
                f"OR eng_name LIKE :like OR aliases LIKE :like) {where} "
                f"LIMIT {_PER_RANK_LIMIT}",
                {**params, "like": like},
            ).fetchall(),
        )

        ordered = sorted(
            found.values(),
            key=lambda pair: (pair[0], len(pair[1]["name"]), pair[1]["name"], pair[1]["source"]),
        )
        return [result for _rank, result in ordered[:limit]]

    def _filters(self, category: str, era: str | None, observer_is_dm: bool) -> tuple[str, dict[str, Any]]:
        where = ""
        params: dict[str, Any] = {}
        if category:
            where += " AND category = :cat"
            params["cat"] = category
        if era:
            where += " AND rules_era = :era"
            params["era"] = era
        if not observer_is_dm:
            marks = ", ".join(f":dm{i}" for i in range(len(DM_ONLY_CATEGORIES)))
            where += f" AND category NOT IN ({marks})"
            params.update({f"dm{i}": cat for i, cat in enumerate(sorted(DM_ONLY_CATEGORIES))})
        return where, params

    # -- 状态 --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        per_category = {
            str(cat): int(n) for cat, n in self._conn.execute("SELECT category, COUNT(*) FROM entries GROUP BY category")
        }
        return {
            "available": True,
            "reason": None,
            "db_path": str(self.path),
            "schema_version": self.meta.get("schema_version"),
            "built_at": self.meta.get("built_at"),
            "fts": self.has_fts,
            "entries": int(self.meta.get("entries") or sum(per_category.values())),
            "categories": per_category,
            "source_repo": self.meta.get("source_repo"),
            "source_ref": self.meta.get("source_ref"),
            "source_commit": self.meta.get("source_commit"),
        }


def open_kiwee_db(path: str | os.PathLike[str]) -> KiweeDb | None:
    """只读打开深库；**快照/库缺席时返回 None**（消费方回落 dnd5e-srd）。"""
    return KiweeDb.open(path)


_RESULT_KEYS = (
    "category",
    "uid",
    "name",
    "eng_name",
    "source",
    "page_hint",
    "deep_link",
    "translator_ok",
    "is_machine_translated",
    "rules_era",
    "raw",
)


def _row_to_result(row: tuple) -> dict[str, Any]:
    """行 → 服务契约 dict（下一刀消费方照这里接）。

    name/eng_name/source/page_hint/deep_link + 机翻徽章 + 完整原文：
    - ``page_hint``：如 "PHB 241"；
    - ``deep_link``：kiwee 深链（真实浏览器可渲染）；
    - ``translator_ok``：True=已校对；``is_machine_translated``：1=未校对机翻；
    - ``rules_era``："2014"/"2024"（source X 系或 srd52 判定）；
    - ``raw``：5etools 原始条目 dict（含 entries/reprintedAs 等，datatag
      解析交给 dnd5e_plugin.kiwee.datatags）。
    """
    (
        _id,
        uid,
        category,
        name,
        eng_name,
        _aliases,
        source,
        page_hint,
        rules_era,
        translator,
        machine,
        deep_link,
        raw_json,
    ) = row
    return {
        "category": category,
        "uid": uid,
        "name": name,
        "eng_name": eng_name,
        "source": source,
        "page_hint": page_hint,
        "deep_link": deep_link,
        "translator_ok": int(machine) == 0,
        "is_machine_translated": int(machine),
        "rules_era": rules_era,
        "raw": json.loads(raw_json),
    }


__all__ = [
    "CATEGORY_ALIASES",
    "CATEGORY_SPECS",
    "CATEGORIES",
    "DM_ONLY_CATEGORIES",
    "ERA_2024_SOURCES",
    "KIWEE_SITE",
    "MACHINE_TRANSLATOR_MARK",
    "SCHEMA_VERSION",
    "SOURCE_REF_DEFAULT",
    "SOURCE_REPO_URL",
    "KiweeDb",
    "build_deep_link",
    "build_kiwee_db",
    "category_id",
    "fts5_available",
    "normalize_rules_version",
    "open_kiwee_db",
]
