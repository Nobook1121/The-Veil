import logging
import json
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from trpg_server.settings import CONFIG_DIR

SENSITIVE_KEYS = {
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "cookie",
    "set-cookie",
    "auth_token",
    "accesstoken",
    "access_token",
    "refresh_token",
    "session_token",
    "csrf_token",
    "token",
    "password",
    "secret",
    "secret_key",
    "private_key",
    "client_secret",
}
MAX_LOG_VALUE_LENGTH = 200
DEFAULT_LOG_LANGUAGE = "en-US"
LEVEL_LABELS = {
    logging.WARNING: "WARN",
    logging.CRITICAL: "FATAL",
}

# 日志文案的语言由 data/config/general.toml 的 [language] language 决定。
# 中文模式下直接使用调用方传入的中文键名/中文动作，英文模式下查表翻译，
# 缺失的条目回落到原文，保证新增日志不会因为缺少译文而丢失信息。
DETAIL_LABELS_EN = {
    "用户ID": "user ID",
    "操作者ID": "operator ID",
    "目标用户ID": "target user ID",
    "目标用户名": "target username",
    "目标用户": "target user",
    "目标玩家": "target player",
    "昵称": "nickname",
    "邮箱": "email",
    "更换头像": "avatar changed",
    "原因": "reason",
    "类型": "type",
    "数值": "value",
    "房间ID": "room ID",
    "房间码": "room code",
    "房间名": "room name",
    "记录ID": "record ID",
    "触发器ID": "trigger ID",
    "角色卡ID": "character card ID",
    "角色名": "character name",
    "剧本": "scenario",
    "剧本ID": "scenario ID",
    "剧本标题": "scenario title",
    "标题": "title",
    "文件": "file",
    "文件名": "filename",
    "文件数": "file count",
    "原文件": "original filename",
    "新文件": "new filename",
    "模块数": "module count",
    "资源数": "resource count",
    "模块类型": "module type",
    "模块标题": "module title",
    "消息数": "message count",
    "内容": "content",
    "内容长度": "content length",
    "回复长度": "reply length",
    "Token数": "token count",
    "输入Token": "prompt tokens",
    "输出Token": "completion tokens",
    "缓存Token": "cached tokens",
    "缓存命中率": "cache hit rate",
    "耗时毫秒": "elapsed ms",
    "转换版本": "conversion version",
    "平台": "platform",
    "平台数": "platform count",
    "模型": "model",
    "端口": "port",
    "局域网发现": "LAN discovery",
    "启用": "enabled",
    "状态": "status",
    "角色": "role",
    "权限": "permissions",
    "操作": "action",
    "IP": "IP",
}

