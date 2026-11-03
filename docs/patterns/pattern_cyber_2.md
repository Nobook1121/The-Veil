# Pattern Cyber 2

## 定位

`pattern_cyber_2` 是当前 COC7 调查员角色卡页面的设计规范，用于后续 AI 统一实现角色卡、规则面板、档案库、装备记录、日志审计和调查终端类界面。

整体气质是“密文档案终端 + 克苏鲁调查员控制台”：深色、克制、高信息密度、可操作性强。它不是营销页，不使用大 hero、不做装饰性光球，也不把页面拆成多层嵌套卡片。

## 视觉关键词

- 深青黑终端背景
- 荧光绿关键状态
- 冷蓝辅助光
- 细网格扫描层
- 半透明档案面板
- 紧凑规则数值卡
- 图标化命令按钮
- 低饱和文本层级
- 可审计日志列表
- 工具型、克制、可读

## 色彩规则

主背景使用深青黑，不使用纯黑。推荐：

```css
background:
  linear-gradient(135deg, rgba(8, 19, 25, 0.96), rgba(11, 35, 38, 0.94)),
  radial-gradient(circle at 85% 8%, rgba(37, 170, 121, 0.22), transparent 30%),
  radial-gradient(circle at 12% 88%, rgba(47, 111, 237, 0.18), transparent 34%);
```

核心颜色：

- 主强调色：`#66e2ad`，用于主按钮、激活态、关键标签、属性名、进度条起点。
- 强文本色：`#f3fff9`，用于关键数值。
- 主文本色：`#e9f7f0`，用于主要内容。
- 次文本色：`rgba(233, 247, 240, 0.62)` 到 `rgba(233, 247, 240, 0.72)`。
- 深底按钮文字：`#061714`。
- 辅助冷蓝：`#72a7ff`，只用于技能熟练度、进度条或辅助状态，不作为大面积主色。
- 装备负载警示：`#f0c36a`，只用于负载条末端或风险提示。

避免形成单一蓝紫渐变主题。页面应以深青黑和荧光绿为主，蓝色只做辅助。

## 背景纹理

使用细网格表达“调查终端/档案系统”，纹理只做氛围层，不压住内容。

```css
.character-matrix-bg {
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0.22;
  background-image:
    linear-gradient(rgba(101, 255, 190, 0.13) 1px, transparent 1px),
    linear-gradient(90deg, rgba(101, 255, 190, 0.13) 1px, transparent 1px);
  background-size: 28px 28px;
  mask-image: linear-gradient(to bottom, black, transparent 92%);
}
```

不要添加光球、bokeh、漂浮圆斑或大面积装饰渐变。

## 布局模式

角色卡页面采用“命令头部 + 左侧档案列表 + 右侧详情检查器”的工作台结构：

- 顶部命令区：页面短标识、标题、说明、导出和新建等主要操作。
- 左侧档案列表：角色列表、筛选器、角色状态摘要。
- 右侧详情检查器：规则计算、属性、技能、装备、背景、伤势日志、San 日志。
- 移动端降为单列，列表在上，详情在下。

推荐网格：

```css
.character-layout {
  display: grid;
  grid-template-columns: minmax(260px, 330px) minmax(0, 1fr);
  gap: 18px;
}
```

右侧详情区允许纵向滚动，左侧列表独立滚动。不要让整个页面因为内容过长而挤压聊天页或其他主布局。

## 面板规范

主要面板使用半透明深底、细边框和轻量阴影：

```css
border: 1px solid rgba(126, 232, 190, 0.22);
border-radius: var(--radius-md);
background: rgba(7, 18, 24, 0.74);
box-shadow: 0 18px 50px rgba(0, 0, 0, 0.22);
backdrop-filter: blur(14px);
```

规则：

