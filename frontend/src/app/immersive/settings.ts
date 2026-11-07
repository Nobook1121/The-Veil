/**
 * 沉浸系统设置页（管理员）：生图平台表、AI 音乐导演、密钥与用量。
 *
 * - 「生图模型平台配置」是外部生图的唯一入口：多条目、默认平台、预设、连接测试、
 *   密钥只回掩码；普通开关仍走「常规设置」的批量保存条。
 * - 「AI 音乐导演」只下发配置（开关/平台/模型/频率/预算/缓存）；规则分析器始终
 *   先生效，导演失败自动回退，这里不提供任何「立即生成音乐」的阻塞入口。
 */
namespace TrpgImmersiveSettingsModule {
    let initialized = false;
    let usageLoading = false;
    let metaLoading = false;
    let platforms: ImmersiveImagePlatform[] = [];
    let presets: ImmersiveImagePreset[] = [];
    let protocols: ImmersiveProtocolOption[] = [];
    let selectedPlatformId = "";
    let audioBlockOptions: ImmersiveAudioBlockOption[] = [];

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function escapeHtml(text: unknown): string {
        return String(text ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function setMessage(text: string, level: "info" | "error" | "success" = "info"): void {
        const target = byId<HTMLElement>("immersiveSettingsMessage");
        if (!target) return;
        target.textContent = text;
        target.dataset.level = level;
    }

    function setPlatformMessage(text: string, level: "info" | "error" | "success" = "info"): void {
        const target = byId<HTMLElement>("immersivePlatformMessage");
        if (!target) return;
        target.textContent = text;
        target.dataset.level = level;
    }

    export function init(): void {
        if (initialized) return;
        const provider = byId<HTMLSelectElement>("immersiveIllustrationProvider");
        if (!provider) return;
        initialized = true;
        byId<HTMLButtonElement>("immersiveRefreshUsage")?.addEventListener("click", () => {
            void refreshUsage();
        });
        byId<HTMLButtonElement>("immersiveTestProvider")?.addEventListener("click", () => {
            void testDefaultPlatform();
        });
        byId<HTMLButtonElement>("immersiveDirectorRefresh")?.addEventListener("click", () => {
            void refreshDirectorStatus();
        });
        byId<HTMLButtonElement>("immersiveDirectorClearCache")?.addEventListener("click", () => {
            void clearDirectorCache();
        });

        byId<HTMLButtonElement>("immersivePlatformRefresh")?.addEventListener("click", () => {
            void refreshPlatforms();
        });
        byId<HTMLButtonElement>("immersivePlatformNew")?.addEventListener("click", () => {
            newPlatform();
        });
        byId<HTMLSelectElement>("immersivePlatformPreset")?.addEventListener("change", () => {
            applyPreset();
        });
        byId<HTMLButtonElement>("immersivePlatformSave")?.addEventListener("click", () => {
            void savePlatform();
        });
        byId<HTMLButtonElement>("immersivePlatformSaveKey")?.addEventListener("click", () => {
            void savePlatformKey();
        });
        byId<HTMLButtonElement>("immersivePlatformClearKey")?.addEventListener("click", () => {
            void clearPlatformKey();
        });
        byId<HTMLButtonElement>("immersivePlatformTest")?.addEventListener("click", () => {
            void testPlatform();
        });
        byId<HTMLButtonElement>("immersivePlatformDefault")?.addEventListener("click", () => {
            void setDefaultPlatform();
        });
        byId<HTMLButtonElement>("immersivePlatformDelete")?.addEventListener("click", () => {
            void deletePlatform();
        });
        byId<HTMLElement>("immersivePlatformList")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const button = target?.closest<HTMLElement>("[data-platform-id]");
            const platformId = button?.dataset.platformId || "";
            if (!platformId) return;
            if (target?.closest("[data-platform-action='default']")) {
                selectedPlatformId = platformId;
                void setDefaultPlatform(platformId);
                return;
            }
            fillForm(findPlatform(platformId));
        });
        // ConfigManager 回填隐藏输入后，重新渲染块选择器（配置加载晚于标签页初始化）。
        (window as unknown as Record<string, unknown>).refreshImmersiveAudioBlocksPicker = () => {
            renderAdminAudioBlockPicker();
        };
    }

