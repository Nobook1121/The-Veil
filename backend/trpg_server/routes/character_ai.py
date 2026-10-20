"""角色卡 AI 生成接口。

用户在角色卡页面提供「可选剧本 + 角色描述」，由本接口调用与 KP 相同的后端 AI 平台
生成一张完整角色卡，前端再填入现有角色卡编辑器供用户复核与保存。

具体编排（平台解析、提示词拼装、请求与解析、日志与用量记录）统一收敛在
``trpg_server.character_ai.run_character_generation``，此处只做鉴权与 HTTP 映射，
控制台 ``ai character generate`` 复用同一实现。
"""

import logging

from flask import Blueprint, current_app, request, session

from trpg_server.character_ai import (
    MAX_DESCRIPTION_LENGTH,
    CharacterGenerationError,
    run_character_generation,
    scenario_context_from_record,
)
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.responses import error_response, server_error, success_response
from trpg_server.routes.characters import _can_use_permission, _require_login
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.settings import SCENARIOS_DIR

bp = Blueprint("character_ai", __name__)
logger = logging.getLogger(__name__)


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
        scenario_id = request_data.get("scenario_id")

        scenario_context = {}
        if scenario_id not in (None, "", 0, "0"):
            scenarios_dir = current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)
            _, scenario = load_scenario_by_id(scenarios_dir, scenario_id)
            if isinstance(scenario, dict):
                scenario_context = scenario_context_from_record(scenario)

        try:
            result = run_character_generation(
                description,
                scenario_context,
                user_id=session.get("user_id"),
                scenario_id=scenario_id,
            )
        except CharacterGenerationError as exc:
            # 上游 HTTP 失败沿用原始状态码与错误文案；其余分类返回可读提示。
            if exc.kind == "http":
                return error_response(None, exc.status, str(exc))
            return error_response(str(exc), exc.status, str(exc))

        log_user_action(
            logger,
            user_action_text(session.get("username"), "通过 AI 生成了角色卡"),
            用户ID=session.get("user_id"),
            剧本ID=str(scenario_id or ""),
            token_count=result.get("token_count"),
            model=result.get("model"),
        )
        return success_response(
            data={
                "character": result["character"],
                "token_count": result.get("token_count"),
                "role_id": result.get("role_id"),
                "model": result.get("model"),
            },
            message="Character generated successfully",
        )
    except Exception:
        logger.exception("Failed to generate character with AI")
        return server_error("Failed to generate character")
