# 作曲流程与自动配乐决策树

本文件回答两个问题：

1. **接到一个场景，怎么从零写出一段配乐？**
2. **没有人工/AI 导演时，程序如何从叙事文本自动选档位、段落、和弦与动机？**

所有步骤都以「可落到参数」为准，不写听感散文。

---

## 一、人工作曲流程（7 步）

| 步骤 | 产出 | 判断标准 | 失败信号 |
| --- | --- | --- | --- |
| 1. 场景功能 | 这段音乐要「推」还是「压」？ | 推 = 让玩家行动/恐惧；压 = 让玩家停顿/观察 | 两种都想要 → 说明场景本身没定调，先问 KP |
| 2. 情绪档位 | 从 8 档选 1 个主档 + 1 个副档 | 主档决定调式/配器；副档只影响 1–2 个段落 | 一上来选 3 档 → 音乐会失焦 |
| 3. 张力曲线 | 五段各几小节 | 至少 1 个高潮、1 个真留白、1 个余韵 | 全程平均 → 没有 cue，只有 loop |
| 4. 调式与 pedal | 调式 + 主音 + pedal 音 | 恐怖/不安用 phrygian/locrian；敬畏用 lydian；余韵用 dorian | 所有档位都用 major/minor → 色彩扁平 |
| 5. 和弦进行 | 4–8 个和弦 + 每段色彩 | 段落共享枢轴和弦；高潮至少 1 个硬色彩 | 每段全新进行 → 断裂、像换歌 |
| 6. 动机 | 3 族 cell + 角色分配 | 陈述/应答/高潮/碎片各就位 | 只有一句 loop → 没有发展 |
| 7. 配器与混音 | 每段声部、进入时机、动态/滤波/混响 | 声部随段进出；高潮更干更亮；留白真静 | 全段同一套音色 → 和弦垫 |

### 段落长度速查

| 档位 | bed | rise | climax | void | tail |
| --- | --- | --- | --- | --- | --- |
| daily / sorrow | 8 | 6 | 5 | 2 | 7 |
| suspense / awe | 7 | 6 | 5 | 3 | 6 |
| unease / aftermath | 6 | 6 | 5 | 4 | 5 |
| horror | 6 | 6 | 6 | 4 | 5 |
| urgency | 4 | 6 | 6 | 2 | 4 |

> `bars` 是基准值。第二次循环可换成「慢燃」（bed +2、tail +2）或「突变」（bed 压到 3、rise +2），避免每次同一形状。

---

## 二、场景 → 档位的自动映射

自动配乐不靠「感觉」，靠可解释的文本特征。按以下顺序打分，取最高档：

| 信号 | 关键词/特征 | 档位 | 强度 |
| --- | --- | --- | --- |
| 直接威胁、追击、倒计时、受伤 | 追、逃、跑、砍、开枪、倒计时、抓住、撕裂 | urgency | high |
| 不可名状的存在、尸体、仪式、低语、深渊 | 尸体、血、触手、仪式、低语、深渊、非人 | horror | high |
| 异常但未确认 | 影子、脚步、门缝、湿痕、冷风、失踪 | suspense | medium |
| 环境腐化、精神压迫、缓慢恶化 | 腐臭、发霉、墙皮、头痛、失眠、幻觉 | unease | medium |
| 安全屋、日常调查、城镇 | 旅店、酒馆、白天、集市、整理、休息 | daily | low/medium |
| 失去、悼念、告别、回忆 | 埋葬、遗物、信、照片、墓、告别 | sorrow | medium |
| 宏大揭示、神话尺度、星空/深海 | 巨构、星空、深海、神祇、纪元、光 | awe | high |
| 事件结束、废墟、余波 | 之后、废墟、安静、灰烬、黎明 | aftermath | low |

规则：

1. 同一段文本同时命中多档时，**威胁 > 恐怖 > 悬念 > 不安 > 日常**；哀悼与敬畏不参与优先级竞争，只在没有威胁时生效。
2. 强度由「动词强度 + 句长 + 感叹/短句比例」决定：短句 + 强动词 = high；长描写 = medium/low。
3. 连续两回合命中同一档才允许切换（滞回），除非强度跨两级或进入 horror/urgency。
4. 若文本没有明显信号，保持当前档位，推进段落而不是换档。

