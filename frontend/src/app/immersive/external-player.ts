/**
 * 外链 BGM 播放器（P3）。
 *
 * 关键约束（比「能播」更重要）：
 *
 * 1. **底床可让位，但必须交还**：外链生效期间程序化底床（引擎总增益）被淡到静音，
 *    外链是叠加层而不是替代品；加载失败时若有旧曲仍在就继续放旧曲，否则由
 *    `settleEngine` 把引擎交还（有谱面 → `procedural` 重建底床，无谱面 → `off`），
 *    绝不允许「引擎停在 external 但什么都不响」；
 * 2. **等功率交叉淡化**：底床用 cos 曲线下降、外链用 sin 曲线上升，功率之和恒定；
 * 3. **切换点对齐小节/乐句边界**：从 `TrpgImmersiveMusic.barTiming()` 取当前小节
 *    时基，优先对齐到最近的乐句边界（最多等 4 小节），否则等下一条小节线；
 * 4. **不阻塞主线程**：所有网络等待都是 `canplay` 事件 + 超时，失败仅降级日志；
 * 5. **不硬切**：引擎 `off` / 切回程序化 / 换曲都用淡化，暂停动作放在淡化之后。
 *
 * 音频图：`<audio> → elementGain → musicMuteGain → 限幅/EQ/主输出`。
 * 外链刻意绕开 `engineGain`：external 档会把引擎总增益淡到 0 来关掉程序化配乐，
 * 挂在那里会被一起静音。两路都受「配乐静音」与语音 ducking 控制。
 */
namespace TrpgImmersiveExternalModule {
    const LOAD_TIMEOUT_MS = 7000;
    const LOOP_FADE_SECONDS = 1.8;
    const LOOP_MIN_DURATION = 12;
    const DEFAULT_FADE_MS = 4200;

    type ExternalMode = "idle" | "loading" | "playing" | "paused";

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
    /** 当前可用曲目队列（服务端选中的曲目 + alternatives），用于统一播放器切歌。 */
    let queue: ImmersiveExternalTrack[] = [];
    let queueIndex = 0;
    /** 用户主动暂停：与「切歌/降级导致的停止」区分，恢复时从当前播放位置继续。 */
    let pausedByUser = false;
    /** 最近一次外链事件；本地切歌时复用它携带的淡入时长与乐句参数。 */
    let lastPayload: ImmersiveExternalEvent | null = null;
    /** 每次换曲/停止都递增；异步回调据此丢弃过期结果。 */
    let generation = 0;
    let pendingTimers: number[] = [];
    let lastReason = "";
    let lastError = "";
    let lastBedLevel = 1;
    let reporter: ((event: Record<string, unknown>) => void) | null = null;
    let lastFailureKey = "";
    let lastFailureAt = 0;

    function reportPlayback(event: string, track: ImmersiveExternalTrack | null, reason = "", extra: Record<string, unknown> = {}): void {
        if (!reporter || !track) return;
        if (event !== "crossfade_done") {
            const key = `${track.track_key}|${reason}`;
            const now = Date.now();
            if (key === lastFailureKey && now - lastFailureAt < 60000) return;
            lastFailureKey = key;
            lastFailureAt = now;
        }
        try {
            reporter({
                event,
                track_key: track.track_key,
                title: track.title,
                artist: track.artist,
                reason,
                ...extra,
            });
        } catch {
            // 上报失败不影响播放。
        }
    }

    export function setReporter(next: ((event: Record<string, unknown>) => void) | null): void {
        reporter = next;
    }

    function clamp(value: number, minimum: number, maximum: number): number {
        if (!Number.isFinite(value)) return minimum;
        return Math.max(minimum, Math.min(maximum, value));
    }

    /** 服务端只保证「当前曲目」，alternatives 是本地的切歌池；按 track_key 去重。 */
    function buildQueue(payload: ImmersiveExternalEvent): ImmersiveExternalTrack[] {
        const list: ImmersiveExternalTrack[] = [];
        const seen = new Set<string>();
        const candidates = [payload.track, ...(payload.alternatives || [])];
        for (const item of candidates) {
            if (!item || !item.track_key || !item.stream_path || seen.has(item.track_key)) continue;
            seen.add(item.track_key);
            list.push(item);
        }
        return list;
    }

