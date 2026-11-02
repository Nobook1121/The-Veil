/**
 * 「深渊帷幕」主题 · 沉没的建筑层
 *
 * 只做三件事，却决定了整幅画面的“空间感”：
 * - 画面上缘的肋拱（一座沉入水下的教堂穹顶残骸）；
 * - 法阵背后巨大的尖拱石门轮廓，把视线收束到中央；
 * - 底部的断崖与礁石剪影，压住画面下缘，藏起触手的根部。
 *
 * 全部几何在尺寸变化时一次性烘焙到离屏 Canvas，逐帧只做一次带视差的 drawImage，
 * 因此不会增加每帧成本，也不会和近景触手抢细节。
 */
namespace AbyssTheme {
    interface Layer {
        canvas: HTMLCanvasElement;
        /** 离屏画布四周留出的可视差边距（CSS 像素）。 */
        pad: number;
        cssWidth: number;
        cssHeight: number;
    }

    const PAD = 36;
    const TAU = Math.PI * 2;
    /** 瞳孔用的近黑色：比背景再深一点，眼球才有“洞”的感觉。 */
    const EYE_VOID: Rgb = { r: 3, g: 2, b: 5 };

    let layer: Layer | null = null;

    function rebuild(viewport: Viewport): void {
        const cssWidth = viewport.width + PAD * 2;
        const cssHeight = viewport.height + PAD * 2;
        const dpr = Math.min(viewport.dpr, 2);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(cssWidth * dpr));
        canvas.height = Math.max(1, Math.round(cssHeight * dpr));
        const ctx = canvas.getContext("2d");
        if (!ctx) {
            layer = null;
            return;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.translate(PAD, PAD);
        paint(ctx, viewport.width, viewport.height);
        layer = { canvas, pad: PAD, cssWidth, cssHeight };
    }

    function paint(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        // 由远到近：深海微光 → 远触手 → 巨物剪影 → 肋拱与石门 → 悬链 → 断柱 → 海床。
        paintDeepWater(ctx, width, height);
        paintFarTentacles(ctx, width, height);
        paintColossus(ctx, width, height);
        paintVault(ctx, width, height);
        paintGate(ctx, width, height);
        paintChains(ctx, width, height);
        paintPillars(ctx, width, height);
        paintLedge(ctx, width, height);
        paintSmallEyes(ctx, width, height);
    }

    /**
     * 散落在底部暗处的小眼睛。
     *
     * 它们在画面里只是“纹样”而不是角色：尺寸、开合程度、倾斜角、亮度
     * 全部不一致，左右也不镜像。有几只只露出半条眼睑的弧线，像是刚睁开
     * 又闭上；这样底部就不会只是一片黑色海床，而是有东西在暗处看着。
     * 全部在烘焙时画进离屏层，逐帧零成本。
     */
    function paintSmallEyes(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const min = Math.min(width, height);
        const count = 13;
        for (let i = 0; i < count; i += 1) {
            const sideBias = i % 2 === 0 ? 1 : -1;
            // 只铺在底部两侧：正中央留给法阵与眼睛，纹样不和焦点抢位置。
            const x = width * clamp(0.5 + sideBias * randomBetween(0.16, 0.45), 0.03, 0.97);
            const y = height * randomBetween(0.62, 0.92);
            const size = min * randomBetween(0.009, 0.03);
            // 横向半径再单独抖一次，避免每只眼睛都是一个等比例的椭圆。
            const wide = randomBetween(0.86, 1.2);
            // 开合程度：小值只剩一条眼缝，大值才是完整的眼。
            const open = randomBetween(0.45, 1);
            const tilt = randomBetween(-0.6, 0.6);
            const alpha = randomBetween(0.035, 0.095);

            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(tilt);

            // 眼睑：浅色椭圆，只比周围亮一点点。
            ctx.beginPath();
            ctx.ellipse(0, 0, size * wide, size * open * 0.62, 0, 0, TAU);
            ctx.fillStyle = rgba(BONE, alpha);
            ctx.fill();
            // 眼窝外圈一层暗色，避免椭圆像贴上去的贴纸。
            ctx.lineWidth = Math.max(1, size * 0.12);
            ctx.strokeStyle = rgba(BONE, alpha * 0.55);
            ctx.stroke();

            if (open < 0.62) {
                // 半睁：只留一条眼缝和一线冷光。
                ctx.beginPath();
                ctx.ellipse(0, 0, size * 0.96 * wide, size * 0.1, 0, 0, TAU);
                ctx.fillStyle = rgba(EYE_VOID, alpha * 4.2);
                ctx.fill();
            } else {
                // 虹膜：偏黄铜或暗血红，随机取一种。
                const irisR = size * randomBetween(0.34, 0.46);
                ctx.beginPath();
                ctx.arc(randomBetween(-size * 0.12, size * 0.12), 0, irisR, 0, TAU);
                ctx.fillStyle = i % 3 === 0 ? rgba(OXBLOOD, alpha * 5) : rgba(BRASS, alpha * 4.6);
                ctx.fill();
                // 竖瞳。
                ctx.beginPath();
                ctx.ellipse(0, 0, irisR * 0.18, irisR * 0.82, 0, 0, TAU);
                ctx.fillStyle = rgba(EYE_VOID, alpha * 9);
                ctx.fill();
                // 一点湿润高光。
                ctx.beginPath();
                ctx.arc(-irisR * 0.32, -irisR * 0.34, Math.max(0.8, irisR * 0.16), 0, TAU);
                ctx.fillStyle = rgba(BONE, alpha * 4.5);
                ctx.fill();
            }
            ctx.restore();
        }
    }

