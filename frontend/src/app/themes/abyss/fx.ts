/**
 * 「深渊帷幕」主题 · 交互与氛围特效层
 *
 * 三件事：
 * 1. 背景符文缓慢上浮，像水里的刻痕在随流漂移（低透明度，不抢正文）。
 * 2. 鼠标移动 / 拖动时留下极短的符文拖尾，点击时炸开一圈稍亮的符文。
 *    默认强度刻意压低，避免“满屏粒子”的视觉疲劳。
 * 3. 把个人设置里的透明度、拖尾、符文开关落到 CSS 变量与运行状态上。
 *
 * 所有开关保存在 localStorage，只影响当前浏览器；默认值即可看到效果，
 * 但不建议把拖尾强度调到最高——那会让长文阅读变得很吵。
 */
namespace AbyssTheme {
    export interface FxSettings {
        /** 聊天面板基础不透明度（0.3 → 1）。 */
        chatAlpha: number;
        /** 左侧栏不透明度（0.3 → 1），默认偏低，让左侧的触手能透出来。 */
        sidebarAlpha: number;
        /** 右侧栏（房间成员）不透明度（0.3 → 1）。 */
        railAlpha: number;
        /** 是否启用鼠标符文拖尾与点击爆裂。 */
        trail: boolean;
        /** 拖尾强度（0.15 → 1），控制粒子数量与亮度。 */
        trailStrength: number;
        /** 是否启用背景符文飘动。 */
        driftRunes: boolean;
        /** 世界意志遮罩不透明度（0.25 → 0.95），越大背景越暗。 */
        veilAlpha: number;
        /** 世界意志遮罩的背景模糊半径（0 → 24px），0 表示不模糊。 */
        veilBlur: number;
        /** 触手密度（0.4 → 1.4），以底部主触手数量为基准缩放。 */
        tentacleDensity: number;
        /** 触手高度（0.6 → 1.4），统一缩放触手长度。 */
        tentacleHeight: number;
        /** 触手高度随机性（0 → 0.5），越大相邻触手高差越明显。 */
        tentacleHeightVariance: number;
        /** 触手摆动速度（0.4 → 1.6）。 */
        swaySpeed: number;
        /** 法阵大小（0.7 → 1.3）。 */
        sigilScale: number;
        /** 鼠标拖尾密度（0.2 → 1.5），控制拖尾粒子生成频率。 */
        trailDensity: number;
    }

    interface DriftRune {
        x: number;
        y: number;
        size: number;
        glyph: string;
        alpha: number;
        speed: number;
        sway: number;
        phase: number;
        angle: number;
        spin: number;
    }

    interface FxParticle {
        x: number;
        y: number;
        vx: number;
        vy: number;
        size: number;
        glyph: string;
        age: number;
        life: number;
        alpha: number;
        angle: number;
        spin: number;
        warm: boolean;
    }

    const SETTINGS_KEY = "trpg_abyss_fx_settings";
    const RUNES = "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟ";
    const TAU = Math.PI * 2;
    const DEFAULT_SETTINGS: FxSettings = {
        chatAlpha: 0.6,
        // 左侧栏默认更透一些：触手从画面左侧探进来时不会被整块面板挡住。
        sidebarAlpha: 0.5,
        railAlpha: 0.7,
        trail: true,
        trailStrength: 0.55,
        driftRunes: true,
        // 世界意志遮罩：半透明 + 模糊，让背后的深渊场景隐约可见，
        // 同时 alpha 0.55 足够压住背景、保证弹窗文字对比度。
        veilAlpha: 0.55,
        veilBlur: 10,
        tentacleDensity: 1,
        tentacleHeight: 1,
        tentacleHeightVariance: 0.2,
        swaySpeed: 1,
        sigilScale: 1,
        trailDensity: 0.55,
    };
    const MAX_PARTICLES = 90;

    let settings: FxSettings = { ...DEFAULT_SETTINGS };
    let viewWidth = 0;
    let viewHeight = 0;
    let driftRunes: DriftRune[] = [];
    let particles: FxParticle[] = [];
    let lastTrailX = 0;
    let lastTrailY = 0;
    let hasTrailOrigin = false;
    let dragging = false;
    let bound = false;

    /**
     * 符文精灵缓存：背景符文每帧要画二十多枚，逐帧 fillText 会反复切换字体、
     * 重新走一遍文字排版。这里按「字形 + 字号」烘焙成小图，逐帧只做 drawImage，
     * 画面保持一致，CPU 开销明显下降。
     */
    const glyphSpriteCache = new Map<string, HTMLCanvasElement>();
    const GLYPH_SPRITE_RATIO = 2;

