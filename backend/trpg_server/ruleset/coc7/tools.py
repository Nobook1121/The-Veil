"""CoC7 专属 AI 工具声明。

工具实现仍在核心工具表（``agents/tools/dice.py``），这里只声明「哪些工具属于本规则」，
供后续按房间规则集组装 profile。命名保持现状（``san.*`` / ``check.*``）以兼容既有
``DEFAULT_KP_TOOLS`` 与提示词，不新造二级命名空间。
"""

from __future__ import annotations

# 只有 CoC7 才需要的工具；其余（room / dice.roll / knowledge / trigger / narrative）是核心通用工具。
COC7_TOOL_NAMES = (
    "dice.roll_coc_check",
    "check.roll_room_check",
    "san.roll_sanity_check",
    "sanity.roll_sanity_check",
    "san.roll_madness",
    "sanity.roll_madness",
    # 批次 3 房规：理智结构化 / 发作 / 习惯恐惧 / 现实认知 / 恐惧躁狂表。
    "san.bout",
    "san.habituation",
    "san.reality_check",
    "san.traits",
    # 批次 1 房规：开关关闭时由 routes/chat.py 从 profile 中剔除。
    "coc7.social_check",
    "coc7.combined_check",
    "coc7.group_luck",
    "coc7.contact_check",
    "coc7.economy",
)


# 通用检定流程（社交 / 组合 / 团体幸运等）引用的工具 id 由插件声明，
# 核心不再拼接 ``coc7.xxx`` 或硬编码具体工具名。
CHECK_TOOL_KEYS = {
    "social": "coc7.social_check",
    "combined": "coc7.combined_check",
    "group_luck": "coc7.group_luck",
    "contact": "coc7.contact_check",
    "economy": "coc7.economy",
}


class Coc7Tools:
    ruleset_id = "coc7"

    def tool_names(self) -> tuple[str, ...]:
        return COC7_TOOL_NAMES

    def check_tool_key(self, kind: str) -> str:
        """通用检定流程使用的工具 id；未声明的类型回退到本规则集命名空间。"""
        return CHECK_TOOL_KEYS.get(str(kind or "").strip().lower(), f"coc7.{kind}")

    def prompt_fragments(self) -> list[str]:
        # 现有 KP 提示词已包含 CoC7 判定口径，本期不额外注入，避免重复占 token。
        return []
