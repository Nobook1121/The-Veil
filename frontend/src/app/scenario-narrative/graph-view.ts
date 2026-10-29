/**
 * 剧本世界树 / 图谱：图谱绘制、连线详情与视图模式（§8.1.2、§8.2）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, edgeText, edgeMeta, nodeById, edgeById, parentOf, setEmpty, VISIBILITY_LABEL, visibilitySourceLabel, revealBlockMarkup, readReveal, revealToCondition, syncRevealType } = N;

    const NODE_W = 170;
    const NODE_H = 48;
    const PARENT_COLOR = "#7048e8";

    function centerOf(position: { x: number; y: number }): { x: number; y: number } {
        return { x: position.x + NODE_W / 2, y: position.y + NODE_H / 2 };
    }

    /** 线段与节点矩形边界的交点：让箭头停在节点外，而不是被节点盖住。 */
    function boundary(
        center: { x: number; y: number },
        target: { x: number; y: number },
        halfW = NODE_W / 2 + 2,
        halfH = NODE_H / 2 + 2,
    ): { x: number; y: number } {
        const dx = target.x - center.x;
        const dy = target.y - center.y;
        if (!dx && !dy) return center;
        const scaleX = dx ? halfW / Math.abs(dx) : Infinity;
        const scaleY = dy ? halfH / Math.abs(dy) : Infinity;
        const scale = Math.min(scaleX, scaleY);
        return { x: center.x + dx * scale, y: center.y + dy * scale };
    }

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
        const fromCenter = centerOf(from);
        const toCenter = centerOf(to);
        const start = boundary(fromCenter, toCenter);
        const end = boundary(toCenter, fromCenter);
        const hiddenEdge = (edge.visibility || "public") !== "public";
        const meta = edgeMeta(edge.type);
        const dashed = edge.hard && !hiddenEdge ? "" : ` stroke-dasharray="6 4"`;
        const color = hiddenEdge ? "#adb5bd" : edge.hard ? "#c0392b" : meta.color;
        const active = state.selectedEdgeId === edge.id;
        // `bidirectional !== false`：老数据缺字段时按双向处理，与模型层的默认值一致。
        const directed = edge.bidirectional === false;
        const marker = directed ? ` marker-end="url(#scenarioGraphArrow)"` : "";
        const midpoint = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 - 6 };
        const stroke = active ? "#f08c00" : color;
        return `<line class="scenario-graph-edge${active ? " active" : ""}" data-edge-id="${escapeHtml(edge.id)}" data-edge-from="${escapeHtml(edge.from)}" data-edge-to="${escapeHtml(edge.to)}" x1="${start.x}" y1="${start.y}" x2="${end.x}" y2="${end.y}" stroke="${stroke}" stroke-width="${active ? 2.6 : 1.6}"${dashed}${marker}><title>${escapeHtml(meta.label)}：${escapeHtml(meta.desc)}${hiddenEdge ? " · 隐藏" : ""}</title></line>
            <text class="scenario-graph-edge-label${active ? " active" : ""}" data-edge-id="${escapeHtml(edge.id)}" data-edge-label-for="${escapeHtml(edge.id)}" x="${midpoint.x}" y="${midpoint.y}" fill="${stroke}" text-anchor="middle">${escapeHtml(edgeText(edge))}</text>`;
    }

    /** 隶属关系：parent 用虚线画出来，并标注「隶属」，让子节点归属一目了然。 */
    function parentMarkup(node: GraphNode, positions: Map<string, { x: number; y: number }>): string {
        const parentId = parentOf(node);
        if (!parentId) return "";
        const from = positions.get(parentId);
        const to = positions.get(node.id);
        if (!from || !to) return "";
        const playerMode = state.viewMode === "player";
        if (playerMode) {
            const parentNode = nodeById(parentId);
            if (!parentNode) return "";
            if ((parentNode.visibility || "public") === "kp_only" || (node.visibility || "public") === "kp_only") return "";
        }
        const start = boundary(centerOf(from), centerOf(to), NODE_W / 2 + 2, NODE_H / 2 + 8);
        const end = boundary(centerOf(to), centerOf(from), NODE_W / 2 + 2, NODE_H / 2 + 8);
        return `<line class="scenario-graph-parent" data-parent-from="${escapeHtml(parentId)}" data-parent-to="${escapeHtml(node.id)}" x1="${start.x}" y1="${start.y}" x2="${end.x}" y2="${end.y}" stroke="${PARENT_COLOR}" stroke-width="1.3" stroke-dasharray="4 4"></line>
            <text class="scenario-graph-parent-label" data-parent-label-for="${escapeHtml(node.id)}" x="${(start.x + end.x) / 2}" y="${(start.y + end.y) / 2 - 5}" fill="${PARENT_COLOR}" text-anchor="middle">${escapeHtml(t("scenario.graph.parent_link", "隶属"))}</text>`;
    }

    /** 拖拽节点后重算所有连线端点，避免连线停在旧位置。 */
    function updateEdgeGeometry(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;
        canvas.querySelectorAll<SVGLineElement>("line[data-edge-from]").forEach((line) => {
            const from = state.positions.get(line.dataset.edgeFrom || "");
            const to = state.positions.get(line.dataset.edgeTo || "");
            if (!from || !to) return;
            const start = boundary(centerOf(from), centerOf(to));
            const end = boundary(centerOf(to), centerOf(from));
            line.setAttribute("x1", String(start.x));
            line.setAttribute("y1", String(start.y));
            line.setAttribute("x2", String(end.x));
            line.setAttribute("y2", String(end.y));
            const label = canvas.querySelector<SVGTextElement>(
                `text[data-edge-label-for="${line.dataset.edgeId || ""}"]`,
            );
            if (label) {
                label.setAttribute("x", String((start.x + end.x) / 2));
                label.setAttribute("y", String((start.y + end.y) / 2 - 6));
            }
        });
        canvas.querySelectorAll<SVGLineElement>("line[data-parent-from]").forEach((line) => {
            const from = state.positions.get(line.dataset.parentFrom || "");
            const to = state.positions.get(line.dataset.parentTo || "");
            if (!from || !to) return;
            const start = boundary(centerOf(from), centerOf(to), NODE_W / 2 + 2, NODE_H / 2 + 8);
            const end = boundary(centerOf(to), centerOf(from), NODE_W / 2 + 2, NODE_H / 2 + 8);
            line.setAttribute("x1", String(start.x));
            line.setAttribute("y1", String(start.y));
            line.setAttribute("x2", String(end.x));
            line.setAttribute("y2", String(end.y));
            const label = canvas.querySelector<SVGTextElement>(
                `text[data-parent-label-for="${line.dataset.parentTo || ""}"]`,
            );
            if (label) {
                label.setAttribute("x", String((start.x + end.x) / 2));
                label.setAttribute("y", String((start.y + end.y) / 2 - 5));
            }
        });
    }

    /** 图谱页签的「说明 / 图例」：用途、节点类型、每种边的含义与线型约定。 */
    function renderGraphGuide(): void {
        const host = byId("scenarioGraphGuideBody");
        if (!host) return;
        const kinds = Object.values(N.KIND_META)
            .sort((left, right) => left.order - right.order)
            .map((meta) => `<span class="scenario-graph-legend-kind"><i class="fa ${meta.icon}" style="color:${meta.color}" aria-hidden="true"></i>${escapeHtml(meta.label)}</span>`)
            .join("");
        const edges = Object.entries(N.EDGE_META)
            .map(([type, meta]) => `<tr><td><span class="scenario-graph-legend-line" style="background:${meta.color}"></span>${escapeHtml(meta.label)}${meta.movement ? ` <span class="badge bg-light text-dark">${escapeHtml(t("scenario.graph.movement_badge", "可走位"))}</span>` : ""}</td><td><code>${escapeHtml(type)}</code></td><td>${escapeHtml(meta.desc)}</td></tr>`)
            .join("");
        host.innerHTML = `
            <p class="mb-2">${escapeHtml(t("scenario.graph.guide_intro", "图谱把剧情结构画成一张网：节点是地点、NPC、物件、线索与结局；边是它们之间的关系。连线上的文字就是关系说明，箭头表示方向。"))}</p>
            <div class="scenario-graph-legend-kinds">${kinds}</div>
            <ul class="mb-2">
                <li>${escapeHtml(t("scenario.graph.guide_direction", "箭头 = 单向：只能从起点走向终点。例如车厢之间用「顺序」单向连接，进入下一节车厢后不能回头，AI 也就不会把队伍送回三号车厢。"))}</li>
                <li>${escapeHtml(t("scenario.graph.guide_parent", "紫色虚线 = 隶属：子节点的 parent 指向父节点。例如 NPC 乘务员作为子节点挂在四号车厢下，钥匙挂在三号车厢下。"))}</li>
                <li>${escapeHtml(t("scenario.graph.guide_hard", "红色实线 = hard 边：运行时真的会拦住玩家；虚线 = soft 边：只参与排序与提示。"))}</li>
                <li>${escapeHtml(t("scenario.graph.guide_notes", "边的「KP 备注」只给 KP 看：把剧情约束写死在这里（例如「钥匙在乘务员身上，不在别处」），可减少 KP 临场编造。"))}</li>
            </ul>
            <table class="table table-sm scenario-graph-legend-table">
                <thead><tr><th>${escapeHtml(t("scenario.graph.guide_edge", "关系"))}</th><th>ID</th><th>${escapeHtml(t("scenario.graph.guide_meaning", "含义"))}</th></tr></thead>
                <tbody>${edges}</tbody>
            </table>`;
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
        // 记录本次渲染所用的画布宽度：页签从隐藏变为可见后据此重新适配视图。
        canvas.dataset.graphWidth = String(width);
        const parentLinks = bundle.nodes.map((node) => parentMarkup(node, positions)).join("");
        const edges = bundle.edges.map((edge) => edgeMarkup(edge, positions)).join("");
        const nodes = bundle.nodes.map((node) => nodeMarkup(node, positions)).join("");
        canvas.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="世界树图谱">
            <defs>
                <marker id="scenarioGraphArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" fill="context-stroke"></path>
                </marker>
            </defs>
            <g id="scenarioGraphViewport">${parentLinks}${edges}${nodes}</g>
        </svg>`;
        api.applyView();
        api.setGraphHint(state.viewMode === "player" ? api.playerHint() : api.modeHint(state.mode));
        api.renderEdgeActions();
    }

    /** 从节点编辑器里选中某条连接：画出选中态，并把边编辑区滚动到可见位置。 */
    function selectEdge(edgeId: string, scroll = true): void {
        if (!edgeId) return;
        state.selectedEdgeId = edgeId;
        api.renderGraph();
        api.renderEdgeActions();
        if (scroll) {
            byId("scenarioGraphEdgeActions")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
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
        const type = edge.type || "adjacent";
        const meta = edgeMeta(type);
        const oneWay = edge.bidirectional === false;
        container.innerHTML = `
            <span class="small"><code>${escapeHtml(owner?.title || edge.from)}</code> → <code>${escapeHtml(other?.title || edge.to)}</code></span>
            <label class="scenario-edge-field">
                <span>${escapeHtml(t("scenario.graph.edge_type", "关系类型"))}</span>
                <select class="form-select form-select-sm" id="scenarioEdgeType">
                    ${N.EDGE_TYPES.map(([value, label]) => `<option value="${value}"${type === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("")}
                </select>
            </label>
            <label class="scenario-edge-field scenario-edge-field-wide">
                <span>${escapeHtml(t("scenario.graph.edge_label", "边描述"))}</span>
                <input type="text" class="form-control form-control-sm" id="scenarioEdgeLabel" value="${escapeHtml(edge.label || "")}" placeholder="${escapeHtml(t("scenario.graph.edge_label_hint", "例：单向通过，进入后不能返回"))}">
            </label>
            <label class="scenario-graph-hard-toggle">
                <input type="checkbox" id="scenarioEdgeOneWay" ${oneWay ? "checked" : ""}>
                <span>${escapeHtml(t("scenario.graph.one_way", "单向"))}</span>
            </label>
            <label class="scenario-edge-field scenario-edge-field-wide">
                <span>${escapeHtml(t("scenario.graph.edge_kp_notes", "KP 备注"))}</span>
                <textarea class="form-control form-control-sm" id="scenarioEdgeKpNotes" rows="2" placeholder="${escapeHtml(t("scenario.graph.edge_kp_notes_hint", "例：钥匙在乘务员身上，不在别处；找不到钥匙时不要临时改设定"))}">${escapeHtml(edge.kp_notes || "")}</textarea>
            </label>
            <label class="scenario-edge-field">
                <span>${escapeHtml(t("scenario.tree.edit_visibility", "可见性"))}</span>
                <select class="form-select form-select-sm" id="scenarioEdgeVisibility">
                    ${(["public", "hidden", "kp_only"] as const).map((value) => `<option value="${value}"${visibility === value ? " selected" : ""}>${escapeHtml(VISIBILITY_LABEL[value])}</option>`).join("")}
                </select>
            </label>
            <span class="text-muted small">${escapeHtml(visibilitySourceLabel(edge))}</span>
            ${revealBlockMarkup("scenarioEdge", edge.reveal_when, visibility)}
            <span class="scenario-edge-type-desc text-muted small" id="scenarioEdgeTypeDesc">${escapeHtml(meta.desc)}</span>
            <button type="button" class="btn btn-sm btn-outline-primary" data-edge-save>${escapeHtml(t("scenario.graph.save_edge", "保存边"))}</button>
            <button type="button" class="btn btn-sm btn-outline-info" data-edge-reverse title="${escapeHtml(t("scenario.graph.reverse_edge_hint", "交换起点与终点；id 保持不变"))}">
                <i class="fa fa-exchange" aria-hidden="true"></i>
                <span>${escapeHtml(t("scenario.graph.reverse_edge", "换向"))}</span>
            </button>
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
        const type = byId<HTMLSelectElement>("scenarioEdgeType")?.value || edge.type || "adjacent";
        const label = byId<HTMLInputElement>("scenarioEdgeLabel")?.value.trim() || "";
        const oneWay = byId<HTMLInputElement>("scenarioEdgeOneWay")?.checked === true;
        const kpNotes = byId<HTMLTextAreaElement>("scenarioEdgeKpNotes")?.value.trim() || "";
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                {
                    method: "PATCH",
                    body: {
                        base_revision: state.revision,
                        type,
                        label,
                        bidirectional: !oneWay,
                        kp_notes: kpNotes,
                        visibility,
                        reveal_when: revealWhen,
                    },
                },
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
            api.renderGraphEditor();
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
        renderGraphGuide,
        renderEdgeActions,
        selectEdge,
        saveEdge,
        updateEdgeGeometry,
        setMode,
        setViewMode,
        resetViewMode,
    });
})(window);
