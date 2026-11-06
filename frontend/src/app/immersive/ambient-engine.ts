/**
 * 程序化环境音（P2）：雨、风、车流、室内嘈杂、火焰、滴水、远雷。
 *
 * 设计约束：
 * - 零音频资源、零网络请求：全部由共享噪声缓冲 + 滤波 + 包络现场合成；
 * - 与配乐共用同一个 AudioContext、混音汇入点、限幅器与语音 ducking，
 *   但拥有独立音量与开关，配乐静音不会连带关掉环境音；
 * - 每条床层都有慢速强度起伏，细节事件按随机间隔排程（远雷、雨滴、车经过）；
 * - 换场景用等功率交叉淡化，不做硬切；
 * - 环境音高频与低频都给 BGM 让路：90 Hz 高通、1.1 kHz 轻挖、8.5 kHz 低通。
 *
 * 所有排程都基于 AudioContext 时钟的 lookahead，不依赖 requestAnimationFrame，
 * 因此页面卡顿不会让雨声忽快忽慢。
 */
namespace TrpgImmersiveAmbientModule {
    const ENABLED_KEY = "trpg_immersive_ambient_enabled";
    const VOLUME_KEY = "trpg_immersive_ambient_volume";
    const SCHEDULE_AHEAD_SECONDS = 1.8;
    const SCHEDULER_INTERVAL_MS = 260;
    const CROSSFADE_MS = 2200;
    const DEFAULT_VOLUME = 0.5;
    /** 配乐在响时环境音自动让位，避免两套声音抢同一频段。 */
    const MUSIC_DUCK = 0.62;

    type SceneId = "interior" | "street" | "storm" | "wind" | "dread" | "rain" | "fire" | "forest" | "void";

    interface LayerSpec {
        /** 噪声滤波类型；`tone` 层忽略。 */
        type: BiquadFilterType;
        frequency: number;
        q?: number;
        gain: number;
        pan: number;
        /** 慢速起伏深度（相对 gain 的比例）。 */
        swell?: number;
        /** 起伏频率，Hz。 */
        rate?: number;
        /** 固定音高层（drone），单位 MIDI。 */
        tone?: number;
    }

    type EventKind = "drop" | "drip" | "thunder" | "car" | "crackle" | "gust" | "creak" | "murmur";

    interface EventSpec {
        kind: EventKind;
        /** 两次事件之间的最小/最大间隔，秒。 */
        min: number;
        max: number;
        gain: number;
        pan?: number;
    }

    interface SceneDef {
        id: SceneId;
        label: string;
        bed: LayerSpec[];
        events: EventSpec[];
    }

