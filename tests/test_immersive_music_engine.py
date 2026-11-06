"""配乐引擎的结构性质测试。

这些断言验证的不是「听感」，而是可以被确定性检查的作曲性质：
- 每档场景都走完整的 cue 结构（铺垫/上升/高潮/抽空/余韵）；
- 动态曲线与留白不是常数；
- 动机在段落间轮换、同种子可复现；
- ostinato 分层与定音鼓/铜管按时进入；
- 旧导演乐谱仍然兼容，新增可选字段（pedal / motif_variants）生效；
- 非法乐谱回落到档位配方而不是让播放链路崩溃。

测试对象是 `npm run build:frontend` 产物里的纯数据模块（music-theory / music-arranger），
通过 Node 探针执行；未构建前端时自动跳过，避免在无 Node 环境里误报。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from trpg_server.immersive.auto_score import build_auto_score  # noqa: E402

MODULE_DIR = ROOT / "dist" / "public" / "js" / "immersive"
PROBE = Path(__file__).resolve().parent / "tools" / "music_cue_probe.cjs"

REQUIRED_FILES = ("music-theory.js", "music-arranger.js")
AUDIT_PROFILES = ("daily", "suspense", "horror")


def _node_binary() -> str | None:
    return shutil.which("node")


@pytest.fixture(scope="module")
def cue_probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    node = _node_binary()
    if node is None:
        pytest.skip("未安装 Node，跳过前端配乐结构测试")
    missing = [name for name in REQUIRED_FILES if not (MODULE_DIR / name).is_file()]
    if missing:
        pytest.skip(f"未找到前端构建产物：{', '.join(missing)}")
    score_file = tmp_path_factory.mktemp("music") / "auto_scores.json"
    scores = {
        profile: build_auto_score(profile=profile, intensity="medium", section=0, seed="room:probe")
        for profile in AUDIT_PROFILES
    }
    score_file.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [node, str(PROBE), str(MODULE_DIR), str(score_file)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"配乐探针执行失败：{completed.stderr.strip()}")
    return json.loads(completed.stdout)


def test_all_profiles_have_full_cue_structure(cue_probe: dict) -> None:
    profiles = cue_probe["profiles"]
    assert len(profiles) == 8
    for profile in profiles:
        kinds = profile["kinds"]
        assert kinds, profile["profile"]
        assert set(kinds) == {"bed", "rise", "climax", "void", "tail"}, profile["profile"]
        assert 20 <= profile["total_bars"] <= 56, (profile["profile"], profile["total_bars"])
        assert profile["bar_duration"] > 0
        assert profile["tempo"] > 0
        # 段落推进必须真的换和弦，而不是整段一个和弦垫。
        assert len(set(profile["chord_roots"])) >= 3, profile["profile"]


def test_dynamics_and_silence_are_not_constant(cue_probe: dict) -> None:
    for profile in cue_probe["profiles"]:
        dynamics = profile["dynamics"]
        assert max(dynamics) - min(dynamics) >= 0.3, profile["profile"]
        # v1.0.1：留白不再整小节静音（旧实现每 3 小节插一个 silence 小节，
        # horror 44 BPM 下等于连续 5.45 秒没有推进，听感就是播放中断）。
        # 现在改为「抽掉和弦 + 压低总线 + 心跳/颗粒托底」的低电平空洞。
        assert profile["silence_bars"] == 0, profile["profile"]
        assert profile["hollow_bars"] >= 1, profile["profile"]
        # 高潮必须明显高于开场铺垫。
        climax_levels = [level for level, kind in zip(dynamics, profile["kinds"]) if kind == "climax"]
        bed_levels = [level for level, kind in zip(dynamics, profile["kinds"]) if kind == "bed"]
        assert climax_levels and bed_levels
        assert max(climax_levels) > max(bed_levels) + 0.25, profile["profile"]


def test_motifs_rotate_and_use_scene_specific_instruments(cue_probe: dict) -> None:
    for profile in cue_probe["profiles"]:
        motifs = profile["motif_ids"]
        assert len(motifs) >= 3, profile["profile"]
        assert len(set(motifs)) >= 2, profile["profile"]
        instruments = set(profile["motif_instruments"])
        assert instruments, profile["profile"]
        if profile["profile"] in {"suspense", "horror", "awe", "aftermath"}:
            assert "bell" in instruments or "bells" in instruments, profile["profile"]


def test_ostinato_layering_and_percussion_entries(cue_probe: dict) -> None:
    by_profile = {item["profile"]: item for item in cue_probe["profiles"]}
    suspense = by_profile["suspense"]
    urgency = by_profile["urgency"]
    horror = by_profile["horror"]
    assert max(suspense["ostinato_layers"]) >= 2
    assert max(urgency["ostinato_layers"]) >= 3
    assert max(urgency["ostinato_steps"]) >= 16
    assert horror["timpani_hits"] > 0
    assert urgency["timpani_hits"] > 0
    assert horror["brass_bars"] >= 1
    assert urgency["brass_bars"] >= 1
    assert horror["bell_bars"] >= 1


def test_intensity_changes_tempo_density_and_peak(cue_probe: dict) -> None:
    high = cue_probe["intensity_high"]
    low = cue_probe["intensity_low"]
    assert high["tempo"] > low["tempo"]
    assert max(high["ostinato_layers"]) >= max(low["ostinato_layers"])
    assert high["brass_bars"] >= low["brass_bars"]
    # 强度还会改变基础电平与声部密度；动态曲线本身仍保持段落形状。
    assert max(high["dynamics"]) - min(high["dynamics"]) >= 0.3
    assert max(low["dynamics"]) - min(low["dynamics"]) >= 0.3


def test_seed_reproducibility_and_cycle_rotation(cue_probe: dict) -> None:
    assert cue_probe["determinism_a"]["timeline_json"] == cue_probe["determinism_b"]["timeline_json"]
    assert cue_probe["determinism_a"]["timeline_json"] != cue_probe["determinism_other"]["timeline_json"]
    assert cue_probe["cycle_0"] != cue_probe["cycle_1"]
    cycle_zero = json.loads(cue_probe["cycle_0"])
    cycle_one = json.loads(cue_probe["cycle_1"])
    motifs_zero = [bar["motif"]["cell"]["id"] for bar in cycle_zero if bar["motif"]]
    motifs_one = [bar["motif"]["cell"]["id"] for bar in cycle_one if bar["motif"]]
    assert set(motifs_zero) != set(motifs_one)


def test_director_score_compatibility_and_optional_extensions(cue_probe: dict) -> None:
    director = cue_probe["director"]
    legacy = director["legacy"]
    extended = director["extended"]
    assert legacy["explicit_chords"] >= 2
    assert set(legacy["kinds"]) >= {"climax", "void", "tail"}
    assert director["explicit_chord_roots"] == [53, 51]

    assert extended["pedal"] == 48
    assert extended["mode"] == "phrygian"
    assert all(motif.startswith("score-motif-") for motif in extended["motif_ids"])
    assert any("score-motif-1" in motif for motif in extended["motif_ids"])

    invalid = director["invalid"]
    assert invalid["explicit_chords"] == 0
    assert invalid["total_bars"] >= 20
    assert set(invalid["kinds"]) == {"bed", "rise", "climax", "void", "tail"}


def test_auto_score_sections_are_long_enough_and_unique(cue_probe: dict) -> None:
    scored = cue_probe["scored"]
    assert scored, "探针没有收到自动乐谱"
    for profile, data in scored.items():
        runs = data["section_runs"]
        ids = [run["id"] for run in runs]
        assert len(ids) == len(set(ids)), (profile, ids)
        assert len(runs) >= 5, (profile, ids)
        # 叙事段落至少 8 小节（void 见下一行单独约束）。
        assert data["min_melodic_section_bars"] >= 8, (profile, data["min_melodic_section_bars"])
        # 整条 cue 以「时长」而不是固定小节数衡量：留白段收敛后，
        # 慢速档位少几小节并不代表曲子变短。
        assert data["total_bars"] >= 30, (profile, data["total_bars"])
        assert data["total_bars"] * data["bar_duration"] >= 60, (profile, data["total_bars"], data["bar_duration"])
        # 叙事段落至少 8 小节；void 呼吸段按秒数收敛到 2–4 小节（约 6–11 秒），
        # 避免慢速档位出现 40 秒以上的空档。
        assert all(
            run["bars"] >= 8 if run["kind"] != "void" else 2 <= run["bars"] <= 4
            for run in runs
        ), (profile, runs)
        for run in runs:
            if run["kind"] == "void":
                continue
            roots = {
                row["chordRoot"]
                for row in data["rows"][run["startBar"] : run["endBar"] + 1]
                if row["chordRoot"] is not None
            }
            assert len(roots) >= 2, (profile, run["id"], roots)


def test_sections_have_audible_transitions(cue_probe: dict) -> None:
    scored = cue_probe["scored"]
    for profile, data in scored.items():
        pivots = data["pivots"]
        assert len(pivots) >= 4, (profile, pivots)
        for pivot in pivots:
            assert pivot["transition"] in {"pickup", "pivot", "breath"}, (profile, pivot)
            # 进入高潮/余韵必须有动机承接，进入留白则用呼吸桥段。
            assert pivot["transition"] == ("breath" if pivot["to"] == "void" else "pickup"), (profile, pivot)
            # 枢轴和弦：每个段落边界都带上一段末和弦，而不是硬切。
            assert pivot["carriedChord"] is not None, (profile, pivot)


def test_no_short_loop_and_bounded_motif_repetition(cue_probe: dict) -> None:
    scored = cue_probe["scored"]
    for profile, data in scored.items():
        # ostinato 不允许在相邻两小节逐音符原样重复（只允许单小节自身循环一次）。
        assert data["max_identical_ostinato_run"] <= 1, (profile, data["max_identical_ostinato_run"])
        # 跨段落/跨循环的动机族由 id（含变形算子）区分，同一 id 最多出现 2 次。
        assert data["max_motif_repeat"] <= 2, (profile, data["motif_counts"])


def test_engine_handoff_starts_at_current_dramatic_phase(cue_probe: dict) -> None:
    scored = cue_probe["scored"]
    for profile, data in scored.items():
        handoff = data["handoff_bars"]
        # 高潮与余韵一定在 cue 中段之后；换歌从这里切入就不会每次都从第 0 小节重放。
        assert handoff["climax"] > 0, (profile, handoff)
        assert handoff["tail"] > handoff["climax"], (profile, handoff)
        # 若开篇不是 rise（例如以 bed 铺底），rise 也必须指向真正的中段起点。
        first_run = data["section_runs"][0]
        if first_run["kind"] != "rise":
            assert handoff["rise"] >= first_run["bars"], (profile, handoff)


def test_cycle_openings_are_not_identical(cue_probe: dict) -> None:
    scored = cue_probe["scored"]
    for profile, data in scored.items():
        openings = [json.dumps(item, sort_keys=True) for item in data["cycles"]]
        assert len(openings) >= 2
        assert len(set(openings)) >= 2, (profile, openings)


def test_room_sync_maps_same_position_to_same_bar(cue_probe: dict) -> None:
    sync = cue_probe["sync"]
    # 同一个共享位置，任意玩家/任意进程都必须落到同一小节、同一和弦。
    assert sync["same_position_a"] == sync["same_position_b"]
    # 小节内的位置对齐到同一小节边界。
    assert sync["fractional_early"]["barIndex"] == sync["fractional_late"]["barIndex"]
    assert sync["fractional_early"]["cycle"] == 0
    # 中途加入的玩家听到的是 cue 中段，而不是从第 0 小节重播。
    assert sync["fresh_join"]["barIndex"] > 0
    assert sync["fresh_join"]["section"] != "bed"
    # 超过一个完整 cue 后进入下一次循环。
    assert sync["wrap"]["cycle"] >= 1
    assert sync["wrap"]["barIndex"] == 4
