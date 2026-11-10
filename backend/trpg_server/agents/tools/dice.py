import logging
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.trigger import reveal_trigger_after_check
from trpg_server.dice_policy import DEFAULT_ROLL_POLICY, faces_with_policy, roll_with_policy

logger = logging.getLogger(__name__)
def _as_int(value: Any, fallback: int = 0) -> int:
    try: return int(value)
    except (TypeError, ValueError): return fallback
def _threshold(target: int, difficulty: str) -> int:
    checks = _checks_capability()
    threshold = getattr(checks, "threshold", None)
    return threshold(int(target), difficulty) if callable(threshold) else int(target)
def _normalize_difficulty(value: Any) -> str:
    checks = _checks_capability()
    normalize = getattr(checks, "normalize_difficulty", None)
    if callable(normalize):
        return normalize(value)
    text = str(value or "regular").strip().lower()
    return "regular" if text in {"", "regular", "普通"} else ""
def _default_thresholds(ruleset_id: str | None = None) -> tuple[int, int]:
    """当前规则集的大成功 / 大失败默认阈值；未声明时返回中性值（不额外判定）。"""
    checks = _checks_capability(ruleset_id)
    provider = getattr(checks, "default_thresholds", None)
    if callable(provider):
        return provider()
    return 0, 101
def _difficulty_label(difficulty: str) -> str:
    checks = _checks_capability()
    label = getattr(checks, "difficulty_label", None)
    return label(difficulty) if callable(label) else ""
def _level_label(level: str) -> str:
    checks = _checks_capability()
    label = getattr(checks, "level_label", None)
    return label(level) if callable(label) else str(level or "")
def _adjust_threshold(threshold: int, adjustment: Any) -> int:
    if isinstance(adjustment, (int, float)) and not isinstance(adjustment, bool):
        return threshold + int(adjustment)
    text = str(adjustment or "").strip()
    if not text: return threshold
    if not (text.startswith(("+", "-")) and text[1:].isdigit()): raise ValueError("adjustment must start with + or - and contain a number")
    amount = int(text[1:]); return threshold + amount if text.startswith("+") else threshold - amount
def _dice_count(arguments: dict[str, Any], key: str, alias: str) -> int:
    value = arguments.get(key, arguments.get(alias, 0))
    if isinstance(value, bool): return 1 if value else 0
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "on"}: return 1
    try: return max(0, min(10, int(value or 0)))
    except (TypeError, ValueError): return 0
MAX_BONUS_PENALTY = 2


def _normalize_bonus_penalty(bonus: Any, penalty: Any) -> tuple[int, int]:
    """奖励 / 惩罚骰净额：两者同时存在先相互抵消，净额最多 2 颗。

    通用检定、战斗检定与对抗检定都走这里，保证口径一致。
    """
    try:
        b = max(0, int(bonus or 0))
    except (TypeError, ValueError):
        b = 0
    try:
        p = max(0, int(penalty or 0))
    except (TypeError, ValueError):
        p = 0
    net = max(-MAX_BONUS_PENALTY, min(MAX_BONUS_PENALTY, b - p))
    return (net, 0) if net > 0 else (0, -net)


def _active_roll_policy(ruleset_id: str | None = None):
    """取当前规则集的骰子策略；插件未提供时用核心默认的百分骰原语。"""
    try:
        from trpg_server.ruleset import get_plugin

        policy = getattr(get_plugin(ruleset_id), "roll_policy", None)
        if policy is not None:
            return policy
    except Exception:  # noqa: BLE001 - 无可用插件时退回核心默认策略
        logger.debug("Falling back to default roll policy", exc_info=True)
    return DEFAULT_ROLL_POLICY


def _percentile_roll(
    roller: Callable[[int], int], bonus: int = 0, penalty: int = 0, ruleset_id: str | None = None
) -> tuple[int, list[int]]:
    """掷骰并返回 (结果, 候选集)；候选与选择规则由规则集的 RollPolicy 决定。"""
    return roll_with_policy(_active_roll_policy(ruleset_id), roller, bonus, penalty)


def _active_members(context: Any) -> list[dict[str, Any]]:
    return [m for m in context.room_info().get("members", []) if isinstance(m, dict)]
def _member_names(member: dict[str, Any]) -> set[str]:
    """成员可被 KP 引用的名字：登录名、角色卡姓名、绑定角色名。"""
    names: set[str] = set()
    for key in ("username", "character_name", "display_name"):
        value = str(member.get(key) or "").strip()
        if value:
            names.add(value.casefold())
    card = member.get("character_card")
    if isinstance(card, dict):
        value = str(card.get("name") or "").strip()
        if value:
            names.add(value.casefold())
    return names


def _find_member(player_name: str, context: Any) -> dict[str, Any] | None:
    expected = str(player_name or "").strip().casefold()
    if not expected:
        return None
    return next(
        (
            m for m in _active_members(context)
            if m.get("is_active", True) is not False
            and m.get("status", "active") != "removed"
            and expected in _member_names(m)
        ),
        None,
    )


def _checks_capability(ruleset_id: str | None = None):
    """当前规则集的判定能力；插件未提供时返回 ``None``（按不识别处理）。"""
    try:
        from trpg_server.ruleset import get_plugin

        return getattr(get_plugin(ruleset_id), "checks", None)
    except Exception:  # noqa: BLE001 - 插件缺失不应阻断工具注册
        logger.debug("Falling back to no checks capability", exc_info=True)
        return None


def _is_known_check_name(check_name: str) -> bool:
    """判断检定名是否为当前规则集认可的属性、技能或专精。"""
    checks = _checks_capability()
    known = getattr(checks, "is_known_check_name", None)
    return bool(known(check_name)) if callable(known) else False


def _resolve_check_name(arguments: dict[str, Any]) -> str:
    """确定检定名。

    ``name`` 是历史参数名，容易被模型误解成“玩家/角色名”。若同时提供了
    ``skill`` 且它是合法检定名，则优先使用 ``skill``；否则再退回 ``name``。
    """
    skill = str(arguments.get("skill") or "").strip()
    name = str(
        arguments.get("name") or arguments.get("check") or arguments.get("check_name") or ""
    ).strip()
    if skill and _is_known_check_name(skill):
        return skill
    if name and _is_known_check_name(name):
        return name
    return skill or name


def _lookup_check_value(card: dict[str, Any], check_name: str) -> int | None:
    ruleset_id = card.get("rulesetId") if isinstance(card, dict) else None
    checks = _checks_capability(ruleset_id)
    lookup = getattr(checks, "lookup_target", None)
    return lookup(card, check_name) if callable(lookup) else None
def _format_room_check_summary(name: str, difficulty: str, roll: int, threshold: int, success: bool) -> str:
    return f"{_difficulty_label(difficulty)}{name} d%: [{roll}] = {roll} / {threshold} {'成功' if success else '失败'}"
def _threshold_arg(arguments: dict[str, Any], key: str, fallback: int) -> int:
    """读取可配置的大成功/大失败阈值，做范围保护后返回。"""
    value = _as_int(arguments.get(key), fallback)
    return max(0, min(100, value))
