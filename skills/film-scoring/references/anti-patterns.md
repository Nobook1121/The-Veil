# 反模式清单：哪些做法听起来廉价

本文件是 `SKILL.md` 第「反模式」节的展开版。每条都给出：**听感症状 → 代码成因 → 修复配方 → 本项目落点**。
用法：审查引擎或导演乐谱时逐条对照；新增场景档位时先确认没有重新引入这些模式。

判定口径：如果一段 cue 拿掉音高信息、只剩包络与频谱，仍然「每 4 小节听起来一样」，基本就命中了下面某一条。

---

## AP-01 全程单音色的和弦垫

**症状**：每小节和弦都换了，但听觉上像同一个持续音在微微变形；段落之间只有音量差。

**成因**：所有段落共用同一个 `chordVoices` 数组，或只有 `pad` 一种声部承担和声；配器表形同虚设。

**修复配方**：

1. 按段落指定和声声部，同一段内最多 3 个声部，且相邻段至少换掉 1 个：
   - `bed`：pad / organ / drone（低频铺垫，不出旋律）
   - `rise`：strings / organ / choir（加入有起音过程的声部）
   - `climax`：strings / organ / brass（音色更硬、更亮）
   - `tail`：pad / choir / bells（回到柔软音色）
2. 和弦节奏必须随段落变化：`bed` 4 小节一次、`rise` 2 小节、`climax` 1 小节、`tail` 3 小节。和弦换得比声部换得勤，才会既有和声信息又有音色对比。
3. 和弦声部不是「一直响」：每个和弦只在小节内发声，靠 `dynamicGain` 与滤波器保持段落内的连续性。

**本项目落点**：`music-arranger.ts` 的 `chordVoicesFor()`、`harmonicRhythmFor()`；`music-voices.ts` 的 `Rack.pad/strings/organ/choir`。

---

## AP-02 只推总线音量，不改密度与音色

**症状**：渐强听起来像有人在转音量旋钮；高潮与铺垫的「质感」完全一样。

**成因**：动态曲线只写进一个 gain 节点，滤波截止、混响发送、声部数量、ostinato 层数在整个 cue 内是常数。

**修复配方**（四件事必须同时变化）：

| 维度 | 铺垫 | 上升 | 高潮 | 抽空 | 余韵 |
| --- | --- | --- | --- | --- | --- |
| 总线动态 | 0.55→1.00 | 0.78→1.30 | 0.90–1.06 | 0.95→0.06 | 0.78→0.22 |
| 低通截止 | 620 Hz | 860→1180 Hz | 1380→1900 Hz | 380 Hz | 680 Hz |
| 混响发送 | 0.9× | 0.85× | 0.62× | 1.15× | 1.0× |
| 声部数 / ostinato 层 | 1–2 / 0–1 | 2–3 / 1–2 | 3–4 / 3 | 0 / 0 | 1–2 / 1 |

高潮要**降低**混响发送（干声占比升高才显得「近」），抽空要**提高**混响（让残响暴露空间的空）。

**本项目落点**：`music-arranger.ts` 的 `dynamicCurve()`、`cutoffFor()`、`reverbFor()`、`ostinatoFor()`；`music-engine.ts` 的 `MusicBus.scheduleBar()` 分别把 `dynamicGain`、`filter.frequency`、`send.gain` 自动化。

**验收**：整条 cue 的 `max(dynamic)-min(dynamic) ≥ 0.3`，高潮峰值比铺垫峰值高 ≥ 0.25。

---

## AP-03 没有动机，或每段原样重复

**症状**：能感觉到「有旋律」，但听完记不住任何一句；或者同一句在 5 个段落里一字不差地出现。

**成因**：旋律由逐小节随机取音生成；或动机只做 `identity`，从不换族、从不移调。

**修复配方**：

1. 每档至少 3 族动机，每族 1–2 个 cell；cell 必须同时有音程序列、节奏序列、轮廓标签。
2. 段落角色固定为：`statement`（rise 第 1 小节，原样）→ `answer`（rise 中点，逆行或倒影 + 增值）→ `climax`（高潮第 1 小节，和弦化 + 八度）→ `fragment`（抽空前 / 余韵，只留前半）。
3. 同一族在同一段落内**最多原样陈述 2 次**；跨循环（cycle）必须换族，并按 `transpose(±1/±2 度)` 移调。
4. 余韵段只允许 `augment` 后的 fragment，不允许新的 statement。

