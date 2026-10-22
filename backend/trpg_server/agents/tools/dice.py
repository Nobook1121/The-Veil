import logging
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.trigger import reveal_trigger_after_check
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import CHARACTER_SKILLS_FILE

logger = logging.getLogger(__name__)
DIFFICULTY_ALIASES = {"regular": "regular", "普通": "regular", "hard": "hard", "困难": "hard", "extreme": "extreme", "极难": "extreme"}
DIFFICULTY_LABELS = {"regular": "", "hard": "困难", "extreme": "极难"}
ATTRIBUTE_ALIASES = {"STR": "STR", "力量": "STR", "CON": "CON", "体质": "CON", "SIZ": "SIZ", "体型": "SIZ", "DEX": "DEX", "敏捷": "DEX", "APP": "APP", "外貌": "APP", "INT": "INT", "智力": "INT", "灵感": "INT", "灵感检定": "INT", "智力检定": "INT", "POW": "POW", "意志": "POW", "EDU": "EDU", "教育": "EDU", "LUC": "LUC", "幸运": "LUC", "AGE": "AGE", "年龄": "AGE"}
# 属性英文键到中文展示名的映射，用于把本地化展示写回检定结果。
ATTRIBUTE_LABELS = {"STR": "力量", "CON": "体质", "SIZ": "体型", "DEX": "敏捷", "APP": "外貌", "INT": "智力", "POW": "意志", "EDU": "教育", "LUC": "幸运", "AGE": "年龄"}
# 常见 COC7 技能英文键到中文名的回退映射（键统一小写）。
# 角色卡上若带有本地化名称则优先使用角色卡名称，这里仅处理传入英文键的情况。
SKILL_KEY_LABELS = {
    "stealth": "潜行", "spothidden": "侦察", "listen": "聆听", "track": "追踪", "lipread": "读唇", "libraryuse": "图书馆使用",
    "navigate": "导航", "computeruse": "计算机使用", "charm": "取悦", "fasttalk": "话术", "intimidate": "恐吓", "persuade": "说服",
    "psychology": "心理学", "languageown": "母语", "languageother": "外语", "fighting": "格斗", "firearms": "射击", "dodge": "闪避",
    "throw": "投掷", "demolitions": "爆破", "artillery": "炮术", "firstaid": "急救", "medicine": "医学", "psychoanalysis": "精神分析",
    "hypnosis": "催眠", "climb": "攀爬", "jump": "跳跃", "swim": "游泳", "diving": "潜水", "appraise": "估价", "anthropology": "人类学",
    "accounting": "会计", "law": "法律", "history": "历史", "archaeology": "考古学", "naturalworld": "博物学", "occult": "神秘学",
    "electronics": "电子学", "science": "科学", "disguise": "乔装", "survival": "生存", "artcraft": "技艺", "creditrating": "信用评级",
    "cthulhumythos": "克苏鲁神话", "locksmith": "开锁", "sleightofhand": "妙手", "driveauto": "汽车驾驶", "ride": "骑术",
}
LEVEL_LABELS = {"critical": "大成功", "extreme": "极难成功", "hard": "困难成功", "regular": "成功", "failure": "失败", "fumble": "大失败"}
# 大成功/大失败阈值默认值，保持 COC7 习惯：1 为大成功，96-100 为大失败。
DEFAULT_CRITICAL_THRESHOLD = 1
DEFAULT_FUMBLE_THRESHOLD = 96

def _as_int(value: Any, fallback: int = 0) -> int:
    try: return int(value)
    except (TypeError, ValueError): return fallback
def _threshold(target: int, difficulty: str) -> int:
    return target // 2 if difficulty == "hard" else target // 5 if difficulty == "extreme" else target
