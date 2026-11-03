/**
 * 实时沉浸系统（配乐 / 配图）的前端共享类型。
 *
 * 后端契约见 `backend/trpg_server/routes/immersive.py`：
 * - 配乐只下发「导演指令」（档位/强度/段落/种子），声音由 Web Audio 现场合成；
 * - 配图下发缓存 URL（程序化 SVG 或外部生成图），失败时 `degraded=true`。
 */
type ImmersiveIntensity = "low" | "medium" | "high";
type ImmersiveProfile =
    | "daily"
    | "suspense"
    | "unease"
    | "horror"
    | "urgency"
    | "sorrow"
    | "awe"
    | "aftermath";

interface ImmersiveChord {
    root: number;
    quality: string;
    intervals: number[];
}

interface ImmersiveScoreSection {
    label: string;
    dynamic: ImmersiveIntensity;
    bars: number;
}

/** LLM 音乐导演的乐谱参数（后端 `immersive.director_schema.validate_score` 白名单化后下发）。 */
interface ImmersiveScore {
    scene: string;
    mood: ImmersiveProfile;
    mode: string;
    tempo: number;
    chords: ImmersiveChord[];
    instrumentation: string[];
    motif: number[];
    sections: ImmersiveScoreSection[];
    dynamics: number[];
    confidence: number;
}

interface ImmersiveDirectorMeta {
    fingerprint?: string | null;
    model?: string | null;
    platform?: string | null;
    confidence?: number | null;
    cached?: boolean | null;
    section_index?: number | null;
    at?: string | null;
}

interface ImmersiveMusicPlan {
    kind?: "music";
    room_id?: string;
    profile: ImmersiveProfile;
    intensity: ImmersiveIntensity;
    section: number;
    seed: string;
    motif?: string;
    crossfade_ms?: number;
    /** 0~1，来自管理员音量设置。 */
    volume?: number;
    duck_bgm?: boolean;
    scene?: string;
    reason?: string;
    changed?: boolean;
    auto_switch?: boolean;
    turns?: number;
    analysis?: {
        profile?: string;
        intensity?: string;
        scene?: string;
        confidence?: string;
        reason?: string;
    };
    /** LLM 导演乐谱；为空时按 profile 规则合成。 */
    score?: ImmersiveScore | null;
    director?: ImmersiveDirectorMeta | null;
    history?: Array<Record<string, unknown>>;
}

interface ImmersiveIllustrationItem {
    id?: string | undefined;
    url: string;
    filename?: string | undefined;
    scene?: string | undefined;
    profile?: string | undefined;
    intensity?: string | undefined;
    provider?: string | undefined;
    degraded?: boolean | undefined;
    error?: string | undefined;
    fallback?: boolean | undefined;
    cached?: boolean | undefined;
    elapsed_ms?: number | undefined;
    turn?: number | undefined;
    seed?: string | undefined;
    created_at?: string | undefined;
}

interface ImmersiveIllustrationEvent extends Partial<ImmersiveIllustrationItem> {
    kind?: "illustration";
    status?: "pending" | "ready" | "error";
    room_id?: string;
}

interface ImmersiveSettings {
    enabled: boolean;
    music_enabled: boolean;
    music_auto_switch: boolean;
    music_default_profile: ImmersiveProfile;
    music_default_intensity: ImmersiveIntensity;
    music_volume: number;
    music_crossfade_ms: number;
    music_hysteresis_turns: number;
    music_seed: string;
    music_duck_bgm: boolean;
    music_director_enabled: boolean;
    music_director_platform: string;
    music_director_model: string;
    music_director_interval_turns: number;
    music_director_daily_budget: number;
    music_director_timeout: number;
    music_director_cache_ttl: number;
    music_director_min_confidence: number;
    music_director_fallback: "rule" | "hold";
    music_director_max_context_chars: number;
    illustration_enabled: boolean;
    illustration_provider: "procedural" | "platform" | "openai_compatible";
    illustration_platform_id: string;
    illustration_auto: boolean;
    illustration_timeout: number;
    illustration_concurrency: number;
    illustration_daily_budget: number;
    illustration_room_hourly_cap: number;
    illustration_min_turns: number;
}

interface ImmersiveImagePlatform {
    id: string;
    name: string;
    protocol: string;
    base_url: string;
    model: string;
    size: string;
    timeout: number;
    enabled: boolean;
    has_api_key: boolean;
    api_key_masked?: string;
    is_default?: boolean;
    created_at?: string;
    updated_at?: string;
}

interface ImmersiveImagePreset {
    preset: string;
    name: string;
    protocol: string;
    base_url: string;
    model: string;
    size: string;
    timeout: number;
}

interface ImmersiveProtocolOption {
    value: string;
    label: string;
}

interface ImmersiveDirectorCacheStats {
    entries?: number;
    hits?: number;
    expires_after_seconds?: number;
}

interface ImmersiveDirectorUsage {
    date?: string;
    calls?: number;
}

interface ImmersivePlatformPayload {
    platforms: ImmersiveImagePlatform[];
    presets: ImmersiveImagePreset[];
    protocols: ImmersiveProtocolOption[];
    default_platform_id: string;
    director?: {
        usage?: ImmersiveDirectorUsage;
        cache?: ImmersiveDirectorCacheStats;
        fallbacks?: string[];
    };
}

interface ImmersiveRoomSwitches {
    music: boolean;
    music_auto: boolean;
    illustration: boolean;
    illustration_auto: boolean;
}

interface ImmersiveMusicState {
    profile?: ImmersiveProfile | null;
    intensity?: ImmersiveIntensity | null;
    section?: number | null;
    turns?: number | null;
    base_seed?: string | null;
    scene?: string | null;
    motif?: string | null;
    crossfade_ms?: number | null;
    volume?: number | null;
    duck_bgm?: boolean | null;
    last_reason?: string | null;
    score?: ImmersiveScore | null;
    director?: ImmersiveDirectorMeta | null;
    history?: Array<Record<string, unknown>>;
}

interface ImmersiveRoomState {
    room_id?: string;
    switches?: ImmersiveRoomSwitches;
    music?: ImmersiveMusicState;
    illustration?: {
        last_turn?: number | null;
        history?: ImmersiveIllustrationItem[];
    };
    usage?: {
        date?: string;
        daily_used?: number;
        daily_budget?: number;
        rooms?: Record<string, unknown>;
    };
    config?: ImmersiveSettings;
    can_manage?: boolean;
}

interface TrpgImmersiveMusicApi {
    apply: (plan: ImmersiveMusicPlan) => void;
    stop: () => void;
    setMuted: (muted: boolean) => void;
    isMuted: () => boolean;
    toggleMuted: () => boolean;
    unlock: () => boolean;
    isRunning: () => boolean;
}

interface TrpgImmersiveApi {
    init: () => void;
    setRoom: (room: Room | null) => void;
    refreshState: () => void;
    onMusicEvent: (plan: ImmersiveMusicPlan) => void;
    onIllustrationEvent: (payload: ImmersiveIllustrationEvent) => void;
    renderSettings: () => void;
    requestIllustration: () => void;
    toggleMusic: () => void;
    syncMusicButton: () => void;
}

interface Window {
    TrpgImmersiveMusic?: TrpgImmersiveMusicApi;
    TrpgImmersive?: TrpgImmersiveApi;
}