**本项目落点**：`music-theory.ts` 的 `motifFamilies` 与 `transformMotif()`；`music-arranger.ts` 的 `pickFamily()`、`motifOpFor()`、`buildMotifPlan()`。

**验收**：`tests/test_immersive_music_engine.py` 检查每条 cue 动机出现 ≥3 次、至少 2 种角色；cycle 0 与 cycle 1 的动机集合不同。

---

## AP-04 所有声部同进同出

**症状**：每个段落开头像「总闸合上」，所有乐器一起出现；段落结束一起消失。没有乐器「提前退场」的呼吸感。

**成因**：声部集合按段落静态定义，没有小节级进入条件；ostinato 层数、铜管、定音鼓在同一小节开始。

**修复配方**：

1. ostinato 分层进入：`bed` 从第 3 小节起 1 层；`rise` 前半 1 层、后半 2 层；`climax` 3 层；`tail` 退回 1 层。
2. 铜管只在 `climax` 进入，且不覆盖整个高潮（用 `brass` 标志控制具体小节），`rise` 最多在末 1/3 试探性进入。
3. 定音鼓：`bed` 最后 1 小节才允许首次心跳；`tail` 只允许第 1 小节；`void` 完全禁止。
4. 弦乐/合唱在 `rise` 进入（有缓慢起音），不要在 `bed` 一开始就满编。

**本项目落点**：`ostinatoFor()` 的 `layers` 规则、`timpaniFor()` 的小节门槛、`BarPlan.brass`。

---

## AP-05 混响开满、全湿信号

**症状**：所有东西糊成一片；打击乐没有「击点」，留白也不安静，因为混响尾巴还在响。

**成因**：所有声部共用一条高发送的卷积混响；`void` 只降低增益，没有切断发送与低频。

**修复配方**：

1. 混响发送是**每小节自动化**的，并且高潮低于铺垫（0.62× vs 0.9×）。
2. 打击乐（timpani、noiseHit）走**干声为主**，发送量不超过弦乐的 1/3；不要让定音鼓也泡在混响里。
3. `void`：发送提升到 1.15× 以暴露空间，但声部数为 0；至少 1 小节 `silence` 完全不调度任何节点（连 drone 目标也要压到接近 0）。
4. 延迟只给动机/铃声（短 feedback、低通 2400 Hz），不给低频与和声垫。

**本项目落点**：`music-arranger.ts` 的 `reverbFor()` 与 `silence` 规则；`music-engine.ts` 的 `MusicBus` 里 `dynamicGain → send → convolver` 与 `delaySend → delay` 两条独立发送；`scheduleBar()` 在 `silence` 分支直接 return。

---

## AP-06 恐怖段落用大三和弦「安慰」

**症状**：气氛刚建立起来，一个明亮完整的 I 级大三和弦把紧张感泄掉；听感像儿童动画。

**成因**：调式是 locrian/phrygian，但和弦构建回落到默认 major triad；或导演乐谱给了 `[0,4,7]` 却未做色彩替换。

**修复配方**：

1. 恐怖/不安档位禁止出现完整的 `[0,4,7]`。可用：`half_dim [0,3,6,10]`、`dim7 [0,3,6,9]`、`tritone [0,6,12]`、`cluster [0,1,2]`、`min_maj7 [0,3,7,11]`。
2. 唯一允许的「明亮」是 **bII 那不勒斯**（根音 +1 的 major triad），作用是「假解决 / 突然转向」，用完必须回到不和协音区。
3. 导演乐谱若只给三和弦，前端要按档位色彩表替换，而不是照单全收（`pickColor` 在 `climax` 强制偏向硬色彩）。

**本项目落点**：`music-theory.ts` 的 `buildChord()` / `pickColor()` / `RECIPES.horror.colors`。

---

## AP-07 高潮只靠提速

**症状**：高潮段只是「变快 + 变响」，音色与和声没有升级，听感像把同一段素材按 1.2 倍速播放。

**成因**：强度调节只缩放 tempo；高潮段没有峰值和弦、没有铜管、没有和弦化动机。

**修复配方**（高潮必须满足全部四项）：

1. 和弦节奏从 2 小节收紧到 1 小节。
2. 高潮中点插入 `peakChord`，从 `bII / tritone / cluster / dim7 / half_dim` 里选（不要用普通 min7）。
3. 动机做 `chordify` + `octave`，由 `strings` 或 `brass` 承担，register +12。
4. ostinato 加到 3 层、register +12、gain 从 0.24 提到 0.34；铜管进入；timpani 加密到 4 击/小节。
5. tempo 提升上限 ≤ 7%（`high` 强度即为 1.07×），不要靠大幅提速制造高潮。

