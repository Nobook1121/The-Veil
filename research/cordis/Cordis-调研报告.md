# Cordis 插件框架调研报告

> 调研日期：2026-11-19 ｜ 调研方式：WebSearch + WebFetch（arXiv、GitHub、官方文档站、中文社区解读）
> 调研范围：纯调研，不含代码实现

---

## 0. 一句话结论

**"DeepSeek Harness 的 Cordis" 是真实存在的**：DeepSeek 官方开源的 Agent 运行时 **deepseek-ai/deepseek-harness**（口号 "Everything is a Plugin"）以 **vendor 方式**内置了 Cordis 作为其底层插件框架；而这个 Cordis 正是 Koishi 聊天机器人框架作者 **Shigma（本名 Yifan Shi，北京大学 + DeepSeek 员工）** 开发的元框架的 v4 升级版，并在 2026 年 8 月由 Yifan Shi、Wei Zhang（北大副教授）、Tianyi Cui（DeepSeek Harness 团队负责人）三人写成论文《A Programming Paradigm for Spatiotemporal Composability》（arXiv:2608.25512）。用户既没有记混名字，也没有记混项目来源——它同时是"Koishi 的 Cordis"和"DeepSeek Harness 的 Cordis"。

---

## 1. 关于 "deepseek harness" 的搜索结论

### 1.1 结论：找到了，证据链完整

| 证据 | 内容 |
|---|---|
| GitHub 仓库 | https://github.com/deepseek-ai/deepseek-harness ，描述 "DeepSeek Harness: Everything is a Plugin."，topics 含 `ai-agents`、`cordis`、`dsh`、`dsh-plugin`；检索时 283.9k stars / 35.2k forks / 28,264 commits / 41 releases，MIT 协议，仓库创建于 2026-08-13 |
| README 原文 | "DeepSeek Harness (dsh) is an open-source agent harness developed by DeepSeek AI. It is built on an **everything-is-a-plugin** architecture and **powered by Cordis**, whose design is described in *A Programming Paradigm for Spatiotemporal Composability*." |
| 官方文档 | https://deepseek-harness.github.io/deepseek-harness/ 含独立《Cordis 教程》7 章、《Cordis 入门》参考、Cordis 核心 API 参考（Context/Service/Events/Registry/Fiber） |
| 引入方式 | Cordis 以 **vendor 方式**引入（完整源码拷贝至仓库 `vendor/cordis/`，包名改挂为 `@deepseek-ai/cordis`），启动器为 `vendor/cordis/bin.js`（创建根 Context → 挂载 Loader 插件 → 从 `cordis.yml` 加载插件树） |
| 供应链关系 | Koishi 用的 Cordis v3（koishi 生态，npm 包 `cordis`）；论文描述并随 dsh 发布的是 **Cordis v4**（理论重构版）。腾讯云解读明确："Cordis 作者 Shigma(Yifan Shi) 早年做聊天机器人框架 Koishi，动态装卸插件正是其基因，2023 年重构为 Cordis，2024 年进入 DeepSeek-V3 技术报告作者名单"；什么值得买社区说法："DeepSeek 把作者连同整套源码一起收编"；量子位："而今发布的 DeepSeek Harness，正是 Koishi Cordis 的升级版" |

### 1.2 搜索中需排除的同名干扰项

- **cordis.com** —— 美国/欧洲心血管介入医疗器械公司（60+ 年历史），与软件无关
- **cordis.europa.eu** —— 欧盟研发与创新项目的官方数据库（CORDIS），与软件无关
- 另注意 GitHub 上存在多个不相关的同名/近名仓库，需认准 `deepseek-ai/deepseek-harness`、`cordisjs`（Cordis 官方组织）、`cordiverse/paper`（论文托管仓库，DeepSeek 官方同步）、`koishijs/koishi`

### 1.3 时间线（来龙去脉）

1. **~2020**：Shigma 发布 Koishi v4（跨平台聊天机器人框架，名字源自东方 Project 角色古明地恋），插件化是其核心特性
2. **2023**：Shigma 为 Koishi 官方文档撰写设计文章《可逆的插件系统》（至今仍是 Cordis 官方文档"理念"章节，用范畴论形式化插件可逆性）；同年将 Koishi 底层抽出为独立框架 **Cordis v3**（名字取自拉丁语"心"——"Koishi 的一切都从 Cordis 开始"）
3. **2024**：Yifan Shi 进入 DeepSeek（出现在 DeepSeek-V3 Technical Report 作者名单中）
4. **2026-08-13**：DeepSeek Harness 开源发布（首个产品化 Cordis v4 应用）
5. **2026-08-26**：论文《A Programming Paradigm for Spatiotemporal Composability》提交 arXiv（92 页，cs.PL），正式将 Cordis v4 形式化为"时空可组合性编程范式"

---

## 2. 来源链接列表

### 权威来源（一手）

