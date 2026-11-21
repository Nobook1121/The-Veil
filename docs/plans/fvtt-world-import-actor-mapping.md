# FVTT dnd5e 5.3.3 角色文档 → 本仓库 dnd5e 角色卡/怪物 statblock 逐字段映射

- 状态：调研定稿（纯文档，不含代码改动）
- 日期：2026-11-21
- 服务对象：`fvtt-world-import` 下一刀「actors → dnd5e 角色卡/怪物 statblock」转换层（`docs/plans/dnd5e-vtt-refactor-plan.md` §5.2 的 actors 分支）
- 铁律：本文所有结构结论都来自**两个真实样例包的全量导出实测**，不凭记忆猜 5e 结构。每个结论标注证据出处（包名 + actor 名 + 文档 id）。

## 0. 数据源与复现方法

| 包 | 路径 | actors 构成 | 内嵌 items | 备注 |
| --- | --- | --- | --- | --- |
| heroes（英雄向远山而行） | `docs/样本/heroes-to-yuanshan-0.1.6.zip` | 23 = character 10 + npc 12 + vehicle 1 | `actors.items` 241 | PC 为主，含职业/种族/背景/专长/法术全量装备 |
| sixian（丝线与缠绵） | `docs/样本/sixianyuchanmian-1.0.1.zip` | 14 = npc 14 | `actors.items` 73 | NPC/怪物为主，含 Spellcasting/Multiattack/法术条目 |

导出命令（读取层已落地，git 351f986）：

```bash
PYTHONPATH=data/plugins/fvtt-world-import python -m fvtt_world_import.dump "docs/样本/heroes-to-yuanshan-0.1.6.zip" .tmp/fvtt-dump/heroes
PYTHONPATH=data/plugins/fvtt-world-import python -m fvtt_world_import.dump "docs/样本/sixianyuchanmian-1.0.1.zip" .tmp/fvtt-dump/sixian
```

本次调研的导出产物在 `.tmp/fvtt-dump/heroes/` 与 `.tmp/fvtt-dump/sixian/`（集合 JSON：`actors.json` / `actors.items.json` / `actors.items.effects.json` / `actors.effects.json` / `folders.json` 等）。两包 `_stats` 均确认 `systemId=dnd5e`、`systemVersion=5.3.3`、`coreVersion=13.351`（heroes 法师 `VrylLHMmHRbEQpLH`、sixian 女死灵术士施法条目 `_stats` 实测）。

精读样本（下文引用代号）：

| 代号 | 包 | actor | type | 用途 |
| --- | --- | --- | --- | --- |
| WIZ | heroes | `VrylLHMmHRbEQpLH` 预设角色：卓尔法师 | character | 法师 PC：slots、class item、法术书 |
| DRU | heroes | `ugX5RNZ7KCYRwbNG` 哈普·希尔·软 | character | 德鲁伊 PC：race item、装备、prepared 法术 |
| ROG | heroes | `SYWoLOLtQXsuAvOy` 预设角色：提夫林盗贼 | character | expertise=2 实证 |
| WAR | heroes | `5tFMDd0ORXpRrk3s` 耐罗·斯莫尔·布莱克 | character | 魔契师：pact slots、subclass item |
| BAR | heroes | `Eg92CJK7PizCnlf7` 预设角色：龙裔野蛮人 | character | flat AC 覆盖实证 |
| PAL | heroes | `LezW8CAX1aOWgm2V` 预设角色：人类圣武士 | character | 半施法者 slots |
| WLF | heroes | `pkuHCnaAa9ENHZpn` 狼 | npc | 基础怪物形态 |
| STA-4 | heroes | `JYTMRQDo7R3A1Otx` 侍女雕像（4人版） | npc | natural 攻击 + senses |
| SWM | heroes | `FiNf35frtp0TMM1z` 丹山市的蜘蛛群 | npc | 多段伤害 parts 实证 |
| NEC | sixian | `sETfZ44cI6rCJhI6` 女死灵术士 | npc | NPC 施法者 + slots 表 |
| VET | sixian | `iIqICziePnxXshCX` 105老兵 | npc | Multiattack + 装甲计算 AC |
| KNT | sixian | `fvvJWfHymbPmfZ39` 教团骑士 | npc | 完整 statblock 映射样例 |
| REV | sixian | `42NXiP9aVVRKxNGX` 卡洛斯·奥尔金（还魂尸） | npc | 高 CR + 装甲计算 AC |
| SKL | sixian | `uAB6j3tlDANUTRrY` 骷髅仆人（骷髅管家） | npc | di/dv/ci 实证 |
| PET | sixian | `xqwKyEma2QAeovty` 梦梦（小蓝） | npc | actor 级 effect（中毒）实证 |
| VEH | heroes | `Uovar6FOzQGQE0sg` 马车 | vehicle | 路由兜底 |

---

## 1. FVTT dnd5e 5.3.3 actor 文档结构总览

### 1.1 存储形态：actor 与内嵌条目是分离集合

LevelDB 键格式实测（`data/plugins/fvtt-world-import/fvtt_world_import/leveldb.py` 读取层已覆盖）：

- actor 文档在 `actors` 集合，顶层字段 `name / type / folder / system / img / prototypeToken / items / effects / sort / ownership / flags / _stats`（WIZ 实测 14 个顶层键）。
- **actor 的 `items` 与 `effects` 字段只是 id 引用列表（字符串数组），不是内嵌对象**：

```json
// WLF（heroes 狼）的 items 字段原文
["z1WFJdHb8OXvW2yy", "GtradZW0Fk8JKMAe", "MuH5aMoKOyZanrFf"]
// 逐项在 actors.items 集合解析后：
//   z1WFJdHb8OXvW2yy -> feat  Keen Hearing and Smell（敏锐听觉和嗅觉）
//   GtradZW0Fk8JKMAe -> feat  Pack Tactics（群体战术）
//   （第三项）        -> weapon Bite（啃咬）
```

- 内嵌条目真身在 `actors.items` 集合，**`_id` 是复合键 `<actorId>.<itemId>`**（heroes 实测首键 `5F8kdU242YIleFsW.Q23ui1eFWP8OJTkE`）。聚合规则：`actor.items` 中的 `<itemId>` → 查 `actors.items` 里前缀为 `<actorId>.` 的文档。
- actor 级效果同理：`actor.effects` 是 effectId 列表（PET 实测 `["dnd5epoisoned000"]`），真身在 `actors.effects` 集合，复合键同构（`xqwKyEma2QAeovty.dnd5epoisoned000`）。
- 条目自身的 effect（武器附魔、法术 buff）在 `actors.items.effects` 集合（heroes 41 条 / sixian 14 条）。
- heroes 内嵌条目类型分布（241 条）：`feat 74 / spell 67 / weapon 29 / loot 16 / consumable 12 / container 10 / equipment 10 / class 10 / tool 5 / background 4 / race 3 / subclass 1`；sixian（73 条）：`spell 26 / weapon 21 / feat 18 / equipment 8`。

**导入器要点**：先按复合键聚合 `actors.items` → `actor.items`，悬空引用（引用了不存在的条目）必须按警告降级不中断。两样例包实测未发现悬空（WLF 三条全部解析成功），但仍要写防御分支。

### 1.2 `type=character` vs `type=npc` 的关键差异（两包全量对照实测）

