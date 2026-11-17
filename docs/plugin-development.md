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
| 未启用 | 清单合法时状态为 `disabled`，**不读取资源、不执行代码**；只在 `/api/plugins` 里显示。清单本身非法仍会在发现阶段直接 `failed`。 |
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
>
> `GET /api/plugins` 的 `data` 里有两组记录：`plugins` 是 `data/plugins` 下的第三方插件；`system_plugins` 是随包发布的系统内置插件（只读，不可启用/禁用/卸载，不计入 `counts`）。

插件根目录默认是 `<DATA_DIR>/plugins`；需要换位置时设 `app.config["PLUGIN_DIR"]`。

---

## 3. `plugin.json` 字段

JSON Schema：[docs/schemas/plugin-manifest.schema.json](schemas/plugin-manifest.schema.json)（即 `manifest_json_schema()` 的输出，`tests/test_plugins.py` 断言文件与实际校验一致）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | int | 否 | 清单结构版本，当前固定 `1`；缺省按 `1` 处理。 |
| `id` | string | 是 | `^[a-z][a-z0-9-]{1,63}$`，必须与目录名一致，且不能是保留 id（见下文）。 |
| `name` | string | 是 | 展示名；缺失时回落为 `id`（schema 标注必填，建议始终填写）。 |
| `version` | string | 是 | 语义化版本，如 `1.2.0`。 |
| `description` | string | 否 | 一句话简介。 |
| `author` | string | 否 | 作者/来源。 |
| `api_version` | int | 否 | 插件 API 版本，必须等于宿主 `PLUGIN_API_VERSION`（当前 `1`）；缺省按 `1` 处理。 |
| `requires.app` | string | 否 | 目标 App 版本区间，如 `">=1.0.0 <2.0.0"`；支持 `>= <= > < == !=`，空格或逗号分隔；空串表示不限制。每个版本号必须是完整的 `x.y.z`（可带 `v` 前缀），否则按 `app_version_incompatible` 失败。 |
| `requires.plugins` | object | 否 | `{"other-plugin": ">=1.0.0"}`，依赖的插件及其版本区间；区间格式非法或版本不满足都归 `dependency_version`。 |
| `dependencies` | string[] | 否 | 依赖的插件 id；未启用即视为失败（`dependency_missing`）。 |
| `conflicts` | string[] | 否 | 冲突插件 id；双方都会被标为 `state=failed`、`reason_code=conflict`（不是静默禁用，也不做「先到先得」）。 |
| `capabilities` | object | 否 | 见第 4 节。 |
| `code.entry` | string | 否 | 入口 `.py` 的相对路径；声明 `rulesets` / `agent_tools` / `http_blueprints` 时必须提供。 |

所有路径字段都必须是**插件内相对路径**：不能是绝对路径、不能带盘符、不能含 `..`。

`code.entry` 是插件**唯一会被 import 的模块**。`capabilities.*.module` 仍被 schema 要求填写，但当前实现里只要 `code.entry` 存在就复用它，能力段里的 `module` 不参与导入。

### 保留 id

插件 id 的第一段会成为 Agent 工具命名空间，因此下面这些与核心工具同名的 id 会在清单解析阶段直接判为 `manifest_invalid`：

```
character, chase, check, coc7, combat, dice, knowledge, magic,
music, narrative, props, room, san, sanity, trigger
```

即使 id 不在名单里，激活阶段也会把插件声明的工具名与真实核心工具表再比对一次；重名同样拒绝（`manifest_invalid`），避免插件顶掉核心工具。

---

## 4. 能力接口

### 4.1 `prop_templates`（纯数据，推荐）

```json
"capabilities": {
  "prop_templates": { "dirs": ["templates"] }
}
```

`dirs` 缺省为 `["templates"]`。`dirs` 下每个子目录就是一个模板（与内置模板结构一致）：

```
templates/hello-note/
  manifest.json   # id 必须与目录名一致
  page.html       # 只用文档流标签
  style.css
  assets/         # 平铺 png/jpg，≤5 MB/个，≤24 个，总量 ≤20 MB
```

模板 manifest 与渲染约束见 [prop-templates.md](prop-templates.md)。插件模板的 `source` 为 `plugin:<插件 id>`，单个模板校验失败时插件本身仍是 `loaded`，失败模板只进入道具接口的 `unavailable[]`（带 `reason`），不影响其他模板；但声明的模板目录不存在会让整个插件 `failed`（`manifest_invalid`）。

入口模块固定取自 `code.entry`（见第 3 节），能力段里的 `module` 字段不参与导入。

### 4.2 `rulesets`（代码）

```json
"code": { "entry": "entry.py" },
"capabilities": { "rulesets": { "module": "entry.py", "ids": ["my-rules"] } }
```

入口模块（`code.entry`）导出 `PLUGIN` 或 `create_plugin()`，返回实现了 [ruleset/contracts.py](../backend/trpg_server/ruleset/contracts.py) 的 `RulesetPlugin`：

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

入口模块（`code.entry`）提供 `create_tools() -> list[AgentTool]`（或 `TOOLS` 列表）。约束：

