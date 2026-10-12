"""聊天请求的纯辅助逻辑（从 routes/chat.py 拆出）。

本模块只放不依赖 Flask 请求生命周期、可单测的 helper：历史压缩、提示词与
房间快照组装、剧本知识检索、结构化响应落库等。HTTP 路由与响应收尾仍留在
``routes/chat.py``，从而把原先 1500+ 行的大文件按职责拆成两块，便于阅读维护。
"""
import json
import logging
import re
import time
from pathlib import Path
from uuid import uuid4

from flask import current_app
from trpg_server.ai_capabilities import responses_endpoint
from trpg_server.ai_platform_config import load_platform_config, ordered_platform_paths
from trpg_server.agents.profiles import AgentProfile
from trpg_server.agents.prompt_builder import wrap_user_input
from trpg_server.agents.responses_transport import ResponsesRequester
from trpg_server.agents.memory import remember_room_fact
from trpg_server.agents.room_state import append_room_event, project_room_state
from trpg_server.agents.trigger_system import find_trigger_definition, record_trigger, validate_trigger
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.logging_config import redact_sensitive
from trpg_server.responses import success_response
from trpg_server.role_config import load_roles, select_role_for_content
from trpg_server.security import get_user_manager
from trpg_server.settings import AI_PLATFORM_SECRET_DIR, CONFIG_DIR, HISTORY_DIR

logger = logging.getLogger(__name__)


_HISTORY_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


HISTORY_COMPACT_CHAR_THRESHOLD = 12000


def _profile_for_request(profile, tool_state, enable_suggestions=True):
    """Keep the complete KP capability set while removing schema aliases.

    Alias tools execute the same handler and only exist for compatibility with
    older callers. Advertising both names to the model increases ambiguity and
    schema tokens without adding a capability. Request-time safety remains in
    each tool handler; no legitimate tool chain is disabled here.

    ``room.suggest_actions`` is dropped unless the room house rules enable
    action suggestions, so a disabled feature costs no schema tokens.
    """
    names = list(profile.tool_names or [])
    aliases = {"dice.roll_dice", "sanity.roll_sanity_check"}
    gated = set() if enable_suggestions else {"room.suggest_actions"}
    filtered = [name for name in names if name not in aliases and name not in gated]
    return AgentProfile(
        id=profile.id,
        name=profile.name,
        prompt=profile.prompt,
        provider=profile.provider,
        wake_words=profile.wake_words,
        tool_names=filtered,
        context_providers=profile.context_providers,
    )


def _timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _deliver_structured_triggers(room_dir, room_info, scenario, requests):
    """Validate model-selected trigger ids and persist only approved messages."""
    if not room_dir or not isinstance(scenario, dict):
        return []
    messages_path = Path(room_dir) / "messages.json"
    messages = read_json(messages_path, default=[])
    if not isinstance(messages, list):
        messages = []
    state = read_json(Path(room_dir) / "state.json", default={})
    state = state if isinstance(state, dict) else {}
    delivered = []
    from trpg_server.scenario_store import build_trigger_message
    for requested in requests if isinstance(requests, list) else []:
        trigger_id = str(requested.get("trigger_id") or "").strip() if isinstance(requested, dict) else ""
        if not trigger_id:
            continue
        definition = find_trigger_definition(scenario, trigger_id)
        if definition:
            validation = validate_trigger(trigger_id, {**state, "scenario_version": room_info.get("scenario_version")}, scenario, audience="player")
            if not validation.get("ok"):
                logger.info("structured_trigger_rejected room_id=%s trigger_id=%s reason=%s", room_info.get("id"), trigger_id, validation.get("reason"))
                continue
            body = []
            for attachment in definition.get("attachments", []):
                resource = attachment.get("resourceRef") or {}
                if resource.get("content"):
                    body.append(str(resource["content"]))
                elif resource.get("url"):
                    body.append(f"[{resource.get('alt')}]({resource.get('url')})")
            message = {"type": "trigger", "sender_id": None, "sender_name": definition.get("text") or f"触发器{trigger_id}", "avatar": "/assets/avatars/default_system.jpg", "content": "\n\n".join(body), "time": time.strftime("%H:%M"), "created_at": _timestamp(), "metadata": {"trigger_id": trigger_id, "reason": requested.get("reason", "")}}
        else:
            message = build_trigger_message(scenario, trigger_id)
            if not message:
                continue
            message = {**message, "id": uuid4().hex, "sender_id": None, "time": time.strftime("%H:%M"), "created_at": _timestamp(), "metadata": {**(message.get("metadata") or {}), "reason": requested.get("reason", "")}}
        messages.append(message)
        delivered.append(message)
        record_trigger(room_dir, trigger_id, reason=str(requested.get("reason") or "AI trigger"))
        state = read_json(Path(room_dir) / "state.json", default=state)
    if delivered:
        write_json_atomic(messages_path, messages[-200:])
    return delivered


