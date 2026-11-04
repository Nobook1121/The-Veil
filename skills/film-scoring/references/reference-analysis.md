# 大师配乐手法与工程转译

本文件是 `SKILL.md` 调研部分的来源与推导记录。只记录**可转成参数/规则的技法**，不做听感赏析；
不复制任何乐谱、音频或具体作品旋律。所有作品与录音版权归各自权利人。

## 1. Bernard Herrmann：小细胞 + pedal + ostinato

来源：

- [A Case Study of the Bernard Herrmann Style](https://hitchcock.tv/essays/herrmann/herrcase2.html)
- [Bernard Herrmann – IRCAM](https://ressources.ircam.fr/en/composer/bernard-herrmann/workcourse)
- [Comparing Herrmann's Psycho Score and Sinfonietta](https://filmmusicnotes.com/comparing-bernard-herrmanns-psycho-score-and-sinfonietta-1936/)

可工程化结论：

1. **动机可以短到两个音**（常是下行小二度）；不要强求长旋律。把 `MotifCell.degrees` 限制在 2–6 个音，反而更容易变形。
2. **pedal point 常由定音鼓/低音持续重复**，上层和声自由移动；`pedal` 用绝对音高类（0–11），恐怖档默认主音。
3. **ostinato 是独立声部**，不是和弦垫的节奏化；它必须有自己的音型、音区与增益，且可被切断（用于留白）。
4. **模块化 cue**：旋律模块（有稳定脉冲）与和声模块（暂停脉冲）交替；对应 `rise`（脉冲推进）与 `void`（抽空）。
5. **音色极端化**：弱音器、无揉弦、滑音、拨奏；程序化对应为「低通 + 低增益 + 短滑音 + 无 vibrato」等参数，而非换一种波形。

## 2. Hans Zimmer：分层 ostinato 与 Shepard 式持续上升

来源：

- [The Dark Knight OST: One note hysteria](https://jpaudiolab.wordpress.com/2019/02/22/the-dark-knight-ost-one-note-hysteria/)
- [How Hans Zimmer used science to soundtrack Dunkirk](https://faroutmagazine.co.uk/keeping-score-hans-zimmer-used-science-soundtrack-dunkirk/)
- [Interstellar 配乐分析（Érudit）](https://www.erudit.org/fr/revues/rmo/2018-v5-n2-rmo04134/1054149ar.pdf)

可工程化结论：

1. **ostinato 分层**：低层稳定脉冲、中层加密度、高层加音区；层数随段落 0→1→2→3→1。每层错开 2–3 个十六分音符（节奏卡农），并递减增益。
2. **持续上升错觉**：多个相差八度的音阶同时上行，高音淡出、低音淡入；没有多轨采样时，可用「低频 drone 缓慢上滑 + 高频重复音型 + 不改变根音」近似。**不要**真的无限升调，会失控。
3. **声部角色固定**：低音=时间感，打击=心跳，铜管=宣布，弦乐=持续压力；不要让所有声部都做旋律。
4. **动态不是线性推子**：Zimmer 的渐强常伴随密度增加与音色变硬；对应 `climax` 的低通提高、混响降低、铜管进入。

## 3. Arvo Pärt：tintinnabuli 双声

来源：

- [Arvo Pärt Centre – Für Alina](https://www.arvopart.ee/en/arvo-part/work/477/)
- [The Sound of Spirit](https://www.arvopart.ee/en/arvo-part/article/the-sound-of-spirit/)
- [Universal Edition – Tintinnabuli](https://www.universaledition.com/en/Contacts/Arvo-Paert/)

可工程化结论：

1. **双声结构**：一条自由旋律（M-voice）+ 一条只取主和弦音（T-voice）的声部；两声部不等长、不同节奏。
2. 工程实现：`motif` 走 M-voice；`counter` 走 T-voice——只使用主和弦的根/三/五音，节奏更慢。
3. **严格规则产生宁静**：T-voice 永远取「最近的和弦音」，不做装饰；对应余韵/敬畏档位，不要加随机 ornament。
4. **钟声化**：长音 + 非整数泛音（本项目 `bell` 用 1:2.76:5.4），比真实钟声更可控。

## 4. Ligeti：微复调

来源：公开音乐学对微复调（micropolyphony）的定义与 Ligeti 作品分析。

可工程化结论：

1. 多个声部使用**同一音高集合**，但各自节奏/相位不同；听感是「流动的织体」而非和声进行。
2. 工程实现：`ostinato` 的 3 层使用同一 `pattern`，但 `canonOffset` 不同、`register` 不同；`motif` 的 fragment 可与 counter 共用音级。
3. 禁止：微复调段不要同时放主旋律与铜管，否则会破坏「无中心」的织体。

## 5. Debussy / Ravel：平行进行与色彩和声

来源：

- [Impressionism in Music（Fiveable）](https://www.fiveable.me/history-of-music/unit-6/impressionism-music-debussy-ravel/study-guide/JbmauqDkVTFTnmpK)
- [Impressionism（Toby Rush）](https://tobyrush.com/theorypages/pdf/en-us/impressionism.pdf)
- [Quartal and Quintal Harmony（OERTX）](https://oertx.highered.texas.gov/courseware/lesson/2232/student/?section=28)

可工程化结论：

1. **planing（平行进行）**：整组和弦同向平移，故意保留平行五度/八度；用 `fifth`/`quartal` 声部实现，不按传统禁则修正。
2. **四度/五度和声**：`[0,5,10]`、`[0,7,12]` 可作「空」和声；适合敬畏、余韵、恐怖中的「非人」段落。
3. **非功能**：和弦按色彩连接，不要求 V–I；用共同音或 pedal 保持连贯。
4. **全音阶**：可用于「不确定/梦幻」段落；不要与强功能和声混用。

## 6. 恐怖配乐通用语法

来源：

- [Horror Scoring Techniques](https://spiritsofrebellion.com/horror-scoring-techniques-using-dissonance-silence-and-psycho-strings)
- [Creating Eerie Soundscapes](https://www.audioscene.org/article/creating-eerie-soundscapes-with-dissonant-chords-and-unusual-textures/)
- [Why Certain Music Feels Creepy](https://frameandfocal.com/photography-glossary/what-makes-music-creepy-more-music-theory-filmmakers-461911)

可工程化结论：

1. **tritone / 小二度 / 音簇**是核心不协和材料；恐怖档首和弦避免主音 major。
2. **locrian / phrygian** 提供天然的 bII 与减五；bII major 只作为「假解决」，随后回到不协和。
3. **silence 是重音**：留白前必须有一个可听事件（碎片动机/定音鼓），否则会被误认为故障。
4. **低频脉冲**：17–60 Hz 的 sub 不适合长时间满幅；用短脉冲 + 侧链，避免掩蔽人声与 UI 音。

## 7. 自适应游戏配乐（工程侧）

来源：

- [Best Practices for Adaptive Soundtracks](https://www.audioscene.org/article/best-practices-for-adaptive-soundtracks-in-open-world-games/)
- [A Tool to Implement Adaptive Audio Techniques on Unity Games](https://www.sbgames.org/proceedings2021/ComputacaoFull/218081.pdf)
- [Creating Narrative-Driven Interactive Music](https://www.audioscene.org/article/creating-narrative-driven-interactive-music-for-video-game-soundtracks/)

可工程化结论：

1. **vertical layering**（纵向分层）：同一时间轴上的 stems 按事件淡入淡出；对应 `ostinato.layers`、`brass`、`timpani` 的段落开关。
2. **horizontal resequencing**（横向重排）：段落播完再决定下一段；对应五段 cue 的 `arc` 轮换。
3. **seamless transition**：切换点放在和弦共享/pedal 上，避免硬切；对应枢轴和弦。
4. **优先级混音**：叙事/语音 > 实时配乐 > BGM；对应 `duck_bgm` 与 `dynamicGain` 分离。

## 8. 已有配乐 Skill 的取舍

检索到的现成 skill 主要分两类：

| Skill | 做法 | 是否采用 |
| --- | --- | --- |
| Alterlab Genai Soundtrack Composer | 按情绪标签选择配器/和声，但最终调用生成模型 | 情绪→配器映射可参考；**不采用**生成模型 |
| create-score（ACE-Step） | 每场景调用 Docker 化音乐生成服务 | **不采用**，引入外部模型与许可风险 |
| ElevenLabs Music Skill | 调用 Music API 生成音轨 | **不采用**，同上 |

本项目的约束是「全部程序化合成 + 原创动机 + 不引入外部音乐生成模型」，因此只吸收它们的
**场景分类与情绪标签体系**，音频始终由 `music-theory/arranger/voices/engine` 现场合成。

## 9. 来源使用声明

- 上述链接仅用于理解**公开技法与音乐学概念**；本项目不复制其中的乐谱、音频、歌词或具体旋律。
- 引用的作品名称仅用于说明技法来源，不随项目分发任何素材。
- 若未来引入第三方采样、音源库或生成服务，必须在 `THIRD-PARTY-LICENSES.md` 登记许可证并确认商用授权。
