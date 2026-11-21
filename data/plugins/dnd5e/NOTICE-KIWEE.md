# NOTICE — kiwee 内容深库（数据来源与许可声明）

本插件 `data/plugins/dnd5e/` 内的 `dnd5e_plugin/kiwee/` 子模块（快照脚本、
SQLite 建库/查询、datatag 解析器）服务于 **kiwee 内容深库**：把 5etools
中文镜像站的数据做成本地可检索库，作为 AI DM 的 D&D 5e 规则事实源。

## 数据来源

| 项 | 值 |
|---|---|
| 站点 | https://5e.kiwee.top/ （5etools 非官方中文镜像，津ICP备2024020109号-1） |
| 数据仓库 | https://github.com/tjliqy/5etools-cn （分支 `cn2.0`） |
| 英文上游 | https://5e.tools/ ｜ https://github.com/5etools-mirror-3/5etools-src ｜ wiki.tercept.net |
| 调研文档 | `2026-11-21/kiwee-wiki-survey/outputs/kiwee-wiki-survey.md`（schema 实例、获取通道实测、DM/玩家分界口径） |

## 许可

- **中文本地化数据：CC BY-NC-SA 4.0**（来源仓库 README.zh-CN.md 声明）。
  本项目非商用、完全开源；使用该数据须署名译者与来源站点/仓库链接，衍生
  数据按同协议共享。**声明保留在本插件目录内，不传染主程序与其他插件。**
- 源码及英文源数据为 MIT（上游 5etools-src）。
- 站点内容为非官方镜像，与 The Norr Group Ltd.（威世智）无关联；D&D 5e
  相关名称与商标归其权利人所有。本插件只做规则检索的本地索引。

## 机翻内容免责

kiwee 数据中 `translator` 字段缺失/为空或等于「机翻」的条目是**未人工校对
的机器翻译**（判定逻辑与站点 `js/render.js` 一致）。本插件建库时如实落成
`is_machine_translated` 标记，检索结果带「⚠️机翻」语义徽章
（`translator_ok=False`），消费方应向用户如实展示质量分级，**不得**把机翻
译文当作权威规则文本引用。

## 本地快照不随仓库分发

`data/plugins/dnd5e/kiwee-data/`（GitHub 快照 `raw/` 与建成的
`kiwee.sqlite3`，合计约 200MB+）是**本地生成物，不进 git**（见根
`.gitignore`）。获取方式见 `dnd5e_plugin/kiwee/snapshot.py`：

```bash
cd data/plugins/dnd5e
python -m dnd5e_plugin.kiwee snapshot fetch   # GitHub 稀疏检出，绝不批量爬活站
python -m dnd5e_plugin.kiwee snapshot build   # SQLite + FTS5 建库
```

robots.txt 对 `https://5e.kiwee.top/data/` 为 `Disallow`，任何获取一律走
GitHub 仓库；活站单文件 JSON 仅作低频按需兜底（本刀未实现）。快照缺席时
深库服务自动降级（`get_service("dnd5e", "kiwee_db")` 检索返回
`None` / 状态「深库未建」），全部现有功能照常、回落 dnd5e-srd。
