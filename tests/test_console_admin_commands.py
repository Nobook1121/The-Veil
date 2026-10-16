"""新增控制台命令：服务 / 用户 / AI / 向量 / 知识 / 房间 / 备份 / OWNER 与交互确认。"""
import json
from pathlib import Path

from trpg_server.agents.vector_store import EmbeddedVectorStore
from trpg_server.scenario_store import save_scenario_record


class FakeUserManager:
    def __init__(self):
        self.users = [
            {"id": 1, "username": "owner", "email": "o@x.io", "role": "OWNER", "status": "active", "last_login_at": None},
            {"id": 2, "username": "alice", "email": "a@x.io", "role": "USER", "status": "active", "last_login_at": None},
        ]
        self._next = 3
        self.revoked = []
        self.deleted = []

    def get_all_users(self):
        return self.users

    def get_user_by_id(self, user_id):
        return next((u for u in self.users if u["id"] == int(user_id)), None)

    def update_user_status(self, user_id, status):
        user = self.get_user_by_id(user_id)
        if not user:
            return False, "not found"
        user["status"] = status
        return True, "ok"

    def update_user_role(self, user_id, role):
        user = self.get_user_by_id(user_id)
        if not user:
            return False, "not found"
        user["role"] = role
        return True, "ok"

    def revoke_all_sessions(self, user_id):
        self.revoked.append(int(user_id))
        return 2

    def register(self, username, password, email, terms_accepted, ip_address=None):
        user = {"id": self._next, "username": username, "email": email, "role": "USER", "status": "active"}
        self._next += 1
        self.users.append(user)
        return True, "ok", user

    def delete_user(self, user_id):
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        self.users.remove(user)
        self.deleted.append(int(user_id))
        return True


class FakeEmbeddingProvider:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    def health(self):
        return {"configured": True, "loaded": True, "dimensions": 3}


def _make_scenario(scenarios_dir: Path, scenario_id: int, title: str, owner_id: int = 1) -> Path:
    record = {
        "id": scenario_id,
        "title": title,
        "owner_id": owner_id,
        "modules": [
            {"id": "s1", "module_type": "scene", "title": "Room", "summary": "s", "content": "secret room"},
        ],
    }
    return save_scenario_record(scenarios_dir, record)


def test_log_tail_reads_latest_lines(tmp_path):
    from trpg_server.console_commands import execute_console_command

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_text("line1\nline2\n", encoding="utf-8")

    result = execute_console_command("log tail 1", log_dir=logs)

    assert result.ok
    assert "line2" in result.message


def test_server_info_reports_python():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("server info")

    assert result.ok
    assert "Python" in result.message


def test_user_lifecycle_commands(tmp_path):
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    listed = execute_console_command("user list", user_manager=manager)
    assert listed.ok and "alice" in listed.message

    banned = execute_console_command("user ban alice", user_manager=manager)
    assert banned.ok and manager.get_user_by_id(2)["status"] == "banned"

    unbanned = execute_console_command("user unban 2", user_manager=manager)
    assert unbanned.ok and manager.get_user_by_id(2)["status"] == "active"

    revoked = execute_console_command("user sessions revoke alice", user_manager=manager)
    assert revoked.ok and manager.revoked == [2]


def test_user_create_command_promotes(tmp_path):
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    result = execute_console_command("user create bob ADMIN", user_manager=manager)

    assert result.ok and "临时密码" in result.message
    created = next(u for u in manager.users if u["username"] == "bob")
    assert created["role"] == "ADMIN"


def test_user_delete_preview_then_confirm(tmp_path):
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()
    scenarios_dir = tmp_path / "scenarios"

    preview = execute_console_command("user delete alice", user_manager=manager, scenarios_dir=scenarios_dir)
    assert preview.ok and "预演" in preview.message
    assert manager.get_user_by_id(2) is not None

    done = execute_console_command("user delete alice --yes", user_manager=manager, scenarios_dir=scenarios_dir)
    assert done.ok and manager.get_user_by_id(2) is None


def test_user_delete_interactive_confirm_executes(tmp_path):
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    result = execute_console_command(
        "user delete alice",
        user_manager=manager,
        scenarios_dir=tmp_path / "scenarios",
        confirm=lambda prompt: True,
    )

    assert result.ok
    assert manager.get_user_by_id(2) is None


def test_ai_platform_list_and_disable(tmp_path):
    from trpg_server.console_commands import execute_console_command

    platform_dir = tmp_path / "aiplatform"
    platform_dir.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (platform_dir / "openai.json").write_text(
        json.dumps({"enabled": True, "order": 1, "models": [{"id": "gpt-4o"}]}), encoding="utf-8"
    )

    listed = execute_console_command(
        "ai platform list", ai_platform_dir=platform_dir, ai_platform_secret_dir=secret_dir
    )
    assert listed.ok and "openai" in listed.message and "gpt-4o" in listed.message

    disabled = execute_console_command(
        "ai platform disable openai", ai_platform_dir=platform_dir, ai_platform_secret_dir=secret_dir
    )
    assert disabled.ok and "停用" in disabled.message


