/**
 * 音频块选择器：Discourse 设置页那种「已选项 + 下拉添加」的组合控件。
 *
 * - 管理员模式（allow）：把允许的块加入清单，允许同时保留多个配乐块；
 * - 房间模式（select）：候选只来自管理员清单，配乐块四选一、环境音可叠加；
 * - 未接入的「音乐生成模型」始终展示在候选里，但禁用并写明原因，不做「点了没反应」的死块。
 *
 * 选择结果写入容器的 ``data-blocks``（逗号分隔）。房间/管理员页面各自读取这个值提交。
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
        onChange?: (blocks: string[]) => void;
    }

    interface PickerState {
        options: AudioBlockOption[];
        mode: PickerMode;
        onChange?: (blocks: string[]) => void;
    }

    const states: Record<string, PickerState> = {};
    const MUSIC_BLOCKS = ["procedural", "music_model", "external", "off"];
    const MUSIC_PRIORITY = ["external", "music_model", "procedural", "off"];

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

    function pickDefaultMusic(state: PickerState): string {
        const allowedMusic = state.options.filter((option) => isMusic(option.value));
        const usable = allowedMusic.filter((option) => option.available);
        const pool = usable.length > 0 ? usable : allowedMusic;
        for (const block of MUSIC_PRIORITY) {
            if (pool.some((option) => option.value === block)) return block;
        }
        return "off";
    }

    function normalizeSelection(state: PickerState, blocks: string[]): string[] {
        const result: string[] = [];
        blocks.forEach((block) => {
            const option = findOption(state, block);
            if (option && result.indexOf(block) < 0) result.push(block);
        });
        if (state.mode === "select" && state.options.length > 0) {
            const music = result.filter(isMusic);
            if (music.length > 1) {
                const keep = MUSIC_PRIORITY.find((block) => music.indexOf(block) >= 0) || music[0];
                for (let index = result.length - 1; index >= 0; index -= 1) {
                    const item = result[index];
                    if (item && isMusic(item) && item !== keep) result.splice(index, 1);
                }
            }
            if (!result.some(isMusic)) {
                const fallback = pickDefaultMusic(state);
                if (fallback) result.push(fallback);
            }
        }
        return result;
    }

    function renderPicker(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        const selected = normalizeSelection(state, readBlocks(element));
        writeBlocks(element, selected);

        const rows = selected.map((block) => {
            const option = findOption(state, block);
            if (!option) return "";
            const badge = option.stackable
                ? '<span class="badge bg-info text-dark ms-2">可叠加</span>'
                : '<span class="badge bg-secondary ms-2">配乐四选一</span>';
            const warning = option.available
                ? ""
                : `<div class="small text-warning mt-1">${escapeHtml(option.unavailable_reason || "当前不可用")}</div>`;
            return `
                <div class="audio-block-row border rounded p-2 mb-2" data-block="${escapeHtml(block)}">
                    <div class="d-flex justify-content-between align-items-start gap-2">
                        <div class="flex-grow-1">
                            <div><strong>${escapeHtml(option.label)}</strong>${badge}</div>
                            <div class="small text-muted">${escapeHtml(option.description)}</div>
                            ${warning}
                        </div>
                        <button type="button" class="btn btn-sm btn-outline-danger" data-audio-block-remove="${escapeHtml(block)}">移除</button>
                    </div>
                </div>`;
        }).join("");

        const candidates = state.options.filter((option) => selected.indexOf(option.value) < 0);
        const optionsHtml = candidates.length === 0
            ? '<option value="">已添加全部可选项</option>'
            : candidates.map((option) => {
                const suffix = option.available ? "" : `（不可用：${option.unavailable_reason || "未接入"}）`;
                return `<option value="${escapeHtml(option.value)}"${option.available ? "" : " disabled"}>${escapeHtml(option.label + suffix)}</option>`;
            }).join("");

        element.innerHTML = `
            <div class="audio-block-list" data-audio-block-list>${rows || '<div class="text-muted small mb-2">尚未添加任何音频块。</div>'}</div>
            <div class="input-group">
                <select class="form-select" data-audio-block-add-select>${optionsHtml}</select>
                <button type="button" class="btn btn-outline-secondary" data-audio-block-add>添加</button>
            </div>
            <small class="form-text text-muted d-block mt-1">${state.mode === "allow"
                ? "管理员只划定允许范围；房间只能在这里面选择。"
                : "房间只能选择管理员允许的块；配乐块四选一，环境音可与任意配乐叠加。"}</small>
        `;
    }

    function emit(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        state.onChange?.(readBlocks(element));
    }

    function bindEvents(containerId: string): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state || element.dataset.bound === "true") return;
        element.dataset.bound = "true";
        element.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const remove = target?.closest<HTMLElement>("[data-audio-block-remove]");
            if (remove) {
                const block = remove.dataset.audioBlockRemove || "";
                const next = readBlocks(element).filter((item) => item !== block);
                writeBlocks(element, normalizeSelection(state, next));
                renderPicker(containerId);
                emit(containerId);
                return;
            }
            const addButton = target?.closest<HTMLElement>("[data-audio-block-add]");
            if (addButton) {
                const select = element.querySelector<HTMLSelectElement>("[data-audio-block-add-select]");
                const block = select?.value || "";
                if (!block) return;
                const option = findOption(state, block);
                if (!option || !option.available) return;
                let next = readBlocks(element);
                if (state.mode === "select" && isMusic(block)) {
                    next = next.filter((item) => !isMusic(item));
                }
                next.push(block);
                writeBlocks(element, normalizeSelection(state, next));
                renderPicker(containerId);
                emit(containerId);
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
        const state: PickerState = {
            options: Array.isArray(options) ? options : [],
            mode: renderOptions.mode === "select" ? "select" : "allow",
        };
        if (renderOptions.onChange) state.onChange = renderOptions.onChange;
        states[containerId] = state;
        writeBlocks(element, normalizeSelection(state, Array.isArray(selected) ? selected : []));
        bindEvents(containerId);
        renderPicker(containerId);
    }

    export function getSelection(containerId: string): string[] {
        const element = container(containerId);
        return element ? readBlocks(element) : [];
    }

    export function setSelection(containerId: string, blocks: string[]): void {
        const element = container(containerId);
        const state = states[containerId];
        if (!element || !state) return;
        writeBlocks(element, normalizeSelection(state, Array.isArray(blocks) ? blocks : []));
        renderPicker(containerId);
    }
}
