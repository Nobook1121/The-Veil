---
app: The Veil (帷幕)
version: 1.0.0
package: the-veil-v1.0.0.zip
install_mode: bundled_frontend
python_requires: ">=3.11"
run_command_windows: ".venv\\Scripts\\python.exe server.py"
run_command_unix: "./.venv/bin/python server.py"
default_port: 8086
port_fallback: "8086-8099，启动时自动选择空闲端口"
health_path: "/"
health_expect: "HTTP 200，text/html，页面包含 setup 或应用外壳"
status_path: "/api/setup/status"
setup_path: "/"
setup_api_prefix: "/api/setup"
config_path: "data/config/general.toml"
config_dir_override: "--config-dir <PATH>"
log_path: "data/runtime/logs/"
data_path: "data/"
---

# The Veil 自动安装说明（面向 AI Agent）

本文件是给执行安装的 AI Agent 使用的操作手册，不是给人阅读的散文。请按顺序逐条执行，每一步都给出了「命令 / 期望输出 / 失败处理」。命令默认在解压后的发布包根目录执行（以下简称 `<APP_DIR>`）。

## 0. 执行前必须知道的事

- 运行方式：发布包已经内置构建好的前端产物 `dist/public`，**不需要 Node.js** 即可运行；Node 仅用于可选的源码重建。
- 数据与配置都写在 `<APP_DIR>/data/` 下，升级时保留该目录即可。
- 首次启动会自动生成 `data/config/general.toml`、数据库与日志目录。
- 引导接口（`/api/setup/*`）在系统还没有 OWNER 时开放，且免 CSRF；创建 OWNER 后自动锁定（返回 403）。
- 端口被占用时程序会自动向后寻找空闲端口。**以启动日志中 `listening on` 的实际端口为准**，不要假设一定是 8086。

### 禁止项（任何情况下都不要做）

1. 不要把 API Key、密码、`AI_TRPG_SECRET_KEY` 写进 `general.toml`、脚本、README 或聊天记录以外的任何地方；密钥只允许写入：
   - 环境变量 `AI_TRPG_SECRET_KEY`（会话密钥）；
   - 引导接口 `/api/setup/ai` 或设置页（由服务端写入 `data/config/aiplatform/` 与受保护的 secret 目录）。
2. 不要删除、改名或跳过 `LICENSE`、`THIRD-PARTY-LICENSES.md`。
3. 不要在未确认端口归属前强杀进程（不要使用 `kill -9`、`taskkill /F`、`Stop-Process -Force` 处理占用端口的进程）；应改用 `--port` 指定其他端口。
4. 不要降低安全配置：不要放宽 CORS（`AI_TRPG_CORS_ALLOWED_ORIGINS`）、不要关闭 SSRF 防护、不要绕过 session/CSRF 校验、不要关闭登录验证。
5. 不要手动编辑 `dist/public/**`；需要改前端时走源码构建。
6. 不要把真实密钥提交到 git，或写入发布包。

## 1. 前置条件检查

### 1.1 Python 版本

Windows PowerShell：

```powershell
python --version
```

Linux / macOS：

```bash
python3 --version
```

期望输出：`Python 3.11.x` 或更高（推荐 3.12）。

失败处理：若版本低于 3.11 或命令不存在，安装 Python 3.11+ 后重新执行。Windows 也可直接运行 `start.cmd`，它会自动准备 Python 与虚拟环境；但自动化场景建议按下文手动创建 venv。

### 1.2 pip 可用

```powershell
python -m pip --version
```

期望输出：`pip <版本> from ...`。

失败处理：`python -m ensurepip --upgrade`；仍失败则重装 Python 并勾选 pip。

### 1.3 端口占用（默认 8086）

Windows：

```powershell
Get-NetTCPConnection -LocalPort 8086 -State Listen -ErrorAction SilentlyContinue
```

期望输出：无输出（端口空闲）。

Linux / macOS：

```bash
(ss -ltn 2>/dev/null || netstat -ltn) | grep -E '[:.]8086\b' || true
```

期望输出：无输出。

失败处理：端口被占用时**不要杀进程**，改在启动命令加 `--port 8090`（或任一空闲端口），后续健康检查使用该端口。

### 1.4 磁盘空间

```powershell
Get-PSDrive -Name (Get-Location).Drive.Name | Select-Object Free
```

期望输出：`Free` ≥ 1 GB（依赖安装约 300 MB，加数据与日志建议保留 1 GB 余量）。

失败处理：清理磁盘或换到剩余空间更大的目录。

### 1.5 可选：Node.js（仅源码重建需要）

```powershell
node --version; npm --version
```

若只运行发布包内置前端，可跳过此项。

## 2. 安装依赖

### 2.1 Windows（PowerShell）

```powershell
cd <APP_DIR>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

期望输出：最后出现 `Successfully installed ...`，命令退出码 0。

失败处理：
- 网络超时：加镜像源重试，例如
  `.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
