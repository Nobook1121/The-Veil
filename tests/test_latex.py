"""LaTeX 叙事排版：提示词、配置矩阵、历史清洗、缓存指纹与前端契约。"""
import time
from pathlib import Path

from trpg_server.agents.latex import (
    LatexSettings,
    build_latex_prompt,
    clean_history_for_model,
    clean_message_for_model,
    flatten_latex,
    load_latex_settings,
)
from trpg_server.routes.chat_helpers import _history_needs_compaction

ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides) -> LatexSettings:
    return LatexSettings(**overrides)


def test_prompt_includes_limits_intensity_and_json_rule():
    prompt = build_latex_prompt(_settings(intensity="standard", max_formulas_per_message=12, max_formula_chars=300))

    assert "12" in prompt
    assert "300" in prompt
    assert "raisebox" in prompt
    assert "JSON" in prompt
    assert "最多 2 个独立公式块" in prompt


def test_prompt_intensity_variants():
    light = build_latex_prompt(_settings(intensity="light"))
    rich = build_latex_prompt(_settings(intensity="rich"))

    assert "最多 1 个独立公式块" in light
    assert "最多 3 个独立公式块" in rich


def test_prompt_disabled_by_master_or_kp_switch():
    assert build_latex_prompt(_settings(enabled=False)) == ""
    assert build_latex_prompt(_settings(kp_enabled=False)) == ""


def test_settings_invalid_intensity_and_clamped_limits():
    settings = LatexSettings.from_chat_section(
        {
            "latex_intensity": "wild",
            "latex_max_formulas_per_message": 999,
            "latex_max_formula_chars": 1,
            "latex_max_clean_message_chars": 100000,
        }
    )

    assert settings.intensity == "standard"
    assert settings.max_formulas_per_message == 50
    assert settings.max_formula_chars == 50
    assert settings.max_clean_message_chars == 10000


def test_load_latex_settings_reads_template_defaults(tmp_path):
    settings = load_latex_settings(tmp_path)

    assert settings.enabled is True
    assert settings.kp_enabled is True
    assert settings.player_enabled is True
    assert settings.intensity == "standard"


def test_flatten_colors_boxes_and_rules():
    text = r"$$\fcolorbox{#111}{#eee}{\textcolor{#8b0000}{别回头}}\rule[-0.4em]{1.2em}{2em}$$"

    cleaned = flatten_latex(text)

    assert "别回头" in cleaned
    assert "▮" in cleaned
    assert "\\textcolor" not in cleaned
    assert "\\fcolorbox" not in cleaned
    assert "\\rule" not in cleaned


def test_flatten_array_and_block_characters():
    text = r"$$\begin{array}{c}\Huge 上方\\[0.8em]下方\end{array}\textcolor{#111}{████}$$"

    cleaned = flatten_latex(text)

    assert "上方" in cleaned
    assert "下方" in cleaned
    assert "&" not in cleaned
    assert "0.8em" not in cleaned
    assert "[色块]" in cleaned


def test_escaped_dollar_in_display_formula_survives_as_text():
    assert flatten_latex(r"$$共 \$3$$") == "共 $3"


def test_currency_escaped_and_unclosed_dollars_survive():
    assert flatten_latex("价格是 $100，不是公式") == "价格是 $100，不是公式"
    assert flatten_latex(r"这是 \$ 转义") == r"这是 \$ 转义"
    assert flatten_latex("未闭合的 $xyz") == "未闭合的 $xyz"


def test_code_regions_and_inline_code_are_untouched():
    text = "```\n$\\textcolor{red}{别}$\n```\n行内 `$a$` 与正文 $b$"

    cleaned = flatten_latex(text)

    assert "$\\textcolor{red}{别}$" in cleaned
    assert "`$a$`" in cleaned
    assert "正文 b" in cleaned


def test_flatten_is_idempotent():
    text = r"走廊 $走廊尽头\textcolor{#8b0000}{有什么在呼吸}$ 之后"

    once = flatten_latex(text)

    assert flatten_latex(once) == once


