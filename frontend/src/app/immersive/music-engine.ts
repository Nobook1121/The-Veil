/**
 * 实时程序化配乐引擎（Web Audio）。
 *
 * 职责边界：
 * - `music-theory.ts` 提供调式、和弦色彩、动机库与八档编配配方；
 * - `music-arranger.ts` 把 plan（含导演乐谱）编排成逐小节 cue；
 * - `music-voices.ts` 负责具体声部的合成与音色；
 * - 本文件只负责「总线、调度、自动化、交叉淡入、降级与静音」。
 *
 * 保留的既有契约：
 * - 同一种子 + 同一段落 → 同一份谱面（可复现）；
 * - 切歌使用 A/B 总线交叉淡入，旧总线立即停止补发并淡出；
 * - 静音状态持久化，首次交互前排队，解锁后补播；
 * - 播放时按房间开关压低 BGM；
 * - 房间级同步：按服务端下发的共享位置（小节序号）对齐，换谱/换 tempo 都在
 *   同一小节切入，新加入的玩家接着当前进度听，而不是从 cue 开头重播；
 * - 移动端 / 低性能设备自动降到更轻的合成档位；不阻塞叙事线程（全部异步调度）。
 *
 * 相对旧实现的核心升级：
 * - 不再是「每小节换一个和弦 + 固定 pad」，而是完整 cue：铺垫→上升→高潮→抽空→余韵；
 * - 每小节有独立的动态目标与滤波/混响自动化，留白（silence）会成为真正的重音；
 * - 动机按段落轮换、逆行、倒影、和弦化；ostinato 分层且以卡农错位进入；
 * - 弦乐/铜管/管风琴/合唱/定音鼓/钟声各自拥有独立的进入时机与音色，而不是同一套三角波。
 */
namespace TrpgImmersiveMusicModule {
    const MUTE_KEY = "trpg_immersive_music_muted";
    const SCHEDULE_AHEAD_SECONDS = 2.2;
    const SCHEDULER_INTERVAL_MS = 220;
    /** 本地进度与服务端共享位置偏差超过这么多小节才重新对齐，避免频繁重建。 */
    const SYNC_DRIFT_LIMIT_BARS = 2;

    const INTENSITY_SPECS: Record<ImmersiveIntensity, { gain: number; tempo: number; density: number }> = {
        low: { gain: 0.82, tempo: 0.92, density: 0.72 },
        medium: { gain: 1, tempo: 1, density: 1 },
        high: { gain: 1.14, tempo: 1.07, density: 1.18 },
    };

    /** 不同声部的基准增益；最终响度由动态自动化与站点音量共同决定。 */
    const VOICE_GAIN: Record<string, number> = {
        pad: 0.072,
        strings: 0.086,
        organ: 0.052,
        choir: 0.066,
        brass: 0.058,
        bass: 0.15,
        pluck: 0.052,
        bell: 0.05,
    };

    let context: AudioContext | null = null;
    let master: GainNode | null = null;
    let masterReverb: ConvolverNode | null = null;
    let masterReverbFar: ConvolverNode | null = null;
    let masterFarInput: DelayNode | null = null;
    let masterDelay: DelayNode | null = null;
    let noiseBuffer: AudioBuffer | null = null;
    let qualityTier = 0;
    let schedulerTimer: number | null = null;
    let liveBuses: MusicBus[] = [];
    let activePlan: ImmersiveMusicPlan | null = null;
    let activeSignature = "";
    let queuedPlan: ImmersiveMusicPlan | null = null;
    /** 最近一次服务端下发的房间共享位置；换谱/换 tempo 时据此对齐。 */
    let syncAnchor: SyncAnchor | null = null;
    let muted = readMuted();
    let gestureArmed = false;
    // 语音配音 ducking：0 表示不压低，数字越大音乐越轻。语音总线独立，不接入音乐图。
    let voiceDuckIntensity = 0;

    function readMuted(): boolean {
        try {
            return window.localStorage.getItem(MUTE_KEY) === "true";
        } catch {
            return false;
        }
    }

    function persistMuted(value: boolean): void {
        try {
            window.localStorage.setItem(MUTE_KEY, value ? "true" : "false");
        } catch {
            // 浏览器禁用存储时静音状态只在本次会话生效。
        }
    }

    function clamp(value: number, minimum: number, maximum: number): number {
        if (!Number.isFinite(value)) return minimum;
        return Math.max(minimum, Math.min(maximum, value));
    }

    /** 轻饱和曲线：只给峰值加一点偶次谐波，避免纯数学波形「塑料感」。 */
    function saturationCurve(): Float32Array<ArrayBuffer> {
        const size = 1024;
        const curve = new Float32Array(new ArrayBuffer(size * 4));
        for (let index = 0; index < size; index += 1) {
            const x = (index / (size - 1)) * 2 - 1;
            curve[index] = Math.tanh(x * 1.25);
        }
        return curve;
    }

