"""rooms 蓝图 URL 快照：重构期间锁死路由规则与 endpoint 名称。

拆分 ``routes/rooms.py`` 时只允许移动实现，不允许改动 URL、HTTP 方法或
endpoint 名称（前端、测试与 ``url_for`` 都依赖 ``rooms.*`` 前缀）。
"""
from trpg_server.app_factory import create_app


class FakeManager:
    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": "ADMIN"}


EXPECTED_ROOMS_ROUTES = {
    ("/api/room-archives", "rooms.list_room_archives", "GET"),
    ("/api/room-archives/<room_id>", "rooms.get_room_archive", "GET"),
    ("/api/rooms", "rooms.create_room", "POST"),
    ("/api/rooms", "rooms.list_rooms", "GET"),
    ("/api/rooms/<room_id>", "rooms.delete_room", "DELETE"),
    ("/api/rooms/<room_id>", "rooms.get_room", "GET"),
    ("/api/rooms/<room_id>/archive", "rooms.archive_room", "POST"),
    ("/api/rooms/<room_id>/archive", "rooms.get_room_archive", "GET"),
    ("/api/rooms/<room_id>/autosave", "rooms.load_room_autosave", "GET"),
    ("/api/rooms/<room_id>/autosave", "rooms.save_room_autosave", "POST"),
    ("/api/rooms/<room_id>/character-records", "rooms.create_character_record", "POST"),
    ("/api/rooms/<room_id>/character-records/<record_id>", "rooms.delete_character_record", "DELETE"),
    ("/api/rooms/<room_id>/house-rules", "rooms.get_room_house_rules", "GET"),
    ("/api/rooms/<room_id>/house-rules", "rooms.update_room_house_rules", "PUT"),
    ("/api/rooms/<room_id>/interlude", "rooms.get_room_interlude", "GET"),
    ("/api/rooms/<room_id>/interlude/end", "rooms.end_room_interlude", "POST"),
    ("/api/rooms/<room_id>/interlude/request", "rooms.request_room_interlude", "POST"),
    ("/api/rooms/<room_id>/interlude/review", "rooms.add_room_interlude_review", "POST"),
    ("/api/rooms/<room_id>/interlude/settle", "rooms.settle_room_interlude", "POST"),
    ("/api/rooms/<room_id>/interlude/start", "rooms.start_room_interlude", "POST"),
    ("/api/rooms/<room_id>/invite-token", "rooms.get_room_invite_token", "GET"),
    ("/api/rooms/<room_id>/invite-token", "rooms.rotate_room_invite_token", "POST"),
    ("/api/rooms/<room_id>/leave", "rooms.leave_room", "POST"),
    ("/api/rooms/<room_id>/members/<user_id>", "rooms.delete_room_member", "DELETE"),
    ("/api/rooms/<room_id>/members/<user_id>/character", "rooms.bind_room_member_character", "PUT"),
    ("/api/rooms/<room_id>/members/<user_id>/role", "rooms.update_room_member_role", "PUT"),
    ("/api/rooms/<room_id>/messages", "rooms.create_room_message", "POST"),
    ("/api/rooms/<room_id>/messages", "rooms.get_room_messages", "GET"),
    ("/api/rooms/<room_id>/nodes", "rooms.create_room_node", "POST"),
    ("/api/rooms/<room_id>/nodes", "rooms.list_room_nodes", "GET"),
    ("/api/rooms/<room_id>/nodes/<node_filename>", "rooms.delete_room_node", "DELETE"),
    ("/api/rooms/<room_id>/nodes/<node_filename>", "rooms.get_room_node", "GET"),
    ("/api/rooms/<room_id>/nodes/<node_filename>/restore", "rooms.restore_room_node", "POST"),
    ("/api/rooms/<room_id>/owner", "rooms.transfer_room_owner", "PUT"),
    ("/api/rooms/<room_id>/scenario-migration", "rooms.migrate_room_scenario", "POST"),
    ("/api/rooms/<room_id>/scenario-switch", "rooms.switch_room_scenario", "POST"),
    ("/api/rooms/<room_id>/spectate", "rooms.spectate_room", "GET"),
    ("/api/rooms/<room_id>/triggers", "rooms.trigger_room_scenario", "POST"),
    ("/api/rooms/<room_id>/usage", "rooms.get_room_usage", "GET"),
    ("/api/rooms/<room_id>/world-override", "rooms.get_world_override_status", "GET"),
    ("/api/rooms/archives", "rooms.list_room_archives", "GET"),
    ("/api/rooms/by-name/<path:room_name>/character-records", "rooms.create_character_record_by_room_name", "POST"),
    ("/api/rooms/join", "rooms.join_room_by_code", "POST"),
    ("/api/rooms/lookup", "rooms.lookup_room_by_code", "GET"),
    ("/api/rooms/spectate", "rooms.spectate_room_by_code", "POST"),
}


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
        }
    )


def test_rooms_url_map_matches_snapshot(tmp_path):
    app = _app(tmp_path)
    actual = set()
    for rule in app.url_map.iter_rules():
        if not str(rule.endpoint).startswith("rooms."):
            continue
        methods = sorted(
            method
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        )
        actual.add((rule.rule, rule.endpoint, ",".join(methods)))
    assert actual == EXPECTED_ROOMS_ROUTES
