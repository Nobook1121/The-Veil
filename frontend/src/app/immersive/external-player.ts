/**
 * 外链 BGM 播放器（P3）。
 *
 * 关键约束（比「能播」更重要）：
 *
 * 1. **底床永不停**：外链只做「叠上去」的一层；加载失败、未就绪、播放出错时
 *    立刻把底床抬回 1，而不是先静音再补救；
 * 2. **等功率交叉淡化**：底床用 cos 曲线下降、外链用 sin 曲线上升，功率之和恒定；
 * 3. **切换点对齐小节/乐句边界**：从 `TrpgImmersiveMusic.barTiming()` 取当前小节
 *    时基，优先对齐到最近的乐句边界（最多等 4 小节），否则等下一条小节线；
 * 4. **不阻塞主线程**：所有网络等待都是 `canplay` 事件 + 超时，失败仅降级日志；
 * 5. **不硬切**：引擎 `off` / 切回程序化 / 换曲都用淡化，暂停动作放在淡化之后。
 *
 * 音频图：`<audio> → elementGain → engineGain → musicMuteGain → 限幅/EQ/主输出`。
 * 外链不经过底床增益，所以压低底床不会压低外链；两者都受引擎总增益与配乐静音控制。
 */
namespace TrpgImmersiveExternalModule {
    const LOAD_TIMEOUT_MS = 7000;
    const LOOP_FADE_SECONDS = 1.8;
    const LOOP_MIN_DURATION = 12;
    const DEFAULT_FADE_MS = 4200;

    type ExternalMode = "idle" | "loading" | "playing";

    let elements: HTMLAudioElement[] | null = null;
    let gains: GainNode[] | null = null;
    let sourceCtx: AudioContext | null = null;
    let activeIndex = 0;
    /** 真正在出声的元素下标；-1 表示没有外链在播（此时底床必须是 1）。 */
    let playingIndex = -1;
    let currentTrack: ImmersiveExternalTrack | null = null;
    /** 正在出声的曲目，用于「新曲加载失败时继续放旧的」而不是直接掉回底床。 */
    let playingTrack: ImmersiveExternalTrack | null = null;
    let mode: ExternalMode = "idle";
    /** 每次换曲/停止都递增；异步回调据此丢弃过期结果。 */
    let generation = 0;
    let pendingTimers: number[] = [];
    let lastReason = "";
    let lastError = "";
    let lastBedLevel = 1;

    function clamp(value: number, minimum: number, maximum: number): number {
        if (!Number.isFinite(value)) return minimum;
        return Math.max(minimum, Math.min(maximum, value));
    }

    function clearTimers(): void {
        for (const timer of pendingTimers) window.clearTimeout(timer);
        pendingTimers = [];
    }

    function later(handler: () => void, delayMs: number): void {
        pendingTimers.push(window.setTimeout(handler, Math.max(0, delayMs)));
    }

    function setAttribution(track: ImmersiveExternalTrack | null, suffix = ""): void {
        const node = document.getElementById("immersiveStageTrack");
        if (!node) return;
        if (!track) {
            node.hidden = true;
            node.textContent = "";
            node.removeAttribute("title");
            return;
        }
        const text = suffix ? `${track.attribution} · ${suffix}` : track.attribution;
        node.hidden = false;
        node.textContent = `♪ ${text}`;
        node.setAttribute("title", track.source_url || track.attribution);
        (node as HTMLElement).onclick = () => {
            if (track.source_url) window.open(track.source_url, "_blank", "noopener,noreferrer");
        };
    }

    function teardownGraph(): void {
        if (elements) {
            for (const element of elements) {
                try {
                    element.pause();
                    element.removeAttribute("src");
                    element.load();
                    element.remove();
                } catch {
                    // 元素已分离时忽略。
                }
            }
        }
        elements = null;
        gains = null;
        sourceCtx = null;
        playingIndex = -1;
        playingTrack = null;
    }

    function createElement(): HTMLAudioElement {
        const element = document.createElement("audio");
        element.preload = "auto";
        element.crossOrigin = "anonymous";
        element.style.display = "none";
        document.body.appendChild(element);
        return element;
    }

