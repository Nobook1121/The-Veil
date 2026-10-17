// class 与 types/config.d.ts 中的 interface AIPlatformManager 同名并声明合并：
// 显式实现该接口后，任何方法签名与接口不一致都会在类型检查阶段报错，
// 避免「改了接口、忘了改实现」的静默漂移。接口只描述公开方法，私有字段不参与约束。

// 内置供应商模板：既用于「添加平台」时预填默认值，也用于列表排序（顺序即展示顺序）。
// 平台实际是否展示由后端「列出平台」接口决定，模板不再是写死的加载清单。
const BUILTIN_PLATFORM_TEMPLATES: AIPlatformTemplate[] = [
    {
        id: "aliyun",
        name: "阿里云百炼",
        description: "阿里云 AI 大模型服务平台",
        icon: "/assets/aiplatform/aliyun.png",
        base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        base_url_full: true,
        provider_type: "openai-compatible",
        models: [
            { id: "qwen3.5-plus", name: "Qwen3.5-Plus" },
            { id: "qwen-turbo", name: "Qwen Turbo（经济型）" },
        ],
    },
    {
        id: "siliconflow",
        name: "硅基流动",
        description: "硅基流动 AI 模型服务平台",
        icon: "/assets/aiplatform/siliconflow.png",
        base_url: "https://api.siliconflow.cn/v1",
        base_url_full: false,
        provider_type: "openai-compatible",
        models: [{ id: "gpt-4-turbo", name: "GPT-4 Turbo" }],
    },
    {
        id: "deepseek",
        name: "DeepSeek",
        description: "深度求索 AI 模型服务平台",
        icon: "/assets/aiplatform/deepseek.png",
        base_url: "https://api.deepseek.com/v1",
        base_url_full: false,
        provider_type: "openai-compatible",
        models: [{ id: "deepseek-chat", name: "DeepSeek Chat" }],
    },
    {
        id: "openrouter",
        name: "OpenRouter",
        description: "OpenRouter AI 模型服务平台",
        icon: "/assets/aiplatform/openrouter.png",
        base_url: "https://openrouter.ai/api/v1",
        base_url_full: false,
        provider_type: "openai-compatible",
        models: [{ id: "openai/gpt-4-turbo", name: "GPT-4 Turbo" }],
    },
    {
        id: "lmstudio",
        name: "LMStudio",
        description: "本地运行的 AI 模型服务器，兼容 OpenAI API",
        icon: "/assets/aiplatform/lmstudio.png",
        base_url: "http://localhost:1234/v1",
        base_url_full: false,
        provider_type: "openai-compatible",
        models: [{ id: "local-model", name: "本地模型" }],
    },
    {
        id: "openai",
        name: "OpenAI",
        description: "OpenAI 官方聊天完成接口",
        // 无内置头像文件，前端回落到首字文字头像。
        icon: "",
        base_url: "https://api.openai.com/v1",
        base_url_full: false,
        provider_type: "openai",
        models: [{ id: "gpt-4o", name: "GPT-4o" }],
    },
];

// 内置供应商展示顺序（缺失的内置平台重加后仍排在自定义平台之前）。
const BUILTIN_PLATFORM_ORDER = BUILTIN_PLATFORM_TEMPLATES.map((template) => template.id);

function getBuiltinPlatformTemplate(platform: string): AIPlatformTemplate | null {
    return BUILTIN_PLATFORM_TEMPLATES.find((template) => template.id === platform) || null;
}

// 新模型写入的默认参数，与后端默认请求配置保持一致；用户可在请求模板里覆盖。
const DEFAULT_MODEL_PARAMS = {
    context_window: 8192,
    temperature: 0.7,
    top_p: 0.95,
    max_tokens: 4096,
};

class AIPlatformManager implements AIPlatformManager {
    private readonly platforms: Record<string, AIPlatformConfig> = {};

    async loadPlatforms(): Promise<AIPlatformConfig[]> {
        // 从后端「列出平台」接口读取磁盘上的全部平台（内置 + 自定义）。
        // 旧实现写死内置 id 并静态 fetch，导致自定义新增的平台刷新后丢失。
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ platforms?: unknown }>>(
            "/api/config/aiplatform",
            { method: "GET" },
        );
        if (!response.ok || !data?.success || !Array.isArray(data.data?.platforms)) {
            throw new Error(data?.message || data?.error || "加载平台列表失败");
        }

