from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_frontend_source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_register_password_strength_messages_are_localized():
    source = read_frontend_source("frontend/src/app/auth/api.ts")

    assert '"Password must be at least 8 characters": "密码至少需要 8 个字符"' in source
    assert '"Password must include a letter": "密码必须包含字母"' in source
    assert '"Password must include a number": "密码必须包含数字"' in source


def test_character_editor_save_is_guarded_against_duplicate_clicks():
    source = read_frontend_source("frontend/src/app/character-sheet.ts")

    assert "let characterSaveInFlight = false;" in source
    assert "if (characterSaveInFlight) return;" in source
    assert "characterSaveInFlight = true;" in source
    assert "characterSaveInFlight = false;" in source
    assert 'setButtonBusy("saveCharacter", true);' in source
    assert 'setButtonBusy("saveCharacter", false);' in source


def test_room_write_actions_are_guarded_against_duplicate_clicks():
    source = read_frontend_source("frontend/src/app/rooms.ts")

    assert "const roomActionLocks = new Set<string>();" in source
    assert 'return runRoomAction("create-room", "confirmCreateSave", async () => {' in source
    assert 'return runRoomAction("join-room", "joinRoom", async () => {' in source
    assert 'return runRoomAction(`bind-character:${currentRoom.id}:${userId}`, "confirmRoomCharacterBind", async () => {' in source
    assert "window.recordCharacterChange = recordCharacterChange;" in source


def test_chat_input_supports_enter_to_send_message():
    source = read_frontend_source("frontend/src/app/chat.ts")

    assert 'activeChatInput.addEventListener("keydown"' in source
    assert 'event.key === "Enter"' in source
    assert "event.shiftKey" in source
    assert "void sendMessage();" in source
