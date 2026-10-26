/**
 * 剧本世界树 / 图谱：修订号冲突检测、差异摘要与草稿下载。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, runtime, t, updateStatusBadge } = N;

    function bundleDiffSummary(local: GraphBundle | null, remote: GraphBundle, remoteRevision: number): string {
        if (!local) {
            return t("scenario.tree.conflict_remote_only", "远端版：{rev} · {nodes} 节点")
                .replace("{rev}", String(remoteRevision))
                .replace("{nodes}", String(remote.nodes.length));
        }
        const localNodes = new Map(local.nodes.map((node) => [node.id, node]));
        const remoteNodes = new Map(remote.nodes.map((node) => [node.id, node]));
        const added = [...remoteNodes.keys()].filter((id) => !localNodes.has(id)).length;
        const removed = [...localNodes.keys()].filter((id) => !remoteNodes.has(id)).length;
        const modified = [...remoteNodes.values()].filter((node) => {
            const before = localNodes.get(node.id);
            return Boolean(before) && (before!.title !== node.title || (before!.body || "") !== (node.body || ""));
        }).length;
        const localEdges = new Set(local.edges.map((edge) => edge.id));
        const remoteEdges = new Set(remote.edges.map((edge) => edge.id));
        const edgesAdded = [...remoteEdges].filter((id) => !localEdges.has(id)).length;
        const edgesRemoved = [...localEdges].filter((id) => !remoteEdges.has(id)).length;
        return t(
            "scenario.tree.conflict_detail",
            "远端修订 {rev}（本地 {local}）；远端新增 {added}、删除 {removed}、修改 {modified} 个节点，连线 +{edges_added}/-{edges_removed}。",
        )
            .replace("{rev}", String(remoteRevision))
            .replace("{local}", String(state.revision))
            .replace("{added}", String(added))
            .replace("{removed}", String(removed))
            .replace("{modified}", String(modified))
            .replace("{edges_added}", String(edgesAdded))
            .replace("{edges_removed}", String(edgesRemoved));
    }

    async function fetchRemoteBundle(): Promise<RemoteSnapshot | null> {
        if (!state.scenarioId) return null;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<NarrativePayload>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
            );
            if (!response.ok || !data?.success || !data.data) return null;
            return { bundle: data.data.bundle, revision: data.data.revision };
        } catch {
            return null;
        }
    }

    function downloadDraft(): void {
        if (!state.bundle) return;
        const payload = JSON.stringify(
            { scenario_id: state.scenarioId, revision: state.revision, saved_at: new Date().toISOString(), bundle: state.bundle },
            null,
            2,
        );
        const blob = new Blob([payload], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `narrative-draft-${state.scenarioId}-${Date.now()}.json`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    }

    async function handleConflict(): Promise<void> {
        runtime.conflictPending = true;
        updateStatusBadge();
        const remote = await fetchRemoteBundle();
        const diff = remote
            ? bundleDiffSummary(state.bundle, remote.bundle, remote.revision)
            : t("scenario.tree.conflict_unknown", "无法读取远端版本，请刷新页面后重试。");
        const refresh = await window.TrpgDialog?.confirm({
            title: t("scenario.tree.conflict_title", "版本冲突"),
            message: `${t("scenario.tree.conflict", "内容已被其他编辑修改。")}\n${diff}\n${t("scenario.tree.conflict_hint", "刷新会放弃本地未保存的改动；保留本地会下载草稿供手动合并。")}`,
            confirmText: t("scenario.tree.conflict_refresh", "刷新远端"),
            cancelText: t("scenario.tree.conflict_keep", "保留本地并下载草稿"),
            danger: true,
        });
        if (refresh !== false) {
            await api.load(state.scenarioId, { readonly: state.readonly });
            return;
        }
        downloadDraft();
        showNotification(t("scenario.tree.conflict_draft_saved", "已下载本地草稿，请手动合并后再保存。"), "warning");
    }

    Object.assign(api, {
        handleConflict,
    });
})(window);