| 维度 | character（WIZ/DRU/ROG/WAR/BAR/PAL） | npc（WLF/NEC/KNT/…） |
| --- | --- | --- |
| `system.details.cr` | **无此键** | 数值型 CR（0、0.125、0.25、0.5、1、2、3、5、6、8 实测） |
| `system.details.type` | 无（种族走 `details.race` → race item id） | `{value: "beast"/"undead"/…, subtype, swarm, custom}` |
| `system.attributes.hp` | `value` 有、**`max` 可为 `null`**（heroes 4/10 个 PC 为 null——四张真人 PC 全部如此：DRU/耐罗/图伊勒/丹·里恩，运行时由职业骰+体质推导；6 张预设 PC 由作者手填了 max）；**无 `formula`** | `value`+`max`+`formula`（如 `"2d8 + 2"`）齐全 |
| `system.attributes.ac` | `calc:"default", flat:null`（个别 PC 用 flat 覆盖，BAR 实测 `{calc:"flat", flat:15}`） | `flat` 有值（13/15/16/18 实测）或 `calc:"default"` 需从装备算（VET/KNT/REV 实测） |
| `system.attributes.movement` | **只有 `units/hover/ignoredDifficultTerrain`，没有速度数值**（WIZ/ROG 全文 0 处 `"walk"`） | `walk/burrow/climb/fly/swim` 直接数值（字符串！`"40"`） |
| PC 速度来源 | race item 的 `system.movement.walk`（DRU 实测 `"30"`） | — |
| `system.spells` | 仅存在用到的槽位键（WIZ 只有 `spell1`） | `spell0..spell9 + pact` 全键（WLF/NEC 实测） |
| `system.attributes.hd` | PC 无（生命骰在 class item 的 `system.hd`） | `{spent: 0}`（WLF 实测，无其他用途） |
| `system.resources` | `primary/secondary/tertiary`（短休/长休资源池） | `legact/legres/lair`（传奇动作/传奇抗性/巢穴动作） |
| `system.details.race/background` | race/background 内嵌条目的 **id 引用**（DRU：race `fM1HPNyo4p5c8TFv`、background `aL38uvHa6cFWR4la`） | 无（null） |
| `system.tools` | 工具熟练字典（ROG 无、DRU `herb:1`、丹·里恩 `lyre/lute/flute:1`） | 全空（sixian 14 个 NPC 实测） |
| `system.bastion`（5.5 强垒） | 存在但为空壳（WIZ 实测 `name/description` 空串） | 无此键 |

vehicle（VEH 马车）另有一套 `crew/draft/passengers/cargo` 字段，`attributes.hp` 为 `{value: null, mt: null}`，**没有可战斗数值**。

### 1.3 真实 JSON 片段

**PC（WIZ，heroes，character）——`system` 有 12 个顶层键**：`abilities / attributes / details / traits / spells / currency / bonuses / skills / tools / bastion / resources / favorites`。

```jsonc
// WIZ.system 摘录（原文，仅省略无关子键）
{
  "abilities": {
    "int": {"value": 16, "proficient": 1, "max": null,
            "bonuses": {"check": "", "save": ""},
            "check": {"roll": {"min": null, "max": null, "mode": 0}},
            "save":  {"roll": {"min": null, "max": null, "mode": 0}}}
    // str/dex/con/wis/cha 同构；proficient=1 即豁免熟练
  },
  "attributes": {
    "hp":   {"value": 8, "max": 8, "temp": null, "tempmax": 0, "bonuses": {}},
    "ac":   {"calc": "default", "flat": null},
    "init": {"ability": "", "roll": {…}, "bonus": ""},
    "movement": {"units": null, "hover": false, "ignoredDifficultTerrain": []},
    "attunement": {"max": 3},
    "senses": {"ranges": {"blindsight": null, "darkvision": null, "tremorsense": null, "truesight": null},
               "units": null, "special": ""},
    "exhaustion": 0, "concentration": {"ability": "", …},
    "death": {"roll": {…}, "success": 0, "failure": 0, "bonuses": {"save": ""}},
    "inspiration": false, "spellcasting": "int"   // 施法 PC 的施法属性直接挂 actor（WIZ 实测 "int"）；非施法者为 ""
  },
  "details": {"biography": {"value": "…HTML…", "public": ""},
              "alignment": "", "race": null, "background": null,
              "originalClass": "Aur7JTfK3FVc1JGz", "xp": {"value": 0}},
  "traits": {"languages": {"value": ["common", "undercommon", "elvish"], "custom": "", "communication": {}},
             "size": "med",
             "di": {"value": [], "custom": "", "bypasses": []},
             "dr": {…}, "dv": {…}, "dm": {"amount": {}, "bypasses": []}, "ci": {…},
             "weaponProf": {"value": [], "custom": "", "mastery": {"value": [], "bonus": []}},
             "armorProf": {"value": [], "custom": ""}},
  "spells": {"spell1": {"value": 2, "override": 2}},
  "currency": {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0},
  "skills": {
    // 18 键缩写 → {ability, value(0/1/2), bonuses, roll}
    "arc": {"ability": "int", "value": 1, "bonuses": {"check": "", "passive": ""}, "roll": {…}}
  },
  "resources": {"primary":  {"value": 0, "max": 0, "sr": false, "lr": false, "label": ""},
                "secondary": {…同构…}, "tertiary": {…同构…}}
}
```

```jsonc
// DRU（德鲁伊）证明 PC 的 hp.max 可为 null、速度在 race item：
"hp":  {"value": 10, "max": null, "temp": 0, "tempmax": 0, "bonuses": {}}
"movement": {"units": null, "hover": false, "ignoredDifficultTerrain": []}   // 无速度数值
// race item ugX5RNZ7KCYRwbNG.fM1HPNyo4p5c8TFv「人类 (变体) Human (Variant)」：
"system": {"movement": {"walk": "30", "units": "ft", "hover": false, "ignoredDifficultTerrain": []}, …}
```

**NPC（WLF，heroes，npc）**：

```jsonc
// WLF.system 摘录
{
  "abilities": {"str": {"value": 12, "proficient": 0, …}, "dex": {"value": 15, …}, …},
  "attributes": {
    "ac":   {"flat": 13, "calc": "natural", "formula": ""},
    "hp":   {"value": 11, "max": 11, "temp": null, "tempmax": null, "formula": "2d8 + 2"},
    "init": {"ability": "", "bonus": "0", "roll": {…}},
    "movement": {"burrow": "0", "climb": "0", "fly": "0", "swim": "0", "walk": "40",
                 "units": "ft", "hover": false, "ignoredDifficultTerrain": []},
    "spellcasting": "",   // NPC 施法属性直接挂 actor（NEC 实测为 "int"）
    "hd": {"spent": 0}, "exhaustion": 0, "attunement": {"max": 3},
    "senses": {"units": "ft", "special": "",
               "ranges": {"darkvision": 0, "blindsight": 0, "tremorsense": 0, "truesight": 0}},
    "concentration": {…}, "death": {…}
  },
  "details": {"biography": {"value": "…", "public": ""},
              "alignment": "Unaligned",
              "race": null,
              "type": {"value": "beast", "subtype": "", "swarm": "", "custom": ""},
              "cr": 0.25},
  "traits": {"size": "med", "languages": {"value": [], "custom": "", "communication": {}},
             "di": {…}, "dr": {…}, "dv": {…}, "dm": {…}, "ci": {…},
             "important": false},
  "skills": { // 18 键同 PC，value 0/1/2
  },
  "spells": {"spell0": {"value": 0, "override": null}, "spell1": {…}, …, "spell9": {…}, "pact": {…}},
  "resources": {"legact": {"max": 0, "spent": 0},
                "legres": {"max": 0, "spent": 0},
                "lair":   {"value": false, "initiative": null, "inside": false}},
  "currency": {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0},
  "source": {"custom": "", "book": "", "page": "", "license": "CC-BY-4.0", "revision": 1, "rules": "2014"}
}
```

---

## 2. character → 我们的角色卡逐字段映射表

我们的目标结构（`data/plugins/dnd5e/dnd5e_plugin/schema.py`，前端消费形态 `frontend/src/rulesets/dnd5e/character-sheet.ts`）：

- 信封：`id / public_id / publisher_id / publisher_name / avatar / name / playerId / playerName / isEditable / createdAt / updatedAt`
- sheet：`level / class / subclass / race / background / alignment / abilities{str..cha} / proficiencies{saves,skills,expertise,armor,weapons,tools,languages} / hp{current,max,temp} / armorClass / speed(int) / initiative(派生) / hitDice(str) / equipment[{name,quantity,weight,notes}] / features[str] / spellcasting{ability,known[],prepared[],slots{"1":{max,used}}} / notes / xp`
- `pack()` 会把 `maxHp/currentHp/tempHp/armorClass/proficiencyBonus/initiative/passivePerception/spellSaveDC/spellAttackBonus` 镜像到顶层（派生值由我们引擎重算，不需要导入层填）。

置信度图例：**A=直接映射**（键名/语义一一对应，仅做类型归一）· **B=换算**（需要确定性换算规则）· **C=语义近似**（结构不同但可无损落位）· **D=无法映射**（降级文本/丢弃，见 §4）。

