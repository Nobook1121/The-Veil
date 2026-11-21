"""D&D 5e 房规档案（house-rule profiles）：按房间剧本形态适配默认档。

M5 第二段（docs/plans/dnd5e-vtt-refactor-plan.md §8 M5+ 行）：

- 两个具名档案：``theater-of-mind``（现状兼容默认档，值 = 房规 schema 全
  默认）与 ``grid-combat``（方格战斗档：距离以格计、对角线 5-5-5、夹击
  默认关）。档案只是「键 → 值」的声明集合，核心只按房规 schema 通用
  校验存储；档案合法性（键在 schema 内、值在枚举内）由本模块与测试保证。
- 判定链：房间挂了 veil-map 活动场景（或已有配底图场景）→ 推荐
  grid-combat；否则 theater-of-mind。地图状态只走公开服务
  ``get_service("veil-map", "map_service")``（与 dnd5e-vtt 战桌工具同一
  通道）；veil-map 缺席、服务缺席或读取失败一律降级为纯文本形态，不报错。
- 推荐不等于强改：自动应用只发生在「房主从未人工设置过 dnd5e 房规」的
  窗口内——存量值仍等于 schema 默认，或仍等于上次自动应用留下的快照。
  房主一旦手动改动（现有房规接口/UI 任意键），窗口判定立即翻为 manual
  并永久关闭。记忆落在插件自有的
  ``<room>/plugins/dnd5e/house-rule-profile.json``（学 dnd5e-vtt 的
  templates.json 存储形态），不改核心房间数据结构。
- 站点级开关：``<CONFIG_DIR>/dnd5e-house-rules.json`` 的
  ``auto_profile_recommendation``（缺省视为 true，插件自有文件，核心零
  改动）。关闭后推荐端点不再给出推荐、也绝不自动应用。
- KP 提示词联动：``register_kp_context`` 把「当前房规档案/距离口径」行
  挂进 ``get_service("dnd5e", "kp_context")``，核心回合管线按 ruleset id
  探测并拼进 ruleset_fragments（dnd5e-vtt 战桌状态行同一通道）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

from dnd5e_plugin.house_rules import house_rules

logger = logging.getLogger(__name__)

PROFILE_TOM = "theater-of-mind"
PROFILE_GRID = "grid-combat"
PROFILE_CUSTOM = "custom"

RULESET_ID = "dnd5e"
MARKER_SCHEMA_VERSION = 1
SITE_CONFIG_FILENAME = "dnd5e-house-rules.json"
_SITE_CONFIG_KEY = "auto_profile_recommendation"

# 方格战斗档的距离/位置键在提示词里必须跟引擎现状一致：
# 引擎当前测距固定按 Chebyshev（斜向算 1 格）折算 5 尺/格，
# dnd5e_diagonal_rule / dnd5e_milestone_leveling 是「预留未生效」键，
# 提示词不得声称其已生效。


def _text(zh: str, en: str) -> dict[str, str]:
    return {"zh-CN": zh, "en-US": en}


# ---------------------------------------------------------------- 档案声明


def _profile_overrides() -> dict[str, dict[str, Any]]:
    """两个具名档案的差异键（其余键一律继承 schema 默认）。

    theater-of-mind 显式写出位置口径键以自文档化，值与 schema 默认一致
    （现状兼容：不动任何既有房间的默认行为）。grid-combat 的夹击按
    DMG 可选规则默认关——带地图只改变「距离怎么量」，不默认打开可选加值。
    """
    return {
        PROFILE_TOM: {
            "label_i18n": _text("剧场式（默认）", "Theater of Mind (default)"),
            "description_i18n": _text(
                "纯文本/叙事跑团口径：距离用近战、近、远档位描述，不依赖棋盘格。"
                "与现状默认房规完全一致。",
                "Narrative play: distance told in melee/near/far tiers, no grid dependency. "
                "Identical to the current default house rules.",
            ),
            "values": {
                "dnd5e_positioning": "theater_of_mind",
            },
        },
        PROFILE_GRID: {
            "label_i18n": _text("方格战斗", "Grid Combat"),
            "description_i18n": _text(
                "FVTT 世界包/带地图跑团口径：距离以格计（1 格 = 5 尺），对角线按 "
                "5-5-5（DMG 可选规则的取一档；当前引擎即按此折算）。夹击（DMG 可选）"
                "默认保持关闭，房主可在房规面板打开。",
                "Map-play conventions: distances in cells (5 ft each), diagonals 5-5-5 "
                "(one DMG optional pick; matches the current engine). Flanking (DMG "
                "optional) stays off by default; the owner may enable it.",
            ),
            "values": {
                "dnd5e_positioning": "grid",
                "dnd5e_diagonal_rule": "5-5-5",
            },
        },
    }


def profile_ids() -> tuple[str, ...]:
    return (PROFILE_TOM, PROFILE_GRID)


def schema_defaults() -> dict[str, Any]:
    return {key: spec.get("default") for key, spec in house_rules().items()}


def profile_values(profile_id: str) -> dict[str, Any]:
    """档案的完整取值：schema 全默认 + 该档案的差异键。"""
    overrides = _profile_overrides().get(str(profile_id or "")) or {}
    values = dict(schema_defaults())
    values.update(overrides.get("values") or {})
    return values


def profile_catalog() -> dict[str, dict[str, Any]]:
    """推荐端点展示用：档案名/描述 + 完整取值（房主可见将要生效什么）。"""
    catalog: dict[str, dict[str, Any]] = {}
    for profile_id, declared in _profile_overrides().items():
        catalog[profile_id] = {
            "label_i18n": declared["label_i18n"],
            "description_i18n": declared["description_i18n"],
            "values": profile_values(profile_id),
        }
    return catalog


def _normalize_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把存储里的 5e 房规值按 schema 规整成可比对形态。"""
    schema = house_rules()
    values: dict[str, Any] = {}
    for key, spec in schema.items():
        value = raw.get(key, spec.get("default"))
        if spec.get("type") == "boolean":
            values[key] = bool(value)
        else:
            candidate = str(value or "").strip().lower()
            values[key] = candidate if candidate in spec.get("values", []) else spec.get("default")
    return values


