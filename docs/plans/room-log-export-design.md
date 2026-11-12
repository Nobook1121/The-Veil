---
name: room-log-export-design
description: 团录导出 PDF 设计稿 —— 从 messages.json 生成可搜索的沉浸式跑团记录（3 版式 / 多页分章 / 目录页码 / 前端导出模态）
metadata:
  type: project
  status: implemented
  date: 2026-11-10
  updated: 2026-11-12
---

# 团录导出 PDF · 设计稿

> 目标读者：执行 agent。本文是**规格说明**，不是代码；按 §7 的实施顺序一项一项做，每项跑 §8 验收。
> **落地状态（2026-11-12）**：P0–P4 全部实现（`ea5d298` 归约/Markdown/字体、`f8c4d09` PyMuPDF + archive、`f1c8122` 接口/任务/配置、`726e7f1` 前端入口与模态、`b50a1d1` letter/minimal、`f6ad0cf` 前端对齐与设置页）。实现位于 `backend/trpg_server/room_log/`、`routes/room_log.py`、`frontend/src/app/room-log.ts`、`styles/14-room-log.css`、`tests/test_room_log.py`。已知偏差与遗留见 §9 后的「落地偏差」小节。
> 参考视频里的「一键制作团录 PDF」（回声岛宣传片 01:16–01:20）就是本功能的形态。

---

## 0. 给执行代理的硬约束（先读这一节）

| # | 约束 |
|---|---|
| C1 | **不新增 Python 依赖**。PDF 用 `pymupdf`（已在 `requirements.txt`：`PyMuPDF>=1.24,<2`，环境实测 1.28.2）。不要引入 reportlab / weasyprint / Pillow / markdown / jinja2。 |
| C2 | **不改数据格式**。`messages.json`、`info.json`、`autosave.json` 的结构一个字段都不能动；只读。 |
| C3 | 新增配置键必须**四处同步**：`config/templates/general.toml`、`backend/trpg_server/config/schema.py`（`FieldSpec`）、中英 i18n（若前端可见）、并让 `tests/test_config_template_sync.py` 通过。 |
| C4 | 新蓝图必须注册进 `backend/trpg_server/app_factory.py` 的 `register_blueprints()`；接口权限映射写进 `backend/trpg_server/routes/config.py` 的 `_current_role_can` 映射（默认落到 `settings.general`）。 |
| C5 | **中文字体必须探测后嵌入**，禁止出现方块字。字体探测顺序：`C:\Windows\Fonts\simhei.ttf` → `C:\Windows\Fonts\msyh.ttc` → `C:\Windows\Fonts\simsun.ttc` → `data/assets/fonts/`（若将来随包附带）。找不到时**禁用导出按钮并提示**，不允许静默生成坏 PDF。 |
| C6 | 导出必须**完全离线**：不得引用 CDN（Bootstrap/marked/katex 都不行），字体来自本地文件。 |
| C7 | 所有用户可见文案走 i18n（`frontend/src/locales/zh_cn.json`、`en_us.json`），键名统一 `roomLog.*`。 |
| C8 | 验收：`.\scripts\verify.ps1` 必须全绿；另外按 §12 手工验收一次真实房间。 |

**不要做的事**：不要把导出逻辑写进 `routes/rooms.py`（已经是 2854 行的巨型文件）；不要在 `messages.json` 里加"已导出"标记；不要做 Word/EPUB；不要为了排版引入 HTML 模板引擎。

---

## 1. 现状

### 1.1 消息数据结构（只读）

`data/runtime/rooms/<room_id>/messages.json` 是**数组**，单条：

```json
{
  "id": "dd418517fd1c4f97bceb12bf0ad6ab82",
  "type": "player",                      // player | kp | trigger | dice（实测出现过的四类）
  "sender_id": 3,
  "sender_name": "migu",
  "avatar": "/assets/avatars/default.jpg",
  "content": "@KP 开始",                  // markdown 子集文本
  "time": "19:52",
  "created_at": "2026-08-19 19:52:52",
  "metadata": {
    "processingTime": 15, "tokenCount": 33754,
    "roleId": "kp", "aiRequestId": "ai-...", "senderName": "KP"
  }
}
```

