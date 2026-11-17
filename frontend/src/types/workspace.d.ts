/** 规则集 workspace 声明（由 /api/rulesets 的 workspace 字段转述，核心不解释语义）。 */
interface RulesetSidebarSpec {
    position: "left" | "right" | "none";
    width: number;
    min_width: number;
    max_width: number;
    collapsible: boolean;
    collapsed_width: number;
}

interface RulesetTopbarSpec {
    height: number;
    slots: string[];
}

interface RulesetLeftRailSpec {
    width: number;
    visible: boolean;
}

interface RulesetWorkspaceSpec {
    layout_id: string;
    rulesets: string[];
    primary: "canvas" | "conversation" | "custom";
    primary_min_width: number;
    sidebar: RulesetSidebarSpec;
    topbar: RulesetTopbarSpec;
    left_rail: RulesetLeftRailSpec;
    panels: Record<string, string>;
    priority: number;
    detail: Record<string, unknown>;
}

interface RulesetWorkspaceRow {
    id: string;
    display_name?: string;
    workspace?: RulesetWorkspaceSpec | null;
}

/** 地图舞台的最小状态：只做宿主、网格、背景与标记点位置。 */
interface RoomStageMarker {
    id: string;
    label: string;
    x: number;
    y: number;
    color?: string;
    avatar?: string;
    member?: boolean;
}

interface RoomStageState {
    background?: string;
    grid_size?: number;
    // 格线原点（世界像素，相对背景图中心）；旧数据缺省时按 (0, 0) 处理。
    grid_offset?: { x?: number; y?: number };
    markers?: RoomStageMarker[];
}

interface WorkspaceLayoutChoice {
    layout: "canvas" | "conversation";
    scope: "session" | "room" | "default";
}

/** 插件 UI 声明：由 /api/rooms/<id>/ui 按规则集与权限裁剪后下发。 */
interface RoomUiAsset {
    url: string;
    type: string;
}

interface RoomUiSlot {
    plugin_id: string;
    id: string;
    slot: string;
    title_i18n?: string;
    mount: string;
    layout?: Record<string, unknown>;
    priority?: number;
    assets: RoomUiAsset[];
}

interface RoomUiPayload {
    room_id: string;
    ruleset_id: string;
    permissions: string[];
    slots: RoomUiSlot[];
}

interface StagePluginCamera {
    x: number;
    y: number;
    zoom: number;
}

interface StagePluginPoint {
    x: number;
    y: number;
}

interface StagePluginViewport {
    width: number;
    height: number;
    zoom: number;
    gridSize: number;
    gridOffset: { x: number; y: number };
    camera: StagePluginCamera;
}

interface StagePluginRoom {
    id: string;
    name?: string | undefined;
    ruleset_id?: string | undefined;
    members?: RoomMember[] | undefined;
}

interface StagePluginContext {
    room: StagePluginRoom;
    ruleset_id: string;
    user: { id: string; name: string; role: string };
    permissions: string[];
    assets: RoomUiAsset[];
    events: {
        on(type: string, handler: (payload: unknown) => void): () => void;
    };
    getCamera(): StagePluginCamera;
    setCamera(camera: StagePluginCamera): void;
    panBy(dx: number, dy: number): void;
    zoomAt(factor: number, clientX?: number, clientY?: number): void;
    worldToScreen(x: number, y: number): StagePluginPoint;
    screenToWorld(x: number, y: number): StagePluginPoint;
    getViewport(): StagePluginViewport;
    getGridSize(): number;
    setBaseStage(view: {
        background?: string;
        gridSize?: number;
        gridOffset?: { x?: number; y?: number };
    }): void;
    notify(message: string, type?: string): void;
}

interface TrpgPluginInstance {
    destroy?(): void;
}

type TrpgPluginMount = (element: HTMLElement, context: StagePluginContext) => TrpgPluginInstance | void;

interface TrpgPluginModule {
    [mount: string]: TrpgPluginMount | undefined;
}

interface TrpgPluginsRegistry {
    [pluginId: string]: TrpgPluginModule | undefined;
}

interface Window {
    TrpgWorkspace?: {
        init(): void;
        refresh(): void;
        setLayout(choice: WorkspaceLayoutChoice): void;
        toggleSidebar(): void;
        toggleChatOverlay(force?: boolean): void;
        setPanel(panel: "chat" | "character" | "settings"): void;
        isCanvas(): boolean;
    };
    TrpgSocket?: SocketLike | null;
    TrpgStagePlugins?: TrpgPluginsRegistry;
}