def _normalize_difficulty(value: Any) -> str:
    text = str(value or "regular").strip()
    return DIFFICULTY_ALIASES.get(text, DIFFICULTY_ALIASES.get(text.lower(), ""))
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
def _percentile_roll(roller: Callable[[int], int], bonus: int = 0, penalty: int = 0) -> tuple[int, list[int]]:
    if not bonus and not penalty: return max(1, min(100, int(roller(100)))), []
    units = int(roller(10)) % 10; needed = (bonus or penalty) + 1; tens: list[int] = []
    attempts = 0
    while len(tens) < needed and attempts < 100:
        attempts += 1
        value = (int(roller(10)) - 1) % 10
        if value not in tens: tens.append(value)
    # A deterministic test/random source may return the same tens forever;
    # still honour the rule by selecting the next distinct tens value.
    for value in range(10):
        if len(tens) >= needed: break
        if value not in tens: tens.append(value)
    candidates = [100 if ten == 0 and units == 0 else ten * 10 + units for ten in tens]
    return (min(candidates) if bonus else max(candidates)), candidates
def _active_members(context: Any) -> list[dict[str, Any]]:
    return [m for m in context.room_info().get("members", []) if isinstance(m, dict)]
def _find_member(player_name: str, context: Any) -> dict[str, Any] | None:
    expected = player_name.casefold()
    return next((m for m in _active_members(context) if m.get("is_active", True) is not False and m.get("status", "active") != "removed" and str(m.get("username") or "").casefold() == expected), None)
# 常见的中文技能名变体 -> 技能目录中的正式名称（大小写/字形差异的兼容）。
CHECK_NAME_ALIASES = {
    "侦查": "侦察", "观察": "侦察", "搜索": "侦察", "调查": "侦察",
    "倾听": "聆听", "图书馆": "图书馆使用", "驾驶": "汽车驾驶", "驾车": "汽车驾驶",
    "隐藏": "潜行", "躲藏": "潜行", "电脑使用": "计算机使用",
}
# COC7 技能目录缓存：(配置文件路径, mtime, 技能名集合)。
_CHECK_NAME_CACHE: tuple[str, float, set[str]] | None = None


def _skill_catalog_names() -> set[str]:
    """加载 COC7 技能目录，返回可接受的技能名集合（英文键 + 本地化中文名 + 专精）。

    技能目录是随包发布的参考数据（``data/character_skills.json``），结果按文件
    mtime 缓存，避免每次检定都读盘。
    """
    global _CHECK_NAME_CACHE
    path = CHARACTER_SKILLS_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return set()
    if _CHECK_NAME_CACHE is not None and _CHECK_NAME_CACHE[0] == str(path) and _CHECK_NAME_CACHE[1] == mtime:
        return _CHECK_NAME_CACHE[2]
    catalog = read_json(path, default={})
    names: set[str] = set()
    if isinstance(catalog, dict):
        locales = catalog.get("locales") if isinstance(catalog.get("locales"), dict) else {}
        locale = str(catalog.get("defaultLocale") or "zh-CN")
        labels = locales.get(locale) if isinstance(locales.get(locale), dict) else {}
        skills = catalog.get("skills") if isinstance(catalog.get("skills"), list) else []
        for item in skills:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if key:
                names.add(key.casefold())
            label = labels.get(str(item.get("labelKey") or ""))
            if label:
                names.add(str(label).casefold())
            specialties = item.get("specialties") if isinstance(item.get("specialties"), list) else []
            for specialty in specialties:
                if not isinstance(specialty, dict):
                    continue
                specialty_key = str(specialty.get("key") or "").strip()
                if specialty_key:
                    names.add(specialty_key.casefold())
                specialty_label = labels.get(str(specialty.get("labelKey") or ""))
                if specialty_label:
                    names.add(str(specialty_label).casefold())
    names.update(name.casefold() for name in SKILL_KEY_LABELS)
    names.update(label.casefold() for label in SKILL_KEY_LABELS.values())
    _CHECK_NAME_CACHE = (str(path), mtime, names)
    return names


def _is_known_check_name(check_name: str) -> bool:
    """判断检定名是否为 COC7 认可的属性、技能或专精（接受常见中文变体）。"""
    text = str(check_name or "").strip()
    if not text:
        return False
    if text.endswith("检定"):
        text = text[:-2].strip()
    if ATTRIBUTE_ALIASES.get(text) or ATTRIBUTE_ALIASES.get(text.upper()):
        return True
    text = CHECK_NAME_ALIASES.get(text, text)
    return text.casefold() in _skill_catalog_names()


