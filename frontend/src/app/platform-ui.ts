interface RoleConfig {
    id: string;
    name: string;
    avatar?: string;
    wake_words?: string[];
    provider?: string;
    prompt?: string;
    description?: string;
}

interface RoleConfigResponse {
    roles: RoleConfig[];
    enabled_providers: Array<{ id: string; name: string }>;
}

interface DebugPromptResponse {
    content: string;
}

// 角色卡默认头像：留空即由前端按角色名称首字生成文字头像，不再有内置默认图。
// LM Studio 等本地服务默认使用的模型 id：与 config/aiplatform、TestRequestConfig 中的约定一致。
const LMSTUDIO_DEFAULT_MODEL_ID = "local-model";
// 默认模型请求配置的兜底文件路径（新建模型时作为请求模板来源）。
const DEFAULT_REQUEST_CONFIG_PATH = "config/aiplatform/default-request.json";

// 「添加/编辑平台」弹窗的当前状态：config 为尚未保存的草稿，isNew 表示新增模式。
// iconUploaded 记录用户是否手动上传过头像，避免选择内置供应商时覆盖用户上传的图片。
let platformModalState: { config: AIPlatformConfig; isNew: boolean; iconUploaded: boolean; apiKey: string; savedId: string } | null = null;

// 供应商 API 标准选项：仅作为元数据持久化/展示，不改变后端请求构造。
const PROVIDER_TYPE_OPTIONS = ["openai", "openai-compatible", "anthropic"];
// 平台头像上传大小上限，data URL 会写进平台配置文件，需保持精简。
const PLATFORM_ICON_MAX_BYTES = 1 * 1024 * 1024;

// 弹窗内部元素 id 的稳定后缀：新增自定义平台时 platform 为空，用固定占位符。
function platformModalKey(config: AIPlatformConfig): string {
    return config.platform || "new-platform";
}

function providerTypeLabel(providerType: string): string {
    if (providerType === "openai") return platformT("platform.provider.openai", "OpenAI 官方格式");
    if (providerType === "anthropic") return platformT("platform.provider.anthropic", "Anthropic");
    return platformT("platform.provider.openai_compatible", "OpenAI 兼容");
}

// 头像渲染：有 icon 时用图片，否则用供应商名称首字生成色块文字头像。
function platformAvatarHTML(config: { icon?: string; name: string }, extraClass = ""): string {
    const icon = (config.icon || "").trim();
    if (icon) {
        return `<img src="${platformEscapeHtml(icon)}" alt="${platformEscapeHtml(config.name)}" class="platform-avatar-img ${extraClass}">`;
    }
    // 首字与色相复用全局文字头像工具，保证与用户头像、角色卡头像同一套规则。
    const letter = TrpgAvatar.initial(config.name);
    return `<span class="platform-avatar-text ${extraClass}" style="--avatar-hue:${TrpgAvatar.hue(config.name)}">${platformEscapeHtml(letter)}</span>`;
}

function clonePlatformConfig(config: AIPlatformConfig): AIPlatformConfig {
    return JSON.parse(JSON.stringify(config)) as AIPlatformConfig;
}

// 新增自定义平台时用名称生成一个安全的平台 id（文件名/接口参数）。
function buildPlatformId(name: string, existing: string[]): string {
    let base = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    if (!base) base = `platform-${Date.now()}`;
    let candidate = base;
    let suffix = 2;
    while (existing.includes(candidate)) {
        candidate = `${base}-${suffix}`;
        suffix += 1;
    }
    return candidate;
}

// 平台管理界面的 i18n 取值辅助函数，写法与 chat.ts 的 chatT 保持一致。
function platformT(key: string, fallback: string, values: Record<string, string | number> = {}): string {
    return window.TrpgI18n?.t(key, fallback, values) || fallback;
}

async function initAIPlatforms(): Promise<void> {
    try {
        const platforms = await aiPlatformManager.loadPlatforms();
        renderPlatforms(platforms);
        bindRoleConfigSettings();
        await loadRoleConfigs();
        await loadDebugPrompt();
        bindDebugPromptSettings();
        bindAddModelEvents();
        bindAddPlatformEvents();
        bindAPITestEvents();
        console.log("AI 平台管理初始化完成");
    } catch (error) {
        console.error("初始化 AI 平台管理失败:", error);
    }
}

function renderPlatforms(platforms: AIPlatformConfig[]): void {
    const container = document.getElementById("ai-platforms-container");
    if (!container) return;
    container.innerHTML = "";
    platforms.forEach((platform) => container.appendChild(createPlatformCard(platform)));
    bindPlatformDragAndDrop(container);
}

// 正在被拖动的平台卡片：拖动期间据此在容器内实时重排。
let draggedPlatformCard: HTMLElement | null = null;

// 平台卡片的拖动排序：拖动结束后把新顺序提交给后端，决定运行时的平台加载顺序。
function bindPlatformDragAndDrop(container: HTMLElement): void {
    container.querySelectorAll<HTMLElement>(".ai-platform-card").forEach((card) => {
        card.addEventListener("dragstart", (event) => {
            draggedPlatformCard = card;
            card.classList.add("is-dragging");
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", card.dataset.platform || "");
            }
        });
        card.addEventListener("dragend", () => {
            card.classList.remove("is-dragging");
            draggedPlatformCard = null;
        });
    });

    // 容器级监听只需绑定一次（卡片每次渲染都会重建，但容器是固定的）。
    if (container.dataset.dndBound === "true") return;
    container.dataset.dndBound = "true";

    container.addEventListener("dragover", (event) => {
        if (!draggedPlatformCard) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        const others = Array.from(container.querySelectorAll<HTMLElement>(".ai-platform-card:not(.is-dragging)"));
        const after = others.find((card) => {
            const rect = card.getBoundingClientRect();
            return event.clientY < rect.top + rect.height / 2;
        });
        if (after) container.insertBefore(draggedPlatformCard, after);
        else container.appendChild(draggedPlatformCard);
    });

    container.addEventListener("drop", (event) => {
        event.preventDefault();
        void persistPlatformOrder(container);
    });
}

async function persistPlatformOrder(container: HTMLElement): Promise<void> {
    const order = Array.from(container.querySelectorAll<HTMLElement>(".ai-platform-card"))
        .map((card) => card.dataset.platform || "")
        .filter(Boolean);
    if (!order.length) return;
    try {
        await aiPlatformManager.savePlatformOrder(order);
        showNotification(platformT("platform.order.saved", "平台加载顺序已保存"), "success");
    } catch (error) {
        showNotification(
            platformT("platform.order.save_failed", "保存平台顺序失败：{detail}", { detail: platformErrorMessage(error) }),
            "error",
        );
    }
}

function bindRoleConfigSettings(): void {
    const list = document.getElementById("roleConfigList");
    if (!list || list.dataset.bound === "true") return;

    list.dataset.bound = "true";
    list.addEventListener("click", (event) => {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".save-role-config");
        if (!button) return;
        void saveRoleConfig(button.dataset.roleId || "");
    });
}

