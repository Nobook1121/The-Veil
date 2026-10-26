from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import shlex
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, TextIO

from trpg_server import (
    ai_admin,
    backup_admin,
    config_admin,
    knowledge_admin,
    network_admin,
    room_admin,
    server_admin,
    user_admin,
    vector_admin,
)
from trpg_server.agents.cache_probe import probe_enabled_platform
from trpg_server.agents.http_retry import clear_abort, is_abort_requested, request_abort
from trpg_server.agents.telemetry import load_daily_ai_usage
from trpg_server.agents.token_accounting import format_segment_summary, merge_segment_totals
from trpg_server.character_ai import (
    CharacterGenerationError,
    run_character_generation,
    scenario_context_from_record,
)
from trpg_server.logging_config import log_user_action, user_action_text
from trpg_server.narrative_admin import handle_narrative_command
from trpg_server.scenario_admin import (
    delete_scenario,
    list_scenarios,
    reindex_scenario,
    set_owner,
    validate_scenario,
)
from trpg_server.scenario_store import load_scenario_by_id
from trpg_server.settings import (
    KNOWLEDGE_BASES_DIR,
    LOGS_DIR,
    ROOMS_DIR,
    SCENARIOS_DIR,
)
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

# 破坏性命令统一支持 --yes：缺省为预演（只打印将发生的变更），交互确认或 --yes 才执行。
YES_FLAGS = {"--yes", "-y"}
CONSOLE_ACTOR = "控制台"


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
    ai_platform_dir: Any = None,
    ai_platform_secret_dir: Any = None,
    scenarios_dir: Any = None,
    rooms_dir: Any = None,
    vector_store: Any = None,
    embedding_provider: Any = None,
    ocr_provider: Any = None,
    app: Any = None,
    confirm: Callable[[str], bool] | None = None,
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

    domain = parts[0].lower()
    args = parts[1:]
    subcommand = args[0].lower() if args else ""

    if domain == "server":
        if subcommand == "health":
            return _log_result(
                _server_health_command(args[1:], vector_store, embedding_provider, ocr_provider,
                                        ai_platform_dir, ai_platform_secret_dir, user_manager)
            )
        if subcommand == "info":
            return _log_result(_server_info_command(args[1:]))

    if domain == "log" and subcommand == "tail":
        return _log_result(_log_tail_command(args[1:], log_dir))

    if domain == "user" and subcommand:
        if subcommand == "promote":
            return _log_result(_change_user_role(args[1:], user_manager, "ADMIN", "user promote"))
        if subcommand == "demote":
            return _log_result(
                _change_user_role(args[1:], user_manager, "USER", "user demote", allowed_roles=DEMOTE_TARGETS)
            )
        if subcommand == "reset-password":
            return _log_result(_force_password_reset(args[1:], user_manager))
        if subcommand == "list":
            return _log_result(_user_list_command(args[1:], user_manager))
        if subcommand == "ban":
            return _log_result(_user_status_command(args[1:], user_manager, "banned", "封禁"))
        if subcommand == "unban":
            return _log_result(_user_status_command(args[1:], user_manager, "active", "解封"))
        if subcommand == "create":
            return _log_result(_user_create_command(args[1:], user_manager))
        if subcommand == "sessions":
            return _log_result(_user_sessions_command(args[1:], user_manager))
        if subcommand == "delete":
            return _log_result(
                _user_delete_command(args[1:], user_manager, scenarios_dir, confirm=confirm)
            )

    if domain == "ai" and subcommand:
        if subcommand == "usage":
            if len(args) > 1 and args[1].lower() == "reset":
                return _log_result(_ai_usage_reset_command(args[2:], log_dir, confirm=confirm))
            return _log_result(_report_ai_usage(args[1:], log_dir))
        if subcommand in {"cache_probe", "cache-probe"}:
            return _log_result(_probe_ai_cache(args[1:], ai_platform_dir, ai_platform_secret_dir))
        if subcommand == "platform":
            return _log_result(
                _ai_platform_command(args[1:], ai_platform_dir, ai_platform_secret_dir)
            )
        if subcommand == "cache":
            return _log_result(_ai_cache_command(args[1:], confirm=confirm))
        if subcommand == "character":
            return _log_result(_ai_character_command(args[1:], app))
        if subcommand == "abort":
            return _log_result(_ai_retry_abort_command())
        if subcommand in {"resume", "retry"}:
            return _log_result(_ai_retry_resume_command())

    if domain == "scenario" and subcommand:
        if subcommand == "list":
            return _log_result(_scenario_list_command(args[1:], scenarios_dir, vector_store))
        if subcommand == "owner":
            return _log_result(_scenario_owner_command(args[1:], scenarios_dir, user_manager))
        if subcommand == "delete":
            return _log_result(
                _scenario_delete_command(args[1:], scenarios_dir, rooms_dir, vector_store, confirm=confirm)
            )
        if subcommand == "validate":
            return _log_result(_scenario_validate_command(args[1:], scenarios_dir, vector_store))
        if subcommand == "reindex":
            return _log_result(
                _scenario_reindex_command(args[1:], scenarios_dir, vector_store, embedding_provider, confirm=confirm)
            )

    if domain == "narrative" and subcommand:
        ok, text = handle_narrative_command(
            parts[1:],
            _resolve_scenarios_dir(scenarios_dir),
            rooms_dir=rooms_dir,
            confirm=confirm,
        )
        return _log_result(ConsoleCommandResult(ok, text))

    if domain == "vector" and subcommand:
        if subcommand == "health":
            return _log_result(_vector_health_command(vector_store, embedding_provider, ocr_provider))
        if subcommand == "prune":
            return _log_result(_vector_prune_command(args[1:], scenarios_dir, vector_store, confirm=confirm))

    if domain == "knowledge" and subcommand:
        store = _ruleset_store(rooms_dir, ocr_provider)
        if subcommand == "list":
            return _log_result(_knowledge_list_command(args[1:], scenarios_dir, vector_store, store))
        if subcommand == "reindex":
            return _log_result(_knowledge_reindex_command(args[1:], store, confirm=confirm))

    if domain == "room" and subcommand:
        if subcommand == "list":
            return _log_result(_room_list_command(args[1:], rooms_dir))
        if subcommand == "members":
            return _log_result(_room_members_command(args[1:], rooms_dir))
        if subcommand == "archive":
            return _log_result(_room_archive_command(args[1:], rooms_dir, app, confirm=confirm))
        if subcommand == "prune":
            return _log_result(_room_prune_command(args[1:], rooms_dir, confirm=confirm))

    if domain == "dice":
        if not subcommand:
            return _log_result(
                ConsoleCommandResult(
                    True,
                    "Usage: dice <animation|player-roll|reroll|reroll-limit|timeout|settings|pending|textures|skin>",
                )
            )
        if subcommand == "animation":
            return _log_result(_dice_animation_command(args[1:], confirm=confirm))
        if subcommand == "player-roll":
            return _log_result(_dice_player_roll_command(args[1:], confirm=confirm))
        if subcommand == "reroll":
            return _log_result(_dice_reroll_command(args[1:], confirm=confirm))
        if subcommand == "reroll-limit":
            return _log_result(_dice_reroll_limit_command(args[1:], confirm=confirm))
        if subcommand == "timeout":
            return _log_result(_dice_timeout_command(args[1:], confirm=confirm))
        if subcommand == "settings":
            return _log_result(_dice_settings_command(args[1:], rooms_dir))
        if subcommand == "pending":
            return _log_result(_dice_pending_command(args[1:], rooms_dir))
        if subcommand == "textures":
            return _log_result(_dice_textures_command(args[1:]))
        if subcommand == "skin" and len(args) > 1 and args[1].lower() == "delete":
            return _log_result(_dice_skin_delete_command(args[2:], confirm=confirm))

    if domain == "network":
        if subcommand == "status":
            return _log_result(_network_status_command())
        if subcommand == "test":
            return _log_result(_network_test_command())

    if domain == "config" and subcommand:
        if subcommand == "show":
            return _log_result(_config_show_command(args[1:]))
        if subcommand == "migrate":
            return _log_result(_config_migrate_command(args[1:], confirm=confirm))
        if subcommand == "path":
            return _log_result(_config_path_command(args[1:]))

    if domain == "autosave":
        return _log_result(_autosave_command(app))

    if domain == "backup" and subcommand == "create":
        return _log_result(_backup_create_command(args[1:], confirm=confirm))

    if domain == "owner" and subcommand == "transfer":
        return _log_result(_owner_transfer_command(args[1:], user_manager, confirm=confirm))

    return _log_result(ConsoleCommandResult(False, f"Unknown command: {parts[0]}"))