    function renderAdminAudioBlockPicker(): void {
        const hidden = byId<HTMLInputElement>("immersiveAudioBlocksAllowed");
        if (!hidden || !byId<HTMLElement>("adminAudioBlocksPicker")) return;
        const selected = (hidden.value || "")
            .split(",")
            .map((item) => item.trim())
            .filter((item) => item);
        TrpgAudioBlocks.render("adminAudioBlocksPicker", audioBlockOptions, selected, {
            mode: "allow",
            onChange: (blocks) => {
                hidden.value = blocks.join(",");
                hidden.dispatchEvent(new Event("change", { bubbles: true }));
            },
        });
    }

    export function render(): void {
        init();
        void refreshMeta();
    }

    /** 「模型设置」标签页：生图平台的唯一管理入口，只加载平台表与预设。 */
    export function renderModelSettings(): void {
        init();
        void refreshPlatformMeta();
    }

    async function refreshMeta(): Promise<void> {
        await refreshPlatformMeta();
        void refreshUsage();
        void refreshDirectorStatus();
    }

    async function refreshPlatformMeta(): Promise<void> {
        if (metaLoading) return;
        metaLoading = true;
        try {
            const response = await TrpgApi.get<ApiResponse<ImmersivePlatformPayload & {
                config?: ImmersiveSettings;
            }>>("/api/immersive/config");
            const data = response.data;
            if (!response.success || !data) return;
            if (data.audio_blocks && Array.isArray(data.audio_blocks.options)) {
                audioBlockOptions = data.audio_blocks.options;
                const hidden = byId<HTMLInputElement>("immersiveAudioBlocksAllowed");
                if (hidden && !hidden.value.trim() && Array.isArray(data.audio_blocks.allowed)) {
                    hidden.value = data.audio_blocks.allowed.join(",");
                }
                renderAdminAudioBlockPicker();
            }
            presets = Array.isArray(data.presets) ? data.presets : [];
            protocols = Array.isArray(data.protocols) ? data.protocols : [];
            platforms = Array.isArray(data.platforms) ? data.platforms : [];
            renderPresets();
            renderProtocols();
            renderPlatformList();
            if (!selectedPlatformId && platforms.length) {
                const preferred = platforms.find((item) => item.is_default) || platforms[0];
                fillForm(preferred || null);
            }
        } catch {
            // 未登录或网络异常时保持原状态，不打断设置页。
        } finally {
            metaLoading = false;
        }
    }

