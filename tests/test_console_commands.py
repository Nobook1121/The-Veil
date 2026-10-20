class FakeUserManager:
    def __init__(self):
        self.users = [
            {"id": 1, "username": "alice", "role": "USER"},
            {"id": 2, "username": "bob", "role": "USER"},
        ]
        self.updated = []

    def get_all_users(self):
        return self.users

    def update_user_role(self, user_id, role):
        self.updated.append((user_id, role))
        return True, "Role updated successfully"


class FakeSessionUserManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {
            "id": user_id,
            "username": "alice",
            "email": "alice@example.com",
            "role": "ADMIN",
        }


def test_console_command_promotes_user_by_username():
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    result = execute_console_command("user promote alice ADMIN", user_manager=manager)

    assert result.ok
    assert manager.updated == [(1, "ADMIN")]
    assert "alice" in result.message


def test_console_command_rejects_unknown_command():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("does-not-exist")

    assert not result.ok
    assert "Unknown command" in result.message


def test_console_ai_character_requires_generate_subcommand():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("ai character")

    assert not result.ok
    assert "Usage" in result.message


def test_console_ai_character_generate_reports_card(monkeypatch):
    from trpg_server import console_commands
    from trpg_server.app_factory import create_app

    monkeypatch.setattr(
        console_commands,
        "run_character_generation",
        lambda description, scenario_context=None, **kwargs: {
            "character": {"name": "林清", "occupationName": "医生"},
            "token_count": 321,
            "model": "deepseek-flash",
            "platform": "deepseek",
            "finish_reason": "stop",
            "content_length": 3800,
            "reasoning_length": 900,
        },
    )
    app = create_app({"TESTING": True})

    result = console_commands.execute_console_command("ai character generate 一位医生", app=app)

    assert result.ok
    assert "林清" in result.message
    assert "deepseek-flash" in result.message


def test_console_ai_abort_then_resume_controls_retry_flag():
    from trpg_server import console_commands
    from trpg_server.agents.http_retry import is_abort_requested

    assert not is_abort_requested()
    abort = console_commands.execute_console_command("ai abort")
    assert abort.ok
    assert is_abort_requested()
    resume = console_commands.execute_console_command("ai resume")
    assert resume.ok
    assert not is_abort_requested()


def test_console_command_demotes_user_to_user_by_default():
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()
    manager.users[0]["role"] = "ADMIN"

    result = execute_console_command("user demote alice", user_manager=manager)

    assert result.ok
    assert manager.updated == [(1, "USER")]
    assert "USER" in result.message


def test_console_promote_command_accepts_any_target_role():
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()
    manager.users[0]["role"] = "OWNER"

    # 通过 promote 显式传入较低角色，同样可以实现降级。
    result = execute_console_command("user promote alice USER", user_manager=manager)

    assert result.ok
    assert manager.updated == [(1, "USER")]


def test_console_demote_rejects_owner_target():
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    result = execute_console_command("user demote alice OWNER", user_manager=manager)

    assert not result.ok
    assert manager.updated == []


def test_console_command_loop_uses_provided_user_manager_instance():
    from io import StringIO

    from trpg_server.console_commands import start_console_command_loop

    manager = FakeUserManager()
    stream = StringIO("user promote bob OWNER\n")

    thread = start_console_command_loop(stdin=stream, user_manager=manager)
    thread.join(timeout=2)

    # 命令行循环必须写入传入的 manager 实例，而不是模块级默认实例。
    assert manager.updated == [(2, "OWNER")]
    assert not thread.is_alive()


def test_auth_status_refreshes_session_role_after_console_promotion():
    from trpg_server.app_factory import create_app

    app = create_app({"TESTING": True, "USER_MANAGER": FakeSessionUserManager()})
    client = app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = 1
        session["username"] = "alice"
        session["role"] = "USER"
        session["session_token"] = "token"

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    with client.session_transaction() as session:
        assert session["role"] == "ADMIN"


class FakeRoleUserManager:
    def __init__(self):
        self.users = {
            1: {"id": 1, "username": "admin", "email": "admin@example.com", "role": "ADMIN"},
            2: {"id": 2, "username": "member", "email": "member@example.com", "role": "USER"},
            3: {"id": 3, "username": "root", "email": "root@example.com", "role": "OWNER"},
        }
        self.updated = []

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    def update_user_role(self, user_id, role):
        self.updated.append((user_id, role))
        return True, "Role updated successfully"


def _role_client(manager):
    from trpg_server.app_factory import create_app

    app = create_app({"TESTING": True, "USER_MANAGER": manager})
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            {
                "user_id": 1,
                "username": "admin",
                "role": "ADMIN",
                "session_token": "token",
                "csrf_token": "csrf",
            }
        )
    return client


def test_role_route_allows_admin_to_upgrade_and_downgrade_in_scope():
    manager = FakeRoleUserManager()
    client = _role_client(manager)
    headers = {"X-CSRF-Token": "csrf"}

    upgraded = client.put("/api/users/2/role", json={"role": "ADMIN"}, headers=headers)
    downgraded = client.put("/api/users/2/role", json={"role": "USER"}, headers=headers)

    assert upgraded.status_code == 200
    assert downgraded.status_code == 200
    assert manager.updated == [(2, "ADMIN"), (2, "USER")]


def test_role_route_blocks_admin_from_privilege_escalation():
    manager = FakeRoleUserManager()
    client = _role_client(manager)
    headers = {"X-CSRF-Token": "csrf"}

    # 普通管理员不能把用户提升为 OWNER，也不能修改权限更高的 OWNER 账号。
    grant_owner = client.put("/api/users/2/role", json={"role": "OWNER"}, headers=headers)
    touch_owner = client.put("/api/users/3/role", json={"role": "USER"}, headers=headers)

    assert grant_owner.status_code == 403
    assert touch_owner.status_code == 403
    assert manager.updated == []


def test_console_promote_is_emitted_through_logging(caplog):
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()
    with caplog.at_level("INFO", logger="trpg_server.console_commands"):
        result = execute_console_command("user promote alice ADMIN", user_manager=manager)

    assert result.ok
    assert "alice" in caplog.text
    assert "ADMIN" in caplog.text


def test_compact_formatter_uses_thread_name_in_log_format():
    import logging

    from trpg_server.logging_config import CompactFormatter

    record = logging.LogRecord(
        name="trpg_server.console_commands",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="promoted",
        args=(),
        exc_info=None,
    )
    record.threadName = "trpg-console-commands"

    rendered = CompactFormatter("[%(asctime)s][%(levelname)s][%(threadName)s] %(message)s").format(record)

    assert "[INFO][trpg-console-commands] promoted" in rendered


def test_redact_sensitive_covers_api_key_variants_recursively():
    from trpg_server.logging_config import redact_sensitive

    payload = redact_sensitive(
        {
            "apiKey": "top-secret",
            "providerApiKey": "also-secret",
            "headers": {"Authorization": "Bearer secret"},
            "nested": [{"clientSecret": "client-secret"}],
            "message": "safe",
        }
    )

    assert payload["apiKey"] == "***"
    assert payload["providerApiKey"] == "***"
    assert payload["headers"]["Authorization"] == "***"
    assert payload["nested"][0]["clientSecret"] == "***"
    assert payload["message"] == "safe"
