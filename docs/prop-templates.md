# 道具工坊：用户模板扩展

道具模板支持三种来源：内置、插件声明、用户模板包。放好目录后，在道具编辑器里点模板旁的刷新按钮（站点管理员可见），或调用 `POST /api/props/templates/rescan`，无需重启服务。

- **内置**：`backend/trpg_server/props/templates/`，`source="builtin"`；
- **插件**：`data/plugins/<插件 id>/plugin.json` 里声明 `capabilities.prop_templates.dirs`，`source="plugin:<插件 id>"`，见 [plugin-development.md](plugin-development.md)；
- **用户模板包**：`data/props/templates/`，`source="user"`（本文档描述的就是这种轻量路径，不需要写 Python）。

想把模板打包分发给其他站点时，推荐改成纯数据插件：加一个 `plugin.json` 即可，模板文件结构完全不变。

> **重要：Story 不绘制 `background-image`。** 模板 `style.css` 或内联样式里的背景图不会出现在渲染结果中。纸纹/底纹请依赖渲染器的后置贴图，或在 `page.html` 里用 `assets/` 内的 `<img>` 叠加；不要把关键视觉寄托在 `background-image` 上。

## 目录位置

```
data/props/templates/<template_id>/
  manifest.json      # 必填
  page.html          # 必填
  style.css          # 必填
  assets/            # 可选，平铺的 png/jpg
```

`<template_id>` 必须与 `manifest.json` 的 `id` 一致，只能用小写字母、数字和 `-`，最长 64 字符。内置模板位于 `backend/trpg_server/props/templates/`，**不允许被用户模板覆盖**；同 id 时内置优先，用户模板会被标记为不可用。

## manifest 必填字段

```json
{
  "id": "my-note",
  "name": "我的便签",
  "fields": [
    {"key": "body", "label": "正文", "type": "textarea", "max": 600, "required": true}
  ]
}
```

- `fields` 至少一项；`key` 只能用小写字母、数字、下划线，且以字母开头。
- `type` 只接受 `text` / `textarea`；`max` 为 1–20000。
- 可选：`order`（数字，决定列表顺序）、`group` / `group_name`（界面分组）、`description`、`styles`、`wear_supported`、`page`、`ai_hints`。
- `styles.ink` / `styles.accent` 只允许 `#hex` 颜色；`styles.paper` / `styles.font` 只允许字母数字与 `_ -`。
- `wear_supported` 只能是 `stain` / `fold` / `burn` / `tear`。
- `page.size` 支持 `a3` / `a4` / `a5` / `a6` / `letter` / `legal`；`page.margin` 为 1–4 个 0–100 的数字。

## page.html 允许的内容

只允许文档流标签：

```
div p span h1 h2 h3 h4 table tr td ul ol li hr img strong em
```

- 属性只允许：`class style src width height colspan rowspan align alt`。
- 禁止 `<script>`、`<iframe>`、`on*` 事件属性、`javascript:`。
- `src` 与 CSS `url()` 只能引用 `assets/` 内的 png/jpg 文件名，不接受 `http(s)`、`//`、`data:` 或 `..` 路径。
- `style.css` 同样禁止 `@import`、`javascript:`、`expression()`、`-moz-binding` 和外链 `url(...)`。
- 不使用 flex / grid / absolute / float / column-count / border-image；并禁止 `position` / `transform` / `box-shadow` / `writing-mode` / `direction` / `top` / `right` / `bottom` / `left` / `z-index`（以 `props/template_validation.py` 的 `_DISALLOWED_CSS_PROPS` 为准）；布局用文档流与 table。

## 资源限制

- `assets/` 只能是平铺文件，不允许子目录或符号链接。
- 扩展名仅 `.png` / `.jpg` / `.jpeg`，并校验文件头确实是 PNG/JPEG。
- 单文件 ≤ 5 MB，总量 ≤ 20 MB，文件数 ≤ 24。
- `page.html` ≤ 512 KB，`style.css` ≤ 256 KB，`manifest.json` ≤ 128 KB。

## 失败与冲突时的表现

- 单个用户模板校验失败：只把该模板列入不可用，其他模板照常返回；`templates` 接口的 `unavailable` 数组会带 `id`、`source`、`reason`（服务端绝对路径不会下发）。
- 用户模板 id 与内置冲突：内置保留，用户模板进入 `unavailable`，原因写明「与内置模板 id 冲突」。
- 目录缺 `manifest.json`、`page.html` 或 `style.css`：该模板不可用，不影响列表。
- 重新扫描后模板缓存会清空；如果模板正在被编辑，编辑器会按新列表重新渲染选择器。

## 常见失败原因

| 现象 | 原因 |
| --- | --- |
| 模板不在列表里 | 目录名与 `manifest.id` 不一致，或缺 `manifest.json` |
| 提示标签不允许 | 用了 `br`、`section` 等白名单外标签，或写了 `<script>` |
| 图片不显示 | `src` 带了路径/外链，或文件不是有效 png/jpg |
| 提示 id 冲突 | 与内置模板同名；换一个 `id` |
| 修改后没生效 | 点「重新扫描」或重启服务 |