async function loadRoleConfigs(): Promise<void> {
    const list = document.getElementById("roleConfigList");
    if (!list) return;

    try {
        const response = await TrpgApi.get<ApiResponse<RoleConfigResponse>>("/api/config/roles");
        if (!response.success || !response.data) {
            setRoleConfigMessage(response.error || response.message || platformT("platform.role_config.load_failed", "加载角色配置失败"), true);
            return;
        }
        renderRoleConfigCards(response.data.roles || [], response.data.enabled_providers || []);
        setRoleConfigMessage("");
    } catch (error) {
        console.error("加载角色配置失败:", error);
        setRoleConfigMessage(platformT("platform.role_config.load_failed_admin", "加载角色配置失败，请确认当前账号具有管理员权限"), true);
    }
}

window.reloadRoleConfigs = loadRoleConfigs;

async function loadDebugPrompt(): Promise<void> {
    const prompt = document.getElementById("debugKpPrompt") as HTMLTextAreaElement | null;
    const mode = document.getElementById("debugMode") as HTMLInputElement | null;
    if (!prompt || !mode) return;

    try {
        const response = await TrpgApi.get<ApiResponse<DebugPromptResponse>>("/api/config/debug-prompt");
        if (response.success && response.data) prompt.value = response.data.content || "";
        mode.checked = configManager.get<boolean>("general", "ai", "debug_mode", false);
    } catch (error) {
        console.error("Failed to load AI debug prompt:", error);
        setDebugPromptMessage(platformT("platform.debug.load_failed", "调试提示词加载失败"), true);
    }
}

function bindDebugPromptSettings(): void {
    const saveButton = document.getElementById("saveDebugKpPrompt");
    if (!saveButton || saveButton.dataset.bound === "true") return;
    saveButton.dataset.bound = "true";
    saveButton.addEventListener("click", () => void saveDebugPrompt());
}

async function saveDebugPrompt(): Promise<void> {
    const prompt = document.getElementById("debugKpPrompt") as HTMLTextAreaElement | null;
    if (!prompt || !prompt.value.trim()) {
        setDebugPromptMessage(platformT("platform.debug.empty", "调试提示词不能为空"), true);
        return;
    }

    try {
        const response = await TrpgApi.post<ApiResponse>("/api/config/debug-prompt", { content: prompt.value });
        if (!response.success) throw new Error(response.error || response.message || platformT("platform.debug.save_failed", "保存调试提示词失败"));
        setDebugPromptMessage(platformT("platform.debug.saved", "调试提示词已保存"));
    } catch (error) {
        setDebugPromptMessage(platformErrorMessage(error), true);
    }
}

function setDebugPromptMessage(message: string, isError = false): void {
    const element = document.getElementById("debugPromptMessage");
    if (!element) return;
    element.textContent = message;
    element.className = `settings-message${isError ? " error" : " success"}`;
}

function renderRoleConfigCards(roles: RoleConfig[], providers: Array<{ id: string; name: string }>): void {
    const list = document.getElementById("roleConfigList");
    if (!list) return;

    if (roles.length === 0) {
        list.innerHTML = `<div class="role-config-card">${platformEscapeHtml(platformT("platform.role_config.empty", "暂无角色配置"))}</div>`;
        return;
    }

    list.innerHTML = roles.map((role) => {
        const providerOptions = providers.map((provider) => {
            const selected = provider.id === role.provider ? "selected" : "";
            return `<option value="${platformEscapeHtml(provider.id)}" ${selected}>${platformEscapeHtml(provider.name)}</option>`;
        }).join("");
        const wakeWords = (role.wake_words || []).join(", ");
        return `
            <article class="role-config-card" data-role-id="${platformEscapeHtml(role.id)}">
                <div class="role-config-card-header">
                    <div>
                        <h5 class="role-config-card-title">${platformEscapeHtml(role.name)}</h5>
                        <div class="role-config-wake">${platformEscapeHtml(wakeWords || `@${role.name}`)}</div>
                    </div>
                    <button type="button" class="btn btn-primary save-role-config" data-role-id="${platformEscapeHtml(role.id)}">
                        <i class="fa fa-floppy-o" aria-hidden="true"></i> ${platformEscapeHtml(platformT("common.save", "保存"))}
                    </button>
                </div>
                <label class="form-label" for="roleDescription-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.description", "角色说明"))}</label>
                <input class="form-control role-description-input" id="roleDescription-${platformEscapeHtml(role.id)}" value="${platformEscapeHtml(role.description || "")}" placeholder="${platformEscapeHtml(platformT("platform.role_config.description_placeholder", "例如：模块摘要、调试 KP 或检定助手"))}">
                <label class="form-label" for="roleName-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.name", "角色名称"))}</label>
                <input class="form-control role-name-input" id="roleName-${platformEscapeHtml(role.id)}" value="${platformEscapeHtml(role.name)}" placeholder="KP">
                <label class="form-label" for="roleAvatar-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.avatar", "头像 URL"))}</label>
                <input class="form-control role-avatar-input" id="roleAvatar-${platformEscapeHtml(role.id)}" value="${platformEscapeHtml(role.avatar || "")}" placeholder="留空则显示角色名称首字">
                <label class="form-label" for="roleWakeWords-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.wake_words", "唤醒词"))}</label>
                <input class="form-control role-wake-input" id="roleWakeWords-${platformEscapeHtml(role.id)}" value="${platformEscapeHtml(wakeWords)}" placeholder="@KP, @Keeper">
                <label class="form-label" for="roleProvider-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.provider", "大模型提供商"))}</label>
                <select class="form-control role-provider-select" id="roleProvider-${platformEscapeHtml(role.id)}">${providerOptions}</select>
                <label class="form-label" for="rolePrompt-${platformEscapeHtml(role.id)}">${platformEscapeHtml(platformT("platform.role_config.prompt", "角色提示词"))}</label>
                <textarea class="form-control role-config-prompt" id="rolePrompt-${platformEscapeHtml(role.id)}" rows="8">${platformEscapeHtml(role.prompt || "")}</textarea>
            </article>
        `;
    }).join("");
}

