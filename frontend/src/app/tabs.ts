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
        // 只收集主内容区的一级页签：弹窗内的 Bootstrap .tab-content（如剧本编辑器的六个页签）
        // 不能参与主导航切换，否则会被清掉 active 或被误判为页面容器。
        const tabContents = Array.from(document.querySelectorAll<HTMLElement>("#mainContent > .tab-content"));

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
    const tabContents = Array.from(document.querySelectorAll<HTMLElement>("#mainContent > .tab-content"));
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
    if (tabId === "personal-home") {
        void AuthModule.renderProfileFavorites();
        void window.TrpgBgm?.renderPersonalSettings();
    }
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
    // 兼容旧链接/书签：分类合并后保留旧入口。
    const aliases: Record<string, string> = {
        bgm: "immersive",
        theme: "general",
        character: "play",
        network: "system",
        permissions: "access",
        users: "access",
    };
    const normalized = aliases[tabName] || tabName;
    const settingsTabs = document.querySelectorAll<HTMLElement>(".settings-tab");
    const settingsContents = document.querySelectorAll<HTMLElement>(".settings-content");

    settingsTabs.forEach((tab) => {
        const active = tab.dataset.settings === normalized;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
    });
    settingsContents.forEach((content) => content.classList.remove("active"));

    const targetTab = document.querySelector<HTMLElement>(`.settings-tab[data-settings="${CSS.escape(normalized)}"]`);
    const targetContent = document.getElementById(`${normalized}-settings-content`);
    targetContent?.classList.add("active");
    if (normalized === "access") {
        void loadPermissionConfig();
        void loadUserManagement();
    }
    if (normalized === "knowledge") {
        void loadRulesetKnowledge();
        void loadScenarioKnowledge();
        void loadEmbeddingHealth();
    }
    if (normalized === "model") {
        // 生图平台表已并入「模型设置」，AI 平台/模型/密钥统一在这里维护。
        window.TrpgImmersive?.renderModelSettings?.();
    }
    if (normalized === "immersive") {
        window.TrpgImmersive?.renderSettings?.();
        // 音乐库已并入「实时沉浸」标签卡，切换到这里时按需加载列表。
        void window.TrpgBgm?.loadLibraryPanel();
    }
    if (normalized === "about") {
        window.TrpgAbout?.init?.();
    }
    // 骰子贴图库按需初始化，避免非管理员页面加载时发出多余请求。
    window.TrpgAdminDiceTextures?.init();
    if (targetTab) {
        targetTab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
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
        tab.tabIndex = tab.classList.contains("active") ? 0 : -1;
        tab.addEventListener("click", () => switchSettingsTab(tab.dataset.settings || ""));
    });
    initSettingsRailKeyboard();
    initSettingsBackToTop();

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
    bindGeneralTextSetting("enabledThemesInput", "appearance", "enabled_themes");
    bindGeneralTextSetting("themeOverridesInput", "appearance", "theme_overrides");
    (window as any).TrpgThemeSettings?.initAdmin?.();
    bindGeneralSelectSetting("languageSelect", "language", "language");

    bindGeneralCheckboxSetting("enableSound", "notification", "enable_sound");
    bindGeneralCheckboxSetting("enableNotification", "notification", "enable_desktop_notification");
    bindGeneralCheckboxSetting("enableAutosave", "autosave", "enabled");
    bindGeneralCheckboxSetting("showTimestamp", "chat", "show_timestamp");
    bindGeneralNumberSetting("messageFontSize", "chat", "message_font_size", 10, 24);
    bindGeneralCheckboxSetting("enableAIResponseLock", "ai", "enable_ai_response_lock");
    bindGeneralNumberSetting("messageRateLimit", "chat", "message_rate_limit", 0, 60);
    bindGeneralCheckboxSetting("latexEnabled", "chat", "latex_enabled");
    bindGeneralCheckboxSetting("latexKpEnabled", "chat", "latex_kp_enabled");
    bindGeneralCheckboxSetting("latexPlayerEnabled", "chat", "latex_player_enabled");
    bindGeneralSelectSetting("latexIntensity", "chat", "latex_intensity");
    bindGeneralNumberSetting("latexMaxFormulasPerMessage", "chat", "latex_max_formulas_per_message", 1, 50);
    bindGeneralNumberSetting("latexMaxFormulaChars", "chat", "latex_max_formula_chars", 50, 2000);
    bindGeneralNumberSetting("latexMaxCleanMessageChars", "chat", "latex_max_clean_message_chars", 200, 10000);

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
    bindGeneralCheckboxSetting("memoryEnabled", "ai.memory", "enabled");
    bindGeneralNumberSetting("memoryInjectBudgetChars", "ai.memory", "inject_budget_chars", 0, 4000);
    bindGeneralNumberSetting("memoryInjectMaxItems", "ai.memory", "inject_max_items", 1, 20);
    bindGeneralCheckboxSetting("cloudEmbeddingEnabled", "ai", "cloud_embedding_enabled");
    bindGeneralCheckboxSetting("enableRoomConsole", "room_console", "enabled");
    bindGeneralCheckboxSetting("mirrorRoomConsole", "room_console", "mirror_to_server_console");
    bindGeneralNumberSetting("roomConsoleMaxEntries", "room_console", "max_entries", 50, 5000);
    bindGeneralNumberSetting("roomCleanupInactiveDays", "rooms", "cleanup_inactive_days", 0, 3650);
    bindGeneralNumberSetting("worldOverrideCooldownSeconds", "world_override", "cooldown_seconds", 0, 3600);
    bindGeneralNumberSetting("worldOverrideHourlyLimit", "world_override", "hourly_limit", 0, 10000);
    bindGeneralCheckboxSetting("siteAgentEnabled", "site_agent", "enabled");
    bindGeneralNumberSetting("siteAgentDailyTokenBudget", "site_agent", "daily_token_budget", 0, 100000000);
    bindGeneralNumberSetting("siteAgentMaxTurnsPerHour", "site_agent", "max_turns_per_hour", 1, 1000);
    bindGeneralNumberSetting("siteAgentMaxToolRounds", "site_agent", "max_tool_rounds", 1, 20);
    bindGeneralNumberSetting("siteAgentRequestTimeout", "site_agent", "request_timeout", 30, 1800);
    bindGeneralNumberSetting("siteAgentMaxHistoryMessages", "site_agent", "max_history_messages", 4, 200);
    bindGeneralNumberSetting("siteAgentMaxToolResultChars", "site_agent", "max_tool_result_chars", 200, 20000);
    bindGeneralNumberSetting("siteAgentActionTtlSeconds", "site_agent", "action_ttl_seconds", 30, 3600);
    bindGeneralCheckboxSetting("updateCheckOnStartup", "update", "check_update_on_startup");
    bindGeneralCheckboxSetting("updateAutoDownload", "update", "auto_update");
    // 实时沉浸：配乐/配图的站点级开关、默认档位与预算限额（管理员的密钥与连通性测试走独立接口）。
    bindGeneralCheckboxSetting("immersiveEnabled", "immersive", "enabled");
    bindGeneralCheckboxSetting("immersiveMusicAutoSwitch", "immersive", "music_auto_switch");
    bindGeneralCheckboxSetting("immersiveMusicDuckBgm", "immersive", "music_duck_bgm");
    bindGeneralSelectSetting("immersiveMusicDefaultProfile", "immersive", "music_default_profile");
    bindGeneralSelectSetting("immersiveMusicDefaultIntensity", "immersive", "music_default_intensity");
    bindGeneralSelectSetting("immersiveMusicSource", "immersive", "music_source");
    bindGeneralTextSetting("immersiveMusicSourceAllowed", "immersive", "music_source_allowed");
    bindGeneralCheckboxSetting("immersiveMusicSourceAllowPersonal", "immersive", "music_source_allow_personal");
    bindGeneralTextSetting("immersiveAudioBlocksAllowed", "immersive", "audio_blocks_allowed");
    bindGeneralTextSetting("immersiveExternalProviders", "immersive", "music_external_providers");
    bindGeneralTextSetting("immersiveExternalLicenseAllow", "immersive", "music_external_license_allow");
    bindGeneralNumberSetting("immersiveExternalTimeout", "immersive", "music_external_timeout", 2, 30);
    bindGeneralNumberSetting("immersiveExternalFade", "immersive", "music_external_fade_ms", 1500, 12000);
    bindGeneralNumberSetting("immersiveExternalPhraseBars", "immersive", "music_external_phrase_bars", 1, 16);
    bindGeneralNumberSetting("immersiveExternalSearchTtl", "immersive", "music_external_search_ttl", 0, 604800);
    bindGeneralTextSetting("immersiveExternalJamendoId", "immersive", "music_external_jamendo_client_id");
    bindGeneralNumberSetting("immersiveExternalDailyBudget", "immersive", "music_external_daily_budget", 0, 100000);
    bindGeneralNumberSetting("immersiveExternalHourlyCap", "immersive", "music_external_room_hourly_cap", 0, 10000);
    bindGeneralCheckboxSetting("immersiveExternalInstrumental", "immersive", "music_external_instrumental_only");
    bindGeneralCheckboxSetting("immersiveExternalCacheAudio", "immersive", "music_external_cache_audio");
    bindGeneralNumberSetting("immersiveMusicVolume", "immersive", "music_volume", 0, 100);
    bindGeneralNumberSetting("immersiveMusicCrossfade", "immersive", "music_crossfade_ms", 500, 10000);
    bindGeneralNumberSetting("immersiveMusicHysteresis", "immersive", "music_hysteresis_turns", 0, 10);
    bindGeneralTextSetting("immersiveMusicSeed", "immersive", "music_seed");
    bindGeneralCheckboxSetting("immersiveDirectorEnabled", "immersive", "music_director_enabled");
    bindGeneralNumberSetting("immersiveDirectorInterval", "immersive", "music_director_interval_turns", 1, 20);
    bindGeneralNumberSetting("immersiveDirectorBudget", "immersive", "music_director_daily_budget", 0, 100000);
    bindGeneralNumberSetting("immersiveDirectorTimeout", "immersive", "music_director_timeout", 20, 300);
    bindGeneralNumberSetting("immersiveDirectorCacheTtl", "immersive", "music_director_cache_ttl", 0, 86400);
    bindGeneralNumberSetting("immersiveDirectorMinConfidence", "immersive", "music_director_min_confidence", 0, 100);
    bindGeneralSelectSetting("immersiveDirectorFallback", "immersive", "music_director_fallback");
    bindGeneralNumberSetting("immersiveDirectorMaxContext", "immersive", "music_director_max_context_chars", 200, 4000);
    bindGeneralCheckboxSetting("immersiveIllustrationEnabled", "immersive", "illustration_enabled");
    bindGeneralCheckboxSetting("immersiveIllustrationAuto", "immersive", "illustration_auto");
    bindGeneralSelectSetting("immersiveIllustrationProvider", "immersive", "illustration_provider");
    bindGeneralNumberSetting("immersiveIllustrationTimeout", "immersive", "illustration_timeout", 20, 600);
    bindGeneralNumberSetting("immersiveIllustrationConcurrency", "immersive", "illustration_concurrency", 1, 4);
    bindGeneralNumberSetting("immersiveIllustrationDailyBudget", "immersive", "illustration_daily_budget", 0, 100000);
    bindGeneralNumberSetting("immersiveIllustrationRoomHourlyCap", "immersive", "illustration_room_hourly_cap", 0, 500);
    bindGeneralNumberSetting("immersiveIllustrationMinTurns", "immersive", "illustration_min_turns", 1, 50);
    bindGeneralSelectSetting("immersiveIllustrationTrigger", "immersive", "illustration_trigger");
    bindGeneralTextSetting("immersiveIllustrationTriggerAllowed", "immersive", "illustration_trigger_allowed");
    bindGeneralCheckboxSetting("immersiveScenarioRecord", "immersive", "illustration_scenario_record");
    bindGeneralCheckboxSetting("immersiveScenarioPrefer", "immersive", "illustration_scenario_prefer");
    bindGeneralNumberSetting("immersiveScenarioQuotaMb", "immersive", "illustration_scenario_quota_mb", 0, 102400);
    bindGeneralNumberSetting("immersiveScenarioMaxImageMb", "immersive", "illustration_scenario_max_image_mb", 1, 64);
    bindGeneralSelectSetting("immersiveScenarioOverflow", "immersive", "illustration_scenario_overflow");
    // AI 对话语音配音（P1）：站点默认关闭，system 引擎默认可用；local/cloud 仅占位。
    bindGeneralCheckboxSetting("voiceEnabled", "immersive", "voice_enabled");
    bindGeneralSelectSetting("voiceEngine", "immersive", "voice_engine");
    bindGeneralTextSetting("voiceEngineAllowed", "immersive", "voice_engine_allowed");
    bindGeneralCheckboxSetting("voiceAutoplay", "immersive", "voice_autoplay");
    bindGeneralNumberSetting("voiceVolume", "immersive", "voice_volume", 0, 100);
    bindGeneralNumberSetting("voiceRate", "immersive", "voice_rate", 50, 200);
    bindGeneralNumberSetting("voiceMaxTurnChars", "immersive", "voice_max_turn_chars", 40, 4000);
    bindGeneralNumberSetting("voiceMaxSegmentChars", "immersive", "voice_max_segment_chars", 10, 500);
    bindGeneralNumberSetting("voiceMaxSegments", "immersive", "voice_max_segments", 1, 200);
    bindGeneralSelectSetting("voiceInterruptPolicy", "immersive", "voice_interrupt_policy");
    bindGeneralCheckboxSetting("voiceDucking", "immersive", "voice_ducking");
    bindGeneralNumberSetting("voiceDuckingLevel", "immersive", "voice_ducking_level", 0, 90);
    bindGeneralNumberSetting("voiceRoomHourlyCap", "immersive", "voice_room_hourly_cap", 0, 10000);
    bindGeneralCheckboxSetting("voiceAllowPersonal", "immersive", "voice_allow_personal");
    bindGeneralTextSetting("voiceCloudBaseUrl", "immersive", "voice_cloud_base_url");
    bindGeneralTextSetting("voiceCloudModel", "immersive", "voice_cloud_model");
    bindGeneralCheckboxSetting("voiceLocalEnabled", "immersive", "voice_local_enabled");
    bindGeneralTextSetting("voiceLocalBaseUrl", "immersive", "voice_local_base_url");
    bindGeneralTextSetting("voiceLocalModel", "immersive", "voice_local_model");
    bindGeneralNumberSetting("voiceLocalConcurrency", "immersive", "voice_local_concurrency", 1, 8);
    bindGeneralNumberSetting("voiceLocalTimeout", "immersive", "voice_local_timeout", 5, 180);
    bindGeneralTextSetting("voiceLocalSpeakers", "immersive", "voice_local_speakers");
    bindGeneralCheckboxSetting("voiceCacheEnabled", "immersive", "voice_cache_enabled");
    bindGeneralNumberSetting("voiceCacheQuotaMb", "immersive", "voice_cache_quota_mb", 0, 1024);
    bindGeneralNumberSetting("voiceCacheMaxEntries", "immersive", "voice_cache_max_entries", 0, 10000);
    bindGeneralSelectSetting("voiceCacheOverflow", "immersive", "voice_cache_overflow");
    initVoiceLocalPanel();
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
    // 设置说明按需展开 + 设置搜索索引。
    window.TrpgSettingsAssistant?.init();
    // 管理员骰子贴图库：默认停在常规设置页时也要渲染上传入口与皮肤编辑。
    window.TrpgAdminDiceTextures?.init();
}

