/**
 * 剧本世界树 / 图谱页签（设计文档 §8.1–§8.2）。
 *
 * 只读展示：从 `/api/scenarios/<id>/narrative` 读取 story graph，按
 * act → area → location → ending 渲染树，并用确定性坐标画只读图谱。
 * 编辑、拖拽与生成向导在后续步骤接入。
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
        parent_id?: string;
        keywords?: string[];
        kp_notes?: string;
        objects?: unknown[];
        layout?: { x?: number; y?: number };
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

    interface NarrativeState {
        scenarioId: number;
        bundle: GraphBundle | null;
        revision: number;
        readonly: boolean;
        selectedId: string;
        search: string;
        validation: ValidationPayload | null;
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
        selectedId: "",
        search: "",
        validation: null,
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
    }

    function nodeById(nodeId: string): GraphNode | null {
        return state.bundle?.nodes.find((node) => node.id === nodeId) || null;
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
            const parent = node.parent_id && byIdMap.has(node.parent_id) ? node.parent_id : "";
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
        const edges = (state.bundle?.edges || []).filter((edge) => edge.from === node.id || edge.to === node.id);
        const rows: Array<[string, string]> = [
            ["ID", `<code>${escapeHtml(node.id)}</code>`],
            ["类型", escapeHtml(meta.label)],
            ["父节点", node.parent_id ? escapeHtml(nodeById(node.parent_id)?.title || node.parent_id) : "—"],
            ["可见性", `<span class="badge bg-${node.visibility === "public" ? "success" : node.visibility === "kp_only" ? "danger" : "warning"}">${escapeHtml(VISIBILITY_LABEL[node.visibility || "public"] || node.visibility || "公开")}</span>`],
            ["场景角色", escapeHtml(node.scene_role || "—")],
            ["进入方式", escapeHtml(node.access || "—")],
            ["可重复", node.repeatable === false ? "否" : "是"],
            ["物件数", String(Array.isArray(node.objects) ? node.objects.length : 0)],
            ["关键词", (node.keywords || []).length ? node.keywords!.map((word) => `<span class="badge bg-light text-dark">${escapeHtml(word)}</span>`).join(" ") : "—"],
        ];
        inspector.innerHTML = `
            <h6 class="mb-2"><i class="fa ${meta.icon}" style="color:${meta.color}" aria-hidden="true"></i> ${escapeHtml(node.title || node.id)}</h6>
            ${node.summary ? `<p class="text-muted small">${escapeHtml(node.summary)}</p>` : ""}
            <div class="scenario-inspector-rows">${rows.map(([label, value]) => `<div class="scenario-inspector-row"><span>${escapeHtml(label)}</span><div>${value}</div></div>`).join("")}</div>
            ${node.body ? `<div class="scenario-inspector-body"><h6>${escapeHtml(t("scenario.tree.body", "正文"))}</h6><div class="scenario-inspector-pre">${escapeHtml(node.body)}</div></div>` : ""}
            ${node.kp_notes ? `<div class="scenario-inspector-kp"><h6>${escapeHtml(t("scenario.tree.kp_notes", "KP 备注"))}</h6><div>${escapeHtml(node.kp_notes)}</div></div>` : ""}
            <div class="scenario-inspector-edges">
                <h6>${escapeHtml(t("scenario.tree.edges", "连接"))}</h6>
                ${edges.length ? `<ul class="list-unstyled small mb-0">${edges.map((edge) => {
                    const otherId = edge.from === node.id ? edge.to : edge.from;
                    const other = nodeById(otherId);
                    const arrow = edge.from === node.id ? "→" : "←";
                    return `<li><code>${arrow}</code> ${escapeHtml(other?.title || otherId)} <span class="text-muted">${escapeHtml(edge.type || "")}</span></li>`;
                }).join("")}</ul>` : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.no_edges", "暂无连接。"))}</p>`}
            </div>`;
    }

    function selectNode(nodeId: string): void {
        state.selectedId = nodeId;
        renderTree();
        renderInspector();
    }

    function nodePositions(bundle: GraphBundle): Map<string, { x: number; y: number }> {
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
            positions.set(node.id, { x: 60 + level * 240, y: 50 + index * 110 });
        }
        return positions;
    }

    function renderGraph(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;
        const bundle = state.bundle;
        if (!bundle || bundle.nodes.length === 0) {
            setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
            return;
        }
        const positions = nodePositions(bundle);
        const values = [...positions.values()];
        const minX = Math.min(...values.map((item) => item.x)) - 40;
        const minY = Math.min(...values.map((item) => item.y)) - 40;
        const maxX = Math.max(...values.map((item) => item.x)) + 220;
        const maxY = Math.max(...values.map((item) => item.y)) + 100;

        const edgeMarkup = bundle.edges.map((edge) => {
            const from = positions.get(edge.from);
            const to = positions.get(edge.to);
            if (!from || !to) return "";
            const dashed = edge.hard ? "" : ` stroke-dasharray="6 4"`;
            const color = edge.hard ? "#c0392b" : "#8a94a6";
            return `<line class="scenario-graph-edge" x1="${from.x + 85}" y1="${from.y + 24}" x2="${to.x + 85}" y2="${to.y + 24}" stroke="${color}" stroke-width="1.6"${dashed}><title>${escapeHtml(edge.label || edge.type || "")}</title></line>`;
        }).join("");

        const nodeMarkup = bundle.nodes.map((node) => {
            const position = positions.get(node.id)!;
            const meta = kindMeta(node.kind);
            const selected = state.selectedId === node.id;
            const title = node.title.length > 10 ? `${node.title.slice(0, 10)}…` : node.title;
            return `<g class="scenario-graph-node${selected ? " active" : ""}" data-node-id="${escapeHtml(node.id)}" transform="translate(${position.x},${position.y})">
                <rect width="170" height="48" rx="8" fill="${selected ? meta.color : "#ffffff"}" stroke="${meta.color}" stroke-width="1.6"></rect>
                <text x="12" y="21" font-size="12" fill="${selected ? "#ffffff" : "#212529"}">${escapeHtml(title || node.id)}</text>
                <text x="12" y="37" font-size="10" fill="${selected ? "#ffffff" : meta.color}">${escapeHtml(meta.label)}</text>
            </g>`;
        }).join("");

        canvas.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${minX} ${minY} ${maxX - minX} ${maxY - minY}" width="${Math.max(720, maxX - minX)}" height="${Math.max(360, maxY - minY)}" role="img" aria-label="世界树只读图谱">${edgeMarkup}${nodeMarkup}</svg>`;
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
        state.selectedId = "";
        state.search = "";
        state.validation = null;
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
        const summary = byId("scenarioValidateSummary");
        if (summary) summary.textContent = "";

        if (!state.scenarioId) {
            setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
            setEmpty(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"));
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
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            state.revision = data.data.revision;
            state.readonly = Boolean(data.data.permissions?.readonly);
            const counts = data.data.summary?.counts || {};
            setStatus(
                `${t("scenario.tree.revision", "修订")} ${state.revision} · ${counts.nodes ?? state.bundle.nodes.length} ${t("scenario.tree.nodes", "节点")} · ${state.bundle.topology || "hybrid"}`,
                "success",
            );
            renderAll();
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
        state.selectedId = "";
        state.search = "";
        state.validation = null;
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
        setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
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
        results.innerHTML = issues.map((issue) => `
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
        byId("scenarioGraphCanvas")?.addEventListener("click", (event) => {
            const target = (event.target as Element).closest<SVGElement>("[data-node-id]");
            if (target?.getAttribute("data-node-id")) selectNode(target.getAttribute("data-node-id")!);
        });
        byId("scenarioValidateResults")?.addEventListener("click", (event) => {
            const target = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-jump");
            if (target?.dataset.nodeId) selectNode(target.dataset.nodeId);
        });
        byId("scenarioValidateRun")?.addEventListener("click", () => {
            void runValidation();
        });
    }

    window.ScenarioNarrative = { load, clear, init };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