    function ensureGraph(): { elements: HTMLAudioElement[]; gains: GainNode[]; ctx: AudioContext } | null {
        const api = window.TrpgImmersiveMusic;
        const input = api?.externalInput?.();
        if (!api || !input) return null;
        const ctx = input.context as AudioContext;
        if (elements && gains && sourceCtx === ctx) return { elements, gains, ctx };
        teardownGraph();
        const first = createElement();
        const second = createElement();
        const firstGain = ctx.createGain();
        const secondGain = ctx.createGain();
        firstGain.gain.value = 0.0001;
        secondGain.gain.value = 0.0001;
        ctx.createMediaElementSource(first).connect(firstGain);
        ctx.createMediaElementSource(second).connect(secondGain);
        firstGain.connect(input);
        secondGain.connect(input);
        const created = [first, second];
        const createdGains = [firstGain, secondGain];
        elements = created;
        gains = createdGains;
        sourceCtx = ctx;
        return { elements, gains, ctx };
    }

    /** 等待可播放；`error` / 超时都返回 false，绝不 reject 到调用方之外。 */
    function waitReady(element: HTMLAudioElement, timeoutMs: number): Promise<boolean> {
        return new Promise((resolve) => {
            if (element.readyState >= 3) {
                resolve(true);
                return;
            }
            let settled = false;
            const finish = (ok: boolean): void => {
                if (settled) return;
                settled = true;
                element.removeEventListener("canplay", onReady);
                element.removeEventListener("canplaythrough", onReady);
                element.removeEventListener("loadeddata", onReady);
                element.removeEventListener("error", onError);
                window.clearTimeout(timer);
                resolve(ok);
            };
            const onReady = (): void => finish(true);
            const onError = (): void => {
                lastError = element.error ? `media_${element.error.code}` : "media_error";
                finish(false);
            };
            const timer = window.setTimeout(() => finish(false), Math.max(1000, timeoutMs));
            element.addEventListener("canplay", onReady);
            element.addEventListener("canplaythrough", onReady);
            element.addEventListener("loadeddata", onReady);
            element.addEventListener("error", onError);
        });
    }

    /** 等功率增益曲线：上升用 sin、下降用 cos，与底床曲线配对。 */
    function rampGain(gain: GainNode, target: number, seconds: number, rising: boolean, ctx: AudioContext): void {
        const now = ctx.currentTime;
        const from = Math.max(0.0001, gain.gain.value);
        const to = Math.max(0.0001, target);
        const duration = Math.max(0.08, seconds);
        const samples = 64;
        const curve = new Float32Array(samples);
        for (let index = 0; index < samples; index += 1) {
            const progress = samples <= 1 ? 1 : index / (samples - 1);
            const shape = rising
                ? Math.sin((progress * Math.PI) / 2)
                : Math.cos((progress * Math.PI) / 2);
            curve[index] = Math.max(0.0001, from + (to - from) * shape);
        }
        gain.gain.cancelScheduledValues(now);
        gain.gain.setValueAtTime(from, now);
        try {
            gain.gain.setValueCurveAtTime(curve, now, duration);
        } catch {
            gain.gain.exponentialRampToValueAtTime(to, now + duration);
        }
    }

    function rampBed(target: number, seconds: number, rising: boolean): void {
        const api = window.TrpgImmersiveMusic;
        if (!api?.rampBedLevel) return;
        lastBedLevel = target;
        try {
            api.rampBedLevel(target, seconds, rising);
        } catch {
            // 音频图尚未建立时忽略；底床本来就是 1。
        }
    }

    /** 外链接入最多等这么久：4s 淡入本身已跨越大半个慢速小节，再等更久只会显得迟滞。 */
    const MAX_ALIGN_WAIT_SECONDS = 3.2;

    /**
     * 计算切换等待：对齐下一条小节线；如果下一小节正好是乐句边界则天然对齐。
     *
     * 不为了「凑满 4/8 小节乐句」而额外等待——慢速氛围曲的小节长达 5s+，
     * 多等两小节会让接入延迟超过 10s，听感上像卡住。上限 3.2s，保证
     * 「请求后很快听到，但仍落在小节线上」。
     */
    function boundaryDelaySeconds(_phraseBars: number): number {
        const timing = window.TrpgImmersiveMusic?.barTiming?.();
        if (!timing) return 0.15;
        const wait = Math.max(0.12, timing.nextBarIn);
        return Math.min(wait, MAX_ALIGN_WAIT_SECONDS);
    }

    function fadeSeconds(payload: ImmersiveExternalEvent): number {
        const raw = payload.fade_ms ?? payload.crossfade_ms ?? DEFAULT_FADE_MS;
        return clamp(Number(raw) / 1000, 1.5, 12);
    }