    function glyphSprite(glyph: string, size: number, tint: Rgb): HTMLCanvasElement | null {
        const rounded = Math.max(1, Math.round(size));
        const key = `${glyph}-${rounded}-${tint.r},${tint.g},${tint.b}`;
        const cached = glyphSpriteCache.get(key);
        if (cached !== undefined) {
            return cached;
        }
        const pad = Math.ceil(rounded * 0.5);
        const cssSize = rounded + pad * 2;
        const sprite = document.createElement("canvas");
        sprite.width = Math.ceil(cssSize * GLYPH_SPRITE_RATIO);
        sprite.height = Math.ceil(cssSize * GLYPH_SPRITE_RATIO);
        const spriteCtx = sprite.getContext("2d");
        if (!spriteCtx) {
            return null;
        }
        spriteCtx.scale(GLYPH_SPRITE_RATIO, GLYPH_SPRITE_RATIO);
        spriteCtx.font = `600 ${rounded}px "Cinzel", "Noto Serif SC", serif`;
        spriteCtx.textAlign = "center";
        spriteCtx.textBaseline = "middle";
        spriteCtx.fillStyle = rgba(tint, 0.9);
        spriteCtx.fillText(glyph, cssSize / 2, cssSize / 2);
        glyphSpriteCache.set(key, sprite);
        return sprite;
    }

    function clampSetting(value: unknown, min: number, max: number, fallback: number): number {
        const parsed = typeof value === "number" ? value : Number(value);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return clamp(parsed, min, max);
    }

    function loadSettings(): void {
        // 统一从主题契约读取：站点默认 < 个人覆盖已在 ThemeRegistry.getValues 中合并。
        const parsed = ThemeRegistry.getValues("pattern_coc") as Partial<Record<keyof FxSettings, unknown>>;
        settings = {
            chatAlpha: clampSetting(parsed.chatAlpha, 0.3, 1, DEFAULT_SETTINGS.chatAlpha),
            sidebarAlpha: clampSetting(parsed.sidebarAlpha, 0.3, 1, DEFAULT_SETTINGS.sidebarAlpha),
            railAlpha: clampSetting(parsed.railAlpha, 0.3, 1, DEFAULT_SETTINGS.railAlpha),
            trail: typeof parsed.trail === "boolean" ? parsed.trail : DEFAULT_SETTINGS.trail,
            trailStrength: clampSetting(parsed.trailStrength, 0.15, 1, DEFAULT_SETTINGS.trailStrength),
            driftRunes:
                typeof parsed.driftRunes === "boolean" ? parsed.driftRunes : DEFAULT_SETTINGS.driftRunes,
            veilAlpha: clampSetting(parsed.veilAlpha, 0.25, 0.95, DEFAULT_SETTINGS.veilAlpha),
            veilBlur: clampSetting(parsed.veilBlur, 0, 24, DEFAULT_SETTINGS.veilBlur),
            tentacleDensity: clampSetting(parsed.tentacleDensity, 0.4, 1.4, DEFAULT_SETTINGS.tentacleDensity),
            tentacleHeight: clampSetting(parsed.tentacleHeight, 0.6, 1.4, DEFAULT_SETTINGS.tentacleHeight),
            tentacleHeightVariance: clampSetting(
                parsed.tentacleHeightVariance,
                0,
                0.5,
                DEFAULT_SETTINGS.tentacleHeightVariance,
            ),
            swaySpeed: clampSetting(parsed.swaySpeed, 0.4, 1.6, DEFAULT_SETTINGS.swaySpeed),
            sigilScale: clampSetting(parsed.sigilScale, 0.7, 1.3, DEFAULT_SETTINGS.sigilScale),
            trailDensity: clampSetting(parsed.trailDensity, 0.2, 1.5, DEFAULT_SETTINGS.trailDensity),
        };
    }

    function saveSettings(): void {
        try {
            window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
        } catch {
            // 隐私模式下写入失败：只影响下次打开页面时的记忆。
        }
    }

