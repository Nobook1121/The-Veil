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
    // 计费用单价（每百万 token），由管理员填写；未填写时只统计 token。
    input_price?: number;
    output_price?: number;
    // 缓存命中时的输入单价（每百万 token）；未填写时按 input_price 计。
    cache_hit_price?: number;
    // 峰时/谷时价格倍率：peak_hours 为空表示不分峰谷。倍率作用于全部 token 费用。
    peak_multiplier?: number;
    offpeak_multiplier?: number;
    peak_hours?: string;
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
        // 计费货币代码（CNY/USD/EUR/JPY），默认 CNY。
        currency?: string;
        // true 表示 base_url 已经是完整端点；false 表示由后端自动补齐 chat/completions 后缀。
        base_url_full?: boolean;
        // 平台默认模型 id，用于测试连接等默认选型。
        default_model?: string;
        // 平台级请求参数（temperature / top_p / top_k / max_tokens 等），在「高级选项」
        // 中配置；运行时优先级高于默认请求模板与模型请求模板的同名键。
        request_params?: Record<string, unknown>;
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
