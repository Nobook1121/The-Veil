"""FVTT 世界包的审计与只读视图（zip → world.json 校验 → LevelDB 分组）。

「登记台」用这一层完成嗅探：只校验、只计数，**不写任何持久数据**。防滥用
沿用 veil-map 导入审计的思路：包体积上限（压缩后 + 解压后）、成员数上限、
LevelDB 文档数/单值上限（:class:`fvtt_world_import.leveldb.DbLimits`）。

包形态（两个样例包实测）：zip 根部是 ``world.json``，``data/<collection>/``
是 FVTT v10+ 的 LevelDB 库；场景子集合 ``!scenes.tokens!/!scenes.walls!/
!scenes.lights!`` 与主文档同库，键为 ``<sceneId>.<subId>``。
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from fvtt_world_import.convert_journal import page_plain_text
from fvtt_world_import.convert_scenes import (
    GRID_MAX,
    GRID_MIN,
    GRID_TYPE_SQUARE,
    MAX_BACKGROUND_BYTES,
    MAX_LIGHTS,
    MAX_WALLS,
)
from fvtt_world_import.leveldb import DbLimits, LevelDbError, LevelDbReader

MAX_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 40_000
WORLD_MARKER = "world.json"
DATA_DIR = "data"
SCENES_DIR = "scenes"

SUPPORTED_SYSTEMS = {"dnd5e"}
# coreVersion 兼容窗口：本期只放行 FVTT 13.x（v10+ LevelDB 存储格式）。
SUPPORTED_CORE_MAJORS = {13}

# 剧本条目预估里的版本归一：与核心 ``agents/versioning.normalize_semver``
# 同一套规则（本模块不 import 核心，改那边时同步这里）。
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

SCENE_COLLECTION = "scenes"
SUBCOLLECTIONS = ("scenes.tokens", "scenes.walls", "scenes.lights")
AUDITED_COLLECTION_DIRS = ("actors", "folders", "items", "journal", "tables")

ACTORS_DIR = "actors"
FOLDERS_DIR = "folders"
JOURNAL_DIR = "journal"
ACTOR_COLLECTION = "actors"
ACTOR_ITEM_COLLECTION = "actors.items"
ACTOR_ITEM_EFFECT_COLLECTION = "actors.items.effects"
ACTOR_EFFECT_COLLECTION = "actors.effects"
FOLDER_COLLECTION = "folders"
JOURNAL_COLLECTION = "journal"
JOURNAL_PAGE_COLLECTION = "journal.pages"


class PackageError(Exception):
    """世界包校验失败；``code`` 稳定，``message`` 面向用户。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _world_semver(value: Any) -> str:
    """world.json version → n.n.n；纯数字按补丁位解释，其余回落 1.0.0。"""
    text = str(value or "").strip()
    match = _SEMVER_RE.fullmatch(text)
    if match:
        return ".".join(str(int(part)) for part in match.groups())
    if text.isdigit():
        return f"1.0.{int(text)}"
    return "1.0.0"


def _core_major(world: dict[str, Any]) -> int:
    """coreVersion 主版本；缺省回落 compatibility.verified / minimum。"""
    for value in (
        world.get("coreVersion"),
        (world.get("compatibility") or {}).get("verified"),
        (world.get("compatibility") or {}).get("minimum"),
    ):
        text = str(value or "").strip().lstrip("vV")
        if not text:
            continue
        major = text.split(".", 1)[0]
        if major.isdigit():
            return int(major)
    return 0


