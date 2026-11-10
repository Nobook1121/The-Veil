type BgmPlaybackPriority = "room" | "personal";
type BgmTrackSource = "room" | "personal";
/** 播放模式：列表循环 / 单曲循环 / 随机播放。 */
type BgmPlaybackMode = "list" | "single" | "shuffle";

interface BgmPersonalSettings {
    urls: string[];
    mode: BgmPlaybackMode;
    priority: BgmPlaybackPriority;
}

/** 服务端音乐库中的一首曲目：内置目录或管理员上传的音乐。 */
interface BgmTrack {
    url: string;
    name: string;
    builtin: boolean;
    size: number;
}

interface BgmTracksPayload {
    tracks: BgmTrack[];
    can_manage: boolean;
}

interface BgmRoomPlaylistOptions {
    /** 房主/房间管理员才能编辑曲目与播放模式；其他成员只读。 */
    editable?: boolean;
}

/** 统一播放器读取的 BGM 快照。 */
interface BgmPlaybackState {
    active: boolean;
    playing: boolean;
    paused: boolean;
    source: BgmTrackSource | null;
    title: string;
    index: number;
    count: number;
    current: number;
    duration: number;
    mode: BgmPlaybackMode;
    suppressed: boolean;
    ducked: boolean;
}

interface TrpgBgmApi {
    init: () => void;
    setRoom: (room: Room | null) => void;
    /** 沉浸配乐播放时压低 BGM；传入 false 恢复原音量。 */
    setDuck: (ducked: boolean) => void;
    /** 仅实时配乐模式：淡出并暂停 BGM；false 时恢复原音量与播放状态。 */
    setSuppressed: (suppressed: boolean) => void;
    isSuppressed: () => boolean;
    refresh: () => void;
    getPersonalSettings: () => BgmPersonalSettings;
    savePersonalSettings: (settings: Partial<BgmPersonalSettings>) => BgmPersonalSettings;
    renderPersonalSettings: () => Promise<void>;
    renderRoomPlaylist: (room: Room, options?: BgmRoomPlaylistOptions) => Promise<void>;
    fetchTracks: () => Promise<BgmTrack[]>;
    loadLibraryPanel: () => Promise<void>;
    getPlaybackState: () => BgmPlaybackState;
    play: () => void;
    pause: () => void;
    toggle: () => void;
    next: () => void;
    prev: () => void;
    seek: (seconds: number) => void;
    hasPlaylist: () => boolean;
}

interface Window {
    TrpgBgm?: TrpgBgmApi;
}
