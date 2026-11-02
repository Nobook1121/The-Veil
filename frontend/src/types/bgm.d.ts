type BgmPlaybackPriority = "room" | "personal";
type BgmTrackSource = "room" | "personal";
/** 播放模式：列表循环 / 单曲循环 / 随机播放。 */
type BgmPlaybackMode = "list" | "single" | "shuffle";

interface BgmPersonalSettings {
    urls: string[];
    mode: BgmPlaybackMode;
    priority: BgmPlaybackPriority;
}

/** 服务端音乐库中的一首曲目：内置音乐或管理员上传的音乐。 */
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

interface TrpgBgmApi {
    init: () => void;
    setRoom: (room: Room | null) => void;
    refresh: () => void;
    getPersonalSettings: () => BgmPersonalSettings;
    savePersonalSettings: (settings: Partial<BgmPersonalSettings>) => BgmPersonalSettings;
    renderPersonalSettings: () => Promise<void>;
    renderRoomPlaylist: (room: Room, options?: BgmRoomPlaylistOptions) => Promise<void>;
    fetchTracks: () => Promise<BgmTrack[]>;
    loadLibraryPanel: () => Promise<void>;
}

interface Window {
    TrpgBgm?: TrpgBgmApi;
}
