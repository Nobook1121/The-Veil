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
        aliases?: string[];
        negative_keywords?: string[];
        scope?: string;
        constant?: boolean;
        keyword_weight?: number;
        keyword_match?: string;
        kp_notes?: string;
        objects?: unknown[];
        layout?: { x?: number; y?: number };
        layout_locked?: boolean;
        reveal_when?: unknown;
        source?: string;
        visibility_source?: string;
        visibility_confidence?: number;
        visibility_reasons?: string[];
        spoiler_level?: number;
    }

    interface GraphEdge {
        id: string;
        from: string;
        to: string;
        type?: string;
        label?: string;
        visibility?: string;
        visibility_source?: string;
        hard?: boolean;
        condition?: unknown;
        reveal_when?: unknown;
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
        quality?: {
            topology: string;
            total: number;
            passed: number;
            failed: string[];
            metrics: {
                key: string;
                label: string;
                value: string | number | boolean | null;
                target: string;
                passed: boolean | null;
                detail: string;
            }[];
        };
        revision: number;
    }

    interface ReclassifyChange {
        id: string;
        title: string;
        from: string;
        to: string;
        confidence: number;
        reasons: string[];
    }

    interface ReclassifyPayload {
        applied: boolean;
        scanned: number;
        auto: number;
        changed: number;
        changes: ReclassifyChange[];
        nodes: GraphNode[];
        revision: number;
    }

    interface StartLocation {
        id: string;
        title: string;
        kind: string;
    }

    interface StartPayload {
        start: Record<string, unknown>;
        locations: StartLocation[];
        revision: number;
        permissions: { can_view: boolean; can_edit: boolean; readonly: boolean };
    }

    interface GenerateJob {
        id: string;
        status: string;
        progress: number;
        stage?: string;
        error?: string | null;
    }

    interface GenerateDiff {
        added: string[];
        removed: string[];
        modified: string[];
    }

    interface GenerateResult {
        bundle: GraphBundle;
        summary?: { counts?: Record<string, number> };
        topology: { topology: string; confidence: number; source: string };
        diff: { nodes: GenerateDiff; edges: GenerateDiff; counts: Record<string, number> };
        warnings: Array<{ code: string; level?: string; message: string }>;
        base_revision: number;
        report?: { ok: boolean; error_count: number; warning_count: number };
    }

    interface GenerateJobPayload {
        job: GenerateJob;
        result?: GenerateResult;
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
        viewMode: "kp" | "player";
        connectSource: string;
        positions: Map<string, { x: number; y: number }>;
        view: { x: number; y: number; k: number };
        drag: DragState | null;
        pan: PanState | null;
        draggingFrame: number | null;
        generateJobId: string;
        generateResult: GenerateResult | null;
        generateBusy: boolean;
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

    const REVEAL_LABEL: Record<string, string> = {
        skill: "检定成功",
        clue: "发现线索",
        clock: "时钟达到",
        flag: "旗标成立",
    };

    interface RevealSpec {
        type: "" | "skill" | "clue" | "clock" | "flag";
        target: string;
        threshold: number;
    }

    function revealOf(value: unknown): RevealSpec {
        if (!value || typeof value !== "object") return { type: "", target: "", threshold: 0 };
        const raw = value as Record<string, unknown>;
        if (raw.skill) return { type: "skill", target: String(raw.skill), threshold: Number(raw.difficulty || 0) };
        if (raw.clue) return { type: "clue", target: String(raw.clue), threshold: 0 };
        if (raw.flag) return { type: "flag", target: String(raw.flag), threshold: 0 };
        if (raw.clock) return { type: "clock", target: String(raw.clock), threshold: Number(raw.gte ?? 0) };
        return { type: "", target: "", threshold: 0 };
    }

    function revealToCondition(spec: RevealSpec): Record<string, unknown> | null {
        if (!spec.type || !spec.target) return null;
        if (spec.type === "skill") return { skill: spec.target, difficulty: spec.threshold };
        if (spec.type === "clock") return { clock: spec.target, gte: spec.threshold };
        if (spec.type === "clue") return { clue: spec.target };
        return { flag: spec.target };
    }

    function revealSummary(value: unknown): string {
        const spec = revealOf(value);
        if (!spec.type || !spec.target) return t("scenario.visibility.no_reveal", "未设置揭示条件");
        const label = REVEAL_LABEL[spec.type] || spec.type;
        const threshold = spec.type === "skill" || spec.type === "clock" ? ` ≥ ${spec.threshold}` : "";
        return `${label}：${spec.target}${threshold}`;
    }

    /** 节点与边共用的揭示条件编辑区（§8.7）。 */
    function revealFieldsMarkup(prefix: string, value: unknown): string {
        const reveal = revealOf(value);
        const options = (["skill", "clue", "clock", "flag"] as const)
            .map((type) => `<option value="${type}"${reveal.type === type ? " selected" : ""}>${escapeHtml(REVEAL_LABEL[type])}</option>`)
            .join("");
        return `
            <label class="form-group">
                <span>${escapeHtml(t("scenario.visibility.reveal_when", "揭示条件"))}</span>
                <select class="form-select form-select-sm" id="${prefix}RevealType">
                    <option value=""${reveal.type === "" ? " selected" : ""}>${escapeHtml(t("scenario.visibility.no_reveal", "未设置（需人工复核）"))}</option>
                    ${options}
                </select>
            </label>
            <label class="form-group">
                <span>${escapeHtml(t("scenario.visibility.reveal_target", "目标"))}</span>
                <input type="text" class="form-control form-control-sm" id="${prefix}RevealTarget" value="${escapeHtml(reveal.target)}" placeholder="${escapeHtml(t("scenario.visibility.reveal_target_hint", "技能名 / 线索、时钟或旗标 id"))}">
            </label>
            <label class="form-group">
                <span>${escapeHtml(t("scenario.visibility.reveal_threshold", "难度 / 阈值"))}</span>
                <input type="number" class="form-control form-control-sm" id="${prefix}RevealThreshold" min="0" value="${Number(reveal.threshold || 0)}">
            </label>`;
    }

    function revealBlockMarkup(prefix: string, value: unknown, visibility: string): string {
        return `
            <div class="scenario-reveal-fields" id="${prefix}Reveal"${visibility === "hidden" ? "" : " hidden"}>
                ${revealFieldsMarkup(prefix, value)}
            </div>
            <p class="scenario-reveal-hint${visibility === "kp_only" ? "" : " d-none"}" id="${prefix}KpHint">${escapeHtml(t("scenario.visibility.kp_only_hint", "仅 KP：内容永远不会进入玩家提示词、检索与界面。"))}</p>`;
    }

    function readReveal(prefix: string): RevealSpec {
        return {
            type: (byId<HTMLSelectElement>(`${prefix}RevealType`)?.value || "") as RevealSpec["type"],
            target: byId<HTMLInputElement>(`${prefix}RevealTarget`)?.value.trim() || "",
            threshold: Number(byId<HTMLInputElement>(`${prefix}RevealThreshold`)?.value || 0),
        };
    }

    function syncVisibilityFields(prefix: string, visibility: string): void {
        byId(`${prefix}Reveal`)?.toggleAttribute("hidden", visibility !== "hidden");
        byId(`${prefix}KpHint`)?.classList.toggle("d-none", visibility !== "kp_only");
        syncRevealType(prefix);
    }

    function syncRevealType(prefix: string): void {
        const type = byId<HTMLSelectElement>(`${prefix}RevealType`)?.value || "";
        const threshold = byId<HTMLInputElement>(`${prefix}RevealThreshold`);
        if (threshold) threshold.disabled = type !== "skill" && type !== "clock";
    }

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
        viewMode: "kp",
        connectSource: "",
        positions: new Map(),
        view: { x: 40, y: 40, k: 1 },
        drag: null,
        pan: null,
        draggingFrame: null,
        generateJobId: "",
        generateResult: null,
        generateBusy: false,
    };

    interface KeywordCandidate {
        key: string;
        source: string;
        confidence: number;
    }

    /** 「从正文提取」的候选只对当前节点生效，切换节点即失效。 */
    let keywordCandidates: { nodeId: string; items: KeywordCandidate[] } | null = null;

    /** 图编辑撤销 / 重做（§8.1.6 / §8.1.2）：保存操作前的整包快照。 */
    interface HistoryEntry {
        bundle: GraphBundle;
        revision: number;
    }

    const HISTORY_LIMIT = 30;
    let undoStack: HistoryEntry[] = [];
    let redoStack: HistoryEntry[] = [];
    let restoringHistory = false;

    function splitTerms(value: string | undefined): string[] {
        const seen = new Set<string>();
        const result: string[] = [];
        for (const item of String(value || "").split(/[,，、;；\n]+/)) {
            const word = item.trim();
            if (!word) continue;
            const folded = word.toLowerCase();
            if (seen.has(folded)) continue;
            seen.add(folded);
            result.push(word);
        }
        return result;
    }

    function keywordSourceLabel(source: string): string {
        if (source === "title") return t("scenario.tree.candidate_source_title", "标题");
        if (source === "quoted") return t("scenario.tree.candidate_source_quoted", "引号专名");
        return t("scenario.tree.candidate_source_body", "正文");
    }

    function globalKeywordConflicts(node: GraphNode): Array<{ key: string; others: string[] }> {
        if ((node.scope || "local") !== "global") return [];
        const owners = new Map<string, string[]>();
        for (const item of state.bundle?.nodes || []) {
            if ((item.scope || "local") !== "global") continue;
            const keys = new Set(
                [...(item.keywords || []), ...(item.aliases || [])]
                    .map((word) => word.trim().toLowerCase())
                    .filter(Boolean),
            );
            for (const key of keys) {
                const ids = owners.get(key) || [];
                if (!ids.includes(item.id)) ids.push(item.id);
                owners.set(key, ids);
            }
        }
        const seen = new Set<string>();
        const result: Array<{ key: string; others: string[] }> = [];
        for (const word of [...(node.keywords || []), ...(node.aliases || [])]) {
            const folded = word.trim().toLowerCase();
            if (!folded || seen.has(folded)) continue;
            seen.add(folded);
            const others = (owners.get(folded) || []).filter((id) => id !== node.id);
            if (others.length) {
                result.push({ key: word, others: others.map((id) => nodeById(id)?.title || id) });
            }
        }
        return result;
    }

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

    function isTypingTarget(target: EventTarget | null): boolean {
        return (
            target instanceof HTMLElement &&
            (target.tagName === "INPUT" ||
                target.tagName === "TEXTAREA" ||
                target.tagName === "SELECT" ||
                target.isContentEditable)
        );
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

    const REASON_LABEL: Record<string, string> = {
        module: "KP 模块",
        title_keyword: "标题关键词",
        truth_phrase: "真相措辞",
        stat_block: "数据块",
        explicit_no_disclose: "明确禁止透露",
        spatial_secret: "空间秘密",
        reveal_verb: "揭示动作",
        clue_default_hidden: "线索默认隐藏",
        ending_default_kp_only: "结局默认仅 KP",
        keyword_spoiler: "关键词剧透",
        hidden_without_reveal_when: "隐藏但无揭示条件",
        perceptible: "可感知",
    };

    function reasonLabel(raw: string): string {
        const [name = "", suffix = ""] = String(raw || "").split(":", 2);
        const label = REASON_LABEL[name] || name;
        return suffix ? `${label}（${suffix}）` : label;
    }

    function visibilitySourceLabel(item: { visibility_source?: string; visibility_confidence?: number; visibility_reasons?: string[] }): string {
        if ((item.visibility_source || "auto") === "author") {
            return t("scenario.visibility.source_author", "作者设置");
        }
        const confidence = typeof item.visibility_confidence === "number" ? ` · ${item.visibility_confidence.toFixed(2)}` : "";
        const reasons = (item.visibility_reasons || []).map(reasonLabel).slice(0, 3);
        const reason = reasons.length ? ` · ${t("scenario.visibility.reason_prefix", "理由")}：${reasons.join("、")}` : "";
        return `${t("scenario.visibility.auto_badge", "自动判定")}${confidence}${reason}`;
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
        const conflicts = globalKeywordConflicts(node);
        const conflictKeys = new Set(conflicts.map((item) => item.key.trim().toLowerCase()));
        const edges = (state.bundle?.edges || []).filter((edge) => edge.from === node.id || edge.to === node.id);
        const visibility = node.visibility || "public";
        const rows: Array<[string, string]> = [
            ["ID", `<code>${escapeHtml(node.id)}</code>`],
            ["类型", escapeHtml(meta.label)],
            ["父节点", parentOf(node) ? escapeHtml(nodeById(parentOf(node))?.title || parentOf(node)) : "—"],
            ["可见性来源", escapeHtml(visibilitySourceLabel(node))],
            ["场景角色", escapeHtml(node.scene_role || "—")],
            ["进入方式", escapeHtml(node.access || "—")],
            ["可重复", node.repeatable === false ? "否" : "是"],
            ["物件数", String(Array.isArray(node.objects) ? node.objects.length : 0)],
            ["关键词", (node.keywords || []).length ? node.keywords!.map((word) => {
                const danger = conflictKeys.has(String(word).trim().toLowerCase());
                return `<span class="badge ${danger ? "bg-danger" : "bg-light text-dark"}">${escapeHtml(word)}</span>`;
            }).join(" ") : "—"],
        ];
        if (!canEdit) {
            rows.splice(3, 0, [
                "可见性",
                `<span class="badge bg-${visibility === "public" ? "success" : visibility === "kp_only" ? "danger" : "warning"}">${escapeHtml(VISIBILITY_LABEL[visibility] || visibility)}</span>`,
            ]);
            if (visibility === "hidden") {
                rows.push(["揭示条件", escapeHtml(revealSummary(node.reveal_when))]);
            }
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
                ${revealBlockMarkup("scenarioNode", node.reveal_when, visibility)}
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_body", "正文"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioNodeBody" rows="5">${escapeHtml(node.body || "")}</textarea>
                </label>
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_kp_notes", "KP 备注"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioNodeKpNotes" rows="3">${escapeHtml(node.kp_notes || "")}</textarea>
                </label>
                <div class="scenario-keyword-panel">
                    <div class="scenario-keyword-head">
                        <span>${escapeHtml(t("scenario.tree.keywords", "关键词"))}</span>
                        <button type="button" class="btn btn-outline-secondary btn-sm" id="scenarioNodeExtractKeywords">
                            <i class="fa fa-magic" aria-hidden="true"></i>
                            <span>${escapeHtml(t("scenario.tree.extract_keywords", "从正文提取"))}</span>
                        </button>
                    </div>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_keywords", "主关键词"))} <small class="text-muted">${escapeHtml(t("scenario.tree.edit_keywords_hint", "逗号或换行分隔"))}</small></span>
                        <input type="text" class="form-control form-control-sm${conflicts.length ? " is-invalid" : ""}" id="scenarioNodeKeywords" value="${escapeHtml((node.keywords || []).join(", "))}">
                    </label>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_aliases", "别名"))}</span>
                        <input type="text" class="form-control form-control-sm" id="scenarioNodeAliases" value="${escapeHtml((node.aliases || []).join(", "))}">
                    </label>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_negative_keywords", "否定词"))}</span>
                        <input type="text" class="form-control form-control-sm" id="scenarioNodeNegativeKeywords" value="${escapeHtml((node.negative_keywords || []).join(", "))}">
                    </label>
                    <div class="scenario-keyword-options">
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.edit_scope", "作用域"))}</span>
                            <select class="form-select form-select-sm" id="scenarioNodeScope">
                                ${(["local", "global"] as const).map((value) => `<option value="${value}"${(node.scope || "local") === value ? " selected" : ""}>${escapeHtml(t(`scenario.tree.scope_${value}`, value === "global" ? "全局" : "局部"))}</option>`).join("")}
                            </select>
                        </label>
                        <label class="scenario-module-toggle">
                            <span>${escapeHtml(t("scenario.tree.constant", "常驻"))}</span>
                            <input type="checkbox" id="scenarioNodeConstant" ${node.constant ? "checked" : ""}>
                        </label>
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.keyword_weight", "权重"))}</span>
                            <input type="number" class="form-control form-control-sm" id="scenarioNodeKeywordWeight" min="0" max="2" step="0.1" value="${escapeHtml(String(node.keyword_weight ?? 1))}">
                        </label>
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.keyword_match", "匹配模式"))}</span>
                            <select class="form-select form-select-sm" id="scenarioNodeKeywordMatch">
                                ${(["word", "substring"] as const).map((value) => `<option value="${value}"${(node.keyword_match || "word") === value ? " selected" : ""}>${escapeHtml(t(`scenario.tree.keyword_match_${value}`, value === "substring" ? "子串" : "整词"))}</option>`).join("")}
                            </select>
                        </label>
                    </div>
                    <div id="scenarioNodeKeywordCandidates" class="scenario-keyword-candidates"></div>
                    ${conflicts.length ? `<p class="scenario-keyword-conflict">${escapeHtml(t("scenario.tree.keyword_conflict", "全局关键词同时绑定多个节点；建议改为局部作用域或加否定词。"))} ${conflicts.map((item) => `<strong>${escapeHtml(item.key)}</strong> → ${escapeHtml(item.others.join("、"))}`).join("；")}</p>` : ""}
                </div>
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
        if (canEdit) {
            syncRevealType("scenarioNode");
            renderKeywordCandidates(node);
        }
    }

    function renderKeywordCandidates(node: GraphNode): void {
        const host = byId("scenarioNodeKeywordCandidates");
        if (!host) return;
        const cached = keywordCandidates && keywordCandidates.nodeId === node.id ? keywordCandidates : null;
        if (!cached) {
            host.innerHTML = "";
            return;
        }
        if (!cached.items.length) {
            host.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.candidate_empty", "正文中没有新的候选实体。"))}</p>`;
            return;
        }
        const coverage = t("scenario.tree.candidate_coverage", "正文中还有 {count} 个候选实体未被关键词覆盖。")
            .replace("{count}", String(cached.items.length));
        host.innerHTML = `
            <p class="scenario-keyword-coverage">${escapeHtml(coverage)}</p>
            <div class="scenario-keyword-chips">
                ${cached.items.map((item, index) => `<button type="button" class="scenario-keyword-chip" data-keyword-index="${index}" title="${escapeHtml(t("scenario.tree.candidate_add", "加入主关键词"))}">
                    <span>${escapeHtml(item.key)}</span>
                    <small>${escapeHtml(keywordSourceLabel(item.source))} · ${item.confidence.toFixed(2)}</small>
                </button>`).join("")}
            </div>`;
    }

    function applyKeywordCandidate(index: number): void {
        if (!keywordCandidates) return;
        const item = keywordCandidates.items[index];
        const input = byId<HTMLInputElement>("scenarioNodeKeywords");
        if (!item || !input) return;
        const values = splitTerms(input.value);
        if (!values.some((word) => word.toLowerCase() === item.key.toLowerCase())) values.push(item.key);
        input.value = values.join(", ");
        input.classList.remove("is-invalid");
        keywordCandidates.items.splice(index, 1);
        const node = nodeById(keywordCandidates.nodeId);
        if (node) renderKeywordCandidates(node);
    }

    async function extractNodeKeywords(): Promise<void> {
        const node = nodeById(state.selectedId);
        if (!node || !state.scenarioId || !editable()) return;
        const button = byId<HTMLButtonElement>("scenarioNodeExtractKeywords");
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ suggestions: Array<{ node_id: string; suggested: KeywordCandidate[] }> }>>(
                `/api/scenarios/${state.scenarioId}/knowledge/suggest-keywords`,
                { method: "POST", body: { target: "node", node_ids: [node.id], limit: 8 } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const entry = (data.data.suggestions || []).find((item) => item.node_id === node.id);
            keywordCandidates = { nodeId: node.id, items: entry?.suggested || [] };
            renderKeywordCandidates(node);
            if (!keywordCandidates.items.length) {
                showNotification(t("scenario.tree.candidate_empty", "正文中没有新的候选实体。"), "warning");
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message || t("scenario.tree.keyword_extract_failed", "关键词提取失败。"), "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function selectNode(nodeId: string): void {
        state.selectedId = nodeId;
        state.selectedEdgeId = "";
        keywordCandidates = null;
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
        const body = byId<HTMLTextAreaElement>("scenarioNodeBody")?.value || "";
        const keywords = splitTerms(byId<HTMLInputElement>("scenarioNodeKeywords")?.value);
        const aliases = splitTerms(byId<HTMLInputElement>("scenarioNodeAliases")?.value);
        const negativeKeywords = splitTerms(byId<HTMLInputElement>("scenarioNodeNegativeKeywords")?.value);
        const scope = byId<HTMLSelectElement>("scenarioNodeScope")?.value || "local";
        const constant = byId<HTMLInputElement>("scenarioNodeConstant")?.checked === true;
        const rawWeight = Number(byId<HTMLInputElement>("scenarioNodeKeywordWeight")?.value);
        const keywordWeight = Number.isFinite(rawWeight) ? Math.max(0, Math.min(2, rawWeight)) : 1;
        const keywordMatch = byId<HTMLSelectElement>("scenarioNodeKeywordMatch")?.value === "substring" ? "substring" : "word";
        const layoutLocked = byId<HTMLInputElement>("scenarioNodeLayoutLocked")?.checked === true;
        const reveal = readReveal("scenarioNode");
        const revealWhen = visibility === "hidden" ? revealToCondition(reveal) : null;
        if (!title) {
            showNotification(t("scenario.tree.title_required", "节点标题不能为空。"), "warning");
            return;
        }
        const button = byId<HTMLButtonElement>("scenarioNodeSave");
        if (button) button.disabled = true;
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(node.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, title, visibility, body, kp_notes: kpNotes, keywords, aliases, negative_keywords: negativeKeywords, scope, constant, keyword_weight: keywordWeight, keyword_match: keywordMatch, reveal_when: revealWhen, layout_locked: layoutLocked } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
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

    // ------------------------------------------------------------------
    // 撤销 / 重做：操作前记录整包快照，恢复时走全量 PUT（带 base_revision）
    // ------------------------------------------------------------------
    function snapshotBundle(): HistoryEntry | null {
        if (!state.bundle) return null;
        try {
            return { bundle: JSON.parse(JSON.stringify(state.bundle)) as GraphBundle, revision: state.revision };
        } catch {
            return null;
        }
    }

    function resetHistory(): void {
        undoStack = [];
        redoStack = [];
        updateHistoryButtons();
    }

    /** 在发起写请求前调用；失败时用 `discardHistory` 撤销这次记录。 */
    function pushHistory(): HistoryEntry | null {
        const entry = snapshotBundle();
        if (!entry) return null;
        undoStack.push(entry);
        if (undoStack.length > HISTORY_LIMIT) undoStack.shift();
        redoStack = [];
        updateHistoryButtons();
        return entry;
    }

    function discardHistory(entry: HistoryEntry | null): void {
        if (!entry) return;
        const index = undoStack.lastIndexOf(entry);
        if (index >= 0) undoStack.splice(index, 1);
        updateHistoryButtons();
    }

    function updateHistoryButtons(): void {
        const allowed = editable();
        const undo = byId<HTMLButtonElement>("scenarioGraphUndo");
        const redo = byId<HTMLButtonElement>("scenarioGraphRedo");
        if (undo) {
            undo.disabled = !allowed || undoStack.length === 0;
            undo.title = `${t("scenario.graph.undo", "撤销")} (Ctrl+Z)`;
        }
        if (redo) {
            redo.disabled = !allowed || redoStack.length === 0;
            redo.title = `${t("scenario.graph.redo", "重做")} (Ctrl+Y)`;
        }
    }

    async function restoreHistory(entry: HistoryEntry): Promise<boolean> {
        if (!state.scenarioId || !state.bundle) return false;
        const bundle = JSON.parse(JSON.stringify(entry.bundle)) as GraphBundle & { meta?: Record<string, unknown> };
        // 服务端以传入 meta.revision 作为基线累加；这里强制对齐当前修订号，
        // 避免撤销把 revision 回退导致后续写请求全部冲突。
        bundle.meta = { ...(bundle.meta || {}), revision: state.revision };
        restoringHistory = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: state.revision, bundle } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return false;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            applyRevision(data.data.revision);
            keywordCandidates = null;
            renderAll();
            if (state.canEdit) void runValidation();
            return true;
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
            return false;
        } finally {
            restoringHistory = false;
        }
    }

    async function undoHistory(): Promise<void> {
        if (restoringHistory || !editable() || undoStack.length === 0) return;
        const entry = undoStack.pop() as HistoryEntry;
        const current = snapshotBundle();
        const ok = await restoreHistory(entry);
        if (!ok) {
            undoStack.push(entry);
            updateHistoryButtons();
            return;
        }
        if (current) {
            redoStack.push(current);
            if (redoStack.length > HISTORY_LIMIT) redoStack.shift();
        }
        updateHistoryButtons();
    }

    async function redoHistory(): Promise<void> {
        if (restoringHistory || !editable() || redoStack.length === 0) return;
        const entry = redoStack.pop() as HistoryEntry;
        const current = snapshotBundle();
        const ok = await restoreHistory(entry);
        if (!ok) {
            redoStack.push(entry);
            updateHistoryButtons();
            return;
        }
        if (current) {
            undoStack.push(current);
            if (undoStack.length > HISTORY_LIMIT) undoStack.shift();
        }
        updateHistoryButtons();
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
        const meta = kindMeta(node.kind);
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
        state.positions = defaultPositions(bundle);
        const positions = state.positions;
        const { width, height } = viewportSize();
        const edges = bundle.edges.map((edge) => edgeMarkup(edge, positions)).join("");
        const nodes = bundle.nodes.map((node) => nodeMarkup(node, positions)).join("");
        canvas.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="100%" height="100%" role="img" aria-label="世界树图谱"><g id="scenarioGraphViewport">${edges}${nodes}</g></svg>`;
        applyView();
        setGraphHint(state.viewMode === "player" ? playerHint() : modeHint(state.mode));
        renderEdgeActions();
    }

    function playerHint(): string {
        return t("scenario.visibility.player_hint", "玩家视图：只显示公开内容；隐藏节点灰显，仅 KP 内容不会出现。");
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
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, visibility, reveal_when: revealWhen } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            showNotification(t("scenario.graph.edge_saved", "边已保存。"), "success");
        } catch (error) {
            discardHistory(history);
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
        setGraphHint(modeHint(mode));
        renderGraph();
    }

    function setViewMode(mode: "kp" | "player"): void {
        state.viewMode = mode;
        state.connectSource = "";
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphView === mode);
        });
        byId("scenarioGraphCanvas")?.classList.toggle("is-player-view", mode === "player");
        renderGraph();
    }

    function resetViewMode(): void {
        state.viewMode = "kp";
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.classList.toggle("active", button.dataset.graphView === "kp");
        });
        byId("scenarioGraphCanvas")?.classList.toggle("is-player-view", false);
    }

    interface PreviewEntry {
        node: GraphNode;
        visibility: "public" | "hidden" | "kp_only";
    }

    function previewEntryMarkup(entry: PreviewEntry): string {
        const { node, visibility } = entry;
        const meta = kindMeta(node.kind);
        if (visibility === "public") {
            const excerpt = [node.summary || "", node.body || ""].filter(Boolean).join(" ").slice(0, 180);
            return `
                <article class="scenario-preview-card">
                    <header><span class="badge bg-light text-dark">${escapeHtml(meta.label)}</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                    ${excerpt ? `<p class="mb-0">${escapeHtml(excerpt)}${excerpt.length >= 180 ? "…" : ""}</p>` : ""}
                    ${(node.keywords || []).length ? `<div class="scenario-preview-keywords">${node.keywords!.map((word) => `<span class="badge bg-light text-dark">${escapeHtml(word)}</span>`).join(" ")}</div>` : ""}
                </article>`;
        }
        if (visibility === "hidden") {
            return `
                <article class="scenario-preview-card is-hidden">
                    <header><span class="badge bg-warning text-dark">${escapeHtml(t("scenario.visibility.hidden_placeholder", "未揭示"))}</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                    <p class="mb-0 small">${escapeHtml(t("scenario.visibility.hidden_reason", "玩家暂不可见，满足揭示条件后出现："))}${escapeHtml(revealSummary(node.reveal_when))}</p>
                </article>`;
        }
        return `
            <article class="scenario-preview-card is-kp">
                <header><span class="badge bg-secondary">KP ONLY</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                <p class="mb-0 small">${escapeHtml(t("scenario.visibility.kp_only_hint", "仅 KP：内容永远不会进入玩家提示词、检索与界面。"))}</p>
            </article>`;
    }

    function renderPlayerPreview(): void {
        const body = byId("scenarioPlayerPreviewBody");
        const summary = byId("scenarioPlayerPreviewSummary");
        if (!body) return;
        const bundle = state.bundle;
        if (!bundle || bundle.nodes.length === 0) {
            body.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.visibility.player_empty", "当前剧本没有可预览的节点。"))}</p>`;
            if (summary) summary.textContent = "";
            return;
        }
        const entries: PreviewEntry[] = sortNodes(bundle.nodes).map((node) => ({
            node,
            visibility: ((node.visibility || "public") === "kp_only" ? "kp_only" : (node.visibility || "public") === "hidden" ? "hidden" : "public"),
        }));
        const visibleEdges = (bundle.edges || []).filter((edge) => {
            const from = nodeById(edge.from);
            const to = nodeById(edge.to);
            return (edge.visibility || "public") === "public"
                && (from?.visibility || "public") === "public"
                && (to?.visibility || "public") === "public";
        }).length;
        const groups: Array<[string, "public" | "hidden" | "kp_only"]> = [
            ["scenario.visibility.group_public", "public"],
            ["scenario.visibility.group_hidden", "hidden"],
            ["scenario.visibility.group_kp", "kp_only"],
        ];
        const fallback: Record<string, string> = {
            "scenario.visibility.group_public": "玩家可见",
            "scenario.visibility.group_hidden": "未揭示（满足条件后出现）",
            "scenario.visibility.group_kp": "仅 KP（玩家永不可见）",
        };
        body.innerHTML = groups.map(([key, group]) => {
            const items = entries.filter((entry) => entry.visibility === group);
            if (items.length === 0) return "";
            return `<section class="scenario-preview-group"><h6>${escapeHtml(t(key, fallback[key] || key))} <span class="text-muted small">${items.length}</span></h6>${items.map(previewEntryMarkup).join("")}</section>`;
        }).join("");
        const counts = {
            public: entries.filter((entry) => entry.visibility === "public").length,
            hidden: entries.filter((entry) => entry.visibility === "hidden").length,
            kp: entries.filter((entry) => entry.visibility === "kp_only").length,
        };
        if (summary) {
            summary.textContent = `${counts.public} ${t("scenario.visibility.count_visible", "可见")} · ${counts.hidden} ${t("scenario.visibility.count_hidden", "未揭示")} · ${counts.kp} ${t("scenario.visibility.count_kp", "仅 KP")} · ${visibleEdges} ${t("scenario.visibility.count_edges", "可见连接")}`;
        }
    }

    function openPlayerPreview(): void {
        if (!state.bundle) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        renderPlayerPreview();
        const modal = byId("scenarioPlayerPreviewModal");
        const api = (window as unknown as { bootstrap?: { Modal?: { getOrCreateInstance: (element: Element) => { show: () => void } } } }).bootstrap?.Modal;
        if (modal) api?.getOrCreateInstance(modal).show();
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
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(nodeId)}`,
                { method: "PATCH", body: { base_revision: state.revision, layout: { x: position.x, y: position.y } } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function connectNodes(sourceId: string, targetId: string): Promise<void> {
        const type = (byId<HTMLSelectElement>("scenarioGraphEdgeType")?.value || "adjacent");
        const hard = byId<HTMLInputElement>("scenarioGraphEdgeHard")?.checked === true;
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges`,
                { method: "POST", body: { base_revision: state.revision, from: sourceId, to: targetId, type, hard, source: "author" } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    async function handleNodeClick(nodeId: string): Promise<void> {
        if (state.mode === "connect" && editable() && state.viewMode === "kp") {
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
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, hard: !edge.hard } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
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
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edge.id)}`,
                { method: "DELETE", body: { base_revision: state.revision } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
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
        const history = pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: state.revision, bundle: { ...bundle, nodes } } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    discardHistory(history);
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
            discardHistory(history);
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
                    await handleConflict();
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
            const history = pushHistory();
            const applied = await TrpgApi.requestWithResponse<ApiResponse<ReclassifyPayload>>(
                endpoint,
                { method: "POST", body: { base_revision: state.revision, apply: true } },
            );
            if (!applied.response.ok || !applied.data?.success || !applied.data.data) {
                discardHistory(history);
                if (applied.response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(applied.data?.message || `HTTP ${applied.response.status}`);
            }
            const result = applied.data.data;
            applyRevision(result.revision);
            if (state.bundle) {
                const updated = new Map(result.nodes.map((node) => [node.id, node]));
                state.bundle.nodes = state.bundle.nodes.map((node) => updated.get(node.id) || node);
            }
            renderAll();
            showNotification(
                `${t("scenario.visibility.bulk_done", "已重新判定")} ${result.changed}`,
                "success",
            );
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        }
    }

    let openingStart: Record<string, unknown> = {};

    async function openOpeningEditor(): Promise<void> {
        if (!state.scenarioId) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<StartPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/start`,
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const payload = data.data;
            openingStart = payload.start || {};
            const locationSelect = byId<HTMLSelectElement>("scenarioOpeningLocation");
            if (locationSelect) {
                locationSelect.innerHTML = `<option value="">${escapeHtml(t("scenario.visibility.opening_location_none", "未设置"))}</option>`
                    + payload.locations.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}（${escapeHtml(item.kind)}）</option>`).join("");
                locationSelect.value = String(openingStart.location_id || "");
            }
            const mode = byId<HTMLSelectElement>("scenarioOpeningMode");
            if (mode) mode.value = String(openingStart.opening_mode || "hybrid");
            const intro = byId<HTMLTextAreaElement>("scenarioOpeningIntro");
            if (intro) {
                intro.value = (Array.isArray(openingStart.public_intro) ? openingStart.public_intro as string[] : []).join("\n\n");
            }
            const kpTruth = byId<HTMLTextAreaElement>("scenarioOpeningKpTruth");
            if (kpTruth) kpTruth.value = String(openingStart.kp_truth || "");
            const enterTriggers = byId<HTMLInputElement>("scenarioOpeningEnterTriggers");
            if (enterTriggers) enterTriggers.checked = openingStart.allow_enter_triggers !== false;
            const modelContinue = byId<HTMLInputElement>("scenarioOpeningModelContinue");
            if (modelContinue) modelContinue.checked = openingStart.allow_model_continuation !== false;

            const readonly = payload.permissions?.readonly === true;
            document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | HTMLButtonElement>(
                "#scenarioOpeningModal input, #scenarioOpeningModal select, #scenarioOpeningModal textarea, #scenarioOpeningSave",
            ).forEach((field) => { field.disabled = readonly; });
            const status = byId("scenarioOpeningStatus");
            if (status) status.textContent = readonly ? t("scenario.visibility.readonly", "只读剧本：可预览，不可保存。") : "";
            const preview = byId("scenarioOpeningPreview");
            if (preview) preview.innerHTML = "";
            const modal = byId("scenarioOpeningModal");
            const api = (window as unknown as { bootstrap?: { Modal?: { getOrCreateInstance: (element: Element) => { show: () => void } } } }).bootstrap?.Modal;
            if (modal) api?.getOrCreateInstance(modal).show();
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function readOpeningForm(): Record<string, unknown> | null {
        const locationSelect = byId<HTMLSelectElement>("scenarioOpeningLocation");
        if (!locationSelect) return null;
        const intro = byId<HTMLTextAreaElement>("scenarioOpeningIntro")?.value || "";
        return {
            ...openingStart,
            location_id: locationSelect.value || "",
            opening_mode: byId<HTMLSelectElement>("scenarioOpeningMode")?.value || "hybrid",
            public_intro: intro.split(/\n\s*\n|\n/).map((part) => part.trim()).filter(Boolean),
            kp_truth: byId<HTMLTextAreaElement>("scenarioOpeningKpTruth")?.value.trim() || "",
            allow_enter_triggers: byId<HTMLInputElement>("scenarioOpeningEnterTriggers")?.checked !== false,
            allow_model_continuation: byId<HTMLInputElement>("scenarioOpeningModelContinue")?.checked !== false,
        };
    }

    async function saveOpening(): Promise<void> {
        if (!state.scenarioId || !editable()) return;
        const start = readOpeningForm();
        if (!start) return;
        const button = byId<HTMLButtonElement>("scenarioOpeningSave");
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ start: Record<string, unknown>; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/start`,
                { method: "PUT", body: { base_revision: state.revision, start } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            openingStart = data.data.start || {};
            applyRevision(data.data.revision);
            const status = byId("scenarioOpeningStatus");
            if (status) status.textContent = t("scenario.visibility.opening_saved", "开场配置已保存。");
            showNotification(t("scenario.visibility.opening_saved", "开场配置已保存。"), "success");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function previewOpening(): void {
        const container = byId("scenarioOpeningPreview");
        if (!container) return;
        const start = readOpeningForm() || {};
        const intro = Array.isArray(start.public_intro) ? start.public_intro as string[] : [];
        const mode = String(start.opening_mode || "hybrid");
        const bubbles = intro.map((paragraph) => `<div class="scenario-opening-bubble"><span class="scenario-opening-speaker">${escapeHtml(t("scenario.visibility.opening_narrator", "旁白"))}</span><p class="mb-0">${escapeHtml(paragraph)}</p></div>`);
        if (mode !== "fixed") {
            bubbles.push(`<div class="scenario-opening-note small text-muted">${escapeHtml(t("scenario.visibility.opening_model_note", "模型会在固定开场后继续叙事。"))}</div>`);
        }
        container.innerHTML = bubbles.length ? bubbles.join("") : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.visibility.opening_empty", "尚未填写公开开场。"))}</p>`;
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
            if (target.closest("[data-edge-save]")) {
                void saveEdge();
                return;
            }
            if (target.closest("[data-edge-toggle]")) {
                void toggleEdgeHard();
                return;
            }
            if (target.closest("[data-edge-delete]")) {
                void deleteSelectedEdge();
            }
        });

        byId("scenarioGraphEdgeActions")?.addEventListener("change", (event) => {
            const target = event.target as HTMLSelectElement;
            if (target.id === "scenarioEdgeVisibility") syncVisibilityFields("scenarioEdge", target.value);
            if (target.id === "scenarioEdgeRevealType") syncRevealType("scenarioEdge");
        });

        document.querySelectorAll<HTMLElement>("[data-graph-mode]").forEach((button) => {
            button.addEventListener("click", () => setMode((button.dataset.graphMode as GraphMode) || "select"));
        });
        document.querySelectorAll<HTMLElement>("[data-graph-view]").forEach((button) => {
            button.addEventListener("click", () => setViewMode(button.dataset.graphView === "player" ? "player" : "kp"));
        });
        byId("scenarioGraphFit")?.addEventListener("click", () => fitView());
        byId("scenarioGraphUndo")?.addEventListener("click", () => {
            void undoHistory();
        });
        byId("scenarioGraphRedo")?.addEventListener("click", () => {
            void redoHistory();
        });
        byId("scenarioGraphAutoLayout")?.addEventListener("click", () => {
            void autoLayout();
        });
        byId("scenarioGraphReclassify")?.addEventListener("click", () => {
            void reclassifyVisibility();
        });
        byId("scenarioGraphPlayerPreview")?.addEventListener("click", () => openPlayerPreview());
        byId("scenarioOpeningOpen")?.addEventListener("click", () => {
            void openOpeningEditor();
        });
        byId("scenarioOpeningSave")?.addEventListener("click", () => {
            void saveOpening();
        });
        byId("scenarioOpeningPreviewBtn")?.addEventListener("click", () => previewOpening());

        // §8.1.6：Esc 退出连线；Ctrl+Z / Ctrl+Y 撤销重做；Delete 删除选中连接。
        document.addEventListener(
            "keydown",
            (event) => {
                if (!byId("scenarioModal")?.classList.contains("show")) return;
                if (event.key === "Escape" && state.mode === "connect") {
                    event.preventDefault();
                    event.stopPropagation();
                    state.connectSource = "";
                    setMode("select");
                    return;
                }
                const typing = isTypingTarget(event.target);
                if ((event.ctrlKey || event.metaKey) && !typing) {
                    const key = event.key.toLowerCase();
                    if (key === "z") {
                        event.preventDefault();
                        void (event.shiftKey ? redoHistory() : undoHistory());
                        return;
                    }
                    if (key === "y") {
                        event.preventDefault();
                        void redoHistory();
                        return;
                    }
                }
                if ((event.key !== "Delete" && event.key !== "Backspace") || state.selectedEdgeId === "" || typing) return;
                event.preventDefault();
                void deleteSelectedEdge(true);
            },
            true,
        );
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
        keywordCandidates = null;
        resetHistory();
        resetViewMode();
        resetGeneratePanel();
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
            syncGeneratePanel();
            renderAll();
            updateHistoryButtons();
            window.requestAnimationFrame(() => fitView());
            window.ScenarioTriggers?.syncRevision(state.revision);
            emitSelection(null);
            if (state.canEdit) void runValidation();
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
        keywordCandidates = null;
        resetHistory();
        resetViewMode();
        resetGeneratePanel();
        const search = byId<HTMLInputElement>("scenarioTreeSearch");
        if (search) search.value = "";
        setStatus(t("scenario.tree.empty_badge", "未生成世界树"), "secondary");
        setEmpty(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"));
        const results = byId("scenarioValidateResults");
        if (results) results.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.validate.hint", "运行校验会检查死亡节点、死锁触发器、越级出口与禁词泄漏。"))}</p>`;
        updatePublishGuard(0);
        emitSelection(null);
        showTab("basic");
    }

    function updatePublishGuard(count: number): void {
        const button = byId<HTMLButtonElement>("saveScenario");
        if (!button) return;
        button.classList.toggle("scenario-publish-blocked", count > 0);
        button.title = count > 0
            ? t("scenario.visibility.publish_blocked", "还有 {count} 项可见性待复核，发布前请先处理。").replace("{count}", String(count))
            : "";
    }

    async function acceptReviewIssue(target: string): Promise<void> {
        if (!state.scenarioId) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/visibility/review`,
                { method: "POST", body: { target, base_revision: state.revision } },
            );
            if (!response.ok || !data?.success) throw new Error(data?.message || `HTTP ${response.status}`);
            if (data.data?.revision != null) state.revision = data.data.revision;
            await runValidation();
            showNotification(t("scenario.visibility.review_accepted", "已确认该可见性判定。"), "success");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function renderValidation(payload: ValidationPayload): void {
        const results = byId("scenarioValidateResults");
        const summary = byId("scenarioValidateSummary");
        if (summary) {
            summary.textContent = `${payload.ok ? t("scenario.validate.ok", "通过") : t("scenario.validate.failed", "存在问题")} · ${payload.error_count} error · ${payload.warning_count} warn`;
        }
        if (!results) return;
        const quality = payload.quality;
        const qualityMarkup = quality
            ? `<section class="scenario-validate-group"><h6 class="scenario-validate-group-title">${escapeHtml(t("scenario.validate.quality", "质量指标"))} <span class="badge bg-${quality.failed.length ? "warning text-dark" : "success"}">${quality.passed}/${quality.total}</span></h6><div class="d-flex flex-wrap gap-2">${quality.metrics.map((metric) => `<span class="badge bg-${metric.passed === false ? "danger" : metric.passed === true ? "success" : "secondary"}" title="${escapeHtml(`${metric.target} · ${metric.detail}`)}">${escapeHtml(metric.label)} ${escapeHtml(String(metric.value ?? "-"))}</span>`).join("")}</div></section>`
            : "";
        const issues = payload.issues || [];
        if (issues.length === 0) {
            updatePublishGuard(0);
            results.innerHTML = `<p class="text-success small mb-0">${escapeHtml(t("scenario.validate.clean", "未发现问题。"))}</p>${qualityMarkup}`;
            return;
        }
        const levelVariant: Record<string, string> = { error: "danger", warn: "warning", info: "info" };
        const items = issues.slice();
        if (payload.order_simulation && !payload.order_simulation.ok) {
            items.push(...payload.order_simulation.issues.filter((issue) => issue.level === "error"));
        }
        const issueMarkup = (issue: ValidationIssue): string => `
            <div class="scenario-validate-item scenario-validate-${escapeHtml(levelVariant[issue.level] || "info")}">
                <div class="scenario-validate-head">
                    <span class="badge bg-${escapeHtml(levelVariant[issue.level] || "info")}">${escapeHtml(issue.level)}</span>
                    <code>${escapeHtml(issue.code)}</code>
                    ${issue.node_id ? `<button type="button" class="btn btn-link btn-sm p-0 scenario-validate-jump" data-node-id="${escapeHtml(issue.node_id)}">${escapeHtml(nodeById(issue.node_id)?.title || issue.node_id)}</button>` : ""}
                </div>
                <div class="small">${escapeHtml(issue.message)}</div>
                ${issue.suggestion ? `<div class="small text-muted">${escapeHtml(issue.suggestion)}</div>` : ""}
                ${issue.code === "visibility.needs_review"
                    ? `<button type="button" class="btn btn-sm btn-outline-success scenario-validate-accept" data-review-target="${escapeHtml(issue.node_id ? `node:${issue.node_id}` : `edge:${issue.edge_id || ""}`)}">${escapeHtml(t("scenario.visibility.accept_review", "确认该判定"))}</button>`
                    : ""}
            </div>`;
        const reviewItems = items.filter((issue) => issue.code.startsWith("visibility.") || issue.code === "trigger.needs_review");
        const otherItems = items.filter((issue) => !reviewItems.includes(issue));
        updatePublishGuard(reviewItems.filter((issue) => issue.code === "visibility.needs_review").length);
        results.innerHTML = [
            qualityMarkup,
            reviewItems.length
                ? `<section class="scenario-validate-group"><h6 class="scenario-validate-group-title">${escapeHtml(t("scenario.visibility.review_queue", "待复核可见性"))} <span class="badge bg-warning text-dark">${reviewItems.length}</span></h6>${reviewItems.map(issueMarkup).join("")}</section>`
                : "",
            otherItems.length ? otherItems.map(issueMarkup).join("") : "",
        ].join("");
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

    function generateSource(): string {
        const checked = document.querySelector<HTMLInputElement>('input[name="scenarioGenerateSource"]:checked');
        return checked?.value || "document";
    }

    function syncGenerateSource(): void {
        const premise = byId<HTMLTextAreaElement>("scenarioGeneratePremise");
        if (premise) premise.disabled = generateSource() !== "premise";
    }

    function collectGenerateOptions(): Record<string, unknown> {
        return {
            source: generateSource(),
            premise: byId<HTMLTextAreaElement>("scenarioGeneratePremise")?.value.trim() || "",
            topology: byId<HTMLSelectElement>("scenarioGenerateTopology")?.value || "auto",
            acts: Number(byId<HTMLInputElement>("scenarioGenerateActs")?.value || 2),
            scenes_per_act: Number(byId<HTMLInputElement>("scenarioGenerateScenes")?.value || 3),
            endings: Number(byId<HTMLInputElement>("scenarioGenerateEndings")?.value || 0),
            clocks: Number(byId<HTMLInputElement>("scenarioGenerateClocks")?.value || 0),
            branch: byId<HTMLSelectElement>("scenarioGenerateBranch")?.value || "medium",
            free_order: byId<HTMLInputElement>("scenarioGenerateFreeOrder")?.checked !== false,
            ai_fill: byId<HTMLInputElement>("scenarioGenerateAiFill")?.checked === true,
        };
    }

    function setGenerateStatus(text: string, variant: "secondary" | "success" | "warning" | "danger" = "secondary"): void {
        const status = byId("scenarioGenerateStatus");
        if (!status) return;
        status.textContent = text;
        status.className = variant === "secondary" ? "small text-muted" : `small text-${variant}`;
    }

    function renderGeneratePreview(result: GenerateResult | null): void {
        const preview = byId("scenarioGeneratePreview");
        if (!preview) return;
        if (!result) {
            preview.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.generate.preview.empty", "还没有生成预览。"))}</p>`;
            return;
        }
        const counts = result.diff?.counts || {};
        const stat = (label: string, value: unknown): string => `
            <div class="scenario-generate-preview-stat">
                <strong>${escapeHtml(value ?? "-")}</strong>
                <span class="text-muted small">${escapeHtml(label)}</span>
            </div>`;
        const badge = (variant: "added" | "removed" | "modified" | "info", label: string, value: unknown): string =>
            `<span class="scenario-generate-badge ${variant}">${escapeHtml(label)} ${escapeHtml(value ?? 0)}</span>`;
        const warnings = result.warnings?.length
            ? `<ul class="small text-muted mb-0 mt-2">${result.warnings
                .slice(0, 8)
                .map((item) => `<li>${escapeHtml(item.message)}</li>`)
                .join("")}</ul>`
            : "";
        preview.innerHTML = `
            <div class="scenario-generate-preview-summary">
                ${stat(t("scenario.generate.preview.nodes", "节点"), result.bundle.nodes.length)}
                ${stat(t("scenario.generate.preview.edges", "边"), result.bundle.edges.length)}
                ${stat(t("scenario.generate.preview.topology", "拓扑"), result.topology?.topology || "-")}
                ${stat(t("scenario.generate.preview.warnings", "提示"), result.warnings?.length || 0)}
            </div>
            <div class="scenario-generate-preview-changes">
                ${badge("added", t("scenario.generate.preview.added", "新增"), counts.nodes_added)}
                ${badge("modified", t("scenario.generate.preview.modified", "修改"), counts.nodes_modified)}
                ${badge("removed", t("scenario.generate.preview.removed", "移除"), counts.nodes_removed)}
                ${badge("info", t("scenario.generate.preview.free_edges", "自由边"), counts.free_edges)}
                ${badge("info", t("scenario.generate.preview.gated_edges", "门控边"), counts.gated_edges)}
                ${badge("info", t("scenario.generate.preview.soft_edges", "软边"), counts.soft_edges)}
            </div>
            ${warnings}`;
    }

    function syncGeneratePanel(): void {
        const run = byId<HTMLButtonElement>("scenarioGenerateRun");
        if (run) run.disabled = !editable() || state.generateBusy;
        const apply = byId<HTMLButtonElement>("scenarioGenerateApply");
        if (apply) apply.disabled = !state.generateResult || !editable() || state.generateBusy;
    }

    function resetGeneratePanel(): void {
        state.generateJobId = "";
        state.generateResult = null;
        state.generateBusy = false;
        setGenerateStatus("");
        renderGeneratePreview(null);
        syncGeneratePanel();
    }

    async function pollGenerateJob(jobId: string): Promise<GenerateResult> {
        for (let attempt = 0; attempt < 300; attempt += 1) {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<GenerateJobPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/jobs/${encodeURIComponent(jobId)}`,
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const job = data.data.job;
            setGenerateStatus(`${job.progress || 0}% · ${job.stage || job.status}`);
            if (job.status === "failed") {
                throw new Error(job.error || t("scenario.generate.failed", "生成失败"));
            }
            if (job.status === "done") {
                if (!data.data.result) throw new Error(t("scenario.generate.failed", "生成失败"));
                return data.data.result;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 600));
        }
        throw new Error(t("scenario.generate.timeout", "生成超时，请稍后重试。"));
    }

    async function startGeneration(): Promise<void> {
        if (!state.scenarioId || !editable()) {
            showNotification(t("scenario.generate.readonly", "只读剧本不能生成世界树。"), "warning");
            return;
        }
        state.generateBusy = true;
        state.generateResult = null;
        renderGeneratePreview(null);
        setGenerateStatus(t("scenario.generate.running", "生成中…"));
        syncGeneratePanel();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ job_id: string; job: GenerateJob }>>(
                `/api/scenarios/${state.scenarioId}/narrative/generate`,
                { method: "POST", body: { options: collectGenerateOptions() } },
            );
            if (!response.ok || !data?.success || !data.data?.job_id) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.generateJobId = data.data.job_id;
            const result = await pollGenerateJob(state.generateJobId);
            state.generateResult = result;
            renderGeneratePreview(result);
            setGenerateStatus(t("scenario.generate.preview.ready", "预览已生成"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setGenerateStatus(message, "danger");
            showNotification(message, "error");
        } finally {
            state.generateBusy = false;
            syncGeneratePanel();
        }
    }

    async function applyGeneration(): Promise<void> {
        if (!state.generateResult || !editable()) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: t("scenario.generate.apply", "应用到草稿"),
            message: t("scenario.generate.confirm_apply", "应用后会用预览覆盖当前草稿的世界树，继续吗？"),
            confirmText: t("scenario.generate.apply", "应用到草稿"),
        });
        if (!confirmed) return;
        const result = state.generateResult;
        setGenerateStatus(t("scenario.generate.applying", "应用中…"));
        const applyButton = byId<HTMLButtonElement>("scenarioGenerateApply");
        if (applyButton) applyButton.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: result.base_revision, bundle: result.bundle } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            state.revision = data.data.revision;
            state.generateResult = null;
            state.selectedId = "";
            state.selectedEdgeId = "";
            renderAll();
            window.requestAnimationFrame(() => fitView());
            window.ScenarioTriggers?.syncRevision(state.revision);
            renderGeneratePreview(null);
            setGenerateStatus(t("scenario.generate.applied", "已应用到草稿，请检查后保存并发布。"), "success");
            showNotification(t("scenario.generate.applied", "已应用到草稿，请检查后保存并发布。"), "success");
            showTab("tree");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setGenerateStatus(message, "danger");
            showNotification(message, "error");
        } finally {
            syncGeneratePanel();
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
            const accept = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-accept");
            if (accept?.dataset.reviewTarget) {
                void acceptReviewIssue(accept.dataset.reviewTarget);
                return;
            }
            const target = (event.target as HTMLElement).closest<HTMLElement>(".scenario-validate-jump");
            if (target?.dataset.nodeId) selectNode(target.dataset.nodeId);
        });
        byId("scenarioValidateRun")?.addEventListener("click", () => {
            void runValidation();
        });
        byId("scenarioNodeInspector")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("#scenarioNodeSave")) {
                void saveNode();
                return;
            }
            if (target.closest("#scenarioNodeExtractKeywords")) {
                void extractNodeKeywords();
                return;
            }
            const chip = target.closest<HTMLElement>("[data-keyword-index]");
            if (chip) applyKeywordCandidate(Number(chip.dataset.keywordIndex));
        });
        byId("scenarioNodeInspector")?.addEventListener("change", (event) => {
            const target = event.target as HTMLSelectElement;
            if (target.id === "scenarioNodeVisibility") syncVisibilityFields("scenarioNode", target.value);
            if (target.id === "scenarioNodeRevealType") syncRevealType("scenarioNode");
        });
        document.querySelectorAll<HTMLInputElement>('input[name="scenarioGenerateSource"]').forEach((radio) => {
            radio.addEventListener("change", syncGenerateSource);
        });
        byId("scenarioGenerateRun")?.addEventListener("click", () => {
            void startGeneration();
        });
        byId("scenarioGenerateApply")?.addEventListener("click", () => {
            void applyGeneration();
        });
        syncGenerateSource();
        resetGeneratePanel();
        bindGraphEvents();
    }

    window.ScenarioNarrative = {
        load,
        clear,
        init,
        syncRevision,
        revision: () => state.revision,
        canEdit: () => state.canEdit && !state.readonly,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
