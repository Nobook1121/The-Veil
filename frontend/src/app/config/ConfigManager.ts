class ConfigManager {
    private readonly configs: Record<string, TomlConfig> = {};
    private readonly configPath = "config";
    private themeInitialized = false;
    private themeTransitionTimer: number | null = null;

    async loadConfig(configName: string): Promise<TomlConfig | null> {
        try {
            const response = await fetch(`${this.configPath}/${configName}.toml`);
            if (!response.ok) {
                throw new Error(`无法加载配置文件: ${configName}.toml`);
            }
            const config = this.parseTOML(await response.text());
            this.configs[configName] = config;
            console.log(`配置文件 ${configName}.toml 加载成功`);
            return config;
        } catch (error) {
            console.error(`加载配置文件失败: ${configErrorMessage(error)}`);
            return null;
        }
    }

    parseTOML(tomlContent: string): TomlConfig {
        const config: TomlConfig = {};
        let currentSection: string | null = null;

        for (let line of tomlContent.split("\n")) {
            const commentIndex = line.indexOf("#");
            if (commentIndex !== -1) {
                line = line.substring(0, commentIndex);
            }
            line = line.trim();
            if (!line) continue;

            const sectionMatch = line.match(/^\[(.+)\]$/);
            if (sectionMatch) {
                currentSection = normalizeTomlKey(sectionMatch[1] || "");
                config[currentSection] = {};
                continue;
            }

            const keyValueMatch = line.match(/^([^=]+)=(.+)$/);
            if (!keyValueMatch) continue;

            const key = normalizeTomlKey(keyValueMatch[1] || "");
            const value = this.parseValue((keyValueMatch[2] || "").trim());
            if (currentSection) {
                const section = config[currentSection];
                if (isTomlConfig(section)) section[key] = value;
            } else {
                config[key] = value;
            }
        }

        return config;
    }

    parseValue(value: string): TomlConfigValue {
        if (value.startsWith('"') && value.endsWith('"')) {
            // TOML 基本字符串需要还原 \" \\ 等转义，否则嵌套结构（以 JSON 字符串保存）
            // 读出来会残留反斜杠，再次保存时转义会反复叠加。
            try {
                const parsed = JSON.parse(value) as unknown;
                if (typeof parsed === "string") return parsed;
            } catch {
                // 非法 JSON 字符串（如未转义的引号）按字面量处理
            }
            return value.slice(1, -1);
        }
        if (value.startsWith("'") && value.endsWith("'")) {
            return value.slice(1, -1);
        }
        if (value === "true") return true;
        if (value === "false") return false;
        if (/^-?\d+$/.test(value)) return Number.parseInt(value, 10);
        if (/^-?\d+\.\d+$/.test(value)) return Number.parseFloat(value);

        if (value.startsWith("[") && value.endsWith("]")) {
            try {
                const parsed = JSON.parse(value.replace(/'/g, '"')) as unknown;
                if (Array.isArray(parsed)) {
                    return parsed.filter(isTomlScalar);
                }
            } catch {
                return value.slice(1, -1).split(",").map((item) => String(this.parseValue(item.trim())));
            }
        }

        return value;
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
        configSetCheckboxValue("dicePlayerRollDefault", this.get("general", "ai", "dice_player_roll", false));
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
        // 网站 Agent 的启用开关与限额
        configSetCheckboxValue("siteAgentEnabled", this.get("general", "site_agent", "enabled", false));
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
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`/api/config/${configName}`, {
                method: "POST",
                body: settings,
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

function isTomlScalar(value: unknown): value is string | number | boolean {
    return ["string", "number", "boolean"].includes(typeof value);
}

function isTomlConfig(value: unknown): value is TomlConfig {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 还原 TOML 段名 / 键名的外层引号，例如 ["ai.small_models"] 应解析为 ai.small_models。
 * 若不去引号，保存回写时引号会被再次转义，反复保存会不断叠加转义。
 */
function normalizeTomlKey(raw: string): string {
    const text = raw.trim();
    if (text.length >= 2 && text.startsWith('"') && text.endsWith('"')) {
        try {
            const parsed = JSON.parse(text) as unknown;
            if (typeof parsed === "string") return parsed;
        } catch {
            // 非合法 JSON 字符串时按字面量处理
        }
        return text.slice(1, -1);
    }
    if (text.length >= 2 && text.startsWith("'") && text.endsWith("'")) {
        return text.slice(1, -1);
    }
    return text;
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
