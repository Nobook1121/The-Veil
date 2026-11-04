/**
 * 电影配乐乐理数据：调式、和弦色彩、动机库、变形算子与八个氛围档位的编配配方。
 *
 * 这份文件只放「可复用的作曲规则」，不碰 Web Audio、DOM 或调度器：
 * - 调式的音级是半音集合，`scaleNote` 可以把任意音级（含负数与跨八度）折算成相对主音的半音偏移；
 * - 和弦必须从调式音级生长出来，而不是随手写死三和弦，保证「同一份谱面在换调式后仍然成立」；
 * - 动机用「音程形 + 节奏型」描述，变形算子全部是纯函数，便于用种子复现与单元测试；
 * - 每个档位固定「调式 + 和声策略 + 配器 + 动机族 + 段落曲线」，避免出现只换 tempo 的假差异。
 *
 * 设计参照的工程化手法（详见 `skills/film-scoring/SKILL.md`）：
 * Bernard Herrmann 的 pedal point / 固定低音 ostinato / 集群和弦；Hans Zimmer 的分层
 * ostinato 与分阶段声部进入；Arvo Pärt 的 M-voice + T-voice 双声写法；Ligeti 的微复调
 * （同一音高集合、不同节奏的卡农）；Debussy / Ravel 的平行五度、七九和弦与非功能色彩；
 * 以及电影 cue 通用的「铺垫 → 上升 → 高潮 → 抽空 → 余韵」结构。
 *
 * 全部动机为本项目原创的音程/节奏组合，不复制任何受版权保护的旋律。
 */
namespace TrpgImmersiveMusicTheory {
    export type SectionKind = "bed" | "rise" | "climax" | "void" | "tail";
    export type MotifRole = "statement" | "answer" | "climax" | "fragment";
    export type MotifHarmony = "single" | "fifth" | "chord";

    export type TransformName =
        | "identity"
        | "transpose"
        | "invert"
        | "retrograde"
        | "augment"
        | "diminish"
        | "fragment"
        | "ornament"
        | "octave"
        | "fifth"
        | "chordify";

    export type ColorName =
        | "add9"
        | "sus2"
        | "sus4"
        | "maj7"
        | "min7"
        | "min_maj7"
        | "half_dim"
        | "dim7"
        | "tritone"
        | "cluster"
        | "quartal"
        | "power"
        | "aug"
        | "bII"
        | "sharp11"
        | "plain";

    export interface ModeSpec {
        name: string;
        /** 七个音级相对主音的半音偏移（升序，0 为主音）。 */
        degrees: number[];
    }

