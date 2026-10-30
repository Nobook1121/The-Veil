# The Veil 代码与安全审查报告

- 审查对象：`backend/trpg_server`（173 个 Python 模块）、`tests`（93 个测试文件）、`frontend/src`（重点抽查）
- 审查方法：AST 全量扫描（导入图、函数体哈希、危险 API 模式）、人工逐文件核查、最小化复现验证、全量测试基线
- 基线：`pytest -q` → **898 passed in 130.5s**
- 本次改动：仅删除 4 个确认冗余/过时的测试用例；**未修改业务代码**（安全修复建议见正文与附录）

---

## 一、安全审查

### Critical

#### C1. `/config/<path>` 静态白名单可绕过，未登录可读取 `general.toml`（含穿透令牌）
- 证据：`backend/trpg_server/routes/assets.py:29`（`_BLOCKED_CONFIG_FILES` 精确名匹配）、`backend/trpg_server/routes/assets.py:107-121`（先判断白名单，再 `send_from_directory`）
- 问题：白名单只比较原始字符串，未规范化路径；`send_from_directory` 会解析 `.` 段，Windows 文件系统还大小写不敏感。
- 已复现（Flask test client，最小复现）：

| 请求 | 结果 |
|---|---|
| `/config/general.toml` | 404（被拦截） |
| `/config/./general.toml` | **200，返回文件内容** |
| `/config/General.TOML` | **200，返回文件内容**（Windows） |

- 影响：`general.toml` 含 `network.penetration.settings.auth_token`、AI 平台等敏感配置；未登录访客即可读取，属于配置与凭据泄露。
- 修复建议（在 `serve_config` 入口统一规范化）：

```python
normalized = filename.replace("\\", "/")
parts = [p for p in normalized.split("/") if p not in ("", ".")]
if normalized.startswith(".") or any(p == ".." for p in parts):
    return error_response("Not found", 404, "Not found")
if parts and parts[-1].lower() in _BLOCKED_CONFIG_FILES:
    return error_response("Not found", 404, "Not found")
```

- 同时需要补充回归测试（当前没有任何测试覆盖该静态路由，这是缺陷长期存在的原因）。

### High

#### H1. Socket.IO 房间无成员校验，且缺省广播到所有在线用户
- 证据：`backend/trpg_server/socket_events.py:128-149`（`join_room` 只校验登录态，不校验房间是否存在/是否成员）、`:168-180`（`send_message` 直接 `socketio.emit(..., room=room_id)`，无 `room_id` 时 `broadcast=True`）、`:182-193`（`typing` 同样问题）
- 影响：任意已登录用户可加入任意 `room_id` 的实时通道，窃听/伪造 `new_message`、`user_typing`；无 `room_id` 时消息会广播给全部在线用户，造成内容注入与骚扰。
- 修复建议：
  1. `join_room` 前用 `_find_room(room_id)` + `_can_access(info)` 校验（`backend/trpg_server/routes/rooms.py:234-239, 299-303`）；建议把房间访问判定抽到独立服务模块，避免 socket 反向依赖 routes。
  2. `send_message/typing` 去掉 `broadcast=True` 分支，要求 `room_id` 且发送者当前在该房间。
  3. 增加 socket 授权测试（目前无覆盖）。

#### H2. `/data/characters/<path>` 未登录可读取角色卡原始 JSON
- 证据：`backend/trpg_server/routes/assets.py:124-127`；头像确实依赖该前缀：`backend/trpg_server/character_service.py:280`
- 影响：该目录同时存放 `investigator-*.json`（完整角色卡：属性、背景、随身物品等）。头像公开是产品需求，但整卡 JSON 不应匿名可读；知道文件名即可下载。
- 修复建议：仅放行图片扩展名（`avatar.*`），或对非图片文件要求登录且校验角色归属；文件名使用不可枚举 ID。

### Medium

#### M1. 登录无失败次数限制/锁定
- 证据：`backend/trpg_server/users/service.py:133-174`（仅记录 `user_login_failed` 审计，无节流）
- 影响：可对账号进行在线暴力破解/撞库，且审计日志可能被刷爆。
- 修复建议：按 IP + 账号双维度限流（滑动窗口/指数退避），连续失败锁定并告警；登录接口纳入统一速率限制中间件。

