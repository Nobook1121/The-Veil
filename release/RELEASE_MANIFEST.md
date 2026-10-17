# 帷幕 The Veil · AI 跑团平台 v0.5.0

这是可用于 GitHub Release 的源码发布包，包含后端源码、前端源码、已构建的 `dist/public`、默认配置与运行脚本。

## 发布前检查

- `scripts/verify.ps1`：通过（Python 语法检查、前端 TypeScript 类型检查与生产构建、`node --check` 产物校验、`pytest` 312 passed）
- 发布包敏感信息扫描：不应包含本地 `.env`、运行时密钥、日志、缓存、本地数据库或依赖目录
- 发布包内容核对：不应包含 `.gitignore`、`.gitattributes`、`docs/`

## 打包原则

- **不预置模型提供商**：不包含 `data/config/aiplatform/*.json`（仅保留 `default-request.json`）与 `data/config/aimodel/**`。内置供应商只以「模板」形式存在于前端，用户在「添加平台」中自行创建第一条。
- **首次部署引导开启**：`data/config/site.json` 的 `setup_dismissed=false`，且不包含任何 OWNER 数据，启动后即进入首次部署引导。
- **不含自动构建**：`server.py` 的前端自动构建仅在开发环境设置 `AI_TRPG_DEV_BUILD=1` 时启用；发布包直接使用随包的 `dist/` 构建产物。
- **不含 Git 元文件与开发文档**：不打包 `.gitignore`、`.gitattributes` 与 `docs/`。发布包是解压即用的运行副本、不是 Git 工作区，这些文件在包内不起作用；其中 `.gitignore` 还会暴露内部开发路径。
- **不含开发侧内容**：不打包 `tests/`、开发 / 校验脚本（如 `scripts/verify.ps1`）与内部计划文档。
- **保留一键启动脚本**：`start.cmd` 与根目录的 `start-bootstrap.ps1` 必须打包，二者共同实现「无需预装 Python 的一键启动」。

## 启动

1. Windows 下双击 `start.cmd` 启动：它会先运行 `start-bootstrap.ps1` 准备环境（缺 Python 时自动安装，再创建 `.venv` 并安装 `requirements.txt` 依赖），随后启动 `server.py`；无需预先安装 Python，也无需预先 `pip install`，不受 PowerShell 执行策略限制。其他系统运行 `python server.py`。
2. 发布包使用随包的 `dist/` 构建产物，无需构建前端；如需自行重新构建，安装 Node.js 依赖后运行 `npm run build:frontend`。
3. 打开页面，按首次部署引导完成 AI 平台、站点与 owner 账号配置。

AI 平台 API key 仅应通过运行时配置写入 `data/runtime/config/aiplatform/` 或环境变量提供，不要提交到 Git 或发布包。

## 本次更新

详见仓库 `release/changelog/v0.5.0.md`。