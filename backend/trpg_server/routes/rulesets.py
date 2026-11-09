"""规则集元数据接口。

只读、仅登录用户可见：前端据此渲染规则集选择与「不兼容插件」提示。
插件清单来自内置注册表，没有任何动态加载入口。
"""

from __future__ import annotations

from flask import Blueprint, session

from trpg_server.responses import error_response, success_response
from trpg_server.ruleset import builtin_registry

bp = Blueprint("rulesets", __name__)


@bp.route("/api/rulesets", methods=["GET"])
def list_rulesets():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return success_response(builtin_registry().describe(), "Rulesets loaded successfully")
