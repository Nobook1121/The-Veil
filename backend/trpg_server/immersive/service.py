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

from trpg_server.immersive import (
    broadcast,
    director,
    external_music,
    illustration_director,
    image_platforms,
)
from trpg_server.immersive.analyzer import analyze_narrative
from trpg_server.immersive.config import (
    load_immersive_settings,
    resolve_illustration_trigger,
    resolve_music_source,
    resolve_room_switches,
)
from trpg_server.immersive.illustration import (
    generate_now,
    history_for_room,
    load_room_state,
    next_turn_index,
    scenario_accepts_room_images,
    should_auto_generate,
    usage_snapshot,
)
from trpg_server.immersive.music import apply_director
from trpg_server.immersive.music import current_payload as live_music_payload
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


def _music_payload(
    plan: dict[str, Any], analysis: dict[str, Any], music_source: str = "hybrid"
) -> dict[str, Any]:
    payload = dict(plan)
    payload["kind"] = "music"
    payload["music_source"] = music_source
    payload["analysis"] = {
        "profile": analysis.get("profile"),
        "intensity": analysis.get("intensity"),
        "scene": analysis.get("scene"),
        "confidence": analysis.get("confidence"),
        "reason": analysis.get("reason"),
    }
    return payload


def _broadcast_thinking_start(room_id: str, thinking_id: str) -> None:
    broadcast.emit(
        "new_message",
        {
            "room_id": room_id,
            "type": "ai_thinking_start",
            "aiRequestId": thinking_id,
            "roleName": "KP · 配图",
            "startedAt": time.time() * 1000,
        },
    )


def _broadcast_thinking_stage(
    room_id: str, thinking_id: str, stage: str, label: str
) -> None:
    broadcast.emit(
        "new_message",
        {
            "room_id": room_id,
            "type": "ai_thinking_stage",
            "aiRequestId": thinking_id,
            "stage": stage,
            "label": label,
        },
    )


def _broadcast_thinking_end(room_id: str, thinking_id: str) -> None:
    broadcast.emit(
        "new_message",
        {"room_id": room_id, "type": "ai_thinking_end", "aiRequestId": thinking_id},
    )


def _is_important_scene(
    structured: dict[str, Any] | None, analysis: dict[str, Any], room_dir: Path | None
) -> bool:
    """「仅重要场景转换」档位判定：显式 next_scene、高置信度或场景标签变化。"""
    if isinstance(structured, dict) and str(structured.get("next_scene") or "").strip():
        return True
    if str(analysis.get("confidence") or "").lower() == "high":
        return True
    current = str(analysis.get("scene") or "").strip()
    if not current:
        return False
    history = history_for_room(room_dir)
    last_scene = str((history[0] or {}).get("scene") or "").strip() if history else ""
    return bool(last_scene) and current != last_scene


