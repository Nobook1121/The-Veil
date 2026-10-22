class ConfigManager {
    private readonly configs: Record<string, TomlConfig> = {};
    // 部署层（环境变量/命令行）锁定的键："section.key" -> 来源（env/cli）。
    private readonly overrides: Record<string, Record<string, string>> = {};
    private themeInitialized = false;
    private themeTransitionTimer: number | null = null;

    async loadConfig(configName: string): Promise<TomlConfig | null> {
        try {
            const { response, data } = await TrpgApi.requestWithResponse<
                ApiResponse<{ config: TomlConfig; overrides?: Record<string, string> }>
            >(`/api/config/${configName}`);
            if (!response.ok || !data.success || !data.data) {
                throw new Error(`无法加载配置: ${configName}`);
            }
            this.configs[configName] = data.data.config;
            this.overrides[configName] = data.data.overrides || {};
            console.log(`配置 ${configName} 加载成功`);
            return data.data.config;
        } catch (error) {
            console.error(`加载配置失败: ${configErrorMessage(error)}`);
            return null;
        }
    }

    /** 返回键的部署锁定来源（"env"/"cli"），未锁定返回 null。 */
    getOverrideSource(configName: string, section: string, key: string): string | null {
        return this.overrides[configName]?.[`${section}.${key}`] || null;
    }

    get<T = unknown>(configName: string, section: string | null, key: string, defaultValue?: T): T {
        const config = this.configs[configName];
        if (!config) return defaultValue as T;

        const source = section ? config[section] : config;
        if (!isTomlConfig(source)) return defaultValue as T;

        return source[key] !== undefined ? source[key] as T : defaultValue as T;
    }

    getSection(configName: string, section: string): TomlConfig | null {
        const value = this.configs[configName]?.[section];
        return isTomlConfig(value) ? value : null;
    }

    getConfig(configName: string): TomlConfig {
        return this.configs[configName] || {};
    }

    applyGeneralSettings(): void {
        const generalConfig = this.configs.general;
        if (!generalConfig) {
            console.warn("常规设置配置未加载");
            return;
        }

        configSetSelectValue("themeSelect", this.get("general", "appearance", "theme", "light"));
        this.applyTheme();
        const language = this.get("general", "language", "language", "zh-CN");
        configSetSelectValue("languageSelect", language);
        void window.TrpgI18n?.setLocale(language);
        configSetCheckboxValue("enableSound", this.get("general", "notification", "enable_sound", true));
        configSetCheckboxValue("enableNotification", this.get("general", "notification", "enable_desktop_notification", false));
        configSetCheckboxValue("enableAutosave", this.get("general", "autosave", "enabled", true));
        configSetInputValue("autosaveInterval", this.get("general", "autosave", "interval", 300));
        configSetInputValue("autosaveMaxNodes", this.get("general", "autosave", "max_nodes", 3));
        configSetInputValue("triggerMaxFileSize", this.get("general", "scenario", "trigger_max_file_size", 5242880));
        configSetCheckboxValue("showTimestamp", this.get("general", "chat", "show_timestamp", true));
        configSetInputValue("messageFontSize", this.get("general", "chat", "message_font_size", 14));
        configSetInputValue("messageRateLimit", this.get("general", "chat", "message_rate_limit", 10));
        configSetCheckboxValue("enableAIResponseLock", this.get("general", "ai", "enable_ai_response_lock", false));
        configSetCheckboxValue("streamOutput", this.get("general", "ai", "stream_output", false));
        configSetCheckboxValue("scenarioImportStreamOutput", this.get("general", "scenario_import", "stream_output", true));
        configSetCheckboxValue("debugMode", this.get("general", "ai", "debug_mode", false));
        configSetCheckboxValue("showAIHints", this.get("general", "ai", "show_ai_hints", true));
        // KP 请求限制（达到后自动收尾并提示，不静默无响应）
        configSetInputValue("maxToolRounds", this.get("general", "ai", "max_tool_rounds", 8));
        configSetInputValue("aiRequestTimeout", this.get("general", "ai", "ai_request_timeout", 300));
        configSetInputValue("aiRequestMaxRetries", this.get("general", "ai", "ai_request_max_retries", 5));
        // 最大输出 token 全局默认值（模板未自行声明时统一下发；关闭则不下发）
        configSetCheckboxValue("constrainMaxTokens", this.get("general", "ai", "constrain_max_tokens", true));
        configSetInputValue("maxTokens", this.get("general", "ai", "max_tokens", 16384));
        configSetCheckboxValue("dedupeToolCalls", this.get("general", "ai.prompt", "dedupe_tool_calls", true));
        // 本地响应缓存（命中即跳过上游请求，直接省 token）
        configSetCheckboxValue("semanticCacheEnabled", this.get("general", "ai.cache", "semantic_enabled", true));
        configSetInputValue("semanticCacheTtl", this.get("general", "ai.cache", "semantic_ttl_seconds", 120));
        configSetInputValue("exactCacheTtl", this.get("general", "ai.cache", "exact_ttl_seconds", 300));
        configSetCheckboxValue("recordPromptSegments", this.get("general", "ai", "record_prompt_segments", true));
        // 骰娘默认阈值（房间规则未单独设置时生效）
        configSetInputValue("diceCriticalThresholdDefault", this.get("general", "ai", "dice_critical_threshold", 1));
        configSetInputValue("diceFumbleThresholdDefault", this.get("general", "ai", "dice_fumble_threshold", 96));
        configSetSelectValue("diceAnimationLevel", this.get("general", "ai", "dice_animation_level", "3d"));
        configSetCheckboxValue("dicePhysicsResults", this.get("general", "ai", "dice_physics_results", true));
        configSetCheckboxValue("dicePlayerRollDefault", this.get("general", "ai", "dice_player_roll", true));
        configSetInputValue("dicePlayerRollTimeout", this.get("general", "ai", "dice_player_roll_timeout", 120));
        // 重投模式与次数（关闭时次数控件置灰，但仍保留已保存值）
        configSetSelectValue("diceRerollMode", this.get("general", "ai", "dice_reroll_mode", "off"));
        configSetInputValue("diceRerollLimit", this.get("general", "ai", "dice_reroll_limit", 3));
        // 知识库检索与切块
        configSetInputValue("retrievalTopKDefault", this.get("general", "ai.knowledge", "top_k_default", 5));
        configSetInputValue("retrievalTopKDirect", this.get("general", "ai.knowledge", "top_k_direct", 6));
        configSetInputValue("chunkChildMaxChars", this.get("general", "ai.knowledge", "chunk_child_max_chars", 800));
        configSetInputValue("chunkParentMaxChars", this.get("general", "ai.knowledge", "chunk_parent_max_chars", 2400));
        configSetInputValue("stickyRounds", this.get("general", "ai.knowledge", "sticky_rounds", 1));
        configSetInputValue("cooldownRounds", this.get("general", "ai.knowledge", "cooldown_rounds", 3));
        configSetCheckboxValue("keywordChannelEnabled", this.get("general", "ai.knowledge", "keyword_channel_enabled", true));
        configSetCheckboxValue("recursiveScanning", this.get("general", "ai.knowledge", "recursive_scanning", true));
        configSetInputValue("maxRecursionDepth", this.get("general", "ai.knowledge", "max_recursion_depth", 3));
        configSetInputValue("lorebookTokenBudget", this.get("general", "ai.knowledge", "lorebook_token_budget", 0));
        configSetCheckboxValue("cloudEmbeddingEnabled", this.get("general", "ai", "cloud_embedding_enabled", false));
        // 房间内嵌控制台：开关与保留条数必须回填，否则刷新后 UI 会退回 HTML 默认值。
        configSetCheckboxValue("enableRoomConsole", this.get("general", "room_console", "enabled", true));
        configSetCheckboxValue("mirrorRoomConsole", this.get("general", "room_console", "mirror_to_server_console", false));
        configSetInputValue("roomConsoleMaxEntries", this.get("general", "room_console", "max_entries", 500));
        // 网站 Agent 的启用开关与限额
        configSetCheckboxValue("siteAgentEnabled", this.get("general", "site_agent", "enabled", true));
        configSetInputValue("siteAgentDailyTokenBudget", this.get("general", "site_agent", "daily_token_budget", 200000));
        configSetInputValue("siteAgentMaxTurnsPerHour", this.get("general", "site_agent", "max_turns_per_hour", 60));
        configSetInputValue("siteAgentMaxToolRounds", this.get("general", "site_agent", "max_tool_rounds", 6));
        configSetInputValue("siteAgentRequestTimeout", this.get("general", "site_agent", "request_timeout", 120));
        configSetInputValue("siteAgentMaxHistoryMessages", this.get("general", "site_agent", "max_history_messages", 40));
        configSetInputValue("siteAgentMaxToolResultChars", this.get("general", "site_agent", "max_tool_result_chars", 4000));
        configSetInputValue("siteAgentActionTtlSeconds", this.get("general", "site_agent", "action_ttl_seconds", 300));

        // UI 回填完成后记录「已保存基线」，让设置页的保存条据此判断未保存改动。
        window.refreshGeneralSettingsBaseline?.();
        console.log("常规设置已应用到 UI");
    }

    getEffectiveTheme(): string {
        const personalTheme = getPersonalThemeCookie();
        return personalTheme || this.get<string>("general", "appearance", "theme", "light");
    }

    applyTheme(): void {
        const theme = this.getEffectiveTheme();
        const themeClassNames = ["theme-light", "theme-dark", "theme-cyber-2", "theme-tome", "light-theme", "dark-theme"];
        const body = document.body;
        body.classList.remove(...themeClassNames);

        // 仅在运行时切换主题时启用过渡，避免首屏加载出现闪烁
        if (this.themeInitialized) this.startThemeTransition();

        if (theme === "pattern_cyber_2") {
            body.classList.add("theme-cyber-2");
        } else if (theme === "pattern_tome") {
            body.classList.add("theme-tome");
        } else if (theme === "dark") {
            body.classList.add("theme-dark", "dark-theme");
        } else if (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches) {
            body.classList.add("theme-dark", "dark-theme");
        } else {
            body.classList.add("theme-light", "light-theme");
        }

        this.themeInitialized = true;
        console.log(`主题已应用: ${theme}`);
    }

    private startThemeTransition(): void {
        document.body.classList.add("theme-transition");
        if (this.themeTransitionTimer !== null) {
            window.clearTimeout(this.themeTransitionTimer);
        }
        this.themeTransitionTimer = window.setTimeout(() => {
            document.body.classList.remove("theme-transition");
            this.themeTransitionTimer = null;
        }, 420);
    }

    initThemeSystem(): void {
        this.applyTheme();
        this.applyAdminNameGradient();
        const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        mediaQuery.addEventListener("change", () => {
            const theme = this.get<string>("general", "appearance", "theme", "light");
            if (theme === "system") {
                this.applyTheme();
            }
        });
    }

    applyAdminNameGradient(): void {
        const { from, to } = getAdminNameGradientCookie();
        const root = document.documentElement;
        root.style.setProperty("--admin-name-from", from || DEFAULT_ADMIN_NAME_FROM);
        root.style.setProperty("--admin-name-to", to || DEFAULT_ADMIN_NAME_TO);
    }

    async saveConfig(configName: string, settings: TomlConfig): Promise<boolean> {
        try {
            // 部署层锁定键不参与写回（后端会再过滤一次，这里避免把生效值误当用户意图提交）。
            const locked = this.overrides[configName] || {};
            const payload: TomlConfig = {};
            for (const [section, values] of Object.entries(settings)) {
                if (!isTomlConfig(values)) {
                    payload[section] = values;
                    continue;
                }
                const kept: TomlConfig = {};
                for (const [key, value] of Object.entries(values)) {
                    if (locked[`${section}.${key}`]) continue;
                    kept[key] = value;
                }
                payload[section] = kept;
            }

            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`/api/config/${configName}`, {
                method: "POST",
                body: payload,
            });

            if (!response.ok || !data.success) {
                throw new Error(data.message || data.error || "保存配置失败");
            }

            this.configs[configName] = settings;
            console.log(`配置 ${configName} 保存成功`);
            return true;
        } catch (error) {
            console.error(`保存配置失败: ${configErrorMessage(error)}`);
            return false;
        }
    }
}

