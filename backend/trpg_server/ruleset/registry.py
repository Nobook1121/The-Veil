"""规则书插件注册表。

只做三件事：静态注册内置插件、校验契约版本、按房间/剧本解析出唯一规则集。
不存在从用户目录动态加载插件的入口——这是刻意的信任边界。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from trpg_server.ruleset.contracts import (
    DEFAULT_RULESET_ID,
    RULESET_API_VERSION,
    RulesetPlugin,
)

logger = logging.getLogger(__name__)

# 插件状态：只有 available 的规则集可以被选择。
STATUS_AVAILABLE = "available"
STATUS_DEVELOPMENT = "development"
STATUS_INCOMPATIBLE = "incompatible"


class RulesetSelectionError(ValueError):
    """选择了不可用的规则集；路由层转成 400 并原样返回原因。"""

    def __init__(self, ruleset_id: str, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.ruleset_id = ruleset_id
        self.message = message
        self.reason_code = reason_code


@dataclass(frozen=True)
class DeclaredRuleset:
    """尚未实现、但需要在界面上占位展示的规则集（如「开发中」）。"""

    id: str
    display_name: str
    state: str = STATUS_DEVELOPMENT
    message: str = "该规则集仍在开发中，暂不可选"
    reason_code: str = "ruleset_in_development"


class RulesetRegistry:
    """不可变注册表；构造时完成兼容性校验。"""

    def __init__(
        self,
        plugins: Iterable[RulesetPlugin] | None = None,
        declared: Iterable[DeclaredRuleset] | None = None,
    ) -> None:
        self._plugins: dict[str, RulesetPlugin] = {}
        self._incompatible: dict[str, str] = {}
        self._declared: dict[str, DeclaredRuleset] = {}
        for entry in declared or ():
            self._declared[entry.id] = entry
        for plugin in plugins or ():
            ruleset_id = str(getattr(plugin, "id", "") or "").strip().lower()
            if not ruleset_id:
                logger.warning("Ignoring ruleset plugin without id: %r", plugin)
                continue
            if ruleset_id in self._plugins:
                logger.warning("Ignoring duplicate ruleset plugin: %s", ruleset_id)
                continue
            api_version = int(getattr(plugin, "api_version", 0) or 0)
            if api_version != RULESET_API_VERSION:
                reason = f"api_version {api_version} != {RULESET_API_VERSION}"
                self._incompatible[ruleset_id] = reason
                logger.warning("Ruleset %s disabled: %s", ruleset_id, reason)
                continue
            self._plugins[ruleset_id] = plugin

    # -- 查询 -------------------------------------------------------------
    def ids(self) -> list[str]:
        return list(self._plugins)

    def get(self, ruleset_id: str | None = None) -> RulesetPlugin | None:
        key = str(ruleset_id or "").strip().lower()
        return self._plugins.get(key)

    def resolve(self, ruleset_id: str | None = None) -> RulesetPlugin:
        """解析规则集；未知或不兼容时回退默认，保证旧房间/旧卡仍可打开。"""
        plugin = self.get(ruleset_id)
        if plugin is not None:
            return plugin
        if ruleset_id:
            logger.info("Unknown ruleset %r, falling back to %s", ruleset_id, DEFAULT_RULESET_ID)
        fallback = self.get(DEFAULT_RULESET_ID)
        if fallback is None:
            raise RuntimeError("No built-in ruleset registered")
        return fallback

    def describe(self) -> list[dict[str, Any]]:
        """展示用清单：前端据此渲染可选/灰色卡片，无需硬编码规则集 id。"""
        items: list[dict[str, Any]] = []
        for ruleset_id, plugin in self._plugins.items():
            items.append(
                {
                    "id": ruleset_id,
                    "display_name": plugin.display_name,
                    "api_version": plugin.api_version,
                    "ruleset_version": plugin.ruleset_version,
                    "schema_version": plugin.schema_version,
                    "supported_locales": list(plugin.supported_locales),
                    "capabilities": sorted(plugin.capabilities),
                    "state": STATUS_AVAILABLE,
                    "selectable": True,
                    "enabled": True,
                    "reason": "",
                    "reason_code": "",
                }
            )
        for ruleset_id, reason in self._incompatible.items():
            items.append(
                {
                    "id": ruleset_id,
                    "display_name": ruleset_id,
                    "api_version": None,
                    "ruleset_version": "",
                    "schema_version": 0,
                    "supported_locales": [],
                    "capabilities": [],
                    "state": STATUS_INCOMPATIBLE,
                    "selectable": False,
                    "enabled": False,
                    "reason": reason,
                    "reason_code": "ruleset_incompatible",
                }
            )
        for entry in self._declared.values():
            if entry.id in self._plugins or entry.id in self._incompatible:
                continue
            items.append(
                {
                    "id": entry.id,
                    "display_name": entry.display_name,
                    "api_version": None,
                    "ruleset_version": "",
                    "schema_version": 0,
                    "supported_locales": [],
                    "capabilities": [],
                    "state": entry.state,
                    "selectable": False,
                    "enabled": False,
                    "reason": entry.message,
                    "reason_code": entry.reason_code,
                }
            )
        return items

    def assert_selectable(self, ruleset_id: str | None) -> RulesetPlugin:
        """选择入口的硬校验：不可选/未实现/不兼容都抛错，并给出可读原因。"""
        key = str(ruleset_id or "").strip().lower() or DEFAULT_RULESET_ID
        plugin = self._plugins.get(key)
        if plugin is not None:
            return plugin
        declared = self._declared.get(key)
        if declared is not None:
            raise RulesetSelectionError(key, declared.message, declared.reason_code)
        if key in self._incompatible:
            raise RulesetSelectionError(
                key,
                f"规则集 {key} 与当前服务端不兼容（{self._incompatible[key]}）",
                "ruleset_incompatible",
            )
        raise RulesetSelectionError(key, f"未知的规则集：{key}", "ruleset_unknown")

    # -- 场景 / 房间解析 ---------------------------------------------------
    def resolve_for_scenario(self, scenario: Mapping[str, Any] | None) -> RulesetPlugin:
        data = scenario if isinstance(scenario, Mapping) else {}
        return self.resolve(str(data.get("ruleset_id") or data.get("rulesetId") or ""))

    def resolve_for_room(self, room_info: Mapping[str, Any] | None) -> RulesetPlugin:
        """房间级唯一规则集。

        优先级：房间显式字段 > 场景快照里的 ruleset_id > 默认规则集。
        其它并行功能（房间模式、世界意志）不得让规则解析依赖剧本正文。
        """
        info = room_info if isinstance(room_info, Mapping) else {}
        explicit = info.get("ruleset_id") or info.get("rulesetId")
        if explicit:
            return self.resolve(str(explicit))
        scenario = info.get("scenario")
        if isinstance(scenario, Mapping) and (scenario.get("ruleset_id") or scenario.get("rulesetId")):
            return self.resolve(str(scenario.get("ruleset_id") or scenario.get("rulesetId")))
        return self.resolve("")


_BUILTIN_REGISTRY: RulesetRegistry | None = None


def builtin_registry() -> RulesetRegistry:
    """进程级内置注册表；插件清单在这里静态列出。"""
    global _BUILTIN_REGISTRY
    if _BUILTIN_REGISTRY is None:
        from trpg_server.ruleset.coc7 import Coc7Plugin

        _BUILTIN_REGISTRY = RulesetRegistry(
            [Coc7Plugin()],
            # 仅作「开发中」占位：不可选、不注册为插件、不提供任何规则能力。
            declared=[DeclaredRuleset(id="dnd5e", display_name="DnD 5e")],
        )
    return _BUILTIN_REGISTRY


def get_plugin(ruleset_id: str | None = None) -> RulesetPlugin:
    return builtin_registry().resolve(ruleset_id)
