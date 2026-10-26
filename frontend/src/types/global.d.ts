interface Window {
    restoreThinkingState?: () => void;
    resumePendingAIRequest?: () => void;
    startScenario?: () => Promise<void>;
    TRPG?: TrpgNamespace;
    TrpgApi: TrpgApiClient;
    TrpgDom: TrpgDomClient;
    TrpgTemplates: TrpgTemplateRenderer;
    ScenarioModel: ScenarioModelConstructor;
    ScenarioView: ScenarioViewConstructor;
    ScenarioController: { new(): unknown };
    ScenarioNarrative?: {
        load: (scenarioId: number | string, options?: { readonly?: boolean }) => Promise<void>;
        clear: () => void;
        init: () => void;
    };
    DiceTool: DiceToolConstructor;
    CheckTool: CheckToolConstructor;
    ToolManager: ToolManagerConstructor;
    toolManager?: ToolManager;
    currentRoom?: Room | null;
    uploadedCoverFile?: File;
    currentPlatform?: string;
    currentTestingPlatform?: string;
    loadAIRoles?: () => void;
    reloadRoleConfigs?: () => Promise<void>;
    showNotification?: (message: string, type?: string) => void;
    recordCharacterChange?: (payload: Record<string, unknown>) => Promise<unknown>;
    TrpgCookies?: TrpgCookieClient;
    configManager: ConfigManager;
    aiPlatformManager: AIPlatformManager;
    testRequestConfigs: Record<string, TestRequestConfig>;
    getTestRequestConfig(modelId: string): TestRequestConfig;
    marked?: MarkedParser;
    DOMPurify?: DomPurifySanitizer;
    renderChatMessages?: (messages: ChatMessage[]) => void;
    getCurrentChatMessages?: () => ChatMessage[];
    persistLocalDiceMessage?: (content: string, metadata: Record<string, unknown>) => Promise<ChatMessage | null>;
    clearChatMessages?: () => void;
    setChatReadOnly?: (readOnly: boolean) => void;
    refreshRoomArchiveUi?: () => void;
    openRoomConsole?: (roomId: string, options?: RoomConsoleOpenOptions) => void;
    appendRoomConsoleEntry?: (payload?: RoomConsoleSocketPayload) => void;
    closeRoomConsole?: () => void;
    refreshCurrentRoomMembers?: () => Promise<void>;
    refreshRoomNodes?: () => Promise<void>;
    refreshRoomUsage?: () => Promise<void>;
    reloadRoomMessages?: () => Promise<void>;
    joinSocketRoom?: (roomId: string) => void;
    leaveSocketRoom?: (roomId: string) => void;
    reconnectSocket?: () => void;
    disconnectSocket?: () => void;
    initRoomManagement?: () => void;
    loadRoomsList?: () => Promise<void>;
    autoLoadLastRoom?: () => Promise<void>;
    clearCurrentRoom?: () => void;
    clearCharacterManagement?: () => void;
    reloadCharacterManagement?: () => Promise<void>;
    switchMainTab?: (tabId: string, options?: { clearNav?: boolean }) => void;
    refreshAdminNavigation?: () => void;
    refreshGeneralSettingsBaseline?: () => void;
    refreshScenarioManagement?: () => void;
    openCreateRoomWithScenario?: (scenarioId: number) => Promise<void>;
    TrpgQr?: { renderSvg: (text: string, options?: { cellSize?: number; margin?: number; alt?: string }) => string };
    TrpgDialog?: {
        confirm: (options: TrpgDialogOptions) => Promise<boolean>;
        prompt: (options: TrpgDialogOptions & { defaultValue?: string }) => Promise<string | null>;
    };
    TrpgSettingsAssistant?: {
        init: () => void;
        runSearch: () => void;
    };
    TrpgAdminDiceTextures?: {
        init: () => void;
        refresh: () => Promise<void>;
    };
}

interface TrpgDialogOptions {
    title?: string;
    message: string;
    confirmText?: string;
    cancelText?: string;
    danger?: boolean;
}

declare function showNotification(message: string, type?: string): void;
declare function showAuthModal(): void;
declare const TrpgCookies: TrpgCookieClient;
declare function initAuth(): Promise<boolean>;
declare const TrpgApi: TrpgApiClient;
declare const TrpgDom: TrpgDomClient;
