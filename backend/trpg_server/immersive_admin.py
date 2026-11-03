"""站点级「实时沉浸系统」控制台命令（独立于 Flask 与请求上下文）。

职责：
- 查看/开关站点级总开关与配乐、配图子开关；
- 调整配图服务（程序化 / OpenAI 兼容）、地址、模型、尺寸、预算与配额；
- 写入生图密钥（敏感，默认预演，``--yes`` 才落盘，日志不回显密钥）；
- 真实连通性测试（会产生一次生图费用，默认预演）；
- 清理图片缓存、重置单个房间的沉浸状态（破坏性，默认预演）。

配置写入沿用 :mod:`trpg_server.config.writer`（tomlkit 往返，保留注释）。
本模块不依赖 Flask，依赖目录由调用方注入，便于在浏览器控制台与测试中复用。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from trpg_server.config import update_sections
from trpg_server.config.paths import general_config_path, resolve_config_dir
from trpg_server.immersive import audit, director, image_platforms, scenario_cache
from trpg_server.immersive.config import (
    DIRECTOR_FALLBACKS,
    INTENSITIES,
    PROFILES,
    PROVIDERS,
    load_immersive_settings,
)
from trpg_server.immersive.illustration import (
    IMAGE_CACHE_DIR,
    USAGE_PATH,
    usage_snapshot,
)
from trpg_server.settings import ROOMS_DIR, SCENARIOS_DIR

logger = logging.getLogger(__name__)

YES_FLAGS = {"--yes", "-y"}
_ON = {"on", "enable", "enabled", "true", "1", "开", "开启"}
_OFF = {"off", "disable", "disabled", "false", "0", "关", "关闭"}


def _as_bool_arg(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    return None


def _write(values: dict[str, Any], config_dir: Any) -> None:
    update_sections(general_config_path(resolve_config_dir(config_dir)), {"immersive": values})


def _redacted_view(settings: dict[str, Any], config_dir: Any = None) -> dict[str, Any]:
    view = dict(settings)
    view.pop("locked", None)
    view["api_key"] = "已配置" if settings.get("api_key") else "未配置"
    view["platforms"] = [
        {key: item.get(key) for key in ("id", "name", "model", "size", "enabled", "is_default", "has_api_key")}
        for item in image_platforms.list_platforms(config_dir)
    ]
    view["director_usage"] = director.usage_snapshot()
    return view


def _bool_label(value: Any) -> str:
    return "开" if value else "关"


def _usage_text() -> str:
    usage = usage_snapshot()
    daily_used = int(usage.get("daily_used") or 0)
    rooms = usage.get("rooms") if isinstance(usage.get("rooms"), dict) else {}
    director_usage = director.usage_snapshot()
    lines = [f"今日外部生图提交: {daily_used}", f"今日导演调用: {director_usage.get('calls') or 0}"]
    for room_id, entry in sorted(rooms.items()):
        if isinstance(entry, dict):
            lines.append(f"  {room_id}: 本小时 {entry.get('hour_used') or 0} 次（{entry.get('hour') or '-'}）")
    return "\n".join(lines)


def handle_immersive_command(
    args: list[str],
    *,
    confirm: Callable[[str], bool] | None = None,
    rooms_dir: Any = None,
    config_dir: Any = None,
) -> tuple[bool, str]:
    """执行一条 ``immersive`` 控制台命令，返回 ``(ok, message)``。"""
    if not args:
        return True, immersive_help_text()

    sub = args[0].lower()
    rest = args[1:]
    flags = {item.lower() for item in rest if item.startswith("-")}
    positionals = [item for item in rest if not item.startswith("-")]
    assume_yes = bool(flags & YES_FLAGS)
    settings = load_immersive_settings(config_dir)

    if sub in {"help", "?"}:
        return True, immersive_help_text()

    if sub == "status":
        if positionals and positionals[0].lower() == "json":
            payload = {"settings": _redacted_view(settings, config_dir), "usage": usage_snapshot()}
            return True, json.dumps(payload, ensure_ascii=False, indent=2)
        lines = [
            "实时沉浸系统状态：",
            f"  总开关: {_bool_label(settings.get('enabled'))}",
            f"  配乐: {_bool_label(settings.get('music_enabled'))} · 自动切换 {_bool_label(settings.get('music_auto_switch'))} "
            f"· 默认 {settings.get('music_default_profile')}/{settings.get('music_default_intensity')} "
            f"· 音量 {settings.get('music_volume')}% · 交叉淡入 {settings.get('music_crossfade_ms')}ms",
            f"  配图: {_bool_label(settings.get('illustration_enabled'))} · 服务 {settings.get('illustration_provider')} "
            f"· 自动 {_bool_label(settings.get('illustration_auto'))} · 默认平台 {_default_platform_text(settings, config_dir)}",
            f"  预算/配额: 每日 {settings.get('illustration_daily_budget') or '不限'} 次 · 房间每小时 {settings.get('illustration_room_hourly_cap') or '不限'} 次 "
            f"· 自动最小间隔 {settings.get('illustration_min_turns')} 回合 · 并发 {settings.get('illustration_concurrency')}",
            _director_status_line(settings, config_dir),
            _usage_text(),
            "  提示：房间内开关使用房间控制台 `immersive ...`，站点级设置请用本模块。",
        ]
        return True, "\n".join(lines)

    if sub in {"on", "off", "enable", "disable"}:
        value = sub in {"on", "enable"}

        def _apply() -> tuple[bool, str]:
            _write({"enabled": value}, config_dir)
            logger.info("immersive site enabled=%s by console", value)
            return True, f"站点沉浸系统已{'开启' if value else '关闭'}（房间内仍需各自的开关与房规）"

        return _confirm(
            preview=f"将把站点沉浸系统总开关设为『{'开' if value else '关'}』。",
            apply=_apply,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认修改站点沉浸总开关?",
        )

    if sub == "music":
        if not positionals:
            return True, _music_summary(settings)
        action = positionals[0].lower()
        if action in {"on", "off", "enable", "disable"}:
            value = action in {"on", "enable"}

            def _apply_music() -> tuple[bool, str]:
                _write({"music_enabled": value}, config_dir)
                logger.info("immersive music enabled=%s by console", value)
                return True, f"站点配乐已{'开启' if value else '关闭'}"

            return _confirm(
                preview=f"将把站点配乐开关设为『{'开' if value else '关'}』。",
                apply=_apply_music,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认修改站点配乐开关?",
            )
        if action == "auto" and len(positionals) > 1:
            value = _as_bool_arg(positionals[1])
            if value is None:
                return False, "Usage: immersive music auto <on|off> [--yes]"

            def _apply_auto() -> tuple[bool, str]:
                _write({"music_auto_switch": value}, config_dir)
                return True, f"配乐自动切换已{'开启' if value else '关闭'}"

            return _confirm(
                preview=f"将把配乐自动切换设为『{'开' if value else '关'}』。",
                apply=_apply_auto,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认修改自动切换?",
            )
        if action == "volume" and len(positionals) > 1:
            return _set_int("music_volume", positionals[1], 0, 100, "配乐音量", config_dir, assume_yes, confirm)
        if action == "crossfade" and len(positionals) > 1:
            return _set_int("music_crossfade_ms", positionals[1], 500, 10000, "交叉淡入时长(ms)", config_dir, assume_yes, confirm)
        if action == "hysteresis" and len(positionals) > 1:
            return _set_int("music_hysteresis_turns", positionals[1], 0, 10, "切换滞回回合数", config_dir, assume_yes, confirm)
        if action == "seed":
            value = " ".join(positionals[1:]).strip()
            if not value:
                return False, "Usage: immersive music seed <seed>|clear [--yes]"

            def _apply_seed() -> tuple[bool, str]:
                _write({"music_seed": "" if value == "clear" else value[:64]}, config_dir)
                return True, "配乐种子已" + ("清除（按房间自动生成）" if value == "clear" else "固定，便于复现同一房间的音乐走向")

            return _confirm(
                preview=f"将把配乐基准种子设为『{value}』。",
                apply=_apply_seed,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认修改配乐种子?",
            )
        return False, _music_usage()

    if sub == "illustration":
        if not positionals:
            return True, _illustration_summary(settings, config_dir)
        action = positionals[0].lower()
        if action in {"on", "off", "enable", "disable"}:
            value = action in {"on", "enable"}

            def _apply_illustration() -> tuple[bool, str]:
                _write({"illustration_enabled": value}, config_dir)
                logger.info("immersive illustration enabled=%s by console", value)
                return True, f"站点配图已{'开启' if value else '关闭'}"

            return _confirm(
                preview=f"将把站点配图开关设为『{'开' if value else '关'}』。",
                apply=_apply_illustration,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认修改站点配图开关?",
            )
        if action == "auto" and len(positionals) > 1:
            value = _as_bool_arg(positionals[1])
            if value is None:
                return False, "Usage: immersive illustration auto <on|off> [--yes]"

            def _apply_auto2() -> tuple[bool, str]:
                _write({"illustration_auto": value}, config_dir)
                return True, f"自动配图已{'开启' if value else '关闭'}"

            return _confirm(
                preview=f"将把自动配图设为『{'开' if value else '关'}』。",
                apply=_apply_auto2,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认修改自动配图?",
            )
        if action == "provider" and len(positionals) > 1:
            value = positionals[1].lower()
            if value not in PROVIDERS:
                return False, f"服务只能是 {list(PROVIDERS)}"
            return _set_value(
                "illustration_provider", value, f"配图服务为 {value}", config_dir, assume_yes, confirm,
                "改为外部服务会产生费用；未配置默认生图平台时会自动回落到程序化图。",
            )
        if action in {"base-url", "model", "size"}:
            return False, (
                f"`immersive illustration {action}` 已弃用：生图地址/模型/尺寸统一在"
                "「生图模型平台配置」里维护，请用 `immersive platform` 系列命令。"
            )
        if action == "timeout" and len(positionals) > 1:
            return _set_int("illustration_timeout", positionals[1], 20, 600, "生图超时(秒)", config_dir, assume_yes, confirm)
        if action == "concurrency" and len(positionals) > 1:
            return _set_int("illustration_concurrency", positionals[1], 1, 4, "生图并发", config_dir, assume_yes, confirm)
        if action == "budget" and len(positionals) > 1:
            return _set_int("illustration_daily_budget", positionals[1], 0, 100000, "每日预算（0=不限）", config_dir, assume_yes, confirm)
        if action == "cap" and len(positionals) > 1:
            return _set_int("illustration_room_hourly_cap", positionals[1], 0, 500, "房间每小时上限（0=不限）", config_dir, assume_yes, confirm)
        if action == "min-turns" and len(positionals) > 1:
            return _set_int("illustration_min_turns", positionals[1], 1, 50, "自动配图最小间隔（回合）", config_dir, assume_yes, confirm)
        return False, _illustration_usage()

    if sub == "scenario":
        return _handle_scenario_cache_command(
            positionals,
            settings=settings,
            assume_yes=assume_yes,
            confirm=confirm,
            config_dir=config_dir,
        )

    if sub == "platform":
        return _handle_platform_command(
            rest,
            flags=flags,
            positionals=positionals,
            assume_yes=assume_yes,
            confirm=confirm,
            config_dir=config_dir,
        )

    if sub == "director":
        return _handle_director_command(
            rest,
            positionals=positionals,
            assume_yes=assume_yes,
            confirm=confirm,
            config_dir=config_dir,
            settings=settings,
        )

    if sub == "key":
        action = positionals[0].lower() if positionals else ""
        platform = image_platforms.resolve_platform(config_dir, settings)
        if platform is None:
            return False, "未配置可用的生图平台；请先 `immersive platform` 查看并创建平台。"
        platform_id = str(platform.get("id") or "")
        if action == "set" and len(positionals) > 1:
            key = " ".join(positionals[1:]).strip()
            if not key:
                return False, "Usage: immersive key set <api_key> [--yes]"
            if len(key) > 4096:
                return False, "API Key 过长"

            def _apply_key() -> tuple[bool, str]:
                image_platforms.set_platform_secret(config_dir, platform_id, key)
                # 只记录长度与平台，绝不把密钥写进日志。
                logger.info("immersive image api key updated platform=%s length=%s", platform_id, len(key))
                return True, f"密钥已写入默认平台 {platform_id}（日志已脱敏）"

            return _confirm(
                preview=f"将把生图 API Key 写入默认平台 {platform_id}（不会回显密钥内容）。",
                apply=_apply_key,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认写入生图 API Key?",
            )
        if action in {"clear", "remove"}:
            def _clear_key() -> tuple[bool, str]:
                image_platforms.set_platform_secret(config_dir, platform_id, "")
                logger.info("immersive image api key cleared platform=%s", platform_id)
                return True, f"平台 {platform_id} 的密钥已清除"

            return _confirm(
                preview=f"将清除默认平台 {platform_id} 的密钥（外部生图会立即降级为程序化图）。",
                apply=_clear_key,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认清除生图 API Key?",
            )
        return False, "Usage: immersive key set <api_key> [--yes] | immersive key clear [--yes]"

    if sub == "test":
        platform = image_platforms.resolve_platform(config_dir, settings)
        if settings.get("illustration_provider") == "procedural":
            return True, "当前是程序化服务：本地 SVG 始终可用，无需外部连通性测试。"
        if platform is None:
            return False, "未配置可用的生图平台；请先 `immersive platform` 创建并设为默认。"

        def _apply_test() -> tuple[bool, str]:
            result = image_platforms.probe_platform(
                config_dir, str(platform.get("id") or ""), allow_generation=True
            )
            return True, _probe_text(result)

        return _confirm(
            preview=(
                f"将对默认平台 {platform.get('id')} 做连通性测试：先列模型，失败时会真实生成一张最小图"
                "（可能产生外部服务费用）。"
            ),
            apply=_apply_test,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认执行生图连通性测试?",
        )

    if sub == "usage":
        return True, _usage_text()

    if sub == "cache" and positionals and positionals[0].lower() == "clear":
        def _clear_cache() -> tuple[bool, str]:
            removed = 0
            if IMAGE_CACHE_DIR.exists():
                for child in IMAGE_CACHE_DIR.iterdir():
                    if child.is_file():
                        child.unlink(missing_ok=True)
                        removed += 1
            USAGE_PATH.unlink(missing_ok=True)
            return True, f"已清理沉浸图片缓存 {removed} 个文件，并重置用量记录"

        return _confirm(
            preview="将删除全部沉浸图片缓存与当日用量记录（不可恢复；已有房间历史中的图片链接会失效）。",
            apply=_clear_cache,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认清理图片缓存?",
        )

    if sub == "room":
        action = positionals[0].lower() if positionals else ""
        if action == "reset" and len(positionals) > 1:
            room_id = positionals[1]
            room_dir = _safe_room_dir(rooms_dir, room_id)
            if room_dir is None:
                return False, "房间 id 非法或不存在"

            def _reset_room() -> tuple[bool, str]:
                removed = []
                for name in ("immersive_music.json", "immersive_illustration.json"):
                    path = room_dir / name
                    if path.exists():
                        path.unlink()
                        removed.append(name)
                logger.info("immersive room reset room=%s files=%s", room_id, ",".join(removed) or "-")
                return True, f"已重置房间 {room_id} 的沉浸状态（{', '.join(removed) or '无需清理'}）"

            return _confirm(
                preview=f"将清除房间 {room_id} 的配乐/配图状态与历史（不影响房间消息与存档）。",
                apply=_reset_room,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt=f"确认重置房间 {room_id}?",
            )
        if action == "status" and len(positionals) > 1:
            room_dir = _safe_room_dir(rooms_dir, positionals[1])
            if room_dir is None:
                return False, "房间 id 非法或不存在"
            return True, _room_state_text(room_dir, positionals[1])
        return False, "Usage: immersive room reset <room_id> [--yes] | immersive room status <room_id>"

    return False, f"未知命令: immersive {sub}\n" + immersive_help_text()


def immersive_help_text() -> str:
    return "\n".join(
        [
            "immersive 命令（站点级，作用于所有房间；房间内请用房间控制台）：",
            "  immersive status [json]                          （查看开关、档位、平台、导演与今日用量）",
            "  immersive on|off [--yes]                         （站点沉浸总开关）",
            "  immersive music                                  （配乐设置概览）",
            "  immersive music on|off [--yes]                   （站点配乐开关）",
            "  immersive music auto <on|off> [--yes]            （自动切换配乐）",
            "  immersive music volume <0-100> [--yes]           （默认音量）",
            "  immersive music crossfade <500-10000> [--yes]    （交叉淡入毫秒）",
            "  immersive music hysteresis <0-10> [--yes]        （切换滞回回合）",
            "  immersive music seed <seed>|clear [--yes]        （固定可复现种子）",
            "  immersive illustration                           （配图设置概览）",
            "  immersive illustration on|off [--yes]            （站点配图开关）",
            "  immersive illustration auto <on|off> [--yes]     （自动配图）",
            "  immersive illustration provider <procedural|platform> [--yes]",
            "  immersive illustration timeout <20-600> [--yes]  （单次请求总预算秒数）",
            "  immersive illustration concurrency <1-4> [--yes] （后台并发）",
            "  immersive illustration budget <n> [--yes]        （每日提交上限，0=不限）",
            "  immersive illustration cap <n> [--yes]           （房间每小时上限，0=不限）",
            "  immersive illustration min-turns <1-50> [--yes]  （自动配图最小间隔回合）",
            "  immersive scenario                               （剧本配图缓存概览）",
            "  immersive scenario record <on|off> [--yes]       （是否把新图写入剧本目录）",
            "  immersive scenario prefer <on|off> [--yes]       （游玩时是否优先复用剧本图）",
            "  immersive scenario quota <MB> [--yes]            （单剧本容量上限，0=不限）",
            "  immersive scenario max-image <MB> [--yes]        （单图写入上限）",
            "  immersive scenario overflow <lru|reject> [--yes] （超限策略）",
            "  immersive scenario stats [scenario_id]           （查看缓存文件数与体积）",
            "  immersive scenario clear [scenario_id] [--yes]   （删除缓存，默认预演）",
            "  immersive scenario prune [scenario_id] [--yes]   （按配额 LRU 清理，默认预演）",
            "  immersive platform                               （列出生图平台表）",
            "  immersive platform show <id>                     （查看单条平台）",
            "  immersive platform use <id> [--yes]              （设为默认生图平台）",
            "  immersive platform enable|disable <id> [--yes]   （启用/停用平台）",
            "  immersive platform delete <id> [--yes]           （破坏性；默认预演）",
            "  immersive platform key <id> set <key>|clear [--yes] （敏感；密钥不回显）",
            "  immersive platform test <id> [--no-generate] [--yes] （先列模型，必要时最小生图）",
            "  immersive platform migrate [--yes]               （把旧单条配置迁移为平台条目）",
            "  immersive director                               （AI 音乐导演概览）",
            "  immersive director on|off [--yes]                （导演开关）",
            "  immersive director platform <id>|clear [--yes]   （导演使用的 AI 平台）",
            "  immersive director model <name>|clear [--yes]    （导演模型；空=平台默认）",
            "  immersive director interval <1-20> [--yes]       （每 N 回合尝试一次）",
            "  immersive director budget <n> [--yes]            （每日调用预算，0=不限）",
            "  immersive director ttl <0-86400> [--yes]         （乐谱缓存 TTL 秒，0=不复用）",
            "  immersive director min-confidence <0-100> [--yes]（低于该值回退规则分析器）",
            "  immersive director fallback <rule|hold> [--yes]  （失败回退策略）",
            "  immersive director cache [clear] [--yes]          （查看 / 清理乐谱缓存）",
            "  immersive key set <api_key>|clear [--yes]        （写入默认平台密钥；默认预演）",
            "  immersive test [--yes]                           （探测默认生图平台，可能产生费用）",
            "  immersive usage                                  （今日外部生图与导演用量）",
            "  immersive cache clear [--yes]                    （清理运行期图片缓存与用量）",
            "  immersive scenario ...                           （剧本配图缓存，见上）",
            "  immersive room status <room_id>                  （查看单房间沉浸状态）",
            "  immersive room reset <room_id> [--yes]           （清除单房间沉浸状态与历史）",
            "  说明：破坏性/敏感命令缺省为「预演」，加 --yes 或交互确认输入 y 才会执行。",
        ]
    )


def _confirm(
    *,
    preview: str,
    apply: Callable[[], tuple[bool, str]],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    prompt: str,
) -> tuple[bool, str]:
    if assume_yes:
        return apply()
    if confirm is None:
        return True, preview + "\n（预演模式：确认无误后加 --yes 执行）"
    if confirm(f"{prompt} (y/N)"):
        return apply()
    return True, preview + "\n（已取消，未执行）"


def _set_value(
    key: str,
    value: Any,
    summary: str,
    config_dir: Any,
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    extra: str = "",
) -> tuple[bool, str]:
    def _apply() -> tuple[bool, str]:
        _write({key: value}, config_dir)
        return True, summary

    preview = f"将设置 {key} = {value!r}。" + (f"\n{extra}" if extra else "")
    return _confirm(preview=preview, apply=_apply, assume_yes=assume_yes, confirm=confirm, prompt="确认修改?")


def _set_int(
    key: str,
    raw: str,
    minimum: int,
    maximum: int,
    label: str,
    config_dir: Any,
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False, f"{label}必须是 {minimum}-{maximum} 的整数"
    if not minimum <= value <= maximum:
        return False, f"{label}必须是 {minimum}-{maximum} 的整数"
    return _set_value(key, value, f"{label}已设为 {value}", config_dir, assume_yes, confirm)


def _default_platform_text(settings: dict[str, Any], config_dir: Any) -> str:
    platform = image_platforms.resolve_platform(config_dir, settings)
    if not platform:
        return "（未配置）"
    key_state = "有密钥" if platform.get("has_api_key") else "无密钥"
    return f"{platform.get('id')} / {platform.get('model') or '-'} / {key_state}"


def _director_status_line(settings: dict[str, Any], config_dir: Any) -> str:
    usage = director.usage_snapshot()
    cache = director.cache_stats(ttl=int(settings.get("music_director_cache_ttl") or 0))
    target = director.resolve_ai_target(
        config_dir,
        platform_id=str(settings.get("music_director_platform") or ""),
        model_id=str(settings.get("music_director_model") or ""),
    )
    model_text = f"{target['platform']}/{target['model']}" if target else "（无可用 AI 平台）"
    return (
        f"  音乐导演: {_bool_label(settings.get('music_director_enabled'))} · 平台 {model_text} "
        f"· 每 {settings.get('music_director_interval_turns')} 回合 · 预算 {settings.get('music_director_daily_budget') or '不限'} "
        f"· TTL {settings.get('music_director_cache_ttl')}s · 最低置信 {settings.get('music_director_min_confidence')} "
        f"· 回退 {settings.get('music_director_fallback')}\n"
        f"  导演用量: 今日 {usage.get('calls') or 0} 次 · 缓存 {cache.get('valid')}/{cache.get('entries')} 条有效"
    )


def _probe_text(result: dict[str, Any]) -> str:
    status = "成功" if result.get("ok") else "失败"
    models = result.get("models") or []
    lines = [
        f"平台探测{status}：{result.get('platform_id') or '-'} · 方式 {result.get('method') or '-'} "
        f"· 耗时 {result.get('elapsed_ms') or 0}ms",
    ]
    if models:
        lines.append(f"  可用模型: {', '.join(str(item) for item in models[:12])}")
    if result.get("error"):
        lines.append(f"  错误: {result.get('error')}")
    if result.get("note"):
        lines.append(f"  说明: {result.get('note')}")
    return "\n".join(lines)


def _handle_platform_command(
    rest: list[str],
    *,
    flags: set[str],
    positionals: list[str],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    config_dir: Any,
) -> tuple[bool, str]:
    if not positionals or positionals[0].lower() in {"list", "ls"}:
        return True, _platform_table_text(config_dir)
    action = positionals[0].lower()

    if action == "show" and len(positionals) > 1:
        platform = image_platforms.get_platform(config_dir, positionals[1], include_secret=False)
        if platform is None:
            return False, f"平台不存在: {positionals[1]}"
        return True, _platform_detail_text(platform)

    if action in {"use", "default"} and len(positionals) > 1:
        platform_id = positionals[1]

        def _apply_use() -> tuple[bool, str]:
            if not image_platforms.set_default_platform(config_dir, platform_id):
                return False, f"平台不存在: {platform_id}"
            return True, f"默认生图平台已设为 {platform_id}（同时把配图来源切到 platform）"

        return _confirm(
            preview=f"将把默认生图平台设为 {platform_id}。",
            apply=_apply_use,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认切换默认生图平台?",
        )

    if action in {"enable", "disable"} and len(positionals) > 1:
        platform_id = positionals[1]
        value = action == "enable"
        platform = image_platforms.get_platform(config_dir, platform_id, include_secret=False)
        if platform is None:
            return False, f"平台不存在: {platform_id}"

        def _apply_toggle() -> tuple[bool, str]:
            payload = {key: platform.get(key) for key in
                       ("id", "name", "protocol", "base_url", "model", "size", "timeout")}
            payload["enabled"] = value
            image_platforms.save_platform(config_dir, payload)
            return True, f"平台 {platform_id} 已{'启用' if value else '停用'}"

        return _confirm(
            preview=f"将{('启用' if value else '停用')}平台 {platform_id}。",
            apply=_apply_toggle,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认修改平台启用状态?",
        )

    if action in {"delete", "remove"} and len(positionals) > 1:
        platform_id = positionals[1]

        def _apply_delete() -> tuple[bool, str]:
            if not image_platforms.delete_platform(config_dir, platform_id):
                return False, f"平台不存在: {platform_id}"
            return True, f"平台 {platform_id} 已删除" + (
                "（它原是默认平台，配图来源已回落 procedural）"
                if load_immersive_settings(config_dir).get("illustration_provider") == "procedural" else ""
            )

        return _confirm(
            preview=f"将删除平台 {platform_id} 及其密钥（不可恢复）。",
            apply=_apply_delete,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认删除平台?",
        )

    if action == "key" and len(positionals) > 2:
        platform_id = positionals[1]
        key_action = positionals[2].lower()
        if key_action == "set" and len(positionals) > 3:
            key = " ".join(positionals[3:]).strip()
            if not key:
                return False, "Usage: immersive platform key <id> set <api_key> [--yes]"

            def _apply_key() -> tuple[bool, str]:
                if image_platforms.set_platform_secret(config_dir, platform_id, key) is None:
                    return False, f"平台不存在: {platform_id}"
                logger.info("immersive platform key updated id=%s length=%s", platform_id, len(key))
                return True, f"平台 {platform_id} 密钥已写入（日志已脱敏）"

            return _confirm(
                preview=f"将写入平台 {platform_id} 的密钥（不会回显）。",
                apply=_apply_key,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认写入密钥?",
            )
        if key_action in {"clear", "remove"}:
            def _clear_key() -> tuple[bool, str]:
                if image_platforms.set_platform_secret(config_dir, platform_id, "") is None:
                    return False, f"平台不存在: {platform_id}"
                return True, f"平台 {platform_id} 密钥已清除"

            return _confirm(
                preview=f"将清除平台 {platform_id} 的密钥。",
                apply=_clear_key,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认清除密钥?",
            )
    if action == "key":
        return False, "Usage: immersive platform key <id> set <api_key>|clear [--yes]"

    if action == "test" and len(positionals) > 1:
        platform_id = positionals[1]
        allow_generation = "--no-generate" not in flags

        def _apply_test() -> tuple[bool, str]:
            result = image_platforms.probe_platform(
                config_dir, platform_id, allow_generation=allow_generation
            )
            return True, _probe_text(result)

        return _confirm(
            preview=(
                f"将探测平台 {platform_id}：先列模型"
                + ("，失败时最小生图（可能产生费用）" if allow_generation else "（不生成图片）")
                + "。"
            ),
            apply=_apply_test,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认执行平台探测?",
        )
    if action == "test":
        return False, "Usage: immersive platform test <id> [--no-generate] [--yes]"

    if action == "migrate":
        def _apply_migrate() -> tuple[bool, str]:
            migrated = image_platforms.migrate_legacy(config_dir)
            if not migrated:
                return True, "无需迁移：平台表已存在，或旧配置缺少地址/密钥。"
            return True, f"旧单条配置已迁移为平台 {migrated}，并清空旧的地址与密钥字段。"

        return _confirm(
            preview="将把旧的单条生图配置（地址/模型/尺寸/密钥）迁移为一条平台记录，并清空旧密钥字段。",
            apply=_apply_migrate,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认迁移旧配置?",
        )

    return False, "Usage: immersive platform [list|show|use|enable|disable|delete|key|test|migrate] ..."


def _handle_director_command(
    rest: list[str],
    *,
    positionals: list[str],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    config_dir: Any,
    settings: dict[str, Any],
) -> tuple[bool, str]:
    if not positionals:
        return True, _director_summary(settings, config_dir)
    action = positionals[0].lower()

    if action in {"on", "off", "enable", "disable"}:
        value = action in {"on", "enable"}

        def _apply() -> tuple[bool, str]:
            _write({"music_director_enabled": value}, config_dir)
            return True, f"AI 音乐导演已{'开启' if value else '关闭'}（关闭后仅使用规则分析器）"

        return _confirm(
            preview=f"将把 AI 音乐导演开关设为『{'开' if value else '关'}』。",
            apply=_apply,
            assume_yes=assume_yes,
            confirm=confirm,
            prompt="确认修改导演开关?",
        )

    if action == "platform" and len(positionals) > 1:
        value = positionals[1].strip()
        return _set_value(
            "music_director_platform",
            "" if value.lower() == "clear" else value[:64],
            f"导演 AI 平台已设为 {value or '（默认平台）'}",
            config_dir, assume_yes, confirm,
            "平台 ID 来自「AI 平台配置」；留空表示取第一个启用的平台。",
        )
    if action == "model" and len(positionals) > 1:
        value = positionals[1].strip()
        return _set_value(
            "music_director_model",
            "" if value.lower() == "clear" else value[:120],
            f"导演模型已设为 {value or '（平台默认）'}",
            config_dir, assume_yes, confirm,
        )
    if action == "interval" and len(positionals) > 1:
        return _set_int("music_director_interval_turns", positionals[1], 1, 20, "导演触发间隔（回合）",
                        config_dir, assume_yes, confirm)
    if action == "budget" and len(positionals) > 1:
        return _set_int("music_director_daily_budget", positionals[1], 0, 100000,
                        "导演每日预算（0=不限）", config_dir, assume_yes, confirm)
    if action == "ttl" and len(positionals) > 1:
        return _set_int("music_director_cache_ttl", positionals[1], 0, 86400,
                        "乐谱缓存 TTL（秒，0=不复用）", config_dir, assume_yes, confirm)
    if action in {"min-confidence", "confidence"} and len(positionals) > 1:
        return _set_int("music_director_min_confidence", positionals[1], 0, 100,
                        "导演最低置信度", config_dir, assume_yes, confirm)
    if action == "timeout" and len(positionals) > 1:
        return _set_int("music_director_timeout", positionals[1], 20, 300,
                        "导演单次超时（秒）", config_dir, assume_yes, confirm)
    if action == "max-context" and len(positionals) > 1:
        return _set_int("music_director_max_context_chars", positionals[1], 200, 4000,
                        "导演上下文最大字符数", config_dir, assume_yes, confirm)
    if action == "fallback" and len(positionals) > 1:
        value = positionals[1].lower()
        if value not in DIRECTOR_FALLBACKS:
            return False, f"回退策略只能是 {list(DIRECTOR_FALLBACKS)}"
        return _set_value(
            "music_director_fallback", value, f"导演失败回退策略为 {value}",
            config_dir, assume_yes, confirm,
            "rule=回退规则分析器；hold=保持当前档位不切换。",
        )
    if action == "cache":
        if len(positionals) > 1 and positionals[1].lower() == "clear":
            def _clear() -> tuple[bool, str]:
                cleared = director.clear_cache()
                audit.record("director.cache_clear", actor="console", target="immersive:director_cache",
                             detail={"cleared": cleared})
                return True, f"已清理导演乐谱缓存 {cleared} 条"

            return _confirm(
                preview="将删除全部导演乐谱缓存（下次相同上下文会重新调用模型）。",
                apply=_clear,
                assume_yes=assume_yes,
                confirm=confirm,
                prompt="确认清理导演缓存?",
            )
        return True, _director_cache_text(settings)

    return False, "Usage: immersive director [on|off|platform|model|interval|budget|ttl|min-confidence|fallback|cache] ..."


def _platform_table_text(config_dir: Any) -> str:
    platforms = image_platforms.list_platforms(config_dir)
    if not platforms:
        return "当前没有生图平台。可用 `immersive platform migrate --yes` 迁移旧配置，或在设置页新增。"
    lines = ["生图平台表："]
    for item in platforms:
        lines.append(
            f"  {'*' if item.get('is_default') else ' '} {item.get('id')} · {item.get('name')} · "
            f"{item.get('protocol')} · {item.get('model')} · {item.get('size')} · "
            f"{'启用' if item.get('enabled', True) else '停用'} · "
            f"{'有密钥' if item.get('has_api_key') else '无密钥'} · {item.get('base_url') or '-'}"
        )
    return "\n".join(lines)


def _platform_detail_text(platform: dict[str, Any]) -> str:
    return (
        f"平台 {platform.get('id')}：\n"
        f"  名称: {platform.get('name')} · 协议: {platform.get('protocol')}\n"
        f"  Base URL: {platform.get('base_url') or '-'}\n"
        f"  模型: {platform.get('model')} · 尺寸: {platform.get('size')} · 超时: {platform.get('timeout')}s\n"
        f"  启用: {_bool_label(platform.get('enabled', True))} · 默认: {_bool_label(platform.get('is_default'))}\n"
        f"  密钥: {platform.get('api_key_masked') or '未配置'}"
    )


def _director_summary(settings: dict[str, Any], config_dir: Any) -> str:
    return "\n".join(
        [
            "AI 音乐导演：",
            f"  开关: {_bool_label(settings.get('music_director_enabled'))} · 平台/模型: "
            f"{settings.get('music_director_platform') or '（默认）'}/{settings.get('music_director_model') or '（平台默认）'}",
            f"  触发: 每 {settings.get('music_director_interval_turns')} 回合，或档位变化时立即触发",
            f"  预算: {settings.get('music_director_daily_budget') or '不限'} 次/日 · 超时 {settings.get('music_director_timeout')}s "
            f"· 上下文 {settings.get('music_director_max_context_chars')} 字符",
            f"  校验: 最低置信 {settings.get('music_director_min_confidence')} · 回退 {settings.get('music_director_fallback')} · "
            f"缓存 TTL {settings.get('music_director_cache_ttl')}s",
            "  " + _director_cache_text(settings).replace("\n", "\n  "),
        ]
    )


def _director_cache_text(settings: dict[str, Any]) -> str:
    usage = director.usage_snapshot()
    cache = director.cache_stats(ttl=int(settings.get("music_director_cache_ttl") or 0))
    return (
        f"导演用量: 今日 {usage.get('calls') or 0} 次\n"
        f"乐谱缓存: {cache.get('entries')} 条（TTL 内有效 {cache.get('valid')} 条，TTL {cache.get('ttl')}s）"
    )


def _music_summary(settings: dict[str, Any]) -> str:
    return (
        f"站点配乐: {_bool_label(settings.get('music_enabled'))} · 自动 {_bool_label(settings.get('music_auto_switch'))} · "
        f"默认 {settings.get('music_default_profile')}/{settings.get('music_default_intensity')} · "
        f"音量 {settings.get('music_volume')}% · 交叉淡入 {settings.get('music_crossfade_ms')}ms · "
        f"滞回 {settings.get('music_hysteresis_turns')} 回合 · 种子 {settings.get('music_seed') or '（自动）'}\n"
        f"可选档位: {', '.join(PROFILES)}\n可选强度: {', '.join(INTENSITIES)}"
    )


def _illustration_summary(settings: dict[str, Any], config_dir: Any = None) -> str:
    platform = image_platforms.resolve_platform(config_dir, settings)
    platform_text = (
        f"{platform.get('id')} / {platform.get('model')} / {platform.get('size')} / "
        f"{'有密钥' if platform.get('has_api_key') else '无密钥'}"
        if platform else "（未配置，任何外部请求都会回落到程序化图）"
    )
    return (
        f"站点配图: {_bool_label(settings.get('illustration_enabled'))} · 自动 {_bool_label(settings.get('illustration_auto'))} · "
        f"服务 {settings.get('illustration_provider')}\n"
        f"默认平台: {platform_text}\n"
        f"超时 {settings.get('illustration_timeout')}s · 并发 {settings.get('illustration_concurrency')} · "
        f"每日预算 {settings.get('illustration_daily_budget') or '不限'} · 房间每小时 {settings.get('illustration_room_hourly_cap') or '不限'}\n"
        f"剧本缓存: 记录新图 {_bool_label(settings.get('illustration_scenario_record'))} · "
        f"优先复用 {_bool_label(settings.get('illustration_scenario_prefer'))} · "
        f"单剧本上限 {settings.get('illustration_scenario_quota_mb') if settings.get('illustration_scenario_quota_mb') else '不限'}MB · "
        f"单图上限 {settings.get('illustration_scenario_max_image_mb')}MB · 超限 {settings.get('illustration_scenario_overflow')}"
    )


def _handle_scenario_cache_command(
    positionals: list[str],
    *,
    settings: dict[str, Any],
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
    config_dir: Any,
) -> tuple[bool, str]:
    """站点级剧本配图缓存：配置、统计、清理与 LRU 收缩。"""
    if not positionals:
        return True, _scenario_summary(settings)
    action = positionals[0].lower()

    if action in {"on", "off", "enable", "disable", "record"} and (
        action in {"on", "off", "enable", "disable"} or len(positionals) > 1
    ):
        value = _as_bool_arg(positionals[1]) if len(positionals) > 1 else action in {"on", "enable"}
        if value is None:
            return False, "Usage: immersive scenario record <on|off> [--yes]"

        def _apply_record() -> tuple[bool, str]:
            _write({"illustration_scenario_record": value}, config_dir)
            audit.record("scenario_cache.record", actor="console", target="immersive:scenario_cache",
                         detail={"enabled": value})
            return True, f"新生成配图{'会' if value else '不会'}写入剧本目录"

        return _confirm(
            preview=f"将把「记录新图到剧本」设为『{'开' if value else '关'}』。",
            apply=_apply_record, assume_yes=assume_yes, confirm=confirm,
            prompt="确认修改剧本配图记录开关?",
        )

    if action == "prefer" and len(positionals) > 1:
        value = _as_bool_arg(positionals[1])
        if value is None:
            return False, "Usage: immersive scenario prefer <on|off> [--yes]"

        def _apply_prefer() -> tuple[bool, str]:
            _write({"illustration_scenario_prefer": value}, config_dir)
            audit.record("scenario_cache.prefer", actor="console", target="immersive:scenario_cache",
                         detail={"enabled": value})
            return True, f"游玩时{'会' if value else '不会'}优先复用剧本配图"

        return _confirm(
            preview=f"将把「优先复用剧本配图」设为『{'开' if value else '关'}』。",
            apply=_apply_prefer, assume_yes=assume_yes, confirm=confirm,
            prompt="确认修改剧本配图复用开关?",
        )

    if action == "quota" and len(positionals) > 1:
        return _set_int("illustration_scenario_quota_mb", positionals[1], 0, 102400,
                        "单剧本容量上限（MB，0=不限）", config_dir, assume_yes, confirm)
    if action in {"max-image", "max_image"} and len(positionals) > 1:
        return _set_int("illustration_scenario_max_image_mb", positionals[1], 1, 64,
                        "单图大小上限（MB）", config_dir, assume_yes, confirm)
    if action == "overflow" and len(positionals) > 1:
        value = positionals[1].lower()
        if value not in {"lru", "reject"}:
            return False, "Usage: immersive scenario overflow <lru|reject> [--yes]"
        return _set_value(
            "illustration_scenario_overflow", value, f"剧本缓存超限策略为 {value}",
            config_dir, assume_yes, confirm,
            "lru=驱逐最旧配图后写入；reject=拒绝新图并保留原图。",
        )

    if action == "stats":
        target = positionals[1] if len(positionals) > 1 else ""
        return True, _scenario_stats_text(target)

    if action in {"clear", "prune"}:
        target = positionals[1] if len(positionals) > 1 else ""
        target_text = target or "全部剧本"
        if action == "clear":
            def _apply_clear() -> tuple[bool, str]:
                files, size = scenario_cache.clear(target, scenarios_dir=SCENARIOS_DIR)
                audit.record("scenario_cache.clear", actor="console", target=target or "all",
                             detail={"files": files, "bytes": size})
                return True, f"已清理 {target_text} 的剧本配图缓存：{files} 个文件 / {_human_bytes(size)}"

            return _confirm(
                preview=f"将删除 {target_text} 的剧本配图缓存（图片与索引；不可恢复，相关房间历史链接可能失效）。",
                apply=_apply_clear, assume_yes=assume_yes, confirm=confirm,
                prompt="确认清理剧本配图缓存?",
            )

        def _apply_prune() -> tuple[bool, str]:
            files, size = scenario_cache.prune(
                scenario_id=target, settings=settings, scenarios_dir=SCENARIOS_DIR
            )
            audit.record("scenario_cache.prune", actor="console", target=target or "all",
                         detail={"files": files, "bytes": size})
            return True, f"已按 {settings.get('illustration_scenario_quota_mb')}MB 配额清理 {target_text}：{files} 个文件 / {_human_bytes(size)}"

        return _confirm(
            preview=f"将按当前配额对 {target_text} 做一次 LRU 清理（删除最旧配图，默认预演）。",
            apply=_apply_prune, assume_yes=assume_yes, confirm=confirm,
            prompt="确认执行剧本缓存 LRU 清理?",
        )

    return False, "Usage: immersive scenario [record|prefer|quota|max-image|overflow|stats|clear|prune] ..."


def _scenario_summary(settings: dict[str, Any]) -> str:
    data = scenario_cache.stats(scenarios_dir=SCENARIOS_DIR)
    return (
        f"剧本配图缓存：记录新图 {_bool_label(settings.get('illustration_scenario_record'))} · "
        f"优先复用 {_bool_label(settings.get('illustration_scenario_prefer'))}\n"
        f"  单剧本上限 {settings.get('illustration_scenario_quota_mb') if settings.get('illustration_scenario_quota_mb') else '不限'}MB · "
        f"单图上限 {settings.get('illustration_scenario_max_image_mb')}MB · 超限策略 {settings.get('illustration_scenario_overflow')}\n"
        f"  当前缓存 {data.get('total_files') or 0} 个文件 / {_human_bytes(int(data.get('total_bytes') or 0))}\n"
        "  旧数据无场景指纹时不参与命中（不回填、不猜测），只影响新生成的图片。"
    )


def _scenario_stats_text(scenario_id: str) -> str:
    data = scenario_cache.stats(scenario_id, scenarios_dir=SCENARIOS_DIR)
    rows = data.get("scenarios") or []
    if not rows:
        return f"剧本 {scenario_id or '（全部）'} 暂无配图缓存。"
    lines = [f"剧本配图缓存（{scenario_id or '全部剧本'}）："]
    for row in rows:
        lines.append(
            f"  {row.get('scenario_id')} · {row.get('files')} 个文件 / {_human_bytes(int(row.get('bytes') or 0))} "
            f"· 索引内 {row.get('tracked_files')} / 未登记 {row.get('untracked_files')}\n"
            f"    目录: {row.get('directory')}"
        )
    lines.append(
        f"合计: {data.get('total_files') or 0} 个文件 / {_human_bytes(int(data.get('total_bytes') or 0))}"
    )
    return "\n".join(lines)


def _human_bytes(value: int) -> str:
    number = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    return f"{number:.1f}GB"


def _music_usage() -> str:
    return "Usage: immersive music [on|off|auto|volume|crossfade|hysteresis|seed] ...\n" + immersive_help_text()


def _illustration_usage() -> str:
    return "Usage: immersive illustration [on|off|auto|provider|timeout|concurrency|budget|cap|min-turns] ...\n" + immersive_help_text()


def _provider_problems(settings: dict[str, Any], config_dir: Any = None) -> list[str]:
    """外部生图前的配置检查：只认平台表，旧单条字段不再参与判定。"""
    if settings.get("illustration_provider") not in {"platform", "openai_compatible"}:
        return []
    platform = image_platforms.resolve_platform(config_dir, settings)
    if platform is None:
        return ["未配置可用的生图平台（`immersive platform` 查看，或到设置页创建）"]
    problems: list[str] = []
    if not platform.get("base_url"):
        problems.append(f"平台 {platform.get('id')} 未配置 Base URL")
    if not platform.get("has_api_key"):
        problems.append(f"平台 {platform.get('id')} 未配置 API Key（`immersive platform key ...`）")
    if not platform.get("model"):
        problems.append(f"平台 {platform.get('id')} 未配置模型名")
    return problems


def _safe_room_dir(rooms_dir: Any, room_id: str) -> Path | None:
    text = str(room_id or "").strip()
    if not text or text in {".", ".."} or any(sep in text for sep in ("/", "\\", "..")):
        return None
    base = Path(rooms_dir) if rooms_dir else Path(ROOMS_DIR)
    target = (base / text).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target if target.is_dir() else None


def _room_state_text(room_dir: Path, room_id: str) -> str:
    music: dict[str, Any] = {}
    illustration: dict[str, Any] = {}
    for name, target in (("immersive_music.json", music), ("immersive_illustration.json", illustration)):
        path = room_dir / name
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                target.update(data)
    lines = [
        f"房间 {room_id} 沉浸状态：",
        f"  配乐: {music.get('profile') or '-'} / {music.get('intensity') or '-'} · 第 {music.get('section') or '-'} 段 · "
        f"回合 {music.get('turns') or 0} · 原因 {music.get('last_reason') or '-'}",
        f"  配图: 最近回合 {illustration.get('last_turn') if illustration.get('last_turn') is not None else '-'} · "
        f"历史 {len(illustration.get('history') or [])} 条",
    ]
    history = illustration.get("history") if isinstance(illustration.get("history"), list) else []
    for item in history[-4:]:
        if isinstance(item, dict):
            lines.append(
                f"    {item.get('created_at') or '-'} · {item.get('scene') or '-'} · "
                f"{item.get('provider') or '-'} · degraded={item.get('degraded')}"
            )
    return "\n".join(lines)