ACTION_LABELS_EN = {
    "进行了操作": "performed an action",
    "注册了账号": "registered an account",
    "登录失败": "failed to log in",
    "登录成功": "logged in successfully",
    "退出登录": "logged out",
    "模拟登录了用户": "impersonated a user",
    "退出了模拟登录": "exited impersonation",
    "更改了个人资料": "updated profile",
    "更改了登录密码": "changed the login password",
    "更改了用户角色": "changed a user role",
    "更改了在线状态": "changed online status",
    "更改了用户状态": "changed user status",
    "更改了认证设置": "changed authentication settings",
    "更改了 IP 配置": "changed the IP configuration",
    "发布了角色卡到广场": "published a character card to the gallery",
    "更新了角色卡广场内容": "updated a character card gallery entry",
    "删除了角色卡广场内容": "deleted a character card gallery entry",
    "保存了角色卡": "saved a character card",
    "删除了角色卡": "deleted a character card",
    "生成了剧本模块摘要": "generated a scenario module summary",
    "导入并解析剧本": "imported and parsed a scenario",
    "创建了剧本": "created a scenario",
    "更新了剧本": "updated a scenario",
    "删除了剧本": "deleted a scenario",
    "上传了剧本封面": "uploaded a scenario cover",
    "删除了剧本封面": "deleted a scenario cover",
    "重命名了剧本封面": "renamed a scenario cover",
    "创建了房间": "created a room",
    "加入了房间": "joined a room",
    "离开了房间": "left a room",
    "绑定了房间角色卡": "bound a room character card",
    "移除了房间成员": "removed a room member",
    "更改了房间成员权限": "changed room member permissions",
    "删除了房间": "deleted a room",
    "归档了房间": "archived a room",
    "触发了场景触发器": "triggered a scene trigger",
    "记录了角色状态变化": "recorded a character state change",
    "删除了角色状态记录": "deleted a character state record",
    "创建了房间回档节点": "created a room save point",
    "恢复了房间回档节点": "restored a room save point",
    "删除了房间回档节点": "deleted a room save point",
    "保存了房间自动存档": "saved a room autosave",
    "更改了网络设置": "changed network settings",
    "更改了穿透设置": "changed penetration settings",
    "更改了 AI 平台设置": "changed AI platform settings",
    "删除了 AI 平台设置": "deleted AI platform settings",
    "查看了 AI 平台列表": "viewed the AI platform list",
    "测试了 AI 平台连接": "tested the AI platform connection",
    "更改了 AI 模型请求设置": "changed AI model request settings",
    "更改了系统提示词": "changed the system prompt",
    "更新了 AI 调试提示词": "updated the AI debug prompt",
    "更改了角色配置": "changed character configuration",
    "删除了 AI 模型请求设置": "deleted AI model request settings",
    "确认 AI 模型请求设置已不存在": "confirmed AI model request settings no longer exist",
    "更新了权限配置": "updated the permission configuration",
    "发送了房间消息": "sent a room message",
    "开始 AI 对话": "started an AI chat",
    "收到 AI 回复": "received an AI reply",
    "发送了首页消息": "sent a home message",
    "发送了剧本消息": "sent a scenario message",
    "创建了触发器卡": "created a trigger card",
    "更新了触发器卡": "updated a trigger card",
    "删除了触发器卡": "deleted a trigger card",
    "上传了触发器资源": "uploaded a trigger asset",
    "删除了触发器资源": "deleted a trigger asset",
    "上传了知识库源文件": "uploaded a knowledge base source file",
    "删除了知识库源文件": "deleted a knowledge base source file",
    "更改了剧本所有者": "changed the scenario owner",
    "归档了剧本（仍有房间占用）": "archived the scenario (still in use by rooms)",
    "强制删除了剧本（仍有房间占用）": "force-deleted the scenario (still in use by rooms)",
    "清除了剧本知识库（仅向量与索引）": "cleared the scenario knowledge base (vectors and index only)",
    "发布了导入任务": "published an import job",
    "删除了导入任务": "deleted an import job",
    "访问被拒绝": "was denied access",
    "访问房间被拒绝": "was denied room access",
    "访问剧本被拒绝": "was denied scenario access",
}

# 复合动作的前缀回落（例如 "更改了外观设置"）。
ACTION_PREFIX_LABELS_EN = (
    ("更改了", "changed "),
    ("更新了", "updated "),
    ("删除了", "deleted "),
    ("创建了", "created "),
    ("上传了", "uploaded "),
    ("保存了", "saved "),
    ("发布了", "published "),
    ("移除了", "removed "),
    ("记录了", "recorded "),
    ("恢复了", "restored "),
    ("重命名了", "renamed "),
    ("触发了", "triggered "),
    ("绑定了", "bound "),
    ("加入了", "joined "),
)

_log_config_dir: Path = CONFIG_DIR
_language_cache: tuple[float, str] | None = None


class CompactFormatter(logging.Formatter):
    def format(self, record):
        original_levelname = record.levelname
        record.levelname = LEVEL_LABELS.get(record.levelno, record.levelname)
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    compact = normalized.replace("_", "")
    if normalized in SENSITIVE_KEYS or compact in {
        "apikey",
        "authorization",
        "cookie",
        "setcookie",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "csrftoken",
        "token",
        "password",
        "secret",
        "secretkey",
        "privatekey",
        "clientsecret",
    }:
        return True
    return compact.endswith(("apikey", "accesstoken", "refreshtoken", "sessiontoken", "csrftoken", "password", "secret", "privatekey"))


