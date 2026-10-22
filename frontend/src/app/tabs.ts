interface AdminUserRecord {
    id: number;
    username: string;
    email: string;
    role: string;
    status: string;
    is_online?: boolean;
    presence?: string;
    created_at?: string;
    last_login?: string;
}

// 用户组（角色）权限等级：管理员只能在自己的权限范围内调整用户组。
const USER_ROLE_RANK: Record<string, number> = { USER: 1, ADMIN: 2, OWNER: 3 };
const USER_ROLE_OPTIONS = ["USER", "ADMIN", "OWNER"];

// 用户管理列表的完整数据缓存，过滤/搜索都基于它在前端进行。
let allUserRecords: AdminUserRecord[] = [];

function initTabs(): void {
    try {
        const navLinks = Array.from(document.querySelectorAll<HTMLAnchorElement>("#sidebar .nav-link"));
        const tabContents = Array.from(document.querySelectorAll<HTMLElement>(".tab-content"));

        if (navLinks.length === 0 || tabContents.length === 0) {
            console.error("无法找到导航链接或标签内容");
            return;
        }

        updateNavigationState(document.querySelector<HTMLAnchorElement>("#sidebar .nav-link.active"), navLinks);

        navLinks.forEach((link) => {
            link.addEventListener("click", (event) => {
                event.preventDefault();
                handleMainNavigationClick(link, navLinks, tabContents);
            });
        });

        bindDropdownButtons();
        refreshAdminNavigation();
        console.log("标签切换初始化成功");
    } catch (error) {
        console.error("初始化标签切换时出错:", error);
    }
}

function handleMainNavigationClick(
    link: HTMLAnchorElement,
    navLinks: HTMLAnchorElement[],
    tabContents: HTMLElement[],
): void {
    const isInDropdown = Boolean(link.closest(".dropdown-container"));
    if (!isInDropdown) {
        closeDropdownButtons();
    }

    navLinks.forEach((item) => item.classList.remove("active"));
    tabContents.forEach((tab) => tab.classList.remove("active"));

    link.classList.add("active");
    updateNavigationState(link, navLinks);

    const tabId = link.dataset.tab;
    if (!tabId) {
        console.error("导航链接缺少 data-tab 属性");
        return;
    }

    const targetTab = document.getElementById(tabId);
    if (!targetTab) {
        console.error(`找不到 id 为 ${tabId} 的标签内容`);
        return;
    }

    targetTab.classList.add("active");
    console.log(`切换到标签页: ${tabId}`);

    // 设置页在页面顶部提供横向子标签，因此仅在 hash 明确指向 settings-* 时切换。
    if (tabId === "settings") {
        const hash = link.hash.replace("#", "");
        if (hash.startsWith("settings-")) switchSettingsTab(hash.slice("settings-".length));
    }
}

function switchMainTab(tabId: string, options: { clearNav?: boolean } = {}): void {
    const navLinks = Array.from(document.querySelectorAll<HTMLAnchorElement>("#sidebar .nav-link"));
    const tabContents = Array.from(document.querySelectorAll<HTMLElement>(".tab-content"));
    tabContents.forEach((tab) => tab.classList.remove("active"));
    const targetTab = document.getElementById(tabId);
    if (!targetTab) {
        console.error(`找不到 id 为 ${tabId} 的标签内容`);
        return;
    }
    targetTab.classList.add("active");

    navLinks.forEach((item) => item.classList.remove("active"));
    if (!options.clearNav) {
        const activeLink = navLinks.find((link) => link.dataset.tab === tabId);
        activeLink?.classList.add("active");
        updateNavigationState(activeLink || null, navLinks);
    } else {
        updateNavigationState(null, navLinks);
    }
    refreshMainTabData(tabId);
}

function refreshAdminNavigation(): void {
    const role = window.currentUser?.role || "USER";
    const canSeeAdmin = role === "ADMIN" || role === "OWNER";
    document.querySelectorAll<HTMLElement>("[data-admin-only='true']").forEach((element) => {
        element.hidden = !canSeeAdmin;
    });
}

function updateNavigationState(activeLink: HTMLAnchorElement | null, navLinks: HTMLAnchorElement[]): void {
    navLinks.forEach((link) => {
        if (link === activeLink) {
            link.setAttribute("aria-current", "page");
        } else {
            link.removeAttribute("aria-current");
        }
    });
}

function refreshMainTabData(tabId: string): void {
    if (tabId === "save") {
        void window.loadRoomsList?.();
    }
    if (tabId === "user-settings") {
        void window.loadUserSettings?.();
    }
}

function bindDropdownButtons(): void {
    document.querySelectorAll<HTMLElement>(".dropdown-btn").forEach((button) => {
        button.setAttribute("aria-expanded", button.classList.contains("active") ? "true" : "false");
        button.addEventListener("click", () => {
            const dropdownContent = button.nextElementSibling as HTMLElement | null;
            if (!dropdownContent) return;

            const isExpanded = dropdownContent.style.display !== "block";
            button.classList.toggle("active", isExpanded);
            button.setAttribute("aria-expanded", String(isExpanded));
            dropdownContent.style.display = isExpanded ? "block" : "none";

            document.querySelectorAll<HTMLElement>(".dropdown-btn").forEach((otherButton) => {
                if (otherButton === button) return;
                otherButton.classList.remove("active");
                otherButton.setAttribute("aria-expanded", "false");
                const otherContent = otherButton.nextElementSibling as HTMLElement | null;
                if (otherContent) otherContent.style.display = "none";
            });
        });
    });
}

function closeDropdownButtons(): void {
    document.querySelectorAll<HTMLElement>(".dropdown-btn").forEach((button) => {
        button.classList.remove("active");
        const dropdownContent = button.nextElementSibling as HTMLElement | null;
        if (dropdownContent) dropdownContent.style.display = "none";
    });
}

