import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, current_app, request, session

from trpg_server import room_interlude, weapon_catalog
from trpg_server.config import load_settings
from trpg_server.immersive.config import AUDIO_BLOCKS, MUSIC_BLOCKS, MUSIC_BLOCK_PRIORITY
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media_defaults import normalize_avatar
from trpg_server.logging_config import log_access_denied, log_user_action, user_action_text
from trpg_server.room_card_occupancy import card_binding_lock, card_id_of, find_card_occupancy
from trpg_server.room_log import ARCHIVE_LOG_LIMIT, load_room_log, log_room_event
from trpg_server.responses import error_response, success_response
from trpg_server.ruleset import RulesetSelectionError, builtin_registry, default_ruleset_id
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_mode import (
    MODE_PRESETS,
    ROOM_MODE_FREEFORM,
    ROOM_MODE_SCRIPTED,
    ROOM_MODES,
    SCENARIO_BINDING_REQUIRED,
    accepts_scenario,
    has_bound_scenario,
    normalize_room_mode,
)
from trpg_server.room_usage import load_room_usage
from trpg_server.role_config import load_roles
from trpg_server.world_override import evidence_entries, rate_limit_snapshot, resolve_policy
from trpg_server.security import get_user_manager, is_socket_user_online, is_user_in_room, safe_join
from trpg_server.settings import CONFIG_DIR, ROOMS_DIR, SCENARIOS_DIR, ROOM_ARCHIVES_DIR, CHARACTERS_DIR, HISTORY_DIR
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.dice_settings import DICE_ANIMATION_LEVELS, DICE_REROLL_MODES, resolve_dice_settings
from trpg_server.agents.room_state import save_room_state
from trpg_server.agents.versioning import migrate_room_binding
from trpg_server.agents.trigger_system import find_trigger_definition, record_trigger, validate_trigger
from trpg_server.scenario_store import load_scenario_by_id

bp = Blueprint("rooms", __name__)
logger = logging.getLogger(__name__)


def _fallback_ruleset_id() -> str:
    """当前默认插件 id；无可用插件时返回空串，避免读取旧房间快照时抛错。"""
    try:
        return default_ruleset_id()
    except RuntimeError:
        return ""


USER_ROOM_LIMIT = 3
ROOM_ROLE_OWNER = "owner"
ROOM_ROLE_ADMIN = "admin"
ROOM_ROLE_MEMBER = "member"
ROOM_MEMBER_ACTIVE = "active"
ROOM_MEMBER_REMOVED = "removed"
ROOM_VISIBILITY_PRIVATE = "private"
ROOM_VISIBILITY_PUBLIC = "public"
ROOM_VISIBILITIES = {ROOM_VISIBILITY_PRIVATE, ROOM_VISIBILITY_PUBLIC}

# Structured per-room house rules. Only rules flagged ``ai_prompt`` are ever
# injected into the KP prompt (and only when active), keeping prompt tokens low;
# future non-AI rules can be registered here without prompt cost.
HOUSE_RULE_DEFINITIONS = {
    "action_suggestions_enabled": {"type": "boolean", "default": False, "ai_prompt": True},
    # 骰娘大成功/大失败阈值（全房间统一）。留空表示沿用管理员设置页配置的全局默认值。
    "dice_critical_threshold": {"type": "nullable_integer", "default": None, "min": 0, "max": 100, "ai_prompt": False},
    "dice_fumble_threshold": {"type": "nullable_integer", "default": None, "min": 0, "max": 100, "ai_prompt": False},
    # 骰子动画档位与玩家投掷（留空表示继承管理员默认值）。两者相互独立：
    # 纯文本档位同样可以开启玩家投掷。
    "dice_animation_level": {"type": "nullable_enum", "default": None, "values": list(DICE_ANIMATION_LEVELS), "ai_prompt": False},
    "player_dice_roll": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    # 玩家投掷的重投配置（留空表示继承管理员默认值）。
    "dice_reroll_mode": {"type": "nullable_enum", "default": None, "values": list(DICE_REROLL_MODES), "ai_prompt": False},
    "dice_reroll_limit": {"type": "nullable_integer", "default": None, "min": 1, "max": 20, "ai_prompt": False},
    # 房间背景音乐播放列表（空列表表示该房间不播放背景音乐）。
    # 每首可以是音乐库站内地址或 http(s) 外链，最多 20 首。
    "bgm_urls": {"type": "url_list", "default": [], "max_items": 20, "ai_prompt": False},
    # 播放模式：列表循环 / 单曲循环 / 随机播放。
    "bgm_mode": {"type": "enum", "default": "list", "values": ["list", "single", "shuffle"], "ai_prompt": False},
    # 实时沉浸：房间级开关。null 表示跟随管理员默认；房间只能「关」，不能越过站点总开关重开。
    "immersive_music_enabled": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    "immersive_music_auto_switch": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    # 旧版单值音乐引擎：仅保留读取迁移，新房间请使用 immersive_audio_blocks。
    "immersive_music_engine": {
        "type": "nullable_enum",
        "default": None,
        "values": ["external", "procedural", "off"],
        "ai_prompt": False,
    },
    # 音频块组合：环境音可叠加，配乐四选一；候选项来自管理员允许清单。
    # null 表示未配置（沿用管理员默认组合）。
    "immersive_audio_blocks": {
        "type": "audio_blocks",
        "default": None,
        "values": list(AUDIO_BLOCKS),
        "ai_prompt": False,
    },
    "immersive_illustration_enabled": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    "immersive_illustration_auto": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    # 房间只能把配图触发档位收得更紧：important（仅重要场景）/ off；null 表示跟随。
    "immersive_illustration_trigger": {
        "type": "nullable_enum",
        "default": None,
        "values": ["important", "off"],
        "ai_prompt": False,
    },
    # 剧本配图缓存：房间只能收紧（关闭/不优先），不能越过站点的「记录/优先复用」开关。
    "immersive_scenario_record": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    "immersive_scenario_prefer": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    # 技能基础值覆盖表：键为技能键（带专精时为 `skillKey.specialtyKey`），值为 0-99 整数。
    # 留空表示沿用下一优先级（用户个性化 > 管理员设置 > 技能目录默认值）。
    "skill_bases": {"type": "skill_bases", "default": {}, "ai_prompt": False},
    # 图谱运行档位（§12.5）：留空表示跟随默认（profile=graph / guard=enforce）。
    # narrative_enabled=false 是本房间的 kill switch，立即退回旧编排。
    "narrative_enabled": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    "narrative_profile": {"type": "nullable_enum", "default": None, "values": ["graph", "degraded"], "ai_prompt": False},
    "narrative_guard": {"type": "nullable_enum", "default": None, "values": ["off", "shadow", "enforce"], "ai_prompt": False},
    # 世界意志直执行：跳过剧本拓扑/战斗等判定。站点管理员永远可用（开关无效），
    # 房主与房管默认可用；user_ids 里的普通成员额外获得权限。
    "world_override_enabled": {"type": "boolean", "default": True, "ai_prompt": False},
    "world_override_user_ids": {"type": "user_ids", "default": [], "max_items": 50, "ai_prompt": False},
    # 世界意志限频的房间级收紧（null 表示沿用站点 [world_override] 设置）。
    # 房间只能比站点更严格：冷却取较大值，每小时次数取较小值。
    "world_override_cooldown_seconds": {"type": "nullable_integer", "default": None, "min": 0, "max": 3600, "ai_prompt": False},
    "world_override_hourly_limit": {"type": "nullable_integer", "default": None, "min": 0, "max": 10000, "ai_prompt": False},
    # 归档时是否把本房间内获得的物品 / 武器写回持久角色卡（默认开启）。
    "rule_persist_on_archive": {"type": "boolean", "default": True, "ai_prompt": False},
    # CoC7 章节级规则开关（默认全部关闭，关闭时任何既有行为都不变）。
    # 社交技能难度：按对方社交/心理学与态度建议难度，见 ruleset/coc7/social.py。
    "coc7_social_difficulty": {"type": "boolean", "default": False, "ai_prompt": False},
    # 组合技能：一次掷骰对多个技能比较（全部成功 / 任一成功）。
    "coc7_combined_checks": {"type": "boolean", "default": False, "ai_prompt": False},
    # 团体幸运：由在场幸运最低者代掷。
    "coc7_group_luck": {"type": "boolean", "default": False, "ai_prompt": False},
    # 信用评级开支：消费水平、现金/资产扣减与幕间 CR 变动。
    "coc7_economy": {"type": "boolean", "default": False, "ai_prompt": False},
    # 熟人：创建熟人与熟人协助（房规 1 奖励骰）。
    "coc7_contacts": {"type": "boolean", "default": False, "ai_prompt": False},
    # 年龄调整：建卡年龄修正与老化 / EDU 增强检定。
    "coc7_age_adjustments": {"type": "boolean", "default": False, "ai_prompt": False},
    # 战斗规则留白配置（报告 §9）。这些都不是 CoC7 核心规则，默认值取最保守解释。
    # 突袭：bonus_die = 未准备目标近战给攻击者 1 奖励骰；auto_success = 自动成功（除大失败）。
    "combat_surprise_model": {
        "type": "enum",
        "default": "bonus_die",
        "values": ["bonus_die", "auto_success"],
        "ai_prompt": False,
    },
    # 准备武器耗时：规则书未规定（p96 只说已准备武器 DEX+50）。
    "combat_ready_weapon_cost": {
        "type": "enum",
        "default": "free",
        "values": ["free", "action", "round"],
        "ai_prompt": False,
    },
    # 施法打断：keeper = KP 裁定（规则书原文 p206）；any_damage = 当轮受伤即打断；never = 不打断。
    "combat_cast_interrupt": {
        "type": "enum",
        "default": "keeper",
        "values": ["keeper", "any_damage", "never"],
        "ai_prompt": False,
    },
    "combat_cast_defense_allowed": {"type": "boolean", "default": False, "ai_prompt": False},
    # 重伤是否限制行动：规则书未规定，默认不额外惩罚。
    "combat_major_wound_penalty": {
        "type": "enum",
        "default": "none",
        "values": ["none", "cannot_act"],
        "ai_prompt": False,
    },
    # 盾牌：核心无盾牌条目（p371-374），默认禁用独立格挡；counter_alias 仅把盾牌视为反击描述。
    "combat_shield_action": {
        "type": "enum",
        "default": "disabled",
        "values": ["disabled", "counter_alias"],
        "ai_prompt": False,
    },
    # 房规：DEX 达到阈值给 1 次额外行动。默认关闭；核心规则没有此条（p86）。
    "combat_dex_extra_action_threshold": {
        "type": "nullable_integer",
        "default": None,
        "min": 1,
        "max": 100,
        "ai_prompt": False,
    },
    "combat_dying_check": {"type": "boolean", "default": True, "ai_prompt": False},
}
DEFAULT_ROOM_HOUSE_RULES = {key: spec["default"] for key, spec in HOUSE_RULE_DEFINITIONS.items()}


def _get_rooms_dir():
    return current_app.config.get("ROOMS_DIR", ROOMS_DIR)


def _get_archives_dir():
    return current_app.config.get("ROOM_ARCHIVES_DIR", ROOM_ARCHIVES_DIR)


def _get_config_dir():
    return current_app.config.get("CONFIG_DIR", CONFIG_DIR)


def _get_ai_platform_dir():
    return current_app.config.get("AI_PLATFORM_DIR", _get_config_dir() / "aiplatform")


def _get_kp_prompt_file():
    return current_app.config.get("KP_PROMPT_FILE", _get_config_dir() / "roles" / "kp.md")


def _get_role_config_file():
    return current_app.config.get("ROLE_CONFIG_FILE", _get_config_dir() / "roles" / "roles.json")


def _autosave_node_limit():
    # schema 已收敛到 [1, 50]，默认 3。
    return int(load_settings(_get_config_dir()).data["autosave"]["max_nodes"])


def _autosave_settings():
    """读取生效配置的 [autosave] 段，供服务端后台调度使用（schema 保证键齐全）。"""
    section = load_settings(_get_config_dir()).data["autosave"]
    return {"enabled": bool(section["enabled"]), "interval": int(section["interval"])}


def _timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _require_login():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    return None


def _is_elevated():
    return is_elevated()


def _denied(room_ref=None):
    log_access_denied(
        logger,
        user_action_text(session.get("username"), "访问房间被拒绝"),
        用户ID=session.get("user_id"),
        房间ID=room_ref,
    )
    return error_response("Permission denied", 403, "Permission denied")


def _iter_room_dirs():
    rooms_dir = _get_rooms_dir()
    if not rooms_dir.exists():
        return []
    return [path for path in rooms_dir.iterdir() if path.is_dir()]


def _read_room(room_dir):
    return read_json(room_dir / "info.json", default={})


def _write_room(room_dir, info):
    info["updated_at"] = _timestamp()
    write_json_atomic(room_dir / "info.json", info)


