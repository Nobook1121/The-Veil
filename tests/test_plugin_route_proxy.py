"""插件代理路由层（cordis-paradigm-adoption-plan.md 阶段 3 · 路线 A spike）测试。

验收口径 = 阶段 3 spike 评审：

- mount 后路由可达：int/float/path 转换器、defaults、strict_slashes、多 methods、
  url_prefix 根规则、query/session/current_app 解析到真实宿主；
- unmount 立即 404、重新 mount 恢复，50 次往返零残留（表空、规则数无叠加）；
- 多插件共存相互隔离、同插件多 rule、manifest 多蓝图一次入表、重复 mount 换表、
  未挂载插件 404；
- 与宿主核心路由共存：核心静态段优先、同 pattern 同 method 注册顺序先者赢
  （总蓝图必须晚于核心注册——接线顺序约束的钉住用例）；
- abort / 未捕获异常的错误码透传；
- 与原生 ``register_blueprint`` 的差分对拍：同一个蓝图对象挂两种方式，
  逐请求比对 status / body / Allow / Location——路线 A「无损保真」的直接证据；
- 已知限制显式钉住：url_for 反查在代理 app 上 BuildError（接线时的缓解项
  是 ``app.url_build_error_handlers``，见模块头「不支持面」）。

测试纪律对齐 ``tests/test_plugins.py``：纯 Flask 临时 app，不碰真实用户数据；
autouse 清理模块级单例。
"""

from __future__ import annotations

import pytest
from flask import Blueprint, Flask, abort, current_app, jsonify, request, session, url_for
from werkzeug.routing import BuildError

from trpg_server.plugins.route_proxy import PROXY_METHODS, bp, route_proxy


# ---------------------------------------------------------------- 工具


@pytest.fixture(autouse=True)
def _reset_proxy():
    route_proxy.unmount_all()
    yield
    route_proxy.unmount_all()


def _make_app(*blueprints) -> Flask:
    """测试宿主：先注册传入的核心蓝图（接线顺序约束），再挂总蓝图。"""
    app = Flask("route-proxy-test", static_folder=None)
    app.config["SECRET_KEY"] = "route-proxy-spike"  # session_transaction 需要
    for item in blueprints:
        app.register_blueprint(item)
    app.register_blueprint(bp)
    return app


def _alpha_blueprint() -> Blueprint:
    """形态对齐 dnd5e/veil-map 的插件蓝图：url_prefix + 混合转换器 + 多 methods。"""
    plugin_bp = Blueprint("alpha_plugin", __name__, url_prefix="/api/plugins/alpha")

    @plugin_bp.route("/rooms/<int:room_id>/content/search", methods=["GET"])
    def content_search(room_id: int):
        return jsonify(
            {
                "room_id": room_id,
                "q": str(request.args.get("q") or ""),
                "user": str(session.get("user_id") or ""),
                "cfg": str(current_app.config.get("SPIKE_CFG") or ""),
            }
        )

    @plugin_bp.route("/echo", methods=["GET", "POST"])
    def echo():
        return jsonify({"echo": "post" if request.method == "POST" else "get"})

    @plugin_bp.route("/multi/<kind>/<path:tail>")
    def multi(kind: str, tail: str):
        return jsonify({"kind": kind, "tail": tail})

    @plugin_bp.route("/calc/<int:a>/<float:scale>")
    def calc(a: int, scale: float):
        return jsonify({"a": a, "scale": scale})

    @plugin_bp.route("/page/", defaults={"page": 1})
    @plugin_bp.route("/page/<int:page>")
    def page(page: int):
        return jsonify({"page": page})

    @plugin_bp.route("/loose", strict_slashes=False)
    def loose():
        return jsonify({"loose": True})

    @plugin_bp.route("/slash/", methods=["PUT"])
    def slash():
        return jsonify({"slash": True})

    @plugin_bp.route("/boom")
    def boom():
        abort(409)

    @plugin_bp.route("/forbidden")
    def forbidden():
        abort(403, "no way")

    @plugin_bp.route("/crash")
    def crash():
        raise RuntimeError("plugin view boom")

    @plugin_bp.route("/custom-options", methods=["OPTIONS"])
    def custom_options():
        return jsonify({"custom": "options"})

    @plugin_bp.route("/enable", methods=["GET"])
    def plugin_enable():
        return jsonify({"plugin": "enable"})

    @plugin_bp.route("/")
    def root():
        return jsonify({"plugin": "root"})

    return plugin_bp


