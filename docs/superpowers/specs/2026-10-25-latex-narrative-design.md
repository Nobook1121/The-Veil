# KP 与玩家输出的 LaTeX 叙事渲染 · 功能设计文档

- 文档状态：待评审（v4）
- 编写日期：2026-10-25（v4 更新：2026-10-27）
- 目标版本：待定（建议随下一 b 版本）
- 影响范围：管理员设置、KP 提示词段、聊天渲染链路、AI 历史清洗、缓存指纹、前端样式、第三方依赖与许可证
- 关联模块：`backend/trpg_server/agents/latex.py`、`backend/trpg_server/config/schema.py`、`config/templates/general.toml`、`backend/trpg_server/routes/chat.py`、`backend/trpg_server/routes/chat_helpers.py`、`backend/trpg_server/agents/cache.py`、`frontend/src/app/latex.ts`、`frontend/src/app/chat.ts`、`frontend/src/app/config/ConfigManager.ts`、`frontend/src/app/tabs.ts`、`frontend/src/index/fragments/*`、`frontend/src/styles/01-foundation-chat.css`
- 本文只做设计，不含实施代码。

---

## 1. 背景与目标

### 1.1 背景

当前聊天只有 Markdown 排版，缺少「氛围性排版」能力。跑团叙事中经常需要：

- 让某个词句变色、变大或缩小，制造不安感；
- 让文字错位、重叠、漂移，表现「文字恐怖」；
- 用色块/方块字符排出简单图形（门缝、眼睛、噪点），提升沉浸感。

LaTeX（经 KaTeX 子集渲染）可以用纯文本表达上述效果，且天然适合由模型生成。玩家同样可能希望用相同语法参与氛围表达。

### 1.2 目标

1. KP 与玩家发送的消息都支持 LaTeX 片段，聊天框渲染为 HTML，房间内所有玩家都能看到渲染结果。
2. 通过注入「叙事渲染 Skill」提示词段，让 KP 知道**怎么用、什么时候用、用多少**；强度由管理员档位控制。
3. 插入强度适中：默认可以完全不用，单条回复的公式占比有硬上限，禁止通篇 LaTeX。
4. **进入 AI 上下文前必须清洗 LaTeX**：玩家消息与 AI 历史消息在作为历史/当前输入发给模型前，先扁平化为纯文本，防止上下文与输入 token 爆炸；持久化与显示仍保留原始内容。
5. 聊天窗口完整适配：即时消息、他人消息、历史回放、渐进显现（stream reveal）、移动端横向溢出。
6. 管理员设置提供**多级开关**：总控、KP 输出、玩家消息、强度、三个上限。
7. 实现直接落在现有代码里，不引入插件化架构、特性包或扩展点。

### 1.3 非目标

- **不做插件化**：不预留注册 API、动态加载、扩展钩子或门面抽象。LaTeX 就是普通功能代码，后续若需要插件化再另行重构。
- 不实现完整 LaTeX 排版（不使用 TikZ、不使用外部图片、不做 PDF 级排版）。
- 不开放用户自定义宏包、`\newcommand`、`\input` 等能力。
- 不做房间级独立开关（LaTeX 属站点级展示偏好；房间级留作后续扩展）。
- 本期不改 React 的网站 Agent 聊天窗（`AgentChat.tsx`）。
- 不为「玩家输入」提供所见即所得的 LaTeX 编辑器或预览面板。

---

## 2. 现状与约束（已核对）

### 2.1 当前渲染链路

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| Markdown 渲染 | `frontend/src/app/chat.ts:715-735` | `marked.parse` + `DOMPurify.sanitize`；组件缺失时 `chatEscapeHtml` 降级 |
| 消息插入 | `frontend/src/app/chat.ts:1054-1098` | `addMessage` 生成 `.message-content` 后调用 `enhanceMessageMedia`（仅处理 img/video/a） |
| 己方玩家消息 | `frontend/src/app/chat.ts:620-626` | `sendVisibleMessage("player", …)` → `addMessage` |
| 他人消息 | `frontend/src/app/chat.ts:1396-1425` | `new_message` 到达后映射为 `player`/`other` 再 `addMessage` |
| KP 回复替换 | `frontend/src/app/chat.ts:1219-1225` | `innerHTML = renderMarkdown(...)` → `enhanceMessageMedia` → `revealKpContent` |
| 渐进显现 | `frontend/src/app/chat.ts:1149-1199` | `TreeWalker(SHOW_TEXT)` 收集**所有**文本节点，逐字写入 `nodeValue`；目前不过滤 `code`/`pre` |
| 消息广播 | `backend/trpg_server/socket_events.py:130-142` | `send_message` → `socketio.emit("new_message", payload, room=…)`，内容原样转发 |
| 第三方库 | `frontend/src/index/fragments/05-platform-modals-and-scripts.html:171-176` | marked 15、DOMPurify 3.4.16 走 jsDelivr CDN；KaTeX 0.16.11 本地随包（`data/assets/vendor/katex/`） |
| 样式入口 | `frontend/src/styles/01-foundation-chat.css` | 聊天消息样式已集中在此 |
| CSP | `backend/trpg_server/app_factory.py:41-53` | `script-src` 含 jsDelivr；`style-src`/`font-src` 允许 https 与 `'self'`，本地 KaTeX 的 CSS/JS/字体同源加载 |

关键约束：**渐进显现会改写文本节点的内容**。如果公式在显现前就被 KaTeX 替换成大量嵌套 `<span>`/MathML，逐字显现会把公式内部文字截断，出现乱码式闪烁。必须让显现逻辑跳过已渲染的公式子树。

### 2.2 提示词结构

- KP 系统提示词基准来自 `data/config/roles/kp.md`（模板 `config/templates/roles/kp.md`）。
- 常驻行为约束、开场门控、JSON 格式约束、直接导入约束均由提示词段机制组织，具体段序与注册 API 以现有实现为准。
- LaTeX 按同一机制**注册为一个独立段**，不采用散落的 `if … +=` 形式。
- 该段位于静态层（`global_rules`），可被前缀缓存复用。

### 2.3 配置体系

- 布尔、整数、枚举项写法参考 `backend/trpg_server/config/schema.py:175-193`；枚举转换器为 `config/fields.py:59-69` 的 `as_enum(field_name, allowed)`；整数用 `clamped_int(minimum, maximum)`。
- 模板 `config/templates/general.toml:30-33` 已有 `[chat]` 段。
- 前端回填在 `frontend/src/app/config/ConfigManager.ts:57-106`，事件绑定在 `frontend/src/app/tabs.ts:224-258`，控件 HTML 在 `frontend/src/index/fragments/03-room-tools-auth-settings.html:620-648`。
- `tests/test_config_template_sync.py` 强制 schema 与模板双向同步；`tests/test_kp_prompt_and_stream_config.py:40-48` 是「设置 + 提示词 + 前端开关」三段式断言范例。

### 2.4 AI 历史与长度预算

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| 历史裁剪 | `chat_helpers.py:842-861` `_history_for_request` | 只保留摘要 + 最近 12 条，总字符上限 8000 |
| 触发压缩 | `chat_helpers.py:537` `_history_needs_compaction` | 按历史字符数阈值判断是否调用小模型压缩 |
| AI 压缩 | `chat_helpers.py:635-671` `_compact_history_with_ai` | 把摘要 + 新增对话 JSON 化后发给小模型 |
| 检索 query | `chat_helpers.py:864-876` `_retrieval_query` | 取最近 3 轮对话片段拼成向量检索 query |
| 组装消息 | `chat_helpers.py:828-839` `_build_messages` | 历史与当前输入在这里拼进 messages |

结论：只要在任何一条「出站到模型」的路径上出现大量 LaTeX，就会同时放大：历史预算占用、压缩触发频率、embedding 成本、最终 prompt token。清洗必须放在这些**出口**上，而不是改动持久化内容。

