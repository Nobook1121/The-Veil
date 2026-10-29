# 剧本导入与低 token 运行方式

`scripts/convert_scenario.py` 支持 `.txt`、`.md` 和 `.docx`：

```powershell
python scripts/convert_scenario.py docs/样本/模组.docx -o scenario.json --title "我的剧本"
```

输出 JSON 可在剧本编辑器中导入，或提交到 `POST /api/scenarios`。网页端也可调用
`POST /api/scenarios/import`（JSON `{ "text": "...", "title": "..." }` 或 multipart
字段 `file`）先预览转换结果，再决定是否保存。

转换器把原文拆成有序模块，并为每个模块生成短摘要；`content` 永远保留原文，摘要
不作为事实来源。场景/结局会获得稳定 ID，便于 KP 确定性转场。

运行时房间快照仅发送剧本身份、当前场景 ID 和场景清单摘要。KP 需要叙事细节时才调用
`room.get_scenario_module`；明确转场时调用 `room.activate_scenario_scene`，且 ID 必须来自
清单。房间创建时自动锁定第一个场景，后续状态写入 `info.json`。因此不会因摘要遗漏而
凭空产生“驾驶室”等剧本外设施。

运行时通过精简房间快照、只注入最近历史、摘要与正文分离来降低输入 token；不对 KP
单轮输出设置人为上限。模块摘要接口使用低温度并按需调用，避免每轮重复发送长文本。
# 异步剧本导入

剧本文档可通过 `POST /api/scripts/import` 上传 `.docx`、`.pdf`、`.txt` 或 `.md`。任务状态保存在
`data/runtime/scenario_imports/<job_id>/job.json`，原始文件和中间结果位于同一任务目录；使用
`GET /api/scripts/import/<job_id>` 查询，或连接 `/stream` 获取 SSE 进度。失败任务可调用
`POST .../retry`，运行中任务可调用 `POST .../cancel`。

完成后，审核内容通过 `PUT .../<job_id>/preview` 保存，并调用 `POST /api/scripts/<script_id>/publish`
发布。正式剧本位于 `data/scenarios/scenario-<id>/scenario.json`，版本快照位于
`versions/<version>.json`，知识索引（embedding 或词法兜底）位于 `knowledge-index/<version>.json`。
旧房间继续使用创建时绑定的版本；只有显式迁移接口会改变绑定。扫描 PDF 当前会提示需要 OCR，
不会丢弃原始文件。

启用 OCR：安装 `requirements-ocr.txt` 并设置 `AI_TRPG_OCR_ENABLED=1`。启用本地向量库：安装
默认使用内置 SQLite 向量库，无需 Docker；如需 Qdrant，请安装 `requirements-vector-qdrant.txt`，设置 `AI_TRPG_VECTOR_BACKEND=qdrant`，并使用 `data/runtime/vector-db/qdrant/`。
Embedding 仍按本地模型 → OpenAI-compatible 服务 → 本地哈希向量顺序回退；失败时保留 JSON 索引。

## AI 全链路导入

上传时在 `POST /api/scripts/import` 的表单里带 `mode=ai`（不传则为 `direct`）。该模式不按标题
正则切块，也不再依赖场景卡推边，而是让模型依次完成四个阶段：

1. **通读剔除**：按行窗口扫描，找出作者吐槽、游玩心得、目录/排版残留、广告等与游玩无关的内容，
   返回 `prefix`（原文前几个字）、`line_start`/`line_end`、`category` 与 `reason`。程序只在原文中
   能定位到该前缀时才删除，定位失败不误删。剔除结果保存在剧本的 `import_exclusions` 与图谱
   `meta.exclusions`，导入后仍可审计。
2. **语义分模块**：结合文档自带标题与完整上下文划分场景/结局/背景模块，输出绝对行号与摘要；
   程序只做行号裁剪与防重叠。
3. **世界书**：为每个模块生成触发词、次级关键词、常驻标记、核心/背景分层与分组。
4. **图谱**：模型直接产出全量节点与关系边（地点连接、物品所属、NPC 常驻、NPC 与 NPC 关系等），
   并复用模块 id 作为地点节点 id；无论篇幅长短，都会再按模块做一次「资产补全」，逐场景核对
   NPC、物品、线索与归属边，避免只生成地点。程序只负责归一化、ID 去重、端点补全、非法边丢弃、
   缺节点补齐、`repair_bundle` 结构修复与 `validate_bundle` 报告。若图谱阶段请求失败或整份剧本
   没有产出任何 NPC/物品/线索，导入会直接失败并说明原因，不会静默发布只含地点的骨架图。

直接导入（`direct`）不再要求用户另点「自动建图」：导入任务结束前会按文档标题生成地点/结局
骨架图并随预览发布。它不含 NPC、物品及其关系，需要完整关系时请使用 AI 全链路或剧本包导入。

图谱的目的不是画得好看，而是让 KP 能判断「A 地的物品能否拿到 B 地使用」：地点之间的
`adjacent`/`portal`/`leads_to` 表达通行关系，`contains` 表达地点包含物品/子地点，`inhabits`
表达 NPC 常驻地点，`relationship` 表达 NPC 与 NPC 的关系。所有关系边都要求端点必须真实存在，
否则在归一化阶段被丢弃并在警告里说明。

## 剧本包导入（Skill / 外部 AI）

`POST /api/scripts/import/bundle` 接收 `the-veil-scenario-bundle` JSON（可直接上传文件或提交
JSON body），不重新调用 AI，只做校验、修复与落盘。网页端在导入入口选择 `.json` 即可走这条路径。

同一格式也由离线 Skill [`skills/scenario-import`](../skills/scenario-import/SKILL.md) 产出：
Skill 让外部 AI 先剔除无关内容，再按语义分模块、生成世界书与图谱，最后用
`scripts/validate_bundle.py` 自检并输出 `剧本名.veil-scenario.json`。字段定义见
[`bundle-schema.md`](../skills/scenario-import/references/bundle-schema.md)。

站内 Agent 不需要先产出文件再导入，可直接调用 `scenarios.import_script`：传 `content` 时执行
完整 AI 链路，传 `bundle` 时只校验修复；两段式预览确认后才真正创建剧本并写入世界树。

