// 骰娘的大成功/大失败阈值设置：默认遵循 COC7（1 为大成功，96-100 为大失败）。
type DiceSuccessLevel = "critical" | "extreme" | "hard" | "regular" | "failure" | "fumble";
interface DiceSuccessSettings {
    criticalThreshold: number;
    fumbleThreshold: number;
}
const DICE_SETTINGS_STORAGE_KEY = "trpg_dice_settings";
const DEFAULT_DICE_SETTINGS: DiceSuccessSettings = { criticalThreshold: 1, fumbleThreshold: 96 };

class DiceTool {
    private readonly diceTypes: Record<string, number> = {
        d4: 4,
        d6: 6,
        d8: 8,
        d10: 10,
        d12: 12,
        d20: 20,
        d100: 100,
    };

    private clampThreshold(value: unknown, fallback: number): number {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return fallback;
        return Math.max(0, Math.min(100, Math.floor(parsed)));
    }

    // 读取已保存的大成功/大失败阈值，任何异常都回退到默认值。
    getSuccessSettings(): DiceSuccessSettings {
        try {
            const raw = localStorage.getItem(DICE_SETTINGS_STORAGE_KEY);
            if (raw) {
                const parsed = JSON.parse(raw) as Partial<DiceSuccessSettings>;
                return {
                    criticalThreshold: this.clampThreshold(parsed.criticalThreshold, DEFAULT_DICE_SETTINGS.criticalThreshold),
                    fumbleThreshold: this.clampThreshold(parsed.fumbleThreshold, DEFAULT_DICE_SETTINGS.fumbleThreshold),
                };
            }
        } catch { /* localStorage 不可用时使用默认值 */ }
        return { ...DEFAULT_DICE_SETTINGS };
    }

    // 保存阈值到 localStorage，并返回规范化后的结果（入参允许字符串，便于直接读取输入框）。
    saveSuccessSettings(settings: { criticalThreshold?: unknown; fumbleThreshold?: unknown }): DiceSuccessSettings {
        const current = this.getSuccessSettings();
        const next: DiceSuccessSettings = {
            criticalThreshold: this.clampThreshold(settings.criticalThreshold, current.criticalThreshold),
            fumbleThreshold: this.clampThreshold(settings.fumbleThreshold, current.fumbleThreshold),
        };
        try { localStorage.setItem(DICE_SETTINGS_STORAGE_KEY, JSON.stringify(next)); } catch { /* ignore */ }
        return next;
    }

    // 依据阈值判定成功等级，大失败仍需满足检定失败（点数大于目标值）。
    // thresholds 缺省时使用本面板的个人阈值（仅影响面板内的个人判定）。
    evaluateSuccessLevel(roll: number, target: number, thresholds?: DiceSuccessSettings): DiceSuccessLevel {
        const { criticalThreshold, fumbleThreshold } = thresholds || this.getSuccessSettings();
        if (roll <= criticalThreshold) return "critical";
        if (target > 0 && roll <= Math.floor(target / 5)) return "extreme";
        if (target > 0 && roll <= Math.floor(target / 2)) return "hard";
        if (roll <= target) return "regular";
        if (roll >= fumbleThreshold) return "fumble";
        return "failure";
    }

    isSuccessLevel(level: DiceSuccessLevel): boolean {
        return level !== "failure" && level !== "fumble";
    }

    // 大成功/大失败的中文标注，便于聊天与 AI 上下文直接读取。
    successLevelSuffix(level: DiceSuccessLevel): string {
        return level === "critical" ? "（大成功）" : level === "fumble" ? "（大失败）" : "";
    }

    rollSingleDice(diceType: string): number {
        const sides = this.diceTypes[diceType] || 6;
        return Math.floor(Math.random() * sides) + 1;
    }

    // COC7 奖励/惩罚骰：个位骰共用，额外掷出（N+1）个互不相同的十位骰，
    // 奖励骰取最小值、惩罚骰取最大值作为最终结果。
    rollPercentile(bonusDice = 0, penaltyDice = 0): { result: number; rolls: number[] } {
        if (bonusDice > 0 && penaltyDice > 0) throw new Error("奖励骰和惩罚骰不能同时使用");
        if (!bonusDice && !penaltyDice) return { result: this.rollSingleDice("d100"), rolls: [] };
        const units = this.rollSingleDice("d10") % 10;
        const tens: number[] = [];
        const count = (bonusDice || penaltyDice) + 1;
        let attempts = 0;
        while (tens.length < count && attempts < 100) {
            attempts += 1;
            const value = this.rollSingleDice("d10") - 1;
            if (!tens.includes(value)) tens.push(value);
        }
        for (let value = 0; tens.length < count && value < 10; value += 1) {
            if (!tens.includes(value)) tens.push(value);
        }
        const rolls = tens.map((ten) => ten === 0 && units === 0 ? 100 : ten * 10 + units);
        return { result: bonusDice ? Math.min(...rolls) : Math.max(...rolls), rolls };
    }

    parseDiceCommand(command: string): DiceParseResult {
        const match = command.match(/^(\d+)d(\d+)$/i);
        if (!match) {
            return {
                success: false,
                error: '无效的骰子命令格式，请使用类似 "1d6" 的格式',
            };
        }

        const count = Number.parseInt(match[1] || "", 10);
        const sides = Number.parseInt(match[2] || "", 10);
        if (!Number.isFinite(count) || count < 1 || count > 100) {
            return {
                success: false,
                error: "骰子数量必须在 1-100 之间",
            };
        }

        if (!Number.isFinite(sides) || sides < 2 || sides > 100) {
            return {
                success: false,
                error: "骰子面数必须在 2-100 之间",
            };
        }

        const results: number[] = [];
        let total = 0;
        for (let index = 0; index < count; index += 1) {
            const result = Math.floor(Math.random() * sides) + 1;
            results.push(result);
            total += result;
        }

        return {
            success: true,
            count,
            sides,
            results,
            total,
        };
    }

