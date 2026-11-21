"""fvtt-world-import：FVTT 世界包导入插件（读取层 + 登记台 + 转换管线）。

- ``leveldb`` / ``snappy``：只读 LevelDB + snappy 解码（第一刀）；
- ``world_package``：zip 审计（world.json 校验 / 集合存在性 / 计数与警告，
  含 journal 空库警告）；
- ``convert_scenes``：场景（墙/门/光/棋子/网格直供）→ veil-map 载荷；
- ``convert_actors``：actors → dnd5e 角色卡 + 剧本 NPC statblock；
- ``convert_journal``：journal/folders → 剧本场景文本条目 + 世界树图谱数据
  （AI 辅助语义切块可选注入，缺席/失败确定性降级）；
- HTTP 入口在插件根的 ``entry.py``（``/api/plugins/fvtt-world-import/...``）。
"""

from fvtt_world_import.leveldb import DbLimits, LevelDbError, LevelDbReader

__all__ = ["DbLimits", "LevelDbError", "LevelDbReader"]