### 2.1 信封（envelope）

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `name` | `name` | A | WIZ「预设角色：卓尔法师」原样可用 |
| `img` | `avatar` | B | 值形态两种实测：`worlds/<id>/assets/arcanedesk/<sha256>.jpg`（heroes 19 个 actor，包内资产，走素材区落盘+路径重写）；`systems/dnd5e/tokens/beast/Wolf.webp`（heroes 4 个 actor，**系统资产不在包内**，置空头像+导入警告）；sixian 另有 `worlds/sixianyuchanmian/assets/tokens/mengmeng.jpg` 形态 |
| `_stats.createdTime / modifiedTime` | `createdAt / updatedAt` | B | 毫秒 epoch（WIZ：1790379213305）→ 转 ISO 串 |
| `ownership` | — | D | FVTT 用户权限表（`{default:0, <uid>:3}`），与平台账号体系无关 → 进「归属待指派」流程，不落卡 |
| `folder` | — | C | actor 文件夹 id → `folders` 集合取名字，作为指派批次分组：heroes「英雄向远山而行」文件夹放预设角色，「机核小队」文件夹（q1jv8YGvSux1GZ32）放 4 个真人 PC（耐罗/图伊勒/丹·里恩/哈普）——**folder 分组是「谁在玩哪张卡」的现成提示**，写进导入完成报告 |
| `sort` | — | D | 排序值，全部为 0（实测），丢弃 |
| `prototypeToken` | — | D | 不属于卡数据；棋子摆位由场景 tokens 消费（重构计划 §5.2 已覆盖） |
| `flags` | — | D | 空 dict（实测），丢弃 |

### 2.2 identity

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `items[type=class].system.levels` | `sheet.level` | A | WIZ class item `system.levels: 1`；多职业 PC 取 Σ levels（**样本无多职业，待实测**）；`pack()` 会 clamp 1-20 |
| `items[type=class].name` | `sheet.class` | B | 名称是中文双语「法师 Wizard」——建议取空格前的主名「法师」；也可匹配 `identifier: "wizard"` 对齐我们 `classes.json` 的 id（见 §2.8） |
| `items[type=subclass].name` | `sheet.subclass` | A | WAR（耐罗）实测持有 subclass item「邪魔 The Fiend」（identifier `the-fiend`、classIdentifier `warlock`）；取主名「邪魔」 |
| `items[type=race].name`（或 `details.race` id 引用反查） | `sheet.race` | A | DRU `details.race: "fM1HPNyo4p5c8TFv"` → race item「人类 (变体) Human (Variant)」→「人类 (变体)」；WIZ 无 race item 且 `details.race: null` → 留空 + 警告 |
| `items[type=background].name`（或 `details.background` 引用反查） | `sheet.background` | A | DRU `details.background: "aL38uvHa6cFWR4la"` →「隐士 Hermit」 |
| `details.alignment` | `sheet.alignment` | A | PC 样本多为空串，原样 |
| `details.xp.value` | `sheet.xp` | A | 全部 0（实测），原样 |

### 2.3 六属性

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `system.abilities.<str\|dex\|con\|int\|wis\|cha>.value` | `sheet.abilities.<key>` | A | 键名完全一致（WIZ int=16、ROG dex=16 实测）；`pack()` clamp 1-30，null 缺省 10 |
| `system.abilities.<k>.proficient`（0/1） | `sheet.proficiencies.saves`（存入 `<k>`） | B | ROG 实测 dex/int=1 其余 0 → `saves: ["dex","int"]`。我们 `proficiency_flags` 对 saves 只认熟练、无 expertise 概念，FVTT 也没有存豁免 expertise，零损失 |
| `system.abilities.<k>.bonuses.check / save` | — | D | 掷骰公式串（样本全空串）。非空时 → 警告+notes（**样本无实例，待实测**） |
| `system.abilities.<k>.max` | — | D | 5.5 传奇变体上限，样本全 null，丢弃 |

### 2.4 技能熟练 / expertise / 工具 / 语言

FVTT 技能是 18 个缩写键，`value` 语义实测：**0=不熟练、1=熟练、2=expertise**（ROG `slt=2, ste=2`——盗贼的巧手+隐匿 expertise，教科书配置）。

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `system.skills.<abbr>.value >= 1` | `sheet.proficiencies.skills`（存入技能全名 id） | B | 缩写→全名对照表见下 |
| `system.skills.<abbr>.value == 2` | `sheet.proficiencies.expertise` | B | 同上；我们 `proficiency_flags` 要求 expertise 技能同时出现在 skills 或 expertise 里——**导入时 expertise 技能应同时写入 skills 与 expertise 两个列表** |
| `system.skills.<abbr>.bonuses.check` | — | D | 公式串，样本全空，非空→警告 |
| `system.traits.weaponProf.value` + `custom` | `sheet.proficiencies.weapons` | C | FVTT 用**武器代码**（DRU：`club/dagger/dart/javelin/mace/quarterstaff/sickle/spear/sling/scimitar`）。我们是自由字符串列表 → 按代码→中文名表转换（`club→木棒`…），`custom` 文本直接追加 |
| `system.traits.armorProf.value` + `custom` | `sheet.proficiencies.armor` | C | 代码 `lgt/med/hvl/shl`（DRU 实测 `["lgt","med","shl"]`）→ `轻甲/中甲/重甲/盾牌`；custom 追加 |
| `system.traits.weaponProf.mastery` | — | D | 5.5 武器精通，样本全空，丢弃 |
| `system.tools.<id>.value`（0/1/2） | `sheet.proficiencies.tools` | B | id→名称：`herb→草药师工具包`、`lyre/lute/flute→里拉琴/鲁特琴/长笛`（DRU/丹·里恩实测）；非零即入列表（value=2 工具 expertise 我们 schema 不分，入列表即可，可附「(expertise)」后缀） |
| `system.traits.languages.value` + `custom` | `sheet.proficiencies.languages` | C | 小写代码（WIZ：common/undercommon/elvish；DRU 含 druidic）。我们列表自由字符串 → 代码→中文名表（common→通用语…），custom 文本追加 |
| class/background/race item 的 advancement 定义 | — | — | **不需要导入**：FVTT 已把 advancement 结果落到上面的 `traits/skills/abilities` 字段里，直接读结果即可 |

技能缩写对照（实测每键 `ability` 字段交叉验证，与我们 `rules.py` 的 18 技能一一对应）：

```
acr→acrobatics(体操,dex)  ani→animal-handling(驯兽,wis)  arc→arcana(奥秘,int)
ath→athletics(运动,str)    dec→deception(欺瞒,cha)        his→history(历史,int)
ins→insight(洞悉,wis)      itm→intimidation(威吓,cha)     inv→investigation(调查,int)
med→medicine(医药,wis)     nat→nature(自然,int)           prc→perception(察觉,wis)
prf→performance(表演,cha)  per→persuasion(游说,cha)       rel→religion(宗教,int)
slt→sleight-of-hand(巧手,dex) ste→stealth(隐匿,dex)       sur→survival(求生,wis)
```

### 2.5 HP 与生命骰

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `system.attributes.hp.value` | `sheet.hp.current` | A | WIZ 8、DRU 10 实测 |
| `system.attributes.hp.max` | `sheet.hp.max` | B | **heroes 4/10 个 PC 为 `null`**（DRU/耐罗/图伊勒/丹·里恩四张真人卡实测；预设 PC 全部手填了 max）。换算规则（按优先级）：① `max` 非空 → 直接用；② `max` 为空 → 按职业骰推导 `Σ(levels × hd均值) + con_mod × levels`，并校验与 `value` 一致（DRU：1×d8 平均 8？——实测 value=10 = d8 上限 8 + con2，推导值一致才算成功）；③ 推导失败 → `max = value`（导入即满血）+ 警告「HP max 未导出，按当前值导入」 |
| `system.attributes.hp.temp` | `sheet.hp.temp` | A | null → 0 |
| `system.attributes.hp.tempmax` | — | D | 临时 HP 上限，样本全 0/null → 丢弃；非 0 → notes |
| `system.attributes.hp.bonuses` | — | D | 公式对象（`{}` 实测），丢弃 |
| class item `system.hd.denomination` + `levels` | `sheet.hitDice` | B | WIZ：`hd: {denomination: "d6", spent: 0}`、levels 1 → `"1d6"`。多职业拼 `"2d8+1d6"`（待实测）。`hd.spent`（已耗骰）我们无字段 → 非零时 notes |
| `system.attributes.death` | — | D | 死亡豁免计数（success/failure 全 0）运行态，丢弃 |

