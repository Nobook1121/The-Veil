# 控制台命令

服务器启动后，监听窗口会显示 `>` 提示符。网站管理员可以在该窗口输入站点管理命令。

## 语法

- 命令按空格分隔参数。
- 参数包含空格时使用英文双引号，例如 `"user name"`。
- 输入 `help` 查看当前可用命令。

## 破坏性命令与确认机制

涉及**删除 / 覆盖 / 清空**的命令（`user delete`、`scenario delete`、`scenario reindex`、
`vector prune`、`knowledge reindex`、`room archive`、`room prune`、`ai cache clear`、
`ai usage reset`、`backup create`、`owner transfer`）统一遵循同一套保护约定：

1. **缺省为「预演」**：直接执行时只打印「将发生的变更」，**不改动任何数据**；
2. **交互确认**：若当前是交互式控制台，会弹出 `是否执行(y/N)` 提示，输入 `y` 才真正执行；
3. **`--yes` / `-y`**：跳过提示直接执行，适合脚本化批处理。

> 房间控制台的所有命令域统一使用纯词参数，预演确认写 `yes`（旧 `--yes` 仍兼容），
> 详见下方「房间内嵌控制台」。

```text
user delete alice            # 预演：只打印将要删除什么
user delete alice --yes      # 直接执行
vector prune                 # 预演：列出将被清理的孤儿向量
vector prune --yes           # 直接执行
```

## 用户权限

```text
user promote <username-or-id> [USER|ADMIN|OWNER]
```

将指定用户调整为目标权限。省略目标权限时默认提升为 `ADMIN`。

示例：

```text
user promote alice
user promote alice ADMIN
user promote 1 OWNER
```

也可使用 `user demote <username-or-id> [USER|ADMIN]` 降级，省略目标权限时默认降为 `USER`。

## 强制用户修改密码

```text
user reset-password <username-or-id>
```

将指定用户的密码重置为一个随机临时密码，并把账号标记为「必须修改密码」：

- 该用户所有活跃登录会话会被立即撤销；
- 用户需使用打印到控制台的临时密码重新登录；
- 登录后会被强制要求设置新密码，修改成功前无法继续使用其他功能。

示例：

```text
user reset-password alice
user reset-password 1
```

执行后控制台会输出形如 `Temporary password for alice: Xxxx1234 ...` 的临时密码，请妥善转交给用户。

## AI 用量与成本

```text
ai usage [days]
```

汇总最近 N 天（默认 1 天，范围 1-90）的 AI 用量，并按角色与输入 token 分段打印，用于判断成本花在哪里：

- **总览**：请求次数、输入 token、输出 token、总计，以及平均缓存命中率；
- **按角色（role）**：KP、摘要等不同角色的用量分布；
- **输入分段**：把输入 token 粗估拆分为规则、剧本静态层、场景静态层、知识库、房间状态、历史、玩家输入等段，便于定位「静态前缀占了多大部分」这类问题。

分段 token 为本地粗估（CJK 约 1 token/字，其余约 1/4 字符），真实计费仍以上游 `usage` 为准。

示例：

```text
ai usage
ai usage 7
```

## 探测上游前缀缓存

```text
ai cache_probe [platform-id]
```

把同一段足够长的 payload **连发两次**到当前启用的 AI 平台，比较第二次响应 `usage`
里是否出现缓存命中 token，用来判断这个平台的前缀缓存是否**可观测**。

- 省略 `platform-id` 时使用「加载顺序最靠前」的已启用平台；
- 指定 `platform-id`（配置文件主名，如 `aliyun`、`inkstone`）时探测该平台。

结论有三种：

- **命中**：第二次 `cached_tokens > 0` —— 平台支持且确实复用了前缀缓存；
- **未命中**：平台回报了 `cached_tokens` 字段但为 0 —— 说明该前缀本轮没被复用；
- **无法判定**：`usage` 里根本没有 `cached_tokens` 字段 —— 该平台不回报缓存信息，
  界面上的「0% 命中率」属于正常现象，不能据此判断缓存是否生效。

示例：

```text
ai cache_probe
ai cache_probe aliyun
```

输出形如：

```text
AI 前缀缓存探针:
  平台: aliyun · 模型: qwen-max
  端点: https://.../v1/chat/completions
  第一次: HTTP 200 · prompt_tokens=1204 · cached_tokens=未回报
  第二次: HTTP 200 · prompt_tokens=1204 · cached_tokens=未回报
  结论: 平台未在 usage 中回报 cached_tokens，无法判定（该平台的前缀缓存不可观测）
```

## 剧本管理

### 列出剧本

```text
scenario list [search]
```

列出全部剧本（可按 ID/标题模糊过滤），显示 ID、标题、所有者、版本、向量数与是否已归档。

```text
scenario list
scenario list 灯塔
```

### 更改剧本所有者

```text
scenario owner <scenario-id> <username-or-id>
```

改写剧本记录的 `owner_id`（决定谁有编辑权限，包括世界书字段）。用户可用用户名或数字 ID 指定。