def _beta_blueprint() -> Blueprint:
    plugin_bp = Blueprint("beta_plugin", __name__, url_prefix="/api/plugins/beta")

    @plugin_bp.route("/echo")
    def echo():
        return jsonify({"echo": "beta"})

    return plugin_bp


def _core_blueprint() -> Blueprint:
    """模拟 routes/plugins.py 的管理面（路径与方法一比一）。"""
    core = Blueprint("core_plugins_admin", __name__)

    @core.route("/api/plugins", methods=["GET"])
    def list_plugins():
        return jsonify({"core": "list"})

    @core.route("/api/plugins/schema", methods=["GET"])
    def schema():
        return jsonify({"core": "schema"})

    @core.route("/api/plugins/rescan", methods=["POST"])
    def rescan():
        return jsonify({"core": "rescan"})

    @core.route("/api/plugins/<plugin_id>/enable", methods=["POST"])
    def enable(plugin_id: str):
        return jsonify({"core": "enable", "id": plugin_id})

    @core.route("/api/plugins/<plugin_id>/disable", methods=["POST"])
    def disable(plugin_id: str):
        return jsonify({"core": "disable", "id": plugin_id})

    @core.route("/api/plugins/<plugin_id>", methods=["DELETE"])
    def uninstall(plugin_id: str):
        return jsonify({"core": "uninstall", "id": plugin_id})

    return core


# ---------------------------------------------------------------- 可达性


def test_mount_reaches_routes_converters_methods_and_url_prefix():
    app = _make_app()
    app.config["SPIKE_CFG"] = "host-config"
    route_proxy.mount(app, _alpha_blueprint(), "alpha")
    client = app.test_client()

    # int 转换器 + query 透传；session/current_app 解析到真实宿主。
    with client.session_transaction() as sess:
        sess["user_id"] = "u-42"
    data = client.get("/api/plugins/alpha/rooms/12/content/search?q=fireball").get_json()
    assert data == {"room_id": 12, "q": "fireball", "user": "u-42", "cfg": "host-config"}
    # int 转换器不匹配 → 404。
    assert client.get("/api/plugins/alpha/rooms/twelve/content/search").status_code == 404

    # 多 methods。
    assert client.get("/api/plugins/alpha/echo").get_json() == {"echo": "get"}
    assert client.post("/api/plugins/alpha/echo").get_json() == {"echo": "post"}
    assert client.put("/api/plugins/alpha/echo").status_code == 405
    assert client.delete("/api/plugins/alpha/echo").status_code == 405

    # path 转换器吃多段、float/int 组合。
    assert client.get("/api/plugins/alpha/multi/x/y/z").get_json() == {"kind": "x", "tail": "y/z"}
    assert client.get("/api/plugins/alpha/calc/7/2.5").get_json() == {"a": 7, "scale": 2.5}
    assert client.get("/api/plugins/alpha/calc/7/abc").status_code == 404

    # defaults：两个 rule 共享一个 view，默认值作为 kwargs 传进 view。
    assert client.get("/api/plugins/alpha/page/3").get_json() == {"page": 3}

    # strict_slashes=False：带不带尾斜杠都命中。
    assert client.get("/api/plugins/alpha/loose").status_code == 200
    assert client.get("/api/plugins/alpha/loose/").status_code == 200

    # 分支规则的缺斜杠：方法命中才给 308（GET 对 PUT-only 规则是 404，
    # 与原生一致）；url_prefix 根规则（无尾段 + 尾斜杠两条总路由都接得住）。
    redirect = client.put("/api/plugins/alpha/slash")
    assert redirect.status_code == 308
    assert redirect.headers["Location"].endswith("/api/plugins/alpha/slash/")
    assert client.get("/api/plugins/alpha/slash").status_code == 404
    assert client.put("/api/plugins/alpha/slash/").get_json() == {"slash": True}
    assert client.get("/api/plugins/alpha", follow_redirects=True).get_json() == {"plugin": "root"}
    assert client.get("/api/plugins/alpha/", follow_redirects=True).get_json() == {"plugin": "root"}

    # 挂载元数据。
    assert route_proxy.is_mounted("alpha")
    assert route_proxy.mounted_ids() == ["alpha"]
    assert route_proxy.route_count("alpha") == 14  # 14 条 rule（page 的 defaults 双 rule 与根规则在内）