    /** 远处的一层水下微光：让深渊有“看不见底”的纵深，而不是一块纯黑。 */
    function paintDeepWater(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const glow = ctx.createLinearGradient(0, height * 0.42, 0, height * 0.9);
        glow.addColorStop(0, "rgba(62, 48, 62, 0)");
        glow.addColorStop(0.55, "rgba(62, 48, 62, 0.05)");
        glow.addColorStop(1, "rgba(40, 30, 42, 0.01)");
        ctx.fillStyle = glow;
        ctx.fillRect(0, height * 0.42, width, height * 0.48);
    }

    /**
     * 雾里更远处的几条触手。
     *
     * 水下的远物不会更黑，而是被散射光洗得更浅、更糊——所以这里用比近景更亮的
     * 灰紫色剪影，并在烘焙时做一次高斯模糊。它们与近景触手、巨物构成三层纵深，
     * 画面因此有“巢穴深不见底”的空间，而不是贴在墙上的一层花纹。
     */
    function paintFarTentacles(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const min = Math.min(width, height);
        const arms: number[][] = [
            // [起点x, 起点y, 长度, 起始角度, 弯曲, 根部半径比例, 透明度]
            [0.06, 0.62, 0.52, -0.62, 0.7, 0.045, 0.3],
            [0.94, 0.58, 0.56, -2.5, -0.8, 0.05, 0.32],
            [0.3, -0.08, 0.44, 1.32, 0.55, 0.038, 0.24],
            [0.76, -0.06, 0.4, 1.9, -0.5, 0.034, 0.22],
        ];
        ctx.save();
        // 只在这一次烘焙里使用模糊：逐帧没有额外开销。
        ctx.filter = "blur(9px)";
        for (const arm of arms) {
            const sx = arm[0] ?? 0;
            const sy = arm[1] ?? 0;
            const len = arm[2] ?? 0;
            const angle = arm[3] ?? 0;
            const bend = arm[4] ?? 0;
            const radius = arm[5] ?? 0.04;
            const alpha = arm[6] ?? 0.25;
            const steps = 26;
            ctx.beginPath();
            for (let i = 0; i <= steps; i += 1) {
                const p = i / steps;
                const t = angle + bend * p * 1.6;
                const x = width * sx + Math.cos(t) * len * min * p * 0.92;
                const y = height * sy + Math.sin(t) * len * min * p * 0.92;
                const r = Math.max(1, min * radius * Math.pow(1 - p, 0.85));
                ctx.arc(x, y, r, 0, TAU);
            }
            ctx.fillStyle = `rgba(58, 46, 54, ${alpha})`;
            ctx.fill();
        }
        ctx.restore();
    }

