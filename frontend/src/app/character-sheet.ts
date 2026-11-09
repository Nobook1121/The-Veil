// 角色卡页面外壳。
//
// 这里不含任何规则集私有逻辑：只从 TrpgCharacterSheetRegistry 取默认模块并委托初始化。
// 「兜底」策略是明确阻断而不是通用只读渲染——当前没有任何通用渲染器能可靠编辑
// CoC 卡的属性/技能/物品并完成导入导出与保存，给出半残界面只会让人误以为数据还在、
// 实际改不了，因此模块缺失时直接报错并禁用入口。

interface CharacterSheetHostApi {
    init: () => void;
}

interface Window {
    TrpgCharacterSheetHost?: CharacterSheetHostApi;
}

(function installCharacterSheetHost(global: Window & typeof globalThis): void {
    "use strict";

    const BLOCKED_ACTION_IDS = [
        "createCharacter",
        "importCharacter",
        "exportCharacter",
        "openCharacterGallery",
        "characterBatchToggle",
    ];

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
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

    function init(): void {
        const registry = global.TrpgCharacterSheetRegistry;
        const module = registry?.defaultModule();
        if (!module) {
            // 其他脚本（登录、房间）会调用这两个全局钩子，缺省为 no-op 避免连锁报错。
            global.clearCharacterManagement = () => {};
            global.reloadCharacterManagement = async () => {};
            block("没有可用的角色卡渲染器：规则集模块未加载，无法打开或编辑角色卡。");
            return;
        }
        global.clearCharacterManagement = () => module.clearCharacterManagement?.();
        global.reloadCharacterManagement = async () => {
            await module.reloadCharacterManagement?.();
        };
        module.initCharacterSheet();
    }

    global.TrpgCharacterSheetHost = { init };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