def _context_thresholds(context: Any, ruleset_id: str | None = None) -> tuple[int, int]:
    """读取房间统一的大成功/大失败阈值。

    优先级：房规内配置 > 管理员设置页的全局默认值 > 规则集默认值。
    """
    critical = fumble = None
    room_info = context.room_info() if hasattr(context, "room_info") else None
    if isinstance(room_info, dict):
        rules = room_info.get("house_rules")
        if isinstance(rules, dict):
            critical = _nullable_int(rules.get("dice_critical_threshold"))
            fumble = _nullable_int(rules.get("dice_fumble_threshold"))
    if critical is None or fumble is None:
        defaults = _admin_thresholds(getattr(context, "config_dir", None), ruleset_id)
        if critical is None:
            critical = defaults[0]
        if fumble is None:
            fumble = defaults[1]
    return max(0, min(100, critical)), max(0, min(100, fumble))


def _nullable_int(value: Any) -> int | None:
    """房规中的阈值可能为空（表示沿用默认值）。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _admin_thresholds(config_dir: Any, ruleset_id: str | None = None) -> tuple[int, int]:
    """读取管理员设置页配置的骰娘默认阈值，读取失败时回退到规则集默认值。"""
    if not config_dir:
        return _default_thresholds(ruleset_id)
    try:
        from trpg_server.agents.config import load_ai_runtime_config

        config = load_ai_runtime_config(config_dir)
        return (
            max(0, min(100, int(config.dice_critical_threshold))),
            max(0, min(100, int(config.dice_fumble_threshold))),
        )
    except Exception:  # 配置缺失或损坏时不影响检定
        return _default_thresholds(ruleset_id)


def _merge_context_thresholds(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """在未显式传参时，用房间统一阈值补齐大成功/大失败阈值。"""
    if "critical_threshold" in arguments and "fumble_threshold" in arguments:
        return arguments
    critical, fumble = _context_thresholds(context)
    merged = dict(arguments)
    merged.setdefault("critical_threshold", critical)
    merged.setdefault("fumble_threshold", fumble)
    return merged
def _success_level(
    roll: int,
    target: int,
    critical_threshold: int | None = None,
    fumble_threshold: int | None = None,
) -> str:
    """成功等级由规则集插件决定；未声明时返回中性的成功 / 失败。"""
    checks = _checks_capability()
    resolve = getattr(checks, "success_level", None)
    if callable(resolve):
        return resolve(roll, target, critical_threshold, fumble_threshold)
    return "regular" if int(roll) <= int(target) else "failure"
def _level_suffix(level: str) -> str:
    """为大成功/大失败追加明确的文字标注，供聊天与 AI 上下文直接读取。"""
    checks = _checks_capability()
    suffix = getattr(checks, "level_suffix", None)
    return suffix(level) if callable(suffix) else ""
def _localize_check_label(name: str) -> str:
    """把英文键形式的属性/技能名转换为当前规则集的展示名。"""
    checks = _checks_capability()
    localize = getattr(checks, "localize_label", None)
    return localize(name) if callable(localize) else str(name or "").strip()


def _resolve_check_display_name(card: dict[str, Any], check_name: str) -> str:
    """优先使用角色卡上的本地化技能名，避免检定结果里出现英文键。"""
    ruleset_id = card.get("rulesetId") if isinstance(card, dict) else None
    checks = _checks_capability(ruleset_id)
    display = getattr(checks, "display_name", None)
    if callable(display):
        return display(card, check_name)
    return str(check_name or "").strip()
def _result_message(summary: str, tool_name: str, extra_metadata: dict[str, Any] | None = None, **metadata: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {"tool_name": tool_name, **metadata}
    if isinstance(extra_metadata, dict):
        merged.update(extra_metadata)
    return {"type": "dice", "sender_name": "骰娘", "avatar": "", "content": summary, "metadata": merged}


def _percentile_from_faces(
    faces: list[int] | tuple[int, ...],
    bonus: int = 0,
    penalty: int = 0,
    ruleset_id: str | None = None,
) -> tuple[int, list[int]]:
    """由物理 d10 骰面（十位/个位/候选十位）推导结果。

    与 :func:`_percentile_roll` 完全同一口径：候选集与选择规则由规则集的
    ``RollPolicy`` 决定；奖励与惩罚先抵消，净额最多 2 级。
    """
    return faces_with_policy(_active_roll_policy(ruleset_id), faces, bonus, penalty)


def _roll_terms_from_faces(
    terms: list[tuple[int, int]],
    faces: list[int] | tuple[int, ...],
) -> list[dict[str, Any]]:
    """把物理骰面按投掷表达式还原成各加法项的骰值；d100 消耗两枚 d10。"""
    values = [int(value) for value in faces]
    cursor = 0
    rolled_terms: list[dict[str, Any]] = []
    for count, sides in terms:
        results: list[int] = []
        if sides == 100:
            for _ in range(count):
                if cursor + 2 > len(values):
                    raise ValueError("not enough faces for d100")
                tens = values[cursor] % 10
                units = values[cursor + 1] % 10
                cursor += 2
                results.append(100 if tens == 0 and units == 0 else tens * 10 + units)
        else:
            for _ in range(count):
                if cursor >= len(values):
                    raise ValueError("not enough faces for dice expression")
                face = values[cursor]
                cursor += 1
                if not 1 <= face <= sides:
                    raise ValueError("dice face out of range")
                results.append(face)
        rolled_terms.append(
            {
                "expression": f"{count}d{sides}",
                "count": count,
                "sides": sides,
                "rolls": results,
                "total": sum(results),
            }
        )
    return rolled_terms


def _percentile_faces(value: int) -> tuple[list[int], list[str]]:
    """把 1-100 的百分骰结果拆成两枚 d10 的骰面（十位 / 个位）。"""
    roll = max(1, min(100, int(value)))
    tens = 0 if roll == 100 else roll // 10
    units = roll % 10
    return [tens, units], [f"{tens * 10:02d}", str(units)]


def _percentile_groups(value: int, candidates: list[int] | None = None) -> tuple[list[dict[str, Any]], dict[str, int] | None]:
    """百分骰的骰组：两枚 d10（十位/个位），奖励/惩罚候选追加候选组。

    普通检定的百分骰只是十位/个位组合，没有「二选一」语义，因此不返回
    ``selected``（前端不高亮任何骰子）；只有奖励骰/惩罚骰需要从多个十位
    候选里取值时，才高亮被选中的十位骰。
    """
    faces, labels = _percentile_faces(value)
    groups: list[dict[str, Any]] = [
        {"role": "tens", "type": "d10", "count": 1, "faces": [faces[0]], "labels": [labels[0]]},
        {"role": "ones", "type": "d10", "count": 1, "faces": [faces[1]], "labels": [labels[1]]},
    ]
    seen = {int(value)}
    for candidate in candidates or []:
        try:
            number = int(candidate)
        except (TypeError, ValueError):
            continue
        if number in seen or not 1 <= number <= 100:
            continue
        seen.add(number)
        candidate_faces, candidate_labels = _percentile_faces(number)
        groups.append(
            {
                "role": "candidate",
                "type": "d10",
                "count": 1,
                "faces": [candidate_faces[0]],
                "labels": [candidate_labels[0]],
            }
        )
    # 有候选十位时才存在「取哪一个」的选择语义；否则不高亮任何骰面。
    selected = {"group": 0, "index": 0} if len(groups) > 2 else None
    return groups, selected


def _standard_groups(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 NdM（或混合加法项）转换成骰组；d100 展开为两枚 d10。"""
    groups: list[dict[str, Any]] = []
    for term in terms:
        sides = int(term.get("sides") or 0)
        rolls = [int(item) for item in (term.get("rolls") or [])]
        if sides == 100:
            for value in rolls:
                faces, labels = _percentile_faces(value)
                groups.append({"role": "tens", "type": "d10", "count": 1, "faces": [faces[0]], "labels": [labels[0]]})
                groups.append({"role": "ones", "type": "d10", "count": 1, "faces": [faces[1]], "labels": [labels[1]]})
            continue
        groups.append(
            {
                "role": "normal",
                "type": f"d{sides}" if sides else "d20",
                "count": len(rolls),
                "faces": rolls,
                "labels": [str(item) for item in rolls],
            }
        )
    return groups


