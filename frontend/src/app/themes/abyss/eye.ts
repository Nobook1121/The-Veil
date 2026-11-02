/**
 * 「深渊帷幕」主题 · 注视之眼
 *
 * 方案调整：撤掉上一版法阵上方并排的三只小眼（它们像贴纸，且破坏构图）。
 * 现在只保留：
 * - 法阵中心的一只主体之眼：暗色眼窝 + 黄铜虹膜环 + 竖瞳，随指针移动、缓慢眨眼，
 *   法阵苏醒时瞳孔收缩、虹膜增亮；
 * - 右上方雾里极淡的一只远眼，平时几乎不可见，只在法阵脉冲时短暂被余光捕捉到。
 *
 * 关键：巩膜保持暗色，只有虹膜环发光——这样它躲在正文后面也不会影响阅读。
 */
namespace AbyssTheme {
    interface Blinker {
        /** 下一次眨眼的时间点。 */
        nextAt: number;
        /** 当前这次眨眼的起始时间，-1 表示未在眨。 */
        startAt: number;
        /** 这次眨完后是否紧接着再眨一次。 */
        doublePending: boolean;
    }

    interface Distant {
        x: number;
        y: number;
        size: number;
        alpha: number;
        blinker: Blinker;
    }

    const BLINK_DURATION = 0.55;
    const TAU = Math.PI * 2;

    const mainBlinker: Blinker = { nextAt: randomBetween(7, 14), startAt: -1, doublePending: false };
    const distantBlinker: Blinker = { nextAt: randomBetween(20, 34), startAt: -1, doublePending: false };
    const distant: Distant = { x: 0.82, y: 0.19, size: 0.026, alpha: 0.055, blinker: distantBlinker };

    /** 法阵苏醒脉冲：与 sigils.ts 的 flare 同一条曲线，眼睛据此同步呼吸。 */
    function flare(time: number): number {
        return Math.pow(Math.max(0, Math.sin(time * 0.285)), 22);
    }

    /** 返回 1 表示完全睁开、0 表示完全闭合。 */
    function openness(blinker: Blinker, time: number, minGap: number, maxGap: number): number {
        if (blinker.startAt < 0 && time >= blinker.nextAt) {
            blinker.startAt = time;
        }
        if (blinker.startAt >= 0) {
            const progress = (time - blinker.startAt) / BLINK_DURATION;
            if (progress >= 1) {
                blinker.startAt = -1;
                if (blinker.doublePending) {
                    blinker.doublePending = false;
                    blinker.nextAt = time + 0.42;
                } else {
                    blinker.nextAt = time + randomBetween(minGap, maxGap);
                    blinker.doublePending = Math.random() < 0.16;
                }
                return 1;
            }
            // 缓慢闭合、快速睁开：更像爬行动物的眼睑，而不是机械缩放。
            const p = clamp(progress, 0, 1);
            const lid = p < 0.62 ? easeOutCubic(p / 0.62) : 1 - easeOutCubic((p - 0.62) / 0.38);
            return clamp(1 - lid * 0.98, 0.02, 1);
        }
        return 1;
    }

    function traceAlmond(ctx: CanvasRenderingContext2D, r: number): void {
        ctx.beginPath();
        ctx.moveTo(-r, 0);
        ctx.quadraticCurveTo(0, -r * 0.92, r, 0);
        ctx.quadraticCurveTo(0, r * 0.92, -r, 0);
        ctx.closePath();
    }