    const SCENES: Record<SceneId, SceneDef> = {
        interior: {
            id: "interior",
            label: "室内",
            bed: [
                { type: "lowpass", frequency: 420, gain: 0.2, pan: 0, swell: 0.22, rate: 0.06 },
                { type: "lowpass", frequency: 220, gain: 0.13, pan: -0.35, swell: 0.18, rate: 0.045 },
                { type: "bandpass", frequency: 760, q: 0.8, gain: 0.045, pan: 0.42, swell: 0.5, rate: 0.09 },
            ],
            events: [
                { kind: "creak", min: 26, max: 64, gain: 0.05 },
                { kind: "drop", min: 22, max: 55, gain: 0.035 },
            ],
        },
        street: {
            id: "street",
            label: "街道",
            bed: [
                { type: "lowpass", frequency: 540, gain: 0.17, pan: 0, swell: 0.2, rate: 0.05 },
                { type: "bandpass", frequency: 960, q: 0.7, gain: 0.035, pan: 0.5, swell: 0.45, rate: 0.07 },
            ],
            events: [
                { kind: "car", min: 7, max: 19, gain: 0.07 },
                { kind: "drop", min: 28, max: 70, gain: 0.03 },
            ],
        },
        storm: {
            id: "storm",
            label: "暴雨",
            bed: [
                { type: "highpass", frequency: 850, gain: 0.15, pan: -0.25, swell: 0.18, rate: 0.08 },
                { type: "lowpass", frequency: 460, gain: 0.13, pan: 0.3, swell: 0.25, rate: 0.04 },
                { type: "bandpass", frequency: 2400, q: 0.6, gain: 0.05, pan: 0.55, swell: 0.4, rate: 0.11 },
            ],
            events: [
                { kind: "thunder", min: 38, max: 96, gain: 0.085 },
                { kind: "drop", min: 1.6, max: 4.6, gain: 0.05 },
            ],
        },
        wind: {
            id: "wind",
            label: "山风",
            bed: [
                { type: "lowpass", frequency: 380, gain: 0.19, pan: -0.15, swell: 0.35, rate: 0.035 },
                { type: "bandpass", frequency: 1180, q: 1.3, gain: 0.045, pan: 0.45, swell: 0.7, rate: 0.05 },
            ],
            events: [
                { kind: "gust", min: 11, max: 26, gain: 0.06 },
            ],
        },
        dread: {
            id: "dread",
            label: "压迫",
            bed: [
                { type: "lowpass", frequency: 150, gain: 0.16, pan: 0, swell: 0.3, rate: 0.03 },
                { type: "bandpass", frequency: 460, q: 0.9, gain: 0.035, pan: 0, swell: 0.55, rate: 0.045 },
                { type: "lowpass", frequency: 90, gain: 0.09, pan: 0, tone: 29 },
            ],
            events: [
                { kind: "drip", min: 14, max: 36, gain: 0.04 },
                { kind: "thunder", min: 70, max: 150, gain: 0.06 },
            ],
        },
        rain: {
            id: "rain",
            label: "细雨",
            bed: [
                { type: "highpass", frequency: 780, gain: 0.12, pan: -0.3, swell: 0.14, rate: 0.07 },
                { type: "lowpass", frequency: 520, gain: 0.11, pan: 0.28, swell: 0.2, rate: 0.045 },
            ],
            events: [
                { kind: "drop", min: 1.4, max: 3.8, gain: 0.04 },
                { kind: "thunder", min: 80, max: 180, gain: 0.045 },
            ],
        },
        fire: {
            id: "fire",
            label: "火焰",
            bed: [
                { type: "lowpass", frequency: 240, gain: 0.14, pan: 0, swell: 0.28, rate: 0.09 },
                { type: "bandpass", frequency: 1800, q: 0.5, gain: 0.03, pan: 0.2, swell: 0.6, rate: 0.12 },
            ],
            events: [
                { kind: "crackle", min: 0.25, max: 1.1, gain: 0.05 },
            ],
        },
        forest: {
            id: "forest",
            label: "林地",
            bed: [
                { type: "lowpass", frequency: 340, gain: 0.12, pan: 0, swell: 0.3, rate: 0.04 },
                { type: "bandpass", frequency: 1900, q: 0.7, gain: 0.04, pan: -0.45, swell: 0.65, rate: 0.06 },
            ],
            events: [
                { kind: "creak", min: 24, max: 58, gain: 0.045 },
                { kind: "drip", min: 9, max: 24, gain: 0.03 },
            ],
        },
        void: {
            id: "void",
            label: "留白",
            bed: [
                { type: "lowpass", frequency: 120, gain: 0.09, pan: 0, swell: 0.25, rate: 0.025 },
                { type: "bandpass", frequency: 3200, q: 0.5, gain: 0.018, pan: 0, swell: 0.5, rate: 0.04 },
            ],
            events: [
                { kind: "drip", min: 18, max: 46, gain: 0.03 },
                { kind: "thunder", min: 100, max: 200, gain: 0.04 },
            ],
        },
    };

