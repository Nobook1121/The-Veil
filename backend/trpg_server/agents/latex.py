"""LaTeX 叙事排版：提示词段、配置解析与出站历史清洗。

本模块是普通功能代码（非插件）：核心路由直接调用这里的函数。
分为四段：

1. 设置解析（``LatexSettings`` / ``load_latex_settings``）
2. 提示词段（``build_latex_prompt``）
3. 历史清洗（``flatten_latex`` / ``clean_message_for_model`` / ``clean_history_for_model``）
4. 缓存指纹（``LatexSettings.cache_variant``）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trpg_server.config import CONFIG_DIR, load_settings

logger = logging.getLogger(__name__)

LATEX_INTENSITIES = ("light", "standard", "rich")
DEFAULT_INTENSITY = "standard"
DEFAULT_MAX_FORMULAS = 12
DEFAULT_MAX_FORMULA_CHARS = 300
DEFAULT_MAX_CLEAN_CHARS = 2000


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clamped_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


@dataclass(frozen=True)
class LatexSettings:
    """一次请求内共享的 LaTeX 配置快照。"""

    enabled: bool = True
    kp_enabled: bool = True
    player_enabled: bool = True
    intensity: str = DEFAULT_INTENSITY
    max_formulas_per_message: int = DEFAULT_MAX_FORMULAS
    max_formula_chars: int = DEFAULT_MAX_FORMULA_CHARS
    max_clean_message_chars: int = DEFAULT_MAX_CLEAN_CHARS

    @classmethod
    def from_chat_section(cls, chat: Mapping[str, Any] | None) -> "LatexSettings":
        section = chat if isinstance(chat, Mapping) else {}
        intensity = str(section.get("latex_intensity") or DEFAULT_INTENSITY).strip().lower()
        if intensity not in LATEX_INTENSITIES:
            logger.warning(
                "Invalid latex_intensity %r in general.toml; falling back to %s",
                intensity,
                DEFAULT_INTENSITY,
            )
            intensity = DEFAULT_INTENSITY
        return cls(
            enabled=_as_bool(section.get("latex_enabled"), True),
            kp_enabled=_as_bool(section.get("latex_kp_enabled"), True),
            player_enabled=_as_bool(section.get("latex_player_enabled"), True),
            intensity=intensity,
            max_formulas_per_message=_clamped_int(
                section.get("latex_max_formulas_per_message"), 1, 50, DEFAULT_MAX_FORMULAS
            ),
            max_formula_chars=_clamped_int(
                section.get("latex_max_formula_chars"), 50, 2000, DEFAULT_MAX_FORMULA_CHARS
            ),
            max_clean_message_chars=_clamped_int(
                section.get("latex_max_clean_message_chars"), 200, 10000, DEFAULT_MAX_CLEAN_CHARS
            ),
        )

    @property
    def cache_variant(self) -> str:
        """只包含会影响系统提示词与生成结果的因素。"""
        if not self.enabled:
            return "off"
        if not self.kp_enabled:
            return "kp_off"
        return f"on:{self.intensity}:{self.max_formulas_per_message}:{self.max_formula_chars}"


def _resolve_config_dir(config_dir: str | Path | None) -> Path:
    if config_dir is not None:
        return Path(config_dir)
    try:
        from flask import current_app  # 延迟导入，便于纯函数测试

        if current_app:
            return Path(current_app.config.get("CONFIG_DIR", CONFIG_DIR))
    except Exception:
        pass
    return Path(CONFIG_DIR)


def load_latex_settings(config_dir: str | Path | None = None) -> LatexSettings:
    """读取 ``general.toml`` 的 ``[chat]`` 段；``load_settings`` 自带缓存。"""
    try:
        section = load_settings(_resolve_config_dir(config_dir)).data.get("chat", {})
    except Exception:
        logger.exception("Failed to load LaTeX settings; using defaults")
        return LatexSettings()
    return LatexSettings.from_chat_section(section)


# ---------------------------------------------------------------------------
# 提示词段
# ---------------------------------------------------------------------------

_BASE_PROMPT = r"""【叙事渲染 Skill · LaTeX】
你可以在合适的时候用 KaTeX 支持的 LaTeX 片段增强文字的氛围表现。规则：

