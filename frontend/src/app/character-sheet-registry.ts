// 角色卡渲染器注册表。
//
// 角色卡是规则集私有结构：核心页面只准备容器与入口，具体卡面由规则集模块接管。
// 这里只做「按 id 登记 / 查询」，不解释任何规则语义；未登记时统一交给外壳做阻断提示，
// 绝不退化成只读伪界面（否则会出现「看似正常、实际改不了」的状态）。

interface CharacterSheetModule {
    id: string;
    /** 接管 #characterWorkspace 的渲染与事件绑定。 */
    initCharacterSheet: () => void;
    clearCharacterManagement?: () => void;
    reloadCharacterManagement?: () => Promise<void>;
    /** 该模块能否接管指定规则集的卡；缺省表示只接管登记时使用的 id。 */
    supportsRuleset?: (rulesetId?: string | null) => boolean;
}

interface CharacterSheetRegistryApi {
    register: (id: string, module: CharacterSheetModule, options?: { default?: boolean }) => void;
    get: (id: string) => CharacterSheetModule | undefined;
    ids: () => string[];
    defaultModule: () => CharacterSheetModule | undefined;
    /**
     * 按角色卡的 ``rulesetId`` 取模块：
     * - 缺省 / 空值走默认模块（历史卡兼容）；
     * - 明确声明且未登记时返回 undefined，由调用方阻断，不做静默降级。
     */
    forRuleset: (rulesetId?: string | null) => CharacterSheetModule | undefined;
}

interface Window {
    TrpgCharacterSheetRegistry?: CharacterSheetRegistryApi;
}

(function installCharacterSheetRegistry(global: Window & typeof globalThis): void {
    "use strict";

    const modules = new Map<string, CharacterSheetModule>();
    let defaultId = "";

    function keyOf(id: unknown): string {
        return String(id ?? "").trim().toLowerCase();
    }

    function register(id: string, module: CharacterSheetModule, options?: { default?: boolean }): void {
        const key = keyOf(id);
        if (!key || !module) return;
        modules.set(key, module);
        if (options?.default || !defaultId) defaultId = key;
    }

    function get(id: string): CharacterSheetModule | undefined {
        return modules.get(keyOf(id));
    }

    function ids(): string[] {
        return Array.from(modules.keys());
    }

    function defaultModule(): CharacterSheetModule | undefined {
        if (defaultId) {
            const module = modules.get(defaultId);
            if (module) return module;
        }
        // 没有显式默认值时，只有唯一模块才允许自动接管；多个模块时必须显式指定。
        const registered = Array.from(modules.values());
        return registered.length === 1 ? registered[0] : undefined;
    }

    function forRuleset(rulesetId?: string | null): CharacterSheetModule | undefined {
        const key = keyOf(rulesetId);
        if (!key) return defaultModule();
        const direct = modules.get(key);
        if (direct) return direct;
        for (const module of modules.values()) {
            try {
                if (module.supportsRuleset?.(rulesetId)) return module;
            } catch {
                // 单个模块的判断异常不应影响其他规则集解析。
            }
        }
        return undefined;
    }

    global.TrpgCharacterSheetRegistry = { register, get, ids, defaultModule, forRuleset };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
