"""5.9 组合技能检定（规则书 p.79）。

只掷一次骰，把同一结果与多个技能比较；守秘人决定要求「全部成功」还是
「任一成功」。不能退化成分别掷骰，否则成功率会按乘法骤降。

本模块只做比较与判定，掷骰由调用方完成。
"""

from __future__ import annotations

from typing import Any, Mapping

COMBINED_MODES = ("all", "any")


def _checks(checks: Any = None) -> Any:
    if checks is not None:
        return checks
    from trpg_server.ruleset.coc7.checks import Coc7Checks

    return Coc7Checks()


def resolve_combined_check(
    card: Mapping[str, Any],
    names: list[str] | tuple[str, ...],
    *,
    roll: int,
    mode: str = "all",
    difficulty: str = "regular",
    checks: Any = None,
) -> dict[str, Any]:
    """用同一个 ``roll`` 对多个技能结算组合检定。

    返回每项技能的目标值、难度阈值与成功等级；``success`` 按 ``mode`` 汇总。
    任一技能不在卡上时抛 ``ValueError``，避免静默跳过导致误判。
    """
    mode_key = str(mode or "all").strip().lower()
    if mode_key not in COMBINED_MODES:
        raise ValueError("mode must be all or any")
    names = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not names:
        raise ValueError("at least one skill is required")
    roll_value = max(1, min(100, int(roll)))

    provider = _checks(checks)
    rows: list[dict[str, Any]] = []
    for name in names:
        value = provider.lookup_target(card, name)
        if value is None:
            raise ValueError(f"{name} was not found on character card")
        value = max(0, int(value))
        threshold = int(provider.threshold(value, difficulty))
        level = str(provider.success_level(roll_value, value))
        success = (roll_value <= threshold or level == "critical") and level != "fumble"
        rows.append(
            {
                "name": name,
                "value": value,
                "threshold": threshold,
                "level": level,
                "success": success,
            }
        )

    overall = all(row["success"] for row in rows) if mode_key == "all" else any(
        row["success"] for row in rows
    )
    return {
        "roll": roll_value,
        "mode": mode_key,
        "difficulty": difficulty,
        "success": overall,
        "skills": rows,
    }
