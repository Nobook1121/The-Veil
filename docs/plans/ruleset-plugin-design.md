# 规则插件化设计（Ruleset Plugin）

> 状态：**已拍板并部分落地**（见 §11）。信任边界：只允许随包内置插件。
> 目标：新增一套跑团规则（第二个规则集）只需新增一个插件包，核心不改；CoC7 作为第一个插件被适配进来。

## 1. 现状锚点（已确认的实际落点）

| 规则逻辑 | 现在的落点 |
| --- | --- |
| 检定（CoC7 成功率/奖励骰/难度） | [dice.py](../../backend/trpg_server/agents/tools/dice.py#L36-L71)、`roll_coc_check`/`roll_room_check`（[L831-L990](../../backend/trpg_server/agents/tools/dice.py#L831-L990)） |
| 理智与疯狂 | `roll_sanity_check`/`roll_madness`（[dice.py L1004-L1318](../../backend/trpg_server/agents/tools/dice.py#L1004-L1318)），核心直接认识 SAN |
| 战斗结算 | 已插件化：[combat/contracts.py](../../backend/trpg_server/combat/contracts.py#L54-L101)、[combat/registry.py](../../backend/trpg_server/combat/registry.py)、[scheduler.py](../../backend/trpg_server/combat/scheduler.py) 负责调度，[rulesets/coc7.py](../../backend/trpg_server/combat/rulesets/coc7.py) 负责规则 |
| 战斗房规 | [combat/settings.py](../../backend/trpg_server/combat/settings.py#L47-L90)，从 `room_info.house_rules` 解析；`COMBAT_RULESETS` 目前只列 `coc7`（[L18](../../backend/trpg_server/combat/settings.py#L18)），与注册表中另一个未启用的规则实现不一致（本期不动） |
| 角色卡模型 | [routes/characters.py](../../backend/trpg_server/routes/characters.py#L364-L572) 里 `_runtime_to_test_character` / `_test_character_to_runtime` 写死 CoC 字段（STR..LUC、sanity/hp/mp、battleAttributes、skillGroups） |
| 卡写路径 | [character_service.py](../../backend/trpg_server/character_service.py#L24-L43) 的 `CHARACTER_WRITE_FIELDS` 是全量白名单，`apply_save()` 直接落盘 |
| 技能表 | `data/character_skills.json`（`version/defaultLocale/skills[{key,labelKey,category,base,specialties}]`），由 `list_character_skills()` 提供（[characters.py L815-L832](../../backend/trpg_server/routes/characters.py#L815-L832)） |
| 武器表 | `data/weapons/builtin/*.json`，[weapon_catalog.py](../../backend/trpg_server/weapon_catalog.py)；`combat` 与 `routes/combat.py` 共用 `weapon_rules()` |
| 职业表 | `data/occupations/builtin/*.json`（`list_builtin_occupations`） |
| 规则书检索 | [ruleset_adapters.py](../../backend/trpg_server/agents/ruleset_adapters.py#L40-L110)（`COC7Adapter` + `RulesetRegistry`，只做知识分块，不参与结算） |
| AI 工具注册 | [agents/tools/__init__.py](../../backend/trpg_server/agents/tools/__init__.py#L24-L50) 全局 `default_tool_registry()`；[profiles.py](../../backend/trpg_server/agents/profiles.py#L26-L54) 固定 `DEFAULT_KP_TOOLS`；[runtime.py `_resolve_tool`](../../backend/trpg_server/agents/runtime.py#L247-L256) 只把首个 `_` 还原成 `.` |
| 卡系统配置 | `general.toml [character_rules]`（[config/schema.py L230-L244](../../backend/trpg_server/config/schema.py#L230-L244)）含属性骰、技能基础值，属于 CoC 口径 |
| 前端卡面 | [character-sheet.ts](../../frontend/src/app/character-sheet.ts) 顶部写死 `COC7CoreAttributeKey` |

结论：战斗已经验证了「核心调度 + 插件数学」可行；检定、角色卡、目录、工具注册仍是 CoC 硬编码。

## 2. 插件边界与注册

新增包 `backend/trpg_server/ruleset/`，与 `combat/` 平级，`combat` 成为它的子能力。

```
ruleset/
  contracts.py      # RULESET_API_VERSION + Protocol 定义
  registry.py       # 发现 / 注册 / 启用 / 解析
  check_service.py  # 核心检定调度（唯一的检定入口）
  coc7/
    __init__.py     # Coc7Plugin 聚合
    schema.py       # 角色卡 schema + legacy 适配
    checks.py       # 检定数学（接管 dice.py 的 CoC 部分）
    catalogs.py     # 技能/武器/物品/职业
    tools.py        # 插件 AI 工具与提示词片段
```
> 注：**没有 loader.py**。已确认信任边界为「只允许随包内置插件」，不提供任何从 `data/` 或用户目录加载 Python 的入口。

契约元数据（只允许声明，加载时不得有网络/磁盘副作用）：

```python
RULESET_API_VERSION = 1

class RulesetPlugin(Protocol):
    id: str                      # 首段工具命名空间，如 "coc7"
    display_name: str
    ruleset_version: str         # 插件自身语义版本
    api_version: int             # 必须 == RULESET_API_VERSION
    min_core_version: str        # 可选，低于则禁用
    supported_locales: tuple[str, ...]
    capabilities: frozenset[str] # {"checks","combat","sanity","items",...}
    schema: CharacterSchemaProvider
    checks: CheckProvider
    catalogs: CatalogProvider
    combat: CombatRuleset       # 复用 combat/contracts.py，不新造一套
    tools: ToolProvider | None
```

### 2.1 发现方式（**已拍板：只做内置静态清单**）

- **唯一方式：内置静态清单**。`registry.py` 里显式 `{id: instance}`，与 [combat/registry.py](../../backend/trpg_server/combat/registry.py) 同风格。
- 参考 Discourse 的插件实践：插件随宿主代码一起发布、显式列出、声明 API 版本；宿主不扫描用户可写目录，不执行运行时下载的代码。
- **明确不做**：不从 `data/`、`plugins/`、环境变量路径加载 `.py`；不接 pip entry_points（项目是自托管压缩包分发，没有包管理上下文）。
- 允许放进 `data/` 的只有**纯数据**（技能/武器/物品/职业 JSON、规则书文本），且只被解析、不被执行。

### 2.2 注册与启用

- 注册：`RulesetRegistry(builtins=[...])`；构造完成后不可变。
- 站点启用：本期只有 `coc7`，**不引入** `[rulesets]` 配置段，避免空配置面；引入第二个规则集时再加 `enabled/default`（走 `config/schema.py` + 模板同步）。
- 房间选择：`info.json.ruleset_id`，只能取已启用集合中的值。规则与剧本绑定：房间取剧本的 `ruleset_id`，CoC7 剧本的房间即用 CoC7。
- 解析优先级：`room_info.ruleset_id` > 房间快照里的 `scenario.ruleset_id` > 默认 `coc7`；未知或禁用时回退默认并写日志（不抛错，避免旧房间打不开）。
- 个人设置不得覆盖规则集（同房间玩家必须用同一套规则）。

### 2.3 版本与兼容性

- `api_version != RULESET_API_VERSION`：插件禁用，房间回退默认，设置页与 `/api/rulesets` 标注「不兼容」。
- `ruleset_version` 只用于展示与知识库分块（`ruleset_id + knowledge_version`），不参与门控。
- 角色卡记录 `rulesetId` + `rulesetVersion`（写入时的插件版本），用于提示「此卡由旧版规则创建」，不在读取时强制升级。
- 预留 `migrations`：插件可提供 `migrate_sheet(old, from_version) -> sheet`，仅显式迁移时调用。

## 3. 角色卡 schema

两段式存储，核心只碰信封，不拆规则字段：

```jsonc
{
  "id": "...", "name": "...", "avatar": "...", "playerId": "...",
  "rulesetId": "coc7", "rulesetVersion": "1.0",
  "sheet": { /* 插件定义，核心整体透传 */ },
  "createdAt": "...", "updatedAt": "..."
}
```

`CharacterSchema`（插件提供，纯数据 + 两个纯函数）：

| 成员 | 内容 | CoC7 示例 |
| --- | --- | --- |
| `attributes` | `key/labelKey/min/max/roll_formula/default` | STR..LUC，`3d6x5`、`(2d6+6)x5` |
| `resources` | `key/labelKey/max/init/min` | hp/mp/san/luck |
| `derived` | `key/labelKey/compute` | `hp=(CON+SIZ)//10`、`db/build=体格表`、`mov` |
| `status_flags` | `key/labelKey/kind(body\|mental)` | majorWound/unconscious/dead；三类疯狂 |
| `sections` | 前端分组排版（可选） | 属性 / 战斗 / 技能 / 背景 |
| `skills_ref` | 指向目录（见 §5） | `skills.json` |
| `validate(payload)` | 返回 `{ok, errors[]}` | 属性范围、技能不越界 |
| `derive(sheet)` | 保存时重算派生值 | 改 CON 后 HP 上限自动更新 |

- 校验与派生只在服务端做，前端提交值不可信；入口是 [character_service.apply_save()](../../backend/trpg_server/character_service.py#L315-L342)。
- 写白名单 `CHARACTER_WRITE_FIELDS` 收敛为信封字段 + `sheet`，不再逐个列 CoC 字段。
- **旧卡兼容**：CoC 插件实现 `adapt_legacy(raw) -> sheet` 与 `export_legacy(sheet) -> raw`；一期读写仍保留现有顶层字段（双写），不做批量改文件。
- 房间内 `member.character_card` 是快照拷贝，[`_sync_room_character_snapshots`](../../backend/trpg_server/routes/characters.py#L748-L775) 必须连带写入 `rulesetId`，读快照时按该 id 取插件。

## 4. 检定与战斗结算

### 4.1 检定契约

```python
@dataclass
class CheckRequest:
    kind: str            # skill / attribute / resource / opposed / custom
    name: str            # 技能键或属性键，插件负责别名归一
    target: int | None   # 显式目标值时跳过推算
    difficulty: str = "regular"
    modifier: int = 0    # 情景修正
    bonus: int = 0
    penalty: int = 0

@dataclass
class CheckResolution:
    roll: int
    target: int
    level: str           # 插件自定义等级名，核心只透传展示
    success: bool
    detail: dict         # 插件补充（对抗双方等级、消耗等）
    state_patch: dict    # 需要写回角色卡的变化（扣 SAN/MP、加状态）
```

插件方法只有四个：`list_check_names(card)`、`resolve_target(card, req)`、`roll(card, req, rng)`、`apply_patch(card, patch)`。

### 4.2 核心只做调度

新增 `check_service.py`（核心层，规则无关），是唯一检定入口，负责：

1. 定位成员与角色卡（沿用 `_find_member` 语义）；
2. 调插件算目标值；
3. 走玩家掷骰等待 / 3D 动画 / 降级链（[player_roll.py](../../backend/trpg_server/agents/player_roll.py)、`_dice_roll_metadata`）；
4. 应用 `state_patch` 并同步房间快照；
5. 写房间日志、广播 socket、触发器揭示（`reveal_trigger_after_check`）。

AI 工具、REST、房间控制台三处**都调这一个服务**，不允许各自实现规则运算。
AI 工具的返回结构保持现有 `result_message + metadata` 形态，前端不需要同步改。

### 4.3 战斗

- `CombatRuleset` 契约不变，只是由 `plugin.combat` 提供；
- CoC 插件内部**组合**现有 `combat/rulesets/coc7.py`，不复制代码；
- `combat/scheduler.py` 继续作为核心调度；
- `combat/registry.py` 二期改为从 `ruleset.registry` 转发，删除自己的静态表；
- `combat/settings.py` 的 `COMBAT_RULESETS` 改为读 registry 的 enabled 列表（修正当前只列 coc7 的不一致）。

## 5. SAN 等专属概念如何不污染核心

分三层，缺一层都不影响其他规则运行：

1. **通用资源层**：`resources` 可声明任意资源（hp / san / hope / mana）。核心只有 `resource.adjust(card, key, delta)` 与前端通用资源条，不出现任何 SAN 字面量。
2. **插件扩展位**：`plugin.extensions` 可选字典，键由插件自定。CoC 提供 `sanity`：临时/不定期/永久疯狂阈值、疯狂检定、恢复规则。核心只把它当作「有一组状态会被写回」。
3. **插件工具层**：`san.roll_sanity_check` / `san.roll_madness` 由 CoC 插件注册；核心只注册通用 `check.roll` / `resource.adjust`。

兼容与降级：

- 保留 `san.roll_sanity_check` 与 `sanity.roll_sanity_check` 两个**别名**，映射到插件工具，保证现有 `DEFAULT_KP_TOOLS` 与提示词不改也能跑。
- 插件没有 `sanity` 扩展时：不注册相关工具、前端不渲染疯狂区块、插件提示词片段不注入。
- 疯狂等级判定完全在插件内，核心只接收 `state_patch`（例如 `status.mental.temporaryInsanity=true`）。

## 6. 技能表 / 武器表 / 物品表

`CatalogProvider` 四个方法，全部返回现有 JSON 结构，前端已有渲染逻辑可直接复用：

- `skills(locale)` → 同 `data/character_skills.json`；
- `weapons()` → 同 `weapon_catalog.load_preset_weapons()` 输出；
- `occupations()` → 同 `data/occupations/builtin/*.json`；
- `items()` → 新；物品目前是 `equipment` 自由文本，一期只读目录，不改存储。

落盘位置迁移为 `data/rulesets/<id>/catalogs/`：

```
data/rulesets/coc7/catalogs/skills.json
data/rulesets/coc7/catalogs/weapons/builtin/*.json
data/rulesets/coc7/catalogs/occupations/builtin/*.json
data/rulesets/coc7/catalogs/items.json      # 可空
```

- 一期：插件优先读新路径，**缺失回退旧路径**（`settings.CHARACTER_SKILLS_FILE` / `WEAPONS_DIR` / `OCCUPATIONS_DIR`），部署不改也能跑。
- HTTP：新增 `GET /api/rulesets`；`/api/character-catalogs/{skills,weapons,occupations}` 增加可选 `?ruleset=`，缺省用站点默认；旧 URL 行为不变。
- 武器数值解析（射程/连发/故障）仍留在 `weapon_catalog.weapon_rules()`，插件只供数据；若某规则需要不同口径，插件可覆盖 `weapon_stats(weapon)`。

## 7. AI 工具如何按插件暴露

### 7.1 命名空间

- 核心保留：`room / dice / check / knowledge / trigger / narrative / combat / music / suggest / san / sanity`（含现有别名）。
- 插件工具必须以插件 id 为首段：`coc7.roll_sanity_check`。
- 下发 API 前由现成的 [`to_api_tool_name`](../../backend/trpg_server/agents/tools/base.py#L14-L22) 转成 `coc7_roll_sanity_check`；`_resolve_tool` 只替换首个 `_`，因此**不允许**出现 `coc7_x_y` 之外的二级命名空间（即插件内不要用 `coc7.sanity.check`）。
- 加载时校验：与核心名/别名重复、或不符合首段规范的工具直接拒绝注册并告警。

### 7.2 注册与下发

- `default_tool_registry()` 只装核心工具；
- 新增 `build_tool_registry(plugin)`：核心工具 + 该插件 `tools()`；在 [profiles.resolve_agent_profile()](../../backend/trpg_server/agents/profiles.py#L87-L124) 处合并，而不是启动时全局加。
- 这样 manual/freeform 房间（无 AI KP）不构造 profile，也不会实例化插件工具。

### 7.3 越权边界

插件工具 handler 由核心统一包一层 `RulesetToolGuard`：

- 只能操作 `rulesetId == plugin.id` 的角色卡；跨规则卡返回明确错误，不做静默降级；
- 只允许改 `sheet` 与 `status`；房间/剧本/配置/用户一律不可写；
- 入参按 JSON Schema 白名单清洗，丢弃路径、命令、URL、身份类字段（复用 [site_agent_tools/base.py](../../backend/trpg_server/site_agent_tools/base.py) 的思路）；
- 落盘必须走 `character_service.apply_save()` 或房间快照同步函数；
- 破坏性动作（重置卡、批量改属性）走 `pending_action` 待确认，不直接执行。

### 7.4 提示词

插件提供 `prompt_fragments()`（判定口径、资源含义、专属概念说明），由 [prompt_builder.py](../../backend/trpg_server/agents/prompt_builder.py) 在规则段落注入；缺失时用通用文案。角色卡生成提示词（[role_config.py](../../backend/trpg_server/role_config.py#L20) 现在写死 CoC）也改为插件提供。

## 8. 与现有 CoC 共存 / 迁移路径

**推荐：先抽契约，再内建 CoC 插件（适配器包裹现状），不做并行双实现。**

理由：

- `combat/` 已验证「核心调度 + 插件数学」模式，复用同一套思路成本最低；
- CoC 是唯一在跑的规则，另写一份实现必然与现有行为漂移（奖励骰、濒死、大成功阈值都已调过）；
- 适配器可保证零行为变化，能按模块逐个切换并随时回退。

阶段（每阶段可独立验证、独立发布）：

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | `contracts.py` + `registry.py` + coc7 插件骨架（schema / 目录 / 判定桥 / 战斗组合） | **已落地** |
| 1 | 角色卡信封与 `sheet` 写入、目录接口走插件、`/api/rulesets` | **已落地** |
| 2 | 旧卡一次性迁移（幂等，默认 dry-run） | **已落地并已执行** |
| 3 | 检定调度收敛到 `check_service`，`dice.py` 只留调度 | **已落地** |
| 4 | AI 工具集按房间规则集组装（`profiles.py` / `runtime.py`） | **已落地** |
| 5 | `combat/registry.py` 改为从规则插件转发 | **已落地** |
| 6 | 房间 / 剧本 `ruleset_id` 持久化与继承 | **已落地** |

并行会话避让（**本设计不修改下列文件**）：

- 房间模式 / 世界意志：`routes/rooms.py`、`room_mode.py`、`immersive/*`、`room_console_commands.py`。
- 检定 / SAN / 物品命令：`agents/tools/dice.py`、`agents/tools/combat.py`、`room_console_commands.py`。
- 一期只新增 `ruleset/` 包，并只改 `character_service.py` / `routes/characters.py` 的**新增参数与校验**，不动上述文件；阶段 2、3 等对方落地后再做。

## 9. 改动面清单（按依赖顺序）

| # | 文件 | 改动 | 依赖 |
| --- | --- | --- | --- |
| 1 | `ruleset/contracts.py`（新） | `RULESET_API_VERSION`、`RulesetPlugin`、`CheckRequest/CheckResolution`、`CharacterSchema` 等 Protocol/dataclass | 无 |
| 2 | `ruleset/registry.py`（新） | 内置静态注册 + 版本校验 + `resolve_for_room/scenario` | 1 |
| 3 | `ruleset/coc7/{__init__,schema,legacy}.py`（新） | 聚合入口、卡 schema、旧卡双向适配 | 1、2 |
| 4 | `config/schema.py` + `config/templates/general.toml` | 新增 `[rulesets] enabled/default/implementation`，保持模板同步测试通过 | 2 |
| 5 | `character_service.py`、`routes/characters.py` | 读写 `rulesetId` + `sheet`，调插件校验/派生；旧字段双写；目录接口加 `?ruleset=` | 3、4 |
| 6 | `ruleset/coc7/catalogs.py` + `data/rulesets/coc7/catalogs/**`（新） | 技能/武器/职业/物品目录，旧路径兜底 | 3 |
| 7 | `check_service.py`、`ruleset/coc7/checks.py`（新） | 唯一检定入口；CoC 数学迁入 | 3 |
| 8 | `agents/tools/__init__.py`、`profiles.py`、`runtime.py` | `build_tool_registry(plugin)`；按房间规则集合并工具；命名冲突校验 | 7 |
| 9 | `combat/registry.py`、`combat/settings.py` | 改为从 ruleset registry 转发；`COMBAT_RULESETS` 跟随 enabled | 7；需等 combat 空闲 |
| 10 | `routes/rooms.py` | 房间写入/校验 `ruleset_id`，摘要输出 | 2；需等房间会话空闲 |
| 11 | `frontend/src/app/character-sheet.ts`、`frontend/src/types/room.d.ts` | 卡面由 schema 驱动渲染（CoC 静态布局保留为兜底） | 5 |
| 12 | `tests/`、`docs/`、`docs/the-veil.wiki/`、`release/changelog/` | 契约测试（参照 `tests/test_combat_rules.py`）、接口文档、变更日志 | 全部 |

## 10. 风险与未决问题

1. **插件加载即任意代码执行**：**已拍板只允许随包内置插件**，不提供运行时加载入口；`data/` 只放纯数据。
2. **旧卡与房间快照**：卡文件与广场卡已完成一次性迁移（补 `rulesetId/sheet`，旧字段保留镜像）；`member.character_card` 快照在下一次保存时自动带 `rulesetId`，历史快照按默认规则读取。
3. **混规则房间**：已定为「一个房间一套规则」；卡 `rulesetId` 与房间不一致时应只读（该校验待房间会话空闲后接入）。
4. **工具名冲突**：`_resolve_tool` 只替换首个下划线，插件若用多级命名空间会解析错；需在加载期强校验。
5. **无 AI 房间**：manual/freeform 没有 AI KP，检定必须由 REST / 房间控制台驱动，因此 `check_service` 不能依赖 `AgentContext`；这是与并行会话最关键的接口约定。
6. **规则与剧本解耦**：插件解析不得读取剧本字段（freeform 房间没有剧本），`resolve(room_info)` 只能看房间级字段。
7. **配置口径**：`[character_rules]` 里的属性骰、技能基础值本质是 CoC 规则，长期应移入插件；一期保留，避免设置页与旧配置断裂。
8. **知识库 adapter**：`COC7Adapter` 已存在但未与插件 id 绑定，二期需统一到同一个 id 下，避免出现两套 `ruleset_id` 语义。
9. **前端硬编码**：`character-sheet.ts` 的 `COC7CoreAttributeKey` 与 `styles/02-scenario-character.css` 的固定布局，schema 化是二期工作；一期可出现「数据通用、界面仍 CoC」的过渡态。
10. **依赖与许可**：插件若引入新依赖，需登记 `THIRD-PARTY-LICENSES.md`。

## 11. 实现状态（2026-11-08）

已落地：

- `backend/trpg_server/ruleset/contracts.py`：`RULESET_API_VERSION`、`RulesetPlugin`、`CheckRequest/CheckResolution` 等协议。
- `backend/trpg_server/ruleset/registry.py`：内置静态注册、`api_version` 门控、`describe()`、`resolve_for_room/scenario()`。
- `backend/trpg_server/ruleset/coc7/`：`schema.py`（信封 + `sheet` 打包与校验）、`catalogs.py`（技能/武器/职业/物品，旧路径兜底）、`checks.py`（判定兼容桥）、`tools.py`（工具归属声明）。
- `backend/trpg_server/ruleset/migrations.py` + `scripts/migrate_ruleset_cards.py`：旧卡一次性迁移，默认 dry-run，`--yes` 执行，幂等。
- `character_service.apply_save()`：落盘前由插件补齐 `rulesetId/rulesetVersion/rulesetSchemaVersion/sheet`。
- `routes/characters.py`：读写透传 `rulesetId`；三个目录接口改走插件并支持 `?ruleset=`；旧 URL 与返回结构不变。
- `routes/rulesets.py` + `GET /api/rulesets`：只读元数据（含被禁用插件的版本原因）。
- `tests/test_ruleset_plugin.py`：注册/版本门控/打包/目录/迁移幂等/保存回写/判定桥/工具组装/战斗转发/检定调度共 11 项。

已执行的数据迁移：10 张角色卡（含 1 张广场卡）全部 `migrated`，重复执行为 `skipped`；迁移后经 `_character_from_storage → _runtime_to_test_character` 读回正常，`attributes` 完整。

2026-11-09 收尾（并行会话占用解除后）：

- `backend/trpg_server/check_service.py`（新）：唯一检定调度入口，输入校验 / 难度与房规阈值 / 结果格式在核心，目标值与成功等级由 `plugin.checks` 提供；`room_rules.roll_check`（REST + 房间控制台）与 `dice.roll_room_check`（AI 工具）都调用它。
- `ruleset/coc7/checks.py`：`roll()` 完整支持难度、调整值、奖励/惩罚骰、可配置大成功/大失败阈值，返回 `CheckResolution`；`lookup_target()` 提供目标值查询。
- `agents/profiles.py`：`CORE_KP_TOOL_NAMES` + `build_kp_tool_names(ruleset_id)`，KP 工具由插件 `tools.tool_names()` 组装；`routes/chat.py` 按房间规则集传入。
- `combat/registry.py`：优先返回 `plugin.combat`；未插件化的兼容实现保留为回退（`dnd5e` 仍只作开发中占位）。
- `routes/rooms.py`：创建 / 切换剧本 / 剧本迁移时持久化 `ruleset_id`；显式选择不可用规则集返回 400；房间摘要输出 `ruleset_id`。旧房间读取时按剧本推断，推断不到回退 `coc7`（不写回）。
- 前端 `character-sheet.ts`：`cardInputFromSchema()` 按 `rulesetId + sheet` 归一化视图模型，顶层字段仅作非空兜底，旧卡不会出现空卡；`rulesetId` 随卡片保存往返。
- 验证：`tests/test_ruleset_plugin.py`、`test_room_modes.py` 新增检定调度、工具组装、战斗转发、房间规则集持久化用例；全量 `pytest` 与 `npm run build:frontend` 见本轮回报。

### 11.1 规则集选择入口（2026-11-09）

- 落点：**剧本编辑器的基础信息区**（`frontend/src/index/fragments/04-editor-modals.html` 的 `#scenarioRulesetOptions`）。理由：已定「规则与剧本绑定」，房间通过剧本继承规则；房间创建页无需重复选择。
- 注册表新增状态模型：`state` = `available` / `development` / `incompatible`，并输出 `selectable`、`reason`、`reason_code`。前端完全按接口渲染，不硬编码规则集 id。
- 「开发中」规则集以 `DeclaredRuleset` 占位登记：不注册为插件、不提供任何能力，只出现在 `/api/rulesets` 清单里，卡片灰色不可选。
- 后端硬校验：`RulesetRegistry.assert_selectable()`；剧本创建/更新经 `scenario_service._normalize_ruleset_id()` 校验，传不可选规则集直接返回 400（`ruleset_in_development` 等），并给出明确原因。
- 缺省与兼容：剧本缺失 `ruleset_id` 时读取默认 `coc7`（不写回文件）；导入剧本落库时由 `scenario_store` 补默认值。
- 验证：`tests/test_ruleset_selection.py` 覆盖状态清单、`/api/rulesets` 接口契约（未登录 401、开发中占位 `selectable=false`）、拒绝选择、创建默认值与更新；`pytest -k "scenario or ruleset"` 共 129 项通过。
- 房间侧：创建房间时固化 `ruleset_id`（显式请求必须 `selectable`，否则 400；剧本房间以剧本规则集为准）；切换剧本 / 剧本迁移同步更新。旧房间读取时按剧本推断，推断不到回退 `coc7`，不写回文件。
- 导入侧：角色卡导入复用 `builtin_registry().ids()`（只含可用插件），`rulesetId=dnd5e` 会被拒绝并返回 `unsupported_ruleset`；剧本导入只做结构化，落库仍走剧本创建接口，因此同样受后端硬校验。

### 11.2 检定数学整块下沉（2026-11-09，S3 / S7 / S8）

- **金样先行**：`tests/test_coc7_check_golden.py` + `tests/fixtures/coc7_check_golden.json` 在搬迁前录制当前行为，覆盖技能 / 属性取值与名称解析、难度归一、8 组目标 × 3 组阈值 × 100 次掷骰的成功等级矩阵、奖励 / 惩罚骰候选、房间检定、SAN 检定、疯狂判定、社交 / 熟人 / 组合检定 / 团体幸运的完整返回结构与文字。
- **批次 1（`fc1d1f2`）**：`SKILL_KEY_LABELS`、属性 / 技能别名、`CHECK_NAME_ALIASES`、判定名解析与目标值查询迁入 `ruleset/coc7/checks.py`；`dice.py` 只保留委托。
- **批次 2（`982049a`）**：`DIFFICULTY_ALIASES`、`DIFFICULTY_LABELS`、`LEVEL_LABELS`、默认大成功 / 大失败阈值、`threshold`、`success_level`、`level_suffix`、房间检定摘要迁入插件；`dice.py` 与 `check_service.py` 不再持有 CoC 常量，仅保留百分骰原语、奖励 / 惩罚骰净额、广播与等待流程。
- **批次 3（`dd01a5d`）**：`coc7/sanity.py` 声明 `check_name` / `madness_check_name`，SAN 计划与骰桌展示由核心读取插件声明；删除 `room_rules.py` 中未使用的 `MADNESS_LOSS_THRESHOLD` 副本。
- 三批均先跑金样逐字一致，再跑全量 `pytest`（1328 passed / 1 skipped），逐批提交。
- **金样未覆盖、仍需注意的路径**：`run_room_economy` 的信用评级消费换算；`combat/rulesets/coc7.py` 与 `agents/tools/combat.py` 内部的 `_success_level` 调用（走同一委托，但未纳入金样）；叙述层 `agents/narrative/*` 与 `agents/tools/narrative.py` 的成功 / 失败词表；前端 `checkTool.ts` 的别名与阈值渲染。

### 11.3 边界复核（2026-11-12）

复核 docs/plans/ruleset-boundary-audit.md 与 coc7-rules-audit.md 的当前状态（只读审计，未改实现）：

- **10 个钩子**：6 个完整（注册表解耦、RollPolicy、SchemaDescriptor、StateKeys、能力降级、Combat 工厂）；4 个部分——`CheckPolicy`（已走 `CheckProvider` + `check_service`）、`ToolBundle`（插件工具已组装，但核心仍持有 `roll_coc_check` / `COC7_EXTRA_TOOLS` 兜底）、版本键（只有卡片信封 + 知识库 `active_version`，没有运行态 `rulesets:{id:version}`）、`COC7Adapter` 默认兜底。
- **收敛目标**：S1/S4/S9/S10 达成；S2/S5/S6/S7 部分达成。
- **§11.2 四项金样缺口**：①`run_room_economy` 已补独立测试（`test_coc7_batch1_rules` / `batch1_runtime`，只读 credit/cash），但未纳入 check 金样矩阵；②`combat/_success_level` 确认委托 `dice._success_level`（`combat/rulesets/coc7.py:17-24`、`agents/tools/combat.py:29-38`），数学被金样间接覆盖，combat 链路本身仍无金样；③叙述层词表仍硬编码（`agents/narrative/extract.py:29,67,392`、`resolver.py:34-36`、`tools/narrative.py:223`）；④`checkTool.ts` 的别名 / 难度仍硬编码（`:3,:47,:73`），仅阈值读房间（`:35-39`），无前端测试。
- **新增回归**：2026-11-09 后在核心模块函数内新增 8 处直接 `import ... ruleset.coc7`（`room_chase.py:29`、`room_interlude.py:25,270`、`room_magic.py:14`、`room_rule_commands.py:258,281,721-862`、`room_sanity.py:25`、`turn_pipeline.py:850`），与「只有 bootstrap 写死插件路径」的约束相悖，「删掉 coc7 后核心仍可启动/导入」尚未达成。
- **后续收敛方案（未做）**：P1 把这 8 处改走 `capabilities` 并加「断开 coc7 后核心可导入」用例；P2 适配器默认空实现 + 核心注册、运行态版本键；P3 补四项金样与前端测试。B2/B14 已在 2026-11-12 的规则整改中修复（奖惩骰统一净额夹 ±2、射击体格差改 `target − attacker`）。