def current_values(info: Mapping[str, Any] | None) -> dict[str, Any]:
    """房间当前生效的 5e 房规值（缺键回 schema 默认，枚举小写规整）。"""
    data = info if isinstance(info, Mapping) else {}
    raw = data.get("house_rules")
    if not isinstance(raw, Mapping):
        return dict(schema_defaults())
    from trpg_server.house_rules_store import flatten, is_namespaced

    if is_namespaced(raw):
        raw = flatten(raw, RULESET_ID)
    return _normalize_values(raw)


def match_profile(values: Mapping[str, Any]) -> str:
    """当前值精确等于哪个具名档案；都不等则为房主自定义混合档。"""
    normalized = _normalize_values(values)
    for profile_id in profile_ids():
        if normalized == profile_values(profile_id):
            return profile_id
    return PROFILE_CUSTOM


# ---------------------------------------------------------------- 场景形态判定


def _map_service() -> Any | None:
    """veil-map 公开地图服务；插件缺席/核心无服务表时返回 None。"""
    try:
        from trpg_server.plugins.runtime import get_service

        return get_service("veil-map", "map_service")
    except Exception:  # noqa: BLE001 - 核心未装插件运行期时静默降级
        return None


def detect_scenario_form(room_dir: Any) -> dict[str, Any]:
    """判定房间剧本形态：挂了地图 → mapped；否则 text。

    只用现有可判定信号（不依赖 fvtt-world-import 未合并的剧本登记）：
    veil-map 场景目录经公开服务读取。活动场景 → mapped；无活动但有配
    底图的场景（如导入时 activate=false 的世界包）同样视为 mapped；
    只有空场景或没有任何场景 → text。
    """
    service = _map_service()
    if service is None or not callable(getattr(service, "list_scenes", None)):
        return {"form": "text", "reasons": ["veil-map 未启用或无场景服务"]}
    try:
        listing = service.list_scenes(room_dir)
    except Exception:  # noqa: BLE001 - 地图读取失败按纯文本形态降级
        return {"form": "text", "reasons": ["地图场景目录读取失败"]}
    if not isinstance(listing, Mapping):
        return {"form": "text", "reasons": ["地图场景目录读取失败"]}
    scenes = [item for item in (listing.get("scenes") or []) if isinstance(item, dict)]
    active = next((item for item in scenes if item.get("active")), None)
    if active is not None:
        title = str(active.get("title") or active.get("scene_id") or "").strip()
        return {"form": "mapped", "reasons": [f"活动地图场景「{title or '未命名'}」"]}
    mapped = [item for item in scenes if str(item.get("background") or "").strip()]
    if mapped:
        return {
            "form": "mapped",
            "reasons": [f"{len(mapped)} 张已配底图的场景（当前未激活）"],
        }
    if scenes:
        return {"form": "text", "reasons": ["仅有未配底图的空场景"]}
    return {"form": "text", "reasons": ["房间未挂地图"]}


