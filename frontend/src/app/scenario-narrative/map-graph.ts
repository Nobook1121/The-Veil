/**
 * 剧本世界树 / 图谱：地图层画法（`graph_style === "map"`）。
 *
 * dnd5e 不使用 CoC 的房间网状图，而是以地图为主干：
 * - 大节点是一张地图，展开后是该图的兴趣点小节点；
 * - 兴趣点之间不画网状连线；
 * - 地图之间只用 portal 通道相连，并在通道行上标注方向与过场文本。
 *
 * 本模块只读写世界树里既有的 `node_role / map / poi / transition` 字段，不引入
 * 规则集专属的数据结构；是否使用地图层画法由后端给出的 `graph_style` 决定。
 */
(function (window: Window): void {
    "use strict";

    const N = window.TrpgNarrative!;
    const { api, state, byId, t, escapeHtml, editable, edgeById, applyRevision, emitSelection } = N;

    type Dict = Record<string, unknown>;

    /** 折叠过（而不是展开过）的地图 id：默认全部展开，保存重绘后仍保持用户的折叠状态。 */
    const collapsedMaps = new Set<string>();

    function dict(value: unknown): Dict {
        return value && typeof value === "object" ? value as Dict : {};
    }

    function text(value: unknown): string {
        return String(value ?? "");
    }

    function numberValue(value: unknown): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function isMapNode(node: GraphNode): boolean {
        return node.node_role === "map" || Boolean(node.map && typeof node.map === "object");
    }

    function isPoiNode(node: GraphNode): boolean {
        return node.node_role === "poi" && Boolean(node.poi && typeof node.poi === "object");
    }

    function mapNodes(): GraphNode[] {
        return (state.bundle?.nodes || []).filter(isMapNode);
    }

    function mapIds(): Set<string> {
        return new Set(mapNodes().map((node) => node.id));
    }

    function poiArea(node: GraphNode): string {
        return text(dict(node.poi).area_id || node.parent || "");
    }

    function poiNodes(areaId: string): GraphNode[] {
        const key = text(areaId);
        if (!key) return [];
        return (state.bundle?.nodes || [])
            .filter((node) => isPoiNode(node) && poiArea(node) === key)
            .sort((left, right) => {
                const leftCode = text(dict(left.poi).code).trim();
                const rightCode = text(dict(right.poi).code).trim();
                const leftNum = Number.parseInt(leftCode, 10);
                const rightNum = Number.parseInt(rightCode, 10);
                if (Number.isFinite(leftNum) && Number.isFinite(rightNum) && leftNum !== rightNum) {
                    return leftNum - rightNum;
                }
                return (left.title || left.id).localeCompare(right.title || right.id, "zh-Hans-CN");
            });
    }

    function areaOf(nodeId: string): string {
        const node = N.nodeById(nodeId);
        if (!node) return "";
        if (isMapNode(node)) return node.id;
        if (isPoiNode(node)) return poiArea(node);
        return node.parent || "";
    }

    interface MapMarkerInfo {
        id: string;
        label: string;
        poi_id: string;
        visibility: string;
    }

    /** 地图配置里的 DM 标记（scenario.json.map_config[scene_id].markers）。 */
    function mapMarkers(map: GraphNode): MapMarkerInfo[] {
        const sceneId = text(dict(map.map).scene_id) || map.id;
        const config = dict(state.mapConfig[sceneId]);
        const raw = config.markers;
        if (!Array.isArray(raw)) return [];
        return raw.map((item, index) => {
            const marker = dict(item);
            return {
                id: text(marker.id) || `marker-${index + 1}`,
                label: text(marker.label),
                poi_id: text(marker.poi_id),
                visibility: text(marker.visibility) || "public",
            };
        });
    }

    function markerForPoi(map: GraphNode, poi: GraphNode): { marker: MapMarkerInfo; byPoiId: boolean } | null {
        const markers = mapMarkers(map);
        const geometry = dict(dict(poi.poi).geometry);
        const markerId = text(geometry.marker_id);
        if (markerId) {
            const marker = markers.find((item) => item.id === markerId);
            if (marker) return { marker, byPoiId: false };
        }
        const marker = markers.find((item) => item.poi_id === poi.id);
        return marker ? { marker, byPoiId: true } : null;
    }

    /** 兴趣点卡片上的标记关联提示；玩家视图不展示 DM 标记。 */
    function markerAssociationMarkup(map: GraphNode, poi: GraphNode): string {
        if (state.viewMode === "player") return "";
        const geometry = dict(dict(poi.poi).geometry);
        const markerId = text(geometry.marker_id);
        if (!mapMarkers(map).length && !markerId) return "";
        const linked = markerForPoi(map, poi);
        if (linked) {
            const marker = linked.marker;
            const pending = linked.byPoiId && markerId !== marker.id ? "（待回写 marker_id）" : "";
            return `<p class="scenario-map-poi-marker">已关联标记：${escapeHtml(marker.label || marker.id)} <code>${escapeHtml(marker.id)}</code>${pending}</p>`;
        }
        if (markerId) {
            return `<p class="scenario-map-poi-marker is-invalid">关联标记失效：<code>${escapeHtml(markerId)}</code> 不在这张地图的标记里。</p>`;
        }
        return `<p class="scenario-map-poi-marker is-empty">未关联 DM 标记</p>`;
    }

    function isVisible(node: GraphNode): boolean {
        return state.viewMode !== "player" || (node.visibility || "public") !== "kp_only";
    }

    function isHidden(node: GraphNode): boolean {
        return (node.visibility || "public") === "hidden";
    }

    function visibilityBadge(node: GraphNode): string {
        const visibility = node.visibility || "public";
        if (visibility === "kp_only") return `<span class="badge bg-secondary">KP ONLY</span>`;
        if (visibility === "hidden") return `<span class="badge bg-warning text-dark">未揭示</span>`;
        return "";
    }

    // ------------------------------------------------------------------
    // 通道归类
    // ------------------------------------------------------------------

    interface Channel {
        edge: GraphEdge;
        fromArea: string;
        toArea: string;
        fromTitle: string;
        toTitle: string;
        inbound: boolean;
    }

    function portalChannels(): Channel[] {
        const ids = mapIds();
        const result: Channel[] = [];
        for (const edge of state.bundle?.edges || []) {
            if ((edge.type || "") !== "portal") continue;
            const transition = dict(edge.transition);
            const fromArea = text(transition.from_area || areaOf(edge.from));
            const toArea = text(transition.to_area || areaOf(edge.to));
            if (!fromArea && !toArea) continue;
            if (fromArea && toArea && fromArea === toArea) continue;
            const fromMap = ids.has(fromArea);
            const toMap = ids.has(toArea);
            if (!fromMap && !toMap) continue;
            const fromNode = N.nodeById(edge.from);
            const toNode = N.nodeById(edge.to);
            result.push({
                edge,
                fromArea,
                toArea,
                fromTitle: fromNode?.title || text(transition.from_area) || edge.from,
                toTitle: toNode?.title || text(transition.to_area) || edge.to,
                inbound: !fromMap && toMap,
            });
        }
        return result;
    }

    function channelsForMap(mapId: string, channels: Channel[]): Channel[] {
        return channels.filter((channel) => {
            if (channel.inbound) return channel.toArea === mapId;
            return channel.fromArea === mapId;
        });
    }

    function channelVisible(channel: Channel): boolean {
        if (state.viewMode !== "player") return true;
        if ((channel.edge.visibility || "public") === "kp_only") return false;
        const fromNode = N.nodeById(channel.edge.from);
        const toNode = N.nodeById(channel.edge.to);
        if (!fromNode || !toNode) return true;
        return isVisible(fromNode) && isVisible(toNode);
    }

    // ------------------------------------------------------------------
    // 渲染
    // ------------------------------------------------------------------

    function matchesSearch(values: string[]): boolean {
        const needle = state.search.trim().toLowerCase();
        if (!needle) return false;
        return values.join(" ").toLowerCase().includes(needle);
    }

    function mapCard(map: GraphNode, channels: Channel[]): string {
        const block = dict(map.map);
        const pois = poiNodes(map.id).filter(isVisible);
        const allPois = poiNodes(map.id);
        const provisional = allPois.filter((node) => dict(dict(node.poi).geometry).provisional === true).length;
        const open = !collapsedMaps.has(map.id)
            || matchesSearch([map.id, map.title || "", ...pois.map((node) => node.title || "")]);
        const hidden = isHidden(map);
        const readonly = !editable() || state.viewMode === "player";
        const rows = channelsForMap(map.id, channels).filter(channelVisible);
        return `
            <details class="scenario-map-card" data-map-id="${escapeHtml(map.id)}"${open ? " open" : ""}>
                <summary class="scenario-map-card-head">
                    <span class="scenario-map-card-icon"><i class="fa fa-map" aria-hidden="true"></i></span>
                    <span class="scenario-map-card-title">${escapeHtml(map.title || map.id)}</span>
                    <span class="badge bg-light text-dark">${escapeHtml(text(block.scene_id) || map.id)}</span>
                    <span class="badge bg-info text-dark">${pois.length} 个兴趣点</span>
                    ${provisional ? `<span class="badge bg-warning text-dark">${provisional} 个 provisional</span>` : ""}
                    ${visibilityBadge(map)}
                </summary>
                <div class="scenario-map-card-body">
                    ${hidden && state.viewMode === "player"
                        ? `<p class="scenario-map-empty-hint">未揭示的地图，玩家视图不展示内部兴趣点。</p>`
                        : `<p class="scenario-map-card-brief">${escapeHtml(text(block.brief) || "（未填写玩家可见简报）")}</p>`}
                    ${hidden && state.viewMode === "player" ? "" : `
                    <details class="scenario-map-meta"${readonly ? "" : ""}>
                        <summary>地图信息</summary>
                        <div class="scenario-map-form-grid">
                            <label>标题<input class="form-control form-control-sm" data-map-field="title" value="${escapeHtml(map.title || "")}"${readonly ? " disabled" : ""}></label>
                            <label>scene_id<input class="form-control form-control-sm" data-map-field="scene" value="${escapeHtml(text(block.scene_id) || map.id)}"${readonly ? " disabled" : ""}></label>
                            <label>格边长(ft)<input class="form-control form-control-sm" type="number" min="1" data-map-field="grid" value="${escapeHtml(numberValue(block.grid_ft) || 5)}"${readonly ? " disabled" : ""}></label>
                            <label>边界(格)<input class="form-control form-control-sm" type="number" min="0" data-map-field="bounds" value="${escapeHtml(numberValue(block.bounds_cells))}"${readonly ? " disabled" : ""}></label>
                            <label>父地图<input class="form-control form-control-sm" data-map-field="parent" value="${escapeHtml(text(block.parent_area))}" placeholder="嵌套地图填写父 area_id"${readonly ? " disabled" : ""}></label>
                            <label>入口 POI<input class="form-control form-control-sm" data-map-field="entry" value="${escapeHtml(Array.isArray(block.entry_pois) ? block.entry_pois.join(", ") : text(block.entry_pois))}" placeholder="逗号分隔"${readonly ? " disabled" : ""}></label>
                        </div>
                        <label>玩家可见简报<textarea class="form-control form-control-sm" rows="2" data-map-field="brief"${readonly ? " disabled" : ""}>${escapeHtml(text(block.brief))}</textarea></label>
                        <label>DM 备注<textarea class="form-control form-control-sm" rows="2" data-map-field="dm-brief"${readonly ? " disabled" : ""}>${escapeHtml(text(block.dm_brief))}</textarea></label>
                        <div class="scenario-map-form-grid">
                            <label>光照<input class="form-control form-control-sm" data-map-field="lighting" value="${escapeHtml(text(dict(block.environment).lighting))}"${readonly ? " disabled" : ""}></label>
                            <label>温度<input class="form-control form-control-sm" data-map-field="temperature" value="${escapeHtml(text(dict(block.environment).temperature))}"${readonly ? " disabled" : ""}></label>
                            <label>气味<input class="form-control form-control-sm" data-map-field="smell" value="${escapeHtml(text(dict(block.environment).smell))}"${readonly ? " disabled" : ""}></label>
                            <label>声音<input class="form-control form-control-sm" data-map-field="sound" value="${escapeHtml(text(dict(block.environment).sound))}"${readonly ? " disabled" : ""}></label>
                        </div>
                        <label>地形<input class="form-control form-control-sm" data-map-field="terrain" value="${escapeHtml(Array.isArray(dict(block.environment).terrain) ? (dict(block.environment).terrain as unknown[]).join("、") : text(dict(block.environment).terrain))}" placeholder="逗号分隔"${readonly ? " disabled" : ""}></label>
                        ${readonly ? "" : `<button type="button" class="btn btn-sm btn-primary" data-map-save="${escapeHtml(map.id)}">保存地图</button>`}
                    </details>`}
                    <div class="scenario-map-poi-list">
                        ${pois.length
                            ? pois.map((poi) => poiCard(poi, readonly, map)).join("")
                            : `<p class="scenario-map-empty-hint">${hidden && state.viewMode === "player" ? "未揭示的地图没有可展示的兴趣点。" : "该地图还没有兴趣点。"}</p>`}
                    </div>
                    ${rows.length ? `
                    <div class="scenario-map-channel-list">
                        <div class="scenario-map-channel-head">通道与过场</div>
                        ${rows.map((channel) => channelCard(channel, readonly)).join("")}
                    </div>` : ""}
                </div>
            </details>`;
    }

    function poiCard(poi: GraphNode, readonly: boolean, map: GraphNode): string {
        const block = dict(poi.poi);
        const trigger = dict(block.trigger);
        const geometry = dict(block.geometry);
        const rect = dict(geometry.rect_cells);
        const mode = text(trigger.mode || "manual");
        const provisional = geometry.provisional === true;
        const hidden = isHidden(poi);
        const markerId = text(geometry.marker_id);
        const markerInfo = markerForPoi(map, poi);
        const markerBadge = state.viewMode === "player"
            ? ""
            : markerInfo
                ? '<span class="badge bg-success">已关联标记</span>'
                : markerId
                    ? '<span class="badge bg-danger">标记失效</span>'
                    : "";
        const open = matchesSearch([poi.id, poi.title || "", text(block.code)]);
        const code = text(block.code).trim();
        return `
            <details class="scenario-map-poi-item" data-poi-id="${escapeHtml(poi.id)}"${open ? " open" : ""}>
                <summary class="scenario-map-poi-head">
                    <span class="scenario-map-poi-code">${escapeHtml(code || "·")}</span>
                    <span class="scenario-map-poi-title">${escapeHtml(hidden && state.viewMode === "player" ? "未揭示的兴趣点" : (poi.title || poi.id))}</span>
                    <span class="badge bg-light text-dark">${escapeHtml(mode)}</span>
                    ${provisional ? '<span class="badge bg-warning text-dark">provisional</span>' : ""}
                    ${markerBadge}
                    ${visibilityBadge(poi)}
                </summary>
                <div class="scenario-map-poi-body">
                    ${markerAssociationMarkup(map, poi)}
                    <div class="scenario-map-form-grid">
                        <label>标题<input class="form-control form-control-sm" data-poi-field="title" value="${escapeHtml(poi.title || "")}"${readonly ? " disabled" : ""}></label>
                        <label>编号<input class="form-control form-control-sm" data-poi-field="code" value="${escapeHtml(code)}" placeholder="DM 地图编号"${readonly ? " disabled" : ""}></label>
                        <label>area_id<input class="form-control form-control-sm" data-poi-field="area" value="${escapeHtml(poiArea(poi))}"${readonly ? " disabled" : ""}></label>
                        <label>触发<select class="form-select form-select-sm" data-poi-field="mode"${readonly ? " disabled" : ""}>
                            ${["enter", "adjacent", "inspect", "manual"].map((value) => `<option value="${value}"${value === mode ? " selected" : ""}>${value}</option>`).join("")}
                        </select></label>
                        <label>半径(格)<input class="form-control form-control-sm" type="number" min="0" data-poi-field="radius" value="${escapeHtml(numberValue(trigger.radius_cells))}"${readonly ? " disabled" : ""}></label>
                        <label class="scenario-map-check"><input type="checkbox" data-poi-field="once"${trigger.once ? " checked" : ""}${readonly ? " disabled" : ""}> 只触发一次</label>
                    </div>
                    <div class="scenario-map-form-grid">
                        <label>x<input class="form-control form-control-sm" type="number" data-poi-field="x" value="${escapeHtml(numberValue(rect.x))}"${readonly ? " disabled" : ""}></label>
                        <label>y<input class="form-control form-control-sm" type="number" data-poi-field="y" value="${escapeHtml(numberValue(rect.y))}"${readonly ? " disabled" : ""}></label>
                        <label>w<input class="form-control form-control-sm" type="number" min="0" data-poi-field="w" value="${escapeHtml(numberValue(rect.w))}"${readonly ? " disabled" : ""}></label>
                        <label>h<input class="form-control form-control-sm" type="number" min="0" data-poi-field="h" value="${escapeHtml(numberValue(rect.h))}"${readonly ? " disabled" : ""}></label>
                    </div>
                    <label class="scenario-map-check"><input type="checkbox" data-poi-field="provisional"${provisional ? " checked" : ""}${readonly ? " disabled" : ""}> 几何推算（provisional）</label>
                    <label>推算原因<input class="form-control form-control-sm" data-poi-field="note" value="${escapeHtml(text(geometry.note))}" placeholder="例如：底图未标定"${readonly ? " disabled" : ""}></label>
                    <label>入口摘要<textarea class="form-control form-control-sm" rows="2" data-poi-field="entry-brief"${readonly ? " disabled" : ""}>${escapeHtml(text(block.entry_brief))}</textarea></label>
                    ${readonly ? "" : `<button type="button" class="btn btn-sm btn-primary" data-poi-save="${escapeHtml(poi.id)}">保存兴趣点</button>`}
                </div>
            </details>`;
    }

    function channelCard(channel: Channel, readonly: boolean): string {
        const edge = channel.edge;
        const transition = dict(edge.transition);
        const direction = text(transition.direction || (edge.bidirectional === false ? "one_way" : "two_way"));
        const target = channel.inbound ? channel.fromTitle : channel.toTitle;
        const arrow = channel.inbound ? "←" : "→";
        const hiddenEdge = (edge.visibility || "public") === "hidden";
        return `
            <details class="scenario-map-channel" data-edge-id="${escapeHtml(edge.id)}">
                <summary class="scenario-map-channel-row">
                    <span class="scenario-map-channel-arrow">${arrow}</span>
                    <span class="scenario-map-channel-target">${escapeHtml(target)}</span>
                    <span class="badge bg-light text-dark">${direction === "two_way" ? "双向" : "单向"}</span>
                    <span class="scenario-map-channel-label">${escapeHtml(text(transition.label || edge.label) || "通道")}</span>
                    ${(edge.visibility || "public") === "kp_only" ? '<span class="badge bg-secondary">KP ONLY</span>' : ""}
                </summary>
                <div class="scenario-map-channel-body">
                    ${hiddenEdge || (state.viewMode === "player" && (edge.visibility || "public") !== "public")
                        ? ""
                        : `<p class="scenario-map-channel-text">${escapeHtml(text(transition.text))}</p>`}
                    <div class="scenario-map-form-grid">
                        <label>标签<input class="form-control form-control-sm" data-edge-field="label" value="${escapeHtml(text(transition.label || edge.label))}"${readonly ? " disabled" : ""}></label>
                        <label>方向<select class="form-select form-select-sm" data-edge-field="direction"${readonly ? " disabled" : ""}>
                            <option value="one_way"${direction === "one_way" ? " selected" : ""}>单向</option>
                            <option value="two_way"${direction === "two_way" ? " selected" : ""}>双向</option>
                        </select></label>
                        <label>from_area<input class="form-control form-control-sm" data-edge-field="from-area" value="${escapeHtml(text(transition.from_area))}"${readonly ? " disabled" : ""}></label>
                        <label>to_area<input class="form-control form-control-sm" data-edge-field="to-area" value="${escapeHtml(text(transition.to_area))}"${readonly ? " disabled" : ""}></label>
                        <label>from_poi<input class="form-control form-control-sm" data-edge-field="from-poi" value="${escapeHtml(text(transition.from_poi))}"${readonly ? " disabled" : ""}></label>
                        <label>entry_poi<input class="form-control form-control-sm" data-edge-field="entry-poi" value="${escapeHtml(text(transition.entry_poi))}"${readonly ? " disabled" : ""}></label>
                        <label>耗时(分)<input class="form-control form-control-sm" type="number" min="0" data-edge-field="time" value="${escapeHtml(numberValue(transition.time_cost_minutes))}"${readonly ? " disabled" : ""}></label>
                    </div>
                    <label>过场文本<textarea class="form-control form-control-sm" rows="2" data-edge-field="text"${readonly ? " disabled" : ""}>${escapeHtml(text(transition.text))}</textarea></label>
                    ${readonly ? "" : `<button type="button" class="btn btn-sm btn-outline-primary" data-edge-save="${escapeHtml(edge.id)}">保存过场</button>`}
                </div>
            </details>`;
    }

    interface Issue {
        level: "error" | "warn";
        text: string;
    }

    function graphIssues(): Issue[] {
        const issues: Issue[] = [];
        const maps = new Map<string, GraphNode>();
        mapNodes().forEach((node) => maps.set(node.id, node));
        const codes = new Map<string, Set<string>>();

        maps.forEach((map) => {
            const block = dict(map.map);
            const sceneId = text(block.scene_id).trim();
            if (!sceneId) {
                issues.push({ level: "error", text: `地图「${map.title || map.id}」缺少 scene_id。` });
            } else if (sceneId !== map.id) {
                issues.push({ level: "warn", text: `地图「${map.id}」的 scene_id 与 area_id 不一致（${sceneId}）。` });
            }
            if (!numberValue(block.bounds_cells)) {
                issues.push({ level: "warn", text: `地图「${map.id}」未填写 bounds_cells。` });
            }
            if (!text(block.brief).trim()) {
                issues.push({ level: "warn", text: `地图「${map.id}」缺少 AreaBrief 简报。` });
            }
            const poiIds = new Set(poiNodes(map.id).map((node) => node.id));
            const markers = mapMarkers(map);
            if (markers.length) {
                markers.forEach((marker) => {
                    if (!marker.poi_id) return;
                    if (!poiIds.has(marker.poi_id)) {
                        issues.push({
                            level: "error",
                            text: `地图「${map.id}」的标记「${marker.label || marker.id}」关联的兴趣点 ${marker.poi_id} 不存在或不属于该地图。`,
                        });
                    }
                });
                const markerIds = new Set(markers.map((marker) => marker.id));
                poiNodes(map.id).forEach((node) => {
                    const markerId = text(dict(dict(node.poi).geometry).marker_id);
                    if (markerId && !markerIds.has(markerId)) {
                        issues.push({
                            level: "warn",
                            text: `兴趣点「${node.title || node.id}」关联的标记 ${markerId} 不在这张地图的标记里。`,
                        });
                    }
                });
            }
        });

        (state.bundle?.nodes || []).filter(isPoiNode).forEach((node) => {
            const block = dict(node.poi);
            const areaId = poiArea(node);
            if (!areaId || !maps.has(areaId)) {
                issues.push({ level: "error", text: `兴趣点「${node.title || node.id}」的 area_id 不是地图节点。` });
            }
            const code = text(block.code).trim();
            if (code && areaId) {
                const bucket = codes.get(areaId) || new Set<string>();
                if (bucket.has(code)) {
                    issues.push({ level: "error", text: `地图「${areaId}」的编号 ${code} 重复。` });
                }
                bucket.add(code);
                codes.set(areaId, bucket);
            }
            const trigger = dict(block.trigger);
            const geometry = dict(block.geometry);
            const rect = dict(geometry.rect_cells);
            const mode = text(trigger.mode || "manual");
            if ((mode === "enter" || mode === "adjacent") && !(numberValue(rect.w) > 0 && numberValue(rect.h) > 0)) {
                issues.push({ level: "error", text: `兴趣点「${node.title || node.id}」是 ${mode} 触发，但缺少格几何。` });
            }
            if (geometry.provisional === true && !text(geometry.note).trim()) {
                issues.push({ level: "warn", text: `兴趣点「${node.title || node.id}」标记为 provisional，但没有写明原因。` });
            }
        });

        (state.bundle?.edges || []).filter((edge) => (edge.type || "") === "portal").forEach((edge) => {
            const transition = dict(edge.transition);
            if (!Object.keys(transition).length) {
                issues.push({ level: "error", text: `通道 ${edge.id} 缺少 transition 过场。` });
                return;
            }
            const toArea = text(transition.to_area);
            if (toArea && !maps.has(toArea)) {
                issues.push({ level: "error", text: `通道 ${edge.id} 的 to_area「${toArea}」不是地图节点。` });
            }
            const entryPoi = text(transition.entry_poi);
            if (entryPoi) {
                const entry = N.nodeById(entryPoi);
                if (!entry || areaOf(entryPoi) !== toArea) {
                    issues.push({ level: "error", text: `通道 ${edge.id} 的 entry_poi「${entryPoi}」不在目标地图内。` });
                }
            }
        });
        return issues;
    }

    function renderMapGraph(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;
        const bundle = state.bundle;
        if (!bundle || !(bundle.nodes || []).length) {
            canvas.innerHTML = `<p class="text-muted small p-3 mb-0">${escapeHtml(t("scenario.tree.empty", "保存剧本并生成世界树后，可在此查看章节、地点与结局。"))}</p>`;
            return;
        }
        const maps = mapNodes().filter(isVisible);
        const pois = (bundle.nodes || []).filter((node) => isPoiNode(node) && isVisible(node));
        const channels = portalChannels().filter(channelVisible);
        const issues = graphIssues();
        const provisional = pois.filter((node) => dict(dict(node.poi).geometry).provisional === true).length;
        if (!maps.length) {
            canvas.innerHTML = `<div class="scenario-map-graph">
                <div class="scenario-map-graph-summary">该剧本还没有标记为地图的大节点。</div>
                <p class="text-muted small p-3 mb-0">在世界树里把承载底图的大节点设为 <code>node_role = map</code> 后，这里会按地图展开兴趣点。</p>
            </div>`;
            return;
        }
        canvas.innerHTML = `
            <div class="scenario-map-graph">
                <div class="scenario-map-graph-summary">
                    <span><i class="fa fa-map" aria-hidden="true"></i> ${maps.length} 张地图</span>
                    <span><i class="fa fa-location-dot" aria-hidden="true"></i> ${pois.length} 个兴趣点</span>
                    <span class="${provisional ? "is-warn" : ""}"><i class="fa fa-triangle-exclamation" aria-hidden="true"></i> ${provisional} 个 provisional</span>
                    <span><i class="fa fa-door-open" aria-hidden="true"></i> ${channels.length} 条跨图/进入通道</span>
                    <span class="${issues.some((item) => item.level === "error") ? "is-error" : issues.length ? "is-warn" : "is-ok"}">
                        ${issues.length ? `${issues.filter((item) => item.level === "error").length} 个错误 · ${issues.filter((item) => item.level === "warn").length} 个提示` : "校验通过"}
                    </span>
                </div>
                ${issues.length ? `<ul class="scenario-map-graph-issues">${issues.map((issue) => `<li class="${issue.level === "error" ? "is-error" : "is-warn"}">${issue.level === "error" ? "错误" : "提示"}：${escapeHtml(issue.text)}</li>`).join("")}</ul>` : ""}
                ${maps.map((map) => mapCard(map, channels)).join("")}
            </div>`;
        api.setGraphHint(t(
            "scenario.graph.map_hint",
            "地图层画法：地图是大节点，兴趣点挂在所属地图下；只有跨图/进入通道会连线并标注方向与过场。",
        ));
    }

    function renderMapGuide(): void {
        const host = byId("scenarioGraphGuideBody");
        if (!host) return;
        host.innerHTML = `
            <p class="mb-2">${escapeHtml(t("scenario.graph.map_guide_intro", "地图层画法按「地图 → 兴趣点」组织：每张地图是一张大卡片，展开后是这张图里的兴趣点；兴趣点之间不画网状关系线。"))}</p>
            <ul class="mb-2">
                <li>${escapeHtml(t("scenario.graph.map_guide_children", "大节点 = 地图；小节点 = 该图的兴趣点。兴趣点通过 area_id 或 parent 归属地图。"))}</li>
                <li>${escapeHtml(t("scenario.graph.map_guide_portal", "地图之间只用 portal 通道相连；通道行上标注方向（单向/双向）与过场文本。"))}</li>
                <li>${escapeHtml(t("scenario.graph.map_guide_calibration", "底图标定在地图页签完成；标定后可以把兴趣点的格几何与 DM 标记批量回写。"))}</li>
                <li>${escapeHtml(t("scenario.graph.map_guide_visibility", "玩家视图会隐藏 KP ONLY 内容；未揭示的地图与通道只显示占位，不显示过场文本。"))}</li>
            </ul>`;
    }

    // ------------------------------------------------------------------
    // 编辑
    // ------------------------------------------------------------------

    function fieldValue(root: Element, name: string): string {
        const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[data-map-field="${name}"]`);
        return String(field?.value ?? "").trim();
    }

    function poiValue(root: Element, name: string): string {
        const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[data-poi-field="${name}"]`);
        return String(field?.value ?? "").trim();
    }

    function poiChecked(root: Element, name: string): boolean {
        return root.querySelector<HTMLInputElement>(`[data-poi-field="${name}"]`)?.checked === true;
    }

    function edgeValue(root: Element, name: string): string {
        const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[data-edge-field="${name}"]`);
        return String(field?.value ?? "").trim();
    }

    function replaceNode(updated: GraphNode): void {
        if (!state.bundle) return;
        state.bundle.nodes = state.bundle.nodes.map((node) => (node.id === updated.id ? updated : node));
    }

    async function saveMap(mapId: string): Promise<void> {
        const node = N.nodeById(mapId);
        const card = document.querySelector<HTMLElement>(`.scenario-map-card[data-map-id="${CSS.escape(mapId)}"]`);
        if (!node || !card || !state.scenarioId || !editable()) return;
        const block = dict(node.map);
        const environment = {
            ...dict(block.environment),
            lighting: fieldValue(card, "lighting"),
            temperature: fieldValue(card, "temperature"),
            smell: fieldValue(card, "smell"),
            sound: fieldValue(card, "sound"),
            terrain: fieldValue(card, "terrain").split(/[、,，]/).map((item) => item.trim()).filter(Boolean),
        };
        const entry = fieldValue(card, "entry").split(",").map((item) => item.trim()).filter(Boolean);
        const nextMap: Dict = {
            ...block,
            scene_id: fieldValue(card, "scene") || node.id,
            grid_ft: numberValue(fieldValue(card, "grid")) || 5,
            bounds_cells: numberValue(fieldValue(card, "bounds")),
            environment,
            brief: fieldValue(card, "brief"),
            dm_brief: fieldValue(card, "dm-brief"),
            entry_pois: entry,
        };
        const parent = fieldValue(card, "parent");
        if (parent) nextMap.parent_area = parent;
        else delete nextMap.parent_area;
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(mapId)}`,
                {
                    method: "PATCH",
                    body: {
                        base_revision: state.revision,
                        title: fieldValue(card, "title") || node.id,
                        node_role: "map",
                        map: nextMap,
                    },
                },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            replaceNode(data.data.node);
            renderMapGraph();
            showNotification(t("scenario.graph.map_saved", "地图节点已保存。"), "success");
        } catch (error) {
            api.discardHistory(history);
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    async function savePoi(poiId: string): Promise<void> {
        const node = N.nodeById(poiId);
        const item = document.querySelector<HTMLElement>(`.scenario-map-poi-item[data-poi-id="${CSS.escape(poiId)}"]`);
        if (!node || !item || !state.scenarioId || !editable()) return;
        const block = dict(node.poi);
        const trigger = dict(block.trigger);
        const geometry = dict(block.geometry);
        const width = numberValue(poiValue(item, "w"));
        const height = numberValue(poiValue(item, "h"));
        const nextGeometry: Dict = {
            ...geometry,
            provisional: poiChecked(item, "provisional"),
        };
        const note = poiValue(item, "note");
        if (note) nextGeometry.note = note;
        else delete nextGeometry.note;
        if (width > 0 && height > 0) {
            nextGeometry.rect_cells = {
                x: numberValue(poiValue(item, "x")),
                y: numberValue(poiValue(item, "y")),
                w: width,
                h: height,
            };
        } else {
            delete nextGeometry.rect_cells;
        }
        const nextPoi: Dict = {
            ...block,
            area_id: poiValue(item, "area") || (node.parent || ""),
            code: poiValue(item, "code") || null,
            trigger: {
                ...trigger,
                mode: poiValue(item, "mode") || "manual",
                radius_cells: numberValue(poiValue(item, "radius")),
                once: poiChecked(item, "once"),
            },
            geometry: nextGeometry,
            entry_brief: poiValue(item, "entry-brief"),
        };
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ node: GraphNode; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/nodes/${encodeURIComponent(poiId)}`,
                {
                    method: "PATCH",
                    body: {
                        base_revision: state.revision,
                        title: poiValue(item, "title") || node.id,
                        node_role: "poi",
                        parent: poiValue(item, "area") || node.parent || null,
                        poi: nextPoi,
                    },
                },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            replaceNode(data.data.node);
            renderMapGraph();
            showNotification(t("scenario.graph.poi_saved", "兴趣点已保存。"), "success");
        } catch (error) {
            api.discardHistory(history);
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    async function saveChannel(edgeId: string): Promise<void> {
        const edge = edgeById(edgeId);
        const row = document.querySelector<HTMLElement>(`.scenario-map-channel[data-edge-id="${CSS.escape(edgeId)}"]`);
        if (!edge || !row || !state.scenarioId || !editable()) return;
        const direction = edgeValue(row, "direction") || "one_way";
        const time = numberValue(edgeValue(row, "time"));
        const transition: Dict = {
            ...dict(edge.transition),
            from_area: edgeValue(row, "from-area"),
            to_area: edgeValue(row, "to-area"),
            from_poi: edgeValue(row, "from-poi") || null,
            entry_poi: edgeValue(row, "entry-poi") || null,
            direction,
            label: edgeValue(row, "label"),
            text: edgeValue(row, "text"),
            time_cost_minutes: time,
            handoff: {
                ...dict(dict(edge.transition).handoff),
                inject_area_brief: true,
                inject_entry_poi: true,
                time_cost_minutes: time,
            },
        };
        const history = api.pushHistory();
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{ edge: GraphEdge; revision: number }>>(
                `/api/scenarios/${state.scenarioId}/narrative/edges/${encodeURIComponent(edgeId)}`,
                {
                    method: "PATCH",
                    body: {
                        base_revision: state.revision,
                        type: "portal",
                        label: transition.label,
                        bidirectional: direction === "two_way",
                        transition,
                    },
                },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 409) {
                    api.discardHistory(history);
                    await api.handleConflict();
                    return;
                }
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            applyRevision(data.data.revision);
            if (state.bundle) {
                state.bundle.edges = state.bundle.edges.map((item) => (item.id === data.data!.edge.id ? data.data!.edge : item));
            }
            renderMapGraph();
            showNotification(t("scenario.graph.channel_saved", "过场已保存。"), "success");
        } catch (error) {
            api.discardHistory(history);
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function bindEvents(): void {
        const canvas = byId("scenarioGraphCanvas");
        if (!canvas) return;
        canvas.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            const mapSave = target.closest<HTMLElement>("[data-map-save]");
            if (mapSave?.dataset.mapSave) {
                void saveMap(mapSave.dataset.mapSave);
                return;
            }
            const poiSave = target.closest<HTMLElement>("[data-poi-save]");
            if (poiSave?.dataset.poiSave) {
                void savePoi(poiSave.dataset.poiSave);
                return;
            }
            const edgeSave = target.closest<HTMLElement>("[data-edge-save]");
            if (edgeSave?.dataset.edgeSave) {
                void saveChannel(edgeSave.dataset.edgeSave);
                return;
            }
            const mapId = target.closest<HTMLElement>("[data-map-id]")?.dataset.mapId;
            if (mapId && target.closest(".scenario-map-card-head")) {
                state.selectedId = mapId;
                state.selectedEdgeId = "";
                emitSelection(N.nodeById(mapId));
                return;
            }
            const poiId = target.closest<HTMLElement>("[data-poi-id]")?.dataset.poiId;
            if (poiId && target.closest(".scenario-map-poi-head")) {
                state.selectedId = poiId;
                state.selectedEdgeId = "";
                emitSelection(N.nodeById(poiId));
            }
        });
        // toggle 不冒泡，用捕获阶段记录地图折叠状态，重绘后仍能保持。
        canvas.addEventListener("toggle", (event) => {
            const details = event.target as HTMLDetailsElement;
            const mapId = details.closest<HTMLElement>("[data-map-id]")?.dataset.mapId;
            if (!mapId || !details.classList.contains("scenario-map-card")) return;
            if (details.open) collapsedMaps.delete(mapId);
            else collapsedMaps.add(mapId);
        }, true);
    }

    bindEvents();

    Object.assign(api, {
        renderMapGraph,
        renderMapGuide,
    });
})(window);
