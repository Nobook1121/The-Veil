/**
 * 剧本世界树 / 图谱：确定性校验结果面板与发布闸门提示。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, nodeById } = N;

    function updatePublishGuard(count: number): void {
        const button = byId<HTMLButtonElement>("saveScenario");
        if (!button) return;
        button.classList.toggle("scenario-publish-blocked", count > 0);
        button.title = count > 0
            ? t("scenario.visibility.publish_blocked", "还有 {count} 项可见性待复核，发布前请先处理。").replace("{count}", String(count))
            : "";
    }

    async function acceptReviewIssue(target: string): Promise<void> {
        if (!state.scenarioId) return;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/visibility/review`,
                { method: "POST", body: { target, base_revision: state.revision } },
            );
            if (!response.ok || !data?.success) throw new Error(data?.message || `HTTP ${response.status}`);
            if (data.data?.revision != null) state.revision = data.data.revision;
            await api.runValidation();
            showNotification(t("scenario.visibility.review_accepted", "已确认该可见性判定。"), "success");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function renderValidation(payload: ValidationPayload): void {
        const results = byId("scenarioValidateResults");
        const summary = byId("scenarioValidateSummary");
        if (summary) {
            summary.textContent = `${payload.ok ? t("scenario.validate.ok", "通过") : t("scenario.validate.failed", "存在问题")} · ${payload.error_count} error · ${payload.warning_count} warn`;
        }
        if (!results) return;
        const quality = payload.quality;
        const qualityMarkup = quality
            ? `<section class="scenario-validate-group"><h6 class="scenario-validate-group-title">${escapeHtml(t("scenario.validate.quality", "质量指标"))} <span class="badge bg-${quality.failed.length ? "warning text-dark" : "success"}">${quality.passed}/${quality.total}</span></h6><div class="d-flex flex-wrap gap-2">${quality.metrics.map((metric) => `<span class="badge bg-${metric.passed === false ? "danger" : metric.passed === true ? "success" : "secondary"}" title="${escapeHtml(`${metric.target} · ${metric.detail}`)}">${escapeHtml(metric.label)} ${escapeHtml(String(metric.value ?? "-"))}</span>`).join("")}</div></section>`
            : "";
        const issues = payload.issues || [];
        if (issues.length === 0) {
            updatePublishGuard(0);
            results.innerHTML = `<p class="text-success small mb-0">${escapeHtml(t("scenario.validate.clean", "未发现问题。"))}</p>${qualityMarkup}`;
            return;
        }
        const levelVariant: Record<string, string> = { error: "danger", warn: "warning", info: "info" };
        const items = issues.slice();
        if (payload.order_simulation && !payload.order_simulation.ok) {
            items.push(...payload.order_simulation.issues.filter((issue) => issue.level === "error"));
        }
        const issueMarkup = (issue: ValidationIssue): string => `
            <div class="scenario-validate-item scenario-validate-${escapeHtml(levelVariant[issue.level] || "info")}">
                <div class="scenario-validate-head">
                    <span class="badge bg-${escapeHtml(levelVariant[issue.level] || "info")}">${escapeHtml(issue.level)}</span>
                    <code>${escapeHtml(issue.code)}</code>
                    ${issue.node_id ? `<button type="button" class="btn btn-link btn-sm p-0 scenario-validate-jump" data-node-id="${escapeHtml(issue.node_id)}">${escapeHtml(nodeById(issue.node_id)?.title || issue.node_id)}</button>` : ""}
                </div>
                <div class="small">${escapeHtml(issue.message)}</div>
                ${issue.suggestion ? `<div class="small text-muted">${escapeHtml(issue.suggestion)}</div>` : ""}
                ${issue.code === "visibility.needs_review"
                    ? `<button type="button" class="btn btn-sm btn-outline-success scenario-validate-accept" data-review-target="${escapeHtml(issue.node_id ? `node:${issue.node_id}` : `edge:${issue.edge_id || ""}`)}">${escapeHtml(t("scenario.visibility.accept_review", "确认该判定"))}</button>`
                    : ""}
            </div>`;
        const reviewItems = items.filter((issue) => issue.code.startsWith("visibility.") || issue.code === "trigger.needs_review");
        const otherItems = items.filter((issue) => !reviewItems.includes(issue));
        updatePublishGuard(reviewItems.filter((issue) => issue.code === "visibility.needs_review").length);
        results.innerHTML = [
            qualityMarkup,
            reviewItems.length
                ? `<section class="scenario-validate-group"><h6 class="scenario-validate-group-title">${escapeHtml(t("scenario.visibility.review_queue", "待复核可见性"))} <span class="badge bg-warning text-dark">${reviewItems.length}</span></h6>${reviewItems.map(issueMarkup).join("")}</section>`
                : "",
            otherItems.length ? otherItems.map(issueMarkup).join("") : "",
        ].join("");
    }

    async function runValidation(): Promise<void> {
        if (!state.scenarioId) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        const button = byId<HTMLButtonElement>("scenarioValidateRun");
        const allOrders = byId<HTMLInputElement>("scenarioValidateAllOrders")?.checked === true;
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ValidationPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/validate`,
                { method: "POST", body: { all_orders: allOrders } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.validation = data.data;
            renderValidation(data.data);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    Object.assign(api, {
        updatePublishGuard,
        acceptReviewIssue,
        renderValidation,
        runValidation,
    });
})(window);