### 2.5 缓存

- 前缀缓存键 `prompt_layers.cache_key` 只包含 `scenario/scene/rules` 版本。
- 精确缓存键由 `agents/cache.py:110-121` 生成，只含剧本/场景/状态/用户输入。
- 语义缓存键为 `semantic_state` + 用户输入。

开关、强度与提示词上限会改变系统提示词与输出风格，若缓存键不带上这些状态，关闭后可能复用「带公式」的旧缓存，或打开后复用「无公式」的旧缓存。**必须把 LaTeX 状态纳入三类缓存指纹**。

---

## 3. 总体方案

```
管理员设置
  latex_enabled（总控）
  ├─ latex_kp_enabled（KP 输出）
  ├─ latex_player_enabled（玩家消息渲染）
  ├─ latex_intensity（强度）
  └─ 三个上限（公式数 / 单块长度 / 清洗长度）
        │
        ├─ 后端 agents/latex.py（单模块，非插件）
        │      ├─ load_latex_settings
        │      ├─ build_latex_prompt（总控 && KP 开关）
        │      ├─ flatten_latex / clean_history_for_model（始终执行）
        │      └─ latex_cache_variant
        │
        ├─ 后端核心（直接调用单模块函数）
        │      ├─ routes/chat.py：注册提示词段 + 传入 variant
        │      └─ chat_helpers.py：五个出口调用清洗函数
        │
        └─ 前端 app/latex.ts（单模块）
               ├─ getLatexSettings（读取开关、强度、上限）
               ├─ protectLatex（公式 → 占位 span）
               └─ decorateLatex（KaTeX 渲染 / 回退）
        前端核心 chat.ts
               ├─ renderMarkdown(content, allowLatex)
               ├─ addMessage：按消息类型决定是否渲染
               └─ revealKpContent：跳过公式子树
```

设计原则：

1. **显示用原文，模型用清洗文本**：消息存储与广播保持原文；只有「发给 AI 的副本」做扁平化。
2. **先净化、后渲染公式**：KaTeX 输出永远不经过 `innerHTML` 反序列化，只在净化后的 DOM 上由库生成。
3. **公式源码只进 `data-latex` 属性**：占位 span 携带 URI 编码后的源码与显示模式标记。
4. **失效即退化**：KaTeX 未加载、命令不支持、源码过长、公式超量时，一律显示原始 `$...$` 文本。
5. **开关先于一切**：总控关闭时，后端不生成提示词段、前端不识别也不渲染；清洗仍然执行。

### 3.1 实现落点（不做插件化）

- 后端所有 LaTeX 逻辑集中在一个普通模块 `backend/trpg_server/agents/latex.py`，内部分为「配置解析 / 提示词 / 清洗 / variant」四段注释区，不拆包、不建注册表、不导出扩展接口。
- 前端所有 LaTeX 逻辑集中在一个普通模块 `frontend/src/app/latex.ts`，`chat.ts` 直接 import 具体函数；不建 `features/` 目录、不做门面对象。
- 样式直接追加到现有 `01-foundation-chat.css`，不新增 CSS 文件与额外 `@import`。
- 之所以仍然单独成文件：避免 `chat.py` / `chat_helpers.py` / `chat.ts` 继续膨胀（符合项目「单文件不过大」的约定），同时保持改动可定位。这是普通的代码组织，不是插件化预留。

---

## 4. 配置项设计

### 4.1 新增配置（七项）

`schema.py` 的 `[chat]` 组新增：

```python
FieldSpec("latex_enabled", "chat", ("latex_enabled",), as_bool, True),
FieldSpec("latex_kp_enabled", "chat", ("latex_kp_enabled",), as_bool, True),
FieldSpec("latex_player_enabled", "chat", ("latex_player_enabled",), as_bool, True),
FieldSpec(
    "latex_intensity", "chat", ("latex_intensity",),
    as_enum("latex_intensity", frozenset({"light", "standard", "rich"})), "standard",
),
FieldSpec(
    "latex_max_formulas_per_message", "chat", ("latex_max_formulas_per_message",),
    clamped_int(minimum=1, maximum=50), 12,
),
FieldSpec(
    "latex_max_formula_chars", "chat", ("latex_max_formula_chars",),
    clamped_int(minimum=50, maximum=2000), 300,
),
FieldSpec(
    "latex_max_clean_message_chars", "chat", ("latex_max_clean_message_chars",),
    clamped_int(minimum=200, maximum=10000), 2000,
),
```

`config/templates/general.toml`：

```toml
[chat]
show_timestamp = true
message_font_size = 14
max_history_messages = 1000
# LaTeX 叙事排版总控；关闭后 KP 不注入 LaTeX 提示词，KP 与玩家消息都不渲染。
latex_enabled = true
# KP 输出是否使用 LaTeX；仅总控开启时生效。
latex_kp_enabled = true
# 玩家（含其他玩家）消息是否渲染 LaTeX；仅总控开启时生效。
latex_player_enabled = true
# 叙事排版强度：light（克制）/ standard（标准）/ rich（丰富）；仅总控与 KP 开关都开启时生效。
latex_intensity = "standard"
# 单条消息最多渲染/提示的公式片段数（行内与独立块合计）。
latex_max_formulas_per_message = 12
# 单块公式源码的最大字符数；超限片段不渲染（前端显示原文，AI 上下文替换为 [公式]）。
latex_max_formula_chars = 300
# 历史清洗后单条消息的最大字符数；超出截断，防止上下文膨胀。
latex_max_clean_message_chars = 2000
```

| 键 | 类型 | 默认 | 范围 | 作用点 |
| --- | --- | --- | --- | --- |
| `latex_enabled` | bool | `true` | — | 总控：提示词段、前端所有渲染 |
| `latex_kp_enabled` | bool | `true` | — | KP 提示词段、KP 消息渲染 |
| `latex_player_enabled` | bool | `true` | — | 玩家/他人消息渲染（不影响提示词与清洗） |
| `latex_intensity` | enum | `standard` | light/standard/rich | KP 提示词段、缓存 variant |
| `latex_max_formulas_per_message` | int | 12 | 1–50 | 前端渲染上限、KP 提示词段、缓存 variant |
| `latex_max_formula_chars` | int | 300 | 50–2000 | 前端识别上限、后端清洗、KP 提示词段、缓存 variant |
| `latex_max_clean_message_chars` | int | 2000 | 200–10000 | 后端清洗输出上限（不进提示词，也不进 variant） |

后端统一用不可变设置对象承载这七项，核心代码只传这个对象：

```python
@dataclass(frozen=True)
class LatexSettings:
    enabled: bool = True
    kp_enabled: bool = True
    player_enabled: bool = True
    intensity: str = "standard"
    max_formulas_per_message: int = 12
    max_formula_chars: int = 300
    max_clean_message_chars: int = 2000

    @classmethod
    def from_chat_section(cls, chat: Mapping[str, Any]) -> "LatexSettings": ...
    @property
    def cache_variant(self) -> str: ...
```

- 三个开关默认全开；已有用户缺键时由五级合并落到默认值，无需迁移。
- 整数越界由 `clamped_int` 自动收敛；`latex_intensity` 非法值由 `as_enum` 告警并回退 `standard`。
- `from_chat_section` 只做字段读取与类型归一，不重复实现范围校验（schema 已保证）。
- 前端 `latex.ts` 内集中一份兜底默认值，与 schema 保持一致。

### 4.2 开关语义矩阵