async function saveRoleConfig(roleId: string): Promise<void> {
    if (!roleId) return;
    const card = document.querySelector<HTMLElement>(`.role-config-card[data-role-id="${CSS.escape(roleId)}"]`);
    if (!card) return;

    const wakeWords = (card.querySelector<HTMLInputElement>(".role-wake-input")?.value || "")
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
    const provider = card.querySelector<HTMLSelectElement>(".role-provider-select")?.value || "";
    const prompt = card.querySelector<HTMLTextAreaElement>(".role-config-prompt")?.value || "";
    const name = card.querySelector<HTMLInputElement>(".role-name-input")?.value.trim() || roleId;
    const avatar = card.querySelector<HTMLInputElement>(".role-avatar-input")?.value.trim() || "";
    const description = card.querySelector<HTMLInputElement>(".role-description-input")?.value.trim() || "";

    try {
        const response = await TrpgApi.post<ApiResponse>(`/api/config/roles/${encodeURIComponent(roleId)}`, {
            name,
            avatar,
            description,
            wake_words: wakeWords,
            provider,
            prompt,
        });
        if (!response.success) {
            setRoleConfigMessage(response.error || response.message || platformT("platform.role_config.save_failed", "保存角色配置失败"), true);
            return;
        }
        setRoleConfigMessage(platformT("platform.role_config.saved", "角色配置已保存"));
        window.loadAIRoles?.();
        await loadRoleConfigs();
    } catch (error) {
        console.error("保存角色配置失败:", error);
        setRoleConfigMessage(platformT("platform.role_config.save_failed_retry", "保存角色配置失败，请稍后重试"), true);
    }
}

function setRoleConfigMessage(message: string, isError = false): void {
    const messageElement = document.getElementById("roleConfigMessage");
    if (!messageElement) return;
    messageElement.textContent = message;
    messageElement.classList.toggle("error", isError);
    messageElement.classList.toggle("success", Boolean(message && !isError));
}

function createPlatformCard(platform: AIPlatformConfig): HTMLElement {
    const card = document.createElement("div");
    card.className = "ai-platform-card";
    // 可拖动排序：卡片顺序即平台加载顺序（越靠上越先加载）。
    card.draggable = true;
    card.dataset.platform = platform.platform;
    const enabledClass = platform.enabled ? " is-enabled" : "";
    card.innerHTML = `
        <div class="platform-card-row">
            <span class="platform-drag-handle" title="${platformEscapeHtml(platformT("platform.card.drag", "拖动排序（顺序即平台加载顺序）"))}" aria-label="${platformEscapeHtml(platformT("platform.card.drag", "拖动排序（顺序即平台加载顺序）"))}">
                <i class="fa fa-grip-vertical" aria-hidden="true"></i>
            </span>
            <div class="platform-card-avatar">${platformAvatarHTML(platform)}</div>
            <div class="platform-card-info">
                <h5>${platformEscapeHtml(platform.name)}</h5>
                <p>${platformEscapeHtml(platform.description || providerTypeLabel(platform.config.provider_type || ""))}</p>
            </div>
            <div class="platform-card-actions">
                <button type="button" class="btn btn-sm platform-enabled-btn${enabledClass}" data-platform="${platformEscapeHtml(platform.platform)}">
                    ${platformEscapeHtml(platform.enabled ? platformT("platform.card.in_use", "使用中") : platformT("platform.card.enable", "启用"))}
                </button>
                <button type="button" class="btn btn-sm btn-outline-secondary platform-icon-btn platform-edit-btn" data-platform="${platformEscapeHtml(platform.platform)}" title="${platformEscapeHtml(platformT("platform.card.edit", "编辑"))}" aria-label="${platformEscapeHtml(platformT("platform.card.edit", "编辑"))}">
                    <i class="fa fa-pencil" aria-hidden="true"></i>
                </button>
                <button type="button" class="btn btn-sm btn-outline-secondary platform-icon-btn test-api-btn" data-platform="${platformEscapeHtml(platform.platform)}" title="${platformEscapeHtml(platformT("platform.card.detect", "检测连通"))}" aria-label="${platformEscapeHtml(platformT("platform.card.detect", "检测连通"))}">
                    <i class="fa fa-plug" aria-hidden="true"></i>
                </button>
                <button type="button" class="btn btn-sm btn-outline-danger platform-icon-btn platform-delete-btn" data-platform="${platformEscapeHtml(platform.platform)}" title="${platformEscapeHtml(platformT("platform.card.delete", "删除"))}" aria-label="${platformEscapeHtml(platformT("platform.card.delete", "删除"))}">
                    <i class="fa fa-trash" aria-hidden="true"></i>
                </button>
            </div>
        </div>
    `;

    card.querySelector(".platform-enabled-btn")?.addEventListener("click", async (event) => {
        const button = event.currentTarget as HTMLButtonElement;
        const next = !platform.enabled;
        const success = await aiPlatformManager.setPlatformEnabled(platform.platform, next);
        if (!success) {
            showNotification(platformT("platform.card.toggle_failed", "更新启用状态失败"), "error");
            return;
        }
        platform.enabled = next;
        button.classList.toggle("is-enabled", next);
        button.textContent = next ? platformT("platform.card.in_use", "使用中") : platformT("platform.card.enable", "启用");
    });

    card.querySelector(".platform-edit-btn")?.addEventListener("click", () => {
        void openPlatformConfigModal(clonePlatformConfig(platform), false);
    });

    card.querySelector(".platform-delete-btn")?.addEventListener("click", () => {
        void deletePlatform(platform);
    });

    return card;
}

async function deletePlatform(platform: AIPlatformConfig): Promise<void> {
    if (!confirm(platformT("platform.card.delete_confirm", "确定删除「{name}」吗？该平台的配置与密钥会一并删除。", { name: platform.name }))) return;
    const success = await aiPlatformManager.deletePlatform(platform.platform);
    if (!success) {
        showNotification(platformT("platform.card.delete_failed", "删除平台失败"), "error");
        return;
    }
    renderPlatforms(await aiPlatformManager.loadPlatforms());
    showNotification(platformT("platform.card.deleted", "平台已删除"), "success");
}

// 打开「添加/编辑平台」弹窗：config 是草稿副本，isNew 决定保存时是否新建平台 id。
async function openPlatformConfigModal(config: AIPlatformConfig, isNew: boolean): Promise<void> {
    platformModalState = { config, isNew, iconUploaded: Boolean(config.icon && config.icon.startsWith("data:")), apiKey: "", savedId: "" };
    const modalElement = document.getElementById("platformConfigModal");
    const content = document.getElementById("platformConfigContent");
    const title = document.getElementById("platformConfigModalLabel");
    if (!modalElement || !content || !title) return;

    title.textContent = isNew
        ? platformT("platform.form.add_title", "添加平台")
        : platformT("platform.form.edit_title", "编辑平台");
    renderPlatformModalBody(config, isNew);

    new bootstrap.Modal(modalElement, { backdrop: false }).show();
}

// 渲染（或重渲染）弹窗主体并重新绑定事件。所有弹窗交互都基于当前 draft。
function renderPlatformModalBody(config: AIPlatformConfig, isNew: boolean): void {
    const content = document.getElementById("platformConfigContent");
    if (!content) return;
    content.innerHTML = buildPlatformConfigHTML(config, isNew);
    bindPasswordToggles();
    bindPlatformVendorPicker(config);
    bindPlatformIconUpload();
    bindPlatformFormFields();
    bindTimeoutSlider(config.platform);
    bindResponsesApiDetect(config);
    bindModelEvents(config, isNew);
    bindPlatformModalFooter(config, isNew);
}

