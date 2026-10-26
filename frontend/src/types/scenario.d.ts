type ScenarioTriggerContentMode = "text" | "richtext" | "image" | "file";
type TriggerResourceType = "image" | "video" | "audio" | "richtext" | "file";
type TriggerVisibility = "kp_only" | "player_visible";

interface ResourceRef {
    type: TriggerResourceType;
    path: string;
    url: string;
    alt: string;
    mime: string;
    size: number;
    hash: string;
    content?: string;
}

interface TriggerCondition {
    type: "scene_enter" | "event_triggered" | "clue_found" | "npc_dialogue" | "player_action" | "custom";
    sceneId?: string;
    eventId?: string;
    clueId?: string;
    npcId?: string;
    keyword?: string;
    naturalLanguage?: string;
}

interface Attachment {
    triggerId: string;
    resourceRef: ResourceRef;
    condition: TriggerCondition;
    relatedCards?: string[];
    spoilerLevel: number;
    visibility: TriggerVisibility;
    repeatable: boolean;
    priority: number;
    enabled: boolean;
    note?: string;
}

interface TriggerCard {
    id: string;
    scriptId: string;
    scriptVersion: string;
    cardType: "trigger";
    sceneId?: string;
    spoilerLevel: number;
    visibility: TriggerVisibility;
    unlockCondition?: string;
    text: string;
    attachments: Attachment[];
    metadata: Record<string, unknown>;
}
type ScenarioModuleType = "opening" | "background" | "public_info" | "preparation" | "timeline" | "scene" | "ending" | "monster" | "npc" | "custom";

interface ScenarioModuleTimelineEntry {
    id: string;
    time_point: string;
    event: string;
}

interface ScenarioModuleInputItem {
    id: string;
    label: string;
    value: string;
    send_to_ai: boolean;
}

interface ScenarioModuleWeaponRow {
    name: string;
    skill: string;
    damage: string;
    range: string;
    attacks: string;
    ammo: string;
    malfunction: string;
    note: string;
}

interface ScenarioModuleSkillRow {
    name: string;
    base: string;
}

interface ScenarioTrigger {
    id: number;
    display_name: string;
    keyword: string;
    condition: string;
    content_mode: ScenarioTriggerContentMode;
    content?: string | undefined;
    asset_name?: string | undefined;
    asset_mime?: string | undefined;
    asset_size?: number | undefined;
    asset_path?: string | undefined;
    asset_url?: string | undefined;
    asset_data_url?: string | undefined;
    spoiler_level?: number | undefined;
    visibility?: TriggerVisibility | undefined;
    repeatable?: boolean | undefined;
    priority?: number | undefined;
    enabled?: boolean | undefined;
}

interface ScenarioModule {
    id: string;
    module_type: ScenarioModuleType;
    title: string;
    summary: string;
    content?: string | undefined;
    notes?: string | undefined;
    visibility?: "public" | "kp" | undefined;
    send_to_ai?: boolean | undefined;
    code?: string | undefined;
    scene_id?: number | undefined;
    ending_id?: number | undefined;
    triggers?: ScenarioTrigger[] | undefined;
    attachments?: Attachment[] | undefined;
    timeline_entries?: ScenarioModuleTimelineEntry[] | undefined;
    open_ending?: boolean | undefined;
    fixed_opening?: boolean | undefined;
    inputs?: ScenarioModuleInputItem[] | undefined;
    attributes?: Record<string, string> | undefined;
    battle?: Record<string, string> | undefined;
    skills?: ScenarioModuleSkillRow[] | undefined;
    weapons?: ScenarioModuleWeaponRow[] | undefined;
}

interface Scenario {
    id: number;
    scenario_version?: string;
    title: string;
    author: string;
    playerCount: number;
    notes?: string;
    allow_open_ending?: boolean;
    modules?: ScenarioModule[];
    cover?: string;
    owner_id?: string | number;
    public_id?: string;
    creator_username?: string;
    createdAt?: string;
    updatedAt?: string;
    user_id?: string | number;
    trigger_cards?: TriggerCard[];
    /** 文档「直接导入」的剧本不含可编辑场景卡，此标记用于前端禁用编辑。 */
    import_mode?: "direct" | "review" | string;
}

type ScenarioInput = Omit<Scenario, "id" | "createdAt" | "updatedAt" | "owner_id"> & {
    id?: number;
};

interface ScenarioModelConstructor {
    new(): ScenarioModel;
}

