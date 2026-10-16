from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import secrets
import string
import time
import tomllib
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.media_defaults import LEGACY_DEFAULT_COVERS, normalize_cover
from trpg_server.security import normalize_filename, safe_join
from trpg_server.agents.trigger_system import normalize_attachment


SCENARIO_DESCRIPTOR_NAME = "scenario.json"
SCENARIO_VERSIONS_DIR_NAME = "versions"
SCENARIO_TRIGGER_DIR_NAME = "trigger-content"
SCENARIO_PUBLIC_ROUTE_PREFIX = "/assets/scenarios"
DEFAULT_TRIGGER_SIZE_LIMIT = 5 * 1024 * 1024
SCENARIO_MODULE_TYPES = {
    "opening",
    "background",
    "public_info",
    "preparation",
    "timeline",
    "scene",
    "ending",
    "monster",
    "npc",
    "custom",
}
SCENARIO_MODULE_VISIBILITY = {"public", "kp"}

_PUBLIC_ID_ALPHABET = string.ascii_letters + string.digits
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.IGNORECASE)


def scenario_descriptor_paths(scenarios_dir: Path) -> list[Path]:
    if not scenarios_dir.exists():
        return []

    descriptor_paths: list[Path] = []
    for path in sorted(scenarios_dir.iterdir()):
        if path.is_dir():
            descriptor = path / SCENARIO_DESCRIPTOR_NAME
            if descriptor.exists():
                descriptor_paths.append(descriptor)

    for path in sorted(scenarios_dir.glob("*.json")):
        if path.name != SCENARIO_DESCRIPTOR_NAME:
            descriptor_paths.append(path)

    return descriptor_paths


def scenario_storage_dir(descriptor_path: Path) -> Path:
    return descriptor_path.parent


def clear_legacy_default_covers(scenarios_dir: Path) -> int:
    """一次性清理历史剧本封面：把 JSON 里指向默认封面图的 cover 字段清空。

    项目已不再内置默认封面图（剧本封面改由前端用剧本名渲染），因此历史默认路径
    应视为「无封面」。返回被改写的文件数量；语句幂等，重复执行不会产生副作用。
    """

    cleaned = 0
    for descriptor_path in scenario_descriptor_paths(Path(scenarios_dir)):
        targets = [descriptor_path]
        versions_dir = descriptor_path.parent / SCENARIO_VERSIONS_DIR_NAME
        if versions_dir.is_dir():
            targets.extend(sorted(versions_dir.glob("*.json")))
        for target in targets:
            data = read_json(target, default=None)
            if not isinstance(data, dict):
                continue
            if str(data.get("cover") or "").strip() not in LEGACY_DEFAULT_COVERS:
                continue
            data["cover"] = ""
            write_json_atomic(target, data)
            cleaned += 1
    return cleaned


def find_scenario_descriptor_by_id(scenarios_dir: Path, scenario_id: int | str) -> Path | None:
    expected = str(scenario_id)
    for descriptor_path in scenario_descriptor_paths(scenarios_dir):
        try:
            scenario = load_scenario_record(descriptor_path, scenarios_dir)
        except (OSError, ValueError, json.JSONDecodeError):  # type: ignore[name-defined]
            continue
        if str(scenario.get("id")) == expected:
            return descriptor_path
    return None


def scenario_version_path(descriptor_path: Path, version: int | str) -> Path:
    return descriptor_path.parent / SCENARIO_VERSIONS_DIR_NAME / f"{version}.json"


def load_scenario_by_id(
    scenarios_dir: Path,
    scenario_id: int | str,
    scenario_version: int | str | None = None,
) -> tuple[Path | None, dict[str, Any] | None]:
    descriptor_path = find_scenario_descriptor_by_id(scenarios_dir, scenario_id)
    if not descriptor_path:
        return None, None
    if scenario_version not in (None, ""):
        version_path = scenario_version_path(descriptor_path, scenario_version)
        if not version_path.exists() and str(scenario_version).isdigit():
            from trpg_server.agents.versioning import normalize_semver
            version_path = scenario_version_path(descriptor_path, normalize_semver(scenario_version))
        if version_path.exists():
            return version_path, load_scenario_record(version_path, scenarios_dir, storage_dir_override=descriptor_path.parent)
    return descriptor_path, load_scenario_record(descriptor_path, scenarios_dir)


def scenario_public_asset_url(asset_path: str | Path) -> str:
    relative = Path(asset_path).as_posix().lstrip("/")
    return f"{SCENARIO_PUBLIC_ROUTE_PREFIX}/{relative}"


def scenario_draft_path(drafts_dir: Path, owner_id: Any) -> Path:
    """用户草稿文件路径；用户 ID 中的非法字符统一替换，避免路径穿越。"""
    owner = re.sub(r"[^A-Za-z0-9_.-]", "_", str(owner_id or "anonymous"))
    return Path(drafts_dir) / f"{owner}.json"


