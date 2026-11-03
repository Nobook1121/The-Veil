/**
 * 「赛博档案」主题 · 分层数据雨
 *
 * 与旧版「矩阵字母雨」的区别：
 * - 三层景深（远 / 中 / 近）：字号、速度、亮度分开，画面有纵深而不是一整片同样的字。
 * - 字符池混入少量中文档案术语（檔案 / 數據 / 終端 / 協議…），让雨读起来像“档案索引”而非纯代码。
 * - 少量琥珀色数据包混在青绿色雨里，呼应归档标记。
 * - 字形烘焙成精灵缓存，逐帧只做 drawImage，避免反复 fillText 造成主线程抖动。
 */
namespace CyberTheme {
    interface Drop {
        x: number;
        y: number;
        speed: number;
        trail: number;
        glyphs: string[];
        lastGlyphY: number;
        layer: number;
        warm: boolean;
    }

    const GLYPH_POOLS = [
        { pool: "01", weight: 0.30 },
        { pool: "ABCDEF0123456789", weight: 0.22 },
        { pool: "abcdefghijklmnopqrstuvwxyz", weight: 0.14 },
        { pool: "#@%&*!?:;<>[]{}", weight: 0.12 },
        { pool: "檔案數據終端協議監控權限存取解密歸檔索引節點", weight: 0.22 },
    ];

    /** 下落字符集预设：单选后整屏数据雨都只使用对应字符。 */
    const CHARSET_POOLS: Record<string, Array<{ pool: string; weight: number }>> = {
        katakana: [{ pool: "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ", weight: 1 }],
        symbols: [{ pool: "#@%&*!?:;<>[]{}/\\|+=-_~^", weight: 1 }],
        hex: [{ pool: "0123456789ABCDEF", weight: 1 }],
        binary: [{ pool: "01", weight: 1 }],
        mixed: GLYPH_POOLS,
    };

    let activeCharset = "mixed";

    /** 三层景深参数：远层小而暗、近层大而亮。 */
    const LAYERS = [
        { spacing: 62, size: 10, alpha: 0.30, speed: 0.62, trailMin: 10, trailMax: 20 },
        { spacing: 92, size: 13, alpha: 0.44, speed: 0.95, trailMin: 9, trailMax: 18 },
        { spacing: 148, size: 16, alpha: 0.6, speed: 1.3, trailMin: 7, trailMax: 14 },
    ];

    const GLYPH_STEP = 5.5;
    const SPRITE_RATIO = 2;
    const spriteCache = new Map<string, HTMLCanvasElement>();

    let drops: Drop[] = [];
    let viewWidth = 0;
    let viewHeight = 0;

    function pickGlyph(): string {
        const pools = CHARSET_POOLS[activeCharset] || GLYPH_POOLS;
        let roll = Math.random();
        for (const entry of pools) {
            if (roll < entry.weight) {
                return entry.pool.charAt(Math.floor(Math.random() * entry.pool.length));
            }
            roll -= entry.weight;
        }
        return "0";
    }

