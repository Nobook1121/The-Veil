from __future__ import annotations

from dataclasses import dataclass
import logging
import shlex
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, TextIO

from trpg_server.agents.telemetry import load_daily_ai_usage
from trpg_server.agents.token_accounting import format_segment_summary, merge_segment_totals
from trpg_server.settings import LOGS_DIR
from trpg_server.users.manager import user_manager as default_user_manager

logger = logging.getLogger(__name__)

VALID_ROLES = {"USER", "ADMIN", "OWNER"}
# 降级命令只允许落到 USER/ADMIN；OWNER 属于最高权限，不作降级目标。
DEMOTE_TARGETS = {"USER", "ADMIN"}

# The console loop runs in its own thread, separate from the web server.  A
# callback lets the embedding server decide how to perform the actual graceful
# stop (a stoppable WSGI server must be shut down from a different thread than
# the one running its request loop).
_shutdown_callback: Callable[[], None] | None = None
_shutdown_lock = threading.Lock()

# ``shutdown`` is the canonical spelling; the short aliases are convenient when
# typing directly into a local console and all follow the same graceful path.
SHUTDOWN_COMMANDS = {"shutdown", "stop", "quit", "exit", "server shutdown"}


@dataclass
class ConsoleCommandResult:
    ok: bool
    message: str


def set_shutdown_callback(callback: Callable[[], None] | None) -> None:
    """Register the action invoked by the ``shutdown`` console command.

    Passing ``None`` removes a previous callback.  This keeps command parsing
    independent from the concrete web-server runtime and makes the command
    straightforward to exercise in tests.
    """

    global _shutdown_callback
    with _shutdown_lock:
        _shutdown_callback = callback


def _log_result(result: ConsoleCommandResult) -> ConsoleCommandResult:
    """Log a command result exactly once at the level matching its outcome."""

    if result.message:
        if result.ok:
            logger.info("%s", result.message)
        else:
            logger.warning("%s", result.message)
    return result


def _invoke_shutdown_callback(callback_override: Callable[[], None] | None = None) -> ConsoleCommandResult:
    with _shutdown_lock:
        callback = callback_override if callback_override is not None else _shutdown_callback
    if callback is None:
        return ConsoleCommandResult(False, "Shutdown is not available in this server process")
    try:
        callback()
    except Exception as exc:  # pragma: no cover - defensive logging path
        return ConsoleCommandResult(False, f"Shutdown request failed: {exc}")
    return ConsoleCommandResult(True, "Shutdown requested; waiting for the server to stop safely.")


def execute_console_command(
    command_line: str,
    user_manager: Any = default_user_manager,
    shutdown_callback: Callable[[], None] | None = None,
    log_dir: Any = None,
) -> ConsoleCommandResult:
    try:
        parts = shlex.split(command_line)
    except ValueError as exc:
        return _log_result(ConsoleCommandResult(False, f"Invalid command syntax: {exc}"))

    if not parts:
        return ConsoleCommandResult(True, "")

    if parts[0] in {"help", "?"}:
        return _log_result(ConsoleCommandResult(True, command_help_text()))

    normalized_parts = [part.lower() for part in parts]
    is_shutdown = (
        normalized_parts[0] in {"shutdown", "stop", "quit", "exit"} and len(parts) == 1
    ) or (normalized_parts[:2] == ["server", "shutdown"] and len(parts) == 2)
    if is_shutdown:
        return _log_result(_invoke_shutdown_callback(shutdown_callback))

    # user 子命令统一走 _change_user_role，同时支持升级与降级。
    if parts[0] == "user" and len(parts) >= 2:
        subcommand = parts[1].lower()
        if subcommand == "promote":
            return _log_result(
                _change_user_role(parts[2:], user_manager, "ADMIN", "user promote")
            )
        if subcommand == "demote":
            return _log_result(
                _change_user_role(
                    parts[2:],
                    user_manager,
                    "USER",
                    "user demote",
                    allowed_roles=DEMOTE_TARGETS,
                )
            )
        if subcommand == "reset-password":
            return _log_result(_force_password_reset(parts[2:], user_manager))

    if parts[0] == "ai" and len(parts) >= 2 and parts[1].lower() == "usage":
        return _log_result(_report_ai_usage(parts[2:], log_dir))

    return _log_result(ConsoleCommandResult(False, f"Unknown command: {parts[0]}"))


