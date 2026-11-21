"""dnd5e-vtt 的 AI DM 战桌工具（重构计划 §7.3）+ KP 战桌状态行（§7.2 最小版）。

四个工具通过本插件的 ``agent_tools`` 能力声明（plugin.json），由核心现成的
插件工具合并点自动接入两条 AI 面，**不改核心分派、不动 dnd5e 规则集文件**：

- 房间 KP / AI 玩家：:func:`trpg_server.agents.profiles.build_kp_tool_names`
  里的 :func:`plugin_tool_names` 按工具声明的 ``rulesets=("dnd5e",)`` 过滤并入；
- 网站 Agent：:func:`trpg_server.site_agent_tools.ruleset._tool_names` 同一
  合并点（dnd5e 规则集 profile 之外的插件工具照常并入）。

工具实现只复用既有原语，不复制几何/存储/广播逻辑：

- ``place_template`` 复用本插件模板端点的校验、存储与 socket 广播（entry.py）；
- ``switch_scene`` 调 veil-map ``map_service`` 的场景激活原语
  （``activate_for_area``），解析与广播用它的公开服务补齐
  （``list_scenes`` / ``broadcast_state``，本刀的最小新增）；
- ``roll_broadcast`` 走核心 ``dice.roll`` 同一条管线（``roll_dice`` +
  ``dice_roll`` 展示元数据 + ``visible_message`` 聊天卡），AI 身份与现有
  AI 工具消息形态一致，前端照常全员播放骰桌动画；
- ``worldtree_recall`` 复用 narrative store 的 :class:`NarrativeRuntime`
  读取（不另行解析 narrative.json），按 DM 观察者返回 kp_only 内容。

权限：四个工具都是 DM 面（AI KP 或房主/房管发起）。AI 玩家代理也会收到
这些工具名（核心对插件工具不做面别过滤），但每个处理器都会给出可读拒绝，
拿不到 kp_only 内容，也改不动战桌。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from trpg_server.agents.tools.base import AgentTool

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent

# 与 veil-map 的 DM_AGENT_IDS 同口径：KP 代理视为 DM；真人房主/房管（控制台
# 或手动代理路径）同样放行。
DM_AGENT_IDS = {"kp"}
OWNER_ROLES = {"owner", "admin"}
MAX_SCENE_HINTS = 8
MAX_RECALL_NODES = 5
DEFAULT_RECALL_NODES = 3
MAX_BODY_EXCERPT = 400
MAX_EDGE_SUMMARY = 12
MAX_ROLL_LABEL = 60
_QUERY_SPLIT = re.compile(r"[\s,，、;；/]+")


# ============================================================
# 上下文与权限
# ============================================================


def _tool_context(context: Any) -> tuple[Any, str, dict[str, Any]]:
    """取工具上下文的房间三元组；照 veil-map 工具的容错读取方式。"""
    room_dir = getattr(context, "room_dir", None)
    room_id = str(getattr(context, "room_id", "") or "")
    info_fn = getattr(context, "room_info", None)
    info = info_fn() if callable(info_fn) else {}
    if not isinstance(info, dict):
        info = {}
    return (Path(room_dir) if room_dir else None), room_id, info


def _is_dm(info: dict[str, Any], agent_id: Any, user_id: Any) -> bool:
    """AI KP 视为 DM；其余按房间角色（房主/房管）判定，口径同 veil-map。"""
    if str(agent_id or "").strip().lower() in DM_AGENT_IDS:
        return True
    target = str(user_id or "")
    if not target:
        return False
    if target == str((info or {}).get("creator_id") or ""):
        return True
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        if str(member.get("user_id")) != target:
            continue
        return str(member.get("room_role") or "").strip().lower() in OWNER_ROLES
    return False


def _dm_gate(context: Any) -> tuple[Any, str, dict[str, Any]] | dict[str, Any]:
    """统一门闸：房间上下文 + DM 身份；不满足时直接返回可读拒绝。"""
    room_dir, room_id, info = _tool_context(context)
    if not room_dir or not room_id:
        return {"ok": False, "error": "战桌工具需要房间上下文；请在房间内使用。", "code": "dnd5e_vtt.room_required"}
    if not _is_dm(info, getattr(context, "agent_id", ""), getattr(context, "user_id", None)):
        return {
            "ok": False,
            "error": "战桌工具只能由 DM（AI KP 或房主/房管）使用。",
            "code": "dnd5e_vtt.dm_only",
        }
    return room_dir, room_id, info


def _entry():
    """取本插件 entry 模块（模板校验/存储/广播的唯一实现）。

    插件运行时按固定名 ``trpg_plugin_<id>_entry`` 注册模块；测试直接以
    importlib 加载本模块时按文件路径兜底装载，与 veil-map 的
    ``_load_local_module`` 同一手法。
    """
    module = sys.modules.get("trpg_plugin_dnd5e_vtt_entry")
    if module is not None:
        return module
    module_name = "dnd5e_vtt_entry_for_agent_tools"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_DIR / "entry.py")
    if spec is None or spec.loader is None:
        raise ImportError("无法加载 dnd5e-vtt entry 模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _map_service():
    """veil-map 的公开地图服务；插件缺席时返回 None，调用方自行降级。"""
    try:
        from trpg_server.plugins.runtime import get_service

        return get_service("veil-map", "map_service")
    except Exception:  # noqa: BLE001 - 服务钩子故障按缺席处理
        return None


# ============================================================
# vtt.place_template：复用模板端点的校验/存储/广播
# ============================================================


def _place_template(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    gate = _dm_gate(context)
    if isinstance(gate, dict):
        return gate
    room_dir, room_id, info = gate
    entry = _entry()
    template, error = entry._normalize_template(
        {
            "kind": arguments.get("kind"),
            "cell_x": arguments.get("cell_x"),
            "cell_y": arguments.get("cell_y"),
            "size_cells": arguments.get("size_cells"),
            "w_cells": arguments.get("w_cells"),
            "h_cells": arguments.get("h_cells"),
            "direction": arguments.get("direction"),
            "angle": arguments.get("angle"),
            "color": arguments.get("color"),
            "label": arguments.get("label"),
        }
    )
    if error:
        return {"ok": False, "error": f"模板参数不合法：{error}", "code": "dnd5e_vtt.invalid_template"}
    request_id = str(arguments.get("request_id") or "").strip()[:64]
    if not entry._TEMPLATE_REQUEST_ID.match(request_id):
        request_id = f"ai-{uuid4().hex[:12]}"
    from trpg_server.room_clock import room_lock

    with room_lock(room_dir):
        store = entry._load_templates(room_dir)
        revision = int(store.get("revision") or 0)
        for record in store.get("recent_requests") or []:
            if isinstance(record, dict) and str(record.get("request_id")) == request_id:
                # 幂等重放：同一 request_id 不再产生第二个模板。
                return {
                    "ok": True,
                    "duplicate": True,
                    "revision": revision,
                    "template_id": "",
                    "summary": "该模板操作已生效（幂等重放），没有重复放置。",
                }
        templates = [item for item in store.get("templates") or [] if isinstance(item, dict)]
        if len(templates) >= entry.MAX_TEMPLATES:
            return {
                "ok": False,
                "error": f"模板数量已达上限（{entry.MAX_TEMPLATES}），请先删除或清空。",
                "code": "dnd5e_vtt.too_many_templates",
            }
        template["id"] = f"tpl-{uuid4().hex[:12]}"
        template["placed_by"] = "ai"
        template["created_at"] = entry._utc_now()
        templates.append(template)
        store["templates"] = templates
        store["revision"] = revision + 1
        store["updated_at"] = entry._utc_now()
        records = [item for item in store.get("recent_requests") or [] if isinstance(item, dict)]
        records.append(
            {
                "request_id": request_id,
                "action": "place",
                "revision": store["revision"],
                "at": store["updated_at"],
            }
        )
        store["recent_requests"] = records[-entry.TEMPLATE_RECENT_LIMIT:]
        entry.write_json_atomic(entry._templates_path(room_dir), store)
    # 广播失败不影响已落盘的模板（玩家端拉取端点仍在）；entry 的广播
    # 依赖 Flask 应用上下文，脱离请求上下文（测试/控制台）时静默降级。
    try:
        entry._broadcast_templates(room_id, store)
    except Exception:  # noqa: BLE001
        logger.debug("dnd5e_vtt_agent_broadcast_failed room=%s", room_id, exc_info=True)
    where = f"({template['cell_x']}, {template['cell_y']})"
    size_text = (
        f"{template.get('w_cells')}x{template.get('h_cells')} 格"
        if template["kind"] == "rect"
        else f"{template.get('size_cells')} 格"
    )
    label = f"「{template['label']}」" if template.get("label") else ""
    return {
        "ok": True,
        "duplicate": False,
        "template_id": template["id"],
        "revision": store["revision"],
        "template": template,
        "summary": f"已在 {where} 放置{label}{template['kind']}模板（{size_text}，全员可见）。",
    }


PLACE_TEMPLATE_TOOL = AgentTool(
    name="dnd5e-vtt.place_template",
    description=(
        "在战桌地图上放置 D&D 5e 测量模板（circle/cone/line/rect），服务端做几何"
        "校验后登记并全员广播，玩家端立即可见。何时用哪种：龙息、燃烧之手等锥形"
        "法术用 cone（原点是施法者所在格，direction 指向目标）；火球术等爆发用 "
        "circle；闪电束用 line；雷鸣波等方形效果用 rect。cell_x/cell_y 为原点格"
        "坐标（可为负），1 格 = 5 尺，尺寸用 size_cells（rect 用 w_cells/h_cells）。"
        "放置模板只是标注范围，命中与伤害结算另走 combat.attack / combat.damage；"
        "不要为不占范围的检定放模板。request_id 相同的重复调用幂等。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["circle", "cone", "line", "rect"],
                "description": "模板类型：circle 圆形、cone 锥形、line 线形、rect 方形。",
            },
            "cell_x": {"type": "integer", "description": "原点格列坐标（施法者所在格；可为负）。"},
            "cell_y": {"type": "integer", "description": "原点格行坐标。"},
            "size_cells": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "circle/cone/line 的尺寸（格）。火球术半径 20 尺 = 4 格；15 尺锥 = 3 格。",
            },
            "w_cells": {"type": "integer", "minimum": 1, "maximum": 100, "description": "rect 宽（格），rect 必填。"},
            "h_cells": {"type": "integer", "minimum": 1, "maximum": 100, "description": "rect 高（格），rect 必填。"},
            "direction": {
                "type": "object",
                "properties": {
                    "dx": {"type": "number"},
                    "dy": {"type": "number"},
                },
                "required": ["dx", "dy"],
                "description": "cone/line 必填：单位方向向量，如朝东 {dx:1,dy:0}、朝东北 {dx:1,dy:-1}。",
            },
            "angle": {"type": "number", "description": "cone 张角（度），默认 53.13。"},
            "label": {"type": "string", "description": "模板标注（≤60 字），如「燃烧之手」。"},
            "color": {"type": "string", "description": "可选 #RRGGBB；缺省按类型配色。"},
            "request_id": {"type": "string", "description": "可选幂等键；同键重复放置只生效一次。"},
        },
        "required": ["kind", "cell_x", "cell_y"],
    },
    handler=_place_template,
    rulesets=("dnd5e",),
)


# ============================================================
# vtt.switch_scene：veil-map 场景激活原语 + 场景切换广播
# ============================================================


def _scene_candidates(room_dir: Path, context: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """场景候选：veil-map 已存场景（服务目录）+ 世界树地图大节点。"""
    scenes: list[dict[str, Any]] = []
    service = _map_service()
    if service is not None and hasattr(service, "list_scenes"):
        listing = service.list_scenes(room_dir)
        if isinstance(listing, dict):
            raw = listing.get("scenes") if isinstance(listing.get("scenes"), list) else []
            scenes = [item for item in raw if isinstance(item, dict)]
    areas: list[dict[str, Any]] = []
    try:
        from trpg_server.agents.narrative.runtime import NarrativeRuntime
        from trpg_server.agents.narrative.mapgraph import area_id_of

        runtime = NarrativeRuntime.load(context)
        for node in runtime.bundle.nodes_of("location"):
            if node.is_map:
                areas.append({"area_id": node.id, "title": node.title or node.id, "aliases": list(node.aliases)})
    except Exception:  # noqa: BLE001 - 无剧本/无图谱时只有已存场景可切
        areas = []
    return scenes, areas


def _match_scene(ref: str, scenes: list[dict[str, Any]], areas: list[dict[str, Any]]) -> tuple[str, str]:
    """scene_id 精确 → 名称精确 → 前缀/子串模糊；返回 ``(area_id, title)``。"""
    folded = ref.casefold()
    for item in scenes:
        if str(item.get("scene_id") or "").strip() == ref:
            return str(item.get("scene_id")), str(item.get("title") or ref)
    for item in scenes:
        if str(item.get("title") or "").casefold() == folded:
            return str(item.get("scene_id")), str(item.get("title") or ref)
    for item in areas:
        if str(item.get("area_id") or "").strip() == ref:
            return str(item["area_id"]), str(item.get("title") or ref)
    for item in [*scenes, *areas]:
        title = str(item.get("title") or "").casefold()
        if title and (title.startswith(folded) or folded in title):
            key = str(item.get("scene_id") or item.get("area_id") or "")
            if key:
                return key, str(item.get("title") or key)
    return "", ""


def _emit_scene_switch(room_id: str, scene_id: str, title: str) -> None:
    """「场景切换」事件：前端拿它做加载遮罩；广播失败不影响切换结果。"""
    try:
        from flask import current_app

        socketio = current_app.extensions.get("socketio")
        if socketio is None:
            return
        socketio.emit(
            "dnd5e_vtt_scene_switch",
            {"room_id": str(room_id), "scene_id": str(scene_id), "title": str(title), "by": "ai_kp"},
            room=str(room_id),
        )
    except Exception:  # noqa: BLE001
        logger.debug("dnd5e_vtt_scene_switch_emit_failed room=%s", room_id, exc_info=True)


def _switch_scene(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    gate = _dm_gate(context)
    if isinstance(gate, dict):
        return gate
    room_dir, room_id, info = gate
    ref = str(
        arguments.get("scene")
        or arguments.get("scene_id")
        or arguments.get("area")
        or arguments.get("to_area")
        or ""
    ).strip()
    if not ref:
        return {"ok": False, "error": "请提供目标场景的 id 或名称。", "code": "dnd5e_vtt.scene_required"}
    service = _map_service()
    if service is None:
        return {
            "ok": False,
            "error": "地图插件（veil-map）未启用，无法切换场景。",
            "code": "dnd5e_vtt.map_unavailable",
        }
    scenes, areas = _scene_candidates(room_dir, context)
    area_id, title = _match_scene(ref, scenes, areas)
    if not area_id:
        hints = "、".join(
            f"「{str(item.get('title') or item.get('scene_id') or item.get('area_id'))}」"
            for item in [*scenes, *areas][:MAX_SCENE_HINTS]
        )
        hint_text = f"可用场景：{hints}。" if hints else "当前没有可切换的地图场景。"
        return {
            "ok": False,
            "error": f"找不到场景「{ref}」。{hint_text}",
            "code": "dnd5e_vtt.scene_not_found",
        }
    result = service.activate_for_area(str(room_dir), info, area_id)
    if not isinstance(result, dict) or not result.get("ok"):
        error_text = str((result or {}).get("error") or "目标场景不可用。")
        return {
            "ok": False,
            "error": f"切换到「{title}」失败：{error_text}",
            "code": str((result or {}).get("code") or "map.scene_unavailable"),
        }
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    scene_id = str(result.get("scene_id") or area_id)
    resolved_title = str(state.get("title") or title or scene_id)
    changed = bool(result.get("changed"))
    if changed:
        # 复用 veil-map 的观察者裁剪广播（迷雾开启时逐成员推送），不复制其裁剪逻辑。
        if hasattr(service, "broadcast_state"):
            try:
                service.broadcast_state(room_id, state)
            except Exception:  # noqa: BLE001
                logger.debug("dnd5e_vtt_scene_broadcast_failed room=%s", room_id, exc_info=True)
        _emit_scene_switch(room_id, scene_id, resolved_title)
    reason = str(arguments.get("reason") or "").strip()
    summary = f"已切换到场景「{resolved_title}」" if changed else f"场景「{resolved_title}」已是当前场景"
    if reason:
        summary += f"：{reason[:80]}"
    return {
        "ok": True,
        "scene_id": scene_id,
        "area_id": str(result.get("area_id") or area_id),
        "title": resolved_title,
        "changed": changed,
        "created": bool(result.get("created")),
        "placements": list(result.get("placements") or []),
        "summary": summary + ("。" if not summary.endswith("。") else ""),
    }


SWITCH_SCENE_TOOL = AgentTool(
    name="dnd5e-vtt.switch_scene",
    description=(
        "切换战桌的地图场景（veil-map 场景激活原语），并广播「场景切换」事件，"
        "所有客户端会重绘地图并短暂显示加载遮罩。scene 接受场景 id 或名称，支持"
        "模糊匹配（如「墓穴」）；不确定场景名时先用 dnd5e-vtt.worldtree_recall "
        "检索世界树节点。整个队伍移到新地点、或剧情转场需要换图时调用；区域内"
        "的小范围移动用 veil-map.move_token，不要为挪几格切场景。切换失败时会"
        "返回当前可用场景列表。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scene": {"type": "string", "description": "目标场景 id 或名称（支持模糊匹配）。"},
            "reason": {"type": "string", "description": "给玩家的一句过场说明（可选）。"},
        },
        "required": ["scene"],
    },
    handler=_switch_scene,
    rulesets=("dnd5e",),
)


# ============================================================
# vtt.roll_broadcast：走现有房间掷骰消息管线（dice_roll 元数据）
# ============================================================


def _roll_broadcast(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    gate = _dm_gate(context)
    if isinstance(gate, dict):
        return gate
    from trpg_server.agents.tools.dice import (
        _finalize_dice_metadata,
        _log_roll_result,
        roll_dice,
    )

    expression = str(arguments.get("expression") or "").strip()
    if not expression:
        return {"ok": False, "error": "请提供骰表达式，例如 1d20 或 8d6。", "code": "dnd5e_vtt.expression_required"}
    label = str(arguments.get("label") or "").strip()[:MAX_ROLL_LABEL]
    roller = str(arguments.get("roller") or "").strip()[:MAX_ROLL_LABEL]
    result = roll_dice({"expression": expression.lower(), "show_table": True})
    if not isinstance(result, dict) or result.get("error"):
        return {
            "ok": False,
            "error": f"骰表达式无法解析：{(result or {}).get('error') or expression}",
            "code": "dnd5e_vtt.invalid_expression",
        }
    # 标注掷骰身份：骰卡内容 + dice_roll 元数据都带上 label/roller，
    # 其余字段与 dice.roll 的 AI 工具消息形态完全一致。
    if label or roller:
        base = str(result.get("summary") or "")
        prefix = f"{roller}的{label}" if (roller and label) else (roller or label or "掷骰广播")
        result["summary"] = f"{prefix}：{base}"
        message = result.get("visible_message")
        if isinstance(message, dict):
            message["content"] = result["summary"]
            metadata = message.get("metadata")
            if isinstance(metadata, dict):
                if label:
                    metadata["label"] = label
                if roller:
                    metadata["roller"] = roller
                dice = metadata.get("dice_roll")
                if isinstance(dice, dict):
                    if roller:
                        dice["roller"] = roller
                    if label:
                        dice["label"] = label
    result = _finalize_dice_metadata(result, context, show_table=True)
    _log_roll_result(result, context, tool_name="dnd5e-vtt.roll_broadcast", actor=roller or "KP")
    payload = {
        "ok": True,
        "expression": result.get("expression"),
        "rolls": result.get("rolls"),
        "total": result.get("total"),
        "summary": str(result.get("summary") or ""),
        "broadcast": True,
    }
    # visible_message 是聊天卡/骰桌动画的广播载体（runtime 把它收进 tool_messages
    # 后剥离模型回传），必须随结果返回，否则骰卡永远到不了房间。
    if isinstance(result.get("visible_message"), dict):
        payload["visible_message"] = result["visible_message"]
    return payload


ROLL_BROADCAST_TOOL = AgentTool(
    name="dnd5e-vtt.roll_broadcast",
    description=(
        "广播一次掷骰并让全员看到骰桌动画：走房间现有掷骰消息管线，骰卡进聊天流、"
        "地图上落一桌 3D 骰子（受 DM 的骰桌开关与上限约束）。expression 是骰表达式"
        "（1d20、2d6、8d6，可加法混合如 1d100+2d6；不支持固定加值，带加值的检定"
        "走 dnd5e.roll_check）；label 说明这把骰子是什么（如「强盗头目的火焰喷吐"
        "伤害」）；roller 是掷骰的 NPC 名。何时用：为 NPC/怪物掷攻击、伤害、豁免"
        "或表中随机结果，且结果需要玩家看到时；玩家自己的检定用 dnd5e.roll_check，"
        "不需要动画的内部结算不要广播。骰值由服务端生成，不得编造结果。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "骰表达式，如 1d20、2d6、8d6、1d100+2d6。"},
            "label": {"type": "string", "description": "这把骰子的用途（≤60 字），显示在骰卡上。"},
            "roller": {"type": "string", "description": "掷骰者（NPC 名），显示在骰卡与动画上。"},
        },
        "required": ["expression"],
    },
    handler=_roll_broadcast,
    rulesets=("dnd5e",),
)


# ============================================================
# worldtree.recall：剧本 narrative.json 图谱检索（DM 视角）
# ============================================================


def _node_score(node: Any, tokens: list[str]) -> float:
    """关键词打分：精确 id/标题/别名 > 关键词命中 > 子串命中。"""
    title = str(node.title or "").casefold()
    node_id = str(node.id or "").casefold()
    aliases = [str(item).casefold() for item in (node.aliases or [])]
    keywords = [str(item).casefold() for item in (node.keywords or [])]
    score = 0.0
    for token in tokens:
        folded = token.casefold()
        if not folded:
            continue
        if folded == node_id or folded == title or folded in aliases:
            score += 3.0
        elif any(folded == item for item in keywords):
            score += 2.0
        elif (
            folded in title
            or folded in node_id
            or any(folded in item for item in aliases)
            or any(folded in item for item in keywords)
        ):
            score += 1.0
    return score


def _worldtree_recall(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    gate = _dm_gate(context)
    if isinstance(gate, dict):
        return gate
    from trpg_server.agents.narrative.runtime import NarrativeRuntime, NarrativeRuntimeError

    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "请提供检索关键词。", "code": "dnd5e_vtt.query_required"}
    try:
        top_k = max(1, min(int(arguments.get("top_k") or DEFAULT_RECALL_NODES), MAX_RECALL_NODES))
    except (TypeError, ValueError):
        top_k = DEFAULT_RECALL_NODES
    try:
        runtime = NarrativeRuntime.load(context)
    except NarrativeRuntimeError as exc:
        return {
            "ok": False,
            "error": f"世界树不可用：{exc}",
            "code": "dnd5e_vtt.worldtree_unavailable",
        }
    except Exception as exc:  # noqa: BLE001 - 图谱损坏等按不可用降级
        return {
            "ok": False,
            "error": f"世界树不可用：{exc}",
            "code": "dnd5e_vtt.worldtree_unavailable",
        }
    bundle = runtime.bundle
    tokens = [item for item in _QUERY_SPLIT.split(query) if item]
    scored: list[tuple[float, Any]] = []
    for node in bundle.nodes:
        score = _node_score(node, tokens)
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].id)))
    matched = [node for _, node in scored[:top_k]]
    if not matched:
        return {
            "ok": True,
            "kp_view": True,
            "query": query,
            "matches": [],
            "summary": f"世界树里没有匹配「{query}」的节点；换个说法或用更短的词再试。",
        }
    items: list[dict[str, Any]] = []
    for node in matched:
        body = str(node.body or "").strip()
        item: dict[str, Any] = {
            "node_id": node.id,
            "kind": node.kind,
            "title": node.title or node.id,
            "summary": str(node.summary or "").strip()[:200],
            "visibility": node.visibility,
        }
        if body:
            item["body"] = body[:MAX_BODY_EXCERPT] + ("…" if len(body) > MAX_BODY_EXCERPT else "")
        if node.kp_notes:
            item["kp_notes"] = str(node.kp_notes).strip()[:MAX_BODY_EXCERPT]
        edges: list[dict[str, Any]] = []
        for edge in bundle.edges:
            if edge.from_id != node.id and edge.to_id != node.id:
                continue
            other_id = edge.to_id if edge.from_id == node.id else edge.from_id
            other = bundle.node(other_id)
            row: dict[str, Any] = {
                "type": edge.type,
                "label": edge.label,
                "to": other_id,
                "to_title": (other.title if other is not None and other.title else other_id),
                "visibility": edge.visibility,
            }
            if edge.condition:
                row["condition"] = edge.condition
            if edge.kp_notes:
                row["kp_notes"] = str(edge.kp_notes).strip()[:200]
            edges.append(row)
            if len(edges) >= MAX_EDGE_SUMMARY:
                break
        item["edges"] = edges
        items.append(item)
    return {
        "ok": True,
        "kp_view": True,
        "query": query,
        "matches": items,
        "summary": f"世界树命中 {len(items)} 个节点（DM 视角，含 kp_only 内容）。",
        "note": "以上是 DM 视角检索结果；kp_notes 与 kp_only 节点只可用来铺垫剧情，不得原文复述给玩家。",
    }


WORLDTREE_RECALL_TOOL = AgentTool(
    name="dnd5e-vtt.worldtree_recall",
    description=(
        "检索本房间剧本的世界树（narrative.json 图谱）：按关键词匹配节点（id/标题/"
        "别名/关键词），返回节点摘要与相邻边，含 kp_only 内容与 kp_notes。何时用："
        "需要回忆某个地点/NPC/线索/物品的设定、确认它们与谁相连、或找当前章节的"
        "隐藏真相时；切场景前查目标地点的准确名称也用它。检索结果是 DM 视角，"
        "kp_notes 与 kp_only 节点不得原文复述给玩家。查规则原文（法术/怪物数值）"
        "用 knowledge.search_ruleset 或 content.search，不要用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词（可空格分隔多个），如「墓穴 银钥匙」。"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "description": "最多返回几个节点，默认 3。"},
        },
        "required": ["query"],
    },
    handler=_worldtree_recall,
    rulesets=("dnd5e",),
)


# ============================================================
# KP 战桌状态行（§7.2 最小版）：插件服务，一行摘要全部取自现有快照
# ============================================================


class _KpContextService:
    """给 KP 提示词装配层用的战桌状态行。

    核心只认 ``get_service("dnd5e-vtt", "kp_context")`` 这个公开通道
    （与 veil-map ``map_service`` 同一机制）；插件缺席/非 dnd5e 房间/
    任何读取失败都返回空列表，KP 主流程不受影响。
    """

    def kp_prompt_fragments(self, context: Any) -> list[str]:
        try:
            room_dir = getattr(context, "room_dir", None)
            if not room_dir:
                return []
            info_fn = getattr(context, "room_info", None)
            info = info_fn() if callable(info_fn) else {}
            if not isinstance(info, dict):
                return []
            if self._ruleset_id(info, getattr(context, "scenarios_dir", None)) != "dnd5e":
                return []
            line = battle_status_line(Path(room_dir), info)
            return [line] if line else []
        except Exception:  # noqa: BLE001 - 状态行失败绝不阻断 KP 回合
            logger.debug("dnd5e_vtt_kp_context_failed", exc_info=True)
            return []

    @staticmethod
    def _ruleset_id(info: dict[str, Any], scenarios_dir: Any) -> str:
        try:
            from trpg_server.room_rules import resolve_rule_id

            return str(resolve_rule_id(info, scenarios_dir=scenarios_dir) or "").strip().lower()
        except Exception:  # noqa: BLE001 - 解析失败退回房间显式规则集
            return str((info or {}).get("ruleset_id") or "").strip().lower()


def battle_status_line(room_dir: Path, info: dict[str, Any]) -> str:
    """「战桌状态」一行摘要：活动场景/棋子数/战斗回合/已放置模板数。

    数据全部取自现有快照：veil-map 场景目录（公开服务）、房间战斗状态
    （state.json）与本插件模板存储；任何一源缺席就如实省略，不造假数据。
    """
    scene_text = "未启用地图"
    token_text = ""
    service = _map_service()
    if service is not None and hasattr(service, "list_scenes"):
        # 插件在场：零场景与未启用是两回事，别把「空目录」误报成「没地图」。
        scene_text = "无地图场景"
        listing = service.list_scenes(room_dir)
        scenes = [item for item in (listing.get("scenes") or []) if isinstance(item, dict)]
        active = next((item for item in scenes if item.get("active")), None)
        if active is not None:
            scene_text = f"「{str(active.get('title') or active.get('scene_id'))}」"
            token_text = f"（地图棋子 {int(active.get('tokens') or 0)}）"
        elif scenes:
            scene_text = "无活动场景"
    combat_text = "未开战"
    try:
        from trpg_server.agents.room_state import load_room_state

        combat = load_room_state(room_dir).get("combat")
        if isinstance(combat, dict) and combat.get("active"):
            order = [str(name).strip() for name in combat.get("order") or [] if str(name).strip()]
            try:
                turn = int(combat.get("turn"))
            except (TypeError, ValueError):
                turn = 0
            current = order[turn] if 0 <= turn < len(order) else ""
            combat_text = f"第 {int(combat.get('round') or 1)} 回合，当前行动：{current or '未知'}（先攻 {len(order)} 人）"
    except Exception:  # noqa: BLE001 - 状态读取失败按未开战呈现
        combat_text = "战斗状态不可读"
    template_count = 0
    try:
        template_count = len(_entry()._load_templates(room_dir).get("templates") or [])
    except Exception:  # noqa: BLE001
        template_count = 0
    return (
        f"【战桌状态】场景 {scene_text}{token_text}｜战斗：{combat_text}｜"
        f"测量模板 {template_count} 个。战桌工具（仅 DM 可用）：dnd5e-vtt.place_template "
        "放法术范围模板、dnd5e-vtt.switch_scene 切换地图场景、"
        "dnd5e-vtt.roll_broadcast 广播 NPC 掷骰动画、dnd5e-vtt.worldtree_recall 查世界树。"
    )


_KP_CONTEXT_SERVICE = _KpContextService()
_SERVICE_REGISTERED = False


def _register_kp_context() -> None:
    """把 KP 状态行服务挂进插件服务表（veil-map map_service 同一通道）。"""
    global _SERVICE_REGISTERED
    if _SERVICE_REGISTERED:
        return
    try:
        from trpg_server.plugins.runtime import register_service

        register_service("dnd5e-vtt", "kp_context", _KP_CONTEXT_SERVICE)
    except Exception:  # noqa: BLE001 - 旧核心没有服务表时静默跳过
        return
    _SERVICE_REGISTERED = True


def create_tools() -> list[AgentTool]:
    """插件 agent_tools 能力的工厂：核心在插件激活期调用一次。"""
    _register_kp_context()
    return [PLACE_TEMPLATE_TOOL, SWITCH_SCENE_TOOL, ROLL_BROADCAST_TOOL, WORLDTREE_RECALL_TOOL]


__all__ = [
    "PLACE_TEMPLATE_TOOL",
    "SWITCH_SCENE_TOOL",
    "ROLL_BROADCAST_TOOL",
    "WORLDTREE_RECALL_TOOL",
    "battle_status_line",
    "create_tools",
]
