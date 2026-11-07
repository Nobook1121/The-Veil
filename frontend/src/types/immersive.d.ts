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
interface ImmersiveMotifVariant {
    degrees: number[];
    rhythm?: number[];
    role?: "statement" | "answer" | "climax" | "fragment";
}

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
    /** 可选：导演指定的持续低音（绝对音高类 0~11），缺失时按档位规则推导。 */
    pedal?: number | null;
    /** 可选：导演给出的多个动机变体；缺失时回落到 `motif` 或档位动机库。 */
    motif_variants?: ImmersiveMotifVariant[];
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
    /** 站点/房间收紧后的生效音乐来源。 */
    music_source?: "realtime" | "bgm" | "hybrid";
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
    /** 服务端给出的房间级播放位置；缺失时按本地进度播放（旧服务端兼容）。 */
    sync?: ImmersiveMusicSync | null;
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
    /** scene（场景图）或 portrait（NPC 头像）；用于避免两类图混命中与混展示。 */
    category?: "scene" | "portrait" | string | undefined;
    npc_name?: string | undefined;
    /** 缓存命中层级：scenario / room / miss。 */
    cache_level?: string | undefined;
}

interface ImmersiveIllustrationEvent extends Partial<ImmersiveIllustrationItem> {
    kind?: "illustration";
    status?: "pending" | "ready" | "deciding" | "generating" | "skipped" | "complete" | "error";
    room_id?: string;
    /** 「正在思考」占位里展示的阶段文案。 */
    label?: string;
    reason?: string;
    retryable?: boolean;
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
    music_source: "realtime" | "bgm" | "hybrid";
    music_source_allowed: string;
    music_source_allow_personal: boolean;
    illustration_trigger: "off" | "important" | "turn_interval" | "frequent";
    illustration_trigger_allowed: string;
    illustration_kp_timeout: number;
    illustration_kp_daily_budget: number;
    illustration_kp_max_context_chars: number;
    illustration_npc_enabled: boolean;
    illustration_npc_size: string;
    illustration_npc_max_per_turn: number;
    /** 管理员允许的音频块清单（缺失时后端按旧 music_engine 推导）。 */
    audio_blocks_allowed?: string[] | string;
}

type ImmersiveAudioBlock = "ambient" | "procedural" | "music_model" | "external" | "off";

interface ImmersiveAudioBlockOption {
    value: ImmersiveAudioBlock;
    label: string;
    description: string;
    stackable: boolean;
    available: boolean;
    unavailable_reason?: string;
}

interface ImmersiveAudioBlocksMeta {
    options: ImmersiveAudioBlockOption[];
    allowed: ImmersiveAudioBlock[];
    music_model_available: boolean;
}

