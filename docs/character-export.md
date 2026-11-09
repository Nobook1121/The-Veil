# 角色卡导出与导入

角色卡支持导出为 **JSON 文件** 或 **可直接复制分享的文本串**，也支持从这两者导入为一张新角色卡。导出与导入共用同一套字段定义与校验（`backend/trpg_server/character_card_io.py`），避免两边字段漂移。

## 1. 入口与交互

- 入口：角色卡页顶部工具栏的「导出」「导入」按钮。
- 导出弹窗：
  - **下载 JSON 文件**：下载一份 UTF-8、2 空格缩进的完整角色卡 JSON。
  - **复制分享文本串**：把同一份 JSON 做 zlib 压缩再 base64 编码，写入剪贴板。
- 导入弹窗：
  - **选择 JSON 文件**：可多选，逐个导入。
  - **粘贴分享文本串 / JSON**：文本框内容交给后端自动识别。
- 所有结果提示走项目既有 `notify()` / 通知组件，不使用原生 `alert` / `confirm` / `prompt`。

## 2. 权限

| 操作 | 权限 |
| --- | --- |
| 导出 | 角色卡本人，或具备 `characters.manage_all` 的管理员 |
| 导入 | 已登录且具备创建角色卡权限；受站点「普通用户角色卡数量上限」约束 |

接口：

- `GET /api/characters/<character_id>/export`：未登录 401、无权 403、不存在 404。
- `POST /api/characters/import`：未登录 401，请求体不合法 400，超限 413。

## 3. 数据来源

按角色卡 ID 读取自己的目录，兼容旧版平铺文件：

| 文件 | 用途 |
| --- | --- |
| `data/runtime/characters/<id>/character.json` | 角色卡主体 |
| `data/runtime/characters/<id>/weapons.json` | 自定义武器库 |
| `data/runtime/characters/<id>/avatar.*` | 头像（png/jpg/jpeg/gif/webp） |
| `data/runtime/characters/<id>.json` | 旧版平铺角色卡（兼容读取） |

## 4. 导出 JSON 结构

导出的 JSON 顶层就是完整的角色卡对象（与前端运行时角色卡模型一致，字段使用 camelCase），并额外附带两个导出专用字段：

```json
{
  "id": "investigator-...",
  "rulesetId": "coc7",
  "name": "调查员姓名",
  "playerId": "8",
  "playerName": "",
  "era": "1920s",
  "gender": "",
  "age": 25,
  "occupationName": "记者",
  "residence": "",
  "birthplace": "",
  "avatar": "/data/characters/<id>/avatar.png",
  "attributes": { "STR": 50, "DEX": 50, "CON": 50, "APP": 50, "POW": 50, "SIZ": 50, "EDU": 50, "INT": 50, "LUC": 50, "AGE": 25 },
  "maxHp": 10,
  "currentHp": 9,
  "maxSan": 50,
  "initialSan": 50,
  "currentSan": 40,
  "maxMp": 10,
  "currentMp": 10,
  "magicPoints": 10,
  "damageBonus": "0",
  "build": 0,
  "mov": 8,
  "armor": 0,
  "status": {},
  "skills": [],
  "weapons": [],
  "equipment": [],
  "assets": {},
  "background": {},
  "relationships": [],
  "experiencedScenarios": [],
  "createdAt": "2026-11-01T00:00:00.000Z",
  "updatedAt": "2026-11-02T00:00:00.000Z",
  "customWeapons": [],
  "avatarData": "data:image/png;base64,..."
}
```

| 字段 | 说明 |
| --- | --- |
| `attributes` / `skills` / `weapons` / `equipment` | 角色卡主体数值与物品，完整导出 |
| `maxHp` / `currentHp` / `maxSan` / `initialSan` / `currentSan` / `maxMp` / `currentMp` | 角色卡自身持久化的生命、理智、魔法值 |
| `status` | 重伤、昏迷、死亡、疯狂等状态标记 |
| `background` / `assets` / `relationships` / `experiencedScenarios` | 背景故事、资产、人际关系、经历模组 |
| `avatar` | 卡片上的头像访问路径（本实例内有效） |
| `avatarData` | 头像文件内容内嵌为 data URI；没有头像时为空字符串 `""` |
| `customWeapons` | 角色卡目录下 `weapons.json` 的完整自定义武器库；没有时为空数组 `[]` |
| `playerId` / `playerName` | 角色卡的归属信息（本实例内的用户标识） |
| `rulesetId` | 角色卡使用的规则集标识 |
| `createdAt` / `updatedAt` | 角色卡创建 / 更新时间 |

职业点数、个人兴趣点、技能成功上限等派生值不单独存储，前端会根据 `attributes`、`occupationName` 与技能数据重新推导，因此不重复导出。

下载的 JSON 文件与分享文本串解出来的内容完全一致（同一份 `jsonText`）。

## 5. 分享文本串格式

```text
base64(zlib.compress(json_text.encode("utf-8"), 9))
```

- 裸串：不加 JSON 外壳、不加自定义前缀，标准工具可直接解压。
- 典型形态以 `eN` 开头。
- 解码示例：

Python：

```python
import base64, json, zlib
card = json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))
```

Node.js：

```js
const json = JSON.parse(require("zlib").inflateSync(Buffer.from(text, "base64")).toString("utf8"));
```