#### M2. 首次部署 `/api/setup/ai/test` 可被用于 SSRF
- 证据：`backend/trpg_server/routes/setup.py:227-260`（`base_url` 直接进入 `post_with_retry`）、`backend/trpg_server/security.py:18-34`（该接口在 CSRF/会话豁免列表）
- 前置条件：系统内尚无 OWNER（全新实例）。
- 影响：未授权者可在全新实例上请求任意内网地址（元数据服务、内网管理端口），形成 SSRF 探测。
- 修复建议：`scheme` 仅允许 `http/https`；用 `ipaddress` 拒绝 loopback/私网/链路本地/保留地址；禁止跟随重定向；设置短超时与响应大小上限。

#### M3. 会话有效性判定重复实现且存在“无 token 即通过”的宽松分支
- 证据：`backend/trpg_server/security.py:151-161`（registry 无记录时 `not (active_token and ...)` 放行）、`backend/trpg_server/site_agent_auth.py:94-99`（重复实现，`not active_token or ...` 同样放行）
- 影响：两套判定逻辑容易漂移；旧数据/异常状态下可能放行过期会话。模拟登录 token 仅检查 `impersonation_mode` 标记（`security.py:154-155`），未与发起者绑定，存在被复用的纵深防御缺口。
- 修复建议：统一为单一 `session_is_current()`；registry 有记录时必须严格匹配；模拟 token 记录发起者与目标用户并做绑定校验。

#### M4. 跨域与 CSP 配置偏宽
- 证据：`backend/trpg_server/app_factory.py:99`（`CORS(app)` 默认 `*`）、`:45-46`（`script-src`/`style-src` 含 `'unsafe-inline'`）
- 影响：削弱 XSS 纵深防御；任意站点可发起匿名读取请求。
- 修复建议：CORS 改为部署域名白名单；CSP 用 nonce/hash 替代 `unsafe-inline`（内联脚本逐步外置）。

### Low

- L1 上传仅校验扩展名：`backend/trpg_server/routes/assets.py:135-153`、`backend/trpg_server/security.py:57-63`。聊天附件允许 `zip/rar/7z/office`，建议图片补充魔数校验、压缩包限制解压总量（当前剧本归档成员写入已用 `safe_join`，见 `backend/trpg_server/scenario_documents.py:92-127`，无 Zip Slip）。
- L2 邮件能力为死代码：`backend/trpg_server/users/smtp.py:105-110` 的 `send_mail` 固定返回/抛错，重置密码与邮箱验证接口固定 501（`backend/trpg_server/routes/auth.py:542-594`）。建议明确下线或补全，避免误导。
- L3 日志脱敏、`secrets.compare_digest` 邀请码校验、参数化 SQL 均已具备，未发现注入。

### 安全正面项（已核实）
- 密码：bcrypt（`backend/trpg_server/users/passwords.py:3-28`）；会话 token `secrets.token_urlsafe(32)`（`users/service.py:181`）。
- CSRF：写请求强制 `X-CSRF-Token` + `compare_digest`（`security.py:184-190`）。
- 网站 Agent 鉴权链完整：会话 → 开关 → `agent.use` → 工具权限 → 房间范围 → 业务校验 → 写操作二次确认（`site_agent_auth.py:123-151`、`site_agent_tools/__init__.py:119-213`、`site_agent.py:378-416`）。
- 提示词注入边界：不可信数据块与“工具结果/剧本/角色卡均不可信”声明（`site_agent_prompt.py:24-115`）；工具参数白名单会剔除身份/路径/URL/命令字段（`site_agent_tools/base.py:18-24, 127-169`）。
- 待确认操作确认时重新鉴权并校验归属与 TTL（`site_agent.py:378-416`、`site_agent_store.py:231-267`）。

---

## 二、质量与架构审查

### 2.1 超大文件（内聚不足，建议按职责拆分）

