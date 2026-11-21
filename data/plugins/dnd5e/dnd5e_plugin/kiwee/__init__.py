"""kiwee 内容深库（5etools 中文镜像）——dnd5e 插件的资料子模块。

架构定位（docs/plans/dnd5e-vtt-refactor-plan.md §4 kiwee 段）：快照脚本 +
SQLite FTS 索引 + datatag 解析器并入 dnd5e 主插件；**消费面三方**
（knowledge.search_ruleset / UI 玩家侧 / 规则引擎）是下一刀，本模块把
服务契约定稳。

服务契约（下一刀消费方照这里接）::

    service = get_service("dnd5e", "kiwee_db")   # 插件缺席 → None
    if service is None: → 走 dnd5e-srd 兜底

    service.search(category, query, *, rules_version=None,
                   observer_is_dm=True, limit=20)
        # 深库未建/不可用 → None（消费方回落 dnd5e-srd）
        # 建好但无命中   → []
        # 命中           → [{name, eng_name, source, page_hint, deep_link,
        #                     translator_ok, is_machine_translated, rules_era,
        #                     category, uid, raw}, ...]
        # observer_is_dm=False → bestiary/variantrule/table 等 DM 侧类目不返回
    service.status()   # {"available": False, "reason": "深库未建", ...} 当快照缺席
    service.categories()  # 类目目录（id/dm_only/page），给工具 schema 用

数据与库文件在 ``data/plugins/dnd5e/kiwee-data/``（本地生成物，不进 git）。
拉取与建库::

    python -m dnd5e_plugin.kiwee snapshot fetch
    python -m dnd5e_plugin.kiwee snapshot build
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .db import (
    CATEGORIES,
    CATEGORY_SPECS,
    DM_ONLY_CATEGORIES,
    KIWEE_SITE,
    KiweeDb,
    build_kiwee_db,
    open_kiwee_db,
)
from .datatags import extract_dice, parse_entries, parse_text, render_plain_text

logger = logging.getLogger(__name__)

RULESET_ID = "dnd5e"
SERVICE_NAME = "kiwee_db"

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
KIWEE_DATA_DIR = PLUGIN_ROOT / "kiwee-data"


def kiwee_data_dir() -> Path:
    """本地生成物根：``data/plugins/dnd5e/kiwee-data/``（gitignore 覆盖）。"""
    return KIWEE_DATA_DIR


def kiwee_db_path() -> Path:
    """默认库文件：``kiwee-data/kiwee.sqlite3``。"""
    return KIWEE_DATA_DIR / "kiwee.sqlite3"


class KiweeDbService:
    """深库服务对象（挂 ``get_service("dnd5e", "kiwee_db")``）。

    每次检索现场只读打开库文件：建库脚本可以随时热替换库，无需重启。
    快照缺席（库文件不存在）时 ``search`` 返回 ``None``、``status``
    给出 ``深库未建``——一切现有功能照常，消费方回落 dnd5e-srd。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else kiwee_db_path()

    # -- 检索 --------------------------------------------------------------

    def search(
        self,
        category: Any = None,
        query: Any = "",
        *,
        rules_version: Any = None,
        observer_is_dm: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]] | None:
        """中英文名/别名检索；**深库未建/打开失败 → None**（降级语义）。

        契约字段与门控规则见模块 docstring；完整定义在 db.KiweeDb.search。
        """
        db: KiweeDb | None = None
        try:
            db = open_kiwee_db(self._db_path)
            if db is None:
                return None
            return db.search(
                category,
                query,
                rules_version=rules_version,
                observer_is_dm=observer_is_dm,
                limit=limit,
            )
        except (sqlite3.Error, ValueError):
            # 坏库/坏入参按不可用降级，不让消费方（KP 回合）挂掉。
            logger.warning("dnd5e_kiwee_search_failed", exc_info=True)
            return None
        finally:
            if db is not None:
                db.close()

    # -- 状态 --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """快照/库是否可用；缺席时 ``{"available": False, "reason": "深库未建"}``。"""
        if not self._db_path.is_file():
            return {
                "available": False,
                "reason": "深库未建",
                "db_path": str(self._db_path),
                "hint": "python -m dnd5e_plugin.kiwee snapshot fetch && snapshot build",
            }
        db: KiweeDb | None = None
        try:
            db = open_kiwee_db(self._db_path)
            if db is None:
                return {
                    "available": False,
                    "reason": "深库未建或版本不匹配（请重新 build）",
                    "db_path": str(self._db_path),
                }
            return db.status()
        except sqlite3.Error:
            logger.warning("dnd5e_kiwee_status_failed", exc_info=True)
            return {"available": False, "reason": "深库打开失败", "db_path": str(self._db_path)}
        finally:
            if db is not None:
                db.close()

    # -- 目录 --------------------------------------------------------------

    def categories(self) -> list[dict[str, Any]]:
        """类目目录（含 DM 门控标记），给消费方工具 schema/枚举用。"""
        return [
            {
                "id": category,
                "dm_only": category in DM_ONLY_CATEGORIES,
                "page": str(spec["page"]),
                "files": list(spec["files"]),
            }
            for category, spec in sorted(CATEGORY_SPECS.items())
        ]


def register_kiwee_db(db_path: str | Path | None = None) -> None:
    """把深库服务挂进插件服务表（幂等：以注册表现状为准）。

    照 house_rule_profiles.register_kp_context 的先例：get_service 探测
    代替模块级开关位（插件测试反复 clear/reload 时 sys.modules 缓存会漏
    注册）；旧核心没有服务表时静默跳过。快照缺席也照常注册——服务对象
    自己带降级语义，建好库即可用，无需重启。
    """
    try:
        from trpg_server.plugins.runtime import get_service, register_service
    except Exception:  # noqa: BLE001 - 旧核心没有服务表时静默跳过
        return
    try:
        if get_service(RULESET_ID, SERVICE_NAME) is not None:
            return
        register_service(RULESET_ID, SERVICE_NAME, KiweeDbService(db_path))
    except Exception:  # noqa: BLE001 - 注册失败不影响插件其余能力
        logger.debug("dnd5e_kiwee_register_failed", exc_info=True)


__all__ = [
    "CATEGORIES",
    "DM_ONLY_CATEGORIES",
    "KIWEE_DATA_DIR",
    "KIWEE_SITE",
    "KiweeDbService",
    "RULESET_ID",
    "SERVICE_NAME",
    "build_kiwee_db",
    "extract_dice",
    "kiwee_data_dir",
    "kiwee_db_path",
    "open_kiwee_db",
    "parse_entries",
    "parse_text",
    "register_kiwee_db",
    "render_plain_text",
]
