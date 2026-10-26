/**
 * 剧本世界树 / 图谱：整包快照式的撤销 / 重做（§8.1.6 / §8.1.2）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, runtime, byId, t, editable, applyRevision, markDirty } = N;

    const HISTORY_LIMIT = 30;

    function snapshotBundle(): HistoryEntry | null {
        if (!state.bundle) return null;
        try {
            return { bundle: JSON.parse(JSON.stringify(state.bundle)) as GraphBundle, revision: state.revision };
        } catch {
            return null;
        }
    }

    function resetHistory(): void {
        runtime.undoStack = [];
        runtime.redoStack = [];
        updateHistoryButtons();
    }

    /** 在发起写请求前调用；失败时用 `discardHistory` 撤销这次记录。 */
    function pushHistory(): HistoryEntry | null {
        const entry = snapshotBundle();
        if (!entry) return null;
        runtime.undoStack.push(entry);
        if (runtime.undoStack.length > HISTORY_LIMIT) runtime.undoStack.shift();
        runtime.redoStack = [];
        updateHistoryButtons();
        return entry;
    }

    function discardHistory(entry: HistoryEntry | null): void {
        if (!entry) return;
        const index = runtime.undoStack.lastIndexOf(entry);
        if (index >= 0) runtime.undoStack.splice(index, 1);
        updateHistoryButtons();
    }

    function updateHistoryButtons(): void {
        const allowed = editable();
        const undo = byId<HTMLButtonElement>("scenarioGraphUndo");
        const redo = byId<HTMLButtonElement>("scenarioGraphRedo");
        if (undo) {
            undo.disabled = !allowed || runtime.undoStack.length === 0;
            undo.title = `${t("scenario.graph.undo", "撤销")} (Ctrl+Z)`;
        }
        if (redo) {
            redo.disabled = !allowed || runtime.redoStack.length === 0;
            redo.title = `${t("scenario.graph.redo", "重做")} (Ctrl+Y)`;
        }
    }

    async function restoreHistory(entry: HistoryEntry): Promise<boolean> {
        if (!state.scenarioId || !state.bundle) return false;
        const bundle = JSON.parse(JSON.stringify(entry.bundle)) as GraphBundle & { meta?: Record<string, unknown> };
        // 服务端以传入 meta.revision 作为基线累加；这里强制对齐当前修订号，
        // 避免撤销把 revision 回退导致后续写请求全部冲突。
        bundle.meta = { ...(bundle.meta || {}), revision: state.revision };
        runtime.restoringHistory = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: state.revision, bundle } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await api.handleConflict();
                    return false;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            applyRevision(data.data.revision);
            runtime.keywordCandidates = null;
            markDirty(false);
            api.renderAll();
            if (state.canEdit) void api.runValidation();
            return true;
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
            return false;
        } finally {
            runtime.restoringHistory = false;
        }
    }

    async function undoHistory(): Promise<void> {
        if (runtime.restoringHistory || !editable() || runtime.undoStack.length === 0) return;
        const entry = runtime.undoStack.pop() as HistoryEntry;
        const current = snapshotBundle();
        const ok = await restoreHistory(entry);
        if (!ok) {
            runtime.undoStack.push(entry);
            updateHistoryButtons();
            return;
        }
        if (current) {
            runtime.redoStack.push(current);
            if (runtime.redoStack.length > HISTORY_LIMIT) runtime.redoStack.shift();
        }
        updateHistoryButtons();
    }

    async function redoHistory(): Promise<void> {
        if (runtime.restoringHistory || !editable() || runtime.redoStack.length === 0) return;
        const entry = runtime.redoStack.pop() as HistoryEntry;
        const current = snapshotBundle();
        const ok = await restoreHistory(entry);
        if (!ok) {
            runtime.redoStack.push(entry);
            updateHistoryButtons();
            return;
        }
        if (current) {
            runtime.undoStack.push(current);
            if (runtime.undoStack.length > HISTORY_LIMIT) runtime.undoStack.shift();
        }
        updateHistoryButtons();
    }

    Object.assign(api, {
        snapshotBundle,
        resetHistory,
        pushHistory,
        discardHistory,
        updateHistoryButtons,
        undoHistory,
        redoHistory,
    });
})(window);
