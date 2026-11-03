/**
 * 主题能力契约（ThemeDefinition）
 *
 * 所有主题（浅色、深色、赛博档案、深渊帷幕、秘典）都通过这一份接口注册，
 * 设置面板由 schema 生成控件，主题模块只负责把值落到自己的 CSS 变量或运行时上。
 *
 * 层叠上下文约定（重要）：
 * 1. 所有浮层（头像上拉窗、世界意志弹窗、配图灯箱、通知等）统一挂到 document.body；
 * 2. 主题不得在浮层的祖先元素上建立会困住浮层的层叠上下文，
 *    包括 transform / filter / backdrop-filter / perspective / contain: paint /
 *    isolation: isolate / will-change / z-index。
 *
 * 数据优先级：字段默认值 < 站点默认（管理员） < 个人覆盖。
 * 管理员可以在站点默认里用 __locked 锁定字段，此时个人值被忽略。
 */
namespace ThemeRegistry {
    export type ThemeValue = string | number | boolean;
    export type ThemeValues = Record<string, ThemeValue>;

    export type ThemeSettingType = "slider" | "color" | "select" | "toggle";

    export interface ThemeSettingOption {
        value: string;
        labelKey: string;
    }

    export interface ThemeSettingField {
        key: string;
        type: ThemeSettingType;
        labelKey: string;
        defaultValue: ThemeValue;
        /** slider 专用：范围、步进与展示格式。 */
        min?: number;
        max?: number;
        step?: number;
        display?: "percent" | "px" | "plain";
        /** select 专用。 */
        options?: ThemeSettingOption[];
        /** 可选的补充说明。 */
        hintKey?: string;
    }

    export interface ThemeTokens {
        bg: string;
        panel: string;
        text: string;
        accent: string;
        border: string;
    }

    export interface ThemeOverlayPolicy {
        /** 浮层宿主，契约固定为 body。 */
        host: "body";
        /** 祖先元素上禁止出现的属性（会新建层叠上下文）。 */
        forbiddenAncestorStyles: string[];
        note: string;
    }

    export type ReducedMotionBehavior = "freeze" | "none";

    export interface ThemeDefinition {
        id: string;
        /** 写入 body 的主题 class；system 由运行环境解析成 light/dark。 */
        bodyClass: string;
        labelKey: string;
        descriptionKey: string;
        /** 主题卡片的缩略色板。 */
        preview: ThemeTokens;
        /** 契约要求每个主题都声明深浅两套 token，便于浅色/深色适配与测试兜底。 */
        tokens: { light: ThemeTokens; dark: ThemeTokens };
        supportsLightAndDark: boolean;
        animated: boolean;
        reducedMotion: ReducedMotionBehavior;
        overlayPolicy: ThemeOverlayPolicy;
        settings: ThemeSettingField[];
        mount(): void;
        unmount(): void;
        apply(values: ThemeValues): void;
    }

    export const OVERLAY_POLICY: ThemeOverlayPolicy = {
        host: "body",
        forbiddenAncestorStyles: [
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain: paint",
            "isolation: isolate",
            "will-change",
            "z-index",
        ],
        note: "所有浮层挂载到 document.body；主题不得在浮层祖先上建立层叠上下文，否则 fixed 浮层会被困住（如早期 .col-10 的 z-index 问题）。",
    };

    const STORE_KEY = "trpg_theme_settings_v1";
    const BACKUP_KEY = "trpg_theme_settings_legacy_backup";
    const LEGACY_ABYSS_KEY = "trpg_abyss_fx_settings";
    const LEGACY_CYBER_KEY = "trpg_cyber_fx_settings";

    const TOKEN_VARS: Array<[keyof ThemeTokens, string]> = [
        ["bg", "--theme-bg-page"],
        ["panel", "--theme-bg-panel"],
        ["text", "--theme-text-primary"],
        ["accent", "--theme-accent-primary"],
        ["border", "--theme-border-subtle"],
    ];