要点：
- 时间有两套：`time`（`HH:MM`，展示用）与 `created_at`（`YYYY-MM-DD HH:MM:SS`，排序/分组用）。
- `type == "dice"` 由 `backend/trpg_server/agents/tools/dice.py:267` 产出，`metadata` 里带骰子明细。
- `type == "trigger"` 可能带 `metadata.asset_url`（图片/附件）。
- `content` 是 markdown 子集：`#`~`###` 标题、`**粗体**`、`*斜体*`、`---` 分隔线、`>` 引用、`-`/`1.` 列表、`` `code` ``。KP 文本里大量中文引号 `“”『』` 作为台词。
- `info.json` 顶层字段（实测）：`id`、`name`、`room_code`、`scenario_id`、`scenario_title`、`creator_id`、`creator_name`、`created_at`、`updated_at`、`members[]`（成员含 `character_card`、`character_state`，可用来做角色索引页）。
- 房间目录：`messages.json`、`info.json`、`autosave.json`、`autosave-<ts>.json`。路径助手在 `backend/trpg_server/room_ops/storage.py:130`（`messages.json` 的 path 函数）。

### 1.2 可复用的既有模式

- **JSON 存储**：`from trpg_server.json_store import read_json, write_json_atomic`。
- **异步任务**：项目已有 `ImportJobStore`、`NarrativeJobStore`（在 `app_factory.py` 里初始化），导出任务照抄它们的 store 形态（内存字典 + 锁 + 状态机 `queued/running/done/failed`）。
- **导出接口先例**：`backend/trpg_server/routes/characters.py:1184` 附近的角色卡导出（JSON 文件流）。PDF 用 `flask.send_file(..., mimetype="application/pdf", as_attachment=True, download_name=...)`。
- **前端模态先例**：`frontend/src/index/fragments/05-platform-modals-and-scripts.html` 里的 `#roomQrModal`；脚本用 `frontend/src/app/*.ts` → tsc 输出 `js/*.js` → 在 05 片段里 `<script src="js/...">` 引入。
- **样式**：新 CSS 写到 `frontend/src/styles/14-room-log.css`，并在 `frontend/src/react/app.css` 的 `@import` 列表末尾加一行（该文件由 esbuild 打包成 `dist/public/js/react/main.css`，`01-head-and-sidebar.html` 已经引用）。

### 1.3 缺口

现在没有任何"团录"视图或导出：玩家只能翻聊天记录。也没有字体嵌入、PDF 生成、分章、目录这些基础设施。

---

## 2. 目标与非目标

**目标**
- G1 房主/管理员在房间详情里点「导出团录」，选版式与范围，30 秒内拿到一份**中文、可搜索、带目录页码**的 A4 PDF。
- G2 三种版式（§4）：`archive` 档案卷宗（默认）/ `letter` 手记信纸 / `minimal` 极简排版。
- G3 范围可选：全篇 / 某日期区间 / 仅 KP 叙述 / 是否含 OOC 与骰子 / 是否含图片。
- G4 单次导出 2000 条消息、约 60 页，生成时间 ≤ 15 s；文件大小 ≤ 25 MB（字体子集化后）。
- G5 失败可读：缺字体、缺图片、房间为空都给出明确提示，不产生半截文件。

**非目标（本次不做）**
- 不做倒序/无限滚动阅读器；不做导出 Word/Markdown/EPUB/HTML 包。
- 不做多人在线协作排版；不做自定义模板上传（P4 之后再说）。
- 不把 PDF 回写进房间或自动发到群里。

---

## 3. 数据：RoomLogDocument

后端先把消息归约成与渲染无关的中间文档（便于测试、便于以后换渲染器）：

```json
{
  "schema_version": 1,
  "room": {"id":"...","name":"...","code":"...","scenario_title":"...",
           "system":"COC7","created_at":"2026-08-19 19:51:23",
           "creator_name":"migu","player_count":3},
  "options": {"template":"archive","include_dice":true,"include_ooc":true,
              "include_images":true,"date_from":null,"date_to":null},
  "stats": {"messages_total":312,"messages_exported":288,"chapters":6,
            "dice_rolls":41,"images":2,"duration_days":12},
  "chapters": [
    {
      "index": 1,
      "title": "2018-08-19",
      "subtitle": "第一夜 · 西安",
      "started_at": "2026-08-19 19:52:52",
      "entries": [
        {"kind":"narration","speaker":null,"time":"19:53","html":"&lt;p&gt;...&lt;/p&gt;"},
        {"kind":"dialogue","speaker":"KP","time":"19:53","html":"..."},
        {"kind":"speaker","speaker":"migu","character":"何清越","time":"19:54","html":"..."},
        {"kind":"dice","speaker":"骰娘","time":"19:55","html":"...","roll":{"expr":"1d100","total":37}},
        {"kind":"image","speaker":"KP","url":"/assets/...","caption":""},
        {"kind":"system","speaker":null,"time":"20:01","html":"..."}
      ]
    }
  ],
  "cast": [{"name":"migu","character":"何清越","avatar":"/assets/...","role":"player"}]
}
```