def _kp_done(
    fut: Any,
    *,
    room_id: str,
    room_dir: Path | None,
    seed: str,
    started: float,
    thinking_id: str,
    analysis: dict[str, Any],
    scenario_id: str,
    settings: dict[str, Any],
    config_dir: Path | str | None,
    house_rules: dict[str, Any] | None,
) -> None:
    """AI KP 决策回调：生成场景图与 NPC 头像，广播状态并结束「正在思考」占位。"""
    try:
        outcome = fut.result()
    except Exception as exc:  # noqa: BLE001 - 后台任务失败不能影响房间
        logger.warning("immersive illustration KP job failed: %s", exc)
        broadcast.emit(
            "immersive_illustration",
            {
                "kind": "illustration",
                "status": "error",
                "room_id": room_id,
                "seed": seed,
                "reason": "kp_job_failed",
                "error": str(exc)[:180],
            },
        )
        _broadcast_thinking_end(room_id, thinking_id)
        _room_log(room_dir, room_id, "配图判断失败，已跳过本轮配图", level="warning", error=str(exc)[:180])
        return

    decision = outcome.get("decision") if isinstance(outcome, dict) else None
    decision = decision if isinstance(decision, dict) else {}
    should = bool(decision.get("should_illustrate"))
    _room_log(
        room_dir,
        room_id,
        f"配图判断：{'需要' if should else '不需要'}（{decision.get('reason') or '-'}）",
        level="info",
        trigger=outcome.get("trigger") if isinstance(outcome, dict) else "",
        kp_accepted=outcome.get("accepted") if isinstance(outcome, dict) else False,
        kp_validation=outcome.get("validation") if isinstance(outcome, dict) else "",
        kp_platform=outcome.get("platform") if isinstance(outcome, dict) else "",
        kp_model=outcome.get("model") if isinstance(outcome, dict) else "",
        kp_elapsed_ms=outcome.get("elapsed_ms") if isinstance(outcome, dict) else 0,
        kp_fallback=bool(outcome.get("fallback")) if isinstance(outcome, dict) else False,
        kp_fallback_reason=str(outcome.get("fallback_reason") or "")[:180]
        if isinstance(outcome, dict)
        else "",
        prompt=(decision.get("scene_prompt") or "")[:120],
        npc_count=len(decision.get("npcs") or []),
    )
    if not should:
        broadcast.emit(
            "immersive_illustration",
            {
                "kind": "illustration",
                "status": "skipped",
                "room_id": room_id,
                "seed": seed,
                "reason": decision.get("reason") or "kp_declined",
                "trigger": outcome.get("trigger") if isinstance(outcome, dict) else "",
            },
        )
        _broadcast_thinking_stage(room_id, thinking_id, "done", "本轮无需配图")
        _broadcast_thinking_end(room_id, thinking_id)
        return

    scene = str(analysis.get("scene") or "未知场景")
    profile = str(analysis.get("profile") or "daily")
    intensity = str(analysis.get("intensity") or "medium")
    allow_cross_room = scenario_accepts_room_images(scenario_id)
    _broadcast_thinking_stage(room_id, thinking_id, "illustrating", "正在配图…")
    broadcast.emit(
        "immersive_illustration",
        {
            "kind": "illustration",
            "status": "generating",
            "room_id": room_id,
            "seed": seed,
            "scene": scene,
            "profile": profile,
            "intensity": intensity,
            "category": "scene",
        },
    )

    scene_payload: dict[str, Any] | None = None
    try:
        scene_payload = generate_now(
            room_dir=room_dir,
            room_id=room_id,
            scene=scene,
            profile=profile,
            intensity=intensity,
            seed=seed,
            settings=settings,
            force=False,
            config_dir=config_dir,
            scenario_id=scenario_id,
            house_rules=house_rules,
            prompt_override=str(decision.get("scene_prompt") or ""),
            category="scene",
            allow_cross_room=allow_cross_room,
        )
        scene_payload = dict(scene_payload)
        scene_payload.update(
            {
                "kind": "illustration",
                "status": "ready",
                "room_id": room_id,
                "category": "scene",
                "trigger": outcome.get("trigger") if isinstance(outcome, dict) else "",
                "kp_reason": decision.get("reason") or "",
            }
        )
        broadcast.emit("immersive_illustration", scene_payload)
        _room_log(
            room_dir,
            room_id,
            f"场景配图完成：{scene}（{scene_payload.get('provider')}）",
            level="warning" if scene_payload.get("degraded") else "info",
            category="scene",
            provider=scene_payload.get("provider"),
            degraded=scene_payload.get("degraded"),
            error=scene_payload.get("error") or "-",
            cache_hit=bool(scene_payload.get("cache_hit")),
            cache_level=scene_payload.get("cache_level") or "-",
            source_room=scene_payload.get("source_room") or "-",
            reuse_source=scene_payload.get("reuse_source") or "-",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("immersive scene illustration failed: %s", exc)
        broadcast.emit(
            "immersive_illustration",
            {
                "kind": "illustration",
                "status": "error",
                "room_id": room_id,
                "seed": seed,
                "category": "scene",
                "reason": "generation_failed",
                "error": str(exc)[:180],
            },
        )
        _room_log(room_dir, room_id, "场景配图失败", level="warning", error=str(exc)[:180])

    npc_limit = int(settings.get("illustration_npc_max_per_turn") or 0)
    if settings.get("illustration_npc_enabled") and npc_limit > 0:
        for npc in (decision.get("npcs") or [])[:npc_limit]:
            name = str(npc.get("name") or "").strip()
            if not name:
                continue
            fingerprint = f"npc:{' '.join(name.casefold().split())}"
            npc_prompt = str(npc.get("prompt") or "").strip() or (
                f"Character portrait of {name}, head and shoulders composition, "
                "detailed face, cinematic lighting, simple uncluttered background"
            )
            try:
                npc_payload = generate_now(
                    room_dir=room_dir,
                    room_id=room_id,
                    scene=name,
                    profile=profile,
                    intensity=intensity,
                    seed=f"{seed}:npc:{fingerprint}",
                    settings=settings,
                    force=False,
                    config_dir=config_dir,
                    scenario_id=scenario_id,
                    house_rules=house_rules,
                    prompt_override=npc_prompt,
                    category="portrait",
                    fingerprint=fingerprint,
                    npc_name=name,
                    size_override=str(settings.get("illustration_npc_size") or "768x768"),
                    allow_cross_room=allow_cross_room,
                )
                npc_payload = dict(npc_payload)
                npc_payload.update(
                    {
                        "kind": "illustration",
                        "status": "ready",
                        "room_id": room_id,
                        "category": "portrait",
                        "npc_name": name,
                        "trigger": outcome.get("trigger") if isinstance(outcome, dict) else "",
                    }
                )
                broadcast.emit("immersive_illustration", npc_payload)
                _room_log(
                    room_dir,
                    room_id,
                    f"NPC 头像完成：{name}（{npc_payload.get('provider')}）",
                    level="warning" if npc_payload.get("degraded") else "info",
                    category="portrait",
                    npc_name=name,
                    provider=npc_payload.get("provider"),
                    cache_hit=bool(npc_payload.get("cache_hit")),
                    cache_level=npc_payload.get("cache_level") or "-",
                    source_room=npc_payload.get("source_room") or "-",
                    degraded=bool(npc_payload.get("degraded")),
                    error=npc_payload.get("error") or "-",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("immersive NPC portrait failed: %s", exc)
                _room_log(room_dir, room_id, f"NPC 头像失败：{name}", level="warning", error=str(exc)[:180])

    broadcast.emit(
        "immersive_illustration",
        {
            "kind": "illustration",
            "status": "complete",
            "room_id": room_id,
            "seed": seed,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    _broadcast_thinking_end(room_id, thinking_id)


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
    personal_override: dict[str, Any] | None = None,
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
        # 生效音乐来源：站点默认 < 个人覆盖（站点允许时）< 房间收紧。
        music_source = resolve_music_source(
            settings, house_rules, (personal_override or {}).get("music_source")
        )
        result["music_source"] = music_source
        # 引擎矩阵：外部曲库只是「叠在程序化底床之上」，底床逻辑保持不变。
        engine, engine_reason = external_music.resolve_engine(settings, house_rules)
        result["music_engine"] = engine
        result["music_engine_reason"] = engine_reason
        external_music.notify_engine(
            room_id,
            engine,
            fade_ms=int(
                settings.get("music_external_fade_ms")
                if engine == external_music.ENGINE_EXTERNAL
                else settings.get("music_crossfade_ms") or 3000
            ),
        )

        analysis = analyze_narrative(
            text,
            structured=structured,
            state=state,
            default_profile=settings.get("music_default_profile") or "daily",
            default_intensity=settings.get("music_default_intensity") or "medium",
        )
        result["analysis"] = analysis

        if switches["music"] and engine != external_music.ENGINE_OFF:
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

            broadcast.emit("immersive_music", _music_payload(plan, analysis, music_source))
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

            # 外链检索：服务端按剧情自动触发（主要路径），KP 工具只做显式请求。
            # 只有段落真的变化时才发起，避免每回合都重复广播同一首。
            if engine == external_music.ENGINE_EXTERNAL and (
                plan.get("changed") or int(plan.get("turns") or 0) <= 1
            ):
                external_music.submit(
                    room_dir=room_dir,
                    room_id=room_id,
                    scenario_id=scenario_id,
                    profile=str(plan.get("profile") or "daily"),
                    intensity=str(plan.get("intensity") or "medium"),
                    scene=str(analysis.get("scene") or ""),
                    settings=settings,
                    config_dir=config_dir,
                    house_rules=house_rules,
                    trigger="auto",
                )

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
                        broadcast.emit("immersive_music", _music_payload(updated, analysis, music_source))
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

        # 配图触发档位：站点默认 < 个人覆盖（站点允许时）< 房间收紧。
        illustration_trigger = resolve_illustration_trigger(
            settings, house_rules, (personal_override or {}).get("illustration_trigger")
        )
        result["illustration_trigger"] = illustration_trigger

        if switches["illustration"] and illustration_trigger != "off":
            # 配图冷却用独立回合计数：即使配乐被关闭，冷却仍按叙事回合推进。
            turn_index = next_turn_index(room_dir)
            min_turns = (
                int(settings.get("illustration_min_turns") or 4)
                if illustration_trigger == "turn_interval"
                else 1
            )
            allowed, cooldown_reason = should_auto_generate(room_dir, turn_index, min_turns)
            if not allowed:
                logger.info(
                    "immersive illustration room=%s skipped=%s trigger=%s",
                    room_id, cooldown_reason, illustration_trigger,
                )
            else:
                important_signal = _is_important_scene(structured, analysis, room_dir)
                if illustration_trigger == "important" and not important_signal:
                    logger.info(
                        "immersive illustration room=%s skipped=not_important_scene trigger=important",
                        room_id,
                    )
                else:
                    seed = f"{room_id}:{turn_index}:{analysis.get('scene')}"
                    pending = {
                        "kind": "illustration",
                        "status": "deciding",
                        "room_id": room_id,
                        "scene": analysis.get("scene"),
                        "profile": analysis.get("profile"),
                        "intensity": analysis.get("intensity"),
                        "seed": seed,
                        "turn": turn_index,
                        "trigger": illustration_trigger,
                    }
                    broadcast.emit("immersive_illustration", pending)
                    result["illustration"] = pending
                    thinking_id = f"illus-{room_id}-{turn_index}"
                    _broadcast_thinking_start(room_id, thinking_id)
                    _broadcast_thinking_stage(room_id, thinking_id, "deciding", "KP 正在判断是否需要配图…")
                    started = time.monotonic()
                    future = illustration_director.submit(
                        room_id=room_id,
                        room_dir=room_dir,
                        text=text,
                        analysis=analysis,
                        structured=structured,
                        settings=settings,
                        trigger=illustration_trigger,
                        important_signal=important_signal,
                        config_dir=config_dir,
                    )
                    if future is None:
                        _broadcast_thinking_end(room_id, thinking_id)
                        logger.info(
                            "immersive illustration room=%s skipped=kp_in_flight trigger=%s",
                            room_id, illustration_trigger,
                        )
                    else:
                        future.add_done_callback(
                            lambda fut, room_id=room_id, room_dir=room_dir, seed=seed,
                            started=started, thinking_id=thinking_id, analysis=analysis,
                            scenario_id=scenario_id, settings=settings,
                            config_dir=config_dir, house_rules=house_rules: _kp_done(
                                fut,
                                room_id=room_id,
                                room_dir=room_dir,
                                seed=seed,
                                started=started,
                                thinking_id=thinking_id,
                                analysis=analysis,
                                scenario_id=scenario_id,
                                settings=settings,
                                config_dir=config_dir,
                                house_rules=house_rules,
                            )
                        )
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
    live = live_music_payload(room_dir)
    illustration = load_room_state(room_dir)
    engine, engine_reason = external_music.resolve_engine(settings, house_rules)
    return {
        "room_id": room_id,
        "switches": switches,
        # 站点 + 房间收紧后的生效值；个人覆盖由前端在允许范围内再解析一次。
        "music_source": resolve_music_source(settings, house_rules),
        "music_engine": engine,
        "music_engine_reason": engine_reason,
        "external_music": {
            "providers": str(settings.get("music_external_providers") or ""),
            "license_allow": str(settings.get("music_external_license_allow") or ""),
            "in_flight": external_music.in_flight(room_id),
        },
        "illustration_trigger": resolve_illustration_trigger(settings, house_rules),
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
            # 优先复用最后一次实际广播的乐谱（含本地自动作曲家结果），
            # 否则刷新页面后自动配乐会回落到「只有档位参数」。
            "score": (live or {}).get("score")
            or (
                (music.get("director") or {}).get("score")
                if isinstance(music.get("director"), dict)
                else None
            ),
            # 共享播放位置：刷新页面/回到房间时也能接上当前进度，而不是从头播放。
            "sync": (live or {}).get("sync"),
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
