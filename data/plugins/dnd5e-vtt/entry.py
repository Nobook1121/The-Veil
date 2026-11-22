"""D&D 5e 战桌插件（dnd5e-vtt）服务端：面板数据、测量模板与棋子操作卡。

M2 骨架 + M3 第一段（地图交互）：

- ``GET /api/plugins/dnd5e-vtt/panels?room=<room_id>``：按观察者（DM/玩家）
  裁剪的面板清单。**场景面板与角色面板的 NPC/怪物分组只对 DM 下发**，
  玩家的响应里整块不出现（不是前端隐藏）；前端只消费、不做本地权限判断。
- 角色速览 ``quick_stats``（本刀）：角色面板的每个玩家条目与（DM 视角的）
  NPC 条目都多一个 ``quick_stats`` 字段——四盒（AC / 命中 / 法术豁免 DC /
  被动感知）+ 当前 HP + 有限资源池（生命骰剩余、法术位合计，长休刷新）。
  数值**一律取 dnd5e 插件已有的派生值**：卡面已落盘的派生键（核心
  ``sync_derived_stats`` 写回）优先，缺失的由 ``plugin.schema.derive`` 补齐；
  命中加值取插件 ``combat`` 推导的攻击行（``attack_bonus``），卡上无可用
  武器时退回法术命中 ``spellAttackBonus``；资源池走 ``rest.hit_dice_state``
  与 ``spells.spell_slots_from_sheet``。本插件不重算任何 5e 公式，拿不到就
  少给或给 null，绝不编造。**没有角色卡的成员也带 quick_stats（全 null +
  resources 空数组）**，前端据此渲染「未建卡」态。
- 测量模板（圆/锥/线/方）：房间级登记 + 广播。
  ``GET  /api/plugins/dnd5e-vtt/rooms/<id>/templates`` 全员可读（玩家端只读
  展示）；``POST`` 仅 DM（玩家 403），``request_id`` 幂等 + ``revision``
  乐观并发，接口风格学 veil-map（四态回执）。存
  ``<room>/plugins/dnd5e-vtt/templates.json``；写入成功后向房间广播
  ``dnd5e_vtt_templates``，所有客户端重绘；清空（clear）同样走该端点且仅 DM。
- 棋子操作卡数据：``GET /api/plugins/dnd5e-vtt/rooms/<id>/token-actions``。
  DM 拉任意棋子的动作清单（攻击/法术/特性/能力/物品，来自房间战斗状态、
  剧本 NPC 模块 statblock 与成员角色卡）；**玩家只允许查自己绑定的棋子且
  响应不含 actions 键**（服务端不下发 → 前端 DOM 不渲染，拍板决策）。
- 战桌 UI 由 ``ui/dnd5e-vtt.js`` 通过 E0 的 ``room.rail.right`` 槽挂载，
  并在同一挂载里自绘画布左上工具组（选择/测距尺/模板四型/清空）与棋子
  操作卡浮层；布局沿用 dnd5e 规则集已声明的 ``dnd5e-canvas``。
- veil-map（地图）是**可选**依赖：不 import 它的 Python 模块、不读它的
  存储文件。空间几何全部走它的公开 HTTP 端点（``/spatial/range`` 取格、
  ``/spatial/line`` 测距判墙），由前端直接调用；veil-map 缺席或调用失败时
  前端按本地几何近似并明说（锥 53° 与方形没有对应原语，本来就是本地近似）。

数据全部取自现有核心存储：战斗快照与事件流来自房间状态（``state.json``），
玩家角色来自成员卡，NPC 目录与动作清单来自剧本 NPC 模块（世界包导入的
产物，``statblock`` 含结构化 attacks / spellcasting / senses）。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, session

from trpg_server.agents.room_state import load_room_state
from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.responses import error_response, success_response
from trpg_server.room_access import can_access, find_room, is_elevated
from trpg_server.room_clock import room_lock
from trpg_server.room_rules import resolve_rule_id

bp = Blueprint("dnd5e_vtt", __name__, url_prefix="/api/plugins/dnd5e-vtt")
logger = logging.getLogger(__name__)

SUPPORTED_RULESETS = {"dnd5e"}
OWNER_ROLES = {"owner", "admin"}
MAX_JOURNAL_ENTRIES = 30
MAX_NPCS = 60
MAX_PLAYERS = 60
# 玩家侧日志只放行公开事件类型；KP 手记（room console）等不入玩家流。
PLAYER_SAFE_EVENT_KINDS = {"player_decision"}

# ------------------------------------------------------------ 测量模板常量
TEMPLATES_VERSION = 1
TEMPLATE_KINDS = ("circle", "cone", "line", "rect")
TEMPLATE_ACTIONS = ("place", "move", "delete", "clear")
MAX_TEMPLATES = 60
MIN_TEMPLATE_CELLS = 1
MAX_TEMPLATE_CELLS = 100  # 单边最大格数（500 尺）
# FVTT 锥形模板默认张角约 53.13°（对角网格）；veil-map 的锥原语固定 90°，
# 几何不匹配，受影响格由前端本地近似并明说（见 ui/dnd5e-vtt.js 注释）。
DEFAULT_CONE_ANGLE = 53.13
MIN_CONE_ANGLE = 5.0
MAX_CONE_ANGLE = 360.0
TEMPLATE_COLORS = {
    "circle": "#e06666",
    "cone": "#f6b26b",
    "line": "#6fa8dc",
    "rect": "#93c47d",
}
MAX_TEMPLATE_LABEL = 60
TEMPLATE_RECENT_LIMIT = 64
_TEMPLATE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TEMPLATE_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_CARD_TEXT = 120

PANEL_CHAT = {
    "id": "chat",
    "title": "聊天",
    "hint": "房间聊天流：发言、掷骰卡与检定回执都在这里。",
}
PANEL_ITEMS = {
    "id": "items",
    "title": "物品",
    "items": [],
    "hint": "物品与战利品目录将在角色卡装备与剧本数据接入后列出。",
}


def _login_required():
    if "user_id" not in session:
        return error_response("请先登录。", 401, "auth.required")
    return None


def _active_member(info: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    for item in info.get("members") or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_active", True) is False or item.get("status") == "removed":
            continue
        if str(item.get("user_id")) == user_id:
            return item
    return None


def _is_manager(info: dict[str, Any]) -> bool:
    """观察者角色判定：房主/房管/站点管理员视为 DM；权限口径与核心一致。"""
    user_id = str(session.get("user_id") or "")
    member = _active_member(info, user_id)
    role = str((member or {}).get("room_role") or "").strip().lower()
    if member is not None and str(member.get("user_id")) == str(info.get("creator_id")):
        role = "owner"
    return bool(is_elevated() or role in OWNER_ROLES)


def _ruleset_id(info: dict[str, Any]) -> str:
    """规则集解析：优先核心解析器；解析失败时退回房间显式值（学 veil-map）。"""
    try:
        return str(resolve_rule_id(info, scenarios_dir=current_app.config.get("SCENARIOS_DIR")) or "").strip().lower()
    except Exception:  # noqa: BLE001 - 未启用规则集等场景退回显式字段再判断
        return str((info or {}).get("ruleset_id") or "").strip().lower()


def _combat_view(state: dict[str, Any]) -> dict[str, Any]:
    """战斗面板数据骨架：先攻名单来自房间战斗状态，未开战给空态。

    完整战斗交互（掷先攻/回合步进/操作按钮）仍由现有 ``/api/rooms/<id>/combat``
    与战斗轮面板承担；本端点只给目录级快照，前端据此渲染先攻列表。
    """
    combat = state.get("combat") if isinstance(state.get("combat"), dict) else None
    if not combat or not combat.get("active"):
        return {"active": False, "round": None, "turn": None, "order": [], "current": None}
    order = [str(name or "").strip() for name in combat.get("order") or [] if str(name or "").strip()]
    try:
        turn = int(combat.get("turn"))
    except (TypeError, ValueError):
        turn = 0
    current = order[turn] if 0 <= turn < len(order) else None
    return {
        "active": True,
        "round": combat.get("round") or 1,
        "turn": turn if 0 <= turn < len(order) else None,
        "order": order,
        "current": current,
    }


def _card_text(card: Any, *keys: str, limit: int = 120) -> str:
    if not isinstance(card, dict):
        return ""
    for key in keys:
        value = card.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:limit]
    return ""


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return fallback


def _optional_int(value: Any) -> int | None:
    """面板里的可选数值：拿不到就是 ``None``（前端渲染成「—」），不落 0。"""
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ 角色速览常量
# 派生值全部由 dnd5e 插件给出；这里只列要读的键名（顺序即回退顺序）。
QUICK_DERIVED_KEYS = (
    "maxHp",
    "armorClass",
    "proficiencyBonus",
    "initiative",
    "passivePerception",
    "spellSaveDC",
    "spellAttackBonus",
)
# 资源池最多两个：生命骰优先，其次法术位合计（都是长休刷新）。
QUICK_RESOURCE_LIMIT = 2
QUICK_RESET_LONG_REST = "long_rest"
# 命中加值来源：主要武器攻击加值 → 法术命中加值。
QUICK_ATTACK_WEAPON = "weapon"
QUICK_ATTACK_SPELL = "spell"


def _dnd5e_plugin():
    """规则集插件对象（通过核心注册表拿，不直接 import 插件私有实现）。"""
    try:
        from trpg_server.ruleset import get_plugin

        return get_plugin("dnd5e")
    except Exception:  # noqa: BLE001 - 插件未装载时降级为「无速览」
        logger.debug("dnd5e-vtt: 取 dnd5e 插件失败", exc_info=True)
        return None


def _dnd5e_modules() -> dict[str, Any] | None:
    """dnd5e 插件的模块级助手（派生 / 资源池的现成逻辑，只读不改）。

    dnd5e-vtt 依赖 dnd5e（plugin.json 的 depends），运行期插件目录已在
    ``sys.path`` 上；单独加载 entry.py 做静态检查 / 单测时可能还没挂上，
    因此统一按「拿不到就不给速览」处理——绝不在本插件里重算规则公式。
    """
    try:
        from dnd5e_plugin import rest as rest_module
        from dnd5e_plugin import spells as spells_module
        from dnd5e_plugin import statblock as statblock_module
    except Exception:  # noqa: BLE001 - 插件未装入 sys.path：降级
        return None
    return {
        "rest": rest_module,
        "spells": spells_module,
        "statblock": statblock_module,
    }


def _equipment_entry(identifier: str) -> dict[str, Any] | None:
    """装备条目查询（核心数据包目录）：用来判断某件装备是不是武器。"""
    key = _trim_card_text(identifier, 60)
    if not key:
        return None
    try:
        from trpg_server.plugins import runtime

        row = runtime.content_packs_for("dnd5e").get("dnd5e", "equipment", key)
    except Exception:  # noqa: BLE001 - 数据包未启用：按查不到处理
        return None
    return row if isinstance(row, dict) else None


def _derived_card(card: dict[str, Any], plugin: Any) -> dict[str, Any]:
    """补派生值：卡面已落盘的键优先，缺的交给插件 ``schema.derive``。"""
    data = dict(card or {})
    if plugin is None:
        return data
    if all(data.get(key) is not None for key in QUICK_DERIVED_KEYS):
        return data
    payload = dict(data)
    if not str(payload.get("rulesetId") or "").strip():
        # 战桌只服务 dnd5e 房间；旧卡可能没写 rulesetId，按房间规则集补。
        payload["rulesetId"] = "dnd5e"
    try:
        derived = dict(plugin.schema.derive(payload) or {})
    except Exception:  # noqa: BLE001 - 卡面异常时保留原卡，不阻断面板
        logger.debug("dnd5e-vtt: 派生角色卡失败", exc_info=True)
        return data
    for key in QUICK_DERIVED_KEYS:
        if data.get(key) is None and derived.get(key) is not None:
            data[key] = derived[key]
    return data


def _quick_hp(card: dict[str, Any], state: dict[str, Any]) -> dict[str, int]:
    """当前 HP：房间 ``character_state`` 优先，其次卡面扁平键，最后卡片 ``hp``。

    三源都没有（例如只填了名字与等级的简化卡）时给 ``max=0``，前端按
    「未知」渲染——不用派生默认值凑一个假血量。
    """
    sheet = card.get("sheet") if isinstance(card.get("sheet"), dict) else {}
    hp_field = card.get("hp") if isinstance(card.get("hp"), dict) else {}
    sheet_hp = sheet.get("hp") if isinstance(sheet.get("hp"), dict) else {}
    maximum = 0
    for source in (
        state.get("max_hp"),
        card.get("maxHp"),
        hp_field.get("max"),
        sheet_hp.get("max"),
        card.get("max_hp"),
    ):
        value = _as_int(source, 0)
        if value > 0:
            maximum = value
            break
    current: int | None = None
    for source in (
        state.get("current_hp"),
        card.get("currentHp"),
        card.get("current_hp"),
        hp_field.get("current"),
        sheet_hp.get("current"),
    ):
        if source is None or source == "":
            continue
        current = _as_int(source, 0)
        break
    temp = 0
    for source in (card.get("tempHp"), hp_field.get("temp"), sheet_hp.get("temp")):
        value = _as_int(source, 0)
        if value > 0:
            temp = value
            break
    if maximum <= 0:
        return {"value": 0, "max": 0, "temp": max(0, temp)}
    if current is None:
        current = maximum
    return {"value": max(0, min(maximum, current)), "max": maximum, "temp": max(0, temp)}


def _quick_resources(
    card: dict[str, Any],
    state: dict[str, Any],
    modules: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """有限资源池：生命骰剩余（优先）+ 法术位合计，均按长休刷新。

    生命骰骰型 / 池上限走插件 ``hit_dice_state``（卡面显式 ``hit_dice`` 优先，
    否则按职业生命骰与等级），法术位走 ``spell_slots_from_sheet``（卡面
    ``spellcasting.slots`` 优先，否则按职业表）。都没有就给空数组。
    注意 5e 长休只恢复**一半**生命骰、法术位全部恢复；``reset`` 字段沿用
    契约的 ``long_rest``，细则由前端 tooltip 说明。
    """
    if modules is None:
        return []
    sheet = card.get("sheet") if isinstance(card.get("sheet"), dict) else {}
    holder = dict(state) if isinstance(state, dict) else {}
    resources: list[dict[str, Any]] = []
    dice: dict[str, Any] = {}
    try:
        dice = dict(modules["rest"].hit_dice_state(holder, sheet) or {})
    except Exception:  # noqa: BLE001 - 生命骰读不到就不给这一池
        logger.debug("dnd5e-vtt: 生命骰状态读取失败", exc_info=True)
        dice = {}
    maximum = _as_int(dice.get("max"), 0)
    if maximum > 0:
        used = max(0, min(maximum, _as_int(dice.get("used"), 0)))
        resources.append(
            {
                "id": "hit_dice",
                "label": "生命骰",
                "value": maximum - used,
                "max": maximum,
                # 骰型（d6/d8…）随行下发，前端显示「生命骰 5/5 · d8」。
                "die": _as_int(dice.get("die"), 0),
                "reset": QUICK_RESET_LONG_REST,
            }
        )
    slots: dict[str, Any] = {}
    try:
        slots = dict(modules["spells"].spell_slots_from_sheet(sheet) or {})
    except Exception:  # noqa: BLE001 - 法术位读不到就不给这一池
        logger.debug("dnd5e-vtt: 法术位读取失败", exc_info=True)
        slots = {}
    total = 0
    used = 0
    for row in slots.values():
        if not isinstance(row, dict):
            continue
        total += max(0, _as_int(row.get("max"), 0))
        used += max(0, _as_int(row.get("used"), 0))
    if total > 0:
        resources.append(
            {
                "id": "spell_slots",
                "label": "法术位",
                "value": max(0, total - min(total, used)),
                "max": total,
                "reset": QUICK_RESET_LONG_REST,
            }
        )
    return resources[:QUICK_RESOURCE_LIMIT]


def _quick_attack(card: dict[str, Any], plugin: Any) -> tuple[int | None, str]:
    """命中加值：优先主要武器攻击加值，其次法术命中加值。

    字段语义（写清在这里，前后端同口径）：
    - ``attack_bonus`` = d20 攻击检定要加的加值（不含 d20 本身）；
    - 卡上有可用武器（``sheet.equipment`` 里存在装备中的 weapon 条目）时，
      取插件 ``combat.prepare_combatant`` 推导出的攻击行里**最高**的
      ``attack_bonus``（= 熟练加值 + 力量/敏捷调整值），来源 ``weapon``；
    - 没有可用武器（纯施法者 / 只填了名字的简化卡）时退回派生值
      ``spellAttackBonus``（法术攻击加值），来源 ``spell``；
    - 两者都没有 → ``None``，前端渲染「—」。
    """
    if plugin is None:
        return None, ""
    sheet = card.get("sheet") if isinstance(card.get("sheet"), dict) else {}
    has_weapon = False
    raw_items = sheet.get("equipment")
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items")
    for item in raw_items if isinstance(raw_items, list) else []:
        identifier = ""
        if isinstance(item, dict):
            identifier = _trim_card_text(item.get("id") or item.get("item_id") or item.get("name"), 60)
            equipped = item.get("equipped", item.get("worn", item.get("equip", True)))
            if not bool(equipped):
                continue
        elif isinstance(item, str):
            identifier = _trim_card_text(item, 60)
        if not identifier:
            continue
        # 条目的分类只有数据包知道：查核心装备目录，别在本地猜武器类型。
        row = _equipment_entry(identifier)
        if isinstance(row, dict) and str(row.get("category") or "").strip().lower() == "weapon":
            has_weapon = True
            break
    combat = getattr(plugin, "combat", None)
    if has_weapon and combat is not None:
        holder: dict[str, Any] = {
            "name": _card_text(card, "name", limit=60) or "冒险者",
            "is_player": True,
            "level": _as_int(card.get("level"), 1),
        }
        try:
            combat.prepare_combatant(holder, card=card, settings=None)
        except Exception:  # noqa: BLE001 - 推导失败退回法术命中
            logger.debug("dnd5e-vtt: 玩家攻击行推导失败", exc_info=True)
            holder = {}
        bonuses = [
            _as_int(row.get("attack_bonus"), 0)
            for row in holder.get("attacks") or []
            if isinstance(row, dict)
        ]
        if bonuses:
            return max(bonuses), QUICK_ATTACK_WEAPON
    spell = _optional_int(card.get("spellAttackBonus"))
    if spell is not None:
        return spell, QUICK_ATTACK_SPELL
    return None, ""


def _player_quick_stats(
    member: dict[str, Any],
    plugin: Any,
    modules: dict[str, Any] | None,
) -> dict[str, Any]:
    """玩家角色的速览数据；没有角色卡时给全 null + 空资源池（不报错）。"""
    card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    if not card or not _card_text(card, "name"):
        return {
            "ac": None,
            "attack_bonus": None,
            "attack_bonus_source": "",
            "spell_save_dc": None,
            "passive_perception": None,
            "hp": _quick_hp({}, state),
            "resources": [],
        }
    derived = _derived_card(card, plugin)
    attack_bonus, attack_source = _quick_attack(derived, plugin)
    return {
        "ac": _optional_int(derived.get("armorClass")),
        "attack_bonus": attack_bonus,
        "attack_bonus_source": attack_source,
        "spell_save_dc": _optional_int(derived.get("spellSaveDC")),
        "passive_perception": _optional_int(derived.get("passivePerception")),
        "hp": _quick_hp(card, state),
        "resources": _quick_resources(card, state, modules),
    }


def _npc_passive_perception(module: dict[str, Any]) -> int | None:
    """NPC 被动察觉：世界包导入的 ``senses.passive_perception``（导入时算好）。"""
    sources: list[dict[str, Any]] = []
    for key in ("statblock", "battle"):
        value = module.get(key)
        if isinstance(value, dict):
            sources.append(value)
    sources.append(module)
    for source in sources:
        value = _optional_int(source.get("passive_perception"))
        if value is not None:
            return value
        senses = source.get("senses")
        if isinstance(senses, dict):
            value = _optional_int(senses.get("passive_perception"))
            if value is not None:
                return value
    return None


def _npc_quick_stats(module: dict[str, Any]) -> dict[str, Any] | None:
    """NPC/怪物的速览数据：数值全部来自剧本模块 statblock 的现成转换。

    ``combatant_from_module`` 是 c9e1358 那刀的产物：数据包怪物数值为基线、
    模块字段覆盖。它拿不到完整数值时会显式抛错——这里如实降级成「没有
    速览」（条目上不出现 ``quick_stats`` 键），不塞占位数字。
    """
    modules = _dnd5e_modules()
    if modules is None:
        return None
    try:
        spec = modules["statblock"].combatant_from_module(module)
    except Exception:  # noqa: BLE001 - 模块缺数值：不给速览
        logger.debug("dnd5e-vtt: NPC statblock 转换失败", exc_info=True)
        return None
    if not isinstance(spec, dict):
        return None
    attacks = [row for row in spec.get("attacks") or [] if isinstance(row, dict)]
    attack_bonus: int | None = None
    if attacks:
        attack_bonus = max(_as_int(row.get("attack_bonus"), 0) for row in attacks)
    spellcasting = spec.get("spellcasting") if isinstance(spec.get("spellcasting"), dict) else {}
    spell_save_dc = _optional_int(spellcasting.get("spellSaveDC"))
    if spell_save_dc == 0:
        spell_save_dc = None
    resources: list[dict[str, Any]] = []
    total = 0
    used = 0
    for row in spec.get("spell_slots") or []:
        if not isinstance(row, dict):
            continue
        total += max(0, _as_int(row.get("max"), 0))
        used += max(0, _as_int(row.get("used"), 0))
    if total > 0:
        resources.append(
            {
                "id": "spell_slots",
                "label": "法术位",
                "value": max(0, total - min(total, used)),
                "max": total,
                "reset": QUICK_RESET_LONG_REST,
            }
        )
    return {
        "ac": _optional_int(spec.get("armorClass")),
        "attack_bonus": attack_bonus,
        "attack_bonus_source": QUICK_ATTACK_WEAPON if attack_bonus is not None else "",
        "spell_save_dc": spell_save_dc,
        "passive_perception": _npc_passive_perception(module),
        "hp": {
            "value": max(0, _as_int(spec.get("hp"), 0)),
            "max": max(0, _as_int(spec.get("max_hp"), 0)),
            "temp": max(0, _as_int(spec.get("temp_hp"), 0)),
        },
        "resources": resources[:QUICK_RESOURCE_LIMIT],
    }


def _player_roster(
    info: dict[str, Any],
    plugin: Any = None,
    modules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """玩家角色目录：成员卡是唯一数据源；未建卡的成员如实标记。"""
    roster: list[dict[str, Any]] = []
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if member.get("is_active", True) is False or member.get("status") == "removed":
            continue
        card = member.get("character_card") if isinstance(member.get("character_card"), dict) else {}
        name = _card_text(card, "name") or _card_text(member, "username")
        if not name:
            continue
        roster.append(
            {
                "user_id": str(member.get("user_id") or ""),
                "username": _card_text(member, "username"),
                "name": name,
                "avatar": _card_text(card, "avatar", limit=400),
                "level": _card_text(card, "level", limit=4),
                "class": _card_text(card, "class", "character_class"),
                "race": _card_text(card, "race"),
                "online": member.get("is_online") is True,
                "has_card": bool(_card_text(card, "name")),
                # 速览四盒 + HP + 资源池；派生值口径见 _player_quick_stats。
                "quick_stats": _player_quick_stats(member, plugin, modules),
            }
        )
        if len(roster) >= MAX_PLAYERS:
            break
    return roster


def _npc_roster(info: dict[str, Any]) -> dict[str, Any]:
    """NPC/怪物分组（仅 DM 下发）：来自房间绑定剧本的 NPC 模块。

    世界包导入（fvtt-world-import actors/run）的产物就是 ``module_type=npc``
    的剧本模块；没有绑定剧本或剧本为空时如实给空态，不造假数据。
    每个条目额外带 ``quick_stats``（AC / 命中 / 法术 DC / 被动察觉 + HP），
    数值来自 statblock 模块；模块数值不全时该键不出现（不塞占位值）。
    """
    scenario_id = str(info.get("scenario_id") or "").strip()
    if not scenario_id:
        return {"items": [], "hint": "房间未绑定剧本；导入世界包或绑定剧本后，NPC 与怪物会列在这里。"}
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR") or "")
    scenario: dict[str, Any] | None = None
    try:
        from trpg_server.scenario_store import load_scenario_by_id

        _descriptor, scenario = load_scenario_by_id(
            scenarios_dir,
            scenario_id,
            scenario_version=info.get("scenario_version"),
        )
    except Exception:  # noqa: BLE001 - 剧本读取失败按空目录降级，不阻断面板
        logger.debug("dnd5e-vtt: 读取剧本 %s 失败", scenario_id, exc_info=True)
        scenario = None
    if not isinstance(scenario, dict):
        return {"items": [], "hint": "剧本不可读，NPC 目录暂为空。"}
    items: list[dict[str, Any]] = []
    for module in scenario.get("modules") or []:
        if not isinstance(module, dict):
            continue
        module_type = str(module.get("module_type") or module.get("type") or "").strip().lower()
        if module_type != "npc":
            continue
        title = _card_text(module, "title", "name")
        if not title:
            continue
        item = {
            "id": str(module.get("id") or ""),
            "title": title,
            "summary": _card_text(module, "summary", limit=200),
        }
        quick = _npc_quick_stats(module)
        if quick is not None:
            item["quick_stats"] = quick
        items.append(item)
        if len(items) >= MAX_NPCS:
            break
    return {
        "items": items,
        "hint": "" if items else "剧本暂无 NPC 模块；导入 FVTT 世界包后，怪物与 NPC 会列在这里。",
    }


def _journal_entries(state: dict[str, Any], *, manager: bool) -> list[dict[str, Any]]:
    """日志事件流：房间事件账本的尾部；玩家侧按公开类型过滤（过滤版日志）。"""
    entries: list[dict[str, Any]] = []
    for item in state.get("event_log") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "event")
        if not manager and kind not in PLAYER_SAFE_EVENT_KINDS:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        entries.append(
            {
                "id": str(item.get("id") or ""),
                "kind": kind,
                "content": content[:400],
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return entries[-MAX_JOURNAL_ENTRIES:]


def _panels_payload(room_dir: Path | None, info: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    state = load_room_state(Path(room_dir)) if room_dir else {}
    room_id = str(info.get("id") or "")
    panels: list[dict[str, Any]] = [
        dict(PANEL_CHAT),
        {
            "id": "combat",
            "title": "战斗",
            "state": _combat_view(state),
            # 接线点：完整战斗状态与写操作沿用核心接口，前端按需拉取。
            "source": f"/api/rooms/{room_id}/combat",
        },
    ]
    if manager:
        # 场景面板只对 DM 下发；玩家响应里整个 tab 不出现（服务端裁剪）。
        panels.append(
            {
                "id": "scenes",
                "title": "场景",
                "items": [],
                # 接线点：场景列表来自 veil-map 场景存储（M3 接入），本刀占位。
                "hint": "场景列表将在地图插件数据接入后列出，用于切换场景与把玩家拉入场景。",
            }
        )
    # 角色速览用的派生值 / 资源池口径全部由 dnd5e 插件提供（本插件只搬数据）。
    plugin = _dnd5e_plugin()
    modules = _dnd5e_modules()
    characters: dict[str, Any] = {
        "id": "characters",
        "title": "角色",
        "players": _player_roster(info, plugin, modules),
        # 四盒字段说明：前端 tooltip 与文档同源（字段名 = 语义）。
        "quick_stats_fields": {
            "ac": "护甲等级（派生 armorClass）",
            "attack_bonus": "命中加值：d20 + 此值做攻击检定",
            "spell_save_dc": "法术豁免 DC（派生 spellSaveDC）",
            "passive_perception": "被动感知（派生 passivePerception）",
            "hp": "当前生命值 / 上限（+临时值）",
            "resources": "长休刷新的有限资源池：生命骰剩余、法术位合计",
        },
    }
    if manager:
        # NPC/怪物分组仅 DM 视图；玩家响应里没有 npc 键。
        characters["npc"] = _npc_roster(info)
    panels.append(characters)
    panels.append(dict(PANEL_ITEMS))
    panels.append(
        {
            "id": "journal",
            "title": "日志",
            "entries": _journal_entries(state, manager=manager),
        }
    )
    return {
        "room_id": room_id,
        "ruleset_id": "dnd5e",
        "observer": "dm" if manager else "player",
        "panels": panels,
    }


@bp.route("/panels", methods=["GET"])
def panels():
    denied = _login_required()
    if denied:
        return denied
    room_id = str(request.args.get("room") or "").strip()
    if not room_id:
        return error_response("缺少 room 参数。", 400, "dnd5e_vtt.room_required")
    room_dir, info = find_room(room_id)
    if not info:
        return error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return error_response("你不在这个房间里。", 403, "room.access_denied")
    if _ruleset_id(info) not in SUPPORTED_RULESETS:
        return error_response("当前房间的规则集未启用战桌。", 400, "dnd5e_vtt.ruleset_disabled")
    manager = _is_manager(info)
    return success_response(_panels_payload(room_dir, info, manager=manager), "D&D 5e 战桌面板")


# ============================================================
# 测量模板（圆/锥/线/方）：房间级登记、DM 写入门控、request_id 幂等
# 与 revision 乐观并发；接口风格与回执结构学 veil-map 的写协议。
# ============================================================


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _templates_path(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "dnd5e-vtt" / "templates.json"


def _default_templates() -> dict[str, Any]:
    return {
        "version": TEMPLATES_VERSION,
        "revision": 0,
        "templates": [],
        "recent_requests": [],
        "updated_at": "",
    }


def _load_templates(room_dir) -> dict[str, Any]:
    store = read_json(_templates_path(room_dir), default=None)
    if not isinstance(store, dict):
        return _default_templates()
    store.setdefault("version", TEMPLATES_VERSION)
    store.setdefault("revision", 0)
    if not isinstance(store.get("templates"), list):
        store["templates"] = []
    store.setdefault("recent_requests", [])
    return store


def _normalize_direction(raw: Any) -> tuple[float, float] | None:
    """方向向量归一化（数据校验，不是空间计算；取格判定仍走 veil-map）。"""
    if isinstance(raw, dict):
        dx, dy = raw.get("dx"), raw.get("dy")
    elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
        dx, dy = raw[0], raw[1]
    else:
        return None
    try:
        fx, fy = float(dx), float(dy)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(fx) and math.isfinite(fy)) or (fx == 0 and fy == 0):
        return None
    length = math.hypot(fx, fy)
    return round(fx / length, 6), round(fy / length, 6)


def _normalize_template(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """模板字段规范化；返回 ``(template, error)``，出错时 template 为 ``None``。"""
    if not isinstance(raw, dict):
        return None, "template 必须是 JSON 对象。"
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in TEMPLATE_KINDS:
        return None, "模板类型只能是 circle / cone / line / rect。"
    try:
        cell_x = int(round(float(raw.get("cell_x"))))
        cell_y = int(round(float(raw.get("cell_y"))))
    except (TypeError, ValueError):
        return None, "模板原点需要整数 cell_x / cell_y。"
    if abs(cell_x) > 1_000_000 or abs(cell_y) > 1_000_000:
        return None, "模板原点超出可表示范围。"
    template: dict[str, Any] = {"kind": kind, "cell_x": cell_x, "cell_y": cell_y}
    if kind == "rect":
        try:
            width = int(round(float(raw.get("w_cells"))))
            height = int(round(float(raw.get("h_cells"))))
        except (TypeError, ValueError):
            return None, "方形模板需要整数 w_cells / h_cells。"
        if not (MIN_TEMPLATE_CELLS <= width <= MAX_TEMPLATE_CELLS):
            return None, "方形宽度必须在 1–100 格之间。"
        if not (MIN_TEMPLATE_CELLS <= height <= MAX_TEMPLATE_CELLS):
            return None, "方形高度必须在 1–100 格之间。"
        template["w_cells"] = width
        template["h_cells"] = height
    else:
        try:
            size = int(round(float(raw.get("size_cells"))))
        except (TypeError, ValueError):
            return None, "模板尺寸需要整数 size_cells。"
        if not (MIN_TEMPLATE_CELLS <= size <= MAX_TEMPLATE_CELLS):
            return None, "模板尺寸必须在 1–100 格之间（每格 5 尺）。"
        template["size_cells"] = size
    if kind in {"cone", "line"}:
        unit = _normalize_direction(raw.get("direction"))
        if unit is None:
            return None, "锥形/线形模板需要非零 direction {dx, dy}。"
        template["direction"] = {"dx": unit[0], "dy": unit[1]}
    if kind == "cone":
        try:
            angle = float(raw.get("angle") or DEFAULT_CONE_ANGLE)
        except (TypeError, ValueError):
            angle = DEFAULT_CONE_ANGLE
        if not math.isfinite(angle):
            angle = DEFAULT_CONE_ANGLE
        template["angle"] = round(min(MAX_CONE_ANGLE, max(MIN_CONE_ANGLE, angle)), 2)
    color = str(raw.get("color") or "").strip()
    if not _TEMPLATE_HEX_COLOR.match(color):
        color = TEMPLATE_COLORS[kind]
    template["color"] = color
    label = str(raw.get("label") or "").strip()
    if label:
        template["label"] = label[:MAX_TEMPLATE_LABEL]
    return template, ""


def _templates_payload(store: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    return {
        "templates": store.get("templates") or [],
        "revision": int(store.get("revision") or 0),
        "can_manage": manager,
        "observer": "dm" if manager else "player",
    }


def _template_receipt(
    status: str,
    message: str,
    *,
    code: str = "",
    request_id: str = "",
    revision: int = 0,
    duplicate: bool = False,
    data: dict[str, Any] | None = None,
    http_status: int = 200,
):
    """四态回执（success / conflict / rejected / invalid），学 veil-map。"""
    payload: dict[str, Any] = {
        "success": status == "success",
        "status": status,
        "code": code,
        "message": message,
        "request_id": str(request_id or ""),
        "revision": int(revision),
        "duplicate": bool(duplicate),
    }
    if data is not None:
        payload["data"] = data
    return jsonify(payload), http_status


def _broadcast_templates(room_id: str, store: dict[str, Any]) -> None:
    """模板广播到房间：模板是 DM 放置的公共信息，全员可见、无观察者分叉。"""
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit(
            "dnd5e_vtt_templates",
            {
                "room_id": str(room_id),
                "templates": store.get("templates") or [],
                "revision": int(store.get("revision") or 0),
            },
            room=str(room_id),
        )
    except Exception:  # noqa: BLE001 - 广播失败不能影响写入结果
        logger.warning("dnd5e_vtt_templates_broadcast_failed room=%s", room_id, exc_info=True)


def _templates_room_or_error(room_id: str):
    """模板与棋子卡接口统一入口：登录、房间、成员与规则集门闸（与 /panels 同口径）。"""
    denied = _login_required()
    if denied:
        return None, None, denied
    room_dir, info = find_room(room_id)
    if not info:
        return room_dir, None, error_response("房间不存在。", 404, "room.not_found")
    if not can_access(info, session.get("user_id"), session.get("role", "USER")):
        return room_dir, info, error_response("你不在这个房间里。", 403, "room.access_denied")
    if _ruleset_id(info) not in SUPPORTED_RULESETS:
        return room_dir, info, error_response(
            "当前房间的规则集未启用战桌。", 400, "dnd5e_vtt.ruleset_disabled"
        )
    return room_dir, info, None


@bp.route("/rooms/<room_id>/templates", methods=["GET"])
def get_templates(room_id: str):
    """模板读取：全员可读（玩家端只读展示，编辑工具不在玩家视图渲染）。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    with room_lock(room_dir):
        store = _load_templates(room_dir)
    manager = _is_manager(info)
    return success_response(_templates_payload(store, manager=manager), "测量模板清单")