interface ScenarioModel {
    scenarios: Scenario[];
    apiBaseUrl: string;
    userId: string | number | null;
    isAuthenticated: boolean;
    getCurrentUserId(): string | number | null;
    checkAuthStatus(): Promise<boolean>;
    init(): Promise<Scenario[]>;
    loadScenarios(): Promise<Scenario[]>;
    createScenario(scenarioData: ScenarioInput): Promise<Scenario>;
    updateScenario(id: number, scenarioData: ScenarioInput): Promise<Scenario>;
    deleteScenario(id: number): Promise<boolean>;
    getScenario(id: number): Scenario | undefined;
    getScenarios(): Scenario[];
    saveScenarios(): void;
    importScenario(scenarioData: unknown): Promise<Scenario>;
    convertScript(text: string, title?: string): Promise<ScenarioInput>;
    convertScriptFile(file: File, title?: string): Promise<ScenarioInput>;
    getKnowledgeStats(id: number): Promise<{ scenario_id: number; version: string; vector_count: number; path: string; backend: string }>;
    createImportJob(formData: FormData, onProgress?: (value: number) => void): Promise<ScenarioImportStart>;
    getImportJob(id: string): Promise<ScenarioImportJob>;
    publishImport(scriptId: number, jobId: string): Promise<Scenario>;
    waitForImportJob(id: string, onProgress?: (job: ScenarioImportJob) => void): Promise<ScenarioImportJob>;
    loadDraft(): Promise<ScenarioInput | null>;
    saveDraft(scenarioData: ScenarioInput): Promise<ScenarioInput>;
    discardDraft(): Promise<void>;
    validateScenarioData(data: unknown): data is ScenarioInput;
    listTriggerAssets(id: number): Promise<ResourceRef[]>;
    uploadTriggerAssets(id: number, files: File[], alt: string[]): Promise<ResourceRef[]>;
    updateTriggerAsset(id: number, assetId: string, patch: Partial<ResourceRef>): Promise<ResourceRef>;
    deleteTriggerAsset(id: number, assetId: string): Promise<void>;
    createTriggerCard(id: number, card: Omit<TriggerCard, "scriptId" | "scriptVersion">): Promise<TriggerCard>;
}

interface ScenarioViewHandlers {
    onCreateScenarioClick(): void;
    onImportScenarioDocumentClick(): Promise<void>;
    onImportScenarioDocument(file: File): Promise<void>;
    onSaveScenario(): Promise<void>;
    onSaveDraft(): Promise<void>;
    onDiscardDraft(): Promise<void>;
    onPreviewScenario(id: number): void;
    onEditScenario(id: number): void;
    onPlayScenario(id: number): void;
    onDeleteScenario(id: number): Promise<void>;
    onBatchDeleteScenarios(ids: number[]): Promise<void>;
    onImportScenario(files: FileList | null): Promise<void>;
}

interface ScenarioViewConstructor {
    new(): ScenarioView;
}

interface ScenarioView {
    scenarioList: HTMLElement;
    saveScenarioHandler: () => Promise<void>;
    setEventHandlers(handlers: ScenarioViewHandlers): void;
    renderScenarioList(scenarios: Scenario[]): void;
    openCreateModal(): Promise<void>;
    openEditModal(scenario: Scenario): void;
    fillDraftData(draft: ScenarioInput): void;
    setImportReviewReadOnly(readonly: boolean): void;
    showDraftPrompt(): Promise<"continue" | "discard" | "cancel">;
    closeModal(): void;
    previewScenario(scenario: Scenario, knowledge?: { vector_count?: number; path?: string; backend?: string }): void;
    showImportChoice(): Promise<"edit" | "direct" | "cancel">;
    showDirectImportMetadata(defaults: { title: string; creator: string }): Promise<{ title: string; author: string; creator: string; playerCount: number; description: string } | null>;
    getFormData(): ScenarioInput;
    showMessage(message: string, isError?: boolean): void;
    showConversionProgress(fileName: string): void;
    updateConversionProgress(stage: number, state: "pending" | "active" | "complete" | "error", detail?: string): void;
    updateImportJobProgress(job: ScenarioImportJob): void;
    closeConversionProgress(): void;
}

type ScenarioImportStatus = "pending" | "parsing" | "chunking" | "extracting" | "merging" | "carding" | "summarizing" | "embedding" | "done" | "failed" | "cancelled" | "published";
interface ScenarioImportJob { id: string; script_id: number; status: ScenarioImportStatus; progress: number; current_stage: string; stage_progress: number; stage_meta?: Record<string, unknown>; error?: string; preview?: ScenarioInput; }
// POST /api/scripts/import 的返回：创建任务后用于轮询与发布。
interface ScenarioImportStart { jobId: string; scriptId: number; }

// 剧本各章节的世界书字段（触发词/常驻/分层等），由 /api/scripts/<id>/knowledge 返回。
interface KnowledgeSectionInfo {
    section_key: string;
    title: string;
    chunk_ids: string[];
    preview: string;
    keywords: string[];
    secondary_keywords: string[];
    visibility: string;
    keywords_visibility: string;
    keyword_spoiler: boolean;
    secondary_logic: string;
    is_constant: boolean;
    tier: string;
    probability: number;
    group: string;
    group_weight: number;
    priority: number;
    order: number;
    trigger_chunks: boolean;
    sticky_rounds: number;
    cooldown_rounds: number;
    delay_rounds: number;
}
