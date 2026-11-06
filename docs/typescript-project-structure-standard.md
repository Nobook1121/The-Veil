# TypeScript 项目文件结构与命名规范

本文基于当前仓库 `C:\Mine\AIbased TRPG` 的实际文件盘点，输出四部分内容：现状诊断、规范化目录方案、项目文件标准、落地维护建议。

本文最初用于输出方案和标准；当前已完成两阶段落地：前端浏览器源码迁入 `frontend/src/app/`，手写类型迁入 `frontend/src/types/`，前端产物统一输出到 `dist/public/`，浏览器 URL 继续兼容 `/js/...` 和 `/data/tools/...`。

## 第一部分：现状诊断报告

### 1.1 项目技术栈判断

当前项目不是纯 TypeScript 项目，而是 Python Flask 后端加 TypeScript/React 前端的全栈项目：

- 后端源码：`backend/trpg_server/`、`server.py`
- 前端源码：`frontend/src/`
- 前端编译/打包产物：`dist/public/`
- 运行数据与静态资源：`data/`
- 工程脚本：`scripts/`
- 测试：`tests/`
- 文档：`docs/`

因此目录标准需要同时保留 Python 后端入口、Flask 静态路径、前端构建输出和运行数据边界。

### 1.2 文件盘点统计

排除 `.git/`、`node_modules/`、`.worktrees/`、`__pycache__/`、`.pytest_cache/` 后，当前主要文件数量如下：

| 顶级路径 | 文件数 | 判定 |
|---|---:|---|
| `data/` | 1090 | 运行数据、业务配置、静态资源、生成脚本产物混合 |
| `frontend/` | 50 | 前端源码加部分构建产物 |
| `backend/trpg_server/` | 39 | Python 后端源码 |
| `js/` | 31 | 前端构建产物，应忽略 |
| `docs/` | 13 | 文档，但含敏感/测试数据候选 |
| `tests/` | 6 | 测试源码 |
| `scripts/` | 3 | 工程脚本 |
| 根目录配置 | 12 | `package.json`、`tsconfig*.json`、`README.md` 等 |

按扩展名统计：

| 扩展名 | 数量 | 说明 |
|---|---:|---|
| `.log` | 1042 | 运行日志，应全部忽略 |
| `.py` | 46 | 后端源码与测试 |
| `.json` | 33 | 配置、数据、测试样例、运行数据混合 |
| `.js` | 32 | 构建产物为主 |
| `.ts` | 30 | 前端 TS 源码与手写声明 |
| `.md` | 13 | 文档、角色提示词、规范 |
| `.html` | 10 | 前端模板源码与构建 HTML |
| `.css` | 8 | 前端样式源码和 React 构建 CSS |
| `.tsx` | 3 | React 源码 |
| `.secret` | 1 | 敏感文件候选，不应入库 |
| `.sqlite3` | 1 | 本地用户数据库，不应入库 |

### 1.3 完整层级结构摘要

当前有效层级如下，省略大批量日志文件明细：

```text
.
|-- data/
|   |-- assets/
|   |   |-- aiplatform/
|   |   |-- avatars/
|   |   |-- scenario_covers/
|   |   `-- vendor/font-awesome/
|   |-- characters/
|   |-- config/
|   |   |-- aimodel/
|   |   |-- aiplatform/
|   |   `-- roles/
|   |-- history/
|   |-- logs/
|   |-- occupations/builtin/
|   |-- rooms/
|   |-- scenarios/
|   |-- tools/
|   |-- users/
|   `-- weapons/builtin/
|-- docs/
|   |-- patterns/
|   |-- superpowers/
|   |   |-- plans/
|   |   `-- specs/
|   |-- api.md
|   |-- character_skills.md
|   |-- development.md
|   |-- typescript-security-migration.md
|   |-- Test_charactor.json
|   |-- anythingllm.secret
|   `-- 问题点.md
|-- frontend/
|   |-- dist/index.html
|   `-- src/
|       |-- index/
|       |-- js/
|       |   |-- auth/
|       |   |-- config/
|       |   |-- controllers/
|       |   |-- generated/
|       |   |-- models/
|       |   |-- views/
|       |   `-- types.d.ts
|       |-- react/
|       |-- styles/
|       |-- templates/
|       `-- tools/
|-- js/
|   |-- auth/
|   |-- config/
|   |-- controllers/
|   |-- generated/
|   |-- models/
|   |-- react/
|   |-- tools/
|   `-- views/
|-- scripts/
|-- tests/
|-- backend/
|   `-- trpg_server/
|   |-- agents/
|   |-- routes/
|   `-- users/
|-- package.json
|-- tsconfig.json
|-- tsconfig.frontend.json
|-- tsconfig.react.json
`-- server.py
```

