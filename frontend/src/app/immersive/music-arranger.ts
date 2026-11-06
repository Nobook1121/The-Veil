/**
 * Cue 结构编排器：把「氛围档位 + LLM 导演乐谱」翻译成逐小节的演奏计划。
 *
 * 电影配乐的 cue 不是「一直铺一个和弦垫」，而是一段有记忆点的结构：
 *   铺垫（bed）→ 上升（rise）→ 高潮（climax）→ 抽空/静止（void）→ 余韵（tail）
 *
 * 这里的编排规则：
 * - 段落之间用「枢轴和弦」衔接：新段落的第一小节优先重复上一段最后一个和弦，
 *   或让低音保持在 pedal point 上，避免段落切换时出现断裂式的换调；
 * - 动机按段落轮换与变形：陈述 → 应答（逆行/倒影）→ 高潮（和弦化/加八度）→ 碎片/静止；
 * - 每个小节都有独立的动态目标、滤波截止频率与混响发送量，动态曲线绝不恒定；
 * - 高潮段强制使用更重的和声色彩（半减七、减七、三全音、音簇、那不勒斯 bII）；
 * - 抽空段真正留白（silence 小节），让「静止」本身成为重音；
 * - 所有随机都来自 (seed, cycle, section, bar, layer) 的确定性哈希，同一种子必然复现同一份谱。
 *
 * 该模块是纯数据计算，不引用 Web Audio，便于在 Node 里直接验证结构性质。
 */
namespace TrpgImmersiveMusicArranger {
    import Theory = TrpgImmersiveMusicTheory;
    type SectionKind = TrpgImmersiveMusicTheory.SectionKind;
    type MotifCell = TrpgImmersiveMusicTheory.MotifCell;
    type MotifRole = TrpgImmersiveMusicTheory.MotifRole;
    type SectionShape = TrpgImmersiveMusicTheory.SectionShape;
    type ModeSpec = TrpgImmersiveMusicTheory.ModeSpec;

    export interface ChordPlan {
        /** 绝对 MIDI 根音。 */
        root: number;
        intervals: number[];
    }

    export interface MotifPlan {
        cell: MotifCell;
        role: MotifRole;
        instrument: string;
        gain: number;
        octave: number;
        startBeat: number;
    }

    export interface OstinatoPlan {
        offsets: number[];
        steps: number;
        pattern: number[];
        layers: number;
        canonOffset: number;
        gain: number;
        register: number;
    }

    export interface TimpaniPlan {
        hits: number[];
        gain: number;
    }

    /**
     * 内声部推进：不改变和弦，只在和声内部做一次短小的级进/经过音。
     * 当和声节奏本来就慢时，这一层负责让听感「在走」，而不是停在一个音响块上。
     */
    export interface MotionPlan {
        degrees: number[];
        /** 每个音的时值，单位拍。 */
        rhythm: number[];
        startBeat: number;
        instrument: "pluck" | "organ" | "strings" | "bell";
        gain: number;
        octave: number;
    }

    /** 低频/时钟式律动：心跳让人感到「还在呼吸」，时钟让人感到时间在走。 */
    export interface PulsePlan {
        kind: "heartbeat" | "clock";
        /** 单位拍。 */
        hits: number[];
        gain: number;
    }

    /** 段落间衔接：枢轴和弦、动机承接或留白呼吸。探针据此断言「段落不是硬切」。 */
    export interface TransitionPlan {
        kind: "open" | "pivot" | "pickup" | "breath";
        /** 上一段的段落 ID；开篇为 null。 */
        from: string | null;
        /** 沿用的上一段末和弦根音，用于枢轴衔接。 */
        chordRoot: number | null;
        /** 承接的上一段动机 ID。 */
        motifId: string | null;
    }

    export interface BarPlan {
        index: number;
        /** 稳定段落 ID（循环 + 段序 + 段落类型），用于验证段落真的轮换。 */
        sectionId: string;
        section: SectionKind;
        sectionBar: number;
        sectionBars: number;
        /** 0~1，本小节的响度目标（由总线增益自动执行）。 */
        dynamic: number;
        chord: ChordPlan | null;
        bass: number | null;
        pedal: number | null;
        chordVoices: string[];
        brass: boolean;
        ostinato: OstinatoPlan | null;
        motif: MotifPlan | null;
        counter: MotifPlan | null;
        /** 段落内部的内声部推进；与动机不同，它不占前景，只维持流动感。 */
        motion: MotionPlan | null;
        pulse: PulsePlan | null;
        /** 颗粒织体层：给长音垫加一层极轻的「空气」，避免整段只有振荡器。 */
        texture: boolean;
        /** 高潮进入点的打击落点。 */
        impact: boolean;
        /** 进入高潮前的长渐强（riser）。 */
        riser: boolean;
        timpani: TimpaniPlan | null;
        bell: number | null;
        noiseHit: boolean;
        swell: boolean;
        cutoff: number;
        reverb: number;
        silence: boolean;
        /** 段落第一小节携带的衔接信息；其余小节为 null。 */
        transition: TransitionPlan | null;
    }

    export interface Timeline {
        bars: BarPlan[];
        barDuration: number;
        totalBars: number;
        cycle: number;
        signature: string;
    }

    export interface CompositionSpec {
        profile: ImmersiveProfile;
        intensity: ImmersiveIntensity;
        tempo: number;
        barDuration: number;
        tonic: number;
        mode: ModeSpec;
        strategy: "functional" | "modal" | "pedal" | "nonfunctional" | "ostinato";
        progression: number[];
        explicitChords: ChordPlan[] | null;
        palette: string[];
        arc: SectionShape[];
        /** 是否由导演乐谱驱动；乐谱驱动的 arc 不再做模板改写。 */
        scoreDriven: boolean;
        colors: TrpgImmersiveMusicTheory.ColorName[];
        motifFamilies: MotifCell[][];
        motifInstruments: Record<MotifRole, string>;
        reverb: number;
        brightness: number;
        drone: boolean;
        lowPulse: boolean;
        timpani: boolean;
        pedal: number | null;
        seedBase: string;
        cycleSignature: string;
    }

