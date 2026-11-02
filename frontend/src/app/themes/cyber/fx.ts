/**
 * 「赛博档案」主题 · 交互特效与个人设置
 *
 * 负责三件事：
 * 1. 指针锁定框：跟随鼠标的角括号 + 坐标读数，按下时短暂收紧，形成“终端锁定”反馈。
 * 2. 指针数据拖尾与点击涟漪：极短的十六进制碎片，强度刻意压低，不干扰阅读。
 * 3. 把个人设置中的开关落到 CSS 变量与运行时状态上（保存在 localStorage）。
 *
 * 所有特效只在 body.theme-cyber-2 下出现，随主题切换自动启停。
 */
namespace CyberTheme {
    export interface FxSettings {
        /** 沉浸特效总开关：关闭后整页动画与数据雨停摆，只保留静态底色。 */
        enabled: boolean;
        /** 氛围强度（0.2 → 1）：同时影响 WebGL 氛围层与扫描线浓度。 */
        intensity: number;
        /** 数据雨密度（0.3 → 1）。 */
        rainDensity: number;
        /** 是否显示指针锁定框与数据拖尾。 */
        reticle: boolean;
    }

    interface TrailParticle {
        x: number;
        y: number;
        vx: number;
        vy: number;
        size: number;
        life: number;
        maxLife: number;
        glyph: string;
        warm: boolean;
    }

    interface Ripple {
        x: number;
        y: number;
        progress: number;
        glyphs: Array<{ angle: number; distance: number; glyph: string }>;
    }

    const SETTINGS_KEY = "trpg_cyber_fx_settings";
    const DEFAULT_SETTINGS: FxSettings = {
        enabled: true,
        intensity: 0.68,
        rainDensity: 0.8,
        reticle: true,
    };
    const MAX_PARTICLES = 60;
    const TAU = Math.PI * 2;

    let settings: FxSettings = { ...DEFAULT_SETTINGS };
    let bound = false;
    let reticleElement: HTMLElement | null = null;
    let reticleLabel: HTMLElement | null = null;
    let reticleX = 0;
    let reticleY = 0;
    let reticleTargetX = 0;
    let reticleTargetY = 0;
    let reticleVisible = false;
    let lastLabelUpdate = 0;
    let lastMoveX = 0;
    let lastMoveY = 0;
    let hasMoveOrigin = false;
    let particles: TrailParticle[] = [];
    let ripples: Ripple[] = [];

    function clampSetting(value: unknown, min: number, max: number, fallback: number): number {
        const parsed = typeof value === "number" ? value : Number(value);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return clamp(parsed, min, max);
    }

    function loadSettings(): void {
        let raw: string | null = null;
        try {
            raw = window.localStorage.getItem(SETTINGS_KEY);
        } catch {
            raw = null;
        }
        if (raw === null) {
            settings = { ...DEFAULT_SETTINGS };
            applySettings();
            return;
        }
        try {
            const parsed = JSON.parse(raw) as Partial<Record<keyof FxSettings, unknown>>;
            settings = {
                enabled: typeof parsed.enabled === "boolean" ? parsed.enabled : DEFAULT_SETTINGS.enabled,
                intensity: clampSetting(parsed.intensity, 0.2, 1, DEFAULT_SETTINGS.intensity),
                rainDensity: clampSetting(parsed.rainDensity, 0.3, 1, DEFAULT_SETTINGS.rainDensity),
                reticle: typeof parsed.reticle === "boolean" ? parsed.reticle : DEFAULT_SETTINGS.reticle,
            };
        } catch {
            settings = { ...DEFAULT_SETTINGS };
        }
        applySettings();
    }

    function saveSettings(): void {
        try {
            window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
        } catch {
            // 隐私模式下写入失败，只影响下次打开页面时的记忆。
        }
    }