class WorldPackage:
    """解包后的世界只读视图：``root`` 是 world.json 所在目录，资产留在 zip 里按需读。"""

    def __init__(self, root: Path, archive: zipfile.ZipFile) -> None:
        self.root = root
        self._archive = archive
        self._names = set(archive.namelist())
        self._sizes = {info.filename: info.file_size for info in archive.infolist()}
        with open(root / WORLD_MARKER, "rb") as handle:
            self.world = json.loads(handle.read().decode("utf-8-sig"))
        self.id = str(self.world.get("id") or "")
        self.title = str(self.world.get("title") or self.id)
        self.version = str(self.world.get("version") or "")
        self.system = str(self.world.get("system") or "")
        self.system_version = str(self.world.get("systemVersion") or "")
        self.core_version = str(self.world.get("coreVersion") or "")
        self.compatibility = self.world.get("compatibility") if isinstance(self.world.get("compatibility"), dict) else {}

    # -- 资产 -------------------------------------------------------------

    def has_asset(self, rel_path: str) -> bool:
        return str(rel_path or "").strip().lstrip("/") in self._names

    def asset_size(self, rel_path: str) -> int:
        return int(self._sizes.get(str(rel_path or "").strip().lstrip("/"), 0))

    def read_asset(self, rel_path: str) -> bytes:
        return self._archive.read(str(rel_path).strip().lstrip("/"))

    def resolve_background(self, src: Any) -> str | None:
        """把 ``background.src`` 解析回 zip 内资产路径。

        实测形态：``worlds/<id>/assets/...``（剥 ``worlds/<id>/`` 前缀）、
        ``assets/...`` 直引；都找不到时按文件名在 ``assets/`` 下兜底
        （覆盖 ``assets/arcanedesk/<sha256>.jpg`` 内容寻址池）。
        """
        text = str(src or "").strip().lstrip("/")
        if not text:
            return None
        candidates: list[str] = []
        if text.startswith("worlds/"):
            # worlds/<worldId>/assets/foo.jpg → assets/foo.jpg
            candidates.append(text.split("/", 2)[-1])
        if not text.startswith("assets/"):
            candidates.append(f"assets/{text}")
        else:
            candidates.append(text)
        candidates.append(f"assets/{os.path.basename(text)}")
        for candidate in candidates:
            if self.has_asset(candidate):
                return candidate
        return None

    # -- LevelDB ----------------------------------------------------------

    def scenes_db_dir(self) -> Path:
        return self.root / DATA_DIR / SCENES_DIR

    def scenes_reader(self, limits: DbLimits | None = None) -> LevelDbReader:
        db_dir = self.scenes_db_dir()
        if not db_dir.is_dir():
            raise PackageError(
                "fvtt.scenes_missing",
                f"包里没有 data/{SCENES_DIR} 集合，无法读取场景。",
            )
        return LevelDbReader(str(db_dir), limits=limits or DbLimits())

    def actors_db_dir(self) -> Path:
        return self.root / DATA_DIR / ACTORS_DIR

    def actors_reader(self, limits: DbLimits | None = None) -> LevelDbReader:
        db_dir = self.actors_db_dir()
        if not db_dir.is_dir():
            raise PackageError(
                "fvtt.actors_missing",
                f"包里没有 data/{ACTORS_DIR} 集合，无法读取角色。",
            )
        return LevelDbReader(str(db_dir), limits=limits or DbLimits())

    def folders_db_dir(self) -> Path:
        return self.root / DATA_DIR / FOLDERS_DIR

    def folders_reader(self, limits: DbLimits | None = None) -> LevelDbReader:
        db_dir = self.folders_db_dir()
        if not db_dir.is_dir():
            raise PackageError(
                "fvtt.folders_missing",
                f"包里没有 data/{FOLDERS_DIR} 集合，无法读取文件夹。",
            )
        return LevelDbReader(str(db_dir), limits=limits or DbLimits())

    def journal_db_dir(self) -> Path:
        return self.root / DATA_DIR / JOURNAL_DIR

    def journal_reader(self, limits: DbLimits | None = None) -> LevelDbReader:
        """journal 库读取器（空库合法：sixianyuchanmian 形态）。

        目录缺席抛 :class:`PackageError`（``fvtt.journal_missing``）——登记台
        把它降级为警告 + 0 计数；``/journal/run`` 把它降级为「0 条 + 警告」，
        不拒收整个包。
        """
        db_dir = self.journal_db_dir()
        if not db_dir.is_dir():
            raise PackageError(
                "fvtt.journal_missing",
                f"包里没有 data/{JOURNAL_DIR} 集合，无法读取 journal。",
            )
        return LevelDbReader(str(db_dir), limits=limits or DbLimits())

    def collection_presence(self) -> dict[str, bool]:
        """「集合存在性」只看目录，不解库（审计快路径）。"""
        data_root = self.root / DATA_DIR
        present: dict[str, bool] = {}
        if data_root.is_dir():
            for name in AUDITED_COLLECTION_DIRS:
                present[name] = (data_root / name).is_dir()
        return present

    # -- 报告 -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "system": self.system,
            "system_version": self.system_version,
            "core_version": self.core_version,
            "compatibility": self.compatibility,
        }


