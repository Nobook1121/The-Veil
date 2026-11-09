# 房间模式扩展规划：无 AI KP 房间 / 无剧本 AI 房间

> 状态：规划稿，待拍板。本文只描述形态、改动面与顺序，不含实现细节与代码。

## 1. 目标

在现有「剧本 + AI KP」房间之外，新增两类玩法：

- **A. 无 AI KP 房间（manual）**：AI 完全不参与叙事，玩家自发聊天、掷骰、执行规则；房间内一切自动化叙事能力关闭。
- **B. 无剧本 AI 房间（freeform）**：不绑定剧本，AI KP 以沙盒方式自由发挥；剧本相关工具、场景指针、触发器与剧本级缓存全部降级。

现有剧本房（scripted）行为保持不变；旧房间缺省即视为 scripted。

## 2. 模式如何表达

推荐**两个正交能力位 + 三个 UI 预设**，而不是单一枚举：

| 字段（`info.json`） | 取值 | 含义 |
| --- | --- | --- |
| `room_mode` | `scripted` / `freeform` / `manual` | 展示与预设，驱动 UI 与默认能力 |
| `ai_kp_enabled` | bool | 是否允许调用 `/api/chat` 的 KP 链路 |
| `scenario_binding` | `required` / `optional` / `none` | 是否必须绑定剧本 |

预设映射：

- `scripted`：AI 开、剧本必填（等于现状）。
- `freeform`：AI 开、剧本禁止绑定。
- `manual`：AI 关、剧本可选（可选仅作玩家自查资料，不驱动任何 AI 能力）。

这样表达的好处：未来若需要「有剧本但无 AI（真人 KP 带模组）」或「无剧本无 AI（纯聊天+骰子）」，不需要再改数据模型，只加预设即可。旧房间没有这些字段时按 `scripted / true / required` 推断，保证向后兼容。

互斥与降级规则：

- `ai_kp_enabled=false` 时，`room_mode` 不得为 `freeform`（无 AI 的 freeform 无意义，自动落为 `manual`）。
- `room_mode=freeform` 时，任何写入 `scenario_id` 的接口必须拒绝（含切换剧本、迁移、Agent 工具）。
- 模式创建后默认不可变；确需变更时只允许房主/管理员走显式「房间模式迁移」接口（带 dry-run 与日志），不做静默兼容。
- 旧房间不做数据改写，读取时补默认值；归档快照保留当时的模式字段。

## 3. 现状锚点（已确认）

