import json
import logging
import re
import shutil
import time

import requests
from flask import Blueprint, current_app, request, session

from trpg_server.ai_capabilities import chat_completions_endpoint, probe_responses_api
from trpg_server.ai_platform_config import (
    load_platform_config,
    load_public_platform_config,
    ordered_platform_paths,
    save_platform_config,
)
from trpg_server.json_store import write_json_atomic
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.permission_config import load_permission_config, permission_config_path, save_permission_config
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.role_config import enabled_provider_options, load_roles, save_role
from trpg_server.security import require_permission_node, safe_join
from trpg_server.settings import AI_PLATFORM_SECRET_DIR, CONFIG_DIR

bp = Blueprint("config", __name__)
logger = logging.getLogger(__name__)
_BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CONFIG_LABELS = {
    "general": "通用设置",
}


def _get_config_dir():
    return current_app.config.get("CONFIG_DIR", CONFIG_DIR)


def _get_ai_platform_dir():
    return current_app.config.get("AI_PLATFORM_DIR", _get_config_dir() / "aiplatform")


def _get_ai_platform_secret_dir():
    return current_app.config.get("AI_PLATFORM_SECRET_DIR", AI_PLATFORM_SECRET_DIR)


def _get_ai_model_dir():
    return current_app.config.get("AI_MODEL_DIR", _get_config_dir() / "aimodel")


def _get_kp_prompt_file():
    return current_app.config.get("KP_PROMPT_FILE", _get_config_dir() / "roles" / "kp.md")


def _get_debug_kp_prompt_file():
    return current_app.config.get("DEBUG_KP_PROMPT_FILE", _get_config_dir() / "roles" / "debug-kp.md")


def _get_role_config_file():
    return current_app.config.get("ROLE_CONFIG_FILE", _get_config_dir() / "roles" / "roles.json")


def _get_permission_config_file():
    return current_app.config.get("PERMISSION_CONFIG_FILE", permission_config_path(_get_config_dir()))


def _config_permission_node(config_name):
    return {
        "general": "settings.general",
    }.get(config_name, "settings.general")


def _current_role_can(node_id):
    return load_permission_config(_get_permission_config_file())["matrix"].get(node_id, []).count(session.get("role", "USER")) > 0


def _format_toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _format_toml_key(key):
    key_text = str(key)
    if _BARE_TOML_KEY_RE.fullmatch(key_text):
        return key_text
    return json.dumps(key_text, ensure_ascii=False)


def _normalize_toml_name(name):
    """剥离历史脏数据中反复叠加的引号转义，使段名收敛到基名。

    早期版本把 ``ai.small_models`` 写成 ``["ai.small_models"]``，读取时又未去引号，
    前端整对象回写后引号被再次转义，导致每次保存转义翻倍。这里做有界收敛。
    """
    text = str(name).strip()
    for _ in range(8):
        if len(text) < 2 or text[0] != text[-1] or text[0] not in {'"', "'"}:
            break
        if text[0] == '"':
            try:
                unquoted = json.loads(text)
            except ValueError:
                break
            if not isinstance(unquoted, str) or unquoted == text:
                break
            text = unquoted
        else:
            text = text[1:-1]
    return text


def _format_toml_section(section):
    # 点号分隔的段名（如 ai.small_models）按 TOML 点分键原样输出；
    # 否则 json.dumps 会写成 ["ai.small_models"] 并触发读取侧的引号叠加问题。
    section_text = _normalize_toml_name(section)
    parts = section_text.split(".")
    if section_text and all(_BARE_TOML_KEY_RE.fullmatch(part) for part in parts):
        return ".".join(parts)
    return _format_toml_key(section_text)


def convert_to_toml(config_data):
    lines = []
    for section, values in config_data.items():
        if not isinstance(values, dict):
            continue

        lines.append(f"[{_format_toml_section(section)}]")
        for key, value in values.items():
            lines.append(f"{_format_toml_key(key)} = {_format_toml_value(value)}")
        lines.append("")

    return "\n".join(lines)


