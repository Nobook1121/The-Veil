"""房间房规常量与规范化：BGM、技能基础值、骰子阈值、沉浸开关。"""
from pathlib import Path
from trpg_server.agents.config import load_ai_runtime_config
from trpg_server.agents.dice_settings import resolve_dice_settings
from trpg_server.immersive.config import AUDIO_BLOCKS
from trpg_server.immersive.config import MUSIC_BLOCKS
from trpg_server.immersive.config import MUSIC_BLOCK_PRIORITY
from trpg_server.room_ops.constants import (
    HOUSE_RULE_DEFINITIONS,
    _BGM_AUDIO_SUFFIXES,
    _BGM_LOCAL_PREFIXES,
)
from trpg_server.room_ops.storage import _get_config_dir

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
