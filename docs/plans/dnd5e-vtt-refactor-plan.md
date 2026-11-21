# D&D 5e 跑团桌（VTT 模式）重构计划

- 状态：规划稿（不动代码）
- 日期：2026-11-21
- 目标：把 dnd5e 体验从「聊天 + 简易地图」升级为奥法地图 / FVTT 式的线上跑团桌：进入 dnd5e 房间自动切换战桌 UI、兼容奥法地图发布的 FVTT 世界包（我们称「剧本」）与地图包导入、世界包自动生成世界树/图谱供 AI DM 记忆、AI 担任 DM、玩家进房跑团、地图即骰桌。
- 参考基准与调研存档：
  - 视频：【奥法地图: 你的线上跑团桌】 https://www.bilibili.com/video/BV1JSsT6JEhK → `2026-11-21/arcanemap-video-survey/outputs/arcanemap-video-survey.md`
  - 奥法地图大厅/资料库 https://arcanemap.com/user-files/compat/lobby.html#/library → `2026-11-21/arcanemap-lobby-survey/outputs/arcanemap-lobby-survey.md`
  - 5e 中文 wiki https://5e.kiwee.top/ → `2026-11-21/kiwee-wiki-survey/outputs/kiwee-wiki-survey.md`
  - FVTT UI 布局专项 → `2026-11-21/fvtt-ui-survey/outputs/fvtt-ui-survey.md`
  - FVTT 机制（场景/墙/光/法术模型，已落地的差距分析）→ `2026-11-17/fvtt-mechanics-survey/outputs/fvtt-mechanics-survey.md`
  - ArcaneDesk（AI 驱动 Foundry 的写协议参考）→ `2026-11-17/arcanedesk-survey/outputs/arcanedesk-survey.md`
  - 样例世界包：`docs/样本/sixianyuchanmian-1.0.1.zip` 与 `docs/样本/heroes-to-yuanshan-0.1.6.zip`（均为奥法地图 OSS 分发的 FVTT 世界导出，dnd5e 系统）
- 铁律：一切能力落在插件里，插件可整体拔出，主程序不因缺插件损坏；插件之间允许声明依赖（`requires.plugins`）。

---

## 1. 现状基线（已确认，截至 2026-11-21）

### 1.1 已经存在、可直接搭房子的东西

| 能力 | 现状 | 位置 |
| --- | --- | --- |
| dnd5e 规则引擎 | 已实现：角色卡 schema/d20 检定/战斗轮次与行动经济/法术位与专注/效果与条件/升级/短休长休/怪物 stat block/遭遇工具 | `data/plugins/dnd5e/dnd5e_plugin/` |
| AI DM 工具面 | 已声明 30+ 工具：roll_check、combat.*（attack/damage/heal/next…）、spell.cast/concentration、effect.*、scene.advance、area.*、workflow.*、reaction.*、content.search | `dnd5e_plugin/site_agent.py`、`tools.py` |
| 服务化工作流 | 战斗写操作已有 requestId 幂等 + revision 乐观并发 + 四态回执 | `agents/tools/` + 战斗存储 |
| veil-map 场景引擎 | 场景分片代际存储、格坐标棋子、墙体四类+门状态、角度扫描视线、明暗光照、探索迷雾、观察者裁剪下发、空间查询（圆/锥/线）、UVTT/FVTT 场景 JSON/裸图导入、网格标定 | `data/plugins/veil-map/` |
| dnd5e ↔ veil-map 胶水 | 战斗参战者自动同步棋子；AI 移动复用边界/占位/行动预算校验 | `dnd5e_plugin/map_link.py` |
| E0 UI 契约 | 规则集可声明 workspace 布局（primary=canvas、右 sidebar、topbar slots、left_rail、panels 归属），未声明回落聊天布局；前端 `WorkspaceLayoutChoice` 已消费 | `backend/trpg_server/ruleset/ui.py`、`frontend/src/types/workspace.d.ts` |
| dnd5e workspace 声明 | `plugin.json` 已声明 `dnd5e-canvas`（右栏 380px 可收、顶栏 slots、panels: chat=sidebar/character=drawer/combat=topbar） | `data/plugins/dnd5e/plugin.json` |
| 画布宿主 | 世界坐标画布 + `setBaseStage({background, gridSize})`；插件 UI 脚本同源加载 | `frontend/src/app/workspace.ts` |
| 3D 骰子 | three + cannon-es 独立 bundle（dice3d），按需加载 | `frontend/src/dice3d/` |
| 剧本体系 | 场景文本 + 触发器 + 叙事图谱（世界树）+ JEV 预判断 + 记忆账本，支持非线性拓扑 | `agents/narrative/`，设计书 `docs/kp-worldtree-graph-design.md` |
| 剧本导入管线 | docx/pdf → 剧本导入管线与 AI 辅助导入已有 | `scenario_import_pipeline.py` |
| SRD 内容包 | dnd5e-srd 八个结构化 JSON，CC BY 4.0 | `data/plugins/dnd5e-srd/packs/` |
| 房间模式规划 | `room_mode`/`ai_kp_enabled`/`scenario_binding` 正交设计稿（未实现） | `docs/plans/room-modes-plan.md` |

### 1.2 明确的缺口（本计划要补的）

1. **FVTT 世界包导入**：两个样例包的核心数据都在 **LevelDB**（FVTT v10+ 存储格式）里，仓库没有任何 LevelDB 读取能力；世界包 → 「剧本 + 场景 + 角色 + 资产」的转换管线不存在。
2. **战桌 UI**：`dnd5e-canvas` 只有布局声明，FVTT 式完整战桌交互不存在——左上工具组（选择/移动 ↔ 模板工具）、圆/锥/线/方模板、棋子操作卡（攻击/法术/特性/能力/物品）、右侧栏图标切换面板（聊天/战斗/场景/角色/物品/日志）、3D 骰子掷在地图上。
3. **DM/玩家可见性分层**：veil-map 有观察者裁剪，但 dnd5e 战桌各面板没有系统的 DM/玩家视图差异（场景面板对玩家隐藏、NPC/怪物卡对玩家隐藏、战斗操作由 AI DM 执行）。
4. **世界树自动生成**：世界包 journal/scenes/folders → 世界树/图谱的自动转换（现有世界树由 CoC 剧本导入生成）。
5. **奥法地图式大厅闭环**：现状创建房间强制 scenario_id + 前端强制选卡，缺「选剧本→开桌→请柬邀请→玩家入座」的体验层与 joinId 请柬模式。
6. **规则内容深度**：dnd5e-srd 只有 SRD 5.1 骨架，kiwee 数据（PHB 全量中英双语、机翻标记、2014/2024 双版本）尚未接入。