def _card_in_use_response(conflict):
    """角色卡已被其它活跃房间占用时的统一拒绝响应。

    带上占用房间与角色卡名，前端据此给出「换卡 / 去那个房间退出」的具体提示，
    而不是只显示一句通用失败。
    """
    room_name = conflict.get("room_name") or conflict.get("room_id") or "另一个房间"
    card_name = conflict.get("card_name") or "该角色卡"
    return error_response(
        f"{card_name}正在《{room_name}》中使用，请更换角色卡或先退出那个房间",
        409,
        "character_card_in_use",
        conflict=conflict,
    )


def _find_card_conflict(character_card, *, exclude_room_id=None):
    return find_card_occupancy(
        _get_rooms_dir(),
        card_id_of(character_card),
        exclude_room_id=exclude_room_id,
    )


def _pending_room_changes(member):
    """成员在房间内尚未归档、会随归档写回的改动数量；0 表示没有。

    与归档写回的判断保持一致：优先看统一队列 ``pending_changes``，
    旧房间再兼容 ``item_changes`` 与 ``pending_persist`` 标记。
    """
    from trpg_server import room_rules

    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    queued = room_rules.pending_changes(state)
    if queued:
        return len(queued)
    legacy_items = [
        record
        for record in (state.get("item_changes") or [])
        if isinstance(record, dict) and not record.get("persisted")
    ]
    if legacy_items:
        return len(legacy_items)
    return 1 if state.get("pending_persist") else 0


def _emit_room_members_changed(room_id):
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_members_changed", {"room_id": str(room_id)}, room=str(room_id))