@bp.route("/rooms/<room_id>/templates", methods=["POST"])
def write_templates(room_id: str):
    """模板写入口：place / move / delete / clear，仅 DM；request_id 幂等。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    manager = _is_manager(info)
    if not manager:
        return _template_receipt(
            "rejected",
            "测量模板只能由 DM（房主/房管）放置与修改。",
            code="dnd5e_vtt.templates_dm_only",
            http_status=403,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _template_receipt(
            "invalid", "请求体必须是 JSON 对象。", code="dnd5e_vtt.invalid_payload"
        )
    request_id = str(payload.get("request_id") or "").strip()
    if not _TEMPLATE_REQUEST_ID.match(request_id):
        return _template_receipt(
            "invalid",
            "写请求缺少合法的 request_id（1–64 位字母、数字或 . _ : -）。",
            code="dnd5e_vtt.invalid_request_id",
            http_status=400,
        )
    raw_revision = payload.get("revision")
    if isinstance(raw_revision, bool):
        raw_revision = None
    try:
        expected = int(str(raw_revision).strip())
    except (TypeError, ValueError):
        expected = -1
    if expected < 0:
        return _template_receipt(
            "invalid",
            "写请求缺少非负整数 revision。",
            code="dnd5e_vtt.invalid_revision",
            request_id=request_id,
            http_status=400,
        )
    action = str(payload.get("action") or "").strip().lower()
    if action not in TEMPLATE_ACTIONS:
        return _template_receipt(
            "invalid",
            "action 只能是 place / move / delete / clear。",
            code="dnd5e_vtt.invalid_action",
            request_id=request_id,
            http_status=400,
        )
    with room_lock(room_dir):
        store = _load_templates(room_dir)
        revision = int(store.get("revision") or 0)
        if expected != revision:
            return _template_receipt(
                "conflict",
                f"模板已被其他端更新（当前版本 {revision}），请刷新后再试。",
                code="dnd5e_vtt.revision_conflict",
                request_id=request_id,
                revision=revision,
                http_status=409,
                data=_templates_payload(store, manager=manager),
            )
        for record in store.get("recent_requests") or []:
            if str(record.get("request_id")) == request_id:
                # 幂等：同一 request_id 直接回成功，不再产生副作用。
                return _template_receipt(
                    "success",
                    "该操作已生效，重复提交已忽略。",
                    code="dnd5e_vtt.duplicate_request",
                    request_id=request_id,
                    revision=revision,
                    duplicate=True,
                    data=_templates_payload(store, manager=manager),
                )
        templates: list[dict[str, Any]] = [
            item for item in store.get("templates") or [] if isinstance(item, dict)
        ]
        if action == "place":
            template, error = _normalize_template(payload.get("template"))
            if error:
                return _template_receipt(
                    "invalid",
                    error,
                    code="dnd5e_vtt.invalid_template",
                    request_id=request_id,
                    revision=revision,
                    http_status=400,
                )
            if len(templates) >= MAX_TEMPLATES:
                return _template_receipt(
                    "rejected",
                    f"模板数量已达上限（{MAX_TEMPLATES}），请先清理。",
                    code="dnd5e_vtt.too_many_templates",
                    request_id=request_id,
                    revision=revision,
                    http_status=409,
                )
            template_id = str(payload.get("id") or "").strip()[:64]
            if template_id and any(str(item.get("id")) == template_id for item in templates):
                return _template_receipt(
                    "invalid",
                    "模板 id 已存在。",
                    code="dnd5e_vtt.duplicate_template_id",
                    request_id=request_id,
                    revision=revision,
                    http_status=409,
                )
            template["id"] = template_id or f"tpl-{uuid4().hex[:12]}"
            template["placed_by"] = str(session.get("user_id") or "")
            template["created_at"] = _utc_now()
            templates.append(template)
        elif action == "move":
            template_id = str(payload.get("id") or "").strip()
            target = next(
                (item for item in templates if str(item.get("id")) == template_id), None
            )
            if target is None:
                return _template_receipt(
                    "invalid",
                    "模板不存在。",
                    code="dnd5e_vtt.template_not_found",
                    request_id=request_id,
                    revision=revision,
                    http_status=404,
                )
            incoming, error = _normalize_template(payload.get("template") or target)
            if error:
                return _template_receipt(
                    "invalid",
                    error,
                    code="dnd5e_vtt.invalid_template",
                    request_id=request_id,
                    revision=revision,
                    http_status=400,
                )
            moved = dict(target)
            moved.update(incoming)
            moved["id"] = str(target.get("id"))
            moved["updated_at"] = _utc_now()
            templates[templates.index(target)] = moved
        else:  # delete / clear
            if action == "clear":
                templates = []
            else:
                template_id = str(payload.get("id") or "").strip()
                if not any(str(item.get("id")) == template_id for item in templates):
                    return _template_receipt(
                        "invalid",
                        "模板不存在。",
                        code="dnd5e_vtt.template_not_found",
                        request_id=request_id,
                        revision=revision,
                        http_status=404,
                    )
                templates = [
                    item for item in templates if str(item.get("id")) != template_id
                ]
        store["templates"] = templates
        store["revision"] = revision + 1
        store["updated_at"] = _utc_now()
        records = [
            item for item in store.get("recent_requests") or [] if isinstance(item, dict)
        ]
        records.append(
            {
                "request_id": request_id,
                "action": action,
                "revision": store["revision"],
                "at": store["updated_at"],
            }
        )
        store["recent_requests"] = records[-TEMPLATE_RECENT_LIMIT:]
        write_json_atomic(_templates_path(room_dir), store)
    _broadcast_templates(room_id, store)
    return _template_receipt(
        "success",
        "模板已更新。",
        code=f"dnd5e_vtt.templates_{action}",
        request_id=request_id,
        revision=store["revision"],
        data=_templates_payload(store, manager=manager),
    )


# ============================================================
# 棋子操作卡：DM 拉任意棋子的动作清单；玩家只读自己的卡且无 actions 键。
# ============================================================


def _trim_card_text(value: Any, limit: int = MAX_CARD_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _combatant_candidates(params: dict[str, Any]) -> list[str]:
    """棋子 → 战斗者/NPC 模块的候选名（保序去重；``npc:`` 前缀剥掉后并保留）。"""
    raw = [params.get("combatant_name"), params.get("combatant"), params.get("name")]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        text = _trim_card_text(item, 96)
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
        if text.lower().startswith("npc:"):
            stripped = text[4:].strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                ordered.append(stripped)
    return ordered


def _find_combatant(state: dict[str, Any], candidates: list[str]) -> dict[str, Any] | None:
    combat = state.get("combat") if isinstance(state.get("combat"), dict) else None
    if not combat or not combat.get("active"):
        return None
    combatants = combat.get("combatants")
    if not isinstance(combatants, dict):
        return None
    for candidate in candidates:
        row = combatants.get(candidate)
        if isinstance(row, dict):
            return row
        lowered = str(candidate).lower()
        for name, item in combatants.items():
            if str(name).lower() == lowered and isinstance(item, dict):
                return item
    return None


def _find_npc_module(info: dict[str, Any], candidates: list[str]) -> dict[str, Any] | None:
    """按候选名匹配剧本 NPC 模块（世界包导入产物，含结构化 statblock）。"""
    scenario_id = str(info.get("scenario_id") or "").strip()
    if not scenario_id or not candidates:
        return None
    scenarios_dir = Path(current_app.config.get("SCENARIOS_DIR") or "")
    try:
        from trpg_server.scenario_store import load_scenario_by_id

        _descriptor, scenario = load_scenario_by_id(
            scenarios_dir,
            scenario_id,
            scenario_version=info.get("scenario_version"),
        )
    except Exception:  # noqa: BLE001 - 剧本读取失败按无模块降级
        logger.debug("dnd5e-vtt: token-actions 读取剧本 %s 失败", scenario_id, exc_info=True)
        return None
    if not isinstance(scenario, dict):
        return None
    lowered = [str(item).lower() for item in candidates]
    for module in scenario.get("modules") or []:
        if not isinstance(module, dict):
            continue
        if str(module.get("module_type") or module.get("type") or "").strip().lower() != "npc":
            continue
        title = str(module.get("title") or module.get("name") or "").strip().lower()
        if title and title in lowered:
            return module
    return None


def _find_member(info: dict[str, Any], owner_user_id: Any) -> dict[str, Any] | None:
    owner = str(owner_user_id or "").strip()
    if not owner:
        return None
    for member in info.get("members") or []:
        if not isinstance(member, dict):
            continue
        if str(member.get("user_id")) == owner:
            return member
    return None


def _attack_rows(source: dict[str, Any]) -> list[dict[str, Any]]:
    """攻击行：命中 + 伤害文本（本刀只展示，发起结算归后续刀接 combat 工作流）。"""
    rows: list[dict[str, Any]] = []
    for row in source.get("attacks") or []:
        if not isinstance(row, dict):
            continue
        name = _trim_card_text(row.get("name"), 60)
        if not name:
            continue
        try:
            bonus = int(round(float(row.get("attack_bonus") or 0)))
        except (TypeError, ValueError):
            bonus = 0
        try:
            damage_bonus = int(round(float(row.get("damage_bonus") or 0)))
        except (TypeError, ValueError):
            damage_bonus = 0
        dice = _trim_card_text(row.get("damage_dice"), 32)
        damage_text = dice
        if dice and damage_bonus:
            damage_text = f"{dice}{damage_bonus:+d}"
        damage_type = _trim_card_text(row.get("damage_type"), 24)
        if damage_text and damage_type:
            damage_text += f" {damage_type}"
        detail = ""
        ranged = row.get("range")
        reach = row.get("reach_ft")
        if isinstance(ranged, dict):
            detail = f"射程 {ranged.get('normal_ft') or 0}/{ranged.get('long_ft') or 0} 尺"
        elif reach:
            detail = f"触及 {reach} 尺"
        versatile = _trim_card_text(row.get("versatile_dice"), 32)
        if versatile:
            detail = (detail + " · " if detail else "") + f"双手 {versatile}"
        rows.append(
            {
                "name": name,
                "kind": _trim_card_text(row.get("kind"), 12) or "melee",
                "hit": f"{bonus:+d}" if bonus else "",
                "damage": damage_text,
                "detail": detail,
            }
        )
    return rows


def _spell_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    names: list[str] = []
    for key in ("spells_known", "spells_prepared"):
        for item in source.get(key) or []:
            text = _trim_card_text(item, 80)
            if text and text not in names:
                names.append(text)
    spellcasting = source.get("spellcasting")
    detail = ""
    if isinstance(spellcasting, dict):
        parts = []
        save_dc = spellcasting.get("spellSaveDC") or spellcasting.get("save_dc")
        if save_dc:
            parts.append(f"豁免 DC {save_dc}")
        attack_bonus = spellcasting.get("spellAttackBonus") or spellcasting.get("attack_bonus")
        if attack_bonus:
            parts.append(f"法术命中 {attack_bonus}")
        detail = " · ".join(parts)
    return [{"name": name, "detail": detail} for name in names]


def _ability_rows(source: dict[str, Any], *, npc: bool) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if npc:
        senses = source.get("senses") if isinstance(source.get("senses"), dict) else {}
        for key, value in senses.items():
            name = _trim_card_text(key, 40)
            if name:
                rows.append({"name": name, "detail": _trim_card_text(value, 60)})
        legendary = source.get("legendary_actions_max")
        if legendary:
            rows.append({"name": "传奇动作", "detail": f"每轮最多 {legendary} 次"})
        resistances = source.get("legendary_resistances_max")
        if resistances:
            rows.append({"name": "传奇抗性", "detail": f"每日 {resistances} 次"})
        return rows
    proficiencies = (
        source.get("proficiencies") if isinstance(source.get("proficiencies"), dict) else {}
    )
    labels = {
        "weapons": "武器熟练",
        "armor": "护甲熟练",
        "tools": "工具熟练",
        "languages": "语言",
    }
    for key, label in labels.items():
        items = [
            str(item).strip() for item in proficiencies.get(key) or [] if str(item or "").strip()
        ]
        if items:
            rows.append({"name": label, "detail": "、".join(items)[:MAX_CARD_TEXT]})
    return rows


def _item_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in source.get("equipment") or []:
        if isinstance(row, dict):
            name = _trim_card_text(row.get("name"), 60)
            if not name:
                continue
            quantity = row.get("quantity")
            detail = f"数量 {quantity}" if quantity not in (None, "") else ""
            rows.append({"name": name, "detail": _trim_card_text(detail, 24)})
        else:
            name = _trim_card_text(row, 60)
            if name:
                rows.append({"name": name, "detail": ""})
    return rows


def _feature_rows(source: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in source.get("features") or []:
        name = _trim_card_text(item, 80)
        if name:
            rows.append({"name": name, "detail": ""})
    return rows


def _card_hp(source: dict[str, Any]) -> tuple[int, int]:
    """HP 双源：资源字段 ``hp{current,max}`` 优先，退回扁平键。"""
    hp_field = source.get("hp")
    if isinstance(hp_field, dict):
        try:
            current = int(round(float(hp_field.get("current") or hp_field.get("current_hp") or 0)))
        except (TypeError, ValueError):
            current = 0
        try:
            maximum = int(
                round(
                    float(
                        hp_field.get("max")
                        or hp_field.get("max_hp")
                        or source.get("maxHp")
                        or 0
                    )
                )
            )
        except (TypeError, ValueError):
            maximum = 0
        if maximum > 0:
            return max(0, min(maximum, current)), maximum
    try:
        current = int(round(float(source.get("hp") or 0)))
    except (TypeError, ValueError):
        current = 0
    try:
        maximum = int(round(float(source.get("max_hp") or source.get("maxHp") or 0)))
    except (TypeError, ValueError):
        maximum = 0
    return max(0, current), max(0, maximum)


def _token_actions_payload(
    room_dir,
    info: dict[str, Any],
    *,
    manager: bool,
    params: dict[str, Any],
) -> dict[str, Any]:
    """棋子操作卡数据装配。

    匹配顺序：房间战斗状态（含已推导的攻击加值/伤害）→ 成员角色卡 →
    剧本 NPC 模块 statblock。DM 得到完整动作清单；**玩家响应不含
    actions 键**（服务端裁剪，DOM 不渲染，拍板决策）。
    """
    state = load_room_state(Path(room_dir)) if room_dir else {}
    candidates = _combatant_candidates(params)
    combatant = _find_combatant(state, candidates)
    member = _find_member(info, params.get("owner"))
    module = _find_npc_module(info, candidates)
    member_card = member.get("character_card") if isinstance(member, dict) else None
    member_card = member_card if isinstance(member_card, dict) else {}
    statblock = module.get("statblock") if isinstance(module, dict) else None
    statblock = statblock if isinstance(statblock, dict) else {}
    kind = "token"
    card_source: dict[str, Any] = {}
    if combatant:
        card_source = combatant
        kind = "npc" if str(params.get("combatant") or "").lower().startswith("npc:") else "combatant"
    if member is not None:
        card_source = member_card or card_source
        kind = "player"
    elif module is not None and not combatant:
        # 战斗状态优先于剧本 statblock：前者有实时 HP，后者只有满血模板。
        card_source = statblock or card_source
        if kind == "token":
            kind = "npc"
    hp, max_hp = _card_hp(card_source)
    try:
        ac = int(round(float(card_source.get("armorClass") or card_source.get("ac") or 0)))
    except (TypeError, ValueError):
        ac = 0
    name = (
        _trim_card_text(params.get("name"), 60)
        or _trim_card_text(member_card.get("name"), 60)
        or _trim_card_text((module or {}).get("title"), 60)
        or _trim_card_text(card_source.get("name"), 60)
        or "棋子"
    )
    card: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "hp": hp,
        "max_hp": max_hp,
        "ac": ac,
    }
    if kind == "player":
        card["avatar"] = _trim_card_text(member_card.get("avatar"), 400)
        card["level"] = _trim_card_text(member_card.get("level"), 4)
        card["class"] = _trim_card_text(member_card.get("class"), 40)
        card["race"] = _trim_card_text(member_card.get("race"), 40)
    elif kind == "npc":
        card["avatar"] = ""
        card["cr"] = _trim_card_text(card_source.get("cr"), 12)
    data: dict[str, Any] = {
        "card": card,
        "matched": bool(combatant or member is not None or module is not None),
    }
    if not manager:
        return data
    # DM 动作清单：战斗状态优先，NPC statblock 次之，成员角色卡最后。
    actions_source = combatant or card_source or {}
    actions: dict[str, Any] = {
        "attacks": _attack_rows(actions_source),
        "spells": _spell_rows(actions_source),
        "features": _feature_rows(actions_source if kind == "player" else {}),
        "abilities": _ability_rows(actions_source, npc=kind != "player"),
        "items": _item_rows(actions_source),
    }
    if not (combatant or member is not None or module is not None):
        actions["hint"] = "未找到该棋子对应的战斗者、剧本 NPC 或角色卡；开战后动作清单会从战斗状态生成。"
    data["actions"] = actions
    return data


@bp.route("/rooms/<room_id>/token-actions", methods=["GET"])
def token_actions(room_id: str):
    """棋子操作卡数据：DM 全量；玩家仅自己棋子且不含操作按钮。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    params = {
        "name": request.args.get("name") or "",
        "owner": request.args.get("owner") or "",
        "combatant": request.args.get("combatant") or "",
        "combatant_name": request.args.get("combatant_name") or "",
    }
    if not any(str(value).strip() for value in params.values()):
        return error_response(
            "缺少棋子标识（name / owner / combatant / combatant_name 至少一项）。",
            400,
            "dnd5e_vtt.token_required",
        )
    manager = _is_manager(info)
    viewer_id = str(session.get("user_id") or "")
    if not manager and str(params["owner"]).strip() != viewer_id:
        return error_response(
            "玩家只能查看自己角色的棋子卡。",
            403,
            "dnd5e_vtt.token_actions_forbidden",
        )
    payload = _token_actions_payload(room_dir, info, manager=manager, params=params)
    return success_response(payload, "棋子操作卡数据")


