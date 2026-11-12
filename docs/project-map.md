# 项目地图（AI 上下文速查）

> **给 AI 的用法**：开始任何任务前先读本文件，再按需读 `docs/api.md`、`docs/development.md` 和目标模块。
> 本文件只描述「东西在哪、怎么跑、依赖什么」，不复制实现细节；行数为量级参考，会随迭代变化。

## 1. 项目定位

**帷幕 The Veil**：自托管的 AI 跑团（TRPG）平台。AI 担任 KP/守秘人，真人玩家在同一房间聊天中掷骰、交流、推进剧情。
仓库名 `the-veil`，当前版本 `v1.0.5`（`backend/trpg_server/version.py` 是唯一真源，`package.json` 同源），Apache-2.0。

技术栈：

- 后端：Python 3.11+（当前验证 3.13）/ Flask 2.3.3 / Werkzeug 2.3.8 / Flask-SocketIO 5.3.6（线程模式）/ SQLite / JSON 文件存储
- 前端：TypeScript + 原生 DOM（历史页面）+ React 19（侧边栏与首页聊天）/ Bootstrap 5（本地随包）
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
| 指定端口 | `python server.py 8087` 或 `--port 8087`（默认 8086，被占用时自动向后找） |
| 指定配置目录 | `python server.py --config-dir <PATH>`（首次启动的种子文件也生成到该目录） |
| 临时覆盖配置 | `python server.py --set section.key=value`（可重复；优先级最高，高于环境变量） |
| 开发时自动重建前端 | 设 `AI_TRPG_DEV_BUILD=1` 后启动 |
| 前端构建 | `npm run build:frontend`（含 `build:react` 与 `build:dice3d`） |
| 仅 3D 骰子 bundle | `npm run build:dice3d` |
| 类型检查 | `npm run typecheck` |
| 全量验证 | `.\scripts\verify.ps1`（py_compile → typecheck → build → node --check → pytest） |
| 单元测试 | `python -m pytest -q`（测试目录 `tests/`，`conftest.py` 把 `backend/` 加入 `sys.path`） |

入口链路：`server.py` 先解析 CLI 并 `ensure_user_config()`（首次从模板生成 `data/config/`），再 `create_app()`（`backend/trpg_server/app_factory.py`）→ 注册蓝图/Socket.IO/Embedding/向量库 → `werkzeug.make_server` 监听 `0.0.0.0`。
`server.py` 另含：端口选择（优先级 CLI > 环境变量 > 用户配置 > 模板/代码默认）、控制台命令循环、仅回环可用的优雅停机端点 `POST /__internal/shutdown`。

## 3. 顶层目录

| 路径 | 作用 |
| --- | --- |
| `server.py` | 兼容启动入口，只做启动/端口/控制台/停机；解析 `--config-dir` / `--set section.key=value` / `--port` |
| `backend/trpg_server/` | 全部后端源码（Python 包） |
| `frontend/src/` | 前端源码（TS、HTML 片段、样式、i18n、模板） |
| `dist/public/` | 前端构建产物（发布包内置，禁止手改） |
| `data/` | 持久化数据（剧本、资源、配置、运行时数据；`data/config/` 不入库，首次启动生成） |
| `tests/` | pytest 测试（147 个 `test_*.py`；2026-11-12 全量 1730 passed / 2 skipped） |
| `scripts/` | 构建、迁移、验证、导入辅助脚本 |
| `config/` | 配置模板（`templates/general.toml`、`templates/roles/`、`templates/aiplatform/`）与示例环境配置（`vector-embedding-ocr.example.env`） |
| `docs/` | 开发文档、API 文档、格式说明、本地 wiki |
| `release/` | 变更日志与发布清单 |
| `pyrightconfig.json` | Pyright 静态检查配置（`extraPaths: ["backend"]`，让测试能解析 `trpg_server.*`） |
| `node_modules/`、`.venv/`、`__pycache__/`、`.pytest_cache/` | 依赖与缓存，不手改 |

## 4. 后端结构

### 4.1 启动与基础设施（`backend/trpg_server/` 根层）

