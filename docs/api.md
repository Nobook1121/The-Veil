# API Overview

本文档记录当前后端 Flask 蓝图暴露的 HTTP API 与 Socket.IO 事件，供二次开发与自动化脚本使用。

## 通用约定

- **响应格式**：业务接口统一返回 `success`、`message`、`data` 三个字段；出错时 `success` 为 `false`，`message` 为可读原因。
- **鉴权**：登录态基于会话 Cookie。部分写操作会在会话失效时返回 `401 Session expired`。
- **权限节点**：受控接口通过 `require_permission_node(...)` 校验权限节点（如 `settings.network`、`scenarios.delete`）；无权限时拒绝请求。角色为 `OWNER` / `ADMIN` / `USER` 三级，节点权限可在「设置 → 权限配置」中逐项勾选。
- **占位符**：`<int:xxx>` 表示整型路径参数，`<path:filename>` 表示可含 `/` 的路径参数，`<xxx>` 表示字符串参数。

---

## 一、页面与静态资源

来源：`routes/pages.py`、`routes/assets.py`

- `GET /`：返回主页面。
- `GET /<path:path>`：静态页面与资源回退（前端路由兜底）。
- `GET /assets/avatars/<path:filename>`：用户头像。
- `GET /assets/scenario_covers/<path:filename>`：剧本封面。
- `GET /assets/scenarios/<path:filename>`：剧本相关资源。
- `GET /assets/aiplatform/<path:filename>`：AI 平台图标。
- `GET /assets/vendor/<path:filename>`：第三方前端依赖资源。
- `GET /assets/theme/<path:filename>`：界面主题静态资源（如主题背景图）。
- `GET /assets/uploads/<path:filename>`：用户上传资源。
- `GET /config/<path:filename>`：客户端配置文件。
- `GET /data/characters/<path:filename>`：角色数据资源。
- `GET /data/tools/<path:filename>`：小工具静态资源。
- `POST /api/assets/upload`：上传资源，返回可访问的 URL。

---

## 二、首次部署引导（Setup）

来源：`routes/setup.py`

系统内还没有任何 `OWNER` 时，`/api/setup/status` 的 `setup_required` 为 `true`，前端会弹出六步向导。

- `GET /api/setup/status`：读取引导状态（是否需要引导、站点信息、AI 配置状态）。
- `POST /api/setup/dismiss`：关闭引导。
- `POST /api/setup/site`：保存站点名称等基础信息。
- `POST /api/setup/ai`：保存向导中的 AI 平台配置。
- `POST /api/setup/ai/test`：测试 AI 连接是否可用。
- `GET /api/setup/embedding/status`：读取 Embedding 步骤状态（本地模型是否就绪、下载进度、可选的云端平台）。只读接口，不随 OWNER 存在与否锁定。
- `POST /api/setup/embedding/download`：在后台线程下载本地 Embedding 模型（默认 `BAAI/bge-small-zh-v1.5`，可从官方仓库回落到镜像站）。
- `POST /api/setup/embedding/upload`：接收用户拖入的模型（`.zip` 压缩包或散文件），自动展开顶层目录后写入模型目录。
- `POST /api/setup/embedding/cloud`：复用已配置的 AI 平台，写入其 embedding 接口配置，并开启 `cloud_embedding_enabled`。
- `POST /api/setup/owner`：创建首个最高权限（OWNER）账号，完成后自动登录。

> 本地模型与云端 Embedding 均在程序下次启动时生效；未准备模型时系统会退回内置哈希向量。

---

## 三、认证与会话（Auth）

来源：`routes/auth.py`

- `POST /api/auth/register`：注册用户。
- `POST /api/auth/login`：登录；同一账号新的登录会使旧会话失效。
- `POST /api/auth/logout`：退出登录。
- `GET /api/auth/status`：读取当前认证状态；旧会话失效时返回 `401 Session expired`。
- `POST /api/auth/update`：更新当前用户资料。
- `POST /api/auth/password/change`：修改当前用户密码。
- `GET /api/auth/settings`：读取当前用户可用的认证方式设置。
- `POST /api/auth/password/forgot`：发起找回密码。
- `POST /api/auth/password/reset`：重置密码。
- `POST /api/auth/email/verify/request`：请求邮箱验证。
- `POST /api/auth/email/verify/confirm`：确认邮箱验证。
- `POST /api/auth/impersonation/start`：以其他用户身份进入（管理用途）。
- `POST /api/auth/impersonation/stop`：退出身份模拟。

