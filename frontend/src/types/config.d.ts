interface ConfigManager {
    loadConfig(configName: string): Promise<TomlConfig | null>;
    saveConfig(configName: string, settings: TomlConfig): Promise<boolean>;
    getConfig(configName: string): TomlConfig;
    get<T = unknown>(configName: string, section: string | null, key: string, defaultValue?: T): T;
    getSection(configName: string, section: string): TomlConfig | null;
    getOverrideSource(configName: string, section: string, key: string): string | null;
    applyGeneralSettings(): void;
    initThemeSystem(): void;
    getEffectiveTheme(): string;
    applyTheme(): void;
    applyAdminNameGradient(): void;
}

type TomlConfigValue = string | number | boolean | Array<string | number | boolean> | TomlConfig;
interface TomlConfig {
    [key: string]: TomlConfigValue;
}

interface AIModelConfig {
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    params?: Record<string, unknown>;
}

interface AIPlatformConfig {
    platform: string;
    name: string;
    description: string;
    icon: string;
    enabled: boolean;
    // 管理员拖动排序后写入的加载顺序：数值越小越先加载、越靠前展示。
    order?: number | undefined;
    config: {
        api_key?: string;
        base_url: string;
        timeout: number;
        // 提供商 API 标准（openai / openai-compatible / anthropic），仅作元数据存储与展示。
        provider_type?: string;
        // true 表示 base_url 已经是完整端点；false 表示由后端自动补齐 chat/completions 后缀。
        base_url_full?: boolean;
        // 平台默认模型 id，用于测试连接等默认选型。
        default_model?: string;
        responses_api_supported?: boolean;
        responses_api_checked_at?: string;
        responses_api_detail?: string;
        use_previous_response_id?: boolean;
    };
    models: AIModelConfig[];
}

// 内置供应商模板：作为「添加平台」时的可选项与预填默认值，而非写死的加载清单。
interface AIPlatformTemplate {
    id: string;
    name: string;
    description: string;
    icon: string;
    base_url: string;
    base_url_full: boolean;
    provider_type: string;
    models: Array<{ id: string; name: string; description?: string }>;
}

interface AIPlatformManager {
    loadPlatforms(): Promise<AIPlatformConfig[]>;
    getPlatform(platform: string): AIPlatformConfig | null;
    getAllPlatforms(): AIPlatformConfig[];
    getBuiltinTemplates(): AIPlatformTemplate[];
    setPlatformEnabled(platform: string, enabled: boolean): Promise<boolean>;
    updatePlatformConfig(platform: string, config: AIPlatformConfig): Promise<boolean>;
    savePlatformConfig(platform: string, config: AIPlatformConfig): Promise<boolean>;
    deletePlatform(platform: string): Promise<boolean>;
    addModel(platform: string, model: Pick<AIModelConfig, "id" | "name"> & Partial<Pick<AIModelConfig, "description">>): Promise<boolean>;
    removeModel(platform: string, modelId: string): Promise<boolean>;
    testAPI(platform: string, modelId: string): Promise<AITestResult>;
    savePlatformOrder(order: string[]): Promise<boolean>;
}

interface AITestResult {
    success: boolean;
    time?: string;
    model?: string;
    speed?: string;
    consumption?: string;
    duration?: string;
    response?: unknown;
    error?: string;
}

interface TestRequestConfig {
    messages: Array<{ role: string; content: string }>;
    temperature: number;
    max_tokens: number;
    stop: string[];
    extra_body?: Record<string, unknown>;
}

interface PermissionNode {
    id: string;
    label: string;
    description?: string;
}

interface PermissionGroup {
    id: string;
    label: string;
    description?: string;
    nodes: PermissionNode[];
}

interface PermissionConfig {
    roles: string[];
    groups: PermissionGroup[];
    matrix: Record<string, string[]>;
}