    /** 把运行时设置落到 CSS 变量上；CSS 只读这些变量，不再感知 JS 状态。 */
    function applySettings(): void {
        const body = document.body;
        if (body === null) {
            return;
        }
        body.classList.toggle("cyber-fx-off", !settings.enabled);
        const scanAlpha = (0.10 + settings.intensity * 0.16).toFixed(3);
        body.style.setProperty("--theme-cyber-scanline-alpha", scanAlpha);
        body.style.setProperty("--theme-cyber-glow-alpha", (0.18 + settings.intensity * 0.34).toFixed(3));
        if (!settings.reticle || !settings.enabled) {
            hideReticle();
        }
        window.dispatchEvent(new CustomEvent("cyber-fx-settings"));
    }

    export function getSettings(): FxSettings {
        return { ...settings };
    }

    export function isEnabled(): boolean {
        return settings.enabled;
    }

    // ---------------------------------------------------------------- 指针锁定框

    function buildReticle(): void {
        if (reticleElement !== null) {
            return;
        }
        const element = document.createElement("div");
        element.className = "theme-cyber-reticle";
        element.setAttribute("aria-hidden", "true");
        element.innerHTML = [
            '<span class="cyber-reticle-corner tl"></span>',
            '<span class="cyber-reticle-corner tr"></span>',
            '<span class="cyber-reticle-corner bl"></span>',
            '<span class="cyber-reticle-corner br"></span>',
            '<span class="cyber-reticle-dot"></span>',
            '<span class="cyber-reticle-label">LOCK // 0000.0000</span>',
        ].join("");
        document.body.appendChild(element);
        reticleElement = element;
        reticleLabel = element.querySelector<HTMLElement>(".cyber-reticle-label");
    }

    function hideReticle(): void {
        reticleVisible = false;
        if (reticleElement !== null) {
            reticleElement.classList.remove("is-visible", "is-locking");
        }
    }

    /** 屏幕四角的取景框：纯装饰 DOM，显隐由 body.theme-cyber-2 / .cyber-fx-off 控制。 */
    function buildFrame(): void {
        if (document.querySelector(".theme-cyber-frame") !== null) {
            return;
        }
        const element = document.createElement("div");
        element.className = "theme-cyber-frame";
        element.setAttribute("aria-hidden", "true");
        element.innerHTML = [
            '<span class="cyber-frame-corner tl"></span>',
            '<span class="cyber-frame-corner tr"></span>',
            '<span class="cyber-frame-corner bl"></span>',
            '<span class="cyber-frame-corner br"></span>',
        ].join("");
        document.body.appendChild(element);
    }

    function ensureReticle(): void {
        if (!settings.reticle || !isThemeActive()) {
            hideReticle();
            return;
        }
        buildReticle();
        if (reticleElement === null) {
            return;
        }
        if (!reticleVisible) {
            reticleVisible = true;
            reticleElement.classList.add("is-visible");
        }
    }

    /** 主循环调用：用阻尼跟随指针并刷新坐标读数，避免直接绑定 pointermove 造成抖动。 */
    export function updateReticle(delta: number): void {
        ensureReticle();
        if (!reticleVisible || reticleElement === null) {
            return;
        }
        const settle = 1 - Math.pow(0.0016, delta);
        reticleX = lerp(reticleX, reticleTargetX, clamp(settle, 0, 1));
        reticleY = lerp(reticleY, reticleTargetY, clamp(settle, 0, 1));
        reticleElement.style.transform = `translate3d(${reticleX.toFixed(1)}px, ${reticleY.toFixed(1)}px, 0)`;
        const now = performance.now();
        if (now - lastLabelUpdate > 140 && reticleLabel !== null) {
            lastLabelUpdate = now;
            reticleLabel.textContent = `LOCK // ${reticleTargetX.toFixed(0).padStart(4, "0")}.${reticleTargetY
                .toFixed(0)
                .padStart(4, "0")}`;
        }
    }