def _joined_at_timestamp(member):
    """解析成员 ``joined_at``；缺失或格式无法识别时返回 ``None``。"""
    raw = str(member.get("joined_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _earliest_active_member(info, exclude_user_id):
    """按加入时间挑选最早的活跃成员，用于房主自动移交。

    以成员记录的 ``joined_at`` 为准；重新加入房间只会追加 ``rejoined_at``，
    不会覆盖 ``joined_at``。字段缺失/无法解析的成员排在可解析成员之后，
    同一时间按成员列表顺序、再按 user_id 兜底，确保结果稳定可复现。
    """
    candidates = [
        (index, member)
        for index, member in enumerate(info.get("members", []))
        if _is_active_member(member) and str(member.get("user_id")) != str(exclude_user_id)
    ]
    if not candidates:
        return None

    def sort_key(item):
        index, member = item
        stamp = _joined_at_timestamp(member)
        if stamp is None:
            return (1, 0.0, index, str(member.get("user_id") or ""))
        return (0, stamp, index, str(member.get("user_id") or ""))

    return min(candidates, key=sort_key)[1]


def _assign_room_owner(info, new_owner, previous_user_id=None):
    """把房主身份落到 ``new_owner``，并把原房主降为普通成员。"""
    new_user_id = str(new_owner.get("user_id"))
    for member in info.get("members", []):
        if str(member.get("user_id")) == new_user_id:
            member["room_role"] = ROOM_ROLE_OWNER
            member["status"] = ROOM_MEMBER_ACTIVE
            member["is_active"] = True
        elif previous_user_id is not None and str(member.get("user_id")) == str(previous_user_id):
            if member.get("room_role") == ROOM_ROLE_OWNER:
                member["room_role"] = ROOM_ROLE_MEMBER
    info["creator_id"] = new_user_id
    info["creator_name"] = str(new_owner.get("username") or "")


def _global_hints_enabled():
    """Global admin switch. When off, no room may enable action suggestions."""
    return bool(load_ai_runtime_config(_get_config_dir()).show_ai_hints)


def _room_house_rules(info):
    """Normalize stored house rules against ``HOUSE_RULE_DEFINITIONS``.

    Missing keys fall back to defaults and wrong types are coerced, so a room
    written by an older version always yields a complete, well-typed dict.
    """
    stored = info.get("house_rules") if isinstance(info, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    # 旧版房间只存了单曲 bgm_url：升级后迁移为列表，避免背景音乐丢失。
    legacy_bgm_url = _normalize_bgm_url(stored.get("bgm_url", ""))
    normalized = {}
    for key, spec in HOUSE_RULE_DEFINITIONS.items():
        value = stored.get(key, spec["default"])
        if spec["type"] == "boolean":
            value = bool(value)
        elif spec["type"] == "nullable_boolean":
            value = value if isinstance(value, bool) else None
        elif spec["type"] == "nullable_enum":
            candidate = str(value).strip().lower() if value is not None else ""
            value = candidate if candidate in spec.get("values", []) else None
        elif spec["type"] == "nullable_integer":
            value = _normalize_nullable_int(value, spec.get("min", 0), spec.get("max", 100))
        elif spec["type"] == "audio_blocks":
            value = _normalize_audio_blocks(value)
        elif spec["type"] == "enum":
            candidate = str(value).strip().lower() if value is not None else ""
            value = candidate if candidate in spec.get("values", []) else spec["default"]
        elif spec["type"] == "url":
            value = _normalize_bgm_url(value)
        elif spec["type"] == "url_list":
            value = _normalize_bgm_url_list(value, spec.get("max_items", 20))
            if not value and key == "bgm_urls" and legacy_bgm_url:
                value = [legacy_bgm_url]
        elif spec["type"] == "skill_bases":
            value = _normalize_skill_bases(value)
        elif spec["type"] == "user_ids":
            value = _normalize_user_ids(value, spec.get("max_items", 50))
        normalized[key] = value
    return normalized


def _normalize_user_ids(value, max_items=50):
    """房规白名单：只保留非空字符串用户 ID，去重并限制数量。"""
    if not isinstance(value, list):
        return []
    seen = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= max_items:
            break
    return seen


def world_override_allowed(info, user_id=None, role=None):
    """世界意志直执行权限：站点管理员恒可用，房主/房管按房规开关，白名单成员额外放行。"""
    mode = normalize_room_mode(info)
    # 世界意志只依赖 AI KP：无 AI 的房间不可用，无剧本房间（freeform）同样可用。
    if not mode["ai_kp_enabled"]:
        return False
    role_value = str(role or session.get("role") or "USER").upper()
    if role_value in {"ADMIN", "OWNER"} or _is_elevated():
        return True
    rules = _room_house_rules(info)
    if not rules.get("world_override_enabled", True):
        return False
    uid = str(user_id if user_id is not None else session.get("user_id") or "")
    if not uid:
        return False
    if uid in set(rules.get("world_override_user_ids") or []):
        return True
    member = _find_member(info, user_id=uid, active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


_BGM_LOCAL_PREFIXES = ("/assets/bgm/builtin/", "/assets/bgm/library/")
_BGM_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".webm", ".opus"}


def _is_local_bgm_path(url):
    """判断是否为音乐库内的站内相对地址（内置目录或管理员上传）。"""

    lowered = url.lower()
    prefix = next((item for item in _BGM_LOCAL_PREFIXES if lowered.startswith(item)), None)
    if not prefix:
        return False
    filename = url[len(prefix):]
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return False
    return Path(filename).suffix.lower() in _BGM_AUDIO_SUFFIXES


def _normalize_bgm_url(value):
    """接受 http/https 外链或音乐库内的站内地址；非法值统一回落为空字符串。"""

    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > 2048:
        return ""
    if _is_local_bgm_path(url):
        return url
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return ""
    return url


def _normalize_bgm_url_list(value, limit=20):
    """把背景音乐列表规范化为去重后的地址数组。

    读取旧数据时宽松处理：非字符串或非法地址直接跳过，重复地址只保留一次。
    """

    if not isinstance(value, list):
        return []
    urls = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        url = _normalize_bgm_url(item)
        if not url or url in seen:
            continue
        urls.append(url)
        seen.add(url)
        if len(urls) >= limit:
            break
    return urls


def _normalize_skill_bases(value):
    """把房规中的技能基础值覆盖表规范化：键为非空字符串，值为 0-99 整数。

    非法键值直接丢弃（等同「未填写」），从而回退到下一优先级。
    """
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or raw_value is None or raw_value == "":
            continue
        try:
            number = int(raw_value)
        except (TypeError, ValueError):
            continue
        normalized[key] = max(0, min(99, number))
    return normalized


def _normalize_nullable_int(value, minimum, maximum):
    """把房规中的可空整数字段规范化；空值表示沿用全局默认值。"""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _normalize_audio_blocks(value):
    """规范化房间音频块：去重、过滤未知值，并保证最多一个配乐块。"""
    if not isinstance(value, list):
        return None
    blocks = []
    for item in value:
        block = str(item or "").strip().lower()
        if block in AUDIO_BLOCKS and block not in blocks:
            blocks.append(block)
    music = [block for block in blocks if block in MUSIC_BLOCKS]
    if len(music) > 1:
        keep = next(block for block in MUSIC_BLOCK_PRIORITY if block in music)
        blocks = [block for block in blocks if block not in MUSIC_BLOCKS]
        blocks.append(keep)
    return blocks


def _admin_dice_thresholds():
    """管理员设置页配置的骰娘默认阈值（general.toml 的 [ai] 段）。"""
    config = load_ai_runtime_config(_get_config_dir())
    return {"critical": config.dice_critical_threshold, "fumble": config.dice_fumble_threshold}


def _room_dice_thresholds(info):
    """房间生效阈值：房规内配置优先，未配置则回退到管理员默认值。"""
    rules = _room_house_rules(info)
    defaults = _admin_dice_thresholds()
    critical = rules.get("dice_critical_threshold")
    fumble = rules.get("dice_fumble_threshold")
    return {
        "critical": defaults["critical"] if critical is None else critical,
        "fumble": defaults["fumble"] if fumble is None else fumble,
    }


def _room_dice_animation(info):
    """房间生效的骰子动画档位与玩家投掷开关（房规优先，管理员默认兜底）。"""
    return resolve_dice_settings(_get_config_dir(), _room_house_rules(info))


def _room_dir(room_id):
    return safe_join(_get_rooms_dir(), room_id)


def _find_room(room_id):
    return find_room(room_id)


def _find_room_by_code(room_code):
    normalized = str(room_code or "").strip().upper()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if info.get("room_code") == normalized:
            return room_dir, info
    return None, None


def _new_invite_token():
    """生成可轮换的房间邀请令牌（与固定的房间码分离，便于「刷新」后旧链接失效）。"""
    return secrets.token_urlsafe(16)


def _find_room_by_invite_token(invite_token):
    normalized = str(invite_token or "").strip()
    if not normalized:
        return None, None
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        token = str(info.get("invite_token") or "")
        if token and secrets.compare_digest(token, normalized):
            return room_dir, info
    return None, None


def _ensure_invite_token(room_dir, info):
    """读取房间的邀请令牌；老房间缺失时惰性补发并落盘。"""
    token = str(info.get("invite_token") or "")
    if token:
        return token
    token = _new_invite_token()
    info["invite_token"] = token
    _write_room(room_dir, info)
    return token


def _normalize_room_name(room_name):
    return str(room_name or "").strip().casefold()


def _find_room_by_name(room_name):
    normalized = _normalize_room_name(room_name)
    if not normalized:
        return None, None
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if _normalize_room_name(info.get("name")) == normalized:
            return room_dir, info
    return None, None


def _room_name_exists(room_name):
    room_dir, _ = _find_room_by_name(room_name)
    return room_dir is not None


def _can_access(info):
    return can_access(info, session.get("user_id"), session.get("role", "USER"))


def _can_manage(info):
    if _is_elevated() or str(info.get("creator_id")) == str(session.get("user_id")):
        return True
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _room_usage_for(info, room_dir):
    """只有房主/房间管理员（或站点管理员）能看到房间 token 与花费。"""
    if not _can_manage(info):
        return None
    return load_room_usage(room_dir, str(info.get("id") or ""))


def _archive_visible(archive):
    if _is_elevated():
        return True
    user_id = str(session.get("user_id"))
    return any(str(member.get("user_id")) == user_id for member in archive.get("members", []) if isinstance(member, dict))


def _scenario_finished(info, state=None):
    if info.get("completed_at") or info.get("ended_at"):
        return True
    scene_id = str(info.get("active_scene_id") or ((state or {}).get("active_scene_id") if isinstance(state, dict) else "") or "")
    if isinstance(state, dict) and (state.get("completed_at") or state.get("completed") or state.get("ending_reached")):
        return True
    if not scene_id:
        return False
    _, scenario = load_scenario_by_id(_get_scenarios_dir(), info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    for module in (scenario or {}).get("modules", []):
        if isinstance(module, dict) and str(module.get("scene_id") or module.get("id")) == scene_id:
            return str(module.get("module_type") or module.get("type") or "").lower() == "ending"
    return False


def _get_scenarios_dir():
    return current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)


def _current_member(character_card=None):
    user_id = session["user_id"]
    user = None
    try:
        user = current_app.config.get("USER_MANAGER").get_user_by_id(user_id)
    except AttributeError:
        from trpg_server.users.manager import user_manager

        user = user_manager.get_user_by_id(user_id)

    avatar = ""
    if user:
        avatar = normalize_avatar(user.get("avatar"))

    member = {
        "user_id": user_id,
        "username": session.get("username", "user"),
        "role": session.get("role", "USER"),
        "room_role": ROOM_ROLE_MEMBER,
        "status": ROOM_MEMBER_ACTIVE,
        "is_active": True,
        "avatar": avatar,
        "joined_at": _timestamp(),
    }
    if character_card:
        member["character_card"] = _sanitize_character_card(character_card)
        member["character_state"] = _initial_character_state(member["character_card"])
    return member


def _sanitize_character_card(character_card):
    if not isinstance(character_card, dict):
        return None

    name = str(character_card.get("name", "")).strip()
    card_id = str(character_card.get("id", "")).strip()
    if not name or not card_id:
        return None

    attributes = character_card.get("attributes") if isinstance(character_card.get("attributes"), dict) else {}
    max_hp = _bounded_int(character_card.get("maxHp", character_card.get("max_hp")), 1, 999, 1)
    max_san = _bounded_int(character_card.get("maxSan", character_card.get("max_san")), 0, 999, 0)
    max_mp = _bounded_int(character_card.get("maxMp", character_card.get("max_mp")), 0, 999, 0)
    status = character_card.get("status") if isinstance(character_card.get("status"), dict) else {}
    sanitized = {
        "id": card_id[:80],
        "name": name[:80],
        "rulesetId": str(character_card.get("rulesetId") or _fallback_ruleset_id())[:40],
        "occupationId": str(character_card.get("occupationId", character_card.get("occupation_id", "")))[:80],
        "attributes": attributes,
        "maxHp": max_hp,
        "currentHp": _bounded_int(character_card.get("currentHp", character_card.get("current_hp")), 0, max_hp, max_hp),
        "maxSan": max_san,
        "initialSan": _bounded_int(character_card.get("initialSan"), 0, 999, max_san),
        "currentSan": _bounded_int(character_card.get("currentSan", character_card.get("current_san")), 0, max_san, max_san),
        "maxMp": max_mp,
        "currentMp": _bounded_int(character_card.get("currentMp", character_card.get("magicPoints")), 0, max_mp, max_mp),
        "mov": _bounded_int(character_card.get("mov"), 0, 99, 0),
        "build": _bounded_int(character_card.get("build"), -2, 99, 0),
        "damageBonus": str(character_card.get("damageBonus") or "")[:20],
        "armor": _bounded_int(character_card.get("armor"), 0, 99, 0),
        "skills": character_card.get("skills", []) if isinstance(character_card.get("skills", []), list) else [],
        "weapons": weapon_catalog.sanitize_character_weapons(character_card.get("weapons")),
        "customWeapons": weapon_catalog.sanitize_character_weapons(character_card.get("customWeapons")),
        "equipment": character_card.get("equipment", []) if isinstance(character_card.get("equipment", []), list) else [],
        "background": character_card.get("background", {}) if isinstance(character_card.get("background", {}), dict) else {},
        "status": status,
    }
    return _derive_character_card(sanitized)


def _derive_character_card(character_card):
    """调用当前规则集插件重算派生值；插件不可用时原样返回。"""
    if not isinstance(character_card, dict):
        return character_card
    try:
        from trpg_server.ruleset import get_plugin
        from trpg_server.ruleset.capabilities import derive_card

        return derive_card(get_plugin(character_card.get("rulesetId")), character_card)
    except Exception:  # noqa: BLE001 - 插件异常不应阻断房间读取
        return dict(character_card)


def _ruleset_plugin(character_card):
    """卡所属规则集插件；缺失 / 不可用时返回 ``None``（回落到核心默认槽位）。"""
    if not isinstance(character_card, dict):
        return None
    try:
        from trpg_server.ruleset import get_plugin

        return get_plugin(character_card.get("rulesetId"))
    except Exception:  # noqa: BLE001 - 插件异常按默认槽位处理
        return None


def _bounded_int(value, minimum, maximum, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _initial_character_state(character_card):
    """按规则集派生的上限初始化状态。

    资源键（上限 / 当前值 / 初始值）由插件 ``resource_slots`` 声明，核心不写死
    HP / SAN / MP 字段名；理智从「初始值 = POW」开始，而不是直接拉满上限。
    """
    from trpg_server.ruleset.capabilities import resource_state_defaults

    card = _derive_character_card(character_card or {})
    state = dict(resource_state_defaults(_ruleset_plugin(card), card))
    state.update(
        {
            "injury_records": [],
            "sanity_records": [],
            "skill_changes": [],
            "item_changes": [],
            "major_wound": False,
            "unconscious": False,
            "dying": False,
            "dead": False,
            "first_aid_used": False,
        }
    )
    return state


def _ensure_character_state(member):
    from trpg_server.ruleset.capabilities import clamp_resource_state, derived_card_keys

    if not member.get("character_state"):
        member["character_state"] = _initial_character_state(member.get("character_card") or {})
    state = member["character_state"]
    state.setdefault("injury_records", [])
    state.setdefault("sanity_records", [])
    state.setdefault("skill_changes", [])
    state.setdefault("item_changes", [])
    state.setdefault("major_wound", False)
    state.setdefault("unconscious", False)
    state.setdefault("dying", False)
    state.setdefault("dead", False)
    state.setdefault("first_aid_used", False)
    # 上限与当前值以规则集派生结果为准，旧房间读取时同步修正。
    card = _derive_character_card(member.get("character_card") or {})
    plugin = _ruleset_plugin(card)
    snapshot = member.get("character_card")
    if card and isinstance(snapshot, dict):
        for key in derived_card_keys(plugin):
            if key in card:
                snapshot[key] = card[key]
    clamp_resource_state(plugin, card, state)
    return state


def _bind_character(member, character_card):
    sanitized = _sanitize_character_card(character_card)
    if not sanitized:
        return False
    member["character_card"] = sanitized
    member["character_state"] = _initial_character_state(sanitized)
    return True


def _can_bind_character_card(character_card, target_member):
    if _is_elevated():
        return True
    player_id = str((character_card or {}).get("playerId") or "")
    return player_id in {
        str(target_member.get("user_id")),
        str(target_member.get("username")),
    }


def _is_active_member(member):
    return member.get("is_active", True) is not False and member.get("status", ROOM_MEMBER_ACTIVE) != ROOM_MEMBER_REMOVED


def _find_member(info, user_id=None, username=None, active_only=False):
    for member in info.get("members", []):
        if active_only and not _is_active_member(member):
            continue
        if user_id is not None and str(member.get("user_id")) == str(user_id):
            return member
        if username and str(member.get("username", "")).lower() == str(username).lower():
            return member
    return None


def _normalize_members(info):
    creator_id = str(info.get("creator_id"))
    for member in info.setdefault("members", []):
        member.setdefault("status", ROOM_MEMBER_ACTIVE)
        member.setdefault("is_active", member.get("status") != ROOM_MEMBER_REMOVED)
        if str(member.get("user_id")) == creator_id:
            member["room_role"] = ROOM_ROLE_OWNER
            # 房主自助退出后 creator_id 可能仍指向已移除成员；不要把他重新激活，
            # 否则房间会凭空多出一位「在场房主」。重新加入时由 join 流程恢复成员状态。
            if _is_active_member(member):
                member["status"] = ROOM_MEMBER_ACTIVE
                member["is_active"] = True
        else:
            member.setdefault("room_role", ROOM_ROLE_MEMBER)
    return info["members"]


def _room_permission(member, info):
    if _is_elevated():
        return ROOM_ROLE_ADMIN
    if not member or not _is_active_member(member):
        return ""
    if str(member.get("user_id")) == str(info.get("creator_id")):
        return ROOM_ROLE_OWNER
    return member.get("room_role") or ROOM_ROLE_MEMBER


def _can_manage_members(info):
    member = _find_member(info, user_id=session.get("user_id"), active_only=True)
    return _is_elevated() or _room_permission(member, info) in {ROOM_ROLE_OWNER, ROOM_ROLE_ADMIN}


def _recalculate_character_state(state):
    damage = sum(_bounded_int(record.get("value", record.get("damage")), 0, 999, 0) for record in state.get("injury_records", []))
    san_loss = sum(_bounded_int(record.get("value", record.get("loss")), 0, 999, 0) for record in state.get("sanity_records", []))
    state["current_hp"] = max(0, state["max_hp"] - damage)
    # 理智从「初始值 = POW」起算，而不是从上限（99 − 克苏鲁神话）起算。
    san_base = _bounded_int(state.get("initial_san"), 0, state["max_san"], state["max_san"])
    state["current_san"] = max(0, min(state["max_san"], san_base - san_loss))


def _messages_file(room_dir):
    return room_dir / "messages.json"


def _read_messages(room_dir):
    return read_json(_messages_file(room_dir), default=[])


def _write_messages(room_dir, messages):
    write_json_atomic(_messages_file(room_dir), messages)


def _character_delta(member):
    """Return ``(san_change_text, other_changes_text)`` for a room member.

    ``other_changes_text`` aggregates HP/injury/skill/item changes so it can be
    rendered as the third line of a "参与过的模组" record.
    """
    state = member.get("character_state") if isinstance(member, dict) else {}
    state = state if isinstance(state, dict) else {}
    max_san = int(state.get("max_san") or 0)
    max_hp = int(state.get("max_hp") or 0)
    current_hp = int(state.get("current_hp") if state.get("current_hp") is not None else max_hp)
    san_base = int(state.get("initial_san") if state.get("initial_san") is not None else max_san)
    current_san = int(state.get("current_san") if state.get("current_san") is not None else san_base)
    san_change = current_san - san_base
    other = []
    if current_hp != max_hp:
        other.append(f"HP {max_hp}->{current_hp}")
    for record in (state.get("injury_records") or [])[:20]:
        if isinstance(record, dict) and record.get("reason"):
            other.append(str(record["reason"])[:120])
    for record in (state.get("skill_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    for record in (state.get("item_changes") or [])[:20]:
        text = _change_record_text(record)
        if text:
            other.append(text)
    san_text = f"SAN {san_change:+d}" if san_change else "SAN 无变化"
    return san_text, "；".join(dict.fromkeys(other))


def _change_record_text(record):
    """Format a structured skill/item change record into a short line."""
    if not isinstance(record, dict):
        return ""
    change_type = str(record.get("type") or "")
    target = str(record.get("target") or record.get("name") or record.get("item") or "").strip()
    if not target:
        return ""
    verb = record.get("change") or record.get("operation") or record.get("action") or "变化"
    amount = record.get("amount")
    if amount is None and record.get("value") is not None:
        amount = record["value"]
    reason = str(record.get("reason") or "").strip()
    prefix = "技能" if change_type == "skill" else "物品" if change_type == "item" else "变更"
    if amount is not None:
        try:
            amount_signed = f"{int(amount):+d}"
        except (TypeError, ValueError):
            amount_signed = str(amount)
        line = f"{prefix} {target} {verb} {amount_signed}".strip() + (f"（{reason}）" if reason else "")
    else:
        line = f"{prefix} {target} {verb}".strip() + (f"（{reason}）" if reason else "")
    return line[:160]


def _append_character_archive_records(info):
    """Copy the finished room outcome to each participating character card.

    Each entry is stored with ``name`` (module/scenario name), ``san_change``
    and ``other_changes`` so UIs can render the required three-line format:
    参与过的模组名 / SAN 值变化 / 其他变化.
    """
    scenario_name = str(info.get("scenario_title") or info.get("name") or "未命名剧本").strip()
    for member in info.get("members", []):
        card = member.get("character_card") if isinstance(member, dict) else None
        card_id = str((card or {}).get("id") or "").strip()
        if not card_id:
            continue
        path = Path(current_app.config.get("CHARACTERS_DIR", CHARACTERS_DIR)) / f"{card_id}.json"
        if not path.exists():
            continue
        stored = read_json(path, default={})
        if not isinstance(stored, dict):
            continue
        san_change, other_changes = _character_delta(member)
        entry = {
            "name": scenario_name,
            "san_change": san_change,
            "other_changes": other_changes,
            "experience": f"{san_change}" + (f"；{other_changes}" if other_changes else ""),
        }
        existing = stored.get("experiencedModules")
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (TypeError, ValueError):
                existing = []
        if not isinstance(existing, list):
            # Runtime-shaped character cards use experiencedScenarios.
            existing = stored.get("experiencedScenarios") if isinstance(stored.get("experiencedScenarios"), list) else []
        # One immutable outcome entry per archived room/scenario.
        if not any(isinstance(item, dict) and item.get("name") == scenario_name and item.get("experience") == entry["experience"] for item in existing):
            existing.append(entry)
        if "experiencedModules" in stored or "experiencedScenarios" not in stored:
            stored["experiencedModules"] = json.dumps(existing[-100:], ensure_ascii=False)
        else:
            stored["experiencedScenarios"] = existing[-100:]
        write_json_atomic(path, stored)


def _build_room_archive(room_dir, info):
    """Build the room archive snapshot. Rooms are archived in place (kept usable
    for dice and history) rather than deleted/moved; the archive JSON is retained
    as an immutable history copy and participating players' character cards are
    updated with a three-line "参与过的模组" record."""
    mode = normalize_room_mode(info)
    archive = {
        "id": info.get("id"),
        "name": info.get("name"),
        "room_mode": mode["room_mode"],
        "ai_kp_enabled": mode["ai_kp_enabled"],
        "scenario_binding": mode["scenario_binding"],
        "created_at": info.get("created_at"),
        "completed_at": info.get("completed_at") or _timestamp(),
        "archived_at": info.get("archived_at") or _timestamp(),
        "archived": True,
        "creator_id": info.get("creator_id"),
        "members": info.get("members", []),
        "messages": _read_messages(room_dir),
        "console_log": load_room_log(room_dir, limit=ARCHIVE_LOG_LIMIT),
    }
    interlude_state = room_interlude.interlude_state(info)
    if interlude_state.get("startedAt") or interlude_state.get("reviews") or interlude_state.get("settlement"):
        archive["interlude"] = room_interlude.snapshot(info)
        archive["interlude_settlement"] = interlude_state.get("settlement") or {}
        archive["interlude_reviews"] = interlude_state.get("reviews") or []
    # 世界意志留痕：把已拍定的既定事实随归档快照一并保存，便于事后核对。
    state = read_json(room_dir / "state.json", default={})
    facts = evidence_entries(state) if isinstance(state, dict) else []
    if facts:
        archive["world_override_facts"] = facts
    # 无剧本房间（freeform / 未绑定资料的 manual）跳过剧本字段与角色卡模组记录，
    # 避免生成一条空白的「参与过的模组」。
    if has_bound_scenario(info):
        archive.update(
            {
                "scenario_id": info.get("scenario_id"),
                "scenario_title": info.get("scenario_title"),
                "scenario_version": info.get("scenario_version"),
            }
        )
    history_dir = current_app.config.get("HISTORY_DIR", HISTORY_DIR)
    if history_dir:
        history_file = Path(history_dir) / f"room-{info.get('id')}-kp.json"
        archive["ai_history"] = read_json(history_file, default=[])
    archive_dir = _get_archives_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{info.get('id')}.json"
    write_json_atomic(archive_path, archive)
    if has_bound_scenario(info):
        _append_character_archive_records(info)
    return archive


def _configured_role(role_id="kp"):
    roles = load_roles(_get_role_config_file(), _get_kp_prompt_file(), _get_ai_platform_dir())
    expected = str(role_id or "kp")
    return next((role for role in roles if str(role.get("id")) == expected), roles[0] if roles else {})


def _new_room_code():
    existing = {
        _read_room(room_dir).get("room_code")
        for room_dir in _iter_room_dirs()
    }
    while True:
        code = uuid4().hex[:8].upper()
        if code not in existing:
            return code


def _room_permission_label(member, info):
    if str(member.get("role") or "") in {"ADMIN", "OWNER"}:
        label = "\u7ba1\u7406\u5458"
    elif member.get("room_role") == ROOM_ROLE_OWNER or str(member.get("user_id")) == str(info.get("creator_id")):
        label = "\u623f\u4e3b"
    elif member.get("room_role") == ROOM_ROLE_ADMIN:
        label = "\u7ba1\u7406\u5458"
    else:
        label = "\u6210\u5458"
    if not _is_active_member(member):
        return f"{label}\uff08\u5df2\u79fb\u9664\uff09"
    return label


def _is_member_online(member, room_id=None):
    """成员在该房间是否显示为「在线」。

    仅当用户既在线、又已经进入这个房间时才算在线：用户在线但停留在别的房间/页面时，
    不应在本房间的成员列表里显示为在线。
    """
    if not _is_active_member(member):
        return False
    user_id = member.get("user_id")
    if user_id is None or not is_socket_user_online(user_id):
        return False
    # 未提供 room_id 时退回到旧的「全局在线」判定，兼容不传房间的调用方。
    if room_id in (None, ""):
        return True
    return is_user_in_room(user_id, room_id)


def _room_has_online_member(info):
    """房间内是否存在「已进入该房间且在线」的成员（用于自动存档判定）。"""
    room_id = (info or {}).get("id")
    for member in (info or {}).get("members") or []:
        if isinstance(member, dict) and _is_member_online(member, room_id):
            return True
    return False


def _live_member_role(member):
    """实时读取账号角色：用户被降级/升级后，房间成员快照里的旧角色不再生效。"""
    fallback = member.get("role")
    user_id = member.get("user_id")
    if user_id is None:
        return fallback
    try:
        manager = get_user_manager()
        user = manager.get_user_by_id(user_id) if hasattr(manager, "get_user_by_id") else None
    except Exception:  # noqa: BLE001 - 用户查询失败时回退到房间快照，不影响房间读取
        user = None
    return (user or {}).get("role") or fallback


def _room_visibility(info):
    value = str((info or {}).get("visibility") or ROOM_VISIBILITY_PRIVATE).strip().lower()
    return value if value in ROOM_VISIBILITIES else ROOM_VISIBILITY_PRIVATE


def _room_ruleset_id(info):
    """解析房间规则集：显式字段优先，旧房间按剧本推断，最后回退 coc7。"""
    data = info or {}
    explicit = str(data.get("ruleset_id") or "").strip()
    if explicit:
        return builtin_registry().resolve(explicit).id
    scenario_id = str(data.get("scenario_id") or "").strip()
    if scenario_id:
        try:
            _, scenario = load_scenario_by_id(
                _get_scenarios_dir(),
                scenario_id,
                scenario_version=data.get("scenario_version"),
            )
            inferred = str((scenario or {}).get("ruleset_id") or "").strip()
            if inferred:
                return builtin_registry().resolve(inferred).id
        except Exception:  # pragma: no cover - 剧本缺失时按默认规则集处理
            logger.debug("Failed to infer ruleset for room %s", data.get("id"), exc_info=True)
    return builtin_registry().resolve("").id


def _room_summary(info):
    _normalize_members(info)
    mode = normalize_room_mode(info)

    def _member_view(member):
        # 角色以账号数据库为准，避免降级后成员仍显示为管理员配色/标签。
        role = _live_member_role(member)
        view = {**member, "role": role}
        view["is_active"] = _is_active_member(member)
        view["is_online"] = _is_member_online(member, info.get("id"))
        view["permission_label"] = _room_permission_label(view, info)
        return view

    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "room_code": info.get("room_code"),
        "scenario_id": info.get("scenario_id"),
        "scenario_version": info.get("scenario_version"),
        "scenario_title": info.get("scenario_title"),
        # 房间级唯一规则集：剧本侧已持久化，旧房间在读取时按剧本推断。
        "ruleset_id": _room_ruleset_id(info),
        # 房间模式的两个正交能力位：AI KP 是否参与、剧本绑定强度。
        # 旧房间在读取时补默认值，不写回 info.json。
        "room_mode": mode["room_mode"],
        "ai_kp_enabled": mode["ai_kp_enabled"],
        "scenario_binding": mode["scenario_binding"],
        "visibility": _room_visibility(info),
        "scenario_started_at": info.get("scenario_started_at"),
        "scenario_started_by": info.get("scenario_started_by"),
        "archived": bool(info.get("archived")),
        "archived_at": info.get("archived_at"),
        "completed_at": info.get("completed_at"),
        "interlude": room_interlude.snapshot(info),
        "creator_id": info.get("creator_id"),
        "creator_name": info.get("creator_name"),
        "members": [_member_view(member) for member in info.get("members", [])],
        "house_rules": _room_house_rules(info),
        # 全房间统一的骰娘大成功/大失败阈值（房规优先，其次管理员默认值）。
        "dice_thresholds": _room_dice_thresholds(info),
        "dice_animation": _room_dice_animation(info),
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
    }


def _invisible_room_view(room_dir, info):
    return {
        "id": info.get("id"),
        "name": info.get("name"),
        "messages": _read_messages(room_dir),
        "invisible_view": True,
    }


def _created_room_count(user_id):
    return sum(1 for room_dir in _iter_room_dirs() if _read_room(room_dir).get("creator_id") == user_id)


@bp.route("/api/rooms", methods=["GET"])
def list_rooms():
    login_error = _require_login()
    if login_error:
        return login_error

    rooms = []
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        # 公开房间展示给所有玩家；私人房间仅成员可访问。
        if _can_access(info) or (_room_visibility(info) == ROOM_VISIBILITY_PUBLIC and not info.get("archived")):
            summary = _room_summary(info)
            usage = _room_usage_for(info, room_dir)
            if usage is not None:
                summary["usage"] = usage
            rooms.append(summary)
    rooms.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return success_response(rooms, "Rooms loaded successfully")


@bp.route("/api/rooms", methods=["POST"])
def create_room():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    # 房间模式预设：scripted（剧本 + AI）/ freeform（无剧本 + AI）/ manual（无 AI）。
    # 默认 scripted，旧客户端不传该字段时行为不变。
    room_mode = str(data.get("room_mode") or ROOM_MODE_SCRIPTED).strip().lower()
    if room_mode not in ROOM_MODES:
        return error_response("Unknown room mode", 400, "Unknown room mode")
    scenario_id = data.get("scenario_id")
    if scenario_id in (None, ""):
        scenario_id = None
    scenario_title = str(data.get("scenario_title", "")).strip()
    # 规则集在创建时固化：显式请求必须可选择（dnd5e 等开发中规则集会在此被拒绝），
    # 未指定时用默认规则集；剧本房间随后以剧本的 ruleset_id 为准。
    requested_ruleset = str(data.get("ruleset_id") or data.get("rulesetId") or "").strip()
    try:
        ruleset_id = builtin_registry().assert_selectable(requested_ruleset or default_ruleset_id()).id
    except RulesetSelectionError as exc:
        return error_response(exc.message, 400, exc.reason_code)
    # 房间可见性默认为私人房间（仅能通过房间码加入）；公开房间会展示给所有玩家。
    visibility = str(data.get("visibility", ROOM_VISIBILITY_PRIVATE)).strip().lower()
    if visibility not in {ROOM_VISIBILITY_PUBLIC, ROOM_VISIBILITY_PRIVATE}:
        visibility = ROOM_VISIBILITY_PRIVATE
    if not name:
        return error_response("Please enter room name", 400, "Room name is required")
    if room_mode == ROOM_MODE_FREEFORM and scenario_id is not None:
        return error_response("Freeform room cannot bind a scenario", 400, "Scenario binding is not allowed")
    if room_mode == ROOM_MODE_SCRIPTED and scenario_id is None:
        return error_response("Please choose a scenario", 400, "Scenario is required")
    if _room_name_exists(name):
        return error_response("Room name already exists", 409, "Room name already exists")
    if not _is_elevated() and _created_room_count(session["user_id"]) >= USER_ROOM_LIMIT:
        return error_response(
            "Room creation limit reached",
            403,
            "Room creation limit reached",
        )
    room_id = uuid4().hex
    room_dir = _room_dir(room_id)
    member = _current_member(data.get("character_card"))
    member["room_role"] = ROOM_ROLE_OWNER
    now = _timestamp()
    preset = MODE_PRESETS[room_mode]
    info = {
        "id": room_id,
        "name": name,
        "room_code": _new_room_code(),
        # 可轮换的邀请令牌：邀请链接/二维码用它生成，刷新后旧链接立即失效。
        "invite_token": _new_invite_token(),
        # 模式能力位：room_mode 负责展示与预设，两个布尔/枚举位负责实际校验。
        "room_mode": room_mode,
        "ai_kp_enabled": preset["ai_kp_enabled"],
        "scenario_binding": preset["scenario_binding"],
        "ruleset_id": ruleset_id,
        "visibility": visibility,
        "creator_id": session["user_id"],
        "creator_name": session.get("username", "user"),
        "members": [member],
        "created_at": now,
        "updated_at": now,
    }
    if scenario_id is not None:
        # manual 下剧本只作玩家自查资料，不驱动任何 AI 能力。
        info["scenario_id"] = scenario_id
        info["scenario_title"] = scenario_title
    # Pin a deterministic starting scene in room state. The KP receives this
    # pointer and can only move it via room.activate_scenario_scene.
    # 只有 scripted 房间需要场景指针；freeform 无剧本，manual 不跑 AI 流程。
    if room_mode == ROOM_MODE_SCRIPTED:
        try:
            from trpg_server.scenario_store import load_scenario_by_id
            _, bound_scenario = load_scenario_by_id(
                current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR), scenario_id
            )
            if bound_scenario:
                scenario_ruleset = str(bound_scenario.get("ruleset_id") or "").strip()
                if scenario_ruleset:
                    scenario_plugin = builtin_registry().resolve(scenario_ruleset)
                    if requested_ruleset and scenario_plugin.id != ruleset_id:
                        return error_response(
                            "房间规则集必须与剧本一致",
                            400,
                            "ruleset_mismatch",
                        )
                    ruleset_id = scenario_plugin.id
                    info["ruleset_id"] = ruleset_id
                info["scenario_version"] = str(bound_scenario.get("scenario_version") or bound_scenario.get("version") or "1")
                scene = next((m for m in bound_scenario.get("modules", [])
                              if isinstance(m, dict) and str(m.get("module_type")) == "scene"), None)
                if scene:
                    info["active_scene_id"] = str(scene.get("scene_id") or scene.get("id"))
                    info["active_scene_title"] = scene.get("title")
        except Exception:
            logger.exception("Failed to initialize room active scene")
    # 与加入房间共用同一把占用锁：建房也会绑定角色卡，同一张卡不能同时存在于两个房间。
    conflict = None
    with card_binding_lock():
        conflict = _find_card_conflict(member.get("character_card"))
        if not conflict:
            write_json_atomic(room_dir / "info.json", info)
    if conflict:
        return _card_in_use_response(conflict)
    _write_messages(room_dir, [])

    write_json_atomic(room_dir / "autosave.json", {"updated_at": now, "messages": []})

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "创建了房间"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间码=info["room_code"],
        房间名=name,
        模式=room_mode,
        剧本=scenario_title,
    )
    data = _room_summary(info)
    data["messages"] = []
    data["usage"] = load_room_usage(room_dir, str(room_id))
    return success_response(data, "Room created successfully", 201)


@bp.route("/api/rooms/spectate", methods=["POST"])
def spectate_room_by_code():
    login_error = _require_login()
    if login_error:
        return login_error
    if not _is_elevated():
        return _denied()

    data = request.get_json(silent=True) or {}
    room_dir, info = _find_room_by_code(data.get("room_code"))
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return success_response(_invisible_room_view(room_dir, info), "Room spectated successfully")


@bp.route("/api/rooms/lookup", methods=["GET"])
def lookup_room_by_code():
    """按房间码/邀请令牌查询最小的房间信息。

    加入房间前需要知道房间模式（无 AI 房间允许不绑定角色卡进入），
    但不能泄露成员列表与角色卡，因此只返回模式、名称与剧本标题。
    """
    login_error = _require_login()
    if login_error:
        return login_error
    invite_token = str(request.args.get("invite_token") or "").strip()
    if invite_token:
        room_dir, info = _find_room_by_invite_token(invite_token)
    else:
        room_dir, info = _find_room_by_code(request.args.get("code"))
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    mode = normalize_room_mode(info)
    return success_response(
        {
            "id": info.get("id"),
            "name": info.get("name"),
            "room_mode": mode["room_mode"],
            "ai_kp_enabled": mode["ai_kp_enabled"],
            "scenario_binding": mode["scenario_binding"],
            "scenario_title": info.get("scenario_title"),
            "archived": bool(info.get("archived")),
        }
    )


@bp.route("/api/rooms/join", methods=["POST"])
def join_room_by_code():
    login_error = _require_login()
    if login_error:
        return login_error

    data = request.get_json(silent=True) or {}
    invite_token = str(data.get("invite_token") or "").strip()
    if invite_token:
        # 通过邀请链接（?invite=<token>）加入：令牌被刷新后旧链接即失效。
        room_dir, info = _find_room_by_invite_token(invite_token)
        if not room_dir:
            return error_response(
                "Invite link is invalid or has expired",
                404,
                "Invite link is invalid or has expired",
            )
    else:
        room_dir, info = _find_room_by_code(data.get("room_code"))
        if not room_dir:
            return error_response("Room not found", 404, "Room not found")

    user_id = session["user_id"]
    character_card = data.get("character_card")
    # 「查占用 + 落盘」放在同一把锁里：同一张卡并发加入两个房间时只有一个能成功，
    # 同时也避免两个请求基于同一份旧快照互相覆盖。已在房间里的重复加入不算冲突。
    with card_binding_lock():
        info = _read_room(room_dir)
        member = _find_member(info, user_id=user_id)
        will_bind_card = bool(character_card) and (
            member is None or not _is_active_member(member) or not member.get("character_card")
        )
        if will_bind_card:
            conflict = _find_card_conflict(character_card, exclude_room_id=info.get("id") or room_dir.name)
            if conflict:
                return _card_in_use_response(conflict)
        changed = True
        if not member:
            info.setdefault("members", []).append(_current_member(character_card))
        elif not _is_active_member(member):
            current_member = _current_member(character_card)
            member.update(
                {
                    "username": current_member["username"],
                    "role": current_member["role"],
                    "avatar": current_member["avatar"],
                    "status": ROOM_MEMBER_ACTIVE,
                    "is_active": True,
                    "rejoined_at": _timestamp(),
                }
            )
            if character_card:
                _bind_character(member, character_card)
        elif character_card and not member.get("character_card"):
            _bind_character(member, character_card)
        else:
            # 已在房间里且无需绑卡：重复加入保持幂等，不写盘也不报冲突。
            changed = False
        if changed:
            _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "加入了房间"),
        actor=session.get("username"),
        room_id=info["id"],
        logger_=logger,
        用户ID=user_id,
        房间码=info.get("room_code"),
        房间名=info.get("name"),
    )
    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    usage = _room_usage_for(info, room_dir)
    if usage is not None:
        data["usage"] = usage
    return success_response(data, "Room joined successfully")


@bp.route("/api/rooms/<room_id>/invite-token", methods=["GET"])
def get_room_invite_token(room_id):
    """获取房间当前的邀请令牌（无则惰性生成），供前端拼出邀请链接与二维码。"""
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    token = _ensure_invite_token(room_dir, info)
    return success_response(
        {"invite_token": token, "can_rotate": _can_manage(info)},
        "Invite token loaded successfully",
    )


@bp.route("/api/rooms/<room_id>/invite-token", methods=["POST"])
def rotate_room_invite_token(room_id):
    """刷新房间邀请令牌：旧邀请链接/二维码随即失效（仅房主或管理员可操作）。"""
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    token = _new_invite_token()
    info["invite_token"] = token
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "刷新了房间邀请链接"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        房间码=info.get("room_code"),
        房间名=info.get("name"),
    )
    return success_response({"invite_token": token}, "Invite link refreshed successfully")


