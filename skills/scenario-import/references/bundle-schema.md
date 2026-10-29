# 剧本包字段说明（format_version 1）

站点 `backend/trpg_server/scenario_bundle.py` 与外部 Skill 共用这份契约。
字段名不可改写；未知字段会被保留但不参与展示。

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
| `title` | string | 是 | 剧本标题；缺省时用文件名 |
| `author` | string | 否 | 作者/整理者 |
| `creator_username` | string | 否 | 站点导入时会用当前登录用户覆盖 |
| `playerCount` | number | 否 | 推荐人数，默认 1 |
| `notes` | string | 否 | 导入页说明 |
| `global_summary` | string | 否 | 全局概要（≤ 1000 字） |

## modules[]

每个模块就是一条世界书/知识库条目。`scene`、`ending` 模块还会对应图谱地点节点。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 稳定唯一 id；建议英文/拼音；场景模块的 id 直接作为图谱地点 id |
| `title` | string | 是 | 模块标题 |
| `module_type` | string | 是 | `scene` / `ending` / `background` / `npc` / `item` / `clue` / `handout` / `custom` |
| `content` | string | 是 | 原文正文（不要改写） |
| `summary` | string | 是 | 30–120 字客观摘要 |
| `visibility` | string | 否 | `public` / `hidden` / `kp_only`，默认 `public` |
| `keywords` | string[] | 是 | 2–8 个专名触发词 |
| `secondary_keywords` | string[] | 否 | 需要同时命中的第二组词 |
| `secondary_logic` | string | 否 | `and_any` / `and_all` / `not_any` / `not_all`，默认 `and_any` |
| `is_constant` | boolean | 否 | 世界观/背景常驻条目为 `true` |
| `tier` | string | 否 | `core` / `background` / `archived` |
| `probability` | number | 否 | 0–100，默认 100 |
| `group` | string | 否 | 检索分组 |
| `trigger_chunks` | boolean | 否 | 是否参与关键词触发，默认有 `keywords` 时为 `true` |
| `source_ref` | object | 否 | `{"line_start": 12, "line_end": 30}` 便于回溯原文 |

`worldbook` 是 `modules` 的另一种写法，适合不属于任何场景的纯设定条目；
导入时会自动追加到 `modules`，`module_type` 默认 `background`。

## narrative

```json
{
  "topology": "linear | hub | sandbox | hybrid",
  "start": {"location_id": "hall"},
  "nodes": [],
  "edges": [],
  "meta": {}
}
```

### nodes[]

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一 id；场景模块复用模块 id |
| `kind` | string | 是 | `act` / `area` / `location` / `ending` / `npc` / `item` / `clue` / `concept` / `flag` / `clock` |
| `title` | string | 是 | 展示名 |
| `parent` | string \| null | 否 | 上级地点/区域/幕；只能指向树节点 |
| `summary` | string | 否 | KP 可读摘要 |
| `body` | string | 否 | 补充正文；长文建议放模块 |
| `visibility` | string | 否 | `public` / `hidden` / `kp_only` |
| `keywords` | string[] | 否 | 触发词 |
| `aliases` | string[] | 否 | 别名 |
| `scene_role` | string | 否 | 结局节点写 `ending` |
| `access` | string | 否 | `free` / `locked` / `hidden` 等 |
| `repeatable` | boolean | 否 | 是否可重复进入 |
| `spoiler_level` | number | 否 | 剧透等级 |

`contains` / `inhabits` 边会推导资产节点的 `parent`；资产有 `parent` 却没有边时，
程序会补一条 `地点 contains 资产`，保证「物品在哪个地点」可检索。

### edges[]

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` | string | 是 | 起点节点 id |
| `to` | string | 是 | 终点节点 id |
| `type` | string | 是 | 见 SKILL.md 边类型表 |
| `hard` | boolean | 否 | 是否强制移动/不可绕过 |
| `condition` | object \| null | 否 | 与运行时条件表达式一致，如 `{"item": "key"}` |
| `label` | string | 否 | 展示标签；`relationship` 写「父子/敌对/雇佣」 |
| `visibility` | string | 否 | `public` / `hidden` / `kp_only` |
| `evidence` | object | 否 | `{"quote": "原文依据"}` |

程序会丢弃端点不存在、类型未知、指向自身的边；`relationship` 只保留 NPC ↔ NPC。

## exclusions[]

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `prefix` | string | 被剔除段落开头 8–20 字 |
| `line_start` | number | 原文起始行（1 开始） |
| `line_end` | number | 原文结束行 |
| `category` | string | `author_note` / `play_experience` / `meta` / `toc` / `ad` / `duplicate` / `format` / `other` |
| `reason` | string | 剔除理由 |

## 最小可用示例

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
      "content": "调查员进入大厅，管家迎接，壁炉上放着黄铜钥匙。",
      "summary": "大厅是旧宅的入口，管家在此迎接调查员。",
      "visibility": "public",
      "keywords": ["旧宅", "大厅", "管家", "黄铜钥匙"],
      "tier": "background"
    },
    {
      "id": "ending",
      "title": "结局",
      "module_type": "ending",
      "content": "仪式被阻止，调查员离开旧宅。",
      "summary": "调查员阻止仪式并离开。",
      "visibility": "public",
      "keywords": ["仪式", "结局"],
      "tier": "core"
    }
  ],
  "narrative": {
    "topology": "hub",
    "start": {"location_id": "hall"},
    "nodes": [
      {"id": "hall", "kind": "location", "title": "旧宅大厅", "summary": "旧宅入口"},
      {"id": "ending", "kind": "ending", "title": "结局", "scene_role": "ending"},
      {"id": "gm", "kind": "npc", "title": "管家", "parent": "hall"},
      {"id": "key", "kind": "item", "title": "黄铜钥匙","parent": "hall"}
    ],
    "edges": [
      {"from": "hall", "to": "gm", "type": "inhabits"},
      {"from": "hall", "to": "key", "type": "contains"},
      {"from": "hall", "to": "ending", "type": "adjacent"}
    ]
  },
  "exclusions": [
    {"prefix": "作者吐槽：这剧本", "line_start": 1, "line_end": 1, "category": "author_note", "reason": "作者个人吐槽，与剧情无关"}
  ]
}
```