    /**
     * 降级：只丢弃「没接上的那一首」。
     *
     * - 如果已有外链在播，保持它和压低的底床，用户听不到断档；
     * - 如果没有，抬回底床并清空署名；
     * - 任何情况下都不把播放器推进错误态，也不阻塞。
     */
    function degrade(reason: string): void {
        lastReason = reason;
        clearTimers();
        generation += 1;
        const token = generation;
        const keep = playingIndex;
        const graph = elements && gains && sourceCtx ? { elements, gains, ctx: sourceCtx } : null;
        if (graph) {
            for (let index = 0; index < graph.gains.length; index += 1) {
                if (index === keep) continue;
                const gain = graph.gains[index];
                if (gain) rampGain(gain, 0.0001, 0.9, false, graph.ctx);
            }
        }
        if (keep >= 0 && playingTrack) {
            // 旧曲仍在：恢复「播放中」状态并续上循环调度。
            mode = "playing";
            currentTrack = playingTrack;
            setAttribution(playingTrack, "播放中");
            scheduleLoop(token);
            return;
        }
        mode = "idle";
        currentTrack = null;
        playingTrack = null;
        setAttribution(null);
        rampBed(1, 0.9, true);
        later(() => {
            if (generation !== token || mode !== "idle") return;
            if (elements) {
                for (const element of elements) {
                    try {
                        element.pause();
                    } catch {
                        // 忽略已销毁元素。
                    }
                }
            }
        }, 950);
    }

    /** 外链停止：先淡化，再暂停；底床同步抬回 1。 */
    function stop(fadeMs = 1800): void {
        const seconds = clamp(Number(fadeMs) / 1000, 0.35, 8);
        clearTimers();
        generation += 1;
        const graph = elements && gains && sourceCtx ? { elements, gains, ctx: sourceCtx } : null;
        if (graph) {
            for (const gain of graph.gains) rampGain(gain, 0.0001, seconds, false, graph.ctx);
        }
        rampBed(1, seconds, true);
        const token = generation;
        mode = "idle";
        playingIndex = -1;
        playingTrack = null;
        currentTrack = null;
        lastReason = "stopped";
        setAttribution(null);
        later(() => {
            if (generation !== token) return;
            if (elements) {
                for (const element of elements) {
                    try {
                        element.pause();
                        element.currentTime = 0;
                    } catch {
                        // 忽略。
                    }
                }
            }
        }, Math.round(seconds * 1000) + 80);
    }

    function scheduleLoop(token: number): void {
        if (!elements || !currentTrack || mode !== "playing") return;
        const element = elements[activeIndex];
        if (!element) return;
        const duration = Number(element.duration);
        if (!Number.isFinite(duration) || duration < LOOP_MIN_DURATION) return;
        const remaining = duration - element.currentTime - LOOP_FADE_SECONDS;
        later(() => void loopOnce(token), Math.max(0, remaining) * 1000);
    }

    async function loopOnce(token: number): Promise<void> {
        if (token !== generation || mode !== "playing" || !currentTrack || !elements || !gains || !sourceCtx) return;
        const nextIndex = activeIndex === 0 ? 1 : 0;
        const element = elements[nextIndex];
        const gain = gains[nextIndex];
        if (!element || !gain) return;
        gain.gain.cancelScheduledValues(sourceCtx.currentTime);
        gain.gain.value = 0.0001;
        element.pause();
        element.src = currentTrack.stream_path;
        element.load();
        const ready = await waitReady(element, 4200);
        if (!ready || token !== generation || mode !== "playing") return;
        try {
            await element.play();
        } catch {
            lastError = "loop_play_rejected";
            return;
        }
        const previousIndex = activeIndex;
        activeIndex = nextIndex;
        playingIndex = nextIndex;
        const previousGain = gains[previousIndex];
        if (previousGain) rampGain(previousGain, 0.0001, LOOP_FADE_SECONDS, false, sourceCtx);
        rampGain(gain, 1, LOOP_FADE_SECONDS, true, sourceCtx);
        later(() => {
            if (token !== generation) return;
            try {
                elements?.[previousIndex]?.pause();
            } catch {
                // 忽略。
            }
        }, LOOP_FADE_SECONDS * 1000 + 80);
        scheduleLoop(token);
    }