def test_request_context_resolves_to_host_app():
    """插件 view 里的 request/session/current_app 必须指向真实宿主，不是 scratch。"""
    app = Flask("ctx-fidelity", static_folder=None)
    app.config["SPIKE_CFG"] = "host-config"
    app.config["SECRET_KEY"] = "route-proxy-spike"
    app.register_blueprint(bp)
    ctx_bp = Blueprint("ctx_plugin", __name__, url_prefix="/api/plugins/ctx")

    @ctx_bp.route("/whoami")
    def whoami():
        return jsonify(
            {
                "user": str(session.get("user_id") or ""),
                "cfg": str(current_app.config["SPIKE_CFG"]),
                "path": request.path,
            }
        )

    route_proxy.mount(app, ctx_bp, "ctx")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-7"
    assert client.get("/api/plugins/ctx/whoami").get_json() == {
        "user": "u-7",
        "cfg": "host-config",
        "path": "/api/plugins/ctx/whoami",
    }


# ---------------------------------------------------------------- 可逆性


def test_unmount_immediately_404_remount_restores_50_cycles():
    app = _make_app()
    alpha = _alpha_blueprint()
    route_proxy.mount(app, alpha, "alpha")
    client = app.test_client()
    expected_rules = route_proxy.route_count("alpha")
    assert expected_rules > 0

    for _ in range(50):
        assert route_proxy.unmount("alpha") is True
        assert route_proxy.is_mounted("alpha") is False
        assert client.get("/api/plugins/alpha/echo").status_code == 404
        assert client.get("/api/plugins/alpha", follow_redirects=True).status_code == 404
        assert client.get("/api/plugins/ghost/echo").status_code == 404  # 从未挂载的插件
        route_proxy.mount(app, alpha, "alpha")
        assert client.get("/api/plugins/alpha/echo").status_code == 200
        assert route_proxy.route_count("alpha") == expected_rules  # 规则数零叠加

    # 全部往返后：表空、路径 404；重挂一次行为与首轮一致。
    assert route_proxy.unmount("alpha") is True
    assert route_proxy.mounted_ids() == []
    assert client.get("/api/plugins/alpha/echo").status_code == 404
    route_proxy.mount(app, alpha, "alpha")
    assert client.get("/api/plugins/alpha/echo").get_json() == {"echo": "get"}
    assert route_proxy.unmount("ghost") is False  # 未挂载的摘除是幂等 False


def test_multiple_plugins_coexist_with_isolation():
    app = _make_app()
    route_proxy.mount(app, _alpha_blueprint(), "alpha")
    route_proxy.mount(app, _beta_blueprint(), "beta")
    client = app.test_client()

    assert client.get("/api/plugins/alpha/echo").status_code == 200
    assert client.get("/api/plugins/beta/echo").get_json() == {"echo": "beta"}
    assert set(route_proxy.mounted_ids()) == {"alpha", "beta"}

    # 摘 alpha 不影响 beta；重挂恢复。
    route_proxy.unmount("alpha")
    assert client.get("/api/plugins/alpha/echo").status_code == 404
    assert client.get("/api/plugins/beta/echo").status_code == 200
    route_proxy.mount(app, _alpha_blueprint(), "alpha")
    assert client.get("/api/plugins/alpha/echo").status_code == 200

    assert route_proxy.unmount_all() == ["alpha", "beta"]
    assert route_proxy.mounted_ids() == []
    assert client.get("/api/plugins/beta/echo").status_code == 404


def test_manifest_multi_blueprint_mounts_into_one_table():
    """manifest ``blueprints: ["a", "b"]`` 的多蓝图一次 mount 全量入表，按插件摘。"""
    app = _make_app()
    one = Blueprint("multi_one", __name__, url_prefix="/api/plugins/multi")

    @one.route("/one")
    def part_one():
        return jsonify({"part": "one"})

    two = Blueprint("multi_two", __name__, url_prefix="/api/plugins/multi")

    @two.route("/two")
    def part_two():
        return jsonify({"part": "two"})

    route_proxy.mount(app, [one, two], "multi")
    client = app.test_client()
    assert client.get("/api/plugins/multi/one").get_json() == {"part": "one"}
    assert client.get("/api/plugins/multi/two").get_json() == {"part": "two"}
    assert route_proxy.route_count("multi") == 2

    route_proxy.unmount("multi")  # 按插件整体摘，一次摘掉两个蓝图
    assert client.get("/api/plugins/multi/one").status_code == 404
    assert client.get("/api/plugins/multi/two").status_code == 404