### 2.6 AC（难点一：FVTT 存的是算法选择，不是数值）

FVTT 的 AC 字段两种形态（两包 37 个 actor 全量实测）：

- PC：`{calc: "default", flat: null}` ——**没有任何数值**，运行时由已装备护甲重算（WIZ/ROG/DRU…全如此）；
- 例外 PC：BAR `{calc: "flat", flat: 15}` ——作者手填（野蛮人无甲防御）；
- NPC：`{flat: <n>, calc: "natural"}`（WLF flat=13）或 `{flat: null, calc: "default", formula: ""}`（VET/KNT/REV——**NPC 也可能没有 AC 数值，要从护甲算**）。

**导入决策树（按序）：**

1. `ac.flat` 是数字 → 直接用它（`calc` 不再关心）。⚠️ 实测提醒：flat 可能与任何推导不一致（BAR flat=15，而其 16/16/16 属性按无甲防御推导是 16）——**flat 优先、不回退、不告警**，作者意图优先。
2. `ac.flat` 为空 → 从内嵌装备重算：
   - 取 `items[type=equipment]` 中 `system.equipped == true` 的护甲（`type.value ∈ {light, medium, heavy}`）与盾牌（`type.value == "shield"`，每面 +2，可多面相加）；
   - 护甲 AC = `armor.value + (armor.magicalBonus 数字 ? magicalBonus : 0) + 敏捷加值`，敏捷上限按类型：light=不限、medium=+2、heavy=+0（`armor.dex` 字段为 null 表示「按类型规则」，为数字表示显式上限——REV 的「破损的秘银甲」`armor: {value: 17, dex: null}` 且 `type: heavy` → AC 17）；
   - 多件护甲取算出值最高者（样本无多件实例，待实测）；
   - **无任何已装备护甲** → `10 + 敏捷调整值`，并输出警告「按无甲推导」。
3. `calc` 出现样本外取值（`formula` 非空、自定义 calc）→ `10 + dex` + 警告（**样本无实例，待实测**）。

实证：VET（105老兵）`calc=default` + 已装备板条甲 `armor:{value:17, dex:null}`（heavy）→ AC 17；KNT（教团骑士）同构；heroes 塞巴斯蒂安 `{calc:"flat", flat:18}` 与其「板条甲 +1」（`armor:{value:17, dex:0, magicalBonus:"1"}`）吻合（17+0+1=18）——决策树 1、2 两条路径都有真实样本对上。

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `attributes.ac`（按决策树） | `sheet.armorClass` | B | 注意我们的 `_armor_class()` 逻辑：显式 `armorClass` 优先，否则 `10+dex`。**导入层必须把算好的数值写进 `sheet.armorClass`**，否则 pack 后一律变 10+dex |
| 算出 AC 的来源说明 | equipment 行 `notes` | C | 例：`"板条甲（heavy, 17），shield 无"` |

### 2.7 速度与先攻

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| PC：`items[type=race].system.movement.walk` | `sheet.speed` | B | 字符串 `"30"` → int 30（DRU race 实测）。无 race item（WIZ）→ 默认 30 + 警告 |
| `movement.fly/swim/climb/burrow` | — | D | 我们 speed 是单值整数。全部丢弃？**建议降级 notes**：「飞行 60 尺」等（样本中 NPC 才有非零值且全为 `"0"`，PC 无此数据；**PC 特殊速度在 race/feat 文本里，样本无实例**） |
| `movement.hover`、`ignoredDifficultTerrain`、`units` | — | D | 丢弃（units 恒 `ft`/null） |
| `attributes.init.ability` | — | D | 非空表示用其他属性算先攻；两包 23+14 个 actor 全部空串 → 待实测；非空 → 警告 + notes |
| `attributes.init.bonus` | — | D | 公式串，实测全 `""` 或 `"0"`；非 `"0"` 的非空串 → 警告 + notes（我们 initiative 是派生只读值 = dex mod） |

### 2.8 法术位与施法（难点二：FVTT 只存「剩余」，不存「上限」）

`system.spells` 的键：`spell0..spell9` + `pact`，每键 `{value, override}`（PC 只出现用到的键）。

**语义实测**（关键交叉验证）：

- WIZ（法师 L1）：`spell1: {value: 2, override: 2}`；我们 `classes.json` wizard `slot_table[0] = [2,0,…]` → L1=2 槽 ✓；
- NEC（女死灵术士，9 级施法者 NPC）：`spell1..5 = {4,3,3,3,1}`、override 全 null；我们 wizard `slot_table[8] = [4,3,3,3,1,0,0,0,0]` → 9 级施法者表**逐位吻合** ✓；
- PAL（圣武士 L2）：`spell1: {value: 2, override: 2}` ✓（半施法者 2 级=2 槽）；
- WAR（魔契师）：`pact: {value: 1}`（1 级魔契师 1 个契约槽）且 `spell1..9` 全 0；
- ROG（非施法者）：`spell1..9 + pact` 全 0。

结论：**`value` = 当前剩余槽位；`max` 不落盘**——PC 由职业升级表运行时推导，NPC 由作者手填 value（无 class item 可推）。`override` = 对推导 max 的手工覆盖。

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `attributes.spellcasting` 或 class item `spellcasting.ability` | `sheet.spellcasting.ability` | A | 实测：**施法 PC 的 actor 字段本身就有值**（WIZ `"int"`、DRU `"wis"`、PAL `"cha"`、WAR `"cha"`），class item 同值冗余；非施法者 actor 字段为 `""`。取值顺序：**actor 字段优先，class item 兜底**；都空则非施法者 |
| class item `spellcasting.progression`（实测 `full`/`half`/`pact`：WIZ full、PAL half、WAR pact） | 施法者判定 | A | 决定是否启用 slot 表；我们 `classes.json` 只有 wizard/cleric 两职业的表，**其余职业靠 FVTT 的 value/override 兜底**（下条） |
| `system.spells.spellN.value / override` | `sheet.spellcasting.slots["N"] = {max, used}` | B | 换算：`max = override ?? 我们slot表[class][level] ?? value`；`used = max - value`（负值 clamp 0）。**无我们表的职业（bard/paladin/warlock 等）用 `value` 当 max**（导入即满槽）+ 警告。不存在的键跳过 |
| `system.spells.pact` | — | C | 魔契师槽。我们引擎 `spell_slots_from_sheet` 只认 1..9 键。建议：按魔契师等级折算环阶（L1-4→1环…）写入对应 slots 键 + notes 标注「契约魔法，短休恢复」；**样本仅 WAR 一例，待实测** |
| `items[type=spell, prepared=0]` | `sheet.spellcasting.known`（法术名列表） | B | WIZ 10 个法术全部 prepared=0（法师法术书，运行时自选）；入 known |
| `items[type=spell, prepared=1]` | `sheet.spellcasting.prepared` | B | DRU 牧师系实测（sixian NPC 26 个法术全部 prepared=1；heroes 瑟拉菲娜的 Bless/Light 等也=1）。**同一条目只进一个列表** |
| `items[type=spell].method`（`spell`/`pact`） | — | C | `pact` 标记魔契术法术（heroes 4 条：魔能爆/脆弱诅咒等），法术名后缀或 notes 区分 |
| `items[type=spell].identifier` | SRD 对齐线索 | C | `magic-missile/mage-armor/…` 与我们 `spells.json` id 同源（WIZ 10 条 identifier 实测全命中我们 SRD id 命名规范，但 SRD 只有 11 个法术，**深度对齐留待 kiwee 层**）。导入时把 identifier 存 notes 或扩展字段备查 |
| class item `spellcasting.preparation.formula`（`"@abilities.int.mod + @classes.wizard.levels"`） | — | D | 公式求值引擎我们不做；prepared 列表直接来自 `prepared=1` 的法术条目，不需要它 |
| `items[type=spell].uses`（`{max, spent, recovery}`） | — | D | 限量法术（非槽位）样本全空；待实测 |

### 2.9 装备、货币与 attunement