- 工具名必须以 `<插件 id>.` 开头，且必须与 `tools` 声明完全一致；schema 还要求整体匹配 `^[a-z][a-z0-9-]*\.[a-z0-9_]+$`，加载时后端只强制前缀与声明，请自行遵守该正则；
- 工具会合并进 `default_tool_registry()`；下发给模型时只把 `.` 换成 `_`，`-` 保留，所以 `my-plugin.roll_special` 的模型侧名称是 `my-plugin_roll_special`（不是 `my_plugin_roll_special`）；
- 激活时会与真实核心工具表比对，工具名与核心重名会被拒绝（`manifest_invalid`）；
- 插件只用一次命名空间（`my-plugin.roll_special` 可以，`my-plugin.sanity.roll` 不行）。

### 4.4 `http_blueprints`（代码）

```json
"capabilities": {
  "http_blueprints": { "module": "entry.py", "blueprints": ["bp"] }
}
```

入口模块（`code.entry`）导出对应的 `flask.Blueprint` 对象。约定：

- 路由请挂在自己的命名空间（`/api/plugins/<插件 id>/...`）；
- 插件蓝图在核心蓝图之后注册；只有**同一 path + 同一 method** 由核心优先匹配，不同 method 的路由各自可用；
- 蓝图对象名（`Blueprint.name`）与已注册蓝图重名时会被跳过（不报错），请用插件 id 作前缀；
- 单条蓝图注册异常只记 `plugin_blueprint_failed` 日志，插件本身仍是 `loaded`；
- 蓝图注册后**无法撤销**，禁用插件不会移除已注册路由，需重启服务。

### 4.5 `settings_sections`（声明式）

声明插件希望在设置页出现的配置段（`key` / `title` / `fields[]`）。校验规则：`key` 必须匹配 `^[a-z][a-z0-9_]{1,31}$`；`title` 缺省回落为 `key`；`fields` 必须是数组，非对象项会被静默丢弃。当前版本只做**声明与校验**，不会自动生成设置页；插件可自行读取 `data/config/general.toml` 的对应段（请先与维护者约定命名，避免与核心键冲突）。

### 4.6 `frontend_assets`（声明式）

声明前端资源（`path` + `type: script|style|image|font`，`type` 缺省为 `script`）。激活时会校验每个 `path` 存在且不超过 16 MB；当前版本不做自动注入，如需把界面嵌进主站，请先与维护者约定入口。

---

## 5. 版本兼容约定

| 版本 | 谁维护 | 不兼容时 |
| --- | --- | --- |
| `schema_version`（清单结构） | 宿主 | 缺省按 `1`；不等于 `1` → 直接判为清单非法（`manifest_invalid`）。 |
| `api_version`（插件 API） | 宿主 | 缺省按 `1`；不等于宿主版本 → `reason_code=api_version_mismatch`，不加载。 |
| `version`（插件自身） | 插件作者 | 只用于展示与依赖区间判断。 |
| `requires.app` | 插件作者 | 空串不限制；每个 token 必须是完整 `x.y.z`（可带 `v` 前缀）；不满足或格式非法 → `reason_code=app_version_incompatible`。 |
| `requires.plugins` | 插件作者 | 依赖未启用 → `dependency_missing`；区间格式非法或版本不满足 → `reason_code=dependency_version`。 |

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
| `disabled` | 在磁盘上但未启用（`reason_code=not_enabled`）；被 `--no-plugins` 等全局禁用时 `reason_code=plugins_disabled_by_operator`。 |
| `failed` | 清单非法、兼容性/依赖/冲突不通过或激活失败；`reason` 是可读原因，`reason_code` 用于程序判断。启用列表里的目录不存在也记为 `failed`（`plugin_dir_missing`）。 |

`plugins[]` 每条记录的字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `state` / `enabled` | 插件 id、状态、是否启用。 |
| `reason_code` / `reason` | 失败或禁用原因（见第 7 节）；成功时为空白。 |
| `dir` | 插件目录的绝对路径（仅管理员接口可见）。 |
| `source` | 发现位置：`plugin_dir`（第三方目录）；`template_pack` 为预留值。 |
| `capabilities` | 实际加载成功的能力名列表（`loaded` 时才有意义）。 |
| `pending_restart` | 代码插件被禁用后为 `true`：蓝图仍存活到重启。 |
| `manifest` | 清单位图（`id` / `name` / `version` / `author` / `capabilities` / `has_code` 等），不含绝对路径。 |

> `pending_restart` 记录在条目上并跨请求保留；`restart_required` 是**启用/禁用接口响应顶层**的一次性布尔值，两者不要混用。
>
> `system_plugins[]` 是只读的系统内置插件（`system=true`、`source=system`），字段结构相同，但不计入 `counts`，也不支持启用/禁用/卸载。

启用/禁用的即时性：

- **纯数据插件**（只有 `prop_templates` / `settings_sections` / `frontend_assets`）：立即生效，模板来源会重新扫描；
- **代码插件**（`rulesets` / `agent_tools` / `http_blueprints`）：启用/禁用会立即更新状态与模板来源；入口模块按文件签名缓存，**禁用后已注册的 HTTP 路由仍在**，完整回滚请重启服务（接口返回 `restart_required`）。

