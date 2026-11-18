"""CoC 7e 插件对网站 Agent 的声明。

工具名、创作口径、建卡提示与角色卡结构都由本插件提供；核心只按当前规则集分派。
"""

from __future__ import annotations

from trpg_server.ruleset.coc7.schema import (
    ATTRIBUTE_KEYS,
    CARD_DERIVED_KEYS,
    DERIVED_KEYS,
    ENVELOPE_FIELDS,
    EXTENSION_SCHEMA_VERSIONS,
    RESOURCE_KEYS,
    SHEET_FIELDS,
    STATUS_GROUPS,
)

SITE_AGENT_TOOL_NAMES: tuple[str, ...] = (
    "dice.roll",
    "dice.roll_coc_check",
    "check.roll_room_check",
    "san.roll_sanity_check",
    "sanity.roll_sanity_check",
    "san.roll_madness",
    "sanity.roll_madness",
    "san.bout",
    "san.habituation",
    "san.reality_check",
    "san.traits",
    "san.treat",
    "coc7.social_check",
    "coc7.combined_check",
    "coc7.group_luck",
    "coc7.contact_check",
    "coc7.economy",
    "knowledge.search_ruleset",
    "combat.start",
    "combat.state",
    "combat.next",
    "combat.attack",
    "combat.shoot",
    "combat.damage",
    "combat.treat",
    "combat.hit_location",
    "combat.knockout",
    "combat.poison",
    "combat.poison_queue",
    "combat.reload",
    "combat.end",
    "magic.cast",
    "magic.grimoire",
    "magic.learn",
    "magic.lookup",
    "magic.status",
    "chase.start",
    "chase.next",
    "chase.move",
    "chase.hazard",
    "chase.conflict",
    "chase.status",
)

SITE_AGENT_PROMPT = """
当前规则集是 CoC 7e（克苏鲁的呼唤第七版）。创作与建卡时遵循百分制技能、理智值（SAN）、
调查员职业、时代背景与调查结构；不要套用 D&D 的职业/等级、AC、法术位等概念。

- 先确认调查主题、时代与地点、玩家人数、调查员入场方式、基调、预计时长、结局数量与内容红线。
- 冒险按“钩子 → 初步调查 → 线索链 → 真相 → 对抗/抉择 → 结局”组织；关键线索至少给三种获取方式。
- 线索要冗余，检定失败给“更模糊但还能继续”的信息；战斗不是唯一解。
- 遇到超自然现象时标注 SAN 损失与心理准备；不要在剧本字段里假装自动结算。
- NPC 要有动机、私心、知道的信息与可被说服/激怒的触发点。
- 输出剧本包时显式声明 ruleset_id=coc7，并通过 scenarios.import_script 生成待确认预览。
""".strip()

