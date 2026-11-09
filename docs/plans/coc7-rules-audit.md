# CoC 7e 规则对照审查报告（第二版）

> 版本：v2（2026-11-09）
> 基准：`c:\Users\19826\.trae-cn\attachments\9b3f7e8b-5ceb-424d-9987-75618acf0db5_a10af97e-9236-43a4-aa5c-40ad41d72c7f_COC7th守秘人规则书2002c.pdf`
> 页码口径：采用书内印刷页码；已抽样核对 PDF PAGE = 印刷页码 + 1。
> 状态：**只做审查，不改代码、不改配置**。文中所有"实现方式"均为落点、配置项、权限的方案描述，不含代码。
> 可追溯性：第一版（v1）的结论在本文件 §1 逐条标注"保留 / 改判 / 撤回"，不静默修改。

---

## 0. 一页结论

| 维度 | v2 结论 |
| --- | --- |
| 整体完成度 | 检定主干、战斗主干、SAN 主干、技能/属性/物品的房间写入链**都已存在**。本轮不是"从零补规则"，而是"修数学、补分支、补生命周期"。 |
| 用户明确纠正的两点 | ① **SAN 上限按您的口径撤回，不再列为缺口**（见 C1，附原文备查）。② **大成功/大失败阈值确实已有房规可配置**，v1 的"固定 96"判断作废；真正的问题是"阈值没有按规则自动派生"且"未贯通 SAN、疯狂、成长等路径"（见 B4）。 |
| 仍会算错结果的阻断项 | ① 奖励/惩罚骰的十位骰去重采样；② 奖励与惩罚不抵消；③ 上限 10 颗而非 2 颗；④ PC 护甲在战斗中被硬编码为 0；⑤ MP 不持久；⑥ 不定性疯狂没有"单日累计"；⑦ SAN 检定掷出 100 不取最大损失；⑧ 技能值被 100 封顶；⑨ 射击目标体型误用攻击者体格。 |
| 最大的规则空洞 | **孤注一掷**、**花费幸运**、**经验成长**、**心理治疗与恢复**、**习惯恐惧/现实认知**、**追逐整章**、**法术库与神话典籍**、**自然恢复**。 |
| 第二个必答问题 | **已改判**：KP 工具与房间命令现在都能修改技能值和基础属性（`character.set_skill`、`character.set_attribute`、`rule skill`、`rule attr`），物品也能新增（`character.add_item`、`rule item`），并共用 `pending_changes` 写回链。**但仍不完整**：HP/MP/护甲不在链上；属性变更不重算派生值；技能封顶 100；物品只增不减。 |
| 建议优先级 | 先修检定数学与护甲（结果错误）→ 贯通房规阈值并修 SAN 大失败/日累计 → 补 Push 与成长（核心交互）→ 补恢复/恐惧症机械效果 → 最后做追逐、法术、内容包。 |

---

## 1. 前提纠错与改判清单（v1 → v2）