    export function onPointerMove(clientX: number, clientY: number): void {
        reticleTargetX = clientX;
        reticleTargetY = clientY;
        if (!reticleVisible) {
            // 首次出现时直接对齐，避免从屏幕左上角滑入。
            reticleX = clientX;
            reticleY = clientY;
        }
        if (!settings.reticle || !isThemeActive()) {
            return;
        }
        ensureReticle();
        if (reticleElement !== null) {
            reticleElement.classList.remove("is-idle");
        }
        spawnTrail(clientX, clientY);
    }

    export function onPointerDown(clientX: number, clientY: number): void {
        if (!isThemeActive()) {
            return;
        }
        if (settings.reticle && reticleElement !== null) {
            reticleElement.classList.add("is-locking");
            window.setTimeout(() => {
                reticleElement?.classList.remove("is-locking");
            }, 320);
        }
        spawnRipple(clientX, clientY);
    }

    export function onPointerLeave(): void {
        hideReticle();
        hasMoveOrigin = true;
    }

    /** 鼠标长时间静止时把锁定框调暗，避免一直“抢眼”。 */
    let lastActivityAt = 0;

    export function markActivity(): void {
        lastActivityAt = performance.now();
    }

    export function refreshIdle(): void {
        if (reticleElement !== null && reticleVisible) {
            const idle = performance.now() - lastActivityAt > 2600;
            reticleElement.classList.toggle("is-idle", idle);
        }
    }

    // ---------------------------------------------------------------- 粒子与涟漪

    function spawnTrail(x: number, y: number): void {
        if (!isThemeActive()) {
            return;
        }
        const distance = Math.hypot(x - lastMoveX, y - lastMoveY);
        lastMoveX = x;
        lastMoveY = y;
        if (!hasMoveOrigin) {
            hasMoveOrigin = true;
            return;
        }
        if (distance < 14 || particles.length >= MAX_PARTICLES) {
            return;
        }
        const warm = Math.random() < 0.18;
        particles.push({
            x: x + randomBetween(-5, 5),
            y: y + randomBetween(-5, 5),
            vx: randomBetween(-16, 16),
            vy: randomBetween(8, 30),
            size: randomInt(1, 2),
            life: 0,
            maxLife: randomBetween(0.5, 1.15),
            glyph: hexLabel(1),
            warm,
        });
    }

    function spawnRipple(x: number, y: number): void {
        const glyphs: Array<{ angle: number; distance: number; glyph: string }> = [];
        const count = 7;
        for (let index = 0; index < count; index += 1) {
            glyphs.push({
                angle: (index / count) * TAU + randomBetween(-0.2, 0.2),
                distance: randomBetween(26, 52),
                glyph: hexLabel(1),
            });
        }
        ripples.push({ x, y, progress: 0, glyphs });
        if (ripples.length > 4) {
            ripples.shift();
        }
    }

    export function drawParticles(scene: Scene): void {
        if (particles.length === 0 && ripples.length === 0) {
            return;
        }
        const ctx = scene.ctx;
        ctx.save();
        ctx.globalCompositeOperation = "lighter";

        for (let index = particles.length - 1; index >= 0; index -= 1) {
            const particle = particles[index];
            if (particle === undefined) continue;
            particle.life += scene.delta;
            particle.x += particle.vx * scene.delta;
            particle.y += particle.vy * scene.delta;
            const fade = 1 - particle.life / particle.maxLife;
            if (fade <= 0) {
                particles.splice(index, 1);
                continue;
            }
            ctx.font = `${particle.size * 5 + 6}px "Share Tech Mono", "Cascadia Mono", monospace`;
            ctx.fillStyle = rgba(particle.warm ? AMBER : PHOSPHOR, fade * 0.5);
            ctx.fillText(particle.glyph, particle.x, particle.y);
        }

        for (let index = ripples.length - 1; index >= 0; index -= 1) {
            const ripple = ripples[index];
            if (ripple === undefined) continue;
            ripple.progress += scene.delta * 1.7;
            if (ripple.progress >= 1) {
                ripples.splice(index, 1);
                continue;
            }
            const eased = easeOutCubic(ripple.progress);
            const radius = 34 + eased * 86;
            ctx.strokeStyle = rgba(PHOSPHOR, (1 - ripple.progress) * 0.5);
            ctx.lineWidth = 1.4 * (1 - ripple.progress);
            ctx.beginPath();
            ctx.arc(ripple.x, ripple.y, radius, 0, TAU);
            ctx.stroke();
            ctx.strokeStyle = rgba(ICE, (1 - ripple.progress) * 0.3);
            ctx.beginPath();
            ctx.arc(ripple.x, ripple.y, radius * 0.72, 0, TAU);
            ctx.stroke();
            for (const item of ripple.glyphs) {
                const distance = 20 + item.distance * eased;
                const x = ripple.x + Math.cos(item.angle) * distance;
                const y = ripple.y + Math.sin(item.angle) * distance;
                ctx.font = '11px "Share Tech Mono", "Cascadia Mono", monospace';
                ctx.fillStyle = rgba(item.glyph.charCodeAt(0) % 3 === 0 ? AMBER : PHOSPHOR,
                    (1 - ripple.progress) * 0.6);
                ctx.fillText(item.glyph, x, y);
            }
        }
        ctx.restore();
    }

