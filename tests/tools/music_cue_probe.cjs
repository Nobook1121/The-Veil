/**
 * 配乐编排探针：在 Node 里加载编译后的乐理/编配模块，输出可被 pytest 断言的结构数据。
 *
 * 只依赖 `dist/public/js/immersive` 下的纯数据模块（music-theory / music-arranger），
 * 不触碰 Web Audio 与 DOM，因此可以在 CI 中稳定验证「段落推进、动机轮换、动态曲线、种子复现」。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const baseDir = process.argv[2];
if (!baseDir) {
    process.stderr.write("missing module directory\n");
    process.exit(2);
}

const files = ["music-theory.js", "music-arranger.js"];
const source = files
    .map((name) => fs.readFileSync(path.join(baseDir, name), "utf8"))
    .join("\n;\n");

const sandbox = { console, Math, Number, JSON, Object, Array, String, Boolean, isFinite, parseInt, parseFloat };
vm.createContext(sandbox);
vm.runInContext(
    `${source}\n;globalThis.__modules = { theory: TrpgImmersiveMusicTheory, arranger: TrpgImmersiveMusicArranger };`,
    sandbox,
);

const { theory, arranger } = sandbox.__modules;

function makePlan(profile, intensity, seed, extra) {
    return Object.assign(
        {
            room_id: "probe-room",
            profile,
            intensity,
            seed,
            motif: `${profile}-1`,
            section: 2,
            volume: 0.35,
            crossfade_ms: 3000,
            duck_bgm: true,
        },
        extra || {},
    );
}

function summarize(profile, intensity, seed, score) {
    const plan = makePlan(profile, intensity, seed, score ? { score } : null);
    const spec = arranger.resolveSpec(plan);
    const timeline = arranger.buildTimeline(spec, 0);
    const bars = timeline.bars;
    return {
        profile,
        tempo: spec.tempo,
        bar_duration: Number(spec.barDuration.toFixed(3)),
        total_bars: bars.length,
        kinds: bars.map((bar) => bar.section),
        dynamics: bars.map((bar) => Number(bar.dynamic.toFixed(3))),
        silence_bars: bars.filter((bar) => bar.silence).length,
        // 留白段的和弦被抽空，但仍有心跳/颗粒/钟声托底：
        // 用「无和弦小节数」衡量空洞，而不是用全静音小节数。
        hollow_bars: bars.filter((bar) => !bar.chord).length,
        chord_roots: bars.filter((bar) => bar.chord).map((bar) => bar.chord.root),
        motif_ids: bars.filter((bar) => bar.motif).map((bar) => bar.motif.cell.id),
        motif_instruments: bars.filter((bar) => bar.motif).map((bar) => bar.motif.instrument),
        ostinato_layers: bars.map((bar) => (bar.ostinato ? bar.ostinato.layers : 0)),
        ostinato_steps: bars.map((bar) => (bar.ostinato ? bar.ostinato.steps : 0)),
        timpani_hits: bars.reduce((sum, bar) => sum + (bar.timpani ? bar.timpani.hits.length : 0), 0),
        brass_bars: bars.filter((bar) => bar.brass).length,
        bell_bars: bars.filter((bar) => bar.bell !== null).length,
        pedal: spec.pedal,
        mode: spec.mode.name,
        explicit_chords: spec.explicitChords ? spec.explicitChords.length : 0,
        timeline_json: JSON.stringify(bars),
    };
}

const profiles = ["daily", "suspense", "unease", "horror", "urgency", "sorrow", "awe", "aftermath"];
const output = {
    profiles: profiles.map((profile) => summarize(profile, "medium", `probe-${profile}`)),
    intensity_high: summarize("horror", "high", "probe-intensity-high"),
    intensity_low: summarize("horror", "low", "probe-intensity-low"),
    determinism_a: summarize("suspense", "medium", "shared-seed"),
    determinism_b: summarize("suspense", "medium", "shared-seed"),
    determinism_other: summarize("suspense", "medium", "other-seed"),
    cycle_0: JSON.stringify(arranger.buildTimeline(arranger.resolveSpec(makePlan("horror", "medium", "cycle-probe")), 0).bars),
    cycle_1: JSON.stringify(arranger.buildTimeline(arranger.resolveSpec(makePlan("horror", "medium", "cycle-probe")), 1).bars),
    director: {},
};

/** 把引擎 `playOstinato` 的取音规则镜像到探针里，得到可比较的实际音符序列。 */
function ostinatoNotes(bar) {
    if (!bar.ostinato || !bar.chord) return null;
    const plan = bar.ostinato;
    const notes = [];
    const registerOffsets = [0, 12, -12];
    for (let layer = 0; layer < plan.layers; layer += 1) {
        const register = registerOffsets[layer % registerOffsets.length] || 0;
        for (let step = 0; step < plan.steps; step += 1) {
            if (plan.pattern[step % plan.pattern.length] !== 1) continue;
            const index = (step + layer * plan.canonOffset) % plan.offsets.length;
            const offset = plan.offsets[index] || 0;
            notes.push(bar.chord.root + offset + plan.register + register);
        }
    }
    return notes;
}

