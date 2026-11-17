"""房规命名空间编解码（core + by_ruleset）。

磁盘结构（``info.json``）::

    "house_rules": {
      "schema_version": 2,
      "core": {...},                       # BGM / 骰子 / 沉浸 / 叙事 / 世界意志
      "by_ruleset": {"coc7": {...}}        # 各规则集自己的房规
    }

兼容策略：

- 旧扁平 ``house_rules`` 读入时仍按扁平使用；下一次写入自动迁移。
- 旧扁平键按当前规则集 schema 拆分；schema 不认识的键放进 ``_legacy``，
  不丢失，也不冒充新规则集的字段。
- 切换规则集时，旧值快照到旧命名空间；新命名空间只接受新 schema 认识的键。
"""

from __future__ import annotations

from typing import Any, Mapping

HOUSE_RULES_SCHEMA_VERSION = 2
CORE_SCOPE = "core"
BY_RULESET_SCOPE = "by_ruleset"
LEGACY_KEY = "_legacy"


def core_definitions() -> dict[str, dict[str, Any]]:
    from trpg_server.room_ops.constants import CORE_HOUSE_RULE_DEFINITIONS

    return CORE_HOUSE_RULE_DEFINITIONS


def ruleset_definitions(ruleset_id: str) -> dict[str, dict[str, Any]]:
    from trpg_server.ruleset import builtin_registry
    from trpg_server.ruleset.capabilities import house_rules_definitions_of

    key = str(ruleset_id or "").strip().lower()
    if not key:
        return {}
    # 未知/禁用规则集必须显式失败，不能静默返回空定义让房间按默认规则跑。
    plugin = builtin_registry().resolve(key)
    return house_rules_definitions_of(plugin)


def definitions_for(ruleset_id: str) -> dict[str, dict[str, Any]]:
    merged = {key: dict(spec) for key, spec in core_definitions().items()}
    merged.update(ruleset_definitions(ruleset_id))
    return merged


def default_flat(ruleset_id: str) -> dict[str, Any]:
    return {key: spec.get("default") for key, spec in definitions_for(ruleset_id).items()}


def active_ruleset_id(info: Mapping[str, Any] | None) -> str:
    """房规所属规则集；显式/场景快照不可用时显式失败，不回退 coc7。"""
    from trpg_server.ruleset import RulesetSelectionError, builtin_registry

    data = info if isinstance(info, Mapping) else {}
    explicit = str(data.get("ruleset_id") or "").strip()
    scenario = data.get("scenario")
    scenario_id = ""
    if isinstance(scenario, Mapping):
        scenario_id = str(scenario.get("ruleset_id") or "").strip()
    if explicit or scenario_id:
        try:
            return builtin_registry().resolve_for_room(data).id
        except RulesetSelectionError as exc:
            raise RulesetSelectionError(
                exc.ruleset_id,
                f"房间使用的规则集 {exc.ruleset_id} 未启用或不存在（{exc.message}）",
                "ruleset_unavailable_for_room",
            ) from exc
    try:
        return str(builtin_registry().default_id() or "").strip().lower()
    except Exception:  # noqa: BLE001 - 规则集不可用时按“无规则集”处理
        return ""


def is_namespaced(stored: Any) -> bool:
    return (
        isinstance(stored, Mapping)
        and isinstance(stored.get(CORE_SCOPE), Mapping)
        and isinstance(stored.get(BY_RULESET_SCOPE), Mapping)
    )


def flatten(stored: Any, ruleset_id: str) -> dict[str, Any]:
    """把存储结构压成当前规则集的扁平视图；旧扁平结构原样返回。"""
    if not isinstance(stored, Mapping):
        return {}
    if not is_namespaced(stored):
        return dict(stored)
    result = dict(stored.get(CORE_SCOPE) or {})
    scoped = (stored.get(BY_RULESET_SCOPE) or {}).get(str(ruleset_id or "").strip().lower())
    if isinstance(scoped, Mapping):
        result.update({key: value for key, value in scoped.items() if key != LEGACY_KEY})
    return result


def flat_rules(info: Mapping[str, Any] | None) -> dict[str, Any]:
    """读取扁平房规；兼容未经过 ``attach_view`` 的原始 ``info``。"""
    data = info if isinstance(info, Mapping) else {}
    raw = data.get("house_rules")
    if is_namespaced(raw):
        return flatten(raw, active_ruleset_id(data))
    return dict(raw) if isinstance(raw, Mapping) else {}


def attach_view(info: Any) -> Any:
    """在房间 info 上挂扁平兼容视图与原始命名空间；可重复调用。"""
    if not isinstance(info, dict) or "_house_rules_ns" in info:
        return info
    raw = info.get("house_rules")
    if is_namespaced(raw):
        info["_house_rules_ns"] = dict(raw)
        info["house_rules"] = flatten(raw, active_ruleset_id(info))
    elif isinstance(raw, Mapping):
        info["house_rules"] = dict(raw)
        if raw:
            # 保留迁移前的原始扁平值，写入时据此把 schema 不认识的键放进 _legacy。
            info["_house_rules_legacy"] = dict(raw)
    return info


