# CoC 7e 章节级缺口实施路线图

> 状态：**只出计划，不写代码、不改配置、不改 `coc7-rules-audit.md` 的正文结论**。
> 来源：第二版审查报告 §4「逐章新增」的口径，范围仅限本轮点名的章节级缺口。
> 前置共识：SAN 初始值 = POW，上限 = 99 − 克苏鲁神话，钳制由另一条线实现，本计划不再涉及。
> 插件边界：规则数学、表数据、校验优先落在 `backend/trpg_server/ruleset/coc7/`；`check_service` 只做调度；`room_rules` / `character_service` 只做快照与持久化适配；`agents/tools`、`room_rule_commands` 只做薄封装；前端只负责呈现。参见 [ruleset-plugin-design.md](ruleset-plugin-design.md)。

## 1. 排序原则

不按章节顺序，按**依赖 → 风险 → 可验证性**排：

1. **先做不改结构的纯函数**：可单测、无迁移、马上能用。
2. **再做只加记录的成长主线**：它是 90%+ 技能恢复、训练、幕间结算的前置；只新增可选字段，风险可控。
3. **然后做 SAN 状态结构**：需要改角色卡结构、加时间与来源追踪，属于第一次真正的 schema 迁移；放在成长之后，避免同时开两个迁移面。
4. **再做战斗流程**：战斗状态最脆，且部分规则依赖 HP/房间时钟；必须放在规则数学与记录层稳定之后。
5. **最后做追逐与法术库**：新子系统/内容层，投入最大、耦合最低，不应阻塞前面的核心修复。

风险标记：**L** = 纯规则数学或查询，无持久化结构变更；**M** = 新增可选字段/记录，向后兼容；**H** = 改角色卡结构或战斗流程，需要迁移与兼容。

## 2. 批次总览

| 批次 | 内容 | 主要风险 | 为什么在这个位置 |
| --- | --- | --- | --- |
| 0 | 契约与字段命名冻结 | L | 先统一 `extensions` 键、版本号与迁移策略，后续不反复改结构。 |
| 1 | 社交难度、组合技能、团体幸运、信用评级、熟人、年龄/EDU | L/M | 纯计算或只读现有字段，能先交付且可单测。 |
| 2 | 技能成长主线 + 90%+ SAN + 训练 | M | 只加记录；是幕间与恢复规则的前置；比 SAN/战斗迁移简单。 |
| 3 | SAN 状态结构化 + 首次神话 + 习惯恐惧 + 现实认知 + 治疗 + 症状/恐惧表 | H | 第一次改角色卡结构与时间模型，必须独立成批。 |
| 4 | 战斗细节（瞄准/掩体/近战误伤/击晕/部位/毒剂/自然恢复）+ 房规前端入口 | H（UI 为 L） | 战斗流程最脆，依赖战斗状态与 HP 持久化，放在后面。 |
| 5 | 追逐整章、法术库与典籍 | H | 新子系统与内容层，投入最大、可并行但不阻塞核心。 |

## 3. 批次详情

### 批次 0：契约冻结（L，无功能）

- 在 `ruleset/coc7/schema.py` 的 `sheet.extensions` 下预声明命名空间：`growth`、`mental`、`social`、`economy`、`contacts`、`training`、`age`、`chase`、`magic`。
- 每个命名空间带 `schemaVersion`；只增字段，不删字段；旧卡读取时按缺省值补全，不批量改写文件。
- 明确“规则数学在插件、调度在核心、持久化在适配层”的边界，后续条目不再重复讨论落点。

### 批次 1：纯函数与低耦合（L/M）