    /**
     * 一套通用绘制：眼窝 → 暗色巩膜 → 眼睑 → 黄铜虹膜 → 竖瞳 → 高光。
     * detail 越大，虹膜条纹与角膜反光越完整；远处的眼只保留轮廓。
     */
    function paintEye(
        scene: Scene,
        cx: number,
        cy: number,
        r: number,
        alpha: number,
        open: number,
        detail: number,
        energy: number,
    ): void {
        const ctx = scene.ctx;
        ctx.save();
        ctx.translate(cx, cy);
        ctx.scale(1, Math.max(0.02, open));
        ctx.globalAlpha = clamp(alpha, 0, 1);

        // 眼窝：比周围更深的一圈，让眼睛像是从黑暗里挖出来的。
        traceAlmond(ctx, r * 1.08);
        const socket = ctx.createRadialGradient(0, 0, r * 0.1, 0, 0, r * 1.1);
        socket.addColorStop(0, "rgba(3, 2, 5, 0.9)");
        socket.addColorStop(0.55, "rgba(6, 5, 9, 0.86)");
        socket.addColorStop(1, "rgba(2, 2, 4, 0.92)");
        ctx.fillStyle = socket;
        ctx.fill();

        // 巩膜：上缘被月光照亮，下缘沉入阴影，整体保持暗色以免干扰正文。
        traceAlmond(ctx, r);
        const sclera = ctx.createLinearGradient(0, -r * 0.9, 0, r * 0.9);
        sclera.addColorStop(0, rgba(BONE, 0.46));
        sclera.addColorStop(0.42, rgba({ r: 116, g: 96, b: 92 }, 0.24));
        sclera.addColorStop(1, "rgba(10, 8, 14, 0.12)");
        ctx.fillStyle = sclera;
        ctx.fill();

        // 巩膜血丝：极细的暗红线，从眼角向虹膜方向蔓延，让眼睛像活物。
        if (detail > 0) {
            ctx.save();
            ctx.strokeStyle = rgba(OXBLOOD, 0.16);
            ctx.lineWidth = Math.max(0.7, r * 0.012);
            for (let i = 0; i < 6; i += 1) {
                const side = i % 2 === 0 ? -1 : 1;
                const startY = -r * (0.12 + i * 0.07);
                const wobble = Math.sin(i * 2.4) * r * 0.08;
                ctx.beginPath();
                ctx.moveTo(side * r * 0.9, startY);
                ctx.quadraticCurveTo(
                    side * r * 0.62,
                    startY + wobble * 0.4,
                    side * r * (0.48 + i * 0.02),
                    startY + wobble,
                );
                ctx.stroke();
            }
            ctx.restore();
        }

        // 上眼睑：一道更暗的弧，加上一道极细的黄铜眼线。
        ctx.beginPath();
        ctx.moveTo(-r * 0.96, 0);
        ctx.quadraticCurveTo(0, -r * 0.9, r * 0.96, 0);
        ctx.lineWidth = Math.max(1.6, r * 0.055);
        ctx.strokeStyle = "rgba(1, 1, 3, 0.7)";
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(-r * 0.9, -r * 0.02);
        ctx.quadraticCurveTo(0, -r * 0.84, r * 0.9, -r * 0.02);
        ctx.lineWidth = Math.max(0.8, r * 0.016);
        ctx.strokeStyle = rgba(BRASS, 0.26 + energy * 0.3);
        ctx.stroke();

        // 虹膜：内缘血红、外缘黄铜，中心是深不见底的黑。
        const irisR = r * 0.52;
        const iris = ctx.createRadialGradient(0, 0, irisR * 0.05, 0, 0, irisR);
        iris.addColorStop(0, "rgba(4, 3, 6, 0.98)");
        iris.addColorStop(0.4, "rgba(30, 15, 18, 0.96)");
        iris.addColorStop(0.66, rgba(OXBLOOD, 0.56 + energy * 0.3));
        iris.addColorStop(0.84, rgba(BRASS, 0.7 + energy * 0.28));
        iris.addColorStop(0.95, rgba(BRASS, 0.18));
        iris.addColorStop(1, rgba(BRASS, 0));
        ctx.beginPath();
        ctx.arc(0, 0, irisR, 0, TAU);
        ctx.fillStyle = iris;
        ctx.fill();

        // 角膜缘暗环：虹膜外缘再压一圈很细的深色，眼球因此有“嵌进眼眶”的深度。
        ctx.beginPath();
        ctx.arc(0, 0, irisR * 0.995, 0, TAU);
        ctx.lineWidth = Math.max(1.6, r * 0.028);
        ctx.strokeStyle = "rgba(2, 1, 4, 0.55)";
        ctx.stroke();

        if (detail > 0) {
            // 虹膜放射细丝：极慢地旋转，像冰裂纹或年轮。
            const spin = scene.time * 0.1;
            ctx.strokeStyle = rgba(BRASS, (0.1 + energy * 0.3) * detail);
            ctx.lineWidth = Math.max(0.6, r * 0.012);
            for (let i = 0; i < 22; i += 1) {
                const angle = (i / 22) * TAU + spin + Math.sin(i * 2.3) * 0.04;
                const inner = irisR * (0.4 + (i % 3) * 0.05);
                ctx.beginPath();
                ctx.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
                ctx.lineTo(Math.cos(angle) * irisR * 0.96, Math.sin(angle) * irisR * 0.96);
                ctx.stroke();
            }
        }

        // 角膜缘环：一圈略亮的黄铜边，让虹膜有明确边界。
        ctx.beginPath();
        ctx.arc(0, 0, irisR * 0.94, 0, TAU);
        ctx.lineWidth = Math.max(0.9, r * 0.018);
        ctx.strokeStyle = rgba(BRASS, 0.36 + energy * 0.4);
        ctx.stroke();

        // 虹膜领圈：靠近瞳孔的一圈细碎波浪，模仿真实虹膜的收缩沟。
        if (detail > 0) {
            ctx.beginPath();
            for (let i = 0; i <= 64; i += 1) {
                const angle = (i / 64) * TAU;
                const ripple = 0.42 + Math.sin(angle * 9.0) * 0.014 + Math.sin(angle * 17.0) * 0.007;
                const x = Math.cos(angle) * irisR * ripple;
                const y = Math.sin(angle) * irisR * ripple;
                if (i === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.lineWidth = Math.max(0.7, r * 0.011);
            ctx.strokeStyle = rgba(BRASS, 0.16 + energy * 0.24);
            ctx.stroke();
        }

        // 竖瞳：随指针偏移，瞳孔越大代表越放松；脉冲时收缩。
        const pupilX = scene.pointer.x * irisR * 0.26;
        const pupilY = scene.pointer.y * irisR * 0.22;
        const pupilTall = irisR * (0.62 - energy * 0.12);
        ctx.beginPath();
        ctx.ellipse(pupilX, pupilY, irisR * 0.26, pupilTall * 1.14, 0, 0, TAU);
        ctx.fillStyle = rgba(OXBLOOD, 0.16 + energy * 0.2);
        ctx.fill();
        ctx.beginPath();
        ctx.ellipse(pupilX, pupilY, irisR * 0.15, pupilTall, 0, 0, TAU);
        ctx.fillStyle = "rgba(0, 0, 2, 0.99)";
        ctx.fill();

        // 上眼睑压在虹膜上的阴影：让眼球有“嵌在眼眶里”的遮挡关系。
        ctx.beginPath();
        ctx.moveTo(-r * 0.85, 0);
        ctx.quadraticCurveTo(0, -r * 0.82, r * 0.85, 0);
        ctx.lineWidth = Math.max(2, r * 0.09);
        ctx.strokeStyle = "rgba(0, 0, 1, 0.26)";
        ctx.stroke();

        if (detail > 0) {
            // 湿润高光：左上角一颗很小的骨白亮点。
            ctx.beginPath();
            ctx.arc(pupilX - irisR * 0.2, pupilY - irisR * 0.3, irisR * (0.07 + energy * 0.03), 0, TAU);
            ctx.fillStyle = rgba(BONE, 0.5 + energy * 0.3);
            ctx.fill();
            // 角膜反光弧：只出现在上缘，提示眼球表面是湿的。
            ctx.beginPath();
            ctx.arc(0, 0, irisR * 1.16, Math.PI * 1.14, Math.PI * 1.58);
            ctx.lineWidth = Math.max(1, r * 0.02);
            ctx.strokeStyle = rgba(BONE, 0.14 + energy * 0.2);
            ctx.stroke();
        }
        ctx.restore();
    }

    function drawDistant(scene: Scene): void {
        const min = Math.min(scene.viewport.width, scene.viewport.height);
        const energy = flare(scene.time);
        const open = openness(distant.blinker, scene.time, 18, 46);
        // 平时几乎不可见，法阵脉冲时才短暂浮现。
        const alpha = (distant.alpha + energy * 0.12) * scene.intensity * tide(scene.time);
        const x = distant.x * scene.viewport.width + scene.pointer.x * 4;
        const y = distant.y * scene.viewport.height + scene.pointer.y * 3;
        paintEye(scene, x, y, distant.size * min, alpha, open, 0, energy);
        if (open > 0.6 && energy > 0.1) {
            drawGlow(scene.ctx, BRASS, x, y, distant.size * min * 1.5, 0.04 * energy * scene.intensity);
        }
    }

    function drawMain(scene: Scene): void {
        const ctx = scene.ctx;
        const width = scene.viewport.width;
        const height = scene.viewport.height;
        const min = Math.min(width, height);
        const r = Math.max(52, min * 0.1);
        const cx = width * 0.5 + scene.pointer.x * 8;
        const cy = height * 0.455 + scene.pointer.y * 6;
        const open = openness(mainBlinker, scene.time, 16, 44);
        const energy = flare(scene.time);

        // 主体之眼同样压低不透明度：它是从黑暗里“浮”出来的注视，而不是贴在画面上的灯泡。
        paintEye(scene, cx, cy, r, 0.6 * scene.intensity * tide(scene.time), open, 1, energy);
        drawGlow(
            ctx,
            energy > 0.2 ? OXBLOOD : BRASS,
            cx,
            cy,
            r * 1.5,
            (0.045 + energy * 0.06) * scene.intensity * Math.max(0.2, open),
        );
    }

    function draw(scene: Scene): void {
        if (scene.intensity <= 0.01) {
            return;
        }
        drawDistant(scene);
        drawMain(scene);
    }

    export const Eye = { draw };
}
