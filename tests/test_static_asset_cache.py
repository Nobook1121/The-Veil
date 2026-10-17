from pathlib import Path

from trpg_server.app_factory import create_app


class _Manager:
    def is_session_current(self, user_id, token): return True
    def get_user_by_id(self, user_id): return {"id": user_id, "username": "admin", "role": "ADMIN"}


def _client(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "USER_MANAGER": _Manager(),
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "EMBEDDED_VECTOR_DB_PATH": tmp_path / "vectors",
        }
    )
    return app.test_client()


def test_index_and_assets_are_served_with_no_cache(tmp_path: Path):
    """前端产物文件名固定、不含内容指纹，必须每次回源校验。

    否则前端更新后，手机等设备会按 Last-Modified 启发式缓存继续加载旧样式，
    表现为「改了 CSS 但界面上看不到变化」。
    """
    client = _client(tmp_path)

    index = client.get("/")
    assert index.status_code == 200
    assert "no-cache" in index.headers.get("Cache-Control", "")

    stylesheet = client.get("/js/react/main.css")
    assert stylesheet.status_code == 200
    assert "no-cache" in stylesheet.headers.get("Cache-Control", "")