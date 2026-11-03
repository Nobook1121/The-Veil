/**
 * 沉浸系统入口：房间切换、Socket 事件分发、设置页与手动操作。
 *
 * 与既有功能的关系：
 * - 配乐是「程序化实时音轨」，与 `bgm.ts` 的音频播放器并行；播放时通过 ducking 压低 BGM。
 * - 配图是房间级场景面板，独立于聊天消息流，不改变任何消息渲染逻辑。
 */
namespace TrpgImmersiveModule {
    let currentRoom: Room | null = null;
    let loadingRoomId = "";
    let initialized = false;

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function roomIdOf(room: Room | null): string {
        return room ? String(room.id || "") : "";
    }

    function toggleMusicPlayback(): void {
        const muted = Boolean(window.TrpgImmersiveMusic?.toggleMuted());
        if (!muted) window.TrpgImmersiveMusic?.unlock();
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    export function init(): void {
        if (initialized) return;
        initialized = true;
        TrpgImmersiveIllustrationModule.init();
        TrpgImmersiveSettingsModule.init();
        byId<HTMLButtonElement>("immersiveMusicToggle")?.addEventListener("click", toggleMusicPlayback);
        // 右侧栏可能因刷新时处于收起状态而没有主按钮，初始化时同步一次窄条图标。
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    export function toggleMusic(): void {
        toggleMusicPlayback();
    }

    export function setRoom(room: Room | null): void {
        currentRoom = room;
        const roomId = roomIdOf(room);
        TrpgImmersiveIllustrationModule.setRoom(room);
        if (!roomId) {
            loadingRoomId = "";
            window.TrpgImmersiveMusic?.stop();
            return;
        }
        void loadState(roomId);
    }

    async function loadState(roomId: string): Promise<void> {
        loadingRoomId = roomId;
        try {
            const response = await TrpgApi.get<ApiResponse<ImmersiveRoomState>>(
                `/api/immersive/rooms/${encodeURIComponent(roomId)}/state`,
            );
            if (loadingRoomId !== roomId) return;
            const state = response.success ? response.data || null : null;
            if (!state) {
                TrpgImmersiveIllustrationModule.applyState(null);
                return;
            }
            TrpgImmersiveIllustrationModule.applyState(state);
            const switches = state.switches;
            if (switches?.music && state.music?.profile) {
                window.TrpgImmersiveMusic?.apply(planFromState(roomId, state.music));
            } else {
                window.TrpgImmersiveMusic?.stop();
            }
        } catch (error) {
            console.warn("读取沉浸状态失败，配乐与配图保持静默", error);
        }
    }

    function planFromState(roomId: string, music: ImmersiveMusicState): ImmersiveMusicPlan {
        const section = Number(music.section || 0);
        const baseSeed = String(music.base_seed || `${roomId}:default`);
        const profile = (music.profile || "daily") as ImmersiveProfile;
        return {
            room_id: roomId,
            profile,
            intensity: (music.intensity || "medium") as ImmersiveIntensity,
            section,
            seed: `${baseSeed}:${section}`,
            motif: music.motif || `${profile}-${(section % 4) + 1}`,
            crossfade_ms: Number(music.crossfade_ms || 3000),
            volume: Number(music.volume ?? 0.35),
            duck_bgm: music.duck_bgm !== false,
            scene: music.scene || "",
            turns: Number(music.turns || 0),
            changed: false,
            score: music.score || null,
            director: music.director || null,
        };
    }

    export function onMusicEvent(plan: ImmersiveMusicPlan): void {
        const roomId = roomIdOf(currentRoom);
        if (!roomId) return;
        if (plan.room_id && String(plan.room_id) !== roomId) return;
        window.TrpgImmersiveMusic?.apply(plan);
        TrpgImmersiveIllustrationModule.onMusic(plan);
    }

    export function onIllustrationEvent(payload: ImmersiveIllustrationEvent): void {
        const roomId = roomIdOf(currentRoom);
        if (!roomId) return;
        if (payload.room_id && String(payload.room_id) !== roomId) return;
        TrpgImmersiveIllustrationModule.onIllustration(payload);
    }

    export function renderSettings(): void {
        TrpgImmersiveSettingsModule.render();
    }

    export function requestIllustration(): void {
        TrpgImmersiveIllustrationModule.requestNow();
    }

    export function refreshState(): void {
        const roomId = roomIdOf(currentRoom);
        if (!roomId) return;
        TrpgImmersiveIllustrationModule.setRoom(currentRoom);
        void loadState(roomId);
    }
}

window.TrpgImmersive = {
    init: TrpgImmersiveModule.init,
    setRoom: TrpgImmersiveModule.setRoom,
    refreshState: TrpgImmersiveModule.refreshState,
    onMusicEvent: TrpgImmersiveModule.onMusicEvent,
    onIllustrationEvent: TrpgImmersiveModule.onIllustrationEvent,
    renderSettings: TrpgImmersiveModule.renderSettings,
    requestIllustration: TrpgImmersiveModule.requestIllustration,
    toggleMusic: TrpgImmersiveModule.toggleMusic,
    syncMusicButton: TrpgImmersiveIllustrationModule.syncMusicButton,
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => TrpgImmersiveModule.init());
} else {
    TrpgImmersiveModule.init();
}