    function queueIndexOf(trackKey: string): number {
        if (!trackKey) return -1;
        return queue.findIndex((item) => item.track_key === trackKey);
    }

    function currentElement(): HTMLAudioElement | null {
        const index = playingIndex >= 0 ? playingIndex : activeIndex;
        return elements?.[index] ?? null;
    }

    function trackDuration(track: ImmersiveExternalTrack | null, element: HTMLAudioElement | null): number {
        const fromElement = Number(element?.duration);
        if (Number.isFinite(fromElement) && fromElement > 0) return fromElement;
        const fromTrack = Number(track?.duration_ms);
        return Number.isFinite(fromTrack) && fromTrack > 0 ? fromTrack / 1000 : 0;
    }

    /**
     * 自然播完（循环调度未生效时的兜底）。
     * 没有自动续播，所以队列到这里就作废：清空后交给 `settleEngine`，
     * 否则引擎会永久停在 external、总线已淡到 0，变成「显示在播但没声音」。
     */
    function handleEnded(index: number): void {
        if (playingIndex !== index) return;
        lastReason = "track_ended";
        mode = "idle";
        playingIndex = -1;
        playingTrack = null;
        currentTrack = null;
        queue = [];
        queueIndex = 0;
        setAttribution(null);
        settleEngine(1200);
        notifyState();
    }

    function clearTimers(): void {
        for (const timer of pendingTimers) window.clearTimeout(timer);
        pendingTimers = [];
    }

    function later(handler: () => void, delayMs: number): void {
        pendingTimers.push(window.setTimeout(handler, Math.max(0, delayMs)));
    }

    /** 通知统一播放器刷新外链叠加层（曲名/序号），不引入第二套状态。 */
    function notifyState(): void {
        try {
            document.dispatchEvent(new CustomEvent("trpg:external-state"));
        } catch {
            // 忽略事件派发失败。
        }
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

    /**
     * 外链结束/失败时是否把程序化底床抬回来。
     * 外链块生效期间程序化配乐已关闭（服务端 `fallback=off` 是既定策略），
     * 所以 external 模式下保持静音本身是对的；收尾不在这里做，而是先由
     * `settleEngine` 把引擎切走，再让底床抬回。
     */
    function restoreBed(seconds: number): void {
        if (window.TrpgImmersiveMusic?.currentEngineMode?.() === "external") return;
        rampBed(1, seconds, true);
    }

    /**
     * 外链退到 idle 后引擎该归谁。判定表固定，便于状态机测试直接核对：
     * - 引擎本来就不在 external：不动；
     * - 队列里还有备选曲：保留 external，播放器仍可继续切歌；
     * - 队列空了：有可用谱面回 `procedural`，否则显式 `off`——绝不停在 external。
     */
    export function nextEngineForIdle(
        engine: string,
        hasPlan: boolean,
        hasQueue: boolean,
    ): "keep" | "procedural" | "off" {
        if (engine !== "external") return "keep";
        if (hasQueue) return "keep";
        return hasPlan ? "procedural" : "off";
    }

    /** 按判定表把引擎从 external 交还，并让底床跟着回来。 */
    function settleEngine(fadeMs = 1400): void {
        const api = window.TrpgImmersiveMusic;
        const decision = nextEngineForIdle(
            api?.currentEngineMode?.() || "",
            Boolean(api?.currentPlan?.()),
            queue.length > 0,
        );
        if (decision === "keep") return;
        api?.setEngineMode?.(decision, fadeMs);
        // 引擎已离开 external，restoreBed 的守卫不再拦截，底床这时才抬得回来。
        restoreBed(Math.max(0.6, fadeMs / 1000));
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
     * - 如果没有，清空署名，并把这一首从切歌池剔除；
     * - 队列随之清空时交给 `settleEngine` 收尾（procedural / off），
     *   还剩备选曲时保留 external，播放器可以继续切歌；
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
            // 旧曲仍在：恢复原有状态（用户暂停时保持暂停），并续上循环调度。
            mode = pausedByUser ? "paused" : "playing";
            currentTrack = playingTrack;
            setAttribution(playingTrack, pausedByUser ? "已暂停" : "播放中");
            notifyState();
            if (pausedByUser) {
                return;
            }
            // 暂停加载期间可能已经把旧元素停了：恢复「播放中」时必须重新 play。
            void graph?.elements[keep]?.play().catch(() => undefined);
            scheduleLoop(token);
            return;
        }
        const failedKey = currentTrack?.track_key || "";
        mode = "idle";
        currentTrack = null;
        playingTrack = null;
        setAttribution(null);
        // 接不上的这一首留在队列里只会让引擎被判定为「还有曲目」而拒绝交还。
        if (failedKey) queue = queue.filter((item) => item.track_key !== failedKey);
        queueIndex = Math.min(queueIndex, Math.max(0, queue.length - 1));
        settleEngine(900);
        notifyState();
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
        restoreBed(seconds);
        const token = generation;
        mode = "idle";
        playingIndex = -1;
        playingTrack = null;
        currentTrack = null;
        lastReason = "stopped";
        pausedByUser = false;
        queue = [];
        queueIndex = 0;
        lastPayload = null;
        setAttribution(null);
        notifyState();
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
            reportPlayback("load_failed", next, "no_audio_graph");
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
            reportPlayback("load_failed", next, "no_audio_element");
            degrade("no_audio_element");
            return;
        }
        mode = "loading";
        currentTrack = next;
        queueIndex = Math.max(0, queueIndexOf(next.track_key));
        lastReason = "";
        lastError = "";
        setAttribution(next, "接入中");
        notifyState();

