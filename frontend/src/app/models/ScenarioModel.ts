class ScenarioModel {
    scenarios: Scenario[] = [];
    apiBaseUrl = "/api";
    userId: string | number | null = null;
    isAuthenticated = false;

    getCurrentUserId(): string | number | null {
        return this.userId;
    }

    async checkAuthStatus(): Promise<boolean> {
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ user_id: string | number }>>("/api/auth/status");
            if (response.ok && data.success && data.data) {
                this.userId = data.data.user_id;
                this.isAuthenticated = true;
                return true;
            }
        } catch (error) {
            console.error("检查认证状态失败:", error);
        }
        this.userId = null;
        this.isAuthenticated = false;
        return false;
    }

    async init(): Promise<Scenario[]> {
        await this.checkAuthStatus();
        return this.loadScenarios();
    }

    async loadScenarios(): Promise<Scenario[]> {
        this.scenarios = [];

        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<unknown>>(`${this.apiBaseUrl}/scenarios`);
            if (response.ok && data.success && Array.isArray(data.data)) {
                this.scenarios = data.data.filter(isScenario);
                this.saveScenarios();
                return this.scenarios;
            }
            console.warn("剧本 API 返回异常:", data.message || data.error || response.status);
        } catch (error) {
            console.warn("从 API 加载剧本失败，尝试使用本地缓存:", error);
        }

        this.scenarios = this.loadCachedScenarios();
        return this.scenarios;
    }

    async createScenario(scenarioData: ScenarioInput): Promise<Scenario> {
        if (!this.isAuthenticated) {
            await this.checkAuthStatus();
        }

        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<unknown>>(`${this.apiBaseUrl}/scenarios`, {
            method: "POST",
            body: {
                ...scenarioData,
                user_id: this.userId || "anonymous",
            },
        });

        if (!response.ok || !data.success || !isScenario(data.data)) {
            throw new Error(data.message || data.error || `API 请求失败: ${response.status}`);
        }

        this.scenarios.push(data.data);
        this.saveScenarios();
        return data.data;
    }

    async updateScenario(id: number, scenarioData: ScenarioInput): Promise<Scenario> {
        if (!this.isAuthenticated) {
            const authenticated = await this.checkAuthStatus();
            if (!authenticated) throw new Error("请先登录");
        }

        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<unknown>>(`${this.apiBaseUrl}/scenarios/${id}`, {
            method: "PUT",
            body: {
                ...scenarioData,
                user_id: this.userId,
            },
        });

        if (!response.ok || !data.success || !isScenario(data.data)) {
            const payload = data as ApiResponse<unknown> & { code?: string };
            const error = new Error(data.message || data.error || `API 请求失败: ${response.status}`) as Error & {
                code?: string | undefined;
                detail?: unknown;
            };
            error.code = payload.code;
            error.detail = data.data;
            throw error;
        }

        const index = this.scenarios.findIndex((scenario) => scenario.id === id);
        if (index === -1) throw new Error("剧本不存在");
        this.scenarios[index] = data.data;
        this.saveScenarios();
        return data.data;
    }

    async deleteScenario(id: number): Promise<{ deleted: boolean; archived: boolean; activeRooms: number }> {
        if (!this.isAuthenticated) {
            const authenticated = await this.checkAuthStatus();
            if (!authenticated) throw new Error("请先登录");
        }

        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`${this.apiBaseUrl}/scenarios/${id}`, {
            method: "DELETE",
            body: {
                user_id: this.userId,
            },
        });

        if (!response.ok || !data.success) {
            throw new Error(data.message || data.error || `API 请求失败: ${response.status}`);
        }

        const index = this.scenarios.findIndex((scenario) => scenario.id === id);
        if (index === -1) throw new Error("剧本不存在");
        this.scenarios.splice(index, 1);
        this.saveScenarios();
        // 仍有房间占用时后端只归档（剧本仍存在于磁盘，知识库需保留给房间使用），
        // 调用方据此给出准确提示，而不是让用户误以为知识库没删干净。
        const payload = (data.data || {}) as { archived?: boolean; active_rooms?: number };
        return { deleted: payload.archived !== true, archived: payload.archived === true, activeRooms: Number(payload.active_rooms || 0) };
    }

    getScenario(id: number): Scenario | undefined {
        return this.scenarios.find((scenario) => scenario.id === id);
    }

    getScenarios(): Scenario[] {
        return this.scenarios;
    }

    saveScenarios(): void {
        localStorage.setItem("trpg_scenarios", JSON.stringify(this.scenarios));
    }

    /** Convert source text on the server without creating a scenario. */
    async convertScript(text: string, title = "Imported scenario"): Promise<ScenarioInput> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioInput>>(
            `${this.apiBaseUrl}/scenarios/import`,
            { method: "POST", body: { text, title } },
        );
        if (!response.ok || !data.success || !data.data) {
            throw new Error(data.message || data.error || `Script import failed: ${response.status}`);
        }
        return data.data;
    }

    async convertScriptFile(file: File, title = "Imported scenario"): Promise<ScenarioInput> {
        const formData = new FormData();
        formData.append("file", file, file.name);
        formData.append("title", title);
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioInput>>(
            `${this.apiBaseUrl}/scenarios/import`,
            { method: "POST", body: formData },
        );
        if (!response.ok || !data.success || !data.data) {
            throw new Error(data.message || data.error || `Script import failed: ${response.status}`);
        }
        return data.data;
    }

    async getKnowledgeStats(id: number): Promise<{ scenario_id: number; version: string; vector_count: number; path: string; backend: string }> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ scenario_id: number; version: string; vector_count: number; path: string; backend: string }>>(`${this.apiBaseUrl}/scenarios/${id}/knowledge`);
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || "无法加载知识库信息");
        return data.data;
    }

    // 使用 XMLHttpRequest 而非 fetch：需要 upload.onprogress 上报上传百分比，
    // 同时手动补上 CSRF 头（与 api-client 保持一致）。
    async createImportJob(formData: FormData, onProgress?: (value: number) => void): Promise<ScenarioImportStart> {
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = () => {
            if (xhr.readyState === XMLHttpRequest.OPENED) {
                xhr.setRequestHeader("Accept", "application/json");
                const csrf = window.currentUser?.csrf_token;
                if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
            }
        };
        const result = await new Promise<{ jobId: string; scriptId: number }>((resolve, reject) => { xhr.open("POST", "/api/scripts/import"); xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total * 100); }; xhr.onload = () => { try { const data = JSON.parse(xhr.responseText); if (xhr.status >= 200 && xhr.status < 300 && data.success && data.data?.jobId) resolve(data.data); else reject(new Error(data.message || "导入失败")); } catch { reject(new Error("导入响应无效")); } }; xhr.onerror = () => reject(new Error("导入请求失败")); xhr.send(formData); });
        return result;
    }
    async importBundle(file: File): Promise<ScenarioImportStart & { preview: ScenarioInput }> {
        const formData = new FormData();
        formData.append("file", file, file.name);
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioImportStart & { preview: ScenarioInput }>>(
            "/api/scripts/import/bundle",
            { method: "POST", body: formData },
        );
        if (!response.ok || !data.success || !data.data) {
            throw new Error(data.message || data.error || `剧本包导入失败：${response.status}`);
        }
        return data.data;
    }
    async getImportJob(id: string): Promise<ScenarioImportJob> { const { data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioImportJob>>(`/api/scripts/import/${encodeURIComponent(id)}`); if (!data.success || !data.data) throw new Error(data.message || "任务不存在"); return data.data; }
    async listImportJobs(): Promise<ScenarioImportJob[]> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioImportJob[]>>("/api/scripts/import/jobs");
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || data.error || "无法读取导入任务");
        return data.data;
    }
    async saveImportPreview(jobId: string, payload: ScenarioInput): Promise<ScenarioInput> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioInput>>(`/api/scripts/import/${encodeURIComponent(jobId)}/preview`, { method: "PUT", body: payload });
        if (!response.ok || !data.success) throw new Error(data.message || data.error || "保存审核结果失败");
        return data.data || payload;
    }
    async retryImportJob(id: string, stage?: string): Promise<ScenarioImportJob> { const { data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioImportJob>>(`/api/scripts/import/${encodeURIComponent(id)}/retry`, { method: "POST", body: stage ? { stage } : {} }); if (!data.success || !data.data) throw new Error(data.message || "重试失败"); return data.data; }
    async abandonImportJob(id: string): Promise<void> { const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ jobId: string }>>(`/api/scripts/import/${encodeURIComponent(id)}`, { method: "DELETE" }); if (!response.ok || !data.success) throw new Error(data.message || data.error || "放弃导入失败"); }
    async cancelImportJob(id: string): Promise<ScenarioImportJob> { const { data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioImportJob>>(`/api/scripts/import/${encodeURIComponent(id)}/cancel`, { method: "POST" }); if (!data.success || !data.data) throw new Error(data.message || "取消失败"); return data.data; }

    async publishImport(scriptId: number, jobId: string): Promise<Scenario> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<Scenario>>(`/api/scripts/${scriptId}/publish`, { method: "POST", body: { jobId } });
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || data.error || "Import publish failed");
        const index = this.scenarios.findIndex((item) => item.id === data.data!.id);
        if (index >= 0) this.scenarios[index] = data.data!; else this.scenarios.push(data.data!);
        this.saveScenarios();
        return data.data!;
    }

    // 轮询导入任务直到进入终态（done/failed/cancelled/published）；
    // done 与 published（后台已自动发布）都视为成功，其余终态抛出任务自身错误。
    // 间隔 1.2 秒：超长剧本可能跑十几分钟，350ms 轮询会累积上千次请求。
    async waitForImportJob(id: string, onProgress?: (job: ScenarioImportJob) => void): Promise<ScenarioImportJob> {
        for (;;) {
            const job = await this.getImportJob(id);
            onProgress?.(job);
            if (["done", "published", "failed", "cancelled"].includes(job.status)) {
                if (job.status !== "done" && job.status !== "published") throw new Error(job.error || "Import job failed");
                return job;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
    }

    async loadDraft(): Promise<ScenarioInput | null> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioInput | null>>(`${this.apiBaseUrl}/scenarios/draft`);
        if (!response.ok || !data.success) throw new Error(data.message || data.error || "加载草稿失败");
        return data.data || null;
    }

    async saveDraft(scenarioData: ScenarioInput): Promise<ScenarioInput> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioInput>>(`${this.apiBaseUrl}/scenarios/draft`, { method: "POST", body: scenarioData });
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || data.error || "保存草稿失败");
        return data.data;
    }

    async discardDraft(): Promise<void> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`${this.apiBaseUrl}/scenarios/draft`, { method: "DELETE" });
        if (!response.ok || !data.success) throw new Error(data.message || data.error || "舍弃草稿失败");
    }

    async listTriggerAssets(id: number): Promise<ResourceRef[]> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef[]>>(`${this.apiBaseUrl}/scripts/${id}/assets`);
        if (!response.ok || !data.success || !Array.isArray(data.data)) throw new Error(data.message || "无法加载资源");
        return data.data;
    }

    async uploadTriggerAssets(id: number, files: File[], alt: string[]): Promise<ResourceRef[]> {
        const form = new FormData();
        files.forEach((file) => form.append("files", file, file.name));
        alt.forEach((value) => form.append("alt", value));
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef[]>>(`${this.apiBaseUrl}/scripts/${id}/assets`, { method: "POST", body: form });
        if (!response.ok || !data.success || !Array.isArray(data.data)) throw new Error(data.message || "资源上传失败");
        return data.data;
    }

    async updateTriggerAsset(id: number, assetId: string, patch: Partial<ResourceRef>): Promise<ResourceRef> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef>>(`${this.apiBaseUrl}/scripts/${id}/assets/${encodeURIComponent(assetId)}`, { method: "PUT", body: patch });
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || "资源更新失败");
        return data.data;
    }

    async deleteTriggerAsset(id: number, assetId: string): Promise<void> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse>(`${this.apiBaseUrl}/scripts/${id}/assets/${encodeURIComponent(assetId)}`, { method: "DELETE" });
        if (!response.ok || !data.success) throw new Error(data.message || "资源删除失败");
    }

    async createTriggerCard(id: number, card: Omit<TriggerCard, "scriptId" | "scriptVersion">): Promise<TriggerCard> {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<TriggerCard>>(`${this.apiBaseUrl}/scripts/${id}/triggers`, { method: "POST", body: card });
        if (!response.ok || !data.success || !data.data) throw new Error(data.message || "触发器创建失败");
        return data.data;
    }

    private loadCachedScenarios(): Scenario[] {
        const storedScenarios = localStorage.getItem("trpg_scenarios");
        if (!storedScenarios) return [];

        try {
            const parsed = JSON.parse(storedScenarios) as unknown;
            return Array.isArray(parsed) ? parsed.filter(isScenario) : [];
        } catch (error) {
            console.warn("本地剧本缓存解析失败:", error);
            return [];
        }
    }
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

function isScenarioModule(value: unknown): value is ScenarioModule {
    if (!isRecord(value)) return false;
    return typeof value.id === "string"
        && typeof value.module_type === "string"
        && typeof value.title === "string"
        && typeof value.summary === "string";
}

function normalizeModuleList(value: unknown): ScenarioModule[] {
    if (!Array.isArray(value)) return [];
    return value.filter(isScenarioModule);
}

function isScenarioInput(data: unknown): data is ScenarioInput {
    if (!isRecord(data)) return false;
    if (typeof data.title !== "string" || !data.title.trim()) return false;
    if (typeof data.author !== "string" || !data.author.trim()) return false;

    const playerCount = data.playerCount;
    if (typeof playerCount !== "number" || !Number.isFinite(playerCount)) return false;

    if (data.modules !== undefined && !Array.isArray(data.modules)) return false;
    return true;
}

function isScenario(data: unknown): data is Scenario {
    if (!isScenarioInput(data) || !isRecord(data)) return false;
    if (typeof data.id !== "number") return false;

    const modules = normalizeModuleList(data.modules);
    if (Array.isArray(data.modules) && modules.length !== data.modules.length) return false;
    return true;
}

window.ScenarioModel = ScenarioModel;
