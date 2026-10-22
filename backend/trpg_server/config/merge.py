"""分层合并与加载缓存。

优先级（低 → 高）：代码默认值 < 包内模板 < 用户配置 < 环境变量 < CLI。
每层先各自解析，再按「叶子键整体覆盖」合并，最后经 FieldSpec 转换器统一
类型化；某层的非法值被该键的转换器丢弃回退，不会污染其它层。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from trpg_server.config import paths, sources
from trpg_server.config.schema import (
    AI_SECTIONS,
    AI_SPECS,
    SMALL_MODELS_SECTION,
    SMALL_MODEL_TASK_KEYS,
    SPECS,
    AIRuntimeConfig,
)

logger = logging.getLogger(__name__)


class _Missing:
    """非法值哨兵：转换器以此默认值回退时表示「本层该键非法，丢弃回退下层」。"""

    def __repr__(self) -> str:
        return "<lower layer>"


_MISSING = _Missing()

# 已知 env 名 → (section, key)，启动时由 schema 派生；用于冲突检测与未知变量告警。
_KNOWN_ENV_NAMES: dict[str, tuple[str, str]] = {}
for _spec in SPECS:
    for _key in _spec.keys:
        _env_name = sources.env_key_name(_spec.section, _key)
        if _env_name in _KNOWN_ENV_NAMES and _KNOWN_ENV_NAMES[_env_name] != (_spec.section, _key):
            raise RuntimeError(f"env name conflict: {_env_name}")
        _KNOWN_ENV_NAMES[_env_name] = (_spec.section, _key)
for _task in SMALL_MODEL_TASK_KEYS:
    _KNOWN_ENV_NAMES[sources.env_key_name(SMALL_MODELS_SECTION, _task)] = (SMALL_MODELS_SECTION, _task)


@dataclass(frozen=True)
class Settings:
    """一次加载的生效配置。``data`` 为扁平 section dict（已类型化并补齐默认值）。"""

    data: dict[str, dict[str, Any]]
    ai: AIRuntimeConfig
    # 被部署层（env / cli）锁定的键："section.key" -> 来源；设置页据此禁用对应字段。
    locked: dict[str, str]

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name, {}))


_cache_lock = threading.Lock()
_settings_cache: dict[tuple, Settings] = {}


def invalidate_settings_cache() -> None:
    with _cache_lock:
        _settings_cache.clear()


def _type_layer(layer: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """单层类型化：注册键经转换器解析并归并到主键名；非法值（转换器回退到哨兵）
    整键丢弃，让下层同键的合法值生效；未注册键（动态小模型任务等）原样保留。
    """
    typed: dict[str, dict[str, Any]] = {section: dict(values) for section, values in layer.items()}
    for spec in SPECS:
        section_values = typed.get(spec.section)
        if not section_values:
            continue
        raw: Any = _MISSING
        for key in spec.keys:
            if key in section_values:
                raw = section_values[key]
                break
        if raw is _MISSING:
            continue
        for alias in spec.keys:
            section_values.pop(alias, None)
        try:
            converted = spec.convert(raw, _MISSING)
        except Exception:  # 转换器对哨兵做数值比较等操作时抛错，视为非法值
            converted = _MISSING
        if converted is not _MISSING:
            section_values[spec.keys[0]] = converted
    return typed


def _merge_layers(layers: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """按优先级合并已类型化的层：叶子键整体覆盖，高层胜出。"""
    merged: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for section, values in layer.items():
            if not isinstance(values, dict):
                continue
            merged.setdefault(section, {}).update(values)
    return merged


def _fill_defaults(merged: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """所有层都缺失的注册键补代码默认值（L1 兜底）。"""
    typed: dict[str, dict[str, Any]] = {section: dict(values) for section, values in merged.items()}
    for spec in SPECS:
        section_values = typed.setdefault(spec.section, {})
        if spec.keys[0] not in section_values:
            section_values[spec.keys[0]] = spec.default
    return typed


def _build_ai_config(typed: dict[str, dict[str, Any]]) -> AIRuntimeConfig:
    overrides: dict[str, Any] = {}
    for spec in AI_SPECS:
        for key in spec.keys:
            if key in typed.get(spec.section, {}):
                overrides[spec.name] = typed[spec.section][key]
                break
    small_model_tasks = dict(AIRuntimeConfig().small_model_tasks)
    for key, value in typed.get(SMALL_MODELS_SECTION, {}).items():
        small_model_tasks[key] = str(value)
    overrides["small_model_tasks"] = small_model_tasks
    return replace(AIRuntimeConfig(), **overrides)


def _env_signature(env: Mapping[str, str]) -> tuple:
    return tuple(sorted((k, v) for k, v in env.items() if k.startswith(sources.ENV_PREFIX)))


def load_settings(
    config_dir: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cli_overrides: dict[str, dict[str, Any]] | None = None,
) -> Settings:
    """加载生效配置（带缓存）。``env``/``cli_overrides`` 主要供测试注入。"""
    resolved_dir = paths.resolve_config_dir(config_dir)
    user_path = resolved_dir / paths.GENERAL_CONFIG_FILENAME
    template_path = paths.template_general_path()
    environ = dict(os.environ if env is None else env)
    cli_layer = cli_overrides or {}

    try:
        stat = user_path.stat()
        user_stamp: tuple = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        user_stamp = (0, 0)
    try:
        tstat = template_path.stat()
        template_stamp: tuple = (tstat.st_mtime_ns, tstat.st_size)
    except OSError:
        template_stamp = (0, 0)
    cache_key = (str(resolved_dir), user_stamp, template_stamp, _env_signature(environ),
                 repr(sorted(cli_layer.items())) if cli_layer else "")

    with _cache_lock:
        cached = _settings_cache.get(cache_key)
    if cached is not None:
        return cached

    template_layer = sources.read_toml_file(template_path)
    user_layer = sources.read_toml_file(user_path)
    env_layer, locked, unknown_env = sources.collect_env_layer(environ, _KNOWN_ENV_NAMES)
    for name in unknown_env:
        logger.warning("Unknown config env var %s ignored", name)
    cli_locked = {f"{section}.{key}": "cli" for section, values in cli_layer.items() for key in values}

    layers = [template_layer, user_layer, env_layer, cli_layer]
    merged = _merge_layers([_type_layer(layer) for layer in layers])
    typed = _fill_defaults(merged)
    settings = Settings(
        data=typed,
        ai=_build_ai_config(typed),
        locked={**locked, **cli_locked},
    )

    with _cache_lock:
        _settings_cache[cache_key] = settings
    return settings


__all__ = [
    "Settings",
    "load_settings",
    "invalidate_settings_cache",
    "AI_SECTIONS",
    "AIRuntimeConfig",
]
