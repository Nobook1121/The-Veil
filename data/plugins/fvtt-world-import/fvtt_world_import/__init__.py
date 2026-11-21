"""fvtt-world-import：FVTT 世界包导入插件（读取层 + 登记台 + 场景转换）。

- ``leveldb`` / ``snappy``：只读 LevelDB + snappy 解码（第一刀）；
- ``world_package``：zip 审计（world.json 校验 / 集合存在性 / 计数与警告）；
- ``convert_scenes``：场景（墙/门/光/棋子/网格直供）→ veil-map 载荷；
- HTTP 入口在插件根的 ``entry.py``（``/api/plugins/fvtt-world-import/...``）。

actors→角色卡与 journal→世界树由后续刀叠加。
"""

from fvtt_world_import.leveldb import DbLimits, LevelDbError, LevelDbReader

__all__ = ["DbLimits", "LevelDbError", "LevelDbReader"]
