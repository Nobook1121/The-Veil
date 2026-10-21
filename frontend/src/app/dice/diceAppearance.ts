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
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
}

/** 把空值解析成当前主题下的实际颜色，供 CSS 与 3D 渲染器共用。 */
function diceResolvedColors(config: DiceAppearanceConfig): { body: string; text: string; table: string; edge: string } {
    return {
        body: config.body || diceCssVariable("--color-surface", "#f6f1e7"),
        text: config.text || diceCssVariable("--color-text", "#2b2a33"),
        table: config.table || diceCssVariable("--color-surface-alt", "#1d3a34"),
        edge: config.edge || diceCssVariable("--color-primary", "#d8b56d"),
    };
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

function diceDieTypeFor(sides: number, mode: string): string {
    if (mode === "percentile") return "d10";
    const map: Record<number, string> = { 4: "d4", 6: "d6", 8: "d8", 10: "d10", 12: "d12", 20: "d20", 100: "d100" };
    return map[sides] || "d6";
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
    element.style.setProperty("--dice-body-color", colors.body);
    element.style.setProperty("--dice-text-color", colors.text);
    element.style.setProperty("--dice-table-color", colors.table);
    element.style.setProperty("--dice-edge-color", colors.edge);
    const tableUrl = diceTableSkinUrl(config.tableSkinId);
    if (tableUrl) {
        element.style.setProperty("--dice-table-image", `url("${tableUrl}")`);
        element.classList.add("has-table-texture");
    } else {
        element.style.removeProperty("--dice-table-image");
        element.classList.remove("has-table-texture");
    }
}