def recommendation_for(form: str) -> str:
    return PROFILE_GRID if str(form or "").strip() == "mapped" else PROFILE_TOM


# ---------------------------------------------------------------- 站点开关与记忆


def site_recommendation_enabled(config_dir: Any) -> bool:
    """站点级推荐开关：``<CONFIG_DIR>/dnd5e-house-rules.json``。

    文件缺席或键缺失视为开启（默认行为不变）；只有显式写 false 才关闭。
    """
    if not config_dir:
        return True
    try:
        from trpg_server.json_store import read_json

        data = read_json(Path(config_dir) / SITE_CONFIG_FILENAME, default={})
    except Exception:  # noqa: BLE001 - 配置读失败按默认开启
        return True
    if not isinstance(data, Mapping) or data.get(_SITE_CONFIG_KEY) is None:
        return True
    return bool(data.get(_SITE_CONFIG_KEY))


def _marker_path(room_dir: Any) -> Path:
    return Path(room_dir) / "plugins" / RULESET_ID / "house-rule-profile.json"


def read_marker(room_dir: Any) -> dict[str, Any]:
    """插件自有的档案记忆（auto/manual、上次应用的快照与理由）。"""
    try:
        from trpg_server.json_store import read_json

        data = read_json(_marker_path(room_dir), default={})
    except Exception:  # noqa: BLE001 - 记忆不可读按「从未自动应用」处理
        return {}
    return data if isinstance(data, Mapping) else {}