    const KEYWORDS: Array<{ test: RegExp; scene: SceneId }> = [
        { test: /火|篝火|燃烧|熔炉|fire|flame|furnace|ember/i, scene: "fire" },
        { test: /森林|树林|丛林|forest|woods|jungle|grove/i, scene: "forest" },
        { test: /暴雨|雷|暴风|storm|thunder|downpour|tempest/i, scene: "storm" },
        { test: /雨|rain|drizzle|wet/i, scene: "rain" },
        { test: /风|山|海|wind|mountain|coast|cliff|sea/i, scene: "wind" },
        { test: /街|城|车|traffic|street|city|alley|road/i, scene: "street" },
        { test: /室内|房间|旅店|酒馆|图书|办公室|indoor|room|inn|tavern|library|office/i, scene: "interior" },
    ];

    const PROFILE_SCENE: Record<ImmersiveProfile, SceneId> = {
        daily: "interior",
        suspense: "street",
        unease: "wind",
        horror: "dread",
        urgency: "storm",
        sorrow: "rain",
        awe: "wind",
        aftermath: "rain",
    };

    interface Graph {
        ctx: AudioContext;
        input: GainNode;
        noise: AudioBuffer;
        quality: number;
    }

    let enabled = readEnabled();
    let volume = readVolume();
    let musicActive = false;
    let current: SceneBus | null = null;
    let currentScene: SceneId | null = null;
    /** 最近一次确定过的场景；stop() 后仍保留，供 resume() 重新拉起。 */
    let lastScene: SceneId | null = null;
    let pendingScene: SceneId | null = null;
    let masterGain: GainNode | null = null;
    let graphKey: AudioContext | null = null;
    let schedulerTimer: number | null = null;
    let gestureArmed = false;
    let scheduledEvents = 0;

    function readEnabled(): boolean {
        try {
            const raw = window.localStorage.getItem(ENABLED_KEY);
            return raw === null ? true : raw === "true";
        } catch {
            return true;
        }
    }

    function persistEnabled(value: boolean): void {
        try {
            window.localStorage.setItem(ENABLED_KEY, value ? "true" : "false");
        } catch {
            // 浏览器禁用存储时只在本次会话生效。
        }
    }

    function readVolume(): number {
        try {
            const raw = Number(window.localStorage.getItem(VOLUME_KEY));
            return Number.isFinite(raw) && raw > 0 ? Math.max(0.05, Math.min(1, raw)) : DEFAULT_VOLUME;
        } catch {
            return DEFAULT_VOLUME;
        }
    }

    function persistVolume(value: number): void {
        try {
            window.localStorage.setItem(VOLUME_KEY, String(value));
        } catch {
            // 同上。
        }
    }

    function clamp(value: number, min: number, max: number): number {
        if (!Number.isFinite(value)) return min;
        return Math.max(min, Math.min(max, value));
    }

    function levelTarget(): number {
        if (!enabled) return 0;
        return clamp(volume * (musicActive ? MUSIC_DUCK : 1), 0, 1);
    }

    /** 场景文本优先；没有明确场景词时回落到档位。 */
    function resolveScene(plan: ImmersiveMusicPlan): SceneId {
        const text = [
            plan.scene || "",
            plan.score?.scene || "",
            plan.reason || "",
            plan.analysis?.scene || "",
        ].join(" ");
        if (text.trim()) {
            for (const rule of KEYWORDS) {
                if (rule.test.test(text)) return rule.scene;
            }
        }
        return PROFILE_SCENE[plan.profile] ?? "interior";
    }

    function ensureGraph(): Graph | null {
        try {
            const graph = TrpgImmersiveMusicModule.audioGraph();
            if (!graph) return null;
            if (graphKey !== graph.ctx) {
                // 配乐 shutdown 会关闭旧上下文；换上下文时必须丢掉旧场景总线，
                // 否则场景判断会以为「还在播」，实际接在已关闭的图上。
                if (current) current.dispose();
                current = null;
                currentScene = null;
                masterGain = null;
                graphKey = graph.ctx;
                buildMaster(graph);
            }
            return graph;
        } catch {
            return null;
        }
    }