        for (const key of Object.keys(this.platforms)) delete this.platforms[key];

        const platforms: AIPlatformConfig[] = [];
        for (const raw of data.data.platforms) {
            const normalized = normalizeAIPlatformConfig(raw);
            if (!normalized) continue;
            this.platforms[normalized.platform] = normalized;
            platforms.push(normalized);
        }
        platforms.sort(comparePlatformOrder);
        return platforms;
    }

    getPlatform(platform: string): AIPlatformConfig | null {
        return this.platforms[platform] || null;
    }

    getAllPlatforms(): AIPlatformConfig[] {
        return Object.values(this.platforms);
    }

    getBuiltinTemplates(): AIPlatformTemplate[] {
        // 返回副本，避免调用方修改内置模板常量。
        return BUILTIN_PLATFORM_TEMPLATES.map((template) => ({
            ...template,
            models: template.models.map((model) => ({ ...model })),
        }));
    }

    async setPlatformEnabled(platform: string, enabled: boolean): Promise<boolean> {
        const config = this.getPlatform(platform);
        if (!config) return false;
        config.enabled = enabled;
        return this.updatePlatformConfig(platform, config);
    }

    async updatePlatformConfig(platform: string, config: AIPlatformConfig): Promise<boolean> {
        try {
            await this.savePlatformConfig(platform, config);
            this.platforms[platform] = config;
            return true;
        } catch (error) {
            console.error("更新平台配置失败:", error);
            return false;
        }
    }

    async savePlatformConfig(platform: string, config: AIPlatformConfig): Promise<boolean> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`/api/config/aiplatform/${platform}`, {
            method: "POST",
            body: config,
        });

        if (!response.ok || !data.success) {
            throw new Error(data.message || data.error || "保存配置失败");
        }
        return true;
    }

    async deletePlatform(platform: string): Promise<boolean> {
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(
                `/api/config/aiplatform/${encodeURIComponent(platform)}`,
                { method: "DELETE" },
            );
            if (!response.ok || !data.success) {
                throw new Error(data.message || data.error || "删除平台失败");
            }
            delete this.platforms[platform];
            return true;
        } catch (error) {
            console.error("删除平台失败:", error);
            return false;
        }
    }

    async detectResponsesApi(platform: string): Promise<{ supported: boolean; status: number | null; detail: string }> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse & { data?: { supported?: boolean; status?: number | null; detail?: string } }>(
            `/api/config/aiplatform/${platform}/detect-responses`,
            { method: "POST", body: {} },
        );
        if (!response.ok || !data.success) {
            throw new Error(data.message || data.error || "探测失败");
        }
        const result = data.data || {};
        return {
            supported: result.supported === true,
            status: typeof result.status === "number" ? result.status : null,
            detail: typeof result.detail === "string" ? result.detail : "",
        };
    }

    async addModel(
        platform: string,
        model: Pick<AIModelConfig, "id" | "name"> & Partial<Pick<AIModelConfig, "description">>,
    ): Promise<boolean> {
        try {
            const config = this.getPlatform(platform);
            if (!config) throw new Error("平台不存在");

            config.models.push({
                id: model.id,
                name: model.name,
                description: model.description || "",
                enabled: true,
                params: { ...DEFAULT_MODEL_PARAMS },
            });

            await this.generateModelRequestConfig(platform, model.id);
            await this.savePlatformConfig(platform, config);
            this.platforms[platform] = config;
            return true;
        } catch (error) {
            console.error("添加模型失败:", error);
            return false;
        }
    }

    async removeModel(platform: string, modelId: string): Promise<boolean> {
        try {
            const config = this.getPlatform(platform);
            if (!config) throw new Error("平台不存在");

            const modelIndex = config.models.findIndex((model) => model.id === modelId);
            if (modelIndex === -1) throw new Error("模型不存在");

            config.models.splice(modelIndex, 1);
            await this.deleteModelRequestConfig(platform, modelId);
            await this.savePlatformConfig(platform, config);
            this.platforms[platform] = config;
            return true;
        } catch (error) {
            console.error("移除模型失败:", error);
            return false;
        }
    }

    async generateModelRequestConfig(platform: string, modelId: string): Promise<void> {
        const defaultConfig = await this.getDefaultRequestConfig();
        const requestConfig: Record<string, unknown> = {
            ...defaultConfig,
            model: modelId,
        };

        const { response } = await TrpgApi.requestWithResponse<ApiResponse>("/api/config/aimodel/save", {
            method: "POST",
            body: {
                platform,
                modelId,
                content: requestConfig,
            },
        });

        if (!response.ok) {
            throw new Error("保存模型请求配置失败");
        }
        console.log(`模型请求配置已生成: config/aimodel/${platform}/${modelId}.json`);
    }

    async deleteModelRequestConfig(platform: string, modelId: string): Promise<void> {
        try {
            const { response } = await TrpgApi.requestWithResponse<ApiResponse>("/api/config/aimodel/delete", {
                method: "POST",
                body: {
                    platform,
                    modelId,
                },
            });

            if (!response.ok) {
                throw new Error("删除模型请求配置失败");
            }
            console.log(`模型请求配置已删除: config/aimodel/${platform}/${modelId}.json`);
        } catch (error) {
            console.error("删除模型请求配置失败:", error);
        }
    }

    async getDefaultRequestConfig(): Promise<Record<string, unknown>> {
        try {
            const response = await fetch("config/aiplatform/default-request.json");
            if (!response.ok) throw new Error("无法加载默认模型请求配置");
            const parsed = await response.json() as unknown;
            return aiPlatformIsRecord(parsed) ? parsed : {};
        } catch (error) {
            console.error("获取默认模型请求配置失败:", error);
            return {};
        }
    }

    async testAPI(platform: string, modelId: string): Promise<AITestResult> {
        try {
            const config = this.getPlatform(platform);
            if (!config) throw new Error("平台配置不存在");

            // 自定义平台可能尚未添加任何模型（models 为空）或处于未启用状态，
            // 这两种情况都仍应允许测试连接：只要 base_url/密钥齐全即可发起请求。
            const model = config.models.find((item) => item.id === modelId);
            const timeoutSeconds = typeof config.config.timeout === "number" && config.config.timeout > 0
                ? config.config.timeout
                : 30;

            const startTime = Date.now();
            const testRequest = {
                model: modelId,
                ...getTestRequestConfig(modelId),
            };

            console.log("API 测试请求:", testRequest);
            const { response, data } = await TrpgApi.requestWithResponse<AIPlatformTestResponse>(
                `/api/config/aiplatform/${platform}/test`,
                {
                    method: "POST",
                    body: testRequest,
                    timeout: timeoutSeconds * 1000,
                },
            );

            const duration = (Date.now() - startTime) / 1000;
            if (!response.ok || !data.success) {
                throw new Error(data.error || data.message || `API 请求失败: ${response.status}`);
            }

            const result = data.response;
            const totalTokens = extractTotalTokens(result);
            const tokenSpeed = duration > 0 ? totalTokens / duration : 0;

            return {
                success: true,
                time: new Date().toLocaleString(),
                model: model?.name || modelId,
                speed: `${tokenSpeed.toFixed(2)} token/s`,
                consumption: `${totalTokens} tokens`,
                duration: `${duration.toFixed(2)}s`,
                response: result,
            };
        } catch (error) {
            console.error("API 测试失败:", error);
            return {
                success: false,
                error: aiPlatformErrorMessage(error),
            };
        }
    }

    async savePlatformOrder(order: string[]): Promise<boolean> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(
            "/api/config/aiplatform/order",
            { method: "POST", body: { order } },
        );
        if (!response.ok || !data.success) {
            throw new Error(data.message || data.error || "保存排序失败");
        }
        // 同步本地缓存里的 order，避免重新加载前顺序错乱。
        order.forEach((platformId, index) => {
            const config = this.platforms[platformId];
            if (config) config.order = index;
        });
        return true;
    }

    private async loadPlatform(platform: string): Promise<AIPlatformConfig | null> {
        // 保留该方法以兼容历史调用：直接从磁盘静态路径加载单个平台。
        try {
            const response = await fetch(`config/aiplatform/${platform}.json`);
            if (!response.ok) throw new Error(`无法加载平台配置: ${platform}`);
            const config = await response.json() as unknown;
            const normalized = normalizeAIPlatformConfig(config);
            if (!normalized) throw new Error(`平台配置格式错误: ${platform}`);
            this.platforms[normalized.platform] = normalized;
            return normalized;
        } catch (error) {
            console.error(`加载平台 ${platform} 失败:`, error);
            return null;
        }
    }
}

