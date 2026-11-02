/**
 * 「深渊帷幕」主题 · Canvas 引擎核心
 *
 * 设计约定：
 * - 采用 namespace + 独立 <script> 的分段结构（tsconfig.frontend.json 为 module:none，
 *   无法使用 import/export），由 js/themes/abyss/ 下的脚本按顺序加载。
 * - Canvas 位于 z-index:-1，只在 body.theme-coc 下启用，不接收任何指针事件。
 * - 所有随机与缓动工具集中在此，避免各绘制模块重复实现。
 */
namespace AbyssTheme {
    export const CANVAS_CLASS = "theme-abyss-canvas";
    export const THEME_CLASS = "theme-coc";

    export interface Viewport {
        width: number;
        height: number;
        dpr: number;
    }

    export interface Pointer {
        /** 归一化的指针横向位置，范围 [-1, 1]。 */
        x: number;
        /** 归一化的指针纵向位置，范围 [-1, 1]。 */
        y: number;
    }

    export interface Scene {
        ctx: CanvasRenderingContext2D;
        viewport: Viewport;
        /** 主题激活后的累计秒数，驱动全部周期动画。 */
        time: number;
        /** 距上一帧的秒数，用于匀速运动。 */
        delta: number;
        /** 阻尼平滑后的指针位置。 */
        pointer: Pointer;
        /** 激活渐入系数，0 → 1。 */
        intensity: number;
        /** 画质系数，低性能设备下调低细节密度。 */
        quality: number;
    }

    export interface Rgb {
        r: number;
        g: number;
        b: number;
    }

    export interface Point {
        x: number;
        y: number;
    }

    /** 各绘制层统一的对外接口。 */
    export interface CanvasLayerApi {
        rebuild(viewport: Viewport): void;
        draw(scene: Scene): void;
    }

    /**
     * 「渊红抄本」色板：以旧羊皮纸、氧化黄铜、干涸血红与瘀伤紫构成，
     * 刻意避开常见的青绿霓虹，让画面更像一本 1920 年代的深海调查档案。
     */
    /** 骨白：羊皮纸、眼睛巩膜与高光的颜色。 */
    export const BONE: Rgb = { r: 232, g: 224, b: 205 };
    /** 氧化黄铜：仪式线条、边饰与强调色。 */
    export const BRASS: Rgb = { r: 199, g: 162, b: 92 };
    /** 干涸血红：危险、瞳孔内缘与法阵脉冲。 */
    export const OXBLOOD: Rgb = { r: 176, g: 74, b: 66 };
    /** 瘀伤紫：非欧几里得几何与深渊阴影。 */
    export const PLUM: Rgb = { r: 124, g: 96, b: 158 };
    /** 极深的海渊底色，用于压边与剪影。 */
    export const ABYSS: Rgb = { r: 8, g: 7, b: 13 };

    /** 光晕精灵缓存：同色同尺寸只生成一次，避免逐帧 createRadialGradient。 */
    const glowCache = new Map<string, HTMLCanvasElement>();