浏览器：

```js
const bytes = Uint8Array.from(atob(text), (ch) => ch.charCodeAt(0));
const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
const card = JSON.parse(await new Response(stream).text());
```

## 6. 导入规则

### 6.1 新卡语义

- 每次导入都**创建一张新角色卡**，生成全新 ID，**不覆盖、不改动任何已有角色卡**。
- 归属固定为**当前登录用户**：忽略来源中的 `playerId` / `playerName`，并清空 `public_id` / `publisher_*`（不会自动发布到广场）。
- 重名处理：与当前用户已有角色卡重名时，自动追加中文序号，例如 `调查员（2）`、`调查员（3）`，并在导入成功提示中说明「重名已自动改名」。
- 单次只能导入一张卡；JSON 数组会被拒绝并提示拆分。

### 6.2 来源识别

`mode=auto`（前端默认）：去掉首尾空白与 BOM 后，首字符为 `{` 或 `[` 视为 JSON，否则视为 zlib + base64 分享串。也可显式指定 `mode=json` 或 `mode=text`。

### 6.3 字段校验

- 导出与导入共用 `CARD_FIELD_SPECS`，逐字段校验类型并补齐可选默认值。
- 必填：`name`（非空）、`attributes`（含 `STR`/`DEX`/`CON`/`APP`/`POW`/`SIZ`/`EDU`/`INT`/`LUC`）、`skills`、`weapons`、`equipment`。
- 数字字段必须是整数（或整数值的浮点数），布尔字段必须是布尔值，列表 / 对象字段类型必须匹配。
- 缺失必填字段、类型不符、数值越界都会返回 400 与明确错误信息（如 `字段 attributes.STR 必须是整数。`）。
- 校验全部在写入前完成，**不会出现半张卡写进库**。
- 兼容旧版角色卡 JSON（含 `deriveAttributes` / `battleAttributes` / `skillGroups` / `stories` 等字段的格式）：先转换为运行时结构，再走同一套校验。

### 6.4 规则集校验

- 读取 `rulesetId`（缺省按 `coc7`）；若带 `sheet` 对象，会先把 `sheet` 里的字段补到顶层，以顶层值为准。
- 仅支持规则集注册表（`builtin_registry().ids()`）中已注册的规则集，目前为 `coc7`。
- 不支持的规则集**明确拒绝**并提示当前支持列表，不做静默回退。理由：不同规则集的 `sheet` 字段语义不同，把其他规则集的数据按 coc7 解析会生成一张数值错误的卡；明确拒绝能让分享者知道该卡当前无法导入。
- 「开发中」的占位规则集同样不可导入，会收到不支持提示。

### 6.5 头像与自定义武器还原

- `avatarData` 会写入新卡目录的 `avatar.*`，并替换卡片头像路径；没有 `avatarData` 时不设置头像。
- 为避免 SSRF，导入**不会**按来源里的 `avatar` URL 去抓取远程图片，只接受内嵌 data URI。
- `customWeapons` 会写入新卡目录的 `weapons.json`，与导出前的自定义武器库一致。

## 7. 体积与安全上限

| 限制 | 数值 |
| --- | --- |
| 导入请求体 | 16MB |
| 导入文本（字符） | 12MB |
| 压缩数据（base64 解码后） | 8MB |
| 解压后 JSON | 12MB |
| 前端单文件选择 | 8MB |
| 头像文件 | 4MB |

- 分享串必须能完整解压：截断的 zlib 流、非法 base64、非 zlib 数据、解压后超过上限（压缩炸弹）、UTF-8 解码失败、JSON 语法错误都会返回明确错误码。
- 解压采用 `zlib.decompressobj` + `max_length` 限制，并检查 `eof` 与 `unused_data`，不会把整段数据无限展开后再判断。

## 8. 不导出、也不导入：房间内临时状态

以下内容属于房间运行态或账号态，不属于角色卡持久数据，**不会**进入导出文件，导入时也会被忽略：

- 房间成员快照 `member.character_card`：角色卡在房间信息里的同步副本。
- 房间内的角色状态 `member.character_state`：`current_hp` / `current_san` 以及 `injury_records`、`sanity_records`（房间内伤势与理智变动记录）。
- 房间绑定关系、房间角色（房主 / 管理员 / 玩家）、在线状态、加入时间等成员信息。
- 待投掷请求、掷骰等待队列、AI 对话上下文与聊天记录。
- 房间自动存档、回档节点、房间 AI 用量账本。
- 用户登录态、会话令牌、CSRF Token 等认证信息。
- 账号级 / 站点级房规与技能基础值覆盖（属于用户或站点配置，不属于某张角色卡）。

注意：角色卡自身的 `currentHp` / `currentSan` / `status` 是持久化在 `character.json` 里的当前值，会随卡片导出并导入；房间内另行记录的变动历史不会。

## 9. 限制

- 头像文件上限 4MB（保存接口限制）；内嵌为 data URI 后，JSON 与分享文本串的体积会随头像增大。
- 角色卡目录内其他派生文件（如缩略图等，若存在）不进入导出。
- 导入不改动角色卡 schema、不重构规则集注册表与契约文件，写入复用既有 `character_service.apply_save()` 链路。
