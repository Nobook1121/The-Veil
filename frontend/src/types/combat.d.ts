/** 战斗轮面板、世界意志直执行的前端共享类型（仅声明，不产生运行时文件）。 */
interface CombatParticipantState {
    name: string;
    user_id?: string | number | null;
    is_player?: boolean;
    side?: string | null;
    dex?: number | null;
    initiative?: number | null;
    hp?: number | null;
    max_hp?: number | null;
    status?: string | null;
    armor?: number | null;
    major_wound?: boolean;
    used_dodge?: boolean;
    used_counter?: boolean;
    surprised?: boolean;
    actions_remaining?: number;
    actions_per_round?: number;
    reactions_this_round?: number;
    ready_weapon?: boolean;
    disengaged?: boolean;
    dying?: boolean;
}

interface CombatWeaponOption {
    name: string;
    skill: string;
    damage: string;
    ranged: boolean;
}

interface CombatTargetOption {
    name: string;
    hp?: number | null;
    status?: string | null;
    side?: string | null;
}

interface CombatLogEntry {
    time: string;
    text: string;
}

interface CombatStatePayload {
    active: boolean;
    round?: number | null;
    turn?: number | null;
    current?: string | null;
    order?: CombatParticipantState[];
    surprised?: string[];
    log?: CombatLogEntry[];
    room_id?: string;
    actor_user_id?: string;
    my_combatant?: string;
    can_act?: boolean;
    can_advance?: boolean;
    is_manager?: boolean;
    pending_roll?: { request_id: string; roller_name?: string; roller_user_id?: string | number; tool_name?: string; state?: string } | null;
    weapons?: CombatWeaponOption[];
    targets?: CombatTargetOption[];
    summary?: string;
}

type CombatActionKind =
    | "attack"
    | "shoot"
    | "maneuver"
    | "next"
    | "end"
    | "delay"
    | "disengage"
    | "reload"
    | "ready"
    | "cast";

interface CombatActionRequest {
    action: CombatActionKind;
    actor?: string;
    target?: string;
    weapon?: string;
    defense?: "dodge" | "counter" | "none";
    maneuver?: string;
    bonus_dice?: number;
    penalty_dice?: number;
    reason?: string;
    spell?: string;
    cast_time?: "instant" | "one_round" | "two_rounds";
    mp_cost?: number;
    san_cost?: number;
}

interface CombatActionResponse {
    request_id?: string;
    plan?: DicePlan;
    skill_label?: string;
    skill_value?: number;
    weapon?: string;
    target?: string;
    immediate?: boolean;
    active?: boolean;
    error?: string;
    round?: number | null;
    current?: string | null;
    order?: CombatParticipantState[];
}

interface WorldOverrideStatus {
    allowed: boolean;
    enabled: boolean;
    user_ids: string[];
    can_configure: boolean;
    members: Array<{ user_id: string; username: string; character_name: string }>;
}

interface Window {
    TrpgCombat?: {
        init(): void;
        refresh(): void;
        onRoomChanged(): void;
        onUpdated(payload: CombatStatePayload): void;
        onDiceResolved(): void;
    };
    TrpgWorldOverride?: {
        init(): void;
        refresh(room?: Room | null): void;
        open(): void;
    };
    sendWorldOverride?: (text: string) => Promise<void>;
}
