/** 骰子动画与玩家投掷的前端共享类型（仅声明，不产生运行时文件）。 */
type DiceAnimationLevel = "text" | "simple" | "3d";

interface DiceRollMeta {
    mode: "percentile" | "standard";
    expression: string;
    count: number;
    sides: number;
    faces: number[];
    labels: string[];
    roll?: number | null;
    rolls: number[];
    candidates: number[];
    bonus_dice: number;
    penalty_dice: number;
    total: number;
    success?: boolean | null;
    success_level?: string | null;
    check_name?: string | null;
    difficulty?: string | null;
    target?: number | null;
    roller?: string | null;
    roller_user_id?: string | number | null;
    request_id?: string | null;
    auto?: boolean;
    show_table?: boolean;
    player_roll?: boolean;
    player_roll_ignored?: boolean;
    faces_urls?: Record<string, string> | null;
}

interface DicePlan {
    mode: "percentile" | "standard";
    expression: string;
    count: number;
    sides: number;
    bonus_dice: number;
    penalty_dice: number;
    candidates: number[];
    check?: { name?: string | null; difficulty?: string | null; target?: number | null } | null;
}

interface DiceResultSnapshot {
    faces: number[];
    labels: string[];
    roll?: number | null;
    rolls: number[];
    total?: number | null;
    success?: boolean | null;
    success_level?: string | null;
    summary?: string;
    check_name?: string | null;
    difficulty?: string | null;
    target?: number | null;
    roller?: string | null;
    mode?: "percentile" | "standard";
    expression?: string;
    count?: number;
    sides?: number;
    candidates?: number[];
    auto?: boolean;
}

interface DiceRequestPayload {
    room_id: string;
    request_id: string;
    tool_name?: string;
    roller?: { user_id: string | number; username: string };
    check?: { name?: string | null; difficulty?: string | null; target?: number | null } | null;
    plan: DicePlan;
    expires_at?: number;
    state?: string;
    result?: DiceResultSnapshot | null;
}

interface DiceRolledPayload extends DiceResultSnapshot {
    room_id?: string;
    request_id?: string;
    auto?: boolean;
    player_roll?: boolean;
    reason?: string;
    operator?: string;
    result?: DiceResultSnapshot | null;
    request?: DiceRequestPayload | null;
}

interface DiceAppearanceConfig {
    body: string;
    text: string;
    table: string;
    edge: string;
    tableSkinId: string;
    diceSkinIds: Record<string, string>;
}

interface DiceTextureAsset {
    id: string;
    name: string;
    kind: "table" | "dice" | "face";
    file: string;
    content_type: string;
    width: number;
    height: number;
    size: number;
    enabled: boolean;
}

interface DiceTableSkin {
    id: string;
    name: string;
    asset_id: string;
    enabled: boolean;
}

interface DiceSkin {
    id: string;
    name: string;
    die_type: string;
    faces: Record<string, string | null>;
    enabled: boolean;
}

interface DiceTextureLibrary {
    assets: DiceTextureAsset[];
    table_skins: DiceTableSkin[];
    dice_skins: DiceSkin[];
}

interface TrpgDiceTableApi {
    init(): void;
    onRoomChanged(): void;
    renderForMessage(messageElement: HTMLElement, message: ChatMessage, animate: boolean): void;
    handleRequest(payload: DiceRequestPayload): void;
    handleRolled(payload: DiceRolledPayload): void;
    handleCancelled(payload: DiceRolledPayload): void;
    handleForced(payload: DiceRolledPayload): void;
    handleRollCommand(force: boolean): Promise<boolean>;
    restorePending(): Promise<void>;
    rollRequest(requestId: string): Promise<void>;
    forceRequest(requestId: string): Promise<boolean>;
    effectiveLevel(): DiceAnimationLevel;
    baseLevel(): DiceAnimationLevel;
}

interface TrpgDice3DApi {
    create(
        container: HTMLElement,
        options: Record<string, unknown>,
    ): {
        roll(snapshot: DiceResultSnapshot): Promise<void>;
        setStatic(snapshot: DiceResultSnapshot): void;
        dispose(): void;
    };
}

interface Window {
    TrpgDiceTable?: TrpgDiceTableApi;
    TrpgDice3D?: TrpgDice3DApi;
    TrpgDiceAppearanceSettings?: {
        init(): void;
        open(): void;
    };
    TrpgAdminDiceTextures?: {
        init(): void;
        refresh(): Promise<void>;
    };
}
