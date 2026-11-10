"""规则集可选能力的访问层。

核心不假设插件一定提供 ``schema`` / ``catalogs`` / ``checks``：这里统一做
"能力存在就调用、不存在就用中性值"的降级，避免删除某个插件或换成只有元数据的
占位插件时出现 ``AttributeError``。

降级原则：

- 角色卡：无法打包/派生时按原样返回，读路径不丢数据、写路径不破坏结构。
- 目录：无法提供时返回空列表，调用方自行展示"该规则集暂无目录"。
- 状态槽位：插件可声明自己的资源键；未声明时使用核心默认槽位。
"""

from __future__ import annotations

from typing import Any, Mapping

# 默认派生键：插件未声明时按历史 CoC 口径处理，保证旧卡行为不变。
DEFAULT_DERIVED_CARD_KEYS: tuple[str, ...] = (
    "maxHp",
    "maxSan",
    "initialSan",
    "maxMp",
    "mov",
    "build",
    "damageBonus",
    "armor",
)
# 默认运行时资源槽位：kind → 快照键 / 卡面镜像键 / 上限来源。
# ``mirror_aliases`` 兼容旧卡的蛇形 / 历史字段；``initial_*`` 表示该资源另有
# 「初始值」概念（如理智从 POW 起算），由插件声明，核心不判断具体规则。
DEFAULT_RESOURCE_SLOTS: tuple[dict[str, Any], ...] = (
    {
        "kind": "hp",
        "state": "current_hp",
        "mirror": "currentHp",
        "mirror_aliases": ("current_hp",),
        "cap_state": "max_hp",
        "cap_derived": "maxHp",
        "cap_min": 1,
    },
    {
        "kind": "san",
        "state": "current_san",
        "mirror": "currentSan",
        "mirror_aliases": ("current_san",),
        "cap_state": "max_san",
        "cap_derived": "maxSan",
        "cap_min": 0,
        "initial_state": "initial_san",
        "initial_mirror": "initialSan",
    },
    {
        "kind": "mp",
        "state": "current_mp",
        "mirror": "currentMp",
        "mirror_aliases": ("magicPoints",),
        "cap_state": "max_mp",
        "cap_derived": "maxMp",
        "cap_min": 0,
    },
)


def schema_of(plugin: Any) -> Any | None:
    return getattr(plugin, "schema", None)


def checks_of(plugin: Any) -> Any | None:
    return getattr(plugin, "checks", None)


def sanity_of(plugin: Any) -> Any | None:
    """插件的理智 / 疯狂能力；未声明时返回 None。"""
    return getattr(plugin, "sanity", None)


def combat_rules_of(plugin: Any) -> Any | None:
    """插件的战斗细节纯规则模块；未声明时返回 None。"""
    return getattr(plugin, "combat_rules", None)


def catalogs_of(plugin: Any) -> Any | None:
    return getattr(plugin, "catalogs", None)


def tools_of(plugin: Any) -> Any | None:
    return getattr(plugin, "tools", None)