def set_log_config_dir(config_dir: str | Path | None = None) -> None:
    """记录配置目录，供日志语言检测读取 general.toml。"""
    global _log_config_dir, _language_cache
    if config_dir:
        _log_config_dir = Path(config_dir)
    else:
        _log_config_dir = CONFIG_DIR
    _language_cache = None


def log_language() -> str:
    """读取当前界面语言；统一走分层加载（模板/env/CLI），异常时回落默认语言。

    用户配置文件缺失时保持历史行为：直接回落 ``DEFAULT_LOG_LANGUAGE``（英文），
    不读模板——日志语言只跟随用户显式配置，避免首次启动前的日志突然换语言。
    """
    global _language_cache
    path = _log_config_dir / "general.toml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return DEFAULT_LOG_LANGUAGE
    if _language_cache is not None and _language_cache[0] == mtime:
        return _language_cache[1]
    try:
        from trpg_server.config import load_settings

        language = str(load_settings(_log_config_dir).data.get("language", {}).get("language") or DEFAULT_LOG_LANGUAGE)
    except Exception:  # noqa: BLE001 - 日志路径绝不能因配置异常而崩溃
        language = DEFAULT_LOG_LANGUAGE
    _language_cache = (mtime, language)
    return language


def _is_english() -> bool:
    return log_language().strip().lower().startswith("en")


def _detail_separator() -> str:
    return ", " if _is_english() else "，"


def _message_separator() -> str:
    return "; " if _is_english() else "；"


def _unknown_user() -> str:
    return "unknown user" if _is_english() else "未知用户"


def _translate_action(action: str) -> str:
    if not _is_english():
        return action
    if action in ACTION_LABELS_EN:
        return ACTION_LABELS_EN[action]
    for prefix, translated in ACTION_PREFIX_LABELS_EN:
        if action.startswith(prefix):
            return f"{translated}{action[len(prefix):]}"
    return action


def user_action_text(username: Any = None, action: str = "进行了操作") -> str:
    display_name = str(username or _unknown_user()).strip() or _unknown_user()
    if _is_english():
        return f"User {display_name} {_translate_action(action)}"
    return f"用户 {display_name} {action}"


def log_user_action(logger: logging.Logger, message: str, **details: Any) -> None:
    _log(logger, logging.INFO, message, details)


def log_access_denied(logger: logging.Logger, message: str, **details: Any) -> None:
    """权限拒绝等安全事件，使用 WARNING 级别记录。"""
    _log(logger, logging.WARNING, message, details)


def _log(logger: logging.Logger, level: int, message: str, details: dict[str, Any]) -> None:
    safe_details = redact_sensitive(details)
    detail_text = _format_details(safe_details)
    if detail_text:
        logger.log(level, "%s%s%s", message, _message_separator(), detail_text)
        return
    logger.log(level, "%s", message)


def _format_details(details: dict[str, Any]) -> str:
    parts = []
    for key, value in details.items():
        if value is None or value == "":
            continue
        parts.append(_format_detail(key, value))
    return _detail_separator().join(parts)


def _format_detail(key: Any, value: Any) -> str:
    label = DETAIL_LABELS_EN.get(str(key), str(key)) if _is_english() else str(key)
    return f"{label} {_format_log_value(value)}"


def _format_log_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if len(text) > MAX_LOG_VALUE_LENGTH:
        return f"{text[:MAX_LOG_VALUE_LENGTH]}..."
    return text


_logging_configured = False


def configure_logging(log_dir: str | Path = "logs", config_dir: str | Path | None = None) -> None:
    global _logging_configured
    set_log_config_dir(config_dir)
    # 同一进程内只初始化一次。否则每次 create_app() 都会新建一个日志文件并
    # 重挂 handler（例如 server.py 为了让「启动开始」尽早输出而先调用一次，
    # create_app 再调用一次就会产生多余的日志文件）。
    if _logging_configured:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(log_dir) / f"ai_trpg_{started_at}.log"

    formatter = CompactFormatter("[%(asctime)s][%(levelname)s][%(threadName)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    _logging_configured = True