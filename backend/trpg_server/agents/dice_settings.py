"""骰子动画档位与玩家投掷的生效值解析。

这是「房间规则优先，管理员默认兜底」的唯一实现：房间里的 ``null`` 表示继承
管理员设置页配置。前端与工具都通过本模块取值，避免出现两套判断。
"""
from __future__ import annotations

from typing import Any

from trpg_server.agents.config import AIRuntimeConfig, load_ai_runtime_config

DICE_ANIMATION_LEVELS = ("text", "simple", "3d")
DEFAULT_DICE_ANIMATION_LEVEL = "3d"
DICE_REROLL_MODES = ("off", "limited", "unlimited")
DEFAULT_DICE_REROLL_MODE = "off"
DEFAULT_DICE_REROLL_LIMIT = 3


def normalize_level(value: Any, fallback: str = DEFAULT_DICE_ANIMATION_LEVEL) -> str:
    """把任意输入规范化为合法档位；非法值回退。"""
    text = str(value or "").strip().lower()
    return text if text in DICE_ANIMATION_LEVELS else fallback


def normalize_reroll_mode(value: Any, fallback: str = DEFAULT_DICE_REROLL_MODE) -> str:
    text = str(value or "").strip().lower()
    return text if text in DICE_REROLL_MODES else fallback


def normalize_reroll_limit(value: Any, fallback: int = DEFAULT_DICE_REROLL_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(1, min(20, number))


def reroll_remaining(mode: str, limit: int, used: int) -> int:
    """剩余重投次数：``0`` 关闭、``-1`` 无限、其余为 ``limit - used``。"""
    if mode == "unlimited":
        return -1
    if mode == "limited":
        return max(0, int(limit) - int(used))
    return 0


def admin_dice_settings(config_dir: Any = None) -> AIRuntimeConfig:
    from trpg_server.settings import CONFIG_DIR

    directory = config_dir or CONFIG_DIR
    try:
        return load_ai_runtime_config(directory)
    except Exception:  # noqa: BLE001 - 配置损坏时用默认值，不影响投掷
        return AIRuntimeConfig()


def resolve_dice_settings(
    config_dir: Any = None,
    house_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回房间内的生效档位、玩家投掷开关、重投配置与超时。

    ``level_source`` / ``player_roll_source`` / ``reroll_source`` 为 ``room`` 或
    ``admin``，供房间详情展示「跟随默认」的当前实际值。``rerolls`` 是重投
    次数上限的规范化结果：``0`` 关闭、``-1`` 无限、``N`` 指定次数。
    """
    admin = admin_dice_settings(config_dir)
    rules = house_rules if isinstance(house_rules, dict) else {}

    room_level = rules.get("dice_animation_level")
    has_room_level = room_level in DICE_ANIMATION_LEVELS
    level = room_level if has_room_level else normalize_level(admin.dice_animation_level)

    room_player = rules.get("player_dice_roll")
    has_room_player = isinstance(room_player, bool)
    player_roll = room_player if has_room_player else bool(admin.dice_player_roll)

    # 结果来源只由管理员开关决定：物理骰面需要客户端参与，无法按房规区分。
    physics_results = bool(getattr(admin, "dice_physics_results", True))

    room_mode = rules.get("dice_reroll_mode")
    has_room_mode = room_mode in DICE_REROLL_MODES
    mode = room_mode if has_room_mode else normalize_reroll_mode(admin.dice_reroll_mode)

    room_limit = rules.get("dice_reroll_limit")
    limit = normalize_reroll_limit(room_limit if room_limit is not None else admin.dice_reroll_limit)

    timeout = int(getattr(admin, "dice_player_roll_timeout", 120) or 120)
    return {
        "level": level,
        "player_roll": player_roll,
        "timeout": max(30, min(300, timeout)),
        "reroll_mode": mode,
        "reroll_limit": limit,
        "rerolls": 0 if mode == "off" else (-1 if mode == "unlimited" else limit),
        "physics_results": physics_results,
        "result_source": "physics" if physics_results else "server",
        "level_source": "room" if has_room_level else "admin",
        "player_roll_source": "room" if has_room_player else "admin",
        "reroll_source": "room" if (has_room_mode or room_limit is not None) else "admin",
    }
