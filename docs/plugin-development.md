# 插件开发指南

> 适用版本：宿主 ≥ 1.0.4（插件 API `api_version = 1`）。
> 目标：让第三方开发者**不改核心代码**就能给帷幕加道具模板、规则集、AI 工具、HTTP 接口与设置段。
> 实现入口：[plugins/contracts.py](../backend/trpg_server/plugins/contracts.py)、[plugins/manifest.py](../backend/trpg_server/plugins/manifest.py)、[plugins/registry.py](../backend/trpg_server/plugins/registry.py)。

---

## 1. 信任模型（先读这一节）

插件代码以**宿主进程权限**运行，能做宿主能做的一切（读写文件、发起网络请求、访问房间数据）。

因此信任边界只有一条：**站点管理员把插件目录放进 `data/plugins/`，并把插件 id 写进 `enabled.json` 的启用列表**。

具体保证：

| 阶段 | 行为 |
| --- | --- |
| 发现 | 只读取每个目录下的 `plugin.json`（≤128 KB）；不读 HTML/CSS/资源，不 import 任何 `.py`。 |
| 未启用 | 状态恒为 `disabled`，**不读取资源、不执行代码**；只在 `/api/plugins` 里显示。 |
| 启用 | 先做资源审计（符号链接 / 文件数 / 总量），再注册模板、导入入口模块、注册能力。 |
| 失败 | 只把该插件（及依赖它的插件）标为 `failed` 并写日志；主程序照常启动。 |
| 回滚 | 启动参数 `--no-plugins`、环境变量 `TRPG_PLUGINS_DISABLED=1`、或 `create_app({"PLUGINS_DISABLED": True})` 可一键禁用全部第三方插件。 |

如果你只是要提供道具模板，**不要写 Python**：只含 `plugin.json + templates/` 的纯数据插件不会被 import，风险最低。

---

## 2. 目录与安装

```
data/plugins/
  enabled.json                  # 启用列表（宿主维护，可直接手改）
  hello-prop/
    plugin.json                 # 必需：清单
    entry.py                    # 可选：入口模块（只在启用后 import）
    templates/                  # 可选：道具模板
      hello-note/
        manifest.json
        page.html
        style.css
        assets/*.png
```

`enabled.json`：

```json
{
  "version": 1,
  "enabled": ["hello-prop"],
  "updated_at": "2026-11-12 15:00:00"
}
```

安装示例插件（最小可用，含模板 + HTTP 接口）：

```powershell
Copy-Item -Recurse examples\plugins\hello-prop data\plugins\hello-prop
# 然后把 hello-prop 写进 data/plugins/enabled.json 的 enabled
```

也可以用管理员接口（会同步落盘 `enabled.json`）：

```
GET    /api/plugins                      # 清单：loaded / disabled / failed + 原因
POST   /api/plugins/<id>/enable
POST   /api/plugins/<id>/disable
DELETE /api/plugins/<id>                 # body {"confirm": "<id>"}，需先禁用
POST   /api/plugins/rescan
GET    /api/plugins/schema               # 清单 JSON Schema
```

> 这些接口只对 `ADMIN` / `OWNER` 开放；普通用户连清单都看不到。

---

## 3. `plugin.json` 字段

JSON Schema：[docs/schemas/plugin-manifest.schema.json](schemas/plugin-manifest.schema.json)（由 `manifest_json_schema()` 生成，测试会断言两者一致）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | int | 否 | 清单结构版本，当前固定 `1`。 |
| `id` | string | 是 | `^[a-z][a-z0-9-]{1,63}$`，必须与目录名一致。 |
| `name` | string | 是 | 展示名。 |
| `version` | string | 是 | 语义化版本，如 `1.2.0`。 |
| `description` | string | 否 | 一句话简介。 |
| `author` | string | 否 | 作者/来源。 |
| `api_version` | int | 否 | 插件 API 版本，必须等于宿主 `PLUGIN_API_VERSION`（当前 `1`）。 |
| `requires.app` | string | 否 | 目标 App 版本区间，如 `">=1.0.0 <2.0.0"`；支持 `>= <= > < == !=`，空格或逗号分隔。 |
| `requires.plugins` | object | 否 | `{"other-plugin": ">=1.0.0"}`，依赖的插件及其版本区间。 |
| `dependencies` | string[] | 否 | 依赖的插件 id；未启用即视为失败。 |
| `conflicts` | string[] | 否 | 冲突插件 id；双方都会被禁用（不做「先到先得」）。 |
| `capabilities` | object | 否 | 见第 4 节。 |
| `code.entry` | string | 否 | 入口 `.py` 的相对路径；有该能力时才会 import。 |

所有路径字段都必须是**插件内相对路径**：不能是绝对路径、不能带盘符、不能含 `..`。

---

## 4. 能力接口

### 4.1 `prop_templates`（纯数据，推荐）

