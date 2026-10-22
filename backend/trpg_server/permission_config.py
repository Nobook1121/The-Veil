from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.settings import CONFIG_DIR

DEFAULT_ROLE_ORDER = ["USER", "ADMIN", "OWNER"]
PERMISSION_CONFIG_FILENAME = "permissions.json"

_GROUPS = {
    "navigation": ["navigation.admin_tabs"],
    "accounts": ["accounts.manage_users", "accounts.auth_settings"],
    "scenarios": [
        "scenarios.create", "scenarios.preview", "scenarios.edit", "scenarios.delete",
        "scenarios.manage_own", "scenarios.manage_all",
    ],
    "characters": [
        "characters.create", "characters.manage_own", "characters.manage_all",
        "characters.gallery.publish", "characters.gallery.apply",
        "characters.gallery.edit", "characters.gallery.delete",
    ],
    "agent": ["agent.use"],
    "settings": [
        "settings.general", "settings.ai_models", "settings.network",
        "settings.character_rules", "settings.permissions", "settings.ai_debug",
        "settings.knowledge_bases",
    ],
    "rooms": ["rooms.create", "rooms.manage_members", "rooms.runtime_records"],
}

DEFAULT_PERMISSION_GROUPS: list[dict[str, Any]] = [
    {
        "id": group_id,
        "label": group_id,
        "description": f"Permissions for {group_id}.",
        "nodes": [
            {"id": node_id, "label": node_id, "description": f"Permission node {node_id}."}
            for node_id in nodes
        ],
    }
    for group_id, nodes in _GROUPS.items()
]

DEFAULT_PERMISSION_MATRIX: dict[str, list[str]] = {
    "navigation.admin_tabs": ["ADMIN", "OWNER"],
    "accounts.manage_users": ["ADMIN", "OWNER"],
    "accounts.auth_settings": ["ADMIN", "OWNER"],
    "scenarios.create": ["USER", "ADMIN", "OWNER"],
    "scenarios.preview": ["USER", "ADMIN", "OWNER"],
    "scenarios.edit": ["USER", "ADMIN", "OWNER"],
    "scenarios.delete": ["USER", "ADMIN", "OWNER"],
    "scenarios.manage_own": ["USER", "ADMIN", "OWNER"],
    "scenarios.manage_all": ["ADMIN", "OWNER"],
    "characters.create": ["USER", "ADMIN", "OWNER"],
    "characters.manage_own": ["USER", "ADMIN", "OWNER"],
    "characters.manage_all": ["ADMIN", "OWNER"],
    "characters.gallery.publish": ["USER", "ADMIN", "OWNER"],
    "characters.gallery.apply": ["USER", "ADMIN", "OWNER"],
    "characters.gallery.edit": ["USER", "ADMIN", "OWNER"],
    "characters.gallery.delete": ["USER", "ADMIN", "OWNER"],
    "agent.use": ["USER", "ADMIN", "OWNER"],
    "settings.general": ["ADMIN", "OWNER"],
    "settings.ai_models": ["ADMIN", "OWNER"],
    "settings.ai_debug": ["ADMIN", "OWNER"],
    "settings.network": ["ADMIN", "OWNER"],
    "settings.character_rules": ["ADMIN", "OWNER"],
    "settings.permissions": ["ADMIN", "OWNER"],
    "settings.knowledge_bases": ["ADMIN", "OWNER"],
    "rooms.create": ["USER", "ADMIN", "OWNER"],
    "rooms.manage_members": ["ADMIN", "OWNER"],
    "rooms.runtime_records": ["USER", "ADMIN", "OWNER"],
}


def permission_config_path(config_dir: Path | None = None) -> Path:
    return Path(config_dir or CONFIG_DIR) / PERMISSION_CONFIG_FILENAME


def permission_node_ids() -> set[str]:
    return set(DEFAULT_PERMISSION_MATRIX)


def default_permission_config() -> dict[str, Any]:
    return {
        "roles": list(DEFAULT_ROLE_ORDER),
        "groups": deepcopy(DEFAULT_PERMISSION_GROUPS),
        "matrix": deepcopy(DEFAULT_PERMISSION_MATRIX),
    }


def normalize_permission_config(data: dict[str, Any] | None) -> dict[str, Any]:
    normalized = default_permission_config()
    if not isinstance(data, dict) or not isinstance(data.get("matrix"), dict):
        return normalized
    for node_id, roles in data["matrix"].items():
        if node_id not in permission_node_ids() or not isinstance(roles, list):
            continue
        normalized["matrix"][node_id] = [
            role for role in DEFAULT_ROLE_ORDER if role in {str(item) for item in roles}
        ]
    return normalized


def load_permission_config(config_path: Path | None = None) -> dict[str, Any]:
    return normalize_permission_config(read_json(config_path or permission_config_path(), default={}))


def save_permission_config(config_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    config = normalize_permission_config(data)
    write_json_atomic(config_path, {"matrix": config["matrix"]})
    return config


def is_role_allowed(role: str, node_id: str, config_path: Path | None = None) -> bool:
    return str(role or "USER") in load_permission_config(config_path)["matrix"].get(node_id, [])
