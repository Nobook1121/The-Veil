// 角色卡页面外壳。
//
// 这里不含任何规则集私有逻辑：只从 TrpgCharacterSheetRegistry 取默认模块并委托初始化。
// 页眉标题/描述由 /api/rulesets 返回的插件元数据驱动，插件缺省时回退到中性通用文案。
// 「兜底」策略是明确阻断而不是通用只读渲染——当前没有任何通用渲染器能可靠编辑
// CoC 卡的属性/技能/物品并完成导入导出与保存，给出半残界面只会让人误以为数据还在、
// 实际改不了，因此模块缺失时直接报错并禁用入口。

interface CharacterSheetHostApi {
    init: () => void;
}

interface RulesetHeaderOption {
    id: string;
    display_name?: string;
    description?: string;
    selectable?: boolean;
}

interface Window {
    TrpgCharacterSheetHost?: CharacterSheetHostApi;
}

(function installCharacterSheetHost(global: Window & typeof globalThis): void {
    "use strict";

    const RULESET_HEADER_ENDPOINT = "/api/rulesets";
    const BLOCKED_ACTION_IDS = [
        "createCharacter",
        "importCharacter",
        "exportCharacter",
        "openCharacterGallery",
        "characterBatchToggle",
    ];

    let cachedRulesetOptions: RulesetHeaderOption[] | null = null;
    let rulesetHeaderRequest: Promise<void> | null = null;
    let rulesetHeaderEventsBound = false;
    let roomEventsBound = false;
    let activeSheetRulesetId = "";
    let activeModule: CharacterSheetModule | null = null;
    let activeModuleId = "";

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function i18nText(key: string, fallback: string, values?: Record<string, string>): string {
        return global.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function pluginText(value: unknown): string {
        const text = String(value ?? "").trim();
        if (!text) return "";
        // legacy 词典按源文案做映射；插件文案随中文源进入 locale，英文界面可自动翻译。
        return global.TrpgI18n?.t("", text) || text;
    }

    function pickRuleset(options: RulesetHeaderOption[] | null): RulesetHeaderOption | null {
        if (!options) return null;
        const usable = options.filter((option) => (
            option
            && option.selectable !== false
            && Boolean(pluginText(option.display_name))
        ));
        if (!usable.length) return null;
        if (activeSheetRulesetId) {
            const preferred = usable.find((option) => (
                String(option.id || "").trim().toLowerCase() === activeSheetRulesetId
            ));
            if (preferred) return preferred;
        }
        return usable[0] || null;
    }

    function setHeaderText(element: HTMLElement | null, text: string): void {
        if (!element) return;
        element.removeAttribute("data-i18n");
        element.dataset.i18nDynamic = "true";
        element.textContent = text;
    }

    function applyRulesetHeader(option: RulesetHeaderOption | null): void {
        if (typeof document === "undefined") return;
        const title = document.getElementById("characterPageTitle");
        const subtitle = document.getElementById("characterPageSubtitle");
        if (!title && !subtitle) return;
        const name = option ? pluginText(option.display_name) : "";
        const description = option ? pluginText(option.description) : "";
        setHeaderText(
            title,
            name
                ? i18nText("character.page.title.ruleset", "{ruleset} 角色卡", { ruleset: name })
                : i18nText("character.page.title", "角色卡工作区"),
        );
        setHeaderText(
            subtitle,
            description || i18nText("character.page.subtitle", "管理角色卡的属性、技能、装备、伤势与状态记录。"),
        );
    }

    async function loadRulesetHeader(): Promise<void> {
        if (rulesetHeaderRequest) return rulesetHeaderRequest;
        rulesetHeaderRequest = (async () => {
            try {
                await global.TrpgI18n?.ready;
            } catch {
                // 词典加载失败不阻塞角色卡操作，直接使用源码文案。
            }
            try {
                const result = await global.TrpgApi.requestWithResponse<ApiResponse<RulesetHeaderOption[]>>(RULESET_HEADER_ENDPOINT);
                const body = result?.data;
                cachedRulesetOptions = body?.success && Array.isArray(body.data) ? body.data : [];
            } catch {
                // 未登录、接口不可达或旧服务端都不阻塞角色卡页面：用中性文案即可。
                cachedRulesetOptions = [];
            }
            applyRulesetHeader(pickRuleset(cachedRulesetOptions));
            rulesetHeaderRequest = null;
        })();
        return rulesetHeaderRequest;
    }

    function bindRulesetHeaderEvents(): void {
        if (rulesetHeaderEventsBound) return;
        rulesetHeaderEventsBound = true;
        global.addEventListener("trpg:locale-changed", () => {
            applyRulesetHeader(pickRuleset(cachedRulesetOptions));
        });
        global.addEventListener("trpg:user-changed", () => {
            cachedRulesetOptions = null;
            void (async () => {
                if (rulesetHeaderRequest) await rulesetHeaderRequest;
                await loadRulesetHeader();
            })();
        });
    }

    function block(reason: string): void {
        if (typeof document === "undefined") return;
        const workspace = document.getElementById("characterWorkspace");
        if (workspace) {
            workspace.innerHTML = `
                <div class="character-matrix-bg" aria-hidden="true"></div>
                <div role="alert" style="position:relative;z-index:1;max-width:680px;margin:48px auto;padding:24px 28px;border:1px solid rgba(255,110,110,.45);border-radius:12px;background:rgba(40,10,12,.72);color:#ffe3e3;">
                    <h3 style="margin:0 0 12px;">角色卡不可用</h3>
                    <p style="margin:0 0 8px;">${escapeHtml(reason)}</p>
                    <p style="margin:0;opacity:.82;">已禁用创建、导入、编辑与导出入口，避免在缺少规则集实现时写入不完整或错误的数据。</p>
                </div>`;
        }
        BLOCKED_ACTION_IDS.forEach((id) => {
            const element = document.getElementById(id) as HTMLButtonElement | null;
            if (element) element.disabled = true;
        });
        const notifier = (global as unknown as { showNotification?: (message: string, type?: string) => void }).showNotification;
        if (typeof notifier === "function") notifier(reason, "error");
    }

    /**
     * 切换规则集模块前重建工作区：克隆节点可以一次性丢掉旧模块绑在按钮上的
     * 直接监听，避免 CoC 与 5e 的创建/编辑入口互相触发。克隆保留内部标记结构，
     * 但清掉 ``dataset.initialized``，让新模块按自己的流程重新初始化。
     */
    function resetWorkspaceBindings(): void {
        const workspace = document.getElementById("characterWorkspace");
        if (!workspace) return;
        const replacement = workspace.cloneNode(false) as HTMLElement;
        replacement.innerHTML = workspace.innerHTML;
        delete replacement.dataset.initialized;
        workspace.replaceWith(replacement);
    }

    function activateModule(module: CharacterSheetModule): void {
        const moduleId = String(module.id || "").trim().toLowerCase();
        if (activeModuleId === moduleId && activeModule) {
            void module.reloadCharacterManagement?.();
            return;
        }
        activeModule?.clearCharacterManagement?.();
        resetWorkspaceBindings();
        activeModule = module;
        activeModuleId = moduleId;
        global.clearCharacterManagement = () => module.clearCharacterManagement?.();
        global.reloadCharacterManagement = async () => {
            await module.reloadCharacterManagement?.();
        };
        activeSheetRulesetId = moduleId;
        try {
            global.localStorage?.setItem("trpg:active-character-ruleset", moduleId);
        } catch {
            // 隐私模式下不可写；不影响本次切换。
        }
        applyRulesetHeader(pickRuleset(cachedRulesetOptions));
        module.initCharacterSheet();
    }

    function storedRulesetId(): string {
        try {
            return String(global.localStorage?.getItem("trpg:active-character-ruleset") || "").trim().toLowerCase();
        } catch {
            return "";
        }
    }

    function resolveModule(): CharacterSheetModule | undefined {
        const registry = global.TrpgCharacterSheetRegistry;
        // 房间内跟随房间规则集；不在房间时沿用上次使用的模块，最后回退默认（CoC）。
        const roomRulesetId = String(global.currentRoom?.ruleset_id || "").trim().toLowerCase();
        const rulesetId = roomRulesetId || storedRulesetId();
        return (rulesetId ? registry?.forRuleset(rulesetId) : undefined) || registry?.defaultModule();
    }

    function syncModule(): void {
        const module = resolveModule();
        if (!module) {
            const roomRulesetId = String(global.currentRoom?.ruleset_id || "").trim().toLowerCase();
            global.clearCharacterManagement = () => {};
            global.reloadCharacterManagement = async () => {};
            block(
                roomRulesetId
                    ? `没有可用的角色卡渲染器：当前房间规则集 ${roomRulesetId} 尚未加载对应模块。`
                    : "没有可用的角色卡渲染器：规则集模块未加载，无法打开或编辑角色卡。"
            );
            return;
        }
        activateModule(module);
    }

    function init(): void {
        bindRulesetHeaderEvents();
        if (!roomEventsBound) {
            roomEventsBound = true;
            global.addEventListener("trpg:room-changed", () => syncModule());
        }
        void loadRulesetHeader();
        syncModule();
    }

    global.TrpgCharacterSheetHost = { init };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
