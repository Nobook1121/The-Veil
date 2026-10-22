# 项目地图（AI 上下文速查）

> **给 AI 的用法**：开始任何任务前先读本文件，再按需读 `docs/api.md`、`docs/development.md` 和目标模块。
> 本文件只描述「东西在哪、怎么跑、依赖什么」，不复制实现细节；行数为量级参考，会随迭代变化。

## 1. 项目定位

**帷幕 The Veil**：自托管的 AI 跑团（TRPG）平台。AI 担任 KP/守秘人，真人玩家在同一房间聊天中掷骰、交流、推进剧情。
仓库名 `the-veil`，当前开发版本 `v0.5.2b`（`package.json` 版本号尚未 bump，仍为 `0.5.0-b`），Apache-2.0。

技术栈：

- 后端：Python 3.11+ / Flask 2.3 / Flask-SocketIO（线程模式）/ SQLite / JSON 文件存储
- 前端：TypeScript + 原生 DOM（历史页面）+ React 19（侧边栏与首页聊天）/ Bootstrap 5（CDN）
- 构建：`tsc`（legacy TS → `dist/public/js`）+ `esbuild`（React bundle、3D 骰子 bundle）
- 3D 骰子：`three` + `cannon-es`（独立 bundle `dist/public/js/dice/dice3d.js`，按需加载）
- AI：OpenAI 兼容接口（DeepSeek / OpenAI / 阿里百炼 / SiliconFlow / OpenRouter / LM Studio / 任意兼容端点）
- 向量检索：内置 SQLite 向量库（默认）/ 可选 Qdrant；Embedding 支持本地 `sentence-transformers`、云端接口、哈希降级
- 文档解析：PyMuPDF（PDF/DOC，AGPL-3.0 双许可）、olefile；可选 PaddleOCR（扫描件）

## 2. 启动与运行

| 场景 | 命令 |
| --- | --- |
| 源码启动（Windows） | 双击 `start.cmd`，或 `python server.py` |
| 源码启动（macOS/Linux） | `python server.py` |
| 指定端口 | `python server.py 8087`（默认 8086，被占用时自动向后找） |
| 开发时自动重建前端 | 设 `AI_TRPG_DEV_BUILD=1` 后启动 |
| 前端构建 | `npm run build:frontend`（含 `build:react` 与 `build:dice3d`） |
| 仅 3D 骰子 bundle | `npm run build:dice3d` |
| 类型检查 | `npm run typecheck` |
| 全量验证 | `.\scripts\verify.ps1`（py_compile → typecheck → build → node --check → pytest） |
| 单元测试 | `python -m pytest -q`（测试目录 `tests/`，`conftest.py` 把 `backend/` 加入 `sys.path`） |

入口链路：`server.py` → `create_app()`（`backend/trpg_server/app_factory.py`）→ 注册蓝图/Socket.IO/Embedding/向量库 → `werkzeug.make_server` 监听 `0.0.0.0`。
`server.py` 另含：端口选择、控制台命令循环、仅回环可用的优雅停机端点 `POST /__internal/shutdown`。

## 3. 顶层目录

| 路径 | 作用 |
| --- | --- |
| `server.py` | 兼容启动入口，只做启动/端口/控制台/停机 |
| `backend/trpg_server/` | 全部后端源码（Python 包） |
| `frontend/src/` | 前端源码（TS、HTML 片段、样式、i18n、模板） |
| `dist/public/` | 前端构建产物（发布包内置，禁止手改） |
| `data/` | 持久化数据（剧本、资源、配置、运行时数据） |
| `tests/` | pytest 测试（55 个 `test_*.py`；全量 511 passed / 1 skipped） |
| `scripts/` | 构建、迁移、验证、导入辅助脚本 |
| `config/` | 示例环境配置（`vector-embedding-ocr.example.env`） |
| `docs/` | 开发文档、API 文档、格式说明、本地 wiki |
| `release/` | 变更日志与发布清单 |
| `pyrightconfig.json` | Pyright 静态检查配置（`extraPaths: ["backend"]`，让测试能解析 `trpg_server.*`） |
| `node_modules/`、`.venv/`、`__pycache__/`、`.pytest_cache/` | 依赖与缓存，不手改 |

## 4. 后端结构

### 4.1 启动与基础设施（`backend/trpg_server/` 根层）

