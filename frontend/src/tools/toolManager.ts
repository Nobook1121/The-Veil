interface InterludeCommandData {
    state?: RoomInterludeState;
    settlement?: Record<string, unknown>;
    reviews?: Array<Record<string, unknown>>;
    kp_prompt?: string;
}

class ToolManager {
    private readonly tools: {
        dice: DiceTool;
        check: CheckTool;
    };

    private readonly commands: Record<string, (command: string) => string | Promise<string>>;
    private interludePrompt: string | null = null;

    constructor() {
        const dice = new DiceTool();
        this.tools = {
            dice,
            check: new CheckTool(dice),
        };
        this.commands = {
            "/dice": this.handleDiceCommand.bind(this),
            "/check": this.handleCheckCommand.bind(this),
            "/sc": this.handleSanityCommand.bind(this),
            "/item": this.handleItemCommand.bind(this),
            "/skill": this.handleSkillCommand.bind(this),
            "/attr": this.handleAttributeCommand.bind(this),
            "/interlude": this.handleInterludeCommand.bind(this),
        };
    }

    consumeInterludePrompt(): string | null {
        const prompt = this.interludePrompt;
        this.interludePrompt = null;
        return prompt;
    }

    handleDiceCommand(command: string): string {
        return this.tools.dice.handleDiceCommand(command);
    }

    handleCheckCommand(command: string): string {
        return this.tools.check.handleCheckCommand(command);
    }

    // /sc 玩家名 成功变化/失败变化 [no-madness] [reason 文本]：
    // 由后端结算 SAN 并留痕（不依赖 AI KP），一次损失 ≥5 时自动接疯狂判定。
    async handleSanityCommand(command: string): Promise<string> {
        const parsed = parseCommandTokens(command);
        const sanChange = parsed.positionals[1] || "";
        if (parsed.positionals.length < 2 || !sanChange.includes("/")) {
            return "格式：/sc 玩家名 成功变化/失败变化 [no-madness] [reason 文本]";
        }
        const roomId = window.currentRoom?.id;
        if (!roomId) return "当前不在房间内，无法结算理智检定";
        try {
            const result = await TrpgApi.post<ApiResponse<RuleCommandData>>(
                `/api/rooms/${encodeURIComponent(roomId)}/rules/sanity-check`,
                {
                    player_name: parsed.positionals[0],
                    san_change: sanChange,
                    auto_madness: !parsed.flags.has("no-madness"),
                    reason: parsed.options.reason || "玩家命令",
                },
            );
            if (!result.success) return result.message || "理智检定失败";
            await window.refreshCurrentRoomMembers?.();
            return result.data?.summary || "理智检定完成";
        } catch (error) {
            return `理智检定失败：${String((error as Error)?.message || error)}`;
        }
    }

    // /skill [玩家名] <技能名> [值|±N] [persist] [reason 文本]
    // 不带数值时查看；默认操作为本人，指定他人需房主 / 房间管理员。
    async handleSkillCommand(command: string): Promise<string> {
        return this.handleStatCommand(command, "skill");
    }

    // /attr [玩家名] <属性> [值|±N] [persist] [reason 文本]
    async handleAttributeCommand(command: string): Promise<string> {
        return this.handleStatCommand(command, "attr");
    }

