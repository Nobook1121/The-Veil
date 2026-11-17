"""D&D 5e 休息规则数值。

核心把「短休/长休」当作规则集中立操作，只负责资源与状态的通用恢复流程；
休息时长、生命骰恢复比例等具体数值属于插件，由 ``dnd5e_rest_variant`` 房规决定。
"""

from __future__ import annotations

from typing import Any

from dnd5e_plugin.combat import house_rule

# 各休息变体的时长（分钟）；用于叙事时间推进、记录与提示词。
REST_PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "short_rest_minutes": 60,
        "long_rest_minutes": 480,
        "label": "标准休息",
    },
    "gritty": {
        "short_rest_minutes": 480,
        "long_rest_minutes": 10080,
        "label": "坚韧现实",
    },
    "heroic": {
        "short_rest_minutes": 5,
        "long_rest_minutes": 60,
        "label": "英雄式休息",
    },
}


def rest_profile(settings: Any) -> dict[str, Any]:
    """按房间房规返回休息档案；未知取值退回标准休息。"""
    variant = str(house_rule(settings, "dnd5e_rest_variant", "standard") or "").strip().lower()
    return dict(REST_PROFILES.get(variant) or REST_PROFILES["standard"])


__all__ = ["REST_PROFILES", "rest_profile"]