    export const MODES: Record<string, ModeSpec> = {
        major: { name: "major", degrees: [0, 2, 4, 5, 7, 9, 11] },
        minor: { name: "minor", degrees: [0, 2, 3, 5, 7, 8, 10] },
        dorian: { name: "dorian", degrees: [0, 2, 3, 5, 7, 9, 10] },
        phrygian: { name: "phrygian", degrees: [0, 1, 3, 5, 7, 8, 10] },
        lydian: { name: "lydian", degrees: [0, 2, 4, 6, 7, 9, 11] },
        mixolydian: { name: "mixolydian", degrees: [0, 2, 4, 5, 7, 9, 10] },
        locrian: { name: "locrian", degrees: [0, 1, 3, 5, 6, 8, 10] },
        harmonic_minor: { name: "harmonic_minor", degrees: [0, 2, 3, 5, 7, 8, 11] },
        whole_tone: { name: "whole_tone", degrees: [0, 2, 4, 6, 8, 10] },
        chromatic: { name: "chromatic", degrees: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11] },
    };

    export interface MotifCell {
        /** 稳定的动机 ID，用于验证「同一档位内确有轮换」。 */
        id: string;
        /** 相对当前和弦根音的半音序列。 */
        degrees: number[];
        /** 每个音的时值（单位：拍，四分音符 = 1）。 */
        rhythm: number[];
        /** 轮廓标签，仅用于可读性与调试，不参与调度。 */
        contour: string;
        /** 播放时的纵向处理；单声、平行五度（空和声）或整块和弦化。 */
        harmony: MotifHarmony;
        /** 动机随版本可整体移调，正负均可。 */
        transpose: number;
    }

    export interface SectionShape {
        kind: SectionKind;
        bars: number;
        /** 0~1，段落的目标响度（还会被逐小节曲线细化）。 */
        dynamic: number;
        /** 0~1，段落的目标声部密度。 */
        density: number;
    }

    export interface ProfileRecipe {
        tempo: number;
        /** 主音的 MIDI 音高（中央 C = 60）。 */
        root: number;
        mode: string;
        strategy: "functional" | "modal" | "pedal" | "nonfunctional" | "ostinato";
        /** 和弦根音在调式中的音级序号（0 = 主和弦，允许负数循环）。 */
        progression: number[];
        /** 该档位允许和必须偏好的和声色彩。 */
        colors: ColorName[];
        /** 配器白名单，按优先级排列。 */
        palette: string[];
        arc: SectionShape[];
        /** 至少三族动机，段落推进时轮换，避免一直重复同一句。 */
        motifFamilies: MotifCell[][];
        motifInstruments: Record<MotifRole, string>;
        reverb: number;
        brightness: number;
        drone: boolean;
        lowPulse: boolean;
        timpani: boolean;
    }

    function cell(
        id: string,
        degrees: number[],
        rhythm: number[],
        contour: string,
        harmony: MotifHarmony = "single",
    ): MotifCell {
        return { id, degrees, rhythm, contour, harmony, transpose: 0 };
    }

    /**
     * 八个氛围档位。每档 = 调式 + 和声策略 + 和弦色彩偏好 + 配器 + 动机族 + 段落曲线。
     * 同一档位的动机族会在段落/循环间轮换，`climax` 强制使用更重的和声色彩。
     */
    export const RECIPES: Record<ImmersiveProfile, ProfileRecipe> = {
        daily: {
            tempo: 78,
            root: 60,
            mode: "dorian",
            strategy: "functional",
            progression: [0, 5, 3, 4, 0],
            colors: ["add9", "sus2", "maj7", "min7", "plain"],
            palette: ["pad", "pluck", "bass", "strings", "bells"],
            arc: [
                { kind: "bed", bars: 8, dynamic: 0.34, density: 0.34 },
                { kind: "rise", bars: 6, dynamic: 0.55, density: 0.52 },
                { kind: "climax", bars: 5, dynamic: 0.68, density: 0.62 },
                { kind: "void", bars: 2, dynamic: 0.24, density: 0.18 },
                { kind: "tail", bars: 7, dynamic: 0.4, density: 0.34 },
            ],
            motifFamilies: [
                [
                    cell("daily-arch", [0, 4, 7, 4], [1, 1, 1, 1], "arch", "single"),
                    cell("daily-answer", [0, -3, 2, 5], [1.5, 0.5, 1, 1], "answering", "single"),
                ],
                [
                    cell("daily-step", [0, 2, 4, 9], [1, 1, 1.5, 0.5], "ascending", "single"),
                    cell("daily-turn", [0, 5, 2, 0], [0.5, 1, 1.5, 1], "turning", "single"),
                ],
                [
                    cell("daily-waltz", [0, 4, 9, 4, 0], [1, 0.5, 1, 0.5, 1], "rocking", "single"),
                ],
            ],
            motifInstruments: { statement: "pluck", answer: "strings", climax: "strings", fragment: "bells" },
            reverb: 0.32,
            brightness: 1080,
            drone: false,
            lowPulse: false,
            timpani: false,
        },
        suspense: {
            tempo: 62,
            root: 50,
            mode: "dorian",
            strategy: "pedal",
            progression: [0, 6, 3, 0, 4],
            colors: ["min7", "sus2", "tritone", "quartal", "half_dim"],
            palette: ["pad", "strings", "bass", "bells", "ostinato", "noise"],
            arc: [
                { kind: "bed", bars: 8, dynamic: 0.22, density: 0.24 },
                { kind: "rise", bars: 8, dynamic: 0.5, density: 0.5 },
                { kind: "climax", bars: 6, dynamic: 0.76, density: 0.68 },
                { kind: "void", bars: 4, dynamic: 0.12, density: 0.1 },
                { kind: "tail", bars: 7, dynamic: 0.34, density: 0.28 },
            ],
            motifFamilies: [
                [
                    cell("suspense-sigh", [0, -1, -5], [1.5, 0.5, 2], "sigh", "single"),
                    cell("suspense-probe", [0, 6, 0], [1, 0.5, 1.5], "probe", "single"),
                ],
                [
                    cell("suspense-step", [0, -2, -3, -7], [1, 1, 1, 1], "descending", "single"),
                    cell("suspense-ring", [0, 5, 2, -1], [0.5, 1.5, 1, 1], "ringing", "single"),
                ],
                [
                    cell("suspense-ostinato", [0, 3, 0, 6], [0.5, 0.5, 0.5, 2.5], "hammer", "single"),
                ],
            ],
            motifInstruments: { statement: "bells", answer: "strings", climax: "strings", fragment: "bells" },
            reverb: 0.56,
            brightness: 680,
            drone: true,
            lowPulse: true,
            timpani: false,
        },
        unease: {
            tempo: 54,
            root: 49,
            mode: "phrygian",
            strategy: "nonfunctional",
            progression: [0, 1, 0, 6, 1],
            colors: ["bII", "half_dim", "cluster", "quartal", "sus4", "tritone"],
            palette: ["pad", "strings", "drone", "bass", "noise", "bells"],
            arc: [
                { kind: "bed", bars: 7, dynamic: 0.2, density: 0.22 },
                { kind: "rise", bars: 7, dynamic: 0.46, density: 0.46 },
                { kind: "climax", bars: 6, dynamic: 0.72, density: 0.64 },
                { kind: "void", bars: 4, dynamic: 0.1, density: 0.08 },
                { kind: "tail", bars: 6, dynamic: 0.28, density: 0.24 },
            ],
            motifFamilies: [
                [
                    cell("unease-crawl", [0, 1, -1, 2], [1, 1, 1, 1], "crawl", "single"),
                    cell("unease-static", [0, -2, 1, -1], [0.5, 1.5, 1, 1], "static", "single"),
                ],
                [
                    cell("unease-shiver", [0, 3, 2, -2], [0.5, 0.5, 1, 2], "shiver", "single"),
                    cell("unease-descend", [0, -1, -4, -6], [1, 1, 1, 1], "descending", "single"),
                ],
                [
                    cell("unease-bell", [0, 2, -3, -7], [2, 0.5, 0.5, 1], "distant", "single"),
                ],
            ],
            motifInstruments: { statement: "strings", answer: "bells", climax: "strings", fragment: "bells" },
            reverb: 0.64,
            brightness: 540,
            drone: true,
            lowPulse: true,
            timpani: false,
        },
        horror: {
            tempo: 44,
            root: 40,
            mode: "locrian",
            strategy: "pedal",
            progression: [0, 1, 0, 6, 0],
            colors: ["half_dim", "dim7", "tritone", "cluster", "bII", "min_maj7"],
            palette: ["drone", "organ", "timpani", "strings", "bass", "noise", "bells", "brass"],
            arc: [
                { kind: "bed", bars: 6, dynamic: 0.18, density: 0.2 },
                { kind: "rise", bars: 6, dynamic: 0.44, density: 0.44 },
                { kind: "climax", bars: 6, dynamic: 0.8, density: 0.7 },
                { kind: "void", bars: 5, dynamic: 0.06, density: 0.06 },
                { kind: "tail", bars: 4, dynamic: 0.2, density: 0.18 },
            ],
            motifFamilies: [
                [
                    cell("horror-descent", [0, -6, -1, -6], [1.5, 0.5, 1.5, 0.5], "descent", "single"),
                    cell("horror-tritone", [0, 6, 0, -6], [0.75, 2, 0.75, 0.5], "probe", "fifth"),
                ],
                [
                    cell("horror-creep", [0, -6, -7, -13], [1, 0.5, 1, 1.5], "creep", "single"),
                    cell("horror-organ", [0, 1, 0, -4], [1, 1, 1, 1], "organum", "fifth"),
                ],
                [
                    cell("horror-claw", [0, 3, -2, -6], [0.5, 0.5, 1.5, 1.5], "claw", "chord"),
                ],
            ],
            motifInstruments: { statement: "strings", answer: "bells", climax: "strings", fragment: "bells" },
            reverb: 0.78,
            brightness: 430,
            drone: true,
            lowPulse: true,
            timpani: true,
        },
        urgency: {
            tempo: 132,
            root: 57,
            mode: "phrygian",
            strategy: "ostinato",
            progression: [0, 0, 3, 1, 0],
            colors: ["power", "sus4", "bII", "min7", "tritone"],
            palette: ["ostinato", "bass", "timpani", "brass", "strings", "noise", "pluck"],
            arc: [
                { kind: "bed", bars: 4, dynamic: 0.42, density: 0.4 },
                { kind: "rise", bars: 6, dynamic: 0.68, density: 0.62 },
                { kind: "climax", bars: 6, dynamic: 0.92, density: 0.82 },
                { kind: "void", bars: 2, dynamic: 0.16, density: 0.12 },
                { kind: "tail", bars: 4, dynamic: 0.38, density: 0.32 },
            ],
            motifFamilies: [
                [
                    cell("urgency-hammer", [0, 0, 3, 0], [0.5, 0.5, 0.5, 2.5], "hammer", "single"),
                    cell("urgency-rise", [0, 3, 0, -1], [0.5, 0.5, 0.5, 0.5], "rising", "single"),
                ],
                [
                    cell("urgency-stab", [0, 7, 0, 3], [0.25, 0.75, 0.5, 2.5], "stab", "chord"),
                    cell("urgency-run", [0, 3, 5, 7], [0.5, 0.5, 0.5, 1.5], "running", "single"),
                ],
                [
                    cell("urgency-climb", [0, 1, 3, 6], [0.5, 0.5, 0.5, 0.5], "climbing", "single"),
                ],
            ],
            motifInstruments: { statement: "pluck", answer: "brass", climax: "brass", fragment: "pluck" },
            reverb: 0.2,
            brightness: 1420,
            drone: false,
            lowPulse: true,
            timpani: true,
        },
        sorrow: {
            tempo: 58,
            root: 55,
            mode: "minor",
            strategy: "functional",
            progression: [0, 3, 5, 0, 4],
            colors: ["add9", "min7", "sus2", "plain", "half_dim"],
            palette: ["strings", "pad", "bass", "bells", "choir"],
            arc: [
                { kind: "bed", bars: 8, dynamic: 0.28, density: 0.3 },
                { kind: "rise", bars: 6, dynamic: 0.48, density: 0.48 },
                { kind: "climax", bars: 5, dynamic: 0.64, density: 0.58 },
                { kind: "void", bars: 3, dynamic: 0.14, density: 0.12 },
                { kind: "tail", bars: 7, dynamic: 0.3, density: 0.26 },
            ],
            motifFamilies: [
                [
                    cell("sorrow-sigh", [0, -3, -5, -3], [1.5, 1, 1, 0.5], "sigh", "single"),
                    cell("sorrow-answer", [0, 2, 0, -4], [1, 1, 1.5, 0.5], "answering", "single"),
                ],
                [
                    cell("sorrow-fall", [0, -4, -5, -7], [1, 1.5, 0.5, 1], "falling", "single"),
                    cell("sorrow-rise", [0, -7, -4, -2], [1, 1, 1, 1], "rising", "single"),
                ],
                [
                    cell("sorrow-memory", [0, -2, -5, -9], [2, 1, 0.5, 0.5], "memory", "single"),
                ],
            ],
            motifInstruments: { statement: "strings", answer: "bells", climax: "strings", fragment: "bells" },
            reverb: 0.58,
            brightness: 780,
            drone: false,
            lowPulse: false,
            timpani: false,
        },
        awe: {
            tempo: 68,
            root: 48,
            mode: "lydian",
            strategy: "pedal",
            progression: [0, 4, 3, 6, 0],
            colors: ["add9", "quartal", "maj7", "sharp11", "power"],
            palette: ["choir", "organ", "strings", "bells", "bass", "timpani", "pad", "brass"],
            arc: [
                { kind: "bed", bars: 8, dynamic: 0.3, density: 0.32 },
                { kind: "rise", bars: 8, dynamic: 0.6, density: 0.58 },
                { kind: "climax", bars: 6, dynamic: 0.86, density: 0.76 },
                { kind: "void", bars: 3, dynamic: 0.18, density: 0.14 },
                { kind: "tail", bars: 7, dynamic: 0.36, density: 0.3 },
            ],
            motifFamilies: [
                [
                    cell("awe-fourth", [0, 5, 10, 12], [1.5, 1.5, 1, 1], "ascending", "single"),
                    cell("awe-open", [0, 7, 12, 14], [2, 1, 0.5, 0.5], "open", "fifth"),
                ],
                [
                    cell("awe-ring", [0, 4, 9, 12], [1, 1, 1, 1], "ringing", "single"),
                    cell("awe-descend", [0, 9, 4, 0], [1, 1, 2, 1], "descending", "single"),
                ],
                [
                    cell("awe-call", [0, 7, 11, 16], [1, 0.5, 1.5, 1], "calling", "single"),
                ],
            ],
            motifInstruments: { statement: "bell", answer: "choir", climax: "strings", fragment: "bells" },
            reverb: 0.72,
            brightness: 1060,
            drone: true,
            lowPulse: false,
            timpani: true,
        },
        aftermath: {
            tempo: 56,
            root: 52,
            mode: "dorian",
            strategy: "pedal",
            progression: [0, 3, 0, 5, 0],
            colors: ["sus2", "min7", "add9", "plain", "quartal"],
            palette: ["drone", "pad", "bells", "strings", "bass"],
            arc: [
                { kind: "bed", bars: 8, dynamic: 0.16, density: 0.2 },
                { kind: "rise", bars: 4, dynamic: 0.3, density: 0.3 },
                { kind: "climax", bars: 3, dynamic: 0.46, density: 0.44 },
                { kind: "void", bars: 4, dynamic: 0.08, density: 0.08 },
                { kind: "tail", bars: 9, dynamic: 0.24, density: 0.22 },
            ],
            motifFamilies: [
                [
                    cell("aftermath-echo", [0, -3, -7, -3], [2, 1, 1, 1], "echo", "single"),
                    cell("aftermath-return", [0, -1, -2, -7], [1.5, 0.5, 1, 1], "returning", "single"),
                ],
                [
                    cell("aftermath-distant", [0, -5, -7, -12], [2, 1, 1, 1], "distant", "single"),
                    cell("aftermath-warm", [0, 2, 5, 2], [1, 1, 1, 1], "warming", "single"),
                ],
                [
                    cell("aftermath-breath", [0, -2, 0, -5], [2, 0.5, 0.5, 1], "breath", "single"),
                ],
            ],
            motifInstruments: { statement: "bells", answer: "strings", climax: "strings", fragment: "bells" },
            reverb: 0.56,
            brightness: 640,
            drone: true,
            lowPulse: false,
            timpani: false,
        },
    };

    const GENTLE_COLORS: ColorName[] = ["add9", "sus2", "maj7", "min7", "plain", "sharp11"];
    const MID_COLORS: ColorName[] = ["sus4", "quartal", "power", "min7", "add9", "tritone"];
    const HARD_COLORS: ColorName[] = ["half_dim", "dim7", "tritone", "cluster", "bII", "aug", "min_maj7"];

    export function modeSpec(name: string, fallback: ModeSpec): ModeSpec {
        return MODES[String(name || "")] || fallback;
    }

    /** 音级序号 → 相对主音的半音偏移；支持负数与跨八度循环。 */
    export function scaleNote(mode: ModeSpec, degree: number): number {
        const size = mode.degrees.length;
        if (!Number.isFinite(degree) || size < 1) return 0;
        const index = Math.trunc(degree);
        const wrapped = ((index % size) + size) % size;
        const octave = Math.floor(index / size);
        const semitone = mode.degrees[wrapped];
        return (semitone === undefined ? 0 : semitone) + octave * 12;
    }

    function chordTones(mode: ModeSpec, degree: number): { third: number; fifth: number } {
        const root = scaleNote(mode, degree);
        return {
            third: scaleNote(mode, degree + 2) - root,
            fifth: scaleNote(mode, degree + 4) - root,
        };
    }

    /**
     * 从调式音级生长出一个和弦。
     * 特殊色彩（那不勒斯 bII、音簇 cluster）会改写根音或强制固定音程，其余色彩保留调式
     * 自身的三度/五度（洛克里亚的减五度、弗里吉亚的 bII 因此不会被「修正」成正三和弦）。
     */
    export function buildChord(mode: ModeSpec, degree: number, color: ColorName): { root: number; intervals: number[] } {
        const root = scaleNote(mode, degree);
        const { third, fifth } = chordTones(mode, degree);
        switch (color) {
            case "bII":
                return { root: root + 1, intervals: [0, 4, 7] };
            case "cluster":
                return { root, intervals: [0, 1, 2, third] };
            case "add9":
                return { root, intervals: [0, third, fifth, 14] };
            case "sus2":
                return { root, intervals: [0, 2, fifth] };
            case "sus4":
                return { root, intervals: [0, 5, fifth] };
            case "maj7":
                return { root, intervals: [0, third, fifth, 11] };
            case "min7":
                return { root, intervals: [0, third, fifth, 10] };
            case "min_maj7":
                return { root, intervals: [0, 3, 7, 11] };
            case "half_dim":
                return { root, intervals: [0, 3, 6, 10] };
            case "dim7":
                return { root, intervals: [0, 3, 6, 9] };
            case "tritone":
                return { root, intervals: [0, 6, 12] };
            case "quartal":
                return { root, intervals: [0, 5, 10] };
            case "power":
                return { root, intervals: [0, 7, 12] };
            case "aug":
                return { root, intervals: [0, 4, 8] };
            case "sharp11":
                return { root, intervals: [0, third, fifth, 18] };
            case "plain":
            default:
                return { root, intervals: [0, third, fifth] };
        }
    }

    /** 按段落选择色彩：铺垫/余韵偏柔和，高潮强制偏向不协和色彩。 */
    export function pickColor(kind: SectionKind, colors: ColorName[], random: () => number): ColorName {
        const preferred = (kind === "climax" ? HARD_COLORS : kind === "rise" ? MID_COLORS : GENTLE_COLORS)
            .filter((item) => colors.indexOf(item) >= 0);
        const pool = preferred.length ? preferred : colors;
        if (!pool.length) return "plain";
        const index = Math.min(pool.length - 1, Math.floor(random() * pool.length));
        return pool[index] || "plain";
    }

    function clampDegree(value: number): number {
        return Math.max(-24, Math.min(24, value));
    }

    /** 动机变形算子：只改音程形/节奏，不改变「这是一句可识别的主题」这一事实。 */
    export function transformMotif(
        source: MotifCell,
        op: TransformName,
        amount = 0,
    ): MotifCell {
        const degrees = source.degrees.slice();
        const rhythm = source.rhythm.slice();
        const next: MotifCell = {
            id: `${source.id}·${op}${amount ? amount : ""}`,
            degrees,
            rhythm,
            contour: source.contour,
            harmony: source.harmony,
            transpose: source.transpose,
        };
        switch (op) {
            case "identity":
                return next;
            case "transpose":
                next.degrees = degrees.map((value) => clampDegree(value + amount));
                return next;
            case "invert": {
                const pivot = degrees[0] || 0;
                next.degrees = degrees.map((value) => clampDegree(pivot - (value - pivot)));
                next.contour = "inverted";
                return next;
            }
            case "retrograde":
                next.degrees = degrees.slice().reverse();
                next.rhythm = rhythm.slice().reverse();
                next.contour = "retrograde";
                return next;
            case "augment":
                next.rhythm = rhythm.map((value) => value * 2);
                return next;
            case "diminish":
                next.rhythm = rhythm.map((value) => Math.max(0.25, value * 0.5));
                return next;
            case "fragment": {
                const keep = Math.max(1, Math.ceil(degrees.length / 2));
                next.degrees = degrees.slice(0, keep);
                next.rhythm = rhythm.slice(0, keep);
                next.contour = "fragment";
                return next;
            }
            case "ornament": {
                const decorated: number[] = [];
                const decoratedRhythm: number[] = [];
                for (let index = 0; index < degrees.length; index += 1) {
                    const value = degrees[index] || 0;
                    decorated.push(value);
                    decoratedRhythm.push(Math.max(0.25, (rhythm[index] || 1) * 0.6));
                    if (index < degrees.length - 1 && decorated.length < 8) {
                        decorated.push(clampDegree(value + (index % 2 === 0 ? 1 : -1)));
                        decoratedRhythm.push(Math.max(0.25, (rhythm[index] || 1) * 0.4));
                    }
                }
                next.degrees = decorated;
                next.rhythm = decoratedRhythm;
                next.contour = "ornamented";
                return next;
            }
            case "octave":
                next.degrees = degrees.map((value, index) => clampDegree(index % 2 === 0 ? value : value + 12));
                return next;
            case "fifth":
                next.harmony = "fifth";
                return next;
            case "chordify":
                next.harmony = "chord";
                return next;
            default:
                return next;
        }
    }

    /** 把动机节奏压缩到指定拍数内，保证一个动机不会跨小节「漏拍」。 */
    export function fitRhythm(source: MotifCell, beats: number): MotifCell {
        const total = source.rhythm.reduce((sum, value) => sum + Math.max(0.25, value), 0);
        const limit = Math.max(0.5, beats);
        if (total <= limit || total <= 0) return source;
        const scale = limit / total;
        return {
            ...source,
            rhythm: source.rhythm.map((value) => Math.max(0.5, value * scale)),
        };
    }

    /** 段落之间的连贯：优先复用上一个和弦作为枢轴，否则落在主/属和弦上。 */
    export function pivotDegree(progression: number[], index: number, fallback: number): number {
        if (!progression.length) return fallback;
        const value = progression[index % progression.length];
        return Number.isFinite(value as number) ? (value as number) : fallback;
    }
}