function isTomlConfig(value: unknown): value is TomlConfig {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function configSetSelectValue(id: string, value: unknown): void {
    const select = document.getElementById(id) as HTMLSelectElement | null;
    if (select) select.value = String(value ?? "");
}

function configSetInputValue(id: string, value: unknown): void {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.value = String(value ?? "");
}

function configSetCheckboxValue(id: string, value: unknown): void {
    const checkbox = document.getElementById(id) as HTMLInputElement | null;
    if (checkbox) checkbox.checked = Boolean(value);
}

function configErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

const configManager = new ConfigManager();
window.configManager = configManager;

function getPersonalThemeCookie(): string {
    const prefix = "trpg_user_theme=";
    const item = document.cookie
        .split(";")
        .map((part) => part.trim())
        .find((part) => part.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

const DEFAULT_ADMIN_NAME_FROM = "#84ff42";
const DEFAULT_ADMIN_NAME_TO = "#0ebeff";
const HEX_COLOR_PATTERN = /^#[0-9a-fA-F]{6}$/;

function getAdminNameGradientCookie(): { from: string; to: string } {
    const prefix = "trpg_admin_name_gradient=";
    const item = document.cookie
        .split(";")
        .map((part) => part.trim())
        .find((part) => part.startsWith(prefix));
    if (!item) return { from: "", to: "" };
    const [rawFrom = "", rawTo = ""] = decodeURIComponent(item.slice(prefix.length)).split(",");
    return {
        from: HEX_COLOR_PATTERN.test(rawFrom) ? rawFrom : "",
        to: HEX_COLOR_PATTERN.test(rawTo) ? rawTo : "",
    };
}
