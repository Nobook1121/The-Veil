"""应用版本与产品信息的唯一真源。

版本号只允许出现在这里；``trpg_server.__version__`` 通过重新导出保持兼容，
关于页、更新检查与打包脚本都从本模块读取。
"""

APP_VERSION = "1.0.4"
APP_NAME = "The Veil"
APP_NAME_ZH = "帷幕"
APP_DISPLAY_NAME = f"{APP_NAME_ZH} {APP_NAME}"
APP_DESCRIPTION = "自托管 AI 跑团平台"

APP_LICENSE_SPDX = "Apache-2.0"
APP_LICENSE_NAME = "Apache License 2.0"
APP_LICENSE_URL = "https://github.com/Nobook1121/The-Veil/blob/main/LICENSE"

APP_DEFAULT_REPO = "Nobook1121/The-Veil"

__all__ = [
    "APP_DEFAULT_REPO",
    "APP_DESCRIPTION",
    "APP_DISPLAY_NAME",
    "APP_LICENSE_NAME",
    "APP_LICENSE_SPDX",
    "APP_LICENSE_URL",
    "APP_NAME",
    "APP_NAME_ZH",
    "APP_VERSION",
]
