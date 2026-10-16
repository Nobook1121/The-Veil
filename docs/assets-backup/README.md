# 默认头像 / 默认封面备份

这里保存的是项目早期使用的「默认图片」，现已从运行时资产目录移除，仅作历史备份。

| 文件名 | 原用途 | 原路径 |
| --- | --- | --- |
| `default.jpg` | 用户默认头像、非 KP 的 AI 角色默认头像 | `data/assets/avatars/default.jpg` |
| `default_kp.jpg` | KP 角色默认头像 | `data/assets/avatars/default_kp.jpg` |
| `default_system.jpg` | 系统 / 触发器消息默认头像 | `data/assets/avatars/default_system.jpg` |
| `default_dice.jpg` | 骰娘消息默认头像 | `data/assets/avatars/default_dice.jpg` |
| `default_cover.png` | 剧本默认封面 | `data/assets/scenario_covers/default_cover.png` |

替代方案：不再使用任何默认图片。

- 头像：取「名称首字」生成文字头像（用户用用户名首字、角色卡用角色名首字、AI 角色用角色名首字）。
- 剧本封面：直接使用剧本名，自动换行填充封面区域。

实现见前端 `frontend/src/app/avatar-utils.ts`（文字头像/文字封面 SVG 生成）。
