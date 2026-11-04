/**
 * AI 对话语音配音播放器（system / local 双引擎）。
 *
 * - ``system``：浏览器 Web Speech（默认、无外部依赖）；
 * - ``local``：向 ``/api/voice/synthesize`` 按片段拉取真实 WAV 播放，
 *   合成失败或播放失败时对该片段回落到 ``system``，并上报 ``local_*`` 降级原因。
 *
 * 设计约定：
 * - 单一播放队列，同时只播一条；新回合按站点策略「打断」或「排队」。
 * - 语音总线独立于音乐引擎，只通过公开接口对 BGM / 实时配乐做 ducking。
 * - 首次用户手势前只准备不播放，并显示「点击开启语音」，不静默失败。
 * - 所有状态通过 getState / onStateChange 暴露给聊天区按钮与设置面板。
 */
namespace TrpgVoiceModule {
    const PREF_KEY = "trpg_immersive_personal";
    const MAX_QUEUE = 3;

    interface VoicePreferences {
        voice_enabled?: boolean;
        voice_autoplay?: boolean;
        voice_volume?: number;
        voice_rate?: number;
        voice_text_only?: boolean;
        voice_muted_speakers?: string[];
    }

    interface PlaybackTask {
        payload: VoicePayload;
        messageId: string;
        force: boolean;
    }

    interface ActivePlayback extends PlaybackTask {
        segments: VoiceSegmentPayload[];
        index: number;
        settings: VoicePayloadSettings;
    }

    let initialized = false;
    const systemSupported = typeof window !== "undefined" && "speechSynthesis" in window;
    const audioSupported = typeof window !== "undefined" && typeof Audio !== "undefined";
    let supported = systemSupported || audioSupported;
    let unlocked = false;
    let voices: SpeechSynthesisVoice[] = [];
    let active: ActivePlayback | null = null;
    let queued: PlaybackTask[] = [];
    let pendingBlocked: PlaybackTask | null = null;
    let activeUtterance: SpeechSynthesisUtterance | null = null;
    let activeAudio: HTMLAudioElement | null = null;
    let activeObjectUrl: string | null = null;
    let activeAbort: AbortController | null = null;
    let restoreTimer: number | null = null;
    let ducked = false;
    let state: VoicePlaybackState = {
        status: "idle",
        reason: "",
        messageId: "",
        speaker: "",
        profileId: "",
        engine: "system",
        segmentIndex: 0,
        segmentCount: 0,
    };
    const listeners: Array<(state: VoicePlaybackState) => void> = [];

    function readPreferences(): VoicePreferences {
        try {
            const raw = window.localStorage.getItem(PREF_KEY);
            const parsed = raw ? JSON.parse(raw) : {};
            return parsed && typeof parsed === "object" ? parsed as VoicePreferences : {};
        } catch {
            return {};
        }
    }

    function writePreferences(patch: VoicePreferences): void {
        try {
            const current = readPreferences();
            window.localStorage.setItem(PREF_KEY, JSON.stringify({ ...current, ...patch }));
        } catch {
            // 浏览器禁用存储时仅在本次会话内生效，不做静默失败提示。
        }
    }

    function setState(patch: Partial<VoicePlaybackState>): void {
        state = { ...state, ...patch };
        const snapshot = { ...state };
        for (const listener of listeners.slice()) {
            try {
                listener(snapshot);
            } catch {
                // 单个监听器异常不影响播放器状态。
            }
        }
        try {
            document.dispatchEvent(new CustomEvent("voice:state", { detail: snapshot }));
        } catch {
            // 老浏览器不支持 CustomEvent 细节时忽略。
        }
    }

    function clamp(value: number, minimum: number, maximum: number): number {
        if (!Number.isFinite(value)) return minimum;
        return Math.max(minimum, Math.min(maximum, value));
    }