---

## 四、用户与权限（Users）

来源：`routes/users.py`

- `GET /api/users`：列出用户。
- `PUT /api/users/<int:user_id>/role`：更新用户角色（不能授予高于自身的角色）。
- `PUT /api/users/<int:user_id>/status`：启用或停用用户。
- `GET /api/user/profile`：读取当前用户资料。
- `PUT /api/user/profile`：更新当前用户资料。
- `PUT /api/user/presence`：上报在线状态。
- `GET /api/user/ip/config`：读取当前用户的 IP 配置。
- `POST /api/user/ip/config`：保存当前用户的 IP 配置。
- `GET /api/admin/ip/configs`：读取管理员可见的全部 IP 配置。
- `GET /api/admin/auth/settings`：读取全局认证设置（注册开关、邮箱验证等）。
- `PUT /api/admin/auth/settings`：保存全局认证设置。

---

## 五、角色卡（Characters）

来源：`routes/characters.py`

- `GET /api/character-catalogs/occupations`：职业目录。
- `GET /api/character-catalogs/skills`：技能目录（含专精与中文变体）。
- `GET /api/character-catalogs/weapons`：武器目录。
- `GET /api/characters`：列出当前用户的角色卡。
- `PUT /api/characters/<character_id>`：更新角色卡。
- `DELETE /api/characters/<character_id>`：删除角色卡。
- `GET /api/character-gallery`：浏览角色卡广场。
- `POST /api/character-gallery`：发布角色卡到广场。
- `PUT /api/character-gallery/<public_id>`：更新已发布的角色卡。
- `DELETE /api/character-gallery/<public_id>`：从广场下架角色卡。

---

## 六、剧本（Scenarios）

来源：`routes/scenarios.py`、`routes/scenario_summary.py`

- `GET /api/scenarios`：列出剧本。
- `GET /api/scenarios/<int:scenario_id>`：读取单个剧本（含模块内容）。
- `POST /api/scenarios`：创建剧本。
- `PUT /api/scenarios/<int:scenario_id>`：更新剧本。
- `DELETE /api/scenarios/<int:scenario_id>`：删除剧本（同步清理该剧本的向量库与知识索引）。
- `POST /api/scenarios/<int:scenario_id>/archive`：归档剧本。
- `GET /api/scenarios/list`：列出剧本摘要。
- `GET /api/scenarios/<int:scenario_id>/knowledge`：读取该剧本的世界书条目。
- `GET /api/scenarios/draft`：读取剧本草稿。
- `POST /api/scenarios/draft`：保存剧本草稿。
- `DELETE /api/scenarios/draft`：删除剧本草稿。
- `POST /api/scenarios/import`：导入剧本（别名 `POST /api/scenarios/import-script`）。
- `POST /api/scenarios/module-summary`：为指定模块生成摘要。
- `POST /api/scenarios/cover`：上传剧本封面。
- `DELETE /api/scenarios/cover`：删除剧本封面。
- `POST /api/scenarios/cover/rename`：重命名剧本封面。

---

## 七、剧本导入与发布（Import Pipeline）

来源：`routes/scenario_imports.py`

导入为异步任务：先拿到 `jobId`，再轮询或订阅进度流。

- `POST /api/scripts/import`：multipart 上传（`file`、`title`、`author` 等），返回 `202` 与 `jobId`。
- `GET /api/scripts/import/<job_id>`：任务状态、当前阶段与审核预览摘要。
- `GET /api/scripts/import/<job_id>/stream`：SSE 进度流。
- `POST /api/scripts/import/<job_id>/retry`：重试任务。
- `POST /api/scripts/import/<job_id>/cancel`：取消任务。
- `PUT /api/scripts/import/<job_id>/preview`：保存审核阶段的修改。
- `POST /api/scripts/<int:script_id>/publish`：发布，生成版本快照与知识索引。
- `GET /api/scripts/<int:script_id>/versions`：查看版本列表。
- `GET /api/scripts/<int:script_id>/knowledge`：读取该剧本的世界书。
- `PUT /api/scripts/<int:script_id>/knowledge`：保存该剧本的世界书（触发词、副键、常驻、分层、分组、优先级、概率、粘滞/冷却/延迟轮数）。
- `POST /api/scripts/<int:script_id>/search`：在剧本知识库中检索。

