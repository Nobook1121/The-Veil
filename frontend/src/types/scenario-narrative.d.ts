/**
 * 剧本世界树 / 图谱编辑器的共享类型（用于 `frontend/src/app/scenario-narrative/`）。
 *
 * 前端使用 `module: none` 编译，各 `.ts` 是独立的经典脚本，因此类型必须声明在
 * 全局 `.d.ts` 中，供所有拆分后的模块复用。
 */

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
    order?: number;
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
    bidirectional?: boolean;
    kp_notes?: string;
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

interface RepairPayload {
    ok: boolean;
    applied: { code: string; action: string; target: string }[];
    applied_count: number;
    remaining_count: number;
    remaining: ValidationIssue[];
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

interface RevealSpec {
    type: "" | "skill" | "clue" | "clock" | "flag";
    target: string;
    threshold: number;
}

interface KeywordCandidate {
    key: string;
    source: string;
    confidence: number;
}

interface HistoryEntry {
    bundle: GraphBundle;
    revision: number;
}

interface RemoteSnapshot {
    bundle: GraphBundle;
    revision: number;
}

interface PreviewEntry {
    node: GraphNode;
    visibility: "public" | "hidden" | "kp_only";
}

/** 模块级的可变状态（不属于渲染快照，单独放在 runtime 里）。 */
interface NarrativeRuntime {
    dirty: boolean;
    conflictPending: boolean;
    keywordCandidates: { nodeId: string; items: KeywordCandidate[] } | null;
    undoStack: HistoryEntry[];
    redoStack: HistoryEntry[];
    restoringHistory: boolean;
}

/** 跨模块函数注册表：各拆分模块加载时把自己的能力挂到这里。 */
interface NarrativeApi {
    renderTree: () => void;
    renderInspector: () => void;
    renderGraph: () => void;
    renderGraphEditor: () => void;
    renderAll: () => void;
    renderEdgeActions: () => void;
    renderGraphGuide: () => void;
    updateEdgeGeometry: () => void;
    openNodeCreate: () => void;
    submitNodeCreate: () => Promise<void>;
    deleteNode: () => Promise<void>;
    setMode: (mode: GraphMode) => void;
    setViewMode: (mode: "kp" | "player") => void;
    connectNodes: (sourceId: string, targetId: string, options?: { type?: string; hard?: boolean }) => Promise<void>;
    selectEdge: (edgeId: string, scroll?: boolean) => void;
    saveGraphNode: () => Promise<void>;
    addGraphEdge: () => Promise<void>;
    setGraphHint: (text: string) => void;
    fitView: () => void;
    resetViewMode: () => void;
    viewportSize: () => { width: number; height: number };
    applyView: () => void;
    defaultPositions: (bundle: GraphBundle) => Map<string, { x: number; y: number }>;
    modeHint: (mode: GraphMode) => string;
    playerHint: () => string;
    selectNode: (nodeId: string) => void;
    moveNode: (nodeId: string, parentId: string) => Promise<void>;
    bindTreeEditing: () => void;
    saveNode: () => Promise<void>;
    extractNodeKeywords: () => Promise<void>;
    applyKeywordCandidate: (index: number) => void;
    pushHistory: () => HistoryEntry | null;
    discardHistory: (entry: HistoryEntry | null) => void;
    resetHistory: () => void;
    updateHistoryButtons: () => void;
    undoHistory: () => Promise<void>;
    redoHistory: () => Promise<void>;
    handleConflict: () => Promise<void>;
    load: (scenarioId: number | string, options?: { readonly?: boolean }) => Promise<void>;
    saveEdge: () => Promise<void>;
    toggleEdgeHard: () => Promise<void>;
    deleteSelectedEdge: (confirmFirst?: boolean) => Promise<void>;
    autoLayout: () => Promise<void>;
    reclassifyVisibility: () => Promise<void>;
    bindGraphEvents: () => void;
    openPlayerPreview: () => void;
    openOpeningEditor: () => Promise<void>;
    saveOpening: () => Promise<void>;
    previewOpening: () => void;
    runValidation: () => Promise<void>;
    repairGraph: () => Promise<void>;
    explainWithAgent: () => void;
    updatePublishGuard: (count: number) => void;
    acceptReviewIssue: (target: string) => Promise<void>;
    resetGeneratePanel: () => void;
    syncGeneratePanel: () => void;
    syncGenerateSource: () => void;
    startGeneration: () => Promise<void>;
    cancelGeneration: () => Promise<void>;
    applyGeneration: () => Promise<void>;
}

/** `window.TrpgNarrative`：共享状态、常量与公共工具。 */
interface NarrativeCore {
    state: NarrativeState;
    runtime: NarrativeRuntime;
    api: NarrativeApi;
    KIND_META: Record<string, { label: string; icon: string; color: string; order: number }>;
    EDGE_TYPES: Array<[string, string]>;
    EDGE_META: Record<string, { label: string; desc: string; movement: boolean; directed: boolean; color: string }>;
    VISIBILITY_LABEL: Record<string, string>;
    REVEAL_LABEL: Record<string, string>;
    byId<T extends HTMLElement = HTMLElement>(id: string): T | null;
    t(key: string, fallback: string): string;
    escapeHtml(value: unknown): string;
    isTypingTarget(target: EventTarget | null): boolean;
    kindMeta(kind: string): { label: string; icon: string; color: string; order: number };
    edgeMeta(type: string | undefined): { label: string; desc: string; movement: boolean; directed: boolean; color: string };
    edgeText(edge: GraphEdge): string;
    editable(): boolean;
    showTab(tab: "basic" | "modules" | "tree" | "graph" | "validate" | "generate"): void;
    setStatus(text: string, variant?: "secondary" | "success" | "warning" | "danger"): void;
    setEmpty(message: string): void;
    nodeById(nodeId: string): GraphNode | null;
    parentOf(node: GraphNode): string;
    edgeById(edgeId: string): GraphEdge | null;
    matchesSearch(node: GraphNode): boolean;
    sortNodes(nodes: GraphNode[]): GraphNode[];
    emitSelection(node: GraphNode | null): void;
    splitTerms(value: string | undefined): string[];
    keywordSourceLabel(source: string): string;
    globalKeywordConflicts(node: GraphNode): Array<{ key: string; others: string[] }>;
    reasonLabel(raw: string): string;
    visibilitySourceLabel(item: {
        visibility_source?: string;
        visibility_confidence?: number;
        visibility_reasons?: string[];
    }): string;
    revealOf(value: unknown): RevealSpec;
    revealToCondition(spec: RevealSpec): Record<string, unknown> | null;
    revealSummary(value: unknown): string;
    revealBlockMarkup(prefix: string, value: unknown, visibility: string): string;
    readReveal(prefix: string): RevealSpec;
    syncVisibilityFields(prefix: string, visibility: string): void;
    syncRevealType(prefix: string): void;
    updateStatusBadge(): void;
    markDirty(value: boolean): void;
    applyRevision(revision: number): void;
    syncRevision(revision: number): void;
}

interface Window {
    TrpgNarrative?: NarrativeCore;
}
