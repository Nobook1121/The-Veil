import logging
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trpg_server.agents.tools.base import AgentTool
from trpg_server.agents.tools.trigger import reveal_trigger_after_check
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import CONFIG_DIR

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


def _skill_catalog_names(config_dir: Any = None) -> set[str]:
    """加载 COC7 技能目录，返回可接受的技能名集合（英文键 + 本地化中文名 + 专精）。

    结果按文件 mtime 缓存，避免每次检定都读盘。
    """
    global _CHECK_NAME_CACHE
    base = Path(config_dir) if config_dir else CONFIG_DIR
    path = base / "character_skills.json"
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


def _is_known_check_name(check_name: str, config_dir: Any = None) -> bool:
    """判断检定名是否为 COC7 认可的属性、技能或专精（接受常见中文变体）。"""
    text = str(check_name or "").strip()
    if not text:
        return False
    if text.endswith("检定"):
        text = text[:-2].strip()
    if ATTRIBUTE_ALIASES.get(text) or ATTRIBUTE_ALIASES.get(text.upper()):
        return True
    text = CHECK_NAME_ALIASES.get(text, text)
    return text.casefold() in _skill_catalog_names(config_dir)


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
def _result_message(summary: str, tool_name: str, **metadata: Any) -> dict[str, Any]:
    return {"type": "dice", "sender_name": "骰娘", "avatar": "", "content": summary, "metadata": {"tool_name": tool_name, **metadata}}