# ============================================================
# 骰桌动画设置（M4）：DM 节流开关（enabled）+ 单次动画骰数上限
# （max_dice，clamp 1–20）。GET 全员可读（玩家端据此决定是否播放
# 地图骰子动画）；POST 仅 DM，玩家 403 rejected。写后广播
# ``dnd5e_vtt_dice_settings``，所有客户端即时应用。
# ============================================================

DICE_SETTINGS_VERSION = 1
MIN_DICE_ANIMATION = 1
MAX_DICE_ANIMATION = 20
DEFAULT_DICE_ANIMATION = 20


def _dice_settings_path(room_dir) -> Path:
    return Path(room_dir) / "plugins" / "dnd5e-vtt" / "dice-settings.json"


def _default_dice_settings() -> dict[str, Any]:
    return {
        "version": DICE_SETTINGS_VERSION,
        "enabled": True,
        "max_dice": DEFAULT_DICE_ANIMATION,
        "updated_at": "",
    }


def _normalize_dice_settings(store: Any) -> dict[str, Any]:
    """读盘归一：字段缺失或越界都折回默认值，前端拿到的永远是合法设置。"""
    if not isinstance(store, dict):
        return _default_dice_settings()
    enabled = store.get("enabled")
    try:
        max_dice = int(store.get("max_dice"))
    except (TypeError, ValueError):
        max_dice = DEFAULT_DICE_ANIMATION
    return {
        "version": DICE_SETTINGS_VERSION,
        "enabled": True if enabled is None else bool(enabled),
        "max_dice": min(MAX_DICE_ANIMATION, max(MIN_DICE_ANIMATION, max_dice)),
        "updated_at": str(store.get("updated_at") or ""),
    }