    /** 播放状态回报给服务端，补齐 voice_playback 结构化事件；失败不影响本地播放。 */
    function reportPlayback(
        status: string,
        task: PlaybackTask,
        segment?: VoiceSegmentPayload,
        segmentCount = 0,
        reason = ""
    ): void {
        try {
            const body = {
                room_id: task.payload.room_id || "",
                message_id: task.messageId || task.payload.message_id || "",
                turn: task.payload.turn || 0,
                speaker: segment?.display_name || "",
                profile_id: segment?.profile_id || "",
                engine: segment?.engine || task.payload.engine || "system",
                text_chars: segment?.text ? segment.text.length : 0,
                segment_count: segmentCount,
                degraded: status !== "playing" && status !== "ended",
                reason: reason || status,
            };
            const headers: Record<string, string> = { "Content-Type": "application/json" };
            const csrf = window.currentUser?.csrf_token;
            if (csrf) headers["X-CSRF-Token"] = csrf;
            void fetch("/api/voice/playback", {
                method: "POST",
                headers,
                credentials: "same-origin",
                body: JSON.stringify(body),
                keepalive: true,
            }).catch(() => undefined);
        } catch {
            // 播放回报失败不阻塞语音。
        }
    }

    function loadVoices(): void {
        if (!systemSupported) return;
        try {
            voices = window.speechSynthesis.getVoices() || [];
        } catch {
            voices = [];
        }
    }

    function zhVoices(): SpeechSynthesisVoice[] {
        return voices.filter((voice) => {
            const lang = String(voice.lang || "").toLowerCase();
            const name = String(voice.name || "");
            return lang.startsWith("zh") || lang.indexOf("cmn") >= 0 || /中文|普通话|国语/.test(name);
        });
    }

    /** 返回 {voice, reason}；匹配不到中文音色时使用浏览器默认音色并记录降级原因。 */
    function pickVoice(segment: VoiceSegmentPayload): { voice: SpeechSynthesisVoice | null; reason: string } {
        const candidates = zhVoices();
        if (!candidates.length) {
            return { voice: null, reason: "no_zh_voice" };
        }
        const preferred = Array.isArray(segment.preferred_voices) ? segment.preferred_voices : [];
        for (const token of preferred) {
            const needle = String(token || "").toLowerCase();
            if (!needle) continue;
            const hit = candidates.find((voice) => {
                const name = String(voice.name || "").toLowerCase();
                const lang = String(voice.lang || "").toLowerCase();
                if (needle.startsWith("zh") || needle.indexOf("chinese") >= 0) {
                    return lang.startsWith(needle.slice(0, 2)) || lang.indexOf("cmn") >= 0;
                }
                return name.indexOf(needle) >= 0;
            });
            if (hit) return { voice: hit, reason: "" };
        }
        return { voice: candidates[0] ?? null, reason: "profile_voice_miss" };
    }

    function effectiveSettings(payload: VoicePayload): VoicePayloadSettings {
        const prefs = readPreferences();
        const settings = { ...payload.settings };
        if (settings.allow_personal !== false) {
            if (typeof prefs.voice_volume === "number") {
                settings.volume = clamp(prefs.voice_volume, 0, 100);
            }
            if (typeof prefs.voice_rate === "number") {
                settings.rate = clamp(prefs.voice_rate, 50, 200);
            }
            if (typeof prefs.voice_autoplay === "boolean") {
                settings.autoplay = prefs.voice_autoplay;
            }
        }
        return settings;
    }

    function filteredSegments(payload: VoicePayload): VoiceSegmentPayload[] {
        const prefs = readPreferences();
        const muted = new Set(
            (Array.isArray(prefs.voice_muted_speakers) ? prefs.voice_muted_speakers : [])
                .map((item) => String(item || "").trim().toLowerCase())
                .filter(Boolean)
        );
        return (payload.segments || []).filter((segment) => {
            if (segment.kind === "narrator") return true;
            const name = String(segment.display_name || "").trim().toLowerCase();
            const id = String(segment.speaker_id || "").trim().toLowerCase();
            return !muted.has(name) && !muted.has(id);
        });
    }

    function speakerSummary(segments: VoiceSegmentPayload[]): string {
        const names: string[] = [];
        for (const segment of segments) {
            const name = String(segment.display_name || "");
            if (name && names.indexOf(name) < 0) names.push(name);
        }
        return names.slice(0, 4).join("、") + (names.length > 4 ? " 等" : "");
    }

    function applyDucking(settings: VoicePayloadSettings): void {
        if (!settings.ducking) return;
        if (restoreTimer !== null) {
            window.clearTimeout(restoreTimer);
            restoreTimer = null;
        }
        if (ducked) return;
        ducked = true;
        try {
            window.TrpgBgm?.setDuck?.(true);
            window.TrpgImmersiveMusic?.setDucking?.(settings.ducking_level);
        } catch {
            // 音乐未初始化时忽略，不影响语音播放。
        }
    }

