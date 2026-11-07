/**
 * 音频块标签输入控件（管理员 / 房间共用）。
 *
 * 形态：单行输入框内嵌已选 chip，输入即时筛选，下拉列出匹配块的名称与说明；
 * 点选或回车添加，chip 上可单独移除，空输入时退格删除最后一个。
 *
 * - allow（管理员）：把允许的块加入清单，可同时保留多个配乐块；
 * - select（房间）：候选只来自管理员允许清单，配乐块四选一、环境音可叠加；
 * - 只能选「已知块」：自由文本永远不会入库，输入不匹配时只显示空态；
 * - 不可用（如未接入的 music_model）或管理员未允许的块在下拉里禁用并写明原因；
 * - 配乐块互斥：已有配乐块时再添加另一个，会替换并给出可见提示，不静默丢弃；
 * - 兼容英文键、中文名、说明关键词与常见别名（如 environment → ambient）。
 *
 * 选择结果写入容器的 ``data-blocks``（逗号分隔），页面据此提交。
 */
namespace TrpgAudioBlocks {
    export interface AudioBlockOption {
        value: string;
        label: string;
        description: string;
        stackable: boolean;
        available: boolean;
        unavailable_reason?: string;
    }

    export type PickerMode = "allow" | "select";

    export interface RenderOptions {
        mode?: PickerMode;
        allowed?: string[];
        onChange?: (blocks: string[]) => void;
        /** 覆盖默认提示语：成员白名单等非音频场景复用本控件时使用。 */
        placeholder?: string;
        /** 传空串可隐藏控件自带的说明文字。 */
        hint?: string;
        /** 提示语里对候选项的称呼，默认「块」；成员白名单传「成员」。 */
        noun?: string;
        /** allow 模式清空时的提示；传空串可关闭该提示。 */
        allowEmptyNotice?: string;
    }

    interface PickerState {
        options: AudioBlockOption[];
        allowed: string[] | null;
        mode: PickerMode;
        onChange: ((blocks: string[]) => void) | null;
        placeholder: string;
        hint: string;
        noun: string;
        allowEmptyNotice: string;
        query: string;
        activeIndex: number;
        open: boolean;
    }

    interface MenuItem {
        option: AudioBlockOption;
        selectable: boolean;
        reason: string;
        score: number;
    }

    const states: Record<string, PickerState> = {};
    const noticeTimers: Record<string, number> = {};
    const MUSIC_BLOCKS = ["procedural", "music_model", "external", "off"];
    const MUSIC_PRIORITY = ["external", "music_model", "procedural", "off"];

    /**
     * 宽松匹配用的别名：英文键之外，补上常见说法、中文名与说明关键词。
     * 值会被 normalizeText 归一化后比较，因此可以写 ``music-model`` 这类带分隔符的写法。
     */
    const BLOCK_ALIASES: Record<string, string[]> = {
        ambient: [
            "ambient", "environment", "environmental", "env", "ambience", "atmosphere",
            "环境", "环境音", "氛围", "背景音", "背景环境", "雨", "雨声", "风", "风声",
            "火焰", "篝火", "噪声", "白噪音", "自然",
        ],
        procedural: [
            "procedural", "procedural-music", "program", "generated", "generative", "synth",
            "synthesized", "realtime", "程序化", "程序化生成", "生成", "自动生成", "合成",
            "实时配乐", "配乐", "现场生成",
        ],
        music_model: [
            "music_model", "music-model", "model", "musicgen", "music-gen", "generation-model",
            "ai", "ai-music", "模型", "音乐模型", "音乐生成", "生成模型", "音乐生成模型",
        ],
        external: [
            "external", "external-bgm", "bgm", "link", "url", "stream", "online",
            "外链", "外链bgm", "外部", "外部音乐", "曲库", "音乐库", "在线",
        ],
        off: [
            "off", "none", "mute", "muted", "silence", "silent", "disable", "disabled",
            "静音", "不使用", "关闭", "无", "不要",
        ],
    };

