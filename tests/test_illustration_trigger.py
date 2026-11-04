"""3A：配图触发分层与 AI KP 决策降级的单元测试。"""
from __future__ import annotations

from pathlib import Path

from trpg_server.immersive import illustration_director
from trpg_server.immersive.config import resolve_illustration_trigger


def _settings(**overrides):
    base = {
        "illustration_enabled": True,
        "illustration_auto": True,
        "illustration_trigger": "important",
        "illustration_trigger_allowed": ["important", "turn_interval", "frequent"],
        "illustration_kp_daily_budget": 5,
        "illustration_kp_max_context_chars": 500,
    }
    base.update(overrides)
    return base


def test_trigger_layering_site_personal_room():
    settings = _settings()
    assert resolve_illustration_trigger(settings, {}) == "important"
    # 个人覆盖在允许范围内生效
    assert resolve_illustration_trigger(settings, {}, "frequent") == "frequent"
    # 不在允许集合内则不生效
    assert resolve_illustration_trigger(_settings(illustration_trigger_allowed=["important"]), {}, "frequent") == "important"
    # 房间只能收紧
    assert resolve_illustration_trigger(settings, {"immersive_illustration_trigger": "off"}, "frequent") == "off"
    assert resolve_illustration_trigger(settings, {"immersive_illustration_trigger": "important"}, "frequent") == "important"
    # 房间不能把站点/个人关闭放宽
    assert resolve_illustration_trigger(settings, {"immersive_illustration_trigger": "important"}, "off") == "off"
    # 站点关闭后房间与个人都无法开启
    assert resolve_illustration_trigger(_settings(illustration_enabled=False), {"immersive_illustration_trigger": "important"}, "frequent") == "off"


def test_validate_decision_rejects_bad_shapes():
    assert illustration_director.validate_decision("not json")[0] is None
    assert illustration_director.validate_decision({"should_illustrate": "yes", "scene_prompt": "x"})[0] is None
    assert illustration_director.validate_decision({"should_illustrate": True, "scene_prompt": "  "})[0] is None
    ok, errors = illustration_director.validate_decision(
        {"should_illustrate": True, "scene_prompt": "雾气中的石桥", "npcs": [{"name": "老船夫"}]}
    )
    assert ok is not None and not errors
    assert ok["should_illustrate"] is True


def test_fallback_decision_respects_trigger():
    analysis = {"scene": "河岸"}
    assert illustration_director.fallback_decision(trigger="important", analysis=analysis)["should_illustrate"] is False
    assert illustration_director.fallback_decision(
        trigger="important", analysis=analysis, important_signal=True
    )["should_illustrate"] is True
    assert illustration_director.fallback_decision(trigger="frequent", analysis=analysis)["should_illustrate"] is True
    assert illustration_director.fallback_decision(trigger="turn_interval", analysis=analysis)["should_illustrate"] is True
    assert illustration_director.fallback_decision(trigger="off", analysis=analysis)["should_illustrate"] is False


def test_kp_run_never_raises_and_degrades(tmp_path: Path):
    def boom(**_kwargs):
        raise RuntimeError("upstream down")

    result = illustration_director.run(
        room_id="room-a",
        room_dir=tmp_path,
        text="玩家推开了门。",
        analysis={"scene": "走廊"},
        structured=None,
        settings=_settings(),
        trigger="frequent",
        usage_path=tmp_path / "kp_usage.json",
        request_fn=boom,
    )
    assert result["fallback"] is True
    assert result["validation"] == "error"
    assert "upstream down" in result["fallback_reason"]
    assert result["decision"]["should_illustrate"] is True


def test_kp_budget_gate_degrades_to_rules(tmp_path: Path):
    usage = tmp_path / "kp_usage.json"
    usage.write_text('{"date": "' + illustration_director._today() + '", "calls": 5}', encoding="utf-8")

    called = {"value": False}

    def should_not_call(**_kwargs):
        called["value"] = True
        return {"raw": {}}

    result = illustration_director.run(
        room_id="room-b",
        room_dir=tmp_path,
        text="继续前进。",
        analysis={"scene": "隧道"},
        structured=None,
        settings=_settings(illustration_kp_daily_budget=5),
        trigger="important",
        important_signal=False,
        usage_path=usage,
        request_fn=should_not_call,
    )
    assert called["value"] is False
    assert result["fallback_reason"] == "illustration_kp_budget_exhausted"
    assert result["decision"]["should_illustrate"] is False