function buildPlatformConfigHTML(config: AIPlatformConfig, isNew: boolean): string {
    const id = platformEscapeHtml(platformModalKey(config));
    const templates = aiPlatformManager.getBuiltinTemplates();
    const vendorChips = templates.map((template) => {
        const active = template.id === config.platform ? " is-active" : "";
        return `<button type="button" class="platform-vendor-option${active}" data-vendor="${platformEscapeHtml(template.id)}" title="${platformEscapeHtml(template.name)}">${platformAvatarHTML({ icon: template.icon, name: template.name }, "platform-vendor-avatar")}</button>`;
    }).join("");
    const customActive = config.platform === "" ? " is-active" : "";
    const providerOptions = PROVIDER_TYPE_OPTIONS.map((value) => {
        const selected = value === (config.config.provider_type || "openai-compatible") ? "selected" : "";
        return `<option value="${platformEscapeHtml(value)}" ${selected}>${platformEscapeHtml(providerTypeLabel(value))}</option>`;
    }).join("");

    return `
        <div class="platform-form">
            <div class="platform-vendor-picker" role="group" aria-label="${platformEscapeHtml(platformT("platform.form.vendor", "供应商头像"))}">
                <div class="platform-vendor-avatar-preview" id="modal-avatar-preview-${id}">${platformAvatarHTML(config, "platform-vendor-preview-img")}</div>
                <div class="platform-vendor-options">
                    ${vendorChips}
                    <button type="button" class="platform-vendor-option platform-vendor-custom${customActive}" data-vendor="" title="${platformEscapeHtml(platformT("platform.form.custom", "自定义"))}">
                        <span class="platform-avatar-text platform-vendor-avatar" style="--avatar-hue:210"><i class="fa fa-sliders" aria-hidden="true"></i></span>
                    </button>
                </div>
                <label class="platform-icon-upload">
                    <i class="fa fa-upload" aria-hidden="true"></i> ${platformEscapeHtml(platformT("platform.form.upload_icon", "上传头像"))}
                    <input type="file" accept="image/*" id="modal-icon-upload-${id}" class="platform-icon-upload-input" hidden>
                </label>
            </div>

            <div class="form-group mt-3">
                <label for="modal-platform-name-${id}">${platformEscapeHtml(platformT("platform.form.name", "供应商名称"))}</label>
                <input type="text" class="form-control" id="modal-platform-name-${id}" value="${platformEscapeHtml(config.name)}" placeholder="${platformEscapeHtml(platformT("platform.form.name_placeholder", "请输入供应商名称"))}">
            </div>
            <div class="form-group mt-2">
                <label for="modal-api-key-${id}">API Key</label>
                <div class="password-input-group">
                    <input type="password" class="form-control api-key-input" id="modal-api-key-${id}" value="${platformEscapeHtml(platformModalState?.apiKey || "")}" autocomplete="new-password" placeholder="${platformEscapeHtml(platformT("platform.config.api_key_placeholder", "输入新密钥"))}">
                    <span class="password-toggle" data-target="modal-api-key-${id}"><i class="bi bi-eye"></i></span>
                </div>
            </div>
            <div class="form-group mt-2">
                <label for="modal-base-url-${id}">Base URL</label>
                <input type="text" class="form-control base-url-input" id="modal-base-url-${id}" value="${platformEscapeHtml(config.config.base_url)}" placeholder="${platformEscapeHtml(platformT("platform.form.base_url_placeholder", "https://api.example.com/v1"))}">
                <div class="form-check form-switch mt-2">
                    <input class="form-check-input" type="checkbox" id="modal-full-url-${id}" ${config.config.base_url_full ? "checked" : ""}>
                    <label class="form-check-label" for="modal-full-url-${id}">${platformEscapeHtml(platformT("platform.form.full_url", "完整 URL"))}</label>
                </div>
                <small class="text-muted">${platformEscapeHtml(platformT("platform.form.full_url_hint", "开启时表示上面填写的就是完整端点；关闭时程序会自动拼接 /v1/chat/completions（已带版本号则补 /chat/completions），请只填服务地址且不要以斜杠结尾。"))}</small>
            </div>

            <details class="platform-advanced">
                <summary>${platformEscapeHtml(platformT("platform.form.advanced", "高级选项"))}</summary>
                <div class="platform-advanced-body">
                    <div class="form-group mt-2">
                        <label for="modal-provider-type-${id}">${platformEscapeHtml(platformT("platform.form.provider_type", "提供商类型"))}</label>
                        <select class="form-select" id="modal-provider-type-${id}">${providerOptions}</select>
                    </div>
                    ${buildModelList(config, isNew)}
                    <div class="form-group mt-3">
                        <label for="modal-timeout-${id}">${platformEscapeHtml(platformT("platform.config.timeout_label", "超时设置 ({value} 秒)", { value: config.config.timeout }))}</label>
                        <input type="range" class="form-range timeout-slider" id="modal-timeout-${id}" min="10" max="120" step="5" value="${config.config.timeout}" data-platform="${platformEscapeHtml(config.platform)}">
                        <div class="timeout-value" id="modal-timeout-value-${id}">${platformEscapeHtml(platformT("platform.timeout.seconds", "{value} 秒", { value: config.config.timeout }))}</div>
                    </div>
                    ${isNew ? "" : buildResponsesApiSection(config)}
                </div>
            </details>
        </div>
    `;
}

function buildResponsesApiSection(config: AIPlatformConfig): string {
    const id = platformEscapeHtml(platformModalKey(config));
    return `
        <div class="form-group mt-3 responses-api-section">
            <label>Responses API（previous_response_id）</label>
            <p class="responses-api-status" id="modal-responses-status-${id}">${responsesApiStatusText(config)}</p>
            <div class="d-flex align-items-center gap-3 flex-wrap">
                <button type="button" class="btn btn-sm btn-primary detect-responses-btn" data-platform="${platformEscapeHtml(config.platform)}">${platformEscapeHtml(platformT("platform.responses.detect_button", "检测支持情况"))}</button>
                <div class="form-check form-switch">
                    <input class="form-check-input use-previous-response-input" type="checkbox" id="modal-use-previous-response-${id}" ${config.config.use_previous_response_id ? "checked" : ""} ${config.config.responses_api_supported === true ? "" : "disabled"}>
                    <label class="form-check-label" for="modal-use-previous-response-${id}">${platformEscapeHtml(platformT("platform.responses.toggle_label", "启用 previous_response_id"))}</label>
                </div>
            </div>
            <small class="text-muted">${platformEscapeHtml(platformT("platform.responses.hint", "检测通过后才能启用：多轮工具调用只发送增量上下文以降低重复计费。该能力要求平台提供 /responses 端点，并把会话保存在服务端。"))}</small>
        </div>
    `;
}