def _dice_roll_metadata(
    result: dict[str, Any],
    *,
    mode: str,
    check_name: str | None = None,
    difficulty: str | None = None,
    target: int | None = None,
    roller: str | None = None,
    roller_user_id: Any = None,
    show_table: bool = True,
    player_roll: bool = False,
    request_id: str | None = None,
    auto: bool = False,
    player_roll_ignored: bool = False,
) -> dict[str, Any]:
    """生成供前端虚拟投掷桌使用的展示元数据（只追加，不改变原有返回字段）。"""
    if mode == "percentile":
        value = int(result.get("roll") or 0)
        faces, labels = _percentile_faces(value)
        bonus_dice = int(result.get("bonus_dice") or 0)
        penalty_dice = int(result.get("penalty_dice") or 0)
        rolls = [value]
        candidates = list(result.get("rolls") or []) if (bonus_dice or penalty_dice) else []
        expression, count, sides, total = "1d100", 2, 10, value
        groups, selected = _percentile_groups(value, candidates)
        success = bool(result.get("success"))
        success_level = result.get("success_level")
    else:
        expression = str(result.get("expression") or "")
        terms = result.get("terms") if isinstance(result.get("terms"), list) else []
        match = re.fullmatch(r"(\d+)d(\d+)", expression)
        count = int(sum(int(term.get("count") or 0) for term in terms)) if terms else (int(match.group(1)) if match else len(result.get("rolls") or []))
        sides = int(match.group(2)) if match else 0
        rolls = [int(item) for item in (result.get("rolls") or [])]
        faces = rolls
        labels = [str(item) for item in rolls]
        total = int(result.get("total") or sum(rolls))
        candidates = []
        if terms:
            groups = _standard_groups(terms)
        elif sides == 100:
            groups = _standard_groups([{"sides": 100, "rolls": rolls}])
        else:
            groups = [
                {
                    "role": "normal",
                    "type": f"d{sides}" if sides else "d20",
                    "count": len(rolls),
                    "faces": rolls,
                    "labels": labels,
                }
            ]
        selected = None
        success = result.get("success")
        success_level = result.get("success_level")
    return {
        "mode": mode,
        "expression": expression,
        "count": count,
        "sides": sides,
        "faces": faces,
        "labels": labels,
        "groups": groups,
        "selected": selected,
        "roll": int(result.get("roll") if result.get("roll") is not None else total),
        "rolls": rolls,
        "candidates": candidates,
        "bonus_dice": int(result.get("bonus_dice") or 0),
        "penalty_dice": int(result.get("penalty_dice") or 0),
        "total": total,
        "success": success,
        "success_level": success_level,
        "check_name": check_name,
        "difficulty": difficulty if difficulty != "regular" else None,
        "target": target,
        "roller": roller,
        "roller_user_id": roller_user_id,
        "request_id": request_id,
        "auto": auto,
        "show_table": bool(show_table),
        "player_roll": bool(player_roll),
        "player_roll_ignored": bool(player_roll_ignored),
        "reroll": {"enabled": False, "mode": "off", "limit": 0, "used": 0, "remaining": 0},
        "confirmed": True,
    }


def _effective_dice_settings(context: Any) -> dict[str, Any]:
    """读取生效的骰子档位与玩家投掷开关（聊天路径用 tool_state，其他路径自行解析）。"""
    state = getattr(context, "tool_state", None)
    if isinstance(state, dict) and "dice_animation_level" in state:
        return {
            "level": str(state.get("dice_animation_level") or "3d"),
            "player_roll": bool(state.get("player_dice_roll")),
            "physics_results": bool(state.get("dice_physics_results", True)),
            "timeout": int(state.get("player_dice_roll_timeout") or 120),
        }
    from trpg_server.agents.dice_settings import resolve_dice_settings

    room_info = context.room_info() if hasattr(context, "room_info") else {}
    rules = room_info.get("house_rules") if isinstance(room_info, dict) else None
    return resolve_dice_settings(getattr(context, "config_dir", None), rules)


def _context_username(context: Any) -> str:
    state = getattr(context, "tool_state", None)
    if isinstance(state, dict):
        name = str(state.get("user_name") or "").strip()
        if name:
            return name
    return str(getattr(context, "user_id", "") or "")


def _player_online(user_id: Any) -> bool:
    """目标玩家是否在线；无 Socket 基础设施（测试/控制台）时视为在线。"""
    if user_id in (None, ""):
        return False
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return True
        registry = current_app.config.get("ACTIVE_SOCKET_SESSIONS") or {}
        if not registry:
            return True
        from trpg_server.security import is_socket_user_online

        return is_socket_user_online(user_id)
    except Exception:  # noqa: BLE001
        return True


def _should_wait_for_visual_roll(context: Any, arguments: dict[str, Any], roller_user_id: Any) -> bool:
    """物理结果模式：需要等待客户端动画落定并上报骰面。

    与玩家投掷开关无关；纯文本档位、暗投、无 Socket 或目标玩家离线时
    直接使用服务端旧生成器兜底。
    """
    if arguments.get("dark") or arguments.get("secret"):
        return False
    settings = _effective_dice_settings(context)
    if not settings.get("physics_results"):
        return False
    if str(settings.get("level") or "") == "text":
        return False
    if not getattr(context, "room_id", None):
        return False
    if roller_user_id in (None, ""):
        return False
    try:
        from trpg_server.agents import player_roll

        if not player_roll.has_broadcaster():
            return False
    except Exception:  # noqa: BLE001
        return False
    return _player_online(roller_user_id)


def _should_wait_for_player(context: Any, arguments: dict[str, Any], roller_user_id: Any) -> bool:
    """是否需要阻塞等待玩家投掷（与动画档位无关，只有暗投/模式关闭/离线才跳过）。"""
    if arguments.get("dark") or arguments.get("secret"):
        return False
    settings = _effective_dice_settings(context)
    if not settings.get("player_roll"):
        return False
    if not getattr(context, "room_id", None):
        return False
    if roller_user_id in (None, ""):
        return False
    try:
        from trpg_server.agents import player_roll

        if not player_roll.has_broadcaster():
            return False
    except Exception:  # noqa: BLE001
        return False
    return _player_online(roller_user_id)


def _plan_percentile(*, check_name: str | None, difficulty: str | None, target: int | None, bonus: int, penalty: int) -> dict[str, Any]:
    bonus, penalty = _normalize_bonus_penalty(bonus, penalty)
    groups: list[dict[str, Any]] = [
        {"role": "tens", "type": "d10", "count": 1, "labels": []},
        {"role": "ones", "type": "d10", "count": 1, "labels": []},
    ]
    for _ in range(max(0, int(bonus)) + max(0, int(penalty))):
        groups.append({"role": "candidate", "type": "d10", "count": 1, "labels": []})
    return {
        "mode": "percentile",
        "expression": "1d100",
        "count": 2,
        "sides": 10,
        "groups": groups,
        "selected": None,
        "bonus_dice": int(bonus),
        "penalty_dice": int(penalty),
        "candidates": [],
        "check": {"name": check_name, "difficulty": difficulty, "target": target},
    }