    export function prefersReducedMotion(): boolean {
        return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    /** 主题是否处于激活状态（且用户未要求减少动态效果）。 */
    export function isThemeActive(): boolean {
        return document.body.classList.contains(THEME_CLASS) && !prefersReducedMotion();
    }

    export function randomBetween(min: number, max: number): number {
        return min + Math.random() * (max - min);
    }

    export function randomInt(min: number, max: number): number {
        return Math.floor(min + Math.random() * (max - min + 1));
    }

    /**
     * 会话种子随机数。
     *
     * 触手构型如果在每次 resize 时重新随机，用户拖动窗口就会看到整片腕足
     * 在眼前重排。这里把种子存进 sessionStorage：同一会话内保持构图，
     * 刷新页面或开新会话才换一批形态。各绘制层在 rebuild 时用
     * createSessionRandom() 取得一条从头开始、确定性的序列。
     */
    const SESSION_SEED_KEY = "trpg_abyss_session_seed";
    let sessionSeed = 0;

    export function getSessionSeed(): number {
        if (sessionSeed !== 0) {
            return sessionSeed;
        }
        let stored: string | null = null;
        try {
            stored = window.sessionStorage.getItem(SESSION_SEED_KEY);
        } catch {
            stored = null;
        }
        const parsed = stored === null ? Number.NaN : Number(stored);
        if (Number.isFinite(parsed) && parsed > 0) {
            sessionSeed = Math.floor(parsed);
        } else {
            sessionSeed = Math.floor(Math.random() * 0xfffffff0) + 16;
            try {
                window.sessionStorage.setItem(SESSION_SEED_KEY, String(sessionSeed));
            } catch {
                // 隐私模式下 sessionStorage 不可用：只影响“刷新后保持”，不影响画面。
            }
        }
        return sessionSeed;
    }

    /** 以会话种子 + 偏移量构造一个确定性的 0→1 伪随机序列（mulberry32）。 */
    export function createSessionRandom(offset = 0): () => number {
        let state = (getSessionSeed() ^ (offset * 0x9e3779b9)) >>> 0;
        return function next(): number {
            state = (state + 0x6d2b79f5) >>> 0;
            let value = state;
            value = Math.imul(value ^ (value >>> 15), value | 1);
            value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
            return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
        };
    }

    export function clamp(value: number, min: number, max: number): number {
        if (value < min) return min;
        if (value > max) return max;
        return value;
    }

    export function lerp(from: number, to: number, ratio: number): number {
        return from + (to - from) * ratio;
    }

    export function easeOutCubic(progress: number): number {
        const p = clamp(progress, 0, 1) - 1;
        return 1 + p * p * p;
    }

    /**
     * 全局潮汐节拍：所有图层共用的一个极缓呼吸系数。
     * 法阵、触手、眼睛、光柱都用它调制强度，画面才会像同一个场景在呼吸，
     * 而不是几个各自独立的动画叠在一起。
     */
    export function tide(time: number): number {
        return 1 + Math.sin(time * 0.62) * 0.022;
    }

    /** 潮汐的亮度分量：与 tide 同相位，用于统一各层亮度起伏。 */
    export function tideLight(time: number): number {
        return 0.5 + 0.5 * Math.sin(time * 0.62);
    }

    export function rgba(rgb: Rgb, alpha: number): string {
        return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
    }

    /** 生成（或复用）一张径向光晕贴图。 */
    export function glowSprite(rgb: Rgb, size: number): HTMLCanvasElement {
        const key = `${rgb.r}-${rgb.g}-${rgb.b}-${size}`;
        const cached = glowCache.get(key);
        if (cached !== undefined) {
            return cached;
        }
        const sprite = document.createElement("canvas");
        sprite.width = size;
        sprite.height = size;
        const spriteCtx = sprite.getContext("2d");
        if (spriteCtx) {
            const half = size / 2;
            const gradient = spriteCtx.createRadialGradient(half, half, 0, half, half, half);
            gradient.addColorStop(0, rgba(rgb, 1));
            gradient.addColorStop(0.2, rgba(rgb, 0.58));
            gradient.addColorStop(0.52, rgba(rgb, 0.16));
            gradient.addColorStop(1, rgba(rgb, 0));
            spriteCtx.fillStyle = gradient;
            spriteCtx.fillRect(0, 0, size, size);
        }
        glowCache.set(key, sprite);
        return sprite;
    }

    /** 以加法混合绘制一团光晕，让叠加处更亮而不是发灰。 */
    export function drawGlow(
        ctx: CanvasRenderingContext2D,
        rgb: Rgb,
        x: number,
        y: number,
        radius: number,
        alpha: number,
    ): void {
        if (alpha <= 0.002 || radius <= 0.4) {
            return;
        }
        const sprite = glowSprite(rgb, 64);
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = clamp(alpha, 0, 1);
        ctx.drawImage(sprite, x - radius, y - radius, radius * 2, radius * 2);
        ctx.restore();
    }
}