我们的 equipment 行是 `{name, quantity, weight, notes}`（`character-sheet.ts` 实测）。

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `items[type=weapon|equipment|consumable|loot|tool|container].name` | `equipment[].name` | B | 双语名取主名。类型映射：weapon→武器、equipment→护甲/穿戴、consumable→消耗品、loot→杂物、tool→工具、container→容器（容器展开成行，见下） |
| `system.quantity` | `equipment[].quantity` | A | |
| `system.weight.value`（+`units`） | `equipment[].weight` | A | 磅数数字（DRU 布甲 8） |
| `system.equipped / attuned / rarity / type.baseItem / armor.value` | `equipment[].notes` | C | 拼一行摘述：如 `"已装备；轻甲(11)；baseItem:padded；白色消耗品…"`。**已装备标记必须保留在 notes**，因为 AC 计算要在导入时先做掉（§2.6），卡面只剩展示 |
| `system.currency`（pp/gp/ep/sp/cp） | 一条 equipment 行 | C | 我们无货币字段。建议合成一行：`{name: "货币", quantity: 1, weight: 0, notes: "29 gp"}`（heroes 5 个预设 PC 有 gp 15-30，实测非零） |
| `system.attunement`（需求描述串）、`attributes.attunement.max` | — | C | attunement 需求 → notes；`max:3` 默认值 → 丢弃。已 attuned 的物品在行 notes 标「已同调」。我们无 attunement 机制（语义有损，见 §4） |
| `system.container`（父容器 id） | — | C | 容器嵌套（DRU 的 Backpack）→ 全部平铺 + notes 标「在 背包 内」。**禁止丢弃内容** |
| `system.price` | — | D | 无货币字段承载 → 并入该行 notes（`"5 gp"`） |
| item `img` | — | D | equipment 行无图字段，丢弃 |
| `items[type=spell]` | 不进 equipment | — | 进 §2.8 的 known/prepared |
| `items[type=class|race|background|subclass|feat]` | 不进 equipment | — | 进 identity/features |

### 2.10 特性 / 专长（items 里 type=feat）

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `items[type=feat].name` | `sheet.features`（追加） | A | 我们是平铺字符串列表。heroes feat 的 `type.value` 分五类（class 23/race 16/monster 6/feat 5/'' 24，实测计数）——**分组语义丢失**：建议按 type 加前缀？否——保持纯名称（AI DM 可读名称即可），type 信息不必保留 |
| `items[type=feat].system.type.value`（`class/race/feat/monster/''`） | — | C | 丢弃分组；如需保留建议写入 notes 汇总（可选） |
| `items[type=feat].system.identifier` | — | C | `multiattack/spellcasting/parry/bardic-inspiration` 等机器名（heroes+sixian 实测分布）；NPC 侧有用（§3.3），PC 侧丢弃或 notes |
| 占位型 feat（「升至 2 级（届时更新）」「原卡动作备注」） | `sheet.features` | A | 保留原文（heroes 多个 PC 实测存在，是作者写给玩家的备注，不删） |

### 2.11 notes 与杂项

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `details.biography.value`（HTML） | `sheet.notes` | C | HTML→纯文本/markdown 转换；`public` 分栏样本全空串 |
| `details.ideal/bond/flaw/trait/appearance/eyes/hair/height/weight/gender/skin/age/faith` | 追加进 `sheet.notes` | C | 我们 schema 只有 alignment 一个叙事字段；全部拼进 notes（有值的才拼） |
| `attributes.inspiration`（bool） | notes | C | 「激励 true/false」一行（样本全 false） |
| `attributes.exhaustion`（int） | notes | C | 非 0 时「力竭 N 级」（样本全 0；我们 conditions 有 exhaustion 概念，可后续进房间态，导入先落 notes） |
| `resources.primary/secondary/tertiary`（value/max/sr/lr/label） | notes | C | 有限资源池我们无对应（schema 只声明 hp 槽）。有 label/max 的池（样本全空 label）→ 拼一行 |
| `traits.di/dr/dv/ci`（PC 侧） | notes | C | 样本 PC 全空；非空时拼「伤害免疫：…」 |
| `bonuses.*`（mwak/rwak/msak/rsak/abilities/spell） | — | D | 全部公式串、样本全空 → 非空告警（待实测） |
| `system.favorites` / `bastion` | — | D | 空壳/无对应，丢弃 |

### 2.12 效果与条件

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| actor 级 effect（`effects` id 列表 → `actors.effects` 集合）`.statuses` | 条件名（我们 `conditions.json` 的 id） | B | PET（梦梦）实测：`statuses: ["poisoned"]`、name「中毒」——与我们 conditions.json 的 `poisoned` **直接同 id**。静态卡不带运行态：导入为卡 notes「当前状态：中毒」，不预挂战斗条件 |
| 条目级 effect（`actors.items.effects`，transfer buff） | — | D | Shillelagh/Barkskin 等施法时产生的临时 buff（`changes` 是 FVTT 数据路径的 AE 模式补丁，如 `{key:"system.damage.base.denomination", mode:5, value:"8"}`）→ **导入层直接丢弃**，房间内由我们 effects/conditions 引擎接管 |

---

## 3. npc → 怪物 statblock 逐字段映射表

我们的目标有两个消费面：

1. **战斗数值基线**——`statblock.py combatant_from_module()` 的剧本 NPC 模块字段（`abilities / armorClass / hp{current,max} / attacks[{id,name,kind,attack_bonus,damage_bonus,damage_dice,damage_type,versatile_dice,properties}] / speed_ft / creature_type / size / cr / xp / damage_resistances / damage_vulnerabilities / damage_immunities / side`，缺一不可否则显式失败）；
2. **展示/AI 参考卡**——`dnd5e-srd/packs/monsters.json` 条目形态（`name/size/type/subtype/alignment/ac[{value,note}]/hp{average,formula}/speed{walk}/abilities/saving_throws/skills/senses/languages/cr/xp/traits[]/actions[]/reactions[]/legendary_actions[]/spellcasting/locales/source`）。

NPC 的熟练加值：FVTT **不存**，由 CR 推导。推导公式 `prof = 2 + (max(1, ceil(CR)) - 1) // 4`，两包实测全部对上：
- NEC：CR 6 → prof 3；int 17(+3) → 法术 DC 8+3+3=14、法术攻击 +6——与其 Spellcasting 条目散文「法术豁免 DC 14，法术攻击 +6」**逐字吻合** ✓；
- 瑟拉菲娜：CR 0.25 → prof 2；wis 14(+2) → DC 12、攻击 +4，与散文「spell save DC 12, +4 to hit」吻合 ✓。

