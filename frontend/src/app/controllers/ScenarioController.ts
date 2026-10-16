class ScenarioController {
    private readonly model: ScenarioModel;
    private readonly view: ScenarioView;

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
        } catch (error) {
            console.error("初始化剧本控制器时出错:", error);
            this.view.showMessage(`初始化失败: ${scenarioErrorMessage(error)}`, true);
        }
    }

    private async restoreImportJob(): Promise<void> { const id = sessionStorage.getItem("ai-trpg:scenario-import-job"); if (!id) return; try { const job = await this.model.getImportJob(id); if (job.status === "done") this.view.showMessage("文档导入完成，请审核后发布"); } catch { sessionStorage.removeItem("ai-trpg:scenario-import-job"); } }

    private bindEventHandlers(): void {
        this.view.setEventHandlers({
            onCreateScenarioClick: () => this.onCreateScenarioClick(),
            onImportScenarioDocumentClick: () => this.onImportScenarioDocumentClick(),
            onImportScenarioDocument: (file) => this.onImportScenarioDocumentV2(file),
            onSaveScenario: () => this.onSaveScenario(),
            onSaveDraft: () => this.onSaveDraft(),
            onDiscardDraft: () => this.onDiscardDraft(),
            onPreviewScenario: (id) => this.onPreviewScenario(id),
            onEditScenario: (id) => this.onEditScenario(id),
            onPlayScenario: (id) => this.onPlayScenario(id),
            onDeleteScenario: (id) => this.onDeleteScenario(id),
            onBatchDeleteScenarios: (ids) => this.onBatchDeleteScenarios(ids),
            onImportScenario: (files) => this.onImportScenario(files),
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

    private async onImportScenarioDocumentV2(file: File): Promise<void> {
        const title = file.name.replace(/\.[^.]+$/, "") || "Imported scenario";
        const choice = await this.view.showImportChoice();
        if (choice === "cancel") return;
        // 直接导入前先让用户确认剧本基础信息，取消则中止本次导入。
        const metadata = choice === "direct"
            ? await this.view.showDirectImportMetadata({ title, creator: window.currentUser?.username || "" })
            : null;
        if (choice === "direct" && !metadata) return;
        this.view.showConversionProgress(file.name);
        this.view.updateConversionProgress(0, "active", "Uploading document");
        try {
            if (choice === "direct" && metadata) {
                const form = new FormData();
                form.append("file", file, file.name);
                form.append("title", metadata.title);
                form.append("author", metadata.author);
                form.append("creator", metadata.creator);
                form.append("playerCount", String(metadata.playerCount));
                form.append("description", metadata.description);
                const started = await this.model.createImportJob(form, (value) => {
                    this.view.updateConversionProgress(0, "active", `${Math.round(value)}% uploaded`);
                });
                sessionStorage.setItem("ai-trpg:scenario-import-job", started.jobId);
                const finished = await this.model.waitForImportJob(started.jobId, (progress) => this.view.updateImportJobProgress(progress));
                const scenario = await this.model.publishImport(started.scriptId || finished.script_id, started.jobId);
                sessionStorage.removeItem("ai-trpg:scenario-import-job");
                this.view.updateConversionProgress(3, "complete", "Vector index ready");
                this.view.closeConversionProgress();
                this.renderScenarioList();
                this.view.showMessage(`Direct import completed: ${scenario.title}`);
                return;
            }

            // Review mode keeps the existing editor flow, but the choice is
            // made before parsing starts and the source can be replaced by
            // selecting another document if the generated draft is unsuitable.
            this.view.updateConversionProgress(0, "complete");
            this.view.updateConversionProgress(1, "active", "Extracting sections");
            const converted = await this.model.convertScriptFile(file, title);
            this.view.updateConversionProgress(1, "complete");
            this.view.updateConversionProgress(2, "complete", "Draft ready for review");
            this.view.updateConversionProgress(3, "complete");
            this.view.closeConversionProgress();
            await this.view.openCreateModal();
            this.view.fillDraftData(converted);
            this.view.setImportReviewReadOnly(true);
            this.view.showMessage("Review mode is ready. Upload another document to replace this draft, or publish it after checking.");
        } catch (error) {
            this.view.updateConversionProgress(1, "error", scenarioErrorMessage(error));
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            this.view.closeConversionProgress();
            this.view.showMessage(`Scenario import failed: ${scenarioErrorMessage(error)}`, true);
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
                try {
                    const scenarioData = this.view.getFormData();
                    const tempScenario: Pick<Scenario, "id" | "cover" | "title"> = {
                        id,
                        title: scenarioData.title,
                        cover: scenarioData.cover || "",
                    };
                    await this.renameScenarioCover(tempScenario, scenarioData.cover || "");
                    scenarioData.cover = tempScenario.cover || "";

                    const updatedScenario = await this.model.updateScenario(id, scenarioData);
                    void updatedScenario;

                    restoreCreateHandler();
                    this.renderScenarioList();
                    this.view.closeModal();
                    this.view.showMessage("剧本更新成功");
                } catch (error) {
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
        if (!confirm("确定要删除这个剧本吗？")) return;

        try {
            await this.model.deleteScenario(id);
            this.renderScenarioList();
            this.view.showMessage("剧本删除成功");
        } catch (error) {
            console.error("删除剧本时出错:", error);
            this.view.showMessage(`删除剧本失败: ${scenarioErrorMessage(error)}`, true);
        }
    }

    private async onBatchDeleteScenarios(ids: number[]): Promise<void> {
        let deleted = 0;
        const failures: string[] = [];
        for (const id of ids) {
            try {
                await this.model.deleteScenario(id);
                deleted += 1;
            } catch (error) {
                failures.push(scenarioErrorMessage(error));
                console.error(`批量删除剧本 ${id} 失败:`, error);
            }
        }
        this.renderScenarioList();
        if (failures.length === 0) {
            this.view.showMessage(`已删除 ${deleted} 个剧本`);
        } else {
            this.view.showMessage(`已删除 ${deleted} 个剧本，${failures.length} 个失败。\n\n失败详情:\n${failures.join("\n")}`, true);
        }
    }

    private async onImportScenario(files: FileList | null): Promise<void> {
        if (!files || files.length === 0) return;

        let successCount = 0;
        let errorCount = 0;
        const errors: string[] = [];

        for (const file of Array.from(files)) {
            try {
                const content = await this.readFile(file);
                await this.model.importScenario(JSON.parse(content) as unknown);
                successCount += 1;
            } catch (error) {
                errorCount += 1;
                errors.push(`"${file.name}": ${scenarioErrorMessage(error)}`);
                console.error(`导入文件 "${file.name}" 失败:`, error);
            }
        }

        this.renderScenarioList();

        if (successCount > 0 && errorCount === 0) {
            this.view.showMessage(`成功导入 ${successCount} 个剧本`);
        } else if (successCount > 0) {
            this.view.showMessage(`成功导入 ${successCount} 个剧本，${errorCount} 个失败。\n\n失败详情:\n${errors.join("\n")}`);
        } else {
            this.view.showMessage(`导入失败：\n\n错误详情:\n${errors.join("\n")}`, true);
        }
    }

    private readFile(file: File): Promise<string> {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result || ""));
            reader.onerror = () => reject(new Error("文件读取失败"));
            reader.readAsText(file);
        });
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

window.ScenarioController = ScenarioController;