def extract_package(raw: bytes, workdir: str | Path) -> WorldPackage:
    """校验并解包世界 zip；任何失败抛 :class:`PackageError`，不留半成品目录。

    只把 ``world.json`` 与 ``data/**`` 解到磁盘（LevelDB 需要真实文件），
    资产留在 zip 里按需读取——125MB 的样例包审计时不必展开 100MB 图片。
    """
    if not raw:
        raise PackageError("fvtt.package_empty", "上传的世界包是空文件，请重新选择。")
    if len(raw) > MAX_PACKAGE_BYTES:
        limit = MAX_PACKAGE_BYTES // (1024 * 1024)
        raise PackageError(
            "fvtt.package_too_large",
            f"世界包不能超过 {limit} MB，请压缩后再上传。",
            status=413,
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError("fvtt.package_not_zip", "文件不是有效的 zip 包。") from exc
    infos = archive.infolist()
    names = archive.namelist()
    if WORLD_MARKER not in names:
        raise PackageError(
            "fvtt.world_json_missing",
            "zip 里没有 world.json——不是 FVTT 世界包。",
        )
    if len(names) > MAX_MEMBERS:
        raise PackageError("fvtt.package_members", "包内文件数过多，无法审计。")
    total_bytes = sum(info.file_size for info in infos if not info.is_dir())
    if total_bytes > MAX_UNCOMPRESSED_BYTES:
        raise PackageError(
            "fvtt.package_too_large",
            "包解压后超过 2 GB，无法审计。",
            status=413,
        )

    root = Path(workdir)
    members = [name for name in names if name == WORLD_MARKER or name.startswith(f"{DATA_DIR}/")]
    try:
        for member in members:
            archive.extract(member, root)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PackageError("fvtt.package_extract_failed", "世界包解压失败，文件可能不完整。") from exc

    marker = root / WORLD_MARKER
    if not marker.is_file():
        raise PackageError("fvtt.world_json_missing", "world.json 不是文件，包结构异常。")
    try:
        world = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("fvtt.world_json_invalid", f"world.json 解析失败：{exc}") from exc
    if not isinstance(world, dict):
        raise PackageError("fvtt.world_json_invalid", "world.json 顶层必须是对象。")

    system = str(world.get("system") or "").strip().lower()
    if system not in SUPPORTED_SYSTEMS:
        raise PackageError(
            "fvtt.system_unsupported",
            f"仅支持 dnd5e 系统的世界包（本包 system={system or '未知'}）。",
        )
    major = _core_major(world)
    if major not in SUPPORTED_CORE_MAJORS:
        found = world.get("coreVersion") or world.get("compatibility") or "未知"
        raise PackageError(
            "fvtt.core_version_unsupported",
            f"仅支持 FVTT 13.x 世界包（本包 coreVersion={found}）；老版平文件导出暂不在兼容窗口。",
        )

    return WorldPackage(root, archive)


class SceneIndex:
    """``data/scenes`` 的单次遍历结果：主文档 + 子集合按 scene id 分组。"""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, list[dict[str, Any]]] = {}
        self.walls: dict[str, list[dict[str, Any]]] = {}
        self.lights: dict[str, list[dict[str, Any]]] = {}
        self.orphans: list[str] = []


def read_scenes(pkg: WorldPackage, *, limits: DbLimits | None = None) -> SceneIndex:
    """遍历场景库一次：``!scenes!`` 主文档 + ``!scenes.tokens!/walls!/lights!`` 子集合。

    子集合键 ``<sceneId>.<subId>``；父 id 找不到主文档的孤儿条目进
    ``orphans``（审计报告里如实警告，不静默丢弃）。
    """
    index = SceneIndex()
    reader = pkg.scenes_reader(limits)
    for user_key, value in reader.iter_entries():
        parts = LevelDbReader.split_key(user_key)
        if parts is None:
            continue
        collection, doc_id = parts
        try:
            doc = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if collection == SCENE_COLLECTION:
            index.documents[doc_id] = doc
        elif collection in SUBCOLLECTIONS:
            parent = doc_id.split(".", 1)[0]
            bucket = {
                "scenes.tokens": index.tokens,
                "scenes.walls": index.walls,
                "scenes.lights": index.lights,
            }[collection]
            bucket.setdefault(parent, []).append(doc)
    known = set(index.documents)
    for bucket in (index.tokens, index.walls, index.lights):
        for parent in bucket:
            if parent not in known:
                index.orphans.append(parent)
    return index


