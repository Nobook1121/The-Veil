/**
 * 剧本世界树 / 图谱：生成向导、任务轮询与应用到草稿（§8.7）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, showTab } = N;

    function generateSource(): string {
        const checked = document.querySelector<HTMLInputElement>('input[name="scenarioGenerateSource"]:checked');
        return checked?.value || "document";
    }

    function syncGenerateSource(): void {
        const premise = byId<HTMLTextAreaElement>("scenarioGeneratePremise");
        if (premise) premise.disabled = generateSource() !== "premise";
    }

    function collectGenerateOptions(): Record<string, unknown> {
        return {
            source: generateSource(),
            premise: byId<HTMLTextAreaElement>("scenarioGeneratePremise")?.value.trim() || "",
            topology: byId<HTMLSelectElement>("scenarioGenerateTopology")?.value || "auto",
            acts: Number(byId<HTMLInputElement>("scenarioGenerateActs")?.value || 2),
            scenes_per_act: Number(byId<HTMLInputElement>("scenarioGenerateScenes")?.value || 3),
            endings: Number(byId<HTMLInputElement>("scenarioGenerateEndings")?.value || 0),
            clocks: Number(byId<HTMLInputElement>("scenarioGenerateClocks")?.value || 0),
            branch: byId<HTMLSelectElement>("scenarioGenerateBranch")?.value || "medium",
            free_order: byId<HTMLInputElement>("scenarioGenerateFreeOrder")?.checked !== false,
            ai_fill: byId<HTMLInputElement>("scenarioGenerateAiFill")?.checked === true,
        };
    }

    function setGenerateStatus(text: string, variant: "secondary" | "success" | "warning" | "danger" = "secondary"): void {
        const status = byId("scenarioGenerateStatus");
        if (!status) return;
        status.textContent = text;
        status.className = variant === "secondary" ? "small text-muted" : `small text-${variant}`;
    }

    function renderGeneratePreview(result: GenerateResult | null): void {
        const preview = byId("scenarioGeneratePreview");
        if (!preview) return;
        if (!result) {
            preview.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.generate.preview.empty", "还没有生成预览。"))}</p>`;
            return;
        }
        const counts = result.diff?.counts || {};
        const stat = (label: string, value: unknown): string => `
            <div class="scenario-generate-preview-stat">
                <strong>${escapeHtml(value ?? "-")}</strong>
                <span class="text-muted small">${escapeHtml(label)}</span>
            </div>`;
        const badge = (variant: "added" | "removed" | "modified" | "info", label: string, value: unknown): string =>
            `<span class="scenario-generate-badge ${variant}">${escapeHtml(label)} ${escapeHtml(value ?? 0)}</span>`;
        const warnings = result.warnings?.length
            ? `<ul class="small text-muted mb-0 mt-2">${result.warnings
                .slice(0, 8)
                .map((item) => `<li>${escapeHtml(item.message)}</li>`)
                .join("")}</ul>`
            : "";
        preview.innerHTML = `
            <div class="scenario-generate-preview-summary">
                ${stat(t("scenario.generate.preview.nodes", "节点"), result.bundle.nodes.length)}
                ${stat(t("scenario.generate.preview.edges", "边"), result.bundle.edges.length)}
                ${stat(t("scenario.generate.preview.topology", "拓扑"), result.topology?.topology || "-")}
                ${stat(t("scenario.generate.preview.warnings", "提示"), result.warnings?.length || 0)}
            </div>
            <div class="scenario-generate-preview-changes">
                ${badge("added", t("scenario.generate.preview.added", "新增"), counts.nodes_added)}
                ${badge("modified", t("scenario.generate.preview.modified", "修改"), counts.nodes_modified)}
                ${badge("removed", t("scenario.generate.preview.removed", "移除"), counts.nodes_removed)}
                ${badge("info", t("scenario.generate.preview.free_edges", "自由边"), counts.free_edges)}
                ${badge("info", t("scenario.generate.preview.gated_edges", "门控边"), counts.gated_edges)}
                ${badge("info", t("scenario.generate.preview.soft_edges", "软边"), counts.soft_edges)}
            </div>
            ${warnings}`;
    }

    function syncGeneratePanel(): void {
        const run = byId<HTMLButtonElement>("scenarioGenerateRun");
        if (run) run.disabled = !editable() || state.generateBusy;
        const apply = byId<HTMLButtonElement>("scenarioGenerateApply");
        if (apply) apply.disabled = !state.generateResult || !editable() || state.generateBusy;
        const cancel = byId<HTMLButtonElement>("scenarioGenerateCancel");
        if (cancel) {
            const active = state.generateBusy && Boolean(state.generateJobId);
            cancel.hidden = !active;
            cancel.disabled = !active;
        }
    }

    function resetGeneratePanel(): void {
        state.generateJobId = "";
        state.generateResult = null;
        state.generateBusy = false;
        setGenerateStatus("");
        renderGeneratePreview(null);
        syncGeneratePanel();
    }

    async function pollGenerateJob(jobId: string): Promise<GenerateResult> {
        for (let attempt = 0; attempt < 300; attempt += 1) {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<GenerateJobPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/jobs/${encodeURIComponent(jobId)}`,
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const job = data.data.job;
            setGenerateStatus(`${job.progress || 0}% · ${job.stage || job.status}`);
            if (job.status === "cancelled") {
                const cancelled = new Error(t("scenario.narrative.job.cancelled", "已取消生成。"));
                cancelled.name = "GenerationCancelled";
                throw cancelled;
            }
            if (job.status === "failed") {
                throw new Error(job.error || t("scenario.generate.failed", "生成失败"));
            }
            if (job.status === "done") {
                if (!data.data.result) throw new Error(t("scenario.generate.failed", "生成失败"));
                return data.data.result;
            }
            await new Promise((resolve) => window.setTimeout(resolve, 600));
        }
        throw new Error(t("scenario.generate.timeout", "生成超时，请稍后重试。"));
    }

    async function startGeneration(): Promise<void> {
        if (!state.scenarioId || !editable()) {
            showNotification(t("scenario.generate.readonly", "只读剧本不能生成世界树。"), "warning");
            return;
        }
        state.generateBusy = true;
        state.generateResult = null;
        renderGeneratePreview(null);
        setGenerateStatus(t("scenario.generate.running", "生成中…"));
        syncGeneratePanel();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ job_id: string; job: GenerateJob }>>(
                `/api/scenarios/${state.scenarioId}/narrative/generate`,
                { method: "POST", body: { options: collectGenerateOptions() } },
            );
            if (!response.ok || !data?.success || !data.data?.job_id) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.generateJobId = data.data.job_id;
            const result = await pollGenerateJob(state.generateJobId);
            state.generateResult = result;
            renderGeneratePreview(result);
            setGenerateStatus(t("scenario.generate.preview.ready", "预览已生成"), "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            const cancelled = error instanceof Error && error.name === "GenerationCancelled";
            setGenerateStatus(message, cancelled ? "warning" : "danger");
            showNotification(message, cancelled ? "warning" : "error");
        } finally {
            state.generateBusy = false;
            syncGeneratePanel();
        }
    }

    /** 协作式取消：后端只把任务标记为 cancelled，worker 跑完当前调用后丢弃结果。 */
    async function cancelGeneration(): Promise<void> {
        const jobId = state.generateJobId;
        if (!jobId || !state.generateBusy) return;
        const button = byId<HTMLButtonElement>("scenarioGenerateCancel");
        if (button) button.disabled = true;
        setGenerateStatus(t("scenario.narrative.job.cancelling", "正在取消…"));
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<GenerateJobPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/jobs/${encodeURIComponent(jobId)}`,
                { method: "DELETE" },
            );
            if (!response.ok || !data?.success) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setGenerateStatus(message, "danger");
            showNotification(message, "error");
            if (button) button.disabled = false;
        }
    }

    async function applyGeneration(): Promise<void> {
        if (!state.generateResult || !editable()) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: t("scenario.generate.apply", "应用到草稿"),
            message: t("scenario.generate.confirm_apply", "应用后会用预览覆盖当前草稿的世界树，继续吗？"),
            confirmText: t("scenario.generate.apply", "应用到草稿"),
        });
        if (!confirmed) return;
        const result = state.generateResult;
        setGenerateStatus(t("scenario.generate.applying", "应用中…"));
        const applyButton = byId<HTMLButtonElement>("scenarioGenerateApply");
        if (applyButton) applyButton.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: result.base_revision, bundle: result.bundle } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            state.revision = data.data.revision;
            state.generateResult = null;
            state.selectedId = "";
            state.selectedEdgeId = "";
            api.renderAll();
            window.requestAnimationFrame(() => api.fitView());
            window.ScenarioTriggers?.syncRevision(state.revision);
            renderGeneratePreview(null);
            setGenerateStatus(t("scenario.generate.applied", "已应用到草稿，请检查后保存并发布。"), "success");
            showNotification(t("scenario.generate.applied", "已应用到草稿，请检查后保存并发布。"), "success");
            showTab("tree");
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            setGenerateStatus(message, "danger");
            showNotification(message, "error");
        } finally {
            syncGeneratePanel();
        }
    }

    Object.assign(api, {
        resetGeneratePanel,
        syncGeneratePanel,
        syncGenerateSource,
        startGeneration,
        cancelGeneration,
        applyGeneration,
    });
})(window);