def _lookup_check_value(card: dict[str, Any], check_name: str) -> int | None:
    normalized_name = str(check_name or "").strip()
    key = ATTRIBUTE_ALIASES.get(normalized_name) or ATTRIBUTE_ALIASES.get(normalized_name.upper())
    if not key and normalized_name.endswith("检定"):
        base_name = normalized_name[:-2].strip()
        key = ATTRIBUTE_ALIASES.get(base_name) or ATTRIBUTE_ALIASES.get(base_name.upper())
    attrs = card.get("attributes")
    if isinstance(attrs, dict) and key and attrs.get(key) is not None: return _as_int(attrs.get(key), -1)
    # 技能匹配同时接受原始名称、英文键与其本地化中文名（例如 stealth / 潜行）。
    localized = SKILL_KEY_LABELS.get(normalized_name.casefold(), normalized_name)
    expected = {normalized_name.casefold(), localized.casefold()}
    for skill in card.get("skills", []) if isinstance(card.get("skills"), list) else []:
        if isinstance(skill, dict) and any(str(skill.get(k) or "").casefold() in expected for k in ("name", "skillKey", "id")): return _as_int(skill.get("value"), -1)
    return None
def _format_room_check_summary(name: str, difficulty: str, roll: int, threshold: int, success: bool) -> str:
    return f"{DIFFICULTY_LABELS[difficulty]}{name} d%: [{roll}] = {roll} / {threshold} {'成功' if success else '失败'}"
def _threshold_arg(arguments: dict[str, Any], key: str, fallback: int) -> int:
    """读取可配置的大成功/大失败阈值，做范围保护后返回。"""
    value = _as_int(arguments.get(key), fallback)
    return max(0, min(100, value))
def _context_thresholds(context: Any) -> tuple[int, int]:
    """读取房间统一的大成功/大失败阈值。

    优先级：房规内配置 > 管理员设置页的全局默认值 > COC7 默认值（1/96）。
    """
    critical = fumble = None
    room_info = context.room_info() if hasattr(context, "room_info") else None
    if isinstance(room_info, dict):
        rules = room_info.get("house_rules")
        if isinstance(rules, dict):
            critical = _nullable_int(rules.get("dice_critical_threshold"))
            fumble = _nullable_int(rules.get("dice_fumble_threshold"))
    if critical is None or fumble is None:
        defaults = _admin_thresholds(getattr(context, "config_dir", None))
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


def _admin_thresholds(config_dir: Any) -> tuple[int, int]:
    """读取管理员设置页配置的骰娘默认阈值，读取失败时回退到 COC7 默认值。"""
    if not config_dir:
        return DEFAULT_CRITICAL_THRESHOLD, DEFAULT_FUMBLE_THRESHOLD
    try:
        from trpg_server.agents.config import load_ai_runtime_config

        config = load_ai_runtime_config(config_dir)
        return (
            max(0, min(100, int(config.dice_critical_threshold))),
            max(0, min(100, int(config.dice_fumble_threshold))),
        )
    except Exception:  # 配置缺失或损坏时不影响检定
        return DEFAULT_CRITICAL_THRESHOLD, DEFAULT_FUMBLE_THRESHOLD