function responsesApiStatusText(config: AIPlatformConfig): string {
    if (config.config.responses_api_supported === true) {
        const checked = config.config.responses_api_checked_at
            ? platformT("platform.responses.checked_at", "（检测于 {time}）", { time: platformEscapeHtml(config.config.responses_api_checked_at) })
            : "";
        return platformT("platform.responses.supported", "该平台支持 Responses API{checked}。", { checked });
    }
    if (config.config.responses_api_checked_at) {
        const detail = config.config.responses_api_detail
            ? platformT("platform.responses.detail_suffix", "：{detail}", { detail: platformEscapeHtml(config.config.responses_api_detail) })
            : "";
        return platformT("platform.responses.unsupported", "未检测到 Responses API 支持{detail}", { detail });
    }
    return platformT("platform.responses.not_detected", "尚未检测。请先点击「检测支持情况」确认该平台是否支持 previous_response_id。");
}

function refreshResponsesApiSection(config: AIPlatformConfig): void {
    const id = platformModalKey(config);
    const status = document.getElementById(`modal-responses-status-${id}`);
    if (status) status.textContent = responsesApiStatusText(config);
    const toggle = document.getElementById(`modal-use-previous-response-${id}`) as HTMLInputElement | null;
    if (toggle) {
        toggle.disabled = config.config.responses_api_supported !== true;
        if (!config.config.responses_api_supported) toggle.checked = false;
    }
}

function buildModelList(config: AIPlatformConfig, isNew: boolean): string {
    const id = platformEscapeHtml(platformModalKey(config));
    const rows = config.models.map((model) => {
        const isDefault = config.config.default_model === model.id;
        const savedOnly = isNew ? "" : `
                            <button class="btn btn-sm btn-primary test-model-btn" data-model="${platformEscapeHtml(model.id)}">${platformEscapeHtml(platformT("platform.model.test", "测试连接"))}</button>
                            <button class="btn btn-sm btn-primary config-model-btn" data-model="${platformEscapeHtml(model.id)}">${platformEscapeHtml(platformT("platform.model.config", "配置"))}</button>`;
        return `
                    <div class="model-item" data-model-id="${platformEscapeHtml(model.id)}">
                        <div class="model-info">
                            <h7>${platformEscapeHtml(model.name)}</h7>
                            <p>${platformEscapeHtml(model.description || model.id)}</p>
                        </div>
                        <div class="model-actions">
                            <label class="model-default-option" title="${platformEscapeHtml(platformT("platform.model.default_hint", "设为平台默认模型"))}">
                                <input class="form-check-input model-default-radio" type="radio" name="default-model-${id}" value="${platformEscapeHtml(model.id)}" ${isDefault ? "checked" : ""}>
                                ${platformEscapeHtml(platformT("platform.model.default", "默认"))}
                            </label>
                            <div class="form-check form-switch">
                                <input class="form-check-input model-toggle-input" type="checkbox" ${model.enabled ? "checked" : ""} data-model="${platformEscapeHtml(model.id)}">
                            </div>${savedOnly}
                            <button class="btn btn-sm btn-danger remove-model-btn" data-model="${platformEscapeHtml(model.id)}">${platformEscapeHtml(platformT("platform.model.delete", "删除"))}</button>
                        </div>
                    </div>
                `;
    }).join("");

    return `
        <div class="models-section">
            <h6>
                ${platformEscapeHtml(platformT("platform.model.section_title", "模型管理"))}
                <span class="models-section-actions">
                    ${isNew ? "" : `<button class="btn btn-sm btn-outline-primary edit-request-template-btn" data-platform="${platformEscapeHtml(config.platform)}">${platformEscapeHtml(platformT("platform.model.edit_template", "编辑请求模板"))}</button>`}
                    <button class="btn btn-sm btn-primary add-model-btn" data-platform="${platformEscapeHtml(config.platform)}">${platformEscapeHtml(platformT("platform.model.add", "+ 添加模型"))}</button>
                </span>
            </h6>
            <div class="models-list" id="modal-models-list-${id}">
                ${rows || `<p class="text-muted">${platformEscapeHtml(platformT("platform.model.empty", "还没有模型，点右上角「添加模型」。"))}</p>`}
            </div>
        </div>
    `;
}

// 把弹窗当前输入回写到 draft，避免重渲染（增删模型/切换供应商）时丢掉用户已填内容。
function syncPlatformFormToConfig(config: AIPlatformConfig): void {
    const id = platformModalKey(config);
    const nameInput = document.getElementById(`modal-platform-name-${id}`) as HTMLInputElement | null;
    if (nameInput) config.name = nameInput.value.trim();
    const baseUrlInput = document.getElementById(`modal-base-url-${id}`) as HTMLInputElement | null;
    if (baseUrlInput) config.config.base_url = baseUrlInput.value.trim();
    const fullUrl = document.getElementById(`modal-full-url-${id}`) as HTMLInputElement | null;
    if (fullUrl) config.config.base_url_full = fullUrl.checked;
    const providerType = document.getElementById(`modal-provider-type-${id}`) as HTMLSelectElement | null;
    if (providerType) config.config.provider_type = providerType.value;
    const timeout = document.getElementById(`modal-timeout-${id}`) as HTMLInputElement | null;
    if (timeout) config.config.timeout = Number.parseInt(timeout.value, 10) || 30;
    const usePrev = document.getElementById(`modal-use-previous-response-${id}`) as HTMLInputElement | null;
    if (usePrev) config.config.use_previous_response_id = config.config.responses_api_supported === true && usePrev.checked;
}

function bindPasswordToggles(): void {
    document.querySelectorAll<HTMLElement>(".password-toggle").forEach((toggle) => {
        toggle.addEventListener("click", () => {
            const targetId = toggle.dataset.target || "";
            const input = document.getElementById(targetId) as HTMLInputElement | null;
            if (!input) return;
            input.type = input.type === "password" ? "text" : "password";
            const icon = toggle.querySelector("i");
            if (icon) icon.className = input.type === "password" ? "bi bi-eye" : "bi bi-eye-slash";
        });
    });
}

function bindTimeoutSlider(platformName: string): void {
    document.getElementById(`modal-timeout-${platformName}`)?.addEventListener("input", (event) => {
        const input = event.currentTarget as HTMLInputElement;
        const valueDisplay = document.getElementById(`modal-timeout-value-${platformName}`);
        if (valueDisplay) valueDisplay.textContent = platformT("platform.timeout.seconds", "{value} 秒", { value: input.value });
    });
}

function bindResponsesApiDetect(platform: AIPlatformConfig): void {
    const button = document.querySelector<HTMLButtonElement>(".detect-responses-btn");
    if (!button) return;
    button.addEventListener("click", async () => {
        button.disabled = true;
        const original = button.textContent;
        button.textContent = platformT("platform.responses.detecting", "检测中…");
        try {
            const result = await aiPlatformManager.detectResponsesApi(platform.platform);
            platform.config.responses_api_supported = result.supported;
            platform.config.responses_api_detail = result.detail;
            platform.config.responses_api_checked_at = new Date().toLocaleString();
            if (!result.supported) platform.config.use_previous_response_id = false;
            refreshResponsesApiSection(platform);
            showNotification(
                result.supported
                    ? platformT("platform.responses.detect.supported", "该平台支持 Responses API，可启用 previous_response_id。")
                    : platformT("platform.responses.detect.unsupported", "该平台暂不支持 Responses API。{detail}", { detail: result.detail ? ` ${result.detail}` : "" }),
                result.supported ? "success" : "error",
            );
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        } finally {
            button.disabled = false;
            button.textContent = original;
        }
    });
}

