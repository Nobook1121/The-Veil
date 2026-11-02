# 剧本包字段说明（创作 Skill 版）

站点 `backend/trpg_server/scenario_bundle.py` 与外部 Skill 共用这份契约。
基础字段与导入 Skill 的 `skills/scenario-import/references/bundle-schema.md` 相同；
本页补充创作时最常用的**触发器、战斗、结局、旗标与时钟**写法。

```json
{
  "format": "the-veil-scenario-bundle",
  "format_version": 1,
  "scenario": {
    "title": "剧本名",
    "author": "作者",
    "playerCount": 2,
    "notes": "给 KP 的说明",
    "global_summary": "一句话概要"
  },
  "modules": [],
  "narrative": {},
  "exclusions": [],
  "worldbook": []
}
```

## scenario

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 是 | 剧本标题 |
| `author` | string | 否 | 作者/整理者 |
| `playerCount` | number | 否 | 推荐人数，默认 1 |
| `notes` | string | 否 | 导入页说明、测试清单 |
| `global_summary` | string | 否 | 全局概要（≤ 1000 字） |

## modules[]

每个模块是一条世界书/知识库条目；`scene` / `ending` 模块还会对应图谱节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 稳定唯一 id；场景模块的 id 直接作为地点节点 id |
| `title` | string | 是 | 模块标题 |
| `module_type` | string | 是 | `scene` / `ending` / `background` / `npc` / `item` / `clue` / `handout` / `custom` |
| `content` | string | 是 | 正文 |
| `summary` | string | 是 | 30–120 字客观摘要 |
| `visibility` | string | 否 | `public` / `hidden` / `kp_only` |
| `keywords` | string[] | 是 | 2–8 个专名触发词 |
| `tier` | string | 否 | `core` / `background` / `archived` |
| `triggers` | object[] | 否 | 旧式触发器（见下方，推荐改用 `narrative.triggers`） |
| `battle` | object | 否 | NPC/怪物战斗数值 |
| `skills` | object[] | 否 | NPC 技能数值 |
| `weapons` | object[] | 否 | NPC 武器 |
| `open_ending` | boolean | 否 | 结局是否允许开放式收尾 |
| `notes` | string | 否 | 给 KP 的补充说明 |

`worldbook` 是 `modules` 的另一种写法，适合纯设定条目，导入时自动并入 `modules`。

## narrative

```json
{
  "topology": "linear | hub | sandbox | hybrid",
  "start": {"location_id": "hall"},
  "nodes": [],
  "edges": [],
  "triggers": [],
  "assets": [],
  "flags": [],
  "clocks": [],
  "meta": {}
}
```

### nodes[]

`kind` 只能是：`act` / `area` / `location` / `ending` / `npc` / `item` / `clue` / `concept` / `flag` / `clock`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 唯一 id；场景模块复用模块 id |
| `kind` | string | 节点类型 |
| `title` | string | 展示名 |
| `parent` | string \| null | 上级地点/区域 |
| `summary` / `body` | string | 摘要 / 正文 |
| `visibility` | string | `public` / `hidden` / `kp_only` |
| `keywords` / `aliases` | string[] | 触发词 / 别名 |
| `completion` | object | 结局完成条件（仅 `ending`） |
| `objects` | object[] | 房间内可交互物件（可选） |
| `on_enter` / `on_reenter` | object[] | 进入效果（可选） |

### edges[]

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `from` / `to` | string | 真实存在的节点 id |
| `type` | string | `adjacent` / `portal` / `next` / `leads_to` / `contains` / `inhabits` / `knows` / `uses` / `requires` / `reveals` / `triggers` / `conflicts` / `relationship` |
| `hard` | boolean | 是否强制/不可绕过 |
| `condition` | object | 结构化条件（同 `completion`） |
| `label` | string | `relationship` 写「父子/敌对/雇佣/主从」 |
| `visibility` | string | `public` / `hidden` / `kp_only` |
| `evidence` | object | `{"quote": "依据"}` |

硬规则：

- 位置关系：地点 → 物品/子地点 = `contains`；NPC → 地点 = `inhabits`。
- 人物关系只能是 NPC → NPC 的 `relationship`。
- 所有端点必须存在，否则导入时被丢弃并报警告。
- 场景模块必须有一个同 id 的地点节点。

## narrative.triggers[]

结构化触发器，运行时直接读取。推荐创作时使用这一组字段。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 id |
| `label` | string | 是 | 触发器显示名 |
| `binding` | object | 是 | 绑定方式，见下表 |
| `condition` | object | 否 | 额外条件（同 `completion`） |
| `content` | string | 否 | 纯文本内容 |
| `attachments` | object[] | 否 | 富文本/图片/文件附件 |
| `visibility` | string | 否 | `player_visible` / `kp_only` |
| `spoiler_level` | number | 否 | 0–5 |
| `once` | boolean | 否 | 默认 `true` |
| `once_scope` | string | 否 | `room` / `pc` |
| `cooldown` | number | 否 | 冷却轮数 |
| `priority` | number | 否 | 排序 |
| `enabled` | boolean | 否 | 默认 `true` |