    /**
     * 沉睡在石门后的巨物：只给一团轮廓与几道纵向沟纹，
     * 既补足画面的“有人比你大得多”的压迫感，又不会喧宾夺主。
     */
    function paintColossus(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const cx = width * 0.5;
        const baseY = height * 1.02;
        const headR = Math.min(width * 0.155, height * 0.26);
        const shoulderR = Math.min(width * 0.4, height * 0.52);

        // 肩部：一座半沉在水下的巨大雕塑或生物躯体。
        ctx.beginPath();
        ctx.ellipse(cx, baseY + height * 0.14, shoulderR, height * 0.3, 0, 0, TAU);
        const shoulder = ctx.createRadialGradient(cx, baseY, 0, cx, baseY, shoulderR);
        shoulder.addColorStop(0, "rgba(3, 3, 7, 0.42)");
        shoulder.addColorStop(0.7, "rgba(3, 3, 7, 0.24)");
        shoulder.addColorStop(1, "rgba(3, 3, 7, 0)");
        ctx.fillStyle = shoulder;
        ctx.fill();

        // 头部：介于兜帽与头骨之间的圆顶轮廓，顶缘有一线极淡的冷光。
        ctx.beginPath();
        ctx.ellipse(cx, baseY - height * 0.07, headR, headR * 1.22, 0, 0, TAU);
        ctx.fillStyle = "rgba(3, 3, 7, 0.36)";
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(cx, baseY - height * 0.07, headR, headR * 1.22, 0, Math.PI * 1.02, Math.PI * 1.98);
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = rgba(BONE, 0.05);
        ctx.stroke();

        // 纵向沟纹：像石像的衣褶，也像鳃裂。
        for (let i = -2; i <= 2; i += 1) {
            const offset = i * headR * 0.34;
            ctx.beginPath();
            ctx.moveTo(cx + offset, baseY - height * 0.07 - headR * 0.72);
            ctx.quadraticCurveTo(
                cx + offset * 1.3,
                baseY - height * 0.05,
                cx + offset * 1.14,
                baseY + height * 0.02,
            );
            ctx.lineWidth = 1;
            ctx.strokeStyle = rgba(BRASS, 0.028);
            ctx.stroke();
        }
    }

    /** 上缘肋拱：从画面外的一个消失点发散下来的几道浅拱。 */
    function paintVault(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const vx = width * 0.5;
        const vy = -height * 0.62;
        const start = Math.PI * 0.06;
        const end = Math.PI * 0.94;

        for (let i = 0; i < 6; i += 1) {
            const radius = height * (0.72 + i * 0.105);
            const alpha = 0.125 - i * 0.012;
            ctx.beginPath();
            ctx.arc(vx, vy, radius, start, end);
            ctx.lineWidth = i === 0 ? 1.8 : 1;
            ctx.strokeStyle = rgba(BRASS, Math.max(0.02, alpha));
            ctx.stroke();
        }

        // 放射状的肋：像哥特穹顶的棱线，只在画面顶部留下淡淡的一段。
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(BRASS, 0.04);
        for (let i = 0; i <= 8; i += 1) {
            const angle = Math.PI * (0.12 + i * 0.095);
            const r1 = height * 0.55;
            const r2 = height * 1.55;
            ctx.beginPath();
            ctx.moveTo(vx + Math.cos(angle) * r1, vy + Math.sin(angle) * r1);
            ctx.lineTo(vx + Math.cos(angle) * r2, vy + Math.sin(angle) * r2);
            ctx.stroke();
        }
    }