| 文件 | 职责 |
| --- | --- |
| `app_factory.py` | 创建 Flask app、CORS、Socket.IO、日志、蓝图、CSP/安全头、Embedding/向量库初始化、自动存档线程 |
| `settings.py` | 全部路径常量与环境变量（端口 8086、数据目录、向量库、OCR、Secret Key） |
| `socket_events.py` | Socket.IO 事件注册（房间聊天、成员/输入状态、房间控制台日志推送等） |
| `security.py` | `safe_join`、上传扩展名校验、会话守卫 |
| `json_store.py` | `read_json` / `write_json_atomic`（原子写，防半写文件） |
| `logging_config.py` | 日志格式 `[Time][LEVEL][Thread]`、轮转文件、敏感字段脱敏 |
| `responses.py` | 统一 `success/message/data` 响应与错误响应 |
| `permission_config.py` / `role_config.py` | 权限节点与 OWNER/ADMIN/USER 角色配置 |
| `ai_platform_config.py` / `ai_capabilities.py` / `ai_admin.py` | AI 平台配置读写、能力检测、管理 |
| `console_commands.py` | **服务器控制台**命令（用户/剧本/房间/备份/停机等，约 1456 行） |
| `room_console_commands.py` | **房间内嵌控制台**命令（严格限定当前房间） |
| `room_log.py` | 房间日志 `data/runtime/rooms/<id>/console.jsonl`，随房间归档 |
| `backup_admin.py` / `server_admin.py` / `user_admin.py` | 备份、服务器、用户管理逻辑 |
| `scenario_store.py` | 剧本文件存储：`scenario.json` + `versions/` + `trigger-content/` |
| `scenario_documents.py` / `scenario_importer.py` / `scenario_import_pipeline.py` / `scenario_import_jobs.py` | 剧本导入：文档解析、分块、AI 结构化、任务队列 |
| `scenario_admin.py` / `scenario_ai.py` | 剧本管理命令与 AI 生成/摘要 |
| `character_ai.py` | AI 生成角色卡 |
| `dice_textures.py` (222) | 骰子贴图库：UUID 文件名、索引原子写、魔数与尺寸校验（不依赖 Pillow） |
| `media_defaults.py` | 媒体与默认封面处理 |
| `network_discovery.py` / `network_admin.py` | 局域网 IP/端口发现、网络配置 |
| `setup_embedding.py` / `vector_admin.py` / `knowledge_admin.py` | Embedding 模型下载/上传、向量库与知识库管理 |
| `site_agent.py` (439) | 网站 Agent 编排层：轮次循环、token 记账、平台故障转移 |
| `site_agent_config.py` / `site_agent_auth.py` / `site_agent_prompt.py` / `site_agent_store.py` | Agent 配置（`general.toml [site_agent]`）、统一鉴权（会话→开关→`agent.use`→工具权限→房间范围→业务校验）、系统提示词（不可信数据块包裹）、按用户隔离的会话/待确认操作/审计/用量存储 |
| `site_agent_tools/` | Agent 工具注册表：`base.py`（JSON Schema 白名单清洗，丢弃身份/路径/命令字段）+ `characters` / `scenarios` / `rooms` / `permissions` / `users` / `utilities`；只读工具直接执行，写工具只生成 `pending_action` 待确认卡片 |
| `character_service.py` (244) / `scenario_service.py` (312) | 角色卡/剧本的领域服务：校验、版本、知识库索引；路由与 Agent 写工具共用，避免两条写路径分叉 |

### 4.2 HTTP 路由（`backend/trpg_server/routes/`，每个文件一个业务组）

