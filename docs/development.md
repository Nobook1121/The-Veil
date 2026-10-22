# Development Notes

## 架构边界

- `server.py` 只保留兼容启动入口和端口选择逻辑。
- `trpg_server.app_factory.create_app()` 负责创建 Flask app、初始化 CORS、Socket.IO、日志和蓝图。
- `backend/trpg_server/routes/` 下每个文件只维护一个业务路由组。
- `backend/trpg_server/socket_events.py` 维护 Socket.IO 事件注册。
- 前端仍使用静态 HTML、Bootstrap、原生 JavaScript 和全局初始化函数；迁移时必须保持现有 DOM id、接口路径和可见功能不变。

## 配置规范

- 全部配置经 `trpg_server.config` 分层加载：代码默认值 < `config/templates/general.toml` < `data/config/general.toml` < 环境变量 `AI_TRPG_CFG_*` < CLI `--set`；新增配置项必须先在 `config/schema.py` 注册 FieldSpec（默认值、类型转换器），再补 `config/templates/general.toml` 对应键与注释（`tests/test_config_template_sync.py` 强制双向同步）。
- 业务代码只读 `load_settings()` / `load_ai_runtime_config()` 的生效值，禁止再用 `tomllib` 直读 `general.toml` 或自带默认值兜底；写入一律走 `config.writer.update_sections()`（注释保留、原子替换、写后缓存失效）。
- 启动参数：`python server.py [port] [--port N] [--config-dir PATH] [--set section.key=value ...]`；被 env/CLI 覆盖的键在设置页自动锁定。
- 遗留 `site.json`/`network.json`/`penetration.json` 仅兼容读取，控制台 `config migrate` 可并入 `general.toml`。

## 验证流程

提交前运行：

```powershell
.\scripts\verify.ps1
```

如果只改了单个前端文件，也要先运行对应文件的语法检查，例如：

```powershell
node --check js\tabs.js
```

兼容性变更应先用更严格的警告策略复现问题，例如：

```powershell
python -W error::DeprecationWarning -m pytest tests\test_app_factory.py -q
```

依赖升级后再运行完整 `.\scripts\verify.ps1`，确认没有引入路由、session 或 Socket.IO 回归。

## 日志规范

- 使用 `logging.getLogger(__name__)` 或业务命名 logger。
- 日志前缀统一为 `[Time][INFO/WARN/ERROR/FATAL][Thread]`。
- `INFO` 仅记录关键操作：用户登录、用户发送消息、请求 AI；普通 `GET`、列表读取、状态轮询和配置查看不记录 `INFO`。
- 业务维护类成功操作如配置保存、剧本维护、房间维护默认使用 `DEBUG`，避免生产日志噪声。
- 日志文件写入 `logs/ai_trpg_YYYYMMDD_HHMMSS.log`，使用 `RotatingFileHandler` 控制文件大小。
- 不要记录密码、API key、auth token、provider secret、上传文件内容或完整认证载荷。
- 需要输出字典前，优先使用 `trpg_server.logging_config.redact_sensitive`。

## 文件访问规范

- 请求参数参与路径拼接时，使用 `trpg_server.security.safe_join`。
- 上传文件扩展名校验使用 `trpg_server.security.is_allowed_upload`。
- JSON 读取使用 `trpg_server.json_store.read_json`。
- JSON 写入使用 `trpg_server.json_store.write_json_atomic`，避免进程中断时留下半写文件。

## 前端规范

- 新的接口调用优先使用 `js/api-client.js` 中的 `TrpgApi`。
- 新的 DOM 查询和事件绑定优先使用 `js/dom-utils.js` 中的 `TrpgDom`。
- 脚本只在 `index.html` 的 `</body>` 前加载，避免重复初始化。
- 交互状态需要同步可访问性属性，例如 `aria-expanded`、`aria-selected` 和 `aria-current`。
- UI 样式优先使用 `style.css` 顶部 token，避免继续散落硬编码颜色、阴影和圆角。
- **禁止使用浏览器原生弹窗 `alert()` / `confirm()` / `prompt()`**：其外观无法与产品风格统一、会阻塞主线程，且在部分浏览器中会被拦截。轻量反馈统一用 `showNotification()`；需要用户确认或输入时用 `window.TrpgDialog.confirm()` / `window.TrpgDialog.prompt()`（`js/dialog.js`，基于 Bootstrap 模态框，返回 Promise 可直接 `await`）。
