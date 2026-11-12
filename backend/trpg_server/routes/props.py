"""道具工坊 REST 接口（设计稿 §5.2）。

- ``GET    /api/rooms/<room_id>/props/templates``  模板清单
- ``GET    /api/rooms/<room_id>/props``            道具索引（按可见性过滤）
- ``POST   /api/rooms/<room_id>/props``            创建
- ``GET    /api/rooms/<room_id>/props/<prop_id>``  单个（按可见性脱敏）
- ``PATCH  /api/rooms/<room_id>/props/<prop_id>``  修改
- ``DELETE /api/rooms/<room_id>/props/<prop_id>``  删除
- ``GET    /api/rooms/<room_id>/props/<prop_id>/render`` 渲染 PDF/PNG
- ``POST   /api/rooms/<room_id>/props/<prop_id>/reveal`` 投递
- ``POST   /api/rooms/<room_id>/props/<prop_id>/redact`` 追加脱敏区间

读写权限：站点管理员 / 房主 / 房管；玩家只能 GET 已揭示的道具，未揭示一律
404（不泄露存在性）。所有路径经 ``store`` 内的 ``safe_join``。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Mapping

from flask import Blueprint, current_app, request, send_file, session

from trpg_server import props as props_config
from trpg_server.props import draft, image, normalize, policy, registry, store
from trpg_server.props.render import RenderError, render_cached
from trpg_server.responses import error_response, success_response
from trpg_server.room_log.fonts import FontMissing, resolve_font
from trpg_server.routes.rooms import (
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_OWNER,
    _can_access,
    _find_member,
    _find_room,
    _is_elevated,
    _read_messages,
    _require_login,
    _room_permission,
    _timestamp,
    _write_messages,
)

bp = Blueprint("props", __name__)
logger = logging.getLogger(__name__)

_RENDER_SEMAPHORE = threading.Semaphore(2)
_RENDER_MAX_WAITING = 8
_RENDER_ACQUIRE_TIMEOUT = 15.0
_RENDER_WAITING = 0
_RENDER_WAITING_LOCK = threading.Lock()
_FORMATS = {"pdf": "application/pdf", "png": "image/png"}
_WRITE_ROLES = {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}
_WRITE_KEYS = {"title", "fields", "style", "wear", "visibility", "redaction"}


def _settings() -> dict[str, Any]:
    return props_config.props_settings(current_app.config.get("CONFIG_DIR"))


def _disabled() -> bool:
    return not bool(_settings().get("enabled", True))


def _disabled_response():
    return error_response("Prop workshop is disabled", 403, "prop.disabled")


def _not_found():
    return error_response("Prop not found", 404, "prop.not_found")


def _load_room(room_id: str):
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return None, None, error_response("Room not found", 404, "prop.room_not_found")
    return room_dir, info, None


def _is_kp(info: Mapping[str, Any]) -> bool:
    if _is_elevated():
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in _WRITE_ROLES


def _write_forbidden():
    return error_response("Permission denied", 403, "prop.forbidden")


def _public_unavailable() -> list[dict[str, Any]]:
    """剥掉服务端绝对路径 ``dir``，只保留 id/source/reason（C7）。"""
    return [
        {key: value for key, value in item.items() if key != "dir"}
        for item in registry.list_unavailable()
        if isinstance(item, Mapping)
    ]


# ---------------------------------------------------------------- 参数规整


# 规整逻辑统一收敛在 ``props/normalize.py``（B3）；这里保留薄封装，
# 让路由侧继续用「strict=True」把非法请求体映射成 prop.invalid_body。
BodyError = normalize.NormalizeError


def _payload() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return dict(data) if isinstance(data, Mapping) else {}


def _normalize_title(raw: Any, fallback: str) -> str:
    return normalize.normalize_title(raw, fallback)


def _normalize_style(raw: Any, previous: Mapping[str, Any] | None = None) -> dict[str, str]:
    return normalize.normalize_style(raw, previous)


def _normalize_wear(raw: Any, previous: Mapping[str, Any] | None = None) -> dict[str, float]:
    return normalize.normalize_wear(raw, previous)


def _normalize_redaction(raw: Any) -> list[dict[str, Any]]:
    return normalize.normalize_redaction(raw, strict=True)


def _normalize_visibility(raw: Any, previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return normalize.normalize_visibility(raw, previous)


def _clamp_redaction(spec: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize.clamp_redaction(spec, items)


def _build_spec(room_id: str, body: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    template_id = str(body.get("template_id") or "").strip()
    manifest = registry.get_template(template_id)
    fields = registry.validate_fields(
        template_id,
        body.get("fields") if isinstance(body.get("fields"), Mapping) else {},
        max_field_chars=int(settings.get("max_field_chars") or 0) or None,
    )
    now = _timestamp()
    spec = {
        "schema_version": 1,
        "id": store.new_prop_id(),
        "room_id": str(room_id),
        "template_id": template_id,
        "title": _normalize_title(body.get("title"), str(manifest.get("name") or template_id)),
        "fields": fields,
        "style": _normalize_style(body.get("style")),
        "wear": _normalize_wear(body.get("wear")),
        "redaction": _normalize_redaction(body.get("redaction") or []),
        "visibility": _normalize_visibility(body.get("visibility")),
        "source": _normalize_source(body.get("source")),
        "assets": {},
        "created_by": _user_id(),
        "created_at": now,
        "updated_at": now,
    }
    spec["redaction"] = _clamp_redaction(spec, spec["redaction"])
    return spec


def _normalize_source(raw: Any) -> dict[str, Any]:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    kind = str(source.get("kind") or "manual").strip().lower()
    if kind not in {"ai", "manual", "imported"}:
        kind = "manual"
    return {
        "kind": kind,
        "clue_id": source.get("clue_id"),
        "scenario_id": source.get("scenario_id"),
        "ai_request_id": source.get("ai_request_id"),
    }


def _user_id() -> int | str | None:
    value = session.get("user_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------- 模板与索引


@bp.route("/api/rooms/<room_id>/props/templates", methods=["GET"])
def list_prop_templates(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    _room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    return success_response(
        {
            "templates": registry.list_templates(),
            "groups": registry.list_template_groups(),
            "unavailable": _public_unavailable(),
        },
        "Prop templates loaded",
    )


@bp.route("/api/props/templates/rescan", methods=["POST"])
def rescan_prop_templates():
    """重新扫描内置 + 用户模板；仅站点管理员可触发。"""
    login_error = _require_login()
    if login_error:
        return login_error
    if str(session.get("role") or "").upper() not in {"ADMIN", "OWNER"}:
        return error_response("Forbidden", 403, "prop.forbidden")
    if _disabled():
        return _disabled_response()
    result = registry.rescan()
    result["unavailable"] = [
        {key: value for key, value in item.items() if key != "dir"}
        for item in result.get("unavailable") or []
        if isinstance(item, Mapping)
    ]
    return success_response(result, "Prop templates rescanned")


@bp.route("/api/rooms/<room_id>/props/draft", methods=["POST"])
def draft_prop(room_id):
    """AI 起草：只做一次小请求，失败返回可读错误让编辑器提示手动填写。"""
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    if not settings.get("enabled", True):
        return _disabled_response()
    if not settings.get("ai_draft_enabled", True):
        return error_response("AI draft is disabled", 403, "prop.draft_disabled")
    _room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()

    body = _payload()
    try:
        result = draft.draft_fields(
            str(body.get("template_id") or ""),
            {
                "era": body.get("era"),
                "place": body.get("place"),
                "clues": body.get("clues"),
                "tone": body.get("tone"),
            },
            config_dir=current_app.config.get("CONFIG_DIR"),
            max_field_chars=int(settings.get("max_field_chars") or 0) or None,
        )
    except draft.DraftError as exc:
        return error_response(str(exc), 502, "prop.draft_failed")
    except Exception:  # noqa: BLE001 - 起草是辅助功能，失败不能影响编辑器
        logger.exception("props draft failed room=%s", room_id)
        return error_response("AI 起草失败，请手动填写。", 502, "prop.draft_failed")
    return success_response(result, "Prop draft ready")


@bp.route("/api/rooms/<room_id>/props", methods=["GET"])
def list_props(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()

    is_kp = _is_kp(info)
    user_id = session.get("user_id")
    entries: list[dict[str, Any]] = []
    for entry in store.list_index(room_dir):
        spec = store.read_prop(room_dir, str(entry.get("id") or ""))
        if spec is None:
            continue
        view = policy.visible_spec(spec, user_id, is_kp=is_kp)
        if view is None:
            continue
        entries.append(store.index_entry(spec))
    return success_response({"props": entries}, "Props loaded")


# ---------------------------------------------------------------- 创建


@bp.route("/api/rooms/<room_id>/props", methods=["POST"])
def create_prop(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    if not settings.get("enabled", True):
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if len(store.list_index(room_dir)) >= int(settings.get("max_props_per_room") or 200):
        return error_response("Prop limit reached", 400, "prop.limit_reached")

    body = _payload()
    try:
        spec = _build_spec(room_id, body, settings)
    except registry.UnknownTemplate as exc:
        return error_response(str(exc), 400, "prop.unknown_template")
    except registry.FieldError as exc:
        return error_response(str(exc), 400, "prop.invalid_fields")
    except BodyError as exc:
        return error_response(str(exc), 400, "prop.invalid_body")

    store.write_prop(room_dir, spec)
    return success_response(store.read_prop(room_dir, spec["id"]), "Prop created", status=201)


# ---------------------------------------------------------------- 单个


@bp.route("/api/rooms/<room_id>/props/<prop_id>", methods=["GET"])
def get_prop(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()
    spec = store.read_prop(room_dir, prop_id)
    view = policy.visible_spec(spec, session.get("user_id"), is_kp=_is_kp(info))
    if view is None:
        return _not_found()
    return success_response(view, "Prop loaded")


@bp.route("/api/rooms/<room_id>/props/<prop_id>", methods=["PATCH"])
def update_prop(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    if not settings.get("enabled", True):
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()
    body = _payload()
    unknown = set(body) - _WRITE_KEYS
    if unknown:
        return error_response("不可修改的字段：" + ", ".join(sorted(unknown)), 400, "prop.invalid_body")

    failure: dict[str, Any] = {}

    def mutate(current: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if "title" in body:
                current["title"] = _normalize_title(body.get("title"), str(current.get("title") or ""))
            if "fields" in body:
                merged = dict(current.get("fields") or {})
                if isinstance(body.get("fields"), Mapping):
                    merged.update(body["fields"])
                current["fields"] = registry.validate_fields(
                    str(current.get("template_id") or ""),
                    merged,
                    max_field_chars=int(settings.get("max_field_chars") or 0) or None,
                )
            if "style" in body:
                current["style"] = _normalize_style(body.get("style"), current.get("style"))
            if "wear" in body:
                current["wear"] = _normalize_wear(body.get("wear"), current.get("wear"))
            if "visibility" in body:
                current["visibility"] = _normalize_visibility(body.get("visibility"))
            if "redaction" in body:
                current["redaction"] = _normalize_redaction(body.get("redaction"))
            current["redaction"] = _clamp_redaction(current, current.get("redaction") or [])
            current["updated_at"] = _timestamp()
            return current
        except (registry.TemplateError, BodyError) as exc:
            failure["error"] = exc
            return None

    updated = store.update_prop(room_dir, prop_id, mutate)
    if failure:
        exc = failure["error"]
        code = "prop.invalid_fields" if isinstance(exc, registry.TemplateError) else "prop.invalid_body"
        return error_response(str(exc), 400, code)
    if updated is None:
        return _not_found()
    return success_response(updated, "Prop updated")


@bp.route("/api/rooms/<room_id>/props/<prop_id>", methods=["DELETE"])
def delete_prop(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()
    try:
        deleted = store.delete_prop(room_dir, prop_id)
    except (OSError, store.PropStoreError):
        trace_id = uuid.uuid4().hex[:12]
        logger.exception(
            "props delete failed room=%s id=%s trace=%s", room_id, prop_id, trace_id
        )
        return error_response(
            "Prop delete failed", 500, "prop.delete_failed", trace_id=trace_id
        )
    return success_response({"deleted": bool(deleted)}, "Prop deleted")


# ---------------------------------------------------------------- 渲染


@bp.route("/api/rooms/<room_id>/props/<prop_id>/render", methods=["GET"])
def render_prop_file(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    if not settings.get("enabled", True):
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()

    fmt = str(request.args.get("format") or "pdf").strip().lower()
    if fmt not in _FORMATS:
        return error_response("format 只支持 pdf / png", 400, "prop.invalid_format")
    spec = store.read_prop(room_dir, prop_id)
    is_kp = _is_kp(info)
    # 照片模板：KP 首次预览时补生成图片；失败只降级为空白框，不阻断渲染。
    if spec is not None and str(spec.get("template_id") or "") == "photo" and is_kp:
        try:
            image.ensure_photo(
                room_dir,
                spec,
                config_dir=current_app.config.get("CONFIG_DIR"),
                settings=settings,
            )
            spec = store.read_prop(room_dir, prop_id) or spec
        except Exception:  # noqa: BLE001 - 生图失败不应让渲染 500
            logger.warning("props photo ensure failed room=%s id=%s", room_id, prop_id, exc_info=True)
    view = policy.visible_spec(spec, session.get("user_id"), is_kp=is_kp)
    if view is None:
        return _not_found()

    trace_id = uuid.uuid4().hex[:12]
    try:
        _family, font_path = resolve_font()
    except FontMissing:
        return error_response("No usable CJK font", 503, "prop.font_missing")

    global _RENDER_WAITING
    with _RENDER_WAITING_LOCK:
        if _RENDER_WAITING >= _RENDER_MAX_WAITING:
            return error_response("Render queue is full", 429, "prop.render_busy")
        _RENDER_WAITING += 1
    try:
        acquired = _RENDER_SEMAPHORE.acquire(timeout=_RENDER_ACQUIRE_TIMEOUT)
    finally:
        with _RENDER_WAITING_LOCK:
            _RENDER_WAITING -= 1
    if not acquired:
        return error_response("Render queue timeout", 503, "prop.render_timeout")

    try:
        result = render_cached(
            room_dir,
            view,
            fmt,
            settings=settings,
            font_path=font_path,
            data_dir=current_app.config.get("DATA_DIR"),
        )
    except RenderError:
        logger.exception("props render failed room=%s id=%s trace=%s", room_id, prop_id, trace_id)
        return error_response("Prop render failed", 500, "prop.render_failed", trace_id=trace_id)
    except Exception:  # noqa: BLE001 - 不把内部异常文本回给客户端
        logger.exception("props render failed room=%s id=%s trace=%s", room_id, prop_id, trace_id)
        return error_response("Prop render failed", 500, "prop.render_failed", trace_id=trace_id)
    finally:
        _RENDER_SEMAPHORE.release()

    title = str(view.get("title") or prop_id)
    response = send_file(
        result["path"],
        mimetype=_FORMATS[fmt],
        as_attachment=False,
        download_name=f"{title}.{fmt}",
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Prop-Render-Cached"] = "1" if result.get("cached") else "0"
    return response


# ---------------------------------------------------------------- 投递与脱敏


@bp.route("/api/rooms/<room_id>/props/<prop_id>/reveal", methods=["POST"])
def reveal_prop(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()

    body = _payload()
    recipients: list[int] = []
    if body.get("all") is True:
        visibility = {"mode": "revealed", "revealed_to": []}
    else:
        raw = body.get("to")
        if not isinstance(raw, (list, tuple)) or not raw:
            return error_response("需要 to 数组或 all=true", 400, "prop.invalid_body")
        member_ids = {
            str(member.get("user_id"))
            for member in info.get("members") or []
            if member.get("is_active", True) is not False
        }
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if str(value) in member_ids and value not in recipients:
                recipients.append(value)
        if not recipients:
            return error_response("收件人不在房间内", 400, "prop.invalid_recipients")
        visibility = {"mode": "partial", "revealed_to": recipients}

    updated = store.update_prop(
        room_dir,
        prop_id,
        lambda current: {**current, "visibility": visibility, "updated_at": _timestamp()},
    )
    if updated is None:
        return _not_found()

    # partial 投递只把 socket 推送给收件人与 KP/房管，避免非收件人看到卡片（C1）。
    audience: list[int] | None = None
    if visibility["mode"] == "partial":
        audience = list(recipients)
        for member in info.get("members") or []:
            if member.get("is_active", True) is False:
                continue
            if str(member.get("room_role") or "") not in _WRITE_ROLES:
                continue
            try:
                value = int(member.get("user_id"))
            except (TypeError, ValueError):
                continue
            if value not in audience:
                audience.append(value)

    content = str(body.get("message") or "").strip() or f"递来一件道具：{updated.get('title') or ''}"
    message = {
        "id": uuid.uuid4().hex,
        "type": "prop",
        "sender_id": None,
        "sender_name": "KP",
        "avatar": "/assets/avatars/default_kp.jpg",
        "content": content[:500],
        "time": time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": {
            "prop_id": prop_id,
            "preview": str(updated.get("template_id") or ""),
            "revealed_to": recipients,
        },
    }
    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)

    try:
        from trpg_server.immersive import broadcast

        broadcast.emit(
            "new_message", {**message, "room_id": str(room_id)}, audience=audience
        )
    except Exception:  # noqa: BLE001 - 广播失败不回滚已落库的消息
        logger.warning("props reveal broadcast failed room=%s id=%s", room_id, prop_id, exc_info=True)

    return success_response({"prop": updated, "message": message}, "Prop revealed")


@bp.route("/api/rooms/<room_id>/props/<prop_id>/redact", methods=["POST"])
def redact_prop(room_id, prop_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if _disabled():
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()

    body = _payload()
    raw = body.get("redactions") if "redactions" in body else [body]
    try:
        incoming = _normalize_redaction(raw)
    except BodyError as exc:
        return error_response(str(exc), 400, "prop.invalid_body")

    failure: dict[str, Any] = {}

    def mutate(current: dict[str, Any]) -> dict[str, Any] | None:
        merged = list(current.get("redaction") or []) + incoming
        current["redaction"] = _clamp_redaction(current, merged)
        if not current["redaction"]:
            failure["empty"] = True
            return None
        current["updated_at"] = _timestamp()
        return current

    updated = store.update_prop(room_dir, prop_id, mutate)
    if updated is None:
        if failure.get("empty"):
            return error_response("脱敏区间无效", 400, "prop.invalid_body")
        return _not_found()
    return success_response(updated, "Prop redaction added")


@bp.route("/api/rooms/<room_id>/props/<prop_id>/photo", methods=["POST"])
def generate_prop_photo(room_id, prop_id):
    """显式生成/重绘照片；预算耗尽或失败返回 degraded，不视为 500。"""
    login_error = _require_login()
    if login_error:
        return login_error
    settings = _settings()
    if not settings.get("enabled", True):
        return _disabled_response()
    room_dir, info, error = _load_room(room_id)
    if error:
        return error
    if not _can_access(info):
        return _write_forbidden()
    if not _is_kp(info):
        return _write_forbidden()
    if not store.is_valid_prop_id(prop_id):
        return _not_found()
    spec = store.read_prop(room_dir, prop_id)
    if spec is None:
        return _not_found()
    if str(spec.get("template_id") or "") != "photo":
        return error_response("Only photo props support images", 400, "prop.invalid_template")

    body = _payload()
    result = image.ensure_photo(
        room_dir,
        spec,
        config_dir=current_app.config.get("CONFIG_DIR"),
        settings=settings,
        force=bool(body.get("force")),
    )
    message = "Photo ready" if result.get("ok") else "Photo degraded"
    return success_response(result, message)


__all__ = ["bp"]
