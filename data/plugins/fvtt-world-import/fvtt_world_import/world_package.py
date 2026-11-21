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
import zipfile
from pathlib import Path
from typing import Any

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

SCENE_COLLECTION = "scenes"
SUBCOLLECTIONS = ("scenes.tokens", "scenes.walls", "scenes.lights")
AUDITED_COLLECTION_DIRS = ("actors", "folders", "items", "journal", "tables")


class PackageError(Exception):
    """世界包校验失败；``code`` 稳定，``message`` 面向用户。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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

    return {
        **pkg.summary(),
        "collections": pkg.collection_presence(),
        "scenes": len(index.documents),
        "convertible_scenes": convertible,
        "tokens": total_tokens,
        "walls": total_walls,
        "lights": total_lights,
        "scenes_detail": scenes_detail,
        "warnings": warnings,
    }


__all__ = [
    "AUDITED_COLLECTION_DIRS",
    "MAX_PACKAGE_BYTES",
    "PackageError",
    "SceneIndex",
    "WorldPackage",
    "audit",
    "extract_package",
    "read_scenes",
]