| 来源 | 链接 |
|---|---|
| DeepSeek Harness 仓库 | https://github.com/deepseek-ai/deepseek-harness |
| DeepSeek Harness 官方文档 | https://deepseek-harness.github.io/deepseek-harness/ |
| — Cordis 教程（7 章实践） | https://deepseek-harness.github.io/deepseek-harness/develop/cordis-tutorial/ |
| — Cordis 入门（概念参考） | https://deepseek-harness.github.io/deepseek-harness/reference/cordis-primer |
| — Cordis 核心 API（Context/Service/Events/Registry/Fiber） | https://deepseek-harness.github.io/deepseek-harness/reference/cordis-api/context 等同名目录 |
| — 框架能力（插件与生命周期/服务与依赖/事件系统） | https://deepseek-harness.github.io/deepseek-harness/develop/framework/ |
| — 架构文档 | https://deepseek-harness.github.io/deepseek-harness/reference/ |
| Cordis 官方文档站 | https://cordis.moe/（GitHub 组织：https://github.com/cordisjs） |
| — 理念：可逆的插件系统（数学推导） | https://cordis.moe/zh-CN/guide/philosophy/disposable.html |
| — 基础：认识插件 / 生命周期 / 服务与依赖 / 插件配置 | https://cordis.moe/zh-CN/guide/essential/ |
| 论文 arXiv 页 | https://arxiv.org/abs/2608.25512 |
| 论文官方托管仓库 | https://github.com/cordiverse/paper |
| Koishi 框架 | https://github.com/koishijs/koishi |
| Schemastery（配套校验库） | https://github.com/shigma/schemastery |

注：旧文档站 cordis.koishi.chat 已不可达（SSL 525 错误），现行权威文档为 cordis.moe。

### 解读来源（二手，中文）

- 腾讯云开发者社区《解读 Cordis：dsh 一切皆插件背后的运行时设计》 https://cloud.tencent.com/developer/article/2731762 （对论文的逐章解读，质量最高）
- 量子位《DeepSeek 的「自进化」蓝图，曝光了》 https://app.myzaker.com/news/article.php?pk=6a7f7c168e9f0959fd73c542
- 掘金《DeepSeek Harness 里的 CORDIS，到底是个啥？【一】》 https://juejin.cn/post/7674341421633093659
- 知乎专栏《全文首发 deepseek harness 背后的秘密，Cordis 的设计哲学》 https://zhuanlan.zhihu.com/p/2071345388896924946 （403 未能直接抓取正文，仅见摘要：论文实现 Cordis v4，提供 ctx.effect / ctx.set / ctx.get / ctx.use / dependency 语义）
- 腾讯云《两种 Harness 哲学：从 DeepSeek Harness 的"过度抽象"争议看另一种答案》 https://cloud.tencent.com/developer/article/2727010
- 百度百科 Cordis 词条 https://baike.baidu.com/item/Cordis/68637248 （词条描述与官方一致："面向时空可组合性的插件框架，核心是一个元框架，仅负责插件的加载、卸载以及依赖关系管理"；403 未抓取正文）

---

## 3. 框架定位

**Cordis 是一个"元框架"（meta-framework）**：它不绑定任何具体领域（不是机器人框架、不是编辑器、不是 Agent 框架），只提供"插件如何挂载、卸载、互相依赖"这套**通用的动态组合语义**。框架作者在其上封装领域 API（Koishi 封装成聊天机器人，DeepSeek Harness 封装成 Agent 运行时）。

官方定义的三句话定位：

1. **"不存在需要打补丁的特权内核"** —— dsh 的每一部分都是插件，包括模型适配器、工具注册表、会话日志、乃至 agent loop 本身；"扩展 dsh 的方式是把插件挂载到其他插件旁边"。
2. **"插件描述自己的贡献，配置文件组合应用"** —— 插件文件里没有框架启动代码；`cordis.yml` 是插件树的声明。
3. **"注册是可逆的副作用"** —— 所有注册随所属插件卸载而自动撤销。

论文给出的核心命题（摘要直译）：现代软件——从插件系统到自进化 Agent harness——日益需要**动态组合**，但其形式化基础薄弱。论文将其拆为两个正交维度：

- **时间可组合性（temporal composability）**：组件被移除时，其副作用可被完全回退；
- **空间可组合性（spatial composability）**：组件间依赖可声明并被响应式管理。

解法是把编程语言理论中经典的 **effect（效应，程序如何改变环境）** 与 **coeffect（余效应，程序需要环境提供什么）** 这对对偶概念，从静态类型系统工具**提升为运行时机制**：

- **Revertible Effects（可逆效应）**：每一次上下文变换都携带一个由运行时持有的逆操作 → 单组件的时间可组合性；
- **Reactive Coeffects（响应式余效应）**：每一次上下文变化都对照该组件的 coeffect 规格分类，驱动其激活/停用 → 单组件的空间可组合性；
- 两者统一于单一 Context 类型，经其中介产生"上下文范式"（Context Paradigm），元理论证明不同组件的效应可交错执行而互不干扰。

官方文档《可逆的插件系统》的数学表达：副作用函数 f: C→C 在复合 ∘ 下构成幺半群；要求每个 f 存在逆元 f⁻¹ 后即构成**群**。`effect` 变换定义为 f ↦ (c,h) ↦ (f(c), h∘f⁻¹)（可以证明它是同态），`restore` = (c,h) ↦ (h(c), id) 用于回收副作用。**"副作用就是对资源的占用，计算机内的资源设计出来就是可重复使用的，所以这些副作用一定是可逆的。"** 文档还把这一关系类比 Rust：Rust 用所有权机制在语言层强制内存安全，Cordis 用上下文机制在应用层提供资源安全（但**非强制**）。