@bp.route("/api/config/<config_name>", methods=["POST"])
def save_config(config_name):
    try:
        if not _current_role_can(_config_permission_node(config_name)):
            return error_response("Permission denied", 403, "Permission denied")

        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Invalid config data", 400)

        config_path = safe_join(_get_config_dir(), f"{config_name}.toml")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(convert_to_toml(config_data), encoding="utf-8")

        log_user_action(
            logger,
            user_action_text(session.get("username"), f"更改了{CONFIG_LABELS.get(config_name, config_name + '设置')}"),
            用户ID=session.get("user_id"),
            文件=config_path.name,
        )
        return success_response(message="Config saved successfully")
    except Exception as exc:
        logger.exception("Failed to save config: %s", config_name)
        return server_error("Save failed")


@bp.route("/api/config/permissions", methods=["GET"])
@require_permission_node("settings.permissions")
def get_permission_config():
    try:
        return success_response(data=load_permission_config(_get_permission_config_file()))
    except Exception as exc:
        logger.exception("Failed to load permission config")
        return server_error("Load failed")


@bp.route("/api/config/permissions", methods=["POST"])
@require_permission_node("settings.permissions")
def save_permissions():
    try:
        permission_data = request.get_json(silent=True)
        if not isinstance(permission_data, dict):
            return error_response("Invalid permission config data", 400)

        config = save_permission_config(_get_permission_config_file(), permission_data)
        log_user_action(
            logger,
            user_action_text(session.get("username"), "更新了权限配置"),
            用户ID=session.get("user_id"),
        )
        return success_response(message="Permission config saved successfully", data=config)
    except Exception as exc:
        logger.exception("Failed to save permission config")
        return server_error("Save failed")


@bp.route("/api/config/aiplatform", methods=["GET"])
@require_permission_node("settings.ai_models")
def list_ai_platform_configs():
    """列出磁盘上的全部 AI 平台配置（内置 + 自定义）。

    只回传公开配置：``load_public_platform_config`` 会把 ``api_key`` 拆分到独立
    secret 目录并把它从返回结果里剔除，避免密钥经此接口明文回传。
    """
    try:
        platform_dir = _get_ai_platform_dir()
        secret_dir = _get_ai_platform_secret_dir()
        platforms = []
        # 平台目录里还放着 default-request.json（模型请求模板），它不是平台配置，跳过。
        # ordered_platform_paths 已按各平台配置的 order 升序返回，前端据此展示拖动顺序。
        for public_path in ordered_platform_paths(platform_dir):
            if public_path.name == "default-request.json":
                continue
            try:
                config = load_public_platform_config(public_path, secret_dir / public_path.name)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Failed to load AI platform config: %s", public_path.name)
                continue
            if not isinstance(config, dict):
                continue
            config.setdefault("platform", public_path.stem)
            platforms.append(config)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "查看了 AI 平台列表"),
            用户ID=session.get("user_id"),
            平台数=len(platforms),
        )
        return success_response(data={"platforms": platforms})
    except Exception as exc:
        logger.exception("Failed to list AI platform configs")
        return server_error("Load failed")


@bp.route("/api/config/aiplatform/<platform>", methods=["POST"])
@require_permission_node("settings.ai_models")
def save_ai_platform_config(platform):
    try:
        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Invalid config data", 400)

        config_path = safe_join(_get_ai_platform_dir(), f"{platform}.json")
        secret_path = safe_join(_get_ai_platform_secret_dir(), f"{platform}.json")
        save_platform_config(config_path, secret_path, config_data)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了 AI 平台设置"),
            用户ID=session.get("user_id"),
            平台=platform,
        )
        return success_response(message="Config saved successfully")
    except Exception as exc:
        logger.exception("Failed to save AI platform config: %s", platform)
        return server_error("Save failed")