def _split_flat(flat: Mapping[str, Any], ruleset_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    core_keys = set(core_definitions())
    ruleset_keys = set(ruleset_definitions(ruleset_id))
    core_values: dict[str, Any] = {}
    scoped_values: dict[str, Any] = {}
    legacy: dict[str, Any] = {}
    for key, value in flat.items():
        if key in core_keys:
            core_values[key] = value
        elif key in ruleset_keys:
            scoped_values[key] = value
        else:
            legacy[key] = value
    return core_values, scoped_values, legacy


def build_namespaced(
    flat: Mapping[str, Any],
    ruleset_id: str,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把扁平值写回命名空间；保留其它规则集已有的命名空间。"""
    key = str(ruleset_id or "").strip().lower()
    base_ns = base if is_namespaced(base) else {}
    namespaces = {
        str(name): dict(values)
        for name, values in (base_ns.get(BY_RULESET_SCOPE) or {}).items()
        if isinstance(values, Mapping)
    }
    core_values, scoped_values, legacy = _split_flat(flat, key)

    core = dict(base_ns.get(CORE_SCOPE) or {})
    core.update(core_values)

    current = dict(namespaces.get(key) or {})
    current_legacy = current.pop(LEGACY_KEY, None)
    # 只保留 schema 认识的旧值；未知键进入 _legacy，避免跨 schema 串味。
    known = set(ruleset_definitions(key))
    for name in list(current):
        if name not in known:
            legacy.setdefault(name, current.pop(name))
    current.update(scoped_values)
    if not isinstance(current_legacy, Mapping):
        current_legacy = {}
    merged_legacy = dict(current_legacy)
    merged_legacy.update(legacy)
    if merged_legacy:
        current[LEGACY_KEY] = merged_legacy
    namespaces[key] = current

    return {
        "schema_version": HOUSE_RULES_SCHEMA_VERSION,
        CORE_SCOPE: core,
        BY_RULESET_SCOPE: namespaces,
    }


def namespaced_for_storage(info: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """生成要落盘的命名空间；归档后无房规时返回 ``None``。"""
    data = info if isinstance(info, Mapping) else {}
    raw = data.get("house_rules")
    if data.get("archived") and not isinstance(raw, Mapping):
        return None
    ruleset_id = active_ruleset_id(data)
    if is_namespaced(raw):
        base = raw
        flat = flatten(raw, ruleset_id)
        # 兼容直接写方：它们读到命名空间后可能把扁平键加在顶层，
        # 这里把这些覆盖合并回扁平视图，避免写入时丢值。
        flat.update(
            {
                key: value
                for key, value in raw.items()
                if key not in {"schema_version", CORE_SCOPE, BY_RULESET_SCOPE}
            }
        )
    else:
        base = data.get("_house_rules_ns")
        flat = dict(raw) if isinstance(raw, Mapping) else {}
        # 旧扁平结构里有、但当前 schema 不认识的键也要保留进 _legacy。
        legacy_raw = data.get("_house_rules_legacy")
        if isinstance(legacy_raw, Mapping):
            for key, value in legacy_raw.items():
                flat.setdefault(key, value)
    if not flat:
        flat = default_flat(ruleset_id)
    return build_namespaced(flat, ruleset_id, base)


def switch_namespaces(info: dict[str, Any], new_ruleset_id: str) -> tuple[str, str]:
    """切换规则集：快照旧值、初始化新默认、重新校验新命名空间。

    返回 ``(old_ruleset_id, new_ruleset_id)``。调用方负责暂停不兼容绑定与广播。
    """
    new_id = str(new_ruleset_id or "").strip().lower()
    old_id = active_ruleset_id(info)
    if not new_id or new_id == old_id:
        return old_id, old_id

    attach_view(info)
    raw = info.get("house_rules")
    base = info.get("_house_rules_ns") if isinstance(info.get("_house_rules_ns"), Mapping) else {}
    old_flat = flatten(raw, old_id) if is_namespaced(raw) else dict(raw or {})
    snapped = build_namespaced(old_flat, old_id, base)

    new_core = core_definitions()
    new_defs = ruleset_definitions(new_id)
    new_flat = {key: spec.get("default") for key, spec in {**new_core, **new_defs}.items()}
    # 房间级 core 设置跨规则集保留。
    for key in new_core:
        if key in old_flat:
            new_flat[key] = old_flat[key]

    # 回到曾用过的规则集时，只恢复新 schema 认识的值；其余进 _legacy。
    previous = (snapped.get(BY_RULESET_SCOPE) or {}).get(new_id)
    extra_legacy: dict[str, Any] = {}
    if isinstance(previous, Mapping):
        for key, value in previous.items():
            if key == LEGACY_KEY:
                continue
            if key in new_defs or key in new_core:
                new_flat[key] = value
            else:
                extra_legacy[key] = value
        legacy = previous.get(LEGACY_KEY)
        if isinstance(legacy, Mapping):
            extra_legacy = {**legacy, **extra_legacy}

    scoped = {key: value for key, value in new_flat.items() if key in new_defs}
    legacy_values = {
        key: value for key, value in new_flat.items() if key not in new_core and key not in new_defs
    }
    legacy_values.update(extra_legacy)
    if legacy_values:
        scoped[LEGACY_KEY] = legacy_values
    namespaces = {
        str(name): dict(values)
        for name, values in (snapped.get(BY_RULESET_SCOPE) or {}).items()
        if isinstance(values, Mapping)
    }
    namespaces[new_id] = scoped
    namespaced = {
        "schema_version": HOUSE_RULES_SCHEMA_VERSION,
        CORE_SCOPE: {key: value for key, value in new_flat.items() if key in new_core},
        BY_RULESET_SCOPE: namespaces,
    }

    info["house_rules"] = new_flat
    info["_house_rules_ns"] = namespaced
    info["ruleset_id"] = new_id
    return old_id, new_id


__all__ = [
    "BY_RULESET_SCOPE",
    "CORE_SCOPE",
    "HOUSE_RULES_SCHEMA_VERSION",
    "LEGACY_KEY",
    "active_ruleset_id",
    "attach_view",
    "build_namespaced",
    "core_definitions",
    "default_flat",
    "definitions_for",
    "flat_rules",
    "flatten",
    "is_namespaced",
    "namespaced_for_storage",
    "ruleset_definitions",
    "switch_namespaces",
]