```text
scenario owner 12 alice
scenario owner 12 3
```

### 删除剧本

```text
scenario delete <scenario-id> [--vectors-only] [--force]
```

默认行为与网页一致：**仍有房间占用时归档**，否则删除剧本本体 + 知识库（向量与索引）+ 封面。

| 参数 | 作用 |
|---|---|
| （无） | 默认：有房间占用 → 归档；无占用 → 删除整个剧本 |
| `--vectors-only` | 只删向量库与知识索引，**保留剧本本体** |
| `--force` | 即使仍有房间占用，也**强制删除剧本本体** |

`--vectors-only` 与 `--force` 不能同时使用。

```text
scenario delete 12
scenario delete 12 --vectors-only
scenario delete 12 --force
```

以上操作都会写入用户操作日志（操作者记为「控制台」）。

### 校验剧本

```text
scenario validate <scenario-id>
```

只读校验剧本 JSON / 知识索引 / 向量库三者是否一致：索引分块数与向量条数是否对上、
索引文件是否存在、JSON 能否解析。输出 `三者一致 ✅` 或列出具体问题。

```text
scenario validate 12
```

### 重建剧本知识索引

```text
scenario reindex <scenario-id> [--yes]
```

重建某剧本的知识索引与向量。优先复用磁盘上已有的索引分块（为**直接导入、无场景卡**的
剧本保留分块），仅当索引缺失时才从场景卡 `modules` 重建，**不会把已导入的知识覆盖为空**。
会调用 embedding，可能较慢。

### 世界树 / 图谱（narrative）

`narrative` 命令组只读或重建剧本目录下的 `narrative.json`，不会改动 `scenario.json`。

```text
narrative status [<scenario-id>] [--json]                查看单个或全部剧本的生成状态
narrative validate <scenario-id> [--simulate] [--json]   拓扑分档校验；--simulate 追加乱序模拟
narrative topology <scenario-id> [--json]                查看拓扑判定、置信度与证据
narrative build <scenario-id>|--all [--yes] [--json]     生成 / 重建 narrative.json
narrative opening <room-id> [--publish|--repair] [--yes] [--json]  预览 / 发布开局
narrative opening set <scenario-id> --location <node-id> [--mode fixed|model|hybrid] [--yes]
narrative topology <scenario-id> --set <linear|hub|sandbox|hybrid> [--yes]
narrative rebuild-index <scenario-id> [--json]           重建派生索引（仅内存，不写盘）
narrative diff <scenario-id> <from-version> <to-version> [--json]  对比两版世界树
narrative backfill [--yes] [--json]                      为存量剧本回填 bundle；缺省预演
narrative media <scenario-id> [--missing] [--json]       列出媒体资产与缺失项
narrative media verify <scenario-id> [--fix-thumbs] [--yes] [--json]
narrative visibility <scenario-id> [--review|--auto] [--yes] [--json]
narrative stats [--json]                                 全站世界树规模、质量总览与房间指标（按 profile 分组）
```

- `status`：显示拓扑、节点/边数量、入口、`build_hash`、不可达地点与孤立资产；省略 ID 时列出全部剧本。
- `validate`：运行通用规则包 + 当前拓扑包，输出 `error/warn` 与修复建议；`--simulate` 额外检测隐式顺序依赖、自锁门控与不可达结局。
- `topology`：输出自动判定结果、存储值、边类型统计与可达地点数量；带 `--set` 时为人工锁定分档（需确认，`source` 记为 `author`）。
- `build`：默认**预演**（只打印结果、不写盘）；加 `--yes` 或在交互确认时输入 `y` 才真正写入 `narrative.json`。`--all` 批量处理所有剧本。
- `opening`：默认只预览开局包（起始地点、开场白、附件、可见出口、触发器的数量），不写盘；`--publish` 重置房间运行态并把开场消息写入 `messages.json`，`--repair` 只补发开场、不重置已有进度。
- `opening set`：直接修改 `narrative.json` 的起始地点与开场模式（需确认）。
- `rebuild-index`：重建内存派生索引并失效缓存，只读取、不写盘。
- `diff`：按剧本版本号分别构建 bundle 后对比节点/边的增删改；版本不存在时返回失败。
- `backfill`：给缺少 `narrative.json` 的存量剧本生成只读 bundle；默认预演，`--yes` 才写盘。
- `media` / `media verify`：列出 `assets.json` 内的媒体资产，标出类型、MIME、本地文件、缩略图与 poster 缺失；`--fix-thumbs` 可补生成派生文件。
- `visibility`：按确定性规则给出三态可见性判定与复核标记；`--review` 只看需要复核的项，`--auto` 在确认后写回自动项的可见性。
- `stats`：汇总全部剧本的节点/边规模、校验错误与拓扑分布。
- 所有子命令支持 `--json`，便于脚本与前端消费。