### 1.4 现存问题清单与严重程度

| 严重程度 | 问题 | 当前证据 | 风险 |
|---|---|---|---|
| 已整改 | 构建产物曾输出到仓库根级 `js/` | 当前 `tsconfig.frontend.json` 输出到 `dist/public`，脚本再把 `app` 产物映射到浏览器 `/js` URL | 保持 `dist/public/` 忽略并由构建重建 |
| 已降低 | 生成源码文件放在 `frontend/src/app/generated/` | `frontend/src/app/generated/templates.ts` 标注由 `scripts/build-frontend.mjs` 生成，构建时重建 | 不手改该文件；长期可继续迁到 `frontend/generated/` |
| 已整改 | 敏感文件候选位于文档目录 | `docs/anythingllm.secret` 已迁出到本地 `.env.local` | `.gitignore` 忽略 `*.secret` 和本地 env |
| 已整改 | 前端产物曾分散在 `js/`、`data/tools/`、`frontend/dist/` | 当前统一位于 `dist/public/` | 构建清理和 Flask 静态服务已统一 |
| 中 | `config` 目录职责重复 | `data/config/`、`frontend/src/app/config/`、`js/config/` | 业务配置、前端配置类源码、编译产物同名但职责不同 |
| 已整改 | `tools` 源码和产物分离方式特殊 | `frontend/src/tools/*.ts` 编译到 `dist/public/tools/` 后被脚本移动到 `dist/public/data/tools/` | URL 兼容，产物边界清晰 |
| 已整改 | `.gitignore` 规则包含全量 `/docs` 和 `/tests/*` | 当前文档和测试默认可提交 | 新文档和新测试不再默认漏提交 |
| 已整改 | 运行数据和种子数据同处 `data/` | 默认运行写入位置已迁到 `data/runtime/`，`data/config`、`data/assets` 继续作为版本化资源 | 旧运行目录仍在 `.gitignore` 中防止遗留数据误提交 |
| 已整改 | 文件命名存在非英文和拼写问题 | `tests/fixtures/test_character.json` 已使用英文工程名；内容资产仍可保留中文 | 内容资产边界继续按数据目录规则管理 |
| 已整改 | 构建产物没有统一 `dist/` 目录 | 当前使用 `dist/public/` | 与常见 TS 项目约定一致 |

### 1.5 重复用途文件夹对照表

| 用途 | 路径 | 内容 | 冲突点 | 处理结论 |
|---|---|---|---|---|
| `config` | `data/config/` | 运行时 TOML/JSON、AI 平台配置、角色提示词 | 业务运行配置 | 保留为 `data/config/` 或未来迁至 `config/runtime/` |
| `config` | `frontend/src/app/config/` | `AIPlatformManager.ts`、`ConfigManager.ts`、`TestRequestConfig.ts` | 前端配置管理源码 | 保留在前端源码，建议未来改为 `frontend/src/config/` |
| `config` | `js/config/` | 上述 TS 编译后的 JS | 构建产物 | 归入产物目录，保持忽略 |
| `tools` | `frontend/src/tools/` | `diceTool.ts`、`toolManager.ts` | 前端工具源码 | 保留源码路径或未来并入 `frontend/src/app/tools/` |
| `tools` | `js/tools/` | TS 编译中间产物 | 构建产物 | 应由构建清理或避免落地 |
| `tools` | `dist/public/data/tools/` | 最终浏览器加载脚本 | 构建产物，URL 兼容 `/data/tools/...` | 保持忽略并由构建生成 |
| `generated` | `frontend/src/app/generated/` | `templates.ts` | 自动生成 TS 文件 | 建议加明确生成规则，长期迁至 `frontend/generated/` 或构建缓存 |
| `generated` | `js/generated/` | `templates.js` | 编译产物 | 忽略，不入库 |

当前类型声明已拆分到 `frontend/src/types/`，根目录 `types/` 仍仅保留给第三方模块补丁。

