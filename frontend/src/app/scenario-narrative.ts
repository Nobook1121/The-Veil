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
                    setEmpty(t("scenario.tree.not_generated", "该剧本还没有世界树。运行 narrative build 生成后再查看。"));
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
        api.syncGenerateSource();
        api.resetGeneratePanel();
        api.bindGraphEvents();
    }

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