interface AIPlatformTestResponse extends ApiResponse {
    response?: unknown;
}

function aiPlatformIsRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

// 把后端返回的原始平台配置规整成前端可用的形状：缺失字段（如内置 openai.json 没有 icon）
// 用内置模板或安全默认值兜底，而不是像旧实现那样整条丢弃。
function normalizeAIPlatformConfig(value: unknown): AIPlatformConfig | null {
    if (!aiPlatformIsRecord(value)) return null;
    const platform = typeof value.platform === "string" ? value.platform.trim() : "";
    if (!platform) return null;

    const template = getBuiltinPlatformTemplate(platform);
    const rawConfig = aiPlatformIsRecord(value.config) ? value.config : {};
    const config = rawConfig as unknown as AIPlatformConfig["config"];

    const baseUrl = typeof rawConfig.base_url === "string" && rawConfig.base_url
        ? rawConfig.base_url
        : (template?.base_url || "");
    config.base_url = baseUrl;
    config.timeout = typeof rawConfig.timeout === "number" ? rawConfig.timeout : 30;
    config.provider_type = typeof rawConfig.provider_type === "string" && rawConfig.provider_type
        ? rawConfig.provider_type
        : (template?.provider_type || "openai-compatible");
    config.base_url_full = typeof rawConfig.base_url_full === "boolean"
        ? rawConfig.base_url_full
        : (template?.base_url_full ?? baseUrl.endsWith("/chat/completions"));

    const models = (Array.isArray(value.models) ? value.models : [])
        .map(normalizeAIModelConfig)
        .filter((model): model is AIModelConfig => model !== null);
    config.default_model = typeof rawConfig.default_model === "string" && rawConfig.default_model
        ? rawConfig.default_model
        : (models.find((model) => model.enabled)?.id || models[0]?.id || "");

    return {
        platform,
        name: typeof value.name === "string" && value.name ? value.name : platform,
        description: typeof value.description === "string" ? value.description : "",
        icon: typeof value.icon === "string" ? value.icon : "",
        enabled: value.enabled === true,
        order: typeof value.order === "number" && Number.isFinite(value.order) ? value.order : undefined,
        config,
        models,
    };
}