/** 分类轨方向键导航：↑↓/Home/End 在标签间移动焦点并直接切换。 */
function initSettingsRailKeyboard(): void {
    const rail = document.getElementById("settingsRail");
    if (!rail) return;
    rail.addEventListener("keydown", (event) => {
        const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
        if (!keys.includes(event.key)) return;
        const tabs = Array.from(rail.querySelectorAll<HTMLElement>(".settings-tab")).filter((tab) => !tab.hidden);
        if (tabs.length === 0) return;
        const current = tabs.indexOf(document.activeElement as HTMLElement);
        let next = current < 0 ? 0 : current;
        if (event.key === "ArrowDown") next = (current + 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowUp") next = (current - 1 + tabs.length) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        event.preventDefault();
        const target = tabs[next];
        if (!target) return;
        target.focus();
        switchSettingsTab(target.dataset.settings || "");
    });
}

/** 内容区滚动超过一屏时显示「回到顶部」。 */
function initSettingsBackToTop(): void {
    const button = document.getElementById("settingsBackToTop");
    const contents = Array.from(document.querySelectorAll<HTMLElement>(".settings-content"));
    if (!button || contents.length === 0) return;
    const outerScroller = document.querySelector<HTMLElement>("#settings .p-4");
    const activeContent = (): HTMLElement | undefined => contents.find((content) => content.classList.contains("active"));
    // 宽屏滚动在 .settings-content，窄屏由外层 .p-4 统一滚动。
    const scrollerOf = (content: HTMLElement): HTMLElement => {
        if (outerScroller && outerScroller.scrollHeight > outerScroller.clientHeight + 1) {
            return outerScroller;
        }
        return content;
    };
    const sync = (): void => {
        const content = activeContent();
        if (!content) {
            button.hidden = true;
            return;
        }
        button.hidden = scrollerOf(content).scrollTop < 600;
    };
    contents.forEach((content) => content.addEventListener("scroll", sync, { passive: true }));
    outerScroller?.addEventListener("scroll", sync, { passive: true });
    button.addEventListener("click", () => {
        const content = activeContent();
        if (!content) return;
        scrollerOf(content).scrollTo({ top: 0, behavior: "smooth" });
        content.scrollTo({ top: 0, behavior: "smooth" });
    });
    sync();
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
        list.innerHTML = response.data.length ? response.data.map((item) => {
            const archived = item.archived === true;
            const archivedNote = archived
                ? '<div class="small text-warning mt-1"><i class="fa fa-archive"></i> 剧本已归档（已从剧本列表隐藏），知识库暂时保留以支持仍在进行的房间。可点「彻底删除剧本」连同剧本记录一起清理。</div>'
                : "";
            const purgeButton = archived
                ? `<button class="btn btn-sm btn-outline-danger ms-2" data-purge-scenario-kb="${String(item.scenario_id)}">彻底删除剧本</button>`
                : "";
            return `<div class="list-group-item d-flex justify-content-between align-items-center"><div><strong>${settingsEscapeHtml(item.title)}</strong>${archived ? '<span class="badge bg-secondary ms-2">已归档</span>' : ""}<div class="small text-muted">向量 ${Number(item.vector_count || 0)} · ${settingsEscapeHtml(item.backend)}<br>${settingsEscapeHtml(item.path)}</div>${archivedNote}</div><div class="d-flex align-items-center"><button class="btn btn-sm btn-outline-danger" data-delete-scenario-kb="${String(item.scenario_id)}">删除知识库</button>${purgeButton}</div></div>`;
        }).join("") : '<div class="list-group-item text-muted">没有匹配的剧本知识库</div>';
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
        list.querySelectorAll<HTMLButtonElement>("[data-purge-scenario-kb]").forEach((button) => button.addEventListener("click", async () => {
            const confirmed = await window.TrpgDialog?.confirm({
                title: "彻底删除归档剧本",
                message: "将删除该剧本记录、知识库向量与索引、封面，且无法恢复。若仍有房间引用它，这些房间会失去剧本内容。确定继续吗？",
                confirmText: "彻底删除",
                danger: true,
            });
            if (!confirmed) return;
            const result = await TrpgApi.del<ApiResponse<unknown>>(`/api/knowledge-bases/scenarios/${encodeURIComponent(button.dataset.purgeScenarioKb || "")}?purge=1`);
            if (!result.success) throw new Error(result.message || "彻底删除失败");
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
type GeneralSettingKind = "checkbox" | "number" | "select" | "text";

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

/** 纯文本设置（种子、模型名、尺寸等）：不做数值裁剪，按原样保存。 */
function bindGeneralTextSetting(elementId: string, sectionName: string, key: string): void {
    const input = document.getElementById(elementId) as HTMLInputElement | null;
    if (!input) return;
    registerGeneralSetting({ element: input, kind: "text", section: sectionName, key, savedValue: input.value });
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
        const group = control.element.closest<HTMLElement>(".form-group")
            || control.element.closest<HTMLElement>(".form-check");
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
    // 音频块选择器由隐藏输入驱动；配置回填后需要同步重绘。
    (window as unknown as { refreshImmersiveAudioBlocksPicker?: () => void }).refreshImmersiveAudioBlocksPicker?.();
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
    document.querySelectorAll<HTMLElement>(".settings-tab-dirty").forEach((dot) => {
        dot.hidden = true;
    });
    generalSettingControls.forEach((control) => {
        const isDirty = readGeneralSettingValue(control) !== control.savedValue;
        if (isDirty) dirtyCount += 1;
        const group = control.element.closest<HTMLElement>(".form-group")
            || control.element.closest<HTMLElement>(".form-check");
        group?.classList.toggle("setting-dirty", isDirty);
        if (isDirty) {
            const tabName = control.element.closest<HTMLElement>(".settings-content")?.id.replace(/-settings-content$/, "");
            const dot = tabName
                ? document.querySelector<HTMLElement>(`.settings-tab[data-settings="${CSS.escape(tabName)}"] .settings-tab-dirty`)
                : null;
            if (dot) dot.hidden = false;
        }
    });
    if (!pendingBar || !count) return;
    pendingBar.hidden = dirtyCount === 0;
    count.textContent = String(dirtyCount);
}

async function saveGeneralSettingsChanges(): Promise<void> {
    let generalConfig = configManager.getConfig("general");
    // 配置未成功加载时不能保存：否则会用界面上的默认值覆盖磁盘里的真实配置。
    if (!isConfigObject(generalConfig) || Object.keys(generalConfig).length === 0) {
        await configManager.loadConfig("general");
        generalConfig = configManager.getConfig("general");
    }
    if (!isConfigObject(generalConfig) || Object.keys(generalConfig).length === 0) {
        showNotification("通用设置尚未加载完成，请刷新页面后重试", "error");
        return;
    }
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
    // 「启用程序化实时配乐」复选框已并入「允许的音频块」：清单含任一配乐块即视为启用站点配乐，
    // 只剩环境音/静音时视为关闭。空清单保持后端按旧配置推导，这里不改写。
    const immersiveSection = generalConfig["immersive"];
    if (isConfigObject(immersiveSection)) {
        const allowed = String(immersiveSection["audio_blocks_allowed"] || "")
            .split(",")
            .map((item) => item.trim())
            .filter((item) => item);
        if (allowed.length > 0) {
            immersiveSection["music_enabled"] = allowed.some(
                (block) => block === "procedural" || block === "music_model" || block === "external",
            );
        }
    }
    if (!(await configManager.saveConfig("general", generalConfig))) return;
    generalSettingControls.forEach((control) => {
        control.savedValue = readGeneralSettingValue(control);
    });
    configManager.applyTheme();
    syncGeneralSettingsLocale();
    syncThemeAdminPanel();
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
    syncThemeAdminPanel();
    updateGeneralPendingState();
}

function syncThemeAdminPanel(): void {
    (window as any).TrpgThemeSettings?.syncAdminFromInputs?.();
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

// 本地多音色引擎面板：状态、安装、移除与首次启用未安装提示。
let voiceLocalPanelInitialized = false;
let voiceLocalInstallTimer: number | null = null;
let voiceLocalInstallRunning = false;

function initVoiceLocalPanel(): void {
    if (voiceLocalPanelInitialized) return;
    const panel = document.getElementById("voiceLocalPanel");
    if (!panel) return;
    voiceLocalPanelInitialized = true;
    document.getElementById("voiceLocalStatusButton")?.addEventListener("click", () => {
        void refreshVoiceLocalStatus(true);
    });
    document.getElementById("voiceLocalInstallButton")?.addEventListener("click", () => {
        void installVoiceLocalModel();
    });
    document.getElementById("voiceLocalRemoveButton")?.addEventListener("click", () => {
        void removeVoiceLocalModel();
    });
    // 首次勾选启用时主动刷新状态：未安装会立即看到「请安装模型」的提示。
    document.getElementById("voiceLocalEnabled")?.addEventListener("change", () => {
        void refreshVoiceLocalStatus(false);
    });
    void refreshVoiceLocalStatus(false);
}

function setVoiceLocalStatus(text: string, tone: "muted" | "success" | "warning" | "error" = "muted"): void {
    const element = document.getElementById("voiceLocalStatus");
    if (!element) return;
    element.textContent = text;
    element.style.whiteSpace = "pre-line";
    element.classList.remove("text-muted", "text-success", "text-warning", "text-danger");
    element.classList.add(
        tone === "success" ? "text-success" : tone === "warning" ? "text-warning" : tone === "error" ? "text-danger" : "text-muted",
    );
}

function formatVoiceLocalBytes(bytes: unknown): string {
    const value = Number(bytes) || 0;
    if (value >= 1024 * 1024 * 1024) return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
    if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
    if (value >= 1024) return `${(value / 1024).toFixed(0)} KB`;
    return `${value} B`;
}

function describeVoiceLocalStatus(data: Record<string, any>): { text: string; tone: "muted" | "success" | "warning" | "error" } {
    const model = (data.model || {}) as Record<string, any>;
    const health = (data.health || {}) as Record<string, any>;
    const install = (data.install || model.install || {}) as Record<string, any>;
    const lines: string[] = [];
    if (install.running) {
        lines.push(`模型安装进行中：${String(install.message || "下载中")}${install.started_at ? `（开始于 ${install.started_at}）` : ""}`);
    }
    lines.push(
        model.installed
            ? `模型：${String(model.label || model.model || "Kokoro")} 已安装（${formatVoiceLocalBytes(model.bytes)}）`
            : `模型：${String(model.label || model.model || "Kokoro")} 未安装。启用本地引擎会因缺少权重降级为浏览器音色，请点击「安装模型」下载（约 170 MB）。`,
    );
    if (data.enabled) {
        lines.push(
            health.ok
                ? `本地子服务正常：${String(data.base_url || "自动托管")}，并发 ${Number(data.concurrency || 1)}，当前合成 ${Number(data.active || 0)} 条。`
                : `本地子服务不可用（${String(health.reason || "unknown")}），将降级为浏览器音色。`,
        );
    } else {
        lines.push("站点当前未启用本地引擎；勾选「启用本地引擎」并保存后才会启动子服务。");
    }
    const bindings = (data.bindings || {}) as Record<string, Record<string, any>>;
    const profileIds = Object.keys(bindings);
    if (profileIds.length) {
        lines.push(
            "人设说话人：" +
                profileIds
                    .map((id) => `${id} → ${String(bindings[id]?.voice_id || "?")}（${String(bindings[id]?.gender || "?")}）`)
                    .join("，"),
        );
    }
    if (install.error && !install.running) lines.push(`最近一次安装失败：${String(install.error)}`);
    const tone: "muted" | "success" | "warning" | "error" = install.running
        ? "warning"
        : !model.installed
          ? "warning"
          : data.enabled && health.ok
            ? "success"
            : "muted";
    return { text: lines.join("\n"), tone };
}

async function refreshVoiceLocalStatus(announce = false): Promise<void> {
    if (announce) setVoiceLocalStatus("正在读取本地引擎状态…");
    try {
        const response = await TrpgApi.get<ApiResponse<Record<string, any>>>("/api/voice/local/status");
        if (!response.success || !response.data) {
            throw new Error(response.message || response.error || "读取本地引擎状态失败");
        }
        const data = response.data as Record<string, any>;
        const model = (data.model || {}) as Record<string, any>;
        voiceLocalInstallRunning = Boolean((data.install || model.install || {}).running);
        const described = describeVoiceLocalStatus(data);
        setVoiceLocalStatus(described.text, described.tone);
        if (voiceLocalInstallRunning) {
            scheduleVoiceLocalInstallPoll();
        } else {
            clearVoiceLocalInstallPoll();
            // 安装刚结束时刷新一次，让「已安装」状态和按钮语义立即更新。
            if (announce) void refreshVoiceLocalStatus(false);
        }
    } catch (error) {
        setVoiceLocalStatus(`读取本地引擎状态失败：${settingsErrorMessage(error)}`, "error");
    }
}

function scheduleVoiceLocalInstallPoll(): void {
    if (voiceLocalInstallTimer !== null) return;
    voiceLocalInstallTimer = window.setTimeout(() => {
        voiceLocalInstallTimer = null;
        void refreshVoiceLocalStatus(false).then(() => {
            if (voiceLocalInstallRunning) scheduleVoiceLocalInstallPoll();
        });
    }, 3000);
}

function clearVoiceLocalInstallPoll(): void {
    if (voiceLocalInstallTimer === null) return;
    window.clearTimeout(voiceLocalInstallTimer);
    voiceLocalInstallTimer = null;
}

async function installVoiceLocalModel(): Promise<void> {
    const confirmed = await window.TrpgDialog?.confirm({
        title: "安装本地语音模型",
        message: "将从 Hugging Face 镜像下载约 170 MB 的 Kokoro 中文多音色权重到运行时数据目录（不会进入仓库）。下载失败不会破坏已有安装。继续吗？",
        confirmText: "开始安装",
    });
    if (confirmed === false) return;
    setVoiceLocalStatus("已提交安装请求，正在启动下载…", "warning");
    try {
        const response = await TrpgApi.post<ApiResponse<Record<string, any>>>("/api/voice/local/install", {});
        if (!response.success) throw new Error(response.message || response.error || "安装请求失败");
        setVoiceLocalStatus(response.message || "已开始后台安装。", "warning");
        voiceLocalInstallRunning = true;
        scheduleVoiceLocalInstallPoll();
    } catch (error) {
        setVoiceLocalStatus(`安装请求失败：${settingsErrorMessage(error)}`, "error");
    }
}

async function removeVoiceLocalModel(): Promise<void> {
    const confirmed = await window.TrpgDialog?.confirm({
        title: "移除本地语音模型",
        message: "将删除运行时数据目录中的模型权重（已生成的语音缓存不受影响）。之后本地引擎会降级为浏览器音色。确认移除吗？",
        confirmText: "移除",
        danger: true,
    });
    if (confirmed === false) return;
    setVoiceLocalStatus("正在移除模型…", "warning");
    try {
        const response = await TrpgApi.post<ApiResponse<Record<string, any>>>("/api/voice/local/remove", {});
        if (!response.success) throw new Error(response.message || response.error || "移除请求失败");
        clearVoiceLocalInstallPoll();
        await refreshVoiceLocalStatus(false);
    } catch (error) {
        setVoiceLocalStatus(`移除模型失败：${settingsErrorMessage(error)}`, "error");
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
