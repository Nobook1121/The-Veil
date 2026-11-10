"""关于页数据与版本比较的基础契约。"""

from trpg_server import update_service
from trpg_server.app_info import build_app_info, load_credits
from trpg_server.version import APP_VERSION


def test_compare_versions_orders_releases():
    assert update_service.compare_versions("1.0.0", "0.9.9") == 1
    assert update_service.compare_versions("v1.0.0", "1.0.0") == 0
    assert update_service.compare_versions("1.0.0", "1.0.1") == -1
    assert update_service.compare_versions("1.0.0-beta.1", "1.0.0") == -1
    assert update_service.compare_versions("1.0.0", "1.0.0-beta.1") == 1


def test_normalize_repo_accepts_url_and_slug():
    assert update_service.normalize_repo("Nobook1121/The-Veil") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("https://github.com/Nobook1121/The-Veil.git") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("https://github.com/Nobook1121/The-Veil/releases") == "Nobook1121/The-Veil"
    assert update_service.normalize_repo("") == ""
    assert update_service.normalize_repo("not-a-repo") == ""


def test_build_app_info_has_stable_shape():
    info = build_app_info()
    assert info["name"] == "帷幕 The Veil"
    assert info["version"] == APP_VERSION
    assert info["build"]["mode"] in {"dev", "release"}
    assert info["license"]["spdx"] == "Apache-2.0"
    assert info["repo"]["github"] == "Nobook1121/The-Veil"
    assert isinstance(info["runtime"]["python"], str)


def test_load_credits_returns_lists():
    credits = load_credits()
    for key in ("contributors", "special_thanks", "dependencies", "sponsor_channels", "sponsors"):
        assert isinstance(credits[key], list)
