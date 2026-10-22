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
}

window.DiceTool = DiceTool;
