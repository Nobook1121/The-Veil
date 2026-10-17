# 控制台命令扩展规划（待审阅）

> **怎么用这份文档**
> 1. 逐条看「候选清单」，把 **采纳** 列改成 `✅`（要做）/ `✗`（不做）/ `待定`。
> 2. 想改语法或参数，直接在「命令」列上改（例如把 `room prune --days 30` 改成 `room prune --idle 30`）。
> 3. 看完在对话里回我一句「按最终清单实施」，我就按你的选择落地代码 + 测试 + 帮助文本 + `docs/console-commands.md`。
>
> 未标注的行默认**暂不实施**；只有标 `✅` 的才会进入编码。

---

## 一、现状（已实现）

| 命令 | 作用 |
|---|---|
| `help` / `?` | 打印可用命令 |
| `user promote/demote <user> [角色]` | 调整用户角色 |
| `user reset-password <user>` | 重置为随机临时密码并强制改密 |
| `ai usage [days]` | 最近 N 天 AI 用量与输入 token 分段 |
| `ai cache_probe [platform]` | 连发两次相同请求，判断平台前缀缓存是否可观测 |
| `scenario list [search]` | 列出剧本：ID/标题/所有者/版本/向量数/归档 |
| `scenario owner <id> <user>` | 改剧本 owner_id（编辑权限） |
| `scenario delete <id> [--vectors-only\|--force]` | 删剧本（默认：有房间占用则归档）|
| `shutdown`（含 stop/quit/exit 别名） | 优雅停机 |

---

## 二、设计约定（新增命令统一遵循）

1. **命名**：`<领域> <动作> [参数]`，领域用小写单词：`server` / `user` / `ai` / `scenario` / `vector` / `knowledge` / `room` / `log` / `network` / `backup`。
2. **模块归属（高内聚低耦合）**：每个领域逻辑放进独立的**非 Flask** 模块（沿用 `scenario_admin.py`、`agents/cache_probe.py` 的做法），`console_commands.py` 只做解析与分发，保持文件体积可控。
3. **依赖注入**：`vector_store` / `user_manager` / 各数据目录由 `server.py` 启动时经 `app.extensions` / `app.config` 注入，避免 `console ↔ routes` 循环依赖。
4. **破坏性命令**：涉及删除/覆盖/清空的命令必须支持，询问是否操作(例如弹出"是否执行(y/n)"确认)（缺省为「预演」，只打印将发生的变更，不执行）。
5. **日志**：所有写操作走 `log_user_action(...)`，操作者固定记为「控制台」；只读命令不写审计日志。
6. **同步更新**：每次新增命令必须同时更新 `command_help_text()` 与 `docs/console-commands.md`，并补 `tests/`。

---

## 三、候选清单

### A. 服务与巡检

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `server health` | 汇总向量库 / embedding / OCR / AI 平台 / 数据目录 / 用户库的可用性 | `routes/vector_health.py` 三段 health + 平台配置 | 只读 | ★★★ |✅ |
| `server info` | 版本、启动时长、各数据目录路径与占用大小 | `settings.py` 常量 | 只读 | ★★★ |✅ |
| `log tail [n]` | 打印最近 n 行日志（默认 50） | `LOGS_DIR` | 只读 | ★★★ |✅ |
| `log level <DEBUG\|INFO\|WARNING\|ERROR>` | 运行期热切换日志级别，免重启 | `logging_config.py`（需补 set-level 入口） | 低 | ★★ | |
| `server health --json` | 同上，但输出机器可读 JSON（脚本化用） | 同上 | 只读 | ★ | ✅|

### B. 用户与账户

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `user list [search]` | 列出用户：ID / 用户名 / 角色 / 状态 | `user_manager.get_all_users()` | 只读 | ★★★ | ✅|
| `user ban <user>` | 封禁账号（保留数据） | `update_user_status`（`routes/users.py`） | 低 | ★★★ | ✅|
| `user unban <user>` | 解封账号 | 同上 | 低 | ★★★ |✅|
| `user sessions revoke <user>` | 强制下线（撤销活跃会话，不禁号） | `service.revoke_session` | 低 | ★★ |✅|
| `user create <name> [USER\|ADMIN\OWNER]` | 本地建号（无自助注册/救急用），返回临时密码 | `service.register` | 中 | ★★ |✅|
| `user delete <user>` | 彻底删除账号 | 需新增（当前无 HTTP 等价） | 高 | ★ |✅|
| `user delete <user> --force` | 不计后果强制删除账号 | 需新增（当前无 HTTP 等价） | 高 | ★ |✅|

