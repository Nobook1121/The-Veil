/**
 * 设置页辅助：
 * 1. 说明文字默认收起，聚焦输入框或点击 ⓘ 时再展开，避免整页说明堆叠；
 * 2. 提供设置搜索：匹配设置名称与说明文字，支持连续包含与子序列模糊匹配，
 *    点击结果自动切到对应分类并定位高亮。
 */
const SettingsAssistant = (() => {
    interface SettingEntry {
        tab: string;
        host: HTMLElement;
        title: string;
        hint: string;
        section: string;
        kind: "setting" | "section";
    }

    const EXCLUDED = [
        ".permission-settings-panel",
        ".permission-matrix",
        ".user-management-panel",
        ".role-config-settings",
        ".settings-search",
        ".table-responsive",
        ".modal",
        "#adminDiceTextures",
        "#ai-platforms-container",
        "#aiPlatformsContainer",
        "#knowledgeList",
        "[data-settings-assist='off']",
    ].join(", ");
    const HINT_SELECTOR = "small.form-text, small.text-muted, .form-text";
    const HIGHLIGHT_CLASS = "settings-search-highlight";

    let initialized = false;

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function textOf(element: Element | null): string {
        if (!element) return "";
        return (element.textContent || "").replace(/\s+/g, " ").trim();
    }

    function headingText(element: Element | null | undefined): string {
        if (!element) return "";
        const clone = element.cloneNode(true) as HTMLElement;
        clone.querySelectorAll(".setting-hint-toggle").forEach((node) => node.remove());
        return textOf(clone);
    }

    function isExcluded(element: Element | null): boolean {
        return Boolean(element?.closest(EXCLUDED));
    }

    function createHintToggle(label?: string): HTMLButtonElement {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "setting-hint-toggle";
        button.dataset.i18nDynamic = "true";
        if (label) {
            button.textContent = label;
            button.classList.add("setting-section-toggle");
        } else {
            button.innerHTML = '<i class="fa fa-info-circle" aria-hidden="true"></i>';
            button.title = t("settings.hint.show", "显示说明");
            button.setAttribute("aria-label", button.title);
        }
        button.setAttribute("aria-expanded", "false");
        return button;
    }

    function toggleHintHost(host: HTMLElement, toggle: HTMLButtonElement): void {
        const open = !host.classList.contains("setting-hint-open");
        host.classList.toggle("setting-hint-open", open);
        toggle.setAttribute("aria-expanded", String(open));
        if (!labelToggle(toggle) && toggle.title) {
            toggle.title = open ? t("settings.hint.hide", "收起说明") : t("settings.hint.show", "显示说明");
            toggle.setAttribute("aria-label", toggle.title);
        }
    }

    function labelToggle(toggle: HTMLButtonElement): boolean {
        return toggle.classList.contains("setting-section-toggle");
    }

    /** 设置项说明：默认隐藏，点击 ⓘ / 聚焦控件后展开。 */
    function decorateGroups(content: HTMLElement): void {
        const groups = Array.from(content.querySelectorAll<HTMLElement>(".form-group, .form-check"));
        groups.forEach((group) => {
            if (isExcluded(group) || group.dataset.hintDecorated === "1") return;
            // 嵌套在 form-group 里的 form-check 交给外层统一处理，避免出现两个 ⓘ。
            if (group.classList.contains("form-check") && group.closest(".form-group")) return;

            const hints = Array.from(group.querySelectorAll<HTMLElement>(HINT_SELECTOR))
                .filter((hint) => !isExcluded(hint) && !hint.closest(".setting-section-hint"));
            const anchor = group.querySelector<HTMLElement>(":scope > label")
                || group.querySelector<HTMLElement>(".form-check-label")
                || group.querySelector<HTMLElement>(":scope > .form-check");
            // 没有标题的分组（按钮行、状态块）不进索引；有标题但无说明的分组仍要能被搜索命中。
            if (!anchor) return;

            group.dataset.hintDecorated = "1";
            group.classList.add("setting-hint-host");
            if (hints.length === 0) return;
            hints.forEach((hint) => hint.classList.add("setting-hint"));

            const toggle = createHintToggle();
            anchor.insertAdjacentElement("afterend", toggle);

            toggle.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleHintHost(group, toggle);
            });
            // 用户开始编辑该设置时再展示说明，这是最需要它的时机。
            group.addEventListener("focusin", (event) => {
                const targetEl = event.target as HTMLElement | null;
                if (!targetEl || targetEl === toggle) return;
                if (targetEl.matches("input, select, textarea")) {
                    group.classList.add("setting-hint-open");
                    toggle.setAttribute("aria-expanded", "true");
                }
            });
        });
    }

    /** 分区级说明（整段灰色文字）：收进标题旁的「说明」按钮。 */
    function decorateSections(content: HTMLElement): void {
        content.querySelectorAll<HTMLElement>(".mb-4").forEach((section) => {
            if (isExcluded(section)) return;
            const hints = Array.from(section.children).filter((child) => {
                if (!(child instanceof HTMLElement)) return false;
                if (child.id || child.dataset.noSettingsHint !== undefined) return false;
                return child.matches("small.form-text, small.text-muted, .form-text")
                    || (child.matches("p") && child.classList.contains("text-muted"));
            }) as HTMLElement[];
            if (hints.length === 0) return;

            const heading = section.querySelector<HTMLElement>(":scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > h6");
            if (!heading) return;

            hints.forEach((hint) => hint.classList.add("setting-section-hint"));
            const toggle = createHintToggle(t("settings.hint.section", "说明"));
            // 标题与「说明」按钮放在同一行，避免按钮独占一行挤到标题下方。
            const headingRow = document.createElement("div");
            headingRow.className = "setting-section-heading";
            heading.insertAdjacentElement("beforebegin", headingRow);
            headingRow.appendChild(heading);
            headingRow.appendChild(toggle);

            const setOpen = (open: boolean): void => {
                hints.forEach((hint) => hint.classList.toggle("setting-hint-open", open));
                toggle.setAttribute("aria-expanded", String(open));
                toggle.textContent = open ? t("settings.hint.hide_section", "收起") : t("settings.hint.section", "说明");
            };
            toggle.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                setOpen(toggle.getAttribute("aria-expanded") !== "true");
            });
        });
    }

    function sectionTitle(host: HTMLElement): string {
        const block = host.closest<HTMLElement>(".mb-4, .tool-panel, section");
        const heading = block?.querySelector<HTMLElement>("h1, h2, h3, h4, h5, h6");
        return headingText(heading);
    }

    function buildIndex(): SettingEntry[] {
        const entries: SettingEntry[] = [];
        document.querySelectorAll<HTMLElement>(".settings-content").forEach((content) => {
            const tab = content.id.replace(/-settings-content$/, "");
            if (!tab) return;

            content.querySelectorAll<HTMLElement>(".setting-hint-host").forEach((host) => {
                if (isExcluded(host)) return;
                const titleEl = host.querySelector<HTMLElement>(":scope > label, .form-check-label");
                const title = headingText(titleEl);
                if (!title) return;
                const hint = Array.from(host.querySelectorAll<HTMLElement>(".setting-hint"))
                    .map((element) => headingText(element))
                    .join(" ");
                entries.push({ tab, host, title, hint, section: sectionTitle(host), kind: "setting" });
            });

            content.querySelectorAll<HTMLElement>(".mb-4").forEach((section) => {
                if (isExcluded(section)) return;
                const heading = section.querySelector<HTMLElement>("h1, h2, h3, h4, h5, h6");
                const title = headingText(heading);
                if (!title) return;
                const hint = Array.from(section.children)
                    .filter((child) => child.classList.contains("setting-section-hint"))
                    .map((child) => headingText(child))
                    .join(" ");
                entries.push({ tab, host: section, title, hint, section: "", kind: "section" });
            });
        });
        return entries;
    }

    function normalize(value: string): string {
        return value.toLowerCase().replace(/\s+/g, "");
    }

    /** 连续包含优先，其次按子序列匹配计算跨度（跨度越小越接近）。 */
    function fuzzyScore(needle: string, haystack: string): number {
        const query = normalize(needle);
        if (!query) return 0;
        const text = normalize(haystack);
        if (!text) return 0;
        const direct = text.indexOf(query);
        if (direct >= 0) {
            const density = query.length / Math.max(text.length, 1);
            return 1000 - Math.min(direct, 40) * 4 + density * 80;
        }
        let cursor = 0;
        let gaps = 0;
        for (const char of query) {
            const found = text.indexOf(char, cursor);
            if (found < 0) return 0;
            gaps += found - cursor;
            cursor = found + 1;
        }
        return Math.max(30, 260 - Math.min(gaps, 220));
    }

    function scoreEntry(entry: SettingEntry, tokens: string[]): number {
        let total = 0;
        for (const token of tokens) {
            const titleScore = fuzzyScore(token, entry.title) * 1.6;
            const hintScore = fuzzyScore(token, entry.hint);
            const sectionScore = fuzzyScore(token, entry.section) * 0.7;
            const best = Math.max(titleScore, hintScore, sectionScore);
            if (best <= 0) return 0;
            total += best;
        }
        // 标题命中的条目排在同分说明命中的前面。
        return total + (fuzzyScore(tokens[0] || "", entry.title) > 0 ? 40 : 0);
    }

    function escapeHtml(value: string): string {
        return value
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function highlight(value: string, tokens: string[]): string {
        let output = escapeHtml(value);
        tokens.forEach((token) => {
            if (!token) return;
            const index = output.toLowerCase().indexOf(escapeHtml(token).toLowerCase());
            if (index < 0) return;
            const length = escapeHtml(token).length;
            output = `${output.slice(0, index)}<mark>${output.slice(index, index + length)}</mark>${output.slice(index + length)}`;
        });
        return output;
    }

    function activate(entry: SettingEntry): void {
        closeResults();
        const input = document.getElementById("settingsSearchInput") as HTMLInputElement | null;
        input?.blur();
        switchSettingsTab(entry.tab);
        entry.host.classList.remove(HIGHLIGHT_CLASS);
        // 强制重排后再加类，重复搜索同一条也能看到闪烁提示。
        void entry.host.offsetWidth;
        entry.host.classList.add(HIGHLIGHT_CLASS);
        window.setTimeout(() => entry.host.classList.remove(HIGHLIGHT_CLASS), 2600);

        if (entry.kind === "section") {
            entry.host.querySelectorAll<HTMLElement>(".setting-section-hint").forEach((hint) => hint.classList.add("setting-hint-open"));
        } else {
            entry.host.classList.add("setting-hint-open");
            entry.host.querySelectorAll<HTMLElement>(".setting-hint-toggle").forEach((toggle) => toggle.setAttribute("aria-expanded", "true"));
        }
        window.requestAnimationFrame(() => {
            entry.host.scrollIntoView({ behavior: "smooth", block: "center" });
        });
    }

    function resultsPanel(): HTMLElement | null {
        return document.getElementById("settingsSearchResults");
    }

    function closeResults(): void {
        const panel = resultsPanel();
        if (!panel) return;
        panel.hidden = true;
        panel.innerHTML = "";
    }

    function renderResults(matches: Array<{ entry: SettingEntry; score: number }>, tokens: string[]): void {
        const panel = resultsPanel();
        if (!panel) return;
        panel.hidden = false;
        panel.dataset.i18nDynamic = "true";
        if (matches.length === 0) {
            panel.innerHTML = `<div class="settings-search-empty">${escapeHtml(t("settings.search.empty", "没有匹配的设置，换个关键词试试"))}</div>`;
            return;
        }
        panel.innerHTML = "";
        matches.forEach(({ entry }) => {
            const tabButton = document.querySelector<HTMLElement>(`.settings-tab[data-settings="${entry.tab}"]`);
            const tabLabel = headingText(tabButton);
            const meta = [tabLabel, entry.section].filter(Boolean).join(" · ");
            const item = document.createElement("button");
            item.type = "button";
            item.className = "settings-search-result";
            item.dataset.i18nDynamic = "true";
            item.innerHTML = `
                <span class="settings-search-result-title">${highlight(entry.title, tokens)}</span>
                ${meta ? `<span class="settings-search-result-meta">${escapeHtml(meta)}</span>` : ""}
                ${entry.hint ? `<span class="settings-search-result-hint">${highlight(entry.hint, tokens)}</span>` : ""}
            `;
            item.addEventListener("click", () => activate(entry));
            panel.appendChild(item);
        });
    }

    function runSearch(): void {
        const input = document.getElementById("settingsSearchInput") as HTMLInputElement | null;
        if (!input) return;
        const query = input.value.trim();
        const clearButton = document.getElementById("settingsSearchClear") as HTMLButtonElement | null;
        if (clearButton) clearButton.hidden = query.length === 0;
        const tokens = query.toLowerCase().split(/\s+/).filter(Boolean);
        if (tokens.length === 0) {
            closeResults();
            return;
        }
        const matches = buildIndex()
            .map((entry) => ({ entry, score: scoreEntry(entry, tokens) }))
            .filter((item) => item.score > 0)
            .sort((left, right) => right.score - left.score)
            .slice(0, 12);
        renderResults(matches, tokens);
    }

    function bindSearch(): void {
        const search = document.getElementById("settingsSearchInput") as HTMLInputElement | null;
        if (!search) return;
        const clearButton = document.getElementById("settingsSearchClear") as HTMLButtonElement | null;

        search.addEventListener("input", runSearch);
        search.addEventListener("focus", () => {
            if (search.value.trim()) runSearch();
        });
        search.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                search.value = "";
                if (clearButton) clearButton.hidden = true;
                closeResults();
                search.blur();
                return;
            }
            if (event.key === "Enter") {
                const first = resultsPanel()?.querySelector<HTMLButtonElement>(".settings-search-result");
                if (first) {
                    event.preventDefault();
                    first.click();
                }
            }
        });
        clearButton?.addEventListener("click", () => {
            search.value = "";
            clearButton.hidden = true;
            closeResults();
            search.focus();
        });
        document.addEventListener("click", (event) => {
            const targetNode = event.target as Node | null;
            if (!targetNode) return;
            if (targetNode instanceof Element && targetNode.closest(".settings-search")) return;
            closeResults();
        });
    }

    function decorateAll(): void {
        document.querySelectorAll<HTMLElement>(".settings-content").forEach((content) => {
            decorateGroups(content);
            decorateSections(content);
        });
    }

    function init(): void {
        if (initialized) return;
        const search = document.getElementById("settingsSearchInput");
        if (!search) return;
        initialized = true;
        decorateAll();
        bindSearch();
    }

    return { init, runSearch };
})();

window.TrpgSettingsAssistant = SettingsAssistant;
