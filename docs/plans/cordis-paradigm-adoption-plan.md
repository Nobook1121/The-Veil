# Cordis 范式借鉴计划（RFC，未实施）

> 状态：**提案** —— 本文档只是计划，尚未改动任何代码。
> 日期：2026-11-19
> 背景：调研 DeepSeek Harness 内置的 Cordis 插件框架能否参考应用于 The Veil 的插件化体系。
> 配套材料：`research/cordis/Cordis-调研报告.md`（外部框架调研）、`research/cordis/`（抓取的官方文档原文）。

---

## 0. 结论先行

**能否参考应用？—— 范式可借鉴，框架本身不可移植。**

- **直接复用 Cordis：不可行。** Cordis 是 TypeScript/Node 元框架（Koishi 作者 Shigma 的 v4，DeepSeek 以 vendor 方式内置进 deepseek-harness），其 Context/Proxy/声明合并/HMR 全部建立在 Node 生态上，The Veil 后端是 Python Flask，语言层不通。
- **移植其范式：可行且高度对口。** Cordis 论文（《A Programming Paradigm for Spatiotemporal Composability》，arXiv:2608.25512）的两个核心主张 —— **副作用可逆化（Revertible Effects）** 与 **依赖反应式管理（Reactive Coeffects）** —— 恰好命中本仓库插件体系当前最大的三个结构性债务（见 §2）。这不是巧合：Cordis 论文的靶子就是 VSCode 那种「manifest + 能力门控、插上去拔不下来」的体系，而 The Veil 的 `plugins/` 体系正是同一形态。

一句话：**不引入 Cordis，引入「注册即副作用 + 撤销链」的运行时纪律，用 Python 写一个克制的 mini 内核。**

---

## 1. Cordis 是什么（速览，详见调研报告）

| 概念 | Cordis 机制 | 一句话 |
| --- | --- | --- |
| Context | 插件加载时从父上下文派生子上下文，插件全部副作用挂在自己 ctx 上 | 「副作用的插座」 |
| Effect | `ctx.effect(() => { ...; return disposer })`，卸载时 LIFO 逆序执行 disposer | 注册即副作用，注册带回执 |
| Service | `ctx.provide(key, obj)` + 消费方声明 `inject: ['key']`，一对多按名查找 | 类 IoC，但服务消失时依赖者自动回滚卸载 |
| Fiber | PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED / FAILED 状态机 | 每个插件实例一个可观测状态 |
| 事件 | `namespace/action` 扁平命名，emit/waterfall/serial/bail/parallel 五模式 | 拦截用事件，直调用服务方法 |
| HMR | 事务性模块热替换（接受/回滚判定 + 失败回滚旧代码） | 本计划**不借鉴** |

Cordis 与 manifest 式体系的本质区别：manifest 回答「你是什么、允许你做什么」（静态准入）；Cordis 回答「你改变了什么、你依赖的世界变了怎么办」（可逆计算 + 反应式依赖）。

## 2. 现状痛点与 Cordis 范式的对应关系

来源：本仓库插件体系代码审计（`plugins/`、`ruleset/`、`routes/plugins.py`、三个真实插件），证据均可在代码注释与 `docs/plans/ruleset-boundary-audit.md` 复核。

| # | 现状痛点 | 证据 | Cordis 对应解法 |
| --- | --- | --- | --- |
| P1 | **Flask 蓝图不可撤销，「禁用」是假的** —— 代码插件 disable 后路由仍活，只能 `pending_restart` 等重启 | `registry.py` / `contracts.py:PluginRecord` 注释、`docs/plugin-development.md §6` | 撤销链：蓝图挂载必须收进 effect。Flask 本身不支持摘除蓝图 → 需代理路由层（§4 阶段三） |
| P2 | **ruleset 边界是名义边界** —— 核心仍有 8 处直 `import ruleset.coc7`，2026-11-09 后还在回潮；「删掉 coc7 核心仍能启动」未达成 | `docs/plans/ruleset-boundary-audit.md §0`，收敛方案 P1/P2/P3 标注未做 | 无特权内核：核心按服务名查找规则数学，coc7 只是一个服务提供方 |
| P3 | **enable/disable 是全量重跑** —— 任何启停都触发 `registry.load()` 全生命周期重跑（重发现、重导入、重建全部注册表） | `registry.py:enable/disable/rescan` | Fiber 增量协调：只动目标插件及其依赖子图 |
| P4 | **导入方式全局突变** —— `_import_module` 临时插 `sys.path`；插件被迫单文件巨石化（veil-map entry.py 5243 行手写 `spec_from_file_location` 加载子模块） | `registry.py:_import_module`、veil-map `entry.py:_load_local_module` | 作用域化加载（借理念，Python 侧用隔离的 import 机制） |
| P5 | **契约死重** —— manifest 的 `capabilities.*.module` 被 schema 要求填写但实际被 `code.entry` 短路，误导第三方开发者 | `docs/plugin-development.md §3` | 契约表面积 = 实际语义（小问题，随手修） |
| P6 | **声明了不实现** —— `settings_sections`、`frontend_assets` 只校验不落地；`ui` 六槽位前端只消费 1 个；sandbox V1 强制禁用 | 文档 §4.5/§4.6、`ruleset/ui.py` 模块头 | 不由本计划解决，但增量启停内核会让补齐成本更低 |
| P7 | **依赖是一次性检查** —— manifest `dependencies` 在加载时校验一次，运行期无跟踪；服务提供方消失时消费方行为未定义 | `registry.py` 拓扑排序 + `_mark_failed_dependents`（仅加载期） | Reactive Coeffects：持续跟踪，Provider 消失 → Consumer 自动卸载，恢复 → 自动重载 |

