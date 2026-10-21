# 现代化骰子动画与玩家投掷 · 功能开发文档

- 文档状态：已评审（v10，新增「物理骰面结果生成器」与管理员开关；v9 补充「骰子静止后不再操作，只读取朝上的面」；v8 补充任意面数兜底、骰子数量上限、历史 `roller` 兼容、3D 实例上限与 WebGL context 治理、落定不回抬；文中的「居中遮罩浮层 / 行内结果卡 / 居中内嵌」以 v5 / v6 修正为准）
- v10 修正（2026-10-21 23:56）：
  - 删除「把服务端结果贴到静止骰面」的做法：骰子落定后不再写入、替换或改写任何骰面贴图，结果直接来自物理静止后朝上的面。
  - 新增物理结果生成器：服务端只生成 32 位 `throw_seed`，不再单独生成点数；所有客户端用同一 seed 推演一致轨迹（`mulberry32` + 固定 1/60 步进 + 步数上限 480 + 静止帧判定），由投掷者读取朝上的原始骰面并上报。
  - 服务端负责结算：百分骰十位/个位组合（重复十位去重、不足按 0–9 补齐）、奖励骰取小、惩罚骰取大、总和、成功等级；SAN 变化骰仍用服务端 RNG。
  - 新增管理员开关 `[ai] dice_physics_results`（默认开启，管理员设置「启用现代化骰子点数生成器」）；关闭时保留原随机数生成器，纯文本、暗投、离线、强制投掷与超时始终回退旧生成器并标记 `fallback`。
  - 新增 `POST .../begin`、`POST .../faces` 与 Socket `player_dice_rolling` / `player_dice_rerolled`；AI 发起的检定在物理模式下自动开始动画。
- v9 修正（2026-10-21 23:13）：
  - 3D 动画删除物理静止后的 settle 阶段（原先对每个 mesh 做 220ms `slerp(target)`），骰子落定即停在桌面原位置，不再抬起、旋转或缩放。
  - 静止后**只读取朝上的面**：按面法线经 mesh 四元数旋转后的 y 分量最大者确定顶面，把本次结果贴到该面材质，并还原上一张朝上面的贴图（`syncUpFaceLabels()`）；滚动过程中骰面照常随翻滚变化。
  - 简易档位同步取消 `.settled` 位移/缩放关键帧与 280ms 落定等待，抖动停止后原地显示最终点数。
- v8 修正（2026-10-21 20:50）：
  - 不常见面数（如 `d5`）在 3D 档位必须自动回落到简易档位，不得出现骰桌空白；降级时必须先销毁旧渲染器再创建后备渲染器（3D 实例销毁只移除自己的 canvas，不得清空整个骰桌容器）。
  - 单次投掷最多在骰桌上渲染 60 枚骰子（`DICE_SIMPLE_RENDER_LIMIT`）；超出时不渲染骰子，在骰桌区域显示「骰子数量过多……」提示，结果数字仍显示在下方，并上报 `dice_count_exceeded`。3D 档位本身最多 20 枚，21–60 枚自动改用简易档位完整展示。
  - 历史静态还原不得因骰型不支持而跳过渲染；`roller` 兼容字符串与 `{user_id, username}` 对象，禁止把对象直接拼成 `[object Object]`。
  - 每个客户端最多同时保留 4 个 3D 实例（`MAX_LIVE_3D`），超出时把最早且不在投掷中的实例截图冻结为静态图并销毁；换房释放全部实例；`dispose()` 主动 `forceContextLoss()`，WebGL 能力探测结果缓存且立即释放探测用 context。
  - 3D 与简易档位落定后骰子都停在桌面上的位置，不做抬起、放大或旋转展示（简易档位移除选中骰位移/缩放与落定上浮关键帧）。
- v7 修正（2026-10-21 20:12）：
  - 骰桌标题、状态与结果文字必须跟随主题，并保证与桌面底色对比度可读：主题变量从 `body` 域读取（避免 `:root` 别名已按默认主题求值），对比度不足时退回黑 / 白；新增 `--theme-dice-table` / `--theme-dice-accent` / `--theme-dice-table-text` 主题钩子与运行时 `--dice-ui-text-color` / `--dice-result-color`。
  - `/dice <表达式> --table` 本地投掷必须持久化为 `type=dice` 的聊天消息（含完整 `metadata.dice_roll`），刷新 / 重连后静态还原同一张骰桌；此前只显示不写库，导致刷新后历史骰子记录消失。
  - 3D 动画结束后骰子保持落定位置，不做放大或抬起展示；选中骰仅保留发光高亮。
  - 多个骰子（如 `10d100` 的 20 枚 d10）必须共享同一张桌面、同一个物理世界与同一组碰撞墙，按容器宽高比的全局紧凑网格排布，同时翻滚并互相碰撞，不再按组各自占一小块区域。
- v6 修正（2026-10-21 18:09）：
  - 结果数字必须在 3D / 简易动画完全落定后才显示，且与动画结束时朝上的骰面严格一致；动画期间结果区留空。
  - 同一结果经 Socket 广播与 REST 响应各到达一次时只播放一次动画（按骰值 / 骰面 / 重投次数去重），避免二次翻滚造成画面与结果不一致。
  - 等待玩家投掷时即渲染空骰桌（`Dice3DAdapter.showTable()`），不再等点击「投掷」后才出现。
  - 骰桌铺满聊天宽度与容器高度：桌面平面随正交相机视野铺满画布，CSS 不再给内嵌区域留 `margin/padding` 或圆角，消除桌面四周一圈非桌面留白。
  - 普通检定的百分骰只是十位 / 个位组合，不高亮任何骰子（`selected=null`）；仅奖励骰 / 惩罚骰需要从多个候选十位中取值时高亮选中骰。
- v5 修正（2026-10-21 17:30）：骰桌不再使用网站中央的半透明遮罩浮层，而是作为聊天内容内嵌在
  聊天记录中并水平居中；所有玩家可见，仅目标玩家可投掷。投掷结束后保留骰桌与骰子画面（冻结），
  结果数字显示在骰桌下方；历史消息与重连恢复只做静态还原，不重播动画，也不折叠为独立结果卡。
  同一房间同时只允许一个待投掷请求，重复检定返回 `dice_request_pending`；`complete` 在未投掷时
  不结束请求，避免工具拿到空结果后重复检定。
- 编写日期：2026-10-21
- 目标版本：v0.5.1b
- 影响范围：管理员设置、房间规则、AI 骰子工具、聊天窗口、Socket 事件、房间 API、房间日志、控制台命令、用户个性化、贴图管理、前端 `/dice` 指令
- 关联模块：`agents/tools/dice.py`、`agents/config.py`、`routes/rooms.py`、`routes/chat.py`、`room_log.py`、`room_console_commands.py`、`console_commands.py`、`dice_textures.py`、`routes/dice_textures.py`、`frontend/src/app/chat.ts`、`frontend/src/tools/diceTool.ts`、`frontend/src/app/dice-appearance-settings.ts`、`frontend/src/app/admin-dice-textures.ts`

---

## 1. 背景与目标

现有骰子结果只在聊天里以纯文本出现（如「侦察检定：1d100=34，目标 45，结果：成功」），
缺乏视觉反馈。本次改造希望在**不破坏现有骰子指令、工具名与返回结构**的前提下：

1. 骰子动画改为三档：**纯文本 / 简易骰子动画 / 3D 骰子动画**，管理员设置全局默认（默认 3D），房间可覆盖。
2. AI 每次调用骰子时，在聊天窗口正中生成「骰桌浮层」，以垂直俯视播放对应骰型的 3D 骰子动画
   （参考：`https://github.com/Matteas-Eden/dice-roller`）。
3. 简易骰子动画作为中间降级档（参考：`https://www.suijimm.com/dice/`）。
4. 保留原有纯文本结果输出，作为最终兜底；降级采用 `3D → 简易 → 纯文本` 逐层降级，
   **每次降级及报错都写入房间日志**。
5. 增加「玩家投掷骰子」开关（管理员默认 + 房间覆盖）：开启后，AI 需要玩家检定时
   自动摆好骰子但不投掷，等待被请求的玩家点击「投掷」或输入 `/roll`，再把结果返回给 AI。
   玩家投掷**不再依赖**动画档位，纯文本 / 简易 / 3D 下均可使用。
6. 通用投掷工具新增两个可选参数并对 AI 开放：
   - `show_table`：本次投掷召唤骰桌浮层；
   - `player_roll`：本次投掷改由玩家点击完成（仅在房间生效玩家投掷开启时有效）。
7. 允许引入经过许可的 3D/物理库，以获得真实的骰子-桌面、骰子-骰子碰撞效果。
8. 所有新增参数进入系统设置，后端有明确校验、日志与超时兜底。
9. 补充 API 文档、wiki 文档、房间控制台命令与管理员控制台命令。
10. 桌面与骰子的颜色支持玩家个性化：在用户设置中提供选色板，只影响该玩家本机显示。
11. 管理员可上传桌面贴图、骰子逐面贴图与皮肤套装；玩家可选择管理员上传的贴图，
    骰子贴图支持按面（1–20 / 0–9）逐面自定义。
12. 新增 `/roll` 指令：在任意动画档位下完成当前等待中的投掷；`/roll force` 允许
    站点管理员、房主或房间管理员强制投掷，不再校验目标玩家身份。
13. **骰型必须按实际类型渲染**：d4/d6/d8/d10/d12/d20/d100 各自使用对应几何，
    禁止所有骰型统一退化为 d6；解析失败时降级简易，而不是用 d6 冒充。
14. **真正的垂直俯视镜头**：相机从骰桌正上方 90° 向下看，而不是 45°–70° 斜俯视。
15. **骰面必须可读**：d6 显示点阵，其余骰型显示数字；落定后朝上的面必须清晰可见。
16. **骰桌为独立居中浮层**：不嵌套在骰娘聊天气泡内；骰娘先输出「正在召唤骰桌…」，
    骰桌显示在聊天窗口正中，结果展示在骰桌下方；历史记录中折叠为行内结果卡。
17. **多骰型同场与选中骰高亮**：百分骰由十位/个位两枚 d10 组成，奖励/惩罚骰可同时
    召唤多枚，最终被采纳的一枚使用高亮特效，其余弱化。
18. **房间级重投**：房主可设置「关闭 / 指定次数 / 无限重投」；开启后玩家可重投并在
    满意后点击「确定」，结果才返回 AI；关闭时不显示重投与确定按钮。
19. `/dice` 新增召桌参数：`/dice 2d6 --table` 召唤同一个居中骰桌并播放当前档位动画。

### 非目标

- 不改变前端指令 `/dice`、`/check`、`/sc` 的既有语法与输出文本；`/dice` 仅新增可选
  `--table` 参数用于召桌展示，结果仍由前端本地计算，不进入 AI 上下文。
- 不改变现有工具名、必填参数、已有返回字段；通用投掷只**新增可选参数**，
  其余工具只在 `visible_message.metadata` 中**追加**展示用字段。
- 不为骰子动画单独做设置页，动画相关开关统一放在现有「骰娘设置」区块。
- 首版不追求 100% 复刻参考仓库的全部交互（选骰界面、URL 参数等），
  只复刻其骰子模型、物理手感、桌面效果与垂直俯视镜头。

---

## 2. 名词与生效规则

| 名词 | 说明 |
| --- | --- |
| 动画档位 | `text`（纯文本）/ `simple`（简易动画）/ `3d`（3D 物理骰子） |
| 骰桌浮层 | 覆盖在聊天窗口正中、不嵌套在聊天气泡内的投掷桌面；结果展示在其下方 |
| 行内结果卡 | 骰桌结束后折叠进聊天记录的精简结果卡，用于历史回看 |
| 骰组 | 一次投掷中的一组同类型骰子，如 `d10(十位)`、`d10(个位)`、`d6×2` |
| 选中骰 | 奖励/惩罚或百分骰中最终决定结果的那一枚，使用高亮特效 |
| 玩家投掷模式 | AI 只摆骰不投掷，等待目标玩家点击后播放动画并返回结果 |
| 重投 | 房间开启后，玩家对已投出的结果再次投掷，仍由服务端 RNG 决定 |
| 目标玩家 | 本次检定对应的玩家（`player_name` 或触发 AI 的 `user_id`） |
| 生效值 | 房间规则优先；房间未设置（`null`）时继承管理员默认 |
| 降级 | 当前档位运行失败时，按 `3d → simple → text` 逐层回退，并写房间日志 |

**硬约束**