| 项 | 模块（插件内优先） | 配置项与默认值 | 存档/快照影响 | AI 工具或命令 | 风险 |
| --- | --- | --- | --- | --- | --- |
| 社交技能难度 | `ruleset/coc7/social.py`：按对方社交/心理学取高者、态度 ±1–2 级，输出建议难度 | `social.auto_difficulty=true`、`social.attitude_modifier=true` | 无 | 检定结果附带 `suggested_difficulty`；可选 `check.social` | L |
| 组合技能检定 | `ruleset/coc7/checks.py` + `check_service.run_combined_check`：一次掷骰对多技能，全部成功或任一成功 | `checks.combined_mode=all`、`checks.combined_max_skills=3` | 无 | `check.combined`；`rule check --skills=A,B --mode=all` | L |
| 团体幸运检定 | `check_service.run_group_check`：取在场最低幸运者 | `checks.group_luck_pick=lowest` | 无 | `check.group`；`rule check --group=luck` | L |
| 信用评级开支 | `ruleset/coc7/economy.py`：生活标准、消费限额、现金/资产折算、幕间 CR 七档 | `economy.enabled=true`、`economy.credit_tiers=book` | 只读现有 `creditRating/cash/spendingLevel`；新增 `economy.history` 可选日志 | `character.spend`；`rule economy spend` | L/M |
| 熟人 | `ruleset/coc7/contacts.py`：把 `friends` 转成一次性帮助/信息/借物修正 | `contacts.enabled=true`、`contacts.bonus_dice=1` | 只读现有 `friends`；不新增结构 | `character.contacts`；KP 裁定 | L |
| 年龄与 EDU 增强 | `ruleset/coc7/creation.py` + 创建向导：20/40/50/60 岁 EDU 增强，STR/CON/DEX/APP/MOV 分段修正 | `creation.age_adjustments=book`、`creation.edu_enhancement=book` | 只改创建时的属性值；记录 `extensions.age.profile` | 创建页步骤；可选 `character.apply_age` | M |

> 这一批先做的原因：全部是“给定输入算输出”，不碰房间快照和战斗；组合、团体、社交难度也会被后续 SAN/战斗复用。

### 批次 2：成长主线（M）

| 项 | 模块 | 配置项与默认值 | 存档/快照影响 | AI 工具或命令 | 风险 |
| --- | --- | --- | --- | --- | --- |
| 成长标记与幕末成长检定 | `ruleset/coc7/growth.py`：成功使用打勾；幕末 1D100 > 当前值或 >95 则 +1D10 | `growth.enabled=true`、`growth.strict_marks=true` | 新增 `extensions.growth.marks[]`；房间快照增加 `growth`；归档走 `pending_changes` | `character.experience_check`；`rule growth` | M |
| 成长排除项 | 同上：使用奖励骰、花费幸运、对抗失败者不发标记；神话与信用评级永不成长 | `growth.strict_marks=true` | 同上；检定记录增加 `growthEligible` 与原因 | 无独立工具，随检定返回 | M |
| 90%+ 技能幕间恢复 | `growth.py` 末尾：技能达 90%+ 时 SAN +2D6 | `growth.san_bonus_over_90=true` | 走现有 SAN 写回链，不新增结构 | 随 `rule growth` 结算 | L（依赖批次 2 第一项） |
| 训练 | `ruleset/coc7/training.py`：幕间花时间训练，KP 批准 | `growth.training_enabled=false`（可选）、`training.time_unit=week` | 新增 `extensions.training.records[]`，可选 | `character.train`；`rule train` | M |

> 依赖提示：成长要允许技能超过 100；审查报告 B13 已记录“技能被 100 封顶”。如果 B13 不在同一轮修，成长会在 100 处停住，需要在本批验收里明确这一限制。

### 批次 3：SAN 状态与恢复（H）

