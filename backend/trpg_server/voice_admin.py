"""站点控制台 ``voice`` 命令。

本模块不依赖 Flask，配置目录/房间目录由调用方注入；配置写入走 tomlkit 往返，
保留 ``general.toml`` 注释。所有会改动状态的操作都支持预演 / ``--yes``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from trpg_server.config import update_sections
from trpg_server.config.paths import general_config_path, resolve_config_dir
from trpg_server.settings import ROOMS_DIR
from trpg_server.voice import events, local_cache, local_models, local_service
from trpg_server.voice.config import (
    INTERRUPT_POLICIES,
    VOICE_ENGINES,
    load_room_voice,
    load_voice_settings,
    room_usage_snapshot,
    save_room_voice,
)
from trpg_server.voice.engine import engine_health
from trpg_server.voice.profiles import PROFILE_MAP, PROFILE_IDS, PROFILES

YES_FLAGS = {"--yes", "-y"}
_ON = {"on", "enable", "enabled", "true", "1", "开", "开启"}
_OFF = {"off", "disable", "disabled", "false", "0", "关", "关闭"}


def _bool_arg(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    return None


def _write(values: dict[str, Any], config_dir: Any) -> None:
    update_sections(general_config_path(resolve_config_dir(config_dir)), {"immersive": values})


def _mask_key(value: str) -> str:
    text = str(value or "")
    if not text:
        return "未配置"
    if len(text) <= 8:
        return "已配置（••••）"
    return f"已配置（{text[:4]}••••{text[-4:]}）"


def _redacted(settings: dict[str, Any]) -> dict[str, Any]:
    view = dict(settings)
    view["engine_health"] = engine_health(settings)
    return view


def _room_dir(rooms_dir: Any, room_id: str) -> Path | None:
    text = str(room_id or "").strip()
    if not text:
        return None
    base = Path(rooms_dir) if rooms_dir else Path(ROOMS_DIR)
    candidate = base / text
    return candidate if candidate.is_dir() else None


def _confirm_or_preview(
    preview: str,
    apply: Callable[[], tuple[bool, str]],
    *,
    assume_yes: bool,
    confirm: Callable[[str], bool] | None,
) -> tuple[bool, str]:
    if assume_yes:
        return apply()
    if confirm is not None and confirm(preview):
        return apply()
    return True, f"[预演] {preview}\n确认执行请加 --yes（或在交互控制台输入 y）。"


def _usage_summary(limit: int = 50) -> str:
    recent = events.recent(limit)
    if not recent:
        return "语音事件日志暂无记录。\n日志位置: " + str(events.path())
    counts: dict[str, int] = {}
    rooms: dict[str, int] = {}
    for item in recent:
        name = str(item.get("event") or "")
        counts[name] = counts.get(name, 0) + 1
        room_id = str(item.get("room_id") or "-")
        rooms[room_id] = rooms.get(room_id, 0) + 1
    lines = [f"最近 {len(recent)} 条语音事件："]
    for name, count in sorted(counts.items()):
        lines.append(f"  {name}: {count}")
    lines.append("按房间：")
    for room_id, count in sorted(rooms.items(), key=lambda pair: -pair[1]):
        lines.append(f"  {room_id}: {count}")
    lines.append("日志位置: " + str(events.path()))
    return "\n".join(lines)


def voice_help_text() -> str:
    return "\n".join(
        [
            "语音配音控制台命令：",
            "  voice status [--json]                          （站点开关、引擎、配额与引擎健康）",
            "  voice on|off                                   （站点总开关；默认关闭）",
            "  voice engine <system|local|cloud>              （默认系统音色；local 需先安装模型）",
            "  voice engine allowed <system,local,cloud>       （引擎白名单，逗号分隔）",
            "  voice profile list                             （人设、真实说话人绑定与来源）",
            "  voice map list [--room <room_id>]              （查看房间 NPC 音色映射）",
            "  voice map set <speaker> <profile> --room <room_id> [--yes]",
            "  voice local status [--json]                    （模型安装状态与子服务健康）",
            "  voice local install [--yes] [--force]          （下载并校验模型；默认预演）",
            "  voice local remove [--yes]                      （移除本地模型；默认预演）",
            "  voice local model <模型ID> | base-url <URL> | concurrency <1-8> | timeout <5-180> [--yes]",
            "  voice cache stats [--room <room_id>]            （本房间缓存条数/字节/命中）",
            "  voice cache clear [--room <room_id>] [--yes]    （清空本房间缓存；默认预演）",
            "  voice cache prune [--room <room_id>] [--yes]    （按额度裁剪本房间缓存；默认预演）",
            "  voice test [--json]                            （引擎健康检查，不发起外部请求）",
            "  voice usage [--room <room_id>]                 （最近事件与房间小时用量）",
            "  说明：修改类命令缺省预演，加 --yes 才落盘；配置写入保留 general.toml 注释。",
        ]
    )


def handle_voice_command(
    args: list[str],
    *,
    confirm: Callable[[str], bool] | None = None,
    rooms_dir: Any = None,
    config_dir: Any = None,
) -> tuple[bool, str]:
    if not args:
        return True, voice_help_text()
    sub = args[0].lower()
    rest = args[1:]
    flags = {item.lower() for item in rest if item.startswith("-")}
    positionals = [item for item in rest if not item.startswith("-")]
    assume_yes = bool(flags & YES_FLAGS)
    room_flag = ""
    for index, item in enumerate(rest):
        if item.lower() in {"--room", "-r"} and index + 1 < len(rest):
            room_flag = rest[index + 1]
    settings = load_voice_settings(config_dir)

    if sub in {"help", "?"}:
        return True, voice_help_text()

    if sub == "status":
        if positionals and positionals[0].lower() == "json":
            return True, json.dumps(_redacted(settings), ensure_ascii=False, indent=2)
        health = engine_health(settings)
        lines = [
            "语音配音状态：",
            f"  总开关: {'开' if settings['enabled'] else '关'}",
            f"  引擎: {settings['engine']} · 允许: {', '.join(settings['engine_allowed']) or '-'} "
            f"· 健康: {'可用' if health.get('ok') else '不可用（' + str(health.get('reason')) + '）'}",
            f"  自动播放: {'开' if settings['autoplay'] else '关'} · 音量 {settings['volume']}% · 语速 {settings['rate']}%",
            f"  单回合 {settings['max_turn_chars']} 字 / 单段 {settings['max_segment_chars']} 字 / 最多 {settings['max_segments']} 段",
            f"  冲突策略: {settings['interrupt_policy']} · ducking {'开' if settings['ducking'] else '关'} "
            f"({settings['ducking_level']})",
            f"  房间小时上限: {settings['room_hourly_cap'] if settings['room_hourly_cap'] else '不限'}",
            f"  个人覆盖: {'允许' if settings['allow_personal'] else '禁止'}",
            f"  云端占位: {settings['cloud_base_url'] or '未配置'} · {settings['cloud_model'] or '未配置'} "
            f"· 凭据复用「模型设置」中的 AI 平台",
        ]
        return True, "\n".join(lines)

    if sub in {"on", "off"}:
        target = sub == "on"
        _write({"voice_enabled": target}, config_dir)
        note = "" if target else "\n已关闭站点语音；房间与个人设置无法越过该开关。"
        return True, f"站点语音已{'开启' if target else '关闭'}。{note}"

    if sub == "engine":
        if not positionals:
            return False, "Usage: voice engine <system|local|cloud> | voice engine allowed <a,b,c>"
        action = positionals[0].lower()
        if action == "allowed":
            raw = positionals[1] if len(positionals) > 1 else ""
            items = [item.strip().lower() for item in raw.split(",") if item.strip()]
            invalid = [item for item in items if item not in VOICE_ENGINES]
            if invalid:
                return False, f"未知引擎: {', '.join(invalid)}；可选: {', '.join(VOICE_ENGINES)}"
            if not items:
                return False, "允许列表不能为空。"
            _write({"voice_engine_allowed": ",".join(dict.fromkeys(items))}, config_dir)
            return True, f"引擎白名单已设为: {', '.join(dict.fromkeys(items))}"
        target = action
        if target not in VOICE_ENGINES:
            return False, f"Usage: voice engine <system|local|cloud>；未知引擎 {target}"
        _write({"voice_engine": target}, config_dir)
        if target == "local":
            status = local_models.installed_status(settings.get("local_model") or local_models.MODEL_ID)
            if not status["installed"]:
                return True, (
                    "引擎已设为 local，但本地模型尚未安装。\n"
                    "请执行 `voice local install --yes` 下载权重，然后 `voice test` 检查子服务。"
                )
        if target != "system":
            return True, f"引擎已设为 {target}。"
        return True, "引擎已设为 system（浏览器 Web Speech，无需外部服务）。"

    if sub == "local":
        action = positionals[0].lower() if positionals else "status"
        if action == "status":
            data = local_service.service_status(settings)
            if positionals[1:2] and positionals[1].lower() == "json":
                return True, json.dumps(data, ensure_ascii=False, indent=2)
            model = data["model"]
            health = data["health"]
            lines = [
                "本地语音引擎：",
                f"  状态: {'开启' if settings['local_enabled'] else '关闭'}"
                f" · 引擎 {'可用' if health.get('ok') else '不可用（' + str(health.get('reason')) + '）'}",
                f"  模型: {model['model']} · {model['label']} · {model['license']}",
                f"  安装: {'已安装' if model['installed'] else '未安装'} · {model['bytes'] / 1024 / 1024:.1f} MB"
                f" · {model['path']}",
                f"  服务: {data['base_url'] or '未启动'}"
                f" · {'托管' if data['managed'] else '外部回环'}"
                f" · 并发 {data['concurrency']} · 进行中 {data['active']}",
                f"  缓存: {'开' if data['cache']['enabled'] else '关'}"
                f" · {data['cache']['quota_mb']} MB / {data['cache']['max_entries']} 条"
                f" · {data['cache']['overflow']}",
            ]
            if model["missing"]:
                lines.append(f"  缺失文件: {', '.join(model['missing'][:6])}")
            return True, "\n".join(lines)
        if action == "install":
            status = local_models.installed_status(settings.get("local_model") or local_models.MODEL_ID)
            if status["installed"] and not ({"--force", "-f"} & flags):
                return True, f"模型已安装：{status['path']}（如需重装请加 --force）"
            preview = (
                f"将从 HuggingFace 镜像下载 {local_models.MODEL_LABEL}"
                f"（约 175 MB，{local_models.MODEL_LICENSE}）到 {status['path']}。"
            )
            if not assume_yes and not (confirm is not None and confirm(preview)):
                return True, f"[预演] {preview}\n确认执行请加 --yes。"

            def _install() -> tuple[bool, str]:
                ok, message = local_models.install(
                    progress=lambda line: None,
                    force=bool({"--force", "-f"} & flags),
                )
                events.emit(
                    "model_install", engine="local", model=local_models.MODEL_ID,
                    degraded=not ok, reason=message,
                )
                return ok, message

            return _confirm_or_preview(preview, _install, assume_yes=True, confirm=None)
        if action == "remove":
            status = local_models.installed_status(settings.get("local_model") or local_models.MODEL_ID)
            if not status["installed"]:
                return True, f"模型未安装：{status['path']}"
            preview = f"将删除本地模型目录 {status['path']}（缓存不受影响）。"
            if not assume_yes:
                return True, f"[预演] {preview}\n确认执行请加 --yes。"

            def _remove() -> tuple[bool, str]:
                ok, message = local_models.remove()
                local_service.stop_service()
                events.emit(
                    "model_remove", engine="local", model=local_models.MODEL_ID,
                    degraded=not ok, reason=message,
                )
                return ok, message

            return _remove()
        if action in {"model", "base-url", "concurrency", "timeout"}:
            keys = {
                "model": ("voice_local_model", "local_model"),
                "base-url": ("voice_local_base_url", "local_base_url"),
                "concurrency": ("voice_local_concurrency", "local_concurrency"),
                "timeout": ("voice_local_timeout", "local_timeout"),
            }
            config_key, view_key = keys[action]
            if len(positionals) < 2:
                return True, f"{action} 当前值: {settings.get(view_key) or '(空)'}"
            value: Any = positionals[1]
            if action == "concurrency":
                try:
                    value = int(positionals[1])
                except ValueError:
                    return False, "并发必须是 1-8 的整数。"
                if not 1 <= value <= 8:
                    return False, "并发必须是 1-8 的整数。"
            elif action == "timeout":
                try:
                    value = int(positionals[1])
                except ValueError:
                    return False, "超时必须是 5-180 秒的整数。"
                if not 5 <= value <= 180:
                    return False, "超时必须是 5-180 秒的整数。"
            preview = f"将把 {action} 从 {settings.get(view_key) or '(空)'} 改为 {value}。"

            def _apply() -> tuple[bool, str]:
                _write({config_key: value}, config_dir)
                return True, f"已更新 {action} = {value}。"

            return _confirm_or_preview(preview, _apply, assume_yes=assume_yes, confirm=confirm)
        return False, "Usage: voice local status|install|remove|model|base-url|concurrency|timeout"

    if sub == "cache":
        action = positionals[0].lower() if positionals else "stats"
        room_dir = _room_dir(rooms_dir, room_flag)
        if not room_flag or room_dir is None:
            return False, "Usage: voice cache stats|clear|prune --room <room_id>"
        if action == "stats":
            data = local_cache.stats(room_dir)
            limits = {
                "quota_mb": settings.get("cache_quota_mb"),
                "max_entries": settings.get("cache_max_entries"),
                "overflow": settings.get("cache_overflow"),
            }
            return True, (
                f"房间 {room_flag} 语音缓存：\n"
                f"  条目 {data['entries']} · 文件 {data['files']}"
                f" · {data['bytes'] / 1024 / 1024:.2f} MB · 累计命中 {data['hits']}\n"
                f"  额度 {limits['quota_mb']} MB / {limits['max_entries']} 条 · {limits['overflow']}\n"
                f"  目录 {data['directory'] or '(尚未创建)'}"
            )
        if action == "clear":
            preview = f"将清空房间 {room_flag} 的语音缓存文件（不影响模型与剧本）。"
            if not assume_yes:
                return True, f"[预演] {preview}\n确认执行请加 --yes。"
            files, size = local_cache.clear(room_dir)

            def _log() -> None:
                events.emit("cache_clear", room_id=room_flag, engine="local", reason=f"{files} files")

            _log()
            return True, f"已清空 {files} 个缓存文件（{size / 1024 / 1024:.2f} MB）。"
        if action == "prune":
            preview = f"将按当前额度裁剪房间 {room_flag} 的语音缓存（LRU）。"
            if not assume_yes:
                return True, f"[预演] {preview}\n确认执行请加 --yes。"
            removed, freed = local_cache.prune(room_dir, settings)
            events.emit("cache_evict", room_id=room_flag, engine="local", reason=f"{removed} files")
            return True, f"已裁剪 {removed} 个文件，释放 {freed / 1024 / 1024:.2f} MB。"
        return False, "Usage: voice cache stats|clear|prune --room <room_id>"

    if sub == "profile":
        action = positionals[0].lower() if positionals else "list"
        if action != "list":
            return False, "Usage: voice profile list"
        lines = ["内置音色人设："]
        for profile in PROFILES:
            binding = (
                local_service.speaker_binding(profile.id, settings)
                if settings.get("local_enabled")
                else None
            )
            suffix = ""
            if binding:
                suffix = (
                    f" speaker={binding['voice_id']}(sid={binding['sid']},{binding['gender']},"
                    f"{binding['source']})"
                )
            lines.append(
                f"  {profile.id:20s} {profile.label:8s} kind={profile.kind:8s} "
                f"pitch={profile.pitch:.2f} rate={profile.rate:.2f} "
                f"偏好={', '.join(profile.preferred_voices[:4])}{suffix}"
            )
        return True, "\n".join(lines)

    if sub == "map":
        action = positionals[0].lower() if positionals else "list"
        if action == "list":
            room_dir = _room_dir(rooms_dir, room_flag)
            if room_dir is None:
                return False, "Usage: voice map list --room <room_id>"
            data = load_room_voice(room_dir)
            lines = [f"房间 {room_flag} NPC 音色映射（{len(data['npc_profiles'])} 条）："]
            if not data["npc_profiles"]:
                lines.append("  （未设置显式映射；当前按剧本/名字哈希稳定分配）")
            for name, profile_id in sorted(data["npc_profiles"].items()):
                label = PROFILE_MAP.get(profile_id).label if profile_id in PROFILE_MAP else "未知"
                lines.append(f"  {name} -> {profile_id}（{label}）")
            lines.append(f"  静音名单: {', '.join(data['muted']) or '无'}")
            return True, "\n".join(lines)
        if action == "set":
            if len(positionals) < 3:
                return False, "Usage: voice map set <speaker> <profile> --room <room_id> [--yes]"
            speaker = positionals[1]
            profile_id = positionals[2].lower()
            room_dir = _room_dir(rooms_dir, room_flag)
            if room_dir is None:
                return False, "Usage: voice map set <speaker> <profile> --room <room_id> [--yes]"
            if profile_id not in PROFILE_MAP or PROFILE_MAP[profile_id].kind != "npc":
                return False, f"未知 NPC 人设: {profile_id}；可选: {', '.join(PROFILE_IDS[1:])}"

            def _apply() -> tuple[bool, str]:
                data = load_room_voice(room_dir)
                data["npc_profiles"][speaker] = profile_id
                if not save_room_voice(room_dir, data):
                    return False, "写入房间 voice.json 失败。"
                return True, f"已将房间 {room_flag} 的「{speaker}」映射到 {profile_id}。"

            return _confirm_or_preview(
                f"将把房间 {room_flag} 的「{speaker}」映射到 {profile_id}。",
                _apply,
                assume_yes=assume_yes,
                confirm=confirm,
            )
        return False, "Usage: voice map list|set ..."

    if sub == "test":
        health = engine_health(settings)
        payload = {"engine": settings["engine"], "health": health, "profiles": len(PROFILES)}
        if positionals and positionals[0].lower() == "json":
            return True, json.dumps(payload, ensure_ascii=False, indent=2)
        return True, (
            f"语音引擎检查：{settings['engine']} · {'可用' if health.get('ok') else '不可用'}\n"
            f"  原因: {health.get('reason') or '-'}\n"
            f"  人设: {len(PROFILES)} 个（8 NPC + 1 旁白）\n"
            "  本阶段 system 引擎在浏览器端发声，服务端 test 不生成音频。"
        )

    if sub == "usage":
        lines = [_usage_summary()]
        room_dir = _room_dir(rooms_dir, room_flag)
        if room_flag and room_dir is None:
            return False, f"房间 {room_flag} 不存在。"
        if room_dir is not None:
            data = load_room_voice(room_dir)
            usage = room_usage_snapshot(data)
            cap = settings["room_hourly_cap"]
            lines.append(
                f"\n房间 {room_flag} 本小时用量: {usage['count']} 回合"
                f"（{usage['hour'] or '-'}，上限 {cap if cap else '不限'}）"
            )
        return True, "\n".join(lines)

    return False, voice_help_text()