1. 玩家投掷模式**不依赖动画档位**：`text` / `simple` / `3d` 均可开启；管理员与房间规则不再做联动置灰或冲突校验。
2. 运行时动画降级不影响等待流程：3D 失败降级简易、简易失败降级纯文本，玩家投掷照常完成，但必须记录房间日志。
3. 暗投（`dark=true`）永远不召唤桌面、不等待玩家。
4. 纯文本档位下，等待期间骰娘输出「等待玩家投掷」；触发玩家可通过聊天输入区的投掷按钮或 `/roll` 完成投掷。
5. 颜色与贴图是个性化显示设置，只影响本机渲染，不改变服务端结果、其他玩家画面或 AI 上下文。
6. `/roll` 默认只能由目标玩家使用；`/roll force` 仅站点管理员、房主或房间管理员可用。
7. **禁止骰型默认化**：几何解析必须由骰型决定；任何未知类型走简易降级，绝不回退成 d6。
8. **镜头必须垂直**：3D 档位固定 90° 俯视（正交或超长焦透视），不提供斜视角度。
9. **骰桌不进入气泡**：任何档位下骰桌都是独立浮层，聊天记录只保留骰娘文字与行内结果卡。
10. **重投必须确认**：房间开启重投时，玩家未点击「确定」前不得把结果返回 AI；关闭时不得显示重投/确定按钮。

---

## 3. 现状梳理

### 3.1 后端

- [dice.py](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/backend/trpg_server/agents/tools/dice.py)
  - `roll_room_check()`、`roll_coc_check()`、`roll_sanity_check()`、`roll_dice()` 为纯函数；
  - `execute_*` 为 Agent 包装；`ROLL_DICE_TOOL` 当前仅支持 `expression`、`dark`
    （L401-L402），需新增 `show_table`、`player_roll`；
  - `_result_message()` 生成 `visible_message`，需支持追加 metadata。
- `routes/chat.py`
  - AI 请求内同步执行工具，`tool_messages` 随 HTTP 响应返回；
  - `tool_state` 提供 `thinking_stage_callback`，可更新等待阶段的思考气泡。
- `routes/rooms.py`
  - `HOUSE_RULE_DEFINITIONS` 现有 `boolean`、`nullable_integer`、`skill_bases` 等类型；
  - 房规 GET/PUT：`/api/rooms/<room_id>/house-rules`。
- [room_log.py](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/backend/trpg_server/room_log.py)
  - `log_room_event()`（L252-L295）统一写 `console.jsonl` 并广播；
  - 条目结构：`ts/level/source/actor/room_id/message/details`；
  - 归档时按 `load_room_log()` 一并保存。
- 控制台：
  - 管理员：[console_commands.py](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/backend/trpg_server/console_commands.py)
    按 domain 分发，写操作遵循 `--yes/-y` 确认约定；
  - 房间：[room_console_commands.py](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/backend/trpg_server/room_console_commands.py)
    使用 `ROOM_COMMAND_WHITELIST` + `room_command_help_text()`，仅限本房间。
- `server.py` 为多线程 WSGI；Socket.IO 为 `threading` 模式，允许工具线程阻塞等待。

### 3.2 前端

- [chat.ts](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/frontend/src/app/chat.ts)
  - 收到 `tool_messages` 后持久化并 `renderRoomMessage()`；
  - `type === "dice"` 使用现有 `dice-message` 样式；SAN 结果会同步角色状态；
  - `COMMAND_DEFINITIONS` 与命令分支需新增 `/roll`、`/roll confirm`、`/roll force`，
    并在 `toolManager.handleCommand` 之前异步处理。