    /** 环境音总链：音量/让位 → 高通 → 中频让位 → 低通 → 共享混音汇入点。 */
    function buildMaster(graph: Graph): void {
        const previous = masterGain;
        const ctx = graph.ctx;
        const gain = ctx.createGain();
        gain.gain.value = levelTarget();
        const highpass = ctx.createBiquadFilter();
        highpass.type = "highpass";
        highpass.frequency.value = 90;
        const notch = ctx.createBiquadFilter();
        notch.type = "peaking";
        notch.frequency.value = 1100;
        notch.Q.value = 0.9;
        notch.gain.value = -3.5;
        const lowpass = ctx.createBiquadFilter();
        lowpass.type = "lowpass";
        lowpass.frequency.value = 8500;
        gain.connect(highpass);
        highpass.connect(notch);
        notch.connect(lowpass);
        lowpass.connect(graph.input);
        masterGain = gain;
        if (previous) {
            try {
                previous.disconnect();
            } catch {
                // 旧主链已断开。
            }
        }
    }

    function applyMasterLevel(seconds = 0.6): void {
        if (!masterGain || !graphKey) return;
        masterGain.gain.setTargetAtTime(levelTarget(), graphKey.currentTime, Math.max(0.05, seconds / 3));
    }

    class SceneBus {
        private readonly graph: Graph;
        private readonly def: SceneDef;
        private readonly gain: GainNode;
        private readonly nodes: AudioScheduledSourceNode[] = [];
        private readonly nextAt = new Map<EventSpec, number>();
        private disposed = false;

        constructor(graph: Graph, def: SceneDef) {
            this.graph = graph;
            this.def = def;
            this.gain = graph.ctx.createGain();
            this.gain.gain.value = 0;
            if (masterGain) this.gain.connect(masterGain);
            this.buildBed();
        }

        private get ctx(): AudioContext {
            return this.graph.ctx;
        }

        private get quality(): number {
            return this.graph.quality;
        }

        private buildBed(): void {
            const ctx = this.ctx;
            const now = ctx.currentTime + 0.05;
            let layers = this.def.bed;
            if (this.quality >= 2) layers = layers.slice(0, 1);
            else if (this.quality >= 1) layers = layers.slice(0, 2);
            for (const layer of layers) {
                const panner = ctx.createStereoPanner();
                panner.pan.value = clamp(layer.pan, -1, 1);
                panner.connect(this.gain);
                const level = ctx.createGain();
                // 基础增益直接写值，慢速起伏由 LFO 叠加到 AudioParam 上。
                level.gain.value = Math.max(0.0005, layer.gain);
                level.connect(panner);
                let source: AudioScheduledSourceNode;
                if (layer.tone !== undefined) {
                    const osc = ctx.createOscillator();
                    osc.type = "sine";
                    osc.frequency.value = TrpgImmersiveMusicVoices.midiToFreq(layer.tone);
                    source = osc;
                } else {
                    const buffer = ctx.createBufferSource();
                    buffer.buffer = this.graph.noise;
                    buffer.loop = true;
                    buffer.playbackRate.value = 0.9 + Math.random() * 0.2;
                    source = buffer;
                }
                const filter = ctx.createBiquadFilter();
                filter.type = layer.type;
                filter.frequency.value = clamp(layer.frequency, 30, 12000);
                filter.Q.value = clamp(layer.q ?? 0.7, 0.1, 12);
                source.connect(filter);
                filter.connect(level);
                if (layer.swell && this.quality <= 1) {
                    const lfo = ctx.createOscillator();
                    lfo.type = "sine";
                    lfo.frequency.value = clamp(layer.rate ?? 0.05, 0.01, 1);
                    const depth = ctx.createGain();
                    depth.gain.value = Math.max(0.0002, layer.gain * clamp(layer.swell, 0, 0.95));
                    lfo.connect(depth);
                    depth.connect(level.gain);
                    lfo.start(now + Math.random() * 0.6);
                    this.nodes.push(lfo);
                }
                if (source instanceof AudioBufferSourceNode) {
                    source.start(now, Math.random() * Math.max(0.1, this.graph.noise.duration - 0.6));
                } else {
                    source.start(now);
                }
                this.nodes.push(source);
            }
            if (this.quality >= 2) return;
            const nowSeconds = this.ctx.currentTime;
            for (const event of this.def.events) {
                this.nextAt.set(event, nowSeconds + event.min * (0.35 + Math.random() * 0.6));
            }
        }