interface ImmersiveAudioBlocksState {
    ambient: boolean;
    music: ImmersiveAudioBlock;
    blocks: ImmersiveAudioBlock[];
    requested: ImmersiveAudioBlock[];
    allowed: ImmersiveAudioBlock[];
    revoked: ImmersiveAudioBlock[];
    degrade_reason: string;
    degrade_message: string;
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
    audio_blocks?: ImmersiveAudioBlocksMeta;
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

/** 房间级共享播放位置：同一房间的所有客户端据此对齐到同一小节。 */
interface ImmersiveMusicSync {
    /** 换谱时递增；同一 epoch 表示同一首 cue 的同一段播放。 */
    epoch: number;
    /** cue 内的位置，单位小节（小数表示小节内进度，跨循环持续累加）。 */
    position_bars: number;
    /** 服务端使用的单小节秒数，用于与客户端 tempo 对齐。 */
    bar_seconds?: number;
    server_now_ms?: number;
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
    sync?: ImmersiveMusicSync | null;
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
    /** 站点 + 房间收紧后的生效值；个人覆盖由前端再解析。 */
    music_source?: "realtime" | "bgm" | "hybrid";
    /** 生效音乐引擎（P4）：由音频块模型派生。 */
    music_engine?: "procedural" | "external" | "off";
    music_engine_reason?: string;
    /** 房间生效的音频块组合与降级说明。 */
    audio_blocks?: ImmersiveAudioBlocksState;
    audio_block_options?: ImmersiveAudioBlockOption[];
    ambient_audio?: boolean;
    music_model_available?: boolean;
    illustration_trigger?: "off" | "important" | "turn_interval" | "frequent";
}

/** 外链曲目：地址只给同源代理路径，上游 URL 不出服务端。 */
interface ImmersiveExternalTrack {
    track_key: string;
    provider: string;
    title: string;
    artist: string;
    license: string;
    license_label: string;
    license_url: string;
    source_url: string;
    attribution: string;
    duration_ms: number;
    tags: string[];
    instrumental: boolean | null;
    stream_path: string;
    url_expires_at: number;
    elapsed_ms?: number;
    license_allowed?: boolean;
}

/** 服务端下发的外链检索结果；失败时 track 为 null，前端应留在底床。 */
interface ImmersiveExternalEvent {
    ok: boolean;
    status: "selected" | "empty" | "degraded" | "disabled" | "throttled";
    engine: "procedural" | "external" | "off";
    engine_reason?: string;
    fallback: "procedural" | "off";
    track: ImmersiveExternalTrack | null;
    alternatives?: ImmersiveExternalTrack[];
    provider?: string;
    reason?: string;
    room_id?: string;
    request_id?: string;
    trigger?: string;
    cached?: boolean;
    fade_ms?: number;
    crossfade_ms?: number;
    phrase_bars?: number;
    elapsed_ms?: number;
}

/** 引擎切换事件：off 时要平滑淡出，不能硬切。 */
interface ImmersiveMusicEngineEvent {
    room_id?: string;
    engine: "procedural" | "external" | "off";
    fade_ms?: number;
    fallback?: string;
}

interface TrpgImmersiveMusicApi {
    apply: (plan: ImmersiveMusicPlan) => void;
    stop: () => void;
    /** 仅 BGM 模式：淡出后销毁 AudioContext，完全不发声、不占用调度资源。 */
    shutdown: () => void;
    isEnabled: () => boolean;
    setMuted: (muted: boolean) => void;
    /** 语音配音期间压低实时配乐（0-90），只改主增益，不接入语音总线。 */
    setDucking: (level: number) => void;
    isMuted: () => boolean;
    toggleMuted: () => boolean;
    unlock: () => boolean;
    isRunning: () => boolean;
    /** 运行时诊断：当前 AudioContext 状态、每条总线的进度与下一小节时间。 */
    diagnostics: () => ImmersiveMusicDiagnostics;
    /** 引擎切换（P4）：off 平滑淡出并停止补发；恢复时按共享位置重建。 */
    setEngineMode: (mode: "procedural" | "external" | "off", fadeMs?: number) => void;
    currentEngineMode: () => "procedural" | "external" | "off";
    /** 底床等功率斜变（外链淡入/淡出时配对使用）。 */
    rampBedLevel: (target: number, seconds: number, rising: boolean) => void;
    bedDuckLevel: () => number;
    /** 外链播放器汇入点（引擎总增益，不经过底床增益）。 */
    externalInput: () => GainNode | null;
    /** 当前小节时基，用于把切换点对齐到小节/乐句边界。 */
    barTiming: () => { barSeconds: number; nextBarIn: number; positionBars: number } | null;
    audioClock: () => number;
}

interface ImmersiveMusicBusDiagnostics {
    cycle: number;
    bar_index: number;
    next_bar_at: number;
    position_bars: number;
    section: string;
    /** 累计已排程小节数；连续两个窗口不增长就说明调度断供。 */
    scheduled_bars: number;
    /** 当前小节的总线动态目标。 */
    level: number;
}

/** 一个调度窗口：这一拍补排了几个小节、总线上有几条声部。 */
interface ImmersiveMusicScheduleWindow {
    at: number;
    buses: number;
    bars: number;
    level: number;
}

interface ImmersiveMusicDiagnostics {
    context: string;
    quality: number;
    muted: boolean;
    buses: ImmersiveMusicBusDiagnostics[];
    /** 最近约 26 秒的调度窗口（220ms 一次，最多 120 条）。 */
    windows: ImmersiveMusicScheduleWindow[];
}

/**
 * 程序化环境音（P2）。与配乐共用混音总线、限幅与语音 ducking，
 * 但有独立开关与音量，默认开启。
 */
interface TrpgImmersiveAmbientApi {
    /** 跟随配乐 plan 的场景/档位切换环境音；相同场景不会重建。 */
    apply: (plan: ImmersiveMusicPlan) => void;
    stop: (fadeMs?: number) => void;
    setEnabled: (enabled: boolean) => void;
    isEnabled: () => boolean;
    /** 0~1，独立于配乐音量。 */
    setVolume: (volume: number) => void;
    getVolume: () => number;
    isRunning: () => boolean;
    getScene: () => string | null;
    /** 重新拉起最近一个场景（用于「仅 BGM」模式下停掉配乐但保留环境音）。 */
    resume: () => void;
    /** 配乐是否在播放；环境音据此自动让位，避免两套声音打架。 */
    setMusicActive: (active: boolean) => void;
    diagnostics: () => ImmersiveAmbientDiagnostics;
}

interface ImmersiveAmbientDiagnostics {
    context: string;
    enabled: boolean;
    running: boolean;
    scene: string | null;
    volume: number;
    music_active: boolean;
    layers: string[];
    scheduled_events: number;
}

interface TrpgImmersiveApi {
    init: () => void;
    setRoom: (room: Room | null) => void;
    refreshState: () => void;
    onMusicEvent: (plan: ImmersiveMusicPlan) => void;
    onIllustrationEvent: (payload: ImmersiveIllustrationEvent) => void;
    renderSettings: () => void;
    renderModelSettings: () => void;
    requestIllustration: () => void;
    toggleMusic: () => void;
    toggleAmbient: () => void;
    isAmbientEnabled: () => boolean;
    syncMusicButton: () => void;
    getPersonalPreferences: () => { music_source?: string | undefined; illustration_trigger?: string | undefined };
    getMusicSource: () => "realtime" | "bgm" | "hybrid";
}

interface TrpgImmersiveExternalApi {
    applyEvent: (payload: ImmersiveExternalEvent | null | undefined) => void;
    applyEngine: (event: ImmersiveMusicEngineEvent | null | undefined) => void;
    stop: (fadeMs?: number) => void;
    state: () => Record<string, unknown>;
    setReporter: (reporter: ((event: Record<string, unknown>) => void) | null) => void;
}

/**
 * 标签输入控件的候选项。除音频块外，房规里的成员白名单也复用同一控件，
 * 因此 `value` 放宽为 string 而不是音频块枚举。
 */
interface TrpgAudioBlockOptionInput {
    value: string;
    label: string;
    description?: string;
    stackable?: boolean;
    available?: boolean;
    unavailable_reason?: string;
}

interface TrpgAudioBlocksApi {
    render: (
        containerId: string,
        options: TrpgAudioBlockOptionInput[],
        selected: string[],
        renderOptions?: {
            mode?: "allow" | "select";
            allowed?: string[];
            onChange?: (blocks: string[]) => void;
            /** 覆盖默认提示语；传空串可隐藏控件自带的说明文字。 */
            placeholder?: string;
            hint?: string;
            /** 提示语里对候选项的称呼，默认「块」。 */
            noun?: string;
            /** allow 模式清空时的提示；传空串可关闭该提示。 */
            allowEmptyNotice?: string;
        },
    ) => void;
    getSelection: (containerId: string) => string[];
    setSelection: (containerId: string, blocks: string[]) => void;
}

interface Window {
    TrpgImmersiveMusic?: TrpgImmersiveMusicApi;
    TrpgImmersiveAmbient?: TrpgImmersiveAmbientApi;
    TrpgImmersiveExternal?: TrpgImmersiveExternalApi;
    TrpgImmersive?: TrpgImmersiveApi;
    TrpgAudioBlocks?: TrpgAudioBlocksApi;
}