def _message_response(user_id, content, message, script_id=None):
    payload = {
        "user_id": user_id,
        "content": content,
        "timestamp": _timestamp(),
    }
    if script_id is not None:
        payload["script_id"] = script_id

    return success_response(payload, message)


def _get_ai_platform_dir():
    return current_app.config.get("AI_PLATFORM_DIR", CONFIG_DIR / "aiplatform")


def _get_ai_platform_secret_dir():
    return current_app.config.get("AI_PLATFORM_SECRET_DIR", AI_PLATFORM_SECRET_DIR)


def _get_config_dir():
    return current_app.config.get("CONFIG_DIR", CONFIG_DIR)


def _get_history_dir():
    return current_app.config.get("HISTORY_DIR", HISTORY_DIR)


def _get_kp_prompt_file():
    return current_app.config.get("KP_PROMPT_FILE", CONFIG_DIR / "roles" / "kp.md")


def _get_debug_kp_prompt_file():
    return current_app.config.get("DEBUG_KP_PROMPT_FILE", _get_config_dir() / "roles" / "debug-kp.md")


def _get_role_config_file():
    return current_app.config.get("ROLE_CONFIG_FILE", CONFIG_DIR / "roles" / "roles.json")


def _load_enabled_platforms(provider_id=None):
    """返回按加载顺序排列的「已启用」平台列表：``[(platform_id, config), ...]``。

    顺序由各平台配置的 ``order`` 决定（管理员在设置页拖动排序）。房间内 AI 对话
    依次尝试：先加载 order 最小的启用平台，失败（报错、余额不足等）时回退到下一个。
    """
    platform_dir = _get_ai_platform_dir()
    secret_dir = _get_ai_platform_secret_dir()
    if not platform_dir.exists():
        logger.warning("AI platform config directory does not exist: %s", platform_dir)
        return []

    platforms = []
    for path in ordered_platform_paths(platform_dir):
        if provider_id and path.stem != provider_id:
            continue
        try:
            secret_path = secret_dir / path.name
            config = load_platform_config(path, secret_path)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read AI platform config: %s", path.name)
            continue

        if config.get("enabled", False):
            platforms.append((path.stem, config))

    return platforms


def _load_enabled_platform(provider_id=None):
    platforms = _load_enabled_platforms(provider_id)
    return platforms[0] if platforms else (None, None)


def _platform_display_name(platform_id):
    try:
        config = read_json(_get_ai_platform_dir() / f"{platform_id}.json", default={})
        name = config.get("name") if isinstance(config, dict) else None
        if name:
            return str(name)
    except (OSError, ValueError):
        pass
    return str(platform_id)


def _notify_platform_switch(from_platform, to_platform, reason):
    """房间内 AI 对话自动切换平台时通知在线管理员。

    明确不使用网页 ``alert``：改为向在线的 ADMIN/OWNER 各自的 ``user:{id}`` 房间推送
    ``admin_notification`` 事件，前端以顶部提示条展示切换原因。
    """
    socketio = current_app.extensions.get("socketio")
    active_sockets = current_app.config.get("ACTIVE_SOCKET_SESSIONS") or {}
    from_name = _platform_display_name(from_platform)
    to_name = _platform_display_name(to_platform)
    detail = str(reason or "").strip()[:200] or "未知原因"
    message = f"「{from_name}」无法加载（{detail}），已自动切换至「{to_name}」平台"
    logger.warning("AI platform auto-switch: %s", message)
    if socketio is None or not active_sockets:
        return

    try:
        manager = get_user_manager()
    except Exception:  # noqa: BLE001 - 通知失败不影响主对话流程
        logger.debug("Unable to resolve user manager for platform switch notice", exc_info=True)
        return

    payload = {
        "type": "ai_platform_switch",
        "level": "warning",
        "message": message,
        "from_platform": from_platform,
        "to_platform": to_platform,
        "reason": detail,
    }
    for user_key in list(active_sockets.keys()):
        try:
            user = manager.get_user_by_id(int(user_key))
        except Exception:  # noqa: BLE001 - 忽略无法解析的会话
            continue
        if str((user or {}).get("role") or "").upper() not in {"ADMIN", "OWNER"}:
            continue
        socketio.emit("admin_notification", payload, room=f"user:{user_key}")