由此得出框架/库/插件的三分法：**不产生副作用的是库；会产生副作用的是插件；能管理副作用的是框架。**

---

## 4. 核心机制详解

以下机制均以 DeepSeek Harness 的 Cordis v4 文档为准，标注 Koishi v3 差异处。包名 `@deepseek-ai/cordis`（Koishi 生态中即 `cordis`/`koishi`）。

### 4.1 Context 模型与插件生命周期

**Context（上下文）**是 Cordis 的核心对象："一个服务占据一个稳定的 `ctx.<key>`"——所有服务、事件、生命周期 API 都经 `ctx` 访问。官方把上下文比作**"副作用的插座"**：

> 当上下文被卸载时，它将会把所有的副作用一一回收。插件就是连接到另一个插座的插头，管理着子级上下文的全部副作用。

- 每个插件加载时，从当前上下文**派生**（fork/extend）出一个子上下文；子上下文管理插件内全部副作用，而"插件整体作为一个副作用被父级上下文收集"——递归形成**组件树**。
- 论文 Component 三元组：**d**（需要哪些依赖）、**p**（提供哪些依赖）、**e**（激活后产生的 effect 及其逆操作）。组件的一次运行时实例化称为 **fiber**。

**Fiber 状态机**（每个已加载插件实例一个 fiber）：

```
PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
                ↘ FAILED
```

| 状态 | 含义 |
|---|---|
| PENDING | 已声明，但所需服务不可用（依赖未就绪）——静默等待，不崩溃、不半运行，且不占活事件循环（组合中无其他运行项时进程会以 0 退出） |
| LOADING | 依赖就绪，`apply` 正在运行 |
| ACTIVE | 插件运行中 |
| FAILED | `apply` 或配置校验抛异常（对应论文模型的 Inactive(ξ)） |
| UNLOADING / DISPOSED | disposer 正在运行 / 一切已拆除 |

插件被卸载的四种诱因：**配置修改、热重载、显式 `fiber.dispose()`、所需服务消失**。

关键语义：

- `ctx.plugin()` 返回 fiber（Koishi v3 中叫 **Fork** 对象），`fiber.dispose()` 会等待该插件全部清理（含异步 disposer）完成，并**递归卸载其挂载的所有子插件**。
- 生命周期事件：`ready`（应用启动完成时触发；含异步初始化的操作放这里）、`dispose`（插件停用时手动清理非托管副作用）、`fork`（每次插件被调用时触发，可重用性的基础，见 4.4）。
- 论文的"执行惯性"：异步操作一旦发出则先让其完成，再依据最新目标状态决定是否卸载，避免停在执行到一半的中间态。
- 论文的 Provider 退出顺序（消费方清理代码本身可能需要依赖，如连接池归还连接）：

```
Provider 决定退出 → 停止接受新的依赖绑定 → 相关 Consumer 开始停用
→ Consumer 仍可使用原 committed binding 完成清理 → Consumer 全部退出
→ Provider 执行自己的恢复操作
```

实现中由 `refresh`、`committed`、`unload` 分别承担这几步。

### 4.2 Effect：注册即副作用，副作用皆可逆

**"通过 Cordis API 建立的注册属于 effect，会在所属插件卸载时撤销。"**

```ts
import type { Context } from '@deepseek-ai/cordis'

export function apply(ctx: Context) {
  // 非托管资源（定时器/连接/watcher）必须包进 ctx.effect 并返回 disposer
  ctx.effect(() => {
    const timer = setInterval(() => console.log('tick'), 200)
    return () => {                    // disposer：卸载期间运行
      clearInterval(timer)
    }
  })
}
```

- **内建 API 本身已是 effect**，开发者大多数场景完全不需要手写 `ctx.effect()`：`ctx.on()`（监听器随插件移除）、`ctx.plugin()`（子插件随父 dispose）、服务注册、`ctx.tools.register(...)` 等领域注册表（harness 将返回的 disposer 自动附着到调用插件）。
- disposer 按**注册顺序的逆序**启动（LIFO，对应数学推导中逆操作按相反顺序组合：执行 A→B→C，恢复 C⁻¹→B⁻¹→A⁻¹），但多个**异步** disposer 会**并发**运行；有顺序要求的清理步骤必须放进同一个 effect 的同一个 disposer 中串行 await。
- 代价说明（论文明确）：**运行时记录、组合、执行逆操作，但不验证逆操作本身是否正确**——disposer 写错是插件作者的责任。
- **系统边界**（论文）：可回滚要求"系统可独占修改 + 可恢复到此前状态"两条同时成立；边界不按介质（内存/文件/网络）划分而按控制权划分。资源获取类操作（open→close、malloc→free、fork→kill）可纳入可回滚 effect；**对外输出**（write 落盘、send 上网）一旦数据离开系统控制范围即无法保证恢复，需用**暂缓输出**（先在系统内缓冲，待状态确定不回滚再提交）或**补偿操作**（业务等价恢复，如删除已建文件、退款）。

### 4.3 Service 与依赖注入

