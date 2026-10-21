/**
 * 用户骰子外观设置：颜色、桌面贴图与骰子皮肤只保存在本机 localStorage。
 * 表单在「用户设置 → 骰子外观」中渲染，保存后立即应用到已存在的投掷桌。
 */
(function initDiceAppearanceSettings(global: Window) {
    "use strict";

    const CONTAINER_ID = "diceAppearanceSettings";
    const DIE_TYPES = ["d4", "d6", "d8", "d10", "d12", "d20", "d100"];
    const COLOR_FIELDS: Array<{ key: keyof DiceAppearanceConfig & ("body" | "text" | "table" | "edge"); label: string }> = [
        { key: "body", label: "骰身颜色" },
        { key: "text", label: "点数颜色" },
        { key: "table", label: "桌面底色" },
        { key: "edge", label: "结果强调色" },
    ];

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function save(config: DiceAppearanceConfig): DiceAppearanceConfig {
        const saved = diceSaveAppearance(config);
        document.querySelectorAll<HTMLElement>(".dice-table-card").forEach((card) => diceApplyAppearance(card, saved));
        return saved;
    }

    function activePreset(config: DiceAppearanceConfig): string {
        const match = DICE_APPEARANCE_PRESETS.find((preset) => {
            const presetConfig = diceCloneAppearance(preset.config);
            return presetConfig.body === config.body
                && presetConfig.text === config.text
                && presetConfig.table === config.table
                && presetConfig.edge === config.edge;
        });
        return match ? match.id : "custom";
    }

    function optionsFor(values: Array<{ value: string; label: string }>, selected: string): string {
        return values.map((item) => `<option value="${escapeHtml(item.value)}"${item.value === selected ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
    }

    function render(): void {
        const container = document.getElementById(CONTAINER_ID);
        if (!container) return;
        const config = diceLoadAppearance();
        const colors = diceResolvedColors(config);
        const library = diceTextureLibraryCache;
        const preset = activePreset(config);
        const presetValues = DICE_APPEARANCE_PRESETS.map((item) => ({ value: item.id, label: item.name }));
        presetValues.push({ value: "custom", label: "自定义" });

        const colorRows = COLOR_FIELDS.map((field) => {
            const follow = !config[field.key];
            return `
                <div class="dice-appearance-row" data-color-row="${field.key}">
                    <span class="dice-appearance-label">${escapeHtml(field.label)}</span>
                    <label class="form-check form-check-inline dice-appearance-follow">
                        <input class="form-check-input" type="checkbox" data-follow="${field.key}"${follow ? " checked" : ""}>
                        <span class="form-check-label">跟随主题</span>
                    </label>
                    <input type="color" class="form-control form-control-color" data-color="${field.key}" value="${escapeHtml(colors[field.key])}"${follow ? " disabled" : ""}>
                </div>`;
        }).join("");

        const tableSkins = library ? library.table_skins.filter((item) => item.enabled) : [];
        const tableOptions = optionsFor(
            [{ value: "", label: "无（纯色桌面）" }].concat(tableSkins.map((item) => ({ value: item.id, label: item.name }))),
            config.tableSkinId,
        );

        const diceSkinRows = DIE_TYPES.map((dieType) => {
            const skins = library ? library.dice_skins.filter((item) => item.enabled && item.die_type === dieType) : [];
            const selected = config.diceSkinIds[dieType] || "";
            const values = [{ value: "", label: "无（纯色骰子）" }].concat(skins.map((item) => ({ value: item.id, label: item.name })));
            return `
                <label class="dice-appearance-row">
                    <span class="dice-appearance-label">${dieType.toUpperCase()} 皮肤</span>
                    <select class="form-select form-select-sm" data-dice-skin="${dieType}">${optionsFor(values, selected)}</select>
                </label>`;
        }).join("");

        container.innerHTML = `
            <div class="dice-appearance-form">
                <div class="dice-appearance-row">
                    <span class="dice-appearance-label">配色方案</span>
                    <select class="form-select form-select-sm" data-preset>${optionsFor(presetValues, preset)}</select>
                </div>
                ${colorRows}
                <div class="dice-appearance-row">
                    <span class="dice-appearance-label">桌面贴图</span>
                    <select class="form-select form-select-sm" data-table-skin>${tableOptions}</select>
                </div>
                <details class="dice-appearance-skins">
                    <summary>骰子逐面皮肤</summary>
                    ${diceSkinRows}
                </details>
                <div class="dice-appearance-actions">
                    <button type="button" class="btn btn-primary btn-sm" data-save>保存外观</button>
                    <button type="button" class="btn btn-outline-secondary btn-sm" data-reset>恢复默认</button>
                    <span class="text-muted small">仅保存在当前浏览器，不影响其他玩家。</span>
                </div>
            </div>
        `;
        container.dataset.rendered = "1";
        bind(container);
    }

    function bind(container: HTMLElement): void {
        container.querySelectorAll<HTMLInputElement>("[data-follow]").forEach((checkbox) => {
            checkbox.addEventListener("change", () => {
                const key = checkbox.dataset.follow as "body" | "text" | "table" | "edge";
                const colorInput = container.querySelector<HTMLInputElement>(`[data-color="${key}"]`);
                if (colorInput) colorInput.disabled = checkbox.checked;
            });
        });
        container.querySelector<HTMLSelectElement>("[data-preset]")?.addEventListener("change", (event) => {
            const id = (event.target as HTMLSelectElement).value;
            if (id === "custom") return;
            const preset = DICE_APPEARANCE_PRESETS.find((item) => item.id === id);
            if (!preset) return;
            save(diceCloneAppearance(preset.config));
            render();
        });
        container.querySelector<HTMLElement>("[data-save]")?.addEventListener("click", () => {
            const current = diceLoadAppearance();
            const next = diceCloneAppearance(current);
            COLOR_FIELDS.forEach((field) => {
                const follow = container.querySelector<HTMLInputElement>(`[data-follow="${field.key}"]`);
                const color = container.querySelector<HTMLInputElement>(`[data-color="${field.key}"]`);
                next[field.key] = follow?.checked || !color ? "" : color.value;
            });
            next.tableSkinId = container.querySelector<HTMLSelectElement>("[data-table-skin]")?.value || "";
            next.diceSkinIds = {};
            container.querySelectorAll<HTMLSelectElement>("[data-dice-skin]").forEach((select) => {
                const dieType = select.dataset.diceSkin || "";
                if (dieType && select.value) next.diceSkinIds[dieType] = select.value;
            });
            save(next);
            global.showNotification?.("骰子外观已保存到当前浏览器", "success");
            render();
        });
        container.querySelector<HTMLElement>("[data-reset]")?.addEventListener("click", () => {
            save(diceCloneAppearance(DICE_APPEARANCE_DEFAULTS));
            global.showNotification?.("骰子外观已恢复默认", "info");
            render();
        });
    }

    async function init(): Promise<void> {
        const container = document.getElementById(CONTAINER_ID);
        if (!container || container.dataset.rendered === "1") return;
        await diceTextureLibrary();
        render();
    }

    function open(): void {
        document.querySelector<HTMLElement>('[data-profile-target="profile-dice-appearance"]')?.click();
        void init();
    }

    global.TrpgDiceAppearanceSettings = { init, open };
})(window);