    private async handleStatCommand(command: string, kind: "skill" | "attr"): Promise<string> {
        const parsed = parseCommandTokens(command);
        const label = kind === "skill" ? "技能" : "属性";
        const resource = kind === "skill" ? "skills" : "attributes";
        const usage = kind === "skill"
            ? "格式：/skill [玩家名] <技能名> [值|±N] [persist] [reason 文本]"
            : "格式：/attr [玩家名] <属性> [值|±N] [persist] [reason 文本]";
        const roomId = window.currentRoom?.id;
        if (!roomId) return `当前不在房间内，无法查看或修改${label}`;

        const parts = [...parsed.positionals];
        const valueToken = parts.length ? parts[parts.length - 1] ?? "" : "";
        const relative = /^[+-]\d+$/.test(valueToken);
        const absolute = /^\d+$/.test(valueToken);
        if (relative || absolute) parts.pop();

        let playerName = "";
        if (parts.length > 1 && isKnownMember(parts[0] || "")) playerName = parts.shift() || "";
        const name = parts.join(" ").trim();
        const url = `/api/rooms/${encodeURIComponent(roomId)}/rules/${resource}`;

        if (relative || absolute) {
            if (!name) return usage;
            const amount = Number(valueToken);
            try {
                const result = await TrpgApi.post<ApiResponse<StatCommandData>>(url, {
                    player_name: playerName || undefined,
                    name,
                    value: absolute ? amount : undefined,
                    delta: relative ? amount : undefined,
                    persist: parsed.flags.has("persist"),
                    reason: parsed.options.reason || "玩家命令",
                });
                if (!result.success) return result.message || `${label}修改失败`;
                await window.refreshCurrentRoomMembers?.();
                const data = result.data;
                const target = data?.name || data?.key || data?.skill || name;
                const delta = data?.delta ?? 0;
                const summary = `${data?.player_name || "角色"} 的${label}「${target}」：${data?.before ?? "?"} → ${data?.after ?? "?"}（${delta >= 0 ? "+" : ""}${delta}）`;
                const suffix = data?.persisted ? "（已写回持久角色卡）" : "";
                const persistError = data?.persist_error ? `\n写回角色卡失败：${data.persist_error}` : "";
                return `${summary}${suffix}${persistError}`;
            } catch (error) {
                return `${label}修改失败：${String((error as Error)?.message || error)}`;
            }
        }

        const query = new URLSearchParams();
        if (playerName) query.set("player_name", playerName);
        if (name) query.set("name", name);
        const suffix = query.toString() ? `?${query.toString()}` : "";
        try {
            const result = await TrpgApi.get<ApiResponse<StatViewData>>(`${url}${suffix}`);
            if (!result.success) return result.message || `查看${label}失败`;
            return formatStatView(result.data, kind);
        } catch (error) {
            return `查看${label}失败：${String((error as Error)?.message || error)}`;
        }
    }

    // /item                        查看本人物品与武器
    // /item list [玩家名]          同上，可指定他人
    // /item rm [玩家名] <物品名> [qty N] [weapon] [persist] [reason 文本]
    // /item [玩家名] <物品名> [qty N] [weapon] [persist] [note 文本]
    // 默认只写本房间快照；persist 才写回持久角色卡。
    async handleItemCommand(command: string): Promise<string> {
        const parsed = parseCommandTokens(command);
        const parts = [...parsed.positionals];
        const sub = (parts[0] || "").toLowerCase();
        const roomId = window.currentRoom?.id;
        if (!roomId) return "当前不在房间内，无法处理物品";
        const url = `/api/rooms/${encodeURIComponent(roomId)}/rules/items`;

        if (!sub || sub === "list" || sub === "ls") {
            if (sub) parts.shift();
            const playerName = parts.length && isKnownMember(parts[0] || "") ? parts[0] : "";
            const query = playerName ? `?${new URLSearchParams({ player_name: playerName }).toString()}` : "";
            try {
                const result = await TrpgApi.get<ApiResponse<ItemViewData>>(`${url}${query}`);
                if (!result.success) return result.message || "查看物品失败";
                return formatItemView(result.data);
            } catch (error) {
                return `查看物品失败：${String((error as Error)?.message || error)}`;
            }
        }

        if (["rm", "remove", "del", "delete"].includes(sub)) {
            parts.shift();
            const playerName = parts.length > 1 && isKnownMember(parts[0] || "") ? parts.shift() || "" : "";
            const name = parts.join(" ").trim();
            if (!name) return "格式：/item rm [玩家名] <物品名> [qty N] [weapon] [persist]";
            try {
                const result = await TrpgApi.del<ApiResponse<ItemCommandData>>(url, {
                    body: {
                        player_name: playerName || undefined,
                        name,
                        kind: parsed.flags.has("weapon") ? "weapon" : "",
                        quantity: Number(parsed.options.qty || 1),
                        reason: parsed.options.reason || "玩家命令",
                        persist: parsed.flags.has("persist"),
                    },
                });
                if (!result.success) return result.message || "移除物品失败";
                await window.refreshCurrentRoomMembers?.();
                const data = result.data;
                const summary = `${data?.player_name || "角色"} 移除${data?.kind === "weapon" ? "武器" : "物品"}「${data?.item?.name || name}」×${data?.quantity ?? 1}`;
                const suffix = data?.persisted ? "（已写回持久角色卡）" : "";
                const persistError = data?.persist_error ? `\n写回角色卡失败：${data.persist_error}` : "";
                return `${summary}${suffix}${persistError}`;
            } catch (error) {
                return `移除物品失败：${String((error as Error)?.message || error)}`;
            }
        }

        const playerName = parts.length > 1 && isKnownMember(parts[0] || "") ? parts.shift() || "" : "";
        const name = parts.join(" ").trim();
        if (!name) {
            return "格式：/item [玩家名] <物品名> [qty N] [weapon] [persist] [note 文本]；查看用 /item list";
        }
        try {
            const result = await TrpgApi.post<ApiResponse<RuleCommandData>>(url, {
                player_name: playerName || undefined,
                name,
                kind: parsed.flags.has("weapon") ? "weapon" : "item",
                quantity: Number(parsed.options.qty || 1),
                note: parsed.options.note || "",
                reason: parsed.options.reason || "玩家命令",
                persist: parsed.flags.has("persist"),
            });
            if (!result.success) return result.message || "添加物品失败";
            await window.refreshCurrentRoomMembers?.();
            const data = result.data;
            const summary = data?.player_name && data?.item?.name
                ? `${data.player_name} 获得${data.kind === "weapon" ? "武器" : "物品"}「${data.item.name}」×${data.quantity ?? 1}`
                : "物品已添加";
            const suffix = data?.persisted ? "（已写回持久角色卡）" : "";
            const persistError = data?.persist_error ? `\n写回角色卡失败：${data.persist_error}` : "";
            return `${summary}${suffix}${persistError}`;
        } catch (error) {
            return `添加物品失败：${String((error as Error)?.message || error)}`;
        }
    }