    /** 结束后 400ms 平滑恢复；攻击 80ms 由音乐引擎内部完成。 */
    function scheduleRestore(): void {
        if (!ducked) return;
        if (restoreTimer !== null) window.clearTimeout(restoreTimer);
        restoreTimer = window.setTimeout(() => {
            restoreTimer = null;
            ducked = false;
            try {
                window.TrpgBgm?.setDuck?.(false);
                window.TrpgImmersiveMusic?.setDucking?.(0);
            } catch {
                // 忽略音乐恢复失败。
            }
        }, 400);
    }

    function finish(status: VoicePlaybackStatus = "ready", reason = ""): void {
        const finished = active;
        if (finished) {
            reportPlayback(status === "ready" ? "ended" : status, finished, finished.segments[finished.segments.length - 1], finished.segments.length, reason);
        }
        cleanupAudio();
        active = null;
        activeUtterance = null;
        setState({ status, reason, speaker: "", segmentIndex: 0, segmentCount: 0 });
        scheduleRestore();
        const next = queued.shift();
        if (next) {
            playTask(next);
        }
    }

    function failCurrent(reason: string, continueNext: boolean): void {
        if (!active) return;
        setState({ status: "failed", reason });
        if (continueNext) {
            active.index += 1;
            speakNext();
        } else {
            finish("failed", reason);
        }
    }

    function cleanupAudio(): void {
        try {
            activeAbort?.abort();
        } catch {
            // 忽略取消请求失败。
        }
        activeAbort = null;
        if (activeAudio) {
            activeAudio.onended = null;
            activeAudio.onerror = null;
            activeAudio.onplaying = null;
            try {
                activeAudio.pause();
                activeAudio.src = "";
            } catch {
                // 忽略音频回收失败。
            }
            activeAudio = null;
        }
        if (activeObjectUrl) {
            try {
                URL.revokeObjectURL(activeObjectUrl);
            } catch {
                // 忽略 URL 回收失败。
            }
            activeObjectUrl = null;
        }
    }

    function speakNext(): void {
        if (!active) return;
        const task = active;
        if (task.index >= task.segments.length) {
            finish("ready", "");
            return;
        }
        const segment = task.segments[task.index];
        if (!segment) {
            finish("ready", "");
            return;
        }
        if (task.payload.engine === "local" && audioSupported) {
            speakLocalSegment(task, segment);
            return;
        }
        speakSystemSegment(task, segment, "");
    }

    /** 本地引擎：按片段拉取真实 WAV；任一步失败都把该片段回落给 system。 */
    function speakLocalSegment(task: ActivePlayback, segment: VoiceSegmentPayload): void {
        const roomId = task.payload.room_id || "";
        const messageId = task.messageId || task.payload.message_id || "";
        setState({
            status: "playing",
            reason: "local_synthesizing",
            messageId,
            engine: "local",
            speaker: segment.display_name || "旁白",
            profileId: segment.profile_id || "",
            segmentIndex: task.index,
            segmentCount: task.segments.length,
        });
        const headers: Record<string, string> = { "Content-Type": "application/json" };
        const csrf = window.currentUser?.csrf_token;
        if (csrf) headers["X-CSRF-Token"] = csrf;
        const controller = new AbortController();
        activeAbort = controller;
        const body = {
            room_id: roomId,
            message_id: messageId,
            turn: task.payload.turn || 0,
            text: String(segment.text || ""),
            profile_id: segment.profile_id || "narrator",
            sid: segment.speaker_sid,
            speaker: segment.display_name || "",
            speed: 1,
        };
        void fetch("/api/voice/synthesize", {
            method: "POST",
            headers,
            credentials: "same-origin",
            signal: controller.signal,
            body: JSON.stringify(body),
        })
            .then((response) => {
                if (!response.ok) throw new Error(`local_${response.status}`);
                return response.blob();
            })
            .then((blob) => {
                if (!active || active !== task) return;
                activeAbort = null;
                if (!blob || blob.size === 0) throw new Error("local_empty_audio");
                const objectUrl = URL.createObjectURL(blob);
                activeObjectUrl = objectUrl;
                const audio = new Audio(objectUrl);
                activeAudio = audio;
                audio.preload = "auto";
                audio.volume = clamp(task.settings.volume / 100, 0, 1);
                audio.onplaying = () => {
                    setState({
                        status: "playing",
                        reason: "",
                        speaker: segment.display_name || "旁白",
                        engine: "local",
                        segmentIndex: task.index,
                    });
                    reportPlayback("playing", task, segment, task.segments.length);
                };
                audio.onended = () => {
                    cleanupAudio();
                    if (!active || active !== task) return;
                    task.index += 1;
                    speakNext();
                };
                audio.onerror = () => {
                    cleanupAudio();
                    if (!active || active !== task) return;
                    reportPlayback("degraded", task, segment, task.segments.length, "local_playback_error");
                    speakSystemSegment(task, segment, "local_playback_error");
                };
                void audio.play().catch((error: unknown) => {
                    cleanupAudio();
                    if (!active || active !== task) return;
                    const name = String((error as { name?: string } | null)?.name || "");
                    if (name === "NotAllowedError") {
                        pendingBlocked = { ...task };
                        finish("blocked", "gesture_required");
                        showUnlockBanner(task.payload);
                        return;
                    }
                    reportPlayback("degraded", task, segment, task.segments.length, "local_playback_error");
                    speakSystemSegment(task, segment, "local_playback_error");
                });
            })
            .catch((error: unknown) => {
                if (!active || active !== task) return;
                if (String((error as { name?: string } | null)?.name || "") === "AbortError") return;
                activeAbort = null;
                const reason = "local_unavailable";
                reportPlayback("degraded", task, segment, task.segments.length, reason);
                speakSystemSegment(task, segment, reason);
            });
    }

