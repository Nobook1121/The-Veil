# The Veil（帷幕）部署与访问指南

本文件随发布包分发，面向自行架设的部署者。自动化安装请优先阅读同目录的 `AGENT-INSTALL.md`。

## 1. 运行要求

- Python 3.11 或更高版本（推荐 3.12，64 位）
- 无需 Node.js：发布包内置已构建的 `dist/public`
- 磁盘空间：至少 1 GB 可用（依赖约 300 MB，另需数据与日志空间）
- 操作系统：Windows 10/11、Linux、macOS

## 2. Windows 一键启动

1. 将 `the-veil-v1.0.5.zip` 解压到路径中不含中文与空格的目录，例如 `D:\TheVeil`。
2. 双击 `start.cmd`。脚本会自动：
   - 查找 Python 3.11+，缺失时自动安装；
   - 创建独立虚拟环境 `.venv` 并安装依赖；
   - 启动服务。
3. 等待控制台出现类似输出：

```text
listening on http://127.0.0.1:8086
```

4. 浏览器访问日志中显示的地址。默认端口为 8086；若被占用，程序会自动选择附近的空闲端口，以日志为准。
5. 首次访问会进入初始化向导，见第 5 节。

命令行方式（可指定端口）：

```powershell
.\start.cmd 8090
```

## 3. Linux / macOS 手动部署

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python server.py
```

首次启动会自动生成 `data/config/general.toml`、数据库和 `data/runtime/logs/`。

长期运行建议使用 systemd（示例，路径按实际修改）：

```ini
[Unit]
Description=The Veil TRPG server
After=network.target

[Service]
WorkingDirectory=/opt/the-veil
Environment=AI_TRPG_SECRET_KEY=<RANDOM_SECRET>
ExecStart=/opt/the-veil/.venv/bin/python server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 4. 端口、配置与日志

- 默认端口：`8086`（配置项 `[network] port`）。
- 指定端口：`python server.py --port 8090` 或位置参数 `python server.py 8090`。
- 配置目录：`data/config/`；主配置为 `data/config/general.toml`。
- 多实例/自定义配置目录：`python server.py --config-dir D:\my-trpg-config`（首次启动会在该目录生成种子配置）。
- 命令行覆盖单项：`python server.py --set ai.max_tokens=8192 --set site.name=我的跑团站`。
- 环境变量覆盖：`AI_TRPG_CFG_` + 段名（点号转下划线）+ `_` + 键名，全大写，例如：
  - Windows：`$env:AI_TRPG_CFG_NETWORK_PORT = "9000"`
  - Linux：`export AI_TRPG_CFG_NETWORK_PORT=9000`
- 日志：`data/runtime/logs/`。
- 会话密钥：环境变量 `AI_TRPG_SECRET_KEY`，不要写入配置文件。

配置优先级：代码默认值 < 包内模板 < `general.toml` < 环境变量 < 命令行参数。

## 5. 首次初始化向导

全新实例访问根路径会自动进入引导（`/api/setup/status` 返回 `setup_required: true`）。

向导顺序：

1. AI 平台配置（DeepSeek 或任意 OpenAI 兼容平台，填 API Key、Base URL、模型名）。
2. Embedding 模型（可选，可跳过；用于剧本与规则检索）。
3. 站点名称。
4. 域名（可选，仅展示）。
5. 可选功能：实时程序化配乐、实时配图、AI 对话语音。三项默认关闭，避免首次运行触发外部调用或产生费用。
6. 创建 OWNER 账号（最高权限，请妥善保管密码）。
7. 完成页：进入系统 / 查看部署文档。

引导接口在创建 OWNER 之前开放，创建后锁定。若需自动化完成，参见 `AGENT-INSTALL.md` 第 6 节。

## 6. 让其他玩家访问

### 同一局域网

启动日志会打印局域网地址，形如 `http://192.168.1.23:8086`。将该地址发给玩家。若无法访问，检查系统防火墙是否允许该端口（Windows 首次启动通常会弹出允许提示）。

### 异地联机

程序本身不负责把端口暴露到公网。可选方案：

- 内网穿透工具：把本机 8086 端口映射为公网地址；
- 异地组网工具（如 ZeroTier、Radmin VPN）：加入同一虚拟网络后使用分配的地址加端口；
- 云服务器部署：见下一节。

**不要**把服务端口直接裸露到公网，也不要关闭登录验证。

## 7. 云服务器与反向代理

程序本身不处理 HTTPS，建议在前面放 Nginx 或 Caddy 处理域名与证书，并**必须转发 WebSocket**，否则实时聊天会退回长轮询。

Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8086;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

若前端与后端跨源部署，需通过 `AI_TRPG_CORS_ALLOWED_ORIGINS` 显式配置白名单（逗号分隔）；默认同源，不允许任意站点匿名访问。

## 8. 安全建议

- 设置随机 `AI_TRPG_SECRET_KEY`，不要长期使用开发默认值。
- 每位玩家单独开账号，不要共享管理账号。
- 只对外开放必要端口。
- 定期备份整个 `data/` 目录（账号、房间、聊天记录、剧本、日志都在其中）。
- `data/config/` 内含 API Key 等敏感信息，不要打包分享或上传公开网盘。
- 怀疑密钥泄露时，先在模型平台作废旧 Key，再在设置页更新。

## 9. 升级

1. 停止服务；备份 `data/`。
2. 解压新版本到新目录。
3. 将旧目录的 `data/` 复制到新目录。
4. 启动新版本并验证首页与数据。
5. 确认无误后再删除旧目录。

不要用新版本直接覆盖旧目录，避免误覆盖 `data/`。

## 10. 常见问题

- **控制台一闪而过**：多为 Python 或依赖安装失败。重试 `start.cmd`，或按 `AGENT-INSTALL.md` 手动创建 venv 查看错误。
- **浏览器打不开默认端口**：查看日志中实际 `listening on` 的端口与地址。
- **开局无 AI 回复**：检查 API Key、余额、Base URL、模型名，并适当调大 `[ai] timeout`。
- **忘记管理员密码**：在服务控制台输入 `user list` 查看账号，再执行 `user reset-password <用户名>` 获取临时密码。
- **需要控制台命令列表**：在服务控制台输入 `help`。

## 11. 源码构建（可选）

发布包已包含前端构建产物，正常部署不需要 Node.js。仅在修改前端源码后需要重建：

```bash
npm install
npm run build:frontend
```

构建产物输出到 `dist/public/`。后端启动时默认不自动构建；如需开发期自动检测并重建，设置 `AI_TRPG_DEV_BUILD=1`。

## 12. 相关文档

- `AGENT-INSTALL.md`：面向 AI Agent 的逐步安装与验证手册。
- `使用指南.txt`：面向普通用户的图文式上手说明。
- `THIRD-PARTY-LICENSES.md`：依赖许可证汇总。
- `data/runtime/logs/`：运行日志。
