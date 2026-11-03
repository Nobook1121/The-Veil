# 克苏鲁档案主题设计指南

## 主题概述

**克苏鲁档案（Cthulhu Archive）** 是为 CoC7 跑团平台新增的第五款主题，设计目标是营造调查员深入黑暗真相的既视感，让玩家在视觉层面感受到克苏鲁式恐怖的压抑感与诡异氛围。

---

## 设计理念

### 色彩体系

| 角色 | 色值 | 用途 |
|------|------|------|
| **主背景** | `#1a1c2e → #0f3460` 梯度 | 深紫蓝纵深感，暗示无尽深渊 |
| **暗金** | `#d4a574` | CTA 按钮、强调、骰桌边框 |
| **祭祀紫** | `#a78bfa` | 次级强调、边框发光 |
| **血红** | `#f16365` | 危险/疯狂/伤害提示 |
| **幽灵白** | `#e8e9f0` | 主文本，低透明度营造雾感 |
| **沉寂蓝** | `#7db5e8` | 聊天气泡、链接、网格 |

### 核心特效

1. **微妙网格背景**
   - 32×32px 网格，仅 8% 透明度
   - 蓝色网格线，上方渐隐
   - 营造"调查档案"的质感

2. **聊天消息光晕**
   - 玩家消息：蓝光边框 + 上方渐变光
   - KP 消息：紫光内侵光效
   - 骰子消息：暗金色边框与内光

3. **动画反馈**
   - 按钮悬停时有光线扫过效果（`::before` 伪元素左右移动）
   - 卡片悬停时增加紫光晕与深阴影
   - 骰子总数显示时有暗金文字阴影

4. **毛玻璃效果**
   - 模态框和面板使用 `backdrop-filter: blur(8-12px)`
   - 营造"透过神秘薄纱观看" 的视觉隐喻

---

## 实施细节

### 文件变更

#### 1. **样式定义** (`frontend/src/styles/00-theme-tokens.css`)
   - 新增 `body.theme-coc` 选择器，包含 50+ CSS 变量
   - 网格背景层 (`body.theme-coc::before`)
   - 骰桌与卡片质感增强

#### 2. **聊天消息** (`frontend/src/styles/01-foundation-chat.css`)
   - 玩家、KP、骰子消息的光晕与边框
   - 透明背景以显示网格

#### 3. **骰桌** (`frontend/src/styles/05-dice-animation.css`)
   - 暗金边框与梯度背景
   - 骰子总数的文字阴影
   - 检定结果（成功/失败/大成功）的彩色边框

#### 4. **工具面板** (`frontend/src/styles/03-tools-settings-platform.css`)
   - 面板悬停时的动态阴影与紫光
   - 活跃标签页的暗金色与文字发光

#### 5. **前端逻辑** (`frontend/src/app/config/ConfigManager.ts`)
   - `applyTheme()` 方法中添加 `pattern_coc` 处理
   - 主题切换时平滑过渡

#### 6. **HTML 选项** (`frontend/src/index/fragments/03-room-tools-auth-settings.html`)
   - 两个主题选择器中各添加 `<option value="pattern_coc">`

#### 7. **国际化** 
   - 中文：`profile.theme.coc: "克苏鲁档案"`
   - 英文：`profile.theme.coc: "Cthulhu archive"`

---

## 视觉效果清单

### ✓ 已实现

- [x] 深紫蓝渐变背景
- [x] 微妙网格动画（顶部渐隐）
- [x] 聊天消息光晕（蓝/紫/金）
- [x] 暗金色 CTA 按钮与悬停扫光
- [x] 骰桌暗金边框与梯度
- [x] 骰子结果彩色标签（成功/失败/大成功）
- [x] 模态框毛玻璃效果
- [x] 侧栏深沉配色
- [x] 活跃标签页发光效果
- [x] 平滑主题切换过渡

### 🎨 视觉特征总结

**第一印象**
- 深邃、神秘、压抑
- 类似 CoC 调查笔记本的色调
- 不失现代感的克制设计

**交互感受**
- 悬停时有"发现端倪"的光效反馈
- 按钮扫光表现出"翻开秘密档案"的仪式感
- 骰子光晕暗示"命运的宣判"

---

## 使用方式

### 玩家端

1. 打开设置（右上角齿轮图标）
2. 找到 **"网站主题切换"** 或 **"默认主题"**
3. 从下拉菜单选择 **"克苏鲁档案"**
4. 页面自动应用新主题（0.4 秒平滑过渡）