    /** 法阵背后的尖拱石门：几乎不可见，但把视线收束到中央。 */
    function paintGate(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const ax = width * 0.5;
        const half = Math.min(width * 0.27, height * 0.4);
        const bottom = height * 1.06;
        const shoulder = height * 0.36;

        // 石门内部的一层更深的凹影：让门洞读起来是“向内退”的，而不是一条线。
        const hollow = ctx.createLinearGradient(0, height * 0.12, 0, height);
        hollow.addColorStop(0, "rgba(2, 2, 5, 0.14)");
        hollow.addColorStop(0.6, "rgba(2, 2, 5, 0.08)");
        hollow.addColorStop(1, "rgba(2, 2, 5, 0)");
        ctx.beginPath();
        ctx.moveTo(ax - half * 0.94, bottom);
        ctx.lineTo(ax - half * 0.94, shoulder);
        ctx.quadraticCurveTo(ax - half * 0.86, height * 0.18, ax, height * 0.12);
        ctx.quadraticCurveTo(ax + half * 0.86, height * 0.18, ax + half * 0.94, shoulder);
        ctx.lineTo(ax + half * 0.94, bottom);
        ctx.closePath();
        ctx.fillStyle = hollow;
        ctx.fill();

        ctx.beginPath();
        ctx.moveTo(ax - half, bottom);
        ctx.lineTo(ax - half, shoulder);
        ctx.quadraticCurveTo(ax - half * 0.92, height * 0.17, ax, height * 0.11);
        ctx.quadraticCurveTo(ax + half * 0.92, height * 0.17, ax + half, shoulder);
        ctx.lineTo(ax + half, bottom);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = rgba(BRASS, 0.13);
        ctx.stroke();

        // 内圈再收一道，暗示石门的厚度。
        ctx.beginPath();
        ctx.moveTo(ax - half * 0.9, bottom);
        ctx.lineTo(ax - half * 0.9, shoulder + height * 0.02);
        ctx.quadraticCurveTo(ax - half * 0.82, height * 0.21, ax, height * 0.155);
        ctx.quadraticCurveTo(ax + half * 0.82, height * 0.21, ax + half * 0.9, shoulder + height * 0.02);
        ctx.lineTo(ax + half * 0.9, bottom);
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(PLUM, 0.05);
        ctx.stroke();

        // 门楣上的刻痕：一排极淡的符文，只有凑近才看得见。
        const runes = "ᚠᚢᚦᚨᚱᚲᚷᚹ";
        ctx.font = `${Math.max(9, Math.round(height * 0.016))}px "Cinzel", "Noto Serif SC", serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = rgba(BONE, 0.07);
        for (let i = 0; i < 8; i += 1) {
            const spanAngle = Math.PI * (1.08 + (i / 7) * 0.84);
            const rx = half * 0.94;
            const ry = height * 0.29;
            const x = ax + Math.cos(spanAngle) * rx;
            const y = height * 0.23 + Math.sin(spanAngle) * ry * 0.28;
            ctx.fillText(runes.charAt(i), x, y);
        }
    }

    /** 从上缘垂下的两条锁链：给画面加一层“人造物”的垂直节奏。 */
    function paintChains(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const anchors = [0.355, 0.645];
        for (const anchor of anchors) {
            const x = width * anchor;
            const length = height * 0.2;
            const links = 15;
            for (let i = 0; i < links; i += 1) {
                const y = (i / links) * length;
                const sway = Math.sin(i * 0.55 + anchor * 12) * 1.6;
                const rx = 3.1;
                const ry = 5.2;
                ctx.save();
                ctx.translate(x + sway, y);
                ctx.rotate(i % 2 === 0 ? 0 : Math.PI / 2);
                ctx.beginPath();
                ctx.ellipse(0, 0, rx, ry, 0, 0, TAU);
                ctx.lineWidth = 1.1;
                ctx.strokeStyle = rgba(BRASS, 0.12);
                ctx.stroke();
                ctx.restore();
            }
        }
    }

    /** 两根半没入淤泥的断柱：中景的垂直体块，衬托触手的柔软。 */
    function paintPillars(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const drawColumn = (cx: number, top: number, w: number, h: number, tilt: number): void => {
            ctx.save();
            ctx.translate(cx, top);
            ctx.rotate(tilt);
            ctx.beginPath();
            ctx.moveTo(-w * 0.5, 0);
            ctx.lineTo(w * 0.5, 0);
            ctx.lineTo(w * 0.42, h);
            ctx.lineTo(-w * 0.42, h);
            ctx.closePath();
            const body = ctx.createLinearGradient(-w * 0.5, 0, w * 0.5, 0);
            body.addColorStop(0, "rgba(16, 13, 20, 0.85)");
            body.addColorStop(0.55, "rgba(10, 9, 14, 0.9)");
            body.addColorStop(1, "rgba(4, 4, 8, 0.95)");
            ctx.fillStyle = body;
            ctx.fill();
            // 柱顶断面的一点边光。
            ctx.beginPath();
            ctx.moveTo(-w * 0.5, 0);
            ctx.lineTo(w * 0.5, 0);
            ctx.lineWidth = 1.2;
            ctx.strokeStyle = rgba(BONE, 0.07);
            ctx.stroke();
            // 柱身的纵向凹槽。
            for (let i = -1; i <= 1; i += 1) {
                ctx.beginPath();
                ctx.moveTo(i * w * 0.3, h * 0.12);
                ctx.lineTo(i * w * 0.26, h * 0.92);
                ctx.lineWidth = 1;
                ctx.strokeStyle = "rgba(0, 0, 0, 0.35)";
                ctx.stroke();
            }
            ctx.restore();
        };
        drawColumn(width * 0.155, height * 0.68, width * 0.028, height * 0.26, -0.06);
        drawColumn(width * 0.845, height * 0.7, width * 0.026, height * 0.24, 0.05);
    }

    /** 底部断崖：不规则的海床剪影，压住下缘并藏住触手根部。 */
    function paintLedge(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        const top = height * 0.885;
        ctx.beginPath();
        ctx.moveTo(-PAD, height + PAD);
        ctx.lineTo(-PAD, top + height * 0.02);
        // 一条起伏很小的海岸线，避免变成生硬的水平色块。
        const steps = 9;
        for (let i = 0; i <= steps; i += 1) {
            const x = (width * i) / steps;
            const y = top + Math.sin(i * 1.7 + 0.6) * height * 0.016 + Math.sin(i * 3.1) * height * 0.006;
            ctx.lineTo(x, y);
        }
        ctx.lineTo(width + PAD, height + PAD);
        ctx.closePath();
        const fill = ctx.createLinearGradient(0, top - height * 0.04, 0, height + PAD);
        fill.addColorStop(0, "rgba(6, 5, 10, 0)");
        fill.addColorStop(0.4, "rgba(4, 4, 8, 0.55)");
        fill.addColorStop(1, "rgba(2, 2, 4, 0.9)");
        ctx.fillStyle = fill;
        ctx.fill();

        // 断崖顶缘的一点冷光，让它读作岩石而不是黑色缺口。
        ctx.beginPath();
        ctx.moveTo(-PAD, top + height * 0.02);
        for (let i = 0; i <= steps; i += 1) {
            const x = (width * i) / steps;
            const y = top + Math.sin(i * 1.7 + 0.6) * height * 0.016 + Math.sin(i * 3.1) * height * 0.006;
            ctx.lineTo(x, y);
        }
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(BONE, 0.035);
        ctx.stroke();

        // 左右各一块更近的礁石，形成前景遮挡。
        paintBoulder(ctx, width * 0.035, height * 0.965, width * 0.1, height * 0.075);
        paintBoulder(ctx, width * 0.955, height * 0.975, width * 0.085, height * 0.065);
    }

    function paintBoulder(
        ctx: CanvasRenderingContext2D,
        cx: number,
        cy: number,
        rx: number,
        ry: number,
    ): void {
        ctx.beginPath();
        ctx.ellipse(cx, cy, rx, ry, 0.1, 0, TAU);
        ctx.fillStyle = "rgba(2, 2, 4, 0.92)";
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(cx - rx * 0.12, cy - ry * 0.4, rx * 0.72, ry * 0.22, -0.16, Math.PI * 0.92, Math.PI * 1.95);
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgba(BONE, 0.05);
        ctx.stroke();
    }

    function draw(scene: Scene): void {
        if (layer === null || scene.intensity <= 0.004) {
            return;
        }
        const ctx = scene.ctx;
        // 远景视差与镜头反向：指针右移，建筑略微左移，纵深被拉开。
        const offsetX = -scene.pointer.x * 7;
        const offsetY = -scene.pointer.y * 4;
        ctx.save();
        ctx.globalAlpha = clamp(scene.intensity * 0.95, 0, 1);
        ctx.drawImage(
            layer.canvas,
            -layer.pad + offsetX,
            -layer.pad + offsetY,
            layer.cssWidth,
            layer.cssHeight,
        );
        ctx.restore();
    }

    export const Environment: CanvasLayerApi = { rebuild, draw };
}