    /** 系统引擎：浏览器 Web Speech；``fallbackReason`` 用于本地引擎失败后的降级标记。 */
    function speakSystemSegment(task: ActivePlayback, segment: VoiceSegmentPayload, fallbackReason: string): void {
        if (!systemSupported) {
            failCurrent(fallbackReason || "speech_unsupported", true);
            return;
        }
        const { voice, reason } = pickVoice(segment);
        const utterance = new SpeechSynthesisUtterance(String(segment.text || ""));
        activeUtterance = utterance;
        utterance.lang = voice?.lang || "zh-CN";
        if (voice) utterance.voice = voice;
        utterance.pitch = clamp(Number(segment.pitch) || 1, 0.1, 2);
        utterance.rate = clamp((Number(segment.rate) || 1) * (task.settings.rate / 100), 0.5, 2);
        utterance.volume = clamp(task.settings.volume / 100, 0, 1);
        const mergedReason = fallbackReason || reason;
        utterance.onstart = () => {
            setState({
                status: "playing",
                reason: mergedReason,
                speaker: segment.display_name || "旁白",
                profileId: segment.profile_id || "",
                engine: "system",
                segmentIndex: task.index,
                segmentCount: task.segments.length,
            });
            reportPlayback("playing", task, segment, task.segments.length);
        };
        utterance.onend = () => {
            if (!active) return;
            active.index += 1;
            speakNext();
        };
        utterance.onerror = (event: SpeechSynthesisErrorEvent) => {
            const error = String(event.error || "");
            if (error === "interrupted" || error === "canceled") {
                if (active) {
                    active.index += 1;
                    speakNext();
                }
                return;
            }
            if (error === "not-allowed") {
                if (active) pendingBlocked = { ...task };
                finish("blocked", "gesture_required");
                showUnlockBanner(task.payload);
                return;
            }
            // 单个片段失败时继续后面片段，避免整条叙事静默。
            failCurrent(error || "speech_error", true);
        };
        try {
            window.speechSynthesis.speak(utterance);
        } catch (error) {
            failCurrent(fallbackReason || "speech_unavailable", true);
        }
    }

    function playTask(task: PlaybackTask): void {
        const segments = filteredSegments(task.payload);
        if (!segments.length) {
            setState({ status: "degraded", reason: "all_muted", messageId: task.messageId, engine: task.payload.engine });
            return;
        }
        cleanupAudio();
        active = {
            ...task,
            segments,
            index: 0,
            settings: effectiveSettings(task.payload),
        };
        applyDucking(active.settings);
        const first = segments[0];
        setState({
            status: "playing",
            reason: "",
            messageId: task.messageId || task.payload.message_id || "",
            engine: task.payload.engine,
            speaker: first?.display_name || "旁白",
            profileId: first?.profile_id || "",
            segmentIndex: 0,
            segmentCount: segments.length,
        });
        speakNext();
    }