---

## 八、触发器资源（Triggers & Assets）

来源：`routes/triggers.py`

- `GET /api/scripts/<int:script_id>/assets`：列出剧本资源库。
- `POST /api/scripts/<int:script_id>/assets`：新增资源。
- `PUT /api/scripts/<int:script_id>/assets/<asset_id>`：更新资源。
- `DELETE /api/scripts/<int:script_id>/assets/<asset_id>`：删除资源。
- `POST /api/scripts/<int:script_id>/cards/<card_id>/attachments`：给触发器卡片挂载资源。
- `PUT /api/scripts/<int:script_id>/cards/<card_id>/attachments/<trigger_id>`：更新挂载。
- `DELETE /api/scripts/<int:script_id>/cards/<card_id>/attachments/<trigger_id>`：移除挂载。
- `GET|POST /api/scripts/<int:script_id>/triggers`：读取 / 新增剧本触发器。
- `PUT /api/scripts/<int:script_id>/triggers/<trigger_id>`：更新触发器。
- `GET|POST /api/rooms/<room_id>/triggers`：读取房间已解锁的触发器 / 向房间推送触发器内容。

---

## 九、知识库（Knowledge Bases）

来源：`routes/knowledge_bases.py`、`routes/vector_health.py`

- `GET /api/knowledge-bases/scenarios`：查看剧本知识库状态（位置、向量数量）。
- `DELETE /api/knowledge-bases/scenarios/<int:scenario_id>`：清理该剧本的向量与索引（不会删除剧本本体）。
- `GET /api/knowledge-bases/rulesets`：列出规则书知识库。
- `GET /api/knowledge-bases/<ruleset_id>/sources`：列出规则书来源文件。
- `POST /api/knowledge-bases/<ruleset_id>/sources`：上传并索引规则书文件。
- `POST /api/knowledge-bases/<ruleset_id>/reindex`：重建索引。
- `POST /api/knowledge-bases/<ruleset_id>/enable`：启用规则集。
- `POST /api/knowledge-bases/<ruleset_id>/archive`：归档规则集。
- `POST /api/rooms/<room_id>/rulesets`：为房间指定规则集。
- `GET /api/vector/health`：查看向量库、Embedding 与 OCR 的可用性（不返回密钥）。

---

## 十、房间（Rooms）

来源：`routes/rooms.py`

- `GET /api/rooms`：列出当前用户可访问的房间；`ADMIN` 与 `OWNER` 可查看全部。
- `POST /api/rooms`：创建房间并生成唯一房间码；普通 `USER` 默认最多创建 3 个房间，`ADMIN` / `OWNER` 不限。
- `POST /api/rooms/join`：通过房间码加入房间。
- `POST /api/rooms/spectate`：以旁观身份加入房间。
- `GET /api/rooms/<room_id>`：读取房间详情、成员与当前消息。
- `DELETE /api/rooms/<room_id>`：删除房间；仅房主、`ADMIN` 或 `OWNER` 可操作。
- `GET /api/rooms/<room_id>/spectate`：读取旁观信息。
- `PUT /api/rooms/<room_id>/members/<user_id>/character`：为成员绑定角色卡。
- `PUT /api/rooms/<room_id>/members/<user_id>/role`：调整成员在房间内的角色。
- `DELETE /api/rooms/<room_id>/members/<user_id>`：移除成员。
- `POST /api/rooms/<room_id>/scenario-switch`：切换剧本。
- `POST /api/rooms/<room_id>/scenario-migration`：在换剧本时迁移剧情进度。
- `GET|PUT /api/rooms/<room_id>/house-rules`：读取 / 保存房间规则（含技能基础值覆盖）。
- `GET /api/rooms/<room_id>/console/log`：读取本房间控制台日志（可按 `level` / `search` / `limit` 过滤）。
- `GET /api/rooms/<room_id>/console/commands`：读取房间控制台命令白名单与帮助文本。
- `POST /api/rooms/<room_id>/console/command`：执行一条房间范围内命令（body: `{command}`）。
- `DELETE /api/rooms/<room_id>/console/log`：清空本房间控制台日志（破坏性，已归档房间拒绝）。
- `POST /api/rooms/<room_id>/archive`：归档房间。
- `GET /api/rooms/<room_id>/archive`：读取归档房间内容（别名 `GET /api/room-archives/<room_id>`）。
- `GET /api/room-archives`：列出归档房间（别名 `GET /api/rooms/archives`）。
- `GET /api/rooms/<room_id>/messages`：读取房间聊天记录。
- `POST /api/rooms/<room_id>/messages`：写入房间消息（含发送者 ID、用户名、头像）。
- `POST /api/rooms/<room_id>/character-records`：写入角色卡变动记录。
- `POST /api/rooms/by-name/<path:room_name>/character-records`：按房间名写入角色卡变动记录。
- `DELETE /api/rooms/<room_id>/character-records/<record_id>`：删除角色卡变动记录。

