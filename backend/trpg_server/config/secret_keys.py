"""密钥类配置键的统一定义。

配置读取接口（routes/config.py）与网站 Agent 工具（site_agent_tools/settings.py）
必须共用同一份清单，避免出现「配置接口打码、Agent 接口明文」的分叉。

键为 ``(section, key)``：section 可以带点号（如 ``network.penetration.settings``），
与 general.toml 中出现的段名保持一致。
"""

from __future__ import annotations

# 密钥字段：读取时一律打码，写入时一律拒绝。
SECRET_GENERAL_KEYS = frozenset(
    {
        # 网络穿透令牌，经 /api/network/penetration/config 管理。
        ("network.penetration.settings", "auth_token"),
        # 旧生图接口密钥，经 /api/immersive/settings/secret 管理。
        ("immersive", "api_key"),
    }
)

__all__ = ["SECRET_GENERAL_KEYS"]