| 文件（约行数） | 职责 |
| --- | --- |
| `rooms.py` (1717) | 房间 CRUD、成员、邀请码、角色、自动存档、归档、切换剧本 |
| `chat.py` (1028) + `chat_helpers.py` (676) | 聊天消息、AI KP 对话/流式输出、上下文组装 |
| `characters.py` (883) | COC7 角色卡、职业/技能/武器目录、角色卡广场 |
| `scenarios.py` (790) + `scenario_summary.py` (130) | 剧本 CRUD、模块、版本、摘要 |
| `scenario_imports.py` (216) | 剧本导入任务与审核发布 |
| `auth.py` (553) | 注册/登录/登出/改密/找回密码/邮箱验证/身份模拟 |
| `users.py` (335) | 用户列表、角色与状态、IP 配置、全局认证设置 |
| `config.py` (545) | 站点、AI 平台、运行配置 |
| `setup.py` (426) | 首次部署六步向导（站点、AI、Embedding、Owner） |
| `room_console.py` (180) | 房间控制台 REST 接口与日志读取 |
| `site_agent.py` (193) | 网站 Agent HTTP 接口（会话、消息、待确认操作、用量） |
| `triggers.py` (282) | 触发器资源与附件 |
| `dice_rolls.py` (202) | 待投掷请求查询/投掷/幂等完成/取消/强制投掷，前端降级上报 |
| `dice_textures.py` (111) | 骰子贴图上传、桌面皮肤、逐面皮肤、删除引用保护 |
| `knowledge_bases.py` (153) | 规则书知识库 |
| `network.py` (212) | 局域网/内网穿透配置 |
| `themes.py` (174) | 主题资源 |
| `telemetry.py` | AI 用量与缓存统计 |
| `vector_health.py` | 向量库健康检查 |
| `character_ai.py` (73) | 角色卡 AI 生成接口 |
| `pages.py` / `assets.py` | 主页/SPA 回退、静态资源与上传 |

完整接口清单见 [api.md](./api.md)；文件访问、日志、前端规范见 [development.md](./development.md)。

### 4.3 AI 子系统（`backend/trpg_server/agents/`）

| 文件 | 职责 |
| --- | --- |
| `runtime.py` (554) | Agent 主循环：消息组装、工具调用、工具结果折叠、usage 记录 |
| `prompt_builder.py` (158) | KP/NPC 提示词拼装 |
| `profiles.py` (62) | AgentProfile 与默认 KP 工具集 |
| `config.py` (235) / `ai_request.py` (113) | AI 运行配置（字段规格表 `_FIELD_SPECS` 驱动，新增配置项只需加一行）、统一请求封装 |
| `responses_transport.py` (187) / `http_retry.py` (102) | Responses/Chat Completions 传输、超时重试 |
| `cache.py` (92) / `cache_probe.py` (225) | 精确/语义缓存、缓存探测 |
| `token_accounting.py` (87) / `telemetry.py` (117) | token 计量、用量看板数据 |
| `structured_output.py` (219) | 结构化 JSON 输出与校验 |
| `vector_store.py` (367) / `embedding_provider.py` (86) / `vector_migration.py` (67) | 向量库抽象、Embedding 选择、旧索引迁移 |
| `knowledge_base.py` (1025) / `ruleset_knowledge.py` (100) / `ruleset_adapters.py` (93) | 剧本/规则书检索与规则适配 |
| `trigger_system.py` (290) | 触发器匹配、剧透等级、可见性 |
| `room_state.py` (80) / `memory.py` (43) / `context.py` (47) | 房间状态、长期记忆、上下文提供器 |
| `versioning.py` (65) | 剧本/数据版本 |
| `dice_settings.py` (44) | 骰子动画/玩家投掷生效值解析：房规优先、管理员默认兜底 |
| `player_roll.py` (~620) | 玩家投掷等待：内存态 `PendingRoll` + `threading.Event`，超时自动投掷、断线恢复、房间关闭清理；投掷/检定结果写入房间日志 |
| `ocr_provider.py` | PaddleOCR 封装 |

Agent 工具（`agents/tools/`，注册名用 `命名空间.工具名`，发送给 API 前 `.` 会转成 `_`）：

- `room.py`：房间快照、角色卡、剧本上下文/模块、场景激活、记忆读写、行动建议
- `dice.py`：COC 检定、普通掷骰、理智检定
- `knowledge.py`：规则书检索
- `trigger.py`：揭示剧本触发器
- `suggest.py`：建议行动
- `base.py`：ToolRegistry 与 API 工具名规范化

### 4.4 用户与认证（`backend/trpg_server/users/`）

- `database.py`：SQLite（`data/runtime/users/users.sqlite3`）
- `service.py` (686)：用户服务、登录态、IP 配置
- `migrations.py`：旧 `users.json` → SQLite 迁移
- `passwords.py`：bcrypt
- `smtp.py` / `tokens.py`：邮件验证与令牌

## 5. 前端结构

### 5.1 源码组织（`frontend/src/`）

