"""叙事回合编排：分析 → 配乐导演 → 配图队列 → 广播/日志。

入口 ``on_narrative_turn`` 由 ``routes/chat.py`` 在叙事返回前调用，**永不抛错**：
任何异常只记录并返回空结果，主叙事流程不受影响。所有日志都带触发原因、档位、
耗时与降级原因，便于追溯「为什么切了这首歌 / 为什么没出图」。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from trpg_server.immersive import broadcast, director, image_platforms
from trpg_server.immersive.analyzer import analyze_narrative
from trpg_server.immersive.config import load_immersive_settings, resolve_room_switches
from trpg_server.immersive.illustration import (
    generate_now,
    history_for_room,
    load_room_state,
    next_turn_index,
    should_auto_generate,
    submit_generation,
    usage_snapshot,
)
from trpg_server.immersive.music import apply_director
from trpg_server.immersive.music import load_state as load_music_state
from trpg_server.immersive.music import plan_music
from trpg_server.room_log import log_room_event

logger = logging.getLogger(__name__)

# 旧单条生图配置的迁移每个配置目录只尝试一次，避免每回合都写 general.toml。
_MIGRATED_CONFIG_DIRS: set[str] = set()


def _ensure_legacy_migrated(config_dir: Path | str | None) -> None:
    key = str(config_dir or "")
    if key in _MIGRATED_CONFIG_DIRS:
        return
    _MIGRATED_CONFIG_DIRS.add(key)
    try:
        image_platforms.migrate_legacy(config_dir)
    except Exception:  # noqa: BLE001 - 迁移失败不能影响叙事
        logger.exception("immersive image platform legacy migration failed")


def _room_log(room_dir: Path | None, room_id: str, message: str, *, level: str = "info", **details: Any) -> None:
    try:
        log_room_event(
            room_dir,
            message,
            level=level,
            actor="沉浸系统",
            source="immersive",
            room_id=room_id,
            logger_=logger,
            **details,
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响主流程
        logger.debug("immersive room log failed", exc_info=True)


def _log_director(
    room_dir: Path | None,
    room_id: str,
    *,
    result: dict[str, Any],
    model: str,
    platform: str,
    elapsed_ms: float,
    confidence: int,
) -> None:
    """导演链路可追溯日志：触发原因、指纹、缓存、模型耗时、校验与回退。"""
    validation = str(result.get("validation") or "")
    level = "info" if validation in {"ok", "cache_hit"} else "warning"
    _room_log(
        room_dir,
        room_id,
        f"AI 音乐导演：{result.get('profile') or '-'} 第 {result.get('section') or 0} 段"
        + ("（缓存命中）" if result.get("cached") else ""),
        level=level,
        trigger=result.get("trigger") or "-",
        fingerprint=result.get("fingerprint") or "-",
        cached=bool(result.get("cached")),
        model=model or "-",
        platform=platform or "-",
        elapsed_ms=round(float(elapsed_ms or 0), 1),
        validation=validation or "-",
        confidence=confidence,
        fallback=result.get("fallback_reason") or "-",
    )
    logger.info(
        "immersive director room=%s trigger=%s fingerprint=%s cached=%s platform=%s model=%s "
        "elapsed_ms=%s validation=%s confidence=%s fallback=%s profile=%s section=%s",
        room_id, result.get("trigger") or "-", result.get("fingerprint") or "-",
        bool(result.get("cached")), platform or "-", model or "-", elapsed_ms,
        validation or "-", confidence, result.get("fallback_reason") or "-",
        result.get("profile") or "-", result.get("section") or 0,
    )


def _music_payload(plan: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    payload["kind"] = "music"
    payload["analysis"] = {
        "profile": analysis.get("profile"),
        "intensity": analysis.get("intensity"),
        "scene": analysis.get("scene"),
        "confidence": analysis.get("confidence"),
        "reason": analysis.get("reason"),
    }
    return payload


def on_narrative_turn(
    *,
    room_id: str,
    room_dir: Path | None,
    house_rules: dict[str, Any] | None,
    text: str,
    structured: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    scenario_id: str = "",
    config_dir: Path | str | None = None,
) -> dict[str, Any]:
    """处理一个叙事回合；返回本次触发的摘要（失败时为空结构）。"""
    result: dict[str, Any] = {"music": None, "illustration": None}
    try:
        _ensure_legacy_migrated(config_dir)
        settings = load_immersive_settings(config_dir)
        if not settings.get("enabled"):
            return result
        switches = resolve_room_switches(settings, house_rules)
        if not switches["music"] and not switches["illustration"]:
            return result

        analysis = analyze_narrative(
            text,
            structured=structured,
            state=state,
            default_profile=settings.get("music_default_profile") or "daily",
            default_intensity=settings.get("music_default_intensity") or "medium",
        )
        result["analysis"] = analysis

        if switches["music"]:
            room_settings = dict(settings)
            # 房间级自动切换开关只能覆盖「关闭」，不会越过站点总开关。
            room_settings["music_auto_switch"] = bool(switches.get("music_auto"))
            plan = plan_music(
                room_dir=room_dir,
                room_id=room_id,
                analysis=analysis,
                settings=room_settings,
                scenario_id=scenario_id,
            )
            result["music"] = plan

            # LLM 导演：先查缓存（同步命中可直接生效），未命中则后台精修。
            director_due, trigger = director.should_trigger(plan, settings=settings, switches=switches)
            director_result: dict[str, Any] | None = None
            if director_due:
                audio_state = load_music_state(room_dir)
                fingerprint = director.context_fingerprint(text, analysis, audio_state)
                cached = director.get_cached(
                    fingerprint, ttl=int(settings.get("music_director_cache_ttl") or 0)
                )
                if cached and isinstance(cached.get("score"), dict):
                    plan = apply_director(
                        room_dir=room_dir,
                        plan=plan,
                        score=cached["score"],
                        fingerprint=fingerprint,
                        model=str(cached.get("model") or ""),
                        platform=str(cached.get("platform") or ""),
                        confidence=int(cached.get("confidence") or 0),
                        cached=True,
                    )
                    result["music"] = plan
                    director_result = {
                        "trigger": trigger,
                        "fingerprint": fingerprint,
                        "cached": True,
                        "validation": "cache_hit",
                        "fallback_reason": "",
                        "profile": plan.get("profile"),
                        "section": plan.get("section"),
                    }
                    _log_director(
                        room_dir,
                        room_id,
                        result=director_result,
                        model=str(cached.get("model") or ""),
                        platform=str(cached.get("platform") or ""),
                        elapsed_ms=0,
                        confidence=int(cached.get("confidence") or 0),
                    )

            broadcast.emit("immersive_music", _music_payload(plan, analysis))
            if plan.get("changed"):
                _room_log(
                    room_dir,
                    room_id,
                    f"配乐切换为 {plan['profile']} / {plan['intensity']}（第 {plan['section']} 段）",
                    profile=plan["profile"],
                    intensity=plan["intensity"],
                    section=plan["section"],
                    reason=plan["reason"],
                    scene=analysis.get("scene"),
                    seed=plan["seed"],
                )
            logger.info(
                "immersive music room=%s profile=%s intensity=%s section=%s changed=%s reason=%s scene=%s director=%s",
                room_id, plan["profile"], plan["intensity"], plan["section"], plan["changed"],
                plan["reason"], analysis.get("scene"), director_result.get("validation") if director_result else trigger,
            )
            result["director"] = director_result

            if director_due and director_result is None and director.in_flight(room_id) is False:
                future = director.submit(
                    room_dir=room_dir,
                    room_id=room_id,
                    text=text,
                    analysis=analysis,
                    plan=plan,
                    audio_state=load_music_state(room_dir),
                    settings=room_settings,
                    config_dir=config_dir,
                    trigger=trigger,
                )
                if future is not None:
                    def _director_done(fut, room_id=room_id, room_dir=room_dir, plan=plan, trigger=trigger, analysis=analysis):
                        try:
                            refined = fut.result()
                        except Exception as exc:  # noqa: BLE001 - 导演失败不影响播放
                            logger.warning("immersive director job failed room=%s: %s", room_id, exc)
                            return
                        summary = {
                            "trigger": refined.get("trigger") or trigger,
                            "fingerprint": refined.get("fingerprint"),
                            "cached": bool(refined.get("cached")),
                            "validation": refined.get("validation"),
                            "fallback_reason": refined.get("fallback_reason") or "",
                            "profile": plan.get("profile"),
                            "section": plan.get("section"),
                        }
                        if not refined.get("accepted") or not isinstance(refined.get("score"), dict):
                            _log_director(
                                room_dir,
                                room_id,
                                result=summary,
                                model=str(refined.get("model") or ""),
                                platform=str(refined.get("platform") or ""),
                                elapsed_ms=float(refined.get("elapsed_ms") or 0),
                                confidence=int(refined.get("confidence") or 0),
                            )
                            return
                        updated = apply_director(
                            room_dir=room_dir,
                            plan=plan,
                            score=refined["score"],
                            fingerprint=str(refined.get("fingerprint") or ""),
                            model=str(refined.get("model") or ""),
                            platform=str(refined.get("platform") or ""),
                            confidence=int(refined.get("confidence") or 0),
                            cached=bool(refined.get("cached")),
                        )
                        summary.update(
                            {
                                "validation": "ok",
                                "profile": updated.get("profile"),
                                "section": updated.get("section"),
                            }
                        )
                        broadcast.emit("immersive_music", _music_payload(updated, analysis))
                        _log_director(
                            room_dir,
                            room_id,
                            result=summary,
                            model=str(refined.get("model") or ""),
                            platform=str(refined.get("platform") or ""),
                            elapsed_ms=float(refined.get("elapsed_ms") or 0),
                            confidence=int(refined.get("confidence") or 0),
                        )

                    future.add_done_callback(_director_done)

        if switches["illustration"] and switches["illustration_auto"]:
            # 配图冷却用独立回合计数：即使配乐被关闭，冷却仍按叙事回合推进。
            turn_index = next_turn_index(room_dir)
            allowed, cooldown_reason = should_auto_generate(
                room_dir, turn_index, settings.get("illustration_min_turns") or 4
            )
            if not allowed:
                logger.info("immersive illustration room=%s skipped=%s", room_id, cooldown_reason)
            else:
                seed = f"{room_id}:{turn_index}:{analysis.get('scene')}"
                pending = {
                    "kind": "illustration",
                    "status": "pending",
                    "room_id": room_id,
                    "scene": analysis.get("scene"),
                    "profile": analysis.get("profile"),
                    "intensity": analysis.get("intensity"),
                    "seed": seed,
                    "turn": turn_index,
                }
                broadcast.emit("immersive_illustration", pending)
                started = time.monotonic()
                future = submit_generation(
                    room_dir=room_dir,
                    room_id=room_id,
                    scene=analysis.get("scene") or "未知场景",
                    profile=analysis.get("profile") or "daily",
                    intensity=analysis.get("intensity") or "medium",
                    seed=seed,
                    turn_index=turn_index,
                    settings=settings,
                    force=False,
                    config_dir=config_dir,
                    scenario_id=scenario_id,
                    house_rules=house_rules,
                )
                result["illustration"] = pending

                def _done(fut, room_id=room_id, room_dir=room_dir, seed=seed, started=started):
                    try:
                        payload = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("immersive illustration job failed: %s", exc)
                        broadcast.emit(
                            "immersive_illustration",
                            {
                                "kind": "illustration",
                                "status": "error",
                                "room_id": room_id,
                                "seed": seed,
                                "error": str(exc)[:180],
                            },
                        )
                        _room_log(room_dir, room_id, "配图生成失败（已降级为无图）", level="warning", error=str(exc)[:180])
                        return
                    payload = dict(payload)
                    payload["kind"] = "illustration"
                    payload["status"] = "ready"
                    broadcast.emit("immersive_illustration", payload)
                    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                    _room_log(
                        room_dir,
                        room_id,
                        f"配图完成：{payload.get('scene') or '-'}（{payload.get('provider')}）",
                        level="warning" if payload.get("degraded") else "info",
                        provider=payload.get("provider"),
                        degraded=payload.get("degraded"),
                        error=payload.get("error") or "-",
                        cache_hit=bool(payload.get("cache_hit")),
                        reuse_source=payload.get("reuse_source") or "-",
                        elapsed_ms=elapsed_ms,
                        turn=payload.get("turn"),
                    )

                future.add_done_callback(_done)
        return result
    except Exception:  # noqa: BLE001 - 沉浸系统永远不能阻塞叙事
        logger.exception("immersive turn failed room=%s", room_id)
        return result


def set_music_directive(
    *,
    room_id: str,
    room_dir: Path | None,
    settings: dict[str, Any],
    profile: str | None = None,
    intensity: str | None = None,
    scene: str = "手动指令",
    scenario_id: str = "",
) -> dict[str, Any]:
    """手动指定配乐档位/强度（房主或管理员触发）。"""
    analysis = {
        "profile": profile or settings.get("music_default_profile") or "daily",
        "intensity": intensity or settings.get("music_default_intensity") or "medium",
        "scene": scene,
        "confidence": "manual",
        "reason": "manual",
    }
    plan = plan_music(
        room_dir=room_dir,
        room_id=room_id,
        analysis=analysis,
        settings=settings,
        scenario_id=scenario_id,
        force_profile=profile,
        force_intensity=intensity,
    )
    broadcast.emit("immersive_music", _music_payload(plan, analysis))
    _room_log(
        room_dir,
        room_id,
        f"手动配乐指令：{plan['profile']} / {plan['intensity']}（第 {plan['section']} 段）",
        profile=plan["profile"],
        intensity=plan["intensity"],
        section=plan["section"],
        reason="manual",
    )
    return plan


def generate_illustration_now(
    *,
    room_id: str,
    room_dir: Path | None,
    settings: dict[str, Any],
    scene: str,
    profile: str,
    intensity: str,
    seed: str = "",
    turn_index: int = 0,
    force: bool = True,
    config_dir: Path | str | None = None,
    scenario_id: str = "",
    house_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """立即生成一张配图（手动触发），返回结果并广播。"""
    payload = generate_now(
        room_dir=room_dir,
        room_id=room_id,
        scene=scene,
        profile=profile,
        intensity=intensity,
        seed=seed or f"{room_id}:{int(time.time())}",
        turn_index=turn_index,
        settings=settings,
        force=force,
        config_dir=config_dir,
        scenario_id=scenario_id,
        house_rules=house_rules,
    )
    payload["kind"] = "illustration"
    payload["status"] = "ready"
    broadcast.emit("immersive_illustration", payload)
    _room_log(
        room_dir,
        room_id,
        f"手动配图：{scene}（{payload.get('provider')}）",
        level="warning" if payload.get("degraded") else "info",
        provider=payload.get("provider"),
        degraded=payload.get("degraded"),
        error=payload.get("error") or "-",
        cache_hit=bool(payload.get("cache_hit")),
        reuse_source=payload.get("reuse_source") or "-",
    )
    return payload


def immersive_status(
    *,
    room_id: str,
    room_dir: Path | None,
    settings: dict[str, Any],
    house_rules: dict[str, Any] | None = None,
    scenario_id: str = "",
) -> dict[str, Any]:
    switches = resolve_room_switches(settings, house_rules)
    music = load_music_state(room_dir)
    illustration = load_room_state(room_dir)
    return {
        "room_id": room_id,
        "switches": switches,
        "music": {
            "profile": music.get("profile"),
            "intensity": music.get("intensity"),
            "section": music.get("section"),
            "turns": music.get("turns"),
            "base_seed": music.get("base_seed"),
            "scene": music.get("scene"),
            "motif": f"{music.get('profile') or 'daily'}-{(int(music.get('section') or 0) % 4) + 1}",
            "crossfade_ms": int(settings.get("music_crossfade_ms") or 3000),
            "volume": int(settings.get("music_volume") or 35) / 100.0,
            "duck_bgm": bool(settings.get("music_duck_bgm", True)),
            "last_reason": music.get("last_reason"),
            "history": music.get("history") or [],
            "score": (music.get("director") or {}).get("score") if isinstance(music.get("director"), dict) else None,
            "director": (
                {
                    key: music["director"].get(key)
                    for key in ("fingerprint", "model", "platform", "confidence", "cached", "section_index", "at")
                }
                if isinstance(music.get("director"), dict)
                else None
            ),
        },
        "illustration": {
            "last_turn": illustration.get("last_turn"),
            "history": history_for_room(room_dir),
        },
        "usage": usage_snapshot(),
    }
