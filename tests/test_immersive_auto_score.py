"""本地自动配乐的契约测试：无需 LLM 也能生成可演奏、可复现的乐谱。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from trpg_server.immersive.auto_score import build_auto_score  # noqa: E402
from trpg_server.immersive.director_schema import validate_score  # noqa: E402
from trpg_server.immersive.music import plan_music  # noqa: E402


def test_auto_score_is_valid_for_every_profile() -> None:
    for profile in ("daily", "suspense", "unease", "horror", "urgency", "sorrow", "awe", "aftermath"):
        for intensity in ("low", "medium", "high"):
            score = build_auto_score(profile=profile, intensity=intensity, section=0, seed="room:7")
            parsed, errors, confidence = validate_score(score)
            assert parsed is not None, (profile, intensity, errors)
            assert not errors, (profile, intensity, errors)
            assert confidence >= 0


def test_auto_score_is_deterministic_and_rotates_by_section() -> None:
    first = build_auto_score(profile="horror", intensity="medium", section=0, seed="room:7")
    same = build_auto_score(profile="horror", intensity="medium", section=0, seed="room:7")
    later = build_auto_score(profile="horror", intensity="medium", section=3, seed="room:7")
    other_seed = build_auto_score(profile="horror", intensity="medium", section=0, seed="room:8")
    assert first == same
    assert first["chords"] != later["chords"]
    assert first != other_seed


def test_intensity_is_expression_not_a_new_piece() -> None:
    """强度只改 tempo/演奏力度，不得重新随机化曲子，否则前端每回合都会从开头重放。"""
    low = build_auto_score(profile="horror", intensity="low", section=2, seed="room:7")
    medium = build_auto_score(profile="horror", intensity="medium", section=2, seed="room:7")
    high = build_auto_score(profile="horror", intensity="high", section=2, seed="room:7")
    for field in ("mode", "chords", "motif_variants", "sections", "dynamics", "instrumentation", "pedal"):
        assert low.get(field) == medium.get(field), field
        assert high.get(field) == medium.get(field), field
    assert low["tempo"] < medium["tempo"] < high["tempo"]


def test_auto_score_has_cue_structure_and_motifs() -> None:
    score = build_auto_score(profile="horror", intensity="high", section=1, seed="room:7")
    assert [section["dynamic"] for section in score["sections"]] == ["low", "medium", "high", "low"]
    assert len(score["sections"]) == 4
    assert len(score["motif_variants"]) >= 3
    assert max(score["dynamics"]) - min(score["dynamics"]) >= 0.5
    assert score["pedal"] in range(12)
    # 恐怖档不允许以主音大三和弦开场（bII 那不勒斯是允许的例外）。
    assert score["chords"][0]["quality"] != "major"


def test_plan_music_attaches_auto_score_without_director(tmp_path: Path) -> None:
    analysis = {"profile": "horror", "intensity": "high", "scene": "石壁后传来刮擦声"}
    plan = plan_music(
        room_dir=tmp_path,
        room_id="room-auto",
        analysis=analysis,
        settings={"music_seed": "auto-score"},
    )
    assert plan["score"] is not None
    assert plan["score"]["mood"] == "horror"
    parsed, errors, _ = validate_score(plan["score"])
    assert parsed is not None
    assert not errors

    again = plan_music(
        room_dir=tmp_path,
        room_id="room-auto",
        analysis=analysis,
        settings={"music_seed": "auto-score"},
    )
    assert again["score"] == plan["score"]