**服务是一个插件提供、其他插件通过 `ctx` 消费的具名能力**（harness 中 `ctx.tools`、`ctx.llm`、`ctx.agents` 都是服务）。官方定位："类似 IoC 的实现（但并非通过 DI 实现）"——消费方只按 key 查找服务，**不导入具体实现**，因此配置可以换提供方而无需修改消费方；且依赖是**一对多**的（同一服务名可有多个候选实现，与 npm peerDependencies 的一对一硬依赖形成对比）。

**提供服务**（Service 子类本身就是插件）：

```ts
import { Service, type Context } from '@deepseek-ai/cordis'

// 编译期：TypeScript 声明合并，把 greeter 加入 Context 接口（零运行时代码）
declare module '@deepseek-ai/cordis' {
  interface Context {
    greeter: GreeterService
  }
}

export class GreeterService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'greeter')   // 运行期：以名称 'greeter' 注册，此后任何插件经 ctx.greeter 访问
  }
  greet(who: string) { return `Hello, ${who}!` }
}

export function apply(ctx: Context) {
  ctx.plugin(GreeterService)   // 注册属于 effect，提供方卸载时服务随之移除
}
```

Service 基类构造参数 `(ctx, name, immediate?)`；三个可选抽象方法 `start()`/`stop()`/`fork()` 分别在 `ready`/`dispose`/`fork` 事件触发；默认等服务就绪后才可见，`immediate: true` 则立即注册。Koishi v3 还有 `Context.service('name')` 语法糖（修改 Context 原型链使属性对所有上下文可见）。

**消费服务（inject 依赖注入）**：

```ts
export const inject = ['greeter']          // 必需依赖（数组）
// 或 export const inject = { required: ['database'], optional: ['assets'] }

export function apply(ctx: Context) {
  console.log(ctx.greeter.greet('world'))   // apply 内保证 ctx.greeter 已就绪
}
```

- Cordis 让插件保持 **PENDING** 直到 inject 列出的每个服务都存在；**`cordis.yml` 中的加载顺序无关紧要**——决定启动时序的是依赖关系而非文件顺序（列表中各项并发启动）。
- **inject 不是一次性启动检查，而是持续跟踪**：运行期所需服务消失（提供方被卸载/热替换）时，依赖插件自动回滚并卸载；服务恢复时自动重新加载。这使"替换服务"成为合法操作：卸载 `dsh-bash-local`、挂载另一个 `shell` 提供方，所有注入 `'shell'` 的插件自动重启并使用新实现。
- **部分功能依赖**：`ctx.inject(['console'], callback)` 语法糖等价于 `ctx.plugin({ using: ['console'], apply: callback })`——把依赖片段拆成子插件单独享受依赖驱动生命周期（注意回调参数里的子 `ctx` 与外层 `ctx` 是不同对象）。
- **可选依赖**：跳过 inject、在使用点用 `ctx.get('metrics')` 探测（未提供时为 undefined，插件照常运行）；Koishi v3 中也可声明 `inject.optional`（不阻塞加载、不因变化回滚）。
- **服务命名**：全应用共享一个扁平命名空间，建议自有服务加辨识度前缀；harness 已占用 `tools`、`llm` 等通用名。
- **服务隔离**：`cordis.yml` 中组（group）+ `isolate: { shell: true }` 使同一个服务 key 在不同组解析到不同 realm，两组各见自己的配置化实例互不影响；`intercept` 为依赖访问附加元数据约束。论文/腾讯云解读：`ctx[key]` 属性访问由 Proxy 接管，沿 fiber 链检查 committed view——key 在 committed view 中→允许；声明了但当前无 committed binding→INACTIVE_ACCESS；从未声明→UNDECLARED_ACCESS。

### 4.4 插件注册方式

**三种基本形态**（一个插件是三者之一，加载等价于"函数被调用"）：

```ts
// 1. 函数：接受 (ctx, config) 两个参数
export function apply(ctx: Context, config: Config) {}

// 2. 对象：带 apply 方法的对象
export const objectPlugin = {
  name: 'object-plugin',       // 可选，仅诊断用，不影响运行时行为
  inject: ['tools'],           // 可选
  apply(ctx: Context) {},
}

// 3. 类：Service 子类（类形态即"要公开服务"的场景）
export class MyService extends Service {
  static inject = ['tools']
  constructor(ctx: Context) { super(ctx, 'myService') }
}
```

官方演示了"框架 API 皆 effect"的等价性（Koishi 文档原文）：`ctx.middleware(callback)` ≈ `ctx.plugin(ctx => ctx.middleware(callback))` ≈ `ctx.plugin({ apply: ctx => ctx.middleware(callback) })` ≈ `ctx.plugin(class { constructor(ctx) { ctx.middleware(callback) } })`。

**两种挂载途径**：

- 代码内 `ctx.plugin(插件, config?)`：函数插件不需 apply 方法（直接被调用，名称仅用于诊断）；返回 fiber。
- 配置文件 loader：`cordis.yml` 列表，`name` 为模块指定符（相对路径或 npm 包名；Koishi 约定自动映射 `@koishijs/plugin-foo`、`koishi-plugin-foo`）。模块需满足：默认导出是插件，或导出整体是插件（默认导出优先级更高）。

**入口 main 函数约定**：即命名导出 `apply(ctx, config)`（可附元属性 `name`、`Config`、`usage`、`inject`、`reusable`、`filter`、`using`）。**没有名为 `definePlugin` 的核心 API**（用户若记得 "definePlugin"，应是把它与 dsh 的 `defineTool`（工具定义 DSL）记混了；Cordis 的注册面是 `ctx.plugin()` + 导出约定）。