    /** 把设置落到 CSS 变量上，聊天面板的透明度因此即时生效。 */
    function applySettings(): void {
        const root = document.body;
        if (root === null) {
            return;
        }
        const chatAlpha = settings.chatAlpha;
        // 输入栏、成员栏需要比消息气泡略实，否则文字会飘在背景上读不清；
        // 默认只比气泡高 15%，底部触手仍能从输入栏下方透出来。
        const panelAlpha = clamp(chatAlpha + 0.15, 0.35, 1);
        root.style.setProperty("--theme-chat-alpha", chatAlpha.toFixed(3));
        root.style.setProperty("--theme-chat-panel-alpha", panelAlpha.toFixed(3));
        // 左右侧栏各自独立：左侧默认更透，触手可以从中透出；右侧以可读性优先。
        root.style.setProperty("--theme-sidebar-alpha", settings.sidebarAlpha.toFixed(3));
        root.style.setProperty("--theme-rail-alpha", settings.railAlpha.toFixed(3));
        // 世界意志遮罩（见 08-theme-abyss-components.css）：不透明度与模糊半径。
        root.style.setProperty("--theme-veil-alpha", settings.veilAlpha.toFixed(3));
        root.style.setProperty("--theme-veil-blur", `${Math.round(settings.veilBlur)}px`);
    }

    function randomRune(): string {
        return RUNES[Math.floor(Math.random() * RUNES.length)] ?? "ᚠ";
    }

    /** 设置界面里的三个控件：只绑定一次，值即时生效。 */
    function bindSettingsControls(): void {
        if (bound) {
            return;
        }
        bound = true;
        const chat = document.getElementById("abyssChatTransparency") as HTMLInputElement | null;
        const chatValue = document.getElementById("abyssChatTransparencyValue");
        const sidebar = document.getElementById("abyssSidebarTransparency") as HTMLInputElement | null;
        const sidebarValue = document.getElementById("abyssSidebarTransparencyValue");
        const rail = document.getElementById("abyssRailTransparency") as HTMLInputElement | null;
        const railValue = document.getElementById("abyssRailTransparencyValue");
        const veilOpacity = document.getElementById("abyssVeilOpacity") as HTMLInputElement | null;
        const veilOpacityValue = document.getElementById("abyssVeilOpacityValue");
        const veilBlur = document.getElementById("abyssVeilBlur") as HTMLInputElement | null;
        const veilBlurValue = document.getElementById("abyssVeilBlurValue");
        const trail = document.getElementById("abyssTrailEnabled") as HTMLInputElement | null;
        const strength = document.getElementById("abyssTrailStrength") as HTMLInputElement | null;
        const strengthValue = document.getElementById("abyssTrailStrengthValue");
        const drift = document.getElementById("abyssDriftRunes") as HTMLInputElement | null;

        if (chat) {
            chat.value = String(Math.round(settings.chatAlpha * 100));
            if (chatValue) chatValue.textContent = `${Math.round(settings.chatAlpha * 100)}%`;
            chat.addEventListener("input", () => {
                settings.chatAlpha = clampSetting(Number(chat.value) / 100, 0.3, 1, DEFAULT_SETTINGS.chatAlpha);
                if (chatValue) chatValue.textContent = `${Math.round(settings.chatAlpha * 100)}%`;
                applySettings();
                saveSettings();
            });
        }
        if (sidebar) {
            sidebar.value = String(Math.round(settings.sidebarAlpha * 100));
            if (sidebarValue) sidebarValue.textContent = `${Math.round(settings.sidebarAlpha * 100)}%`;
            sidebar.addEventListener("input", () => {
                settings.sidebarAlpha = clampSetting(Number(sidebar.value) / 100, 0.3, 1, DEFAULT_SETTINGS.sidebarAlpha);
                if (sidebarValue) sidebarValue.textContent = `${Math.round(settings.sidebarAlpha * 100)}%`;
                applySettings();
                saveSettings();
            });
        }
        if (rail) {
            rail.value = String(Math.round(settings.railAlpha * 100));
            if (railValue) railValue.textContent = `${Math.round(settings.railAlpha * 100)}%`;
            rail.addEventListener("input", () => {
                settings.railAlpha = clampSetting(Number(rail.value) / 100, 0.3, 1, DEFAULT_SETTINGS.railAlpha);
                if (railValue) railValue.textContent = `${Math.round(settings.railAlpha * 100)}%`;
                applySettings();
                saveSettings();
            });
        }
        if (veilOpacity) {
            veilOpacity.value = String(Math.round(settings.veilAlpha * 100));
            if (veilOpacityValue) veilOpacityValue.textContent = `${Math.round(settings.veilAlpha * 100)}%`;
            veilOpacity.addEventListener("input", () => {
                settings.veilAlpha = clampSetting(Number(veilOpacity.value) / 100, 0.25, 0.95, DEFAULT_SETTINGS.veilAlpha);
                if (veilOpacityValue) veilOpacityValue.textContent = `${Math.round(settings.veilAlpha * 100)}%`;
                applySettings();
                saveSettings();
            });
        }
        if (veilBlur) {
            veilBlur.value = String(Math.round(settings.veilBlur));
            if (veilBlurValue) veilBlurValue.textContent = `${Math.round(settings.veilBlur)}px`;
            veilBlur.addEventListener("input", () => {
                settings.veilBlur = clampSetting(Number(veilBlur.value), 0, 24, DEFAULT_SETTINGS.veilBlur);
                if (veilBlurValue) veilBlurValue.textContent = `${Math.round(settings.veilBlur)}px`;
                applySettings();
                saveSettings();
            });
        }
        if (trail) {
            trail.checked = settings.trail;
            trail.addEventListener("change", () => {
                settings.trail = trail.checked;
                if (!settings.trail) particles = [];
                saveSettings();
            });
        }
        if (strength) {
            strength.value = String(Math.round(settings.trailStrength * 100));
            if (strengthValue) strengthValue.textContent = `${Math.round(settings.trailStrength * 100)}%`;
            strength.addEventListener("input", () => {
                settings.trailStrength = clampSetting(Number(strength.value) / 100, 0.15, 1, DEFAULT_SETTINGS.trailStrength);
                if (strengthValue) strengthValue.textContent = `${Math.round(settings.trailStrength * 100)}%`;
                saveSettings();
            });
        }
        if (drift) {
            drift.checked = settings.driftRunes;
            drift.addEventListener("change", () => {
                settings.driftRunes = drift.checked;
                saveSettings();
            });
        }
    }