### 3.1 数值基线字段

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `name` | `name` | A | |
| `img` | 头像/token 图 | B | 同 §2.1 策略 |
| `traits.size`（`sm/med/lg`，另有 `tiny/hug/gar`） | `size` | B | 换算表：`tiny→tiny, sm→small, med→medium, lg→large, hug→huge, gar→gargantuan`（两包实测 sm 1/med 20/lg 2） |
| `details.type.value/subtype` | `creature_type / subtype` | A | `beast/undead/humanoid/construct` 直取；我们战斗侧只吃 `creature_type` 字符串 |
| `details.type.swarm`（`""` 或群尺寸代码） | — | C | SWM 是 `type.value:"beast"` 但名称含「群」；swarm 非空 → 在 subtype 后拼 `（swarm）`（**样本 swarm 字段全空串，群语义只在名称里，待实测**） |
| `details.alignment` | `alignment` | A | `Unaligned/Lawful Evil/Any Alignment` 原样（我们 monsters 也是英文串） |
| `attributes.ac` | `armorClass`（模块）/ `ac[{value,note}]`（pack 条目） | B | 决策树同 §2.6；NPC 的 `calc:"natural"` + flat 是主流（WLF 13）；`calc:"default"` 的 NPC 从护甲算（VET 17/KNT 18/REV 17）；note 写来源（`natural` 或护甲名） |
| `attributes.hp.max / value` | `maxHp / hp`（模块）/ `hp{average, formula}`（pack 条目） | A | NEC 40/40、KNT 52/52、PET 4/4 实测齐全；`average=max`、`formula` 原样保留（`"2d8 + 2"`、`"13d10+40"`） |
| `attributes.hp.formula` | `hitDice` 的怪物版 | A | 我们 statblock combatant 无 hitDice 字段，只进 pack 条目的 `hp.formula` |
| `attributes.movement.walk` | `speed_ft`（模块）/ `speed.walk`（pack） | B | 字符串→int（全 `"30"`/`"40"` 实测；`units:"ft"`） |
| `attributes.movement.fly/swim/climb/burrow` | — | C | 我们 speed 只有 walk。pack 条目 `speed` 建议扩展 `fly/swim` 键（schema 是我们的，additive 不破坏消费方）或降级 traits 文本；样本非零值不存在（全 `"0"`），**待实测** |
| `abilities.<k>.value` | `abilities.<k>` | A | 六键同名（KNT str16/dex11/con14/int11/wis11/cha15 实测） |
| `abilities.<k>.proficient`（0/1） | pack 条目 `saving_throws{<k>: mod+prof}` | B | WLF 全 0 ✓（SRD 狼无豁免熟练）；SKL、NEC 等同理。**注意我们 `saving_throws` 是「加值」不是「熟练位」** |
| `skills.<abbr>.value`（0/1/2） | pack 条目 `skills{<skill_id>: mod+prof×value}` | B | 缩写表同 §2.4。NEC arc/his=1 → `{arcana: +5, history: +5}`（int3+prof3）；我们 monsters.json goblin `{stealth: 6}` 同构 ✓ |
| `attributes.senses.ranges`（darkvision/blindsight/tremorsense/truesight，**数值**） | `senses.darkvision_ft` + 建议扩展 | B | PC 侧是 null，NPC 是数字（SKL/REV darkvision 60、STA-4 blindsight 60+special「Blind beyond this radius」）。我们 pack 只有 `darkvision_ft`+`passive_perception` 两键 → 建议扩展 `blindsight_ft/tremorsense_ft/truesight_ft/special`（无消费方破坏）；不扩展则降级 traits 文本 |
| （无） | `senses.passive_perception` | B | FVTT 不存，推导 `10 + 察觉加值`（ability wis + prof×skills.prc.value）。NEC：wis10+0 → 10 |
| `traits.languages.value + custom` | `languages` | C | 同 §2.4；NEC `value:[] + custom:"any four languages"` → `["any four languages"]` |
| `details.cr`（数值 0.125/0.25/…） | `cr`（字符串）+ `xp` | B | `0.25 → "1/4"`、`6 → "6"`。**FVTT 不存 xp**，按 DMG 表推导：0→10(或0)、1/8→25、1/4→50、1/2→100、1→200、2→450、3→700、4→1100、5→1800、6→2300、7→2900、8→3900（两包 CR 范围 0-8 全覆盖） |
| `traits.di/dr/dv`（`value[] + bypasses[] + custom`） | `damage_immunities/resistances/vulnerabilities` | B | SKL 实测：`di:[poison], dv:[bludgeoning]` → 直取；custom 追加；bypasses（「非魔法攻击」豁免条件）→ 括注进对应条目文本（`"非魔法的钝击"`），样本全空 |
| `traits.ci` | — | C | **我们 monsters.json 没有 condition_immunities 字段**（SKL `ci:[poisoned, exhaustion]` 实证缺口）→ 建议 additive 扩展；否则降级 traits 文本 |
| `traits.important`、`details.habitat/treasure` | — | D | 5.3.3 新增装饰字段，丢弃 |

### 3.2 动作与攻击（难点三：伤害有双源）

NPC 动作在 FVTT 里是内嵌条目，不是 actor 字段：

- **攻击 = `items[type=weapon]`**，`type.value ∈ {simpleM, martialM, simpleR, martialR, natural}`（两包实测：natural 7、simpleM 20、martialM 15、simpleR 5、martialR 3）；
- **非攻击动作/特性 = `items[type=feat]`**（multiattack/parry/leadership 等散文）。

武器伤害**有两个存放点，必须都读**（SWM 实证）：

```jsonc
// 形态甲：item 级 base（KNT 巨剑）
"damage": {"base": {"number": 2, "denomination": 6, "bonus": "", "types": ["slashing"]}}
// 形态乙：activity 级 parts（SWM 蜘蛛群啃咬——base 为空壳，真数据在 activity！）
"damage": {"base": {"types": [], "custom": {"enabled": false}, "scaling": {"number": 1}}}
"activities": {"biteAttack": {"type": "attack",
  "attack": {"ability": "dex", "flat": false, "type": {"value": "melee", "classification": "weapon"}},
  "damage": {"parts": [
    {"number": 2, "denomination": 4, "types": ["piercing"]},
    {"number": 2, "denomination": 4, "types": ["poison"]}]}}}
```

**攻击行组装规则（可执行）：**

1. 遍历 `items[type=weapon]`；`kind` = `attack.type.value == "ranged"` → `"ranged"` 否则 `"melee"`（STA-4 猛击 melee、VET 重弩 ranged 实测）；
2. `damage_dice`：优先 `activities[type=attack].damage.parts[0]`（无 parts 则用 `damage.base`）→ `"{number}d{denomination}"`，`bonus` 非空且为纯数字 → 拼进 `damage_bonus`，公式串 → 警告；
3. `damage_type` = 该 part `types[0]`；**多段 parts（SWM 穿刺+毒素）只映射第一段**，其余段拼 `properties` 标签 `on-hit:2d4[piercing]` 并出警告（我们攻击行单伤害结构，语义有损，§4）；
4. `attack_bonus` = `ability_mod(attack.ability) + prof(CR)`（STA-4 猛击 str、SWM dex 实测），`attack.bonus` 非空纯数字则相加；`attack.flat==true` 表示「直接给值」→ 用 `attack.bonus` 全量替换并停止推导（**样本无 flat=true 实例，待实测**）；
5. `versatile_dice`：`damage.versatile` 非空 → `"{number}d{denomination}"`（heroes 塞巴斯蒂安火焰长剑 `properties:['ver','gear']` 实测有 ver 属性）；
6. 近战触及：`activities[type=attack].range.value`（STA-4 猛击 `"5"` ft）→ pack 条目 `reach_ft`；远程：item `range.value/long`（DRU 投石索 30/120）→ `range{normal_ft, long_ft}`；
7. 组装不出来的武器 → 降级 trait 文本（不丢名字）；
8. 一个 NPC 无任何可组装攻击 → `DEFAULT_UNARMED`（我们 statblock.py 兜底）+ 警告（PET 梦梦只有 1 个 item，实测无武器 → 走此分支）。

pack 条目 `actions[]` 行（对齐我们 monsters.json goblin 形态）：

```jsonc
{"name": "啃咬", "type": "melee_weapon", "attack_bonus": 4, "reach_ft": 5,
 "damage": [{"dice": "2d4", "type": "piercing"}], "text": "…"}
```

`text` 由武器描述+活动描述拼装（HTML→文本），`type` 取 `melee_weapon/ranged_weapon`。

### 3.3 动作经济：Multiattack / Spellcasting / Legendary

| FVTT 形态 | 识别规则 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- | --- |
| Multiattack | feat + `system.identifier == "multiattack"`（sixian 6 条、heroes 2 条实测）；内容是散文 + `[[/item <itemId>]]{武器名}` 内链（VET：`"<p>The veteran makes two [[/item .WaEunPCFG0EddNTt]]{longsword} attacks…</p>"`） | pack 条目 `actions[]` 追加一条 `{name: "多重攻击", type: "multiattack", text: …}`；内链 `[[/item .id]]{label}` → 保留 label 文本 | C | 我们的 statblock.py 不认识 multiattack 类型（`_monster_attacks` 只挑 weapon/melee/ranged 类）→ **文本进 actions，战斗侧由 AI DM 读文本执行**，不生成攻击行 |
| Spellcasting（NPC 施法） | feat + `identifier == "spellcasting"` + `type.value:"monster"`（heroes 瑟拉菲娜英文条目、NEC「施法」中文条目，**两包都有、名字语言不同，识别靠 identifier**） | 见 §3.4 | C | |
| Parry/Leadership/Brave 等反应与特性 | feat + 其他 identifier（`parry/leadership/brave/sneak-attack/assassinate/evasion/cunning-action` 实测清单） | pack `reactions[]`（parry 类）/`traits[]`（其余）按语义分桶：**简化规则**：`identifier=="parry"` → reactions，其余全进 traits | C | 我们没有反应动作消费引擎，纯展示 |
| 传奇动作 | `system.resources.legact/legres/lair`（`{max, spent}` / `{value, initiative, inside}`） | pack 条目 `legendary_actions[]` + 模块侧字段 | — | **两包 25 个 NPC 全部 max=0/value=false（实测），零实例**。结构在（键已实测存在），按结构写好转换分支但标注待实测：`legact.max` → 传奇动作条数、`legres.max` → 传奇抗性次数；条目本体应是 feat items（识别约定待有样本后补） |
| 巢穴动作 | `resources.lair` | — | — | 同上，无实例 |