def _load_dice_settings(room_dir) -> dict[str, Any]:
    return _normalize_dice_settings(read_json(_dice_settings_path(room_dir), default=None))


def _dice_settings_payload(settings: dict[str, Any], *, manager: bool) -> dict[str, Any]:
    return {
        "enabled": bool(settings["enabled"]),
        "max_dice": int(settings["max_dice"]),
        "can_manage": manager,
        "observer": "dm" if manager else "player",
    }


def _dice_receipt(status: str, message: str, *, code: str = "", data: dict[str, Any] | None = None, http_status: int = 200):
    """四态回执（success / rejected / invalid），照模板端点的回执结构。"""
    payload: dict[str, Any] = {
        "success": status == "success",
        "status": status,
        "code": code,
        "message": message,
    }
    if data is not None:
        payload["data"] = data
    return jsonify(payload), http_status


def _broadcast_dice_settings(room_id: str, settings: dict[str, Any]) -> None:
    """设置广播到房间：开关与上限对全员生效，所有客户端立即应用。"""
    socketio = current_app.extensions.get("socketio")
    if socketio is None:
        return
    try:
        socketio.emit(
            "dnd5e_vtt_dice_settings",
            {
                "room_id": str(room_id),
                "enabled": bool(settings["enabled"]),
                "max_dice": int(settings["max_dice"]),
            },
            room=str(room_id),
        )
    except Exception:  # noqa: BLE001 - 广播失败不能影响写入结果
        logger.warning("dnd5e_vtt_dice_settings_broadcast_failed room=%s", room_id, exc_info=True)