    function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
        const parsed = typeof value === "number" ? value : Number(value);
        if (!Number.isFinite(parsed)) {
            return fallback;
        }
        return Math.min(max, Math.max(min, parsed));
    }

    function normalizeHex(value: unknown): string | null {
        if (typeof value !== "string") {
            return null;
        }
        const raw = value.trim().toLowerCase();
        if (/^#[0-9a-f]{6}$/.test(raw)) {
            return raw;
        }
        if (/^#[0-9a-f]{3}$/.test(raw)) {
            return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}`;
        }
        return null;
    }

    function hexToRgb(hex: string): [number, number, number] {
        return [
            parseInt(hex.slice(1, 3), 16),
            parseInt(hex.slice(3, 5), 16),
            parseInt(hex.slice(5, 7), 16),
        ];
    }

    function shadeHex(hex: string, amount: number): string {
        const [r, g, b] = hexToRgb(hex);
        const mix = (channel: number): number => {
            const target = amount < 0 ? 0 : 255;
            return Math.round(channel + (target - channel) * Math.abs(amount));
        };
        return `#${mix(r).toString(16).padStart(2, "0")}${mix(g).toString(16).padStart(2, "0")}${mix(b).toString(16).padStart(2, "0")}`;
    }

    function setBodyVar(name: string, value: string): void {
        if (document.body !== null) {
            document.body.style.setProperty(name, value);
        }
    }

    function clearBodyVars(names: string[]): void {
        if (document.body === null) {
            return;
        }
        for (const name of names) {
            document.body.style.removeProperty(name);
        }
    }

    function applyTokenValues(values: ThemeValues): void {
        for (const [key, variable] of TOKEN_VARS) {
            const value = normalizeHex(values[key]);
            if (value !== null) {
                setBodyVar(variable, value);
            }
        }
        const accent = normalizeHex(values.accentPrimary);
        if (accent !== null) {
            setBodyVar("--theme-accent-primary-hover", shadeHex(accent, -0.14));
        }
        const border = normalizeHex(values.borderSubtle);
        if (border !== null) {
            setBodyVar("--theme-border-strong", shadeHex(border, -0.16));
        }
        const panel = normalizeHex(values.bgPanel);
        if (panel !== null) {
            setBodyVar("--theme-bg-panel-muted", shadeHex(panel, -0.05));
        }
        const textSecondary = normalizeHex(values.textSecondary);
        if (textSecondary !== null) {
            setBodyVar("--theme-text-secondary", textSecondary);
            setBodyVar("--theme-text-muted", shadeHex(textSecondary, 0.18));
        }
    }

    function clearTokenValues(): void {
        clearBodyVars([
            "--theme-bg-page",
            "--theme-bg-panel",
            "--theme-bg-panel-muted",
            "--theme-text-primary",
            "--theme-text-secondary",
            "--theme-text-muted",
            "--theme-accent-primary",
            "--theme-accent-primary-hover",
            "--theme-border-subtle",
            "--theme-border-strong",
        ]);
    }

    const CYBER_VARS = [
        "--cyber-ink",
        "--cyber-phosphor",
        "--theme-bg-page",
        "--theme-text-primary",
        "--theme-text-secondary",
        "--theme-accent-primary",
        "--theme-accent-primary-hover",
    ];

    function applyCyberColors(values: ThemeValues): void {
        const primary = normalizeHex(values.primaryColor);
        const background = normalizeHex(values.backgroundColor);
        const text = normalizeHex(values.textColor);
        if (primary !== null) {
            setBodyVar("--cyber-phosphor", primary);
            setBodyVar("--theme-accent-primary", primary);
            setBodyVar("--theme-accent-primary-hover", shadeHex(primary, -0.14));
        }
        if (background !== null) {
            setBodyVar("--cyber-ink", background);
            setBodyVar("--theme-bg-page", background);
        }
        if (text !== null) {
            setBodyVar("--theme-text-primary", text);
            setBodyVar("--theme-text-secondary", shadeHex(text, 0.2));
        }
    }

    function noop(): void {
        // 主题模块自行监听 theme-settings-change 事件，这里无需额外动作。
    }

    function tokenDefinition(config: {
        id: string;
        bodyClass: string;
        labelKey: string;
        descriptionKey: string;
        preview: ThemeTokens;
        tokens: { light: ThemeTokens; dark: ThemeTokens };
        supportsLightAndDark: boolean;
        settings: ThemeSettingField[];
    }): ThemeDefinition {
        return {
            ...config,
            animated: false,
            reducedMotion: "none",
            overlayPolicy: OVERLAY_POLICY,
            mount: noop,
            unmount: clearTokenValues,
            apply(values: ThemeValues): void {
                applyTokenValues(values);
            },
        };
    }

    const lightTokens: ThemeTokens = {
        bg: "#eef2f6",
        panel: "#ffffff",
        text: "#1f2933",
        accent: "#2f6fed",
        border: "#c9d4df",
    };

    const darkTokens: ThemeTokens = {
        bg: "#0f1319",
        panel: "#161c24",
        text: "#e6edf4",
        accent: "#4cc9f0",
        border: "#2b3542",
    };

    const cyberDarkTokens: ThemeTokens = {
        bg: "#05080b",
        panel: "#0a1116",
        text: "#d7fff1",
        accent: "#35f6c3",
        border: "#1d4b41",
    };

    const cyberLightTokens: ThemeTokens = {
        bg: "#e4f7f0",
        panel: "#f2fffb",
        text: "#0c2b23",
        accent: "#0b8f74",
        border: "#9fd8c9",
    };

    const abyssTokens: ThemeTokens = {
        bg: "#0b0a0c",
        panel: "#14101a",
        text: "#e8e0f0",
        accent: "#b28cff",
        border: "#3a2f49",
    };

    const abyssLightTokens: ThemeTokens = {
        bg: "#e9e3ef",
        panel: "#f7f2fa",
        text: "#241a2f",
        accent: "#6d3fd1",
        border: "#c2b4d6",
    };

    const tomeTokens: ThemeTokens = {
        bg: "#efe2c4",
        panel: "#fbf3dd",
        text: "#3a2c1b",
        accent: "#8a5a2b",
        border: "#c9b08a",
    };

    const tomeDarkTokens: ThemeTokens = {
        bg: "#1b1710",
        panel: "#241d14",
        text: "#efe2c4",
        accent: "#d1a565",
        border: "#5a4932",
    };

    const colorField = (key: string, defaultValue: string): ThemeSettingField => ({
        key,
        type: "color",
        labelKey: `theme.field.${key}`,
        defaultValue,
    });

    const sliderField = (
        key: string,
        defaultValue: number,
        min: number,
        max: number,
        step: number,
        display: "percent" | "px" | "plain" = "plain",
    ): ThemeSettingField => ({
        key,
        type: "slider",
        labelKey: `theme.field.${key}`,
        defaultValue,
        min,
        max,
        step,
        display,
    });

    const toggleField = (key: string, defaultValue: boolean): ThemeSettingField => ({
        key,
        type: "toggle",
        labelKey: `theme.field.${key}`,
        defaultValue,
    });

    const paletteFields = (tokens: ThemeTokens, textSecondary: string): ThemeSettingField[] => [
        colorField("bgPage", tokens.bg),
        colorField("bgPanel", tokens.panel),
        colorField("textPrimary", tokens.text),
        colorField("textSecondary", textSecondary),
        colorField("accentPrimary", tokens.accent),
        colorField("borderSubtle", tokens.border),
    ];

    const DEFINITIONS: Record<string, ThemeDefinition> = {
        light: tokenDefinition({
            id: "light",
            bodyClass: "theme-light",
            labelKey: "profile.theme.light",
            descriptionKey: "theme.detail.light",
            preview: lightTokens,
            tokens: { light: lightTokens, dark: darkTokens },
            supportsLightAndDark: true,
            settings: paletteFields(lightTokens, "#5b6773"),
        }),
        dark: tokenDefinition({
            id: "dark",
            bodyClass: "theme-dark",
            labelKey: "profile.theme.dark",
            descriptionKey: "theme.detail.dark",
            preview: darkTokens,
            tokens: { light: lightTokens, dark: darkTokens },
            supportsLightAndDark: true,
            settings: paletteFields(darkTokens, "#9aa7b4"),
        }),
        system: {
            id: "system",
            bodyClass: "system",
            labelKey: "profile.theme.system",
            descriptionKey: "theme.detail.system",
            preview: darkTokens,
            tokens: { light: lightTokens, dark: darkTokens },
            supportsLightAndDark: true,
            animated: false,
            reducedMotion: "none",
            overlayPolicy: OVERLAY_POLICY,
            settings: [],
            mount: noop,
            unmount: noop,
            apply: noop,
        },
        pattern_cyber_2: {
            id: "pattern_cyber_2",
            bodyClass: "theme-cyber-2",
            labelKey: "profile.theme.cyber",
            descriptionKey: "theme.detail.cyber",
            preview: cyberDarkTokens,
            tokens: { light: cyberLightTokens, dark: cyberDarkTokens },
            supportsLightAndDark: false,
            animated: true,
            reducedMotion: "freeze",
            overlayPolicy: OVERLAY_POLICY,
            settings: [
                toggleField("enabled", true),
                sliderField("intensity", 0.68, 0.2, 1, 0.01, "percent"),
                sliderField("rainDensity", 0.8, 0.3, 1, 0.01, "percent"),
                sliderField("rainSpeed", 1, 0.5, 2, 0.05),
                {
                    key: "charset",
                    type: "select",
                    labelKey: "theme.field.charset",
                    defaultValue: "mixed",
                    options: [
                        { value: "katakana", labelKey: "theme.charset.katakana" },
                        { value: "symbols", labelKey: "theme.charset.symbols" },
                        { value: "hex", labelKey: "theme.charset.hex" },
                        { value: "binary", labelKey: "theme.charset.binary" },
                        { value: "mixed", labelKey: "theme.charset.mixed" },
                    ],
                },
                colorField("primaryColor", cyberDarkTokens.accent),
                colorField("backgroundColor", cyberDarkTokens.bg),
                colorField("textColor", cyberDarkTokens.text),
                toggleField("reticle", true),
            ],
            mount: noop,
            unmount(): void {
                clearBodyVars(CYBER_VARS);
            },
            apply(values: ThemeValues): void {
                applyCyberColors(values);
            },
        },
        pattern_coc: {
            id: "pattern_coc",
            bodyClass: "theme-coc",
            labelKey: "profile.theme.coc",
            descriptionKey: "theme.detail.abyss",
            preview: abyssTokens,
            tokens: { light: abyssLightTokens, dark: abyssTokens },
            supportsLightAndDark: false,
            animated: true,
            reducedMotion: "freeze",
            overlayPolicy: OVERLAY_POLICY,
            settings: [
                sliderField("chatAlpha", 0.6, 0.3, 1, 0.01, "percent"),
                sliderField("sidebarAlpha", 0.5, 0.3, 1, 0.01, "percent"),
                sliderField("railAlpha", 0.7, 0.3, 1, 0.01, "percent"),
                sliderField("veilAlpha", 0.55, 0.25, 0.95, 0.01, "percent"),
                sliderField("veilBlur", 10, 0, 24, 1, "px"),
                toggleField("trail", true),
                sliderField("trailStrength", 0.55, 0.15, 1, 0.01, "percent"),
                sliderField("trailDensity", 0.55, 0.2, 1.5, 0.05),
                toggleField("driftRunes", true),
                sliderField("tentacleDensity", 1, 0.4, 1.4, 0.05),
                sliderField("tentacleHeight", 1, 0.6, 1.4, 0.05),
                sliderField("tentacleHeightVariance", 0.2, 0, 0.5, 0.05),
                sliderField("swaySpeed", 1, 0.4, 1.6, 0.05),
                sliderField("sigilScale", 1, 0.7, 1.3, 0.05),
            ],
            mount: noop,
            unmount: noop,
            apply: noop,
        },
        pattern_tome: tokenDefinition({
            id: "pattern_tome",
            bodyClass: "theme-tome",
            labelKey: "profile.theme.tome",
            descriptionKey: "theme.detail.tome",
            preview: tomeTokens,
            tokens: { light: tomeTokens, dark: tomeDarkTokens },
            supportsLightAndDark: false,
            settings: paletteFields(tomeTokens, "#6b5636"),
        }),
    };

    const ORDER = ["light", "dark", "system", "pattern_cyber_2", "pattern_coc", "pattern_tome"];

    export function definitions(): ThemeDefinition[] {
        return ORDER.map((id) => DEFINITIONS[id] as ThemeDefinition);
    }

    export function getDefinition(id: string): ThemeDefinition | null {
        return DEFINITIONS[id] || null;
    }

    export function defaultValues(themeId: string): ThemeValues {
        const definition = DEFINITIONS[themeId];
        const values: ThemeValues = {};
        if (!definition) {
            return values;
        }
        for (const field of definition.settings) {
            values[field.key] = field.defaultValue;
        }
        return values;
    }

    export function coerceValue(field: ThemeSettingField, value: unknown): ThemeValue {
        if (field.type === "slider") {
            return clampNumber(value, field.min ?? 0, field.max ?? 1, Number(field.defaultValue));
        }
        if (field.type === "toggle") {
            return typeof value === "boolean" ? value : Boolean(field.defaultValue);
        }
        if (field.type === "color") {
            return normalizeHex(value) ?? String(field.defaultValue);
        }
        if (field.type === "select") {
            const allowed = (field.options || []).map((option) => option.value);
            return typeof value === "string" && allowed.includes(value) ? value : String(field.defaultValue);
        }
        return field.defaultValue;
    }

    /** 只保留实际出现过的键：避免保存时把站点默认值整份复制成个人覆盖。 */
    function normalizeOverrides(themeId: string, input: unknown): ThemeValues {
        const definition = DEFINITIONS[themeId];
        const values: ThemeValues = {};
        if (!definition || typeof input !== "object" || input === null) {
            return values;
        }
        const source = input as Record<string, unknown>;
        for (const field of definition.settings) {
            if (Object.prototype.hasOwnProperty.call(source, field.key)) {
                values[field.key] = coerceValue(field, source[field.key]);
            }
        }
        return values;
    }

    interface SiteOverride {
        values: ThemeValues;
        locked: string[];
    }

    let siteEnabled: string[] = ORDER.slice();
    let siteOverrides: Record<string, SiteOverride> = {};
    let personalTheme = "";
    let personalValues: Record<string, ThemeValues> = {};

    function readJson(key: string): unknown {
        try {
            const raw = window.localStorage.getItem(key);
            return raw === null ? null : JSON.parse(raw);
        } catch {
            return null;
        }
    }

    function writeJson(key: string, value: unknown): void {
        try {
            window.localStorage.setItem(key, JSON.stringify(value));
        } catch {
            // 隐私模式写入失败：本次会话仍然生效，只是下次打开不再记忆。
        }
    }

    function initPersonalStore(): void {
        const raw = readJson(STORE_KEY);
        if (raw !== null && typeof raw === "object") {
            const data = raw as {
                theme?: unknown;
                values?: unknown;
            };
            personalTheme = typeof data.theme === "string" ? data.theme : "";
            if (typeof data.values === "object" && data.values !== null) {
                const source = data.values as Record<string, unknown>;
                for (const id of ORDER) {
                    personalValues[id] = normalizeOverrides(id, source[id]);
                }
            }
            return;
        }
        migrateLegacySettings();
    }

    function migrateLegacySettings(): void {
        const legacyAbyss = readJson(LEGACY_ABYSS_KEY);
        const legacyCyber = readJson(LEGACY_CYBER_KEY);
        if (legacyAbyss === null && legacyCyber === null) {
            return;
        }
        const migrated: Record<string, ThemeValues> = {};
        if (legacyAbyss !== null && typeof legacyAbyss === "object") {
            migrated.pattern_coc = normalizeOverrides("pattern_coc", legacyAbyss);
        }
        if (legacyCyber !== null && typeof legacyCyber === "object") {
            migrated.pattern_cyber_2 = normalizeOverrides("pattern_cyber_2", legacyCyber);
        }
        writeJson(STORE_KEY, { version: 2, theme: "", values: migrated });
        writeJson(BACKUP_KEY, {
            migratedAt: new Date().toISOString(),
            abyss: legacyAbyss,
            cyber: legacyCyber,
        });
        try {
            window.localStorage.removeItem(LEGACY_ABYSS_KEY);
            window.localStorage.removeItem(LEGACY_CYBER_KEY);
        } catch {
            // 删除失败也不影响新配置读取，旧键只作为备份存在。
        }
        personalTheme = "";
        personalValues = migrated;
    }

    initPersonalStore();

    function parseEnabled(raw: unknown): string[] {
        const list: string[] = [];
        const push = (value: string): void => {
            const id = value.trim();
            if (id !== "" && DEFINITIONS[id] && !list.includes(id)) {
                list.push(id);
            }
        };
        if (Array.isArray(raw)) {
            raw.forEach((item) => {
                if (typeof item === "string") {
                    push(item);
                }
            });
        } else if (typeof raw === "string") {
            raw.split(",").forEach(push);
        }
        return list.length > 0 ? list : ORDER.slice();
    }

    function parseSiteOverrides(raw: unknown): Record<string, SiteOverride> {
        let source: unknown = raw;
        if (typeof raw === "string") {
            if (raw.trim() === "") {
                return {};
            }
            try {
                source = JSON.parse(raw);
            } catch {
                return {};
            }
        }
        const result: Record<string, SiteOverride> = {};
        if (typeof source !== "object" || source === null) {
            return result;
        }
        const table = source as Record<string, unknown>;
        for (const id of ORDER) {
            const entry = table[id];
            if (typeof entry !== "object" || entry === null) {
                continue;
            }
            const record = entry as Record<string, unknown>;
            const locked = Array.isArray(record.__locked)
                ? record.__locked.filter((key): key is string => typeof key === "string")
                : [];
            result[id] = {
                values: normalizeOverrides(id, record),
                locked,
            };
        }
        return result;
    }

    export function setSiteConfig(enabledRaw: unknown, overridesRaw: unknown): void {
        siteEnabled = parseEnabled(enabledRaw);
        siteOverrides = parseSiteOverrides(overridesRaw);
    }

    export function getEnabledThemes(): string[] {
        return siteEnabled.slice();
    }

    export function isThemeEnabled(themeId: string): boolean {
        return siteEnabled.includes(themeId);
    }

    export function getSiteLockedKeys(themeId: string): string[] {
        return siteOverrides[themeId]?.locked.slice() || [];
    }

    export function getSiteValue(themeId: string, key: string): ThemeValue | null {
        const override = siteOverrides[themeId];
        if (!override || !Object.prototype.hasOwnProperty.call(override.values, key)) {
            return null;
        }
        return override.values[key] ?? null;
    }

    export function getValues(themeId: string): ThemeValues {
        const definition = DEFINITIONS[themeId];
        const values = defaultValues(themeId);
        if (!definition) {
            return values;
        }
        const site = siteOverrides[themeId];
        const personal = personalValues[themeId];
        const locked = site?.locked || [];
        for (const field of definition.settings) {
            let next: ThemeValue | null = null;
            if (site && Object.prototype.hasOwnProperty.call(site.values, field.key)) {
                next = site.values[field.key] ?? null;
            }
            if (
                personal !== undefined
                && Object.prototype.hasOwnProperty.call(personal, field.key)
                && !locked.includes(field.key)
            ) {
                next = personal[field.key] ?? null;
            }
            if (next !== null && next !== undefined) {
                values[field.key] = coerceValue(field, next);
            }
        }
        // 未保存的即时预览优先，保证拖动滑杆时动画模块读到的是预览值。
        if (previewState !== null && previewState.themeId === themeId) {
            for (const field of definition.settings) {
                const previewValue = previewState.values[field.key];
                if (previewValue !== undefined) {
                    values[field.key] = coerceValue(field, previewValue);
                }
            }
        }
        return values;
    }

    export function getPersonalTheme(): string {
        return personalTheme;
    }

    export function getPersonalValues(themeId: string): ThemeValues {
        return getValues(themeId);
    }

    /** 只返回用户显式覆盖过的键，保存时不会把站点默认值整份复制成个人值。 */
    export function getPersonalOverrideValues(themeId: string): ThemeValues {
        return { ...(personalValues[themeId] || {}) };
    }

    export function savePersonal(themeId: string, values: Record<string, ThemeValues>): void {
        const sanitized: Record<string, ThemeValues> = {};
        for (const id of ORDER) {
            sanitized[id] = normalizeOverrides(id, values[id]);
        }
        personalTheme = themeId;
        personalValues = sanitized;
        writeJson(STORE_KEY, {
            version: 2,
            theme: themeId,
            values: sanitized,
            updatedAt: new Date().toISOString(),
        });
    }

    let activeThemeId = "";
    let activeResolvedId = "";
    /** 未保存的即时预览值：只在内存里生效，保存或取消时清空。 */
    let previewState: { themeId: string; values: ThemeValues } | null = null;

    export function resolveSystemTheme(): "light" | "dark" {
        try {
            return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        } catch {
            return "light";
        }
    }

    export function activate(themeId: string, overrideValues?: ThemeValues, preview = false): void {
        const resolved = themeId === "system" ? resolveSystemTheme() : themeId;
        const definition = DEFINITIONS[resolved];
        if (!definition) {
            return;
        }
        if (activeResolvedId !== "" && activeResolvedId !== resolved) {
            DEFINITIONS[activeResolvedId]?.unmount();
        }
        activeThemeId = themeId;
        activeResolvedId = resolved;
        definition.mount();
        if (!preview) {
            previewState = null;
        }
        const values = overrideValues ?? getValues(resolved);
        if (preview) {
            previewState = { themeId: resolved, values };
        }
        definition.apply(values);
        window.dispatchEvent(
            new CustomEvent("theme-settings-change", {
                detail: { themeId: resolved, requestedThemeId: themeId, preview },
            }),
        );
    }

    export function applyPreview(themeId: string, values: ThemeValues): void {
        const resolved = themeId === "system" ? resolveSystemTheme() : themeId;
        previewState = { themeId: resolved, values };
        DEFINITIONS[resolved]?.apply(values);
        window.dispatchEvent(
            new CustomEvent("theme-settings-change", {
                detail: { themeId: resolved, requestedThemeId: themeId, preview: true },
            }),
        );
    }

    export function active(): string {
        return activeThemeId;
    }
}
