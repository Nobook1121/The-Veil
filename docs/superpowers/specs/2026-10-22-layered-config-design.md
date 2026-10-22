# 配置分层重构设计（Layered Config）

- 状态：设计草案，待评审后实施
- 日期：2026-10-22
- 范围：`backend/trpg_server/config/`（新增）、`data/config/`、`config/templates/`、前端 `ConfigManager`、`requirements.txt`、`THIRD-PARTY-LICENSES.md`、发布流程、测试
- 相关文档：`docs/project-map.md`、`docs/development.md`、`release/RELEASE_MANIFEST.md`

## 1. 目标与非目标

### 1.1 目标

1. **四层优先级统一加载**：代码默认值 < 包内模板 < 用户配置文件 < 环境变量/CLI。
2. **首次启动自动生成用户配置**：仓库/发布包只带模板，`data/config/` 由第一次启动生成，之后永不被覆盖。
3. **默认值可维护**：改 `config/templates/general.toml` 即可改变「第一次部署生成的默认配置」。
4. **本地配置不泄漏**：本地测试改过的 `data/config/**` 不再进入 Git，也不进入发布包。
5. **去重**：后端只保留一份 TOML 解析与一份默认值定义；前端不再自带 TOML 解析器和大量重复默认值。
6. **明确整合边界**：把「人类编辑的设置」收敛到一个文件，把「机器管理的集合/密钥/内容」保留为独立文件，并给出理由。

### 1.2 非目标

- 不统一 `aiplatform/*.json`、`aimodel/**`、`roles/roles.json` 的结构（它们是有独立增删改生命周期的集合）。
- 不引入与配置加载无直接收益的新依赖；依赖决策见 §16.1。
- 不做多租户、多套配置 profile（dev/prod 由环境变量与 `--config-dir` 覆盖）。
- 不改变权限节点语义、AI 平台回退顺序、房间/剧本等业务行为。

## 2. 现状盘点

### 2.1 配置文件与职责

| 文件 | 格式 | 主要读者 | 主要写者 | 性质 |
| --- | --- | --- | --- | --- |
| `data/config/general.toml` | TOML | `agents/config.py`、`room_log.py`、`routes/rooms.py`、`routes/characters.py`、`scenario_store.py`、`site_agent_config.py`、前端 `/config/general.toml` | `routes/config.py::save_config`（整文件重写）、`agents/config.py::write_ai_setting`（单键） | 人类设置 |
| `data/config/site.json` | JSON | `routes/setup.py` | `routes/setup.py` | 站点名与引导状态 |
| `data/config/network.json` | JSON | `network_discovery.py`、`server.py`、`routes/network.py` | `routes/network.py`、`network_admin.py` | 人类设置 |
| `data/config/penetration.json` | JSON | `network_discovery.py`、`routes/network.py` | 同上 | 人类设置（含穿透令牌） |
| `data/config/permissions.json` | JSON | `permission_config.py` | `routes/config.py`（仅保存后落盘） | 安全矩阵，机器规范化 |
| `data/config/roles/roles.json` | JSON | `role_config.py` | `routes/config.py` | 动态集合 |
| `data/config/roles/kp.md`、`debug-kp.md` | Markdown | `role_config.py`、`routes/config.py` | 设置页 | 用户内容 |
| `data/config/aiplatform/*.json` | JSON | `ai_platform_config.py` | 设置页/引导页 | 动态集合 |
| `data/config/aimodel/**/*.json` | JSON | 设置页 | 设置页 | 动态集合 |
| `data/config/character_skills.json` | JSON | `routes/characters.py` | 手工 | 规则参考数据 |
| `AI_TRPG_*` 环境变量 | env | `settings.py`、`users/manager.py`、`server.py` | — | 基础设施/密钥 |
| `server.py` 位置参数 | CLI | `server.py::_select_port` | — | 端口 |
| `data/runtime/flask_secret_key.txt` | 文本 | `settings.py` | `settings.py` | 运行时密钥 |

