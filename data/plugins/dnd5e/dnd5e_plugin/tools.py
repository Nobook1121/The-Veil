"""D&D 5e 专属 AI 工具声明与提示词片段。

既有检定工具的实现仍在核心工具表里，本模块只声明「哪些专属工具属于 5e」以及
KP 必须知道的规则口径；插件自有工具（遭遇开战、公式求值）分别由
``encounter.py`` 与本模块的 ``create_tools()`` 提供给插件注册表。核心只负责把
这些片段拼进提示词，不解释任何 5e 语义。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.agents.tools.base import AgentTool

# 5e 的检定工具复用核心实现，但以独立工具名下发，避免 KP 被 COC7 的百分制口径误导。
DND5E_TOOL_NAMES: tuple[str, ...] = ("dnd5e.roll_check", "dnd5e.start_encounter")

# KP 工具裁剪：百分制/克苏鲁专用工具与 5e 下不应出现的直接改卡入口不下发；
# combat.heal / combat.join 属于通用战斗工具，但对 5e 房间有实际用途，显式补回。
# 注：核心的技能调整工具 id 是 ``character.set_skill``。
DND5E_KP_TOOL_DENY: tuple[str, ...] = (
    "combat.shoot",
    "combat.treat",
    "character.set_attribute",
    "character.set_skill",
    "character.add_item",
    "room.request_interlude",
)
DND5E_KP_TOOL_ALLOW: tuple[str, ...] = ("combat.heal", "combat.join")

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

    def kp_tool_policy(self) -> dict[str, tuple[str, ...]]:
        """KP 工具 allow/deny；核心只执行声明，不写 5e 分支。"""
        return {"deny": DND5E_KP_TOOL_DENY, "allow": DND5E_KP_TOOL_ALLOW}

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


# ---------------------------------------------------------------- 公式求值工具

#: 允许从工具参数直接注入的上下文键（其余的过滤掉，避免把任意 JSON 灌进求值器）。
FORMULA_CONTEXT_KEYS: tuple[str, ...] = ("level", "abilities", "attributes", "bonuses", "prof")

FORMULA_EVAL_DESCRIPTION = (
    "安全求值 D&D 5e 公式字符串（FVTT 公式字段、骰式与 @ 引用），只做算术与投骰，不能执行任意代码。"
    "适用场景："
    "① 导入的 FVTT 数据里出现 max(30, 30 + @bonuses.speed)、1d6 + @abilities.str.mod 这类公式字段，"
    "需要算出当前角色上的实际数值；"
    "② 玩家问「这个加值/伤害/AC 是怎么算出来的」，需要逐项代入角色卡数据并给出投骰明细；"
    "③ 需要临时结算一条骰式（2d6+3、1d8 + @abilities.dex.mod）而不想建战斗。"
    "用法：formula 填公式原文；character_id 填房间内绑定的角色卡 id，"
    "工具会从该卡 sheet 取 level/prof/abilities/attributes 等 roll data；"
    "也可以直接给 context（键限 level/abilities/attributes/bonuses/prof）覆盖个别数值。"
    "支持：数字/字符串/布尔、+ - * / // %、比较、and or not、括号、一元负号、"
    "骰式 NdM（如 1d6、2d6、d20）、@path.to.value 引用、"
    "函数 max/min/floor/ceil/round/abs/clamp/sign/if(cond,a,b)。"
    "不支持属性访问代码、下标、lambda、推导式、f-string、__import__/open 等任何 Python 能力；"
    "越界或未知引用会返回可读错误。骰点由工具真实投出，不要自己编造点数。"
)

FORMULA_EVAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "formula": {
            "type": "string",
            "description": (
                "公式原文，例如 \"1d6 + @abilities.str.mod\"、\"max(30, 30 + 10)\"、"
                "\"2 * @level\"、\"clamp(@abilities.dex.mod, 0, 5)\"。"
            ),
        },
        "character_id": {
            "type": "string",
            "description": (
                "可选。房间成员绑定的角色卡 id；给了就从该卡 sheet 生成 roll data"
                "（level/prof/abilities/attributes），不必手工传数值。"
            ),
        },
        "context": {
            "type": "object",
            "description": (
                "可选。直接给定 roll data，只接受 level/abilities/attributes/bonuses/prof 五个键；"
                "与 character_id 同时给时，标量键覆盖角色卡算出的值。"
            ),
            "properties": {
                "level": {"type": "integer", "description": "角色等级。"},
                "prof": {"type": "integer", "description": "熟练加值。"},
                "abilities": {
                    "type": "object",
                    "description": "属性分值，如 {\"str\": 16, \"dex\": 14}；@abilities.dex.mod 会自动按 (值-10)//2 补。",
                },
                "attributes": {
                    "type": "object",
                    "description": "派生数值，如 {\"prof\": 2, \"hp\": {\"value\": 20, \"max\": 24}}。",
                },
                "bonuses": {
                    "type": "object",
                    "description": "效果/物品加值，如 {\"speed\": 10, \"abilities\": {\"save\": 1}}。",
                },
            },
        },
    },
    "required": ["formula"],
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _character_card(context: Any, character_id: str) -> dict[str, Any] | None:
    """按 id 在房间成员绑定的角色卡里找卡；找不到返回 None（不读盘外数据）。"""
    if not character_id:
        return None
    room_info = getattr(context, "room_info", None)
    info = room_info() if callable(room_info) else room_info
    members = (info or {}).get("members") if isinstance(info, Mapping) else None
    for member in members if isinstance(members, list) else []:
        if not isinstance(member, Mapping):
            continue
        card = member.get("character_card")
        if not isinstance(card, Mapping):
            continue
        known = {
            _text(card.get("id")),
            _text(member.get("character_id")),
            _text(member.get("characterCardId")),
        }
        if character_id in known:
            return dict(card)
    return None


def _explicit_context(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """取参数里的 context，只保留白名单键；第二项表示是否给了非法类型。"""
    raw = arguments.get("context")
    if raw is None:
        return {}, True
    if not isinstance(raw, Mapping):
        return {}, False
    return {key: raw[key] for key in FORMULA_CONTEXT_KEYS if key in raw}, True


def _evaluate_formula(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """``dnd5e.formula_eval`` 的处理函数：装配 roll data 后交给 ``formula.evaluate``。"""
    from dnd5e_plugin import formula as formula_module

    formula_text = arguments.get("formula")
    if not isinstance(formula_text, str) or not formula_text.strip():
        return {
            "ok": False,
            "code": "dnd5e.formula_required",
            "error": "请提供要计算的公式（formula），例如 1d6 + @abilities.str.mod。",
        }
    explicit, context_ok = _explicit_context(arguments)
    if not context_ok:
        return {
            "ok": False,
            "code": "dnd5e.context_invalid",
            "error": "context 必须是对象，只接受 level/abilities/attributes/bonuses/prof 五个键。",
        }

    roll_data: dict[str, Any] = {}
    source = "context" if explicit else "none"
    character_id = _text(arguments.get("character_id"))
    if character_id:
        card = _character_card(context, character_id)
        if card is None:
            return {
                "ok": False,
                "code": "dnd5e.character_not_found",
                "error": (
                    f"房间成员里找不到角色卡「{character_id}」；"
                    "请确认 id 属于本房间绑定的卡，或改用 context 直接给数值。"
                ),
            }
        from dnd5e_plugin.rules import sheet_of

        roll_data.update(formula_module.roll_data_from_sheet(sheet_of(card)))
        source = "character"
    if explicit:
        roll_data.update(explicit)
        source = "character+context" if source == "character" else "context"

    result = formula_module.evaluate(formula_text, roll_data)
    if result.get("ok"):
        result["context_source"] = source
        if character_id:
            result["character_id"] = character_id
    return result


FORMULA_EVAL_TOOL = AgentTool(
    name="dnd5e.formula_eval",
    description=FORMULA_EVAL_DESCRIPTION,
    parameters=FORMULA_EVAL_PARAMETERS,
    handler=_evaluate_formula,
    # 与既有插件工具同一口径：只在 dnd5e 房间下发（KP 与 AI 玩家共用规则集门闸）。
    rulesets=("dnd5e",),
)


def create_tools() -> list[AgentTool]:
    """本模块提供的插件工具（``dnd5e.formula_eval``）。"""
    return [FORMULA_EVAL_TOOL]


__all__ = [
    "DND5E_KP_PROMPT",
    "DND5E_KP_TOOL_ALLOW",
    "DND5E_KP_TOOL_DENY",
    "DND5E_TOOL_NAMES",
    "Dnd5eTools",
    "FORMULA_CONTEXT_KEYS",
    "FORMULA_EVAL_TOOL",
    "create_tools",
]