    function rebuild(viewport: Viewport): void {
        viewWidth = viewport.width;
        viewHeight = viewport.height;
        const seeded = createSessionRandom(0xda57);
        const narrow = viewport.width < 900;
        const count = narrow ? 14 : 24;
        const list: DriftRune[] = [];
        for (let i = 0; i < count; i += 1) {
            list.push({
                x: seeded() * viewWidth,
                y: seeded() * viewHeight,
                size: 9 + seeded() * 15,
                glyph: RUNES[Math.floor(seeded() * RUNES.length)] ?? "ᚠ",
                alpha: 0.03 + seeded() * 0.09,
                speed: 3 + seeded() * 12,
                sway: 6 + seeded() * 20,
                phase: seeded() * TAU,
                angle: (seeded() - 0.5) * 0.5,
                spin: (seeded() - 0.5) * 0.04,
            });
        }
        driftRunes = list;
        particles = [];
        hasTrailOrigin = false;
    }

    /** 背景符文：缓慢上浮 + 轻微横摆，像随洋流漂移的刻痕。 */
    function drawAmbient(scene: Scene): void {
        if (!settings.driftRunes || driftRunes.length === 0) {
            return;
        }
        const ctx = scene.ctx;
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        for (const rune of driftRunes) {
            rune.y -= rune.speed * scene.delta;
            rune.x += Math.sin(scene.time * 0.35 + rune.phase) * rune.sway * scene.delta * 0.12;
            rune.angle += rune.spin * scene.delta;
            if (rune.y < -40) {
                rune.y = viewHeight + 30;
                rune.x = Math.random() * viewWidth;
            }
            if (rune.x < -60) {
                rune.x = viewWidth + 40;
            } else if (rune.x > viewWidth + 60) {
                rune.x = -40;
            }
            // 靠近画面中心时压暗一点，避免在正文下方形成闪烁的噪点。
            const cx = (rune.x - viewWidth * 0.5) / (viewWidth * 0.5);
            const cy = (rune.y - viewHeight * 0.46) / (viewHeight * 0.5);
            const centerBias = clamp(Math.hypot(cx, cy) * 0.5, 0, 1);
            const alpha = rune.alpha * (0.55 + centerBias * 0.45) * scene.intensity;
            const sprite = glyphSprite(rune.glyph, rune.size, BRASS);
            if (sprite === null) {
                continue;
            }
            ctx.globalAlpha = clamp(alpha, 0, 1);
            ctx.save();
            ctx.translate(rune.x, rune.y);
            ctx.rotate(rune.angle);
            const drawSize = sprite.width / GLYPH_SPRITE_RATIO;
            ctx.drawImage(sprite, -drawSize * 0.5, -drawSize * 0.5, drawSize, drawSize);
            ctx.restore();
        }
        ctx.restore();
    }