`git ls-files data/config` 确认以上文件（除 `permissions.json` 外）当前全部被 Git 跟踪，因此本地测试状态会直接进入提交与发布包。

### 2.2 现存问题

- **P1 本地状态入库**：`data/config/*.json` 被跟踪，本地测试的站点名、端口、权限、AI 平台会随提交上传；发布时依赖人工删除。
- **P2 默认值三处分散**：代码里的兜底（如 `DEFAULT_NETWORK_CONFIG`、`AIRuntimeConfig`）、磁盘 JSON 默认、前端 `ConfigManager.get(..., default)` 各写一份，容易漂移。
- **P3 无注释来源**：JSON 无法写注释；`general.toml` 一旦被设置页保存就被 `convert_to_toml` 整文件重写，注释与顺序丢失。
- **P4 首次启动不生成**：除密钥与 SQLite 外，缺失文件只走代码兜底且不落盘，用户看不到「默认配置长什么样」。
- **P5 解析器重复**：后端 `agents/config.py` 与 `routes/config.py` 各有一套手写 TOML 行解析；前端 `ConfigManager.parseTOML` 是第三套，三者的引号/点号处理已经出现过历史脏数据。
- **P6 文件分散**：`site.json`、`network.json`、`penetration.json` 都是小型人类设置，却各自独立、无法注释。
- **P7 生效值不可见**：环境变量覆盖只影响后端运行时，设置页仍显示文件值，管理员无法判断某项是否被部署层锁定。

## 3. 目标模型

### 3.1 优先级（从低到高）

```
L1 代码默认值（schema.py 中的 dataclass 字段）
  < L2 包内模板（config/templates/*.toml，带注释，只读）
    < L3 用户配置（data/config/general.toml，首次启动从模板生成）
      < L4 环境变量（AI_TRPG_CFG_*）
        < L5 CLI（--set section.key=value）
```

用户表述的「环境变量 / CLI」合并为最高层，内部再分：**CLI > 环境变量**。

### 3.2 合并粒度与类型规则

- 以「叶子键」为合并单位，section 内逐键覆盖；数组整体替换，不做元素级合并。
- 每个键在 `schema.py` 中声明一个转换器（bool/int/enum/text/路径/时长）。
- 每层先独立解析并转换，再合并类型化结果：某一层的非法值被丢弃并记录 `WARNING`，回退到下一优先级层的合法值，最终回退到代码默认值。
- 未知键：保留在用户文件中不删除，启动时记录一次 `WARNING`（帮助发现拼写错误）；不参与解析。
- 内存中的规范形态保持现有的 `{section: {key: value}}`，section 名是扁平字符串（如 `"ai.small_models"`），与前端现有对象形状一致，避免前后端契约变化。

### 3.3 生效时机

- 绝大多数设置按请求/按操作读取，改文件后下一次读取即生效（通过 `(path, mtime)` 缓存失效）。
- `network.port` 在启动时读取，修改后需重启；`network.penetration` 的启停需重启穿透进程。两者在文档和 UI 中明确标注。

## 4. 配置整合决策

### 4.1 合并进 `general.toml`（推荐）

| 原文件 | 新位置 | 理由 |
| --- | --- | --- |
| `site.json` | `[site]` | 3 个字段，纯人类设置，适合注释 |
| `network.json` | `[network]` | 小型人类设置，`server.py` 可在无 Flask 时读取 |
| `penetration.json` | `[network.penetration]` | 同上；`data/config` 变为不入库后，令牌不再有入库风险 |

这三者字段少、读写路径清晰、迁移成本低，合并后「人类设置」从 4 个文件降为 1 个。

### 4.2 保留独立