---

## 2. 调研结论摘要

### 2.1 奥法地图视频（BV1JSsT6JEhK）

详见 `2026-11-21/arcanemap-video-survey/outputs/arcanemap-video-survey.md`（含完整旁白转录 + 关键帧索引，素材在 `.tmp/video-bv1jsst6jekh/`）。要点：

- **产品流程实拍**：大厅（战役卡片 + 导入/创建）→ 规则印章三选一（5E24/5E14/CoC7，锁定不可改）→ **FVTT 世界包「放包→嗅探→报告」导入**（识别 dnd5e 5.3.3）→ 跑团桌 → **6 位短码请柬邀请** → 手机 6 步车卡向导 → 战斗演示 → 战役自动持久化回大厅。
- **掷骰展示确认了参考图五**：红色 3D d20 **直接投在地图上**（移动端投在聊天消息卡上）；GM 可限同批最多 20 骰/关动画；多款骰子皮肤；聊天支持 `/r 2d6+3`。
- **战斗链路**：参战者自动入列 → 玩家自掷先攻、DM 代掷 NPC → **地图顶部先攻卡条**高亮当前回合 → 点 token 卡按钮一键攻击/施法 → **结构化日志卡**（公式→结果→命中 vs AC→伤害→HP 增减全程数值链）；AOE 有「放置测量模板」按钮（锥形/球形）。
- 角色卡：速览四盒（命中/法术 DC/被动感知）+ 有限资源池 + 购点/掷骰/标准三模式建卡。
- **全程无任何 AI 功能**（画面里的"问问 Gemini"是 Chrome 扩展）——**AI DM 正是该产品留下的生态位空白，我们的核心差异化**。
- 页即上下文的路由切分（lobby/play/studio/mobile/invite/display 六页）、公共显示屏投 TV（玩家视野并集）值得借鉴。
- 视频未展示：邀请弹窗细节、战役结束/存档 UI、地图编辑器、语音通话（调研文档第 6 节有完整清单）。

### 2.2 5e.kiwee.top wiki → 规则事实源策略

详见 `2026-11-21/kiwee-wiki-survey/outputs/kiwee-wiki-survey.md`。要点：

- 它不是 MediaWiki，是 **5etools 完整中文镜像**：静态 HTML 壳 + 客户端 JS 渲染，**全部规则数据在公开 JSON**（`data/*.json`，总量约 117 MB），开源于 GitHub `tjliqy/5etools-cn`（源码 MIT、中文数据 CC BY-NC-SA 4.0）。
- **用法**：GitHub 快照建库为主（不碰活站，robots.txt 对 `/data/` 是 Disallow），SQLite FTS5 中英文索引，`translator` 字段做机翻质量标记，`reprintedAs`/`source` 做 2014/2024 版本切换。
- **关键富数据**：法术/怪物的伤害与豁免是 `{@damage 8d6}`、`{@scaledamage …}` 机器可解析 datatag——正是 AI DM 自动结算需要的「规则即数据」；怪物属性块完整结构化（ac/hp 公式/速度/属性/cr/动作）。
- **DM/玩家分界**（我们要抄的可见性口径）：玩家侧 = 种族/职业/专长/背景/物品/法术/状态；DM 侧 = 怪物图鉴（bestiary）、冒险模组全文（adventure）、DMG 书目、掷表、陷阱物件、遭遇构建。站点本身无鉴权，分界是内容定位——**我们的平台把这条分界变成真正的权限**。
- **引用溯源**：AI DM 回答附 `https://5e.kiwee.top/spells.html#phb_火球术` 深链（真实浏览器可渲染）。
- 与现有 `dnd5e-srd` 的关系：kiwee 作为**深度补全层**（PHB 全量 + 中文），SRD 包保持计算兜底；两者按 `ENG_name` 对齐。

### 2.3 奥法地图大厅/资料库 → 跑团闭环与包分发

详见 `2026-11-21/arcanemap-lobby-survey/outputs/arcanemap-lobby-survey.md`（633 行，含全部 API 反推）。要点：

- **「房间」= 战役 campaign（持久对象）**：owner + players[] + joinId + 创建时锁死的 ruleset；没有「散桌」概念，退出即回大厅，销毁靠删除战役。
- **请柬邀请闭环**（最值得抄）：`invite.html?join=<joinId>` 落地页先 GET 预览（战役名/GM/规则/在座人数，无鉴权知情），登录后 PUT 加入自动入桌；非成员直链进 play 给「输入请柬编号」兜底框；**踢人不收回角色卡，可凭原链接再入座**。
- **导入「登记台」三步流**：放包（拖 zip）→ 嗅探（sha256 验身、world/module/assets 三类分流、同名冲突规则）→ 完成报告（统计 + 「归属待指派 N 张卡」待办指引）；失败 abort 会补偿清理（回滚素材、删除误建空战役）。
- **写一致性纪律**：清单 `revision` 乐观锁（REVISION_MISMATCH 一律零写入）+ 规则写门（428/409 + RULESET_* 错误码）。与我们战斗写协议同构，直接并入各写接口。
- **资料库双层模型**：只读「系统内容包」（静态托管）+ 用户「我的内容包」（zip 上传 module 分组）；战役侧只存 refs + enabledSources，删包不破坏已取用世界。**「库」与「桌上的引用」分离**。
- **世界包分发**：OSS `worlds/<id>/<version>/{world.json, zip, previews/}` + 单文件 `index.json` 总目录（17 个世界，含 sha256/bytes/统计/署名/标签/正版链接）——零后端的剧本库分发形态，`credits/tags/moduleLinks` 三件套解决授权署名，可同构用于我们的剧本市场。
- **技术形态**：无框架手写 ESM + Caddy 静态目录 + 同域 Keycloak + 自研轻量 WS；play 页本质是「浏览器内 Foundry 兼容运行时」（我们不需要复制它，我们要的是它的**产品流程与数据格式**）。

### 2.4 FVTT UI 布局专项 → 战桌 UI 规格

详见 `2026-11-21/fvtt-ui-survey/outputs/fvtt-ui-survey.md`。要点（对照参考图一~五）：

