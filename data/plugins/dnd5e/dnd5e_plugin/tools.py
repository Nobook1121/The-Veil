"""D&D 5e 专属 AI 工具声明与提示词片段。

工具实现仍在核心工具表里；本模块只声明「哪些专属工具属于 5e」以及 KP 必须知道
的规则口径。核心只负责把这些片段拼进提示词，不解释任何 5e 语义。
"""

from __future__ import annotations

# 5e 目前没有新增专属工具：通用 spell.* / scene.advance / combat.* 已覆盖。
DND5E_TOOL_NAMES: tuple[str, ...] = ()

# 提示词片段：每条独立成句，由核心按顺序拼进「规则集判定口径」段落。
PROMPT_FRAGMENTS = (
    "战斗外施放带持续时间的法术（含仪式与专注）后，效果只在时间推进时到期；"
    "需要让效果过期时调用 scene.advance 推进分钟数，不要假定它会自动消失。",
    "仪式施法只在战斗外可用，不消耗法术位，但额外耗时 10 分钟；"
    "施法后需要对齐游戏时间时，用 scene.advance 或房间时钟推进。",
    "战斗外专注因受伤需要检定时，调用 spell.concentration 并传入 damage，"
    "由规则集计算 DC；主动解除专注用 action=break。",
    "战斗外的法术位、HP 与专注都记录在角色状态里；施法前可用 spell.list 查看剩余法术位。",
)


class Dnd5eTools:
    ruleset_id = "dnd5e"

    def tool_names(self) -> tuple[str, ...]:
        return DND5E_TOOL_NAMES

    def prompt_fragments(self) -> list[str]:
        return list(PROMPT_FRAGMENTS)