```text
narrative status 12
narrative validate 12 --simulate
narrative topology 12 --json
narrative topology 12 --set hybrid --yes
narrative build 12 --yes
narrative build --all --yes
narrative diff 12 1 2
narrative media verify 12 --fix-thumbs --yes
narrative visibility 12 --review
narrative stats
```

## 用户管理

```text
user list [search]                 列出用户（ID/用户名/角色/状态/邮箱/最近登录）
user ban <username-or-id>          封禁账号（状态 -> banned，无法登录）
user unban <username-or-id>        解封账号（状态 -> active）
user sessions revoke <username-or-id>   撤销该用户全部会话，强制下线
user create <username> [角色]      本地建号，返回一次性临时密码（角色缺省 USER）
user delete <username-or-id> [--force]  删除账号（破坏性，需确认）
```

- `user create` 生成的临时密码只打印一次，请立即转交用户并提醒修改；
- `user delete` 默认拒绝删除**系统 OWNER** 或**仍持有剧本**的用户；加 `--force` 可强制删除；
  该用户的会话、资料、审计关联会被级联清理。

## AI 平台与缓存

### 平台管理

```text
ai platform list                        列出平台：ID/启用状态/顺序/模型
ai platform test [id]                   连通性冒烟测试（省略 id 时要求当前只有一个平台）
ai platform enable <id> / disable <id>  启用 / 停用平台
ai platform detect-responses [id]       探测是否支持 Responses API
```

### 本地缓存与用量

```text
ai cache stats                  本地前缀/精确/语义缓存的存活条数与命中次数
ai cache clear [--yes]          清空本地缓存（破坏性，需确认）
ai usage reset <all|agent-id> [--yes]   删除当天的用量记录（all 为全部）
```

### 角色卡 AI 生成自测

```text
ai character generate <描述> [--scenario <id>] [--json]
```

在服务器控制台直接触发一次角色卡 AI 生成，用于自测与排障。它复用 Web 端
`/api/characters/generate` 完全相同的生成流水线（平台解析、提示词拼装、请求、
解析、日志与 AI 用量记录），因此控制台观察到的行为与线上一致。

- `<描述>`：角色描述文本，可省略（省略时依据剧本或自由发挥）；
- `--scenario <id>`：可选，绑定某个剧本，让 AI 参考该剧本的角色要求；
- `--json`：以 JSON 打印完整生成结果（含 `finish_reason`、正文字数、思考字数），
  便于排查「模型返回内容为空」这类问题。

示例：

```text
ai character generate 一位在旧金山经营古董店的华人女性，胆小却博学
ai character generate --scenario 1 一位退役军医
ai character generate 自由发挥一位调查员 --json
```

> 生成失败会返回带分类的错误（如 `platform` / `timeout` / `connection` /
> `empty_content`），其中 `empty_content` 通常意味着推理模型的思考 token 占满了
> `max_tokens` 输出预算，导致正文为空，可重试，或在管理员设置页把「最大输出 Token（全局）」
> 调大（默认已开启、默认 16384）。

### 自动重试控制

所有发起 AI 请求的功能（KP 对话、角色卡生成、模块摘要、剧本导入、平台连通性测试等）
共用同一套「日志 + 报错 + 在总预算内自动重试」的封装。当上游持续抖动、希望立即停止
等待时，可在控制台终止自动重试：

```text
ai abort     立即终止进行中的 AI 自动重试（下一次重试前生效）
ai resume    解除终止状态，恢复自动重试（别名 ai retry）
```

被终止的请求会以「已被手动终止自动重试」的错误返回，并在日志中记录。

## 向量库与知识库

```text
vector health                   向量库后端 / 向量总数 / embedding / OCR 状态
vector prune [--yes]            清理「孤儿向量」：库中 scenario_id 已不存在于剧本目录
knowledge list [search]         列出规则集，以及各剧本的知识索引块数与向量数
knowledge reindex [ruleset] [--yes]     重建规则集索引，省略 ruleset 时重建全部
```

说明：剧本被删除时控制台/网页**不会**自动清理其向量条目，这些残留即「孤儿向量」，
可用 `vector prune` 发现并清理；后端不支持枚举 `scenario_id`（如 Qdrant）时会明确提示无法判定。

## 语音配音（voice）

站点级命令默认关闭语音；`system`（浏览器 Web Speech）始终可用，`local` 需要先安装
Kokoro v1.1-zh 权重并启用，`cloud` 仍为占位。本地合成走独立回环子服务，异常时自动降级到 `system`。

