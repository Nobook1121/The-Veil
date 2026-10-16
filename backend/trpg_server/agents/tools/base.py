import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 各厂商对 function name 的校验规则统一为 ^[a-zA-Z0-9_-]+$（如 DeepSeek、OpenAI）。
# 内部工具 id 用点号做命名空间（如 room.get_room_snapshot），对外发送前必须规整化，
# 解析模型调用时再映射回内部 id。
_FUNCTION_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_function_name(name: str) -> str:
    """把内部工具 id 规整为符合各厂商校验规则的 function name。"""
    return _FUNCTION_NAME_PATTERN.sub("_", str(name or ""))


class ToolExecutionError(ValueError):
    pass


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], Any], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": sanitize_function_name(self.name),
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[AgentTool] | None = None):
        self._tools = {tool.name: tool for tool in tools or []}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool

    def select(self, names: list[str]) -> list[AgentTool]:
        return [self._tools[name] for name in names if name in self._tools]

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)
