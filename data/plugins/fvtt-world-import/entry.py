"""FVTT 世界包导入插件入口：审计（inspect）与落库（run）HTTP 蓝图。

本刀范围：场景（含墙/光/棋子）→ veil-map；actors→角色卡、journal→世界树
是后续刀，不在本文件里出现半成品。

落库走 ``plugins.runtime.get_service("veil-map", "map_service")`` 的
``import_scene``——插件之间不直接 import 对方的 Python 模块；veil-map 缺席
时返回可读降级错误（503），不写任何数据。inspect 只审计、不落任何持久
数据，临时目录用完即删。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

from flask import Blueprint, request, session

from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated

from fvtt_world_import import convert_scenes, world_package
from fvtt_world_import.convert_scenes import convert_world_scenes
from fvtt_world_import.world_package import PackageError, extract_package, read_scenes

bp = Blueprint("fvtt_world_import", __name__, url_prefix="/api/plugins/fvtt-world-import")
logger = logging.getLogger(__name__)

_OWNER_ROLES = {"owner", "admin"}


def _authenticated() -> bool:
    return bool(str(session.get("user_id") or "").strip())


def _read_upload() -> bytes:
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        raise PackageError("fvtt.package_missing", "请选择要导入的世界包 zip 文件。")
    raw = uploaded.read(world_package.MAX_PACKAGE_BYTES + 1)
    if len(raw) > world_package.MAX_PACKAGE_BYTES:
        raise PackageError(
            "fvtt.package_too_large",
            f"世界包不能超过 {world_package.MAX_PACKAGE_BYTES // (1024 * 1024)} MB，请压缩后再上传。",
            status=413,
        )
    return raw


def _map_service() -> Any | None:
    """探测 veil-map 的场景写入服务；缺席时静默降级为 None。"""
    try:
        from trpg_server.plugins.runtime import get_service

        service = get_service("veil-map", "map_service")
    except Exception:  # noqa: BLE001 - 核心未装插件运行期时静默降级
        return None
    if service is None or not callable(getattr(service, "import_scene", None)):
        return None
    return service


def _room_and_manager(room_id: str):
    """房间访问 + 房主/房管校验；返回 (room_dir, info, error_response)。"""
    room_dir, info = find_room(room_id)
    if not info:
        return None, None, error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return None, None, error_response("你不在这个房间里。", 403, "room.access_denied")
    user_id = str(session.get("user_id") or "")
    if is_elevated():
        return room_dir, info, None
    for member in info.get("members") or []:
        if isinstance(member, dict) and str(member.get("user_id")) == user_id:
            if str(member.get("room_role") or "").strip().lower() in _OWNER_ROLES:
                return room_dir, info, None
            break
    if str(info.get("creator_id") or "") == user_id:
        return room_dir, info, None
    return room_dir, info, error_response(
        "只有房主或房管可以导入世界包。", 403, "fvtt.manager_only"
    )


def _package_error_response(exc: PackageError):
    return error_response(exc.message, exc.status, exc.code)


def _close_package(pkg: Any) -> None:
    archive = getattr(pkg, "_archive", None) if pkg is not None else None
    if archive is None:
        return
    try:
        archive.close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响审计结果
        pass


@bp.route("/inspect", methods=["POST"])
def inspect_world():
    """审计世界包：嗅探报告（标题/版本/场景/棋子/墙/光计数/警告），不落任何数据。"""
    if not _authenticated():
        return error_response("请先登录再审计世界包。", 403, "fvtt.login_required")
    try:
        raw = _read_upload()
    except PackageError as exc:
        return _package_error_response(exc)
    pkg = None
    with tempfile.TemporaryDirectory(prefix="fvtt-inspect-") as workdir:
        try:
            pkg = extract_package(raw, workdir)
            report = world_package.audit(pkg)
        except PackageError as exc:
            return _package_error_response(exc)
        finally:
            _close_package(pkg)
    return success_response(report, "世界包审计完成。")


@bp.route("/rooms/<room_id>/run", methods=["POST"])
def run_import(room_id: str):
    """把世界包的场景（墙/光/棋子）写入目标房间的 veil-map 场景库。"""
    room_dir, info, denied = _room_and_manager(room_id)
    if denied:
        return denied
    service = _map_service()
    if service is None:
        return error_response(
            "地图插件（veil-map）未加载，无法写入场景；请启用后重试。",
            503,
            "fvtt.map_unavailable",
        )
    try:
        raw = _read_upload()
    except PackageError as exc:
        return _package_error_response(exc)

    activate_requested = str(request.form.get("activate", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    with tempfile.TemporaryDirectory(prefix="fvtt-run-") as workdir:
        try:
            pkg = extract_package(raw, workdir)
            index = read_scenes(pkg)
        except PackageError as exc:
            return _package_error_response(exc)
        except Exception as exc:  # noqa: BLE001 - LevelDB 结构问题转可读错误
            logger.warning("fvtt_world_read_failed room=%s", room_id, exc_info=True)
            return error_response(
                f"世界包数据读取失败：{exc}", 400, "fvtt.leveldb_invalid"
            )

        def resolve_background(scene_doc: dict[str, Any]) -> dict[str, Any] | None:
            rel = pkg.resolve_background(convert_scenes.scene_background_src(scene_doc))
            if rel is None:
                return None
            return {
                "path": rel,
                "name": os.path.basename(rel),
                "bytes": pkg.asset_size(rel),
            }

        def companion_for(scene_doc: dict[str, Any]) -> dict[str, Any] | None:
            """sixianyuchanmian 形态的伴生 JSON（``assets/scenes/<名>.json``）。"""
            src = convert_scenes.scene_background_src(scene_doc)
            base = os.path.basename(src)
            if not base:
                return None
            candidate = os.path.join("assets", "scenes", os.path.splitext(base)[0] + ".json")
            if not pkg.has_asset(candidate):
                return None
            try:
                data = json.loads(pkg.read_asset(candidate).decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                return None
            return data if isinstance(data, dict) else None

        converted, skipped, warnings = convert_world_scenes(
            index,
            resolve_background=resolve_background,
            companion_for=companion_for,
        )

        results: list[dict[str, Any]] = []
        chosen: dict[str, Any] | None = None
        for payload in converted:
            if payload["active"]:
                chosen = payload
        chosen = chosen or (converted[0] if converted else None)

        # 先写非激活场景，最后写激活场景（带 activate 一起切换房间的当前地图）。
        ordered = [item for item in converted if item is not chosen]
        if chosen is not None:
            ordered.append(chosen)
        for payload in ordered:
            try:
                payload["image"] = {
                    "bytes": pkg.read_asset(payload["background"]["path"]),
                    "name": payload["background"]["name"],
                }
            except (OSError, KeyError) as exc:
                logger.warning("fvtt_world_asset_failed room=%s", room_id, exc_info=True)
                return error_response(
                    f"背景图读取失败：{payload['background'].get('path', '')}",
                    500,
                    "fvtt.asset_read_failed",
                )
            activate = bool(activate_requested and payload is chosen)
            try:
                outcome = service.import_scene(room_dir, info, payload, activate=activate)
            except Exception:  # noqa: BLE001 - 服务端异常按失败处理
                logger.warning("fvtt_world_import_scene_failed room=%s", room_id, exc_info=True)
                outcome = {"ok": False, "code": "map.import_failed", "error": "场景写入失败。"}
            if not outcome.get("ok"):
                return error_response(
                    str(outcome.get("error") or "场景写入失败。"),
                    400 if str(outcome.get("code")) == "map.ruleset_disabled" else 500,
                    str(outcome.get("code") or "map.import_failed"),
                )
            results.append(
                {
                    "scene_id": outcome.get("scene_id"),
                    "title": outcome.get("title"),
                    "created": bool(outcome.get("created")),
                    "revision": int(outcome.get("revision") or 0),
                    "active": activate,
                    "grid_size": outcome.get("grid_size"),
                    "grid_offset": outcome.get("grid_offset"),
                    "bounds_cells": outcome.get("bounds_cells"),
                    "walls": outcome.get("walls"),
                    "lights": outcome.get("lights"),
                    "tokens": outcome.get("tokens"),
                    "background": outcome.get("background"),
                    "warnings": payload.get("warnings") or [],
                }
            )
        _close_package(pkg)

    totals = {
        "scenes": len(results),
        "skipped": len(skipped),
        "walls": sum(int(item.get("walls") or 0) for item in results),
        "lights": sum(int(item.get("lights") or 0) for item in results),
        "tokens": sum(int(item.get("tokens") or 0) for item in results),
    }
    activated = chosen if (activate_requested and chosen is not None) else None
    return success_response(
        {
            "world": pkg_summary_safe(pkg),
            "scenes": results,
            "skipped": skipped,
            "warnings": warnings,
            # activate=false 时不切换房间当前场景：active_scene_id 如实为空。
            "active_scene_id": (activated or {}).get("scene_id", ""),
            "activate": activate_requested,
            "totals": totals,
        },
        f"已导入 {len(results)} 个场景"
        + (f"，跳过 {len(skipped)} 个。" if skipped else "。"),
    )


def pkg_summary_safe(pkg: Any) -> dict[str, Any]:
    try:
        return pkg.summary()
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["bp"]