    async function beginTrack(next: ImmersiveExternalTrack, payload: ImmersiveExternalEvent): Promise<void> {
        const api = window.TrpgImmersiveMusic;
        if (api?.currentEngineMode?.() === "off") return;
        try {
            api?.unlock?.();
        } catch {
            // 解锁失败交给 play() 的 reject 处理。
        }
        const graph = ensureGraph();
        if (!graph) {
            degrade("no_audio_graph");
            return;
        }
        const token = generation + 1;
        generation = token;
        clearTimers();
        // 有外链正在出声时用另一个元素预加载，避免覆盖正在播放的那一路。
        const index = playingIndex >= 0 ? (playingIndex === 0 ? 1 : 0) : activeIndex;
        const element = graph.elements[index];
        if (!element) {
            degrade("no_audio_element");
            return;
        }
        mode = "loading";
        currentTrack = next;
        lastReason = "";
        lastError = "";
        setAttribution(next, "接入中");

        element.pause();
        element.src = next.stream_path;
        element.load();
        const ready = await waitReady(element, LOAD_TIMEOUT_MS);
        if (token !== generation) return;
        if (!ready) {
            degrade(lastError || "load_timeout");
            return;
        }
        const waitSeconds = boundaryDelaySeconds(Number(payload.phrase_bars) || 8);
        later(() => {
            void startPlayback(element, index, next, fadeSeconds(payload), token);
        }, waitSeconds * 1000);
    }

    async function startPlayback(
        element: HTMLAudioElement,
        index: number,
        track: ImmersiveExternalTrack,
        fade: number,
        token: number,
    ): Promise<void> {
        if (token !== generation || !gains || !sourceCtx) return;
        try {
            await element.play();
        } catch {
            lastError = "play_rejected";
            degrade("play_rejected");
            return;
        }
        if (token !== generation || !gains || !sourceCtx) {
            element.pause();
            return;
        }
        const previousIndex = playingIndex >= 0 && playingIndex !== index ? playingIndex : -1;
        const api = window.TrpgImmersiveMusic;
        const bedLevel = api?.bedDuckLevel ? api.bedDuckLevel() : 0.38;
        rampBed(bedLevel, fade, false);
        const previousGain = previousIndex >= 0 ? gains[previousIndex] : undefined;
        if (previousGain) {
            rampGain(previousGain, 0.0001, fade, false, sourceCtx);
            later(() => {
                if (token !== generation) return;
                try {
                    elements?.[previousIndex]?.pause();
                } catch {
                    // 忽略。
                }
            }, fade * 1000 + 80);
        }
        const targetGain = gains[index];
        if (targetGain) rampGain(targetGain, 1, fade, true, sourceCtx);
        element.onended = () => {
            if (token !== generation) return;
            // 循环调度失败时的兜底：轨道自然结束后退回底床，不报错、不静音。
            lastReason = "track_ended";
            mode = "idle";
            if (playingIndex === index) playingIndex = -1;
            playingTrack = null;
            rampBed(1, 1.4, true);
            setAttribution(null);
            currentTrack = null;
        };
        activeIndex = index;
        playingIndex = index;
        playingTrack = track;
        mode = "playing";
        setAttribution(track, "播放中");
        scheduleLoop(token);
    }

    export function applyEvent(payload: ImmersiveExternalEvent | null | undefined): void {
        if (!payload) return;
        if (payload.status === "disabled") {
            stop(1200);
            return;
        }
        if (payload.status !== "selected" || !payload.track) {
            lastReason = payload.reason || payload.status;
            // 已在播放的曲子继续播；空结果/降级不打断当前氛围。
            if (mode !== "playing") setAttribution(null);
            return;
        }
        const engine = window.TrpgImmersiveMusic?.currentEngineMode?.();
        if (engine === "procedural" || engine === "off") return;
        if (currentTrack && currentTrack.track_key === payload.track.track_key && mode === "playing") {
            return;
        }
        void beginTrack(payload.track, payload);
    }

    export function applyEngine(event: ImmersiveMusicEngineEvent | null | undefined): void {
        if (!event) return;
        const engine = event.engine === "external" || event.engine === "off" ? event.engine : "procedural";
        try {
            window.TrpgImmersiveMusic?.setEngineMode?.(engine, Number(event.fade_ms) || 2200);
        } catch {
            // 引擎尚未初始化时忽略，外链本身也不会开始。
        }
        if (engine !== "external") {
            stop(Number(event.fade_ms) || 1800);
        }
    }

    export function stopAll(fadeMs = 1200): void {
        stop(fadeMs);
    }

    export function state(): Record<string, unknown> {
        return {
            mode,
            reason: lastReason,
            error: lastError,
            track_key: currentTrack?.track_key || "",
            attribution: currentTrack?.attribution || "",
            bed_level: lastBedLevel,
            engine: window.TrpgImmersiveMusic?.currentEngineMode?.() || "unknown",
        };
    }
}

window.TrpgImmersiveExternal = {
    applyEvent: TrpgImmersiveExternalModule.applyEvent,
    applyEngine: TrpgImmersiveExternalModule.applyEngine,
    stop: TrpgImmersiveExternalModule.stopAll,
    state: TrpgImmersiveExternalModule.state,
};