**归约规则（必须严格按此实现）**

1. 按 `created_at` 升序排序；缺失则回退到原始顺序（保持稳定）。
2. **章节切分**（优先级从高到低）：
   a. `metadata.scene_id` 或 `metadata.chapter` 存在时按其变化切章（当前数据里大多没有，留扩展位）；
   b. 否则按 `created_at` 的**日期**切章，标题 `YYYY-MM-DD`；
   c. 若单日消息 > 250 条，再按每 250 条切分为 `2018-08-19（一）`、`（二）`。
3. **条目类型映射**：

| message.type | kind | 说明 |
|---|---|---|
| `kp` | `narration` 或 `dialogue` | 若整段以 `“`/`『`/`「` 开头且段落 ≤ 2 行 → `dialogue`；否则 `narration`。混合段落一律 `narration`（不拆分，避免破坏语义）。 |
| `player` | `speaker` | `speaker=sender_name`，`character` 取该成员 `character_card.name`；找不到则空。 |
| `dice` | `dice` | 渲染骰子块（§4.6）。 |
| `trigger` | `image`（有 `metadata.asset_url` 且扩展名属图片）或 `system` | 其他附件降级为 `system` 文本 + 链接。 |
| `system` / 未知 | `system` | 居中灰字。 |

4. **OOC 过滤**：`content` 以 `((`/`（（` 包裹、或 `metadata.ooc === true` 时视为场外；`include_ooc=false` 时丢弃。
5. **图片解析**：`url` 以 `/assets/` 或 `/data/` 开头时映射到项目磁盘路径（`data/assets/...`），渲染时作为 `<img>`；文件不存在则降级为 `[图片缺失]` 文本条目并在 `stats` 里计数。
6. **HTML 转义**：`content` 先 markdown 子集转换（§附录 C），最后再做一次白名单过滤（只允许 `p/strong/em/h1-h3/ul/ol/li/blockquote/hr/code/br/a`）。
7. **统计**：`duration_days` = 首末 `created_at` 的天数差 + 1。

---

## 4. 版式规范（三套）

> **重要：渲染器是 PyMuPDF 的 `Story`（HTML 子集 + CSS 子集）。** 它在 Q16 实测：块级元素、`h1~h3`、`p`、`strong/em`、`ul/ol/li`、`blockquote`、`hr`、`table`、`img`、`@font-face`、`color/background-color/margin/padding/font-size/line-height/border` 都能用；**不要依赖 flex、grid、absolute/fixed 定位、float、box-shadow、border-radius、transform、clip-path、filter、SVG、多栏 column-count**（多数不支持或未验证，踩了就白做）。所有"高级视觉"用**预渲染的背景图 + 普通块级流**实现。

通用页面参数：A4（`pymupdf.paper_rect("a4")`），页边距 18/16 mm，正文 10.5 pt / 行高 1.75，页脚右下页码「第 n 页 / 共 m 页」。

### 4.1 `archive` 档案卷宗（默认）
- 封面：深墨绿底（`#0d1f1a`），烫银细线框（用 table 边框模拟），标题《房间名》，副标题剧本名 + 日期区间 + `本记录共 N 章 / M 条`，底部「THE VEIL · 团录」小字。
- 每章首页：章节头（大号等宽日期 + 中文副题 + 一条 1px 分隔线）。
- 叙述：左对齐，首行不缩进，段间 6pt。KP 旁白用 `#1c2b26` 字色，左侧 3px 竖线（用 `border-left`）。
- 台词：整段缩进 10pt，左侧 2px 竖线，斜体关闭（中文斜体难看，用浅色区分：`#3a4a44`）。
- 玩家发言：`strong` 角色名 + 时间右对齐（用两列 `table`，宽度 100%）。
- 骰子块：浅灰底（`#f2f0ea`）小表格，两列：左 `表达式 / 结果`，右 `成功/失败` 判定词。