def _load_role_for_content(content):
    roles = load_roles(_get_role_config_file(), _get_kp_prompt_file(), _get_ai_platform_dir())
    return select_role_for_content(roles, content)


def _can_start_scenario(room_info, user_id, global_role="USER"):
    if str(global_role or "").upper() in {"ADMIN", "OWNER"}:
        return True
    if str(room_info.get("creator_id")) == str(user_id):
        return True
    for member in room_info.get("members", []):
        if str(member.get("user_id")) != str(user_id):
            continue
        if member.get("is_active", True) is False or member.get("status", "active") == "removed":
            return False
        return member.get("room_role") in {"owner", "admin"}
    return False


def _build_knowledge_usage(result, scenario_results):
    ruleset = getattr(result, "knowledge_usage", None)
    ruleset = ruleset if isinstance(ruleset, dict) else {}
    scenario_chunks = len(scenario_results) if isinstance(scenario_results, list) else 0
    ruleset_chunks = int(ruleset.get("ruleset_chunks") or 0)
    ruleset_calls = int(ruleset.get("tool_calls") or 0)
    return {
        "used": bool(ruleset_chunks or scenario_chunks),
        "total_chunks": ruleset_chunks + scenario_chunks,
        "ruleset": {
            "called": ruleset_calls > 0,
            "calls": ruleset_calls,
            "chunks": ruleset_chunks,
            "sources": int(ruleset.get("ruleset_sources") or 0),
            "ruleset_ids": list(ruleset.get("ruleset_ids") or []),
            "knowledge_versions": list(ruleset.get("knowledge_versions") or []),
            "topics": list(ruleset.get("topics") or []),
            "citations": list(ruleset.get("citations") or []),
        },
        "scenario": {"chunks": scenario_chunks},
    }


def _mark_scenario_started(room_dir, user_id):
    info_path = room_dir / "info.json"
    room_info = read_json(info_path, default={})
    started_at = room_info.get("scenario_started_at") or _timestamp()
    room_info["scenario_started_at"] = started_at
    room_info["scenario_started_by"] = user_id
    write_json_atomic(info_path, room_info)
    return started_at


def _sync_room_active_scene(room_dir, scene_id, scenario):
    """Mirror a state-update scene transition into ``info.json``.

    Structured KP responses persist ``active_scene_id`` into ``state.json``,
    while the room snapshot, scenario tools and archive checks read the pointer
    from ``info.json`` (which ``room.activate_scenario_scene`` writes). Keeping
    both in sync avoids the model receiving two different "current scenes".
    """
    if not room_dir or scene_id in (None, ""):
        return
    info_path = Path(room_dir) / "info.json"
    info = read_json(info_path, default={})
    if not isinstance(info, dict) or str(info.get("active_scene_id") or "") == str(scene_id):
        return
    info["active_scene_id"] = str(scene_id)
    manifest = scenario.get("scene_manifest") if isinstance(scenario, dict) and isinstance(scenario.get("scene_manifest"), list) else []
    module = next(
        (item for item in manifest if isinstance(item, dict) and str(item.get("id") or item.get("module_id")) == str(scene_id)),
        None,
    )
    if module:
        info["active_scene_title"] = module.get("title")
    write_json_atomic(info_path, info)


def _json_for_log(value):
    return json.dumps(redact_sensitive(value), ensure_ascii=False, default=str)


def _emit_thinking_stage(room_id, ai_request_id, stage, label):
    """Best-effort stage updates for connected clients; never block chat."""
    if not room_id or not ai_request_id:
        return
    try:
        socketio = current_app.extensions.get("socketio")
        if socketio is None:
            return
        socketio.emit(
            "new_message",
            {
                "room_id": str(room_id),
                "type": "ai_thinking_stage",
                "aiRequestId": str(ai_request_id),
                "stage": stage,
                "label": label,
            },
            room=str(room_id),
            namespace="/",
        )
    except Exception:
        logger.debug("Unable to emit AI thinking stage", exc_info=True)