@bp.route("/api/rooms/<room_id>/members/<user_id>/character", methods=["PUT"])
def bind_room_member_character(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if str(target.get("user_id")) != str(session["user_id"]) and not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    character_card = data.get("character_card")
    if not _can_bind_character_card(character_card, target):
        return _denied(room_id)
    if not _bind_character(target, character_card):
        return error_response("Character card is required", 400, "Character card is required")
    # 换卡同样是「绑定」，要在同一把占用锁里校验并落盘，否则先换卡再加入别的房间会绕过校验。
    with card_binding_lock():
        conflict = _find_card_conflict(target.get("character_card"), exclude_room_id=room_id)
        if conflict:
            return _card_in_use_response(conflict)
        _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "绑定了房间角色卡"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        角色卡ID=target.get("character_card", {}).get("id"),
        角色名=target.get("character_card", {}).get("name"),
    )
    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    return success_response(data, "Character bound successfully")


@bp.route("/api/rooms/<room_id>/members/<user_id>", methods=["DELETE"])
def delete_room_member(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner cannot be removed", 400, "Room owner cannot be removed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    target["status"] = ROOM_MEMBER_REMOVED
    target["is_active"] = False
    target["removed_at"] = _timestamp()
    if target.get("room_role") == ROOM_ROLE_ADMIN:
        target["room_role"] = ROOM_ROLE_MEMBER
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "移除了房间成员"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        房间名=info.get("name"),
    )
    return success_response(_room_summary(info), "Player removed")