def write_marker(room_dir: Any, marker: Mapping[str, Any]) -> None:
    from trpg_server.json_store import write_json_atomic

    path = _marker_path(room_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": MARKER_SCHEMA_VERSION,
        **{str(key): value for key, value in dict(marker).items()},
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_json_atomic(path, payload)


def resolve_state(room_dir: Any, info: Mapping[str, Any] | None) -> dict[str, Any]:
    """房间房规三态：default（从未设置）/ auto（仅自动应用过）/ manual。

    manual 判定：档案记忆已标 manual，或当前值既不是 schema 全默认、也
    不等于上次自动应用的快照——房主经现有房规接口改过任何键即成立。
    """
    marker = read_marker(room_dir)
    values = current_values(info)
    source = str(marker.get("source") or "").strip().lower()
    if source == "manual":
        state = "manual"
    elif source == "auto":
        snapshot = marker.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        state = "auto" if values == _normalize_values(snapshot) else "manual"
    elif values == schema_defaults():
        state = "default"
    else:
        state = "manual"
    return {
        "source": state,
        "window_open": state in {"default", "auto"},
        "profile": match_profile(values),
        "values": values,
    }


# ---------------------------------------------------------------- 自动应用


def apply_profile(room_dir: Any, info: dict[str, Any], profile_id: str, *, reasons: list[str] | None = None) -> dict[str, Any]:
    """把档案值写进房间房规（唯一整份写入口，与核心房规 PUT 同一持久化路径）。

    只覆盖本插件 schema 声明的 5e 键，core 键与其它规则集命名空间原样
    保留（``build_namespaced`` 按 base 重建）。写入后落档案记忆
    （source=auto + 完整快照），供后续判定「房主是否手动改过」。
    """
    if str(profile_id or "") not in profile_ids():
        raise ValueError(f"未知的房规档案：{profile_id}")
    target = profile_values(profile_id)
    current = current_values(info)
    marker = read_marker(room_dir)
    already_in_effect = current == target
    already_recorded = (
        str(marker.get("profile") or "") == str(profile_id)
        and str(marker.get("source") or "") == "auto"
    )
    if already_in_effect and already_recorded:
        return {"changed": False, "values": current}

    from trpg_server.house_rules_store import build_namespaced, flatten, is_namespaced

    raw = info.get("house_rules")
    raw = raw if isinstance(raw, Mapping) else {}
    if is_namespaced(raw):
        base = raw
        flat = dict(flatten(raw, RULESET_ID))
    else:
        base = info.get("_house_rules_ns")
        base = base if isinstance(base, Mapping) else {}
        flat = dict(raw)
        legacy = info.get("_house_rules_legacy")
        if isinstance(legacy, Mapping):
            for key, value in legacy.items():
                flat.setdefault(key, value)
    flat.update(target)

    namespaced = build_namespaced(flat, RULESET_ID, base)
    # 内存视图与核心 PUT 一致：扁平视图 + 原始命名空间，落盘时统一重构。
    info["house_rules"] = flat
    info["_house_rules_ns"] = namespaced

    from trpg_server.room_rules import save_room_info

    save_room_info(room_dir, info)
    write_marker(
        room_dir,
        {
            "profile": str(profile_id),
            "source": "auto",
            "snapshot": target,
            "reasons": [str(item) for item in (reasons or [])],
        },
    )
    return {"changed": True, "values": target}


# ---------------------------------------------------------------- 推荐端点载荷


def recommendation_payload(
    room_dir: Any,
    info: Mapping[str, Any],
    *,
    can_apply: bool,
    config_dir: Any,
) -> dict[str, Any]:
    """推荐端点的完整载荷：判定 → 推荐 → （窗口内且调用方可写时）自动应用。

    任何一环失败都不抛出：站点开关关闭 → status=disabled；房主已手动
    设置 → status=manual（不再自动推荐）；其余 status=ok。
    """
    site_enabled = site_recommendation_enabled(config_dir)
    detection = detect_scenario_form(room_dir)
    recommended = recommendation_for(detection["form"])
    state = resolve_state(room_dir, info)

    payload: dict[str, Any] = {
        "site_enabled": site_enabled,
        "scenario_form": detection["form"],
        "reasons": detection["reasons"],
        "current_profile": state["profile"],
        "current_source": state["source"],
        "can_apply": bool(can_apply),
        "profiles": profile_catalog(),
        "recommended_profile": "",
        "recommendation_active": False,
        "auto_applied": False,
    }
    if not site_enabled:
        payload["status"] = "disabled"
        return payload
    if not state["window_open"]:
        # 房主手动设置过：记住 manual，此后只报告现状、不再自动推荐。
        payload["status"] = "manual"
        return payload

    payload["recommended_profile"] = recommended
    payload["recommendation_active"] = True
    payload["status"] = "ok"
    if can_apply and isinstance(info, dict):
        try:
            result = apply_profile(room_dir, info, recommended, reasons=detection["reasons"])
            payload["auto_applied"] = bool(result.get("changed"))
            state = resolve_state(room_dir, info)
            payload["current_profile"] = state["profile"]
            payload["current_source"] = state["source"]
        except Exception:  # noqa: BLE001 - 自动应用失败只降级为纯推荐
            logger.warning("dnd5e_profile_auto_apply_failed", exc_info=True)
            payload["auto_applied"] = False
    return payload


# ---------------------------------------------------------------- KP 提示词联动


def profile_prompt_line(values: Mapping[str, Any]) -> str:
    """「房规口径」一行：档案名 + 距离口径 + 夹击开关。

    距离口径按 ``dnd5e_positioning`` 实际值叙述（自定义混合档也如实），
    只描述引擎当前真实生效的口径；预留未生效键不进提示词。
    """
    profile = match_profile(values)
    labels = {
        PROFILE_TOM: "剧场式（theater-of-mind）",
        PROFILE_GRID: "方格战斗（grid-combat）",
        PROFILE_CUSTOM: "房主自定义混合档",
    }
    if str(values.get("dnd5e_positioning") or "") == "grid":
        positioning = (
            "位置口径：方格——距离以格计，1 格 = 5 尺，对角线按 5-5-5"
            "（斜向也算 1 格，引擎当前口径）折算；描述移动、射程、法术范围与"
            "战术位置时先按棋盘格数换算再叙述，与棋子实际位置保持一致，"
            "不要凭空估计距离。"
        )
    else:
        positioning = (
            "位置口径：剧场式——距离用档位叙述：近战（约 5 尺内）、近（约 30 尺内）、"
            "远（30 尺外）；位置关系由叙事维持，除非工具返回了明确数值，"
            "不要以格数或精确英尺结算移动与射程。"
        )
    if values.get("dnd5e_flanking"):
        flanking = (
            "夹击（flanking）：开启——被标记为夹击的近战攻击由战斗工具结算为优势。"
        )
    else:
        flanking = "夹击（flanking）：关闭——不因站位关系给近战攻击优势。"
    return f"【房规口径】当前档案：{labels[profile]}。{positioning}{flanking}"


class _KpContextService:
    """给 KP 提示词装配层的房规口径行。

    核心只认 ``get_service("dnd5e", "kp_context")`` 这个公开通道
    （dnd5e-vtt 战桌状态行同一机制）；插件缺席、非 dnd5e 房间、任何读取
    失败都返回空列表，KP 主流程不受影响。
    """

    def kp_prompt_fragments(self, context: Any) -> list[str]:
        try:
            room_dir = getattr(context, "room_dir", None)
            if not room_dir:
                return []
            info_fn = getattr(context, "room_info", None)
            info = info_fn() if callable(info_fn) else info_fn
            if not isinstance(info, dict):
                return []
            if self._ruleset_id(info, getattr(context, "scenarios_dir", None)) != RULESET_ID:
                return []
            line = profile_prompt_line(current_values(info))
            return [line] if str(line or "").strip() else []
        except Exception:  # noqa: BLE001 - 房规口径行失败绝不阻断 KP 回合
            logger.debug("dnd5e_house_rule_kp_context_failed", exc_info=True)
            return []

    @staticmethod
    def _ruleset_id(info: dict[str, Any], scenarios_dir: Any) -> str:
        try:
            from trpg_server.room_rules import resolve_rule_id

            return str(resolve_rule_id(info, scenarios_dir=scenarios_dir) or "").strip().lower()
        except Exception:  # noqa: BLE001 - 解析失败退回房间显式规则集
            return str((info or {}).get("ruleset_id") or "").strip().lower()


_KP_CONTEXT_SERVICE = _KpContextService()


def register_kp_context() -> None:
    """把房规口径服务挂进插件服务表（幂等：以注册表现状为准）。

    用 ``get_service`` 探测而不是模块级开关位：测试里插件反复
    clear/reload 时，本模块经 sys.modules 缓存复用，模块级布尔会漏注册。
    """
    try:
        from trpg_server.plugins.runtime import get_service, register_service
    except Exception:  # noqa: BLE001 - 旧核心没有服务表时静默跳过
        return
    try:
        if get_service(RULESET_ID, "kp_context") is not None:
            return
        register_service(RULESET_ID, "kp_context", _KP_CONTEXT_SERVICE)
    except Exception:  # noqa: BLE001 - 注册失败不影响插件其余能力
        logger.debug("dnd5e_kp_context_register_failed", exc_info=True)


__all__ = [
    "PROFILE_CUSTOM",
    "PROFILE_GRID",
    "PROFILE_TOM",
    "current_values",
    "detect_scenario_form",
    "match_profile",
    "profile_catalog",
    "profile_ids",
    "profile_prompt_line",
    "profile_values",
    "recommendation_for",
    "recommendation_payload",
    "register_kp_context",
    "resolve_state",
    "schema_defaults",
    "site_recommendation_enabled",
]