    // ---------------------------------------------------------------- 设置面板绑定

    function bindSettingsControls(): void {
        if (bound) {
            return;
        }
        const enabled = document.getElementById("cyberFxEnabled") as HTMLInputElement | null;
        const intensity = document.getElementById("cyberFxIntensity") as HTMLInputElement | null;
        const intensityValue = document.getElementById("cyberFxIntensityValue");
        const density = document.getElementById("cyberRainDensity") as HTMLInputElement | null;
        const densityValue = document.getElementById("cyberRainDensityValue");
        const reticle = document.getElementById("cyberReticleEnabled") as HTMLInputElement | null;
        if (enabled === null && intensity === null && density === null && reticle === null) {
            return;
        }
        bound = true;

        if (enabled !== null) {
            enabled.checked = settings.enabled;
            enabled.addEventListener("change", () => {
                settings.enabled = enabled.checked;
                applySettings();
                saveSettings();
            });
        }
        if (intensity !== null) {
            intensity.value = String(Math.round(settings.intensity * 100));
            if (intensityValue !== null) {
                intensityValue.textContent = `${Math.round(settings.intensity * 100)}%`;
            }
            intensity.addEventListener("input", () => {
                settings.intensity = clampSetting(Number(intensity.value) / 100, 0.2, 1, DEFAULT_SETTINGS.intensity);
                if (intensityValue !== null) {
                    intensityValue.textContent = `${Math.round(settings.intensity * 100)}%`;
                }
                applySettings();
                saveSettings();
            });
        }
        if (density !== null) {
            density.value = String(Math.round(settings.rainDensity * 100));
            if (densityValue !== null) {
                densityValue.textContent = `${Math.round(settings.rainDensity * 100)}%`;
            }
            density.addEventListener("input", () => {
                settings.rainDensity = clampSetting(Number(density.value) / 100, 0.3, 1, DEFAULT_SETTINGS.rainDensity);
                if (densityValue !== null) {
                    densityValue.textContent = `${Math.round(settings.rainDensity * 100)}%`;
                }
                applySettings();
                saveSettings();
            });
        }
        if (reticle !== null) {
            reticle.checked = settings.reticle;
            reticle.addEventListener("change", () => {
                settings.reticle = reticle.checked;
                if (!settings.reticle) {
                    hideReticle();
                    particles = [];
                }
                saveSettings();
            });
        }
    }

    export const Fx = {
        init(): void {
            loadSettings();
            buildFrame();
            buildReticle();
            bindSettingsControls();
        },
        settings: getSettings,
        isEnabled,
        onPointerMove,
        onPointerDown,
        onPointerLeave,
        updateReticle,
        drawParticles,
        markActivity,
        refreshIdle,
        applySettings,
    };
}
