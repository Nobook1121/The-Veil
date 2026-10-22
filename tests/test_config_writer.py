"""注释保留写入：定点更新、键序/注释保留、新 section 追加、缓存失效。"""

import tomlkit

from trpg_server.config.merge import load_settings
from trpg_server.config.writer import update_sections

INITIAL = """\
# 头部注释
[ai]
# 超时注释
ai_request_timeout = 600
stream_output = true

[chat]
message_font_size = 16
"""


def _load(path):
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def test_updates_only_listed_keys_and_preserves_comments(tmp_path):
    path = tmp_path / "general.toml"
    path.write_text(INITIAL, encoding="utf-8")
    update_sections(path, {"ai": {"ai_request_timeout": 900}})
    text = path.read_text(encoding="utf-8")
    assert "# 头部注释" in text
    assert "# 超时注释" in text
    doc = _load(path)
    assert doc["ai"]["ai_request_timeout"] == 900
    assert doc["ai"]["stream_output"] is True
    assert doc["chat"]["message_font_size"] == 16


def test_adds_new_key_and_new_section(tmp_path):
    path = tmp_path / "general.toml"
    path.write_text(INITIAL, encoding="utf-8")
    update_sections(path, {"chat": {"show_timestamp": False}, "update": {"auto_update": True}})
    doc = _load(path)
    assert doc["chat"]["show_timestamp"] is False
    assert doc["update"]["auto_update"] is True


def test_creates_file_when_missing(tmp_path):
    path = tmp_path / "sub" / "general.toml"
    update_sections(path, {"ai": {"debug_mode": True}})
    assert _load(path)["ai"]["debug_mode"] is True


def test_nested_section_written_as_dotted_table(tmp_path):
    path = tmp_path / "general.toml"
    update_sections(path, {"ai.cache": {"semantic_enabled": False}})
    assert _load(path)["ai"]["cache"]["semantic_enabled"] is False


def test_write_invalidates_settings_cache(tmp_path):
    path = tmp_path / "general.toml"
    path.write_text(INITIAL, encoding="utf-8")
    assert load_settings(tmp_path, env={}).ai.ai_request_timeout == 600
    # 600 → 901 长度不变，验证的是显式缓存失效而非 mtime/大小变化。
    update_sections(path, {"ai": {"ai_request_timeout": 901}})
    assert load_settings(tmp_path, env={}).ai.ai_request_timeout == 901