SITE_AGENT_GUIDE = """
# CoC 7e 剧本创作规范

这份参考只讲“怎么写一个能跑、好玩、符合 CoC 7e 的调查剧本”，字段定义见 bundle-schema.md。

## 1. 调查剧本的基本结构

```
钩子 → 初步调查 → 线索链 → 真相 → 对抗/抉择 → 结局
```

| 阶段 | 作用 | 常见问题 |
| --- | --- | --- |
| 钩子 | 让调查员有理由介入 | 理由太弱，玩家不想去 |
| 初步调查 | 给 1 个安全入口与 3 条线索 | 一进门就遇怪，没有调查空间 |
| 线索链 | 把玩家从表象引向真相 | 只有一条线，检定失败就卡死 |
| 真相 | KP 掌握的完整事实 | 真相写得像小说，缺少可查证据 |
| 对抗 | 仪式、怪物、NPC 摊牌 | 战斗过强，调查员必死 |
| 结局 | 成功/失败/逃脱/疯狂 | 结局没有条件，随时可进 |

## 2. 线索设计：三重原则

任何推进剧情的关键结论，至少给三种获取方式：

1. 现场观察：不用检定或简单检定就能看到（血迹、脚印、符文）。
2. NPC / 文献：问人、读日记、查剪报。
3. 技能检定：侦查、图书馆使用、心理学、神秘学等，成功给更精确的信息。

检定失败时给“更模糊但还能继续”的信息，而不是直接卡住。
线索节点用 `kind: "clue"`，并通过 `contains` 挂到地点，或用 `reveals` 边标出发现方式。

## 3. 房间（场景）设计

每个房间回答四个问题：

- 玩家为什么来？（入口/线索指向）
- 玩家能在这里发现什么？（物品、线索、NPC）
- 玩家可能遇到什么风险？（战斗、理智、陷阱）
- 玩家离开时能带走什么？（物品、信息、新地点）

房间之间用 `adjacent`（普通连通）、`portal`（隐藏/条件通道）、`leads_to`（单向离开）连接。
需要钥匙/线索才能进的房间写成 `portal` + `condition`，并确保钥匙真的能拿到。

## 4. NPC 设计

| 要素 | 写法 |
| --- | --- |
| 动机 | 一句话说清他想要什么、怕什么 |
| 信息 | 他知道哪些线索（`knows`） |
| 位置 | 常驻地点（`inhabits`） |
| 关系 | 与其他 NPC 的 `relationship`，label 写“父子/敌对/雇佣/主从/恋人” |
| 态度 | 敌对、戒备、友善、疯狂；给出可被说服/激怒的触发点 |
| 战斗 | 只有可能打起来才写完整数值 |

NPC 不要只是“发线索的工具人”：给他一个与真相冲突的私心，玩家才有戏。

## 5. 战斗与数值（CoC 7e）

- HP = (CON + SIZ) / 10（向下取整）；DB 与 build 由 STR + SIZ 查表决定；MOV 人类通常 7–9；护甲是固定减伤。
- 普通人类杂兵 HP 8–11、格斗 40–50；训练有素的敌人 HP 11–14、护甲 0–1、格斗 50–65；
  首领/怪物 HP 14–20、护甲 1–3、格斗 60–75；不可名状之物 HP 20+、护甲 3+、优先给“无法正面对抗”的提示。
- 武器字段示例：`{"name":"砍刀","skill":"格斗（刀剑）","damage":"1D8+DB","range":"接触","attacks":"1","impale":false}`；
  射击武器补 `ammo` 与 `malfunction`。
- 战斗数值写在 NPC 模块的 `battle` 与 `weapons` 里，KP 开战时把模块 `id` 传给 `combat.start` 的 `module_id`。
- 每个战斗场景至少给两种替代方案：谈判、潜行、逃跑、破坏环境、用线索威胁、献祭物品。

## 6. 理智（Sanity）

- 遇到超自然现象给 SAN 损失，例如 `1/1D6`（成功/失败）。
- 关键真相可以给更大的损失，但要提前给玩家心理准备的描写。
- 理智归零或一次性损失过多会进入疯狂；剧本应准备疯狂后的收尾方式。
- 不要在剧本包字段里假装有自动 SAN 结算；把损失写在正文，KP 用骰子工具结算。

## 7. 结局设计

至少两个结局，每个都要有明确条件：

- 成功结局：阻止仪式 / 救出目标 / 封印存在。
- 失败结局：仪式完成 / 关键 NPC 死亡 / 时钟满。
- 逃脱结局：离开现场但真相未解决。
- 疯狂结局：调查员理智崩溃（可作为开放式收尾）。

`completion` 条件只使用结构化原子：线索、物品、旗标、访问、检定、时钟或它们的组合。
结局节点要能从起点走到；进入结局时才会检查 `completion`。

## 8. 触发器使用建议

| 场景 | binding | 内容 |
| --- | --- | --- |
| 进入房间 | `enter` + `location_id` | 场景描写、气味、声音 |
| 发现线索 | `clue` + `clue_id` | 信件/日记/剪报（富文本） |
| NPC 对话 | `npc` + `npc_id` | 一段独白或关键台词 |
| 时钟到阈值 | `clock` + `clock_id` + `gte` | 仪式推进、环境恶化 |
| KP 手动 | `manual` + `keyword` | 仪式开始、战斗开始提示 |

触发器内容只写玩家能看到的部分；KP 真相放模块正文或 `visibility: kp_only` 的节点。

## 9. 常见错误

- 只画地点节点，不建 NPC/物品/线索 → 图谱无法回答“东西在哪”。
- 关键线索只有一条获取方式 → 检定失败就卡死。
- 结局没有条件或不可达 → 校验报错，玩家无法收束。
- 战斗数值缺失或过强 → KP 只能凭感觉裁决。
- 触发器引用不存在的地点/NPC → 导入校验报错。
- 把 DND 的等级、AC、法术位写进 CoC 剧本。
""".strip()

CHARACTER_PROMPT = """
当前规则集是 CoC 7e。建卡前先调用 characters.schema 读取字段与约束，并根据角色描述与剧本要求
生成一张可被角色卡编辑器直接载入的调查员卡；不要套用 D&D 的职业/等级、AC、法术位等概念。

角色卡使用公共信封 + sheet 结构，至少填：name、rulesetId=coc7、sheet.job、sheet.age、
sheet.gender、sheet.attributes（str/dex/con/app/pow/siz/edu/int/luc）、sheet.deriveAttributes、
sheet.battleAttributes、sheet.characterStatus、sheet.pointValues、sheet.proSkills、sheet.skillPoints、
sheet.weapons、sheet.stories、sheet.assets。派生值与资源上限由服务端计算，不要手写覆盖。
保存前用 characters.save 生成待确认预览，确认字段完整且没有其它规则集字段。
""".strip()


def character_schema() -> dict:
    return {
        "ruleset_id": "coc7",
        "envelope_fields": list(ENVELOPE_FIELDS),
        "sheet_fields": list(SHEET_FIELDS),
        "attribute_keys": list(ATTRIBUTE_KEYS),
        "resource_keys": list(RESOURCE_KEYS),
        "derived_keys": list(DERIVED_KEYS),
        "card_derived_keys": list(CARD_DERIVED_KEYS),
        "status_groups": list(STATUS_GROUPS),
        "extension_schema_versions": dict(EXTENSION_SCHEMA_VERSIONS),
    }


def site_agent_profile() -> dict:
    return {
        "ruleset_id": "coc7",
        "display_name": "CoC 7e",
        "tool_names": list(SITE_AGENT_TOOL_NAMES),
        "prompt": SITE_AGENT_PROMPT,
        "guide_sections": {
            "skill": SITE_AGENT_GUIDE,
            "ruleset": SITE_AGENT_GUIDE,
            "coc7": SITE_AGENT_GUIDE,
        },
        "character_prompt": CHARACTER_PROMPT,
        "character_schema": character_schema(),
        "test_scenario": "docs/样本/COC7-测试剧本-裂隙回廊.veil-scenario.json",
    }