- **整体**：顶场景导航条 + 中央画布 + 左控制栏（图层→工具两级）+ 右图标边栏 + 左下用户列表/宏热栏 + 底部聊天输入；侧栏面板可 pop-out。
- **左栏图层开关**：Token 层 / 测量模板层 / 图块 / 绘制 / 墙壁 / 光照 / 环境音 / 注记；**玩家只开放 Token / 测量模板 / 注记**。
- **右栏面板顺序**：聊天 → 战斗追踪器 → 场景 → 角色 → 物品 → 日志 →（随机表/卡牌）→ 播放列表 → 合集 → 设置。参考图四的红圈图标组与此吻合；我们的六面板 = 聊天/战斗/场景/角色/物品/日志。
- **战斗追踪器**：遭遇列表 + 逐人/全部/NPC 掷先攻 + 隐藏参战者 + 阵亡标记 + 轮/回合步进 + 玩家自助 End Turn；条目 = 缩略图+显示名+先攻值+资源条（默认 HP）；**Displayed Name 可对怪物改名掩护**。
- **模板四型交互**：Circle（拖出半径）/ Cone（默认约 53°，圆头或平头）/ Rectangle（原点为角点）/ Ray（默认宽一格）；点击-拖出-松手成型、自动吸附格位与格点、Shift+滚轮旋转、Ctrl+滚轮微调、右键原点对玩家隐藏、双击开配置（角度/宽度/距离/颜色/纹理）。受影响格位高亮。
- **Token HUD（右键）**：资源条+数值输入、状态图标、目标标记、Toggle Combat 入战斗；阵营色环（中立黄/友青/敌红）；玩家拖 token 不能穿墙（GM 可）。
- **掷骰形态**：原生是聊天卡（公式+分骰展开+`/r 2d6+3`），地图上原生只有 token 头顶聊天气泡；3D 骰子是 Dice So Nice! 模块（物理投掷、nat20 特效、**可把 3D 骰子丢在桌面上**）。→ 我们参考图五的「骰子直接投在地图上」= dice3d 以画布为落点，原生能力现成。
- **角色卡操作按钮** → dnd5e 系统的 **Activities**（12 类活动：Attack/Cast/Check/Damage/Heal/Save/Summon/Teleport/…）：物品行内按钮 → 用量对话框 → 带按钮聊天卡；新版角色卡 tab = 详情/物品/特性/法术/效果/传记；另有 favorites 快捷动作区（最接近参考图按钮组的原生形态）。
- **玩家/GM 差异**：用户角色五档 + 单文档权限四档（默认仅 GM 可见——怪物卡靠这条）+ 内容可见性（GM-Only 场景 + pull to scene）；暂停时玩家不能动 token；whisper/Blind Roll 收件人限定。
- **测距尺**：R 键尺子 + token 拖动自动显示移动代价、F 放路径点——veil-map 的空间查询已支持等价判定，交互可渐进实现。

---

## 3. 样例世界包解剖（两个包都实测确认）

两个包都是奥法地图 OSS 分发的 FVTT 世界导出（`system: dnd5e`、`coreVersion: 13.351`、`systemVersion: 5.3.3`）：

| | `sixianyuchanmian-1.0.1.zip`（丝线与缠绵，125MB） | `heroes-to-yuanshan-0.1.6.zip`（英雄向远山而行，15MB） |
| --- | --- | --- |
| 场景 | 6 张；`assets/scenes/<name>.jpg + <name>.json` **伴生 JSON 齐全**（name/width/height/grid/lights/walls/darkness/globalLight/foreground） | 4 张；**无伴生 JSON**，场景元数据只在 LevelDB；背景图在 `assets/` 根，缩略图在 `assets/scenes/` |
| LevelDB | 15 个集合目录；**.ldb 块是 snappy 压缩**（实测原始字节确认） | 同结构；journal 有真实条目（JournalEntry + pages，含 DC 检定文本） |
| actors | NPC/怪物卡内嵌法术/武器/多攻击条目（标准 dnd5e 5.3.3 文档） | PC 卡带全装备（背囊/水袋/提灯/盗贼工具…） |
| journal | **空库** | 9 个 JournalEntry，含章节化 pages（HTML 内容、DM 文本、Handouts） |
| 资产复用池 | `assets/arcanedesk/<sha256>.jpg` 内容寻址去重池 | 同（24 个哈希文件） |
| 封面 | `assets/world-cover.png`，`background` 字段是 FVTT 内部相对路径（`worlds/<id>/assets/…`） | 同（.jpg） |

对导入器的硬要求（两个包交集，读取层已实测落地，见 §3.1）：

1. **LevelDB 读取是必须的**（伴生 JSON 只是 sixianyuchanmian 的加速器，heroes 的场景、以及两包的 actors/journal/items 都在 LevelDB）：MANIFEST → version set → .ldb table → block（snappy 解码 + restart 数组 + 共享前缀键）的纯 Python 只读链路。
2. snappy 压缩块已实测存在（非可选）；**这些包的 .ldb 尾部 magic 被奥法地图替换成变体 `0xdb4775248b80fb57`**，其余表结构与标准 LevelDB 逐字节一致——读取器需双 magic 放行。
3. 键格式 `!<collection>!<id>`；**场景的 tokens/walls/lights 是独立子集合**（`!scenes.tokens!<sceneId>.<tokenId>` 等），不是嵌在场景文档里；journal.pages 同理按 journalId 聚合。
4. 资产引用路径形态多样：`worlds/<id>/assets/…`（manifest）、`assets/…`、`assets/arcanedesk/<sha256>`——按「文件名后缀匹配 + sha256 池直查」双策略解析。
5. FVTT 文档字段以 5.3.3 dnd5e schema 为准（fvtt-mechanics-survey 字段参考 + kiwee 的 foundry-*.json 导出对齐 + 实测映射表 §3.2）。
6. 网格尺寸并非恒定：heroes 有 grid.size=192/282 的场景（sixian 全 150）——换算一律以每场景自己的 grid.size 为准，别假设 150。

### 3.1 实施进度快照（2026-11-21）