```text
voice status [--json]                     查看站点开关、引擎健康、配额、个人覆盖与云端占位（密钥掩码）
voice on|off                              开启/关闭站点语音总开关
voice engine <system|local|cloud>         设置默认引擎（cloud 仍为占位）
voice engine allowed <a,b,c>              设置引擎白名单（逗号分隔）
voice profile list                        列出 8 个 NPC 人设 + 1 个旁白（含当前绑定的真实说话人）
voice map list --room <room-id>           查看房间 NPC 音色映射与静音名单
voice map set <speaker> <profile> --room <room-id> [--yes]
                                          绑定房间 NPC 音色；缺省预演，--yes 才落盘
voice test [--json]                       引擎健康检查（不生成音频、不请求外部服务）
voice usage [--room <room-id>]            最近 voice_* 事件统计与房间小时用量
voice local status [--json]               本地模型安装状态、子服务健康与 9 个人设的说话人绑定
voice local install [--yes] [--force]     下载并校验本地模型权重；默认预演
voice local remove [--yes]                移除本地模型权重；默认预演
voice local model <模型ID> [--yes]        切换本地模型 ID
voice local base-url <URL> [--yes]        指定外部托管的子服务地址（留空恢复自动托管）
voice local concurrency <1-8> [--yes]     设置合成并发（默认 1）
voice local timeout <5-180> [--yes]       设置单句合成超时秒数（默认 30）
voice cache stats [--room <room-id>]     本房间语音缓存条数/字节/命中次数（只读）
voice cache clear [--room <room-id>] [--yes]   清空本房间语音缓存；默认预演
voice cache prune [--room <room-id>] [--yes]   按配额 LRU 裁剪本房间缓存；默认预演
```

模型权重与许可证文本落在 `data/runtime/voice/models/<模型>/`，不会进入仓库或发布包。
安装/卸载/切换引擎仅站点管理员可执行；房间只能查看状态并清理本房间缓存。

配置写入 `general.toml` 的 `[immersive]` 段，使用 tomlkit 往返保留注释。房间只能收紧：
`voice on` 无法越过站点总开关；字符上限、小时上限只能调低；ducking 级别只能调高。

## 房间管理

```text
room list [search]              列出房间：ID/名称/房间码/模式/剧本/成员数/是否归档
room members <room-id>          查看房间成员（账号角色 + 房间角色）
room archive <room-id> [--yes]  归档房间（有剧本时要求已到达结局；无剧本房间可直接归档）
room mode <room-id> <scripted|freeform|manual> [--yes]
                                迁移房间模式（缺省预演；freeform 会清空剧本绑定与场景指针）
room prune [--days N] [--yes]   清理僵尸房间：未归档、无有效成员、且 >N 天（默认 30）无活动
```

`room archive` 会生成与网页一致的不可变归档快照（需要运行体注入的 Flask 上下文；
若未注入则只落盘归档标记）。

`room mode` 是房间模式唯一的变更入口（创建后默认锁定）。三种模式对应两个能力位：

- `scripted`：AI KP 开启、剧本必填（等同于旧房间）；
- `freeform`：AI KP 开启、禁止绑定剧本；
- `manual`：AI KP 关闭、剧本可选（保留的剧本只作玩家自查资料）。

### 骰子动画与贴图

```text
dice animation show                             查看管理员默认骰子动画档位
dice animation set <text|simple|3d> [--yes]      设置默认动画档位
dice player-roll show                            查看默认玩家投掷开关
dice player-roll <on|off> [--yes]                 设置默认玩家投掷开关
dice reroll show                                 查看默认重投模式与上限
dice reroll <off|limited|unlimited> [--yes]      设置默认重投模式
dice reroll-limit <1-20> [--yes]                 设置「指定次数」模式的上限
dice timeout <30-300> [--yes]                    设置等待玩家投掷的超时秒数
dice settings [room-id]                          管理员默认与房间生效值概览
dice pending [room-id]                           列出等待玩家投掷的请求（可按房间过滤）
dice textures                                    贴图资产、桌面皮肤与骰子皮肤概览
dice skin delete <skin-id> [--yes]               删除骰子皮肤（破坏性，缺省预演）
```

## 房间内嵌控制台（网页内，非服务器终端）

除服务器启动窗口的站点控制台外，每个房间在**网页内**还有一个只显示本房间日志的控制台
（房间详情页的「房间控制台」卡片）。它与服务器控制台相互独立：

- 房间事件（创建/加入房间、刷新邀请链接、绑定角色卡、发送房间消息、记录状态变化、自动存档……）
  默认只写入房间目录下的 `console.jsonl`，**不再刷服务器启动窗口**，用于消除日志噪音；
- 房间日志随房间目录一起保留，并在**房间归档时一并归档**（归档快照中的 `console_log` 字段）；
- 每次投掷与检定（`dice.roll` / `dice.roll_coc_check` / `check.roll_room_check` / 理智检定）
  的结果都会以 `source=dice` 写入房间日志，包含骰值、目标、难度与成功等级；暗投会额外标记
  「暗投=是」，方便事后核对判定过程；
- 房间控制台只能执行**房间范围内**的命令，任何站点级命令（`user` / `shutdown` / `scenario` /
  `backup` / `owner` …）都会被拒绝，影响范围严格限于该房间；
- 房间控制台只对网站管理员/所有者与房间所有者/房间管理员显示，普通房间成员既看不到面板，也无法
  通过接口读取；执行权限同样默认限「房间所有者 / 管理员」。可在「设置 → 通用设置」的
  「房间控制台」区块调整：启用开关、是否同时镜像到服务器控制台、房间日志条数上限。