```json
"capabilities": {
  "prop_templates": { "dirs": ["templates"] }
}
```

`dirs` 下每个子目录就是一个模板（与内置模板结构一致）：

```
templates/hello-note/
  manifest.json   # id 必须与目录名一致
  page.html       # 只用文档流标签
  style.css
  assets/         # 平铺 png/jpg，≤5 MB/个，≤24 个，总量 ≤20 MB
```

模板 manifest 与渲染约束见 [prop-templates.md](prop-templates.md)。插件模板的 `source` 为 `plugin:<插件 id>`，出问题时只进入 `unavailable` 列表，不影响其他模板。

### 4.2 `rulesets`（代码）

```json
"code": { "entry": "entry.py" },
"capabilities": { "rulesets": { "module": "entry.py", "ids": ["my-rules"] } }
```

入口模块导出 `PLUGIN` 或 `create_plugin()`，返回实现了 [ruleset/contracts.py](../backend/trpg_server/ruleset/contracts.py) 的 `RulesetPlugin`：

- `api_version` 必须等于 `RULESET_API_VERSION`；
- `id` 必须出现在 `capabilities.rulesets.ids`（若声明）；
- 规则集通过 `plugins.runtime` 合并进 `ruleset.bootstrap.build_registry()`，不修改核心注册表。

> 规则集与剧本/房间绑定，新增规则集后仍需在剧本编辑器里选择它。

### 4.3 `agent_tools`（代码）

```json
"capabilities": {
  "agent_tools": { "module": "entry.py", "tools": ["my-plugin.roll_special"] }
}
```

入口模块提供 `create_tools() -> list[AgentTool]`（或 `TOOLS` 列表）。约束：

- 工具名必须以 `<插件 id>.` 开头，且必须与 `tools` 声明一致；
- 工具会合并进 `default_tool_registry()`，模型侧名称由核心转成 `my_plugin_roll_special`；
- 插件只用一次命名空间（`my-plugin.roll_special` 可以，`my-plugin.sanity.roll` 不行）。

### 4.4 `http_blueprints`（代码）

```json
"capabilities": {
  "http_blueprints": { "module": "entry.py", "blueprints": ["bp"] }
}
```

入口模块导出对应的 `flask.Blueprint` 对象。约定：

- 路由请挂在自己的命名空间（`/api/plugins/<插件 id>/...`）；
- 插件蓝图在核心蓝图之后注册，与核心重复的路径由核心优先匹配；
- 蓝图注册后**无法撤销**，禁用插件不会移除已注册路由，需重启服务。

### 4.5 `settings_sections`（声明式）

声明插件希望在设置页出现的配置段（`key` / `title` / `fields[]`）。当前版本只做**声明与校验**，不会自动生成设置页；插件可自行读取 `data/config/general.toml` 的对应段（请先与维护者约定命名，避免与核心键冲突）。

### 4.6 `frontend_assets`（声明式）

声明前端资源（`path` + `type: script|style|image|font`）。当前版本校验文件存在与大小，不做自动注入；如需把界面嵌进主站，请先与维护者约定入口。

---

## 5. 版本兼容约定

| 版本 | 谁维护 | 不兼容时 |
| --- | --- | --- |
| `schema_version`（清单结构） | 宿主 | 不等于 `1` → 直接判为清单非法。 |
| `api_version`（插件 API） | 宿主 | 不等于宿主版本 → `reason_code=api_version_mismatch`，不加载。 |
| `version`（插件自身） | 插件作者 | 只用于展示与依赖区间判断。 |
| `requires.app` | 插件作者 | 不满足 → `reason_code=app_version_incompatible`。 |
| `requires.plugins` | 插件作者 | 不满足 → `reason_code=dependency_version`。 |

**升级建议**：`api_version` 变化前，宿主会先发布兼容窗口；插件应把 `requires.app` 写成自己验证过的上限（例如 `>=1.0.0 <2.0.0`），避免在新宿主上「能加载但行为漂移」。

---

## 6. 生命周期与状态

```
发现(discover) → 校验(manifest) → 兼容性/依赖/冲突 → 拓扑排序 → 激活(资源审计 → 注册能力) → 启用/禁用 → 卸载
```

`GET /api/plugins` 返回的 `data.plugins[]` 里，`state` 只会是：

| state | 含义 |
| --- | --- |
| `loaded` | 已激活；`capabilities` 是实际加载成功的能力。 |
| `disabled` | 在磁盘上但未启用（`reason_code=not_enabled`），或被 `--no-plugins` 全局禁用。 |
| `failed` | 已启用但加载失败；`reason` 是可读原因，`reason_code` 用于程序判断。 |

启用/禁用的即时性：

