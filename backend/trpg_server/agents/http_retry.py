"""带「剩余预算」语义的 HTTP POST 重试。

非流式的长请求会被网络层（防火墙/NAT 空闲超时、代理掉线等）静默掐断：操作系统的
TCP 重传往往在配置的等待时间远未用满时就先放弃，于是抛出超时。这里统一实现
「按总预算重试」——每次重试的 socket 超时都取剩余预算，因此总耗时不会超过管理员
配置的 ``ai_request_timeout``，并保证超时错误里上报的是真实等待时间。

chat/completions 与 Responses API 两条传输路径共用本模块，避免两处行为不一致：
Responses 路径若只做单次请求，一次瞬时抖动就会永久降级回 chat/completions，
白白丢掉 ``previous_response_id`` 带来的增量收益。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)


def post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
    max_retries: int,
    post: Callable[..., Any] | None = None,
    on_retry: Callable[[int, int], None] | None = None,
) -> Any:
    """在总预算内重试一次 POST；超时/连接失败最终以 :class:`RuntimeError` 抛出。

    ``on_retry`` 会在每次重试前被调用（用于在聊天框里提示重试进度），它自身抛错
    不影响重试。错误信息里的等待秒数取真实耗时，而非配置值。
    """
    sender = post or requests.post
    budget = max(1, timeout)
    deadline = time.monotonic() + budget
    total_attempts = max(1, max_retries + 1)
    attempt = 0
    while True:
        attempt += 1
        attempt_started = time.monotonic()
        try:
            return sender(
                url,
                headers=headers,
                json=payload,
                timeout=max(1, deadline - attempt_started),
            )
        except requests.exceptions.Timeout as exc:
            attempt_elapsed = time.monotonic() - attempt_started
            remaining = deadline - time.monotonic()
            if attempt < total_attempts and remaining >= 1:
                logger.warning(
                    "AI API request interrupted after %.1fs; retrying (%s/%s)",
                    attempt_elapsed,
                    attempt,
                    max_retries,
                )
                if on_retry is not None:
                    # 重试提示属于「锦上添花」，失败不能影响重试本身。
                    try:
                        on_retry(attempt, max_retries)
                    except Exception:
                        logger.debug("AI retry notifier failed", exc_info=True)
                continue
            retried = attempt - 1
            total_elapsed = budget - max(0.0, remaining)
            logger.warning(
                "AI API request timed out after %.1fs (budget %.0fs, attempts %s)",
                total_elapsed,
                budget,
                attempt,
            )
            if retried:
                raise RuntimeError(
                    f"AI 平台请求超时（已等待约 {int(total_elapsed)} 秒；已重试 {retried} 次仍未收到响应）"
                ) from exc
            raise RuntimeError(f"AI 平台请求超时（等待 {int(total_elapsed)} 秒未响应）") from exc
        except requests.exceptions.RequestException as exc:
            logger.warning("AI API request failed: %s", exc)
            raise RuntimeError(f"AI 平台连接失败：{exc}") from exc