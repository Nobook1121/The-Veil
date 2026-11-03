/**
 * 「深渊帷幕」主题 · 仪式法阵层
 *
 * 屏幕中部一组缓慢反向旋转的符文法阵：外圈刻度、符文环、内部深渊螺旋。
 * 内部几何从上一版的七芒星改成一条手绘感的海渊螺旋，并只保留黄铜与骨白，
 * 去掉紫色几何线，画面因此不再有 HUD 般的科技感，更像刻在石上的古老仪式。
 * 由于元素数量多，三层静态几何在尺寸变化时各预渲染到离屏 Canvas，
 * 逐帧只做一次旋转 + drawImage，避免每帧重画上百个文字与线条。
 */
namespace AbyssTheme {
    interface Layer {
        canvas: HTMLCanvasElement;
        /** 该层在 CSS 像素下的绘制边长。 */
        cssSize: number;
    }

    /**
     * 法阵改用旧羊皮纸与氧化黄铜的配色，整体透明度下调，
     * 让它退成背景的一层纹样，而不是和正文抢亮度。
     */
    const TAU = Math.PI * 2;
    const RUNES = "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟᛥᛯᛰ";

    let ticksLayer: Layer | null = null;
    let runeLayer: Layer | null = null;
    let starLayer: Layer | null = null;
    let centerX = 0;
    let centerY = 0;
    let baseRadius = 0;

    /**
     * 浮动符文的精灵缓存：每帧要画 16 枚带旋转/缩放的符文，
     * 逐枚 fillText 会让浏览器反复排版与栅格化字形。烘焙成小图后
     * 每帧只做 drawImage，观感一致但开销低得多。
     */
    const runeSpriteCache = new Map<string, HTMLCanvasElement>();
    const RUNE_SPRITE_RATIO = 2;

    function runeSprite(glyph: string, size: number, tint: Rgb): HTMLCanvasElement | null {
        const rounded = Math.max(1, Math.round(size));
        const key = `${glyph}-${rounded}-${tint.r},${tint.g},${tint.b}`;
        const cached = runeSpriteCache.get(key);
        if (cached !== undefined) {
            return cached;
        }
        const pad = Math.ceil(rounded * 0.5);
        const cssSize = rounded + pad * 2;
        const sprite = document.createElement("canvas");
        sprite.width = Math.ceil(cssSize * RUNE_SPRITE_RATIO);
        sprite.height = Math.ceil(cssSize * RUNE_SPRITE_RATIO);
        const spriteCtx = sprite.getContext("2d");
        if (!spriteCtx) {
            return null;
        }
        spriteCtx.scale(RUNE_SPRITE_RATIO, RUNE_SPRITE_RATIO);
        spriteCtx.font = `${rounded}px "Cinzel", "Noto Serif SC", serif`;
        spriteCtx.textAlign = "center";
        spriteCtx.textBaseline = "middle";
        spriteCtx.fillStyle = rgba(tint, 0.92);
        spriteCtx.fillText(glyph, cssSize / 2, cssSize / 2);
        runeSpriteCache.set(key, sprite);
        return sprite;
    }

