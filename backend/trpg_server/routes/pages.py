from flask import Blueprint, abort, send_from_directory

from trpg_server.settings import BASE_DIR, FRONTEND_DIST_DIR

bp = Blueprint("pages", __name__)


def _no_cache(response):
    """页面与本地资源必须每次回源校验。

    这些文件名固定（如 ``js/react/main.css``）且不含内容指纹，若交给浏览器按
    Last-Modified 做启发式长期缓存，前端更新后手机等设备会持续加载旧样式，表现为
    「改了 CSS 但界面上看不到变化」。强制 ``no-cache``：浏览器仍可用 304 复用缓存，
    但每次都会先问一次服务器，保证拿到的是最新文件。
    """
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@bp.route("/")
def serve_index():
    return _no_cache(send_from_directory(FRONTEND_DIST_DIR, "index.html"))


@bp.route("/<path:path>")
def serve_static(path):
    if path == "data" or path.startswith("data/"):
        abort(404)
    if (FRONTEND_DIST_DIR / path).is_file():
        return _no_cache(send_from_directory(FRONTEND_DIST_DIR, path))
    return _no_cache(send_from_directory(BASE_DIR, path))
