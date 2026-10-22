let toolManager: ToolManager | null = null;

document.addEventListener("DOMContentLoaded", () => {
    void initializeApplication();
});

async function initializeApplication(): Promise<void> {
    const dom = window.TrpgDom;
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
    window.COC7CharacterSheet?.initCharacterSheet();
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
