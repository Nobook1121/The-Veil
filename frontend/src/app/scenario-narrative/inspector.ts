/**
 * 剧本世界树 / 图谱：节点详情面板、关键词候选与节点保存（§8.1.3、§8.7）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, runtime, byId, t, escapeHtml, editable, kindMeta, nodeById, parentOf, globalKeywordConflicts, visibilitySourceLabel, revealSummary, revealBlockMarkup, readReveal, revealToCondition, syncRevealType, splitTerms, keywordSourceLabel, VISIBILITY_LABEL } = N;

    function renderInspector(): void {
        const inspector = byId("scenarioNodeInspector");
        if (!inspector) return;
        const node = nodeById(state.selectedId);
        if (!node) {
            inspector.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.select_hint", "选择左侧节点查看详情。"))}</p>`;
            return;
        }
        const meta = kindMeta(node.kind);
        const canEdit = editable();
        const conflicts = globalKeywordConflicts(node);
        const conflictKeys = new Set(conflicts.map((item) => item.key.trim().toLowerCase()));
        const edges = (state.bundle?.edges || []).filter((edge) => edge.from === node.id || edge.to === node.id);
        const visibility = node.visibility || "public";
        const rows: Array<[string, string]> = [
            ["ID", `<code>${escapeHtml(node.id)}</code>`],
            ["类型", escapeHtml(meta.label)],
            ["父节点", parentOf(node) ? escapeHtml(nodeById(parentOf(node))?.title || parentOf(node)) : "—"],
            ["可见性来源", escapeHtml(visibilitySourceLabel(node))],
            ["场景角色", escapeHtml(node.scene_role || "—")],
            ["进入方式", escapeHtml(node.access || "—")],
            ["可重复", node.repeatable === false ? "否" : "是"],
            ["物件数", String(Array.isArray(node.objects) ? node.objects.length : 0)],
            ["关键词", (node.keywords || []).length ? node.keywords!.map((word) => {
                const danger = conflictKeys.has(String(word).trim().toLowerCase());
                return `<span class="badge ${danger ? "bg-danger" : "bg-light text-dark"}">${escapeHtml(word)}</span>`;
            }).join(" ") : "—"],
        ];
        if (!canEdit) {
            rows.splice(3, 0, [
                "可见性",
                `<span class="badge bg-${visibility === "public" ? "success" : visibility === "kp_only" ? "danger" : "warning"}">${escapeHtml(VISIBILITY_LABEL[visibility] || visibility)}</span>`,
            ]);
            if (visibility === "hidden") {
                rows.push(["揭示条件", escapeHtml(revealSummary(node.reveal_when))]);
            }
        }
        const editForm = canEdit ? `
            <div class="scenario-inspector-edit">
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_title", "标题"))}</span>
                    <input type="text" class="form-control form-control-sm" id="scenarioNodeTitle" value="${escapeHtml(node.title)}">
                </label>
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_visibility", "可见性"))}</span>
                    <select class="form-select form-select-sm" id="scenarioNodeVisibility">
                        ${(["public", "hidden", "kp_only"] as const).map((value) => `<option value="${value}"${visibility === value ? " selected" : ""}>${escapeHtml(VISIBILITY_LABEL[value])}</option>`).join("")}
                    </select>
                </label>
                ${revealBlockMarkup("scenarioNode", node.reveal_when, visibility)}
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_body", "正文"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioNodeBody" rows="5">${escapeHtml(node.body || "")}</textarea>
                </label>
                <label class="form-group">
                    <span>${escapeHtml(t("scenario.tree.edit_kp_notes", "KP 备注"))}</span>
                    <textarea class="form-control form-control-sm" id="scenarioNodeKpNotes" rows="3">${escapeHtml(node.kp_notes || "")}</textarea>
                </label>
                <div class="scenario-keyword-panel">
                    <div class="scenario-keyword-head">
                        <span>${escapeHtml(t("scenario.tree.keywords", "关键词"))}</span>
                        <button type="button" class="btn btn-outline-secondary btn-sm" id="scenarioNodeExtractKeywords">
                            <i class="fa fa-magic" aria-hidden="true"></i>
                            <span>${escapeHtml(t("scenario.tree.extract_keywords", "从正文提取"))}</span>
                        </button>
                    </div>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_keywords", "主关键词"))} <small class="text-muted">${escapeHtml(t("scenario.tree.edit_keywords_hint", "逗号或换行分隔"))}</small></span>
                        <input type="text" class="form-control form-control-sm${conflicts.length ? " is-invalid" : ""}" id="scenarioNodeKeywords" value="${escapeHtml((node.keywords || []).join(", "))}">
                    </label>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_aliases", "别名"))}</span>
                        <input type="text" class="form-control form-control-sm" id="scenarioNodeAliases" value="${escapeHtml((node.aliases || []).join(", "))}">
                    </label>
                    <label class="form-group">
                        <span>${escapeHtml(t("scenario.tree.edit_negative_keywords", "否定词"))}</span>
                        <input type="text" class="form-control form-control-sm" id="scenarioNodeNegativeKeywords" value="${escapeHtml((node.negative_keywords || []).join(", "))}">
                    </label>
                    <div class="scenario-keyword-options">
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.edit_scope", "作用域"))}</span>
                            <select class="form-select form-select-sm" id="scenarioNodeScope">
                                ${(["local", "global"] as const).map((value) => `<option value="${value}"${(node.scope || "local") === value ? " selected" : ""}>${escapeHtml(t(`scenario.tree.scope_${value}`, value === "global" ? "全局" : "局部"))}</option>`).join("")}
                            </select>
                        </label>
                        <label class="scenario-module-toggle">
                            <span>${escapeHtml(t("scenario.tree.constant", "常驻"))}</span>
                            <input type="checkbox" id="scenarioNodeConstant" ${node.constant ? "checked" : ""}>
                        </label>
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.keyword_weight", "权重"))}</span>
                            <input type="number" class="form-control form-control-sm" id="scenarioNodeKeywordWeight" min="0" max="2" step="0.1" value="${escapeHtml(String(node.keyword_weight ?? 1))}">
                        </label>
                        <label class="form-group">
                            <span>${escapeHtml(t("scenario.tree.keyword_match", "匹配模式"))}</span>
                            <select class="form-select form-select-sm" id="scenarioNodeKeywordMatch">
                                ${(["word", "substring"] as const).map((value) => `<option value="${value}"${(node.keyword_match || "word") === value ? " selected" : ""}>${escapeHtml(t(`scenario.tree.keyword_match_${value}`, value === "substring" ? "子串" : "整词"))}</option>`).join("")}
                            </select>
                        </label>
                    </div>
                    <div id="scenarioNodeKeywordCandidates" class="scenario-keyword-candidates"></div>
                    ${conflicts.length ? `<p class="scenario-keyword-conflict">${escapeHtml(t("scenario.tree.keyword_conflict", "全局关键词同时绑定多个节点；建议改为局部作用域或加否定词。"))} ${conflicts.map((item) => `<strong>${escapeHtml(item.key)}</strong> → ${escapeHtml(item.others.join("、"))}`).join("；")}</p>` : ""}
                </div>
                <label class="scenario-module-toggle">
                    <span>${escapeHtml(t("scenario.graph.lock_layout", "锁定布局"))}</span>
                    <input type="checkbox" id="scenarioNodeLayoutLocked" ${node.layout_locked ? "checked" : ""}>
                </label>
                <button type="button" class="btn btn-primary btn-sm" id="scenarioNodeSave">
                    <i class="fa fa-save" aria-hidden="true"></i>
                    <span>${escapeHtml(t("scenario.tree.save_node", "保存节点"))}</span>
                </button>
            </div>` : "";
        inspector.innerHTML = `
            <h6 class="mb-2"><i class="fa ${meta.icon}" style="color:${meta.color}" aria-hidden="true"></i> ${escapeHtml(node.title || node.id)}</h6>
            ${node.summary ? `<p class="text-muted small">${escapeHtml(node.summary)}</p>` : ""}
            ${editForm}
            <div class="scenario-inspector-rows">${rows.map(([label, value]) => `<div class="scenario-inspector-row"><span>${escapeHtml(label)}</span><div>${value}</div></div>`).join("")}</div>
            ${node.body ? `<div class="scenario-inspector-body"><h6>${escapeHtml(t("scenario.tree.body", "正文"))}</h6><div class="scenario-inspector-pre">${escapeHtml(node.body)}</div></div>` : ""}
            ${!canEdit && node.kp_notes ? `<div class="scenario-inspector-kp"><h6>${escapeHtml(t("scenario.tree.kp_notes", "KP 备注"))}</h6><div>${escapeHtml(node.kp_notes)}</div></div>` : ""}
            <div class="scenario-inspector-edges">
                <h6>${escapeHtml(t("scenario.tree.edges", "连接"))}</h6>
                ${edges.length ? `<ul class="list-unstyled small mb-0">${edges.map((edge) => {
                    const otherId = edge.from === node.id ? edge.to : edge.from;
                    const other = nodeById(otherId);
                    const arrow = edge.from === node.id ? "→" : "←";
                    const hard = edge.hard ? ` <span class="badge bg-danger">hard</span>` : "";
                    return `<li><code>${arrow}</code> ${escapeHtml(other?.title || otherId)} <span class="text-muted">${escapeHtml(edge.type || "")}</span>${hard}</li>`;
                }).join("")}</ul>` : `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.no_edges", "暂无连接。"))}</p>`}
            </div>`;
        if (canEdit) {
            syncRevealType("scenarioNode");
            renderKeywordCandidates(node);
        }
    }

    function renderKeywordCandidates(node: GraphNode): void {
        const host = byId("scenarioNodeKeywordCandidates");
        if (!host) return;
        const cached = runtime.keywordCandidates && runtime.keywordCandidates.nodeId === node.id ? runtime.keywordCandidates : null;
        if (!cached) {
            host.innerHTML = "";
            return;
        }
        if (!cached.items.length) {
            host.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.tree.candidate_empty", "正文中没有新的候选实体。"))}</p>`;
            return;
        }
        const coverage = t("scenario.tree.candidate_coverage", "正文中还有 {count} 个候选实体未被关键词覆盖。")
            .replace("{count}", String(cached.items.length));
        host.innerHTML = `
            <p class="scenario-keyword-coverage">${escapeHtml(coverage)}</p>
            <div class="scenario-keyword-chips">
                ${cached.items.map((item, index) => `<button type="button" class="scenario-keyword-chip" data-keyword-index="${index}" title="${escapeHtml(t("scenario.tree.candidate_add", "加入主关键词"))}">
                    <span>${escapeHtml(item.key)}</span>
                    <small>${escapeHtml(keywordSourceLabel(item.source))} · ${item.confidence.toFixed(2)}</small>
                </button>`).join("")}
            </div>`;
    }

    function applyKeywordCandidate(index: number): void {
        if (!runtime.keywordCandidates) return;
        const item = runtime.keywordCandidates.items[index];
        const input = byId<HTMLInputElement>("scenarioNodeKeywords");
        if (!item || !input) return;
        const values = splitTerms(input.value);
        if (!values.some((word) => word.toLowerCase() === item.key.toLowerCase())) values.push(item.key);
        input.value = values.join(", ");
        input.classList.remove("is-invalid");
        runtime.keywordCandidates.items.splice(index, 1);
        const node = nodeById(runtime.keywordCandidates.nodeId);
        if (node) renderKeywordCandidates(node);
    }

    async function extractNodeKeywords(): Promise<void> {
        const node = nodeById(state.selectedId);
        if (!node || !state.scenarioId || !editable()) return;
        const button = byId<HTMLButtonElement>("scenarioNodeExtractKeywords");
        if (button) button.disabled = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ suggestions: Array<{ node_id: string; suggested: KeywordCandidate[] }> }>>(
                `/api/scenarios/${state.scenarioId}/knowledge/suggest-keywords`,
                { method: "POST", body: { target: "node", node_ids: [node.id], limit: 8 } },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            const entry = (data.data.suggestions || []).find((item) => item.node_id === node.id);
            runtime.keywordCandidates = { nodeId: node.id, items: entry?.suggested || [] };
            renderKeywordCandidates(node);
            if (!runtime.keywordCandidates.items.length) {
                showNotification(t("scenario.tree.candidate_empty", "正文中没有新的候选实体。"), "warning");
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message || t("scenario.tree.keyword_extract_failed", "关键词提取失败。"), "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function saveNode(): Promise<void> {
        const node = nodeById(state.selectedId);
        if (!node || !state.scenarioId || !editable()) return;
        const title = byId<HTMLInputElement>("scenarioNodeTitle")?.value.trim() || "";
        const visibility = byId<HTMLSelectElement>("scenarioNodeVisibility")?.value || "public";
        const kpNotes = byId<HTMLTextAreaElement>("scenarioNodeKpNotes")?.value.trim() || "";
        const body = byId<HTMLTextAreaElement>("scenarioNodeBody")?.value || "";
        const keywords = splitTerms(byId<HTMLInputElement>("scenarioNodeKeywords")?.value);
        const aliases = splitTerms(byId<HTMLInputElement>("scenarioNodeAliases")?.value);
        const negativeKeywords = splitTerms(byId<HTMLInputElement>("scenarioNodeNegativeKeywords")?.value);
        const scope = byId<HTMLSelectElement>("scenarioNodeScope")?.value || "local";
        const constant = byId<HTMLInputElement>("scenarioNodeConstant")?.checked === true;
        const rawWeight = Number(byId<HTMLInputElement>("scenarioNodeKeywordWeight")?.value);
        const keywordWeight = Number.isFinite(rawWeight) ? Math.max(0, Math.min(2, rawWeight)) : 1;
        const keywordMatch = byId<HTMLSelectElement>("scenarioNodeKeywordMatch")?.value === "substring" ? "substring" : "word";
        const layoutLocked = byId<HTMLInputElement>("scenarioNodeLayoutLocked")?.checked === true;
        const reveal = readReveal("scenarioNode");
        const revealWhen = visibility === "hidden" ? revealToCondition(reveal) : null;
        if (!title) {
            showNotification(t("scenario.tree.title_required", "节点标题不能为空。"), "warning");
            return;
        }
        const button = byId<HTMLButtonElement>("scenarioNodeSave");
        if (button) button.disabled = true;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(node.id)}`,
                { method: "PATCH", body: { base_revision: state.revision, title, visibility, body, kp_notes: kpNotes, keywords, aliases, negative_keywords: negativeKeywords, scope, constant, keyword_weight: keywordWeight, keyword_match: keywordMatch, reveal_when: revealWhen, layout_locked: layoutLocked } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            N.applyRevision(data.data.revision);
            const saved = data.data.node;
            if (state.bundle) {
                state.bundle.nodes = state.bundle.nodes.map((item) => (item.id === saved.id ? { ...item, ...saved } : item));
            }
            api.renderAll();
            N.markDirty(false);
            showNotification(t("scenario.tree.saved", "节点已保存。"), "success");
        } catch (error) {
            api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    Object.assign(api, {
        renderInspector,
        applyKeywordCandidate,
        extractNodeKeywords,
        saveNode,
    });
})(window);