### 回档与自动存档

- `GET /api/rooms/<room_id>/nodes`：列出回档节点。
- `POST /api/rooms/<room_id>/nodes`：把当前房间全部消息保存为回档节点。
- `GET /api/rooms/<room_id>/nodes/<node_filename>`：读取回档节点。
- `POST /api/rooms/<room_id>/nodes/<node_filename>/restore`：回档到指定节点。
- `DELETE /api/rooms/<room_id>/nodes/<node_filename>`：删除回档节点。
- `POST /api/rooms/<room_id>/autosave`：保存当前消息为自动存档。
- `GET /api/rooms/<room_id>/autosave`：读取自动存档。

### 骰子动画与玩家投掷

来源：`routes/dice_rolls.py`

房间生效档位由房规优先、管理员默认兜底：`dice_animation_level`（`text` / `simple` / `3d`，`null` 表示继承）与 `player_dice_roll`（`true` / `false` / `null`）。房间详情与房规接口的 `dice_animation` 字段给出当前生效值及来源（`room` / `admin`）。

- `GET /api/rooms/<room_id>/dice-requests`：列出本房间等待玩家投掷的请求。
- `GET /api/rooms/<room_id>/dice-requests/<request_id>`：读取单个待投掷请求（重连恢复用）。
- `POST /api/rooms/<room_id>/dice-requests/<request_id>/roll`：目标玩家点击投掷；非目标玩家返回 403。
- `POST .../complete`：幂等确认结果已播放（服务端仅允许目标玩家）。
- `POST .../cancel`：取消等待。
- `POST .../force`：房主 / 房间管理员 / 站点管理员强制立即投掷。
- `POST /api/rooms/<room_id>/dice-animation/degraded`：前端降级上报（`from_level`、`to_level`、`reason`、`error`、`scope`、`dice`、`request_id`、`tool_name`）；会话内去重并限流 30 条 / 60 秒，写入房间日志（`source=dice_animation`）。

### 骰子贴图库

来源：`routes/dice_textures.py`（写入接口仅 `ADMIN` / `OWNER`）

- `GET /api/dice-textures`：贴图资产、桌面皮肤与骰子皮肤；`include_disabled=1` 仅管理员可用。
- `GET /api/dice-textures/assets/<asset_id>`：读取图片（长缓存，文件名不可猜测）。
- `POST /api/dice-textures/assets`：multipart 上传（`file`、`kind`=`table|dice|face`、`name`）；服务端按文件头与尺寸（128–2048px，≤3MB）校验。
- `DELETE /api/dice-textures/assets/<asset_id>`：删除资产；被皮肤引用时返回 409。
- `POST /api/dice-textures/table-skins`：新建 / 更新桌面皮肤（`name`、`asset_id`）。
- `POST /api/dice-textures/dice-skins`：新建 / 更新骰子皮肤（`id` 可选、`name`、`die_type`、`faces` 逐面资产映射）。
- `DELETE /api/dice-textures/skins/<skin_id>`：删除皮肤。

---

## 十一、聊天（Chat）

来源：`routes/chat.py`

- `POST /api/chat`：向 AI 发送请求，返回 AI 内容与 Token 统计（前端仅在 AI 消息上展示耗时与 Token）。
- `POST /api/messages`：发送主页消息。
- `POST /api/scenarios/<int:script_id>/messages`：发送剧本消息。

多轮工具调用时，后端会限制输入预算并折叠较早的工具结果，避免输入 Token 随轮次线性增长。

---

## 十二、配置（Config）

来源：`routes/config.py`