def _build_agent_requester(platform_config, base_url, headers, model, timeout, fallback, max_retries=0):
    """多轮工具调用用的请求器。

    当平台被探测为支持 Responses API、且用户显式开启了 ``previous_response_id`` 时，
    改用有状态续答（只发增量，省掉轮间重复上下文）；否则保持原本的 chat/completions。
    ``max_retries`` 与 chat/completions 路径共用，保证瞬时抖动不会立刻锁定回退。
    """
    config = platform_config.get("config") if isinstance(platform_config, dict) else None
    config = config if isinstance(config, dict) else {}
    if not config.get("use_previous_response_id") or not config.get("responses_api_supported"):
        return fallback
    endpoint = responses_endpoint(base_url)
    if not endpoint:
        return fallback
    logger.info("Using Responses API with previous_response_id for agent tool rounds")
    return ResponsesRequester(endpoint, headers, model, timeout, fallback, max_retries=max_retries)


def _load_kp_prompt():
    prompt_path = _get_kp_prompt_file()
    try:
        content = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to load KP prompt file: {prompt_path}") from exc

    content_lines = [
        line for line in content.splitlines() if not line.startswith("#") and line.strip()
    ]
    if content_lines:
        return "\n".join(content_lines)

    return "你是KP（守秘人），负责主持以克苏鲁的呼唤第七版规则为基础的桌上角色扮演游戏。"


def _load_debug_kp_prompt():
    prompt_path = _get_debug_kp_prompt_file()
    try:
        content = prompt_path.read_text(encoding="utf-8")
    except OSError:
        return (
            "你是用于调试工具调用的KP。只能使用已启用的房间资料和检定工具。\n"
            "需要检定时调用 check.roll_room_check，并在工具返回后复述检定摘要。"
        )

    content_lines = [line for line in content.splitlines() if not line.startswith("#") and line.strip()]
    return "\n".join(content_lines) or "你是用于调试工具调用的KP。"


def _safe_history_part(value):
    text = str(value or "unknown").strip() or "unknown"
    return _HISTORY_SAFE_RE.sub("_", text)[:120]


def _history_filename(user_id, room_id=None, agent_id="kp"):
    safe_agent = _safe_history_part(agent_id)
    if room_id:
        return f"room-{_safe_history_part(room_id)}-{safe_agent}.json"
    return f"user-{_safe_history_part(user_id)}-{safe_agent}.json"


def _room_session_id(room_id, agent_id="kp"):
    return f"room:{_safe_history_part(room_id)}:agent:{_safe_history_part(agent_id)}"


def _load_history(user_id, room_id=None, agent_id="kp"):
    history_file = _get_history_dir() / _history_filename(user_id, room_id, agent_id)
    return history_file, read_json(history_file, default=[])


def _speaker_for_user(room_info, user_id):
    for member in room_info.get("members", []):
        if str(member.get("user_id")) != str(user_id):
            continue
        character = member.get("character_card") or member.get("character") or {}
        return {
            "user_id": member.get("user_id"),
            "username": member.get("username") or str(user_id),
            "character_name": character.get("name"),
        }
    return {"user_id": user_id, "username": str(user_id)}


def _format_user_content(content, speaker=None):
    if not speaker:
        return content

    parts = [f"speaker={speaker.get('username') or speaker.get('user_id')}"]
    character_name = speaker.get("character_name")
    if character_name:
        parts.append(f"character={character_name}")
    return f"[{'; '.join(parts)}]\n{content}"


def _is_compact_command(content):
    text = str(content or "").strip()
    if text.startswith("@KP"):
        text = text[3:].strip()
    return text.casefold() == "/compact"


def _strip_compact_command(content):
    lines = str(content or "").splitlines()
    kept = []
    requested = False
    for line in lines:
        if line.strip().casefold() == "/compact":
            requested = True
            continue
        kept.append(line)
    return "\n".join(kept).strip(), requested


def _history_needs_compaction(history, threshold=HISTORY_COMPACT_CHAR_THRESHOLD):
    return sum(len(str(item.get("content") or "")) for item in history) > threshold


def _request_allows_check(content):
    text = str(content or "").strip()
    if text.startswith("@KP"):
        text = text[3:].strip()
    return bool(re.search(r"(?:^|\s)(?:/check|检定|投骰|掷骰|侦察|侦查|观察|搜索|搜查|调查|检查)(?:\s|$)", text, re.IGNORECASE))


