import logging

import requests
from flask import Blueprint, current_app, request, session

from trpg_server.agents.ai_request import build_request_payload
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.http_retry import post_with_retry
from trpg_server.ai_capabilities import chat_completions_endpoint
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.scenario_ai import (
    clean_module_summary,
    extract_ai_response,
    load_enabled_platform,
    module_summary_user_prompt,
    select_model,
    summary_role,
)
from trpg_server.routes.scenarios import _can_use_permission, _require_login
from trpg_server.settings import CONFIG_DIR

bp = Blueprint("scenario_summary", __name__)
logger = logging.getLogger(__name__)


@bp.route("/api/scenarios/module-summary", methods=["POST"])
def summarize_scenario_module():
    try:
        login_error = _require_login()
        if login_error:
            return login_error
        if not (_can_use_permission("scenarios.edit") or _can_use_permission("scenarios.create")):
            return error_response("Permission denied", 403, "Permission denied")

        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return error_response("Invalid module summary data", 400)

        module = request_data.get("module")
        if not isinstance(module, dict):
            return error_response("Module data is required", 400)

        role = summary_role()
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

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        model = select_model(platform_config)
        config_dir = current_app.config.get("CONFIG_DIR", CONFIG_DIR)
        runtime_config = load_ai_runtime_config(config_dir)
        payload = build_request_payload(
            selected_platform,
            model,
            [
                {"role": "system", "content": str(role.get("prompt") or "")},
                {"role": "user", "content": module_summary_user_prompt(request_data.get("scenario_title"), module)},
            ],
            runtime_config=runtime_config,
            config_dir=config_dir,
            params={"temperature": 0.2, "top_p": 0.8},
        )
        logger.info(
            "module_summary_request platform=%s model=%s max_tokens=%s constrain=%s token_count_target=%s",
            selected_platform,
            model,
            payload.get("max_tokens"),
            runtime_config.constrain_max_tokens,
            module.get("token_count"),
        )
        try:
            timeout = int(platform_config.get("config", {}).get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        response = post_with_retry(
            base_url,
            headers=headers,
            payload=payload,
            timeout=max(15, min(timeout, 60)),
            max_retries=runtime_config.ai_request_max_retries,
            post=requests.post,
            label="module_summary",
        )
        response_data = response.json()
        if not isinstance(response_data, dict):
            return error_response("AI 平台返回的数据格式无效", 502, "Response must be a JSON object")
        if response.status_code != 200:
            error_message = response_data.get("error", {}).get("message", f"API request failed: {response.status_code}")
            return error_response(None, response.status_code, error_message)

        summary, token_count = extract_ai_response(response_data)
        summary = clean_module_summary(summary)
        if not summary:
            return error_response("AI platform did not return a module summary", 500, "No summary")

        log_user_action(
            logger,
            user_action_text(session.get("username"), "生成了剧本模块摘要"),
            用户ID=session.get("user_id"),
            模块类型=str(module.get("module_type") or ""),
            模块标题=str(module.get("title") or ""),
            token_count=token_count,
        )
        return success_response(
            data={"summary": summary, "token_count": token_count, "role_id": role.get("id"), "model": model},
            message="Module summary generated successfully",
        )
    except RuntimeError as exc:
        # post_with_retry 统一把「超时/连接失败/手动终止」抛为 RuntimeError，按文案分类。
        message = str(exc)
        logger.warning("Scenario module summary request failed after retries: %s", message)
        if "超时" in message:
            return error_response(message, 504, "Request timeout")
        return error_response(message, 503, "Connection error")
    except requests.exceptions.Timeout:
        logger.warning("Scenario module summary request timed out")
        return error_response("AI 平台请求超时，请稍后重试", 504, "Request timeout")
    except requests.exceptions.ConnectionError:
        logger.warning("Scenario module summary connection failed")
        return error_response("无法连接 AI 平台，请检查网络或平台配置", 503, "Connection error")
    except requests.exceptions.RequestException as exc:
        logger.warning("Scenario module summary request failed: %s", exc)
        return error_response("AI 平台请求失败", 502, str(exc))
    except ValueError as exc:
        logger.warning("Scenario module summary returned invalid JSON: %s", exc)
        return error_response("AI 平台返回的数据格式无效", 502, str(exc))
    except Exception as exc:
        logger.exception("Failed to generate module summary")
        return server_error("Failed to generate module summary")