### 管理员端

在 `data/config/general.toml` 中修改：

```toml
[appearance]
theme = "pattern_coc"
```

---

## 兼容性

- ✓ 所有现代浏览器（Chrome、Firefox、Safari、Edge）
- ✓ 移动端响应式设计
- ✓ 无障碍支持（`prefers-reduced-motion` 时禁用网格）
- ✓ 与现有 Bootstrap 5 / 第三方库完全兼容

---

## 世界意志界面（v0.5.7b）

「世界意志」是玩家直接改写叙事结果的入口，弹窗由 `createPortal` 挂到 `document.body`。深渊主题在这里叠加一层仪式化法阵：

- **法阵层次**：外层符文环（22px 符文 + 细深色描边保证可读）、436 刻度环、中层 {7/3} 七芒星、内层符文环；外扩 `world-override-sigil-aura`（blur 20px）与 `world-override-sigil-halo`（blur 8px）负责环带自发光，二者都是静态模糊、只栅格化一次。
- **层级**：遮罩 `z-index: 1040`（`08-theme-abyss-components.css`）→ 法阵层 1049（`06-combat-world-override.css`）→ Bootstrap 弹窗 1050+。法阵 `pointer-events: none`，永远不会挡住弹窗内容与输入框。
- **遮罩**：半透明 + `backdrop-filter: blur()`，让背后的深渊场景隐约可见；不支持 `backdrop-filter` 时通过 `@supports` 回退到更实的暗色遮罩。
- **可配置项**：世界意志遮罩不透明度（25%–95%，默认 55%）与世界意志背景模糊（0–24px，默认 10px）。值写入 `--theme-veil-alpha` / `--theme-veil-blur`。自 v0.5.11b 起，这两项已并入统一的「主题」设置面板（管理员与个人共用），旧键 `trpg_abyss_fx_settings` 会在首次加载时迁移到 `trpg_theme_settings_v1` 并保留备份。
- **性能**：法阵只出现在 `body.theme-coc`；其它主题不创建法阵层。遮罩出现时背景 Canvas 的 `overlayCovered()` 会跳过整帧绘制；三层刻线只做 transform 旋转，柔光层静态缓存；模态关闭后立即移除法阵 DOM。

## 主题设置（v0.5.11b）

深渊帷幕的所有可调项现在都由统一主题契约（`frontend/src/app/themes/contract.ts`）的 schema 生成，设置入口在「主题」标签卡（管理员）与个人设置「主题」分区，不再有独立的「深渊特效」面板。

- **聊天 / 侧栏**：聊天面板不透明度（`chatAlpha`）、左侧栏不透明度（`sidebarAlpha`）、右侧栏不透明度（`railAlpha`）。
- **世界意志**：遮罩不透明度（`veilAlpha`）、背景模糊（`veilBlur`）。
- **拖尾**：拖尾开关（`trail`）、拖尾强度（`trailStrength`）、拖尾密度（`trailDensity`，0.2–1.5）。
- **触手**：密度（`tentacleDensity`，0.4–1.4）、大小（`tentacleSize`，0.7–1.3，步进 0.05，默认 1）、高度（`tentacleHeight`，0.6–1.4）、高度随机性（`tentacleHeightVariance`，0–0.5，默认 0.2）、摆动速度（`swaySpeed`，0.4–1.6）。
- **法阵**：大小（`sigilScale`，0.7–1.3）。
- **飘浮符文**：开关（`driftRunes`）。

### 触手大小（v0.5.11c）

- 只缩放整根触手的**粗细 / 体量**（根部半宽 `widthRatio`），不改长度、不改密度、不改摆动相位，因此与「触手高度」「触手密度」「摆动速度」彼此独立。
- 默认值 1 == 既有关闭参数时的观感，升级后画面与之前一致。
- 渲染端对最终宽度再做一次保护钳制：`MIN_WIDTH_RATIO = 0.048`、`MAX_WIDTH_RATIO = 0.175`。以短边 1080px 计，最小根部直径约 52px（细但可见），最大约 189px；配合最多 9 根底部触手仍能留出中央法阵与聊天区。
- 旧配置缺少 `tentacleSize` 时按默认值 1 处理，不报错。

### 高度随机性语义（v0.5.11c）

