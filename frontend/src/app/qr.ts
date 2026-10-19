(function initializeQrRenderer(global: Window) {
    "use strict";

    interface QrInstance {
        addData(data: string, mode?: string): void;
        make(): void;
        createSvgTag(options: Record<string, unknown>): string;
    }

    interface QrFactory {
        (typeNumber: number, errorCorrectionLevel: string): QrInstance;
    }

    interface RenderOptions {
        cellSize?: number;
        margin?: number;
        alt?: string;
    }

    /**
     * 生成二维码 SVG 字符串。依赖 js/vendor/qrcode.js 暴露的全局 `qrcode` 工厂。
     * typeNumber 传 0 表示自动选择最小可用版本；纠错等级 M，兼顾屏幕扫码容错与尺寸。
     */
    function renderSvg(text: string, options: RenderOptions = {}): string {
        const content = String(text || "").trim();
        if (!content) {
            throw new Error("二维码内容为空");
        }
        const factory = (global as unknown as { qrcode?: QrFactory }).qrcode;
        if (typeof factory !== "function") {
            throw new Error("二维码库未加载");
        }
        const qr = factory(0, "M");
        qr.addData(content);
        qr.make();
        const cellSize = options.cellSize ?? 5;
        return qr.createSvgTag({
            cellSize,
            margin: options.margin ?? cellSize * 4,
            alt: options.alt ? { text: options.alt } : undefined,
        });
    }

    global.TrpgQr = { renderSvg };
})(window);
