/**
 * 「赛博档案」主题 · Canvas 引擎入口
 *
 * 调度三层绘制：数据雨（rain）、背景 HUD（hud）、指针交互（fx），
 * 以及底层 WebGL 氛围（atmosphere）。
 *
 * 性能约束：DPR 上限 1.5、约 30fps、页面隐藏 / 模态遮罩 / 主题未激活时停摆、
 * 用户开启“减少动态效果”或关闭沉浸特效时完全停用。
 */
namespace CyberTheme {
    const FRAME_INTERVAL = 34;
    const MAX_FRAME_INTERVAL = 55;
    const MAX_DPR = 1.5;
    const RAMP_SECONDS = 1.5;

    let canvas: HTMLCanvasElement | null = null;
    let ctx: CanvasRenderingContext2D | null = null;
    let viewport: Viewport = { width: 0, height: 0, dpr: 1 };
    let elapsed = 0;
    let intensity = 0;
    let quality = 1;
    let frameHandle: number | null = null;
    let lastFrameAt = 0;
    let lastTimeAt = 0;
    let resizeTimer = 0;
    let frameInterval = FRAME_INTERVAL;
    let renderCost = 0;
    let backingScale = 1;
    let backingDegraded = false;
    let pointerX = 0;
    let pointerY = 0;
    let pointerInside = false;

    const pointer: Pointer = { x: 0, y: 0, inside: false };

    function overlayCovered(): boolean {
        return document.querySelector(".modal-backdrop.show") !== null;
    }

    function shouldRun(): boolean {
        return isThemeActive() && Fx.isEnabled() && !document.hidden;
    }

    function render(delta: number): void {
        if (ctx === null) {
            return;
        }
        elapsed += delta;
        intensity = Math.min(1, intensity + delta / RAMP_SECONDS);
        pointer.x = pointerX;
        pointer.y = pointerY;
        pointer.inside = pointerInside;

        const scene: Scene = {
            ctx,
            viewport,
            time: elapsed,
            delta,
            pointer,
            intensity,
            quality,
        };

        ctx.clearRect(0, 0, viewport.width, viewport.height);
        Rain.draw(scene);
        Hud.draw(scene);
        Fx.drawParticles(scene);
        Fx.updateReticle(delta);
        Fx.refreshIdle();
        Atmosphere.draw(elapsed, pointer, Fx.settings().intensity);
    }

    function loop(timestamp: number): void {
        frameHandle = window.requestAnimationFrame(loop);
        if (timestamp - lastFrameAt < frameInterval) {
            return;
        }
        lastFrameAt = timestamp;
        if (overlayCovered()) {
            lastTimeAt = timestamp;
            return;
        }
        const delta = lastTimeAt === 0 ? 0.034 : Math.min((timestamp - lastTimeAt) / 1000, 0.12);
        lastTimeAt = timestamp;
        const startedAt = performance.now();
        render(delta);
        // 自适应帧距：绘制耗时逼近帧间隔时放宽间隔，把余量让给浏览器合成。
        const cost = performance.now() - startedAt;
        renderCost = renderCost === 0 ? cost : renderCost * 0.82 + cost * 0.18;
        if (renderCost > frameInterval * 0.6 && frameInterval < MAX_FRAME_INTERVAL) {
            frameInterval = Math.min(MAX_FRAME_INTERVAL, frameInterval + 3);
        } else if (renderCost < frameInterval * 0.38 && frameInterval > FRAME_INTERVAL) {
            frameInterval = Math.max(FRAME_INTERVAL, frameInterval - 2);
        }
        if (!backingDegraded && frameInterval >= MAX_FRAME_INTERVAL && renderCost > 26 && viewport.dpr > 1.1) {
            backingDegraded = true;
            backingScale = 0.74;
            applySize();
        }
    }

    function start(): void {
        if (frameHandle !== null) {
            return;
        }
        lastFrameAt = 0;
        lastTimeAt = 0;
        renderCost = 0;
        frameHandle = window.requestAnimationFrame(loop);
    }