### 1.6 类型文件专项说明

| 文件 | 判定 | 依据 | 建议 |
|---|---|---|---|
| `frontend/src/types/*.d.ts` | 手写全局/业务/第三方声明文件 | 内容按 core、vendor、scenario、room、config、global 等职责拆分 | 保持在 `frontend/src/types/`，避免回流到业务源码目录 |
| `node_modules/**/*.d.ts` | 第三方依赖类型 | 包管理器安装 | 不纳入项目规范迁移 |
| `js/**/*.d.ts` | 当前无 | 未发现 tsc 生成声明 | 如未来开启 `declaration`，必须输出到 `dist/types/` |

### 1.7 构建产物与运行产物分布

| 路径 | 类型 | 当前来源 | 是否纳入版本控制 | 建议 |
|---|---|---|---|---|
| `dist/public/js/**/*.js` | TS/React 构建产物 | `tsc --project tsconfig.frontend.json`、`scripts/relocate-tools.mjs`、`esbuild` | 否 | 保持忽略，通过 `npm run build:frontend` 重建 |
| `dist/public/js/react/main.css` | React CSS 构建产物 | esbuild 相关输出 | 否 | 保持忽略 |
| `dist/public/index.html` | HTML 构建产物 | `scripts/build-frontend.mjs` 拼接生成 | 否 | 保持忽略，并由构建生成 |
| `frontend/src/app/generated/templates.ts` | 生成源码中间文件 | `scripts/build-frontend.mjs` | 当前作为中间文件保留 | 不手改；构建时重建 |
| `dist/public/data/tools/*.js` | 前端工具 JS 产物 | `scripts/relocate-tools.mjs` | 否 | 保持忽略，URL 兼容 `/data/tools/...` |
| `data/runtime/logs/*.log` | 运行日志 | 服务运行生成 | 否 | 保持忽略 |
| `data/runtime/history/*.json` | 运行历史 | 服务运行生成 | 否 | 保持忽略 |
| `data/runtime/rooms/**` | 房间运行数据 | 服务运行生成 | 否 | 保持忽略 |
| `data/runtime/users/users.sqlite3` | 用户数据库 | 服务运行生成 | 否 | 保持忽略 |
| `.pytest_cache/`、`__pycache__/` | 测试/解释器缓存 | Python 工具生成 | 否 | 保持忽略 |
| `node_modules/` | 依赖产物 | npm install | 否 | 保持忽略 |

## 第二部分：规范化目录结构方案

### 2.1 推荐目标目录树

该目标结构兼容当前 Flask 加 TS 前端项目。短期可保留现有运行路径；长期按下列结构收敛：

```text
.
|-- backend/
|   `-- trpg_server/                 # Python 后端源码
|-- frontend/
|   |-- src/
|   |   |-- app/                     # 前端业务 TS，可由现有 js/ 逐步迁入
|   |   |-- react/                   # React island/runtime 源码
|   |   |-- styles/                  # 手写 CSS
|   |   |-- templates/               # 手写 HTML template
|   |   |-- index/                   # HTML 拼装片段和 manifest
|   |   |-- tools/                   # 手写前端工具 TS
|   |   `-- types/                   # 手写全局/跨模块类型
|-- dist/
|   `-- public/                      # 统一前端 HTML/JS/CSS 产物，忽略
|-- data/
|   |-- assets/                      # 版本化静态资源、第三方字体、图标
|   |-- config/                      # 版本化默认运行配置
|   |-- occupations/
|   |-- weapons/
|   |-- scenarios/                   # 示例/内置剧本；用户上传剧本另设 runtime
|   `-- runtime/                     # 长期运行数据根，忽略
|       |-- characters/
|       |-- history/
|       |-- logs/
|       |-- rooms/
|       `-- users/
|-- docs/
|-- scripts/
|-- tests/
|-- types/                           # 第三方模块补丁；当前无需创建
|-- package.json
|-- package-lock.json
|-- tsconfig.json
|-- tsconfig.frontend.json
|-- tsconfig.react.json
|-- requirements.txt
|-- server.py
`-- README.md
```

### 2.2 分阶段迁移策略

推荐采用两阶段方案：