| 路径 | 作用 |
| --- | --- |
| `index/fragments/*.html` | 页面片段，按 `index.parts.json` 顺序拼接为 `dist/public/index.html` |
| `index/index.parts.json` | 片段加载顺序清单（01 头部侧栏 → 02 主标签 → 03 房间/工具/认证/设置 → 04 编辑器弹窗 → 06 向导 → 05 平台弹窗与脚本） |
| `app/*.ts` | legacy 前端主逻辑，`tsc` 编译到 `dist/public/js/` |
| `app/views/` | 剧本视图与模块渲染 |
| `app/controllers/` | `ScenarioController.ts` 等控制器 |
| `app/models/` | `ScenarioModel.ts` |
| `app/config/` | `ConfigManager.ts`、`AIPlatformManager.ts`、`TestRequestConfig.ts` |
| `app/auth/` | 登录/注册/资料/用户卡片/认证状态 |
| `app/dice/` | 简易动画、3D 适配器、降级记忆、骰子外观、管理员贴图库、`diceOverlay.ts`（覆盖层）、`diceTypes.ts`（共享类型） |
| `app/diceTable.ts` (~1400) | 虚拟投掷桌：物理骰面结果生成、本地骰桌结算、3D → 简易 → 纯文本逐层降级并上报 |
| `dice3d/main.ts` (~1100) | Three.js + cannon-es 3D 骰子舞台（物理骰面结算），独立打包为 `js/dice/dice3d.js` |
| `app/generated/templates.ts` | 构建时由 `templates/*.html` 生成（勿手改） |
| `templates/*.html` | `<template id="...">` 模板源 |
| `react/` | React 19 局部挂载：`main.tsx`、`shell/Sidebar.tsx`、`home/HomeChat.tsx`，esbuild 打包 |
| `react/agent/` | 网站 Agent：`AgentProvider`（共享状态）、`AgentBall`（悬浮球）、`AgentWindow`（可拖拽悬浮窗）、`AgentPage`（全屏页）、`AgentChat` / `AgentActionCard`（Markdown 消息与待确认卡片）、`agent-api.ts` / `agent-types.ts` / `agent.css` |
| `styles/` | 分层 CSS：`00-theme-tokens` → `01-foundation-chat` → `02-scenario-character` → `03-tools-settings-platform` → `04-auth-profile-overrides` → `05-dice-animation` |
| `locales/zh_cn.json` / `en_us.json` | 中英文案（运行时 `i18n.ts`） |
| `tools/` | 骰子/检定/工具管理（编译到 `dist/public/data/tools/`） |
| `types/*.d.ts` | 手写全局类型声明 |

### 5.2 构建产物（`dist/public/`，发布包直接使用）

- `index.html`：片段拼接结果
- `js/`：tsc 输出的 legacy JS（`app/`、`config/`、`views/`、`generated/`、`auth/`、`vendor/`……）
- `js/react/main.js` + `main.css`：esbuild 打包的 React 部分
- `js/dice/dice3d.js`：esbuild 打包的 3D 骰子 bundle（按需加载）
- `data/tools/`：骰子/检定工具脚本
- `locales/`：i18n JSON

第三方前端库通过 CDN 引入（见 `index/fragments/05-platform-modals-and-scripts.html`）：
Bootstrap 5.3、Socket.IO 4.7.5、marked 15、DOMPurify 3.4.16。二维码为仓库内裁剪版 `qrcode-generator`（MIT）。

## 6. 数据布局（`data/`）

| 路径 | 内容 |
| --- | --- |
| `data/scenarios/<id>/` | 已发布剧本：`scenario.json`、`versions/`、`trigger-content/` |
| `data/scenarios/knowledge-index/` | 剧本知识索引 |
| `data/assets/` | 头像、剧本封面、AI 平台图标、主题、上传资源、vendor 资源 |
| `data/dice-textures/` | 骰子贴图库（桌面贴图 / 骰面贴图 + 索引） |
| `data/config/` | `site.json`、`network.json`、`penetration.json`、`general.toml`、`roles/`、`aiplatform/`、`aimodel/`、角色技能表 |
| `data/occupations/` / `data/weapons/` | 职业与武器目录 |
| `data/runtime/users/` | `users.sqlite3`、IP 配置 |
| `data/runtime/rooms/<room_id>/` | `info.json`、`messages.json`、`autosave.json`、`nodes/`、`console.jsonl` |
| `data/runtime/agent/` | 网站 Agent：`conversations/<user_id>/`、`actions/`（待确认操作，TTL 默认 5 分钟）、`audit/`（JSONL 滚动审计）、`usage/<user_id>/`（逐日 token） |
| `data/runtime/room_archives/` | 房间归档快照（含控制台日志） |
| `data/runtime/characters/` / `character_gallery/` | 角色卡与广场 |
| `data/runtime/knowledge-bases/` / `scenario_imports/` / `scenario_drafts/` | 知识库、导入任务、草稿 |
| `data/runtime/vector-db/` | 内置向量库 / Qdrant 数据 |
| `data/runtime/models/` | 本地 Embedding（bge-small-zh）与 PaddleOCR 模型 |
| `data/runtime/logs/` | 服务器日志 `ai_trpg_YYYYMMDD_HHMMSS.log` |
| `data/runtime/flask_secret_key.txt` | 会话密钥（持久化） |

