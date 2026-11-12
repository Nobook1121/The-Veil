/**
 * 道具编辑器（设计稿 §8.3）。
 *
 * 左侧表单按模板 manifest 的字段动态渲染，右侧 iframe 指向
 * `/render?format=png&v=<非ce>`；保存后刷新预览并广播 `trpg:props-changed`。
 */
interface PropEditorOpenOptions {
    roomId: string;
    propId?: string;
    templateId?: string;
    returnToLibrary?: boolean;
}

interface Window {
    TrpgPropEditor?: { open: (options: PropEditorOpenOptions) => void };
    TrpgPropLibrary?: { reopen?: () => void };
}

(function initializePropEditor(global: Window): void {
    "use strict";

    type OpenOptions = PropEditorOpenOptions;

    const WEAR_KEYS = ["stain", "fold", "burn", "tear"] as const;
    let initialized = false;
    let modal: BootstrapModalInstance | null = null;
    let roomId = "";
    let propId = "";
    let templates: PropTemplate[] = [];
    let currentSpec: PropSpec | null = null;
    let saving = false;
    let drafting = false;
    let returnToLibrary = false;

    function api(): TrpgPropApiRuntime {
        const runtime = global.TrpgPropApi;
        if (!runtime) throw new Error("TrpgPropApi is not ready");
        return runtime;
    }

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string, values?: Record<string, string | number>): string {
        return global.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function setStatus(text: string, tone: "idle" | "ok" | "error" = "idle"): void {
        const status = byId<HTMLElement>("propEditorStatus");
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("is-error", tone === "error");
        status.classList.toggle("is-ok", tone === "ok");
    }

    function notifyError(error: unknown): void {
        const runtime = api();
        const fallback = t("prop.error.generic", "操作失败，请稍后重试。");
        const message = t(`prop.error.${runtime.errorCode(error)}`, runtime.errorText(error, fallback));
        global.showNotification?.(message, "error");
        setStatus(message, "error");
    }

    function currentTemplate(): PropTemplate | null {
        const select = byId<HTMLSelectElement>("propEditorTemplate");
        const id = select?.value || currentSpec?.template_id || "";
        return templates.find((item) => item.id === id) || null;
    }

    function fieldValues(): Record<string, string> {
        const container = byId<HTMLElement>("propEditorFields");
        const values: Record<string, string> = {};
        if (!container) return values;
        container.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-prop-field]").forEach((input) => {
            const key = input.dataset.propField || "";
            if (key) values[key] = input.value;
        });
        return values;
    }

    function renderTemplateSelect(): void {
        const select = byId<HTMLSelectElement>("propEditorTemplate");
        if (!select) return;
        const groups: Array<{ name: string; items: PropTemplate[] }> = [];
        templates.forEach((template) => {
            const name = template.group_name || t("prop.group.other", "其他");
            let group = groups.find((item) => item.name === name);
            if (!group) {
                group = { name, items: [] };
                groups.push(group);
            }
            group.items.push(template);
        });
        select.innerHTML = groups
            .map((group) => {
                const options = group.items
                    .map((template) => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.name || template.id)}</option>`)
                    .join("");
                return `<optgroup label="${escapeHtml(group.name)}">${options}</optgroup>`;
            })
            .join("");
        select.disabled = Boolean(propId);
        const selected = currentSpec?.template_id || templates[0]?.id || "";
        if (selected) select.value = selected;
    }

    function renderPaperSelect(manifest: PropTemplate | null): void {
        const select = byId<HTMLSelectElement>("propEditorPaper");
        if (!select) return;
        const papers = manifest?.styles?.paper?.length ? manifest.styles.paper : ["ivory", "aged", "white"];
        select.innerHTML = papers
            .map((paper) => `<option value="${escapeHtml(paper)}">${escapeHtml(t(`prop.paper.${paper}`, paper))}</option>`)
            .join("");
        const selected = currentSpec?.style?.paper || papers[0];
        if (selected && papers.includes(selected)) select.value = selected;
    }

    function renderFields(manifest: PropTemplate | null, values: Record<string, string>): void {
        const container = byId<HTMLElement>("propEditorFields");
        if (!container) return;
        const fields = manifest?.fields || [];
        container.innerHTML = fields.map((field) => {
            const value = escapeHtml(values[field.key] ?? "");
            const label = `${escapeHtml(field.label || field.key)}${field.required ? ' <span class="prop-required">*</span>' : ""}`;
            const maxLength = Number.isFinite(Number(field.max)) ? ` maxlength="${Number(field.max)}"` : "";
            const required = field.required ? " required" : "";
            const input = field.type === "textarea"
                ? `<textarea class="form-control" rows="6" data-prop-field="${escapeHtml(field.key)}"${maxLength}${required}>${value}</textarea>`
                : `<input type="text" class="form-control" data-prop-field="${escapeHtml(field.key)}" value="${value}"${maxLength}${required}>`;
            return `<div class="prop-editor-field"><label class="form-label">${label}</label>${input}</div>`;
        }).join("");
    }

    function renderWear(wear: Record<string, number>): void {
        const grid = byId<HTMLElement>("propWearGrid");
        if (!grid) return;
        grid.innerHTML = WEAR_KEYS.map((key) => {
            const value = Math.max(0, Math.min(1, Number(wear[key] ?? 0)));
            return `<label class="prop-wear-item">
                <span class="prop-wear-label">${escapeHtml(t(`prop.wear.${key}`, key))}</span>
                <input type="range" min="0" max="1" step="0.05" value="${value}" data-prop-wear="${key}">
                <span class="prop-wear-value" data-prop-wear-value="${key}">${Math.round(value * 100)}%</span>
            </label>`;
        }).join("");
        grid.querySelectorAll<HTMLInputElement>("[data-prop-wear]").forEach((input) => {
            input.addEventListener("input", () => {
                const key = input.dataset.propWear || "";
                const label = grid.querySelector<HTMLElement>(`[data-prop-wear-value="${key}"]`);
                if (label) label.textContent = `${Math.round(Number(input.value) * 100)}%`;
            });
        });
    }

    function collectWear(): Record<string, number> {
        const result: Record<string, number> = {};
        const grid = byId<HTMLElement>("propWearGrid");
        grid?.querySelectorAll<HTMLInputElement>("[data-prop-wear]").forEach((input) => {
            const key = input.dataset.propWear || "";
            if (key) result[key] = Math.max(0, Math.min(1, Number(input.value) || 0));
        });
        return result;
    }

    function refreshPreview(): void {
        const frame = byId<HTMLIFrameElement>("propEditorPreviewFrame");
        const empty = byId<HTMLElement>("propEditorPreviewEmpty");
        if (!frame || !empty) return;
        if (!propId) {
            frame.removeAttribute("src");
            frame.hidden = true;
            empty.hidden = false;
            empty.textContent = t("prop.status.preview_hint", "保存后可预览");
            return;
        }
        frame.hidden = false;
        empty.hidden = true;
        frame.src = api().renderUrl(roomId, propId, "png", Date.now());
    }

    function renderAll(): void {
        const manifest = currentTemplate();
        renderTemplateSelect();
        renderPaperSelect(manifest);
        renderFields(manifest, currentSpec?.fields || {});
        renderWear(currentSpec?.wear || {});
        const titleInput = byId<HTMLInputElement>("propEditorTitle");
        if (titleInput) {
            titleInput.value = currentSpec?.title || "";
            titleInput.placeholder = manifest?.name || t("prop.title.editor_new", "新建道具");
        }
        const label = byId<HTMLElement>("propEditorModalLabel");
        if (label) {
            label.textContent = propId
                ? t("prop.title.editor_edit", "编辑道具")
                : t("prop.title.editor_new", "新建道具");
        }
        refreshPreview();
    }

    async function load(manifestId?: string): Promise<void> {
        setStatus(t("prop.status.loading", "加载中…"));
        try {
            templates = await api().templates(roomId);
            if (propId) {
                currentSpec = await api().get(roomId, propId);
            } else {
                const template = templates.find((item) => item.id === manifestId) || templates[0] || null;
                currentSpec = template
                    ? {
                        id: "",
                        template_id: template.id,
                        title: "",
                        fields: {},
                        style: {},
                        wear: {},
                        redaction: [],
                        visibility: { mode: "hidden", revealed_to: [] },
                    }
                    : null;
            }
            renderAll();
            setStatus("");
        } catch (error) {
            notifyError(error);
        }
    }

    async function runDraft(): Promise<void> {
        if (drafting || !currentSpec) return;
        const manifest = currentTemplate();
        if (!manifest) return;
        const button = byId<HTMLButtonElement>("propDraftRun");
        drafting = true;
        if (button) button.disabled = true;
        setStatus(t("prop.draft.running", "AI 正在起草…"));
        try {
            const result = await api().draft(roomId, {
                template_id: manifest.id,
                era: byId<HTMLInputElement>("propDraftEra")?.value || "",
                place: byId<HTMLInputElement>("propDraftPlace")?.value || "",
                clues: byId<HTMLTextAreaElement>("propDraftClues")?.value || "",
                tone: byId<HTMLInputElement>("propDraftTone")?.value || "",
            });
            currentSpec = {
                ...currentSpec,
                fields: { ...(currentSpec.fields || {}), ...(result.fields || {}) },
                wear: { ...(currentSpec.wear || {}), ...(result.wear || {}) },
                style: { ...(currentSpec.style || {}), ...(result.style || {}) },
            };
            const titleInput = byId<HTMLInputElement>("propEditorTitle");
            if (titleInput && !titleInput.value.trim() && result.title) titleInput.value = result.title;
            renderFields(manifest, currentSpec.fields || {});
            renderWear(currentSpec.wear || {});
            const paperSelect = byId<HTMLSelectElement>("propEditorPaper");
            const paper = result.style?.paper;
            if (paperSelect && paper && Array.from(paperSelect.options).some((option) => option.value === paper)) {
                paperSelect.value = paper;
            }
            setStatus(t("prop.draft.done", "AI 已起草，可继续修改后保存。"), "ok");
        } catch (error) {
            const runtime = api();
            const message = runtime.errorText(error, t("prop.draft.failed", "AI 起草失败，请手动填写。"));
            global.showNotification?.(message, "error");
            setStatus(message, "error");
        } finally {
            drafting = false;
            if (button) button.disabled = false;
        }
    }

    async function save(): Promise<void> {
        if (saving || !currentSpec) return;
        const manifest = currentTemplate();
        const titleInput = byId<HTMLInputElement>("propEditorTitle");
        const paper = byId<HTMLSelectElement>("propEditorPaper")?.value || "";
        const fields = fieldValues();
        const title = titleInput?.value.trim() || "";

        const missing = (manifest?.fields || []).find((field) => field.required && !String(fields[field.key] || "").trim());
        if (missing) {
            const message = t("prop.error.invalid_fields", "有必填字段未填写，或内容超出长度限制。");
            global.showNotification?.(`${message}（${missing.label || missing.key}）`, "error");
            setStatus(message, "error");
            return;
        }

        saving = true;
        const button = byId<HTMLButtonElement>("propEditorSave");
        if (button) button.disabled = true;
        setStatus(t("prop.status.rendering", "生成中…"));
        try {
            const body: PropCreateBody = {
                template_id: currentSpec.template_id,
                title,
                fields,
                style: { ...(currentSpec.style || {}), paper },
                wear: collectWear(),
                redaction: currentSpec.redaction || [],
                visibility: currentSpec.visibility || { mode: "hidden", revealed_to: [] },
            };
            const saved = propId
                ? await api().update(roomId, propId, {
                    title: body.title ?? "",
                    fields: body.fields ?? {},
                    style: body.style ?? {},
                    wear: body.wear ?? {},
                    redaction: body.redaction ?? [],
                })
                : await api().create(roomId, body);
            currentSpec = saved;
            propId = saved.id;
            setStatus(t("prop.status.saved", "已保存"), "ok");
            global.showNotification?.(t("prop.status.saved", "已保存"), "success");
            refreshPreview();
            global.dispatchEvent(new CustomEvent("trpg:props-changed", { detail: { roomId, propId } }));
        } catch (error) {
            notifyError(error);
        } finally {
            saving = false;
            if (button) button.disabled = false;
        }
    }

    function bind(): void {
        if (initialized) return;
        initialized = true;
        const modalElement = byId<HTMLElement>("propEditorModal");
        if (!modalElement) return;
        modal = new bootstrap.Modal(modalElement);
        byId<HTMLButtonElement>("propEditorSave")?.addEventListener("click", () => { void save(); });
        byId<HTMLButtonElement>("propDraftRun")?.addEventListener("click", () => { void runDraft(); });
        byId<HTMLElement>("propEditorTemplate")?.addEventListener("change", () => {
            const values = fieldValues();
            const manifest = currentTemplate();
            renderPaperSelect(manifest);
            renderFields(manifest, values);
            const titleInput = byId<HTMLInputElement>("propEditorTitle");
            if (titleInput && !propId) titleInput.placeholder = manifest?.name || "";
        });
        modalElement.addEventListener("hidden.bs.modal", () => {
            const shouldReturn = returnToLibrary;
            returnToLibrary = false;
            if (shouldReturn) global.TrpgPropLibrary?.reopen?.();
        });
        global.addEventListener("trpg:locale-changed", () => {
            if (modalElement.classList.contains("show")) renderAll();
        });
    }

    function open(options: OpenOptions): void {
        bind();
        roomId = options.roomId;
        propId = options.propId || "";
        returnToLibrary = options.returnToLibrary === true;
        currentSpec = null;
        modal?.show();
        void load(options.templateId);
    }

    global.TrpgPropEditor = { open };
})(window);
