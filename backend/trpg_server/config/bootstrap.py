"""首次启动生成用户配置。

仓库/发布包只带 ``config/templates/`` 模板；``data/config/`` 在第一次启动时
从模板复制生成，之后永不被自动覆盖（幂等）。模板缺失时按 schema 默认值渲染
一份无注释的最小配置并告警。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import tomlkit

from trpg_server.config import paths
from trpg_server.config.schema import SMALL_MODEL_TASK_KEYS, SMALL_MODELS_SECTION, SPECS

logger = logging.getLogger(__name__)

# (模板相对路径, 目标相对路径)。permissions.json、roles.json、平台配置不预生成：
# 它们在首次保存/创建时写入，读取时走代码默认值。
SEED_FILES: tuple[tuple[str, str], ...] = (
    ("general.toml", "general.toml"),
    ("roles/kp.md", "roles/kp.md"),
    ("roles/debug-kp.md", "roles/debug-kp.md"),
    ("aiplatform/default-request.json", "aiplatform/default-request.json"),
)


def render_minimal_general() -> str:
    """模板缺失时按 schema 默认值渲染最小 general.toml（无注释）。"""
    doc = tomlkit.document()
    for spec in SPECS:
        if spec.default == "":
            continue  # 空字符串默认（如小模型覆盖）不渲染，保持文件最小
        table: tomlkit.items.Table = doc  # type: ignore[assignment]
        for part in spec.section.split("."):
            nxt = table.get(part)
            if not isinstance(nxt, tomlkit.items.Table):
                nxt = tomlkit.table()
                table[part] = nxt
            table = nxt
        table[spec.keys[0]] = spec.default
    small_models = tomlkit.table()
    for task in SMALL_MODEL_TASK_KEYS:
        small_models[task] = ""
    ai = doc.get("ai")
    if not isinstance(ai, tomlkit.items.Table):
        ai = tomlkit.table()
        doc["ai"] = ai
    ai["small_models"] = small_models
    return tomlkit.dumps(doc)


def ensure_user_config(
    config_dir: str | Path | None = None,
    template_dir: str | Path | None = None,
) -> list[Path]:
    """把缺失的用户配置文件从模板复制到配置目录，返回本次新建的文件列表。"""
    resolved_dir = paths.resolve_config_dir(config_dir)
    templates = Path(template_dir) if template_dir else paths.TEMPLATE_DIR
    created: list[Path] = []

    for rel_template, rel_target in SEED_FILES:
        source = templates / rel_template
        target = resolved_dir / rel_target
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copyfile(source, target)
        elif rel_target == paths.GENERAL_CONFIG_FILENAME:
            logger.warning("Template %s missing; rendering minimal defaults from schema", source)
            target.write_text(render_minimal_general(), encoding="utf-8")
        else:
            continue
        created.append(target)
        logger.info("config_seeded file=%s source=template", target)

    return created
