/**
 * 主题设置面板（schema 驱动）
 *
 * 管理员与个人共用同一套控件生成逻辑：
 * - slider / color / select / toggle 四类控件；
 * - 每行提供「恢复默认」；
 * - 个人面板按站点 __locked 显示锁定状态；
 * - 管理员面板额外提供每个主题的「允许个人使用」与站点默认参数。
 *
 * 所有拖动预览都经过 requestAnimationFrame 节流，只在保存时写入 localStorage。
 */
namespace ThemeSettingsUI {
    type ThemeValue = ThemeRegistry.ThemeValue;
    type ThemeValues = ThemeRegistry.ThemeValues;
    type ThemeDefinition = ThemeRegistry.ThemeDefinition;

    interface PersonalApi {
        initPersonal(onChange: () => void): void;
        initAdmin(): void;
        syncAdminFromInputs(): void;
        resetPersonalDraft(): void;
        commitPersonal(themeId: string): void;
        personalDirtyCount(): number;
        previewSelection(themeId: string): void;
    }

    const personalDraft: Record<string, ThemeValues> = {};
    const personalSaved: Record<string, ThemeValues> = {};
    let personalInitialized = false;
    let personalChangeHandler: (() => void) | null = null;
    let previewThemeId = "";
    let previewRaf = 0;

    let adminInitialized = false;
    let adminEnabled: string[] = [];
    let adminValues: Record<string, ThemeValues> = {};
    let adminLocks: Record<string, string[]> = {};

    function translate(key: string, fallback: string): string {
        const i18n = (window as any).TrpgI18n as { t?: (name: string) => string } | undefined;
        const translated = i18n?.t?.(key);
        return translated !== undefined && translated !== key ? translated : fallback;
    }

    function element<K extends keyof HTMLElementTagNameMap>(
        tag: K,
        className?: string,
    ): HTMLElementTagNameMap[K] {
        const node = document.createElement(tag);
        if (className !== undefined) {
            node.className = className;
        }
        return node;
    }