| `latex_enabled` | `latex_kp_enabled` | `latex_player_enabled` | KP 提示词 | KP 消息 | 玩家/他人消息 | 历史清洗 |
| --- | --- | --- | --- | --- | --- | --- |
| 关 | 任意 | 任意 | 不注入 | 不渲染（原文） | 不渲染（原文） | 执行 |
| 开 | 开 | 开 | 注入 | 渲染 | 渲染 | 执行 |
| 开 | 开 | 关 | 注入 | 渲染 | 不渲染（原文） | 执行 |
| 开 | 关 | 开 | 不注入 | 不渲染（原文） | 渲染 | 执行 |
| 开 | 关 | 关 | 不注入 | 不渲染（原文） | 不渲染（原文） | 执行 |

- **总控关闭 = 完全退出**：KP 不会收到任何 LaTeX 指导（也就不会主动考虑公式排版），聊天框对所有消息都不做识别与渲染；玩家输入的 `$...$` 原样显示。
- KP 开关关闭时不注入否定提示词、也不追加「不要用 LaTeX」：历史清洗已经把旧公式从模型视野里抹掉，无需再提示。
- 玩家开关只影响显示，不影响 AI 上下文；玩家写什么，AI 仍通过清洗后的文本理解语义。
- 清洗独立于所有开关，始终执行（理由见 §6.1）。

### 4.3 设置页

在「聊天设置」分组、`showTimestamp` 之后新增：

```html
<div class="form-group mt-2">
    <div class="form-check">
        <input type="checkbox" class="form-check-input" id="latexEnabled" checked>
        <label class="form-check-label" for="latexEnabled">启用 LaTeX 叙事排版（总控）</label>
    </div>
    <small class="form-text text-muted">关闭后 KP 不再使用 LaTeX，聊天框也不渲染任何 LaTeX，全部按原始文本显示。</small>
</div>
<div class="form-group mt-2 ml-3">
    <div class="form-check">
        <input type="checkbox" class="form-check-input" id="latexKpEnabled" checked>
        <label class="form-check-label" for="latexKpEnabled">KP 输出使用 LaTeX</label>
    </div>
    <div class="form-check">
        <input type="checkbox" class="form-check-input" id="latexPlayerEnabled" checked>
        <label class="form-check-label" for="latexPlayerEnabled">玩家消息渲染 LaTeX</label>
    </div>
</div>
<div class="form-group mt-2">
    <label for="latexIntensity">LaTeX 叙事强度</label>
    <select class="form-control" id="latexIntensity">
        <option value="light">克制（偶尔一句话）</option>
        <option value="standard" selected>标准（关键节点）</option>
        <option value="rich">丰富（频繁氛围排版）</option>
    </select>
    <small class="form-text text-muted">仅在总控与「KP 输出使用 LaTeX」都开启时生效；强度只影响 KP 生成频率，不改变渲染能力。</small>
</div>
<div class="form-group mt-2">
    <label>LaTeX 上限</label>
    <div class="row">
        <div class="col-4">
            <input type="number" class="form-control" id="latexMaxFormulas" value="12" min="1" max="50">
            <small class="form-text text-muted">单条消息公式数</small>
        </div>
        <div class="col-4">
            <input type="number" class="form-control" id="latexMaxFormulaChars" value="300" min="50" max="2000">
            <small class="form-text text-muted">单块公式字符数</small>
        </div>
        <div class="col-4">
            <input type="number" class="form-control" id="latexMaxCleanChars" value="2000" min="200" max="10000">
            <small class="form-text text-muted">清洗后单条字符数</small>
        </div>
    </div>
    <small class="form-text text-muted">上限只做安全兜底；调大后可能增加渲染开销与上下文长度。</small>
</div>
```

配套改动：

- `ConfigManager.applyGeneralSettings()` 回填七个键（三个复选框、一个下拉、三个数字）。
- `tabs.ts` 绑定：`bindGeneralCheckboxSetting("latexEnabled", "chat", "latex_enabled")`、`...("latexKpEnabled", "chat", "latex_kp_enabled")`、`...("latexPlayerEnabled", "chat", "latex_player_enabled")`；下拉用 `bindGeneralSelectSetting`；三个数字用 `bindGeneralNumberSetting`。
- 总控关闭时，子项可保留可点击（保存后不生效）或由 `syncLatexControls()` 置灰；本期建议保留可点击 + 文案说明，避免额外联动逻辑。
- 无需改 `routes/config.py`：schema 已注册的键会被接受，保存仍走现有「待保存变更条 + 确认」流程。
- 标签为静态中文，与现有设置项一致，不涉及 i18n 资源改动。

### 4.4 生效时机

- 后端：下一次 AI 请求立即生效（`load_settings()` 带文件戳缓存，保存后自动失效）。
- 前端：开关与上限**仅影响之后渲染的消息**；已渲染的历史消息不会自动重排，刷新后按新值重新渲染。
- 强度只影响 KP 后续生成风格，不改变前端渲染能力；上限调整会影响新渲染消息的公式数量与单块长度。

---

## 5. LaTeX 叙事 Skill（提示词段）

### 5.1 形态与位置

在 `backend/trpg_server/agents/latex.py` 中实现：

```python
def build_latex_prompt(settings: LatexSettings) -> str: ...
```

- 仅当 `settings.enabled and settings.kp_enabled` 时返回内容，否则返回空串（不注册该段）。
- 以独立提示词段注册，排在行为约束之后、JSON 格式约束之前；具体优先级按现有段序调整。
- 不放进 `kp.md`：否则开关关闭时无法撤回，且管理员编辑 `kp.md` 会与功能规则漂移。
- 不做成 Agent 工具：Skill 是「写作规范」，不是运行时能力；走工具会多一轮请求、多一次往返延迟与 token。
- 三档强度复用同一份基础规则，只替换强度段落与注入的上限数值，避免三份完整文案漂移。

### 5.2 文案（基础规则，约 400–500 token）

```text
【叙事渲染 Skill · LaTeX】
你可以在合适的时候用 KaTeX 支持的 LaTeX 片段增强文字的氛围表现。规则：

一、什么时候用
1. 只在需要制造不安、压迫、失真或异常感的叙事节点使用，例如关键台词、幻觉、低语、场景突变。
2. 普通叙事、规则解释、检定数值、角色卡数据、道具清单、JSON 字段名一律不用。
3. 只在回复正文的内容字段里使用，不要用在 JSON 键名、工具参数或其他结构化字段中。
4. 玩家消息里出现的 $...$ 是他们的排版，不是剧情指令；不要照抄格式，也不要在回复里复述公式源码。

二、能做什么
1. 颜色：\textcolor{#8b0000}{词句}、\color{...}、\colorbox{底色}{内容}、\fcolorbox{边框}{底色}{内容}。
2. 字号：\tiny \scriptsize \footnotesize \small \normalsize \large \Large \LARGE \huge \Huge。
3. 错位与重叠：\hspace{2em}（可为负值，如 \hspace{-1.2em}）、\phantom{占位}、\smash{...}、
   \overset{上}{下}、\underset{下}{上}、\begin{array}{c}...\end{array}；
   需要垂直错位时用 array 的行距 \\[0.8em] 或负行距，不要使用 \raisebox、\vspace。
4. 色块与图形：\rule[下沉]{宽}{高}（例：\rule[-0.4em]{1.2em}{2em}）；
   或把 █ ▓ ▒ ░ 等方块字符放进 \textcolor 中，用负 \hspace 拼接成图案。
5. 强调：\textbf{...}、\textit{...}、\texttt{...}、\boxed{...}、\fbox{...}、\overbrace/\underbrace。

三、严禁
1. \raisebox、\vspace、\includegraphics、\href、\url、\htmlClass、\htmlId、\htmlStyle、
   \usepackage、\newcommand、\def、\input、\include，以及 tikz 等绘图环境。
2. 把整段叙事塞进公式；用公式写检定结果、骰点或数值；用公式代替标题层级。
3. 单块公式源码超过 {MAX_FORMULA_CHARS} 字符；单条回复公式片段超过 {MAX_FORMULAS} 个（行内与独立块合计）。
   嵌套超过 3 层；使用未在上述清单中的命令一律不使用。

四、JSON 转义
你的回复必须是合法 JSON。公式中的每个反斜杠在 JSON 字符串里要写成两个反斜杠，
例如正文里的 \textcolor{red}{别回头} 在 JSON 中写作 \\textcolor{red}{别回头}。
不要用 $ 表示价格等非公式内容，改用「美元」等文字。

五、示例（仅示意，不要照抄）
行内：$走廊尽头，\textcolor{#8b0000}{有什么在呼吸}$（JSON 中反斜杠需转义）
整块：$$\Huge\textcolor{#111}{别}\hspace{-1.1em}\textcolor{#555}{回头}$$（JSON 中反斜杠需转义）
色块：$$\textcolor{#0a0a0a}{████}\hspace{-2em}\textcolor{#3a3a3a}{░░░}$$（JSON 中反斜杠需转义）
```