- 读取层完成并提交：`351f986 feat(fvtt-world-import): read-only LevelDB+snappy layer with collection dump CLI`——两包全量导出验证（heroes 17 集合/578 文档、sixian 12 集合/1213 文档），14 个测试，snappy copy2/4 长度位宽（6 位）与 .log WriteBatch 解析修正。
- 格线对齐宿主契约完成并提交：`60014ca fix(map): setBaseStage 背景支持对象形态`（在此之前的 `3c2ac67` 已透传 gridOffset）——M1 的唯一核心改动收口。
- 场景转换管线 + 插件注册完成并提交：`bde5a87`（audit + scenes/run → veil-map）。
- actors 转换完成并提交：`c9e1358`（characters → dnd5e 角色卡 + NPC statblock 模块并入剧本，配额预检 + 升版重钉）；字段映射表 `docs/plans/fvtt-world-import-actor-mapping.md`。
- journal → 剧本场景文本 + 世界树完成并提交（M1 收官刀）：`convert_journal` 纯函数（folders→章节、页→节点、ownership→public/hidden/kp_only、场景挂点拓扑；AI 辅助切块/摘要/边推断可选注入，缺席/失败确定性降级）+ `journal/run` 端点（模块并入绑定剧本 + 图谱合并进 narrative.json）+ inspect journal 计数；heroes 2 条目 7 页全量对账，sixian 空库优雅返回 0 条。
- M2 第一刀完成并提交：`374a024`——dnd5e-vtt 战桌骨架：骑在 dnd5e 已有的 `dnd5e-canvas` workspace 声明上（进房自动切画布是核心现成路径），新增 `room.rail.right` 插件槽消费（每插件独立事件 handlers）；六面板：聊天全桥接现有聊天流、战斗接核心 combat 事件、角色含 NPC/怪物目录（仅 DM）、日志按观察者过滤、场景与物品为空态接线点；`/panels` 观察者裁剪端点（玩家响应无 scenes/npc 键）；无地图降级提示与插件拔出回落 E7 三 tab。基线 2447 passed / 3 skipped。
- M3 完成并提交：`febb5d8`——测量模板四型（圆/锥 53°/线/方，FVTT 式拖放+格吸附+ft 标注+Shift/Ctrl 滚轮，格空间存储）+ 测距尺（判墙走 veil-map spatial）+ 棋子操作卡（DM 五按钮、玩家服务端不下发 actions 键）；写协议 request_id 幂等 + revision。
- M4 完成并提交：`e1181df`——地图骰桌：new_message/combat_dice 双路监听 + 8s 去重、3D 骰盘落点=掷骰者棋子、结果飘字（nat20 金色/nat1 低亮）、DM 开关与 1–20 上限（与 bundle 硬上限取小）、玩家底栏 d20 快捷掷骰走现有接口；无地图自动回落聊天卡。基线 2474 passed / 3 skipped。
- 第五刀完成并提交：`c85e815`——AI DM 战桌四工具（dnd5e-vtt.place_template / switch_scene / roll_broadcast / worldtree_recall，DM 门闸、幂等/四态回执、观察者裁剪广播）+ KP 提示词【战桌状态】行注入（kp_context 服务 → ruleset_fragments，任何一环缺席零注入）+ 场景切换前端遮罩；顺手修正核心 plugins/registry 的能力段 module 激活 bug（第二模块被已导入 entry 静默吞掉）。基线 2483 passed / 3 skipped。
- M2 第二刀（计划里 M3 的第一段）完成：地图交互——画布左上工具组（选择/测距尺 ↔ 模板四型 + 清空，学 FVTT 点击-拖出-松手、格点吸附、ft 标注、Shift+滚轮旋转/Ctrl+滚轮微调）；模板服务端登记 `GET|POST /api/plugins/dnd5e-vtt/rooms/<id>/templates`（request_id 幂等 + revision 乐观并发 + 四态回执，写仅 DM 玩家 403，存 `plugins/dnd5e-vtt/templates.json`，socket 广播 `dnd5e_vtt_templates` 全员只读）；测距尺复用 veil-map `/spatial/line`；受影响格高亮：圆/线走 veil-map `/spatial/range`、锥（53.13° 无对应原语）与方形本地几何近似并明说；棋子操作卡 `GET …/token-actions`（DM 从战斗状态/剧本 statblock/角色卡拉攻击/法术/特性/能力/物品清单，本刀只做详情浮层；玩家仅自己棋子且响应无 actions 键 → DOM 不渲染，他人 403）；玩家工具组只有选择+测距尺（模板按钮不渲染）。veil-map 零改动、fvtt-world-import 与规则引擎未动。测试 `tests/test_dnd5e_vtt_templates.py` 17 例。
- M4 地图骰桌完成：所有掷骰汇聚为带 `metadata.dice_roll` 的房间消息 + `combat_dice` 广播，两路共用去重键（request_id 缺省时表达式/骰面/总数/掷骰者复合键，TTL ~8s）；插件在掷骰者可见棋子（无则视口中心）落一桌 3D 骰子（`/js/dice/dice3d.js` 按需注入不重编译、不传 seed——bundle 翻滚后 `applyLegacyResultFaces` 把服务端 faces 贴到朝上面，动画即权威值；外观照 diceAppearance 精简移植：localStorage + 主题变量 + `/api/dice-textures`；同屏最多 2 桌活 WebGL，超限冻结成静态图），同时上浮结果飘字（nat20/nat1 高亮、多骰合并 total+expression）；DM 齿轮弹「骰桌动画」开关 + 上限输入（`GET|POST /api/plugins/dnd5e-vtt/rooms/<id>/dice-settings`，GET 全员、POST 仅 DM 403 rejected 四态回执，max_dice clamp 1–20，存 `plugins/dnd5e-vtt/dice-settings.json`，广播 `dnd5e_vtt_dice_settings`）；底栏 d20 dock 全员快捷掷骰（表达式限 NdM 加法，复用 `TrpgDiceTable.handleDiceTableCommand`，本地掷骰者以 persistLocalDiceMessage 包装捕获喂地图动画，失败静默降级）。无地图（background 空）时地图层整体跳过；聊天掷骰卡管线零改动。测试 `tests/test_dnd5e_vtt_dice.py`。
- M5 第一刀完成（范围修订版 §7.5）：登记台自动建剧本 + 一键开团指引——`POST /api/plugins/fvtt-world-import/scenario/import`（登录即可，multipart zip）：审计复用 inspect 的 world_package.audit（不过审零写入）→ 自动创建可开团的新剧本条目（标题/描述取 world.json、封面 background 解 zip 后内容寻址落剧本 `assets/<hash16>`、journal 场景文本模块 + 图谱 `narrative.json` 复用第四刀 convert_world_journal 直接写入，journal 空库/集合缺席 → 0 条 + 警告、图谱不写、条目照常产出）；幂等：同 world id 重导按 `fvtt_world_import` 溯源标记定位既有剧本原位升版（journal 产物与图谱整体替换，作者手写模块与 actors 刀 NPC 模块保留），不生成重复剧本；孤儿防御：新建路径图谱写失败整目录清理、升版路径回滚剧本记录并报告；inspect 补 `scenario_would_create` 摘要（标题/版本/模块数预估）；场景与角色卡仍走房间维度（`rooms/<id>/run`、`rooms/<id>/actors/run` 不变），响应附「开团指引」（已核实 `POST /api/rooms` 本就接受 scenario_id 直选：room_mode=scripted 时校验剧本规则集并钉版本与首个场景指针，无需另做「从剧本开房」封装），玩家进桌走现有房间码/邀请链接，零新造请柬体系。测试 `tests/test_fvtt_world_import_scenario.py` 10 例（两包实测：heroes 7 模块/14 节点/封面 337KB；sixian 0 模块/封面 6.5MB 如实落盘）。基线 2502 passed / 3 skipped。
- M5 第二段完成并提交：`64d1433`——dnd5e 房规档案按剧本类型适配：`theater-of-mind`（=schema 全默认，现状兼容）与 `grid-combat`（dnd5e_positioning=grid，对角线/里程碑键预留未生效并明说）两档案；判定链走 veil-map 活动场景（带地图世界包房间开箱即命中 grid）；推荐端点 `/rooms/<id>/rules/recommendation`，自动应用仅限「房主从未手动设置」窗口（改任何键即永久 manual 不回写），站点配置可全关；KP 提示词【房规口径】行；记忆落插件自有文件，核心 room_ops 零改动。
- M5 收官刀完成并提交：`8c333ec`——前端 UI 胶水（后端零补缺）：剧本页「导入剧本」对话框第三选项「导入世界包」（审计预览卡→导入为剧本→幂等升版提示）+ 结果卡「用此剧本开团」直调现有 openCreateRoomWithScenario 预填 + 房间详情操作栏 DM 专属「导入世界包」（三步勾选：场景/角色/journal）+ 房规弹窗推荐横幅（auto_applied 才渲染，撤销/切档走现有房规 PUT 单键）。前端 typecheck/build 全绿，全量 2502 passed。**M1-M5 全部里程碑收口。**