        fadeTo(target: number, seconds: number): void {
            if (this.disposed) return;
            const now = this.ctx.currentTime;
            const value = target <= 0.001 ? 0.0001 : clamp(target, 0.0001, 1);
            try {
                this.gain.gain.cancelAndHoldAtTime(now);
            } catch {
                this.gain.gain.cancelScheduledValues(now);
            }
            const currentValue = Math.max(0.0001, Number(this.gain.gain.value) || 0.0001);
            const samples = 40;
            const curve = new Float32Array(new ArrayBuffer(samples * 4));
            const rising = value > currentValue;
            for (let index = 0; index < samples; index += 1) {
                const progress = index / (samples - 1);
                const shape = rising ? Math.sin((progress * Math.PI) / 2) : Math.cos((progress * Math.PI) / 2);
                curve[index] = Math.max(0.0001, currentValue + (value - currentValue) * shape);
            }
            this.gain.gain.setValueAtTime(currentValue, now);
            this.gain.gain.setValueCurveAtTime(curve, now, Math.max(0.2, seconds));
            if (value <= 0.001) {
                window.setTimeout(() => this.dispose(), Math.max(0.2, seconds) * 1000 + 350);
            }
        }

        /** 在 until 之前把所有随机细节事件排好；迟到的窗口一次性补齐，不堆到同一时刻。 */
        scheduleUntil(until: number): void {
            if (this.disposed || this.quality >= 2) return;
            const now = this.ctx.currentTime;
            const scale = this.quality >= 1 ? 1.8 : 1;
            for (const event of this.def.events) {
                let at = this.nextAt.get(event) ?? now;
                if (at < now - 0.5) at = now + 0.05;
                let guard = 0;
                while (at < until && guard < 4) {
                    this.spawn(event, at);
                    const gap = (event.min + Math.random() * Math.max(0.01, event.max - event.min)) * scale;
                    at += Math.max(0.12, gap);
                    guard += 1;
                }
                this.nextAt.set(event, at);
            }
        }

        private noiseSource(): AudioBufferSourceNode {
            const source = this.ctx.createBufferSource();
            source.buffer = this.graph.noise;
            source.loop = true;
            return source;
        }

        private spawn(event: EventSpec, at: number): void {
            scheduledEvents += 1;
            switch (event.kind) {
                case "drop": this.spawnDrop(event, at); break;
                case "drip": this.spawnDrip(event, at); break;
                case "thunder": this.spawnThunder(event, at); break;
                case "car": this.spawnCar(event, at); break;
                case "crackle": this.spawnCrackle(event, at); break;
                case "gust": this.spawnGust(event, at); break;
                case "creak": this.spawnCreak(event, at); break;
                default: this.spawnMurmur(event, at); break;
            }
        }