**反向结论（为什么不整体换掉）**：The Veil 的 `plugins/` 体系并非烂摊子 —— manifest 校验、15 种结构化失败码、资源审计、默认不启用、异常围栏，工程上已经很扎实，且已用 dnd5e / veil-map 合计约 1.6 万行真实插件完成全链路验证。**值得换的是生命周期语义，不是清单体系。** Cordis 的 manifest-less 纯代码注册（`ctx.plugin(fn)`）反而不如现有的声明式 manifest + 白名单适合「管理员显式安装」的信任模型。

## 3. 借鉴 / 不借鉴清单

**借鉴（核心四件套）**
1. `ctx.effect(fn)` —— 注册带回执（disposer），卸载 LIFO 逆序撤销。内建 API（注册工具/蓝图/模板/守卫/服务）本身就是 effect，插件作者大多数时候无感。
2. Service 按名注册 + `inject` 声明式依赖 + 反应式跟踪（P7）。依赖声明从 manifest 的静态字段升级为运行时持续协议。
3. Fiber 式插件实例状态机（ACTIVE / FAILED / PENDING-waiting-deps），`GET /api/plugins` 的 summary 直接透出状态与「在等谁」。
4. 撤销顺序纪律：Provider 先停新绑定 → Consumer 用 committed 绑定清理 → Consumer 全退 → Provider 执行逆操作（服务多提供方场景下的退出顺序）。

**不借鉴（明确排除）**
- ❌ HMR / 事务性模块热替换 —— Python 无对应生态收益，本地开发重启成本可忽略；Cordis 官方也自认这是最脆弱的部分。
- ❌ 声明合并 / Proxy 访问控制 / isolate realm —— TS 类型体操与深层运行时隔离，Python 侧成本远大于收益；本仓库信任模型（管理员显式安装）不需要。
- ❌ 五模式事件总线 —— 现阶段只需要 `serial`（守卫链）与 `emit`（广播）两种，veil-map 的 `register_action_guard` 模式已经够用；不新造全局事件系统。
- ❌ cordis.yml loader / 柯里化 Schema —— 已有 `plugin.json` + `enabled.json` + `config/schema.py` FieldSpec 体系，理念等价，不推倒。
- ❌ 「无特权内核」的激进形态 —— 不把核心拆成插件拼盘（dsh 那种形态）。只做**核心 → 服务查找**的定向收敛（P2），保留核心框架职责。

## 4. 分阶段实施计划

约束：每阶段独立可交付、可回滚，全程不破坏现役三个插件（veil-map / dnd5e / dnd5e-srd）与既有 HTTP 契约。测试纪律：每阶段新增 pytest 用例先于实现合入，全程跑 `.\scripts\verify.ps1`。

### 阶段 0：契约冻结与试点选定（0.5 天，纯文档）

- 本 RFC 定稿，`research/cordis/` 归档不删。
- 修订 `docs/plugin-development.md`：明确 lifecycle v2 演进方向（预告，不删 v1 描述）。
- 选定试点能力：**agent_tools + content_packs + register_service / action_guard**（全部内存注册、天然可逆、有 veil-map/dnd5e 真实用例，不碰 Flask 路由）。

### 阶段 1：mini 内核（`backend/trpg_server/plugins/kernel/`，预估 300–500 行 + 测试）