### 3.4 NPC 法术（难点四：施法数值是散文）

| FVTT 路径 | 我们的路由 | 置信度 | 备注 |
| --- | --- | --- | --- |
| `attributes.spellcasting`（`"int"/"wis"/…`） | 施法属性 | A | sixian 仅两人非空：孔中尉 `"wis"`（CR 2）、NEC `"int"`（CR 6），其余 12 个 `""` |
| `system.spells.spellN.value` | `slots` | B | NPC 无 class item → max 不可推导，**`max=value`（导入即满槽）**+ 警告。NEC spell1..5={4,3,3,3,1} 与 9 级全施法表吻合（§2.8 已交叉验证），但那是作者手工填的 value，不是系统推导 |
| Spellcasting feat `description`（「9 级施法者…DC 14…+6」） | — | C | **DC/攻击加值必须重算**：`DC = 8 + prof(CR) + ability_mod`、`attack = prof + ability_mod`（NEC/瑟拉菲娜两条实测全部吻合，§3 开头）；散文里的施法者等级（9 级）只可作 slots 校验参考 |
| `items[type=spell]`（NPC 全部 `prepared=1`、`method="spell"`，sixian 26 条实测） | 法术清单（名称+环阶+identifier） | A | NEC 挂 18 个法术、施法 feat 散文用 `[[/item .id]]{名}` 引用它们——**清单直接扫 items 更可靠，不要解析散文内链** |
| （我们侧缺口） | statblock combatant **没有 spellcasting 数值字段** | — | `combatant_from_module` 产出的战斗者无 spellSaveDC/slots；NPC 施法在战斗中要可用，需要模块附带 `statblock: {spellcasting: …}` 扩展或 combatant 字段扩展——**这是我们缺 FVTT 有的字段**（见 §5），下一刀需要小规模扩展 statblock.py 或接受「NPC 法术降级为 AI DM 读文本」 |

---

## 4. 无法映射 / 语义有损字段清单（降级策略）

按「降级为文本说明」策略整理（目标全部进 `notes`（PC）或 `traits[]`（NPC），带 `[导入降级]` 标记由导入报告汇总计数）：

| # | FVTT 字段 | 损失原因 | 降级动作 |
| --- | --- | --- | --- |
| 1 | PC `attributes.hp.max == null`（heroes 4/10 PC 实测，四张真人卡全部如此） | FVTT 运行时推导不落盘 | 按职业骰推导；失败则 `max=value` + 警告 |
| 2 | `attributes.ac.calc=="default"` 无 flat | 同上 | 装备重算（§2.6 决策树）；无装备 → 10+dex + 警告 |
| 3 | 公式串字段（`bonuses.*`、`init.bonus`、`ac.formula`、`abilities.*.bonuses`、`preparation.formula`、`attack.bonus` 非数字） | 我们无 FVTT 公式求值引擎 | 样本几乎全空；非空 → notes 原文 + 警告。**不实现公式求值是明确取舍**（避免把 FVTT 的 `@abilities.int.mod` 变量体系搬进来） |
| 4 | `resources.primary/secondary/tertiary` 有限资源池 | 我们 schema 只声明 hp 资源槽 | notes 拼行；后续若 schema 加资源槽可升级 |
| 5 | `currency` 五币 | 无货币字段 | 合成一条 equipment 行「货币」（预设 PC 有 15-30 gp 实测） |
| 6 | attunement 机制（`attributes.attunement.max`、item `attunement/attuned`） | 无同调概念 | notes 标「需同调/已同调」 |
| 7 | `attributes.inspiration/exhaustion/death/concentration` | 运行态/无字段 | notes；exhaustion>0 也可走条件引擎（后续） |
| 8 | PC `traits.di/dr/dv/ci` | 无对应（样本 PC 全空） | notes 拼行 |
| 9 | 多段伤害 parts（SWM 啃咬 2d4 穿刺+2d4 毒素） | 我们攻击行单伤害 | 首段入行，其余 `on-hit:` properties 标签 + 警告（建议下一刀考虑给攻击行加 `damage_extra`） |
| 10 | NPC `traits.ci`（条件免疫） | 我们 monsters.json 无此键 | additive 扩展 `condition_immunities` 或 traits 文本 |
| 11 | `details.biography` 双栏（value/public） | 单 notes 字段 | 拼接（public 非空时加分隔） |
| 12 | `bastion`、`favorites`、`traits.important`、`details.habitat/treasure`、`system.source`（NPC 出处装饰） | 无对应概念 | 丢弃（source 的 license/rules 字段可进导入报告元数据） |
| 13 | item 级 effects（transfer buff，`changes` AE 补丁） | 运行态 buff，语义依赖 FVTT 数据路径 | 直接丢弃（§2.12），房间内由我们效果引擎重建 |
| 14 | `[[/item <id>]]` / `@UUID[...]` / `@Embed[...]` 内链 | 我们的引用体系不同 | 提取 label 文本保留；**不尝试解析成我们的引用**（fvtt-world-import 只做文本降级，对齐 §5.5「文档 id 引用降级纯文本」决策） |
| 15 | spell `uses`（限量施法 innate，3/day 类） | 样本全空 | 待实测；出现时降级 notes |
| 16 | vehicle actor | 无坐骑/载具概念 | 整卡降级为剧本物件文本（VEH 马车无战斗数值实测） |
| 17 | `prototypeToken`、token 图 `systems/dnd5e/...` 引用 | 系统资产不在包内（WLF 实测引用系统内置图） | 棋子图走素材区缺省占位 + 警告 |

---

## 5. 怪物 statblock 专项：双向缺口对照

### 5.1 FVTT 有、我们 statblock 链路缺（下一刀要么扩展、要么明确降级）

1. **NPC 施法数值**：FVTT 有施法属性、槽位、法术条目、Spellcasting 特性；我们 `combatant_from_module` 产出的战斗者**没有 spellcasting 字段**（`attacks/abilities/AC/HP/speed` 之外全丢）。NPC 施法者（NEC CR 6、孔中尉 CR 2、瑟拉菲娜 CR 0.25 都是实战会遇到的）要么扩展 statblock 模块约定（建议：模块 `statblock.spellcasting = {ability, dc, attack_bonus, slots, spells[]}`，spells.py 的 `prepare_spellcasting` 已支持从 sheet 读 slots——打通点在把 NPC 模块字段折叠成 combatant.spell_slots），要么明确「AI DM 读散文施法」降级。
2. **多段伤害**（§4 #9）。
3. **condition_immunities**（§4 #10，SKL 实证）。
4. **senses 的 blindsight/tremorsense/truesight/special**（STA-4「60 尺盲视，超出即盲」实测——这是战斗语义信息，盲视影响隐匿判定，AI DM 需要知道）。
5. **swarm 标记**（SWM 群聚语义：对群只能近战/针对性法术；样本仅名称体现）。
6. **bypasses**（伤害抗性的「非魔法攻击」豁免条件；样本全空但字段存在）。
7. **legendary_actions/legres/lair**（结构双方都有——我们 monsters.json 有 `legendary_actions[]` 键，FVTT 有 `resources.legact/legres/lair`——但**两包零实例**，转换分支写好但无法实测验收）。

### 5.2 我们有、FVTT 缺（导入时补齐）

1. `xp` ——由 CR 表推导（§3.1）；
2. `passive_perception` ——由 wis+察觉熟练推导（§3.1）；
3. `saving_throws/skills` 的**总加值** ——FVTT 只存熟练位，我们存加值，按 `mod+prof(CR)` 重算（§3 开头双实证）；
4. `ac.note` 的可读来源文本——由装备名/calc 拼装；
5. `hp.average`——取 `max`（FVTT 只有 formula+max，average 本就是公式期望值）。

### 5.3 可行性结论

