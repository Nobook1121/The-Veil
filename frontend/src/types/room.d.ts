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
    scenario_started_at?: string;
    scenario_started_by?: string | number;
    visibility?: "public" | "private";
    invisible_view?: boolean;
    archived?: boolean;
    completed_at?: string;
    archived_at?: string;
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
    // 房间级技能基础值覆盖（键为 skillKey 或 skillKey.specialtyKey）。
    skill_bases?: Record<string, number>;
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

interface RoomConsoleSocketPayload {
    room_id?: string;
    type?: string;
    entry?: RoomConsoleEntry;
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