    // /interlude status|request|start|settle|review|end [--yes] [--credit=玩家:事件]
    async handleInterludeCommand(command: string): Promise<string> {
        const parsed = parseCommandTokens(command);
        const sub = (parsed.positionals[0] || "status").toLowerCase();
        const roomId = window.currentRoom?.id;
        if (!roomId) return "当前不在房间内，无法操作幕间";
        const base = `/api/rooms/${encodeURIComponent(roomId)}/interlude`;
        try {
            if (sub === "status") {
                const result = await TrpgApi.get<ApiResponse<InterludeCommandData>>(base);
                if (!result.success) return result.message || "读取幕间状态失败";
                return formatInterludeData(result.data);
            }
            if (sub === "request") {
                const reason = parsed.positionals.slice(1).join(" ").trim();
                const result = await TrpgApi.post<ApiResponse<InterludeCommandData>>(`${base}/request`, { reason });
                if (!result.success) return result.message || "幕间提议失败";
                return `已登记幕间提议，等待房主 / 管理员确认。${reason ? `\n理由：${reason}` : ""}`;
            }
            if (sub === "start") {
                const creditEvents = parseCreditEvents(parsed.options.credit);
                const result = await TrpgApi.post<ApiResponse<InterludeCommandData>>(`${base}/start`, {
                    credit_events: creditEvents,
                });
                if (!result.success) return result.message || "进入幕间失败";
                this.interludePrompt = result.data?.kp_prompt
                    || "（系统）房间已进入幕间。请只做复盘与点评：先给一段简短的点评和结语，不要推进剧情。";
                await window.refreshCurrentRoomMembers?.();
                return `已进入幕间并完成结算。\n${formatInterludeData(result.data)}`;
            }
            if (sub === "settle") {
                const creditEvents = parseCreditEvents(parsed.options.credit);
                const result = await TrpgApi.post<ApiResponse<InterludeCommandData>>(`${base}/settle`, {
                    credit_events: creditEvents,
                });
                if (!result.success) return result.message || "幕间结算失败";
                await window.refreshCurrentRoomMembers?.();
                return `幕间结算完成。\n${formatInterludeData(result.data)}`;
            }
            if (sub === "review") {
                const content = parsed.options.text || parsed.positionals.slice(1).join(" ").trim();
                if (!content) return "格式：/interlude review <复盘内容>";
                const result = await TrpgApi.post<ApiResponse<InterludeCommandData>>(`${base}/review`, {
                    content,
                    topic: parsed.options.topic || "",
                });
                if (!result.success) return result.message || "保存复盘失败";
                return "复盘已保存。";
            }
            if (sub === "end") {
                if (!parsed.flags.has("yes") && !parsed.flags.has("confirm")) {
                    return "结束幕间会自动归档房间。确认后请输入：/interlude end --yes";
                }
                const result = await TrpgApi.post<ApiResponse<InterludeCommandData>>(`${base}/end`, { confirm: true });
                if (!result.success) return result.message || "结束幕间失败";
                return "幕间已结束并完成归档。";
            }
            return "格式：/interlude [status|request|start|settle|review|end] [--yes] [--credit=玩家:事件]";
        } catch (error) {
            return `幕间操作失败：${String((error as Error)?.message || error)}`;
        }
    }

