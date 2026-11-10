"""密钥类配置键的统一定义与打码工具。

配置读取接口（routes/config.py）与网站 Agent 工具（site_agent_tools/settings.py）
必须共用同一套规则，避免出现「配置接口打码、Agent 接口明文」的分叉。

识别方式以键名后缀为主：``apikey`` / ``api_key`` / ``token`` / ``secret`` /
``password`` 等，大小写与 ``-``/``_`` 等分隔符归一化后匹配，新增密钥（例如
``voice_cloud_api_key``）无需再登记。显式清单 ``SECRET_GENERAL_KEYS`` 仅作补充，
用于键名不符合后缀规则的密钥。

键为 ``(section, key)``：section 可以带点号（如 ``network.penetration.settings``），
与 general.toml 中出现的段名保持一致。
"""

from __future__ import annotations

from typing import Any

# 显式清单（补充）：读取时一律打码，写入时一律拒绝。
SECRET_GENERAL_KEYS = frozenset(
    {
        # 网络穿透令牌，经 /api/network/penetration/config 管理。
        ("network.penetration.settings", "auth_token"),
        # 旧生图接口密钥，经 /api/immersive/settings/secret 管理。
        ("immersive", "api_key"),
    }
)

# 后缀清单：键名归一化（小写、仅保留字母数字）后以此结尾即视为密钥。
SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "authtoken",
    "accesstoken",
    "refreshtoken",
    "sessiontoken",
    "csrftoken",
    "token",
    "secret",
    "password",
    "passwd",
    "privatekey",
    "credential",
)

SECRET_MASK = "******"


def normalize_key(key: Any) -> str:
    """归一化键名：小写并去掉 ``_``/``-``/``.`` 等分隔符，便于后缀匹配。"""
    return "".join(character for character in str(key or "").strip().lower() if character.isalnum())


def is_sensitive_key(key: Any) -> bool:
    """按键名后缀判断是否为密钥字段（大小写与下划线不敏感）。"""
    normalized = normalize_key(key)
    if not normalized:
        return False
    return any(normalized.endswith(suffix) for suffix in SENSITIVE_KEY_SUFFIXES)


def is_secret_config_key(section: str | None, key: Any) -> bool:
    """显式清单或后缀规则任一命中即视为密钥字段。"""
    if is_sensitive_key(key):
        return True
    return section is not None and (str(section), str(key)) in SECRET_GENERAL_KEYS


def redact_config_value(value: Any, *, section: str | None = None, mask: str = SECRET_MASK) -> Any:
    """递归打码：命中密钥规则的键整值替换为掩码，列表/嵌套字典逐层处理。"""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_secret_config_key(section, key):
                redacted[key] = mask
            else:
                redacted[key] = redact_config_value(item, section=section, mask=mask)
        return redacted
    if isinstance(value, list):
        return [redact_config_value(item, section=section, mask=mask) for item in value]
    return value


def redact_config_tree(data: dict[str, Any], *, mask: str = SECRET_MASK) -> dict[str, Any]:
    """对整棵扁平段配置树（``{section: {key: value}}``）逐段递归打码。"""
    return {
        str(section): redact_config_value(values, section=str(section), mask=mask)
        for section, values in data.items()
    }


__all__ = [
    "SECRET_GENERAL_KEYS",
    "SECRET_MASK",
    "SENSITIVE_KEY_SUFFIXES",
    "is_secret_config_key",
    "is_sensitive_key",
    "normalize_key",
    "redact_config_tree",
    "redact_config_value",
]