**可重用性与 fork 事件**：默认插件不可重用——`ctx.plugin(callback)` 检测到同一插件已加载时直接返回原 fiber，不再执行（防止指令等资源重复注册）。声明 `reusable: true`（类形态用 `static reusable = true`）即可多实例挂载，其本质是 **`fork` 事件的语法糖**：

```ts
// 这两种写法等价（Koishi 文档原文）
ctx.plugin({ reusable: true, apply: (ctx) => { ctx.middleware(callback) } })
ctx.plugin((ctx) => { ctx.on('fork', (ctx) => { ctx.middleware(callback) }) })
```

`fork` 事件把插件分割为两个作用域：外侧代码只执行一次（不可重用部分，适合注册指令/服务），内侧代码每次调用执行（可重用部分）；配合 `ctx.on('fork')` 内再挂 `ctx.on('dispose')` 可维护共享状态（如调用计数）。嵌套规则：不可重用外层里嵌可重用内层不会重复执行（应改用 fork 事件）；可重用外层嵌不可重用内层则自然正确。Koishi 还提供 `ctx.registry.delete(plugin)` 移除可重用插件的全部实例。

### 4.5 事件总线（插件间通信）

服务面向**直接能力调用**；**事件让插件无需知道有哪些插件正在监听就能发通知**。harness 用事件处理工具结果、模型请求、审批决定等交互。命名采用扁平 `namespace/action` 约定（如 `agent/pre-step`、`tools/result`、`approval/request`、`stats/report`）。

**类型化事件**：通过声明合并注册事件名与监听器签名，`ctx.emit`/`ctx.on` 全类型推断：

```ts
declare module '@deepseek-ai/cordis' {
  interface Events {
    'stats/report'(name: string, count: number): void
  }
}
// ctx.emit('stats/report', name, count) 与 ctx.on('stats/report', (name, count) => {}) 均被推断
```

**五种分发模式**——每个事件具有且仅具有一种模式，只能用对应方法分发（新 harness 事件以 `@mode` 文档标签记录模式，生成目录对"声明 vs 调用点"做交叉校验）：

| 模式 | await？ | 分发顺序 | 返回值 |
|---|---|---|---|
| `emit` | 否 | 按注册顺序 | 无（观察通知） |
| `waterfall` | 否 | 按注册顺序 | 有（链式透传） |
| `parallel` | 是 | 全部并行 | 无（全量等待） |
| `serial` | 是 | 按注册顺序串行 | 有（第一个非 falsy 返回值终止后续） |
| `bail` | 否 | 按注册顺序，直到某监听器返回 bail 值 | 有（短路） |

**waterfall 是环绕中间件**：监听器签名 `(...args, next)`；调用 `next()` 执行下游并把下游返回值带回本层（可包装后继续外传）；不调用 `next()` 直接返回即**短路**。

```ts
ctx.on('demo/transform', async (input, next) => {
  const downstream = await next()
  return downstream.toUpperCase()          // 观察者：包装下游结果
})
ctx.on('demo/transform', async (input, next) => {
  if (input.includes('blocked')) return '** blocked **'  // 决策者：有意短路
  return next()
})
```

由此产生一条常设纪律：**只负责观察/标注的 waterfall 监听器必须调用 `next()`**——忘了调用会悄无声息吞掉所有下游默认行为。`prepend: true` 仅当监听器必须先于普通注册运行时使用。

事件监听器同样是 effect，插件卸载自动移除。harness 里另有一套持久化"会话事件"（`turn/*`、`step/*`、`tool/call`、`compaction/*`），须监听 `session/event` 并检查 `event.type`，与 Cordis 事件同名但不同体系。

**实践规则**（primer 原文）："将行为封装为插件：工具流水线事件属于 `ctx.tools`，模型流式输出属于 `ctx.llm`，实时 agent 协调属于 `ctx.agents`。**拦截和策略优先使用事件；直接能力调用优先使用服务方法。**"

### 4.6 配置系统

**Schema 双重身份**：导出的 `Config` 既是 TypeScript 接口（消费方获得类型），又是同名运行时校验器（Cordis 在运行 `apply` 前验证）。dsh 用 **Schemastery** 定义 schema（Shigma 的配套库）；Cordis 本身接受任意 Standard Schema（standardschema.dev）验证器——导出普通对象无效。

```ts
import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'

export interface Config {
  greeting: string
  targets: string[]
}

export const Config: Schema<Config> = Schema.object({
  greeting: Schema.string().default('Hello'),
  targets:  Schema.array(String).default(['world']),
})

export function apply(ctx: Context, config: Config) {
  // apply 始终收到完整且经过验证的配置
}
```

- 配置错误 → 明确报错（`ValidationError: invalid config: - $.targets expected array but got ...`），fiber 进 FAILED；**插件绝不会在配置不完整时启动**。
- Schema 能力：`object/array/dict/union/intersect/lazy/transform` 复合类型 + 链式 `.required()/.default()/.min()/.max()/.role('slider')/.description()`，可构造上下关联的配置项；同时驱动 Web 控制台表单渲染（Koishi 的插件市场即用它做在线配置 UI）。
- 类插件的配置用 `namespace` 模式组织：`namespace Bar { export interface Config{}; export const Config: Schema<Config> = Schema.object({...}) }`。

