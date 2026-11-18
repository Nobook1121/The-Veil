"""D&D 5e 插件对网站 Agent 的声明。

核心只读取这里返回的 profile，并按当前规则集分派；不在核心写死 5e 的工具名、
创作口径或反向约束。
"""

from __future__ import annotations

from dnd5e_plugin.schema import DERIVED_KEYS, ENVELOPE_FIELDS, SECTIONS, SHEET_FIELDS

SITE_AGENT_TOOL_NAMES: tuple[str, ...] = (
    "dnd5e.roll_check",
    "content.search",
    "knowledge.search_ruleset",
    "dice.roll",
    "combat.start",
    "combat.state",
    "combat.next",
    "combat.attack",
    "combat.damage",
    "combat.heal",
    "combat.join",
    "combat.delay",
    "combat.disengage",
    "combat.ready",
    "combat.reload",
    "combat.spend",
    "combat.end",
    "spell.list",
    "spell.cast",
    "spell.concentration",
    "effect.list",
    "effect.apply",
    "effect.remove",
    "scene.advance",
    "area.create",
    "area.list",
    "area.remove",
    "area.tick",
    "area.trigger",
    "workflow.cancel",
    "workflow.decide",
    "workflow.expire",
    "workflow.get",
    "workflow.list",
    "workflow.policy",
    "reaction.list",
    "reaction.resolve",
)

SITE_AGENT_PROMPT = """
当前规则集是 D&D 5e（SRD 5.1）。创作与建卡时遵循 5e 的 d20 体系、职业/等级、
法术位、短休/长休、挑战等级与魔法物品稀有度；不要套用百分制、理智值（SAN）、
调查员职业等其它规则集的概念。

- 先确认队伍等级与人数、游玩时长、战斗/探索/社交的比重、允许的扩展内容与内容红线。
- 冒险按“钩子 → 探索/社交 → 关键线索 → 决战/抉择 → 结局”组织；关键线索至少给三种获取方式。
- 战斗遭遇要与队伍等级和人数匹配，并给谈判、潜行、逃跑或环境互动等替代方案。
- 属性检定、豁免与技能检定使用 d20 + 调整值 + 熟练加值，对抗 DC；优势/劣势按 5e 规则处理。
- NPC 要有目标、动机、态度与可被说服/激怒的触发点；需要战斗时给出可在 5e 中运行的数值或数据卡来源。
- 法术、魔法物品、怪物与装备优先通过 content.search 查当前规则集内容，不要凭记忆编造数值。
- 输出剧本包时显式声明 ruleset_id=dnd5e，并通过 scenarios.import_script 生成待确认预览。
""".strip()