def command_help_text() -> str:
    return "\n".join(
        [
            "Available console commands:",
            "  help",
            "  user promote/demote <user> [角色]                （升级/降级，缺省 ADMIN / USER）",
            "  user reset-password <user>                       （重置为随机临时密码并强制改密）",
            "  user list [search]                               （列出用户：ID/用户名/角色/状态）",
            "  user ban <user> / user unban <user>              （封禁 / 解封账号）",
            "  user sessions revoke <user>                      （撤销全部会话，强制下线）",
            "  user create <name> [USER|ADMIN|OWNER]            （本地建号，返回一次性临时密码）",
            "  user delete <user> [--force]                     （删除账号；--yes 跳过确认）",
            "  ai usage [days]                                  （最近 N 天 AI 用量与输入 token 分段）",
            "  ai usage reset <all|agent> [--yes]               （清空当天用量记录）",
            "  ai cache_probe [platform]                        （探测平台前缀缓存是否可观测）",
            "  ai platform list                                 （列出 AI 平台：启用/顺序/模型）",
            "  ai platform test [id]                            （平台连通性冒烟测试）",
            "  ai platform enable|disable <id>                  （启用 / 停用平台）",
            "  ai platform detect-responses [id]                （探测是否支持 Responses API）",
            "  ai cache stats                                   （本地前缀/精确/语义缓存统计）",
            "  ai cache clear [--yes]                           （清空本地缓存）",
            "  ai character generate <描述> [--scenario <id>] [--json]  （触发一次角色卡 AI 生成，用于自测）",
            "  ai abort                                         （立即终止进行中的 AI 自动重试）",
            "  ai resume                                        （解除终止状态，恢复自动重试）",
            "  scenario list [search]                           （列出剧本）",
            "  scenario owner <id> <user>                       （更改剧本所有者）",
            "  scenario delete <id> [--vectors-only|--force]    （删剧本；--yes 跳过确认）",
            "  scenario validate <id>                           （校验剧本/索引/向量一致性）",
            "  scenario reindex <id> [--yes]                    （重建剧本知识索引与向量）",
            "  narrative status [<id>] [--json]                 （查看世界树/图谱生成状态）",
            "  narrative validate <id> [--simulate] [--json]    （拓扑分档校验与乱序模拟）",
            "  narrative topology <id> [--json]                 （查看拓扑判定与证据）",
            "  narrative build <id>|--all [--yes] [--json]      （生成/重建 narrative.json；缺省预演）",
            "  narrative opening <房间id> [--publish|--repair] [--yes] [--json]",
            "                                                   （预览/发布开局；--repair 不重置）",
            "  vector health                                    （向量库/embedding/OCR 状态）",
            "  vector prune [--yes]                             （清理孤儿向量）",
            "  knowledge list [search]                          （列出规则集与各剧本知识/向量数）",
            "  knowledge reindex [ruleset] [--yes]              （重建规则集索引，省略则全部）",
            "  room list [search]                               （列出房间）",
            "  room members <room_id>                           （查看房间成员）",
            "  room archive <room_id> [--yes]                   （归档房间）",
            "  room prune [--days N] [--yes]                    （清理僵尸房间，默认 30 天）",
            "  dice animation show                             （查看管理员默认骰子动画档位）",
            "  dice animation set <text|simple|3d> [--yes]      （设置默认动画档位）",
            "  dice player-roll show                           （查看默认玩家投掷开关）",
            "  dice player-roll <on|off> [--yes]                （设置默认玩家投掷开关）",
            "  dice reroll show                                （查看默认重投模式与上限）",
            "  dice reroll <off|limited|unlimited> [--yes]      （设置默认重投模式）",
            "  dice reroll-limit <1-20> [--yes]                 （设置指定次数模式的上限）",
            "  dice timeout <30-300> [--yes]                    （设置等待玩家投掷的超时秒数）",
            "  dice settings [room_id]                          （管理员默认与房间生效值概览）",
            "  dice pending [room_id]                           （列出等待玩家投掷的请求）",
            "  dice textures                                    （贴图资产、桌面皮肤与骰子皮肤概览）",
            "  dice skin delete <skin_id> [--yes]               （删除骰子皮肤；--yes 跳过确认）",
            "  network status / network test                    （网络状态 / 连通性测试）",
            "  config show [--section <name>]                   （查看生效配置与部署锁定来源）",
            "  config migrate [--yes]                           （遗留 JSON 并入 general.toml，原文件改名 .legacy）",
            "  config path                                      （打印配置目录与模板目录）",
            "  server health [--json] / server info             （子系统健康 / 进程与目录信息）",
            "  log tail [n]                                     （打印最近 n 行日志，默认 50）",
            "  autosave                                         （立即触发一次自动存档检查）",
            "  backup create [--out <dir>] [--yes]              （打包关键数据为 zip）",
            "  owner transfer <user> [--yes]                    （转移系统 OWNER 身份）",
            "  shutdown (aliases: stop, quit, exit, server shutdown)",
            "  说明：破坏性命令缺省为「预演」，加 --yes 或在交互确认时输入 y 才会真正执行。",
        ]
    )