| 文件 | 职责 |
| --- | --- |
| `app_factory.py` | 创建 Flask app、CORS、Socket.IO、日志、蓝图、CSP/安全头、Embedding/向量库初始化、自动存档线程 |
| `settings.py` | 路径常量（从 `config.paths` 再导出）、模型/向量库/OCR 环境变量、Secret Key 持久化；`DEFAULT_PORT` 派生自 `config.schema.DEFAULT_NETWORK_PORT` |
| `config/` 包 | 分层配置权威实现：优先级 代码默认(`schema`) < `config/templates` < 遗留 JSON 兼容层 < `data/config/general.toml` < env `AI_TRPG_CFG_*` < CLI `--set`；`paths`（目录/CLI 解析与注入）、`fields`（FieldSpec 与转换器）、`schema`（全部配置项规格 + `AIRuntimeConfig`）、`sources`（tomlkit/遗留 JSON/env 读取）、`merge`（逐层类型化合并 + 缓存）、`writer`（注释保留定点写入）、`bootstrap`（首启种子） |
| `config_admin.py` (89) | 控制台配置工具：`config show` / `config migrate` / `config path`；迁移只写 TOML 缺失键，并把遗留文件改名 `*.legacy` |
| `props/` | 道具工坊：`registry`（12 内置 + 用户模板）、`template_validation`（字段白名单 + CSS 禁用清单）、`store`、`policy`（防剧透可见性 / 脱敏）、`render`（PDF/PNG）、`image`（photo 模板生图）、`draft` / `prompt`（AI 起草） |
| `plugins/` | 第三方插件扩展体系：`manifest` / `contracts` / `registry` / `runtime` / `bootstrap` / `security`（清单解析、启用与能力门控） |
| `ruleset/` | 规则集插件契约与注册：`contracts` / `registry` / `capabilities` / `descriptors` / `migrations` / `bootstrap` + `coc7/`（检定 / 成长与训练 / 社交 / 理智 / 战斗 / 追逐 / 法术 / 信用 / 治疗等数学与 schema） |
| `combat/` | 战斗调度：`contracts` / `registry` / `scheduler` / `settings` + `rulesets/coc7.py`（战斗数学委托规则集） |
| `room_ops/` | 房间域操作：`access` / `autosave` / `cards` / `constants` / `house_rules` / `log` / `storage` / `views` / `visibility` |
| `immersive/` + `voice/` | 实时沉浸（配乐 / 配图 / 导演 / 外部曲库 / 房间缓存）与语音（TTS 引擎、角色音色、事件、本地模型） |
| `update_service.py` | 应用更新：releases 检查、channel 过滤、zip 下载 + sha256 校验、60s 节流与互斥锁、DISABLE 开关 |
| `version.py` / `app_info.py` | 版本唯一真源 / 应用信息载荷（版本、平台、credits、更新状态） |
| `socket_events.py` | Socket.IO 事件注册（房间聊天、成员/输入状态、房间控制台日志推送等） |
| `security.py` | `safe_join`、上传扩展名校验、会话守卫 |
| `json_store.py` | `read_json` / `write_json_atomic`（原子写，防半写文件） |
| `logging_config.py` | 日志格式 `[Time][LEVEL][Thread]`、轮转文件、敏感字段脱敏 |
| `responses.py` | 统一 `success/message/data` 响应与错误响应 |
| `permission_config.py` / `role_config.py` | 权限节点与 OWNER/ADMIN/USER 角色配置 |
| `ai_platform_config.py` / `ai_capabilities.py` / `ai_admin.py` | AI 平台配置读写、能力检测、管理 |
| `console_commands.py` | **服务器控制台**命令（用户/剧本/房间/备份/停机等） |
| `room_console_commands.py` | **房间内嵌控制台**命令（严格限定当前房间） |
| `room_log/` | 房间控制台日志（`console.jsonl`，随房间归档，`room_log/__init__.py`）+ **团录导出**：`model`（归约）/ `markdown` / `fonts` / `render`（PyMuPDF）/ `jobs`（异步任务）+ `templates/` 三版式 |
| `backup_admin.py` / `server_admin.py` / `user_admin.py` | 备份、服务器、用户管理逻辑 |
| `scenario_store.py` | 剧本文件存储：`scenario.json` + `versions/` + `trigger-content/` |
| `scenario_documents.py` / `scenario_importer.py` / `scenario_import_pipeline.py` / `scenario_import_jobs.py` | 剧本导入：文档解析、分块、AI 结构化、任务队列 |
| `scenario_admin.py` / `scenario_ai.py` | 剧本管理命令与 AI 生成/摘要 |
| `character_ai.py` | AI 生成角色卡 |
| `dice_textures.py` (222) | 骰子贴图库：UUID 文件名、索引原子写、魔数与尺寸校验（不依赖 Pillow） |
| `media_defaults.py` | 媒体与默认封面处理 |
| `network_discovery.py` / `network_admin.py` | 局域网 IP/端口发现、网络配置 |
| `setup_embedding.py` / `vector_admin.py` / `knowledge_admin.py` | Embedding 模型下载/上传、向量库与知识库管理 |
| `site_agent.py` | 网站 Agent 编排层：轮次循环、token 记账、平台故障转移 |
| `site_agent_config.py` / `site_agent_auth.py` / `site_agent_prompt.py` / `site_agent_store.py` | Agent 配置（`general.toml [site_agent]`）、统一鉴权（会话→开关→`agent.use`→工具权限→房间范围→业务校验）、系统提示词（不可信数据块包裹）、按用户隔离的会话/待确认操作/审计/用量存储 |
| `site_agent_tools/` | Agent 工具注册表：`base.py`（JSON Schema 白名单清洗，丢弃身份/路径/命令字段）+ `characters` / `scenarios` / `rooms`（含消息读取/删除/清空）/ `settings`（网站设置读写，ADMIN/OWNER 硬门控）/ `profile`（本人昵称/邮箱/在线状态）/ `permissions` / `users` / `utilities`；只读工具直接执行，写工具只生成 `pending_action` 待确认卡片 |
| `room_usage.py` | 房间 AI 用量账本：按平台模型单价与币种累加 `data/runtime/rooms/<room_id>/usage.json`（总/输入/输出/缓存 token、调用次数、分模型明细、多币种花费） |
| `character_service.py` (244) / `scenario_service.py` (312) | 角色卡/剧本的领域服务：校验、版本、知识库索引；路由与 Agent 写工具共用，避免两条写路径分叉 |