def _request_allows_manual_trigger(content):
    text = str(content or "").strip()
    if text.startswith("@KP"):
        text = text[3:].strip()
    return text.startswith("/trigger") or "手动触发" in text


def _request_allows_scene_transition(content):
    text = str(content or "").strip()
    if text.startswith("@KP"):
        text = text[3:].strip()
    if text.startswith("/scene"):
        return True
    if re.search(r"(?:不要|别|不想|无需)(?:进入|前往|来到|离开|跑到|去往|转场|切换场景)", text, re.IGNORECASE):
        return False
    return bool(re.search(r"(进入|前往|来到|离开|跑到|去往|转场|切换场景|go to|enter|leave)", text, re.IGNORECASE))


def _maybe_remember_important_action(content, context):
    """Persist explicit continuity decisions without storing every chat line."""
    text = str(content or "").strip()
    if not context.room_dir or len(text) < 8:
        return
    patterns = (r"(?:进入|前往|来到|离开|跑到|去往|转场|切换场景)", r"(?:带上|带着|留下|加入|护送|跟随)")
    if not any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
        return
    try:
        remember_room_fact({"kind": "player_decision", "content": text, "importance": 3}, context)
        append_room_event(context.room_dir, {"kind": "player_decision", "content": text})
    except Exception:
        logger.exception("Failed to remember important player action")


# key_facts 里 kind 字段到中文标签的映射，用于把滚动摘要渲染成可读条目。
_KEY_FACT_LABELS = {
    "location_discovered": "地点",
    "npc_status": "NPC",
    "item_acquired": "物品",
    "decision_made": "决策",
}


def _parse_compact_payload(text):
    """解析模型返回的 JSON 摘要；解析失败时退化为纯文本叙事。"""
    raw = str(text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    candidate = match.group(0) if match else raw
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        return {"narrative": raw, "key_facts": []}
    if not isinstance(data, dict):
        return {"narrative": raw, "key_facts": []}
    narrative = str(data.get("narrative") or data.get("summary") or "").strip()
    facts = data.get("key_facts")
    return {"narrative": narrative or raw, "key_facts": facts if isinstance(facts, list) else []}


def _compact_history_entries(payload):
    # Keep the persisted summary useful without imposing an output-token cap on
    # normal KP replies.
    if isinstance(payload, dict):
        narrative = str(payload.get("narrative") or "").strip()
        facts = payload.get("key_facts")
    else:
        narrative = str(payload or "").strip()
        facts = []
    lines = [narrative] if narrative else []
    rendered = []
    if isinstance(facts, list):
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text") or "").strip()
            if not text:
                continue
            kind = str(fact.get("type") or "").strip()
            rendered.append(f"- [{_KEY_FACT_LABELS.get(kind, kind or '事实')}] {text}")
    if rendered:
        lines.append("关键事实：")
        lines.extend(rendered)
    clean = "\n".join(lines).strip() or "（无可压缩内容）"
    if len(clean) > 4000:
        clean = clean[:3999].rstrip() + "…"
    return [{"role": "system", "content": f"历史压缩摘要：\n{clean}", "compact": True}]


def _compact_history_with_ai(requester, model, history):
    if not history:
        return []

    # 双层滚动摘要：把已有的 compact 摘要与新增对话一起交给模型融合，
    # 避免每次压缩都丢掉之前的记忆；同时要求输出结构化的 key_facts，
    # 让关键剧情事实（地点/NPC/物品/决策）不会随叙事压缩而丢失。
    previous = next((item for item in history if isinstance(item, dict) and item.get("compact")), None)
    dialogue = [item for item in history if isinstance(item, dict) and not item.get("compact")]
    if previous is None and not dialogue:
        return []

    sections = []
    if previous is not None:
        sections.append(f"【已有滚动摘要】\n{previous.get('content') or ''}")
    sections.append(f"【新增对话】\n{_json_for_log(dialogue)}")
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你在维护跑团房间的滚动记忆。请把「已有滚动摘要」与「新增对话」融合成一份更新的摘要，"
                    "不要编造未发生的内容。只输出 JSON，不要输出多余文字，格式："
                    '{"narrative": "不超过250字的叙事摘要，保留当前场景、未解决的线索与检定结果", '
                    '"key_facts": [{"type": "location_discovered|npc_status|item_acquired|decision_made", '
                    '"text": "一句话事实"}]}'
                ),
            },
            {"role": "user", "content": "\n\n".join(sections)},
        ],
    }
    summary, _token_count = _extract_ai_response(requester(payload))
    if not summary.strip():
        raise RuntimeError("AI 平台未返回历史压缩摘要")
    return _compact_history_entries(_parse_compact_payload(summary))