    handleCommand(command: string): string | Promise<string> | null {
        if (!command.startsWith("/")) return null;
        const commandName = (command.split(" ")[0] || "").toLowerCase();
        const handler = this.commands[commandName];
        return handler ? handler(command) : "\u672a\u77e5\u547d\u4ee4\uff0c\u8bf7\u67e5\u770b\u53ef\u7528\u547d\u4ee4\u5217\u8868";
    }

    recordCharacterChange(payload: Record<string, unknown>): Promise<unknown> {
        if (typeof window.recordCharacterChange === "function") {
            return window.recordCharacterChange(payload);
        }
        return Promise.resolve(null);
    }
}

window.ToolManager = ToolManager;

interface ParsedCommandTokens {
    positionals: string[];
    flags: Set<string>;
    options: Record<string, string>;
}

interface RuleCommandData {
    summary?: string;
    player_name?: string;
    kind?: string;
    quantity?: number;
    item?: { name?: string };
    persisted?: boolean;
    persist_error?: string;
}

interface StatCommandData extends RuleCommandData {
    name?: string;
    key?: string;
    skill?: string;
    before?: number;
    after?: number;
    delta?: number;
}

interface StatViewData {
    player_name?: string;
    skills?: Array<{ name?: string; value?: number }>;
    skill?: { name?: string; value?: number } | null;
    attributes?: Record<string, number>;
}

interface ItemViewData {
    player_name?: string;
    equipment?: Array<{ name?: string; quantity?: number; note?: string }>;
    weapons?: Array<{ name?: string; damage?: string; skill?: string; note?: string }>;
}

interface ItemCommandData extends RuleCommandData {
    item?: { name?: string; quantity?: number };
}

const MAX_VIEW_SKILLS = 24;

/** 判断一个参数是否是房间内可被引用的玩家名（用于省略 player_name 时区分技能名）。 */
function isKnownMember(name: string): boolean {
    const target = String(name || "").trim().toLowerCase();
    if (!target) return false;
    const members = window.currentRoom?.members || [];
    return members.some((member) => {
        const cardName = member.character_card?.name;
        return [member.username, cardName]
            .filter(Boolean)
            .some((value) => String(value).trim().toLowerCase() === target);
    });
}

function formatStatView(data: StatViewData | undefined, kind: "skill" | "attr"): string {
    const owner = data?.player_name || "角色";
    if (kind === "attr") {
        const entries = Object.entries(data?.attributes || {});
        if (!entries.length) return `${owner} 还没有基础属性`;
        return `${owner} 的基础属性：${entries.map(([key, value]) => `${key} ${value}`).join("，")}`;
    }
    if (data?.skill) return `${owner} 的${data.skill.name}：${data.skill.value}`;
    const rows = data?.skills || [];
    if (!rows.length) return `${owner} 还没有技能数据`;
    const shown = rows.slice(0, MAX_VIEW_SKILLS).map((row) => `${row.name} ${row.value}`);
    const suffix = rows.length > shown.length ? `，…… 共 ${rows.length} 项` : "";
    return `${owner} 的技能：${shown.join("，")}${suffix}`;
}