function normalizeAIModelConfig(value: unknown): AIModelConfig | null {
    if (!aiPlatformIsRecord(value) || typeof value.id !== "string" || !value.id) return null;
    const model: AIModelConfig = {
        id: value.id,
        name: typeof value.name === "string" && value.name ? value.name : value.id,
        description: typeof value.description === "string" ? value.description : "",
        enabled: value.enabled !== false,
    };
    if (aiPlatformIsRecord(value.params)) model.params = value.params;
    return model;
}

// 排序：管理员拖动排序写入的 order 优先；未设置 order 的排在最后，再按内置模板顺序 + 名称。
function comparePlatformOrder(left: AIPlatformConfig, right: AIPlatformConfig): number {
    const leftOrder = typeof left.order === "number" ? left.order : null;
    const rightOrder = typeof right.order === "number" ? right.order : null;
    if (leftOrder !== null && rightOrder !== null) {
        if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    } else if (leftOrder !== null) {
        return -1;
    } else if (rightOrder !== null) {
        return 1;
    }

    const leftIndex = BUILTIN_PLATFORM_ORDER.indexOf(left.platform);
    const rightIndex = BUILTIN_PLATFORM_ORDER.indexOf(right.platform);
    const leftRank = leftIndex === -1 ? BUILTIN_PLATFORM_ORDER.length : leftIndex;
    const rightRank = rightIndex === -1 ? BUILTIN_PLATFORM_ORDER.length : rightIndex;
    if (leftRank !== rightRank) return leftRank - rightRank;
    return left.name.localeCompare(right.name);
}

function extractTotalTokens(result: unknown): number {
    if (!aiPlatformIsRecord(result)) return 0;
    const usage = aiPlatformIsRecord(result.usage) ? result.usage : null;
    if (usage && typeof usage.total_tokens === "number") return usage.total_tokens;

    const output = aiPlatformIsRecord(result.output) ? result.output : null;
    const outputUsage = output && aiPlatformIsRecord(output.usage) ? output.usage : null;
    return outputUsage && typeof outputUsage.total_tokens === "number" ? outputUsage.total_tokens : 0;
}

function aiPlatformErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

const aiPlatformManager = new AIPlatformManager();
window.aiPlatformManager = aiPlatformManager;