### 4.2 HTTP 路由（`backend/trpg_server/routes/`，每个文件一个业务组）

| 文件（约行数） | 职责 |
| --- | --- |
| `rooms.py` (201) | 房间域入口：共享 helper 与蓝图注册；生命周期 / 成员 / 消息 / 房规 / 幕间见下三行；`GET /api/rooms/<room_id>/usage` 用量（仅房主/房间管理员） |
| `rooms_lifecycle.py` (642) / `rooms_members.py` (287) / `rooms_messages.py` (498) | 房间创建/加入/邀请/切换剧本/归档；成员与角色卡绑定/房主移交/退出；消息、触发器触发与回档节点 |
| `rooms_house_rules.py` (291) / `rooms_interlude.py` (233) | 世界意志权限与房规读写；自动存档与幕间（请求/开始/结算/复核/结束） |
| `chat.py` (217) + `chat_helpers.py` (941) | 聊天入口与兼容 re-export；AI 回合管线在 `agents/turn_pipeline.py`，上下文组装在 `chat_helpers.py` |
| `props.py` (718) | 道具工坊 12 个接口（模板 / 索引 / 创建 / 修改 / 删除 / 渲染 / 投递 / 脱敏 / photo 生图 / AI 起草） |
| `room_log.py` (321) | 团录导出 5 个接口（预览 / 创建 202 任务 / 状态 / 下载 PDF / 删除） |
| `plugins.py` (131) | 第三方插件：列表、schema、rescan、启用 / 停用 / 删除 |
| `app_info.py` (43) | 应用信息与更新：`GET /api/app/info`、更新检查 / 下载 |
| `room_rules.py` (385) | 斜杠规则命令：技能 / 属性 / 物品 / SAN 检定 / 通用检定 |
| `combat.py` (415) | 房间战斗状态与战斗行动 |
| `room_narrative.py` (185) | 房间叙事图、触发器读取与触发 |
| `narrative.py` (1262) | 剧本叙事图：节点 / 边 / 触发器 / 可见性审查 / 启动点 / 校验修复 / 生成任务 |
| `rulesets.py` (18) | 可用规则集列表 |
| `characters.py` (883) | COC7 角色卡、职业/技能/武器目录、角色卡广场 |
| `scenarios.py` (790) + `scenario_summary.py` (130) | 剧本 CRUD、模块、版本、摘要 |
| `scenario_imports.py` (216) | 剧本导入任务与审核发布 |
| `auth.py` (553) | 注册/登录/登出/改密/找回密码/邮箱验证/身份模拟 |
| `users.py` (335) | 用户列表、角色与状态、IP 配置、全局认证设置 |
| `config.py` (536) | `GET /api/config/general` 返回分层合并后的生效配置 + `overrides` 锁定来源（免登录、密钥掩码）；`POST` 逐键 diff 写入并拒绝锁定/密钥键；AI 平台、提示词、权限配置 |
| `setup.py` (426) | 首次部署六步向导（站点、AI、Embedding、Owner） |
| `room_console.py` (316) | 房间控制台命令执行、日志读取/清空、AI 写卡记录查询与回滚 |
| `site_agent.py` (193) | 网站 Agent HTTP 接口（会话、消息、待确认操作、用量） |
| `triggers.py` (282) | 触发器资源与附件 |
| `dice_rolls.py` (398) | 待投掷请求查询/投掷/幂等完成/取消/强制投掷，前端降级上报 |
| `dice_textures.py` (131) | 骰子贴图上传、桌面皮肤、逐面皮肤、删除引用保护 |
| `knowledge_bases.py` (153) | 规则书知识库 |
| `network.py` (212) | 局域网/内网穿透配置 |
| `themes.py` (174) | 主题资源 |
| `telemetry.py` | AI 用量与缓存统计；按 `room_id` 联动 `room_usage.py` 记账 |
| `vector_health.py` | 向量库健康检查 |
| `character_ai.py` (73) | 角色卡 AI 生成接口 |
| `pages.py` / `assets.py` | 主页/SPA 回退、静态资源与上传；`/config/<path>` 在 `assets.py` 中封禁 `general.toml`、`site.json`、`network.json`、`penetration.json` |