function parseCreditEvents(raw: string | undefined): Array<{ player: string; event: string }> {
    return String(raw || "")
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => {
            const index = part.indexOf(":");
            if (index < 0) return { player: "", event: "" };
            return { player: part.slice(0, index).trim(), event: part.slice(index + 1).trim() };
        })
        .filter((entry) => entry.player && entry.event);
}

function formatInterludeData(data: InterludeCommandData | undefined): string {
    const state = data?.state;
    if (!state) return "幕间状态不可用";
    const lines = [state.active ? "幕间：进行中" : "幕间：未进行"];
    if (state.startedAt) lines.push(`开始时间：${state.startedAt}`);
    if (state.startedBy) lines.push(`发起人：${state.startedBy}`);
    if (state.request?.by) {
        lines.push(`待确认提议：${state.request.by}${state.request.reason ? `（${state.request.reason}）` : ""}`);
    }
    const settlement = data?.settlement as { members?: Array<Record<string, unknown>>; settled_at?: string } | undefined;
    if (settlement?.settled_at) {
        const members = Array.isArray(settlement.members) ? settlement.members : [];
        lines.push(`结算时间：${settlement.settled_at}，成长结算成员 ${members.length} 人`);
        for (const entry of members) {
            const growth = Array.isArray(entry.growth) ? entry.growth as Array<Record<string, unknown>> : [];
            const gains = growth.filter((row) => Number(row.gain) > 0);
            const san = Array.isArray(entry.san_recovery) ? entry.san_recovery as Array<Record<string, unknown>> : [];
            const parts: string[] = [];
            if (gains.length) parts.push(`成长：${gains.map((row) => `${row.skill}+${row.gain}`).join("、")}`);
            if (san.length) parts.push(`SAN恢复：${san.map((row) => `${row.skill}+${row.applied ?? row.san}`).join("、")}`);
            lines.push(`  ${String(entry.player || "-")}：${parts.length ? parts.join("；") : "无变化"}`);
        }
    }
    lines.push(`复盘记录：${state.reviewCount ?? (data?.reviews?.length || 0)} 条`);
    return lines.join("\n");
}

function formatItemView(data: ItemViewData | undefined): string {
    const owner = data?.player_name || "角色";
    const equipment = data?.equipment || [];
    const weapons = data?.weapons || [];
    if (!equipment.length && !weapons.length) return `${owner} 身上没有物品或武器`;
    const lines = [`${owner} 的物品：`];
    lines.push(
        equipment.length
            ? equipment.map((item) => `${item.name}×${item.quantity ?? 1}`).join("，")
            : "（无物品）"
    );
    lines.push(
        weapons.length
            ? `武器：${weapons.map((weapon) => `${weapon.name}${weapon.damage ? `（${weapon.damage}）` : ""}`).join("，")}`
            : "武器：（无）"
    );
    return lines.join("\n");
}

// 纯词开关：写成 persist / weapon / no-madness，不再要求 -- 前缀。
const FLAG_WORDS = new Set(["persist", "weapon", "no-madness"]);
const STEP_VALUE_WORDS: Record<string, string> = { qty: "qty", quantity: "qty" };
// 尾部取值选项：reason / note 之后的全部内容都算它的值。
const TRAILING_VALUE_WORDS: Record<string, string> = { reason: "reason", note: "note" };