class ActorIndex:
    """``data/actors`` 单库多命名空间的一次遍历结果（角色第三刀）。

    - ``documents``：``!actors!`` 主文档（actor id → 文档）；
    - ``items``：``!actors.items!`` 内嵌条目，键 ``<actorId>.<itemId>``；
      聚合时把**复合键写回条目 ``_id``**（原文档 ``_id`` 只是裸 itemId）；
    - ``effects``：``!actors.effects!`` actor 级效果（如梦梦的中毒）；
    - ``item_effects``：``!actors.items.effects!`` 条目级 buff——按降级清单
      #13 直接丢弃，只保留计数；
    - ``folders``：``!folders!`` 里 ``type=Actor`` 的文件夹（actor id 分组名，
      NPC 模块 title 前缀用）。
    """

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.effects: dict[str, list[dict[str, Any]]] = {}
        self.folders: dict[str, str] = {}
        self.item_effects = 0
        self.orphan_items = 0
        self.orphan_effects = 0

    def actor_type_counts(self) -> dict[str, int]:
        counts = {"character": 0, "npc": 0, "vehicle": 0}
        for doc in self.documents.values():
            kind = str(doc.get("type") or "").strip().lower()
            if kind in counts:
                counts[kind] += 1
        return counts

    def folder_groups(self) -> list[dict[str, Any]]:
        """actor 按 type=Actor 文件夹分组计数（inspect 报告 / 导入完成报告用）。"""
        groups: dict[str, dict[str, int]] = {}
        for doc in self.documents.values():
            folder = self.folders.get(str(doc.get("folder") or ""))
            if not folder:
                continue
            kind = str(doc.get("type") or "").strip().lower()
            row = groups.setdefault(folder, {"character": 0, "npc": 0, "vehicle": 0})
            if kind in row:
                row[kind] += 1
        return [
            {"name": name, **counts}
            for name, counts in sorted(groups.items(), key=lambda item: item[0])
        ]