新模块，不改动现有 registry 行为（并行共存）：

- `context.py`：`PluginContext` —— 每插件一个，持有效果栈；`effect(fn)` 执行 fn 收集 disposer；`dispose()` LIFO 逆序执行全部 disposer（异步 disposer 用同一 effect 包住保证顺序）。
- `services.py`：`ServiceRegistry` —— `provide(key, obj, ctx)` / `get(key, ctx)` / `inject(list, callback)`；提供方 dispose 时自动卸载所有注入方（反应式），双方回滚后状态落 PENDING-waiting。
- `fiber.py`：`Fiber` 状态机（只保留 ACTIVE / FAILED / WAITING 三个状态 + 失败原因码），挂进现有 `PluginRecord`。
- 硬性红线：**内核不 import Flask、不 import 业务模块**；单测覆盖撤销顺序、依赖消失回滚、异常 disposer 不中断兄弟撤销。

### 阶段 2：registry 切换注册语义（试点能力，1–2 天）

- `_activate` 内所有 `runtime.register_*` 调用改为经 `ctx.effect` 包装：成功注册 → 收 disposer；插件 `_rollback` 语义从「重跑全量」改为「dispose 该插件 fiber」（消灭 P3 的主要场景：纯数据与工具类插件启停即时生效、零重启）。
- `disable()` / `enable()` 变为增量操作：只重建目标插件 fiber + 其依赖子图（`_mark_failed_dependents` 的正向版）。
- HTTP 契约不变：`restart_required` 字段保留，仅在**插件含 http_blueprints 且尚未完成阶段三**时为 true。
- 验收：veil-map 的 `map_service`、action guard、四个 agent 工具在 enable/disable 循环中注册/撤销往返 100 次，核心注册表状态零残留（对拍测试）。

### 阶段 3：HTTP 蓝图可逆化（最难，独立评审后再动工，2–3 天）

Flask 蓝图注册不可撤销是平台事实，两条路线：

- **路线 A（推荐）：插件代理路由层。** 宿主只注册一个总蓝图 `/api/plugins/<plugin_id>/<path>`（现有插件路由本就强制此前缀约定），内部查「插件路由表」分发到插件 blueprint 的 view functions。禁用 = 从表中摘除（可逆、即时）。插件 manifest 的 `http_blueprints` 声明不变，只是挂载点变了。代价：`url_for` 在插件内不可用（现有插件未用）、需补 404 与权限短路。
- 路线 B（保底）：维持 `pending_restart` 语义，但把它从「全体代码插件」收窄为「仅含 http_blueprints 的插件」，其余能力全部即时可逆。
- 先做一次路线 A 的技术验证 spike（单独分支），评审通过再合入。

### 阶段 4：ruleset 边界收敛合流（与既有审计计划合并执行，3–5 天）

这不是新任务，是把 `docs/plans/ruleset-boundary-audit.md` 里标注「未做」的 P1/P2/P3 收敛方案改用服务化语义执行：

- 核心 8 处直 `import ruleset.coc7`（room_chase / room_interlude / room_magic / room_rule_commands / room_sanity / turn_pipeline）改为经规则集服务查找（`ctx.get('ruleset')` 的 Python 等价物，走 `ruleset.capabilities` 既有入口）。
- 验收升级为可机测：**临时移除 coc7 注册后核心模块可启动**（审计文档原话「删掉 coc7 后核心仍可启动」的测试化）。
- 此阶段与 Cordis 范式的关系：借「无特权内核」的理念，但收口动作完全沿用已有审计方案，避免两套改造打架。

### 阶段 5：契约清理与体验收尾（1 天）

- manifest `capabilities.*.module` 字段：schema 中降为可选并在文档删除误导描述（P5）。
- 工具命名 mangle：加载期强校验「插件 id + 单级工具名」，多级命名空间直接 `manifest_invalid`（文档 §10 已预警，补上机测）。
- `GET /api/plugins` summary 透出 fiber 状态（`active/failed/waiting:缺谁`），替代现在只能看 reason_code 的方式。
- （远期另立项）前端插件 iframe sandbox + postMessage：文档 §8 已自行规划，属安全边界而非 Cordis 范式范畴，不纳入本计划。

### 时间线汇总（人日为粗估）

