"""只读主题目录 API：供第三方前端/组件开发者做无头客制化。

该模块只提供元数据，不参与也不改变现有主题切换逻辑：
- 前端主题仍由 ``body`` 上的类名（``theme-light`` / ``theme-tome`` 等）决定；
- 这里的 ``tokens`` 是设计 token 的公开快照，便于外部实现所需的 CSS 变量；
- ``token_contract`` 给出 token 与 CSS 变量的对应关系，组件只需使用这些变量即可自动适配所有主题。
"""

from flask import Blueprint

from trpg_server.responses import success_response

bp = Blueprint("themes", __name__)

THEME_API_VERSION = 1
DEFAULT_THEME = "light"

# token 键名 -> CSS 变量名，作为组件客制化的稳定契约。
TOKEN_CSS_VARIABLES = {
    "bg_page": "--theme-bg-page",
    "bg_panel": "--theme-bg-panel",
    "bg_panel_muted": "--theme-bg-panel-muted",
    "bg_elevated": "--theme-bg-elevated",
    "text_primary": "--theme-text-primary",
    "text_secondary": "--theme-text-secondary",
    "text_muted": "--theme-text-muted",
    "border_subtle": "--theme-border-subtle",
    "border_strong": "--theme-border-strong",
    "accent_primary": "--theme-accent-primary",
    "accent_primary_hover": "--theme-accent-primary-hover",
    "accent_secondary": "--theme-accent-secondary",
    "accent_soft": "--theme-accent-soft",
    "danger": "--theme-danger",
    "success": "--theme-success",
    "warning": "--theme-warning",
    "chat_other_bg": "--theme-chat-other-bg",
    "chat_player_bg": "--theme-chat-player-bg",
    "chat_kp_bg": "--theme-chat-kp-bg",
    "chat_dice_bg": "--theme-chat-dice-bg",
}

_THEME_SELECTORS = [
    {"value": "light", "theme_id": "light", "label_key": "profile.theme.light"},
    {"value": "dark", "theme_id": "dark", "label_key": "profile.theme.dark"},
    {"value": "pattern_cyber_2", "theme_id": "pattern_cyber_2", "label_key": "profile.theme.cyber"},
    {"value": "pattern_tome", "theme_id": "pattern_tome", "label_key": "profile.theme.tome"},
    {"value": "pattern_coc", "theme_id": "pattern_coc", "label_key": "profile.theme.coc"},
    {"value": "system", "theme_id": None, "label_key": "profile.theme.system"},
]