SITE_AGENT_GUIDE = """
# D&D 5e 剧本创作规范

这份规范只讲“怎么写一个能跑、好玩、符合 5e 的冒险”，字段定义见 bundle-schema.md。

## 1. 冒险结构

```
钩子 → 探索/社交 → 关键线索 → 决战/抉择 → 结局
```

| 阶段 | 作用 | 常见问题 |
| --- | --- | --- |
| 钩子 | 给角色一个明确目标或压力 | 目标太泛，队伍不知道下一步 |
| 探索/社交 | 给 2–3 个可自由选择的地点或阵营 | 只有一条路，玩家没有选择 |
| 关键线索 | 把队伍引向真相与决战 | 线索只有一种获取方式，失败就卡死 |
| 决战/抉择 | 战斗、谈判、环境或阵营摊牌 | 只有正面战斗，没有替代方案 |
| 结局 | 成功/失败/妥协/开放 | 结局没有条件，随时可进 |

## 2. 等级与节奏

- 先确定队伍等级、人数和预计时长；1–4 级适合本地威胁，5–10 级适合区域威胁，11 级以上需要更高层级的目标。
- 每场战斗前说明它服务的剧情目的；不要把“清怪”当成唯一内容。
- 安排短休与长休的节奏：让资源消耗有意义，但不要用“不能休息”硬卡队伍。
- 关键战斗要预留撤退、谈判、地形互动或消耗资源换胜利的空间。

## 3. 线索设计：三重原则

任何推进主线的关键结论，至少给三种获取方式：

1. **现场观察**：不用检定或低 DC 检定就能看到（脚印、血迹、徽记、残留法术）。
2. **NPC/文献**：问人、读信、查档案、审讯俘虏。
3. **检定**：调查、察觉、洞悉、奥秘、历史、生存等，成功给更精确的信息。

检定失败时给“更模糊但还能继续”的信息，而不是直接卡住。线索节点用 `kind: "clue"`，
并通过 `contains` 挂到地点，或用 `reveals` 边标出发现方式。

## 4. 场景设计

每个场景回答四个问题：

- 队伍为什么来？（入口/线索/任务）
- 能发现什么？（线索、物品、NPC、秘密通道）
- 可能遇到什么风险？（战斗、陷阱、环境、社交冲突）
- 离开时能带走什么？（信息、物品、新地点、盟友）

场景之间用 `adjacent`（普通连通）、`portal`（隐藏/条件通道）、`leads_to`（单向离开）连接。
需要钥匙/线索才能进的地方写成 `portal` + `condition`，并确保钥匙真的能拿到。

## 5. NPC 与阵营

| 要素 | 写法 |
| --- | --- |
| 目标 | 一句话说清他想要什么、怕什么 |
| 信息 | 他知道哪些线索（`knows`） |
| 位置 | 常驻地点（`inhabits`） |
| 关系 | 与其他 NPC 的 `relationship`，label 写“雇主/盟友/敌对/主从/亲属” |
| 态度 | 友善、中立、戒备、敌对；给出可被说服/激怒的触发点 |
| 战斗 | 只有可能打起来才写完整数值或数据卡来源 |

NPC 不要只是“发任务的人”：给他一个与主线冲突的私心，玩家才有选择。

## 6. 战斗与数值

- 用 5e 的挑战等级、护甲等级、生命值、豁免、抗性/免疫、动作与附赠动作来组织敌人。
- 敌人要有明确的战术：谁在前排、谁远程、谁控制、什么时候撤退。
- 不要凭记忆编造怪物数值；优先用 `content.search` 查当前规则集的怪物/法术/装备。
- 需要临时数值时，给“数据卡来源 + 调整项”，不要伪造官方数据。
- 每场战斗至少给两种替代方案：谈判、潜行、逃跑、破坏环境、利用线索或消耗资源。

## 7. 法术、魔法物品与休息

- 法术要有施法者、法术位、持续时间、专注与可见效果；仪式与长耗时法术会影响时间线。
- 魔法物品要写稀有度、同调需求与可发现的线索；不要无提示地塞给队伍。
- 短休/长休会影响资源恢复；剧本要说明哪些阶段允许休息，以及休息被打断的后果。

## 8. 结局设计

至少两个结局，每个都要有明确条件：

- **成功结局**：目标达成、威胁解除、队伍生还。
- **失败结局**：目标失败、关键 NPC 死亡、敌人完成计划。
- **妥协结局**：付出代价换胜利，或与某一方达成交易。
- **开放结局**：主线收束但留下后续钩子。

`completion` 条件只使用结构化原子：

| 原子 | 例子 |
| --- | --- |
| 线索 | `{"clue": "ritual_diagram"}` |
| 物品 | `{"item": "silver_key"}` |
| 旗标 | `{"flag": "boss_defeated"}` |
| 访问 | `{"visited": "crypt"}` |
| 检定 | `{"check": {"skill": "察觉", "result": "success"}}` |
| 时钟 | `{"clock": "doom_clock", "gte": 4}` |
| 组合 | `{"all": [...]}` / `{"any": [...]}` / `{"not": {...}}` |

结局节点要能从起点走到；进入结局时才会检查 `completion`。

## 9. 触发器与可见性

| 场景 | binding | 内容 |
| --- | --- | --- |
| 进入房间 | `enter` + `location_id` | 场景描写、气味、声音 |
| 发现线索 | `clue` + `clue_id` | 信件/日记/地图（富文本或图片） |
| NPC 对话 | `npc` + `npc_id` | 一段独白或关键台词 |
| 时钟到阈值 | `clock` + `clock_id` + `gte` | 敌人计划推进、环境恶化 |
| 主持人手动 | `manual` + `keyword` | 战斗开始、陷阱触发 |

触发器内容只写玩家能看到的部分；主持人真相放模块正文或 `visibility: kp_only` 的节点。

## 10. 常见错误

- 只画地点节点，不建 NPC/物品/线索 → 图谱无法回答“东西在哪”。
- 关键线索只有一条获取方式 → 检定失败就卡死。
- 结局没有条件或不可达 → 校验报错，玩家无法收束。
- 战斗数值缺失或过强 → 主持人只能凭感觉裁决。
- 触发器引用不存在的地点/NPC → 导入校验报错。
- 把百分制、理智值、调查员职业等其它规则集概念写进 5e 剧本。
""".strip()

CHARACTER_PROMPT = """
当前规则集是 D&D 5e。建卡前先调用 characters.schema 读取当前规则集的字段与约束，
必要时用 content.search 查职业、种族、法术、装备或怪物；不要套用其它规则集的职业、
属性名或理智值。

角色卡使用公共信封 + sheet 结构，至少填：name、rulesetId=dnd5e、sheet.level、
sheet.class、sheet.race、sheet.abilities（str/dex/con/int/wis/cha）、sheet.hp、
sheet.armorClass、sheet.proficiencies、sheet.equipment；施法者补 sheet.spellcasting。
派生值（熟练加值、先攻、被动察觉、法术豁免 DC 等）由服务端计算，不要手写覆盖。
保存前用 characters.save 生成待确认预览，确认字段完整且没有其它规则集字段。
""".strip()


def character_schema() -> dict:
    return {
        "ruleset_id": "dnd5e",
        "envelope_fields": list(ENVELOPE_FIELDS),
        "sheet_fields": list(SHEET_FIELDS),
        "derived_keys": list(DERIVED_KEYS),
        "sections": list(SECTIONS),
    }


def site_agent_profile() -> dict:
    return {
        "ruleset_id": "dnd5e",
        "display_name": "D&D 5e",
        "tool_names": list(SITE_AGENT_TOOL_NAMES),
        "prompt": SITE_AGENT_PROMPT,
        "guide_sections": {
            "skill": SITE_AGENT_GUIDE,
            "ruleset": SITE_AGENT_GUIDE,
            "dnd5e": SITE_AGENT_GUIDE,
        },
        "character_prompt": CHARACTER_PROMPT,
        "character_schema": character_schema(),
    }