    function glyphSprite(glyph: string, size: number, rgb: Rgb): HTMLCanvasElement | null {
        const key = `${glyph}-${size}-${rgb.r},${rgb.g},${rgb.b}`;
        const cached = spriteCache.get(key);
        if (cached !== undefined) {
            return cached;
        }
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(size * 1.4 * SPRITE_RATIO);
        canvas.height = Math.ceil(size * 1.4 * SPRITE_RATIO);
        const ctx = canvas.getContext("2d");
        if (ctx === null) {
            return null;
        }
        ctx.scale(SPRITE_RATIO, SPRITE_RATIO);
        ctx.font = `500 ${size}px "Share Tech Mono", "Cascadia Mono", monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = rgba(rgb, 1);
        ctx.fillText(glyph, canvas.width / SPRITE_RATIO / 2, canvas.height / SPRITE_RATIO / 2);
        spriteCache.set(key, canvas);
        return canvas;
    }

    function createDrop(x: number, lambda: number, initial: boolean): Drop {
        const layer = LAYERS[lambda];
        if (layer === undefined) {
            throw new Error("invalid rain layer");
        }
        const trail = randomInt(layer.trailMin, layer.trailMax);
        const glyphs: string[] = [];
        for (let index = 0; index < trail; index += 1) {
            glyphs.push(pickGlyph());
        }
        const y = initial
            ? -Math.random() * viewHeight * 0.6
            : -trail * layer.size - Math.random() * viewHeight * 0.35;
        return {
            x,
            y,
            speed: layer.speed * randomBetween(34, 78) * clamp(Fx.settings().rainSpeed, 0.5, 2),
            trail,
            glyphs,
            lastGlyphY: y,
            layer: lambda,
            // 约 7% 的列是琥珀色数据包，数量克制，只做点缀。
            warm: Math.random() < 0.07,
        };
    }

    function rebuild(viewport: Viewport): void {
        viewWidth = viewport.width;
        viewHeight = viewport.height;
        const density = clamp(Fx.settings().rainDensity, 0.3, 1);
        const nextCharset = Fx.settings().charset || "mixed";
        if (activeCharset !== nextCharset) {
            activeCharset = nextCharset;
            // 字符集变了才丢精灵缓存；普通 resize 不必重新烘焙。
            spriteCache.clear();
        }
        const seeded = createSessionRandom(0xc1be);
        drops = [];

        for (let lambda = 0; lambda < LAYERS.length; lambda += 1) {
            const layer = LAYERS[lambda];
            if (layer === undefined) continue;
            const spacing = layer.spacing / density;
            for (let x = spacing * 0.5; x < viewWidth; x += spacing) {
                // 稀疏抖动：避免所有列排成严格的等距栅格。
                const offset = (seeded() - 0.5) * spacing * 0.42;
                const column = x + offset;
                if (column < 4 || column > viewWidth - 4) {
                    continue;
                }
                if (seeded() < 0.12) {
                    continue;
                }
                drops.push(createDrop(column, lambda, true));
            }
        }
    }

    function draw(scene: Scene): void {
        if (drops.length === 0) {
            return;
        }
        const ctx = scene.ctx;
        ctx.save();
        // 远层单独画在最下面，近层最后画，形成深度叠压。
        for (let lambda = 0; lambda < LAYERS.length; lambda += 1) {
            const layer = LAYERS[lambda];
            if (layer === undefined) continue;
            for (const drop of drops) {
                if (drop.layer !== lambda) {
                    continue;
                }
                drop.y += drop.speed * scene.delta;
                if (drop.y - drop.lastGlyphY >= GLYPH_STEP) {
                    drop.lastGlyphY = drop.y;
                    drop.glyphs.unshift(pickGlyph());
                    drop.glyphs.length = drop.trail;
                }

                const layerAlpha = layer.alpha * scene.intensity;
                const tint = drop.warm ? AMBER : PHOSPHOR;
                const head = drop.glyphs[0] ?? "0";
                const headSprite = glyphSprite(head, layer.size + 2, tint);
                if (headSprite !== null) {
                    ctx.globalAlpha = clamp(layerAlpha + 0.24, 0, 1);
                    ctx.drawImage(
                        headSprite,
                        drop.x - headSprite.width / SPRITE_RATIO / 2,
                        drop.y - headSprite.height / SPRITE_RATIO / 2,
                        headSprite.width / SPRITE_RATIO,
                        headSprite.height / SPRITE_RATIO,
                    );
                }
                // 近层头部加一点光晕，远层不加，避免整片发亮。
                if (lambda === LAYERS.length - 1) {
                    drawGlow(ctx, tint, drop.x, drop.y + layer.size * 0.4, layer.size * 1.7, layerAlpha * 0.42);
                }

                ctx.globalAlpha = 1;
                for (let index = 1; index < drop.glyphs.length; index += 1) {
                    const glyph = drop.glyphs[index];
                    if (glyph === undefined) continue;
                    const y = drop.y - index * layer.size;
                    if (y < -layer.size || y > viewHeight + layer.size) continue;
                    const sprite = glyphSprite(glyph, layer.size, tint);
                    if (sprite === null) continue;
                    const fade = 1 - index / drop.glyphs.length;
                    ctx.globalAlpha = layerAlpha * fade * fade * 0.9;
                    ctx.drawImage(
                        sprite,
                        drop.x - sprite.width / SPRITE_RATIO / 2,
                        y - sprite.height / SPRITE_RATIO / 2,
                        sprite.width / SPRITE_RATIO,
                        sprite.height / SPRITE_RATIO,
                    );
                }

                if (drop.y - drop.trail * layer.size > viewHeight) {
                    Object.assign(drop, createDrop(drop.x, drop.layer, false));
                }
            }
        }
        ctx.restore();
    }

    /** 主题字体异步加载完成后重建字形精灵，避免首帧用回退字体烘焙。 */
    function invalidateGlyphs(): void {
        spriteCache.clear();
    }

    export const Rain: CanvasLayerApi & {
        rebuildLayer(viewport: Viewport): void;
        invalidateGlyphs(): void;
    } = {
        rebuild,
        rebuildLayer: rebuild,
        invalidateGlyphs,
        draw,
    };
}