    const DYNAMIC_LEVELS: Record<string, number> = { low: 0.32, medium: 0.62, high: 0.88 };

    /**
     * 叙事段落的最短长度。低于这个长度，听众只会听到「一句没展开就换段」，
     * 因此除 `void` 呼吸段外的所有段落都不少于 8 小节。
     */
    const MIN_SECTION_BARS = 8;
    /**
     * 留白是「一口气」，不是「一首曲子结束」。
     * 旧实现同样强制 8 小节：horror（44 BPM，每小节 5.45 秒）会留下 43.6 秒
     * 几乎没有和声的空档，听感就是播放戛然而止。这里按秒数收敛到 7.5 秒左右，
     * 再按当前速度折算成小节，最快与最慢档位都落在 7–11 秒。
     */
    const BREATH_SECONDS = 7.5;
    const BREATH_BARS_MIN = 2;
    const BREATH_BARS_MAX = 4;

    /**
     * 和声节奏（多少小节换一次和弦）不再固定为 4/2/1/3 小节，
     * 而是按「目标秒数 ÷ 当前小节秒数」折算：慢速档位每小节就换和弦，
     * 快速档位可以连续 4 小节再换。这样任何速度下，一个和弦都不会拖过约 8 秒。
     * 旧规则在 horror 的 bed 段是 4 × 5.45 = 21.8 秒一个和弦，audit 实测最长 32.7 秒。
     */
    const HARMONY_SECONDS: Record<SectionKind, number> = {
        bed: 6,
        rise: 4.5,
        climax: 3,
        void: 99,
        tail: 5,
    };

