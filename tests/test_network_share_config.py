"""房间分享地址（network.share_base_url）相关接口的回归测试。"""

from trpg_server.app_factory import create_app


class FakeManager:
    def __init__(self, role="ADMIN"):
        self.role = role

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "admin", "role": self.role}


def _app(tmp_path, role="ADMIN"):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(role),
            "CONFIG_DIR": config_dir,
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
        }
    )


def _login(client, role="ADMIN"):
    with client.session_transaction() as session:
        session.update(
            user_id=1,
            username="tester",
            role=role,
            session_token="token",
            csrf_token="csrf",
        )


def test_share_addresses_exposes_local_candidates_and_default_config(tmp_path):
    client = _app(tmp_path).test_client()

    response = client.get("/api/network/addresses")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["port"] == 8086
    assert payload["share_base_url"] == ""
    assert payload["addresses"]
    assert all(
        address.startswith("http://") and address.endswith(":8086")
        for address in payload["addresses"]
    )


def test_share_base_url_rejects_non_http_value(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)

    response = client.post(
        "/api/network/config",
        json={"share_base_url": "192.168.1.10:8086", "port": 8086},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 400


def test_share_base_url_is_persisted_and_can_be_cleared(tmp_path):
    client = _app(tmp_path).test_client()
    _login(client)

    saved = client.post(
        "/api/network/config",
        json={"share_base_url": "http://192.168.1.10:8086", "port": 8086},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert saved.status_code == 200
    assert (
        client.get("/api/network/addresses").get_json()["data"]["share_base_url"]
        == "http://192.168.1.10:8086"
    )

    cleared = client.post(
        "/api/network/config",
        json={"share_base_url": "", "port": 8086},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert cleared.status_code == 200
    assert client.get("/api/network/addresses").get_json()["data"]["share_base_url"] == ""