    function stop(): void {
        if (frameHandle !== null) {
            window.cancelAnimationFrame(frameHandle);
            frameHandle = null;
        }
        if (ctx !== null) {
            ctx.clearRect(0, 0, viewport.width, viewport.height);
        }
    }

    function applySize(): void {
        if (canvas === null || ctx === null) {
            return;
        }
        const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR) * backingScale;
        viewport = { width: window.innerWidth, height: window.innerHeight, dpr };
        canvas.width = Math.max(1, Math.round(viewport.width * dpr));
        canvas.height = Math.max(1, Math.round(viewport.height * dpr));
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        Rain.rebuild(viewport);
        Hud.rebuild(viewport);
        Atmosphere.resize(viewport);
        render(0);
    }

    function sync(): void {
        if (shouldRun()) {
            applySize();
            start();
        } else {
            stop();
            Fx.onPointerLeave();
            intensity = 0;
        }
    }

    function handleResize(): void {
        if (resizeTimer !== 0) {
            window.clearTimeout(resizeTimer);
        }
        resizeTimer = window.setTimeout(() => {
            resizeTimer = 0;
            if (shouldRun()) {
                applySize();
            }
        }, 160);
    }

    function handlePointer(event: PointerEvent): void {
        pointerX = (event.clientX / Math.max(1, window.innerWidth)) * 2 - 1;
        pointerY = (event.clientY / Math.max(1, window.innerHeight)) * 2 - 1;
        pointerInside = true;
        Fx.onPointerMove(event.clientX, event.clientY);
        Fx.markActivity();
    }

    function handlePointerDown(event: PointerEvent): void {
        Fx.onPointerDown(event.clientX, event.clientY);
        Fx.markActivity();
    }

    function handlePointerLeave(): void {
        pointerInside = false;
        pointerX = 0;
        pointerY = 0;
        Fx.onPointerLeave();
    }

    function handleSettings(): void {
        // 连续拖动滑杆时合并重建，避免每个 input 事件都重排整屏数据雨。
        if (resizeTimer !== 0) {
            window.clearTimeout(resizeTimer);
        }
        resizeTimer = window.setTimeout(() => {
            resizeTimer = 0;
            if (shouldRun()) {
                applySize();
                start();
            } else {
                stop();
            }
        }, 180);
    }

    function observe(): void {
        const observer = new MutationObserver(() => {
            sync();
        });
        observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });

        const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
        if (typeof motionQuery.addEventListener === "function") {
            motionQuery.addEventListener("change", sync);
        }

        window.addEventListener("resize", handleResize);
        window.addEventListener("pointermove", handlePointer, { passive: true });
        window.addEventListener("pointerdown", handlePointerDown, { passive: true });
        window.addEventListener("pointerleave", handlePointerLeave, { passive: true });
        window.addEventListener("cyber-fx-settings", handleSettings);
        window.addEventListener("load", sync, { once: true });

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                stop();
            } else if (shouldRun()) {
                applySize();
                start();
            }
        });
    }

    export function boot(): void {
        if (canvas !== null) {
            return;
        }
        Fx.init();
        const element = document.createElement("canvas");
        element.className = CANVAS_CLASS;
        element.setAttribute("aria-hidden", "true");
        document.body.appendChild(element);
        canvas = element;
        ctx = element.getContext("2d");
        if (ctx === null) {
            return;
        }

        const cores = navigator.hardwareConcurrency;
        quality = typeof cores === "number" && cores > 0 && cores <= 4 ? 0.72 : 1;

        Atmosphere.boot();
        observe();
        sync();

        // 主题字体加载完成后重烘焙数据雨字形，避免首帧用回退字体。
        if (typeof document.fonts !== "undefined") {
            void document.fonts.ready.then(() => {
                Rain.invalidateGlyphs();
                if (canvas !== null && shouldRun()) {
                    Rain.rebuild(viewport);
                    render(0);
                }
            });
        }
    }
}

if (document.body) {
    CyberTheme.boot();
} else {
    document.addEventListener("DOMContentLoaded", () => CyberTheme.boot(), { once: true });
}
