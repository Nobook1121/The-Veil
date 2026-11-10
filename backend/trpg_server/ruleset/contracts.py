"""规则书插件的公共契约。

边界（硬约定）：

- **只允许随包内置插件**：插件由 :mod:`trpg_server.ruleset.bootstrap` 按模块登记，
  不从 ``data/`` 或用户可写目录加载任意本地 Python。
- 每个插件声明 ``api_version``；与 :data:`RULESET_API_VERSION` 不一致即禁用。
- 核心只依赖本文件的协议与数据结构，不 import 具体规则包。
- 「未指定规则集时用哪个」由插件的加载顺序/配置决定（见
  :func:`trpg_server.ruleset.registry.default_ruleset_id`），本文件不再写死 id。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

# 契约版本：插件接口发生不兼容变更时递增。
RULESET_API_VERSION = 1


@dataclass(frozen=True)
class CheckRequest:
    """一次判定的输入，字段刻意保持规则中立。"""

    kind: str = "skill"  # skill / attribute / resource / opposed / custom
    name: str = ""
    target: int | None = None
    difficulty: str = "regular"
    modifier: int = 0
    bonus: int = 0
    penalty: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResolution:
    """一次判定的可序列化结果；``state_patch`` 由核心负责写回角色卡。"""

    roll: int
    target: int
    level: str
    success: bool
    detail: dict[str, Any] = field(default_factory=dict)
    state_patch: dict[str, Any] = field(default_factory=dict)


class CharacterSchemaProvider(Protocol):
    """插件提供的角色卡 schema 与旧卡打包/校验能力。

    ``pack`` 只负责把已存储的卡整理成「公共信封 + ``sheet``」两段式结构，
    不负责落盘；写盘仍由 :mod:`trpg_server.character_service` 统一完成。
    """

    envelope_fields: tuple[str, ...]
    sheet_fields: tuple[str, ...]

    def pack(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        """返回带 ``rulesetId/rulesetVersion/rulesetSchemaVersion/sheet`` 的副本。"""

    def validate(self, sheet: Mapping[str, Any]) -> list[str]:
        """返回问题描述；空列表表示通过。核心只记录告警，不强阻断旧卡保存。"""

    def derive(self, sheet: Mapping[str, Any]) -> dict[str, Any]:
        """按规则重算派生值；无法重算时原样返回。"""


class CatalogProvider(Protocol):
    """技能 / 武器 / 物品 / 职业目录，返回结构与现有前端渲染保持一致。"""

    def skills(self) -> dict[str, Any]: ...

    def weapons(self) -> list[dict[str, Any]]: ...

    def occupations(self) -> list[dict[str, Any]]: ...

    def items(self) -> list[dict[str, Any]]: ...


class CheckProvider(Protocol):
    """判定的纯规则部分；调度、动画、日志由核心负责。"""

    def list_check_names(self, card: Mapping[str, Any]) -> list[str]: ...

    def resolve_target(self, card: Mapping[str, Any], request: CheckRequest) -> int: ...

    def roll(self, card: Mapping[str, Any], request: CheckRequest, rng: Any) -> CheckResolution: ...

    def success_level(self, roll: int, target: int) -> str: ...


class ToolProvider(Protocol):
    """插件声明的 AI 工具名与提示词片段。

    工具必须以插件 ``id`` 作为命名空间首段（如 ``coc7.roll_sanity_check``）；
    实现本身仍注册在核心工具表里，由核心守卫后下发。
    """

    def tool_names(self) -> tuple[str, ...]: ...

    def prompt_fragments(self) -> list[str]: ...


@runtime_checkable
class RulesetPlugin(Protocol):
    """规则书插件必须提供的元数据与能力。"""

    id: str
    display_name: str
    # 展示用简介；可缺省。缺省时前端回退到中性通用文案，不回退成具体规则集名称。
    description: str
    api_version: int
    ruleset_version: str
    schema_version: int
    supported_locales: tuple[str, ...]
    capabilities: frozenset[str]

    schema: CharacterSchemaProvider
    catalogs: CatalogProvider
    checks: CheckProvider
    # 可选能力：复用 combat/contracts.py 的 CombatRuleset，不另造协议。
    combat: Any | None
    tools: ToolProvider | None