def pack_card(plugin: Any, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """按插件提供的 schema 打包；无 schema 时原样返回。"""
    pack = getattr(schema_of(plugin), "pack", None)
    if callable(pack):
        return dict(pack(dict(card or {})) or {})
    return dict(card or {})


def derive_card(plugin: Any, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """按插件提供的 schema 派生；无 schema 时原样返回。"""
    derive = getattr(schema_of(plugin), "derive", None)
    if callable(derive):
        return dict(derive(dict(card or {})) or {})
    return dict(card or {})


def catalog_value(plugin: Any, name: str, default: Any = None) -> Any:
    """读取插件目录原始值；缺失时返回 ``default``（目录可能是列表或字典）。"""
    provider = getattr(catalogs_of(plugin), name, None)
    if not callable(provider):
        return default
    return provider()


def catalog_items(plugin: Any, name: str) -> list[Any]:
    """读取插件目录项（skills / weapons / occupations / items）；缺失时为空。"""
    items = catalog_value(plugin, name, [])
    if isinstance(items, dict):
        return []
    return list(items or [])


def derived_card_keys(plugin: Any) -> tuple[str, ...]:
    declared = getattr(schema_of(plugin), "derived_card_keys", None)
    if callable(declared):
        keys = tuple(str(key) for key in declared())
        if keys:
            return keys
    return DEFAULT_DERIVED_CARD_KEYS


def resource_slots(plugin: Any) -> tuple[dict[str, Any], ...]:
    declared = getattr(schema_of(plugin), "resource_slots", None)
    if callable(declared):
        slots = tuple(dict(slot) for slot in declared())
        if slots:
            return slots
    return DEFAULT_RESOURCE_SLOTS


def resource_slot(plugin: Any, kind: str) -> dict[str, Any] | None:
    """按资源种类取槽位声明；未声明该种类时返回 ``None``。"""
    target = str(kind or "").strip().lower()
    for slot in resource_slots(plugin):
        if str(slot.get("kind") or "").strip().lower() == target:
            return slot
    return None


def attribute_cap(derived: Mapping[str, Any] | None) -> int | None:
    """从插件派生结果读取属性上限；插件未声明时返回 ``None``。

    上限属于规则数据，核心只认 ``attributeCap`` 这个键，不写死 99 / 999。
    """
    value = derived.get("attributeCap") if isinstance(derived, Mapping) else None
    try:
        cap = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def clamp_attribute_value(derived: Mapping[str, Any] | None, value: Any) -> Any:
    """按插件上限钳制属性写入值；未声明上限时原样返回。"""
    cap = attribute_cap(derived)
    if cap is None:
        return value
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    return min(cap, number)


def clamp_attribute_change(
    derived: Mapping[str, Any] | None,
    card: Mapping[str, Any] | None,
    key: str,
    *,
    value: int | None = None,
    delta: int | None = None,
) -> tuple[int | None, int | None]:
    """把属性写入请求钳制到插件声明的上限，返回新的 ``(value, delta)``。

    未声明上限的规则集原样返回；减益不做额外限制，只拦截会把值推过上限的增益。
    """
    cap = attribute_cap(derived)
    if cap is None:
        return value, delta
    attributes = card.get("attributes") if isinstance(card, Mapping) else None
    target = str(key or "").strip().casefold()
    before = 0
    if isinstance(attributes, Mapping):
        for name, current in attributes.items():
            if str(name).strip().casefold() == target:
                before = _as_int(current, 0)
                break
    if value is not None:
        # 与 ``apply_attribute_change`` 一致：value 优先于 delta。
        return min(_as_int(value, 0), cap), delta
    if delta is not None:
        return None, max(0, min(delta, cap - before)) if delta > 0 else delta
    return value, delta


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bounded(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _card_mirror_value(card: Mapping[str, Any], slot: Mapping[str, Any]) -> Any:
    """按「主镜像键 → 历史别名」顺序读卡面值；键存在即返回（含 None）。"""
    keys = [str(slot.get("mirror") or "")]
    keys.extend(str(alias) for alias in slot.get("mirror_aliases") or ())
    for key in keys:
        if key and key in card:
            return card[key]
    return None


def resource_state_defaults(plugin: Any, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """按插件声明的资源槽位生成房间状态初始值。

    产出 ``{上限键: N, 当前键: N, 初始键: N}``；核心只使用槽位声明，
    不假设任何具体规则的资源名称或上限公式。
    """
    data = card if isinstance(card, Mapping) else {}
    result: dict[str, Any] = {}
    for slot in resource_slots(plugin):
        cap_min = _as_int(slot.get("cap_min"), 0)
        cap = _bounded(data.get(str(slot["cap_derived"])), cap_min, 999, cap_min)
        initial_key = str(slot.get("initial_state") or "")
        initial_value = None
        current_fallback = cap
        if initial_key:
            initial_value = _bounded(data.get(str(slot.get("initial_mirror") or "")), 0, cap, cap)
            current_fallback = min(cap, initial_value)
        result[str(slot["cap_state"])] = cap
        result[str(slot["state"])] = _bounded(_card_mirror_value(data, slot), 0, cap, current_fallback)
        if initial_key:
            result[initial_key] = initial_value
    return result


def clamp_resource_state(plugin: Any, card: Mapping[str, Any] | None, state: dict[str, Any]) -> dict[str, Any]:
    """按插件声明的资源槽位夹紧房间状态快照（原地修改并返回）。

    上限优先取卡面派生值，读不到时沿用快照已有上限；当前值与初始值夹在
    ``[0, 上限]`` 内。槽位未声明时使用核心默认槽位，保持旧房间行为不变。
    """
    data = card if isinstance(card, Mapping) else {}
    snapshot = state if isinstance(state, dict) else {}
    for slot in resource_slots(plugin):
        cap_key = str(slot["cap_state"])
        cap_min = _as_int(slot.get("cap_min"), 0)
        existing_cap = _bounded(snapshot.get(cap_key), cap_min, 999, cap_min)
        cap = _bounded(data.get(str(slot["cap_derived"])), cap_min, 999, existing_cap)
        snapshot[cap_key] = cap
        snapshot[str(slot["state"])] = _bounded(snapshot.get(str(slot["state"])), 0, cap, cap)
        initial_key = str(slot.get("initial_state") or "")
        if initial_key:
            fallback = _bounded(data.get(str(slot.get("initial_mirror") or "")), 0, cap, cap)
            snapshot[initial_key] = _bounded(snapshot.get(initial_key), 0, cap, fallback)
    return snapshot