@bp.route("/rooms/<room_id>/dice-settings", methods=["GET"])
def get_dice_settings(room_id: str):
    """骰桌设置读取：全员可读（玩家端据此播放或跳过地图动画）。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    with room_lock(room_dir):
        settings = _load_dice_settings(room_dir)
    manager = _is_manager(info)
    return success_response(
        _dice_settings_payload(settings, manager=manager), "骰桌动画设置"
    )


@bp.route("/rooms/<room_id>/dice-settings", methods=["POST"])
def write_dice_settings(room_id: str):
    """骰桌设置写入：仅 DM；enabled 必须是布尔，max_dice 数值 clamp 1–20。"""
    room_dir, info, denied = _templates_room_or_error(room_id)
    if denied:
        return denied
    manager = _is_manager(info)
    if not manager:
        return _dice_receipt(
            "rejected",
            "骰桌动画设置只能由 DM（房主/房管）修改。",
            code="dnd5e_vtt.dice_settings_dm_only",
            http_status=403,
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _dice_receipt(
            "invalid",
            "请求体必须是 JSON 对象。",
            code="dnd5e_vtt.invalid_payload",
            http_status=400,
        )
    has_enabled = "enabled" in payload
    has_max_dice = "max_dice" in payload
    if not (has_enabled or has_max_dice):
        return _dice_receipt(
            "invalid",
            "至少提供 enabled 或 max_dice 之一。",
            code="dnd5e_vtt.dice_settings_empty",
            http_status=400,
        )
    if has_enabled and not isinstance(payload.get("enabled"), bool):
        return _dice_receipt(
            "invalid",
            "enabled 必须是布尔值。",
            code="dnd5e_vtt.invalid_enabled",
            http_status=400,
        )
    raw_max = payload.get("max_dice") if has_max_dice else None
    if raw_max is not None and isinstance(raw_max, bool):
        return _dice_receipt(
            "invalid",
            "max_dice 必须是 1–20 的整数。",
            code="dnd5e_vtt.invalid_max_dice",
            http_status=400,
        )
    if raw_max is None:
        max_dice = None
    else:
        try:
            max_dice = int(round(float(raw_max)))
        except (TypeError, ValueError):
            return _dice_receipt(
                "invalid",
                "max_dice 必须是 1–20 的整数。",
                code="dnd5e_vtt.invalid_max_dice",
                http_status=400,
            )
        if not math.isfinite(float(raw_max)):
            return _dice_receipt(
                "invalid",
                "max_dice 必须是 1–20 的整数。",
                code="dnd5e_vtt.invalid_max_dice",
                http_status=400,
            )
        # 超限不报错：按 dice3d bundle 的骰数上限语义 clamp 到 1–20。
        max_dice = min(MAX_DICE_ANIMATION, max(MIN_DICE_ANIMATION, max_dice))
    with room_lock(room_dir):
        settings = _load_dice_settings(room_dir)
        if has_enabled:
            settings["enabled"] = bool(payload.get("enabled"))
        if max_dice is not None:
            settings["max_dice"] = max_dice
        settings["updated_at"] = _utc_now()
        write_json_atomic(_dice_settings_path(room_dir), settings)
    _broadcast_dice_settings(room_id, settings)
    return _dice_receipt(
        "success",
        "骰桌动画设置已更新。",
        code="dnd5e_vtt.dice_settings_updated",
        data=_dice_settings_payload(settings, manager=manager),
    )