### 4.2 `letter` 手记信纸
- 封面 = 第一页即正文，抬头：房间名（18pt 衬线）+ 剧本名；右上角日期。
- 正文行距 2.0，段首缩进 2 字符（用 `text-indent: 2em`？Story 支持有限——**改用两个全角空格 `　　` 前缀**，最稳）。
- 每章之间插一条装饰性 `***` 分隔（居中）。
- 玩家发言像"书信摘录"：整段左缩进 12pt + 5pt 灰竖线。
- 无目录页（可选开关 `toc:false`）。

### 4.3 `minimal` 极简
- 白底黑字，无装饰；标题 16pt、正文 10pt；章节标题用小号大写英文 + 日期。
- 每页顶部细线 + 「房间名 · 团录」小字。
- 骰子与系统消息压成行内灰字，不占独立块。

### 4.4 共用的结构（每版式目录下都要有）
```
backend/trpg_server/room_log/templates/<id>/
  template.json     # 元数据：id/名称/描述/默认选项/可用变量清单
  cover.html        # 封面（minimal/letter 可为空文件）
  chapter.html      # 章节头
  body.html         # 正文流（由 render.py 逐条拼接后整体交给 Story）
  print.css         # 该版式样式（Story 的 user_css）
  assets/           # 背景图、花边（PNG，随包发布）
```

`template.json` 示例：
```json
{"id":"archive","name":"档案卷宗","description":"深色封面 + 卷宗内页，适合长篇",
 "toc":true,"cover":true,
 "page":{"size":"a4","margin":[18,16,18,16]},
 "fonts":{"body":"Body","heading":"Heading"},
 "options":{"theme_color":"#0d1f1a","accent":"#b9a06a"}}
```

### 4.5 目录页
- 位置：封面之后；每章一条：`01  ……  2018-08-19 第一夜 · 西安  ………  5`（页码右对齐，用 `table` 两列 + 点线用 `·` 填充或 table 顶端虚线下划线）。
- 目录里的页码来自**二次渲染**：见 §5.4。

### 4.6 骰子块（三版式统一的可选皮肤）
- 一行浅底表格：`骰娘 · 19:55` 左，`1d100 = 37` 右；下一行判定短语（`成功 / 困难成功 / 失败 / 大失败`），由 `metadata` 里已有判定字段决定，没有就只显示数值。

---

## 5. 后端实现

### 5.1 文件清单

```
backend/trpg_server/room_log/__init__.py        # 对外导出 build_document / render_pdf / export_jobs
backend/trpg_server/room_log/model.py           # §3 的归约逻辑（纯函数，最需要单测）
backend/trpg_server/room_log/markdown.py        # §附录 C 的子集转换 + 白名单
backend/trpg_server/room_log/fonts.py           # 字体探测 + 缓存 + Story 用的 Archive
backend/trpg_server/room_log/render.py          # PyMuPDF 渲染：封面/目录/正文/页码/合并
backend/trpg_server/room_log/jobs.py            # 导出任务 store（线程 + 状态机 + TTL 清理）
backend/trpg_server/room_log/templates/...      # §4.4
backend/trpg_server/routes/room_log.py          # 蓝图 room_log_bp
```

### 5.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rooms/<room_id>/log/preview` | 返回 §3 的 `RoomLogDocument`（不含字体/渲染），用于前端预览与统计。 |
| POST | `/api/rooms/<room_id>/log/exports` | body：`{"template":"archive","include_dice":true,...}`；创建后台任务，返回 `{"export_id":"...","status":"queued"}`。 |
| GET | `/api/rooms/<room_id>/log/exports/<export_id>` | 返回 `{"status":"queued|running|done|failed","progress":0-100,"error":null,"file":{"bytes":123,"pages":12}}`。 |
| GET | `/api/rooms/<room_id>/log/exports/<export_id>/file` | 流式返回 PDF（`as_attachment=True`，名 `团录-<房间名>-<YYYYMMDD>.pdf`）。 |
| DELETE | `/api/rooms/<room_id>/log/exports/<export_id>` | 删除产物与记录。**非成员 403；未知 id 404；运行中 409 `log.not_ready`**（幂等语义仅指「已删除后再删」不再报错，不适用于未知 id）。 |

