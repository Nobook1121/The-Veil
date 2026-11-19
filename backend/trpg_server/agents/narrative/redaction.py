"""地图信息剥离：AI 侧输出只保留剧情与规则字段。

本模块集中处理「AI 不看地图」的出口清洗：

- `strip_map_fields`：递归删除底图引用、像素标定与 DM 标记；
- `strip_trigger_asset_fields`：删除触发器的资源地址与附件元数据；
- `redact_map_text`：把 Markdown/HTML 图片替换为占位文本；
- `scenario_has_map_layer`：判断剧本是否已标注 map/poi 层。

所有函数都返回新对象，不修改传入的 scenario/bundle 缓存。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

# 明确属于地图编辑器/底图的键；不要加入单数 `marker`，它被当作模块摘要文本使用。
MAP_FIELD_KEYS = frozenset(
    {
        "map_config",
        "map_asset",
        "map_asset_id",
        "map_asset_url",
        "map_assets",
        "dm_asset",
        "dm_asset_id",
        "dm_asset_url",
        "player_asset_id",
        "background_url",
        "background_asset_id",
        "cell_px",
        "offset_x",
        "offset_y",
        "marker_types",
        "markers",
        "map_markers",
    }
)

# 触发器/附件的资源字段；`kind`/`alt` 保留，附件正文走剧情链路。
TRIGGER_ASSET_KEYS = frozenset(
    {
        "asset_url",
        "asset_name",
        "asset_path",
        "asset_data",
        "asset_data_url",
    }
)

TRIGGER_CATALOG_FIELDS = (
    "id",
    "scene_id",
    "module_id",
    "module_title",
    "display_name",
    "keyword",
    "condition",
    "content_mode",
    "spoiler_level",
    "visibility",
)

_IMAGE_MARKDOWN = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def _strip_keys(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_keys(item, keys)
            for key, item in value.items()
            if str(key) not in keys
        }
    if isinstance(value, (list, tuple)):
        return [_strip_keys(item, keys) for item in value]
    return value


def strip_map_fields(value: Any) -> Any:
    """递归删除地图底图、像素标定与 DM 标记字段。"""
    return _strip_keys(value, MAP_FIELD_KEYS)


def strip_trigger_asset_fields(value: Any) -> Any:
    """递归删除触发器与附件里的资源地址/内联数据。"""
    return _strip_keys(value, TRIGGER_ASSET_KEYS)


def public_trigger_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """触发器目录只保留白名单字段，避免 `asset_url` 等间接泄露。"""
    return {key: item[key] for key in TRIGGER_CATALOG_FIELDS if key in item}


def redact_map_text(text: Any) -> Any:
    """把图片语法替换为占位文本；普通链接与正文保持不变。"""
    if not isinstance(text, str) or not text:
        return text

    def _replace_markdown(match: re.Match[str]) -> str:
        alt = str(match.group(1) or "").strip()
        return f"[图片：{alt}]" if alt else "[图片]"

    redacted = _IMAGE_MARKDOWN.sub(_replace_markdown, text)
    return _HTML_IMAGE.sub("[图片]", redacted)


def scenario_has_map_layer(
    scenarios_dir: Any,
    scenario_id: Any,
    scenario_version: Any = None,
) -> bool:
    """剧本是否具备 map/poi 层；缺失或损坏时安全返回 False。"""
    if not scenarios_dir or scenario_id in (None, ""):
        return False
    try:
        from trpg_server.agents.narrative.mapgraph import has_poi_layer
        from trpg_server.agents.narrative.store import load_bundle, scenario_dir_for_descriptor
        from trpg_server.scenario_store import load_scenario_by_id

        descriptor, _scenario = load_scenario_by_id(
            Path(scenarios_dir), scenario_id, scenario_version=scenario_version
        )
        if not descriptor:
            return False
        bundle = load_bundle(scenario_dir_for_descriptor(descriptor))
        return bool(bundle is not None and has_poi_layer(bundle))
    except Exception:  # noqa: BLE001 - 地图层判定失败按「无地图」降级
        return False


__all__ = [
    "MAP_FIELD_KEYS",
    "TRIGGER_ASSET_KEYS",
    "TRIGGER_CATALOG_FIELDS",
    "public_trigger_item",
    "redact_map_text",
    "scenario_has_map_layer",
    "strip_map_fields",
    "strip_trigger_asset_fields",
]