    function detectQuality(): number {
        try {
            const cores = Number((navigator as unknown as { hardwareConcurrency?: number }).hardwareConcurrency || 8);
            const memory = Number((navigator as unknown as { deviceMemory?: number }).deviceMemory || 8);
            const mobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)
                || (window.matchMedia && window.matchMedia("(max-width: 767.98px)").matches);
            if (cores <= 2 || memory <= 2) return 2;
            if (mobile || cores <= 4 || memory <= 4) return 1;
            return 0;
        } catch {
            return 1;
        }
    }

    function normalizePlan(plan: ImmersiveMusicPlan): ImmersiveMusicPlan {
        const profile = (TrpgImmersiveMusicTheory.RECIPES[plan.profile] ? plan.profile : "daily") as ImmersiveProfile;
        const intensity = (plan.intensity && INTENSITY_SPECS[plan.intensity] ? plan.intensity : "medium") as ImmersiveIntensity;
        return {
            ...plan,
            profile,
            intensity,
            section: Number.isFinite(plan.section) ? Math.max(0, plan.section) : 0,
            seed: String(plan.seed || `${plan.room_id || "room"}:0`),
            volume: clamp(Number(plan.volume ?? 0.35), 0, 1),
            crossfade_ms: clamp(Number(plan.crossfade_ms ?? 3000), 300, 10000),
            motif: plan.motif || `${profile}-1`,
        };
    }

    /** 基础电平：站点音量 × 强度增益；段落内部的起伏由总线动态自动化完成。 */
    function levelFor(plan: ImmersiveMusicPlan): number {
        const volume = clamp(Number(plan.volume ?? 0.35), 0, 1);
        const intensity = INTENSITY_SPECS[plan.intensity] || INTENSITY_SPECS.medium;
        return clamp(volume * intensity.gain, 0.02, 1);
    }

    /**
     * 总线「内容签名」：只有真正会改变谱面的字段才触发换总线。
     * 强度、音量、导演缓存段号都不在其中——它们只改音量/推进位置，
     * 否则用户每回合听到的都是「从现在最响的地方从头再放一次」。
     */
    function scoreFingerprint(score: ImmersiveScore | null | undefined): string {
        if (!score) return "";
        return [
            score.mode || "",
            (score.instrumentation || []).join(","),
            (score.chords || [])
                .map((chord) => `${chord.root}:${(chord.intervals || []).join(",")}`)
                .join(";"),
            (score.motif_variants || [])
                .map((variant) => `${variant.role || ""}:${(variant.degrees || []).join(",")}:${(variant.rhythm || []).join(",")}`)
                .join(";"),
            (score.sections || [])
                .map((section) => `${section.bars || 0}:${section.dynamic || ""}`)
                .join(";"),
            (score.dynamics || []).join(","),
            score.pedal === null || score.pedal === undefined ? "" : String(score.pedal),
        ].join("|");
    }

    /** 去掉随段落变化的 `:N` 后缀；段落推进由乐谱指纹表达，不用重复触发换歌。 */
    function baseSeed(plan: ImmersiveMusicPlan): string {
        return String(plan.seed || "").replace(/:\d+$/, "");
    }

    function contentSignature(plan: ImmersiveMusicPlan): string {
        return [
            plan.profile,
            baseSeed(plan),
            plan.director?.fingerprint || "",
            scoreFingerprint(plan.score),
        ].join("|");
    }

    /**
     * 房间共享位置锚点：服务端只说「此刻在第几小节」，本地用单调时钟往前推。
     * 不用绝对时间戳，避免各玩家系统时钟偏差把音乐推歪。
     */
    interface SyncAnchor {
        epoch: number;
        positionBars: number;
        atMs: number;
        barSeconds: number;
    }

    function nowMs(): number {
        return typeof performance !== "undefined" && typeof performance.now === "function"
            ? performance.now()
            : Date.now();
    }

    function anchorFromPlan(plan: ImmersiveMusicPlan): SyncAnchor | null {
        const sync = plan.sync;
        if (!sync) return null;
        const position = Number(sync.position_bars);
        if (!Number.isFinite(position)) return null;
        const barSeconds = Number(sync.bar_seconds);
        return {
            epoch: Number.isFinite(Number(sync.epoch)) ? Number(sync.epoch) : 0,
            positionBars: Math.max(0, position),
            atMs: nowMs(),
            barSeconds: Number.isFinite(barSeconds) && barSeconds > 0 ? barSeconds : 0,
        };
    }

    function anchorPositionBars(anchor: SyncAnchor | null, barDuration: number): number | null {
        if (!anchor) return null;
        const seconds = anchor.barSeconds > 0 ? anchor.barSeconds : barDuration;
        const elapsed = Math.max(0, (nowMs() - anchor.atMs) / 1000);
        return anchor.positionBars + (seconds > 0 ? elapsed / seconds : 0);
    }

    /** 换谱时的起始位置：优先服务端共享位置，其次沿用旧总线的本地进度。 */
    function startPositionBars(
        live: MusicBus | null,
        anchor: SyncAnchor | null,
    ): number | null {
        const duration = live?.barDuration() ?? 0;
        const shared = anchorPositionBars(anchor, duration);
        if (shared !== null) return shared;
        return live ? live.positionBars() : null;
    }

    function createNoiseBuffer(ctx: AudioContext, seconds: number): AudioBuffer {
        const length = Math.max(1, Math.floor(ctx.sampleRate * seconds));
        const buffer = ctx.createBuffer(1, length, ctx.sampleRate);
        const data = buffer.getChannelData(0);
        for (let index = 0; index < length; index += 1) {
            data[index] = Math.random() * 2 - 1;
        }
        return buffer;
    }

    /** 带早期反射的立体声脉冲响应：让弦乐/钟声有「厅堂感」，而不是直接在干声上叠静音。 */
    function createReverbBuffer(ctx: AudioContext, seconds: number): AudioBuffer {
        const length = Math.max(1, Math.floor(ctx.sampleRate * seconds));
        const buffer = ctx.createBuffer(2, length, ctx.sampleRate);
        const early = [0.018, 0.031, 0.047, 0.066, 0.089];
        for (let channel = 0; channel < 2; channel += 1) {
            const data = buffer.getChannelData(channel);
            for (let index = 0; index < length; index += 1) {
                const time = index / ctx.sampleRate;
                const decay = Math.pow(1 - index / length, 2.1);
                let value = (Math.random() * 2 - 1) * decay * 0.9;
                early.forEach((offset, earlyIndex) => {
                    if (Math.abs(time - offset - channel * 0.003) < 0.004) {
                        value += (1 - earlyIndex / early.length) * 0.4;
                    }
                });
                data[index] = value;
            }
        }
        return buffer;
    }

    function ensureContext(): AudioContext | null {
        if (context) return context;
        const ctor = window.AudioContext
            || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!ctor) return null;
        try {
            context = new ctor();
            qualityTier = detectQuality();
            master = context.createGain();
            master.gain.value = masterTargetGain();

            // 安全限幅：多声部叠加时优先保护听力，避免高潮段削波。
            const limiter = context.createDynamicsCompressor();
            limiter.threshold.value = -12;
            limiter.knee.value = 6;
            limiter.ratio.value = 5;
            limiter.attack.value = 0.006;
            limiter.release.value = 0.24;

            // 总线低频收敛 + 高频空气感；饱和只做很轻的 tanh，避免数字方波感。
            const eqLow = context.createBiquadFilter();
            eqLow.type = "lowshelf";
            eqLow.frequency.value = 70;
            eqLow.gain.value = -2.5;
            const eqHigh = context.createBiquadFilter();
            eqHigh.type = "highshelf";
            eqHigh.frequency.value = 4200;
            eqHigh.gain.value = 1.5;
            const shaper = context.createWaveShaper();
            shaper.curve = saturationCurve();
            shaper.oversample = "2x";
            limiter.connect(eqLow);
            eqLow.connect(eqHigh);
            eqHigh.connect(shaper);
            shaper.connect(master);
            master.connect(context.destination);

            // 双混响：近场给打击乐与主旋律「击点」，远场加 pre-delay 做空间纵深。
            masterReverb = context.createConvolver();
            masterReverb.buffer = createReverbBuffer(context, qualityTier >= 2 ? 1.0 : 1.7);
            const reverbGain = context.createGain();
            reverbGain.gain.value = 0.42;
            masterReverb.connect(reverbGain);
            reverbGain.connect(limiter);

            masterReverbFar = context.createConvolver();
            masterReverbFar.buffer = createReverbBuffer(context, qualityTier >= 2 ? 1.8 : 3.2);
            masterFarInput = context.createDelay(0.2);
            masterFarInput.delayTime.value = qualityTier >= 2 ? 0.02 : 0.045;
            const farGain = context.createGain();
            farGain.gain.value = 0.34;
            masterFarInput.connect(masterReverbFar);
            masterReverbFar.connect(farGain);
            farGain.connect(limiter);

            // 短延迟用做「回声 cue」，只在关键动机上发送，避免整首都在浴室里。
            masterDelay = context.createDelay(1.2);
            masterDelay.delayTime.value = 0.34;
            const feedback = context.createGain();
            feedback.gain.value = 0.28;
            const damping = context.createBiquadFilter();
            damping.type = "lowpass";
            damping.frequency.value = 2400;
            masterDelay.connect(damping);
            damping.connect(feedback);
            feedback.connect(masterDelay);
            const delayOut = context.createGain();
            delayOut.gain.value = 0.35;
            masterDelay.connect(delayOut);
            delayOut.connect(limiter);

            noiseBuffer = createNoiseBuffer(context, 2);
            return context;
        } catch (error) {
            console.warn("程序化配乐初始化失败", error);
            context = null;
            return null;
        }
    }

    class MusicBus {
        private readonly ctx: AudioContext;
        private readonly plan: ImmersiveMusicPlan;
        private readonly spec: TrpgImmersiveMusicArranger.CompositionSpec;
        private readonly masterNode: GainNode;
        private readonly convolverNear: ConvolverNode;
        private readonly convolverFar: ConvolverNode;
        private readonly farInput: DelayNode;
        private readonly delay: DelayNode;
        private readonly noise: AudioBuffer;
        private readonly rack: TrpgImmersiveMusicVoices.Rack;

        readonly input: GainNode;
        readonly filter: BiquadFilterNode;
        readonly dynamicGain: GainNode;
        readonly gain: GainNode;
        private readonly sendNear: GainNode;
        private readonly sendFar: GainNode;
        private readonly delaySend: GainNode;
        private readonly droneGain: GainNode;
        private readonly droneNodes: OscillatorNode[] = [];
        private droneLfo: OscillatorNode | null = null;

        private timeline: TrpgImmersiveMusicArranger.Timeline;
        private cycle = 0;
        private barIndex = 0;
        private nextBarTime = 0;
        private currentLevel = 0;
        private baseLevel: number;
        private disposed = false;
        private expired = false;
        private disposeTimer: number | null = null;
        /** 服务端共享 epoch：同 epoch 表示同一段播放，换 epoch 才需要重新对齐。 */
        private readonly syncEpoch: number;
        /** 从共享位置切入时，到下一整小节边界的等待秒数（0 表示正落在边界）。 */
        private bridgeSeconds = 0;
        /** 等待期间用哪一小节的余料做过渡，避免新玩家进来先听到静音。 */
        private bridgeBarIndex = 0;

        constructor(
            ctx: AudioContext,
            plan: ImmersiveMusicPlan,
            spec: TrpgImmersiveMusicArranger.CompositionSpec,
            masterNode: GainNode,
            convolverNear: ConvolverNode,
            convolverFar: ConvolverNode,
            farInput: DelayNode,
            delay: DelayNode,
            noise: AudioBuffer,
            startKind: TrpgImmersiveMusicTheory.SectionKind | null = null,
            startPositionBars: number | null = null,
        ) {
            this.ctx = ctx;
            this.plan = plan;
            this.spec = spec;
            this.masterNode = masterNode;
            this.convolverNear = convolverNear;
            this.convolverFar = convolverFar;
            this.farInput = farInput;
            this.delay = delay;
            this.noise = noise;
            this.baseLevel = levelFor(plan);
            this.syncEpoch = Number.isFinite(Number(plan.sync?.epoch)) ? Number(plan.sync?.epoch) : 0;
            this.timeline = TrpgImmersiveMusicArranger.buildTimeline(spec, 0);
            if (startPositionBars !== null && startPositionBars > 0) {
                // 房间共享位置：对齐到下一整小节，并把余料作为过渡垫，
                // 避免每个客户端各自从「自己收到消息的那一刻」重新开歌。
                const ordinal = Math.max(0, Math.ceil(startPositionBars));
                this.seekToOrdinal(ordinal);
                const fraction = startPositionBars - Math.floor(startPositionBars);
                if (fraction > 0.02 && ordinal > Math.floor(startPositionBars)) {
                    this.bridgeSeconds = (1 - fraction) * this.timeline.barDuration;
                    const total = Math.max(1, this.timeline.totalBars);
                    this.bridgeBarIndex = Math.floor(startPositionBars) % total;
                }
            } else if (startKind) {
                // 旧服务端 / 无共享位置：从当前戏剧阶段切入，避免永远从第 0 小节重放。
                this.barIndex = TrpgImmersiveMusicArranger.sectionStartBar(this.timeline, startKind);
            }

            this.input = ctx.createGain();
            this.input.gain.value = 1;
            this.filter = ctx.createBiquadFilter();
            this.filter.type = "lowpass";
            this.filter.frequency.value = clamp(spec.brightness, 200, 5000);
            this.filter.Q.value = 0.7;
            this.dynamicGain = ctx.createGain();
            this.dynamicGain.gain.value = 0.0001;
            this.gain = ctx.createGain();
            this.gain.gain.value = 0;
            this.sendNear = ctx.createGain();
            this.sendNear.gain.value = spec.reverb * 0.55;
            this.sendFar = ctx.createGain();
            this.sendFar.gain.value = spec.reverb * 0.35;
            this.delaySend = ctx.createGain();
            this.delaySend.gain.value = 0.08;
            this.droneGain = ctx.createGain();
            this.droneGain.gain.value = 0.0001;

            this.input.connect(this.filter);
            this.filter.connect(this.dynamicGain);
            this.dynamicGain.connect(this.gain);
            this.gain.connect(masterNode);
            this.dynamicGain.connect(this.sendNear);
            this.sendNear.connect(convolverNear);
            this.dynamicGain.connect(this.sendFar);
            this.sendFar.connect(farInput);
            this.dynamicGain.connect(this.delaySend);
            this.delaySend.connect(delay);
            this.droneGain.connect(this.filter);

            this.rack = new TrpgImmersiveMusicVoices.Rack({
                ctx,
                noise,
                out: this.input,
                quality: qualityTier,
                random: TrpgImmersiveMusicArranger.streamRandom(
                    `${spec.seedBase}|rack|${plan.intensity}`,
                    ["noise"],
                ),
            });
        }

        start(at: number, fadeMs: number): void {
            this.currentLevel = 0;
            this.gain.gain.setValueAtTime(0.0001, at);
            this.gain.gain.linearRampToValueAtTime(Math.max(0.001, this.baseLevel), at + fadeMs / 1000);
            this.currentLevel = this.baseLevel;
            // 从共享位置切入时先补齐当前小节的余料，再在整齐的小节边界继续，
            // 这样不同客户端听到的是同一小节里的同一拍，而不是各自重新开歌。
            const begin = at + 0.02;
            this.nextBarTime = begin + this.bridgeSeconds;
            this.dynamicGain.gain.setTargetAtTime(0.35, begin, 0.5);
            this.startDrone(begin);
            if (this.bridgeSeconds > 0.05) {
                this.playBridge(begin, this.bridgeSeconds);
            }
        }

        setVolume(level: number, fadeSeconds: number): void {
            if (this.baseLevel === level) return;
            const now = this.ctx.currentTime;
            const prev = this.baseLevel;
            this.baseLevel = level;
            if (this.currentLevel > 0 && prev > 0) {
                this.currentLevel = this.currentLevel * (level / prev);
            }
            this.gain.gain.cancelScheduledValues(now);
            this.gain.gain.setValueAtTime(Math.max(0.0001, this.currentLevel), now);
            this.gain.gain.linearRampToValueAtTime(Math.max(0.0001, level), now + Math.max(0.2, fadeSeconds));
        }

        fadeOut(now: number, fadeMs: number): void {
            if (this.expired || this.disposed) return;
            this.expired = true;
            const seconds = Math.max(0.2, fadeMs / 1000);
            this.gain.gain.cancelScheduledValues(now);
            this.gain.gain.setValueAtTime(Math.max(0.0001, this.currentLevel), now);
            this.gain.gain.linearRampToValueAtTime(0.0001, now + seconds);
            this.currentLevel = 0;
            this.disposeTimer = window.setTimeout(() => this.dispose(), seconds * 1000 + 320);
        }

        get isDisposed(): boolean {
            return this.disposed;
        }

        get isExpired(): boolean {
            return this.expired;
        }

        /** 当前正在出声的段落类型（按已排程时间回推，而不是取最后一小节）。 */
        currentSectionKind(): TrpgImmersiveMusicTheory.SectionKind | null {
            const bars = this.timeline.bars;
            if (!bars.length) return null;
            const barDuration = Math.max(0.1, this.timeline.barDuration);
            const ahead = Math.max(
                1,
                Math.ceil((this.nextBarTime - this.ctx.currentTime) / barDuration),
            );
            const index = Math.max(0, Math.min(this.barIndex - ahead, bars.length - 1));
            return bars[index]?.section ?? null;
        }

        barDuration(): number {
            return this.timeline.barDuration;
        }

        tempo(): number {
            return this.spec.tempo;
        }

        /** 本条总线接下来要演奏的绝对小节序号（跨循环连续）。 */
        positionBars(): number {
            return this.cycle * this.timeline.totalBars + this.barIndex;
        }

        epoch(): number {
            return this.syncEpoch;
        }

        /** 跳到共享位置的下一整小节；若跨循环，重建对应循环的谱面变奏。 */
        private seekToOrdinal(ordinal: number): void {
            const total = Math.max(1, this.timeline.totalBars);
            let cycle = Math.floor(ordinal / total);
            let index = ordinal - cycle * total;
            if (cycle !== this.cycle) {
                this.cycle = cycle;
                this.timeline = TrpgImmersiveMusicArranger.buildTimeline(this.spec, cycle);
                const rebuiltTotal = Math.max(1, this.timeline.totalBars);
                if (rebuiltTotal !== total) {
                    cycle = Math.floor(ordinal / rebuiltTotal);
                    index = ordinal - cycle * rebuiltTotal;
                    if (cycle !== this.cycle) {
                        this.cycle = cycle;
                        this.timeline = TrpgImmersiveMusicArranger.buildTimeline(this.spec, cycle);
                    }
                }
            }
            this.barIndex = Math.max(0, Math.min(index, Math.max(0, this.timeline.bars.length - 1)));
        }

        /** 切入等待期的过渡垫：用同一位置上的和弦/低音先填满余下的小节。 */
        private playBridge(at: number, seconds: number): void {
            const bars = this.timeline.bars;
            if (!bars.length) return;
            const bar = bars[Math.max(0, Math.min(this.bridgeBarIndex, bars.length - 1))];
            if (!bar) return;
            const duration = Math.max(0.2, seconds);
            if (bar.chord) {
                const notes = bar.chord.intervals.map((interval) => (bar.chord ? bar.chord.root + interval : 0));
                this.rack.pad(notes, at, duration, bar.dynamic * 0.7, bar.cutoff);
            }
            if (bar.bass !== null) {
                this.rack.bass(bar.bass, at, duration, bar.dynamic * 0.8);
            }
        }

        scheduleUntil(until: number): void {
            if (this.disposed || this.expired) return;
            // 后台标签页被节流时不要一次性补发几十个小节，直接跳到当前时间继续。
            if (this.nextBarTime < until - this.timeline.barDuration * 3) {
                const skipped = Math.floor((until - this.nextBarTime) / this.timeline.barDuration) - 1;
                if (skipped > 0) {
                    this.barIndex += skipped;
                    this.nextBarTime += skipped * this.timeline.barDuration;
                    this.normalizeTimeline();
                }
            }
            let guard = 0;
            while (this.nextBarTime < until && guard < 8) {
                this.ensureTimeline();
                this.scheduleBar(this.nextBarTime);
                this.nextBarTime += this.timeline.barDuration;
                this.barIndex += 1;
                guard += 1;
            }
        }

        dispose(): void {
            if (this.disposed) return;
            this.disposed = true;
            if (this.disposeTimer !== null) {
                window.clearTimeout(this.disposeTimer);
                this.disposeTimer = null;
            }
            for (const node of [...this.droneNodes, this.droneLfo]) {
                if (!node) continue;
                try {
                    node.stop();
                } catch {
                    // 已停止的振荡器无需重复处理。
                }
                try {
                    node.disconnect();
                } catch {
                    // 同上。
                }
            }
            const nodes: AudioNode[] = [
                this.input,
                this.filter,
                this.dynamicGain,
                this.gain,
                this.sendNear,
                this.sendFar,
                this.delaySend,
                this.droneGain,
            ];
            for (const node of nodes) {
                try {
                    node.disconnect();
                } catch {
                    // 同上。
                }
            }
        }

        private ensureTimeline(): void {
            if (this.barIndex < this.timeline.bars.length) return;
            this.barIndex = 0;
            this.cycle += 1;
            this.timeline = TrpgImmersiveMusicArranger.buildTimeline(this.spec, this.cycle);
        }

        /** 后台节流跳过后，把超出一个循环的索引折算成新的循环，保持谱面连续。 */
        private normalizeTimeline(): void {
            let guard = 0;
            while (this.barIndex >= this.timeline.bars.length && guard < 64) {
                this.barIndex -= this.timeline.bars.length;
                this.cycle += 1;
                this.timeline = TrpgImmersiveMusicArranger.buildTimeline(this.spec, this.cycle);
                guard += 1;
            }
        }

        private startDrone(at: number): void {
            if (!this.spec.drone && !this.spec.lowPulse) return;
            const root = this.spec.pedal !== null ? this.spec.pedal : this.spec.tonic;
            const voices = this.spec.drone ? (qualityTier >= 2 ? 1 : 2) : 0;
            for (let index = 0; index < voices; index += 1) {
                const osc = this.ctx.createOscillator();
                osc.type = "sine";
                osc.frequency.value = TrpgImmersiveMusicVoices.midiToFreq(root - 12 - index * 12);
                const tone = this.ctx.createGain();
                tone.gain.value = index === 0 ? 0.5 : 0.3;
                osc.connect(tone);
                tone.connect(this.droneGain);
                osc.start(at);
                this.droneNodes.push(osc);
            }
            if (this.spec.drone && qualityTier <= 1) {
                this.droneLfo = this.ctx.createOscillator();
                this.droneLfo.type = "sine";
                this.droneLfo.frequency.value = 0.18;
                const depth = this.ctx.createGain();
                depth.gain.value = 0.012;
                this.droneLfo.connect(depth);
                depth.connect(this.droneGain.gain);
                this.droneLfo.start(at);
            }
            this.droneGain.gain.setTargetAtTime(this.droneTarget("bed"), at, 0.8);
        }

        private droneTarget(kind: TrpgImmersiveMusicTheory.SectionKind): number {
            const scale = this.spec.drone ? 1 : 0.45;
            switch (kind) {
                case "bed": return 0.06 * scale;
                case "rise": return 0.11 * scale;
                case "climax": return 0.15 * scale;
                case "void": return 0.02 * scale;
                case "tail": return 0.05 * scale;
                default: return 0.06 * scale;
            }
        }

        private scheduleBar(start: number): void {
            const bar = this.timeline.bars[this.barIndex];
            if (!bar) return;
            const barDuration = this.timeline.barDuration;
            const beat = barDuration / 4;
            const nextBar = this.timeline.bars[this.barIndex + 1];
            const closing = Boolean(nextBar && nextBar.silence);

            const target = bar.silence
                ? 0.0006
                : this.baseLevel * (0.22 + bar.dynamic * 0.85);
            this.dynamicGain.gain.setTargetAtTime(Math.max(0.0006, target), start, bar.silence ? 0.05 : 0.16);
            this.filter.frequency.setTargetAtTime(clamp(bar.cutoff, 180, 5200), start, 0.22);
            this.sendNear.gain.setTargetAtTime(clamp(bar.reverb * 0.55, 0, 1), start, 0.28);
            const farFactor = bar.section === "climax" ? 0.24 : bar.section === "void" ? 0.7 : 0.42;
            this.sendFar.gain.setTargetAtTime(clamp(bar.reverb * farFactor, 0, 1), start, 0.28);
            this.delaySend.gain.setTargetAtTime(bar.section === "climax" ? 0.05 : 0.1, start, 0.3);
            this.droneGain.gain.setTargetAtTime(this.droneTarget(bar.section), start, 0.5);

            if (bar.silence) return;
            const random = TrpgImmersiveMusicArranger.streamRandom(
                this.spec.seedBase,
                ["play", this.cycle, bar.index],
            );

            if (bar.chord) {
                const notes = bar.chord.intervals.map((interval) => bar.chord ? bar.chord.root + interval : 0);
                const duration = Math.max(beat * 0.8, barDuration * (closing ? 0.55 : 1.04));
                bar.chordVoices.forEach((voice, voiceIndex) => {
                    const pan = (voiceIndex - (bar.chordVoices.length - 1) / 2) * 0.18;
                    const gain = VOICE_GAIN[voice] || 0.06;
                    switch (voice) {
                        case "strings":
                            this.rack.strings(notes, start, duration, gain, bar.cutoff, pan);
                            break;
                        case "organ":
                            this.rack.organ(notes, start, duration, gain * 0.85);
                            break;
                        case "choir":
                            this.rack.choir(notes, start, duration, gain * 0.9);
                            break;
                        case "pad":
                        default:
                            this.rack.pad(notes, start, duration, gain, bar.cutoff, pan);
                            break;
                    }
                });
                if (bar.brass) {
                    this.rack.brass(notes, start + beat * 0.55, duration * 0.75, VOICE_GAIN.brass || 0.05, bar.cutoff);
                }
            }

            const bassNote = bar.bass;
            if (bassNote !== null) {
                if (this.spec.profile === "urgency") {
                    const pulses = bar.section === "climax" ? 4 : 2;
                    for (let index = 0; index < pulses; index += 1) {
                        this.rack.bass(bassNote, start + index * beat, beat * 0.62, (VOICE_GAIN.bass || 0.15) * 0.85);
                    }
                } else {
                    this.rack.bass(bassNote, start, barDuration * (closing ? 0.6 : 0.96), VOICE_GAIN.bass || 0.15);
                }
            }

            if (bar.ostinato) {
                this.playOstinato(bar, start, barDuration);
            }
            if (bar.motif && bar.chord) {
                this.playMotif(bar.motif, bar, start, beat);
            }
            if (bar.counter && bar.chord) {
                this.playMotif(bar.counter, bar, start, beat);
            }
            if (bar.timpani) {
                const note = (bar.pedal !== null ? bar.pedal : bar.chord ? bar.chord.root : this.spec.tonic) - 12;
                const duckBase = this.droneTarget(bar.section);
                bar.timpani.hits.forEach((hit) => {
                    const hitAt = start + hit * beat;
                    this.rack.timpani(note, hitAt, 0.11 * bar.timpani!.gain);
                    // 低频侧链：定音鼓击点短暂压住持续低音，让心跳有「落点」。
                    this.droneGain.gain.setTargetAtTime(Math.max(0.002, duckBase * 0.35), hitAt, 0.02);
                    this.droneGain.gain.setTargetAtTime(duckBase, hitAt + 0.22, 0.16);
                });
            }
            const bellNote = bar.bell;
            if (bellNote !== null) {
                this.rack.bell(bellNote, start + random() * beat * 0.5, VOICE_GAIN.bell || 0.05);
            }
            if (bar.noiseHit) {
                this.rack.noiseHit(start + beat * 2.4, 0.26, 0.045, 620 + random() * 500, "bandpass");
            }
            if (bar.swell) {
                this.rack.swell(start, barDuration * 0.9, 0.05);
            }
        }

        private playOstinato(bar: TrpgImmersiveMusicArranger.BarPlan, start: number, barDuration: number): void {
            const plan = bar.ostinato;
            if (!plan || !bar.chord) return;
            const stepDuration = barDuration / plan.steps;
            const registerOffsets = [0, 12, -12];
            for (let layer = 0; layer < plan.layers; layer += 1) {
                const register = registerOffsets[layer % registerOffsets.length] || 0;
                for (let step = 0; step < plan.steps; step += 1) {
                    if (plan.pattern[step % plan.pattern.length] !== 1) continue;
                    if (stepDuration < 0.055) continue;
                    const index = (step + layer * plan.canonOffset) % plan.offsets.length;
                    const offset = plan.offsets[index] || 0;
                    const note = bar.chord.root + offset + plan.register + register;
                    const accent = step % 4 === 0 ? 1 : 0.72;
                    this.rack.pluck(
                        note,
                        start + step * stepDuration,
                        plan.gain * accent * (layer === 0 ? 1 : 0.72),
                    );
                }
            }
        }

        private playMotif(
            motif: TrpgImmersiveMusicArranger.MotifPlan,
            bar: TrpgImmersiveMusicArranger.BarPlan,
            start: number,
            beat: number,
        ): void {
            if (!bar.chord) return;
            const available = Math.max(0.5, 4 - motif.startBeat);
            const cell = TrpgImmersiveMusicTheory.fitRhythm(motif.cell, available);
            let cursor = start + motif.startBeat * beat;
            const baseNote = bar.chord.root + motif.octave;
            cell.degrees.forEach((degree, index) => {
                const duration = (cell.rhythm[index] || 1) * beat;
                const note = baseNote + degree;
                this.renderMotifNote(motif.instrument, note, cursor, duration, motif.gain, bar.cutoff, cell.harmony);
                cursor += duration;
            });
        }

        private renderMotifNote(
            instrument: string,
            note: number,
            start: number,
            duration: number,
            gain: number,
            cutoff: number,
            harmony: TrpgImmersiveMusicTheory.MotifHarmony,
        ): void {
            const chord = harmony === "chord" ? [note, note + 7, note + 12] : harmony === "fifth" ? [note, note + 7] : [note];
            switch (instrument) {
                case "bells":
                case "bell":
                    this.rack.bell(note, start, gain * 0.55);
                    if (harmony !== "single") this.rack.bell(note + 7, start, gain * 0.3);
                    break;
                case "strings":
                    this.rack.strings(chord, start, duration * 1.3, gain * (harmony === "chord" ? 0.42 : 0.72), cutoff * 1.4);
                    break;
                case "brass":
                    this.rack.brass(chord, start, duration * 1.1, gain * 0.7, cutoff);
                    break;
                case "choir":
                    this.rack.choir(chord, start, duration * 1.1, gain * 0.7);
                    break;
                case "organ":
                    this.rack.organ(chord, start, duration, gain * 0.6);
                    break;
                case "pluck":
                default:
                    this.rack.pluck(note, start, gain * 0.8);
                    if (harmony !== "single") this.rack.pluck(note + 7, start, gain * 0.45);
                    break;
            }
        }
    }

    function switchBus(
        plan: ImmersiveMusicPlan,
        startKind: TrpgImmersiveMusicTheory.SectionKind | null = null,
        startPosition: number | null = null,
    ): void {
        const ctx = ensureContext();
        if (!ctx || !master || !masterReverb || !masterReverbFar || !masterFarInput || !masterDelay || !noiseBuffer) return;
        const spec = TrpgImmersiveMusicArranger.resolveSpec(plan);
        const bus = new MusicBus(
            ctx,
            plan,
            spec,
            master,
            masterReverb,
            masterReverbFar,
            masterFarInput,
            masterDelay,
            noiseBuffer,
            startKind,
            startPosition,
        );
        const fadeMs = clamp(Number(plan.crossfade_ms ?? 3000), 300, 10000);
        const now = ctx.currentTime;
        for (const existing of liveBuses.slice()) {
            existing.fadeOut(now, fadeMs);
        }
        liveBuses.push(bus);
        bus.start(now + 0.06, fadeMs);
        startScheduler();
        applyDuck();
    }

    function tick(): void {
        if (!context) return;
        const until = context.currentTime + SCHEDULE_AHEAD_SECONDS;
        liveBuses = liveBuses.filter((bus) => !bus.isDisposed);
        for (const bus of liveBuses) bus.scheduleUntil(until);
        if (!liveBuses.length) stopScheduler();
    }

    function startScheduler(): void {
        if (schedulerTimer !== null) return;
        schedulerTimer = window.setInterval(tick, SCHEDULER_INTERVAL_MS);
    }

    function stopScheduler(): void {
        if (schedulerTimer === null) return;
        window.clearInterval(schedulerTimer);
        schedulerTimer = null;
    }

    function armGesture(): void {
        if (gestureArmed) return;
        gestureArmed = true;
        const handler = (): void => {
            gestureArmed = false;
            document.removeEventListener("pointerdown", handler);
            document.removeEventListener("keydown", handler);
            unlock();
        };
        document.addEventListener("pointerdown", handler, { passive: true });
        document.addEventListener("keydown", handler);
    }

    function applyDuck(): void {
        const audible = Boolean(activePlan) && !muted && Boolean(context) && liveBuses.length > 0;
        const shouldDuck = audible && activePlan?.duck_bgm !== false;
        try {
            window.TrpgBgm?.setDuck?.(shouldDuck);
        } catch {
            // BGM 播放器尚未初始化时忽略。
        }
    }

    export function apply(plan: ImmersiveMusicPlan): void {
        if (!plan || !plan.profile) return;
        const normalized = normalizePlan(plan);
        activePlan = normalized;
        const signature = contentSignature(normalized);
        const anchor = anchorFromPlan(normalized);
        // 每次消息都刷新共享位置；但忽略明显过期的状态快照（例如页面加载时缓存的
        // 旧 sync），否则切换音源会把所有人倒回到几小节前。
        if (anchor) {
            const previous = syncAnchor;
            const stale = previous && (
                anchor.epoch < previous.epoch
                || (anchor.epoch === previous.epoch && anchor.positionBars < previous.positionBars - 0.5)
            );
            if (!stale) syncAnchor = anchor;
        }
        if (muted) {
            queuedPlan = normalized;
            return;
        }
        const ctx = ensureContext();
        if (!ctx) return;
        if (ctx.state === "suspended") {
            queuedPlan = normalized;
            void ctx.resume().then(() => {
                if (ctx.state === "running") {
                    const next = queuedPlan;
                    queuedPlan = null;
                    if (next) {
                        activeSignature = "";
                        const live = liveBuses[liveBuses.length - 1] ?? null;
                        const position = startPositionBars(live, syncAnchor);
                        switchBus(next, position === null ? live?.currentSectionKind() ?? null : null, position);
                    }
                }
            }).catch(() => armGesture());
            armGesture();
            return;
        }
        const live = liveBuses[liveBuses.length - 1] ?? null;
        if (activeSignature === signature && live && !live.isExpired) {
            live.setVolume(levelFor(normalized), 0.8);
            const desired = TrpgImmersiveMusicArranger.resolveSpec(normalized);
            // 强度变化只改 tempo/密度，不换内容：按同一音乐位置重排，
            // 让速度立刻生效，同时房间里所有人都停在同一小节。
            if (Math.abs(live.tempo() - desired.tempo) >= 1 || (syncAnchor && syncAnchor.epoch !== live.epoch())) {
                const position = startPositionBars(live, syncAnchor);
                switchBus(normalized, null, position);
                return;
            }
            applyDuck();
            return;
        }
        // 真正的换谱（档位/种子/段落/导演乐谱）：交叉淡入到新总线。
        // 有共享位置就精确对齐；旧服务端没有 sync 时退回「当前戏剧阶段」切入。
        const position = startPositionBars(live, syncAnchor);
        activeSignature = signature;
        switchBus(
            normalized,
            position === null ? (live?.currentSectionKind() ?? null) : null,
            position,
        );
    }

    export function stop(): void {
        activePlan = null;
        queuedPlan = null;
        activeSignature = "";
        syncAnchor = null;
        if (context) {
            const now = context.currentTime;
            for (const bus of liveBuses.slice()) bus.fadeOut(now, 900);
        }
        stopScheduler();
        applyDuck();
    }

    /**
     * 音乐来源切换到「仅 BGM」时调用：淡出后销毁 AudioContext，确保引擎完全不发声、
     * 也不再占用音频线程与调度资源。只处理总线/上下文，不触碰编曲与音色实现。
     */
    export function shutdown(): void {
        stop();
        activePlan = null;
        queuedPlan = null;
        activeSignature = "";
        liveBuses = [];
        gestureArmed = false;
        const previous = context;
        context = null;
        master = null;
        masterReverb = null;
        masterReverbFar = null;
        masterFarInput = null;
        masterDelay = null;
        noiseBuffer = null;
        if (previous) {
            // 等 900ms 淡出结束后再关闭，避免直接切断产生爆音。
            window.setTimeout(() => {
                void previous.close().catch(() => undefined);
            }, 950);
        }
    }

    export function isEnabled(): boolean {
        return Boolean(context);
    }

    export function setMuted(value: boolean): void {
        muted = Boolean(value);
        persistMuted(muted);
        if (master && context) {
            master.gain.setTargetAtTime(masterTargetGain(), context.currentTime, 0.12);
        }
        if (!muted && activePlan) {
            if (!liveBuses.length) {
                const ctx = ensureContext();
                if (ctx && ctx.state === "running") {
                    activeSignature = "";
                    switchBus(activePlan);
                } else {
                    armGesture();
                }
            }
        }
        applyDuck();
    }

    export function isMuted(): boolean {
        return muted;
    }

    /** 语音配音期间压低实时配乐；语音总线与音乐总线分离，只改主增益。 */
    export function setDucking(level: number): void {
        voiceDuckIntensity = clamp(Number(level) || 0, 0, 90);
        if (master && context) {
            master.gain.setTargetAtTime(masterTargetGain(), context.currentTime, 0.08);
        }
    }

    function masterTargetGain(): number {
        if (muted) return 0;
        return Math.max(0.1, 1 - voiceDuckIntensity / 100);
    }

    export function toggleMuted(): boolean {
        setMuted(!muted);
        return muted;
    }

    export function unlock(): boolean {
        const ctx = ensureContext();
        if (!ctx) return false;
        if (ctx.state === "suspended") {
            void ctx.resume().then(() => {
                if (ctx.state === "running" && activePlan && !liveBuses.length && !muted) {
                    activeSignature = "";
                    switchBus(activePlan, null, startPositionBars(null, syncAnchor));
                }
            }).catch(() => undefined);
        }
        if (ctx.state === "running" && activePlan && !liveBuses.length && !muted) {
            activeSignature = "";
            switchBus(activePlan, null, startPositionBars(null, syncAnchor));
        }
        applyDuck();
        return ctx.state === "running";
    }

    export function isRunning(): boolean {
        return Boolean(activePlan) && Boolean(context) && liveBuses.length > 0 && !muted;
    }

    export function currentPlan(): ImmersiveMusicPlan | null {
        return activePlan;
    }

    export function quality(): number {
        return qualityTier;
    }
}

window.TrpgImmersiveMusic = {
    apply: TrpgImmersiveMusicModule.apply,
    stop: TrpgImmersiveMusicModule.stop,
    shutdown: TrpgImmersiveMusicModule.shutdown,
    isEnabled: TrpgImmersiveMusicModule.isEnabled,
    setMuted: TrpgImmersiveMusicModule.setMuted,
    setDucking: TrpgImmersiveMusicModule.setDucking,
    isMuted: TrpgImmersiveMusicModule.isMuted,
    toggleMuted: TrpgImmersiveMusicModule.toggleMuted,
    unlock: TrpgImmersiveMusicModule.unlock,
    isRunning: TrpgImmersiveMusicModule.isRunning,
};
