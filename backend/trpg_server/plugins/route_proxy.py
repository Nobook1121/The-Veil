"""HTTP 蓝图可逆化 · 插件代理路由层（cordis-paradigm-adoption-plan.md §4 阶段 3）。

**已接线（阶段 3 正式接线完成）**：宿主只注册本模块的总蓝图
:data:`bp` 一次，插件蓝图全部经 :meth:`PluginRouteProxy.mount` 进路由表——
含 ``http_blueprints`` 的插件禁用后路由**立即** 404，``pending_restart`` /
``restart_required`` 成为历史。接线点：

- ``app_factory.create_app``：核心蓝图全部注册**之后**经
  ``bootstrap.register_plugin_blueprints(app)`` 注册总蓝图（顺序硬约束，
  见下方「接线顺序约束」）；
- ``registry._register_blueprint_entries``：蓝图入册改为 mount 进本表，
  挂在插件自己的效果链上——``ctx.dispose()`` 时 unmount，与阶段 2 的
  撤销链合流；``enable`` mount 即生效，``disable`` / ``uninstall`` 摘表。

    app.register_blueprint(route_proxy.bp)              # 总蓝图，注册一次
    route_proxy.mount(app, plugin_bp, plugin_id)        # 每个插件入表
    # 禁用插件：route_proxy.unmount(plugin_id) → 立即 404，重启不再必需

机制
----
Flask 蓝图注册不可撤销是平台事实：规则进了宿主 ``url_map`` 就摘不出来
（registry 的 ``pending_restart`` 就是这个现实的表达）。路线 A 把「注册」
改成「入表」：

- 宿主把总蓝图 :data:`bp` 注册**一次**，三条 catch-all 规则接住整个
  ``/api/plugins/<plugin_id>/...`` 命名空间；
- :meth:`PluginRouteProxy.mount` **不调用** ``app.register_blueprint``：
  插件蓝图在一次性 scratch app 上走 Flask **原生注册路径**（url_prefix 合并、
  methods/defaults 归一化、``provide_automatic_options`` 全部由 Flask 自己
  完成），然后把 scratch app 的 ``url_map`` 与 ``view_functions`` 搬进本插件的
  路由表——匹配保真度因此等同原生注册，而不是手工解析规则的近似；
- 分发时用这张真实的 ``url_map`` 匹配当前请求，命中后直接调用插件 view
  function；表按 ``plugin_id`` 整体挂摘，禁用 = 摘表（可逆、即时、零重启）。

保真面（与原生 ``app.register_blueprint`` 差分对拍过，见
``tests/test_plugin_route_proxy.py`` 的对拍用例）
----------------------------------------------------------------
- url 转换器：int / float / path / any / uuid，含宿主 ``url_map.converters``
  注册的自定义转换器（mount 时从宿主继承）；
- ``defaults``（含 redirect_defaults 别名重定向）、``strict_slashes``、
  ``merge_slashes``（308 重定向）；
- 多 methods、405 + Allow 头、HEAD、自动 OPTIONS 与 ``methods=["OPTIONS"]``
  的自定义 OPTIONS view；
- view 返回值转换、``abort`` / ``HTTPException`` 透传、未捕获异常走宿主 500；
- ``request`` / ``session`` / ``current_app`` / ``g`` 解析到**真实宿主 app**
  （代理视图运行在真实请求上下文里）；异步 view 经宿主 ``ensure_sync``。

不支持面（逐项结论）
--------------------
- ``url_for`` 反查：插件 endpoint 不在宿主 ``url_map`` → ``BuildError``。
  现役三插件（dnd5e / veil-map / dnd5e-srd）均未使用（已 grep 复核）。接线
  时可加 ``app.url_build_error_handlers`` 兜底（逐插件表 build，宿主名/相对
  路径语义不变），本 spike 未实现——接线前插件需规避。
- 插件蓝图钩子 ``before_request`` / ``after_request`` / ``teardown_*`` /
  ``url_value_preprocessors`` / ``url_default_functions``：注册进 scratch app
  即被丢弃；宿主 app 级钩子照常作用于代理响应。插件需规避；接线时若确有
  需求，可在 :meth:`PluginRouteProxy.dispatch` 外层补插件级钩子转发，语义
  另行评审。
- ``blueprint.errorhandler``：插件视图的异常发生在总蓝图视图里，只匹配
  宿主级与总蓝图级 handler。插件需在 view 内自捕转 ``error_response``——
  现役插件（dnd5e ``http.py``）本来就是这个写法。
- ``request.blueprint`` / ``request.endpoint`` / ``request.url_rule`` /
  ``request.view_args`` 在插件 view 内看到的是总蓝图的值
  （``plugin_route_proxy``），不是插件蓝图的。现役插件未读这些值。
- 插件蓝图 ``template_folder``：jinja loader 只收录注册在宿主上的蓝图，
  ``render_template`` 找不到插件模板；``render_template_string`` / 自读文件
  可用。蓝图 ``static_folder`` 路由反而**可用**（``send_static_file`` 只依赖
  蓝图自身属性，随表代理）。现役插件未用模板。
- 接线顺序约束：同一路径 pattern 上「先注册者赢」（Werkzeug state 的 rules
  按注册顺序匹配）。**总蓝图必须注册在核心蓝图之后**——``app_factory`` 的
  ``register_blueprints(app)`` → ``register_plugin_blueprints(app)`` 调用顺序
  就是这个约束的保障（管理面 ``DELETE /api/plugins/<id>``、
  ``POST /api/plugins/<id>/enable|disable``、``GET /api/plugins/<id>/assets/...``
  等核心静态段路由因此永远优先）；反例见 tests 的顺序钉住用例
  （``tests/test_plugin_route_wiring.py`` 在真实启动路径上钉住同一约束，
  总蓝图先注册会吞掉 ``DELETE /api/plugins/<id>``）。
- 超出 :data:`PROXY_METHODS` 的 exotic 方法（TRACE / CONNECT…）到不了插件表，
  外层 405 的 Allow 头列总蓝图方法集。现役插件未使用。
- ``websocket=True`` 的规则不在代理面（插件 HTTP 契约本就是普通 WSGI 路由）。

评审注记（阶段 3 spike 结论，已按接线评审清单逐条落地）
------------------------------------------------------
1. 可行性：路线 A 可**无损**承接现有插件形态——dnd5e ``http.py`` 与
   veil-map ``entry.py`` 的 ``Blueprint(url_prefix="/api/plugins/<id>")`` +
   ``@bp.route`` 用法零改动；匹配/分发语义与原生注册差分对拍一致（status /
   body / Allow / Location 逐请求相等）。
2. 兼容性风险（现役 3 插件）：无。不用 url_for / 钩子 / errorhandler / 模板 /
   ``request.blueprint``；veil-map 的 ``send_from_directory``、
   ``current_app.config``、``session`` 全部经真实请求上下文。第三方插件风险
   集中在上面的「不支持面」，已写进插件开发文档的「当前限制」。
3. 接线落地清单（逐条照办）：
   - 总蓝图注册顺序（核心之后）：``app_factory`` 调用顺序即约束，
     ``tests/test_plugin_route_wiring.py`` 双重钉住（url_map 注册顺序 +
     管理面路由行为）；
   - 插件蓝图不再直接 ``app.register_blueprint``：``registry`` 的
     ``_register_blueprint_entries`` 改为 ``mount`` 入表、挂插件自己的
     效果链，``enable`` mount 即生效、不再需要补挂路径；
   - auth 短路维持「插件 view 自带」（dnd5e/veil-map 现状）；管理面路由是
     核心静态路由、不经过本层；
   - CSP / 安全头由宿主 after_request 照常覆盖代理响应；性能——每请求一次
     dict 查表 + ``MapAdapter.bind`` 常数开销（规则已在 mount 时编译并预热
     排序），远小于一次视图执行的量级；
   - 契约收口：``restart_required`` 与 ``PluginRecord.pending_restart`` 字段
     **保留但恒为 false**（API 兼容），``REASON_RESTART`` 不再产生；
     ``docs/plugin-development.md`` 生命周期节同步改写。
4. 路线 B 反证：未发现。路线 A 的全部缺口都有「插件需规避」或「接线补钩」的
   明确出路，且现役插件已天然规避；维持「路线 A 推进、路线 B 保底」结论。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from flask import Blueprint, Flask, abort, current_app, request
from werkzeug.exceptions import HTTPException, NotFound

logger = logging.getLogger(__name__)

#: 总蓝图名（接线时宿主 ``app.register_blueprint(bp)`` 只注册一次）。
PROXY_BLUEPRINT_NAME = "plugin_route_proxy"

#: 代理层接受的 HTTP 方法。插件表内按各自 rule 精确判 405；显式包含 OPTIONS
#: 会让 Flask 关掉总路由的自动 OPTIONS（``provide_automatic_options=False``），
#: 把 OPTIONS 请求交进 :meth:`PluginRouteProxy.dispatch` 按插件表语义应答。
PROXY_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")

# scratch app 只用来走一遍 Flask 原生注册路径；static_folder=None 保证它
# 自己不产生 static 规则（插件蓝图自带的 static 路由仍会随表代理）。
_SCRATCH_APP_NAME = "plugin_route_proxy_scratch"

_EXPECTED_PREFIX = "/api/plugins/{plugin_id}"


class _RouteTable:
    """一个插件的全部已解析路由；mount 完成后只读，保证分发线程无锁。"""

    __slots__ = (
        "plugin_id",
        "url_map",
        "views",
        "blueprint_names",
        "rule_count",
        "out_of_namespace",
    )

    def __init__(
        self,
        plugin_id: str,
        url_map: Any,
        views: dict[str, Any],
        blueprint_names: tuple[str, ...],
        rule_count: int,
        out_of_namespace: tuple[str, ...],
    ) -> None:
        self.plugin_id = plugin_id
        self.url_map = url_map
        self.views = views
        self.blueprint_names = blueprint_names
        self.rule_count = rule_count
        self.out_of_namespace = out_of_namespace


class PluginRouteProxy:
    """插件代理路由层：mount 入表 / unmount 摘表 / dispatch 查表分发。

    表结构 ``plugin_id → _RouteTable``。启停是单线程管理路径（与 kernel 的
    并发前提一致），挂摘在 ``_lock`` 内串行化；mount 后的表对象不可变，
    分发线程只做无锁 dict 读取。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, _RouteTable] = {}

    # -- 只读视图 ---------------------------------------------------------
    def is_mounted(self, plugin_id: str) -> bool:
        return str(plugin_id or "") in self._tables

    def mounted_ids(self) -> list[str]:
        return sorted(self._tables)

    def route_count(self, plugin_id: str) -> int:
        """插件当前入表的规则数（启停往返「零残留 / 无叠加」的对拍口径）。"""
        table = self._tables.get(str(plugin_id or ""))
        return table.rule_count if table is not None else 0

    # -- 挂载 / 摘除 -----------------------------------------------------
    def mount(self, app: Any, blueprint: Any, plugin_id: str) -> None:
        """把插件蓝图解析进路由表（幂等：同 id 重挂 = 摘旧表换新表）。

        ``app`` 只用来继承 ``url_map.converters`` 里的自定义转换器，可为
        ``None``（registry 独立于宿主 app 使用时，scratch app 用 Werkzeug
        默认转换器，平台宿主未注册自定义转换器所以语义等同）；本方法
        **绝不**在宿主 app 上 ``register_blueprint``——插件路由对宿主
        ``url_map`` 不可见，摘除才可逆。``blueprint`` 可传单个 Blueprint 或
        其列表/元组：manifest ``http_blueprints.blueprints`` 声明多蓝图时一次
        mount 全量入表（unmount 按插件整体摘除，所以不要对同一插件分次挂）。
        蓝图本身非法（坏规则等）会在这里抛出，由调用方决定该插件的命运。
        """
        items = list(blueprint) if isinstance(blueprint, (list, tuple)) else [blueprint]
        items = [item for item in items if item is not None]
        key = str(plugin_id or "").strip()
        if not key:
            raise ValueError("plugin_id 不能为空")
        if not items:
            raise ValueError("插件至少要提供一个蓝图")
        table = self._extract(app, items, key)
        with self._lock:
            self._tables[key] = table
        logger.debug(
            "plugin_routes_mounted plugin=%s blueprints=%s rules=%s",
            key,
            ",".join(table.blueprint_names),
            table.rule_count,
        )
        if table.out_of_namespace:
            logger.warning(
                "plugin_routes_out_of_namespace plugin=%s expected=%s unreachable_rules=%s",
                key,
                _EXPECTED_PREFIX.format(plugin_id=key),
                ",".join(table.out_of_namespace),
            )

    def unmount(self, plugin_id: str) -> bool:
        """按插件摘表，路由立即 404；返回是否真的摘到了表。"""
        key = str(plugin_id or "")
        with self._lock:
            table = self._tables.pop(key, None)
        if table is None:
            return False
        logger.debug("plugin_routes_unmounted plugin=%s rules=%s", key, table.rule_count)
        return True

    def unmount_all(self) -> list[str]:
        """摘除全部表（全量 rescan / 测试清理用）；返回被摘的插件 id。"""
        with self._lock:
            removed = sorted(self._tables)
            self._tables.clear()
        if removed:
            logger.debug("plugin_routes_unmounted_all plugins=%s", ",".join(removed))
        return removed

    # -- 分发 -------------------------------------------------------------
    def dispatch(self, plugin_id: str, rest: str = "") -> Any:
        """总入口视图体：查表 → 匹配 → 调用插件 view function。

        ``rest`` 仅为路由占位符的透传，匹配一律用 ``request.path``（与原生
        dispatch 同源的解码路径，规避重拼接歧义）。匹配用的是 mount 时从原生
        注册搬来的那张 ``url_map``，因此语义与原生挂载逐位一致：

        - 插件未挂载 / 路径未命中 → 404；
        - ``MethodNotAllowed``（405 + Allow）、``RequestRedirect``
          （strict / merge slashes 的 308）原样外抛，由 Flask 按原生方式渲染；
        - OPTIONS 命中自动选项规则 → 与原生 ``make_default_options_response``
          同构的 Allow 应答（方法集取自插件表，不是总蓝图的）；
        - 其余交给插件 view function（经宿主 ``ensure_sync``，异步视图可用）；
          返回值 / abort / HTTPException / 未捕获异常全部沿用宿主外层分发语义。
        """
        table = self._tables.get(str(plugin_id or ""))
        if table is None:
            abort(404)
        adapter = table.url_map.bind_to_environ(request.environ)
        try:
            rule, view_args = adapter.match(request.path, request.method, return_rule=True)
        except NotFound:
            abort(404)
        except HTTPException:
            raise  # 405 / 308 / WebsocketMismatch 等语义透传
        if request.method == "OPTIONS" and getattr(rule, "provide_automatic_options", False):
            response = current_app.response_class()
            response.allow.update(list(adapter.allowed_methods()) or sorted(rule.methods or ()))
            return response
        view = table.views.get(rule.endpoint)
        if view is None:  # 与规则同源抽取，理论不可达；保底不泄露内部结构
            logger.warning("plugin_route_view_missing plugin=%s endpoint=%s", plugin_id, rule.endpoint)
            abort(404)
        return current_app.ensure_sync(view)(**view_args)

    # -- 内部 --------------------------------------------------------------
    def _extract(self, app: Any, blueprints: list[Any], plugin_id: str) -> _RouteTable:
        """一次性 scratch app 上走 Flask 原生注册，再搬走 map 与 view。

        用原生注册路径提取（而不是手工解析 ``deferred_functions``），保证
        url_prefix 合并、methods / defaults 归一化、provide_automatic_options
        等全部与宿主原生挂载逐位一致。scratch app 本体在返回后被丢弃，只留
        它的 ``url_map``（Werkzeug Map 与 app 无关）与 view 映射。
        """
        scratch = Flask(_SCRATCH_APP_NAME, static_folder=None)
        host_converters = getattr(getattr(app, "url_map", None), "converters", None)
        if isinstance(host_converters, dict) and host_converters:
            scratch.url_map.converters.update(host_converters)
        names: list[str] = []
        for item in blueprints:
            scratch.register_blueprint(item)
            names.append(str(getattr(item, "name", "") or ""))
        rules = [rule for rule in scratch.url_map.iter_rules() if rule.endpoint != "static"]
        views = {
            endpoint: view for endpoint, view in scratch.view_functions.items() if endpoint != "static"
        }
        expected = _EXPECTED_PREFIX.format(plugin_id=plugin_id)
        # catch-all 只接得住本插件的命名空间；蓝图前缀跑偏的规则会入表但不可达，
        # 在 mount 时点名告警（现有插件契约就是 /api/plugins/<id> 前缀）。
        out_of_namespace = sorted(
            {
                str(rule.rule)
                for rule in rules
                if rule.rule != expected and not rule.rule.startswith(expected + "/")
            }
        )
        url_map = scratch.url_map
        url_map.update()  # 预热动态转移排序：之后的 match 只读，分发线程无锁
        return _RouteTable(
            plugin_id=plugin_id,
            url_map=url_map,
            views=views,
            blueprint_names=tuple(names),
            rule_count=len(rules),
            out_of_namespace=tuple(out_of_namespace),
        )