| 文件 | 保留理由 |
| --- | --- |
| `permissions.json` | 安全矩阵，有独立的默认值补全/规范化契约与整表写回；与设置文件同文件会放大「一处 TOML 语法错误导致整站设置不可读」的爆炸半径 |
| `roles/roles.json` + `roles/*.md` | 动态集合 + 长文本内容，按条目增删，不适合塞进设置文件 |
| `aiplatform/*.json` | 每个平台独立文件、独立启停/排序/密钥分离；引导页会创建第一条 |
| `aimodel/**/*.json` | 按平台/模型生成的请求模板，数量随用户增长 |
| `data/runtime/config/aiplatform/*.json` | 密钥必须与公开配置分离（现状已正确） |

### 4.3 移出 `data/config`

- `data/config/character_skills.json` → `data/character_skills.json`：它是随包发布的**规则参考数据**，不是用户配置，不应随着 `data/config` 被忽略。与 `data/occupations/`、`data/weapons/` 同级。

### 4.4 结论

- 「设置」收敛为 **1 个 TOML**（`general.toml`）。
- 「机器管理的集合 / 密钥 / 内容 / 参考数据」保持 **4 类独立文件**，按生命周期分离，而不是按主题强行合并。

## 5. 目录与模块设计

### 5.1 目标目录

```
config/                          # 跟踪、随包发布，只读模板
  templates/
    general.toml                 # 带注释的完整默认设置（唯一人类可读默认值）
    roles/
      kp.md
      debug-kp.md
    aiplatform/
      default-request.json
  vector-embedding-ocr.example.env

data/
  character_skills.json          # 跟踪，参考数据
  config/                        # 不跟踪，首次启动生成/用户写入
    general.toml
    permissions.json             # 首次保存权限时创建
    roles/roles.json
    roles/kp.md
    roles/debug-kp.md
    aiplatform/default-request.json
    aiplatform/<platform>.json
    aimodel/<platform>/<model>.json
  runtime/config/aiplatform/     # 密钥，不跟踪
```

### 5.2 新增后端模块

```
backend/trpg_server/config/
  __init__.py       # 公共 API：load_settings / update_setting / ensure_user_config
  paths.py          # CONFIG_DIR、TEMPLATE_DIR、--config-dir 解析（纯 stdlib）
  fields.py         # FieldSpec、转换器、env 名派生、冲突检测
  schema.py         # 各 section 的 dataclass 默认值（超过 300 行时拆 schema_ai.py）
  sources.py        # tomlkit 读取 + 扁平化 + 逐层类型化
  merge.py          # 分层合并与缓存
  writer.py         # 基于 tomlkit 的注释保留写入（diff + 原子替换 + 进程内锁）
  bootstrap.py      # 首次启动生成
```

约束：

- 该包除 `tomlkit` 外只依赖标准库，不 import Flask，保证 `server.py` 在创建 app 之前就能读取 `network.port`。
- `settings.py` 中的 `CONFIG_DIR` 等常量改为从 `config/paths.py` 导入再导出，避免 40+ 处调用点被迫修改。

### 5.3 关键接口

```python
# trpg_server/config/__init__.py
def load_settings(config_dir: Path | None = None, *,
                  env: Mapping[str, str] | None = None,
                  cli_overrides: Mapping[str, Any] | None = None) -> Settings: ...

def get_ai_config(config_dir: Path | None = None) -> AIRuntimeConfig: ...  # 兼容旧调用

def ensure_user_config(config_dir: Path | None = None,
                       template_dir: Path | None = None) -> list[Path]: ...

def update_section(config_dir: Path | None, section: str,
                   values: Mapping[str, Any]) -> Path: ...

def effective_overrides(config_dir: Path | None = None) -> dict[str, str]:
    """返回被 env/CLI 锁定的键 -> 来源，供设置页显示。"""
```

`Settings` 结构：

```python
@dataclass(frozen=True)
class Settings:
    appearance: AppearanceConfig
    language: LanguageConfig
    notification: NotificationConfig
    autosave: AutosaveConfig
    room_console: RoomConsoleConfig
    chat: ChatConfig
    ai: AIRuntimeConfig          # 复用并迁移现有 dataclass
    character_rules: CharacterRulesConfig
    scenario: ScenarioConfig
    window: WindowConfig
    update: UpdateConfig
    site_agent: SiteAgentConfig  # 复用现有
    scenario_import: ScenarioImportConfig
    site: SiteConfig
    network: NetworkConfig
```

