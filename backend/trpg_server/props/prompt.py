"""道具相关的 prompt 片段（设计稿 §6.2 / §6.3 / §6.6）。

只负责拼文本，不做 IO；由 ``turn_pipeline`` 在每次请求时按房间状态调用，
关闭道具工坊时不产生任何注入。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import policy

MAX_CONTEXT_PROPS = 3
MAX_CONTEXT_CHARS = 1200

BASE_DIRECTIVE = (
    "【道具】\n"
    "你可以把线索做成实物：调用 props.create 填写模板字段创建道具，再用 props.reveal 发给玩家。\n"
    "- 优先在建团/备团阶段准备好关键线索道具；游戏中临时创建也可以。\n"
    "- 道具上的文字要像那个年代的真实文件：有称呼、落款、编号、错别字、被烧掉的段落。\n"
    "- 用 redaction 烧掉/涂黑不该被玩家看到的信息，而不是写在正文里再要求玩家忽略。\n"
    "- 一次回合内最多创建 1 件道具；已存在的信息不要重复造道具。"
)


def build_props_directive(
    settings: Mapping[str, Any] | None,
    templates: Iterable[Mapping[str, Any]] | None,
    specs: Iterable[Mapping[str, Any]] | None,
) -> str:
    """拼接 AI 侧道具指令；关闭时返回空串。"""
    if not isinstance(settings, Mapping) or settings.get("enabled", True) is False:
        return ""

    template_list = [
        f"{item.get('id')}({item.get('name') or item.get('id')})"
        for item in (templates or [])
        if isinstance(item, Mapping) and item.get("id")
    ]
    if not template_list:
        # 模板全坏时也保留工具说明，并明确告知模型不要创建。
        template_list = ["（当前没有可用模板，禁止调用 props.create）"]

    sections = [BASE_DIRECTIVE, "模板：" + " / ".join(template_list)]
    context = _revealed_context(specs)
    if context:
        sections.append(context)
    return "\n".join(sections)


def _revealed_context(specs: Iterable[Mapping[str, Any]] | None) -> str:
    """只拼已揭示道具，字段统一走 ``apply_redaction``（AI 侧 4 个方块）。"""
    rows: list[str] = []
    used = 0
    for spec in _revealed_specs(specs):
        spec = policy.apply_redaction(spec, mask=policy.AI_MASK)
        fields = spec.get("fields") if isinstance(spec.get("fields"), Mapping) else {}
        lines = [f"- 《{spec.get('title') or '未命名'}》{spec.get('id')}（模板 {spec.get('template_id')}）"]
        for key, value in fields.items():
            text = str(value or "").strip()
            if not text:
                continue
            lines.append(f"  {key}: {text}")
        block = "\n".join(lines)
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        rows.append(block)
        used += len(block)
        if len(rows) >= MAX_CONTEXT_PROPS:
            break
    if not rows:
        return ""
    return (
        "【已揭示道具（仅可据此解读；████ 处不得编造）】\n"
        + "\n".join(rows)
        + "\n玩家问起道具内容时，只描述上面能看到的部分；不得编造被烧毁/涂黑部分的内容。"
    )


def _revealed_specs(specs: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for spec in specs or []:
        if not isinstance(spec, Mapping):
            continue
        if policy.visibility_mode(spec) == "hidden":
            continue
        result.append(spec)
    result.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return result


__all__ = ["BASE_DIRECTIVE", "MAX_CONTEXT_CHARS", "MAX_CONTEXT_PROPS", "build_props_directive"]