- 创建房间强制 `scenario_id`，并预置首个场景指针：`routes/rooms.py` 的 `create_room()`（[rooms.py](../../backend/trpg_server/routes/rooms.py#L1017-L1097)）。
- 加入房间的角色卡在后端是可选参数，前端强制选择：`join_room_by_code()`（[rooms.py](../../backend/trpg_server/routes/rooms.py#L1115-L1177)）、[rooms.ts](../../frontend/src/app/rooms.ts#L278-L322)。
- 房间摘要字段固定含 `scenario_id/title/version`：`_room_summary()`（[rooms.py](../../backend/trpg_server/routes/rooms.py#L947-L981)）。
- 剧本未开启时的 KP 行为由提示词硬门控：`chat()`（[chat.py](../../backend/trpg_server/routes/chat.py#L583-L593)）。
- AI 开场依赖叙事图谱与剧本：`agents/narrative/opening.py`、`agents/narrative/runtime.py`（无剧本会直接抛错，[runtime.py](../../backend/trpg_server/agents/narrative/runtime.py#L126-L133)）。
- 沉浸系统只在 AI 回合后触发：`chat.py` 的 `on_narrative_turn` 调用（[chat.py](../../backend/trpg_server/routes/chat.py#L1376-L1386)）、[service.py](../../backend/trpg_server/immersive/service.py#L409-L430)。
- 世界意志权限：`world_override_allowed()`（[rooms.py](../../backend/trpg_server/routes/rooms.py#L290-L304)）。
- 房间归档会记录剧本信息并写角色卡「参与过的模组」：`_build_room_archive()`（[rooms.py](../../backend/trpg_server/routes/rooms.py#L839-L868)）。
- 房间控制台已有 `narrative / immersive / voice / dice` 命令组：[room_console_commands.py](../../backend/trpg_server/room_console_commands.py#L34-L63)。

## 4. 按模块的改动清单

### 4.1 数据模型与房间生命周期

- `info.json`：新增 `room_mode / ai_kp_enabled / scenario_binding`；创建时写入，读取时补默认值（旧房间推断为 scripted）。
- `create_room()`：按模式分支校验——scripted 仍要求剧本；freeform 拒绝剧本；manual 允许剧本为空；共享逻辑只对 scripted 初始化场景指针与 `scenario_version`。
- `join_room_by_code()`：manual 模式允许无角色卡加入（是否放开见未决问题 2）；freeform/scripted 保持现有行为。
- `_room_summary()`：输出模式字段与展示标签，供列表卡片、详情页与 Agent 快照使用。
- 归档：无剧本房间跳过剧本字段与「参与过的模组」写入，避免生成空模组记录。
- 剧本切换/迁移接口：freeform 直接拒绝；manual 仅允许「绑定/解绑参考资料」，不得触发任何 AI 流程。

### 4.2 叙事与聊天管线

- `/api/chat`：manual 房间在入口统一拒绝 KP 请求（返回明确错误码，前端不展示 AI 输入），避免靠提示词软约束。
- 提示词：为 freeform 增加独立 KP profile——去掉「不得创造剧本未写出的内容」「以剧本为唯一事实来源」等约束，改为沙盒一致性、玩家设定优先、可自建 NPC/地点但需保持连贯。
- 工具集：freeform/manual 裁剪场景类工具（`room.get_scenario_context`、`room.get_scenario_module`、`room.activate_scenario_scene`、`trigger.reveal_scenario_trigger`）；规则书检索与骰子检定保留。
- 开场：freeform 不走剧本开场 plan，改为「自由开场」轻量提示或由首条玩家消息触发；manual 无开场。
- 世界意志：manual 隐藏入口；freeform 可用，且不依赖剧本（见未决问题 3 的决议）。
- 玩家消息通道：明确继续走 Socket `send_message`，与 AI 链路解耦（[socket_events.py](../../backend/trpg_server/socket_events.py#L202-L213)）。

### 4.3 沉浸、语音与叙事图谱

- `on_narrative_turn`：manual 模式直接短路（零调用、零成本）；freeform 正常触发，但 `scenario_id` 传空。
- 配图：freeform 关闭剧本级缓存与「剧本作者开关/剧本偏好」，只保留房间级缓存；无剧本不生成「与剧本一致」类提示。
- 配乐：freeform 沿用；manual 不触发自动配乐，但保留房间 BGM 手动播放与音乐库。
- 语音：manual 无 AI 回复，自动无触发；freeform 保持。
- 叙事图谱/世界树：freeform 与 manual 均关闭自动构建与 keyframe，房间内存档降级为纯消息快照。
- 用量：manual 房间的 AI 用量面板隐藏或显示「本房间不产生 AI 用量」。

### 4.4 前端入口与 UI

- 创建房间弹窗：加入「房间模式」三选一；选 freeform 时隐藏剧本选择器，选 manual 时把剧本选择器标为可选并提示「AI 不参与叙事」。
- 房间卡片：展示模式徽章；无剧本时不再显示「未知」，改显示「自由叙事」/「玩家自跑」。
- 房间详情：manual 隐藏「开启剧本」、AI 输入框、世界意志、AI 用量、沉浸自动开关；freeform 隐藏「开启剧本」与剧本切换入口，保留沉浸、语音与世界意志开关；详情页显示模式徽章与能力说明。
- 加入/进入弹窗：manual 允许「不绑定角色卡进入」；无卡时提示仅能使用基础掷骰。
- 设置页：站点级新增「允许的房间模式」与默认模式（是否纳入配置见未决问题 6）。

### 4.5 控制台、权限与审计

- 服务器控制台：`room list` 展示模式；新增或扩展房间模式迁移命令，默认 dry-run + `--yes`。
- 房间控制台：`narrative / immersive / voice` 子命令在 manual 模式返回「当前模式不支持」，而不是静默成功。
- `narrative opening <room_id>`：manual/freeform 明确拒绝并说明原因。
- 日志与审计：创建、加入、模式迁移、AI 拒绝调用都写房间日志，字段补 `模式`；AI 拒绝需有明确 reason，便于排查「为什么 AI 不回话」。
- 权限矩阵：沿用 `rooms.create` / `rooms.manage_members`，不新增节点；模式可用范围用站点配置控制，避免权限系统膨胀。

## 5. 受影响的既有约束：放松 / 保留

| 约束点 | 现状 | 处理 |
| --- | --- | --- |
| 创建房间必须选剧本 | 后端 400 + 前端校验 | **放松**：仅 scripted 保留；freeform 禁止；manual 可选 |
| 进入房间必须选角色卡 | 前端强校验，后端可选 | **放松**：manual 允许无卡；是否全模式放开由用户拍板 |
| 房间必须有 `scenario_id` | 摘要/归档/工具默认读取 | **放松**：允许为空，所有读取方需空值兜底 |
| 剧本未开启时 KP 不得推进剧本 | 提示词硬门控 | **保留**（scripted）；freeform 用另一套沙盒约束替换 |
| 剧本级缓存与剧本作者开关 | 配图依赖 | **保留**（scripted）；freeform 明确不读取 |
| 世界意志需房主/房管或白名单 | 权限校验 | **保留**；manual 直接隐藏与后端拒绝 |
| 房间 AI 用量仅房主/房管可见 | 现有行为 | **保留**；manual 无数据时展示空态 |
| 房间控制台仅限本房间 | 现有行为 | **保留**，另加模式可用性校验 |
| 归档含控制台日志与消息 | 现有行为 | **保留**；仅剧本字段与模组记录按模式降级 |
| 邀请码/邀请链接/隐身旁观 | 现有行为 | **保留**，不受模式影响 |
| 用户建房数量上限 | 现有行为 | **保留**，不按模式放宽 |

## 6. 能力降级矩阵

| 能力 | scripted + AI | freeform + AI | manual |
| --- | --- | --- | --- |
| AI KP 叙事 | 开 | 开（沙盒提示词） | 关 |
| 剧本绑定 | 必填 | 禁止 | 可选（仅资料） |
| 剧本开场 / 场景指针 | 有 | 无（自由开场） | 无 |
| 场景/触发器工具 | 有 | 移除 | 移除 |
| 规则书检索 | 有 | 有 | 不触发 |
| 世界意志 | 有 | 有（不依赖剧本） | 隐藏 |
| 实时配乐 | 有 | 有 | 关（保留手动 BGM） |
| 实时配图 | 有 | 有（无剧本级缓存） | 关 |
| LLM 配乐导演 | 有 | 有 | 关 |
| 语音配音 | 有 | 有 | 关 |
| 叙事图谱 / 世界树 | 有 | 关 | 关 |
| 自动存档 | 消息快照 | 消息快照 | 消息快照（保留） |
| AI 用量 | 有 | 有 | 无 |

## 7. 风险与未决问题（需拍板）

1. **模式表达方式**：单一枚举三值，还是本文推荐的两个正交开关 + 三预设？如果未来明确要「有剧本但无 AI」，正交更稳。
2. **manual 的角色卡约束**：是否允许完全无卡进入？无卡时是否只允许基础掷骰、禁用技能检定与角色卡相关工具？
3. **freeform 的世界意志语义**：已决议为「直接可用，不依赖剧本」。实现上把声明落盘为既定事实、后续回合持续注入，并在执行回合移除战斗/检定工具；「导演指令」留到第二阶段。
4. **模式是否可变更**：创建后锁定（推荐），还是允许房主迁移？迁移时已有消息、存档、沉浸缓存如何处理。
5. **无剧本房间的归档记录**：角色卡「参与过的模组」写什么（房间名 / 自定义标题 / 不写）。
6. **站点级管控**：是否新增 `[rooms] allowed_modes` 与默认模式？新增即需进 `config/schema.py`、模板与配置同步测试。
7. **freeform 的规则边界**：COC 规则是否仍然强制（检定、理智、战斗），还是允许 KP 更宽松地自由裁定。
8. **免费额度与限流**：freeform 会持续消耗 AI 成本，是否需要默认预算/频率上限，或复用房间级沉浸预算。

## 8. 建议实施顺序

| 阶段 | 交付物 | 验收标准 |
| --- | --- | --- |
| 0. 决策与契约 | 本文定稿 + 能力矩阵签字 | 8 个未决问题有明确结论，字段命名冻结 |
| 1. 数据模型与后端校验 | `info.json` 模式字段、创建/加入/摘要/归档分支、旧房间默认推断 | 旧房间行为不变；三种模式均可创建并正确回显 |
| 2. 叙事管线分流 | `/api/chat` 模式门控、沙盒提示词、工具裁剪、开场降级、世界意志门控 | manual 无法触发 AI；freeform 无剧本可正常多轮叙事且不调用场景工具 |
| 3. 沉浸/语音/图谱降级 | manual 短路、freeform 去剧本缓存、叙事图谱关闭、日志字段 | AI 用量与沉浸日志符合矩阵；manual 零 AI 调用 |
| 4. 前端入口与 UI | 创建弹窗、卡片徽章、详情页控件隐藏、加入弹窗 | 三种模式从创建到进入全流程可用，无遗留「未知剧本」文案 |
| 5. 控制台/配置/文档/测试 | 房间控制台提示、服务器命令、站点配置（若采纳）、wiki 与 changelog | `verify.ps1` 通过；新增测试覆盖三种模式的关键分支 |

每阶段独立可交付，阶段 1 完成后即可先让后端支持模式字段，前端再跟上；不建议把前端与管线改动放在同一步。

## 9. 需要同步的文档与测试

- 文档：`docs/api.md`（房间字段与错误码）、`docs/console-commands.md`、`docs/the-veil.wiki/`（房间模式章节）、`release/changelog/`。
- 测试：创建房间三模式校验、加入房间无卡分支、manual 拒绝 AI、freeform 提示词不含剧本约束、沉浸 manual 短路、配置模板同步（若新增配置项）、前端结构测试。