        element.pause();
        element.src = next.stream_path;
        element.load();
        const ready = await waitReady(element, LOAD_TIMEOUT_MS);
        if (token !== generation) return;
        if (!ready) {
            reportPlayback("load_failed", next, lastError || "load_timeout");
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
        // 用户已暂停时只准备好曲目（预加载完成、停在 0 秒），恢复时直接播放。
        if (pausedByUser) {
            element.pause();
            element.onended = () => handleEnded(index);
            activeIndex = index;
            playingIndex = index;
            playingTrack = track;
            currentTrack = track;
            mode = "paused";
            setAttribution(track, "已暂停");
            return;
        }
        try {
            await element.play();
        } catch {
            lastError = "play_rejected";
            reportPlayback("play_failed", track, "play_rejected");
            degrade("play_rejected");
            return;
        }
        if (token !== generation || !gains || !sourceCtx) {
            element.pause();
            return;
        }
        const previousIndex = playingIndex >= 0 && playingIndex !== index ? playingIndex : -1;
        // 外链块生效时程序化配乐必须完全关闭（不再保留底床），环境音独立播放。
        const bedLevel = 0.0001;
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
        // 用「当前出声下标」而非 token 判断，暂停/恢复导致 generation 变化后依然有效。
        element.onended = () => handleEnded(index);
        activeIndex = index;
        playingIndex = index;
        playingTrack = track;
        mode = "playing";
        setAttribution(track, "播放中");
        scheduleLoop(token);
        later(() => {
            if (token !== generation) return;
            reportPlayback("crossfade_done", track, "", { seconds: fade });
        }, fade * 1000 + 100);
    }

