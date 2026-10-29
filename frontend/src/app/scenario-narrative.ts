/**
 * 剧本世界树 / 图谱编辑器入口。
 *
 * 具体能力按职责拆分到 `scenario-narrative/` 下的模块：共享状态与工具（shared）、
 * 布局（layout）、世界树（tree）、详情面板（inspector）、冲突处理（conflict）、
 * 撤销重做（history）、图谱绘制（graph-view）、图谱交互（graph-edit）、玩家预览
 * （preview）、开场配置（opening）、校验（validation）与生成（generate）。
 * 本文件只负责加载剧本、清空面板、绑定事件并向外部暴露入口。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, runtime, byId, t, escapeHtml, setStatus, setEmpty, updateStatusBadge, emitSelection, markDirty, showTab } = N;

    function renderAll(): void {
        api.renderTree();
        api.renderInspector();
        api.renderGraph();
        api.renderGraphEditor();
    }

    async function load(scenarioId: number | string, options: { readonly?: boolean } = {}): Promise<void> {
        const parsed = typeof scenarioId === "number" ? scenarioId : Number.parseInt(scenarioId, 10);
        state.scenarioId = Number.isFinite(parsed) ? parsed : 0;
        state.bundle = null;
        state.revision = 0;
        state.readonly = options.readonly === true;
        state.canEdit = false;
        state.selectedId = "";
        state.selectedEdgeId = "";
        state.connectSource = "";
        state.search = "";
        state.validation = null;
        runtime.dirty = false;
        runtime.conflictPending = false;
        state.mode = "select";
        runtime.keywordCandidates = null;
        api.resetHistory();
        api.resetViewMode();
        api.resetGeneratePanel();
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
        const summary = byId("scenarioValidateSummary");
        if (summary) summary.textContent = "";

        if (!state.scenarioId) {
            setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
            setEmpty(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"));
            emitSelection(null);
            return;
        }

        setStatus(t("scenario.tree.loading", "加载中…"), "secondary");
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<NarrativePayload>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 404) {
                    setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "warning");
                    renderNotBuiltState();
                    emitSelection(null);
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            state.revision = data.data.revision;
            state.readonly = Boolean(data.data.permissions?.readonly);
            state.canEdit = Boolean(data.data.permissions?.can_edit);
            updateStatusBadge();
            api.syncGeneratePanel();
            renderAll();
            api.updateHistoryButtons();
            window.requestAnimationFrame(() => api.fitView());
            window.ScenarioTriggers?.syncRevision(state.revision);
            emitSelection(null);
            if (state.canEdit) void api.runValidation();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setStatus(t("scenario.tree.load_failed", "世界树加载失败"), "danger");
            setEmpty(message);
        }
    }

    /** 没有世界树时的空态：图谱页签给出「自动建图」入口，其它面板只给提示。 */
    function renderNotBuiltState(): void {
        const message = t("scenario.narrative.build.title", "该剧本还没有世界树");
        const hint = t("scenario.narrative.build.hint", "按现有场景卡生成，生成后可在编辑器里继续调整。");
        const readonlyHint = t("scenario.narrative.build.readonly", "直接导入的剧本没有场景卡，无法自动建图。");
        const inline = `<p class="text-muted small mb-0">${escapeHtml(message)}</p>`;
        const list = byId("scenarioTreeList");
        if (list) list.innerHTML = inline;
        const inspector = byId("scenarioNodeInspector");
        if (inspector) inspector.innerHTML = inline;
        const graph = byId("scenarioGraphCanvas");
        if (!graph) return;
        if (state.readonly) {
            graph.innerHTML = `
                <div class="scenario-graph-empty">
                    <p class="mb-2">${escapeHtml(message)}</p>
                    <p class="text-muted small mb-0">${escapeHtml(readonlyHint)}</p>
                </div>`;
            return;
        }
        graph.innerHTML = `
            <div class="scenario-graph-empty">
                <p class="mb-2">${escapeHtml(message)}</p>
                <button type="button" class="btn btn-sm btn-primary" data-build-narrative>
                    ${escapeHtml(t("scenario.narrative.build.action", "自动建图"))}
                </button>
                <p class="text-muted small mt-2 mb-0">${escapeHtml(hint)}</p>
            </div>`;
    }

    /** 按现有场景卡手动建图；只在还没有 narrative.json 时由后端执行。 */
    async function buildNarrative(trigger: HTMLButtonElement | null): Promise<void> {
        if (!state.scenarioId) return;
        const button = trigger || byId<HTMLButtonElement>("scenarioGraphCanvas")?.querySelector<HTMLButtonElement>("[data-build-narrative]") || null;
        if (button) {
            button.disabled = true;
            button.textContent = t("scenario.narrative.build.building", "正在建图…");
        }
        setStatus(t("scenario.narrative.build.building", "正在建图…"), "secondary");
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<NarrativePayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/build`,
                { method: "POST" },
            );
            if (!response.ok || !data?.success) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            showNotification(t("scenario.narrative.build.done", "世界树已生成。"), "success");
            await load(state.scenarioId);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "warning");
            renderNotBuiltState();
            showNotification(message, "error");
        }
    }

    function clear(): void {
        state.scenarioId = 0;
        state.bundle = null;
        state.revision = 0;
        state.readonly = false;
        state.canEdit = false;
        state.selectedId = "";
        state.selectedEdgeId = "";
        state.connectSource = "";
        state.search = "";
        state.validation = null;
        runtime.dirty = false;
        runtime.conflictPending = false;
        state.positions = new Map();
        state.drag = null;
        state.pan = null;
        runtime.keywordCandidates = null;
        api.resetHistory();
        api.resetViewMode();
        api.resetGeneratePanel();
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
        setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
        api.updatePublishGuard(0);
        emitSelection(null);
        showTab("basic");
    }

    function init(): void {
        byId("scenarioTreeSearch")?.addEventListener("input", (event) => {
            state.search = (event.target as HTMLInputElement).value.trim().toLowerCase();
            api.renderTree();
        });
        byId("scenarioTreeList")?.addEventListener("click", (event) => {
            const target = (event.target as HTMLElement).closest<HTMLElement>("[data-node-id]");
            if (target?.dataset.nodeId) api.selectNode(target.dataset.nodeId);
        });
        api.bindTreeEditing();
        byId("scenarioNodeInspector")?.addEventListener("input", () => markDirty(true));
        byId("scenarioNodeInspector")?.addEventListener("change", () => markDirty(true));
        byId("scenarioValidateResults")?.addEventListener("click", (event) => {
            const accept = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-accept");
            if (accept?.dataset.reviewTarget) {
                void api.acceptReviewIssue(accept.dataset.reviewTarget);
                return;
            }
            const target = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-jump");
            if (target?.dataset.nodeId) api.selectNode(target.dataset.nodeId);
        });
        byId("scenarioValidateRun")?.addEventListener("click", () => {
            void api.runValidation();
        });
        byId("scenarioRepairRun")?.addEventListener("click", () => {
            void api.repairGraph();
        });
        byId("scenarioRepairExplain")?.addEventListener("click", () => {
            api.explainWithAgent();
        });
        byId("scenarioNodeInspector")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("#scenarioNodeSave")) {
                void api.saveNode();
                return;
            }
            if (target.closest("#scenarioNodeExtractKeywords")) {
                void api.extractNodeKeywords();
                return;
            }
            if (target.closest("#scenarioNodeDelete")) {
                void api.deleteNode();
                return;
            }
            const chip = target.closest<HTMLElement>("[data-keyword-index]");
            if (chip) api.applyKeywordCandidate(Number(chip.dataset.keywordIndex));
        });
        byId("scenarioNodeInspector")?.addEventListener("change", (event) => {
            const target = event.target as HTMLSelectElement;
            if (target.id === "scenarioNodeVisibility") N.syncVisibilityFields("scenarioNode", target.value);
            if (target.id === "scenarioNodeRevealType") N.syncRevealType("scenarioNode");
        });
        document.querySelectorAll<HTMLInputElement>('input[name="scenarioGenerateSource"]').forEach((radio) => {
            radio.addEventListener("change", api.syncGenerateSource);
        });
        byId("scenarioGenerateRun")?.addEventListener("click", () => {
            void api.startGeneration();
        });
        byId("scenarioGenerateApply")?.addEventListener("click", () => {
            void api.applyGeneration();
        });
        byId("scenarioGraphCanvas")?.addEventListener("click", (event) => {
            const buildButton = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-build-narrative]");
            if (buildButton) {
                event.preventDefault();
                void buildNarrative(buildButton);
            }
        });
        byId("scenarioGenerateCancel")?.addEventListener("click", () => {
            void api.cancelGeneration();
        });
        api.syncGenerateSource();
        api.resetGeneratePanel();
        api.bindGraphEvents();
    }

    // 各子模块通过 api 注册表回调这两个入口；漏注册会让 api.renderAll() /
    // api.load() 变成 undefined，保存节点、撤销重做、冲突刷新都会抛错。
    Object.assign(api, { renderAll, load });

    window.ScenarioNarrative = {
        load,
        clear,
        init,
        syncRevision: N.syncRevision,
        revision: () => state.revision,
        canEdit: () => state.canEdit && !state.readonly,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