    function stopActive(status: VoicePlaybackStatus, reason: string): void {
        const interrupted = active;
        if (interrupted) {
            reportPlayback("interrupted", interrupted, interrupted.segments[interrupted.index], interrupted.segments.length, reason);
        }
        queued = [];
        pendingBlocked = null;
        cleanupAudio();
        if (systemSupported) {
            try {
                window.speechSynthesis.cancel();
            } catch {
                // 忽略取消失败。
            }
        }
        activeUtterance = null;
        active = null;
        setState({ status, reason, speaker: "", segmentIndex: 0, segmentCount: 0 });
        scheduleRestore();
    }

    function showUnlockBanner(payload: VoicePayload): void {
        const host = document.getElementById("chatHistory")
            || document.querySelector<HTMLElement>(".chat-history")
            || document.querySelector<HTMLElement>("#chatMessages");
        if (!host) {
            showNotification("语音已就绪，请点击聊天区任意位置开启语音。", "info");
            return;
        }
        let banner = host.querySelector<HTMLElement>(".voice-unlock-banner");
        if (!banner) {
            banner = document.createElement("div");
            banner.className = "voice-unlock-banner";
            const text = document.createElement("span");
            text.textContent = "语音已就绪，浏览器需要一次点击才能发声。";
            const button = document.createElement("button");
            button.type = "button";
            button.className = "voice-unlock-btn";
            button.textContent = "点击开启语音";
            button.addEventListener("click", () => {
                markUnlocked();
                const pending = pendingBlocked;
                pendingBlocked = null;
                banner?.remove();
                if (pending) {
                    playPayload(pending.payload, { force: true, messageId: pending.messageId });
                } else {
                    playPayload(payload, { force: true, messageId: payload.message_id });
                }
            });
            banner.append(text, button);
            host.insertBefore(banner, host.firstChild);
        }
    }

    function markUnlocked(): void {
        unlocked = true;
        const banner = document.querySelector<HTMLElement>(".voice-unlock-banner");
        banner?.remove();
    }

    export function init(): void {
        if (initialized) return;
        initialized = true;
        loadVoices();
        if (!supported) {
            setState({ status: "failed", reason: "speech_unsupported" });
            return;
        }
        if (!systemSupported) {
            return;
        }
        try {
            window.speechSynthesis.addEventListener?.("voiceschanged", loadVoices);
        } catch {
            window.speechSynthesis.onvoiceschanged = loadVoices;
        }
        const unlockHandler = () => {
            if (unlocked) return;
            markUnlocked();
            const pending = pendingBlocked;
            if (pending) {
                pendingBlocked = null;
                playPayload(pending.payload, { force: true, messageId: pending.messageId });
            }
        };
        document.addEventListener("pointerdown", unlockHandler, { once: true });
        document.addEventListener("keydown", unlockHandler, { once: true });
    }

    export function playPayload(
        payload: VoicePayload | null | undefined,
        options: { force?: boolean; messageId?: string } = {}
    ): void {
        if (!payload || !Array.isArray(payload.segments) || !payload.segments.length) {
            return;
        }
        const messageId = options.messageId || payload.message_id || "";
        if (payload.engine !== "system" && payload.engine !== "local") {
            setState({ status: "degraded", reason: payload.reason || "engine_unavailable", messageId, engine: payload.engine });
            return;
        }
        if (!payload.playable) {
            setState({ status: "degraded", reason: payload.reason || "not_playable", messageId, engine: payload.engine });
            return;
        }
        const prefs = readPreferences();
        if (prefs.voice_enabled === false || prefs.voice_text_only === true) {
            setState({ status: "disabled", reason: prefs.voice_text_only ? "text_only" : "personal_off", messageId });
            return;
        }
        const segments = filteredSegments(payload);
        if (!segments.length) {
            setState({ status: "degraded", reason: "all_muted", messageId });
            return;
        }
        const settings = effectiveSettings(payload);
        const task: PlaybackTask = { payload, messageId, force: Boolean(options.force) };
        if (!options.force && !settings.autoplay) {
            setState({
                status: "ready",
                reason: "autoplay_off",
                messageId,
                engine: payload.engine,
                speaker: speakerSummary(segments),
                segmentCount: segments.length,
            });
            return;
        }
        if (!unlocked) {
            pendingBlocked = task;
            setState({ status: "blocked", reason: "gesture_required", messageId, engine: payload.engine });
            showUnlockBanner(payload);
            return;
        }
        if (active && settings.interrupt_policy === "queue") {
            if (queued.length >= MAX_QUEUE) queued.shift();
            queued.push(task);
            return;
        }
        if (systemSupported) {
            try {
                window.speechSynthesis.cancel();
            } catch {
                // 忽略取消失败。
            }
        }
        active = null;
        playTask(task);
    }

