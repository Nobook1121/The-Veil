namespace SetupWizard {
    interface SetupStatusData {
        setup_required?: boolean;
        owner_exists?: boolean;
        site_name?: string;
        site_domain?: string;
    }

    interface SetupEmbeddingJob {
        state?: string;
        message?: string;
        error?: string;
        host?: string;
        total_files?: number;
        completed_files?: number;
        running?: boolean;
    }

    interface SetupEmbeddingPlatform {
        platform?: string;
        name?: string;
        has_api_key?: boolean;
        embedding_base_url?: string;
        embedding_model?: string;
        embedding_dimensions?: number;
    }

    interface SetupEmbeddingStatus {
        local_model_present?: boolean;
        local_model_files?: number;
        cloud_embedding_enabled?: boolean;
        local_embedding_repo?: string;
        job?: SetupEmbeddingJob;
        platforms?: SetupEmbeddingPlatform[];
    }

    interface SetupUploadItem {
        file: File;
        path: string;
    }

    const TOTAL_STEPS = 6;
    const SKIPPABLE_STEPS = [1, 2, 4];
    const DEFAULT_SITE_NAME = "帷幕";
    const DISMISS_STORAGE_KEY = "trpg_setup_dismissed";
    const EMBEDDING_POLL_INTERVAL = 2000;
    let currentStep = 1;
    let brandingName = "";
    let brandingBound = false;
    let wizardBound = false;
    let siteName = "";
    let siteDomain = "";
    let aiPlatform = "";
    let aiModel = "";
    let embeddingSummary = "";
    let embeddingPlatformsLoaded = false;
    let embeddingPollTimer: number | null = null;
    let skipPromise: Promise<void> | null = null;
    let skipResolver: (() => void) | null = null;

    export async function init(): Promise<boolean> {
        try {
            const status = await TrpgApi.get<ApiResponse<SetupStatusData>>("/api/setup/status");
            const data = status.data;
            applySiteBranding(data?.site_name || "");
            // 已在本浏览器跳过引导时不再弹出（服务端状态未及时更新也能生效）。
            // 只要服务端已有 OWNER，就说明本机已完成初始化，任何入口都不再弹出引导。
            if (status.success && data && data.setup_required && data.owner_exists !== true && !isDismissedLocally()) {
                startWizard();
                return true;
            }
        } catch (error) {
            console.error("[setup] 无法获取初始化状态", error);
        }
        return false;
    }

    function isDismissedLocally(): boolean {
        try {
            return localStorage.getItem(DISMISS_STORAGE_KEY) === "true";
        } catch {
            return false;
        }
    }

    function markDismissedLocally(): void {
        try {
            localStorage.setItem(DISMISS_STORAGE_KEY, "true");
        } catch {
            // 存储不可用时仍可通过服务端状态避免重复弹出。
        }
    }

    /** 用户点击“跳过全部引导”后完成，调用方据此继续初始化主应用。 */
    export function whenSkipped(): Promise<void> {
        if (!skipPromise) {
            skipPromise = new Promise<void>((resolve) => {
                skipResolver = resolve;
            });
        }
        return skipPromise;
    }

    export function applySiteBranding(name: string): void {
        brandingName = name || "";
        if (!brandingBound) {
            brandingBound = true;
            window.addEventListener("trpg:locale-changed", () => window.setTimeout(renderBranding, 0));
        }
        renderBranding();
    }

    function renderBranding(): void {
        if (!brandingName) return;
        document.title = brandingName;
        document.querySelectorAll<HTMLElement>("[data-i18n='app.title']").forEach((element) => {
            element.textContent = brandingName;
        });
    }

    function startWizard(): void {
        const wizard = document.getElementById("setupWizard");
        if (!wizard) {
            console.error("[setup] 未找到引导向导容器");
            return;
        }
        // 引导期间隐藏登录弹窗，避免登录流程与首次初始化冲突。
        const authModal = document.getElementById("auth-modal");
        if (authModal) authModal.style.display = "none";
        // 先绑定事件再显示遮罩：即使绑定过程异常，也不会留下无法点击的引导层。
        if (!wizardBound) {
            wizardBound = true;
            bindEvents();
        }
        wizard.hidden = false;
        prefillAiDefaults();
        prefillSiteName();
        renderStep(1);
    }

    /** 填入默认网站名称，用户直接点击“下一步”时即采用该名称。 */
    function prefillSiteName(): void {
        const field = byId<HTMLInputElement>("setupSiteName");
        if (field && !field.value.trim()) {
            field.value = DEFAULT_SITE_NAME;
        }
    }

    /** 跳过全部引导：关闭向导并让主应用继续初始化。 */
    function skipAll(): void {
        markDismissedLocally();
        // 通知服务端记录“已跳过”，避免刷新后再次弹出；失败也不影响本地跳过。
        void TrpgApi.post<ApiResponse>("/api/setup/dismiss", {}).catch(() => undefined);
        const wizard = document.getElementById("setupWizard");
        if (wizard) wizard.hidden = true;
        if (skipResolver) {
            const resolve = skipResolver;
            skipResolver = null;
            resolve();
        }
    }

    function bindEvents(): void {
        byId("setupPrevBtn")?.addEventListener("click", () => {
            if (currentStep > 1) renderStep(currentStep - 1);
        });
        byId("setupNextBtn")?.addEventListener("click", () => {
            void handleNext();
        });
        byId("setupSkipBtn")?.addEventListener("click", () => {
            handleSkip();
        });
        byId("setupFinishBtn")?.addEventListener("click", () => {
            window.location.reload();
        });
        byId("setupSkipAllBtn")?.addEventListener("click", () => {
            skipAll();
        });
        byId("setupAiTestBtn")?.addEventListener("click", () => {
            void testAiConnection();
        });
        byId("setupPlatform")?.addEventListener("change", prefillAiDefaults);
        bindEmbeddingEvents();
    }

    function prefillAiDefaults(): void {
        const select = byId("setupPlatform") as HTMLSelectElement | null;
        if (!select) return;
        const option = select.selectedOptions[0];
        if (!option) return;
        setValue("setupBaseUrl", option.dataset.baseUrl || "");
        setValue("setupModel", option.dataset.model || "");
    }

    function renderStep(step: number): void {
        currentStep = step;
        document.querySelectorAll<HTMLElement>(".setup-panel").forEach((panel) => {
            panel.classList.toggle("is-active", panel.dataset.setupStep === String(step));
        });
        document.querySelectorAll<HTMLElement>(".setup-progress-step").forEach((dot) => {
            const value = Number(dot.dataset.step || "0");
            dot.classList.toggle("is-done", value < step);
            dot.classList.toggle("is-active", value === step);
        });
        setHidden("setupPrevBtn", step <= 1);
        setHidden("setupNextBtn", step >= TOTAL_STEPS);
        setHidden("setupFinishBtn", step < TOTAL_STEPS);
        setHidden("setupSkipBtn", !SKIPPABLE_STEPS.includes(step));
        setMessage("");
        if (step === 2) {
            void refreshEmbeddingStatus();
        }
        if (step === TOTAL_STEPS) {
            void refreshEmbeddingStatus().then(renderSummary);
        }
    }

    async function handleNext(): Promise<void> {
        try {
            if (currentStep === 1) {
                if (!(await saveAiStep())) return;
                renderStep(2);
            } else if (currentStep === 2) {
                renderStep(3);
            } else if (currentStep === 3) {
                if (!(await saveSiteStep())) return;
                renderStep(4);
            } else if (currentStep === 4) {
                if (!(await saveSiteStep())) return;
                renderStep(5);
            } else if (currentStep === 5) {
                if (!(await createOwnerStep())) return;
                renderStep(6);
            }
        } catch (error) {
            setMessage(errorMessage(error), "error");
        }
    }

    function handleSkip(): void {
        if (currentStep === 1) {
            renderStep(2);
            setMessage(msg("setup.msg.ai_skipped", "已跳过 AI 配置，可稍后在“设置 - 模型设置”中补充。"), "success");
            return;
        }
        if (currentStep === 2) {
            stopEmbeddingPoll();
            renderStep(3);
            setMessage(msg("setup.msg.embedding_skipped", "已跳过 Embedding 模型配置，可稍后在“设置 - 模型设置”中补充。"), "success");
            return;
        }
        if (currentStep === 4) {
            renderStep(5);
        }
    }

    async function saveAiStep(): Promise<boolean> {
        const platform = value("setupPlatform") || "custom";
        const baseUrl = value("setupBaseUrl").trim();
        const apiKey = value("setupApiKey").trim();
        const model = value("setupModel").trim();
        if (!baseUrl && !apiKey && !model) {
            return true;
        }
        if (!baseUrl || !model) {
            setMessage(msg("setup.error.ai_required", "请填写 API 地址和模型 ID。"), "error");
            return false;
        }
        if (!apiKey && platform !== "lmstudio") {
            setMessage(msg("setup.error.api_key_required", "请填写 API Key。"), "error");
            return false;
        }

        const response = await TrpgApi.post<ApiResponse<{ platform?: string; model?: string }>>("/api/setup/ai", {
            platform,
            base_url: baseUrl,
            api_key: apiKey,
            model,
        });
        if (!response.success) {
            setMessage(backendMessage(response.message || response.error || "", msg("setup.error.ai_save_failed", "保存 AI 配置失败")), "error");
            return false;
        }
        aiPlatform = platform;
        aiModel = model;
        return true;
    }

    async function saveSiteStep(): Promise<boolean> {
        const domain = value("setupSiteDomain").trim();
        const name = value("setupSiteName").trim() || DEFAULT_SITE_NAME;
        setValue("setupSiteName", name);

        const response = await TrpgApi.post<ApiResponse<{ name?: string; domain?: string }>>("/api/setup/site", {
            name,
            domain,
        });
        if (!response.success) {
            setMessage(backendMessage(response.message || response.error || "", msg("setup.error.site_save_failed", "保存网站信息失败")), "error");
            return false;
        }
        siteName = name;
        siteDomain = domain;
        return true;
    }

    async function createOwnerStep(): Promise<boolean> {
        const username = value("setupOwnerUsername").trim();
        const email = value("setupOwnerEmail").trim();
        const password = value("setupOwnerPassword");
        const confirm = value("setupOwnerConfirm");
        if (!username || !email || !password) {
            setMessage(msg("setup.error.owner_incomplete", "请完整填写 Owner 账号信息。"), "error");
            return false;
        }
        if (password !== confirm) {
            setMessage(msg("setup.error.password_mismatch", "两次输入的密码不一致。"), "error");
            return false;
        }

        const response = await TrpgApi.post<ApiResponse<{ username?: string }>>("/api/setup/owner", {
            username,
            email,
            password,
            confirm_password: confirm,
        });
        if (!response.success) {
            setMessage(backendMessage(response.message || response.error || "", msg("setup.error.owner_create_failed", "创建 Owner 账号失败")), "error");
            return false;
        }
        return true;
    }

    async function testAiConnection(): Promise<void> {
        const platform = value("setupPlatform") || "custom";
        const baseUrl = value("setupBaseUrl").trim();
        const apiKey = value("setupApiKey").trim();
        const model = value("setupModel").trim();
        if (!baseUrl || !model) {
            setMessage(msg("setup.test.required", "请先填写 API 地址和模型 ID。"), "error");
            return;
        }

        setMessage(msg("setup.test.testing", "正在测试连接…"), "");
        try {
            const response = await TrpgApi.post<ApiResponse>("/api/setup/ai/test", {
                platform,
                base_url: baseUrl,
                api_key: apiKey,
                model,
            });
            if (response.success) {
                setMessage(msg("setup.test.success", "连接成功。"), "success");
            } else {
                setMessage(backendMessage(response.error || response.message || "", msg("setup.test.failure", "连接失败，请检查配置。")), "error");
            }
        } catch (error) {
            setMessage(errorMessage(error), "error");
        }
    }

    function bindEmbeddingEvents(): void {
        document.querySelectorAll<HTMLInputElement>('input[name="setupEmbeddingMode"]').forEach((radio) => {
            radio.addEventListener("change", () => {
                if (radio.checked) selectEmbeddingMode(radio.value);
            });
        });
        byId("setupEmbeddingDownloadBtn")?.addEventListener("click", () => {
            void startEmbeddingDownload();
        });
        const dropzone = byId("setupEmbeddingDropzone");
        const fileInput = byId<HTMLInputElement>("setupEmbeddingFileInput");
        dropzone?.addEventListener("click", () => fileInput?.click());
        dropzone?.addEventListener("keydown", (event) => {
            if ((event as KeyboardEvent).key === "Enter" || (event as KeyboardEvent).key === " ") {
                event.preventDefault();
                fileInput?.click();
            }
        });
        dropzone?.addEventListener("dragover", (event) => {
            event.preventDefault();
            dropzone.classList.add("is-dragover");
        });
        dropzone?.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
        dropzone?.addEventListener("drop", (event) => {
            event.preventDefault();
            dropzone.classList.remove("is-dragover");
            void collectDataTransferFiles((event as DragEvent).dataTransfer).then(uploadEmbeddingFiles);
        });
        fileInput?.addEventListener("change", () => {
            if (fileInput.files?.length) void uploadEmbeddingFiles(collectInputFiles(fileInput));
        });
        byId("setupEmbeddingPlatform")?.addEventListener("change", prefillEmbeddingCloudDefaults);
        byId("setupEmbeddingCloudBtn")?.addEventListener("click", () => {
            void enableCloudEmbedding();
        });
    }

    function selectEmbeddingMode(mode: string): void {
        document.querySelectorAll<HTMLElement>(".setup-embed-pane").forEach((pane) => {
            pane.classList.toggle("is-active", pane.dataset.embedPane === mode);
        });
        setMessage("");
    }

    /** 拉取 embedding 步骤状态：本地模型、下载进度、已配置的平台。 */
    async function refreshEmbeddingStatus(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<SetupEmbeddingStatus>>("/api/setup/embedding/status");
            const data = response.data;
            if (!response.success || !data) return;
            if (data.local_model_present) {
                embeddingSummary = msg("setup.embed.summary_local", "本地模型");
            } else if (data.cloud_embedding_enabled) {
                embeddingSummary = msg("setup.embed.summary_cloud", "云端 Embedding");
            }
            renderEmbeddingRepo(data.local_embedding_repo);
            renderEmbeddingPlatforms(data.platforms || []);
            updateEmbeddingJob(data.job);
        } catch (error) {
            console.error("[setup] 无法获取 Embedding 状态", error);
        }
    }

    /** 下载选项显示的仓库名取自服务端配置，避免在前端文案里写死。 */
    function renderEmbeddingRepo(repo?: string): void {
        const target = byId<HTMLElement>("setupEmbeddingRepo");
        if (!target) return;
        target.textContent = repo || "";
        target.hidden = !repo;
    }

    function renderEmbeddingPlatforms(platforms: SetupEmbeddingPlatform[]): void {
        const select = byId<HTMLSelectElement>("setupEmbeddingPlatform");
        if (!select) return;
        const previous = select.value;
        select.innerHTML = "";
        if (!platforms.length) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = msg("setup.embed.no_platform", "尚未配置 AI 平台，请先完成第 1 步");
            select.appendChild(option);
            select.disabled = true;
            setHidden("setupEmbeddingCloudBtn", true);
            return;
        }
        select.disabled = false;
        setHidden("setupEmbeddingCloudBtn", false);
        platforms.forEach((item) => {
            const option = document.createElement("option");
            option.value = item.platform || "";
            option.textContent = item.name || item.platform || "";
            option.dataset.embeddingUrl = item.embedding_base_url || "";
            option.dataset.embeddingModel = item.embedding_model || "";
            option.dataset.embeddingDimensions = item.embedding_dimensions ? String(item.embedding_dimensions) : "";
            select.appendChild(option);
        });
        if (previous && platforms.some((item) => item.platform === previous)) {
            select.value = previous;
        } else if (!embeddingPlatformsLoaded && aiPlatform && platforms.some((item) => item.platform === aiPlatform)) {
            select.value = aiPlatform;
        }
        embeddingPlatformsLoaded = true;
        prefillEmbeddingCloudDefaults();
    }

    function prefillEmbeddingCloudDefaults(): void {
        const select = byId<HTMLSelectElement>("setupEmbeddingPlatform");
        const option = select?.selectedOptions[0];
        if (!option) return;
        setValue("setupEmbeddingBaseUrl", option.dataset.embeddingUrl || "");
        setValue("setupEmbeddingModel", option.dataset.embeddingModel || "");
        setValue("setupEmbeddingDimensions", option.dataset.embeddingDimensions || "");
    }

    async function startEmbeddingDownload(): Promise<void> {
        setMessage(msg("setup.embed.download_starting", "正在启动下载…"));
        try {
            const response = await TrpgApi.post<ApiResponse<{ started?: boolean; job?: SetupEmbeddingJob }>>(
                "/api/setup/embedding/download",
                {},
            );
            const data = response.data;
            if (!response.success || !data) {
                setMessage(backendMessage(response.message || response.error || "", msg("setup.embed.download_failed", "启动下载失败")), "error");
                return;
            }
            updateEmbeddingJob(data.job);
            if (data.started === false) {
                setMessage(msg("setup.embed.download_running", "下载已在进行中…"));
            }
            startEmbeddingPoll();
        } catch (error) {
            setMessage(errorMessage(error), "error");
        }
    }

    function startEmbeddingPoll(): void {
        if (embeddingPollTimer !== null) return;
        embeddingPollTimer = window.setInterval(() => {
            void pollEmbeddingJob();
        }, EMBEDDING_POLL_INTERVAL);
        void pollEmbeddingJob();
    }

    async function pollEmbeddingJob(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<SetupEmbeddingStatus>>("/api/setup/embedding/status");
            const job = response.data?.job;
            if (!response.success || !job) {
                stopEmbeddingPoll();
                return;
            }
            updateEmbeddingJob(job);
            if (job.state !== "running") stopEmbeddingPoll();
        } catch {
            stopEmbeddingPoll();
        }
    }

    function stopEmbeddingPoll(): void {
        if (embeddingPollTimer !== null) {
            window.clearInterval(embeddingPollTimer);
            embeddingPollTimer = null;
        }
    }

    function updateEmbeddingJob(job?: SetupEmbeddingJob): void {
        if (!job) return;
        const progress = byId("setupEmbeddingProgress");
        const bar = byId<HTMLProgressElement>("setupEmbeddingProgressBar");
        const text = byId("setupEmbeddingProgressText");
        const total = job.total_files || 0;
        const completed = job.completed_files || 0;
        if (progress) progress.hidden = job.state === "idle";
        if (bar) {
            if (total > 0) {
                bar.max = total;
                bar.value = Math.min(completed, total);
            } else {
                bar.removeAttribute("value");
                bar.max = 100;
            }
        }
        if (text) text.textContent = total > 0 ? `${completed}/${total}` : "";

        // 下载在后台进行，用户可能已经翻到后续步骤；此时不要把进度文案写到共享提示区。
        if (currentStep !== 2) return;

        if (job.state === "done") {
            embeddingSummary = msg("setup.embed.summary_local", "本地模型");
            setMessage(msg("setup.embed.download_done", "模型下载完成。重启程序后即可启用本地模型。"), "success");
        } else if (job.state === "error") {
            console.error("[setup] Embedding 下载失败", job.error);
            setMessage(msg("setup.embed.download_failed", "模型下载失败，请改用上传或云端方案。"), "error");
        } else if (job.state === "running") {
            setMessage(job.message || msg("setup.embed.downloading", "正在下载模型…"));
        }
    }

    function collectInputFiles(input: HTMLInputElement): SetupUploadItem[] {
        return Array.from(input.files || []).map((file) => ({
            file,
            path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
        }));
    }

    async function collectDataTransferFiles(dataTransfer: DataTransfer | null): Promise<SetupUploadItem[]> {
        if (!dataTransfer) return [];
        const entries: FileSystemEntry[] = [];
        Array.from(dataTransfer.items || []).forEach((item) => {
            if (item.kind !== "file") return;
            const entry = (item as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry?.();
            if (entry) entries.push(entry);
        });
        if (!entries.length) {
            return Array.from(dataTransfer.files || []).map((file) => ({ file, path: file.name }));
        }
        const collected: SetupUploadItem[] = [];
        for (const entry of entries) {
            await walkEntry(entry, "", collected);
        }
        return collected;
    }

    function walkEntry(entry: FileSystemEntry, prefix: string, output: SetupUploadItem[]): Promise<void> {
        if (entry.isFile) {
            return new Promise<void>((resolve) => {
                (entry as FileSystemFileEntry).file((file) => {
                    output.push({ file, path: prefix ? `${prefix}/${file.name}` : file.name });
                    resolve();
                }, () => resolve());
            });
        }
        if (entry.isDirectory) {
            const reader = (entry as FileSystemDirectoryEntry).createReader();
            const dirPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
            return readAllDirectoryEntries(reader).then((children) =>
                Promise.all(children.map((child) => walkEntry(child, dirPrefix, output))).then(() => undefined),
            );
        }
        return Promise.resolve();
    }

    function readAllDirectoryEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
        const entries: FileSystemEntry[] = [];
        const readBatch = (): Promise<void> =>
            new Promise<void>((resolve) => {
                reader.readEntries((batch) => {
                    if (!batch.length) {
                        resolve();
                        return;
                    }
                    entries.push(...batch);
                    resolve(readBatch());
                }, () => resolve());
            });
        return readBatch().then(() => entries);
    }

    async function uploadEmbeddingFiles(items: SetupUploadItem[]): Promise<void> {
        if (!items.length) return;
        const form = new FormData();
        const single = items.length === 1 ? items[0] : undefined;
        if (single && single.file.name.toLowerCase().endsWith(".zip")) {
            form.append("file", single.file, single.file.name);
        } else {
            items.forEach((item) => form.append("files", item.file, item.path));
        }

        setMessage(msg("setup.embed.uploading", "正在上传模型文件…"));
        try {
            const response = await TrpgApi.post<ApiResponse<{ local_model_present?: boolean }>>(
                "/api/setup/embedding/upload",
                form,
            );
            if (!response.success) {
                setMessage(backendMessage(response.message || response.error || "", msg("setup.embed.upload_failed", "上传失败")), "error");
                return;
            }
            embeddingSummary = msg("setup.embed.summary_local", "本地模型");
            setMessage(msg("setup.embed.upload_done", "模型文件上传完成。重启程序后即可启用本地模型。"), "success");
        } catch (error) {
            setMessage(errorMessage(error), "error");
        }
    }

    async function enableCloudEmbedding(): Promise<void> {
        const platform = value("setupEmbeddingPlatform");
        const baseUrl = value("setupEmbeddingBaseUrl").trim();
        const modelId = value("setupEmbeddingModel").trim();
        const dimensions = Number(value("setupEmbeddingDimensions"));
        if (!platform) {
            setMessage(msg("setup.embed.cloud_no_platform", "请先在第 1 步配置 AI 平台。"), "error");
            return;
        }
        if (!baseUrl || !modelId) {
            setMessage(msg("setup.embed.cloud_required", "请填写 Embedding 接口地址与模型 ID。"), "error");
            return;
        }
        if (!dimensions || dimensions < 1) {
            setMessage(msg("setup.embed.cloud_dimensions_required", "请填写向量维度。"), "error");
            return;
        }

        try {
            const response = await TrpgApi.post<ApiResponse>("/api/setup/embedding/cloud", {
                platform,
                base_url: baseUrl,
                model: modelId,
                dimensions,
            });
            if (!response.success) {
                setMessage(backendMessage(response.message || response.error || "", msg("setup.embed.cloud_failed", "启用云端 Embedding 失败")), "error");
                return;
            }
            embeddingSummary = msg("setup.embed.summary_cloud", "云端 Embedding");
            setMessage(msg("setup.embed.cloud_done", "已启用云端 Embedding。重启程序后生效。"), "success");
        } catch (error) {
            setMessage(errorMessage(error), "error");
        }
    }

    function renderSummary(): void {
        const summary = byId("setupSummary");
        if (!summary) return;
        const rows: Array<[string, string]> = [
            [msg("setup.summary.platform", "AI 平台"), aiPlatform || msg("setup.summary.unset", "未配置")],
            [msg("setup.summary.model", "模型"), aiModel || msg("setup.summary.unset", "未配置")],
            [msg("setup.summary.embedding", "Embedding 模型"), embeddingSummary || msg("setup.summary.unset", "未配置")],
            [msg("setup.summary.site_name", "网站名称"), siteName || "-"],
            [msg("setup.summary.domain", "域名"), siteDomain || msg("setup.summary.domain_unbound", "未绑定")],
        ];
        summary.innerHTML = rows
            .map(([label, content]) => `<li><span>${escapeHtml(label)}</span><strong>${escapeHtml(content)}</strong></li>`)
            .join("");
    }

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function value(id: string): string {
        const element = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
        return element?.value || "";
    }

    function setValue(id: string, content: string): void {
        const element = document.getElementById(id) as HTMLInputElement | null;
        if (element && element.value !== content) element.value = content;
    }

    function setHidden(id: string, hidden: boolean): void {
        const element = document.getElementById(id);
        if (element) element.hidden = hidden;
    }

    function setMessage(content: string, kind: "" | "error" | "success" = ""): void {
        const element = document.getElementById("setupMessage");
        if (!element) return;
        element.textContent = content;
        element.className = `setup-message${kind ? ` is-${kind}` : ""}`;
    }

    function escapeHtml(content: string): string {
        const element = document.createElement("div");
        element.textContent = content;
        return element.innerHTML;
    }

    function msg(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    /** 引导相关后端提示（英文）到 i18n key 的映射。 */
    const SETUP_BACKEND_KEYS: Record<string, string> = {
        "Setup already completed": "setup.backend.locked",
        "Site name is required": "setup.backend.site_name_required",
        "Site name is too long": "setup.backend.site_name_too_long",
        "Domain is too long": "setup.backend.domain_too_long",
        "Base URL and model are required": "setup.backend.ai_incomplete",
        "Invalid platform id": "setup.backend.platform_invalid",
        "Failed to create owner account": "setup.backend.owner_create_failed",
        "Invalid model repo": "setup.backend.embedding_repo_invalid",
        "Failed to start download": "setup.backend.embedding_download_failed",
        "Failed to store model files": "setup.backend.embedding_upload_failed",
        "Platform not configured": "setup.backend.embedding_platform_missing",
        "Platform has no API key": "setup.backend.embedding_platform_no_key",
        "Incomplete embedding config": "setup.backend.embedding_incomplete",
        "Failed to save embedding config": "setup.backend.embedding_cloud_failed",
        "Uploaded file is too large": "setup.backend.upload_too_large",
    };

    /** 把后端返回的提示转换为当前语言；未知提示复用认证模块的映射，仍无法匹配则原样返回。 */
    function backendMessage(message: string, fallback: string): string {
        if (!message) {
            return fallback;
        }
        const key = SETUP_BACKEND_KEYS[message];
        if (key) {
            return msg(key, message);
        }
        return AuthModule.localizedAuthMessage({ success: false, message }, fallback);
    }

    function errorMessage(error: unknown): string {
        return error instanceof Error ? error.message : String(error);
    }
}