def test_formula_limits_keep_overflow_raw():
    settings = _settings(max_formulas_per_message=2)
    text = "$a_1$ $b_2$ $c_3$"

    cleaned = flatten_latex(text, settings)

    assert "c_3" in cleaned
    assert "$c_3$" in cleaned


def test_long_inline_formula_becomes_placeholder():
    settings = _settings(max_formula_chars=50)
    text = "前面 $" + "\\textcolor{red}{" + ("x" * 60) + "}$ 后面"

    assert flatten_latex(text, settings) == "前面 [公式] 后面"


def test_clean_message_truncates_and_marks_ellipsis():
    settings = _settings(max_clean_message_chars=200)
    cleaned = clean_message_for_model("字" * 5000, settings)

    assert len(cleaned) <= 200
    assert cleaned.endswith("…")


def test_cleaning_ignores_switches():
    text = r"$ \textcolor{red}{红色} $"
    for settings in (
        _settings(enabled=False),
        _settings(kp_enabled=False),
        _settings(player_enabled=False),
    ):
        assert "红色" in clean_message_for_model(text, settings)


def test_clean_history_copies_and_skips_non_dialogue_roles():
    history = [
        {"role": "system", "content": "$system$", "compact": True},
        {"role": "tool", "content": "$tool$"},
        {"role": "user", "content": r"我看向 $\textcolor{red}{门}$"},
        {"role": "assistant", "content": r"$\Huge 别$"},
    ]
    original = [dict(item) for item in history]

    cleaned = clean_history_for_model(history, _settings())

    assert history == original
    assert cleaned is not history
    assert cleaned[0]["content"] == "$system$"
    assert cleaned[1]["content"] == "$tool$"
    assert "门" in cleaned[2]["content"]
    assert "别" in cleaned[3]["content"]


def test_clean_message_empty_text_stays_empty():
    assert clean_message_for_model("", _settings()) == ""
    assert clean_message_for_model("   ", _settings()) == "   "


def test_cache_variant_matrix():
    base = _settings(intensity="standard", max_formulas_per_message=12, max_formula_chars=300)

    assert _settings(enabled=False).cache_variant == "off"
    assert _settings(kp_enabled=False).cache_variant == "kp_off"
    assert base.cache_variant == "on:standard:12:300"
    assert _settings(intensity="rich", max_formulas_per_message=12, max_formula_chars=300).cache_variant != base.cache_variant
    assert _settings(intensity="standard", max_formulas_per_message=6, max_formula_chars=300).cache_variant != base.cache_variant
    assert _settings(intensity="standard", max_formulas_per_message=12, max_formula_chars=200).cache_variant != base.cache_variant
    # 只影响显示/清洗的设置不进指纹。
    assert _settings(intensity="standard", player_enabled=False).cache_variant == base.cache_variant
    assert _settings(intensity="standard", max_clean_message_chars=500).cache_variant == base.cache_variant


def test_history_compaction_uses_cleaned_size():
    formula = r"$$\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}\hspace{-1.2em}\rule{1em}{2em}$$"
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": formula * 10}
        for index in range(10)
    ]
    raw_size = sum(len(item["content"]) for item in history)

    assert raw_size > 12000
    assert _history_needs_compaction(history) is False


def test_bare_multiline_latex_without_delimiters_is_flattened():
    text = (
        "意气骄满路，鞍马光照尘。\\\\借问何者为，人称是内臣。\\\\"
        r"\rlap{\textcolor{red}{\hspace{0.1pt}\textit{\textbf{\large是\normalsize岁\small江"
        r"\normalsize南\small旱，\normalsize衢\small州\large人\normalsize食\small人！？}}}"
        r"\textcolor{blue}{\large是\normalsize岁\small江\normalsize南\small旱}"
    )

    cleaned = flatten_latex(text)

    assert "意气骄满路，鞍马光照尘。" in cleaned
    assert "是岁江南旱" in cleaned
    for token in ("\\textcolor", "\\rlap", "\\hspace", "\\large", "\\normalsize", "\\small", "\\textbf", "\\\\"):
        assert token not in cleaned


