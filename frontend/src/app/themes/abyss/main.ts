/**
 * 「深渊帷幕」主题 · Canvas 引擎入口
 *
 * 负责创建背景 Canvas、调度各绘制层、节流帧率、处理尺寸变化与主题切换。
 * 性能约束：DPR 上限 1.5、约 30fps、页面隐藏时停机、主题未激活时停机、
 * 用户开启“减少动态效果”时完全停用。
 */
namespace AbyssTheme {
    /** 基础帧间隔（约 30fps）；慢机会自动放宽到 MAX_FRAME_INTERVAL，画面内容不变。 */
    const FRAME_INTERVAL = 34;
    const MAX_FRAME_INTERVAL = 55;
    const MAX_DPR = 1.5;
    const RAMP_SECONDS = 1.6;
    const TAU = Math.PI * 2;

    let canvas: HTMLCanvasElement | null = null;
    let ctx: CanvasRenderingContext2D | null = null;
    let viewport: Viewport = { width: 0, height: 0, dpr: 1 };
    let elapsed = 0;
    let pointerX = 0;
    let pointerY = 0;
    let pointerTargetX = 0;
    let pointerTargetY = 0;
    let quality = 1;
    let frameHandle: number | null = null;
    let lastFrameAt = 0;
    let lastTimeAt = 0;
    let resizeTimer = 0;
    /** 当前帧间隔：按实测绘制耗时在 FRAME_INTERVAL ↔ MAX_FRAME_INTERVAL 之间浮动。 */
    let frameInterval = FRAME_INTERVAL;
    /** 平滑后的单帧绘制耗时（毫秒）。 */
    let renderCost = 0;
    /**
     * 画布背衬分辨率系数。正常情况下恒为 1；只有在帧间隔已经放宽到上限、
     * 单帧仍明显超时时才降到 0.72（仅弱显卡触发），画面构图与特效完全不变，
     * 只是像素密度略降，用来把“每秒几帧”救回可用的流畅度。
     */
    let backingScale = 1;
    let backingDegraded = false;

    /**
     * 模态遮罩是否正盖住整页。
     *
     * 遮罩带 backdrop-filter 全屏模糊，下面的画布每重绘一帧，
     * 浏览器都要重新采样整屏并做一次高斯模糊，这是主题下最重的一笔开销。
     * 遮罩后的画面本来就被模糊+压暗到几乎看不出动静，因此暂停绘制、
     * 保留最后一帧即可：观感不变，但卡顿消失。
     */
    function overlayCovered(): boolean {
        return document.querySelector(".modal-backdrop.show") !== null;
    }

    /** 切换主题时的“法阵点燃”扩散环，只在最初 2.4 秒出现一次。 */
    function drawIgnition(scene: Scene): void {
        const progress = clamp(scene.time / 2.4, 0, 1);
        if (progress >= 1) {
            return;
        }
        const radius = Math.min(scene.viewport.width, scene.viewport.height)
            * (0.12 + 0.95 * easeOutCubic(progress));
        const ctx2 = scene.ctx;
        ctx2.save();
        ctx2.globalCompositeOperation = "lighter";
        ctx2.beginPath();
        ctx2.arc(scene.viewport.width * 0.5, scene.viewport.height * 0.46, radius, 0, TAU);
        ctx2.lineWidth = 2 + (1 - progress) * 8;
        ctx2.strokeStyle = rgba(BRASS, 0.3 * (1 - progress) * scene.intensity);
        ctx2.stroke();
        ctx2.restore();
    }

    /** 景深雾与暗角的离屏缓存：三层渐变只和视口尺寸有关，逐帧重算纯属浪费。 */
    let depthFogCanvas: HTMLCanvasElement | null = null;