    /** FNV-1a：把任意种子字符串压成 32 位整数。 */
    export function hashSeed(input: string): number {
        let hash = 2166136261;
        const text = String(input || "");
        for (let index = 0; index < text.length; index += 1) {
            hash ^= text.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return hash >>> 0;
    }

    /** mulberry32：小、快、确定性的伪随机数生成器。 */
    export function makeRandom(seed: number): () => number {
        let state = seed >>> 0;
        return function next(): number {
            state = (state + 0x6d2b79f5) >>> 0;
            let value = state;
            value = Math.imul(value ^ (value >>> 15), value | 1);
            value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
            return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
        };
    }

    /** 分层哈希：同一 seed 下，各段落/小节/声部得到互不干扰但稳定复现的随机流。 */
    export function streamRandom(seedBase: string, parts: Array<string | number>): () => number {
        return makeRandom(hashSeed(`${seedBase}|${parts.join("|")}`));
    }

    export function clamp(value: number, min: number, max: number): number {
        if (!Number.isFinite(value)) return min;
        return Math.max(min, Math.min(max, value));
    }

    function numberOr(value: unknown, fallback: number): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function validScore(score: ImmersiveScore | null | undefined): ImmersiveScore | null {
        if (!score) return null;
        const chords = Array.isArray(score.chords) ? score.chords : [];
        if (chords.length < 1) return null;
        if (!Array.isArray(chords[0]?.intervals) || (chords[0]?.intervals.length || 0) < 1) return null;
        const tempo = numberOr(score.tempo, 0);
        if (tempo < 30 || tempo > 200) return null;
        return score;
    }

    function normalizeCell(
        id: string,
        degrees: number[],
        rhythm: number[],
        contour: string,
        harmony: TrpgImmersiveMusicTheory.MotifHarmony = "single",
    ): MotifCell {
        const safeDegrees = degrees.filter((value) => Number.isFinite(value)).slice(0, 8);
        const safeRhythm = rhythm
            .map((value) => clamp(numberOr(value, 1), 0.25, 4))
            .slice(0, safeDegrees.length);
        while (safeRhythm.length < safeDegrees.length) safeRhythm.push(1);
        return {
            id,
            degrees: safeDegrees.length ? safeDegrees : [0, 4, 7],
            rhythm: safeRhythm.length ? safeRhythm : [1, 1, 1],
            contour,
            harmony,
            transpose: 0,
        };
    }

    function scoreMotifFamilies(score: ImmersiveScore): MotifCell[][] | null {
        const variants = Array.isArray(score.motif_variants) ? score.motif_variants : [];
        const families: MotifCell[][] = [];
        variants.forEach((variant, index) => {
            const degrees = Array.isArray(variant?.degrees) ? variant.degrees.map((value) => numberOr(value, 0)) : [];
            if (!degrees.length) return;
            const rhythm = Array.isArray(variant?.rhythm) ? variant.rhythm.map((value) => numberOr(value, 1)) : [];
            const role = String(variant?.role || "statement");
            const harmony: TrpgImmersiveMusicTheory.MotifHarmony = role === "climax" ? "fifth" : "single";
            families.push([normalizeCell(`score-motif-${index}`, degrees, rhythm, role, harmony)]);
        });
        if (families.length) return families;
        const motif = Array.isArray(score.motif) ? score.motif : [];
        if (motif.length >= 2) {
            const degrees = motif.map((value) => numberOr(value, 0));
            const rhythm = degrees.map(() => 4 / degrees.length);
            return [[normalizeCell("score-motif", degrees, rhythm, "score")]];
        }
        return null;
    }

    function directorArc(score: ImmersiveScore): SectionShape[] {
        const sections = Array.isArray(score.sections) ? score.sections : [];
        if (!sections.length) return [];
        const levels = Array.isArray(score.dynamics) ? score.dynamics : [];
        const shapes: SectionShape[] = sections.map((section, index) => {
            const fallback = DYNAMIC_LEVELS[String(section?.dynamic || "")] ?? 0.58;
            const level = clamp(numberOr(levels[index], fallback), 0, 1);
            return {
                kind: "rise",
                bars: Math.round(clamp(numberOr(section?.bars, 4), 1, 24)),
                dynamic: level,
                density: clamp(level * 0.95 + 0.05, 0, 1),
            };
        });
        if (shapes.length === 1) {
            const level = shapes[0]?.dynamic ?? 0.4;
            return [
                { kind: "bed", bars: 10, dynamic: clamp(level * 0.6, 0.2, 0.45), density: 0.3 },
                { kind: "rise", bars: 10, dynamic: clamp(level * 0.85, 0.45, 0.7), density: 0.55 },
                { kind: "climax", bars: 10, dynamic: clamp(level, 0.72, 1), density: 0.9 },
                { kind: "void", bars: 8, dynamic: 0.08, density: 0.06 },
                { kind: "tail", bars: 8, dynamic: clamp(level * 0.4, 0.12, 0.3), density: 0.18 },
            ];
        }
        // 按动态曲线的峰值定位高潮：峰前是铺垫/上升，峰后回落；
        // 不能只靠阈值分类，否则会出现「两个高潮、没有上升段」的断裂 cue。
        let peakIndex = 1;
        shapes.forEach((shape, index) => {
            if (index > 0 && shape.dynamic > (shapes[peakIndex]?.dynamic ?? 0)) peakIndex = index;
        });
        shapes.forEach((shape, index) => {
            if (index === peakIndex) shape.kind = "climax";
            else if (index < peakIndex) shape.kind = index === 0 ? "bed" : "rise";
            else shape.kind = shape.dynamic >= 0.5 ? "rise" : "tail";
        });
        if (peakIndex === shapes.length - 1) {
            shapes.push({ kind: "tail", bars: 8, dynamic: 0.22, density: 0.18 });
        } else {
            const lastShape = shapes[shapes.length - 1];
            if (lastShape) lastShape.kind = "tail";
        }
        if (!shapes.some((shape) => shape.kind === "rise")) {
            const climax = shapes[peakIndex];
            shapes.splice(peakIndex, 0, {
                kind: "rise",
                bars: Math.max(8, Math.min(12, Math.round((climax?.bars ?? 8) * 0.8))),
                dynamic: clamp((climax?.dynamic ?? 0.8) - 0.18, 0.45, 0.7),
                density: 0.6,
            });
            peakIndex += 1;
        }
        if (!shapes.some((shape) => shape.kind === "void")) {
            const climaxIndex = shapes.findIndex((shape) => shape.kind === "climax");
            // 高潮之后插入八小节「抽空」，让静止成为重音，再接余韵。
            shapes.splice(climaxIndex + 1, 0, { kind: "void", bars: 8, dynamic: 0.08, density: 0.06 });
        }
        return shapes;
    }

    /**
     * 解析 plan：优先采用导演乐谱（有校验回退），缺失字段回落到档位配方。
     * 旧乐谱（只有 chords/mode/tempo/instrumentation/motif/sections/dynamics）行为保持不变。
     */
    export function resolveSpec(plan: ImmersiveMusicPlan): CompositionSpec {
        const recipe = Theory.RECIPES[plan.profile] || Theory.RECIPES.suspense;
        const score = validScore(plan.score);
        const intensity = plan.intensity || "medium";
        const seedBase = String(plan.seed || "immersive-core");
        const tempoScale: Record<ImmersiveIntensity, number> = { low: 0.92, medium: 1, high: 1.07 };

        let tempo = Math.round(recipe.tempo * (score ? 1 : (tempoScale[intensity] || 1)));
        let modeName = recipe.mode;
        let tonic = recipe.root;
        let progression = recipe.progression.slice();
        let explicitChords: ChordPlan[] | null = null;
        let palette = recipe.palette.slice();
        let colors = recipe.colors.slice();
        let arc = recipe.arc.map((shape) => ({ ...shape }));
        let motifFamilies = recipe.motifFamilies;
        let pedal: number | null = null;

        if (score) {
            tempo = Math.round(clamp(numberOr(score.tempo, recipe.tempo), 30, 200));
            modeName = String(score.mode || recipe.mode);
            const chords = Array.isArray(score.chords) ? score.chords : [];
            const firstRoot = numberOr(chords[0]?.root, 0);
            tonic = 48 + clamp(Math.round(firstRoot), 0, 11);
            explicitChords = chords.slice(0, 6).map((chord, index) => {
                const rootOffset = numberOr(chord?.root, firstRoot) - firstRoot;
                const intervals = Array.isArray(chord?.intervals) ? chord.intervals.filter((value) => Number.isFinite(value)) : [];
                return {
                    root: tonic + clamp(rootOffset, -18, 18),
                    intervals: intervals.length ? intervals.slice(0, 6) : [0, 4, 7],
                };
            });
            if (Array.isArray(score.instrumentation) && score.instrumentation.length) {
                palette = score.instrumentation.map((item) => String(item));
            }
            const scoreFamilies = scoreMotifFamilies(score);
            if (scoreFamilies) motifFamilies = scoreFamilies;
            const scoreArc = directorArc(score);
            if (scoreArc.length) arc = scoreArc;
            const pedalValue = Number(score.pedal);
            if (Number.isFinite(pedalValue) && pedalValue >= 0 && pedalValue <= 11) {
                pedal = 48 + Math.round(pedalValue);
            }
        }

        const mode = Theory.modeSpec(modeName, Theory.modeSpec(recipe.mode, Theory.MODES.major!));
        if (recipe.strategy === "pedal" && pedal === null) pedal = tonic;

        const resolvedColors = colors.length ? colors : recipe.colors;
        const hasOstinato = palette.indexOf("ostinato") >= 0;
        const drone = palette.indexOf("drone") >= 0 || (score ? false : recipe.drone);
        const timpani = palette.indexOf("timpani") >= 0 || (score ? false : recipe.timpani);
        const lowPulse = recipe.lowPulse || palette.indexOf("timpani") >= 0;

        return {
            profile: plan.profile,
            intensity,
            tempo,
            barDuration: 60 / tempo * 4,
            tonic,
            mode,
            strategy: recipe.strategy,
            progression,
            explicitChords,
            palette,
            arc,
            scoreDriven: Boolean(score),
            colors: hasOstinato
                ? resolvedColors.concat(["power", "sus4"] as TrpgImmersiveMusicTheory.ColorName[])
                : resolvedColors,
            motifFamilies: motifFamilies.length ? motifFamilies : recipe.motifFamilies,
            motifInstruments: recipe.motifInstruments,
            reverb: recipe.reverb,
            brightness: recipe.brightness,
            drone,
            lowPulse,
            timpani,
            pedal,
            seedBase,
            cycleSignature: [
                plan.profile,
                intensity,
                plan.section,
                plan.seed,
                score ? score.mode : "",
                score ? score.tempo : "",
                plan.director?.fingerprint || "",
            ].join("|"),
        };
    }

    /**
     * 动机族轮换：同一循环内优先用「还没出现过」的族；不得不用旧族时，
     * 对它做逆行/倒影，避免同一句旋律在跨段落原样重复。
     */
    function pickFamily(
        spec: CompositionSpec,
        sectionIndex: number,
        cycle: number,
        usage: Map<number, number>,
    ): MotifCell[] {
        const families = spec.motifFamilies;
        if (!families.length) return [];
        // 用种子做盐：换段/换歌后同一戏剧阶段也不会拿到同一族动机。
        const salt = hashSeed(spec.seedBase) % families.length;
        const prefer = ((sectionIndex + cycle + salt) % families.length + families.length) % families.length;
        let index = prefer;
        for (let offset = 0; offset < families.length; offset += 1) {
            const candidate = (prefer + offset) % families.length;
            if ((usage.get(candidate) || 0) === 0) {
                index = candidate;
                break;
            }
        }
        const reuseCount = usage.get(index) || 0;
        usage.set(index, reuseCount + 1);
        const family = families[index] || [];
        if (reuseCount === 0) return family;
        // 复用旧族时换一个变形算子，让「同一族」听起来仍是新的句子。
        const op: TrpgImmersiveMusicTheory.TransformName = reuseCount % 2 === 1 ? "retrograde" : "invert";
        return family.map((cell) => Theory.transformMotif(cell, op));
    }

    /** 每档位 / 每种段落的首选和弦声部（音色对比的核心，而不是全程同一个 pad）。 */
    function chordVoicesFor(kind: SectionKind, spec: CompositionSpec): string[] {
        const available = spec.palette;
        const has = (name: string): boolean => available.indexOf(name) >= 0;
        const pick = (...names: string[]): string[] => names.filter(has);
        switch (kind) {
            case "bed":
                if (spec.profile === "horror") return pick("organ", "drone", "pad");
                if (spec.profile === "awe") return pick("organ", "choir", "pad");
                return pick("pad", "strings", "organ", "choir");
            case "rise":
                return pick("strings", "organ", "choir", "pad");
            case "climax":
                return pick("strings", "organ", "choir", "pad");
            case "void":
                return [];
            case "tail":
                return pick("pad", "choir", "strings", "organ");
            default:
                return pick("pad");
        }
    }

    function harmonicRhythmFor(kind: SectionKind, barDuration: number): number {
        const target = HARMONY_SECONDS[kind] ?? 6;
        // 至少 1 小节；最多 4 小节，避免快速档位把和弦拖成「一块背景」。
        return Math.max(1, Math.min(4, Math.round(target / Math.max(0.2, barDuration))));
    }

    function dynamicCurve(kind: SectionKind, bar: number, bars: number, base: number, random: () => number): number {
        const span = Math.max(1, bars - 1);
        const t = clamp(bar / span, 0, 1);
        let level = base;
        switch (kind) {
            case "bed":
                level = base * (0.55 + 0.45 * t);
                break;
            case "rise":
                level = base * (0.78 + 0.52 * t);
                break;
            case "climax":
                level = base * (0.9 + 0.16 * Math.sin(Math.PI * t)) + (random() - 0.5) * 0.04;
                break;
            case "void":
                level = base * Math.max(0.06, 0.95 * (1 - t));
                break;
            case "tail":
                level = base * (0.78 - 0.56 * t);
                break;
            default:
                break;
        }
        return clamp(level, 0.02, 1);
    }

    function cutoffFor(kind: SectionKind, sectionBar: number, bars: number, brightness: number): number {
        const t = clamp(sectionBar / Math.max(1, bars - 1), 0, 1);
        const factor = clamp(brightness / 900, 0.45, 1.6);
        let base = 700;
        switch (kind) {
            case "bed": base = 620; break;
            case "rise": base = 860 + 320 * t; break;
            case "climax": base = 1380 + 520 * t; break;
            case "void": base = 380; break;
            case "tail": base = 680; break;
            default: break;
        }
        return clamp(base * factor, 180, 5200);
    }

    function reverbFor(kind: SectionKind, spec: CompositionSpec): number {
        const base = spec.reverb;
        switch (kind) {
            case "bed": return clamp(base * 0.9, 0, 1);
            case "rise": return clamp(base * 0.85, 0, 1);
            case "climax": return clamp(base * 0.62, 0, 1);
            case "void": return clamp(base * 1.15, 0, 1);
            case "tail": return clamp(base * 1.0, 0, 1);
            default: return base;
        }
    }

    function motifOpFor(role: MotifRole, random: () => number): TrpgImmersiveMusicTheory.TransformName {
        switch (role) {
            case "statement": return "identity";
            case "answer": return random() < 0.5 ? "retrograde" : "invert";
            case "climax": return "chordify";
            case "fragment": return "fragment";
            default: return "identity";
        }
    }

    function buildMotifPlan(
        family: MotifCell[],
        role: MotifRole,
        instrument: string,
        random: () => number,
    ): MotifPlan {
        const source = family[Math.min(family.length - 1, Math.floor(random() * family.length))] || family[0];
        if (!source) {
            return {
                cell: normalizeCell("empty", [0, 4, 7], [1, 1, 1], "static"),
                role,
                instrument,
                gain: 0.4,
                octave: 0,
                startBeat: 0,
            };
        }
        let cell = Theory.transformMotif(source, motifOpFor(role, random));
        if (role === "answer") cell = Theory.transformMotif(cell, "augment");
        if (role === "climax") cell = Theory.transformMotif(cell, "octave");
        if (role === "fragment") cell = Theory.transformMotif(cell, "augment");
        const config: Record<MotifRole, { gain: number; octave: number; startBeat: number }> = {
            statement: { gain: 0.62, octave: 0, startBeat: 0 },
            answer: { gain: 0.42, octave: -12, startBeat: 1 },
            climax: { gain: 0.82, octave: 12, startBeat: 0 },
            fragment: { gain: 0.34, octave: 24, startBeat: 1.5 },
        };
        const meta = config[role];
        return { cell, role, instrument, gain: meta.gain, octave: meta.octave, startBeat: meta.startBeat };
    }

    function chordFromDegree(
        spec: CompositionSpec,
        degree: number,
        kind: SectionKind,
        random: () => number,
    ): ChordPlan {
        const color = Theory.pickColor(kind, spec.colors, random);
        const built = Theory.buildChord(spec.mode, degree, color);
        return { root: spec.tonic + built.root, intervals: built.intervals };
    }

    function peakChord(spec: CompositionSpec, random: () => number): ChordPlan {
        const hard = ["bII", "tritone", "cluster", "dim7", "half_dim"].filter(
            (name) => spec.colors.indexOf(name as TrpgImmersiveMusicTheory.ColorName) >= 0,
        );
        const color = (hard[Math.floor(random() * hard.length)] || "tritone") as TrpgImmersiveMusicTheory.ColorName;
        const degree = spec.progression[Math.floor(random() * spec.progression.length)] || 1;
        const built = Theory.buildChord(spec.mode, degree, color);
        return { root: spec.tonic + built.root, intervals: built.intervals };
    }

    function buildSectionChords(
        spec: CompositionSpec,
        shape: SectionShape,
        sectionIndex: number,
        cycle: number,
        previous: ChordPlan | null,
        random: () => number,
    ): ChordPlan[] {
        const count = Math.max(1, shape.bars);
        const rhythm = harmonicRhythmFor(shape.kind, spec.barDuration);
        const result: ChordPlan[] = [];
        let current: ChordPlan | null = previous;
        let step = Math.floor(cycle / 2) + sectionIndex;
        // 第 0 小节沿用上一段最后一个和弦（延留），所以第一次换和弦从第 1 小节起算；
        // 否则「延留 + 一个完整和声节奏」会在段落线附近叠成两倍时长。
        const anchor = previous ? 1 : 0;
        const sameChord = (a: ChordPlan | null, b: ChordPlan | null): boolean => Boolean(
            a && b
            && a.root === b.root
            && a.intervals.length === b.intervals.length
            && a.intervals.every((value, index) => value === b.intervals[index]),
        );
        for (let bar = 0; bar < count; bar += 1) {
            const shouldChange = bar === 0 || (bar - anchor) % rhythm === 0;
            if (shouldChange) {
                if (bar === 0 && previous) {
                    current = previous;
                } else {
                    // 进行里允许重复级数（例如 0-0-3-1-0），但连续两次落在同一个和弦上
                    // 会让听感重新「停住」；这里跳过与上一小节完全相同的和弦。
                    const lastPushed = result.length ? result[result.length - 1] ?? null : null;
                    let candidate: ChordPlan | null = null;
                    let guard = 0;
                    do {
                        if (spec.explicitChords && spec.explicitChords.length) {
                            const list = spec.explicitChords;
                            const picked = list[step % list.length];
                            if (picked) {
                                candidate = { root: picked.root + (cycle % 2 === 0 ? 0 : 1), intervals: picked.intervals.slice() };
                            }
                            step += 1;
                        } else {
                            const list = spec.progression;
                            const degree = list.length ? list[step % list.length] || 0 : 0;
                            candidate = chordFromDegree(spec, degree, shape.kind, random);
                            step += 1;
                        }
                        guard += 1;
                    } while (guard < 4 && sameChord(candidate, lastPushed));
                    if (candidate) current = candidate;
                }
            }
            if (shape.kind === "climax" && bar === Math.floor(count / 2)) {
                current = peakChord(spec, random);
            }
            result.push(current || chordFromDegree(spec, 0, shape.kind, random));
        }
        return result;
    }

    /**
     * Ostinato 不是「每小节原样重放」的背景音型，而是一条会发展的固定低音：
     * 每小节轮换音型起点、每 4 小节做一次反向、每 8 小节换一次八度，
     * 再叠加按小节错位的卡农层。这样同一段里不会出现 1 小节原样循环。
     */
    function ostinatoFor(
        spec: CompositionSpec,
        family: MotifCell[],
        shape: SectionShape,
        sectionBar: number,
        cycle: number,
    ): OstinatoPlan | null {
        const hasOstinato = spec.palette.indexOf("ostinato") >= 0 || spec.strategy === "ostinato";
        if (!hasOstinato) return null;
        if (shape.kind === "void") return null;
        const base = family[0]?.degrees || [0, 3, 7];
        const offsets = base.slice(0, Math.min(3, base.length));
        while (offsets.length < 3) offsets.push(0);
        const phase = sectionBar + cycle * 2;
        const rotation = offsets.length ? phase % offsets.length : 0;
        const rotated = offsets.slice(rotation).concat(offsets.slice(0, rotation));
        const developed = Math.floor(phase / 4) % 2 === 1 ? rotated.map((value) => -value) : rotated;
        const steps = spec.profile === "urgency" ? 16 : 8;
        const basePattern = steps === 16
            ? [1, 0, 1, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1]
            : [1, 0, 1, 1, 0, 1, 0, 1];
        const patternShift = basePattern.length ? phase % basePattern.length : 0;
        const pattern = basePattern.slice(patternShift).concat(basePattern.slice(0, patternShift));
        // 奇偶小节各让一个落点「缺席」：同一音型在相邻小节不会逐音符完全相同。
        if (phase % 2 === 1) {
            const lastOnset = pattern.lastIndexOf(1);
            if (lastOnset >= 0) pattern[lastOnset] = 0;
        }
        let layers = 0;
        if (shape.kind === "bed") layers = sectionBar > 1 ? 1 : 0;
        else if (shape.kind === "rise") layers = sectionBar >= Math.floor(shape.bars / 2) ? 2 : 1;
        else if (shape.kind === "climax") layers = 3;
        else if (shape.kind === "tail") layers = 1;
        if (layers <= 0) return null;
        if (spec.intensity === "low") layers = Math.min(layers, 1);
        if (spec.intensity === "high" && shape.kind === "rise") layers = Math.max(layers, 2);
        const register = (shape.kind === "climax" ? 12 : 0)
            + (Math.floor(phase / 8) % 2 === 0 ? 0 : 12);
        return {
            offsets: developed,
            steps,
            pattern,
            layers,
            canonOffset: 1 + (phase % 2),
            gain: shape.kind === "climax" ? 0.34 : 0.24,
            register,
        };
    }

    /**
     * 内声部推进：用和弦音在弱拍做 2–3 个音的短句，不引入调外音，
     * 因此不会改变和声功能，只让长音之间「有东西在走」。
     * 有前景动机的小节让位，避免把乐句塞满。
     */
    function motionFor(
        spec: CompositionSpec,
        shape: SectionShape,
        sectionBar: number,
        chord: ChordPlan,
        random: () => number,
    ): MotionPlan | null {
        const tones = chord.intervals;
        if (tones.length < 2 || shape.kind === "void") return null;
        const low = tones[0] ?? 0;
        const third = tones[1] ?? low;
        const fifth = tones[tones.length - 1] ?? low;
        let degrees: number[];
        let rhythm: number[];
        let startBeat: number;
        let gain: number;
        let octave: number;
        switch (shape.kind) {
            case "bed":
                degrees = [third + 12, fifth + 12];
                rhythm = [1.25, 0.75];
                startBeat = 2.5;
                gain = 0.085;
                octave = 0;
                break;
            case "rise":
                degrees = [low + 12, third + 12, fifth + 12];
                rhythm = [0.5, 0.5, 1];
                startBeat = 2;
                gain = 0.1;
                octave = 0;
                break;
            case "climax":
                degrees = [fifth + 12, third + 12, low + 24];
                rhythm = [0.5, 0.5, 1];
                startBeat = 1.5;
                gain = 0.105;
                octave = 0;
                break;
            case "tail":
                degrees = [third + 12, low + 12];
                rhythm = [1.5, 1];
                startBeat = 2;
                gain = 0.07;
                octave = 0;
                break;
            default:
                return null;
        }
        // 每隔几小节换一次音区与落拍，避免内声部自己也变成固定 loop。
        if (sectionBar % 4 === 3) {
            degrees = degrees.slice().reverse();
            startBeat = Math.min(3, startBeat + 0.5);
        }
        if (random() < 0.22) octave = 12;
        if (spec.intensity === "low") gain *= 0.75;
        if (spec.intensity === "high") gain *= 1.12;
        const instrument: MotionPlan["instrument"] = spec.profile === "horror" || spec.profile === "unease"
            ? "organ"
            : spec.profile === "awe" || spec.profile === "suspense"
                ? "bell"
                : "pluck";
        return { degrees, rhythm, startBeat, instrument, gain, octave };
    }

    /**
     * 低频脉动：心跳是「Lub-Dub」两下，时钟是单点。
     * 这两层只在低频/时间感重要的档位出现，紧迫档位已有 16 分 ostinato，不再叠加。
     */
    function pulseFor(
        spec: CompositionSpec,
        shape: SectionShape,
        sectionBar: number,
    ): PulsePlan | null {
        if (!spec.lowPulse) return null;
        // 留白段保留一条极稀疏的心跳：抽空的是和声，不是「房间」。
        // 这也是判断「音乐还在不在播」最直接的听觉锚点；
        // urgency 的 ostinato 在留白段整体撤掉，更需要这条心跳托住。
        if (shape.kind === "void") {
            return { kind: "heartbeat", hits: [0], gain: 0.03 };
        }
        if (spec.profile === "urgency") return null;
        if (spec.profile === "suspense" || spec.profile === "unease") {
            if (shape.kind === "bed") {
                return sectionBar % 2 === 0
                    ? { kind: "clock", hits: [sectionBar % 4 === 0 ? 1 : 2], gain: 0.028 }
                    : null;
            }
            if (shape.kind === "tail" && sectionBar > 1) return null;
        }
        if (shape.kind === "bed") {
            return sectionBar % 2 === 0 ? { kind: "heartbeat", hits: [0, 0.3], gain: 0.048 } : null;
        }
        if (shape.kind === "tail") {
            return sectionBar % 2 === 0 ? { kind: "heartbeat", hits: [0, 0.3], gain: 0.038 } : null;
        }
        if (shape.kind === "rise") {
            return { kind: "heartbeat", hits: sectionBar % 2 === 0 ? [0, 0.3] : [1.5, 1.8], gain: 0.052 };
        }
        return { kind: "heartbeat", hits: [0, 0.3, 2, 2.3], gain: 0.062 };
    }

    /** 颗粒织体只给「空、亮、残响」类档位与留白，普通日常不加，避免糊成一片。 */
    function textureFor(spec: CompositionSpec, shape: SectionShape): boolean {
        if (shape.kind === "void") return true;
        if (spec.intensity === "low") return false;
        if (spec.profile === "awe" || spec.profile === "aftermath") {
            return shape.kind === "bed" || shape.kind === "tail";
        }
        if (spec.profile === "sorrow") return shape.kind === "bed";
        return false;
    }

    function timpaniFor(
        spec: CompositionSpec,
        shape: SectionShape,
        sectionBar: number,
        random: () => number,
    ): TimpaniPlan | null {
        if (!spec.timpani) return null;
        if (shape.kind === "bed" && sectionBar < shape.bars - 1) return null;
        if (shape.kind === "void") return null;
        if (shape.kind === "tail" && sectionBar > 1) return null;
        let hits: number[] = [];
        if (spec.profile === "horror") {
            hits = shape.kind === "climax" ? [0, 0.7, 1.4, 2.1] : [0, 1.5];
        } else if (spec.profile === "urgency") {
            hits = shape.kind === "climax" ? [0, 1, 2, 3] : [0, 2];
        } else {
            hits = shape.kind === "climax" ? [0, 1.5, 3] : [0];
        }
        if (shape.kind === "rise" && random() > 0.6) return null;
        return {
            hits,
            gain: shape.kind === "climax" ? 0.9 : shape.kind === "rise" ? 0.55 : 0.35,
        };
    }

    function buildSection(
        spec: CompositionSpec,
        shape: SectionShape,
        sectionIndex: number,
        cycle: number,
        previous: ChordPlan | null,
        previousSectionId: string | null,
        previousMotif: MotifCell | null,
        usage: Map<number, number>,
    ): { bars: BarPlan[]; last: ChordPlan | null; lastMotif: MotifCell | null } {
        const random = streamRandom(spec.seedBase, ["section", cycle, sectionIndex, shape.kind]);
        const chords = buildSectionChords(spec, shape, sectionIndex, cycle, previous, random);
        const family = pickFamily(spec, sectionIndex, cycle, usage);
        const sectionId = `c${cycle}-s${sectionIndex}-${shape.kind}`;
        const voices = chordVoicesFor(shape.kind, spec);
        const brassAvailable = spec.palette.indexOf("brass") >= 0;
        const hasBells = spec.palette.indexOf("bells") >= 0;
        const hasNoise = spec.palette.indexOf("noise") >= 0;
        const bars: BarPlan[] = [];
        let sectionMotif: MotifCell | null = previousMotif;

        for (let bar = 0; bar < shape.bars; bar += 1) {
            const barRandom = streamRandom(spec.seedBase, ["bar", cycle, sectionIndex, bar]);
            const chord = chords[bar] || chords[0] || null;
            const kind = shape.kind;
            const dynamic = dynamicCurve(kind, bar, shape.bars, shape.dynamic, barRandom);
            const isVoid = kind === "void";
            // 留白段不再设「整小节全静音」：旧实现每 3 小节插一个 silence 小节，
            // 在 44 BPM 的 horror 里等于连续 5.45 秒没有任何推进，听感就是播放中断。
            // 现在留白靠「抽掉和弦 + 压低总线 + 只留心跳/颗粒/钟声」表达，
            // 是低电平的空洞，而不是真正的静音，因此永远不会出现断流。
            const pedal = isVoid ? null : spec.pedal;
            // 踏板音按「2 小节一块」在主音与属音之间交替（I–V pedal）：
            // 既保留 pedal point 的悬置感，又不会在 44 BPM 的 horror 里
            // 连续 43 秒只有一个低音（那会让人误以为整段停在一个音上）。
            const pedalHold = pedal !== null
                && (kind === "bed" || kind === "tail")
                && Math.floor(bar / 2) % 2 === 0;
            const bass = isVoid || !chord
                ? null
                : pedal !== null && (kind === "bed" || kind === "tail")
                    ? pedal - 12 + (pedalHold ? 0 : 7)
                    : chord.root - 12;
            const transition: TransitionPlan | null = bar === 0
                ? {
                    kind: previousSectionId === null
                        ? "open"
                        : isVoid
                            ? "breath"
                            : previousMotif
                                ? "pickup"
                                : "pivot",
                    from: previousSectionId,
                    chordRoot: previous ? previous.root : null,
                    motifId: previousMotif ? previousMotif.id : null,
                }
                : null;

            let motif: MotifPlan | null = null;
            let counter: MotifPlan | null = null;
            if (kind === "rise" && bar === 0) {
                motif = buildMotifPlan(family, "statement", spec.motifInstruments.statement, barRandom);
            } else if (kind === "rise" && shape.bars > 4 && bar === Math.floor(shape.bars / 2)) {
                counter = buildMotifPlan(family, "answer", spec.motifInstruments.answer, barRandom);
            } else if (kind === "rise" && shape.bars >= 3 && bar === shape.bars - 1) {
                // 弱起（anacrusis）：上升段最后一小节后半拍提前抛出一句碎片，让高潮进入有「拽」的感觉。
                counter = buildMotifPlan(family, "fragment", spec.motifInstruments.fragment, barRandom);
                counter.startBeat = 2.5;
                counter.gain = 0.18;
                counter.octave = 12;
            } else if (kind === "climax" && bar === 0) {
                motif = buildMotifPlan(family, "climax", spec.motifInstruments.climax, barRandom);
            } else if (kind === "climax" && shape.bars > 3 && bar === Math.floor(shape.bars / 2)) {
                motif = buildMotifPlan(family, "fragment", spec.motifInstruments.fragment, barRandom);
            } else if (kind === "void" && bar === 0) {
                motif = buildMotifPlan(family, "fragment", spec.motifInstruments.fragment, barRandom);
            } else if (kind === "tail" && shape.bars > 3 && bar === 2) {
                const reprise = buildMotifPlan(family, "statement", spec.motifInstruments.statement, barRandom);
                reprise.cell = Theory.transformMotif(reprise.cell, "augment");
                reprise.gain = 0.4;
                motif = reprise;
            } else if (kind === "bed" && shape.bars >= 6 && bar === shape.bars - 1) {
                motif = buildMotifPlan(family, "fragment", spec.motifInstruments.fragment, barRandom);
                motif.gain = 0.22;
            }
            // 动机承接：新段落第一小节用上一段末句的碎片做 pickup，让段落边界有可听的「接句」。
            if (bar === 0 && previousMotif && !isVoid && counter === null) {
                counter = buildMotifPlan([previousMotif], "fragment", spec.motifInstruments.fragment, barRandom);
                counter.cell = Theory.transformMotif(previousMotif, "fragment");
                counter.startBeat = 0.5;
                counter.gain = 0.2;
                counter.octave = 12;
            }
            if (motif) sectionMotif = motif.cell;

            const ostinato = ostinatoFor(spec, family, shape, bar, cycle);
            const timpani = timpaniFor(spec, shape, bar, barRandom);
            // 内声部只在没有前景动机的小节进入，保证「一直在动」但不和动机抢位置。
            const motion = isVoid || !chord || motif || counter
                ? null
                : motionFor(spec, shape, bar, chord, barRandom);
            const pulse = pulseFor(spec, shape, bar);
            const texture = textureFor(spec, shape);
            const impact = kind === "climax" && bar === 0;
            const riser = kind === "rise" && bar === shape.bars - 1;
            const densityScale = spec.intensity === "low" ? 0.72 : spec.intensity === "high" ? 1.16 : 1;
            const bellChance = (kind === "bed" ? 0.16 : kind === "tail" ? 0.2 : 0.08) * densityScale;
            const bell = hasBells && (kind === "void" || barRandom() < bellChance)
                ? (chord ? chord.root + 24 + (chord.intervals[0] || 0) : spec.tonic + 24)
                : null;
            const brass = brassAvailable && kind === "climax" && bar >= Math.floor(shape.bars / 2);
            const swell = kind === "climax" ? bar === 0 : kind === "rise" && bar === shape.bars - 1 && hasNoise;
            const noiseHit = hasNoise && kind === "climax" && barRandom() < 0.4 * densityScale;

            bars.push({
                index: -1,
                sectionId,
                section: kind,
                sectionBar: bar,
                sectionBars: shape.bars,
                dynamic,
                chord: isVoid ? null : chord,
                bass,
                pedal,
                chordVoices: isVoid ? [] : voices,
                brass,
                ostinato,
                motif,
                counter,
                motion,
                pulse,
                texture,
                impact,
                riser,
                timpani,
                bell,
                noiseHit,
                swell,
                cutoff: cutoffFor(kind, bar, shape.bars, spec.brightness),
                reverb: reverbFor(kind, spec),
                silence: false,
                transition,
            });
        }
        return { bars, last: chords[chords.length - 1] || previous, lastMotif: sectionMotif };
    }

    /**
     * 同一档位的 cue 模板轮换：避免循环几轮后总是同一个段落形状。
     * 导演乐谱驱动的 cue 不做改写，保持导演给出的结构。
     */
    function cueArc(spec: CompositionSpec, cycle: number): SectionShape[] {
        if (spec.scoreDriven) return spec.arc;
        const variant = ((cycle % 3) + 3) % 3;
        if (variant === 0) return spec.arc;
        return spec.arc.map((shape) => {
            const bars = shape.bars;
            if (variant === 1) {
                // 慢燃：更长铺垫与余韵，高潮不变，留白多一拍。
                if (shape.kind === "bed") return { ...shape, bars: Math.min(16, bars + 2) };
                if (shape.kind === "rise") return { ...shape, bars: Math.min(16, bars + 1) };
                if (shape.kind === "void") return { ...shape, bars: Math.min(6, bars + 1) };
                if (shape.kind === "tail") return { ...shape, bars: Math.min(12, bars + 2) };
                return shape;
            }
            // 突变：短铺垫、快速上升、更短余韵，适合恐怖/紧迫的第二次循环。
            if (shape.kind === "bed") return { ...shape, bars: clamp(bars, 2, 4) };
            if (shape.kind === "rise") return { ...shape, bars: Math.min(12, bars + 2) };
            if (shape.kind === "climax") return { ...shape, bars: Math.min(10, bars + 1) };
            if (shape.kind === "void") return { ...shape, bars: clamp(bars, 1, 2) };
            if (shape.kind === "tail") return { ...shape, bars: clamp(bars, 2, 4) };
            return shape;
        });
    }

    /**
     * 生成一个循环（cue）的逐小节计划。
     * 每次循环会轮换动机族、和弦序位并整体移调 1~2 个半音，避免长时间循环后「一直是同一个调」。
     */
    export function buildTimeline(spec: CompositionSpec, cycle = 0): Timeline {
        const bars: BarPlan[] = [];
        let previous: ChordPlan | null = null;
        let previousSectionId: string | null = null;
        let previousMotif: MotifCell | null = null;
        const usage = new Map<number, number>();
        cueArc(spec, cycle).forEach((shape, sectionIndex) => {
            const barsCount = shape.kind === "void"
                ? Math.max(
                    BREATH_BARS_MIN,
                    Math.min(BREATH_BARS_MAX, Math.round(BREATH_SECONDS / Math.max(0.2, spec.barDuration))),
                )
                : Math.round(Math.max(MIN_SECTION_BARS, shape.bars));
            const normalized: SectionShape = { ...shape, bars: barsCount };
            const section = buildSection(
                spec,
                normalized,
                sectionIndex,
                cycle,
                previous,
                previousSectionId,
                previousMotif,
                usage,
            );
            section.bars.forEach((bar) => {
                bar.index = bars.length;
                bars.push(bar);
            });
            previous = section.last;
            previousSectionId = section.bars[0]?.sectionId || previousSectionId;
            previousMotif = section.lastMotif || previousMotif;
        });
        return {
            bars,
            barDuration: spec.barDuration,
            totalBars: bars.length,
            cycle,
            signature: `${spec.cycleSignature}|c${cycle}`,
        };
    }

    /** 段落起点小节：引擎换歌时从这里切入，避免每次都从 0 号小节的「开头」重放。 */
    export function sectionStartBar(timeline: Timeline, kind: SectionKind): number {
        const found = timeline.bars.find((bar) => bar.section === kind && bar.sectionBar === 0);
        return found ? found.index : 0;
    }

    /** 兼容旧调用：判断任意 plan 能否走到导演乐谱分支。 */
    export function hasDirectorScore(plan: ImmersiveMusicPlan): boolean {
        return validScore(plan.score) !== null;
    }
}