def test_bare_block_keeps_escaped_dollar_and_currency_paragraphs():
    assert "价 $3" in flatten_latex(r"\textcolor{red}{价 \$3}")
    # 含未转义 $ 的段落交给公式逻辑，不整段扁平化（$5$ 是价格，保持原样）。
    text = r"价格 $5$，并写了 \textcolor{red}{重点}"
    assert flatten_latex(text) == text


def test_bare_latex_in_code_region_is_untouched():
    text = "```\n\\textcolor{red}{别回头}\n```"

    assert flatten_latex(text) == text


def test_flatten_large_input_is_fast():
    text = "普通文本 $a$ " * 10000
    started = time.perf_counter()

    cleaned = flatten_latex(text)

    elapsed = time.perf_counter() - started
    assert cleaned
    assert elapsed < 2.0


def test_frontend_settings_and_contract():
    settings_html = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    config_source = (ROOT / "frontend/src/app/config/ConfigManager.ts").read_text(encoding="utf-8")
    tabs_source = (ROOT / "frontend/src/app/tabs.ts").read_text(encoding="utf-8")
    latex_source = (ROOT / "frontend/src/app/latex.ts").read_text(encoding="utf-8")
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")

    for element_id in (
        "latexEnabled",
        "latexKpEnabled",
        "latexPlayerEnabled",
        "latexIntensity",
        "latexMaxFormulasPerMessage",
        "latexMaxFormulaChars",
        "latexMaxCleanMessageChars",
    ):
        assert f'id="{element_id}"' in settings_html
        assert element_id in config_source
        assert element_id in tabs_source

    for name in (
        "getLatexSettings",
        "isLatexEnabledFor",
        "protectLatex",
        "decorateLatex",
        "protectBareLatexBlock",
        "protectWrappedLatexBlock",
        "protectDelimitedBlock",
        "balanceLatexBraces",
    ):
        assert f"function {name}" in latex_source
    assert "const LATEX_BARE_SIGNAL_RE" in latex_source
    assert "const LATEX_RENDERED_SELECTOR = \".katex, .md-latex\"" in latex_source
    assert 'const LATEX_SANITIZE_ATTRS = ["data-latex", "data-display"]' in latex_source
    # 只输出 HTML 副本：MathML 副本在 CSS 缺失时会与 HTML 副本同时可见（文字重复）。
    assert 'output: "html"' in latex_source

    assert "function renderMarkdown(content: string, allowLatex = false)" in chat_source


def test_katex_assets_are_vendored_locally():
    fragment = (ROOT / "frontend/src/index/fragments/05-platform-modals-and-scripts.html").read_text(encoding="utf-8")

    assert 'href="/assets/vendor/katex/katex.min.css"' in fragment
    assert 'src="/assets/vendor/katex/katex.min.js"' in fragment
    assert "cdn.jsdelivr.net/npm/katex" not in fragment
    for relative in ("katex.min.css", "katex.min.js", "fonts/KaTeX_Main-Regular.woff2"):
        assert (ROOT / "data/assets/vendor/katex" / relative).is_file()


def test_long_single_dollar_block_is_flattened():
    poem = "意气骄满路，鞍马光照尘。" * 30
    wrapped = f"${poem}\\textcolor{{red}}{{别回头}}$"

    cleaned = flatten_latex(wrapped)

    assert "[公式]" not in cleaned
    assert "意气骄满路" in cleaned
    assert "别回头" in cleaned
    assert "\\textcolor" not in cleaned


def test_explicit_latex_delimiters_are_stripped():
    assert flatten_latex(r"\[\textcolor{red}{别回头}\]") == "别回头"
    assert flatten_latex(r"\(a_1 + b_2\)") == "a_1 + b_2"


def test_explicit_delimiters_in_sentence_are_untouched():
    assert flatten_latex(r"见附录\[1\]") == r"见附录\[1\]"


def test_long_display_block_is_flattened():
    long_text = "是岁江南旱，衢州人食人。" * 40
    text = f"$$\\textcolor{{red}}{{{long_text}}}$$"

    cleaned = flatten_latex(text)

    assert "[公式]" not in cleaned
    assert "是岁江南旱" in cleaned
    assert "\\textcolor" not in cleaned

