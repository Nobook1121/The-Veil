/**
 * 剧本世界树 / 图谱：共享状态、常量、公共工具与跨模块注册表。
 *
 * 前端按 `module: none` 编译，每个 `.ts` 都是独立的经典脚本，因此这里不使用
 * 模块语法，而是把共享状态和工具挂到 `window.TrpgNarrative`。拆分后的各模块
 * 通过 `NarrativeCore.api` 互相调用，避免加载顺序造成环形依赖。
 */
(function (window: Window): void {
    "use strict";

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

    const runtime: NarrativeRuntime = {
        dirty: false,
        conflictPending: false,
        keywordCandidates: null,
        undoStack: [],
        redoStack: [],
        restoringHistory: false,
    };

    /** 各模块在加载时把自己的能力挂进来；调用点都在页面就绪之后。 */
    const api = {} as NarrativeApi;

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
        api.setGraphHint("");
        api.renderEdgeActions();
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

    function updateStatusBadge(): void {
        if (!state.bundle) return;
        const counts = state.bundle.nodes.length;
        const label = runtime.conflictPending
            ? t("scenario.tree.status_conflict", "版本冲突")
            : !editable()
                ? t("scenario.tree.status_readonly", "只读")
                : runtime.dirty
                    ? t("scenario.tree.status_dirty", "有未保存改动")
                    : t("scenario.tree.status_saved", "已保存");
        const variant: "secondary" | "success" | "warning" | "danger" = runtime.conflictPending
            ? "danger"
            : !editable()
                ? "secondary"
                : runtime.dirty
                    ? "warning"
                    : "success";
        setStatus(
            `${label} · ${t("scenario.tree.revision", "修订")} ${state.revision} · ${counts} ${t("scenario.tree.nodes", "节点")} · ${state.bundle.topology || "hybrid"}`,
            variant,
        );
    }

    /** 未保存改动标记：只影响状态芯片，不阻断任何操作（§8.1.1）。 */
    function markDirty(value: boolean): void {
        if (runtime.dirty === value) return;
        runtime.dirty = value;
        updateStatusBadge();
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

    window.TrpgNarrative = {
        state,
        runtime,
        api,
        KIND_META,
        EDGE_TYPES,
        VISIBILITY_LABEL,
        REVEAL_LABEL,
        byId,
        t,
        escapeHtml,
        isTypingTarget,
        kindMeta,
        editable,
        showTab,
        setStatus,
        setEmpty,
        nodeById,
        parentOf,
        edgeById,
        matchesSearch,
        sortNodes,
        emitSelection,
        splitTerms,
        keywordSourceLabel,
        globalKeywordConflicts,
        reasonLabel,
        visibilitySourceLabel,
        revealOf,
        revealToCondition,
        revealSummary,
        revealBlockMarkup,
        readReveal,
        syncVisibilityFields,
        syncRevealType,
        updateStatusBadge,
        markDirty,
        applyRevision,
        syncRevision,
    };
})(window);
