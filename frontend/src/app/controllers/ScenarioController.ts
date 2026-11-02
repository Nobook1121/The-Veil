class ScenarioController {
    private readonly model: ScenarioModel;
    private readonly view: ScenarioView;
    /** 后台导入任务徽标的轮询计时器。 */
    private importRefreshTimer: number | null = null;
    /** 已提示过的任务状态，避免每次轮询重复弹 Toast。 */
    private readonly importJobStates = new Map<string, string>();
    /** 正在打开审核弹窗的任务，防止前台轮询与后台轮询重复弹窗。 */
    private readonly reviewingJobs = new Set<string>();

    constructor() {
        this.model = new ScenarioModel();
        this.view = new ScenarioView();
        this.bindEventHandlers();
        void this.init();
    }

    private async init(): Promise<void> {
        try {
            await this.model.init();
            this.renderScenarioList();
            await this.restoreImportJob();
            this.startImportJobsPolling();
        } catch (error) {
            console.error("初始化剧本控制器时出错:", error);
            this.view.showMessage(`初始化失败: ${scenarioErrorMessage(error)}`, true);
        }
    }

    private async restoreImportJob(): Promise<void> {
        sessionStorage.removeItem("ai-trpg:scenario-import-job");
        await this.refreshImportJobs();
    }

    private startImportJobsPolling(): void {
        if (this.importRefreshTimer !== null) window.clearInterval(this.importRefreshTimer);
        this.importRefreshTimer = window.setInterval(() => { void this.refreshImportJobs(); }, 4000);
    }

    /** 轮询服务端任务列表，渲染剧本页顶部的后台导入徽标并提示状态变化。 */
    private async refreshImportJobs(): Promise<void> {
        if (!this.model.isAuthenticated) return;
        try {
            const jobs = await this.model.listImportJobs();
            this.view.renderImportJobs(jobs);
            this.notifyImportJobChanges(jobs);
        } catch {
            // 轮询失败多半是网络抖动或未登录，不应打断当前操作。
        }
    }

    private notifyImportJobChanges(jobs: ScenarioImportJob[]): void {
        for (const job of jobs) {
            const previous = this.importJobStates.get(job.id);
            this.importJobStates.set(job.id, job.status);
            if (previous === undefined || previous === job.status) continue;
            const name = job.source_filename || job.metadata?.title || "剧本文档";
            if (job.status === "done") {
                this.view.showMessage(`后台导入完成：${name}，可在剧本页顶部审核并发布`);
            } else if (job.status === "published") {
                this.view.showMessage(`后台导入已自动发布：${name}`);
                void this.model.loadScenarios().then(() => this.renderScenarioList()).catch(() => undefined);
            } else if (job.status === "failed") {
                this.view.showMessage(`后台导入失败：${job.error || name}`, true);
            } else if (job.status === "cancelled") {
                this.view.showMessage(`后台导入已取消：${name}`);
            }
        }
    }

    private bindEventHandlers(): void {
        this.view.setEventHandlers({
            onCreateScenarioClick: () => this.onCreateScenarioClick(),
            onImportScenarioEntryClick: () => this.onImportScenarioEntryClick(),
            onImportScenarioDocumentClick: () => this.onImportScenarioDocumentClick(),
            onImportScenarioDocument: (file) => this.onImportScenarioDocumentV2(file),
            onImportScenarioBundle: (file) => this.onImportScenarioBundle(file),
            onSaveScenario: () => this.onSaveScenario(),
            onSaveDraft: () => this.onSaveDraft(),
            onDiscardDraft: () => this.onDiscardDraft(),
            onPreviewScenario: (id) => this.onPreviewScenario(id),
            onEditScenario: (id) => this.onEditScenario(id),
            onPlayScenario: (id) => this.onPlayScenario(id),
            onDeleteScenario: (id) => this.onDeleteScenario(id),
            onBatchDeleteScenarios: (ids) => this.onBatchDeleteScenarios(ids),
            onImportJobOpen: (job) => this.onImportJobOpen(job),
            onImportJobRetry: (job) => this.onImportJobRetry(job),
            onImportJobAbandon: (job) => this.onImportJobAbandon(job),
        });
    }

    private renderScenarioList(): void {
        this.view.renderScenarioList(this.model.getScenarios());
    }

    refresh(): void {
        this.renderScenarioList();
    }

    private async onCreateScenarioClick(): Promise<void> {
        window.setCurrentEditingScenarioId?.(null);
        try {
            const draft = await this.model.loadDraft();
            if (draft) {
                const draftAction = await this.view.showDraftPrompt();
                if (draftAction === "cancel") return;
                this.view.openCreateModal();
                if (draftAction === "continue") {
                    this.view.fillDraftData?.(draft);
                } else if (draftAction === "discard") {
                    await this.model.discardDraft();
                }
                return;
            }
        } catch (error) {
            console.warn("加载剧本草稿失败", error);
        }
        this.view.openCreateModal();
    }

    /** 统一导入入口：先选原始文档还是整理好的剧本包，再打开对应文件选择框。 */
    private async onImportScenarioEntryClick(): Promise<void> {
        const choice = await this.view.showImportEntryChoice();
        if (choice === "cancel") return;
        if (choice === "document") {
            await this.onImportScenarioDocumentClick();
            return;
        }
        inputElement("importScenarioFile")?.click();
    }

    private async onImportScenarioDocumentClick(): Promise<void> {
        window.setCurrentEditingScenarioId?.(null);
        try {
            const draft = await this.model.loadDraft();
            if (draft) {
                const draftAction = await this.view.showDraftPrompt();
                if (draftAction === "cancel") return;
                if (draftAction === "continue") {
                    this.view.openCreateModal();
                    this.view.fillDraftData?.(draft);
                    this.view.showMessage("请先完成或舍弃当前草稿，再导入新的剧本文档");
                    return;
                }
                await this.model.discardDraft();
            }
        } catch (error) {
            console.warn("加载剧本草稿失败", error);
        }
        inputElement("importScenarioDocumentFile")?.click();
    }

    /** 导入 Skill / 外部 AI 生成的剧本包：不调用 AI，校验与修复后直接发布。 */
    private async onImportScenarioBundle(file: File): Promise<void> {
        this.view.showConversionProgress(file.name, false);
        this.view.setImportProgressHandlers(null);
        this.view.updateConversionProgress(0, "active", "正在校验剧本包");
        try {
            const started = await this.model.importBundle(file);
            const finished = await this.model.waitForImportJob(started.jobId, (progress) => this.view.updateImportJobProgress(progress));
            const scenario = await this.model.publishImport(started.scriptId || finished.script_id, started.jobId);
            this.view.updateConversionProgress(3, "complete", "剧本包已导入并发布");
            this.view.closeConversionProgress();
            this.renderScenarioList();
            this.view.showMessage(`剧本包已导入并发布：${scenario.title}`);
        } catch (error) {
            this.view.updateConversionProgress(0, "error", scenarioErrorMessage(error));
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            this.view.closeConversionProgress();
            this.view.showMessage(`剧本包导入失败：${scenarioErrorMessage(error)}`, true);
        }
    }

    private async onImportScenarioDocumentV2(file: File): Promise<void> {
        const title = file.name.replace(/\.[^.]+$/, "") || "Imported scenario";
        if (file.name.toLowerCase().endsWith(".json")) {
            await this.onImportScenarioBundle(file);
            return;
        }
        const choice = await this.view.showImportChoice();
        if (choice === "cancel") return;
        // 直接导入 / AI 全链路导入前先让用户确认剧本基础信息，取消则中止本次导入。
        const needsJobImport = choice === "direct" || choice === "ai";
        const metadata = needsJobImport
            ? await this.view.showDirectImportMetadata({ title, creator: window.currentUser?.username || "" })
            : null;
        if (needsJobImport && !metadata) return;
        this.view.showConversionProgress(file.name, needsJobImport);
        this.view.updateConversionProgress(0, "active", choice === "ai" ? "正在通读剧本" : "正在上传文档");
        const foreground = { active: true };
        let jobId = "";
        this.view.setImportProgressHandlers({
            onBackground: () => {
                foreground.active = false;
                this.view.closeConversionProgress();
                this.view.showMessage("导入已转入后台，可继续使用其它页面；完成后会在剧本页提示你");
                void this.refreshImportJobs();
            },
            onCancel: async () => {
                foreground.active = false;
                try {
                    if (jobId) await this.model.cancelImportJob(jobId);
                } catch (error) {
                    this.view.showMessage(`取消导入失败：${scenarioErrorMessage(error)}`, true);
                }
                this.view.closeConversionProgress();
                this.view.showMessage("已请求取消导入；已完成的阶段会保留，重试时可复用");
                void this.refreshImportJobs();
            },
        });
        try {
            if (needsJobImport && metadata) {
                const form = new FormData();
                form.append("file", file, file.name);
                form.append("mode", choice);
                form.append("title", metadata.title);
                form.append("author", metadata.author);
                form.append("creator", metadata.creator);
                form.append("playerCount", String(metadata.playerCount));
                form.append("description", metadata.description);
                form.append("autoPublish", metadata.autoPublish ? "1" : "");
                const started = await this.model.createImportJob(form, (value) => {
                    if (foreground.active) this.view.updateConversionProgress(0, "active", `已上传 ${Math.round(value)}%`);
                });
                jobId = started.jobId;
                const finished = await this.model.waitForImportJob(started.jobId, (progress) => {
                    this.importJobStates.set(progress.id, progress.status);
                    if (foreground.active) this.view.updateImportJobProgress(progress);
                });
                this.view.setImportProgressHandlers(null);
                if (foreground.active) {
                    this.view.updateConversionProgress(3, "complete", "知识库与图谱已就绪");
                    this.view.closeConversionProgress();
                }
                await this.refreshImportJobs();
                if (finished.status === "published") {
                    // 勾选了「完成后自动发布」：后台已经落盘，这里只需刷新列表。
                    await this.model.loadScenarios();
                    this.renderScenarioList();
                    this.view.showMessage(`剧本已导入并自动发布：${finished.metadata?.title || metadata.title}`);
                } else if (foreground.active) {
                    await this.reviewImportJob(finished);
                } else {
                    this.view.showMessage(`后台导入完成：${finished.metadata?.title || metadata.title}，可在剧本页顶部审核并发布`);
                }
                return;
            }

            // Review mode keeps the existing editor flow, but the choice is
            // made before parsing starts and the source can be replaced by
            // selecting another document if the generated draft is unsuitable.
            this.view.updateConversionProgress(0, "complete");
            this.view.updateConversionProgress(1, "active", "正在提取章节");
            const converted = await this.model.convertScriptFile(file, title);
            this.view.updateConversionProgress(1, "complete");
            this.view.updateConversionProgress(2, "complete", "草稿已生成，等待审核");
            this.view.updateConversionProgress(3, "complete");
            this.view.closeConversionProgress();
            await this.view.openCreateModal();
            this.view.fillDraftData(converted);
            this.view.setImportReviewReadOnly(true);
            this.view.showMessage("已进入审核模式：可重新上传文档替换草稿，或检查后发布。");
        } catch (error) {
            this.view.setImportProgressHandlers(null);
            foreground.active = false;
            this.view.updateConversionProgress(1, "error", scenarioErrorMessage(error));
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            this.view.closeConversionProgress();
            this.view.showMessage(`剧本导入失败：${scenarioErrorMessage(error)}`, true);
            void this.refreshImportJobs();
        }
    }

    /** 后台任务卡片「查看进度 / 审核并发布」。 */
    private async onImportJobOpen(job: ScenarioImportJob): Promise<void> {
        if (job.status === "done") {
            await this.reviewImportJob(job);
            return;
        }
        if (job.status === "failed" || job.status === "cancelled") {
            this.view.showMessage(job.error || "该导入任务已结束，可点击「重试」继续（已完成的阶段会复用）", true);
            return;
        }
        // 仍在后台跑：重新贴回进度弹窗，并继续轮询直到结束。
        this.view.showConversionProgress(job.source_filename || "剧本文档");
        this.view.updateImportJobProgress(job);
        this.view.setImportProgressHandlers({
            onBackground: () => { this.view.closeConversionProgress(); this.view.setImportProgressHandlers(null); },
            onCancel: async () => {
                try { await this.model.cancelImportJob(job.id); } catch (error) { this.view.showMessage(scenarioErrorMessage(error), true); }
                this.view.closeConversionProgress();
                this.view.setImportProgressHandlers(null);
                void this.refreshImportJobs();
            },
        });
        try {
            const finished = await this.model.waitForImportJob(job.id, (progress) => this.view.updateImportJobProgress(progress));
            this.view.closeConversionProgress();
            this.view.setImportProgressHandlers(null);
            await this.refreshImportJobs();
            if (finished.status === "done") await this.reviewImportJob(finished);
        } catch (error) {
            this.view.closeConversionProgress();
            this.view.setImportProgressHandlers(null);
            this.view.showMessage(`后台导入任务结束：${scenarioErrorMessage(error)}`, true);
            void this.refreshImportJobs();
        }
    }

    private async onImportJobRetry(job: ScenarioImportJob): Promise<void> {
        try {
            await this.model.retryImportJob(job.id, job.current_stage !== "pending" ? job.current_stage : undefined);
            this.view.showMessage("已重新开始导入，已完成的阶段会直接复用");
            await this.refreshImportJobs();
        } catch (error) {
            this.view.showMessage(`重试失败：${scenarioErrorMessage(error)}`, true);
        }
    }

    /** 放弃失败/已取消的导入：删除任务目录以及已经写入的向量、知识索引和半成品剧本。 */
    private async onImportJobAbandon(job: ScenarioImportJob): Promise<void> {
        const confirmed = await window.TrpgDialog?.confirm({
            title: "放弃这次导入",
            message: "将删除这次导入的临时文件、已生成的向量与知识索引，之后无法恢复。确定放弃吗？",
            confirmText: "放弃",
            danger: true,
        });
        if (!confirmed) return;
        try {
            await this.model.abandonImportJob(job.id);
            this.importJobStates.delete(job.id);
            await this.refreshImportJobs();
            this.view.showMessage("已放弃本次导入，已导入的临时内容与向量已清理");
        } catch (error) {
            this.view.showMessage(`放弃导入失败：${scenarioErrorMessage(error)}`, true);
        }
    }

    /** 后台导入完成后的审核与发布：用户可修改基础信息，确认后落盘。 */
    private async reviewImportJob(job: ScenarioImportJob): Promise<void> {
        if (this.reviewingJobs.has(job.id)) return;
        let current = job;
        if (!current.preview) {
            try { current = await this.model.getImportJob(job.id); } catch { /* 下面统一提示 */ }
        }
        if (!current.preview) {
            this.view.showMessage("导入已完成，但预览数据缺失；请在任务卡片上点击「重试」重新生成", true);
            return;
        }
        const preview = current.preview;
        this.reviewingJobs.add(job.id);
        try {
            const edited = await this.view.showImportReview(preview, current);
            if (!edited) return;
            await this.model.saveImportPreview(current.id, { ...preview, ...edited });
            const scenario = await this.model.publishImport(current.script_id, current.id);
            await this.model.loadScenarios();
            this.renderScenarioList();
            await this.refreshImportJobs();
            this.view.showMessage(`剧本已发布：${scenario.title}`);
        } catch (error) {
            this.view.showMessage(`发布失败：${scenarioErrorMessage(error)}`, true);
        } finally {
            this.reviewingJobs.delete(job.id);
        }
    }

    private async onImportScenarioDocument(file: File): Promise<void> {
        const extension = file.name.toLowerCase().split(".").pop() || "";
        if (!["doc", "docx", "pdf", "txt", "md", "markdown", "text"].includes(extension)) {
            this.view.showMessage("仅支持 doc、docx、txt、md 文档", true);
            return;
        }
        this.view.showConversionProgress(file.name);
        this.view.updateConversionProgress(0, "active");
        try {
            await new Promise((resolve) => window.setTimeout(resolve, 120));
            this.view.updateConversionProgress(0, "complete");
            this.view.updateConversionProgress(1, "active");
            const title = file.name.replace(/\.[^.]+$/, "") || "Imported scenario";
            const converted = await this.model.convertScriptFile(file, title);
            this.view.updateConversionProgress(1, "complete");
            this.view.updateConversionProgress(2, "active");
            await new Promise((resolve) => window.setTimeout(resolve, 120));
            this.view.updateConversionProgress(2, "complete");
            this.view.updateConversionProgress(3, "active");
            await new Promise((resolve) => window.setTimeout(resolve, 120));
            this.view.updateConversionProgress(3, "complete");
            await new Promise((resolve) => window.setTimeout(resolve, 180));
            this.view.closeConversionProgress();
            const choice = await this.view.showImportChoice();
            if (choice === "cancel") return;
            if (choice === "direct") {
                const created = await this.model.createScenario(converted);
                await this.model.updateScenario(created.id, { ...converted, id: created.id });
                this.renderScenarioList();
                this.view.showMessage("文档已直接导入并完成知识库向量化");
                return;
            }
            await this.view.openCreateModal();
            this.view.fillDraftData(converted);
            const conversionStatus = (converted as ScenarioInput & { conversion?: { ai?: { status?: string } } }).conversion?.ai?.status;
            this.view.showMessage(conversionStatus === "fallback"
                ? "AI 转换暂时超时，已使用本地结构化结果填入创建剧本窗口，请检查后发布"
                : "文档转换完成，已填入创建剧本窗口，请检查后发布");
        } catch (error) {
            this.view.updateConversionProgress(1, "error", scenarioErrorMessage(error));
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            this.view.closeConversionProgress();
            this.view.showMessage(`剧本文档转换失败：${scenarioErrorMessage(error)}`, true);
        }
    }

    private async onSaveDraft(): Promise<void> {
        try { await this.model.saveDraft(this.view.getDraftData()); this.view.showMessage("剧本草稿已保存"); }
        catch (error) { this.view.showMessage(scenarioErrorMessage(error), true); }
    }

    /** 用户在关闭编辑器的草稿确认框中选择「不保留」时删除已有草稿。 */
    private async onDiscardDraft(): Promise<void> {
        try { await this.model.discardDraft(); } catch { /* 草稿可能本就不存在，忽略即可 */ }
    }

    private async onSaveScenario(): Promise<void> {
        try {
            const scenarioData = this.view.getFormData();
            const scenario = await this.model.createScenario(scenarioData);
            await this.model.discardDraft().catch(() => undefined);

            const coverUrl = inputValue("scenarioCoverUrl");
            await this.renameScenarioCover(scenario, coverUrl);
            scenarioData.cover = scenario.cover || "";
            const savedScenario = await this.model.updateScenario(scenario.id, {
                ...scenarioData,
                id: scenario.id,
            });

            const scenarioIndex = this.model.scenarios.findIndex((item) => item.id === scenario.id);
            if (scenarioIndex !== -1) {
                this.model.scenarios[scenarioIndex] = savedScenario;
                this.model.saveScenarios();
            }

            this.renderScenarioList();
            this.view.closeModal();
            this.view.showMessage("剧本保存成功");

            setTimeout(() => {
                location.reload();
            }, 1000);
        } catch (error) {
            console.error("保存剧本时出错:", error);
            this.view.showMessage(scenarioErrorMessage(error), true);
        }
    }

    private async onPreviewScenario(id: number): Promise<void> {
        const scenario = this.model.getScenario(id);
        if (scenario) {
            try {
                this.view.previewScenario(scenario, await this.model.getKnowledgeStats(id));
            } catch {
                this.view.previewScenario(scenario, { vector_count: 0, backend: "unknown" });
            }
        } else {
            this.view.showMessage("剧本不存在", true);
        }
    }

    private async onEditScenario(id: number): Promise<void> {
        const scenario = this.model.getScenario(id);
        if (!scenario) {
            this.view.showMessage("剧本不存在", true);
            return;
        }

        // 直接导入的剧本与其他剧本共用同一个编辑界面（含场景卡、触发器资源库等）；
        // 区别只在后端：仅修改基础信息时不会升版本、也不会重新嵌入。
        window.setCurrentEditingScenarioId?.(id);
        this.view.openEditModal(scenario);

        const saveButton = document.getElementById("saveScenario");
        if (!saveButton) return;

        saveButton.removeEventListener("click", this.view.saveScenarioHandler);
        const restoreCreateHandler = () => {
            // 编辑流程结束后恢复“保存并发布”按钮的默认创建行为，避免残留的编辑
            // 回调与新建监听器同时触发，导致一次点击被重复提交。
            saveButton.onclick = null;
            saveButton.addEventListener("click", this.view.saveScenarioHandler);
        };
        saveButton.onclick = () => {
            void runSaveButtonCooldown(saveButton, async () => {
                const complete = () => {
                    restoreCreateHandler();
                    this.renderScenarioList();
                    this.view.closeModal();
                    this.view.showMessage("剧本更新成功");
                };
                let scenarioData: ScenarioInput | null = null;
                try {
                    scenarioData = this.view.getFormData();
                    const tempScenario: Pick<Scenario, "id" | "cover" | "title"> = {
                        id,
                        title: scenarioData.title,
                        cover: scenarioData.cover || "",
                    };
                    await this.renameScenarioCover(tempScenario, scenarioData.cover || "");
                    scenarioData.cover = tempScenario.cover || "";

                    const updatedScenario = await this.model.updateScenario(id, scenarioData);
                    void updatedScenario;
                    complete();
                } catch (error) {
                    const blockers = publishBlockerDetail(error);
                    if (blockers && scenarioData) {
                        const confirmed = await window.TrpgDialog?.confirm({
                            title: "世界树校验未通过",
                            message: publishBlockerMessage(blockers),
                            confirmText: "强制发布",
                            danger: true,
                        });
                        if (confirmed) {
                            try {
                                await this.model.updateScenario(id, { ...scenarioData, force_narrative: true });
                                complete();
                                return;
                            } catch (retryError) {
                                console.error("强制发布剧本时出错:", retryError);
                                this.view.showMessage(scenarioErrorMessage(retryError), true);
                                restoreCreateHandler();
                                return;
                            }
                        }
                        restoreCreateHandler();
                        return;
                    }
                    console.error("更新剧本时出错:", error);
                    this.view.showMessage(scenarioErrorMessage(error), true);
                    restoreCreateHandler();
                }
            });
        };
    }

    private onPlayScenario(id: number): void {
        window.switchMainTab?.("save");
        void window.openCreateRoomWithScenario?.(id);
    }

    private async onDeleteScenario(id: number): Promise<void> {
        const confirmed = await window.TrpgDialog?.confirm({
            title: "删除剧本",
            message: "确定要删除这个剧本吗？",
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;

        try {
            const result = await this.model.deleteScenario(id);
            this.renderScenarioList();
            if (result.archived) {
                this.view.showMessage(`该剧本仍被 ${result.activeRooms} 个进行中的房间使用，已归档并从列表隐藏；归档/删除这些房间后再删除，才会彻底清理知识库`);
            } else {
                this.view.showMessage("剧本删除成功，知识库向量与索引已一并清理");
            }
        } catch (error) {
            console.error("删除剧本时出错:", error);
            this.view.showMessage(`删除剧本失败: ${scenarioErrorMessage(error)}`, true);
        }
    }

    private async onBatchDeleteScenarios(ids: number[]): Promise<void> {
        let deleted = 0;
        let archived = 0;
        const failures: string[] = [];
        for (const id of ids) {
            try {
                const result = await this.model.deleteScenario(id);
                if (result.archived) archived += 1;
                else deleted += 1;
            } catch (error) {
                failures.push(scenarioErrorMessage(error));
                console.error(`批量删除剧本 ${id} 失败:`, error);
            }
        }
        this.renderScenarioList();
        if (failures.length === 0) {
            const archivedHint = archived > 0 ? `，${archived} 个仍被房间使用已归档（知识库保留）` : "";
            this.view.showMessage(`已删除 ${deleted} 个剧本${archivedHint}`);
        } else {
            this.view.showMessage(`已删除 ${deleted} 个剧本，${failures.length} 个失败。\n\n失败详情:\n${failures.join("\n")}`, true);
        }
    }

    async renameScenarioCover<T extends Pick<Scenario, "id" | "cover"> & Partial<Pick<Scenario, "title">>>(
        scenario: T,
        coverUrl: string,
    ): Promise<T> {
        // 无封面（或封面地址不在受支持的目录下）时统一按「无封面」处理，
        // 由前端渲染剧本名文字封面；项目已不再有内置默认封面图。
        if (!coverUrl || (!coverUrl.startsWith("/assets/scenarios/") && !coverUrl.startsWith("/assets/scenario_covers/"))) {
            scenario.cover = "";
            return scenario;
        }

        try {
            const oldCoverPath = coverUrl.startsWith("/assets/scenarios/")
                ? coverUrl.replace("/assets/scenarios/", "")
                : coverUrl.replace("/assets/scenario_covers/", "scenario-covers/");
            if (!oldCoverPath) {
                scenario.cover = "";
                return scenario;
            }

            const newCoverPath = `scenario-${scenario.id}/cover.png`;

            if (oldCoverPath === newCoverPath) {
                scenario.cover = `/assets/scenarios/${newCoverPath}`;
                return scenario;
            }

            const data = await TrpgApi.post<ApiResponse>("/api/scenarios/cover/rename", {
                old_path: oldCoverPath,
                new_path: newCoverPath,
            });

            scenario.cover = data.success
                ? `/assets/scenarios/${newCoverPath}`
                : "";
        } catch (error) {
            console.error("重命名封面文件时出错:", error);
            scenario.cover = "";
        }

        return scenario;
    }
}

function inputValue(id: string): string {
    return (document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null)?.value || "";
}

function inputElement(id: string): HTMLInputElement | null {
    const element = document.getElementById(id);
    return element instanceof HTMLInputElement ? element : null;
}

function scenarioErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

interface PublishBlockerDetail {
    errors?: number;
    needs_review?: number;
    issues?: { message?: string }[];
}

/** 后端以 `code=narrative_publish_blocked` 标记世界树校验拦截（§14.2 硬闸门）。 */
function publishBlockerDetail(error: unknown): PublishBlockerDetail | null {
    const candidate = error as { code?: string; detail?: unknown } | null;
    if (!candidate || candidate.code !== "narrative_publish_blocked") return null;
    return candidate.detail && typeof candidate.detail === "object"
        ? (candidate.detail as PublishBlockerDetail)
        : {};
}

function publishBlockerMessage(detail: PublishBlockerDetail): string {
    const errors = Number(detail.errors ?? 0);
    const review = Number(detail.needs_review ?? 0);
    const parts: string[] = [];
    if (errors > 0) parts.push(`${errors} 条校验错误`);
    if (review > 0) parts.push(`${review} 项可见性待复核`);
    const first = detail.issues?.find((issue) => issue.message)?.message;
    const head = `世界树尚有${parts.join("、") || "未解决的问题"}。`;
    const tail = first ? `\n首个问题：${first}` : "";
    return `${head}${tail}\n\n强制发布会写入日志，玩家侧可能出现剧本断裂或未复核的隐藏内容。`;
}

window.ScenarioController = ScenarioController;