def _attach_trigger_reveal(result: dict[str, Any], arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """检定工具的可选能力：同一轮内揭示与该检定绑定的触发器。

    模型确认触发器 id 后，可把它随检定一起传入（``trigger_id``），工具按自己的
    检定结果决定是否揭示，省掉「先检定、再单独调用触发器工具」中间那次 AI 往返。
    未传 ``trigger_id`` 时原样返回，既有行为完全不变。
    """
    trigger_id = arguments.get("trigger_id")
    if trigger_id in (None, ""):
        return result
    revealed = reveal_trigger_after_check(trigger_id, bool(result.get("success")), context)
    if isinstance(revealed.get("direct_message"), dict):
        result["direct_message"] = revealed["direct_message"]
    if revealed.get("error"):
        result["trigger_error"] = revealed["error"]
    elif not revealed.get("triggered"):
        result["trigger_note"] = revealed.get("message")
    return result


def roll_coc_check(arguments: dict[str, Any], rng: Callable[[int], int] | None = None) -> dict[str, Any]:
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
    roll, candidates = _percentile_roll(roller, bonus_dice, penalty_dice); level = _success_level(roll, target, critical_threshold, fumble_threshold); success = (roll <= threshold or level == "critical") and level != "fumble"
    skill = _localize_check_label(arguments.get("skill") or "检定"); reason = str(arguments.get("reason") or ""); modifier = str(arguments.get("adjustment", arguments.get("correction")) or "").strip()
    summary = f"{skill}检定：1d100={roll}，目标{target}，{DIFFICULTY_LABELS[difficulty] or '普通'}难度阈值{threshold}，结果：{LEVEL_LABELS.get(level, level)}。"
    # Keep the compact /check-compatible summary stable; adjustment is exposed
    # in the structured result while bonus/penalty candidates are expanded.
    if candidates: summary += f" {'奖励骰' if bonus_dice else '惩罚骰'}（{', '.join(map(str, candidates))}），取{roll}。"
    if reason: summary += f" 原因：{reason}。"
    return {"skill": skill, "reason": reason, "roll": roll, "rolls": candidates or [roll], "target": target, "difficulty": difficulty, "threshold": threshold, "adjustment": modifier, "bonus_dice": bonus_dice, "penalty_dice": penalty_dice, "success": success, "success_level": level if success else "failure", "raw_success_level": level, "critical": level == "critical", "fumble": level == "fumble", "critical_threshold": critical_threshold, "fumble_threshold": fumble_threshold, "summary": summary, "visible_message": _result_message(summary, "dice.roll_coc_check", check_type="coc")}
def execute_roll_coc_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _attach_trigger_reveal(roll_coc_check(_merge_context_thresholds(arguments, context)), arguments, context)

def roll_room_check(arguments: dict[str, Any], context: Any, rng: Callable[[int], int] | None = None) -> dict[str, Any]:
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
            if not _is_known_check_name(check_name, getattr(context, "config_dir", None)):
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
    roll, candidates = _percentile_roll(rng or (lambda sides: random.randint(1, sides)), bonus_dice, penalty_dice)
    level = _success_level(roll, base_target, critical_threshold, fumble_threshold); success = (roll <= threshold or level == "critical") and level != "fumble"
    summary = _format_room_check_summary(display_name, difficulty, roll, threshold, success) + _level_suffix(level)
    modifier = str(arguments.get("adjustment", arguments.get("correction")) or "").strip()
    if candidates: summary += f" {'奖励骰' if bonus_dice else '惩罚骰'}（{', '.join(map(str, candidates))}），取{roll}。"
    return {"player_name": player_name, "character_name": card.get("name"), "name": display_name, "roll": roll, "rolls": candidates or [roll], "base_target": base_target, "target": threshold, "difficulty": difficulty, "difficulty_label": DIFFICULTY_LABELS[difficulty], "adjustment": modifier, "bonus_dice": bonus_dice, "penalty_dice": penalty_dice, "success": success, "success_level": level if success else "failure", "raw_success_level": level, "critical": level == "critical", "fumble": level == "fumble", "critical_threshold": critical_threshold, "fumble_threshold": fumble_threshold, "summary": summary, "visible_message": _result_message(summary, "check.roll_room_check", check_type="room", player_name=player_name, name=display_name)}
def execute_roll_room_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return _attach_trigger_reveal(roll_room_check(arguments, context), arguments, context)

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

def roll_sanity_check(arguments: dict[str, Any], context: Any, rng: Callable[[int], int] | None = None) -> dict[str, Any]:
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
        roll = int(roller(100)); success = roll <= current
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
    return {"player_name": player_name, "roll": roll, "san": current, "current_san": new_san, "max_san": maximum, "success": success, "change": delta, "change_expression": selected_expr, "change_rolls": detail_rolls, "san_change": change, "temporary_insanity": bool(flags.get("temporaryInsanity")), "indefinite_insanity": bool(flags.get("indefiniteInsanity")), "permanent_insanity": bool(flags.get("permanentInsanity")), "summary": summary, "visible_message": {**_result_message(summary, "san.roll_sanity_check", check_type="sanity", player_name=player_name), "metadata": {"tool_name": "san.roll_sanity_check", "check_type": "sanity", "player_name": player_name, "current_san": new_san, "max_san": maximum, "temporary_insanity": bool(flags.get("temporaryInsanity")), "indefinite_insanity": bool(flags.get("indefiniteInsanity")), "permanent_insanity": bool(flags.get("permanentInsanity"))}}}

def execute_roll_sanity_check(arguments: dict[str, Any], context: Any) -> dict[str, Any]: return roll_sanity_check(arguments, context)

def roll_dice(arguments: dict[str, Any], rng: Callable[[int], int] | None = None) -> dict[str, Any]:
    expression = str(arguments.get("expression") or arguments.get("dice") or "1d100").strip().lower(); match = re.fullmatch(r"(\d+)d(\d+)", expression)
    if not match: return {"error": "dice expression must look like 1d6"}
    count, sides = int(match.group(1)), int(match.group(2))
    if count < 1 or count > 100 or sides < 2 or sides > 1000: return {"error": "dice count must be 1-100 and sides 2-1000"}
    rolls = [int((rng or (lambda n: random.randint(1, n)))(sides)) for _ in range(count)]; total = sum(rolls); summary = f"投掷 {expression}：{' + '.join(map(str, rolls))}" + (f" = {total}" if count > 1 else "")
    result = {"expression": expression, "rolls": rolls, "total": total, "summary": summary}
    if not arguments.get("dark", arguments.get("secret", False)): result["visible_message"] = _result_message(summary, "dice.roll", check_type="dice")
    return result
def execute_roll_dice(arguments: dict[str, Any], context: Any) -> dict[str, Any]: return roll_dice(arguments)

ROLL_COC_CHECK_TOOL = AgentTool(name="dice.roll_coc_check", description="Roll a real backend COC7 percentile check. The result reports whether it is a critical success or fumble. Uses the room's configured critical/fumble thresholds unless overridden by arguments.", parameters={"type": "object", "properties": {"character_name": {"type": "string"}, "skill": {"type": "string"}, "target": {"type": "integer", "minimum": 1, "maximum": 100}, "difficulty": {"type": "string"}, "adjustment": {"type": "string"}, "correction": {"type": "string"}, "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "critical_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "fumble_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "reason": {"type": "string"}, "trigger_id": {"type": ["integer", "string"], "description": "Optional. When this check gates a scenario trigger, pass the trigger id so the trigger content is revealed in the same round if the check succeeds. Do not pass it when the trigger condition is not tied to this check."}}, "required": ["skill", "target"]}, handler=execute_roll_coc_check)
ROLL_ROOM_CHECK_TOOL = AgentTool(name="check.roll_room_check", description="Resolve a current-room player's character card and roll a COC check. The result reports whether it is a critical success or fumble. Uses the room's configured critical/fumble thresholds unless overridden by arguments.", parameters={"type": "object", "properties": {"player_name": {"type": "string"}, "name": {"type": "string"}, "difficulty": {"type": "string"}, "adjustment": {"type": "string"}, "correction": {"type": "string"}, "bonus_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "penalty_dice": {"type": "integer", "minimum": 0, "maximum": 10}, "critical_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "fumble_threshold": {"type": "integer", "minimum": 0, "maximum": 100}, "trigger_id": {"type": ["integer", "string"], "description": "Optional. When this check gates a scenario trigger, pass the trigger id so the trigger content is revealed in the same round if the check succeeds. Do not pass it when the trigger condition is not tied to this check."}}, "required": ["player_name", "name"]}, handler=execute_roll_room_check)
ROLL_SANITY_CHECK_TOOL = AgentTool(name="san.roll_sanity_check", description="Roll a player's SAN check and apply success/failure sanity change directly to the room character state.", parameters={"type": "object", "properties": {"player_name": {"type": "string"}, "san_change": {"type": "string", "description": "Success/failure change, e.g. 1d4/1d6 or +1d4/-1d6"}}, "required": ["player_name", "san_change"]}, handler=execute_roll_sanity_check)
ROLL_SANITY_CHECK_ALIAS_TOOL = AgentTool(name="sanity.roll_sanity_check", description=ROLL_SANITY_CHECK_TOOL.description, parameters=ROLL_SANITY_CHECK_TOOL.parameters, handler=execute_roll_sanity_check)
ROLL_DICE_TOOL = AgentTool(name="dice.roll", description="Roll generic dice such as 1d100. Set dark=true for a secret roll.", parameters={"type": "object", "properties": {"expression": {"type": "string"}, "dark": {"type": "boolean"}}, "required": ["expression"]}, handler=execute_roll_dice)
ROLL_DICE_FUNCTION_TOOL = AgentTool(name="dice.roll_dice", description="Roll generic dice such as 1d100. Set dark=true for a secret roll.", parameters=ROLL_DICE_TOOL.parameters, handler=execute_roll_dice)
