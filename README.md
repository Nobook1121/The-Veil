<div align="center">

# 帷幕 The Veil

### 自托管的 AI 跑团平台 · AI 当守秘人，和真人朋友一起玩

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-2ea44f.svg)]()
[![Docs](https://img.shields.io/badge/Docs-Wiki-8a2be2.svg)](https://github.com/Nobook1121/The-Veil/wiki)

**下载、解压、双击、开始游玩。**

</div>

---

帷幕（The Veil）把「AI 当 KP / 守秘人」和「真人玩家一起玩」结合：AI 负责推进剧情、裁决检定、扮演 NPC，玩家在同一个房间聊天里掷骰、交流、推进故事。

**这是你自己的跑团服务器**。账号、剧本、聊天记录全都留在你自己的电脑上，依赖任何官方服务器。

**它适合谁**

- 想随时开一局、却总是凑不齐主持人的跑团玩家。
- 想把手里的剧本文档，变成能对话、能交互的模组的 KP。
- 想在自己电脑或服务器上，搭一个只给朋友用的跑团小站的人。

---

## 一分钟开始

1. **下载**：[点这里下载最新版本](https://github.com/Nobook1121/The-Veil/releases/latest)，解压到任意文件夹。
2. **启动**：解压压缩包，运行 **`start.cmd`**。
   - 电脑里没装 Python？**脚本会自动帮你下载安装**，全程不用敲任何命令。
   - 第一次启动会自动准备运行环境，请耐心等一两分钟。
3. **开跑！**：浏览器打开 <http://127.0.0.1:8086>，跟着 5 步向导走完，就可以开团。

> 想让朋友也来玩？把启动窗口里列出的「局域网地址」发给他们就行。异地联机的办法见 [部署与访问](https://github.com/Nobook1121/The-Veil/wiki/%E9%83%A8%E7%BD%B2%E4%B8%8E%E8%AE%BF%E9%97%AE)。

### 第一次打开会自动引导

当系统里还没有管理员账号时，打开首页会自动弹出向导：

| 步骤 | 要做什么 |
| --- | --- |
| 1. 配置 AI | 选择平台、填 API Key 与模型，点「测试连接」验证通过后保存 |
| 2. 起个站名 | 显示在标题栏与侧边栏 |
| 3. 绑定域名 | 可跳过，仅作展示 |
| 4. 创建 Owner | 最高权限账号，用来管理整站 |
| 5. 进入系统 | 点一下即自动登录 |

---

## 它有什么

### 给玩家

- **AI 守秘人全程主持**：AI 会记住当前场景和已经发生的剧情，按需检索剧本内容，不会一次性把剧透全倒出来。
- **COC7 角色卡与检定**：属性、技能、武器、专精一应俱全；技能基础值支持「房规 > 管理员默认 > 规则书默认」三级覆盖。
- **聊天里直接掷骰**：成功、失败、大成功、大失败都有明确反馈。
- **剧情不会丢**：房间聊天记录持久保存，可手动打「回档节点」，也能按周期自动存档。
- **富媒体消息**：头像、图片、视频都能发，图片点开放大，视频直接内嵌播放。

### 给房主 / KP

- **房间管理**：房间码邀请、成员列表、角色变更实时同步、正在输入提示。
- **随时换剧本**：跑团中途也能换。换同一剧本的新版本时可继承剧情进度，换别的剧本则重置。
- **细粒度权限**：OWNER / ADMIN / USER 三级身份之外，还能按功能逐项勾选权限，界面已完整中文化。
- **世界书可视化编辑**：按章节查看 AI 实际会读到的内容，并手动调整触发词、常驻、分层、概率、粘滞与冷却。

### 剧本与内容

- **三种导入方式**：写好的成熟模组可「直接导入」，导入即玩；格式比较乱的文档用「AI 全链路导入」，由 AI 通读、剔除作者吐槽等无关内容、按语义划分模块并生成世界书与图谱；也可以直接上传 AI / Skill 生成好的剧本包 `.json`。
- **离线剧本导入 Skill**：[`skills/scenario-import`](skills/scenario-import/SKILL.md) 可单独交给 AI 使用，先剔除无关内容、再按语义分模块、生成世界书与图谱，产出一个可直接拖进网站的剧本包；站内 Agent 也能直接调用 `scenarios.import_script` 完成同样流程。
- **完整剧本编辑器**：场景卡、结局、时间线、NPC、怪物、道具模块化编辑，带封面与简介。
- **触发器资源库**：给剧本挂图片 / 音频 / 视频 / 富文本 / 附件，按场景进入、线索发现、NPC 对话等条件自动推送，可设剧透等级与可见范围。
- **版本管理**：改内容自动升版本，老房间锁定在原版本继续跑，新房间用最新版，互不影响。
- **规则书知识库**：上传 COC7 规则文本，AI 回答规则问题时会查到你自己的房规。

### 管理

- **一键接入主流大模型**：DeepSeek / OpenAI / 阿里云百炼 / SiliconFlow / OpenRouter / LM Studio，也支持任意 OpenAI 兼容接口。
- **AI 用量看板**：按天统计 token 消耗、缓存命中率与各房间的成本分布。
- **中英双语界面**：内置 i18n，可实时切换并记住选择。
- **服务器控制台命令**：在服务运行窗口里直接管理账号权限，例如 `user promote <用户名> ADMIN`。

---

## 系统要求

- **Windows 10 / 11**：什么都不用装，双击 `start.cmd` 即可（缺 Python 时脚本会自动安装）。
- **macOS / Linux**：装好 [Python 3.11+](https://www.python.org/downloads/) 后运行 `python server.py`。
- 只想在**自己电脑上玩**时，不需要域名、不需要证书、不需要云服务器。

发布包已内置构建好的前端页面，**不需要安装 Node.js**。只有从源码自行构建前端时才需要 Node.js 18+。

---

## 文档

玩法、部署、AI 配置等技术细节都整理在文档站（Wiki）里：

| 想了解 | 看这里 |
| --- | --- |
| 安装、启动与首次部署 | [快速开始](https://github.com/Nobook1121/The-Veil/wiki/%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B) |
| 完整的跑团流程 | [怎么玩](https://github.com/Nobook1121/The-Veil/wiki/%E6%80%8E%E4%B9%88%E7%8E%A9) |
| 让朋友连进来（局域网 / 异地） | [部署与访问](https://github.com/Nobook1121/The-Veil/wiki/%E9%83%A8%E7%BD%B2%E4%B8%8E%E8%AE%BF%E9%97%AE) |
| 配置 AI 平台与模型 | [AI 设置](https://github.com/Nobook1121/The-Veil/wiki/AI-%E8%AE%BE%E7%BD%AE) |
| 剧本导入、世界书、触发器 | [剧本与内容](https://github.com/Nobook1121/The-Veil/wiki/%E5%89%A7%E6%9C%AC%E4%B8%8E%E5%86%85%E5%AE%B9) |
| 连不上、AI 不回复等排查 | [常见问题](https://github.com/Nobook1121/The-Veil/wiki/%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98) |
| 源码结构、开发命令、进阶选项 | [开发者](https://github.com/Nobook1121/The-Veil/wiki/%E5%BC%80%E5%8F%91%E8%80%85) |

完整目录见 [文档首页](https://github.com/Nobook1121/The-Veil/wiki)。

就近的文档也在仓库里：[接口概览](docs/api.md)、[开发说明](docs/development.md)。

---

## 许可

本项目以 [Apache License 2.0](LICENSE) 发布。请同时遵守你所用剧本、规则书与 AI 服务各自的授权条款。