网页房间控制台可用的命令（仅作用于本房间）：

```text
help                        （显示本帮助）
info                        （房间信息：房间码/模式/剧本/成员数）
members                     （成员及其房间角色）
members kick <user> [yes] （将成员移出本房间，破坏性）
members promote <user> [yes]（将成员提升为房间管理员，破坏性）
state                       （房间状态：场景/线索/任务/触发器）
event add <kind> <content>  （记录一条房间事件）
log tail [n]                （查看本房间最近 n 条日志，默认 50）
log clear [yes]           （清空本房间日志，破坏性）
autosave                    （立即为本房间保存一次自动存档）
archive [yes]             （归档本房间；有剧本时需已到达结局，无剧本可直接归档）
dice                        （本房间骰子动画档位、玩家投掷、重投配置与待投掷请求）
dice animation show         （查看本房间生效的动画档位）
dice animation set <text|simple|3d|inherit> [yes]（设置动画档位，inherit 跟随管理员默认）
dice player-roll show       （查看本房间生效的玩家投掷开关）
dice player-roll <on|off|inherit> [yes]（设置玩家投掷开关）
dice reroll show            （查看本房间生效的重投模式与上限）
dice reroll <off|limited|unlimited|inherit> [yes]（设置重投模式）
dice reroll-limit <1-20|inherit> [yes]（设置「指定次数」模式的上限）
dice pending                （列出本房间等待玩家投掷的请求）
dice cancel [request-id] [yes]（取消待投掷请求，缺省取消最早一条）
narrative                   （等同 narrative status：能力档、拓扑、位置与护栏）
narrative status            （世界树开关、能力档、拓扑、版本、位置、回合与指标）
narrative state [json]    （位置、旗标、时钟、地点状态、线索与触发记录）
narrative map [depth N]   （当前可达地图，标记已访问/未揭示/门控）
narrative exits             （当前地点出口与缺失条件）
narrative ledger [limit N] [key-only] [search <text>]（账本事件）
narrative facts [entity <id>] [status <s>]（事实三元组）
narrative clocks            （全局时钟与阈值）
narrative triggers [location <id>] [object <id>] [fired]（触发器与触发次数）
narrative trigger fire <id> [yes]（手动触发触发器，破坏性）
narrative media [missing] （本房间已发媒体与缺失素材）
narrative opening [publish|repair] [yes]（预览/补发开场）
narrative trace [turn N] [limit N]（JEV/检索/护栏轨迹）
narrative summary [rebuild] [yes]（查看或重建摘要）
narrative reset <location-id> [yes]（重置队伍位置，破坏性）
narrative unlock <edge|location> [yes]（手动跳过条件门控，破坏性）
narrative guard <off|shadow|enforce> [yes]（切换本房间护栏模式）
narrative profile <graph|degraded> [yes]（切换本房间能力档）
narrative rebuild-state [yes]（从关键帧+账本重放恢复运行态，破坏性）
immersive                   （等同 immersive status：本房间配乐/配图状态与历史）
immersive status            （本房间生效的开关、当前档位/段落与最近配图）
immersive music show|on|off [yes]（查看或收紧本房间配乐开关；show 同时显示生效音乐来源）
immersive music auto <on|off> [yes]（本房间是否随剧情自动切歌）
immersive music set <profile> <intensity> [yes]（手动切到指定档位/强度）
immersive illustration show|on|off [yes]（查看或收紧本房间配图开关）
immersive illustration auto <on|off> [yes]（本房间是否随剧情自动配图）
immersive illustration trigger show（查看生效配图触发档位与来源）
immersive illustration trigger <inherit|important|off> [yes]（房间收紧配图档位，只能更紧）
immersive illustration now [场景描述] [yes]（立即生成本房间配图，可能产生费用）
immersive cache show|stats  （只读：本房间缓存 + 本剧本缓存条数/体积/命中次数）
immersive cache room show  （本房间缓存条数、文件数、体积、累计命中与目录）
immersive cache room clear [yes]（清空本房间目录缓存，默认预演）
immersive cache record|prefer <on|off|inherit> [yes]（房间级收紧剧本缓存，不能越过站点开关）
immersive cache prune [yes]（按单房间配额 LRU 清理本房间缓存，默认预演）
immersive cache clear [yes]（删除本房间目录缓存，默认预演；剧本缓存需站点控制台清理）
immersive history           （本房间配乐切换与配图历史）
immersive director          （只读查看 AI 音乐导演最近采用的乐谱、指纹与段落；不可在房间内修改）
voice status                （本房间语音生效值、NPC 映射、静音名单与小时用量）
voice on|off [yes]        （本房间语音开关；无法越过站点总开关）
voice map set <speaker> <profile> [yes]（绑定 NPC 音色，仅限内置 NPC 人设）
voice mute <speaker>|list|clear [yes]（本房间 NPC 静音名单）
voice usage                 （本房间小时用量与最近 voice_* 事件）
voice cache stats           （本房间语音缓存条数/体积/命中次数，只读）
voice cache clear [yes]   （删除本房间语音缓存，默认预演）
voice cache prune [yes]   （按配额 LRU 清理本房间缓存，默认预演）
rule                        （本房间规则集、世界意志限频与成员 SAN 概览）
rule check <玩家> <技能/属性> [difficulty normal|hard|extreme] [adjustment ±N] [bonus N] [penalty N]
rule social <玩家> <技能> [attitude supportive|neutral|opposed|hostile] [leverage] [target-skill N]（房规：社交难度）
rule combined <玩家> <技能1> <技能2> [...] [mode all|any] [difficulty normal|hard|extreme] [bonus N] [penalty N]（房规：组合检定只掷一次）
rule groupluck              （房规：由在场最低幸运者代掷团体幸运）
rule contact <玩家> <技能> [hometown] [remote] [profession] [push] [difficulty normal|hard|extreme]（房规：建立熟人）
rule economy <玩家> [金额] [era 1920s|modern] [assets N] [apply] [reason 原因]（房规：信用评级与开支；默认只读预览，apply 登记现金写回）
rule san add <玩家> <±N> [reason 原因] [yes]（调整 SAN；默认预演，yes 落盘）
rule san set <玩家> <N> [reason 原因] [yes]（设为指定 SAN；默认预演）
rule item <玩家> <物品名> [qty N] [weapon] [persist] [reason 原因]
rule skill <玩家> <技能名> <值|±N> [persist] [reason 原因] [yes]
rule attr <玩家> <属性> <值|±N> [persist] [reason 原因] [yes]
rule limits                 （查看世界意志生效冷却与每小时次数）
rule limits cooldown <秒|inherit>（房间只能比站点更严格）
rule limits hourly <次数|inherit>（房间只能比站点更严格）
```

