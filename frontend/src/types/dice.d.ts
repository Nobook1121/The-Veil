/** 骰子动画与玩家投掷的前端共享类型（仅声明，不产生运行时文件）。 */
type DiceAnimationLevel = "text" | "simple" | "3d";

interface DiceGroupSpecLike {
    role: string;
    type: string;
    count: number;
    faces?: number[];
    labels?: string[];
}

interface DiceRerollState {
    enabled: boolean;
    mode: "off" | "limited" | "unlimited";
    limit: number;
    used: number;
    /** 剩余次数：0 关闭、-1 无限、其余为剩余次数。 */
    remaining: number;
}

interface DiceRollMeta {
    mode: "percentile" | "standard";
    expression: string;
    count: number;
    sides: number;
    faces: number[];
    labels: string[];
    groups?: DiceGroupSpecLike[];
    selected?: { group: number; index: number } | null;
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
    roller?: string | { user_id?: string | number | null; username?: string } | null;
    roller_user_id?: string | number | null;
    request_id?: string | null;
    auto?: boolean;
    show_table?: boolean;
    player_roll?: boolean;
    player_roll_ignored?: boolean;
    faces_urls?: Record<string, string> | null;
    reroll?: DiceRerollState | null;
    confirmed?: boolean;
}

interface DicePlan {
    mode: "percentile" | "standard";
    expression: string;
    count: number;
    sides: number;
    bonus_dice: number;
    penalty_dice: number;
    candidates: number[];
    groups?: DiceGroupSpecLike[];
    selected?: { group: number; index: number } | null;
    check?: { name?: string | null; difficulty?: string | null; target?: number | null } | null;
}

interface DiceResultSnapshot {
    faces: number[];
    labels: string[];
    groups?: DiceGroupSpecLike[];
    selected?: { group: number; index: number } | null;
    roll?: number | null;
    rolls: number[];
    total?: number | null;
    success?: boolean | null;
    success_level?: string | null;
    summary?: string;
    check_name?: string | null;
    difficulty?: string | null;
    target?: number | null;
    roller?: string | { user_id?: string | number | null; username?: string } | null;
    mode?: "percentile" | "standard";
    expression?: string;
    count?: number;
    sides?: number;
    candidates?: number[];
    auto?: boolean;
    reroll?: DiceRerollState | null;
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
    reroll?: DiceRerollState | null;
    /** 物理结果：骰子落定后由客户端读取朝上的面。 */
    physics?: boolean;
    /** AI 自动投掷：所有客户端无需点击直接播放。 */
    auto?: boolean;
    seed?: number | null;
    reroll_index?: number;
}

interface DiceRolledPayload extends DiceResultSnapshot {
    room_id?: string;
    request_id?: string;
    auto?: boolean;
    player_roll?: boolean;
    reason?: string;
    operator?: string;
    confirmed_by?: string;
    state?: string;
    reroll?: DiceRerollState | null;
    result?: DiceResultSnapshot | null;
    request?: DiceRequestPayload | null;
    /** 结果来自服务端旧生成器（纯文本/超时/强制），前端应静态还原。 */
    fallback?: boolean;
    seed?: number | null;
    reroll_index?: number;
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
    handleRolling(payload: DiceRequestPayload): void;
    handleRolled(payload: DiceRolledPayload): void;
    handleRerolled(payload: DiceRolledPayload): void;
    handleConfirmed(payload: DiceRolledPayload): void;
    handleCancelled(payload: DiceRolledPayload): void;
    handleForced(payload: DiceRolledPayload): void;
    handleRollCommand(mode: "roll" | "force" | "confirm"): Promise<boolean>;
    handleDiceTableCommand(expression: string): Promise<boolean>;
    restorePending(): Promise<void>;
    rollRequest(requestId: string): Promise<void>;
    rerollRequest(requestId: string): Promise<void>;
    confirmRequest(requestId: string): Promise<void>;
    forceRequest(requestId: string): Promise<boolean>;
    effectiveLevel(): DiceAnimationLevel;
    baseLevel(): DiceAnimationLevel;
}

interface TrpgDice3DApi {
    create(
        container: HTMLElement,
        options: Record<string, unknown>,
    ): {
        roll(snapshot: DiceResultSnapshot, seed?: number | null): Promise<number[]>;
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