- 依赖编译失败：确认 Python 为 3.11/3.12（64 位），删除 `.venv` 后重试。
- 也可改用一键脚本：`powershell -NoProfile -ExecutionPolicy Bypass -File .\start-bootstrap.ps1`（会自动安装 Python 与依赖）。

### 2.2 Linux / macOS

```bash
cd <APP_DIR>
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

期望输出：同上。

失败处理：若 `python3 -m venv` 失败，先安装 venv 组件（Debian/Ubuntu：`sudo apt install python3-venv`）；网络问题同样用 `-i` 指定镜像源。

## 3. 配置写入

默认不需要手工编辑配置文件：首次启动会根据 `config/templates/general.toml` 生成 `data/config/general.toml`，随后通过引导接口写入站点名、AI 平台与可选功能开关。

必须由部署者提供的唯一密钥是会话密钥，写入环境变量（不要写入文件）：

Windows：

```powershell
$env:AI_TRPG_SECRET_KEY = "<RANDOM_SECRET>"
```

Linux / macOS：

```bash
export AI_TRPG_SECRET_KEY="<RANDOM_SECRET>"
```

期望输出：无报错。生成示例（不要复用固定值）：`python -c "import secrets; print(secrets.token_urlsafe(48))"`。

失败处理：若忘记设置，程序仍可启动但会使用开发默认密钥；自动安装时应视为失败，补设后重启。

其他配置优先级（不需要时保持默认）：代码默认 < 包内模板 < `data/config/general.toml` < `AI_TRPG_CFG_*` 环境变量 < `--set section.key=value` 命令行参数。

## 4. 启动服务

### 4.1 Windows（后台启动并记录 PID）

```powershell
New-Item -ItemType Directory -Force data\runtime | Out-Null
$proc = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList "server.py" -WorkingDirectory (Get-Location) -PassThru `
  -RedirectStandardOutput "data\runtime\agent-stdout.log" `
  -RedirectStandardError "data\runtime\agent-stderr.log"
$proc.Id | Set-Content data\runtime\agent.pid
```

期望输出：`data/runtime/agent.pid` 中存在一个进程号。

失败处理：读取 `data/runtime/agent-stderr.log`。若提示端口被占用，改用 `-ArgumentList "server.py", "--port", "8090"` 重启。

### 4.2 Linux / macOS（后台启动）

```bash
mkdir -p data/runtime
nohup ./.venv/bin/python server.py > data/runtime/agent-stdout.log 2>&1 &
echo $! > data/runtime/agent.pid
```

期望输出：`data/runtime/agent.pid` 中写入进程号。

失败处理：`tail -n 50 data/runtime/agent-stdout.log`；端口冲突加 `--port 8090`。

## 5. 健康检查

### 5.1 首页

Windows：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:<PORT>/ | Select-Object StatusCode
```