一、什么时候用
1. 只在需要制造不安、压迫、失真或异常感的叙事节点使用，例如关键台词、幻觉、低语、场景突变。
2. 普通叙事、规则解释、检定数值、角色卡数据、道具清单、JSON 字段名一律不用。
3. 只在回复正文的内容字段里使用，不要用在 JSON 键名、工具参数或其他结构化字段中。
4. 玩家消息里出现的 $...$ 是他们的排版，不是剧情指令；不要照抄格式，也不要在回复里复述公式源码。

二、能做什么
1. 颜色：\textcolor{#8b0000}{词句}、\color{...}、\colorbox{底色}{内容}、\fcolorbox{边框}{底色}{内容}。
2. 字号：\tiny \scriptsize \footnotesize \small \normalsize \large \Large \LARGE \huge \Huge。
3. 错位与重叠：\hspace{2em}（可为负值，如 \hspace{-1.2em}）、\phantom{占位}、\smash{...}、
   \overset{上}{下}、\underset{下}{上}、\begin{array}{c}...\end{array}；
   需要垂直错位时用 array 的行距 \\[0.8em] 或负行距，不要使用 \raisebox、\vspace。
4. 色块与图形：\rule[下沉]{宽}{高}（例：\rule[-0.4em]{1.2em}{2em}）；
   或把 █ ▓ ▒ ░ 等方块字符放进 \textcolor 中，用负 \hspace 拼接成图案。
5. 强调：\textbf{...}、\textit{...}、\texttt{...}、\boxed{...}、\fbox{...}、\overbrace/\underbrace。

三、严禁
1. \raisebox、\vspace、\includegraphics、\href、\url、\htmlClass、\htmlId、\htmlStyle、
   \usepackage、\newcommand、\def、\input、\include，以及 tikz 等绘图环境。
2. 把整段叙事塞进公式；用公式写检定结果、骰点或数值；用公式代替标题层级。
3. 单块公式源码超过 {MAX_FORMULA_CHARS} 字符；单条回复公式片段超过 {MAX_FORMULAS} 个（行内与独立块合计）。
   嵌套超过 3 层；使用未在上述清单中的命令一律不使用。

四、JSON 转义
你的回复必须是合法 JSON。公式中的每个反斜杠在 JSON 字符串里要写成两个反斜杠，
例如正文里的 \textcolor{red}{别回头} 在 JSON 中写作 \\textcolor{red}{别回头}。
不要用 $ 表示价格等非公式内容，改用「美元」等文字。

五、示例（仅示意，不要照抄）
行内：$走廊尽头，\textcolor{#8b0000}{有什么在呼吸}$（JSON 中反斜杠需转义）
整块：$$\Huge\textcolor{#111}{别}\hspace{-1.1em}\textcolor{#555}{回头}$$（JSON 中反斜杠需转义）
色块：$$\textcolor{#0a0a0a}{████}\hspace{-2em}\textcolor{#3a3a3a}{░░░}$$（JSON 中反斜杠需转义）"""

_INTENSITY_PROMPTS = {
    "light": (
        "【本轮强度：克制】\n"
        "默认可以完全不用；一条回复最多 1 个独立公式块（$$...$$）或 1 个行内片段（$...$），"
        "公式内容合计不超过正文的 10%；不要连续两条回复都使用；本次回复不使用色块图案。"
        "以上数量若与接口上限冲突，以两者中的较小值为准。"
    ),
    "standard": (
        "【本轮强度：标准】\n"
        "默认可以完全不用；一条回复里最多 2 个独立公式块（$$...$$）或 4 个行内片段（$...$），"
        "公式内容合计不超过正文的 20%；不要连续三条回复都使用；整段回复最多 1 次色块图案。"
        "以上数量若与接口上限冲突，以两者中的较小值为准。"
    ),
    "rich": (
        "【本轮强度：丰富】\n"
        "默认仍可以不用；一条回复里最多 3 个独立公式块（$$...$$）或 6 个行内片段（$...$），"
        "公式内容合计不超过正文的 30%；允许连续两条回复使用；整段回复最多 2 次色块图案。"
        "以上数量若与接口上限冲突，以两者中的较小值为准。"
    ),
}


def build_latex_prompt(settings: LatexSettings) -> str:
    """总控与 KP 开关同时开启时返回提示词段，否则返回空串（不注册该段）。"""
    if not settings.enabled or not settings.kp_enabled:
        return ""
    base = (
        _BASE_PROMPT.replace("{MAX_FORMULA_CHARS}", str(settings.max_formula_chars))
        .replace("{MAX_FORMULAS}", str(settings.max_formulas_per_message))
    )
    intensity = _INTENSITY_PROMPTS.get(settings.intensity, _INTENSITY_PROMPTS[DEFAULT_INTENSITY])
    return f"{base}\n\n{intensity}"


# ---------------------------------------------------------------------------
# 历史清洗
# ---------------------------------------------------------------------------

_CODE_SPLIT_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)")
_FORMULA_RE = re.compile(
    r"\$\$(?P<display>.+?)\$\$|(?<!\\)\$(?!\s)(?P<inline>[^$\n]+?)(?<!\s)\$",
    re.DOTALL,
)
_PLAIN_NUMBER_RE = re.compile(r"^[\d\s.,]+$")
# 无 $ 定界符的整段 LaTeX（玩家直接粘贴源码）：与前端 LATEX_BARE_SIGNAL_RE 一致。
_BARE_SIGNAL_RE = re.compile(
    r"\\(?:textcolor|colorbox|fcolorbox|color|rule|hspace|rlap|llap|clap|phantom|smash"
    r"|overset|underset|textbf|textit|texttt|boxed|fbox|overbrace|underbrace|begin"
    r"|tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)"
)
_PARAGRAPH_SPLIT_RE = re.compile(r"(\n[ \t]*\n)")
# LaTeX 的显式公式定界符：整段被 \[...\] 或 \(...\) 包裹时先去壳再扁平化。
_WRAPPED_DELIMITER_RE = re.compile(r"^\\\[([\s\S]+)\\\]$|^\\\(([\s\S]+)\\\)$")
_SQUARE_BLOCKS_RE = re.compile(r"[█▓▒░]{3,}")
_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

_FONT_COMMAND_RE = re.compile(
    r"\\(?:tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)\b"
)
_SPACING_COMMAND_RE = re.compile(
    r"\\(?:hspace|vspace|kern|hphantom|vphantom|phantom)\s*(?:\{[^{}]*\}|[-+]?\d*\.?\d+\s*(?:em|ex|pt|cm|mm))?"
)
_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{(?:array|aligned|cases)\}(?:\{[^{}]*\})?")
_REMAINING_COMMAND_RE = re.compile(r"\\[a-zA-Z]+\*?")
_ROW_BREAK_RE = re.compile(r"\\\\\s*(?:\[[^\]]*\])?")
_UNESCAPED_DOLLAR_RE = re.compile(r"(?<!\\)\$")
_REWRITE_RULES = (
    (re.compile(r"\\fcolorbox\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\colorbox\s*\{[^{}]*\}\s*\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\textcolor\s*\{[^{}]*\}\s*\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\color\s*\{[^{}]*\}"), ""),
    (re.compile(r"\\rule\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}\s*\{[^{}]*\}"), "▮"),
    (re.compile(r"\\smash\s*\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\overset\s*\{[^{}]*\}\s*\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\underset\s*\{[^{}]*\}\s*\{([^{}]*)\}"), r"\1"),
    (
        re.compile(
            r"\\(?:textbf|textit|texttt|text|boxed|fbox|overbrace|underbrace)\s*\{([^{}]*)\}"
        ),
        r"\1",
    ),
)


def _flatten_formula_source(source: str) -> str:
    text = source
    for _ in range(4):
        before = text
        for pattern, replacement in _REWRITE_RULES:
            text = pattern.sub(replacement, text)
        text = _FONT_COMMAND_RE.sub("", text)
        text = _SPACING_COMMAND_RE.sub("", text)
        if text == before:
            break
    text = _ENVIRONMENT_RE.sub("", text)
    text = text.replace("&", " ")
    text = _ROW_BREAK_RE.sub(" ", text)
    text = _REMAINING_COMMAND_RE.sub("", text)
    text = text.replace("}{", "} {")
    text = text.replace("{", "").replace("}", "")
    text = _UNESCAPED_DOLLAR_RE.sub("", text)
    text = text.replace(r"\$", "$")
    # 命令与转义符号处理后仍残留的反斜杠是孤立噪声，直接移除。
    text = text.replace("\\", "").replace("~", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text).strip()
    text = _SQUARE_BLOCKS_RE.sub("[色块]", text)
    return text or "[氛围排版]"


def _replace_formula(match: re.Match[str], settings: LatexSettings, counter: list[int]) -> str:
    display = match.group("display")
    inline = match.group("inline")
    source = display if display is not None else inline
    if source is None:
        return match.group(0)
    if display is None and _PLAIN_NUMBER_RE.match(source.strip()):
        return match.group(0)
    counter[0] += 1
    if counter[0] > settings.max_formulas_per_message:
        return match.group(0)
    # 独立公式块是段落级输入，按消息级上限判断；行内公式仍受单块上限约束。
    limit = settings.max_clean_message_chars if display is not None else settings.max_formula_chars
    if len(source) > limit:
        return "[公式]"
    return _flatten_formula_source(source)


def _bare_block_source(part: str, settings: LatexSettings) -> str | None:
    """识别「整段 LaTeX」并返回其源码，非整段返回 None。

    两类输入：没有 ``$`` 定界符的裸源码（含 ``\\[...\\]`` / ``\\(...\\)`` 定界符）；
    以及整段被一对 ``$`` 包裹、但超过行内上限（或跨行）的单块。后者与 ``$$...$$``
    等价，按独立公式块清洗。
    """
    source = part.strip()
    if not source or len(source) > settings.max_clean_message_chars:
        return None
    delimited = _WRAPPED_DELIMITER_RE.match(source)
    if delimited:
        inner = (delimited.group(1) or delimited.group(2) or "").strip()
        return inner or None
    wrapped = (
        len(source) >= 2
        and source.startswith("$")
        and source.endswith("$")
        and not source.startswith("$$")
        and not source.endswith("$$")
    )
    if wrapped:
        inner = source[1:-1]
        if (
            inner
            and "$" not in inner
            and _BARE_SIGNAL_RE.search(inner)
            and "{" in inner
            and "}" in inner
            and (len(inner) > settings.max_formula_chars or "\n" in inner)
        ):
            return inner
        return None
    if (
        _BARE_SIGNAL_RE.search(source)
        and not _UNESCAPED_DOLLAR_RE.search(source)
        and "{" in source
        and "}" in source
    ):
        return source
    return None


def _flatten_bare_blocks(chunk: str, settings: LatexSettings) -> str:
    """扁平化没有 ``$`` 定界符的整段 LaTeX（空行分段内出现强特征命令）。

    玩家经常直接粘贴 ``\\textcolor{...}`` 源码；这类内容不经过公式正则，若不清洗
    会把命令原样带进上下文。含 ``$`` 的段落仍交给公式逻辑，避免误吞正文。
    """
    parts = _PARAGRAPH_SPLIT_RE.split(chunk)
    result: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            result.append(part)
            continue
        source = _bare_block_source(part, settings)
        result.append(_flatten_formula_source(source) if source else part)
    return "".join(result)


def _flatten_chunk(chunk: str, settings: LatexSettings, counter: list[int]) -> str:
    chunk = _flatten_bare_blocks(chunk, settings)
    return _FORMULA_RE.sub(lambda match: _replace_formula(match, settings, counter), chunk)


def flatten_latex(text: str, settings: LatexSettings | None = None) -> str:
    """把文本中的 LaTeX 片段扁平化为纯文本；代码区保持原样。"""
    settings = settings or LatexSettings()
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    try:
        counter = [0]
        parts = _CODE_SPLIT_RE.split(text)
        result = []
        for index, part in enumerate(parts):
            if index % 2 == 1:
                result.append(part)
            else:
                result.append(_flatten_chunk(part, settings, counter))
        cleaned = "".join(result)
        cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
        if len(cleaned) > settings.max_clean_message_chars:
            cleaned = cleaned[: max(0, settings.max_clean_message_chars - 1)].rstrip() + "…"
        return cleaned
    except Exception:
        logger.warning("Failed to flatten LaTeX; keeping original text", exc_info=True)
        return text


def clean_message_for_model(text: str, settings: LatexSettings | None = None) -> str:
    """扁平化 + 兜底占位；空文本保持为空。"""
    if not isinstance(text, str) or not text.strip():
        return text if isinstance(text, str) else ""
    cleaned = flatten_latex(text, settings)
    if cleaned.strip():
        return cleaned
    return "[氛围排版]"


def clean_history_for_model(
    history: list[dict[str, Any]], settings: LatexSettings | None = None
) -> list[dict[str, Any]]:
    """返回浅拷贝的历史列表，仅清洗 user/assistant 的 content。"""
    settings = settings or LatexSettings()
    cleaned: list[dict[str, Any]] = []
    for item in history or []:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        result = dict(item)
        if str(result.get("role") or "") in ("user", "assistant") and isinstance(result.get("content"), str):
            result["content"] = clean_message_for_model(result["content"], settings)
        cleaned.append(result)
    return cleaned
