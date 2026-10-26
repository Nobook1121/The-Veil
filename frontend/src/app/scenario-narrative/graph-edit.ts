/**
 * 剧本世界树 / 图谱：画布交互、拖拽落盘、连线增删与快捷键（§8.1.6、§8.2）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, editable, isTypingTarget, nodeById, edgeById, VISIBILITY_LABEL } = N;

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
        const canvas = byId("scenarioGraphCanvas");
        canvas?.querySelectorAll<SVGLineElement>("[data-edge-from]").forEach((line) => {
            const fromId = line.dataset.edgeFrom || "";
            const toId = line.dataset.edgeTo || "";
            if (fromId === nodeId) {
                line.setAttribute("x1", String(x + 85));
                line.setAttribute("y1", String(y + 24));
            }
            if (toId === nodeId) {
                line.setAttribute("x2", String(x + 85));
                line.setAttribute("y2", String(y + 24));
            }
        });
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

    async function connectNodes(sourceId: string, targetId: string): Promise<void> {
        const type = (byId<HTMLSelectElement>("scenarioGraphEdgeType")?.value || "adjacent");
        const hard = byId<HTMLInputElement>("scenarioGraphEdgeHard")?.checked === true;
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
            api.renderGraph();
            showNotification(t("scenario.graph.edge_created", "连接已创建。"), "success");
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

    function bindGraphEvents(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;

        canvas.addEventListener("pointerdown", (event) => {
            const pointer = event as PointerEvent;
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
                }
                return;
            }
            if (state.mode === "pan") {
                state.pan = {
                    pointerId: pointer.pointerId,
                    startClientX: pointer.clientX,
                    startClientY: pointer.clientY,
                    originX: state.view.x,
                    originY: state.view.y,
                };
                canvas.setPointerCapture?.(pointer.pointerId);
                pointer.preventDefault();
            }
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
                state.view.x = state.pan.originX + (pointer.clientX - state.pan.startClientX) * scale;
                state.view.y = state.pan.originY + (pointer.clientY - state.pan.startClientY) * scale;
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
        });

        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.addEventListener("click", () => api.setMode((button.dataset.graphMode as GraphMode) || "select"));
        });
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.addEventListener("click", () => api.setViewMode(button.dataset.graphView === "player" ? "player" : "kp"));
        });
        byId("scenarioGraphFit")?.addEventListener("click", () => api.fitView());
        byId("scenarioGraphUndo")?.addEventListener("click", () => {
            void api.undoHistory();
        });
        byId("scenarioGraphRedo")?.addEventListener("click", () => {
            void api.redoHistory();
        });
        byId("scenarioGraphAutoLayout")?.addEventListener("click", () => {
            void api.autoLayout();
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
        reclassifyVisibility,
        bindGraphEvents,
    });
})(window);
