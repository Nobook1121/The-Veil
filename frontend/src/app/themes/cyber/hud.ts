/**
 * 「赛博档案」主题 · 背景 HUD 层
 *
 * 只画“档案库的空间感”，不承担任何信息展示：
 * - 右上角的扫描雷达环：三段弧线缓慢旋转，带一条扫掠线。
 * - 缓慢下移的扫描带：约 18 秒一次，极低透明度。
 * - 散落的“档案索引卡”：细角标 + 编号，随呼吸轻微漂移。
 *
 * 全部元素刻意压到最低亮度，透过半透明面板时仍能看见，但不会与正文抢焦点。
 */
namespace CyberTheme {
    interface IndexCard {
        x: number;
        y: number;
        width: number;
        height: number;
        label: string;
        alpha: number;
        phase: number;
        driftX: number;
        driftY: number;
        rotation: number;
    }

    let cards: IndexCard[] = [];
    let viewWidth = 0;
    let viewHeight = 0;

    function rebuild(viewport: Viewport): void {
        viewWidth = viewport.width;
        viewHeight = viewport.height;
        const seeded = createSessionRandom(0x51d3);
        const narrow = viewport.width < 900;
        const count = narrow ? 3 : 7;
        cards = [];
        for (let index = 0; index < count; index += 1) {
            const width = 66 + seeded() * 86;
            const height = 24 + seeded() * 22;
            const labelHex = "0123456789ABCDEF";
            let code = "";
            for (let digit = 0; digit < 4; digit += 1) {
                code += labelHex.charAt(Math.floor(seeded() * labelHex.length));
            }
            cards.push({
                x: 40 + seeded() * Math.max(1, viewWidth - 220),
                y: 90 + seeded() * Math.max(1, viewHeight - 260),
                width,
                height,
                label: `${seeded() < 0.5 ? "CASE" : "FILE"} // ${code}`,
                alpha: 0.05 + seeded() * 0.07,
                phase: seeded() * Math.PI * 2,
                driftX: 3 + seeded() * 6,
                driftY: 4 + seeded() * 8,
                rotation: (seeded() - 0.5) * 0.05,
            });
        }
    }

    function drawRadar(scene: Scene): void {
        const ctx = scene.ctx;
        const unit = Math.min(viewportUnit(), 1.15);
        const cx = viewWidth - 96 * unit;
        const cy = 124 * unit;
        const radius = 44 * unit;
        const time = scene.time;
        const alpha = 0.12 * scene.intensity;

        ctx.save();
        ctx.translate(cx, cy);
        ctx.lineCap = "round";
        for (let ring = 0; ring < 2; ring += 1) {
            const r = radius * (0.62 + ring * 0.38);
            ctx.strokeStyle = rgba(ICE, alpha * (ring === 0 ? 0.9 : 0.55));
            ctx.lineWidth = 1;
            // 每圈拆成三段弧，随环半径反向旋转。
            for (let segment = 0; segment < 3; segment += 1) {
                const offset = time * (ring === 0 ? 0.26 : -0.18) + segment * (Math.PI * 2) / 3;
                ctx.beginPath();
                ctx.arc(0, 0, r, offset, offset + 0.62);
                ctx.stroke();
            }
        }
        // 扫掠线：从中心向外的一根细线，缓慢转一圈。
        const sweep = time * 0.34;
        const gradient = ctx.createLinearGradient(0, 0, Math.cos(sweep) * radius, Math.sin(sweep) * radius);
        gradient.addColorStop(0, rgba(PHOSPHOR, alpha * 0.8));
        gradient.addColorStop(1, rgba(PHOSPHOR, 0));
        ctx.strokeStyle = gradient;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(Math.cos(sweep) * radius, Math.sin(sweep) * radius);
        ctx.stroke();
        ctx.restore();

        // 雷达旁的一行小字，作为“终端状态”的暗示。
        ctx.save();
        ctx.font = `${9 * unit}px "Share Tech Mono", "Cascadia Mono", monospace`;
        ctx.fillStyle = rgba(PHOSPHOR, alpha * 0.9);
        ctx.textAlign = "right";
        ctx.fillText("ARCHIVE // NODE 07", cx + radius, cy + radius + 18 * unit);
        ctx.fillStyle = rgba(ICE, alpha * 0.7);
        ctx.fillText("LINK STABLE", cx + radius, cy + radius + 31 * unit);
        ctx.restore();
    }

    function viewportUnit(): number {
        return clamp(Math.min(viewWidth, viewHeight) / 900, 0.78, 1.25);
    }

    function drawCards(scene: Scene): void {
        const ctx = scene.ctx;
        const time = scene.time;
        ctx.save();
        for (const card of cards) {
            const breathe = Math.sin(time * 0.42 + card.phase);
            const x = card.x + Math.sin(time * 0.18 + card.phase) * card.driftX;
            const y = card.y + Math.cos(time * 0.14 + card.phase) * card.driftY;
            const alpha = card.alpha * (0.72 + 0.28 * (0.5 + 0.5 * breathe)) * scene.intensity;

            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(card.rotation + breathe * 0.004);
            ctx.strokeStyle = rgba(PHOSPHOR, alpha);
            ctx.lineWidth = 1;

            // 四个角标：每边只画很短的一截，形成“文件框选”的感觉。
            const corner = 7;
            ctx.beginPath();
            ctx.moveTo(0, corner);
            ctx.lineTo(0, 0);
            ctx.lineTo(corner, 0);
            ctx.moveTo(card.width - corner, 0);
            ctx.lineTo(card.width, 0);
            ctx.lineTo(card.width, corner);
            ctx.moveTo(0, card.height - corner);
            ctx.lineTo(0, card.height);
            ctx.lineTo(corner, card.height);
            ctx.moveTo(card.width - corner, card.height);
            ctx.lineTo(card.width, card.height);
            ctx.lineTo(card.width, card.height - corner);
            ctx.stroke();

            // 顶部细横线与编号，文字只做极淡的索引暗示。
            ctx.beginPath();
            ctx.moveTo(corner + 3, 3);
            ctx.lineTo(card.width * 0.46, 3);
            ctx.strokeStyle = rgba(ICE, alpha * 0.5);
            ctx.stroke();

            ctx.font = `9px "Share Tech Mono", "Cascadia Mono", monospace`;
            ctx.fillStyle = rgba(AMBER, alpha * 1.35);
            ctx.fillText(card.label, 5, card.height - 6);
            ctx.restore();
        }
        ctx.restore();
    }

    function drawScanBand(scene: Scene): void {
        const ctx = scene.ctx;
        const cycle = (scene.time * 0.055) % 1.28;
        if (cycle > 1) {
            return;
        }
        const y = cycle * viewHeight;
        const bandHeight = 2.2;
        const alpha = 0.05 * scene.intensity;
        ctx.save();
        const gradient = ctx.createLinearGradient(0, y - 26, 0, y + bandHeight);
        gradient.addColorStop(0, rgba(PHOSPHOR, 0));
        gradient.addColorStop(0.82, rgba(PHOSPHOR, alpha * 0.55));
        gradient.addColorStop(1, rgba(PHOSPHOR, alpha * 1.8));
        ctx.fillStyle = gradient;
        ctx.fillRect(0, y - 26, viewWidth, 26 + bandHeight);
        ctx.restore();
    }

    function draw(scene: Scene): void {
        drawScanBand(scene);
        drawCards(scene);
        drawRadar(scene);
    }

    export const Hud: CanvasLayerApi = { rebuild, draw };
}