function motifInfo(bar) {
    const plan = bar.motif || bar.counter;
    if (!plan || !bar.chord) return null;
    return {
        id: plan.cell.id,
        role: plan.role,
        instrument: plan.instrument,
        notes: plan.cell.degrees.map((degree) => bar.chord.root + plan.octave + degree),
        startBeat: Number(plan.startBeat.toFixed(2)),
    };
}

function sectionRuns(bars) {
    const runs = [];
    bars.forEach((bar) => {
        const last = runs[runs.length - 1];
        if (last && last.id === bar.sectionId) {
            last.bars += 1;
            last.endBar = bar.index;
        } else {
            runs.push({
                id: bar.sectionId,
                kind: bar.section,
                startBar: bar.index,
                endBar: bar.index,
                bars: 1,
                transition: bar.transition ? bar.transition.kind : null,
                from: bar.transition ? bar.transition.from : null,
            });
        }
    });
    return runs;
}

function auditScore(profile, score) {
    const plan = makePlan(profile, "medium", `audit-${profile}`, { score, section: 0 });
    const spec = arranger.resolveSpec(plan);
    const timeline = arranger.buildTimeline(spec, 0);
    const bars = timeline.bars;
    const rows = bars.map((bar) => ({
        index: bar.index,
        section: bar.section,
        sectionId: bar.sectionId,
        sectionBar: bar.sectionBar,
        start: Number((bar.index * timeline.barDuration).toFixed(2)),
        duration: Number(timeline.barDuration.toFixed(2)),
        chordRoot: bar.chord ? bar.chord.root : null,
        transition: bar.transition ? bar.transition.kind : null,
        ostinato: ostinatoNotes(bar),
        motif: motifInfo(bar),
    }));
    const runs = sectionRuns(bars);
    // 只统计「非空」的 ostinato 连续相同次数；空小节不参与比较。
    let run = 0;
    let maxOstRun = 0;
    for (let index = 0; index < rows.length; index += 1) {
        const current = rows[index].ostinato;
        const previous = index > 0 ? rows[index - 1].ostinato : null;
        if (!current) {
            run = 0;
            continue;
        }
        run = previous && JSON.stringify(current) === JSON.stringify(previous) ? run + 1 : 1;
        maxOstRun = Math.max(maxOstRun, run);
    }
    const motifCounts = {};
    rows.forEach((row) => {
        if (!row.motif) return;
        motifCounts[row.motif.id] = (motifCounts[row.motif.id] || 0) + 1;
    });
    const melodic = runs.filter((item) => item.kind !== "void");
    const cycles = [0, 1, 2].map((cycle) => {
        const cycleTimeline = arranger.buildTimeline(spec, cycle);
        return cycleTimeline.bars.slice(0, 4).map((bar) => ({
            section: bar.section,
            chordRoot: bar.chord ? bar.chord.root : null,
            ostinato: ostinatoNotes(bar),
            motifId: bar.motif ? bar.motif.cell.id : bar.counter ? bar.counter.cell.id : null,
        }));
    });
    const pivots = runs.slice(1).map((item, index) => {
        const previousRun = runs[index];
        const previousBar = bars[previousRun.endBar];
        const firstBar = bars[item.startBar];
        return {
            from: previousRun.kind,
            to: item.kind,
            transition: item.transition,
            carriedChord: firstBar.transition ? firstBar.transition.chordRoot : null,
            previousChord: previousBar && previousBar.chord ? previousBar.chord.root : null,
            firstChord: firstBar && firstBar.chord ? firstBar.chord.root : null,
        };
    });
    return {
        bar_duration: Number(timeline.barDuration.toFixed(3)),
        total_bars: bars.length,
        section_runs: runs,
        min_melodic_section_bars: melodic.length ? Math.min(...melodic.map((item) => item.bars)) : 0,
        max_identical_ostinato_run: maxOstRun,
        max_motif_repeat: Object.keys(motifCounts).length ? Math.max(...Object.values(motifCounts)) : 0,
        motif_counts: motifCounts,
        handoff_bars: {
            bed: arranger.sectionStartBar(timeline, "bed"),
            rise: arranger.sectionStartBar(timeline, "rise"),
            climax: arranger.sectionStartBar(timeline, "climax"),
            tail: arranger.sectionStartBar(timeline, "tail"),
        },
        pivots,
        cycles,
        rows,
    };
}