各 section 的读取函数改为：

```python
# 旧
config = load_ai_runtime_config(config_dir)
# 新（保留同名 facade，内部走统一加载）
config = get_ai_config(config_dir)
```

`room_log.py`、`routes/rooms.py`、`routes/characters.py`、`scenario_store.py` 等模块直接取 `load_settings(config_dir).room_console` 等，不再各自手写 TOML 行解析。

## 6. 配置域与字段规格

字段规格继续使用现有 `_FieldSpec` 思路（名称、section、别名、转换器），迁移到 `config/fields.py`，作为**唯一**默认值与校验来源。

| section | 主要读者 | 需重启 | env 示例 |
| --- | --- | --- | --- |
| `appearance` | 前端 | 否 | `AI_TRPG_CFG_APPEARANCE_THEME` |
| `language` | `logging_config.py`、前端 | 日志语言需重启 | `AI_TRPG_CFG_LANGUAGE_LANGUAGE` |
| `notification` | 前端 | 否 | `AI_TRPG_CFG_NOTIFICATION_ENABLE_SOUND` |
| `autosave` | `routes/rooms.py` | 否 | `AI_TRPG_CFG_AUTOSAVE_INTERVAL` |
| `room_console` | `room_log.py` | 否 | `AI_TRPG_CFG_ROOM_CONSOLE_ENABLED` |
| `chat` | 前端 | 否 | `AI_TRPG_CFG_CHAT_MAX_HISTORY_MESSAGES` |
| `ai` | `agents/config.py` | 否 | `AI_TRPG_CFG_AI_MAX_TOKENS` |
| `ai.small_models` | `role_config.py` | 否 | `AI_TRPG_CFG_AI_SMALL_MODELS_INTENT_CLASSIFICATION` |
| `ai.prompt` | `agents/config.py` | 否 | `AI_TRPG_CFG_AI_PROMPT_MAX_TOOL_RESULT_CHARS` |
| `ai.cache` | `agents/config.py` | 否 | `AI_TRPG_CFG_AI_CACHE_SEMANTIC_ENABLED` |
| `ai.knowledge` | `agents/config.py` | 否 | `AI_TRPG_CFG_AI_KNOWLEDGE_TOP_K_DEFAULT` |
| `character_rules` | `routes/characters.py` | 否 | `AI_TRPG_CFG_CHARACTER_RULES_MAX_CARDS_PER_USER` |
| `scenario` | `scenario_store.py` | 否 | `AI_TRPG_CFG_SCENARIO_TRIGGER_MAX_FILE_SIZE` |
| `window` | 前端 | 否 | `AI_TRPG_CFG_WINDOW_WINDOW_WIDTH` |
| `update` | 前端 | 否 | `AI_TRPG_CFG_UPDATE_AUTO_UPDATE` |
| `site_agent` | `site_agent_config.py` | 否 | `AI_TRPG_CFG_SITE_AGENT_ENABLED` |
| `scenario_import` | 导入流水线 | 否 | `AI_TRPG_CFG_SCENARIO_IMPORT_STREAM_OUTPUT` |
| `site` | `routes/setup.py`、前端 | 否 | `AI_TRPG_CFG_SITE_NAME` |
| `network` | `server.py`、`routes/network.py` | `port` 需重启 | `AI_TRPG_CFG_NETWORK_PORT` |
| `network.penetration` | `routes/network.py` | 是 | `AI_TRPG_CFG_NETWORK_PENETRATION_ENABLED` |

**env 名派生规则**：`AI_TRPG_CFG_` + section 去点转下划线 + `_` + key，全大写。启动时建立反向映射并检测冲突；若未来出现冲突，可在 `FieldSpec` 中显式指定 `env_name`。