function switchSettingsTab(tabName: string): void {
    const settingsTabs = document.querySelectorAll<HTMLElement>(".settings-tab");
    const settingsContents = document.querySelectorAll<HTMLElement>(".settings-content");

    settingsTabs.forEach((tab) => {
        tab.classList.remove("active");
        tab.setAttribute("aria-selected", "false");
    });
    settingsContents.forEach((content) => content.classList.remove("active"));

    const targetTab = document.querySelector<HTMLElement>(`.settings-tab[data-settings="${CSS.escape(tabName)}"]`);
    const targetContent = document.getElementById(`${tabName}-settings-content`);
    targetTab?.classList.add("active");
    targetTab?.setAttribute("aria-selected", "true");
    targetContent?.classList.add("active");
    if (tabName === "permissions") {
        void loadPermissionConfig();
    }
    if (tabName === "users") {
        void loadUserManagement();
    }
    if (tabName === "knowledge") {
        void loadRulesetKnowledge();
        void loadScenarioKnowledge();
        void loadEmbeddingHealth();
    }
    // 骰子贴图库按需初始化，避免非管理员页面加载时发出多余请求。
    window.TrpgAdminDiceTextures?.init();
}

function initSettingsTabs(): void {
    const settingsTabs = document.querySelectorAll<HTMLElement>(".settings-tab");
    const settingsContents = document.querySelectorAll<HTMLElement>(".settings-content");
    if (settingsTabs.length === 0 || settingsContents.length === 0) {
        console.error("无法找到设置标签或设置内容");
        return;
    }

    settingsTabs.forEach((tab) => {
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-selected", tab.classList.contains("active") ? "true" : "false");
        tab.addEventListener("click", () => switchSettingsTab(tab.dataset.settings || ""));
    });

    const temperatureSlider = document.getElementById("temperature") as HTMLInputElement | null;
    const temperatureValue = document.getElementById("temperatureValue");
    if (temperatureSlider && temperatureValue) {
        temperatureSlider.addEventListener("input", () => {
            temperatureValue.textContent = temperatureSlider.value;
        });
    }

    document.getElementById("saveGeneralSettingsChanges")?.addEventListener("click", () => {
        void saveGeneralSettingsChanges();
    });
    document.getElementById("cancelGeneralSettingsChanges")?.addEventListener("click", cancelGeneralSettingsChanges);

    bindGeneralSelectSetting("themeSelect", "appearance", "theme");
    bindGeneralSelectSetting("languageSelect", "language", "language");

    bindGeneralCheckboxSetting("enableSound", "notification", "enable_sound");
    bindGeneralCheckboxSetting("enableNotification", "notification", "enable_desktop_notification");
    bindGeneralCheckboxSetting("enableAutosave", "autosave", "enabled");
    bindGeneralCheckboxSetting("showTimestamp", "chat", "show_timestamp");
    bindGeneralNumberSetting("messageFontSize", "chat", "message_font_size", 10, 24);
    bindGeneralCheckboxSetting("enableAIResponseLock", "ai", "enable_ai_response_lock");
    bindGeneralNumberSetting("messageRateLimit", "chat", "message_rate_limit", 0, 60);

    bindGeneralCheckboxSetting("streamOutput", "ai", "stream_output");
    bindGeneralCheckboxSetting("scenarioImportStreamOutput", "scenario_import", "stream_output");
    bindGeneralCheckboxSetting("debugMode", "ai", "debug_mode");
    bindGeneralCheckboxSetting("showAIHints", "ai", "show_ai_hints");
    bindGeneralNumberSetting("autosaveInterval", "autosave", "interval", 30, 3600);
    bindGeneralNumberSetting("autosaveMaxNodes", "autosave", "max_nodes", 1, 50);
    bindGeneralNumberSetting("triggerMaxFileSize", "scenario", "trigger_max_file_size", 1024, 52428800);
    bindGeneralNumberSetting("maxToolRounds", "ai", "max_tool_rounds", 1, 30);
    bindGeneralNumberSetting("aiRequestTimeout", "ai", "ai_request_timeout", 30, 1800);
    bindGeneralNumberSetting("aiRequestMaxRetries", "ai", "ai_request_max_retries", 0, 20);
    bindGeneralCheckboxSetting("constrainMaxTokens", "ai", "constrain_max_tokens");
    bindGeneralNumberSetting("maxTokens", "ai", "max_tokens", 1, 200000);
    bindGeneralCheckboxSetting("dedupeToolCalls", "ai.prompt", "dedupe_tool_calls");
    bindGeneralCheckboxSetting("semanticCacheEnabled", "ai.cache", "semantic_enabled");
    bindGeneralNumberSetting("semanticCacheTtl", "ai.cache", "semantic_ttl_seconds", 30, 86400);
    bindGeneralNumberSetting("exactCacheTtl", "ai.cache", "exact_ttl_seconds", 30, 86400);
    bindGeneralCheckboxSetting("recordPromptSegments", "ai", "record_prompt_segments");
    bindGeneralNumberSetting("diceCriticalThresholdDefault", "ai", "dice_critical_threshold", 0, 100);
    bindGeneralNumberSetting("diceFumbleThresholdDefault", "ai", "dice_fumble_threshold", 0, 100);
    bindGeneralSelectSetting("diceAnimationLevel", "ai", "dice_animation_level");
    bindGeneralCheckboxSetting("dicePhysicsResults", "ai", "dice_physics_results");
    bindGeneralCheckboxSetting("dicePlayerRollDefault", "ai", "dice_player_roll");
    bindGeneralNumberSetting("dicePlayerRollTimeout", "ai", "dice_player_roll_timeout", 30, 300);
    bindGeneralSelectSetting("diceRerollMode", "ai", "dice_reroll_mode");
    bindGeneralNumberSetting("diceRerollLimit", "ai", "dice_reroll_limit", 1, 20);
    bindDicePlayerRollTimeoutState();
    bindDiceRerollLimitState();
    bindGeneralNumberSetting("retrievalTopKDefault", "ai.knowledge", "top_k_default", 1, 32);
    bindGeneralNumberSetting("retrievalTopKDirect", "ai.knowledge", "top_k_direct", 1, 32);
    bindGeneralNumberSetting("chunkChildMaxChars", "ai.knowledge", "chunk_child_max_chars", 200, 8000);
    bindGeneralNumberSetting("chunkParentMaxChars", "ai.knowledge", "chunk_parent_max_chars", 200, 16000);
    bindGeneralNumberSetting("stickyRounds", "ai.knowledge", "sticky_rounds", 0, 10);
    bindGeneralNumberSetting("cooldownRounds", "ai.knowledge", "cooldown_rounds", 0, 20);
    bindGeneralCheckboxSetting("keywordChannelEnabled", "ai.knowledge", "keyword_channel_enabled");
    bindGeneralCheckboxSetting("recursiveScanning", "ai.knowledge", "recursive_scanning");
    bindGeneralNumberSetting("maxRecursionDepth", "ai.knowledge", "max_recursion_depth", 0, 10);
    bindGeneralNumberSetting("lorebookTokenBudget", "ai.knowledge", "lorebook_token_budget", 0, 200000);
    bindGeneralCheckboxSetting("cloudEmbeddingEnabled", "ai", "cloud_embedding_enabled");
    bindGeneralCheckboxSetting("enableRoomConsole", "room_console", "enabled");
    bindGeneralCheckboxSetting("mirrorRoomConsole", "room_console", "mirror_to_server_console");
    bindGeneralNumberSetting("roomConsoleMaxEntries", "room_console", "max_entries", 50, 5000);
    bindGeneralCheckboxSetting("siteAgentEnabled", "site_agent", "enabled");
    bindGeneralNumberSetting("siteAgentDailyTokenBudget", "site_agent", "daily_token_budget", 0, 100000000);
    bindGeneralNumberSetting("siteAgentMaxTurnsPerHour", "site_agent", "max_turns_per_hour", 1, 1000);
    bindGeneralNumberSetting("siteAgentMaxToolRounds", "site_agent", "max_tool_rounds", 1, 20);
    bindGeneralNumberSetting("siteAgentRequestTimeout", "site_agent", "request_timeout", 30, 1800);
    bindGeneralNumberSetting("siteAgentMaxHistoryMessages", "site_agent", "max_history_messages", 4, 200);
    bindGeneralNumberSetting("siteAgentMaxToolResultChars", "site_agent", "max_tool_result_chars", 200, 20000);
    bindGeneralNumberSetting("siteAgentActionTtlSeconds", "site_agent", "action_ttl_seconds", 30, 3600);
    document.getElementById("savePermissionConfig")?.addEventListener("click", () => {
        void savePermissionConfig();
    });
    document.getElementById("refreshUserManagement")?.addEventListener("click", () => {
        void loadUserManagement(true);
    });
    bindUserManagementFilters();
    document.getElementById("refreshAITokenDashboard")?.addEventListener("click", () => {
        void loadAITokenDashboard();
    });
    void loadAITokenDashboard();
}