- 页面级区域不做卡片嵌套。
- 重复实体可以用卡片，例如角色列表项、技能项、装备项、日志项。
- 卡片圆角保持 8px 或更小。
- 卡片 hover 可轻微上移，但不改变布局尺寸。

## 命令按钮

按钮应像“终端命令”，不是营销 CTA。优先使用 Font Awesome 或项目已有 icon。

主按钮：

```css
.character-command-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 38px;
  padding: 8px 14px;
  border: 1px solid rgba(103, 232, 173, 0.58);
  border-radius: var(--radius-sm);
  color: #061714;
  background: #66e2ad;
  box-shadow: 0 0 18px rgba(102, 226, 173, 0.26);
  transition: transform 0.18s ease, box-shadow 0.18s ease, background-color 0.18s ease;
}
```

次按钮使用透明深底、浅色文字和绿色细边框。hover 使用 `translateY(-2px)` 和轻微光晕，不使用弹跳。

## 信息层级

页面标题层级：

- kicker：小写或短大写风格，例如 `COC7 INVESTIGATOR NODE`，搭配终端 icon。
- 标题：不超过 2rem，用于当前工具名称。
- 副标题：说明页面职责，不写操作教程。

详情区标题使用图标 + 短标签，例如：

- 状态
- 基础属性
- 职业被动
- 技能系统
- 装备管理
- 背景信息
- 生命值/受伤记录
- San 值检定和损失记录

关键数值必须进入独立 stat card：

- HP
- San
- MOV
- 伤害加值
- 体格
- 总重量
- 总体积

## 角色卡特定组件

### 属性芯片

STR、CON、SIZ、DEX、INT、POW、AGE 使用等宽紧凑网格，属性名用荧光绿，数值用强文本色。

```css
.character-attribute-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(78px, 1fr));
  gap: 10px;
}
```

### 技能卡

技能以卡片展示，包含：

- 技能名称
- 技能等级：新手 / 学习 / 熟修 / 主修
- 属性分组：物理 / 神秘 / 学术 / 社交 / 战斗 / 生存
- 数值进度条

技能等级筛选使用小型 pill：

```css
.skill-rank-pill {
  min-height: 28px;
  padding: 4px 8px;
  font-size: 0.78rem;
}
```

active 状态必须有明显边框和背景变化。

### 装备负载

装备区显示总重量、总体积、装备列表和负载条。负载条使用绿到黄的线性渐变，表达负载接近上限。

```css
.equipment-load-meter span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #66e2ad, #f0c36a);
}
```

### 日志行

伤势日志与 San 日志按时间倒序展示。每条记录包含短标题和说明，不要用大段文本挤压规则信息。

## 表单与弹窗

角色编辑弹窗使用普通浅色表单系统，与站点其他配置弹窗保持一致：

- 表单分区使用图标 + 标题。
- 字段之间保留足够间距，不要紧贴。
- 属性输入使用 7 列网格，移动端改为 2 列。
- 技能和装备允许快速文本录入，格式示例放在 placeholder。
- 保存按钮放在 footer，取消和保存右对齐。

表单弹窗不要使用深色终端背景，避免输入区可读性下降。

### 世界意志终端（例外）

「世界意志」弹窗（`#worldOverrideModal`）是全站唯一允许使用深色终端表单的场景，因为它本身就是一条“改写指令”：

- 弹窗顶部固定一条 30px 的命令行标题条，文案 `OVERRIDE.EXE // ROOT ACCESS // NODE 07`，左侧三枚红/琥珀/青指示灯；窄屏缩短为 `OVERRIDE.EXE // NODE 07`。
- 标题区使用 `>` 提示符 + 光标块（唯一的持续动画，`prefers-reduced-motion` 时关闭）；标题文字用静态红/青错位阴影制造故障感，不做抖动。
- 正文提示以 `//` 开头；输入框使用 `--theme-font-family-mono`、磷光青 `caret-color` 与霓虹描边。
- 底部“执行”按钮为磷光青实心深色字，“取消”为暗底青边。
- 浅色配色（`body.theme-cyber-2.light-theme`）翻成纸质终端：纸白底、深翡翠边框、深色文字，避免浅色下文字发白。
- 扫描线是静态 `repeating-linear-gradient`，透明控制在 0.05 级别，不遮挡输入；整个界面不铺满屏动画。