@bp.route("/api/rooms/<room_id>/owner", methods=["PUT"])
def transfer_room_owner(room_id):
    """显式移交房主：房主本人或站点管理员把房主交给另一位活跃成员。

    移交后原房主降为普通成员，双方都继续留在房间；目标必须是当前活跃成员，
    不能是自己，也不能是当前房主。
    """
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, _ = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")

    data = request.get_json(silent=True) or {}
    target_user_id = str(data.get("user_id") or "").strip()
    if not target_user_id:
        return error_response("Target user is required", 400, "invalid_target")

    requester_id = str(session["user_id"])
    if target_user_id == requester_id:
        return error_response("Cannot transfer room ownership to yourself", 400, "cannot_transfer_to_self")

    # 与加入/换卡/退出共用同一把占用锁，避免读到并改写中间态。
    with card_binding_lock():
        info = _read_room(room_dir)
        current_owner_id = str(info.get("creator_id") or "")
        if not (_is_elevated() or current_owner_id == requester_id):
            return _denied(room_id)
        if target_user_id == current_owner_id:
            return error_response("Target is already the room owner", 400, "already_room_owner")
        target = _find_member(info, user_id=target_user_id, active_only=True)
        if not target:
            return error_response("Player not found in room", 404, "Player not found in room")
        _assign_room_owner(info, target, previous_user_id=current_owner_id)
        _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), f"把房主移交给了 {target.get('username') or target_user_id}"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        原房主=current_owner_id,
        新房主=target_user_id,
        房间名=info.get("name"),
    )
    _emit_room_members_changed(info.get("id") or room_id)
    return success_response(_room_summary(info), "Room owner transferred")


@bp.route("/api/rooms/<room_id>/leave", methods=["POST"])
def leave_room(room_id):
    """玩家自助退出房间：只移除自己，不放宽房主/管理员的移除权限。

    退出即释放角色卡占用（占用按活跃成员现算）；若还有未归档的待写回改动，
    必须先由前端二次确认再带 ``confirm_pending`` 重试，避免静默丢弃。
    房主未显式移交就退出时，自动移交给加入时间最早的活跃成员；没有其他
    活跃成员时允许退出，房间保留。
    """
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, _ = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")

    data = request.get_json(silent=True) or {}
    confirm_pending = bool(data.get("confirm_pending"))
    user_id = session["user_id"]
    heir = None

    # 与加入/换卡共用同一把占用锁：退出写盘与别处的占用校验串行，避免并发读到中间态。
    with card_binding_lock():
        info = _read_room(room_dir)
        member = _find_member(info, user_id=user_id, active_only=True)
        # 重复退出保持幂等：已经不在房间里时直接按成功返回。
        if not member:
            return success_response({"room_id": info.get("id"), "already_left": True}, "Already left room")

        others = [
            row
            for row in info.get("members", [])
            if _is_active_member(row) and str(row.get("user_id")) != str(user_id)
        ]
        # 房主未显式移交就退出：自动交给加入时间最早的活跃成员，并在房间日志里说明原因。
        if str(info.get("creator_id")) == str(user_id) and others:
            heir = _earliest_active_member(info, exclude_user_id=user_id)
            if heir is not None:
                _assign_room_owner(info, heir, previous_user_id=user_id)

        pending_count = _pending_room_changes(member)
        if pending_count and not confirm_pending:
            return error_response(
                "本房间内的改动尚未归档，退出后不会生效",
                409,
                "pending_changes_unarchived",
                pending_changes=pending_count,
            )

        card_id = card_id_of(member.get("character_card"))
        member["status"] = ROOM_MEMBER_REMOVED
        member["is_active"] = False
        member["removed_at"] = _timestamp()
        if member.get("room_role") in {ROOM_ROLE_ADMIN, ROOM_ROLE_OWNER}:
            member["room_role"] = ROOM_ROLE_MEMBER
        _write_room(room_dir, info)

    heir_username = str((heir or {}).get("username") or "")
    leave_action = (
        f"退出了房间，房主自动移交给加入时间最早的活跃成员 {heir_username}"
        if heir
        else "退出了房间"
    )
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), leave_action),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=user_id,
        房间名=info.get("name"),
        角色卡ID=card_id,
        未归档改动=pending_count,
        新房主=heir_username,
    )
    _emit_room_members_changed(info.get("id") or room_id)
    return success_response(
        {
            "room_id": info.get("id"),
            "pending_changes": pending_count,
            "released_card_id": card_id,
            "owner_transferred_to": str((heir or {}).get("user_id") or "") or None,
        },
        "Left room",
    )


