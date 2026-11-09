# 规则集边界审计：插件能否整块拔出

> 性质：**只读核查 + 收敛方案**，本轮不改代码。
> 方法：假设物理删除 `backend/trpg_server/ruleset/coc7/`、`backend/trpg_server/combat/rulesets/coc7.py`，并在注册表换入一个**只有 `id` / `display_name` 的最小占位插件**（代表另一套规则体系），逐层核对仍然假设 CoC 语义的位置。
> 口径：沿用已定的核心 / 插件分层，见 §1。
> 相关文档：[ruleset-plugin-design.md](./ruleset-plugin-design.md)（设计意图与已落地状态）。

---

## 0. 结论

**插件边界目前是表面边界（名义边界），不是真边界。**

判据不是"有没有插件包"，而是"删掉插件包之后，核心还能不能启动、能不能跑一条最小链路"。实际结果是：

1. **删除即启动失败。** 5 处模块级静态 import 直接指向 `ruleset/coc7` 或 `combat/rulesets/coc7`，其中 `ruleset/registry.py` 与 `combat/registry.py` 位于启动必经路径上，后端进程无法完成导入。
2. **最小占位插件不满足契约。** `RulesetPlugin` 把 `schema` / `catalogs` / `checks` 定为必需，而核心至少有 7 个调用点无条件访问 `.schema.pack()` / `.schema.derive()` / `.catalogs.*`，空插件一进入读路径就 `AttributeError`。
3. **默认规则集写死。** `DEFAULT_RULESET_ID = "coc7"` 被注册表回退、房间创建、剧本归一化、角色卡导入导出、AI 运行时、战斗房规共同引用，换插件后所有"缺省"路径都会回落到一个不存在的 id。
4. **规则数学仍在核心。** 百分骰组合、成功等级、难度阈值、大成功/大失败、技能查值、SAN 与疯狂、派生值、伤害与濒死，全部位于 `agents/tools/dice.py`、`room_rules.py`、`combat` 的调用侧，而不是插件内。
5. **前端整张卡是 CoC 的。** `character-sheet.ts` 的类型、公式、表单、渲染文案、导入导出结构、localStorage 键均为 CoC；`checkTool.ts` 的属性/技能别名表也是 CoC。

一句话概括：**当前是"把 CoC 适配进了一个插件壳"，而不是"核心不认识任何规则"。** 插件化完成度约等于"多了一层可选注入"，还不到"可整块替换"。

有两条边界是**真的**，可以作为后续收敛的样板：

- `combat/scheduler.py`（回合与行动顺序调度）不认具体规则，只调用 `CombatRuleset` 接口。
- 前端 `ScenarioView.ts` 的"运行规则"卡片已经通过 `/api/rulesets` + 注册表状态渲染（不硬编码 id），这是目前唯一一处**按注册表驱动**的 UI。

---

## 1. 审计口径

| 层 | 内容 | 判定 |
| --- | --- | --- |
| **留在核心** | 骰子原语（百分骰 / 多面骰）、奖励骰与惩罚骰机制、成功等级比较、掷骰结果广播与动画、回合与行动顺序调度 | 与规则无关，应**参数化**留在核心，不得塞进某个插件 |
| **必须随插件走** | 理智值与疯狂、克苏鲁神话、技能表与技能点分配、属性集合与派生值（HP/MP/MOV/Build/DB）、护甲与伤害模型、战斗特殊规则、角色卡 schema、判定难度等级、法术与典籍 | 出现 CoC 名词/字段/公式/能力名即判插件 |

补充判定细则（本次实际执行的标准）：

- **核心** ≠ "现在位于核心目录"，而是"换一套规则后语义不变"。同一个函数里既有通用部分又有 CoC 分支，判为**混合**，需要拆。
- 一处代码只要出现下列任一特征，即视为硬编码 CoC 假设：CoC 属性键（STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC）、SAN/MP/HP 派生公式、`克苏鲁神话`/`cthulhumythos`、`critical/extreme/hard/regular/fumble` 等级带、`1/96` 阈值、奖励/惩罚骰上限 2、`1920s` 时代、技能点分配、职业模板、impale/malfunction/重伤/濒死。
- 仅"注释里提到 CoC"不改变判定，但计入清理清单。
- 配置文件里的默认值（`general.toml`、`combat.settings`）同样算硬编码，因为它们是"缺省语义"。

---

## 2. 清单：位置 → 归属 → 核心/插件 → 是否有硬编码假设