/**
 * 镜像引擎的 `seekToOrdinal`：服务端只下发「此刻第几小节」，
 * 各客户端都必须落到同一个 (cycle, barIndex)，新加入的玩家才不会从头重播。
 */
function startFromPosition(spec, positionBars) {
    let timeline = arranger.buildTimeline(spec, 0);
    const ordinal = Math.max(0, Math.ceil(positionBars));
    const total = Math.max(1, timeline.totalBars);
    let cycle = Math.floor(ordinal / total);
    let index = ordinal - cycle * total;
    if (cycle !== 0) {
        timeline = arranger.buildTimeline(spec, cycle);
        const rebuilt = Math.max(1, timeline.totalBars);
        if (rebuilt !== total) {
            cycle = Math.floor(ordinal / rebuilt);
            index = ordinal - cycle * rebuilt;
            if (cycle !== 0) timeline = arranger.buildTimeline(spec, cycle);
        }
    }
    const bar = timeline.bars[Math.min(Math.max(0, index), timeline.bars.length - 1)];
    return {
        position_bars: positionBars,
        ordinal,
        cycle,
        barIndex: Math.max(0, index),
        section: bar ? bar.section : null,
        sectionId: bar ? bar.sectionId : null,
        chordRoot: bar && bar.chord ? bar.chord.root : null,
        ostinato: bar ? ostinatoNotes(bar) : null,
    };
}

const syncSpec = arranger.resolveSpec(makePlan("horror", "medium", "sync-probe"));
const syncTimeline = arranger.buildTimeline(syncSpec, 0);
output.sync = {
    total_bars: syncTimeline.totalBars,
    bar_duration: Number(syncTimeline.barDuration.toFixed(3)),
    // 同一 position 必须得到完全相同的落点（不同玩家/不同进程都一致）。
    same_position_a: startFromPosition(syncSpec, 12.4),
    same_position_b: startFromPosition(syncSpec, 12.4),
    // 落在小节内的两个时间点对齐到同一个小节边界。
    fractional_early: startFromPosition(syncSpec, 12.2),
    fractional_late: startFromPosition(syncSpec, 12.9),
    // 跨过一整个 cue 后进入下一次循环，而不是回到第 0 小节。
    wrap: startFromPosition(syncSpec, syncTimeline.totalBars + 3.5),
    fresh_join: startFromPosition(syncSpec, 28.6),
};

const scorePath = process.argv[3];
if (scorePath && fs.existsSync(scorePath)) {
    const scores = JSON.parse(fs.readFileSync(scorePath, "utf8"));
    output.scored = Object.keys(scores).reduce((acc, profile) => {
        acc[profile] = auditScore(profile, scores[profile]);
        return acc;
    }, {});
} else {
    output.scored = {};
}

const directorScore = {
    scene: "潮湿的地窖",
    mood: "horror",
    mode: "phrygian",
    tempo: 60,
    chords: [
        { root: 5, quality: "minor", intervals: [0, 3, 7] },
        { root: 3, quality: "dim", intervals: [0, 3, 6] },
    ],
    instrumentation: ["strings", "drone", "timpani"],
    motif: [0, 4, 7, 4],
    sections: [
        { label: "沉入", dynamic: "low", bars: 6 },
        { label: "惊变", dynamic: "high", bars: 6 },
    ],
    dynamics: [0.2, 0.85],
    confidence: 80,
};

const extendedScore = Object.assign({}, directorScore, {
    pedal: 0,
    motif_variants: [
        { degrees: [0, -2, -7], rhythm: [1.5, 0.5, 2], role: "statement" },
        { degrees: [0, 6, 0, -6], rhythm: [0.75, 1.5, 0.75, 1], role: "climax" },
    ],
});
const legacyScore = Object.assign({}, directorScore);
delete legacyScore.pedal;
delete legacyScore.motif_variants;

output.director.legacy = summarize("horror", "medium", "director-legacy", legacyScore);
output.director.extended = summarize("horror", "medium", "director-extended", extendedScore);
output.director.invalid = summarize("horror", "medium", "director-invalid", Object.assign({}, directorScore, { tempo: 0 }));
output.director.explicit_chord_roots = arranger
    .resolveSpec(makePlan("horror", "medium", "director-roots", { score: legacyScore }))
    .explicitChords.map((chord) => chord.root);

process.stdout.write(JSON.stringify(output));