def test_remount_replaces_previous_table():
    """同插件重复 mount = 换表（增量 enable 的幂等重挂路径）。"""
    app = _make_app()
    first = Blueprint("swap_plugin", __name__, url_prefix="/api/plugins/swap")

    @first.route("/ping")
    def ping_v1():
        return jsonify({"v": 1})

    second = Blueprint("swap_plugin", __name__, url_prefix="/api/plugins/swap")

    @second.route("/ping")
    def ping_v2():
        return jsonify({"v": 2})

    route_proxy.mount(app, first, "swap")
    client = app.test_client()
    assert client.get("/api/plugins/swap/ping").get_json() == {"v": 1}

    route_proxy.mount(app, second, "swap")
    assert client.get("/api/plugins/swap/ping").get_json() == {"v": 2}
    assert route_proxy.route_count("swap") == 1  # 无叠加残留


def test_mount_warns_on_out_of_namespace_rules(caplog):
    """蓝图前缀跑出 /api/plugins/<id> 命名空间：入表但不可达，mount 时点名告警。"""
    app = _make_app()
    stray = Blueprint("stray_plugin", __name__, url_prefix="/api/elsewhere")

    @stray.route("/ping")
    def stray_ping():
        return jsonify({"ok": True})

    with caplog.at_level("WARNING", logger="trpg_server.plugins.route_proxy"):
        route_proxy.mount(app, stray, "stray")
    assert any("plugin_routes_out_of_namespace" in item.getMessage() for item in caplog.records)
    assert app.test_client().get("/api/plugins/stray/ping").status_code == 404


def test_mount_rejects_empty_arguments():
    app = _make_app()
    with pytest.raises(ValueError):
        route_proxy.mount(app, _alpha_blueprint(), "")
    with pytest.raises(ValueError):
        route_proxy.mount(app, [], "alpha")


# ---------------------------------------------------------------- 核心优先


def test_host_core_routes_take_priority_over_proxy():
    """宿主管理面（模拟 routes/plugins.py）与代理层共存：核心优先，插件各就各位。"""
    app = _make_app(_core_blueprint())
    route_proxy.mount(app, _alpha_blueprint(), "alpha")
    client = app.test_client()

    # 静态段核心路由优先于总蓝图的动态段。
    assert client.get("/api/plugins").get_json() == {"core": "list"}
    assert client.get("/api/plugins/schema").get_json() == {"core": "schema"}
    assert client.post("/api/plugins/rescan").get_json() == {"core": "rescan"}

    # 同 pattern 同 method：注册顺序先者赢——核心 enable/disable/uninstall 不被吞。
    assert client.post("/api/plugins/alpha/enable").get_json() == {"core": "enable", "id": "alpha"}
    assert client.post("/api/plugins/alpha/disable").get_json() == {"core": "disable", "id": "alpha"}
    assert client.delete("/api/plugins/alpha").get_json() == {"core": "uninstall", "id": "alpha"}

    # 核心路由没有 GET：GET 落到插件表（与原生挂载同款语义）。
    assert client.get("/api/plugins/alpha/enable").get_json() == {"plugin": "enable"}

    # 插件自己的命名空间不受管理面影响；摘除后核心面照常、插件面 404。
    assert client.get("/api/plugins/alpha/rooms/1/content/search").status_code == 200
    route_proxy.unmount("alpha")
    assert client.post("/api/plugins/alpha/enable").get_json() == {"core": "enable", "id": "alpha"}
    assert client.delete("/api/plugins/alpha").get_json() == {"core": "uninstall", "id": "alpha"}
    assert client.get("/api/plugins/alpha/enable").status_code == 404


def test_registration_order_constraint_pinned():
    """钉住接线顺序约束：总蓝图先于核心注册时会吞掉同 pattern 的核心路由。

    生产接线必须保持「核心蓝图在前、总蓝图在后」（与 app_factory 现有
    register_blueprints → register_plugin_blueprints 顺序天然一致）；本用例
    把反例的行为显式钉住，防止接线时踩坑。
    """
    app = Flask("order-check", static_folder=None)
    app.register_blueprint(bp)  # 故意反着来
    app.register_blueprint(_core_blueprint())
    client = app.test_client()

    # 总蓝图先注册：DELETE /api/plugins/<id> 被代理吞掉（插件未挂载 → 404），
    # 核心卸载接口失效——这就是顺序约束要防的回归。
    assert client.delete("/api/plugins/alpha").status_code == 404
    # POST 仍然命中核心（同 pattern 上核心带静态尾段 enable/disable，无冲突）。
    assert client.post("/api/plugins/alpha/enable").get_json() == {"core": "enable", "id": "alpha"}


# ---------------------------------------------------------------- 错误透传


