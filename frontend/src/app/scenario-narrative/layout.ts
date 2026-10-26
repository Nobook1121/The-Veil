/**
 * 剧本世界树 / 图谱：坐标计算、视图变换与自动布局。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, kindMeta, sortNodes, parentOf, editable } = N;

    const LAYOUT_CHUNK_THRESHOLD = 300;

    function defaultPositions(bundle: GraphBundle): Map<string, { x: number; y: number }> {
        const positions = new Map<string, { x: number; y: number }>();
        const counters = new Map<number, number>();
        for (const node of sortNodes(bundle.nodes)) {
            const layout = node.layout || {};
            if (typeof layout.x === "number" && typeof layout.y === "number") {
                positions.set(node.id, { x: layout.x, y: layout.y });
                continue;
            }
            const level = kindMeta(node.kind).order <= 3 ? kindMeta(node.kind).order : 2;
            const index = counters.get(level) || 0;
            counters.set(level, index + 1);
            positions.set(node.id, { x: 60 + level * 240, y: 60 + index * 100 });
        }
        return positions;
    }

    function viewportSize(): { width: number; height: number } {
        const canvas = byId("scenarioGraphCanvas");
        const width = canvas?.clientWidth || 900;
        const height = canvas?.clientHeight || 520;
        return { width: Math.max(320, width), height: Math.max(320, height) };
    }

    function applyView(): void {
        const viewport = byId("scenarioGraphViewport");
        if (!viewport) return;
        viewport.setAttribute("transform", `translate(${state.view.x},${state.view.y}) scale(${state.view.k})`);
    }

    function fitView(): void {
        if (state.positions.size === 0) return;
        const values = [...state.positions.values()];
        const minX = Math.min(...values.map((item) => item.x));
        const minY = Math.min(...values.map((item) => item.y));
        const maxX = Math.max(...values.map((item) => item.x)) + 180;
        const maxY = Math.max(...values.map((item) => item.y)) + 70;
        const { width, height } = viewportSize();
        const k = Math.max(0.25, Math.min(1.4, Math.min(width / (maxX - minX), height / (maxY - minY))));
        state.view = {
            x: (width - (maxX - minX) * k) / 2 - minX * k,
            y: (height - (maxY - minY) * k) / 2 - minY * k,
            k,
        };
        applyView();
    }

    function setGraphHint(text: string): void {
        const hint = byId("scenarioGraphHint");
        if (hint) hint.textContent = text;
    }

    function modeHint(mode: GraphMode): string {
        if (mode === "connect") {
            return t("scenario.graph.hint_connect", "连线：依次点击起点与终点节点。");
        }
        if (mode === "pan") {
            return t("scenario.graph.hint_pan", "平移：拖拽画布移动，滚轮缩放。");
        }
        return t("scenario.graph.hint_select", "选择：点击节点查看详情，拖拽节点调整布局。");
    }

    function playerHint(): string {
        return t("scenario.visibility.player_hint", "玩家视图：只显示公开内容；隐藏节点灰显，仅 KP 内容不会出现。");
    }

    function depthResolver(bundle: GraphBundle): (node: GraphNode) => number {
        const byIdMap = new Map(bundle.nodes.map((node) => [node.id, node]));
        const depthCache = new Map<string, number>();
        const depthOf = (node: GraphNode, guard = 0): number => {
            if (depthCache.has(node.id)) return depthCache.get(node.id)!;
            const parentKey = parentOf(node);
            const parent = parentKey ? byIdMap.get(parentKey) : undefined;
            const value = !parent || guard > 8 ? 0 : depthOf(parent, guard + 1) + 1;
            depthCache.set(node.id, value);
            return value;
        };
        return (node: GraphNode) => depthOf(node);
    }

    function hierarchyLayout(bundle: GraphBundle): Map<string, { x: number; y: number }> {
        const depthOf = depthResolver(bundle);
        const counters = new Map<number, number>();
        const positions = new Map<string, { x: number; y: number }>();
        for (const node of sortNodes(bundle.nodes)) {
            if (node.layout_locked && typeof node.layout?.x === "number" && typeof node.layout?.y === "number") {
                positions.set(node.id, { x: node.layout.x, y: node.layout.y });
                continue;
            }
            const depth = Math.min(6, depthOf(node));
            const row = counters.get(depth) || 0;
            counters.set(depth, row + 1);
            positions.set(node.id, { x: 60 + depth * 250, y: 60 + row * 100 });
        }
        return positions;
    }

    /** 大图布局分块让出主线程，避免界面卡死（§8.1.5）。 */
    async function layoutPositions(
        bundle: GraphBundle,
        onProgress?: (done: number, total: number) => void,
    ): Promise<Map<string, { x: number; y: number }>> {
        if (!onProgress || bundle.nodes.length <= LAYOUT_CHUNK_THRESHOLD) return hierarchyLayout(bundle);
        const depthOf = depthResolver(bundle);
        const counters = new Map<number, number>();
        const positions = new Map<string, { x: number; y: number }>();
        const sorted = sortNodes(bundle.nodes);
        for (let index = 0; index < sorted.length; index += 1) {
            const node = sorted[index];
            if (node) {
                if (node.layout_locked && typeof node.layout?.x === "number" && typeof node.layout?.y === "number") {
                    positions.set(node.id, { x: node.layout.x, y: node.layout.y });
                } else {
                    const depth = Math.min(6, depthOf(node));
                    const row = counters.get(depth) || 0;
                    counters.set(depth, row + 1);
                    positions.set(node.id, { x: 60 + depth * 250, y: 60 + row * 100 });
                }
            }
            if (index % 60 === 0) {
                onProgress(index + 1, sorted.length);
                await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
            }
        }
        onProgress(sorted.length, sorted.length);
        return positions;
    }

    async function autoLayout(): Promise<void> {
        const bundle = state.bundle;
        if (!bundle || !editable() || !state.scenarioId) return;
        const button = byId<HTMLButtonElement>("scenarioGraphAutoLayout");
        const large = bundle.nodes.length > LAYOUT_CHUNK_THRESHOLD;
        if (button) button.disabled = true;
        let history: HistoryEntry | null = null;
        try {
            const positions = await layoutPositions(bundle, large
                ? (done, total) => {
                    const percent = Math.round((done / total) * 100);
                    setGraphHint(t("scenario.graph.layout_progress", "自动布局中… {percent}%").replace("{percent}", String(percent)));
                }
                : undefined);
            const nodes = bundle.nodes.map((node) => {
                const position = positions.get(node.id);
                return position ? { ...node, layout: { x: position.x, y: position.y } } : node;
            });
            history = api.pushHistory();
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ bundle: GraphBundle; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative`,
                { method: "PUT", body: { base_revision: state.revision, bundle: { ...bundle, nodes } } },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.bundle = data.data.bundle;
            N.applyRevision(data.data.revision);
            api.renderAll();
            fitView();
        } catch (error) {
            if (history) api.discardHistory(history);
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message, "error");
        } finally {
            if (button) button.disabled = false;
            if (large) setGraphHint(modeHint(state.mode));
        }
    }

    Object.assign(api, {
        defaultPositions,
        viewportSize,
        applyView,
        fitView,
        setGraphHint,
        modeHint,
        playerHint,
        autoLayout,
    });
})(window);
