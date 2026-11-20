"""战斗规则注册表。

插件通过 ``combat`` 能力提供实现；本模块只做「按规则集 id 取实现」与兼容兜底，
不静态 import 任何具体规则包，因此删除某个战斗实现不会让核心导入失败。

兼容兜底只认运维通过 ``TRPG_RULESET_COMBAT_FALLBACKS`` 显式配置的模块，
核心默认不为任何具体规则集预置战斗实现。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Mapping

from trpg_server.combat.contracts import DEFAULT_LEVEL_RANK, DEFAULT_SKIPPED_STATUSES
from trpg_server.ruleset import builtin_registry

logger = logging.getLogger(__name__)


class CombatRulesetUnavailable(LookupError):
    """当前规则集没有战斗实现；调用方应提示「该规则集暂不支持战斗」。"""


_FALLBACK_CACHE: dict[str, Any] = {}


def _fallback_module_map() -> dict[str, str]:
    from trpg_server.ruleset.bootstrap import combat_module_map

    return combat_module_map()


def _fallback_ruleset(ruleset_id: str) -> Any | None:
    if ruleset_id in _FALLBACK_CACHE:
        return _FALLBACK_CACHE[ruleset_id]
    module_path = _fallback_module_map().get(ruleset_id)
    if not module_path:
        return None
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - 兼容实现缺失不应阻断核心
        logger.warning("Combat ruleset module unavailable: %s", module_path, exc_info=True)
        return None
    factory = getattr(module, "COMBAT_RULESET", None)
    if factory is None:
        logger.warning("Combat ruleset module exports no COMBAT_RULESET: %s", module_path)
        return None
    ruleset = factory() if isinstance(factory, type) else factory
    _FALLBACK_CACHE[ruleset_id] = ruleset
    return ruleset


def _plugin_combat(ruleset_id: str) -> Any | None:
    plugin = builtin_registry().get(ruleset_id)
    return getattr(plugin, "combat", None) if plugin is not None else None


def get_ruleset(ruleset_id: str | None = None):
    """按规则集 id 取战斗实现。

    显式 id 只解析该规则集自己的实现，不静默回退默认插件；只有未指定规则集时
    才使用默认插件。这与规则集注册表的显式解析口径一致（D8）。
    """
    registry = builtin_registry()
    key = str(ruleset_id or "").strip().lower()
    if key:
        combat = _plugin_combat(key)
        if combat is not None:
            return combat
        fallback = _fallback_ruleset(key)
        if fallback is not None:
            return fallback
        raise CombatRulesetUnavailable(f"规则集 {key} 没有可用的战斗实现")

    default_id = registry.default_id()
    if default_id:
        combat = _plugin_combat(default_id)
        if combat is not None:
            return combat
        fallback = _fallback_ruleset(default_id)
        if fallback is not None:
            return fallback
    raise CombatRulesetUnavailable(f"规则集 {key or '<none>'} 没有可用的战斗实现")


def list_rulesets() -> list[dict[str, str]]:
    items: dict[str, dict[str, str]] = {}
    registry = builtin_registry()
    for ruleset_id in registry.ids():
        combat = _plugin_combat(ruleset_id)
        if combat is not None:
            items[ruleset_id] = {
                "id": ruleset_id,
                "name": str(getattr(combat, "display_name", ruleset_id)),
            }
    for ruleset_id in _fallback_module_map():
        ruleset = _fallback_ruleset(ruleset_id)
        if ruleset is not None:
            items.setdefault(
                ruleset_id,
                {"id": ruleset_id, "name": str(getattr(ruleset, "display_name", ruleset_id))},
            )
    return list(items.values())


def _capability(name: str, ruleset_id: str | None = None) -> Any | None:
    try:
        ruleset = get_ruleset(ruleset_id)
    except CombatRulesetUnavailable:
        return None
    return getattr(ruleset, name, None)


def level_rank(ruleset_id: str | None = None) -> dict[str, int]:
    """成功等级排序表；插件可用 ``level_rank()`` 提供，缺省用核心默认顺序。"""
    value = _capability("level_rank", ruleset_id)
    table = value() if callable(value) else value
    return dict(table) if isinstance(table, Mapping) else dict(DEFAULT_LEVEL_RANK)


def skipped_statuses(ruleset_id: str | None = None) -> frozenset[str]:
    """失去行动能力的状态集合；插件可用 ``skipped_statuses()`` 覆盖。"""
    value = _capability("skipped_statuses", ruleset_id)
    statuses = value() if callable(value) else value
    if isinstance(statuses, (set, frozenset, list, tuple)):
        return frozenset(str(item) for item in statuses)
    return DEFAULT_SKIPPED_STATUSES


def build_and_damage_bonus(
    str_value: Any, siz_value: Any, ruleset_id: str | None = None
) -> tuple[int, str, list[tuple[int, int]]]:
    """伤害加值/体格；由插件的伤害模型提供，缺失时返回中性值。"""
    handler = _capability("build_and_damage_bonus", ruleset_id)
    if callable(handler):
        try:
            build, bonus, terms = handler(str_value, siz_value)
            return int(build), str(bonus), list(terms or [])
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("build_and_damage_bonus failed", exc_info=True)
    return 0, "", []


def unarmed_weapon(ruleset_id: str | None = None) -> dict[str, Any]:
    """徒手武器目录条目；插件可用 ``unarmed_weapon()`` 提供，缺省退中性徒手。

    中性条目不带任何规则集的专精键 / 贯穿 / 卡壳语义，只保证攻击链路可用。
    """
    from trpg_server.combat.contracts import DEFAULT_UNARMED_WEAPON

    value = _capability("unarmed_weapon", ruleset_id)
    weapon = value() if callable(value) else value
    if isinstance(weapon, Mapping) and weapon:
        return dict(weapon)
    return dict(DEFAULT_UNARMED_WEAPON)


def _max_total(terms: Any) -> int:
    return sum(max(0, int(count)) * max(1, int(sides)) for count, sides in terms or [])


def extreme_damage(
    terms: Any,
    fixed: int,
    db: Any,
    *,
    raw: str = "",
    impale: bool = False,
    allow_impale: bool = True,
    roll_damage: Any = None,
    ruleset_id: str | None = None,
) -> tuple[int, str]:
    """极限成功的伤害合成；插件可用 ``extreme_damage()`` 覆盖。

    核心只转交武器解析结果（骰项 / 固定值 / 伤害加值骰项 / 原始表达式），
    缺省按「取满骰面、无贯穿追加」的中性合成。
    """
    handler = _capability("extreme_damage", ruleset_id)
    if callable(handler):
        try:
            total, detail = handler(
                terms,
                int(fixed),
                db,
                raw=raw,
                impale=impale,
                allow_impale=allow_impale,
                roll_damage=roll_damage,
            )
            return int(total), str(detail)
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("extreme_damage failed", exc_info=True)
    total = _max_total(terms) + int(fixed) + _max_total(db)
    return max(0, total), f"极限成功·满伤害（{raw}{' +DB' if db else ''}）"


def weapon_malfunction(roll: Any, threshold: Any, ruleset_id: str | None = None) -> bool:
    """射击卡壳判定；插件可用 ``weapon_malfunction()`` 提供，缺省恒不卡壳。"""
    handler = _capability("weapon_malfunction", ruleset_id)
    if callable(handler):
        try:
            return bool(handler(roll, threshold))
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("weapon_malfunction failed", exc_info=True)
    return False


def weapon_field_rules(weapon: Any, ruleset_id: str | None = None) -> dict[str, Any]:
    """武器行里规则集专属数据字段的解读；插件可覆盖，缺省中性。

    核心只认识跨规则集的武器行字段（伤害 / 射程 / 每轮攻击数 / 装弹量），
    ``malfunction``（故障值整数）与 ``impale``（贯穿标记）这两列的含义由
    规则集解读：coc7 把故障值 ``95`` 解析为卡壳阈值、``impale`` 解析为贯穿
    武器标记；缺席规则集退中性（``malfunction=None`` 恒不卡壳、
    ``impale=False`` 恒不贯穿），与 ``weapon_malfunction`` 缺省语义一致。
    """
    handler = _capability("weapon_field_rules", ruleset_id)
    if callable(handler):
        try:
            value = handler(weapon)
            if isinstance(value, Mapping):
                return {"malfunction": value.get("malfunction"), "impale": bool(value.get("impale"))}
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("weapon_field_rules failed", exc_info=True)
    return {"malfunction": None, "impale": False}


def skill_names(kind: str, ruleset_id: str | None = None) -> tuple[str, ...]:
    """战斗技能名回退表；插件可用 ``<kind>_skill_names()`` 提供，缺省空表。

    角色卡技能行缺失或未声明专精键时，核心按名称兜底取技能值；CoC 的
    「格斗（斗殴）/ 射击（手枪）/ 闪避」名称表由 coc7 插件提供，其他规则集
    缺席退空表（按 default 与请求值呈现，不套用 CoC 名称）。
    """
    handler = _capability(f"{str(kind or '').strip().lower()}_skill_names", ruleset_id)
    value = handler() if callable(handler) else handler
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def auto_fire_composition(
    bullets: Any, extreme: Any, difficulty_rank: Any, ruleset_id: str | None = None
) -> tuple[int, int]:
    """全自动的（命中数, 贯穿数）配比；插件可覆盖，缺省全中且不贯穿。"""
    handler = _capability("auto_fire_composition", ruleset_id)
    if callable(handler):
        try:
            hits, impale_count = handler(bullets, extreme, difficulty_rank)
            return int(hits), int(impale_count)
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("auto_fire_composition failed", exc_info=True)
    return max(1, int(bullets or 1)), 0


def initiative_score(level: Any, dex: Any, ruleset_id: str | None = None) -> int:
    """可选先攻检定的数值合成；插件可用 ``initiative_score()`` 覆盖。

    缺省按「等级排序 ×1000 + DEX」合成：等级名与排序仍由 ``level_rank()``
    提供，本缺省只保证占位规则集也能给出确定顺序。
    """
    handler = _capability("initiative_score", ruleset_id)
    if callable(handler):
        try:
            return int(handler(level, dex))
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("initiative_score failed", exc_info=True)
    return level_rank(ruleset_id).get(level, 0) * 1000 + int(dex or 0)


def initial_sanity(power: Any, ruleset_id: str | None = None) -> int:
    """无卡参与者未显式给出理智时的起始值；插件可覆盖，缺省 0。

    该钩子只在规则集声明了 ``san`` 资源槽位（战斗侧理智字段存在）时被调用，
    因此缺省值不会给无关规则集的战斗者凭空造出理智字段。
    """
    handler = _capability("initial_sanity", ruleset_id)
    if callable(handler):
        try:
            return max(0, int(handler(power)))
        except Exception:  # noqa: BLE001 - 规则实现异常不应击穿战斗主循环
            logger.warning("initial_sanity failed", exc_info=True)
    return 0