    function build(
        radius: number,
        dpr: number,
        painter: (ctx: CanvasRenderingContext2D, r: number) => void,
    ): Layer | null {
        const pad = 20;
        const cssSize = (radius + pad) * 2;
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(cssSize * dpr));
        canvas.height = canvas.width;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
            return null;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.translate(radius + pad, radius + pad);
        painter(ctx, radius);
        return { canvas, cssSize };
    }

    function rebuild(viewport: Viewport): void {
        const min = Math.min(viewport.width, viewport.height);
        // 窄屏收小法阵，避免符文环压到页头与右侧成员栏。
        baseRadius = Math.max(120, min * (viewport.width < 1000 ? 0.27 : 0.35)) * AbyssFx.settings().sigilScale;
        centerX = viewport.width * 0.5;
        centerY = viewport.height * 0.46;
        const dpr = Math.min(viewport.dpr, 2);
        // 字号随尺寸变化，旧精灵作废（字体加载完成后也会走这里重烘焙）。
        runeSpriteCache.clear();
        ticksLayer = build(baseRadius * 1.04, dpr, paintTicks);
        runeLayer = build(baseRadius * 0.9, dpr, paintRunes);
        starLayer = build(baseRadius * 0.76, dpr, paintGeometry);
    }

    function paintTicks(ctx: CanvasRenderingContext2D, r: number): void {
        ctx.strokeStyle = rgba(BRASS, 0.42);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, TAU);
        ctx.stroke();

        ctx.strokeStyle = rgba(BONE, 0.16);
        ctx.beginPath();
        ctx.arc(0, 0, r * 0.965, 0, TAU);
        ctx.stroke();

        const count = 96;
        for (let i = 0; i < count; i += 1) {
            const angle = (i / count) * TAU;
            const major = i % 8 === 0;
            const inner = r * (major ? 0.88 : 0.938);
            const outer = r * (major ? 1.05 : 0.994);
            ctx.beginPath();
            ctx.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
            ctx.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
            ctx.lineWidth = major ? 1.8 : 1;
            ctx.strokeStyle = rgba(BRASS, major ? 0.86 : 0.3);
            ctx.stroke();
        }

        // 四个方位的菱形标记，像旧地图上的罗盘刻度。
        for (let i = 0; i < 4; i += 1) {
            const angle = (i / 4) * TAU - Math.PI / 2;
            const size = r * 0.028;
            ctx.save();
            ctx.translate(Math.cos(angle) * r * 0.905, Math.sin(angle) * r * 0.905);
            ctx.rotate(angle);
            ctx.beginPath();
            ctx.moveTo(0, -size);
            ctx.lineTo(size * 1.6, 0);
            ctx.lineTo(0, size);
            ctx.lineTo(-size * 1.6, 0);
            ctx.closePath();
            ctx.lineWidth = 1.4;
            ctx.strokeStyle = rgba(BRASS, 0.58);
            ctx.stroke();
            ctx.restore();
        }
    }

    function paintRunes(ctx: CanvasRenderingContext2D, r: number): void {
        const count = 34;
        const fontSize = clamp(Math.round(r * 0.085), 9, 26);
        ctx.font = `${fontSize}px "Cinzel", "Noto Serif SC", serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        for (let i = 0; i < count; i += 1) {
            const angle = (i / count) * TAU;
            ctx.save();
            ctx.translate(Math.cos(angle) * r, Math.sin(angle) * r);
            ctx.rotate(angle + Math.PI / 2);
            ctx.fillStyle = i % 6 === 0
                ? rgba(BRASS, 0.82)
                : rgba(BONE, i % 2 === 0 ? 0.48 : 0.32);
            ctx.fillText(RUNES.charAt(i % RUNES.length), 0, 0);
            ctx.restore();
        }

        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(BONE, 0.18);
        ctx.beginPath();
        ctx.arc(0, 0, r - fontSize * 1.35, 0, TAU);
        ctx.stroke();
        ctx.strokeStyle = rgba(BRASS, 0.18);
        ctx.beginPath();
        ctx.arc(0, 0, r + fontSize * 1.35, 0, TAU);
        ctx.stroke();
    }

    /** 内部几何：一条手绘感的海渊螺旋 + 几圈断裂的刻度环。 */
    function paintGeometry(ctx: CanvasRenderingContext2D, r: number): void {
        // 三圈带缺口的细环：像石盘上被岁月磨蚀的刻线。
        const rings: number[][] = [
            [0.66, 0.2, 0.28, 1.3],
            [0.5, -0.5, 0.72, 1.1],
            [0.33, 2.4, 3.05, 1],
        ];
        for (const ring of rings) {
            const radius = ring[0] ?? 0;
            const from = ring[1] ?? 0;
            const to = ring[2] ?? 0;
            const width = ring[3] ?? 1;
            ctx.beginPath();
            ctx.arc(0, 0, r * radius, from, to);
            ctx.lineWidth = width;
            ctx.strokeStyle = rgba(BRASS, 0.3);
            ctx.stroke();
        }

        // 主螺旋：从中心孔外围起笔，向外旋出两圈半，半径带一点不规则起伏。
        const turns = 2.15;
        const steps = 220;
        ctx.beginPath();
        for (let i = 0; i <= steps; i += 1) {
            const p = i / steps;
            const angle = p * TAU * turns - Math.PI * 0.42;
            const radius = r * (0.14 + 0.46 * p) * (1 + Math.sin(angle * 2.6) * 0.018);
            const x = Math.cos(angle) * radius;
            const y = Math.sin(angle) * radius;
            if (i === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = rgba(BRASS, 0.44);
        ctx.stroke();

        // 副螺旋：暗血红的一缕，错开半圈叠在主螺旋上，像干涸的墨迹。
        ctx.beginPath();
        for (let i = 0; i <= steps; i += 1) {
            const p = i / steps;
            const angle = p * TAU * turns - Math.PI * 0.02;
            const radius = r * (0.12 + 0.44 * p) * (1 + Math.cos(angle * 3.1) * 0.02);
            const x = Math.cos(angle) * radius;
            const y = Math.sin(angle) * radius;
            if (i === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(OXBLOOD, 0.2);
        ctx.stroke();

        // 七个方位的短刻痕：让螺旋之外仍有可读的仪式结构。
        for (let i = 0; i < 7; i += 1) {
            const angle = (i / 7) * TAU + Math.PI * 0.16;
            const inner = r * 0.52;
            const outer = r * 0.63;
            ctx.beginPath();
            ctx.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
            ctx.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
            ctx.lineWidth = 1.2;
            ctx.strokeStyle = rgba(BONE, i % 2 === 0 ? 0.2 : 0.12);
            ctx.stroke();
        }
    }

    function drawLayer(
        ctx: CanvasRenderingContext2D,
        layer: Layer | null,
        rotation: number,
        alpha: number,
        scale: number,
    ): void {
        if (layer === null || alpha <= 0.004) {
            return;
        }
        ctx.save();
        ctx.translate(centerX, centerY);
        ctx.rotate(rotation);
        const size = layer.cssSize * scale;
        ctx.globalAlpha = clamp(alpha, 0, 1);
        ctx.drawImage(layer.canvas, -size / 2, -size / 2, size, size);
        ctx.restore();
    }

    /** 沿符文环快速移动的一段流光，像法阵在扫描周围。 */
    function drawSweeper(scene: Scene, flare: number): void {
        const ctx = scene.ctx;
        const radius = baseRadius * 0.9;
        const segments = 26;
        const span = Math.PI * 0.5;
        const head = scene.time * 0.55;
        ctx.save();
        ctx.translate(centerX, centerY);
        for (let i = 0; i < segments; i += 1) {
            const fade = 1 - i / segments;
            const alpha = 0.1 * fade * fade * scene.intensity * (1 + flare * 1.4);
            const from = head - (i / segments) * span;
            ctx.beginPath();
            ctx.arc(0, 0, radius, from - span / segments, from);
            ctx.lineWidth = 2;
            ctx.strokeStyle = rgba(BRASS, alpha);
            ctx.stroke();
        }
        ctx.restore();
    }

    /** 法阵背后的柔光：给画面中央一个焦点，避免大片死黑。 */
    function drawAura(scene: Scene, flare: number): void {
        const ctx = scene.ctx;
        const radius = baseRadius * 1.5 * tide(scene.time);
        const aura = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, radius);
        aura.addColorStop(0, rgba(BRASS, (0.05 + flare * 0.08) * scene.intensity));
        aura.addColorStop(0.46, rgba(PLUM, 0.03 * scene.intensity));
        aura.addColorStop(1, rgba(BRASS, 0));
        ctx.save();
        ctx.fillStyle = aura;
        ctx.fillRect(centerX - radius, centerY - radius, radius * 2, radius * 2);
        ctx.restore();
    }

    /** 法阵自发光：环带自己亮起来，中心留空把焦点让给眼睛。 */
    function drawSelfGlow(scene: Scene, flare: number): void {
        const ctx = scene.ctx;
        const radius = baseRadius * 1.18;
        const strength = (0.052 + flare * 0.095) * scene.intensity * (0.85 + tideLight(scene.time) * 0.15);
        const glow = ctx.createRadialGradient(centerX, centerY, baseRadius * 0.44, centerX, centerY, radius);
        glow.addColorStop(0, rgba(BRASS, 0));
        glow.addColorStop(0.3, rgba(BRASS, strength * 0.45));
        glow.addColorStop(0.66, rgba(BRASS, strength));
        glow.addColorStop(0.9, rgba(BRASS, strength * 0.32));
        glow.addColorStop(1, rgba(BRASS, 0));
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        ctx.fillStyle = glow;
        ctx.fillRect(centerX - radius, centerY - radius, radius * 2, radius * 2);
        ctx.restore();
    }

    /** 符文环上的一条细亮线：让法阵有清晰的轮廓，而不只是一片雾光。 */
    function drawRingHighlight(scene: Scene, flare: number): void {
        const ctx = scene.ctx;
        const radius = baseRadius * 0.9;
        ctx.save();
        ctx.globalAlpha = (0.2 + flare * 0.45) * scene.intensity * (0.9 + tideLight(scene.time) * 0.2);
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = rgba(BRASS, 0.55);
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, TAU);
        ctx.stroke();
        ctx.restore();
    }

    /**
     * 会漂浮的符文。
     *
     * 预烘焙的符文环只能整体旋转，看起来像一张贴纸；这里再叠一层独立的符文，
     * 每一枚都有各自的上下浮动、明暗呼吸与微小角度偏移，于是符文环有了
     * “刻在空气里”的体积感。数量克制，只在最内圈附近铺一圈。
     */
    function drawFloatingRunes(scene: Scene, flare: number): void {
        const ctx = scene.ctx;
        const t = scene.time;
        const count = 16;
        const fontSize = clamp(Math.round(baseRadius * 0.08), 9, 24);
        ctx.save();
        ctx.translate(centerX, centerY);
        for (let i = 0; i < count; i += 1) {
            // 黄金角错开相位，符文不会像节拍器一样整齐地一起呼吸。
            const phase = i * 2.399;
            const angle = (i / count) * TAU - t * 0.021 + Math.sin(t * 0.18 + phase) * 0.014;
            const bob = Math.sin(t * 0.7 + phase) * baseRadius * 0.024;
            // 半径按三档错开，符文不再钉在同一条圆周上，而像一层散在空气里的铭文。
            const radius = baseRadius * (0.9 + ((i % 3) - 1) * 0.05) + bob;
            const x = Math.cos(angle) * radius;
            const y = Math.sin(angle) * radius;
            const flicker = 0.5 + 0.5 * Math.sin(t * 1.15 + phase * 1.7);
            const lit = i % 5 === 0;
            if (lit) {
                drawGlow(
                    ctx,
                    BRASS,
                    x,
                    y,
                    fontSize * 1.1,
                    clamp(0.1 * flicker * scene.intensity * (1 + flare), 0, 1),
                );
            }
            const glyph = RUNES.charAt((i * 7) % RUNES.length);
            const sprite = runeSprite(glyph, fontSize, lit ? BRASS : BONE);
            if (sprite === null) {
                continue;
            }
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(angle + Math.PI / 2 + Math.sin(t * 0.6 + phase) * 0.05);
            // 字号也轻微错开：有的像远处刻深的大符，有的像近处的小注记。
            const scale = 0.84 + (i % 4) * 0.07;
            ctx.globalAlpha = clamp(
                (lit ? 0.62 : 0.3) * (0.55 + flicker * 0.45) * scene.intensity * (1 + flare * 0.85),
                0,
                1,
            );
            // 精灵已按不透明字色烘焙，未点亮的符文用整体透明度压暗。
            if (!lit) {
                ctx.globalAlpha = clamp(ctx.globalAlpha * 0.78, 0, 1);
            }
            const drawSize = (sprite.width / RUNE_SPRITE_RATIO) * scale;
            ctx.drawImage(sprite, -drawSize * 0.5, -drawSize * 0.5, drawSize, drawSize);
            ctx.restore();
        }
        ctx.restore();
    }

    function draw(scene: Scene): void {
        if (scene.intensity <= 0.005 || baseRadius <= 0) {
            return;
        }
        const ctx = scene.ctx;
        const t = scene.time;
        // 与触手、眼睛共用同一条潮汐节拍，画面才像同一个场景在呼吸。
        const pulse = tide(t);
        // 每约 22 秒一次的“法阵苏醒”：短暂而克制的亮度峰值。
        const flare = Math.pow(Math.max(0, Math.sin(t * 0.285)), 22);
        const base = 0.42 * scene.intensity * (0.9 + tideLight(t) * 0.1);

        drawAura(scene, flare);
        // 自发光放在几何之前：先把环带「点亮」，再让刻度与符文压在上面，
        // 于是铭文像是被自己发出的光烘亮的，而不是贴着一张发光的纸。
        drawSelfGlow(scene, flare);
        drawLayer(ctx, ticksLayer, t * 0.028, base * (0.86 + flare * 0.7), pulse);
        drawLayer(ctx, runeLayer, -t * 0.021, base * (1.05 + flare * 0.5), pulse * 0.99);
        drawLayer(ctx, starLayer, t * 0.011, base * (0.9 + flare * 0.4), pulse);
        drawRingHighlight(scene, flare);
        // 浮动符文是法阵的「呼吸」：脱离环面轻轻起伏，让整体不再是一张静态的图。
        drawFloatingRunes(scene, flare);
        drawSweeper(scene, flare);
    }

    export const Sigils: CanvasLayerApi = { rebuild, draw };
}
