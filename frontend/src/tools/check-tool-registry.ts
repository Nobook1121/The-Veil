// /check 检定命令的规则集分派注册表。
//
// /check 的语义（属性别名 / 技能键表 / 难度解析 / 目标值折算 / 成功等级文案）
// 是规则集私有概念：核心工具只准备骰面工具与命令文本，具体检定数学由规则集模块
// 接管。这里只做「按 id 登记 / 查询」，不解释任何规则语义；形态与 character-sheet-registry
// 的 register / forRuleset 完全同构（同一套边界收敛惯例）。
//
// 未登记时核心壳显式拒绝并指向通用骰入口，绝不静默降级成某个规则集的检定
// （对齐 C1 / D3 的拒绝教义：「看似可用、实际错位」比「明确不可用」更糟）。

interface CheckToolModule {
    id: string;
    /**
     * 核心只转交骰面工具与命令文本；检定规则全在模块内解释。
     * 返回值是渲染进聊天的一行文本（与旧版 /check 输出同一形态）。
     */
    run: (dice: DiceTool, command: string) => string;
}

interface CheckToolRegistryApi {
    register: (id: string, module: CheckToolModule, options?: { default?: boolean }) => void;
    get: (id: string) => CheckToolModule | undefined;
    ids: () => string[];
    defaultModule: () => CheckToolModule | undefined;
    /**
     * 按房间 ``ruleset_id`` 取模块：
     * - 缺省 / 空值走默认模块（历史房间兼容，coc7 登记为默认）；
     * - 明确声明且未登记时返回 undefined，由调用方显式拒绝，不做静默降级。
     */
    forRuleset: (rulesetId?: string | null) => CheckToolModule | undefined;
}

interface Window {
    TrpgCheckToolRegistry?: CheckToolRegistryApi;
}

(function installCheckToolRegistry(global: Window & typeof globalThis): void {
    "use strict";

    const modules = new Map<string, CheckToolModule>();
    let defaultId = "";

    function keyOf(id: unknown): string {
        return String(id ?? "").trim().toLowerCase();
    }

    function register(id: string, module: CheckToolModule, options?: { default?: boolean }): void {
        const key = keyOf(id);
        if (!key || !module) return;
        modules.set(key, module);
        if (options?.default || !defaultId) defaultId = key;
    }

    function get(id: string): CheckToolModule | undefined {
        return modules.get(keyOf(id));
    }

    function ids(): string[] {
        return Array.from(modules.keys());
    }

    function defaultModule(): CheckToolModule | undefined {
        if (defaultId) {
            const module = modules.get(defaultId);
            if (module) return module;
        }
        // 没有显式默认值时，只有唯一模块才允许自动接管；多个模块时必须显式指定。
        const registered = Array.from(modules.values());
        return registered.length === 1 ? registered[0] : undefined;
    }

    function forRuleset(rulesetId?: string | null): CheckToolModule | undefined {
        const key = keyOf(rulesetId);
        if (!key) return defaultModule();
        return modules.get(key);
    }

    global.TrpgCheckToolRegistry = { register, get, ids, defaultModule, forRuleset };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
