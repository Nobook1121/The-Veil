/**
 * AI 对话语音配音（P1）前端类型。
 * 服务端下发的 voice 载荷与播放器状态都从这里统一约束。
 */
interface VoiceSegmentPayload {
    speaker_id: string;
    display_name: string;
    kind: "narrator" | "npc";
    text: string;
    source: "markup" | "heuristic" | "narrator";
    profile_id: string;
    profile_label?: string;
    profile_i18n_key?: string;
    pitch: number;
    rate: number;
    preferred_voices: string[];
    engine?: string;
    /** 本地引擎绑定的真实说话人（Kokoro sid 与可读音色名）。 */
    voice_id?: string;
    speaker_sid?: number;
    voice_gender?: string;
    voice_source?: string;
}

interface VoicePayloadSettings {
    volume: number;
    rate: number;
    autoplay: boolean;
    interrupt_policy: "interrupt" | "queue";
    ducking: boolean;
    ducking_level: number;
    max_segment_chars: number;
    max_turn_chars: number;
    max_segments: number;
    allow_personal?: boolean;
}

interface VoicePayload {
    room_id: string;
    message_id: string;
    turn: number;
    engine: string;
    engine_allowed: string[];
    playable: boolean;
    degraded: boolean;
    reason: string;
    segments: VoiceSegmentPayload[];
    settings: VoicePayloadSettings;
    fallback_from?: string;
    audio?: {
        endpoint: string;
        format: string;
        sample_rate: number;
        model: string;
    };
}

type VoicePlaybackStatus =
    | "idle"
    | "ready"
    | "playing"
    | "blocked"
    | "failed"
    | "interrupted"
    | "degraded"
    | "disabled";

interface VoicePlaybackState {
    status: VoicePlaybackStatus;
    reason: string;
    messageId: string;
    speaker: string;
    profileId: string;
    engine: string;
    segmentIndex: number;
    segmentCount: number;
}

interface VoiceProfileInfo {
    id: string;
    label: string;
    kind: "narrator" | "npc";
    gender: string;
    age_group: string;
    pitch: number;
    rate: number;
    preferred_voices: string[];
}

/** 挂载语音按钮只需要 id 与 metadata，避免依赖完整 ChatMessage 结构。 */
interface VoiceMessageLike {
    id?: string | undefined;
    metadata?: Record<string, unknown> | undefined;
}

interface TrpgVoiceApi {
    init: () => void;
    playPayload: (payload: VoicePayload | null | undefined, options?: { force?: boolean; messageId?: string }) => void;
    attachMessageButton: (element: HTMLElement, message: VoiceMessageLike, animate?: boolean) => void;
    attachToMessage: (messageId: string, payload: VoicePayload | null | undefined, animate?: boolean) => void;
    stop: () => void;
    getState: () => VoicePlaybackState;
    onStateChange: (listener: (state: VoicePlaybackState) => void) => () => void;
    isSupported: () => boolean;
    refreshPreferences: () => void;
    setTextOnly: (value: boolean) => void;
    setMutedSpeakers: (names: string[]) => void;
}

interface Window {
    TrpgVoice?: TrpgVoiceApi;
}
