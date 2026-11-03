/**
 * 实时程序化配乐引擎（Web Audio）。
 *
 * - 服务端只下发导演指令（profile / intensity / section / seed），本引擎按同一份种子
 *   现场合成乐句：同一房间、同一段落永远得到同一串走向，便于复现调试。
 * - 每个档位有独立的调式、和弦走向、配器组合与明暗度；每种强度再叠加节奏密度、
 *   音量与不协和程度。段落推进时换和弦走向与配器，避免「一直一个调」。
 * - 切换使用 A/B 总线交叉淡入淡出：旧总线立即停止调度并淡出，新总线淡入，
 *   不会出现突兀的断音。
 * - 播放时会按设置压低房间 BGM（ducking），静音或停止时恢复。
 */
namespace TrpgImmersiveMusicModule {
    const MUTE_KEY = "trpg_immersive_music_muted";
    const SCHEDULE_AHEAD_SECONDS = 2.2;
    const SCHEDULER_INTERVAL_MS = 250;

    interface IntensitySpec {
        /** 节奏/配器密度倍率。 */
        density: number;
        /** 总线音量倍率。 */
        gain: number;
        /** 速度倍率。 */
        tempo: number;
    }

    interface ProfileSpec {
        tempo: number;
        root: number;
        scale: number[];
        /** 每个小节的和弦音程（相对根音，允许超过 12）。 */
        progression: number[][];
        pad: boolean;
        bass: boolean;
        arp: boolean;
        pulse: boolean;
        bells: boolean;
        drone: boolean;
        reverb: number;
        brightness: number;
    }

    const INTENSITY_SPECS: Record<ImmersiveIntensity, IntensitySpec> = {
        low: { density: 0.55, gain: 0.82, tempo: 0.9 },
        medium: { density: 0.8, gain: 1, tempo: 1 },
        high: { density: 1, gain: 1.16, tempo: 1.12 },
    };

    // 每个档位的「性格」：调式、和弦、配器、混响与滤波器开度。
    const PROFILE_SPECS: Record<ImmersiveProfile, ProfileSpec> = {
        daily: {
            tempo: 78, root: 60, scale: [0, 2, 4, 7, 9],
            progression: [[0, 4, 7], [0, 4, 7], [5, 9, 12], [0, 4, 7]],
            pad: true, bass: true, arp: true, pulse: false, bells: false, drone: false,
            reverb: 0.34, brightness: 980,
        },
        suspense: {
            tempo: 64, root: 55, scale: [0, 3, 5, 7, 10],
            progression: [[0, 3, 7], [0, 3, 7], [6, 9, 12], [6, 8, 11]],
            pad: true, bass: true, arp: true, pulse: false, bells: false, drone: true,
            reverb: 0.52, brightness: 620,
        },
        unease: {
            tempo: 56, root: 52, scale: [0, 1, 3, 5, 6, 8, 10],
            progression: [[0, 1, 6], [0, 3, 6], [1, 6, 8], [0, 1, 5]],
            pad: true, bass: true, arp: false, pulse: false, bells: false, drone: true,
            reverb: 0.62, brightness: 520,
        },
        horror: {
            tempo: 46, root: 45, scale: [0, 1, 4, 6, 9],
            progression: [[0, 1, 4], [0, 2, 5], [0, 1, 6], [0, 3, 6]],
            pad: true, bass: true, arp: false, pulse: true, bells: false, drone: true,
            reverb: 0.76, brightness: 430,
        },
        urgency: {
            tempo: 126, root: 58, scale: [0, 2, 4, 5, 7, 9],
            progression: [[0, 4, 7], [5, 9, 11], [0, 4, 7], [3, 7, 10]],
            pad: false, bass: true, arp: true, pulse: true, bells: false, drone: false,
            reverb: 0.2, brightness: 1180,
        },
        sorrow: {
            tempo: 62, root: 57, scale: [0, 2, 3, 7, 9, 10],
            progression: [[0, 3, 7], [0, 3, 10], [5, 8, 12], [0, 3, 7]],
            pad: true, bass: true, arp: false, pulse: false, bells: true, drone: false,
            reverb: 0.56, brightness: 760,
        },
        awe: {
            tempo: 70, root: 50, scale: [0, 2, 4, 7, 9, 11],
            progression: [[0, 4, 11], [0, 5, 9], [0, 4, 7], [7, 11, 14]],
            pad: true, bass: true, arp: true, pulse: false, bells: true, drone: false,
            reverb: 0.66, brightness: 1020,
        },
        aftermath: {
            tempo: 58, root: 54, scale: [0, 2, 3, 7, 10],
            progression: [[0, 2, 7], [0, 3, 6], [0, 2, 5], [0, 4, 7]],
            pad: true, bass: true, arp: false, pulse: false, bells: false, drone: true,
            reverb: 0.5, brightness: 640,
        },
    };