- 作用维度：**只作用于高度**（`lengthRatio`），不再由宽度、摆动相位或 seed 分支间接扰动高度。
- 每个位置类型有固定基准高度（`BASE_HEIGHT`：底部内圈 0.57、底部外圈 0.62、侧肢 0.49–0.78、顶部 0.28–0.32）。随机扰动为 `rand(-1, 1) * variance`，乘在基准高度上。
- 方差为 0 时扰动严格为 0：同一排底部触手完全平齐；侧肢 / 顶部仍按各自基准排列，不会出现「设 0 还有抖动」的残留。
- 随机数由固定种子 `createSessionRandom(0x7e11)` 驱动，同一组参数下重建结果可复现；无论方差取多少，每次都会消耗一次随机数，所以调节方差不会改变其他维度的随机序列。
- 三档观感（默认高度 1、底部宽屏）：
  - `0`：底部一排触手高度完全一致，像一道整齐的帷幕边；
  - `0.2`（默认）：相邻触手有轻微高低差，保留参差但整体仍有秩序；
  - `0.5`（最大）：相邻触手高差最明显，出现明显的错落与「被撕开」的边缘。
- 旧配置里保存的 `tentacleHeightVariance` 数值按原值直接沿用（不做归一化），但含义从「叠加在随机长度上」改为「叠加在固定基准高度上」，因此同样的数值观感会比旧版更规整；若不希望观感变化，可把该值调高约 0.05–0.1。

优先级：字段默认值 < 站点默认（管理员）< 个人覆盖；管理员可用 `__locked` 锁定任一项，个人面板会显示「管理员锁定」且控件禁用。所有滑杆都有默认值、范围与步进，越界值由 `coerceValue` 钳制；拖动预览用 `requestAnimationFrame` 节流。

## 兼容与迁移（v0.5.11b）

- 新存储键：`localStorage["trpg_theme_settings_v1"]`，结构为 `{ version, theme, values: { [themeId]: {...} } }`。
- 首次加载时读取 `trpg_abyss_fx_settings` 与 `trpg_cyber_fx_settings`，只迁移当前 schema 认识的显式键，写入新键；原值备份到 `localStorage["trpg_theme_settings_legacy_backup"]` 后再删除旧键。
- 回退策略：如需恢复旧面板数据，可从备份键读回并手动写回旧键；新键损坏时删除即可回落到字段默认值与站点默认。

---

## 后续增强方向

1. **可选元素**
   - KP 消息增加"古老羊皮纸"的背景纹理
   - 骰桌添加 SVG 雕刻装饰边框
   - 侧栏聊天列表增加竖向发光分隔线

2. **动画升级**
   - 网格背景偶发"闪烁"效果（低概率事件）
   - 新消息到达时的紫光脉冲
   - KP 输入状态的诡异"符号生成"动画

3. **主题配置**
   - 在设置中开放"网格密度"、"光晕强度"调整
   - 用户自定义暗金色和祭祀紫

---

## 测试清单

在发布前，请在以下场景验证：

- [ ] 浅色/深色/赛博/羊皮纸/克苏鲁主题切换无误
- [ ] 玩家消息、KP 消息、骰子消息显示正确
- [ ] 按钮悬停/点击效果流畅
- [ ] 模态框打开/关闭没有闪烁
- [ ] 移动端（iPhone/Android）显示清晰
- [ ] 禁用动画设置下网格隐藏
- [ ] 国际化文案正确显示（中英文）
- [ ] 主题锁定 (env/CLI) 时选择器禁用

---

## 文件清单

**新增/修改文件**（共 7 个）

```
frontend/src/styles/
  ├─ 00-theme-tokens.css          ↑ 新增 300+ 行（身体部分，由于大小限制）
  ├─ 01-foundation-chat.css       ↑ 新增 ~40 行
  ├─ 03-tools-settings-platform.css ↑ 新增 ~50 行
  └─ 05-dice-animation.css        ↑ 新增 ~70 行

frontend/src/
  ├─ index/fragments/
  │  └─ 03-room-tools-auth-settings.html ↑ 修改 2 处（添加 option 元素）
  ├─ app/config/
  │  └─ ConfigManager.ts          ↑ 修改 1 处（applyTheme 方法）
  └─ locales/
     ├─ zh_cn.json               ↑ 新增 1 行翻译
     └─ en_us.json               ↑ 新增 1 行翻译
```

**构建命令**

```bash
npm run build:frontend
```

---

## 许可证

本主题设计基于项目 Apache-2.0 许可证。所有色彩值、动画参数均为原创设计。