def start_console_command_loop(
    stdin: TextIO | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    user_manager: Any | None = None,
    scenarios_dir: Any = None,
    rooms_dir: Any = None,
    vector_store: Any = None,
    embedding_provider: Any = None,
    ocr_provider: Any = None,
    app: Any = None,
    log_dir: Any = None,
    ai_platform_dir: Any = None,
    ai_platform_secret_dir: Any = None,
) -> threading.Thread:
    stream = stdin
    # 复用调用方传入的 user_manager（通常与正在运行的 Web 应用是同一实例），
    # 否则退回到模块级默认实例。这样控制台写入与 Web 应用读写的是同一份数据，
    # 避免出现“控制台改了用户组却没有生效”的问题。
    manager = user_manager if user_manager is not None else default_user_manager

    def _confirm(prompt: str) -> bool:
        """读取一行确认输入；无输入渠道或读取失败时一律视为「不执行」。"""

        try:
            print(prompt)
        except Exception:  # noqa: BLE001 - 输出失败不应影响命令执行
            pass
        try:
            if stream is None:
                answer = input()
            else:
                answer = stream.readline()
        except (EOFError, OSError):
            return False
        if answer == "":  # 流已结束
            return False
        return answer.strip().lower() in {"y", "yes"}

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
                command,
                user_manager=manager,
                shutdown_callback=shutdown_callback,
                log_dir=log_dir,
                ai_platform_dir=ai_platform_dir,
                ai_platform_secret_dir=ai_platform_secret_dir,
                scenarios_dir=scenarios_dir,
                rooms_dir=rooms_dir,
                vector_store=vector_store,
                embedding_provider=embedding_provider,
                ocr_provider=ocr_provider,
                app=app,
                confirm=_confirm,
            )
            # Once a graceful shutdown has been accepted there is no reason to
            # keep the command reader alive.
            if result.ok and command.lower() in SHUTDOWN_COMMANDS:
                logger.info("Console command loop stopped after shutdown request.")
                return

    thread = threading.Thread(target=_loop, name="trpg-console-commands", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


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


def _resolve_scenarios_dir(scenarios_dir: Any) -> Path:
    return Path(scenarios_dir) if scenarios_dir is not None else Path(SCENARIOS_DIR)


def _resolve_rooms_dir(rooms_dir: Any) -> Path:
    return Path(rooms_dir) if rooms_dir is not None else Path(ROOMS_DIR)


def _ruleset_store(rooms_dir: Any, ocr_provider: Any) -> Any:
    """构造规则集知识库（与 Web 路由同一实现，指向同一目录）。"""

    from trpg_server.agents.ruleset_knowledge import RulesetKnowledgeStore

    return RulesetKnowledgeStore(
        Path(KNOWLEDGE_BASES_DIR),
        rooms_dir=_resolve_rooms_dir(rooms_dir),
        ocr_provider=ocr_provider,
    )


def _split_args(args: list[str]) -> tuple[list[str], set[str]]:
    positionals = [item for item in args if not item.startswith("-")]
    flags = {item.lower() for item in args if item.startswith("-")}
    return positionals, flags


def _reject_unknown_flags(flags: set[str], allowed: set[str]) -> str | None:
    unknown = flags - allowed
    if unknown:
        return f"未知参数: {', '.join(sorted(unknown))}"
    return None


def _assume_yes(flags: set[str]) -> bool:
    return bool(flags & YES_FLAGS)


def _confirm_action(
    *,
    preview: str,
    apply: Callable[[], ConsoleCommandResult],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    prompt: str,
) -> ConsoleCommandResult:
    """破坏性命令统一入口。

    ``--yes`` 直接执行；否则先打印预演内容，再按交互确认回答决定是否执行；
    没有交互渠道时保持纯预演（不改动任何数据）。
    """

    if assume_yes:
        return apply()
    if confirm is None:
        return ConsoleCommandResult(True, preview + "\n（预演模式：确认无误后加 --yes 执行）")
    if confirm(f"{prompt} (y/N)"):
        return apply()
    return ConsoleCommandResult(True, preview + "\n（已取消，未执行）")


def _json_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _invalidate_scenarios_cache() -> None:
    """控制台改动剧本后，尽力让 Web 层的剧本列表缓存（60s TTL）立即失效。

    用惰性导入避免 console ↔ routes 的模块级耦合；导入或调用失败只记 debug，
    不影响控制台命令本身——Web 最迟 60 秒后也会自行刷新。
    """

    try:
        from trpg_server.routes.scenarios import clear_scenarios_cache

        clear_scenarios_cache()
    except Exception:  # noqa: BLE001 - 缓存失效属于锦上添花
        logger.debug("Unable to clear scenarios cache after console change", exc_info=True)


# ---------------------------------------------------------------------------
# server / log
# ---------------------------------------------------------------------------


def _server_health_command(
    args: list[str],
    vector_store: Any,
    embedding_provider: Any,
    ocr_provider: Any,
    ai_platform_dir: Any,
    ai_platform_secret_dir: Any,
    user_manager: Any,
) -> ConsoleCommandResult:
    _, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, {"--json"})
    if unknown:
        return ConsoleCommandResult(False, unknown)
    data = server_admin.server_health(
        vector_store=vector_store,
        embedding_provider=embedding_provider,
        ocr_provider=ocr_provider,
        ai_platform_dir=ai_platform_dir,
        ai_platform_secret_dir=ai_platform_secret_dir,
        user_manager=user_manager,
    )
    if "--json" in flags:
        return ConsoleCommandResult(True, _json_block(data))
    vector = data["vector"]
    lines = [
        "服务健康检查:",
        f"  向量库: {'可用' if vector.get('available') else '不可用'} · 后端={vector.get('backend', '-')} · {vector.get('path', '')}",
        f"  Embedding: {data['embedding']}",
        f"  OCR: {'启用' if data['ocr']['enabled'] else '未启用'} · 可用={data['ocr']['available']}",
        f"  AI 平台: 启用 {data['platforms']['enabled']}/{data['platforms']['total']} · {', '.join(data['platforms']['ids']) or '-'}",
        f"  用户库: {data['users']['count']} 人 · OWNER {'存在' if data['users']['owner_exists'] else '缺失'}",
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _server_info_command(args: list[str]) -> ConsoleCommandResult:
    _, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, {"--json"})
    if unknown:
        return ConsoleCommandResult(False, unknown)
    info = server_admin.server_info()
    if "--json" in flags:
        return ConsoleCommandResult(True, _json_block(info))
    lines = [
        f"服务信息: 版本 {info['version']} · Python {info['python']}",
        f"  运行平台: {info['platform']}",
        f"  数据目录: {info['data_dir']}",
        "  目录占用:",
    ]
    for name, stats in info["directories"].items():
        if not stats["exists"]:
            lines.append(f"    {name}: 不存在")
            continue
        lines.append(f"    {name}: {stats['files']} 个文件 · {stats['bytes'] / 1024:.1f} KB")
    return ConsoleCommandResult(True, "\n".join(lines))


def _log_tail_command(args: list[str], log_dir: Any) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    lines = 50
    if positionals:
        try:
            lines = int(positionals[0])
        except ValueError:
            return ConsoleCommandResult(False, f"Usage: log tail [n] (n must be a number), got {positionals[0]!r}")
    result = server_admin.tail_log(log_dir, lines)
    if not result["lines"]:
        return ConsoleCommandResult(True, f"日志：{result.get('detail') or '无内容'}")
    header = f"日志文件：{result['path']}（末尾 {len(result['lines'])} 行）"
    return ConsoleCommandResult(True, "\n".join([header, *result["lines"]]))


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------


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


