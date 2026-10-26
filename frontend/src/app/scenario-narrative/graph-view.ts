/**
 * 剧本世界树 / 图谱：图谱绘制、连线详情与视图模式（§8.1.2、§8.2）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, nodeById, edgeById, setEmpty, VISIBILITY_LABEL, visibilitySourceLabel, revealBlockMarkup, readReveal, revealToCondition, syncRevealType } = N;

    function edgeMarkup(edge: GraphEdge, positions: Map<string, { x: number; y: number }>): string {
        const from = positions.get(edge.from);
        const to = positions.get(edge.to);
        if (!from || !to) return "";
        const playerMode = state.viewMode === "player";
        const fromNode = nodeById(edge.from);
        const toNode = nodeById(edge.to);
        if (playerMode) {
            if (!fromNode || !toNode) return "";
            if ((fromNode.visibility || "public") === "kp_only" || (toNode.visibility || "public") === "kp_only") return "";
            if ((edge.visibility || "public") === "kp_only") return "";
        }
        const hiddenEdge = (edge.visibility || "public") !== "public";
        const dashed = edge.hard && !hiddenEdge ? "" : ` stroke-dasharray="6 4"`;
        const color = hiddenEdge ? "#adb5bd" : edge.hard ? "#c0392b" : "#8a94a6";
        const active = state.selectedEdgeId === edge.id;
        return `<line class="scenario-graph-edge${active ? " active" : ""}" data-edge-id="${escapeHtml(edge.id)}" data-edge-from="${escapeHtml(edge.from)}" data-edge-to="${escapeHtml(edge.to)}" x1="${from.x + 85}" y1="${from.y + 24}" x2="${to.x + 85}" y2="${to.y + 24}" stroke="${active ? "#f08c00" : color}" stroke-width="${active ? 2.6 : 1.6}"${dashed}><title>${escapeHtml(edge.label || edge.type || "")}${hiddenEdge ? " · 隐藏" : ""}</title></line>`;
    }

    function nodeMarkup(node: GraphNode, positions: Map<string, { x: number; y: number }>): string {
        const position = positions.get(node.id);
        if (!position) return "";
        const visibility = node.visibility || "public";
        const playerMode = state.viewMode === "player";
        if (playerMode && visibility === "kp_only") return "";
        const meta = N.kindMeta(node.kind);
        const selected = state.selectedId === node.id;
        const connectSource = state.connectSource === node.id;
        const title = node.title.length > 10 ? `${node.title.slice(0, 10)}…` : node.title;
        let stroke = meta.color;
        let fill = "#ffffff";
        let textColor = "#212529";
        let dash = "";
        if (visibility === "hidden") {
            fill = playerMode ? "#e9ecef" : "#fff3bf";
            stroke = playerMode ? "#adb5bd" : "#f08c00";
            textColor = "#495057";
            dash = ` stroke-dasharray="5 4"`;
        } else if (visibility === "kp_only") {
            fill = "#dee2e6";
            stroke = "#868e96";
            textColor = "#495057";
            dash = ` stroke-dasharray="3 3"`;
        }
        if (selected) {
            fill = meta.color;
            textColor = "#ffffff";
            dash = "";
        }
        if (connectSource) stroke = "#f08c00";
        const badge = playerMode
            ? (visibility === "hidden" ? t("scenario.visibility.hidden_placeholder", "未揭示") : meta.label)
            : (visibility === "kp_only" ? "KP ONLY" : visibility === "hidden" ? t("scenario.visibility.hidden_placeholder", "未揭示") : meta.label);
        return `<g class="scenario-graph-node${selected ? " active" : ""}${connectSource ? " source" : ""}" data-node-id="${escapeHtml(node.id)}" data-visibility="${escapeHtml(visibility)}" data-x="${position.x}" data-y="${position.y}" transform="translate(${position.x},${position.y})">
            <rect width="170" height="48" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="${connectSource ? 3 : 1.6}"${dash}></rect>
            <text x="12" y="21" font-size="12" fill="${textColor}">${escapeHtml(title || node.id)}</text>
            <text x="12" y="37" font-size="10" fill="${selected ? "#ffffff" : textColor}">${escapeHtml(badge)}</text>
            ${node.layout_locked ? `<text x="154" y="14" font-size="10" fill="${selected ? "#ffffff" : "#868e96"}">🔒</text>` : ""}
            ${visibility === "kp_only" ? `<text x="154" y="40" font-size="10" fill="${selected ? "#ffffff" : "#868e96"}">🔐</text>` : ""}
        </g>`;
    }

    function renderGraph(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;
        const bundle = state.bundle;
        if (!bundle || bundle.nodes.length === 0) {
            setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
            return;
        }
        state.positions = api.defaultPositions(bundle);
        const positions = state.positions;
        const { width, height } = api.viewportSize();
        const edges = bundle.edges.map((edge) => edgeMarkup(edge, positions)).join("");
        const nodes = bundle.nodes.map((node) => nodeMarkup(node, positions)).join("");
        canvas.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="世界树图谱"><g id="scenarioGraphViewport">${edges}${nodes}</g></svg>`;
        api.applyView();
        api.setGraphHint(state.viewMode === "player" ? api.playerHint() : api.modeHint(state.mode));
        api.renderEdgeActions();
    }

    function renderEdgeActions(): void {
        const container = byId("scenarioGraphEdgeActions");
        if (!container) return;
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable()) {
            container.innerHTML = "";
            return;
        }
        const other = nodeById(edge.to);
        const owner = nodeById(edge.from);
        const visibility = edge.visibility || "public";
        container.innerHTML = `
            <span class="small"><code>${escapeHtml(owner?.title || edge.from)}</code> → <code>${escapeHtml(other?.title || edge.to)}</code> <span class="text-muted">${escapeHtml(edge.type || "")}</span></span>
            <label class="scenario-edge-visibility">
                <span>${escapeHtml(t("scenario.tree.edit_visibility", "可见性"))}</span>
                <select class="form-select form-select-sm" id="scenarioEdgeVisibility">
                    ${(["public", "hidden", "kp_only"] as const).map((value) => `<option value="${value}"${visibility === value ? " selected" : ""}>${escapeHtml(VISIBILITY_LABEL[value])}</option>`).join("")}
                </select>
            </label>
            <span class="text-muted small">${escapeHtml(visibilitySourceLabel(edge))}</span>
            ${revealBlockMarkup("scenarioEdge", edge.reveal_when, visibility)}
            <button type="button" class="btn btn-sm btn-outline-primary" data-edge-save>${escapeHtml(t("scenario.graph.save_edge", "保存边"))}</button>
            <button type="button" class="btn btn-sm btn-outline-${edge.hard ? "secondary" : "danger"}" data-edge-toggle>${escapeHtml(edge.hard ? t("scenario.graph.make_soft", "改为 soft") : t("scenario.graph.make_hard", "改为 hard"))}</button>
            <button type="button" class="btn btn-sm btn-outline-danger" data-edge-delete>${escapeHtml(t("scenario.graph.delete_edge", "删除边"))}</button>`;
        syncRevealType("scenarioEdge");
    }

    async function saveEdge(): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !state.scenarioId || !editable()) return;
        const visibility = byId<HTMLSelectElement>("scenarioEdgeVisibility")?.value || "public";
        const reveal = readReveal("scenarioEdge");
        const revealWhen = visibility === "hidden" ? revealToCondition(reveal) : null;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, visibility, reveal_when: revealWhen } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            N.applyRevision(data.data.revision);
            if (state.bundle) {
                state.bundle.edges = state.bundle.edges.map((item) => (item.id === data.data!.edge.id ? data.data!.edge : item));
            }
            api.renderGraph();
            showNotification(t("scenario.graph.edge_saved", "边已保存。"), "success");
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    function setMode(mode: GraphMode): void {
        state.mode = mode;
        state.connectSource = "";
        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphMode === mode);
        });
        api.setGraphHint(api.modeHint(mode));
        api.renderGraph();
    }

    function setViewMode(mode: "kp" | "player"): void {
        state.viewMode = mode;
        state.connectSource = "";
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphView === mode);
        });
        byId("scenarioGraphCanvas")?.classList.toggle("is-player-view", mode === "player");
        api.renderGraph();
    }

    function resetViewMode(): void {
        state.viewMode = "kp";
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphView === "kp");
        });
        byId("scenarioGraphCanvas")?.classList.toggle("is-player-view", false);
    }

    Object.assign(api, {
        renderGraph,
        renderEdgeActions,
        saveEdge,
        setMode,
        setViewMode,
        resetViewMode,
    });
})(window);
