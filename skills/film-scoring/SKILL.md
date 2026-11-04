---
name: film-scoring
description: 程序化电影配乐的工程规则库：调式/和声选择表、动机库与变形算子、cue 结构与密度/动态曲线模板、按情绪档位的编配配方、以及「听起来廉价」的反模式清单。用于编写、审查或改造实时程序化配乐引擎（当前实现位于 frontend/src/app/immersive/music-*.ts），也可作为 AI 音乐导演的输出校验依据。
---

# 电影配乐 Skill（Film Scoring Rules）

## 适用场景

- 给实时程序化配乐引擎新增场景档位、改写作曲/编配逻辑；
- 审查一份 cue（段落结构、动机发展、动态曲线、配器）是否「像电影配乐」；
- 设计或校验 AI 音乐导演（`immersive.director_schema`）输出的乐谱 JSON；
- 需要在不引入外部音乐生成模型、不复制版权旋律的前提下，把合成音乐做到 cinematic 水准。

不适用：需要真实录音采样、外部 TTS/音乐生成 API、或纯音效设计的任务。

## 先读哪里

| 需要什么 | 读哪里 |
| --- | --- |
| 从场景到成品的完整流程、自动档位映射 | `references/composition-workflow.md` |
| 大师手法与工程转译、调研来源 | `references/reference-analysis.md` |
| 调式、和弦色彩、进行、pedal、平行五度 | `references/harmony-and-motif.md` |
| 动机写法、变形算子、节奏卡农、微复调 | `references/harmony-and-motif.md` |
| cue 五段结构、动态曲线、配器进入、打击乐语法 | `references/cue-and-orchestration.md` |
| 「廉价感」来源与修复 | `references/anti-patterns.md` |
| 现有代码契约 | `frontend/src/app/immersive/music-theory.ts`、`music-arranger.ts`、`music-voices.ts`、`music-engine.ts`、`backend/trpg_server/immersive/auto_score.py` |

## 一页速查：八档情绪的默认配方

`T` = tempo（BPM），`R` = 主音 MIDI，`S` = 和声策略。

| 档位 | T | R | 调式 | S | 和弦色彩 | 配器（按进入顺序） | 动机轮廓 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| daily 日常 | 78 | 60 | dorian | functional | add9/sus2/maj7 | pad → pluck → bass → strings → bells | 拱形、轻快回旋 |
| suspense 悬念 | 62 | 50 | dorian | pedal | min7/sus2/tritone/quartal | pad → bass → bells → strings → ostinato | 叹息、下行二度探针 |
| unease 不安 | 54 | 49 | phrygian | nonfunctional | bII/half_dim/cluster | pad → drone → strings → noise | 半音爬行、碎片 |
| horror 恐怖 | 44 | 40 | locrian | pedal | half_dim/dim7/tritone/cluster/bII | drone → organ → strings → timpani → bells → brass | 下行减五、管风琴平行五度 |
| urgency 紧迫 | 132 | 57 | phrygian | ostinato | power/sus4/bII | ostinato → bass → timpani → brass → noise | 三音锤击、级进爬升 |
| sorrow 悲伤 | 58 | 55 | minor | functional | add9/min7/sus2 | strings → pad → bass → bells → choir | 叹息、下行四/五度 |
| awe 敬畏 | 68 | 48 | lydian | pedal | add9/quartal/maj7/sharp11 | organ → choir → strings → bells → timpani → brass | 上行的纯四/五度堆叠 |
| aftermath 余韵 | 56 | 52 | dorian | pedal | sus2/min7/add9 | drone → pad → bells → strings | 回声式三音、远距离 |

规则：**每档至少三族动机**；同一档位内按段落轮换族，不允许整段只重复一句。

## 核心结构：五段 cue

```
bed 铺垫 → rise 上升 → climax 高潮 → void 抽空/静止 → tail 余韵
```

