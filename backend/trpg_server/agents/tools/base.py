from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 各厂商对 function name 的校验规则统一为 ^[a-zA-Z0-9_-]+$（如 DeepSeek、OpenAI）；
# 内部工具 id 用点号做命名空间（如 room.get_room_snapshot），对外发送前由
# to_api_tool_name 规整化，解析模型调用时再映射回内部 id。


class ToolExecutionError(ValueError):
    pass


def to_api_tool_name(name: str) -> str:
    """把内部工具名转换成厂商 API 可接受的形式。

    OpenAI 兼容接口要求 ``function.name`` 匹配 ``^[a-zA-Z0-9_-]+$``，而内部工具名用
    ``.`` 作为命名空间分隔符（如 ``room.get_room_snapshot``），直接下发给 DeepSeek 等
    严格校验的平台会返回 HTTP 400。这里统一把 ``.`` 换成 ``_``；运行时再用
    ``runtime._resolve_tool`` 把首段下划线还原回命名空间。
    """
    return name.replace(".", "_")


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
                "name": to_api_tool_name(self.name),
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
