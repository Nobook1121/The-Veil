// /check 命令的通用外壳（批次 E1）：按房间规则集经 TrpgCheckToolRegistry 分派。
//
// 属性别名 / 技能键表 / 难度解析 / 目标值折算（÷2 / ÷5）全部在规则集模块
// （见 rulesets/coc7/checkTool.ts）；本壳不做任何规则数学。未登记的规则集
// 显式拒绝并指向通用骰入口（C1 / D3 的拒绝教义：静默降级会造出「看似可用、
// 实际按错规则结算」的检定），历史房间（ruleset_id 缺省）回退默认模块。
class CheckTool {
    constructor(private readonly dice: DiceTool) {}
    handleCheckCommand(command: string): string {
        const registry = window.TrpgCheckToolRegistry;
        if (!registry) return "检定工具注册表未加载，无法执行 /check";
        const rulesetId = String(window.currentRoom?.ruleset_id || "").trim();
        const module = registry.forRuleset(rulesetId || undefined);
        if (module) return module.run(this.dice, command);
        // 明确声明但未登记（如 dnd5e 房间）：拒绝并指认规则集，给出替代入口。
        const label = rulesetId ? rulesetId.toUpperCase() : "当前";
        return `当前房间规则集（${label}）未提供 /check 检定命令；可使用 /dice 进行通用掷骰。`;
    }
}
window.CheckTool = CheckTool;
