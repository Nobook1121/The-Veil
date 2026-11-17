"""插件 UI 声明壳（E0 UI 契约）。

本轮只做参数位、校验与夹紧，不实现前端挂载、资产代理或沙箱运行时：

- ``workspace`` 声明主视图归属与布局参数；核心不预设 canvas / conversation。
- ``slots`` 只允许核心定义的 6 个宿主，不接受任意坐标。
- ``sandbox`` 只保留参数；``enabled`` 在 V1 固定为 ``False``（D4）。

没有插件声明 workspace 时，``default_workspace()`` 返回当前的聊天主列布局，
因此 coc7 与旧房间的行为不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

UI_DECLARATION_VERSION = 1

WORKSPACE_PRIMARIES = frozenset({"canvas", "conversation", "custom"})
SIDEBAR_POSITIONS = frozenset({"left", "right", "none"})
TOPBAR_SLOTS = frozenset({"scene", "players", "turn", "status", "settings", "layout"})
PANELS = frozenset({"chat", "character", "combat", "log", "settings"})
PANEL_DESTINATIONS = frozenset({"primary", "sidebar", "topbar", "drawer", "overlay", "inline", "none"})
UI_SLOTS = frozenset(
    {
        "room.stage.primary",
        "room.chat.aboveHistory",
        "room.rail.right",
        "room.tab",
        "character.sheet.side",
        "settings.section",
    }
)
SLOT_LAYOUTS = frozenset({"fill", "fullscreen", "inline", "drawer", "overlay"})
SANDBOX_MODES = frozenset({"iframe", "shadow", "none"})
SANDBOX_MOUNT = frozenset({"eager", "lazy", "on_demand"})
SANDBOX_UNMOUNT = frozenset(
    {"on_room_change", "on_ruleset_change", "on_route_change", "on-room-or-ruleset-change", "manual"}
)
SANDBOX_ISOLATION = frozenset({"slot", "plugin", "page"})
SANDBOX_FALLBACK = frozenset({"collapse", "placeholder", "chat_first"})
SANDBOX_NOTIFY = frozenset({"dm", "all", "none"})
SANDBOX_LAYER_STACKS = frozenset({"base", "workspace", "drawer", "overlay", "toast"})
SANDBOX_ATTRS = frozenset({"allow", "csp", "referrerpolicy"})

# 核心层级档位：插件只能选档，不能写任意 z-index。
LAYER_TIERS: dict[str, int] = {"base": 0, "workspace": 10, "drawer": 20, "overlay": 30, "toast": 40}


class UiDeclarationError(ValueError):
    """插件 UI 声明不合法；清单解析层会转成 ManifestError。"""


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _strings(value: Any, *, label: str, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise UiDeclarationError(f"{label} 必须是字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise UiDeclarationError(f"{label} 不能包含空字符串")
        key = text.lower()
        if allowed is not None and key not in allowed:
            raise UiDeclarationError(f"{label} 不支持：{text}")
        result.append(key)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class SidebarSpec:
    position: str = "right"
    width: int = 260
    min_width: int = 240
    max_width: int = 480
    collapsible: bool = False
    collapsed_width: int = 48

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "width": self.width,
            "min_width": self.min_width,
            "max_width": self.max_width,
            "collapsible": self.collapsible,
            "collapsed_width": self.collapsed_width,
        }


@dataclass(frozen=True)
class TopbarSpec:
    height: int = 0
    slots: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"height": self.height, "slots": list(self.slots)}


@dataclass(frozen=True)
class LeftRailSpec:
    width: int = 0
    visible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"width": self.width, "visible": self.visible}


@dataclass(frozen=True)
class WorkspaceSpec:
    layout_id: str = "conversation"
    rulesets: tuple[str, ...] = ()
    primary: str = "conversation"
    primary_min_width: int = 480
    sidebar: SidebarSpec = field(default_factory=SidebarSpec)
    topbar: TopbarSpec = field(default_factory=TopbarSpec)
    left_rail: LeftRailSpec = field(default_factory=LeftRailSpec)
    panels: dict[str, str] = field(default_factory=dict)
    priority: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_conversation(self) -> bool:
        return self.primary == "conversation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_id": self.layout_id,
            "rulesets": list(self.rulesets),
            "primary": self.primary,
            "primary_min_width": self.primary_min_width,
            "sidebar": self.sidebar.to_dict(),
            "topbar": self.topbar.to_dict(),
            "left_rail": self.left_rail.to_dict(),
            "panels": dict(self.panels),
            "priority": self.priority,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class UiSlot:
    id: str
    slot: str
    title_i18n: str = ""
    rulesets: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    priority: int = 0
    layout: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    mount: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slot": self.slot,
            "title_i18n": self.title_i18n,
            "rulesets": list(self.rulesets),
            "requires": list(self.requires),
            "priority": self.priority,
            "layout": dict(self.layout),
            "permissions": list(self.permissions),
            "mount": self.mount,
        }


@dataclass(frozen=True)
class UiSandbox:
    """沙箱占位参数；V1 恒为 ``enabled=False``，只校验不运行。"""

    enabled: bool = False
    mode: str = "none"
    position: str = ""
    size: dict[str, Any] = field(default_factory=dict)
    layer: dict[str, Any] = field(default_factory=dict)
    collapsible: bool = False
    collapsed_size: int | None = None
    exclusive_group: str = ""
    lifecycle: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    sandbox_attrs: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "position": self.position,
            "size": dict(self.size),
            "layer": dict(self.layer),
            "collapsible": self.collapsible,
            "collapsed_size": self.collapsed_size,
            "exclusive_group": self.exclusive_group,
            "lifecycle": dict(self.lifecycle),
            "failure": dict(self.failure),
            "permissions": list(self.permissions),
            "sandbox_attrs": dict(self.sandbox_attrs),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class UiDeclaration:
    api_version: int = UI_DECLARATION_VERSION
    workspace: WorkspaceSpec | None = None
    slots: tuple[UiSlot, ...] = ()
    sandbox: UiSandbox | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "workspace": self.workspace.to_dict() if self.workspace else None,
            "slots": [slot.to_dict() for slot in self.slots],
            "sandbox": self.sandbox.to_dict() if self.sandbox else None,
        }


def default_workspace() -> WorkspaceSpec:
    """无插件声明时的核心兜底：完全沿用当前聊天主列 + 260px 成员栏。"""
    return WorkspaceSpec(
        layout_id="conversation",
        primary="conversation",
        sidebar=SidebarSpec(position="right", width=260, min_width=240, max_width=420, collapsible=False, collapsed_width=48),
        topbar=TopbarSpec(height=0, slots=()),
        left_rail=LeftRailSpec(width=0, visible=False),
        panels={"chat": "primary", "character": "sidebar", "combat": "inline"},
    )


def _parse_workspace(raw: Any) -> WorkspaceSpec:
    if not isinstance(raw, Mapping):
        raise UiDeclarationError("ui.workspace 必须是对象")
    primary = str(raw.get("primary") or "conversation").strip().lower()
    if primary not in WORKSPACE_PRIMARIES:
        raise UiDeclarationError(f"ui.workspace.primary 不支持：{primary}")

    sidebar_raw = raw.get("sidebar") if isinstance(raw.get("sidebar"), Mapping) else {}
    position = str(sidebar_raw.get("position") or "right").strip().lower()
    if position not in SIDEBAR_POSITIONS:
        raise UiDeclarationError(f"ui.workspace.sidebar.position 不支持：{position}")
    min_width = _clamp(_as_int(sidebar_raw.get("min_width"), 240), 160, 720)
    max_width = _clamp(_as_int(sidebar_raw.get("max_width"), 480), min_width, 960)
    width = _clamp(_as_int(sidebar_raw.get("width"), 260), min_width, max_width)
    sidebar = SidebarSpec(
        position=position,
        width=0 if position == "none" else width,
        min_width=min_width,
        max_width=max_width,
        collapsible=bool(sidebar_raw.get("collapsible")),
        collapsed_width=_clamp(_as_int(sidebar_raw.get("collapsed_width"), 48), 0, 160),
    )

    topbar_raw = raw.get("topbar") if isinstance(raw.get("topbar"), Mapping) else {}
    topbar = TopbarSpec(
        height=_clamp(_as_int(topbar_raw.get("height"), 0), 0, 96),
        slots=_strings(topbar_raw.get("slots"), label="ui.workspace.topbar.slots", allowed=TOPBAR_SLOTS),
    )

    rail_raw = raw.get("left_rail") if isinstance(raw.get("left_rail"), Mapping) else {}
    left_rail = LeftRailSpec(
        width=_clamp(_as_int(rail_raw.get("width"), 0), 0, 96),
        visible=bool(rail_raw.get("visible")),
    )

    panels: dict[str, str] = {}
    raw_panels = raw.get("panels")
    if isinstance(raw_panels, Mapping):
        for key, value in raw_panels.items():
            panel = str(key or "").strip().lower()
            destination = str(value or "").strip().lower()
            if panel not in PANELS or destination not in PANEL_DESTINATIONS:
                raise UiDeclarationError(f"ui.workspace.panels 不接受：{panel}={destination}")
            panels[panel] = destination

    return WorkspaceSpec(
        layout_id=str(raw.get("layout_id") or primary),
        rulesets=_strings(raw.get("rulesets"), label="ui.workspace.rulesets"),
        primary=primary,
        primary_min_width=_clamp(_as_int(raw.get("primary_min_width"), 480), 320, 1920),
        sidebar=sidebar,
        topbar=topbar,
        left_rail=left_rail,
        panels=panels,
        priority=_as_int(raw.get("priority"), 0),
        detail=dict(raw.get("detail") or {}),
    )


def _parse_slots(raw: Any) -> tuple[UiSlot, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise UiDeclarationError("ui.slots 必须是数组")
    slots: list[UiSlot] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise UiDeclarationError("ui.slots[] 必须是对象")
        slot_id = str(item.get("id") or "").strip()
        slot = str(item.get("slot") or "").strip()
        if not slot_id or slot_id in seen:
            raise UiDeclarationError(f"ui.slots[].id 缺失或重复：{slot_id}")
        if slot not in UI_SLOTS:
            raise UiDeclarationError(f"ui.slots[].slot 不支持：{slot}")
        seen.add(slot_id)
        layout_raw = item.get("layout")
        layout = dict(layout_raw) if isinstance(layout_raw, Mapping) else {}
        layout_kind = str(layout.get("desktop") or "").strip().lower()
        if layout_kind and layout_kind not in SLOT_LAYOUTS:
            raise UiDeclarationError(f"ui.slots[].layout.desktop 不支持：{layout_kind}")
        slots.append(
            UiSlot(
                id=slot_id,
                slot=slot,
                title_i18n=str(item.get("title_i18n") or ""),
                rulesets=_strings(item.get("rulesets"), label=f"ui.slots[{slot_id}].rulesets"),
                requires=_strings(item.get("requires"), label=f"ui.slots[{slot_id}].requires"),
                priority=_as_int(item.get("priority"), 0),
                layout=layout,
                permissions=_strings(item.get("permissions"), label=f"ui.slots[{slot_id}].permissions"),
                mount=str(item.get("mount") or ""),
            )
        )
    return tuple(slots)


def _parse_sandbox(raw: Any) -> UiSandbox | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UiDeclarationError("ui.sandbox 必须是对象")
    warnings: list[str] = []
    enabled = bool(raw.get("enabled"))
    if enabled:
        warnings.append("ui.sandbox.enabled 在 V1 不受支持，已按 false 处理")
    mode = str(raw.get("mode") or "none").strip().lower()
    if mode not in SANDBOX_MODES:
        raise UiDeclarationError(f"ui.sandbox.mode 不支持：{mode}")
    position = str(raw.get("position") or "").strip()
    if position and position not in UI_SLOTS:
        raise UiDeclarationError(f"ui.sandbox.position 必须是核心 slot：{position}")

    size_raw = raw.get("size") if isinstance(raw.get("size"), Mapping) else {}
    size = {
        key: _clamp(_as_int(size_raw.get(key), 0), 0, 4096)
        for key in ("width", "height", "min_width", "min_height", "max_width", "max_height")
        if size_raw.get(key) is not None
    }
    aspect = size_raw.get("aspect")
    if isinstance(aspect, (int, float)) and aspect > 0:
        size["aspect"] = float(aspect)

    layer_raw = raw.get("layer") if isinstance(raw.get("layer"), Mapping) else {}
    stack = str(layer_raw.get("stack") or "workspace").strip().lower()
    if stack not in SANDBOX_LAYER_STACKS:
        raise UiDeclarationError(f"ui.sandbox.layer.stack 不支持：{stack}")
    layer = {"stack": stack, "z_index": LAYER_TIERS[stack]}

    lifecycle_raw = raw.get("lifecycle") if isinstance(raw.get("lifecycle"), Mapping) else {}
    mount = str(lifecycle_raw.get("mount") or "lazy").strip().lower()
    unmount = str(lifecycle_raw.get("unmount") or "on_room_change").strip().lower()
    if mount not in SANDBOX_MOUNT:
        raise UiDeclarationError(f"ui.sandbox.lifecycle.mount 不支持：{mount}")
    if unmount not in SANDBOX_UNMOUNT:
        raise UiDeclarationError(f"ui.sandbox.lifecycle.unmount 不支持：{unmount}")

    failure_raw = raw.get("failure") if isinstance(raw.get("failure"), Mapping) else {}
    isolation = str(failure_raw.get("isolation") or "slot").strip().lower()
    fallback = str(failure_raw.get("fallback") or "collapse").strip().lower()
    notify = str(failure_raw.get("notify") or "dm").strip().lower()
    if isolation not in SANDBOX_ISOLATION:
        raise UiDeclarationError(f"ui.sandbox.failure.isolation 不支持：{isolation}")
    if fallback not in SANDBOX_FALLBACK:
        raise UiDeclarationError(f"ui.sandbox.failure.fallback 不支持：{fallback}")
    if notify not in SANDBOX_NOTIFY:
        raise UiDeclarationError(f"ui.sandbox.failure.notify 不支持：{notify}")

    attrs_raw = raw.get("sandbox_attrs") if isinstance(raw.get("sandbox_attrs"), Mapping) else {}
    unknown_attrs = sorted(str(key) for key in attrs_raw if str(key) not in SANDBOX_ATTRS)
    if unknown_attrs:
        raise UiDeclarationError("ui.sandbox.sandbox_attrs 只允许 allow/csp/referrerpolicy：" + ", ".join(unknown_attrs))

    collapsed_raw = raw.get("collapsible")
    collapsible = bool(collapsed_raw) if isinstance(collapsed_raw, bool) else False
    collapsed_size = (
        _clamp(_as_int(raw.get("collapsed_size"), 0), 0, 1024)
        if raw.get("collapsed_size") is not None
        else None
    )

    return UiSandbox(
        enabled=False,
        mode=mode,
        position=position,
        size=size,
        layer=layer,
        collapsible=collapsible,
        collapsed_size=collapsed_size,
        exclusive_group=str(raw.get("exclusive_group") or ""),
        lifecycle={
            "mount": mount,
            "unmount": unmount,
            "keep_alive": bool(lifecycle_raw.get("keep_alive")),
        },
        failure={
            "isolation": isolation,
            "fallback": fallback,
            "retry": _clamp(_as_int(failure_raw.get("retry"), 1), 0, 5),
            "notify": notify,
        },
        permissions=_strings(raw.get("permissions"), label="ui.sandbox.permissions"),
        sandbox_attrs=dict(attrs_raw),
        warnings=tuple(warnings),
    )


def parse_ui(raw: Any) -> UiDeclaration | None:
    """解析 ``capabilities.ui``；未声明返回 ``None``，非法声明抛 UiDeclarationError。"""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise UiDeclarationError("capabilities.ui 必须是对象")
    api_version = _as_int(raw.get("api_version"), UI_DECLARATION_VERSION)
    if api_version != UI_DECLARATION_VERSION:
        raise UiDeclarationError(f"capabilities.ui.api_version 不支持：{api_version}")
    workspace = _parse_workspace(raw["workspace"]) if raw.get("workspace") is not None else None
    return UiDeclaration(
        api_version=api_version,
        workspace=workspace,
        slots=_parse_slots(raw.get("slots")),
        sandbox=_parse_sandbox(raw.get("sandbox")),
    )


def workspace_of(plugin: Any) -> WorkspaceSpec:
    """读取插件的 workspace 声明；缺失或非法时回退核心的 conversation 布局。"""
    for attr in ("workspace", "ui_workspace"):
        declared = getattr(plugin, attr, None)
        if isinstance(declared, WorkspaceSpec):
            return declared
        if isinstance(declared, Mapping):
            try:
                return _parse_workspace(declared)
            except UiDeclarationError:
                return default_workspace()
    ui = getattr(plugin, "ui", None)
    if isinstance(ui, UiDeclaration) and ui.workspace is not None:
        return ui.workspace
    if isinstance(ui, Mapping):
        try:
            parsed = parse_ui(ui)
        except UiDeclarationError:
            parsed = None
        if parsed is not None and parsed.workspace is not None:
            return parsed.workspace
    return default_workspace()


def select_workspace(
    plugins: Iterable[Any],
    ruleset_id: str,
) -> WorkspaceSpec:
    """按房间规则集选择 workspace；匹配优先，其次按 priority，最后回退 conversation。"""
    target = str(ruleset_id or "").strip().lower()
    candidates: list[WorkspaceSpec] = []
    for plugin in plugins:
        workspace = workspace_of(plugin)
        if workspace.rulesets and target not in workspace.rulesets:
            continue
        candidates.append(workspace)
    if not candidates:
        return default_workspace()
    candidates.sort(key=lambda item: item.priority, reverse=True)
    return candidates[0]


__all__ = [
    "LAYER_TIERS",
    "PANELS",
    "PANEL_DESTINATIONS",
    "SANDBOX_ATTRS",
    "SANDBOX_FALLBACK",
    "SANDBOX_ISOLATION",
    "SANDBOX_MOUNT",
    "SANDBOX_NOTIFY",
    "SANDBOX_UNMOUNT",
    "SIDEBAR_POSITIONS",
    "SLOT_LAYOUTS",
    "TOPBAR_SLOTS",
    "UI_DECLARATION_VERSION",
    "UI_SLOTS",
    "WORKSPACE_PRIMARIES",
    "LeftRailSpec",
    "SidebarSpec",
    "TopbarSpec",
    "UiDeclaration",
    "UiDeclarationError",
    "UiSandbox",
    "UiSlot",
    "WorkspaceSpec",
    "default_workspace",
    "parse_ui",
    "select_workspace",
    "workspace_of",
]