**cordis.yml（Loader 配置树）**：Cordis 配置项除 `name`/`config` 外的元数据：

```yaml
- id: greeter            # 稳定标识：loader 用它 diff「修改现有条目」vs「删除再新增」
  name: './greeter.ts'
- id: consumer
  name: './consumer.ts'
  disabled: true         # 卸载插件但保留条目；改回即恢复加载（连同所有因它 PENDING 的插件）
  config:
    targets: ['alpha', 'beta']
    greeting: !!js process.env.DEMO_GREETING ?? 'Hello'   # 加载时求值表达式（仅 config 与 disabled 内有效）
```

- **无 `id` 的条目每次读取都会获得新生成的 id**——配置文件任何编辑都会使其被视作"先删后加"而整体重挂载；显式 `id` 是 HMR 增量更新的关键。
- `disabled: !!js ...` 在每次挂载决策时按 loader 上下文求值（dsh 扩展），可按平台/环境门控条目；环境级选择应使用 overlay（profile patch）。
- 组可嵌套子条目列表作为整体加载/卸载；`isolate` 提供组级服务隔离（见 4.3）。
- **Volatile 字段**（v4 特性）：`.volatile()` 声明的字段变化**不重挂载插件**，而是把新值提交到运行中的稳定引用（`.get()` 读取），并向所属实例发一次 `loader/volatile-update` 通知；普通字段有效值变化仍走"回滚→重载"全流程。适合插件每次操作都要读的选项（如日志级别），避免热更新抖动。

**配置树增量协调**（Loader，腾讯云对论文的解读）：Entry = { id, url, isolate, intercept, config, disabled }。变化分派：`id/url` 变→重建条目；`isolate` 变→重新分配 realm；`intercept` 变→原地更新；`config` 变→**交给组件自己决定是否 reload**；`disabled`→卸载/恢复；group 的子列表按 `id` 做 keyed diff 递归协调。**依赖关系只影响组件何时激活，不要求 Loader 按拓扑序加载**——模块并发获取求值，依赖不满足的 fiber 留在 PENDING 等 Provider。

### 4.7 HMR（后端模块热替换）

Cordis 自称实现了"目前仅有的后端 CJS/ESM HMR 技术"：原生 Node.js 运行、不依赖打包工具、支持 CommonJS 与 ES Module、支持 `--require` 与 `--loader`。

基于组件生命周期的三阶段（论文）：

1. **Module Classification**：从变更模块沿 import 图传播，划分 accepted / declined（不可热替换的外部模块、依赖环中无法确定的模块→declined）；
2. **Stale-entry Detection**：只找出依赖路径触及 accepted 模块的 Entry，其余不动；
3. **Transactional Reload（事务性重载）**：备份并清除模块缓存 → 对每个 stale Entry 执行 dispose 旧 fiber → 重新 import → `ctx.use(...)` → 创建新 fiber；任一模块重导入失败则**回滚**：恢复旧模块缓存、清理本轮新 fiber、用备份旧组件重建——系统不会停在新旧代码各半的中间态。

卸载释放 effect（4.2）、加载遵循依赖（4.3），所以 HMR = "先卸载再加载"即可替换运行中的插件。dsh 的 `@deepseek-ai/dsh-hmr` 插件负责文件监视 + 去抖（自身 inject `timer` 与 logger 服务——缺 timer 会永远 PENDING，是文档专门给出的"静默失败"示例）。编辑 `cordis.yml` 本身也触发按 `id` 的增量更新。

### 4.8 Registry 与可诊断性

`ctx.registry` 可枚举所有已加载插件：`ctx.registry.values()` → runtime → `runtime.fibers` → `fiber.state`。官方教程给出的"为什么我的插件没输出"标准答案就是遍历 registry 找 PENDING 状态的 fiber。`ctx.fiber` 是当前 fiber；`ctx.effect()` 实际委托给它；`getEffects()` 带标签诊断副作用清单。

---

## 5. 与常见「manifest + 能力门控」式插件系统的本质区别

以 VSCode 扩展为对照（论文自己的靶子）：截至 2026-06-09，VSCode Marketplace 前 100 扩展中 **87 个包含可执行代码，激活后无法在运行时单独卸载，禁用/删除必须重启扩展宿主**。量子位总结："「插」件，实际上插上去就拔不下来。" 逐条对比：