# ---------------------------------------------------------------- 总蓝图

bp = Blueprint(PROXY_BLUEPRINT_NAME, __name__)
route_proxy = PluginRouteProxy()


@bp.route("/api/plugins/<plugin_id>/<path:rest>", methods=PROXY_METHODS)
def _dispatch(plugin_id: str, rest: str) -> Any:
    """总入口：按插件查表分发；未挂载 / 未命中 → 404。"""
    return route_proxy.dispatch(plugin_id, rest)


@bp.route("/api/plugins/<plugin_id>", methods=PROXY_METHODS)
def _dispatch_root(plugin_id: str) -> Any:
    """零尾段入口：兜住 ``url_prefix="/api/plugins/<id>"`` + 根规则 ``"/"`` 的形态。"""
    return route_proxy.dispatch(plugin_id, "")


@bp.route("/api/plugins/<plugin_id>/", methods=PROXY_METHODS)
def _dispatch_root_slash(plugin_id: str) -> Any:
    """根规则带斜杠变体：``url_prefix + "/"`` 的原生归一是分支路径（尾斜杠）。"""
    return route_proxy.dispatch(plugin_id, "")


__all__ = [
    "PROXY_BLUEPRINT_NAME",
    "PROXY_METHODS",
    "PluginRouteProxy",
    "bp",
    "route_proxy",
]