async function loadRulesetKnowledge(): Promise<void> {
    const list = document.getElementById("rulesetList");
    const select = document.getElementById("rulesetSelect") as HTMLSelectElement | null;
    if (!list || list.dataset.loaded === "true") return;
    try {
        const response = await TrpgApi.get<ApiResponse<Array<Record<string, unknown>>>>("/api/knowledge-bases/rulesets");
        if (!response.success || !response.data) throw new Error(response.message || "规则集加载失败");
        if (select) {
            select.innerHTML = response.data.map((item) => `<option value="${String(item.ruleset_id || "")}">${String(item.display_name || item.ruleset_id || "")}</option>`).join("");
        }
        list.innerHTML = response.data.map((item) => `<div class="list-group-item"><strong>${String(item.display_name || item.ruleset_id || "")}</strong><span class="text-muted ms-2">版本 ${String(item.active_version || "未索引")} · ${String(Array.isArray(item.sources) ? item.sources.length : 0)} 个来源</span></div>`).join("");
        list.dataset.loaded = "true";
    } catch (error) {
        list.textContent = settingsErrorMessage(error);
    }
    document.getElementById("uploadRulesetButton")?.addEventListener("click", () => void uploadRulesetSource());
    document.getElementById("scenarioKnowledgeRefresh")?.addEventListener("click", () => void loadScenarioKnowledge());
}

async function loadScenarioKnowledge(): Promise<void> {
    const list = document.getElementById("scenarioKnowledgeList");
    const search = (document.getElementById("scenarioKnowledgeSearch") as HTMLInputElement | null)?.value || "";
    if (!list) return;
    try {
        const response = await TrpgApi.get<ApiResponse<Array<Record<string, unknown>>>>(`/api/knowledge-bases/scenarios?search=${encodeURIComponent(search)}`);
        if (!response.success || !response.data) throw new Error(response.message || "剧本知识库加载失败");
        list.innerHTML = response.data.length ? response.data.map((item) => `<div class="list-group-item d-flex justify-content-between align-items-center"><div><strong>${settingsEscapeHtml(item.title)}</strong><div class="small text-muted">向量 ${Number(item.vector_count || 0)} · ${settingsEscapeHtml(item.backend)}<br>${settingsEscapeHtml(item.path)}</div></div><button class="btn btn-sm btn-outline-danger" data-delete-scenario-kb="${String(item.scenario_id)}">删除知识库</button></div>`).join("") : '<div class="list-group-item text-muted">没有匹配的剧本知识库</div>';
        list.querySelectorAll<HTMLButtonElement>("[data-delete-scenario-kb]").forEach((button) => button.addEventListener("click", async () => {
            const confirmed = await window.TrpgDialog?.confirm({
                title: "删除知识库",
                message: "确认删除该剧本的知识库向量和索引吗？",
                confirmText: "删除",
                danger: true,
            });
            if (!confirmed) return;
            const result = await TrpgApi.del<ApiResponse<unknown>>(`/api/knowledge-bases/scenarios/${encodeURIComponent(button.dataset.deleteScenarioKb || "")}`);
            if (!result.success) throw new Error(result.message || "删除失败");
            await loadScenarioKnowledge();
        }));
    } catch (error) { list.textContent = settingsErrorMessage(error); }
}

