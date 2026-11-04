"""配乐导演：带滞回、房间级同步时钟与可复现种子的「切歌」状态机。

只决定「放什么」（档位/强度/段落/种子），实际声音由前端 Web Audio 合成器按
同一份种子现场生成，因此没有版权风险，也不需要下载音频。

可复现性：``seed = base_seed + section``。同一房间、同一基准种子、同一段落，
前端永远得到同一串乐句走向；调试时可用 ``immersive music seed`` 固定种子。

房间同步：状态里维护一个 ``cue_epoch`` 与 ``cue_position_bars``。同一份谱面
只推进播放位置（以小节计），换谱才开启新 epoch。所有客户端拿到同一个
``sync`` 后按位置对齐，新加入的玩家补发当前状态，因此不会从 cue 开头重放。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from trpg_server.immersive.auto_score import build_auto_score
from trpg_server.immersive.config import INTENSITIES, PROFILES
from trpg_server.json_store import read_json, write_json_atomic

STATE_FILENAME = "immersive_music.json"
_HISTORY_LIMIT = 8
# 缺省小节时长（72 BPM 的 4/4）——只有乐谱缺少合法 tempo 时才会用到。
_DEFAULT_BAR_SECONDS = 240.0 / 72.0


def _state_path(room_dir: Path | None) -> Path | None:
    return room_dir / STATE_FILENAME if room_dir else None


def load_state(room_dir: Path | None) -> dict[str, Any]:
    path = _state_path(room_dir)
    if path is None:
        return {}
    data = read_json(path, default={})
    return data if isinstance(data, dict) else {}


def _save_state(room_dir: Path | None, state: dict[str, Any]) -> None:
    path = _state_path(room_dir)
    if path is None:
        return
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        write_json_atomic(path, state)
    except OSError:
        pass


def _seed_base(room_id: str, seed_setting: str, scenario_id: str) -> str:
    if seed_setting:
        return seed_setting
    return f"{room_id}:{scenario_id or 'default'}"


def _history(state: dict[str, Any]) -> list[dict[str, Any]]:
    history = state.get("history")
    return [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []


def _score_identity(score: Any) -> str:
    """谱面身份：只包含真正改变听感的字段。

    tempo 不在其中——强度变化只该改变演奏速度，不应该被当成「换了一首曲子」
    让房间里所有人从头再听一遍。
    """
    if not isinstance(score, dict):
        return ""
    chords = score.get("chords") if isinstance(score.get("chords"), list) else []
    variants = score.get("motif_variants") if isinstance(score.get("motif_variants"), list) else []
    sections = score.get("sections") if isinstance(score.get("sections"), list) else []
    instrumentation = score.get("instrumentation") if isinstance(score.get("instrumentation"), list) else []
    dynamics = score.get("dynamics") if isinstance(score.get("dynamics"), list) else []
    return "|".join(
        [
            str(score.get("mode") or ""),
            ",".join(str(item) for item in instrumentation),
            ";".join(
                f"{item.get('root')}:{','.join(str(value) for value in (item.get('intervals') or []))}"
                for item in chords
                if isinstance(item, dict)
            ),
            ";".join(
                f"{item.get('role')}:{','.join(str(value) for value in (item.get('degrees') or []))}:"
                f"{','.join(str(value) for value in (item.get('rhythm') or []))}"
                for item in variants
                if isinstance(item, dict)
            ),
            ";".join(
                f"{item.get('bars')}:{item.get('dynamic')}"
                for item in sections
                if isinstance(item, dict)
            ),
            ",".join(str(value) for value in dynamics),
            "" if score.get("pedal") is None else str(score.get("pedal")),
        ]
    )


def _bar_seconds(score: Any) -> float:
    """单小节秒数。与前端 ``tempo=Math.round(...)`` + ``barDuration = 60 / tempo * 4`` 对齐。"""
    tempo = 0.0
    if isinstance(score, dict):
        try:
            tempo = float(score.get("tempo") or 0)
        except (TypeError, ValueError):
            tempo = 0.0
    tempo = round(tempo)
    if tempo < 30 or tempo > 200:
        return _DEFAULT_BAR_SECONDS
    return 240.0 / tempo


def _advance_cue(
    state: dict[str, Any],
    *,
    score: Any,
    profile: str,
    section: int,
    base_seed: str,
    now: float,
) -> dict[str, Any]:
    """推进房间共享时钟：同一谱面累加播放位置，换谱才开新 epoch。"""
    bar_seconds = _bar_seconds(score)
    key = "|".join([base_seed, str(profile), str(section), _score_identity(score)])
    epoch = int(state.get("cue_epoch") or 0)
    previous_key = str(state.get("cue_key") or "")
    position = float(state.get("cue_position_bars") or 0.0)
    previous_bar_seconds = float(state.get("cue_bar_seconds") or 0.0)
    updated = float(state.get("cue_updated_at") or 0.0)
    if previous_key and previous_key == key and updated > 0:
        elapsed = max(0.0, now - updated)
        if previous_bar_seconds > 0:
            position += elapsed / previous_bar_seconds
    else:
        epoch += 1
        position = 0.0
    state.update(
        {
            "cue_epoch": epoch,
            "cue_key": key,
            "cue_bar_seconds": round(bar_seconds, 4),
            "cue_position_bars": round(position, 4),
            "cue_updated_at": now,
        }
    )
    return {
        "epoch": epoch,
        "position_bars": round(position, 3),
        "bar_seconds": round(bar_seconds, 4),
        "server_now_ms": int(now * 1000),
    }


def current_sync(room_dir: Path | None) -> dict[str, Any] | None:
    """只读地推算当前播放位置，供新玩家加入时补发（不写盘）。"""
    state = load_state(room_dir)
    if not state.get("cue_key"):
        return None
    now = time.time()
    position = float(state.get("cue_position_bars") or 0.0)
    bar_seconds = float(state.get("cue_bar_seconds") or 0.0)
    updated = float(state.get("cue_updated_at") or 0.0)
    if bar_seconds > 0 and updated > 0:
        position += max(0.0, now - updated) / bar_seconds
    return {
        "epoch": int(state.get("cue_epoch") or 0),
        "position_bars": round(position, 3),
        "bar_seconds": round(bar_seconds, 4),
        "server_now_ms": int(now * 1000),
    }


def current_payload(room_dir: Path | None, room_id: str = "") -> dict[str, Any] | None:
    """加入房间时补发：当前配乐指令 + 共享播放位置（不从 cue 开头重放）。"""
    state = load_state(room_dir)
    payload = state.get("cue_payload")
    sync = current_sync(room_dir)
    if not isinstance(payload, dict) or not payload or sync is None:
        return None
    result = dict(payload)
    result["room_id"] = result.get("room_id") or room_id
    result["sync"] = sync
    return result


def plan_music(
    *,
    room_dir: Path | None,
    room_id: str,
    analysis: dict[str, Any],
    settings: dict[str, Any],
    scenario_id: str = "",
    force_profile: str | None = None,
    force_intensity: str | None = None,
) -> dict[str, Any]:
    """根据分析结果推进房间配乐状态，返回给前端的完整指令。"""
    state = load_state(room_dir)
    base_seed = _seed_base(room_id, str(settings.get("music_seed") or ""), scenario_id)
    default_profile = settings.get("music_default_profile") or "daily"
    default_intensity = settings.get("music_default_intensity") or "medium"

    current_profile = state.get("profile")
    if current_profile not in PROFILES:
        current_profile = default_profile if default_profile in PROFILES else "daily"
    current_intensity = state.get("intensity")
    if current_intensity not in INTENSITIES:
        current_intensity = default_intensity if default_intensity in INTENSITIES else "medium"
    section = int(state.get("section") or 0)
    total_turns = int(state.get("turns") or 0)
    turns_in_profile = int(state.get("turns_in_profile") or 0)
    pending = state.get("pending_profile")
    pending_turns = int(state.get("pending_turns") or 0)

    target_profile = force_profile if force_profile in PROFILES else analysis.get("profile") or default_profile
    target_intensity = force_intensity if force_intensity in INTENSITIES else analysis.get("intensity") or default_intensity
    target_rank = INTENSITIES.index(target_intensity) if target_intensity in INTENSITIES else 1
    current_rank = INTENSITIES.index(current_intensity) if current_intensity in INTENSITIES else 1

    changed = False
    reason = "initial" if not state else "stable"
    auto_switch = bool(settings.get("music_auto_switch", True))
    hysteresis = int(settings.get("music_hysteresis_turns") or 0)

    if force_profile is not None or force_intensity is not None:
        # 手动指令立即生效，并推进段落，给玩家明确的「换歌」反馈。
        if target_profile != current_profile or target_intensity != current_intensity:
            changed = True
            reason = "manual"
            section += 1
        current_profile, current_intensity = target_profile, target_intensity
        turns_in_profile = 0
        pending, pending_turns = None, 0
    elif not state:
        current_profile, current_intensity = target_profile, target_intensity
        changed = True
    elif auto_switch:
        if target_profile == current_profile:
            pending, pending_turns = None, 0
            if target_intensity != current_intensity:
                current_intensity = target_intensity
                changed = True
                reason = "intensity"
        else:
            if pending != target_profile:
                pending, pending_turns = target_profile, 1
            else:
                pending_turns += 1
            # 紧张度跨两级（低→高）或进入恐怖/紧迫时允许更快切换，避免压迫感被滞回拖住。
            escalate = (
                target_rank - current_rank >= 2
                or (target_profile in {"horror", "urgency"} and target_rank > current_rank)
            )
            if pending_turns >= max(1, hysteresis) or escalate:
                current_profile, current_intensity = target_profile, target_intensity
                section += 1
                turns_in_profile = 0
                pending, pending_turns = None, 0
                changed = True
                reason = "escalate" if escalate and pending_turns < max(1, hysteresis) else "hysteresis"
            else:
                reason = f"pending:{pending_turns}"
    else:
        reason = "auto_disabled"

    total_turns += 1
    turns_in_profile += 1
    # 段落推进只由「内容真的变了」触发（换档位/手动指令/导演新谱）。
    # cue 自身在客户端按循环持续变奏，不再每 4 回合硬切一次，
    # 否则慢回合的 cue 会在高潮前被替换，快回合又永远听不到完整结构。

    director_state = state.get("director") if isinstance(state.get("director"), dict) else None
    prior_history = _history(state)
    if reason == "manual":
        # 手动指令代表房主明确要求换歌，旧导演乐谱不再代表当前意图。
        director_state = None

    director_score = (
        (director_state or {}).get("score")
        if director_state and ((director_state.get("score") or {}).get("mood") == current_profile)
        else None
    )
    # AI 导演不可用（未配置/预算耗尽/校验失败）时，本地自动作曲家接管，
    # 保证「叙事→配乐」链路始终有完整乐谱，而不是只有氛围档位参数。
    auto_score = None if director_score else build_auto_score(
        profile=current_profile,
        intensity=current_intensity,
        section=section,
        scene=str(analysis.get("scene") or ""),
        seed=base_seed,
    )
    score = director_score or auto_score

    new_state = dict(state)
    new_state.update(
        {
            "profile": current_profile,
            "intensity": current_intensity,
            "section": section,
            "turns": total_turns,
            "turns_in_profile": turns_in_profile,
            "pending_profile": pending,
            "pending_turns": pending_turns,
            "base_seed": base_seed,
            "scene": analysis.get("scene") or "",
            "last_reason": reason,
            "director": director_state,
            "updated_at": state.get("updated_at"),
        }
    )
    entry = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile": current_profile,
        "intensity": current_intensity,
        "section": section,
        "reason": reason,
        "analysis": analysis.get("reason"),
    }
    new_state["history"] = (prior_history + [entry])[-_HISTORY_LIMIT:]
    sync = _advance_cue(
        new_state,
        score=score,
        profile=current_profile,
        section=section,
        base_seed=base_seed,
        now=time.time(),
    )

    payload = {
        "room_id": room_id,
        "profile": current_profile,
        "intensity": current_intensity,
        "section": section,
        "seed": f"{base_seed}:{section}",
        "motif": f"{current_profile}-{(section % 4) + 1}",
        "crossfade_ms": int(settings.get("music_crossfade_ms") or 3000),
        "volume": int(settings.get("music_volume") or 35) / 100.0,
        "duck_bgm": bool(settings.get("music_duck_bgm", True)),
        "scene": analysis.get("scene") or "",
        "reason": reason,
        "changed": changed,
        "auto_switch": auto_switch,
        "turns": total_turns,
        "score": score,
        "sync": sync,
        "director": (
            {
                "fingerprint": director_state.get("fingerprint"),
                "model": director_state.get("model"),
                "platform": director_state.get("platform"),
                "confidence": director_state.get("confidence"),
                "cached": director_state.get("cached"),
                "section_index": director_state.get("section_index"),
            }
            if director_state
            else None
        ),
        "history": new_state["history"],
    }
    # 供新玩家加入时补发：保留最后一份完整指令（sync 会在补发时按当前时间重算）。
    new_state["cue_payload"] = payload
    _save_state(room_dir, new_state)
    return payload


def apply_director(
    *,
    room_dir: Path | None,
    plan: dict[str, Any],
    score: dict[str, Any],
    fingerprint: str,
    model: str = "",
    platform: str = "",
    confidence: int = 0,
    cached: bool = False,
) -> dict[str, Any]:
    """把 LLM 导演乐谱落到房间配乐状态，并返回更新后的前端指令。

    导演只在后台精修：规则计划已经播放，因此这里不重新跑滞回，而是直接采用导演的
    ``mood`` 与当前段落 ``dynamic``。同一指纹重复命中时保留段号、轮换乐谱内部段落，
    避免每次命中都「换一首歌」。
    """
    state = load_state(room_dir)
    director = state.get("director") if isinstance(state.get("director"), dict) else {}
    same_context = str(director.get("fingerprint") or "") == str(fingerprint)
    sections = score.get("sections") if isinstance(score.get("sections"), list) else []
    if not sections:
        return plan
    if same_context:
        # 同一上下文再次命中：轮换乐谱内部段落，让动态起伏继续推进。
        section_index = int(director.get("section_index") or 0) + 1
    else:
        section_index = 0
    section_index = max(0, min(int(section_index), len(sections) - 1))
    active = sections[section_index] if isinstance(sections[section_index], dict) else {}
    profile = str(score.get("mood") or plan.get("profile") or "daily")
    intensity = str(active.get("dynamic") or plan.get("intensity") or "medium")
    section = int(plan.get("section") or state.get("section") or 0)
    if not same_context:
        section += 1
    base_seed = str(state.get("base_seed") or plan.get("seed") or "room:0").rsplit(":", 1)[0]
    state.update(
        {
            "profile": profile,
            "intensity": intensity,
            "section": section,
            "scene": score.get("scene") or plan.get("scene") or state.get("scene") or "",
            "last_reason": "director",
            "director": {
                "fingerprint": fingerprint,
                "score": score,
                "section_index": section_index,
                "model": model,
                "platform": platform,
                "confidence": confidence,
                "cached": cached,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
    )
    entry = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile": profile,
        "intensity": intensity,
        "section": section,
        "reason": "director",
        "analysis": f"{model or '-'} · cached={cached}",
    }
    state["history"] = (_history(state) + [entry])[-_HISTORY_LIMIT:]
    sync = _advance_cue(
        state,
        score=score,
        profile=profile,
        section=section,
        base_seed=base_seed,
        now=time.time(),
    )
    payload = dict(plan)
    payload.update(
        {
            "profile": profile,
            "intensity": intensity,
            "section": section,
            "seed": f"{base_seed}:{section}",
            "motif": f"{profile}-{(section % 4) + 1}",
            "scene": score.get("scene") or plan.get("scene") or "",
            "reason": "director",
            "changed": True,
            "score": score,
            "sync": sync,
            "director": {
                "fingerprint": fingerprint,
                "model": model,
                "platform": platform,
                "confidence": confidence,
                "cached": cached,
                "section_index": section_index,
            },
            "history": state["history"],
        }
    )
    state["cue_payload"] = payload
    _save_state(room_dir, state)
    return payload