| 维度 | manifest + 能力门控（VSCode/OBS/浏览器扩展式） | Cordis |
|---|---|---|
| **卸载语义** | 名义卸载，实质是"禁用 + 等重启"；宿主进程生命周期绑定所有插件 | 卸载是真正有运行时语义的操作：LIFO 撤销链精确恢复到插件加入前的状态，其余插件纹丝不动（Koishi 控制台禁用插件=原地撤回影响） |
| **依赖模型** | 静态清单声明（activationEvents / contributes），一次性检查 | coeffect 是**持续性运行时协议**：Provider 出现/消失/替换时反应式重算依赖方目标状态并驱动其激活/停用 |
| **组合观** | 宿主有**固定特权内核**，插件是内核周围的点状增强 | 元框架：**宿主本身由插件组装**（连 agent loop、LLM 适配器、Web UI 都是插件）；扩展方式是"把插件挂到其他插件旁边"，不存在要打补丁的内核 |
| **绑定方式** | 插件 import 宿主 API（版本耦合，`engines` 字段锁宿主） | 消费方按 key 查服务（`inject: ['tools']`），不 import 实现；提供方可整体替换，消费方零修改 |
| **能力/权限** | 能力门控是**准入控制**：装之前问一次（permissions / activationEvents），运行期不再变 | "能力"以服务+事件形式持续存在；`isolate`/`intercept`/committed view 提供运行期访问约束。注：Cordis 不是安全沙箱，不可信代码仍需进程级隔离 |
| **失败行为** | 配置错误常表现为崩溃、静默失效或宿主拒绝启动 | 配置校验失败→FAILED 带精确错误；依赖缺失→PENDING 静默等待（不崩溃、不半运行）；apply 抛错→明确终止；模块名拼写错→仅 logger 报错不崩进程 |
| **更新模型** | 扩展更新→重启；宿主更新→全量重启 | 插件级 HMR（事务性）；企业级特性：单文件快速部署（bootstrap）、**零停机滚动更新（0dt）**、细粒度访问权限——这些是 manifest 系统做不到的 |
| **理论地位** | 工程约定，无形式化 | 92 页 cs.PL 论文：effect/coeffect 运行时化 + 组件演算 + 元理论（单组件性质推至交错组件系统） |

本质一句话：manifest 系统回答"**你是什么、允许你做什么**"（静态身份与准入）；Cordis 回答"**你改变了什么、你依赖的世界变化了怎么办**"（动态修改与依赖的反应式管理）。前者把插件当资源，后者把插件当**可逆计算**。

---

## 6. TypeScript 实现细节与关键代码模式（值得借鉴的部分）

1. **声明合并作为类型-运行时桥梁**（最有特色的模式）：`declare module '@deepseek-ai/cordis' { interface Context {...}; interface Events {...} }` 只加类型不生成任何运行时接线；运行时注册由 `super(ctx, 'name')` / 事件 emit 完成。跨包扩展用 `import type {} from '@deepseek-ai/dsh-tools'` 引入对方的声明合并，使 `'tools/result'` 事件及 payload 在本文件全类型化。这是"接口开放、实现封闭"的 TS 惯用法在插件系统的极致应用。
2. **Context 组合**：原型链注入（`Context.service('name')` 使属性对所有上下文可见）+ `ctx.extend(meta)` 派生子上下文（own properties shadow 继承属性，父上下文不被修改——不可变派生）+ Proxy 拦截 `ctx[key]` 做 committed-view 访问控制 + 服务方法内 `this[Context.current]` 拿到**调用方上下文**，把调用者注册的副作用附着到调用者自己的 fiber（Koishi Console `addEntry` 的热重载安全写法）。
3. **effect 同态模式**：框架每个领域 API 都是"某普通函数的 effect 版本"（`ctx.on` = effect 化的 addListener；`ctx.plugin` = effect 化的函数调用）。框架作者可**渐进改造**存量 API，开发者零成本获得资源安全。
4. **柯里化 Schema**：`Schema.object({...})` 的产物既是类型描述也是可调用验证器；调用 `Config(raw)` 返回稳定引用（volatile 模式），`simplify()` 解包。一次声明同时服务：TS 类型推导、运行时校验、默认值填充、Web 表单渲染、HMR diff 忽略 volatile 字段。
5. **namespace 组织类插件元数据**：`class Bar {...} namespace Bar { export interface Config; export const Config: Schema<Config> }`——同名双身份导出。
6. **fiber/registry 状态机诊断**：`FiberState` 枚举 + registry 遍历 + effect 标签（`ctx.effect(fn, label?)` 供 `getEffects()` 展示），把"依赖驱动的静默状态"变成可观测的。
7. **领域 DSL 惯例**（dsh 层，供借鉴）：`defineTool({...})` 把参数规约转 JSON Schema、推导 args 类型、执行前校验，`output.render` 声明原生渲染器，`brandString<ToolCallId>('demo-1')` 品牌类型做关联 ID——"定义即元数据+校验+渲染"的三合一模式。

---

## 7. 优势与代价

### 7.1 优势

- **真正的可逆性 / 资源安全**（时间维度）：热重载、运行时替换、禁用、升级都不需要重启宿主；Koishi 4 年 4000+ 社区插件验证（论文的 "existence-and-adoption result"：跨作者独立开发的插件仅靠 reactive coeffect 协调即可共存）。
- **零手写的依赖编排**（空间维度）：加载顺序由 inject 自动推导；缺依赖静默等待而非空引用崩溃；Provider 替换自动传播到所有依赖方。
- **无内核架构**：产品每一部分可替换可裁剪（dsh 用 profile/bundle/patch 层叠组装出 web/headless/sdk/acp 多形态产品线）。
- **全链路类型安全** + 配置即表单 + 明确报错。
- **开发体验**：后端 HMR 保存即生效（缓存与连接保留）；企业特性（单文件部署、零停机滚动更新、细粒度权限）。
- **有形式化理论背书**（这在插件系统领域几乎绝无仅有），概念正交、可推理。

### 7.2 代价与局限（论文/文档自认 + 社区观察）