完整接口清单见 [api.md](./api.md)；文件访问、日志、前端规范见 [development.md](./development.md)。

### 4.3 AI 子系统（`backend/trpg_server/agents/`）

| 文件 | 职责 |
| --- | --- |
| `runtime.py` | Agent 主循环：消息组装、工具调用、工具结果折叠、usage 记录 |
| `turn_pipeline.py` (~1670) | 一次房间回合的完整管线：模式门控、上下文与提示词、AI 调用、工具执行、叙事守卫与写回 |
| `prompt_builder.py` (158) | KP/NPC 提示词拼装 |
| `latex.py` | LaTeX 氛围排版：站点开关解析、提示词片段、历史公式清洗（`flatten_latex` / `clean_history_for_model`）与缓存指纹 variant |
| `profiles.py` (62) | AgentProfile 与默认 KP 工具集 |
| `config.py` (~57) / `ai_request.py` (113) | `config.py` 仅保留兼容门面（`load_ai_runtime_config` / `write_ai_setting` / `AIRuntimeConfig`），实现已迁入 `trpg_server.config`；`ai_request.py` 为统一请求封装 |
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
- `combat.py`：战斗（瞄准 / 掩体 / 部位 / 击晕 / 毒 / 自然恢复 / 追逐…）
- `coc7.py`：规则集扩展（社交 / 组合检定 / 团体幸运 / 信用 / 联系人 / 成长 / 训练…）
- `props.py`：道具创建 / 投递 / 修改（仅 KP 可调用）
- `narrative.py` / `character_items.py` / `character_stats.py`：叙事图查询、物品与属性写回
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
| `app/settings/` | 设置页关于页（`about.ts`）、第三方插件设置（`plugins.ts`）、设置助手（`settingsAssistant.ts`）；标签页容器与绑定主逻辑在 `app/tabs.ts` |
| `app/props/` | 道具工坊前端：道具库、编辑器、卡片渲染 |
| `app/room-log.ts` | 团录导出入口、选项模态与任务轮询 |
| `app/immersive/` | 沉浸配乐/配图前端 |
| `app/auth/` | 登录/注册/资料/用户卡片/认证状态 |
| `app/dice/` | 简易动画、3D 适配器、降级记忆、骰子外观、管理员贴图库、`diceOverlay.ts`（覆盖层）、`diceTypes.ts`（共享类型） |
| `app/diceTable.ts` (~1400) | 虚拟投掷桌：物理骰面结果生成、本地骰桌结算、3D → 简易 → 纯文本逐层降级并上报 |
| `app/themes/` | 主题视觉引擎（namespace + 独立脚本）：`abyss/`（深渊帷幕：WebGL 氛围、法阵、触手、符文）、`cyber/`（赛博档案：WebGL 氛围、三层数据雨、HUD、指针锁定框） |
| `dice3d/main.ts` (~1100) | Three.js + cannon-es 3D 骰子舞台（物理骰面结算），独立打包为 `js/dice/dice3d.js` |
| `app/generated/templates.ts` | 构建时由 `templates/*.html` 生成（勿手改） |
| `templates/*.html` | `<template id="...">` 模板源 |
| `react/` | React 19 局部挂载：`main.tsx`、`shell/Sidebar.tsx`、`home/HomeChat.tsx`，esbuild 打包 |
| `react/agent/` | 网站 Agent：`AgentProvider`（共享状态）、`AgentBall`（悬浮球）、`AgentWindow`（可拖拽悬浮窗）、`AgentPage`（全屏页）、`AgentChat` / `AgentActionCard`（Markdown 消息与待确认卡片）、`agent-api.ts` / `agent-types.ts` / `agent.css` |
| `styles/` | 分层 CSS：`00-theme-tokens` → `01-foundation-chat` → `02-scenario-character` → `03-tools-settings-platform` → `04-auth-profile-overrides` → `05-dice-animation` → `06-combat-world-override` → `07/08-theme-abyss-*` → `09/10-theme-cyber-*` → `11-immersive` → `12-theme-settings` → `13-settings-redesign` → `14-room-log` → `15-props`（由 `react/app.css` 统一 `@import`） |
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