## 7. 依赖清单（直接依赖）

### Python（`requirements.txt`）

`Flask==2.3.3`、`Flask-Cors==4.0.1`、`Werkzeug==2.3.8`、`Flask-SocketIO==5.3.6`、`python-socketio==5.11.2`、`bcrypt==4.0.1`、`pytest==8.4.0`、`olefile==0.47`、`requests>=2.31,<3`、`PyMuPDF>=1.24,<2`

可选：

- `requirements-vector.txt`：`sentence-transformers>=3.0,<6`（本地 Embedding；不装则用云端或哈希向量）
- `requirements-vector-qdrant.txt`：Qdrant 客户端
- `requirements-ocr.txt`：`paddlepaddle` + `paddleocr`（扫描 PDF OCR，模型较大）

### Node / 前端（`package.json`）

- dependencies：`react`、`react-dom`（19.x）、`three` 0.180、`cannon-es` 0.20
- devDependencies：`typescript` 5.x、`esbuild` 0.28、`@types/react`、`@types/react-dom`、`@types/three` 0.180
- `three` / `cannon-es` 仅用于 3D 骰子 bundle，均为 MIT
- 运行时前端库走 CDN：Bootstrap、Socket.IO client、marked、DOMPurify
- 仓库内置：裁剪版 qrcode-generator

许可证与合规：见 [THIRD-PARTY-LICENSES.md](../THIRD-PARTY-LICENSES.md)。**PyMuPDF 为 AGPL-3.0 / Artifex 商业双许可**，分发前必须确认。

## 8. 关键工程约定（改代码前必看）

- 后端包必须位于 `backend/trpg_server/`；根目录 `server.py` 只作兼容入口（有结构测试约束）。
- 前端浏览器源码必须在 `frontend/src/app/`，手写类型放 `frontend/src/types/`；不得出现 `frontend/src/js`、根 `js/`、`frontend/dist/`。
- 新增路由：`routes/` 下新文件一个业务组，并在 `app_factory.register_blueprints()` 注册。
- 路径拼接用 `security.safe_join`；上传校验用 `is_allowed_upload`；JSON 读写用 `json_store`。
- 新接口前端优先用 `TrpgApi`（`app/api-client.ts`），DOM 操作用 `TrpgDom`。
- 禁止 `alert()` / `confirm()` / `prompt()`；用 `showNotification()` 或 `TrpgDialog.confirm()/prompt()`。
- 新增控制台命令：独立非 Flask 模块 + 依赖注入；破坏性命令默认 dry-run + 交互确认，支持 `--yes`；同时更新 `command_help_text()` 与 `docs/console-commands.md`。
- 房间控制台命令严格限定当前房间；日志写房间目录 `console.jsonl` 并纳入归档。
- 网站 Agent：权限节点 `agent.use` + 内置角色 `site_agent`；所有写操作只生成 `pending_action` 待确认卡片，确认时服务端重新鉴权并校验操作有效期（默认 5 分钟）；工具入参按 JSON Schema 白名单清洗，身份与路径/命令字段一律丢弃；角色卡/剧本写入必须走 `character_service.py` / `scenario_service.py`，不在路由或工具里另起写路径。
- 角色卡编辑器的「AI 生成」按钮已移除，AI 生成角色卡能力由网站 Agent 承接；原「小工具」页已删除，侧边栏入口改为 Agent（`/check`、`/roll` 等聊天命令保留）。
- 每次提交后更新 `release/changelog/`（写明时间）；新增依赖必须登记 `THIRD-PARTY-LICENSES.md`；新功能同步更新 `docs/the-veil.wiki/`。
- 日志：`logging.getLogger(__name__)`；INFO 只记关键操作；不记录密码/密钥/令牌/上传内容；输出字典前用 `redact_sensitive`。
- 前端静态资源需带缓存控制头，避免浏览器长期缓存旧样式。

## 9. 常见任务定位

