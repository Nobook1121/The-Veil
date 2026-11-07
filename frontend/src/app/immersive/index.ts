/**
 * 沉浸系统入口：房间切换、Socket 事件分发、设置页与手动操作。
 *
 * 与既有功能的关系：
 * - 配乐是「程序化实时音轨」，与 `bgm.ts` 的音频播放器并行；播放时通过 ducking 压低 BGM。
 * - 配图是房间级场景面板，独立于聊天消息流，不改变任何消息渲染逻辑。
 */
namespace TrpgImmersiveModule {
    const PERSONAL_PREF_KEY = "trpg_immersive_personal";
    /** 配乐/环境音开关按账号隔离，避免同浏览器切换账号时串用彼此偏好。 */
    const AUDIO_PREF_PREFIX = "trpg_immersive_audio:";

    type MusicSource = "realtime" | "bgm" | "hybrid";
    type IllustrationTrigger = "off" | "important" | "turn_interval" | "frequent";

    interface UserAudioPrefs {
        music_muted?: boolean | undefined;
        ambient_enabled?: boolean | undefined;
    }

    interface PersonalImmersivePrefs {
        music_source?: MusicSource | undefined;
        illustration_trigger?: IllustrationTrigger | undefined;
        voice_enabled?: boolean | undefined;
        voice_autoplay?: boolean | undefined;
        voice_volume?: number | undefined;
        voice_rate?: number | undefined;
        voice_text_only?: boolean | undefined;
        voice_muted_speakers?: string[] | undefined;
    }

    let currentRoom: Room | null = null;
    let loadingRoomId = "";
    let initialized = false;
    let lastState: ImmersiveRoomState | null = null;
    let currentSource: MusicSource = "hybrid";

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function roomIdOf(room: Room | null): string {
        return room ? String(room.id || "") : "";
    }

    function siteValue(key: string, fallback: unknown): unknown {
        try {
            return window.configManager?.get?.("general", "immersive", key, fallback) ?? fallback;
        } catch {
            return fallback;
        }
    }

    function allowedChoices(key: string, fallback: string[]): string[] {
        const raw = siteValue(key, fallback.join(","));
        const values = Array.isArray(raw) ? raw.map(String) : String(raw ?? "").split(",");
        const cleaned = values.map((item) => item.trim().toLowerCase()).filter(Boolean);
        return cleaned.length ? cleaned : fallback;
    }

    export function getPersonalPreferences(): PersonalImmersivePrefs {
        try {
            const parsed: unknown = JSON.parse(window.localStorage.getItem(PERSONAL_PREF_KEY) || "{}");
            if (!parsed || typeof parsed !== "object") return {};
            const record = parsed as PersonalImmersivePrefs;
            return {
                music_source: record.music_source,
                illustration_trigger: record.illustration_trigger,
                voice_enabled: typeof record.voice_enabled === "boolean" ? record.voice_enabled : undefined,
                voice_autoplay: typeof record.voice_autoplay === "boolean" ? record.voice_autoplay : undefined,
                voice_volume: typeof record.voice_volume === "number" ? record.voice_volume : undefined,
                voice_rate: typeof record.voice_rate === "number" ? record.voice_rate : undefined,
                voice_text_only: typeof record.voice_text_only === "boolean" ? record.voice_text_only : undefined,
                voice_muted_speakers: Array.isArray(record.voice_muted_speakers)
                    ? record.voice_muted_speakers.map((item) => String(item)).filter(Boolean)
                    : undefined,
            };
        } catch {
            return {};
        }
    }

    function savePersonalPreferences(patch: PersonalImmersivePrefs): void {
        const next = { ...getPersonalPreferences(), ...patch };
        try {
            window.localStorage.setItem(PERSONAL_PREF_KEY, JSON.stringify(next));
        } catch (error) {
            console.warn("保存个人沉浸偏好失败", error);
        }
        applyMusicSource(resolveMusicSource(lastState), lastState);
    }

    function audioPrefKey(): string {
        const userId = String(window.currentUser?.user_id || "");
        return `${AUDIO_PREF_PREFIX}${userId || "guest"}`;
    }

