# 前端全局 API 清单（window.Trpg*）

本清单冻结当前「原生全局 TS + React 岛」混合期的共享边界，供 review 时据此判断
「能不能再往全局挂东西」。统计基线：`frontend/src` 下 25 个全局、约 250 处引用、48 个文件。

## 状态定义

| 状态 | 含义 | 允许的改动 |
| --- | --- | --- |
| **保留** | 长期契约：基础设施或跨模块稳定入口 | 可新增方法，需同步 `types/*.d.ts` 与本文档 |
| **冻结** | 历史模块的兼容出口 | 只修 bug，不新增方法/字段；新功能不得依赖 |
| **待迁移** | 已有明确 React 化计划 | 只做兼容修补，新能力加在 React 侧 |

## 清单

| 全局名 | 定义位置 | 引用 | 状态 | 说明 |
| --- | --- | --- | --- | --- |
| `TrpgApi` | `app/api-client.ts` | 3 | 保留 | 统一接口调用入口（新代码首选） |
| `TrpgDom` | `app/dom-utils.ts` | 2 | 保留 | DOM 查询与事件绑定工具 |
| `TrpgDialog` | `app/dialog.ts` | 35 | 保留 | `confirm()` / `prompt()`，禁止原生弹窗 |
| `TrpgI18n` | `app/i18n.ts` | 32 | 保留 | `t()` / `apply()` / `setLocale()` |
| `TrpgTemplates` | `app/generated/templates.ts` | 30 | 保留 | 服务端拼装模板的渲染入口 |
| `TrpgAudioBlocks` | `app/immersive/audio-blocks.ts` | 4 | 保留 | 音频块选择器通用组件 |
| `TrpgCharacterSheetRegistry` | `app/character-sheet-registry.ts` | 3 | 保留 | 规则集 → 角色卡模块注册表（新规则集只加注册） |
| `TrpgCharacterSheetHost` | `app/character-sheet.ts` | 1 | 保留 | 角色卡宿主挂载点 |
| `TrpgCookies` | `app/cookie-consent.ts` | 1 | 保留 | Cookie 同意状态 |
| `TrpgQr` | `app/qr.ts` | 1 | 保留 | 二维码 SVG 渲染 |
| `TrpgBgm` | `app/bgm.ts` | 22 | 冻结 | 房间 BGM 播放列表 |
| `TrpgDiceTable` | `app/diceTable.ts` | 15 | 冻结 | 骰桌渲染与投掷交互 |
| `TrpgCombat` | `app/combat.ts` | 9 | 冻结 | 战斗面板与战斗态刷新 |
| `TrpgImmersive` | `app/immersive/index.ts` | 16 | 冻结 | 沉浸总控（音乐/配图/音效开关） |
| `TrpgImmersiveMusic` | `app/immersive/music-engine.ts` | 27 | 冻结 | 程序化音乐引擎 |
| `TrpgImmersiveAmbient` | `app/immersive/ambient-engine.ts` | 12 | 冻结 | 环境音引擎 |
| `TrpgImmersiveExternal` | `app/immersive/external-player.ts` | 4 | 冻结 | 外链播放器桥 |
| `TrpgImmersivePlayer` | `app/immersive/player.ts` | 1 | 冻结 | 播放器 UI |
| `TrpgVoice` | `app/immersive/voice.ts` | 5 | 冻结 | 语音朗读与消息按钮 |
| `TrpgWorldOverride` | `app/worldOverride.ts` | 4 | 冻结 | 世界意志直执行面板 |
| `TrpgCommandSuggestions` | `tools/toolManager.ts` | 2 | 冻结 | 斜杠命令补全 |
| `TrpgAdminDiceTextures` | `app/dice/adminDiceTextures.ts` | 3 | 冻结 | 管理员骰子材质 |
| `TrpgDiceAppearanceSettings` | `app/dice/diceAppearanceSettings.ts` | 1 | 冻结 | 个人骰子外观设置 |
| `TrpgSettingsAssistant` | `app/settings/settingsAssistant.ts` | 2 | 冻结 | 设置页向导 |
| `TrpgNarrative` | `app/scenario-narrative/shared.ts` | 15 | 待迁移 | 剧本叙事编辑器（体量最大，优先 React 岛化） |

## 新 React 岛约定

1. **挂载点固定**：在 `index/fragments/*.html` 预留 `<div id="react-island-<name>">`，
   由对应的 `mount<Name>()` 在 `main.ts` 初始化阶段挂载；岛内不得查询岛外的全局 DOM 节点。
2. **必须可卸载**：挂载函数返回 cleanup；在 unmount 时移除事件监听、`clearInterval` /
   `clearTimeout`、`socket.off(...)`，并用 `AbortController` 取消进行中的请求。
3. **必须有类型**：props、事件 payload、对外暴露的方法都要有 TS 类型；禁止 `any` 逃逸。
   访问 `window.Trpg*` 前，确认 `types/*.d.ts` 已有声明。
4. **不直接写全局**：岛内不新增 `window.TrpgXxx`；需要跨模块能力时通过 props / context 注入。
5. **新功能一律进 React 岛**；老模块只在被本次改动触及的范围内顺带迁移，不做无关重写。

## 变更流程

- 新增全局：先加 `types/*.d.ts` 声明，再更新本清单（状态只能是「保留」或「待迁移」）。
- 删除全局：确认引用数为 0，移除声明与本清单条目。
- 「冻结」模块若必须新增方法：先评估能否改为 React 岛内实现；确实不能时，同步更新本文档并注明原因。