function bindPlatformModalFooter(config: AIPlatformConfig, isNew: boolean): void {
    const saveButton = document.getElementById("savePlatformConfigBtn") as HTMLButtonElement | null;
    if (saveButton) {
        saveButton.textContent = isNew ? platformT("platform.form.add", "添加") : platformT("platform.form.save", "保存");
        saveButton.onclick = () => void savePlatformModal(config, isNew, true);
    }
    const testButton = document.getElementById("testPlatformConnectionBtn") as HTMLButtonElement | null;
    if (testButton) testButton.onclick = () => void testPlatformFromModal(config, isNew);
}

// 保存弹窗草稿：回写表单、处理密钥、必要时生成平台 id，然后落盘并刷新卡片。
async function savePlatformModal(config: AIPlatformConfig, isNew: boolean, closeAfter: boolean): Promise<boolean> {
    const id = platformModalKey(config);
    const apiKeyInput = document.getElementById(`modal-api-key-${id}`) as HTMLInputElement | null;
    const apiKeyValue = apiKeyInput?.value.trim() || "";
    syncPlatformFormToConfig(config);
    if (apiKeyValue) {
        config.config.api_key = apiKeyValue;
    } else {
        delete config.config.api_key;
    }

    if (!config.name.trim()) {
        showNotification(platformT("platform.form.need_name", "请填写供应商名称"), "error");
        return false;
    }
    if (!config.config.base_url.trim()) {
        showNotification(platformT("platform.form.need_base_url", "请填写 Base URL"), "error");
        return false;
    }
    if (isNew) {
        const state = platformModalState;
        const existingIds = aiPlatformManager.getAllPlatforms().map((item) => item.platform);
        if (state?.savedId) {
            // 「测试连接」会先落盘一次草稿；再次保存必须沿用同一个 id，否则每次
            // 保存都会重新编号、凭空多出一个平台。
            config.platform = state.savedId;
        } else if (!config.platform) {
            config.platform = buildPlatformId(config.name, existingIds);
        } else if (existingIds.includes(config.platform)) {
            // 同一个内置供应商允许重复添加：选中的模板 id 已被占用时自动换用带序号的
            // id 与名称（如 deepseek → deepseek-2 / DeepSeek 2），避免保存到同名文件
            // 把先前添加的平台直接顶替掉。
            const platformId = buildPlatformId(config.platform, existingIds);
            const suffix = platformId.slice(config.platform.length + 1);
            config.platform = platformId;
            if (suffix) config.name = `${config.name} ${suffix}`;
        }
        if (state) state.savedId = config.platform;
    }

    const success = await aiPlatformManager.updatePlatformConfig(config.platform, config);
    if (!success) {
        showNotification(platformT("platform.form.save_failed", "保存平台失败"), "error");
        return false;
    }
    renderPlatforms(await aiPlatformManager.loadPlatforms());
    showNotification(
        isNew ? platformT("platform.form.added", "平台添加成功") : platformT("platform.form.saved", "平台配置已保存"),
        "success",
    );
    if (closeAfter) {
        bootstrap.Modal.getInstance(document.getElementById("platformConfigModal"))?.hide();
    } else if (platformModalKey(config) !== id) {
        // 保存时平台 id 变了（自动编号/改名）：同步重渲染弹窗，让内部元素 id 跟随新平台
        // id。否则「测试连接」之后再点保存会找不到 API Key 输入框，把已填的密钥清掉。
        renderPlatformModalBody(config, isNew);
    }
    return true;
}

// 弹窗内「测试连接」：沿用现有测试流程；新增平台尚未落盘时先保存再测试。
async function testPlatformFromModal(config: AIPlatformConfig, isNew: boolean): Promise<void> {
    if (isNew) {
        const saved = await savePlatformModal(config, isNew, false);
        if (!saved) return;
    }
    const modelId = resolveTestModelId(config.platform, config);
    if (!config.platform || !modelId) {
        showNotification(platformT("platform.form.need_model", "请先添加一个模型，或填写默认模型后再测试连接"), "error");
        return;
    }
    await testModelAPI(config.platform, modelId);
}

function bindPlatformVendorPicker(config: AIPlatformConfig): void {
    document.querySelectorAll<HTMLButtonElement>(".platform-vendor-option").forEach((chip) => {
        chip.addEventListener("click", () => applyVendorTemplate(config, chip.dataset.vendor || ""));
    });
}

// 选择内置供应商时预填名称/地址/标准/模型；选择「自定义」则清空这些字段（保留已填的 API Key）。
function applyVendorTemplate(config: AIPlatformConfig, vendorId: string): void {
    const template = aiPlatformManager.getBuiltinTemplates().find((item) => item.id === vendorId) || null;
    if (template) {
        config.platform = template.id;
        config.name = template.name;
        config.description = template.description;
        config.config.base_url = template.base_url;
        config.config.base_url_full = template.base_url_full;
        config.config.provider_type = template.provider_type;
        config.config.default_model = template.models[0]?.id || "";
        config.models = template.models.map((model) => ({
            id: model.id,
            name: model.name,
            description: model.description || "",
            enabled: true,
            params: {},
        }));
        if (!platformModalState?.iconUploaded) config.icon = template.icon;
    } else {
        config.platform = "";
        config.name = "";
        config.description = "";
        config.icon = "";
        config.config.base_url = "";
        config.config.base_url_full = false;
        config.config.provider_type = "openai-compatible";
        config.config.default_model = "";
        config.models = [];
    }
    // 切换供应商不应覆盖用户已输入的 API Key，渲染时会从 state.apiKey 恢复。
    renderPlatformModalBody(config, platformModalState?.isNew ?? false);
}

function bindPlatformIconUpload(): void {
    const state = platformModalState;
    if (!state) return;
    const input = document.getElementById(`modal-icon-upload-${platformModalKey(state.config)}`) as HTMLInputElement | null;
    if (!input) return;
    input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (!file) return;
        if (!file.type.startsWith("image/")) {
            showNotification(platformT("platform.form.icon_invalid", "请上传图片文件"), "error");
            return;
        }
        if (file.size > PLATFORM_ICON_MAX_BYTES) {
            showNotification(platformT("platform.form.icon_too_large", "头像文件不能超过 1MB"), "error");
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            state.config.icon = String(reader.result || "");
            state.iconUploaded = true;
            const preview = document.getElementById(`modal-avatar-preview-${platformModalKey(state.config)}`);
            if (preview) preview.innerHTML = platformAvatarHTML(state.config, "platform-vendor-preview-img");
        };
        reader.readAsDataURL(file);
    });
}