    handleDiceCommand(command: string): string {
        const diceCommand = command.replace(/^\/dice\s+/i, "").trim();
        const result = this.parseDiceCommand(diceCommand);

        if (!result.success) {
            return result.error;
        }

        let message = `投掷 ${result.count}d${result.sides}：`;
        message += result.results.join(" + ");
        if (result.count > 1) {
            message += ` = ${result.total}`;
        }
        return message;
    }

    /** 解析本地骰桌表达式（NdM 与加法混合），返回每一项的数量与面数。 */
    private parseLocalTerms(expression: string): Array<{ count: number; sides: number }> {
        const compact = String(expression || "").replace(/\s+/g, "").toLowerCase();
        const terms = compact.split("+").map((term) => /^(\d+)d(\d+)$/.exec(term));
        if (!compact || terms.some((match) => !match)) {
            throw new Error('无效的骰子表达式，请使用类似 "2d6" 或 "1d100+1d6" 的格式');
        }
        const parsed = terms.map((match) => ({
            count: Number.parseInt(match![1] || "1", 10),
            sides: Number.parseInt(match![2] || "6", 10),
        }));
        const totalCount = parsed.reduce((sum, term) => sum + term.count, 0);
        if (totalCount < 1 || totalCount > 100 || parsed.some((term) => term.sides < 2 || term.sides > 1000)) {
            throw new Error("骰子数量必须为 1-100，面数必须为 2-1000");
        }
        return parsed;
    }

    /** 只生成骰桌计划，不产生随机点数；真正点数由落定后的物理骰面决定。 */
    localPlan(expression: string): DicePlan {
        const parsed = this.parseLocalTerms(expression);
        const compact = String(expression || "").replace(/\s+/g, "").toLowerCase();
        const groups: DiceGroupSpec[] = [];
        parsed.forEach((term) => {
            if (term.sides === 100) {
                for (let index = 0; index < term.count; index += 1) {
                    groups.push({ role: "tens", type: "d10", count: 1, faces: [], labels: [] });
                    groups.push({ role: "ones", type: "d10", count: 1, faces: [], labels: [] });
                }
                return;
            }
            groups.push({ role: "normal", type: `d${term.sides}`, count: term.count, faces: [], labels: [] });
        });
        return {
            mode: "standard",
            expression: compact,
            count: parsed.reduce((sum, term) => sum + term.count, 0),
            sides: parsed.length === 1 ? parsed[0]!.sides : 0,
            bonus_dice: 0,
            penalty_dice: 0,
            candidates: [],
            groups,
            selected: null,
            check: null,
        };
    }

    /**
     * 本地投掷（/dice --table）：支持 NdM 与加法混合表达式。
     * 传入 faces 时按物理落定的骰面结算；不传时退回随机数生成器（纯文本/兜底）。
     */
    rollLocal(expression: string, faces?: number[] | null): { plan: DicePlan; snapshot: DiceResultSnapshot } {
        const plan = this.localPlan(expression);
        const groups: DiceGroupSpec[] = [];
        const flatFaces: number[] = [];
        const flatLabels: string[] = [];
        const expected = (plan.groups || []).reduce((sum, group) => sum + group.count, 0);
        const provided = Array.isArray(faces) && faces.length === expected ? faces : null;
        let cursor = 0;
        let total = 0;
        const parsed = this.parseLocalTerms(expression);
        parsed.forEach((term) => {
            if (term.sides === 100) {
                for (let index = 0; index < term.count; index += 1) {
                    let tens: number;
                    let units: number;
                    if (provided) {
                        tens = Number(provided[cursor++] ?? 0);
                        units = Number(provided[cursor++] ?? 0);
                        // 百分骰的 00 + 0 记为 100。
                        if (tens === 0 && units === 0) total += 100;
                        else total += tens * 10 + units;
                    } else {
                        const value = 1 + Math.floor(Math.random() * 100);
                        tens = value === 100 ? 0 : Math.floor(value / 10);
                        units = value % 10;
                        total += value;
                    }
                    groups.push({ role: "tens", type: "d10", count: 1, faces: [tens], labels: [String(tens * 10).padStart(2, "0")] });
                    groups.push({ role: "ones", type: "d10", count: 1, faces: [units], labels: [String(units)] });
                    flatFaces.push(tens, units);
                    flatLabels.push(String(tens * 10).padStart(2, "0"), String(units));
                }
                return;
            }
            const rolls: number[] = [];
            for (let index = 0; index < term.count; index += 1) {
                const value = provided ? Number(provided[cursor++] ?? 1) : 1 + Math.floor(Math.random() * term.sides);
                rolls.push(value);
                total += value;
            }
            groups.push({
                role: "normal",
                type: `d${term.sides}`,
                count: term.count,
                faces: rolls,
                labels: rolls.map((value) => String(value)),
            });
            flatFaces.push(...rolls);
            flatLabels.push(...rolls.map((value) => String(value)));
        });
        const snapshot: DiceResultSnapshot = {
            faces: flatFaces,
            labels: flatLabels,
            groups,
            selected: null,
            roll: total,
            rolls: flatFaces,
            total,
            success: null,
            success_level: null,
            summary: `投掷 ${plan.expression}：${flatLabels.join(" + ")} = ${total}`,
            check_name: null,
            difficulty: null,
            target: null,
            roller: null,
            mode: "standard",
            expression: plan.expression,
            count: plan.count,
            sides: plan.sides,
            candidates: [],
        };
        return { plan, snapshot };
    }
}

window.DiceTool = DiceTool;
