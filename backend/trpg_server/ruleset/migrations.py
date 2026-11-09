"""旧角色卡一次性迁移：补齐规则集信封与 ``sheet``。

迁移只做加法：写入 ``rulesetId/rulesetVersion/rulesetSchemaVersion/sheet``，
原有字段保留为镜像，确保旧前端与历史数据读取路径不会断档。
重复执行是幂等的（已有 ``sheet`` 的卡会跳过）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json, write_json_atomic
from trpg_server.ruleset import get_plugin
from trpg_server.ruleset.capabilities import pack_card, schema_of

logger = logging.getLogger(__name__)


def iter_card_files(directory: Path) -> list[Path]:
    """兼容新目录结构（<id>/character.json）与旧平铺结构（<id>.json）。"""
    base = Path(directory)
    if not base.exists():
        return []
    files: list[Path] = []
    for item in sorted(base.iterdir()):
        if item.is_dir():
            card = item / "character.json"
            if card.exists():
                files.append(card)
        elif item.suffix.lower() == ".json":
            files.append(item)
    return files


def migrate_card_file(path: Path, *, plugin: Any | None = None, dry_run: bool = True) -> dict[str, Any]:
    ruleset = plugin or get_plugin(None)
    result: dict[str, Any] = {"path": str(path), "status": "skipped", "warnings": []}
    raw = read_json(path, default=None)
    if not isinstance(raw, dict):
        result["status"] = "failed"
        result["warnings"] = ["not a JSON object"]
        return result
    if raw.get("rulesetId") and isinstance(raw.get("sheet"), dict):
        result["status"] = "skipped"
        result["rulesetId"] = raw.get("rulesetId")
        return result

    packed = pack_card(ruleset, raw)
    validate = getattr(schema_of(ruleset), "validate", None)
    warnings = list(validate(packed.get("sheet") or {})) if callable(validate) else []
    result.update(
        {
            "status": "migrated" if not dry_run else "would_migrate",
            "rulesetId": packed.get("rulesetId"),
            "warnings": warnings,
        }
    )
    if not dry_run:
        write_json_atomic(path, packed)
    return result


def migrate_character_cards(
    characters_dir: Path,
    *,
    gallery_dir: Path | None = None,
    dry_run: bool = True,
    plugin: Any | None = None,
) -> dict[str, Any]:
    """迁移角色卡与角色卡广场；返回可直接打印/记录的统计。"""
    ruleset = plugin or get_plugin(None)
    targets: list[Path] = iter_card_files(Path(characters_dir))
    if gallery_dir:
        targets.extend(iter_card_files(Path(gallery_dir)))

    results: list[dict[str, Any]] = []
    warning_count = 0
    for path in targets:
        item = migrate_card_file(path, plugin=ruleset, dry_run=dry_run)
        warning_count += len(item["warnings"])
        if item["warnings"]:
            logger.warning("Character card migration warning for %s: %s", path, "; ".join(item["warnings"]))
        results.append(item)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "dry_run": dry_run,
        "ruleset_id": getattr(ruleset, "id", ""),
        "scanned": len(targets),
        "counts": counts,
        "warning_count": warning_count,
        "items": results,
    }