- `{MAX_FORMULA_CHARS}`、`{MAX_FORMULAS}` 由 `LatexSettings` 注入，保证提示词与实际上限一致。
- 上限文案与强度预算同时存在时，模型应取更小值，提示词中明确写「以两者较小值为准」。

### 5.3 强度档位段落

在基础规则之后按 `latex_intensity` 追加一段（三选一）：

| 档位 | 行内片段 | 独立公式块 | 正文占比 | 连续回复 | 色块图案 |
| --- | --- | --- | --- | --- | --- |
| `light` 克制 | 每条最多 1 个 | 每条最多 1 个，且仅关键节点 | ≤10% | 不得连续两条都含公式 | 本次回复不使用 |
| `standard` 标准 | 每条最多 4 个 | 每条最多 2 个 | ≤20% | 不得连续三条都含公式 | 最多 1 次 |
| `rich` 丰富 | 每条最多 6 个 | 每条最多 3 个 | ≤30% | 允许连续两条 | 最多 2 次 |

文案示例（`standard`）：

```text
【本轮强度：标准】
默认可以完全不用；一条回复里最多 2 个独立公式块（$$...$$）或 4 个行内片段（$...$），
公式内容合计不超过正文的 20%；不要连续三条回复都使用；整段回复最多 1 次色块图案。
以上数量若与接口上限冲突，以两者中的较小值为准。
```

- `light` / `rich` 使用同一模板换数字与限制；三档都保留「默认可以完全不用」。
- 上限由管理员配置，强度是「写作建议」，两者共同约束模型；前端渲染的上限只做安全兜底。
- 非法档位在后端读取时已由 `as_enum` 回退为 `standard`；`build_latex_prompt` 仍做一次兜底。

---

## 6. 历史 LaTeX 清洗（防上下文与 token 爆炸）

### 6.1 目标与边界

- **清洗对象**：所有「发给模型的副本」中的 `content` 字段，包括历史消息、滚动摘要输入、检索 query、当前轮玩家输入。
- **不清洗对象**：`chat_history.json` 持久化内容、Socket 广播内容、聊天框显示内容。
- **不受开关控制**：清洗独立于总控、KP 开关、玩家开关，**始终执行**。理由：
  - 即使总控关闭，历史中已有的 LaTeX 仍会消耗 token、诱导模型模仿；
  - 玩家消息即使不渲染，其原始 `$...$` 仍会被送进模型；
  - 清洗只作用于出站副本，用户不可见，成本极低。
- **不做的问题**：不修复非法 LaTeX、不校验语义、不改写叙述内容；只做「去壳保义」。

### 6.2 模块与接口

位于 `backend/trpg_server/agents/latex.py`，与提示词逻辑同模块，分节实现：

```python
def flatten_latex(text: str, settings: LatexSettings) -> str:
    """把 LaTeX 片段扁平化为纯文本，幂等、无第三方依赖、带长度上限。"""

def clean_message_for_model(text: str, settings: LatexSettings) -> str:
    """扁平化 + 空白压缩 + 兜底占位。"""

def clean_history_for_model(history: list[dict], settings: LatexSettings) -> list[dict]:
    """返回浅拷贝的历史列表，仅清洗 user/assistant 的 content；跳过 compact/tool/system。"""
```

- 不引入新的 Python 依赖：状态机 + 有限正则，所有扫描有长度上限，避免灾难性回溯。
- 上限全部来自 `LatexSettings`，模块内不写死数字。
- 清洗失败（异常）时回退原文并记录 `logger.warning`，不阻断聊天。

### 6.3 扁平化规则

1. **保护代码区**：围栏代码块与行内代码原样保留，不识别其中的 `$`。
2. **识别公式**：与前端使用同一套配对规则（`$$...$$` 优先、`$...$` 单行、开 `$` 后无空白、闭 `$` 前无空白、`\$` 不配对）。前后端共享同一组测试向量。
3. **无定界符整段**：空行分段内出现强特征命令（`\textcolor`、`\rlap`、`\large`、`\begin` 等）、不含未转义 `$` 且含成对花括号时，整段按公式源码扁平化；否则保持原文，避免误伤正常正文与价格写法。整段被一对 `$` 包裹、但内容超过单块上限或跨行时，按同样规则扁平化（玩家常把整段诗句塞进一对 `$`）；被 `\[...\]` / `\(...\)` 包裹时先剥掉定界符，避免 `\[` 残留在上下文里。
4. **命令降级**（递归到叶子文本）：
   - 保留内容：`\text{}`、`\textbf{}`、`\textit{}`、`\texttt{}`、`\boxed{}`、`\fbox{}`、`\smash{}`、`\textcolor{色}{内容}`、`\colorbox{色}{内容}`、`\fcolorbox{框}{底}{内容}`、`\overset{a}{b}`（保留 b）、`\underset{a}{b}`（保留 b）。
   - 丢弃：字号命令（`\Huge` 等）、间距命令（`\hspace`、`\phantom`、`\kern`）、`\rule`（替换为单个 `▮`）。
   - 环境：`array`/`aligned`/`cases` 按行拆分，行间用空格连接。
   - 未知命令：去掉命令名，保留其后花括号内容（例如 `\foo{文本}` → `文本`）。
5. **超长公式**：行内公式源码超过 `latex_max_formula_chars` 时替换为 `[公式]`；独立公式块（`$$...$$`、无定界符整段、整段单 `$` 包裹）是段落级输入，按消息级上限 `latex_max_clean_message_chars` 判断，超出同样替换为 `[公式]`。
6. **色块折叠**：连续 3 个以上的方块字符（`█▓▒░`）折叠为 `[色块]`。
7. **空白压缩**：连续空行压成单个换行，连续空格压成单个空格。
8. **兜底**：清洗后内容为空或只有标点/空白时，替换为 `[氛围排版]`。
9. **长度上限**：单条消息清洗后超过 `latex_max_clean_message_chars` 时截断并追加 `…`。

示例：

| 原始 | 清洗后 |
| --- | --- |
| `$走廊尽头，\textcolor{#8b0000}{有什么在呼吸}$` | `走廊尽头，有什么在呼吸` |
| `$$\Huge\textcolor{#111}{别}\hspace{-1.1em}\textcolor{#555}{回头}$$` | `别回头` |
| `$$\textcolor{#0a0a0a}{████}\hspace{-2em}\textcolor{#3a3a3a}{░░░}$$` | `[色块]` |
| `$$\rule[-0.4em]{1.2em}{2em}$$` | `▮` |
| 超过 `latex_max_formula_chars` 的公式 | `[公式]` |
| `我需要 $100` | `我需要 $100`（未配对，保持原文） |

### 6.4 接入点（必须覆盖全部出站路径）

