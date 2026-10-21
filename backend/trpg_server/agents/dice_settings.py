"""骰子动画档位与玩家投掷的生效值解析。

这是「房间规则优先，管理员默认兜底」的唯一实现：房间里的 ``null`` 表示继承
管理员设置页配置。前端与工具都通过本模块取值，避免出现两套判断。
"""
from __future__ import annotations

from typing import Any

from trpg_server.agents.config import AIRuntimeConfig, load_ai_runtime_config

DICE_ANIMATION_LEVELS = ("text", "simple", "3d")
DEFAULT_DICE_ANIMATION_LEVEL = "3d"


def normalize_level(value: Any, fallback: str = DEFAULT_DICE_ANIMATION_LEVEL) -> str:
    """把任意输入规范化为合法档位；非法值回退。"""
    text = str(value or "").strip().lower()
    return text if text in DICE_ANIMATION_LEVELS else fallback


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
    """返回房间内的生效档位、玩家投掷开关与超时。

    ``level_source`` / ``player_roll_source`` 为 ``room`` 或 ``admin``，
    供房间详情展示「跟随默认」的当前实际值。
    """
    admin = admin_dice_settings(config_dir)
    rules = house_rules if isinstance(house_rules, dict) else {}

    room_level = rules.get("dice_animation_level")
    has_room_level = room_level in DICE_ANIMATION_LEVELS
    level = room_level if has_room_level else normalize_level(admin.dice_animation_level)

    room_player = rules.get("player_dice_roll")
    has_room_player = isinstance(room_player, bool)
    player_roll = room_player if has_room_player else bool(admin.dice_player_roll)

    timeout = int(getattr(admin, "dice_player_roll_timeout", 120) or 120)
    return {
        "level": level,
        "player_roll": player_roll,
        "timeout": max(30, min(300, timeout)),
        "level_source": "room" if has_room_level else "admin",
        "player_roll_source": "room" if has_room_player else "admin",
    }