**保留既有基础设施环境变量**：`AI_TRPG_SECRET_KEY`、`AI_TRPG_VECTOR_*`、`AI_TRPG_EMBEDDING_*`、`AI_TRPG_OCR_*`、`AI_TRPG_USER_DATABASE_FILE`、`AI_TRPG_DEV_BUILD` 等继续由 `settings.py` 直接读取，不进入 `AI_TRPG_CFG_*` 命名空间，避免破坏现有部署。

## 7. 加载流程

```python
def load_settings(config_dir=None, env=None, cli_overrides=None):
    config_dir = resolve_config_dir(config_dir)
    template = read_toml(TEMPLATE_DIR / "general.toml")      # L2，失败则 {}
    user     = read_toml(config_dir / "general.toml")        # L3，失败则 {}
    legacy   = read_legacy_json(config_dir)                  # 兼容期：site/network/penetration
    env_raw  = collect_env(env or os.environ)                # L4
    cli_raw  = cli_overrides or {}                           # L5

    layers = [template, legacy, user, env_raw, cli_raw]
    partials = [typed_partial(layer) for layer in layers]    # 逐层转换，非法值丢弃+WARN
    merged = merge_partials(partials)
    return build_settings(merged)
```

要点：

- `read_toml` 使用 `tomlkit`（TOML 1.0，往返保留注释），替代现有手写解析器；读取后经 `TOMLDocument.unwrap()` 转为普通 dict 参与合并。
- 嵌套表在读取后扁平化为 dotted section（`[ai.prompt]` → `"ai.prompt"`），与前端对象形状一致；扁平化逻辑位于 `sources.py`。
- 缓存键为 `(config_dir.resolve(), user_mtime_ns, user_size, env_signature)`；命中缓存时直接返回不可变 `Settings`。
- 写文件后主动失效缓存，避免读到自己写之前的值。

## 8. 首次启动与模板

### 8.1 生成顺序

`ensure_user_config()` 是幂等的，绝不覆盖已存在文件：

1. `mkdir -p data/config/roles data/config/aiplatform`
2. `general.toml` 不存在 → 从 `config/templates/general.toml` **原样复制**（保留注释）。
3. `roles/kp.md`、`roles/debug-kp.md` 不存在 → 从模板复制。
4. `aiplatform/default-request.json` 不存在 → 从模板复制。
5. `permissions.json`、`roles/roles.json`、平台配置**不预生成**：它们在首次保存/首次创建时写入，读取时使用代码默认值。
6. 记录 `INFO config_seeded file=... source=template`。

调用点：

- `server.py`：在 `configure_logging()` 之前调用（日志语言需要读取 `general.toml`）。
- `create_app()`：开头再调用一次（测试、WSGI 部署、其他入口同样安全）。
- 模板缺失时：由 schema 渲染一份无注释的最小文件，并记录 `WARNING`。

### 8.2 模板规则

- `config/templates/general.toml` 是**唯一带注释的默认值文件**，包含全部键。
- 模板值必须与代码默认值一致；由测试 `test_config_template_sync.py` 强制校验（键集合 + 值）。
- 模板中不得出现任何密钥（API Key、穿透令牌默认留空）。
- 升级新增键：用户文件缺少时由 L2 模板在运行时补齐，**不**回写用户文件，保证用户文件不被自动改写。

### 8.3 「修改第一次部署默认值」的操作方式

直接编辑 `config/templates/general.toml` 并提交。新部署首次启动复制该模板即为默认配置；已有部署不受影响。

## 9. 写入模型

### 9.1 注释保留写入（tomlkit）

不手写行级替换逻辑，直接使用 `tomlkit` 的往返解析：

```python
def update_section(path: Path, section: str, values: Mapping[str, Any]) -> None:
    """读取 TOMLDocument，仅更新给定 section 中的键，注释、键序、空白原样保留，原子写回。"""
```

