"""房间模式：AI KP 开关与剧本绑定强度的共享判定。

房间在 ``info.json`` 里保存三个正交字段：

- ``room_mode``：``scripted`` / ``freeform`` / ``manual``，用于展示与 UI 预设；
- ``ai_kp_enabled``：是否允许调用 AI KP 叙事链路；
- ``scenario_binding``：``required`` / ``optional`` / ``none``，剧本绑定强度。

旧房间没有这些字段时按 ``scripted / True / required`` 推断，保证向后兼容；
读取时不做数据改写，写回 ``info.json`` 的路径必须显式落盘。

HTTP 路由、Socket 与控制台都通过本模块回答「这个房间能不能用 AI / 能不能绑定剧本」，
避免各自实现一套判断。
"""

from __future__ import annotations

ROOM_MODE_SCRIPTED = "scripted"
ROOM_MODE_FREEFORM = "freeform"
ROOM_MODE_MANUAL = "manual"
ROOM_MODES = (ROOM_MODE_SCRIPTED, ROOM_MODE_FREEFORM, ROOM_MODE_MANUAL)

SCENARIO_BINDING_REQUIRED = "required"
SCENARIO_BINDING_OPTIONAL = "optional"
SCENARIO_BINDING_NONE = "none"
SCENARIO_BINDINGS = (
    SCENARIO_BINDING_REQUIRED,
    SCENARIO_BINDING_OPTIONAL,
    SCENARIO_BINDING_NONE,
)

# 三个 UI 预设到两个能力位的映射。存储层允许更自由的组合（例如 scripted + AI 关，
# 即「真人 KP 带模组」），但创建入口只暴露这三个预设，避免把组合爆炸推给玩家。
MODE_PRESETS = {
    ROOM_MODE_SCRIPTED: {
        "ai_kp_enabled": True,
        "scenario_binding": SCENARIO_BINDING_REQUIRED,
    },
    ROOM_MODE_FREEFORM: {
        "ai_kp_enabled": True,
        "scenario_binding": SCENARIO_BINDING_NONE,
    },
    ROOM_MODE_MANUAL: {
        "ai_kp_enabled": False,
        "scenario_binding": SCENARIO_BINDING_OPTIONAL,
    },
}


def _clean_choice(value, allowed):
    text = str(value or "").strip().lower()
    return text if text in allowed else ""


def normalize_room_mode(info):
    """返回 ``{"room_mode", "ai_kp_enabled", "scenario_binding"}``。

    - ``freeform`` 必须开启 AI 且禁止绑定剧本；
    - ``manual`` 默认关闭 AI，剧本绑定降为可选；
    - ``scripted`` 保留 AI 开关的自定义值（可关，用于真人 KP），剧本必填；
    - 缺少 ``room_mode`` 的旧房间：``ai_kp_enabled`` 显式为 false 时推断为
      manual，否则一律按 scripted 处理。
    """
    info = info if isinstance(info, dict) else {}
    stored_ai = info.get("ai_kp_enabled")
    mode = _clean_choice(info.get("room_mode"), ROOM_MODES)
    binding = _clean_choice(info.get("scenario_binding"), SCENARIO_BINDINGS)

    if not mode:
        if stored_ai is False:
            mode = ROOM_MODE_MANUAL
        else:
            mode = ROOM_MODE_SCRIPTED

    preset = MODE_PRESETS[mode]
    if mode == ROOM_MODE_FREEFORM:
        return {
            "room_mode": mode,
            "ai_kp_enabled": True,
            "scenario_binding": SCENARIO_BINDING_NONE,
        }
    if mode == ROOM_MODE_MANUAL:
        return {
            "room_mode": mode,
            "ai_kp_enabled": False,
            "scenario_binding": (
                binding if binding in {SCENARIO_BINDING_OPTIONAL, SCENARIO_BINDING_NONE} else SCENARIO_BINDING_OPTIONAL
            ),
        }
    return {
        "room_mode": mode,
        "ai_kp_enabled": stored_ai if isinstance(stored_ai, bool) else preset["ai_kp_enabled"],
        "scenario_binding": SCENARIO_BINDING_REQUIRED,
    }


def room_mode_of(info):
    return normalize_room_mode(info)["room_mode"]


def ai_kp_enabled(info):
    """房间是否允许调用 AI KP（配乐、配图、语音、导演、世界意志都以它为前提）。"""
    return normalize_room_mode(info)["ai_kp_enabled"]


def scenario_binding_of(info):
    return normalize_room_mode(info)["scenario_binding"]


def accepts_scenario(info):
    """房间是否允许写入剧本绑定（freeform 一律拒绝）。"""
    return scenario_binding_of(info) != SCENARIO_BINDING_NONE


def requires_scenario(info):
    return scenario_binding_of(info) == SCENARIO_BINDING_REQUIRED


def has_bound_scenario(info):
    """房间当前是否真的绑定了剧本；manual 下仅作参考资料，不代表会驱动 AI。"""
    return accepts_scenario(info) and bool((info or {}).get("scenario_id"))
