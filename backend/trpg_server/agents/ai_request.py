"""AI 请求的统一构造：请求模板合并与输出长度策略。

集中处理两件此前散落各处的事：

1. **请求模板真正生效**。``data/config/aiplatform/default-request.json`` 与
   ``data/config/aimodel/<平台>/<模型>.json`` 以前只被设置页读写、运行时不参与请求
   构造。现在由 :func:`merge_request_template` 把模板当作「默认参数」并入真实请求；
   调用方显式传入的字段（messages / model / temperature 等）始终优先，模板只补齐
   调用方未声明的可调参数（stop、extra_body、frequency_penalty……）。

2. **输出长度只有一处开关**。``max_tokens`` 不再硬编码在各个调用点，而由管理员
   设置页的「最大输出 Token（全局）」统一决定，优先级为：

   1. **请求模板 / 调用方显式声明** → 用户「自行更改」，以它为准；
   2. 否则若开启「约束最大输出 token」→ 下发管理员设置的全局值（所有平台一致的默认）；
   3. 否则不下发 ``max_tokens``，把上限交给平台自身。

   这既满足「平台默认遵循管理员设置」，也满足「请求模板是真实请求体、改动真正生效」。

探针类请求只需要极小输出（连通性检查），其固定值集中在 :data:`PROBE_MAX_TOKENS`，
避免这个魔数再次散落到各处。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from trpg_server.json_store import read_json
from trpg_server.settings import CONFIG_DIR

# 探针 / 连通性测试只需确认「能拿到响应」，输出预算固定取极小值即可。
PROBE_MAX_TOKENS = 16

DEFAULT_REQUEST_FILENAME = "default-request.json"

# 这些键由运行时（调用方）决定，绝不允许被请求模板覆盖：
# ``messages``/``model`` 是每轮请求的实体内容；``tools``/``tool_choice`` 由工具调用
# 流程注入；``stream`` 关系到响应解析方式（本服务按非流式解析），模板误设会直接破坏
# 响应处理。其余键一律以模板为默认、调用方为准。
RUNTIME_RESERVED_KEYS = frozenset({"model", "messages", "tools", "tool_choice", "stream"})


def _resolve_config_dir(config_dir: str | Path | None) -> Path:
    if config_dir is None:
        from flask import has_app_context

        if has_app_context():  # 仅在 Web 上下文中才依赖 Flask
            from flask import current_app

            config_dir = current_app.config.get("CONFIG_DIR", CONFIG_DIR)
        else:
            config_dir = CONFIG_DIR
    return Path(config_dir)


def load_request_template(
    config_dir: str | Path | None,
    platform: str | None,
    model: str | None,
) -> dict[str, Any]:
    """读取「默认请求模板 + 模型请求模板」合并后的请求参数。

    默认模板来自 ``aiplatform/default-request.json``，模型模板来自
    ``aimodel/<平台>/<模型>.json``，后者覆盖前者。文件缺失或非法时按空对象处理，
    不因模板问题阻断请求。
    """
    root = _resolve_config_dir(config_dir)
    template: dict[str, Any] = {}
    default_file = root / "aiplatform" / DEFAULT_REQUEST_FILENAME
    default_template = read_json(default_file, default={})
    if isinstance(default_template, dict):
        template.update(default_template)

    if platform and model:
        model_file = root / "aimodel" / str(platform) / f"{model}.json"
        model_template = read_json(model_file, default={})
        if isinstance(model_template, dict):
            template.update(model_template)
    return template


def apply_max_tokens_policy(payload: dict[str, Any], runtime_config: Any) -> dict[str, Any]:
    """决定本次请求下发的 ``max_tokens``。

    优先级：请求模板/调用方已显式声明 → 原样保留（用户「自行更改」优先）；
    否则若开启「约束最大输出 token」→ 下发管理员设置的全局值；
    再否则移除该键，把输出长度的决定权交还平台。
    """
    try:
        explicit = int(payload.get("max_tokens"))
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        payload["max_tokens"] = explicit
        return payload
    if bool(getattr(runtime_config, "constrain_max_tokens", False)):
        payload["max_tokens"] = max(1, int(getattr(runtime_config, "max_tokens", 16384)))
    else:
        payload.pop("max_tokens", None)
    return payload


def merge_request_template(
    payload: dict[str, Any],
    platform: str | None,
    model: str | None,
    *,
    runtime_config: Any = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """把请求模板作为默认参数并入 ``payload``，调用方已有键保持不变。

    运行时保留键（见 :data:`RUNTIME_RESERVED_KEYS`）从模板中剔除，最后统一应用
    :func:`apply_max_tokens_policy`，确保输出长度只受全局开关控制。
    """
    template = load_request_template(config_dir, platform, model)
    merged = {key: value for key, value in template.items() if key not in RUNTIME_RESERVED_KEYS}
    merged.update(payload)
    return apply_max_tokens_policy(merged, runtime_config)


def build_request_payload(
    platform: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    *,
    runtime_config: Any = None,
    config_dir: str | Path | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一次 chat/completions 请求体：运行时字段 + 请求模板默认值 + 输出长度策略。

    调用方通过 ``params`` 显式声明的可调参数（temperature / top_p / metadata 等）
    优先级最高；模板只补其未声明者。
    """
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if isinstance(params, dict):
        payload.update(params)
    return merge_request_template(
        payload, platform, model, runtime_config=runtime_config, config_dir=config_dir
    )