| 编号 | v1 结论 | v2 处理 | 依据与说明 |
| --- | --- | --- | --- |
| **C1** | B8：SAN 上限应等于 `99 − 克苏鲁神话`，当前 `maxSan = POW` 是缺陷。 | **撤回** | 按您的裁决，本书 SAN 上限以 POW 为准，本条不再作为缺口。实现 [character-sheet.ts L692-L694](../../frontend/src/app/character-sheet.ts#L692-L694) 与 [characters.py](../../backend/trpg_server/routes/characters.py) 的 `maxSan` 逻辑不再改判为错误。<br>**原文备查（不影响本轮结论）**：本 PDF p.130 与 p.143 出现"最大理智值 = 99 − 当前克苏鲁神话技能值"的表述，p.130 同时说明"初始理智值等于 POW"。若未来要与 PDF 字面完全一致，建议单独确认口径；本轮尊重您的裁决，不列为缺口。 |
| **C2** | B4：大失败阈值固定 96，不随技能值 50 分界。 | **改判** | 房规阈值确实存在且可配置：房间 schema 的 `dice_critical_threshold` / `dice_fumble_threshold`（[rooms.py L61-L62](../../backend/trpg_server/routes/rooms.py#L61-L62)），后端 `_resolve_thresholds`（[dice.py L216-L237](../../backend/trpg_server/agents/tools/dice.py#L216-L237)）与前端设置项（[03-room-tools-auth-settings.html L73-L82](../../frontend/src/index/fragments/03-room-tools-auth-settings.html#L73-L82)）。<br>真正缺口改为：**阈值没有按"检定所需骰出的上限"自动派生**，且 **SAN、疯狂、成长等路径不读取该阈值**。详见 B4 与 §3.1.1。 |
| **C3** | B11：花费幸运是核心规则，应在投骰前声明。 | **改判** | 本书 5.16（p.85）把"花费幸运"列为**可选规则**，且是**检定结束后**按 1:1 花费；不能用于幸运检定、伤害检定、理智检定、理智损失掷骰；一次检定只能改一次；孤注一掷结果不能改；大成功/大失败/枪械故障始终生效；使用幸运的检定不获得成长标记。v1 的"核心、投骰前"表述撤回，改为可选规则 O1。 |
| **C4** | B5 的 Push 禁列包含"克苏鲁神话"。 | **撤回该子项** | 本书 5.3（p.74）禁列仅为：幸运检定、理智检定、战斗检定、伤害与理智损失掷骰。克苏鲁神话**可以**孤注一掷（书中示例即以神话知识孤注一掷）。Push 其余结论保留。 |
| **C5** | E8：战斗房规已进 schema，但前端完全没有入口。 | **部分撤回** | 大成功/大失败、动画、重投等设置**已有前端入口**；**战斗房规**（突袭模型、拔枪消耗、0 HP 死亡、重伤惩罚、寡不敌众等）仍无前端入口，见 E8。 |
| **C6** | E10：房间记录的 `type=skill` 只写日志不改数值，技能/属性无法修改。 | **撤回** | 现在已有 `rule skill`、`rule attr` 命令与 `character.set_skill`、`character.set_attribute` 工具，走 `pending_changes` 与归档写回，见 §7。 |
| **C7** | 必答二：KP 工具与房间命令都不能修改技能值或基础属性。 | **改判** | 现在都能改；剩余缺口是派生值重算、HP/MP/护甲写回、技能 100 封顶、物品只增不减。详见 §7。 |
| **C8** | 审查范围排除追逐、法术列表、神话典籍、配图配乐。 | **追补** | 本轮按要求逐章覆盖，新增第七章追逐、第九章魔法、第十一至十四章内容层、第十六章摘要的结论（见 §4.5、§4.7）。 |
| **C9** | O2 把"事后补花幸运"当作需要默认关闭的房规。 | **改判** | 本书默认就是"检定后花费"；真正需要开关的是"是否启用花费幸运"本身，而不是"是否允许事后补花"。 |

---

## 2. 审查方法与逐章覆盖矩阵

- 方法：以 PDF 目录为章节清单，逐章抽取原文；对每条规则在实现中定位入口与分支；只读，不运行、不改动。
- 抽样核对：PDF PAGE 与印刷页码偏移为 1（例：PDF PAGE 81 显示"80"）。
- 覆盖矩阵：

| 章节 | 印刷页 | 覆盖情况 | v2 结论 |
| --- | --- | --- | --- |
| 第一章 介绍、第二章 爱手艺与神话 | 8-23 | 叙事与背景 | 无机械规则，不涉及实现。 |
| 第三章 创建调查员 | 24-43 | 属性、职业、技能点、背景、装备、创建选项 | 基本可用；年龄调整与 EDU 增强检定缺失（C1-A/C2-A 见 §4.1）。 |
| 第四章 技能 | 44-71 | 技能定义、专攻、对立、Push、组合、技能表、可选 | 技能表与专攻字段存在；组合技能、专攻转移、技能超过 100 缺失。 |
| 第五章 游戏系统 | 72-85 | 检定、Push、大成功/大失败、对抗、奖惩骰、组合、社交难度、成长、信用评级、熟人、训练、老化、可选 | 主干在；Push、成长、信用评级、熟人、训练、老化、花费幸运缺失或不全。 |
| 第六章 战斗 | 86-111 | 战斗轮、近战、战技、射击、伤害治疗、可选、护甲、毒剂 | 主干完整度较高；护甲、MP、射击细节、自然恢复、击晕、部位命中、毒剂等有缺口。 |
| 第七章 追逐 | 112-129 | 建立追逐、切入、移动、冲突、可选 | **整章未实现**。 |
| 第八章 理智 | 130-143 | 理智检定、疯狂、影响、治疗、习惯恐惧、可选 | 检定与发作主干在；日累计、SAN 大失败、首次神话、习惯恐惧、现实认知、治疗缺失。 |
| 第九章 魔法 | 144-152 | 神话典籍、使用魔法、成为相信者、可选 | 施法时间/MP/SAN 骨架在；法术库、典籍、学习、POW 获取缺失。 |
| 第十章 主持游戏 | 154-189 | KP 指引、NPC、灵感/洞察、节奏 | 由 AI KP 叙事替代；灵感/洞察可复用现有属性检定，无独立机械。 |
| 第十一章 神话典籍 | 190-205 | 典籍描述、使用、列表 | 内容层未实现。 |
| 第十二章 法术 | 206-231 | 法术规则、变体、列表 | 施法框架在；法术列表、变体、学习条件未实现。 |
| 第十三章 外星科技 | 232-239 | 造物 | 内容层未实现。 |
| 第十四章 怪物与诸神 | 240-313 | 生物、神祇、野兽 | 内容层未实现；战斗工具支持 NPC，但无图鉴数据。 |
| 第十五章 模组 | 314-355 | 两个模组 | 内容不属于规则引擎。 |
| 第十六章 附录 | 356-392 | 术语、物价、武器、系统摘要 | 武器表完整（内置 104 条）；护甲表、摘要未代码化。 |

---

## 3. 阻断性问题（会算错结果或让规则不可用）

### 3.1 检定数学

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **B1** | 5.8 奖励骰与惩罚骰（p.79）：单位骰只掷一次，额外掷 N 颗**十位骰**，各自独立，**允许重复**；按完整百分位取最低或最高。 | [dice.py L57-L71](../../backend/trpg_server/agents/tools/dice.py#L57-L71) 的 `_percentile_roll` 对十位骰**去重采样**（重复就重掷），`_percentile_from_faces` 同样。 | 奖励骰偏强、惩罚骰偏狠：技能 50 时 +1 奖励为 77.8%（规则 75%），+2 为 91.7%（规则 87.5%）；惩罚骰对称偏差。骰数越多误差越大。 | 落点：`ruleset/coc7/checks.py` 与 `dice.py` 共用的百分位函数；单位骰 1 次、十位独立 N+1 次、允许重复。配置：无。权限：规则层，无。补每档 10 万次统计测试，误差 <1%。 |
| **B2** | 5.8（p.79）：奖励骰与惩罚骰**先一一抵消**，剩余部分再掷，最多 2 颗。 | 三处都不对：[check_service.py L99-L102](../../backend/trpg_server/check_service.py#L99-L102) 同时传入直接报错；[combat.py L1415-L1416](../../backend/trpg_server/agents/tools/combat.py#L1415-L1416) 近战报错；[combat.py L1678-L1679](../../backend/trpg_server/agents/tools/combat.py#L1678-L1679) 只要存在惩罚就把奖励置 0。 | "瞄准 + 掩体"应为普通骰，现变成 1 颗惩罚骰；近战同时有奖励与惩罚时直接无法检定；KP 只能手动拆成两次。 | 落点：`check_service.run_room_check`、`dice.roll_coc_check`、`combat.attack`、`combat.shoot` 四个入口统一调用 `normalize_bonus_penalty(bonus, penalty)`：先抵消，再各自截断到 2。配置：`dice.max_dice`（默认 2）。权限：规则层。补 +1/−1、+2/−1、+2/−2、−3 对照测试。 |
| **B3** | 5.8（p.79）：单次检定最多 **2 颗**奖励骰或惩罚骰。 | [check_service.py L99-L100](../../backend/trpg_server/check_service.py#L99-L100) 与 [dice.py L51-L56](../../backend/trpg_server/agents/tools/dice.py#L51-L56) 允许到 10 颗；只有战斗路径截断到 2。 | KP 一句"环境极有利"即可给 10 颗奖励骰，成功率接近必成；房间控制台与战斗行为不一致。 | 落点：同上统一归一化函数；配置：站点级 `dice.max_bonus_dice`、`dice.max_penalty_dice`（默认 2），房间只能收紧不能放宽；权限：站点管理员设定上限，房主在房间内调整。 |
| **B4**（改判） | 5.5 大成功与大失败（p.77）：大成功为 01；大失败按"检定需要骰出的上限"判断——该上限 **≥50** 时只有 **100**，**<50** 时为 **96-100**。 | 阈值已有房规配置：`dice_critical_threshold` / `dice_fumble_threshold` 经 `_context_thresholds` 注入 [checks.py L58-L100](../../backend/trpg_server/ruleset/coc7/checks.py#L58-L100) 与 [dice.py L264-L271](../../backend/trpg_server/agents/tools/dice.py#L264-L271)。但：① 默认是**单一绝对值 96**，不会按最终上限自动切换；② 判定传入的是基础技能值 `base_target`，不是难度调整后的最终上限；③ SAN、疯狂、成长等路径不读取。 | 技能 50-95 的角色在 96-99 被误判大失败；把房规改成 100 又会让低技能角色失去 96-99 的大失败。一个平面阈值无法同时满足书里的两侧规则。 | 落点：新增规则函数 `fumble_threshold(final_target)`：`final_target >= 50 → 100`，否则 `96`；`critical_threshold` 仍默认 1。配置：`dice.fumble_mode=auto`（默认）或 `flat`；`flat` 时才使用房间 `dice_fumble_threshold`；前端设置页标注"自动/房规覆盖"。权限：站点管理员选模式，房主设房规值。 |
| **B12**（新增） | 8.x 理智检定（p.131-132）：理智检定掷出大失败（100）时，失去该遭遇**可损失的最大理智值**。 | [dice.py L1017](../../backend/trpg_server/agents/tools/dice.py#L1017) 只按 `roll <= current` 判成功或失败；掷出 100 与普通失败走同一损失表达式，没有"取最大值"分支。 | SAN 检定的大失败被降级为普通失败；高 SAN 角色面对 100 时损失明显偏轻，恐怖感与规则不符。 | 落点：`roll_sanity_check` 在判定失败后增加 `roll == 100` 分支，取失败损失表达式的最大值。配置：`sanity.fumble_max_loss`（默认开）。权限：规则层，无。补 100、99、01 边界测试。 |
| **B13**（新增） | 5.11 经验奖励（p.81）：技能成长可以**超过 100%**；技能值本身不设 100 硬上限。 | [check_service.py](../../backend/trpg_server/check_service.py) 拒绝 `base_target > 100`；[checks.py](../../backend/trpg_server/ruleset/coc7/checks.py) 对目标做 1-100 截断；[room_rules.py L430-L470](../../backend/trpg_server/room_rules.py#L430-L470) 的 `apply_skill_change` 封顶 100；[character-sheet.ts L956-L965](../../frontend/src/app/character-sheet.ts#L956-L965) 的技能输入上限 99。 | 幕间成长到 100 后无法继续；高龄 EDU、神话知识、专精叠加都被截断；与"可以超过 100"直接冲突。 | 落点：属性/技能校验层与 `apply_skill_change`；配置：`skills.allow_over_100`（默认开）、`skills.max_value`（默认 200，仅防脏数据）；权限：无。成功等级仍按 1/2、1/5 计算。 |
| **B14**（新增） | 6.5 射击（p.96-97）：目标体型/体格与攻击者相差越大，给予奖励骰或惩罚骰；大体型目标更难被命中。 | [combat.py L1670-L1676](../../backend/trpg_server/agents/tools/combat.py#L1670-L1676) 使用 `attacker.get("build")` 计算目标体型修正。 | 修正对象搞反：大个子射手打普通目标反而获得体型奖励；PC 的 Build 与目标无关，等于规则失效。 | 落点：`combat.shoot` 的体型差计算改为 `target.get("build") - attacker.get("build")`。配置：无。权限：无。补 Build 差 −2、−1、0、+1、+2 的骰数测试。 |

#### 3.1.1 房规阈值在各检定路径的读取情况

| 检定路径 | 是否读取房规阈值 | 实际行为 |
| --- | --- | --- |
| 通用技能/属性检定（`check_service.run_room_check`） | 是 | 通过 `_context_thresholds` 注入，房间设置生效。 |
| KP 房间检定工具（`dice.roll_room_check`） | 是 | [dice.py L878](../../backend/trpg_server/agents/tools/dice.py#L878) 合并上下文阈值。 |
| KP 旧检定工具（`dice.roll_coc_check`） | 仅 `execute_roll_coc_check` 包装时是 | 包装层合并阈值；直接调用函数或未传 context 时回落默认 1/96。 |
| 房间控制台 `rule check` | 是 | 走 `check_service`，见 [room_rules.py L582 起](../../backend/trpg_server/room_rules.py#L582)。 |
| 战斗近战/对抗 | 是 | `_opposed_rolls` 传入阈值。 |
| 战斗射击 | 是 | `shoot` 传入阈值。 |
| 战斗重伤/濒死体质检定 | 是 | [combat/rulesets/coc7.py L150-L190](../../backend/trpg_server/combat/rulesets/coc7.py#L150-L190) 传入阈值。 |
| **SAN 检定** | **否** | `roll_sanity_check` 自行判定成功/失败，不读取也不使用大成功/大失败阈值；且缺少 100 取最大损失。 |
| **疯狂 INT 检定** | **否** | `roll_madness` 直接与 INT 比较，未读取阈值。 |
| **技能成长检定** | **否（未实现）** | 无成长结算入口，见 E2。 |
| **通用对抗检定** | **否（未实现）** | 只有战斗内对抗，见 E1。 |

> 另外：即使路径读取了阈值，`_success_level` 仍用 `base_target` 计算大成功/大失败，而书里要求按"难度调整后的最终上限"判断。这属于 B4，不是"没读取"，而是"读取了平面值但没派生"。

### 3.2 战斗资源

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **B6** | 6.4 其它战斗情况：护甲（p.96-97）：护甲按点抵扣每次伤害；不减免魔法、毒素、溺水等。 | [combat.py L341](../../backend/trpg_server/agents/tools/combat.py#L341) 把 PC 的 `armor` 硬编码为 0；房间快照也不带护甲（[rooms.py L608-L626](../../backend/trpg_server/routes/rooms.py#L608-L626)、[characters.py L718-L756](../../backend/trpg_server/routes/characters.py#L718-L756)）。 | 防弹衣、皮甲、头盔全部无效；伤害永远打满，PC 致死率显著高于规则。 | 落点：`_combatant_from_member` 读取卡面 `battleAttributes.armor` 或 `armor`；房间快照 sanitize 增加 `armor`；归档写回带上护甲。配置：无。权限：无。补"护甲 2 受 5 点伤害 → 3"测试。 |
| **B7** | 6.6 与 9.2（p.101-103、p.148-150）：施法消耗 MP；MP = POW/5，是持久资源，只能靠休息与时间恢复。 | 房间初始状态没有 `max_mp/current_mp`（[rooms.py L638-L655](../../backend/trpg_server/routes/rooms.py#L638-L655)）；PC 每次进战斗都把 MP 重置为 `POW/5`（[combat.py L334](../../backend/trpg_server/agents/tools/combat.py#L334)）；[`_persist_player_state` L718-L748](../../backend/trpg_server/agents/tools/combat.py#L718-L748) 不回写 MP，`pending_changes` 也不接受 mp。同一处还把 SAN 读成 `state.sanity / state.san`（[combat.py L335](../../backend/trpg_server/agents/tools/combat.py#L335)），而房间实际字段是 `current_san`，PC 的 SAN 在战斗参与者里通常被读成 0。 | 连续战斗可以无限施法；MP 消耗只在本场战斗内有效；归档后 MP 丢失；依赖 SAN 的战斗效果对 PC 取值为 0。 | 落点：房间初始状态与快照增加 `max_mp/current_mp`；`apply_room_changes` 增加 `mp` 类型；`_persist_player_state` 入队 `pending_changes` 并写回；SAN 改读 `current_san / currentSan`。配置：无。权限：无。补"两场战斗之间 MP 不恢复"与"SAN 参与战斗效果"测试。 |
| **B15**（新增） | 3.1 生成属性（p.24-27）：HP、MP、MOV、Build、伤害加值、SAN 均由属性派生。 | `apply_attribute_change` 只改属性值，**不重算** `max_hp/max_san/mp/mov/build/db`；房间快照保留旧派生值。 | KP 用 `rule attr` 改 CON/POW/SIZ 后，房间与战斗仍按旧的 HP/MP/SAN/MOV 运行；规则上应同步变化。 | 落点：`apply_attribute_change` 结束时调用 `ruleset/coc7/schema.derive()`，把派生值写回快照并同步派生上限。配置：无。权限：无。补 CON/POW/SIZ 变更后的派生断言。 |
| **B16**（新增） | 6.6 伤害与治疗（p.101-105）：HP 是持久资源；重伤、濒死、治疗结果应留在角色卡上。 | 战斗 `_persist_player_state` 只改房间 `current_hp`，**不置 `pending_persist`**；`apply_room_changes` 不支持 `hp`；归档只写 SAN/状态/背景。 | 战斗结束后 HP 可能只存在房间快照里；换房间或重进后恢复原值；治疗与重伤结果不落到角色卡。 | 落点：新增 `hp` 变更类型；战斗结束统一入队；`persist_member_card` 写入 `currentHp/maxHp`。配置：`archive.auto_persist_hp`（默认开）。权限：无。 |

---

## 4. 体验缺口与实现不完整（逐章）

### 4.1 第三章 创建调查员（p.24-43）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **C1-A** | 3.1 与 5.15 老化（p.24-27、p.84）：20 岁时 STR、SIZ 合计 +5；40 岁起 STR、CON、DEX 合计 −5，APP −5，MOV −1；60 岁合计 −10；70 岁 −20；80 岁 −40；之后每十年 −80；效果累加。 | 创建流程没有年龄属性修正；只有 MOV 在 [character-sheet.ts L738-L745](../../frontend/src/app/character-sheet.ts#L738-L745) 按年龄递减。 | 40 岁角色不会掉 STR/CON/DEX/APP；20 岁角色得不到 +5；MOV 与其余属性不同步。 | 落点：创建向导的年龄步骤与 `schema.derive()`；配置：`creation.age_adjustments=book`（默认）；权限：玩家可选"手动覆盖"，房主可锁定。落点数据按 p.84 表。 |
| **C2-A** | 5.15 老化（p.84）：达到 20/40/50/60 岁时各获得一次 EDU 增强检定（1D100 > 当前 EDU 则 +1D10）；70 岁与 80 岁不再获得。 | 没有 EDU 增强检定入口；年龄只影响 MOV。 | 高龄调查员的 EDU 不会按规则增长；技能点与知识类检定偏低。 | 落点：创建/幕间流程；配置：`creation.edu_enhancement=book`；权限：玩家掷骰，KP 可代掷。补 20/40/50/60 岁各一次的记录。 |
| **C3-A** | 3.5 装备与表Ⅱ（p.36-37）：起始现金与资产由信用评级决定。 | 前端有现金与消费水平字段，但没有按 CR 生成或校验起始资产。 | 创建时资产可以随意填；信用评级与生活标准脱节。 | 落点：创建向导的装备步骤；配置：`creation.assets_by_credit`（默认开）；权限：玩家可改，KP 审核。 |
| **C4-A** | 3.4 背景（p.32-35）：背景条目、关键连接、信念会影响恢复与理智。 | 卡面有 `stories`、`friends` 等字段，但没有机械效果。 | 背景只是文本，无法参与"关键连接恢复 SAN"等规则。 | 落点：`schema` 的背景模型与 SAN 恢复；配置：`sanity.use_key_connection`（默认开）；权限：玩家填写，KP 裁定。 |

### 4.2 第四章 技能（p.44-71）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **K1** | 4.5 与 5.9 组合技能检定（p.45、p.80）：只掷一次骰，与多个技能比较；可要求全部成功或任一成功。 | 没有组合检定入口；KP 只能分别检定，容易出现"总成功率"与规则不一致。 | 追踪、伪装、驾驶等需要多技能的场景无法按规则一次结算。 | 落点：`check_service.run_combined_check`；配置：`checks.combined_mode=all 或 any`（默认 all）；权限：KP 选择模式。工具 `check.combined`，命令 `rule check --skills=A,B --mode=all`。 |
| **K2** | 4.7 可选规则：专业技能转移（p.70）：某专攻首次达到 50% 或 90% 时，相关专攻各 +10%，封顶 50% 或 90%。 | `skillGroups` 只是存储分组，没有专攻转移计算。 | 玩家升满一种格斗/射击/语言后，其他同类专攻不会按可选规则提升。 | 落点：技能变更后的专攻组计算；配置：`skills.specialty_transfer`（默认关，可选规则）；权限：房主开启，玩家确认。 |
| **K3** | 4.6 技能列表（p.46-69）：技能值可超过 100（与 5.11 一致）。 | 见 B13，前端 0-99、后端 0-100。 | 同上。 | 见 B13。 |

### 4.3 第五章 游戏系统（p.72-85）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **B5** | 5.3 孤注一掷（p.74-76）：失败后可描述额外努力重试一次；时间流逝；目标必须仍可实现；KP 可揭示预兆；失败后果必须升级；幸运、理智、战斗、伤害与理智损失不可 Push；对抗不可 Push。 | 全库没有 Push 概念（无 `push` 状态、无失败记忆、无后果升级契约）。KP 只能再叫一次检定，实际退化为无限重试。 | 核心交互缺失：失败没有代价，也没有"这件事已经失败过"的记忆；紧张感与规则都不成立。 | 详见 §6。落点：房间 `check_state` 与 `check_service`；配置：`push.enabled`（默认开）、`push.max_per_check=1`；权限：玩家发起，KP 确认后果。 |
| **E1** | 5.7 对抗检定（p.78）：通用机制，成功等级高者胜；同级比技能值；双失败无事发生；一方用属性时另一方应获得"也用属性或技能"的选择；对抗不能 Push。 | 只有战斗内 [`_opposed_rolls`](../../backend/trpg_server/agents/tools/combat.py#L1303-L1323) 支持对抗；通用工具只能两次独立检定，由 KP 口头比较，也没有"属性对技能"的平衡提示。 | 潜行 vs 聆听、话术 vs 心理学等场景容易出现"双方都成功却判一方赢"的错误；高属性一方会天然占优。 | 落点：`check_service.run_opposed_check`，复用 `LEVEL_RANK`；工具 `check.opposed`；命令 `rule opposed <A> <技能A> vs <B> <技能B>`；输出中提示"属性对技能"的选择。配置：无。权限：KP。 |
| **E2** | 5.11 经验奖励（p.81）：成功使用技能打勾；使用奖励骰无标记；对抗仅胜者得标记；劣势或花费幸运后无标记；幕末对每个标记掷 1D100，`> 当前值` 或 `> 95` 则 +1D10；神话与信用评级永不成长；技能达 90%+ 时幕间 SAN +2D6。 | 只有卡面手填的"成长点数"列（[character-sheet.ts L1942-L1961](../../frontend/src/app/character-sheet.ts#L1942-L1961)）；没有自动打勾、没有幕末成长检定、没有 90%+ 的 SAN 奖励。 | 调查员发展完全靠玩家自觉加数字；成长规则实际不存在。 | 落点：检定记录增加 `used_successfully`；新增 `rule growth` 与 `character.experience_check`；配置：`growth.enabled`（默认开）、`growth.san_bonus_over_90`（默认开）；权限：KP 结算，玩家确认。 |
| **E2-a** | 5.11（p.81）：使用奖励骰、花费幸运、对抗失败者不获得成长标记；神话与信用评级不可成长。 | 没有成长标记，无法执行这些排除项。 | 成长一旦实现，若不记录这些条件，会多发标记。 | 落点：`check_service` 输出 `growth_eligible` 与原因；配置：`growth.strict_marks`（默认开）。权限：无。 |
| **E3** | 5.12 信用评级与开支（p.81-83）：生活标准内自由花销；超出消费限额扣现金，现金不足动用资产；幕间按七档调整 CR。 | 卡面有 `creditRating`、现金、消费水平字段，但没有消费限额、资产折算或幕间 CR 调整流程。 | 金钱只是文本，信用评级不会随生活与事件变化。 | 落点：角色卡资产模型与幕间流程；配置：`economy.enabled`（默认开）、`economy.credit_tiers`；权限：玩家申报，KP 调整 CR。 |
| **E4** | 5.13 熟人（p.83）：背景关系可提供帮助、借物、信息。 | 卡面有 `friends`，没有调用或机械效果。 | 熟人只是文本，无法在检定或资源上提供规则化帮助。 | 落点：背景关系与检定修正；配置：`background.contacts_bonus`（默认 1 颗奖励骰，KP 裁定）；权限：KP 决定是否生效。 |
| **E5** | 5.14 训练（p.84）：幕间可花时间训练技能，KP 判定可行性。 | 没有训练入口与时间成本。 | 幕间只能靠成长检定，无法按规则训练。 | 落点：幕间流程；配置：`growth.training_enabled`（默认关，可选）；权限：KP 批准，玩家消耗时间。 |
| **E6** | 5.15 老化（p.84）：见 C1-A、C2-A。 | 见 C1-A、C2-A。 | 见 C1-A、C2-A。 | 见 C1-A、C2-A。 |
| **O1** | 5.16 花费幸运（p.85，可选）：检定后 1:1 扣幸运改结果；不能用于幸运、伤害、理智、理智损失；一次检定只能改一次；Push 结果不能改；大成功/大失败/枪械故障始终生效；使用后无成长标记；上限为当前幸运。 | 完全没有实现。幸运只是一个可检定的属性；不能把差一点的成功买回来。 | 这个可选资源在游戏中实际不存在；玩家无法按规则补救关键检定。 | 落点：检定入口增加 `spend_luck`；扣当前幸运后比较；写 `luck_records` 并入队 `pending_changes`。配置：`luck.spend_enabled`（默认开，可选）、`luck.max_spend_per_check=1`。权限：玩家声明，KP 确认。 |
| **O2** | 5.16 幸运恢复（p.85，可选）：每次聚会结束作幸运增强检定，1D100 > 当前幸运则 +1D10，幸运不超过 99。 | 没有恢复入口；幸运只减不增。 | 幸运是消耗品但没有补充途径。 | 落点：`rule luck restore` 与 `character.restore_luck`；配置：`luck.recovery=check`（默认）或 `fixed`；权限：KP 结算。 |
| **E7** | 5.4 复数玩家为一项技能检定掷骰（p.76）：团队检定由最差者或指定代表掷，成功则全队成功。 | 没有团队检定入口；KP 只能逐个掷。 | 团队潜行、团队聆听等场景无法一次结算。 | 落点：`check_service.run_group_check`；配置：`checks.group_mode=worst 或 representative`；权限：KP 选择。 |
| **E7-b** | 5.6 团体幸运检定（p.76）：由在场调查员中幸运最低者掷幸运。 | 幸运只是单角色属性检定；没有"取最低者"的团体入口，容易让高幸运角色代掷。 | 团体幸运检定的概率会被人为抬高。 | 落点：`check_service.run_group_check` 支持 `attribute=luck, pick=lowest`；配置：`checks.group_luck_pick=lowest`；权限：KP。 |
| **E9** | 5.6 智力/灵感/知识检定（p.77）：INT 检定解决谜题；灵感检定在卡关时给线索，成功轻松获得、失败需付出代价；知识检定用 EDU。 | 可以用通用属性检定手工完成，但没有专用入口与提示词；"灵感检定失败要付出代价"没有契约。 | KP 容易把灵感检定当成普通 INT 检定，白送线索。 | 落点：`check_service` 的检定类型与 `prompt_fragments`；配置：`checks.idea_roll=true`（默认开）；权限：KP 决定代价。 |
| **E12** | 全书检定口径（p.72-85）：难度、奖惩骰、抵消、上限、大成功/大失败、Push 禁列需要 KP 一致执行。 | [ruleset/coc7/tools.py L27-L29](../../backend/trpg_server/ruleset/coc7/tools.py#L27-L29) 的 `prompt_fragments()` 返回空；KP 提示词没有注入这些口径。 | 同一规则在不同回合可能被 AI KP 执行成不同版本；玩家无法预期。 | 落点：`prompt_fragments()` 输出"难度—奖惩骰—抵消—上限—大成功/大失败—Push 禁列"的固定段落；配置：`prompt.rules_digest=true`（默认开）；权限：站点管理员可改文案。 |

### 4.4 第六章 战斗（p.86-111）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **F1** | 6.5 射击（p.96-100）：瞄准需在前一轮声明，下一轮攻击获得奖励骰；若角色受伤或移动则失去瞄准。 | `shoot` 有 `aim` 参数（[combat.py L2205-L2244](../../backend/trpg_server/agents/tools/combat.py#L2205-L2244)），但不跟踪"上一轮已瞄准"，也不在受伤/移动时清除。 | KP 需要自己记住瞄准状态；受伤后仍可享受瞄准奖励。 | 落点：战斗参与者状态增加 `aiming` 标记与失效条件；配置：`combat.aim_tracking`（默认开）；权限：KP。 |
| **F2** | 6.5 射击（p.96-100）：抵近射击为 DEX/5 英尺内；掩体需要目标进行闪避检定，成功则攻击者失去下次攻击机会；射击入近战有惩罚骰，大失败可能命中同伴。 | `point_blank` 只是布尔参数，没有距离校验；`cover` 只是修正值，没有闪避与"失去下次攻击"；没有 `in_melee` 参数，也没有大失败误伤。 | 抵近、掩体、混战射击都可以被口头滥用；大失败不会伤到同伴。 | 落点：`combat.shoot` 参数与战斗状态；配置：`combat.tactical_detail`（默认开）；权限：KP。 |
| **F3** | 6.5 射击（p.96-97）：目标体型差修正。 | 见 B14，使用攻击者 Build。 | 见 B14。 | 见 B14。 |
| **F4** | 6.7 可选：护甲调整（p.106-107）：霰弹枪护甲按每颗伤害骰扣减；局部护甲需要幸运检定决定是否覆盖。 | 伤害结算按总伤害减一次护甲；没有霰弹枪逐骰扣减，也没有局部护甲的幸运检定。 | 霰弹枪对护甲目标伤害偏高；头盔等局部护甲要么全生效要么全不生效。 | 落点：伤害管线按武器类型分支；配置：`combat.armor_model=simple 或 book`（默认 book）；权限：房主。 |
| **F5** | 6.6 伤害与治疗（p.101-105）：重伤后每周/每日自然恢复；昏迷与休养影响恢复速度。 | 仅有人工 `heal` 与急救/医学工具；没有按时间推进的自然恢复。 | 战斗后必须 KP 手动加血，否则角色永远停在重伤值。 | 落点：房间时钟与休息流程；配置：`healing.natural_rate`（默认按书）、`healing.require_rest`；权限：KP 推进时间。 |
| **F6** | 6.7 可选：击晕（p.106）：钝器战技成功可击晕并只造成 1 点伤害。 | 没有击晕状态或击晕战技。 | 电影式击晕无法实现；只能把目标打到 0 HP。 | 落点：`combat.maneuver` 增加 `knockout`；配置：`combat.knockout_enabled`（默认关，可选）；权限：房主开启。 |
| **F7** | 6.7 可选：部位命中（p.108）、毒剂（p.110）、瞄准具/支架、战斗中移动、完全隐蔽。 | 均未实现；射击只有距离难度、瞄准、抵近、掩体、体型等简化项。 | 可选的战术深度无法使用；毒剂与部位伤害只能口头裁定。 | 落点：战斗可选规则模块；配置：`combat.optional.*` 逐项开关；权限：房主。 |
| **F8** | 6.7 可选：先攻检定（p.106）：敏捷检定决定顺序，大成功获得首击奖励，大失败失去首击。 | 先攻固定按 DEX 降序；没有先攻检定模式。 | 无法使用"充满变数"的先攻可选规则。 | 落点：战斗先攻模块；配置：`combat.initiative_model=dex 或 check`（默认 dex）；权限：房主。 |
| **F9** | 6.7 可选：消耗幸运维持意识（p.107）：每轮 1、2、4、8 点幸运维持清醒。 | 没有该规则。 | 重伤昏迷后无法用幸运争取行动。 | 落点：重伤/昏迷流程与 `luck`；配置：`combat.luck_stay_conscious`（默认关）；权限：房主。 |
| **F10** | 6.7 可选：双方都失败时的三种处理（p.107）。 | 默认"无事发生"；没有"较高技能者胜"或"双方互伤"模式。 | 战斗平局处理单一。 | 落点：`_opposed_rolls`；配置：`combat.both_fail=nothing 或 higher_skill 或 mutual`（默认 nothing）；权限：房主。 |
| **E8** | 6.x 战斗房规：突袭模型、拔枪消耗、0 HP 死亡、重伤惩罚、寡不敌众奖励、濒死检定。 | 后端 schema 与 `combat/settings.py` 已支持（[rooms.py L123-L170](../../backend/trpg_server/routes/rooms.py#L123-L170)、[combat/settings.py L38-L81](../../backend/trpg_server/combat/settings.py#L38-L81)），但前端只有大成功/大失败等基础设置，没有战斗房规入口。 | 房主只能改 API 或数据库；普通用户无法配置。 | 落点：房间设置页新增"战斗房规"分组；配置：复用现有 schema 键；权限：房主。 |

### 4.5 第七章 追逐（p.112-129）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **CH1** | 7.1-7.5（p.112-129）：建立追逐、切入、移动、冲突、可选规则；用抽象距离与障碍点推进，而不是精确网格。 | 全库没有追逐模块；只有 `ruleset_adapters` 里的规则集映射名。 | 追车、追人、逃跑场景只能纯叙事；没有距离、障碍、行动点与冲突结算。 | 落点：新增 `combat/chase` 或 `ruleset/coc7/chase` 子模块；配置：`chase.enabled`（默认关）、`chase.distance_model=abstract`；权限：房主开启，KP 主持。建议作为独立阶段，不与战斗混写。 |

### 4.6 第八章 理智（p.130-143）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **C1** | 8.1（p.130）：初始理智值等于 POW；上限为 **99 − 克苏鲁神话值**（本轮用户裁决，取代 v2 的"上限 = POW"）。 | `ruleset/coc7/rules.py` 的 `initial_san` / `san_cap` / `derive_sheet`；前端 `calculateMaxSan(attributes, mythosValue)`；房间 `max_san` 由插件派生并夹紧当前值，神话上升立即压低上限。 | 按裁决不构成缺口。 | 已按裁决实现并补测试；保留本行作为口径记录。 |
| **S1** | 8.1-8.2（p.130-132）：单次理智损失 ≥5 时，KP 要求 INT 检定；成功则临时疯狂 1D10 小时；失败则压抑记忆、不进入疯狂。 | `roll_sanity_check` 置 `madness_check_required`；`roll_madness` 做 INT 检定（[dice.py L1114-L1125](../../backend/trpg_server/agents/tools/dice.py#L1114-L1125)），成功置 `temporaryInsanity` 并记录时长。 | 主干正确。 | 保留。补"INT 失败不进入疯狂"的回归测试。 |
| **S2** | 8.2（p.131-132）：不定性疯狂为"一天内损失 ≥ 当前理智值的 1/5"；持续到接受治疗。 | [dice.py L1028](../../backend/trpg_server/agents/tools/dice.py#L1028) 用**单次** `loss * 5 >= before` 判定；没有按游戏日聚合。 | 多次小损失（如 SAN 100 连续 5 次各 4 点）不会触发；SAN 变低后单次小损失又可能误触发。 | 落点：`sanity_records` 增加 `game_day` 与日初 SAN；`apply_san_change` 与 `roll_sanity_check` 共用聚合函数。配置：`sanity.indefinite_window=day`（默认）、`sanity.indefinite_threshold=0.2`；权限：KP 推进"一天"的边界。 |
| **S3** | 8.2-8.3（p.131-134）：临时疯狂持续 1D10 小时；不定性疯狂持续 1D6 个月或直到治疗；期间有即时症状与总结症状。 | `roll_madness` 记录 `duration_hours` / `duration_rounds`，但没有到期时间、没有状态检查、没有"总结症状"表；`MADNESS_BOUTS` 只有一张 1D10 表（[dice.py L1102-L1111](../../backend/trpg_server/agents/tools/dice.py#L1102-L1111)）。 | 疯狂不会自动结束；临时与不定性共用一张表，语义混淆。 | 落点：`mental_status` 结构化 `{type, started_at, expires_at, bout}`；新增总结症状表；配置：`sanity.bout_tables=book`；权限：无。 |
| **S4** | 8.3（p.132-138）：恐惧症/躁狂症在接触源时给惩罚骰；发作有强制行为；治疗后可移除。 | [dice.py L1227-L1239](../../backend/trpg_server/agents/tools/dice.py#L1227-L1239) 只把名称拼进 `background.phobiasManias` 文本；`SAMPLE_PHOBIAS` / `SAMPLE_MANIAS` 只有各 10 条示例（[dice.py L1126-L1134](../../backend/trpg_server/agents/tools/dice.py#L1126-L1134)），书中是 100 条表；没有结构化、没有触发惩罚骰、没有到期或治疗移除。 | 恐惧症只是文字；KP 忘记就没有机械效果；抽取范围过窄，重复率高。 | 落点：`mental_status.phobias[]`、`manias[]`；补齐 100 条表；检定入口支持 `exposure_tag`；配置：`sanity.phobia_penalty=1`（默认 1 颗惩罚骰）；权限：KP 标记暴露源。 |
| **S5** | 8.2（p.132）：角色第一次因神话知识陷入疯狂时，克苏鲁神话技能 +5；之后每次 +1。 | 没有该逻辑；SAN 与神话技能互不影响。 | 神话知识不会因疯狂增长，角色对神话的理解曲线不符。 | 落点：`roll_madness` 与 `apply_skill_change`；配置：`sanity.mythos_on_madness`（默认开）；权限：无。 |
| **S6** | 8.5 习惯恐惧（p.143）：记录同一生物造成的最大理智损失；已达上限后不再损失；每次幕间上限减 1。 | 没有按生物/来源记录最大损失。 | 同一怪物反复出现时，SAN 可以无限掉；无法表现"习惯恐惧"。 | 落点：`sanity_records` 增加 `source_id/max_loss`；配置：`sanity.habituation`（默认开）；权限：KP 标记来源。 |
| **S7** | 8.4（p.139-143）：心理治疗每月一次精神分析检定，成功 +1D3 SAN，大失败 −1D6；不定性疯狂需 1D6 个月；自救与关键连接可辅助恢复；技能 90%+ 时幕间 +2D6 SAN。 | 只有 `rule san add` 裸改；没有治疗周期、检定、费用、技能 90%+ 奖励。 | SAN 恢复完全靠 KP 手动加数；治疗规则与成长奖励不存在。 | 落点：新增 `sanity_treatment` 流程；配置：`sanity.treatment=book`、`sanity.key_connection_bonus`；权限：KP 结算，玩家消耗时间与金钱。 |
| **S8** | 8.3 现实认知（p.138-139）：面对幻象可作现实认知检定；失败失去 1 点 SAN 并进入发作，成功看破，直到下次 SAN 损失前免疫。 | 没有现实认知检定与免疫标记。 | 幻象场景只能靠 KP 口头描述，玩家无法按规则看破。 | 落点：`sanity` 子模块；配置：`sanity.reality_check`（默认开）；权限：玩家发起，KP 裁定。 |
| **S9** | 8.6 可选（p.143）：精神固化（神话 > 当前 SAN 时所有损失减半）、疯狂洞悉、多重理智检定。 | 均未实现。 | 可选风格无法启用。 | 落点：`sanity.optional.*`；权限：房主逐项开关。 |
| **S10** | 8.x（p.132-138）：理智状态应与身体状态并存。 | [dice.py L1034-L1035](../../backend/trpg_server/agents/tools/dice.py#L1034-L1035) 与 [L1229-L1230](../../backend/trpg_server/agents/tools/dice.py#L1229-L1230) 用 `card["status"] = flags` 覆盖整个状态对象。 | 可能把 `unconscious/dead/majorWound` 等身体状态覆盖掉，战斗与房间显示错乱。 | 落点：状态合并改为按字段写入，保留身体状态；配置：无；权限：无。补"重伤 + 临时疯狂"并存测试。 |

### 4.7 第九章 魔法（p.144-152）与第十一至十四章内容层

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **M1** | 9.2 使用魔法（p.148-151）：施法消耗 MP 与 SAN，耗时、可被打断；法术有学习条件与检定。 | 施法工具已有时间、MP、SAN 成本与打断（`cast_spell`、`cast_interrupt`），但 MP 不持久（B7）。 | 单场战斗内可用；跨场景 MP 不成立。 | 先修 B7；其余保留。 |
| **M2** | 9.1 神话典籍（p.145-148）、第十二章法术列表（p.206-231）、第十三章外星科技（p.232-239）、第十四章怪物与诸神（p.240-313）。 | 没有典籍、法术库、外星科技、怪物图鉴数据；只有通用 NPC 战斗支持。 | KP 只能口述法术效果；玩家无法学习、准备、消耗材料；怪物没有统一数值。 | 落点：`data/` 内容包与 `ruleset/coc7` 数据加载；配置：`content.packs`（按模组启用）；权限：站点管理员安装，房主启用。属于内容层，不建议写进检定核心。 |
| **M3** | 9.3 成为相信者（p.151）：长期接触神话会改变角色（POW、神话技能、疯狂）。 | 没有 POW 获取/损失流程。 | 神话研习的长期代价缺失。 | 落点：幕间流程；配置：`magic.pow_gain`（默认按书）；权限：KP。 |

### 4.8 物品与写回链（与 SAN 链路的一致性）

| 编号 | 书中原文要求（章节/页） | 当前实现的实际行为 | 影响 | 不写代码的实现方式（落点/配置/权限） |
| --- | --- | --- | --- | --- |
| **I1** | 装备会被消耗、丢弃、损坏；弹药与消耗品数量会减少。 | `character.add_item` 与 `rule item` 只支持新增/补充；`apply_room_changes` 的 item 分支只增不减。 | 弹药、药水、火把无法按规则消耗；背包只增不减。 | 落点：`item` 变更增加 `delta`（正负）与 `remove`；配置：`inventory.allow_consume`（默认开）；权限：玩家申报，KP 确认。 |
| **I2** | 护甲是装备的一种，影响战斗减伤。 | 卡面有 `armor` 输入，但房间快照与战斗参与者不读取（B6）。 | 装备了护甲也不生效。 | 见 B6；另在物品模型增加 `armor` 字段与装备槽。 |
| **I3** | 物品/武器写入应与 SAN 一样有"快照 → 命令/工具 → 留痕 → 归档写回"。 | 已具备：`pending_changes`、`item_changes`、`persist_member_card`、`character.add_item`、`rule item`。 | 主干正确。 | 保留；补消耗与删除。 |
| **I4** | 角色卡是完整档案，房间快照不应丢失持久字段。 | [characters.py L718-L756](../../backend/trpg_server/routes/characters.py#L718-L756) 与 [rooms.py L595-L627](../../backend/trpg_server/routes/rooms.py#L595-L627) 的 sanitize 不含 `armor`、`mp`、`assets`、`experiencedModules`、`friends`。 | 进房间后这些字段丢失或不可见；归档时可能被空值覆盖。 | 落点：快照白名单补齐持久字段；配置：`room.snapshot_fields`；权限：无。 |
| **I5** | 技能/属性修改应即时影响检定。 | 已具备：`apply_skill_change` / `apply_attribute_change` 同步房间卡与记录。 | 主干正确；但属性派生值不重算（B15），技能封顶 100（B13）。 | 见 B15、B13。 |

---

## 5. 可选规则清单（O 系列）

| 编号 | 规则（页） | 当前状态 | 建议 |
| --- | --- | --- | --- |
| O1 | 花费幸运（p.85） | 未实现 | 按 5.16 完整实现；默认开启但可关。 |
| O2 | 幸运恢复（p.85） | 未实现 | 每次聚会结束 1D100 > 当前幸运则 +1D10，上限 99。 |
| O3 | 先攻检定（p.106） | 未实现 | `combat.initiative_model=check`。 |
| O4 | 击晕（p.106） | 未实现 | 钝器战技，成功击晕并只造成 1 点伤害。 |
| O5 | 消耗幸运维持意识（p.107） | 未实现 | 每轮 1/2/4/8 点。 |
| O6 | 双方失败的处理（p.107） | 仅"无事发生" | 增加较高技能者胜、双方互伤两种模式。 |
| O7 | 无法得知伤害（p.107） | 未实现 | KP 只描述伤势，不显示数值。 |
| O8 | 火力压制（p.107-108） | 未实现 | 群体寻找掩体 + 随机命中。 |
| O9 | 护甲调整与局部护甲（p.107） | 未实现 | 霰弹枪逐骰扣甲、局部护甲幸运检定。 |
| O10 | 部位命中（p.108） | 未实现 | 1D20 表，可与护甲模型联动。 |
| O11 | 瞄准具/支架（p.108） | 未实现 | 基础射程翻倍、支架奖励骰。 |
| O12 | 战斗中移动（p.108-109） | 部分（MOV 与距离未强校验） | 明确 MOV×5 与奔跑射击惩罚。 |
| O13 | 完全隐蔽（p.109） | 未实现 | 对掩体射击，按遮蔽物大小定难度。 |
| O14 | 技能专攻转移（p.70） | 未实现 | 50%/90% 两个触发点，封顶 50%/90%。 |
| O15 | 精神固化（p.143） | 未实现 | 神话 > 当前 SAN 时损失减半。 |
| O16 | 疯狂洞悉（p.143） | 未实现 | 用疯狂换取线索。 |
| O17 | 多重理智检定（p.143） | 未实现 | 同一场景多次 SAN 检定的可选口径。 |
| O18 | 技能等级保险（p.40） | 未实现 | 创建时给技能下限。 |
| O19 | 闪避枪械（p.100 附近） | 未实现 | 可选，通常给高难度。 |
| O20 | 不知伤害 + 每次体质检定（p.107） | 未实现 | 与 O7 合并实现。 |

> 可选规则的原则：全部以 `house_rules` 或 `combat.optional.*` 开关接入，默认值保持当前行为；房主可开，站点管理员可禁用高风险项。

---

## 6. 必答一：孤注一掷（Push）设计（不写代码）

### 6.1 书中要求（5.3 p.74-76，4.4 p.45）

- 仅**技能与属性检定**可以 Push；**幸运、理智、战斗检定、伤害与理智损失掷骰不可 Push**；**对抗检定不可 Push**。
- 克苏鲁神话**可以** Push（书中示例）；v1 的"神话不可 Push"已撤回。
- 玩家必须描述"额外努力"；时间会流逝；目标必须仍然可实现；KP 可以"揭示预兆"说明失败后果。
- Push 失败后，后果必须升级，不能只是"再失败一次"。
- **大失败不能被 Push 无效化**（5.5 p.77 原文）。
- 一次检定最多 Push 一次。

### 6.2 现状

- 全库没有 Push 概念：没有"已失败"状态、没有失败后果契约、没有 Push 次数限制。
- 失败后 KP 可以无限次再调用检定，实际退化为无限重试。

### 6.3 建议落点

| 项 | 方案 |
| --- | --- |
| 状态 | 房间 `check_state` 保存最近一次检定的 `check_id / skill / target / result / pushed / consequence`。 |
| 入口 | `check_service.run_room_check` 接受 `push_of=check_id`；校验：原检定失败、技能/属性、非幸运/理智/战斗/对抗、未 Push 过。 |
| 后果 | Push 时必须填写 `stakes`（失败后果），KP 可先"揭示预兆"；失败后把后果写入日志与角色状态。 |
| 权限 | 玩家发起 Push，KP 确认后果；KP 可拒绝不合理 Push。 |
| 配置 | `push.enabled`（默认开）、`push.max_per_check=1`、`push.require_stakes=true`。 |
| 记录 | `check_records` 增加 `pushed`、`push_of`、`stakes`、`consequence`，供成长与复盘使用。 |

---

## 7. 必答二：KP 工具与房间命令对技能/属性/物品的修改能力（已改判）

### 7.1 现状（与 SAN 链路对照）

| 数值/对象 | 房间快照 | 命令 | KP 工具 | 归档写回 | 结论 |
| --- | --- | --- | --- | --- | --- |
| SAN | `max_san/current_san` | `rule san add/set` | `san` 相关工具 | 是（`pending_changes` + `persist_member_card`） | 完整链路，基准实现。 |
| 技能 | 房间卡 `skills` | `rule skill` | `character.set_skill` | 是（`apply_room_changes` 的 `skill`） | **已实现**；封顶 100（B13）。 |
| 属性 | 房间卡 `attributes` | `rule attr` | `character.set_attribute` | 是（`attr`） | **已实现**；派生值不重算（B15）。 |
| 物品/武器 | 房间卡 `items/weapons` | `rule item` | `character.add_item` | 是（`item` / `weapon`） | **只增不减**（I1）；护甲字段不完整（B6）。 |
| HP | `current_hp/max_hp` | 无 | 战斗内改 | **否** | 缺口（B16）。 |
| MP | 无 | 无 | 战斗内临时值 | **否** | 缺口（B7）。 |
| 护甲 | 无 | 无 | 无 | **否** | 缺口（B6）。 |

### 7.2 结论

- v1 的"KP 工具与房间命令都不能修改技能值或基础属性"**已改判为不成立**：现在两者都能改，而且共用 `pending_changes`、`apply_room_changes`、`persist_member_card` 的同一套链路。
- 但"和 SAN 完全一致"还差三件事：**HP/MP/护甲入链**、**派生值重算**、**技能超过 100**。
- 权限现状：命令由房主/管理员执行，KP 工具受角色权限约束；建议在 UI 中把"修改技能/属性"标记为高影响操作，保留操作者、旧值、新值、原因。

---

## 8. 实施顺序建议（不含代码）

1. **先修数学**：B1、B2、B3、B4、B12、B14。这些会直接改变掷骰结果，必须先做，并补统计与边界测试。
2. **再修资源**：B6、B7、B15、B16、I4、S10。让护甲、MP、HP、派生值、状态在房间与归档之间一致。
3. **补核心交互**：B5（Push）、O1（花费幸运）、E2（成长）、E4（幸运恢复）。
4. **补 SAN 生命周期**：S2、S3、S4、S5、S6、S7、S8。
5. **补战斗细节**：F1-F10、E8。
6. **补内容与可选**：O3-O20、CH1（追逐）、M2（法术/典籍/图鉴）。
7. **最后做 UI 与提示词**：E8 战斗房规入口、`prompt_fragments` 注入检定口径与 Push 提示。

---

## 9. 需要确认的口径（不阻塞审查）

1. **SAN 上限**：按您的裁决以 POW 为准；本 PDF p.130 另有"99 − 神话"的表述。建议在项目内固定一条口径并写入规则说明，避免后续实现反复。
2. **"一天"的边界**：不定性疯狂的日累计需要一个"游戏日"定义（自然日、长休、场景切换）。建议默认"KP 宣布长休或场景切换时结算"。
3. **技能上限**：规则允许超过 100；是否需要 `skills.max_value` 防脏数据，建议设为 200 而不是 100。
4. **房规阈值模式**：默认 `auto`（按最终上限派生）还是 `flat`（房主填绝对值）。建议默认 `auto`，房主显式选择 `flat` 时才使用平面值。
5. **内容层范围**：法术、典籍、怪物图鉴是"规则引擎"还是"模组内容包"。建议放内容包，按模组启用。

---

## 附录 A：风险与回归重点

- **概率回归**：奖励/惩罚骰每档 10 万次统计；+1/−1、+2/−1、+2/−2、−3；技能 49/50/95/99/100 的大失败边界。
- **状态回归**：重伤 + 临时疯狂 + 昏迷 + 死亡并存；SAN 状态覆盖身体状态（S10）。
- **写回回归**：战斗后 HP/MP/护甲/技能/属性/物品在换房间与归档后保持一致。
- **权限回归**：玩家不能直接改技能/属性；KP 工具与房主命令的操作留痕。
- **兼容回归**：旧角色卡没有 `armor/mp/assets` 字段时，快照与归档不能把旧值清空。

## 附录 B：本轮引用到的关键落点

- 检定数学：[dice.py L57-L71](../../backend/trpg_server/agents/tools/dice.py#L57-L71)、[L216-L271](../../backend/trpg_server/agents/tools/dice.py#L216-L271)、[check_service.py L99-L102](../../backend/trpg_server/check_service.py#L99-L102)、[checks.py L58-L100](../../backend/trpg_server/ruleset/coc7/checks.py#L58-L100)
- 战斗：[combat.py L341](../../backend/trpg_server/agents/tools/combat.py#L341)、[L718-L748](../../backend/trpg_server/agents/tools/combat.py#L718-L748)、[L1385-L1420](../../backend/trpg_server/agents/tools/combat.py#L1385-L1420)、[L1645-L1690](../../backend/trpg_server/agents/tools/combat.py#L1645-L1690)
- SAN：[dice.py L1010-L1105](../../backend/trpg_server/agents/tools/dice.py#L1010-L1105)、[L1110-L1240](../../backend/trpg_server/agents/tools/dice.py#L1110-L1240)、[room_rules.py L256-L324](../../backend/trpg_server/room_rules.py#L256-L324)
- 写入链：[room_rules.py L416-L575](../../backend/trpg_server/room_rules.py#L416-L575)、[L896-L1070](../../backend/trpg_server/room_rules.py#L896-L1070)、[character_service.py L352-L403](../../backend/trpg_server/character_service.py#L352-L403)、[character_stats.py](../../backend/trpg_server/agents/tools/character_stats.py)
- 房间设置与快照：[rooms.py L61-L62](../../backend/trpg_server/routes/rooms.py#L61-L62)、[L123-L170](../../backend/trpg_server/routes/rooms.py#L123-L170)、[L595-L655](../../backend/trpg_server/routes/rooms.py#L595-L655)、[characters.py L718-L756](../../backend/trpg_server/routes/characters.py#L718-L756)
- 前端：[character-sheet.ts L692-L694](../../frontend/src/app/character-sheet.ts#L692-L694)、[L738-L745](../../frontend/src/app/character-sheet.ts#L738-L745)、[L956-L965](../../frontend/src/app/character-sheet.ts#L956-L965)、[03-room-tools-auth-settings.html L73-L82](../../frontend/src/index/fragments/03-room-tools-auth-settings.html#L73-L82)