@bp.route("/api/rooms/<room_id>/members/<user_id>/role", methods=["PUT"])
def update_room_member_role(room_id, user_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage_members(info):
        return _denied(room_id)
    if str(user_id) == str(info.get("creator_id")):
        return error_response("Room owner role cannot be changed", 400, "Room owner role cannot be changed")

    target = _find_member(info, user_id=user_id, active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    data = request.get_json(silent=True) or {}
    role = data.get("room_role")
    if role not in {ROOM_ROLE_ADMIN, ROOM_ROLE_MEMBER}:
        return error_response("Invalid room role", 400, "Invalid room role")
    target["room_role"] = role
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "更改了房间成员权限"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        目标用户=user_id,
        权限=role,
    )
    return success_response(_room_summary(info), "Room role updated")


@bp.route("/api/rooms/<room_id>", methods=["GET"])
def get_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    data = _room_summary(info)
    data["messages"] = _read_messages(room_dir)
    usage = _room_usage_for(info, room_dir)
    if usage is not None:
        data["usage"] = usage
    return success_response(data, "Room loaded successfully")


@bp.route("/api/rooms/<room_id>/usage", methods=["GET"])
def get_room_usage(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    return success_response(load_room_usage(room_dir, str(room_id)), "Room usage loaded successfully")


@bp.route("/api/rooms/<room_id>/scenario-migration", methods=["POST"])
def migrate_room_scenario(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if not accepts_scenario(info):
        return error_response("Freeform room has no scenario to migrate", 400, "Scenario binding is not allowed")
    payload = request.get_json(silent=True) or {}
    target_version = payload.get("scenario_version")
    _, target = load_scenario_by_id(
        current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR),
        info.get("scenario_id"),
        scenario_version=target_version,
    )
    if not target:
        return error_response("Target scenario version not found", 404, "Scenario version not found")
    state = read_json(room_dir / "state.json", default={})
    old_entities = {str(item.get("id")) for item in payload.get("old_entities", []) if isinstance(item, dict)}
    if not old_entities:
        for key in ("clues", "items", "quests", "triggered_event_ids"):
            values = state.get(key, []) if isinstance(state, dict) else []
            if isinstance(values, list):
                old_entities.update(str(item.get("id") if isinstance(item, dict) else item) for item in values)
    new_entities = {str(item.get("id")) for item in target.get("modules", []) if isinstance(item, dict) and item.get("id")}
    try:
        updated = migrate_room_binding(info, target, payload.get("mapping") or {}, old_entities, new_entities)
    except ValueError as exc:
        return error_response(str(exc), 400, "Scenario migration rejected")
    updated["ruleset_id"] = builtin_registry().resolve(
        str(target.get("ruleset_id") or updated.get("ruleset_id") or "")
    ).id
    write_json_atomic(room_dir / "info.json", updated)
    return success_response(_room_summary(updated), "Room migrated to scenario version")


def _first_scene(target: dict) -> dict | None:
    """取剧本的第一个场景模块，作为换绑后的开场场景指针。"""
    return next(
        (m for m in (target or {}).get("modules", [])
         if isinstance(m, dict) and str(m.get("module_type")) == "scene"),
        None,
    )


@bp.route("/api/rooms/<room_id>/scenario-switch", methods=["POST"])
def switch_room_scenario(room_id):
    """管理员/房主强制把房间切换到另一个剧本（高风险：会重置剧情进度）。

    与 ``scenario-migration``（同一剧本内换版本、尽量保留绑定）不同，本接口是换到
    一个完全不同的剧本，旧的线索/物品/任务/已触发事件/场景指针/滚动摘要都会失效，
    因此整体重置为新剧本的开场状态，并要求重新「开启剧本」。
    """
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("archived"):
        return error_response("Archived room cannot switch scenario", 409, "Room archived")
    if not accepts_scenario(info):
        return error_response("Freeform room cannot bind a scenario", 400, "Scenario binding is not allowed")

    payload = request.get_json(silent=True) or {}
    target_id = payload.get("scenario_id")
    if target_id in (None, ""):
        return error_response("Please choose a scenario", 400, "Scenario is required")
    # 剧情进度是否继承：换到同一剧本的新版本时应保留进度继续游玩，换到不同剧本时
    # 通常需要重置。由调用方决定，默认重置（更安全）。
    preserve_progress = bool(payload.get("preserve_progress"))

    scenarios_dir = current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR)
    _, target = load_scenario_by_id(scenarios_dir, target_id)
    if not target:
        return error_response("Target scenario not found", 404, "Scenario not found")

    previous_title = info.get("scenario_title")
    updated = dict(info)
    updated["scenario_id"] = target_id
    updated["scenario_version"] = str(target.get("scenario_version") or target.get("version") or "1")
    updated["scenario_title"] = str(target.get("title") or payload.get("scenario_title") or "").strip() or f"剧本 {target_id}"
    updated["ruleset_id"] = builtin_registry().resolve(str(target.get("ruleset_id") or "")).id
    # 场景指针：继承进度时，只要旧场景在新剧本中仍存在就保留，避免打断当前场景；
    # 否则回退到新剧本的第一个场景。
    target_scene_ids = {
        str(m.get("scene_id") or m.get("id"))
        for m in target.get("modules", [])
        if isinstance(m, dict) and str(m.get("module_type")) == "scene"
    }
    first_scene = _first_scene(target)
    first_scene_id = str(first_scene.get("scene_id") or first_scene.get("id")) if first_scene else ""
    current_scene_id = str(updated.get("active_scene_id") or "")
    if not (preserve_progress and current_scene_id and current_scene_id in target_scene_ids):
        if first_scene_id:
            updated["active_scene_id"] = first_scene_id
            updated["active_scene_title"] = first_scene.get("title")
        else:
            updated.pop("active_scene_id", None)
            updated.pop("active_scene_title", None)
    # 重置模式下新剧本必须重新开场：清掉旧剧本的「已开始」标记，避免沿用其开场门控。
    # 继承模式下保留该标记，视为同一场剧情的延续。
    if not preserve_progress:
        updated.pop("scenario_started_at", None)
        updated.pop("scenario_started_by", None)
    updated["updated_at"] = _timestamp()
    write_json_atomic(room_dir / "info.json", updated)

    if preserve_progress:
        # 继承进度：保留线索/物品/任务/已触发事件/滚动摘要与开场状态，仅同步场景指针。
        state = read_json(room_dir / "state.json", default={})
        if not isinstance(state, dict):
            state = {}
        if updated.get("active_scene_id"):
            state["active_scene_id"] = updated["active_scene_id"]
        save_room_state(room_dir, state)
    else:
        # 重置剧情进度：线索/物品/任务/已触发事件/时间线/事件日志/滚动摘要全部清空，
        # 否则旧剧本的进度会被当作新剧本的状态继续注入，正是「剧本错乱」的来源。
        # save_room_state 会以默认空状态为底做归一化，因此这里只给出新的场景指针。
        save_room_state(room_dir, {"active_scene_id": first_scene_id or None})
        # 知识库检索游标按「剧本@版本」绑定，换剧本后本就会失效；显式删除以清理残留。
        try:
            (room_dir / "knowledge_cursor.json").unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove knowledge cursor on scenario switch", exc_info=True)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "强制切换了房间剧本"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间码=updated.get("room_code"),
        原剧本=previous_title,
        新剧本=updated["scenario_title"],
    )
    return success_response(_room_summary(updated), "Room scenario switched")


@bp.route("/api/rooms/<room_id>/spectate", methods=["GET"])
def spectate_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    if not _is_elevated():
        return _denied(room_id)

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return success_response(_invisible_room_view(room_dir, info), "Room spectated successfully")


@bp.route("/api/rooms/<room_id>", methods=["DELETE"])
def delete_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("completed_at") and not info.get("archived"):
        return error_response("Please archive the completed room first", 409, "Completed rooms must be archived so their messages are preserved")

    import shutil

    shutil.rmtree(room_dir)
    log_user_action(
        logger,
        user_action_text(session.get("username"), "删除了房间"),
        用户ID=session.get("user_id"),
        房间ID=room_id,
        房间名=info.get("name"),
    )
    return success_response(message="Room deleted successfully")


