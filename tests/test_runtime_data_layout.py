from pathlib import Path

from trpg_server import settings


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_data_defaults_live_under_data_runtime():
    runtime_dir = settings.DATA_DIR / "runtime"

    assert settings.RUNTIME_DIR == runtime_dir
    assert settings.CHARACTERS_DIR == runtime_dir / "characters"
    assert settings.HISTORY_DIR == runtime_dir / "history"
    assert settings.LOGS_DIR == runtime_dir / "logs"
    assert settings.ROOMS_DIR == runtime_dir / "rooms"
    assert settings.USERS_DIR == runtime_dir / "users"


def test_backend_settings_paths_are_rooted_at_repository_root():
    assert settings.BASE_DIR == ROOT
    assert settings.DATA_DIR == ROOT / "data"
    assert settings.FRONTEND_DIST_DIR == ROOT / "dist" / "public"