- [diceTool.ts](file:///c:/Users/19826/Desktop/Mine/AIbased%20TRPG/frontend/src/tools/diceTool.ts)
  - 负责前端 `/dice` 本地投掷与阈值面板，不参与 AI 工具结果；
  - 新增 `--table` / `-t` 解析，召桌时复用 `TrpgDiceOverlay` 与当前动画档位。
- 构建：`tsc` 输出全局脚本 + `esbuild` 单独打包 React；已安装 `esbuild`，
  可将 3D 渲染器打包为独立按需加载 bundle。
- 设置页：
  - 管理员：`03-room-tools-auth-settings.html` + `tabs.ts` + `ConfigManager.ts`；
  - 房间：`roomHouseRulesModal` + `rooms.ts`；
  - 管理员设置沿用「改动待保存 + 确认」机制。

---

## 4. 需求拆解

### 4.1 管理员默认设置（第一级）

在「设置 → 常规设置 → 骰娘设置」新增：

| 控件 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 骰子动画档位 | 下拉：纯文本 / 简易 / 3D | **3D** | 全局默认；房间可覆盖 |
| 允许玩家投掷骰子 | 开关 | 关闭 | 全局默认；房间可覆盖；不依赖动画档位 |
| 玩家投掷等待超时（秒） | 数字 30–300 | 120 | 超时自动确认，避免 AI 永久等待 |
| 重投模式 | 下拉：关闭 / 指定次数 / 无限 | 关闭 | 全局默认；房间可覆盖 |
| 指定重投次数 | 数字 1–20 | 3 | 仅「指定次数」时可编辑 |

- 五项都纳入「改动待保存」栏，点击保存后写入 `general.toml`。
- 玩家投掷开关不依赖动画档位，纯文本 / 简易 / 3D 下均可开启。
- 重投模式不依赖动画档位，纯文本下也可开启。
- 玩家投掷开关关闭时，超时输入框与重投项置灰，仍保留已保存值。
- 「指定重投次数」仅在重投模式 = 指定次数时可编辑。

### 4.2 房间级覆盖（第二级）

在「房间规则」弹窗新增：

| 规则 | 选项 | 默认 |
| --- | --- | --- |
| 骰子动画档位 | 跟随管理员默认（当前：纯文本/简易/3D） / 纯文本 / 简易 / 3D | 跟随 |
| 玩家投掷骰子 | 跟随管理员默认（当前：开/关） / 开启 / 关闭 | 跟随 |
| 重投 | 跟随管理员默认（当前：关/指定次数/无限） / 关闭 / 指定次数 / 无限 | 跟随 |
| 重投次数 | 跟随管理员默认 / 1–20 | 跟随 |

- 玩家投掷、重投均不依赖动画档位，可与任意档位自由组合。
- 重投次数仅在「重投 = 指定次数」时可编辑。
- 保存成功后返回并刷新 `house_rules` 与 `dice_animation`。

### 4.3 自动骰子动画

- 生效档位为 `3D` / `简易` 时，AI 的检定工具先输出骰娘文字「正在召唤骰桌进行投掷…」，
  随后在**聊天窗口正中的独立浮层**中生成骰桌并播放动画；结果展示在骰桌下方。
- 浮层不嵌套在骰娘气泡内，使用聊天容器的绝对/固定定位，带遮罩与关闭（关闭仅收起视觉，不改变结果）。
- 动画结束后，骰桌浮层自动收起，同一结果折叠为聊天流中的**行内结果卡**（静态骰面 + 结果文本）。
- 通用 `NdM` 默认不召唤桌面（保持轻量）；仅当 `show_table=true` 或 `player_roll=true` 时召唤。
- 暗投不召唤桌面，也不创建浮层。
- 实时消息播放动画；加载历史记录时只渲染行内结果卡，不重新播放动画。
- 生效档位为 `纯文本` 时不创建浮层，只显示骰娘文字与原有文本摘要。
- 同一客户端同时只允许一个骰桌浮层；若已有活动浮层，新投掷排队，前一个结束后再展示。

### 4.4 玩家投掷模式

触发范围：

| 工具 | 是否等待 | 目标玩家解析 |
| --- | --- | --- |
| `check.roll_room_check` | 是（生效玩家投掷开启时） | `player_name` |
| `dice.roll_coc_check` | 是（能解析到当前房间玩家时） | `character_name` 或 `context.user_id` |
| `san.roll_sanity_check` | 是（生效玩家投掷开启时） | `player_name` |
| `dice.roll` + `player_roll=true` | 是（生效玩家投掷开启时） | 触发 AI 的 `user_id` |
| `dice.roll`（未传参数） | 否 | 即时返回 |
| `dark=true` | 否 | 即时返回，不召唤桌面 |

流程：

1. AI 调用工具 → 工具检测到需要等待。
2. 生成待投掷请求（骰组与真实骰型、数量、面数、检定名、难度、目标值、奖励/惩罚骰、
   重投模式与剩余次数），广播 `player_dice_request`，思考气泡显示「等待 <玩家> 投掷骰子…」。
3. 前端根据生效档位展示：
   - `3D` / `简易`：在聊天窗口正中挂载骰桌浮层，目标玩家可点击「投掷」；
   - `纯文本`：骰娘在聊天中输出「等待玩家投掷」，触发玩家的聊天输入区出现「投掷骰子」按钮。
   - 三种档位下，目标玩家都可以直接输入 `/roll` 完成投掷。
4. 目标玩家点击按钮或输入 `/roll` → 前端调用 `roll` 接口 →
   后端用服务端 RNG 产生权威结果，返回骰面并广播 `player_dice_rolled`。
5. 前端在居中骰桌浮层中播放当前档位动画（纯文本跳过动画）；
   纯文本模式在收到结果后进入确认/完成阶段。
6. 重投与确认：
   - 房间重投关闭：动画结束后立即调用 `complete`，结果返回 AI；
   - 房间重投开启：动画结束后在骰桌下方显示「重投（剩余 N 次/无限）」与「确定」；
     点击「重投」→ 服务端重新 RNG → 播放新一轮动画 → 再次等待选择；
     点击「确定」→ 调用 `confirm` 后 `complete`，把最终结果返回 AI；
   - 未点击「确定」前，AI 一直等待（受等待超时约束）。
7. 后端解除工具阻塞，结果返回 AI，AI 继续叙事；其他玩家同步看到同一结果，
   骰桌浮层收起为行内结果卡。

重投规则：

- 每个待投掷请求独立计数，仅作用于本请求，不影响下一次检定。
- 指定次数：用尽后「重投」按钮置灰并显示「重投次数已用完」，只保留「确定」。
- 无限重投：按钮始终可用，显示「无限」。
- 重投只重新生成结果，不重建请求、不重复发送 `player_dice_request`。
- 重投期间 AI 仍处于等待状态，思考气泡文案保持「等待 <玩家> 投掷骰子…」。

`/roll force`：

- 站点管理员、房主或房间管理员可用；
- 对当前房间唯一的待投掷请求立即以服务端自动投掷方式结算，不校验目标玩家身份；
- 广播 `player_dice_forced`，所有端直接显示结果；目标玩家的等待状态解除。

超时与兜底：

- 等待超过配置时间 → 自动确认最后一次结果，标注「等待超时，系统自动确认」。
- 目标玩家不在线/已退出 → 自动投掷/自动确认，标注「目标玩家不在线，系统自动处理」。
- 重投关闭时，动画完成后客户端未回调 `complete`（如关页）→ 服务端在投掷后 5 秒自动完成。
- 重投开启时，服务端不按 5 秒自动完成，避免截断重投窗口；以等待截止时间自动确认。
- 房主/房间管理员可 `cancel`：取消并返回「投掷被管理员取消」。
- 房主/房间管理员可 `force`：强制该请求立即自动投掷并确认。

### 4.5 通用投掷新增参数（对 AI 开放）

`dice.roll` / `dice.roll_dice` 的 schema 追加：

```json
{
  "show_table": {
    "type": "boolean",
    "description": "Set true to summon the virtual dice table for this roll. Ignored in text-only animation mode."
  },
  "player_roll": {
    "type": "boolean",
    "description": "Set true to let the current player throw the dice. Only honored when player-roll mode is enabled; otherwise the roll resolves immediately. Works in text, simple, and 3D animation modes."
  }
}
```

行为：

- `show_table=true`：召唤桌面并播放当前档位动画。
- `player_roll=true`：隐含 `show_table=true`，等待发起 AI 请求的玩家点击。
- 生效玩家投掷关闭时，`player_roll` 被忽略并即时投掷；结果 metadata 标记
  `player_roll_ignored: true`，服务器 `logger.info` 记录，不写房间日志（避免噪声）。
- `dark=true` 时两个参数都被忽略，避免泄密。
- 在 `纯文本` 档位下 `show_table` 被忽略，仅输出文本。

### 4.6 逐层降级与房间日志

| 当前档位 | 失败原因示例 | 降级动作 | 记录 |
| --- | --- | --- | --- |
| 3D | bundle 加载失败、WebGL 不可用、物理库初始化异常、单次渲染异常 | 本次（或本会话）改用简易动画 | 写房间日志，`source=dice_animation` |
| 简易 | DOM/动画 API 异常 | 改用纯文本 | 写房间日志 |
| 纯文本 | 不存在渲染 | 无 | 无 |

- 全局性失败（bundle/WebGL）在**本会话**只记录一次，并记住降级状态，后续不再尝试 3D。
- 单次性失败（如某个骰型几何构建异常）每次都记录，但房间级限流 30 条/分钟。
- **骰型解析/几何构建失败不得回退为 d6**：记录 `unsupported_dice` / `geometry_error`
  后直接改用简易动画，并在简易卡片上标注真实骰型（如 `d100`）。
- 记录内容包含：`from_level`、`to_level`、`reason`、`error`、`dice`、`tool_name`、
  `request_id`、`scope`（`session` / `roll`）、操作者。
- 降级不影响 AI 结果，只影响视觉层。

### 4.7 桌面与骰子外观个性化

**颜色**

在「用户设置 → 外观偏好」新增「骰子外观」区域，提供：

| 项目 | 控件 | 默认 |
| --- | --- | --- |
| 骰子主体颜色 | 选色板 | 跟随当前主题 |
| 骰面数字/点阵颜色 | 选色板 | 跟随当前主题 |
| 桌面主体颜色 | 选色板 | 跟随当前主题 |
| 桌沿/高光颜色 | 选色板 | 跟随当前主题 |

- 提供若干预设配色与「恢复默认」。
- 颜色保存于浏览器 `localStorage`，只影响本机显示；
  不写入房间数据，不改变其他玩家画面，也不影响服务端结果。
- 纯文本档位下颜色设置保留但不产生视觉效果。

**贴图**

- 管理员在「设置 → 骰子贴图库」上传：
  - 桌面贴图（单张）；
  - 骰子逐面贴图（按面值绑定，如 d6 的 1–6、d20 的 1–20、d10 的 0–9）；
  - 由贴图组成的「桌面皮肤」与「骰子皮肤」（按骰型保存）。
- 玩家在「骰子外观」中选择管理员已启用的桌面皮肤、以及各骰型的骰子皮肤；
  未绑定贴图的面使用所选颜色 + 点阵/数字渲染。
- d100 使用两枚 d10（十位/个位），共用 d10 逐面贴图；十位骰保留强调色区分。
- 贴图选择同样保存在本机 `localStorage`，只影响本机显示。
- 皮肤/贴图加载失败时回退到颜色渲染，不改变动画档位，但记录一条房间警告日志
  （`reason=texture_load_failed`，不作为降级）。

### 4.8 `/roll` 指令

| 用法 | 权限 | 行为 |
| --- | --- | --- |
| `/roll` | 当前待投掷请求的目标玩家 | 完成当前房间等待中的投掷；已有结果且可重投时等价于「重投」 |
| `/roll confirm` | 目标玩家 | 等价于点击「确定」，仅在房间开启重投时有效 |
| `/roll force` | 站点管理员、房主、房间管理员 | 强制服务端自动投掷并确认，不校验目标玩家 |

- 三种动画档位均可用：
  - 纯文本：立即调用 `roll`，聊天显示结果文本；开启重投时显示「重投/确定」按钮；
  - 简易/3D：调用 `roll` 后在居中骰桌浮层播放对应动画；
    若本机没有活动骰桌（例如错过 Socket），收到结果后临时创建浮层再播放。
- 没有待投掷请求时：提示「当前没有等待中的投掷」。
- 有请求但当前用户不是目标玩家：提示「当前等待 <玩家> 投掷」；
  有权限时可使用 `/roll force`。
- `force` 只作用于当前房间唯一的待投掷请求；无请求时返回 404 提示。
- `/roll` 不额外发送聊天消息，避免与骰娘结果重复；结果消息仍由工具返回。
- 加入 `COMMAND_DEFINITIONS`、命令面板与帮助文档。

### 4.9 `/dice` 召桌参数

`/dice` 原本是纯前端本地投掷（不进入 AI 上下文）。新增可选参数：

| 用法 | 行为 |
| --- | --- |
| `/dice 2d6` | 保持现状：直接输出文本结果 |
| `/dice 2d6 --table` | 在聊天窗口正中召唤骰桌浮层并播放当前档位动画，结果显示在骰桌下方 |
| `/dice 1d100 --table` | 召唤两枚 d10（十位/个位）并按百分骰规则合成结果 |
| `/dice 4d6 --table --keep 3` | 多骰时高亮被采纳的 3 枚（可选扩展，首版不做） |

- 参数别名：`--table` / `-t`；位置不限（`/dice --table 2d6` 同样有效）。
- 仍使用前端本地 RNG，不调用后端工具、不写入 AI 上下文、不触发玩家投掷请求。
- 动画档位为 `text` 时忽略 `--table`，仅输出文本并提示「当前为纯文本动画档位」。
- 结果展示与 AI 投掷一致：骰娘文字「正在召唤骰桌进行投掷…」+ 居中浮层 + 下方结果，
  结束后折叠为行内结果卡。
- 命令解析写入 `COMMAND_DEFINITIONS` 与帮助文案。

### 4.10 骰桌浮层与结果展示

**布局**

```
┌─ 聊天窗口 ──────────────────────────────┐
│  骰娘：正在召唤骰桌进行投掷…             │  ← 普通聊天气泡
│                                          │
│        ┌─ 骰桌浮层（居中，独立） ─┐       │
│        │  俯视 90° 骰桌 + 骰子   │       │
│        │  状态/提示/按钮         │       │
│        └─────────────────────────┘       │
│        ┌─ 结果区（浮层正下方） ──┐       │
│        │  合计 / 成败 / 目标值   │       │
│        └─────────────────────────┘       │
│  （浮层关闭后，结果折叠为行内结果卡）    │
└──────────────────────────────────────────┘
```

- 挂载点：聊天主容器的直接子节点，`position: absolute/fixed` 居中；
  `z-index` 高于消息列表但低于全局弹窗/通知。
- 禁止作为 `.message`、`.dice-message` 或骰娘气泡的子节点。
- 遮罩：半透明，点击遮罩不关闭进行中的投掷；仅结束后可关闭。
- 结果区固定位于骰桌下沿外侧，不覆盖骰面；窄屏时与骰桌同宽并允许换行。
- 浮层内提供状态行（等待投掷 / 投掷中 / 可重投 / 已确认 / 已取消）与操作按钮。
- 关闭/结束时写入一条行内结果卡（包含最终骰面与文本摘要），确保刷新后仍可回看。

### 4.11 多骰型与选中骰高亮

- 投掷计划由「骰组」组成，而非单一骰型：

```json
{
  "groups": [
    { "role": "tens", "type": "d10", "count": 1, "labels": ["30"] },
    { "role": "ones", "type": "d10", "count": 1, "labels": ["4"] }
  ]
}
```

- 常见组合：
  - 百分骰：两枚 d10（十位、个位），十位骰使用不同主色/标记，避免混淆；
  - 奖励/惩罚骰：个位骰 + 多枚十位候选骰，同场显示；
  - `NdM`：N 枚同型骰；
  - 混合表达式（如 `1d100+2d6`）：按骰组分别生成，桌面上分区摆放。
- 选中骰：
  - 服务端在 metadata 返回 `selected` 索引（骰组 + 组内序号）；
  - 被选中骰使用高亮特效：外发光描边、轻微上浮/放大、落地后脉冲；
  - 未选中的候选骰降低不透明度（约 0.45）并去饱和；
  - 纯文本档位在结果文本中用 `**`/括号标注选中值，不显示视觉特效。
- 高亮只在结果落定后出现，投掷过程中所有骰子同等显示。

### 4.12 房间重投设置

房间规则新增「重投」：

| 选项 | 含义 | 按钮 |
| --- | --- | --- |
| 关闭 | 不提供重投 | 无重投、无确定（动画结束自动返回） |
| 指定次数 | 最多重投 N 次（1–20） | 「重投（剩余 N）」+「确定」 |
| 无限重投 | 不限次数 | 「重投（无限）」+「确定」 |

- 仅房主/房间管理员可修改；房间规则与管理员默认同样遵循「房间优先、null 继承」。
- 只对「玩家投掷」模式生效；自动投掷不显示重投按钮。
- 关闭时，玩家投掷流程与旧版一致：动画结束即 `complete`。
- 开启时，必须点击「确定」才 `confirm → complete`；重投不改变请求与等待状态。
- 重投仍由服务端 RNG 决定，不能通过前端篡改。
- 重投次数用尽后按钮置灰，仍可「确定」；无限模式不显示计数。
- 重投与玩家投掷同属房间规则，但**重投不依赖动画档位**，纯文本下同样可用。

### 4.13 控制台命令

管理员控制台新增 `dice` 域：

```
dice animation show
dice animation set <text|simple|3d>     # 需 --yes
dice player-roll show
dice player-roll <on|off>               # 需 --yes
dice reroll show
dice reroll <off|limited|unlimited>    # 需 --yes
dice reroll-limit <1-20>                # 需 --yes
dice timeout <30-300>                   # 需 --yes
```

房间控制台新增（仅房主/房间管理员，仅本房间）：

```
dice animation show
dice animation set <text|simple|3d|inherit>
dice player-roll show
dice player-roll <on|off|inherit>       # 与动画档位无关
dice reroll show
dice reroll <off|limited|unlimited|inherit>
dice reroll-limit <1-20|inherit>
```

- 更新 `command_help_text()`、`room_command_help_text()`、`ROOM_COMMAND_WHITELIST`；
- 更新 `docs/console-commands.md`；
- 所有设置变更写入房间/服务器日志。

---

## 5. 配置与生效算法

### 5.1 管理员配置（`general.toml` → `[ai]`）

| 键 | 类型 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- | --- |
| `dice_animation_level` | enum | `3d` | `text`/`simple`/`3d` | 骰子动画默认档位 |
| `dice_player_roll` | bool | `false` | - | 玩家投掷默认开关 |
| `dice_player_roll_timeout` | int | `120` | 30–300 | 等待玩家投掷秒数 |
| `dice_reroll_mode` | enum | `off` | `off`/`limited`/`unlimited` | 重投默认模式 |
| `dice_reroll_limit` | int | `3` | 1–20 | 指定次数模式下的重投上限 |

- `AIRuntimeConfig` 新增字段；非法档位回落 `3d` / `off` 并 `logger.warning`。
- `dice_reroll_limit` 仅在 `dice_reroll_mode=limited` 时参与计算。
- `ConfigManager.loadConfigs()` 回填设置页；`tabs.ts` 增加下拉绑定与待保存逻辑。
- `data/config/general.toml` 的 `[ai]` 段补齐默认值。

### 5.2 房间规则（`info.json` → `house_rules`）

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `dice_animation_level` | nullable enum | `null` | `null` 继承管理员默认 |
| `player_dice_roll` | nullable_boolean | `null` | `null` 继承管理员默认 |
| `dice_reroll_mode` | nullable enum | `null` | `null` 继承管理员默认 |
| `dice_reroll_limit` | nullable_integer | `null` | `null` 继承管理员默认；1–20 |

- `HOUSE_RULE_DEFINITIONS` 新增 `nullable_enum` 与 `nullable_boolean`。
- 校验：
  - 档位只能是 `text/simple/3d/null`；
  - 玩家投掷只能是 `true/false/null`；
  - 重投模式只能是 `off/limited/unlimited/null`；
  - 重投次数只能是 `null` 或 1–20；
  - 各项相互独立，不做联动校验。
- GET/PUT/房间详情返回 `dice_animation`。

### 5.3 生效算法（后端唯一实现）

```
level   = room.dice_animation_level if not null else admin.dice_animation_level
player  = room.player_dice_roll      if not null else admin.dice_player_roll
timeout = admin.dice_player_roll_timeout
mode    = room.dice_reroll_mode      if not null else admin.dice_reroll_mode
limit   = room.dice_reroll_limit     if not null else admin.dice_reroll_limit
rerolls = 0 if mode == "off" else (-1 if mode == "unlimited" else limit)
```

`routes/chat.py` 构建上下文时写入：

```
tool_state["dice_animation_level"] = level
tool_state["player_dice_roll"] = player
tool_state["player_dice_roll_timeout"] = timeout
tool_state["dice_reroll_mode"] = mode
tool_state["dice_reroll_limit"] = limit
```

工具在测试/控制台等非聊天路径下自行读取房间规则 + 管理员配置兜底。

### 5.4 展示配置（房间详情）

```json
{
  "dice_animation": {
    "level": "3d",
    "player_roll": true,
    "timeout": 120,
    "reroll_mode": "limited",
    "reroll_limit": 3,
    "level_source": "room",
    "player_roll_source": "admin",
    "reroll_source": "room"
  }
}
```

---

## 6. 后端设计

### 6.1 新增模块：`agents/player_roll.py`

内存状态机，不依赖 Flask：

```python
@dataclass
class PendingRoll:
    request_id: str
    room_id: str
    roller_user_id: str
    roller_name: str
    plan: dict
    resolver: Callable[[], dict]
    created_at: float
    expires_at: float
    state: str          # pending/rolled/awaiting_confirm/finished/cancelled/timeout/forced
    result: dict | None
    rolled_at: float | None
    reroll_limit: int   # 0=关闭，-1=无限，N=可重投 N 次
    rerolls_used: int
    confirmed: bool
    event: threading.Event
    lock: threading.Lock
```

对外函数：

| 函数 | 作用 |
| --- | --- |
| `register_broadcaster(fn)` | 注入 `socketio.emit(..., room=room_id)` |
| `wait_for_player_roll(context, plan, resolver)` | 创建请求、广播、阻塞等待 |
| `get_pending(room_id, request_id)` | 刷新/重连恢复 |
| `resolve_roll(room_id, request_id, user_id)` | 首次投掷：校验身份 → `resolver()` → 广播结果 |
| `reroll(room_id, request_id, user_id)` | 重投：校验身份与剩余次数 → `resolver()` → 重置动画状态 |
| `confirm_roll(room_id, request_id, user_id)` | 玩家确认 → 标记 `confirmed` → 解除阻塞 |
| `complete_roll(room_id, request_id, user_id)` | 动画结束：重投关闭时等价于确认；开启时忽略 |
| `cancel_roll(...)` | 管理员取消，工具返回「投掷被管理员取消」 |
| `force_roll(...)` | 管理员强制以自动投掷方式立即结算并确认 |
| `cleanup_room(room_id)` | 归档/删除时清理 |

规则：

- 每房间同时仅 1 个待投掷请求；已有请求时新的检定即时投掷。
- 只有目标玩家能 `roll/reroll/complete/confirm`；首次生效，重复投掷在重投关闭时返回 409。
- `/roll force` 对应的 `force_roll` 跳过目标玩家校验，允许站点管理员、房主或房间管理员调用。
- 等待流程与动画档位无关；纯文本档位同样创建待投掷请求与等待阻塞。
- 重投关闭：`roll` 后 5 秒自动 `complete`，避免客户端关页卡死。
- 重投开启：进入 `awaiting_confirm`，不自动完成；在 `expires_at` 到达时自动确认最后一次结果。
- 重投开启时 `complete` 不解除阻塞；`confirm` 立即解除；已确认后的 `complete` 幂等返回成功。
- 重投次数：`reroll_limit=0` 拒绝重投（409）；`-1` 不扣减；`N>0` 每次 `rerolls_used += 1`，用尽后 409。
- 重投会重置 `rolled_at` 与前端动画状态，但不重置 `expires_at`、不重发请求事件。
- 等待上限 `min(timeout, ai_request_timeout - 5)`。
- 广播未注入时立即投掷并直接确认（测试/控制台）。
- 关键动作写 `logger.info` + 房间日志（创建/投掷/重投/确认/超时/取消/强制）。

### 6.2 新增蓝图：`routes/dice_rolls.py`

| 方法 | 路径 | 权限 | 作用 |
| --- | --- | --- | --- |
| GET | `/api/rooms/<room_id>/dice-requests/<request_id>` | 房间成员 | 查询状态（含重投剩余次数） |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/roll` | 目标玩家 | 首次投掷，返回骰面 |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/reroll` | 目标玩家 | 重投（校验剩余次数），返回新骰面 |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/confirm` | 目标玩家 | 确认最终结果，解除阻塞 |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/complete` | 目标玩家 | 动画结束；重投关闭时等价于确认 |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/cancel` | 房主/房间管理员 | 取消并返回取消结果 |
| POST | `/api/rooms/<room_id>/dice-requests/<request_id>/force` | 站点管理员/房主/房间管理员 | 强制自动投掷并确认（`/roll force`） |

- `roll` 在重投开启且已有结果时返回 409，提示使用 `reroll`。
- `reroll` 在重投关闭或次数用尽时返回 409，并带 `remaining`。
- `confirm` 在无结果时返回 409；重复确认幂等返回当前结果。
- 响应统一带 `remaining`（`-1` 表示无限）与 `state`，供前端更新按钮。

### 6.3 Socket 事件

| 事件 | 方向 | 负载 |
| --- | --- | --- |
| `player_dice_request` | 服务端 → 房间 | `{room_id, request_id, roller, plan, check, expires_at, tool_name, reroll: {mode, limit, remaining}}` |
| `player_dice_rolled` | 服务端 → 房间 | `{room_id, request_id, faces, groups, selected, rolls, total, success, success_level, summary, reroll: {mode, remaining}}` |
| `player_dice_rerolled` | 服务端 → 房间 | 同上，附 `reroll_index` |
| `player_dice_confirmed` | 服务端 → 房间 | `{room_id, request_id, result, confirmed_by}` |
| `player_dice_cancelled` | 服务端 → 房间 | `{room_id, request_id, reason}` |
| `player_dice_forced` | 服务端 → 房间 | `{room_id, request_id, operator, result}` |

### 6.4 骰子工具拦截与新增参数

保持纯函数 `roll_*` 不变，只改 `execute_*`：

```python
def execute_roll_room_check(arguments, context):
    if _should_wait_for_player(context, arguments):
        result = wait_for_player_roll(
            context,
            plan=_build_room_check_plan(arguments, context),
            resolver=lambda: roll_room_check(_merge_context_thresholds(arguments, context)),
        )
    else:
        result = roll_room_check(_merge_context_thresholds(arguments, context))
    return _attach_trigger_reveal(result, arguments, context)
```

- `_should_wait_for_player()`：生效玩家投掷开启、有房间、目标玩家在线、非暗投、非重复调用。
- `_build_plan()` 生成骰组，供前端按真实骰型渲染：

```python
{
  "mode": "percentile",
  "expression": "1d100",
  "groups": [
    {"role": "tens", "type": "d10", "count": 1},
    {"role": "ones", "type": "d10", "count": 1},
    # 奖励/惩罚时追加 candidates 组
  ],
  "selected": null,
  "bonus_dice": 1,
  "penalty_dice": 0
}
```

- `selected` 在 `_build_plan` 中恒为 `null`；结果产生后由 `_result_metadata()` 计算并写回，
  普通检定取决定值的那一对，奖励/惩罚取最终采纳的十位候选骰。
- 通用 `NdM`：`groups=[{"role":"normal","type":"dN","count":N}]`；混合表达式按加法项拆成多个组。
- `san.roll_sanity_check` 会修改角色状态，必须延迟到 `resolver()` 内执行。
- 通用投掷：
  - `roll_dice()` 保持原签名与返回；
  - `execute_roll_dice(arguments, context)` 解析 `show_table/player_roll/dark`；
  - `player_roll=true` 且模式开启 → `wait_for_player_roll`，roller 为 `context.user_id`；
  - 否则即时投掷，并在 metadata 标记参数是否生效。
- 暗投优先级最高：忽略 `show_table/player_roll`。
- **禁止默认骰型**：`groups` 中的 `type` 必须来自表达式/检定规则；解析失败时抛错并走简易降级，
  不允许把未知类型替换为 `d6`。

### 6.5 展示元数据（只追加）

在检定与通用投掷的 `visible_message.metadata` 追加：

```json
"dice_roll": {
  "mode": "percentile",
  "expression": "1d100",
  "count": 2,
  "sides": 10,
  "faces": [3, 4],
  "labels": ["30", "4"],
  "groups": [
    { "role": "tens", "type": "d10", "count": 1, "faces": [3], "labels": ["30"] },
    { "role": "ones", "type": "d10", "count": 1, "faces": [4], "labels": ["4"] }
  ],
  "selected": { "group": 0, "index": 0 },
  "roll": 34,
  "rolls": [34],
  "candidates": [34, 84],
  "bonus_dice": 0,
  "penalty_dice": 0,
  "total": 34,
  "success": true,
  "success_level": "regular",
  "check_name": "侦察",
  "difficulty": "hard",
  "target": 45,
  "roller": "调查员A",
  "roller_user_id": 12,
  "request_id": null,
  "auto": false,
  "show_table": true,
  "player_roll": false,
  "player_roll_ignored": false,
  "reroll": { "enabled": false, "mode": "off", "limit": 0, "used": 0 },
  "confirmed": true
}
```

- 检定工具默认 `show_table=true`；通用投掷按参数。
- percentile 使用两枚 d10（十位/个位）；奖励/惩罚候选放 `candidates`。
- `groups` 为前端渲染的真实依据（每组的 `type`、`count`、面值）；旧字段保留兼容。
- `selected` 指向最终决定结果的骰子，用于高亮；无选中概念时为 `null`。
- `reroll` 仅在玩家投掷时有意义；自动投掷恒为 `enabled=false`。
- 原有字段不删除、不改名；`_result_message()` 增加可选 `extra_metadata` 参数。

### 6.6 去重与等待阶段

- 玩家投掷模式下，三个检定工具 + 带 `player_roll` 的通用投掷绕过同参数去重。
- `routes/chat.py` 写入 `tool_state["no_dedupe_tools"]`；`runtime.py` 在去重前读取。
- 等待期间调用 `thinking_stage_callback("dice_wait", "等待 <玩家> 投掷骰子…")`。

### 6.7 降级日志接口

新增：

```
POST /api/rooms/<room_id>/dice-animation/degraded
```

请求体：

```json
{
  "request_id": "roll-uuid",
  "tool_name": "dice.roll",
  "from_level": "3d",
  "to_level": "simple",
  "reason": "webgl_unavailable",
  "error": "WebGL context creation failed",
  "scope": "session",
  "dice": { "count": 2, "sides": 6, "expression": "2d6" }
}
```

- 权限：房间成员；CSRF 正常校验。
- 调用 `log_room_event(..., level="warning", source="dice_animation", actor=..., room_id=..., **details)`。
- 房间级限流 30 条/分钟；超限静默丢弃并 `logger.warning`。
- `reason` 枚举：`bundle_load_failed`、`webgl_unavailable`、`physics_init_failed`、
  `render_error`、`unsupported_dice`、`geometry_error`、`dice_count_limit`、
  `simple_animation_error`、`texture_load_failed`（后者只记警告，不参与档位降级）。
- `dice` 字段包含 `expression`、`groups`、`types`，便于定位具体骰型。
- `3d → simple → text` 每层都记录；全局失败以 `scope=session` 记录一次。

### 6.8 骰子贴图库与皮肤

**存储**

```
data/dice-textures/
  index.json
  assets/<uuid>.png|jpg|webp
```

`index.json` 结构：

```json
{
  "version": 1,
  "assets": [
    {
      "id": "asset-uuid",
      "name": "猩红桌布",
      "kind": "table",
      "file": "assets/<uuid>.png",
      "content_type": "image/png",
      "width": 1024,
      "height": 1024,
      "size": 345678,
      "uploaded_by": 1,
      "created_at": 1730000000.0,
      "enabled": true
    }
  ],
  "table_skins": [
    { "id": "table-skin-uuid", "name": "猩红桌布", "asset_id": "asset-uuid", "enabled": true }
  ],
  "dice_skins": [
    {
      "id": "dice-skin-uuid",
      "name": "古铜 d6",
      "die_type": "d6",
      "faces": { "1": "asset-a", "2": "asset-b", "3": "asset-c", "4": "asset-d", "5": "asset-e", "6": "asset-f" },
      "enabled": true
    }
  ]
}
```

- 骰型：`d4`、`d6`、`d8`、`d10`、`d12`、`d20`、`d100`。
- d10 / d100 的面值键为 `0`–`9`；其余为 `1`–面数。
- 缺少某面的贴图时，该面使用颜色 + 点阵/数字渲染。
- 删除被皮肤引用的贴图时返回 409，要求先解除引用。

**模块与接口**

- 新增 `backend/trpg_server/dice_textures.py`：原子读写 `index.json`、校验、CRUD、引用检查。
- 新增 `routes/dice_textures.py` 蓝图：

| 方法 | 路径 | 权限 | 作用 |
| --- | --- | --- | --- |
| GET | `/api/dice-textures/library` | 登录用户 | 返回启用的贴图与皮肤 |
| POST | `/api/dice-textures/assets` | 管理员 | 上传贴图（multipart） |
| DELETE | `/api/dice-textures/assets/<id>` | 管理员 | 删除贴图（被引用则 409） |
| POST | `/api/dice-textures/table-skins` | 管理员 | 创建/更新桌面皮肤 |
| POST | `/api/dice-textures/dice-skins` | 管理员 | 创建/更新骰子皮肤（逐面绑定） |
| DELETE | `/api/dice-textures/skins/<id>` | 管理员 | 删除皮肤 |
| GET | `/api/dice-textures/assets/<id>` | 登录用户 | 读取图片文件 |

**上传校验**

- 允许 PNG / JPEG / WebP；通过文件头魔数校验，不信任扩展名与 MIME。
- 单文件 ≤ 3 MB；尺寸 128–2048 px；建议骰面为正方形。
- 文件名重命名为 UUID，禁止路径穿越；原子写入索引。
- 上传/删除写入服务器日志，记录操作者与文件名。
- 读取接口设置 `Cache-Control: public, max-age=31536000, immutable`（UUID 不复用）。

### 6.9 控制台命令

- 管理员 `console_commands.py` 增加 `dice` 域与帮助文本；写操作 `--yes` 确认。
- 房间 `room_console_commands.py` 增加白名单与 `dice` 子命令；仅本房间、仅房主/管理员。
- 每次设置变更调用 `log_room_event()`（房间命令）或全局日志（管理员命令）。

---

## 7. 前端设计

### 7.1 模块与构建

| 文件 | 职责 |
| --- | --- |
| `frontend/src/dice3d/index.ts` | 3D 渲染器入口（three + cannon-es，独立 bundle） |
| `frontend/src/dice3d/scene.ts` | 场景、**垂直俯视正交相机**、灯光、桌面、阴影 |
| `frontend/src/dice3d/diceFactory.ts` | 显式 `resolveGeometry(dieType)`，按骰型构建几何 |
| `frontend/src/dice3d/faceTexture.ts` | 逐面 CanvasTexture：d6 点阵、其余数字，朝向与 UV 对齐 |
| `frontend/src/dice3d/physics.ts` | cannon-es 世界、每种骰型对应碰撞体、投掷冲量、落定检测 |
| `frontend/src/dice3d/materials.ts` | 骰子/桌面的颜色与逐面贴图材质、选中高亮 |
| `frontend/src/dice3d/types.ts` | 渲染器对外接口（含骰组、选中索引、调色板与皮肤） |
| `frontend/src/tools/dice/diceTypes.ts` | 骰型解析、骰组构建、支持判断、档位与降级决策 |
| `frontend/src/tools/dice/diceAppearance.ts` | 读取/保存本机颜色与贴图选择，生成调色板与纹理 URL |
| `frontend/src/tools/dice/diceOverlay.ts` | 聊天窗口居中骰桌浮层的挂载/收起/排队与遮罩 |
| `frontend/src/tools/dice/dice3dAdapter.ts` | 按需加载 bundle，桥接 `window.TrpgDice3D` |
| `frontend/src/tools/dice/diceSimple.ts` | 简易动画（按骰组渲染数字/点阵，应用颜色、贴图与高亮） |
| `frontend/src/tools/dice/diceDegradation.ts` | 降级上报与本地记忆 |
| `frontend/src/tools/dice/diceTable.ts` | 骰桌控制器、投掷/重投/确定按钮、Socket/REST 协调 |
| `frontend/src/app/dice-appearance-settings.ts` | 用户设置「骰子外观」界面与预览 |
| `frontend/src/app/admin-dice-textures.ts` | 管理员贴图库、逐面皮肤编辑器 |
| `frontend/src/app/chat.ts` | 拦截 `/dice --table`，接入居中浮层 |
| `frontend/src/styles/05-dice-animation.css` | 桌面、浮层、简易动画、选色与贴图 UI、窄屏适配 |

- 3D 代码单独用 esbuild 打包为 IIFE：
  `frontend/src/dice3d/index.ts --bundle --format=iife --global-name=TrpgDice3D --minify`
  → `dist/public/js/dice/dice3d.js`，仅在需要 3D 时注入 `<script>` 按需加载。
- 新增 `npm run build:dice` 并串入 `build:frontend`。
- 全局脚本顺序：`diceTypes.js` → `diceAppearance.js` → `diceOverlay.js` → `diceSimple.js`
  → `dice3dAdapter.js` → `diceDegradation.js` → `diceTable.js` → `diceAppearanceSettings.js`
  → `adminDiceTextures.js` → `chat.js`。
- `types/global.d.ts` 增加 `TrpgDiceTable`、`TrpgDice3D`、`TrpgDiceOverlay`；
  `types/room.d.ts` 增加 `dice_animation`（含 reroll 字段）。

### 7.2 自动投掷渲染

1. `renderRoomMessage()` 检查 `metadata.dice_roll`、`show_table` 与暗投标记。
2. 实时消息且需要桌面、生效档位 ≠ `text`：
   - 在聊天流插入骰娘文字气泡「正在召唤骰桌进行投掷…」；
   - 调用 `TrpgDiceOverlay.mount(plan)` 在聊天窗口正中创建浮层（不进入气泡）；
   - 浮层下方创建结果区，动画结束后填入合计/成败/目标值。
3. 渲染前读取本机 `diceAppearance`：颜色写入 CSS 变量/3D 材质，
   贴图皮肤解析为纹理 URL；缺失时回退颜色。
4. 按 `groups` 逐组渲染真实骰型（`d10` 就是 d10，不能统一成 d6）；
   依据 `selected` 为选中骰添加高亮，其余候选弱化。
5. 动画结束后收起浮层，并在聊天流追加**行内结果卡**（静态骰面 + 文本摘要）。
6. 历史消息只渲染行内结果卡，不创建浮层、不重播动画。
7. 生效档位为 `text` 或无桌面时，只输出骰娘文字与文本摘要。
8. 原有文本摘要保留。

### 7.3 玩家投掷渲染

1. 收到 `player_dice_request`：
   - `3D` / `简易`：骰娘气泡输出「等待玩家投掷」；`TrpgDiceOverlay.mount(plan)` 在
     聊天窗口正中摆好骰桌与对应骰型（不投掷）；
   - `text`：只插入骰娘文本「等待玩家投掷」，不创建浮层。
2. 仅目标玩家的投掷按钮可点；其他玩家在浮层/文本中看到等待状态。
   目标玩家的聊天输入区同时出现「投掷骰子」快捷按钮。
3. 点击按钮或输入 `/roll` → `POST roll` → 浮层内按当前档位播放动画 →
   结果展示在骰桌下方（纯文本直接展示文本结果）。
4. 重投与确认：
   - 重投关闭：动画结束 → `POST complete` → 收起浮层并追加行内结果卡；
   - 重投开启：结果显示后，浮层底部出现「重投（剩余 N/无限）」与「确定」；
     点击「重投」→ `POST reroll` → 清空上一轮骰面 → 重新播放动画与结果；
     点击「确定」→ `POST confirm` → `POST complete` → 收起浮层并追加行内结果卡；
   - 纯文本档位下同样在输入区/消息下方显示「重投」「确定」按钮。
5. 收到 `player_dice_rolled` / `player_dice_rerolled` → 各端同步到相同骰面与选中高亮；
   纯文本只更新文本/结果。
6. `cancelled/forced` → 更新等待状态；`/roll force` 由服务端自动结算并确认。
7. 刷新后通过 GET 恢复未完成浮层、按钮与剩余重投次数；已完成消息只展示行内结果卡。
8. 浮层关闭/收起后，输入区的「投掷骰子」按钮同步移除。

### 7.4 3D 骰子与物理实现（严格参考 `Matteas-Eden/dice-roller`）

参考项目使用 `three.js` + `cannon.js`，本实现采用 **`three`（MIT）+ `cannon-es`（MIT）**，
以获得持续维护的物理引擎；视觉与物理参数对齐参考项目：

**骰子设计（必须按骰型）**

显式几何映射，禁止任何默认回退：

| 骰型 | 几何 | 面数 | 面值 |
| --- | --- | --- | --- |
| d4 | 四面体 `TetrahedronGeometry` | 4 | 1–4 |
| d6 | 立方体 `BoxGeometry` | 6 | 1–6 |
| d8 | 八面体 `OctahedronGeometry` | 8 | 1–8 |
| d10 | 五角偏方面体（自定义顶点/面） | 10 | 0–9 |
| d12 | 十二面体 `DodecahedronGeometry` | 12 | 1–12 |
| d20 | 二十面体 `IcosahedronGeometry` | 20 | 1–20 |
| d100 | 两枚 d10（十位 + 个位） | 2×10 | 00–99 / 100 |

- `resolveGeometry(dieType)` 由 `switch` 穷举实现；未知类型 `throw new UnsupportedDiceError`，
  由降级控制器转简易动画，**绝不 `default: d6`**。
- 百分骰固定生成两枚 d10：十位骰与个位骰使用不同主色/边框标记，桌面上并排摆放，
  禁止用两枚 d6 表示 d100。
- 骰面编号：每个面绑定一个面值，生成独立的 `CanvasTexture`：
  - d6 绘制点阵（1–6 点）；
  - d4/d8/d10/d12/d20 绘制阿拉伯数字；
  - d100 的十位骰显示 `00/10/…/90`，个位骰显示 `0–9`。
- 数字方向：按面的法线与 UV 旋转，保证骰子落定后**从正上方看数字是正的**，不镜像、不侧躺。
- 每个骰面独立 `MeshStandardMaterial`；贴图皮肤存在时覆盖该面 Canvas，
  未绑定面回退“颜色 + 数字/点阵”。
- three.js 内置多面体不提供可用的逐面 UV：实现时按面拆分几何组（`geometry.groups` +
  材质数组）或为每个面建立独立面片，确保“一面一材质/一贴图”。
- 贴图加载失败时仅该面回退 Canvas，并记录 `texture_load_failed` 警告日志，
  不影响动画档位与其他骰子。
- 静止后禁止任何骰子操作：物理停稳后不旋转、不抬起、不缩放、不做落定补间；
  只读取当前朝上的面（面法线经 mesh 四元数旋转后 y 分量最大者），把服务端结果
  贴到该面的材质上，并还原上一张朝上面的贴图（`syncUpFaceLabels()`）。
- 多骰组同场：按 `groups` 分区摆放（十位区/个位区/普通骰区），组间留出间距，
  不同组可有不同主色。
- 选中高亮：`selected` 指向的骰子加外发光描边（`emissive` + 轮廓），
  其余候选骰 `opacity≈0.45`、去饱和；高亮在落定后出现，且只改发光与透明度，不位移。

**场景与镜头（垂直俯视）**

- 相机：`OrthographicCamera`，位置 `(0, H, 0)`，`up=(0,0,-1)`，`lookAt(0,0,0)`，
  即**从正上方 90° 垂直向下看**；不使用 45°–70° 斜俯视。
- 正交视野按桌面包围盒自适应，保证整张桌子与所有骰子入镜；窗口缩放时只改
  `left/right/top/bottom`，不改变角度。
- 若必须用透视相机，使用超长焦（`fov ≤ 12°`）并置于正上方，效果接近正交。
- 桌面为静态平面 + 不可见边界墙，防止骰子飞出；默认应用玩家选定的桌面色与桌沿色，
  选择桌面皮肤时在平面材质上叠加贴图（无贴图时回退纯色）；
- 灯光：顶部平行光（略微偏移以避免完全垂直的死影）+ 环境光/半球光，开启 PCFSoft 阴影；
  阴影方向与俯视镜头匹配，骰子与桌面均投影；
- 背景透明或使用与聊天主题一致的暗色渐变；桌面铺满取景框，四周保留少量边距。

**物理**

- `cannon-es` 世界固定步长 1/60s，`maxSubSteps=3`；
- 重力约 `-9.82 * scale`；骰子-桌面摩擦约 0.5、弹性约 0.3；
  骰子-骰子摩擦约 0.3、弹性约 0.4；
- 碰撞体：d6 用 `Box`；d4/d8/d10/d12/d20 用 `ConvexPolyhedron`（顶点与渲染网格同源）；
  d100 的两枚 d10 各自独立刚体，禁止用立方体碰撞体代替；
- 骰子视觉尺寸按面数归一化（外接球半径统一），避免 d4 过小、d20 过大；
- 投掷：随机初始位置（桌面圆内）、随机四元数、向下速度 + 横向冲量 + 随机角速度；
- 骰子之间、骰子与桌面之间全部由物理引擎求解，不使用手工位移伪造碰撞；
- 落定检测：线速度/角速度低于阈值并持续 18 帧（约 300ms）即视为静止，或超过 4 秒强制结束；
  静止后对刚体调用 `sleep()` 冻结，不再有任何后续求解与变换；
- 结果一致性：结果由服务端 RNG 决定；物理停稳后**不再旋转骰子**，而是读取当前朝上的面，
  把结果贴到该面材质上（`syncUpFaceLabels()`，仅在接近静止时与最终静止时调用）；
- 因此「动画结束时朝上的面」与下方结果恒等，且骰子落定后位置、朝向完全不再变化。

**动画收尾**

- 相机不抖动，避免晕动；
- 骰子落定后，在骰桌正下方的结果区显示合计与成败标签（不在骰面上叠字）；
- `prefers-reduced-motion` 时直接渲染静态结果；
- 多骰超过 20 枚（桌面）/ 10 枚（移动端）改用简易动画，避免物理开销。

### 7.5 简易动画

- 同在居中骰桌浮层内渲染，不使用 3D/WebGL：
  - 按 `groups` 渲染每个骰组，骰型用形状 + 面值标识（d6 点阵、其余数字）；
  - d100 固定渲染两枚 d10（十位/个位），十位使用强调色；
  - 圆角骰子卡片约 620ms 抖动，数字快速跳变；停止抖动后**不做落定位移/缩放动画**，原地替换为最终点数；
- 通过 CSS 变量应用玩家颜色；桌面使用所选桌面贴图，骰子定格时显示该面的贴图；
- 选中骰只加发光描边（不放大、不位移），未选中候选降低不透明度；
- 桌面布局与 3D 版本一致，结果区同样位于骰桌下方；
- 兼容低端设备与非常见面数；重投时重新播放同一套动画。

### 7.6 降级控制器

```text
render(plan, level):
  if level == "3d":
    try: await load3DBundle(); render3D()
    catch e: reportDegradation("3d","simple", e); return renderSimple()
  if level == "simple":
    try: renderSimple()
    catch e: reportDegradation("simple","text", e); return renderText()
  renderText()
```

- `reportDegradation()`：
  - `scope=session` 的全局失败只上报一次并本地记忆；
  - 单次失败每次上报（服务端限流）；
  - 上报失败仅 `console.warn`，不影响游戏。
- 3D 不可用后，本会话直接走简易；用户刷新或房间重进后可重试。

### 7.7 设置界面

**管理员（站点设置）**

- 动画档位三选一下拉；玩家投掷开关；超时数字；重投模式下拉（关闭/指定次数/无限）；
  重投次数数字（仅指定次数可编辑，1–20）。
- 项间相互独立，不做置灰联动；`text/simple/3d` 均可开启玩家投掷。
- 除重投次数随模式联动外，其余控件都进入待保存提示，确认后才写入。
- 新增「骰子贴图库」区块（`admin-dice-textures.ts`）：
  - 上传区：拖拽/选择文件，展示校验结果与失败原因；
  - 贴图列表：缩略图、名称、尺寸、启用开关、删除（被引用时提示先解除）；
  - 桌面皮肤：名称 + 选择贴图；
  - 骰子皮肤：选择骰型 → 逐面网格（d6 六格、d10 十格），每格选贴图或「无（使用颜色）」，
    提供一键清空与预览；
  - 上传/删除/保存均为显式操作，成功后原地刷新列表。
- 文案键：`settings.dice.animation_level`、`.level_text`、`.level_simple`、`.level_3d`、
  `.player_roll`、`.player_roll_timeout`、`.player_roll_hint`、
  `.reroll_mode`、`.reroll_mode_off`、`.reroll_mode_limited`、`.reroll_mode_unlimited`、
  `.reroll_limit`、`settings.dice.textures.*`。

**房间**

- 三档下拉（含跟随默认）；玩家投掷三态下拉；重投模式三态下拉（跟随默认/关闭/指定次数/无限）；
  重投次数数字（仅指定次数可编辑）。
- 各项互不依赖；重投不要求 3D 档位。
- 保存请求新增 `dice_animation_level`、`player_dice_roll`、`dice_reroll_mode`、`dice_reroll_limit`。
- 文案键：`room.house_rules.dice_animation_level`、`.player_dice_roll`、`.dice_reroll_mode`、
  `.dice_reroll_limit`、`.follow_default` 等。

**骰桌浮层按钮**

- 关闭重投：仅「投掷」（等待时）/ 无按钮（投掷后自动返回）。
- 开启重投：投掷后显示「重投（剩余 N / 无限）」+「确定」；次数用尽时重投置灰。
- 按钮仅在目标玩家界面可用；其他玩家看到只读状态与剩余次数。

**玩家个性化（用户设置 → 「骰子外观」标签页）**

- 位置：沿用现有用户设置弹窗，在「外观」标签内追加「骰子外观」分区（`dice-appearance-settings.ts`）。
- 颜色（四组选色板 `<input type="color">` + 预设色块）：
  - 骰子主体色、骰面数字/点阵色、桌面主体色、桌沿/边界色。
- 贴图：
  - 桌面皮肤下拉（含「无（纯色）」）；
  - 骰子皮肤按骰型分别选择（d4/d6/d8/d10/d12/d20/d100），含「无（纯色）」。
- 预览：一个小型静态骰子 + 桌面缩略图，实时反映颜色/贴图选择。
- 操作：保存（写入 `localStorage`）、恢复默认；仅本机生效，不发请求、不影响他人。
- 贴图库为空时隐藏对应下拉，仅保留颜色设置，并提示「管理员尚未上传贴图」。
- 文案键：`profile.dice_appearance.*`。

### 7.8 性能与历史

- 历史批量渲染全部静态；实时消息才播放动画。
- 同一 `data-id` 去重，避免 Socket 回显重复播放。
- 3D 渲染器在桌面销毁时 `dispose()` 几何/材质/渲染器，释放 WebGL 上下文。
- 窄屏缩放桌面尺寸；移动端降低骰子上限与像素比（`pixelRatio ≤ 2`）。
- 外观读取在页面初始化时完成一次并缓存，切换设置后仅更新内存副本与 CSS 变量，不重复请求贴图库。
- 贴图资源按 UUID 永久缓存；3D 侧复用 `THREE.Texture`，同一皮肤的多枚骰子共享纹理对象。
- 简易动画只使用 CSS 背景图；桌面贴图缺失或加载失败时回退纯色。
- 浮层单例：同一客户端只保留一个活动浮层，其余请求排队；浮层收起时解除事件监听与定时器。
- 几何与面纹理按骰型缓存（每种骰型只构建一次），重投时复用，仅重置物理刚体与动画状态。
- 3D 场景销毁时释放 `Geometry`、`Material`、`Texture` 与渲染器，避免 WebGL 上下文泄漏。

---

## 8. 关键时序

```mermaid
sequenceDiagram
    participant P as 触发AI的玩家
    participant S as 前端(目标玩家)
    participant R as 房间Socket
    participant B as 后端(chat请求线程)
    participant D as 骰子工具
    participant AI as 大模型

    P->>B: POST /api/chat
    B->>AI: 请求 KP
    AI->>D: check.roll_room_check(...) 或 dice.roll(player_roll=true)
    D->>D: 生效玩家投掷=开（与动画档位无关）
    D->>R: player_dice_request
    R-->>S: 骰娘文字「等待玩家投掷」
    alt 档位 = 3d / simple
        R-->>S: 聊天窗口正中挂载骰桌浮层(不投掷)
    else 档位 = text
        R-->>S: 输入区显示投掷按钮
    end
    Note over B: 工具线程阻塞，思考气泡显示等待文案
    S->>B: 点击按钮 / `/roll` → POST .../roll
    B->>D: resolver() 服务端 RNG
    D-->>B: 检定结果（groups + selected）
    B-->>S: 返回骰面
    B->>R: player_dice_rolled
    alt 档位 = 3d / simple
        S->>S: 浮层内播放动画（失败逐层降级并记日志）
    end
    alt 重投关闭
        S->>B: POST .../complete
    else 重投开启
        loop 玩家不满意且剩余次数 > 0
            S->>B: POST .../reroll
            B->>D: resolver() 重新 RNG
            B-->>S: 新骰面
            S->>S: 清空上一轮并重播动画
        end
        S->>B: POST .../confirm
        S->>B: POST .../complete
    end
    B->>D: 解除阻塞
    D-->>AI: 工具结果（最终结果）
    AI-->>B: KP 叙事
    B-->>P: 聊天内容 + tool_messages
    S->>S: 收起浮层，追加行内结果卡
```

强制投掷分支：管理员/房主/房间管理员输入 `/roll force` → 前端调用
`POST .../force` → 服务端跳过目标玩家校验，以自动投掷方式结算并确认 →
广播 `player_dice_forced` → 解除阻塞，等待中的浮层/文本直接显示结果。

自动投掷：工具同步返回 → 响应带回 `tool_messages` → 骰娘文字「正在召唤骰桌进行投掷…」
→ 在聊天窗口正中挂载浮层 → 渲染时读取本机外观偏好 → 按当前档位播放动画 →
收起浮层并追加行内结果卡。

`/dice --table`：前端本地 RNG → 骰娘文字「正在召唤骰桌进行投掷…」→ 挂载浮层 →
播放动画 → 结果显示在骰桌下方 → 收起并追加行内结果卡；不进入 AI 上下文。

外观解析（每次渲染前）：

```
appearance = readLocalAppearance()      // localStorage，失败则用内置默认
colors     = appearance.colors
tableSkin  = appearance.tableSkinId     // 贴图库中不存在 → 回退纯色
diceSkin   = appearance.diceSkinIds[dieType]
materials  = buildMaterials(colors, diceSkin, tableSkin)   // 逐面贴图 + 颜色回退
```

---

## 9. 接口契约

### 9.1 通用投掷参数

```json
{
  "expression": "2d6",
  "dark": false,
  "show_table": true,
  "player_roll": false
}
```

### 9.2 待投掷请求（Socket）

```json
{
  "room_id": "room-abc",
  "request_id": "roll-uuid",
  "tool_name": "dice.roll",
  "roller": { "user_id": 12, "username": "调查员A" },
  "check": null,
  "plan": {
    "mode": "standard",
    "expression": "2d6",
    "count": 2,
    "sides": 6,
    "bonus_dice": 0,
    "penalty_dice": 0,
    "groups": [
      { "role": "normal", "type": "d6", "count": 2, "labels": [] }
    ],
    "selected": null,
    "candidates": []
  },
  "reroll": { "mode": "limited", "limit": 3, "remaining": 3 },
  "expires_at": 1730000000.0
}
```

### 9.3 投掷结果响应

```json
{
  "success": true,
  "data": {
    "request_id": "roll-uuid",
    "faces": [3, 4],
    "labels": ["30", "4"],
    "groups": [
      { "role": "tens", "type": "d10", "count": 1, "faces": [3], "labels": ["30"] },
      { "role": "ones", "type": "d10", "count": 1, "faces": [4], "labels": ["4"] }
    ],
    "selected": { "group": 0, "index": 0 },
    "roll": 34,
    "rolls": [34],
    "total": 34,
    "success": false,
    "success_level": "failure",
    "summary": "困难侦察 d%: [34] = 34 / 22 失败",
    "state": "awaiting_confirm",
    "reroll": { "mode": "limited", "limit": 3, "used": 1, "remaining": 2 }
  }
}
```

### 9.4 降级日志

```json
{
  "request_id": "roll-uuid",
  "tool_name": "dice.roll",
  "from_level": "3d",
  "to_level": "simple",
  "reason": "webgl_unavailable",
  "error": "WebGL context creation failed",
  "scope": "session",
  "dice": { "count": 2, "sides": 6, "expression": "2d6" }
}
```

### 9.5 房间详情

```json
"dice_animation": {
  "level": "3d",
  "player_roll": true,
  "timeout": 120,
  "reroll_mode": "limited",
  "reroll_limit": 3,
  "level_source": "room",
  "player_roll_source": "admin",
  "reroll_source": "room"
}
```

### 9.6 `/roll` 指令

| 输入 | 权限 | 行为 |
| --- | --- | --- |
| `/roll` | 当前房间有未完成请求的目标玩家 | 首次投掷；已有结果且可重投时等价于「重投」 |
| `/roll confirm` | 目标玩家 | 确认最终结果（房间开启重投时必填步骤） |
| `/roll` | 非目标玩家 | 提示「本次投掷由 <玩家> 完成」，不发起请求 |
| `/roll` | 无待投掷请求 | 提示「当前没有等待中的投掷」 |
| `/roll force` | 站点管理员 / 房主 / 房间管理员 | 跳过目标玩家校验，立即结算并确认当前请求 |
| `/roll force` | 普通玩家 | 提示无权限，不发起请求 |

- 前端识别 `/roll` 时在 `toolManager.handleCommand` 之前拦截（与 `/record`、`/trigger` 同级），
  因为该命令需要异步请求。
- 命令不写入聊天记录；结果只通过浮层/文本与通知呈现。
- 输入区按钮与 `/roll` 调用同一个请求函数。
- 权限以后端校验为准，前端隐藏按钮只是体验优化。

### 9.7 `/dice --table`

| 输入 | 行为 |
| --- | --- |
| `/dice 2d6` | 保持现状：文本结果 |
| `/dice 2d6 --table` / `/dice 2d6 -t` | 居中浮层 + 当前档位动画 + 下方结果 |
| `/dice --table 1d100` | 两枚 d10 合成百分骰，浮层展示 |
| `/dice 4d6 --table` | 4 枚 d6 同场，全部显示 |

- 本地 RNG，不调用后端、不进入 AI 上下文。
- `text` 档位忽略 `--table`，输出文本并提示。
- 结束后的历史回看使用行内结果卡。

### 9.8 本机外观存储（`localStorage`）

键：`trpg_dice_appearance_v1`

```json
{
  "version": 1,
  "colors": {
    "diceBody": "#f4f1ea",
    "diceFace": "#1f2328",
    "tableBody": "#2f3b47",
    "tableEdge": "#1b232b"
  },
  "tableSkinId": null,
  "diceSkinIds": {
    "d4": null, "d6": "dice-skin-uuid", "d8": null, "d10": null,
    "d12": null, "d20": null, "d100": null
  }
}
```

- 读取失败、JSON 损坏、字段缺失或颜色非法时整体回退内置默认，不阻塞渲染。
- `tableSkinId` / `diceSkinIds` 指向的皮肤在贴图库中不存在或被禁用时视为 `null`。
- 不落库、不上传、不参与 Socket 同步；清空站点数据即恢复默认。

### 9.9 贴图库接口

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/api/dice-textures/library` | - | `{assets, table_skins, dice_skins}`（仅启用项） |
| POST | `/api/dice-textures/assets` | multipart：`file`、`name`、`kind` | 新 asset 对象 |
| DELETE | `/api/dice-textures/assets/<id>` | - | `{ok}`；被引用返回 409 |
| POST | `/api/dice-textures/table-skins` | JSON：`{id?, name, asset_id, enabled}` | 皮肤对象 |
| POST | `/api/dice-textures/dice-skins` | JSON：`{id?, name, die_type, faces, enabled}` | 皮肤对象 |
| DELETE | `/api/dice-textures/skins/<id>` | - | `{ok}` |
| GET | `/api/dice-textures/assets/<id>` | - | 图片二进制 |

- 上传/写入均要求管理员；读取要求登录。
- 校验失败返回 400 并给出 `reason`（`unsupported_type` / `too_large` / `bad_dimensions` / `bad_die_type` / `bad_face_key`）。
- `faces` 的键必须是该骰型的合法面值，值为 asset id 或 `null`。

---

## 10. 边界与异常

| 场景 | 处理 |
| --- | --- |
| 目标玩家离线/被移出 | 自动投掷并标注原因 |
| 同房间已有待投掷请求 | 新检定立即投掷 |
| 目标玩家重复点击 | 首次生效；重投关闭时后续 409，重投开启时视为重投 |
| 动画结束回调丢失 | 重投关闭时 5 秒后自动完成；重投开启时不自动完成 |
| 等待超时 | 自动确认最后一次结果并标注 |
| AI 请求超时 | 等待上限 `ai_request_timeout - 5` |
| 房间归档/删除 | `cleanup_room()` 结束等待 |
| 服务重启 | 内存请求丢失，前端提示重试 |
| 3D bundle/WebGL 失败 | 降级简易 + 房间日志（session 一次） |
| 单次 3D 渲染异常 | 本次降级简易 + 房间日志 |
| 简易动画异常 | 降级纯文本 + 房间日志 |
| **骰型解析失败** | 不使用 d6 冒充；记录 `unsupported_dice` 并降级简易，简易用文字标注骰型 |
| **几何构建异常** | 该骰组降级简易，房间日志记录 `geometry_error` 与骰型 |
| 不支持的骰型/过多骰子 | 主动降级简易，记录 `unsupported_dice` / `dice_count_limit` |
| `dark=true` + `show_table/player_roll` | 忽略参数，不召唤浮层 |
| `player_roll` 但模式关闭 | 即时投掷，metadata 标记，服务器 info 日志 |
| 纯文本档位 + 玩家投掷 | 输出「等待玩家投掷」，输入区出现投掷按钮，`/roll` 可用 |
| 非目标玩家点击/输入 `/roll` | 提示由目标玩家完成，不发起请求，记录房间日志 |
| `/roll` 无待投掷请求 | 提示无等待中的投掷，不报错 |
| `/roll force` 权限不足 | 403 提示，前端不下发请求 |
| `/roll force` 时请求已结束 | 409，提示已完成 |
| 重投次数用尽 | `reroll` 返回 409，按钮置灰，仍可确认 |
| 重投关闭时调用 `reroll` | 409，提示本房间未开启重投 |
| 重投请求在动画中到达 | 丢弃并重播，不叠加动画 |
| 未确认就关闭页面 | 结果在 `expires_at` 自动确认，AI 不会永久阻塞 |
| 浮层挂载点缺失 | 回退挂到 `body` 居中，记录前端 warning |
| 同时到达多个投掷事件 | 浮层单例排队，只展示队首 |
| 窄屏/移动端 | 浮层宽度取容器 92%，按钮换行，结果区随桌面缩放 |
| 减少动态效果偏好 | 直接静态结果（视为 `text` 渲染） |
| 历史消息 | 只渲染行内结果卡 |
| 贴图上传类型/体积/尺寸不合法 | 400 + `reason`，不写入索引，服务器日志记录失败原因 |
| 贴图文件缺失或加载失败 | 该面/桌面回退纯色，房间日志记录 `texture_load_failed` |
| 玩家选择被删除/禁用的皮肤 | 视为未选择，回退纯色，不提示错误 |
| 删除仍被皮肤引用的贴图 | 409，要求先解除引用 |
| 贴图库为空 | 个性化界面隐藏贴图下拉，仅保留颜色 |
| `localStorage` 不可用或数据损坏 | 静默使用内置默认外观，不影响投掷 |

---

## 11. 兼容性、依赖与许可

- 现有前端指令与工具名不变；通用投掷仅新增可选参数；`/dice` 仅新增可选 `--table`。
- 检定工具返回字段不变，`metadata` 只追加。
- 玩家投掷与重投默认关闭，升级后默认行为与当前版本一致（默认档位 3D，但普通投掷仍即时返回）。
- 未设置 `--table` 的 `/dice` 输出与旧版逐字符一致。
- 重投关闭时不显示重投/确定按钮，玩家投掷流程与旧版一致。
- 骰型几何不可用时不回退 d6：宁可降级简易，也不显示错误骰型。
- 新增依赖：
  - `three`（MIT）；
  - `cannon-es`（MIT）。
- `THIRD-PARTY-LICENSES.md`，记录版本与许可证；
  同时注明 3D 骰子视觉/物理参考自 `Matteas-Eden/dice-roller`（MIT，原作者 Anton Naratov）。
- 3D bundle 按需加载，不影响纯文本/简易模式的加载体积。
- 静态资源沿用现有缓存控制策略，bundle 带版本化查询参数。
- 管理端设置继续遵循「改动后提示保存、确认后才生效」。
- 贴图上传不新增 Python 依赖：沿用现有上传限额与文件头魔数校验，不引入 Pillow；
  尺寸校验从图片头（PNG IHDR / JPEG SOF / WebP VP8X）解析。
- 外观个性化只存于浏览器 `localStorage`，不改动房间数据、角色卡或用户账号，
  因此多设备不会同步；换浏览器/清缓存后回到默认外观。
- 未上传贴图时行为与当前版本一致（纯色骰子/桌面），不影响旧房间。
- `/roll` 与原有 `/dice`、`/check`、`/sc` 并存，不改变其语义。

---

## 12. 测试计划

### 12.1 后端

- `tests/test_dice_player_roll.py`：
  - 档位/玩家投掷各自独立生效（`text`/`simple`/`3d` × 开/关）；
  - 正常点击、重复点击、超时、取消、强制、离线回退；
  - `complete` 与 5 秒自动完成（仅重投关闭）；
  - 重投关闭/指定次数/无限三种模式的 `reroll → confirm` 流程、次数扣减与 409；
  - 重投不重置 `expires_at`，到期自动确认最后结果；
  - `/roll`（目标玩家）、`/roll confirm`、`/roll force`（管理员/房主/房间管理员/普通玩家）
    的权限与状态码；
  - 房间清理无残留线程。
- `tests/test_dice_tools.py`：
  - `show_table` / `player_roll` 参数在开启/关闭/暗投下的行为；
  - `dice_roll` metadata 字段（含 `groups` / `selected` / `reroll`）；
  - 检定工具在玩家投掷开启时阻塞并由 resolver 返回（含 `text` 档位）；
  - 百分骰 `groups` 为两枚 d10（十位/个位），奖励/惩罚的 `selected` 指向采纳骰；
  - 玩家投掷关闭时逐字段与旧行为一致。
- `tests/test_rooms_house_rules.py`：
  - `nullable_enum` / `nullable_boolean`；
  - 档位、玩家投掷、重投可任意组合（如 `text` + `true` + `unlimited`）均合法；
  - `dice_animation` 返回结构。
- `tests/test_dice_textures.py`：
  - 上传成功、魔数不符、超体积、尺寸越界；
  - d10 / d100 面值键 `0`–`9`，d6 为 `1`–`6`，非法面值被拒；
  - 删除被引用贴图返回 409；
  - 索引原子写入与损坏恢复；
  - 读取接口缓存头。
- `tests/test_ai_config.py`：三档默认值、重投默认值、非法值回落。
- `tests/test_room_log_degradation.py`：降级日志写入、限流、归档包含
  （含 `unsupported_dice` / `geometry_error`）。
- 控制台命令测试：管理员 `dice animation set` / `dice reroll`、房间 `dice player-roll on` /
  `dice reroll-limit` 的权限与日志（`text` 档位下 `player-roll on` 必须成功）。

### 12.2 前端

- `npm run typecheck`、`npm run build:frontend`（含 `build:dice`）通过。
- 浏览器验证：
  - 三档切换：纯文本无浮层、简易无 3D、3D 有物理动画；
  - **骰型正确性**：`1d6` 显示立方体点阵；`1d100` 显示两枚 d10（十位/个位）；
    `1d4/1d8/1d12/1d20` 分别显示对应多面体，绝不出现 d6 冒充；
  - **骰面可读**：落定后朝上面显示数字/点阵，且从屏幕方向看是正的；
  - **落定后静止**：3D 与简易档位在骰子停止后不再出现抬起、旋转、放大或落定位移，顶面点数与下方结果一致；
  - **镜头**：3D 画面为垂直 90° 俯视，不出现骰子侧面或斜视透视；
  - **浮层**：骰桌位于聊天窗口正中，DOM 上不是 `.message`/气泡的子节点；
    骰娘气泡先出现「正在召唤骰桌进行投掷…」，结果位于骰桌下方；
  - 浮层结束后折叠为行内结果卡，刷新后不重播；
  - `/dice 2d6 --table`、`/dice --table 1d100` 正常召桌；不带 `--table` 时输出与旧版一致；
  - 多骰组同场（百分骰 + 2d6）分区摆放，类型清晰可辨；
  - 奖励/惩罚骰：采纳骰发光/放大，未采纳候选弱化；
  - 3D bundle 加载失败 → 简易 + 房间日志；
  - 简易动画异常 → 纯文本 + 房间日志（可用测试开关模拟）；
  - 玩家投掷：仅目标玩家可点、动画结束且确认后 AI 才继续；
  - 纯文本档位：出现「等待玩家投掷」与输入区按钮，`/roll` 可完成投掷；
  - 重投关闭：无重投/确定按钮，动画结束自动返回；
  - 重投指定次数：按钮显示剩余次数，用尽置灰，确定后 AI 收到最终结果；
  - 无限重投：按钮始终可用；
  - 重投期间 AI 持续等待，确认后才继续；
  - `/roll force` 在有权限/无权限下的表现；
  - 玩家投掷开关在 `text` / `simple` 下同样生效；
  - 颜色选择实时反映在 3D 与简易动画，且只影响本机；
  - 管理员上传贴图 → 玩家可选择 → 逐面贴图正确显示，未绑定面回退颜色；
  - 贴图被删除后玩家侧回退纯色；
  - `dice.roll` 的 `show_table` / `player_roll` 参数；
  - 非常见面数、超量骰子走简易；
  - 历史消息不重复动画；窄屏布局正常。

### 12.3 全流程

- 执行 `scripts/verify.ps1`（如存在）。
- 检查 3D `dispose()` 后无 WebGL 上下文泄漏。
- 检查房间日志中降级记录的 `from/to/reason` 正确。
- 代码层检查 `resolveGeometry` 覆盖 7 种骰型且无 `default: d6`；
  相机配置满足 `position.x=0`、`position.z=0`、`up=(0,0,-1)`。

---

## 13. 实施顺序

1. **配置与三档开关**：`config.py`、`general.toml`、管理员设置页下拉、重投模式、
   待保存确认。
2. **房间规则**：`nullable_enum` / `nullable_boolean` / `nullable_integer`、
   `dice_animation`、重投设置、房间弹窗与校验。
3. **通用投掷参数与 metadata**：`show_table` / `player_roll`、`groups` / `selected`、
   去重绕过、百分骰与奖励骰的骰组构建。
4. **外观底座**：`diceAppearance`、`diceTypes`、`05-dice-animation.css` 的选色变量与默认值。
5. **居中浮层**：`diceOverlay`（挂载、遮罩、单例排队、收起与行内结果卡）。
6. **简易动画与降级骨架**：`diceSimple` / `diceDegradation` / `diceTable` + 房间日志接口，
   按骰组渲染真实骰型与选中高亮。
7. **玩家投掷后端**：`player_roll.py`（含重投状态、`reroll`/`confirm`）、
   `routes/dice_rolls.py`、Socket、工具拦截（不限档位）。
8. **玩家投掷前端**：浮层交互、纯文本「等待玩家投掷」、输入区按钮、`/roll` 与
   `/roll confirm`、`/roll force`、重投/确定按钮、刷新恢复。
9. **`/dice --table`**：`chat.ts` 参数解析、复用浮层与当前档位动画、行内结果卡。
10. **贴图库后端**：`dice_textures.py`、`routes/dice_textures.py`、上传校验、索引与引用检查。
11. **贴图库前端**：管理员贴图库与逐面皮肤编辑器、玩家「骰子外观」选择与预览。
12. **3D bundle**：three + cannon-es、`resolveGeometry` 逐型几何、逐面数字/点阵 Canvas、
    垂直 90° 正交相机、多骰组布局、选中高亮、物理与结果对齐、按需加载。
13. **控制台与文档**：管理员/房间命令（含重投）、API 文档、wiki、变更日志、许可证。

每一步单独提交，便于评审与回滚。

---

## 14. 文档与发布

- `docs/api.md`：新增骰子请求接口（含 `reroll`/`confirm`）、`dice_animation`、降级日志接口、
  通用投掷参数与 `groups/selected`、`/roll` / `/roll confirm` / `/roll force`、
  `/dice --table`、贴图库接口。
- `docs/console-commands.md`：新增管理员与房间 `dice` 命令（含重投与贴图库维护提示）。
- `docs/the-veil.wiki/`：更新骰子、房间规则、设置、房间控制台、玩家个性化（颜色/贴图）、
  `/roll` 与 `/dice --table`、重投机制相关页面。
- `release/changelog/v0.5.1b.md`：记录功能、时间与验证结果。
- `THIRD-PARTY-LICENSES.md`：新增 `three`、`cannon-es` 与参考项目署名。
- 新增参数必须同步进入管理员设置与本文档。

---

## 15. 验收标准

- [ ] 管理员可设置三档动画（默认 3D）、玩家投掷与重投默认值，改动需确认保存。
- [ ] 房间可用三态覆盖档位、玩家投掷与重投；各项互不依赖，`text` / `simple` 也能开启。
- [ ] 3D 档位下，骰娘提示「正在召唤骰桌进行投掷…」，骰桌以居中浮层出现，不包裹在气泡内。
- [ ] 骰桌浮层下方展示结果；结束后折叠为行内结果卡，刷新不重播。
- [ ] 骰型严格正确：d6 立方体点阵，d100 两枚 d10，d4/d8/d10/d12/d20 各自几何，无 d6 冒充。
- [ ] 骰面落定后朝上面显示对应数字/点阵，方向正确可读。
- [ ] 骰子静止后不再有任何抬起、旋转、放大或落定补间；只读取朝上的面，顶面点数与下方结果一致。
- [ ] 3D 镜头为垂直 90° 俯视，不出现斜 45° 视角或骰子侧面。
- [ ] 3D 失败自动降级简易，简易失败降级纯文本，每次降级写入房间日志。
- [ ] 通用 `dice.roll` 支持 `show_table` 与 `player_roll`，行为符合设置与暗投规则。
- [ ] 多骰组可同场（百分骰 + 其他骰型），采纳骰高亮、候选骰弱化。
- [ ] `/dice <expr> --table` 可召唤居中骰桌并播放当前档位动画；不带参数时行为不变。
- [ ] 玩家投掷开启后，AI 等待目标玩家点击或 `/roll`；重投关闭时动画结束即返回。
- [ ] 重投开启时，玩家可在次数内重投，点击「确定」后结果才返回 AI；关闭时无这两个按钮。
- [ ] 重投支持关闭/指定次数/无限三种模式，次数用尽后按钮置灰。
- [ ] 纯文本档位等待时输出「等待玩家投掷」，输入区出现投掷按钮，`/roll` 可用。
- [ ] `/roll force` 仅站点管理员、房主、房间管理员可用，且跳过目标玩家校验。
- [ ] 玩家可在用户设置中用选色板自定义骰子与桌面的四种颜色，仅影响本机显示。
- [ ] 管理员可上传桌面贴图与逐面骰子贴图；玩家可选择皮肤，未绑定面回退颜色。
- [ ] 贴图删除/禁用后玩家侧自动回退纯色，不报错。
- [ ] 旧指令、工具、返回字段兼容；历史消息不重复动画。
- [ ] API 文档、wiki、房间/管理员控制台命令、变更日志、许可证全部更新。
- [ ] 新增测试与构建检查全部通过。

---

## 16. 待确认事项（已给出推荐默认值）

| # | 事项 | 推荐方案 |
| --- | --- | --- |
| 1 | 玩家投掷全局默认 | **关闭**，保持升级后普通投掷即时返回 |
| 2 | 玩家投掷与档位 | 完全独立，`text`/`simple`/`3d` 均可开启；运行时降级不中断已开始的投掷 |
| 3 | 通用投掷参数名 | `show_table`、`player_roll`（均为可选布尔值，对 AI 开放） |
| 4 | 3D 技术栈 | `three` + `cannon-es`（MIT），按需加载，严格对齐参考项目的物理与镜头 |
| 5 | 结果与物理的关系 | **默认结果来自物理骰面**（v10）：服务端只给 seed，客户端读取落定后朝上的面并上报，服务端做百分骰与奖励/惩罚结算；管理员可关闭开关回退服务端 RNG |
| 6 | 降级记录粒度 | 全局失败按会话记录一次；单次失败每次记录，房间级限流 30 条/分钟 |
| 7 | AI 是否等动画播完 | **是**；重投关闭时 `roll → 动画 → complete`，开启时 `roll → (reroll) → confirm → complete` |
| 8 | 玩家离线 | 立即自动投掷并标注原因 |
| 9 | `/roll` 权限 | 默认仅目标玩家；`force` 子参数开放给站点管理员、房主、房间管理员 |
| 10 | 个性化存储范围 | 仅浏览器 `localStorage`，不落库、不跨设备同步 |
| 11 | 贴图上传限制 | PNG/JPEG/WebP，≤ 3 MB，128–2048 px，魔数校验 |
| 12 | 骰子贴图粒度 | 逐面绑定；d10/d100 面值 `0`–`9`，其余 `1`–面数；缺失面回退颜色 |
| 13 | 贴图库维护方式 | 仅网页管理员界面，不新增控制台子命令 |
| 14 | 重投配置编码 | `dice_reroll_mode`（`off`/`limited`/`unlimited`）+ `dice_reroll_limit`（1–20）；房间 `null` 继承 |
| 15 | 重投等待与超时 | 重投期间 AI 持续等待；`expires_at` 不变，到期自动确认最后结果 |
| 16 | 骰桌浮层与历史 | 实时用居中浮层；结束后折叠为行内结果卡，历史只显示结果卡 |
| 17 | 镜头类型 | `OrthographicCamera` 正上方 90°，超长焦透视仅作兼容兜底 |
| 18 | 同屏多投掷 | 单浮层排队，不并行展示多个骰桌 |

---

## 17. v10：物理骰面结果生成器

### 17.1 设计原则

- 骰子结果**就是**骰子落定后朝上的面，不再由服务端随机数决定后贴到骰面。
- 服务端只负责「掷骰种子 + 结算」；点数的产生发生在客户端真实物理模拟结束之后。
- 所有在线客户端必须看到同一段动画与同一个结果；只有投掷者上报骰面。
- 旧生成器完整保留，用于纯文本、暗投、离线、强制投掷与超时兜底，由管理员开关切换。

### 17.2 管理员开关

| 配置 | 管理员设置项 | 默认 | 行为 |
| --- | --- | --- | --- |
| `[ai] dice_physics_results` | 骰娘设置 → 启用现代化骰子点数生成器 | `true` | 开启：服务端只生成 seed，结果由物理骰面决定 |
| 同一项关闭 | 同上 | — | 关闭：沿用服务端随机数生成器，动画只负责表现 |

`resolve_dice_settings()` 输出 `physics_results` 与 `result_source`（`physics` / `server`），
经 `room.house_rules.dice_animation` 与 `chat` 的 `tool_state.dice_physics_results` 下发给前后端。

### 17.3 数据流

1. AI 调用骰子工具 → 服务端生成投掷计划与 `throw_seed`（32 位无符号）。
2. 广播 `player_dice_request`（含 `physics` / `auto` / `seed` / `state`），需要玩家点击时等待；
   物理模式下若不需要点击则自动调用 `begin`。
3. `POST .../begin`（重投为 `{reroll:true}`）→ 广播 `player_dice_rolling`（含 seed 与 `reroll_index`）。
4. 各客户端用同一 seed 与固定步长推演物理，落定后由投掷者读取朝上的原始骰面。
5. 投掷者 `POST .../faces {faces:[...]}` → 服务端结算 → 广播 `player_dice_rolled`（重投为 `player_dice_rerolled`），
   带 `fallback`、真实 `faces` 与最终结果；客户端只更新下方结果，不再改动骰面。
6. 本地 `/dice <表达式> --table` 使用同一原则：本地生成 seed 播放动画，落定后读取朝上的面并在本地结算，
   不经过 AI 或服务端；不再预先生成结果后贴到骰面（v10 修复画面与结果不一致）。

### 17.4 骰面到点数的规则

- 普通多面骰：取法线经骰子四元数旋转后 y 分量最大的面；`d4` 静止无朝上面，取朝下的面。
- `d10`：面值为 `0`–`9`；普通角色 `d10` 映射为 `1`–`10`，百分骰候选十位按 `×10` 解释。
- `d100`：两枚 `d10` 分别作为十位与个位；十位重复只保留一次，不足时按 `0`–`9` 补齐；`00 + 0` 记为 `100`。
- 奖励骰取候选最小值，惩罚骰取候选最大值；选中骰只在结算结果中标注 `selected`，不改变骰面。
- 服务端对上报面值做范围校验，非法面值返回 400；数量不足或多余同样拒绝。

### 17.5 兜底与降级

| 场景 | 结果来源 | 表现 |
| --- | --- | --- |
| 管理员关闭开关 | 服务端 RNG | 动画只表现，`fallback=true` |
| `text` 档位 / 无人可渲染 | 服务端 RNG | 纯文本结果 |
| 暗投 | 服务端 RNG | 不展示骰桌 |
| 目标玩家离线 / 强制投掷 | 服务端 RNG | 静态结果 |
| 等待超时 | 服务端 RNG | 静态结果并记录超时 |

兜底路径不会回填骰面贴图，避免再次出现「落定后数字变化」。

### 17.6 验收补充

- [ ] 物理模式下骰子静止后数字不再变化，结果等于朝上的面。
- [ ] 百分骰十位/个位、奖励/惩罚、重复十位去重与 100 的规则正确。
- [ ] 投掷者与观看者播放同一段 seed 动画，观看者不重复上报。
- [ ] 管理员开关默认开启，关闭后行为与旧版一致。
- [ ] 纯文本、暗投、离线、强制与超时全部走旧生成器且带 `fallback` 标记。
- [ ] 落定后不对骰子做任何旋转、抬起、缩放或贴图替换。
