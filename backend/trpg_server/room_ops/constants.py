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
    # 幕间训练（批次 2 可选规则）：KP 批准投入周数换成长检定；默认关闭。
    "coc7_growth_training": {"type": "boolean", "default": False, "ai_prompt": False},
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
    # 批次 4 战斗细节（默认全部关闭；关闭时射击/近战流程与旧版本完全一致）。
    # 瞄准跟踪：须上一轮声明，下一轮射击获得 1 奖励骰；受伤或移动后失效。
    "coc7_combat_aim_tracking": {"type": "boolean", "default": False, "ai_prompt": False},
    # 掩体与闪避：目标借掩体闪避成功后，攻击者失去下一次攻击。
    "coc7_combat_cover_dodge": {"type": "boolean", "default": False, "ai_prompt": False},
    # 射击入近战：额外 1 惩罚骰；大失败命中同方幸运最低的同伴。
    "coc7_combat_friendly_fire": {"type": "boolean", "default": False, "ai_prompt": False},
    # 击晕：钝器战技成功改为造成 1 点伤害并击晕（1D10 轮）。
    "coc7_combat_knockout": {"type": "boolean", "default": False, "ai_prompt": False},
    # 部位命中：每次伤害掷 1D20 定位，用于部位重伤描述。
    "coc7_combat_hit_locations": {"type": "boolean", "default": False, "ai_prompt": False},
    # 毒剂：KP 给毒性/伤害/发作轮数，系统做 CON 抵抗与伤害结算。
    "coc7_combat_poison": {"type": "boolean", "default": False, "ai_prompt": False},
    # 自然恢复：开放 rule heal natural，必须显式给出休养天数。
    "coc7_healing_natural": {"type": "boolean", "default": False, "ai_prompt": False},
    # 自然恢复必须声明休养；关闭时不强制 rest。
    "coc7_healing_require_rest": {"type": "boolean", "default": False, "ai_prompt": False},
    # 房间游戏时钟：按服务器时间累计房间内经过的时间（默认关闭）。
    # 开启后 rule heal natural / spell grimoire / heal mp / 毒剂到期 / 幕间疗养
    # 可以用「当前时间 − 上次结算时间」代替显式天数，显式参数仍然优先。
    "coc7_game_clock": {"type": "boolean", "default": False, "ai_prompt": False},
    # 追逐：开放地点 / 行动点 / 险境 / 障碍 / 冲突的追逐会话（默认关闭）。
    "coc7_chase_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 法术与典籍：施法消耗 / 首次施法检定 / 典籍阅读结算（默认关闭，不含内容表）。
    "coc7_magic_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 法术 / 典籍内容表：按需查询规则书条目（默认关闭，不影响施法结算）。
    "coc7_magic_catalog_enabled": {"type": "boolean", "default": False, "ai_prompt": False},
    # 批次 3 理智结构化（总开关；关闭时理智链路与旧版本逐字一致）。
    "coc7_sanity_state": {"type": "boolean", "default": False, "ai_prompt": False},
    # 目击神话：首次因神话疯狂时克苏鲁神话 +5，之后每次 +1。
    "coc7_sanity_mythos": {"type": "boolean", "default": False, "ai_prompt": False},
    # 习惯恐惧：按来源累计理智损失，上限为该存在单次遭遇最大损失；幕间 −1。
    "coc7_sanity_habituation": {"type": "boolean", "default": False, "ai_prompt": False},
    # 现实认知：理智检定（目标值为当前 SAN）成功看穿幻象，获得对幻觉的抗性直到下次失去理智值。
    "coc7_sanity_reality": {"type": "boolean", "default": False, "ai_prompt": False},
    # 发作计时：即时症状 1D10 战斗轮 / 总结症状 1D10 小时，写入可查询的 bout。
    "coc7_sanity_bout": {"type": "boolean", "default": False, "ai_prompt": False},
    # 即时症状收尾安全阀（分钟）。不是规则书数值：规则书只规定即时症状持续 1D10 战斗轮，
    # 系统没有按战斗轮推进的时钟，故用挂钟分钟数兜底收尾，防止 KP 忘记收尾导致永久免疫。
    # 0 = 关闭兜底（完全按原逻辑，需要 KP / rule san bout end 显式收尾）；留空按默认 60。
    "coc7_sanity_bout_safety_minutes": {
        "type": "nullable_integer",
        "default": 60,
        "min": 0,
        "max": 1440,
        "ai_prompt": False,
    },
    # 恐惧 / 躁狂 100 条表：按需查表，暴露来源时检定吃惩罚骰。
    "coc7_sanity_traits": {"type": "boolean", "default": False, "ai_prompt": False},
}

DEFAULT_ROOM_HOUSE_RULES = {key: spec["default"] for key, spec in HOUSE_RULE_DEFINITIONS.items()}

_BGM_LOCAL_PREFIXES = ("/assets/bgm/builtin/", "/assets/bgm/library/")

_BGM_AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".webm", ".opus"}