@bp.route("/api/config/aiplatform/order", methods=["POST"])
@require_permission_node("settings.ai_models")
def reorder_ai_platform_configs():
    """保存大模型供应商的加载顺序（管理员拖动排序的结果）。

    请求体 ``{"order": [platform_id, ...]}``，数组下标即加载优先级：下标越小越先加载。
    运行时房间对话据此顺序加载启用平台，失败时按同一顺序回退到下一个。
    """
    try:
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            return error_response("Invalid order data", 400)

        platform_dir = _get_ai_platform_dir()
        secret_dir = _get_ai_platform_secret_dir()
        updated = []
        for index, platform in enumerate(order):
            platform_id = str(platform or "").strip()
            if not platform_id:
                continue
            config_path = safe_join(platform_dir, f"{platform_id}.json")
            secret_path = safe_join(secret_dir, f"{platform_id}.json")
            if not config_path.exists():
                continue
            config = load_platform_config(config_path, secret_path)
            config["order"] = index
            save_platform_config(config_path, secret_path, config)
            updated.append(platform_id)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "调整了 AI 平台加载顺序"),
            用户ID=session.get("user_id"),
            顺序=",".join(updated),
        )
        return success_response(message="Order saved successfully")
    except Exception:
        logger.exception("Failed to save AI platform order")
        return server_error("Save failed")


@bp.route("/api/config/aiplatform/<platform>", methods=["DELETE"])
@require_permission_node("settings.ai_models")
def delete_ai_platform_config(platform):
    """删除平台配置、对应 secret 及其模型请求模板目录。"""
    try:
        config_path = safe_join(_get_ai_platform_dir(), f"{platform}.json")
        secret_path = safe_join(_get_ai_platform_secret_dir(), f"{platform}.json")
        model_dir = safe_join(_get_ai_model_dir(), platform)

        removed = False
        for path in (config_path, secret_path):
            if path.exists():
                path.unlink()
                removed = True
        if model_dir.exists() and model_dir.is_dir():
            shutil.rmtree(model_dir)
            removed = True
        if not removed:
            logger.info("AI platform config already absent: %s", platform)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "删除了 AI 平台设置"),
            用户ID=session.get("user_id"),
            平台=platform,
        )
        return success_response(message="Config deleted successfully")
    except Exception as exc:
        logger.exception("Failed to delete AI platform config: %s", platform)
        return server_error("Delete failed")


@bp.route("/api/config/aiplatform/<platform>/test", methods=["POST"])
@require_permission_node("settings.ai_models")
def test_ai_platform_api(platform):
    try:
        test_data = request.get_json(silent=True)
        if not test_data:
            return error_response("Invalid test data", 400)

        config_path = safe_join(_get_ai_platform_dir(), f"{platform}.json")
        secret_path = safe_join(_get_ai_platform_secret_dir(), f"{platform}.json")
        if not config_path.exists():
            return error_response("Platform config file does not exist", 404)

        config = load_platform_config(config_path, secret_path)
        api_key = config.get("config", {}).get("api_key")
        base_url = chat_completions_endpoint(config.get("config", {}).get("base_url"))
        if not base_url:
            return error_response("Base URL is not set", 400)
        if not api_key and platform == "lmstudio":
            api_key = "lm-studio"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        test_payload = test_data.copy()
        if "extra_body" in test_payload:
            extra_body = test_payload.pop("extra_body")
            test_payload.update(extra_body)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "测试了 AI 平台连接"),
            用户ID=session.get("user_id"),
            平台=platform,
            BaseURL=base_url,
        )
        response = requests.post(base_url, headers=headers, json=test_payload, timeout=30)
        response_data = response.json()
        if response.status_code != 200:
            error_message = response_data.get("error", {}).get(
                "message",
                f"API request failed: {response.status_code}",
            )
            return error_response(None, response.status_code, error_message)

        return success_response(message=None, response=response_data)
    except Exception as exc:
        logger.exception("Failed to test AI platform API: %s", platform)
        return server_error()