| 阶段 | 内容 | 估时 | 依赖 |
| --- | --- | --- | --- |
| 0 | RFC 定稿 + 试点选定 | 0.5d | — |
| 1 | mini 内核（context/services/fiber） | 1–2d | 0 |
| 2 | registry 注册语义切换（增量启停） | 1–2d | 1 |
| 3 | HTTP 代理路由层（先 spike 后评审） | 2–3d | 2（可并行评审） |
| 4 | ruleset 去硬依赖收敛（合流既有方案） | 3–5d | 1（可与 2/3 并行） |
| 5 | 契约清理 + 状态透出 | 1d | 2 |

合计约 8–13 人日；阶段 1–2 完成后即获得「除 HTTP 外全部能力真·即时启停」的可见收益，阶段 3/4 可按需推迟。

## 5. 风险与代价（Cordis 官方踩过的坑，提前立规矩）

1. **抽象税**：现 `plugins/` 核心 9 文件 2666 行，内核必须克制（≤500 行红线）。凡是现有 `registry.py` 已经能做的（拓扑排序、冲突检测、版本区间），不搬进内核重写。
2. **运行时不验证逆操作正确性**（Cordis 官方明示）：disposer 写错是插件作者责任 → 用「启停往返对拍测试」作为发布门禁（§阶段 2 验收），并在 `docs/plugin-development.md` 把「写 effect 必须写 disposer」定为契约。
3. **静默 WAITING 是头号调试疑难**（Cordis 社区公认）：凡进入 waiting 态的插件必须在 API summary 与日志中显式列出缺失依赖名，禁止静默。
4. **向后兼容**：现役插件与 `manifest schema_version=1` 原样可用；内核是内部实现替换，manifest/HTTP 契约只在阶段 5 做减法（删死重字段），不做破坏性变更。
5. **系统边界**：不可逆副作用（对外发消息、写盘）不能假装可逆 —— 插件文档需明确「暂缓输出或补偿操作」纪律；本平台插件多数操作是内存注册与本地文件，天然落在可逆边界内。
6. **学习成本**：新概念控制在三个（Context / effect+disposer / service inject），全部映射到开发者已熟悉的现有 API（`register_agent_tools` 等），官方称此为「无感性」—— 目标是插件作者**不学新东西也能写出正确可卸载的插件**。

## 6. 决策点（开工前需拍板）

1. 是否接受路线 A（插件代理路由层）作为 http_blueprints 可逆化的方向？—— 建议先跑 spike 再定。
2. 阶段 4 是否与 `coc7-rules-roadmap.md` 的排期协调合并？—— 建议 whoever 先动工，另一份计划标注依赖。
3. `settings_sections` / `frontend_assets` / `ui` 其余槽位这些「声明了不实现」的半成品（P6），是本计划外单独排期，还是接受现状？—— 本计划默认不碰。

---

*参考：`research/cordis/Cordis-调研报告.md`（框架机制详解与来源链接）、`docs/plans/ruleset-boundary-audit.md`（P2 依据）、`docs/plugin-development.md`（现契约权威）。*

---

## 7. 决策记录（2026-11-19，已拍板）

按本计划建议执行：

1. **HTTP 可逆化走路线 A（插件代理路由层）**，先做技术验证 spike（独立模块 + 测试，不接线进 app 启动路径），spike 通过并评审后才把 `register_blueprints` 切换到代理层。
2. **阶段 4（ruleset 去硬依赖收敛）推迟**，与 `coc7-rules-roadmap.md` 排期协调后另起实施；本计划只负责阶段 0/1/2/3(spike)/5。
3. **P6 半成品能力（settings_sections / frontend_assets / ui 其余槽位）本计划不碰**，另行排期。

实施顺序：阶段 0（本文档决策落档）→ 阶段 1（mini 内核）→ 阶段 2（registry 注册语义切换 + 增量启停）→ 阶段 3（仅 spike，不接线）→ 阶段 5（契约清理）。每阶段合入前跑目标测试，收尾跑全量 `.\scripts\verify.ps1`。

### 实施状态（2026-11-19 收工盘点）