## 3.2 实测反馈修复（2026-11-22 凌晨）

- 剧本卡作者名过长拉长卡片：`3f1865f`——scenario-card-meta 作者/创建者行省略号截断（与标题行同款）。
- kiwee 深库资料层并入 dnd5e 插件：`7241a8e`——快照脚本（GitHub 稀疏检出版，不爬活站）+ SQLite FTS + datatag 解析器 + `kiwee_db` 服务注册 + NOTICE 锁插件内；数据/建库产物不进 git；消费面接线（knowledge/UI/引擎）待下一刀。
- 迷雾视觉全面升级：`3817af5`——三档（未探索近黑 0.95 / 探索记忆 0.63 / 视野透亮+冷调区分明暗）、边缘 1.6 格柔化（ctx.filter 高斯 + JS 退化路径）、新区域 300ms 交叉淡入、雾裁剪到底图矩形（溢出区死黑修漏）、掩码版本号节流；200×200 格 worst case 重建 1.0ms。
- **开房自动装配**：`c238126`——根因：登记台只写剧本数据，场景/actors 是房间维度，「导入剧本→开房」路径无人调它们。方案：scenario/import 时把已转换场景+NPC 模块暂存剧本侧（stash，背景图内容寻址复制，重导幂等刷新）+ `rooms/<id>/auto-provision`（房主/房管首次进房前端静默触发，幂等零重复，房间已有场景绝不抢占）+ veil-map 广播全员见图。heroes 实测：3 场景（雨中伏击激活，10 棋子）+ 12 NPC 模块自动落房。

---

## 4. 总体架构：插件划分与依赖

```text
核心（不动或最小改动：房间/成员/权限 · 剧本存储 · 叙事/世界树运行时 · AI 链路 · 骰子 · 插件运行时）
        │                    │                     │
        │ ruleset contract   │ scenario 剧本契约    │ workspace UI 契约(E0)
        ▼                    ▼                     ▼
  ┌──────────┐   hard   ┌───────────────┐   ┌─────────────────┐
  │ dnd5e    │────────▶│ dnd5e-srd     │   │ veil-map        │
  │ 规则引擎  │         │ SRD 内容包     │   │ 场景/墙/光/迷雾  │
  └────┬─────┘         └───────────────┘   └────────┬────────┘
       │ optional(map_service)                    │
       │ soft            ┌──────────────────────┐  │
  ┌────▼───────────────┐│ fvtt-world-import（新）│◀─┘
  │ dnd5e-vtt（新增）   │▶ 场景/actors/journal →   │
  │ 战桌 UI+战斗面板    │ │ 剧本+veil-map场景+世界树│
  │ 模板/骰桌/视图分层   │ └──────────────────────┘
  └────────────────────┘
```

- `fvtt-world-import`（新插件）：LevelDB 读取、世界包/地图包 → 剧本（世界树）+ veil-map 场景 + 角色/statblock + 资产落盘 + 剧本条目登记（index 模型学奥法 OSS：credits/tags/时长/人数统计）。依赖 `veil-map`（场景写入服务）与剧本存储契约；**不依赖 dnd5e**（导入器规则集中立，但本期只放行 `system=dnd5e` 的包）。
- `dnd5e-vtt`（新插件）：战桌 UI（模板工具、右侧栏六面板、token 操作卡、地图骰桌、DM/玩家视图差异、请柬/入座体验层）。依赖 `veil-map`（渲染宿主）与 `dnd5e`（战斗/法术引擎数据），只做前端资产 + HTTP + slots 叠加，不复制规则代码。
- `dnd5e` / `veil-map` 保持现状职责，开放少量稳定服务接口（场景批量写入、剧本世界树注入、面板数据 scope 裁剪），不互相 import Python 模块。
- kiwee 内容深库（2026-11-23 夜范围修订）：**并入 dnd5e 主插件**（不再独立成插件）——快照脚本 + SQLite FTS 索引 + datatag 解析器作为 dnd5e 插件的资料子模块；CC BY-NC-SA 声明与署名锁在插件目录 NOTICE 内不传染主程序；**插件默认加载**，快照未拉取时自动回落 dnd5e-srd（零功能损失，只是深度降级）。**消费面三方**：①KP/网站 Agent 的 knowledge.search_ruleset 深库优先（带 translator 质量徽章与深链）②UI/玩家侧（法术/怪物/物品浏览与查询，bestiary/DM 书目按 DM 权限门控——沿用 6.3 可见性口径）③规则引擎（datatag 解析出结构化骰公式供战斗工具消费）。数据不进 git：建库产物是本地生成物，脚本与 NOTICE 进库。
- 拔出任何新插件：dnd5e 回落到现状聊天+简化地图体验，主程序无感。