| 项 | 模块 | 配置项与默认值 | 存档/快照影响 | AI 工具或命令 | 风险 |
| --- | --- | --- | --- | --- | --- |
| `mental_status` 结构化 | `ruleset/coc7/sanity.py`：`phobias[]`、`manias[]`、`bout{type,start,expires}`、`habituations{}`、`realityImmunity` | `sanity.state_version=2` | **改角色卡结构**：旧 `background.phobiasManias` 文本惰性迁移；快照保留未知字段 | 扩展现有 `san.*` 工具 | H |
| 首次目击神话 +5/+1 | `sanity.py` + 技能变更：首次因神话疯狂 +5 神话，之后 +1 | `sanity.mythos_on_madness=true` | 写 `skills.克苏鲁神话` 与 `mental_status`；走技能写回链 | 随 `san.roll_madness` 自动结算 | M |
| 习惯恐惧 | `sanity.py`：按来源记录最大损失，达上限后免疫；幕间上限 −1 | `sanity.habituation=true` | 新增 `mental_status.habituations`；快照需带该字段 | `san.habituation`；`rule san habituation` | M |
| 现实认知 | `sanity.py`：失败失 1 SAN 并发作，成功看破并免疫到下次损失 | `sanity.reality_check=true` | 新增 `mental_status.realityImmunity` | `san.reality_check` | M |
| 心理治疗与疗养 | `ruleset/coc7/treatment.py`：每月精神分析检定，成功 +1D3，大失败 −1D6；不定性需 1D6 月 | `sanity.treatment=book`、`sanity.asylum=true` | 新增 `extensions.mental.treatment` 记录；SAN 走现有链 | `rule san treat`；`san.treat` | M |
| 总结症状表 | `ruleset/coc7/data/bout_summary.json` + `sanity.py`：不定性疯狂的 1D10 小时表 | `sanity.bout_tables=book` | 只读数据；发作写入 `mental_status.bout` | 随 `san.roll_madness` 自动抽取 | L/M |
| 恐惧症/躁狂症 100 条表 + 惩罚骰 | `data/ruleset/coc7/catalogs/phobias.json`、`manias.json` + `sanity.py` | `sanity.trait_tables=full`、`sanity.phobia_penalty=1` | 结构化写入 `mental_status`；不再拼文本 | 随发作抽取；检定入口读 `exposureTag` | M |

> 这一批必须独立：它会真正改角色卡结构与时间模型；先做结构化，再做治疗、习惯、现实认知，避免在旧文本字段上叠逻辑。

### 批次 4：战斗细节（H，UI 入口为 L）

| 项 | 模块 | 配置项与默认值 | 存档/快照影响 | AI 工具或命令 | 风险 |
| --- | --- | --- | --- | --- | --- |
| 战斗房规前端入口 | 房间设置页，复用现有 `rooms.py` 的 combat schema | 现有键；无新默认 | 无 | 无 | L |
| 瞄准跟踪 | `ruleset/coc7/combat_rules.py` + 战斗状态：声明、下一轮生效、受伤/移动失效 | `combat.aim_tracking=true` | 只改战斗会话状态，不改角色卡 | 扩展 `combat.attack` 或 `combat.shoot` 参数 | H |
| 掩体与闪避 | 同上：目标闪避成功则攻击者失去下次攻击；掩体修正 | `combat.cover_dodge=true` | 战斗会话状态 | `combat.shoot` 增加 `cover` 语义 | H |
| 射击入近战误伤 | 同上：近战惩罚骰；大失败按幸运最低者误伤 | `combat.friendly_fire=true` | 战斗会话状态；可能写 HP | `combat.shoot` 增加 `in_melee` | H |
| 击晕 | `combat_rules.py`：钝器战技成功击晕并只造成 1 点伤害 | `combat.knockout=false`（可选） | 新增 `status.stunned`；战斗状态 | `combat.knockout` | H |
| 部位命中 | `combat_rules.py` + 1D20 表：部位与护甲联动 | `combat.hit_locations=false`（可选） | 战斗会话状态；可能写重伤部位 | `combat.hit_location` | H |
| 毒剂 | `ruleset/coc7/poison.py`：剂量、发作、抗性 | `combat.poison=true` | 新增 `status.poisoned`；可能写 HP | `combat.poison`；`rule combat poison` | M/H |
| 自然恢复 | `ruleset/coc7/healing.py` + 房间时钟：按天/周恢复，重伤与休养修正 | `healing.natural_rate=book`、`healing.require_rest=true` | 写 HP；依赖 HP 持久化链 | `rule heal natural`；`combat.heal` 扩展 | H |

> 依赖提示：自然恢复与“战斗伤害写回角色卡”强相关；审查报告 B16 已记录 HP 未进入 `pending_changes`。若 B16 不在本轮修，自然恢复只能停留在房间快照，换房/归档会丢。战斗细节默认全部走房规开关，未开启时保持现有行为。