function bindPlatformFormFields(): void {
    const state = platformModalState;
    if (!state) return;
    const config = state.config;
    const id = platformModalKey(config);
    const nameInput = document.getElementById(`modal-platform-name-${id}`) as HTMLInputElement | null;
    nameInput?.addEventListener("input", () => {
        // 未上传/未使用内置头像时，名称首字即头像内容。
        if ((config.icon || "").trim()) return;
        const preview = document.getElementById(`modal-avatar-preview-${id}`);
        if (preview) preview.innerHTML = platformAvatarHTML({ icon: "", name: nameInput.value }, "platform-vendor-preview-img");
    });
    const apiKeyInput = document.getElementById(`modal-api-key-${id}`) as HTMLInputElement | null;
    apiKeyInput?.addEventListener("input", () => {
        state.apiKey = apiKeyInput.value;
    });
}

function createCustomPlatformDraft(): AIPlatformConfig {
    return {
        platform: "",
        name: "",
        description: "",
        icon: "",
        enabled: true,
        config: {
            base_url: "",
            timeout: 30,
            provider_type: "openai-compatible",
            base_url_full: false,
            default_model: "",
            use_previous_response_id: false,
        },
        models: [],
    };
}

function bindAddPlatformEvents(): void {
    document.getElementById("addPlatformBtn")?.addEventListener("click", () => {
        void openPlatformConfigModal(createCustomPlatformDraft(), true);
    });
}

function bindModelEvents(config: AIPlatformConfig, isNew: boolean): void {
    document.querySelectorAll<HTMLButtonElement>(".remove-model-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            const modelId = button.dataset.model || "";
            if (!modelId || !confirm(platformT("platform.model.delete_confirm", "确定要删除这个模型吗？"))) return;
            if (isNew) {
                syncPlatformFormToConfig(config);
                config.models = config.models.filter((model) => model.id !== modelId);
                if (config.config.default_model === modelId) {
                    config.config.default_model = config.models.find((model) => model.enabled)?.id || config.models[0]?.id || "";
                }
                renderPlatformModalBody(config, isNew);
                return;
            }
            if (await aiPlatformManager.removeModel(config.platform, modelId)) {
                const refreshed = aiPlatformManager.getPlatform(config.platform);
                if (refreshed && platformModalState) platformModalState.config = refreshed;
                renderPlatformModalBody(refreshed || config, isNew);
            }
        });
    });

    document.querySelectorAll<HTMLInputElement>(".model-default-radio").forEach((radio) => {
        radio.addEventListener("change", () => {
            if (radio.checked) config.config.default_model = radio.value;
        });
    });

    document.querySelectorAll<HTMLInputElement>(".model-toggle-input").forEach((input) => {
        input.addEventListener("change", () => {
            const model = config.models.find((item) => item.id === input.dataset.model);
            if (model) model.enabled = input.checked;
        });
    });

    document.querySelectorAll<HTMLButtonElement>(".test-model-btn").forEach((button) => {
        button.addEventListener("click", () => {
            const modelId = button.dataset.model || config.config.default_model || config.models[0]?.id || "";
            if (modelId) void testModelAPI(config.platform, modelId);
        });
    });

    document.querySelectorAll<HTMLButtonElement>(".config-model-btn").forEach((button) => {
        button.addEventListener("click", () => {
            const modelId = button.dataset.model || "";
            if (modelId) void configModel(config.platform, modelId);
        });
    });

    // 平台级「编辑请求模板」：打开该平台首个可用模型的请求 JSON，供用户自行改写请求写法。
    document.querySelectorAll<HTMLButtonElement>(".edit-request-template-btn").forEach((button) => {
        button.addEventListener("click", () => {
            const target = config.models.find((model) => model.enabled) || config.models[0];
            if (!target) {
                showNotification(platformT("platform.model.need_model_for_template", "请先添加一个模型，再编辑请求模板"), "error");
                return;
            }
            void configModel(config.platform, target.id);
        });
    });
}

function bindAddModelEvents(): void {
    document.addEventListener("click", (event) => {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".add-model-btn");
        if (!button) return;
        window.currentPlatform = button.dataset.platform || "";
        setFormValue("modelName", "");
        setFormValue("modelId", "");
        setFormValue("modelDescription", "");
        setDisabled("addModelBtn", true);
        const modalElement = document.getElementById("addModelModal");
        if (modalElement) new bootstrap.Modal(modalElement).show();
    });

    ["modelName", "modelId"].forEach((id) => {
        document.getElementById(id)?.addEventListener("input", validateAddModelForm);
    });

    document.getElementById("addModelBtn")?.addEventListener("click", async () => {
        const modelName = formValue("modelName").trim();
        const modelId = formValue("modelId").trim();
        const description = formValue("modelDescription").trim();
        if (!modelName || !modelId) return;

        // 新增平台尚未落盘：模型先加到弹窗草稿，等保存平台时一起写入。
        const state = platformModalState;
        if (state && state.isNew) {
            if (state.config.models.some((item) => item.id === modelId)) {
                showNotification(platformT("platform.model.duplicate", "该模型已存在"), "error");
                return;
            }
            syncPlatformFormToConfig(state.config);
            state.config.models.push({ id: modelId, name: modelName, description, enabled: true, params: {} });
            if (!state.config.config.default_model) state.config.config.default_model = modelId;
            bootstrap.Modal.getInstance(document.getElementById("addModelModal"))?.hide();
            renderPlatformModalBody(state.config, true);
            showNotification(platformT("platform.model.added", "模型添加成功"), "success");
            return;
        }

        const platform = window.currentPlatform || "";
        if (!platform) return;
        const success = await aiPlatformManager.addModel(platform, { id: modelId, name: modelName, description });
        if (success) {
            bootstrap.Modal.getInstance(document.getElementById("addModelModal"))?.hide();
            const refreshed = aiPlatformManager.getPlatform(platform);
            if (refreshed && platformModalState && platformModalState.config.platform === platform) {
                platformModalState.config = refreshed;
                renderPlatformModalBody(refreshed, false);
            } else {
                renderPlatforms(await aiPlatformManager.loadPlatforms());
            }
            showNotification(platformT("platform.model.added", "模型添加成功"), "success");
        } else {
            showNotification(platformT("platform.model.add_failed", "模型添加失败"), "error");
        }
    });
}

function validateAddModelForm(): void {
    setDisabled("addModelBtn", !formValue("modelName").trim() || !formValue("modelId").trim());
}

function bindAPITestEvents(): void {
    document.addEventListener("click", (event) => {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".test-api-btn");
        if (!button) return;
        const platform = button.dataset.platform || "";
        if (platform) void testAPI(platform);
    });

    document.getElementById("reTestBtn")?.addEventListener("click", () => {
        const platform = window.currentTestingPlatform || "";
        if (platform) void testAPI(platform);
    });
}