| 要改什么 | 先看哪里 |
| --- | --- |
| AI 对话/工具调用/上下文/token | `agents/runtime.py`、`agents/prompt_builder.py`、`routes/chat.py`、`agents/tools/*` |
| AI 平台配置/超时/重试 | `ai_platform_config.py`、`agents/ai_request.py`、`agents/responses_transport.py`、`agents/http_retry.py` |
| 剧本导入/解析 | `scenario_import_pipeline.py`、`scenario_documents.py`、`scenario_import_jobs.py`、`routes/scenario_imports.py` |
| 剧本 CRUD/版本/模块 | `scenario_store.py`、`routes/scenarios.py`、`frontend/src/app/views/*` |
| 房间/邀请/成员/自动存档 | `routes/rooms.py`、`room_admin.py`、`data/runtime/rooms/` |
| 聊天与 Socket 事件 | `routes/chat.py`、`routes/chat_helpers.py`、`socket_events.py`、`frontend/src/app/chat.ts` |
| 角色卡/COC7 检定 | `routes/characters.py`、`character_ai.py`、`frontend/src/app/character-sheet.ts`、`agents/tools/dice.py` |
| 骰子动画/玩家投掷/贴图 | `agents/dice_settings.py`、`agents/player_roll.py`、`routes/dice_rolls.py`、`routes/dice_textures.py`、`dice_textures.py`、`frontend/src/app/diceTable.ts`、`frontend/src/app/dice/*`、`frontend/src/dice3d/main.ts`、`styles/05-dice-animation.css` |
| 权限/角色 | `permission_config.py`、`role_config.py`、`routes/users.py`、`routes/auth.py` |
| 向量检索/Embedding | `agents/vector_store.py`、`agents/embedding_provider.py`、`setup_embedding.py`、`routes/vector_health.py` |
| 房间控制台 | `room_console_commands.py`、`room_log.py`、`routes/room_console.py`、`frontend/src/app/room-console.ts` |
| 网站 Agent | `site_agent.py`、`site_agent_auth.py`、`site_agent_config.py`、`site_agent_prompt.py`、`site_agent_store.py`、`site_agent_tools/*`、`routes/site_agent.py`、`frontend/src/react/agent/*`；写操作走 `character_service.py` / `scenario_service.py` |
| 服务器控制台 | `console_commands.py`、`docs/console-commands.md` |
| 前端页面/样式 | `frontend/src/index/fragments/*`、`frontend/src/app/*`、`frontend/src/styles/*` |
| 国际化 | `frontend/src/locales/zh_cn.json`、`en_us.json`、`frontend/src/app/i18n.ts` |

## 10. 已知坑（来自历史排查，避免重复踩）

- 长耗时非流式 AI 请求可能被网关约 20 秒空闲超时静默中断，必须在总超时预算内做重试，不能只调大 timeout。
- 同一请求里同时使用 `response_format: json_object` 和原生 `tools`，模型可能把工具调用塞进 JSON 正文而永不执行。
- 缓存命中率不能简单用 `cached_tokens / prompt_tokens`（本地缓存命中时 token 为 0，会显示 0%），应逐请求记录命中。
- DeepSeek 要求工具函数名匹配 `^[a-zA-Z0-9_-]+$`，内部 `命名空间.工具` 必须转成 `_`，assistant/tool 消息里的 name 也要同步规范化。
- 3D 骰子不可用时前端按 `3D → simple → text` 逐层降级并写入房间日志；改动动画逻辑必须覆盖整条降级链。
- 玩家投掷等待超时取 `min(玩家投掷超时, AI 请求超时 - 5)`；AI 端不能无限等待玩家点击。
- 修改前端后必须重新 `npm run build:frontend`；发布包用 `dist/`，不依赖 Node。
- 浏览器会缓存未带版本的静态资源，样式改动需强制 revalidate。
- 测试 `test_backend_project_structure.py` / `test_frontend_project_structure.py` 会校验目录边界，移动文件前先看它们。

## 11. 相关文档

- [API 概览](./api.md)：按蓝图分组的全部 HTTP API 与 Socket.IO 事件
- [开发说明](./development.md)：架构边界、验证流程、日志/文件/前端规范
- [控制台命令](./console-commands.md)
- [剧本格式](./scenario-format.md)、[剧本导入](./scenario-import.md)
- [向量库](./vector-store.md)
- 本地 Wiki：`docs/the-veil.wiki/`