### binding

| kind | 必需字段 | 触发时机 |
| --- | --- | --- |
| `enter` | `location_id` | 进入地点 |
| `interaction` | `location_id`、`object_id`、可选 `result` | 与物件交互 |
| `clue` | `clue_id` | 发现线索 |
| `npc` | `npc_id`、可选 `event` | NPC 对话 |
| `clock` | `clock_id`、`gte`/`lte`/`eq` | 时钟达到阈值 |
| `event` | `event_id` | 事件 |
| `manual` | `keyword` 或 `condition` | KP 手动触发 |

### 富文本附件

```json
{
  "id": "att_letter_1",
  "kind": "richtext",
  "format": "markdown",
  "body": "## 一封泛黄的信\n\n> 亲爱的安妮：\n\n- 第三块砖下藏着**黄铜钥匙**\n- 不要相信管家\n\n| 日期 | 事件 |\n| --- | --- |\n| 1924-03-01 | 第一次失踪 |",
  "order": 0,
  "visibility": "player_visible"
}
```

图片/文件附件用 `kind: "image"` / `"file"` + `url` 或 `asset_id`。

## 战斗数值

写在 NPC 模块：

```json
{
  "id": "npc_cultist",
  "title": "邪教徒",
  "module_type": "npc",
  "content": "……",
  "battle": {
    "STR": "60",
    "CON": "55",
    "SIZ": "60",
    "DEX": "50",
    "HP": "11",
    "DB": "+1D4",
    "build": "1",
    "MOV": "7",
    "armor": "0"
  },
  "skills": [
    {"name": "格斗（斗殴）", "value": 55},
    {"name": "闪避", "value": 30}
  ],
  "weapons": [
    {"name": "砍刀", "skill": "格斗（刀剑）", "damage": "1D8+DB", "range": "接触", "attacks": "1"}
  ]
}
```

KP 开战时把模块 `id` 传给 `combat.start` 的 `module_id`，即可按卡自动建参；也可在 `participants` 中传入 `name / dex / str / siz / con / hp / armor / skills / weapons` 覆盖。

## 结局条件

```json
{
  "id": "ending_seal",
  "kind": "ending",
  "title": "结局：重新封印",
  "completion": {
    "all": [
      {"clue": "ritual_diagram"},
      {"item": "silver_dagger"},
      {"flag": "altar_ready"}
    ]
  }
}
```

支持的原子：`clue` / `item` / `flag` / `visited` / `visited_object` / `check` / `clock` / `attitude` /
`type: location` / `type: interaction_result` / `type: skill`，可用 `all`、`any`、`not` 组合。

## narrative.flags[] / narrative.clocks[]

```json
"flags": [{"id": "altar_ready", "label": "祭坛已准备"}],
"clocks": [{"id": "doom_clock", "label": "仪式进度", "threshold": 6, "value": 0}]
```

时钟也可以写成 `kind: "clock"` 的节点。

## exclusions[]

创作剧本通常为空；如果要剔除草稿段，格式同导入 Skill：

```json
{"prefix": "这段开头的前 8-20 字", "line_start": 12, "line_end": 14,
 "category": "author_note", "reason": "一句话理由"}
```

## 最小示例

```json
{
  "format": "the-veil-scenario-bundle",
  "format_version": 1,
  "scenario": {"title": "旧宅", "playerCount": 2},
  "modules": [
    {
      "id": "hall",
      "title": "旧宅大厅",
      "module_type": "scene",
      "content": "管家迎接，壁炉上放着黄铜钥匙。",
      "summary": "大厅是旧宅入口，管家在此迎接调查员。",
      "keywords": ["旧宅", "大厅", "管家", "黄铜钥匙"]
    },
    {
      "id": "ending",
      "title": "结局：离开旧宅",
      "module_type": "ending",
      "content": "调查员带着证据离开。",
      "summary": "调查员离开旧宅。",
      "keywords": ["结局", "离开"]
    }
  ],
  "narrative": {
    "topology": "hub",
    "start": {"location_id": "hall"},
    "nodes": [
      {"id": "hall", "kind": "location", "title": "旧宅大厅"},
      {"id": "ending", "kind": "ending", "title": "结局：离开旧宅", "completion": {"item": "key"}},
      {"id": "butler", "kind": "npc", "title": "管家", "parent": "hall"},
      {"id": "key", "kind": "item", "title": "黄铜钥匙", "parent": "hall"}
    ],
    "edges": [
      {"from": "butler", "to": "hall", "type": "inhabits"},
      {"from": "hall", "to": "key", "type": "contains"},
      {"from": "hall", "to": "ending", "type": "leads_to"}
    ],
    "triggers": [
      {
        "id": "trg_hall_enter",
        "label": "大厅描写",
        "binding": {"kind": "enter", "location_id": "hall"},
        "attachments": [
          {"id": "att_hall", "kind": "richtext", "format": "markdown",
           "body": "壁炉里的火已经熄灭。**黄铜钥匙**在灰烬中反光。"}
        ]
      }
    ]
  }
}
```