---

## 5. FVTT 世界包导入管线（fvtt-world-import，设计）

### 5.1 「登记台」三步流（抄奥法地图的产品形态）

```text
上传 zip → 审计（体积/文件数/魔数/sha256 验身——学习它的 FILE_IDENTITY_MISMATCH）
  → 嗅探（world.json 校验 system=dnd5e、coreVersion 兼容窗口；world / module(地图包) / assets 三类分流；
          同名剧本冲突规则：沿用原规则或换名新建，不静默覆盖）
  → 预览报告（plan：标题/版本/场景数/角色数/journal 数/资产字节/缺失项警告；journal 空的包如实显示）
  → 人工确认 → 执行导入（失败策略 abort：补偿清理半成品）
  → 完成报告（入册统计 + 「角色归属待指派」待办 + 剧本编号/请柬）
```

### 5.2 转换管线

```text
world.json ─────────▶ 剧本条目（标题/描述/封面/版本/作者/标签/时长/人数）
data/scenes（LevelDB）→ veil-map 场景：背景/网格(grid.size→格宽, grid.type=1 放行)/墙折线(walls[].c)/
                        门/光源(lights)/环境光(darkness,globalLight)/激活态；
                        sixianyuchanmian 的伴生 JSON 优先、LevelDB 补 flags；heroes 直读 LevelDB
  · 场景内嵌 tokens（实测 heroes LevelDB：displayName/disposition(-1敌,0中立,1友)/x,y/宽高/actor 引用）
    → veil-map 棋子直供落位 + 阵营色环，无需手工摆放；
    sixianyuchanmian 场景无内嵌 token（两个包形态不同，导入器都要覆盖）
data/actors（LevelDB）→ dnd5e 角色卡（type=character：属性/熟练/HP/AC/装备/法术/特性）
                        + 怪物 statblock（type=npc：FVTT dnd5e 5.3.3 字段 → 我们 monsters schema 映射表）
data/items ──────────▶ 物品/法术/特性条目（挂角色或入内容库）
data/journal ────────▶ 剧本场景文本 + 世界树节点（页 = 节点；{_DC 10_} 等文本保留为检定线索）
data/folders ────────▶ 世界树的章节/分组层级
data/tables（可选）──▶ 掷表（后期）
assets/ ────────────▶ 内容寻址落盘（沿用素材区 assets/<hash16> 模式）+ 引用重写
```

**「世界包参数直供」原则**（已确认可行，避免现状标定错位问题）：

- **格线对齐**：世界包直接提供对齐数据——伴生 JSON 的 `grid`（150px/格）+ `shiftX/shiftY`，LevelDB 场景文档的 `grid{size,type}` 与 `background.offsetX/offsetY`。FVTT 的 shift 语义就是「把网格原点平移到与底图网格线重合」。映射：`grid.size → grid_size`、`shiftX/shiftY(+background offset) → grid_offset`。**不做导入后二次手工标定**。
- **棋子摆位**：场景内嵌 tokens 的像素坐标 ÷ grid.size → 格坐标，导入即落位；`disposition` → 阵营色环（中立黄/友青/敌红）。
- 配套修复（前置条件）：现状「格线与底图对不齐」的根因已定位——核心画布宿主 `setBaseStage({background, gridSize})` 不接受 `grid_offset`，插件标定后的偏移只在自己的绘制层生效、核心网格仍按世界原点画（`fvtt-mechanics-survey` §1.8 第 1 条遗留项）。M1 必须升级宿主契约为 `setBaseStage({background:{src,width,height,offsetX,offsetY,scale}, grid:{size,offsetX,offsetY}})`，让核心网格从标定原点绘制。这是唯一必须动核心的改动。

### 5.3 世界树生成（AI DM 记忆，允许 AI 辅助语义切块）

- folders 层级 → 章节；journal 页 → 节点（内容 + markdown 资产引用）；journal 与 scene 的名称/uuid 关联 → 边。
- **AI 辅助语义切块（已授权使用）**：journal 页原文 → 小模型做语义切分（场景/线索/遭遇/DM 私注四类标注）、节点摘要（marker）、边推断；**确定性的部分不依赖 AI**——页 ownership 与 folders 层级是规则推导，AI 只做语义增强。失败降级为「章节线性 + 场景挂点」拓扑，导入永不因 AI 失败而中断。
- 入知识库：切块产物进向量库（现有 SQLite 向量库），DM 侧/玩家侧按可见性分别建索引（对齐 6.3 的口径）。
- FVTT `ownership.default` 与 journal 可见位 → 世界树节点的 `public / hidden / kp_only` 标注（接现有可见性模型 §4.6）。
- 目标：导入完成后 AI DM 开局即有图谱检索与记忆账本可用，**不需要重跑**任何导入。
- 验收用例：heroes-to-yuanshan 的 9 个 journal 必须生成可检索的世界树（sixianyuchanmian journal 为空，验收退化为「actors/场景关系图谱 + 提示」）。

### 5.4 地图包（非世界包）

奥法地图还有单地图/场景包与素材包形态。本期支持「zip 内多张伴生 jpg+json 或多场景 LevelDB」的地图包入口：产出多个 veil-map 场景（可切换），不生成剧本。纯素材包给出可读拒绝（学它的 IMPORT_ASSETS_REJECTED 文案策略）。

### 5.5 关键技术点

- **LevelDB 纯 Python 只读**：MANIFEST/VersionSet → `.ldb` table（footer/magic/索引块/数据块）→ block 解码（snappy 纯 Python 解码器 + restart 数组 + 共享前缀键解压）。无 C 扩展依赖；`plyvel` 作可选加速不做前提。上限审计：集合文档数、单文档字节、解压后总字节——防解析型 DoS（沿用 veil-map 导入审计套路）。
- 六边形网格（grid.type≠1）：本期拒绝并警告（提示转换工具），不猜测。
- 文档 id 引用（`@UUID[...]`、`@Embed[...]`）→ 映射为我们的引用或降级纯文本。
- 坐标：FVTT 场景像素 / grid.size → 格坐标；`shiftX/shiftY` → 网格偏移。

---

## 6. 战桌 UI 设计（dnd5e-vtt）

### 6.1 布局总图（融合参考图与 FVTT 规格）