1. 保守阶段：只修正规范、忽略规则和生成物边界，不改 Flask 路由和 HTML 引用路径。
2. 收敛阶段：统一前端产物到 `dist/public/`，再同步修改 Flask 静态服务路径、HTML 脚本引用、构建脚本和测试。

后端源码已迁到 `backend/trpg_server/`。根目录 `server.py` 保留为兼容启动入口，并在启动时把 `backend/` 加入 Python import path，因此公开 import 名仍为 `trpg_server.*`。

### 2.3 全量文件迁移映射表

#### 根目录与工程配置

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `.gitattributes` | `.gitattributes` | 保留 |
| `.gitignore` | `.gitignore` | 保留并修正规则 |
| `LICENSE` | `LICENSE` | 保留 |
| `README.md` | `README.md` | 保留 |
| `package.json` | `package.json` | 保留 |
| `package-lock.json` | `package-lock.json` | 保留 |
| `requirements.txt` | `requirements.txt` | 保留 |
| `server.py` | `server.py` | 保留为兼容启动入口 |
| `tsconfig.json` | `tsconfig.json` | 保留 |
| `tsconfig.frontend.json` | `tsconfig.frontend.json` | 保留并调整输出目录 |
| `tsconfig.react.json` | `tsconfig.react.json` | 保留 |

#### 后端源码

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `trpg_server/**/*.py` | `backend/trpg_server/**/*.py` | 已迁移，import 名保持 `trpg_server.*` |

#### 前端手写源码

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `frontend/src/app/*.ts` | `frontend/src/app/*.ts` | 已迁移并保留 |
| `frontend/src/app/auth/*.ts` | `frontend/src/app/auth/*.ts` | 已迁移并保留 |
| `frontend/src/app/config/*.ts` | `frontend/src/app/config/*.ts` | 已迁移并保留 |
| `frontend/src/app/controllers/*.ts` | `frontend/src/app/controllers/*.ts` | 已迁移并保留 |
| `frontend/src/app/models/*.ts` | `frontend/src/app/models/*.ts` | 已迁移并保留 |
| `frontend/src/app/views/*.ts` | `frontend/src/app/views/*.ts` | 已迁移并保留 |
| `frontend/src/tools/*.ts` | `frontend/src/tools/*.ts` | 保留 |
| `frontend/src/react/**/*.tsx` | `frontend/src/react/**/*.tsx` | 保留 |
| `frontend/src/react/**/*.css` | `frontend/src/react/**/*.css` | 保留 |
| `frontend/src/styles/*.css` | `frontend/src/styles/*.css` | 保留 |
| `frontend/src/templates/*.html` | `frontend/src/templates/*.html` | 保留 |
| `frontend/src/index/**` | `frontend/src/index/**` | 保留 |
| `frontend/src/types/*.d.ts` | `frontend/src/types/*.d.ts` | 已拆分迁移 |
| `frontend/src/app/generated/templates.ts` | `frontend/src/app/generated/templates.ts` | 暂保留为可重建中间文件 |

#### 前端构建产物

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `js/**/*.js` | `dist/public/js/**/*.js` | 已迁移，保留浏览器 URL 兼容 |
| `js/react/main.css` | `dist/public/js/react/main.css` | 已迁移，保留浏览器 URL 兼容 |
| `frontend/dist/index.html` | `dist/public/index.html` | 已迁移为忽略产物 |
| `data/tools/*.js` | `dist/public/data/tools/*.js` | 已迁移，保留浏览器 URL 兼容 |

#### 数据、静态资源与运行数据

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `data/assets/**` | `data/assets/**` | 保留，版本化静态资源 |
| `data/config/**` | `data/config/**` | 保留，版本化默认运行配置 |
| `data/occupations/builtin/**` | `data/occupations/builtin/**` | 保留，内置数据 |
| `data/weapons/builtin/**` | `data/weapons/builtin/**` | 保留，内置数据 |
| `data/scenarios/长生俑.json` | `data/scenarios/changsheng_yong.json` 或保留原名 | 若作为内容资产可保留中文；若按工程规范需英文 |
| `data/characters/*.json` | `data/runtime/characters/*.json` | 已将默认运行路径迁移到 runtime；旧目录仅作遗留忽略 |
| `data/history/*.json` | `data/runtime/history/*.json` | 已将默认运行路径迁移到 runtime；旧目录仅作遗留忽略 |
| `data/logs/*.log` | `data/runtime/logs/*.log` | 已将默认运行路径迁移到 runtime；旧目录仅作遗留忽略 |
| `data/rooms/**` | `data/runtime/rooms/**` | 已将默认运行路径迁移到 runtime；旧目录仅作遗留忽略 |
| `data/users/**` | `data/runtime/users/**` | 已将默认运行路径迁移到 runtime；旧目录仅作遗留忽略 |

