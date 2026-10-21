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

## 房间管理

```text
room list [search]              列出房间：ID/名称/房间码/剧本/成员数/是否归档
room members <room-id>          查看房间成员（账号角色 + 房间角色）
room archive <room-id> [--yes]  归档房间（要求已到达结局，与网页规则一致）
room prune [--days N] [--yes]   清理僵尸房间：未归档、无有效成员、且 >N 天（默认 30）无活动
```

`room archive` 会生成与网页一致的不可变归档快照（需要运行体注入的 Flask app 上下文；
若未注入则只落盘归档标记）。

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
info                        （房间信息：房间码/剧本/成员数）
members                     （成员及其房间角色）
members kick <user> [--yes] （将成员移出本房间，破坏性）
members promote <user> [--yes]（将成员提升为房间管理员，破坏性）
state                       （房间状态：场景/线索/任务/触发器）
event add <kind> <content>  （记录一条房间事件）
log tail [n]                （查看本房间最近 n 条日志，默认 50）
log clear [--yes]           （清空本房间日志，破坏性）
autosave                    （立即为本房间保存一次自动存档）
archive [--yes]             （归档本房间，需已到达结局）
dice                        （本房间骰子动画档位、玩家投掷、重投配置与待投掷请求）
dice animation show         （查看本房间生效的动画档位）
dice animation set <text|simple|3d|inherit> [--yes]（设置动画档位，inherit 跟随管理员默认）
dice player-roll show       （查看本房间生效的玩家投掷开关）
dice player-roll <on|off|inherit> [--yes]（设置玩家投掷开关）
dice reroll show            （查看本房间生效的重投模式与上限）
dice reroll <off|limited|unlimited|inherit> [--yes]（设置重投模式）
dice reroll-limit <1-20|inherit> [--yes]（设置「指定次数」模式的上限）
dice pending                （列出本房间等待玩家投掷的请求）
dice cancel [request-id] [--yes]（取消待投掷请求，缺省取消最早一条）
```

`dice animation set` / `dice player-roll` / `dice reroll` / `dice reroll-limit` 只写入本房间的房规
字段（`inherit` 或 `null` 表示继承管理员默认），不会修改站点级配置；`dice level` / `dice player`
为旧写法别名，仍可使用。`dice cancel` 会让 AI 收到「投掷被管理员取消」，并将其作为工具结果继续对话。

破坏性命令（`log clear`、`archive`）同样遵循「缺省预演、确认后执行」的约定：网页控制台先
返回预演文本，再由交互确认框决定是否真正执行（服务器控制台则用 `--yes` / `-y` 跳过确认）。

成员的 `kick` / `promote` 只调整**房间内角色**（`owner` / `admin` / `member`）：不能提升为房间
所有者，也不会授予网站级 `ADMIN` / `OWNER`，更不复用网站控制台的 `user promote` 实现。

## 网络、存档与备份

```text
network status                  本地 IP / 端口可用性 / 发现与穿透开关
network test                    端口绑定与 UDP 发现连通性测试
autosave                        立即触发一次自动存档检查（为到期房间写入自动存档）
backup create [--out <dir>] [--yes]     打包 config/剧本/房间/知识库/用户库/日志为 zip
```

`backup create` 默认输出到 `data/backups`，可用 `--out` 指定目录。

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

