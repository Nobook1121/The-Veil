interface RoomMember {
    user_id?: string | number;
    username?: string;
    avatar?: string;
    role?: string;
    room_role?: "owner" | "admin" | "member";
    status?: "active" | "removed";
    is_active?: boolean;
    permission_label?: string;
    character_card?: Partial<COC7CharacterCard>;
    character_state?: CharacterRuntimeState;
    is_online?: boolean;
}

interface CharacterRuntimeRecord {
    id: string;
    type: "damage" | "san";
    value: number;
    reason: string;
    created_at?: string;
    created_by?: string | number;
}

interface CharacterRuntimeState {
    current_hp?: number;
    max_hp?: number;
    current_san?: number;
    max_san?: number;
    injury_records?: CharacterRuntimeRecord[];
    sanity_records?: CharacterRuntimeRecord[];
    records?: CharacterRuntimeRecord[];
    // 战斗轮写入的伤势状态（COC7 重伤/昏迷/濒死/死亡）。
    major_wound?: boolean;
    unconscious?: boolean;
    dying?: boolean;
    dead?: boolean;
    first_aid_used?: boolean;
}

interface RoomUsage {
    room_id?: string;
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    cached_tokens?: number;
    costs: Record<string, number>;
    updated_at?: string;
}

interface RoomInterludeState {
    active: boolean;
    startedAt?: string | null;
    startedBy?: string;
    completedAt?: string | null;
    settledAt?: string | null;
    reviewCount?: number;
    request?: {
        id?: string;
        at?: string;
        by?: string;
        byId?: string;
        source?: string;
        reason?: string;
    } | null;
}

interface Room {
    id: string;
    code?: string;
    room_code?: string;
    name: string;
    created_at?: string;
    creator_id?: string | number;
    owner_id?: string | number;
    scenario_id?: number;
    scenario_title?: string;
    scenario_version?: string;
    // 房间级唯一规则集；旧房间由后端按剧本推断，推断不到回退 coc7。
    ruleset_id?: string;
    // 房间模式：预设名 + 两个正交能力位。旧房间由后端补默认值（scripted / true / required）。
    room_mode?: "scripted" | "freeform" | "manual";
    ai_kp_enabled?: boolean;
    scenario_binding?: "required" | "optional" | "none";
    scenario_started_at?: string;
    scenario_started_by?: string | number;
    visibility?: "public" | "private";
    invisible_view?: boolean;
    archived?: boolean;
    completed_at?: string;
    archived_at?: string;
    // 显式幕间阶段；旧房间该字段缺省或 active=false 时按非幕间处理。
    interlude?: RoomInterludeState;
    members?: RoomMember[];
    messages?: ChatMessage[];
    ai_history?: ChatMessage[];
    house_rules?: RoomHouseRules;
    // 全房间统一的骰娘大成功/大失败阈值（房规优先，其次管理员默认值）。
    dice_thresholds?: { critical: number; fumble: number };
    // 房间生效的骰子动画档位与玩家投掷开关（房规优先，管理员默认兜底）。
    dice_animation?: {
        level: "text" | "simple" | "3d";
        player_roll: boolean;
        timeout: number;
        reroll_mode: "off" | "limited" | "unlimited";
        reroll_limit: number;
        rerolls: number;
        level_source: "room" | "admin";
        player_roll_source: "room" | "admin";
        reroll_source: "room" | "admin";
        // 结果来源：物理骰面（默认）或服务端随机数生成器。
        physics_results: boolean;
        result_source: "physics" | "server";
    };
    saves?: Array<{ filename: string; title?: string; created_at?: string }>;
    // 房主/房间管理员可见的累计 AI 用量与花费；其他成员看不到该字段。
    usage?: RoomUsage;
}

