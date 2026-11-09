let toolManager: ToolManager | null = null;

document.addEventListener("DOMContentLoaded", () => {
    void initializeApplication();
});

async function initializeApplication(): Promise<void> {
    const dom = window.TrpgDom;
    initModalOpeningSync();
    await window.TrpgI18n?.ready;
    window.TrpgI18n?.apply();

    if (await SetupWizard.init()) {
        // 向导处于激活状态：用户点击“跳过全部引导”后继续初始化主应用。
        await SetupWizard.whenSkipped();
    }

    toolManager = new ToolManager();
    window.toolManager = toolManager;

    initTabs();
    initSettingsTabs();

    await loadAndApplyConfigs();
    await initAIPlatforms();
    await initAuth();
    initScenarioManagement();
    initCharacterManagement();
    initChat();
    await initNetworkConfig();
    initRoomManagement();
    initSidebarToggle();
    await autoLoadLastRoom();

    dom.on(document, "hidden.bs.modal", () => {
        setTimeout(() => {
            dom.removeModalBackdropsWhenIdle();
        }, 100);
    });
}

/**
 * 统一 Bootstrap 弹窗的打开时序。
 *
 * Bootstrap 的 Modal.show() 会等遮罩的 CSS transition 结束才调用 _showElement，
 * 弹窗内容因此比遮罩（以及世界意志法阵）晚整整一个过渡时长。打开阶段给 body
 * 打上 modal-opening，配合样式把遮罩的 transition 关掉、改用关键帧淡入，
 * _showElement 便会立即执行；关闭阶段仍保留原来的过渡动画。
 */
function initModalOpeningSync(): void {
    document.addEventListener("show.bs.modal", () => {
        document.body.classList.add("modal-opening");
        // 遮罩是在 show 事件之后、当前任务结束前插入 body 的，微任务刚好能看到它。
        queueMicrotask(() => {
            const backdrops = document.querySelectorAll<HTMLElement>(".modal-backdrop.show:not(.is-opening)");
            const backdrop = backdrops[backdrops.length - 1];
            if (!backdrop) return;
            backdrop.classList.add("is-opening");
            backdrop.addEventListener("animationend", () => backdrop.classList.remove("is-opening"), { once: true });
        });
    }, true);

    const finishOpening = (): void => {
        document.body.classList.remove("modal-opening");
        document.querySelectorAll<HTMLElement>(".modal-backdrop.is-opening").forEach((element) => {
            element.classList.remove("is-opening");
        });
    };
    document.addEventListener("shown.bs.modal", finishOpening, true);
    document.addEventListener("hidden.bs.modal", finishOpening, true);
}

async function loadAndApplyConfigs(): Promise<void> {
    try {
        await configManager.loadConfig("general");
        configManager.applyGeneralSettings();
        configManager.initThemeSystem();
    } catch (error) {
        console.error("failed to load config", error);
    }
}

function initCharacterManagement(): void {
    // 角色卡由规则集模块登记，外壳负责选择模块或明确阻断（不再直连 CoC 全局）。
    window.TrpgCharacterSheetHost?.init();
}

function initSidebarToggle(): void {
    const dom = window.TrpgDom;
    const toggleBtn = dom.byId("sidebarToggle");
    const sidebar = dom.byId("sidebar");
    const mainContent = dom.byId("mainContent");

    if (!toggleBtn || !sidebar || !mainContent) return;

    function setSidebarExpanded(isExpanded: boolean): void {
        sidebar?.classList.toggle("sidebar-expanded", isExpanded);
        sidebar?.classList.toggle("sidebar-collapsed", !isExpanded);
        mainContent?.classList.toggle("sidebar-collapsed-content", !isExpanded);
        dom.setButtonDisclosure(toggleBtn, {
            expanded: isExpanded,
            expandedLabel: "Collapse sidebar",
            collapsedLabel: "Expand sidebar",
            expandedIconClass: "fa fa-angle-double-left",
            collapsedIconClass: "fa fa-angle-double-right",
        });
    }

    dom.on(toggleBtn, "click", () => {
        const isCollapsed = sidebar.classList.contains("sidebar-collapsed");
        setSidebarExpanded(isCollapsed);
    });
}