#### 文档与测试

| 原路径 | 目标路径 | 动作 |
|---|---|---|
| `docs/api.md` | `docs/api.md` | 保留 |
| `docs/development.md` | `docs/development.md` | 保留 |
| `docs/character_skills.md` | `docs/character_skills.md` | 保留 |
| `docs/typescript-security-migration.md` | `docs/typescript-security-migration.md` | 保留 |
| `docs/patterns/*.md` | `docs/patterns/*.md` | 保留 |
| `docs/superpowers/**` | `docs/superpowers/**` | 保留 |
| `docs/问题点.md` | 不入库（本地保留） | 已移出仓库并加入忽略规则 |
| `docs/Test_charactor.json` | `tests/fixtures/test_character.json` | 已迁移，且修正 `charactor` 拼写 |
| `docs/anythingllm.secret` | 不入库；本地迁至 `.env.local` 或安全凭证库 | 必须忽略 |
| `tests/test_*.py` | `tests/test_*.py` | 保留并取消 `.gitignore` 的全量忽略 |

### 2.4 同名目录合并/拆分决策

- `data/config/` 是业务运行配置，不与前端源码配置类合并。
- `frontend/src/app/config/` 是前端源码目录，不与 `data/config/` 的业务运行配置合并。
- `js/config/` 是编译产物，应只存在于构建输出目录并被忽略。
- `frontend/src/types/*.d.ts` 是手写全局类型，应迁入 `frontend/src/types/`；根目录 `types/` 只用于第三方模块补丁。
- `frontend/src/app/generated/` 是生成中间文件，应移出手写源码目录或明确在文件头标识并纳入生成流程。

### 2.5 `tsconfig` 适配建议

短期保守方案：

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "module": "none",
    "moduleResolution": "Classic",
    "strict": true,
    "noEmit": false,
    "rootDir": "frontend/src",
    "outDir": "dist/public"
  },
  "include": [
    "frontend/src/app/**/*.ts",
    "frontend/src/tools/**/*.ts",
    "frontend/src/app/generated/**/*.ts"
  ],
  "exclude": [
    "node_modules",
    "js",
    "frontend/dist",
    "dist",
    "data/runtime",
    "data/logs",
    "data/history",
    "data/rooms",
    "data/users"
  ]
}
```

当前已将 `outDir` 从 `.` 改为 `dist/public`，并已同步修改：

- `scripts/relocate-tools.mjs`
- `package.json` 中的 `build:react` 输出路径
- `dist/public/index.html` 或模板片段中的 `<script src="js/...">`
- Flask 静态文件服务路径
- 相关测试中对构建产物路径的断言

当前构建流程已直接使用 `tsconfig.frontend.json` 输出到 `dist/public`，不再需要并行的 `tsconfig.frontend.dist.json`。

### 2.6 `.gitignore` 推荐规则

建议把当前过宽的 `/docs`、`/tests/*` 改成精确忽略运行产物：

```gitignore
# Dependencies
/node_modules/

# TypeScript and frontend build outputs
/js/
/dist/
/frontend/dist/
/data/tools/*.js
/frontend/generated/

# Python caches
__pycache__/
*.py[cod]
.pytest_cache/
tests/**/__pycache__/
backend/trpg_server/**/__pycache__/

