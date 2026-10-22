"""网站 Agent 的配置读取与默认值。

配置集中在 ``data/config/general.toml`` 的 ``[site_agent]`` 段。该模块只做解析与
范围收敛，不读写会话数据；缺少配置时一律按「关闭」处理，避免升级后自动放开入口。
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from trpg_server.settings import CONFIG_DIR


@dataclass(frozen=True)
class SiteAgentConfig:
    enabled: bool = False
    max_tool_rounds: int = 6
    request_timeout: int = 120
    max_history_messages: int = 40
    max_tool_result_chars: int = 4000
    max_message_chars: int = 4000
    max_turns_per_hour: int = 60
    daily_token_budget: int = 200000
    conversation_retention_days: int = 30
    action_ttl_seconds: int = 300
    max_conversations_per_user: int = 30
    max_messages_per_conversation: int = 200

    def as_dict(self) -> dict[str, bool | int]:
        return asdict(self)


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() == "true"


def _as_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def load_site_agent_config(config_dir: Path | None = None) -> SiteAgentConfig:
    config_path = Path(config_dir or CONFIG_DIR) / "general.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    section = data.get("site_agent") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        section = {}

    defaults = SiteAgentConfig()
    return SiteAgentConfig(
        enabled=_as_bool(section.get("enabled"), defaults.enabled),
        max_tool_rounds=_as_int(section.get("max_tool_rounds"), defaults.max_tool_rounds, 1, 20),
        request_timeout=_as_int(section.get("request_timeout"), defaults.request_timeout, 30, 1800),
        max_history_messages=_as_int(
            section.get("max_history_messages"), defaults.max_history_messages, 4, 200
        ),
        max_tool_result_chars=_as_int(
            section.get("max_tool_result_chars"), defaults.max_tool_result_chars, 200, 20000
        ),
        max_message_chars=_as_int(section.get("max_message_chars"), defaults.max_message_chars, 100, 20000),
        max_turns_per_hour=_as_int(section.get("max_turns_per_hour"), defaults.max_turns_per_hour, 1, 1000),
        daily_token_budget=_as_int(
            section.get("daily_token_budget"), defaults.daily_token_budget, 0, 100000000
        ),
        conversation_retention_days=_as_int(
            section.get("conversation_retention_days"), defaults.conversation_retention_days, 1, 3650
        ),
        action_ttl_seconds=_as_int(
            section.get("action_ttl_seconds"), defaults.action_ttl_seconds, 30, 3600
        ),
        max_conversations_per_user=_as_int(
            section.get("max_conversations_per_user"), defaults.max_conversations_per_user, 1, 200
        ),
        max_messages_per_conversation=_as_int(
            section.get("max_messages_per_conversation"),
            defaults.max_messages_per_conversation,
            10,
            2000,
        ),
    )