    function escapeHtml(text: unknown): string {
        return String(text ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function container(containerId: string): HTMLElement | null {
        return document.getElementById(containerId);
    }

    function readBlocks(element: HTMLElement): string[] {
        return (element.dataset.blocks || "")
            .split(",")
            .map((item) => item.trim())
            .filter((item) => item);
    }

    function writeBlocks(element: HTMLElement, blocks: string[]): void {
        element.dataset.blocks = blocks.join(",");
    }

    function isMusic(block: string): boolean {
        return MUSIC_BLOCKS.indexOf(block) >= 0;
    }

    function findOption(state: PickerState, value: string): AudioBlockOption | undefined {
        return state.options.find((option) => option.value === value);
    }

    function normalizeText(text: unknown): string {
        return String(text ?? "")
            .toLowerCase()
            .replace(/[_\-\/]+/g, "")
            .replace(/\s+/g, " ")
            .trim();
    }

    /** 命中返回分数（越高越靠前），未命中返回 -1。多个关键词按「全部命中」处理。 */
    function matchScore(option: AudioBlockOption, query: string): number {
        const tokens = query.trim().toLowerCase().split(/\s+/).filter((token) => token.length > 0);
        if (tokens.length === 0) return 0;
        const value = normalizeText(option.value);
        const label = normalizeText(option.label);
        const description = normalizeText(option.description);
        const aliases = (BLOCK_ALIASES[option.value] || []).map(normalizeText);
        let score = 0;
        for (const token of tokens) {
            const key = normalizeText(token);
            if (!key) continue;
            let best = -1;
            if (value === key || aliases.indexOf(key) >= 0) best = 100;
            else if (label.indexOf(key) === 0) best = 80;
            else if (value.indexOf(key) >= 0) best = 70;
            else if (aliases.some((alias) => alias.indexOf(key) >= 0)) best = 60;
            else if (label.indexOf(key) >= 0) best = 50;
            else if (description.indexOf(key) >= 0) best = 30;
            if (best < 0) return -1;
            score += best;
        }
        return score;
    }

    function optionAvailability(
        state: PickerState,
        option: AudioBlockOption,
    ): { selectable: boolean; reason: string } {
        if (!option.available) {
            return { selectable: false, reason: option.unavailable_reason || "当前不可用" };
        }
        if (state.mode === "select" && state.allowed && state.allowed.indexOf(option.value) < 0) {
            return { selectable: false, reason: "管理员未允许该块" };
        }
        return { selectable: true, reason: "" };
    }

    function buildItems(state: PickerState, selected: string[]): MenuItem[] {
        const query = state.query.trim();
        const items: MenuItem[] = [];
        state.options.forEach((option) => {
            if (selected.indexOf(option.value) >= 0) return;
            const score = query ? matchScore(option, query) : 0;
            if (query && score < 0) return;
            const availability = optionAvailability(state, option);
            items.push({
                option,
                selectable: availability.selectable,
                reason: availability.reason,
                score,
            });
        });
        if (query) {
            // 稳定排序：同分时保持后端给定的块顺序。
            items.sort((left, right) => right.score - left.score);
        }
        return items;
    }

    function normalizeSelection(
        state: PickerState,
        blocks: string[],
    ): { blocks: string[]; dropped: string[] } {
        const result: string[] = [];
        const dropped: string[] = [];
        blocks.forEach((block) => {
            if (result.indexOf(block) >= 0) return;
            if (findOption(state, block)) result.push(block);
        });
        if (state.mode === "select") {
            const music = result.filter(isMusic);
            if (music.length > 1) {
                const keep = MUSIC_PRIORITY.find((block) => music.indexOf(block) >= 0) || music[0] || "off";
                for (let index = result.length - 1; index >= 0; index -= 1) {
                    const item = result[index];
                    if (item && isMusic(item) && item !== keep) {
                        result.splice(index, 1);
                        dropped.push(item);
                    }
                }
            }
        }
        return { blocks: result, dropped };
    }

    function renderSkeleton(element: HTMLElement, containerId: string, state: PickerState): void {
        const hint = state.hint
            ? `<small class="form-text text-muted d-block mt-1">${escapeHtml(state.hint)}</small>`
            : "";
        element.innerHTML = `
            <div class="audio-tags-control" data-audio-tags-control>
                <span class="audio-tags-chips" data-audio-tags-chips></span>
                <input type="text" class="audio-tags-input" data-audio-tags-input
                    role="combobox" aria-autocomplete="list" aria-expanded="false"
                    aria-controls="${escapeHtml(containerId)}-menu" autocomplete="off"
                    placeholder="${escapeHtml(state.placeholder)}">
            </div>
            <div class="audio-tags-menu" id="${escapeHtml(containerId)}-menu" data-audio-tags-menu role="listbox" hidden></div>
            <div class="audio-tags-notice" data-audio-tags-notice role="status" aria-live="polite" hidden></div>
            ${hint}
        `;
    }

    function renderChips(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const host = element.querySelector<HTMLElement>("[data-audio-tags-chips]");
        if (!host) return;
        const selected = readBlocks(element);
        host.innerHTML = selected.map((block) => {
            const option = findOption(state, block);
            const label = option ? option.label : block;
            const availability = option
                ? optionAvailability(state, option)
                : { selectable: false, reason: "未知的音频块" };
            const className = availability.selectable
                ? "audio-tags-chip"
                : "audio-tags-chip is-unavailable";
            const title = availability.selectable
                ? ""
                : ` title="${escapeHtml(availability.reason)}"`;
            return `<span class="${className}" data-audio-block-chip="${escapeHtml(block)}"${title}>
                <span class="audio-tags-chip-label">${escapeHtml(label)}</span>
                <button type="button" class="audio-tags-chip-remove" data-audio-block-remove="${escapeHtml(block)}" aria-label="移除 ${escapeHtml(label)}">&times;</button>
            </span>`;
        }).join("");
    }

    function renderMenu(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const menu = element.querySelector<HTMLElement>("[data-audio-tags-menu]");
        const input = element.querySelector<HTMLInputElement>("[data-audio-tags-input]");
        if (!menu || !input) return;
        const items = buildItems(state, readBlocks(element));
        if (
            state.activeIndex < 0
            || state.activeIndex >= items.length
            || !items[state.activeIndex]?.selectable
        ) {
            state.activeIndex = items.findIndex((item) => item.selectable);
        }
        input.setAttribute("aria-expanded", state.open ? "true" : "false");
        if (!state.open) {
            menu.hidden = true;
            return;
        }
        menu.hidden = false;
        if (items.length === 0) {
            menu.innerHTML = `<div class="audio-tags-empty">${state.query.trim() ? `没有匹配的${escapeHtml(state.noun)}` : "已添加全部可选项"}</div>`;
            return;
        }
        menu.innerHTML = items.map((item, index) => {
            const active = index === state.activeIndex;
            const classes = ["audio-tags-option"];
            if (active) classes.push("is-active");
            if (!item.selectable) classes.push("is-disabled");
            const reason = item.reason
                ? `<span class="audio-tags-option-reason">${escapeHtml(item.reason)}</span>`
                : "";
            const aria = item.selectable
                ? (active ? ' aria-selected="true"' : ' aria-selected="false"')
                : ' aria-disabled="true"';
            return `<button type="button" role="option" class="${classes.join(" ")}" data-audio-block-option="${escapeHtml(item.option.value)}" data-index="${index}"${aria}>
                <span class="audio-tags-option-head"><strong>${escapeHtml(item.option.label)}</strong></span>
                ${reason}
            </button>`;
        }).join("");
    }

    function scrollActiveIntoView(containerId: string, index: number): void {
        const element = container(containerId);
        const option = element?.querySelector<HTMLElement>(
            `[data-audio-block-option][data-index="${index}"]`,
        );
        option?.scrollIntoView({ block: "nearest" });
    }

    function showNotice(
        containerId: string,
        message: string,
        tone: "info" | "warning" = "info",
    ): void {
        const element = container(containerId);
        if (!element) return;
        const notice = element.querySelector<HTMLElement>("[data-audio-tags-notice]");
        if (!notice) return;
        notice.textContent = message;
        notice.className = tone === "warning" ? "audio-tags-notice is-warning" : "audio-tags-notice";
        notice.hidden = !message;
        const previous = noticeTimers[containerId];
        if (previous) window.clearTimeout(previous);
        if (message) {
            noticeTimers[containerId] = window.setTimeout(() => {
                notice.hidden = true;
                notice.textContent = "";
            }, 8000);
        }
    }

    function emit(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        state.onChange?.(readBlocks(element));
    }

    function syncInput(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const input = element.querySelector<HTMLInputElement>("[data-audio-tags-input]");
        if (input) input.value = state.query;
    }

    function focusInput(containerId: string): void {
        const input = container(containerId)?.querySelector<HTMLInputElement>("[data-audio-tags-input]");
        input?.focus();
    }

    function refreshAll(containerId: string): void {
        renderChips(containerId);
        renderMenu(containerId);
    }

    function addBlock(containerId: string, value: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const option = findOption(state, value);
        if (!option) {
            showNotice(containerId, `没有匹配的${state.noun}，未添加任何内容。`, "warning");
            return;
        }
        const availability = optionAvailability(state, option);
        if (!availability.selectable) {
            showNotice(containerId, `「${option.label}」不可选：${availability.reason}`, "warning");
            return;
        }
        const selected = readBlocks(element);
        if (selected.indexOf(value) >= 0) {
            state.query = "";
            state.activeIndex = -1;
            syncInput(containerId);
            refreshAll(containerId);
            showNotice(containerId, `「${option.label}」已在清单中。`);
            return;
        }
        let notice = "";
        let tone: "info" | "warning" = "info";
        if (state.mode === "select" && isMusic(value)) {
            const replaced = selected.filter((block) => isMusic(block) && block !== value);
            if (replaced.length > 0) {
                for (let index = selected.length - 1; index >= 0; index -= 1) {
                    const block = selected[index];
                    if (block && replaced.indexOf(block) >= 0) selected.splice(index, 1);
                }
                const names = replaced
                    .map((block) => findOption(state, block)?.label || block)
                    .join("、");
                notice = `配乐块四选一：已用「${option.label}」替换「${names}」。`;
                tone = "warning";
            }
        }
        selected.push(value);
        writeBlocks(element, selected);
        state.query = "";
        state.activeIndex = -1;
        state.open = true;
        syncInput(containerId);
        refreshAll(containerId);
        emit(containerId);
        if (notice) showNotice(containerId, notice, tone);
    }

    function removeBlock(containerId: string, value: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const selected = readBlocks(element);
        const next = selected.filter((block) => block !== value);
        if (next.length === selected.length) return;
        const option = findOption(state, value);
        writeBlocks(element, next);
        refreshAll(containerId);
        emit(containerId);
        const label = option ? option.label : value;
        if (state.mode === "select" && isMusic(value) && !next.some(isMusic)) {
            showNotice(
                containerId,
                `已移除「${label}」，当前没有配乐块：按静音处理，环境音仍会播放（若已选择）。`,
                "warning",
            );
        } else if (state.mode === "allow" && next.length === 0) {
            if (state.allowEmptyNotice) showNotice(containerId, state.allowEmptyNotice, "warning");
        } else {
            showNotice(containerId, `已移除「${label}」。`);
        }
        focusInput(containerId);
    }

    function menuItems(containerId: string): MenuItem[] {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return [];
        return buildItems(state, readBlocks(element));
    }

    function moveActive(containerId: string, delta: number): void {
        const state = states[containerId];
        if (!state) return;
        const items = menuItems(containerId);
        if (items.length === 0) {
            state.activeIndex = -1;
            renderMenu(containerId);
            return;
        }
        let index = state.activeIndex >= 0 ? state.activeIndex : (delta > 0 ? -1 : 0);
        for (let step = 0; step < items.length; step += 1) {
            index = (index + delta + items.length) % items.length;
            const item = items[index];
            if (item && item.selectable) {
                state.activeIndex = index;
                renderMenu(containerId);
                scrollActiveIntoView(containerId, index);
                return;
            }
        }
    }

    function bindContainerEvents(containerId: string): void {
        const element = container(containerId);
        if (!element || element.dataset.bound === "true") return;
        element.dataset.bound = "true";
        element.addEventListener("click", (event) => {
            const state = states[containerId];
            if (!state) return;
            const target = event.target as HTMLElement | null;
            if (!target) return;
            const remove = target.closest<HTMLElement>("[data-audio-block-remove]");
            if (remove) {
                event.preventDefault();
                removeBlock(containerId, remove.dataset.audioBlockRemove || "");
                return;
            }
            const option = target.closest<HTMLElement>("[data-audio-block-option]");
            if (option) {
                event.preventDefault();
                addBlock(containerId, option.dataset.audioBlockOption || "");
                return;
            }
            if (target.closest<HTMLElement>("[data-audio-tags-control]")) {
                state.open = true;
                renderMenu(containerId);
                focusInput(containerId);
            }
        });
    }

    function bindOutsideClick(containerId: string): void {
        const element = container(containerId);
        if (!element || element.dataset.outsideBound === "true") return;
        element.dataset.outsideBound = "true";
        document.addEventListener("click", (event) => {
            const state = states[containerId];
            if (!state || !state.open) return;
            const target = event.target as Node | null;
            if (target && element.contains(target)) return;
            state.open = false;
            state.query = "";
            syncInput(containerId);
            renderMenu(containerId);
        });
    }

    function bindInputEvents(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const input = element.querySelector<HTMLInputElement>("[data-audio-tags-input]");
        if (!input) return;
        input.addEventListener("input", () => {
            state.query = input.value;
            state.activeIndex = -1;
            state.open = true;
            renderMenu(containerId);
        });
        input.addEventListener("focus", () => {
            state.open = true;
            renderMenu(containerId);
        });
        input.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                if (!state.open) {
                    state.open = true;
                    renderMenu(containerId);
                }
                moveActive(containerId, 1);
                return;
            }
            if (event.key === "ArrowUp") {
                event.preventDefault();
                if (!state.open) {
                    state.open = true;
                    renderMenu(containerId);
                }
                moveActive(containerId, -1);
                return;
            }
            if (event.key === "Enter") {
                if (!state.open) return;
                event.preventDefault();
                const items = menuItems(containerId);
                const active = items[state.activeIndex];
                if (active && active.selectable) {
                    addBlock(containerId, active.option.value);
                    return;
                }
                const fallback = items.find((candidate) => candidate.selectable);
                if (fallback) {
                    addBlock(containerId, fallback.option.value);
                } else {
                    showNotice(
                        containerId,
                        state.query.trim() ? `没有匹配的${state.noun}，未添加任何内容。` : "没有可添加的块。",
                        "warning",
                    );
                }
                return;
            }
            if (event.key === "Escape") {
                event.preventDefault();
                state.open = false;
                state.query = "";
                syncInput(containerId);
                renderMenu(containerId);
                return;
            }
            if (event.key === "Backspace" && input.value === "") {
                const selected = readBlocks(element);
                const last = selected[selected.length - 1];
                if (!last) return;
                event.preventDefault();
                removeBlock(containerId, last);
            }
        });
    }

    export function render(
        containerId: string,
        options: AudioBlockOption[],
        selected: string[],
        renderOptions: RenderOptions = {},
    ): void {
        const element = container(containerId);
        if (!element) return;
        const mode: PickerMode = renderOptions.mode === "select" ? "select" : "allow";
        const state: PickerState = {
            options: Array.isArray(options) ? options : [],
            allowed: Array.isArray(renderOptions.allowed) ? renderOptions.allowed : null,
            mode,
            onChange: renderOptions.onChange || null,
            placeholder: renderOptions.placeholder !== undefined
                ? renderOptions.placeholder
                : (mode === "select"
                    ? "输入关键词筛选音频块（如 environment、静音）…"
                    : "输入关键词添加音频块（如 environment、外链）…"),
            hint: renderOptions.hint !== undefined
                ? renderOptions.hint
                : (mode === "select"
                    ? "候选项来自管理员允许清单；配乐块四选一，环境音可与任意配乐叠加。"
                    : "管理员只划定允许范围，房间只能在这里面选择；不可用的块会标注原因。"),
            noun: renderOptions.noun || "块",
            allowEmptyNotice: renderOptions.allowEmptyNotice !== undefined
                ? renderOptions.allowEmptyNotice
                : "允许清单已清空：保存后会回退为旧版配乐引擎推导出的默认范围。",
            query: "",
            activeIndex: -1,
            open: false,
        };
        states[containerId] = state;
        element.classList.add("audio-tags");
        renderSkeleton(element, containerId, state);
        bindContainerEvents(containerId);
        bindOutsideClick(containerId);
        bindInputEvents(containerId);
        const normalized = normalizeSelection(state, Array.isArray(selected) ? selected : []);
        writeBlocks(element, normalized.blocks);
        refreshAll(containerId);
        if (normalized.dropped.length > 0) {
            const names = normalized.dropped
                .map((block) => findOption(state, block)?.label || block)
                .join("、");
            showNotice(
                containerId,
                `配乐块四选一：已保留一个，忽略「${names}」。`,
                "warning",
            );
        }
    }

    export function getSelection(containerId: string): string[] {
        const element = container(containerId);
        return element ? readBlocks(element) : [];
    }

    export function setSelection(containerId: string, blocks: string[]): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        writeBlocks(element, normalizeSelection(state, Array.isArray(blocks) ? blocks : []).blocks);
        refreshAll(containerId);
    }
}
