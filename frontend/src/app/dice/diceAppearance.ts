/**
 * 骰子外观：颜色与贴图选择只保存在本机 localStorage，不影响服务端结果
 * 或其他玩家画面。空字符串表示跟随当前主题（读取 CSS 变量）。
 */
const DICE_APPEARANCE_STORAGE_KEY = "trpg_dice_appearance";
const DICE_APPEARANCE_DEFAULTS: DiceAppearanceConfig = {
    body: "",
    text: "",
    table: "",
    edge: "",
    tableSkinId: "",
    diceSkinIds: {},
};
const DICE_APPEARANCE_PRESETS: Array<{ id: string; name: string; config: Partial<DiceAppearanceConfig> }> = [
    { id: "theme", name: "跟随主题", config: { body: "", text: "", table: "", edge: "" } },
    { id: "ivory", name: "象牙白", config: { body: "#f6f1e7", text: "#2b2a33", table: "#1d3a34", edge: "#d8b56d" } },
    { id: "obsidian", name: "黑曜石", config: { body: "#24242c", text: "#f4f1e8", table: "#111318", edge: "#8b5cf6" } },
    { id: "blood", name: "血月", config: { body: "#3b1418", text: "#ffe9e2", table: "#1b1013", edge: "#e05a4f" } },
    { id: "jade", name: "青玉", config: { body: "#dfeee4", text: "#15382c", table: "#0f2a24", edge: "#69d6a0" } },
];

let diceTextureLibraryCache: DiceTextureLibrary | null = null;
let diceTextureLibraryPromise: Promise<DiceTextureLibrary | null> | null = null;

function diceCloneAppearance(config: Partial<DiceAppearanceConfig> | null | undefined): DiceAppearanceConfig {
    return {
        body: String(config?.body ?? ""),
        text: String(config?.text ?? ""),
        table: String(config?.table ?? ""),
        edge: String(config?.edge ?? ""),
        tableSkinId: String(config?.tableSkinId ?? ""),
        diceSkinIds: { ...(config?.diceSkinIds || {}) },
    };
}

function diceLoadAppearance(): DiceAppearanceConfig {
    try {
        const raw = window.localStorage.getItem(DICE_APPEARANCE_STORAGE_KEY);
        return raw ? diceCloneAppearance(JSON.parse(raw) as Partial<DiceAppearanceConfig>) : diceCloneAppearance(DICE_APPEARANCE_DEFAULTS);
    } catch {
        return diceCloneAppearance(DICE_APPEARANCE_DEFAULTS);
    }
}

function diceSaveAppearance(config: Partial<DiceAppearanceConfig>): DiceAppearanceConfig {
    const normalized = diceCloneAppearance(config);
    try {
        window.localStorage.setItem(DICE_APPEARANCE_STORAGE_KEY, JSON.stringify(normalized));
    } catch (error) {
        console.warn("保存骰子外观失败", error);
    }
    return normalized;
}

function diceCssVariable(name: string, fallback: string): string {
    // 必须从 body 读取：主题变量定义在 body.theme-* 上，而 :root 的
    // --color-text / --color-primary 等别名在 :root 就已按默认主题求值，
    // 直接读别名会拿到错误主题的颜色。
    const scopes = [document.body, document.documentElement].filter(Boolean) as HTMLElement[];
    for (const scope of scopes) {
        const value = getComputedStyle(scope).getPropertyValue(name).trim();
        if (value) return value;
    }
    return fallback;
}

/** 把空值解析成当前主题下的实际颜色，供 CSS 与 3D 渲染器共用。 */
function diceResolvedColors(config: DiceAppearanceConfig): { body: string; text: string; table: string; edge: string } {
    const body = config.body || diceCssVariable("--theme-dice-body", "#f6f1e7");
    const themeText = diceCssVariable("--theme-text-primary", "#2b2a33");
    return {
        body,
        // 骰面数字始终按骰子本体颜色做对比度兜底，避免浅色骰面配上浅色主题字。
        text: config.text || diceReadableColor(body, themeText),
        table: config.table || diceCssVariable("--theme-dice-table", diceCssVariable("--theme-bg-panel-muted", "#1d3a34")),
        edge: config.edge || diceCssVariable("--theme-dice-accent", diceCssVariable("--theme-accent-primary", "#d8b56d")),
    };
}