- 典型小节数 `8-6-6-3-7`（恐怖可 `6-6-6-5-4`，紧迫可 `4-6-6-2-4`）。
- **段落衔接**：新段落第一小节复用上一段最后一个和弦作为枢轴；或在整段维持 pedal point。
- **钩子**：`rise` 第 1 小节必须出现可识别的动机陈述（motif statement）；高潮用和弦化/加八度再现。
- **抽空**：`void` 至少 1 小节完全留白（不调度任何声部），让下一段的进入成为重音。
- **余韵**：`tail` 用增值的动机再现或只留下钟声/低音，动态衰减到 20% 以下。

## 动态与密度模板

| 段落 | 动态曲线（相对段落基准） | 声部密度 | 滤波截止 | 混响发送 |
| --- | --- | --- | --- | --- |
| bed | `0.55 → 1.00` 线性慢起 | 0.2–0.35 | 600 Hz | 0.9× |
| rise | `0.78 → 1.30` 渐强 | 0.4–0.6 | 860→1180 Hz | 0.85× |
| climax | `0.90–1.06`，允许轻微抖动 | 0.65–0.85 | 1380→1900 Hz | 0.62× |
| void | `0.95 → 0.06` 快速抽空 | 0.06–0.1 | 380 Hz | 1.15× |
| tail | `0.78 → 0.22` 衰减 | 0.2–0.3 | 680 Hz | 1.0× |

硬性要求：整条 cue 的 `max(dynamic) - min(dynamic) ≥ 0.3`；高潮峰值至少比铺垫峰值高 0.25。
**响度变化必须同时改变密度与音色**（声部进出、滤波、混响），不能只调总线增益。

## 动机与变形（代码内名称）

动机 = 音程序列 + 节奏序列 + 轮廓标签 + 纵向处理（单声/平行五度/和弦化）。

| 算子 | 作用 | 适用 |
| --- | --- | --- |
| `identity` | 原样陈述 | rise 开头钩子 |
| `transpose(n)` | 整体移调 | 循环轮换、避免同调 |
| `invert` | 以首音为轴倒影 | 上升段应答 |
| `retrograde` | 音序/节奏逆行 | 应答、余韵 |
| `augment` / `diminish` | 节奏×2 / ×0.5 | 高潮拉宽、过门加速 |
| `fragment` | 只保留前半 | 抽空段、留白前的残句 |
| `ornament` | 插入邻音装饰 | 日常、余韵 |
| `octave` | 奇偶音交替八度 | 高潮增厚 |
| `fifth` | 改平行五度（空和声） | 恐怖、敬畏 |
| `chordify` | 每个音变 power chord | 高潮锤击 |

**禁止**：随机取音、无动机纪律的「旋律」，或在同一段里反复原样陈述同一动机超过 2 次。

## 和声速查（详见 references）

- **半减七** `[0,3,6,10]`、**减七** `[0,3,6,9]`、**三全音** `[0,6,12]`、**音簇** `[0,1,2]`。
- **那不勒斯 bII**：根音 +1 的 major triad，用于 phrygian/locrian 的「突然转向」。
- **空和声**：并行五度/四度（`[0,7,12]`、`[0,5,10]`）刻意保留，制造中世纪/管风琴感。
- **pedal point**：低音固定在主音或导演指定音高，上层和弦自由移动，用不协和积累张力。
- **非功能**：和弦按色彩选择，不追求 V–I 解决；用 pedal 或共同音保持连贯。
- 禁忌：恐怖/不安段落不要出现明亮的完整大三和弦（除 bII 这一「假解决」）。

## 反模式（必须避免）

