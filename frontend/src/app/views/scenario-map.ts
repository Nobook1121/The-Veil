// 剧本地图编辑器：导入底图、人工标定格子大小、放置 DM/玩家标记。
// 标定结果写入 scenario.map_config[<场景模块 id>]，房间侧（veil-map）
// 直接按每格像素与偏移对齐画布网格，不再依赖图片尺寸自动推测。

interface ScenarioMapSceneOption {
    id: string;
    title: string;
}

interface ScenarioMapDragState {
    mode: "pan" | "marker";
    pointerId: number;
    startX: number;
    startY: number;
    panX: number;
    panY: number;
    draft: HTMLElement | null;
}

/** 叙事图谱的最小结构：地图页只读写 node_role / map / poi / transition。 */
interface ScenarioMapGraphNode {
    id: string;
    kind?: string;
    title?: string;
    parent?: string | null;
    node_role?: string;
    visibility?: string;
    summary?: string;
    body_ref?: { module_id?: string } | null;
    map?: Record<string, unknown> | null;
    poi?: Record<string, unknown> | null;
}

interface ScenarioMapGraphEdge {
    id: string;
    from: string;
    to: string;
    type?: string;
    label?: string;
    bidirectional?: boolean;
    transition?: Record<string, unknown> | null;
    condition?: unknown;
}

interface ScenarioMapGraphBundle {
    nodes: ScenarioMapGraphNode[];
    edges: ScenarioMapGraphEdge[];
    [key: string]: unknown;
}

const SCENARIO_MAP_FRAME_SIZE = 96;

function scenarioMapRound(value: number, digits = 3): number {
    const factor = 10 ** digits;
    return Math.round(value * factor) / factor;
}

function scenarioMapClamp(value: number, minimum: number, maximum: number): number {
    return Math.max(minimum, Math.min(maximum, value));
}

function scenarioMapEl<T extends HTMLElement>(id: string): T | null {
    const element = document.getElementById(id);
    return element instanceof HTMLElement ? (element as T) : null;
}

function scenarioMapMarkerTypeVisibility(value: unknown): ScenarioMapMarkerVisibility {
    const text = String(value || "").trim().toLowerCase();
    return text === "kp" || text === "kp_only" || text === "dm" ? "kp" : "public";
}

/** 把服务端/草稿中的地图配置归一化，避免缺字段导致渲染崩溃。 */
function normalizeScenarioMapConfig(raw: unknown): ScenarioMapConfig {
    const result: ScenarioMapConfig = {};
    if (!raw || typeof raw !== "object") return result;
    Object.entries(raw as Record<string, unknown>).forEach(([sceneId, value]) => {
        if (!value || typeof value !== "object") return;
        const item = value as Record<string, unknown>;
        const types: ScenarioMapMarkerType[] = [];
        (Array.isArray(item.marker_types) ? item.marker_types : []).forEach((entry, index) => {
            if (!entry || typeof entry !== "object") return;
            const type = entry as Record<string, unknown>;
            const label = String(type.label || type.name || "").trim();
            if (!label) return;
            const color = String(type.color || "").trim();
            types.push({
                id: String(type.id || `type-${index + 1}`).slice(0, 40),
                label: label.slice(0, 40),
                color: /^#[0-9a-fA-F]{6}$/.test(color) ? color : "#f6b26b",
                description: String(type.description || type.note || "").trim().slice(0, 400),
                visibility: scenarioMapMarkerTypeVisibility(type.visibility),
            });
        });
        const typesById = new Map(types.map((type) => [type.id, type]));
        const markers: ScenarioMapMarker[] = [];
        (Array.isArray(item.markers) ? item.markers : []).forEach((entry, index) => {
            if (!entry || typeof entry !== "object") return;
            const marker = entry as Record<string, unknown>;
            const width = Number(marker.w);
            const height = Number(marker.h);
            if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
            const typeId = String(marker.type || "").slice(0, 40);
            const fallback = typesById.get(typeId);
            markers.push({
                id: String(marker.id || `marker-${index + 1}`).slice(0, 60),
                type: typeId,
                label: String(marker.label || marker.name || "").trim().slice(0, 80),
                x: Number(marker.x) || 0,
                y: Number(marker.y) || 0,
                w: width,
                h: height,
                visibility: scenarioMapMarkerTypeVisibility(
                    marker.visibility || (fallback ? fallback.visibility : "public"),
                ),
                ...(marker.poi_id ? { poi_id: String(marker.poi_id).slice(0, 80) } : {}),
            });
        });
        result[sceneId] = {
            background_url: String(item.background_url || "").trim(),
            ...(item.background_asset_id ? { background_asset_id: String(item.background_asset_id) } : {}),
            width: Math.max(0, Math.round(Number(item.width) || 0)),
            height: Math.max(0, Math.round(Number(item.height) || 0)),
            cell_px: Math.max(0, Number(item.cell_px) || 0),
            offset_x: Number(item.offset_x) || 0,
            offset_y: Number(item.offset_y) || 0,
            marker_types: types,
            markers,
        };
    });
    return result;
}

class ScenarioMapEditor {
    private config: ScenarioMapConfig = {};
    private sceneId = "";
    private imageUrl = "";
    private scale = 1;
    private panX = 0;
    private panY = 0;
    private naturalWidth = 0;
    private naturalHeight = 0;
    private imageReady = false;
    private placing = false;
    private readOnly = false;
    private drag: ScenarioMapDragState | null = null;
    private selectedMarker = "";
    private bound = false;
    private pendingFit = false;
    private rulesetId = "coc7";
    private graphStyle: "mesh" | "map" = "mesh";
    private graph: ScenarioMapGraphBundle | null = null;
    private graphScenarioId = 0;
    private graphRevision = 0;
    private graphReadonly = false;
    private graphLoading = false;
    private batchSnapshot: { bundle: ScenarioMapGraphBundle; revision: number } | null = null;
    private batchRevision = 0;

    constructor() {
        this.bind();
    }

    reset(): void {
        this.config = {};
        this.sceneId = "";
        this.imageUrl = "";
        this.scale = 1;
        this.panX = 0;
        this.panY = 0;
        this.naturalWidth = 0;
        this.naturalHeight = 0;
        this.imageReady = false;
        this.placing = false;
        this.selectedMarker = "";
        this.drag = null;
        this.pendingFit = false;
        this.rulesetId = "coc7";
        this.graphStyle = "mesh";
        this.graph = null;
        this.graphScenarioId = 0;
        this.graphRevision = 0;
        this.graphReadonly = false;
        this.graphLoading = false;
        this.batchSnapshot = null;
        this.batchRevision = 0;
        this.refreshScenes();
    }

    load(mapConfig: ScenarioMapConfig | undefined, rulesetId = "coc7"): void {
        this.config = normalizeScenarioMapConfig(mapConfig);
        this.rulesetId = String(rulesetId || "coc7").trim().toLowerCase() || "coc7";
        this.graphStyle = this.rulesetId === "dnd5e" ? "map" : "mesh";
        this.graph = null;
        this.graphScenarioId = 0;
        this.graphRevision = 0;
        this.batchSnapshot = null;
        this.batchRevision = 0;
        this.sceneId = "";
        this.imageUrl = "";
        this.imageReady = false;
        this.selectedMarker = "";
        this.pendingFit = false;
        this.refreshScenes();
        this.renderBatchState();
    }