def _user_list_command(args: list[str], user_manager: Any) -> ConsoleCommandResult:
    users = user_admin.list_users(user_manager, " ".join(args))
    if not users:
        return ConsoleCommandResult(True, "没有匹配的用户")
    lines = [f"用户共 {len(users)} 个:"]
    for user in users:
        lines.append(
            f"  #{user['id']} {user['username']} · {user['role']} · {user['status']} · "
            f"{user['email']} · 最近登录={user.get('last_login_at') or '-'}"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _user_status_command(
    args: list[str], user_manager: Any, status: str, action_label: str
) -> ConsoleCommandResult:
    if len(args) != 1:
        return ConsoleCommandResult(False, f"Usage: user {'ban' if status == 'banned' else 'unban'} <username-or-id>")
    user = _find_user(args[0], user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {args[0]}")
    success, message = user_admin.set_user_status(user_manager, int(user["id"]), status)
    if not success:
        return ConsoleCommandResult(False, message)
    name = user.get("username") or user["id"]
    log_user_action(logger, user_action_text(CONSOLE_ACTOR, f"{action_label}了账号"), 用户ID=user["id"], 用户名=name)
    return ConsoleCommandResult(True, f"已{action_label} {name}({user['id']})，状态 -> {status}")


def _user_sessions_command(args: list[str], user_manager: Any) -> ConsoleCommandResult:
    if len(args) != 2 or args[0].lower() != "revoke":
        return ConsoleCommandResult(False, "Usage: user sessions revoke <username-or-id>")
    user = _find_user(args[1], user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {args[1]}")
    revoked = user_admin.revoke_user_sessions(user_manager, int(user["id"]))
    name = user.get("username") or user["id"]
    log_user_action(logger, user_action_text(CONSOLE_ACTOR, "强制下线了用户"), 用户ID=user["id"], 用户名=name)
    return ConsoleCommandResult(True, f"已撤销 {name}({user['id']}) 的 {revoked} 个会话")


def _user_create_command(args: list[str], user_manager: Any) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if not positionals:
        return ConsoleCommandResult(False, "Usage: user create <username> [USER|ADMIN|OWNER]")
    username = positionals[0]
    role = positionals[1].upper() if len(positionals) > 1 else "USER"
    try:
        result = user_admin.create_user(user_manager, username, role)
    except ValueError as exc:
        return ConsoleCommandResult(False, str(exc))
    log_user_action(logger, user_action_text(CONSOLE_ACTOR, "创建了用户"), 用户名=result["username"], 角色=result["role"])
    return ConsoleCommandResult(
        True,
        f"已创建用户 {result['username']}({result['id']}) · 角色 {result['role']}\n"
        f"  临时密码: {result['temp_password']}（请立即转交并提醒修改）",
    )


def _user_delete_command(
    args: list[str],
    user_manager: Any,
    scenarios_dir: Any,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, {"--force"})
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: user delete <username-or-id> [--force] [--yes]")
    force = "--force" in flags
    user = _find_user(positionals[0], user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {positionals[0]}")
    name = user.get("username") or user["id"]
    target_dir = _resolve_scenarios_dir(scenarios_dir)

    def _apply() -> ConsoleCommandResult:
        ok, message = user_admin.delete_user(
            user_manager, int(user["id"]), force=force, scenarios_dir=target_dir
        )
        if not ok:
            return ConsoleCommandResult(False, message)
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "删除了用户"), 用户ID=user["id"], 用户名=name)
        return ConsoleCommandResult(True, message)

    preview = f"将删除账号 {name}({user['id']})，其会话/资料/审计关联会被级联清理。"
    if not force:
        preview += "\n（未加 --force：若该用户是系统 OWNER 或仍持有剧本，将会被拒绝）"
    return _confirm_action(
        preview=preview,
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认删除用户 {name}?",
    )


# ---------------------------------------------------------------------------
# ai
# ---------------------------------------------------------------------------


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


def _ai_usage_reset_command(
    args: list[str], log_dir: Any, *, confirm: Callable[[str], bool] | None = None
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: ai usage reset <all|agent-id> [--yes]")
    target = positionals[0]
    agent_id = None if target.lower() == "all" else target

    def _apply() -> ConsoleCommandResult:
        result = ai_admin.reset_usage(log_dir, agent_id=agent_id)
        scope = "全部" if agent_id is None else f"agent={agent_id}"
        return ConsoleCommandResult(
            True, f"已重置 {result['day']} 的用量记录（{scope}）：删除 {result['removed']} 条，保留 {result['kept']} 条"
        )

    return _confirm_action(
        preview=f"将删除 {date.today().isoformat()} 的用量记录（{'全部' if agent_id is None else 'agent=' + agent_id}）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt="确认重置用量计数?",
    )


def _probe_ai_cache(
    args: list[str],
    platform_dir: Any = None,
    secret_dir: Any = None,
) -> ConsoleCommandResult:
    """探测当前启用平台是否支持可观测的前缀缓存。

    连发两次相同 payload，比较第二次 ``usage`` 是否出现缓存命中 token：这是唯一能
    在「本地日志永远 0%」时区分「平台真的没命中」和「平台不回报 cached_tokens」的
    办法。只读平台配置文件，不依赖 Flask 上下文。
    """

    if len(args) > 1:
        return ConsoleCommandResult(False, "Usage: ai cache_probe [platform-id]")
    platform_id = args[0] if args else None
    try:
        result = probe_enabled_platform(
            platform_id,
            platform_dir=Path(platform_dir) if platform_dir is not None else None,
            secret_dir=Path(secret_dir) if secret_dir is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 - 探针失败不应让控制台抛栈
        logger.exception("AI cache probe failed")
        return ConsoleCommandResult(False, f"AI cache probe failed: {exc}")

    return ConsoleCommandResult(bool(result.get("ok")), _format_probe_result(result))


def _usage_text(entry: dict[str, Any]) -> str:
    prompt = entry.get("prompt_tokens")
    cached = entry.get("cached_tokens")
    status = entry.get("status")
    cached_text = "未回报" if cached is None else str(cached)
    return f"HTTP {status} · prompt_tokens={prompt if prompt is not None else '-'} · cached_tokens={cached_text}"


def _format_probe_result(result: dict[str, Any]) -> str:
    first = result.get("first") or {}
    second = result.get("second") or {}
    return "\n".join(
        [
            "AI 前缀缓存探针:",
            f"  平台: {result.get('platform') or '-'} · 模型: {result.get('model') or '-'}",
            f"  端点: {result.get('endpoint') or '-'}",
            f"  第一次: {_usage_text(first)}",
            f"  第二次: {_usage_text(second)}",
            f"  结论: {result.get('detail') or ''}",
        ]
    )


def _ai_platform_command(
    args: list[str], platform_dir: Any, secret_dir: Any
) -> ConsoleCommandResult:
    if not args:
        return ConsoleCommandResult(
            False, "Usage: ai platform <list|test|enable|disable|detect-responses> [id]"
        )
    action = args[0].lower()
    rest = args[1:]
    try:
        if action == "list":
            items = ai_admin.list_platforms(platform_dir, secret_dir)
            if not items:
                return ConsoleCommandResult(True, "没有配置任何 AI 平台")
            lines = [f"AI 平台共 {len(items)} 个:"]
            for item in items:
                lines.append(
                    f"  {item['id']} · {'启用' if item['enabled'] else '停用'} · "
                    f"顺序={item['order']} · 模型={', '.join(item['models']) or '-'}"
                )
            return ConsoleCommandResult(True, "\n".join(lines))
        if action == "test":
            platform = rest[0] if rest else _sole_platform(platform_dir, secret_dir)
            result = ai_admin.test_platform(platform, platform_dir, secret_dir)
            status = "连通" if result["ok"] else "失败"
            return ConsoleCommandResult(
                result["ok"],
                f"平台 {result.get('model')} @ {result.get('endpoint')}: {status} · HTTP {result.get('status')} · {result.get('detail')}",
            )
        if action in {"enable", "disable"}:
            if not rest:
                return ConsoleCommandResult(False, f"Usage: ai platform {action} <id>")
            info = ai_admin.set_platform_enabled(rest[0], action == "enable", platform_dir, secret_dir)
            log_user_action(
                logger,
                user_action_text(CONSOLE_ACTOR, "启用了AI平台" if action == "enable" else "停用了AI平台"),
                平台=info["id"],
            )
            return ConsoleCommandResult(True, f"平台 {info['id']} 已{'启用' if info['enabled'] else '停用'}")
        if action in {"detect-responses", "detect_responses"}:
            platform = rest[0] if rest else _sole_platform(platform_dir, secret_dir)
            result = ai_admin.detect_responses(platform, platform_dir, secret_dir)
            return ConsoleCommandResult(
                True,
                f"平台 {result['id']} Responses API: {'支持' if result['supported'] else '不支持'} · {result['detail']}",
            )
    except LookupError as exc:
        return ConsoleCommandResult(False, str(exc))
    except ValueError as exc:
        return ConsoleCommandResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI platform command failed")
        return ConsoleCommandResult(False, f"AI 平台操作失败: {exc}")
    return ConsoleCommandResult(False, f"Unknown ai platform action: {action}")


def _sole_platform(platform_dir: Any, secret_dir: Any) -> str:
    items = ai_admin.list_platforms(platform_dir, secret_dir)
    if len(items) != 1:
        raise ValueError("请指定平台 id（当前平台不止一个）")
    return str(items[0]["id"])


def _ai_cache_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    if not args:
        return ConsoleCommandResult(False, "Usage: ai cache <stats|clear> [--yes]")
    action = args[0].lower()
    if action == "stats":
        stats = ai_admin.cache_stats()
        if not stats:
            return ConsoleCommandResult(True, "本地缓存不可用（chat 模块未加载）")
        lines = ["本地缓存统计:"]
        for name, snapshot in stats.items():
            lines.append(f"  {name}: 存活 {snapshot.get('entries')} 条 · 命中 {snapshot.get('hits')} 次")
        return ConsoleCommandResult(True, "\n".join(lines))
    if action == "clear":
        flags = {item.lower() for item in args[1:]}
        assume_yes = _assume_yes(flags)
        unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
        if unknown:
            return ConsoleCommandResult(False, unknown)

        stats = ai_admin.cache_stats()
        total = sum(int(item.get("entries") or 0) for item in stats.values()) if stats else 0

        def _apply() -> ConsoleCommandResult:
            cleared = ai_admin.clear_caches()
            log_user_action(logger, user_action_text(CONSOLE_ACTOR, "清空了AI本地缓存"))
            detail = " · ".join(f"{name}={count}" for name, count in cleared.items()) or "-"
            return ConsoleCommandResult(True, f"已清空本地缓存（{detail}）")

        return _confirm_action(
            preview=f"将清空本地缓存（当前存活 {total} 条）。",
            apply=_apply,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认清空本地缓存?",
        )
    return ConsoleCommandResult(False, f"Unknown ai cache action: {action}")


def _ai_character_command(args: list[str], app: Any) -> ConsoleCommandResult:
    """控制台触发一次角色卡 AI 生成，用于自测与排障。

    复用 Web 端点同一条生成流水线（``run_character_generation``），因此控制台输出、
    日志与 AI 用量台账跟线上完全一致。生成不是破坏性操作，无需预演确认。
    """
    if not args:
        return ConsoleCommandResult(False, "Usage: ai character generate <描述> [--scenario <id>] [--json]")

    action = args[0].lower()
    tokens = list(args[1:])
    if action != "generate":
        return ConsoleCommandResult(False, f"未知子命令: ai character {args[0]}（可用：generate）")

    scenario_id: Any = None
    json_output = False
    description_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--scenario", "-s"}:
            if index + 1 >= len(tokens):
                return ConsoleCommandResult(False, "参数 --scenario 缺少取值")
            scenario_id = tokens[index + 1]
            index += 2
            continue
        if token == "--json":
            json_output = True
            index += 1
            continue
        if token.startswith("-"):
            return ConsoleCommandResult(False, f"未知参数: {token}")
        description_parts.append(token)
        index += 1

    description = " ".join(description_parts).strip()
    if app is None:
        return ConsoleCommandResult(False, "未注入应用上下文，无法执行角色卡生成")

    with app.app_context():
        scenario_context: dict[str, Any] = {}
        if scenario_id not in (None, "", 0, "0"):
            _, scenario = load_scenario_by_id(
                _resolve_scenarios_dir(app.config.get("SCENARIOS_DIR")), scenario_id
            )
            if isinstance(scenario, dict):
                scenario_context = scenario_context_from_record(scenario)
        try:
            result = run_character_generation(
                description, scenario_context, user_id="console", scenario_id=scenario_id
            )
        except CharacterGenerationError as exc:
            return ConsoleCommandResult(False, f"角色卡生成失败（{exc.kind}）：{exc}")

    if json_output:
        return ConsoleCommandResult(True, _json_block(result))
    card = result.get("character") or {}
    lines = [
        "角色卡 AI 生成成功:",
        f"  平台: {result.get('platform') or '-'} · 模型: {result.get('model') or '-'}",
        "  结束原因: {finish} · 正文字数: {content} · 思考字数: {reasoning} · token: {tokens}".format(
            finish=result.get("finish_reason") or "-",
            content=result.get("content_length"),
            reasoning=result.get("reasoning_length"),
            tokens=result.get("token_count"),
        ),
        f"  姓名: {card.get('name') or '-'} · 职业: {card.get('occupationName') or '-'}",
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _ai_retry_abort_command() -> ConsoleCommandResult:
    """立即终止所有进行中的 AI 自动重试（下一次重试前生效）。"""
    if is_abort_requested():
        return ConsoleCommandResult(True, "AI 自动重试此前已被终止；如已恢复请重新执行 ai resume。")
    request_abort()
    logger.warning("AI auto-retry abort requested via console")
    return ConsoleCommandResult(
        True,
        "已请求终止 AI 自动重试：进行中的请求会在下一次重试前停止，并返回「已被手动终止」错误。\n"
        "恢复自动重试请执行：ai resume",
    )


def _ai_retry_resume_command() -> ConsoleCommandResult:
    """解除终止状态，恢复自动重试。"""
    was_aborted = is_abort_requested()
    clear_abort()
    logger.info("AI auto-retry abort cleared via console")
    if was_aborted:
        return ConsoleCommandResult(True, "已解除终止状态，AI 请求恢复正常自动重试。")
    return ConsoleCommandResult(True, "AI 自动重试本就处于正常状态，无需恢复。")


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------


def _scenario_list_command(
    args: list[str], scenarios_dir: Any, vector_store: Any
) -> ConsoleCommandResult:
    needle = " ".join(args).strip().casefold()
    try:
        items = list_scenarios(_resolve_scenarios_dir(scenarios_dir), vector_store)
    except Exception as exc:  # noqa: BLE001 - 控制台不应抛栈
        logger.exception("Failed to list scenarios")
        return ConsoleCommandResult(False, f"Failed to list scenarios: {exc}")
    if needle:
        items = [item for item in items if needle in f"{item['id']} {item['title']}".casefold()]
    if not items:
        return ConsoleCommandResult(True, "没有匹配的剧本")
    lines = [f"剧本共 {len(items)} 个:"]
    for item in items:
        vectors = "-" if item["vector_count"] is None else str(item["vector_count"])
        archived = " [已归档]" if item["archived"] else ""
        lines.append(
            f"  #{item['id']} {item['title']} · 所有者={item['owner_id']} · "
            f"版本={item['version']} · 向量={vectors}{archived}"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _scenario_owner_command(
    args: list[str], scenarios_dir: Any, user_manager: Any
) -> ConsoleCommandResult:
    if len(args) != 2:
        return ConsoleCommandResult(False, "Usage: scenario owner <scenario-id> <username-or-id>")
    scenario_id, identifier = args
    user = _find_user(identifier, user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {identifier}")
    try:
        result = set_owner(_resolve_scenarios_dir(scenarios_dir), scenario_id, int(user["id"]))
    except LookupError as exc:
        return ConsoleCommandResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to change scenario owner")
        return ConsoleCommandResult(False, f"Failed to change scenario owner: {exc}")

    _invalidate_scenarios_cache()
    name = user.get("username") or user["id"]
    log_user_action(
        logger,
        user_action_text(CONSOLE_ACTOR, "更改了剧本所有者"),
        剧本ID=result["id"],
        标题=result["title"],
        原所有者=result["previous_owner"],
        新所有者=name,
    )
    return ConsoleCommandResult(
        True,
        f"剧本 #{result['id']}「{result['title']}」所有者 {result['previous_owner']} -> {name}({user['id']})",
    )


def _scenario_delete_command(
    args: list[str],
    scenarios_dir: Any,
    rooms_dir: Any,
    vector_store: Any,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, {"--vectors-only", "--force"})
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: scenario delete <scenario-id> [--vectors-only|--force] [--yes]")
    scenario_id = positionals[0]
    if {"--vectors-only", "--force"} <= flags:
        return ConsoleCommandResult(False, "Use either --vectors-only or --force, not both")
    vectors_only = "--vectors-only" in flags
    force = "--force" in flags

    try:
        result = delete_scenario(
            _resolve_scenarios_dir(scenarios_dir),
            _resolve_rooms_dir(rooms_dir),
            scenario_id,
            vector_store,
            vectors_only=vectors_only,
            force=force,
        )
    except LookupError as exc:
        return ConsoleCommandResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to delete scenario")
        return ConsoleCommandResult(False, f"Failed to delete scenario: {exc}")

    _invalidate_scenarios_cache()
    mode = result["mode"]
    label = f"#{result['id']}「{result['title']}」"
    if mode == "vectors-only":
        action = "清除了剧本知识库（仅向量与索引）"
        message = (
            f"剧本 {label} 已清除知识库：向量 {result['deleted_vectors']} · "
            f"索引文件 {result['deleted_files']}（剧本保留）"
        )
    elif mode == "archived":
        action = "归档了剧本（仍有房间占用）"
        message = f"剧本 {label} 仍有 {result['active_rooms']} 个房间占用，已归档而非删除"
    else:
        action = "强制删除了剧本（仍有房间占用）" if mode == "force" else "删除了剧本"
        message = (
            f"剧本 {label} 已删除：向量 {result['deleted_vectors']} · "
            f"索引文件 {result['deleted_files']} · 封面 {'已删' if result['deleted_cover'] else '无'}"
        )
        if mode == "force":
            message += f"（{result['active_rooms']} 个房间仍在占用）"
    log_user_action(
        logger, user_action_text(CONSOLE_ACTOR, action), 剧本ID=result["id"], 标题=result["title"]
    )
    return ConsoleCommandResult(True, message)


def _scenario_validate_command(
    args: list[str], scenarios_dir: Any, vector_store: Any
) -> ConsoleCommandResult:
    if len(args) != 1:
        return ConsoleCommandResult(False, "Usage: scenario validate <scenario-id>")
    try:
        result = validate_scenario(_resolve_scenarios_dir(scenarios_dir), args[0], vector_store)
    except LookupError as exc:
        return ConsoleCommandResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to validate scenario")
        return ConsoleCommandResult(False, f"Failed to validate scenario: {exc}")
    vectors = "-" if result["vectors"] is None else str(result["vectors"])
    lines = [
        f"剧本 #{result['id']}「{result['title']}」· 版本 {result['version']}",
        f"  知识索引 {result['index_chunks']} 块 · 向量 {vectors} 条",
    ]
    if result["ok"]:
        lines.append("  结论：三者一致 ✅")
    else:
        lines.append("  问题：")
        lines.extend(f"    - {issue}" for issue in result["issues"])
    return ConsoleCommandResult(True, "\n".join(lines))


def _scenario_reindex_command(
    args: list[str],
    scenarios_dir: Any,
    vector_store: Any,
    embedding_provider: Any,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: scenario reindex <scenario-id> [--yes]")
    scenario_id = positionals[0]

    def _apply() -> ConsoleCommandResult:
        try:
            result = reindex_scenario(
                _resolve_scenarios_dir(scenarios_dir),
                scenario_id,
                vector_store=vector_store,
                embedding_provider=embedding_provider,
            )
        except LookupError as exc:
            return ConsoleCommandResult(False, str(exc))
        except ValueError as exc:
            return ConsoleCommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to reindex scenario")
            return ConsoleCommandResult(False, f"Failed to reindex scenario: {exc}")
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "重建了剧本知识索引"), 剧本ID=result["id"])
        return ConsoleCommandResult(
            True,
            f"剧本 #{result['id']}「{result['title']}」已重建 {result['chunks']} 个知识块"
            f"（来源={result['source']}）-> {result['index_path']}",
        )

    return _confirm_action(
        preview=f"将重建剧本 {scenario_id} 的知识索引与向量（会调用 embedding，可能较慢）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认重建剧本 {scenario_id} 的索引?",
    )


# ---------------------------------------------------------------------------
# vector / knowledge
# ---------------------------------------------------------------------------


def _vector_health_command(
    vector_store: Any, embedding_provider: Any, ocr_provider: Any
) -> ConsoleCommandResult:
    data = vector_admin.vector_health(vector_store, embedding_provider, ocr_provider)
    backend = data["backend"]
    total = "-" if data["total"] is None else str(data["total"])
    lines = [
        "向量库状态:",
        f"  后端: {backend.get('backend', '-')} · {'可用' if backend.get('available') else '不可用'}"
        + (f" · {backend.get('path')}" if backend.get("path") else ""),
        f"  向量总数: {total}",
        f"  Embedding: {data['embedding']}",
        f"  OCR: {'启用' if data['ocr']['enabled'] else '未启用'} · 可用={data['ocr']['available']}",
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _vector_prune_command(
    args: list[str],
    scenarios_dir: Any,
    vector_store: Any,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> ConsoleCommandResult:
    flags = {item.lower() for item in args}
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)

    scan = vector_admin.scan_orphans(vector_store, _resolve_scenarios_dir(scenarios_dir))
    if not scan["supported"]:
        return ConsoleCommandResult(False, "当前向量后端不支持枚举 scenario_id，无法识别孤儿向量")
    orphans = scan["orphans"]
    if not orphans:
        return ConsoleCommandResult(
            True, f"没有孤儿向量（库中 scenario_id 共 {len(scan['scenario_ids'])} 个，均对应现存剧本）"
        )

    def _apply() -> ConsoleCommandResult:
        result = vector_admin.prune_orphan_vectors(vector_store, orphans)
        log_user_action(
            logger, user_action_text(CONSOLE_ACTOR, "清理了孤儿向量"), 剧本数=len(orphans), 向量数=result["deleted"]
        )
        detail = " · ".join(f"#{sid}={count}" for sid, count in sorted(result["per_scenario"].items()))
        return ConsoleCommandResult(True, f"已清理孤儿向量：共 {result['deleted']} 条（{detail}）")

    return _confirm_action(
        preview="发现孤儿向量 scenario_id: " + ", ".join(f"#{sid}" for sid in orphans),
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认清理 {len(orphans)} 个剧本的孤儿向量?",
    )


def _knowledge_list_command(
    args: list[str], scenarios_dir: Any, vector_store: Any, ruleset_store: Any
) -> ConsoleCommandResult:
    needle = " ".join(args).strip()
    try:
        data = knowledge_admin.list_knowledge(
            _resolve_scenarios_dir(scenarios_dir), vector_store, ruleset_store, needle
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list knowledge")
        return ConsoleCommandResult(False, f"Failed to list knowledge: {exc}")
    lines = ["规则集知识库:"]
    if data["rulesets"]:
        for item in data["rulesets"]:
            lines.append(
                f"  {item.get('ruleset_id')} · 标题={item.get('title') or '-'} · "
                f"活动版本={item.get('active_version')} · 启用={item.get('enabled')}"
            )
    else:
        lines.append("  （无）")
    lines.append("剧本知识库:")
    if data["scenarios"]:
        for item in data["scenarios"]:
            vectors = "-" if item["vectors"] is None else str(item["vectors"])
            lines.append(
                f"  #{item['id']} {item['title']} · 版本={item['version']} · "
                f"索引 {item['chunks']} 块 · 向量 {vectors} 条"
            )
    else:
        lines.append("  （无）")
    return ConsoleCommandResult(True, "\n".join(lines))


def _knowledge_reindex_command(
    args: list[str], ruleset_store: Any, *, confirm: Callable[[str], bool] | None = None
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    ruleset_id = positionals[0] if positionals else None
    scope = ruleset_id or "全部规则集"

    def _apply() -> ConsoleCommandResult:
        try:
            results = knowledge_admin.reindex_rulesets(ruleset_store, ruleset_id)
        except ValueError as exc:
            return ConsoleCommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to reindex rulesets")
            return ConsoleCommandResult(False, f"Failed to reindex rulesets: {exc}")
        if not results:
            return ConsoleCommandResult(True, "没有可重建的规则集")
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "重建了规则集索引"), 规则集数=len(results))
        lines = [f"已重建 {len(results)} 个规则集:"]
        for item in results:
            lines.append(
                f"  {item['ruleset_id']} · 版本 {item['knowledge_version']} · {item['chunk_count']} 块"
            )
        return ConsoleCommandResult(True, "\n".join(lines))

    return _confirm_action(
        preview=f"将重建 {scope} 的索引（会调用 OCR/adapter，可能较慢）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认重建 {scope}?",
    )


# ---------------------------------------------------------------------------
# room
# ---------------------------------------------------------------------------


def _room_list_command(args: list[str], rooms_dir: Any) -> ConsoleCommandResult:
    rooms = room_admin.list_rooms_summary(_resolve_rooms_dir(rooms_dir), " ".join(args))
    if not rooms:
        return ConsoleCommandResult(True, "没有匹配的房间")
    lines = [f"房间共 {len(rooms)} 个:"]
    for room in rooms:
        archived = " [已归档]" if room["archived"] else ""
        lines.append(
            f"  {room['id']} {room['name']} · 码={room['room_code'] or '-'} · "
            f"剧本={room['scenario_title'] or room['scenario_id'] or '-'} · 成员={room['members']}{archived}"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _room_members_command(args: list[str], rooms_dir: Any) -> ConsoleCommandResult:
    if len(args) != 1:
        return ConsoleCommandResult(False, "Usage: room members <room-id>")
    try:
        data = room_admin.room_members(_resolve_rooms_dir(rooms_dir), args[0])
    except LookupError as exc:
        return ConsoleCommandResult(False, str(exc))
    lines = [f"房间「{data['name'] or data['id']}」成员 {len(data['members'])} 人:"]
    for member in data["members"]:
        state = "" if member["active"] else "（已移除）"
        lines.append(
            f"  {member['username'] or member['user_id']} · 账号角色={member['role'] or '-'} · "
            f"房间角色={member['room_role']}{state}"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _room_archive_command(
    args: list[str], rooms_dir: Any, app: Any, *, confirm: Callable[[str], bool] | None = None
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: room archive <room-id> [--yes]")
    room_id = positionals[0]

    def _apply() -> ConsoleCommandResult:
        try:
            result = room_admin.archive_room(_resolve_rooms_dir(rooms_dir), room_id, app=app)
        except LookupError as exc:
            return ConsoleCommandResult(False, str(exc))
        except ValueError as exc:
            return ConsoleCommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to archive room")
            return ConsoleCommandResult(False, f"Failed to archive room: {exc}")
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "归档了房间"), 房间ID=result["id"], 房间名=result["name"])
        snapshot = "（含归档快照）" if result["snapshot"] else "（未生成归档快照：控制台未注入 app）"
        return ConsoleCommandResult(True, f"房间 {result['id']}「{result['name']}」已归档{snapshot}")

    return _confirm_action(
        preview=f"将归档房间 {room_id}（要求已到达结局，与网页规则一致）。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认归档房间 {room_id}?",
    )


def _room_prune_command(
    args: list[str], rooms_dir: Any, *, confirm: Callable[[str], bool] | None = None
) -> ConsoleCommandResult:
    days = 30
    assume_yes = False
    remaining: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item.lower() in {"--days", "-d"}:
            if index + 1 >= len(args):
                return ConsoleCommandResult(False, "Usage: room prune [--days N] [--yes]")
            try:
                days = int(args[index + 1])
            except ValueError:
                return ConsoleCommandResult(False, f"--days 需要整数，收到 {args[index + 1]!r}")
            index += 2
            continue
        if item.lower() in YES_FLAGS:
            assume_yes = True
            index += 1
            continue
        remaining.append(item)
        index += 1
    if remaining:
        return ConsoleCommandResult(False, f"未知参数: {', '.join(remaining)}")
    if days < 1:
        return ConsoleCommandResult(False, "--days 必须 >= 1")

    stale = room_admin.find_stale_rooms(_resolve_rooms_dir(rooms_dir), days)
    if not stale:
        return ConsoleCommandResult(True, f"没有超过 {days} 天且无成员的僵尸房间")

    def _apply() -> ConsoleCommandResult:
        removed = 0
        for room in stale:
            if room_admin.remove_room(_resolve_rooms_dir(rooms_dir), room["id"]):
                removed += 1
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "清理了僵尸房间"), 房间数=removed, 天数=days)
        return ConsoleCommandResult(True, f"已清理 {removed} 个僵尸房间（阈值 {days} 天）")

    preview_lines = [f"发现 {len(stale)} 个僵尸房间（>{days} 天无活动且无有效成员）:"]
    preview_lines.extend(
        f"  {room['id']} {room['name']} · 剧本={room['scenario_title'] or '-'} · 最后活动={room['last_active']}"
        for room in stale
    )
    return _confirm_action(
        preview="\n".join(preview_lines),
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认删除这 {len(stale)} 个房间（含消息）?",
    )


# ---------------------------------------------------------------------------
# dice
# ---------------------------------------------------------------------------


def _dice_settings_command(args: list[str], rooms_dir: Any) -> ConsoleCommandResult:
    from trpg_server.agents.dice_settings import admin_dice_settings, resolve_dice_settings
    from trpg_server.json_store import read_json

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) > 1:
        return ConsoleCommandResult(False, "Usage: dice settings [room-id]")

    admin = admin_dice_settings()
    lines = [
        "管理员默认: "
        f"动画={admin.dice_animation_level} · "
        f"玩家投掷={'开' if admin.dice_player_roll else '关'} · "
        f"等待超时={admin.dice_player_roll_timeout}s · "
        f"重投={admin.dice_reroll_mode}/{admin.dice_reroll_limit}"
    ]
    if positionals:
        room_id = positionals[0]
        info_path = _resolve_rooms_dir(rooms_dir) / room_id / "info.json"
        info = read_json(info_path, default=None)
        if not isinstance(info, dict):
            return ConsoleCommandResult(False, f"未找到房间 {room_id}")
        settings = resolve_dice_settings(None, info.get("house_rules"))
        source = "房间规则" if settings["level_source"] == "room" else "管理员默认"
        player_source = "房间规则" if settings["player_roll_source"] == "room" else "管理员默认"
        reroll_source = "房间规则" if settings["reroll_source"] == "room" else "管理员默认"
        lines.append(
            f"房间 {room_id}: 动画={settings['level']}（{source}） · "
            f"玩家投掷={'开' if settings['player_roll'] else '关'}（{player_source}） · "
            f"等待超时={settings['timeout']}s · "
            f"重投={settings['reroll_mode']}/{settings['reroll_limit']}（{reroll_source}）"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _dice_pending_command(args: list[str], rooms_dir: Any) -> ConsoleCommandResult:
    from trpg_server.agents import player_roll

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) > 1:
        return ConsoleCommandResult(False, "Usage: dice pending [room-id]")
    room_id = positionals[0] if positionals else None
    items = player_roll.list_pending(room_id)
    if not items:
        return ConsoleCommandResult(True, "当前没有等待玩家投掷的请求")
    lines = [f"待投掷请求 {len(items)} 个:"]
    for item in items:
        roller = item.get("roller") or {}
        plan = item.get("plan") or {}
        lines.append(
            f"  {item.get('request_id')} · 房间={item.get('room_id')} · "
            f"玩家={roller.get('username') or roller.get('user_id') or '-'} · "
            f"工具={item.get('tool_name') or '-'} · 表达式={plan.get('expression') or '-'}"
        )
    return ConsoleCommandResult(True, "\n".join(lines))


def _dice_textures_command(args: list[str]) -> ConsoleCommandResult:
    from trpg_server import dice_textures

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals:
        return ConsoleCommandResult(False, "Usage: dice textures")
    data = dice_textures.library(include_disabled=True)
    lines = [
        f"贴图资产 {len(data['assets'])} · 桌面皮肤 {len(data['table_skins'])} · 骰子皮肤 {len(data['dice_skins'])}"
    ]
    for asset in data["assets"]:
        state = "" if asset.get("enabled", True) else "（停用）"
        lines.append(
            f"  资产 {asset.get('id')} · {asset.get('name')} · {asset.get('kind')} · "
            f"{asset.get('width')}×{asset.get('height')}{state}"
        )
    for skin in data["table_skins"]:
        lines.append(f"  桌面皮肤 {skin.get('id')} · {skin.get('name')} · 资产={skin.get('asset_id')}")
    for skin in data["dice_skins"]:
        lines.append(f"  骰子皮肤 {skin.get('id')} · {skin.get('name')} · {skin.get('die_type')}")
    return ConsoleCommandResult(True, "\n".join(lines))


def _dice_apply_setting(
    *,
    key: str,
    value: Any,
    success: str,
    preview: str,
    flags: set[str],
    confirm: Callable[[str], bool] | None,
) -> ConsoleCommandResult:
    """写入管理员默认骰子设置（破坏性写操作，默认预演）。"""
    from trpg_server.agents.config import write_ai_setting

    def _apply() -> ConsoleCommandResult:
        write_ai_setting(key, value)
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "修改了骰子默认设置"), 配置项=key, 值=str(value))
        return ConsoleCommandResult(True, success)

    return _confirm_action(
        preview=preview,
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认{preview}",
    )


def _dice_admin_config_lines() -> list[str]:
    from trpg_server.agents.dice_settings import admin_dice_settings

    admin = admin_dice_settings()
    return [
        f"动画档位: {admin.dice_animation_level}",
        f"玩家投掷: {'开' if admin.dice_player_roll else '关'}",
        f"等待超时: {admin.dice_player_roll_timeout}s",
        f"重投模式: {admin.dice_reroll_mode}",
        f"重投上限: {admin.dice_reroll_limit}",
    ]


def _dice_animation_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    from trpg_server.agents.dice_settings import DICE_ANIMATION_LEVELS

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals == ["show"]:
        return ConsoleCommandResult(True, "\n".join(_dice_admin_config_lines()[:1]))
    if len(positionals) == 2 and positionals[0] == "set":
        value = positionals[1].lower()
        if value not in DICE_ANIMATION_LEVELS:
            return ConsoleCommandResult(False, "动画档位只能是 text / simple / 3d")
        return _dice_apply_setting(
            key="dice_animation_level",
            value=value,
            success=f"管理员默认动画档位已设为 {value}",
            preview=f"把管理员默认骰子动画档位设为 {value}",
            flags=flags,
            confirm=confirm,
        )
    return ConsoleCommandResult(False, "Usage: dice animation show | dice animation set <text|simple|3d> [--yes]")


def _dice_player_roll_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals == ["show"]:
        return ConsoleCommandResult(True, "\n".join(_dice_admin_config_lines()[1:2]))
    if len(positionals) == 1 and positionals[0] in {"on", "off"}:
        enabled = positionals[0] == "on"
        return _dice_apply_setting(
            key="dice_player_roll",
            value=enabled,
            success=f"管理员默认玩家投掷已设为 {'开' if enabled else '关'}",
            preview=f"把管理员默认玩家投掷设为 {'开' if enabled else '关'}",
            flags=flags,
            confirm=confirm,
        )
    return ConsoleCommandResult(False, "Usage: dice player-roll show | dice player-roll <on|off> [--yes]")


def _dice_reroll_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    from trpg_server.agents.dice_settings import DICE_REROLL_MODES

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals == ["show"]:
        return ConsoleCommandResult(True, "\n".join(_dice_admin_config_lines()[3:]))
    if len(positionals) == 1 and positionals[0] in DICE_REROLL_MODES:
        value = positionals[0]
        return _dice_apply_setting(
            key="dice_reroll_mode",
            value=value,
            success=f"管理员默认重投模式已设为 {value}",
            preview=f"把管理员默认重投模式设为 {value}",
            flags=flags,
            confirm=confirm,
        )
    return ConsoleCommandResult(False, "Usage: dice reroll show | dice reroll <off|limited|unlimited> [--yes]")


def _dice_reroll_limit_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: dice reroll-limit <1-20> [--yes]")
    try:
        value = int(positionals[0])
    except ValueError:
        return ConsoleCommandResult(False, "重投上限必须是 1-20 的整数")
    if not 1 <= value <= 20:
        return ConsoleCommandResult(False, "重投上限必须是 1-20 的整数")
    return _dice_apply_setting(
        key="dice_reroll_limit",
        value=value,
        success=f"管理员默认重投上限已设为 {value}",
        preview=f"把管理员默认重投上限设为 {value}",
        flags=flags,
        confirm=confirm,
    )


def _dice_timeout_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: dice timeout <30-300> [--yes]")
    try:
        value = int(positionals[0])
    except ValueError:
        return ConsoleCommandResult(False, "等待超时必须是 30-300 的整数（秒）")
    if not 30 <= value <= 300:
        return ConsoleCommandResult(False, "等待超时必须是 30-300 的整数（秒）")
    return _dice_apply_setting(
        key="dice_player_roll_timeout",
        value=value,
        success=f"管理员默认等待超时已设为 {value}s",
        preview=f"把管理员默认等待超时设为 {value}s",
        flags=flags,
        confirm=confirm,
    )


def _dice_skin_delete_command(
    args: list[str], *, confirm: Callable[[str], bool] | None = None
) -> ConsoleCommandResult:
    from trpg_server import dice_textures

    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: dice skin delete <skin-id> [--yes]")
    skin_id = positionals[0]

    def _apply() -> ConsoleCommandResult:
        try:
            dice_textures.delete_skin(skin_id)
        except dice_textures.TextureError as exc:
            return ConsoleCommandResult(False, exc.reason)
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "删除了骰子皮肤"), 皮肤ID=skin_id)
        return ConsoleCommandResult(True, f"已删除皮肤 {skin_id}")

    return _confirm_action(
        preview=f"将删除骰子皮肤 {skin_id}（已选择该皮肤的玩家会回退到纯色）。",
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt=f"确认删除皮肤 {skin_id}?",
    )


# ---------------------------------------------------------------------------
# network / autosave / backup / owner
# ---------------------------------------------------------------------------


def _network_status_command() -> ConsoleCommandResult:
    data = network_admin.network_status()
    lines = [
        "网络状态:",
        f"  本地 IP: {data['local_ip']} · 端口 {data['port']} · 端口可用={data['port_available']}",
        f"  局域网发现: {'开启' if data['discovery_enabled'] else '关闭'}",
        f"  内网穿透: {'开启' if data['penetration_enabled'] else '关闭'} · 类型={data['penetration_type']}",
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _network_test_command() -> ConsoleCommandResult:
    data = network_admin.network_test()
    lines = [
        "网络连通性测试:",
        f"  本地 IP: {data['local_ip']} · 端口 {data['port']}",
        f"  端口绑定: {'成功' if data['connection_success'] else '失败'}",
        f"  UDP 发现: {'成功' if data['discovery_success'] else '失败'}"
        + (f" · {data['discovery_error']}" if data["discovery_error"] else ""),
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _config_show_command(args: list[str]) -> ConsoleCommandResult:
    section: str | None = None
    index = 0
    while index < len(args):
        item = args[index]
        if item.lower() == "--section":
            if index + 1 >= len(args):
                return ConsoleCommandResult(False, "Usage: config show [--section <name>]")
            section = args[index + 1]
            index += 2
            continue
        return ConsoleCommandResult(False, f"未知参数: {item}（用法: config show [--section <name>]）")
    return ConsoleCommandResult(True, config_admin.format_settings(section))


def _config_migrate_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals:
        return ConsoleCommandResult(False, f"未知参数: {', '.join(positionals)}")
    if not config_admin.legacy_files_present():
        return ConsoleCommandResult(True, config_admin.format_migration_preview())

    def _apply() -> ConsoleCommandResult:
        actions = config_admin.migrate_legacy_config()
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "迁移了遗留JSON配置到 general.toml"))
        return ConsoleCommandResult(True, "\n".join(actions) if actions else "没有需要迁移的内容。")

    return _confirm_action(
        preview=config_admin.format_migration_preview(),
        apply=_apply,
        assume_yes=_assume_yes(flags),
        confirm=confirm,
        prompt="确认执行配置迁移?",
    )


def _config_path_command(args: list[str]) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if positionals:
        return ConsoleCommandResult(False, f"未知参数: {', '.join(positionals)}")
    info = config_admin.config_path_info()
    lines = [
        f"配置目录: {info['config_dir']}",
        f"模板目录: {info['template_dir']}",
        f"用户配置: {info['general_config']}",
    ]
    return ConsoleCommandResult(True, "\n".join(lines))


def _autosave_command(app: Any) -> ConsoleCommandResult:
    try:
        room_admin.trigger_autosave(app)
    except ValueError as exc:
        return ConsoleCommandResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to trigger autosave")
        return ConsoleCommandResult(False, f"Failed to trigger autosave: {exc}")
    return ConsoleCommandResult(True, "已触发一次自动存档检查（为到期房间写入自动存档）")


def _backup_create_command(args: list[str], *, confirm: Callable[[str], bool] | None = None) -> ConsoleCommandResult:
    out_dir: str | None = None
    assume_yes = False
    index = 0
    remaining: list[str] = []
    while index < len(args):
        item = args[index]
        if item.lower() in {"--out", "-o"}:
            if index + 1 >= len(args):
                return ConsoleCommandResult(False, "Usage: backup create [--out <dir>] [--yes]")
            out_dir = args[index + 1]
            index += 2
            continue
        if item.lower() in YES_FLAGS:
            assume_yes = True
            index += 1
            continue
        remaining.append(item)
        index += 1
    if remaining:
        return ConsoleCommandResult(False, f"未知参数: {', '.join(remaining)}")

    def _apply() -> ConsoleCommandResult:
        try:
            result = backup_admin.create_backup(out_dir)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create backup")
            return ConsoleCommandResult(False, f"Failed to create backup: {exc}")
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "创建了数据备份"), 文件=result["path"])
        return ConsoleCommandResult(
            True,
            f"备份已完成：{result['path']} · {result['entries']} 个文件 · {result['bytes'] / 1024:.1f} KB",
        )

    try:
        from trpg_server.settings import DATA_DIR

        default_out = str(Path(DATA_DIR) / "backups")
    except Exception:  # noqa: BLE001
        default_out = "data/backups"
    return _confirm_action(
        preview=f"将打包 config/scenarios/rooms/知识库/用户库/日志 到 {out_dir or default_out}。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt="确认创建备份?",
    )