权限与错误：
- 房主 / 房管 / 管理员可导出；普通成员可导出（可配置为仅房主，见 `[room_log] members_can_export`）。
- 401/403 用项目既有错误响应格式（参考 `routes/characters.py` 的 `jsonify({"error": ...})` 结构）。
- 房间不存在 → 404；消息为空 → 400 `log.empty`；缺字体 → 503 `log.font_missing`；渲染异常 → 500 `log.render_failed`（附带 `trace_id`，错误细节写 `data/runtime/logs/`）。

### 5.3 字体（`fonts.py`）

```python
CANDIDATES = [
    ("SimHei",  r"C:\Windows\Fonts\simhei.ttf"),
    ("MSYaHei", r"C:\Windows\Fonts\msyh.ttc"),
    ("SimSun",  r"C:\Windows\Fonts\simsun.ttc"),
]
def resolve_font() -> tuple[str, str]:   # (family_name, abs_path)；全部不存在时抛 FontMissing
```
- 用 `pymupdf.Archive(r"C:\Windows\Fonts")` 作为 Story archive，CSS 里 `@font-face { font-family: VeilBody; src: url(SimHei.ttf); }`（**实测可行**，见附录 A）。
- 若命中 `.ttc`（字体集合），先单独跑一次 `pymupdf.Font(fontfile=path)` 验证；失败则跳过该候选。
- 渲染完成后 `doc.subset_fonts()` 缩小体积（9.9 MB → 通常 < 1 MB）。

### 5.4 渲染流程（`render.py`）

1. `doc = build_document(room_dir, options)`（§3）。
2. 逐章渲染成独立 PDF：每章一个 `pymupdf.Story(html_chapter, user_css, archive)` + `DocumentWriter(tmp/<i>.pdf)`，循环 `story.place(where)` 直到 `more==0`；记录每章页数。
3. `pymupdf.Document.insert_pdf` 依次合并（含封面页）。合并顺序：封面（1 页）→ 目录（1–2 页）→ 章节 1..N。
4. 目录页需要页码：先合并正文得到每章起始页（`1 + toc_pages + sum(prev_pages) + 1`），再渲染目录页并 `doc.insert_pdf(toc, start_at=1)`（目录页单独生成，占位页数由目录条目数换算：每页 28 条）。
5. 页码与页眉页脚：遍历 `for page in doc:`，用 `page.insert_font(fontname="pgnum", fontfile=font_path)` + `page.insert_text((x, y), text, fontname="pgnum", fontsize=8)` 写入；不要试图在 Story 里做 running header。
6. 元数据：`doc.set_metadata({"title": f"{room_name} · 团录", "author": ..., "subject": ...})`。
7. 落盘到 `data/runtime/logs/exports/<room_id>/<export_id>.pdf`（该目录已存在，见 `data/runtime/logs/`），返回路径与统计。

> 为什么逐章渲染：`Story` 不暴露"当前章节落在第几页"，逐章独立渲染是唯一稳定拿到页码的办法，也让单章超长时不会拖垮整篇。

### 5.5 配置键（`[room_log]`，四处同步）

| key | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 功能总开关 |
| `members_can_export` | `true` | 普通成员是否可导出 |
| `default_template` | `"archive"` | 默认版式 |
| `include_ooc` | `true` | 默认是否含场外 |
| `include_dice` | `true` | 默认是否含骰子块 |
| `include_images` | `true` | 默认是否含图片 |
| `max_messages_per_export` | `20000` | 上限，超过截断并在 PDF 首页标注 |
| `keep_exports_days` | `7` | 产物保留天数（后台清理线程顺带处理） |
| `font_file` | `""` | 手动指定字体绝对路径（覆盖探测） |

### 5.6 测试（`tests/test_room_log.py`）
- `build_document`：空房间、单条、跨日切章、>250 条二次切章、OOC 过滤、图片缺失降级、图片存在、dice 映射、未知 type → system。
- markdown 转换：`#`/`**`/`---`/`>`/列表、HTML 注入 `<script>` 被转义。
- 字体：`resolve_font()` 在无字体环境抛 `FontMissing`（用 monkeypatch `os.path.exists`）。
- 渲染：用 3 条假消息生成 PDF，断言 `doc.page_count >= 2`、`page.get_text()` 含中文、`doc.metadata["title"]` 正确、文件大小 < 25MB。
- 接口：无权限 403、空房间 400、`GET file` 的 `Content-Type == application/pdf`。