### 2.1 契约与注册表

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py#L19) `DEFAULT_RULESET_ID` | 核心 | 核心（值应可配置） | **是**：值为 `"coc7"`，且注释写明"目前只有内置的 CoC7" |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `CheckRequest` | 核心 | 混合 | **是**：`difficulty` 固定 `regular/hard/extreme`；单 `target` 数值模型隐含百分制；`bonus/penalty` 字段名绑定 CoC 术语 |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `CheckResolution` | 核心 | 核心 | 否（`level` 为字符串，尚可承载任意等级带） |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `CharacterSchemaProvider` | 核心 | 核心接口 | 否（接口本身通用） |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `CatalogProvider` | 核心 | 混合 | **是**：`skills` / `occupations` / `weapons` 是按 CoC 角色卡切分的四个方法，强制所有规则都长这四样 |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `CheckProvider` | 核心 | 核心接口 | 否 |
| [contracts.py](../../backend/trpg_server/ruleset/contracts.py) `ToolProvider` | 核心 | 核心接口 | **是**：注释以 `coc7.roll_sanity_check` 为示例 |
| [registry.py](../../backend/trpg_server/ruleset/registry.py#L196-L207) `builtin_registry()` | 核心 | 核心（实现方式错） | **是**：函数体内静态 `from trpg_server.ruleset.coc7 import Coc7Plugin`，并把占位插件硬写在 `declared=` |
| [registry.py](../../backend/trpg_server/ruleset/registry.py#L85-L95) `resolve()` | 核心 | 核心 | **是**：未知 id 一律回退 `DEFAULT_RULESET_ID`；无默认插件时 `RuntimeError` |
| [registry.py](../../backend/trpg_server/ruleset/registry.py#L155-L170) `assert_selectable()` | 核心 | 核心 | **是**：空值即取 `DEFAULT_RULESET_ID` |
| [registry.py](../../backend/trpg_server/ruleset/registry.py) `DeclaredRuleset` / 状态常量 | 核心 | 核心 | 否（这是全仓库最干净的边界设计） |
| [ruleset/__init__.py](../../backend/trpg_server/ruleset/__init__.py) 导出面 | 核心 | 核心 | 否 |

### 2.2 检定与骰子（`agents/tools/dice.py`、`check_service.py`）

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| `DIFFICULTY_ALIASES` / `DIFFICULTY_LABELS` | 核心 dice.py | **插件**（判定难度等级） | **是**：`普通/困难/极难` 三档，其余规则无此概念 |
| `ATTRIBUTE_ALIASES` / `ATTRIBUTE_LABELS` | 核心 dice.py | **插件**（属性集合） | **是**：STR..LUC + 中文别名，且把 `灵感` 映射到 INT |
| `SKILL_KEY_LABELS` | 核心 dice.py | **插件**（技能表） | **是**：约 50 个 CoC 技能键，含 `cthulhumythos` |
| `LEVEL_LABELS` | 核心 dice.py | 混合：比较器留核心、**文字**回插件 | **是**：`大成功/极难成功/困难成功/大失败` 是 CoC 六级带的命名 |
| `DEFAULT_CRITICAL_THRESHOLD=1` / `DEFAULT_FUMBLE_THRESHOLD=96` | 核心 dice.py | **插件**（大成功/大失败规则） | **是** |
| `_threshold(target, difficulty)` | 核心 dice.py | **插件**（难度阈值 ÷2 / ÷5） | **是** |
| `_normalize_bonus_penalty` / `MAX_BONUS_PENALTY=2` | 核心 dice.py | **核心机制**（策略参数化） | **部分是**：抵消逻辑通用，上限 2 是 CoC 口径 |
| `_percentile_roll` / `_percentile_from_faces` | 核心 dice.py | **核心**（骰子原语） | 部分是：固定"个位固定、十位候选"的百分骰组合方式 |
| `_success_level` | 核心 dice.py | 混合：比较器留核心、等级带/阈值回插件 | **是**：内置 critical/extreme/hard/regular/failure/fumble 六带与 `target>=50 时 fumble=100` 特例 |
| `_skill_catalog_names` / `_is_known_check_name` / `_resolve_check_name` | 核心 dice.py | **插件**（技能表） | **是**：直接读 `data/character_skills.json`，且把 `data/character_skills.json` 当全局唯一真理 |
| `_lookup_check_value` | 核心 dice.py | **插件**（角色卡 schema） | **是**：只认 `card["attributes"]` 与 `card["skills"]` 两种结构 |
| `_member_names` / `_active_members` / `_find_member` | 核心 dice.py | 核心 | 否 |
| `_dice_roll_metadata` | 核心 dice.py | 核心（广播/动画） | 部分是：`mode="percentile"` 与骰面结构假设 |
| `_plan_percentile` / `_room_check_plan` / `_coc_check_plan` | 核心 dice.py | **插件** | **是**：plan 结构里直接带难度、阈值、百分骰 |
| `_sanity_check_plan` / `_roll_sanity_value` / `roll_sanity_check` / `roll_madness` / `SAMPLE_PHOBIAS` / `SAMPLE_MANIAS` / `MADNESS_BOUTS` / `_madness_int_value` | 核心 dice.py | **插件**（理智与疯狂） | **是**：整套 SAN 规则在核心 |
| `roll_coc_check` / `roll_room_check` 工具定义与名称 | 核心 dice.py | 核心入口 + 插件数学 | **是**：工具名 `dice.roll_coc_check`、描述里写 COC7 |
| `reveal_trigger_after_check` | 核心 dice.py | 核心 | 否 |
| [check_service.py](../../backend/trpg_server/check_service.py#L70-L72) 难度校验文案 | 核心 | 核心 + 插件难度枚举 | **是**：只接受 regular/hard/extreme/普通/困难/极难 |
| [check_service.py](../../backend/trpg_server/check_service.py#L83-L97) 目标值校验 | 核心 | **插件**（目标值域） | **是**：`base_target > 100` 硬拒绝；错误文案点名 COC7 技能目录与"侦察/聆听/心理学" |
| [check_service.py](../../backend/trpg_server/check_service.py#L104-L113) 默认阈值 | 核心 | **插件** | **是**：回退 `dice.DEFAULT_CRITICAL_THRESHOLD / DEFAULT_FUMBLE_THRESHOLD` |
| [check_service.py](../../backend/trpg_server/check_service.py#L140-L146) 摘要拼接 | 核心 | 混合 | **是**：硬拼"奖励骰/惩罚骰"字样；`difficulty_label` 直接取 `DIFFICULTY_LABELS` |
| [check_service.py](../../backend/trpg_server/check_service.py#L170-L186) 结果元数据 | 核心 | 核心（广播） | 部分是：`mode="percentile"` |

### 2.3 角色卡与数据 API

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| [character_service.py](../../backend/trpg_server/character_service.py#L339-L354) `pack` / `schema` 取法 | 核心 | 核心调度 | 否（但要求插件必须提供 `schema`） |
| [character_service.py](../../backend/trpg_server/character_service.py#L373-L381) `skillCap` / `currentSan` 写回 | 核心 | **插件** | **是** |
| [character_service.py](../../backend/trpg_server/character_service.py#L424) 派生键白名单 | 核心 | **插件** | **是**：`maxHp/currentHp/maxSan/initialSan/currentSan/maxMp/currentMp/armor/mov/build/damageBonus` |
| [routes/characters.py](../../backend/trpg_server/routes/characters.py#L176-L190) `_is_test_character_shape` | 核心 | **插件**（卡 schema） | **是**：以 `deriveAttributes/battleAttributes/characterStatus/skillGroups/...` 判定卡型 |
| [routes/characters.py](../../backend/trpg_server/routes/characters.py#L473-L520) `_test_character_to_runtime` | 核心 | **插件** | **是**：`rulesetId` 默认 coc7、`era` 默认 `1920s`、九个属性、`maxSan` 默认 99 |
| [routes/characters.py](../../backend/trpg_server/routes/characters.py#L585-L589) `pack`；[L764](../../backend/trpg_server/routes/characters.py#L764) `derive` | 核心 | 核心调度 | 否（依赖插件能力） |
| [routes/characters.py](../../backend/trpg_server/routes/characters.py#L828-L859) 三个目录接口 | 核心 | 核心调度 | 否（已按 `?ruleset=` 选插件，是正确示范） |
| [character_card_io.py](../../backend/trpg_server/character_card_io.py#L100) 字段 spec；[L535](../../backend/trpg_server/character_card_io.py#L535) 默认 `coc7` | 核心 | **插件** | **是**：导入导出字段白名单与默认规则集 |
| [weapon_catalog.py](../../backend/trpg_server/weapon_catalog.py) 分类/默认值 | 核心 | **插件**（武器表） | **是**：中文枪械分类、`1D3` 徒手、`impale`、`malfunction`、`接触` |
| [scenario_service.py](../../backend/trpg_server/scenario_service.py#L17-L33)、[scenario_store.py](../../backend/trpg_server/scenario_store.py#L607)、[routes/scenarios.py](../../backend/trpg_server/routes/scenarios.py#L297) | 核心 | 核心（默认值应可配置） | **是**：统一回退 `DEFAULT_RULESET_ID` |
| [config/schema.py](../../backend/trpg_server/config/schema.py#L235-L249) `[character_rules]` | 核心配置 | **插件** | **是**：`attribute_roll_str..luc`、`skill_bases` 是 CoC 车卡口径，且以全局配置而非插件形式存在 |
| [settings.py](../../backend/trpg_server/settings.py#L32-L35) 目录常量 | 核心 | **插件**数据 | **是**：`data/occupations`、`data/weapons`、`data/character_skills.json` 为全局单份 |

### 2.4 房间规则（`room_rules.py`）

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| `MADNESS_LOSS_THRESHOLD = 5` | 核心 | **插件** | **是** |
| `ROOM_ATTRIBUTE_KEYS` / `ATTRIBUTE_ALIASES` | 核心 | **插件** | **是** |
| `resolve_rule_id` / `room_rule_label` | 核心 | 核心 | 部分是：注释"回退 coc7"，实现已走 `get_plugin(None)` |
| `_schema_for` / `derive_card` | 核心 | 核心 | 否（已做异常兜底） |
| `sync_derived_stats` | 核心 | **插件**（状态键映射） | **是**：派生键 `("maxHp","maxSan","initialSan","maxMp","mov","build","damageBonus","armor")`、夹紧三元组 `hp/san/mp` |
| `san_snapshot` | 核心 | **插件** | **是**：注释"99 − 克苏鲁神话值"，写 `card["maxSan"]` |
| `apply_san_change` | 核心 | **插件** | **是**：`current_san` / `mental_status` / 不定性疯狂标记 |
| `apply_skill_change` | 核心 | **插件** | **是**：`skillCap`，注释"克苏鲁神话上升会压低理智上限" |
| `apply_attribute_change` | 核心 | **插件** | **是**：文案列死 STR..LUC |
| `roll_check` | 核心 | 核心（已收敛到 `check_service`） | 部分是：错误文案"技能 / 属性" |
| `_normalize_san_change` / `roll_sanity` | 核心 | **插件** | **是**：`成功/失败` 损失表达式、`sanity_records` / `madness_records` |
| `add_item` / `item_snapshot` / `remove_item` | 核心 | 核心 + **插件目录** | 部分是：`ITEM_KINDS={"item","weapon"}`、武器字段 |
| `pending_changes` / `persist_member_card` / `persist_room_items_on_archive` | 核心 | 核心 | 否（结构通用，是全仓库第二处干净边界） |

### 2.5 战斗

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| [combat/contracts.py](../../backend/trpg_server/combat/contracts.py#L27-L41) `DamageOutcome` | 核心接口 | 混合 | **是**：`major_wound` / `dying` / `unconscious` 是 CoC 状态集，作为**必填字段**进契约 |
| [combat/contracts.py](../../backend/trpg_server/combat/contracts.py#L114) `spell_timing(cast_time, dex)` | 核心接口 | **插件** | **是**：参数名 `dex` |
| [combat/contracts.py](../../backend/trpg_server/combat/contracts.py#L78) `action_budget` 注释 | 核心接口 | 核心 | 注释直接点名 CoC7 / 另一规则集 |
| [combat/registry.py](../../backend/trpg_server/combat/registry.py#L12-L33) | 核心 | 核心（实现方式错） | **是**：静态 import 两个实现、回退表、默认与兜底键 `"coc7"` |
| [combat/rulesets/__init__.py](../../backend/trpg_server/combat/rulesets/__init__.py#L3-L4) | 核心 | 核心（实现方式错） | **是**：包级静态 import |
| `combat/rulesets/coc7.py` | 插件 | **插件** | 是（本身即 CoC 规则，删除目标） |
| `combat/scheduler.py` | 核心 | **核心** | 否（目前唯一干净的规则无关调度） |
| [combat/settings.py](../../backend/trpg_server/combat/settings.py#L18) `COMBAT_RULESETS`；[L37](../../backend/trpg_server/combat/settings.py#L37)、[L70](../../backend/trpg_server/combat/settings.py#L70) 默认值 | 核心配置 | **插件** | **是**：白名单只有 `coc7`，默认 id 与 choice 都写死 |
| [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L32-L39) 静态 import | 核心工具 | 核心（实现方式错） | **是**：直接 import `LEVEL_RANK` / `SKIPPED_STATUS` / `build_and_damage_bonus` |
| [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L47-L58) `UNARMED_WEAPON` | 核心工具 | **插件** | **是**：`格斗（斗殴）`、`fighting`、`brawl`、`1D3`、`impale` |
| [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L336-L337)、[L735-L768](../../backend/trpg_server/agents/tools/combat.py#L735-L768) SAN 快照读写 | 核心工具 | **插件** | **是** |
| [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L163)、[L239](../../backend/trpg_server/agents/tools/combat.py#L239)、[L795](../../backend/trpg_server/agents/tools/combat.py#L795) 默认规则集 | 核心工具 | 核心（默认值） | **是** |
| [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L1728-L1935) impale / malfunction | 核心工具 | **插件** | **是** |
| [routes/combat.py](../../backend/trpg_server/routes/combat.py#L19) 引用 dice 私有百分骰 | 核心路由 | 核心（分层错误） | **是** |

### 2.6 AI 工具与提示词

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| [agents/tools/__init__.py](../../backend/trpg_server/agents/tools/__init__.py#L2) 工具导入面 | 核心 | 核心（实现方式错） | **是**：按 CoC 命名逐个导入 |
| [agents/profiles.py](../../backend/trpg_server/agents/profiles.py#L5-L74) `CORE_KP_TOOL_NAMES` / `DEFAULT_KP_TOOLS` | 核心 | 核心 + 配置 | **是**：默认工具集按 `DEFAULT_RULESET_ID` 构建 |
| [routes/chat.py](../../backend/trpg_server/routes/chat.py#L37) `COMBAT_TOOL_NAMES` | 核心路由 | 核心（依赖方向错） | 部分是 |
| [routes/chat.py](../../backend/trpg_server/routes/chat.py#L919) `rulesets.coc7` | 核心路由 | **插件** | **是**：从房间快照按 `coc7` 键取版本号 |
| [agents/tools/room.py](../../backend/trpg_server/agents/tools/room.py#L368) 引用战斗私有函数 | 核心工具 | 核心（分层错误） | 部分是 |
| [agents/tools/character_stats.py](../../backend/trpg_server/agents/tools/character_stats.py) 工具描述 | 核心工具 | 核心 + **插件文案** | **是**：示例用"侦查"、属性列为 STR..LUC |
| [agents/ruleset_adapters.py](../../backend/trpg_server/agents/ruleset_adapters.py#L40-L52) `COC7Adapter`；[L99-L101](../../backend/trpg_server/agents/ruleset_adapters.py#L99-L101) 默认实例 | 核心 | **插件** | **是**：主题词表含"理智/SAN/疯狂/技能/检定"；注册表默认塞入 `COC7Adapter()` |
| [agents/ruleset_knowledge.py](../../backend/trpg_server/agents/ruleset_knowledge.py#L111) 默认 `coc7` | 核心 | 核心（默认值） | **是** |
| [site_agent_tools/scenarios.py](../../backend/trpg_server/site_agent_tools/scenarios.py#L21) `coc7-authoring.md` | 站点工具 | **插件** | **是** |
| `config/templates/roles/kp.md` / `debug-kp.md` | 提示词 | **插件** | **是**：CoC / 克苏鲁 / 理智 / SAN / d100 / 技能语义 |

### 2.7 前端

| 文件 / 函数 | 当前归属 | 判定 | 硬编码 CoC 假设 |
| --- | --- | --- | --- |
| [character-sheet.ts](../../frontend/src/app/character-sheet.ts) `COC7CharacterCard` / `TestCharacterJson` 类型 | 前端 | **插件** | **是**：整张卡字段 |
| 同文件 `STORAGE_KEY` 等 4 个 localStorage 键 | 前端 | **插件** | **是**：均含 `coc7` |
| 同文件 `ATTRIBUTE_KEYS` / `ATTRIBUTE_LABELS` / `DEFAULT_ATTRIBUTE_ROLLS` | 前端 | **插件** | **是**：九属性 + `3d6x5` / `(2d6+6)x5` |
| 同文件 `BASE_SKILLS` / `TEST_CHARACTER_SKILL_GROUPS` / `SKILL_FILTER_CATEGORIES` | 前端 | **插件** | **是**：含 `cthulhuMythos` |
| 同文件 `calculateMaxHp/San/Mp/Mov/BuildAndDamageBonus` / `calculateHalfAndFifth` / `mythosSkillValue` | 前端 | **插件** | **是**：CoC 派生公式（与后端 `coc7/rules.py` 重复实现） |
| 同文件 `CharacterStatusFlags` / 疯狂三态 / 重伤昏迷死亡 | 前端 | **插件** | **是** |
| 同文件 `renderCharacterCardSummary` / `renderCharacterBasicInfo` / `renderCharacterVitals` / `renderCharacterSkillSection` | 前端 | **插件** | **是**：HP/SAN/MP/MOV、1920s 时代、技能表头"困难50%/噩梦20%" |
| 同文件导入导出（sanity 结构、`calculateMaxSan`） | 前端 | **插件** | **是** |
| [checkTool.ts](../../frontend/src/tools/checkTool.ts#L3-L16) 别名表；[L35-L41](../../frontend/src/tools/checkTool.ts#L35-L41) 阈值回退 | 前端 | **核心机制 + 插件名表** | **是**：1/96、"困难/极难"、技能键 |
| [chat.ts](../../frontend/src/app/chat.ts#L1552-L1561) `check_type === "sanity"` 写回 | 前端 | **插件** | **是** |
| [types/room.d.ts](../../frontend/src/types/room.d.ts) `COC7CharacterCard` / `CharacterRuntimeState` | 前端 | **插件** | **是**：`current_san` / `max_san` / 重伤濒死 |
| [types/scenario.d.ts](../../frontend/src/types/scenario.d.ts) `RulesetOption` | 前端 | 核心 | 否（正确示范） |
| [ScenarioView.ts](../../frontend/src/app/views/ScenarioView.ts#L1005-L1030) 规则卡片 | 前端 | 核心 | 否（拉 `/api/rulesets`，按状态灰卡，**已复核存在**） |
| [locales/zh_cn.json](../../frontend/src/locales/zh_cn.json) 角色卡文案 | 前端 | **插件** | **是**：`克苏鲁调查员角色卡`、`San 值`、`克苏鲁神话` |

### 2.8 测试与夹具

| 文件 | 当前归属 | 判定 | 备注 |
| --- | --- | --- | --- |
| `tests/fixtures/test_character.json`（及 `tests/test_character.json`） | 测试 | **插件夹具** | 结构为 `attributes/deriveAttributes/battleAttributes/characterStatus/skillGroups` |
| `tests/test_coc7_audit_fixes.py` / `test_coc7_batch1_rules.py` | 测试 | **插件测试** | 删插件后应随插件一起移走 |
| `tests/test_character_skills_catalog.py` / `test_occupation_catalog.py` | 测试 | **插件测试** | 依赖 `data/character_skills.json`、`data/occupations` |
| `tests/test_room_rules.py` / `test_room_house_rules.py` / `test_chat_stat_commands.py` / `test_character_ai.py` / `test_agent_tools.py` / `test_character_import.py` / `test_character_export.py` | 测试 | **混合** | 同时覆盖通用调度与 CoC 数值，需拆分：通用断言留核心测试，数值断言下沉插件测试 |
| `tests/test_ruleset_plugin.py` / `test_ruleset_selection.py` | 测试 | **核心** | 已是契约级测试，应扩展为"占位插件可启动"回归 |

---

## 3. 换插件会直接坏掉的位置（按严重程度）

标注：**D** = 数据假设（字段/常量/公式），**C** = 控制流假设（静态 import / 分支 / 默认值），**U** = UI 假设。

### P0 — 进程无法启动（模块导入失败）

| # | 位置 | 类型 | 说明 |
| --- | --- | --- | --- |
| 1 | [ruleset/registry.py](../../backend/trpg_server/ruleset/registry.py#L196-L207) | **C** | 函数体内静态 `from trpg_server.ruleset.coc7 import Coc7Plugin`；`/api/rulesets`、角色卡、房间、AI 全部经此。历史上把 import 放进函数体只规避了循环依赖，没有解除耦合 |
| 2 | [combat/registry.py](../../backend/trpg_server/combat/registry.py#L12-L20) | **C** | 模块级 import 两个战斗实现并构造回退表 |
| 3 | [combat/rulesets/__init__.py](../../backend/trpg_server/combat/rulesets/__init__.py#L3-L4) | **C** | 包级 import，任何 `from trpg_server.combat.rulesets import ...` 都会失败 |
| 4 | [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py#L32-L39) | **C** | 直接 import `LEVEL_RANK` / `SKIPPED_STATUS` / `build_and_damage_bonus`；该模块又被 [agents/tools/__init__.py](../../backend/trpg_server/agents/tools/__init__.py#L20) 与 [routes/chat.py](../../backend/trpg_server/routes/chat.py#L37) 导入 |
| 5 | [agents/tools/__init__.py](../../backend/trpg_server/agents/tools/__init__.py#L2) | **C** | 逐个导入 CoC 命名的工具定义（`ROLL_COC_CHECK_TOOL` 等）。即使这些定义留在核心，命名与集合也应按插件组装 |

> 结论：这不是"某些功能不可用"，而是"服务起不来"。任何"换插件"演练在第一步就中断。

### P1 — 启动后一进入主链路就崩（`AttributeError` / `RuntimeError`）

| # | 位置 | 类型 | 说明 |
| --- | --- | --- | --- |
| 6 | [contracts.py](../../backend/trpg_server/ruleset/contracts.py#L19) + [registry.py](../../backend/trpg_server/ruleset/registry.py#L85-L95) | **D + C** | `DEFAULT_RULESET_ID="coc7"` 指向已删除插件；`resolve(None)` → `get("coc7")` → `None` → `RuntimeError("No built-in ruleset registered")` |
| 7 | 最小占位插件 vs `RulesetPlugin` | **D** | 契约要求 `schema` / `catalogs` / `checks` 三个子对象；只有 `id` / `display_name` 时，下面 8–11 全部失败 |
| 8 | [character_service.py](../../backend/trpg_server/character_service.py#L339-L354) | **D** | `ruleset.schema.pack(...)`、`.schema` 无条件访问 |
| 9 | [routes/characters.py](../../backend/trpg_server/routes/characters.py#L585-L589)、[L764](../../backend/trpg_server/routes/characters.py#L764) | **D** | 同上；`/api/characters` 的 GET/PUT 全挂 |
| 10 | [routes/rooms.py](../../backend/trpg_server/routes/rooms.py#L654-L657) | **D** | `plugin.schema.derive(character_card)` 无兜底 |
| 11 | [migrations.py](../../backend/trpg_server/ruleset/migrations.py#L36-L50) | **D** | `get_plugin(None)` + `.schema.pack` / `.validate`；迁移脚本与批量迁移直接不可用 |
| 12 | [routes/characters.py](../../backend/trpg_server/routes/characters.py#L839-L859) | **D** | `catalogs.occupations/skills/weapons()`；换插件后若插件不提供目录，三个接口 500 |
| 13 | [room_rules.py](../../backend/trpg_server/room_rules.py#L246-L344) | **D** | `derive_card` 有兜底，但 `sync_derived_stats` / `san_snapshot` 之后按 CoC 键名写回，占位插件返回空派生即写 0 / 默认值 |

### P2 — 能跑但静默算错（最危险的一类）

| # | 位置 | 类型 | 说明 |
| --- | --- | --- | --- |
| 14 | [dice.py](../../backend/trpg_server/agents/tools/dice.py#L279-L292) `_success_level` | **D** | 无论哪个规则集，都按 critical/extreme/hard/regular/failure/fumble 六级带 + `target>=50 fumble=100` 计算；换插件后等级语义错误但不报错 |
| 15 | [dice.py](../../backend/trpg_server/agents/tools/dice.py#L40-L41) `_threshold` | **D** | ÷2 / ÷5 是 CoC 难度，其他规则没有该映射 |
| 16 | [dice.py](../../backend/trpg_server/agents/tools/dice.py#L203-L216) `_lookup_check_value` | **D** | 只读 `attributes` / `skills`；换 schema 后查值恒为空 → 检定全部"未找到" |
| 17 | [dice.py](../../backend/trpg_server/agents/tools/dice.py#L123-L183) `_skill_catalog_names` / `_is_known_check_name` | **D** | 用 `data/character_skills.json` 做合法性白名单；换插件后合法检定名会被拒 |
| 18 | [check_service.py](../../backend/trpg_server/check_service.py#L83-L113) | **D** | `>100` 拒绝 + 1/96 默认阈值 + 难度别名 |
| 19 | [room_rules.py](../../backend/trpg_server/room_rules.py#L26) `MADNESS_LOSS_THRESHOLD=5`；[L400-L509](../../backend/trpg_server/room_rules.py#L400-L509) `apply_san_change` | **D** | 把"损失 5 点触发疯狂"当房间通用规则 |
| 20 | [agents/tools/dice.py](../../backend/trpg_server/agents/tools/dice.py#L1037-L1417) `roll_sanity_check` / `roll_madness` | **D** | SAN 规则在核心，且工具按规则集组装后这两件仍会被暴露 |
| 21 | [combat/rulesets/coc7.py](../../backend/trpg_server/combat/rulesets/coc7.py#L13-L24) 的 `LEVEL_RANK` / `SKIPPED_STATUS` 被 [agents/tools/combat.py](../../backend/trpg_server/agents/tools/combat.py) 直接复用 | **D** | 等级排序与"跳过状态"来自 CoC，主循环按它判断谁能行动 |
| 22 | [combat/contracts.py](../../backend/trpg_server/combat/contracts.py#L27-L41) `DamageOutcome` | **D** | 重伤/濒死/昏迷是必填输出，换伤害模型后要么塞假值要么改契约 |
| 23 | [combat/settings.py](../../backend/trpg_server/combat/settings.py#L18-L37) | **D** | 战斗房规白名单与默认 id |

### P3 — UI / 文案层错位（不影响数据，但用户直接可见）

| # | 位置 | 类型 | 说明 |
| --- | --- | --- | --- |
| 24 | [character-sheet.ts](../../frontend/src/app/character-sheet.ts) 全量 | **U + D** | 换插件后卡面仍显示属性九宫格、SAN/MP、技能点、疯狂状态；`rulesetId` 保存往返已通，但没有第二套渲染路径 |
| 25 | [checkTool.ts](../../frontend/src/tools/checkTool.ts#L3-L16) | **U** | 斜杠命令 `/check 玩家 技能 困难` 的解析与别名表 |
| 26 | [chat.ts](../../frontend/src/app/chat.ts#L1552-L1561) | **U** | `check_type === "sanity"` 时才写回；换插件后该分支永不命中，或命中错误语义 |
| 27 | [locales/zh_cn.json](../../frontend/src/locales/zh_cn.json) | **U** | 页面标题、副标题、San/克苏鲁神话等文案 |
| 28 | [kp.md](../../config/templates/roles/kp.md) / `debug-kp.md` | **U（提示词）** | KP 被要求按 CoC 术语行动；换插件后叙事口径错误 |
| 29 | [routes/chat.py](../../backend/trpg_server/routes/chat.py#L919) `rulesets.coc7` 版本号 | **D** | 取不到就回退 `"1"`，规则版本信息静默丢失 |
| 30 | [config/schema.py](../../backend/trpg_server/config/schema.py#L235-L249) `[character_rules]` | **D** | 管理端"车卡规则"页仍是 CoC 属性骰与技能基础值 |

> 严重度排序依据：P0 影响"能否启动"，P1 影响"核心 CRUD 是否可用"，P2 影响"数值是否正确且不被发现"，P3 影响"用户看到的语义"。

---

## 4. 收敛改动方案（不写代码，只给方案）

### 4.1 目标形态

三层，边界各自可验证：

| 层 | 职责 | 验收方式 |
| --- | --- | --- |
| **核心内核**（不认识任何规则） | 骰子原语、候选集与选择策略、等级比较器、结果广播/动画、房间成员与权限、待写回队列、回合调度、注册表与生命周期 | 删除所有插件后，只加载一个满足契约的空插件，服务能启动、能建房、能掷普通骰、能存取一张"任意 JSON 卡" |
| **规则插件** | 角色卡 schema、属性与派生、技能/职业/武器/法术目录、判定难度与等级带、SAN 等专属状态、战斗规则、AI 工具与提示词片段 | 每个插件可独立测试；删除某个插件不影响其他插件与核心 |
| **前端契约层** | 按插件提供的 `SchemaDescriptor` 渲染，不认识具体字段 | 用两套不同 schema 的卡渲染，均不空白、字段不丢 |

### 4.2 要移到插件的东西

按"必须整块搬"的顺序：

1. **理智值与疯狂**：`dice._sanity_check_plan` / `_roll_sanity_value` / `roll_sanity_check` / `roll_madness` / `SAMPLE_PHOBIAS` / `SAMPLE_MANIAS` / `MADNESS_BOUTS` / `_madness_int_value`，以及 `room_rules.san_snapshot` / `apply_san_change` / `_normalize_san_change` / `roll_sanity` / `MADNESS_LOSS_THRESHOLD`。
2. **克苏鲁神话与技能表**：`MYTHOS_SKILL_NAMES`、`SKILL_KEY_LABELS`、`_skill_catalog_names`、`_is_known_check_name`、`skillCap` 相关逻辑、技能点分配（前后端各一份）。
3. **属性集合与派生值**：`ATTRIBUTE_ALIASES` / `ATTRIBUTE_LABELS` / `ROOM_ATTRIBUTE_KEYS`、`derive_sheet` 全部公式、`character_service.py:424` 派生键白名单。核心只保留"把插件给出的派生值写进卡"的通用写回。
4. **判定难度等级与大成功/大失败**：`DIFFICULTY_ALIASES` / `DIFFICULTY_LABELS`、`_threshold`、`DEFAULT_CRITICAL_THRESHOLD` / `DEFAULT_FUMBLE_THRESHOLD`、`LEVEL_LABELS` 的文字部分。
5. **护甲与伤害模型、战斗特殊规则**：`UNARMED_WEAPON`、impale / malfunction、重伤-濒死-昏迷判定、`LEVEL_RANK` / `SKIPPED_STATUS`、`build_and_damage_bonus`、`spell_timing` 的 `dex` 参数语义。
6. **角色卡 schema**：`_is_test_character_shape`、`_test_character_to_runtime`、`character_card_io` 字段白名单、`config/schema.py [character_rules]`。
7. **法术与典籍**：目前只有 `catalogs` + 提示词里的散落描述，应收进插件目录与工具片段。
8. **提示词**：`kp.md` / `debug-kp.md` 中的 CoC 术语改为"从插件取提示词片段"，核心提供通用 KP 骨架。

### 4.3 要参数化留在核心的东西

| 核心能力 | 现状问题 | 参数化方向 |
| --- | --- | --- |
| 骰子原语 | `_percentile_roll` 写死百分骰组合 | 抽象为"给定骰面数与取值域，生成 N 个候选面"；百分骰、多面骰、单骰都走同一原语 |
| 奖励骰 / 惩罚骰 | `_normalize_bonus_penalty` 与 `MAX_BONUS_PENALTY=2` 混在一起 | 核心保留"正负相抵 + 生成候选集 + 调选择器"三步；**抵消上限**与**候选生成方式**由 `RollPolicy` 提供 |
| 成功等级比较 | `_success_level` 内置六级带与 CoC 特例 | 核心提供"按插件给的 `level_bands` 从高到低匹配"的比较器；等级名与阈值由 `CheckPolicy` 提供 |
| 掷骰广播与动画 | 元数据写死 `mode="percentile"` | 元数据携带 `dice_faces` / `candidates` / `chosen` / `policy`，前端按通用结构渲染，不需要知道是百分骰还是 d20 |
| 回合与行动顺序 | `scheduler.py` 已正确；但行动经济与跳过状态来自 CoC | 保持核心调度；`actions_per_round` / `is_incapacitated` / 先攻并列处理继续走 `CombatRuleset` 接口，把 `LEVEL_RANK` / `SKIPPED_STATUS` 收进接口返回值 |
| 成员定位与权限 | 已通用 | 不动 |
| 待写回队列 | 已通用 | 不动；只把"变更类型"从固定枚举改为插件声明的状态键 |

### 4.4 需要新增的插件钩子

按优先级排列（前三个是解除 P0/P1 的最小集合）：

1. **注册表解耦（P0 必需）**
   - 注册改为"内置清单 + 显式构造"：核心只保留一个空清单与 `register()`，插件在各自的包入口导出 `PLUGIN`；核心不得 import 任何具体插件模块。
   - `default_ruleset_id` 从常量改为可配置项（注册表初始化参数 / 配置文件），未配置时取"第一个可用插件"，而不是字面量。
   - `declared`（灰卡占位）也改为配置/插件自述，不写在核心函数体里。
2. **`RollPolicy`（骰子策略钩子）**
   - 输入：骰面定义、修正来源（难度、环境、装备）。
   - 输出：需要掷几颗、每颗的取值域、候选集、选择规则（取最小/取最大/取某一位组合）。
   - 核心负责：产生随机数、组装候选集、按选择规则得到结果、产出广播元数据。
3. **`CheckPolicy`（判定策略钩子）**
   - 目标值来源（从卡上哪个结构取）。
   - 难度枚举与阈值映射。
   - 等级带（名称 + 判定条件 + 排序），替代 `_success_level` 内的硬编码分支。
   - 大成功/大失败的适用条件与文案。
   - 目标值域校验（替代 `>100` 硬拒绝）。
4. **`SchemaDescriptor`（前端渲染契约）**
   - 分组（基本信息 / 数值 / 技能 / 装备 / 状态）、每组字段的键、标签、类型、只读性、可编辑控件类型、派生显示规则。
   - 前端按描述渲染，不再出现 `COC7CharacterCard` 这种整卡类型；localStorage 键改为 `ai-trpg:character-cards:<rulesetId>`。
   - 与已有 `rulesetId + sheet` 归一化衔接：`sheet` 是数据，`SchemaDescriptor` 是渲染说明。
5. **`StateKeys`（运行时状态键映射）**
   - 插件声明"当前值 / 上限 / 状态标记"分别叫什么键（例如当前实现里的 `current_san` / `max_san` / `mental_status`）。
   - `sync_derived_stats`、`character_service` 的派生键白名单、战斗快照读写都改为按映射访问，不再写死 `hp/san/mp`。
6. **可选能力声明**
   - `sanity`、`combat`、`tools`、`catalogs` 改为可选能力：核心用 `getattr(plugin, "sanity", None)` 判断，缺失时相关工具不注册、相关 API 返回"该规则集不支持"，而不是崩溃。
   - 这样最小占位插件（只有 `id` / `display_name`）也能满足契约跑通"建房 + 掷骰 + 存取任意 JSON 卡"。
7. **`ToolBundle`（工具按插件组装）**
   - 核心只保留与规则无关的工具（掷骰、房间、场景、触发器、记忆）。
   - CoC 命名工具（`dice.roll_coc_check`、`san.*`）由插件声明；核心工具名统一为 `dice.roll` / `check.roll` 这类中性名，`roll_coc_check` 作为插件别名。
8. **`CombatRuleset` 工厂化**
   - `combat/registry.py` 只从主注册表取 `plugin.combat`，删除回退表与静态 import；无 `combat` 能力时返回 `None`，调用侧给"该规则集暂不支持战斗"。
   - `LEVEL_RANK` / `SKIPPED_STATUS` / 徒手武器 / impale 全部走接口返回值或插件目录。
9. **规则版本键**
   - 房间快照里的 `rulesets: {coc7: "1"}` 改为 `rulesets: {<当前 ruleset_id>: version}`，由 `resolve_rule_id` 的结果决定键。
10. **知识适配器注册**
    - `agents/ruleset_adapters.py` 的 `RulesetRegistry` 默认实例改为空集合 + 插件自注册，删除 `COC7Adapter()` 兜底。

### 4.5 奖励骰 / 惩罚骰：核心机制如何被不同规则调用

这是本次分层里最容易做错的一处，单独说明。

**核心提供的能力（规则无关）**

1. **骰面生成**：给定"几颗骰子、每颗几面、是否同一组"，产出原始骰面。
2. **候选集构造**：按策略把原始骰面组合成若干个"可能的最终结果"。
3. **修正合成**：把来自难度、环境、装备、房规的修正合并成一个修正描述（正负相互抵消）。
4. **选择器**：从候选集里按策略挑一个（取小、取大、取指定位、取最接近目标值等）。
5. **广播**：把 `原始骰面 / 候选集 / 被选中值 / 策略名 / 修正来源` 一起下发，前端动画与文本都按这五元组渲染。

**CoC 插件的调用方式**

- 骰面：1 颗个位骰（0–9）+ N 颗十位骰（0–9）。
- 修正合成：奖励颗数与惩罚颗数先相抵，净额上限 2。
- 候选集：个位固定，每个十位组合成一个 1–100 的候选值（00 + 0 记 100）。
- 选择器：净奖励取最小候选，净惩罚取最大候选。
- 展示：文案"奖励骰/惩罚骰（候选…），取 X"。

**另一套规则体系可能只用到核心的另外几个参数**

- 骰面：N 颗同面骰（例如两颗同面骰）。
- 修正合成：`优势` → 增加候选颗数；`劣势` → 增加候选颗数但换选择方向；两者同时存在时按插件规则归零。
- 候选集：就是每颗骰子的面值本身。
- 选择器：取最大或取最小。
- 展示：文案用插件提供的模板，核心不带"奖励骰"字样。

**关键结论：核心不认识"十位""个位""优势""奖励"任何一个词。** 它只认识"候选集 + 选择器 + 修正合成规则"。当前实现把这四件事全压在 `_percentile_roll` 与 `_normalize_bonus_penalty` 两个函数里，所以看起来像 CoC 专属；拆开之后，这两个函数会变成 `RollPolicy` 的两个默认实现，CoC 只是"注册了百分骰策略的插件"。

同理适用于成功等级：核心只做"从高到低匹配插件给的等级带"，CoC 插件注册 `critical/extreme/hard/regular/failure/fumble` 六带与 `target>=50` 的 fumble 特例；另一套规则注册自己的等级带即可。

### 4.6 迁移映射（整块搬 vs 就地改）

| 对象 | 处理方式 | 原因 |
| --- | --- | --- |
| `ruleset/coc7/` 全部 | **整块搬**（已在插件包内，只需把外层依赖摘掉） | 已是插件形态，问题是外层仍然认识它 |
| `combat/rulesets/coc7.py` | **整块搬** | 同上；问题在 `combat/registry.py`、`agents/tools/combat.py` 的静态引用 |
| `dice.py` 的 SAN / 疯狂 / 技能目录 / 难度数学 | **整块搬**（拆到插件，核心保留骰子原语） | 混合文件，拆比就地改安全 |
| `dice.py` 的 `_percentile_roll` / `_normalize_bonus_penalty` | **就地小步改**（抽出策略参数，默认行为不变） | 核心机制，不能搬走；先参数化再迁调用方 |
| `dice.py` 的 `_success_level` | **就地小步改**（先接受外部等级带，默认仍用六级带） | 同上 |
| `check_service.py` | **就地小步改**（难度枚举、目标值域、阈值、摘要文案改为问插件） | 已是唯一入口，改动集中、可回滚 |
| `room_rules.py` | **混合**：`sync_derived_stats` / `san_snapshot` / `apply_san_change` / `apply_skill_change` / `roll_sanity` **整块搬**；`resolve_member` / `pending_changes` / `persist_*` **不动** | 前者是规则，后者是调度与落盘 |
| `character_service.py` | **就地小步改**（派生键白名单改为按 `StateKeys`） | 影响面大，逐键替换 |
| `routes/characters.py` | **就地小步改**（默认 `rulesetId` / `era` / 属性映射改为问插件） | 路由不能整体重写，先摘默认值 |
| `character_card_io.py` | **就地小步改**（字段 spec 改为由插件提供） | 导入导出格式有历史包袱，需要兼容 |
| `weapon_catalog.py` | **整块搬**到插件目录能力 | 整套是 CoC 武器表 |
| `config/schema.py [character_rules]` | **就地小步改**（字段保留，语义改为"由插件解释"） | 管理端配置有迁移成本 |
| `combat/registry.py` / `rulesets/__init__.py` | **就地小步改**（删除静态 import 与回退表） | 改动小、收益最大，是 P0 首刀 |
| `agents/tools/combat.py` | **整块搬**（徒手武器、impale、伤害状态） + **就地改**（import 与默认 id） | 文件巨大，先解 import，再迁规则 |
| `frontend/character-sheet.ts` | **整块搬**（CoC 卡面进插件目录/独立模块） + 新增通用渲染器 | 单文件数千行，就地改风险最高 |
| `frontend/checkTool.ts` | **就地小步改**（别名表与阈值改为读房间规则集） | 交互入口，改动可控 |
| `locales/zh_cn.json` | **就地小步改**（通用文案留核心，规则文案进插件命名空间） | 纯文案 |
| `tests/fixtures/test_character.json` 等 | **整块搬**（进插件测试目录） | 夹具即规则 |
| `tests/test_ruleset_plugin.py` | **就地小步改**（增加"空插件可启动"用例） | 这是本次审计的回归护栏 |

---

## 5. 风险与工作量

工作量用"改动面"衡量（S = 单文件局部；M = 多文件但模式统一；L = 跨层整块迁移），不代表日历时间。

| 阶段 | 内容 | 方式 | 工作量 | 风险 | 前置 |
| --- | --- | --- | --- | --- | --- |
| **S1 注册表解耦** | 删 5 处静态 import；`default_ruleset_id` 可配置；空插件可启动 | 就地小步改 | M | **低**：机械改动，有明确验收（删 coc7 后能启动） | 无 |
| **S2 契约放宽** | `schema` / `catalogs` / `checks` 改可选能力；调用侧补 `.getattr` 与"不支持"响应 | 就地小步改 | M | **中**：所有调用点要逐个加兜底，漏一处就是 500 | S1 |
| **S3 检定策略** | 引入 `RollPolicy` + `CheckPolicy`；`dice._success_level` / `_threshold` / 目标值域 / 摘要文案改为问插件 | 混合 | L | **高**：检定是主链路，数值回归风险最大；必须保留"默认策略 = 现状"的开关 | S2 |
| **S4 状态键映射** | `StateKeys`；`sync_derived_stats` / `character_service` / 战斗快照改为按映射 | 就地小步改 | M | **中**：字段别名多（`currentSan` / `current_san`），容易出现"写进去读不出" | S2 |
| **S5 SAN 搬出** | 理智/疯狂整块迁插件；房间层只留调用与留痕 | 整块搬 | L | **中高**：涉及房间快照与待写回队列，上一轮刚改过，必须保留现有标记逻辑 | S3 + S4 |
| **S6 角色卡 schema + 前端** | `SchemaDescriptor`；通用卡面渲染器；CoC 卡面下沉 | 整块搬 + 新增 | **L（最大）** | **高**：`character-sheet.ts` 数千行，导入导出、localStorage、迁移都要兼容；易出空卡 | S4 |
| **S7 战斗解耦** | `combat/registry` 去静态 import；`DamageOutcome` 软化；徒手/impale/伤害状态迁插件 | 混合 | L | **中高**：状态机语义多，战斗主循环依赖等级排序 | S2 |
| **S8 工具与提示词** | 工具按插件组装；核心工具改中性名；提示词片段化 | 就地小步改 | M | **中**：工具名变更影响前端斜杠命令与已有房间历史 | S3 |
| **S9 默认值与配置** | `character_card_io` / `scenario_*` / `chat.rulesets.coc7` / `[character_rules]` / `combat.settings` | 就地小步改 | S | **低**：改动分散但机械 | S1 |
| **S10 测试与回归** | 夹具迁插件；通用断言与数值断言拆开；新增"空插件启动 + 最小链路"用例 | 混合 | M | **低**：只要能跑起来就有护栏 | S1–S9 并行 |

**可以就地小步改的（优先做）**：S1、S2、S4、S9、S8 的一部分。它们不改数值语义，只改"谁提供参数"，可以逐个合并、逐个验证。

**必须整块搬的（不能混在别的改动里）**：S5（SAN）、S6（角色卡 + 前端）、S7 的规则部分、`weapon_catalog`、`dice.py` 的技能目录与难度数学。混做会导致回归无法定位。

**最高风险点**：S6。前端卡面同时承载"渲染 + 计算 + 导入导出 + localStorage + 迁移"，且已有"旧卡一次性迁移 + 顶层字段镜像"的约束，任何 schema 化渲染都必须先保证"不空卡、不丢字段"。

**第二风险点**：S3。检定是所有链路的共同下游，`_success_level` 一旦参数化出错，影响面覆盖房间控制台、REST、AI 工具三处。

---

## 6. 本轮不做的内容

- 不改任何代码、不改测试、不动数据。
- 不实现最小占位插件；本文只把它当作审计探针。
- 不新增任何具体规则体系的功能性内容；文中"另一套规则体系"仅指抽象能力差异。
- 不调整现有 `dnd5e` 占位的任何行为（仍为开发中、不可选、后端拒绝）。
- 不评估性能与并发；本次只判断边界与依赖方向。

---

## 7. 复核记录

- [ScenarioView.ts](../../frontend/src/app/views/ScenarioView.ts#L1005-L1030) 的"运行规则"卡片**确认存在**：`#scenarioRulesetOptions` 容器在 [04-editor-modals.html](../../frontend/src/index/fragments/04-editor-modals.html#L73)，选项通过 `/api/rulesets` 拉取，按注册表状态渲染可选/灰卡。审计中不以"前端没有规则选择入口"为前提。
- `/api/character-catalogs/*` 三个接口**确认已按 `?ruleset=` 选插件**（[routes/characters.py](../../backend/trpg_server/routes/characters.py#L828-L859)），属于已有的正确边界，不在整改清单内。
- `combat/scheduler.py` **确认规则无关**，是目前唯一可直接复用的核心调度样板。
- `room_rules.py` 的 `pending_changes` / `persist_member_card` / `persist_room_items_on_archive` **确认规则无关**，整改时不得改动其结构。
