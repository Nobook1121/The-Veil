"""角色卡 AI 生成接口。

用户在角色卡页面提供「可选剧本 + 角色描述」，由本接口调用与 KP 相同的后端 AI 平台
生成一张完整角色卡，前端再填入现有角色卡编辑器供用户复核与保存。
"""

import logging

import requests
from flask import Blueprint, current_app, request, session

from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.character_ai import (
    MAX_DESCRIPTION_LENGTH,
    build_character_generation_user_prompt,
    build_character_reference_payload,
    character_generator_role,
    parse_character_generation,
    scenario_context_from_record,
)
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.scenario_ai import extract_ai_response, load_enabled_platform, select_model
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.routes.characters import _can_use_permission, _require_login
from trpg_server.settings import SCENARIOS_DIR

bp = Blueprint("character_ai", __name__)
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 180
MAX_TOKENS = 2400


@bp.route("/api/characters/generate", methods=["POST"])
def generate_character_with_ai():
    try:
        login_error = _require_login()
        if login_error:
            return login_error
        if not _can_use_permission("characters.create"):
            return error_response("Permission denied", 403, "Permission denied")

        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            request_data = {}

        description = str(request_data.get("description") or "").strip()[:MAX_DESCRIPTION_LENGTH]

        scenario_context = {}
        scenario_id = request_data.get("scenario_id")
        if scenario_id not in (None, "", 0, "0"):
            scenarios_dir = current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)
            _, scenario = load_scenario_by_id(scenarios_dir, scenario_id)
            if isinstance(scenario, dict):
                scenario_context = scenario_context_from_record(scenario)

        role = character_generator_role()
        selected_platform, platform_config = load_enabled_platform(role.get("provider"))
        if not platform_config:
            return error_response("No enabled AI platform", 400, "No enabled platform")

        api_key = platform_config.get("config", {}).get("api_key")
        base_url = chat_completions_endpoint(platform_config.get("config", {}).get("base_url"))
        if not base_url:
            return error_response("AI platform config is incomplete", 400, "Incomplete platform config")
        if not api_key and selected_platform == "lmstudio":
            api_key = "lm-studio"
        elif not api_key:
            return error_response("AI platform config is incomplete", 400, "Incomplete platform config")

        reference = build_character_reference_payload()
        messages = [
            {"role": "system", "content": str(role.get("prompt") or "")},
            {
                "role": "user",
                "content": build_character_generation_user_prompt(description, scenario_context, reference),
            },
        ]
        model = select_model(platform_config)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.6,
            "top_p": 0.9,
        }
        try:
            timeout = int(platform_config.get("config", {}).get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        response = requests.post(
            base_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=max(30, min(timeout, MAX_TIMEOUT)),
        )
        response_data = response.json()
        if not isinstance(response_data, dict):
            return error_response("AI 平台返回的数据格式无效", 502, "Response must be a JSON object")
        if response.status_code != 200:
            error_message = response_data.get("error", {}).get("message", f"API request failed: {response.status_code}")
            return error_response(None, response.status_code, error_message)

        content, token_count = extract_ai_response(response_data)
        character = parse_character_generation(content)
        if not character:
            logger.warning(
                "character_generation_parse_failed user_id=%s content_length=%s",
                session.get("user_id"),
                len(str(content or "")),
            )
            return error_response("AI 平台未返回有效的角色卡数据", 502, "Invalid character payload")

        log_user_action(
            logger,
            user_action_text(session.get("username"), "通过 AI 生成了角色卡"),
            用户ID=session.get("user_id"),
            剧本ID=str(scenario_id or ""),
            token_count=token_count,
        )
        return success_response(
            data={
                "character": character,
                "token_count": token_count,
                "role_id": role.get("id"),
                "model": model,
            },
            message="Character generated successfully",
        )
    except requests.exceptions.Timeout:
        logger.warning("Character generation request timed out")
        return error_response("AI 平台请求超时，请稍后重试", 504, "Request timeout")
    except requests.exceptions.ConnectionError:
        logger.warning("Character generation connection failed")
        return error_response("无法连接 AI 平台，请检查网络或平台配置", 503, "Connection error")
    except requests.exceptions.RequestException as exc:
        logger.warning("Character generation request failed: %s", exc)
        return error_response("AI 平台请求失败", 502, str(exc))
    except ValueError as exc:
        logger.warning("Character generation returned invalid JSON: %s", exc)
        return error_response("AI 平台返回的数据格式无效", 502, str(exc))
    except Exception:
        logger.exception("Failed to generate character with AI")
        return server_error("Failed to generate character")
