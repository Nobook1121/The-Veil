"""fvtt-world-import：FVTT 世界包/地图包导入插件（读取层）。

当前交付：只读 LevelDB + snappy 解码与集合导出 CLI。
插件清单、HTTP 入口与转换管线由后续任务叠加。
"""

from fvtt_world_import.leveldb import DbLimits, LevelDbError, LevelDbReader

__all__ = ["DbLimits", "LevelDbError", "LevelDbReader"]