---

## 6. 前端实现

### 6.1 文件
```
frontend/src/app/room-log.ts            # 编译到 js/room-log.js
frontend/src/index/fragments/04-editor-modals.html   # 追加导出模态（也可放 05 片段，与 #roomQrModal 同处）
frontend/src/index/fragments/05-platform-modals-and-scripts.html  # 追加 <script src="js/room-log.js">
frontend/src/styles/14-room-log.css     # 模态与预览样式
frontend/src/react/app.css              # @import 新样式
frontend/src/locales/zh_cn.json / en_us.json  # roomLog.* 键
```

### 6.2 DOM 规格

**入口按钮**（放在 `frontend/src/index/fragments/02-main-tabs.html` 房间详情卡片的分享区旁，`#roomInvitePlayersBtn` 之后）：
```html
<button type="button" class="btn btn-outline-secondary btn-sm" id="roomLogExportBtn">
  <i class="fa fa-book"></i> <span data-i18n="roomLog.action.open">导出团录</span>
</button>
```
权限：非房间成员隐藏；`[data-room-host-only]` 由现有房间详情渲染逻辑控制（若已有类似 JS 钩子就复用；没有就在 `room-log.ts` 里按 `info.members` 判断当前用户角色后 `hidden`）。

**导出模态**（Bootstrap 5 结构，照抄 `#roomQrModal`）：
```html
<div class="modal fade" id="roomLogExportModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" data-i18n="roomLog.modal.title">导出团录</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="room-log-layout">
          <aside class="room-log-options"><!-- 表单 --></aside>
          <section class="room-log-preview"><!-- iframe/摘要 --></section>
        </div>
      </div>
      <div class="modal-footer">
        <span class="room-log-status" id="roomLogStatus"></span>
        <button id="roomLogCancel" class="btn btn-outline-secondary" data-i18n="common.cancel">取消</button>
        <button id="roomLogExport" class="btn btn-primary" data-i18n="roomLog.action.export">生成 PDF</button>
        <a id="roomLogDownload" class="btn btn-success" hidden data-i18n="roomLog.action.download">下载</a>
      </div>
    </div>
  </div>
</div>
```

表单控件（id 全部固定）：
| id | 控件 | 说明 |
|---|---|---|
| `roomLogTemplate` | select | `archive` / `letter` / `minimal`（label 走后端 template.json 的 name） |
| `roomLogDateFrom` / `roomLogDateTo` | date | 空 = 全部 |
| `roomLogIncludeKpOnly` | checkbox | 仅 KP 叙述（默认关） |
| `roomLogIncludeDice` | checkbox | 默认开 |
| `roomLogIncludeOoc` | checkbox | 默认开 |
| `roomLogIncludeImages` | checkbox | 默认开 |
| `roomLogStats` | div | 「312 条消息 · 6 章 · 41 次骰子 · 约 12 页」 |
| `roomLogPreviewFrame` | iframe | 预览首章 HTML（由后端 preview 接口返回的 `html` 拼装；`sandbox=""`） |

### 6.3 交互
1. 点 `#roomLogExportBtn` → `fetch('/api/rooms/<id>/log/preview')` → 填 `roomLogStats`、渲染第一页预览 → `bootstrap.Modal.getOrCreateInstance(el).show()`。
2. 改任一选项 → 300ms 防抖后重新 preview（只请求统计，不重复拉大 HTML）。
3. 点 `#roomLogExport` → POST 创建任务 → 每 800ms 轮询状态（上限 60 次，超时提示"任务仍在后台，稍后可在下载列表查看"）。
4. `done` → 显示 `#roomLogDownload`（href 指向 file 接口），状态文字「已生成 · 12 页 · 3.1 MB」。
5. `failed` → `#roomLogStatus` 显示本地化错误（`log.font_missing` / `log.empty` / 通用）。
6. 关闭模态：不取消后台任务（任务继续，产物按 `keep_exports_days` 清理）。

