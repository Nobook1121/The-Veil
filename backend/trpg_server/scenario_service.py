"""剧本的领域服务。

网站 Agent 的剧本写操作必须与管理员在界面上保存的结果一致，因此这里复用
``scenario_store`` 的落盘、版本与知识库索引逻辑；身份只来自 ``Actor``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app

from trpg_server.agents.versioning import normalize_semver, scenario_content_changed, next_scenario_version
from trpg_server.media_defaults import normalize_cover
from trpg_server.scenario_store import (
    generate_public_id,
    load_scenario_record,
    save_scenario_record,
    scenario_descriptor_paths,
    scenario_public_asset_url,
    trigger_size_limit,
)
from trpg_server.settings import SCENARIOS_DIR

BASIC_FIELDS = ("title", "author", "description", "recommended_players", "cover", "tags")
HEADER_FIELDS = ("title", "author", "description", "recommended_players")


class ScenarioServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _scenarios_dir() -> Path:
    return Path(current_app.config.get("SCENARIOS_DIR", SCENARIOS_DIR))


def _timestamp() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _find(scenario_id: Any) -> tuple[Path | None, dict[str, Any] | None]:
    for path in scenario_descriptor_paths(_scenarios_dir()):
        try:
            data = load_scenario_record(path, _scenarios_dir())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if str(data.get("id")) == str(scenario_id):
            return path, data
    return None, None


def can_modify(actor: Any, scenario: dict[str, Any] | None) -> bool:
    if not isinstance(scenario, dict):
        return False
    if actor.has("scenarios.manage_all"):
        return True
    return str(scenario.get("owner_id")) == str(actor.user_id)


def list_for_actor(actor: Any, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    for path in scenario_descriptor_paths(_scenarios_dir()):
        try:
            scenario = load_scenario_record(path, _scenarios_dir())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not can_modify(actor, scenario):
            continue
        title = str(scenario.get("title") or "")
        if needle and needle not in title.lower() and needle not in str(scenario.get("id") or ""):
            continue
        items.append(
            {
                "id": scenario.get("id"),
                "title": title,
                "scenario_version": scenario.get("scenario_version", ""),
                "updatedAt": scenario.get("updatedAt", ""),
                "archived": bool(scenario.get("archived")),
                "cover": scenario_public_asset_url(str(scenario.get("cover") or "")),
            }
        )
        if len(items) >= max(1, min(int(limit or 20), 50)):
            break
    return items


def get_for_actor(actor: Any, scenario_id: Any) -> dict[str, Any]:
    _, scenario = _find(scenario_id)
    if not scenario:
        raise ScenarioServiceError("scenario_not_found", "剧本不存在", 404)
    if not can_modify(actor, scenario):
        raise ScenarioServiceError("permission_denied", "无权编辑该剧本", 403)
    result = dict(scenario)
    result.pop("knowledge", None)
    return result


def descriptor_path_for_actor(actor: Any, scenario_id: Any) -> Path:
    """返回可编辑剧本的 ``scenario.json`` 路径（Agent 读取图谱等附属文件时用）。"""
    path, scenario = _find(scenario_id)
    if path is None or not scenario:
        raise ScenarioServiceError("scenario_not_found", "剧本不存在", 404)
    if not can_modify(actor, scenario):
        raise ScenarioServiceError("permission_denied", "无权编辑该剧本", 403)
    return path


def _validate_version(value: Any) -> str | None:
    text = str(value or "").strip()
    if text and normalize_semver(text, default="") == "":
        return "剧本版本号必须使用 n.n.n 格式"
    return None


def _duplicate_title(title: str, *, exclude_id: Any = None) -> bool:
    for path in scenario_descriptor_paths(_scenarios_dir()):
        try:
            data = load_scenario_record(path, _scenarios_dir())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if exclude_id is not None and str(data.get("id")) == str(exclude_id):
            continue
        if data.get("title") == title:
            return True
    return False


def preview_create(actor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not actor.has("scenarios.create"):
        raise ScenarioServiceError("permission_denied", "缺少 scenarios.create 权限", 403)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ScenarioServiceError("invalid_scenario", "剧本缺少标题", 400)
    version_error = _validate_version(payload.get("scenario_version"))
    if version_error:
        raise ScenarioServiceError("invalid_version", version_error, 400)
    if _duplicate_title(title):
        raise ScenarioServiceError("title_exists", f'剧本标题「{title}」已存在', 400)
    return {
        "is_new": True,
        "scenario_id": None,
        "title": title,
        "changed_fields": [field for field in HEADER_FIELDS if payload.get(field)],
        "summary": f"创建剧本「{title}」",
    }


def apply_create(actor: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if not actor.has("scenarios.create"):
        raise ScenarioServiceError("permission_denied", "缺少 scenarios.create 权限", 403)
    data = dict(payload or {})
    title = str(data.get("title") or "").strip()
    if not title or _duplicate_title(title):
        raise ScenarioServiceError("title_exists", "剧本标题为空或已存在", 400)
    version_error = _validate_version(data.get("scenario_version"))
    if version_error:
        raise ScenarioServiceError("invalid_version", version_error, 400)

    import time

    scenario_id = int(time.time() * 1000)
    data["id"] = scenario_id
    data["scenario_version"] = normalize_semver(data.get("scenario_version"))
    data["owner_id"] = actor.user_id
    data["creator_username"] = actor.username
    data["public_id"] = generate_public_id(
        {str(item.get("public_id")) for item in _all_scenarios() if item.get("public_id")}
    )
    data["createdAt"] = _timestamp()
    data["updatedAt"] = data["createdAt"]
    data["cover"] = normalize_cover(data.get("cover"))

    file_path = save_scenario_record(_scenarios_dir(), data, trigger_max_file_size=_trigger_size_limit())
    saved = load_scenario_record(file_path, _scenarios_dir())
    saved["knowledge"] = _index_scenario_knowledge(file_path, saved)
    clear_cache()
    return saved


def preview_update(actor: Any, scenario_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    _, existing = _find(scenario_id)
    if not existing:
        raise ScenarioServiceError("scenario_not_found", "剧本不存在", 404)
    if not can_modify(actor, existing):
        raise ScenarioServiceError("permission_denied", "无权编辑该剧本", 403)
    if not actor.has("scenarios.edit"):
        raise ScenarioServiceError("permission_denied", "缺少 scenarios.edit 权限", 403)
    version_error = _validate_version(payload.get("scenario_version"))
    if version_error:
        raise ScenarioServiceError("invalid_version", version_error, 400)
    merged = {**existing, **payload}
    changed = scenario_content_changed(existing, merged)
    current_version = str(existing.get("scenario_version") or existing.get("version") or "1")
    changed_fields = [
        field
        for field in BASIC_FIELDS
        if field in payload and payload.get(field) != existing.get(field)
    ]
    content_fields = [key for key in payload if key not in BASIC_FIELDS]
    summary = f"更新剧本「{merged.get('title') or existing.get('title')}」"
    if changed:
        summary += f"，版本 {current_version} → {next_scenario_version(normalize_semver(current_version))}"
    else:
        summary += "（仅基础信息，版本不变）"
    return {
        "is_new": False,
        "scenario_id": existing.get("id"),
        "title": merged.get("title"),
        "changed_fields": changed_fields,
        "content_fields": content_fields,
        "summary": summary,
    }


def apply_update(actor: Any, scenario_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    target_file, existing = _find(scenario_id)
    if not existing or target_file is None:
        raise ScenarioServiceError("scenario_not_found", "剧本不存在", 404)
    if not can_modify(actor, existing):
        raise ScenarioServiceError("permission_denied", "无权编辑该剧本", 403)
    if not actor.has("scenarios.edit"):
        raise ScenarioServiceError("permission_denied", "缺少 scenarios.edit 权限", 403)
    version_error = _validate_version(payload.get("scenario_version"))
    if version_error:
        raise ScenarioServiceError("invalid_version", version_error, 400)

    current_version = str(existing.get("scenario_version") or existing.get("version") or "1")
    data = {**existing, **payload}
    data["id"] = existing.get("id")
    changed = scenario_content_changed(existing, data)
    requested = str(payload.get("scenario_version") or "").strip()
    if not changed:
        data["scenario_version"] = current_version
    elif requested and normalize_semver(requested) != normalize_semver(current_version) and "." in requested:
        data["scenario_version"] = normalize_semver(requested)
    else:
        data["scenario_version"] = normalize_semver(next_scenario_version(normalize_semver(current_version)))
    data["owner_id"] = existing.get("owner_id")
    data["creator_username"] = existing.get("creator_username") or actor.username
    data["public_id"] = existing.get("public_id") or generate_public_id(
        {
            str(item.get("public_id"))
            for item in _all_scenarios()
            if item.get("public_id") and str(item.get("id")) != str(scenario_id)
        }
    )
    data["updatedAt"] = _timestamp()
    data.setdefault("createdAt", existing.get("createdAt", _timestamp()))

    file_path = save_scenario_record(
        _scenarios_dir(),
        data,
        existing_descriptor=target_file,
        trigger_max_file_size=_trigger_size_limit(),
    )
    saved = load_scenario_record(file_path, _scenarios_dir())
    saved["knowledge"] = (
        _index_scenario_knowledge(file_path, saved) if changed else _knowledge_stats(file_path, saved)
    )
    clear_cache()
    return saved


def _all_scenarios() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in scenario_descriptor_paths(_scenarios_dir()):
        try:
            result.append(load_scenario_record(path, _scenarios_dir()))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return result


def _trigger_size_limit():
    return trigger_size_limit(current_app.config.get("CONFIG_DIR"))


def _index_scenario_knowledge(file_path: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    from trpg_server.agents.knowledge_base import persist_knowledge_index

    try:
        index_path = persist_knowledge_index(
            file_path,
            scenario,
            vector_store=current_app.extensions.get("vector_store"),
            embedding_provider=current_app.extensions.get("embedding_provider"),
        )
        return {
            "success": True,
            "path": str(index_path),
            "version": str(scenario.get("scenario_version") or scenario.get("version") or "1"),
        }
    except Exception as exc:  # 知识库失败不应让剧本保存回滚
        return {"success": False, "vector_count": 0, "error": str(exc)}


def _knowledge_stats(file_path: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    from trpg_server.agents.knowledge_base import knowledge_index_path, load_knowledge_index

    version = str(scenario.get("scenario_version") or scenario.get("version") or "1")
    chunks = load_knowledge_index(file_path, version)
    return {
        "success": True,
        "vector_count": len(chunks),
        "path": str(knowledge_index_path(file_path, version)),
        "version": version,
    }


def clear_cache() -> None:
    from trpg_server.routes.scenarios import clear_scenarios_cache

    clear_scenarios_cache()