interface RoomHouseRules {
    action_suggestions_enabled?: boolean;
    // 留空（null）表示沿用管理员设置页配置的默认阈值。
    dice_critical_threshold?: number | null;
    dice_fumble_threshold?: number | null;
    // 骰子动画档位与玩家投掷：null 表示跟随管理员默认；两者相互独立。
    dice_animation_level?: "text" | "simple" | "3d" | null;
    player_dice_roll?: boolean | null;
    // 重投模式与次数：null 表示跟随管理员默认；次数仅在 limited 时生效。
    dice_reroll_mode?: "off" | "limited" | "unlimited" | null;
    dice_reroll_limit?: number | null;
    // 实时沉浸系统：null 表示跟随管理员设置；房间只能收紧（关闭），不能越过站点总开关。
    immersive_music_enabled?: boolean | null;
    immersive_music_auto_switch?: boolean | null;
    // 旧版音乐引擎：仅用于旧数据迁移。
    immersive_music_engine?: "external" | "procedural" | "off" | null;
    // 音频块组合：环境音可叠加，配乐四选一；null 表示未配置（沿用管理员默认组合）。
    immersive_audio_blocks?: string[] | null;
    immersive_illustration_enabled?: boolean | null;
    immersive_illustration_auto?: boolean | null;
    // 剧本配图缓存：房间只能关闭/不优先，不能越过站点开关重新开启。
    immersive_scenario_record?: boolean | null;
    immersive_scenario_prefer?: boolean | null;
    // 房间背景音乐播放列表（0~20 首，可为音乐库站内地址或 http(s) 外链）。
    bgm_urls?: string[];
    // 播放模式：列表循环 / 单曲循环 / 随机播放。
    bgm_mode?: "list" | "single" | "shuffle";
    // 房间级技能基础值覆盖（键为 skillKey 或 skillKey.specialtyKey）。
    skill_bases?: Record<string, number>;
    // 世界意志直执行：房间开关与额外允许使用的成员白名单（站点管理员不受开关影响）。
    world_override_enabled?: boolean;
    world_override_user_ids?: string[];
    // CoC7 批次 1 房规开关：关闭时对应运行时入口不存在。
    coc7_social_difficulty?: boolean;
    coc7_combined_checks?: boolean;
    coc7_group_luck?: boolean;
    coc7_economy?: boolean;
    coc7_contacts?: boolean;
    coc7_age_adjustments?: boolean;
    // 战斗规则房规：默认值与核心规则一致；新增细节开关默认关闭。
    combat_surprise_model?: "bonus_die" | "auto_success";
    combat_ready_weapon_cost?: "free" | "action" | "round";
    combat_cast_interrupt?: "keeper" | "any_damage" | "never";
    combat_cast_defense_allowed?: boolean;
    combat_major_wound_penalty?: "none" | "cannot_act";
    combat_shield_action?: "disabled" | "counter_alias";
    combat_dex_extra_action_threshold?: number | null;
    combat_dying_check?: boolean;
    // CoC7 批次 4 战斗细节开关：默认关闭。
    coc7_combat_aim_tracking?: boolean;
    coc7_combat_cover_dodge?: boolean;
    coc7_combat_friendly_fire?: boolean;
    coc7_combat_knockout?: boolean;
    coc7_combat_hit_locations?: boolean;
    coc7_combat_poison?: boolean;
    coc7_healing_natural?: boolean;
    coc7_healing_require_rest?: boolean;
}

interface RoomConsoleEntry {
    ts?: string;
    level?: string;
    source?: string;
    actor?: string | null;
    room_id?: string;
    message?: string;
    details?: Record<string, unknown>;
}

interface RoomAiChangeRecord {
    id: string;
    at?: string;
    actor?: string;
    tool?: string;
    player_name?: string;
    field?: string;
    label?: string;
    name?: string;
    before?: unknown;
    after?: unknown;
    reason?: string;
    rolled_back?: boolean;
    rolled_back_at?: string | null;
    rolled_back_by?: string | null;
}

interface RoomConsoleSocketPayload {
    room_id?: string;
    type?: string;
    entry?: RoomConsoleEntry;
}

/** 房间快照相对持久卡的一处差异（用于卡面「房间内临时」标记）。 */
interface RoomCardDiffEntry {
    current: number;
    saved: number | null;
}

interface RoomCardDiff {
    attributes?: Record<string, RoomCardDiffEntry>;
    skills?: Record<string, RoomCardDiffEntry>;
    vitals?: {
        hp?: RoomCardDiffEntry;
        san?: RoomCardDiffEntry;
        mp?: RoomCardDiffEntry;
    };
    cash?: RoomCardDiffEntry;
    items?: Record<string, RoomCardDiffEntry>;
    weapons?: Record<string, RoomCardDiffEntry>;
}

interface RoomConsoleOpenOptions {
    canExecute?: boolean;
    archived?: boolean;
    invisibleView?: boolean;
}

interface ChatMessage {
    id?: string;
    role?: string;
    type?: string;
    content: string;
    sender?: string;
    sender_id?: string | number | null;
    sender_name?: string;
    senderName?: string;
    avatar?: string;
    timestamp?: string;
    time?: string;
    processing_time?: number;
    token_count?: number;
    metadata?: Record<string, unknown>;
}

interface CharacterRecordPayload extends Record<string, unknown> {
    roomId?: string;
    roomName?: string;
    username?: string;
    type?: "damage" | "san";
    value?: number;
    reason?: string;
}