@bp.route("/api/rooms/<room_id>/archive", methods=["POST"])
def archive_room(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    if info.get("archived"):
        return error_response("Room already archived", 409, "Room already archived")
    state = read_json(room_dir / "state.json", default={})
    # 只有 scripted 房间需要「到达结局」；freeform 与 manual 没有剧本结局可判定，
    # 房主/房管显式归档即视为结束。
    if normalize_room_mode(info)["scenario_binding"] == SCENARIO_BINDING_REQUIRED and not _scenario_finished(info, state):
        return error_response("Room is not finished", 409, "Room must reach an ending before archiving")
    now = _timestamp()
    # 房规在归档后会被移除，写回持久角色卡必须发生在这一步之前。
    from trpg_server import room_rules

    room_rules.persist_room_items_on_archive(room_dir, info, app=current_app._get_current_object())
    info["completed_at"] = info.get("completed_at") or now
    info["archived_at"] = now
    info["archived"] = True
    # House rules are scoped to an active room; an archived room no longer needs
    # them (the immutable archive snapshot is built from a fixed field whitelist).
    info.pop("house_rules", None)
    _write_room(room_dir, info)
    archive = _build_room_archive(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "归档了房间"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间名=info.get("name"),
    )
    return success_response({"id": archive["id"], "name": archive["name"], "archived_at": archive["archived_at"]}, "Room archived successfully")


@bp.route("/api/rooms/<room_id>/world-override", methods=["GET"])
def get_world_override_status(room_id):
    """世界意志直执行：当前用户是否有权使用，以及房管可配置的成员白名单。"""
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    rules = _room_house_rules(info)
    mode = normalize_room_mode(info)
    # 世界意志只依赖 AI KP：scripted 与 freeform 都可用，manual 不提供（前端据此隐藏入口）。
    supported = bool(mode["ai_kp_enabled"])
    state = read_json(room_dir / "state.json", default={}) if room_dir else {}
    rate = rate_limit_snapshot(
        state,
        session.get("user_id"),
        policy=resolve_policy(_get_config_dir(), rules),
    )
    members = []
    for member in info.get("members") or []:
        if not isinstance(member, dict) or not _is_active_member(member):
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        members.append(
            {
                "user_id": str(member.get("user_id") or ""),
                "username": str(member.get("username") or ""),
                "character_name": str(card.get("name") or ""),
            }
        )
    return success_response(
        {
            "supported": supported,
            "allowed": supported and world_override_allowed(info),
            "enabled": supported and bool(rules.get("world_override_enabled", True)),
            "user_ids": rules.get("world_override_user_ids") or [],
            "can_configure": supported and _can_manage(info),
            "rate": rate,
            "members": members,
        },
        "World override status loaded successfully",
    )


@bp.route("/api/rooms/<room_id>/house-rules", methods=["GET"])
def get_room_house_rules(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    _path, info = _find_room(room_id)
    if not info:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    return success_response(
        {
            "house_rules": _room_house_rules(info),
            "definitions": HOUSE_RULE_DEFINITIONS,
            "global_hints_enabled": _global_hints_enabled(),
            "visibility": _room_visibility(info),
            "dice_thresholds": _room_dice_thresholds(info),
            "dice_threshold_defaults": _admin_dice_thresholds(),
            "dice_animation": _room_dice_animation(info),
            "dice_animation_defaults": {
                "level": load_ai_runtime_config(_get_config_dir()).dice_animation_level,
                "player_roll": load_ai_runtime_config(_get_config_dir()).dice_player_roll,
                "reroll_mode": load_ai_runtime_config(_get_config_dir()).dice_reroll_mode,
                "reroll_limit": load_ai_runtime_config(_get_config_dir()).dice_reroll_limit,
            },
        },
        "House rules loaded successfully",
    )


@bp.route("/api/rooms/<room_id>/house-rules", methods=["PUT"])
def update_room_house_rules(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    payload = request.get_json(silent=True) or {}
    updates = payload.get("house_rules", payload)
    if not isinstance(updates, dict):
        return error_response("Invalid house rules payload", 400, "house_rules must be an object")

    current = _room_house_rules(info)
    for key, value in updates.items():
        spec = HOUSE_RULE_DEFINITIONS.get(key)
        if not spec:
            return error_response(f"Unknown house rule: {key}", 400, "Unknown house rule")
        if spec["type"] == "boolean":
            if not isinstance(value, bool):
                return error_response(f"House rule {key} must be a boolean", 400, "Invalid house rule type")
            current[key] = value
        elif spec["type"] == "nullable_boolean":
            if value is not None and not isinstance(value, bool):
                return error_response(f"House rule {key} must be a boolean or null", 400, "Invalid house rule type")
            current[key] = value
        elif spec["type"] == "nullable_enum":
            if value is not None:
                candidate = str(value).strip().lower()
                if candidate not in spec.get("values", []):
                    return error_response(f"House rule {key} must be one of {spec.get('values')} or null", 400, "Invalid house rule type")
                current[key] = candidate
            else:
                current[key] = None
        elif spec["type"] == "nullable_integer":
            if value is not None and value != "":
                try:
                    int(value)
                except (TypeError, ValueError):
                    return error_response(f"House rule {key} must be an integer", 400, "Invalid house rule type")
            current[key] = _normalize_nullable_int(value, spec.get("min", 0), spec.get("max", 100))
        elif spec["type"] == "audio_blocks":
            if value is None:
                current[key] = None
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return error_response(
                    f"House rule {key} must be a list of audio blocks",
                    400,
                    "Invalid house rule type",
                )
            blocks = []
            for item in value:
                block = str(item or "").strip().lower()
                if block not in AUDIO_BLOCKS:
                    return error_response(
                        f"House rule {key} contains unknown block: {item}",
                        400,
                        "Invalid house rule type",
                    )
                if block not in blocks:
                    blocks.append(block)
            if len([block for block in blocks if block in MUSIC_BLOCKS]) > 1:
                return error_response(
                    f"House rule {key} allows at most one music block",
                    400,
                    "Invalid house rule type",
                )
            current[key] = blocks
        elif spec["type"] == "url":
            if value is None:
                current[key] = ""
                continue
            if not isinstance(value, str):
                return error_response(f"House rule {key} must be a URL string", 400, "Invalid house rule type")
            normalized = _normalize_bgm_url(value)
            if value.strip() and not normalized:
                return error_response(
                    f"House rule {key} must be an http(s) URL or a local music library path",
                    400,
                    "Invalid house rule type",
                )
            current[key] = normalized
        elif spec["type"] == "enum":
            candidate = str(value).strip().lower() if value is not None else ""
            if candidate not in spec.get("values", []):
                return error_response(
                    f"House rule {key} must be one of {spec.get('values')}",
                    400,
                    "Invalid house rule type",
                )
            current[key] = candidate
        elif spec["type"] == "url_list":
            if value is None:
                current[key] = []
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return error_response(
                    f"House rule {key} must be a list of URL strings",
                    400,
                    "Invalid house rule type",
                )
            raw_urls = [item.strip() for item in value if item.strip()]
            if any(not _normalize_bgm_url(item) for item in raw_urls):
                return error_response(
                    f"House rule {key} must contain only http(s) URLs or local music library paths",
                    400,
                    "Invalid house rule type",
                )
            current[key] = _normalize_bgm_url_list(raw_urls, spec.get("max_items", 20))
        elif spec["type"] == "skill_bases":
            if not isinstance(value, dict):
                return error_response(f"House rule {key} must be an object", 400, "Invalid house rule type")
            current[key] = _normalize_skill_bases(value)
        elif spec["type"] == "user_ids":
            if value is None:
                current[key] = []
                continue
            if not isinstance(value, list) or any(not isinstance(item, (str, int)) for item in value):
                return error_response(f"House rule {key} must be a list of user IDs", 400, "Invalid house rule type")
            current[key] = _normalize_user_ids(value, spec.get("max_items", 50))
        else:
            current[key] = value

    enabled_by_request = {
        key for key, spec in HOUSE_RULE_DEFINITIONS.items()
        if spec.get("ai_prompt") and current.get(key) and updates.get(key)
    }
    if enabled_by_request and not _global_hints_enabled():
        return error_response(
            "全局 AI 提示开关已关闭，无法开启该房规",
            400,
            "Global AI hints are disabled",
        )

    info["house_rules"] = current
    # 房间可见性可以在后续房间规则内更改（私人 <-> 公开）。
    visibility_update = payload.get("visibility")
    if visibility_update is not None:
        candidate = str(visibility_update).strip().lower()
        if candidate not in ROOM_VISIBILITIES:
            return error_response("Invalid room visibility", 400, "Invalid visibility")
        info["visibility"] = candidate
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "更新了房间房规"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        房间名=info.get("name"),
    )
    return success_response(
        {
            "house_rules": _room_house_rules(info),
            "definitions": HOUSE_RULE_DEFINITIONS,
            "global_hints_enabled": _global_hints_enabled(),
            "visibility": _room_visibility(info),
            "dice_thresholds": _room_dice_thresholds(info),
            "dice_threshold_defaults": _admin_dice_thresholds(),
            "dice_animation": _room_dice_animation(info),
            "dice_animation_defaults": {
                "level": load_ai_runtime_config(_get_config_dir()).dice_animation_level,
                "player_roll": load_ai_runtime_config(_get_config_dir()).dice_player_roll,
                "reroll_mode": load_ai_runtime_config(_get_config_dir()).dice_reroll_mode,
                "reroll_limit": load_ai_runtime_config(_get_config_dir()).dice_reroll_limit,
            },
        },
        "House rules updated successfully",
    )


@bp.route("/api/room-archives", methods=["GET"])
@bp.route("/api/rooms/archives", methods=["GET"])
def list_room_archives():
    login_error = _require_login()
    if login_error:
        return login_error
    archives = []
    archive_dir = _get_archives_dir()
    for path in archive_dir.glob("*.json") if archive_dir.exists() else []:
        archive = read_json(path, default={})
        if isinstance(archive, dict) and _archive_visible(archive):
            archives.append({key: archive.get(key) for key in ("id", "name", "scenario_title", "archived_at", "completed_at")})
    archives.sort(key=lambda item: str(item.get("archived_at") or ""), reverse=True)
    return success_response(archives, "Room archives loaded successfully")


@bp.route("/api/room-archives/<room_id>", methods=["GET"])
@bp.route("/api/rooms/<room_id>/archive", methods=["GET"])
def get_room_archive(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    archive = read_json(_get_archives_dir() / f"{room_id}.json", default={})
    if not isinstance(archive, dict) or not archive:
        return error_response("Room archive not found", 404, "Room archive not found")
    if not _archive_visible(archive):
        return _denied(room_id)
    return success_response(archive, "Room archive loaded successfully")


@bp.route("/api/rooms/<room_id>/messages", methods=["GET"])
def get_room_messages(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    return success_response(_read_messages(room_dir), "Room messages loaded successfully")


@bp.route("/api/rooms/<room_id>/messages", methods=["POST"])
def create_room_message(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return error_response("Please provide message content", 400, "No message content")

    member = _find_member(info, user_id=session["user_id"], active_only=True)
    if not member:
        return _denied(room_id)
    message = {
        "id": uuid4().hex,
        "type": data.get("type", "player"),
        "sender_id": member["user_id"],
        "sender_name": member["username"],
        "avatar": member["avatar"],
        "content": content,
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": data.get("metadata", {}),
    }
    if message["type"] == "kp":
        role = _configured_role((message.get("metadata") or {}).get("roleId") or data.get("role_id") or "kp")
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or role.get("name") or "KP"
        message["avatar"] = normalize_avatar(data.get("avatar") or role.get("avatar"))
    elif message["type"] == "dice":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "骰娘")
        message["avatar"] = ""
    elif message["type"] == "system":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name", "系统")
        message["avatar"] = ""
    elif message["type"] == "trigger":
        message["sender_id"] = None
        message["sender_name"] = data.get("sender_name") or "触发器"
        message["avatar"] = normalize_avatar(data.get("avatar"))

    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)

    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "发送了房间消息"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        类型=message["type"],
        内容长度=len(content),
    )
    return success_response(message, "Room message saved successfully", 201)


@bp.route("/api/rooms/<room_id>/triggers", methods=["POST"])
def trigger_room_scenario(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _can_manage_members(info):
        return _denied(room_id)

    data = request.get_json(silent=True) or {}
    trigger_id = data.get("trigger_id")
    if trigger_id in (None, ""):
        return error_response("Please provide trigger id", 400, "No trigger id")

    scenarios_dir = _get_scenarios_dir()
    from trpg_server.scenario_store import build_trigger_message, load_scenario_by_id

    _, scenario = load_scenario_by_id(scenarios_dir, info.get("scenario_id"), scenario_version=info.get("scenario_version"))
    if not scenario:
        return error_response("Scenario not found", 404, "Scenario not found")

    room_state = read_json(room_dir / "state.json", default={})
    new_trigger = find_trigger_definition(scenario, str(trigger_id))
    if new_trigger:
        validation = validate_trigger(str(trigger_id), {**(room_state if isinstance(room_state, dict) else {}), "scenario_version": info.get("scenario_version")}, scenario, audience="kp")
        if not validation.get("ok"):
            logger.info("trigger_rejected room_id=%s trigger_id=%s reason=%s", room_id, trigger_id, validation.get("reason"))
            return error_response("Trigger condition is not satisfied", 409, validation.get("reason", "Trigger rejected"))
        attachments = validation["trigger"].get("attachments", [])
        contents = []
        for attachment in attachments:
            resource = attachment.get("resourceRef") or {}
            if resource.get("content"):
                contents.append(str(resource["content"]))
            elif resource.get("url"):
                contents.append(f"[{resource.get('alt')}]({resource.get('url')})")
        trigger_message = {"sender_name": validation["trigger"].get("text") or f"触发器{trigger_id}", "content": "\n\n".join(contents), "metadata": {"trigger_id": trigger_id, "content_mode": "attachment"}}
    else:
        trigger_message = build_trigger_message(scenario, trigger_id)
    if not trigger_message:
        return error_response("Trigger not found", 404, "Trigger not found")

    message = {
        "id": uuid4().hex,
        "type": "trigger",
        "sender_id": None,
        "sender_name": trigger_message.get("sender_name", "触发器"),
        "avatar": normalize_avatar(trigger_message.get("avatar")),
        "content": trigger_message.get("content", ""),
        "time": data.get("time") or time.strftime("%H:%M"),
        "created_at": _timestamp(),
        "metadata": trigger_message.get("metadata", {}),
    }
    messages = _read_messages(room_dir)
    messages.append(message)
    _write_messages(room_dir, messages)
    _write_room(room_dir, info)
    if new_trigger:
        record_trigger(room_dir, str(trigger_id), reason=str(data.get("reason") or "manual trigger"))
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "触发了场景触发器"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        触发器ID=trigger_id,
    )
    return success_response(message, "Trigger executed successfully", 201)


@bp.route("/api/rooms/<room_id>/character-records", methods=["POST"])
def create_character_record(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    return _create_character_record_for_room(room_dir, info)


def _create_character_record_for_room(room_dir, info):
    data = request.get_json(silent=True) or {}
    record_type = str(data.get("type", "")).strip().lower()
    if record_type not in {"damage", "san", "skill", "item"}:
        return error_response("Invalid record type", 400, "Invalid record type")

    target = _find_member(info, user_id=data.get("user_id"), username=data.get("username"), active_only=True)
    if not target:
        return error_response("Player not found in room", 404, "Player not found in room")
    if not target.get("character_card"):
        return error_response("Player has no bound character card", 400, "No bound character card")

    value = _bounded_int(data.get("value"), 1, 999, 1)
    reason = str(data.get("reason") or "未知").strip() or "未知"
    state = _ensure_character_state(target)
    record = {
        "id": uuid4().hex,
        "type": record_type,
        "value": value,
        "reason": reason[:200],
        "created_at": _timestamp(),
        "created_by": session.get("user_id"),
        "created_by_name": session.get("username", "user"),
    }

    if record_type == "damage":
        max_hp = max(1, _bounded_int(state.get("max_hp"), 1, 999, 1))
        before = _bounded_int(state.get("current_hp"), 0, max_hp, max_hp)
        major_wound = value >= max_hp / 2
        if value > max_hp:
            state["current_hp"] = 0
            state["dead"] = True
            state["dying"] = False
            state["unconscious"] = True
        else:
            state["current_hp"] = max(0, before - value)
        if major_wound:
            state["major_wound"] = True
        if state["current_hp"] <= 0 and not state.get("dead"):
            if state.get("major_wound"):
                state["dying"] = True
            else:
                state["unconscious"] = True
        if state.get("dead") or state.get("dying"):
            state["unconscious"] = True
        record["hp_before"] = before
        record["hp_after"] = state["current_hp"]
        record["major_wound"] = bool(state.get("major_wound"))
        record["dying"] = bool(state.get("dying"))
        record["dead"] = bool(state.get("dead"))
        state["injury_records"].insert(0, record)
        card = target.get("character_card")
        if isinstance(card, dict):
            card["currentHp"] = state["current_hp"]
            card["current_hp"] = state["current_hp"]
            status = card.get("status") if isinstance(card.get("status"), dict) else {}
            status["majorWound"] = bool(state.get("major_wound"))
            status["unconscious"] = bool(state.get("unconscious"))
            status["dead"] = bool(state.get("dead"))
            card["status"] = status
    elif record_type == "san":
        state["current_san"] = max(0, state["current_san"] - value)
        if isinstance(target.get("character_card"), dict):
            target["character_card"]["currentSan"] = state["current_san"]
            target["character_card"]["current_san"] = state["current_san"]
        record["san_after"] = state["current_san"]
        state["sanity_records"].insert(0, record)
    else:
        # skill / item change records (e.g. 技能增减、获得物品) feed the
        # "其他变化" line of the participating-module record on the player card.
        record["target"] = str(data.get("target") or record.get("name") or "").strip()[:80]
        record["change"] = str(data.get("change") or data.get("operation") or data.get("action") or "").strip()[:40] or "变化"
        record["amount"] = data.get("amount")
        collection = "skill_changes" if record_type == "skill" else "item_changes"
        state.setdefault(collection, []).insert(0, record)

    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "记录了角色状态变化"),
        actor=session.get("username"),
        room_id=info.get("id"),
        logger_=logger,
        用户ID=session.get("user_id"),
        类型=record_type,
        目标玩家=target.get("username"),
        数值=value,
        原因=reason,
    )
    return success_response({"member": target, "record": record}, "Character record created", 201)


@bp.route("/api/rooms/by-name/<path:room_name>/character-records", methods=["POST"])
def create_character_record_by_room_name(room_name):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room_by_name(room_name)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    return _create_character_record_for_room(room_dir, info)


@bp.route("/api/rooms/<room_id>/character-records/<record_id>", methods=["DELETE"])
def delete_character_record(room_id, record_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if not _is_elevated():
        return _denied(room_id)

    for member in info.get("members", []):
        state = _ensure_character_state(member)
        for collection_name in ("injury_records", "sanity_records", "skill_changes", "item_changes"):
            records = state.get(collection_name, [])
            next_records = [record for record in records if record.get("id") != record_id]
            if len(next_records) != len(records):
                state[collection_name] = next_records
                _recalculate_character_state(state)
                _write_room(room_dir, info)
                log_room_event(
                    room_dir,
                    user_action_text(session.get("username"), "删除了角色状态记录"),
                    actor=session.get("username"),
                    room_id=room_id,
                    logger_=logger,
                    用户ID=session.get("user_id"),
                    记录ID=record_id,
                )
                return success_response({"member": member}, "Character record deleted")

    return error_response("Record not found", 404, "Record not found")


@bp.route("/api/rooms/<room_id>/nodes", methods=["GET"])
def list_room_nodes(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    nodes_dir = room_dir / "nodes"
    nodes = []
    for node_file in nodes_dir.glob("*.json") if nodes_dir.exists() else []:
        if node_file.name == "autosave.json":
            continue
        node = read_json(node_file, default={})
        nodes.append(
            {
                "filename": node_file.name,
                "created_at": node.get("created_at", ""),
                "message_count": len(node.get("messages", [])),
                "automatic": bool(node.get("automatic")),
            }
        )
    nodes.sort(key=lambda item: item["created_at"], reverse=True)
    return success_response({"info": _room_summary(info), "nodes": nodes}, "Room nodes loaded successfully")


@bp.route("/api/rooms/<room_id>/nodes", methods=["POST"])
def create_room_node(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    timestamp = int(time.time() * 1000)
    node = {
        "filename": f"{timestamp}.json",
        "created_at": _timestamp(),
        "automatic": False,
        "messages": _read_messages(room_dir),
    }
    write_json_atomic(room_dir / "nodes" / node["filename"], node)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "创建了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node["filename"],
    )
    return success_response(node, "Room node created successfully", 201)


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["GET"])
def get_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")
    return success_response(read_json(node_file, default={}), "Room node loaded successfully")


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>/restore", methods=["POST"])
def restore_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    if room_interlude.is_active(info):
        return error_response("Interlude is active", 409, "幕间期间不能回溯存档节点")

    node_file = safe_join(room_dir / "nodes", node_filename)
    if not node_file.exists():
        return error_response("Room node not found", 404, "Room node not found")

    node = read_json(node_file, default={"messages": []})
    _write_messages(room_dir, node.get("messages", []))
    _write_room(room_dir, info)
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "恢复了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node_filename,
    )
    return success_response(node, "Room node restored successfully")


@bp.route("/api/rooms/<room_id>/nodes/<node_filename>", methods=["DELETE"])
def delete_room_node(room_id, node_filename):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)

    node_file = safe_join(room_dir / "nodes", node_filename)
    if node_file.exists():
        node_file.unlink()
    log_room_event(
        room_dir,
        user_action_text(session.get("username"), "删除了房间回档节点"),
        actor=session.get("username"),
        room_id=room_id,
        logger_=logger,
        用户ID=session.get("user_id"),
        文件=node_filename,
    )
    return success_response(message="Room node deleted successfully")