_THEMES = [
    {
        "id": "light",
        "label_key": "profile.theme.light",
        "body_classes": ["theme-light", "light-theme"],
        "color_scheme": "light",
        "tokens": {
            "bg_page": "#f6f7fb",
            "bg_panel": "#ffffff",
            "bg_panel_muted": "#f1f3f7",
            "bg_elevated": "#ffffff",
            "text_primary": "#1f2933",
            "text_secondary": "#374151",
            "text_muted": "#697386",
            "border_subtle": "#d8dee9",
            "border_strong": "#b7c0ce",
            "accent_primary": "#2f6fed",
            "accent_primary_hover": "#255fcf",
            "accent_secondary": "#5b8def",
            "accent_soft": "#eef4ff",
            "danger": "#c83232",
            "success": "#237a4b",
            "warning": "#f0c36a",
            "chat_other_bg": "#ffffff",
            "chat_player_bg": "#007bff",
            "chat_kp_bg": "#e3f2fd",
            "chat_dice_bg": "#fff3cd",
        },
    },
    {
        "id": "dark",
        "label_key": "profile.theme.dark",
        "body_classes": ["theme-dark", "dark-theme"],
        "color_scheme": "dark",
        "tokens": {
            "bg_page": "#141719",
            "bg_panel": "#24292e",
            "bg_panel_muted": "#2f353b",
            "bg_elevated": "#30363d",
            "text_primary": "#d7dde3",
            "text_secondary": "#b8c0ca",
            "text_muted": "#8f9aa5",
            "border_subtle": "#444c56",
            "border_strong": "#636e7b",
            "accent_primary": "#58a6ff",
            "accent_primary_hover": "#79b8ff",
            "accent_secondary": "#66e2ad",
            "accent_soft": "rgba(88, 166, 255, 0.16)",
            "danger": "#f97583",
            "success": "#56d364",
            "warning": "#f0c36a",
            "chat_other_bg": "#24292e",
            "chat_player_bg": "#1f6feb",
            "chat_kp_bg": "#20364e",
            "chat_dice_bg": "#3d3421",
        },
    },
    {
        "id": "pattern_cyber_2",
        "label_key": "profile.theme.cyber",
        "body_classes": ["theme-cyber-2"],
        "color_scheme": "dark",
        "tokens": {
            "bg_page": (
                "linear-gradient(135deg, rgba(8, 19, 25, 0.96), rgba(11, 35, 38, 0.94)), "
                "radial-gradient(circle at 85% 8%, rgba(37, 170, 121, 0.22), transparent 30%), "
                "radial-gradient(circle at 12% 88%, rgba(47, 111, 237, 0.18), transparent 34%)"
            ),
            "bg_panel": "rgba(7, 18, 24, 0.78)",
            "bg_panel_muted": "rgba(14, 37, 43, 0.82)",
            "bg_elevated": "rgba(9, 28, 34, 0.92)",
            "text_primary": "#c7d8d1",
            "text_secondary": "#aebfb8",
            "text_muted": "rgba(199, 216, 209, 0.64)",
            "border_subtle": "rgba(126, 232, 190, 0.22)",
            "border_strong": "rgba(126, 232, 190, 0.42)",
            "accent_primary": "#66e2ad",
            "accent_primary_hover": "#8ff0c6",
            "accent_secondary": "#72a7ff",
            "accent_soft": "rgba(102, 226, 173, 0.14)",
            "danger": "#ff7d90",
            "success": "#66e2ad",
            "warning": "#f0c36a",
            "chat_other_bg": "rgba(7, 18, 24, 0.82)",
            "chat_player_bg": "#66e2ad",
            "chat_kp_bg": "rgba(19, 59, 63, 0.88)",
            "chat_dice_bg": "rgba(77, 61, 29, 0.9)",
        },
    },
    {
        "id": "pattern_tome",
        "label_key": "profile.theme.tome",
        "body_classes": ["theme-tome"],
        "color_scheme": "light",
        "tokens": {
            "bg_page": "#ece3d1",
            "bg_panel": "#faf5e9",
            "bg_panel_muted": "#f0e7d3",
            "bg_elevated": "#fffdf6",
            "text_primary": "#3c2b1e",
            "text_secondary": "#58442f",
            "text_muted": "#8a7358",
            "border_subtle": "#d8c9a8",
            "border_strong": "#c0a87e",
            "accent_primary": "#a6543c",
            "accent_primary_hover": "#8f422d",
            "accent_secondary": "#b07a45",
            "accent_soft": "rgba(166, 84, 60, 0.13)",
            "danger": "#a23e2e",
            "success": "#5d7a4a",
            "warning": "#d9a441",
            "chat_other_bg": "#fffaf0",
            "chat_player_bg": "#a6543c",
            "chat_kp_bg": "rgba(229, 214, 183, 0.62)",
            "chat_dice_bg": "rgba(217, 164, 65, 0.22)",
        },
    },
    {
        "id": "pattern_coc",
        "label_key": "profile.theme.coc",
        "body_classes": ["theme-coc"],
        "color_scheme": "dark",
        "tokens": {
            "bg_page": "#050a10",
            "bg_panel": "rgba(8, 16, 22, 0.84)",
            "bg_panel_muted": "rgba(13, 27, 33, 0.8)",
            "bg_elevated": "rgba(11, 24, 30, 0.94)",
            "text_primary": "#e7dfc9",
            "text_secondary": "#c2bfae",
            "text_muted": "#8b988f",
            "border_subtle": "rgba(104, 214, 192, 0.17)",
            "border_strong": "rgba(213, 173, 98, 0.4)",
            "accent_primary": "#5ddbc0",
            "accent_primary_hover": "#8af3da",
            "accent_secondary": "#d5ad62",
            "accent_soft": "rgba(93, 219, 192, 0.12)",
            "danger": "#e06a6a",
            "success": "#63dcab",
            "warning": "#e8bb6a",
            "chat_other_bg": "rgba(9, 20, 27, 0.88)",
            "chat_player_bg": "#0f5c57",
            "chat_kp_bg": "rgba(24, 20, 42, 0.9)",
            "chat_dice_bg": "rgba(66, 49, 18, 0.82)",
        },
    },
]


@bp.route("/api/themes")
def list_themes():
    """返回主题目录与设计 token 契约（只读，不改变任何现有功能）。"""
    return success_response(
        {
            "version": THEME_API_VERSION,
            "default_theme": DEFAULT_THEME,
            "selectors": _THEME_SELECTORS,
            "themes": _THEMES,
            "token_contract": {
                "description": "组件只需使用这些 CSS 变量即可自动适配全部主题；值为变量快照，可能包含渐变等 CSS 值。",
                "css_variables": TOKEN_CSS_VARIABLES,
            },
        }
    )