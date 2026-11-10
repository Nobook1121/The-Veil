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
    /**
     * 长音跨小节延续：和弦声部只在「和弦真正变化」时重新触发，并额外多留一段尾巴，
     * 让旧声部在下一段起音时仍未归零。旧实现的每小节重触发会让长音在每小节末尾
     * 衰减到约 0.3% 再从小节线重新爬升，听感就是「每小节断一次」。
     */
    const SUSTAIN_OVERLAP_RATIO = 0.16;
    const SUSTAIN_OVERLAP_MIN = 0.3;
    const SUSTAIN_OVERLAP_MAX = 0.75;
    /**
     * 外链曲目播放时底床压到的电平（约 -8.5dB）。不压到 0：
     * 曲库加载失败/缓冲卡顿时，底床必须已经在那儿，而不是「先静音再补救」。
     */
    const BED_DUCK_LEVEL = 0.38;

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
    /** 所有音乐总线与混响的汇入点；限幅、EQ、饱和都在这之后，干声不再绕过安全链。 */
    let mixInput: GainNode | null = null;
    /** 只作用于配乐的静音增益；环境音走自己的总线，因此「配乐静音」不会连环境音一起关掉。 */
    let musicMuteGain: GainNode | null = null;
    /**
     * 底床增益：外链曲目播放时把程序化底床压到 `BED_DUCK_LEVEL`，但**永不停止**。
     * 外链退出/失败时再抬回 1，保证任何时刻都有声音托底。
     */
    let bedGain: GainNode | null = null;
    /**
     * 引擎总增益：`music_engine=off` 时整体淡出到静音（含外链轨道），
     * 但仍保留 AudioContext，避免重新开启时重建整张图。
     */
    let engineGain: GainNode | null = null;
    /** 当前生效引擎（procedural / external / off），由服务端事件驱动。 */
    let engineMode: "procedural" | "external" | "off" = "procedural";
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
    /**
     * 用户暂停（播放器按钮）：与「静音」不同——暂停会停掉调度并记住出声位置，
     * 恢复时从那个小节继续，既不会从头重放，也不会让两套总线叠在一起。
     */
    let paused = false;
    let pausedPositionBars = 0;
    let muted = readMuted();
    let gestureArmed = false;
    // 语音配音 ducking：0 表示不压低，数字越大音乐越轻。语音总线独立，不接入音乐图。
    let voiceDuckIntensity = 0;
    /**
     * 调度窗口日志：每次 tick 记录「这一拍补排了几个小节」。
     * 排程供应当断流时，这里会连续出现 bars=0，是判断 lookahead 耗尽的直接证据。
     */
    interface ScheduleWindow {
        at: number;
        buses: number;
        bars: number;
        level: number;
    }
    const scheduleWindows: ScheduleWindow[] = [];
    const SCHEDULE_WINDOW_LIMIT = 120;

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

    /** 长音尾巴长度：按时值成比例，兼顾慢速恐怖的长混响与快速紧迫的干净切分。 */
    function sustainOverlap(barDuration: number): number {
        return clamp(barDuration * SUSTAIN_OVERLAP_RATIO, SUSTAIN_OVERLAP_MIN, SUSTAIN_OVERLAP_MAX);
    }

    /**
     * 等功率交叉淡化曲线：A/B 总线切换时，旧总线按 cos 淡出、新总线按 sin 淡入，
     * 两条不相关信号的功率之和保持恒定，避免线性淡化在中点掉 3dB 的「凹槽」。
     */
    function equalPowerCurve(from: number, to: number, rising: boolean, samples = 48): Float32Array<ArrayBuffer> {
        const curve = new Float32Array(new ArrayBuffer(samples * 4));
        for (let index = 0; index < samples; index += 1) {
            const progress = samples <= 1 ? 1 : index / (samples - 1);
            const shape = rising
                ? Math.sin((progress * Math.PI) / 2)
                : Math.cos((progress * Math.PI) / 2);
            curve[index] = Math.max(0.0001, from + (to - from) * shape);
        }
        return curve;
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
            mixInput = context.createGain();
            mixInput.gain.value = 0.9;
            musicMuteGain = context.createGain();
            musicMuteGain.gain.value = muted ? 0 : 1;
            // 引擎总增益：off 时整体淡出；外链轨道也挂在它下面，所以 off 一定安静。
            engineGain = context.createGain();
            engineGain.gain.value = engineMode === "off" ? 0.0001 : 1;
            bedGain = context.createGain();
            bedGain.gain.value = 1;
            bedGain.connect(engineGain);

            // 安全限幅：多声部叠加时优先保护听力，避免高潮段削波。
            // 干声与环境音都必须经过这里，否则限幅器只保护混响、保护不了主输出。
            const limiter = context.createDynamicsCompressor();
            limiter.threshold.value = -8;
            limiter.knee.value = 8;
            limiter.ratio.value = 4;
            limiter.attack.value = 0.008;
            limiter.release.value = 0.3;

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
            // 配乐先过独立静音增益，再进入共享混音汇入点。
            engineGain.connect(musicMuteGain);
            musicMuteGain.connect(mixInput);
            mixInput.connect(limiter);

            // 双混响：近场给打击乐与主旋律「击点」，远场加 pre-delay 做空间纵深。
            masterReverb = context.createConvolver();
            masterReverb.buffer = createReverbBuffer(context, qualityTier >= 2 ? 1.0 : 1.7);
            const reverbGain = context.createGain();
            reverbGain.gain.value = 0.42;
            masterReverb.connect(reverbGain);
            reverbGain.connect(engineGain);

            masterReverbFar = context.createConvolver();
            masterReverbFar.buffer = createReverbBuffer(context, qualityTier >= 2 ? 1.8 : 3.2);
            masterFarInput = context.createDelay(0.2);
            masterFarInput.delayTime.value = qualityTier >= 2 ? 0.02 : 0.045;
            const farGain = context.createGain();
            farGain.gain.value = 0.34;
            masterFarInput.connect(masterReverbFar);
            masterReverbFar.connect(farGain);
            farGain.connect(engineGain);

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
            delayOut.connect(engineGain);

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
        /** 当前正在延续的持续声部和弦键；相同键的小节共用一条长音，不逐小节重触发。 */
        private sustainKey = "";
        private sustainReady = false;
        /** 低音同样按「同一根音」跨小节延续，避免每小节线出现低频断口。 */
        private bassKey = "";
        private bassReady = false;
        /** 上一条长音/低音实际响到什么时候，用来判断下一条是否需要等功率接力。 */
        private sustainUntil = 0;
        private bassUntil = 0;
        /** 上一次长音的实际音高排列，用于把新和弦转位到最近的位置（声部连接）。 */
        private lastChordNotes: number[] = [];
        /** 累计已排程的小节数：调度窗口诊断据此判断 lookahead 有没有断供。 */
        scheduledBars = 0;
        /** 当前小节的总线动态目标，用于诊断「断流时长」。 */
        lastTarget = 0;

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
                // 共享位置如果正好落在留白/抽空小节，就从最近的有和弦小节起播：
                // 否则换歌的第一秒就是「抽空」，听感等同播放中断。
                const audible = this.audibleOrdinal(ordinal);
                this.seekToOrdinal(audible);
                const fraction = startPositionBars - Math.floor(startPositionBars);
                if (audible === ordinal && fraction > 0.02 && ordinal > Math.floor(startPositionBars)) {
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
            // 等功率淡入：与旧总线的 cos 淡出配对，交叉期间功率不塌陷。
            this.gain.gain.setValueAtTime(0.0001, at);
            this.gain.gain.setValueCurveAtTime(
                equalPowerCurve(0.0001, Math.max(0.001, this.baseLevel), true),
                at,
                Math.max(0.05, fadeMs / 1000),
            );
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
            const live = Math.max(0.0001, Number(this.gain.gain.value) || this.currentLevel);
            if (this.currentLevel > 0 && prev > 0) {
                this.currentLevel = this.currentLevel * (level / prev);
            }
            try {
                this.gain.gain.cancelAndHoldAtTime(now);
            } catch {
                this.gain.gain.cancelScheduledValues(now);
            }
            this.gain.gain.setValueAtTime(live, now);
            this.gain.gain.linearRampToValueAtTime(Math.max(0.0001, level), now + Math.max(0.2, fadeSeconds));
        }

        fadeOut(now: number, fadeMs: number): void {
            if (this.expired || this.disposed) return;
            this.expired = true;
            const seconds = Math.max(0.2, fadeMs / 1000);
            const from = Math.max(0.0001, Number(this.gain.gain.value) || this.currentLevel || 0.0001);
            try {
                this.gain.gain.cancelAndHoldAtTime(now);
            } catch {
                this.gain.gain.cancelScheduledValues(now);
            }
            // 与 oncoming 总线的 sin 淡入配对，为中点保持等功率。
            this.gain.gain.setValueAtTime(from, now);
            this.gain.gain.setValueCurveAtTime(
                equalPowerCurve(from, 0.0001, false),
                now,
                seconds,
            );
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

        /**
         * 此刻真正在出声的小节序号（排程是提前 2 秒做的，直接取 barIndex 会偏前）。
         * 暂停/跳段时用它作为「继续位置」，恢复后不会跳音或从头开始。
         */
        audibleBars(): number {
            const barDuration = Math.max(0.1, this.timeline.barDuration);
            const ahead = Math.max(0, Math.ceil((this.nextBarTime - this.ctx.currentTime) / barDuration));
            const index = this.cycle * this.timeline.totalBars + this.barIndex - ahead;
            return Math.max(0, index);
        }

        /** 外链衔接用：当前小节长度、距下一个小节线的秒数、当前小节位置。 */
        barTiming(): { barSeconds: number; nextBarIn: number; positionBars: number } {
            return {
                barSeconds: Math.max(0.1, this.timeline.barDuration),
                nextBarIn: Math.max(0, this.nextBarTime - this.ctx.currentTime),
                positionBars: this.positionBars(),
            };
        }

        epoch(): number {
            return this.syncEpoch;
        }

        /** 运行时诊断：断流排查时可以直接看到每条总线排到哪、下一次补发在什么时刻。 */
        describe(): ImmersiveMusicBusDiagnostics {
            const bar = this.timeline.bars[this.barIndex];
            return {
                cycle: this.cycle,
                bar_index: this.barIndex,
                next_bar_at: Number(this.nextBarTime.toFixed(3)),
                position_bars: Number(this.positionBars().toFixed(2)),
                section: bar?.section ?? "end",
                scheduled_bars: this.scheduledBars,
                level: Number(this.lastTarget.toFixed(4)),
            };
        }

        /** 换歌切入时避开无和弦的留白小节：向前顺延到最近一个有声小节。 */
        private audibleOrdinal(from: number): number {
            let ordinal = Math.max(0, from);
            for (let step = 0; step < 8; step += 1) {
                const total = Math.max(1, this.timeline.bars.length);
                const cycle = Math.floor(ordinal / total);
                const index = ordinal - cycle * total;
                const timeline = cycle === this.cycle
                    ? this.timeline
                    : TrpgImmersiveMusicArranger.buildTimeline(this.spec, cycle);
                const bar = timeline.bars[index];
                if (bar && bar.chord) return ordinal;
                ordinal += 1;
            }
            return from;
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
            this.lastChordNotes = [];
            this.resetSustain();
        }

        /** 切入等待期的过渡垫：用同一位置上的和弦/低音先填满余下的小节。 */
        private playBridge(at: number, seconds: number): void {
            const bars = this.timeline.bars;
            if (!bars.length) return;
            const bar = bars[Math.max(0, Math.min(this.bridgeBarIndex, bars.length - 1))];
            if (!bar) return;
            const bridgeGain = Math.max(0.32, bar.dynamic);
            const duration = Math.max(0.2, seconds);
            // 过渡垫多留一点尾巴，与下一小节的等功率起音时长一致，
            // 玩家中途加入时不会先听到空白，交接点前后功率互补。
            const tail = Math.max(0.15, sustainOverlap(this.timeline.barDuration));
            // 旧实现只在「目标小节本身有和弦」时铺过渡垫：如果共享位置正好落在
            // 留白/抽空小节，而旧总线已经淡出，这一段就是彻底静音（最多一整小节）。
            // 这里向前回溯到最近一个有声的小节；仍然找不到就用主音三和弦兜底，
            // 保证换歌/中途加入永远不会先出现一小节空白。
            const mode = this.spec.mode;
            const fallbackIntervals = [0, mode.degrees[2] ?? 3, mode.degrees[4] ?? 7];
            let bridgeNotes: number[] | null = null;
            for (let step = 0; step < bars.length; step += 1) {
                const index = ((this.bridgeBarIndex - step) % bars.length + bars.length) % bars.length;
                const candidate = bars[index];
                if (!candidate || !candidate.chord) continue;
                const chord = candidate.chord;
                bridgeNotes = chord.intervals.map((interval) => chord.root + interval);
                break;
            }
            if (!bridgeNotes) bridgeNotes = fallbackIntervals.map((interval) => this.spec.tonic + interval);
            this.rack.pad(bridgeNotes, at, duration + tail, bridgeGain * 0.7, bar.cutoff, 0, tail, { continuous: true });
            this.lastChordNotes = bridgeNotes;
            this.sustainUntil = at + duration + tail;
            this.sustainReady = true;
            if (bar.bass !== null) {
                this.rack.bass(bar.bass, at, duration + tail * 0.7, bar.dynamic * 0.8, tail * 0.7, { continuous: true });
                this.bassUntil = at + duration + tail * 0.7;
                this.bassReady = true;
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
                    this.resetSustain();
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
            // 循环接缝处保留上一条长音的尾巴，让新循环的第一小节做等功率接力。
            this.resetSustain(true);
        }

        /** 持续声部的「段落键」：和弦与配器都相同的小节共用同一条长音。 */
        private sustainKeyFor(bar: TrpgImmersiveMusicArranger.BarPlan): string {
            if (!bar.chord || !bar.chordVoices.length) return "";
            return [
                bar.chord.root,
                bar.chord.intervals.join(","),
                bar.chordVoices.join(","),
            ].join("|");
        }

        private bassKeyFor(bar: TrpgImmersiveMusicArranger.BarPlan): string {
            return bar.bass === null ? "" : `bass:${bar.bass}`;
        }

        /** 从当前小节向后数，有多少个连续小节仍共享同一个段落键（遇到留白即停）。 */
        private spanBars(fromIndex: number, keyOf: (bar: TrpgImmersiveMusicArranger.BarPlan) => string): number {
            const bars = this.timeline.bars;
            const first = bars[fromIndex];
            if (!first) return 0;
            const key = keyOf(first);
            if (!key) return 0;
            let count = 1;
            for (let index = fromIndex + 1; index < bars.length; index += 1) {
                const next = bars[index];
                if (!next || next.silence || keyOf(next) !== key) break;
                count += 1;
            }
            return count;
        }

        /**
         * 保持和弦级数不变，只把各声部按八度移到离上一次发声最近的位置。
         * 这样 C→G 之类的进行不会让整块 pad 平行大跳，听感更接近持续配乐。
         */
        private voiceLead(notes: number[]): number[] {
            if (!this.lastChordNotes.length) return notes.slice();
            const voiced = notes.map((note, index) => {
                const anchor = this.lastChordNotes[index];
                if (anchor === undefined || !Number.isFinite(anchor)) return note;
                let moved = note;
                while (moved - anchor > 6) moved -= 12;
                while (anchor - moved > 6) moved += 12;
                return moved;
            });
            // 连续五度进行时逐声部就近转位会一路把整块和声带偏；以本和弦的原始根音为锚，
            // 整块八度平移回 ±1 八度内，既保留平滑的声部移动，也不会让 pad 越飘越低/越高。
            const anchor = notes[0];
            if (anchor === undefined || !voiced.length) return voiced;
            const low = Math.min(...voiced);
            let shift = 0;
            while (low + shift - anchor > 12) shift -= 12;
            while (anchor - (low + shift) > 12) shift += 12;
            return shift === 0 ? voiced : voiced.map((note) => note + shift);
        }

        /** 换循环 / 跳播 / 重建谱面后必须重新起音，不能沿用上一段的持续状态。 */
        private resetSustain(keepTail = false): void {
            this.sustainKey = "";
            this.bassKey = "";
            if (keepTail) return;
            this.sustainReady = false;
            this.bassReady = false;
            this.sustainUntil = 0;
            this.bassUntil = 0;
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
            // 只有真的跨过循环才清空延续状态；普通逐小节推进必须保持长音不断。
            if (guard > 0) this.resetSustain();
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
                // 纯正弦的 drone 听起来像测试音；叠一层低通锯齿与微失谐，
                // 才有电影低频那种「有厚度的嗡鸣」。
                const filter = this.ctx.createBiquadFilter();
                filter.type = "lowpass";
                filter.frequency.value = 180;
                filter.Q.value = 0.5;
                filter.connect(this.droneGain);
                [-7, 7].forEach((detune) => {
                    const saw = this.ctx.createOscillator();
                    saw.type = "sawtooth";
                    saw.frequency.value = TrpgImmersiveMusicVoices.midiToFreq(root - 24);
                    saw.detune.value = detune;
                    const tone = this.ctx.createGain();
                    tone.gain.value = 0.14;
                    saw.connect(tone);
                    tone.connect(filter);
                    saw.start(at);
                    this.droneNodes.push(saw);
                });
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
                // 休止不等于「把总线拉到 0」：留一点点底噪与混响尾音，
                // 否则前一小节的厅堂混响会被硬切，听感就是「突然关机」。
                ? Math.max(0.0015, this.baseLevel * 0.04)
                // 有和弦：常规动态曲线。
                // 无和弦（留白/抽空）：不是静音，而是把总线压到约 -17dB，
                // 让心跳、颗粒、钟声与混响继续托住「房间还在」。
                : bar.chord
                    ? this.baseLevel * (0.22 + bar.dynamic * 0.85)
                    : this.baseLevel * (0.1 + bar.dynamic * 0.5);
            this.dynamicGain.gain.setTargetAtTime(Math.max(0.0015, target), start, bar.silence ? 0.32 : 0.16);
            this.lastTarget = target;
            this.scheduledBars += 1;
            this.filter.frequency.setTargetAtTime(clamp(bar.cutoff, 180, 5200), start, 0.22);
            this.sendNear.gain.setTargetAtTime(clamp(bar.reverb * 0.55, 0, 1), start, 0.28);
            const farFactor = bar.section === "climax" ? 0.24 : bar.section === "void" ? 0.7 : 0.42;
            this.sendFar.gain.setTargetAtTime(clamp(bar.reverb * farFactor, 0, 1), start, 0.28);
            this.delaySend.gain.setTargetAtTime(bar.section === "climax" ? 0.05 : 0.1, start, 0.3);
            this.droneGain.gain.setTargetAtTime(this.droneTarget(bar.section), start, 0.5);

            if (bar.silence) {
                // 留白是真正的休止：清空延续状态，休止后的同和弦也要重新起音。
                this.resetSustain();
                return;
            }
            const random = TrpgImmersiveMusicArranger.streamRandom(
                this.spec.seedBase,
                ["play", this.cycle, bar.index],
            );

            const overlap = sustainOverlap(barDuration);
            if (bar.chord) {
                const notes = bar.chord.intervals.map((interval) => bar.chord ? bar.chord.root + interval : 0);
                const voiced = this.voiceLead(notes);
                const key = this.sustainKeyFor(bar);
                // 同一和弦/配器只在第一小节起音一次；后续小节共用同一条长音，
                // 不再每小节重新做一遍淡入淡出（那正是「每小节断一次」的来源）。
                if (!key) {
                    this.sustainKey = "";
                    this.sustainReady = false;
                } else if (!this.sustainReady || key !== this.sustainKey) {
                    const span = Math.max(1, this.spanBars(this.barIndex, (item) => this.sustainKeyFor(item)));
                    const spanSeconds = span * barDuration;
                    // 整条长音结束后如果紧接留白，就在最后一个尾音前收干净；否则让它跨过段落线。
                    const afterSpan = this.timeline.bars[this.barIndex + span];
                    const endsInSilence = Boolean(afterSpan && afterSpan.silence);
                    const duration = Math.max(
                        beat * 0.8,
                        endsInSilence
                            ? spanSeconds - barDuration * 0.45
                            : spanSeconds + overlap,
                    );
                    // 上一条长音如果此刻仍在响（和弦切换点），新声部用等功率曲线接力；
                    // 从留白/首次进入时则保留原本的慢起音，音色不变。
                    const legato = this.sustainReady && start < this.sustainUntil - 0.01;
                    const shape = { continuous: true, legato };
                    bar.chordVoices.forEach((voice, voiceIndex) => {
                        const pan = (voiceIndex - (bar.chordVoices.length - 1) / 2) * 0.18;
                        const gain = VOICE_GAIN[voice] || 0.06;
                        switch (voice) {
                            case "strings":
                                this.rack.strings(voiced, start, duration, gain, bar.cutoff, pan, overlap, shape);
                                break;
                            case "organ":
                                this.rack.organ(voiced, start, duration, gain * 0.85, overlap, shape);
                                break;
                            case "choir":
                                this.rack.choir(voiced, start, duration, gain * 0.9, overlap, shape);
                                break;
                            case "pad":
                            default:
                                this.rack.pad(voiced, start, duration, gain, bar.cutoff, pan, overlap, shape);
                                break;
                        }
                    });
                    this.lastChordNotes = voiced;
                    this.sustainKey = key;
                    this.sustainReady = true;
                    this.sustainUntil = start + duration;
                }
                if (bar.brass) {
                    // 铜管是每小节的进入型声部，起音时长保持原配方，不随长音联动。
                    this.rack.brass(
                        voiced,
                        start + beat * 0.55,
                        Math.max(beat * 0.8, barDuration * (closing ? 0.42 : 0.78)),
                        VOICE_GAIN.brass || 0.05,
                        bar.cutoff,
                    );
                }
            } else {
                // 抽空段没有和弦：清空延续状态，下一段仍会正常起音。
                this.sustainKey = "";
                this.sustainReady = false;
            }

            const bassNote = bar.bass;
            if (bassNote !== null) {
                if (this.spec.profile === "urgency") {
                    const pulses = bar.section === "climax" ? 4 : 2;
                    for (let index = 0; index < pulses; index += 1) {
                        this.rack.bass(bassNote, start + index * beat, beat * 0.62, (VOICE_GAIN.bass || 0.15) * 0.85);
                    }
                } else {
                    const key = this.bassKeyFor(bar);
                    if (!key) {
                        this.bassKey = "";
                        this.bassReady = false;
                    } else if (!this.bassReady || key !== this.bassKey) {
                        const span = Math.max(1, this.spanBars(this.barIndex, (item) => this.bassKeyFor(item)));
                        const spanSeconds = span * barDuration;
                        const afterSpan = this.timeline.bars[this.barIndex + span];
                        const endsInSilence = Boolean(afterSpan && afterSpan.silence);
                        const duration = Math.max(
                            beat * 0.8,
                            endsInSilence
                                ? spanSeconds - barDuration * 0.4
                                : spanSeconds + overlap * 0.8,
                        );
                        const legato = this.bassReady && start < this.bassUntil - 0.01;
                        this.rack.bass(
                            bassNote,
                            start,
                            duration,
                            VOICE_GAIN.bass || 0.15,
                            overlap * 0.8,
                            { continuous: true, legato },
                        );
                        this.bassKey = key;
                        this.bassReady = true;
                        this.bassUntil = start + duration;
                    }
                }
            } else {
                this.bassKey = "";
                this.bassReady = false;
            }

            if (bar.ostinato) {
                this.playOstinato(bar, start, barDuration);
            }
            // 留白的动机碎片没有和弦，用主音/踏板音作锚点；旧实现因为 `bar.chord` 守卫
            // 把留白段唯一的前景动机整段丢掉，只剩几颗钟声。
            if (bar.motif && (bar.chord || bar.section === "void")) {
                this.playMotif(bar.motif, bar, start, beat);
            }
            if (bar.counter && (bar.chord || bar.section === "void")) {
                this.playMotif(bar.counter, bar, start, beat);
            }
            if (bar.motion) {
                this.playMotion(bar.motion, bar, start, beat);
            }
            if (bar.pulse) {
                const pulse = bar.pulse;
                pulse.hits.forEach((hit) => {
                    if (pulse.kind === "heartbeat") {
                        this.rack.heartbeat(start + hit * beat, pulse.gain);
                    } else {
                        this.rack.clock(start + hit * beat, pulse.gain, hit % 2 === 0 ? -0.25 : 0.25);
                    }
                });
            }
            if (bar.texture) {
                // 留白里的颗粒层更轻，只做「房间还在呼吸」的底，不抢钟声。
                const textureGain = bar.section === "void" ? 0.014 : 0.02;
                this.rack.texture(start, barDuration, textureGain, bar.cutoff);
            }
            if (bar.impact) {
                this.rack.impact(start, 0.09);
            }
            if (bar.riser) {
                this.rack.riser(start, Math.min(barDuration * 0.9, 4.5), 0.055);
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

        /** 内声部推进：不改变和弦功能，只在弱拍用和弦音走两三个音。 */
        private playMotion(
            motion: TrpgImmersiveMusicArranger.MotionPlan,
            bar: TrpgImmersiveMusicArranger.BarPlan,
            start: number,
            beat: number,
        ): void {
            if (!bar.chord) return;
            let cursor = start + motion.startBeat * beat;
            const root = bar.chord.root;
            motion.degrees.forEach((degree, index) => {
                const duration = Math.max(0.12, (motion.rhythm[index] ?? 1) * beat);
                const note = root + degree + motion.octave;
                this.renderMotifNote(
                    motion.instrument,
                    note,
                    cursor,
                    duration,
                    motion.gain,
                    bar.cutoff,
                    "single",
                );
                cursor += duration;
            });
        }

        private playMotif(
            motif: TrpgImmersiveMusicArranger.MotifPlan,
            bar: TrpgImmersiveMusicArranger.BarPlan,
            start: number,
            beat: number,
        ): void {
            // 留白段没有和弦，但动机仍然要有锚点：优先踏板音，其次主音。
            const root = bar.chord
                ? bar.chord.root
                : (bar.pedal !== null ? bar.pedal : this.spec.tonic);
            const available = Math.max(0.5, 4 - motif.startBeat);
            const cell = TrpgImmersiveMusicTheory.fitRhythm(motif.cell, available);
            let cursor = start + motif.startBeat * beat;
            const baseNote = root + motif.octave;
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
                    // FM 层只做金属泛音的点缀；最小档位里 fmBell 自己会跳过。
                    this.rack.fmBell(note, start, gain * 0.32);
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
        if (!ctx || !bedGain || !engineGain || !masterReverb || !masterReverbFar || !masterFarInput || !masterDelay || !noiseBuffer) return;
        const spec = TrpgImmersiveMusicArranger.resolveSpec(plan);
        const bus = new MusicBus(
            ctx,
            plan,
            spec,
            bedGain,
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
        notifyState();
    }

    function tick(): void {
        if (!context) return;
        // 用户暂停时不补发任何音符：定时器可以留着，但绝不能重新排程。
        if (paused) return;
        // 页面切后台/系统打断后 AudioContext 可能停在 suspended，定时器却还在跑。
        // 这里主动恢复，避免「调度器还在、声音已经没了」的假运行。
        if (context.state === "suspended") {
            void context.resume().catch(() => armGesture());
            return;
        }
        const until = context.currentTime + SCHEDULE_AHEAD_SECONDS;
        liveBuses = liveBuses.filter((bus) => !bus.isDisposed);
        const before = liveBuses.reduce((sum, bus) => sum + bus.scheduledBars, 0);
        for (const bus of liveBuses) bus.scheduleUntil(until);
        const after = liveBuses.reduce((sum, bus) => sum + bus.scheduledBars, 0);
        const top = liveBuses[liveBuses.length - 1];
        scheduleWindows.push({
            at: Number(context.currentTime.toFixed(2)),
            buses: liveBuses.length,
            bars: after - before,
            level: top ? Number(top.lastTarget.toFixed(4)) : 0,
        });
        if (scheduleWindows.length > SCHEDULE_WINDOW_LIMIT) scheduleWindows.shift();
        if (!liveBuses.length) {
            // 所有总线都已结束但仍有活动 plan：说明被异常清空过（例如上下文被打断），
            // 重建一条总线，而不是让调度器静默退出。
            if (activePlan && !muted && context.state === "running" && engineMode !== "off") {
                activeSignature = "";
                switchBus(activePlan);
                return;
            }
            stopScheduler();
        }
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
        const audible = Boolean(activePlan) && !muted && !paused && Boolean(context) && liveBuses.length > 0 && engineMode !== "off";
        const shouldDuck = audible && activePlan?.duck_bgm !== false;
        try {
            window.TrpgBgm?.setDuck?.(shouldDuck);
        } catch {
            // BGM 播放器尚未初始化时忽略。
        }
        // 环境音跟着「配乐是否在响」让位；它有自己的开关，不受配乐静音影响。
        try {
            window.TrpgImmersiveAmbient?.setMusicActive?.(audible);
        } catch {
            // 环境音脚本尚未加载时忽略。
        }
    }

    function applyAmbient(plan: ImmersiveMusicPlan): void {
        try {
            window.TrpgImmersiveAmbient?.apply?.(plan);
        } catch {
            // 环境音是增强层，任何异常都不能影响配乐主链路。
        }
    }

    export function apply(plan: ImmersiveMusicPlan): void {
        if (!plan || !plan.profile) return;
        const normalized = normalizePlan(plan);
        activePlan = normalized;
        // 引擎关闭：只记住最新谱面，等重新开启时再重建；期间不补发任何事件。
        if (engineMode === "off") {
            queuedPlan = normalized;
            const signature = contentSignature(normalized);
            activeSignature = signature;
            return;
        }
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
        // 环境音跟随场景先切换；即使配乐被静音，环境音仍按用户自己的开关工作。
        applyAmbient(normalized);
        // 用户暂停期间只记住「接下来要播什么」，绝不重建总线；
        // 恢复时从暂停位置继续，因此不会出现两条总线叠在一起。
        if (paused) {
            // 暂停期间如果换了曲子，恢复点应跟随新曲子的共享位置，而不是沿用旧曲的小节号。
            if (signature !== activeSignature) {
                activeSignature = signature;
                pausedPositionBars = syncAnchor ? syncAnchor.positionBars : 0;
            }
            notifyState();
            return;
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
            notifyState();
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
        paused = false;
        pausedPositionBars = 0;
        if (context) {
            const now = context.currentTime;
            for (const bus of liveBuses.slice()) bus.fadeOut(now, 900);
        }
        liveBuses = [];
        stopScheduler();
        try {
            window.TrpgImmersiveAmbient?.stop?.(700);
        } catch {
            // 环境音尚未加载时忽略。
        }
        applyDuck();
        notifyState();
    }

    /** 通知播放器刷新：引擎状态变化统一从这一个出口广播，避免各处漏更新。 */
    function notifyState(): void {
        try {
            document.dispatchEvent(new CustomEvent("trpg:music-state"));
        } catch {
            // 非浏览器环境（Node 探针）没有 document，忽略即可。
        }
    }

    /** 用户暂停：淡出并停止补发，但记住此刻真正出声的小节。 */
    export function pause(): void {
        if (paused) return;
        paused = true;
        const live = liveBuses[liveBuses.length - 1] ?? null;
        if (live) pausedPositionBars = live.audibleBars();
        if (context) {
            if (musicMuteGain) musicMuteGain.gain.setTargetAtTime(0, context.currentTime, 0.08);
            for (const bus of liveBuses.slice()) bus.fadeOut(context.currentTime, 380);
        }
        liveBuses = [];
        stopScheduler();
        applyDuck();
        notifyState();
    }

    /** 从暂停位置继续：按小节边界重建一条总线，不会从头开始也不会叠加。 */
    export function resume(): void {
        if (!paused) return;
        paused = false;
        const ctx = ensureContext();
        if (musicMuteGain && ctx) {
            musicMuteGain.gain.setTargetAtTime(muted ? 0 : 1, ctx.currentTime, 0.12);
        }
        if (!ctx || muted || engineMode === "off" || !activePlan) {
            applyDuck();
            notifyState();
            return;
        }
        if (ctx.state === "suspended") {
            void ctx.resume().catch(() => armGesture());
            armGesture();
            applyDuck();
            notifyState();
            return;
        }
        activeSignature = contentSignature(activePlan);
        switchBus(activePlan, null, pausedPositionBars);
    }

    export function isPaused(): boolean {
        return paused;
    }

    export function togglePause(): boolean {
        if (paused) resume();
        else pause();
        return paused;
    }

    /**
     * 程序化配乐没有音轨，所以「下一首」= 跳到当前 cue 的下一个段落
     * （bed → rise → climax → void → tail → bed），保持在同一循环里。
     */
    export function skipSection(direction: number): boolean {
        if (!activePlan) return false;
        const spec = TrpgImmersiveMusicArranger.resolveSpec(activePlan);
        const timeline = TrpgImmersiveMusicArranger.buildTimeline(spec, 0);
        if (!timeline.bars.length) return false;
        const order: TrpgImmersiveMusicTheory.SectionKind[] = ["bed", "rise", "climax", "void", "tail"];
        const live = liveBuses[liveBuses.length - 1] ?? null;
        const current = paused ? null : live?.currentSectionKind() ?? null;
        const currentIndex = current ? order.indexOf(current) : -1;
        const step = direction >= 0 ? 1 : -1;
        const nextIndex = ((Math.max(0, currentIndex) + step) % order.length + order.length) % order.length;
        const nextKind = order[nextIndex] ?? "bed";
        const total = Math.max(1, timeline.totalBars);
        const base = paused ? pausedPositionBars : live?.positionBars() ?? 0;
        const cycle = Math.floor(base / total);
        const targetBar = TrpgImmersiveMusicArranger.sectionStartBar(timeline, nextKind);
        const ordinal = cycle * total + targetBar;
        if (paused) {
            pausedPositionBars = ordinal;
            notifyState();
            return true;
        }
        activeSignature = contentSignature(activePlan);
        switchBus(activePlan, null, ordinal);
        return true;
    }

    /**
     * 音乐来源切换到「仅 BGM」时调用：淡出后销毁 AudioContext，确保引擎完全不发声、
     * 也不再占用音频线程与调度资源。只处理总线/上下文，不触碰编曲与音色实现。
     *
     * 例外：环境音（P2）与配乐是两套独立开关。用户开着环境音时，
     * 只把配乐总线全部淡出，保留共享 AudioContext 给环境音使用；
     * 否则「仅 BGM」会连带把雨声/风声一起关掉。
     */
    export function shutdown(): void {
        // 必须先读环境音状态：stop() 会顺带淡出环境音并清空当前场景。
        let ambientActive = false;
        try {
            ambientActive = window.TrpgImmersiveAmbient?.isEnabled?.() === true;
        } catch {
            ambientActive = false;
        }
        stop();
        activePlan = null;
        queuedPlan = null;
        activeSignature = "";
        liveBuses = [];
        gestureArmed = false;
        if (ambientActive) {
            // 仅 BGM 模式：环境音继续用共享上下文；这里重新拉起场景，
            // 因为 stop() 已经把它淡出了。
            try {
                window.TrpgImmersiveAmbient?.resume?.();
            } catch {
                // 环境音尚未加载时忽略。
            }
            return;
        }
        const previous = context;
        context = null;
        master = null;
        mixInput = null;
        musicMuteGain = null;
        bedGain = null;
        engineGain = null;
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
        // 静音只关配乐总线；环境音有独立开关，必须继续出声。
        if (musicMuteGain && context) {
            musicMuteGain.gain.setTargetAtTime(muted ? 0 : 1, context.currentTime, 0.12);
        }
        if (!muted && activePlan && engineMode !== "off") {
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
        notifyState();
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
        // 静音已交给 musicMuteGain；这里只负责语音配音的 ducking，
        // 因此环境音会跟着语音一起让位，但不会被「配乐静音」误杀。
        return Math.max(0.1, 1 - voiceDuckIntensity / 100);
    }

    export function toggleMuted(): boolean {
        setMuted(!muted);
        return muted;
    }

    export function unlock(): boolean {
        const ctx = ensureContext();
        if (!ctx) return false;
        // 用户暂停期间只解锁上下文，不重建总线：否则一次点击就会让音乐偷偷响起来。
        if (ctx.state === "suspended") {
            void ctx.resume().then(() => {
                if (ctx.state === "running" && activePlan && !liveBuses.length && !muted && !paused && engineMode !== "off") {
                    activeSignature = "";
                    switchBus(activePlan, null, startPositionBars(null, syncAnchor));
                }
            }).catch(() => undefined);
        }
        if (ctx.state === "running" && activePlan && !liveBuses.length && !muted && !paused && engineMode !== "off") {
            activeSignature = "";
            switchBus(activePlan, null, startPositionBars(null, syncAnchor));
        }
        applyDuck();
        return ctx.state === "running";
    }

    export function isRunning(): boolean {
        return Boolean(activePlan) && Boolean(context) && liveBuses.length > 0 && !muted && !paused && engineMode !== "off";
    }

    export function currentPlan(): ImmersiveMusicPlan | null {
        return activePlan;
    }

    export function quality(): number {
        return qualityTier;
    }

    /**
     * 引擎切换（P4）：`off` 时用指数淡化在 `fadeMs` 内把整体压到静音，
     * 并清空总线停止补发；`procedural`/`external` 时把总增益抬回 1，
     * 若此前没有总线则按共享位置重建，保证不是硬切。
     */
    export function setEngineMode(mode: string, fadeMs = 2200): void {
        const next = mode === "external" || mode === "off" ? mode : "procedural";
        const previous = engineMode;
        engineMode = next;
        const ctx = ensureContext();
        if (!ctx || !engineGain) return;
        const seconds = Math.max(0.35, Math.min(12, Number(fadeMs || 2200) / 1000));
        const now = ctx.currentTime;
        const gain = engineGain.gain;
        gain.cancelScheduledValues(now);
        // 外链块生效时程序化配乐必须关闭：与 off 一样淡出并停止总线，不再保留底床。
        if (next === "off" || next === "external") {
            gain.setValueAtTime(Math.max(0.0001, gain.value), now);
            gain.exponentialRampToValueAtTime(0.0001, now + seconds);
            window.setTimeout(() => {
                if (engineMode !== "off" && engineMode !== "external") return;
                activeSignature = "";
                for (const bus of liveBuses.slice()) bus.fadeOut(context?.currentTime ?? now, 0.25);
                liveBuses = [];
                stopScheduler();
                applyDuck();
                notifyState();
            }, Math.round(seconds * 1000) + 80);
            return;
        }
        gain.setValueAtTime(Math.max(0.0001, gain.value), now);
        gain.exponentialRampToValueAtTime(1, now + Math.min(seconds, 3));
        if (previous === "off" && activePlan && !muted && !paused && ctx.state === "running" && !liveBuses.length) {
            activeSignature = "";
            switchBus(activePlan, null, startPositionBars(null, syncAnchor));
        }
        notifyState();
    }

    export function currentEngineMode(): "procedural" | "external" | "off" {
        return engineMode;
    }

    /**
     * 底床电平的等功率斜变：外链淡入时底床按 cos 曲线压下、外链淡出时按 cos 抬起，
     * 与外部轨道的 sin 曲线配对，功率之和恒定，不会在中点掉 3dB。
     */
    export function rampBedLevel(target: number, seconds: number, rising: boolean): void {
        const ctx = context;
        if (!ctx || !bedGain) return;
        const gain = bedGain.gain;
        const now = ctx.currentTime;
        const from = Math.max(0.0001, gain.value);
        const to = clamp(Number(target), 0.0001, 1);
        const duration = Math.max(0.08, Number(seconds) || 0.08);
        const samples = 64;
        const curve = new Float32Array(samples);
        for (let index = 0; index < samples; index += 1) {
            const progress = samples <= 1 ? 1 : index / (samples - 1);
            const shape = rising
                ? Math.sin((progress * Math.PI) / 2)
                : Math.cos((progress * Math.PI) / 2);
            curve[index] = Math.max(0.0001, from + (to - from) * shape);
        }
        gain.cancelScheduledValues(now);
        gain.setValueAtTime(from, now);
        try {
            gain.setValueCurveAtTime(curve, now, duration);
        } catch {
            gain.exponentialRampToValueAtTime(to, now + duration);
        }
    }

    export function bedDuckLevel(): number {
        return BED_DUCK_LEVEL;
    }

    /**
     * 外链播放器的汇入点：引擎总增益（受 off / 静音 / 语音 ducking 控制），
     * 不经过底床增益，所以压低底床不会连带压低外链曲目。
     */
    export function externalInput(): GainNode | null {
        const ctx = ensureContext();
        return ctx && engineGain ? engineGain : null;
    }

    export function barTiming(): { barSeconds: number; nextBarIn: number; positionBars: number } | null {
        const live = liveBuses[liveBuses.length - 1];
        if (!live || !context) return null;
        return live.barTiming();
    }

    export function audioClock(): number {
        return context ? context.currentTime : 0;
    }

    export function diagnostics(): ImmersiveMusicDiagnostics {
        return {
            context: context ? context.state : "none",
            quality: qualityTier,
            muted,
            buses: liveBuses.map((bus) => bus.describe()),
            windows: scheduleWindows.slice(),
        };
    }

    /** 环境音引擎的接入点：共享上下文、噪声源与限幅前的混音汇入点。 */
    export interface AudioGraph {
        ctx: AudioContext;
        input: GainNode;
        noise: AudioBuffer;
        quality: number;
    }

    export function audioGraph(): AudioGraph | null {
        const ctx = ensureContext();
        if (!ctx || !mixInput || !noiseBuffer) return null;
        return { ctx, input: mixInput, noise: noiseBuffer, quality: qualityTier };
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
    diagnostics: TrpgImmersiveMusicModule.diagnostics,
    // P3/P4 接入点：引擎总增益、底床等功率斜变、外链汇入点与小节时基。
    setEngineMode: TrpgImmersiveMusicModule.setEngineMode,
    currentEngineMode: TrpgImmersiveMusicModule.currentEngineMode,
    rampBedLevel: TrpgImmersiveMusicModule.rampBedLevel,
    bedDuckLevel: TrpgImmersiveMusicModule.bedDuckLevel,
    externalInput: TrpgImmersiveMusicModule.externalInput,
    barTiming: TrpgImmersiveMusicModule.barTiming,
    audioClock: TrpgImmersiveMusicModule.audioClock,
    // 统一播放器接入点：暂停/恢复与「下一段」。
    currentPlan: TrpgImmersiveMusicModule.currentPlan,
    pause: TrpgImmersiveMusicModule.pause,
    resume: TrpgImmersiveMusicModule.resume,
    isPaused: TrpgImmersiveMusicModule.isPaused,
    togglePause: TrpgImmersiveMusicModule.togglePause,
    skipSection: TrpgImmersiveMusicModule.skipSection,
};