def command_help_text() -> str:
    return "\n".join(
        [
            "Available console commands:",
            "  help",
            "  user promote <username-or-id> [USER|ADMIN|OWNER]   （升级，缺省为 ADMIN）",
            "  user demote <username-or-id> [USER|ADMIN]          （降级，缺省为 USER）",
            "  user reset-password <username-or-id>               （重置为随机临时密码，并强制下次登录改密）",
            "  ai usage [days]                                    （打印最近 N 天 AI 用量与输入 token 分段，默认 1 天，1-90）",
            "  shutdown (aliases: stop, quit, exit, server shutdown)",
        ]
    )


def start_console_command_loop(
    stdin: TextIO | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    user_manager: Any | None = None,
) -> threading.Thread:
    stream = stdin
    # 复用调用方传入的 user_manager（通常与正在运行的 Web 应用是同一实例），
    # 否则退回到模块级默认实例。这样控制台写入与 Web 应用读写的是同一份数据，
    # 避免出现“控制台改了用户组却没有生效”的问题。
    manager = user_manager if user_manager is not None else default_user_manager

    def _loop() -> None:
        logger.info("Console command loop started. Type 'help' for commands.")
        while True:
            if stream is None:
                # 交互式输入时空行只代表「按了回车」，不代表 EOF；只有 EOFError/OSError
                # 才是输入结束。早期把空行当作结束条件，导致用户按一次回车后控制台循环
                # 直接退出、看起来「失去响应」。
                try:
                    line = input("> ")
                except (EOFError, OSError):
                    logger.info("Console command loop stopped.")
                    return
            else:
                line = stream.readline()
                if line == "":
                    logger.info("Console command loop stopped.")
                    return
            command = line.strip()
            # The command itself logs its outcome; the loop must not log it a
            # second time (that produced a duplicate warning + error pair).
            result = execute_console_command(
                command, user_manager=manager, shutdown_callback=shutdown_callback
            )
            # Once a graceful shutdown has been accepted there is no reason to
            # keep the command reader alive.
            if result.ok and command.lower() in SHUTDOWN_COMMANDS:
                logger.info("Console command loop stopped after shutdown request.")
                return

    thread = threading.Thread(target=_loop, name="trpg-console-commands", daemon=True)
    thread.start()
    return thread


