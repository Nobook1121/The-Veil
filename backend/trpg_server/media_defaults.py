"""历史默认图片路径与归一化工具。

项目不再使用任何内置默认图片：头像改为「名称首字」文字头像，剧本封面改为剧本名
文字封面，两者都由前端生成。本模块集中判定历史上的默认图路径，把曾经写入过
默认图的字段统一归一化为空字符串，避免在各个读写路径里重复散落魔法字符串。
"""

from __future__ import annotations

from typing import Any

# 项目曾内置的默认头像：通用 / KP / 系统消息 / 骰娘消息。
LEGACY_DEFAULT_AVATARS = frozenset(
    {
        "/assets/avatars/default.jpg",
        "/assets/avatars/default_kp.jpg",
        "/assets/avatars/default_system.jpg",
        "/assets/avatars/default_dice.jpg",
    }
)

# 项目曾内置的默认剧本封面。
LEGACY_DEFAULT_COVERS = frozenset(
    {
        "/assets/scenario_covers/default_cover.png",
    }
)


def normalize_avatar(value: Any) -> str:
    """归一化头像路径：未设置或指向历史默认图时返回空字符串。"""

    return _normalize(value, LEGACY_DEFAULT_AVATARS)


def normalize_cover(value: Any) -> str:
    """归一化剧本封面路径：未设置或指向历史默认图时返回空字符串。"""

    return _normalize(value, LEGACY_DEFAULT_COVERS)


def _normalize(value: Any, legacy: frozenset[str]) -> str:
    text = str(value or "").strip()
    if not text or text in legacy:
        return ""
    return text