    async function refreshPlatforms(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<ImmersivePlatformPayload>>("/api/immersive/platforms");
            if (!response.success || !response.data) {
                setPlatformMessage(response.message || response.error || "平台表加载失败", "error");
                return;
            }
            presets = response.data.presets || [];
            protocols = response.data.protocols || [];
            platforms = response.data.platforms || [];
            renderPresets();
            renderProtocols();
            renderPlatformList();
        } catch (error) {
            setPlatformMessage(error instanceof Error ? error.message : "平台表加载失败", "error");
        }
    }

    function renderPresets(): void {
        const select = byId<HTMLSelectElement>("immersivePlatformPreset");
        if (!select) return;
        const options = ['<option value="">选择预设以填充表单…</option>'];
        for (const preset of presets) {
            options.push(`<option value="${escapeHtml(preset.preset)}">${escapeHtml(preset.name)}</option>`);
        }
        select.innerHTML = options.join("");
    }

    function renderProtocols(): void {
        const select = byId<HTMLSelectElement>("immersivePlatformProtocol");
        if (!select) return;
        const options = protocols.length
            ? protocols
            : [{ value: "openai_images", label: "OpenAI 兼容 /v1/images/generations" }];
        select.innerHTML = options
            .map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`)
            .join("");
    }

    function findPlatform(platformId: string): ImmersiveImagePlatform | null {
        return platforms.find((item) => item.id === platformId) || null;
    }

    function renderPlatformList(): void {
        const list = byId<HTMLElement>("immersivePlatformList");
        if (!list) return;
        if (!platforms.length) {
            list.innerHTML = `<p class="text-muted small mb-0">尚未创建平台。选择上方预设后点「新增平台」，或直接填写表单保存。</p>`;
            return;
        }
        list.innerHTML = platforms
            .map((platform) => {
                const badges = [
                    platform.is_default ? '<span class="badge text-bg-success">默认</span>' : "",
                    platform.enabled ? "" : '<span class="badge text-bg-secondary">停用</span>',
                    platform.has_api_key ? "" : '<span class="badge text-bg-warning">无密钥</span>',
                ].join(" ");
                const active = platform.id === selectedPlatformId ? " is-active" : "";
                return `
                    <div class="immersive-platform-card${active}" data-platform-id="${escapeHtml(platform.id)}">
                        <div class="immersive-platform-card-main">
                            <div>
                                <strong>${escapeHtml(platform.name || platform.id)}</strong>
                                <span class="text-muted small ms-2">${escapeHtml(platform.id)}</span>
                                ${badges}
                            </div>
                            <div class="text-muted small">
                                ${escapeHtml(platform.model || "-")} · ${escapeHtml(platform.size || "-")} ·
                                ${escapeHtml(platform.base_url || "-")}
                                ${platform.has_api_key ? ` · 密钥 ${escapeHtml(platform.api_key_masked || "已配置")}` : ""}
                            </div>
                        </div>
                        <div class="d-flex gap-2">
                            ${platform.is_default ? "" : `<button type="button" class="btn btn-outline-success btn-sm" data-platform-action="default">设为默认</button>`}
                            <button type="button" class="btn btn-outline-secondary btn-sm">编辑</button>
                        </div>
                    </div>`;
            })
            .join("");
    }

    function fillForm(platform: ImmersiveImagePlatform | null): void {
        selectedPlatformId = platform?.id || "";
        const title = byId<HTMLElement>("immersivePlatformEditorTitle");
        if (title) title.textContent = platform ? `编辑平台：${platform.name || platform.id}` : "新增平台";
        const idInput = byId<HTMLInputElement>("immersivePlatformId");
        if (idInput) idInput.value = platform?.id || "";
        const name = byId<HTMLInputElement>("immersivePlatformName");
        if (name) name.value = platform?.name || "";
        const protocol = byId<HTMLSelectElement>("immersivePlatformProtocol");
        if (protocol) protocol.value = platform?.protocol || "openai_images";
        const baseUrl = byId<HTMLInputElement>("immersivePlatformBaseUrl");
        if (baseUrl) baseUrl.value = platform?.base_url || "";
        const model = byId<HTMLInputElement>("immersivePlatformModel");
        if (model) model.value = platform?.model || "";
        const size = byId<HTMLInputElement>("immersivePlatformSize");
        if (size) size.value = platform?.size || "1024x1024";
        const timeout = byId<HTMLInputElement>("immersivePlatformTimeout");
        if (timeout) timeout.value = String(platform?.timeout || 120);
        const enabled = byId<HTMLInputElement>("immersivePlatformEnabled");
        if (enabled) enabled.checked = platform ? platform.enabled !== false : true;
        const key = byId<HTMLInputElement>("immersivePlatformKey");
        if (key) key.value = "";
        updateKeyStatus(platform);
        renderPlatformList();
    }

    function updateKeyStatus(platform: ImmersiveImagePlatform | null): void {
        const status = byId<HTMLElement>("immersivePlatformKeyStatus");
        if (!status) return;
        if (!platform) {
            status.textContent = "新增平台时可直接填写密钥；保存后只回掩码。";
            status.dataset.level = "info";
            return;
        }
        status.textContent = platform.has_api_key
            ? `已配置密钥（${platform.api_key_masked || "已配置"}），输入新值可轮换`
            : "未配置密钥";
        status.dataset.level = platform.has_api_key ? "success" : "warning";
    }

    function newPlatform(): void {
        fillForm(null);
        byId<HTMLInputElement>("immersivePlatformName")?.focus();
    }

    function applyPreset(): void {
        const select = byId<HTMLSelectElement>("immersivePlatformPreset");
        const preset = presets.find((item) => item.preset === (select?.value || ""));
        if (!preset) return;
        fillForm(null);
        const name = byId<HTMLInputElement>("immersivePlatformName");
        if (name) name.value = preset.name;
        const protocol = byId<HTMLSelectElement>("immersivePlatformProtocol");
        if (protocol) protocol.value = preset.protocol;
        const baseUrl = byId<HTMLInputElement>("immersivePlatformBaseUrl");
        if (baseUrl) baseUrl.value = preset.base_url;
        const model = byId<HTMLInputElement>("immersivePlatformModel");
        if (model) model.value = preset.model;
        const size = byId<HTMLInputElement>("immersivePlatformSize");
        if (size) size.value = preset.size;
        const timeout = byId<HTMLInputElement>("immersivePlatformTimeout");
        if (timeout) timeout.value = String(preset.timeout);
    }

    function collectPlatformPayload(): Record<string, unknown> {
        const apiKey = byId<HTMLInputElement>("immersivePlatformKey")?.value.trim() || "";
        const payload: Record<string, unknown> = {
            id: byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "",
            name: byId<HTMLInputElement>("immersivePlatformName")?.value.trim() || "",
            protocol: byId<HTMLSelectElement>("immersivePlatformProtocol")?.value || "openai_images",
            base_url: byId<HTMLInputElement>("immersivePlatformBaseUrl")?.value.trim() || "",
            model: byId<HTMLInputElement>("immersivePlatformModel")?.value.trim() || "",
            size: byId<HTMLInputElement>("immersivePlatformSize")?.value.trim() || "1024x1024",
            timeout: Number.parseInt(byId<HTMLInputElement>("immersivePlatformTimeout")?.value || "120", 10) || 120,
            enabled: Boolean(byId<HTMLInputElement>("immersivePlatformEnabled")?.checked),
        };
        if (apiKey) payload.api_key = apiKey;
        return payload;
    }

    async function savePlatform(): Promise<void> {
        setPlatformMessage("正在保存平台…", "info");
        try {
            const response = await TrpgApi.post<ApiResponse<{
                platform?: ImmersiveImagePlatform;
                platforms?: ImmersiveImagePlatform[];
            }>>("/api/immersive/platforms", collectPlatformPayload());
            if (!response.success || !response.data?.platform) {
                setPlatformMessage(response.message || response.error || "保存平台失败", "error");
                window.showNotification?.(response.message || response.error || "保存平台失败", "error");
                return;
            }
            platforms = response.data.platforms || platforms;
            fillForm(response.data.platform);
            setPlatformMessage("平台已保存（密钥只回掩码）", "success");
            window.showNotification?.("生图平台已保存", "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : "保存平台失败";
            setPlatformMessage(message, "error");
        }
    }

    async function savePlatformKey(): Promise<void> {
        const platformId = byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "";
        const value = byId<HTMLInputElement>("immersivePlatformKey")?.value.trim() || "";
        if (!platformId) {
            setPlatformMessage("请先保存平台条目，再单独轮换密钥", "error");
            return;
        }
        if (!value) {
            setPlatformMessage("请先输入新的 API Key", "error");
            return;
        }
        try {
            const response = await TrpgApi.post<ApiResponse<{ platform?: ImmersiveImagePlatform; platforms?: ImmersiveImagePlatform[] }>>(
                `/api/immersive/platforms/${encodeURIComponent(platformId)}/secret`,
                { api_key: value },
            );
            if (!response.success || !response.data?.platform) {
                setPlatformMessage(response.message || response.error || "保存密钥失败", "error");
                return;
            }
            platforms = response.data.platforms || platforms;
            const key = byId<HTMLInputElement>("immersivePlatformKey");
            if (key) key.value = "";
            fillForm(response.data.platform);
            setPlatformMessage("密钥已保存（仅写入运行时目录，不回显）", "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : "保存密钥失败";
            setPlatformMessage(message, "error");
        }
    }

    async function clearPlatformKey(): Promise<void> {
        const platformId = byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "";
        if (!platformId) {
            setPlatformMessage("请先选择一条平台", "error");
            return;
        }
        const confirmed = await window.TrpgDialog?.confirm({
            title: "清除平台密钥",
            message: "清除后该平台的外部生图将立即不可用，配图会回落到程序化场景图。确定继续？",
            confirmText: "清除",
            danger: true,
        });
        if (!confirmed) return;
        try {
            const response = await TrpgApi.post<ApiResponse<{ platform?: ImmersiveImagePlatform; platforms?: ImmersiveImagePlatform[] }>>(
                `/api/immersive/platforms/${encodeURIComponent(platformId)}/secret`,
                { clear: true },
            );
            if (!response.success || !response.data?.platform) {
                setPlatformMessage(response.message || response.error || "清除密钥失败", "error");
                return;
            }
            platforms = response.data.platforms || platforms;
            fillForm(response.data.platform);
            setPlatformMessage("密钥已清除", "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : "清除密钥失败";
            setPlatformMessage(message, "error");
        }
    }

    async function testPlatform(): Promise<void> {
        const platformId = byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "";
        if (!platformId) {
            setPlatformMessage("请先保存平台条目再测试连接", "error");
            return;
        }
        setPlatformMessage("正在测试连接（优先列出模型，必要时做一次最小生图探测）…", "info");
        try {
            const response = await TrpgApi.post<ApiResponse<{
                ok?: boolean;
                method?: string;
                models?: string[];
                elapsed_ms?: number;
                error?: string;
            }>>(`/api/immersive/platforms/${encodeURIComponent(platformId)}/test`, { allow_generation: true });
            const result = response.data;
            if (!response.success || !result) {
                setPlatformMessage(response.message || response.error || "连接测试失败", "error");
                return;
            }
            if (result.ok) {
                const models = Array.isArray(result.models) && result.models.length
                    ? `，可用模型：${result.models.slice(0, 8).join("、")}`
                    : "";
                setPlatformMessage(`连接成功（${result.method || "-"}，${result.elapsed_ms ?? 0}ms${models}）`, "success");
            } else {
                setPlatformMessage(`连接失败：${result.error || "未知原因"}`, "error");
            }
        } catch (error) {
            setPlatformMessage(error instanceof Error ? error.message : "连接测试失败", "error");
        }
    }

    async function setDefaultPlatform(platformId?: string): Promise<void> {
        const targetId = platformId || byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "";
        if (!targetId) {
            setPlatformMessage("请先选择一条平台", "error");
            return;
        }
        try {
            const response = await TrpgApi.post<ApiResponse<{
                default_platform_id?: string;
                platforms?: ImmersiveImagePlatform[];
            }>>(`/api/immersive/platforms/${encodeURIComponent(targetId)}/default`, {});
            if (!response.success) {
                setPlatformMessage(response.message || response.error || "设为默认失败", "error");
                return;
            }
            platforms = response.data?.platforms || platforms;
            const provider = byId<HTMLSelectElement>("immersiveIllustrationProvider");
            if (provider) {
                provider.value = "platform";
                provider.dispatchEvent(new Event("change"));
            }
            const current = findPlatform(targetId);
            fillForm(current);
            setPlatformMessage("已设为默认平台，并把配图来源切换为外部平台", "success");
            window.showNotification?.("已设为默认生图平台", "success");
        } catch (error) {
            setPlatformMessage(error instanceof Error ? error.message : "设为默认失败", "error");
        }
    }

    async function deletePlatform(): Promise<void> {
        const platformId = byId<HTMLInputElement>("immersivePlatformId")?.value.trim() || "";
        if (!platformId) {
            setPlatformMessage("请先选择一条平台", "error");
            return;
        }
        const platform = findPlatform(platformId);
        const confirmed = await window.TrpgDialog?.confirm({
            title: "删除生图平台",
            message: `确定删除「${platform?.name || platformId}」？该平台的密钥会一并移除，删除默认平台后配图会回落到程序化 SVG。`,
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;
        try {
            const response = await TrpgApi.del<ApiResponse<{ platforms?: ImmersiveImagePlatform[] }>>(
                `/api/immersive/platforms/${encodeURIComponent(platformId)}`,
            );
            if (!response.success) {
                setPlatformMessage(response.message || response.error || "删除平台失败", "error");
                return;
            }
            platforms = response.data?.platforms || [];
            selectedPlatformId = "";
            fillForm(platforms.find((item) => item.is_default) || platforms[0] || null);
            setPlatformMessage("平台已删除", "success");
        } catch (error) {
            setPlatformMessage(error instanceof Error ? error.message : "删除平台失败", "error");
        }
    }

    async function testDefaultPlatform(): Promise<void> {
        setMessage("正在检查默认平台…", "info");
        try {
            const response = await TrpgApi.post<ApiResponse<{
                provider?: string;
                ready?: boolean;
                problems?: string[];
                ok?: boolean;
                method?: string;
                error?: string;
                message?: string;
            }>>("/api/immersive/test", {});
            const data = response.data;
            if (!response.success || !data) {
                setMessage(response.message || response.error || "检查失败", "error");
                return;
            }
            const ok = data.ready ?? data.ok;
            if (ok) {
                setMessage(`默认平台可用${data.method ? `（${data.method}）` : ""}`, "success");
            } else {
                const problems = data.problems || (data.error ? [data.error] : []);
                setMessage(`默认平台不可用：${problems.join("；") || "未知原因"}`, "error");
            }
        } catch (error) {
            setMessage(error instanceof Error ? error.message : "检查失败", "error");
        }
    }

    async function refreshDirectorStatus(): Promise<void> {
        const output = byId<HTMLElement>("immersiveDirectorStatus");
        try {
            const response = await TrpgApi.get<ApiResponse<{
                usage?: ImmersiveDirectorUsage;
                cache?: ImmersiveDirectorCacheStats;
                fallbacks?: string[];
                audit?: Array<Record<string, unknown>>;
            }>>("/api/immersive/director");
            if (!response.success || !response.data) return;
            const data = response.data;
            if (!output) return;
            const calls = Number(data.usage?.calls ?? 0);
            const entries = Number(data.cache?.entries ?? 0);
            const ttl = Number(data.cache?.expires_after_seconds ?? 0);
            const fallbacks = Array.isArray(data.fallbacks) ? data.fallbacks.join(" / ") : "rule / hold";
            output.textContent = `今日导演调用：${calls} 次；缓存条目：${entries}${ttl > 0 ? `（TTL ${ttl}s）` : "（不复用）"}；回退策略：${fallbacks}。`;
        } catch {
            if (output) output.textContent = "导演状态读取失败（不影响配乐播放）";
        }
    }

    async function clearDirectorCache(): Promise<void> {
        const confirmed = await window.TrpgDialog?.confirm({
            title: "清理导演乐谱缓存",
            message: "清理后下一次触发会重新调用模型（可能产生费用）。确定继续？",
            confirmText: "清理",
            danger: true,
        });
        if (!confirmed) return;
        try {
            const response = await TrpgApi.post<ApiResponse<{ cleared?: number }>>(
                "/api/immersive/director/cache/clear",
                {},
            );
            if (!response.success) {
                setMessage(response.message || response.error || "清理缓存失败", "error");
                return;
            }
            setMessage(`已清理 ${response.data?.cleared ?? 0} 条乐谱缓存`, "success");
            void refreshDirectorStatus();
        } catch (error) {
            setMessage(error instanceof Error ? error.message : "清理缓存失败", "error");
        }
    }

    async function refreshUsage(): Promise<void> {
        if (usageLoading) return;
        usageLoading = true;
        const output = byId<HTMLElement>("immersiveUsageOutput");
        try {
            const response = await TrpgApi.get<ApiResponse<{
                usage?: { date?: string; daily_used?: number };
                daily_budget?: number;
                room_hourly_cap?: number;
                director_usage?: ImmersiveDirectorUsage;
                director_daily_budget?: number;
            }>>("/api/immersive/usage");
            if (!response.success || !response.data) return;
            const data = response.data;
            const used = Number(data.usage?.daily_used || 0);
            const budget = Number(data.daily_budget || 0);
            const cap = Number(data.room_hourly_cap || 0);
            const directorCalls = Number(data.director_usage?.calls || 0);
            const directorBudget = Number(data.director_daily_budget || 0);
            if (output) {
                output.textContent = [
                    `今日外部生图：${used} 张${budget > 0 ? ` / 预算 ${budget} 张` : "（未设每日预算上限）"}`,
                    `单房间每小时上限：${cap > 0 ? `${cap} 张` : "不限"}`,
                    `音乐导演调用：${directorCalls} 次${directorBudget > 0 ? ` / 预算 ${directorBudget} 次` : "（未设每日预算）"}`,
                ].join("；") + "。";
            }
        } catch {
            if (output) output.textContent = "用量读取失败（不影响配图与叙事）";
        } finally {
            usageLoading = false;
        }
    }
}