    /**
     * 景深雾 + 暗角：把画面上下的边缘并入黑暗，并压住四角。
     * 这是让建筑、法阵、触手、光柱、眼睛“处在同一片水里”的关键一层——
     * 缺少它时各层各自为战，看起来就是几个互不相干的动画叠在一起。
     *
     * 三层渐变的参数全部来自视口尺寸，与时间无关，因此只在尺寸变化时
     * 烘焙一次到离屏画布，逐帧只做一次整屏 drawImage：画面逐像素一致，
     * 但省掉了每帧一次全屏径向渐变与两次线性渐变的着色计算。
     */
    function paintDepthFog(ctx: CanvasRenderingContext2D, width: number, height: number): void {
        ctx.save();

        // 底雾：只吞掉触手根部与海床，不能让带吸盘的前半段一起变黑。
        const bottom = ctx.createLinearGradient(0, height * 0.66, 0, height);
        bottom.addColorStop(0, "rgba(3, 3, 7, 0)");
        bottom.addColorStop(0.55, "rgba(3, 3, 7, 0.16)");
        bottom.addColorStop(1, "rgba(2, 2, 5, 0.42)");
        ctx.fillStyle = bottom;
        ctx.fillRect(0, height * 0.66, width, height * 0.34);

        // 顶雾：让垂下的触手与肋拱一同退进水里。
        const top = ctx.createLinearGradient(0, 0, 0, height * 0.24);
        top.addColorStop(0, "rgba(3, 3, 7, 0.26)");
        top.addColorStop(1, "rgba(3, 3, 7, 0)");
        ctx.fillStyle = top;
        ctx.fillRect(0, 0, width, height * 0.24);

        // 暗角：四周缓慢并入深渊，中心留出供正文呼吸的窗口。
        const vignette = ctx.createRadialGradient(
            width * 0.5,
            height * 0.45,
            Math.min(width, height) * 0.3,
            width * 0.5,
            height * 0.45,
            Math.max(width, height) * 0.78,
        );
        vignette.addColorStop(0, "rgba(2, 2, 5, 0)");
        vignette.addColorStop(0.62, "rgba(2, 2, 5, 0.13)");
        vignette.addColorStop(1, "rgba(1, 1, 3, 0.62)");
        ctx.fillStyle = vignette;
        ctx.fillRect(0, 0, width, height);

        ctx.restore();
    }

    /** 按当前视口尺寸重新烘焙景深雾与暗角。 */
    function buildDepthFog(): void {
        if (viewport.width <= 0 || viewport.height <= 0) {
            depthFogCanvas = null;
            return;
        }
        const fog = document.createElement("canvas");
        fog.width = Math.max(1, Math.round(viewport.width * viewport.dpr));
        fog.height = Math.max(1, Math.round(viewport.height * viewport.dpr));
        const fogCtx = fog.getContext("2d");
        if (!fogCtx) {
            depthFogCanvas = null;
            return;
        }
        fogCtx.setTransform(viewport.dpr, 0, 0, viewport.dpr, 0, 0);
        paintDepthFog(fogCtx, viewport.width, viewport.height);
        depthFogCanvas = fog;
    }

    function render(delta: number): void {
        if (ctx === null) {
            return;
        }
        elapsed += delta;
        const follow = Math.min(1, delta * 4.5);
        pointerX = lerp(pointerX, pointerTargetX, follow);
        pointerY = lerp(pointerY, pointerTargetY, follow);

        const intensity = easeOutCubic(elapsed / RAMP_SECONDS);
        // WebGL 水体先行：它独立于 2D 画布，位于其下层。
        Atmosphere.draw(elapsed, { x: pointerX, y: pointerY }, intensity);

        ctx.clearRect(0, 0, viewport.width, viewport.height);
        const scene: Scene = {
            ctx,
            viewport,
            time: elapsed,
            delta,
            pointer: { x: pointerX, y: pointerY },
            intensity,
            quality,
        };

        Environment.draw(scene);
        // 背景漂移符文在法阵之前：它们属于“水里的刻痕”，不该盖在仪式几何上面。
        AbyssFx.drawAmbient(scene);
        Sigils.draw(scene);
        Eye.draw(scene);
        Spores.draw(scene);
        Tentacles.draw(scene);
        // 雾在触手之后，让触手根部落进黑暗里，而不是硬生生贴在下边缘上。
        if (depthFogCanvas !== null) {
            ctx.drawImage(depthFogCanvas, 0, 0, viewport.width, viewport.height);
        } else {
            paintDepthFog(ctx, viewport.width, viewport.height);
        }
        drawIgnition(scene);
        // 鼠标拖尾与点击爆裂放在最上层，但透明度很低，只做一点点“触到符文”的反馈。
        AbyssFx.drawPointer(scene);
    }