房间控制台的参数统一使用空格分隔的纯词：开关直接写词（`yes`、`persist`、`weapon`、
`publish`），带值的写 `key value`（`depth 2`、`limit 20`、`reason 原因`）。负数值
（如 `-5`、`-12.5`）仍按位置参数处理，不会被误判成开关。旧的 `--yes` / `--json` /
`--key=value` 写法继续兼容，无需改动已有脚本。

`difficulty` 支持 `normal|hard|extreme`，也接受 `regular`（`normal` 与 `regular` 等价）及中文
`普通|困难|极难`。

`rule` 域不需要 AI KP，`manual` 房间同样可用；所有数值变更都会写入房间日志，并在
`character_state.sanity_records` / `skill_changes` / `attr_changes` / `item_changes`
留下操作者、前后值与原因。`rule skill` / `rule attr` 默认预演，`yes`（或网页控制台确认框）
才写盘；属性名支持 `STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC` 与中文名。
`rule item` / `rule skill` / `rule attr` 默认只写本房间快照，`persist` 才立即写回持久角色卡；
房间归档时按房规 `rule_persist_on_archive`（默认开启）把 SAN、技能、属性、物品统一写回，
只覆盖实际变更的字段，不会整卡覆盖玩家在卡面编辑器里的其它修改。

`rule social` / `rule combined` / `rule groupluck` / `rule contact` / `rule economy` 受房间房规开关
`coc7_social_difficulty` / `coc7_combined_checks` / `coc7_group_luck` / `coc7_contacts` / `coc7_economy`
控制，默认关闭。开关关闭时这些子命令按「未知的规则命令」处理，与旧版本完全一致；开启后统一走
`check_service` 的检定入口：社交先按态度与把柄折算难度，组合检定只掷一次并与全部技能比较，
团体幸运由最低幸运者代掷，熟人检定复用普通检定再附后果。`rule economy` 默认只读展示信用评级与开支
预览；加 `apply` 时按开支扣减角色卡现金，并登记一条 `assets` 待写回项，归档时与 SAN / 技能 / 属性
共用 `apply_room_changes` 合并进角色卡。其他资产在卡面是自由文本，不做数值扣减。

玩家侧还可以在聊天框直接使用斜杠命令（不依赖 AI KP）：

```text
/help                                            列出全部聊天命令与用法
/check <玩家> <技能/属性> [困难/极难] [±N]        属性或技能检定
/sc <玩家> <成功变化/失败变化> [no-madness] [reason 原因]     理智检定并结算 SAN；一次损失 ≥5 自动接疯狂判定
/skill [玩家名] <技能名> [值|±N] [persist] [reason 原因]      查看或修改技能值（技能封顶 99）
/attr [玩家名] <属性> [值|±N] [persist] [reason 原因]         查看或修改 STR/DEX/CON/APP/POW/SIZ/EDU/INT/LUC
/item [list] [玩家名]                            查看物品与武器
/item rm [玩家名] <物品名> [qty N] [weapon] [persist] [reason 原因]   移除物品/武器
/item [玩家名] <物品名> [qty N] [weapon] [persist] [note 备注]        追加物品/武器
```