def _owner_transfer_command(
    args: list[str],
    user_manager: Any,
    *,
    confirm: Callable[[str], bool] | None = None,
) -> ConsoleCommandResult:
    positionals, flags = _split_args(args)
    assume_yes = _assume_yes(flags)
    unknown = _reject_unknown_flags(flags - YES_FLAGS, set())
    if unknown:
        return ConsoleCommandResult(False, unknown)
    if len(positionals) != 1:
        return ConsoleCommandResult(False, "Usage: owner transfer <username-or-id> [--yes]")
    user = _find_user(positionals[0], user_manager)
    if not user:
        return ConsoleCommandResult(False, f"User not found: {positionals[0]}")
    name = user.get("username") or user["id"]

    def _apply() -> ConsoleCommandResult:
        try:
            result = user_admin.transfer_owner(user_manager, int(user["id"]))
        except (LookupError, ValueError) as exc:
            return ConsoleCommandResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to transfer owner")
            return ConsoleCommandResult(False, f"Failed to transfer owner: {exc}")
        log_user_action(logger, user_action_text(CONSOLE_ACTOR, "转移了系统OWNER"), 新所有者=result["username"])
        demoted = f"（原 OWNER 已降为 ADMIN：{', '.join(result['demoted'])}）" if result["demoted"] else ""
        return ConsoleCommandResult(True, f"系统 OWNER 已转移给 {result['username']}({result['id']}){demoted}")

    return _confirm_action(
        preview=f"将把系统 OWNER 身份转移给 {name}，原 OWNER 降为 ADMIN。",
        apply=_apply,
        assume_yes=assume_yes,
        confirm=confirm,
        prompt=f"确认把 OWNER 转移给 {name}?",
    )
