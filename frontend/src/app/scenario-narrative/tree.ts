/**
 * 剧本世界树 / 图谱：世界树渲染、节点选中与拖拽 / 键盘编辑（§8.1.4）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, runtime, byId, t, escapeHtml, editable, kindMeta, sortNodes, nodeById, parentOf, matchesSearch, setEmpty, emitSelection } = N;

    function nodeRow(node: GraphNode): string {
        const meta = kindMeta(node.kind);
        const hidden = node.visibility === "kp_only";
        return `
            <div class="scenario-tree-node${state.selectedId === node.id ? " active" : ""}" data-node-id="${escapeHtml(node.id)}" role="treeitem" tabindex="0" draggable="true">
                <button type="button" class="scenario-tree-node-button">
                    <i class="fa ${meta.icon}" aria-hidden="true" style="color:${meta.color}"></i>
                    <span class="scenario-tree-node-title">${escapeHtml(node.title || node.id)}</span>
                    <span class="scenario-tree-node-kind">${escapeHtml(meta.label)}</span>
                    ${hidden ? `<i class="fa fa-eye-slash" title="${escapeHtml(t("scenario.tree.kp_only", "仅 KP 可见"))}" aria-hidden="true"></i>` : ""}
                </button>
            </div>`;
    }

    /** 判断 nodeId 是否为 ancestorId 的后代（拖拽合法性检查）。 */
    function isDescendant(nodeId: string, ancestorId: string): boolean {
        let current = nodeId;
        const guard = new Set<string>();
        while (current && !guard.has(current)) {
            guard.add(current);
            const node = nodeById(current);
            const parent = node ? parentOf(node) : "";
            if (parent === ancestorId) return true;
            current = parent;
        }
        return false;
    }

    function nextSiblingOrder(parentId: string): number {
        const siblings = (state.bundle?.nodes || []).filter((node) => parentOf(node) === parentId);
        return siblings.reduce((max, node) => Math.max(max, Number(node.order || 0)), 0) + 1;
    }

    /** 树拖拽落点：改 parent/order，非法落点（自身或后代）直接拒绝（§8.1.4）。 */
    async function moveNode(nodeId: string, parentId: string): Promise<void> {
        if (!editable() || !state.scenarioId || !state.bundle) return;
        if (parentId && (parentId === nodeId || isDescendant(parentId, nodeId))) {
            showNotification(t("scenario.tree.move_invalid", "不能把节点移动到自身或自己的子节点下。"), "warning");
            return;
        }
        const history = api.pushHistory();
        N.markDirty(true);
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(nodeId)}`,
                { method: "PATCH", body: { base_revision: state.revision, parent: parentId || null, order: nextSiblingOrder(parentId) } },
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
            state.bundle.nodes = state.bundle.nodes.map((item) => (item.id === saved.id ? { ...item, ...saved } : item));
            N.markDirty(false);
            api.renderAll();
        } catch (error) {
            api.discardHistory(history);
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function renderTree(): void {
        const list = byId("scenarioTreeList");
        if (!list) return;
        const bundle = state.bundle;
        if (!bundle || bundle.nodes.length === 0) {
            setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
            return;
        }
        if (state.search) {
            const matched = sortNodes(bundle.nodes.filter(matchesSearch));
            list.innerHTML = matched.length
                ? matched.map(nodeRow).join("")
                : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.no_match", "没有匹配的节点。"))}</p>`;
            return;
        }

        const byIdMap = new Map(bundle.nodes.map((node) => [node.id, node]));
        const children = new Map<string, GraphNode[]>();
        const roots: GraphNode[] = [];
        for (const node of bundle.nodes) {
            const parentKey = parentOf(node);
            const parent = parentKey && byIdMap.has(parentKey) ? parentKey : "";
            if (!parent) {
                roots.push(node);
                continue;
            }
            const bucket = children.get(parent) || [];
            bucket.push(node);
            children.set(parent, bucket);
        }

        const renderBranch = (node: GraphNode): string => {
            const kids = sortNodes(children.get(node.id) || []);
            const nested = kids.length
                ? `<div class="scenario-tree-children">${kids.map((child) => renderBranch(child)).join("")}</div>`
                : "";
            return `${nodeRow(node)}${nested}`;
        };

        list.innerHTML = sortNodes(roots).map((node) => renderBranch(node)).join("");
    }

    function selectNode(nodeId: string): void {
        state.selectedId = nodeId;
        state.selectedEdgeId = "";
        runtime.keywordCandidates = null;
        api.renderTree();
        api.renderInspector();
        api.renderGraph();
        api.renderGraphEditor();
        emitSelection(nodeById(nodeId));
    }

    /** 世界树拖拽改父子/排序 + 键盘导航（§8.1.4）。 */
    function bindTreeEditing(): void {
        const list = byId("scenarioTreeList");
        if (!list) return;
        let dragNodeId = "";

        const rowOf = (target: EventTarget | null): HTMLElement | null =>
            (target as HTMLElement | null)?.closest?.<HTMLElement>(".scenario-tree-node") || null;

        const clearInvalid = (): void => {
            list.querySelectorAll<HTMLElement>(".drag-invalid").forEach((item) => item.classList.remove("drag-invalid"));
        };

        list.addEventListener("dragstart", (event) => {
            const row = rowOf(event.target);
            dragNodeId = row?.dataset.nodeId || "";
            if (!dragNodeId || !event.dataTransfer) return;
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", dragNodeId);
            row?.classList.add("dragging");
        });

        list.addEventListener("dragend", () => {
            dragNodeId = "";
            clearInvalid();
            list.querySelectorAll<HTMLElement>(".dragging").forEach((item) => item.classList.remove("dragging"));
        });

        list.addEventListener("dragover", (event) => {
            if (!dragNodeId) return;
            event.preventDefault();
            const row = rowOf(event.target);
            const targetId = row?.dataset.nodeId || "";
            const invalid = Boolean(targetId) && (targetId === dragNodeId || isDescendant(targetId, dragNodeId));
            clearInvalid();
            if (row && invalid) row.classList.add("drag-invalid");
            if (event.dataTransfer) event.dataTransfer.dropEffect = invalid ? "none" : "move";
        });

        list.addEventListener("drop", (event) => {
            event.preventDefault();
            const row = rowOf(event.target);
            const targetId = row?.dataset.nodeId || "";
            const sourceId = dragNodeId || event.dataTransfer?.getData("text/plain") || "";
            dragNodeId = "";
            clearInvalid();
            list.querySelectorAll<HTMLElement>(".dragging").forEach((item) => item.classList.remove("dragging"));
            if (!sourceId || !editable()) return;
            if (targetId && (targetId === sourceId || isDescendant(targetId, sourceId))) {
                showNotification(t("scenario.tree.move_invalid", "不能把节点移动到自身或自己的子节点下。"), "warning");
                return;
            }
            void moveNode(sourceId, targetId);
        });

        list.addEventListener("keydown", (event) => {
            const row = rowOf(event.target);
            if (!row) return;
            const nodeId = row.dataset.nodeId || "";
            if (!nodeId) return;
            const rows = Array.from(list.querySelectorAll<HTMLElement>(".scenario-tree-node"));
            const index = rows.indexOf(row);
            const focusRow = (target: HTMLElement | null | undefined): void => {
                if (!target) return;
                const id = target.dataset.nodeId || "";
                if (id) selectNode(id);
                // selectNode 会重建树 DOM，必须重新取一次行再聚焦。
                const fresh = id
                    ? Array.from(list.querySelectorAll<HTMLElement>(".scenario-tree-node")).find((item) => item.dataset.nodeId === id)
                    : null;
                (fresh || target).focus();
            };
            if (event.key === "ArrowDown") {
                event.preventDefault();
                focusRow(rows[Math.min(rows.length - 1, index + 1)]);
                return;
            }
            if (event.key === "ArrowUp") {
                event.preventDefault();
                focusRow(rows[Math.max(0, index - 1)]);
                return;
            }
            if (event.key === "ArrowRight") {
                event.preventDefault();
                const next = row.nextElementSibling;
                const child = next && next.classList.contains("scenario-tree-children")
                    ? next.querySelector<HTMLElement>(".scenario-tree-node")
                    : null;
                focusRow(child);
                return;
            }
            if (event.key === "ArrowLeft") {
                event.preventDefault();
                const current = nodeById(nodeId);
                const parentId = current ? parentOf(current) : "";
                const parentRow = parentId
                    ? rows.find((item) => item.dataset.nodeId === parentId)
                    : null;
                focusRow(parentRow);
                return;
            }
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            if (state.mode === "connect" && editable()) {
                if (!state.connectSource) {
                    state.connectSource = nodeId;
                    showNotification(t("scenario.graph.connect_from", "已选择起点，请再选择终点。"), "info");
                } else if (state.connectSource !== nodeId) {
                    const source = state.connectSource;
                    state.connectSource = "";
                    void api.connectNodes(source, nodeId);
                }
                return;
            }
            selectNode(nodeId);
        });
    }

    Object.assign(api, {
        renderTree,
        selectNode,
        moveNode,
        bindTreeEditing,
    });
})(window);