### 6.4 样式（`14-room-log.css`）
- `.room-log-layout { display:grid; grid-template-columns: 300px minmax(0,1fr); gap: 20px; }`（**前端可以用 grid**，只有 PDF 渲染器不行）。
- 选项面板用小标题 + 表单组；预览 iframe 白底、阴影、圆角 `--theme-radius-md`、高度 62vh。
- 状态文字：成功绿、失败红、进行中灰 + 转圈（纯 CSS）。
- 响应式：≤900px 单列，选项在上。

### 6.5 i18n 键（zh / en）
`roomLog.action.open`、`roomLog.modal.title`、`roomLog.option.template`、`roomLog.option.range`、`roomLog.option.include_kp_only`、`roomLog.option.include_dice`、`roomLog.option.include_ooc`、`roomLog.option.include_images`、`roomLog.stats.summary`（`{messages} 条消息 · {chapters} 章 · {dice} 次骰子 · 约 {pages} 页`）、`roomLog.action.export`、`roomLog.action.download`、`roomLog.status.queued|running|done|failed`、`roomLog.error.empty|font_missing|render_failed|timeout`、`roomLog.preview.empty`。

---

## 7. 实施计划

| 包 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| **P0** | 基础设施：`model.py` + `markdown.py` + `fonts.py` + 单元测试 | 纯函数通过 §5.6 的单测 | `pytest tests/test_room_log.py` 全绿 |
| **P1** | 渲染器：`render.py` + `archive` 模板 + 一个 CLI 入口 `python -m trpg_server.room_log.render <room_id> out.pdf` | 一份真实房间的 PDF | 打开 PDF：中文无方块、有目录页码、文本可搜索复制 |
| **P2** | 接口 + 任务：`routes/room_log.py` + `jobs.py` + 注册 + 配置键 | curl 拿到 PDF | 403/400/503 路径都对；两次并发导出互不干扰 |
| **P3** | 前端：模态 + 轮询 + 下载 + i18n + CSS | 完整交互 | §12 手工验收 |
| **P4** | `letter` / `minimal` 两套模板 + 打磨（字距、孤行、图片说明） | 三版式可选 | 三份 PDF 各看一遍 |

每包一个 commit；P0/P1 不碰前端，P2 不碰渲染逻辑核心。

---

## 8. 手工验收清单（P3 完成后逐条过）

1. 普通成员打开房间 → 看到「导出团录」；非成员看不到。
2. 默认参数导出 → 弹窗出现，10 秒内 `done`，点下载得到 PDF。
3. PDF：封面 → 目录（页码正确）→ 章节 → 页脚「第 n 页 / 共 m 页」。
4. 用 PDF 阅读器搜 "KP" 或某句台词，能命中（说明是真实文本不是图片）。
5. 取消勾选「骰子」再导出 → 骰子块消失，统计数字同步变化。
6. 日期范围只选一天 → 只含该日章节。
7. 换三个版式各导一次 → 视觉明显不同，且都无方块字。
8. 空房间（新建未发言）→ 提示"没有可导出的消息"，不生成文件。
9. 中英文界面切换 → 模态文案全部切换。
10. 导出 2000 条消息的压力房间 → < 15 s，PDF < 25 MB。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| `Story` 的 CSS 子集不认某个属性 | 先写在 P1 的样例里验证；不支持就换成 table/块级/内联样式。**绝不上 flex/grid/absolute**。 |
| `.ttc` 字体集合嵌入失败 | 优先 `simhei.ttf`；命中 ttc 前先 `pymupdf.Font(fontfile=...)` 试开；都失败则禁用按钮（C5）。 |
| 超长房间内存峰值 | 逐章渲染 + 每章 `with` 关闭文档；`max_messages_per_export` 保护。 |
| 图片路径穿越 | 只允许 `data/assets/`、`data/runtime/rooms/<id>/` 下的文件，用 `trpg_server.security.safe_join`；其余丢弃。 |
| 并发导出打满 CPU | `jobs.py` 用全局 `threading.Semaphore(2)`，其余排队。 |
| 用户等太久 | 前端 60 次轮询上限 + "后台继续"兜底文案。 |

---

## 9.1 落地偏差与遗留（2026-11-12 复核）