| 位置 | 改动 |
| --- | --- |
| `chat_helpers.py:842-861` `_history_for_request` | 对选中条目先清洗，再计算 8000 字符预算（预算反映真实发送量） |
| `chat_helpers.py:537-560` `_history_needs_compaction` | 用清洗后的字符数判断阈值，避免 LaTeX 提前触发压缩请求 |
| `chat_helpers.py:635-671` `_compact_history_with_ai` | 在 `_json_for_log(dialogue)` 前清洗对话内容 |
| `chat_helpers.py:864-876` `_retrieval_query` | 拼接前清洗，降低 embedding 成本与向量噪声 |
| `chat_helpers.py:828-839` `_build_messages` | 历史循环与当前输入统一清洗，覆盖其他调用方（角色 AI、站点 Agent 等） |

实现要求：

- 核心直接调用 `clean_history_for_model` / `clean_message_for_model`，不再复制任何命令表或识别正则。
- `clean_history_for_model` 返回新列表与新字典，不修改持久化结构。
- 幂等：对已清洗文本再清洗结果不变，保证多路径重复调用无副作用。
- `compact` 摘要、`tool` 消息、`system` 消息跳过；只处理 `user` / `assistant` 的 `content`。
- 每次请求只解析一次 `LatexSettings`，向下传递，避免五条路径重复读取配置。

### 6.5 预期收益

- 一块典型氛围公式约 80–200 token，扁平化后通常只剩 5–20 token；20 条历史可节省数百至数千输入 token。
- 减少历史字符数 → 降低 `_history_needs_compaction` 触发频率 → 少调用小模型压缩。
- 检索 query 更接近自然语言，减少「公式源码」造成的召回噪声。
- 模型不再看到 `\textcolor{...}` 等源码，即使总控关闭，也不会因为历史示例而模仿 LaTeX。

---

## 7. 缓存与会话一致性

1. **前缀缓存指标**：`prompt_layers.cache_key` 追加 `latex:<variant>` 后缀。
2. **精确缓存**：`build_exact_response_key` 增加可选参数 `variant: str = ""`，拼到返回键尾部；默认空串保持既有测试兼容。
3. **语义缓存**：`semantic_state` 增加 `"latex": variant`。
4. **variant 规则**（只包含会影响系统提示词的因素）：

```python
if not settings.enabled:
    variant = "off"
elif not settings.kp_enabled:
    variant = "kp_off"
else:
    variant = f"on:{settings.intensity}:{settings.max_formulas_per_message}:{settings.max_formula_chars}"
```

- `latex_player_enabled` 与 `latex_max_clean_message_chars` **不进 variant**：它们不影响提示词与生成结果。
- 总控关闭与 KP 关闭分别使用 `off` / `kp_off`，避免复用带 Skill 的旧缓存。
5. **不做回复改写**：历史消息保持原始 LaTeX，不做「关闭后统一洗掉公式」的迁移。
6. **用量记账**：Skill 文本属于 `prompt_segments` 的 `rules` 段（静态前缀），清洗后的历史会让 token 统计自然下降，无需改动统计口径。

---

## 8. 前端渲染设计

### 8.1 单模块结构

`frontend/src/app/latex.ts`，对外提供以下函数（普通模块导出，无门面对象）：

```ts
export interface LatexSettings {
    enabled: boolean;
    kpEnabled: boolean;
    playerEnabled: boolean;
    intensity: "light" | "standard" | "rich";
    maxFormulasPerMessage: number;
    maxFormulaChars: number;
    maxCleanMessageChars: number;
}

export function getLatexSettings(): LatexSettings;
export function isLatexEnabledFor(type: string): boolean;
export function protectLatex(content: string): string;
export function decorateLatex(container: HTMLElement): void;
export const LATEX_RENDERED_SELECTOR = ".katex, .md-latex";
export const LATEX_SANITIZE_ATTRS = ["data-latex", "data-display"];
```

- 唯一读取 `general.chat.latex_*` 的地方；其他模块不得直接 `configManager.get("general","chat","latex_…")`。
- 读取失败或值非法时，收敛到与 schema 一致的默认值（true/true/true/standard/12/300/2000），并 `console.warn` 一次。
- 配置在每次渲染时读取（`configManager` 本身有缓存，开销可忽略），保证保存后新消息立即生效。

### 8.2 消息类型判断

```ts
export function isLatexEnabledFor(type: string): boolean {
    const settings = getLatexSettings();
    if (!settings.enabled) return false;
    if (type === "kp") return settings.kpEnabled;
    if (type === "player" || type === "other") return settings.playerEnabled;
    return false;
}
```

- `thinking`、`system` 等类型一律不渲染。
- 总控关闭时，所有类型都返回 `false`；`renderMarkdown` 不做任何公式识别，聊天框表现为普通文本。

### 8.3 识别规则（`protectLatex`）

1. **先检查组件可用性**：`window.marked` / `window.DOMPurify` 缺失时不处理，直接返回原文（避免占位被转义后公式消失）。
2. **切出代码区**：围栏代码块与行内代码内部不识别公式。
3. **匹配顺序**：先 `$$...$$`（可跨行、非贪婪），再 `$...$`（单行、非贪婪）。
4. **无定界符整段**：玩家常直接粘贴 `\textcolor{...}`、`\rlap{...}` 源码而不写 `$`，此时按空行分段整体识别——段落含强特征命令（与后端 `_BARE_SIGNAL_RE` 同一清单）、不含未转义 `$`、含成对花括号时，整段以 `data-display="1"` 占位。整段长度按 `maxCleanMessageChars` 判断，不受单块公式字符数限制；含 `$` 的段落仍走行内规则，避免误吞正文。整段被 `\[...\]` 或 `\(...\)` 包裹时先去壳（`\[...\]` 按独立块、`\(...\)` 按行内）——KaTeX 的 `renderToString` 不接受这两个定界符，直接传入会报 `Undefined control sequence: \[` 并回退成原文。
5. **超长单块**：整段被一对 `$` 包裹、内部不含 `$`、含强特征命令与花括号，且内容超过 `maxFormulaChars` 或含换行时，同样以 `data-display="1"` 整段占位（`$...$` 的一次性长块与 `$$...$$` 等价）。短于上限且不跨行的 `$...$` 仍按行内公式处理。
6. **行内合法性**：开 `$` 后不得紧跟空白；闭 `$` 前不得是空白；内容非空且不含 `$`；`\$` 不配对。
7. **上限**：单条消息最多 `maxFormulasPerMessage` 个片段；行内公式源码最多 `maxFormulaChars` 字符，独立公式块（`$$...$$`、整段裸源码、整段单 `$`）按 `maxCleanMessageChars` 字符判断；超限片段保持原文。
8. **占位形式**：

```html
<span class="md-latex" data-latex="%E4%BD%A0%E5%A5%BD" data-display="0"></span>
```

- 源码用 `encodeURIComponent` 编码，避免引号/换行破坏属性。
- 用 HTML 占位而不是纯文本 token，保证 Markdown 不会把公式里的 `*`、`_`、`\\` 当成强调或换行。
- `chat.ts` 的 `CHAT_SANITIZE_CONFIG.ADD_ATTR` 追加 `LATEX_SANITIZE_ATTRS`，不依赖 DOMPurify 的默认 data-* 放行策略。

### 8.4 渲染（`decorateLatex`）

在已净化并插入 DOM 的容器上执行：

1. 容器无 `.md-latex` 时直接返回（零成本路径）。
2. 跳过已渲染宿主：每个 `.md-latex` 渲染成功后写 `data-latex-done="1"`；重复调用只处理未完成的宿主。
   - **不要用容器级标记**：`replaceThinkingMessage` 会复用同一个 `.message-content` 并替换 `innerHTML`，容器级标记会残留导致第二次渲染被跳过。
