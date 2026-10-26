/**
 * 剧本世界树 / 图谱：开场配置编辑器（起始地点、开场模式与公开开场）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, applyRevision } = N;

    let openingStart: Record<string, unknown> = {};

    async function openOpeningEditor(): Promise<void> {
        if (!state.scenarioId) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<StartPayload>>(
                `/api/scenarios/${state.scenarioId}/narrative/start`,
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const payload = data.data;
            openingStart = payload.start || {};
            const locationSelect = byId<HTMLSelectElement>("scenarioOpeningLocation");
            if (locationSelect) {
                locationSelect.innerHTML = `<option value="">${escapeHtml(t("scenario.visibility.opening_location_none", "未设置"))}</option>`
                    + payload.locations.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}（${escapeHtml(item.kind)}）</option>`).join("");
                locationSelect.value = String(openingStart.location_id || "");
            }
            const mode = byId<HTMLSelectElement>("scenarioOpeningMode");
            if (mode) mode.value = String(openingStart.opening_mode || "hybrid");
            const intro = byId<HTMLTextAreaElement>("scenarioOpeningIntro");
            if (intro) {
                intro.value = (Array.isArray(openingStart.public_intro) ? openingStart.public_intro as string[] : []).join("\n\n");
            }
            const kpTruth = byId<HTMLTextAreaElement>("scenarioOpeningKpTruth");
            if (kpTruth) kpTruth.value = String(openingStart.kp_truth || "");
            const enterTriggers = byId<HTMLInputElement>("scenarioOpeningEnterTriggers");
            if (enterTriggers) enterTriggers.checked = openingStart.allow_enter_triggers !== false;
            const modelContinue = byId<HTMLInputElement>("scenarioOpeningModelContinue");
            if (modelContinue) modelContinue.checked = openingStart.allow_model_continuation !== false;

            const readonly = payload.permissions?.readonly === true;
            document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | HTMLButtonElement>(
                "#scenarioOpeningModal input, #scenarioOpeningModal select, #scenarioOpeningModal textarea, #scenarioOpeningSave",
            ).forEach((field) => { field.disabled = readonly; });
            const status = byId("scenarioOpeningStatus");
            if (status) status.textContent = readonly ? t("scenario.visibility.readonly", "只读剧本：可预览，不可保存。") : "";
            const preview = byId("scenarioOpeningPreview");
            if (preview) preview.innerHTML = "";
            const modal = byId("scenarioOpeningModal");
            const apiModal = (window as unknown as { bootstrap?: { Modal?: { getOrCreateInstance: (element: Element) => { show: () => void } } } }).bootstrap?.Modal;
            if (modal) apiModal?.getOrCreateInstance(modal).show();
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function readOpeningForm(): Record<string, unknown> | null {
        const locationSelect = byId<HTMLSelectElement>("scenarioOpeningLocation");
        if (!locationSelect) return null;
        const intro = byId<HTMLTextAreaElement>("scenarioOpeningIntro")?.value || "";
        return {
            ...openingStart,
            location_id: locationSelect.value || "",
            opening_mode: byId<HTMLSelectElement>("scenarioOpeningMode")?.value || "hybrid",
            public_intro: intro.split(/\n\s*\n|\n/).map((part) => part.trim()).filter(Boolean),
            kp_truth: byId<HTMLTextAreaElement>("scenarioOpeningKpTruth")?.value.trim() || "",
            allow_enter_triggers: byId<HTMLInputElement>("scenarioOpeningEnterTriggers")?.checked !== false,
            allow_model_continuation: byId<HTMLInputElement>("scenarioOpeningModelContinue")?.checked !== false,
        };
    }

    async function saveOpening(): Promise<void> {
        if (!state.scenarioId || !editable()) return;
        const start = readOpeningForm();
        if (!start) return;
        const button = byId<HTMLButtonElement>("scenarioOpeningSave");
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ start: Record<string, unknown>; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/start`,
                { method: "PUT", body: { base_revision: state.revision, start } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            openingStart = data.data.start || {};
            applyRevision(data.data.revision);
            const status = byId("scenarioOpeningStatus");
            if (status) status.textContent = t("scenario.visibility.opening_saved", "开场配置已保存。");
            showNotification(t("scenario.visibility.opening_saved", "开场配置已保存。"), "success");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function previewOpening(): void {
        const container = byId("scenarioOpeningPreview");
        if (!container) return;
        const start = readOpeningForm() || {};
        const intro = Array.isArray(start.public_intro) ? start.public_intro as string[] : [];
        const mode = String(start.opening_mode || "hybrid");
        const bubbles = intro.map((paragraph) => `<div class="scenario-opening-bubble"><span class="scenario-opening-speaker">${escapeHtml(t("scenario.visibility.opening_narrator", "旁白"))}</span><p class="mb-0">${escapeHtml(paragraph)}</p></div>`);
        if (mode !== "fixed") {
            bubbles.push(`<div class="scenario-opening-note small text-muted">${escapeHtml(t("scenario.visibility.opening_model_note", "模型会在固定开场后继续叙事。"))}</div>`);
        }
        container.innerHTML = bubbles.length ? bubbles.join("") : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.visibility.opening_empty", "尚未填写公开开场。"))}</p>`;
    }

    Object.assign(api, {
        openOpeningEditor,
        saveOpening,
        previewOpening,
    });
})(window);