- **学习曲线陡**：Context/Fiber/Service/inject/coeffect/fork/reusable/volatile/committed-view 概念体系大；"两个 ctx 含义不同""不可重用插件嵌套可重用插件不生效"等嵌套规则容易踩坑；论文级理论对普通插件作者是门槛（腾讯云有文章以"过度抽象"为题讨论争议）。
- **调试复杂度**：依赖驱动加载的**静默 PENDING** 是头号疑难（官方教程专门教怎么诊断）；disposer 逆序启动但异步并发执行，清理顺序需显式设计；交错 effect 的撤销行为难以静态推理；`import type` 静默消失导致的"配置项无效果"问题需要检查拼写。
- **运行时不保证逆操作正确**：写错 disposer 仍泄漏；"资源安全（非强制）"对比 Rust 所有权（强制）。
- **系统边界外无能为力**：已发出的网络请求/已写出的数据不可自动回滚，需要暂缓输出/补偿逻辑的业务纪律。
- **验证面窄**（论文自认）：仅 Koishi 单一生态、TypeScript/Node 单语言、无与其他架构的受控对照实验；自进化 Agent Harness 是"后续验证方向"（dsh 正是这个方向的第一次大押注）。
- **vendor 引入的工程代价**：仓库体积膨胀、上游更新需手动同步（dsh 自建 `vendor/README.md` 同步流程缓解）。
- **抽象税**：一次性脚本/小型应用用不上这套机制；且 dsh 处于 developer preview，官方预告将有破坏性变更。

### 7.3 适用性分析

**适合：**

1. **运行时动态性是一等需求的应用**：插件市场、多形态产品线（同一内核组出 web/cli/sdk/desktop）、不停机运维、A/B 能力切换；
2. **多作者开放生态**：Koishi（4000+ 插件）、dsh（发布两天插件从 911 冲到 6000+，社区报道）证明它撑得起第三方作者的大规模独立协作；
3. **Agent Harness / 自进化系统**（最前沿的动机）：Agent 自己生成工具→装进运行时→发现问题→替换，每次变更不丢上下文不重启进程；LLM 适配器、沙箱、审批策略全部热替换——这正是 DeepSeek 选它做 harness 的原因，也是论文点名的后续验证方向；
4. 中大型 TypeScript/Node 应用，且团队愿意支付概念学习成本。

**不适合：**

1. 一次性脚本、简单 CLI、小型工具——抽象税远超收益；
2. **不可信插件的强隔离需求**：realm/isolate 是逻辑依赖隔离不是安全边界，恶意插件仍需进程级沙箱兜底；
3. 非 TS/Node 技术栈：需按论文重新实现核心（论文提供了完整形式化，但现成实现只有 TS）；
4. 插件形态静态、装一次跑到底的场景：manifest+能力门控（VSCode 式）更简单直接，Cordis 的可逆机制在此是过度设计。

---

## 8. 附：DeepSeek Harness 中 Cordis 的实际用法速览（调研所得，供参照）

- 运行中的 dsh = 一棵插件树，由启动时按序叠加的**层**组合：profile（具名组装，存于 Harness home：`web`/`headless`/`sdk`/`sdk-minimal`/`acp` 模板）→ 组合包 bundle（Cordis 配置项+挂载代码的分发格式）→ 各层 `cordis.patch.yml` → home patch → `--patch` overlay；patch 按 `id` 定位条目整体替换 config 或插入新条目；`dsh --profile web --dump-config` 可打印本机实际启动的配置树。
- dsh-base 是 web/headless/sdk/acp 共享第一层：模型适配器、工具、持久化、沙箱与审批策略、设置、凭据、遥测——**全部是插件**；agent loop 本身也是插件（`ctx.agents`）。
- 插件能力三层拆分（实践章节）：core 服务主干 / 可替换能力 seam（capabilities）/ 组合点；服务目录由仓库脚本从源码生成（`cordis-surface` 区块），不维护静态清单。
- 一个最小 harness 工具插件完整示范了全部机制组合：`inject: ['tools']` 等待工具注册表 → `ctx.tools.register(defineTool({...}))` 注册即 effect → 独立 `tool-logger` 插件经 `ctx.on('tools/result', ...)` 观察——两个插件互不知道对方存在，由服务注册表与事件连接。

---

## 9. 调研覆盖与未尽事项

- 已完整获取：dsh 官方 Cordis 教程 7 章 + primer + 6 个 API 参考页 + 架构页 + 框架三页；cordis.moe 理念（可逆插件系统全文数学推导）与基础四篇；arXiv 论文摘要与作者信息；GitHub 仓库元数据与 README 正文；掘金/腾讯云/量子位三篇中文深度解读。
- 未能直接获取（403/网络拦截）：知乎两篇专栏正文、百度百科词条正文、`vendor/README.md` 原文（内容经掘金文章与文档站引述交叉印证）、cordisjs GitHub 组织页与 cordis npm registry 元数据（组织归属经 cordis.moe 官方链接与多方报道印证）。cordis.moe 的"特性""理念引言""从框架到元框架"三页目前是空骨架（VitePress 未填充），特性内容经其余页面与检索摘要补足。
- 论文全文（92 页 PDF）未逐页精读，论文细节以官方摘要 + 腾讯云逐章解读 + 官方文档交叉验证，三方一致。