    // 导演乐谱的调式 → 音阶音程（相对主音）。白名单与后端 `director_schema.DIRECTOR_MODES` 对齐。
    const MODE_SCALES: Record<string, number[]> = {
        major: [0, 2, 4, 7, 9],
        minor: [0, 3, 5, 7, 10],
        dorian: [0, 2, 3, 7, 9],
        phrygian: [0, 1, 3, 7, 8],
        lydian: [0, 2, 4, 7, 11],
        mixolydian: [0, 2, 4, 7, 10],
        harmonic_minor: [0, 3, 5, 7, 11],
        whole_tone: [0, 2, 4, 6, 8, 10],
        chromatic: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    };

    /**
     * 把导演乐谱转成引擎可用的 ProfileSpec；任何缺失/越界都返回 null，
     * 由调用方回落到 ``PROFILE_SPECS`` 规则合成（绝不把半截乐谱送进播放）。
     */
    function scoreSpec(plan: ImmersiveMusicPlan): ProfileSpec | null {
        const score = plan.score;
        if (!score || !Array.isArray(score.chords) || score.chords.length < 2) return null;
        const tempo = Number(score.tempo);
        if (!Number.isFinite(tempo) || tempo < 30 || tempo > 200) return null;
        const firstRoot = score.chords[0]?.root;
        if (!Number.isInteger(firstRoot)) return null;
        const base = PROFILE_SPECS[plan.profile] || PROFILE_SPECS.daily;
        const progression = score.chords.map((chord) => {
            if (!chord || !Number.isInteger(chord.root) || !Array.isArray(chord.intervals) || chord.intervals.length === 0) {
                return [0, 4, 7];
            }
            const offset = chord.root - (firstRoot as number);
            return chord.intervals.map((interval) => offset + Number(interval || 0));
        });
        const instruments = new Set(Array.isArray(score.instrumentation) ? score.instrumentation : []);
        return {
            tempo,
            root: 48 + (firstRoot as number),
            scale: MODE_SCALES[score.mode] || base.scale,
            progression,
            pad: instruments.has("pad") || instruments.has("strings") || instruments.has("choir"),
            bass: instruments.has("bass"),
            arp: instruments.has("arp"),
            pulse: instruments.has("pulse") || instruments.has("noise"),
            bells: instruments.has("bells"),
            drone: instruments.has("drone"),
            reverb: base.reverb,
            brightness: base.brightness,
        };
    }

    function directorSectionIndex(plan: ImmersiveMusicPlan): number {
        const sections = plan.score?.sections;
        if (!Array.isArray(sections) || !sections.length) return 0;
        const raw = Number(plan.director?.section_index ?? 0);
        if (!Number.isFinite(raw)) return 0;
        return Math.max(0, Math.min(Math.trunc(raw), sections.length - 1));
    }

    let context: AudioContext | null = null;
    let master: GainNode | null = null;
    let masterReverb: ConvolverNode | null = null;
    let noiseBuffer: AudioBuffer | null = null;
    let schedulerTimer: number | null = null;
    let liveBuses: MusicBus[] = [];
    let activePlan: ImmersiveMusicPlan | null = null;
    let activeSignature = "";
    let queuedPlan: ImmersiveMusicPlan | null = null;
    let muted = readMuted();
    let gestureArmed = false;

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

