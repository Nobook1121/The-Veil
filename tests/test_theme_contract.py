"""主题能力契约的结构 / 单元测试。

只做静态检查（解析 TS 源码与 HTML 片段），不依赖浏览器：
- 每个已注册主题都必须实现 ``ThemeDefinition`` 的必需字段；
- 每个主题都必须声明浅色与深色两套 token；
- 每个主题都必须声明浮层策略（host = body）与 reduced-motion 行为；
- 旧的 FX 设置键必须被迁移到统一存储，而不是继续作为独立面板存在。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "frontend" / "src" / "app" / "themes" / "contract.ts"
SETTINGS_UI = ROOT / "frontend" / "src" / "app" / "themes" / "settings-ui.ts"
FRAGMENT = ROOT / "frontend" / "src" / "index" / "fragments" / "03-room-tools-auth-settings.html"
SCRIPTS = ROOT / "frontend" / "src" / "index" / "fragments" / "05-platform-modals-and-scripts.html"
APP_CSS = ROOT / "frontend" / "src" / "react" / "app.css"

REQUIRED_BLOCK_FIELDS = (
    "bodyClass",
    "labelKey",
    "descriptionKey",
    "preview",
    "tokens",
    "supportsLightAndDark",
    "settings",
)
TOKEN_KEYS = ("bg", "panel", "text", "accent", "border")
REQUIRED_FORBIDDEN_ANCESTOR_STYLES = (
    "transform",
    "filter",
    "backdrop-filter",
    "perspective",
    "isolation",
    "will-change",
    "z-index",
    "contain",
)


def _contract_source() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def _definition_blocks() -> dict[str, str]:
    source = _contract_source()
    matches = list(re.finditer(r'\bid:\s*"(\w+)"', source))
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        blocks[match.group(1)] = source[start:end]
    return blocks


def _uses_token_factory(theme_id: str) -> bool:
    source = _contract_source()
    position = source.index(f'id: "{theme_id}"')
    return "tokenDefinition(" in source[max(0, position - 160):position]


def test_theme_contract_exports_required_interface():
    source = _contract_source()
    for field in (
        "id",
        "bodyClass",
        "labelKey",
        "descriptionKey",
        "preview",
        "tokens",
        "supportsLightAndDark",
        "animated",
        "reducedMotion",
        "overlayPolicy",
        "settings",
        "mount",
        "unmount",
        "apply",
    ):
        assert re.search(rf"\b{field}[?:]", source), f"ThemeDefinition 缺少字段 {field}"


def test_every_registered_theme_declares_required_fields():
    blocks = _definition_blocks()
    assert set(blocks) == {
        "light",
        "dark",
        "system",
        "pattern_cyber_2",
        "pattern_coc",
        "pattern_tome",
    }
    for theme_id, block in blocks.items():
        for field in REQUIRED_BLOCK_FIELDS:
            assert re.search(rf"\b{field}:", block), f"主题 {theme_id} 缺少必需字段 {field}"


def test_every_registered_theme_declares_light_and_dark_tokens():
    blocks = _definition_blocks()
    token_constants = dict(
        re.findall(r"const (\w+Tokens): ThemeTokens = \{([^}]*)\}", _contract_source())
    )
    assert token_constants, "未找到任何 ThemeTokens 常量"
    for name, body in token_constants.items():
        for key in TOKEN_KEYS:
            assert re.search(rf"\b{key}:", body), f"token 常量 {name} 缺少 {key}"

    for theme_id, block in blocks.items():
        tokens = re.search(r"tokens:\s*\{\s*light:\s*(\w+),\s*dark:\s*(\w+)\s*\}", block)
        assert tokens, f"主题 {theme_id} 未声明 light/dark 两套 token"
        assert tokens.group(1) in token_constants, f"主题 {theme_id} 的 light token 未定义"
        assert tokens.group(2) in token_constants, f"主题 {theme_id} 的 dark token 未定义"


def test_overlay_policy_is_body_and_forbids_stacking_context():
    source = _contract_source()
    assert re.search(r'host:\s*"body"', source), "浮层宿主必须固定为 body"
    forbidden_block = re.search(
        r"forbiddenAncestorStyles:\s*\[(.*?)\]", source, re.S
    )
    assert forbidden_block, "未声明 forbiddenAncestorStyles"
    entries = forbidden_block.group(1)
    for prop in REQUIRED_FORBIDDEN_ANCESTOR_STYLES:
        assert prop in entries, f"浮层策略缺少禁止属性 {prop}"
    assert re.search(r"overlayPolicy:\s*OVERLAY_POLICY", source), "主题未引用统一浮层策略"


def test_every_registered_theme_declares_reduced_motion():
    source = _contract_source()
    blocks = _definition_blocks()
    # tokenDefinition 工厂统一补齐 reducedMotion。
    assert re.search(r"reducedMotion:\s*\"none\"", source)
    for theme_id, block in blocks.items():
        if _uses_token_factory(theme_id):
            continue
        match = re.search(r'reducedMotion:\s*"(freeze|none)"', block)
        assert match, f"主题 {theme_id} 未声明 reducedMotion"
    for theme_id in ("pattern_cyber_2", "pattern_coc"):
        block = blocks[theme_id]
        assert re.search(r'animated:\s*true', block), f"主题 {theme_id} 应带动画"
        assert re.search(r'reducedMotion:\s*"freeze"', block), (
            f"动画主题 {theme_id} 在 reduced-motion 下必须 freeze"
        )
    for theme_id in ("light", "dark", "system", "pattern_tome"):
        block = blocks[theme_id]
        assert not re.search(r"animated:\s*true", block), f"配色主题 {theme_id} 不应带动画"


def test_slider_fields_have_default_range_and_step():
    source = _contract_source()
    calls = re.findall(
        r'sliderField\(\s*"(\w+)",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)',
        source,
    )
    assert calls, "未找到任何 slider 字段"
    for key, default, minimum, maximum, step in calls:
        default_value = float(default)
        minimum_value = float(minimum)
        maximum_value = float(maximum)
        assert minimum_value <= default_value <= maximum_value, (
            f"{key} 默认值 {default_value} 不在 [{minimum_value}, {maximum_value}] 内"
        )
        assert float(step) > 0, f"{key} 缺少有效步进"


def test_color_fields_use_hex_defaults():
    source = _contract_source()
    for match in re.finditer(r'colorField\(\s*"\w+",\s*"([^"]+)"\)', source):
        value = match.group(1)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"颜色默认值不是 #rrggbb: {value}"


def test_cyber_theme_exposes_charset_presets_and_rain_controls():
    block = _definition_blocks()["pattern_cyber_2"]
    for key in ("rainDensity", "rainSpeed", "charset", "primaryColor", "backgroundColor", "textColor"):
        assert re.search(rf'"{key}"', block), f"赛博主题缺少可调项 {key}"
    for preset in ("katakana", "symbols", "hex", "binary", "mixed"):
        assert re.search(rf'value:\s*"{preset}"', block), f"赛博主题缺少字符集预设 {preset}"


def test_abyss_theme_exposes_tentacle_trail_and_veil_controls():
    block = _definition_blocks()["pattern_coc"]
    for key in (
        "tentacleDensity",
        "tentacleHeight",
        "tentacleHeightVariance",
        "swaySpeed",
        "sigilScale",
        "trailDensity",
        "veilAlpha",
        "veilBlur",
        "sidebarAlpha",
        "railAlpha",
        "chatAlpha",
    ):
        assert re.search(rf'"{key}"', block), f"深渊主题缺少可调项 {key}"


def test_palette_themes_offer_token_level_colors():
    blocks = _definition_blocks()
    for theme_id in ("light", "dark", "pattern_tome"):
        block = blocks[theme_id]
        assert re.search(r"paletteFields\(", block), f"{theme_id} 应由 paletteFields 生成配色项"


def test_legacy_fx_settings_are_migrated():
    source = _contract_source()
    assert 'trpg_abyss_fx_settings' in source
    assert 'trpg_cyber_fx_settings' in source
    assert 'trpg_theme_settings_v1' in source
    assert 'trpg_theme_settings_legacy_backup' in source
    assert "removeItem(LEGACY_ABYSS_KEY)" in source
    assert "removeItem(LEGACY_CYBER_KEY)" in source


def test_theme_ui_is_schema_driven_with_all_control_types():
    source = SETTINGS_UI.read_text(encoding="utf-8")
    for control in ("slider", "color", "select"):
        assert re.search(rf'field\.type === "{control}"', source), f"缺少 {control} 控件生成"
    # toggle 是 schema 的最后一种控件，代码中以 else 分支兜底，用类名确认存在。
    assert "theme-toggle" in source, "缺少 toggle 控件生成"
    assert "requestAnimationFrame" in source, "拖动预览必须用 rAF 节流"
    assert "theme-field-reset" in source, "每个字段必须提供恢复默认"


def test_theme_tabs_exist_and_old_fx_panels_are_gone():
    fragment = FRAGMENT.read_text(encoding="utf-8")
    assert 'data-settings="theme"' in fragment
    assert 'id="theme-settings-content"' in fragment
    assert 'id="themeAdminPicker"' in fragment
    assert 'id="profile-theme"' in fragment
    assert 'id="profileThemePicker"' in fragment
    assert 'id="profileThemeSelect"' in fragment
    # 旧的两套 FX 面板必须已经并入统一主题面板。
    assert 'id="abyssFxSettings"' not in fragment
    assert 'id="cyberFxSettings"' not in fragment


def test_theme_scripts_and_styles_are_wired():
    scripts = SCRIPTS.read_text(encoding="utf-8")
    contract_pos = scripts.index("js/themes/contract.js")
    settings_pos = scripts.index("js/themes/settings-ui.js")
    cyber_pos = scripts.index("js/themes/cyber/core.js")
    abyss_pos = scripts.index("js/themes/abyss/core.js")
    assert contract_pos < settings_pos < cyber_pos
    assert contract_pos < abyss_pos
    assert "12-theme-settings.css" in APP_CSS.read_text(encoding="utf-8")
