/**
 * 骰型与骰组：前后端共享的展示模型。
 *
 * 服务端 metadata 会返回 ``groups``（每组角色、骰型、数量、面值与标签）与
 * ``selected``（选择高亮的组/骰索引）。前端必须以真实骰型渲染，禁止把不支持
 * 的骰型伪装成 d6；无法识别的骰型由调用方降级到简易/纯文本档位。
 */
interface DiceGroupSpec {
    role: string;
    type: string;
    count: number;
    faces: number[];
    labels: string[];
}

interface DiceSelection {
    group: number;
    index: number;
}

const DICE_RENDER_TYPES = ["d4", "d6", "d8", "d10", "d12", "d20"];

/** 把 d100 视为两枚 d10（十位 + 个位）；其余骰型原样返回。 */
function diceRenderType(type: string): string {
    const normalized = String(type || "").toLowerCase();
    if (normalized === "d100") return "d10";
    return DICE_RENDER_TYPES.includes(normalized) ? normalized : "";
}

function diceTypeSupported(type: string): boolean {
    return Boolean(diceRenderType(type));
}

function diceTypeSides(type: string): number {
    const match = /^d(\d+)$/i.exec(String(type || ""));
    return match ? Number.parseInt(match[1] || "0", 10) : 0;
}

function dicePlanGroups(plan: DicePlan | null | undefined): DiceGroupSpec[] {
    if (!plan) return [];
    const declared = (plan.groups || []) as DiceGroupSpec[] | undefined;
    if (declared && declared.length) {
        return declared.map((group) => ({
            role: String(group.role || "normal"),
            type: String(group.type || ""),
            count: Math.max(1, Number(group.count) || 1),
            faces: (group.faces || []).map(Number),
            labels: (group.labels || []).map(String),
        }));
    }
    if (plan.mode === "percentile") {
        return [
            { role: "tens", type: "d10", count: 1, faces: [], labels: [] },
            { role: "ones", type: "d10", count: 1, faces: [], labels: [] },
            { role: "candidate", type: "d10", count: Math.max(0, Number(plan.bonus_dice) || 0), faces: [], labels: [] },
            { role: "candidate", type: "d10", count: Math.max(0, Number(plan.penalty_dice) || 0), faces: [], labels: [] },
        ].filter((group) => group.count > 0);
    }
    const groups: DiceGroupSpec[] = [];
    const terms = String(plan.expression || "").replace(/\s+/g, "").toLowerCase().split("+");
    terms.forEach((term) => {
        const match = /^(\d+)d(\d+)$/.exec(term);
        if (!match) return;
        const count = Number.parseInt(match[1] || "1", 10);
        const sides = Number.parseInt(match[2] || "6", 10);
        if (sides === 100) {
            for (let index = 0; index < count; index += 1) {
                groups.push({ role: "tens", type: "d10", count: 1, faces: [], labels: [] });
                groups.push({ role: "ones", type: "d10", count: 1, faces: [], labels: [] });
            }
            return;
        }
        groups.push({ role: "normal", type: `d${sides}`, count, faces: [], labels: [] });
    });
    if (!groups.length) {
        groups.push({ role: "normal", type: "d20", count: Math.max(1, Number(plan.count) || 1), faces: [], labels: [] });
    }
    return groups;
}

function diceCloneGroups(groups: DiceGroupSpec[]): DiceGroupSpec[] {
    return groups.map((group) => ({
        role: group.role,
        type: group.type,
        count: group.count,
        faces: [...group.faces],
        labels: [...group.labels],
    }));
}

/** 合并服务端 groups 与快照面值；缺面值时按顺序从 faces/labels 补齐。 */
function diceSnapshotGroups(plan: DicePlan | null | undefined, snapshot: DiceResultSnapshot | null | undefined): DiceGroupSpec[] {
    const groups = diceCloneGroups(
        (snapshot?.groups as DiceGroupSpec[] | undefined)?.length
            ? (snapshot!.groups as DiceGroupSpec[])
            : dicePlanGroups(plan),
    );
    const faces = (snapshot?.faces || []).map(Number);
    const labels = (snapshot?.labels || []).map(String);
    if (plan?.mode === "percentile" && groups.length >= 2 && groups[0] && groups[1] && (!groups[0].labels?.length || !groups[1].labels?.length)) {
        groups[0].faces = faces.length ? [faces[0] ?? 0] : groups[0].faces;
        groups[1].faces = faces.length > 1 ? [faces[1] ?? 0] : groups[1].faces;
        groups[0].labels = [labels[0] ?? String((groups[0].faces[0] || 0) * 10).padStart(2, "0")];
        groups[1].labels = [labels[1] ?? String(groups[1].faces[0] ?? 0)];
        groups.forEach((group) => {
            if (group.role !== "tens" && group.role !== "ones") group.faces = [];
        });
    }
    let cursor = 0;
    groups.forEach((group) => {
        if (!group.faces.length) {
            group.faces = faces.slice(cursor, cursor + group.count);
        }
        if (!group.labels.length) {
            group.labels = labels.slice(cursor, cursor + group.count);
        }
        group.faces.forEach((value, index) => {
            if (!group.labels[index]) group.labels[index] = String(value);
        });
        cursor += group.count;
    });
    return groups;
}

function diceSelectedIndex(groups: DiceGroupSpec[], selected: DiceSelection | null | undefined): number {
    if (!selected) return -1;
    const groupIndex = Number(selected.group);
    const dieIndex = Number(selected.index);
    if (!Number.isFinite(groupIndex) || groupIndex < 0 || groupIndex >= groups.length) return -1;
    let offset = 0;
    for (let index = 0; index < groupIndex; index += 1) offset += groups[index]?.count || 0;
    return offset + (Number.isFinite(dieIndex) ? dieIndex : 0);
}

function diceTotalCount(groups: DiceGroupSpec[]): number {
    return groups.reduce((total, group) => total + Math.max(0, group.count), 0);
}

/** 简易渲染最多同时绘制多少枚骰子：超过后只提示并保留结果，避免整桌空白与 DOM 卡顿。 */
const DICE_SIMPLE_RENDER_LIMIT = 60;

function diceUnsupportedReason(groups: DiceGroupSpec[]): string {
    const unsupported = groups.find((group) => !diceTypeSupported(group.type));
    return unsupported ? `unsupported_dice:${unsupported.type}` : "";
}

function diceGroupRoleLabel(group: DiceGroupSpec): string {
    if (group.role === "tens") return "十位";
    if (group.role === "ones") return "个位";
    if (group.role === "candidate") return "候选";
    return "";
}