    function payloadFromMessage(message: VoiceMessageLike): VoicePayload | null {
        const metadata = message.metadata || {};
        const candidate = metadata.voice;
        if (!candidate || typeof candidate !== "object") return null;
        const payload = candidate as VoicePayload;
        if (!Array.isArray(payload.segments) || !payload.segments.length) return null;
        return payload;
    }

    export function attachMessageButton(element: HTMLElement, message: VoiceMessageLike, animate = false): void {
        const payload = payloadFromMessage(message);
        if (!payload || !element) return;
        const host = element.querySelector<HTMLElement>(".message-meta") || element;
        if (host.querySelector(".voice-replay-btn")) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "voice-replay-btn";
        const label = payload.reason === "engine_unavailable" ? "语音引擎不可用" : "朗读这条消息";
        const voiceLabel = payload.engine === "local" ? "本地音色" : "浏览器音色";
        const summary = payload.segments?.length ? speakerSummary(payload.segments) : "";
        button.title = summary ? `${label}（${voiceLabel}：${summary}）` : `${label}（${voiceLabel}）`;
        button.setAttribute("aria-label", label);
        button.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">'
            + '<path d="M4 9v6h4l5 4V5L8 9H4z" fill="currentColor"/>'
            + '<path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8 8 0 0 1 0 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>'
            + '</svg>';
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            markUnlocked();
            stopActive("interrupted", "manual_replay");
            playPayload(payload, { force: true, messageId: message.id || payload.message_id });
        });
        host.appendChild(button);
        if (animate) {
            playPayload(payload, { messageId: message.id || payload.message_id });
        }
    }

    export function attachToMessage(
        messageId: string,
        payload: VoicePayload | null | undefined,
        animate = false
    ): void {
        if (!payload) return;
        const selector = `[data-message-id="${String(messageId || "").replace(/"/g, '\\"')}"]`;
        const element = messageId ? document.querySelector<HTMLElement>(selector) : null;
        if (element) {
            attachMessageButton(element, { id: messageId, metadata: { voice: payload } }, animate);
        } else if (animate) {
            playPayload(payload, { messageId });
        }
    }

    export function stop(): void {
        stopActive("interrupted", "manual_stop");
    }

    export function getState(): VoicePlaybackState {
        return { ...state };
    }

    export function onStateChange(listener: (state: VoicePlaybackState) => void): () => void {
        listeners.push(listener);
        return () => {
            const index = listeners.indexOf(listener);
            if (index >= 0) listeners.splice(index, 1);
        };
    }

    export function isSupported(): boolean {
        return supported;
    }

    export function refreshPreferences(): void {
        // 偏好按播放时实时读取；此处仅提供显式刷新入口。
        setState({});
    }

    export function setTextOnly(value: boolean): void {
        writePreferences({ voice_text_only: Boolean(value) });
        if (value) stopActive("disabled", "text_only");
    }

    export function setMutedSpeakers(names: string[]): void {
        writePreferences({ voice_muted_speakers: Array.isArray(names) ? names : [] });
    }
}

TrpgVoiceModule.init();

window.TrpgVoice = {
    init: TrpgVoiceModule.init,
    playPayload: TrpgVoiceModule.playPayload,
    attachMessageButton: TrpgVoiceModule.attachMessageButton,
    attachToMessage: TrpgVoiceModule.attachToMessage,
    stop: TrpgVoiceModule.stop,
    getState: TrpgVoiceModule.getState,
    onStateChange: TrpgVoiceModule.onStateChange,
    isSupported: TrpgVoiceModule.isSupported,
    refreshPreferences: TrpgVoiceModule.refreshPreferences,
    setTextOnly: TrpgVoiceModule.setTextOnly,
    setMutedSpeakers: TrpgVoiceModule.setMutedSpeakers,
};