function parseCommandTokens(command: string): ParsedCommandTokens {
    const tokens = command.trim().split(/\s+/).filter(Boolean).slice(1);
    const positionals: string[] = [];
    const flags = new Set<string>();
    const options: Record<string, string> = {};

    for (let index = 0; index < tokens.length; index += 1) {
        const token = tokens[index] ?? "";
        const lower = token.toLowerCase();

        // 兼容旧写法：--qty=3 / --reason=xxx / --persist。
        if (lower.startsWith("--")) {
            const body = token.slice(2);
            const equals = body.indexOf("=");
            if (equals >= 0) {
                const key = body.slice(0, equals).toLowerCase();
                if (key) options[key] = body.slice(equals + 1);
            } else if (body) {
                flags.add(body.toLowerCase());
            }
            continue;
        }
        // ``-5`` 这类带符号数值是参数，不是开关。
        if (/^-\d+$/.test(token)) {
            positionals.push(token);
            continue;
        }
        // 兼容旧写法：其他 -x 单横线开关。
        if (lower.startsWith("-") && lower.length > 1) {
            flags.add(lower);
            continue;
        }
        if (FLAG_WORDS.has(lower)) {
            flags.add(lower);
            continue;
        }
        if (STEP_VALUE_WORDS[lower]) {
            const next = tokens[index + 1];
            if (next !== undefined) {
                options[STEP_VALUE_WORDS[lower]!] = next;
                index += 1;
            }
            continue;
        }
        if (TRAILING_VALUE_WORDS[lower]) {
            options[TRAILING_VALUE_WORDS[lower]!] = tokens.slice(index + 1).join(" ");
            break;
        }
        positionals.push(token);
    }
    return { positionals, flags, options };
}

// ---------------------------------------------------------------------------
// 命令提示（参考 Minecraft）：按当前所在的这一格给出候选。
// 只做候选生成，展示与键盘交互由 chat.ts 负责。
// ---------------------------------------------------------------------------

interface CommandSuggestion {
    value: string;
    description?: string;
    kind?: "command" | "subcommand" | "member" | "skill" | "attribute" | "item" | "weapon" | "flag" | "value" | "difficulty" | "dice";
}

const ATTRIBUTE_SUGGESTIONS: CommandSuggestion[] = [
    { value: "STR", description: "力量", kind: "attribute" },
    { value: "DEX", description: "敏捷", kind: "attribute" },
    { value: "CON", description: "体质", kind: "attribute" },
    { value: "APP", description: "外貌", kind: "attribute" },
    { value: "POW", description: "意志", kind: "attribute" },
    { value: "SIZ", description: "体型", kind: "attribute" },
    { value: "EDU", description: "教育", kind: "attribute" },
    { value: "INT", description: "智力", kind: "attribute" },
    { value: "LUC", description: "幸运", kind: "attribute" },
];

const FLAG_SUGGESTIONS: Record<string, CommandSuggestion> = {
    qty: { value: "qty", description: "数量：后接数字，如 qty 3", kind: "flag" },
    weapon: { value: "weapon", description: "按武器处理", kind: "flag" },
    persist: { value: "persist", description: "立即写回持久角色卡", kind: "flag" },
    note: { value: "note", description: "备注：后接文本", kind: "flag" },
    reason: { value: "reason", description: "变更原因：后接文本", kind: "flag" },
    "no-madness": { value: "no-madness", description: "跳过自动疯狂判定", kind: "flag" },
};

function suggestion(value: string, description: string, kind: CommandSuggestion["kind"]): CommandSuggestion {
    const result: CommandSuggestion = { value, description };
    if (kind) result.kind = kind;
    return result;
}

function matchesPrefix(value: string, prefix: string): boolean {
    const target = String(value || "").toLowerCase();
    const typed = String(prefix || "").toLowerCase();
    return !typed || target.startsWith(typed);
}

function filterSuggestions(list: CommandSuggestion[], prefix: string): CommandSuggestion[] {
    const seen = new Set<string>();
    const result: CommandSuggestion[] = [];
    for (const entry of list) {
        const value = String(entry.value || "").trim();
        const key = value.toLowerCase();
        if (!value || seen.has(key) || !matchesPrefix(value, prefix)) continue;
        seen.add(key);
        result.push({ ...entry, value });
    }
    return result;
}

function mergeSuggestions(groups: CommandSuggestion[][], prefix: string): CommandSuggestion[] {
    return filterSuggestions(groups.flat(), prefix);
}

function activeRoomMemberList(): RoomMember[] {
    return (window.currentRoom?.members || []).filter(
        (member) => member.is_active !== false && member.status !== "removed"
    );
}

function findRoomMember(name: string): RoomMember | undefined {
    const target = String(name || "").trim().toLowerCase();
    if (!target) return undefined;
    return activeRoomMemberList().find((member) =>
        [member.username, member.character_card?.name]
            .filter(Boolean)
            .some((value) => String(value).trim().toLowerCase() === target)
    );
}

