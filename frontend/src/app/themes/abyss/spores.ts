/**
 * 「深渊帷幕」主题 · 浮游光层
 *
 * 两组粒子：细小而明亮的生物荧光孢子（自下而上缓慢升起、左右摇曳、明灭闪烁），
 * 以及大而极暗的深海尘团（提供景深）。全部使用预渲染光晕贴图 + 加法混合，
 * 保证多层叠加时是“更亮”而不是“发灰”。
 */
namespace AbyssTheme {
    interface Spore {
        /** 归一化位置。 */
        x: number;
        y: number;
        radius: number;
        /** 每秒上升的归一化高度。 */
        rise: number;
        sway: number;
        phase: number;
        /** 颜色索引：0 青 / 1 金 / 2 紫。 */
        tone: number;
        alpha: number;
        depth: number;
    }

    // 光尘改用骨白 / 黄铜 / 瘀紫三色：像旧纸上浮起的尘埃，而不是霓虹粒子。
    const TONES: Rgb[] = [BONE, BRASS, PLUM];
    const SPRITES: HTMLCanvasElement[] = [
        glowSprite(BONE, 64),
        glowSprite(BRASS, 64),
        glowSprite(PLUM, 64),
    ];

    let spores: Spore[] = [];
    let dust: Spore[] = [];

    function makeSpore(deep: boolean): Spore {
        return {
            x: Math.random(),
            y: Math.random(),
            radius: deep ? randomBetween(26, 64) : randomBetween(3.5, 11),
            rise: deep ? randomBetween(0.004, 0.012) : randomBetween(0.012, 0.035),
            sway: randomBetween(0.6, 2.4),
            phase: randomBetween(0, Math.PI * 2),
            tone: randomInt(0, 2),
            alpha: deep ? randomBetween(0.022, 0.042) : randomBetween(0.1, 0.3),
            depth: randomBetween(0.3, 1),
        };
    }

    function rebuild(viewport: Viewport): void {
        const count = viewport.width < 720 ? 34 : 68;
        spores = [];
        dust = [];
        for (let i = 0; i < count; i += 1) {
            spores.push(makeSpore(false));
        }
        for (let i = 0; i < 14; i += 1) {
            dust.push(makeSpore(true));
        }
    }

    /** 水下光柱：从画面上缘斜插下来的几束幽光，让背景有纵深与体积。 */
    function drawBeams(scene: Scene): void {
        const ctx = scene.ctx;
        const width = scene.viewport.width;
        const height = scene.viewport.height;
        const t = scene.time;

        // 天顶的漫射光：水面以上的月光穿过水层，给画面顶部一个统一的亮源。
        const crown = ctx.createRadialGradient(
            width * 0.5,
            -height * 0.1,
            0,
            width * 0.5,
            -height * 0.1,
            height * 0.78,
        );
        crown.addColorStop(0, rgba(BONE, 0.075 * scene.intensity));
        crown.addColorStop(0.44, rgba(BRASS, 0.028 * scene.intensity));
        crown.addColorStop(1, rgba(BRASS, 0));
        ctx.fillStyle = crown;
        ctx.fillRect(0, 0, width, height * 0.8);

        // 每束光柱由 5 层逐渐变窄的梯形叠加而成：加法混合让中心最亮、边缘自然淡出，
        // 避免出现一道生硬的几何楔形边缘。
        const LAYERS = 5;
        for (let i = 0; i < 4; i += 1) {
            const sway = Math.sin(t * 0.17 + i * 1.9) * 0.045;
            const anchor = 0.16 + i * 0.23 + sway;
            const tilt = (i % 2 === 0 ? -1 : 1) * width * 0.05;
            const topHalf = width * 0.024;
            const bottomHalf = width * 0.085;
            const color = i === 1 ? BRASS : BONE;
            const top = anchor * width;

            for (let layer = 1; layer <= LAYERS; layer += 1) {
                const k = layer / LAYERS;
                ctx.beginPath();
                ctx.moveTo(top - topHalf * k, 0);
                ctx.lineTo(top + topHalf * k, 0);
                ctx.lineTo(top + tilt + bottomHalf * k, height);
                ctx.lineTo(top + tilt - bottomHalf * k, height);
                ctx.closePath();
                const breathe = 0.78 + tideLight(scene.time) * 0.44;
                const fade = ctx.createLinearGradient(top, 0, top + tilt, height);
                fade.addColorStop(0, rgba(color, 0.03 * scene.intensity * breathe));
                fade.addColorStop(0.55, rgba(color, 0.008 * scene.intensity * breathe));
                fade.addColorStop(1, rgba(color, 0));
                ctx.fillStyle = fade;
                ctx.fill();
            }
        }
    }

    function paint(ctx: CanvasRenderingContext2D, list: Spore[], scene: Scene): void {
        const width = scene.viewport.width;
        const height = scene.viewport.height;
        for (const spore of list) {
            spore.y -= spore.rise * scene.delta;
            if (spore.y < -0.06) {
                spore.y = 1.06;
                spore.x = Math.random();
            }
            const swayX = Math.sin(scene.time * 0.5 + spore.phase) * spore.sway * 12 * spore.depth;
            const x = spore.x * width + swayX + scene.pointer.x * 10 * spore.depth;
            const y = spore.y * height;
            const twinkle = 0.62 + 0.38 * Math.sin(scene.time * 1.8 + spore.phase * 3);
            const alpha = clamp(spore.alpha * twinkle * scene.intensity, 0, 1);
            const size = spore.radius * (1 + 0.06 * Math.sin(scene.time * 0.9 + spore.phase));
            const sprite = SPRITES[spore.tone] ?? SPRITES[0];
            if (sprite === undefined) {
                continue;
            }
            ctx.globalAlpha = alpha;
            ctx.drawImage(sprite, x - size, y - size, size * 2, size * 2);
        }
    }

    function draw(scene: Scene): void {
        if (scene.intensity <= 0.004) {
            return;
        }
        const ctx = scene.ctx;
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        drawBeams(scene);
        paint(ctx, dust, scene);
        paint(ctx, spores, scene);
        ctx.restore();
    }

    export const Spores: CanvasLayerApi = { rebuild, draw };
}