def test_scenario_validate_and_reindex(tmp_path):
    from trpg_server.console_commands import execute_console_command

    scenarios_dir = tmp_path / "scenarios"
    _make_scenario(scenarios_dir, 7, "灯塔之影")
    store = EmbeddedVectorStore(tmp_path / "vector-db")
    provider = FakeEmbeddingProvider()

    before = execute_console_command("scenario validate 7", scenarios_dir=scenarios_dir, vector_store=store)
    assert before.ok

    reindexed = execute_console_command(
        "scenario reindex 7 --yes",
        scenarios_dir=scenarios_dir,
        vector_store=store,
        embedding_provider=provider,
    )
    assert reindexed.ok and "知识块" in reindexed.message
    assert store.count({"scenario_id": "7"}) > 0

    after = execute_console_command("scenario validate 7", scenarios_dir=scenarios_dir, vector_store=store)
    assert after.ok and "三者一致" in after.message


def test_vector_health_and_prune(tmp_path):
    from trpg_server.console_commands import execute_console_command

    store = EmbeddedVectorStore(tmp_path / "vector-db")
    store.upsert(
        [{"chunk_id": "c1", "scenario_id": "999", "scenario_version": "1", "embedding": [0.1, 0.2]}]
    )
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()

    health = execute_console_command("vector health", vector_store=store)
    assert health.ok and "向量总数: 1" in health.message

    preview = execute_console_command("vector prune", vector_store=store, scenarios_dir=scenarios_dir)
    assert preview.ok and "999" in preview.message

    pruned = execute_console_command("vector prune --yes", vector_store=store, scenarios_dir=scenarios_dir)
    assert pruned.ok and store.count() == 0


def test_knowledge_reindex_helper_reindexes_each_ruleset():
    from trpg_server.knowledge_admin import reindex_rulesets

    class FakeRulesetStore:
        def __init__(self):
            self.reindexed = []

        def list_rulesets(self):
            return [{"ruleset_id": "coc7", "active_version": "1", "enabled": True}]

        def reindex(self, ruleset_id):
            self.reindexed.append(ruleset_id)
            return {"ruleset_id": ruleset_id, "knowledge_version": "2", "chunk_count": 4, "active_version": "2"}

    store = FakeRulesetStore()
    results = reindex_rulesets(store)
    assert store.reindexed == ["coc7"]
    assert results[0]["chunk_count"] == 4


def test_room_list_members_archive_and_prune(tmp_path):
    from trpg_server.console_commands import execute_console_command

    rooms = tmp_path / "rooms"
    room = rooms / "r1"
    room.mkdir(parents=True)
    (room / "info.json").write_text(
        json.dumps(
            {
                "id": "r1",
                "name": "灯塔",
                "room_code": "ABC123",
                "scenario_id": 7,
                "scenario_title": "灯塔之影",
                "creator_id": 1,
                "members": [{"user_id": 1, "username": "owner", "role": "OWNER"}],
                "completed_at": "2026-01-01 00:00:00",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            }
        ),
        encoding="utf-8",
    )
    stale = rooms / "r2"
    stale.mkdir()
    (stale / "info.json").write_text(
        json.dumps({"id": "r2", "name": "僵尸房", "members": [], "created_at": "2000-01-01 00:00:00"}),
        encoding="utf-8",
    )

    listed = execute_console_command("room list", rooms_dir=rooms)
    assert listed.ok and "灯塔" in listed.message

    members = execute_console_command("room members r1", rooms_dir=rooms)
    assert members.ok and "owner" in members.message

    preview = execute_console_command("room archive r1", rooms_dir=rooms)
    assert preview.ok and "预演" in preview.message

    archived = execute_console_command("room archive r1 --yes", rooms_dir=rooms)
    assert archived.ok
    assert json.loads((room / "info.json").read_text(encoding="utf-8"))["archived"] is True

    prune_preview = execute_console_command("room prune --days 1", rooms_dir=rooms)
    assert prune_preview.ok and "r2" in prune_preview.message

    pruned = execute_console_command("room prune --days 1 --yes", rooms_dir=rooms)
    assert pruned.ok and not stale.exists()


def test_backup_create_preview_and_apply(tmp_path, monkeypatch):
    from trpg_server import backup_admin
    from trpg_server.console_commands import execute_console_command

    source = tmp_path / "cfg"
    source.mkdir()
    (source / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(backup_admin, "_BACKUP_SOURCES", {"config": source})

    out_dir = (tmp_path / "backups").as_posix()

    preview = execute_console_command(f"backup create --out {out_dir}")
    assert preview.ok and "预演" in preview.message

    created = execute_console_command(f"backup create --out {out_dir} --yes")
    assert created.ok and "备份已完成" in created.message
    assert list(Path(out_dir).glob("*.zip"))


def test_network_status_command():
    from trpg_server.console_commands import execute_console_command

    result = execute_console_command("network status")

    assert result.ok and "网络状态" in result.message


def test_owner_transfer_command():
    from trpg_server.console_commands import execute_console_command

    manager = FakeUserManager()

    result = execute_console_command("owner transfer alice --yes", user_manager=manager)

    assert result.ok
    assert manager.get_user_by_id(2)["role"] == "OWNER"
    assert manager.get_user_by_id(1)["role"] == "ADMIN"


def test_destructive_command_preview_when_no_confirm_channel(tmp_path):
    from trpg_server.console_commands import execute_console_command

    store = EmbeddedVectorStore(tmp_path / "vector-db")
    store.upsert(
        [{"chunk_id": "c1", "scenario_id": "999", "scenario_version": "1", "embedding": [0.1, 0.2]}]
    )
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()

    result = execute_console_command("vector prune", vector_store=store, scenarios_dir=scenarios_dir)

    assert result.ok and "预演模式" in result.message
    assert store.count() == 1
