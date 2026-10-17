// 头像 / 剧本封面的「文字化」渲染工具。
//
// 项目已不再使用任何内置默认图片：
// - 头像：取名称首字，按名称哈希分配稳定色相，生成 SVG data URI，供现有 <img> 直接使用。
// - 剧本封面：直接用剧本名渲染，自动换行、按长度自适应字号，填充封面区域。
//
// 之所以输出为 data URI 而不是改造成 DOM 结构，是因为前端各处头像/封面都是给
// <img>.src 赋值或写进模板字符串，data URI 可以零侵入替换；后端 CSP 的 img-src
// 已包含 data:，因此无需额外放宽策略。

const TrpgAvatar = (() => {
    function escapeXml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&apos;");
    }

    // 取首个字符作为头像文字：中英文都取第一个字符，英文统一转大写。
    function initial(name: string): string {
        const text = (name || "").trim();
        return text ? text.charAt(0).toUpperCase() : "?";
    }

    // 由名称推导稳定色相，让不同名称的文字头像/封面色调互相区分。
    function hue(name: string): number {
        const text = name || "?";
        let hash = 0;
        for (let index = 0; index < text.length; index += 1) {
            hash = (hash * 31 + text.charCodeAt(index)) % 360;
        }
        return hash;
    }

    function toDataUri(svg: string): string {
        return `data:image/svg+xml,${encodeURIComponent(svg)}`;
    }

    // 正方形文字头像：与容器等比，容器自身的 border-radius 负责裁成圆形。
    function avatarTextDataUri(name: string): string {
        const label = escapeXml(initial(name));
        const background = `hsl(${hue(name)}, 45%, 42%)`;
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">`
            + `<rect width="100" height="100" fill="${background}"/>`
            + `<text x="50" y="50" text-anchor="middle" dominant-baseline="central"`
            + ` font-size="52" font-family="system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif"`
            + ` fill="#ffffff">${label}</text></svg>`;
        return toDataUri(svg);
    }

    // 有头像地址就用地址，否则回退到「名称首字」文字头像。
    function resolveAvatarSrc(name: string, avatar?: string | null): string {
        const value = (avatar || "").trim();
        return value || avatarTextDataUri(name);
    }

    const COVER_WIDTH = 300;
    const COVER_HEIGHT = 150;
    const COVER_PADDING_X = 30;
    // 剧本卡的封面容器固定高 150px，但卡片宽度会随栅格变化：容器长宽比最宽时
    // 会裁掉上下各约 20px，因此文字只占垂直方向中间 70px，保证不被裁切。
    const COVER_TEXT_HEIGHT = 70;
    const COVER_FONT_SIZES = [40, 34, 28, 24, 20, 17, 14, 12];
    // 全部字号都放不下时的兜底字号（与 COVER_FONT_SIZES 的最小值一致）。
    const COVER_MIN_FONT_SIZE = 12;

    // 估算单字宽度：中日韩等全角字符约等于字号，其余按 0.55 字号估算。
    function charWidth(character: string, fontSize: number): number {
        const code = character.codePointAt(0) ?? 0;
        const wide = (code >= 0x1100 && code <= 0x115f)
            || (code >= 0x2e80 && code <= 0xa4cf)
            || (code >= 0xac00 && code <= 0xd7a3)
            || (code >= 0xf900 && code <= 0xfaff)
            || (code >= 0xfe30 && code <= 0xfe6f)
            || (code >= 0xff00 && code <= 0xff60)
            || (code >= 0xffe0 && code <= 0xffe6);
        return wide ? fontSize : fontSize * 0.55;
    }

    // 按估算宽度贪心换行：逐字符累加，超出可用宽度就换行（中文按字断开，
    // 英文长单词也会在需要时按字符断开，避免溢出封面）。
    function wrapCoverTitle(text: string, fontSize: number): string[] {
        const maxWidth = COVER_WIDTH - COVER_PADDING_X * 2;
        const lines: string[] = [];
        let current = "";
        let width = 0;
        for (const character of Array.from(text)) {
            const nextWidth = charWidth(character, fontSize);
            if (current && width + nextWidth > maxWidth) {
                lines.push(current);
                current = "";
                width = 0;
            }
            current += character;
            width += nextWidth;
        }
        if (current) lines.push(current);
        return lines.length ? lines : [text];
    }

    // 剧本名文字封面：自动换行并挑选能放进安全区的最大字号。
    function scenarioCoverTextDataUri(title: string): string {
        const text = (title || "").trim() || "未命名剧本";
        let fontSize = COVER_MIN_FONT_SIZE;
        let lines = wrapCoverTitle(text, fontSize);
        for (const candidateSize of COVER_FONT_SIZES) {
            const candidateLines = wrapCoverTitle(text, candidateSize);
            if (candidateLines.length * candidateSize * 1.3 <= COVER_TEXT_HEIGHT) {
                fontSize = candidateSize;
                lines = candidateLines;
                break;
            }
        }

        const lineHeight = fontSize * 1.3;
        const firstBaseline = COVER_HEIGHT / 2 - ((lines.length - 1) * lineHeight) / 2;
        const tspans = lines
            .map((line, index) => `<tspan x="${COVER_WIDTH / 2}" y="${Number((firstBaseline + index * lineHeight).toFixed(1))}">${escapeXml(line)}</tspan>`)
            .join("");
        const background = `hsl(${hue(text)}, 38%, 32%)`;
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${COVER_WIDTH} ${COVER_HEIGHT}" width="${COVER_WIDTH}" height="${COVER_HEIGHT}">`
            + `<rect width="${COVER_WIDTH}" height="${COVER_HEIGHT}" fill="${background}"/>`
            + `<text text-anchor="middle" dominant-baseline="central" font-size="${fontSize}"`
            + ` font-family="system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif" fill="#f6f6f6">${tspans}</text></svg>`;
        return toDataUri(svg);
    }

    return {
        initial,
        hue,
        avatarTextDataUri,
        resolveAvatarSrc,
        scenarioCoverTextDataUri,
    };
})();
