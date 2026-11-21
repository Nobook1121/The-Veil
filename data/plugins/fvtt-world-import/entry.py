"""FVTT 世界包导入插件入口：审计（inspect）与落库（run）HTTP 蓝图。

本刀范围：场景（含墙/光/棋子）→ veil-map；actors → dnd5e 角色卡 + 剧本
NPC statblock 模块。journal→世界树是后续刀，不在本文件里出现半成品。

落库走 ``plugins.runtime.get_service("veil-map", "map_service")`` 的
``import_scene``——插件之间不直接 import 对方的 Python 模块；veil-map 缺席
时返回可读降级错误（503），不写任何数据。inspect 只审计、不落任何持久
数据，临时目录用完即删。

第三刀（actors）：``rooms/<id>/actors/run`` 把世界包里的 character 落成
当前用户的角色卡（核心 ``character_service.apply_save``，配额写盘前整体
预检），NPC 并入房间绑定剧本（``save_scenario_record`` 升版 +
``bind_room_to_scenario_version`` 重钉房间）；房间规则集不是 dnd5e 时整体
拒绝。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from typing import Any, Callable

from flask import Blueprint, current_app, request, session

from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_rules import resolve_rule_id, save_room_info
from trpg_server.ruleset import RulesetSelectionError

from fvtt_world_import import convert_scenes, world_package
from fvtt_world_import.convert_actors import convert_world_actors
from fvtt_world_import.convert_scenes import convert_world_scenes
from fvtt_world_import.leveldb import LevelDbError
from fvtt_world_import.world_package import PackageError, extract_package, read_actors, read_scenes

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


# -- actors 落库（第三刀）--------------------------------------------------

#: 头像内嵌上限：与核心 ``character_service`` 的 4MB 头像写盘上限对齐。
ACTORS_MAX_AVATAR_BYTES = 4 * 1024 * 1024
_AVATAR_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _avatar_data_uri_factory(pkg: Any) -> Callable[[str], str | None]:
    """``actor.img`` → 包内头像 data URI（≤4MB、png/jpg/gif/webp）。

    解析失败返回 None：转换层据此落「头像留空」警告（img.missing /
    img.system_asset），导入不中断。
    """

    def avatar_data_uri(img_src: str) -> str | None:
        rel = pkg.resolve_background(img_src)
        if rel is None:
            return None
        mime = _AVATAR_MIME_BY_EXT.get(os.path.splitext(str(rel))[1].lower())
        if mime is None:
            return None
        try:
            data = pkg.read_asset(rel)
        except (OSError, KeyError, RuntimeError):
            return None
        if len(data) > ACTORS_MAX_AVATAR_BYTES:
            return None
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    return avatar_data_uri


def _dnd5e_slot_and_weapon_callbacks() -> tuple[Callable | None, Callable | None]:
    """从 dnd5e 数据包注入法术位表 / 武器中文名回调。

    dnd5e-srd（classes.json / equipment.json）未装载时返回 ``(None, None)``：
    转换层回落「源包剩余值当上限 + 警告」，不拒收整个包。
    """
    try:
        from trpg_server.plugins.runtime import content_packs_for

        library = content_packs_for("dnd5e")
        classes = {
            str(row.get("id") or "").strip().casefold(): row
            for row in (library.entries("dnd5e", "class") or [])
        }
        equipment = {
            str(row.get("id") or "").strip().casefold(): row
            for row in (library.entries("dnd5e", "equipment") or [])
        }
    except Exception:  # noqa: BLE001 - 数据包运行期缺席时按空表降级
        return None, None
    if not classes and not equipment:
        return None, None

    def slot_table(class_identifier: str, level: int) -> list[int] | None:
        row = classes.get(str(class_identifier or "").strip().casefold())
        if row is None:
            return None
        casting = row.get("spellcasting") if isinstance(row.get("spellcasting"), dict) else {}
        table = casting.get("slot_table")
        if not isinstance(table, list) or not table:
            return None
        index = max(1, min(20, int(level or 1))) - 1
        if index >= len(table) or not isinstance(table[index], list):
            return None
        return [int(value) for value in table[index]]

    def weapon_name(code: str) -> str | None:
        row = equipment.get(str(code or "").strip().casefold())
        if row is None:
            return None
        locales = row.get("locales") if isinstance(row.get("locales"), dict) else {}
        zh = locales.get("zh-CN") if isinstance(locales.get("zh-CN"), dict) else {}
        name = str((zh or {}).get("name") or row.get("name") or "").strip()
        return name or None

    return slot_table, weapon_name


def _character_service_actor() -> Any:
    """当前会话 → 角色卡领域服务的 Actor（学 routes/characters.py 的 build→session 回落）。"""
    from trpg_server.site_agent_auth import Actor, AgentAuthError, build_actor, permission_set_for_role

    try:
        return build_actor()
    except AgentAuthError:
        role = str(session.get("role") or "USER").upper()
        return Actor(
            user_id=int(session.get("user_id") or 0),
            username=str(session.get("username") or ""),
            role=role,
            permissions=permission_set_for_role(role),
            room_ids=frozenset(),
        )


def _held_card_count(actor: Any) -> int:
    """当前用户已持有的角色卡数（playerId ∈ {user_id, username}）。"""
    try:
        from trpg_server.character_service import _owned_count

        return int(_owned_count(actor))
    except Exception:  # noqa: BLE001 - 预检尽力而为；apply_save 仍有逐卡硬校验
        logger.warning("fvtt_actors_quota_precheck_failed", exc_info=True)
        return 0


def _max_cards_per_user() -> int:
    from trpg_server.config.merge import load_settings

    config_dir = current_app.config.get("CONFIG_DIR")
    data = load_settings(config_dir).data
    rules = data.get("character_rules") if isinstance(data.get("character_rules"), dict) else {}
    try:
        return max(0, int(rules.get("max_cards_per_user", 3)))
    except (TypeError, ValueError):
        return 3


def _actor_is_elevated(actor: Any) -> bool:
    """与 ``character_service._is_elevated`` 同口径：characters.manage_all。"""
    try:
        return bool(actor.has("characters.manage_all"))
    except Exception:  # noqa: BLE001
        return False


def _append_npc_modules(scenario: dict[str, Any], modules: list[dict[str, Any]]) -> None:
    """幂等并入 NPC 模块：同 ``fvtt_actor_id``（或同模块 id）的旧模块先移除再追加。"""
    new_ids = {str(row.get("id") or "") for row in modules}
    new_actor_ids = {str(row.get("fvtt_actor_id") or "") for row in modules}
    new_ids.discard("")
    new_actor_ids.discard("")
    kept: list[Any] = []
    for row in scenario.get("modules") or []:
        if isinstance(row, dict):
            actor_id = str(row.get("fvtt_actor_id") or "")
            module_id = str(row.get("id") or "")
            if (actor_id and actor_id in new_actor_ids) or (module_id and module_id in new_ids):
                continue
        kept.append(row)
    scenario["modules"] = kept + list(modules)


def _npc_scenario_repin(room_dir: Any, info: dict[str, Any], npcs: list[dict[str, Any]]):
    """NPC 模块并入房间绑定剧本 → 升版 → 重钉房间；返回 (scenario_id, version) 或错误响应。"""
    from pathlib import Path

    from trpg_server.agents.versioning import bind_room_to_scenario_version, next_scenario_version, normalize_semver
    from trpg_server.scenario_store import load_scenario_by_id, save_scenario_record

    scenario_id = str(info.get("scenario_id") or "").strip()
    if not scenario_id:
        return None, error_response(
            "房间没有绑定剧本，无法导入 NPC；请先选择剧本再导入。",
            400,
            "fvtt.scenario_missing",
        )
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR") or "")
    descriptor, scenario = load_scenario_by_id(scenarios_dir, scenario_id)
    if not descriptor or not scenario:
        return None, error_response(
            f"房间绑定的剧本（{scenario_id}）不存在，无法导入 NPC。",
            400,
            "fvtt.scenario_missing",
        )
    _append_npc_modules(scenario, [row["module"] for row in npcs])
    current = str(scenario.get("scenario_version") or scenario.get("version") or "1.0.0")
    scenario["scenario_version"] = normalize_semver(next_scenario_version(normalize_semver(current)))
    try:
        save_scenario_record(scenarios_dir, scenario, existing_descriptor=descriptor)
    except (OSError, ValueError) as exc:  # noqa: BLE001 - 剧本写入失败按 500 可读错误处理
        logger.warning("fvtt_actors_scenario_write_failed", exc_info=True)
        return None, error_response(f"剧本写入失败：{exc}", 500, "fvtt.scenario_write_failed")
    pinned = bind_room_to_scenario_version(info, scenario)
    save_room_info(room_dir, pinned)
    return {"scenario_id": scenario_id, "scenario_version": scenario["scenario_version"]}, None


@bp.route("/rooms/<room_id>/actors/run", methods=["POST"])
def run_actors_import(room_id: str):
    """世界包 actors → dnd5e 角色卡 + 剧本 NPC statblock 模块。

    - character：逐卡 ``character_service.apply_save``（信封打包 + 头像落盘 +
      房间卡快照同步）；配额在写盘前整体预检，超额整批拒绝；
    - npc：并入房间绑定剧本（visibility "kp"）→ 升版 → 重钉房间；无绑定剧本
      且包里有 NPC 时 400；
    - vehicle：按降级清单只报告不落库。
    """
    room_dir, info, denied = _room_and_manager(room_id)
    if denied:
        return denied

    # 规则集门闸：学 veil-map 的 _ruleset_enabled（解析失败转 400 可读错误）。
    try:
        ruleset_id = str(
            resolve_rule_id(info, scenarios_dir=current_app.config.get("SCENARIOS_DIR")) or ""
        ).strip().lower()
    except RulesetSelectionError as exc:
        return error_response(str(exc.message or exc), 400, "fvtt.ruleset_unavailable")
    if ruleset_id != "dnd5e":
        return error_response(
            "只有 dnd5e 房间可以导入角色与 NPC（当前房间规则集不支持）。",
            400,
            "fvtt.ruleset_unsupported",
        )

    try:
        raw = _read_upload()
    except PackageError as exc:
        return _package_error_response(exc)

    slot_table, weapon_name = _dnd5e_slot_and_weapon_callbacks()

    with tempfile.TemporaryDirectory(prefix="fvtt-actors-") as workdir:
        pkg = None
        try:
            pkg = extract_package(raw, workdir)
            index = read_actors(pkg)
        except PackageError as exc:
            return _package_error_response(exc)
        except LevelDbError as exc:
            return _package_error_response(
                PackageError("fvtt.leveldb_invalid", f"角色集合（LevelDB）解析失败：{exc}")
            )
        except Exception as exc:  # noqa: BLE001 - LevelDB 结构问题转可读错误
            logger.warning("fvtt_world_read_failed room=%s", room_id, exc_info=True)
            return error_response(f"世界包数据读取失败：{exc}", 400, "fvtt.leveldb_invalid")

        converted = convert_world_actors(
            index,
            slot_table=slot_table,
            weapon_name=weapon_name,
            avatar_data_uri=_avatar_data_uri_factory(pkg),
        )
        _close_package(pkg)

        # 配额预检：写盘前整体拒绝（apply_save 的逐卡校验与本口径一致，
        # 预检通过后逐卡不会中途撞上限）。
        incoming_characters = converted["characters"]
        actor = _character_service_actor()
        if incoming_characters and not _actor_is_elevated(actor):
            held = _held_card_count(actor)
            if held + len(incoming_characters) > _max_cards_per_user():
                return error_response(
                    f"角色卡数量将超过上限（已持 {held} 张 + 本包 "
                    f"{len(incoming_characters)} 张 > 上限 {_max_cards_per_user()} 张），整包未导入。",
                    403,
                    "fvtt.character_quota",
                )

        characters: list[dict[str, Any]] = []
        try:
            from trpg_server import character_card_io, character_service

            for row in incoming_characters:
                character_id = character_card_io.new_unique_character_id()
                saved = character_service.apply_save(actor, character_id, dict(row["card"]))
                characters.append(
                    {
                        "character_id": saved.get("id") or character_id,
                        "name": saved.get("name") or row["name"],
                        "level": row["level"],
                        "folder": row["folder"],
                        "warnings": list(row["warnings"]),
                    }
                )
        except character_service.CharacterServiceError as exc:
            return error_response(str(exc.message or exc), int(exc.status or 400), str(exc.code or "fvtt.character_save_failed"))

        scenario_repin = None
        if converted["npcs"]:
            scenario_repin, scenario_error = _npc_scenario_repin(room_dir, info, converted["npcs"])
            if scenario_error is not None:
                return scenario_error

        npcs = [
            {
                "module_id": row["module"]["id"],
                "title": row["title"],
                "cr": row["cr"],
                "armor_class": row["armor_class"],
                "max_hp": row["max_hp"],
                "warnings": list(row["warnings"]),
            }
            for row in converted["npcs"]
        ]
        totals = dict(converted["totals"])
        payload = {
            "world": pkg_summary_safe(pkg),
            "characters": characters,
            "npcs": npcs,
            "vehicles": converted["vehicles"],
            "warnings": converted["warnings"],
            "totals": totals,
        }
        if scenario_repin is not None:
            payload["scenario"] = scenario_repin

    message = (
        f"已导入 {len(characters)} 张角色卡、{len(npcs)} 个 NPC 模块"
        + (f"，{totals.get('vehicles', 0)} 个载具未导入（无战斗数值）。" if totals.get("vehicles") else "。")
    )
    return success_response(payload, message)


__all__ = ["bp"]