- **纯数据插件**（只有 `prop_templates` / `settings_sections` / `frontend_assets`）：立即生效，模板来源会重新扫描；
- **代码插件**（`rulesets` / `agent_tools` / `http_blueprints`）：启用/禁用会立即更新状态与模板来源；入口模块按文件签名缓存，**禁用后已注册的 HTTP 路由仍在**，完整回滚请重启服务（接口返回 `restart_required`）。

> **当前限制**：Flask 的蓝图注册不可撤销，代码插件被禁用后其路由在本次进程内仍然可访问。`restart_required: true` 就是提示必须重启才能彻底回滚；对安全敏感的回滚请直接用 `--no-plugins` 重启。

卸载：先 `disable`，再 `DELETE /api/plugins/<id>` 且 body 里 `confirm` 填插件 id。宿主只删除插件目录本身；符号链接插件只删链接。

---

## 7. 错误处理约定

- 清单问题 → 该插件 `failed`，`reason_code=manifest_invalid`，消息里带具体字段；
- 版本门控 → `api_version_mismatch` / `app_version_incompatible`；
- 依赖问题 → `dependency_missing` / `dependency_version` / `dependency_cycle` / `dependency_failed`；
- 冲突 → `conflict`（双方都失败，reason 里列出对方）；
- 路径/资源 → `unsafe_path` / `resource_limit`；
- 代码异常（含 `SystemExit`）→ `code_load_failed`，异常栈写进 `data/runtime/logs/`，**不会**中断其它插件或主程序；
- 模板校验失败 → 插件本身仍是 `loaded`，具体模板进入道具接口的 `unavailable[]`（带 `reason`）。

日志关键字（便于 grep）：`plugin_loaded`、`plugin_load_failed`、`plugins_loaded`、`plugin_blueprint_failed`、`plugin_registry_init_failed`。

---

## 8. 安全约定（宿主已强制，插件需配合）

| 项 | 宿主行为 |
| --- | --- |
| 路径 | 所有插件路径经 `security.safe_plugin_path` → `security.safe_join`，拒绝 `..` / 绝对路径 / 盘符。 |
| 符号链接 | 插件目录本身、目录内任意文件/子目录是 symlink 一律拒绝（`unsafe_path`）。 |
| 体积 | 单插件 ≤512 个文件、≤64 MB；入口模块 ≤16 MB；清单 ≤128 KB；模板资源另有 5 MB/文件、20 MB/总量的限制。 |
| 代码 | 只在插件出现在启用列表后才 import；import 前先做资源审计。导入入口模块时会把插件目录临时加入 `sys.path`（方便同目录互相 import），导入结束立即恢复。 |
| HTTP | 插件蓝图最后注册，核心路由优先；插件路由请自带鉴权（参考 `routes/plugins.py` 的 `_guard`）。 |
| 回滚 | `--no-plugins` / `TRPG_PLUGINS_DISABLED=1` / `PLUGINS_DISABLED` 三选一。 |

**不要**在模块顶层做重活（网络请求、扫描磁盘、修改全局状态）：入口模块在 `create_app()` 期间被 import，任何阻塞都会拖慢启动。

---

## 9. 调试方式

1. **看清单状态**：管理员登录后 `GET /api/plugins`，优先看 `reason_code` 与 `reason`。
2. **改完清单不生效**：`POST /api/plugins/rescan`；改模板 HTML/CSS 后同理（道具接口的 `POST /api/props/templates/rescan` 也可以）。
3. **代码改动不生效**：入口模块按 `(mtime, size)` 缓存；改文件后 `rescan` 会重新执行。若改的是 blueprint，仍需重启才能替换已注册路由。
4. **渲染自检**：准备一个 PropSpec JSON，直接渲染插件模板：

   ```powershell
   python -m trpg_server.props.render spec.json out.pdf --template hello-note
   ```

5. **写测试**：参考 [tests/test_plugins.py](../tests/test_plugins.py) 的做法——把示例插件复制到 `tmp_path/plugins`，写 `enabled.json`，然后 `PluginRegistry(root).load()` 断言状态与模板列表。

最小端到端断言（与示例插件一致）：

```python
registry = PluginRegistry(tmp_path / "plugins").load()
assert registry.get("hello-prop").state == "loaded"
assert "hello-note" in props_registry.available_ids()
```

---

## 10. 示例插件

[examples/plugins/hello-prop](../examples/plugins/hello-prop)：

| 文件 | 作用 |
| --- | --- |
| `plugin.json` | 声明 `prop_templates` + `http_blueprints`。 |
| `entry.py` | 只读接口 `GET /api/plugins/hello-prop/ping`。 |
| `templates/hello-note/*` | 一张可渲染的便条模板。 |

覆盖的验收链路：装进 `data/plugins/` → 写 `enabled.json` → `/api/plugins` 显示 `loaded` → `/api/rooms/<id>/props/templates` 出现 `hello-note`（`source=plugin:hello-prop`）→ 渲染 PDF/PNG → 接口可调用。