def test_abort_and_exception_status_passthrough():
    app = _make_app()
    route_proxy.mount(app, _alpha_blueprint(), "alpha")
    client = app.test_client()

    assert client.get("/api/plugins/alpha/boom").status_code == 409
    forbidden = client.get("/api/plugins/alpha/forbidden")
    assert forbidden.status_code == 403
    assert b"no way" in forbidden.data  # abort 的描述透传
    assert client.get("/api/plugins/alpha/crash").status_code == 500  # 未捕获异常走宿主 500


# ---------------------------------------------------------------- 差分对拍


_DIFF_BATTERY = [
    ("GET", "/api/plugins/alpha/rooms/12/content/search"),
    ("GET", "/api/plugins/alpha/rooms/12/content/search?q=fireball"),
    ("GET", "/api/plugins/alpha/rooms/twelve/content/search"),
    ("GET", "/api/plugins/alpha/echo"),
    ("POST", "/api/plugins/alpha/echo"),
    ("PUT", "/api/plugins/alpha/echo"),
    ("DELETE", "/api/plugins/alpha/echo"),
    ("HEAD", "/api/plugins/alpha/echo"),
    ("OPTIONS", "/api/plugins/alpha/echo"),
    ("OPTIONS", "/api/plugins/alpha/custom-options"),
    ("GET", "/api/plugins/alpha/multi/x/y/z"),
    ("GET", "/api/plugins/alpha/calc/7/2.5"),
    ("GET", "/api/plugins/alpha/calc/7/abc"),
    ("GET", "/api/plugins/alpha/page/3"),
    ("GET", "/api/plugins/alpha/page/"),
    ("GET", "/api/plugins/alpha/loose"),
    ("GET", "/api/plugins/alpha/loose/"),
    ("PUT", "/api/plugins/alpha/slash"),
    ("GET", "/api/plugins/alpha/slash"),
    ("GET", "/api/plugins/alpha/boom"),
    ("GET", "/api/plugins/alpha/forbidden"),
    ("GET", "/api/plugins/alpha/crash"),
    ("GET", "/api/plugins/alpha/void"),
    ("GET", "/api/plugins/alpha"),
    ("GET", "/api/plugins/alpha/"),
    ("DELETE", "/api/plugins/alpha/"),
    ("GET", "/api/plugins/ghost/echo"),
]


def test_differential_vs_native_registration():
    """路线 A 的核心证据：同一蓝图对象原生挂载 vs 代理挂载，逐请求行为一致。

    status / body / Allow / Location 全等 → 「无损保真到现有 dnd5e http.py
    用法」成立（对拍蓝图刻意复刻了 dnd5e/veil-map 的规则形态与边界）。
    """
    alpha = _alpha_blueprint()
    native = Flask("native", static_folder=None)
    native.register_blueprint(alpha)  # 今天的生产挂法

    proxied = Flask("proxied", static_folder=None)
    proxied.register_blueprint(bp)
    route_proxy.mount(proxied, alpha, "alpha")  # 同一个蓝图对象，零改动入表

    native_client = native.test_client()
    proxied_client = proxied.test_client()
    for method, path in _DIFF_BATTERY:
        native_response = native_client.open(path, method=method)
        proxy_response = proxied_client.open(path, method=method)
        context = (method, path)
        assert proxy_response.status_code == native_response.status_code, context
        if method != "HEAD":  # HEAD 的空 body 由 WSGI 层保证，只比状态
            assert proxy_response.data == native_response.data, context
        assert proxy_response.headers.get("Allow") == native_response.headers.get("Allow"), context
        assert proxy_response.headers.get("Location") == native_response.headers.get("Location"), context


def test_proxy_methods_declared_for_catch_all():
    """总路由方法面 = 平台约定的全部常规方法（TRACE/CONNECT 等除外，见模块头）。"""
    assert PROXY_METHODS == ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")


# ---------------------------------------------------------------- 已知限制


def test_url_for_build_error_is_the_documented_limitation():
    """钉住已知限制：插件 endpoint 的 url_for 反查在代理 app 上 BuildError。

    现役插件未用 url_for；接线时的缓解项是宿主 ``app.url_build_error_handlers``
    逐插件表 build（见 route_proxy 模块头「不支持面」）。
    """
    alpha = _alpha_blueprint()
    native = Flask("native-url", static_folder=None)
    native.register_blueprint(alpha)
    proxied = _make_app()
    route_proxy.mount(proxied, alpha, "alpha")

    with native.test_request_context("/api/plugins/alpha/echo"):
        assert url_for("alpha_plugin.echo") == "/api/plugins/alpha/echo"
    with proxied.test_request_context("/api/plugins/alpha/echo"):
        with pytest.raises(BuildError):
            url_for("alpha_plugin.echo")