聊天命令的参数统一使用空格分隔的纯词（`persist`、`weapon`、`qty 3`、`reason 原因`），
不再使用 `--` 前缀；`/attr STR -5` 里的 `-5` 仍按数值处理。输入 `/` 会在聊天框上方弹出
逐格候选提示，支持上下键切换与 Tab 补全。

`/skill`、`/attr`、`/item` 与房间控制台的 `rule skill` / `rule attr` / `rule item` 走同一套
服务入口（`room_rules.apply_*` / `remove_item`），数值规则、留痕与写回队列完全一致；默认只改
本房间快照，`persist` 才立即写回持久角色卡。

本人只能操作自己的角色卡，省略玩家名即操作自己；指定其他玩家需要房间所有者 / 房间管理员
（聊天命令）或系统管理员权限。`/sc`、`/check`、`/item`、`/skill`、`/attr` 直接执行，不再二次确认。

`dice animation set` / `dice player-roll` / `dice reroll` / `dice reroll-limit` 只写入本房间的房规
字段（`inherit` 或 `null` 表示继承管理员默认），不会修改站点级配置；`dice level` / `dice player`
为旧写法别名，仍可使用。`dice cancel` 会让 AI 收到「投掷被管理员取消」，并将其作为工具结果继续对话。

破坏性命令（`log clear`、`archive`、`narrative reset` / `unlock` / `trigger fire` /
`rebuild-state`）同样遵循「缺省预演、确认后执行」的约定：网页控制台先返回预演文本，
再由交互确认框决定是否真正执行；加 `yes` 可跳过确认（旧的 `--yes` / `-y` 仍兼容）。

房间 `narrative` 命令只读写本房间的运行态、账本、轨迹与房规：
`trace` 读取房间目录下的 `narrative_trace.jsonl`（最近 2000 条）；`media` 只列本房间
消息附件；`opening` 固定作用于当前房间；`guard` / `profile` 写入房规
`narrative_guard` / `narrative_profile`，下次请求生效；`rebuild-state` 用最近关键帧 +
账本重放覆盖当前状态。任何跨房间或站点级操作都会被拒绝。

房间模式会影响可用命令：`manual`（无 AI KP）房间的 `narrative` / `immersive` / `voice`
一律返回「当前房间模式未启用 AI KP」；`freeform`（无剧本）房间的 `narrative opening`
会被拒绝，直接发送消息即可开始自由叙事。

成员的 `kick` / `promote` 只调整**房间内角色**（`owner` / `admin` / `member`）：不能提升为房间
所有者，也不会授予网站级 `ADMIN` / `OWNER`，更不复用网站控制台的 `user promote` 实现。

## 实时沉浸（配乐 / 配图）

站点级命令作用于所有房间；房间内请用房间控制台，避免越权。