    function getUserAudioPrefs(): UserAudioPrefs {
        try {
            const parsed: unknown = JSON.parse(window.localStorage.getItem(audioPrefKey()) || "{}");
            if (!parsed || typeof parsed !== "object") return {};
            const record = parsed as UserAudioPrefs;
            return {
                music_muted: typeof record.music_muted === "boolean" ? record.music_muted : undefined,
                ambient_enabled: typeof record.ambient_enabled === "boolean" ? record.ambient_enabled : undefined,
            };
        } catch {
            return {};
        }
    }

    function saveUserAudioPrefs(patch: UserAudioPrefs): void {
        const next = { ...getUserAudioPrefs(), ...patch };
        try {
            window.localStorage.setItem(audioPrefKey(), JSON.stringify(next));
        } catch (error) {
            console.warn("保存配乐/环境音偏好失败", error);
        }
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    /**
     * 把当前账号的配乐/环境音偏好应用到引擎。
     *
     * 优先级：账号级偏好 > 引擎默认值（配乐不静音、环境音开启）。不读取引擎里
     * 浏览器级的旧值，避免同一浏览器切换账号时把上一个账号的开关带给新账号。
     */
    function applyUserAudioPrefs(): void {
        // 未登录时账号未知，等 trpg:user-changed 后再应用，避免写入 guest 键。
        if (!window.currentUser?.user_id) return;
        const stored = getUserAudioPrefs();
        const musicMuted = typeof stored.music_muted === "boolean" ? stored.music_muted : false;
        const ambientEnabled = typeof stored.ambient_enabled === "boolean" ? stored.ambient_enabled : true;
        if (typeof stored.music_muted !== "boolean" || typeof stored.ambient_enabled !== "boolean") {
            try {
                window.localStorage.setItem(
                    audioPrefKey(),
                    JSON.stringify({ ...stored, music_muted: musicMuted, ambient_enabled: ambientEnabled }),
                );
            } catch (error) {
                console.warn("保存配乐/环境音偏好失败", error);
            }
        }
        const music = window.TrpgImmersiveMusic;
        if (music && music.isMuted() !== musicMuted) music.setMuted(musicMuted);
        const ambient = window.TrpgImmersiveAmbient;
        if (ambient && ambient.isEnabled() !== ambientEnabled) ambient.setEnabled(ambientEnabled);
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    /** 站点默认 < 个人（仅当站点允许）< 房间收紧（配乐关闭时强制仅 BGM）。 */
    export function resolveMusicSource(state: ImmersiveRoomState | null): MusicSource {
        const site = String(siteValue("music_source", "hybrid")).toLowerCase() as MusicSource;
        const allowed = allowedChoices("music_source_allowed", ["realtime", "bgm", "hybrid"]);
        const allowPersonal = Boolean(siteValue("music_source_allow_personal", true));
        const personal = getPersonalPreferences().music_source;
        let source: MusicSource = site;
        if (allowPersonal && personal && allowed.includes(personal)) source = personal;
        if (state?.switches && !state.switches.music) source = "bgm";
        return source;
    }

    export function getMusicSource(): MusicSource {
        return currentSource;
    }

    function applyMusicSource(source: MusicSource, state: ImmersiveRoomState | null): void {
        currentSource = source;
        const music = window.TrpgImmersiveMusic;
        const bgm = window.TrpgBgm;
        // 不在房间内时不启用实时配乐，也不压制页面 BGM。
        if (!roomIdOf(currentRoom)) {
            if (music?.isEnabled()) music.shutdown();
            else music?.stop();
            bgm?.setSuppressed(false);
            bgm?.setDuck(false);
            return;
        }
        if (source === "bgm") {
            // 仅 BGM：实时引擎完全停止并销毁 AudioContext，绝不发声。
            if (music?.isEnabled()) music.shutdown();
            else music?.stop();
            bgm?.setSuppressed(false);
            bgm?.setDuck(false);
        } else {
            // 仅实时配乐：BGM 淡出并暂停；混合：BGM 由引擎按既有规则压低。
            bgm?.setSuppressed(source === "realtime");
            const roomId = roomIdOf(currentRoom);
            if (!roomId || !state?.switches?.music || !state.music?.profile) {
                music?.stop();
            } else {
                music?.apply(planFromState(roomId, state.music));
            }
        }
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    function updatePersonalControls(): void {
        const musicSelect = byId<HTMLSelectElement>("personalImmersiveMusicSource");
        const triggerSelect = byId<HTMLSelectElement>("personalImmersiveIllustrationTrigger");
        const prefs = getPersonalPreferences();
        const musicAllowed = allowedChoices("music_source_allowed", ["realtime", "bgm", "hybrid"]);
        const allowPersonal = Boolean(siteValue("music_source_allow_personal", true));
        if (musicSelect) {
            const allowedSet = new Set(musicAllowed);
            Array.from(musicSelect.options).forEach((option) => {
                option.disabled = !allowedSet.has(option.value);
            });
            musicSelect.disabled = !allowPersonal;
            const fallback = String(siteValue("music_source", "hybrid")).toLowerCase();
            musicSelect.value = allowPersonal && prefs.music_source && allowedSet.has(prefs.music_source)
                ? prefs.music_source
                : fallback;
        }
        if (triggerSelect) {
            // 「关闭」始终可选（只会更省）；其余档位需在站点允许集合内。
            const allowedSet = new Set([...allowedChoices("illustration_trigger_allowed", ["important", "turn_interval", "frequent"]), "off"]);
            Array.from(triggerSelect.options).forEach((option) => {
                option.disabled = !allowedSet.has(option.value);
            });
            const fallback = String(siteValue("illustration_trigger", "important")).toLowerCase();
            triggerSelect.value = prefs.illustration_trigger && allowedSet.has(prefs.illustration_trigger)
                ? prefs.illustration_trigger
                : fallback;
        }
        updatePersonalVoiceControls(prefs);
    }

    /** 个人语音覆盖：站点允许时才可改开关/自动播放/音量/语速；只看文字与按 NPC 静音始终可用。 */
    function updatePersonalVoiceControls(prefs: PersonalImmersivePrefs): void {
        const allowPersonal = Boolean(siteValue("voice_allow_personal", true));
        const siteEnabled = Boolean(siteValue("voice_enabled", false));
        const siteAutoplay = Boolean(siteValue("voice_autoplay", false));
        const siteVolume = Number(siteValue("voice_volume", 70)) || 70;
        const siteRate = Number(siteValue("voice_rate", 100)) || 100;
        const enabled = byId<HTMLInputElement>("personalVoiceEnabled");
        const autoplay = byId<HTMLInputElement>("personalVoiceAutoplay");
        const volume = byId<HTMLInputElement>("personalVoiceVolume");
        const rate = byId<HTMLInputElement>("personalVoiceRate");
        const volumeLabel = byId<HTMLElement>("personalVoiceVolumeValue");
        const rateLabel = byId<HTMLElement>("personalVoiceRateValue");
        const textOnly = byId<HTMLInputElement>("personalVoiceTextOnly");
        const muted = byId<HTMLInputElement>("personalVoiceMutedSpeakers");
        if (enabled) {
            enabled.checked = allowPersonal && typeof prefs.voice_enabled === "boolean" ? prefs.voice_enabled : siteEnabled;
            enabled.disabled = !allowPersonal;
        }
        if (autoplay) {
            autoplay.checked = allowPersonal && typeof prefs.voice_autoplay === "boolean" ? prefs.voice_autoplay : siteAutoplay;
            autoplay.disabled = !allowPersonal;
        }
        if (volume) {
            volume.value = String(allowPersonal && typeof prefs.voice_volume === "number" ? prefs.voice_volume : siteVolume);
            volume.disabled = !allowPersonal;
            if (volumeLabel) volumeLabel.textContent = volume.value;
        }
        if (rate) {
            rate.value = String(allowPersonal && typeof prefs.voice_rate === "number" ? prefs.voice_rate : siteRate);
            rate.disabled = !allowPersonal;
            if (rateLabel) rateLabel.textContent = `${rate.value}%`;
        }
        if (textOnly) textOnly.checked = Boolean(prefs.voice_text_only);
        if (muted) muted.value = (prefs.voice_muted_speakers || []).join(", ");
    }

    function parseMutedSpeakers(raw: string): string[] {
        return String(raw || "")
            .split(/[,，、]/)
            .map((item) => item.trim())
            .filter(Boolean);
    }

    function bindPersonalControls(): void {
        const musicSelect = byId<HTMLSelectElement>("personalImmersiveMusicSource");
        const triggerSelect = byId<HTMLSelectElement>("personalImmersiveIllustrationTrigger");
        const message = byId<HTMLElement>("personalImmersiveMessage");
        const notify = (text: string): void => {
            if (message) message.textContent = text;
        };
        musicSelect?.addEventListener("change", () => {
            const value = musicSelect.value as MusicSource;
            savePersonalPreferences({ music_source: value });
            updatePersonalControls();
            notify("音乐来源偏好已保存（仅保存在当前浏览器）");
        });
        triggerSelect?.addEventListener("change", () => {
            const value = triggerSelect.value as IllustrationTrigger;
            savePersonalPreferences({ illustration_trigger: value });
            updatePersonalControls();
            notify("配图触发偏好已保存（仅保存在当前浏览器）");
        });
        const voiceEnabled = byId<HTMLInputElement>("personalVoiceEnabled");
        const voiceAutoplay = byId<HTMLInputElement>("personalVoiceAutoplay");
        const voiceVolume = byId<HTMLInputElement>("personalVoiceVolume");
        const voiceRate = byId<HTMLInputElement>("personalVoiceRate");
        const voiceVolumeLabel = byId<HTMLElement>("personalVoiceVolumeValue");
        const voiceRateLabel = byId<HTMLElement>("personalVoiceRateValue");
        const voiceTextOnly = byId<HTMLInputElement>("personalVoiceTextOnly");
        const voiceMuted = byId<HTMLInputElement>("personalVoiceMutedSpeakers");
        voiceEnabled?.addEventListener("change", () => {
            savePersonalPreferences({ voice_enabled: voiceEnabled.checked });
            notify("语音开关偏好已保存（仅保存在当前浏览器）");
        });
        voiceAutoplay?.addEventListener("change", () => {
            savePersonalPreferences({ voice_autoplay: voiceAutoplay.checked });
            notify("语音自动播放偏好已保存（仅保存在当前浏览器）");
        });
        voiceVolume?.addEventListener("input", () => {
            if (voiceVolumeLabel) voiceVolumeLabel.textContent = voiceVolume.value;
        });
        voiceVolume?.addEventListener("change", () => {
            savePersonalPreferences({ voice_volume: Number(voiceVolume.value) });
            notify("语音音量偏好已保存（仅保存在当前浏览器）");
        });
        voiceRate?.addEventListener("input", () => {
            if (voiceRateLabel) voiceRateLabel.textContent = `${voiceRate.value}%`;
        });
        voiceRate?.addEventListener("change", () => {
            savePersonalPreferences({ voice_rate: Number(voiceRate.value) });
            notify("语音语速偏好已保存（仅保存在当前浏览器）");
        });
        voiceTextOnly?.addEventListener("change", () => {
            savePersonalPreferences({ voice_text_only: voiceTextOnly.checked });
            window.TrpgVoice?.setTextOnly(voiceTextOnly.checked);
            notify("只看文字偏好已保存（仅保存在当前浏览器）");
        });
        voiceMuted?.addEventListener("change", () => {
            const names = parseMutedSpeakers(voiceMuted.value);
            voiceMuted.value = names.join(", ");
            savePersonalPreferences({ voice_muted_speakers: names });
            window.TrpgVoice?.setMutedSpeakers(names);
            notify("按 NPC 静音偏好已保存（仅保存在当前浏览器）");
        });
        // 设置页在配置加载后才打开，点击入口时重新按站点允许范围刷新一次。
        document.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            if (target?.closest('[data-profile-target="profile-immersive"]')) updatePersonalControls();
        });
        updatePersonalControls();
    }

    function toggleMusicPlayback(): void {
        // 仅 BGM 档位下实时引擎不启用，按钮不应唤醒 AudioContext。
        if (currentSource === "bgm") return;
        const muted = Boolean(window.TrpgImmersiveMusic?.toggleMuted());
        if (!muted) window.TrpgImmersiveMusic?.unlock();
        saveUserAudioPrefs({ music_muted: muted });
    }

    /** 只切换当前账号的环境音，不写房规、不影响同房间其他人。 */
    export function toggleAmbient(): void {
        const ambient = window.TrpgImmersiveAmbient;
        if (!ambient) return;
        const enabled = !ambient.isEnabled();
        ambient.setEnabled(enabled);
        if (enabled) ambient.resume();
        saveUserAudioPrefs({ ambient_enabled: enabled });
    }

    export function isAmbientEnabled(): boolean {
        return Boolean(window.TrpgImmersiveAmbient?.isEnabled());
    }

    export function init(): void {
        if (initialized) return;
        initialized = true;
        TrpgImmersiveIllustrationModule.init();
        TrpgImmersiveSettingsModule.init();
        bindPersonalControls();
        byId<HTMLButtonElement>("immersiveMusicToggle")?.addEventListener("click", toggleMusicPlayback);
        byId<HTMLButtonElement>("immersiveAmbientToggle")?.addEventListener("click", toggleAmbient);
        // 账号信息异步就绪（/api/auth/me 或登录）后再灌回该账号的偏好。
        window.addEventListener("trpg:user-changed", applyUserAudioPrefs);
        applyUserAudioPrefs();
        // 右侧栏可能因刷新时处于收起状态而没有主按钮，初始化时同步一次窄条图标。
        TrpgImmersiveIllustrationModule.syncMusicButton();
    }

    export function toggleMusic(): void {
        toggleMusicPlayback();
    }

    export function setRoom(room: Room | null): void {
        currentRoom = room;
        // 进入房间时账号信息已就绪，按当前账号再校准一次配乐/环境音开关。
        applyUserAudioPrefs();
        const roomId = roomIdOf(room);
        TrpgImmersiveIllustrationModule.setRoom(room);
        if (!roomId) {
            loadingRoomId = "";
            lastState = null;
            window.TrpgImmersiveMusic?.stop();
            window.TrpgBgm?.setSuppressed(false);
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
            lastState = state;
            if (!state) {
                TrpgImmersiveIllustrationModule.applyState(null);
                applyMusicSource(resolveMusicSource(state), state);
                return;
            }
            TrpgImmersiveIllustrationModule.applyState(state);
            applyMusicSource(resolveMusicSource(state), state);
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
            sync: music.sync || null,
        };
    }

    export function onMusicEvent(plan: ImmersiveMusicPlan): void {
        const roomId = roomIdOf(currentRoom);
        if (!roomId) return;
        if (plan.room_id && String(plan.room_id) !== roomId) return;
        const music = window.TrpgImmersiveMusic;
        const bgm = window.TrpgBgm;
        const source = resolveMusicSource(lastState);
        currentSource = source;
        if (source === "bgm") {
            if (music?.isEnabled()) music.shutdown();
            else music?.stop();
            bgm?.setSuppressed(false);
            bgm?.setDuck(false);
        } else {
            bgm?.setSuppressed(source === "realtime");
            music?.apply(plan);
        }
        TrpgImmersiveIllustrationModule.onMusic(plan);
        TrpgImmersiveIllustrationModule.syncMusicButton();
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

    export function renderModelSettings(): void {
        TrpgImmersiveSettingsModule.renderModelSettings();
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
    renderModelSettings: TrpgImmersiveModule.renderModelSettings,
    requestIllustration: TrpgImmersiveModule.requestIllustration,
    toggleMusic: TrpgImmersiveModule.toggleMusic,
    toggleAmbient: TrpgImmersiveModule.toggleAmbient,
    isAmbientEnabled: TrpgImmersiveModule.isAmbientEnabled,
    syncMusicButton: TrpgImmersiveIllustrationModule.syncMusicButton,
    getPersonalPreferences: TrpgImmersiveModule.getPersonalPreferences,
    getMusicSource: TrpgImmersiveModule.getMusicSource,
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => TrpgImmersiveModule.init());
} else {
    TrpgImmersiveModule.init();
}