- `POST /api/config/<config_name>`：保存通用配置。
- `GET|POST /api/config/permissions`：读取 / 保存权限节点配置。
- `GET /api/config/aiplatform`：列出已配置的 AI 平台。
- `POST /api/config/aiplatform/<platform>`：保存指定平台配置。
- `DELETE /api/config/aiplatform/<platform>`：删除指定平台。
- `POST /api/config/aiplatform/<platform>/test`：测试平台连接。
- `POST /api/config/aiplatform/<platform>/detect-responses`：探测平台是否支持 Responses 传输。
- `POST /api/config/aimodel/save`：保存 AI 模型请求 JSON 配置。
- `POST /api/config/aimodel/delete`：删除 AI 模型请求 JSON 配置。
- `GET|POST /api/config/system-prompt`：读取 / 保存系统提示词。
- `GET|POST /api/config/debug-prompt`：读取 / 保存调试提示词。
- `GET /api/config/roles`：列出角色定义。
- `POST /api/config/roles/<role_id>`：保存角色定义（其模型与上下文提供者）。

---

## 十三、网络（Network）

来源：`routes/network.py`

- `GET /api/network/config`：读取网络配置（端口、局域网发现、访问控制）。
- `POST /api/network/config`：保存网络配置（需 `settings.network` 权限节点）。
- `GET /api/network/status`：读取网络状态（本机 IP、端口占用、发现开关）。
- `POST /api/network/test`：测试本机绑定与局域网发现是否正常。
- `GET /api/network/penetration/config`：读取穿透配置（需 `settings.network`）。
- `POST /api/network/penetration/config`：保存穿透配置（需 `settings.network`）。
- `GET /api/network/penetration/status`：读取穿透状态（启用情况、类型、端口映射）。

---

## 十四、主题（Themes）

来源：`routes/themes.py`

- `GET /api/themes`：只读主题目录与设计 token 契约，供第三方开发者为网页开发自定义主题与组件。
  - 返回 `default_theme`、`selectors`（主题选择器值与对应主题 id，含 `system`）、`themes`（每个主题的 `id`、`label_key`、`body_classes`、`color_scheme` 与 `tokens`）。
  - `token_contract.css_variables` 给出 token 键名到 CSS 变量的映射；组件只要使用这些 `--theme-*` 变量即可自动适配全部主题。
  - 该接口为纯新增能力，不改变现有主题切换逻辑：运行时主题仍由 `body` 上的类名（`theme-light`、`theme-dark`、`theme-cyber-2`、`theme-tome`）决定。

---

## 十五、用量统计（Telemetry）

来源：`routes/telemetry.py`

- `GET /api/telemetry/ai/daily`：按天统计 AI 用量（Token 消耗、缓存命中、按剧本 / 房间的成本分布）。

---

## 十六、Socket.IO 事件

来源：`socket_events.py`

实时聊天与房间状态走 Socket.IO，与 HTTP 共用同一端口（WebSocket 优先，失败回退 HTTP 长轮询）。

**客户端 → 服务端**

- `join_room`：加入房间，开始接收该房间的实时事件。
- `leave_room`：离开房间。
- `send_message`：发送消息，服务端广播给房间内其他成员。
- `typing`：上报输入状态（前端节流发送）。

**连接生命周期**

- `connect`：客户端连接建立；服务端跟踪 `USER_ROOM_MEMBERSHIP`。
- `disconnect`：客户端断开。

**服务端 → 客户端（广播）**

- `room_members_changed`：房间成员上下线或角色变化后广播，前端据此重拉成员信息。
- `role_changed`：成员房间角色变更时广播。
- `player_dice_request`：AI 摆好骰子、等待目标玩家投掷；payload 含 `room_id`、`request_id`、`roller`、`plan`、`expires_at`。
- `player_dice_rolled`：玩家完成投掷（或超时自动投掷，`auto=true`）；payload 含结果快照。
- `player_dice_cancelled`：等待被取消或房间关闭；payload 含 `reason`。
- `player_dice_forced`：房主 / 管理员强制投掷。
- 实时消息、输入提示等由服务端向房间广播。

---

## 相关文档

- [项目结构](project-structure-requirements.md)
- [开发说明](development.md)
- [剧本格式](scenario-format.md)
- [剧本导入](scenario-import.md)
- [控制台命令](console-commands.md)
- [向量库](vector-store.md)