def read_actors(pkg: WorldPackage, *, limits: DbLimits | None = None) -> ActorIndex:
    """遍历 actor 库一次：actor 主文档 + 复合键聚合的 items/effects + 文件夹名。

    - ``!actors.items.effects!`` 条目级效果按降级清单 #13 丢弃只计数；
    - 父 actor 找不到的孤儿条目如实计数（``orphan_items`` / ``orphan_effects``），
      转换层按文档驱动、孤儿天然不进产物，审计报告里给警告；
    - ``!folders!`` 库缺席时文件夹映射为空（folders 只是分组装饰，不拒收）。
    """
    index = ActorIndex()
    reader = pkg.actors_reader(limits)
    for user_key, value in reader.iter_entries():
        parts = LevelDbReader.split_key(user_key)
        if parts is None:
            continue
        collection, doc_id = parts
        if collection == ACTOR_ITEM_EFFECT_COLLECTION:
            index.item_effects += 1
            continue
        try:
            doc = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if collection == ACTOR_COLLECTION:
            doc = dict(doc)
            doc["_id"] = doc_id
            index.documents[doc_id] = doc
        elif collection == ACTOR_ITEM_COLLECTION:
            parent, _sep, _tail = doc_id.partition(".")
            if not _sep:
                index.orphan_items += 1
                continue
            doc = dict(doc)
            # 复合键写回 _id：转换层据此还原 details.originalClass 等条目引用。
            doc["_id"] = doc_id
            index.items.setdefault(parent, []).append(doc)
        elif collection == ACTOR_EFFECT_COLLECTION:
            parent, _sep, _tail = doc_id.partition(".")
            if not _sep:
                index.orphan_effects += 1
                continue
            doc = dict(doc)
            doc["_id"] = doc_id
            index.effects.setdefault(parent, []).append(doc)
    known = set(index.documents)
    index.orphan_items += sum(
        len(rows) for parent, rows in index.items.items() if parent not in known
    )
    index.orphan_effects += sum(
        len(rows) for parent, rows in index.effects.items() if parent not in known
    )
    index.items = {
        parent: rows for parent, rows in index.items.items() if parent in known
    }
    index.effects = {
        parent: rows for parent, rows in index.effects.items() if parent in known
    }

    if pkg.folders_db_dir().is_dir():
        folders_reader = pkg.folders_reader(limits)
        for user_key, value in folders_reader.iter_entries():
            parts = LevelDbReader.split_key(user_key)
            if parts is None or parts[0] != FOLDER_COLLECTION:
                continue
            try:
                doc = json.loads(value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if str(doc.get("type") or "").strip() != "Actor":
                continue
            name = str(doc.get("name") or "").strip()
            if name:
                index.folders[parts[1]] = name
    return index


class JournalIndex:
    """``data/journal`` 单库多命名空间的一次遍历结果（journal 第四刀）。

    - ``entries``：``!journal!`` 主文档（journal id → 文档，``_id`` 写回）；
    - ``pages``：``!journal.pages!`` 页文档，键 ``<journalId>.<pageId>``，
      复合键写回 ``_id``，按 journal id 聚合；
    - ``folders``：``!folders!`` 里 ``type=JournalEntry`` 的文件夹文档
      （章节层级：name/parent/sort）；
    - ``scene_names`` / ``scene_active``：``!scenes!`` 主文档的 id → 名称与
      激活集——journal ↔ scene 名称关联边（§5.3）的确定性挂点；scenes 库
      缺席时静默为空（挂点是装饰，不是 journal 导入的前提）；
    - ``orphan_pages``：主文档找不到的页条目计数（如实警告，不静默丢）。
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, list[dict[str, Any]]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.scene_names: dict[str, str] = {}
        self.scene_active: set[str] = set()
        self.orphan_pages = 0

    def entry_page_counts(self) -> dict[str, int]:
        return {entry_id: len(rows) for entry_id, rows in self.pages.items()}


def read_journal(pkg: WorldPackage, *, limits: DbLimits | None = None) -> JournalIndex:
    """遍历 journal 库一次：条目 + 复合键聚合的页 + JournalEntry 文件夹 + 场景名。

    - 空库（sixianyuchanmian 形态：只有 MANIFEST/CURRENT/.log、零文档）返回
      空 index，不抛错——空库路径由调用方按「0 条 + 警告」汇报；
    - 页的 ``_id`` 是复合键 ``<journalId>.<pageId>``，转换层据此稳定命名
      模块/节点 id（幂等重导不翻倍）；
    - folders 库缺席时章节映射为空（条目直接挂到隐式章节，不拒收）；
    - scenes 库缺席或解析失败时场景挂点为空（只是少了 mentions 边）。
    """
    index = JournalIndex()
    reader = pkg.journal_reader(limits)
    for user_key, value in reader.iter_entries():
        parts = LevelDbReader.split_key(user_key)
        if parts is None:
            continue
        collection, doc_id = parts
        try:
            doc = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if collection == JOURNAL_COLLECTION:
            doc = dict(doc)
            doc["_id"] = doc_id
            index.entries[doc_id] = doc
        elif collection == JOURNAL_PAGE_COLLECTION:
            parent, _sep, _tail = doc_id.partition(".")
            if not _sep:
                index.orphan_pages += 1
                continue
            doc = dict(doc)
            doc["_id"] = doc_id
            index.pages.setdefault(parent, []).append(doc)
    known = set(index.entries)
    index.orphan_pages += sum(
        len(rows) for parent, rows in index.pages.items() if parent not in known
    )
    index.pages = {
        parent: rows for parent, rows in index.pages.items() if parent in known
    }

    if pkg.folders_db_dir().is_dir():
        folders_reader = pkg.folders_reader(limits)
        for user_key, value in folders_reader.iter_entries():
            parts = LevelDbReader.split_key(user_key)
            if parts is None or parts[0] != FOLDER_COLLECTION:
                continue
            try:
                doc = json.loads(value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if str(doc.get("type") or "").strip() != "JournalEntry":
                continue
            if not str(doc.get("name") or "").strip():
                continue
            index.folders[parts[1]] = doc

    # 场景挂点（尽力而为）：scenes 库缺席/损坏不影响 journal 导入。
    if pkg.scenes_db_dir().is_dir():
        try:
            scenes_reader = pkg.scenes_reader(limits)
            for user_key, value in scenes_reader.iter_entries():
                parts = LevelDbReader.split_key(user_key)
                if parts is None or parts[0] != SCENE_COLLECTION:
                    continue
                try:
                    doc = json.loads(value.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(doc, dict):
                    continue
                name = str(doc.get("name") or "").strip()
                if not name:
                    continue
                index.scene_names[parts[1]] = name
                if doc.get("active"):
                    index.scene_active.add(parts[1])
        except LevelDbError:
            pass
    return index


def audit(pkg: WorldPackage, *, limits: DbLimits | None = None) -> dict[str, Any]:
    """嗅探报告：world.json 摘要 + 场景/棋子/墙/光计数 + 警告列表。

    只读不写；LevelDB 结构非法转 :class:`PackageError`（可读中文错误）。
    """
    try:
        index = read_scenes(pkg, limits=limits)
    except LevelDbError as exc:
        raise PackageError("fvtt.leveldb_invalid", f"场景集合（LevelDB）解析失败：{exc}") from exc

    warnings: list[str] = []
    if index.orphans:
        warnings.append(
            f"{len(index.orphans)} 个棋子/墙/光源条目引用了不存在的场景 id，将跳过。"
        )

    scenes_detail: list[dict[str, Any]] = []
    total_tokens = total_walls = total_lights = 0
    convertible = 0
    for scene_id, doc in index.documents.items():
        grid = doc.get("grid") if isinstance(doc.get("grid"), dict) else {}
        grid_size = grid.get("size")
        grid_type = grid.get("type", 1)
        title = str(doc.get("name") or scene_id)
        background = doc.get("background") if isinstance(doc.get("background"), dict) else {}
        asset = pkg.resolve_background(background.get("src"))
        walls = len(index.walls.get(scene_id) or [])
        lights = len(index.lights.get(scene_id) or [])
        tokens = len(index.tokens.get(scene_id) or [])
        total_walls += walls
        total_lights += lights
        total_tokens += tokens
        scene_warnings: list[str] = []
        if grid_type != GRID_TYPE_SQUARE:
            scene_warnings.append("六边形网格暂不支持，本期跳过该场景。")
        if not isinstance(grid_size, (int, float)) or not GRID_MIN <= float(grid_size) <= GRID_MAX:
            scene_warnings.append(
                f"网格像素（{grid_size!r}）不在 {GRID_MIN}–{GRID_MAX} 范围内，本期跳过该场景。"
            )
        units = str(grid.get("units") or "ft")
        if units not in {"ft", "feet", "foot"}:
            scene_warnings.append(f"网格单位是「{units}」，本期只支持英尺，跳过该场景。")
        if asset is None:
            scene_warnings.append("背景图在包内找不到，本期跳过该场景。")
        elif pkg.asset_size(asset) > MAX_BACKGROUND_BYTES:
            scene_warnings.append("背景图超过 40 MB 上限，本期跳过该场景。")
        if walls > MAX_WALLS:
            scene_warnings.append(f"墙体 {walls} 段超过 {MAX_WALLS} 上限，导入时会截断。")
        if lights > MAX_LIGHTS:
            scene_warnings.append(f"光源 {lights} 个超过 {MAX_LIGHTS} 上限，导入时会截断。")
        if not scene_warnings:
            convertible += 1
        warnings.extend(f"场景「{title}」：{text}" for text in scene_warnings)

        scenes_detail.append(
            {
                "scene_id": scene_id,
                "title": title,
                "active": bool(doc.get("active")),
                "grid_size": grid_size,
                "grid_type": grid_type,
                "background": asset or "",
                "walls": walls,
                "lights": lights,
                "tokens": tokens,
                "convertible": not scene_warnings,
            }
        )
    scenes_detail.sort(key=lambda item: (not item["active"], str(item["title"])))
    if not index.documents:
        warnings.append("包里没有任何场景。")

    # actors 段：目录缺席不拒收（只给 0 计数 + 警告），LevelDB 损坏仍转可读错误。
    actor_counts = {"character": 0, "npc": 0, "vehicle": 0}
    actor_folders: list[dict[str, Any]] = []
    actor_items = 0
    item_effects = 0
    try:
        actor_index = read_actors(pkg, limits=limits)
    except LevelDbError as exc:
        raise PackageError("fvtt.leveldb_invalid", f"角色集合（LevelDB）解析失败：{exc}") from exc
    except PackageError:
        warnings.append("包里没有 actors 集合，角色导入时会被拒绝。")
    else:
        actor_counts = actor_index.actor_type_counts()
        actor_folders = actor_index.folder_groups()
        actor_items = sum(len(rows) for rows in actor_index.items.values())
        item_effects = actor_index.item_effects
        if actor_index.orphan_items or actor_index.orphan_effects:
            warnings.append(
                f"{actor_index.orphan_items} 个条目 / {actor_index.orphan_effects} 个效果"
                "引用了不存在的 actor，将跳过。"
            )
        if not actor_index.documents:
            warnings.append("包里没有任何角色。")
        if item_effects:
            warnings.append(f"{item_effects} 个条目级效果（transfer buff）按降级策略丢弃，仅计数。")

    # journal 段：目录缺席或空库都只降级为警告 + 0 计数（journal 是可选集合，
    # sixianyuchanmian 就是空库形态），LevelDB 损坏仍转可读错误。
    journals_detail: list[dict[str, Any]] = []
    journal_folders = 0
    journal_convertible = 0
    try:
        journal_index = read_journal(pkg, limits=limits)
    except LevelDbError as exc:
        raise PackageError("fvtt.leveldb_invalid", f"journal 集合（LevelDB）解析失败：{exc}") from exc
    except PackageError:
        warnings.append("包里没有 journal 集合，journal 导入时将返回 0 条。")
    else:
        journal_folders = len(journal_index.folders)
        for entry_id, entry in sorted(journal_index.entries.items(), key=lambda pair: str(pair[1].get("name") or pair[0])):
            rows = journal_index.pages.get(entry_id) or []
            usable = sum(1 for row in rows if str(page_plain_text(row) or "").strip())
            journal_convertible += usable
            journals_detail.append(
                {
                    "entry_id": entry_id,
                    "title": str(entry.get("name") or entry_id),
                    "folder": str(journal_index.folders.get(str(entry.get("folder") or ""), {}).get("name") or ""),
                    "pages": len(rows),
                    "convertible": usable,
                }
            )
        if not journal_index.entries:
            warnings.append("journal 集合为空库，没有可转换的 journal 条目。")
        if journal_index.orphan_pages:
            warnings.append(
                f"{journal_index.orphan_pages} 个 journal 页引用了不存在的条目，将跳过。"
            )

    return {
        **pkg.summary(),
        "collections": pkg.collection_presence(),
        # 登记台（M5）：本包导入将产出的剧本条目预估——标题/版本/模块数
        # （journal 可转换页）。场景与角色不进剧本条目（运行时数据，走房间维度）。
        "scenario_would_create": {
            "title": str(pkg.title or pkg.id or "").strip(),
            "version": _world_semver(pkg.version),
            "modules": journal_convertible,
        },
        "scenes": len(index.documents),
        "convertible_scenes": convertible,
        "tokens": total_tokens,
        "walls": total_walls,
        "lights": total_lights,
        "scenes_detail": scenes_detail,
        "characters": actor_counts["character"],
        "npcs": actor_counts["npc"],
        "vehicles": actor_counts["vehicle"],
        "actor_items": actor_items,
        "actor_item_effects": item_effects,
        "actor_folders": actor_folders,
        "journals": len(journals_detail),
        "journal_pages": sum(row["pages"] for row in journals_detail),
        "journal_convertible": journal_convertible,
        "journal_folders": journal_folders,
        "journals_detail": journals_detail,
        "warnings": warnings,
    }


__all__ = [
    "ACTOR_COLLECTION",
    "ACTOR_EFFECT_COLLECTION",
    "ACTOR_ITEM_COLLECTION",
    "ACTOR_ITEM_EFFECT_COLLECTION",
    "AUDITED_COLLECTION_DIRS",
    "FOLDER_COLLECTION",
    "JOURNAL_COLLECTION",
    "JOURNAL_PAGE_COLLECTION",
    "MAX_PACKAGE_BYTES",
    "ActorIndex",
    "JournalIndex",
    "PackageError",
    "SceneIndex",
    "WorldPackage",
    "audit",
    "extract_package",
    "read_actors",
    "read_journal",
    "read_scenes",
]