第三方前端库的引入位置见 `index/fragments/01-head-and-sidebar.html`（样式）与
`index/fragments/05-platform-modals-and-scripts.html`（脚本）：Bootstrap 5.3.0、Socket.IO 4.7.5、
KaTeX、marked 15、DOMPurify 3.4.16 均由 `data/assets/vendor/` 本地随包分发，不再依赖外部 CDN。
二维码为仓库内裁剪版 `qrcode-generator`（MIT）。

## 6. 数据布局（`data/`）

| 路径 | 内容 |
| --- | --- |
| `data/scenarios/<id>/` | 已发布剧本：`scenario.json`、`versions/`、`trigger-content/` |
| `data/scenarios/knowledge-index/` | 剧本知识索引 |
| `data/assets/` | 头像、剧本封面、AI 平台图标、主题、上传资源、vendor 资源 |
| `data/dice-textures/` | 骰子贴图库（桌面贴图 / 骰面贴图 + 索引） |
| `data/config/` | 用户配置（`.gitignore` 已忽略，首次启动从模板幂等生成）：`general.toml` 是唯一「人类设置」入口，段包括 `appearance/language/notification/autosave/room_console/chat/ai*/character_rules/scenario/window/update/site_agent/site/network*`；另有 `roles/`、`aiplatform/`（首次保存时创建）、`aimodel/`；遗留 `site.json`/`network.json`/`penetration.json` 仅兼容读取，用 `config migrate` 并入 TOML |
| `data/occupations/` / `data/weapons/` / `data/character_skills.json` | 职业、武器目录与角色技能表 |
| `data/runtime/users/` | `users.sqlite3`、IP 配置 |
| `data/runtime/rooms/<room_id>/` | `info.json`、`messages.json`、`autosave.json`、`usage.json`、`nodes/`、`console.jsonl`、`props/`（道具 spec 与渲染产物） |
| `data/runtime/logs/exports/<room_id>/<export_id>/` | 团录导出任务与 PDF（按 `keep_exports_days` 清理） |
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