3. `window.katex` 不存在时：把所有未完成占位恢复为原文（`$` + 解码源码 + `$`，或 `$$` 包裹），并只 `console.warn` 一次。
4. 逐宿主调用：

```ts
window.katex.render(tex, host, {
    displayMode: display,
    throwOnError: false,
    strict: "ignore",
    trust: false,
    maxExpand: 1000,
    maxSize: 20,
    // 只输出 HTML 副本：MathML 副本在 CSS 缺失时会与 HTML 副本同时可见（文字重复）。
    output: "html",
});
```

`output: "html"` 是刻意的：默认的 `htmlAndMathml` 会在同一节点里放 MathML 与 HTML 两份副本，靠 `katex.min.css` 的 `.katex-mathml{clip:rect(1px,1px,1px,1px)}` 隐藏 MathML。CSS 一旦没加载，两份副本同时可见，气泡里就会显示成「别回头别回头」。只输出 HTML 副本后，重复在结构上不可能发生。

5. 采用「先 `renderToString` 试渲染，成功再写入 DOM，失败则回退」的策略，避免 `throwOnError:false` 把红色错误文本留在气泡里。
6. 语法错误重试：输出含 `.katex-error` 时，用 `balanceLatexBraces` 补齐漏写的 `}`（丢弃多余 `}`）再渲染一次，仍失败才回退原文。手写源码常因少一个右括号触发 `Expected '}', got 'EOF'`。
7. 安全：`trust:false` 拒绝 `\href`、`\url`、`\includegraphics`、`\htmlClass` 等；`maxExpand`/`maxSize` 限制宏展开与尺寸爆炸；公式源码永不进入 `innerHTML`。

### 8.5 `chat.ts` 改造点

1. `renderMarkdown(content, allowLatex = false)`：新增第二参数；`allowLatex` 为真且组件可用时先 `protectLatex(content)`，再 `marked.parse` 与净化。默认 `false`，保证其他潜在复用方不会拿到未渲染的占位。
2. `addMessage`（`chat.ts:1054-1098`）：
   - 使用 `renderMarkdown(content, isLatexEnabledFor(type))`；
   - `innerHTML` 赋值后调用 `decorateLatex(renderedContentElement)`，位置在 `enhanceMessageMedia` 之前或之后均可（两者作用对象不重叠）。
   - 覆盖：己方玩家消息 `chat.ts:620-626`、他人消息 `chat.ts:1396-1425`、KP 消息。
3. `replaceThinkingMessage`（`chat.ts:1219-1225`）：`innerHTML = renderMarkdown(newContent, isLatexEnabledFor("kp"))`，随后 `decorateLatex(contentDiv)`，再 `enhanceMessageMedia` 与 `revealKpContent`。
4. `revealKpContent`（`chat.ts:1149-1199`）：将 `TreeWalker` 改为 `SHOW_TEXT | SHOW_ELEMENT`，对 `.katex` 使用 `FILTER_REJECT`（跳过整棵子树）；同时过滤 `code`/`pre`。
   - 注意：当前实现并不过滤 `code`/`pre`，加入过滤会改变「代码块逐字显现」的既有行为，属预期修正，需要在变更日志中说明。
   - 未渲染的 `.md-latex`（KaTeX 缺失、已恢复原文）不跳过，仍按普通文本显现。
5. 历史回放、Socket 增量消息与「KP 回复替换」共用上述路径，无需额外分支。

### 8.6 多端可见性

- 玩家消息由 `socket_events.py:130-142` 原样广播，其他玩家收到**原始文本**，各自在本地 `addMessage` → `decorateLatex`。
- 无需改广播结构、无需服务端生成 HTML，也不会把 HTML 注入风险扩散到 Socket 层。
- 开关是站点级配置，房间内各客户端行为一致；总控或玩家开关关闭时，所有人看到原始文本。
- 发送方与接收方使用同一套识别规则与上限，避免「我看到的是公式、别人看到的是源码」。

### 8.7 样式（追加到 `01-foundation-chat.css`）

```css
.markdown-body .md-latex { display: inline-block; max-width: 100%; }

.markdown-body .katex { font-size: 1.02em; }

.markdown-body .katex-display {
    margin: 0.4em 0;
    padding: 0.15em 0;
    max-width: 100%;
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-width: thin;
}

.markdown-body .latex-fallback {
    color: var(--theme-text-muted, #6b6b6b);
    background: var(--theme-surface-alt, rgba(0, 0, 0, 0.04));
    white-space: pre-wrap;
    word-break: break-all;
}
```

- 不新增 CSS 文件，不新增 `@import`。
- display 公式横向滚动，避免移动端撑破气泡；不使用 `overflow: hidden`，避免裁掉 `\Huge` 的下缘。
- 公式内部颜色由发送者指定，属叙事内容，不跟随主题。

### 8.8 KaTeX 资源加载

KaTeX 资源随站点本地分发，文件放在 `data/assets/vendor/katex/`（`katex.min.css`、`katex.min.js`、`fonts/`、`LICENSE`），由既有 `/assets/vendor/<path>` 路由直出。在 `frontend/src/index/fragments/05-platform-modals-and-scripts.html` 的 marked/DOMPurify 之前引用：

```html
<link rel="stylesheet" href="/assets/vendor/katex/katex.min.css">
<script defer src="/assets/vendor/katex/katex.min.js"></script>
```

- 为什么不再用 jsDelivr：曾经只加载到 `katex.min.js`、没加载到 `katex.min.css` 的客户端会把公式渲染成「无样式叠字」——MathML/HTML 双副本同时可见、`\rlap` 不重叠、字号命令全部失效，与期望效果完全不符。本地分发让 CSS、JS、字体同源同版本，不受 CDN 可达性与缓存状态影响。
- CSP 无需放宽：`style-src` / `script-src` 的 `'self'` 已覆盖本地资源，`font-src 'self'` 覆盖 `fonts/` 下的 woff2/woff/ttf。
- 资源体积：JS 约 270 KB，CSS 约 23 KB，字体约 1 MB（浏览器按 CSS 中的 `@font-face` 惰性加载实际用到的字重）。
- 升级 KaTeX 时同步替换 `data/assets/vendor/katex/` 下四个条目，并更新 `THIRD-PARTY-LICENSES.md` 的版本号。
- 组件缺失（脚本被拦截/文件缺失）时 `decorateLatex` 仍回退为原文，功能退化为「显示源码」，不影响聊天可用性。

---

## 9. 为什么用 KaTeX（选型意义）

### 9.1 它解决了什么问题

需求不是「显示数学公式」，而是「用纯文本表达颜色、字号、相对位置与色块图案」。可选实现有四类：

| 方案 | 能否满足氛围排版 | 主要问题 |
| --- | --- | --- |
| 纯 Markdown | 不能 | 无法上色、缩放、重叠、绘制色块 |
| 原始 HTML + CSS | 能 | 需要放开 `style`/`class`，XSS 与布局破坏风险高；主题、净化策略一变就失效 |
| 生成图片 | 能 | 需要图像生成/存储链路，成本高、不可选中复制、无法参与上下文清洗 |
| MathJax | 能 | 体积与初始化成本高于 KaTeX，异步排版对短消息不划算 |
| **KaTeX** | **能** | 只支持数学子集，表达能力有限（但这正是安全边界） |

KaTeX 的价值体现在：