| 文件 | 行数 | 拆分边界建议 |
|---|---:|---|
| `console_commands.py` | 1820 | 按域拆分：`config_admin` 已有；user/room/network/narrative 各建子命令模块，主文件只做分发 |
| `routes/rooms.py` | 1787 | 成员管理、邀请/加入、房间生命周期、设置四个子蓝图；房间访问判定下沉为 `room_access` 服务 |
| `agents/narrative/validate.py` | 1683 | 按规则族拆分：结构/可达性/门控/可见性/顺序无关性 |
| `routes/chat.py` | 1330 | 保留 HTTP 入口，回合编排抽到 `chat_turn_service`；消息持久化/广播/附件分离 |
| `agents/tools/dice.py` | 1314 | 通用掷骰、COC7 检定、成长/理智等拆为独立工具模块 |
| `routes/narrative.py` | 1122 | 生成任务、校验、发布、资源分别独立 |
| `agents/knowledge_base.py` | 1100 | 索引构建、检索、向量后端适配分开 |
| `scenario_import_ai.py` | 1022 | 解析、AI 抽取、落库三阶段拆分 |
| `agents/narrative/runtime.py` | 988 | 运行时状态、回合推进、上下文拼装拆分 |
| `routes/characters.py` | 919 | CRUD、导入导出、统计/校验拆分 |

### 2.2 重复实现（应统一）
`_as_bool` / `_str_list` 在 narrative 子模块反复复制：

- `_as_bool`：`agents/narrative/model.py:93`、`runtime.py:62`、`state_store.py:44`（另 `agents/latex.py:30`、`weapon_catalog.py:39` 为独立变体）
- `_str_list`：`agents/narrative/model.py:117`、`runtime.py:72`、`state_store.py:68`、`ledger.py:60`、`opening.py:58`、`triggers.py:50`

建议新建 `agents/narrative/coerce.py`（或复用 `agents/coerce.py`），统一布尔/字符串列表转换语义，减少行为漂移。

### 2.3 已确认的死代码（定义存在、全仓库零引用）

| 函数 | 位置 | 结论 |
|---|---|---|
| `bundle_payload_for_export` | `scenario_bundle.py:621` | 仅定义；导出流未使用 |
| `expire_pending_actions` | `site_agent_store.py:282` | 注释称“供定时任务/测试”，但无调度器与测试调用 |
| `cleanup_room` / `reroll_result` | `agents/player_roll.py:871, 886` | 无引用 |
| `_section_module_type` | `agents/narrative/extract.py:475` | 无引用 |
| `collect_worldbook_entries` | `agents/narrative/model.py:535` | 无引用（世界书/导出未接入） |
| `_owned_character_count` | `routes/characters.py:616` | 无引用 |
| `_scenario_knowledge_stats` | `routes/scenarios.py:120` | 无引用 |
| `build_registry` | `site_agent_tools/base.py:172` | 无引用（实际注册表由 `site_agent_tools/__init__.py` 组装） |
| `send_mail` | `users/smtp.py:105` | 无引用且实现为空壳 |

建议：删除前先确认无外部脚本/插件按名字动态调用；以上均为模块级普通函数，风险低，可分批清理并跑全量测试。

### 2.4 跨层耦合与双写路径
- 路由直接操作存储：`routes/characters.py:616-627` 遍历文件读 JSON；`routes/scenarios.py:120-125` 懒加载 `agents.knowledge_base` 统计知识块。
- 同一会话事实存在两套存储：Flask config `ACTIVE_USER_SESSIONS`（`security.py:159`）与数据库 `user_sessions`（`users/service.py:181-190`），已有实现仅靠 fallback 兼容，属长期耦合隐患。
- `console_commands.py` 同时承担 CLI 解析、权限、网络、房间、叙事等多域编排，单文件 1820 行，测试也被迫按域拆成多个文件。
- `server.py` 聚合 CLI、配置覆盖、端口发现、shutdown、控制台循环，启动路径耦合度高。