---

## 三、自动乐谱生成（无 LLM 回退）

实现位置：`backend/trpg_server/immersive/auto_score.py`。输入为叙事分析结果，输出严格符合 `director_schema.validate_score`。

```
profile + intensity + section + seed
        │
        ├─ 调式：PROFILE_MODES[profile]
        ├─ tempo：PROFILE_TEMPO[profile] × 强度系数（0.92 / 1.0 / 1.07）
        ├─ 和弦：PROFILE_CHORDS[profile] 轮换序位，root = (base + offset) % 12
        ├─ 段落：PROFILE_ARCS[(section + hash) % 3] → 四段（前端补 void）
        ├─ 动机：PROFILE_MOTIFS[profile] 三族（statement / answer / fragment）
        └─ pedal：恐怖/悬念/不安/敬畏固定主音，其余档位省略
```

硬规则：

- **确定性**：同一 `(profile, intensity, section, seed)` 必须产生逐字节相同的 JSON。
- **可演奏**：和弦 `intervals` 只使用 `CHORD_QUALITIES` 中已登记的色彩；`instrumentation` 只能是 `INSTRUMENTS` 的子集（≤6）。
- **不越权**：自动乐谱只给参数，不生成音频；前端仍按五段 cue 补留白、轮换动机、执行动态自动化。
- **不抢占导演**：AI 导演存在且 `mood` 匹配时优先使用导演乐谱；自动乐谱只在其缺失/失败时接管。

### 四段 → 五段的映射

自动乐谱输出 4 段（低/中/高/低），前端 `directorArc` 会：

1. 按 `dynamics` 把段落标为 bed / rise / climax / tail；
2. 若无 climax，把最响的非首段提升为 climax；
3. 若以 climax 收尾，补 2 小节 tail；
4. 在高潮后插入 2 小节 void，并保证其中至少 1 小节真静音。

---

## 四、和弦与动机选择的硬规则

### 和弦

| 档位 | 允许 | 禁止 |
| --- | --- | --- |
| horror / unease | half_dim、dim7、tritone、cluster、min_maj7、bII | 主音上的完整 major |
| urgency | minor、tritone、half_dim、dim7、power | 长段 maj7/add9 的「安慰」 |
| suspense | min7、half_dim、sus2、quartal、tritone | 明亮 maj7 连续 |
| sorrow | minor、min7、sus2、add9 | 减七/音簇（会变成恐怖） |
| awe | maj7、add9、quartal、sharp11、sus4 | 密集减七、音簇 |
| daily | add9、sus2、maj7、mixolydian IV | 连续 tritone/cluster |

### 动机

| 角色 | 出现位置 | 处理 | 增益 |
| --- | --- | --- | --- |
| statement | rise 第 1 小节 | identity，register 0 | 0.22–0.28 |
| answer | rise 中点 | invert/retrograde + augment | 0.18–0.24 |
| climax | climax 第 1 小节 | chordify + octave，register +12 | 0.24–0.32 |
| fragment | void 前 / tail | 只留前半，register +12，弱 | 0.14–0.2 |

禁止：同一段原样陈述同一族超过 2 次；余韵段出现新的 statement；随机取音。

---

## 五、自动配乐验收清单

- [ ] 8 档 × 3 强度 × 至少 2 个 section 都能通过 `validate_score`
- [ ] 同输入两次生成 JSON 完全相同；换 `seed` 或 `section` 必须不同
- [ ] 恐怖档首和弦不是主音 major
- [ ] 每个乐谱有 4 段、动态极差 ≥ 0.5、≥3 个动机变体
- [ ] 有 pedal 的档位 pedal 在 0–11；无 pedal 的档位不输出该字段
- [ ] `plan_music` 在无导演时返回 `score`；导演乐谱存在时导演优先
- [ ] 前端收到乐谱后仍能生成 5 段 timeline，且至少 1 小节 silence