async function loadEmbeddingHealth(): Promise<void> {
    const target = document.getElementById("embeddingModelStatus");
    if (!target) return;
    try {
        const response = await TrpgApi.get<ApiResponse<Record<string, unknown>>>("/api/vector/health");
        const embedding = (response.data?.embedding || {}) as Record<string, unknown>;
        const loaded = embedding.loaded === true;
        const label = loaded ? "Embedding 模型已加载" : "Embedding 模型未成功加载";
        (loaded ? console.info : console.warn)(`[Embedding] ${label}`, embedding);
        target.className = `alert ${loaded ? "alert-success" : "alert-warning"}`;
        target.textContent = `${label} · 后端：${String(embedding.backend || "unknown")} · 维度：${String(embedding.dimensions || "未知")}${embedding.model ? ` · 模型：${String(embedding.model)}` : ""}${embedding.error ? ` · ${String(embedding.error)}` : ""}`;
    } catch (error) {
        target.className = "alert alert-warning";
        target.textContent = `无法读取 Embedding 模型状态：${settingsErrorMessage(error)}`;
    }
}

async function uploadRulesetSource(): Promise<void> {
    const fileInput = document.getElementById("rulesetFile") as HTMLInputElement | null;
    const select = document.getElementById("rulesetSelect") as HTMLSelectElement | null;
    const message = document.getElementById("rulesetMessage");
    const file = fileInput?.files?.[0];
    if (!file || !select?.value) { if (message) message.textContent = "请选择规则文件"; return; }
    const form = new FormData(); form.append("file", file);
    try {
        const response = await TrpgApi.post<ApiResponse<unknown>>(`/api/knowledge-bases/${encodeURIComponent(select.value)}/sources`, form);
        if (!response.success) throw new Error(response.message || "上传失败");
        if (message) message.textContent = "上传并索引成功";
        const list = document.getElementById("rulesetList"); if (list) list.dataset.loaded = "";
        await loadRulesetKnowledge();
    } catch (error) { if (message) message.textContent = settingsErrorMessage(error); }
}

async function loadAITokenDashboard(): Promise<void> {
    const body = document.getElementById("aiTokenDashboardBody");
    const dateLabel = document.getElementById("aiTokenDashboardDate");
    const segmentSummary = document.getElementById("aiTokenSegmentSummary");
    if (!body) return;
    try {
        const response = await TrpgApi.get<ApiResponse<{ day: string; prompt_segments?: Record<string, number>; roles: Record<string, { request_count?: number; prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; cache_hit_rate?: number; prefix_cache_hit_rate?: number }> }>>("/api/telemetry/ai/daily");
        if (!response.success || !response.data) throw new Error(response.message || "无法加载 Token 用量");
        if (dateLabel) dateLabel.textContent = `日期：${response.data.day}`;
        const entries = Object.entries(response.data.roles || {});
        body.innerHTML = entries.length ? entries.map(([role, usage]) => `<tr><td>${settingsEscapeHtml(role)}</td><td>${usage.request_count || 0}</td><td>${usage.prompt_tokens || 0}</td><td>${usage.completion_tokens || 0}</td><td>${usage.total_tokens || 0}</td><td>${usage.cache_hit_rate || 0}%</td><td>${usage.prefix_cache_hit_rate || 0}%</td></tr>`).join("") : '<tr><td colspan="7" class="text-muted">暂无用量记录</td></tr>';
        if (segmentSummary) {
            segmentSummary.innerHTML = renderPromptSegmentSummary(response.data.prompt_segments || {});
        }
    } catch (error) {
        body.innerHTML = `<tr><td colspan="7" class="text-danger">${settingsEscapeHtml(settingsErrorMessage(error))}</td></tr>`;
        if (segmentSummary) segmentSummary.textContent = "无法加载分段数据";
    }
}

// 输入 token 分段标签与展示顺序，需与后端 token_accounting.SEGMENT_LABELS 保持一致。
const PROMPT_SEGMENT_LABELS: Array<[string, string]> = [
    ["rules", "系统规则"],
    ["scenario_static", "剧本静态核心"],
    ["scene_static", "场景静态卡"],
    ["knowledge", "知识库检索"],
    ["room_state", "房间动态状态"],
    ["history", "历史对话"],
    ["user_input", "本轮输入"],
];

function renderPromptSegmentSummary(segments: Record<string, number>): string {
    const parts = PROMPT_SEGMENT_LABELS.filter(([key]) => Number(segments[key]) > 0).map(
        ([key, label]) => `<span class="badge bg-secondary me-2 mb-1">${label} ${Number(segments[key])}</span>`,
    );
    // 兜底展示后端可能新增、前端尚未登记的分段键，避免数据存在却不显示。
    Object.entries(segments)
        .filter(([key, value]) => Number(value) > 0 && !PROMPT_SEGMENT_LABELS.some(([known]) => known === key))
        .forEach(([key, value]) => parts.push(`<span class="badge bg-secondary me-2 mb-1">${settingsEscapeHtml(key)} ${Number(value)}</span>`));
    return parts.length ? parts.join("") : "暂无分段数据（可能未开启分段记录）";
}

