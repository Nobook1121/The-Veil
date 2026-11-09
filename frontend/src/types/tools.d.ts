interface DiceParseSuccess {
    success: true;
    count: number;
    sides: number;
    results: number[];
    total: number;
}

interface DiceParseFailure {
    success: false;
    error: string;
}

type DiceParseResult = DiceParseSuccess | DiceParseFailure;

interface DiceToolConstructor {
    new(): DiceTool;
}

interface DiceTool {
    handleDiceCommand(command: string): string;
    parseDiceCommand(command: string): DiceParseResult;
    rollPercentile(bonusDice?: number, penaltyDice?: number): { result: number; rolls: number[] };
    // 读取骰娘大成功/大失败阈值设置，用于随请求透传给后端骰子工具。
    getSuccessSettings(): { criticalThreshold: number; fumbleThreshold: number };
}

interface CheckToolConstructor {
    new(dice: DiceTool): CheckTool;
}

interface CheckTool {
    handleCheckCommand(command: string): string;
}

interface ToolManagerConstructor {
    new(): ToolManager;
}

interface ToolManager {
    handleCommand(command: string): string | Promise<string> | null;
    handleCheckCommand(command: string): string;
    recordCharacterChange(payload: Record<string, unknown>): Promise<unknown>;
}