def load_scenario_draft(drafts_dir: Path, owner_id: Any) -> dict[str, Any] | None:
    """读取用户草稿；不存在时返回 None。"""
    return read_json(scenario_draft_path(drafts_dir, owner_id))


def save_scenario_draft_file(drafts_dir: Path, owner_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """原子写入用户草稿并返回写入内容。"""
    write_json_atomic(scenario_draft_path(drafts_dir, owner_id), payload)
    return payload


def delete_scenario_draft_file(drafts_dir: Path, owner_id: Any) -> bool:
    """删除用户草稿；返回是否真的删除了文件。"""
    path = scenario_draft_path(drafts_dir, owner_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def _coerce_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _coerce_bool(value: Any, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return fallback


def _module_type(value: Any) -> str:
    module_type = str(value or "custom").strip().lower()
    return module_type if module_type in SCENARIO_MODULE_TYPES else "custom"


def _module_default_title(module_type: str, index: int = 1) -> str:
    return {
        "opening": "导入模块",
        "background": "背景",
        "public_info": "公开信息",
        "preparation": "游戏准备",
        "timeline": "时间线",
        "scene": "场景",
        "ending": "结局",
        "monster": "怪物信息",
        "npc": "NPC信息",
        "custom": "自定义模块",
    }.get(module_type, "自定义模块") + str(max(1, index))


def _module_type_label(module_type: str) -> str:
    return _module_default_title(module_type, 1)[:-1]


def _is_generated_module_title(title: str, module_type: str) -> bool:
    normalized = str(title or "").strip()
    if not normalized or re.fullmatch(r"模块\s*\d*", normalized):
        return True
    label = _module_type_label(module_type)
    if normalized == label or re.fullmatch(re.escape(label) + r"\s*\d+", normalized):
        return True
    return module_type == "custom" and re.fullmatch(r"自定义模块\s*\d*", normalized) is not None


def _module_default_visibility(module_type: str) -> str:
    return "kp" if module_type in {"background", "opening"} else "public"


def _module_code_prefix(module_type: str) -> str | None:
    if module_type == "monster":
        return "M"
    if module_type == "npc":
        return "N"
    return None


def generate_public_id(existing_ids: set[str]) -> str:
    """生成 6 位剧本公开编号；全局唯一的实现，调用方不要各自再写一份。"""
    for _ in range(200):
        value = "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(6))
        if value not in existing_ids:
            return value
    raise RuntimeError("Failed to generate unique scenario public id")


def _generate_module_code(prefix: str, existing_codes: set[str] | None = None) -> str:
    existing = existing_codes or set()
    for _ in range(200):
        value = "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(6))
        code = f"{prefix}-{value}"
        if code not in existing:
            return code
    raise RuntimeError("Failed to generate unique scenario module code")


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ValueError("Invalid trigger attachment data")

    try:
        data = base64.b64decode(match.group("data"), validate=True)
    except binascii.Error as exc:
        raise ValueError("Invalid trigger attachment data") from exc
    return data, match.group("mime")


def trigger_size_limit(config_dir: Path | None = None) -> int:
    if not config_dir:
        return DEFAULT_TRIGGER_SIZE_LIMIT
    config_path = Path(config_dir) / "general.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_TRIGGER_SIZE_LIMIT
    try:
        return int((config.get("scenario") or {}).get("trigger_max_file_size", DEFAULT_TRIGGER_SIZE_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_TRIGGER_SIZE_LIMIT


def _trigger_asset_filename(scene_id: int | str, trigger_id: int | str, asset_name: str) -> str:
    base_name = normalize_filename(asset_name or f"trigger-{scene_id}-{trigger_id}")
    if "." not in base_name:
        return f"{base_name}.bin"
    return base_name


def _normalize_custom_inputs(value: Any, include_content: bool = True) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {
            "id": str(item.get("id") or item.get("input_id") or len(normalized) + 1),
            "label": str(item.get("label") or item.get("name") or "").strip(),
            "value": str(item.get("value") or "").strip(),
            "send_to_ai": _coerce_bool(item.get("send_to_ai"), False),
        }
        if not include_content:
            entry["value"] = ""
        if not entry["label"] and not entry["value"]:
            continue
        normalized.append(entry)
    return normalized


def normalize_trigger(trigger: dict[str, Any], storage_dir: Path | None = None, include_content: bool = True) -> dict[str, Any] | None:
    if not isinstance(trigger, dict):
        return None

    trigger_id = _coerce_int(trigger.get("id") or trigger.get("trigger_id") or trigger.get("triggerId"), 0)
    keyword = str(trigger.get("keyword") or trigger.get("trigger_word") or trigger.get("triggerWord") or "").strip()
    if trigger_id < 0 or not keyword:
        return None

    content_mode = str(trigger.get("content_mode") or trigger.get("contentType") or trigger.get("content_type") or "text").strip().lower()
    content = str(trigger.get("content") or trigger.get("body") or "").strip()
    display_name = str(
        trigger.get("display_name")
        or trigger.get("displayName")
        or trigger.get("name")
        or ""
    ).strip()
    condition = str(
        trigger.get("condition")
        or trigger.get("requirement")
        or trigger.get("trigger_condition")
        or ""
    ).strip()
    asset_name = str(trigger.get("asset_name") or trigger.get("file_name") or "").strip()
    asset_path = str(trigger.get("asset_path") or trigger.get("assetPath") or "").strip()
    asset_url = str(trigger.get("asset_url") or trigger.get("assetUrl") or "").strip()
    asset_mime = str(trigger.get("asset_mime") or trigger.get("mime_type") or "").strip()
    asset_size = _coerce_int(trigger.get("asset_size") or trigger.get("file_size"), 0)

    normalized = {
        "id": trigger_id,
        "keyword": keyword,
        "content_mode": content_mode if content_mode in {"text", "richtext", "image", "file"} else "text",
    }
    if any(key in trigger for key in ("spoiler_level", "spoilerLevel")):
        normalized["spoiler_level"] = max(0, min(5, _coerce_int(trigger.get("spoiler_level") or trigger.get("spoilerLevel"), 0)))
    if "visibility" in trigger:
        normalized["visibility"] = str(trigger.get("visibility") or "player_visible") if str(trigger.get("visibility") or "player_visible") in {"kp_only", "player_visible"} else "player_visible"
    if "repeatable" in trigger:
        normalized["repeatable"] = _coerce_bool(trigger.get("repeatable"), False)
    if "priority" in trigger:
        normalized["priority"] = _coerce_int(trigger.get("priority"), 0)
    if "enabled" in trigger:
        normalized["enabled"] = _coerce_bool(trigger.get("enabled"), True)
    if display_name:
        normalized["display_name"] = display_name
    if condition:
        normalized["condition"] = condition
    if include_content and content and content_mode in {"text", "richtext"}:
        normalized["content"] = content
    if asset_name:
        normalized["asset_name"] = asset_name
    if asset_path:
        normalized["asset_path"] = asset_path
    if asset_url.startswith(f"{SCENARIO_PUBLIC_ROUTE_PREFIX}/"):
        normalized["asset_url"] = asset_url
    if asset_mime:
        normalized["asset_mime"] = asset_mime
    if asset_size:
        normalized["asset_size"] = asset_size
    return normalized


def _normalize_module(
    module: dict[str, Any],
    storage_dir: Path | None = None,
    include_content: bool = True,
    existing_codes: set[str] | None = None,
    ordinal: int = 1,
) -> dict[str, Any] | None:
    if not isinstance(module, dict):
        return None

    module_type = _module_type(module.get("module_type") or module.get("type"))
    module_id = str(module.get("id") or module.get("module_id") or "").strip()
    if not module_id:
        module_id = f"{module_type}-{int(time.time() * 1000)}"

    raw_title = str(module.get("title") or module.get("name") or module.get("label") or "").strip()
    title = _module_default_title(module_type, ordinal) if _is_generated_module_title(raw_title, module_type) else raw_title
    summary = str(module.get("summary") or module.get("marker") or module.get("brief") or "").strip()
    content = str(module.get("content") or module.get("body") or "").strip()
    notes = str(module.get("notes") or module.get("remark") or "").strip()
    visibility = str(module.get("visibility") or _module_default_visibility(module_type)).strip().lower()
    if visibility not in SCENARIO_MODULE_VISIBILITY:
        visibility = _module_default_visibility(module_type)

    normalized = dict(module)
    normalized["id"] = module_id
    normalized["module_type"] = module_type
    normalized["title"] = title
    normalized["summary"] = summary or content[:120]
    normalized["visibility"] = visibility
    normalized["send_to_ai"] = _coerce_bool(
        module.get("send_to_ai"),
        module_type in {"opening", "background", "public_info", "preparation", "timeline", "scene", "ending", "monster", "npc"},
    )
    if module_type == "opening":
        normalized["fixed_opening"] = _coerce_bool(module.get("fixed_opening") or module.get("fixedOpening"), False)
    if include_content and content:
        normalized["content"] = content
    elif not include_content:
        normalized.pop("content", None)
    if notes:
        normalized["notes"] = notes

    code_prefix = _module_code_prefix(module_type)
    if code_prefix:
        code = str(module.get("code") or module.get("module_code") or "").strip()
        if not code or not re.fullmatch(rf"{code_prefix}-[A-Za-z0-9]{{6}}", code):
            code = _generate_module_code(code_prefix, existing_codes)
        normalized["code"] = code
        if existing_codes is not None:
            existing_codes.add(code)

    if module_type == "scene":
        scene_id = _coerce_int(module.get("scene_id") or module.get("sceneId") or module.get("id"), 0)
        if scene_id <= 0:
            scene_id = int(time.time() * 1000)
        normalized["scene_id"] = scene_id
        triggers = []
        for trigger in module.get("triggers", []) if isinstance(module.get("triggers"), list) else []:
            normalized_trigger = normalize_trigger(trigger, storage_dir, include_content=include_content)
            if normalized_trigger is not None:
                triggers.append(normalized_trigger)
        if triggers:
            normalized["triggers"] = triggers
    attachments = []
    for attachment in module.get("attachments", []) if isinstance(module.get("attachments"), list) else []:
        try:
            attachments.append(normalize_attachment(attachment))
        except ValueError:
            continue
    if attachments:
        normalized["attachments"] = attachments
    elif module_type == "ending":
        normalized["open_ending"] = _coerce_bool(module.get("open_ending") or module.get("openEnding"), False)
    elif module_type == "custom":
        inputs = _normalize_custom_inputs(module.get("inputs") or module.get("fields"), include_content=include_content)
        if inputs:
            normalized["inputs"] = inputs

    return normalized


def _module_from_legacy_scene(scene: dict[str, Any], index: int = 0) -> dict[str, Any] | None:
    if not isinstance(scene, dict):
        return None
    module_id = str(scene.get("module_id") or scene.get("id") or f"scene-{index + 1}").strip()
    raw_triggers = scene.get("triggers") if isinstance(scene.get("triggers"), list) else []
    triggers = []
    for item in raw_triggers:
        normalized_trigger = normalize_trigger(item, include_content=True)
        if normalized_trigger is not None:
            triggers.append(normalized_trigger)
    return {
        "id": module_id,
        "module_type": "scene",
        "title": str(scene.get("title") or f"场景 {index + 1}").strip(),
        "summary": str(scene.get("marker") or scene.get("summary") or "").strip(),
        "content": str(scene.get("content") or "").strip(),
        "triggers": triggers,
        "send_to_ai": True,
        "visibility": "public",
    }


def _module_from_legacy_ending(ending: dict[str, Any], index: int = 0) -> dict[str, Any] | None:
    if not isinstance(ending, dict):
        return None
    module_id = str(ending.get("module_id") or ending.get("id") or f"ending-{index + 1}").strip()
    return {
        "id": module_id,
        "module_type": "ending",
        "title": str(ending.get("title") or f"结局 {index + 1}").strip(),
        "summary": str(ending.get("marker") or ending.get("summary") or "").strip(),
        "content": str(ending.get("content") or "").strip(),
        "send_to_ai": True,
        "visibility": "public",
        "open_ending": _coerce_bool(ending.get("open_ending") or ending.get("openEnding"), False),
    }


def _module_from_text(value: Any, module_type: str, title: str, visibility: str = "public", send_to_ai: bool = True) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    return {
        "id": f"{module_type}-{int(time.time() * 1000)}",
        "module_type": module_type,
        "title": title,
        "summary": text[:120],
        "content": text,
        "visibility": visibility,
        "send_to_ai": send_to_ai,
    }


def _scenario_modules_from_payload(scenario: dict[str, Any], storage_dir: Path | None = None, include_content: bool = True) -> list[dict[str, Any]]:
    existing_codes: set[str] = set()
    modules: list[dict[str, Any]] = []

    raw_modules = scenario.get("modules")
    if isinstance(raw_modules, list) and raw_modules:
        type_counts: dict[str, int] = {}
        for module in raw_modules:
            module_type = _module_type(module.get("module_type") or module.get("type")) if isinstance(module, dict) else "custom"
            type_counts[module_type] = type_counts.get(module_type, 0) + 1
            normalized = _normalize_module(
                module,
                storage_dir,
                include_content=include_content,
                existing_codes=existing_codes,
                ordinal=type_counts[module_type],
            )
            if normalized is not None:
                modules.append(normalized)

    if not modules:
        for value, module_type, title, visibility, send_to_ai in (
            (scenario.get("background"), "background", "背景", "kp", True),
            (scenario.get("public_info"), "public_info", "公开信息", "public", True),
            (scenario.get("preparation"), "preparation", "游戏准备", "public", True),
            (scenario.get("timeline"), "timeline", "时间线", "public", True),
        ):
            module = _module_from_text(value, module_type, title, visibility=visibility, send_to_ai=send_to_ai)
            if module:
                modules.append(module)

        for index, scene in enumerate(scenario.get("scenes", []) if isinstance(scenario.get("scenes"), list) else []):
            module = _module_from_legacy_scene(scene, index)
            if module:
                modules.append(module)

        for index, ending in enumerate(scenario.get("endings", []) if isinstance(scenario.get("endings"), list) else []):
            module = _module_from_legacy_ending(ending, index)
            if module:
                modules.append(module)

    normalized_modules = []
    type_counts = {}
    for module in modules:
        module_type = _module_type(module.get("module_type") or module.get("type"))
        type_counts[module_type] = type_counts.get(module_type, 0) + 1
        normalized = _normalize_module(
            module,
            storage_dir,
            include_content=include_content,
            existing_codes=existing_codes,
            ordinal=type_counts[module_type],
        )
        if normalized is not None:
            normalized_modules.append(normalized)
    return normalized_modules


def _scenario_legacy_fields_from_modules(modules: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "background": "",
        "public_info": "",
        "preparation": "",
        "timeline": "",
        "allow_open_ending": False,
    }
    scenes = []
    endings = []
    for module in modules:
        module_type = _module_type(module.get("module_type"))
        content = str(module.get("content") or "").strip()
        if module_type == "background" and content:
            result["background"] = content
        elif module_type == "public_info" and content:
            result["public_info"] = content
        elif module_type == "preparation" and content:
            result["preparation"] = content
        elif module_type == "timeline" and content:
            result["timeline"] = content
        elif module_type == "scene":
            scene = {
                "id": module.get("scene_id") or module.get("id"),
                "content": content,
                "marker": str(module.get("summary") or module.get("marker") or "").strip(),
            }
            if isinstance(module.get("triggers"), list):
                triggers = []
                for trigger in module.get("triggers", []):
                    if isinstance(trigger, dict):
                        normalized_trigger = normalize_trigger(trigger, include_content=True)
                        if normalized_trigger is not None:
                            triggers.append(normalized_trigger)
                if triggers:
                    scene["triggers"] = triggers
            scenes.append(scene)
        elif module_type == "ending":
            endings.append(
                {
                    "id": module.get("ending_id") or module.get("id"),
                    "content": content,
                    "marker": str(module.get("summary") or module.get("marker") or "").strip(),
                }
            )
            result["allow_open_ending"] = result["allow_open_ending"] or _coerce_bool(module.get("open_ending"), False)
    result["scenes"] = scenes
    result["endings"] = endings
    return result


def normalize_scenario_payload(scenario: dict[str, Any], storage_dir: Path | None = None, include_content: bool = True) -> dict[str, Any] | None:
    if not isinstance(scenario, dict):
        return None

    normalized = dict(scenario)
    # 默认封面图已废弃：历史默认路径一律归一化为「无封面」，由前端渲染剧本名文字封面。
    normalized["cover"] = normalize_cover(scenario.get("cover"))
    from trpg_server.agents.versioning import normalize_semver
    normalized["scenario_version"] = normalize_semver(
        scenario.get("scenario_version") or scenario.get("version") or scenario.get("version_id") or "1.0.0"
    )
    modules = _scenario_modules_from_payload(scenario, storage_dir, include_content=include_content)
    legacy_fields = _scenario_legacy_fields_from_modules(modules)
    normalized["modules"] = modules
    normalized["scenes"] = legacy_fields["scenes"]
    normalized["endings"] = legacy_fields["endings"]
    normalized["background"] = legacy_fields["background"]
    normalized["public_info"] = legacy_fields["public_info"]
    normalized["preparation"] = legacy_fields["preparation"]
    normalized["timeline"] = legacy_fields["timeline"]
    normalized["allow_open_ending"] = legacy_fields["allow_open_ending"]
    trigger_cards = []
    for card in scenario.get("trigger_cards", []) if isinstance(scenario.get("trigger_cards"), list) else []:
        if not isinstance(card, dict):
            continue
        card_copy = dict(card)
        card_copy["id"] = str(card.get("id") or card.get("trigger_id") or "").strip()
        card_copy["scriptId"] = str(card.get("scriptId") or card.get("script_id") or normalized.get("id") or "")
        card_copy["scriptVersion"] = str(card.get("scriptVersion") or card.get("script_version") or normalized.get("scenario_version") or "1.0.0")
        card_copy["cardType"] = "trigger"
        card_copy["attachments"] = []
        for attachment in card.get("attachments", []) if isinstance(card.get("attachments"), list) else []:
            try:
                card_copy["attachments"].append(normalize_attachment(attachment))
            except ValueError:
                continue
        if card_copy["id"] and card_copy["attachments"]:
            trigger_cards.append(card_copy)
    if trigger_cards:
        normalized["trigger_cards"] = trigger_cards
    return normalized


def _iter_modules(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    modules = scenario.get("modules")
    if isinstance(modules, list):
        return [module for module in modules if isinstance(module, dict)]
    return []


def iter_scenario_trigger_catalog(scenario: dict[str, Any], scene_id: int | str | None = None) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    expected_scene_id = str(scene_id) if scene_id not in (None, "") else None
    for module in _iter_modules(scenario):
        if _module_type(module.get("module_type")) != "scene":
            continue
        current_scene_id = str(module.get("scene_id") or module.get("id"))
        if expected_scene_id and current_scene_id != expected_scene_id:
            continue
        for trigger in module.get("triggers", []) if isinstance(module.get("triggers"), list) else []:
            if not isinstance(trigger, dict):
                continue
            item = {
                "id": trigger.get("id"),
                "scene_id": module.get("scene_id") or module.get("id"),
                "module_id": module.get("id"),
                "module_title": module.get("title"),
                "display_name": trigger.get("display_name") or trigger.get("name"),
                "keyword": trigger.get("keyword"),
                "condition": trigger.get("condition") or trigger.get("requirement"),
                "content_mode": trigger.get("content_mode", "text"),
            }
            if trigger.get("asset_name"):
                item["asset_name"] = trigger["asset_name"]
            if trigger.get("asset_url"):
                item["asset_url"] = trigger["asset_url"]
            triggers.append(item)
        for attachment in module.get("attachments", []) if isinstance(module.get("attachments"), list) else []:
            if not isinstance(attachment, dict):
                continue
            condition = attachment.get("condition") or {}
            resource = attachment.get("resourceRef") or {}
            triggers.append({
                "id": attachment.get("triggerId"),
                "scene_id": module.get("scene_id") or module.get("id"),
                "module_id": module.get("id"),
                "display_name": resource.get("alt") or attachment.get("triggerId"),
                "keyword": condition.get("keyword") or "",
                "condition": condition.get("naturalLanguage") or condition,
                "content_mode": "attachment",
                "spoiler_level": attachment.get("spoilerLevel", 0),
                "visibility": attachment.get("visibility", "player_visible"),
            })

    if triggers:
        return triggers

    for scene in scenario.get("scenes", []) if isinstance(scenario.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        if expected_scene_id and str(scene.get("id")) != expected_scene_id:
            continue
        for trigger in scene.get("triggers", []) if isinstance(scene.get("triggers"), list) else []:
            if not isinstance(trigger, dict):
                continue
            item = {
                "id": trigger.get("id"),
                "scene_id": scene.get("id"),
                "display_name": trigger.get("display_name") or trigger.get("name"),
                "keyword": trigger.get("keyword"),
                "condition": trigger.get("condition") or trigger.get("requirement"),
                "content_mode": trigger.get("content_mode", "text"),
            }
            if trigger.get("asset_name"):
                item["asset_name"] = trigger["asset_name"]
            if trigger.get("asset_url"):
                item["asset_url"] = trigger["asset_url"]
            triggers.append(item)

    for card in scenario.get("trigger_cards", []) if isinstance(scenario.get("trigger_cards"), list) else []:
        if not isinstance(card, dict):
            continue
        card_scene = card.get("sceneId") or card.get("scene_id")
        if expected_scene_id and card_scene not in (None, "") and str(card_scene) != expected_scene_id:
            continue
        triggers.append({
            "id": card.get("id"),
            "scene_id": card_scene,
            "module_id": card.get("id"),
            "display_name": card.get("text") or card.get("id"),
            "keyword": card.get("unlockCondition") or "",
            "condition": card.get("unlockCondition"),
            "content_mode": "attachment",
            "spoiler_level": card.get("spoilerLevel", 0),
            "visibility": card.get("visibility", "player_visible"),
        })
    return triggers


def load_scenario_record(
    descriptor_path: Path,
    scenarios_dir: Path,
    storage_dir_override: Path | None = None,
) -> dict[str, Any]:
    scenario = read_json(descriptor_path, default={})
    storage_dir = Path(storage_dir_override) if storage_dir_override else scenario_storage_dir(descriptor_path)
    normalized = normalize_scenario_payload(scenario, storage_dir, include_content=True) or {}

    for module in normalized.get("modules", []):
        if not isinstance(module, dict) or _module_type(module.get("module_type")) != "scene":
            continue
        triggers = []
        for trigger in module.get("triggers", []) if isinstance(module.get("triggers"), list) else []:
            if not isinstance(trigger, dict):
                continue
            asset_path = str(trigger.get("asset_path") or "").strip()
            if asset_path:
                asset_full_path = safe_join(storage_dir, asset_path)
                if asset_full_path.exists():
                    trigger["asset_url"] = scenario_public_asset_url(asset_full_path.relative_to(scenarios_dir))
            triggers.append(trigger)
        if triggers:
            module["triggers"] = triggers

    legacy_fields = _scenario_legacy_fields_from_modules(normalized.get("modules", []))
    normalized["scenes"] = legacy_fields["scenes"]
    normalized["endings"] = legacy_fields["endings"]
    normalized["background"] = legacy_fields["background"]
    normalized["public_info"] = legacy_fields["public_info"]
    normalized["preparation"] = legacy_fields["preparation"]
    normalized["timeline"] = legacy_fields["timeline"]
    normalized["allow_open_ending"] = legacy_fields["allow_open_ending"]
    return normalized


def _normalize_trigger_asset_path(storage_dir: Path, trigger: dict[str, Any], size_limit: int = DEFAULT_TRIGGER_SIZE_LIMIT) -> Path | None:
    asset_data = str(trigger.get("asset_data_url") or trigger.get("asset_data") or "").strip()
    if not asset_data:
        return None

    raw_bytes, mime_type = decode_data_url(asset_data)
    if len(raw_bytes) > size_limit:
        raise ValueError(f"Trigger attachment exceeds size limit: {size_limit} bytes")
    if not trigger.get("asset_name"):
        extension = mimetypes.guess_extension(mime_type or "") or ".bin"
        trigger["asset_name"] = f"trigger-{trigger.get('id', 'asset')}{extension}"

    trigger_content_dir = storage_dir / SCENARIO_TRIGGER_DIR_NAME
    trigger_content_dir.mkdir(parents=True, exist_ok=True)
    asset_filename = _trigger_asset_filename(trigger.get("scene_id") or "scene", trigger.get("id") or "trigger", str(trigger["asset_name"]))
    asset_path = trigger_content_dir / asset_filename
    asset_path.write_bytes(raw_bytes)
    trigger["asset_path"] = f"{SCENARIO_TRIGGER_DIR_NAME}/{asset_filename}"
    trigger["asset_size"] = len(raw_bytes)
    trigger["asset_mime"] = trigger.get("asset_mime") or mime_type
    trigger.pop("asset_data_url", None)
    trigger.pop("asset_data", None)
    return asset_path


def save_scenario_record(
    scenarios_dir: Path,
    scenario: dict[str, Any],
    existing_descriptor: Path | None = None,
    trigger_max_file_size: int = DEFAULT_TRIGGER_SIZE_LIMIT,
) -> Path:
    scenario_id = _coerce_int(scenario.get("id"), 0)
    if scenario_id <= 0:
        raise ValueError("Scenario ID is required")

    if existing_descriptor and existing_descriptor.parent != scenarios_dir:
        storage_dir = existing_descriptor.parent
    else:
        storage_dir = safe_join(scenarios_dir, f"scenario-{scenario_id}")

    storage_dir.mkdir(parents=True, exist_ok=True)
    normalized = normalize_scenario_payload(scenario, storage_dir, include_content=True) or {}

    modules = normalized.get("modules", [])
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict) or _module_type(module.get("module_type")) != "scene":
                continue
            triggers = []
            for trigger in module.get("triggers", []) if isinstance(module.get("triggers"), list) else []:
                if not isinstance(trigger, dict):
                    continue
                if trigger.get("content_mode") in {"image", "file"} and trigger.get("asset_data_url"):
                    _normalize_trigger_asset_path(storage_dir, trigger, trigger_max_file_size)
                elif trigger.get("asset_url") and not trigger.get("asset_path"):
                    asset_url = str(trigger["asset_url"])
                    if asset_url.startswith(f"{SCENARIO_PUBLIC_ROUTE_PREFIX}/"):
                        relative = asset_url[len(SCENARIO_PUBLIC_ROUTE_PREFIX) + 1 :]
                        asset_file = safe_join(scenarios_dir, relative)
                        if asset_file.is_relative_to(storage_dir) and asset_file.exists() and asset_file.is_file():
                            trigger["asset_path"] = asset_file.relative_to(storage_dir).as_posix()
                triggers.append(trigger)
            module["triggers"] = triggers

    legacy_fields = _scenario_legacy_fields_from_modules(normalized.get("modules", []))
    normalized["scenes"] = legacy_fields["scenes"]
    normalized["endings"] = legacy_fields["endings"]
    normalized["background"] = legacy_fields["background"]
    normalized["public_info"] = legacy_fields["public_info"]
    normalized["preparation"] = legacy_fields["preparation"]
    normalized["timeline"] = legacy_fields["timeline"]
    normalized["allow_open_ending"] = legacy_fields["allow_open_ending"]

    descriptor_path = storage_dir / SCENARIO_DESCRIPTOR_NAME
    if existing_descriptor and existing_descriptor != descriptor_path and existing_descriptor.exists():
        existing_descriptor.unlink()

    write_json_atomic(descriptor_path, normalized)
    version_path = scenario_version_path(descriptor_path, normalized.get("scenario_version") or "1")
    version_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(version_path, normalized)
    # 仅在知识索引缺失时按 modules 兜底构建一次（首次保存或新版本）。已有索引由
    # 调用方通过带 embedding 的 _index_scenario_knowledge 维护；这里若重建会写入
    # 无向量内容，导致「仅修改基础信息」时向量通道失效。直接导入的剧本没有场景
    # 卡，其知识块由导入管线单独写入，同样不能在此按空 modules 覆盖。
    if normalized.get("modules") or str(normalized.get("import_mode") or "") != "direct":
        from trpg_server.agents.knowledge_base import knowledge_index_path, persist_knowledge_index
        if not knowledge_index_path(descriptor_path, normalized.get("scenario_version") or "1").exists():
            persist_knowledge_index(descriptor_path, normalized)
    return descriptor_path


def delete_scenario_record(descriptor_path: Path) -> None:
    if descriptor_path.is_dir():
        for child in sorted(descriptor_path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        descriptor_path.rmdir()
        return

    storage_dir = descriptor_path.parent
    if descriptor_path.exists():
        descriptor_path.unlink()
    if storage_dir.name.startswith("scenario-") and storage_dir.exists():
        for child in sorted(storage_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        if storage_dir.exists():
            storage_dir.rmdir()


def _iter_scene_sources(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    modules = scenario.get("modules")
    if isinstance(modules, list):
        scenes = [module for module in modules if isinstance(module, dict) and _module_type(module.get("module_type")) == "scene"]
        if scenes:
            return scenes
    return [scene for scene in scenario.get("scenes", []) if isinstance(scene, dict)]


def find_trigger_by_id(scenario: dict[str, Any], trigger_id: int | str) -> dict[str, Any] | None:
    expected = str(trigger_id)
    for scene in _iter_scene_sources(scenario):
        for trigger in scene.get("triggers", []) if isinstance(scene.get("triggers"), list) else []:
            if not isinstance(trigger, dict):
                continue
            if str(trigger.get("id")) == expected:
                return {**trigger, "scene_id": scene.get("scene_id") or scene.get("id"), "module_id": scene.get("id")}
    return None


def render_trigger_content(scenario: dict[str, Any], trigger_id: int | str) -> dict[str, Any] | None:
    trigger = find_trigger_by_id(scenario, trigger_id)
    if not trigger:
        return None

    content_mode = str(trigger.get("content_mode") or "text")
    asset_url = str(trigger.get("asset_url") or "")
    asset_name = str(trigger.get("asset_name") or "")
    if content_mode in {"image", "file"} and asset_url:
        if content_mode == "image":
            content = f"![{asset_name or trigger.get('keyword')}]({asset_url})"
        else:
            content = f"[{asset_name or trigger.get('keyword')}]({asset_url})"
    else:
        content = str(trigger.get("content") or "")

    return {
        "id": trigger.get("id"),
        "scene_id": trigger.get("scene_id"),
        "module_id": trigger.get("module_id"),
        "display_name": trigger.get("display_name") or trigger.get("name"),
        "keyword": trigger.get("keyword"),
        "condition": trigger.get("condition"),
        "content_mode": content_mode,
        "content": content,
        "asset_name": asset_name or None,
        "asset_url": asset_url or None,
        "asset_mime": trigger.get("asset_mime"),
        "asset_size": trigger.get("asset_size"),
    }


def build_trigger_message(scenario: dict[str, Any], trigger_id: int | str) -> dict[str, Any] | None:
    trigger = render_trigger_content(scenario, trigger_id)
    if not trigger:
        return None

    display_name = str(trigger.get("display_name") or trigger.get("name") or "").strip()
    keyword = str(trigger.get("keyword") or "").strip()
    content = str(trigger.get("content") or "").strip()
    asset_name = str(trigger.get("asset_name") or "").strip()
    content_mode = str(trigger.get("content_mode") or "text")

    return {
        "type": "trigger",
        "sender_name": display_name or f"触发器{trigger_id}",
        "avatar": "",
        "content": content,
        "metadata": {
            "trigger_id": trigger.get("id"),
            "trigger_name": display_name or f"触发器{trigger_id}",
            "trigger_keyword": keyword,
            "trigger_condition": trigger.get("condition"),
            "scene_id": trigger.get("scene_id"),
            "module_id": trigger.get("module_id"),
            "content_mode": content_mode,
            "asset_name": asset_name or None,
            "asset_url": trigger.get("asset_url"),
            "asset_mime": trigger.get("asset_mime"),
            "asset_size": trigger.get("asset_size"),
        },
    }
