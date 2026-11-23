"""剧本写盘后列表缓存必须立即失效（导入后不再等 60 秒）。"""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from trpg_server import scenario_store
from trpg_server.routes import scenarios as scenario_routes


def _app(tmp_path: Path) -> Flask:
    app = Flask(__name__)
    app.config.update(SCENARIOS_DIR=tmp_path / "scenarios")
    (tmp_path / "scenarios").mkdir(parents=True, exist_ok=True)
    return app


def _record(scenario_id: int, title: str) -> dict:
    return {
        "id": scenario_id,
        "title": title,
        "scenario_version": "1.0.0",
        "modules": [],
        "import_mode": "direct",
        "createdAt": "2026-11-23T00:00:00",
        "updatedAt": "2026-11-23T00:00:00",
    }


def test_save_scenario_record_invalidates_list_cache(tmp_path):
    app = _app(tmp_path)
    with app.app_context():
        scenario_routes.clear_scenarios_cache()
        assert scenario_routes.load_scenarios() == []

        path = scenario_store.save_scenario_record(
            tmp_path / "scenarios", _record(1795000000001, "新导入的世界包")
        )
        # 缓存若未失效，这里拿到的仍是空列表（旧行为 60 秒后才出现）。
        titles = [item.get("title") for item in scenario_routes.load_scenarios()]
        assert "新导入的世界包" in titles
        assert path.exists()
