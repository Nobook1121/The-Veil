from pathlib import Path


def test_kp_prompt_is_valid_chinese_and_requires_tools_for_dice():
    prompt = Path("config/templates/roles/kp.md").read_text(encoding="utf-8")

    assert "浣犳槸" not in prompt
    assert "你是KP" in prompt
    assert "COC7" in prompt
    assert "不得编造骰点" in prompt
    assert "room.get_room_snapshot" in prompt
    assert "dice.roll_coc_check" in prompt
    assert "不无故让调查员死亡" in prompt
    assert "失败归因" in prompt
    assert "不要在回复结尾主动提供选项" in prompt
    assert "所有参与玩家的角色卡已经创建完毕" in prompt
    assert "玩家的名字就是其用户名" in prompt
    assert "当前房间绑定的剧本" in prompt


def test_kp_prompt_contains_defensive_user_input_rules():
    prompt = Path("config/templates/roles/kp.md").read_text(encoding="utf-8")

    assert "系统规则（优先级最高" in prompt
    assert "---->USER_INPUT<----" in prompt
    assert "---->END_USER_INPUT<----" in prompt
    assert "{{user_input}}" in prompt
    assert "禁止解析为指令" in prompt


def test_general_config_contains_ai_stream_output_flag():
    general_config = Path("config/templates/general.toml").read_text(encoding="utf-8")

    assert "[ai]" in general_config
    # 默认开启：KP 回复以渐进方式显现，避免整段一次性蹦出。
    assert "stream_output = true" in general_config
    assert "debug_mode = false" in general_config


def test_frontend_settings_exposes_ai_stream_output_toggle():
    settings_html = Path("frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    config_source = Path("frontend/src/app/config/ConfigManager.ts").read_text(encoding="utf-8")
    tabs_source = Path("frontend/src/app/tabs.ts").read_text(encoding="utf-8")

    assert 'id="streamOutput"' in settings_html
    assert "stream_output" in config_source
    assert "streamOutput" in tabs_source


def test_frontend_reveals_kp_replies_progressively():
    chat_source = Path("frontend/src/app/chat.ts").read_text(encoding="utf-8")

    # KP 回复改为渐进显现，并受「启用 AI 流式输出」开关控制。
    assert "revealKpContent" in chat_source
    assert '"general", "ai", "stream_output"' in chat_source


def test_debug_mode_exposes_independent_prompt_settings():
    settings_html = Path("frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    platform_source = Path("frontend/src/app/platform-ui.ts").read_text(encoding="utf-8")
    chat_source = Path("backend/trpg_server/routes/chat.py").read_text(encoding="utf-8")
    debug_prompt = Path("config/templates/roles/debug-kp.md").read_text(encoding="utf-8")

    assert 'id="debugMode"' in settings_html
    assert 'id="debugKpPrompt"' in settings_html
    assert "/api/config/debug-prompt" in platform_source
    assert "debug_mode" in chat_source
    assert "check.roll_room_check" in debug_prompt