    collect(): ScenarioMapConfig {
        const existingScenes = new Set(this.domSceneOptions().map((scene) => scene.id));
        this.graphNodes().filter((node) => this.isMapNode(node)).forEach((node) => existingScenes.add(node.id));
        const result: ScenarioMapConfig = {};
        Object.entries(this.config).forEach(([sceneId, config]) => {
            if (existingScenes.size > 0 && !existingScenes.has(sceneId)) return;
            if (!config.background_url && config.markers.length === 0 && config.marker_types.length === 0) return;
            result[sceneId] = {
                ...config,
                marker_types: config.marker_types.map((type) => ({ ...type })),
                markers: config.markers.map((marker) => ({ ...marker })),
            };
        });
        return result;
    }

    setReadOnly(readonly: boolean): void {
        this.readOnly = readonly;
        const pane = document.getElementById("scenarioTabMap");
        pane?.classList.toggle("is-readonly", readonly);
        pane?.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>("input, select, button").forEach((field) => {
            field.disabled = readonly;
        });
    }

    onTabShown(): void {
        this.refreshScenes(true);
        const pane = document.getElementById("scenarioTabMap");
        if (this.readOnly) {
            pane?.classList.add("is-readonly");
        }
        window.setTimeout(() => {
            if (this.pendingFit) this.fit();
            else this.renderImage();
            this.renderMarkers();
        }, 30);
        if (this.graphStyle !== "map") return;
        void this.loadGraph().then(() => {
            this.refreshScenes(true);
            this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
            this.renderBatchState();
        });
    }