        private spawnDrop(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.frequency.value = 2400 + Math.random() * 3800;
            filter.Q.value = 1.4;
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.6 - 0.8;
            const env = ctx.createGain();
            const level = event.gain * (0.35 + Math.random() * 0.85);
            const decay = 0.05 + Math.random() * 0.16;
            env.gain.setValueAtTime(0.0001, at);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0002, level), at + 0.006);
            env.gain.exponentialRampToValueAtTime(0.0001, at + decay);
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            panner.connect(this.gain);
            source.start(at, Math.random() * 1.4);
            source.stop(at + decay + 0.03);
        }

        /** 滴水：短正弦下滑 + 一点高频噪声，和雨滴区分开。 */
        private spawnDrip(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.4 - 0.7;
            panner.connect(this.gain);
            const osc = ctx.createOscillator();
            osc.type = "sine";
            const startFreq = 900 + Math.random() * 1100;
            osc.frequency.setValueAtTime(startFreq, at);
            osc.frequency.exponentialRampToValueAtTime(startFreq * 0.55, at + 0.12);
            const env = ctx.createGain();
            const level = event.gain * (0.5 + Math.random() * 0.7);
            env.gain.setValueAtTime(0.0001, at);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0002, level), at + 0.005);
            env.gain.exponentialRampToValueAtTime(0.0001, at + 0.28);
            osc.connect(env);
            env.connect(panner);
            osc.start(at);
            osc.stop(at + 0.32);
        }

        /** 远雷：低频噪声的慢起慢落 + 一个更低的次声正弦。 */
        private spawnThunder(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.1 - 0.55;
            panner.connect(this.gain);
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "lowpass";
            filter.frequency.value = 110 + Math.random() * 70;
            filter.Q.value = 0.8;
            const env = ctx.createGain();
            const length = 2.6 + Math.random() * 2.2;
            const level = event.gain * (0.6 + Math.random() * 0.6);
            env.gain.setValueAtTime(0.0001, at);
            env.gain.linearRampToValueAtTime(Math.max(0.0002, level), at + 0.45 + Math.random() * 0.5);
            env.gain.exponentialRampToValueAtTime(0.0001, at + length);
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            source.start(at, Math.random() * 1.2);
            source.stop(at + length + 0.05);
            const sub = ctx.createOscillator();
            sub.type = "sine";
            sub.frequency.value = 30 + Math.random() * 8;
            const subEnv = ctx.createGain();
            subEnv.gain.setValueAtTime(0.0001, at);
            subEnv.gain.linearRampToValueAtTime(Math.max(0.0002, level * 0.45), at + 0.6);
            subEnv.gain.exponentialRampToValueAtTime(0.0001, at + length * 0.9);
            sub.connect(subEnv);
            subEnv.connect(panner);
            sub.start(at);
            sub.stop(at + length);
        }

        /** 车经过：带通中心先升后降、声像从左到右，是「远近变化」最省的做法。 */
        private spawnCar(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const panner = ctx.createStereoPanner();
            const duration = 4 + Math.random() * 3;
            const fromLeft = Math.random() < 0.5;
            panner.pan.setValueAtTime(fromLeft ? -0.85 : 0.85, at);
            panner.pan.linearRampToValueAtTime(fromLeft ? 0.85 : -0.85, at + duration);
            panner.connect(this.gain);
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.Q.value = 0.9;
            filter.frequency.setValueAtTime(260, at);
            filter.frequency.linearRampToValueAtTime(800, at + duration * 0.45);
            filter.frequency.linearRampToValueAtTime(250, at + duration);
            const env = ctx.createGain();
            const level = event.gain * (0.5 + Math.random() * 0.7);
            env.gain.setValueAtTime(0.0001, at);
            env.gain.linearRampToValueAtTime(Math.max(0.0002, level), at + duration * 0.42);
            env.gain.linearRampToValueAtTime(0.0001, at + duration);
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            source.start(at, Math.random());
            source.stop(at + duration + 0.05);
        }

        private spawnCrackle(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.frequency.value = 900 + Math.random() * 2600;
            filter.Q.value = 0.8;
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.2 - 0.6;
            const env = ctx.createGain();
            const decay = 0.02 + Math.random() * 0.06;
            env.gain.setValueAtTime(0.0001, at);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0002, event.gain * (0.4 + Math.random())), at + 0.004);
            env.gain.exponentialRampToValueAtTime(0.0001, at + decay);
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            panner.connect(this.gain);
            source.start(at, Math.random() * 1.5);
            source.stop(at + decay + 0.02);
        }

        private spawnGust(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const duration = 2.4 + Math.random() * 2.2;
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.Q.value = 0.6;
            filter.frequency.setValueAtTime(320, at);
            filter.frequency.linearRampToValueAtTime(780, at + duration * 0.5);
            filter.frequency.linearRampToValueAtTime(300, at + duration);
            const panner = ctx.createStereoPanner();
            const fromLeft = Math.random() < 0.5;
            panner.pan.setValueAtTime(fromLeft ? -0.6 : 0.6, at);
            panner.pan.linearRampToValueAtTime(fromLeft ? 0.6 : -0.6, at + duration);
            panner.connect(this.gain);
            const env = ctx.createGain();
            env.gain.setValueAtTime(0.0001, at);
            env.gain.linearRampToValueAtTime(Math.max(0.0002, event.gain * (0.5 + Math.random() * 0.8)), at + duration * 0.45);
            env.gain.linearRampToValueAtTime(0.0001, at + duration);
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            source.start(at, Math.random());
            source.stop(at + duration + 0.05);
        }

        private spawnCreak(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.Q.value = 7;
            filter.frequency.setValueAtTime(190, at);
            filter.frequency.linearRampToValueAtTime(120, at + 0.7);
            const env = ctx.createGain();
            env.gain.setValueAtTime(0.0001, at);
            env.gain.linearRampToValueAtTime(Math.max(0.0002, event.gain), at + 0.18);
            env.gain.exponentialRampToValueAtTime(0.0001, at + 0.8);
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.2 - 0.6;
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            panner.connect(this.gain);
            source.start(at, Math.random());
            source.stop(at + 0.85);
        }

        private spawnMurmur(event: EventSpec, at: number): void {
            const ctx = this.ctx;
            const duration = 0.5 + Math.random() * 0.9;
            const source = this.noiseSource();
            const filter = ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.frequency.value = 380 + Math.random() * 520;
            filter.Q.value = 1.6;
            const env = ctx.createGain();
            env.gain.setValueAtTime(0.0001, at);
            env.gain.linearRampToValueAtTime(Math.max(0.0002, event.gain), at + duration * 0.4);
            env.gain.linearRampToValueAtTime(0.0001, at + duration);
            const panner = ctx.createStereoPanner();
            panner.pan.value = Math.random() * 1.4 - 0.7;
            source.connect(filter);
            filter.connect(env);
            env.connect(panner);
            panner.connect(this.gain);
            source.start(at, Math.random());
            source.stop(at + duration + 0.03);
        }

        dispose(): void {
            if (this.disposed) return;
            this.disposed = true;
            for (const node of this.nodes) {
                try {
                    node.stop();
                } catch {
                    // 已停止。
                }
                try {
                    node.disconnect();
                } catch {
                    // 已断开。
                }
            }
            this.nodes.length = 0;
            try {
                this.gain.disconnect();
            } catch {
                // 已断开。
            }
        }

        get isDisposed(): boolean {
            return this.disposed;
        }

        get layers(): string[] {
            return this.def.bed.map((layer) => layer.tone !== undefined ? `tone:${layer.tone}` : `${layer.type}:${Math.round(layer.frequency)}`);
        }
    }

    function switchScene(scene: SceneId, fadeSeconds = CROSSFADE_MS / 1000): void {
        const graph = ensureGraph();
        if (!graph || !masterGain) return;
        if (graph.ctx.state === "suspended") {
            pendingScene = scene;
            armGesture();
            return;
        }
        if (current && currentScene === scene) return;
        const next = new SceneBus(graph, SCENES[scene]);
        next.fadeTo(1, fadeSeconds);
        if (current) current.fadeTo(0, fadeSeconds);
        current = next;
        currentScene = scene;
        lastScene = scene;
        pendingScene = null;
        startScheduler();
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

    function tick(): void {
        if (!enabled) return;
        if (!graphKey) return;
        if (graphKey.state !== "running") return;
        if (!current) {
            stopScheduler();
            return;
        }
        current.scheduleUntil(graphKey.currentTime + SCHEDULE_AHEAD_SECONDS);
    }

    function armGesture(): void {
        if (gestureArmed) return;
        gestureArmed = true;
        const handler = (): void => {
            gestureArmed = false;
            document.removeEventListener("pointerdown", handler);
            document.removeEventListener("keydown", handler);
            const graph = ensureGraph();
            if (!graph) return;
            void graph.ctx.resume().then(() => {
                if (graph.ctx.state !== "running") return;
                const scene = pendingScene ?? currentScene ?? lastScene;
                if (scene && enabled) {
                    if (current) {
                        current.dispose();
                        current = null;
                        currentScene = null;
                    }
                    switchScene(scene, 0.4);
                }
                applyMasterLevel(0.4);
            }).catch(() => undefined);
        };
        document.addEventListener("pointerdown", handler, { passive: true });
        document.addEventListener("keydown", handler);
    }

    export function apply(plan: ImmersiveMusicPlan): void {
        if (!plan || !plan.profile) return;
        const scene = resolveScene(plan);
        lastScene = scene;
        pendingScene = scene;
        if (!enabled) return;
        if (!ensureGraph()) return;
        if (graphKey && graphKey.state === "suspended") {
            armGesture();
            return;
        }
        switchScene(scene);
        applyMasterLevel();
    }

    export function stop(fadeMs = 700): void {
        const seconds = Math.max(0.15, fadeMs / 1000);
        if (current) current.fadeTo(0, seconds);
        current = null;
        currentScene = null;
        pendingScene = null;
        applyMasterLevel(seconds);
        stopScheduler();
    }

    /**
     * 重新拉起最近一个场景。
     * 用于「仅 BGM」模式：配乐引擎 shutdown 时会顺带调用 stop()，
     * 但环境音是独立开关，需要在共享上下文保留后恢复播放。
     */
    export function resume(): void {
        if (!enabled) return;
        const scene = pendingScene ?? currentScene ?? lastScene;
        if (!scene) return;
        switchScene(scene, 0.8);
        applyMasterLevel(0.8);
    }

    export function setEnabled(value: boolean): void {
        enabled = Boolean(value);
        persistEnabled(enabled);
        applyMasterLevel();
        if (!enabled) {
            if (current) {
                current.fadeTo(0, 0.9);
                // 保留 currentScene 以便重新开启时恢复；但必须丢掉总线引用，
                // 否则再次开启时会被「同场景」判断拦住，听到的是一条已经淡到 0 的旧总线。
                current = null;
            }
            stopScheduler();
            return;
        }
        const scene = pendingScene ?? currentScene ?? lastScene;
        if (scene) switchScene(scene, 1.2);
    }

    export function isEnabled(): boolean {
        return enabled;
    }

    export function setVolume(value: number): void {
        volume = clamp(Number(value) || 0, 0.05, 1);
        persistVolume(volume);
        applyMasterLevel(0.4);
    }

    export function getVolume(): number {
        return volume;
    }

    export function setMusicActive(active: boolean): void {
        musicActive = Boolean(active);
        applyMasterLevel(1.2);
    }

    export function isRunning(): boolean {
        return Boolean(current) && enabled && graphKey !== null && graphKey.state === "running";
    }

    export function getScene(): string | null {
        return currentScene;
    }

    export function diagnostics(): ImmersiveAmbientDiagnostics {
        return {
            context: graphKey ? graphKey.state : "none",
            enabled,
            running: isRunning(),
            scene: currentScene,
            volume,
            music_active: musicActive,
            layers: current ? current.layers : [],
            scheduled_events: scheduledEvents,
        };
    }
}

window.TrpgImmersiveAmbient = {
    apply: TrpgImmersiveAmbientModule.apply,
    stop: TrpgImmersiveAmbientModule.stop,
    setEnabled: TrpgImmersiveAmbientModule.setEnabled,
    isEnabled: TrpgImmersiveAmbientModule.isEnabled,
    setVolume: TrpgImmersiveAmbientModule.setVolume,
    getVolume: TrpgImmersiveAmbientModule.getVolume,
    isRunning: TrpgImmersiveAmbientModule.isRunning,
    getScene: TrpgImmersiveAmbientModule.getScene,
    resume: TrpgImmersiveAmbientModule.resume,
    setMusicActive: TrpgImmersiveAmbientModule.setMusicActive,
    diagnostics: TrpgImmersiveAmbientModule.diagnostics,
};