### C. AI 平台 / 成本 / 缓存（已有 usage、cache_probe）

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `ai platform list` | 列平台：ID / 启用 / 顺序 / 模型 | `routes/config.py` aiplatform GET | 只读 | ★★★ |✅|
| `ai platform test [id]` | 连通性冒烟测试（换平台前必做） | `routes/config.py` .../test | 低 | ★★★ |✅|
| `ai platform enable <id>` / `disable <id>` | 启用 / 停用某平台 | config.py（save / order） | 中 | ★★ |✅|
| `ai platform order <id...>` | 调整平台优先级顺序 | config.py .../order | 中 | ★★ | |
| `ai platform detect-responses [id]` | 探测是否支持 Responses API（决定能否用增量） | `routes/config.py` .../detect-responses | 低 | ★★ |✅|
| `ai cache stats` | 本地语义 / 精确 / 前缀缓存的条目与命中统计 | `agents/cache.py` `_TTLCache.stats` | 只读 | ★★ |✅|
| `ai cache clear --yes` | 清空本地缓存（救火 / 观测） | 需补 `clear()` | 中 | ★★ |✅|
| `ai usage reset <user\|all>` | 重置用量计数 | 需新增 | 中 | ★ |✅|

### D. 知识库与向量

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `vector health` | 向量条数、后端、路径、可用性；embedding / OCR 状态 | `vector_store.count/health`、`embedding.health` | 只读 | ★★★ |✅|
| `vector prune [--yes]` | 清理**孤儿向量**（剧本已删、向量残留；`scenario delete` 不会自动清） | `vector_store.delete_by_filter` | 中 | ★★★ |✅|
| `knowledge list [search]` | 列规则集 + 各剧本知识/向量数 | `routes/knowledge_bases.py` | 只读 | ★★ |✅|
| `knowledge reindex [ruleset]` | 重建规则集索引（上传卡住的救命操作）；省略则全部重建 | `knowledge_bases.py` .../reindex | 中 | ★★★ |✅|
| `scenario validate <id>` | 校验剧本 JSON / 知识索引 / 向量三者一致 | 新增（无 HTTP 等价） | 只读 | ★★ |✅|
| `scenario reindex <id>` | 重建某剧本的知识索引与向量（需 embedding，可能慢） | `knowledge_base.py` 相关 | 中 | ★★ |✅|

### E. 房间

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `room list [search]` | 列房间：ID / 名 / 剧本 / 成员数 / 已归档 | `routes/rooms.py`（需抽到非 Flask 模块） | 只读 | ★★★ |✅|
| `room members <room_id>` | 查看成员与房间角色 | rooms.py 相关 | 只读 | ★ |✅|
| `room archive <room_id>` | 归档房间 | `rooms.py` archive | 低 | ★★ |✅|
| `room delete <room_id>` | 删除房间（含消息） | `rooms.py` delete | 高 | ★★ |  |
| `room prune [--days N]` | 清理僵尸房间（长期无活动 / 无成员；默认 30 天） | 新增（无 HTTP 等价） | 中 | ★★ |✅|

### F. 网络 / 任务 / 备份

| 命令 | 作用 | 复用实现 | 风险 | 优先级 | 采纳 |
|---|---|---|---|---|---|
| `network status` | 端口、内网穿透状态 | `routes/network.py` status / penetration status | 只读 | ★★ | |
| `network test` | 网络连通性测试 | `routes/network.py` .../test | 只读 | ★★ | |
| `autosave run` | 立即触发一次自动存档 | `app_factory.py` → `run_scheduled_autosaves` | 低 | ★★ | |
| `import jobs` | 查看剧本导入任务进度与状态 | `scenario_import_jobs.py` | 只读 | ★ | |
| `backup create [--out <dir>]` | 打包 config / scenarios / rooms / 用户库 / 日志 | 新增 | 中 | ★★ | |
| `owner transfer <user> --yes` | 转移系统 OWNER 身份 | `routes/setup.py` owner | 高 | ★ | |

---

## 四、建议实施批次

- **第一批（只读或可逆、直接复用现成函数、改动小）**
  `server health`、`vector health`、`vector prune`、`user list`、`user lock/unlock`、`log tail`、`log level`、`ai platform list/test`、`knowledge reindex`、`room list`
- **第二批（写操作，需 `--yes` 习惯）**
  `ai cache stats/clear`、`ai platform enable/disable/order`、`user sessions revoke`、`room archive/delete/prune`、`autosave run`、`network status/test`、`scenario validate`
- **第三批（高危，需谨慎评审）**
  `backup create`、`user delete`、`owner transfer`、`ai usage reset`、`scenario reindex`

---

## 五、需要你拍板的问题

1. **批不批 `--yes` 约定**？我建议所有破坏性命令默认预演、加 `--yes` 才执行（防手滑）。
2. **`backup create` 的输出目录**放哪？（建议 `data/backups/`，可被 `--out` 覆盖）
3. **`log level` 是否需要持久化**？（重启后回到配置值 / 还是保持运行期临时切换）
4. **`user delete` / `owner transfer` 要不要做**？（高危，建议只做 `owner transfer` 或都不做）
5. 还有没有你希望补充的领域/命令？（直接写在下面）

### 你的补充

```text
（在此处写你想加/想改的命令）
```