def _select_model(platform_config):
    models = platform_config.get("models", [])
    if not models:
        return "local-model"

    model = next((item for item in models if item.get("enabled", True)), models[0])
    return model.get("id", "local-model")


def _compact_character_card(character_card):
    if not isinstance(character_card, dict):
        return None

    compact = {}
    for key in ("id", "name", "occupation", "age", "gender", "sex"):
        value = character_card.get(key)
        if value not in (None, ""):
            compact[key] = value
    return compact or None


def _compact_character_state(character_state):
    if not isinstance(character_state, dict):
        return None

    compact = {}
    for key in ("max_hp", "current_hp", "max_san", "current_san"):
        value = character_state.get(key)
        if value is not None:
            compact[key] = value
    return compact or None


def _bounded_manifest_entries(items, max_count, char_cap, text_keys=("summary", "description", "content")):
    """Bound total entry count and per-entry text length of a manifest list."""
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if len(result) >= max_count:
            break
        if not isinstance(item, dict):
            result.append(item)
            continue
        bounded = dict(item)
        if char_cap:
            for key in text_keys:
                value = bounded.get(key)
                if isinstance(value, str) and value:
                    bounded[key] = value[:char_cap]
        result.append(bounded)
    return result


def _compact_room_snapshot(snapshot, runtime_config=None):
    if not isinstance(snapshot, dict):
        return {}
    scene_manifest_cap = getattr(runtime_config, "max_snapshot_scene_manifest", None)
    if scene_manifest_cap is None:
        scene_manifest_cap = 30
    char_cap = getattr(runtime_config, "snapshot_manifest_summary_cap", None)
    include_entity = getattr(runtime_config, "snapshot_include_entity_manifest", False)

    scenario = snapshot.get("scenario")
    if isinstance(scenario, dict):
        available_sections = scenario.get("available_sections")
        if not isinstance(available_sections, dict):
            available_sections = {}
        entity_manifest = (scenario.get("entity_manifest", []) if isinstance(scenario.get("entity_manifest", []), list) else [])
        compact_scenario = {
            "id": scenario.get("id"),
            "title": scenario.get("title"),
            "description": scenario.get("description"),
            "found": scenario.get("found"),
            "available_sections": available_sections,
            "allow_open_ending": scenario.get("allow_open_ending"),
            "module_count": scenario.get("module_count"),
            "trigger_count": scenario.get("trigger_count"),
            "active_scene_id": scenario.get("active_scene_id"),
            "scene_manifest": _bounded_manifest_entries(
                (scenario.get("scene_manifest", []) if isinstance(scenario.get("scene_manifest", []), list) else []),
                scene_manifest_cap,
                char_cap,
            ),
            "global_manifest": _bounded_manifest_entries(
                (scenario.get("global_manifest", []) if isinstance(scenario.get("global_manifest", []), list) else []),
                12,
                char_cap,
            ),
        }
        if include_entity and entity_manifest:
            compact_scenario["entity_manifest"] = _bounded_manifest_entries(entity_manifest, scene_manifest_cap, char_cap)
        opening = scenario.get("opening")
        if isinstance(opening, str) and opening and char_cap:
            opening = opening[:char_cap]
        compact_scenario["opening"] = opening
    else:
        compact_scenario = scenario

    members = []
    for member in snapshot.get("members") or []:
        if not isinstance(member, dict):
            continue
        compact_member = {
            "user_id": member.get("user_id"),
            "username": member.get("username"),
            "active": member.get("active"),
        }
        character_card = _compact_character_card(member.get("character_card") or member.get("character"))
        if character_card:
            compact_member["character"] = character_card
        character_state = _compact_character_state(member.get("character_state"))
        if character_state:
            compact_member["character_state"] = character_state
        members.append(compact_member)

    memory = snapshot.get("memory")
    if isinstance(memory, dict) and isinstance(memory.get("items"), list):
        memory = {"items": [
            {**item, "content": str(item.get("content") or "")[:240]}
            for item in memory["items"][:8] if isinstance(item, dict)
        ]}
    triggers = snapshot.get("triggers", [])
    if isinstance(triggers, list):
        triggers = _bounded_manifest_entries(triggers, scene_manifest_cap, char_cap)
    else:
        triggers = snapshot.get("triggers", [])
    return {
        "room": snapshot.get("room"),
        "scenario": compact_scenario,
        "members": members,
        "memory": memory,
        "triggers": triggers,
    }


