"""网站 Agent 工具规格与参数校验。

工具规格是能力白名单：模型只能看到并调用这里登记的条目。所有参数在进入业务逻辑
之前都会按 JSON Schema 做一次白名单校验，身份字段（user_id/role/permission）一律丢弃。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from trpg_server.agents.tools.base import AgentTool

Risk = Literal["read", "write", "danger"]
Scope = Literal["site", "room"]

# 模型即使伪造这些字段也不会被读取：身份只来自服务端会话。
IDENTITY_ARGUMENT_KEYS = frozenset(
    {"user_id", "actor_id", "role", "permissions", "permission", "is_admin", "is_owner", "username"}
)

# 工具参数里不允许出现的键：路径、URL、任意配置键都可能被用来越界。
FORBIDDEN_ARGUMENT_KEYS = frozenset({"path", "url", "file", "sql", "command", "shell", "env", "api_key"})


class ToolArgumentError(ValueError):
    """参数不符合工具 Schema。"""


@dataclass
class ToolContext:
    actor: Any
    config: Any
    conversation_id: str
    turn_id: str
    room_id: str | None = None
    room_info: dict[str, Any] | None = None
    # 剧本弹窗注入的上下文（§8.4.1）：scenario_id / selected_node_id / base_version / tab。
    scenario_context: dict[str, Any] | None = None
    ip: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    event_callback: Callable[[str, dict[str, Any]], None] | None = None

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if callable(self.event_callback):
            self.event_callback(event, payload)

    def room_dir(self):
        from flask import current_app

        from trpg_server.settings import ROOMS_DIR

        if not self.room_id:
            return None
        base = current_app.config.get("ROOMS_DIR", ROOMS_DIR)
        from pathlib import Path

        return Path(base) / str(self.room_id)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            payload: dict[str, Any] = {"ok": True}
            if self.data is not None:
                payload["data"] = self.data
            if self.summary:
                payload["summary"] = self.summary
            return payload
        return {"error": self.error or "工具执行失败"}

    @classmethod
    def success(cls, data: Any = None, summary: str = "") -> "ToolResult":
        return cls(ok=True, data=data, summary=summary)

    @classmethod
    def failure(cls, message: str) -> "ToolResult":
        return cls(ok=False, error=message)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: Risk
    scope: Scope = "site"
    required_permission: str | None = None
    handler: Callable[[Any, dict[str, Any], ToolContext], ToolResult] | None = None
    preview: Callable[[Any, dict[str, Any], ToolContext], ToolResult] | None = None
    apply: Callable[[Any, dict[str, Any], ToolContext], ToolResult] | None = None
    check: Callable[[Any, dict[str, Any]], str | None] | None = None

    def build_agent_tool(self, wrapper: Callable[[dict[str, Any], ToolContext], dict[str, Any]]) -> AgentTool:
        return AgentTool(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            handler=wrapper,
        )


def _schema_type_matches(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def sanitize_arguments(spec: ToolSpec, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """按 Schema 白名单清洗参数；不合法时抛 :class:`ToolArgumentError`。"""
    raw = arguments if isinstance(arguments, dict) else {}
    properties: dict[str, Any] = (spec.parameters or {}).get("properties") or {}
    required = [str(item) for item in (spec.parameters or {}).get("required") or []]

    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        key_text = str(key)
        if key_text in IDENTITY_ARGUMENT_KEYS or key_text in FORBIDDEN_ARGUMENT_KEYS:
            continue
        if key_text not in properties:
            # 未声明的参数直接丢弃，避免模型塞入额外上下文影响业务逻辑。
            continue
        cleaned[key_text] = value

    for key in required:
        if cleaned.get(key) in (None, "", []):
            raise ToolArgumentError(f"缺少参数：{key}")

    for key, value in cleaned.items():
        schema = properties.get(key) or {}
        expected = str(schema.get("type") or "")
        if value is None:
            continue
        if expected and not _schema_type_matches(expected, value):
            raise ToolArgumentError(f"参数 {key} 类型不正确")
        if "enum" in schema and value not in (schema.get("enum") or []):
            raise ToolArgumentError(f"参数 {key} 取值不在允许范围内")
        if expected == "string":
            max_length = int(schema.get("maxLength") or 0)
            if max_length and len(value) > max_length:
                raise ToolArgumentError(f"参数 {key} 超过长度上限")
        if expected in {"integer", "number"}:
            if "minimum" in schema and value < schema["minimum"]:
                raise ToolArgumentError(f"参数 {key} 小于最小值")
            if "maximum" in schema and value > schema["maximum"]:
                raise ToolArgumentError(f"参数 {key} 大于最大值")
        if expected == "array":
            max_items = int(schema.get("maxItems") or 0)
            if max_items and len(value) > max_items:
                raise ToolArgumentError(f"参数 {key} 数量超过上限")
    return cleaned


def schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": additional_properties,
    }