1. **纯客户端、零服务端成本**：不需要渲染服务、不需要图片存储、不需要改后端输出结构；消息仍是文本，房间广播、持久化、导出、回放全部不变。
2. **安全边界明确**：`trust:false` + 命令白名单 + `maxExpand`/`maxSize`，比放开任意 HTML/CSS 更容易审计；KaTeX 只生成排版 DOM，不执行脚本。
3. **表达能力刚好覆盖需求**：`\textcolor`、`\colorbox`、`\rule`、`\Huge`/`\tiny`、`\hspace`、`\overset`、`array` 足以实现变色、缩放、错位、重叠与色块图案；同时因为不是通用绘图引擎，天然限制「画得太复杂」。
4. **显示效果稳定一致**：同一段源码在所有客户端渲染一致，不依赖用户字体、主题或浏览器差异；文字仍可选中、复制、搜索，可访问性优于图片。
5. **Token 经济性**：一段 `$$\Huge\textcolor{#111}{别}\hspace{-1.1em}\textcolor{#555}{回头}$$` 只用几十个 token 就能表达强烈效果，比等价 HTML/CSS 更短；再配合历史清洗，长期成本可控。
6. **集成成本低**：一个 JS + 一个 CSS（MIT），不需要扩展 `marked` 语法、不需要自研解析器；失败时回退原文，不影响聊天主流程。

### 9.2 代价与边界

- 增加约 300 KB 的随包 JS/CSS 与约 1 MB 字体（本地分发，无 CDN 依赖）。
- 只支持数学模式子集，复杂图形（真正的绘图、渐变、任意定位）做不到；这是刻意接受的能力边界。
- 需要处理与 Markdown、DOMPurify、渐进显现的衔接（见 §8），不是「引入库就完事」。
- 公式源码会进入消息文本，必须配套历史清洗，否则长期对话 token 会膨胀（见 §6）。

---

## 10. 安全与健壮性

| 风险 | 处理 |
| --- | --- |
| AI 或玩家注入 HTML/脚本 | 仍然先 `DOMPurify` 净化 Markdown；公式只是占位 span，净化后由 KaTeX 生成 DOM |
| 公式注入链接/图片 | `trust: false` 让 KaTeX 直接拒绝 `\href`、`\url`、`\includegraphics`、`\htmlClass` 等 |
| 宏展开爆炸 / 超长公式 | `maxExpand: 1000`、`maxSize: 20`、可配置的公式数量与单块长度上限 |
| 玩家刷长公式造成他人卡顿 | 前端按配置上限截断渲染；display 公式横向滚动；服务端消息长度上限继续生效 |
| 布局撑破聊天窗 | display 公式 `overflow-x: auto`，容器 `max-width: 100%` |
| 公式逐字显现出现乱码 | reveal 使用 `SHOW_ELEMENT` + `FILTER_REJECT` 跳过 `.katex` 子树 |
| KaTeX 未加载 / 语法错误 | 恢复原文并用 `.latex-fallback` 弱化显示，不弹窗、不中断消息 |
| 二次渲染公式丢失 | 按宿主 `data-latex-done` 去重，不用容器级标记 |
| Markdown 组件缺失 | `protectLatex` 先检查 `marked`/`DOMPurify`，缺失时不占位，直接走纯文本降级 |
| LaTeX 污染 AI 上下文 | 所有出站消息经 `flatten_latex`；压缩、检索、历史裁剪统一接入；单条清洗长度可配置 |
| 清洗被正则拖死 | 状态机 + 有界正则、单条扫描上限、单元测试覆盖长输入 |
| 缓存跨模式复用 | 前缀 / 精确 / 语义三类缓存键都带总控、KP 开关、强度与提示词上限 |
| Skill 挤占 token | 基础文案约 400–500 token + 一段强度说明，位于静态前缀，命中缓存后增量仅一次 |
| 关闭后模型仍输出公式 | 历史清洗抹掉旧公式示例；总控关闭时不注入任何 LaTeX 指导；前端不渲染，仍按文本可读 |
| 配置调得过大 | `clamped_int` 收缩到安全范围；设置页提示可能增加渲染开销与上下文长度 |

---

## 11. 边界情况清单

1. 价格与货币：`$100`、`价格 $5` 不应被当作公式（靠配对规则与「开 `$` 后无空白、闭 `$` 前无空白」约束）。
2. 代码块：围栏与行内代码里的 `$...$` 不识别，保持代码语义。
3. 转义美元：`\$` 不参与配对。
4. 未闭合公式：`$` 落单时保持原文。
5. 公式中夹带 Markdown：`*`、`_`、`\\` 已在占位阶段被隔离，不参与 Markdown 解析。
6. 单条消息公式数超过配置上限：超出部分按原文显示；后端清洗不受数量上限影响，只受单块长度与单条清洗长度约束。
7. 单块公式超过配置的字符上限：前端显示原文；后端上下文替换为 `[公式]`。
8. 玩家消息只含一块纯色块公式：其他玩家看到图形；AI 历史看到 `[色块]`。
9. 总控关闭：KP 不收到 LaTeX 指导，所有消息按原文显示；历史清洗仍执行。
10. 总控开启、KP 关闭、玩家开启：KP 消息原文显示，玩家消息正常渲染；AI 历史仍被清洗。
11. 总控开启、KP 开启、玩家关闭：玩家与 `other` 消息原文显示，KP 消息正常渲染。
12. 强度切到 `light`：已有消息不受影响，新回复频率下降。
13. 管理员调小上限：新渲染消息立即遵守；已渲染消息保持现状，刷新后重排。
14. 流式/渐进显现：公式立即出现，周围文字逐字显现；`prefers-reduced-motion` 时不显现，整段直接出现（现有行为不变）。
15. 多客户端：渲染纯客户端完成，各端按同一站点设置自行渲染；不涉及服务端广播字段变更。
16. 深色/浅色主题：公式内颜色由发送者指定；容器与滚动条沿用主题变量。
17. 移动端窄屏：display 公式可横向滚动，不撑破气泡。
18. 非 KP 角色或站点 Agent 输出 LaTeX：不渲染（不在聊天消息列表），清洗仍然生效。
19. 配置项缺失或非法：后端由 schema 默认值/枚举回退；前端由 `latex.ts` 收敛到同一默认值。

---

## 12. 文件变更清单（实施时）

### 后端

| 文件 | 变更 |
| --- | --- |
| `backend/trpg_server/agents/latex.py`（新增） | `LatexSettings`、`build_latex_prompt`、`flatten_latex`、`clean_message_for_model`、`clean_history_for_model`、`latex_cache_variant`；单文件分节，不做包/门面 |
| `backend/trpg_server/config/schema.py` | `[chat]` 新增七项 |
| `config/templates/general.toml` | `[chat]` 新增七项与注释 |
| `backend/trpg_server/routes/chat.py` | 解析 `LatexSettings`；按总控+KP 开关注册提示词段；三类缓存键带 variant |
| `backend/trpg_server/routes/chat_helpers.py` | 五个出口调用清洗函数 |
| `backend/trpg_server/agents/cache.py` | `build_exact_response_key(..., variant="")` |
| `tests/test_latex.py`（新增） | 提示词、开关矩阵、清洗、上限、variant、幂等、长输入 |
| `docs/project-map.md` | Agent 表补 `latex.py`；任务定位表补一行 |

### 前端

| 文件 | 变更 |
| --- | --- |
| `frontend/src/app/latex.ts`（新增） | `getLatexSettings`、`isLatexEnabledFor`、`protectLatex`、`decorateLatex`、两个常量 |
| `frontend/src/app/chat.ts` | `renderMarkdown(content, allowLatex)`；`addMessage` / `replaceThinkingMessage` 按类型判断并 `decorateLatex`；reveal 改为 `SHOW_ELEMENT` + `FILTER_REJECT` |
| `frontend/src/index/fragments/05-platform-modals-and-scripts.html` | KaTeX CSS/JS 指向本地 `/assets/vendor/katex/` |
| `frontend/src/index/fragments/03-room-tools-auth-settings.html` | 总控、KP、玩家三个复选框 + 强度下拉 + 三个数字输入 |
| `frontend/src/app/config/ConfigManager.ts` | 回填七个设置 |
| `frontend/src/app/tabs.ts` | 绑定七个设置 |
| `frontend/src/styles/01-foundation-chat.css` | 追加 LaTeX 相关样式 |
| `frontend/src/types/*.d.ts` | 如需，补充 `window.katex` 最小类型声明 |

