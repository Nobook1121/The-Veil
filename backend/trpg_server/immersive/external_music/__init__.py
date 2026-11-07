"""外链 BGM 检索（Jamendo 主源 / Openverse 次源）。

模块边界：

- ``license``：许可归一化、白名单过滤、署名完整性校验；
- ``providers``：两个免费曲库的 HTTP 适配器，只做「取回 + 归一化」，不做选择；
- ``cache``：剧本级 / 房间级两级元数据缓存（默认不落音频本体）；
- ``search``：标签映射、打分、去重与选曲；
- ``service``：真正对外的一层，负责降级链、熔断、预算、缓存编排与异步任务。

所有网络调用都带超时并且**绝不抛异常到调用方**，失败一律转成结构化降级结果；
外链不可用时调用方应当继续使用程序化底床，不允许出现静音空档。
"""
from __future__ import annotations

from trpg_server.immersive.external_music.service import (
    ENGINE_EXTERNAL,
    ENGINE_OFF,
    ENGINE_PROCEDURAL,
    audio_target,
    cache_audio_bytes,
    in_flight,
    resolve_engine,
    search_and_select,
    submit,
)

__all__ = [
    "ENGINE_EXTERNAL",
    "ENGINE_OFF",
    "ENGINE_PROCEDURAL",
    "audio_target",
    "cache_audio_bytes",
    "in_flight",
    "resolve_engine",
    "search_and_select",
    "submit",
]