- `tomlkit.parse(text)` 保留全部 trivia（注释/空行/顺序）；对 document 赋值只改对应节点。
- 不存在的 section 由 tomlkit 自动追加到文件末尾。
- 行尾注释、节首注释、键间空行均不需自研代码处理，消除了原设计中最大的一块自研逻辑与测试面。
- `writer.py` 只保留自研部分：变更 diff（不写未变键）、进程内锁、临时文件 + `os.replace` 原子替换、写后缓存失效。

### 9.2 保存语义

`POST /api/config/general`（沿用现有路由）改为：

1. 校验权限（不变）。
2. 读取当前**生效值**与被 env/CLI 锁定的键集合。
3. 丢弃请求体中属于锁定键的字段（部署层优先，不允许被 UI 覆盖）。
4. 与磁盘用户文件逐键 diff，只写入发生变化的键；未提交的 section/键保持不变。
5. 通过 `writer.update_keys()` 原子写入；成功后失效缓存。

### 9.3 并发

`writer.py` 使用模块级 `threading.Lock` 串行化「读-改-写」；临时文件 + `os.replace` 保证原子性。控制台命令与 Web 设置页可能并发写入，锁必须覆盖整个读改写过程。

## 10. 前端契约

1. 新增 `GET /api/config/general`：返回**生效配置**的 JSON（`{section: {key: value}}`）与 `overrides`（被 env/CLI 锁定的键及来源）。
2. `ConfigManager.loadConfig("general")` 改为请求该接口；**删除** `parseTOML`、`normalizeTomlKey` 及 70 余行解析代码。
3. `ConfigManager.get(section, key, default)` 的默认值参数保留签名，但调用点逐步移除硬编码默认值——生效配置保证键齐全。
4. 设置页对 `overrides` 中的字段加锁/禁用，并显示「已被部署配置覆盖」提示；`POST` 时这些字段不参与写回。
5. `/config/<path>` 静态路由保留（供用户直接查看文件、AI 平台 JSON 使用），但不再作为前端设置加载入口。

## 11. 环境变量与 CLI

### 11.1 环境变量

```powershell
$env:AI_TRPG_CFG_AI_MAX_TOKENS = "8192"
$env:AI_TRPG_CFG_NETWORK_PORT = "9000"
$env:AI_TRPG_CFG_AI_SMALL_MODELS_INTENT_CLASSIFICATION = "qwen-turbo"
```

- 非法值：记录 `WARNING env=... key=... reason=...`，回退到文件值。
- 未知 `AI_TRPG_CFG_*`：记录一次 `WARNING unknown_config_env=...`，不 fatal。

### 11.2 CLI

```text
python server.py [port] [--port 8086] [--config-dir PATH] [--set section.key=value ...]
```

- `--set` 可重复，解析为 `{section: {key: value}}`，优先级高于环境变量。
- 位置参数 `port` 保持兼容；`--port` 为显式形式。
- CLI 解析必须在 `ensure_user_config()` 与 `_select_port()` 之前完成。

## 12. Git 与发布包

### 12.1 Git

1. `git mv` 把默认内容迁到 `config/templates/`（保留历史）。
2. `data/config/character_skills.json` `git mv` 到 `data/character_skills.json`。
3. `git rm -r --cached data/config`（保留工作区文件，本地测试配置立即变为未跟踪）。
4. `.gitignore` 增加：

```gitignore
# 运行时用户配置：由 config/templates 首次启动生成
/data/config/
```

5. 本地已有的 `aiplatform/*.json`、`aimodel/`、`roles/roles.json`、`permissions.json` 保留在磁盘，仅变为未跟踪。

### 12.2 发布包

- 发布包包含 `config/templates/**` 与 `data/character_skills.json`、`data/occupations/`、`data/weapons/`。
- 发布包**不包含** `data/config/**`、`data/runtime/**`、`data/scenarios/**`。
- `release/RELEASE_MANIFEST.md` 的打包原则改为：「不带用户配置，首次启动从模板生成；`site.setup_dismissed=false` 由模板默认值保证」。
- 打包脚本（若新增 `scripts/package-release.ps1`）在压缩前执行断言：
  - `git ls-files data/config` 为空；
  - 包内不存在 `data/config`；
  - 包内存在 `config/templates/general.toml`；
  - 包内不存在 `.env`、`*.secret`、`*.sqlite3`。