### 批次 5：追逐与法术（H，独立）

| 项 | 模块 | 配置项与默认值 | 存档/快照影响 | AI 工具或命令 | 风险 |
| --- | --- | --- | --- | --- | --- |
| 追逐整章 | `ruleset/coc7/chase.py` + 房间会话状态：抽象距离、障碍、动作、冲突 | `chase.enabled=false` | 新增房间 `chase` 会话段；不改角色卡 | `chase.start/next/conflict`；`rule chase` | H |
| 法术库与典籍 | `ruleset/coc7/magic.py` + `data/ruleset/coc7/catalogs/spells.json`、`grimoires.json` | `content.packs` 按模组启用；`magic.learning=book` | 新增 `sheet.extensions.magic.spells[]`、`grimoires[]`；只增字段 | `magic.learn`、`magic.cast`；`rule spell` | H |

> 依赖提示：法术库依赖 MP 持久化（审查报告 B7）；若 B7 不在本轮修，法术只能做“单场战斗内消耗”，不能做跨场景资源。追逐与法术都可以在批次 2 之后并行开发，但不要阻塞批次 3/4。

## 4. 存档与房间快照兼容策略

- 所有新增角色卡字段一律放在 `sheet.extensions.coc7.<namespace>`；旧卡缺省即空，不做批量改写。
- 房间快照白名单只增不删；`_sanitize_*` 遇到未知字段保留原值，避免换房时丢数据。
- 现有 `background.phobiasManias` 文本与新的 `mental_status.phobias/manias` 双读：优先新结构，旧文本只做展示。
- 高风险的 SAN/成长结构加 `schemaVersion`；读取时惰性迁移，写入时按新版本；不回写旧文件。
- 战斗会话状态（瞄准、掩体、击晕、部位、毒）只放房间战斗状态，不进角色卡；只有 HP/状态等持久结果走写回链。
- 可选规则默认关闭；核心成长默认开启，但“缺字段 = 无标记”，不会误发成长。

## 5. 工具与命令的最小集合

| 批次 | AI 工具 | 控制台命令 |
| --- | --- | --- |
| 1 | `check.combined`、`check.group`、`character.spend`、`character.contacts` | `rule check --skills`、`rule check --group`、`rule economy`、`rule contacts` |
| 2 | `character.experience_check`、`character.train` | `rule growth`、`rule train` |
| 3 | `san.treat`、`san.habituation`、`san.reality_check` | `rule san treat`、`rule san habituation` |
| 4 | `combat.knockout`、`combat.hit_location`、`combat.poison` | `rule combat knockout`、`rule combat hit-location`、`rule heal natural` |
| 5 | `chase.start`、`chase.next`、`chase.conflict`、`magic.learn`、`magic.cast` | `rule chase`、`rule spell` |

> 原则：工具与命令只做参数转换与权限校验，规则计算全部回到 `ruleset/coc7/*`；AI 工具命名遵循 `coc7_*` 单层命名空间约定。

## 6. 本轮不做

- 不改 SAN 上限与初始值口径；不重复实现钳制。
- 不修审查报告里的既有阻断项（奖励骰数学、护甲、MP、HP 写回、技能 >100 等），除非它们作为上述条目的硬依赖被单独批准。
- 不做法术效果的具体数值内容、怪物图鉴、模组内容；这些属于内容包，不是本轮规则补全。
- 不做前端大规模改版；批次 4 的战斗房规入口只加设置分组与字段。

## 7. 验收与回滚

- 每批先补规则函数单测，再接工具/命令，最后接前端；任何一批都能单独回滚。
- 高风险批次（3、4、5）必须有“旧卡打开正常、旧房间可继续跑、未知字段不丢”的回归用例。
- 可选规则默认关闭；开启后只影响本房间，不改变其他房间与既有存档。
- 若某批依赖未就绪（如 B13 技能上限、B16 HP 写回、B7 MP 持久化），先交付不依赖的部分，并在计划跟踪里标“阻塞项”。
