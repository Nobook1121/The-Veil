/**
 * 「赛博档案」主题 · 引擎核心
 *
 * 与「深渊帷幕」相同的组织方式：namespace + 独立 <script> 顺序加载
 * （tsconfig.frontend.json 为 module:none，不能使用 import/export）。
 *
 * 视觉定位：深空数据档案库 / 冷光调查终端。
 * 不是常见的「满屏绿色代码雨」，而是「档案柜的冷光 + 终端荧光 + 少量琥珀色归档标记」。
 */
namespace CyberTheme {
    export const CANVAS_CLASS = "theme-cyber-canvas";
    export const ATMOSPHERE_CLASS = "theme-cyber-atmosphere";
    export const THEME_CLASS = "theme-cyber-2";

    export interface Viewport {
        width: number;
        height: number;
        dpr: number;
    }

    export interface Pointer {
        /** 归一化指针横坐标，范围 [-1, 1]。 */
        x: number;
        /** 归一化指针纵坐标，范围 [-1, 1]。 */
        y: number;
        /** 指针是否在窗口内。 */
        inside: boolean;
    }

    export interface Scene {
        ctx: CanvasRenderingContext2D;
        viewport: Viewport;
        /** 主题激活后的累计秒数。 */
        time: number;
        /** 距上一帧的秒数。 */
        delta: number;
        pointer: Pointer;
        /** 激活渐入系数 0 → 1。 */
        intensity: number;
        /** 画质系数，低端设备下调细节密度。 */
        quality: number;
    }

    export interface Rgb {
        r: number;
        g: number;
        b: number;
    }

    export interface CanvasLayerApi {
        rebuild(viewport: Viewport): void;
        draw(scene: Scene): void;
    }

    /** 磷光青：主强调色，终端字符、扫描线与激活态。 */
    export const PHOSPHOR: Rgb = { r: 94, g: 240, b: 200 };
    /** 冷光冰蓝：次级强调，用于数据流、坐标与冷光边缘。 */
    export const ICE: Rgb = { r: 110, g: 168, b: 255 };
    /** 档案琥珀：归档标记、危险预警与编号印章。 */
    export const AMBER: Rgb = { r: 242, g: 180, b: 71 };
    /** 警戒红：错误与高优先级。 */
    export const ALERT: Rgb = { r: 255, g: 107, b: 127 };
    /** 数据深渊底色。 */
    export const INK: Rgb = { r: 4, g: 10, b: 13 };

    const glowCache = new Map<string, HTMLCanvasElement>();

    export function prefersReducedMotion(): boolean {
        return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    export function isThemeActive(): boolean {
        return document.body.classList.contains(THEME_CLASS) && !prefersReducedMotion();
    }

    export function clamp(value: number, min: number, max: number): number {
        if (!Number.isFinite(value)) return min;
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

    export function randomBetween(min: number, max: number): number {
        return min + Math.random() * (max - min);
    }

    export function randomInt(min: number, max: number): number {
        return Math.floor(min + Math.random() * (max - min + 1));
    }

    export function rgba(rgb: Rgb, alpha: number): string {
        return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
    }

    /**
     * 会话种子：HUD 刻度、背景档案碎片的位置在同一个浏览器会话里保持稳定，
     * 拖动窗口改变尺寸时不会整片重排。刷新页面才会换一批构图。
     */
    const SESSION_SEED_KEY = "trpg_cyber_session_seed";
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
                // 隐私模式下 sessionStorage 不可用，只影响「刷新后保持」。
            }
        }
        return sessionSeed;
    }

    /** 以会话种子派生确定性伪随机序列（mulberry32）。 */
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
        if (spriteCtx !== null) {
            const half = size / 2;
            const gradient = spriteCtx.createRadialGradient(half, half, 0, half, half, half);
            gradient.addColorStop(0, rgba(rgb, 1));
            gradient.addColorStop(0.24, rgba(rgb, 0.56));
            gradient.addColorStop(0.55, rgba(rgb, 0.16));
            gradient.addColorStop(1, rgba(rgb, 0));
            spriteCtx.fillStyle = gradient;
            spriteCtx.fillRect(0, 0, size, size);
        }
        glowCache.set(key, sprite);
        return sprite;
    }

    export function drawGlow(
        ctx: CanvasRenderingContext2D,
        rgb: Rgb,
        x: number,
        y: number,
        radius: number,
        alpha: number,
    ): void {
        if (alpha <= 0.003 || radius <= 0.4) {
            return;
        }
        const sprite = glowSprite(rgb, 64);
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = clamp(alpha, 0, 1);
        ctx.drawImage(sprite, x - radius, y - radius, radius * 2, radius * 2);
        ctx.restore();
    }

    /** 全局心跳节拍：让扫描线、HUD 与数据雨共享一个极缓的呼吸，避免各层各跳各的。 */
    export function pulse(time: number): number {
        return 0.5 + 0.5 * Math.sin(time * 0.9);
    }

    /** 十六进制数据碎片文本，用于背景档案碎片与浮层标记。 */
    export function hexLabel(length: number): string {
        const alphabet = "0123456789ABCDEF";
        let text = "";
        for (let index = 0; index < length; index += 1) {
            text += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
        }
        return text;
    }
}