1. 每个小节都换和弦、但全程同一个 pad 音色 → 变成「和弦垫 + 节拍器」。
2. 只有总线音量变化，密度/音色不变 → 听起来像推子，不像配乐。
3. 动机每段都原样重复，或根本没有动机 → 没有记忆点。
4. 所有声部同时进入、同时退出 → 没有呼吸与层次。
5. 混响开满、所有声部同一条湿信号 → 糊成一片，失去打击乐与静止的对比。
6. 恐怖段落用大三和弦「安慰」听众 → 情绪泄气。
7. 高潮只靠加快 tempo，不做配器/和声升级 → 廉价。
8. 留白只把音量调到 5%，低频与混响尾巴还在 → 不是静止。
9. ostinato 所有层同相位、同音区、同力度 → 只是重复，不是卡农。
10. 旋律在每一层同时出现 → 声部打架，听感像随机噪声。

每条反模式的修复方式见 `references/anti-patterns.md`。

## 自动配乐（无 LLM 回退）

叙事 → 分析 → 本地作曲的链路不依赖大模型：

1. `analyzer`/`music.plan_music` 从文本得到 `profile`、`intensity`、`section`；
2. 若没有可用的 AI 导演乐谱，`backend/trpg_server/immersive/auto_score.py` 按
   `PROFILE_MODES` / `PROFILE_CHORDS` / `PROFILE_ARCS` / `PROFILE_MOTIFS` 生成四段式乐谱；
3. 前端 `music-arranger.ts` 把四段补成五段 cue（插入 void、补 tail），再执行动机轮换与动态自动化；
4. AI 导演存在且 `mood` 匹配时优先导演乐谱，自动乐谱只在缺失/失败时接管。

自动乐谱的硬约束：同 `(profile, intensity, section, seed)` 逐字节复现；和弦色彩只能取
`CHORD_QUALITIES`；配器只能取 `INSTRUMENTS`（≤6）；不得输出音频或外部素材。
完整决策树见 `references/composition-workflow.md`。

## 工程落地约定（本项目）

- 乐理/配方：`music-theory.ts`；cue 编排：`music-arranger.ts`；音色：`music-voices.ts`；调度/总线：`music-engine.ts`；
  无 LLM 自动配乐：`backend/trpg_server/immersive/auto_score.py`。
- 所有随机必须经 `streamRandom(seedBase, [cycle, section, bar, layer])`，同种子必须复现同一份谱。
- 段落动态由 `MusicBus.dynamicGain` 自动执行；交叉淡入由独立 `gain` 承担，两者不得混用同一个节点。
- 导演乐谱只允许新增**可选**字段（当前为 `pedal`、`motif_variants`），旧字段必须继续可用；
  非法乐谱必须回落到档位配方。
- 验证：`tests/test_immersive_music_engine.py` 会检查五段结构、动态非恒定、留白、动机轮换、种子复现。

## 参考资料与授权

本 Skill 只提炼**公开的音乐学概念与技法名称**（pedal point、ostinato、tintinnabuli、微复调、
平行五度、半减七、bII 等），不包含任何受版权保护的乐谱、音频或歌词，也不复制具体作品旋律。
调研参考（仅作为技法来源，不随项目分发素材）：

- Bernard Herrmann：pedal point、固定低音 ostinato、集群和弦与模块化 cue（`hitchcock.tv` 风格研究、MuseScore 恐怖配乐分析）。
- Hans Zimmer：分层 ostinato、三和弦循环、分阶段声部进入与角色分工（Interstellar 配乐分析、HAL 论文）。
- Arvo Pärt：tintinnabuli 的 M-voice / T-voice 双声写法（Arvo Pärt Centre）。
- György Ligeti：微复调——同一音高集合、不同节奏的卡农（Ligeti 官方网站与学术论文）。
- Debussy / Ravel：平行五度/八度、七九和弦、非功能色彩（音乐史教材与论文）。
- Jóhann Jóhannsson / Ryuichi Sakamoto：慢速管弦、电子处理与留白（乐评与配器分析）。
- Ennio Morricone：打击乐语法、合唱进入与悬置内声部（电影音乐分析）。
- 恐怖配乐通用语法：tritone、locrian、bII、cluster、silence as accent（Behind the Score 等公开分析）。

本项目动机全部为原创的音程/节奏组合；合成音色由 Web Audio 程序化生成。