- ✅ 阶段 0：决策落档（§7 上方）、`docs/plugin-development.md` 预告已改写为「已实施」
- ✅ 阶段 1：`plugins/kernel/` 468 行 + `tests/test_plugin_kernel.py`（12 用例，含零依赖守卫）
- ✅ 阶段 2：registry effect 撤销链 + 增量启停 + fiber 透出；`tests/test_plugin_incremental_lifecycle.py`（100 次往返对拍）
- ✅ 阶段 3（spike + 正式接线）：spike 差分对拍判定路线 A 成立后已接线——`app_factory` 在核心蓝图之后注册代理总蓝图一次，插件蓝图经 `route_proxy.mount` 挂在各自效果链上，禁用即摘表、路由立即 404；`restart_required` / `pending_restart` 恒为 `false`（API 兼容保留），蓝图不可逆的历史限制废除
- ✅ 阶段 5：module 字段可选 + 工具名解析期强校验（fiber 透出已在阶段 2 落地）
- ▶ 阶段 4（2026-11-19 启动，P0 批次完成）：ruleset 去硬依赖收敛。优先级拍板为「边界先于内容」。**P0 已落地**——启动路径注册表解耦：coc7 缺席时进程可启动（4 断言机测）、CoC 命名工具按注册表组装、`tests/test_ruleset_boundary_guard.py` AST 守卫锁定白名单外零新增耦合（P1 六模块 15 处既有 import 进白名单登记）。**P1 已落地（同日）**——六模块 15 处函数内 import 全部改走注册表 + 能力层，守卫白名单收窄到设计内持有者，缺席场景优雅降级有机测锚定。**默认规则集参数化批次已落地（审计 §4.4 第 1 条剩余部分）**——新增 `[ruleset] default_id` 配置项（FieldSpec → 模板 → 同步测试全链，`DEFAULT_RULESET_ID = "coc7"` 收敛为模板默认值单一真源），全部缺省引用方经 `default_ruleset_id()` 统一入口取值；注册表回退链：配置可用 → 用之，不可用/未配置 → 第一个可用插件（注册顺序即确定性来源，WARNING 指认双方），空注册表 → 带区分文案的明确错误（「已配置默认 <id> 但不可用」/「未配置且无可用规则集」）；灰卡占位（declared）核验为已收编（空清单 + 装配点参数化，dnd5e 占位已被真实插件取代），AST 守卫锁定 `DeclaredRuleset` 构造只允许出现在装配点。后续批次经 2026-11-20 全面核账重排：核账发现 RollPolicy / CheckPolicy / 前端 SchemaDescriptor / StateKeys 均已由早期工作落地，审计条目约七成完成。真实剩余排序：**B1 战斗规则集上下文（✅ 已完成，2026-11-20）——R1 静默错位修复（战斗掷骰 / 等级带 / NPC 体格 DB 显式携带房间规则集）+ `test_combat_golden` 金样护栏** → **D1 战斗语义迁入插件（✅ 主体完成，2026-11-20）——四个 CoC 战斗语义块迁入 `combat/rulesets/coc7.py` 的 `Coc7Ruleset`（徒手武器条目 `unarmed_weapon` / 极限伤害合成 `extreme_damage` / 全自动配比 `auto_fire_composition` + 卡壳判定 `weapon_malfunction` / 先攻数值合成 `initiative_score` + NPC 缺省理智 `initial_sanity`，共六个钩子），核心只留 `combat/registry` 中性取用点（缺席钩子退中性徒手 / 满骰面不贯穿 / 恒不卡壳 / 全中不贯穿 / 默认等级表×1000+DEX / 理智 0，空注册表与未声明钩子两形态均有测试锚定）；战斗 SAN 读写改按规则集 `san` 资源槽位驱动（coc7 声明槽位、dnd5e 无 → 战斗者无 `san` 字段、持久化不写 `current_san`、施法 SAN 扣除整体跳过）；`DamageOutcome` 伤势四字段软化 `bool | None`（唯一消费方判空按 False 呈现）；金样三条链路逐字节不变锚定搬家零漂移，`test_ruleset_d1_combat_semantics.py` 17 用例** → C1 CoC 卡形状转换下沉插件 → E1 前端残留三件（checkTool / chat sanity 写回 / 叙述词表）→ F1 变更类型参数化。D1 后续跟进项（主体未覆盖，另行批次）：`weapon_catalog` 武器表整块搬插件（数据层仍在核心）、shoot 流程文案与射程带分派（dnd5e 房间 `combat.shoot` / `combat.treat` 仍跑 CoC 流程）、`MELEE / RANGED / DODGE_SKILL_NAMES` 技能名表下沉。R2 事实（dnd5e 房间 shoot / treat 仍跑 CoC 流程）已由 B1 探针用例固化，D1 主体不动这两条链路，探针继续作 tripwire，待 shoot 分派批次落地时改写为插件语义断言。
- 全量验证：`.\scripts\verify.ps1` 通过（阶段 2 收尾时全绿；阶段 3/5 后由全量 pytest 2244 passed 复核）