def _change_user_role(
    args: list[str],
    user_manager: Any,
    default_role: str,
    command_name: str,
    allowed_roles: set[str] | None = None,
) -> ConsoleCommandResult:
    """调整用户角色/用户组，升级与降级走同一实现。

    ``default_role`` 决定省略目标角色时的默认值（promote 为 ADMIN、demote 为 USER）；
    ``allowed_roles`` 限制可选目标角色，便于让 demote 只能降不能升。
    """

    allowed = allowed_roles or VALID_ROLES
    usage = f"Usage: {command_name} <username-or-id> [{'|'.join(sorted(allowed))}]"
    if not args:
        return ConsoleCommandResult(False, usage)

    identifier = args[0]
    role = args[1].upper() if len(args) > 1 else default_role
    if role not in allowed:
        return ConsoleCommandResult(False, f"Invalid role: {role}. Allowed: {', '.join(sorted(allowed))}")

    user = _find_user(identifier, user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {identifier}")

    current_role = str(user.get("role") or "USER").upper()
    success, message = user_manager.update_user_role(int(user["id"]), role)
    if not success:
        return ConsoleCommandResult(False, message)
    name = user.get("username") or user["id"]
    if role == current_role:
        return ConsoleCommandResult(True, f"{name} already has role {role}")
    return ConsoleCommandResult(True, f"Updated {name} role {current_role} -> {role}")


def _force_password_reset(args: list[str], user_manager: Any) -> ConsoleCommandResult:
    """把用户密码重置为随机临时密码，并强制其下次登录后修改密码。

    临时密码只在此处返回并打印到控制台，不会写入任何持久化日志之外的存储。
    """

    if not args:
        return ConsoleCommandResult(False, "Usage: user reset-password <username-or-id>")

    identifier = args[0]
    user = _find_user(identifier, user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {identifier}")

    reset = getattr(user_manager, "force_password_reset", None)
    if not callable(reset):
        return ConsoleCommandResult(False, "Password reset is not supported by the current user manager")

    result = reset(int(user["id"]))
    success, message = result[0], result[1]
    temp_password = result[2] if len(result) > 2 else None
    if not success:
        return ConsoleCommandResult(False, message)

    name = user.get("username") or user["id"]
    return ConsoleCommandResult(
        True,
        f"Temporary password for {name}: {temp_password}  "
        f"({name} must change the password on next login)",
    )


def _report_ai_usage(args: list[str], log_dir: Any = None) -> ConsoleCommandResult:
    """汇总最近 N 天的 AI 用量并打印，含输入 token 的分段估算。

    分段 token 是本地粗估（见 :mod:`trpg_server.agents.token_accounting`），真实计费
    以上游 usage 为准；分段用于快速判断输入成本花在静态前缀、知识库还是历史上。
    """
    days = 1
    if args:
        try:
            days = int(args[0])
        except ValueError:
            return ConsoleCommandResult(False, f"Usage: ai usage [days] (1-90), got {args[0]!r}")
        if not 1 <= days <= 90:
            return ConsoleCommandResult(False, "Usage: ai usage [days] (days must be 1-90)")

    target_dir = Path(log_dir) if log_dir is not None else Path(LOGS_DIR)
    totals = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    roles: dict[str, dict[str, int]] = {}
    segments: dict[str, int] = {}
    rate_sum = 0.0
    rate_count = 0
    for offset in range(days):
        day = (date.today() - timedelta(days=offset)).isoformat()
        report = load_daily_ai_usage(target_dir, day=day)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += int(report.get(key) or 0)
        rate_sum += float(report.get("cache_hit_rate") or 0)
        rate_count += 1
        # 只累加日级分段，避免与角色级分段重复计数（两者是同一批请求的不同切面）。
        merge_segment_totals(segments, report.get("prompt_segments"))
        report_roles = report.get("roles") or {}
        for role, bucket in report_roles.items():
            if not isinstance(bucket, dict):
                continue
            totals["request_count"] += int(bucket.get("request_count") or 0)
            role_totals = roles.setdefault(
                role, {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            )
            role_totals["request_count"] += int(bucket.get("request_count") or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                role_totals[key] += int(bucket.get(key) or 0)

    lines = [f"AI 用量（最近 {days} 天，截至 {date.today().isoformat()}）:"]
    lines.append(
        f"  请求 {totals['request_count']} 次 · 输入 {totals['prompt_tokens']} · "
        f"输出 {totals['completion_tokens']} · 总计 {totals['total_tokens']}"
    )
    average_rate = round(rate_sum / rate_count, 2) if rate_count else 0.0
    lines.append(f"  平均缓存命中率 {average_rate}%")
    lines.append(f"  输入分段（估算）: {format_segment_summary(segments)}")
    if roles:
        lines.append("  按角色:")
        for role, bucket in sorted(roles.items(), key=lambda item: item[1]["total_tokens"], reverse=True):
            lines.append(
                f"    {role}: 请求 {bucket['request_count']} · 输入 {bucket['prompt_tokens']} · "
                f"输出 {bucket['completion_tokens']} · 总计 {bucket['total_tokens']}"
            )
    else:
        lines.append("  （窗口内没有用量记录）")
    return ConsoleCommandResult(True, "\n".join(lines))


def _find_user(identifier: str, user_manager: Any) -> dict[str, Any] | None:
    if identifier.isdigit() and hasattr(user_manager, "get_user_by_id"):
        user = user_manager.get_user_by_id(int(identifier))
        if user:
            return user

    identifier_lower = identifier.lower()
    for user in user_manager.get_all_users():
        if str(user.get("id")) == identifier or str(user.get("username", "")).lower() == identifier_lower:
            return user
    return None
