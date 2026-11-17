"""房间房规常量与默认值（仅数据，无逻辑）。"""
from trpg_server.immersive.config import AUDIO_BLOCKS
from trpg_server.agents.dice_settings import DICE_ANIMATION_LEVELS
from trpg_server.agents.dice_settings import DICE_REROLL_MODES

USER_ROOM_LIMIT = 3

ROOM_ROLE_OWNER = "owner"

ROOM_ROLE_ADMIN = "admin"

ROOM_ROLE_MEMBER = "member"

ROOM_MEMBER_ACTIVE = "active"

ROOM_MEMBER_REMOVED = "removed"

ROOM_VISIBILITY_PRIVATE = "private"

ROOM_VISIBILITY_PUBLIC = "public"

ROOM_VISIBILITIES = {ROOM_VISIBILITY_PRIVATE, ROOM_VISIBILITY_PUBLIC}

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
    # 叙事图与守卫：跨规则集的房间级选项。
    "narrative_enabled": {"type": "nullable_boolean", "default": None, "ai_prompt": False},
    "narrative_profile": {"type": "nullable_enum", "default": None, "values": ["graph", "degraded"], "ai_prompt": False},
    "narrative_guard": {"type": "nullable_enum", "default": None, "values": ["off", "shadow", "enforce"], "ai_prompt": False},
    # 世界意志直执行：房间级开关、成员白名单与限流。与规则集无关。
    "world_override_enabled": {"type": "boolean", "default": True, "ai_prompt": False},
    "world_override_user_ids": {"type": "user_ids", "default": [], "max_items": 50, "ai_prompt": False},
    "world_override_cooldown_seconds": {"type": "nullable_integer", "default": None, "min": 0, "max": 3600, "ai_prompt": False},
    "world_override_hourly_limit": {"type": "nullable_integer", "default": None, "min": 0, "max": 10000, "ai_prompt": False},
    "rule_persist_on_archive": {"type": "boolean", "default": True, "ai_prompt": False},
}

# 兼容别名：核心定义只包含跨规则集设置；CoC7 字段由插件
# ruleset/coc7/house_rules.py 声明。
CORE_HOUSE_RULE_DEFINITIONS = HOUSE_RULE_DEFINITIONS
DEFAULT_ROOM_HOUSE_RULES = {key: spec["default"] for key, spec in HOUSE_RULE_DEFINITIONS.items()}

_BGM_LOCAL_PREFIXES = ("/assets/bgm/builtin/", "/assets/bgm/library/")

_BGM_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".webm", ".opus"}