### 12.3 首次部署引导

`site.setup_dismissed` 默认值来自模板（`false`），因此任何新部署首次启动都会进入引导；本地已完成的引导只写入本地 `data/config`，不会进入发布包。这直接修复了 `docs/problems.md` 记录的「release 没有引导页」问题。

## 13. 迁移方案

1. **兼容读取**：`sources.py` 在 `general.toml` 缺少 `[site]`/`[network]`/`[network.penetration]` 时，读取遗留 `site.json`/`network.json`/`penetration.json` 作为 L3 的一部分（仅兼容期）。
2. **不自动删除**遗留文件；首次启动只生成缺失文件。
3. 新增控制台命令（遵循现有命令规范）：

```text
config show [--section <name>]        查看生效配置与每项来源
config migrate [--yes]                把遗留 JSON 合并进 general.toml，原文件改名为 *.legacy
config path                           打印配置目录与模板目录
```

4. 迁移完成后可删除兼容读取代码（单独提交，避免长期维护两套路径）。

## 14. 测试计划

新增：

- `tests/test_config_layers.py`：五层优先级矩阵；非法值回退；数组整体替换；未知键告警。
- `tests/test_config_bootstrap.py`：首次生成、幂等、不覆盖、模板缺失、原子写入、权限文件不预生成。
- `tests/test_config_template_sync.py`：模板键集合与代码 schema 完全一致；值一致；无密钥字段非空。
- `tests/test_config_writer.py`：diff 只写变更键、dotted section、追加 section、并发写、原子替换（注释保留由 tomlkit 保证，仅做冒烟断言）。
- `tests/test_config_env_cli.py`：env 名派生、冲突检测、CLI 高于 env、锁定键不可被 API 覆盖。

调整：

- `test_network_share_config.py`、`test_permission_config.py`、`test_role_config_and_module_summary.py`、`test_small_model_config.py`、`test_kp_prompt_and_stream_config.py`、`test_static_asset_cache.py` 中涉及 `general.toml`/`network.json`/`site.json` 的夹具改为统一 loader 或临时配置目录。
- `scripts/verify.ps1` 增加一项：`git ls-files data/config` 必须为空（防止误提交回升）。

## 15. 实施阶段

### Phase 0：地基（低风险，先落地）

1. 引入依赖：`requirements.txt` 增加 `tomlkit>=0.12`，并在 `THIRD-PARTY-LICENSES.md` 登记其 MIT 许可证（项目硬性要求）。
2. 新增 `trpg_server/config/` 包（paths/fields/schema/sources/merge/bootstrap/writer）。
3. `agents/config.py` 的 `_FIELD_SPECS` 与 dataclass 迁入 `config/schema.py`；`load_ai_runtime_config` 改为 facade。
4. `general.toml` 改为首次启动从模板生成；新增 `config/templates/general.toml`（带完整注释）。
5. `.gitignore` 忽略 `data/config/`，`git rm --cached`；迁移模板文件。
6. 完成后：本地测试配置不再入库、发布包不再带配置、默认值可改模板。

### Phase 1：整合 site/network/penetration

1. `setup.py`、`routes/network.py`、`network_discovery.py`、`server.py` 改读写 `[site]`、`[network]`、`[network.penetration]`。
2. 兼容读取遗留 JSON（§13）。
3. 更新涉及这些文件的测试。

### Phase 2：前端与写入

1. `ConfigManager` 改用 `/api/config/general` JSON，删除 JS TOML 解析器。
2. `save_config` 改为 diff + 锁定键过滤 + 注释保留写入。
3. 设置页显示部署锁定项。

### Phase 3：env/CLI