    export function applyEvent(payload: ImmersiveExternalEvent | null | undefined): void {
        if (!payload) return;
        if (payload.status === "disabled") {
            // 外链块被关掉：把引擎交还给程序化/关闭，并停掉外链（stop 的淡出不受影响）。
            if (payload.engine === "procedural" || payload.engine === "off") {
                applyEngine({ room_id: payload.room_id || "", engine: payload.engine, fade_ms: 1200 });
            } else {
                stop(1200);
            }
            return;
        }
        if (payload.status === "selected" && payload.track && payload.engine === "external") {
            // 服务端没有独立的 engine 广播，外链事件本身就是交接信号：
            // 先把引擎切到 external（程序化总线淡出并停止），否则下面的守卫会拒绝开始。
            if (window.TrpgImmersiveMusic?.currentEngineMode?.() !== "external") {
                applyEngine({
                    room_id: payload.room_id || "",
                    engine: "external",
                    fade_ms: payload.fade_ms || payload.crossfade_ms || DEFAULT_FADE_MS,
                });
            }
        }
        if (payload.status !== "selected" || !payload.track) {
            lastReason = payload.reason || payload.status;
            // 已在播放的曲子继续播；空结果/降级不打断当前氛围。
            if (mode !== "playing" && mode !== "paused") setAttribution(null);
            return;
        }
        const engine = window.TrpgImmersiveMusic?.currentEngineMode?.();
        if (engine === "procedural" || engine === "off") return;
        // 队列来自「当前曲目 + alternatives」，本地可据此实现上一首/下一首。
        queue = buildQueue(payload);
        queueIndex = Math.max(0, queueIndexOf(payload.track.track_key));
        lastPayload = payload;
        if (currentTrack && currentTrack.track_key === payload.track.track_key
            && (mode === "playing" || mode === "paused" || mode === "loading")) {
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

    /** 统一播放器读取的播放态；没有曲目或已停止时 active=false。 */
    export function getPlaybackState(): ImmersiveExternalPlaybackState {
        const track = playingTrack || currentTrack;
        const element = currentElement();
        const index = track ? queueIndexOf(track.track_key) : -1;
        return {
            active: Boolean(track) && mode !== "idle",
            playing: mode === "playing",
            paused: mode === "paused" || (pausedByUser && mode === "loading"),
            title: track?.title || "",
            artist: track?.artist || "",
            index: index >= 0 ? index : Math.max(0, queueIndex),
            count: queue.length,
            position: element && Number.isFinite(element.currentTime) ? element.currentTime : 0,
            duration: trackDuration(track, element),
            trackKey: track?.track_key || "",
        };
    }

    export function hasQueue(): boolean {
        return queue.length > 0 && mode !== "idle";
    }

    /**
     * 暂停：记下暂停标记、停掉循环调度，并让增益淡出后暂停元素。
     * 不重置 currentTime，恢复时从原位置继续；引擎处于 external 时底床本来就已关闭。
     */
    export function pause(): void {
        if (pausedByUser || mode === "idle") return;
        pausedByUser = true;
        // 加载中不要清定时器/递增 generation：让 startPlayback 正常跑完后停在暂停态，
        // 否则加载流程会被取消，播放器永久卡在 loading。
        if (mode === "loading") {
            // 旧曲可能仍在交叉淡出：把它一起压下去，否则「暂停」看起来没生效。
            const oldIndex = playingIndex;
            const oldElement = oldIndex >= 0 ? elements?.[oldIndex] ?? null : null;
            const oldGain = oldIndex >= 0 ? gains?.[oldIndex] ?? null : null;
            if (oldElement && oldGain && sourceCtx) {
                rampGain(oldGain, 0.0001, 0.35, false, sourceCtx);
                later(() => {
                    try {
                        oldElement.pause();
                    } catch {
                        // 忽略已销毁元素。
                    }
                }, 420);
            }
            return;
        }
        clearTimers();
        generation += 1;
        const token = generation;
        if (mode !== "playing") return;
        mode = "paused";
        setAttribution(playingTrack || currentTrack, "已暂停");
        notifyState();
        const graph = elements && gains && sourceCtx ? { elements, gains, ctx: sourceCtx } : null;
        if (graph) {
            for (const gain of graph.gains) rampGain(gain, 0.0001, 0.45, false, graph.ctx);
        }
        later(() => {
            if (generation !== token) return;
            for (const element of elements ?? []) {
                try {
                    element.pause();
                } catch {
                    // 忽略已销毁元素。
                }
            }
        }, 540);
    }

    /** 恢复：从暂停位置继续播放当前元素，并重新挂上循环调度。 */
    export function resume(): void {
        if (!pausedByUser) return;
        pausedByUser = false;
        // 加载中恢复：不要在这里重播旧曲，让 startPlayback 就绪后直接开始新曲。
        if (mode === "loading") {
            setAttribution(currentTrack, "接入中");
            return;
        }
        clearTimers();
        const element = currentElement();
        const track = playingTrack || currentTrack;
        if (!element || !track || !gains || !sourceCtx) {
            mode = "idle";
            return;
        }
        try {
            window.TrpgImmersiveMusic?.unlock?.();
        } catch {
            // 解锁失败时 play() 会给出明确结果。
        }
        const token = generation + 1;
        generation = token;
        const index = playingIndex >= 0 ? playingIndex : activeIndex;
        void element.play().then(() => {
            if (token !== generation || pausedByUser) return;
            mode = "playing";
            const gain = gains?.[index];
            if (gain && sourceCtx) rampGain(gain, 1, 0.5, true, sourceCtx);
            setAttribution(track, "播放中");
            notifyState();
            scheduleLoop(token);
        }).catch(() => {
            if (token !== generation) return;
            lastError = "play_rejected";
            pausedByUser = true;
            mode = "paused";
            setAttribution(track, "已暂停");
            notifyState();
        });
    }

    /** 与统一播放器的“播放”语义对齐：外部只需恢复。 */
    export function play(): void {
        resume();
    }

    export function toggle(): void {
        if (pausedByUser) resume();
        else pause();
    }

    function step(direction: number): void {
        if (queue.length < 2) return;
        const index = (queueIndex + (direction >= 0 ? 1 : -1) + queue.length) % queue.length;
        const track = queue[index];
        if (!track) return;
        queueIndex = index;
        const payload: ImmersiveExternalEvent = lastPayload
            ? { ...lastPayload, track, fade_ms: 1200, crossfade_ms: 1200 }
            : {
                ok: true,
                status: "selected",
                engine: "external",
                fallback: "off",
                track,
                fade_ms: 1200,
                crossfade_ms: 1200,
                phrase_bars: 8,
            };
        lastPayload = payload;
        void beginTrack(track, payload);
    }

    export function next(): void {
        step(1);
    }

    export function prev(): void {
        step(-1);
    }

    /** 拖动进度：外链有真实音轨，可以直接 seek；循环定时器要按新位置重排。 */
    export function seek(seconds: number): void {
        const element = currentElement();
        const track = playingTrack || currentTrack;
        if (!element || !track) return;
        const duration = trackDuration(track, element);
        if (!(duration > 0)) return;
        const target = clamp(Number(seconds) || 0, 0, Math.max(0, duration - 0.25));
        try {
            element.currentTime = target;
        } catch {
            return;
        }
        clearTimers();
        if (mode === "playing") {
            generation += 1;
            scheduleLoop(generation);
        }
    }

    export function state(): Record<string, unknown> {
        const playback = getPlaybackState();
        return {
            mode,
            paused: playback.paused,
            reason: lastReason,
            error: lastError,
            track_key: playback.trackKey,
            title: playback.title,
            artist: playback.artist,
            index: playback.index,
            count: playback.count,
            position: playback.position,
            duration: playback.duration,
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
    setReporter: TrpgImmersiveExternalModule.setReporter,
    getPlaybackState: TrpgImmersiveExternalModule.getPlaybackState,
    hasQueue: TrpgImmersiveExternalModule.hasQueue,
    play: TrpgImmersiveExternalModule.play,
    pause: TrpgImmersiveExternalModule.pause,
    resume: TrpgImmersiveExternalModule.resume,
    toggle: TrpgImmersiveExternalModule.toggle,
    next: TrpgImmersiveExternalModule.next,
    prev: TrpgImmersiveExternalModule.prev,
    seek: TrpgImmersiveExternalModule.seek,
};