def _keyframe_for_autosave(room_dir):
    """自动存档前写入关键帧（§6.4.2）；失败只记日志，不阻断存档。"""
    try:
        from trpg_server.agents.narrative.state_store import write_keyframe

        state = read_json(Path(room_dir) / "state.json", default={})
        if isinstance(state, dict) and isinstance(state.get("narrative"), dict) and state["narrative"]:
            write_keyframe(room_dir, reason="autosave")
    except Exception:  # noqa: BLE001 - 关键帧失败不影响自动存档
        logger.warning("narrative.keyframe.autosave_failed room=%s", getattr(room_dir, "name", room_dir), exc_info=True)


def _write_room_autosave(room_dir, room_id, messages, *, username=None, user_id=None):
    """写入自动存档节点并裁剪旧节点。

    ``username`` 为空表示由服务端后台调度触发（记录为系统动作）；传入时保留
    玩家手动触发路径的操作日志。
    """
    timestamp = int(time.time() * 1000)
    autosave = {
        "filename": f"autosave-{timestamp}.json",
        "updated_at": _timestamp(),
        "created_at": _timestamp(),
        "automatic": True,
        "messages": messages,
    }
    _keyframe_for_autosave(room_dir)
    nodes_dir = room_dir / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(nodes_dir / autosave["filename"], autosave)
    write_json_atomic(room_dir / "autosave.json", autosave)
    automatic_nodes = []
    for node_file in nodes_dir.glob("autosave-*.json"):
        node = read_json(node_file, default={})
        if node.get("automatic"):
            automatic_nodes.append((str(node.get("created_at") or ""), node_file))
    automatic_nodes.sort(key=lambda item: item[0], reverse=True)
    for _, old_file in automatic_nodes[_autosave_node_limit():]:
        old_file.unlink(missing_ok=True)
    if username:
        log_room_event(
            room_dir,
            user_action_text(username, "保存了房间自动存档"),
            actor=username,
            room_id=room_id,
            logger_=logger,
            level="debug",
            用户ID=user_id,
            消息数=len(messages),
        )
    else:
        log_room_event(
            room_dir,
            "系统保存了房间自动存档",
            room_id=room_id,
            logger_=logger,
            level="debug",
            消息数=len(messages),
        )
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit("room_autosave_created", {"room_id": room_id, "filename": autosave["filename"]}, room=room_id)
    return autosave


def run_scheduled_autosaves():
    """后台调度入口：为开启自动存档且到期的房间写入自动存档节点。

    由 app_factory 启动的守护线程周期调用，需要处于 Flask 应用上下文中。
    """
    settings = _autosave_settings()
    if not settings["enabled"]:
        return
    interval = settings["interval"]
    now = time.time()
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if not info or info.get("archived"):
            continue
        # 仅当房间内确实有「已进入该房间且在线」的玩家时才自动存档：
        # 玩家在线但没进入这个房间，不应对该房间产生自动存档。
        if not _room_has_online_member(info):
            continue
        autosave_path = room_dir / "autosave.json"
        try:
            if autosave_path.exists() and now - autosave_path.stat().st_mtime < interval:
                continue
        except OSError:
            continue
        messages = _read_messages(room_dir)
        if not messages:
            continue
        room_id = info.get("id") or room_dir.name
        try:
            _write_room_autosave(room_dir, room_id, messages)
        except OSError:
            logger.debug("Scheduled autosave failed for room %s", room_id, exc_info=True)


def save_all_rooms_on_shutdown():
    """服务正常关闭（stop/exit 等）前为每个房间写一个自动存档节点。

    与后台调度不同：忽略 ``[autosave] enabled`` 与间隔限制，确保退出前最后
    一段对话一定落盘；内容与最近一次自动存档完全相同的房间跳过，避免产生
    无意义的重复节点。需要处于 Flask 应用上下文中。
    """

    saved = 0
    for room_dir in _iter_room_dirs():
        info = _read_room(room_dir)
        if not info or info.get("archived"):
            continue
        messages = _read_messages(room_dir)
        if not messages:
            continue
        existing = read_json(room_dir / "autosave.json", default={})
        if isinstance(existing, dict) and existing.get("messages") == messages:
            continue
        room_id = info.get("id") or room_dir.name
        try:
            _write_room_autosave(room_dir, room_id, messages)
            saved += 1
        except Exception:  # noqa: BLE001 - 单个房间失败不能阻断关闭流程
            logger.warning("Shutdown autosave failed for room %s", room_id, exc_info=True)
    if saved:
        logger.info("Saved shutdown autosaves for %d room(s)", saved)
    return saved


@bp.route("/api/rooms/<room_id>/autosave", methods=["POST"])
def save_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = _write_room_autosave(
        room_dir,
        room_id,
        _read_messages(room_dir),
        username=session.get("username"),
        user_id=session.get("user_id"),
    )
    return success_response(autosave, "Room autosave saved successfully")


@bp.route("/api/rooms/<room_id>/autosave", methods=["GET"])
def load_room_autosave(room_id):
    login_error = _require_login()
    if login_error:
        return login_error

    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)

    autosave = read_json(room_dir / "autosave.json", default={"messages": []})
    return success_response(autosave, "Room autosave loaded successfully")


# ---------------------------------------------------------------------------
# 幕间：显式房间阶段。旧房间没有 interlude 字段时快照返回 active=False。
# ---------------------------------------------------------------------------

def _interlude_payload(info):
    state = room_interlude.interlude_state(info)
    return {
        "state": room_interlude.snapshot(info),
        "settlement": state.get("settlement") or {},
        "reviews": state.get("reviews") or [],
        "can_manage": _can_manage(info),
    }


def _emit_interlude_updated(room_id, info):
    socketio = current_app.extensions.get("socketio")
    if socketio is not None:
        socketio.emit(
            "room_interlude_updated",
            {"room_id": room_id, "interlude": room_interlude.snapshot(info)},
            room=str(room_id),
        )


@bp.route("/api/rooms/<room_id>/interlude", methods=["GET"])
def get_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    return success_response(_interlude_payload(info), "Interlude loaded")


@bp.route("/api/rooms/<room_id>/interlude/request", methods=["POST"])
def request_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    try:
        record = room_interlude.request_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source=str(data.get("source") or "player"),
            reason=str(data.get("reason") or ""),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude request failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(_interlude_payload(info) | {"request": record}, "Interlude requested")


@bp.route("/api/rooms/<room_id>/interlude/start", methods=["POST"])
def start_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    credit_events = data.get("credit_events") if isinstance(data.get("credit_events"), list) else []
    try:
        outcome = room_interlude.start_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            credit_events=credit_events,
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude start failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(
        {
            **_interlude_payload(info),
            "settlement": outcome.get("settlement") or {},
            "kp_prompt": room_interlude.interlude_opening_prompt(),
        },
        "Interlude started",
    )


@bp.route("/api/rooms/<room_id>/interlude/settle", methods=["POST"])
def settle_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    credit_events = data.get("credit_events") if isinstance(data.get("credit_events"), list) else []
    try:
        settlement = room_interlude.settle_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            credit_events=credit_events,
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude settle failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response(
        {**_interlude_payload(info), "settlement": settlement},
        "Interlude settled",
    )


@bp.route("/api/rooms/<room_id>/interlude/review", methods=["POST"])
def add_room_interlude_review(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_access(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    try:
        record = room_interlude.add_review(
            room_dir,
            info,
            content=str(data.get("content") or ""),
            topic=str(data.get("topic") or ""),
            actor=session.get("username"),
            actor_id=session.get("user_id"),
            source=str(data.get("source") or "player"),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude review failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    return success_response({**_interlude_payload(info), "review": record}, "Interlude review saved")


@bp.route("/api/rooms/<room_id>/interlude/end", methods=["POST"])
def end_room_interlude(room_id):
    login_error = _require_login()
    if login_error:
        return login_error
    room_dir, info = _find_room(room_id)
    if not room_dir:
        return error_response("Room not found", 404, "Room not found")
    if not _can_manage(info):
        return _denied(room_id)
    data = request.get_json(silent=True) or {}
    confirmed = bool(data.get("confirm")) or request.args.get("confirm") in {"1", "true", "yes"}
    if not confirmed:
        return error_response(
            "Confirmation required",
            409,
            "结束幕间会自动归档房间。确认后请再次提交，并带上 confirm=true。",
        )
    try:
        room_interlude.end_interlude(
            room_dir,
            info,
            actor=session.get("username"),
            actor_id=session.get("user_id"),
        )
    except room_interlude.InterludeError as exc:
        return error_response("Interlude end failed", exc.status, exc.message)
    _emit_interlude_updated(room_id, info)
    from trpg_server import room_admin

    try:
        archive = room_admin.archive_room(_get_rooms_dir(), room_id, app=current_app._get_current_object())
    except Exception as exc:  # noqa: BLE001 - 幕间已结束，归档失败需明确上报
        logger.exception("Failed to archive room %s after interlude", room_id)
        return error_response("Room archive failed", 500, f"幕间已结束，但自动归档失败：{exc}")
    return success_response(
        {**_interlude_payload(info), "archive": {"archived_at": archive.get("archived_at")}},
        "Interlude ended and room archived",
    )