def _room_check_plan(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    check_name = _resolve_check_name(arguments)
    difficulty = _normalize_difficulty(arguments.get("difficulty")) or "regular"
    bonus = _dice_count(arguments, "bonus_dice", "bonus")
    penalty = _dice_count(arguments, "penalty_dice", "penalty")
    target: int | None = None
    display_name = check_name
    try:
        member = _find_member(player_name, context)
        card = member.get("character_card") if member else None
        if isinstance(card, dict):
            base = _lookup_check_value(card, check_name)
            display_name = _resolve_check_display_name(card, check_name)
            if base is not None and base >= 0:
                target = _adjust_threshold(_threshold(base, difficulty), arguments.get("adjustment", arguments.get("correction")))
    except (TypeError, ValueError):
        target = None
    return _plan_percentile(check_name=display_name, difficulty=difficulty, target=target, bonus=bonus, penalty=penalty)


def _coc_check_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    difficulty = _normalize_difficulty(arguments.get("difficulty")) or "regular"
    target = _as_int(arguments.get("target"))
    return _plan_percentile(
        check_name=str(arguments.get("skill") or "检定"),
        difficulty=difficulty,
        target=target if 1 <= target <= 100 else None,
        bonus=_dice_count(arguments, "bonus_dice", "bonus"),
        penalty=_dice_count(arguments, "penalty_dice", "penalty"),
    )


def _sanity_check_plan(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    current: int | None = None
    sanity = None
    try:
        member = _find_member(player_name, context)
        card = member.get("character_card") if member else None
        if isinstance(card, dict):
            # 房间快照的当前理智字段由规则集声明；旧代码读 sanity 字典导致目标恒为 0。
            sanity = _sanity_for(card)
            state = member.get("character_state") if isinstance(member, dict) else {}
            state = state if isinstance(state, dict) else {}
            if sanity is not None:
                from trpg_server.ruleset.capabilities import derive_card

                snapshot = sanity.snapshot(card, derive_card(_plugin_of(card), card), state)
                current = snapshot["current"]
    except Exception:  # noqa: BLE001
        current = None
    bonus, penalty = _normalize_bonus_penalty(
        _dice_count(arguments, "bonus_dice", "bonus"),
        _dice_count(arguments, "penalty_dice", "penalty"),
    )
    check_name = str(getattr(sanity, "check_name", "") or "")
    return _plan_percentile(check_name=check_name, difficulty=None, target=current, bonus=bonus, penalty=penalty)


_DICE_TERM_RE = re.compile(r"(\d+)d(\d+)", re.IGNORECASE)


def _parse_dice_expression(expression: str) -> list[tuple[int, int]] | None:
    """解析 ``NdM`` 或加法混合表达式（如 ``1d100+2d6``）；非法返回 ``None``。"""
    compact = str(expression or "").replace(" ", "").lower()
    if not compact:
        return None
    terms: list[tuple[int, int]] = []
    for part in compact.split("+"):
        match = _DICE_TERM_RE.fullmatch(part)
        if not match:
            return None
        terms.append((int(match.group(1)), int(match.group(2))))
    return terms


def _plan_groups(terms: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """计划阶段的骰组（无面值），供玩家投掷前摆骰。"""
    groups: list[dict[str, Any]] = []
    for count, sides in terms:
        if sides == 100:
            for _ in range(count):
                groups.append({"role": "tens", "type": "d10", "count": 1, "labels": []})
                groups.append({"role": "ones", "type": "d10", "count": 1, "labels": []})
            continue
        groups.append({"role": "normal", "type": f"d{sides}", "count": count, "labels": []})
    return groups


def _generic_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    expression = str(arguments.get("expression") or arguments.get("dice") or "1d100").strip().lower()
    terms = _parse_dice_expression(expression) or [(1, 100)]
    count = sum(item[0] for item in terms)
    sides = terms[0][1] if len(terms) == 1 else 0
    return {
        "mode": "standard",
        "expression": expression,
        "count": count,
        "sides": sides,
        "groups": _plan_groups(terms),
        "selected": None,
        "bonus_dice": 0,
        "penalty_dice": 0,
        "candidates": [],
        "check": None,
    }


def _finalize_dice_metadata(
    result: dict[str, Any],
    context: Any,
    *,
    show_table: bool = True,
    player_roll_requested: bool = False,
) -> dict[str, Any]:
    """统一修正展示元数据：纯文本档位忽略桌面；模式关闭时标记 player_roll_ignored。"""
    if not isinstance(result, dict):
        return result
    settings = _effective_dice_settings(context)
    message = result.get("visible_message")
    if isinstance(message, dict):
        metadata = message.get("metadata")
        dice = metadata.get("dice_roll") if isinstance(metadata, dict) else None
        if isinstance(dice, dict):
            dice["show_table"] = bool(show_table) and settings.get("level") != "text"
            dice["player_roll"] = bool(player_roll_requested) and bool(settings.get("player_roll"))
            dice["player_roll_ignored"] = bool(player_roll_requested) and not settings.get("player_roll")
    if player_roll_requested and not settings.get("player_roll"):
        logger.info("dice.roll player_roll requested but player-roll mode is disabled; rolled immediately")
    return result


def _attach_trigger_reveal(result: dict[str, Any], arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """检定工具的可选能力：同一轮内揭示与该检定绑定的触发器。

    模型确认触发器 id 后，可把它随检定一起传入（``trigger_id``），工具按自己的
    检定结果决定是否揭示，省掉「先检定、再单独调用触发器工具」中间那次 AI 往返。
    未传 ``trigger_id`` 时原样返回，既有行为完全不变。
    """
    trigger_id = arguments.get("trigger_id")
    if trigger_id in (None, "") or result.get("cancelled"):
        return result
    revealed = reveal_trigger_after_check(trigger_id, bool(result.get("success")), context)
    if isinstance(revealed.get("direct_message"), dict):
        result["direct_message"] = revealed["direct_message"]
    if revealed.get("error"):
        result["trigger_error"] = revealed["error"]
    elif not revealed.get("triggered"):
        result["trigger_note"] = revealed.get("message")
    return result


def _log_roll_result(
    result: dict[str, Any],
    context: Any,
    *,
    tool_name: str,
    actor: Any = None,
    dark: bool = False,
) -> None:
    """把投掷/检定结果写入房间日志，让房间控制台能看到每一次投掷。"""
    room_dir = getattr(context, "room_dir", None)
    if not room_dir or not isinstance(result, dict):
        return
    if result.get("error") or result.get("cancelled"):
        return
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return
    details: dict[str, Any] = {"工具": tool_name}
    if dark:
        details["暗投"] = "是"
    try:
        from trpg_server.room_log import log_room_event

        log_room_event(
            room_dir,
            summary,
            source="dice",
            actor=actor,
            room_id=getattr(context, "room_id", None),
            logger_=logger,
            **details,
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响投掷结果
        logger.warning("Failed to write dice result to room log", exc_info=True)
    from trpg_server.agents.narrative.logger import log_event

    log_event(
        "check.resolve",
        room_id=str(getattr(context, "room_id", "") or ""),
        turn=_as_int(getattr(context, "turn", 0), 0),
        check_id=str(result.get("check_id") or ""),
        skill=str(result.get("skill") or result.get("name") or ""),
        difficulty=str(result.get("difficulty") or ""),
        rolled=result.get("roll"),
        success_level=str(result.get("raw_success_level") or result.get("success_level") or ""),
        success=bool(result.get("success")),
    )


def roll_coc_check(
    arguments: dict[str, Any],
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """COC7 检定。``faces`` 提供时使用物理骰面，不再调用随机数生成器。"""
    roller = rng or (lambda sides: random.randint(1, sides)); target = _as_int(arguments.get("target"))
    if target < 1 or target > 100: return {"error": "target must be between 1 and 100"}
    difficulty = _normalize_difficulty(arguments.get("difficulty"))
    if not difficulty: return {"error": "difficulty must be regular, hard, extreme, 普通, 困难, or 极难"}
    bonus_dice = _dice_count(arguments, "bonus_dice", "bonus"); penalty_dice = _dice_count(arguments, "penalty_dice", "penalty")
    # 奖励与惩罚同时存在时互相抵消，只有净额生效（上限 2 颗）。
    bonus_dice, penalty_dice = _normalize_bonus_penalty(bonus_dice, penalty_dice)
    try: threshold = _adjust_threshold(_threshold(target, difficulty), arguments.get("adjustment", arguments.get("correction")))
    except ValueError as exc: return {"error": str(exc)}
    default_critical, default_fumble = _default_thresholds()
    critical_threshold = _threshold_arg(arguments, "critical_threshold", default_critical)
    fumble_threshold = _threshold_arg(arguments, "fumble_threshold", default_fumble)
    roll, candidates = (
        _percentile_from_faces(faces, bonus_dice, penalty_dice)
        if faces is not None
        else _percentile_roll(roller, bonus_dice, penalty_dice)
    )
    level = _success_level(roll, target, critical_threshold, fumble_threshold); success = (roll <= threshold or level == "critical") and level != "fumble"
    skill = _localize_check_label(arguments.get("skill") or "检定"); reason = str(arguments.get("reason") or ""); modifier = str(arguments.get("adjustment", arguments.get("correction")) or "").strip()
    summary = f"{skill}检定：1d100={roll}，目标{target}，{_difficulty_label(difficulty) or '普通'}难度阈值{threshold}，结果：{_level_label(level)}。"
    # Keep the compact /check-compatible summary stable; adjustment is exposed
    # in the structured result while bonus/penalty candidates are expanded.
    if candidates: summary += f" {'奖励骰' if bonus_dice else '惩罚骰'}（{', '.join(map(str, candidates))}），取{roll}。"
    if reason: summary += f" 原因：{reason}。"
    result = {"skill": skill, "reason": reason, "roll": roll, "rolls": candidates or [roll], "target": target, "difficulty": difficulty, "threshold": threshold, "adjustment": modifier, "bonus_dice": bonus_dice, "penalty_dice": penalty_dice, "success": success, "success_level": level if success else "failure", "raw_success_level": level, "critical": level == "critical", "fumble": level == "fumble", "critical_threshold": critical_threshold, "fumble_threshold": fumble_threshold, "summary": summary}
    result["visible_message"] = _result_message(
        summary,
        "dice.roll_coc_check",
        check_type="coc",
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=skill,
                difficulty=difficulty,
                target=target,
                roller=str(arguments.get("character_name") or "") or None,
            )
        },
    )
    return result
def execute_roll_coc_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    roller_id = getattr(context, "user_id", None)
    merged = _merge_context_thresholds(arguments, context)
    physics = _should_wait_for_visual_roll(context, arguments, roller_id)
    click = _should_wait_for_player(context, arguments, roller_id)
    if physics or click:
        from trpg_server.agents import player_roll

        result = player_roll.wait_for_player_roll(
            context,
            _coc_check_plan(arguments),
            lambda: roll_coc_check(merged),
            roller_user_id=roller_id,
            roller_name=_context_username(context),
            tool_name="dice.roll_coc_check",
            face_resolver=(lambda faces: roll_coc_check(merged, faces=faces)) if physics else None,
            auto_roll=physics and not click,
        )
        result = _finalize_dice_metadata(result, context, show_table=True, player_roll_requested=click)
    else:
        result = roll_coc_check(merged)
        result = _finalize_dice_metadata(result, context)
    result = _attach_trigger_reveal(result, arguments, context)
    _log_roll_result(result, context, tool_name="dice.roll_coc_check", actor=_context_username(context))
    return result


def roll_room_check(
    arguments: dict[str, Any],
    context: Any,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """房间成员检定。``faces`` 提供时使用物理骰面，不再调用随机数生成器。

    数值判定统一交给 ``check_service`` 与当前房间规则集插件；本函数只保留
    AI 工具侧的参数解析、成员定位和物理骰等待流程。
    """
    from trpg_server import check_service

    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    check_name = _resolve_check_name(arguments)
    if not player_name:
        return {"error": "player_name is required"}
    if not check_name:
        return {"error": "name is required"}
    difficulty = _normalize_difficulty(arguments.get("difficulty"))
    if not difficulty:
        return {"error": "difficulty must be regular, hard, extreme, 普通, 困难, or 极难"}
    member = _find_member(player_name, context)
    if not member:
        return {"error": f"player {player_name} was not found in current room"}
    room_info = context.room_info() if hasattr(context, "room_info") else {}
    ruleset_id = str((room_info or {}).get("ruleset_id") or "").strip() or None
    exposure = str(arguments.get("exposure") or arguments.get("fear_source") or "").strip()
    fear = {"penalty": 0, "matched": "", "kind": ""}
    if exposure:
        card = member.get("character_card")
        state = member.get("character_state")
        sanity = _sanity_for(card) if isinstance(card, dict) else None
        if sanity is not None and isinstance(state, dict) and hasattr(sanity, "fear_penalty"):
            fear = sanity.fear_penalty(card, state, exposure, room_info=room_info)
    try:
        result = check_service.run_room_check(
            member,
            context=context,
            player_name=player_name,
            name=check_name,
            difficulty=difficulty,
            adjustment=arguments.get("adjustment", arguments.get("correction")),
            bonus=_dice_count(arguments, "bonus_dice", "bonus"),
            penalty=_dice_count(arguments, "penalty_dice", "penalty") + int(fear.get("penalty") or 0),
            critical_threshold=arguments.get("critical_threshold"),
            fumble_threshold=arguments.get("fumble_threshold"),
            ruleset_id=ruleset_id,
            rng=rng,
            faces=faces,
        )
    except check_service.CheckServiceError as exc:
        return {"error": str(exc)}
    if fear.get("penalty") and isinstance(result, dict) and "error" not in result:
        result["fear_exposure"] = fear
        if not result.get("pending") and isinstance(result.get("summary"), str):
            result["summary"] += f" 恐惧暴露「{fear['matched']}」：本次检定额外承受 {fear['penalty']} 颗惩罚骰。"
    return result


def execute_roll_room_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    member = _find_member(player_name, context)
    roller_id = member.get("user_id") if isinstance(member, dict) else None
    physics = _should_wait_for_visual_roll(context, arguments, roller_id)
    click = _should_wait_for_player(context, arguments, roller_id)
    if physics or click:
        from trpg_server.agents import player_roll

        result = player_roll.wait_for_player_roll(
            context,
            _room_check_plan(arguments, context),
            lambda: roll_room_check(arguments, context),
            roller_user_id=roller_id,
            roller_name=player_name,
            tool_name="check.roll_room_check",
            face_resolver=(lambda faces: roll_room_check(arguments, context, faces=faces)) if physics else None,
            auto_roll=physics and not click,
        )
        result = _finalize_dice_metadata(result, context, show_table=True, player_roll_requested=click)
    else:
        result = roll_room_check(arguments, context)
        result = _finalize_dice_metadata(result, context)
    result = _attach_trigger_reveal(result, arguments, context)
    _record_growth_mark(result, context, member)
    _log_roll_result(result, context, tool_name="check.roll_room_check", actor=player_name)
    return result


def _record_growth_mark(result: dict[str, Any], context: Any, member: dict[str, Any] | None) -> None:
    """AI KP 发起的技能检定成功后，同样立刻打成长标记。"""
    if not isinstance(result, dict) or not isinstance(member, dict):
        return
    room_dir = getattr(context, "room_dir", None)
    if room_dir is None or not hasattr(context, "room_info"):
        return
    try:
        from trpg_server import room_rules

        room_rules.record_growth_mark_for_check(room_dir, context.room_info(), member, result)
    except Exception:  # noqa: BLE001 - 成长标记失败不影响检定结果
        pass



def _plugin_of(card: Any):
    """取角色卡所属规则集插件；缺失时返回 None（通用层只做调度）。"""
    try:
        from trpg_server.ruleset import get_plugin

        return get_plugin((card or {}).get("rulesetId"))
    except Exception:  # noqa: BLE001 - 插件缺失时按无扩展能力处理
        return None


def _sanity_for(card: Any):
    """取规则集的理智能力；未声明时返回 None。"""
    from trpg_server.ruleset.capabilities import sanity_of

    return sanity_of(_plugin_of(card))


def _room_info_of(context: Any) -> dict[str, Any] | None:
    """读取房间信息用于房规开关；上下文缺失时视为全部关闭。"""
    try:
        info = context.room_info() if context is not None and hasattr(context, "room_info") else None
    except Exception:  # noqa: BLE001 - 房规读取失败不应中断检定
        return None
    return info if isinstance(info, dict) else None


def _sanity_day_key(arguments: dict[str, Any], context: Any) -> str:
    """游戏内日期键：调用方可显式给 game_date / game_day，否则回退本地日期。"""
    explicit = (arguments or {}).get("game_date") or (arguments or {}).get("game_day")
    if not explicit and context is not None and hasattr(context, "room_info"):
        info = context.room_info()
        if isinstance(info, dict):
            explicit = info.get("game_date") or info.get("game_day")
    text = str(explicit or "").strip()
    return text[:32] or time.strftime("%Y-%m-%d")


def roll_sanity_check(
    arguments: dict[str, Any],
    context: Any,
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """理智检定。``faces`` 只用于 1d100 判定，SAN 变化骰仍由服务端生成。"""
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    change = str(arguments.get("san_change") or arguments.get("sanity_change") or arguments.get("change") or arguments.get("san") or "").strip()
    if not player_name: return {"error": "player_name is required"}
    if "/" not in change: return {"error": "san_change must use success/failure format, e.g. 1d4/1d6"}
    success_expr, failure_expr = (part.strip() for part in change.split("/", 1))
    member = _find_member(player_name, context)
    if not member: return {"error": f"player {player_name} was not found in current room"}
    card = member.get("character_card")
    if not isinstance(card, dict): return {"error": f"player {player_name} has no bound character card"}
    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state
    sanity = _sanity_for(card)
    if sanity is None: return {"error": "当前规则集不支持理智检定"}
    from trpg_server.ruleset.capabilities import derive_card

    snapshot = sanity.snapshot(card, derive_card(_plugin_of(card), card), state)
    maximum, current = snapshot["max"], snapshot["current"]
    roller = rng or (lambda sides: random.randint(1, sides))
    bonus_dice, penalty_dice = _normalize_bonus_penalty(
        _dice_count(arguments, "bonus_dice", "bonus"),
        _dice_count(arguments, "penalty_dice", "penalty"),
    )
    try:
        if faces is not None:
            roll, _candidates = _percentile_from_faces(faces, bonus_dice, penalty_dice)
        else:
            roll, _candidates = _percentile_roll(roller, bonus_dice, penalty_dice)
        roll = max(1, min(100, int(roll)))
        critical_threshold, fumble_threshold = _context_thresholds(context)
        level = _success_level(roll, current, critical_threshold, fumble_threshold)
        success = roll <= current
        outcome = sanity.resolve_check(
            player_name=player_name,
            card=card,
            state=state,
            current=current,
            maximum=maximum,
            roll=roll,
            success=success,
            success_expr=success_expr,
            failure_expr=failure_expr,
            roller=roller,
            day=_sanity_day_key(arguments, context),
            room_info=_room_info_of(context),
            source=arguments.get("source") or arguments.get("cause"),
            illusion=bool(arguments.get("illusion") or arguments.get("hallucination")),
        )
    except ValueError as exc: return {"error": str(exc)}
    delta = int(outcome["delta"])
    new_san = int(outcome["new_san"])
    detail_rolls = list(outcome.get("detail_rolls") or [])
    daily = outcome.get("daily") or {}
    flags = outcome.get("flags") or {}
    madness_check_required = bool(outcome.get("madness_required"))
    # 房间快照里的 SAN 变化需要由归档流程写回持久角色卡，见 room_rules。
    state["pending_persist"] = True
    try:
        from trpg_server import room_rules as _room_rules

        change_list = [
            {"type": "san", "value": new_san, "source": "ai_tool", "reason": "理智检定"},
        ]
        if new_san <= 0:
            change_list.append({"type": "status", "value": dict(flags), "source": "ai_tool", "reason": "理智归零"})
        change_list.extend(outcome.get("pending_changes") or [])
        _room_rules.record_room_changes(member, change_list)
    except Exception:  # noqa: BLE001 - 待写回登记失败不应中断检定
        pass
    if context.room_dir:
        room_info = context.room_info()
        for room_member in room_info.get("members", []) if isinstance(room_info.get("members"), list) else []:
            if isinstance(room_member, dict) and room_member.get("user_id") == member.get("user_id"):
                # 若调用方持有同一个 dict（如房间命令的内存上下文），clear 会先清空再自更新，
                # 反而丢掉角色卡；同对象时无需回写。
                if room_member is not member:
                    room_member.clear(); room_member.update(member)
                break
        from trpg_server import room_rules as _room_rules

        _room_rules.save_room_info(context.room_dir, room_info)
    summary = str(outcome.get("summary") or "")
    result = {"player_name": player_name, "roll": roll, "san": current, "current_san": new_san, "max_san": maximum, "success": success, "success_level": level, "change": delta, "change_expression": success_expr if success else failure_expr, "change_rolls": detail_rolls, "san_change": change, "bonus_dice": bonus_dice, "penalty_dice": penalty_dice, "daily_san_loss": daily.get("loss", max(0, current - new_san)), "daily_san_base": daily.get("base"), "daily_san_date": daily.get("date"), "daily_threshold_met": bool(daily.get("threshold_met")), "madness_check_required": madness_check_required, "madness_note": str(outcome.get("madness_note") or ""), "temporary_insanity": bool(flags.get("temporaryInsanity")), "indefinite_insanity": bool(flags.get("indefiniteInsanity")), "permanent_insanity": bool(flags.get("permanentInsanity")), "summary": summary}
    if outcome.get("mental") is not None:
        result["mental"] = outcome["mental"]
        result["reality_immunity"] = bool(outcome.get("reality_immunity"))
    if outcome.get("habituation") is not None:
        result["habituation"] = outcome["habituation"]
    if outcome.get("bout_immunity"):
        result["bout_immunity"] = True
    if outcome.get("reality_waived"):
        result["reality_waived"] = True
    result["visible_message"] = _result_message(
        summary,
        "san.roll_sanity_check",
        check_type="sanity",
        player_name=player_name,
        current_san=new_san,
        max_san=maximum,
        madness_check_required=madness_check_required,
        temporary_insanity=bool(flags.get("temporaryInsanity")),
        indefinite_insanity=bool(flags.get("indefiniteInsanity")),
        permanent_insanity=bool(flags.get("permanentInsanity")),
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=sanity.check_name,
                target=current,
                roller=player_name,
            )
        },
    )
    return result

def execute_roll_sanity_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    member = _find_member(player_name, context)
    roller_id = member.get("user_id") if isinstance(member, dict) else None
    physics = _should_wait_for_visual_roll(context, arguments, roller_id)
    click = _should_wait_for_player(context, arguments, roller_id)
    if physics or click:
        from trpg_server.agents import player_roll

        # 理智变化必须延迟到玩家投掷完成后由 resolver 写入房间状态；
        # 物理模式只把 1d100 判定交给骰面，SAN 变化骰仍由服务端生成。
        result = player_roll.wait_for_player_roll(
            context,
            _sanity_check_plan(arguments, context),
            lambda: roll_sanity_check(arguments, context),
            roller_user_id=roller_id,
            roller_name=player_name,
            tool_name="san.roll_sanity_check",
            face_resolver=(lambda faces: roll_sanity_check(arguments, context, faces=faces)) if physics else None,
            auto_roll=physics and not click,
        )
        result = _finalize_dice_metadata(result, context, show_table=True, player_roll_requested=click)
    else:
        result = _finalize_dice_metadata(roll_sanity_check(arguments, context), context)
    _log_roll_result(result, context, tool_name="san.roll_sanity_check", actor=player_name)
    return result


def roll_madness(
    arguments: dict[str, Any],
    context: Any,
    rng: Callable[[int], int] | None = None,
) -> dict[str, Any]:
    """临时 / 不定 / 永久疯狂的通用编排：规则语义全部来自规则集插件。"""
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    if not player_name:
        return {"error": "player_name is required"}
    member = _find_member(player_name, context)
    if not member:
        return {"error": f"player {player_name} was not found in current room"}
    card = member.get("character_card")
    if not isinstance(card, dict):
        return {"error": f"player {player_name} has no bound character card"}
    sanity = _sanity_for(card)
    if sanity is None:
        return {"error": "当前规则集不支持理智检定"}
    state = member.setdefault("character_state", {})
    if not isinstance(state, dict):
        state = {}
        member["character_state"] = state

    bonus_dice, penalty_dice = _normalize_bonus_penalty(
        _dice_count(arguments, "bonus_dice", "bonus"),
        _dice_count(arguments, "penalty_dice", "penalty"),
    )
    critical_threshold, fumble_threshold = _context_thresholds(context)
    outcome = sanity.resolve_madness(
        player_name=player_name,
        card=card,
        state=state,
        arguments=arguments,
        roller=rng or (lambda sides: random.randint(1, sides)),
        bonus=bonus_dice,
        penalty=penalty_dice,
        critical_threshold=critical_threshold,
        fumble_threshold=fumble_threshold,
        success_level=_success_level,
        room_info=_room_info_of(context),
    )
    if outcome.get("skipped"):
        return outcome
    result = dict(outcome)
    pending = list(result.pop("pending_changes", []) or [])
    if result.pop("persist", False):
        state["pending_persist"] = True
        if pending:
            try:
                from trpg_server import room_rules as _room_rules

                _room_rules.record_room_changes(member, pending)
            except Exception:  # noqa: BLE001 - 待写回登记失败不应中断疯狂判定
                pass
    if context.room_dir:
        room_info = context.room_info()
        for room_member in room_info.get("members", []) if isinstance(room_info.get("members"), list) else []:
            if isinstance(room_member, dict) and room_member.get("user_id") == member.get("user_id"):
                if room_member is not member:
                    room_member.clear()
                    room_member.update(member)
                break
        from trpg_server import room_rules as _room_rules

        _room_rules.save_room_info(context.room_dir, room_info)
    result["visible_message"] = _result_message(
        str(result.get("summary") or ""),
        "san.roll_madness",
        check_type="sanity",
        player_name=player_name,
        insanity=result.get("madness"),
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=sanity.madness_check_name,
                target=_as_int(result.get("int_target"), 0),
                roller=player_name,
            )
        },
    )
    return result



def execute_roll_madness(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip()
    result = roll_madness(arguments, context)
    _log_roll_result(result, context, tool_name="san.roll_madness", actor=player_name)
    return _finalize_dice_metadata(result, context, show_table=False)

def roll_dice(
    arguments: dict[str, Any],
    rng: Callable[[int], int] | None = None,
    faces: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """通用骰。``faces`` 提供时按物理骰面还原各加法项。"""
    expression = str(arguments.get("expression") or arguments.get("dice") or "1d100").strip().lower()
    terms = _parse_dice_expression(expression)
    if not terms:
        return {"error": "dice expression must look like 1d6 or 1d100+2d6"}
    if sum(count for count, _sides in terms) < 1 or sum(count for count, _sides in terms) > 100:
        return {"error": "dice count must be 1-100 and sides 2-1000"}
    if any(not 2 <= sides <= 1000 for _count, sides in terms):
        return {"error": "dice count must be 1-100 and sides 2-1000"}
    roller = rng or (lambda sides: random.randint(1, sides))
    if faces is not None:
        rolled_terms = _roll_terms_from_faces(terms, faces)
    else:
        rolled_terms = []
        for count, sides in terms:
            values = [int(roller(sides)) for _ in range(count)]
            rolled_terms.append(
                {
                    "expression": f"{count}d{sides}",
                    "count": count,
                    "sides": sides,
                    "rolls": values,
                    "total": sum(values),
                }
            )
    rolls = [value for term in rolled_terms for value in term["rolls"]]
    total = sum(rolls)
    if len(rolled_terms) == 1:
        summary = f"投掷 {expression}：{' + '.join(map(str, rolls))}" + (f" = {total}" if len(rolls) > 1 else "")
    else:
        parts = [f"{term['expression']}={term['total']}" for term in rolled_terms]
        summary = f"投掷 {expression}：{' + '.join(parts)} = {total}"
    result = {"expression": expression, "rolls": rolls, "total": total, "summary": summary, "terms": rolled_terms}
    roll_table = bool(arguments.get("show_table")) or bool(arguments.get("player_roll"))
    want_player_roll = bool(arguments.get("player_roll"))
    if not arguments.get("dark", arguments.get("secret", False)):
        result["visible_message"] = _result_message(
            summary,
            "dice.roll",
            check_type="dice",
            extra_metadata={
                "dice_roll": _dice_roll_metadata(
                    result,
                    mode="standard",
                    show_table=roll_table,
                    player_roll=want_player_roll,
                )
            },
        )
    return result
def execute_roll_dice(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    dark = bool(arguments.get("dark") or arguments.get("secret"))
    request_player = bool(arguments.get("player_roll")) and not dark
    roller_id = getattr(context, "user_id", None)
    physics = _should_wait_for_visual_roll(context, arguments, roller_id)
    click = request_player and _should_wait_for_player(context, arguments, roller_id)
    if physics or click:
        from trpg_server.agents import player_roll

        result = player_roll.wait_for_player_roll(
            context,
            _generic_plan(arguments),
            lambda: roll_dice(arguments),
            roller_user_id=roller_id,
            roller_name=_context_username(context),
            tool_name="dice.roll",
            face_resolver=(lambda faces: roll_dice(arguments, faces=faces)) if physics else None,
            auto_roll=physics and not click,
        )
        result = _finalize_dice_metadata(
            result,
            context,
            show_table=bool(arguments.get("show_table")) or request_player or physics,
            player_roll_requested=click,
        )
        _log_roll_result(result, context, tool_name="dice.roll", actor=_context_username(context))
        return result
    result = roll_dice(arguments)
    if dark:
        _log_roll_result(result, context, tool_name="dice.roll", actor=_context_username(context), dark=True)
        return result
    result = _finalize_dice_metadata(
        result,
        context,
        show_table=bool(arguments.get("show_table")) or request_player,
        player_roll_requested=request_player,
    )
    _log_roll_result(result, context, tool_name="dice.roll", actor=_context_username(context))
    return result

ROLL_COC_CHECK_TOOL = AgentTool(name="dice.roll_coc_check", description="Roll a real backend COC7 percentile check. The result reports whether it is a critical success or fumble. Uses the room's configured critical/fumble thresholds unless overridden by arguments.", parameters={"type": "object", "properties": {"character_name": {"type": "string"}, "skill": {"type": "string"}, "target": {"type": "integer", "minimum": 1, "maximum": 100}, "difficulty": {"type": "string"}, "adjustment": {"type": "string"}, "correction": {"type": "string"}, "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "critical_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "fumble_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "reason": {"type": "string"}, "trigger_id": {"type": ["integer", "string"], "description": "Optional. When this check gates a scenario trigger, pass the trigger id so the trigger content is revealed in the same round if the check succeeds. Do not pass it when the trigger condition is not tied to this check."}}, "required": ["skill", "target"]}, handler=execute_roll_coc_check)
ROLL_ROOM_CHECK_TOOL = AgentTool(name="check.roll_room_check", description="Resolve a current-room player's character card and roll a COC7 check. player_name accepts the login name OR the character card name. For the check itself, pass skill (preferred) or name with the skill/attribute, e.g. 力量/敏捷/侦察/聆听/心理学/理智; never put a player or character name into the check name. The result reports whether it is a critical success or fumble. Uses the room's configured critical/fumble thresholds unless overridden by arguments.", parameters={"type": "object", "properties": {"player_name": {"type": "string", "description": "Room member's login username or character card name (not the skill name)."}, "skill": {"type": "string", "description": "Skill or attribute to test, e.g. 力量/敏捷/侦察/聆听/心理学. Prefer this field."}, "name": {"type": "string", "description": "Legacy alias of skill. Must be a skill or attribute name, never a player/character name."}, "difficulty": {"type": "string"}, "adjustment": {"type": "string"}, "correction": {"type": "string"}, "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "critical_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "fumble_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "exposure": {"type": "string", "description": "Optional. Exact name (or id/English name) of one of the character's recorded phobias/manias that the keeper judges is directly exposed right now, e.g. 蜘蛛恐惧. Use san.traits to look up the exact name first. Only an exact match grants the one penalty die; do not pass a loose keyword."}, "trigger_id": {"type": ["integer", "string"], "description": "Optional. When this check gates a scenario trigger, pass the trigger id so the trigger content is revealed in the same round if the check succeeds. Do not pass it when the trigger condition is not tied to this check."}}, "required": ["player_name"]}, handler=execute_roll_room_check)
ROLL_SANITY_CHECK_TOOL = AgentTool(name="san.roll_sanity_check", description="Roll a player's SAN check and apply success/failure sanity change directly to the room character state. If the room enables sanity habituation, pass source (the creature/phenomenon) so cumulative loss from that source is tracked and capped. Set illusion=true only when the loss comes from a hallucination, so a successful reality check's resistance can cancel it. Never add penalty dice for a phobia here; use check.roll_room_check with exposure for that.", parameters={"type": "object", "properties": {"player_name": {"type": "string"}, "san_change": {"type": "string", "description": "Success/failure change, e.g. 1d4/1d6 or +1d4/-1d6"}, "source": {"type": "string", "description": "Optional. The mythos source of this SAN check (e.g. 深潜者). Used for habituation tracking when enabled."}, "illusion": {"type": "boolean", "description": "Optional. True when the Sanity loss is caused by a hallucination/illusion, which can be cancelled by a successful reality check's resistance."}}, "required": ["player_name", "san_change"]}, handler=execute_roll_sanity_check)
ROLL_SANITY_CHECK_ALIAS_TOOL = AgentTool(name="sanity.roll_sanity_check", description=ROLL_SANITY_CHECK_TOOL.description, parameters=ROLL_SANITY_CHECK_TOOL.parameters, handler=execute_roll_sanity_check)
ROLL_MADNESS_TOOL = AgentTool(name="san.roll_madness", description="CoC7 madness resolution. Call this immediately whenever a player loses 5 or more SAN from one sanity roll: it makes the INT check, decides temporary/indefinite/permanent insanity, rolls 1D10 on the Bouts of Madness table, and writes the result (including any new phobia/mania) into the player's character card. Pass loss (defaults to the last SAN loss recorded by san.roll_sanity_check) and an optional cause.", parameters={"type": "object", "properties": {"player_name": {"type": "string", "description": "Room member's login username or character card name."}, "loss": {"type": "integer", "minimum": 0, "description": "SAN points lost in this single incident. Omit to use the last SAN check's loss."}, "cause": {"type": "string", "description": "What caused the madness, used for the card's madness record."}, "force": {"type": "boolean", "description": "Set true to force a madness roll even if loss is below 5 (e.g. keeper decision)."}, "bout_mode": {"type": "string", "enum": ["immediate", "summary"], "description": "Optional. immediate = 1D10 combat rounds (others present); summary = 1D10 hours (alone / montage). Defaults to immediate."}}, "required": ["player_name"]}, handler=execute_roll_madness)
ROLL_MADNESS_ALIAS_TOOL = AgentTool(name="sanity.roll_madness", description=ROLL_MADNESS_TOOL.description, parameters=ROLL_MADNESS_TOOL.parameters, handler=execute_roll_madness)
ROLL_DICE_TOOL = AgentTool(name="dice.roll", description="Roll generic dice such as 1d100. Set dark=true for a secret roll. Optional show_table summons the virtual dice table for this roll; player_roll lets the current player throw it (only when player-roll mode is enabled).", parameters={"type": "object", "properties": {"expression": {"type": "string"}, "dark": {"type": "boolean"}, "show_table": {"type": "boolean", "description": "Set true to summon the virtual dice table for this roll. Ignored in text-only animation mode."}, "player_roll": {"type": "boolean", "description": "Set true to let the current player throw the dice. Only honored when player-roll mode is enabled; otherwise the roll resolves immediately. Works in text, simple, and 3D animation modes."}}, "required": ["expression"]}, handler=execute_roll_dice)
ROLL_DICE_FUNCTION_TOOL = AgentTool(name="dice.roll_dice", description="Roll generic dice such as 1d100. Set dark=true for a secret roll.", parameters=ROLL_DICE_TOOL.parameters, handler=execute_roll_dice)
