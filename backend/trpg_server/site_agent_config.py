"""网站 Agent 的配置读取与默认值。

配置集中在 ``data/config/general.toml`` 的 ``[site_agent]`` 段，统一走分层加载
（模板/env/CLI 生效，schema 保证键齐全且收敛到合法范围）。该模块只做取值，
不读写会话数据；缺省配置为「开启」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from trpg_server.config import load_settings
from trpg_server.settings import CONFIG_DIR


@dataclass(frozen=True)
class SiteAgentConfig:
    enabled: bool = True
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


def load_site_agent_config(config_dir: Path | None = None) -> SiteAgentConfig:
    section = load_settings(Path(config_dir or CONFIG_DIR)).data.get("site_agent", {})
    defaults = SiteAgentConfig()
    values = {name: section.get(name, getattr(defaults, name)) for name in SiteAgentConfig.__dataclass_fields__}
    return SiteAgentConfig(**values)
