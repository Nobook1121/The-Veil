/**
 * 剧本世界树 / 图谱页签（设计文档 §8.1–§8.3、§8.7）。
 *
 * 世界树负责按 act → area → location → ending 展示结构并编辑节点；
 * 图谱提供选择 / 连线 / 平移缩放三种模式，拖拽节点会写回 `layout`。
 * 触发器与媒体编辑由 `scenario-triggers.ts` 负责，两者通过修订号同步。
 */
(function (window: Window): void {
    "use strict";

    interface GraphNode {
        id: string;
        kind: string;
        title: string;
        summary?: string;
        body?: string;
        scene_role?: string;
        access?: string;
        visibility?: string;
        repeatable?: boolean;
        parent?: string | null;
        parent_id?: string;
        keywords?: string[];
        kp_notes?: string;
        objects?: unknown[];
        layout?: { x?: number; y?: number };
        layout_locked?: boolean;
    }

    interface GraphEdge {
        id: string;
        from: string;
        to: string;
        type?: string;
        label?: string;
        visibility?: string;
        hard?: boolean;
        condition?: unknown;
    }

    interface GraphBundle {
        scenario_id?: string;
        scenario_version?: string;
        topology?: string;
        topology_confidence?: number;
        topology_source?: string;
        nodes: GraphNode[];
        edges: GraphEdge[];
    }

    interface NarrativePayload {
        bundle: GraphBundle;
        summary: { counts?: Record<string, number>; warnings?: number };
        revision: number;
        permissions: { can_view: boolean; can_edit: boolean; readonly: boolean };
    }

    interface ValidationIssue {
        code: string;
        level: string;
        message: string;
        node_id?: string | null;
        edge_id?: string | null;
        rule?: string;
        suggestion?: string;
    }

    interface ValidationPayload {
        topology: string;
        ok: boolean;
        error_count: number;
        warning_count: number;
        info_count: number;
        issues: ValidationIssue[];
        order_simulation?: { ok: boolean; issues: ValidationIssue[] };
        revision: number;
    }

    type GraphMode = "select" | "connect" | "pan";

    interface DragState {
        nodeId: string;
        pointerId: number;
        startWorldX: number;
        startWorldY: number;
        originX: number;
        originY: number;
        moved: boolean;
    }

    interface PanState {
        pointerId: number;
        startClientX: number;
        startClientY: number;
        originX: number;
        originY: number;
    }

    interface NarrativeState {
        scenarioId: number;
        bundle: GraphBundle | null;
        revision: number;
        readonly: boolean;
        canEdit: boolean;
        selectedId: string;
        selectedEdgeId: string;
        search: string;
        validation: ValidationPayload | null;
        mode: GraphMode;
        connectSource: string;
        positions: Map<string, { x: number; y: number }>;
        view: { x: number; y: number; k: number };
        drag: DragState | null;
        pan: PanState | null;
        draggingFrame: number | null;
    }

    const KIND_META: Record<string, { label: string; icon: string; color: string; order: number }> = {
        act: { label: "幕", icon: "fa-bookmark", color: "#4c6ef5", order: 0 },
        area: { label: "区域", icon: "fa-layer-group", color: "#15aabf", order: 1 },
        location: { label: "地点", icon: "fa-door-open", color: "#2f9e44", order: 2 },
        ending: { label: "结局", icon: "fa-flag-checkered", color: "#e8590c", order: 3 },
        npc: { label: "NPC", icon: "fa-user", color: "#7048e8", order: 4 },
        item: { label: "物件", icon: "fa-box", color: "#b8860b", order: 5 },
        clue: { label: "线索", icon: "fa-magnifying-glass", color: "#1c7ed6", order: 6 },
        concept: { label: "概念", icon: "fa-book", color: "#495057", order: 7 },
        interactable: { label: "交互", icon: "fa-hand-pointer", color: "#0c8599", order: 8 },
        flag: { label: "标记", icon: "fa-flag", color: "#868e96", order: 9 },
        clock: { label: "时钟", icon: "fa-clock", color: "#f08c00", order: 10 },
    };

    const EDGE_TYPES: Array<[string, string]> = [
        ["adjacent", "相邻"],
        ["portal", "通道"],
        ["next", "顺序"],
        ["leads_to", "通向"],
        ["requires", "需要"],
        ["reveals", "揭示"],
        ["knows", "知情"],
        ["inhabits", "居住"],
        ["uses", "使用"],
        ["contains", "包含"],
        ["triggers", "触发"],
        ["conflicts", "冲突"],
        ["mentions", "提及"],
        ["alias", "别名"],
    ];

    const VISIBILITY_LABEL: Record<string, string> = {
        public: "公开",
        hidden: "隐藏",
        kp_only: "仅 KP",
    };

    const state: NarrativeState = {
        scenarioId: 0,
        bundle: null,
        revision: 0,
        readonly: false,
        canEdit: false,
        selectedId: "",
        selectedEdgeId: "",
        search: "",
        validation: null,
        mode: "select",
        connectSource: "",
        positions: new Map(),
        view: { x: 40, y: 40, k: 1 },
        drag: null,
        pan: null,
        draggingFrame: null,
    };

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function escapeHtml(value: unknown): string {
        const entities: Record<string, string> = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            "\"": "&quot;",
            "'": "&#39;",
        };
        return String(value ?? "").replace(/[&<>"']/g, (char) => entities[char] || char);
    }

    function kindMeta(kind: string): { label: string; icon: string; color: string; order: number } {
        return KIND_META[kind] || { label: kind || "节点", icon: "fa-circle", color: "#868e96", order: 99 };
    }

    function editable(): boolean {
        return state.canEdit && !state.readonly;
    }

    function showTab(tab: "basic" | "modules" | "tree" | "graph" | "validate" | "generate"): void {
        const button = byId<HTMLButtonElement>(`scenarioTab${tab.charAt(0).toUpperCase()}${tab.slice(1)}Btn`);
        if (!button) return;
        const bootstrapApi = (window as unknown as { bootstrap?: { Tab?: { getOrCreateInstance: (element: Element) => { show: () => void } } } }).bootstrap;
        bootstrapApi?.Tab?.getOrCreateInstance(button).show();
    }

    function setStatus(text: string, variant: "secondary" | "success" | "warning" | "danger" = "secondary"): void {
        const badge = byId("scenarioTreeStatus");
        if (!badge) return;
        badge.textContent = text;
        badge.className = `badge bg-${variant}`;
    }

    function setEmpty(message: string): void {
        const list = byId("scenarioTreeList");
        if (list) list.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(message)}</p>`;
        const inspector = byId("scenarioNodeInspector");
        if (inspector) inspector.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.select_hint", "选择左侧节点查看详情。"))}</p>`;
        const graph = byId("scenarioGraphCanvas");
        if (graph) graph.innerHTML = `<p class="text-muted small p-3 mb-0">${escapeHtml(message)}</p>`;
        setGraphHint("");
        renderEdgeActions();
    }

    function nodeById(nodeId: string): GraphNode | null {
        return state.bundle?.nodes.find((node) => node.id === nodeId) || null;
    }

    function parentOf(node: GraphNode): string {
        return node.parent || node.parent_id || "";
    }

    function edgeById(edgeId: string): GraphEdge | null {
        return state.bundle?.edges.find((edge) => edge.id === edgeId) || null;
    }

    function matchesSearch(node: GraphNode): boolean {
        if (!state.search) return true;
        const haystack = [node.id, node.title, node.summary || "", ...(node.keywords || [])].join(" ").toLowerCase();
        return haystack.includes(state.search);
    }

    function sortNodes(nodes: GraphNode[]): GraphNode[] {
        return [...nodes].sort((left, right) => {
            const kindDiff = kindMeta(left.kind).order - kindMeta(right.kind).order;
            return kindDiff !== 0 ? kindDiff : left.title.localeCompare(right.title, "zh-Hans-CN");
        });
    }

    function emitSelection(node: GraphNode | null): void {
        document.dispatchEvent(new CustomEvent("scenario:node-selected", {
            detail: node ? { nodeId: node.id, title: node.title, kind: node.kind } : { nodeId: "", title: "", kind: "" },
        }));
    }

    function nodeRow(node: GraphNode): string {
        const meta = kindMeta(node.kind);
        const hidden = node.visibility === "kp_only";
        return `
            <div class="scenario-tree-node${state.selectedId === node.id ? " active" : ""}" data-node-id="${escapeHtml(node.id)}" role="treeitem">
                <button type="button" class="scenario-tree-node-button">
                    <i class="fa ${meta.icon}" aria-hidden="true" style="color:${meta.color}"></i>
                    <span class="scenario-tree-node-title">${escapeHtml(node.title || node.id)}</span>
                    <span class="scenario-tree-node-kind">${escapeHtml(meta.label)}</span>
                    ${hidden ? `<i class="fa fa-eye-slash" title="${escapeHtml(t("scenario.tree.kp_only", "仅 KP 可见"))}" aria-hidden="true"></i>` : ""}
                </button>
            </div>`;
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

    function renderInspector(): void {
        const inspector = byId("scenarioNodeInspector");
        if (!inspector) return;
        const node = nodeById(state.selectedId);
        if (!node) {
            inspector.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.select_hint", "选择左侧节点查看详情。"))}</p>`;
            return;
        }
        const meta = kindMeta(node.kind);
        const canEdit = editable();
        const edges = (state.bundle?.edges || []).filter((edge) => edge.from === node.id || edge.to === node.id);
        const visibility = node.visibility || "public";
        const rows: Array<[string, string]> = [
            ["ID", `<code>${escapeHtml(node.id)}</code>`],
            ["类型", escapeHtml(meta.label)],
            ["父节点", parentOf(node) ? escapeHtml(nodeById(parentOf(node))?.title || parentOf(node)) : "—"],
            ["场景角色", escapeHtml(node.scene_role || "—")],
            ["进入方式", escapeHtml(node.access || "—")],
            ["可重复", node.repeatable === false ? "否" : "是"],
            ["物件数", String(Array.isArray(node.objects) ? node.objects.length : 0)],
            ["关键词", (node.keywords || []).length ? node.keywords!.map((word) => `<span class="badge bg-light text-dark">${escapeHtml(word)}</span>`).join(" ") : "—"],
        ];
        if (!canEdit) {
            rows.splice(3, 0, [
                "可见性",
                `<span class="badge bg-${visibility === "public" ? "success" : visibility === "kp_only" ? "danger" : "warning"}">${escapeHtml(VISIBILITY_LABEL[visibility] || visibility)}</span>`,
            ]);
        }
        const editForm = canEdit ? `
            <div class="scenario-inspector-edit">
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_title", "标题"))}</span>
                    <input type="text" class="form-control form-control-sm" id="scenarioNodeTitle" value="${escapeHtml(node.title)}">
                </label>
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_visibility", "可见性"))}</span>
                    <select class="form-select form-select-sm" id="scenarioNodeVisibility">
                        ${(["public", "hidden", "kp_only"] as const).map((value) => `<option value="${value}"${visibility === value ? " selected" : ""}>${escapeHtml(VISIBILITY_LABEL[value])}</option>`).join("")}
                    </select>
                </label>
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_kp_notes", "KP 备注"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioNodeKpNotes" rows="3">${escapeHtml(node.kp_notes || "")}</textarea>
                </label>
                <label class="scenario-module-toggle">
                    <span>${escapeHtml(t("scenario.graph.lock_layout", "锁定布局"))}</span>
                    <input type="checkbox" id="scenarioNodeLayoutLocked" ${node.layout_locked ? "checked" : ""}>
                </label>
                <button type="button" class="btn btn-primary btn-sm" id="scenarioNodeSave">
                    <i class="fa fa-save" aria-hidden="true"></i>
                    <span>${escapeHtml(t("scenario.tree.save_node", "保存节点"))}</span>
                </button>
            </div>` : "";
        inspector.innerHTML = `
            <h6 class="mb-2"><i class="fa ${meta.icon}" style="color:${meta.color}" aria-hidden="true"></i> ${escapeHtml(node.title || node.id)}</h6>
            ${node.summary ? `<p class="text-muted small">${escapeHtml(node.summary)}</p>` : ""}
            ${editForm}
            <div class="scenario-inspector-rows">${rows.map(([label, value]) => `<div class="scenario-inspector-row"><span>${escapeHtml(label)}</span><div>${value}</div></div>`).join("")}</div>
            ${node.body ? `<div class="scenario-inspector-body"><h6>${escapeHtml(t("scenario.tree.body", "正文"))}</h6><div class="scenario-inspector-pre">${escapeHtml(node.body)}</div></div>` : ""}
            ${!canEdit && node.kp_notes ? `<div class="scenario-inspector-kp"><h6>${escapeHtml(t("scenario.tree.kp_notes", "KP 备注"))}</h6><div>${escapeHtml(node.kp_notes)}</div></div>` : ""}
            <div class="scenario-inspector-edges">
                <h6>${escapeHtml(t("scenario.tree.edges", "连接"))}</h6>
                ${edges.length ? `<ul class="list-unstyled small mb-0">${edges.map((edge) => {
                    const otherId = edge.from === node.id ? edge.to : edge.from;
                    const other = nodeById(otherId);
                    const arrow = edge.from === node.id ? "→" : "←";
                    const hard = edge.hard ? ` <span class="badge bg-danger">hard</span>` : "";
                    return `<li><code>${arrow}</code> ${escapeHtml(other?.title || otherId)} <span class="text-muted">${escapeHtml(edge.type || "")}</span>${hard}</li>`;
                }).join("")}</ul>` : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.no_edges", "暂无连接。"))}</p>`}
            </div>`;
    }

    function selectNode(nodeId: string): void {
        state.selectedId = nodeId;
        state.selectedEdgeId = "";
        renderTree();
        renderInspector();
        renderGraph();
        emitSelection(nodeById(nodeId));
    }

    async function handleConflict(): Promise<void> {
        showNotification(t("scenario.tree.conflict", "内容已被其他编辑修改，正在刷新…"), "warning");
        await load(state.scenarioId, { readonly: state.readonly });
    }

    async function saveNode(): Promise<void> {
        const node = nodeById(state.selectedId);
        if (!node || !state.scenarioId || !editable()) return;
        const title = byId<HTMLInputElement>("scenarioNodeTitle")?.value.trim() || "";
        const visibility = byId<HTMLSelectElement>("scenarioNodeVisibility")?.value || "public";
        const kpNotes = byId<HTMLTextAreaElement>("scenarioNodeKpNotes")?.value.trim() || "";
        const layoutLocked = byId<HTMLInputElement>("scenarioNodeLayoutLocked")?.checked === true;
        if (!title) {
            showNotification(t("scenario.tree.title_required", "节点标题不能为空。"), "warning");
            return;
        }
        const button = byId<HTMLButtonElement>("scenarioNodeSave");
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(node.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, title, visibility, kp_notes: kpNotes, layout_locked: layoutLocked } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            const saved = data.data.node;
            if (state.bundle) {
                state.bundle.nodes = state.bundle.nodes.map((item) => (item.id === saved.id ? { ...item, ...saved } : item));
            }
            renderAll();
            showNotification(t("scenario.tree.saved", "节点已保存。"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function applyRevision(revision: number): void {
        state.revision = revision;
        window.ScenarioTriggers?.syncRevision(revision);
        updateStatusBadge();
    }

    function syncRevision(revision: number): void {
        state.revision = revision;
        updateStatusBadge();
    }

    function updateStatusBadge(): void {
        if (!state.bundle) return;
        const counts = state.bundle.nodes.length;
        setStatus(
            `${t("scenario.tree.revision", "修订")} ${state.revision} · ${counts} ${t("scenario.tree.nodes", "节点")} · ${state.bundle.topology || "hybrid"}`,
            "success",
        );
    }

    // ------------------------------------------------------------------
    // 图谱
    // ------------------------------------------------------------------

    function defaultPositions(bundle: GraphBundle): Map<string, { x: number; y: number }> {
        const positions = new Map<string, { x: number; y: number }>();
        const counters = new Map<number, number>();
        for (const node of sortNodes(bundle.nodes)) {
            const layout = node.layout || {};
            if (typeof layout.x === "number" && typeof layout.y === "number") {
                positions.set(node.id, { x: layout.x, y: layout.y });
                continue;
            }
            const level = kindMeta(node.kind).order <= 3 ? kindMeta(node.kind).order : 2;
            const index = counters.get(level) || 0;
            counters.set(level, index + 1);
            positions.set(node.id, { x: 60 + level * 240, y: 60 + index * 100 });
        }
        return positions;
    }

    function viewportSize(): { width: number; height: number } {
        const canvas = byId("scenarioGraphCanvas");
        const width = canvas?.clientWidth || 900;
        const height = canvas?.clientHeight || 520;
        return { width: Math.max(320, width), height: Math.max(320, height) };
    }

    function applyView(): void {
        const viewport = byId("scenarioGraphViewport");
        if (!viewport) return;
        viewport.setAttribute("transform", `translate(${state.view.x},${state.view.y}) scale(${state.view.k})`);
    }

    function fitView(): void {
        if (state.positions.size === 0) return;
        const values = [...state.positions.values()];
        const minX = Math.min(...values.map((item) => item.x));
        const minY = Math.min(...values.map((item) => item.y));
        const maxX = Math.max(...values.map((item) => item.x)) + 180;
        const maxY = Math.max(...values.map((item) => item.y)) + 70;
        const { width, height } = viewportSize();
        const k = Math.max(0.25, Math.min(1.4, Math.min(width / (maxX - minX), height / (maxY - minY))));
        state.view = {
            x: (width - (maxX - minX) * k) / 2 - minX * k,
            y: (height - (maxY - minY) * k) / 2 - minY * k,
            k,
        };
        applyView();
    }

    function setGraphHint(text: string): void {
        const hint = byId("scenarioGraphHint");
        if (hint) hint.textContent = text;
    }

    function modeHint(mode: GraphMode): string {
        if (mode === "connect") {
            return t("scenario.graph.hint_connect", "连线：依次点击起点与终点节点。");
        }
        if (mode === "pan") {
            return t("scenario.graph.hint_pan", "平移：拖拽画布移动，滚轮缩放。");
        }
        return t("scenario.graph.hint_select", "选择：点击节点查看详情，拖拽节点调整布局。");
    }

    function edgeMarkup(edge: GraphEdge, positions: Map<string, { x: number; y: number }>): string {
        const from = positions.get(edge.from);
        const to = positions.get(edge.to);
        if (!from || !to) return "";
        const dashed = edge.hard ? "" : ` stroke-dasharray="6 4"`;
        const color = edge.hard ? "#c0392b" : "#8a94a6";
        const active = state.selectedEdgeId === edge.id;
        return `<line class="scenario-graph-edge${active ? " active" : ""}" data-edge-id="${escapeHtml(edge.id)}" data-edge-from="${escapeHtml(edge.from)}" data-edge-to="${escapeHtml(edge.to)}" x1="${from.x + 85}" y1="${from.y + 24}" x2="${to.x + 85}" y2="${to.y + 24}" stroke="${active ? "#f08c00" : color}" stroke-width="${active ? 2.6 : 1.6}"${dashed}><title>${escapeHtml(edge.label || edge.type || "")}</title></line>`;
    }

    function nodeMarkup(node: GraphNode, positions: Map<string, { x: number; y: number }>): string {
        const position = positions.get(node.id);
        if (!position) return "";
        const meta = kindMeta(node.kind);
        const selected = state.selectedId === node.id;
        const connectSource = state.connectSource === node.id;
        const title = node.title.length > 10 ? `${node.title.slice(0, 10)}…` : node.title;
        const stroke = connectSource ? "#f08c00" : meta.color;
        const fill = selected ? meta.color : "#ffffff";
        const textColor = selected ? "#ffffff" : "#212529";
        return `<g class="scenario-graph-node${selected ? " active" : ""}${connectSource ? " source" : ""}" data-node-id="${escapeHtml(node.id)}" data-x="${position.x}" data-y="${position.y}" transform="translate(${position.x},${position.y})">
            <rect width="170" height="48" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="${connectSource ? 3 : 1.6}"></rect>
            <text x="12" y="21" font-size="12" fill="${textColor}">${escapeHtml(title || node.id)}</text>
            <text x="12" y="37" font-size="10" fill="${selected ? "#ffffff" : meta.color}">${escapeHtml(meta.label)}</text>
            ${node.layout_locked ? `<text x="154" y="14" font-size="10" fill="${selected ? "#ffffff" : "#868e96"}">🔒</text>` : ""}
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
        state.positions = defaultPositions(bundle);
        const positions = state.positions;
        const { width, height } = viewportSize();
        const edges = bundle.edges.map((edge) => edgeMarkup(edge, positions)).join("");
        const nodes = bundle.nodes.map((node) => nodeMarkup(node, positions)).join("");
        canvas.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="世界树图谱"><g id="scenarioGraphViewport">${edges}${nodes}</g></svg>`;
        applyView();
        setGraphHint(modeHint(state.mode));
        renderEdgeActions();
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
        container.innerHTML = `
            <span class="small"><code>${escapeHtml(owner?.title || edge.from)}</code> → <code>${escapeHtml(other?.title || edge.to)}</code> <span class="text-muted">${escapeHtml(edge.type || "")}</span></span>
            <button type="button" class="btn btn-sm btn-outline-${edge.hard ? "secondary" : "danger"}" data-edge-toggle>${escapeHtml(edge.hard ? t("scenario.graph.make_soft", "改为 soft") : t("scenario.graph.make_hard", "改为 hard"))}</button>
            <button type="button" class="btn btn-sm btn-outline-danger" data-edge-delete>${escapeHtml(t("scenario.graph.delete_edge", "删除边"))}</button>`;
    }

    function setMode(mode: GraphMode): void {
        state.mode = mode;
        state.connectSource = "";
        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphMode === mode);
        });
        setGraphHint(modeHint(mode));
        renderGraph();
    }

    function clientToWorld(clientX: number, clientY: number): { x: number; y: number } {
        const canvas = byId("scenarioGraphCanvas");
        const svg = canvas?.querySelector<SVGSVGElement>("svg");
        if (!svg) return { x: 0, y: 0 };
        const rect = svg.getBoundingClientRect();
        const { width } = viewportSize();
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
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(nodeId)}`,
                { method: "PATCH", body: { base_revision: state.revision, layout: { x: position.x, y: position.y } } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            const saved = data.data.node;
            if (state.bundle) {
                state.bundle.nodes = state.bundle.nodes.map((item) => (item.id === saved.id ? { ...item, ...saved } : item));
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function connectNodes(sourceId: string, targetId: string): Promise<void> {
        const type = (byId<HTMLSelectElement>("scenarioGraphEdgeType")?.value || "adjacent");
        const hard = byId<HTMLInputElement>("scenarioGraphEdgeHard")?.checked === true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges`,
                { method: "POST", body: { base_revision: state.revision, from: sourceId, to: targetId, type, hard, source: "author" } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    showNotification(data?.message || t("scenario.graph.duplicate_edge", "这条连接已存在。"), "warning");
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            state.bundle?.edges.push(data.data.edge);
            state.connectSource = "";
            renderGraph();
            showNotification(t("scenario.graph.edge_created", "连接已创建。"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function handleNodeClick(nodeId: string): Promise<void> {
        if (state.mode === "connect" && editable()) {
            if (!state.connectSource) {
                state.connectSource = nodeId;
                renderGraph();
                return;
            }
            if (state.connectSource === nodeId) {
                state.connectSource = "";
                renderGraph();
                return;
            }
            const source = state.connectSource;
            state.connectSource = "";
            await connectNodes(source, nodeId);
            return;
        }
        selectNode(nodeId);
    }

    async function toggleEdgeHard(): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable()) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, hard: !edge.hard } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            if (state.bundle) {
                state.bundle.edges = state.bundle.edges.map((item) => (item.id === data.data!.edge.id ? data.data!.edge : item));
            }
            renderGraph();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function deleteSelectedEdge(): Promise<void> {
        const edge = edgeById(state.selectedEdgeId);
        if (!edge || !editable()) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "DELETE", body: { base_revision: state.revision } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            if (state.bundle) state.bundle.edges = state.bundle.edges.filter((item) => item.id !== edge.id);
            state.selectedEdgeId = "";
            renderGraph();
            renderInspector();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    function hierarchyLayout(bundle: GraphBundle): Map<string, { x: number; y: number }> {
        const byIdMap = new Map(bundle.nodes.map((node) => [node.id, node]));
        const depthCache = new Map<string, number>();
        const depthOf = (node: GraphNode, guard = 0): number => {
            if (depthCache.has(node.id)) return depthCache.get(node.id)!;
            const parentKey = parentOf(node);
            const parent = parentKey ? byIdMap.get(parentKey) : undefined;
            const value = !parent || guard > 8 ? 0 : depthOf(parent, guard + 1) + 1;
            depthCache.set(node.id, value);
            return value;
        };
        const counters = new Map<number, number>();
        const positions = new Map<string, { x: number; y: number }>();
        for (const node of sortNodes(bundle.nodes)) {
            if (node.layout_locked && typeof node.layout?.x === "number" && typeof node.layout?.y === "number") {
                positions.set(node.id, { x: node.layout.x, y: node.layout.y });
                continue;
            }
            const depth = Math.min(6, depthOf(node));
            const row = counters.get(depth) || 0;
            counters.set(depth, row + 1);
            positions.set(node.id, { x: 60 + depth * 250, y: 60 + row * 100 });
        }
        return positions;
    }

    async function autoLayout(): Promise<void> {
        const bundle = state.bundle;
        if (!bundle || !editable() || !state.scenarioId) return;
        const positions = hierarchyLayout(bundle);
        const nodes = bundle.nodes.map((node) => {
            const position = positions.get(node.id);
            return position ? { ...node, layout: { x: position.x, y: position.y } } : node;
        });
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: state.revision, bundle: { ...bundle, nodes } } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            applyRevision(data.data.revision);
            renderAll();
            fitView();
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
                if (state.mode === "select" && editable() && node && !node.layout_locked) {
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
                const scale = viewportSize().width / (canvasRect.width || 1);
                state.view.x = state.pan.originX + (pointer.clientX - state.pan.startClientX) * scale;
                state.view.y = state.pan.originY + (pointer.clientY - state.pan.startClientY) * scale;
                applyView();
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
            const scale = viewportSize().width / (canvasRect.width || 1);
            const localX = (wheel.clientX - canvasRect.left) * scale;
            const localY = (wheel.clientY - canvasRect.top) * scale;
            state.view.k = nextK;
            state.view.x = localX - before.x * nextK;
            state.view.y = localY - before.y * nextK;
            applyView();
        }, { passive: false });

        canvas.addEventListener("click", (event) => {
            const target = event.target as Element;
            const edgeTarget = target.closest?.<SVGLineElement>("[data-edge-id]");
            if (edgeTarget) {
                state.selectedEdgeId = edgeTarget.getAttribute("data-edge-id") || "";
                state.selectedId = "";
                renderGraph();
                renderInspector();
                emitSelection(null);
                return;
            }
            if (!target.closest?.("[data-node-id]")) {
                state.selectedEdgeId = "";
                renderEdgeActions();
            }
        });

        byId("scenarioGraphEdgeActions")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("[data-edge-toggle]")) {
                void toggleEdgeHard();
                return;
            }
            if (target.closest("[data-edge-delete]")) {
                void deleteSelectedEdge();
            }
        });

        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.addEventListener("click", () => setMode((button.dataset.graphMode as GraphMode) || "select"));
        });
        byId("scenarioGraphFit")?.addEventListener("click", () => fitView());
        byId("scenarioGraphAutoLayout")?.addEventListener("click", () => {
            void autoLayout();
        });
    }

    function renderAll(): void {
        renderTree();
        renderInspector();
        renderGraph();
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
        state.mode = "select";
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
            renderAll();
            window.requestAnimationFrame(() => fitView());
            window.ScenarioTriggers?.syncRevision(state.revision);
            emitSelection(null);
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
        state.positions = new Map();
        state.drag = null;
        state.pan = null;
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
        setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
        emitSelection(null);
        showTab("basic");
    }

    function renderValidation(payload: ValidationPayload): void {
        const results = byId("scenarioValidateResults");
        const summary = byId("scenarioValidateSummary");
        if (summary) {
            summary.textContent = `${payload.ok ? t("scenario.validate.ok", "通过") : t("scenario.validate.failed", "存在问题")} · ${payload.error_count} error · ${payload.warning_count} warn`;
        }
        if (!results) return;
        const issues = payload.issues || [];
        if (issues.length === 0) {
            results.innerHTML = `<p class="text-success small mb-0">${escapeHtml(t("scenario.validate.clean", "未发现问题。"))}</p>`;
            return;
        }
        const levelVariant: Record<string, string> = { error: "danger", warn: "warning", info: "info" };
        const items = issues.slice();
        if (payload.order_simulation && !payload.order_simulation.ok) {
            items.push(...payload.order_simulation.issues.filter((issue) => issue.level === "error"));
        }
        results.innerHTML = items.map((issue) => `
            <div class="scenario-validate-item scenario-validate-${escapeHtml(levelVariant[issue.level] || "info")}">
                <div class="scenario-validate-head">
                    <span class="badge bg-${escapeHtml(levelVariant[issue.level] || "info")}">${escapeHtml(issue.level)}</span>
                    <code>${escapeHtml(issue.code)}</code>
                    ${issue.node_id ? `<button type="button" class="btn btn-link btn-sm p-0 scenario-validate-jump" data-node-id="${escapeHtml(issue.node_id)}">${escapeHtml(nodeById(issue.node_id)?.title || issue.node_id)}</button>` : ""}
                </div>
                <div class="small">${escapeHtml(issue.message)}</div>
                ${issue.suggestion ? `<div class="small text-muted">${escapeHtml(issue.suggestion)}</div>` : ""}
            </div>`).join("");
    }

    async function runValidation(): Promise<void> {
        if (!state.scenarioId) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        const button = byId<HTMLButtonElement>("scenarioValidateRun");
        const allOrders = byId<HTMLInputElement>("scenarioValidateAllOrders")?.checked === true;
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ValidationPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/validate`,
                { method: "POST", body: { all_orders: allOrders } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.validation = data.data;
            renderValidation(data.data);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function init(): void {
        byId("scenarioTreeSearch")?.addEventListener("input", (event) => {
            state.search = (event.target as HTMLInputElement).value.trim().toLowerCase();
            renderTree();
        });
        byId("scenarioTreeList")?.addEventListener("click", (event) => {
            const target = (event.target as HTMLElement).closest<HTMLElement>("[data-node-id]");
            if (target?.dataset.nodeId) selectNode(target.dataset.nodeId);
        });
        byId("scenarioValidateResults")?.addEventListener("click", (event) => {
            const target = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-jump");
            if (target?.dataset.nodeId) selectNode(target.dataset.nodeId);
        });
        byId("scenarioValidateRun")?.addEventListener("click", () => {
            void runValidation();
        });
        byId("scenarioNodeInspector")?.addEventListener("click", (event) => {
            if ((event.target as HTMLElement).closest("#scenarioNodeSave")) {
                void saveNode();
            }
        });
        bindGraphEvents();
    }

    window.ScenarioNarrative = { load, clear, init, syncRevision };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