/** 解析 #rgb / #rrggbb / rgb() / rgba()，失败时返回 null（主题变量可能是任意合法 CSS 颜色）。 */
function diceParseColor(value: string): [number, number, number] | null {
    const text = String(value || "").trim();
    const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
    if (hex) {
        const body = hex[1]!;
        if (body.length === 3) {
            return [0, 1, 2].map((index) => Number.parseInt(body[index]! + body[index]!, 16)) as [number, number, number];
        }
        return [
            Number.parseInt(body.slice(0, 2), 16),
            Number.parseInt(body.slice(2, 4), 16),
            Number.parseInt(body.slice(4, 6), 16),
        ];
    }
    const rgb = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i.exec(text);
    if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
    return null;
}

function diceRelativeLuminance(value: string): number | null {
    const rgb = diceParseColor(value);
    if (!rgb) return null;
    const [r, g, b] = rgb.map((channel) => {
        const normalized = Math.max(0, Math.min(255, channel)) / 255;
        return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
    }) as [number, number, number];
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function diceContrastRatio(foreground: string, background: string): number {
    const front = diceRelativeLuminance(foreground);
    const back = diceRelativeLuminance(background);
    if (front === null || back === null) return 0;
    return (Math.max(front, back) + 0.05) / (Math.min(front, back) + 0.05);
}

/** 把颜色按权重混向目标色，用于在保持色相的前提下提亮 / 压暗到可读对比度。 */
function diceMixColor(source: string, target: string, weight: number): string | null {
    const first = diceParseColor(source);
    const second = diceParseColor(target);
    if (!first || !second) return null;
    const mixed = first.map((channel, index) => Math.round(channel * (1 - weight) + second[index]! * weight));
    return `#${mixed.map((channel) => Math.max(0, Math.min(255, channel)).toString(16).padStart(2, "0")).join("")}`;
}

/**
 * 骰桌卡片文字颜色：优先使用主题指定的颜色，对比度不足时先保持色相提亮 / 压暗，
 * 仍不达标才退回黑 / 白，保证任何主题与自定义桌面颜色下都清晰可读。
 */
function diceReadableColor(background: string, preferred: string): string {
    if (diceContrastRatio(preferred, background) >= 4.5) return preferred;
    for (const weight of [0.2, 0.4, 0.6, 0.8]) {
        for (const target of ["#ffffff", "#000000"]) {
            const mixed = diceMixColor(preferred, target, weight);
            if (mixed && diceContrastRatio(mixed, background) >= 4.5) return mixed;
        }
    }
    return diceContrastRatio("#f6f1e7", background) >= diceContrastRatio("#1f2933", background) ? "#f6f1e7" : "#1f2933";
}

function diceAssetUrl(assetId: string): string {
    return `/api/dice-textures/assets/${encodeURIComponent(assetId)}`;
}

function diceTextureLibrary(forceReload = false): Promise<DiceTextureLibrary | null> {
    if (!forceReload && diceTextureLibraryCache) return Promise.resolve(diceTextureLibraryCache);
    if (!forceReload && diceTextureLibraryPromise) return diceTextureLibraryPromise;
    diceTextureLibraryPromise = TrpgApi.get<ApiResponse<DiceTextureLibrary>>("/api/dice-textures")
        .then((response) => {
            diceTextureLibraryCache = response?.data || { assets: [], table_skins: [], dice_skins: [] };
            return diceTextureLibraryCache;
        })
        .catch((error) => {
            console.warn("加载骰子贴图库失败", error);
            return null;
        })
        .finally(() => {
            diceTextureLibraryPromise = null;
        });
    return diceTextureLibraryPromise;
}

/** 骰型映射：只返回真实支持的骰型，d100 以两枚 d10 呈现。 */
function diceDieTypeFor(sides: number, mode: string): string {
    if (mode === "percentile") return "d10";
    const map: Record<number, string> = { 4: "d4", 6: "d6", 8: "d8", 10: "d10", 12: "d12", 20: "d20", 100: "d10" };
    return map[Number(sides)] || "";
}

/** 取当前骰型已选皮肤的面贴图 URL 映射；未绑定的面返回空。 */
function diceFaceUrls(dieType: string, skinIds: Record<string, string>): Record<string, string> {
    const skinId = skinIds[dieType];
    const library = diceTextureLibraryCache;
    if (!skinId || !library) return {};
    const skin = library.dice_skins.find((item) => item.id === skinId && item.enabled);
    if (!skin) return {};
    const urls: Record<string, string> = {};
    Object.entries(skin.faces || {}).forEach(([face, assetId]) => {
        if (assetId) urls[face] = diceAssetUrl(assetId);
    });
    return urls;
}

function diceTableSkinUrl(tableSkinId: string): string {
    const library = diceTextureLibraryCache;
    if (!tableSkinId || !library) return "";
    const skin = library.table_skins.find((item) => item.id === tableSkinId && item.enabled);
    return skin ? diceAssetUrl(skin.asset_id) : "";
}

/** 将外观写入容器 CSS 变量；桌面贴图作为背景图，失败时保持纯色不降级。 */
function diceApplyAppearance(element: HTMLElement, config: DiceAppearanceConfig): void {
    const colors = diceResolvedColors(config);
    const themeText = diceCssVariable("--theme-dice-table-text", diceCssVariable("--theme-text-primary", "#f6f1e7"));
    element.style.setProperty("--dice-body-color", colors.body);
    element.style.setProperty("--dice-text-color", colors.text);
    element.style.setProperty("--dice-table-color", colors.table);
    element.style.setProperty("--dice-edge-color", colors.edge);
    // 卡片文字（标签、状态、结果）跟随主题，并按桌面底色做对比度兜底；
    // 骰面文字仍使用 --dice-text-color，避免浅色骰面被主题色盖掉。
    element.style.setProperty("--dice-ui-text-color", diceReadableColor(colors.table, themeText));
    element.style.setProperty("--dice-result-color", diceReadableColor(colors.table, colors.edge));
    // 成功 / 失败 / 警告等状态色同样按桌面底色兜底，避免浅色主题的深绿、深红糊在深色桌面上。
    element.style.setProperty("--dice-success-color", diceReadableColor(colors.table, diceCssVariable("--theme-success", "#5ad48a")));
    element.style.setProperty("--dice-danger-color", diceReadableColor(colors.table, diceCssVariable("--theme-danger", "#ff8a7a")));
    element.style.setProperty("--dice-warning-color", diceReadableColor(colors.table, diceCssVariable("--theme-warning", "#ffd166")));
    const tableUrl = diceTableSkinUrl(config.tableSkinId);
    if (tableUrl) {
        element.style.setProperty("--dice-table-image", `url("${tableUrl}")`);
        element.classList.add("has-table-texture");
    } else {
        element.style.removeProperty("--dice-table-image");
        element.classList.remove("has-table-texture");
    }
}

let diceThemeObserverBound = false;

/** 主题切换会替换 body 上的 theme-* class，此时重新解析变量让已存在的骰桌立即换色。 */
function diceWatchTheme(): void {
    if (diceThemeObserverBound || typeof MutationObserver === "undefined" || !document.body) return;
    diceThemeObserverBound = true;
    let scheduled = false;
    const observer = new MutationObserver(() => {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(() => {
            scheduled = false;
            document.querySelectorAll<HTMLElement>(".dice-inline").forEach((element) => {
                diceApplyAppearance(element, diceLoadAppearance());
            });
        });
    });
    observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });
}