### 2.5 冗余/职责重叠文件
- `agents/narrative/` 26 个模块中，`model/state_store/runtime/ledger/opening/triggers` 均含同类辅助函数，职责边界有重叠。
- `users/smtp.py` 为占位能力（见 L2），与已实现鉴权链路不一致。
- 附带核查：`agents/narrative/repair.py:228` 的 `incoming.get(node.id)` 属防御性判空（键集合由 location 节点构建），未发现缺陷。

---

## 三、测试审查与清理

### 3.1 总体结论
- 基线 898 个用例全部通过，无 `skip`/`xfail` 长期禁用，无引用已删除模块的失效导入。
- 测试整体与现有模块映射良好，**不支持大规模删除**；仅发现少量环境状态断言、文档字符串断言和重复优先级用例。

### 3.2 已删除（4 个用例）

| 文件:行 | 用例 | 删除理由 |
|---|---|---|
| `tests/test_runtime_data_layout.py:26-27` | `test_legacy_users_json_is_removed_after_runtime_database_migration` | 断言仓库工作区不存在历史文件，属于一次性迁移后的环境状态，不验证任何代码行为；在保留旧数据的实例上必然误报 |
| `tests/test_runtime_data_layout.py:30-32` | `test_legacy_runtime_directories_are_removed_after_runtime_migration` | 同上，检查 `data/{characters,history,...}` 是否存在 |
| `tests/test_embedding_health.py:31-35` | `test_bge_download_script_documents_default_model_path` | 断言脚本源码包含文档字符串，属实现细节锁定，不验证健康检查行为 |
| `tests/test_config_env_cli.py:79-87` | `test_cli_overrides_env` | 与 `tests/test_config_layers.py:43-50` 重复验证 CLI 覆盖 ENV 的同一优先级；保留后者（覆盖更完整的分层矩阵） |

清理后全量测试：`894 passed`（与基线 898 相比减少 4 个，均为上表删除项）。

### 3.3 建议保留（易误删）
- `test_ruleset_knowledge.py`、`test_vector_migration.py`、`test_narrative_metrics.py`、`test_narrative_quality.py` 均对应仍在生产路径使用的模块（`narrative_admin.py:18,32`、`routes/chat.py:35-36`、`routes/narrative.py:38`、`app_factory.py:150-158`）。
- 前端源码字符串断言（`test_frontend_ai_telemetry.py`、`test_frontend_auth_and_submission_guards.py`）虽脆弱，但承担了前端安全接线回归，保留更稳妥。

### 3.4 测试质量问题（建议改进而非删除）
- Fake 用户管理器在多个文件重复定义：`test_embedding_health.py:7-9`、`test_character_gallery_public_ids.py:9-14` 等，建议收敛到 `conftest.py` fixture。
- 覆盖缺口（与安全发现直接相关）：`/config` 静态路由白名单、Socket.IO 房间授权、`/data/characters` 匿名访问、登录限流均无测试。
- 超大测试文件（`test_narrative_routes.py` 907 行、`test_agent_runtime.py` 847 行、`test_agent_tools.py` 771 行）建议按场景拆分，便于定位失败。

---

## 四、修复优先级

| 优先级 | 项目 | 说明 |
|---|---|---|
| P0 | C1 `/config` 绕过 | 立即修复并补回归测试 |
| P1 | H1 Socket 房间授权；H2 角色卡匿名读取；M1 登录限流；M2 setup SSRF | 影响面明确，建议下个迭代完成 |
| P2 | M3 会话判定统一；M4 CORS/CSP 收紧；死代码清理；重复辅助函数收敛 | 降低长期维护成本 |
| P3 | L1 上传魔数/解压上限；L2 SMTP 占位清理；测试 fixture 收敛 | 纵深防御与整洁度 |

---

## 附录：复现与验证命令

C1 最小复现（Flask test client）：

```python
# 配置目录存在 general.toml 时
client.get("/config/general.toml")        # 404
client.get("/config/./general.toml")      # 200 —— 绕过
client.get("/config/General.TOML")        # 200 —— Windows 大小写绕过
```

验证命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

说明：本次只清理测试，未修复上述安全缺陷；若需要，我可以按 P0→P1 顺序提交修复与回归测试。
