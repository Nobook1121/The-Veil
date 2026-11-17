"""通用判定结果信封（E0 判定壳）。

边界：

- 核心只负责发起、掷骰、记录、展示与写回；掷骰数学、目标值、成功等级都由
  规则集插件给。
- ``CheckOutcome`` 不假设 d100 / d20 / 骰池。``dice`` 记录骰面，``degree`` 是
  插件定义的字符串，核心不排序、不解释。
- ``CheckResolution`` 是历史 CoC 形状（``roll/target/level/success``），继续
  保留以兼容现有调用方；``from_resolution()`` 只做单向转换，不改旧行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CHECK_OUTCOME_VERSION = 1


@dataclass(frozen=True)
class CheckOutcome:
    """一次判定的通用结果；字段全部可选，缺失表示该规则不提供此信息。"""

    ruleset_id: str = ""
    kind: str = "check"  # check / attack / save / opposed / custom
    dice: tuple[int, ...] = ()
    total: int | None = None
    target: int | None = None
    margin: int | None = None
    # 插件定义的等级字符串（regular/hard/extreme、四度成功、成功数……）；
    # 核心不比较大小，只原样传递。
    degree: str = ""
    success: bool | None = None
    # 判定产生的通用效果请求；具体语义由插件与效果壳解释。
    effects: tuple[dict[str, Any], ...] = ()
    state_patch: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    # 展示信封：summary / label / rolls_text / severity 等，核心只透传。
    presentation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_version": CHECK_OUTCOME_VERSION,
            "ruleset_id": self.ruleset_id,
            "kind": self.kind,
            "dice": list(self.dice),
            "total": self.total,
            "target": self.target,
            "margin": self.margin,
            "degree": self.degree,
            "success": self.success,
            "effects": [dict(item) for item in self.effects],
            "state_patch": dict(self.state_patch),
            "detail": dict(self.detail),
            "presentation": dict(self.presentation),
        }


def from_resolution(
    resolution: Any,
    *,
    ruleset_id: str = "",
    kind: str = "check",
) -> CheckOutcome:
    """把历史 ``CheckResolution`` 转成通用信封。

    只搬运既有事实，不推断新语义：``margin`` 保持 ``None``，成功等级原样放进
    ``degree``。这样 CoC7 的输出可以无损进入新信封，同时保留旧字段给现有调用方。
    """
    detail = getattr(resolution, "detail", None)
    detail = dict(detail) if isinstance(detail, Mapping) else {}
    state_patch = getattr(resolution, "state_patch", None)
    state_patch = dict(state_patch) if isinstance(state_patch, Mapping) else {}
    roll = getattr(resolution, "roll", None)
    presentation: dict[str, Any] = {}
    label = str(detail.get("display_name") or "").strip()
    if label:
        presentation["label"] = label
    if roll is not None:
        presentation["rolls_text"] = str(roll)
    return CheckOutcome(
        ruleset_id=ruleset_id,
        kind=kind,
        dice=(int(roll),) if isinstance(roll, int) else (),
        total=int(roll) if isinstance(roll, int) else None,
        target=int(resolution.target) if getattr(resolution, "target", None) is not None else None,
        degree=str(getattr(resolution, "level", "") or ""),
        success=getattr(resolution, "success", None),
        state_patch=state_patch,
        detail=detail,
        presentation=presentation,
    )


def coerce_outcome(value: Any, *, ruleset_id: str = "", kind: str = "check") -> CheckOutcome:
    """把插件返回的 ``CheckOutcome`` / ``CheckResolution`` / 字典统一成信封。"""
    if isinstance(value, CheckOutcome):
        return value
    if hasattr(value, "roll") and hasattr(value, "success"):
        return from_resolution(value, ruleset_id=ruleset_id, kind=kind)
    if isinstance(value, Mapping):
        return CheckOutcome(
            ruleset_id=str(value.get("ruleset_id") or ruleset_id),
            kind=str(value.get("kind") or kind),
            dice=tuple(int(item) for item in (value.get("dice") or ())),
            total=_optional_int(value.get("total")),
            target=_optional_int(value.get("target")),
            margin=_optional_int(value.get("margin")),
            degree=str(value.get("degree") or value.get("level") or ""),
            success=value.get("success") if isinstance(value.get("success"), bool) else None,
            effects=tuple(dict(item) for item in (value.get("effects") or ()) if isinstance(item, Mapping)),
            state_patch=dict(value.get("state_patch") or {}),
            detail=dict(value.get("detail") or {}),
            presentation=dict(value.get("presentation") or {}),
        )
    raise TypeError("ruleset check provider returned an unsupported outcome")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "CHECK_OUTCOME_VERSION",
    "CheckOutcome",
    "coerce_outcome",
    "from_resolution",
]
