// CoC7 的 /check 检定模块（批次 E1 自 tools/checkTool.ts 整块迁入）。
//
// 属性别名 / 技能键表 / 难度词解析（困难 ÷2、极难 ÷5）是 CoC7 私有词表，
// 与建图词表（后端 ruleset/coc7/narrative.py）同属一个规则集的声明面；
// 核心工具壳只经 TrpgCheckToolRegistry 分派（见 tools/check-tool-registry.ts）。
// 现值逐位不变：阈值回退 1/96 仍读 window.currentRoom.dice_thresholds 快照。
type CheckDifficulty = "regular" | "hard" | "extreme";
type CheckCommandParseResult = { success: true; playerName: string; name: string; difficulty: CheckDifficulty; adjustment: number; bonusDice: number; penaltyDice: number } | { success: false; error: string };
const CHECK_ATTRIBUTE_ALIASES: Record<string, COC7AttributeKey> = { STR: "STR", "力量": "STR", CON: "CON", "体质": "CON", SIZ: "SIZ", "体型": "SIZ", DEX: "DEX", "敏捷": "DEX", APP: "APP", "外貌": "APP", INT: "INT", "智力": "INT", POW: "POW", "意志": "POW", EDU: "EDU", "教育": "EDU", LUC: "LUC", "幸运": "LUC", AGE: "AGE", "年龄": "AGE" };
// 属性英文键 → 中文展示名，避免检定结果出现英文键。
const CHECK_ATTRIBUTE_LABELS: Record<string, string> = { STR: "力量", CON: "体质", SIZ: "体型", DEX: "敏捷", APP: "外貌", INT: "智力", POW: "意志", EDU: "教育", LUC: "幸运", AGE: "年龄" };
// 常见 COC7 技能英文键 → 中文名（键统一小写）；角色卡自带本地化名称时优先使用角色卡名称。
const CHECK_SKILL_KEY_LABELS: Record<string, string> = {
    stealth: "潜行", spothidden: "侦察", listen: "聆听", track: "追踪", lipread: "读唇", libraryuse: "图书馆使用",
    navigate: "导航", computeruse: "计算机使用", charm: "取悦", fasttalk: "话术", intimidate: "恐吓", persuade: "说服",
    psychology: "心理学", languageown: "母语", languageother: "外语", fighting: "格斗", firearms: "射击", dodge: "闪避",
    throw: "投掷", demolitions: "爆破", artillery: "炮术", firstaid: "急救", medicine: "医学", psychoanalysis: "精神分析",
    hypnosis: "催眠", climb: "攀爬", jump: "跳跃", swim: "游泳", diving: "潜水", appraise: "估价", anthropology: "人类学",
    accounting: "会计", law: "法律", history: "历史", archaeology: "考古学", naturalworld: "博物学", occult: "神秘学",
    electronics: "电子学", science: "科学", disguise: "乔装", survival: "生存", artcraft: "技艺", creditrating: "信用评级",
    cthulhumythos: "克苏鲁神话", locksmith: "开锁", sleightofhand: "妙手", driveauto: "汽车驾驶", ride: "骑术",
};
class Coc7CheckTool {
    constructor(private readonly dice: DiceTool) {}
    run(command: string): string {
        const parsed = this.parseCheckCommand(command); if (!parsed.success) return parsed.error;
        const member = this.findRoomMember(parsed.playerName); if (!member) return `未在当前房间找到玩家 ${parsed.playerName}`;
        if (!member.character_card) return `玩家 ${parsed.playerName} 未绑定角色卡`;
        const found = this.findCheckValue(member.character_card, parsed.name); if (!found) return `玩家 ${parsed.playerName} 的角色卡中未找到 ${parsed.name}`;
        const target = this.applyDifficulty(found.value, parsed.difficulty) + parsed.adjustment;
        let rollResult: { result: number; rolls: number[] }; try { rollResult = this.dice.rollPercentile(parsed.bonusDice, parsed.penaltyDice); } catch (error) { return error instanceof Error ? error.message : String(error); }
        const difficultyLabel = parsed.difficulty === "hard" ? "困难" : parsed.difficulty === "extreme" ? "极难" : "";
        // /check 使用全房间统一阈值（房规 > 管理员默认值），个人面板阈值不影响 /check。
        const level = this.dice.evaluateSuccessLevel(rollResult.result, target, this.roomDiceThresholds());
        const success = this.dice.isSuccessLevel(level);
        let output = `${difficultyLabel}${found.displayName} d%: [${rollResult.result}] = ${rollResult.result} / ${target} ${success ? "成功" : "失败"}${this.dice.successLevelSuffix(level)}`;
        if (parsed.bonusDice || parsed.penaltyDice) output += ` ${parsed.bonusDice ? "奖励骰" : "惩罚骰"}（${rollResult.rolls.join(", ")}），取${rollResult.result}`;
        return output;
    }
    // 房间统一阈值由后端房间快照提供（房规优先，其次管理员默认值），无房间时回退 COC7 默认值。
    // （批次 E1 原样保留：阈值快照读法不属于词表，不做分派。）
    private roomDiceThresholds(): { criticalThreshold: number; fumbleThreshold: number } {
        const thresholds = window.currentRoom?.dice_thresholds;
        return {
            criticalThreshold: Number.isFinite(thresholds?.critical) ? Number(thresholds?.critical) : 1,
            fumbleThreshold: Number.isFinite(thresholds?.fumble) ? Number(thresholds?.fumble) : 96,
        };
    }
    private parseCheckCommand(command: string): CheckCommandParseResult {
        const parts = command.trim().split(/\s+/).filter(Boolean); if ((parts[0] || "").toLowerCase() !== "/check") return { success: false, error: "无效的属性鉴定命令" };
        if (!parts[1] || !parts[2]) return { success: false, error: "格式：/check 玩家名 技能/属性名 困难/极难 +/-调整值 [奖励骰|惩罚骰]" };
        let difficulty: CheckDifficulty = "regular", adjustmentText = "", bonusDice = 0, penaltyDice = 0;
        for (const part of parts.slice(3)) {
            if (part === "困难") difficulty = "hard"; else if (part === "极难") difficulty = "extreme"; else if (/^[+-]\d+$/.test(part)) adjustmentText = part;
            else if (/^(奖励骰|bonus)(?::\d+)?$/i.test(part)) bonusDice = Number.parseInt(part.split(":")[1] || "1", 10);
            else if (/^(惩罚骰|penalty)(?::\d+)?$/i.test(part)) penaltyDice = Number.parseInt(part.split(":")[1] || "1", 10);
            else return { success: false, error: `无法识别的 /check 参数：${part}` };
        }
        if (bonusDice && penaltyDice) return { success: false, error: "奖励骰和惩罚骰不能同时使用" };
        return { success: true, playerName: parts[1], name: parts[2], difficulty, adjustment: adjustmentText ? Number.parseInt(adjustmentText, 10) : 0, bonusDice, penaltyDice };
    }
    private findRoomMember(playerName: string): RoomMember | null { const expected = playerName.toLowerCase(); return (window.currentRoom?.members || []).find((member) => member.is_active !== false && member.status !== "removed" && String(member.username || "").toLowerCase() === expected) || null; }
    // 返回检定数值与用于展示的本地化名称（属性/技能英文键会被翻译成中文）。
    private findCheckValue(card: Partial<COC7CharacterCard>, name: string): { value: number; displayName: string } | null {
        const key = CHECK_ATTRIBUTE_ALIASES[name] || CHECK_ATTRIBUTE_ALIASES[name.toUpperCase()];
        if (key) {
            const value = card.attributes?.[key];
            if (typeof value === "number" && Number.isFinite(value)) return { value, displayName: CHECK_ATTRIBUTE_LABELS[key] || name };
        }
        const localized = CHECK_SKILL_KEY_LABELS[name.toLowerCase()] || name;
        const expected = new Set([name.toLowerCase(), localized.toLowerCase()]);
        const skill = (card.skills || []).find((item) => [item.name, item.skillKey, item.id].some((candidate) => expected.has(String(candidate || "").toLowerCase())));
        if (skill && typeof skill.value === "number" && Number.isFinite(skill.value)) {
            const display = String(skill.name || "").trim();
            return { value: skill.value, displayName: display || localized };
        }
        return null;
    }
    // COC7 难易度：困难取目标值一半，极难取五分之一，常规不变。
    private applyDifficulty(target: number, difficulty: CheckDifficulty): number { return difficulty === "hard" ? Math.floor(target / 2) : difficulty === "extreme" ? Math.floor(target / 5) : target; }
}
// 规则集登记：coc7 同时是历史房间的默认模块（ruleset_id 缺省时由外壳回退到这里）。
window.TrpgCheckToolRegistry?.register?.("coc7", { id: "coc7", run: (dice: DiceTool, command: string) => new Coc7CheckTool(dice).run(command) }, { default: true });
