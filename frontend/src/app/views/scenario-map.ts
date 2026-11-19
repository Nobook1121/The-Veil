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
    private graph: ScenarioMapGraphBundle | null = null;
    private graphScenarioId = 0;
    private graphRevision = 0;
    private graphReadonly = false;
    private graphLoading = false;
    private selectedMapId = "";
    private selectedPoiId = "";

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
        this.graph = null;
        this.graphScenarioId = 0;
        this.graphRevision = 0;
        this.graphReadonly = false;
        this.graphLoading = false;
        this.selectedMapId = "";
        this.selectedPoiId = "";
        this.refreshScenes();
    }

    load(mapConfig: ScenarioMapConfig | undefined): void {
        this.config = normalizeScenarioMapConfig(mapConfig);
        this.sceneId = "";
        this.imageUrl = "";
        this.imageReady = false;
        this.selectedMarker = "";
        this.pendingFit = false;
        this.refreshScenes();
    }

    collect(): ScenarioMapConfig {
        const existingScenes = new Set(this.sceneOptions().map((scene) => scene.id));
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
        void this.loadGraph();
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
        scenarioMapEl<HTMLSelectElement>("scenarioMapGraphMapSelect")?.addEventListener("change", (event) => {
            this.selectedMapId = (event.target as HTMLSelectElement).value;
            this.selectedPoiId = this.poiNodes(this.selectedMapId)[0]?.id || "";
            const map = this.selectedMapNode();
            const sceneId = String(map?.map?.scene_id || map?.id || "");
            if (sceneId && this.config[sceneId]) {
                this.sceneId = sceneId;
                const sceneSelect = scenarioMapEl<HTMLSelectElement>("scenarioMapSceneSelect");
                if (sceneSelect) sceneSelect.value = sceneId;
                this.selectedMarker = "";
                this.applyInitialView();
            }
            this.renderGraph();
        });
        scenarioMapEl<HTMLSelectElement>("scenarioMapGraphPoiSelect")?.addEventListener("change", (event) => {
            this.selectedPoiId = (event.target as HTMLSelectElement).value;
            this.renderGraph();
        });
        scenarioMapEl("scenarioMapGraphSection")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            if (target.closest("[data-graph-save-map]")) {
                void this.saveGraphMap();
                return;
            }
            if (target.closest("[data-graph-save-poi]")) {
                void this.saveGraphPoi();
                return;
            }
            const edgeButton = target.closest<HTMLElement>("[data-graph-save-edge]");
            if (edgeButton?.dataset.graphSaveEdge) {
                void this.saveGraphEdge(edgeButton.dataset.graphSaveEdge);
            }
        });

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
            const poi = target.closest<HTMLElement>("[data-map-poi-id]");
            if (poi) {
                event.stopPropagation();
                this.selectedPoiId = poi.dataset.mapPoiId || "";
                this.renderGraph();
                return;
            }
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
            } else {
                return;
            }
            // 只重画底图上的框，避免输入时重建列表导致光标丢失。
            this.renderMarkerLayer();
        };
        markerList?.addEventListener("input", updateMarker);
        markerList?.addEventListener("change", updateMarker);
        markerList?.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLElement>("[data-map-marker-remove]");
            if (!button || this.readOnly) return;
            const markerId = button.closest<HTMLElement>("[data-map-marker-row]")?.dataset.mapMarkerRow || "";
            const config = this.current();
            if (!config) return;
            config.markers = config.markers.filter((marker) => marker.id !== markerId);
            if (this.selectedMarker === markerId) this.selectedMarker = "";
            this.renderMarkers();
        });
        markerList?.addEventListener("click", (event) => {
            const row = (event.target as HTMLElement).closest<HTMLElement>("[data-map-marker-row]");
            if (!row) return;
            this.selectedMarker = row.dataset.mapMarkerRow || "";
            this.renderMarkers();
        });
    }

    private sceneOptions(): ScenarioMapSceneOption[] {
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

    private refreshScenes(keepScene = false): void {
        const select = scenarioMapEl<HTMLSelectElement>("scenarioMapSceneSelect");
        if (!select) return;
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
            return `
                <div class="scenario-map-marker-row${marker.id === this.selectedMarker ? " is-selected" : ""}" data-map-marker-row="${scenarioMapEscape(marker.id)}">
                    <span class="scenario-map-swatch" style="background:${scenarioMapEscape(type?.color || "#f6b26b")}"></span>
                    <span class="scenario-map-marker-index">#${index + 1}</span>
                    <input class="form-control form-control-sm" data-map-marker-label value="${scenarioMapEscape(marker.label)}" placeholder="标记名称">
                    <select class="form-select form-select-sm" data-map-marker-visibility aria-label="标记可见性">
                        <option value="public"${marker.visibility === "public" ? " selected" : ""}>玩家可见</option>
                        <option value="kp"${marker.visibility === "kp" ? " selected" : ""}>仅 DM</option>
                    </select>
                    ${linkedLabel}
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
    }

    private clearCalibration(): void {
        const config = this.current();
        if (!config || this.readOnly) return;
        config.cell_px = 0;
        config.offset_x = 0;
        config.offset_y = 0;
        this.renderCalibration();
        this.renderStatus();
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
        config.markers = config.markers.filter((marker) => marker.id !== this.selectedMarker);
        this.selectedMarker = "";
        this.renderMarkers();
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
        config.markers.push({
            id: `marker-${Date.now().toString(36)}`,
            type: type?.id || "",
            label: "",
            x: scenarioMapRound(x),
            y: scenarioMapRound(y),
            w: scenarioMapRound(width),
            h: scenarioMapRound(height),
            visibility: type?.visibility || "kp",
        });
        this.placing = false;
        scenarioMapEl("scenarioMapViewport")?.classList.remove("is-placing");
        this.renderCalibration();
        this.renderMarkers();
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
    // 地图图谱（叙事层）：node_role / map / poi / transition
    // ------------------------------------------------------------------

    private scenarioIdValue(): number {
        const value = Number.parseInt(document.getElementById("scenarioModal")?.dataset.scenarioId || "", 10);
        return Number.isFinite(value) && value > 0 ? value : 0;
    }

    private graphNodes(): ScenarioMapGraphNode[] {
        return Array.isArray(this.graph?.nodes) ? this.graph!.nodes : [];
    }

    private graphEdges(): ScenarioMapGraphEdge[] {
        return Array.isArray(this.graph?.edges) ? this.graph!.edges : [];
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

    private selectedMapNode(): ScenarioMapGraphNode | null {
        return this.graphNodes().find((node) => node.id === this.selectedMapId && this.isMapNode(node)) || null;
    }

    private selectedPoiNode(): ScenarioMapGraphNode | null {
        return this.poiNodes(this.selectedMapId).find((node) => node.id === this.selectedPoiId) || null;
    }

    private areaOfNode(nodeId: string): string {
        const node = this.graphNodes().find((item) => item.id === nodeId);
        if (!node) return "";
        if (this.isMapNode(node)) return node.id;
        return String(node.poi?.area_id || node.parent || "");
    }

    private portalEdgesFor(areaId: string): ScenarioMapGraphEdge[] {
        if (!areaId) return [];
        return this.graphEdges().filter((edge) => {
            if (edge.type !== "portal" && edge.transition) return false;
            if (edge.type !== "portal") return false;
            return this.areaOfNode(edge.from) === areaId || this.areaOfNode(edge.to) === areaId
                || edge.from === areaId || edge.to === areaId;
        });
    }

    private poiByMarker(markerId: string): ScenarioMapGraphNode | null {
        if (!markerId) return null;
        return this.graphNodes().find(
            (node) => node.node_role === "poi"
                && String(node.poi?.geometry && (node.poi.geometry as Record<string, unknown>).marker_id || "") === markerId,
        ) || null;
    }

    private async loadGraph(force = false): Promise<void> {
        const scenarioId = this.scenarioIdValue();
        if (!scenarioId) {
            this.clearGraph("请先保存剧本，再编辑地图图谱。");
            return;
        }
        if (!force && this.graph && this.graphScenarioId === scenarioId) {
            this.renderGraph();
            return;
        }
        if (this.graphLoading) return;
        this.graphLoading = true;
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<{
                bundle: ScenarioMapGraphBundle;
                revision: number;
                permissions?: { readonly?: boolean; can_edit?: boolean };
            }>>(`/api/scenarios/${scenarioId}/narrative`);
            if (response.status === 404) {
                this.clearGraph("该剧本还没有世界树；可先在「世界树」页签自动建图。");
                return;
            }
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            this.graph = data.data.bundle;
            this.graphScenarioId = scenarioId;
            this.graphRevision = Number(data.data.revision) || 0;
            this.graphReadonly = Boolean(data.data.permissions?.readonly) || data.data.permissions?.can_edit === false;
            const maps = this.graphNodes().filter((node) => this.isMapNode(node));
            if (!maps.some((node) => node.id === this.selectedMapId)) {
                this.selectedMapId = maps[0]?.id || "";
            }
            if (!this.poiNodes(this.selectedMapId).some((node) => node.id === this.selectedPoiId)) {
                this.selectedPoiId = this.poiNodes(this.selectedMapId)[0]?.id || "";
            }
            this.renderGraph();
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            this.clearGraph(`地图图谱加载失败：${message}`);
        } finally {
            this.graphLoading = false;
        }
    }

    private clearGraph(message: string): void {
        this.graph = null;
        this.graphRevision = 0;
        this.graphReadonly = false;
        this.selectedMapId = "";
        this.selectedPoiId = "";
        const select = scenarioMapEl<HTMLSelectElement>("scenarioMapGraphMapSelect");
        if (select) select.innerHTML = "";
        const poiSelect = scenarioMapEl<HTMLSelectElement>("scenarioMapGraphPoiSelect");
        if (poiSelect) poiSelect.innerHTML = "";
        const mapForm = scenarioMapEl("scenarioMapGraphMapForm");
        if (mapForm) mapForm.innerHTML = `<p class="scenario-map-empty-hint">${scenarioMapEscape(message)}</p>`;
        const poiForm = scenarioMapEl("scenarioMapGraphPoiForm");
        if (poiForm) poiForm.innerHTML = "";
        const transitions = scenarioMapEl("scenarioMapGraphTransitions");
        if (transitions) transitions.innerHTML = "";
        const validation = scenarioMapEl("scenarioMapGraphValidation");
        if (validation) validation.innerHTML = "";
    }

    private renderGraph(): void {
        const mapSelect = scenarioMapEl<HTMLSelectElement>("scenarioMapGraphMapSelect");
        const poiSelect = scenarioMapEl<HTMLSelectElement>("scenarioMapGraphPoiSelect");
        const mapForm = scenarioMapEl("scenarioMapGraphMapForm");
        const poiForm = scenarioMapEl("scenarioMapGraphPoiForm");
        const transitionHost = scenarioMapEl("scenarioMapGraphTransitions");
        const validationHost = scenarioMapEl("scenarioMapGraphValidation");
        if (!mapSelect || !poiSelect || !mapForm || !poiForm) return;

        if (!this.graph) {
            mapSelect.innerHTML = "";
            mapSelect.disabled = true;
            poiSelect.innerHTML = "";
            poiSelect.disabled = true;
            return;
        }
        const maps = this.graphNodes().filter((node) => this.isMapNode(node));
        mapSelect.disabled = this.graphReadonly || maps.length === 0;
        mapSelect.innerHTML = maps.map((node) => (
            `<option value="${scenarioMapEscape(node.id)}"${node.id === this.selectedMapId ? " selected" : ""}>`
            + `${scenarioMapEscape(node.title || node.id)}</option>`
        )).join("");

        const map = this.selectedMapNode();
        if (!map) {
            mapForm.innerHTML = '<p class="scenario-map-empty-hint">还没有标注为地图的大节点。</p>';
        } else {
            mapForm.innerHTML = this.renderMapForm(map);
        }

        const pois = this.poiNodes(this.selectedMapId);
        poiSelect.disabled = this.graphReadonly || pois.length === 0;
        poiSelect.innerHTML = pois.map((node) => {
            const code = String(node.poi?.code || "").trim();
            const label = code ? `${code}. ${node.title || node.id}` : (node.title || node.id);
            return `<option value="${scenarioMapEscape(node.id)}"${node.id === this.selectedPoiId ? " selected" : ""}>${scenarioMapEscape(label)}</option>`;
        }).join("");
        const poi = this.selectedPoiNode();
        poiForm.innerHTML = poi
            ? this.renderPoiForm(poi)
            : `<p class="scenario-map-empty-hint">${this.selectedMapId ? "该地图还没有兴趣点。" : "请先选择一张地图。"}</p>`;

        if (transitionHost) {
            transitionHost.innerHTML = map ? this.renderTransitions(map) : "";
        }
        if (validationHost) {
            validationHost.innerHTML = this.renderValidation();
        }
        this.renderPoiOverlay(scenarioMapEl("scenarioMapMarkerLayer"));
    }

    private renderMapForm(node: ScenarioMapGraphNode): string {
        const map = (node.map || {}) as Record<string, unknown>;
        const environment = (map.environment && typeof map.environment === "object")
            ? map.environment as Record<string, unknown>
            : {};
        const terrain = Array.isArray(environment.terrain) ? environment.terrain.join("、") : String(environment.terrain || "");
        const entry = Array.isArray(map.entry_pois) ? map.entry_pois.join(", ") : String(map.entry_pois || "");
        const readonly = this.graphReadonly ? " disabled" : "";
        return `
            <div class="scenario-map-graph-grid">
                <label>标题<input class="form-control form-control-sm" data-graph-map-title value="${scenarioMapEscape(node.title || "")}"${readonly}></label>
                <label>scene_id<input class="form-control form-control-sm" data-graph-map-scene value="${scenarioMapEscape(map.scene_id || node.id)}"${readonly}></label>
                <label>格边长(ft)<input class="form-control form-control-sm" type="number" min="1" data-graph-map-grid value="${scenarioMapEscape(map.grid_ft ?? 5)}"${readonly}></label>
                <label>边界(格)<input class="form-control form-control-sm" type="number" min="0" data-graph-map-bounds value="${scenarioMapEscape(map.bounds_cells ?? 0)}"${readonly}></label>
                <label>父地图<input class="form-control form-control-sm" data-graph-map-parent value="${scenarioMapEscape(map.parent_area || "")}" placeholder="嵌套地图填写父 area_id"${readonly}></label>
                <label>入口 POI<input class="form-control form-control-sm" data-graph-map-entry value="${scenarioMapEscape(entry)}" placeholder="逗号分隔"${readonly}></label>
            </div>
            <label>玩家可见简报<textarea class="form-control form-control-sm" rows="2" data-graph-map-brief${readonly}>${scenarioMapEscape(map.brief || "")}</textarea></label>
            <label>DM 备注<textarea class="form-control form-control-sm" rows="2" data-graph-map-dm-brief${readonly}>${scenarioMapEscape(map.dm_brief || "")}</textarea></label>
            <div class="scenario-map-graph-grid">
                <label>光照<input class="form-control form-control-sm" data-graph-map-lighting value="${scenarioMapEscape(environment.lighting || "")}"${readonly}></label>
                <label>温度<input class="form-control form-control-sm" data-graph-map-temperature value="${scenarioMapEscape(environment.temperature || "")}"${readonly}></label>
                <label>气味<input class="form-control form-control-sm" data-graph-map-smell value="${scenarioMapEscape(environment.smell || "")}"${readonly}></label>
                <label>声音<input class="form-control form-control-sm" data-graph-map-sound value="${scenarioMapEscape(environment.sound || "")}"${readonly}></label>
            </div>
            <label>地形<input class="form-control form-control-sm" data-graph-map-terrain value="${scenarioMapEscape(terrain)}" placeholder="逗号分隔"${readonly}></label>
            <button type="button" class="btn btn-sm btn-primary" data-graph-save-map${readonly}>保存地图节点</button>
        `;
    }

    private renderPoiForm(node: ScenarioMapGraphNode): string {
        const poi = (node.poi || {}) as Record<string, unknown>;
        const trigger = (poi.trigger && typeof poi.trigger === "object") ? poi.trigger as Record<string, unknown> : {};
        const geometry = (poi.geometry && typeof poi.geometry === "object") ? poi.geometry as Record<string, unknown> : {};
        const rect = (geometry.rect_cells && typeof geometry.rect_cells === "object")
            ? geometry.rect_cells as Record<string, unknown>
            : {};
        const sceneConfig = this.config[this.selectedMapId];
        const markerId = String(geometry.marker_id || "");
        const markers = sceneConfig?.markers || [];
        const markerOptions = ['<option value="">不关联</option>']
            .concat(markers.map((marker) => (
                `<option value="${scenarioMapEscape(marker.id)}"${marker.id === markerId ? " selected" : ""}>`
                + `${scenarioMapEscape(marker.label || marker.id)}</option>`
            ))).join("");
        const readonly = this.graphReadonly ? " disabled" : "";
        const mode = String(trigger.mode || "manual");
        const modeOptions = ["enter", "adjacent", "inspect", "manual"].map((value) => (
            `<option value="${value}"${value === mode ? " selected" : ""}>${value}</option>`
        )).join("");
        return `
            <div class="scenario-map-graph-grid">
                <label>标题<input class="form-control form-control-sm" data-graph-poi-title value="${scenarioMapEscape(node.title || "")}"${readonly}></label>
                <label>编号<input class="form-control form-control-sm" data-graph-poi-code value="${scenarioMapEscape(poi.code || "")}" placeholder="DM 地图编号"${readonly}></label>
                <label>area_id<input class="form-control form-control-sm" data-graph-poi-area value="${scenarioMapEscape(poi.area_id || node.parent || "")}"${readonly}></label>
                <label>触发<select class="form-select form-select-sm" data-graph-poi-mode${readonly}>${modeOptions}</select></label>
                <label>半径(格)<input class="form-control form-control-sm" type="number" min="0" data-graph-poi-radius value="${scenarioMapEscape(trigger.radius_cells ?? 0)}"${readonly}></label>
                <label class="scenario-map-graph-check"><input type="checkbox" data-graph-poi-once${trigger.once ? " checked" : ""}${readonly}> 只触发一次</label>
            </div>
            <div class="scenario-map-graph-grid">
                <label>x<input class="form-control form-control-sm" type="number" data-graph-poi-x value="${scenarioMapEscape(rect.x ?? 0)}"${readonly}></label>
                <label>y<input class="form-control form-control-sm" type="number" data-graph-poi-y value="${scenarioMapEscape(rect.y ?? 0)}"${readonly}></label>
                <label>w<input class="form-control form-control-sm" type="number" min="0" data-graph-poi-w value="${scenarioMapEscape(rect.w ?? 0)}"${readonly}></label>
                <label>h<input class="form-control form-control-sm" type="number" min="0" data-graph-poi-h value="${scenarioMapEscape(rect.h ?? 0)}"${readonly}></label>
            </div>
            <label class="scenario-map-graph-check"><input type="checkbox" data-graph-poi-provisional${geometry.provisional ? " checked" : ""}${readonly}> 几何推算（provisional）</label>
            <label>推算原因<input class="form-control form-control-sm" data-graph-poi-note value="${scenarioMapEscape(geometry.note || "")}" placeholder="例如：底图未标定"${readonly}></label>
            <label>关联 DM 标记<select class="form-select form-select-sm" data-graph-poi-marker${readonly}>${markerOptions}</select></label>
            <label>入口摘要<textarea class="form-control form-control-sm" rows="2" data-graph-poi-entry-brief${readonly}>${scenarioMapEscape(poi.entry_brief || "")}</textarea></label>
            <button type="button" class="btn btn-sm btn-primary" data-graph-save-poi${readonly}>保存兴趣点</button>
        `;
    }

    private renderTransitions(map: ScenarioMapGraphNode): string {
        const edges = this.portalEdgesFor(map.id);
        if (!edges.length) {
            return '<p class="scenario-map-empty-hint">该地图还没有 portal 过场。可在世界树里把跨图边改成「通道」。</p>';
        }
        const readonly = this.graphReadonly ? " disabled" : "";
        return `<div class="scenario-map-graph-edge-head">跨图过场</div>` + edges.map((edge) => {
            const transition = (edge.transition || {}) as Record<string, unknown>;
            const direction = String(transition.direction || (edge.bidirectional ? "two_way" : "one_way"));
            const options = ["one_way", "two_way"].map((value) => (
                `<option value="${value}"${value === direction ? " selected" : ""}>${value}</option>`
            )).join("");
            return `
                <div class="scenario-map-graph-edge" data-graph-edge-row="${scenarioMapEscape(edge.id)}">
                    <strong>${scenarioMapEscape(edge.from)} → ${scenarioMapEscape(edge.to)}</strong>
                    <label>标签<input class="form-control form-control-sm" data-graph-edge-label value="${scenarioMapEscape(transition.label || edge.label || "")}"${readonly}></label>
                    <label>过场文本<textarea class="form-control form-control-sm" rows="2" data-graph-edge-text${readonly}>${scenarioMapEscape(transition.text || "")}</textarea></label>
                    <div class="scenario-map-graph-grid">
                        <label>from_area<input class="form-control form-control-sm" data-graph-edge-from-area value="${scenarioMapEscape(transition.from_area || "")}"${readonly}></label>
                        <label>to_area<input class="form-control form-control-sm" data-graph-edge-to-area value="${scenarioMapEscape(transition.to_area || "")}"${readonly}></label>
                        <label>from_poi<input class="form-control form-control-sm" data-graph-edge-from-poi value="${scenarioMapEscape(transition.from_poi || "")}"${readonly}></label>
                        <label>entry_poi<input class="form-control form-control-sm" data-graph-edge-entry-poi value="${scenarioMapEscape(transition.entry_poi || "")}"${readonly}></label>
                        <label>方向<select class="form-select form-select-sm" data-graph-edge-direction${readonly}>${options}</select></label>
                        <label>耗时(分)<input class="form-control form-control-sm" type="number" min="0" data-graph-edge-time value="${scenarioMapEscape(transition.time_cost_minutes ?? 0)}"${readonly}></label>
                    </div>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-graph-save-edge="${scenarioMapEscape(edge.id)}"${readonly}>保存过场</button>
                </div>
            `;
        }).join("");
    }

    private graphIssues(): Array<{ level: "error" | "warn"; text: string; nodeId?: string; edgeId?: string }> {
        const issues: Array<{ level: "error" | "warn"; text: string; nodeId?: string; edgeId?: string }> = [];
        const maps = new Map<string, ScenarioMapGraphNode>();
        this.graphNodes().filter((node) => this.isMapNode(node)).forEach((node) => maps.set(node.id, node));
        const codes = new Map<string, Set<string>>();

        maps.forEach((map) => {
            const block = (map.map || {}) as Record<string, unknown>;
            const sceneId = String(block.scene_id || "").trim();
            if (!sceneId) {
                issues.push({ level: "error", text: `地图「${map.title || map.id}」缺少 scene_id。`, nodeId: map.id });
            } else if (sceneId !== map.id) {
                issues.push({ level: "warn", text: `地图「${map.id}」的 scene_id 与 area_id 不一致（${sceneId}）。`, nodeId: map.id });
            }
            if (!Number(block.bounds_cells || 0)) {
                issues.push({ level: "warn", text: `地图「${map.id}」未填写 bounds_cells，房间将按默认边界。`, nodeId: map.id });
            }
            if (!String(block.brief || "").trim()) {
                issues.push({ level: "warn", text: `地图「${map.id}」缺少 AreaBrief 简报。`, nodeId: map.id });
            }
            if (!this.config[map.id]?.cell_px) {
                issues.push({ level: "warn", text: `地图「${map.id}」底图未标定，POI 无法在缩略图上定位。`, nodeId: map.id });
            }
        });

        this.graphNodes().filter((node) => node.node_role === "poi").forEach((node) => {
            const poi = (node.poi || {}) as Record<string, unknown>;
            const areaId = String(poi.area_id || node.parent || "");
            if (!areaId || !maps.has(areaId)) {
                issues.push({ level: "error", text: `兴趣点「${node.title || node.id}」的 area_id 不是地图节点。`, nodeId: node.id });
            }
            const code = String(poi.code || "").trim();
            if (code && areaId) {
                const bucket = codes.get(areaId) || new Set<string>();
                if (bucket.has(code)) {
                    issues.push({ level: "error", text: `地图「${areaId}」的编号 ${code} 重复。`, nodeId: node.id });
                }
                bucket.add(code);
                codes.set(areaId, bucket);
            }
            const trigger = (poi.trigger || {}) as Record<string, unknown>;
            const mode = String(trigger.mode || "manual");
            const geometry = (poi.geometry || {}) as Record<string, unknown>;
            const rect = (geometry.rect_cells || {}) as Record<string, unknown>;
            if ((mode === "enter" || mode === "adjacent") && !(Number(rect.w) > 0 && Number(rect.h) > 0)) {
                issues.push({ level: "error", text: `兴趣点「${node.title || node.id}」是 ${mode} 触发，但缺少格几何。`, nodeId: node.id });
            }
            if (geometry.provisional && !String(geometry.note || "").trim()) {
                issues.push({ level: "warn", text: `兴趣点「${node.title || node.id}」标记为 provisional，但没有写明原因。`, nodeId: node.id });
            }
            const markerId = String(geometry.marker_id || "");
            if (markerId && !(this.config[areaId]?.markers || []).some((marker) => marker.id === markerId)) {
                issues.push({ level: "error", text: `兴趣点「${node.title || node.id}」关联的标记 ${markerId} 不存在。`, nodeId: node.id });
            }
        });

        this.graphEdges().filter((edge) => edge.type === "portal").forEach((edge) => {
            const transition = edge.transition as Record<string, unknown> | null | undefined;
            if (!transition) {
                issues.push({ level: "error", text: `通道 ${edge.id} 缺少 transition 过场。`, edgeId: edge.id });
                return;
            }
            const toArea = String(transition.to_area || "");
            if (toArea && !maps.has(toArea)) {
                issues.push({ level: "error", text: `通道 ${edge.id} 的 to_area「${toArea}」不是地图节点。`, edgeId: edge.id });
            }
            const entryPoi = String(transition.entry_poi || "");
            if (entryPoi) {
                const entry = this.graphNodes().find((node) => node.id === entryPoi);
                if (!entry || this.areaOfNode(entryPoi) !== toArea) {
                    issues.push({ level: "error", text: `通道 ${edge.id} 的 entry_poi「${entryPoi}」不在目标地图内。`, edgeId: edge.id });
                }
            }
        });
        return issues;
    }

    private renderValidation(): string {
        const issues = this.graphIssues();
        if (!issues.length) {
            return '<p class="scenario-map-graph-ok">校验通过。</p>';
        }
        return '<ul class="scenario-map-graph-issues">' + issues.map((issue) => (
            `<li class="${issue.level === "error" ? "is-error" : "is-warn"}">`
            + `${issue.level === "error" ? "错误" : "提示"}：${scenarioMapEscape(issue.text)}</li>`
        )).join("") + "</ul>";
    }

    private mapField(name: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
        return document.querySelector(`#scenarioMapGraphMapForm [data-graph-map-${name}]`);
    }

    private poiField(name: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
        return document.querySelector(`#scenarioMapGraphPoiForm [data-graph-poi-${name}]`);
    }

    private fieldText(element: Element | null): string {
        return element ? String((element as HTMLInputElement).value || "").trim() : "";
    }

    private fieldNumber(element: Element | null): number {
        const value = Number((element as HTMLInputElement | null)?.value);
        return Number.isFinite(value) ? value : 0;
    }

    private fieldChecked(element: Element | null): boolean {
        return Boolean((element as HTMLInputElement | null)?.checked);
    }

    private async patchGraph(path: string, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
        const scenarioId = this.scenarioIdValue();
        if (!scenarioId) throw new Error("请先保存剧本。");
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<Record<string, unknown>>>(
            `/api/scenarios/${scenarioId}/narrative/${path}`,
            { method: "PATCH", body: { base_revision: this.graphRevision, ...payload } },
        );
        if (response.status === 409) {
            throw new Error("世界树已被其他编辑保存，请重新打开地图页签后再改。");
        }
        if (!response.ok || !data?.success || !data.data) {
            throw new Error(data?.message || `HTTP ${response.status}`);
        }
        return data.data;
    }

    private async saveGraphMap(): Promise<void> {
        const node = this.selectedMapNode();
        if (!node || this.graphReadonly) return;
        const related = this.graphIssues().filter((issue) => issue.level === "error" && issue.nodeId === node.id);
        if (related.length) {
            showNotification(related[0]?.text || "存在校验错误", "error");
            return;
        }
        const existing = (node.map || {}) as Record<string, unknown>;
        const environment = {
            ...((existing.environment && typeof existing.environment === "object") ? existing.environment as Record<string, unknown> : {}),
            lighting: this.fieldText(this.mapField("lighting")),
            temperature: this.fieldText(this.mapField("temperature")),
            smell: this.fieldText(this.mapField("smell")),
            sound: this.fieldText(this.mapField("sound")),
            terrain: this.fieldText(this.mapField("terrain")).split(/[、,，]/).map((item) => item.trim()).filter(Boolean),
        };
        const entry = this.fieldText(this.mapField("entry")).split(",").map((item) => item.trim()).filter(Boolean);
        const nextMap = {
            ...existing,
            scene_id: this.fieldText(this.mapField("scene")) || node.id,
            grid_ft: this.fieldNumber(this.mapField("grid")) || 5,
            bounds_cells: this.fieldNumber(this.mapField("bounds")),
            environment,
            brief: this.fieldText(this.mapField("brief")),
            dm_brief: this.fieldText(this.mapField("dm-brief")),
            entry_pois: entry,
        };
        const parent = this.fieldText(this.mapField("parent"));
        if (parent) (nextMap as Record<string, unknown>).parent_area = parent;
        else delete (nextMap as Record<string, unknown>).parent_area;
        try {
            const result = await this.patchGraph(`nodes/${encodeURIComponent(node.id)}`, {
                title: this.fieldText(this.mapField("title")) || node.id,
                node_role: "map",
                map: nextMap,
            });
            this.applyNodeResult(result);
            this.notifyGraphSaved("地图节点已保存。");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    private async saveGraphPoi(): Promise<void> {
        const node = this.selectedPoiNode();
        if (!node || this.graphReadonly) return;
        const related = this.graphIssues().filter((issue) => issue.level === "error" && issue.nodeId === node.id);
        if (related.length) {
            showNotification(related[0]?.text || "存在校验错误", "error");
            return;
        }
        const existing = (node.poi || {}) as Record<string, unknown>;
        const existingTrigger = (existing.trigger && typeof existing.trigger === "object") ? existing.trigger as Record<string, unknown> : {};
        const existingGeometry = (existing.geometry && typeof existing.geometry === "object") ? existing.geometry as Record<string, unknown> : {};
        const width = this.fieldNumber(this.poiField("w"));
        const height = this.fieldNumber(this.poiField("h"));
        const geometry: Record<string, unknown> = {
            ...existingGeometry,
            provisional: this.fieldChecked(this.poiField("provisional")),
        };
        const note = this.fieldText(this.poiField("note"));
        if (note) geometry.note = note;
        else delete geometry.note;
        const markerId = this.fieldText(this.poiField("marker"));
        if (markerId) geometry.marker_id = markerId;
        else delete geometry.marker_id;
        if (width > 0 && height > 0) {
            geometry.rect_cells = {
                x: this.fieldNumber(this.poiField("x")),
                y: this.fieldNumber(this.poiField("y")),
                w: width,
                h: height,
            };
        } else {
            delete geometry.rect_cells;
        }
        const areaId = this.fieldText(this.poiField("area")) || this.selectedMapId;
        const nextPoi = {
            ...existing,
            area_id: areaId,
            code: this.fieldText(this.poiField("code")) || null,
            trigger: {
                ...existingTrigger,
                mode: this.fieldText(this.poiField("mode")) || "manual",
                radius_cells: this.fieldNumber(this.poiField("radius")),
                once: this.fieldChecked(this.poiField("once")),
            },
            geometry,
            entry_brief: this.fieldText(this.poiField("entry-brief")),
        };
        try {
            const result = await this.patchGraph(`nodes/${encodeURIComponent(node.id)}`, {
                title: this.fieldText(this.poiField("title")) || node.id,
                node_role: "poi",
                poi: nextPoi,
            });
            this.applyNodeResult(result);
            this.notifyGraphSaved("兴趣点已保存。");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    private async saveGraphEdge(edgeId: string): Promise<void> {
        if (this.graphReadonly) return;
        const row = document.querySelector<HTMLElement>(`#scenarioMapGraphTransitions [data-graph-edge-row="${CSS.escape(edgeId)}"]`);
        if (!row) return;
        const read = (name: string) => row.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[data-graph-edge-${name}]`);
        const edge = this.graphEdges().find((item) => item.id === edgeId);
        if (!edge) return;
        const direction = this.fieldText(read("direction")) || "one_way";
        const transition = {
            ...((edge.transition || {}) as Record<string, unknown>),
            from_area: this.fieldText(read("from-area")),
            to_area: this.fieldText(read("to-area")),
            from_poi: this.fieldText(read("from-poi")) || null,
            entry_poi: this.fieldText(read("entry-poi")) || null,
            direction,
            label: this.fieldText(read("label")),
            text: this.fieldText(read("text")),
            time_cost_minutes: this.fieldNumber(read("time")),
            handoff: {
                inject_area_brief: true,
                inject_entry_poi: true,
                time_cost_minutes: this.fieldNumber(read("time")),
            },
        };
        try {
            const result = await this.patchGraph(`edges/${encodeURIComponent(edgeId)}`, {
                type: "portal",
                label: transition.label,
                bidirectional: direction === "two_way",
                transition,
            });
            const updated = result.edge as ScenarioMapGraphEdge | undefined;
            if (updated) {
                this.graph!.edges = this.graphEdges().map((item) => (item.id === edgeId ? updated : item));
            }
            this.graphRevision = Number(result.revision) || this.graphRevision;
            this.renderGraph();
            this.notifyGraphSaved("过场已保存。");
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    private applyNodeResult(result: Record<string, unknown>): void {
        const node = result.node as ScenarioMapGraphNode | undefined;
        if (node && this.graph) {
            this.graph.nodes = this.graphNodes().map((item) => (item.id === node.id ? node : item));
        }
        this.graphRevision = Number(result.revision) || this.graphRevision;
        this.renderGraph();
        this.syncNarrativeEditor();
    }

    private notifyGraphSaved(message: string): void {
        showNotification(message, "success");
        const status = scenarioMapEl("scenarioMapGraphStatus");
        if (status) status.textContent = `已保存（revision ${this.graphRevision}）`;
        this.syncNarrativeEditor();
    }

    /** 保存后刷新世界树；若世界树有未保存改动则不覆盖。 */
    private syncNarrativeEditor(): void {
        const narrative = (window as unknown as { TrpgNarrative?: { runtime?: { dirty?: boolean } }; ScenarioNarrative?: { load?: (id: number) => Promise<void> } }).TrpgNarrative;
        const editor = (window as unknown as { ScenarioNarrative?: { load?: (id: number) => Promise<void> } }).ScenarioNarrative;
        if (narrative?.runtime?.dirty || !editor?.load) return;
        const scenarioId = this.scenarioIdValue();
        if (scenarioId) void editor.load(scenarioId);
    }

    private renderPoiOverlay(layer: HTMLElement | null): void {
        if (!layer || !this.graph) return;
        layer.querySelectorAll("[data-map-poi-id]").forEach((item) => item.remove());
        const areaId = this.selectedMapId;
        const config = this.config[areaId];
        if (!areaId || !config || !(config.cell_px > 0)) return;
        this.poiNodes(areaId).forEach((node) => {
            const geometry = (node.poi?.geometry || {}) as Record<string, unknown>;
            const rect = (geometry.rect_cells || {}) as Record<string, unknown>;
            const width = Number(rect.w || 0);
            const height = Number(rect.h || 0);
            if (!(width > 0 && height > 0)) return;
            const element = document.createElement("div");
            element.className = `scenario-map-poi${node.id === this.selectedPoiId ? " is-selected" : ""}`;
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