@bp.route("/api/config/aiplatform/<platform>/detect-responses", methods=["POST"])
@require_permission_node("settings.ai_models")
def detect_responses_api_support(platform):
    """探测该平台是否支持 Responses API（previous_response_id），并把结论写入配置。

    探测结论决定前端开关是否可用：不支持时一律关闭 ``use_previous_response_id``，
    避免用户开启一条实际不可用的请求路径。
    """
    try:
        config_path = safe_join(_get_ai_platform_dir(), f"{platform}.json")
        secret_path = safe_join(_get_ai_platform_secret_dir(), f"{platform}.json")
        if not config_path.exists():
            return error_response("Platform config file does not exist", 404)

        config = load_platform_config(config_path, secret_path)
        config_section = config.get("config") if isinstance(config.get("config"), dict) else {}
        api_key = config_section.get("api_key")
        base_url = config_section.get("base_url")
        if not base_url:
            return error_response("Base URL is not set", 400)
        if not api_key and platform == "lmstudio":
            api_key = "lm-studio"

        models = config.get("models") if isinstance(config.get("models"), list) else []
        model = next((item for item in models if isinstance(item, dict) and item.get("enabled", True)), None)
        model = model or (models[0] if models and isinstance(models[0], dict) else {})
        model_id = str(model.get("id") or "")

        result = probe_responses_api(base_url, api_key, model_id)
        config_section.pop("api_key", None)
        supported = bool(result.get("supported"))
        config_section["responses_api_supported"] = supported
        config_section["responses_api_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        config_section["responses_api_detail"] = str(result.get("detail") or "")
        if not supported:
            config_section["use_previous_response_id"] = False
        config["config"] = config_section
        save_platform_config(config_path, secret_path, config)

        log_user_action(
            logger,
            user_action_text(session.get("username"), "探测了 AI 平台的 Responses API 支持"),
            用户ID=session.get("user_id"),
            平台=platform,
            支持=supported,
        )
        return success_response(message=None, data=result)
    except Exception as exc:
        logger.exception("Failed to detect Responses API support: %s", platform)
        return server_error("Detection failed")


@bp.route("/api/config/aimodel/save", methods=["POST"])
@require_permission_node("settings.ai_models")
def save_model_request_config():
    try:
        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Invalid config data", 400)

        platform = config_data.get("platform")
        model_id = config_data.get("modelId")
        content = config_data.get("content")
        if not platform or not model_id or not content:
            return error_response("Platform, model ID, and config content are required", 400)

        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return error_response("Model config content must be valid JSON", 400)
        if not isinstance(content, dict):
            return error_response("Model config content must be a JSON object", 400)

        config_path = safe_join(_get_ai_model_dir(), platform, f"{model_id}.json")
        write_json_atomic(config_path, content)

        legacy_js_path = safe_join(_get_ai_model_dir(), platform, f"{model_id}.js")
        if legacy_js_path.exists():
            legacy_js_path.unlink()

        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了 AI 模型请求设置"),
            用户ID=session.get("user_id"),
            平台=platform,
            模型=model_id,
        )
        return success_response(message="Config saved successfully")
    except Exception as exc:
        logger.exception("Failed to save model request config")
        return server_error("Save failed")


@bp.route("/api/config/system-prompt", methods=["GET"])
@require_permission_node("settings.ai_models")
def get_system_prompt():
    prompt_path = _get_kp_prompt_file()
    try:
        if not prompt_path.exists():
            return success_response(data={"content": ""})
        return success_response(data={"content": prompt_path.read_text(encoding="utf-8")})
    except OSError as exc:
        logger.exception("Failed to load system prompt")
        return server_error("Load failed")


@bp.route("/api/config/debug-prompt", methods=["GET"])
@require_permission_node("settings.ai_debug")
def get_debug_prompt():
    prompt_path = _get_debug_kp_prompt_file()
    try:
        content = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        return success_response(data={"content": content})
    except OSError as exc:
        logger.exception("Failed to load debug prompt")
        return server_error("Load failed")