def _room_snapshot_system_message(snapshot, room_state=None, runtime_config=None):
    compact_snapshot = _compact_room_snapshot(snapshot, runtime_config)
    if room_state is not None:
        compact_snapshot["state"] = project_room_state(room_state, snapshot)
    return (
        "当前房间资料必须通过工具 `room.get_room_snapshot` 获取。\n"
        "优先使用当前房间、绑定剧本、触发器目录和成员角色卡。\n"
        "不要复用其他房间的剧本、角色或记忆资料。\n"
        "注入的上下文是精简版，不包含完整场景文本或完整角色详情。\n"
        "需要详细剧本模块或触发器内容时，调用相应的房间或触发器工具。\n"
        "需要剧本摘要时先调用 `room.get_scenario_context`；只有检索片段不足以回答时，才调用 `room.get_scenario_module` 读取有界原文片段。\n"
        "scene_manifest 是剧本提供的唯一场景索引；global_manifest 是背景/公开信息/时间线的短摘要。"
        "若 scenario.sequential=true，优先按照 scene_manifest 的 order 顺序加载模块；否则按玩家当前需求检索。"
        "summary 仅用于检索，不能替代原文，也不能据此推断未写出的设施。\n"
        f"{_json_for_log(compact_snapshot)}"
    )


def _build_messages(system_prompt, history, content, room_snapshot_message=None):
    messages = [{"role": "system", "content": system_prompt}]
    if room_snapshot_message:
        messages.append({"role": "system", "content": room_snapshot_message})
    for item in history:
        item_content = item["content"]
        if item.get("role") == "user":
            item_content = _format_user_content(item_content, item.get("speaker"))
            item_content = wrap_user_input(item_content)
        messages.append({"role": item["role"], "content": item_content})
    messages.append({"role": "user", "content": wrap_user_input(content)})
    return messages


def _history_for_request(history, max_chars=8000, recent_items=12):
    """Keep prompt history bounded without an extra summarization request.

    Persisted history remains untouched; only the copy sent to the model is
    compacted. Existing explicit ``compact`` summaries are always retained.
    """
    if not isinstance(history, list):
        return []
    pinned = [item for item in history if isinstance(item, dict) and item.get("compact")]
    recent = [item for item in history if isinstance(item, dict) and not item.get("compact")][-recent_items:]
    selected = pinned[-1:] + recent
    total = 0
    result = []
    for item in reversed(selected):
        size = len(str(item.get("content") or ""))
        if result and total + size > max_chars:
            break
        result.append(item)
        total += size
    return list(reversed(result))


def _retrieval_query(history, content, turns: int = 3, part_chars: int = 500, max_chars: int = 1500) -> str:
    """拼接最近若干轮对话作为知识库检索 query。

    只用玩家当前一句话检索时，泛化输入（如「我调查一下周围」）与任何知识块都
    没有词法/向量交集，检索会一直落到同一批兜底块，看起来"召回固定不随剧情变化"。
    把最近几轮对话一并作为 query，命中的剧情上下文才会随剧情推进而改变。
    """
    parts: list[str] = []
    if isinstance(history, list):
        recent = [item for item in history if isinstance(item, dict) and not item.get("compact")][-turns:]
        parts.extend(str(item.get("content") or "").strip()[:part_chars] for item in recent)
    parts.append(str(content or "").strip()[:part_chars])
    return "\n".join(part for part in parts if part)[-max_chars:]


def _extract_ai_response(response_data):
    ai_response = ""
    choices = response_data.get("choices", [])
    if choices:
        choice = choices[0]
        if "message" in choice and "content" in choice["message"]:
            ai_response = choice["message"]["content"]
        elif "delta" in choice and "content" in choice["delta"]:
            ai_response = choice["delta"]["content"]

    token_count = None
    usage = response_data.get("usage")
    if usage:
        if "total_tokens" in usage:
            token_count = usage["total_tokens"]
        elif "completion_tokens" in usage and "prompt_tokens" in usage:
            token_count = usage["completion_tokens"] + usage["prompt_tokens"]

    return ai_response, token_count