    function loop(timestamp: number): void {
        frameHandle = window.requestAnimationFrame(loop);
        if (timestamp - lastFrameAt < frameInterval) {
            return;
        }
        lastFrameAt = timestamp;
        // 遮罩盖住整页时跳过整帧绘制：保留最后一帧，恢复时时间轴从原地继续。
        if (overlayCovered()) {
            lastTimeAt = timestamp;
            return;
        }
        const delta = lastTimeAt === 0 ? 0.034 : Math.min((timestamp - lastTimeAt) / 1000, 0.12);
        lastTimeAt = timestamp;
        const startedAt = performance.now();
        render(delta);
        // 自适应帧距：绘制耗时逼近帧间隔就放宽间隔，留出余量给浏览器合成。
        // 动画本身按时间推进，间隔变化只影响平滑度，不改变任何视觉效果。
        const cost = performance.now() - startedAt;
        renderCost = renderCost === 0 ? cost : renderCost * 0.82 + cost * 0.18;
        if (renderCost > frameInterval * 0.62 && frameInterval < MAX_FRAME_INTERVAL) {
            frameInterval = Math.min(MAX_FRAME_INTERVAL, frameInterval + 3);
        } else if (renderCost < frameInterval * 0.4 && frameInterval > FRAME_INTERVAL) {
            frameInterval = Math.max(FRAME_INTERVAL, frameInterval - 2);
        }
        // 已经退到最慢帧率仍然超时：说明瓶颈是像素填充或 WebGL，而不是帧率，
        // 此时降一次背衬分辨率比继续掉帧更可接受。
        if (!backingDegraded && frameInterval >= MAX_FRAME_INTERVAL && renderCost > 26 && viewport.dpr > 1.1) {
            backingDegraded = true;
            backingScale = 0.72;
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

    /** 重新测量视口并让各绘制层重建几何（离屏层与粒子）。 */
    function applySize(): void {
        if (canvas === null || ctx === null) {
            return;
        }
        const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR) * backingScale;
        viewport = { width: window.innerWidth, height: window.innerHeight, dpr };
        canvas.width = Math.max(1, Math.round(viewport.width * dpr));
        canvas.height = Math.max(1, Math.round(viewport.height * dpr));
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        buildDepthFog();
        Environment.rebuild(viewport);
        Tentacles.rebuild(viewport);
        Sigils.rebuild(viewport);
        Spores.rebuild(viewport);
        AbyssFx.rebuild(viewport);
        Atmosphere.resize(viewport);
        render(0);
    }

    function sync(): void {
        if (isThemeActive() && !document.hidden) {
            applySize();
            start();
        } else {
            stop();
        }
    }

    function handleResize(): void {
        if (resizeTimer !== 0) {
            window.clearTimeout(resizeTimer);
        }
        resizeTimer = window.setTimeout(() => {
            resizeTimer = 0;
            if (isThemeActive() && !document.hidden) {
                applySize();
            }
        }, 180);
    }

    function handlePointer(event: PointerEvent): void {
        pointerTargetX = (event.clientX / window.innerWidth) * 2 - 1;
        pointerTargetY = (event.clientY / window.innerHeight) * 2 - 1;
        // 拖动（按下左键）时拖尾更密，普通移动时更克制。
        AbyssFx.onTrailMove(event.clientX, event.clientY, event.buttons > 0);
    }

    function handlePointerDown(event: PointerEvent): void {
        AbyssFx.onPointerDown(event.clientX, event.clientY);
    }

    function resetPointer(): void {
        pointerTargetX = 0;
        pointerTargetY = 0;
    }

    function observe(): void {
        const observer = new MutationObserver(sync);
        observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });

        const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
        if (typeof motionQuery.addEventListener === "function") {
            motionQuery.addEventListener("change", sync);
        }

        window.addEventListener("resize", handleResize);
        // 主题设置面板改密度 / 高度 / 法阵大小后，重建烘焙图层。
        window.addEventListener("theme-settings-change", handleResize);
        window.addEventListener("pointermove", handlePointer, { passive: true });
        window.addEventListener("pointerdown", handlePointerDown, { passive: true });
        window.addEventListener("pointerleave", resetPointer, { passive: true });
        window.addEventListener("load", sync, { once: true });

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                stop();
            } else if (isThemeActive()) {
                applySize();
                start();
            }
        });
    }

    export function boot(): void {
        if (canvas !== null) {
            return;
        }
        const element = document.createElement("canvas");
        element.className = CANVAS_CLASS;
        element.setAttribute("aria-hidden", "true");
        document.body.appendChild(element);
        canvas = element;
        ctx = element.getContext("2d");

        const cores = navigator.hardwareConcurrency;
        quality = typeof cores === "number" && cores > 0 && cores <= 4 ? 0.7 : 1;

        // WebGL 氛围层失败时内部会静默回退，不影响其余图层。
        Atmosphere.boot();
        observe();
        sync();

        // 字体（Cinzel / Noto Serif SC）异步加载完成后，用真实字形重新烘焙符文环。
        if (typeof document.fonts !== "undefined") {
            void document.fonts.ready.then(() => {
                if (canvas !== null && isThemeActive()) {
                    Sigils.rebuild(viewport);
                }
            });
        }
    }
}

if (document.body) {
    AbyssTheme.boot();
} else {
    document.addEventListener("DOMContentLoaded", () => AbyssTheme.boot(), { once: true });
}