`Flask==2.3.3`、`Flask-Cors==4.0.1`、`Werkzeug==2.3.8`、`Flask-SocketIO==5.3.6`、`python-socketio==5.11.2`、`bcrypt==4.0.1`、`pytest==8.4.0`、`olefile==0.47`、`requests>=2.31,<3`、`PyMuPDF>=1.24,<2`、`tomlkit>=0.12,<1`

可选：

- `requirements-vector.txt`：`sentence-transformers>=3.0,<6`（本地 Embedding；不装则用云端或哈希向量）
- `requirements-vector-qdrant.txt`：Qdrant 客户端
- `requirements-ocr.txt`：`paddlepaddle` + `paddleocr`（扫描 PDF OCR，模型较大）

### Node / 前端（`package.json`）

- dependencies：`react`、`react-dom`（19.x）、`three` 0.180、`cannon-es` 0.20
- devDependencies：`typescript` 5.x、`esbuild` 0.28、`@types/react`、`@types/react-dom`、`@types/three` 0.180
- `three` / `cannon-es` 仅用于 3D 骰子 bundle，均为 MIT
- 运行时前端库：Bootstrap 5.3、Socket.IO client、KaTeX、marked、DOMPurify 本地随包（`data/assets/vendor/`），不依赖外部 CDN
- 仓库内置：裁剪版 qrcode-generator

许可证与合规：见 [THIRD-PARTY-LICENSES.md](../THIRD-PARTY-LICENSES.md)。**PyMuPDF 为 AGPL-3.0 / Artifex 商业双许可**，分发前必须确认。

## 8. 关键工程约定（改代码前必看）

