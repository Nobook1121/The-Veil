from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json
from trpg_server.security import safe_join
from trpg_server.agents.room_state import load_room_state


@dataclass(frozen=True)
class AgentRequestContext:
    room_id: str | None = None
    room_dir: Path | None = None
    scenarios_dir: Path | None = None
    config_dir: Path | None = None
    user_id: str | int | None = None
    agent_id: str = "kp"
    request_content: str = ""
    tool_state: dict[str, Any] = field(default_factory=dict)

    def room_info(self) -> dict[str, Any]:
        if not self.room_dir:
            return {}
        from trpg_server.house_rules_store import attach_view

        info = read_json(self.room_dir / "info.json", default={})
        return attach_view(info) if isinstance(info, dict) else {}

    def room_messages(self) -> list[dict[str, Any]]:
        if not self.room_dir:
            return []
        return read_json(self.room_dir / "messages.json", default=[])

    def room_state(self) -> dict[str, Any]:
        if not self.room_dir:
            return {}
        return load_room_state(self.room_dir)


def build_agent_context(
    room_id,
    rooms_dir,
    scenarios_dir,
    user_id=None,
    agent_id="kp",
    request_content="",
    config_dir=None,
) -> AgentRequestContext:
    room_dir = safe_join(rooms_dir, room_id) if room_id else None
    return AgentRequestContext(
        room_id=str(room_id) if room_id else None,
        room_dir=room_dir,
        scenarios_dir=scenarios_dir,
        config_dir=Path(config_dir) if config_dir else None,
        user_id=user_id,
        agent_id=agent_id,
        request_content=str(request_content or ""),
    )