Linux / macOS：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<PORT>/
```

期望输出：`200`。

失败处理：等待 10 秒重试（首次启动要初始化目录与数据库）；仍失败时查看：
- `data/runtime/logs/` 下最新日志；
- `data/runtime/agent-stderr.log`；
- 确认进程仍存活（Windows `Get-Process -Id (Get-Content data\runtime\agent.pid)`；Unix `kill -0 $(cat data/runtime/agent.pid)`）。

### 5.2 初始化状态

Windows：

```powershell
Invoke-RestMethod http://127.0.0.1:<PORT>/api/setup/status | ConvertTo-Json -Depth 5
```

Linux / macOS：

```bash
curl -fsS http://127.0.0.1:<PORT>/api/setup/status
```

期望输出（全新实例）：`success` 为 `true`，`setup_required` 为 `true`，`owner_exists` 为 `false`。

若 `setup_required=false`：该实例已完成初始化（或已被手动跳过引导），不要重复创建 OWNER；直接跳到第 8 节验收。

## 6. 首次启动 setup（推荐用 API 完成）

按 **site → ai（可选）→ features → owner** 的顺序执行。`owner` 必须最后调用，因为它会把引导接口锁定。

以下示例使用 `<PORT>` 替换为实际端口，`<...>` 替换为真实值。所有请求都是 `Content-Type: application/json`，且不需要 CSRF token。

### 6.1 站点信息

```powershell
$body = @{ name = "The Veil"; domain = "" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:<PORT>/api/setup/site -ContentType 'application/json' -Body $body
```

```bash
curl -fsS -X POST http://127.0.0.1:<PORT>/api/setup/site \
  -H 'Content-Type: application/json' \
  -d '{"name":"The Veil","domain":""}'
```

期望输出：`success: true`。失败处理：`400` 表示 `name` 为空或过长；补齐后重试。

### 6.2 AI 平台（有 Key 时执行；没有可跳过）

DeepSeek 示例：

```json
{"platform":"deepseek","base_url":"https://api.deepseek.com/v1","api_key":"<API_KEY>","model":"deepseek-chat","model_name":"DeepSeek Chat","timeout":60}
```

OpenAI 兼容平台：

```json
{"platform":"openai","base_url":"https://api.openai.com/v1","api_key":"<API_KEY>","model":"gpt-4o-mini","model_name":"GPT-4o mini","timeout":60}
```

```powershell
$body = @{ platform="deepseek"; base_url="https://api.deepseek.com/v1"; api_key="<API_KEY>"; model="deepseek-chat"; model_name="DeepSeek Chat"; timeout=60 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:<PORT>/api/setup/ai -ContentType 'application/json' -Body $body
```

期望输出：`success: true`，`platform` 与 `model` 回显。

失败处理：`400 Invalid platform id` → `platform` 只能包含字母、数字、下划线、连字符；`400 Incomplete AI config` → 检查 `base_url` 与 `model`。不要在这里重试无效密钥，先向用户确认 Key。

### 6.3 可选功能（默认全部关闭）

```powershell
$body = @{ music = $false; illustration = $false; voice = $false } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:<PORT>/api/setup/features -ContentType 'application/json' -Body $body
```

期望输出：`success: true`，三个字段均为 `false`。

说明：三个值必须是 JSON 布尔值；字符串 `"false"` 会被拒绝（400）。除非用户明确要求，否则保持全 `false`，避免首次运行触发外部调用或产生费用。

### 6.4 创建 OWNER（最后执行）

```powershell
$body = @{ username="owner"; email="owner@example.com"; password="<PASSWORD>"; confirm_password="<PASSWORD>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:<PORT>/api/setup/owner -ContentType 'application/json' -Body $body
```

期望输出：`success: true`，`role` 为 `OWNER`，HTTP 201。

规则：用户名 3–32 位，仅 `A-Z a-z 0-9 _ . -`；邮箱需含 `@` 与域名；密码至少 8 位且同时包含字母与数字；`confirm_password` 必须一致。

失败处理：按返回 message 修正；不要重复调用——若已成功再调用会返回 `403 Setup already completed`。

### 6.5 确认引导完成

```powershell
Invoke-RestMethod http://127.0.0.1:<PORT>/api/setup/status | ConvertTo-Json -Depth 5
```

期望输出：`setup_required=false`、`owner_exists=true`。

若未创建 OWNER 但用户要求跳过引导：`POST /api/setup/dismiss`，之后 `setup_required=false` 但 `owner_exists=false`，此时站点没有管理员，必须向用户报告。

### 6.6 浏览器方式（备选）

打开 `http://127.0.0.1:<PORT>/`，按向导操作：AI 平台 → Embedding（可跳过）→ 站点名 → 域名（可跳过）→ 可选功能 → 创建 OWNER → 完成。完成页提供「进入系统」与「查看部署与访问文档」。

## 7. 失败回退

| 现象 | 处理 |
| --- | --- |
| `pip install` 失败 | 换镜像源、确认 Python 3.11/3.12 64 位、删除 `.venv` 重建 |
| 端口占用 | 用 `--port 8090`，不要强杀进程 |
| 首页 502/超时 | 等待 30 秒；检查进程与 `data/runtime/logs/` |
| `setup/status` 返回 `setup_required=false` 但无 OWNER | 实例被 dismiss 过；需要管理员时向用户确认后重新初始化 |
| AI 配置 400 | 检查 platform id、base_url、model；密钥错误不要重试 |
| 引导接口 403 | 已有 OWNER，引导已锁定；改用设置页或控制台命令 |
| 需要完全重置（危险） | 先让用户确认，再停止服务并备份 `data/`，然后删除 `data/config/`、`data/runtime/` 与用户数据库文件后重启；不要删除 `data/assets` 等资源 |

日志位置：
- 服务日志：`data/runtime/logs/`
- 后台标准输出：`data/runtime/agent-stdout.log`、`data/runtime/agent-stderr.log`
- 启动成功标志（日志中应出现）：`Server starting`、`Startup complete`、`listening on http://<IP>:<PORT>`

停止服务：使用 PID 文件正常终止（Windows `Stop-Process -Id <PID>`；Unix `kill <PID>`），不要使用强制终止。

## 8. 验收清单

- [ ] `python --version` ≥ 3.11，`pip` 可用
- [ ] `dist/public/index.html`、`dist/public/js/react/main.js`、`dist/public/js/react/main.css` 均存在
- [ ] `GET /` 返回 200，页面为 HTML
- [ ] `GET /api/setup/status` 返回 `success:true`
- [ ] 全新实例首访 `setup_required:true`
- [ ] 已完成：`setup_required:false` 且 `owner_exists:true`
- [ ] 可选功能 `music/illustration/voice` 均为 `false`（除非用户明确要求开启）
- [ ] 日志中出现 `listening on`，且端口与健康检查一致
- [ ] 未修改 `LICENSE` / `THIRD-PARTY-LICENSES.md`，未写入任何真实密钥