function currentRoomMember(): RoomMember | undefined {
    const userId = window.currentUser?.user_id;
    if (userId === undefined || userId === null) return undefined;
    return activeRoomMemberList().find((member) => String(member.user_id) === String(userId));
}

function memberSuggestions(prefix: string): CommandSuggestion[] {
    return filterSuggestions(
        activeRoomMemberList().map((member) => {
            const username = String(member.username || "").trim();
            const cardName = String(member.character_card?.name || "").trim();
            const description = cardName && cardName !== username ? `角色：${cardName}` : "房间成员";
            return suggestion(username, description, "member");
        }),
        prefix
    );
}

function skillSuggestions(member: RoomMember | undefined, prefix: string): CommandSuggestion[] {
    const skills = member?.character_card?.skills || [];
    return filterSuggestions(
        skills.map((skill) => {
            const name = String(skill.name || skill.skillKey || "").trim();
            const value = typeof skill.value === "number" ? ` · 当前 ${skill.value}` : "";
            return suggestion(name, `技能${value}`, "skill");
        }),
        prefix
    );
}

function itemSuggestions(member: RoomMember | undefined, prefix: string): CommandSuggestion[] {
    const card = member?.character_card;
    const equipment = (card?.equipment || []).map((item) => {
        const quantity = Number(item.quantity) > 1 ? ` ×${item.quantity}` : "";
        return suggestion(String(item.name || ""), `物品${quantity}`, "item");
    });
    const weapons = (card?.weapons || []).map((weapon) =>
        suggestion(
            String(weapon.name || ""),
            `武器${weapon.damage ? ` · ${weapon.damage}` : ""}`,
            "weapon"
        )
    );
    return filterSuggestions([...equipment, ...weapons], prefix);
}

function statValueSuggestions(prefix: string): CommandSuggestion[] {
    const values = ["50", "60", "70", "75", "80", "90", "+5", "+10", "-5", "-10"];
    return filterSuggestions(
        [
            ...values.map((value) =>
                suggestion(value, value.startsWith("+") || value.startsWith("-") ? "在当前值上增减" : "设为该值", "value")
            ),
            FLAG_SUGGESTIONS.persist!,
            FLAG_SUGGESTIONS.reason!,
        ],
        prefix
    );
}

function statSlotSuggestions(kind: "skill" | "attr", prior: string[], prefix: string): CommandSuggestion[] {
    const explicitMember = prior.length > 0 && isKnownMember(prior[0] || "");
    const target = explicitMember ? findRoomMember(prior[0] || "") : currentRoomMember();
    const nameIndex = explicitMember ? 1 : 0;

    if (prior.length === nameIndex) {
        const names = kind === "skill"
            ? skillSuggestions(target, prefix)
            : filterSuggestions(ATTRIBUTE_SUGGESTIONS, prefix);
        return explicitMember ? names : mergeSuggestions([memberSuggestions(prefix), names], prefix);
    }
    if (prior.length === nameIndex + 1) return statValueSuggestions(prefix);
    return filterSuggestions([FLAG_SUGGESTIONS.persist!, FLAG_SUGGESTIONS.reason!], prefix);
}

function itemSlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length === 0) {
        return filterSuggestions(
            [
                suggestion("list", "查看物品与武器", "subcommand"),
                suggestion("rm", "移除物品 / 武器", "subcommand"),
                ...memberSuggestions(prefix),
            ],
            prefix
        );
    }

    const sub = (prior[0] || "").toLowerCase();
    if (sub === "list" || sub === "ls") {
        return prior.length === 1 ? memberSuggestions(prefix) : [];
    }

    const removeMode = ["rm", "remove", "del", "delete"].includes(sub);
    const args = removeMode ? prior.slice(1) : prior;
    const explicitMember = args.length > 0 && isKnownMember(args[0] || "");
    const target = explicitMember ? findRoomMember(args[0] || "") : currentRoomMember();
    const nameIndex = explicitMember ? 1 : 0;

    if (args.length === nameIndex) {
        const items = itemSuggestions(target, prefix);
        return explicitMember ? items : mergeSuggestions([memberSuggestions(prefix), items], prefix);
    }

    const flags = [
        FLAG_SUGGESTIONS.qty!,
        FLAG_SUGGESTIONS.weapon!,
        FLAG_SUGGESTIONS.persist!,
        ...(removeMode ? [] : [FLAG_SUGGESTIONS.note!]),
        FLAG_SUGGESTIONS.reason!,
    ];
    return filterSuggestions(flags, prefix);
}

function sanitySlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length === 0) return memberSuggestions(prefix);
    if (prior.length === 1) {
        return filterSuggestions(
            [
                suggestion("1/1d6", "成功 1 / 失败 1d6", "value"),
                suggestion("1d3/1d10", "成功 1d3 / 失败 1d10", "value"),
                suggestion("1d10/1d100", "成功 1d10 / 失败 1d100", "value"),
            ],
            prefix
        );
    }
    return filterSuggestions([FLAG_SUGGESTIONS["no-madness"]!, FLAG_SUGGESTIONS.reason!], prefix);
}

function checkSlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length === 0) return memberSuggestions(prefix);
    const target = findRoomMember(prior[0] || "") || currentRoomMember();
    if (prior.length === 1) {
        return mergeSuggestions(
            [skillSuggestions(target, prefix), filterSuggestions(ATTRIBUTE_SUGGESTIONS, prefix)],
            prefix
        );
    }
    return filterSuggestions(
        [
            suggestion("困难", "目标值减半", "difficulty"),
            suggestion("极难", "目标值降到五分之一", "difficulty"),
            suggestion("+10", "临时加 10 点", "value"),
            suggestion("-10", "临时减 10 点", "value"),
            suggestion("奖励骰", "额外十位骰取优", "flag"),
            suggestion("惩罚骰", "额外十位骰取劣", "flag"),
        ],
        prefix
    );
}

function diceSlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length === 0) {
        return filterSuggestions(
            [
                suggestion("1d100", "百分骰", "dice"),
                suggestion("1d6", "六面骰", "dice"),
                suggestion("1d10", "十面骰", "dice"),
                suggestion("3d6", "三枚六面骰", "dice"),
            ],
            prefix
        );
    }
    return filterSuggestions([suggestion("table", "在屏幕中央召唤骰桌", "flag")], prefix);
}

function recordSlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length === 0) {
        return filterSuggestions(
            [
                suggestion("damage", "记录房间角色伤害", "subcommand"),
                suggestion("san", "记录 SAN 损失", "subcommand"),
            ],
            prefix
        );
    }
    if (prior.length === 1) return memberSuggestions(prefix);
    if (prior.length === 2) {
        return filterSuggestions(
            ["1", "2", "3", "5", "10"].map((value) => suggestion(value, "数值", "value")),
            prefix
        );
    }
    return [];
}

function rollSlotSuggestions(prior: string[], prefix: string): CommandSuggestion[] {
    if (prior.length > 0) return [];
    return filterSuggestions(
        [
            suggestion("confirm", "确认重投后的最终结果", "subcommand"),
            suggestion("force", "管理员强制结算当前投掷", "subcommand"),
        ],
        prefix
    );
}

function getCommandSuggestions(input: string): CommandSuggestion[] {
    const raw = input.trimStart();
    if (!raw.startsWith("/")) return [];
    const tokens = raw.split(/\s+/);
    if (tokens.length < 2) return [];
    const command = (tokens[0] || "").toLowerCase();
    const prefix = tokens[tokens.length - 1] || "";
    const prior = tokens.slice(1, tokens.length - 1);

    switch (command) {
        case "/item":
            return itemSlotSuggestions(prior, prefix);
        case "/skill":
            return statSlotSuggestions("skill", prior, prefix);
        case "/attr":
            return statSlotSuggestions("attr", prior, prefix);
        case "/sc":
            return sanitySlotSuggestions(prior, prefix);
        case "/check":
            return checkSlotSuggestions(prior, prefix);
        case "/dice":
            return diceSlotSuggestions(prior, prefix);
        case "/record":
            return recordSlotSuggestions(prior, prefix);
        case "/roll":
            return rollSlotSuggestions(prior, prefix);
        default:
            return [];
    }
}

window.TrpgCommandSuggestions = { getSuggestions: getCommandSuggestions };