    function spawnParticle(x: number, y: number, burst: boolean): void {
        if (!settings.trail || particles.length >= MAX_PARTICLES) {
            return;
        }
        const strength = settings.trailStrength;
        const angle = Math.random() * TAU;
        const speed = burst ? 20 + Math.random() * 42 * strength : 4 + Math.random() * 14;
        particles.push({
            x,
            y,
            vx: Math.cos(angle) * speed,
            vy: Math.sin(angle) * speed - (burst ? 0 : 6),
            size: (burst ? 12 : 9) + Math.random() * 12 * strength,
            glyph: randomRune(),
            age: 0,
            life: (burst ? 0.7 : 0.5) + Math.random() * 0.6,
            alpha: (burst ? 0.32 : 0.15) + 0.32 * strength,
            angle: (Math.random() - 0.5) * 1.2,
            spin: (Math.random() - 0.5) * 2.4,
            warm: Math.random() < 0.14,
        });
    }

    /** 指针移动：距离超过阈值才留下一枚符文，避免原地抖动刷屏。 */
    function onTrailMove(x: number, y: number, pressed: boolean): void {
        if (!settings.trail) {
            hasTrailOrigin = false;
            return;
        }
        dragging = pressed;
        if (!hasTrailOrigin) {
            lastTrailX = x;
            lastTrailY = y;
            hasTrailOrigin = true;
            return;
        }
        const dx = x - lastTrailX;
        const dy = y - lastTrailY;
        const distance = Math.hypot(dx, dy);
        // 密度越高，触发阈值越小、生成概率越高；上限仍受 MAX_PARTICLES 约束。
        const density = settings.trailDensity;
        if (distance < (pressed ? 10 : 16) / Math.max(0.4, density)) {
            return;
        }
        lastTrailX = x;
        lastTrailY = y;
        const count = pressed ? 2 : 1;
        const probability = clamp(0.35 + settings.trailStrength * 0.4 + (density - 0.55) * 0.35, 0, 1);
        for (let i = 0; i < count; i += 1) {
            if (Math.random() < probability) {
                spawnParticle(x, y, false);
            }
        }
    }

    /** 点击：只炸开一小圈，强度随设置缩放。 */
    function onPointerDown(x: number, y: number): void {
        if (!settings.trail) {
            return;
        }
        const count = Math.round((6 + settings.trailStrength * 10) * Math.min(1.5, settings.trailDensity + 0.45));
        for (let i = 0; i < count; i += 1) {
            spawnParticle(x, y, true);
        }
    }

    function drawPointer(scene: Scene): void {
        if (particles.length === 0) {
            return;
        }
        const ctx = scene.ctx;
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        const alive: FxParticle[] = [];
        for (const p of particles) {
            p.age += scene.delta;
            if (p.age >= p.life) {
                continue;
            }
            const progress = p.age / p.life;
            const fade = 1 - progress;
            p.x += p.vx * scene.delta;
            p.y += p.vy * scene.delta;
            p.vx *= 0.96;
            p.vy = p.vy * 0.96 - 8 * scene.delta;
            p.angle += p.spin * scene.delta;
            const alpha = p.alpha * fade * scene.intensity;
            // 拖尾符文同样走精灵缓存：逐枚 fillText + 切字体会让拖动时明显掉帧，
            // 大小变化交给 drawImage 缩放，观感几乎一致。
            const sprite = glyphSprite(p.glyph, p.size, p.warm ? OXBLOOD : BONE);
            if (sprite === null) {
                alive.push(p);
                continue;
            }
            const scale = 0.7 + fade * 0.5;
            const drawSize = (sprite.width / GLYPH_SPRITE_RATIO) * scale;
            ctx.globalAlpha = clamp(alpha, 0, 1);
            ctx.save();
            ctx.translate(p.x, p.y);
            ctx.rotate(p.angle);
            ctx.drawImage(sprite, -drawSize * 0.5, -drawSize * 0.5, drawSize, drawSize);
            ctx.restore();
            alive.push(p);
        }
        particles = alive;
        ctx.restore();
    }

    function refreshFromRegistry(): void {
        loadSettings();
        applySettings();
        window.dispatchEvent(new CustomEvent("abyss-fx-settings", { detail: { ...settings } }));
    }

    function init(): void {
        loadSettings();
        applySettings();
        window.addEventListener("theme-settings-change", refreshFromRegistry);
        window.addEventListener("storage", (event: StorageEvent) => {
            if (event.key === "trpg_theme_settings_v1") {
                refreshFromRegistry();
            }
        });
    }

    // 设置界面由 React 之外的静态页面提供，脚本加载完成时节点已经存在；
    // 若页面尚未解析完，则等 DOMContentLoaded 再绑定。
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }

    export const AbyssFx = {
        rebuild,
        drawAmbient,
        drawPointer,
        onTrailMove,
        onPointerDown,
        settings: (): FxSettings => ({ ...settings }),
        refresh: refreshFromRegistry,
    };
}