def _merge_context_thresholds(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """在未显式传参时，用房间统一阈值补齐大成功/大失败阈值。"""
    if "critical_threshold" in arguments and "fumble_threshold" in arguments:
        return arguments
    critical, fumble = _context_thresholds(context)
    merged = dict(arguments)
    merged.setdefault("critical_threshold", critical)
    merged.setdefault("fumble_threshold", fumble)
    return merged
def _success_level(roll: int, target: int, critical_threshold: int = DEFAULT_CRITICAL_THRESHOLD, fumble_threshold: int = DEFAULT_FUMBLE_THRESHOLD) -> str:
    # 大成功优先；大失败仍要求本次检定失败（roll > target），与原 COC7 行为保持兼容。
    if roll <= critical_threshold: return "critical"
    if roll <= target // 5: return "extreme"
    if roll <= target // 2: return "hard"
    if roll <= target: return "regular"
    if roll >= fumble_threshold: return "fumble"
    return "failure"
def _level_suffix(level: str) -> str:
    """为大成功/大失败追加明确的文字标注，供聊天与 AI 上下文直接读取。"""
    return "（大成功）" if level == "critical" else "（大失败）" if level == "fumble" else ""
def _localize_check_label(name: str) -> str:
    """把英文键形式的属性/技能名转换为中文展示名。"""
    raw = str(name or "").strip()
    key = ATTRIBUTE_ALIASES.get(raw) or ATTRIBUTE_ALIASES.get(raw.upper())
    if key: return ATTRIBUTE_LABELS.get(key, raw)
    return SKILL_KEY_LABELS.get(raw.casefold(), raw)
def _resolve_check_display_name(card: dict[str, Any], check_name: str) -> str:
    """优先使用角色卡上的本地化技能名，避免检定结果里出现英文键。"""
    raw = str(check_name or "").strip()
    key = ATTRIBUTE_ALIASES.get(raw) or ATTRIBUTE_ALIASES.get(raw.upper())
    if key: return ATTRIBUTE_LABELS.get(key, raw)
    localized = SKILL_KEY_LABELS.get(raw.casefold(), raw)
    expected = {raw.casefold(), localized.casefold()}
    for skill in card.get("skills", []) if isinstance(card.get("skills"), list) else []:
        if not isinstance(skill, dict): continue
        candidates = {str(skill.get(field) or "").casefold() for field in ("name", "skillKey", "id")}
        if candidates & expected:
            return str(skill.get("name") or "").strip() or localized
    return localized
def _result_message(summary: str, tool_name: str, extra_metadata: dict[str, Any] | None = None, **metadata: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {"tool_name": tool_name, **metadata}
    if isinstance(extra_metadata, dict):
        merged.update(extra_metadata)
    return {"type": "dice", "sender_name": "骰娘", "avatar": "", "content": summary, "metadata": merged}


def _percentile_from_faces(
    faces: list[int] | tuple[int, ...],
    bonus: int = 0,
    penalty: int = 0,
) -> tuple[int, list[int]]:
    """由物理 d10 骰面（十位/个位/候选十位）推导百分骰结果。

    与 :func:`_percentile_roll` 使用同一套规则：个位固定，奖励骰取最小的
    十位组合，惩罚骰取最大的；重复的十位只保留一次，不足时按 0-9 顺序补齐。
    """
    values = [int(value) for value in faces]
    if len(values) < 2:
        raise ValueError("percentile roll requires at least two d10 faces")
    units = values[1] % 10
    if not bonus and not penalty:
        return (100 if values[0] % 10 == 0 and units == 0 else (values[0] % 10) * 10 + units), []
    needed = int(bonus) + int(penalty) + 1
    tens: list[int] = []
    for value in [values[0], *values[2:]]:
        value %= 10
        if value not in tens:
            tens.append(value)
    for value in range(10):
        if len(tens) >= needed:
            break
        if value not in tens:
            tens.append(value)
    candidates = [100 if ten == 0 and units == 0 else ten * 10 + units for ten in tens]
    return (min(candidates) if bonus else max(candidates)), candidates


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
    check_name = str(arguments.get("name") or arguments.get("skill") or "").strip()
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
    try:
        member = _find_member(player_name, context)
        card = member.get("character_card") if member else None
        if isinstance(card, dict):
            sanity = card.get("sanity") if isinstance(card.get("sanity"), dict) else {}
            state = member.get("character_state") if isinstance(member, dict) else {}
            state = state if isinstance(state, dict) else {}
            current = _as_int(state.get("current_san", card.get("currentSan", sanity.get("current", card.get("maxSan", 0)))), 0)
    except Exception:  # noqa: BLE001
        current = None
    return _plan_percentile(check_name="理智", difficulty=None, target=current, bonus=0, penalty=0)


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
    if bonus_dice and penalty_dice: return {"error": "bonus_dice and penalty_dice cannot both be used"}
    try: threshold = _adjust_threshold(_threshold(target, difficulty), arguments.get("adjustment", arguments.get("correction")))
    except ValueError as exc: return {"error": str(exc)}
    critical_threshold = _threshold_arg(arguments, "critical_threshold", DEFAULT_CRITICAL_THRESHOLD)
    fumble_threshold = _threshold_arg(arguments, "fumble_threshold", DEFAULT_FUMBLE_THRESHOLD)
    roll, candidates = (
        _percentile_from_faces(faces, bonus_dice, penalty_dice)
        if faces is not None
        else _percentile_roll(roller, bonus_dice, penalty_dice)
    )
    level = _success_level(roll, target, critical_threshold, fumble_threshold); success = (roll <= threshold or level == "critical") and level != "fumble"
    skill = _localize_check_label(arguments.get("skill") or "检定"); reason = str(arguments.get("reason") or ""); modifier = str(arguments.get("adjustment", arguments.get("correction")) or "").strip()
    summary = f"{skill}检定：1d100={roll}，目标{target}，{DIFFICULTY_LABELS[difficulty] or '普通'}难度阈值{threshold}，结果：{LEVEL_LABELS.get(level, level)}。"
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
    """房间成员检定。``faces`` 提供时使用物理骰面，不再调用随机数生成器。"""
    player_name = str(arguments.get("player_name") or arguments.get("playerName") or "").strip(); check_name = str(arguments.get("name") or arguments.get("skill") or "").strip()
    if not player_name: return {"error": "player_name is required"}
    if not check_name: return {"error": "name is required"}
    difficulty = _normalize_difficulty(arguments.get("difficulty"))
    if not difficulty: return {"error": "difficulty must be regular, hard, extreme, 普通, 困难, or 极难"}
    try:
        member = _find_member(player_name, context)
        if not member: return {"error": f"player {player_name} was not found in current room"}
        card = member.get("character_card")
        if not isinstance(card, dict): return {"error": f"player {player_name} has no bound character card"}
        base_target = _lookup_check_value(card, check_name)
        if base_target is None or base_target < 0:
            if not _is_known_check_name(check_name):
                return {"error": f"“{check_name}”不是 COC7 标准属性或技能目录中的技能，已拒绝该检定；请改用标准检定名（如 侦察/聆听/心理学/力量/敏捷）或角色卡上已有的技能。"}
            return {"error": f"{check_name} was not found on player {player_name}'s character card"}
        if base_target > 100: return {"error": f"{check_name} value must be between 0 and 100"}
        threshold = _adjust_threshold(_threshold(base_target, difficulty), arguments.get("adjustment", arguments.get("correction")))
        display_name = _resolve_check_display_name(card, check_name)
    except ValueError as exc: return {"error": str(exc)}
    bonus_dice = _dice_count(arguments, "bonus_dice", "bonus"); penalty_dice = _dice_count(arguments, "penalty_dice", "penalty")
    if bonus_dice and penalty_dice: return {"error": "bonus_dice and penalty_dice cannot both be used"}
    default_critical, default_fumble = _context_thresholds(context)
    critical_threshold = _threshold_arg(arguments, "critical_threshold", default_critical)
    fumble_threshold = _threshold_arg(arguments, "fumble_threshold", default_fumble)
    roll, candidates = (
        _percentile_from_faces(faces, bonus_dice, penalty_dice)
        if faces is not None
        else _percentile_roll(rng or (lambda sides: random.randint(1, sides)), bonus_dice, penalty_dice)
    )
    level = _success_level(roll, base_target, critical_threshold, fumble_threshold); success = (roll <= threshold or level == "critical") and level != "fumble"
    summary = _format_room_check_summary(display_name, difficulty, roll, threshold, success) + _level_suffix(level)
    modifier = str(arguments.get("adjustment", arguments.get("correction")) or "").strip()
    if candidates: summary += f" {'奖励骰' if bonus_dice else '惩罚骰'}（{', '.join(map(str, candidates))}），取{roll}。"
    result = {"player_name": player_name, "character_name": card.get("name"), "name": display_name, "roll": roll, "rolls": candidates or [roll], "base_target": base_target, "target": threshold, "difficulty": difficulty, "difficulty_label": DIFFICULTY_LABELS[difficulty], "adjustment": modifier, "bonus_dice": bonus_dice, "penalty_dice": penalty_dice, "success": success, "success_level": level if success else "failure", "raw_success_level": level, "critical": level == "critical", "fumble": level == "fumble", "critical_threshold": critical_threshold, "fumble_threshold": fumble_threshold, "summary": summary}
    result["visible_message"] = _result_message(
        summary,
        "check.roll_room_check",
        check_type="room",
        player_name=player_name,
        name=display_name,
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name=display_name,
                difficulty=difficulty,
                target=threshold,
                roller=player_name,
            )
        },
    )
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
    _log_roll_result(result, context, tool_name="check.roll_room_check", actor=player_name)
    return result

