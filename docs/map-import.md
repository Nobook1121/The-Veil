# 地图导入支持范围

veil-map 插件的地图导入按**文件内容**判断格式，扩展名只用于展示，不参与识别。
导入是原子的：解析、图片校验、体积/像素上限、墙体/光源数量上限全部通过后才会
写图片与场景状态；任何一步失败都不会留下半个场景。

## 支持的格式

### 1. 裸图片

PNG / JPG / WEBP / BMP。只导入背景图，保留房间现有墙体与光源，网格尺寸不变，
格线先回到图片中心，由用户确认标定。

### 2. Universal VTT（`.uvtt` / `.dd2vtt`）

Dungeondraft 等工具导出的单文件 JSON，至少包含：

- `image`：base64 内嵌地图图片；
- `resolution.pixels_per_grid`：每格像素（16–256）；
- `resolution.map_size` / `map_origin`：可选，用于自检与网格偏移；
- `walls[]`、`doors[]`：以“格”为单位的折线，导入为墙体与门/窗；
- `lights[]`：位置、半径、颜色、强度、阴影；
- `environment.ambient_light` / `baked_lighting`：环境光。

导入后会一并带出墙体、门与光源，并替换房间原有墙体/光源。

### 3. Foundry VTT 场景 JSON

支持 “Export Scene Data” 导出的场景 JSON，但**仅当背景图内嵌为
`data:image/...` base64 时**可用：

- 可导入：背景图、墙体（`walls[].c`）、门（`door` / `ds`）、光源（`lights[].config`）、
  网格（`grid.size` / `grid.offsetX/Y` / `grid.distance`）、环境光与黑暗度。
- 不导入：棋子、地形、音效、宏、外部图片路径。
- `background.src` 指向外部文件时不会去读取服务器或网络文件，会返回
  `map.import_image_missing` 并提示改用内嵌图片、UVTT 或裸图片。

## 上限

| 项目 | 上限 |
| --- | --- |
| 上传文件（含 base64） | 64 MB |
| 解码后的图片 | 40 MB |
| 图片单边 | 64–16384 px |
| 图片像素总量 | 1.2 亿 |
| 墙体/门 | 200 段 |
| 光源 | 100 个 |

## 常见错误码

| code | 含义 |
| --- | --- |
| `map.import_empty` | 上传文件为空 |
| `map.import_too_large` | 文件超过 64 MB |
| `map.import_unsupported` / `map.image_unsupported` | 不是受支持的图片 |
| `map.image_invalid` | 图片头损坏或内容无法识别 |
| `map.image_tiny` / `map.image_dimension` / `map.image_pixels` | 图片尺寸不合规 |
| `map.import_invalid_json` | JSON 解析失败 |
| `map.import_unknown_json` | JSON 结构不是 UVTT / Foundry |
| `map.import_uvtt_invalid` / `map.import_uvtt_grid` | UVTT 缺字段或每格像素异常 |
| `map.import_foundry_invalid` / `map.import_foundry_grid` | Foundry 场景缺字段或网格异常 |
| `map.import_image_missing` | Foundry 场景没有内嵌背景图 |
| `map.import_image_invalid` | 内嵌 base64 图片损坏 |
| `map.import_too_many_walls` / `map.import_too_many_lights` | 超过数量上限 |
| `map.import_failed` | 落盘失败，原场景未被修改 |

## 网格不自洽时的处理

导入不会“猜”网格。以下情况仍会完成导入，但响应里的 `import.needs_review`
为 `true`，并在 `import.warnings` 与 `calibration_hint.warnings` 中给出原因：

- 格式记录的 `map_size` / 场景尺寸与图片实际像素不一致；
- 图片宽高不是每格像素的整数倍；
- 墙体坐标远超图片范围；
- Foundry 场景使用非英尺单位。

前端会把这些提示显示在导入区域，并打开网格标定面板，由用户调整
`cells_across` 与偏移后保存。