    private bind(): void {
        if (this.bound) return;
        this.bound = true;
        scenarioMapEl<HTMLSelectElement>("scenarioMapSceneSelect")?.addEventListener("change", (event) => {
            this.sceneId = (event.target as HTMLSelectElement).value;
            this.selectedMarker = "";
            this.applyInitialView();
        });
        scenarioMapEl<HTMLInputElement>("scenarioMapUpload")?.addEventListener("change", (event) => {
            const input = event.target as HTMLInputElement;
            const file = input.files?.[0];
            input.value = "";
            if (file) void this.importBackground(file);
        });
        scenarioMapEl("scenarioMapImportButton")?.addEventListener("click", () => {
            if (this.readOnly) return;
            scenarioMapEl<HTMLInputElement>("scenarioMapUpload")?.click();
        });
        scenarioMapEl("scenarioMapZoomIn")?.addEventListener("click", () => this.zoomBy(1.2));
        scenarioMapEl("scenarioMapZoomOut")?.addEventListener("click", () => this.zoomBy(1 / 1.2));
        scenarioMapEl("scenarioMapFit")?.addEventListener("click", () => this.fit());
        scenarioMapEl("scenarioMapActual")?.addEventListener("click", () => this.actualSize());
        scenarioMapEl("scenarioMapCalibrate")?.addEventListener("click", () => this.calibrate());
        scenarioMapEl("scenarioMapMarkerMode")?.addEventListener("click", () => this.togglePlacing());
        scenarioMapEl("scenarioMapDeleteMarker")?.addEventListener("click", () => this.deleteSelectedMarker());
        scenarioMapEl("scenarioMapAddType")?.addEventListener("click", () => this.addMarkerType());
        scenarioMapEl("scenarioMapClearCalibration")?.addEventListener("click", () => this.clearCalibration());
        scenarioMapEl("scenarioMapBatchWriteback")?.addEventListener("click", () => void this.batchWriteback());
        scenarioMapEl("scenarioMapBatchUndo")?.addEventListener("click", () => void this.undoBatchWriteback());

        const viewport = scenarioMapEl("scenarioMapViewport");
        viewport?.addEventListener("pointerdown", (event) => this.onPointerDown(event));
        viewport?.addEventListener("pointermove", (event) => this.onPointerMove(event));
        viewport?.addEventListener("pointerup", (event) => this.onPointerUp(event));
        viewport?.addEventListener("pointercancel", (event) => this.onPointerUp(event));
        viewport?.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });
        viewport?.addEventListener("pointerleave", () => {
            if (this.drag && this.drag.mode === "marker") this.cancelDrag();
        });

        scenarioMapEl("scenarioMapMarkerLayer")?.addEventListener("pointerdown", (event) => {
            const target = event.target as HTMLElement;
            const marker = target.closest<HTMLElement>("[data-map-marker-id]");
            if (!marker) return;
            event.stopPropagation();
            this.selectedMarker = marker.dataset.mapMarkerId || "";
            this.renderMarkers();
        });

        this.bindDelegatedList();
    }

    private bindDelegatedList(): void {
        const typeList = scenarioMapEl("scenarioMapMarkerTypes");
        const updateType = (event: Event) => {
            const target = event.target as HTMLElement;
            const row = target.closest<HTMLElement>("[data-map-type-id]");
            const typeId = row?.dataset.mapTypeId || "";
            const config = this.current();
            const type = config?.marker_types.find((item) => item.id === typeId);
            if (!row || !type || this.readOnly) return;
            if (target.matches("[data-map-type-label]")) {
                type.label = (target as HTMLInputElement).value.trim().slice(0, 40) || "标记";
            } else if (target.matches("[data-map-type-color]")) {
                type.color = (target as HTMLInputElement).value || "#f6b26b";
            } else if (target.matches("[data-map-type-visibility]")) {
                type.visibility = scenarioMapMarkerTypeVisibility((target as HTMLSelectElement).value);
            } else if (target.matches("[data-map-type-description]")) {
                type.description = (target as HTMLInputElement).value.slice(0, 400);
            } else {
                return;
            }
            this.renderMarkerLayer();
            this.renderMarkerRows();
        };
        typeList?.addEventListener("input", updateType);
        typeList?.addEventListener("change", updateType);
        typeList?.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLElement>("[data-map-type-remove]");
            if (!button || this.readOnly) return;
            const typeId = button.closest<HTMLElement>("[data-map-type-id]")?.dataset.mapTypeId || "";
            const config = this.current();
            if (!config) return;
            config.marker_types = config.marker_types.filter((type) => type.id !== typeId);
            config.markers.forEach((marker) => {
                if (marker.type === typeId) marker.type = "";
            });
            this.renderMarkerTypes();
            this.renderMarkers();
        });

        const markerList = scenarioMapEl("scenarioMapMarkers");
        const updateMarker = (event: Event) => {
            const target = event.target as HTMLElement;
            const row = target.closest<HTMLElement>("[data-map-marker-row]");
            const markerId = row?.dataset.mapMarkerRow || "";
            const config = this.current();
            const marker = config?.markers.find((item) => item.id === markerId);
            if (!row || !marker || this.readOnly) return;
            if (target.matches("[data-map-marker-label]")) {
                marker.label = (target as HTMLInputElement).value.slice(0, 80);
            } else if (target.matches("[data-map-marker-visibility]")) {
                marker.visibility = scenarioMapMarkerTypeVisibility((target as HTMLSelectElement).value);
            } else if (target.matches("[data-map-marker-poi]")) {
                const markerId = marker.id;
                const oldPoiId = marker.poi_id || this.poiByMarker(markerId)?.id || "";
                const poiId = (target as HTMLSelectElement).value;
                if (poiId) marker.poi_id = poiId;
                else delete marker.poi_id;
                void this.syncMarkerAssociation(markerId, oldPoiId, poiId);
            } else {
                return;
            }
            // 只重画底图上的框，避免输入时重建列表导致光标丢失。
            this.renderMarkerLayer();
            if (event.type === "change" && this.graphStyle === "map") this.syncMapConfigToNarrative();
        };
        markerList?.addEventListener("input", updateMarker);
        markerList?.addEventListener("change", updateMarker);
        markerList?.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLElement>("[data-map-marker-remove]");
            if (!button || this.readOnly) return;
            const markerId = button.closest<HTMLElement>("[data-map-marker-row]")?.dataset.mapMarkerRow || "";
            const config = this.current();
            if (!config) return;
            const removed = config.markers.find((marker) => marker.id === markerId);
            const oldPoiId = removed?.poi_id || this.poiByMarker(markerId)?.id || "";
            config.markers = config.markers.filter((marker) => marker.id !== markerId);
            if (this.selectedMarker === markerId) this.selectedMarker = "";
            this.renderMarkers();
            if (oldPoiId) void this.syncMarkerAssociation(markerId, oldPoiId, "");
        });
        markerList?.addEventListener("click", (event) => {
            const row = (event.target as HTMLElement).closest<HTMLElement>("[data-map-marker-row]");
            if (!row) return;
            this.selectedMarker = row.dataset.mapMarkerRow || "";
            this.renderMarkers();
        });
    }

    private domSceneOptions(): ScenarioMapSceneOption[] {
        const options: ScenarioMapSceneOption[] = [];
        document.querySelectorAll<HTMLElement>(".scenario-module-item").forEach((card) => {
            const type = card.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene";
            if (type !== "scene") return;
            const id = card.dataset.moduleId || "";
            if (!id) return;
            const title = card.querySelector<HTMLInputElement>("[data-module-title]")?.value.trim()
                || card.querySelector<HTMLElement>("[data-module-number]")?.textContent?.trim()
                || id;
            options.push({ id, title });
        });
        return options;
    }

    /** dnd 地图页按地图标定：下拉只列世界树里的地图大节点；未加载世界树时回退场景模块。 */
    private sceneOptions(): ScenarioMapSceneOption[] {
        if (this.graphStyle === "map") {
            const maps = this.graphNodes()
                .filter((node) => this.isMapNode(node))
                .map((node) => ({ id: node.id, title: String(node.title || node.id) }));
            if (maps.length) return maps;
        }
        return this.domSceneOptions();
    }

    private refreshScenes(keepScene = false): void {
        const select = scenarioMapEl<HTMLSelectElement>("scenarioMapSceneSelect");
        if (!select) return;
        const pane = document.getElementById("scenarioTabMap");
        if (pane) pane.dataset.graphStyle = this.graphStyle;
        const scenes = this.sceneOptions();
        const previous = keepScene ? this.sceneId : "";
        select.innerHTML = scenes.map((scene) => `<option value="${scenarioMapEscape(scene.id)}">${scenarioMapEscape(scene.title)}</option>`).join("");
        const next = scenes.some((scene) => scene.id === previous) ? previous : (scenes[0]?.id || "");
        this.sceneId = next;
        select.value = next;
        select.disabled = scenes.length === 0 || this.readOnly;
        const empty = scenarioMapEl("scenarioMapEmpty");
        if (empty) empty.hidden = scenes.length > 0;
        this.applyInitialView();
    }

    private current(): ScenarioSceneMapConfig | null {
        if (!this.sceneId) return null;
        return this.config[this.sceneId] || null;
    }

    private ensureCurrent(): ScenarioSceneMapConfig {
        let config = this.current();
        if (!config) {
            config = {
                background_url: "",
                width: 0,
                height: 0,
                cell_px: 0,
                offset_x: 0,
                offset_y: 0,
                marker_types: [],
                markers: [],
            };
            this.config[this.sceneId] = config;
        }
        return config;
    }

    private viewportSize(): { width: number; height: number } {
        const viewport = scenarioMapEl("scenarioMapViewport");
        return {
            width: Math.round(viewport?.clientWidth || 0),
            height: Math.round(viewport?.clientHeight || 0),
        };
    }

    private syncImage(config: ScenarioSceneMapConfig | null): void {
        const image = scenarioMapEl<HTMLImageElement>("scenarioMapImage");
        if (!image) return;
        const url = config?.background_url || "";
        if (url === this.imageUrl) return;
        this.imageUrl = url;
        this.imageReady = false;
        this.naturalWidth = config?.width || 0;
        this.naturalHeight = config?.height || 0;
        if (!url) {
            image.removeAttribute("src");
            this.imageReady = false;
            this.renderImage();
            return;
        }
        image.onload = () => {
            this.imageReady = true;
            this.naturalWidth = image.naturalWidth || this.naturalWidth;
            this.naturalHeight = image.naturalHeight || this.naturalHeight;
            const current = this.current();
            if (current) {
                current.width = this.naturalWidth;
                current.height = this.naturalHeight;
            }
            this.applyInitialView();
        };
        image.onerror = () => {
            this.imageReady = false;
            this.renderStatus("底图加载失败，请检查图片地址。", true);
            this.renderImage();
        };
        image.src = url;
        if (image.complete && image.naturalWidth > 0) {
            this.imageReady = true;
            this.naturalWidth = image.naturalWidth;
            this.naturalHeight = image.naturalHeight;
            this.applyInitialView();
        }
    }

    private applyInitialView(): void {
        const config = this.current();
        this.syncImage(config);
        const size = this.viewportSize();
        if (size.width <= 2 || size.height <= 2) {
            // 标签页隐藏时没有尺寸，等 shown.bs.tab 再做初始视图。
            this.pendingFit = true;
            return;
        }
        if (!config || !config.background_url) {
            this.scale = 1;
            this.panX = 0;
            this.panY = 0;
            this.pendingFit = false;
            this.renderAll();
            return;
        }
        if (config.cell_px > 0 && config.width > 0 && config.height > 0) {
            // 返回已标定场景时，把标定框重新对到锚点格上，作者能直接检查。
            this.scale = SCENARIO_MAP_FRAME_SIZE / config.cell_px;
            const frameLeft = (size.width - SCENARIO_MAP_FRAME_SIZE) / 2;
            const frameTop = (size.height - SCENARIO_MAP_FRAME_SIZE) / 2;
            this.panX = frameLeft - config.offset_x * this.scale;
            this.panY = frameTop - config.offset_y * this.scale;
            this.pendingFit = false;
            this.renderAll();
            return;
        }
        this.pendingFit = false;
        this.fit(false);
        this.renderAll();
    }

    private fit(render = true): void {
        const config = this.current();
        const size = this.viewportSize();
        if (!config || !config.background_url || !this.naturalWidth || !this.naturalHeight) return;
        const margin = 32;
        this.scale = Math.min(
            (size.width - margin) / this.naturalWidth,
            (size.height - margin) / this.naturalHeight,
            4,
        );
        if (!(this.scale > 0)) this.scale = 1;
        this.panX = (size.width - this.naturalWidth * this.scale) / 2;
        this.panY = (size.height - this.naturalHeight * this.scale) / 2;
        if (render) {
            this.renderImage();
            this.renderMarkers();
        }
    }

    private actualSize(): void {
        const size = this.viewportSize();
        this.scale = 1;
        this.panX = (size.width - this.naturalWidth) / 2;
        this.panY = (size.height - this.naturalHeight) / 2;
        this.renderImage();
        this.renderMarkers();
    }

    private zoomBy(factor: number): void {
        const size = this.viewportSize();
        this.applyZoom(factor, size.width / 2, size.height / 2);
    }

    private applyZoom(factor: number, anchorX: number, anchorY: number): void {
        if (!this.naturalWidth || !this.naturalHeight) return;
        const imageX = (anchorX - this.panX) / this.scale;
        const imageY = (anchorY - this.panY) / this.scale;
        this.scale = scenarioMapClamp(this.scale * factor, 0.02, 12);
        this.panX = anchorX - imageX * this.scale;
        this.panY = anchorY - imageY * this.scale;
        this.renderImage();
        this.renderMarkers();
    }

    private onWheel(event: WheelEvent): void {
        const config = this.current();
        if (!config || !config.background_url) return;
        event.preventDefault();
        const rect = scenarioMapEl("scenarioMapViewport")?.getBoundingClientRect();
        if (!rect) return;
        this.applyZoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - rect.left, event.clientY - rect.top);
    }

    private renderAll(): void {
        this.syncImage(this.current());
        this.renderImage();
        this.renderCalibration();
        this.renderMarkerTypes();
        this.renderMarkers();
        this.renderStatus();
    }

    private renderImage(): void {
        const image = scenarioMapEl<HTMLImageElement>("scenarioMapImage");
        const viewport = scenarioMapEl("scenarioMapViewport");
        if (!image || !viewport || !this.naturalWidth || !this.naturalHeight) {
            if (image) image.style.display = "none";
            return;
        }
        image.style.display = "block";
        image.style.width = `${this.naturalWidth * this.scale}px`;
        image.style.height = `${this.naturalHeight * this.scale}px`;
        image.style.left = `${this.panX}px`;
        image.style.top = `${this.panY}px`;
        viewport.classList.toggle("has-image", Boolean(this.imageUrl));
    }

    private renderCalibration(): void {
        const config = this.current();
        const summary = scenarioMapEl("scenarioMapCalibrationSummary");
        if (summary) {
            summary.textContent = config && config.cell_px > 0
                ? `每格 ${scenarioMapRound(config.cell_px, 2)} px；锚点 (${scenarioMapRound(config.offset_x, 1)}, ${scenarioMapRound(config.offset_y, 1)})`
                : "尚未标定：把底图上的一个格子放进中间方框，再点「记录标定」。";
        }
        const markerMode = scenarioMapEl<HTMLButtonElement>("scenarioMapMarkerMode");
        if (markerMode) markerMode.classList.toggle("active", this.placing);
        const frame = scenarioMapEl("scenarioMapCalibrationFrame");
        if (frame) frame.hidden = this.placing || !config || !config.background_url;
    }

    private renderStatus(message?: string, isError = false): void {
        const status = scenarioMapEl("scenarioMapStatus");
        if (!status) return;
        if (message === undefined) {
            const config = this.current();
            status.textContent = config && config.background_url
                ? `底图 ${config.width || this.naturalWidth} × ${config.height || this.naturalHeight} px，缩放 ${Math.round(this.scale * 100)}%`
                : "请先选择场景并导入底图。";
            status.classList.remove("is-error");
            return;
        }
        status.textContent = message;
        status.classList.toggle("is-error", isError);
    }

    private renderMarkerTypes(): void {
        const list = scenarioMapEl("scenarioMapMarkerTypes");
        const config = this.current();
        if (!list) return;
        const types = config?.marker_types || [];
        if (types.length === 0) {
            list.innerHTML = '<p class="scenario-map-empty-hint">还没有标记类型。添加类型后，DM 可以在这里看到每种标记的含义。</p>';
            return;
        }
        list.innerHTML = types.map((type) => `
            <div class="scenario-map-type-row" data-map-type-id="${scenarioMapEscape(type.id)}">
                <input type="color" data-map-type-color value="${scenarioMapEscape(type.color)}" aria-label="标记颜色">
                <input class="form-control form-control-sm" data-map-type-label value="${scenarioMapEscape(type.label)}" placeholder="标记名称" maxlength="40">
                <select class="form-select form-select-sm" data-map-type-visibility aria-label="标记可见性">
                    <option value="public"${type.visibility === "public" ? " selected" : ""}>玩家可见</option>
                    <option value="kp"${type.visibility === "kp" ? " selected" : ""}>仅 DM 可见</option>
                </select>
                <input class="form-control form-control-sm" data-map-type-description value="${scenarioMapEscape(type.description)}" placeholder="含义 / 说明（DM 阅读）" maxlength="400">
                <button type="button" class="btn btn-sm btn-outline-danger" data-map-type-remove aria-label="删除标记类型">×</button>
            </div>
        `).join("");
        list.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>("input, select, button").forEach((field) => {
            field.disabled = this.readOnly;
        });
    }

    private renderMarkers(): void {
        this.renderMarkerLayer();
        this.renderMarkerRows();
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
        if (this.graphStyle === "map") this.syncMapConfigToNarrative();
    }

    /** 把地图页正在编辑的 map_config 同步给图谱页，保证未保存的标记关联也能立即显示。 */
    private syncMapConfigToNarrative(): void {
        const narrative = (window as unknown as {
            TrpgNarrative?: {
                state?: { mapConfig?: ScenarioMapConfig };
                renderGraph?: () => void;
            };
        }).TrpgNarrative;
        if (!narrative?.state) return;
        narrative.state.mapConfig = JSON.parse(JSON.stringify(this.config)) as ScenarioMapConfig;
        narrative.renderGraph?.();
    }

    private renderMarkerLayer(): void {
        const layer = scenarioMapEl("scenarioMapMarkerLayer");
        const config = this.current();
        if (!layer) return;
        layer.innerHTML = "";
        if (!config) return;
        config.markers.forEach((marker) => {
            const type = config.marker_types.find((item) => item.id === marker.type);
            const color = type?.color || "#f6b26b";
            const element = document.createElement("div");
            element.className = `scenario-map-marker${marker.id === this.selectedMarker ? " is-selected" : ""}`;
            element.dataset.mapMarkerId = marker.id;
            element.style.left = `${this.panX + marker.x * this.scale}px`;
            element.style.top = `${this.panY + marker.y * this.scale}px`;
            element.style.width = `${Math.max(2, marker.w * this.scale)}px`;
            element.style.height = `${Math.max(2, marker.h * this.scale)}px`;
            element.style.borderColor = color;
            element.style.background = scenarioMapHexToRgba(color, 0.16);
            if (marker.visibility === "kp") element.classList.add("is-dm");
            const label = marker.label || type?.label || "";
            if (label) {
                const chip = document.createElement("span");
                chip.className = "scenario-map-marker-label";
                chip.textContent = label;
                chip.style.background = color;
                element.appendChild(chip);
            }
            layer.appendChild(element);
        });
    }

    private renderMarkerRows(): void {
        const list = scenarioMapEl("scenarioMapMarkers");
        const config = this.current();
        if (!list) return;
        const markers = config?.markers || [];
        if (markers.length === 0) {
            list.innerHTML = '<p class="scenario-map-empty-hint">还没有标记。点「放置标记」后在底图上拖出一个框。</p>';
            return;
        }
        list.innerHTML = markers.map((marker, index) => {
            const type = config?.marker_types.find((item) => item.id === marker.type);
            const linked = this.poiByMarker(marker.id);
            const linkedLabel = linked
                ? `<span class="scenario-map-marker-poi">已关联：${scenarioMapEscape(String(linked.poi?.code || "") ? String(linked.poi?.code) + ". " : "")}${scenarioMapEscape(linked.title || linked.id)}</span>`
                : '<span class="scenario-map-marker-poi is-empty">未关联兴趣点</span>';
            const linkedId = marker.poi_id || linked?.id || "";
            const mapPois = this.poiNodes(this.currentMapId());
            const mapPoiIds = new Set(mapPois.map((node) => node.id));
            const invalidLinked = Boolean(linkedId) && !mapPoiIds.has(linkedId);
            const poiSelect = this.graphStyle === "map"
                ? `<select class="form-select form-select-sm scenario-map-marker-poi-select${invalidLinked ? " is-invalid" : ""}" data-map-marker-poi aria-label="关联兴趣点">
                        <option value="">未关联</option>
                        ${invalidLinked ? `<option value="${scenarioMapEscape(linkedId)}" selected>无效关联：${scenarioMapEscape(linkedId)}</option>` : ""}
                        ${mapPois.map((node) => {
                            const code = String(node.poi?.code || "").trim();
                            const label = code ? `${code}. ${node.title || node.id}` : (node.title || node.id);
                            return `<option value="${scenarioMapEscape(node.id)}"${node.id === linkedId ? " selected" : ""}>${scenarioMapEscape(label)}</option>`;
                        }).join("")}
                    </select>`
                : linkedLabel;
            return `
                <div class="scenario-map-marker-row${marker.id === this.selectedMarker ? " is-selected" : ""}" data-map-marker-row="${scenarioMapEscape(marker.id)}">
                    <span class="scenario-map-swatch" style="background:${scenarioMapEscape(type?.color || "#f6b26b")}"></span>
                    <span class="scenario-map-marker-index">#${index + 1}</span>
                    <input class="form-control form-control-sm" data-map-marker-label value="${scenarioMapEscape(marker.label)}" placeholder="标记名称">
                    <select class="form-select form-select-sm" data-map-marker-visibility aria-label="标记可见性">
                        <option value="public"${marker.visibility === "public" ? " selected" : ""}>玩家可见</option>
                        <option value="kp"${marker.visibility === "kp" ? " selected" : ""}>仅 DM</option>
                    </select>
                    ${poiSelect}
                    <button type="button" class="btn btn-sm btn-outline-danger" data-map-marker-remove aria-label="删除标记">×</button>
                </div>
            `;
        }).join("");
        list.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>("input, select, button").forEach((field) => {
            field.disabled = this.readOnly;
        });
    }

    private calibrate(): void {
        const config = this.current();
        if (!config || !config.background_url) {
            this.renderStatus("请先导入底图。", true);
            return;
        }
        if (!this.imageReady || !this.naturalWidth || !this.naturalHeight) {
            this.renderStatus("底图尚未加载完成，请稍后再试。", true);
            return;
        }
        const size = this.viewportSize();
        const frameLeft = (size.width - SCENARIO_MAP_FRAME_SIZE) / 2;
        const frameTop = (size.height - SCENARIO_MAP_FRAME_SIZE) / 2;
        config.width = this.naturalWidth;
        config.height = this.naturalHeight;
        config.cell_px = scenarioMapRound(SCENARIO_MAP_FRAME_SIZE / this.scale);
        config.offset_x = scenarioMapRound((frameLeft - this.panX) / this.scale);
        config.offset_y = scenarioMapRound((frameTop - this.panY) / this.scale);
        this.renderCalibration();
        this.renderStatus();
        this.renderStatus("标定已记录：保存剧本后，房间会按这个格子大小与偏移对齐画布网格。");
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
        this.renderBatchState();
    }

    private clearCalibration(): void {
        const config = this.current();
        if (!config || this.readOnly) return;
        config.cell_px = 0;
        config.offset_x = 0;
        config.offset_y = 0;
        this.renderCalibration();
        this.renderStatus();
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
        this.renderBatchState();
    }

    private togglePlacing(): void {
        if (this.readOnly) return;
        const config = this.current();
        if (!config || !config.background_url) {
            this.renderStatus("请先导入底图，再放置标记。", true);
            return;
        }
        if (config.marker_types.length === 0) this.addMarkerType();
        this.placing = !this.placing;
        scenarioMapEl("scenarioMapViewport")?.classList.toggle("is-placing", this.placing);
        this.renderCalibration();
    }

    private addMarkerType(): void {
        if (this.readOnly) return;
        const config = this.current();
        if (!config) return;
        const type: ScenarioMapMarkerType = {
            id: `type-${Date.now().toString(36)}`,
            label: `标记 ${config.marker_types.length + 1}`,
            color: "#f6b26b",
            description: "",
            visibility: "kp",
        };
        config.marker_types.push(type);
        this.renderMarkerTypes();
        this.renderMarkers();
    }

    private deleteSelectedMarker(): void {
        if (this.readOnly || !this.selectedMarker) return;
        const config = this.current();
        if (!config) return;
        const markerId = this.selectedMarker;
        const removed = config.markers.find((marker) => marker.id === markerId);
        const oldPoiId = removed?.poi_id || this.poiByMarker(markerId)?.id || "";
        config.markers = config.markers.filter((marker) => marker.id !== markerId);
        this.selectedMarker = "";
        this.renderMarkers();
        if (oldPoiId) void this.syncMarkerAssociation(markerId, oldPoiId, "");
    }

    private onPointerDown(event: PointerEvent): void {
        const config = this.current();
        const viewport = scenarioMapEl("scenarioMapViewport");
        if (!config || !config.background_url || !viewport) return;
        if ((event.target as HTMLElement).closest("[data-map-marker-id]")) return;
        if (this.readOnly) return;
        event.preventDefault();
        this.drag = {
            mode: this.placing ? "marker" : "pan",
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            panX: this.panX,
            panY: this.panY,
            draft: null,
        };
        if (this.placing) {
            const layer = scenarioMapEl("scenarioMapMarkerLayer");
            if (layer) {
                const draft = document.createElement("div");
                draft.className = "scenario-map-marker is-draft";
                draft.style.borderColor = "#f5f0e2";
                layer.appendChild(draft);
                this.drag.draft = draft;
            }
        }
        viewport.setPointerCapture(event.pointerId);
    }

    private onPointerMove(event: PointerEvent): void {
        if (!this.drag || this.drag.pointerId !== event.pointerId) return;
        if (this.drag.mode === "pan") {
            this.panX = this.drag.panX + (event.clientX - this.drag.startX);
            this.panY = this.drag.panY + (event.clientY - this.drag.startY);
            this.renderImage();
            this.renderMarkers();
            return;
        }
        const rect = scenarioMapEl("scenarioMapViewport")?.getBoundingClientRect();
        if (!rect || !this.drag.draft) return;
        const start = this.imagePoint(this.drag.startX - rect.left, this.drag.startY - rect.top);
        const current = this.imagePoint(event.clientX - rect.left, event.clientY - rect.top);
        const draft = this.drag.draft;
        draft.style.left = `${this.panX + Math.min(start.x, current.x) * this.scale}px`;
        draft.style.top = `${this.panY + Math.min(start.y, current.y) * this.scale}px`;
        draft.style.width = `${Math.abs(current.x - start.x) * this.scale}px`;
        draft.style.height = `${Math.abs(current.y - start.y) * this.scale}px`;
    }

    private onPointerUp(event: PointerEvent): void {
        if (!this.drag || this.drag.pointerId !== event.pointerId) return;
        const drag = this.drag;
        this.drag = null;
        scenarioMapEl("scenarioMapViewport")?.releasePointerCapture?.(event.pointerId);
        if (drag.mode !== "marker") return;
        const rect = scenarioMapEl("scenarioMapViewport")?.getBoundingClientRect();
        if (!rect) return;
        const start = this.imagePoint(drag.startX - rect.left, drag.startY - rect.top);
        const end = this.imagePoint(event.clientX - rect.left, event.clientY - rect.top);
        const x = Math.min(start.x, end.x);
        const y = Math.min(start.y, end.y);
        let width = Math.abs(end.x - start.x);
        let height = Math.abs(end.y - start.y);
        const config = this.ensureCurrent();
        if (width < 4 || height < 4) {
            const cell = config.cell_px > 0 ? config.cell_px : 64 / Math.max(0.01, this.scale);
            width = cell;
            height = cell;
        }
        const type = config.marker_types[0];
        const marker: ScenarioMapMarker = {
            id: `marker-${Date.now().toString(36)}`,
            type: type?.id || "",
            label: "",
            x: scenarioMapRound(x),
            y: scenarioMapRound(y),
            w: scenarioMapRound(width),
            h: scenarioMapRound(height),
            visibility: type?.visibility || "kp",
        };
        config.markers.push(marker);
        // dnd 地图层：按兴趣点顺序把新标记关联到下一个未关联的 POI，省去逐条手填。
        if (this.graphStyle === "map") {
            const poi = this.nextUnlinkedPoi();
            if (poi) {
                marker.poi_id = poi.id;
                const code = String(poi.poi?.code || "").trim();
                marker.label = code ? `${code}. ${poi.title || poi.id}` : (poi.title || poi.id);
            }
        }
        this.placing = false;
        scenarioMapEl("scenarioMapViewport")?.classList.remove("is-placing");
        this.renderCalibration();
        this.renderMarkers();
        this.renderBatchState();
        this.renderStatus("已放置标记。保存剧本后，房间会沿用这些标记。");
    }

    private cancelDrag(): void {
        if (!this.drag) return;
        this.drag.draft?.remove();
        this.drag = null;
    }

    private imagePoint(x: number, y: number): { x: number; y: number } {
        return {
            x: (x - this.panX) / this.scale,
            y: (y - this.panY) / this.scale,
        };
    }

    // ------------------------------------------------------------------
    // 地图图层只读侧载与标定回写
    // ------------------------------------------------------------------

    private scenarioIdValue(): number {
        const value = Number.parseInt(document.getElementById("scenarioModal")?.dataset.scenarioId || "", 10);
        return Number.isFinite(value) && value > 0 ? value : 0;
    }

    private graphNodes(): ScenarioMapGraphNode[] {
        return Array.isArray(this.graph?.nodes) ? this.graph!.nodes : [];
    }

    private isMapNode(node: ScenarioMapGraphNode): boolean {
        return node.node_role === "map" || Boolean(node.map);
    }

    private poiNodes(areaId: string): ScenarioMapGraphNode[] {
        const key = String(areaId || "");
        if (!key) return [];
        return this.graphNodes().filter(
            (node) => node.node_role === "poi" && String(node.poi?.area_id || node.parent || "") === key,
        );
    }

    private poiByMarker(markerId: string): ScenarioMapGraphNode | null {
        if (!markerId) return null;
        return this.graphNodes().find(
            (node) => node.node_role === "poi"
                && String(node.poi?.geometry && (node.poi.geometry as Record<string, unknown>).marker_id || "") === markerId,
        ) || null;
    }

    /** 当前场景 id 解析成世界树里的地图 id（兼容 scene_id 与地图 id 不一致的情况）。 */
    private currentMapId(): string {
        if (!this.sceneId) return "";
        for (const node of this.graphNodes()) {
            if (!this.isMapNode(node)) continue;
            if (node.id === this.sceneId) return node.id;
            if (String(node.map?.scene_id || "") === this.sceneId) return node.id;
        }
        return this.sceneId;
    }

    /** 按兴趣点列表顺序找下一个还没有被任何标记关联的 POI。 */
    private nextUnlinkedPoi(): ScenarioMapGraphNode | null {
        const config = this.current();
        const mapId = this.currentMapId();
        if (!config || !mapId) return null;
        const used = new Set(config.markers.map((marker) => marker.poi_id).filter((id): id is string => Boolean(id)));
        return this.poiNodes(mapId).find((node) => !used.has(node.id)) || null;
    }

    private async loadGraph(force = false): Promise<void> {
        const scenarioId = this.scenarioIdValue();
        if (!scenarioId) {
            this.clearGraph();
            return;
        }
        if (!force && this.graph && this.graphScenarioId === scenarioId) return;
        if (this.graphLoading) return;
        this.graphLoading = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{
                bundle: ScenarioMapGraphBundle;
                revision: number;
                graph_style?: string;
                permissions?: { readonly?: boolean; can_edit?: boolean };
            }>>(`/api/scenarios/${scenarioId}/narrative`);
            if (response.status === 404) {
                this.clearGraph();
                return;
            }
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            this.graph = data.data.bundle;
            this.graphScenarioId = scenarioId;
            this.graphRevision = Number(data.data.revision) || 0;
            if (data.data.graph_style === "map" || data.data.graph_style === "mesh") {
                this.graphStyle = data.data.graph_style;
            }
            this.graphReadonly = Boolean(data.data.permissions?.readonly) || data.data.permissions?.can_edit === false;
        } catch {
            this.clearGraph();
        } finally {
            this.graphLoading = false;
            this.renderBatchState();
        }
    }

    private clearGraph(): void {
        this.graph = null;
        this.graphRevision = 0;
        this.graphReadonly = false;
        this.batchSnapshot = null;
        this.batchRevision = 0;
        this.renderBatchState();
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
    }

    private cloneBundle(bundle: ScenarioMapGraphBundle): ScenarioMapGraphBundle {
        return JSON.parse(JSON.stringify(bundle)) as ScenarioMapGraphBundle;
    }

    private normalizeMatch(value: unknown): string {
        return String(value ?? "")
            .toLowerCase()
            .replace(/[\s_\-—·.,，。:：;；'"“”‘’()（）\[\]【】]/g, "");
    }

    private stripCode(value: unknown): string {
        return String(value ?? "").replace(/^\s*\d+\s*[.、:：)\]]?\s*/, "");
    }

    /** 标记的底图像素框 → 兴趣点的格几何。 */
    private markerToRect(marker: ScenarioMapMarker, config: ScenarioSceneMapConfig): Record<string, number> {
        const cell = config.cell_px > 0 ? config.cell_px : 1;
        return {
            x: scenarioMapRound((marker.x - config.offset_x) / cell, 2),
            y: scenarioMapRound((marker.y - config.offset_y) / cell, 2),
            w: Math.max(1, scenarioMapRound(marker.w / cell, 2)),
            h: Math.max(1, scenarioMapRound(marker.h / cell, 2)),
        };
    }

    private async saveBundle(bundle: ScenarioMapGraphBundle, baseRevision: number): Promise<boolean> {
        const scenarioId = this.scenarioIdValue();
        if (!scenarioId) return false;
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{
            bundle: ScenarioMapGraphBundle;
            revision: number;
        }>>(
            `/api/scenarios/${scenarioId}/narrative`,
            { method: "PUT", body: { base_revision: baseRevision, bundle } },
        );
        if (response.status === 409) {
            await this.loadGraph(true);
            this.renderStatus("世界树已被其他编辑保存，已刷新；本次没有写入，请重新回写。", true);
            return false;
        }
        if (!response.ok || !data?.success || !data.data) {
            this.renderStatus(data?.message || `回写失败：HTTP ${response.status}`, true);
            return false;
        }
        this.graph = data.data.bundle;
        this.graphRevision = Number(data.data.revision) || this.graphRevision + 1;
        this.renderMarkers();
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
        this.renderBatchState();
        this.syncNarrativeEditor();
        return true;
    }

    private renderBatchState(): void {
        const pane = document.getElementById("scenarioTabMap");
        if (pane) pane.dataset.graphStyle = this.graphStyle;
        const summary = scenarioMapEl("scenarioMapBatchSummary");
        const undo = scenarioMapEl<HTMLButtonElement>("scenarioMapBatchUndo");
        const write = scenarioMapEl<HTMLButtonElement>("scenarioMapBatchWriteback");
        if (undo) undo.disabled = !this.batchSnapshot || this.graphRevision !== this.batchRevision;
        if (write) write.disabled = this.readOnly || this.graphReadonly || this.graphStyle !== "map";
        if (!summary) return;
        if (this.graphStyle !== "map") {
            summary.textContent = "";
            return;
        }
        const mapId = this.currentMapId();
        if (!mapId) {
            summary.textContent = "";
            return;
        }
        const pois = this.poiNodes(mapId);
        const provisional = pois.filter((node) => {
            const geometry = (node.poi?.geometry || {}) as Record<string, unknown>;
            return geometry.provisional === true;
        }).length;
        const config = this.current();
        const poiIds = new Set(pois.map((node) => node.id));
        const linked = config
            ? config.markers.filter((marker) => {
                const target = marker.poi_id || this.poiByMarker(marker.id)?.id || "";
                return Boolean(target) && poiIds.has(target);
            }).length
            : 0;
        const invalid = config
            ? config.markers.filter((marker) => Boolean(marker.poi_id) && !poiIds.has(String(marker.poi_id))).length
            : 0;
        summary.textContent = `${pois.length} 个兴趣点 · ${provisional} 个 provisional · ${linked} 个标记已关联${invalid ? ` · ${invalid} 条关联失效` : ""}`;
    }

    /**
     * 把标记关联写回世界树：只改 `poi.geometry.marker_id`，不动标记坐标与 POI 的格几何。
     * 清除关联时同步删掉旧 POI 上的 marker_id。
     */
    private async syncMarkerAssociation(markerId: string, oldPoiId: string, newPoiId: string): Promise<void> {
        if (this.readOnly || !markerId) return;
        const config = this.current();
        const marker = config?.markers.find((item) => item.id === markerId);
        const revert = (): void => {
            if (!marker) return;
            if (oldPoiId) marker.poi_id = oldPoiId;
            else delete marker.poi_id;
            this.renderMarkers();
        };
        if (this.graphReadonly) {
            revert();
            this.renderStatus("世界树只读，标记关联未写入。", true);
            return;
        }
        await this.loadGraph(true);
        if (!this.graph) {
            revert();
            this.renderStatus("世界树尚未加载，标记关联未写入。", true);
            return;
        }
        const mapPoiIds = new Set(this.poiNodes(this.currentMapId()).map((node) => node.id));
        if (newPoiId && !mapPoiIds.has(newPoiId)) {
            revert();
            this.renderStatus(`兴趣点 ${newPoiId} 不存在或不属于当前地图，关联未写入。`, true);
            return;
        }

        const bundle = this.cloneBundle(this.graph);
        const byId = new Map(bundle.nodes.map((node) => [node.id, node]));
        if (oldPoiId && oldPoiId !== newPoiId) {
            const oldNode = byId.get(oldPoiId);
            if (oldNode) {
                const poi = { ...(oldNode.poi || {}) } as Record<string, unknown>;
                const geometry = { ...((poi.geometry || {}) as Record<string, unknown>) };
                if (String(geometry.marker_id || "") === markerId) {
                    delete geometry.marker_id;
                    poi.geometry = geometry;
                    oldNode.poi = poi;
                }
            }
        }
        if (newPoiId) {
            const newNode = byId.get(newPoiId);
            if (!newNode) {
                revert();
                this.renderStatus(`兴趣点 ${newPoiId} 不存在，关联未写入。`, true);
                return;
            }
            const poi = { ...(newNode.poi || {}) } as Record<string, unknown>;
            const geometry = { ...((poi.geometry || {}) as Record<string, unknown>) };
            geometry.marker_id = markerId;
            poi.geometry = geometry;
            newNode.poi = poi;
        }

        const saved = await this.saveBundle(bundle, this.graphRevision);
        if (!saved) {
            revert();
            this.renderStatus("标记关联写入失败，已恢复原关联。", true);
            return;
        }
        this.renderStatus(newPoiId ? "标记关联已更新。" : "已清除标记关联。");
    }

    /**
     * 把当前地图的 DM 标记与兴趣点关联，一次性回写格几何。
     *
     * 匹配顺序：标记的 poi_id → 兴趣点已有的 marker_id → 编号/标题精确匹配。
     * 没有任何匹配的 POI 保持原样，仍为 provisional。
     */
    private async batchWriteback(): Promise<void> {
        if (this.readOnly || this.graphReadonly || this.graphStyle !== "map") return;
        const config = this.current();
        const mapId = this.currentMapId();
        if (!config || !mapId) {
            this.renderStatus("请先选择一张地图。", true);
            return;
        }
        if (!(config.cell_px > 0)) {
            this.renderStatus("请先在底图上对齐一格并点「记录标定」，再批量回写。", true);
            return;
        }
        if (!config.markers.length) {
            this.renderStatus("这张图还没有 DM 标记：先放置标记并关联兴趣点，再批量回写。", true);
            return;
        }
        await this.loadGraph(true);
        if (!this.graph) {
            this.renderStatus("世界树尚未加载，无法回写。", true);
            return;
        }
        const mapPois = this.poiNodes(mapId);
        const mapPoiIds = new Set(mapPois.map((node) => node.id));
        const invalidAssociations = config.markers.filter(
            (marker) => Boolean(marker.poi_id) && !mapPoiIds.has(String(marker.poi_id)),
        );
        if (invalidAssociations.length) {
            const detail = invalidAssociations.slice(0, 5)
                .map((marker) => `「${marker.label || marker.id}」→ ${marker.poi_id}`)
                .join("、");
            this.renderStatus(
                `有 ${invalidAssociations.length} 条标记关联了不存在或不属于该地图的兴趣点：${detail}${invalidAssociations.length > 5 ? " 等" : ""}。请先修正关联再回写。`,
                true,
            );
            return;
        }
        const confirmed = await window.TrpgDialog?.confirm({
            title: "批量回写兴趣点",
            message: "将使用当前页面的标定与 DM 标记，把匹配到的兴趣点写入正式格几何；没有匹配的仍保持 provisional。\n如果刚修改过标定，请先保存剧本，否则回写的坐标会与底图不一致。",
            confirmText: "开始回写",
        });
        if (!confirmed) return;

        const usedMarkers = new Set<string>();
        const pairs: Array<{ poi: ScenarioMapGraphNode; marker: ScenarioMapMarker }> = [];
        for (const poi of mapPois) {
            const geometry = (poi.poi?.geometry || {}) as Record<string, unknown>;
            const existingMarkerId = String(geometry.marker_id || "");
            // 优先级：标记的 poi_id → 兴趣点已有的 marker_id → 名称/编号精确匹配。
            let marker = config.markers.find((item) => item.poi_id === poi.id && !usedMarkers.has(item.id))
                || config.markers.find((item) => item.id === existingMarkerId && !usedMarkers.has(item.id));
            if (!marker) {
                const code = String(poi.poi?.code || "").trim();
                const title = String(poi.title || poi.id);
                const aliases = new Set<string>();
                [code, title, this.stripCode(title)].forEach((value) => {
                    const normalized = this.normalizeMatch(value);
                    if (normalized) aliases.add(normalized);
                });
                marker = config.markers.find((item) => !usedMarkers.has(item.id) && aliases.has(this.normalizeMatch(item.label)));
            }
            if (!marker) continue;
            usedMarkers.add(marker.id);
            pairs.push({ poi, marker });
        }
        if (!pairs.length) {
            this.renderStatus("没有可回写的兴趣点：请把标记名称写成编号或标题，或在标记行选择「关联兴趣点」。", true);
            return;
        }

        const promote = scenarioMapEl<HTMLInputElement>("scenarioMapPromoteTrigger")?.checked === true;
        const before = this.cloneBundle(this.graph);
        const bundle = this.cloneBundle(this.graph);
        const byId = new Map(bundle.nodes.map((node) => [node.id, node]));
        for (const pair of pairs) {
            const node = byId.get(pair.poi.id);
            if (!node) continue;
            const poi = { ...(node.poi || {}) } as Record<string, unknown>;
            const geometry = { ...((poi.geometry || {}) as Record<string, unknown>) };
            geometry.rect_cells = this.markerToRect(pair.marker, config);
            geometry.marker_id = pair.marker.id;
            geometry.provisional = false;
            delete geometry.note;
            poi.geometry = geometry;
            const trigger = { ...((poi.trigger || {}) as Record<string, unknown>) };
            if (promote && String(trigger.mode || "manual") === "manual") trigger.mode = "enter";
            poi.trigger = trigger;
            node.poi = poi;
        }

        this.renderStatus("正在回写兴趣点…");
        const saved = await this.saveBundle(bundle, this.graphRevision);
        if (!saved) return;
        this.batchSnapshot = { bundle: before, revision: this.graphRevision };
        this.batchRevision = this.graphRevision;
        const total = this.poiNodes(mapId).length;
        const remaining = this.poiNodes(mapId).filter((node) => {
            const geometry = (node.poi?.geometry || {}) as Record<string, unknown>;
            return geometry.provisional === true;
        }).length;
        const skipped = total - pairs.length;
        this.renderBatchState();
        this.renderStatus(`已回写 ${pairs.length} 个兴趣点，跳过 ${skipped} 个（无匹配标记），仍有 ${remaining} 个 provisional。`);
        showNotification(`已回写 ${pairs.length} 个兴趣点。`, "success");
    }

    /** 撤销最近一次批量回写；世界树 revision 已变化时拒绝，避免覆盖别人的编辑。 */
    private async undoBatchWriteback(): Promise<void> {
        if (this.readOnly || this.graphReadonly || !this.batchSnapshot) return;
        if (this.graphRevision !== this.batchRevision) {
            this.renderStatus("世界树已发生变化，不能撤销这批回写，请手动调整。", true);
            return;
        }
        const snapshot = this.batchSnapshot;
        this.renderStatus("正在撤销回写…");
        const saved = await this.saveBundle(this.cloneBundle(snapshot.bundle), snapshot.revision);
        if (!saved) return;
        this.batchSnapshot = null;
        this.batchRevision = 0;
        this.renderBatchState();
        this.renderStatus("已撤销最近一次回写。");
        showNotification("已撤销最近一次回写。", "success");
    }

    /** 保存后刷新世界树；若世界树有未保存改动则不覆盖。 */
    private syncNarrativeEditor(): void {
        const narrative = (window as unknown as {
            TrpgNarrative?: {
                runtime?: { dirty?: boolean };
                state?: { mapConfig?: ScenarioMapConfig };
            };
        }).TrpgNarrative;
        const editor = (window as unknown as { ScenarioNarrative?: { load?: (id: number) => Promise<void> } }).ScenarioNarrative;
        const snapshot = JSON.parse(JSON.stringify(this.config)) as ScenarioMapConfig;
        // 地图页是 map_config 的编辑源：已保存或未保存的标记关联都以这里为准。
        if (narrative?.state) narrative.state.mapConfig = snapshot;
        if (narrative?.runtime?.dirty || !editor?.load) return;
        const scenarioId = this.scenarioIdValue();
        if (!scenarioId) return;
        void editor.load(scenarioId).then(() => {
            if (narrative?.state) narrative.state.mapConfig = snapshot;
        });
    }

    private renderPoiOverlay(layer: HTMLElement | null): void {
        if (this.graphStyle !== "map") return;
        if (!layer || !this.graph) return;
        layer.querySelectorAll("[data-map-poi-id]").forEach((item) => item.remove());
        const areaId = this.currentMapId();
        const config = this.current();
        if (!areaId || !config || !(config.cell_px > 0)) return;
        this.poiNodes(areaId).forEach((node) => {
            const geometry = (node.poi?.geometry || {}) as Record<string, unknown>;
            const rect = (geometry.rect_cells || {}) as Record<string, unknown>;
            const width = Number(rect.w || 0);
            const height = Number(rect.h || 0);
            if (!(width > 0 && height > 0)) return;
            const element = document.createElement("div");
            element.className = "scenario-map-poi";
            element.dataset.mapPoiId = node.id;
            element.style.left = `${this.panX + (config.offset_x + Number(rect.x || 0) * config.cell_px) * this.scale}px`;
            element.style.top = `${this.panY + (config.offset_y + Number(rect.y || 0) * config.cell_px) * this.scale}px`;
            element.style.width = `${Math.max(2, width * config.cell_px * this.scale)}px`;
            element.style.height = `${Math.max(2, height * config.cell_px * this.scale)}px`;
            const code = String(node.poi?.code || "").trim();
            const chip = document.createElement("span");
            chip.className = "scenario-map-poi-label";
            chip.textContent = code ? `${code}. ${node.title || node.id}` : (node.title || node.id);
            element.appendChild(chip);
            layer.appendChild(element);
        });
    }

    private async importBackground(file: File): Promise<void> {
        if (this.readOnly) return;
        const scenarioId = Number.parseInt(document.getElementById("scenarioModal")?.dataset.scenarioId || "", 10);
        if (!Number.isFinite(scenarioId) || scenarioId <= 0) {
            this.renderStatus("请先保存剧本，再导入地图底图。", true);
            return;
        }
        const config = this.ensureCurrent();
        this.renderStatus("正在上传底图…");
        try {
            const form = new FormData();
            form.append("files", file, file.name);
            form.append("alt", `地图底图：${file.name}`);
            const response = await TrpgApi.requestWithResponse<ApiResponse<ResourceRef[]>>(
                `/api/scripts/${scenarioId}/assets`,
                { method: "POST", body: form },
            );
            if (!response.response.ok || !response.data.success || !Array.isArray(response.data.data)) {
                throw new Error(response.data.message || "底图上传失败");
            }
            const resource = response.data.data[0];
            if (!resource || !resource.url) throw new Error("底图上传失败");
            config.background_url = resource.url;
            config.background_asset_id = resource.asset_id || "";
            config.width = Number(resource.width) || 0;
            config.height = Number(resource.height) || 0;
            config.cell_px = 0;
            config.offset_x = 0;
            config.offset_y = 0;
            this.imageUrl = "";
            this.naturalWidth = config.width;
            this.naturalHeight = config.height;
            this.applyInitialView();
            showNotification("底图已导入。请在中间方框里对齐一个格子后点「记录标定」。", "success");
        } catch (error) {
            const message = error instanceof Error ? error.message : "底图上传失败";
            this.renderStatus(message, true);
            showNotification(message, "error");
        }
    }
}

function scenarioMapEscape(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function scenarioMapHexToRgba(value: string, alpha: number): string {
    const text = String(value || "").replace("#", "");
    if (!/^[0-9a-fA-F]{6}$/.test(text)) return `rgba(246, 178, 107, ${alpha})`;
    const red = Number.parseInt(text.slice(0, 2), 16);
    const green = Number.parseInt(text.slice(2, 4), 16);
    const blue = Number.parseInt(text.slice(4, 6), 16);
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

