"""实时沉浸系统（程序化配乐 + 实时配图）。

拆成小模块，职责边界清晰：

- ``config``       ：读取/归一化 ``[immersive]`` 配置与房间级覆盖；
- ``analyzer``     ：把叙事文本确定性映射为氛围档位、强度与场景标签；
- ``music``        ：带滞回与种子复现的配乐导演（输出给前端 Web Audio 合成器）；
- ``svg_scene``    ：离线可用的确定性程序化场景图；
- ``illustration`` ：配图缓存、预算/限额、外部生图队列与优雅降级；
- ``service``      ：叙事回合编排入口（供 ``routes/chat.py`` 调用）；
- ``broadcast``    ：Socket.IO 广播注入（与 player_roll/combat 同一范式）。
"""
from trpg_server.immersive.broadcast import register_broadcaster
from trpg_server.immersive.service import (
    generate_illustration_now,
    immersive_status,
    on_narrative_turn,
    set_music_directive,
)

__all__ = [
    "register_broadcaster",
    "on_narrative_turn",
    "generate_illustration_now",
    "set_music_directive",
    "immersive_status",
]
