"""Socket 房间订阅清理：被移除/退出的成员不能继续收房间广播。"""

from __future__ import annotations

from flask import Flask

from trpg_server.security import USER_ROOM_MEMBERSHIP_KEY
from trpg_server.socket_events import evict_user_from_room, forget_user_room


class _FakeServer:
    def __init__(self) -> None:
        self.left: list[tuple[str, str]] = []

    def leave_room(self, sid, room, namespace="/"):
        self.left.append((sid, room))


class _FakeSocketIO:
    def __init__(self) -> None:
        self.server = _FakeServer()
        self.events: list[tuple[str, dict, str | None]] = []

    def emit(self, event, payload, room=None, **_kwargs):
        self.events.append((event, payload, room))


def _app():
    app = Flask(__name__)
    app.config[USER_ROOM_MEMBERSHIP_KEY] = {
        "u2": {"sid-1": {"room1", "room2"}, "sid-2": {"room1"}},
        "u3": {"sid-3": {"room1"}},
    }
    socketio = _FakeSocketIO()
    app.extensions["socketio"] = socketio
    return app, socketio


def test_evict_user_leaves_room_and_cleans_membership():
    app, socketio = _app()
    with app.app_context():
        moved = evict_user_from_room("room1", "u2")

    assert moved == 2
    assert set(socketio.server.left) == {("sid-1", "room1"), ("sid-2", "room1")}
    assert any(event == "room_evicted" and room == "user:u2" for event, _payload, room in socketio.events)
    membership = app.config[USER_ROOM_MEMBERSHIP_KEY]
    assert membership["u2"] == {"sid-1": {"room2"}}
    # 其他成员的订阅不受影响。
    assert membership["u3"] == {"sid-3": {"room1"}}


def test_forget_user_room_does_not_touch_other_rooms():
    app, _socketio = _app()
    with app.app_context():
        sids = forget_user_room("room1", "u2")
        assert sids == {"sid-1", "sid-2"}
        # 再次清理是幂等的。
        assert forget_user_room("room1", "u2") == set()
