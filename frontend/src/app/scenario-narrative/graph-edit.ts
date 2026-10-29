/**
 * 剧本世界树 / 图谱：画布交互、拖拽落盘、连线增删与快捷键（§8.1.6、§8.2）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, isTypingTarget, nodeById, edgeById, VISIBILITY_LABEL } = N;

    function clientToWorld(clientX: number, clientY: number): { x: number; y: number } {
        const canvas = byId("scenarioGraphCanvas");
        const svg = canvas?.querySelector<SVGSVGElement>("svg");
        if (!svg) return { x: 0, y: 0 };
        const rect = svg.getBoundingClientRect();
        const { width } = api.viewportSize();
        const scale = width / (rect.width || width);
        const localX = (clientX - rect.left) * scale;
        const localY = (clientY - rect.top) * scale;
        return { x: (localX - state.view.x) / state.view.k, y: (localY - state.view.y) / state.view.k };
    }

    function nodeElement(nodeId: string): SVGGElement | null {
        const canvas = byId("scenarioGraphCanvas");
        return canvas?.querySelector<SVGGElement>(`[data-node-id="${nodeId}"]`) || null;
    }

    function moveNodeVisual(nodeId: string, x: number, y: number): void {
        const element = nodeElement(nodeId);
        if (element) element.setAttribute("transform", `translate(${x},${y})`);
        // 端点带箭头、且要停在节点边框外，统一交给 graph-view 重算。
        api.updateEdgeGeometry();
    }

    function scheduleNodeVisual(nodeId: string, x: number, y: number): void {
        state.positions.set(nodeId, { x, y });
        const node = nodeById(nodeId);
        if (node) node.layout = { x, y };
        if (state.draggingFrame !== null) return;
        state.draggingFrame = window.requestAnimationFrame(() => {
            state.draggingFrame = null;
            moveNodeVisual(nodeId, x, y);
        });
    }

    async function persistLayout(nodeId: string): Promise<void> {
        const position = state.positions.get(nodeId);
        if (!position || !state.scenarioId) return;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(nodeId)}`,
                { method: "PATCH", body: { base_revision: state.revision, layout: { x: position.x, y: position.y } } },
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
            const saved = data.data.node;
            if (state.bundle) {
                state.bundle.nodes = state.bundle.nodes.map((item) => (item.id === saved.id ? { ...item, ...saved } : item));
            }
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function connectNodes(sourceId: string, targetId: string, options: { type?: string; hard?: boolean } = {}): Promise<void> {
        const type = options.type || (byId<HTMLSelectElement>("scenarioGraphEdgeType")?.value || "adjacent");
        const hard = options.hard ?? (byId<HTMLInputElement>("scenarioGraphEdgeHard")?.checked === true);
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges`,
                { method: "POST", body: { base_revision: state.revision, from: sourceId, to: targetId, type, hard, source: "author" } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    showNotification(data?.message || t("scenario.graph.duplicate_edge", "这条连接已存在。"), "warning");
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            N.applyRevision(data.data.revision);
            state.bundle?.edges.push(data.data.edge);
            state.connectSource = "";
            // 新建后立刻选中这条边，方便接着填写关系类型、边描述与 KP 备注。
            state.selectedEdgeId = data.data.edge.id;
            state.selectedId = "";
            api.renderGraph();
            api.renderInspector();
            api.renderGraphEditor();
            byId<HTMLInputElement>("scenarioEdgeLabel")?.focus();
            showNotification(t("scenario.graph.edge_created_edit", "连接已创建，可在下方填写边描述与 KP 备注。"), "success");
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function handleNodeClick(nodeId: string): Promise<void> {
        if (state.mode === "connect" && editable() && state.viewMode === "kp") {
            if (!state.connectSource) {
                state.connectSource = nodeId;
                api.renderGraph();
                return;
            }
            if (state.connectSource === nodeId) {
                state.connectSource = "";
                api.renderGraph();
                return;
            }
            const source = state.connectSource;
            state.connectSource = "";
            await connectNodes(source, nodeId);
            return;
        }
        api.selectNode(nodeId);
    }

    async function toggleEdgeHard(): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable()) return;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, hard: !edge.hard } },
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
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    /** 换向：交换 from / to，id 与可见性、条件、解锁状态都保持不变。 */
    async function reverseSelectedEdge(): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable() || !state.scenarioId) return;
        const fromTitle = nodeById(edge.to)?.title || edge.to;
        const toTitle = nodeById(edge.from)?.title || edge.from;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, from: edge.to, to: edge.from } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409 && /revision/i.test(data?.message || "")) {
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
            api.renderInspector();
            showNotification(
                t("scenario.graph.reverse_edge_done", "已换向：{from} → {to}。请检查边描述与 KP 备注是否还符合新方向。")
                    .replace("{from}", fromTitle)
                    .replace("{to}", toTitle),
                "success",
            );
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function deleteSelectedEdge(confirmFirst = false): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable()) return;
        if (confirmFirst) {
            const confirmed = await window.TrpgDialog?.confirm({
                title: t("scenario.graph.delete_edge", "删除连接"),
                message: t("scenario.graph.delete_edge_confirm", "确定删除选中的连接？"),
                confirmText: t("scenario.common.delete", "删除"),
                danger: true,
            });
            if (!confirmed) return;
        }
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "DELETE", body: { base_revision: state.revision } },
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
            if (state.bundle) state.bundle.edges = state.bundle.edges.filter((item) => item.id !== edge.id);
            state.selectedEdgeId = "";
            api.renderGraph();
            api.renderInspector();
            api.renderGraphEditor();
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    function kindOptions(selected = "location"): string {
        return Object.entries(N.KIND_META)
            .sort((left, right) => left[1].order - right[1].order)
            .map(([value, meta]) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(meta.label)}</option>`)
            .join("");
    }

    function openNodeCreate(): void {
        if (!editable() || !state.bundle) return;
        if (state.viewMode === "player") {
            showNotification(t("scenario.graph.add_node_player_blocked", "请先切换到 KP 视图再新增节点。"), "warning");
            return;
        }
        const modalElement = byId("scenarioNodeCreateModal");
        if (!modalElement) return;
        const parentSelect = byId<HTMLSelectElement>("scenarioNodeCreateParent");
        if (parentSelect) {
            parentSelect.innerHTML = `<option value="">${escapeHtml(t("scenario.graph.no_parent", "无（顶层节点）"))}</option>` +
                state.bundle.nodes
                    .map((node) => `<option value="${escapeHtml(node.id)}">${escapeHtml(N.kindMeta(node.kind).label)} · ${escapeHtml(node.title || node.id)}</option>`)
                    .join("");
        }
        const kindSelect = byId<HTMLSelectElement>("scenarioNodeCreateKind");
        if (kindSelect) kindSelect.innerHTML = kindOptions();
        const titleInput = byId<HTMLInputElement>("scenarioNodeCreateTitle");
        if (titleInput) titleInput.value = "";
        const idInput = byId<HTMLInputElement>("scenarioNodeCreateId");
        if (idInput) idInput.value = "";
        const visibilitySelect = byId<HTMLSelectElement>("scenarioNodeCreateVisibility");
        if (visibilitySelect) visibilitySelect.value = "public";
        (bootstrap.Modal.getInstance(modalElement) || new bootstrap.Modal(modalElement)).show();
        titleInput?.focus();
    }

    async function submitNodeCreate(): Promise<void> {
        if (!state.scenarioId || !editable()) return;
        const title = byId<HTMLInputElement>("scenarioNodeCreateTitle")?.value.trim() || "";
        if (!title) {
            showNotification(t("scenario.graph.add_node_title_required", "请填写节点标题。"), "warning");
            return;
        }
        const kind = byId<HTMLSelectElement>("scenarioNodeCreateKind")?.value || "location";
        const parent = byId<HTMLSelectElement>("scenarioNodeCreateParent")?.value || "";
        const visibility = byId<HTMLSelectElement>("scenarioNodeCreateVisibility")?.value || "public";
        const id = byId<HTMLInputElement>("scenarioNodeCreateId")?.value.trim() || "";
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes`,
                {
                    method: "POST",
                    body: {
                        base_revision: state.revision,
                        id: id || undefined,
                        kind,
                        title,
                        parent: parent || null,
                        visibility,
                        source: "author",
                    },
                },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    showNotification(data?.message || t("scenario.graph.add_node_duplicate", "节点 ID 已存在，请换一个。"), "warning");
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            N.applyRevision(data.data.revision);
            state.bundle?.nodes.push(data.data.node);
            bootstrap.Modal.getInstance(byId("scenarioNodeCreateModal")!)?.hide();
            api.selectNode(data.data.node.id);
            showNotification(t("scenario.graph.add_node_done", "节点已创建。"), "success");
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function deleteNode(): Promise<void> {
        const node = nodeById(state.selectedId);
        if (!node || !editable() || !state.scenarioId) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: t("scenario.graph.delete_node", "删除节点"),
            message: `${t("scenario.graph.delete_node_confirm", "确定删除节点")}「${node.title || node.id}」？${t("scenario.graph.delete_node_detail", "与它相连的边会一起删除，子节点会改挂到它的父节点。")}`,
            confirmText: t("scenario.common.delete", "删除"),
            danger: true,
        });
        if (!confirmed) return;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number; node_id: string; removed_edges: number; reparented: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(node.id)}`,
                { method: "DELETE", body: { base_revision: state.revision } },
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
                state.bundle.nodes = state.bundle.nodes
                    .filter((item) => item.id !== node.id)
                    .map((item) => (item.parent === node.id ? { ...item, parent: node.parent ?? null } : item));
                state.bundle.edges = state.bundle.edges.filter((item) => item.from !== node.id && item.to !== node.id);
            }
            state.selectedId = "";
            state.selectedEdgeId = "";
            api.renderAll();
            showNotification(
                t("scenario.graph.delete_node_done", "节点已删除，关联边也已清理。"),
                "success",
            );
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function reclassifyVisibility(): Promise<void> {
        if (!state.scenarioId || !editable() || !state.bundle) return;
        const endpoint = `/api/scenarios/${state.scenarioId}/narrative/visibility/reclassify`;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ReclassifyPayload>>(
                endpoint,
                { method: "POST", body: { base_revision: state.revision, apply: false } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const preview = data.data;
            if (preview.changed === 0) {
                showNotification(t("scenario.visibility.bulk_none", "没有需要调整的自动判定条目。"), "info");
                return;
            }
            const lines = preview.changes.slice(0, 8)
                .map((change) => `${change.title}：${VISIBILITY_LABEL[change.from] || change.from} → ${VISIBILITY_LABEL[change.to] || change.to}`)
                .join("\n");
            const confirmed = await window.TrpgDialog?.confirm({
                title: t("scenario.visibility.bulk_reclassify", "重新自动判定可见性"),
                message: `${t("scenario.visibility.bulk_confirm", "仅调整自动判定的条目，作者设置的可见性不会被覆盖：")}\n${lines}${preview.changes.length > 8 ? "\n…" : ""}`,
                confirmText: t("scenario.visibility.bulk_apply", "应用"),
            });
            if (!confirmed) return;
            const history = api.pushHistory();
            const applied = await TrpgApi.requestWithResponse<ApiResponse<ReclassifyPayload>>(
                endpoint,
                { method: "POST", body: { base_revision: state.revision, apply: true } },
            );
            if (!applied.response.ok || !applied.data?.success || !applied.data.data) {
                api.discardHistory(history);
                if (applied.response.status === 409) {
                    await api.handleConflict();
                    return;
                }
                throw new Error(applied.data?.message || `HTTP ${applied.response.status}`);
            }
            const result = applied.data.data;
            N.applyRevision(result.revision);
            if (state.bundle) {
                const updated = new Map(result.nodes.map((node) => [node.id, node]));
                state.bundle.nodes = state.bundle.nodes.map((node) => updated.get(node.id) || node);
            }
            api.renderAll();
            showNotification(
                `${t("scenario.visibility.bulk_done", "已重新判定")} ${result.changed}`,
                "success",
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    /** 平移判定阈值：小于它视为点击，避免按下即抖动的误伤。 */
    const PAN_THRESHOLD = 4;
    let panMoved = false;

    function bindGraphEvents(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;

        const startPan = (pointer: PointerEvent): void => {
            state.pan = {
                pointerId: pointer.pointerId,
                startClientX: pointer.clientX,
                startClientY: pointer.clientY,
                originX: state.view.x,
                originY: state.view.y,
            };
            panMoved = false;
            canvas.classList.add("is-panning");
            canvas.setPointerCapture?.(pointer.pointerId);
            pointer.preventDefault();
        };

        canvas.addEventListener("pointerdown", (event) => {
            const pointer = event as PointerEvent;
            // 中键在任意位置都能平移，和常见图编辑器保持一致。
            if (pointer.button === 1) {
                startPan(pointer);
                return;
            }
            const nodeTarget = (pointer.target as Element).closest?.<SVGGElement>("[data-node-id]");
            if (nodeTarget) {
                const nodeId = nodeTarget.getAttribute("data-node-id") || "";
                const node = nodeById(nodeId);
                if (state.mode === "select" && state.viewMode === "kp" && editable() && node && !node.layout_locked) {
                    const world = clientToWorld(pointer.clientX, pointer.clientY);
                    const origin = state.positions.get(nodeId) || { x: 0, y: 0 };
                    state.drag = {
                        nodeId,
                        pointerId: pointer.pointerId,
                        startWorldX: world.x,
                        startWorldY: world.y,
                        originX: origin.x,
                        originY: origin.y,
                        moved: false,
                    };
                    canvas.setPointerCapture?.(pointer.pointerId);
                    pointer.preventDefault();
                    return;
                }
                if (state.mode === "pan") {
                    startPan(pointer);
                    return;
                }
                // 锁定布局的节点、只读视图或连线模式：按下即选中，避免永远点不开编辑器。
                if (state.mode === "select" || state.mode === "connect") {
                    void handleNodeClick(nodeId);
                }
                return;
            }
            // 空白处与连线：任何模式下拖动都平移画布；不移动时仍然按点击处理。
            startPan(pointer);
        });

        canvas.addEventListener("pointermove", (event) => {
            const pointer = event as PointerEvent;
            if (state.drag && pointer.pointerId === state.drag.pointerId) {
                const world = clientToWorld(pointer.clientX, pointer.clientY);
                const x = Math.round(state.drag.originX + (world.x - state.drag.startWorldX));
                const y = Math.round(state.drag.originY + (world.y - state.drag.startWorldY));
                if (Math.abs(x - state.drag.originX) > 2 || Math.abs(y - state.drag.originY) > 2) {
                    state.drag.moved = true;
                }
                scheduleNodeVisual(state.drag.nodeId, x, y);
                return;
            }
            if (state.pan && pointer.pointerId === state.pan.pointerId) {
                const canvasRect = canvas.getBoundingClientRect();
                const scale = api.viewportSize().width / (canvasRect.width || 1);
                const dx = pointer.clientX - state.pan.startClientX;
                const dy = pointer.clientY - state.pan.startClientY;
                if (Math.abs(dx) > PAN_THRESHOLD || Math.abs(dy) > PAN_THRESHOLD) panMoved = true;
                state.view.x = state.pan.originX + dx * scale;
                state.view.y = state.pan.originY + dy * scale;
                api.applyView();
            }
        });

        const finishPointer = (event: PointerEvent): void => {
            if (state.drag && event.pointerId === state.drag.pointerId) {
                const drag = state.drag;
                state.drag = null;
                if (drag.moved) {
                    const position = state.positions.get(drag.nodeId);
                    if (position) void persistLayout(drag.nodeId);
                } else {
                    void handleNodeClick(drag.nodeId);
                }
                return;
            }
            if (state.pan && event.pointerId === state.pan.pointerId) {
                state.pan = null;
                canvas.classList.remove("is-panning");
                // pointercancel 不会再触发 click，这里清掉标记，避免吞掉下一次点击。
                if (event.type === "pointercancel") panMoved = false;
            }
        };
        canvas.addEventListener("pointerup", finishPointer);
        canvas.addEventListener("pointercancel", finishPointer);

        canvas.addEventListener("wheel", (event) => {
            const wheel = event as WheelEvent;
            wheel.preventDefault();
            const before = clientToWorld(wheel.clientX, wheel.clientY);
            const factor = wheel.deltaY < 0 ? 1.12 : 1 / 1.12;
            const nextK = Math.max(0.2, Math.min(2.5, state.view.k * factor));
            const canvasRect = canvas.getBoundingClientRect();
            const scale = api.viewportSize().width / (canvasRect.width || 1);
            const localX = (wheel.clientX - canvasRect.left) * scale;
            const localY = (wheel.clientY - canvasRect.top) * scale;
            state.view.k = nextK;
            state.view.x = localX - before.x * nextK;
            state.view.y = localY - before.y * nextK;
            api.applyView();
        }, { passive: false });

        canvas.addEventListener("click", (event) => {
            // 刚刚拖动过画布：这次 click 是平移的收尾，不应触发选中。
            if (panMoved) {
                panMoved = false;
                return;
            }
            const target = event.target as Element;
            const edgeTarget = target.closest?.<SVGLineElement>("[data-edge-id]");
            if (edgeTarget) {
                state.selectedEdgeId = edgeTarget.getAttribute("data-edge-id") || "";
                state.selectedId = "";
                api.renderGraph();
                api.renderInspector();
                N.emitSelection(null);
                return;
            }
            if (!target.closest?.("[data-node-id]")) {
                state.selectedEdgeId = "";
                api.renderEdgeActions();
            }
        });

        byId("scenarioGraphEdgeActions")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("[data-edge-save]")) {
                void api.saveEdge();
                return;
            }
            if (target.closest("[data-edge-reverse]")) {
                void reverseSelectedEdge();
                return;
            }
            if (target.closest("[data-edge-toggle]")) {
                void api.toggleEdgeHard();
                return;
            }
            if (target.closest("[data-edge-delete]")) {
                void api.deleteSelectedEdge();
            }
        });

        byId("scenarioGraphEdgeActions")?.addEventListener("change", (event) => {
            const target = event.target as HTMLSelectElement;
            if (target.id === "scenarioEdgeVisibility") N.syncVisibilityFields("scenarioEdge", target.value);
            if (target.id === "scenarioEdgeRevealType") N.syncRevealType("scenarioEdge");
            if (target.id === "scenarioEdgeType") {
                const desc = byId("scenarioEdgeTypeDesc");
                if (desc) desc.textContent = N.edgeMeta(target.value).desc;
            }
        });

        byId("scenarioGraphAddNode")?.addEventListener("click", openNodeCreate);
        byId("scenarioNodeCreateSubmit")?.addEventListener("click", () => {
            void submitNodeCreate();
        });
        byId<HTMLInputElement>("scenarioNodeCreateTitle")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                void submitNodeCreate();
            }
        });
        api.renderGraphGuide();

        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.addEventListener("click", () => api.setMode((button.dataset.graphMode as GraphMode) || "select"));
        });
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.addEventListener("click", () => api.setViewMode(button.dataset.graphView === "player" ? "player" : "kp"));
        });
        byId("scenarioGraphFit")?.addEventListener("click", () => api.fitView());
        // 图谱页签首次显示时画布才有真实尺寸：重新渲染并适配，节点不会被挤在角落。
        byId("scenarioTabGraphBtn")?.addEventListener("shown.bs.tab", () => {
            window.requestAnimationFrame(() => {
                const { width } = api.viewportSize();
                if (canvas.dataset.graphWidth !== String(width)) {
                    api.renderGraph();
                    api.fitView();
                } else {
                    api.applyView();
                }
            });
        });
        byId("scenarioGraphUndo")?.addEventListener("click", () => {
            void api.undoHistory();
        });
        byId("scenarioGraphRedo")?.addEventListener("click", () => {
            void api.redoHistory();
        });
        byId("scenarioGraphAutoLayout")?.addEventListener("click", () => {
            void api.autoLayout();
        });

        // 图谱节点编辑面板：保存节点、删除节点、增删改连接。
        const nodeEditor = byId("scenarioGraphNodeEditor");
        nodeEditor?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("[data-graph-node-save]") || target.closest("#scenarioGraphNodeSave")) {
                void api.saveGraphNode();
                return;
            }
            if (target.closest("#scenarioGraphNodeDelete")) {
                void api.deleteNode();
                return;
            }
            if (target.closest("[data-graph-node-add-edge]")) {
                void api.addGraphEdge();
                return;
            }
            const edit = target.closest<HTMLElement>("[data-graph-edge-edit]");
            if (edit) {
                api.selectEdge(edit.dataset.graphEdgeEdit || "");
                return;
            }
            const remove = target.closest<HTMLElement>("[data-graph-edge-delete]");
            if (remove) {
                const edgeId = remove.dataset.graphEdgeDelete || "";
                if (!edgeId) return;
                state.selectedEdgeId = edgeId;
                void api.deleteSelectedEdge(true);
            }
        });
        nodeEditor?.addEventListener("change", (event) => {
            const target = event.target as HTMLSelectElement;
            if (target.id === "scenarioGraphNodeVisibility") N.syncVisibilityFields("scenarioGraphNode", target.value);
            if (target.id === "scenarioGraphNodeRevealType") N.syncRevealType("scenarioGraphNode");
        });
        byId("scenarioGraphReclassify")?.addEventListener("click", () => {
            void api.reclassifyVisibility();
        });
        byId("scenarioGraphPlayerPreview")?.addEventListener("click", () => api.openPlayerPreview());
        byId("scenarioOpeningOpen")?.addEventListener("click", () => {
            void api.openOpeningEditor();
        });
        byId("scenarioOpeningSave")?.addEventListener("click", () => {
            void api.saveOpening();
        });
        byId("scenarioOpeningPreviewBtn")?.addEventListener("click", () => api.previewOpening());

        // §8.1.6：Esc 退出连线；Ctrl+Z / Ctrl+Y 撤销重做；Delete 删除选中连接。
        document.addEventListener(
            "keydown",
            (event) => {
                if (!byId("scenarioModal")?.classList.contains("show")) return;
                if (event.key === "Escape" && state.mode === "connect") {
                    event.preventDefault();
                    event.stopPropagation();
                    state.connectSource = "";
                    api.setMode("select");
                    return;
                }
                const typing = isTypingTarget(event.target);
                if ((event.ctrlKey || event.metaKey) && !typing) {
                    const key = event.key.toLowerCase();
                    if (key === "z") {
                        event.preventDefault();
                        void (event.shiftKey ? api.redoHistory() : api.undoHistory());
                        return;
                    }
                    if (key === "y") {
                        event.preventDefault();
                        void api.redoHistory();
                        return;
                    }
                }
                if ((event.key !== "Delete" && event.key !== "Backspace") || state.selectedEdgeId === "" || typing) return;
                event.preventDefault();
                void api.deleteSelectedEdge(true);
            },
            true,
        );
    }

    Object.assign(api, {
        connectNodes,
        handleNodeClick,
        toggleEdgeHard,
        deleteSelectedEdge,
        openNodeCreate,
        submitNodeCreate,
        deleteNode,
        reclassifyVisibility,
        bindGraphEvents,
    });
})(window);