1. `AI_TRPG_CFG_*` 收集与冲突检测。
2. `server.py` 增加 `--config-dir`、`--set`；`_select_port` 走统一 loader。
3. `config show/migrate/path` 控制台命令与文档。

### Phase 4：清理

1. 删除后端剩余的手写 TOML 行解析与重复默认值。
2. 移除兼容读取（视迁移完成情况）。
3. 更新 `docs/project-map.md`、`docs/development.md` 与本地 wiki。

## 16. 风险与取舍

### 16.1 依赖决策

用户已放宽「零外部依赖」约定：引入依赖以换取更少自研代码与更高正确性是可接受的，但须逐一论证收益并登记许可证。

**引入：`tomlkit>=0.12`（MIT）**

- 收益：原生往返解析，注释/键序/空白全保留，直接替代原设计中的手写 TOML 行解析（后端两处）与手写「注释保留写入器」（约 200+ 行自研代码及其测试面）。这是本重构中最大的去冗余来源。
- 成本：纯 Python、无传递依赖、许可证宽松；发布包随 `requirements.txt` 安装，无需额外打包处理。
- 对比：`tomllib`（stdlib）只能读、丢弃注释；`toml`（老库）往返支持弱；`tomlkit` 是唯一成熟的注释保留方案。

**暂不引入：Pydantic / pydantic-settings**

- 现有 `AIRuntimeConfig` + `_FieldSpec`（迁移后位于 `config/schema.py`）已覆盖「默认值 + 转换 + 边界 clamp」，校验总量小、无深层嵌套。
- pydantic-settings 的 env 命名映射与本设计的 `AI_TRPG_CFG_*` 派生规则、冲突检测、按层告警语义不吻合，引入后仍需大量自定义 glue，净收益为负；且 `pydantic-core` 为 Rust 二进制依赖，扩大发布与许可证面。
- 若未来 `aiplatform/*.json` 等平台配置需要复杂 schema 校验，可单独评估在该子系统引入 Pydantic，与本次解耦。

**不引入：`watchdog`（文件监听）**——配置读取已按 `(path, mtime, size)` 缓存失效，变更检测成本为一次 `stat`，无需文件系统事件。

### 16.2 主要风险

| 风险 | 缓解 |
| --- | --- |
| 一处 TOML 语法错误导致整份设置不可读 | 读取失败时回退模板+代码默认并 `ERROR` 日志；保留 `permissions.json` 独立，缩小爆炸半径 |
| 合并 `network.json` 破坏 `server.py` 启动前读取 | `config` 包纯 stdlib，`server.py` 在 import app 前调用 `load_settings` |
| 旧部署遗留 JSON 被忽略 | 兼容期按 §13 读取，提供 `config migrate` |
| 设置页保存覆盖 env 覆盖值 | 后端过滤锁定键，前端禁用输入 |
| 测试夹具散落 | 统一通过 `create_app({"CONFIG_DIR": tmp_path})` 或 `load_settings(tmp_path)` 注入 |
| 单文件 `schema.py` 过大 | 按域拆分，单文件控制在 ~300 行内 |

## 17. 验收清单

- [ ] 删除 `data/config/` 后首次启动成功，生成带注释的 `data/config/general.toml`。
- [ ] 修改 `config/templates/general.toml` 后，全新部署的默认值随之变化。
- [ ] `AI_TRPG_CFG_AI_MAX_TOKENS=8192` 生效；`--set ai.max_tokens=4096` 覆盖它。
- [ ] 设置页保存后 `general.toml` 的注释与未修改键保持不变（tomlkit 往返保证）。
- [ ] `git ls-files data/config` 为空；发布包内不含 `data/config/`。
- [ ] 发布包首次启动进入部署引导（`setup_dismissed=false`）。
- [ ] 后端只剩一处 TOML 读写实现（tomlkit）；前端不再包含 TOML 解析器。
- [ ] `THIRD-PARTY-LICENSES.md` 包含 tomlkit 的 MIT 许可证条目。
- [ ] 现有全量测试通过，新增 5 组配置测试通过。
