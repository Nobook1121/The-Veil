/**
 * 剧本世界树 / 图谱：按玩家可见性渲染预览（§8.3）。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, kindMeta, sortNodes, nodeById, revealSummary } = N;

    function previewEntryMarkup(entry: PreviewEntry): string {
        const { node, visibility } = entry;
        const meta = kindMeta(node.kind);
        if (visibility === "public") {
            const excerpt = [node.summary || "", node.body || ""].filter(Boolean).join(" ").slice(0, 180);
            return `
                <article class="scenario-preview-card">
                    <header><span class="badge bg-light text-dark">${escapeHtml(meta.label)}</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                    ${excerpt ? `<p class="mb-0">${escapeHtml(excerpt)}${excerpt.length >= 180 ? "…" : ""}</p>` : ""}
                    ${(node.keywords || []).length ? `<div class="scenario-preview-keywords">${node.keywords!.map((word) => `<span class="badge bg-light text-dark">${escapeHtml(word)}</span>`).join(" ")}</div>` : ""}
                </article>`;
        }
        if (visibility === "hidden") {
            return `
                <article class="scenario-preview-card is-hidden">
                    <header><span class="badge bg-warning text-dark">${escapeHtml(t("scenario.visibility.hidden_placeholder", "未揭示"))}</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                    <p class="mb-0 small">${escapeHtml(t("scenario.visibility.hidden_reason", "玩家暂不可见，满足揭示条件后出现："))}${escapeHtml(revealSummary(node.reveal_when))}</p>
                </article>`;
        }
        return `
            <article class="scenario-preview-card is-kp">
                <header><span class="badge bg-secondary">KP ONLY</span><strong>${escapeHtml(node.title || node.id)}</strong></header>
                <p class="mb-0 small">${escapeHtml(t("scenario.visibility.kp_only_hint", "仅 KP：内容永远不会进入玩家提示词、检索与界面。"))}</p>
            </article>`;
    }

    function renderPlayerPreview(): void {
        const body = byId("scenarioPlayerPreviewBody");
        const summary = byId("scenarioPlayerPreviewSummary");
        if (!body) return;
        const bundle = state.bundle;
        if (!bundle || bundle.nodes.length === 0) {
            body.innerHTML = `<p class="text-muted small mb-0">${escapeHtml(t("scenario.visibility.player_empty", "当前剧本没有可预览的节点。"))}</p>`;
            if (summary) summary.textContent = "";
            return;
        }
        const entries: PreviewEntry[] = sortNodes(bundle.nodes).map((node) => ({
            node,
            visibility: ((node.visibility || "public") === "kp_only" ? "kp_only" : (node.visibility || "public") === "hidden" ? "hidden" : "public"),
        }));
        const visibleEdges = (bundle.edges || []).filter((edge) => {
            const from = nodeById(edge.from);
            const to = nodeById(edge.to);
            return (edge.visibility || "public") === "public"
                && (from?.visibility || "public") === "public"
                && (to?.visibility || "public") === "public";
        }).length;
        const groups: Array<[string, "public" | "hidden" | "kp_only"]> = [
            ["scenario.visibility.group_public", "public"],
            ["scenario.visibility.group_hidden", "hidden"],
            ["scenario.visibility.group_kp", "kp_only"],
        ];
        const fallback: Record<string, string> = {
            "scenario.visibility.group_public": "玩家可见",
            "scenario.visibility.group_hidden": "未揭示（满足条件后出现）",
            "scenario.visibility.group_kp": "仅 KP（玩家永不可见）",
        };
        body.innerHTML = groups.map(([key, group]) => {
            const items = entries.filter((entry) => entry.visibility === group);
            if (items.length === 0) return "";
            return `<section class="scenario-preview-group"><h6>${escapeHtml(t(key, fallback[key] || key))} <span class="text-muted small">${items.length}</span></h6>${items.map(previewEntryMarkup).join("")}</section>`;
        }).join("");
        const counts = {
            public: entries.filter((entry) => entry.visibility === "public").length,
            hidden: entries.filter((entry) => entry.visibility === "hidden").length,
            kp: entries.filter((entry) => entry.visibility === "kp_only").length,
        };
        if (summary) {
            summary.textContent = `${counts.public} ${t("scenario.visibility.count_visible", "可见")} · ${counts.hidden} ${t("scenario.visibility.count_hidden", "未揭示")} · ${counts.kp} ${t("scenario.visibility.count_kp", "仅 KP")} · ${visibleEdges} ${t("scenario.visibility.count_edges", "可见连接")}`;
        }
    }

    function openPlayerPreview(): void {
        if (!state.bundle) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        renderPlayerPreview();
        const modal = byId("scenarioPlayerPreviewModal");
        const apiModal = (window as unknown as { bootstrap?: { Modal?: { getOrCreateInstance: (element: Element) => { show: () => void } } } }).bootstrap?.Modal;
        if (modal) apiModal?.getOrCreateInstance(modal).show();
    }

    Object.assign(api, {
        renderPlayerPreview,
        openPlayerPreview,
    });
})(window);