**本项目落点**：`buildSectionChords()` 的 `peakChord` 分支、`buildMotifPlan()` 的 `climax` 配置、`ostinatoFor()`、`INTENSITY_SPECS`。

---

## AP-08 假留白

**症状**：以为做了「静止」，实际听众仍听到低频嗡嗡与混响尾巴；下一段进入时没有「砸下来」的重音感。

**成因**：留白只把总线增益降到 5%；drone 目标未归零、发送未切断、已排度的音符继续发声。

**修复配方**：

1. `void` 段至少 1 个奇数小节标记 `silence = true`：该小节不创建任何新节点，`dynamicGain` 压到极低，drone 目标接近 0。
2. 留白的**前一拍**要有可听的内容（fragment 动机或一次 timpani），否则留白会被误认为播放故障。
3. 留白后的第一个和弦必须是枢轴和弦的「回归」或峰值和弦，让进入成为重音。
4. 不要把整段 `void` 全部静音；2 小节 void 里通常 1 小节有残响、1 小节真静。

**本项目落点**：`music-arranger.ts` 的 `silence = isVoid && bar >= 1 && bar % 2 === 1`；`music-engine.ts` 的 `scheduleBar()` silence 分支与 `droneTarget`。

---

## AP-09 伪卡农：ostinato 所有层同相同区同力度

**症状**：说是多层，听起来只是同一个 loop 被复制粘贴，音量更大而已。

**成因**：每层用相同 offsets、相同 gain、相同 register、相同起始拍；没有 canon offset。

**修复配方**：

1. 层与层之间至少错开 2–3 个 16 分音符（`canonOffset = 2 + random*2`）。
2. 每层的音区不同：低层 bass 区、中层原位、高层 +12。
3. 每层力度递减（0.34 → 0.24 → 0.16），高层最轻，避免糊成一坨。
4. 节奏型不要全是等分：用 `[1,0,1,1,0,1,1,0,...]` 这类含空拍的 16 步型，空拍本身就是节奏信息。
5. 强度 `low` 时最多 1 层；`high` 时 `rise` 至少 2 层——层数是可听辨的动态参数。

**本项目落点**：`ostinatoFor()` 的 `pattern`、`layers`、`canonOffset`、`register`；`music-engine.ts` 的 `playOstinato()` 逐层延迟与降增益。

---

## AP-10 声部打架：旋律在每一层同时出现

**症状**：动机、和声、ostinato 全在同一拍上重音，听感像随机噪声；听众找不到「主句」。

**成因**：motif 与 counter 同拍进入；和弦声部也带旋律；timpani 落在动机首音上。

**修复配方**：

1. 每条 cue 同一时刻只允许 **1 个主旋律声部**（`BarPlan.motif`），counter 必须错拍（`startBeat ≥ 1`）并降 12 度、降增益。
2. 和弦声部只做纵向和声，不写横向旋律；`pad/organ` 的音高变化跟和弦，不跟动机。
3. 打击乐避开动机首音：把 timpani 击点放在动机的弱拍或句尾。
4. 铜管与弦乐不要同时承担旋律：铜管做和弦化强调，弦乐做线条。
5. 高密度段落用「主旋律 + 和声 + 节奏」三层结构，不要四层都抢前景。

**本项目落点**：`buildMotifPlan()` 的 `startBeat`（answer=1、fragment=1.5）、`BarPlan.counter`、`timpaniFor()` 的击点表。

---

## 附：快速自检表

审查一条 cue 时依次问：

1. 段落是否有五种 kind，且至少各有 1 小节？
2. 相邻段落的第一个和弦是否共享（枢轴）或有 pedal 贯穿？
3. 动态极差 ≥ 0.3？高潮比铺垫峰值高 ≥ 0.25？
4. 至少 1 个 `silence` 小节？
5. 动机出现 ≥ 3 次、≥ 2 种角色、跨 cycle 换族？
6. ostinato 层数随段落变化，且有 canon offset？
7. 恐怖/不安段是否混入完整大三和弦？
8. 高潮是否同时具备峰值和弦、铜管、和弦化动机、加密打击乐？
9. 同 seed 两次生成是否逐字节相同？换 seed 是否不同？
10. 低性能档（quality 2）是否仍能听出段落结构（不要求音色完整）？

任何一项为否，先修该项，再谈音色微调。