    function normalizePlan(plan: ImmersiveMusicPlan): ImmersiveMusicPlan {
        const profile = (PROFILE_SPECS[plan.profile] ? plan.profile : "daily") as ImmersiveProfile;
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

    function levelFor(plan: ImmersiveMusicPlan): number {
        const volume = clamp(Number(plan.volume ?? 0.35), 0, 1);
        const dynamics = plan.score?.dynamics;
        if (Array.isArray(dynamics) && dynamics.length) {
            const index = Math.min(directorSectionIndex(plan), dynamics.length - 1);
            const level = Number(dynamics[index]);
            if (Number.isFinite(level)) {
                // 导演动态曲线：0 接近留白，1 接近满配，仍受站点音量上限约束。
                return clamp(volume * (0.55 + clamp(level, 0, 1) * 0.6), 0.02, volume);
            }
        }
        const intensity = INTENSITY_SPECS[plan.intensity];
        return volume * (intensity ? intensity.gain : 1);
    }

    function midiToHz(note: number): number {
        return 440 * Math.pow(2, (note - 69) / 12);
    }

    function hashString(value: string): number {
        let hash = 2166136261;
        for (let index = 0; index < value.length; index += 1) {
            hash ^= value.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return hash >>> 0;
    }

    /** 可复现伪随机：同一 seed 得到同一串 0~1 序列。 */
    function createRandom(seed: number): () => number {
        let state = seed >>> 0;
        return () => {
            state = (state + 0x6d2b79f5) >>> 0;
            let t = state;
            t = Math.imul(t ^ (t >>> 15), t | 1);
            t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
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

    function createReverbBuffer(ctx: AudioContext, seconds: number): AudioBuffer {
        const length = Math.max(1, Math.floor(ctx.sampleRate * seconds));
        const buffer = ctx.createBuffer(2, length, ctx.sampleRate);
        for (let channel = 0; channel < 2; channel += 1) {
            const data = buffer.getChannelData(channel);
            for (let index = 0; index < length; index += 1) {
                const decay = Math.pow(1 - index / length, 2.6);
                data[index] = (Math.random() * 2 - 1) * decay;
            }
        }
        return buffer;
    }

    class MusicBus {
        readonly plan: ImmersiveMusicPlan;
        readonly spec: ProfileSpec;
        readonly barDuration: number;
        readonly targetLevel: number;
        readonly input: GainNode;
        readonly filter: BiquadFilterNode;
        readonly gain: GainNode;
        readonly send: GainNode;
        nextBarTime = 0;
        barIndex = 0;
        expired = false;
        disposed = false;

        private readonly ctx: AudioContext;
        private readonly noise: AudioBuffer;
        private readonly droneNodes: OscillatorNode[] = [];
        private readonly droneGains: GainNode[] = [];
        private currentLevel = 0;
        private disposeTimer: number | null = null;

        constructor(
            ctx: AudioContext,
            plan: ImmersiveMusicPlan,
            spec: ProfileSpec,
            masterNode: GainNode,
            convolver: ConvolverNode,
            noise: AudioBuffer,
        ) {
            this.ctx = ctx;
            this.plan = plan;
            this.spec = spec;
            this.noise = noise;
            const intensity = INTENSITY_SPECS[plan.intensity] || INTENSITY_SPECS.medium;
            // 导演乐谱自带 tempo，不再叠加强度倍率，避免「谱面 84 BPM 实际播成 94」。
            const tempoScale = plan.score ? 1 : intensity.tempo;
            this.barDuration = 60 / (spec.tempo * tempoScale);
            this.targetLevel = levelFor(plan);

            this.input = ctx.createGain();
            this.input.gain.value = 1;
            this.filter = ctx.createBiquadFilter();
            this.filter.type = "lowpass";
            this.filter.frequency.value = spec.brightness;
            this.filter.Q.value = 0.65;
            this.gain = ctx.createGain();
            this.gain.gain.value = 0;
            this.send = ctx.createGain();
            this.send.gain.value = spec.reverb;

            this.input.connect(this.filter);
            this.filter.connect(this.gain);
            this.gain.connect(masterNode);
            this.filter.connect(this.send);
            this.send.connect(convolver);
        }

        start(at: number, fadeMs: number): void {
            this.currentLevel = 0;
            this.gain.gain.setValueAtTime(0.0001, at);
            this.gain.gain.linearRampToValueAtTime(Math.max(0.001, this.targetLevel), at + fadeMs / 1000);
            this.currentLevel = this.targetLevel;
            this.nextBarTime = at + 0.02;
            this.startDrone(at);
        }

        setVolume(level: number, fadeSeconds: number): void {
            const now = this.ctx.currentTime;
            this.gain.gain.cancelScheduledValues(now);
            this.gain.gain.setValueAtTime(Math.max(0.0001, this.currentLevel), now);
            this.gain.gain.linearRampToValueAtTime(Math.max(0.0001, level), now + Math.max(0.2, fadeSeconds));
            this.currentLevel = level;
        }

        fadeOut(now: number, fadeMs: number): void {
            if (this.expired || this.disposed) return;
            this.expired = true;
            const seconds = Math.max(0.2, fadeMs / 1000);
            this.gain.gain.cancelScheduledValues(now);
            this.gain.gain.setValueAtTime(Math.max(0.0001, this.currentLevel), now);
            this.gain.gain.linearRampToValueAtTime(0.0001, now + seconds);
            this.currentLevel = 0;
            this.disposeTimer = window.setTimeout(() => this.dispose(), seconds * 1000 + 260);
        }

        scheduleUntil(until: number): void {
            if (this.disposed || this.expired) return;
            let guard = 0;
            while (this.nextBarTime < until && guard < 64) {
                this.scheduleBar(this.nextBarTime);
                this.nextBarTime += this.barDuration;
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
            for (const gain of this.droneGains) {
                try {
                    gain.disconnect();
                } catch {
                    // 已断开的节点无需重复处理。
                }
            }
            for (const osc of this.droneNodes) {
                try {
                    osc.stop();
                } catch {
                    // 已停止的振荡器无需重复处理。
                }
                try {
                    osc.disconnect();
                } catch {
                    // 同上。
                }
            }
            for (const node of [this.input, this.filter, this.gain, this.send]) {
                try {
                    node.disconnect();
                } catch {
                    // 同上。
                }
            }
        }

        private startDrone(at: number): void {
            if (!this.spec.drone) return;
            const voices = this.plan.intensity === "high" ? 2 : 1;
            for (let index = 0; index < voices; index += 1) {
                const osc = this.ctx.createOscillator();
                osc.type = "sawtooth";
                osc.frequency.value = midiToHz(this.spec.root - 12 + index * 0.7);
                const gain = this.ctx.createGain();
                gain.gain.setValueAtTime(0.0001, at);
                gain.gain.linearRampToValueAtTime(index === 0 ? 0.045 : 0.03, at + 1.4);
                osc.connect(gain);
                gain.connect(this.input);
                osc.start(at);
                this.droneNodes.push(osc);
                this.droneGains.push(gain);
            }
        }

        private playTone(
            type: OscillatorType,
            frequency: number,
            start: number,
            duration: number,
            peak: number,
            detune = 0,
        ): void {
            const osc = this.ctx.createOscillator();
            osc.type = type;
            osc.frequency.setValueAtTime(Math.max(20, frequency), start);
            if (detune) osc.detune.setValueAtTime(detune, start);
            const env = this.ctx.createGain();
            env.gain.setValueAtTime(0.0001, start);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0002, peak), start + Math.min(0.14, duration * 0.3));
            env.gain.exponentialRampToValueAtTime(0.0001, start + Math.max(0.12, duration));
            osc.connect(env);
            env.connect(this.input);
            osc.start(start);
            osc.stop(start + Math.max(0.1, duration) + 0.04);
        }

        private playNoise(start: number, duration: number, peak: number, frequency: number): void {
            const source = this.ctx.createBufferSource();
            source.buffer = this.noise;
            source.loop = true;
            const filter = this.ctx.createBiquadFilter();
            filter.type = "bandpass";
            filter.frequency.value = frequency;
            filter.Q.value = 0.9;
            const env = this.ctx.createGain();
            env.gain.setValueAtTime(0.0001, start);
            env.gain.exponentialRampToValueAtTime(Math.max(0.0002, peak), start + 0.015);
            env.gain.exponentialRampToValueAtTime(0.0001, start + duration);
            source.connect(filter);
            filter.connect(env);
            env.connect(this.input);
            source.start(start);
            source.stop(start + duration + 0.03);
        }

        private scheduleBar(start: number): void {
            const intensity = INTENSITY_SPECS[this.plan.intensity] || INTENSITY_SPECS.medium;
            const random = createRandom(hashString(`${this.plan.seed}|${this.barIndex}|${this.plan.motif || ""}`));
            const chord = this.spec.progression[this.barIndex % this.spec.progression.length] || this.spec.progression[0] || [0];
            const root = this.spec.root;
            const barDuration = this.barDuration;

            if (this.spec.pad) {
                for (let index = 0; index < chord.length; index += 1) {
                    const detune = (random() * 2 - 1) * 9;
                    this.playTone(
                        "triangle",
                        midiToHz(root + (chord[index] || 0)),
                        start + 0.01,
                        barDuration * 1.12,
                        0.05 * intensity.gain,
                        detune,
                    );
                }
            }

            if (this.spec.bass && (intensity.density >= 0.8 || this.barIndex % 2 === 0)) {
                this.playTone(
                    "sine",
                    midiToHz(root - 12 + (chord[0] || 0)),
                    start,
                    barDuration * 0.92,
                    0.11 * intensity.gain,
                );
            }

            if (this.spec.arp && random() > 0.12) {
                const count = intensity.density >= 0.95 ? 5 : intensity.density >= 0.7 ? 3 : 2;
                const step = barDuration / count;
                const motif = this.plan.score?.motif;
                for (let index = 0; index < count; index += 1) {
                    const scaleDegree = this.spec.scale[(index * 2 + this.barIndex) % this.spec.scale.length] || 0;
                    const motifNote = Array.isArray(motif) && motif.length
                        ? Number(motif[(index + this.barIndex) % motif.length])
                        : NaN;
                    const note = Number.isFinite(motifNote)
                        ? root + motifNote + 12
                        : root + (chord[index % chord.length] || 0) + (index >= chord.length ? 12 : 0);
                    const finalNote = Number.isFinite(motifNote)
                        ? note
                        : note + Math.round(scaleDegree / 4) * 2;
                    this.playTone(
                        "triangle",
                        midiToHz(finalNote),
                        start + index * step + random() * 0.04,
                        step * 0.82,
                        0.075 * intensity.gain,
                    );
                }
            }

            if (this.spec.bells && random() < 0.55) {
                const note = root + (chord[Math.floor(random() * chord.length)] || 0) + 12;
                this.playTone(
                    "sine",
                    midiToHz(note),
                    start + random() * barDuration * 0.7,
                    1.35,
                    0.045 * intensity.gain,
                );
            }

            if (this.spec.pulse) {
                const count = intensity.density >= 0.95 ? 4 : 2;
                const step = barDuration / count;
                for (let index = 0; index < count; index += 1) {
                    this.playNoise(
                        start + index * step,
                       Math.min(0.16, step * 0.6),
                        0.1 * intensity.gain,
                        160 + random() * 320,
                    );
                }
            }
        }
    }

    function ensureContext(): AudioContext | null {
        if (context) return context;
        const ctor = window.AudioContext
            || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!ctor) return null;
        try {
            context = new ctor();
            master = context.createGain();
            master.gain.value = muted ? 0 : 1;
            master.connect(context.destination);
            masterReverb = context.createConvolver();
            masterReverb.buffer = createReverbBuffer(context, 1.6);
            const reverbGain = context.createGain();
            reverbGain.gain.value = 0.55;
            masterReverb.connect(reverbGain);
            reverbGain.connect(master);
            noiseBuffer = createNoiseBuffer(context, 2);
            return context;
        } catch (error) {
            console.warn("程序化配乐初始化失败", error);
            context = null;
            return null;
        }
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

    function switchBus(plan: ImmersiveMusicPlan): void {
        const ctx = ensureContext();
        if (!ctx || !master || !masterReverb || !noiseBuffer) return;
        const spec = scoreSpec(plan) || PROFILE_SPECS[plan.profile] || PROFILE_SPECS.daily;
        const bus = new MusicBus(ctx, plan, spec, master, masterReverb, noiseBuffer);
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
        liveBuses = liveBuses.filter((bus) => !bus.disposed);
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

    export function apply(plan: ImmersiveMusicPlan): void {
        if (!plan || !plan.profile) return;
        const normalized = normalizePlan(plan);
        activePlan = normalized;
        const signature = [
            normalized.profile,
            normalized.intensity,
            normalized.seed,
            normalized.director?.fingerprint || "",
            normalized.director?.section_index ?? "",
            normalized.score?.mode || "",
        ].join("|");
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
                        switchBus(next);
                    }
                }
            }).catch(() => armGesture());
            armGesture();
            return;
        }
        if (activeSignature === signature && liveBuses.length) {
            const bus = liveBuses[liveBuses.length - 1];
            if (bus && !bus.expired) bus.setVolume(levelFor(normalized), 0.8);
            applyDuck();
            return;
        }
        activeSignature = signature;
        switchBus(normalized);
    }

    export function stop(): void {
        activePlan = null;
        queuedPlan = null;
        activeSignature = "";
        if (context) {
            const now = context.currentTime;
            for (const bus of liveBuses.slice()) bus.fadeOut(now, 900);
        }
        stopScheduler();
        applyDuck();
    }

    export function setMuted(value: boolean): void {
        muted = Boolean(value);
        persistMuted(muted);
        if (master && context) {
            master.gain.setTargetAtTime(muted ? 0 : 1, context.currentTime, 0.12);
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
                    switchBus(activePlan);
                }
            }).catch(() => undefined);
        }
        if (ctx.state === "running" && activePlan && !liveBuses.length && !muted) {
            activeSignature = "";
            switchBus(activePlan);
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
}

window.TrpgImmersiveMusic = {
    apply: TrpgImmersiveMusicModule.apply,
    stop: TrpgImmersiveMusicModule.stop,
    setMuted: TrpgImmersiveMusicModule.setMuted,
    isMuted: TrpgImmersiveMusicModule.isMuted,
    toggleMuted: TrpgImmersiveMusicModule.toggleMuted,
    unlock: TrpgImmersiveMusicModule.unlock,
    isRunning: TrpgImmersiveMusicModule.isRunning,
};
