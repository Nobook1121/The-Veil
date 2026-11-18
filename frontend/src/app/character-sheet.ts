// 角色卡页面外壳与统一角色卡列表。
//
// 角色卡是规则集私有结构，但列表是跨规则集共享的：本文件负责拉取 /api/characters，
// 按卡片的 rulesetId 解析到对应模块，由模块提供卡面摘要（describeCharacterCard），
// 核心只负责统一排版、规则集标签、批量管理与入口路由；详情 / 编辑 / 新建仍交给模块。
// 模块缺失时明确阻断并提示，绝不退化成只读伪界面。

interface CharacterSheetHostApi {
    init: () => void;
    /** 新建角色卡；传入规则集时直接使用该规则集，否则在多个可用规则集间选择。 */
    createCharacterForRuleset: (rulesetId?: string) => void;
}

interface CharacterListBridge {
    enabled: boolean;
    /** 由规则集模块在每次列表刷新时调用：先用缓存渲染，再异步拉取最新数据。 */
    render: () => void;
    refresh: () => Promise<void>;
}

interface Window {
    TrpgCharacterSheetHost?: CharacterSheetHostApi;
    TrpgCharacterListBridge?: CharacterListBridge;
}

(function installCharacterSheetHost(global: Window & typeof globalThis): void {
    "use strict";

    const CARD_DEFAULT_BIO = "这位角色还没有写下简介。";

    const BLOCKED_ACTION_IDS = [
        "createCharacter",
        "importCharacter",
        "exportCharacter",
        "openCharacterGallery",
        "characterBatchToggle",
    ];

    let roomEventsBound = false;
    let initialized = false;
    let activeModule: CharacterSheetModule | null = null;
    let activeModuleId = "";

    /** /api/characters 的原始数据；null 表示尚未加载。 */
    let listCards: Record<string, unknown>[] | null = null;
    let listRequest: Promise<void> | null = null;
    let refreshTimer: number | null = null;
    let batchMode = false;
    const batchSelection = new Set<string>();

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function i18nText(key: string, fallback: string, values?: Record<string, string | number>): string {
        return global.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function pluginText(value: unknown): string {
        const text = String(value ?? "").trim();
        if (!text) return "";
        return global.TrpgI18n?.t("", text) || text;
    }

    function notify(message: string, type: string = "info"): void {
        const notifier = global.showNotification;
        if (typeof notifier === "function") notifier(message, type);
    }

    function cardIdOf(raw: Record<string, unknown>): string {
        return String(raw?.id ?? "").trim();
    }

    function rulesetIdOf(raw: Record<string, unknown>): string {
        const explicit = String(raw?.rulesetId ?? "").trim().toLowerCase();
        if (explicit) return explicit;
        // 历史卡没有 rulesetId：交给默认模块解释，避免核心硬编码某个规则集 id。
        return String(global.TrpgCharacterSheetRegistry?.defaultModule()?.id ?? "").trim().toLowerCase();
    }

    function moduleForCard(raw: Record<string, unknown>): CharacterSheetModule | undefined {
        return global.TrpgCharacterSheetRegistry?.forRuleset(rulesetIdOf(raw));
    }

    function describeCard(raw: Record<string, unknown>): CharacterSheetCardSummary & { rulesetId: string } {
        const rulesetId = rulesetIdOf(raw);
        let summary: CharacterSheetCardSummary = {};
        try {
            summary = moduleForCard(raw)?.describeCharacterCard?.(raw) || {};
        } catch {
            // 单个模块的摘要异常不应拖垮整个列表。
        }
        return {
            rulesetId,
            tag: String(summary.tag || rulesetId || "?").trim().toUpperCase(),
            name: pluginText(summary.name || raw?.name || "未命名角色卡"),
            avatar: String(summary.avatar ?? raw?.avatar ?? ""),
            subtitle: pluginText(summary.subtitle || ""),
            bio: pluginText(summary.bio || "") || i18nText("character.card.default_bio", CARD_DEFAULT_BIO),
        };
    }

    function pruneBatchSelection(): void {
        if (!listCards) return;
        const ids = new Set(listCards.map(cardIdOf));
        [...batchSelection].forEach((id) => {
            if (!ids.has(id)) batchSelection.delete(id);
        });
    }

    function renderEmptyState(message: string, withCreate: boolean): string {
        return `
            <div class="character-empty-state">
                <i class="fa fa-id-badge" aria-hidden="true"></i>
                <p>${escapeHtml(message)}</p>
                ${withCreate ? `<button type="button" class="btn btn-sm btn-primary" data-card-action="create">新建角色卡</button>` : ""}
            </div>`;
    }

    function renderCard(raw: Record<string, unknown>): string {
        const id = cardIdOf(raw);
        const summary = describeCard(raw);
        const avatar = TrpgAvatar.resolveAvatarSrc(summary.name || "?", summary.avatar || "");
        const selected = batchSelection.has(id);
        const classes = [
            "character-card",
            batchMode ? "batch-mode" : "",
            selected ? "batch-selected" : "",
        ].filter(Boolean).join(" ");
        return `
            <article class="${classes}" data-character-card-id="${escapeHtml(id)}" data-character-ruleset="${escapeHtml(summary.rulesetId)}">
                ${batchMode ? `<span class="batch-card-check" aria-hidden="true"><i class="fa ${selected ? "fa-check-square-o" : "fa-square-o"}"></i></span>` : ""}
                <div class="character-card-avatar"><img src="${escapeHtml(avatar)}" alt=""></div>
                <h5>${escapeHtml(summary.name || "未命名角色卡")}<span class="character-card-tag" data-ruleset="${escapeHtml(summary.rulesetId)}">${escapeHtml(summary.tag || "?")}</span></h5>
                ${summary.subtitle ? `<p class="character-card-subtitle">${escapeHtml(summary.subtitle)}</p>` : ""}
                <p class="character-card-bio">${escapeHtml(summary.bio || "")}</p>
                ${batchMode ? "" : `
                <div class="character-card-actions">
                    <button type="button" data-card-action="detail">详情</button>
                    <button type="button" data-card-action="edit">编辑</button>
                    <button type="button" data-card-action="delete">删除</button>
                </div>`}
            </article>
        `;
    }

    function updateBatchToolbar(): void {
        const toolbar = document.getElementById("characterBatchToolbar");
        const count = document.getElementById("characterBatchCount");
        const selectAll = document.getElementById("characterBatchSelectAll") as HTMLInputElement | null;
        const deleteBtn = document.getElementById("characterBatchDelete") as HTMLButtonElement | null;
        const toggle = document.getElementById("characterBatchToggle");
        toggle?.classList.toggle("active", batchMode);
        if (toolbar) toolbar.hidden = !batchMode;
        const selectedCount = batchSelection.size;
        if (count) {
            count.textContent = selectedCount > 0
                ? i18nText("common.batch.selected", `已选 ${selectedCount} 项`, { count: String(selectedCount) })
                : "";
        }
        if (selectAll) {
            const selectable = listCards ? listCards.length : 0;
            selectAll.checked = selectable > 0 && selectedCount === selectable;
            selectAll.indeterminate = selectedCount > 0 && selectedCount < selectable;
            selectAll.disabled = selectable === 0;
        }
        if (deleteBtn) deleteBtn.disabled = selectedCount === 0;
    }

    function renderUnifiedList(): void {
        const list = document.getElementById("characterList");
        if (!list) return;
        if (batchMode) pruneBatchSelection();
        if (!global.currentUser) {
            list.innerHTML = renderEmptyState(i18nText("character.card.login_required", "登录后查看角色卡。"), false);
            updateBatchToolbar();
            return;
        }
        if (listCards === null) {
            list.innerHTML = renderEmptyState(i18nText("character.card.loading", "正在加载角色卡…"), false);
            void refreshCards();
            updateBatchToolbar();
            return;
        }
        if (!listCards.length) {
            list.innerHTML = renderEmptyState(i18nText("character.card.empty", "还没有角色卡。"), true);
            updateBatchToolbar();
            return;
        }
        list.innerHTML = listCards.map(renderCard).join("");
        updateBatchToolbar();
    }

    async function refreshCards(): Promise<void> {
        if (!global.currentUser) {
            listCards = [];
            renderUnifiedList();
            return;
        }
        if (listRequest) return listRequest;
        const request = (async () => {
            try {
                const response = await TrpgApi.get<ApiResponse<Record<string, unknown>[]>>("/api/characters");
                listCards = response?.success && Array.isArray(response.data) ? response.data : [];
            } catch {
                listCards = [];
            }
            listRequest = null;
            renderUnifiedList();
        })();
        listRequest = request;
        await request;
    }

    function scheduleRefresh(): void {
        if (refreshTimer !== null) return;
        refreshTimer = global.setTimeout(() => {
            refreshTimer = null;
            void refreshCards();
        }, 150);
    }

    function block(reason: string): void {
        if (typeof document === "undefined") return;
        const workspace = document.getElementById("characterWorkspace");
        if (workspace) {
            workspace.innerHTML = `
                <div class="character-matrix-bg" aria-hidden="true"></div>
                <div role="alert" style="position:relative;z-index:1;max-width:680px;margin:48px auto;padding:24px 28px;border:1px solid rgba(255,110,110,.45);border-radius:12px;background:rgba(40,10,12,.72);color:#ffe3e3;">
                    <h3 style="margin:0 0 12px;">角色卡不可用</h3>
                    <p style="margin:0 0 8px;">${escapeHtml(reason)}</p>
                    <p style="margin:0;opacity:.82;">已禁用创建、导入、编辑与导出入口，避免在缺少规则集实现时写入不完整或错误的数据。</p>
                </div>`;
        }
        BLOCKED_ACTION_IDS.forEach((id) => {
            const element = document.getElementById(id) as HTMLButtonElement | null;
            if (element) element.disabled = true;
        });
        notify(reason, "error");
    }

    /**
     * 切换规则集模块前重建工作区：克隆节点可以一次性丢掉旧模块绑在按钮上的
     * 直接监听，避免 CoC 与 5e 的创建/编辑入口互相触发。克隆保留内部标记结构，
     * 但清掉 ``dataset.initialized``，让新模块按自己的流程重新初始化。
     */
    function resetWorkspaceBindings(): void {
        const workspace = document.getElementById("characterWorkspace");
        if (!workspace) return;
        const replacement = workspace.cloneNode(false) as HTMLElement;
        replacement.innerHTML = workspace.innerHTML;
        delete replacement.dataset.initialized;
        workspace.replaceWith(replacement);
    }

    function activateModule(module: CharacterSheetModule): void {
        const moduleId = String(module.id || "").trim().toLowerCase();
        if (activeModuleId === moduleId && activeModule) {
            void module.reloadCharacterManagement?.();
            return;
        }
        activeModule?.clearCharacterManagement?.();
        resetWorkspaceBindings();
        activeModule = module;
        activeModuleId = moduleId;
        global.clearCharacterManagement = () => module.clearCharacterManagement?.();
        global.reloadCharacterManagement = async () => {
            await module.reloadCharacterManagement?.();
        };
        try {
            global.localStorage?.setItem("trpg:active-character-ruleset", moduleId);
        } catch {
            // 隐私模式下不可写；不影响本次切换。
        }
        module.initCharacterSheet();
    }

    function storedRulesetId(): string {
        try {
            return String(global.localStorage?.getItem("trpg:active-character-ruleset") || "").trim().toLowerCase();
        } catch {
            return "";
        }
    }

    function resolveModule(): CharacterSheetModule | undefined {
        const registry = global.TrpgCharacterSheetRegistry;
        // 房间内跟随房间规则集；不在房间时沿用上次使用的模块，最后回退默认（CoC）。
        const roomRulesetId = String(global.currentRoom?.ruleset_id || "").trim().toLowerCase();
        const rulesetId = roomRulesetId || storedRulesetId();
        return (rulesetId ? registry?.forRuleset(rulesetId) : undefined) || registry?.defaultModule();
    }

    function syncModule(): void {
        const module = resolveModule();
        if (!module) {
            global.clearCharacterManagement = () => {};
            global.reloadCharacterManagement = async () => {};
            block(
                global.currentRoom?.ruleset_id
                    ? `没有可用的角色卡渲染器：当前房间规则集 ${String(global.currentRoom.ruleset_id)} 尚未加载对应模块。`
                    : "没有可用的角色卡渲染器：规则集模块未加载，无法打开或编辑角色卡。"
            );
            return;
        }
        activateModule(module);
    }

    async function openCard(raw: Record<string, unknown>, mode: "detail" | "edit"): Promise<void> {
        const module = moduleForCard(raw);
        if (!module) {
            notify(`没有可用的角色卡渲染器：规则集「${rulesetIdOf(raw)}」尚未加载对应模块。`, "error");
            return;
        }
        if (String(module.id || "").trim().toLowerCase() !== activeModuleId) {
            activateModule(module);
        }
        try {
            await module.openCharacter?.(raw, mode);
        } catch (error) {
            notify(`打开角色卡失败：${String((error as Error)?.message || error)}`, "error");
        }
    }

    async function deleteCard(raw: Record<string, unknown>): Promise<void> {
        const id = cardIdOf(raw);
        if (!id) return;
        const summary = describeCard(raw);
        const confirmed = global.TrpgDialog?.confirm
            ? await global.TrpgDialog.confirm({ title: "删除角色卡", message: `确定删除「${summary.name}」吗？`, confirmText: "删除", danger: true })
            : global.confirm(`确定删除「${summary.name}」吗？`);
        if (!confirmed) return;
        try {
            const response = await TrpgApi.del<ApiResponse>(`/api/characters/${encodeURIComponent(id)}`);
            if (!response?.success) {
                notify(response?.message || response?.error || "删除角色卡失败", "error");
                return;
            }
            batchSelection.delete(id);
            if (listCards) listCards = listCards.filter((card) => cardIdOf(card) !== id);
            renderUnifiedList();
            notify("角色卡已删除", "success");
            void activeModule?.reloadCharacterManagement?.();
        } catch (error) {
            notify(`删除角色卡失败：${String((error as Error)?.message || error)}`, "error");
        }
    }

    async function deleteSelectedCards(): Promise<void> {
        const ids = [...batchSelection];
        if (!ids.length) return;
        const confirmed = global.TrpgDialog?.confirm
            ? await global.TrpgDialog.confirm({
                title: "批量删除",
                message: `确定删除选中的 ${ids.length} 张角色卡吗？`,
                confirmText: "删除",
                danger: true,
            })
            : global.confirm(`确定删除选中的 ${ids.length} 张角色卡吗？`);
        if (!confirmed) return;
        let deleted = 0;
        let failed = 0;
        for (const id of ids) {
            try {
                const response = await TrpgApi.del<ApiResponse>(`/api/characters/${encodeURIComponent(id)}`);
                if (response?.success) {
                    deleted += 1;
                    batchSelection.delete(id);
                    if (listCards) listCards = listCards.filter((card) => cardIdOf(card) !== id);
                } else {
                    failed += 1;
                }
            } catch {
                failed += 1;
            }
        }
        batchMode = false;
        renderUnifiedList();
        if (failed === 0) notify(`已删除 ${deleted} 项`, "success");
        else notify(`已删除 ${deleted} 项，${failed} 项失败`, "error");
        void activeModule?.reloadCharacterManagement?.();
    }

    function creatableModules(): CharacterSheetModule[] {
        const registry = global.TrpgCharacterSheetRegistry;
        if (!registry) return [];
        return registry.ids()
            .map((id) => registry.get(id))
            .filter((module): module is CharacterSheetModule => Boolean(module?.createCharacter));
    }

    function startCharacterCreation(module: CharacterSheetModule): void {
        if (String(module.id || "").trim().toLowerCase() !== activeModuleId) activateModule(module);
        if (!module.createCharacter) {
            notify("当前规则集暂不支持新建角色卡", "error");
            return;
        }
        module.createCharacter();
    }

    /** 同时启用多个规则集时，先让用户选择要创建的规则集。 */
    function openRulesetPicker(modules: CharacterSheetModule[]): void {
        document.getElementById("characterRulesetPickerModal")?.remove();
        const first = modules[0];
        if (!first) return;
        if (typeof bootstrap === "undefined") {
            startCharacterCreation(first);
            return;
        }
        const element = document.createElement("div");
        element.id = "characterRulesetPickerModal";
        element.className = "modal fade";
        element.tabIndex = -1;
        element.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">新建角色卡</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <p class="mb-2">请选择要创建的规则集：</p>
                        <div class="d-grid gap-2">
                            ${modules.map((module) => `
                                <button type="button" class="btn btn-outline-primary" data-character-ruleset-choice="${escapeHtml(module.id)}">
                                    ${escapeHtml(module.label || module.id)}
                                </button>`).join("")}
                        </div>
                    </div>
                </div>
            </div>`;
        const instance = new bootstrap.Modal(element);
        element.addEventListener("hidden.bs.modal", () => element.remove());
        element.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-character-ruleset-choice]");
            if (!button) return;
            const module = global.TrpgCharacterSheetRegistry?.get(button.dataset.characterRulesetChoice || "");
            instance.hide();
            if (module) startCharacterCreation(module);
        });
        document.body.appendChild(element);
        instance.show();
    }

    function createCharacterForRuleset(rulesetId?: string): void {
        if (!global.currentUser) {
            notify("请先登录后再创建角色卡", "warning");
            return;
        }
        const target = String(rulesetId || "").trim().toLowerCase();
        if (!target) {
            createCharacter();
            return;
        }
        const module = global.TrpgCharacterSheetRegistry?.forRuleset(target);
        if (!module?.createCharacter) {
            notify(`规则集「${target}」暂不支持新建角色卡`, "error");
            return;
        }
        startCharacterCreation(module);
    }

    function createCharacter(): void {
        if (!global.currentUser) {
            notify("请先登录后再创建角色卡", "warning");
            return;
        }
        const modules = creatableModules();
        if (modules.length === 0) {
            notify("没有可用的角色卡渲染器，无法创建角色卡", "error");
            return;
        }
        if (modules.length === 1) {
            startCharacterCreation(modules[0] as CharacterSheetModule);
            return;
        }
        openRulesetPicker(modules);
    }

    function toggleBatchMode(next: boolean): void {
        batchMode = next;
        batchSelection.clear();
        renderUnifiedList();
    }

    function handleListClick(event: MouseEvent): void {
        const target = event.target as HTMLElement | null;
        if (!target) return;
        const cardElement = target.closest<HTMLElement>("[data-character-card-id]");
        if (!cardElement) return;
        const id = cardElement.dataset.characterCardId || "";
        const raw = listCards?.find((card) => cardIdOf(card) === id);
        if (!raw) return;
        if (batchMode) {
            if (batchSelection.has(id)) batchSelection.delete(id);
            else batchSelection.add(id);
            renderUnifiedList();
            return;
        }
        const action = target.closest<HTMLElement>("[data-card-action]")?.dataset.cardAction || "detail";
        if (action === "delete") {
            void deleteCard(raw);
            return;
        }
        void openCard(raw, action === "edit" ? "edit" : "detail");
    }

    function bindGlobalEvents(): void {
        // 统一列表由外壳渲染，模块切换会克隆工作区节点，因此监听挂在 document 上。
        document.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            if (!target) return;
            if (target.closest("#createCharacter")) {
                event.preventDefault();
                event.stopPropagation();
                createCharacter();
                return;
            }
            if (target.closest("#characterBatchToggle")) {
                event.stopPropagation();
                toggleBatchMode(!batchMode);
                return;
            }
            if (target.closest("#characterBatchExit")) {
                event.stopPropagation();
                toggleBatchMode(false);
                return;
            }
            if (target.closest("#characterBatchDelete")) {
                event.stopPropagation();
                void deleteSelectedCards();
                return;
            }
            if (target.closest('[data-card-action="create"]')) {
                event.stopPropagation();
                createCharacter();
                return;
            }
            handleListClick(event);
        }, true);
        document.addEventListener("change", (event) => {
            const target = event.target as HTMLElement | null;
            if (!target || target.id !== "characterBatchSelectAll") return;
            const selectAll = target as HTMLInputElement;
            batchSelection.clear();
            if (selectAll.checked && listCards) {
                listCards.forEach((card) => batchSelection.add(cardIdOf(card)));
            }
            renderUnifiedList();
        });
        global.addEventListener("trpg:user-changed", () => {
            listCards = null;
            batchSelection.clear();
            batchMode = false;
            void refreshCards();
        });
        global.addEventListener("trpg:locale-changed", () => renderUnifiedList());
    }

    function init(): void {
        if (initialized) return;
        initialized = true;
        bindGlobalEvents();
        if (!roomEventsBound) {
            roomEventsBound = true;
            global.addEventListener("trpg:room-changed", () => {
                syncModule();
                void refreshCards();
            });
        }
        global.TrpgCharacterListBridge = {
            enabled: true,
            render: () => {
                renderUnifiedList();
                scheduleRefresh();
            },
            refresh: () => refreshCards(),
        };
        syncModule();
        void refreshCards();
    }

    global.TrpgCharacterSheetHost = { init, createCharacterForRuleset };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