> **当前限制**：Flask 的蓝图注册不可撤销，代码插件被禁用后其路由在本次进程内仍然可访问。`restart_required: true` 就是提示必须重启才能彻底回滚；对安全敏感的回滚请直接用 `--no-plugins` 重启。

卸载：先 `disable`，再 `DELETE /api/plugins/<id>` 且 body 里 `confirm` 填插件 id。宿主只删除插件目录本身；符号链接插件只删链接。

---

## 7. 错误处理约定

`reason_code` 全部取值：

| reason_code | 触发条件 |
| --- | --- |
| `manifest_invalid` | 清单字段/路径/保留 id/工具名/蓝图导出等校验失败（含模板目录、入口模块、前端资源缺失）。 |
| `api_version_mismatch` | 插件 `api_version` 与宿主不一致。 |
| `app_version_incompatible` | `requires.app` 不满足或格式非法。 |
| `dependency_missing` | `dependencies` / `requires.plugins` 里的插件未启用或不存在。 |
| `dependency_version` | `requires.plugins` 区间格式非法或版本不满足。 |
| `dependency_failed` | 依赖的插件本身加载失败。 |
| `dependency_cycle` | 依赖存在环。 |
| `conflict` | 与 `conflicts` 声明的插件同时启用（双方都 `failed`，reason 里列出对方）。 |
| `not_enabled` | 在磁盘上但不在 `enabled.json` 启用列表里。 |
| `plugins_disabled_by_operator` | 运维参数（`--no-plugins` / 环境变量 / `PLUGINS_DISABLED`）全局禁用。 |
| `plugin_dir_missing` | 启用列表里的插件目录不存在。 |
| `unsafe_path` | 路径越界、盘符、`..` 或经过符号链接。 |
| `resource_limit` | 超过文件数/体积上限。 |
| `capability_failed` | 资源审计过程本身抛异常（非预期错误）。 |
| `code_load_failed` | 插件代码导入/激活抛出异常（含 `SystemExit`）。 |
| `restart_required` | 代码插件已禁用，但蓝图仍存活到进程重启。 |

补充说明：

- 插件代码异常只影响该插件（及依赖它的插件），不会中断其它插件或主程序；
- 单个模板校验失败时插件本身仍是 `loaded`，失败模板进入道具接口的 `unavailable[]`（带 `reason`）。

日志关键字（便于 grep）：`plugin_loaded`、`plugin_load_failed`、`plugins_loaded`、`plugin_blueprint_failed`、`plugin_registry_init_failed`、`plugin_state_corrupt`、`plugin_discover_crashed`、`plugin_resource_check_crashed`、`plugin_activate_crashed`、`plugin_blueprint_registration_failed`、`plugin_rescan_failed`、`third_party_plugins_disabled_by_operator`、`system_plugin_view_failed`。

---

## 8. 安全约定（宿主已强制，插件需配合）

| 项 | 宿主行为 |
| --- | --- |
| 路径 | 所有插件路径经 `security.safe_plugin_path` → `security.safe_join`，拒绝 `..` / 绝对路径 / 盘符。 |
| 符号链接 | 插件目录本身、目录内任意文件/子目录是 symlink 一律拒绝（`unsafe_path`）。 |
| 体积 | 单插件 ≤512 个文件、≤64 MB；入口模块 ≤16 MB；清单 ≤128 KB；模板资源另有 5 MB/文件、20 MB/总量的限制。 |
| 代码 | 只在插件出现在启用列表后才 import；import 前先做资源审计。导入入口模块时会把**入口文件所在目录**（`Path(code.entry).parent`）临时加入 `sys.path`（方便同目录互相 import），导入结束立即恢复。 |
| HTTP | 插件蓝图最后注册，核心路由优先；插件路由请自带鉴权（参考 `routes/plugins.py` 的 `_guard`）。 |
| 回滚 | `--no-plugins` / `TRPG_PLUGINS_DISABLED=1` / `PLUGINS_DISABLED` 三选一。 |
| 根目录 | 默认 `<DATA_DIR>/plugins`，可用 `app.config["PLUGIN_DIR"]` 覆盖。 |

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

`enabled.json` 缺失或损坏不会阻断启动：缺失按空列表处理；JSON 损坏时记 `plugin_state_corrupt` 警告后同样按空列表处理。此时所有第三方插件都显示为 `disabled`（`not_enabled`），修好文件后 `POST /api/plugins/rescan` 即可，不必重启。

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
| `entry.py` | 只读接口 `GET /api/plugins/hello-prop/ping`；**没有鉴权**，仅用于验证链路，生产插件请自行校验登录与角色（参考 `routes/plugins.py` 的 `_guard`）。 |
| `templates/hello-note/*` | 一张可渲染的便条模板。 |

覆盖的验收链路：装进 `data/plugins/` → 写 `enabled.json` → `/api/plugins` 显示 `loaded` → `/api/rooms/<id>/props/templates` 出现 `hello-note`（`source=plugin:hello-prop`）→ 渲染 PDF/PNG → 接口可调用。
