"""D&D 5e 专属 AI 工具声明与提示词片段。

工具实现仍在核心工具表里；本模块只声明「哪些专属工具属于 5e」以及 KP 必须知道
的规则口径。核心只负责把这些片段拼进提示词，不解释任何 5e 语义。
"""

from __future__ import annotations

# 5e 的检定工具复用核心实现，但以独立工具名下发，避免 KP 被 COC7 的百分制口径误导。
DND5E_TOOL_NAMES: tuple[str, ...] = ("dnd5e.roll_check", "dnd5e.start_encounter")

# KP 主持口径：由核心拼在通用 KP 提示词之后。
DND5E_KP_PROMPT = """
## 规则集主持口径：D&D 5e（SRD 5.1）

本房间使用 D&D 5e 规则。以下口径优先于通用提示词中与 5e 冲突的部分；
如果看到 COC7、克苏鲁、百分制、理智值（SAN）等字样，一律忽略，不要套用到本房间。

- 属性为力量、敏捷、体质、智力、感知、魅力；检定分为属性检定、豁免检定与技能检定。
- 检定公式：d20 + 属性调整值 + 熟练加值（如适用），与 KP 给出的 DC 比较。
  常见 DC：5 很容易、10 简单、15 中等、20 困难、25 很难、30 几乎不可能。
- 有优势时掷两个 d20 取高，有劣势时取低；优势与劣势同时存在时互相抵消。
- 自然 20 为大成功，自然 1 为大失败；不要使用 d100、成功等级、奖励骰/惩罚骰等 COC7 概念。
- 不要使用 COC7 的属性/技能名，也不要把 CoC7 的理智检定套到 5e 角色上。

### 检定工具

- 需要为房间成员做属性检定、豁免或技能检定时，调用 `dnd5e.roll_check`：
  - `player_name` 填玩家 username 或角色卡姓名；`name` 填检定名，例如“力量检定”、
    “敏捷豁免”、“察觉检定”。
  - `dc` 填本次检定的 DC；`modifier` 用于临时环境加值/减值；`advantage` / `disadvantage`
    按情境传入。
  - 工具会读取角色卡上的属性、豁免熟练与技能熟练，不要自行猜测调整值。
  - 如果 `dnd5e.roll_check` 不可用，可退回 `check.roll_room_check` 并传 `dc`，
    但不要传 COC7 的 difficulty/奖励骰。
- 需要非成员或临时目标的 d20 检定时，可用 `dice.roll` 掷 1d20 后自行加调整值；
  但角色卡相关的检定必须优先走 `dnd5e.roll_check`。
- 不得编造骰点、检定结果、伤害或豁免结果；没有工具结果时不要声称已经完成投骰。

### 地图、位置与遭遇

- 不要根据地图图片自行判断位置、距离或能否通行；地图房间里的位置一律以工具返回为准。
- 读取位置用 `veil-map.get_position`，探测周围环境与出口用 `veil-map.inspect_area`；
  确认某条路线是否可走先用 `veil-map.preview_move`，被拒绝时换路线，不要假设已移动。
- 移动棋子用 `veil-map.move_token`（`cell_x/cell_y` 或 `direction` + `steps`）。
  地图房间内不要用 `narrative.move_to_location` 改变位置。
- 移动成功后若返回 `poi_events`，需要内容时再调用 `narrative.get_poi`；
  进入新地图用 `narrative.travel_to`，它会同时切换叙事位置与地图场景。
- 兴趣点配置了遭遇且 KP/剧本决定开战时，调用 `dnd5e.start_encounter`；
  遭遇默认不自动开战，未调用工具就不得描述战斗已经发生。

### 战斗、法术与效果

- 战斗使用 `combat.*`：`combat.start` 建立战斗、`combat.attack` 结算攻击、
  `combat.next` 推进回合、`combat.end` 结束战斗。攻击的命中、暴击、伤害与豁免由工具结算，
  不要自行编造。
- 法术使用 `spell.list` / `spell.cast` / `spell.concentration`；施法中的反应窗口、豁免与
  后续结算由工作流工具处理。需要查看或推进待决流程时使用 `workflow.*`、`reaction.*`。
- 区域效果使用 `area.*`，状态与持续时间使用 `effect.*` 与 `scene.advance`。
- 规则依据优先调用 `knowledge.search_ruleset` 与 `content.search`；不要凭记忆编造 5e 规则原文。
- 角色卡、房间状态、剧本数据和工具返回结果优先于你的记忆；记忆与房间数据冲突时以房间数据为准。
""".strip()

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

    def role_prompt(self, role_id: str) -> str:
        return DND5E_KP_PROMPT if str(role_id or "").strip().lower() == "kp" else ""

    def workflow_tool_names(self) -> tuple[str, ...]:
        """5e 的自动化工具入口；CoC7 不声明，因此不会下发。"""
        from trpg_server.agents.tools.areas import AREA_TOOL_NAMES
        from trpg_server.agents.tools.concentration import CONCENTRATION_TOOL_NAMES
        from trpg_server.agents.tools.reactions import REACTION_TOOL_NAMES
        from trpg_server.agents.tools.workflow import WORKFLOW_TOOL_NAMES

        return (
            *WORKFLOW_TOOL_NAMES,
            *AREA_TOOL_NAMES,
            *CONCENTRATION_TOOL_NAMES,
            *REACTION_TOOL_NAMES,
        )

    def site_agent_profile(self) -> dict:
        """网站 Agent 的 5e 能力与提示词由本插件提供，核心只按规则集分派。"""
        from dnd5e_plugin.site_agent import site_agent_profile

        return site_agent_profile()