| 项 | 实际 | 说明 |
|---|---|---|
| 模块数 | `room_log/` 下 6 个实现文件（`model` / `markdown` / `fonts` / `render` / `jobs` / `__init__`）+ 12 个模板 | 设计 §5.1 写的是文件清单，按功能而非文件数计 |
| `[room_log]` 配置键 | 实测 10 键（多出 `max_concurrent_exports`） | 设计 §5.5 写 9 键 |
| 模板资源 | `letter` / `minimal` 的 `cover.html` 为 0 字节占位；§4.4 计划的 `body.html` / `assets` 未建 | `render.py:59` 只校验 `print.css` + `chapter.html`；封面改用代码内联样式，不影响产出 |
| 接口返回 | `preview` 返回 `document` 结构，由前端拼预览 HTML；不直接返回 HTML 字符串 | 设计 §6.2 的表述需按实现理解 |
| 权限映射 | 蓝图权限落 `routes/config.py:95` 的 `_config_permission_node`（设计写的是 `_current_role_can`） | 名称漂移 |
| 任务持久化 | 导出任务落盘 `job.json`，重启后恢复；运行中重启标记 failed | 由 2026-11-12 整改补齐（原仅内存态） |
| 清理 | `keep_exports_days` 在启动与列表/创建时清理过期目录 | 无独立后台线程，避免额外线程生命周期 |

仍未做（低风险）：失败请求的 `trace_id` 只加字段、不改变现有错误形状；`C6` 渲染硬超时依赖同步 Flask + PyMuPDF，不做进程隔离；2000 条以上消息的压力用例未纳入常规测试。

---

## 10. 明确不做

- 不做浏览器端 PDF 生成（jsPDF/html2canvas 一律不引入）。
- 不做"导出后自动发到群里/发邮件"。
- 不做 PDF 加密/水印（如果以后要，加在 `render.py` 的后处理步骤，不侵入 P1）。
- 不追求把聊天里的 KaTeX 公式渲染进 PDF（降级为原文文本，验收不因此失败）。

---

## 附录 A：实测可跑的渲染骨架（已在本机验证）

```python
import pymupdf

html = "<html><body><h1>团录测试</h1><p>2018年6月11日，下午三点。</p></body></html>"
css  = "@font-face{font-family:VeilBody;src:url(SimHei.ttf);} body{font-family:VeilBody;font-size:12pt;}"
arc  = pymupdf.Archive(r"C:\Windows\Fonts")

story  = pymupdf.Story(html=html, user_css=css, archive=arc)
writer = pymupdf.DocumentWriter("out.pdf")
page   = pymupdf.paper_rect("a4")
where  = page + (50, 50, -50, -50)
more   = 1
while more:
    dev = writer.begin_page(page)
    more, _ = story.place(where)
    story.draw(dev)
    writer.end_page()
writer.close()

doc = pymupdf.open("out.pdf")
doc.subset_fonts()
doc.save("out_final.pdf", garbage=3, deflate=True)
pix = pymupdf.open("out.pdf")[0].get_pixmap(dpi=110)   # PNG 导出备用（前端预览图）
pix.save("preview.png")
```
（本机实测：中文正常渲染、`get_text()` 可取回文本、`get_pixmap` 可出 PNG，无 Pillow 依赖。）

## 附录 B：字体探测

```python
def resolve_font(override: str = "") -> tuple[str, str]:
    if override:
        return ("VeilBody", override)
    for path in (r"C:\Windows\Fonts\simhei.ttf",
                 r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simsun.ttc"):
        try:
            pymupdf.Font(fontfile=path)   # ttc 也先试开
            return ("VeilBody", path)
        except Exception:
            continue
    raise FontMissing("no CJK font found; set [room_log] font_file")
```
Story 的 `@font-face` 用 `url(<basename>)`，archive 必须指向字体所在**目录**。

## 附录 C：markdown 子集 → HTML 规则

| 输入 | 输出 |
|---|---|
| `# 标题` / `##` / `###` | `<h1>/<h2>/<h3>` |
| `**粗**` | `<strong>` |
| `*斜*` | `<em>`（中文里少用，仍支持） |
| `---` 独占一行 | `<hr>` |
| `> 引用` | `<blockquote>` |
| `- 项` / `1. 项` | `<ul><li>` / `<ol><li>` |
| `` `code` `` | `<code>` |
| 空行 | 段落边界 |
| `<`、`>`、`&` | 先转义再做上面替换（防止 HTML 注入） |
| 其他 | 原样文本 |

不实现：表格语法、嵌套列表、链接自动识别、HTML 直通（一律转义）。