```text
immersive status [json]                 查看总开关、配乐/配图、默认平台、导演与今日用量
immersive on|off [--yes]                站点沉浸总开关
immersive music                        配乐设置概览
immersive music on|off [--yes]          站点配乐开关
immersive music auto <on|off> [--yes]   是否随叙事自动切歌
immersive music volume <0-100> [--yes]  默认音量
immersive music crossfade <500-10000> [--yes] 换段交叉淡入毫秒
immersive music hysteresis <0-10> [--yes]     切换滞回回合
immersive music seed <seed>|clear [--yes]     固定可复现的音乐种子
immersive music source <realtime|bgm|hybrid> [--yes]  站点音乐来源档位
immersive music allowed <realtime,bgm,hybrid> [--yes] 个人可覆盖的来源范围
immersive music personal <on|off> [--yes]     是否允许个人覆盖音乐来源
immersive illustration                 配图设置概览
immersive illustration on|off [--yes]  站点配图开关
immersive illustration auto <on|off> [--yes]  是否随叙事自动配图
immersive illustration provider <procedural|platform> [--yes]
immersive illustration timeout <20-600> [--yes]
immersive illustration concurrency <1-4> [--yes]
immersive illustration budget <n> [--yes]     每日提交上限（0=不限）
immersive illustration cap <n> [--yes]        单房间每小时上限（0=不限）
immersive illustration min-turns <1-50> [--yes] 自动配图最小间隔回合
immersive illustration trigger <off|important|turn_interval|frequent> [--yes] 站点触发档位
immersive illustration allowed <important,turn_interval,frequent> [--yes] 个人可覆盖的触发档位
immersive illustration kp-budget <n> [--yes]  AI KP 每日判断预算（0=不限）
immersive kp                                  AI KP 今日调用/预算/平台与模型
immersive scenario                            剧本配图缓存概览（文件数/体积/策略）
immersive scenario record <on|off> [--yes]    是否把新生成图写入剧本目录（默认关）
immersive scenario prefer <on|off> [--yes]    游玩时是否优先复用剧本图（默认开）
immersive scenario quota <MB> [--yes]         单剧本容量上限（0=不限，默认 256）
immersive scenario max-image <MB> [--yes]     单张写入剧本的图片上限（默认 8）
immersive scenario overflow <lru|reject> [--yes] 超限策略（默认 lru）
immersive scenario stats [scenario_id]        查看缓存文件数/体积（含未登记旧文件）
immersive scenario clear [scenario_id] [--yes] 删除缓存与索引（默认 dry-run）
immersive scenario prune [scenario_id] [--yes] 按配额做 LRU 清理（默认 dry-run）
immersive platform                            列出生图平台表（密钥只回掩码）
immersive platform show <id>                  查看单条平台
immersive platform use <id> [--yes]           设为默认生图平台（并把来源切到 platform）
immersive platform enable|disable <id> [--yes] 启用/停用平台
immersive platform delete <id> [--yes]        删除平台及其密钥（破坏性，默认预演）
immersive platform key <id> set <key>|clear [--yes] 轮换/清除密钥（敏感，不回显）
immersive platform test <id> [--no-generate] [--yes] 先列模型，必要时做一次最小生图探测
immersive platform migrate [--yes]            把旧单条配置一次性迁移为平台条目
immersive director                            导演概览（开关、平台/模型、预算、缓存）
immersive director on|off [--yes]             导演开关
immersive director platform <id>|clear [--yes] 导演使用的 AI 平台（空=默认平台）
immersive director model <name>|clear [--yes]  导演模型（空=平台默认）
immersive director interval <1-20> [--yes]    每 N 回合尝试一次
immersive director budget <n> [--yes]         每日调用预算（0=不限）
immersive director ttl <0-86400> [--yes]      乐谱缓存 TTL 秒（0=不复用）
immersive director min-confidence <0-100> [--yes] 低于该值回退规则分析器
immersive director fallback <rule|hold> [--yes] 失败回退策略
immersive director cache [clear] [--yes]      查看 / 清理乐谱缓存
immersive key set <api_key>|clear [--yes]     写入默认平台密钥（兼容旧命令，默认预演）
immersive test [--yes]                        探测默认生图平台（可能产生费用）
immersive usage                               今日外部生图与导演用量
immersive cache clear [--yes]                 清理图片缓存与用量
immersive room status <room_id>               查看单房间沉浸状态
immersive room reset <room_id> [--yes]        清除单房间沉浸状态与历史
```

默认 `procedural` 使用本地确定性 SVG，不联网、无版权风险。切换到 `platform` 后，
外部生图只认「生图模型平台表」：每条形如 `config/imageplatform/<id>.json`，密钥单独存放
在运行时目录，列表只回掩码。旧的 `illustration_base_url/model/size/api_key` 已弃用，
仅在首次打开设置页或执行 `immersive platform migrate` 时迁移一次，之后运行期只读平台表。
未配置、超时、预算耗尽或上游报错都会自动回落到程序化图，并在日志与回报中带有
`degraded` 与原因，不阻塞叙事。

`immersive director` 只影响「后台精修」：规则分析器始终先广播并播放，导演乐谱必须通过
严格 schema 校验才会被采用；非法、超时、低置信、预算耗尽都会按 `fallback` 回退，
不会阻塞叙事流或音频播放。房间控制台仅提供 `immersive director` 只读查看。

## 网络、存档与备份

```text
network status                  本地 IP / 端口可用性 / 发现与穿透开关
network test                    端口绑定与 UDP 发现连通性测试
autosave                        立即触发一次自动存档检查（为到期房间写入自动存档）
backup create [--out <dir>] [--yes]     打包 config/剧本/房间/知识库/用户库/日志为 zip
```

`backup create` 默认输出到 `data/backups`，可用 `--out` 指定目录。

## 配置查看与迁移

```text
config show [--section <name>]    查看生效配置（五层合并结果），被 env/CLI 锁定的键标注来源
config migrate [--yes]            把遗留 site.json/network.json/penetration.json 并入 general.toml，
                                  原文件改名为 *.legacy（缺省预演，--yes 直接执行）
config path                       打印配置目录、模板目录与用户配置文件路径
```

`config show` 输出的是部署生效值（代码默认 < 模板 < 用户配置 < 环境变量 `AI_TRPG_CFG_*` < CLI
`--set`）；标注 `(locked: env)` / `(locked: cli)` 的键在设置页不可修改。`config migrate` 只写入
`general.toml` 中尚不存在的键，保证迁移前后生效配置完全一致；迁移完成后遗留文件保留为
`*.legacy` 备份，可人工确认后删除。

## 系统 OWNER 转移

```text
owner transfer <username-or-id> [--yes]
```

把系统 OWNER 身份转移给目标用户，**原 OWNER 自动降为 ADMIN**。用于换人接手站点管理。

## 服务信息与日志

```text
server health [--json]          向量库/embedding/OCR/AI 平台/用户库健康检查
server info                     版本/Python/运行平台/数据目录及各目录占用
log tail [n]                    打印最新日志文件末尾 n 行（默认 50）
```