def _roll_sanity_value(expression: Any, roller: Callable[[int], int]) -> tuple[int, list[int]]:
    text = str(expression or "0").strip()
    sign = -1 if text.startswith("-") else 1
    if text[:1] in {"+", "-"}: text = text[1:]
    match = re.fullmatch(r"(\d+)d(\d+)", text.lower())
    if match:
        count, sides = int(match.group(1)), int(match.group(2))
        if count < 1 or count > 100 or sides < 2 or sides > 1000: raise ValueError("SAN dice expression is out of range")
        rolls = [int(roller(sides)) for _ in range(count)]
        return sign * sum(rolls), rolls
    if not re.fullmatch(r"\d+", text): raise ValueError("SAN change must be a number or dice expression such as 1d6")
    return sign * int(text), [int(text)]

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
    sanity = card.get("sanity") if isinstance(card.get("sanity"), dict) else {}
    current = _as_int(state.get("current_san", card.get("currentSan", card.get("current_san", sanity.get("current", card.get("maxSan", card.get("max_san", 0)))))), 0)
    maximum = _as_int(state.get("max_san", card.get("maxSan", card.get("max_san", sanity.get("max", current)))), current)
    roller = rng or (lambda sides: random.randint(1, sides))
    try:
        if faces is not None:
            roll, _candidates = _percentile_from_faces(faces)
            roll = max(1, min(100, int(roll)))
        else:
            roll = int(roller(100))
        success = roll <= current
        selected_expr = success_expr if success else failure_expr
        delta, detail_rolls = _roll_sanity_value(selected_expr, roller)
    except ValueError as exc: return {"error": str(exc)}
    new_san = max(0, min(maximum, current + delta))
    loss = max(0, -delta)
    flags = state.setdefault("mental_status", {})
    if not isinstance(flags, dict): flags = {}; state["mental_status"] = flags
    if loss >= 5: flags["temporaryInsanity"] = flags["temporary_insanity"] = True
    if current > 0 and loss * 5 >= current and loss > 0: flags["indefiniteInsanity"] = flags["indefinite_insanity"] = True
    if new_san <= 0: flags["permanentInsanity"] = flags["permanent_insanity"] = True
    state["current_san"] = new_san; state["max_san"] = maximum
    card["currentSan"] = new_san; card["current_san"] = new_san; card["mental_status"] = flags; card["status"] = flags
    card["temporary_insanity"] = bool(flags.get("temporaryInsanity")); card["indefinite_insanity"] = bool(flags.get("indefiniteInsanity")); card["permanent_insanity"] = bool(flags.get("permanentInsanity"))
    if context.room_dir:
        room_info = context.room_info()
        for room_member in room_info.get("members", []) if isinstance(room_info.get("members"), list) else []:
            if isinstance(room_member, dict) and str(room_member.get("username") or "").casefold() == player_name.casefold():
                room_member.clear(); room_member.update(member); break
        write_json_atomic(context.room_dir / "info.json", room_info)
    result_text = "成功" if success else "失败"
    summary = f"{player_name} 理智检定：1d100=[{roll}] / 当前 SAN {current}，{result_text}；理智变化 {selected_expr} = {delta:+d}，SAN {current}→{new_san}。"
    result = {"player_name": player_name, "roll": roll, "san": current, "current_san": new_san, "max_san": maximum, "success": success, "change": delta, "change_expression": selected_expr, "change_rolls": detail_rolls, "san_change": change, "temporary_insanity": bool(flags.get("temporaryInsanity")), "indefinite_insanity": bool(flags.get("indefiniteInsanity")), "permanent_insanity": bool(flags.get("permanentInsanity")), "summary": summary}
    result["visible_message"] = _result_message(
        summary,
        "san.roll_sanity_check",
        check_type="sanity",
        player_name=player_name,
        current_san=new_san,
        max_san=maximum,
        temporary_insanity=bool(flags.get("temporaryInsanity")),
        indefinite_insanity=bool(flags.get("indefiniteInsanity")),
        permanent_insanity=bool(flags.get("permanentInsanity")),
        extra_metadata={
            "dice_roll": _dice_roll_metadata(
                result,
                mode="percentile",
                check_name="理智",
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
ROLL_ROOM_CHECK_TOOL = AgentTool(name="check.roll_room_check", description="Resolve a current-room player's character card and roll a COC check. The result reports whether it is a critical success or fumble. Uses the room's configured critical/fumble thresholds unless overridden by arguments.", parameters={"type": "object", "properties": {"player_name": {"type": "string"}, "name": {"type": "string"}, "difficulty": {"type": "string"}, "adjustment": {"type": "string"}, "correction": {"type": "string"}, "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "critical_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "fumble_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "trigger_id": {"type": ["integer", "string"], "description": "Optional. When this check gates a scenario trigger, pass the trigger id so the trigger content is revealed in the same round if the check succeeds. Do not pass it when the trigger condition is not tied to this check."}}, "required": ["player_name", "name"]}, handler=execute_roll_room_check)
ROLL_SANITY_CHECK_TOOL = AgentTool(name="san.roll_sanity_check", description="Roll a player's SAN check and apply success/failure sanity change directly to the room character state.", parameters={"type": "object", "properties": {"player_name": {"type": "string"}, "san_change": {"type": "string", "description": "Success/failure change, e.g. 1d4/1d6 or +1d4/-1d6"}}, "required": ["player_name", "san_change"]}, handler=execute_roll_sanity_check)
ROLL_SANITY_CHECK_ALIAS_TOOL = AgentTool(name="sanity.roll_sanity_check", description=ROLL_SANITY_CHECK_TOOL.description, parameters=ROLL_SANITY_CHECK_TOOL.parameters, handler=execute_roll_sanity_check)
ROLL_DICE_TOOL = AgentTool(name="dice.roll", description="Roll generic dice such as 1d100. Set dark=true for a secret roll. Optional show_table summons the virtual dice table for this roll; player_roll lets the current player throw it (only when player-roll mode is enabled).", parameters={"type": "object", "properties": {"expression": {"type": "string"}, "dark": {"type": "boolean"}, "show_table": {"type": "boolean", "description": "Set true to summon the virtual dice table for this roll. Ignored in text-only animation mode."}, "player_roll": {"type": "boolean", "description": "Set true to let the current player throw the dice. Only honored when player-roll mode is enabled; otherwise the roll resolves immediately. Works in text, simple, and 3D animation modes."}}, "required": ["expression"]}, handler=execute_roll_dice)
ROLL_DICE_FUNCTION_TOOL = AgentTool(name="dice.roll_dice", description="Roll generic dice such as 1d100. Set dark=true for a secret roll.", parameters=ROLL_DICE_TOOL.parameters, handler=execute_roll_dice)