// 常规设置项：所有改动只更新「草稿」，统一由保存条询问是否保存，
// 点「保存」后才写回配置并生效，点「取消」则回退到上次已保存的值。
type GeneralSettingKind = "checkbox" | "number" | "select";

interface GeneralSettingControl {
    element: HTMLInputElement | HTMLSelectElement;
    kind: GeneralSettingKind;
    section: string;
    key: string;
    minimum?: number;
    maximum?: number;
    savedValue: string;
}

const generalSettingControls: GeneralSettingControl[] = [];

function isConfigObject(value: unknown): value is TomlConfig {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readGeneralSettingValue(control: GeneralSettingControl): string {
    return control.kind === "checkbox"
        ? String((control.element as HTMLInputElement).checked)
        : control.element.value;
}

function registerGeneralSetting(control: GeneralSettingControl): void {
    generalSettingControls.push(control);
    control.element.addEventListener("change", updateGeneralPendingState);
}

function bindGeneralCheckboxSetting(elementId: string, sectionName: string, key: string): void {
    const input = document.getElementById(elementId) as HTMLInputElement | null;
    if (!input) return;
    registerGeneralSetting({ element: input, kind: "checkbox", section: sectionName, key, savedValue: String(input.checked) });
}

function bindGeneralNumberSetting(elementId: string, sectionName: string, key: string, minimum: number, maximum: number): void {
    const input = document.getElementById(elementId) as HTMLInputElement | null;
    if (!input) return;
    input.addEventListener("change", () => {
        const value = Math.max(minimum, Math.min(maximum, Number.parseInt(input.value || "0", 10) || minimum));
        input.value = String(value);
    });
    registerGeneralSetting({ element: input, kind: "number", section: sectionName, key, minimum, maximum, savedValue: input.value });
}

function bindGeneralSelectSetting(elementId: string, sectionName: string, key: string): void {
    const select = document.getElementById(elementId) as HTMLSelectElement | null;
    if (!select) return;
    registerGeneralSetting({ element: select, kind: "select", section: sectionName, key, savedValue: select.value });
}

/** 玩家投掷关闭时，等待超时输入仅置灰，仍保留已保存值。 */
function bindDicePlayerRollTimeoutState(): void {
    const toggle = document.getElementById("dicePlayerRollDefault") as HTMLInputElement | null;
    const timeout = document.getElementById("dicePlayerRollTimeout") as HTMLInputElement | null;
    if (!toggle || !timeout) return;
    const sync = (): void => {
        const locked = timeout.dataset.deploymentLocked === "true";
        timeout.disabled = locked || !toggle.checked;
        timeout.closest<HTMLElement>(".form-group")?.classList.toggle("setting-disabled", locked || !toggle.checked);
    };
    toggle.addEventListener("change", sync);
    sync();
}

/** 重投模式不是「指定次数」时，次数输入仅置灰，仍保留已保存值。 */
function bindDiceRerollLimitState(): void {
    const mode = document.getElementById("diceRerollMode") as HTMLSelectElement | null;
    const limit = document.getElementById("diceRerollLimit") as HTMLInputElement | null;
    if (!mode || !limit) return;
    const sync = (): void => {
        const locked = limit.dataset.deploymentLocked === "true";
        const editable = !locked && mode.value === "limited";
        limit.disabled = !editable;
        limit.closest<HTMLElement>(".form-group")?.classList.toggle("setting-disabled", !editable);
    };
    mode.addEventListener("change", sync);
    sync();
}

/** 部署层（环境变量/命令行参数）锁定的设置项：禁用控件并提示「已被部署配置覆盖」。 */
function applyGeneralDeploymentOverrides(): void {
    generalSettingControls.forEach((control) => {
        const source = configManager.getOverrideSource("general", control.section, control.key);
        const group = control.element.closest<HTMLElement>(".form-group");
        let hint = group?.querySelector<HTMLElement>(".setting-override-hint") || null;
        if (source !== null) {
            control.element.dataset.deploymentLocked = "true";
            control.element.disabled = true;
            group?.classList.add("setting-disabled");
            if (group && !hint) {
                hint = document.createElement("div");
                hint.className = "form-text text-warning setting-override-hint";
                group.appendChild(hint);
            }
            if (hint) {
                const sourceLabel = source === "cli" ? "命令行参数" : "环境变量";
                hint.textContent = `已被部署配置覆盖（${sourceLabel}），此处修改不会生效`;
            }
        } else if (control.element.dataset.deploymentLocked === "true") {
            delete control.element.dataset.deploymentLocked;
            control.element.disabled = false;
            group?.classList.remove("setting-disabled");
            hint?.remove();
        }
    });
}

/** 配置加载并回填 UI 之后调用：把当前界面值记为「已保存基线」。 */
function refreshGeneralSettingsBaseline(): void {
    applyGeneralDeploymentOverrides();
    bindDicePlayerRollTimeoutState();
    bindDiceRerollLimitState();
    generalSettingControls.forEach((control) => {
        control.savedValue = readGeneralSettingValue(control);
    });
    updateGeneralPendingState();
}
window.refreshGeneralSettingsBaseline = refreshGeneralSettingsBaseline;

function updateGeneralPendingState(): void {
    const pendingBar = document.getElementById("generalSettingsPendingSaveBar");
    const count = document.getElementById("generalSettingsPendingChangeCount");
    let dirtyCount = 0;
    generalSettingControls.forEach((control) => {
        const isDirty = readGeneralSettingValue(control) !== control.savedValue;
        if (isDirty) dirtyCount += 1;
        control.element.closest<HTMLElement>(".form-group")?.classList.toggle("setting-dirty", isDirty);
    });
    if (!pendingBar || !count) return;
    pendingBar.hidden = dirtyCount === 0;
    count.textContent = String(dirtyCount);
}

async function saveGeneralSettingsChanges(): Promise<void> {
    const generalConfig = configManager.getConfig("general");
    generalSettingControls.forEach((control) => {
        const existing = generalConfig[control.section];
        const section: TomlConfig = isConfigObject(existing) ? existing : {};
        if (control.kind === "checkbox") {
            section[control.key] = readGeneralSettingValue(control) === "true";
        } else if (control.kind === "number") {
            section[control.key] = Number.parseInt(readGeneralSettingValue(control), 10) || control.minimum || 0;
        } else {
            section[control.key] = readGeneralSettingValue(control);
        }
        generalConfig[control.section] = section;
    });
    if (!(await configManager.saveConfig("general", generalConfig))) return;
    generalSettingControls.forEach((control) => {
        control.savedValue = readGeneralSettingValue(control);
    });
    configManager.applyTheme();
    syncGeneralSettingsLocale();
    updateGeneralPendingState();
}

function cancelGeneralSettingsChanges(): void {
    generalSettingControls.forEach((control) => {
        if (control.kind === "checkbox") {
            (control.element as HTMLInputElement).checked = control.savedValue === "true";
        } else {
            control.element.value = control.savedValue;
        }
    });
    configManager.applyTheme();
    syncGeneralSettingsLocale();
    updateGeneralPendingState();
}

/** 语言选项改动后同步界面语言（保存与取消都需要回放最终选择）。 */
function syncGeneralSettingsLocale(): void {
    const select = document.getElementById("languageSelect") as HTMLSelectElement | null;
    if (select) void window.TrpgI18n?.setLocale(select.value);
}

async function loadPermissionConfig(): Promise<void> {
    const matrix = document.getElementById("permissionMatrix");
    if (!matrix || matrix.dataset.loaded === "true") return;
    matrix.textContent = "正在加载权限配置...";

    try {
        const response = await TrpgApi.get<ApiResponse<PermissionConfig>>("/api/config/permissions");
        if (!response.success || !response.data) {
            throw new Error(response.message || response.error || "权限配置加载失败");
        }
        renderPermissionMatrix(response.data);
    } catch (error) {
        matrix.textContent = settingsErrorMessage(error);
    }
}

async function loadUserManagement(forceReload = false): Promise<void> {
    const list = document.getElementById("userManagementList");
    const message = document.getElementById("userManagementMessage");
    if (!list) return;
    if (!forceReload && list.dataset.loaded === "true") return;

    list.innerHTML = `<tr><td colspan="8" class="text-muted">正在加载用户信息...</td></tr>`;
    if (message) {
        message.textContent = "";
        message.className = "settings-message";
    }

    try {
        const response = await TrpgApi.get<ApiResponse<AdminUserRecord[]>>("/api/users");
        if (!response.success || !response.data) {
            throw new Error(response.message || response.error || "用户信息加载失败");
        }
        allUserRecords = response.data;
        list.dataset.loaded = "true";
        applyUserManagementFilter();
        if (message) {
            message.textContent = `已加载 ${response.data.length} 名用户`;
            message.className = "settings-message success";
        }
    } catch (error) {
        allUserRecords = [];
        list.innerHTML = `<tr><td colspan="8" class="text-muted">${settingsEscapeHtml(settingsErrorMessage(error))}</td></tr>`;
        if (message) {
            message.textContent = settingsErrorMessage(error);
            message.className = "settings-message error";
        }
    }
}

function parseFilterDate(value: string): number | null {
    if (!value) return null;
    const date = new Date(`${value}T00:00:00`);
    return Number.isNaN(date.getTime()) ? null : date.getTime();
}

function isWithinDateRange(value: string | undefined, from: string, to: string): boolean {
    const fromTime = parseFilterDate(from);
    const toTime = parseFilterDate(to);
    if (fromTime === null && toTime === null) return true;
    // 未登录/无记录的账号在按时间过滤时应被排除，而不是当成任意时间都命中。
    if (!value) return false;
    const time = new Date(value).getTime();
    if (Number.isNaN(time)) return false;
    if (fromTime !== null && time < fromTime) return false;
    // 结束日期按“当天 23:59:59”包含，避免漏掉当天的记录。
    if (toTime !== null && time > toTime + 24 * 60 * 60 * 1000 - 1) return false;
    return true;
}

function applyUserManagementFilter(): void {
    const list = document.getElementById("userManagementList");
    if (!list) return;

    const role = (document.getElementById("userFilterRole") as HTMLSelectElement | null)?.value || "";
    const createdFrom = (document.getElementById("userFilterCreatedFrom") as HTMLInputElement | null)?.value || "";
    const createdTo = (document.getElementById("userFilterCreatedTo") as HTMLInputElement | null)?.value || "";
    const lastLoginFrom = (document.getElementById("userFilterLastLoginFrom") as HTMLInputElement | null)?.value || "";
    const lastLoginTo = (document.getElementById("userFilterLastLoginTo") as HTMLInputElement | null)?.value || "";
    const keyword = ((document.getElementById("userManagementSearch") as HTMLInputElement | null)?.value || "").trim().toLowerCase();

    const filtered = allUserRecords.filter((user) => {
        if (role && String(user.role).toUpperCase() !== role) return false;
        if (!isWithinDateRange(user.created_at, createdFrom, createdTo)) return false;
        if (!isWithinDateRange(user.last_login, lastLoginFrom, lastLoginTo)) return false;
        if (keyword) {
            const haystack = [user.username, user.email, user.role, user.status]
                .map((item) => String(item || "").toLowerCase())
                .join(" ");
            if (!haystack.includes(keyword)) return false;
        }
        return true;
    });

    renderUserManagementList(filtered);

    const summary = document.getElementById("userFilterSummary");
    const hasFilter = Boolean(role || createdFrom || createdTo || lastLoginFrom || lastLoginTo || keyword);
    if (summary) {
        summary.textContent = hasFilter ? `已筛选 ${filtered.length} / ${allUserRecords.length} 名用户` : "";
        summary.className = "settings-message";
    }
    const toggle = document.getElementById("toggleUserFilter");
    if (toggle) {
        toggle.classList.toggle("active", hasFilter);
        toggle.setAttribute("aria-expanded", String(hasFilter || !(document.getElementById("userFilterPanel") as HTMLElement | null)?.hidden));
    }
}

function resetUserManagementFilter(): void {
    ["userFilterRole", "userFilterCreatedFrom", "userFilterCreatedTo", "userFilterLastLoginFrom", "userFilterLastLoginTo", "userManagementSearch"].forEach((id) => {
        const element = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
        if (element) element.value = "";
    });
    applyUserManagementFilter();
}

function bindUserManagementFilters(): void {
    const toggle = document.getElementById("toggleUserFilter");
    const panel = document.getElementById("userFilterPanel");
    if (toggle && panel && toggle.dataset.bound !== "true") {
        toggle.dataset.bound = "true";
        toggle.addEventListener("click", () => {
            panel.hidden = !panel.hidden;
            toggle.setAttribute("aria-expanded", String(!panel.hidden));
        });
    }
    document.getElementById("applyUserFilter")?.addEventListener("click", applyUserManagementFilter);
    document.getElementById("resetUserFilter")?.addEventListener("click", resetUserManagementFilter);
    const search = document.getElementById("userManagementSearch") as HTMLInputElement | null;
    search?.addEventListener("input", applyUserManagementFilter);
    // 时间范围一旦变化立即重算，管理员无需额外点击“应用筛选”。
    ["userFilterCreatedFrom", "userFilterCreatedTo", "userFilterLastLoginFrom", "userFilterLastLoginTo"].forEach((id) => {
        document.getElementById(id)?.addEventListener("change", applyUserManagementFilter);
    });
    document.getElementById("userFilterRole")?.addEventListener("change", applyUserManagementFilter);
}

function renderUserManagementList(users: AdminUserRecord[]): void {
    const list = document.getElementById("userManagementList");
    if (!list) return;
    if (!users.length) {
        const emptyText = allUserRecords.length ? "没有符合筛选条件的用户" : "暂无用户";
        list.innerHTML = `<tr><td colspan="8" class="text-muted">${emptyText}</td></tr>`;
        return;
    }

    list.innerHTML = users.map((user) => renderUserManagementRow(user)).join("");
    list.querySelectorAll<HTMLButtonElement>("[data-impersonate-user-id]").forEach((button) => {
        button.addEventListener("click", () => void startImpersonation(Number.parseInt(button.dataset.impersonateUserId || "", 10)));
    });
    list.querySelectorAll<HTMLSelectElement>("[data-role-user-id]").forEach((select) => {
        select.addEventListener("change", () => {
            void updateAdminUserRole(Number.parseInt(select.dataset.roleUserId || "", 10), select.value, select);
        });
    });
}

function renderUserManagementRow(user: AdminUserRecord): string {
    const currentUserId = window.currentUser?.user_id;
    const isCurrentUser = String(currentUserId) === String(user.id);
    const canImpersonate = user.status === "active" && !isCurrentUser;
    // 是否在线不影响模拟：仅当账号非 active（停用/封禁）或目标是自己时才禁用。
    const impersonateTitle = canImpersonate
        ? `模拟登录 ${user.username}`
        : isCurrentUser
            ? "不能模拟自己"
            : "仅可模拟状态为 active 的账号";
    return `
        <tr>
            <td>${settingsEscapeHtml(user.username)}</td>
            <td>${settingsEscapeHtml(user.email)}</td>
            <td>${renderAdminUserRoleCell(user, isCurrentUser)}</td>
            <td>${settingsEscapeHtml(user.status)}</td>
            <td>${settingsEscapeHtml(formatOnlineState(user.is_online))}</td>
            <td>${settingsEscapeHtml(formatTimestamp(user.created_at))}</td>
            <td>${settingsEscapeHtml(formatTimestamp(user.last_login))}</td>
            <td class="text-end">
                <div class="user-management-actions">
                    <button type="button" class="btn btn-sm btn-outline-primary" data-impersonate-user-id="${user.id}" title="${settingsEscapeHtml(impersonateTitle)}" ${canImpersonate ? "" : "disabled"}>
                        <i class="fa fa-user-secret" aria-hidden="true"></i> 模拟
                    </button>
                </div>
            </td>
        </tr>
    `;
}

function renderAdminUserRoleCell(user: AdminUserRecord, isCurrentUser: boolean): string {
    const actorRole = window.currentUser?.role || "USER";
    const actorRank = USER_ROLE_RANK[actorRole] || 0;
    const targetRank = USER_ROLE_RANK[String(user.role).toUpperCase()] || 0;
    // 不允许调整自己的用户组（避免误操作失去权限），也不能操作权限高于自己的账号。
    if (isCurrentUser || targetRank > actorRank) {
        return settingsEscapeHtml(user.role);
    }
    // 可选角色仅限不高于自身权限的等级，普通管理员无法借此晋升为 OWNER。
    const options = USER_ROLE_OPTIONS
        .filter((role) => (USER_ROLE_RANK[role] || 0) <= actorRank)
        .map((role) => `<option value="${role}" ${role === user.role ? "selected" : ""}>${role}</option>`)
        .join("");
    return `<select class="form-select form-select-sm user-role-select" data-role-user-id="${user.id}" data-current-role="${settingsEscapeHtml(user.role)}" aria-label="用户组">${options}</select>`;
}

async function updateAdminUserRole(userId: number, role: string, select: HTMLSelectElement): Promise<void> {
    if (!Number.isFinite(userId) || !role) return;
    const message = document.getElementById("userManagementMessage");
    const previousRole = select.dataset.currentRole || "";
    select.disabled = true;
    try {
        const response = await TrpgApi.put<ApiResponse<unknown>>(`/api/users/${userId}/role`, { role });
        if (!response.success) {
            throw new Error(response.message || response.error || "用户组更新失败");
        }
        select.dataset.currentRole = role;
        if (message) {
            message.textContent = response.message || `已将用户组更新为 ${role}`;
            message.className = "settings-message success";
        }
    } catch (error) {
        // 失败时回滚下拉框，避免界面与后端状态不一致。
        select.value = previousRole;
        if (message) {
            message.textContent = settingsErrorMessage(error);
            message.className = "settings-message error";
        }
    } finally {
        select.disabled = false;
    }
}

async function startImpersonation(userId: number): Promise<void> {
    if (!Number.isFinite(userId)) return;
    try {
        const response = await TrpgApi.post<ApiResponse<unknown>>("/api/auth/impersonation/start", { user_id: userId });
        if (!response.success) {
            throw new Error(response.message || response.error || "模拟登录失败");
        }
        window.location.reload();
    } catch (error) {
        const message = settingsErrorMessage(error);
        const statusMessage = document.getElementById("userManagementMessage");
        if (statusMessage) {
            statusMessage.textContent = message;
            statusMessage.className = "settings-message error";
        }
    }
}

function formatOnlineState(isOnline?: boolean): string {
    return isOnline ? "在线" : "离线";
}

function formatTimestamp(value?: string): string {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function renderPermissionMatrix(config: PermissionConfig): void {
    const matrix = document.getElementById("permissionMatrix");
    if (!matrix) return;
    matrix.dataset.loaded = "true";
    matrix.dataset.roles = JSON.stringify(config.roles);
    matrix.innerHTML = config.groups.map((group) => renderPermissionGroup(group, config)).join("");
}

function renderPermissionGroup(group: PermissionGroup, config: PermissionConfig): string {
    const nodes = group.nodes.map((node) => renderPermissionNode(node, config)).join("");
    // 后端返回的 groups/nodes 只有英文占位文案（label=id、description="Permissions for ..."），
    // 这里按 id 用 i18n 覆盖，缺失时回退到后端文案。
    const label = settingsTranslate(`permission.group.${group.id}.label`, group.label);
    const description = settingsTranslate(`permission.group.${group.id}.description`, group.description || "");
    return `
        <section class="permission-group" data-permission-group="${settingsEscapeHtml(group.id)}">
            <div class="permission-group-header">
                <h5>${settingsEscapeHtml(label)}</h5>
                <p>${settingsEscapeHtml(description)}</p>
            </div>
            <div class="permission-node-list">${nodes}</div>
        </section>
    `;
}

function renderPermissionNode(node: PermissionNode, config: PermissionConfig): string {
    const allowedRoles = new Set(config.matrix[node.id] || []);
    const roleToggles = config.roles.map((role) => `
        <label class="permission-role-toggle">
            <input type="checkbox" data-permission-node="${settingsEscapeHtml(node.id)}" data-permission-role="${settingsEscapeHtml(role)}" ${allowedRoles.has(role) ? "checked" : ""}>
            <span>${settingsEscapeHtml(settingsTranslate(`permission.role.${role}.label`, role))}</span>
        </label>
    `).join("");
    const label = settingsTranslate(`permission.node.${node.id}.label`, node.label);
    const description = settingsTranslate(`permission.node.${node.id}.description`, node.description || node.id);
    return `
        <article class="permission-node-card">
            <div>
                <strong>${settingsEscapeHtml(label)}</strong>
                <p>${settingsEscapeHtml(description)}</p>
            </div>
            <div class="permission-role-list">${roleToggles}</div>
        </article>
    `;
}

/** 权限面板文案的 i18n 查询：命中翻译则用之，否则回退到后端提供的原始文案。 */
function settingsTranslate(key: string, fallback: string): string {
    return window.TrpgI18n?.t(key, fallback) || fallback;
}

async function savePermissionConfig(): Promise<void> {
    const matrix = document.getElementById("permissionMatrix");
    const message = document.getElementById("permissionConfigMessage");
    if (!matrix) return;

    const nextMatrix: Record<string, string[]> = {};
    matrix.querySelectorAll<HTMLInputElement>("[data-permission-node][data-permission-role]").forEach((input) => {
        const node = input.dataset.permissionNode || "";
        const role = input.dataset.permissionRole || "";
        if (!node || !role || !input.checked) return;
        nextMatrix[node] = nextMatrix[node] || [];
        nextMatrix[node].push(role);
    });

    try {
        const response = await TrpgApi.post<ApiResponse<PermissionConfig>>("/api/config/permissions", { matrix: nextMatrix });
        if (!response.success || !response.data) {
            throw new Error(response.message || response.error || "权限配置保存失败");
        }
        renderPermissionMatrix(response.data);
        if (message) {
            message.textContent = "权限配置已保存";
            message.className = "settings-message success";
        }
    } catch (error) {
        if (message) {
            message.textContent = settingsErrorMessage(error);
            message.className = "settings-message error";
        }
    }
}

function settingsEscapeHtml(value: unknown): string {
    const element = document.createElement("div");
    element.textContent = String(value ?? "");
    return element.innerHTML;
}

function settingsErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

window.switchMainTab = switchMainTab;
window.refreshAdminNavigation = refreshAdminNavigation;