- 后端包必须位于 `backend/trpg_server/`；根目录 `server.py` 只作兼容入口（有结构测试约束）。
- 配置：新增项先在 `config/schema.py` 注册 `FieldSpec`（默认值 + 转换器），再补 `config/templates/general.toml` 对应键与注释；`tests/test_config_template_sync.py` 强制模板与规格表双向同步。业务代码只读 `load_settings()` / `load_ai_runtime_config()` 的生效值，**禁止用 `tomllib` 直读 `general.toml` 或自带默认值兜底**；写入统一走 `config.writer.update_sections()`（注释保留、原子替换、写后失效缓存）。
- 被 env / CLI 覆盖的键自动进入 `overrides` 锁定集合：设置页禁用对应字段，`POST /api/config/general` 会跳过并记录日志；密钥字段（如穿透令牌）GET 掩码、POST 拒写。
- 前端浏览器源码必须在 `frontend/src/app/`，手写类型放 `frontend/src/types/`；不得出现 `frontend/src/js`、根 `js/`、`frontend/dist/`。
- 新增路由：`routes/` 下新文件一个业务组，并在 `app_factory.register_blueprints()` 注册。
- 规则数学进 `ruleset/`（内置 CoC7）或第三方插件；核心模块不得直接 import 具体插件，应通过 `ruleset.capabilities` / 注册表取用（边界清单见 `docs/plans/ruleset-boundary-audit.md`）。
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
| 全局配置/分层加载/设置页 | `config/`（`schema`/`merge`/`writer`/`bootstrap`）、`config/templates/general.toml`、`routes/config.py`、`config_admin.py`、`frontend/src/app/config/ConfigManager.ts` |
| 剧本导入/解析 | `scenario_import_pipeline.py`、`scenario_documents.py`、`scenario_import_jobs.py`、`routes/scenario_imports.py` |
| 剧本 CRUD/版本/模块 | `scenario_store.py`、`routes/scenarios.py`、`frontend/src/app/views/*` |
| 房间/邀请/成员/自动存档 | `routes/rooms.py` + `routes/rooms_*.py`、`room_ops/`、`room_admin.py`、`data/runtime/rooms/` |
| 道具工坊 | `props/`、`routes/props.py`、`agents/tools/props.py`、`frontend/src/app/props/*`、`styles/15-props.css`、`docs/prop-templates.md` |
| 团录导出 | `room_log/`、`routes/room_log.py`、`frontend/src/app/room-log.ts`、`styles/14-room-log.css` |
| 规则集插件 / COC7 规则 | `ruleset/`、`combat/`、`check_service.py`、`agents/tools/coc7.py`、`docs/plans/coc7-rules-roadmap.md` |
| 第三方插件 | `plugins/`、`routes/plugins.py`、`frontend/src/app/settings/plugins.ts`、`docs/plugin-development.md` |
| 应用更新 / 关于页 | `update_service.py`、`app_info.py`、`version.py`、`routes/app_info.py`、`frontend/src/app/settings/about.ts`、`styles/13-settings-redesign.css` |
| 聊天与 Socket 事件 | `routes/chat.py`、`routes/chat_helpers.py`、`socket_events.py`、`frontend/src/app/chat.ts`、`agents/latex.py`、`frontend/src/app/latex.ts`、`styles/01-foundation-chat.css`；LaTeX 开关位于 `general.toml [chat] latex_*` |
| 角色卡/COC7 检定 | `routes/characters.py`、`character_ai.py`、`frontend/src/app/character-sheet.ts`、`agents/tools/dice.py` |
| 骰子动画/玩家投掷/贴图 | `agents/dice_settings.py`、`agents/player_roll.py`、`routes/dice_rolls.py`、`routes/dice_textures.py`、`dice_textures.py`、`frontend/src/app/diceTable.ts`、`frontend/src/app/dice/*`、`frontend/src/dice3d/main.ts`、`styles/05-dice-animation.css` |
| 权限/角色 | `permission_config.py`、`role_config.py`、`routes/users.py`、`routes/auth.py` |
| 向量检索/Embedding | `agents/vector_store.py`、`agents/embedding_provider.py`、`setup_embedding.py`、`routes/vector_health.py` |
| 房间控制台 | `room_console_commands.py`、`room_log.py`、`routes/room_console.py`、`frontend/src/app/room-console.ts` |
| 网站 Agent | `site_agent.py`、`site_agent_auth.py`、`site_agent_config.py`、`site_agent_prompt.py`、`site_agent_store.py`、`site_agent_tools/*`（含 `settings` / `profile` / `rooms` 消息工具）、`routes/site_agent.py`、`frontend/src/react/agent/*`；写操作走 `character_service.py` / `scenario_service.py`；网站设置读写额外要求 ADMIN/OWNER |
| 房间 AI 用量 | `room_usage.py`、`agents/telemetry.py`、`routes/rooms.py`、`frontend/src/app/rooms.ts`、`frontend/src/react/home/HomeChat.tsx`；仅房主/房间管理员可见 |
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
- 禁止再用 `tomllib` 直读 `general.toml` 或自带默认值兜底：模板、env、CLI 的覆盖会因此失效，schema 才是唯一权威。
- 历史脏数据里根级带引号的点分表（`["ai.small_models"]`）写入前必须归一为嵌套表，否则会与新建点分表并存并生成非法 TOML。
- 静态 `/config/<path>` 必须继续封禁 `general.toml`、`site.json`、`network.json`、`penetration.json`：`general.toml` 含穿透令牌，明文外泄即安全事件。
- 遗留 JSON 兼容层优先级低于用户 TOML、高于模板，只在 TOML 缺键时生效；`config migrate` 也只写缺失键，保证迁移前后生效值一致。
- 测试 `test_backend_project_structure.py` / `test_frontend_project_structure.py` 会校验目录边界，移动文件前先看它们。

## 11. 相关文档

- [API 概览](./api.md)：按蓝图分组的全部 HTTP API 与 Socket.IO 事件
- [开发说明](./development.md)：架构边界、配置规范、验证流程、日志/文件/前端规范
- [控制台命令](./console-commands.md)
- [剧本格式](./scenario-format.md)、[剧本导入](./scenario-import.md)
- [向量库](./vector-store.md)
- 本地 Wiki：`docs/the-veil.wiki/`
