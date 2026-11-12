"""示例插件入口。

只有管理员把 ``hello-prop`` 写进 ``data/plugins/enabled.json`` 的 ``enabled``
列表后，宿主才会导入本文件。插件代码以宿主进程权限运行——这正是「显式启用」
必须由管理员完成的原因。
"""

from flask import Blueprint, jsonify

bp = Blueprint("hello_prop", __name__)


@bp.route("/api/plugins/hello-prop/ping", methods=["GET"])
def ping():
    return jsonify({"success": True, "data": {"plugin": "hello-prop", "pong": True}})
