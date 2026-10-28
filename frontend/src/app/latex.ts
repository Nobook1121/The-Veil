/**
 * LaTeX 氛围排版：设置读取、公式占位与 KaTeX 渲染。
 *
 * 本文件是普通全局模块（项目以 `module: none` 编译），聊天流程直接调用这里的
 * 函数。职责只有三件事：
 *
 * 1. 从 `general.chat.latex_*` 读取站点级开关，并在非法值上收敛到默认值；
 * 2. `protectLatex` 把公式替换成不可被 Markdown 改写的 HTML 占位；
 * 3. `decorateLatex` 在净化后的容器上把占位渲染为 KaTeX。
 */

interface LatexSettings {
    enabled: boolean;
    kpEnabled: boolean;
    playerEnabled: boolean;
    intensity: "light" | "standard" | "rich";
    maxFormulasPerMessage: number;
    maxFormulaChars: number;
    maxCleanMessageChars: number;
}

const LATEX_RENDERED_SELECTOR = ".katex, .md-latex";
const LATEX_SANITIZE_ATTRS = ["data-latex", "data-display"];

const LATEX_INTENSITIES = ["light", "standard", "rich"] as const;

const KATEX_RENDER_OPTIONS: Record<string, unknown> = {
    throwOnError: false,
    strict: "ignore",
    trust: false,
    maxExpand: 1000,
    maxSize: 20,
    // 只要 HTML 副本：KaTeX 默认还会输出一份 MathML 无障碍副本，CSS 未加载时
    // 两份副本同时可见，消息里就会出现「别回头别回头」这类重复文字。
    output: "html",
};