```text
┌──────────────────────────────────────────────────────────────┐
│ 顶栏(48px)：场景名/切换 · 玩家列表(桌边) · 回合指示 · 状态 · 设置 │
├───┬──────────────────────────────────────────┬───┬───────────┤
│左 │                                          │右 │ 右侧栏     │
│栏 │           地图画布（primary）             │图 │ (380px)   │
│48 │  · 左上工具组（两层，学 FVTT）：           │标 │ 图标组：   │
│px │    Token 层：选择/移动 · 测距尺           │48 │ 聊天(默认) │
│   │    模板层：⭕圆 ▽锥 ╱线 ▭方 + 清空        │px │ 战斗       │
│   │  · 模板吸附格位、ft 标注、Shift+滚轮旋转   │   │ 场景(DM)   │
│   │  · 选中棋子弹操作卡（见 6.2）             │   │ 角色       │
│   │  · 底部提示条：选择目标·左键点选·Esc取消    │   │ 物品       │
│   │  · 3D 骰子掷在画布上（dice3d 现成 bundle） │   │ 日志       │
│   │  · 地图顶部先攻卡条（当前回合高亮，视频实拍）│   │           │
├───┴──────────────────────────────────────────┴───┴───────────┤
│ 底部（玩家）：我的角色卡速览 · 桌边状态 · 邀请(请柬链接) · 进行中 │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 面板与工具明细（DM 视图 vs 玩家视图）

| 区 | 内容 | DM 视图 | 玩家视图 |
| --- | --- | --- | --- |
| 模板工具 | 圆（拖出半径）/锥（~53°，拖出方向+距离）/线（宽一格）/方（角点拖出）；吸附格位、受影响格高亮、Shift+滚轮旋转、双击配置 | 全部四型可放置 | 只读展示已放置模板（AI DM 放的可见） |
| 测距尺 | R 键/按钮；token 拖动显示移动代价 | 有 | 有 |
| 棋子操作卡 | 选中棋子弹出：攻击/法术/特性/能力/物品（dnd5e Activities 式，数据来自角色卡/statblock） | 全部可操作（含 NPC/怪物；等价于 AI DM 的执行面） | 仅自己角色可见，**操作按钮隐藏**（DOM 不渲染而非置灰——信息隐藏要求服务端裁剪而非 CSS 隐藏；无 AI 房间未来放开时改由服务端下发开关） |
| 右栏·聊天 | 聊天流 + 掷骰卡（公式+分骰展开）+ `/r 2d6+3`；token 头顶聊天气泡 | 全部 | 按可见性过滤（whisper/DM 豁免骰已有机制沿用） |
| 右栏·战斗 | 战斗追踪器：先攻列表/轮/回合/HP 条/条件图标/隐藏位/阵亡标记；掷先攻（逐人/全部/NPC）+ **地图顶部先攻卡条**（当前回合高亮） | 全量（NPC HP/AC 明细、Displayed Name 可改） | 玩家侧全显 + 敌人只显暴露信息（名字可被 AI 掩饰） |
| 右栏·场景 | 场景列表/缩略图/切换/拉玩家进场景 | 有 | **隐藏**（AI DM 管） |
| 右栏·角色 | 玩家角色 + NPC/怪物分组目录 | 全部 | 只见玩家角色 + 已揭示怪物名（对齐 FVTT 单文档权限：默认仅 GM） |
| 右栏·物品 | 物品/战利品目录 | 有 | 有（自己卡上的） |
| 右栏·日志 | 地图/战斗/检定事件流 + 导出 | 全量 | 过滤版 |
| 地图骰桌 | dice3d 以画布为落点 + 结果飘字 + 掷骰卡同步 | 有 | 有 |

### 6.3 可见性实现原则

「谁该看什么」服务端裁剪、前端只消费——把 veil-map 的 `_observer_scope` 模式扩展到 dnd5e-vtt 的所有面板数据接口：一个 `observer_scope(member, room)` 决定场景列表、怪物卡、战斗明细、日志条目的下发内容。口径依据：

- FVTT 实测的三层模型（用户角色 + 单文档权限 + 内容可见性）→ 我们简化为 DM/AI-DM/玩家 + 文档级 public/hidden/kp_only；
- kiwee 的内容分界（玩家侧：种族/职业/法术/物品/状态；DM 侧：bestiary/冒险全文/掷表）→ AI DM 的知识检索默认全量，玩家查规则入口只开玩家侧集合。

---

## 7. AI DM 接入与跑团闭环

### 7.1 接入点（最大化复用现有链路）

- AI DM = 现有 KP 角色的 dnd5e 分派，接入链路全部已存在：`/api/chat` KP 回合 → 规则集工具白名单（`site_agent_profile` 已声明 30+ 工具）→ 沉浸系统（配乐/配图）→ 聊天广播。**不新造 AI 通道，只扩展工具面与上下文注入。**
- 进房（dnd5e + 已导入剧本 + AI 开）→ 战桌 UI 自动挂载（E0 workspace 声明已就绪）→ AI 开场（复用 opening 流程）。

### 7.2 上下文注入（AI DM 的"眼睛"）

每轮 KP 提示词按层注入（机制全部已有，新增装配器）：

| 层 | 内容 | 来源 |
| --- | --- | --- |
| 世界树记忆 | 当前章节节点 + 图谱检索结果 + 记忆账本 | 导入产物（§5.3），`worldtree.recall` |
| 规则事实 | 涉及的法术/怪物/条件原文（kiwee 深库优先、SRD 兜底）+ 引用深链 | dnd5e-content-kiwee / dnd5e-srd |
| 战桌状态 | 活动场景/棋子/当前回合/先攻/HP 概要 + 观察者 scope 说明 | veil-map + dnd5e combat 现有快照 |
| 玩家输入 | 发言/宣布动作/`/r` 掷骰 | 现有 chat 管线 |

### 7.3 新增/扩展工具（dnd5e-vtt 挂载）

- `map.place_template`（AI 放法术模板：圆/锥/线/方，服务端几何校验后广播，玩家端可见）
- `map.switch_scene`（切场景 + 「拉玩家」事件 + 加载遮罩）
- `map.roll_broadcast`（骰桌动画广播：AI 替玩家或 NPC 掷骰的可视化）
- `worldtree.recall`（图谱检索包装）
- `knowledge.search_ruleset` 扩展：优先走 kiwee 深库（带机翻质量徽章与 2014/2024 版本标注），命中后正文 datatag（`{@damage 8d6}` 等）解析为可结算骰公式喂给 combat 工具。

### 7.4 战斗闭环

AI DM 掷先攻、操 NPC、放模板、结算伤害；玩家回合宣布动作 → AI 工具结算 → 结果同步战桌面板与棋子 HP 条（map_link 同步钩子已有）；掷骰以 3D 骰桌动画全员可见。误伤友军、反应窗口（护盾/反制/借机）走现有 workflow/reaction 工具。

### 7.5 开团闭环（2026-11-21 范围修订：不建平行请柬体系）

**平台已有完整的房间系统与邀请/加入流程，不再照抄奥法地图的 joinId 请柬**。本闭环只做两件事：

1. **登记台自动建剧本**：世界包导入直接产出可开团的剧本条目（world.json 元数据/封面/场景文本/图谱），不再要求房间先绑定既有剧本——导入完即可开房。
2. **一键开团**：从剧本库（或导入完成页）发起开房，预填剧本与规则集，走现有 create_room + 现有房间邀请/房间码流程让玩家进桌；「踢人不收卡可凭原链接回座」的语义沿用现有房间成员管理，不新造。

落地核实（M5 第一刀，2026-11-21）：`POST /api/rooms` **已接受 scenario_id 直选**——`room_mode=scripted` 时校验剧本规则集可用、钉 `scenario_version` 与首个场景指针（`routes/rooms_lifecycle.py` 的 `create_room()`，锚点见 room-modes-plan §3），因此**不做**「从剧本开房」的额外封装，`scenario/import` 响应只附开团指引字段（剧本 id + 现有开房流程提示），开房校验全部沿用现状。

无 AI 房间的预留不变（§7.6）。

### 7.6 无 AI 房间预留

`ai_kp_enabled=false` 的 dnd5e 房间：战桌 UI 照常挂载，棋子操作卡的服务端开关翻转为对玩家下发（§6.2 的隐藏按钮变为可用），战斗追踪器/场景切换的 DM 操作由真人房主接管。同一套面板数据接口，只是 observer_scope 的角色矩阵不同——**不为无 AI 房间另写一套 UI**。

---
## 8. 实施阶段（建议顺序）

| 阶段 | 内容 | 交付判定 |
| --- | --- | --- |
| M1 | fvtt-world-import：LevelDB 只读（含 snappy）+ 两个样例包全量导入（场景→veil-map、actors→角色/statblock、journal→剧本+世界树） | 两个样例都能导入开房；heroes 的 9 journal 生成可检索世界树；AI DM 能答出世界树内容 |
| M2 | dnd5e-vtt 战桌骨架：dnd5e-canvas 完整布局，右侧栏六面板，聊天/战斗/角色先通 | 无地图时 UI 不崩、面板可切换 |
| M3 | 地图交互：模板四型 + 测距尺 + 棋子操作卡（DM 全量、玩家预留）+ 观察者裁剪覆盖新面板 | DM 放锥形模板，玩家端正确展示且看不到场景/怪物面板 |
| M4 | 地图骰桌：dice3d 以画布为落点 + 飘字 + 掷骰事件进聊天卡 | 玩家掷骰全员可见动画 |
| M5 | 大厅体验层（范围修订）：登记台自动建剧本（世界包导入→剧本库条目）、一键开团预填、**复用现有房间邀请/加入流程**（不建 joinId 请柬体系） | 导入世界包→剧本库出现条目→一键开房→玩家走现有邀请进桌跑通 |
| M5+ | **房规按剧本类型适配**：dnd5e 房规（方格战斗/斜向/侧翼/里程碑等）默认档位随房间剧本类型（FVTT 世界包带地图 vs 纯文本剧本）自动选优，房主可改 | 带地图世界包房间默认格子战规则、文本剧本房间默认 theater-of-mind，两类型实测 |
| M6 | 内容与打磨：kiwee 深库并入 dnd5e 插件（快照脚本+SQLite FTS+datatag 解析；KP/UI/引擎三方消费面）、场景缩略图、日志导出、性能基线 | 深库建好后：AI 问答带质量徽章与深链、UI 能浏览全量法术/怪物（DM 门控生效）、快照缺席全量测试仍绿 |

每阶段独立可用、独立可回滚；M1 与 M2 可并行。M1 内部先做 sixianyuchanmian（伴生 JSON 加速），再做 heroes（纯 LevelDB + journal 世界树）作为完整验收。

---

## 9. 决策记录（2026-11-21 已拍板）与剩余风险

### 9.1 已拍板

| 决策 | 结论 |
| --- | --- |
| 玩家侧操作按钮 | **隐藏**：服务端裁剪、DOM 不渲染（非 CSS 置灰）；无 AI 房间未来放开时由服务端开关下发 |
| kiwee 数据许可 | 本项目完全开源、非商用；kiwee 数据仅用于 dnd5e 规则插件开发，**CC BY-NC-SA 4.0 声明与署名保留在 `dnd5e-content-kiwee` 插件目录内**（NOTICE/来源链接/translator 标注），不传染主程序与其他插件 |
| LevelDB 解析兜底 | 纯 Python 解析失败时允许 `plyvel` 兜底；**兜底触发时必须在导入报告与日志中显式提示**（"本次导入使用了兼容模式读取"），不静默切换 |
| 世界包参数直供 | 格线对齐用世界包 `grid/shiftX/shiftY(+background offset)` 直供；棋子摆位用场景内嵌 tokens 直供；不做导入后手工标定/摆放 |
| AI 辅助语义切块 | 世界树/图谱生成与知识库入库存档允许 AI 辅助（语义切分/摘要/边推断），失败确定性降级，不阻塞导入 |

### 9.2 剩余风险

1. **格线对齐宿主契约升级**是 M1 的前置核心改动（`setBaseStage` 收 background 对象 + grid 原点，见 §5.2）：影响面在 `frontend/src/app/workspace.ts` 与所有调用 `setBaseStage` 的插件 UI；需同步 CoC 侧回归。
2. LevelDB/snappy 纯 Python 读取的边角正确性（compaction/MANIFEST 版本集）：样例两包已实测存在 snappy 压缩块；plyvel 兜底已授权，风险可控但兜底提示不能漏。
3. FVTT dnd5e 5.3.3 角色文档 → 我们 schema 的逐字段映射误差：以两个样例实测字段为准写映射表；映射不了的降级「导入为文本说明」并进导入警告。
4. 机翻质量分级（translator 字段）：检索结果带质量徽章，AI DM 提示词中提示可信度。
5. 场景切换对玩家的节奏：玩家面板看不到场景列表，但地图切换全员可见——切换给 1s「场景加载」遮罩，避免剧透式闪现。
6. 两个样例包之外的 FVTT 世界（老版平文件 JSON 导出、六边形网格、更高 systemVersion）不在本期兼容窗口，导入器给出明确可读拒绝而不是半吊子导入。