- **数值基线映射可行性高**：六属性/AC/HP/速度/CR/伤害清单全部有确定换算（KNT 教团骑士全字段走一遍无断点：CR3、AC 由板甲=18、HP 52/8d8+16、巨剑 2d6 砍击 attack_bonus=3+2=5、multiattack 文本入 actions）。
- **强依赖「装备重算 AC」小引擎**（§2.6 决策树）——这是 actors 导入里唯一需要实现的「规则计算」，其余全是搬运/换算。
- **NPC 施法是最大空洞**（§5.1 #1），建议下一刀把「statblock 模块带 spellcasting」列为 dnd5e 插件的小改动项（statblock.py 已有 `statblock` 覆盖通道，加一个键不破坏现有测试）。

---

## 6. 导入类型路由规则建议

| FVTT 文档 | 路由目标 | 处理 | 证据 |
| --- | --- | --- | --- |
| actor `type=character` | **dnd5e 角色卡**（§2 全表） | 生成角色卡 + 进「归属待指派」清单；`folder` 名称作为批次分组提示；`预设角色：` 前缀的卡（heroes 6 张）默认不指派、开放玩家自选 | heroes 10 张 PC 分布在 2 个 folder |
| actor `type=npc` | **怪物 statblock 双产物** | ① 剧本 NPC 模块（statblock 数值覆盖形态，供 encounter/combat 工具直接开战）；②（可选入库）monsters.json 形态的参考卡供 AI DM 引用；同名去重（六个「多重攻击」id 不同名同，按 actor 隔离） | sixian 14 个全部 npc；folder 按地点分组（酒馆/庄园/墓穴），模块 `title` 建议带地点前缀辅助 AI DM 定位 |
| actor `type=vehicle` | **降级为剧本物件文本** | 不进战斗链路；输出说明条目 + 警告 | VEH 马车无战斗数值（`attributes.hp.value=null`） |
| 世界级 `items` 集合（无 actor 归属） | **内容库物品 / 剧本条目** | heroes 9 条是真实道具（弹珠/风车/布头娃娃——「孩子们的手工礼物」folder），进物品目录；**sixian 12 条是奥法导出工件**：名为 Spells/Conditions/Magic Items 的 weapon 空壳——按「weapon 类型 + 集合名词汇表 + 空 description」过滤掉，不进玩家可见目录 | 两包 items.json 对比实测 |
| `folders`（type=Actor） | 路由分组元数据 | 不单独落库，作为指派批次/地点前缀 | §6 上面两条 |
| journal/scenes/tables | 非本文范围 | 见重构计划 §5.2/§5.3 | — |

路由优先级建议：先 `type` 字段，再按 `details.cr` 有无兜底（防未来包出现 `type=character` 但填了 CR 的混合形态——两包无此实例，防御分支即可）。

---

## 7. 待实测清单（样本中不存在、写了分支但无法验收）

| # | 项 | 风险 | 验收方式 |
| --- | --- | --- | --- |
| 1 | 多职业 PC（多个 class item） | level/hitDice/spell 槽合成规则 | 找多职业世界包或手工构造 |
| 2 | pact magic 槽位映射（WAR 仅 1 例 L1 魔契师） | 槽位环阶折算、短休恢复语义 | 用更高等级魔契师包验收 |
| 3 | legendary/lair（`resources.legact/legres/lair` 全零） | 传奇动作条目识别约定（feat identifier？） | 用含传奇怪物的包（如 SRD 龙类）验收 |
| 4 | innate spellcasting（spell `method` 只有 `spell/pact`，无 `atwill/innate`） | 限量施法的 uses 映射 | 同上 |
| 5 | `details.cr` 为字符串（`"1/4"`）的旧形态 | 数值解析 | 我们代码按「数值或字符串」双兼容写 |
| 6 | 公式串非空（`bonuses/init.bonus/ac.formula/attack.bonus`） | 无法精确求值 → 全部按警告+原文 notes | 拿官方 PHB 预设包（prefilled 英雄卡）验收 |
| 7 | attuned 物品实例（`attuned:true`） | notes 标注是否正确 | 同上 |
| 8 | `type.value=shield` 的盾牌 AC 叠加 | AC 决策树第 5 步（+2/面） | 拿带盾 NPC 验收 |
| 9 | `img` 引用 `systems/dnd5e/...` 的资产兜底 | 头像/棋子缺图 | heroes 4 个 actor 已触发，导出时人工核对 |
| 10 | subclass 的 `advancement`（WAR 的 the-fiend 有 5 个 advancement 键） | 不导入（已落到 features），但确认没有 features 漏项 | 对比 FVTT 端特征清单 |
| 11 | `spell0`（戏法池）override 语义 | 我们 slots 不含 0 环 | 出现时忽略+警告 |
| 12 | 六边形网格/其他 systemVersion 包 | 整体拒收窗口（重构计划 §9.2 #6） | 非本文范围 |

---

## 8. 证据索引（快速回查）

- **heroes 全量 actor 清单/类型分布/CR 表/AC 形态**：`.tmp/fvtt-dump/heroes/actors.json`（23 条）；
- **sixian 全量 actor 清单**：`.tmp/fvtt-dump/sixian/actors.json`（14 条）；
- **PC 结构范本**：WIZ（法师）`VrylLHMmHRbEQpLH`、DRU（德鲁伊）`ugX5RNZ7KCYRwbNG`、ROG（盗贼）`SYWoLOLtQXsuAvOy`、WAR（魔契师）`5tFMDd0ORXpRrk3s`、BAR（野蛮人）`Eg92CJK7PizCnlf7`、PAL（圣武士）`LezW8CAX1aOWgm2V`；
- **NPC 结构范本**：WLF（狼）`pkuHCnaAa9ENHZpn`、STA-4（侍女雕像4人）`JYTMRQDo7R3A1Otx`、SWM（蜘蛛群）`FiNf35frtp0TMM1z`、NEC（女死灵术士）sixian `sETfZ44cI6rCJhI6`、VET（105老兵）sixian `iIqICziePnxXshCX`、KNT（教团骑士）sixian `fvvJWfHymbPmfZ39`、SKL（骷髅管家）sixian `uAB6j3tlDANUTRrY`、PET（梦梦）sixian `xqwKyEma2QAeovty`、REV（卡洛斯）sixian `42NXiP9aVVRKxNGX`；
- **内嵌条目**：class item `VrylLHMmHRbEQpLH.Aur7JTfK3FVc1JGz`（法师）、subclass `5tFMDd0ORXpRrk3s.qfjY35TpCmFZxYfp`（邪魔）、race item `ugX5RNZ7KCYRwbNG.fM1HPNyo4p5c8TFv`（人类变体，walk 30）、NPC 施法 feat `sETfZ44cI6rCJhI6.G6MAk6w6YriDlv21`（identifier=spellcasting）、猛击武器（STA-4，activity `dnd5eactivity000`，reach 5）、蜘蛛啃咬（SWM，activity `biteAttack`，双段 parts）；
- **效果**：actor 级 `xqwKyEma2QAeovty.dnd5epoisoned000`（statuses=["poisoned"]）；条目级 Shillelagh（changes 路径补丁）；
- **我们的目标 schema**：`data/plugins/dnd5e/dnd5e_plugin/schema.py`、`statblock.py`、`spells.py`（`spell_slots_from_sheet`）、`rules.py`（18 技能表）；`data/plugins/dnd5e-srd/packs/{monsters,classes,spells,conditions}.json`；`frontend/src/rulesets/dnd5e/character-sheet.ts`（equipment/Spellcasting 行形态）。

### 附：与重构计划的衔接

- 本文档覆盖 `docs/plans/dnd5e-vtt-refactor-plan.md` §5.2 的 `data/actors（LevelDB）→ dnd5e 角色卡 + 怪物 statblock` 分支，并把该计划 §9.2 风险 #3（「逐字段映射误差」）收敛为可执行字段表 + 显式降级清单。
- 下一刀编码顺序建议：① actor/items 聚合器（复合键）→ ② character 转换器（§2 表）→ ③ statblock 转换器（§3 表）→ ④ AC 装备重算小引擎（§2.6 决策树，唯一规则计算件）→ ⑤ 导入报告降级计数（§4）。NPC 施法通道（§5.1 #1）作为 dnd5e 插件侧的独立小改动先行合入，避免 statblock 转换器返工。