@bp.route("/api/config/system-prompt", methods=["POST"])
@require_permission_node("settings.ai_models")
def save_system_prompt():
    try:
        prompt_data = request.get_json(silent=True)
        if not isinstance(prompt_data, dict):
            return error_response("Invalid prompt data", 400)

        content = prompt_data.get("content")
        if not isinstance(content, str):
            return error_response("Prompt content is required", 400)

        prompt_path = _get_kp_prompt_file()
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(content, encoding="utf-8")

        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了系统提示词"),
            用户ID=session.get("user_id"),
            文件=prompt_path.name,
            内容长度=len(content),
        )
        return success_response(message="System prompt saved successfully")
    except Exception as exc:
        logger.exception("Failed to save system prompt")
        return server_error("Save failed")


@bp.route("/api/config/debug-prompt", methods=["POST"])
@require_permission_node("settings.ai_debug")
def save_debug_prompt():
    try:
        prompt_data = request.get_json(silent=True)
        if not isinstance(prompt_data, dict):
            return error_response("Invalid debug prompt data", 400)

        content = prompt_data.get("content")
        if not isinstance(content, str) or not content.strip():
            return error_response("Debug prompt content is required", 400)

        prompt_path = _get_debug_kp_prompt_file()
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(content, encoding="utf-8")
        log_user_action(
            logger,
            user_action_text(session.get("username"), "更新了 AI 调试提示词"),
            用户ID=session.get("user_id"),
            文件名=prompt_path.name,
            内容长度=len(content),
        )
        return success_response(message="Debug prompt saved successfully")
    except Exception as exc:
        logger.exception("Failed to save debug prompt")
        return server_error("Save failed")


@bp.route("/api/config/roles", methods=["GET"])
@require_permission_node("settings.ai_models")
def get_role_configs():
    try:
        roles = load_roles(_get_role_config_file(), _get_kp_prompt_file(), _get_ai_platform_dir())
        providers = enabled_provider_options(_get_ai_platform_dir())
        return success_response(data={"roles": roles, "enabled_providers": providers})
    except Exception as exc:
        logger.exception("Failed to load role configs")
        return server_error("Load failed")


@bp.route("/api/config/roles/<role_id>", methods=["POST"])
@require_permission_node("settings.ai_models")
def save_role_config(role_id):
    try:
        role_data = request.get_json(silent=True)
        if not isinstance(role_data, dict):
            return error_response("Invalid role config data", 400)

        roles = save_role(
            _get_role_config_file(),
            _get_kp_prompt_file(),
            _get_ai_platform_dir(),
            role_id,
            role_data,
        )
        log_user_action(
            logger,
            user_action_text(session.get("username"), "更改了角色配置"),
            用户ID=session.get("user_id"),
            角色=role_id,
            平台=role_data.get("provider"),
        )
        return success_response(message="Role config saved successfully", data={"roles": roles})
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:
        logger.exception("Failed to save role config: %s", role_id)
        return server_error("Save failed")


@bp.route("/api/config/aimodel/delete", methods=["POST"])
@require_permission_node("settings.ai_models")
def delete_model_request_config():
    try:
        config_data = request.get_json(silent=True)
        if not config_data:
            return error_response("Invalid config data", 400)

        platform = config_data.get("platform")
        model_id = config_data.get("modelId")
        if not platform or not model_id:
            return error_response("Platform and model ID are required", 400)

        removed = False
        for suffix in (".json", ".js"):
            config_path = safe_join(_get_ai_model_dir(), platform, f"{model_id}{suffix}")
            if config_path.exists():
                config_path.unlink()
                removed = True

        if removed:
            log_user_action(
                logger,
                user_action_text(session.get("username"), "删除了 AI 模型请求设置"),
                用户ID=session.get("user_id"),
                平台=platform,
                模型=model_id,
            )
        else:
            log_user_action(
                logger,
                user_action_text(session.get("username"), "确认 AI 模型请求设置已不存在"),
                用户ID=session.get("user_id"),
                平台=platform,
                模型=model_id,
            )

        return success_response(message="Config deleted successfully")
    except Exception as exc:
        logger.exception("Failed to delete model request config")
        return server_error("Delete failed")
