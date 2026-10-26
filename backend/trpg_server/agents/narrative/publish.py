"""发布硬闸门：世界树校验与可见性复核（设计文档 §14.2）。

剧本一旦存在 `narrative.json`，作者保存即视为发布。校验错误或未复核的
自动可见性必须清零；允许作者显式强制发布，但会留下 `publish.forced` 日志。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from trpg_server.agents.narrative.store import load_bundle
from trpg_server.agents.narrative.validate import ValidationIssue, validate_bundle

_REVIEW_CODES = frozenset({"visibility.needs_review", "trigger.review_needed"})
_MAX_LISTED = 20


def _issue_payload(issue: ValidationIssue) -> dict[str, Any]:
    return issue.to_dict()


def narrative_publish_blockers(scenario_dir: Path | str) -> dict[str, Any] | None:
    """返回阻止发布的原因；没有世界树或无需阻止时返回 ``None``。"""
    bundle = load_bundle(scenario_dir)
    if bundle is None:
        return None
    report = validate_bundle(bundle)
    review = [issue for issue in report.issues if issue.code in _REVIEW_CODES]
    blocking = [issue for issue in report.issues if issue.level == "error"] + review
    if not blocking:
        return None
    return {
        "errors": report.error_count,
        "warnings": report.warning_count,
        "needs_review": len(review),
        "issues": [_issue_payload(issue) for issue in blocking[:_MAX_LISTED]],
        "truncated": len(blocking) > _MAX_LISTED,
    }