# Runtime data
/data/runtime/
/data/history/
/data/logs/
/data/rooms/
/data/users/
/data/characters/*
/data/scenarios/*
!/data/scenarios/.gitkeep

# Local worktrees
/.worktrees/

# Logs and temp files
*.log
*.tmp
/tmp/
/temp/

# Secrets and local env
.env
.env.*
!.env.example
*.secret
docs/*.secret

# Local databases
*.sqlite3
*.db
```

如果 `data/scenarios/` 需要提交内置剧本，应改成：

```gitignore
/data/scenarios/user/
!/data/scenarios/builtin/
```

## 第三部分：项目文件结构与命名规范标准

### 3.1 顶级目录职责

| 目录 | 职责 | 是否提交 |
|---|---|---|
| `backend/trpg_server/` | Python 后端源码 | 是 |
| `frontend/src/` | 前端手写源码、模板、样式、类型 | 是 |
| `frontend/src/app/generated/` | 前端生成中间文件 | 是；由构建脚本重建，不手改 |
| `frontend/dist/` | 旧前端 HTML 产物目录 | 否，已停用 |
| `js/` | 旧浏览器 JS/CSS 产物目录 | 否，已停用 |
| `dist/` | 统一构建产物目录 | 否 |
| `data/assets/` | 版本化静态资源 | 是 |
| `data/config/` | 默认业务运行配置 | 是 |
| `data/runtime/` | 用户数据、日志、房间、历史、数据库 | 否 |
| `docs/` | 项目文档 | 是 |
| `scripts/` | 工程脚本 | 是 |
| `tests/` | 自动化测试 | 是 |
| `types/` | 第三方模块类型补丁 | 是，当前无需创建 |

### 3.2 命名规则

- 工程目录使用小写英文和下划线，已有生态约定目录如 `src`、`docs`、`config`、`dist` 保持原样。
- TypeScript 普通模块使用 camelCase，例如 `apiClient.ts`。
- 类、React 组件、构造器类文件使用 PascalCase，例如 `ScenarioController.ts`、`Sidebar.tsx`。
- Python 文件继续使用 snake_case，例如 `app_factory.py`。
- 配置文件遵循工具约定，例如 `tsconfig.json`、`package.json`、`.env.example`。
- 生成文件必须在文件头声明来源，例如 `This file is generated by ... Do not edit by hand.`
- 工程源码文件名不使用中文、空格、日期、人名或临时版本号。
- 内容资产可以使用中文文件名，但必须放在数据/资源目录，不能作为工程源码模块名。

### 3.3 前端源码分层

当前结构：

```text
frontend/src/
|-- index/       # HTML 拼装片段和 manifest
|-- app/         # 非模块化浏览器 TS 业务源码
|-- react/       # React island 源码
|-- styles/      # 全局手写 CSS
|-- templates/   # 手写 HTML templates
|-- tools/       # 前端工具 TS
`-- types/       # 手写类型声明
```

如需继续收敛，可在 `app/` 内新增 `utils/` 等更细目录：

```text
frontend/src/
|-- app/
|   |-- auth/
|   |-- config/
|   |-- controllers/
|   |-- models/
|   |-- views/
|   `-- utils/
|-- react/
|-- styles/
|-- templates/
|-- tools/
`-- types/
```

### 3.4 类型文件规则

- 模块私有类型放在模块旁边或模块内 `types/`。
- 跨前端复用类型放在 `frontend/src/types/`。
- 浏览器全局扩展和第三方全局变量声明放在 `frontend/src/types/global.d.ts` 或 `vendor.d.ts`。
- 第三方模块补丁放在根目录 `types/<module-name>/index.d.ts`。
- 编译生成的 `.d.ts` 必须输出到 `dist/types/`，不得进入 `frontend/src/`。
- `frontend/src/types/*.d.ts` 已拆分，后续新增类型应继续按职责拆到相应文件，避免重新形成集中声明文件。

### 3.5 构建产物规则

- 可通过 `npm run build:frontend` 重建的文件一律视为产物。
- `dist/public/` 不应手工修改；旧 `js/`、`frontend/dist/`、`data/tools/*.js` 不应重新引入。
- 若必须提交生成物，必须在 README 和文件头说明原因，并提供重建命令。
- 构建前清理产物目录，避免旧文件残留。
- 禁止将 `tsc` 输出放入仓库根目录；当前 `outDir: "."` 是历史兼容方案，应逐步替换。

### 3.6 数据和配置规则

- `data/config/` 只存默认配置、内置模型配置、角色提示词等可版本化数据。
- `data/assets/` 存静态资源和第三方前端资源。
- 用户上传、房间、历史、日志、数据库统一视为运行数据，不提交。
- 示例/内置数据与用户数据分开，例如 `data/scenarios/builtin/` 和 `data/scenarios/user/`。
- `.env.example` 可提交，`.env.local`、`.env.production`、`*.secret` 不提交。
- 敏感信息不得放入 `docs/`。

### 3.7 新增文件存放判断流程

```text
1. 判断文件是否人工维护。
   |-- 否：放到构建产物目录或运行数据目录，并加入 .gitignore。
   `-- 是：进入下一步。

2. 判断文件类型。
   |-- 后端源码：backend/trpg_server/
   |-- 前端源码：frontend/src/
   |-- 前端全局类型：frontend/src/types/
   |-- 第三方类型补丁：types/
   |-- 工程脚本：scripts/
   |-- 文档：docs/
   |-- 测试：tests/
   |-- 默认运行配置：data/config/
   |-- 静态资源：data/assets/
   `-- 无法判断：misc/ 并标注待确认。

3. 校验命名。
   |-- TS 普通模块：camelCase
   |-- TS 类/组件：PascalCase
   |-- Python：snake_case
   |-- 配置：遵循工具官方命名

4. 校验是否含密钥、数据库、日志、缓存。
   |-- 是：不得提交。
   `-- 否：允许纳入版本控制。
```

## 第四部分：落地执行与维护建议

### 4.1 已完成迁移步骤

1. 修正 `.gitignore`：移除 `/docs` 和 `/tests/*` 的过宽规则，补充 `*.secret`、`frontend/dist/`、`dist/`、`data/runtime/`。
2. 处理敏感文件：`docs/anythingllm.secret` 已迁移到本地 `.env.local`，并确保不入库。
3. 建立类型目录：新增 `frontend/src/types/`，并按职责拆分全局、业务和第三方声明。
4. 处理生成文件：`frontend/src/app/generated/templates.ts` 明确为构建脚本生成文件，构建时重建，不手改。
5. 调整构建配置：`tsconfig.frontend.json` 直接输出到 `dist/public`。
6. 调整构建脚本：`build:react`、`relocate-tools.mjs`、HTML 产物路径已指向统一产物目录。
7. 修改 Flask 静态路径和测试：浏览器加载的 `/js/...`、`/data/tools/...` URL 保持可用。
8. 迁移运行数据默认目录：角色、历史、日志、房间和用户数据默认写入 `data/runtime/`。
9. 运行完整验证：执行 `.\scripts\verify.ps1`。
10. 清理历史产物：旧 `js/`、`frontend/dist/`、`data/tools/*.js` 已由构建脚本清理并可重建。

### 4.2 维护机制

- 每次新增 TS 文件时，先判断是手写源码、生成中间文件还是构建产物。
- PR 或提交前运行 `.\scripts\verify.ps1`。
- 文档和测试默认应可提交，不应被全量忽略。
- 生成文件必须能通过脚本重建，不能依赖手工复制。
- 每季度检查一次 `.gitignore` 是否覆盖新引入的缓存目录。
- 对 `data/` 新增子目录时，必须明确是版本化资源还是运行数据。

### 4.3 团队协作约定

- 不手改 `js/`、`frontend/dist/`、`data/tools/*.js`。
- 不把密钥、令牌、数据库、日志提交到仓库。
- 文档文件使用英文工程文件名；文档标题可以使用中文。
- 内容资产可以使用中文名称，但应在目录标准中说明其资产属性。
- 如果新增目录与现有目录同名，例如 `config`、`types`、`tools`，必须在 README 或本标准中说明职责边界。

### 4.4 当前项目的最小改造清单

优先级从高到低：

1. 已完成：`.gitignore` 删除 `/docs` 和 `/tests/*`，加入 `*.secret`、`docs/*.secret`、`frontend/dist/`、`dist/`。
2. 已完成：处理 `docs/anythingllm.secret`，避免敏感内容进入版本控制。
3. 已完成：把旧集中类型声明拆到 `frontend/src/types/`。
4. 已完成：为 `frontend/src/app/generated/templates.ts` 建立明确生成策略。
5. 已完成：设计并验证 `dist/public/` 输出路径，迁移旧 `js/` 和 `data/tools/` 产物。
6. 已完成：将 `docs/Test_charactor.json` 迁到 `tests/fixtures/test_character.json`。
7. 已完成：将默认运行数据目录迁到 `data/runtime/`。

以上步骤完成后，本项目的 TypeScript 源码、类型声明、构建产物、运行数据、文档和测试边界会清晰许多，后续新增文件也能按固定规则归位。

