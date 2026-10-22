class ScenarioView {
    scenarioList: HTMLElement;
    saveScenarioHandler: () => Promise<void>;
    private handlers: ScenarioViewHandlers | null = null;
    private draftTimer: number | null = null;
    private isCreating = false;
    private conversionProgressModal: HTMLElement | null = null;
    private conversionProgressTimer: number | null = null;
    private batchMode = false;
    private readonly selectedScenarioIds = new Set<number>();
    private currentScenarios: Scenario[] = [];
    /** 正在编辑的是文档直接导入的剧本：没有场景卡，因此不要求场景/结局模块。 */
    private directImportEditing = false;
    /** 允许直接关闭编辑器（保存/发布成功、或用户已在草稿确认框中选择）。 */
    private closeAllowed = false;
    /** 打开编辑器时的表单快照；与当前值比较即可判断「是否有未保存改动」。 */
    private formBaseline: string | null = null;

    constructor() {
        const scenarioList = document.getElementById("scenarioList");
        if (!scenarioList) throw new Error("missing scenarioList container");
        this.scenarioList = scenarioList;
        this.saveScenarioHandler = async () => {
            await runSaveButtonCooldown(document.getElementById("saveScenario"), async () => {
                await this.handlers?.onSaveScenario();
            });
        };
        this.initEventListeners();
        this.bindScenarioActions();
        this.bindBatchActions();
    }

    setEventHandlers(handlers: ScenarioViewHandlers): void {
        this.handlers = handlers;
    }

    /** 未登录时后端会拒绝创建/导入剧本，这里提前拦截并引导登录。 */
    private requireLogin(action: string): boolean {
        if (window.currentUser) return true;
        showNotification(`请先登录后再${action}`, "warning");
        AuthModule.showAuthModal();
        return false;
    }

    renderScenarioList(scenarios: Scenario[]): void {
        this.currentScenarios = scenarios;
        this.scenarioList.innerHTML = "";
        // 数据刷新后同步已选集合，避免删掉的剧本仍留在选中态里
        const selectableIds = new Set(scenarios.filter((scenario) => this.canDeleteScenario(scenario)).map((scenario) => scenario.id));
        [...this.selectedScenarioIds].forEach((id) => {
            if (!selectableIds.has(id)) this.selectedScenarioIds.delete(id);
        });

        scenarios.forEach((scenario) => {
            const card = document.createElement("div");
            const selectable = this.batchMode && this.canDeleteScenario(scenario);
            const selected = selectable && this.selectedScenarioIds.has(scenario.id);
            card.className = `scenario-card${this.batchMode ? " batch-mode" : ""}${selected ? " batch-selected" : ""}`;
            card.dataset.scenarioId = String(scenario.id);
            if (selectable) card.dataset.batchSelectable = "true";
            card.innerHTML = window.TrpgTemplates.render("scenario-card", {
                coverHtml: scenarioCoverHtml(scenario.title, scenario.cover),
                title: scenario.title,
                author: scenario.author,
                createdBy: scenario.creator_username || scenario.author || "Unknown",
                playerCount: scenario.playerCount,
                id: scenario.id,
                publicId: scenario.public_id || String(scenario.id),
                scenarioVersion: scenario.scenario_version || "1.0.0",
                actionButtons: this.batchMode ? "" : this.renderScenarioActionButtons(scenario),
            });
            if (selectable) {
                const check = document.createElement("span");
                check.className = "batch-card-check";
                check.setAttribute("aria-hidden", "true");
                check.innerHTML = `<i class="fa ${selected ? "fa-check-square-o" : "fa-square-o"}"></i>`;
                card.prepend(check);
            }
            window.TrpgI18n?.apply(card);
            this.scenarioList.appendChild(card);
        });
        this.updateBatchToolbar();
    }

    private canDeleteScenario(scenario: Scenario): boolean {
        return canUseScenarioPermission("scenarios.delete") && canModifyScenario(scenario);
    }

    private bindBatchActions(): void {
        document.getElementById("scenarioBatchToggle")?.addEventListener("click", () => {
            this.setBatchMode(!this.batchMode);
        });
        document.getElementById("scenarioBatchExit")?.addEventListener("click", () => {
            this.setBatchMode(false);
        });
        document.getElementById("scenarioBatchDelete")?.addEventListener("click", () => {
            void this.deleteSelectedScenarios();
        });
        document.getElementById("scenarioBatchSelectAll")?.addEventListener("change", (event) => {
            this.setAllScenariosSelected((event.target as HTMLInputElement).checked);
        });
    }

    private setBatchMode(enabled: boolean): void {
        if (this.batchMode === enabled) return;
        this.batchMode = enabled;
        if (!enabled) this.selectedScenarioIds.clear();
        this.renderScenarioList(this.currentScenarios);
    }

    private setAllScenariosSelected(select: boolean): void {
        this.scenarioList.querySelectorAll<HTMLElement>(".scenario-card[data-batch-selectable='true']").forEach((card) => {
            const id = Number.parseInt(card.dataset.scenarioId || "", 10);
            if (!Number.isFinite(id)) return;
            if (select) this.selectedScenarioIds.add(id); else this.selectedScenarioIds.delete(id);
            card.classList.toggle("batch-selected", select);
            const icon = card.querySelector<HTMLElement>(".batch-card-check i");
            if (icon) icon.className = `fa ${select ? "fa-check-square-o" : "fa-square-o"}`;
        });
        this.updateBatchToolbar();
    }

    private updateBatchToolbar(): void {
        const toolbar = document.getElementById("scenarioBatchToolbar");
        const toggle = document.getElementById("scenarioBatchToggle");
        toggle?.classList.toggle("active", this.batchMode);
        if (!toolbar) return;
        toolbar.hidden = !this.batchMode;
        const count = this.selectedScenarioIds.size;
        const countEl = document.getElementById("scenarioBatchCount");
        if (countEl) {
            countEl.textContent = count > 0 ? scenarioT("common.batch.selected", "已选 {count} 项", { count }) : "";
        }
        const selectAll = document.getElementById("scenarioBatchSelectAll") as HTMLInputElement | null;
        if (selectAll) {
            const selectable = this.scenarioList.querySelectorAll(".scenario-card[data-batch-selectable='true']").length;
            selectAll.checked = selectable > 0 && count === selectable;
            selectAll.indeterminate = count > 0 && count < selectable;
            selectAll.disabled = selectable === 0;
        }
        const deleteButton = document.getElementById("scenarioBatchDelete") as HTMLButtonElement | null;
        if (deleteButton) deleteButton.disabled = count === 0;
    }

    private async deleteSelectedScenarios(): Promise<void> {
        const ids = [...this.selectedScenarioIds];
        if (!ids.length) {
            this.showMessage(scenarioT("common.batch.none_selected", "请先选择要删除的项目"));
            return;
        }
        const confirmed = await window.TrpgDialog?.confirm({
            title: "批量删除剧本",
            message: scenarioT("common.batch.delete_confirm", "确定要删除选中的 {count} 项吗？此操作不可恢复。", { count: ids.length }),
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;
        // 先退出批量模式，控制器完成删除后会以普通视图重新渲染列表
        this.selectedScenarioIds.clear();
        this.batchMode = false;
        await this.handlers?.onBatchDeleteScenarios(ids);
    }

    async openCreateModal(): Promise<void> {
        this.isCreating = true;
        this.resetScenarioForm();
        this.setImportReviewReadOnly(false);
        // 允许 ESC / 点击遮罩关闭（默认行为）；关闭时若有未保存改动会弹草稿确认。
        const modal = new bootstrap.Modal(requiredElement("scenarioModal"));
        modal.show();
    }

    openEditModal(scenario: Scenario): void {
        this.isCreating = false;
        this.fillScenarioForm(scenario);
        this.setImportReviewReadOnly(false);
        new bootstrap.Modal(requiredElement("scenarioModal")).show();
    }

    fillDraftData(draft: ScenarioInput): void {
        this.fillScenarioForm({ id: 0, title: draft.title || "", author: draft.author || "", playerCount: draft.playerCount || 0, notes: draft.notes || "", ...(draft.allow_open_ending === undefined ? {} : { allow_open_ending: draft.allow_open_ending }), modules: draft.modules || [], cover: draft.cover || "" });
        updateScenarioModalTitle("scenario.modal.create", "创建剧本");
    }

    setImportReviewReadOnly(readonly: boolean): void {
        const modal = document.getElementById("scenarioModal");
        if (!modal) return;
        modal.dataset.importReviewReadonly = readonly ? "true" : "false";
        modal.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("input, textarea, select").forEach((field) => {
            if (field.id !== "scenarioPublicId") field.disabled = readonly;
        });
        document.getElementById("addModule")?.classList.toggle("d-none", readonly);
        document.getElementById("saveScenarioDraft")?.classList.toggle("d-none", readonly);
    }

    showDraftPrompt(): Promise<"continue" | "discard" | "cancel"> {
        return new Promise((resolve) => {
            const modal = document.createElement("div");
            modal.className = "modal fade";
            modal.tabIndex = -1;
            modal.setAttribute("data-bs-backdrop", "static");
            modal.setAttribute("data-bs-keyboard", "false");
            modal.setAttribute("aria-labelledby", "scenarioDraftPromptLabel");
            modal.setAttribute("aria-hidden", "true");
            modal.innerHTML = `
                <div class="modal-dialog modal-dialog-centered">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title" id="scenarioDraftPromptLabel">发现剧本草稿</h5>
                        </div>
                        <div class="modal-body">检测到尚未发布的剧本草稿，请选择下一步。</div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-primary" data-draft-action="continue">继续编辑</button>
                            <button type="button" class="btn btn-danger" data-draft-action="discard">舍弃</button>
                            <button type="button" class="btn btn-secondary" data-draft-action="cancel">取消</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(modal);
            window.TrpgI18n?.apply(modal);
            const instance = new bootstrap.Modal(modal, { backdrop: "static" });
            let settled = false;
            const finish = (action: "continue" | "discard" | "cancel") => {
                if (settled) return;
                settled = true;
                instance.hide();
                resolve(action);
            };
            modal.querySelectorAll<HTMLElement>("[data-draft-action]").forEach((button) => {
                button.addEventListener("click", () => finish((button.dataset.draftAction || "cancel") as "continue" | "discard" | "cancel"));
            });
            modal.addEventListener("hidden.bs.modal", () => {
                modal.remove();
                if (!settled) resolve("cancel");
            });
            instance.show();
        });
    }

    closeModal(): void {
        // 显式关闭（保存成功、重新上传文档等）不触发草稿确认。
        this.closeAllowed = true;
        bootstrap.Modal.getInstance(document.getElementById("scenarioModal"))?.hide();
    }

    /** 表单当前内容的稳定快照，用于判断是否相对打开时有改动。 */
    private snapshotForm(): string {
        try {
            return JSON.stringify(this.getDraftData());
        } catch {
            return "";
        }
    }

    /**
     * 关闭编辑器时的草稿确认。
     *
     * ESC、点击遮罩、右上角关闭、底部「取消」都会走到这里：只有在「新建」且表单
     * 相对打开时确实发生改动时才询问，避免每次关闭都打扰用户。
     */
    private async confirmDraftOnClose(): Promise<void> {
        const action = await this.showCloseDraftPrompt();
        if (action === "cancel") return;
        if (action === "save") {
            await this.handlers?.onSaveDraft();
        } else if (action === "discard") {
            await this.handlers?.onDiscardDraft?.();
        }
        this.closeAllowed = true;
        this.closeModal();
    }

    /** 询问是否保留草稿；与打开时检测到草稿的提示保持一致的措辞。 */
    private showCloseDraftPrompt(): Promise<"save" | "discard" | "cancel"> {
        return new Promise((resolve) => {
            const modal = document.createElement("div");
            modal.className = "modal fade";
            modal.tabIndex = -1;
            modal.setAttribute("data-bs-backdrop", "static");
            modal.setAttribute("data-bs-keyboard", "false");
            modal.setAttribute("aria-hidden", "true");
            modal.innerHTML = `
                <div class="modal-dialog modal-dialog-centered">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">${scenarioT("scenario.draft.close.title", "保留剧本草稿？")}</h5>
                        </div>
                        <div class="modal-body">${scenarioT("scenario.draft.close.body", "检测到剧本有未保存的改动，是否保留草稿？")}</div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-primary" data-draft-close-action="save">${scenarioT("scenario.draft.close.save", "保留草稿")}</button>
                            <button type="button" class="btn btn-danger" data-draft-close-action="discard">${scenarioT("scenario.draft.close.discard", "不保留")}</button>
                            <button type="button" class="btn btn-secondary" data-draft-close-action="cancel">${scenarioT("scenario.draft.close.cancel", "取消")}</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(modal);
            const instance = new bootstrap.Modal(modal, { backdrop: "static" });
            let settled = false;
            const finish = (action: "save" | "discard" | "cancel") => {
                if (settled) return;
                settled = true;
                instance.hide();
                resolve(action);
            };
            modal.querySelectorAll<HTMLElement>("[data-draft-close-action]").forEach((button) => {
                button.addEventListener("click", () => finish((button.dataset.draftCloseAction || "cancel") as "save" | "discard" | "cancel"));
            });
            modal.addEventListener("hidden.bs.modal", () => {
                modal.remove();
                if (!settled) resolve("cancel");
            });
            instance.show();
        });
    }

    previewScenario(scenario: Scenario, knowledge?: { vector_count?: number; path?: string; backend?: string }): void {
        const modules = normalizeScenarioModules(scenario);
        const previewContent = window.TrpgTemplates.render("scenario-preview-content", {
            title: scenario.title,
            publicId: scenario.public_id || String(scenario.id),
            scenarioVersion: scenario.scenario_version || "1.0.0",
            author: scenario.author,
            playerCount: scenario.playerCount,
            vectorCount: knowledge?.vector_count ?? 0,
            vectorBackend: knowledge?.backend || "unknown",
            notes: scenario.notes || scenarioT("scenario.preview.none", "无"),
            modulesHtml: renderPreviewModules(modules),
        });

        const modal = document.createElement("div");
        modal.className = "modal fade";
        modal.id = "previewModal";
        modal.tabIndex = -1;
        modal.innerHTML = window.TrpgTemplates.render("scenario-preview-modal", { previewContent });

        document.body.appendChild(modal);
        window.TrpgI18n?.apply(modal);
        new bootstrap.Modal(modal).show();
        modal.addEventListener("hidden.bs.modal", () => modal.remove());
    }

    private async openKnowledgeEditor(scenarioId: number): Promise<void> {
        let sections: KnowledgeSectionInfo[];
        try {
            const response = await TrpgApi.requestWithResponse<ApiResponse<KnowledgeSectionInfo[]>>(`/api/scripts/${scenarioId}/knowledge`);
            if (!response.response.ok || !response.data.success || !Array.isArray(response.data.data)) {
                throw new Error(response.data.message || "加载知识块失败");
            }
            sections = response.data.data;
        } catch (error) {
            this.showMessage(scenarioViewErrorMessage(error), true);
            return;
        }

        const modal = document.createElement("div");
        modal.className = "modal fade";
        modal.id = "scenarioKnowledgeModal";
        modal.tabIndex = -1;
        modal.innerHTML = `
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">知识块（世界书）</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <p class="text-muted small">触发词命中即直接激活该章节（不再依赖语义相似度）；常驻条目每轮注入；archived 章节不参与检索。</p>
                        <div class="scenario-knowledge-list">
                            ${sections.length ? sections.map(renderKnowledgeSection).join("") : '<p class="text-muted">该剧本暂无知识块，请先发布或导入剧本。</p>'}
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
                    </div>
                </div>
            </div>`;

        document.body.appendChild(modal);
        window.TrpgI18n?.apply(modal);
        const instance = new bootstrap.Modal(modal);
        modal.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-save-knowledge]");
            if (button) void this.saveKnowledgeSection(scenarioId, button);
        });
        modal.addEventListener("hidden.bs.modal", () => modal.remove());
        instance.show();
    }

    private async saveKnowledgeSection(scenarioId: number, button: HTMLButtonElement): Promise<void> {
        const item = button.closest<HTMLElement>("[data-section-key]");
        if (!item) return;
        const sectionKey = item.dataset.sectionKey || "";
        const fields: Record<string, unknown> = {};
        item.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("[data-field]").forEach((field) => {
            const name = field.dataset.field;
            if (!name) return;
            if (field instanceof HTMLInputElement && field.type === "checkbox") fields[name] = field.checked;
            else fields[name] = field.value;
        });
        button.disabled = true;
        try {
            const response = await TrpgApi.requestWithResponse<ApiResponse<KnowledgeSectionInfo[]>>(`/api/scripts/${scenarioId}/knowledge`, {
                method: "PUT",
                body: { sectionKey, fields },
            });
            if (!response.response.ok || !response.data.success) throw new Error(response.data.message || "保存失败");
            this.showMessage("世界书字段已保存");
        } catch (error) {
            this.showMessage(scenarioViewErrorMessage(error), true);
        } finally {
            button.disabled = false;
        }
    }

    showImportChoice(): Promise<"edit" | "direct" | "cancel"> {
        return new Promise((resolve) => {
            const modal = document.createElement("div");
            modal.className = "modal fade";
            modal.innerHTML = `<div class="modal-dialog modal-dialog-centered"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">选择导入模式</h5></div><div class="modal-body">请选择处理方式：直接导入会按标题和结构分块，逐块抽取字段并向量化；审核模式会生成草稿供检查，发现问题可重新上传文档。</div><div class="modal-footer"><button class="btn btn-secondary" data-import-choice="cancel">取消</button><button class="btn btn-outline-primary" data-import-choice="edit">生成审核草稿</button><button class="btn btn-primary" data-import-choice="direct">直接导入并发布</button></div></div></div>`;
            document.body.appendChild(modal);
            const instance = new bootstrap.Modal(modal, { backdrop: "static" });
            let settled = false;
            const finish = (choice: "edit" | "direct" | "cancel") => { if (settled) return; settled = true; instance.hide(); resolve(choice); };
            modal.querySelectorAll<HTMLElement>("[data-import-choice]").forEach((button) => button.addEventListener("click", () => finish((button.dataset.importChoice || "cancel") as "edit" | "direct" | "cancel")));
            modal.addEventListener("hidden.bs.modal", () => { modal.remove(); if (!settled) resolve("cancel"); });
            instance.show();
        });
    }

    /** 直接导入前收集剧本基础信息；取消时返回 null。 */
    showDirectImportMetadata(defaults: { title: string; creator: string }): Promise<{ title: string; author: string; creator: string; playerCount: number; description: string } | null> {
        return new Promise((resolve) => {
            const modal = document.createElement("div");
            modal.className = "modal fade";
            modal.innerHTML = `<div class="modal-dialog modal-dialog-centered"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">${scenarioT("scenario.import.meta.title", "填写剧本信息")}</h5></div><div class="modal-body">
                <div class="mb-3"><label class="form-label">${scenarioT("scenario.import.meta.name", "剧本名")}</label><input class="form-control" data-import-field="title" value="${scenarioEscapeHtml(defaults.title)}"></div>
                <div class="mb-3"><label class="form-label">${scenarioT("scenario.import.meta.author", "作者")}</label><input class="form-control" data-import-field="author" value=""></div>
                <div class="mb-3"><label class="form-label">${scenarioT("scenario.import.meta.creator", "创建者")}</label><input class="form-control" data-import-field="creator" value="${scenarioEscapeHtml(defaults.creator)}"></div>
                <div class="mb-3"><label class="form-label">${scenarioT("scenario.import.meta.players", "推荐人数")}</label><input type="number" min="1" class="form-control" data-import-field="playerCount" value="1"></div>
                <div class="mb-3"><label class="form-label">${scenarioT("scenario.import.meta.description", "简介")}</label><textarea class="form-control" rows="3" data-import-field="description"></textarea></div>
                <div class="text-danger" data-import-error hidden></div></div>
                <div class="modal-footer"><button class="btn btn-secondary" data-import-meta-action="cancel">${scenarioT("scenario.import.meta.cancel", "取消")}</button><button class="btn btn-primary" data-import-meta-action="confirm">${scenarioT("scenario.import.meta.confirm", "开始导入")}</button></div></div></div>`;
            document.body.appendChild(modal);
            const instance = new bootstrap.Modal(modal, { backdrop: "static" });
            let settled = false;
            const errorEl = modal.querySelector<HTMLElement>("[data-import-error]");
            const fieldValue = (name: string) => (modal.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[data-import-field="${name}"]`)?.value || "").trim();
            const finish = (value: { title: string; author: string; creator: string; playerCount: number; description: string } | null) => { if (settled) return; settled = true; instance.hide(); resolve(value); };
            modal.querySelector<HTMLElement>('[data-import-meta-action="confirm"]')?.addEventListener("click", () => {
                const title = fieldValue("title");
                const playerCount = Number.parseInt(fieldValue("playerCount") || "1", 10);
                if (!title || !Number.isFinite(playerCount) || playerCount < 1) {
                    if (errorEl) { errorEl.hidden = false; errorEl.textContent = scenarioT("scenario.import.meta.required", "请填写剧本名与有效的推荐人数"); }
                    return;
                }
                finish({ title, author: fieldValue("author"), creator: fieldValue("creator"), playerCount, description: fieldValue("description") });
            });
            modal.querySelector<HTMLElement>('[data-import-meta-action="cancel"]')?.addEventListener("click", () => finish(null));
            modal.addEventListener("hidden.bs.modal", () => { modal.remove(); if (!settled) resolve(null); });
            instance.show();
        });
    }

    getFormData(): ScenarioInput {
        const title = input("scenarioTitle").value.trim();
        const author = input("scenarioAuthor").value.trim();
        const playerCount = Number.parseInt(input("scenarioPlayerCount").value, 10);
        if (!title || !author || !Number.isFinite(playerCount)) {
            throw new Error(scenarioT("scenario.form.required", "请填写所有必填项"));
        }
        const scenarioVersion = input("scenarioVersion").value.trim();
        if (scenarioVersion && !/^\d+\.\d+\.\d+$/.test(scenarioVersion)) throw new Error("剧本版本必须使用 n.n.n 格式，例如 1.0.0");

        const modules = collectScenarioModules();
        // 文档直接导入的剧本本身没有场景卡，允许只修改基础信息；
        // 但只要新增了模块，就仍然要求场景与结局模块齐全。
        if (!this.directImportEditing || modules.length > 0) {
            const hasScene = modules.some((module) => module.module_type === "scene");
            const hasEnding = modules.some((module) => module.module_type === "ending");
            if (!hasScene) throw new Error(scenarioT("scenario.form.need_scene", "剧本至少需要一个场景模块"));
            if (!hasEnding) throw new Error(scenarioT("scenario.form.need_ending", "剧本至少需要一个结局模块"));
        }

        return {
            title,
            author,
            playerCount,
            scenario_version: scenarioVersion,
            notes: textarea("scenarioNotes").value.trim(),
            allow_open_ending: checkbox("scenarioAllowOpenEnding").checked,
            modules,
            cover: input("scenarioCoverUrl").value,
        };
    }

    getDraftData(): ScenarioInput {
        return { title: input("scenarioTitle").value.trim(), author: input("scenarioAuthor").value.trim(), playerCount: Number.parseInt(input("scenarioPlayerCount").value, 10) || 0, notes: textarea("scenarioNotes").value.trim(), allow_open_ending: checkbox("scenarioAllowOpenEnding").checked, modules: collectScenarioModules(), cover: input("scenarioCoverUrl").value, scenario_version: input("scenarioVersion").value.trim() };
    }

    showMessage(message: string, isError = false): void {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = window.TrpgTemplates.render("notification-message", {
            variant: isError ? "notification-error" : "notification-success",
            message,
        });
        const notification = wrapper.firstElementChild;
        if (!(notification instanceof HTMLElement)) return;

        const container = document.querySelector(".notification-container");
        if (!container) return;
        container.appendChild(notification);
        setTimeout(() => notification.remove(), 3000);
    }

    private initEventListeners(): void {
        document.getElementById("createScenario")?.addEventListener("click", () => {
            if (!this.requireLogin("创建剧本")) return;
            this.handlers?.onCreateScenarioClick();
        });

        document.getElementById("importScenarioDocument")?.addEventListener("click", () => {
            if (!this.requireLogin("导入剧本文档")) return;
            void this.handlers?.onImportScenarioDocumentClick();
        });

        input("importScenarioDocumentFile").addEventListener("change", async (event) => {
            const target = event.target as HTMLInputElement;
            const file = target.files?.[0];
            target.value = "";
            if (file) await this.handlers?.onImportScenarioDocument?.(file);
        });

        document.getElementById("importScenario")?.addEventListener("click", () => {
            if (!this.requireLogin("导入剧本")) return;
            input("importScenarioFile").click();
        });

        input("importScenarioFile").addEventListener("change", async (event) => {
            const target = event.target as HTMLInputElement;
            await this.handlers?.onImportScenario(target.files);
            target.value = "";
        });

        document.getElementById("saveScenario")?.addEventListener("click", this.saveScenarioHandler);
        document.getElementById("replaceScenarioDocument")?.addEventListener("click", () => {
            this.closeModal();
            window.setTimeout(() => input("importScenarioDocumentFile").click(), 150);
        });
        document.getElementById("saveScenarioDraft")?.addEventListener("click", async () => { await this.handlers?.onSaveDraft(); });
        document.getElementById("scenarioModal")?.addEventListener("keydown", (event) => { if ((event as KeyboardEvent).ctrlKey && (event as KeyboardEvent).key.toLowerCase() === "s") { event.preventDefault(); void this.handlers?.onSaveDraft(); } });
        document.getElementById("addModule")?.addEventListener("click", () => this.addModule());
        document.getElementById("scenarioModal")?.addEventListener("click", (event) => this.handleScenarioEditorClick(event));
        document.getElementById("scenarioModal")?.addEventListener("change", (event) => this.handleScenarioEditorChange(event));
        const resourceDropzone = document.getElementById("scenarioResourceDropzone");
        const resourceFiles = document.getElementById("scenarioResourceFiles") as HTMLInputElement | null;
        resourceDropzone?.addEventListener("click", () => resourceFiles?.click());
        resourceDropzone?.addEventListener("dragover", (event) => { event.preventDefault(); resourceDropzone.classList.add("is-dragging"); });
        resourceDropzone?.addEventListener("dragleave", () => resourceDropzone.classList.remove("is-dragging"));
        resourceDropzone?.addEventListener("drop", (event) => { event.preventDefault(); resourceDropzone.classList.remove("is-dragging"); const files = (event as DragEvent).dataTransfer?.files; if (files) void this.uploadResourceFiles(files); });
        resourceFiles?.addEventListener("change", () => { if (resourceFiles.files) void this.uploadResourceFiles(resourceFiles.files); resourceFiles.value = ""; });
        const modal = document.getElementById("scenarioModal");
        modal?.addEventListener("shown.bs.modal", () => {
            // 动画结束后再记录基线：此时填充草稿 / 导入审核数据都已写入表单，
            // 这些内容不算「用户改动」。
            this.closeAllowed = false;
            this.formBaseline = this.snapshotForm();
            if (!this.isCreating) return;
            if (this.draftTimer !== null) window.clearInterval(this.draftTimer);
            const seconds = Math.max(15, Number(window.configManager?.get<number>("general", "scenario", "draft_autosave_interval", window.configManager?.get<number>("general", "autosave", "interval", 300)) || 300));
            this.draftTimer = window.setInterval(() => { void this.handlers?.onSaveDraft(); }, seconds * 1000);
        });
        // ESC / 点击遮罩 / 关闭按钮都会触发 hide 事件。若有未保存改动则拦下，
        // 先询问是否保留草稿，避免误触直接丢失编辑内容。
        modal?.addEventListener("hide.bs.modal", (event) => {
            if (this.closeAllowed || !this.isCreating) return;
            if (this.formBaseline === null || this.snapshotForm() === this.formBaseline) return;
            event.preventDefault();
            void this.confirmDraftOnClose();
        });
        modal?.addEventListener("hidden.bs.modal", () => {
            if (this.draftTimer !== null) window.clearInterval(this.draftTimer);
            this.draftTimer = null;
            this.formBaseline = null;
        });
    }

    showConversionProgress(fileName: string): void {
        this.closeConversionProgress();
        const modal = document.createElement("div");
        modal.className = "modal fade";
        modal.id = "scenarioConversionProgressModal";
        modal.tabIndex = -1;
        modal.setAttribute("data-bs-backdrop", "static");
        modal.setAttribute("data-bs-keyboard", "false");
        modal.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header"><h5 class="modal-title">正在转换剧本文档</h5></div>
                    <div class="modal-body">
                        <p class="text-muted small mb-3" data-conversion-file></p>
                        <div class="progress mb-3" role="progressbar" aria-label="剧本转换进度"><div class="progress-bar progress-bar-striped progress-bar-animated" data-conversion-progress style="width: 0%">0%</div></div>
                        <div class="list-group list-group-flush" data-conversion-stages>
                            ${["读取文档并提取文本", "分析文档结构与章节", "生成场景/结局模块", "校验并准备编辑器"].map((label, index) => `<div class="list-group-item px-0 d-flex align-items-center gap-2" data-conversion-stage="${index}"><span class="conversion-stage-icon">○</span><span>${label}</span><small class="ms-auto text-muted" data-conversion-stage-detail></small></div>`).join("")}
                        </div>
                    </div>
                </div>
            </div>`;
        modal.querySelector<HTMLElement>("[data-conversion-file]")!.textContent = fileName;
        document.body.appendChild(modal);
        this.conversionProgressModal = modal;
        new bootstrap.Modal(modal, { backdrop: "static" }).show();
    }

    updateConversionProgress(stage: number, state: "pending" | "active" | "complete" | "error", detail = ""): void {
        const modal = this.conversionProgressModal;
        if (!modal) return;
        const item = modal.querySelector<HTMLElement>(`[data-conversion-stage="${stage}"]`);
        if (item) {
            item.dataset.state = state;
            const icon = item.querySelector<HTMLElement>(".conversion-stage-icon");
            if (icon) icon.textContent = state === "complete" ? "✓" : state === "active" ? "●" : state === "error" ? "!" : "○";
            const detailNode = item.querySelector<HTMLElement>("[data-conversion-stage-detail]");
            if (detailNode) detailNode.textContent = detail;
        }
        const progress = modal.querySelector<HTMLElement>("[data-conversion-progress]");
        if (progress) {
            // Keep the current stage visibly alive while a long AI request is
            // running. Stage completion advances the bar; active stages use a
            // stable midpoint instead of appearing frozen at 37%.
            const value = state === "complete" ? Math.min(100, (stage + 1) * 25) : state === "active" ? Math.min(96, stage * 25 + 20) : stage * 25;
            progress.style.width = `${value}%`;
            progress.textContent = state === "active" ? `${value}% · 处理中` : `${value}%`;
            progress.classList.toggle("progress-bar-animated", state === "active");
            progress.classList.toggle("bg-danger", state === "error");
            if (this.conversionProgressTimer !== null) {
                window.clearInterval(this.conversionProgressTimer);
                this.conversionProgressTimer = null;
            }
            if (state === "active") {
                let animatedValue = Number.parseInt(progress.style.width, 10) || value;
                this.conversionProgressTimer = window.setInterval(() => {
                    if (!this.conversionProgressModal || !progress.isConnected) return;
                    animatedValue = Math.min(92, animatedValue + 1);
                    progress.style.width = `${animatedValue}%`;
                    progress.textContent = `${animatedValue}% · 处理中`;
                    if (animatedValue >= 92 && this.conversionProgressTimer !== null) {
                        window.clearInterval(this.conversionProgressTimer);
                        this.conversionProgressTimer = null;
                    }
                }, 900);
            }
        }
    }

    updateImportJobProgress(job: ScenarioImportJob): void {
        const stageIndex: Record<string, number> = { parsing: 0, chunking: 1, extracting: 2, carding: 2, summarizing: 2, embedding: 3, done: 3 };
        const index = stageIndex[job.current_stage] ?? 0;
        const meta = job.stage_meta || {};
        const total = Number(meta.totalChunks || 0);
        const processed = Number(meta.processedChunks || 0);
        this.updateConversionProgress(index, job.status === "failed" ? "error" : job.status === "done" ? "complete" : "active", total ? `${processed}/${total} blocks` : job.current_stage);
        const progress = this.conversionProgressModal?.querySelector<HTMLElement>("[data-conversion-progress]");
        if (progress) {
            const value = Math.max(0, Math.min(100, Number(job.progress || 0)));
            progress.style.width = `${value}%`;
            progress.textContent = `${Math.round(value)}%${total ? ` · ${processed}/${total}` : ""}`;
        }
    }

    closeConversionProgress(): void {
        if (!this.conversionProgressModal) return;
        if (this.conversionProgressTimer !== null) window.clearInterval(this.conversionProgressTimer);
        this.conversionProgressTimer = null;
        bootstrap.Modal.getInstance(this.conversionProgressModal)?.hide();
        this.conversionProgressModal.remove();
        this.conversionProgressModal = null;
    }

    private bindScenarioActions(): void {
        this.scenarioList.addEventListener("click", async (event) => {
            if (this.batchMode) {
                this.toggleScenarioSelection(event.target as HTMLElement);
                return;
            }
            const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button");
            if (!button) return;

            const id = Number.parseInt(button.getAttribute("data-id") || "", 10);
            if (!Number.isFinite(id)) return;

            if (button.classList.contains("preview-scenario")) {
                if (await confirmScenarioSpoilerAccess(id)) this.handlers?.onPreviewScenario(id);
            } else if (button.classList.contains("edit-scenario")) {
                if (await confirmScenarioSpoilerAccess(id)) this.handlers?.onEditScenario(id);
            } else if (button.classList.contains("knowledge-scenario")) {
                await this.openKnowledgeEditor(id);
            } else if (button.classList.contains("play-scenario")) {
                this.handlers?.onPlayScenario(id);
            } else if (button.classList.contains("delete-scenario")) {
                await this.handlers?.onDeleteScenario(id);
            }
        });
    }

    private toggleScenarioSelection(target: HTMLElement): void {
        const card = target.closest<HTMLElement>(".scenario-card");
        if (!card || card.dataset.batchSelectable !== "true") return;
        const id = Number.parseInt(card.dataset.scenarioId || "", 10);
        if (!Number.isFinite(id)) return;
        const nowSelected = !this.selectedScenarioIds.has(id);
        if (nowSelected) this.selectedScenarioIds.add(id); else this.selectedScenarioIds.delete(id);
        card.classList.toggle("batch-selected", nowSelected);
        const icon = card.querySelector<HTMLElement>(".batch-card-check i");
        if (icon) icon.className = `fa ${nowSelected ? "fa-check-square-o" : "fa-square-o"}`;
        this.updateBatchToolbar();
    }

    private renderScenarioActionButtons(scenario: Scenario): string {
        const id = scenarioEscapeHtml(scenario.id);
        const canPreview = canUseScenarioPermission("scenarios.preview");
        const canEdit = canUseScenarioPermission("scenarios.edit") && canModifyScenario(scenario);
        const canDelete = canUseScenarioPermission("scenarios.delete") && canModifyScenario(scenario);
        return [
            canPreview ? `<button class="btn btn-sm btn-primary preview-scenario" data-id="${id}">${scenarioT("scenario.list.preview", "预览")}</button>` : "",
            canEdit ? `<button class="btn btn-sm btn-secondary edit-scenario" data-id="${id}">${scenarioT("scenario.list.edit", "编辑")}</button>` : "",
            canEdit ? `<button class="btn btn-sm btn-outline-secondary knowledge-scenario" data-id="${id}">${scenarioT("scenario.list.knowledge", "世界书")}</button>` : "",
            canDelete ? `<button class="btn btn-sm btn-danger delete-scenario" data-id="${id}">${scenarioT("scenario.list.delete", "删除")}</button>` : "",
        ].join("");
    }

    private resetScenarioForm(): void {
        this.directImportEditing = false;
        requiredElement("scenarioModal").dataset.scenarioId = "";
        input("scenarioTitle").value = "";
        input("scenarioPublicId").value = scenarioT("scenario.cover.auto", "保存后自动生成");
        input("scenarioVersion").value = "1.0.0";
        input("scenarioAuthor").value = "";
        input("scenarioPlayerCount").value = "0";
        textarea("scenarioNotes").value = "";
        checkbox("scenarioAllowOpenEnding").checked = false;
        input("scenarioCoverUrl").value = "";
        image("coverPreview").src = TrpgAvatar.scenarioCoverTextDataUri(input("scenarioTitle").value);
        renderModuleList(requiredElement("scenarioModules"), [
            createModule("scene", 1),
            createModule("ending", 1),
        ]);
        (requiredElement("scenarioModuleType") as HTMLSelectElement).value = "scene";
        updateScenarioModalTitle("scenario.modal.create", "创建剧本");
    }

    private fillScenarioForm(scenario: Scenario): void {
        this.directImportEditing = scenario.import_mode === "direct";
        requiredElement("scenarioModal").dataset.scenarioId = String(scenario.id);
        document.getElementById("scenarioResourcePanel")?.removeAttribute("hidden");
        input("scenarioTitle").value = scenario.title;
        input("scenarioPublicId").value = scenario.public_id || String(scenario.id);
        input("scenarioVersion").value = scenario.scenario_version || "1.0.0";
        input("scenarioAuthor").value = scenario.author;
        input("scenarioPlayerCount").value = String(scenario.playerCount);
        textarea("scenarioNotes").value = scenario.notes || "";
        checkbox("scenarioAllowOpenEnding").checked = Boolean(scenario.allow_open_ending);
        input("scenarioCoverUrl").value = scenario.cover || "";
        image("coverPreview").src = safeScenarioCover(scenario.title, scenario.cover);

        renderModuleList(requiredElement("scenarioModules"), normalizeScenarioModules(scenario));
        void this.loadResourceList(scenario.id);
        (requiredElement("scenarioModuleType") as HTMLSelectElement).value = "scene";
        updateScenarioModalTitle("scenario.modal.edit", "编辑剧本");
    }

    private async uploadResourceFiles(files: FileList): Promise<void> {
        const scenarioId = Number.parseInt(document.getElementById("scenarioModal")?.dataset.scenarioId || "", 10);
        if (!Number.isFinite(scenarioId) || scenarioId <= 0) {
            this.showMessage("请先保存剧本，再上传资源", true);
            return;
        }
        const selected = Array.from(files);
        const alt: string[] = [];
        for (const file of selected) {
            const description = await window.TrpgDialog?.prompt({
                title: "资源描述",
                message: `请为 ${file.name} 填写资源描述（必填）`,
                defaultValue: file.name,
                confirmText: "上传",
            });
            if (description == null) return;
            if (!description.trim()) {
                this.showMessage("每个资源都必须填写描述", true);
                return;
            }
            alt.push(description);
        }
        try {
            const form = new FormData();
            selected.forEach((file) => form.append("files", file, file.name));
            alt.forEach((value) => form.append("alt", value));
            const response = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef[]>>(`/api/scripts/${scenarioId}/assets`, { method: "POST", body: form });
            if (!response.response.ok || !response.data.success) throw new Error(response.data.message || "资源上传失败");
            this.showMessage(`已上传 ${response.data.data?.length || selected.length} 个资源`);
            await this.loadResourceList(scenarioId);
        } catch (error) {
            this.showMessage(error instanceof Error ? error.message : "资源上传失败", true);
        }
    }

    private async loadResourceList(scenarioId: number): Promise<void> {
        const list = document.getElementById("scenarioResourceList");
        if (!list) return;
        const response = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef[]>>(`/api/scripts/${scenarioId}/assets`);
        if (!response.response.ok || !response.data.success || !Array.isArray(response.data.data)) return;
        list.innerHTML = response.data.data.map((resource) => `<div class="scenario-resource-row" data-resource-hash="${scenarioEscapeHtml(resource.hash)}"><span>${scenarioEscapeHtml(resource.alt)}</span><small>${scenarioEscapeHtml(resource.mime)} · ${resource.size} bytes</small></div>`).join("");
    }

    private addModule(): void {
        const type = (requiredElement("scenarioModuleType") as HTMLSelectElement).value as ScenarioModuleType;
        const modules = collectScenarioModules();
        const module = createModule(type, countModulesOfType(modules, type) + 1, modules);
        const list = requiredElement("scenarioModules");
        list.insertAdjacentHTML("beforeend", renderModuleEditor(module, modules.length + 1, countModulesOfType(modules, type) + 1));
        const card = list.lastElementChild;
        if (card instanceof HTMLElement) {
            refreshModuleCard(card);
            window.TrpgI18n?.apply(card);
            this.updateModuleNumbers();
        }
    }

    private handleScenarioEditorClick(event: Event): void {
        const target = event.target as HTMLElement;

        const triggerAdd = target.closest<HTMLElement>("[data-add-trigger]");
        if (triggerAdd) {
            const list = triggerAdd.closest<HTMLElement>(".scenario-module-item")?.querySelector<HTMLElement>("[data-trigger-list]");
            if (list) list.insertAdjacentHTML("beforeend", renderTriggerRow(list.querySelectorAll("[data-trigger-item]").length + 1));
            return;
        }

        const timelineAdd = target.closest<HTMLElement>("[data-add-timeline-entry]");
        if (timelineAdd) {
            const list = timelineAdd.closest<HTMLElement>(".scenario-module-item")?.querySelector<HTMLElement>("[data-timeline-entry-list]");
            if (list) list.insertAdjacentHTML("beforeend", renderTimelineEntryRow(list.querySelectorAll("[data-timeline-entry-item]").length + 1));
            return;
        }

        const customAdd = target.closest<HTMLElement>("[data-add-custom-input]");
        if (customAdd) {
            const list = customAdd.closest<HTMLElement>(".scenario-module-item")?.querySelector<HTMLElement>("[data-custom-input-list]");
            if (list) list.insertAdjacentHTML("beforeend", renderCustomInputRow(list.querySelectorAll("[data-custom-input-item]").length + 1));
            return;
        }

        const summaryButton = target.closest<HTMLElement>("[data-generate-module-summary]");
        if (summaryButton) {
            const card = summaryButton.closest<HTMLElement>(".scenario-module-item");
            if (card) void this.generateModuleSummary(card);
            return;
        }

        const skillAdd = target.closest<HTMLElement>("[data-add-skill]");
        if (skillAdd) {
            const list = skillAdd.closest<HTMLElement>(".scenario-module-item")?.querySelector<HTMLElement>("[data-skill-list]");
            if (list) list.insertAdjacentHTML("beforeend", renderSkillRow(list.querySelectorAll("[data-skill-item]").length + 1));
            return;
        }

        const weaponAdd = target.closest<HTMLElement>("[data-add-weapon]");
        if (weaponAdd) {
            const list = weaponAdd.closest<HTMLElement>(".scenario-module-item")?.querySelector<HTMLElement>("[data-weapon-list]");
            if (list) list.insertAdjacentHTML("beforeend", renderWeaponRow(list.querySelectorAll("[data-weapon-item]").length + 1));
            return;
        }

        const removeButton = target.closest<HTMLElement>("[data-remove-module],[data-remove-trigger],[data-remove-custom-input],[data-remove-skill],[data-remove-weapon],[data-remove-timeline-entry]");
        if (removeButton) {
            if (removeButton.hasAttribute("data-remove-module")) {
                removeButton.closest<HTMLElement>(".scenario-module-item")?.remove();
                this.updateModuleNumbers();
                return;
            }
            removeButton.closest<HTMLElement>("[data-trigger-item],[data-custom-input-item],[data-skill-item],[data-weapon-item],[data-timeline-entry-item]")?.remove();
            return;
        }

        const moveButton = target.closest<HTMLElement>("[data-move-module]");
        if (moveButton) {
            const card = moveButton.closest<HTMLElement>(".scenario-module-item");
            if (!card) return;
            const direction = moveButton.dataset.moveModule;
            if (direction === "up" && card.previousElementSibling) {
                card.parentElement?.insertBefore(card, card.previousElementSibling);
            } else if (direction === "down" && card.nextElementSibling) {
                card.parentElement?.insertBefore(card.nextElementSibling, card);
            }
            this.updateModuleNumbers();
        }
    }

    private handleScenarioEditorChange(event: Event): void {
        const target = event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null;
        if (!target) return;

        if (target.matches("[data-trigger-file]")) {
            void readTriggerFile(target as HTMLInputElement).catch((error) => this.showMessage(scenarioViewErrorMessage(error), true));
            return;
        }

        if (target.matches("[data-module-type]")) {
            const card = target.closest<HTMLElement>(".scenario-module-item");
            if (card) {
                syncModuleDefaultsForTypeChange(card);
                refreshModuleCard(card);
            }
        }
    }

    private updateModuleNumbers(): void {
        const counts = new Map<ScenarioModuleType, number>();
        document.querySelectorAll<HTMLElement>(".scenario-module-item").forEach((card) => {
            const node = card.querySelector<HTMLElement>("[data-module-number]");
            const type = (card.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene") as ScenarioModuleType;
            const ordinal = (counts.get(type) || 0) + 1; counts.set(type, ordinal);
            if (node) node.textContent = defaultModuleTitle(type, ordinal);
        });
    }

    private async generateModuleSummary(card: HTMLElement): Promise<void> {
        const moduleId = card.dataset.moduleId || "";
        const module = collectScenarioModules().find((item) => item.id === moduleId);
        if (!module) {
            this.showMessage(scenarioT("scenario.module.summary_failed", "无法读取模块内容"), true);
            return;
        }

        const scenarioTitle = input("scenarioTitle").value.trim();

        try {
            const response = await TrpgApi.post<ApiResponse<{ summary: string; token_count?: number }>>("/api/scenarios/module-summary", {
                scenario_title: scenarioTitle,
                module,
            });

            if (!response.success || !response.data?.summary) {
                throw new Error(response.error || response.message || scenarioT("scenario.module.summary_failed", "模块摘要生成失败"));
            }

            const summary = response.data.summary.trim();
            const summaryField = card.querySelector<HTMLInputElement>("[data-module-summary]");
            const contentField = card.querySelector<HTMLTextAreaElement>("[data-module-content]");
            const entitySummaryField = card.querySelector<HTMLTextAreaElement>("[data-entity-summary]");
            if (summaryField) summaryField.value = summary;
            if (entitySummaryField) entitySummaryField.value = summary;
            if (module.module_type === "monster" || module.module_type === "npc") {
                const current = contentField?.value.trim() || "";
                if (!current && contentField) contentField.value = summary;
            }
            refreshModuleCard(card);
            this.showMessage(
                response.data.token_count
                    ? `${scenarioT("scenario.module.summary_generated", "模块摘要已生成")} (${response.data.token_count} tokens)`
                    : scenarioT("scenario.module.summary_generated", "模块摘要已生成"),
            );
        } catch (error) {
            this.showMessage(scenarioViewErrorMessage(error), true);
        }
    }
}

window.ScenarioView = ScenarioView;