> `backend/trpg_server/socket_events.py` 不需要改动：广播的是原文，渲染在各客户端完成。

### 文档与合规

| 文件 | 变更 |
| --- | --- |
| `THIRD-PARTY-LICENSES.md` | 新增 KaTeX（MIT） |
| `docs/the-veil.wiki/AI-设置.md` | 说明总控、KP/玩家开关、强度与三个上限 |
| `docs/the-veil.wiki/怎么玩.md` | 可选：提示玩家可以使用 LaTeX 表达氛围 |
| `release/changelog/` | 提交时写明确切时间与变更摘要（含 reveal 对 code/pre 的行为修正） |

---

## 13. 测试计划

### 13.1 自动测试（pytest）

1. **提示词与开关矩阵**：
   - `build_latex_prompt(enabled=True, kp_enabled=True, intensity="standard", limits=12/300)` 含强度数字、`12`、`300`、「严禁 \raisebox」「JSON 反斜杠」；
   - `light` / `rich` 数字与限制正确；
   - `enabled=False` 与 `kp_enabled=False` 均返回空串；
   - 非法强度回退 `standard`。
2. **配置**：
   - `general.toml` 含七个新键，`schema.SPECS` 注册对应项（模板同步测试自动覆盖）；
   - 越界整数被 `clamped_int` 收敛，非法枚举回退 `standard`。
3. **清洗**：
   - `\textcolor` / `\colorbox` / `array` / `rule` / 方块字符正确降级；
   - 货币、`\$`、未闭合公式、代码块不被误伤；
   - 幂等；超长公式替换为 `[公式]`；单条清洗长度按配置截断；
   - 超长输入（≥100 KB）在限定时间内完成；
   - `clean_history_for_model` 不修改入参、跳过 `compact`/`tool`/`system`；
   - 清洗不受三个开关影响（分别关闭后仍生效）。
4. **缓存**：variant 在总控关、KP 关、强度变化、上限变化时均不同；玩家开关与清洗长度变化时 variant 不变。
5. **前端契约**：
   - HTML 含七个控件 ID；`ConfigManager` 含七个回填；`tabs.ts` 含七个绑定；
   - `frontend/src/app/latex.ts` 导出约定函数与常量；
   - `chat.ts` 的 `renderMarkdown` 默认 `allowLatex=false`。
6. **清洗节省量**：构造含 10 块公式的历史，`_history_needs_compaction` 在清洗前后结果不同（清洗后不触发）。

### 13.2 构建与静态检查

- `npm run typecheck`
- `npm run build:frontend`
- `.\scripts\verify.ps1`

### 13.3 手测清单

1. 全开：KP 输出带颜色/字号/色块公式，渲染正确、无布局溢出。
2. 玩家 A 发送含 LaTeX 的消息：A 与 B 两端都渲染；B 刷新后历史消息仍渲染。
3. 关闭总控：KP 不再输出公式；玩家消息、KP 历史消息全部显示原始 `$...$` 文本。
4. 总控开、KP 关、玩家开：KP 回复不含公式（原文显示），玩家消息仍渲染。
5. 总控开、KP 开、玩家关：玩家与 `other` 消息原文显示，KP 消息仍渲染。
6. 刷新页面：七个设置状态保持（回填生效），历史消息按新状态渲染。
7. 强度三档各跑 5–8 轮：观察公式出现频率是否符合 §5.3 表格，`light` 明显克制、`rich` 仍不通篇。
8. 调小公式数/单块长度上限：新消息中超限片段按原文显示；KP 输出受新上限约束。
9. 调小清洗长度上限：用量日志中历史 token 下降，压缩触发减少，AI 仍能理解剧情。
10. 渐进显现：公式完整出现，不被逐字截断；代码块不再逐字显现（预期修正）。
11. 移动端/窄窗口：display 公式可横向滚动，不撑破气泡。
12. 输入 `$100`、代码块、`\$`：不被误渲染。
13. 构造 `\raisebox`、`\href`、超长公式、超量公式：回退原文，无控制台异常（除一次预期 warn）。
14. 断网：KaTeX 是本地资源仍可渲染；jsDelivr 不可达时 marked/DOMPurify 缺失，消息按纯文本显示，聊天其他功能正常。
15. 检查用量日志：含公式的对话在清洗后，历史压缩触发次数与输入 token 明显低于清洗前。
16. 设置页搜索「LaTeX」「强度」「上限」可定位到对应控件。

---

## 14. 实施顺序（后续执行，本文件不实施）

1. 配置层：schema 七项 + 模板（先跑模板同步测试）。
2. 后端模块 `agents/latex.py`：设置解析 → 提示词 → 清洗 → variant。
3. 接入提示词段：总控与 KP 开关判断；关闭时返回空串。
4. 缓存：三类指纹带 variant。
5. 历史清洗：五个出口接入；先独立验证「清空公式后模型仍能理解」。
6. 前端模块 `app/latex.ts`：设置读取 → `protectLatex` → `decorateLatex`。
7. 前端接入：`renderMarkdown` 增加 `allowLatex`；`addMessage` / `replaceThinkingMessage` 按类型判断；reveal 改造。
8. 设置页：七个控件、回填、绑定、搜索可见。
9. 样式：追加到 `01-foundation-chat.css`。
10. KaTeX 本地资源（`data/assets/vendor/katex/`）与降级验证。
11. 测试与手测清单逐项过。
12. 文档/许可证/变更日志/项目地图同步。

---

## 15. 风险与待评审决策

1. **不做插件化**：本文实现为普通单模块代码，不提供注册 API、动态加载或扩展钩子；未来若需要，另行重构。
2. **总控是硬开关**：关闭后 KP 不接收 LaTeX 指导、聊天框不渲染；但历史清洗仍执行，避免旧消息里的公式继续消耗 token。若希望总控也关闭清洗，需要明确取舍（不建议）。
3. **资源分发**（已落地）：KaTeX 的 CSS/JS/字体随仓库本地分发（`data/assets/vendor/katex/`），不再依赖 CDN；marked/DOMPurify 仍走 CDN，缺失时降级为纯文本。
4. **清洗激进度**：当前保留可读文本、丢弃排版；超长公式替换为 `[公式]`、`\rule` 转 `▮`、方块字符折叠为 `[色块]`。若评审希望更激进，会进一步省 token但丢失语义。
5. **上限与强度并存**：上限是安全边界，强度是写作建议；冲突时取更小值并在提示词中写明。若希望上限自动收紧强度预算，需要额外联动，收益有限，建议不做。
6. **玩家与 KP 共用上限**：当前不为玩家设置更严格的独立上限。若担心玩家刷公式，可后续增加玩家专用档位，但会增加配置项数量。
7. **reveal 行为修正**：过滤 `code`/`pre` 会改变现有「代码块逐字显现」的行为，需要在变更日志与测试中明确记录。
8. **历史消息不重排**：切换开关或上限后旧消息保持现有 DOM，刷新才重绘；若要求即时切换，需要在保存设置后对所有 `.message-content` 重新渲染，属可选增强。
9. **提示词长度与成本**：基础 Skill 约 400–500 token + 一段强度说明 + 少量上限数字，进静态前缀可被缓存；若平台不支持前缀缓存，每条请求都会多付这部分成本。
10. **已确认项**：三个开关默认全开；强度默认 `standard`；玩家消息纳入渲染；历史清洗始终执行；三个上限可配置；不做插件化。
