# 帷幕 The Veil · AI 跑团平台 v0.5.3b

这是可用于 GitHub Release 的源码发布包，包含后端源码、前端源码、已构建的 `dist/public`、默认配置与一键启动脚本。

## 发布前检查

- 前端 TypeScript 类型检查：通过
- 前端生产构建：通过
- 后端测试：`pytest` 603 passed
- 发布包敏感信息扫描：不应包含本地 `.env`、运行时密钥、日志、缓存、本地数据库或依赖目录
- 发布包内容核对：不应包含 `.gitignore`、`.gitattributes`、`docs/`、`tests/`、`data/scenarios/`、`data/runtime/`

## 打包原则

- **不预置模型提供商**：不包含 `data/config/aiplatform/*.json`（仅保留 `default-request.json`）与 `data/config/aimodel/**`。内置供应商只以「模板」形式存在于前端，用户在「添加平台」中自行创建第一条。
- **首次部署引导开启**：`data/config/site.json` 的 `setup_dismissed=false`，且不包含任何 OWNER 数据，启动后即进入首次部署引导（README 的「一分钟开始 / 第一次打开会自动引导」即站点部署引导）。
- **不打包运行时会话数据**：不包含 `data/runtime/**`（用户、房间、聊天记录、日志、向量库、密钥）与 `data/scenarios/**`（现存剧本）及 `backend/data/**`。
- **不含自动构建**：`server.py` 的前端自动构建仅在开发环境设置 `AI_TRPG_DEV_BUILD=1` 时启用；发布包直接使用随包的 `dist/` 构建产物。
- **不含 Git 元文件与开发文档**：不打包 `.gitignore`、`.gitattributes` 与 `docs/`。发布包是解压即用的运行副本、不是 Git 工作区，这些文件在包内不起作用；其中 `.gitignore` 还会暴露内部开发路径。
- **不含开发侧内容**：不打包 `tests/` 与开发 / 校验脚本（如 `scripts/verify.ps1`）。
- **保留一键启动脚本**：`start.cmd` 与根目录的 `start-bootstrap.ps1` 必须打包，二者共同实现「无需预装 Python 的一键启动」。
- **随包提供许可证清单**：`THIRD-PARTY-LICENSES.md` 列出运行与构建所用第三方依赖及其许可证，供合规分发参考。
- **随包提供自托管指南**：根目录的 `SELF-HOSTING-GUIDE.txt` 必须复制进发布包根目录。该文件**不被 Git 跟踪**（已在 `.gitignore` 中忽略），打包时需要从工作区手动带入；它是给普通用户看的纯文本安装、启动、联机、备份与排错说明。

## 启动

1. Windows 下双击 `start.cmd` 启动：它会先运行 `start-bootstrap.ps1` 准备环境（缺 Python 时自动安装，再创建 `.venv` 并安装 `requirements.txt` 依赖），随后启动 `server.py`；无需预先安装 Python，也无需预先 `pip install`，不受 PowerShell 执行策略限制。其他系统运行 `python server.py`。
2. 发布包使用随包的 `dist/` 构建产物，无需构建前端；如需自行重新构建，安装 Node.js 依赖后运行 `npm run build:frontend`。
3. 打开页面，按首次部署引导完成 AI 平台、站点与 owner 账号配置。

AI 平台 API key 仅应通过运行时配置写入 `data/runtime/config/aiplatform/` 或环境变量提供，不要提交到 Git 或发布包。

## 本次更新

详见仓库 `release/changelog/v0.5.3b.md`（changelog 不随发布包分发）。