    function normalizeHex(value: string): string | null {
        const raw = value.trim().toLowerCase();
        if (/^#[0-9a-f]{6}$/.test(raw)) {
            return raw;
        }
        if (/^#[0-9a-f]{3}$/.test(raw)) {
            return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}`;
        }
        return null;
    }

    function cloneValues(source: Record<string, ThemeValues>): Record<string, ThemeValues> {
        const copy: Record<string, ThemeValues> = {};
        for (const [themeId, values] of Object.entries(source)) {
            copy[themeId] = { ...values };
        }
        return copy;
    }

    function rawPersonalOverrides(): Record<string, ThemeValues> {
        const result: Record<string, ThemeValues> = {};
        for (const definition of ThemeRegistry.definitions()) {
            const overrides: ThemeValues = {};
            const stored = ThemeRegistry.getPersonalOverrideValues(definition.id);
            for (const key of Object.keys(stored)) {
                const value = stored[key];
                if (value !== undefined) {
                    overrides[key] = value;
                }
            }
            result[definition.id] = overrides;
        }
        return result;
    }

    function ensureDraft(): void {
        if (personalInitialized) {
            return;
        }
        const source = rawPersonalOverrides();
        for (const [themeId, values] of Object.entries(source)) {
            personalSaved[themeId] = { ...values };
            personalDraft[themeId] = { ...values };
        }
        personalInitialized = true;
    }

    function displayValues(themeId: string): ThemeValues {
        const definition = ThemeRegistry.getDefinition(themeId);
        const values = ThemeRegistry.defaultValues(themeId);
        if (!definition) {
            return values;
        }
        const locked = ThemeRegistry.getSiteLockedKeys(themeId);
        for (const field of definition.settings) {
            const siteValue = ThemeRegistry.getSiteValue(themeId, field.key);
            if (siteValue !== null) {
                values[field.key] = siteValue;
            }
            const draft = personalDraft[themeId];
            if (
                draft !== undefined
                && Object.prototype.hasOwnProperty.call(draft, field.key)
                && !locked.includes(field.key)
            ) {
                values[field.key] = ThemeRegistry.coerceValue(field, draft[field.key]);
            }
        }
        return values;
    }

    function siteDefaultTheme(): string {
        const manager = (window as any).configManager as {
            get?: (configName: string, section: string, key: string, fallback?: string) => unknown;
        } | undefined;
        const value = manager?.get?.("general", "appearance", "theme", "light");
        return typeof value === "string" && value !== "" ? value : "light";
    }

    function themeLabel(definition: ThemeDefinition): string {
        return translate(definition.labelKey, definition.id);
    }

    function renderSwatches(definition: ThemeDefinition): HTMLElement {
        const preview = element("span", "theme-card-swatch");
        preview.style.setProperty("--swatch-bg", definition.preview.bg);
        preview.style.setProperty("--swatch-panel", definition.preview.panel);
        preview.style.setProperty("--swatch-text", definition.preview.text);
        preview.style.setProperty("--swatch-accent", definition.preview.accent);
        preview.style.setProperty("--swatch-border", definition.preview.border);
        const panel = element("span", "theme-card-swatch-panel");
        const bar = element("span", "theme-card-swatch-bar");
        const dot = element("span", "theme-card-swatch-dot");
        panel.append(bar, dot);
        preview.append(panel);
        return preview;
    }

    function buildCard(
        definition: ThemeDefinition,
        selected: boolean,
        enabled: boolean,
        mode: "admin" | "personal",
        onSelect: () => void,
        onAvailability?: (next: boolean) => void,
    ): HTMLElement {
        const card = element("div", `theme-card${selected ? " is-current" : ""}${enabled ? "" : " is-disabled"}`);
        const selectButton = element("button", "theme-card-select");
        selectButton.type = "button";
        selectButton.setAttribute("aria-pressed", selected ? "true" : "false");
        selectButton.disabled = mode === "personal" && !enabled;
        selectButton.addEventListener("click", onSelect);
        selectButton.append(renderSwatches(definition));

        const name = element("span", "theme-card-name");
        name.textContent = themeLabel(definition);
        selectButton.append(name);

        const description = element("span", "theme-card-desc");
        description.textContent = translate(definition.descriptionKey, "");
        selectButton.append(description);

        if (selected) {
            const badge = element("span", "theme-card-badge theme-card-badge-current");
            badge.textContent = translate("theme.badge.current", "当前");
            selectButton.append(badge);
        }
        if (!enabled) {
            const badge = element("span", "theme-card-badge theme-card-badge-locked");
            badge.textContent = translate("theme.badge.admin_disabled", "管理员已禁用");
            selectButton.append(badge);
        }
        card.append(selectButton);

        if (mode === "admin" && onAvailability) {
            const availability = element("label", "theme-card-availability");
            const checkbox = element("input");
            checkbox.type = "checkbox";
            checkbox.checked = enabled;
            checkbox.addEventListener("change", () => onAvailability(checkbox.checked));
            const text = element("span");
            text.textContent = translate("theme.availability.allow", "允许个人使用");
            availability.append(checkbox, text);
            card.append(availability);
        }
        return card;
    }

    function renderFieldRow(
        field: ThemeRegistry.ThemeSettingField,
        value: ThemeValue,
        mode: "admin" | "personal",
        locked: boolean,
        onChange: (next: ThemeValue) => void,
        onReset: () => void,
        onLock?: (next: boolean) => void,
    ): HTMLElement {
        const row = element("div", "theme-field");
        const head = element("div", "theme-field-head");
        const label = element("span", "theme-field-label");
        label.textContent = translate(field.labelKey, field.key);
        head.append(label);

        if (mode === "personal" && locked) {
            const badge = element("span", "theme-field-locked");
            badge.textContent = translate("theme.badge.admin_locked", "管理员锁定");
            head.append(badge);
        }
        if (mode === "admin" && onLock) {
            const lockLabel = element("label", "theme-field-lock");
            const checkbox = element("input");
            checkbox.type = "checkbox";
            checkbox.checked = locked;
            checkbox.addEventListener("change", () => onLock(checkbox.checked));
            const text = element("span");
            text.textContent = translate("theme.field.lock", "锁定");
            lockLabel.append(checkbox, text);
            head.append(lockLabel);
        }
        const reset = element("button", "theme-field-reset");
        reset.type = "button";
        reset.title = translate("theme.action.reset", "恢复默认");
        reset.setAttribute("aria-label", translate("theme.action.reset", "恢复默认"));
        reset.textContent = translate("theme.action.reset", "恢复默认");
        reset.addEventListener("click", onReset);
        head.append(reset);
        row.append(head);

        const control = element("div", "theme-field-control");
        if (field.type === "slider") {
            const range = element("input", "theme-range") as HTMLInputElement;
            range.type = "range";
            range.min = String(field.min ?? 0);
            range.max = String(field.max ?? 1);
            range.step = String(field.step ?? 0.01);
            range.value = String(value);
            const output = element("output", "theme-range-output");
            const format = (): string => {
                const numeric = Number(range.value);
                if (field.display === "percent") {
                    return `${Math.round(numeric * 100)}%`;
                }
                if (field.display === "px") {
                    return `${Math.round(numeric)}px`;
                }
                return `${numeric}`;
            };
            output.textContent = format();
            range.disabled = mode === "personal" && locked;
            range.addEventListener("input", () => {
                output.textContent = format();
                onChange(Number(range.value));
            });
            control.append(range, output);
        } else if (field.type === "color") {
            const color = element("input", "theme-color") as HTMLInputElement;
            color.type = "color";
            color.value = String(value);
            const hex = element("input", "theme-color-hex form-control form-control-sm") as HTMLInputElement;
            hex.type = "text";
            hex.value = String(value);
            hex.spellcheck = false;
            const error = element("span", "theme-field-error");
            error.hidden = true;
            color.disabled = mode === "personal" && locked;
            hex.disabled = mode === "personal" && locked;
            color.addEventListener("input", () => {
                hex.value = color.value;
                error.hidden = true;
                onChange(color.value);
            });
            hex.addEventListener("change", () => {
                const normalized = normalizeHex(hex.value);
                if (normalized === null) {
                    error.hidden = false;
                    error.textContent = translate("theme.error.invalid_color", "颜色格式无效，已恢复原值（#rrggbb）");
                    hex.value = color.value;
                    return;
                }
                error.hidden = true;
                color.value = normalized;
                hex.value = normalized;
                onChange(normalized);
            });
            control.append(color, hex, error);
        } else if (field.type === "select") {
            const select = element("select", "form-select form-select-sm theme-select") as HTMLSelectElement;
            select.disabled = mode === "personal" && locked;
            for (const option of field.options || []) {
                const optionNode = element("option") as HTMLOptionElement;
                optionNode.value = option.value;
                optionNode.textContent = translate(option.labelKey, option.value);
                select.append(optionNode);
            }
            select.value = String(value);
            select.addEventListener("change", () => onChange(select.value));
            control.append(select);
        } else {
            const toggleLabel = element("label", "theme-toggle");
            const checkbox = element("input") as HTMLInputElement;
            checkbox.type = "checkbox";
            checkbox.checked = Boolean(value);
            checkbox.disabled = mode === "personal" && locked;
            checkbox.addEventListener("change", () => onChange(checkbox.checked));
            const text = element("span");
            text.textContent = translate("theme.field.enabled", "启用");
            toggleLabel.append(checkbox, text);
            control.append(toggleLabel);
        }
        row.append(control);
        return row;
    }

    function contrastRatio(foreground: string, background: string): number {
        const luminance = (hex: string): number => {
            const channels = [
                parseInt(hex.slice(1, 3), 16),
                parseInt(hex.slice(3, 5), 16),
                parseInt(hex.slice(5, 7), 16),
            ].map((channel) => {
                const value = channel / 255;
                return value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * (channels[0] ?? 0) + 0.7152 * (channels[1] ?? 0) + 0.0722 * (channels[2] ?? 0);
        };
        const first = luminance(foreground);
        const second = luminance(background);
        const lighter = Math.max(first, second);
        const darker = Math.min(first, second);
        return (lighter + 0.05) / (darker + 0.05);
    }

    function renderContrast(values: ThemeValues): HTMLElement {
        const candidateBackground = values.bgPanel ?? values.backgroundColor;
        const candidateText = values.textPrimary ?? values.textColor;
        const background = (typeof candidateBackground === "string" ? normalizeHex(candidateBackground) : null) || "#ffffff";
        const text = (typeof candidateText === "string" ? normalizeHex(candidateText) : null) || "#000000";
        const ratio = contrastRatio(text, background);
        const node = element("p", `theme-contrast${ratio < 4.5 ? " is-warning" : ""}`);
        node.textContent = `${translate("theme.contrast.label", "正文对比度")}: ${ratio.toFixed(2)}:1`;
        if (ratio < 4.5) {
            node.textContent += ` · ${translate("theme.contrast.warning", "低于 4.5:1，建议调整文字或背景")}`;
        }
        return node;
    }

    function renderPanelHeader(definition: ThemeDefinition, subtitleKey: string, subtitle: string): HTMLElement {
        const header = element("div", "theme-detail-header");
        const title = element("h5");
        title.textContent = themeLabel(definition);
        const hint = element("p", "theme-detail-hint");
        hint.textContent = translate(subtitleKey, subtitle);
        header.append(title, hint);
        return header;
    }

    function renderAdminDetail(themeId: string): void {
        const container = document.getElementById("themeAdminDetail");
        if (container === null) {
            return;
        }
        container.replaceChildren();
        const definition = ThemeRegistry.getDefinition(themeId);
        if (!definition) {
            return;
        }
        container.append(
            renderPanelHeader(
                definition,
                "theme.detail.site_default_hint",
                "这些值作为站点默认下发；个人覆盖值优先，锁定项个人不可改。",
            ),
        );
        if (!ThemeRegistry.isThemeEnabled(themeId)) {
            const warning = element("p", "theme-detail-warning");
            warning.textContent = translate("theme.warning.disabled_edit", "该主题当前未允许个人使用，保存后个人将无法选择它。");
            container.append(warning);
        }
        if (definition.settings.length === 0) {
            const empty = element("p", "theme-detail-hint");
            empty.textContent = translate("theme.detail.no_settings", "该主题没有可调参数。");
            container.append(empty);
            return;
        }

        const effective: ThemeValues = ThemeRegistry.defaultValues(themeId);
        const override = adminValues[themeId] || {};
        const locked = adminLocks[themeId] || [];
        for (const field of definition.settings) {
            if (Object.prototype.hasOwnProperty.call(override, field.key)) {
                const next = override[field.key];
                if (next !== undefined) {
                    effective[field.key] = next;
                }
            }
        }
        const list = element("div", "theme-field-list");
        for (const field of definition.settings) {
            const row = renderFieldRow(
                field,
                effective[field.key] ?? field.defaultValue,
                "admin",
                locked.includes(field.key),
                (next: ThemeValue): void => {
                    adminValues[themeId] = {
                        ...(adminValues[themeId] || {}),
                        [field.key]: ThemeRegistry.coerceValue(field, next),
                    };
                    writeAdminInputs();
                },
                (): void => {
                    if (adminValues[themeId]) {
                        delete adminValues[themeId][field.key];
                    }
                    writeAdminInputs();
                    renderAdminDetail(themeId);
                },
                (next: boolean): void => {
                    const current = new Set(adminLocks[themeId] || []);
                    if (next) {
                        current.add(field.key);
                    } else {
                        current.delete(field.key);
                    }
                    adminLocks[themeId] = Array.from(current);
                    writeAdminInputs();
                },
            );
            list.append(row);
        }
        container.append(renderContrast(effective), list);

        const resetAll = element("button", "btn btn-outline-secondary btn-sm theme-reset-all");
        resetAll.type = "button";
        resetAll.textContent = translate("theme.action.reset_all", "全部恢复默认");
        resetAll.addEventListener("click", () => {
            delete adminValues[themeId];
            delete adminLocks[themeId];
            writeAdminInputs();
            renderAdminDetail(themeId);
        });
        container.append(resetAll);
    }

    function renderAdminGrid(): void {
        const grid = document.getElementById("themeAdminPicker");
        const select = document.getElementById("themeSelect") as HTMLSelectElement | null;
        if (grid === null || select === null) {
            return;
        }
        const selectedId = select.value || "light";
        grid.replaceChildren();
        for (const definition of ThemeRegistry.definitions()) {
            const enabled = adminEnabled.includes(definition.id);
            const card = buildCard(
                definition,
                definition.id === selectedId,
                enabled,
                "admin",
                () => {
                    if (!enabled) {
                        const nextEnabled = adminEnabled.includes(definition.id)
                            ? adminEnabled
                            : [...adminEnabled, definition.id];
                        adminEnabled = nextEnabled;
                        writeAdminInputs();
                    }
                    select.value = definition.id;
                    renderAdminGrid();
                    renderAdminDetail(definition.id);
                    previewAdminTheme(definition.id);
                },
                (next: boolean): void => {
                    if (next) {
                        if (!adminEnabled.includes(definition.id)) {
                            adminEnabled = [...adminEnabled, definition.id];
                        }
                    } else {
                        adminEnabled = adminEnabled.filter((id) => id !== definition.id);
                    }
                    writeAdminInputs();
                    renderAdminGrid();
                },
            );
            grid.append(card);
        }
    }

    function renderAdmin(): void {
        renderAdminGrid();
        const select = document.getElementById("themeSelect") as HTMLSelectElement | null;
        if (select !== null) {
            renderAdminDetail(select.value || "light");
        }
    }

    function parseAdminOverrides(raw: string): void {
        adminValues = {};
        adminLocks = {};
        if (raw.trim() === "") {
            return;
        }
        let parsed: unknown;
        try {
            parsed = JSON.parse(raw);
        } catch {
            return;
        }
        if (typeof parsed !== "object" || parsed === null) {
            return;
        }
        const table = parsed as Record<string, unknown>;
        for (const definition of ThemeRegistry.definitions()) {
            const entry = table[definition.id];
            if (typeof entry !== "object" || entry === null) {
                continue;
            }
            const record = entry as Record<string, unknown>;
            const locked = Array.isArray(record.__locked)
                ? record.__locked.filter((key): key is string => typeof key === "string")
                : [];
            adminLocks[definition.id] = locked;
            const values: ThemeValues = {};
            for (const field of definition.settings) {
                if (Object.prototype.hasOwnProperty.call(record, field.key)) {
                    values[field.key] = ThemeRegistry.coerceValue(field, record[field.key]);
                }
            }
            adminValues[definition.id] = values;
        }
    }

    function writeAdminInputs(): void {
        const enabledInput = document.getElementById("enabledThemesInput") as HTMLInputElement | null;
        const overridesInput = document.getElementById("themeOverridesInput") as HTMLInputElement | null;
        if (enabledInput !== null) {
            enabledInput.value = adminEnabled.join(",");
            enabledInput.dispatchEvent(new Event("change", { bubbles: true }));
        }
        if (overridesInput !== null) {
            const payload: Record<string, unknown> = {};
            for (const definition of ThemeRegistry.definitions()) {
                const values = adminValues[definition.id] || {};
                const locked = adminLocks[definition.id] || [];
                if (Object.keys(values).length === 0 && locked.length === 0) {
                    continue;
                }
                payload[definition.id] = { ...values, __locked: locked };
            }
            overridesInput.value = JSON.stringify(payload);
            overridesInput.dispatchEvent(new Event("change", { bubbles: true }));
        }
    }

    function readAdminInputs(): void {
        const enabledInput = document.getElementById("enabledThemesInput") as HTMLInputElement | null;
        const overridesInput = document.getElementById("themeOverridesInput") as HTMLInputElement | null;
        const rawEnabled = enabledInput?.value ?? "";
        const parsed = rawEnabled
            .split(",")
            .map((item) => item.trim())
            .filter((item) => item !== "" && ThemeRegistry.getDefinition(item) !== null);
        adminEnabled = parsed.length > 0 ? parsed : ThemeRegistry.definitions().map((definition) => definition.id);
        parseAdminOverrides(overridesInput?.value ?? "{}");
    }

    export function syncAdminFromInputs(): void {
        if (!adminInitialized) {
            return;
        }
        readAdminInputs();
        renderAdmin();
    }

    export function initAdmin(): void {
        if (adminInitialized) {
            return;
        }
        adminInitialized = true;
        readAdminInputs();
        const select = document.getElementById("themeSelect") as HTMLSelectElement | null;
        if (select !== null) {
            select.addEventListener("change", () => {
                renderAdminGrid();
                renderAdminDetail(select.value || "light");
                previewAdminTheme(select.value || "light");
            });
        }
        renderAdmin();
    }

    function adminPreviewValues(themeId: string): ThemeValues {
        const resolved = themeId === "system" ? ThemeRegistry.resolveSystemTheme() : themeId;
        const definition = ThemeRegistry.getDefinition(resolved);
        const values = ThemeRegistry.defaultValues(resolved);
        const override = adminValues[resolved] || {};
        if (definition !== null) {
            for (const field of definition.settings) {
                const next = override[field.key];
                if (next !== undefined) {
                    values[field.key] = ThemeRegistry.coerceValue(field, next);
                }
            }
        }
        return values;
    }

    /** 管理员选主题时即时预览站点默认效果，保存或取消后由 applyTheme 还原。 */
    function previewAdminTheme(themeId: string): void {
        const manager = (window as any).configManager as { previewTheme?: (theme: string) => void } | undefined;
        manager?.previewTheme?.(themeId);
        ThemeRegistry.activate(themeId, adminPreviewValues(themeId), true);
    }

    function selectedPersonalTheme(): string {
        const select = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        const value = select?.value ?? "";
        return value === "" ? siteDefaultTheme() : value;
    }

    function schedulePreview(themeId: string): void {
        if (previewRaf !== 0) {
            window.cancelAnimationFrame(previewRaf);
        }
        previewRaf = window.requestAnimationFrame(() => {
            previewRaf = 0;
            ThemeRegistry.applyPreview(themeId, valuesFor(themeId));
        });
    }

    function valuesFor(requested: string): ThemeValues {
        const resolved = requested === "system" ? ThemeRegistry.resolveSystemTheme() : requested;
        return displayValues(resolved);
    }

    function previewSelection(themeId: string): void {
        const requested = themeId === "" ? siteDefaultTheme() : themeId;
        const manager = (window as any).configManager as { previewTheme?: (theme: string) => void } | undefined;
        manager?.previewTheme?.(requested);
        previewThemeId = requested;
        ThemeRegistry.activate(requested, valuesFor(requested), true);
    }

    export function renderPersonal(): void {
        if (!personalInitialized) {
            return;
        }
        const grid = document.getElementById("profileThemePicker");
        const select = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        if (grid === null || select === null) {
            return;
        }
        grid.replaceChildren();
        const defaultTheme = ThemeRegistry.getDefinition(siteDefaultTheme());
        if (defaultTheme !== null) {
            const defaultCard = buildCard(
                defaultTheme,
                select.value === "",
                ThemeRegistry.isThemeEnabled(siteDefaultTheme()),
                "personal",
                () => {
                    select.value = "";
                    select.dispatchEvent(new Event("change", { bubbles: true }));
                    previewSelection("");
                    renderPersonal();
                },
            );
            const name = defaultCard.querySelector(".theme-card-name");
            if (name !== null) {
                name.textContent = translate("profile.theme.follow_default", "跟随默认主题");
            }
            const desc = defaultCard.querySelector(".theme-card-desc");
            if (desc !== null) {
                desc.textContent = `${translate("theme.badge.site_default", "站点默认")}: ${themeLabel(defaultTheme)}`;
            }
            const badge = defaultCard.querySelector(".theme-card-badge-current");
            if (badge !== null) {
                badge.textContent = translate("theme.badge.following", "跟随中");
            }
            grid.append(defaultCard);
        }
        for (const definition of ThemeRegistry.definitions()) {
            const enabled = ThemeRegistry.isThemeEnabled(definition.id);
            const card = buildCard(
                definition,
                select.value === definition.id,
                enabled,
                "personal",
                () => {
                    if (!enabled) {
                        return;
                    }
                    select.value = definition.id;
                    select.dispatchEvent(new Event("change", { bubbles: true }));
                    previewSelection(definition.id);
                    renderPersonal();
                    renderPersonalDetail(definition.id);
                },
            );
            grid.append(card);
        }
        renderPersonalDetail(selectedPersonalTheme());
        if (personalChangeHandler !== null) {
            personalChangeHandler();
        }
    }

    function renderPersonalDetail(themeId: string): void {
        const container = document.getElementById("profileThemeDetail");
        if (container === null) {
            return;
        }
        container.replaceChildren();
        const definition = ThemeRegistry.getDefinition(themeId);
        if (definition === null) {
            return;
        }
        const followingDefault = (document.getElementById("profileThemeSelect") as HTMLSelectElement | null)?.value === "";
        container.append(
            renderPanelHeader(
                definition,
                followingDefault ? "theme.detail.following_default_hint" : "theme.detail.personal_hint",
                followingDefault
                    ? `当前跟随站点默认（${themeLabel(definition)}）；下面的调整仍只影响你的个人覆盖值。`
                    : "这些参数只保存在你的浏览器，保存后生效；站点默认值作为基线。",
            ),
        );
        if (!ThemeRegistry.isThemeEnabled(themeId)) {
            const warning = element("p", "theme-detail-warning");
            warning.textContent = translate(
                "theme.warning.theme_disabled",
                "管理员已禁用该主题，保存后会自动回退到可用主题。",
            );
            container.append(warning);
        }
        if (definition.settings.length === 0) {
            const empty = element("p", "theme-detail-hint");
            empty.textContent = translate("theme.detail.system_no_settings", "跟随系统会按系统深浅色自动切换，没有额外参数。");
            container.append(empty);
            return;
        }
        const values = displayValues(themeId);
        const locked = ThemeRegistry.getSiteLockedKeys(themeId);
        const list = element("div", "theme-field-list");
        for (const field of definition.settings) {
            const row = renderFieldRow(
                field,
                values[field.key] ?? field.defaultValue,
                "personal",
                locked.includes(field.key),
                (next: ThemeValue): void => {
                    if (locked.includes(field.key)) {
                        return;
                    }
                    personalDraft[themeId] = {
                        ...(personalDraft[themeId] || {}),
                        [field.key]: ThemeRegistry.coerceValue(field, next),
                    };
                    schedulePreview(themeId);
                    if (personalChangeHandler !== null) {
                        personalChangeHandler();
                    }
                },
                (): void => {
                    if (personalDraft[themeId]) {
                        delete personalDraft[themeId][field.key];
                    }
                    schedulePreview(themeId);
                    renderPersonalDetail(themeId);
                    if (personalChangeHandler !== null) {
                        personalChangeHandler();
                    }
                },
            );
            list.append(row);
        }
        container.append(renderContrast(values), list);

        const resetAll = element("button", "btn btn-outline-secondary btn-sm theme-reset-all");
        resetAll.type = "button";
        resetAll.textContent = translate("theme.action.reset_all", "全部恢复默认");
        resetAll.addEventListener("click", () => {
            personalDraft[themeId] = {};
            schedulePreview(themeId);
            renderPersonalDetail(themeId);
            if (personalChangeHandler !== null) {
                personalChangeHandler();
            }
        });
        container.append(resetAll);
    }

    export function initPersonal(onChange: () => void): void {
        personalChangeHandler = onChange;
        if (personalInitialized) {
            renderPersonal();
            return;
        }
        ensureDraft();
        const select = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        if (select !== null) {
            select.addEventListener("change", () => {
                previewSelection(select.value);
                renderPersonal();
            });
        }
        previewThemeId = selectedPersonalTheme();
        renderPersonal();
        previewSelection(select?.value ?? "");
    }

    export function resetPersonalDraft(): void {
        for (const [themeId, values] of Object.entries(personalSaved)) {
            personalDraft[themeId] = { ...values };
        }
        const select = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        previewThemeId = selectedPersonalTheme();
        if (select !== null) {
            renderPersonal();
        }
    }

    export function commitPersonal(themeId: string): void {
        ThemeRegistry.savePersonal(themeId, personalDraft);
        for (const definition of ThemeRegistry.definitions()) {
            personalSaved[definition.id] = { ...(personalDraft[definition.id] || {}) };
        }
    }

    export function personalDirtyCount(): number {
        let count = 0;
        for (const definition of ThemeRegistry.definitions()) {
            const saved = personalSaved[definition.id] || {};
            const draft = personalDraft[definition.id] || {};
            const keys = new Set([...Object.keys(saved), ...Object.keys(draft)]);
            for (const key of keys) {
                if (saved[key] !== draft[key]) {
                    count += 1;
                }
            }
        }
        return count;
    }

    export function currentPreviewTheme(): string {
        return previewThemeId;
    }

    const api: PersonalApi = {
        initPersonal,
        initAdmin,
        syncAdminFromInputs,
        resetPersonalDraft,
        commitPersonal,
        personalDirtyCount,
        previewSelection,
    };
    (window as any).TrpgThemeSettings = api;
    (window as any).TrpgThemeSettings.renderPersonal = renderPersonal;
}