function resetTestModal(): void {
    toggleClass("testLoading", "d-none", false);
    toggleClass("testStatus", "d-none", true);
    toggleClass("testResult", "d-none", true);
    toggleClass("testError", "d-none", true);
    toggleClass("testDetails", "d-none", true);
    const reTestBtn = document.getElementById("reTestBtn") as HTMLElement | null;
    if (reTestBtn) reTestBtn.style.display = "none";
    const testDetails = document.getElementById("testDetails");
    if (testDetails) testDetails.innerHTML = "";
}

async function testAPI(platform: string): Promise<void> {
    const platformConfig = aiPlatformManager.getPlatform(platform);
    if (!platformConfig) return;
    const modelId = resolveTestModelId(platform, platformConfig);
    if (!modelId) {
        // 自定义平台可能还没添加任何模型：给出明确反馈，而不是像旧实现那样「点了没反应」。
        showNotification(platformT("platform.test.need_model", "请先为该平台添加一个模型，或填写默认模型后再测试连接"), "error");
        return;
    }
    await testModelAPI(platform, modelId);
}

// 测试连接时选用的模型 id：LM Studio 用占位 id；其余优先已启用模型、其次第一个模型，
// 再回退到配置里的默认模型，从而让「尚未添加模型但填了默认模型」的自定义平台也能测试。
function resolveTestModelId(platform: string, config: AIPlatformConfig): string {
    if (platform === "lmstudio") return LMSTUDIO_DEFAULT_MODEL_ID;
    const model = config.models.find((item) => item.enabled) || config.models[0];
    if (model?.id) return model.id;
    return (config.config.default_model || "").trim();
}

async function testModelAPI(platform: string, modelId: string): Promise<void> {
    window.currentTestingPlatform = platform;
    const modalElement = document.getElementById("apiTestModal");
    if (modalElement) new bootstrap.Modal(modalElement, { backdrop: false }).show();
    resetTestModal();

    const result = await aiPlatformManager.testAPI(platform, modelId);
    toggleClass("testLoading", "d-none", true);
    const reTestBtn = document.getElementById("reTestBtn") as HTMLElement | null;
    if (reTestBtn) reTestBtn.style.display = "inline-block";

    if (result.success) {
        toggleClass("testResult", "d-none", false);
        toggleClass("testDetails", "d-none", false);
        fillTestDetails(result);
    } else {
        toggleClass("testError", "d-none", false);
        const errorMessage = document.getElementById("errorMessage");
        if (errorMessage) errorMessage.textContent = result.error || platformT("platform.test.failed", "测试失败");
    }
}

function fillTestDetails(result: AITestResult): void {
    const testDetails = document.getElementById("testDetails");
    if (!testDetails) return;
    testDetails.innerHTML = `
        <h6>${platformEscapeHtml(platformT("platform.test.details", "测试详情"))}</h6>
        <p><strong>${platformEscapeHtml(platformT("platform.test.time", "测试时间："))}</strong>${platformEscapeHtml(result.time || "-")}</p>
        <p><strong>${platformEscapeHtml(platformT("platform.test.model", "模型："))}</strong>${platformEscapeHtml(result.model || "-")}</p>
        <p><strong>${platformEscapeHtml(platformT("platform.test.speed", "平均速度："))}</strong>${platformEscapeHtml(result.speed || "-")}</p>
        <p><strong>${platformEscapeHtml(platformT("platform.test.consumption", "消耗："))}</strong>${platformEscapeHtml(result.consumption || "-")}</p>
    `;
}

async function configModel(platform: string, modelId: string): Promise<void> {
    const platformConfig = aiPlatformManager.getPlatform(platform);
    const model = platformConfig?.models.find((item) => item.id === modelId);
    if (!platformConfig || !model) return;

    const modelRequestConfig = await loadModelRequestConfig(platform, modelId);
    const modalElement = document.createElement("div");
    modalElement.className = "modal fade";
    modalElement.id = "modelConfigModal";
    modalElement.tabIndex = -1;
    modalElement.innerHTML = `
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">${platformEscapeHtml(platformT("platform.model.config_title", "{name} 配置", { name: model.name }))}</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <textarea class="form-control" id="modelRequestConfig" rows="20">${platformEscapeHtml(modelRequestConfig)}</textarea>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${platformEscapeHtml(platformT("common.close", "关闭"))}</button>
                    <button type="button" class="btn btn-primary" id="saveModelConfigBtn">${platformEscapeHtml(platformT("common.save", "保存"))}</button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modalElement);
    const modal = new bootstrap.Modal(modalElement);
    modal.show();

    document.getElementById("saveModelConfigBtn")?.addEventListener("click", async () => {
        try {
            const textarea = document.getElementById("modelRequestConfig") as HTMLTextAreaElement | null;
            const requestConfig = JSON.parse(textarea?.value || "{}") as unknown;
            const { response } = await TrpgApi.requestWithResponse<ApiResponse>("/api/config/aimodel/save", {
                method: "POST",
                body: { platform, modelId, content: requestConfig },
            });
            if (!response.ok) throw new Error(platformT("platform.model.json_save_failed", "保存 JSON 配置失败"));
            modal.hide();
            showNotification(platformT("platform.model.json_saved", "JSON 配置保存成功"), "success");
        } catch (error) {
            showNotification(platformT("platform.model.json_save_failed_detail", "保存 JSON 配置失败: {detail}", { detail: platformErrorMessage(error) }), "error");
        }
    });

    modalElement.addEventListener("hidden.bs.modal", () => {
        setTimeout(() => modalElement.remove(), 100);
    });
}

async function loadModelRequestConfig(platform: string, modelId: string): Promise<string> {
    try {
        const response = await fetch(`config/aimodel/${platform}/${modelId}.json`);
        if (response.ok) return JSON.stringify(await response.json(), null, 2);
        const fallback = await fetch(DEFAULT_REQUEST_CONFIG_PATH);
        if (fallback.ok) {
            const config = await fallback.json() as Record<string, unknown>;
            config.model = modelId;
            return JSON.stringify(config, null, 2);
        }
    } catch (error) {
        console.error("加载模型请求配置失败:", error);
    }
    return "{}";
}

function showNotification(message: string, type = "info"): void {
    const container = document.querySelector(".notification-container");
    if (!container) return;
    const notification = document.createElement("div");
    notification.className = `notification notification-${type}`;
    notification.textContent = message;
    container.appendChild(notification);
    setTimeout(() => notification.remove(), 3000);
}

function formValue(id: string): string {
    return (document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null)?.value || "";
}

function setFormValue(id: string, value: string): void {
    const input = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
    if (input) input.value = value;
}

function setDisabled(id: string, disabled: boolean): void {
    const button = document.getElementById(id) as HTMLButtonElement | null;
    if (button) button.disabled = disabled;
}

function toggleClass(id: string, className: string, force: boolean): void {
    document.getElementById(id)?.classList.toggle(className, force);
}

function platformEscapeHtml(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function platformErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}