## 动效规则

动效只服务反馈：

- 空状态 icon 可以使用慢速 pulse。
- 按钮和卡片 hover 使用 150-200ms 位移或边框变化。
- 不使用大面积旋转、弹跳、漂浮装饰。
- 所有动画必须支持 `prefers-reduced-motion`。

```css
@media (prefers-reduced-motion: reduce) {
  .character-command-button,
  .character-card,
  .character-empty-state .fa {
    animation: none;
    transition: none;
  }
}
```

## 可访问性

- 动态详情区使用 `aria-live="polite"`。
- 图标按钮必须有可见文字，或至少有 `title` / `aria-label`。
- 筛选项使用真实 `button`。
- 表单 label 必须绑定 `for` 和控件 `id`。
- 深色背景上的次级文本透明度不要低于约 60%。
- 交互元素必须保留 `focus-visible`。

## 响应式规则

在 1100px 以下：

- 主布局改单列。
- 装备摘要改单列。
- 列表和详情取消固定最大高度，交给页面自然滚动。

在 768px 以下：

- 顶部命令区纵向排列。
- 详情头部纵向排列。
- HP/San/MOV、属性、背景、表单属性输入使用 2 列。
- 按钮文字不能换出容器，必要时允许换行但保持图标对齐。

## 禁止项

- 不要使用大面积紫蓝渐变。
- 不要使用光球、漂浮圆斑、bokeh。
- 不要把页面区块做成多层嵌套卡片。
- 不要使用营销页 hero 或超大标题。
- 不要让动画影响布局尺寸。
- 不要让文本压进按钮、pill 或卡片边界。
- 不要在工具界面写大段使用说明。
- 不要用纯装饰性 SVG 替代真实 UI 结构。

## 适用场景

适合：

- COC 调查员角色卡
- 规则计算面板
- 装备与线索档案
- AI 角色配置终端

- 剧本节点审计
- 网络连接状态面板
- 日志和回档记录

不适合：

- 登录/注册页面
- 用户个人资料页面
- 营销落地页
- 轻松休闲小游戏
- 需要温暖生活化表达的社交页面

## 主题设置（v0.5.11b）

赛博档案的可调项由统一主题契约（`frontend/src/app/themes/contract.ts`）的 schema 生成，入口为「主题」标签卡（管理员站点默认）与个人设置「主题」分区。旧键 `trpg_cyber_fx_settings` 会自动迁移到 `trpg_theme_settings_v1`，并备份到 `trpg_theme_settings_legacy_backup`。

- **总开关**：`enabled` 关闭后数据雨与指针特效停摆，只保留静态底色。
- **氛围强度**：`intensity`（0.2–1），同时影响 WebGL 氛围层与扫描线浓度。
- **数据雨密度**：`rainDensity`（0.3–1），值越大列越密。
- **数据雨速度**：`rainSpeed`（0.5–2），按倍数缩放每滴下落速度。
- **下落字符集**：`charset` 五档预设——片假名（`katakana`）、程序符号（`symbols`）、十六进制（`hex`）、二进制（`binary`）、混合（`mixed`）。切换后清空字形精灵缓存并重建。
- **元素颜色**：`primaryColor`（主色 / 磷光青）、`backgroundColor`（页面底色）、`textColor`（正文文字）。
- **指针锁定框**：`reticle` 开关。

所有滑杆都有默认值、范围、步进；越界值由 `coerceValue` 钳制。优先级为字段默认值 < 站点默认 < 个人覆盖，管理员可在站点默认里用 `__locked` 锁定字段。
