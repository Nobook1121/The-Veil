"""AI 对话语音配音（第一阶段 P1）。

模块边界：
- :mod:`voice.markup`   标记解析与正文剥离；
- :mod:`voice.profiles` 8+1 内置人设与稳定分配；
- :mod:`voice.config`   站点/房间配置合并与房间映射、用量；
- :mod:`voice.engine`   引擎适配（本阶段仅 system）；
- :mod:`voice.director` 回合编排、降级与结构化日志。
"""
from trpg_server.voice.director import prepare_voice
from trpg_server.voice.profiles import PROFILE_MAP, PROFILES
from trpg_server.voice.markup import parse_narration

__all__ = ["prepare_voice", "parse_narration", "PROFILES", "PROFILE_MAP"]