// 代码区（围栏块与行内代码）不参与公式识别，避免把示例源码渲染成公式。
const LATEX_CODE_SPLIT_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g;
// 与后端清洗保持一致的匹配顺序与行内合法性约束。
const LATEX_FORMULA_RE = /\$\$([\s\S]+?)\$\$|(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$/g;
const LATEX_PLAIN_NUMBER_RE = /^[\d\s.,]+$/;
// 无 $ 定界符的整段 LaTeX（玩家直接粘贴源码）：按空行分段整体识别。
// 只认提示词允许的强特征命令，并要求成对花括号，避免把普通文本里的反斜杠误判成公式。
const LATEX_BARE_SIGNAL_RE = /\\(?:textcolor|colorbox|fcolorbox|color|rule|hspace|rlap|llap|clap|phantom|smash|overset|underset|textbf|textit|texttt|boxed|fbox|overbrace|underbrace|begin|tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)/;
const LATEX_PARAGRAPH_SPLIT_RE = /(\n[ \t]*\n)/;
const LATEX_UNESCAPED_DOLLAR_RE = /(?<!\\)\$/;

let latexWarningShown = false;

function latexWarnOnce(message: string): void {
    if (latexWarningShown) return;
    latexWarningShown = true;
    console.warn(message);
}

function latexAsBoolean(value: unknown, fallback: boolean): boolean {
    if (typeof value === "boolean") return value;
    if (typeof value === "string") return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
    if (value === null || value === undefined) return fallback;
    return Boolean(value);
}

function latexClampedInt(value: unknown, minimum: number, maximum: number, fallback: number): number {
    const parsed = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(minimum, Math.min(maximum, Math.trunc(parsed)));
}

function getLatexSettings(): LatexSettings {
    const manager = window.configManager;
    const read = (key: string, fallback: unknown): unknown => {
        if (!manager) return fallback;
        return manager.get("general", "chat", key, fallback);
    };

    const rawIntensity = String(read("latex_intensity", "standard")).trim().toLowerCase();
    let intensity: LatexSettings["intensity"] = "standard";
    if ((LATEX_INTENSITIES as readonly string[]).includes(rawIntensity)) {
        intensity = rawIntensity as LatexSettings["intensity"];
    } else {
        latexWarnOnce(`无效的 latex_intensity「${rawIntensity}」，已回退为标准强度`);
    }

    return {
        enabled: latexAsBoolean(read("latex_enabled", true), true),
        kpEnabled: latexAsBoolean(read("latex_kp_enabled", true), true),
        playerEnabled: latexAsBoolean(read("latex_player_enabled", true), true),
        intensity,
        maxFormulasPerMessage: latexClampedInt(read("latex_max_formulas_per_message", 12), 1, 50, 12),
        maxFormulaChars: latexClampedInt(read("latex_max_formula_chars", 300), 50, 2000, 300),
        maxCleanMessageChars: latexClampedInt(read("latex_max_clean_message_chars", 2000), 200, 10000, 2000),
    };
}

/** 总控关闭时所有类型都关闭；thinking/system 等类型一律不渲染。 */
function isLatexEnabledFor(type: string): boolean {
    const settings = getLatexSettings();
    if (!settings.enabled) return false;
    if (type === "kp") return settings.kpEnabled;
    if (type === "player" || type === "other") return settings.playerEnabled;
    return false;
}

function latexEscapeAttribute(value: string): string {
    return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * 把公式片段替换为 HTML 占位；单条消息的片段数与源码长度受设置限制，
 * 超限片段保持原文，交给普通文本显示。
 */
function protectLatex(content: string): string {
    // 组件缺失时不做占位：否则占位 HTML 会被转义，公式反而彻底消失。
    if (!content || !window.marked || !window.DOMPurify) return content;
    const settings = getLatexSettings();
    const state = { count: 0 };
    return content
        .split(LATEX_CODE_SPLIT_RE)
        .map((part, index) => (index % 2 === 1 ? part : protectLatexSegment(part, state, settings)))
        .join("");
}

function latexPlaceholder(source: string, display: boolean): string {
    const encoded = latexEscapeAttribute(encodeURIComponent(source));
    return `<span class="md-latex" data-latex="${encoded}" data-display="${display ? "1" : "0"}"></span>`;
}

/**
 * 无 `$` 定界符的整段 LaTeX：在空行分段内出现强特征命令时整体占位。
 *
 * 玩家经常直接把 `\textcolor{...}\\` 这类源码粘进聊天框，Markdown 会把 `\\` 吃成
 * `\`、把 `*`/`_` 当强调。整段占位后由 KaTeX 按独立公式块渲染，与 `$$...$$` 等价。
 * 含 `$` 的段落仍走行内/独立公式逻辑，避免误吞正常正文。
 */
function protectBareLatexBlock(paragraph: string, state: { count: number }, settings: LatexSettings): string | null {
    if (!paragraph || !LATEX_BARE_SIGNAL_RE.test(paragraph)) return null;
    if (LATEX_UNESCAPED_DOLLAR_RE.test(paragraph)) return null;
    if (!paragraph.includes("{") || !paragraph.includes("}")) return null;
    const source = paragraph.trim();
    if (!source || state.count >= settings.maxFormulasPerMessage) return null;
    // 整段源码是「消息级」输入，按清洗上限判断，不受单块公式字符数限制。
    if (source.length > settings.maxCleanMessageChars) return null;
    state.count += 1;
    return latexPlaceholder(source, true);
}

/**
 * 整段被单个 `$...$` 包裹、且长度超过行内上限（或跨行）的源码：按独立公式块渲染。
 *
 * 玩家常把整段诗句塞进一对 `$`，行内上限（默认 300 字符）会把整段拒之门外，
 * 消息只能显示原始源码。这类超长单块按「消息级」上限判断，与 `$$...$$` 等价。
 */
function protectWrappedLatexBlock(paragraph: string, state: { count: number }, settings: LatexSettings): string | null {
    const trimmed = paragraph.trim();
    if (trimmed.length < 4 || !trimmed.startsWith("$") || !trimmed.endsWith("$")) return null;
    if (trimmed.startsWith("$$") || trimmed.endsWith("$$")) return null;
    const source = trimmed.slice(1, -1);
    if (source.includes("$") || !source) return null;
    if (!LATEX_BARE_SIGNAL_RE.test(source)) return null;
    if (!source.includes("{") || !source.includes("}")) return null;
    if (source.length <= settings.maxFormulaChars && !source.includes("\n")) return null;
    if (state.count >= settings.maxFormulasPerMessage) return null;
    if (source.length > settings.maxCleanMessageChars) return null;
    state.count += 1;
    return latexPlaceholder(source, true);
}

/**
 * 整段被 `\[...\]` 或 `\(...\)` 包裹的源码：去掉定界符后按公式占位。
 *
 * `\[`/`\]` 是 LaTeX 的独立公式定界符，但 KaTeX 的 `renderToString` 不接受定界符，
 * 直接传入会报 `Undefined control sequence: \[` 并回退成原文。这里先剥离再渲染。
 */
function protectDelimitedBlock(paragraph: string, state: { count: number }, settings: LatexSettings): string | null {
    const trimmed = paragraph.trim();
    const display = /^\\\[([\s\S]+)\\\]$/.exec(trimmed);
    const inline = /^\\\(([\s\S]+)\\\)$/.exec(trimmed);
    const source = (display ? display[1] : inline ? inline[1] : null) ?? null;
    if (source === null || !source.trim()) return null;
    if (state.count >= settings.maxFormulasPerMessage) return null;
    if (source.length > settings.maxCleanMessageChars) return null;
    state.count += 1;
    return latexPlaceholder(source, Boolean(display));
}

function protectInlineFormulas(segment: string, state: { count: number }, settings: LatexSettings): string {
    return segment.replace(LATEX_FORMULA_RE, (full, displaySource: string | undefined, inlineSource: string | undefined) => {
        const source = displaySource !== undefined ? displaySource : inlineSource;
        if (source === undefined) return full;
        if (state.count >= settings.maxFormulasPerMessage) return full;
        // 独立公式块是段落级输入，按消息级上限判断；行内公式仍受单块上限约束。
        const limit = displaySource !== undefined ? settings.maxCleanMessageChars : settings.maxFormulaChars;
        if (source.length === 0 || source.length > limit) return full;
        if (displaySource === undefined && LATEX_PLAIN_NUMBER_RE.test(source.trim())) return full;
        state.count += 1;
        return latexPlaceholder(source, displaySource !== undefined);
    });
}

function protectLatexSegment(segment: string, state: { count: number }, settings: LatexSettings): string {
    return segment
        .split(LATEX_PARAGRAPH_SPLIT_RE)
        .map((part, index) => {
            if (index % 2 === 1) return part;
            const block = protectDelimitedBlock(part, state, settings)
                ?? protectWrappedLatexBlock(part, state, settings)
                ?? protectBareLatexBlock(part, state, settings);
            return block ?? protectInlineFormulas(part, state, settings);
        })
        .join("");
}

function decodeLatexSource(value: string): string {
    try {
        return decodeURIComponent(value);
    } catch {
        return value;
    }
}

/**
 * 补齐未闭合的花括号，丢弃多余的右括号。
 *
 * 玩家手写/粘贴的 LaTeX 常有漏写的 `}`（例如 `\rlap{\textcolor{red}{...}}` 少一位），
 * KaTeX 会直接报 "Expected '}', got 'EOF'"。这类输入按作者意图补全后仍可渲染。
 */
function balanceLatexBraces(source: string): string {
    let depth = 0;
    let result = "";
    for (let index = 0; index < source.length; index += 1) {
        const char = source[index];
        if (char === "\\") {
            result += char;
            if (index + 1 < source.length) {
                result += source[index + 1];
                index += 1;
            }
            continue;
        }
        if (char === "{") {
            depth += 1;
            result += char;
            continue;
        }
        if (char === "}") {
            if (depth === 0) continue;
            depth -= 1;
            result += char;
            continue;
        }
        result += char;
    }
    return depth > 0 ? result + "}".repeat(depth) : result;
}

/** KaTeX 不可用或渲染失败时把占位恢复为可读原文。 */
function restoreLatexHost(host: HTMLElement): void {
    const source = decodeLatexSource(host.dataset.latex || "");
    if (!source) {
        host.remove();
        return;
    }
    host.textContent = host.dataset.display === "1" ? `$$${source}$$` : `$${source}$`;
    host.classList.add("latex-fallback");
    host.dataset.latexDone = "1";
}

/**
 * 在已净化并插入 DOM 的容器上渲染公式占位。
 *
 * 用宿主级 `data-latex-done` 去重，而不是容器级标记：KP 回复替换会复用同一个
 * `.message-content` 并重写 `innerHTML`，容器级标记会残留导致新内容被跳过。
 */
function decorateLatex(container: HTMLElement): void {
    if (!container) return;
    const hosts = Array.from(container.querySelectorAll<HTMLElement>(".md-latex"));
    if (hosts.length === 0) return;

    const renderer = window.katex;
    if (!renderer) {
        hosts.forEach(restoreLatexHost);
        latexWarnOnce("KaTeX 未加载，LaTeX 片段以原文显示");
        return;
    }

    hosts.forEach((host) => {
        if (host.dataset.latexDone === "1") return;
        const source = decodeLatexSource(host.dataset.latex || "");
        if (!source) {
            host.remove();
            return;
        }
        const display = host.dataset.display === "1";
        try {
            // 先试渲染，成功再写入 DOM；KaTeX 在 throwOnError:false 下会用
            // `.katex-error` 承载语法错误，这类输出同样按失败处理。
            const options = { ...KATEX_RENDER_OPTIONS, displayMode: display };
            let html = renderer.renderToString(source, options);
            if (!html || html.includes("katex-error")) {
                // 手写源码常见漏写花括号，补全后再试一次。
                const balanced = balanceLatexBraces(source);
                if (balanced !== source) html = renderer.renderToString(balanced, options);
            }
            if (!html || html.includes("katex-error")) {
                restoreLatexHost(host);
                return;
            }
            host.innerHTML = html;
            host.dataset.latexDone = "1";
        } catch (error) {
            console.warn("LaTeX 渲染失败，已回退为原文:", error);
            restoreLatexHost(host);
        }
    });
}
