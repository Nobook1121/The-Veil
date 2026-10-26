/**
 * 剧本触发器与媒体编辑器（设计文档 §8.6）。
 *
 * 数据来自 `narrative.json` 的 `triggers` 与 `assets`，媒体列表复用现有
 * `/api/scripts/<id>/assets` 资源库。测试触发只做确定性预览，不写房间。
 */
(function (window: Window): void {
    "use strict";

    interface ResourceItem {
        asset_id?: string;
        used_by?: number;
        hash?: string;
        path?: string;
        url?: string;
        type?: string;
        alt?: string;
        thumb_path?: string;
        thumb_url?: string;
        poster_path?: string;
        poster_url?: string;
        width?: number;
        height?: number;
        duration_ms?: number;
        mime?: string;
        size?: number;
    }

    interface TriggerAttachment {
        id?: string;
        asset_id?: string;
        kind?: string;
        url?: string;
        body?: string | null;
        alt?: string;
        caption?: string;
        visibility?: string;
        spoiler_level?: number;
    }

    interface TriggerBinding {
        kind?: string;
        location_id?: string;
        clue_id?: string;
        npc_id?: string;
        event_id?: string;
        clock_id?: string;
        note?: string;
        [key: string]: unknown;
    }

    interface TriggerSpec {
        id?: string;
        label?: string;
        binding?: TriggerBinding;
        content?: string;
        condition?: unknown;
        attachments?: TriggerAttachment[];
        group?: string;
        visibility?: string;
        spoiler_level?: number;
        once?: boolean;
        once_scope?: string;
        cooldown?: number;
        enabled?: boolean;
        needs_review?: boolean;
    }

    interface TriggerPayload {
        triggers: TriggerSpec[];
        assets: Array<Record<string, unknown>>;
        revision: number;
        permissions: { can_view: boolean; can_edit: boolean; readonly: boolean };
    }

    interface PreviewPayload {
        ok: boolean;
        trigger_id: string;
        binding: TriggerBinding;
        resolved_target: string;
        content?: string;
        attachments: Array<{ kind?: string; url?: string; body?: string | null; caption?: string; alt?: string; visibility?: string; spoiler_level?: number }>;
        warnings: string[];
    }

    interface TriggerState {
        scenarioId: number;
        triggers: TriggerSpec[];
        resources: ResourceItem[];
        revision: number;
        readonly: boolean;
        canEdit: boolean;
        selectedNode: string;
        search: string;
        draft: TriggerSpec | null;
        editingId: string;
        busy: boolean;
    }

    const BINDING_TARGET_KEY: Record<string, string> = {
        enter: "location_id",
        interaction: "location_id",
        clue: "clue_id",
        npc: "npc_id",
        event: "event_id",
        clock: "clock_id",
    };

    const BINDING_LABEL: Record<string, string> = {
        enter: "进入地点",
        interaction: "地点互动",
        clue: "发现线索",
        npc: "NPC 对话",
        event: "事件",
        clock: "时钟",
        manual: "手动",
    };

    const state: TriggerState = {
        scenarioId: 0,
        triggers: [],
        resources: [],
        revision: 0,
        readonly: false,
        canEdit: false,
        selectedNode: "",
        search: "",
        draft: null,
        editingId: "",
        busy: false,
    };

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function escapeHtml(value: unknown): string {
        const entities: Record<string, string> = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            "\"": "&quot;",
            "'": "&#39;",
        };
        return String(value ?? "").replace(/[&<>"']/g, (char) => entities[char] || char);
    }

    function editable(): boolean {
        return state.canEdit && !state.readonly;
    }

    function assetKey(resource: ResourceItem): string {
        return resource.asset_id || resource.hash || resource.path || resource.url || "";
    }

    function assetId(resource: ResourceItem): string {
        if (resource.asset_id) return resource.asset_id;
        const key = resource.hash || resource.path || resource.url || "";
        return key ? `asset_${key.slice(0, 16)}` : "";
    }

    function resourceById(identifier: string): ResourceItem | null {
        return state.resources.find((item) => assetId(item) === identifier || assetKey(item) === identifier) || null;
    }

    /** 缩略图接口（§8.5）：服务端生成的 thumb/poster，缺失时后端回退原图。 */
    function resourceThumb(resource: ResourceItem | null): string {
        const identifier = resource ? (resource.asset_id || resource.hash || resource.path || "") : "";
        if (!identifier || !state.scenarioId) return "";
        return `/api/scripts/${state.scenarioId}/assets/${encodeURIComponent(identifier)}/thumb`;
    }

    function bindingTarget(binding: TriggerBinding | undefined): { key: string; value: string } {
        const kind = binding?.kind || "enter";
        const key = BINDING_TARGET_KEY[kind] || "";
        return { key, value: key ? String(binding?.[key] || "") : "" };
    }

    function bindingText(binding: TriggerBinding | undefined): string {
        const kind = binding?.kind || "manual";
        if (kind === "manual") return binding?.note ? `手动：${binding.note}` : "手动";
        const target = bindingTarget(binding);
        return target.value ? `${BINDING_LABEL[kind] || kind}：${target.value}` : (BINDING_LABEL[kind] || kind);
    }

    function matchesNode(trigger: TriggerSpec, nodeId: string): boolean {
        if (!nodeId) return true;
        const binding = trigger.binding || {};
        const target = bindingTarget(binding).value;
        return Boolean(target && target === nodeId);
    }

    function matchesSearch(trigger: TriggerSpec): boolean {
        if (!state.search) return true;
        const haystack = [trigger.id, trigger.label, trigger.content, bindingText(trigger.binding)].join(" ").toLowerCase();
        return haystack.includes(state.search);
    }

    function attachmentThumb(attachment: TriggerAttachment): string {
        const resource = attachment.asset_id ? resourceById(attachment.asset_id) : null;
        return resourceThumb(resource) || attachment.url || resource?.thumb_url || resource?.poster_url || resource?.url || "";
    }

    function attachmentChip(attachment: TriggerAttachment): string {
        const url = attachmentThumb(attachment);
        const alt = attachment.alt || attachment.caption || attachment.kind || "附件";
        const kind = attachment.kind || "image";
        if (url && (kind === "image" || kind === "video")) {
            return `<span class="scenario-trigger-chip"><img src="${escapeHtml(url)}" alt="${escapeHtml(alt)}" loading="lazy"><span>${escapeHtml(alt)}</span></span>`;
        }
        return `<span class="scenario-trigger-chip"><i class="fa fa-paperclip" aria-hidden="true"></i><span>${escapeHtml(alt)}</span></span>`;
    }

    function triggerCard(trigger: TriggerSpec): string {
        const attachments = trigger.attachments || [];
        const badges = [
            trigger.visibility === "kp_only" ? `<span class="badge bg-danger">KP</span>` : "",
            trigger.once === false ? `<span class="badge bg-secondary">重复</span>` : "",
            trigger.enabled === false ? `<span class="badge bg-warning text-dark">停用</span>` : "",
            trigger.needs_review ? `<span class="badge bg-warning text-dark">待复核</span>` : "",
        ].filter(Boolean).join(" ");
        const canEdit = editable();
        return `
            <article class="scenario-trigger-card" data-trigger-id="${escapeHtml(trigger.id)}">
                <div class="scenario-trigger-card-main">
                    <div class="scenario-trigger-card-title">
                        <strong>${escapeHtml(trigger.label || trigger.id)}</strong>
                        <code>${escapeHtml(trigger.id)}</code>
                        ${badges}
                    </div>
                    <div class="scenario-trigger-card-meta">
                        <span><i class="fa fa-bolt" aria-hidden="true"></i> ${escapeHtml(bindingText(trigger.binding))}</span>
                        ${trigger.content ? `<span class="text-muted">${escapeHtml(trigger.content)}</span>` : ""}
                    </div>
                    ${attachments.length ? `<div class="scenario-trigger-chips">${attachments.map(attachmentChip).join("")}</div>` : ""}
                </div>
                <div class="scenario-trigger-card-actions">
                    <button type="button" class="btn btn-sm btn-outline-secondary" data-trigger-test="${escapeHtml(trigger.id)}">
                        <i class="fa fa-play" aria-hidden="true"></i>
                        <span>测试</span>
                    </button>
                    ${canEdit ? `
                    <button type="button" class="btn btn-sm btn-outline-primary" data-trigger-edit="${escapeHtml(trigger.id)}">
                        <i class="fa fa-pen" aria-hidden="true"></i>
                        <span>编辑</span>
                    </button>
                    <button type="button" class="btn btn-sm btn-outline-danger" data-trigger-delete="${escapeHtml(trigger.id)}">
                        <i class="fa fa-trash" aria-hidden="true"></i>
                    </button>` : ""}
                </div>
            </article>`;
    }

    function renderList(): void {
        const list = byId("scenarioTriggerList");
        if (!list) return;
        if (!state.scenarioId) {
            list.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.trigger.select_scenario", "请先选择剧本。"))}</p>`;
            return;
        }
        const visible = state.triggers.filter((trigger) => matchesNode(trigger, state.selectedNode) && matchesSearch(trigger));
        if (visible.length === 0) {
            const hint = state.selectedNode
                ? t("scenario.trigger.empty_node", "当前节点还没有绑定触发器。")
                : t("scenario.trigger.empty", "这个剧本还没有触发器；可保存剧本后重新生成世界树，或直接添加。");
            list.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(hint)}</p>`;
            return;
        }
        list.innerHTML = visible.map(triggerCard).join("");
    }

    function assetOptions(selected: string): string {
        const options = state.resources.map((resource) => {
            const identifier = assetId(resource);
            const label = resource.alt || resource.path || identifier;
            return `<option value="${escapeHtml(identifier)}"${identifier === selected ? " selected" : ""}>${escapeHtml(label)}</option>`;
        }).join("");
        return `<option value="">— 不使用资源库 —</option>${options}`;
    }

    function attachmentRow(attachment: TriggerAttachment, index: number): string {
        const kind = attachment.kind || "image";
        return `
            <div class="scenario-trigger-attachment" data-att-index="${index}">
                <select class="form-select form-select-sm" data-att-field="kind" aria-label="附件类型">
                    ${["image", "video", "audio", "richtext", "file"].map((value) => `<option value="${value}"${kind === value ? " selected" : ""}>${value}</option>`).join("")}
                </select>
                <select class="form-select form-select-sm" data-att-field="asset_id" aria-label="资源">
                    ${assetOptions(attachment.asset_id || "")}
                </select>
                <input type="text" class="form-control form-control-sm" data-att-field="alt" value="${escapeHtml(attachment.alt || "")}" placeholder="alt 文本">
                <input type="text" class="form-control form-control-sm" data-att-field="caption" value="${escapeHtml(attachment.caption || "")}" placeholder="说明">
                <select class="form-select form-select-sm" data-att-field="visibility" aria-label="可见性">
                    <option value="player_visible"${(attachment.visibility || "player_visible") === "player_visible" ? " selected" : ""}>玩家可见</option>
                    <option value="kp_only"${attachment.visibility === "kp_only" ? " selected" : ""}>仅 KP</option>
                </select>
                <input type="number" class="form-control form-control-sm" data-att-field="spoiler_level" min="0" max="5" value="${Number(attachment.spoiler_level || 0)}" aria-label="剧透等级">
                ${kind === "richtext" ? `<textarea class="form-control form-control-sm" data-att-field="body" rows="2" placeholder="富文本内容">${escapeHtml(attachment.body || "")}</textarea>` : ""}
                <button type="button" class="btn btn-sm btn-outline-danger" data-att-remove="${index}" aria-label="移除附件"><i class="fa fa-xmark" aria-hidden="true"></i></button>
            </div>`;
    }

    function renderEditor(): void {
        const editor = byId("scenarioTriggerEditor");
        if (!editor) return;
        const draft = state.draft;
        if (!draft) {
            editor.hidden = true;
            editor.innerHTML = "";
            return;
        }
        editor.hidden = false;
        const binding = draft.binding || { kind: "enter" };
        const kind = binding.kind || "enter";
        const target = bindingTarget(binding);
        const attachments = draft.attachments || [];
        editor.innerHTML = `
            <div class="scenario-trigger-form">
                <div class="scenario-trigger-form-head">
                    <h6>${escapeHtml(state.editingId ? t("scenario.trigger.edit", "编辑触发器") : t("scenario.trigger.create", "新建触发器"))}</h6>
                    <button type="button" class="btn-close" id="scenarioTriggerCancel" aria-label="关闭"></button>
                </div>
                <div class="scenario-trigger-grid">
                    <label><span>${escapeHtml(t("scenario.trigger.label", "名称"))}</span><input type="text" class="form-control form-control-sm" id="scenarioTriggerLabel" value="${escapeHtml(draft.label || "")}"></label>
                    <label><span>${escapeHtml(t("scenario.trigger.binding", "绑定"))}</span>
                        <select class="form-select form-select-sm" id="scenarioTriggerBindingKind">
                            ${Object.keys(BINDING_LABEL).map((value) => `<option value="${value}"${kind === value ? " selected" : ""}>${escapeHtml(BINDING_LABEL[value])}</option>`).join("")}
                        </select>
                    </label>
                    <label><span>${escapeHtml(t("scenario.trigger.target", "目标节点"))}</span><input type="text" class="form-control form-control-sm" id="scenarioTriggerTarget" value="${escapeHtml(target.value)}" placeholder="node id"></label>
                    <label><span>${escapeHtml(t("scenario.trigger.group", "分组"))}</span><input type="text" class="form-control form-control-sm" id="scenarioTriggerGroup" value="${escapeHtml(draft.group || "")}"></label>
                    <label><span>${escapeHtml(t("scenario.trigger.visibility", "可见性"))}</span>
                        <select class="form-select form-select-sm" id="scenarioTriggerVisibility">
                            <option value="player_visible"${(draft.visibility || "player_visible") === "player_visible" ? " selected" : ""}>玩家可见</option>
                            <option value="kp_only"${draft.visibility === "kp_only" ? " selected" : ""}>仅 KP</option>
                        </select>
                    </label>
                    <label><span>${escapeHtml(t("scenario.trigger.spoiler", "剧透等级"))}</span><input type="number" class="form-control form-control-sm" id="scenarioTriggerSpoiler" min="0" max="5" value="${Number(draft.spoiler_level || 0)}"></label>
                    <label><span>${escapeHtml(t("scenario.trigger.cooldown", "冷却轮数"))}</span><input type="number" class="form-control form-control-sm" id="scenarioTriggerCooldown" min="0" value="${Number(draft.cooldown || 0)}"></label>
                    <label class="scenario-trigger-inline"><span>${escapeHtml(t("scenario.trigger.once", "只触发一次"))}</span><input type="checkbox" id="scenarioTriggerOnce"${draft.once !== false ? " checked" : ""}></label>
                    <label><span>${escapeHtml(t("scenario.trigger.once_scope", "一次范围"))}</span>
                        <select class="form-select form-select-sm" id="scenarioTriggerOnceScope">
                            <option value="room"${(draft.once_scope || "room") === "room" ? " selected" : ""}>房间</option>
                            <option value="pc"${draft.once_scope === "pc" ? " selected" : ""}>每角色</option>
                        </select>
                    </label>
                    <label class="scenario-trigger-inline"><span>${escapeHtml(t("scenario.trigger.enabled", "启用"))}</span><input type="checkbox" id="scenarioTriggerEnabled"${draft.enabled !== false ? " checked" : ""}></label>
                </div>
                <label class="scenario-trigger-content"><span>${escapeHtml(t("scenario.trigger.content", "文本内容"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioTriggerContent" rows="2">${escapeHtml(draft.content || "")}</textarea>
                </label>
                <div class="scenario-trigger-attachments">
                    <div class="scenario-trigger-attachments-head">
                        <span>${escapeHtml(t("scenario.trigger.attachments", "附件"))}</span>
                        <button type="button" class="btn btn-sm btn-outline-secondary" id="scenarioTriggerAddAttachment"><i class="fa fa-plus" aria-hidden="true"></i></button>
                    </div>
                    ${attachments.map((attachment, index) => attachmentRow(attachment, index)).join("")}
                </div>
                <div class="scenario-trigger-form-actions">
                    <button type="button" class="btn btn-sm btn-outline-secondary" id="scenarioTriggerCancelFooter">${escapeHtml(t("common.cancel", "取消"))}</button>
                    <button type="button" class="btn btn-sm btn-primary" id="scenarioTriggerSave">${escapeHtml(t("scenario.trigger.save", "保存触发器"))}</button>
                </div>
            </div>`;
    }

    function openEditor(triggerId: string, seedNode: string): void {
        if (!editable()) return;
        if (triggerId) {
            const existing = state.triggers.find((item) => item.id === triggerId);
            if (!existing) return;
            state.editingId = triggerId;
            state.draft = JSON.parse(JSON.stringify(existing)) as TriggerSpec;
        } else {
            state.editingId = "";
            const binding = seedNode ? { kind: "enter", location_id: seedNode } : { kind: "enter" };
            state.draft = {
                id: `trg_${Date.now().toString(36)}`,
                label: seedNode ? `节点触发器` : "新触发器",
                binding,
                content: "",
                attachments: [],
                visibility: "player_visible",
                once: true,
                once_scope: "room",
                cooldown: 0,
                enabled: true,
                spoiler_level: 0,
            };
        }
        renderEditor();
        byId("scenarioTriggerEditor")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function closeEditor(): void {
        state.draft = null;
        state.editingId = "";
        renderEditor();
    }

    function syncDraftFromFields(): void {
        const draft = state.draft;
        if (!draft) return;
        const label = byId<HTMLInputElement>("scenarioTriggerLabel")?.value.trim();
        const kind = byId<HTMLSelectElement>("scenarioTriggerBindingKind")?.value || "enter";
        const target = byId<HTMLInputElement>("scenarioTriggerTarget")?.value.trim() || "";
        const group = byId<HTMLInputElement>("scenarioTriggerGroup")?.value.trim() || "";
        const visibility = byId<HTMLSelectElement>("scenarioTriggerVisibility")?.value || "player_visible";
        const spoiler = Number(byId<HTMLInputElement>("scenarioTriggerSpoiler")?.value || 0);
        const cooldown = Number(byId<HTMLInputElement>("scenarioTriggerCooldown")?.value || 0);
        const once = byId<HTMLInputElement>("scenarioTriggerOnce")?.checked !== false;
        const onceScope = byId<HTMLSelectElement>("scenarioTriggerOnceScope")?.value || "room";
        const enabled = byId<HTMLInputElement>("scenarioTriggerEnabled")?.checked !== false;
        const content = byId<HTMLTextAreaElement>("scenarioTriggerContent")?.value || "";
        const binding: TriggerBinding = { kind };
        const key = BINDING_TARGET_KEY[kind];
        if (key && target) binding[key] = target;
        draft.label = label || draft.label || "触发器";
        draft.binding = binding;
        draft.group = group;
        draft.visibility = visibility;
        draft.spoiler_level = Math.max(0, Math.min(5, spoiler));
        draft.cooldown = Math.max(0, cooldown);
        draft.once = once;
        draft.once_scope = onceScope;
        draft.enabled = enabled;
        draft.content = content;
    }

    function syncAttachmentFromEvent(target: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): void {
        const row = target.closest<HTMLElement>("[data-att-index]");
        if (!row || !state.draft) return;
        const index = Number(row.dataset.attIndex);
        const attachment = (state.draft.attachments || [])[index];
        if (!attachment) return;
        const field = target.dataset.attField || "";
        if (!field) return;
        if (field === "spoiler_level") {
            attachment.spoiler_level = Math.max(0, Math.min(5, Number(target.value || 0)));
            return;
        }
        (attachment as unknown as Record<string, unknown>)[field] = target.value;
        if (field === "asset_id") {
            const resource = resourceById(target.value);
            if (resource) {
                attachment.url = resource.url || attachment.url || "";
                attachment.alt = resource.alt || attachment.alt || "";
                if (!attachment.kind) attachment.kind = resource.type || "file";
            }
        }
    }

    async function handleConflict(): Promise<void> {
        showNotification(t("scenario.tree.conflict", "内容已被其他编辑修改，正在刷新…"), "warning");
        await load(state.scenarioId, { readonly: state.readonly });
    }

    async function saveTrigger(): Promise<void> {
        if (!state.draft || !state.scenarioId || state.busy || !editable()) return;
        syncDraftFromFields();
        const draft = state.draft;
        if (!draft.binding?.kind) {
            showNotification(t("scenario.trigger.invalid_binding", "请选择触发器绑定。" ), "warning");
            return;
        }
        state.busy = true;
        try {
            const isUpdate = Boolean(state.editingId);
            const url = isUpdate
                ? `/api/scenarios/${state.scenarioId}/narrative/triggers/${encodeURIComponent(state.editingId)}`
                : `/api/scenarios/${state.scenarioId}/narrative/triggers`;
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ trigger: TriggerSpec; revision: number }>>(
                url,
                { method: isUpdate ? "PUT" : "POST", body: { base_revision: state.revision, trigger: draft } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            const saved = data.data.trigger;
            if (isUpdate) {
                state.triggers = state.triggers.map((item) => (item.id === saved.id ? saved : item));
            } else {
                state.triggers.push(saved);
            }
            closeEditor();
            renderList();
            showNotification(t("scenario.trigger.saved", "触发器已保存。"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            state.busy = false;
        }
    }

    async function deleteTrigger(triggerId: string): Promise<void> {
        if (!state.scenarioId || !editable()) return;
        const trigger = state.triggers.find((item) => item.id === triggerId);
        const confirmed = await window.TrpgDialog?.confirm({
            title: t("scenario.trigger.delete_title", "删除触发器"),
            message: t("scenario.trigger.delete_message", "删除后无法恢复。"),
            confirmText: t("common.delete", "删除"),
            danger: true,
        });
        if (!confirmed) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/triggers/${encodeURIComponent(triggerId)}`,
                { method: "DELETE", body: { base_revision: state.revision } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            state.triggers = state.triggers.filter((item) => item.id !== triggerId);
            if (state.editingId === triggerId) closeEditor();
            renderList();
            showNotification(t("scenario.trigger.deleted", "触发器已删除。"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
            void trigger;
        }
    }

    function renderPreview(payload: PreviewPayload): void {
        const container = byId("scenarioTriggerTestResult");
        if (!container) return;
        container.hidden = false;
        const attachments = payload.attachments.map((attachment) => {
            const url = attachment.url || "";
            if (attachment.kind === "image" && url) {
                return `<div class="scenario-trigger-preview-attachment"><img src="${escapeHtml(url)}" alt="${escapeHtml(attachment.alt || "")}" loading="lazy"><span>${escapeHtml(attachment.caption || attachment.alt || "")}</span></div>`;
            }
            if (attachment.kind === "richtext" && attachment.body) {
                return `<pre class="scenario-trigger-preview-text">${escapeHtml(attachment.body)}</pre>`;
            }
            if ((attachment.kind === "audio" || attachment.kind === "video") && url) {
                return `<div class="scenario-trigger-preview-attachment"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(attachment.alt || url)}</a></div>`;
            }
            return `<div class="scenario-trigger-preview-attachment"><span>${escapeHtml(attachment.alt || attachment.caption || attachment.kind || "附件")}</span></div>`;
        }).join("");
        container.innerHTML = `
            <div class="scenario-trigger-preview ${payload.ok ? "ok" : "warn"}">
                <div class="scenario-trigger-preview-head">
                    <span class="badge bg-${payload.ok ? "success" : "warning"}">${payload.ok ? "可解析" : "绑定无效"}</span>
                    <code>${escapeHtml(payload.trigger_id)}</code>
                    <span class="text-muted small">${escapeHtml(bindingText(payload.binding))} → ${escapeHtml(payload.resolved_target)}</span>
                    <button type="button" class="btn-close ms-auto" data-preview-close aria-label="关闭"></button>
                </div>
                ${payload.content ? `<p class="mb-2">${escapeHtml(payload.content)}</p>` : ""}
                ${attachments}
                ${payload.warnings.length ? `<ul class="mb-0 mt-2 small text-warning">${payload.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : ""}
            </div>`;
    }

    async function testTrigger(triggerId: string): Promise<void> {
        if (!state.scenarioId) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<PreviewPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/triggers/${encodeURIComponent(triggerId)}/test`,
                { method: "POST", body: {} },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            renderPreview(data.data);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    function applyRevision(revision: number): void {
        state.revision = revision;
        window.ScenarioNarrative?.syncRevision(revision);
    }

    function syncRevision(revision: number): void {
        state.revision = revision;
    }

    async function loadResources(scenarioId: number): Promise<void> {
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ResourceItem[]>>(
                `/api/scripts/${scenarioId}/assets`,
            );
            if (response.ok && data?.success && Array.isArray(data.data)) {
                state.resources = data.data;
            }
        } catch {
            state.resources = [];
        }
    }

    async function load(scenarioId: number | string, options: { readonly?: boolean } = {}): Promise<void> {
        const parsed = typeof scenarioId === "number" ? scenarioId : Number.parseInt(scenarioId, 10);
        state.scenarioId = Number.isFinite(parsed) ? parsed : 0;
        state.triggers = [];
        state.resources = [];
        state.revision = 0;
        state.readonly = options.readonly === true;
        state.canEdit = false;
        state.selectedNode = "";
        state.search = "";
        state.draft = null;
        state.editingId = "";
        const search = byId<HTMLInputElement>("scenarioTriggerSearch");
        if (search) search.value = "";
        const testResult = byId("scenarioTriggerTestResult");
        if (testResult) {
            testResult.hidden = true;
            testResult.innerHTML = "";
        }
        renderEditor();
        if (!state.scenarioId) {
            renderList();
            return;
        }
        await loadResources(state.scenarioId);
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<TriggerPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/triggers`,
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 404) {
                    renderList();
                    showNotification(t("scenario.trigger.no_narrative", "该剧本还没有世界树，触发器暂时只读。"), "info");
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.triggers = data.data.triggers || [];
            state.revision = data.data.revision;
            state.readonly = Boolean(data.data.permissions?.readonly) || options.readonly === true;
            state.canEdit = Boolean(data.data.permissions?.can_edit);
            renderList();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            renderList();
            showNotification(message, "error");
        }
    }

    function clear(): void {
        state.scenarioId = 0;
        state.triggers = [];
        state.resources = [];
        state.revision = 0;
        state.readonly = false;
        state.canEdit = false;
        state.selectedNode = "";
        state.search = "";
        state.draft = null;
        state.editingId = "";
        const search = byId<HTMLInputElement>("scenarioTriggerSearch");
        if (search) search.value = "";
        const testResult = byId("scenarioTriggerTestResult");
        if (testResult) {
            testResult.hidden = true;
            testResult.innerHTML = "";
        }
        renderEditor();
        renderList();
    }

    function init(): void {
        byId("scenarioTriggerSearch")?.addEventListener("input", (event) => {
            state.search = (event.target as HTMLInputElement).value.trim().toLowerCase();
            renderList();
        });
        byId("scenarioTriggerAdd")?.addEventListener("click", () => {
            if (editable()) openEditor("", state.selectedNode);
        });
        byId("scenarioTriggerList")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            const edit = target.closest<HTMLElement>("[data-trigger-edit]");
            if (edit?.dataset.triggerEdit) {
                openEditor(edit.dataset.triggerEdit, "");
                return;
            }
            const remove = target.closest<HTMLElement>("[data-trigger-delete]");
            if (remove?.dataset.triggerDelete) {
                void deleteTrigger(remove.dataset.triggerDelete);
                return;
            }
            const test = target.closest<HTMLElement>("[data-trigger-test]");
            if (test?.dataset.triggerTest) void testTrigger(test.dataset.triggerTest);
        });
        byId("scenarioTriggerEditor")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("#scenarioTriggerCancel") || target.closest("#scenarioTriggerCancelFooter")) {
                closeEditor();
                return;
            }
            if (target.closest("#scenarioTriggerSave")) {
                void saveTrigger();
                return;
            }
            if (target.closest("#scenarioTriggerAddAttachment")) {
                if (!state.draft) return;
                const attachments = state.draft.attachments || [];
                attachments.push({ kind: "image", visibility: "player_visible", spoiler_level: 0 });
                state.draft.attachments = attachments;
                renderEditor();
                return;
            }
            const remove = target.closest<HTMLElement>("[data-att-remove]");
            if (remove && state.draft) {
                const index = Number(remove.dataset.attRemove);
                state.draft.attachments = (state.draft.attachments || []).filter((_, position) => position !== index);
                renderEditor();
            }
        });
        byId("scenarioTriggerEditor")?.addEventListener("change", (event) => {
            const target = event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
            if (target.dataset.attField) {
                syncAttachmentFromEvent(target);
                if (target.dataset.attField === "kind" || target.dataset.attField === "asset_id") renderEditor();
                return;
            }
            if (target.id === "scenarioTriggerBindingKind") {
                if (state.draft) state.draft.binding = { kind: target.value };
                renderEditor();
                return;
            }
            syncDraftFromFields();
        });
        byId("scenarioTriggerEditor")?.addEventListener("input", (event) => {
            const target = event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
            if (target.dataset.attField) syncAttachmentFromEvent(target);
            else syncDraftFromFields();
        });
        byId("scenarioTriggerTestResult")?.addEventListener("click", (event) => {
            if ((event.target as HTMLElement).closest("[data-preview-close]")) {
                const container = byId("scenarioTriggerTestResult");
                if (container) {
                    container.hidden = true;
                    container.innerHTML = "";
                }
            }
        });
        document.addEventListener("scenario:node-selected", (event) => {
            const detail = (event as CustomEvent<{ nodeId?: string }>).detail || {};
            state.selectedNode = detail.nodeId || "";
            renderList();
        });
    }

    window.ScenarioTriggers = { load, clear, init, syncRevision };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
