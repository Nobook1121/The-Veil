/**
 * 实时配图展示层：房间场景画面面板 + 点击放大 + 历史缩略图。
 *
 * 展示策略：
 * - 主画面固定在聊天流上方（16:9、移动端限高），不插入每条消息，避免刷屏与布局抖动；
 * - 点击主画面打开大图查看；历史缩略图可回看最近若干张；
 * - 生成中是轻量占位，失败时保留上一张图并提示原因，绝不阻塞叙事。
 */
namespace TrpgImmersiveIllustrationModule {
    const PROFILE_LABELS: Record<string, string> = {
        daily: "日常",
        suspense: "悬疑",
        unease: "不安",
        horror: "恐怖",
        urgency: "紧迫",
        sorrow: "悲怆",
        awe: "敬畏",
        aftermath: "余波",
    };
    const INTENSITY_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高" };
    const PROVIDER_LABELS: Record<string, string> = {
        procedural: "程序化场景",
        platform: "平台生成图",
        openai_compatible: "外部生成图",
    };

    let documentBound = false;
    let stageBound = false;
    let stageElement: HTMLElement | null = null;
    let mountRetry = 0;
    let roomId = "";
    let canManage = false;
    let switches: ImmersiveRoomSwitches = {
        music: false,
        music_auto: false,
        illustration: false,
        illustration_auto: false,
    };
    let current: ImmersiveIllustrationItem | null = null;
    let history: ImmersiveIllustrationItem[] = [];
    let busy = false;
    let albumOpen = false;
    let lightbox: HTMLElement | null = null;
    let lightboxReturnFocus: HTMLElement | null = null;
    let lightboxDownloadItem: ImmersiveIllustrationItem | null = null;
    let escapeHandler: ((event: KeyboardEvent) => void) | null = null;
    let lastScene = "";
    let lastProfile = "daily";
    let lastIntensity: ImmersiveIntensity = "medium";
    let lastTurn = 0;
    // NPC 头像按房间复用：同一 NPC 只保留最新一张，避免重复生成与重复展示。
    const npcPortraits = new Map<string, ImmersiveIllustrationItem>();
    let stageHintTimer: number | null = null;

    function clearStageHintTimer(): void {
        if (stageHintTimer !== null) {
            window.clearTimeout(stageHintTimer);
            stageHintTimer = null;
        }
    }

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function setText(id: string, value: string): void {
        const element = byId<HTMLElement>(id);
        if (element) element.textContent = value;
    }

    /**
     * 场景面板由脚本创建并持有，React 只提供两个空宿主：
     * - 桌面端放进右侧栏底部（`immersiveSidebarStageHost`）；
     * - 窄屏右侧栏隐藏时移回聊天区（`immersiveChatStageHost`）。
     * 移动节点而不是重建，避免播放/按钮监听在断点切换时丢失。
     */
    function ensureStage(): HTMLElement {
        if (stageElement) return stageElement;
        const stage = document.createElement("section");
        stage.className = "immersive-stage";
        stage.id = "immersiveStage";
        stage.hidden = true;
        stage.setAttribute("aria-live", "polite");
        stage.innerHTML = `
            <div class="immersive-stage-head">
                <div class="immersive-stage-heading">
                    <span class="immersive-stage-kicker">SCENE</span>
                    <strong>场景</strong>
                </div>
                <span class="immersive-stage-heading-music" id="immersiveStageHeadMusic">配乐 · 日常/中</span>
            </div>
            <div class="immersive-stage-frame">
                <img class="immersive-stage-image" id="immersiveStageImage" alt="实时场景画面" tabindex="0" hidden>
                <div class="immersive-stage-placeholder" id="immersiveStagePlaceholder">
                    <span id="immersiveStagePlaceholderText">等待下一幕场景</span>
                </div>
                <div class="immersive-stage-status" id="immersiveStageStatus" hidden></div>
                <div class="immersive-npc-strip" id="immersiveNpcStrip" hidden></div>
                <div class="immersive-stage-overlay">
                    <span class="immersive-stage-scene" id="immersiveStageScene">实时场景</span>
                    <div class="immersive-stage-actions">
                        <button type="button" id="immersiveMusicToggle" title="静音 / 恢复实时配乐">配乐开</button>
                        <button type="button" id="immersiveAmbientToggle" title="开启 / 关闭环境音（仅影响自己）">环境音开</button>
                        <button type="button" id="immersiveDownloadBtn" title="下载当前场景图" hidden>下载</button>
                        <button type="button" id="immersiveRegenerateBtn" title="按当前场景立即生成一张配图" hidden>重新配图</button>
                        <button type="button" id="immersiveHistoryBtn" hidden>历史 0</button>
                    </div>
                </div>
            </div>
            <div class="immersive-stage-caption" id="immersiveStageCaption"></div>
            <div class="immersive-history" id="immersiveHistory" hidden></div>
        `;
        // 先挂在 body 上保证 init 时按钮可绑定；随后 mountStage 会移入宿主。
        document.body.appendChild(stage);
        stageElement = stage;
        return stage;
    }

    function isNarrowLayout(): boolean {
        return window.matchMedia("(max-width: 767.98px)").matches;
    }

    function mountStage(): void {
        const stage = ensureStage();
        const hostId = isNarrowLayout() ? "immersiveChatStageHost" : "immersiveSidebarStageHost";
        const host = document.getElementById(hostId);
        if (!host) {
            // React 宿主可能比沉浸脚本晚一帧挂载；短暂重试，不阻塞初始化。
            if (mountRetry < 20) {
                mountRetry += 1;
                window.requestAnimationFrame(() => mountStage());
            }
            return;
        }
        mountRetry = 0;
        if (stage.parentElement !== host) host.appendChild(stage);
    }

    function bindStageEvents(): void {
        byId<HTMLElement>("immersiveStageImage")?.addEventListener("click", () => {
            if (current?.url) openLightbox(current);
        });
        byId<HTMLElement>("immersiveStageImage")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                if (current?.url) openLightbox(current);
            }
        });
        byId<HTMLButtonElement>("immersiveRegenerateBtn")?.addEventListener("click", () => {
            requestNow();
        });
        byId<HTMLButtonElement>("immersiveHistoryBtn")?.addEventListener("click", () => {
            toggleHistory();
        });
        byId<HTMLButtonElement>("immersiveDownloadBtn")?.addEventListener("click", () => {
            if (current) void downloadItem(current);
        });
    }

    export function init(): void {
        ensureStage();
        mountStage();
        if (!stageBound) {
            bindStageEvents();
            stageBound = true;
        }
        if (documentBound) return;
        documentBound = true;
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && lightbox) closeLightbox();
        });
        const narrowQuery = window.matchMedia("(max-width: 767.98px)");
        const handleLayoutChange = (): void => mountStage();
        if (typeof narrowQuery.addEventListener === "function") {
            narrowQuery.addEventListener("change", handleLayoutChange);
        } else {
            narrowQuery.addListener(handleLayoutChange);
        }
    }

    export function setRoom(room: Room | null): void {
        const next = room ? String(room.id || "") : "";
        if (next === roomId) return;
        roomId = next;
        current = null;
        history = [];
        busy = false;
        albumOpen = false;
        lastScene = "";
        lastProfile = "daily";
        lastIntensity = "medium";
        lastTurn = 0;
        npcPortraits.clear();
        clearStageHintTimer();
        closeLightbox();
        resetStage();
        setVisible(false);
    }

    export function applyState(state: ImmersiveRoomState | null | undefined): void {
        if (!state || !roomId) return;
        switches = state.switches || switches;
        canManage = Boolean(state.can_manage);
        const music = state.music || {};
        lastScene = music.scene || "";
        lastProfile = music.profile || "daily";
        lastIntensity = music.intensity || "medium";
        lastTurn = Number(music.turns || 0);
        const items = Array.isArray(state.illustration?.history)
            ? state.illustration!.history!.filter((item) => item && typeof item.url === "string" && item.url)
            : [];
        history = items.slice(-12);
        npcPortraits.clear();
        for (const item of items) {
            if (item.category === "portrait" && item.npc_name) {
                npcPortraits.set(String(item.npc_name), item);
            }
        }
        renderNpcStrip();
        updateSceneLabel();
        updateButtons();
        if (history.length) {
            const sceneItems = history.filter((item) => item.category !== "portrait");
            const latest = sceneItems[sceneItems.length - 1];
            if (latest) renderImage(latest, false);
            else showIdle();
            renderHistory();
        } else {
            showIdle();
        }
        setVisible(switches.music || switches.illustration);
    }

    export function updateSwitches(next: ImmersiveRoomSwitches): void {
        switches = next;
        updateButtons();
        if (roomId) setVisible(switches.music || switches.illustration);
    }

    export function onMusic(plan: ImmersiveMusicPlan): void {
        if (!roomId) return;
        if (plan.room_id && String(plan.room_id) !== roomId) return;
        lastProfile = plan.profile || "daily";
        lastIntensity = plan.intensity || "medium";
        lastTurn = Number(plan.turns || lastTurn);
        if (plan.scene) lastScene = plan.scene;
        updateSceneLabel();
        updateButtons();
        if (switches.music || switches.illustration) setVisible(true);
    }

    export function onIllustration(payload: ImmersiveIllustrationEvent): void {
        if (!roomId) return;
        if (payload.room_id && String(payload.room_id) !== roomId) return;
        const status = String(payload.status || "");
        if (status === "deciding") {
            showStageStatus(payload.label || "KP 正在判断是否需要配图…", "pending");
            return;
        }
        if (status === "generating") {
            showPending(payload.scene || lastScene || "当前场景");
            return;
        }
        if (status === "skipped") {
            showTransientStatus("本轮无需配图", "muted");
            return;
        }
        if (status === "complete") {
            showTransientStatus("配图完成", "muted", 1600);
            return;
        }
        if (status === "pending") {
            showPending(payload.scene || lastScene || "当前场景");
            return;
        }
        if (status === "error") {
            showError(payload.error || payload.reason || "配图服务暂不可用", payload.retryable !== false);
            return;
        }
        if (!payload.url) return;
        const item = normalizeItem(payload);
        if (item.category === "portrait" && item.npc_name) {
            const key = String(item.npc_name);
            const existing = npcPortraits.get(key);
            if (existing && existing.url === item.url) return;
            npcPortraits.set(key, item);
            renderNpcStrip();
            showTransientStatus(`${key} 的立绘已就绪`, "muted", 1400);
            return;
        }
        if (current && current.url === item.url) return;
        if (item.scene) lastScene = item.scene;
        appendHistory(item);
        renderImage(item, true);
        renderHistory();
        setVisible(true);
    }

    function showStageStatus(text: string, level: "pending" | "muted" | "error"): void {
        clearStageHintTimer();
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (!status) return;
        status.hidden = false;
        status.textContent = text;
        status.dataset.level = level;
    }

    function showTransientStatus(text: string, level: "pending" | "muted" | "error", delay = 2400): void {
        showStageStatus(text, level);
        clearStageHintTimer();
        stageHintTimer = window.setTimeout(() => {
            const status = byId<HTMLElement>("immersiveStageStatus");
            if (status) {
                status.hidden = true;
                status.textContent = "";
                delete status.dataset.level;
            }
            stageHintTimer = null;
        }, delay);
    }

    function renderNpcStrip(): void {
        const strip = byId<HTMLElement>("immersiveNpcStrip");
        if (!strip) return;
        strip.innerHTML = "";
        const entries = Array.from(npcPortraits.entries());
        if (!entries.length) {
            strip.hidden = true;
            return;
        }
        for (const [name, item] of entries) {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "immersive-npc-chip";
            chip.title = `查看 ${name} 的立绘`;
            const image = document.createElement("img");
            image.src = item.url;
            image.alt = `${name} 立绘`;
            image.loading = "lazy";
            image.decoding = "async";
            const label = document.createElement("span");
            label.textContent = name;
            chip.appendChild(image);
            chip.appendChild(label);
            chip.addEventListener("click", () => openLightbox(item));
            strip.appendChild(chip);
        }
        strip.hidden = false;
    }

    export function requestNow(): void {
        if (!roomId || !canManage || busy) return;
        if (!switches.illustration) {
            window.showNotification?.("当前房间未开启实时配图", "warning");
            return;
        }
        busy = true;
        updateButtons();
        const scene = lastScene || "当前场景";
        showPending(scene);
        void submitRequest(scene);
    }

    async function submitRequest(scene: string): Promise<void> {
        try {
            const response = await TrpgApi.post<ApiResponse<ImmersiveIllustrationEvent>>(
                `/api/immersive/rooms/${encodeURIComponent(roomId)}/illustration`,
                {
                    scene,
                    profile: lastProfile,
                    intensity: lastIntensity,
                    turn: lastTurn,
                },
            );
            if (!response.success || !response.data) {
                showError(response.message || response.error || "配图失败");
                window.showNotification?.(response.message || response.error || "配图失败", "error");
                return;
            }
            onIllustration({ ...response.data, status: "ready" });
        } catch (error) {
            const message = error instanceof Error ? error.message : "配图请求失败";
            showError(message);
            window.showNotification?.(`配图失败：${message}`, "error");
        } finally {
            busy = false;
            updateButtons();
        }
    }

    function normalizeItem(payload: ImmersiveIllustrationEvent): ImmersiveIllustrationItem {
        return {
            url: String(payload.url || ""),
            filename: payload.filename,
            scene: payload.scene,
            profile: payload.profile,
            intensity: payload.intensity,
            provider: payload.provider,
            degraded: payload.degraded,
            error: payload.error,
            turn: payload.turn,
            category: payload.category,
            npc_name: payload.npc_name,
            cache_level: payload.cache_level,
            created_at: payload.created_at || new Date().toLocaleTimeString(),
        };
    }

    function appendHistory(item: ImmersiveIllustrationItem): void {
        if (history.some((entry) => entry.url === item.url)) return;
        history.push(item);
        if (history.length > 12) history = history.slice(-12);
    }

    function resetStage(): void {
        clearStageHintTimer();
        const image = byId<HTMLImageElement>("immersiveStageImage");
        if (image) {
            image.removeAttribute("src");
            image.hidden = true;
            image.classList.remove("is-visible");
        }
        byId<HTMLElement>("immersiveStagePlaceholder")?.classList.remove("is-error", "is-loading");
        const placeholderText = byId<HTMLElement>("immersiveStagePlaceholderText");
        if (placeholderText) placeholderText.textContent = "等待下一幕场景";
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (status) {
            status.hidden = true;
            status.textContent = "";
        }
        setText("immersiveStageScene", "实时场景");
        setText("immersiveStageCaption", "");
        const download = byId<HTMLButtonElement>("immersiveDownloadBtn");
        if (download) download.hidden = true;
        const musicLabel = byId<HTMLElement>("immersiveStageHeadMusic");
        if (musicLabel) musicLabel.textContent = "配乐 · 日常/中";
        const historyList = byId<HTMLElement>("immersiveHistory");
        if (historyList) {
            historyList.innerHTML = "";
            historyList.hidden = true;
        }
        const npcStrip = byId<HTMLElement>("immersiveNpcStrip");
        if (npcStrip) {
            npcStrip.innerHTML = "";
            npcStrip.hidden = true;
        }
    }

    function setVisible(visible: boolean): void {
        const stage = byId<HTMLElement>("immersiveStage");
        if (stage) stage.hidden = !visible;
    }

    function showIdle(): void {
        const image = byId<HTMLImageElement>("immersiveStageImage");
        if (image && !current) image.hidden = true;
        const placeholder = byId<HTMLElement>("immersiveStagePlaceholder");
        if (placeholder) {
            placeholder.hidden = false;
            placeholder.classList.remove("is-error", "is-loading");
        }
        const placeholderText = byId<HTMLElement>("immersiveStagePlaceholderText");
        if (placeholderText) placeholderText.textContent = switches.illustration ? "等待场景推进，关键回合会自动配图" : "等待场景推进";
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (status) {
            status.hidden = true;
            status.textContent = "";
        }
        setText("immersiveStageCaption", "");
    }

    function renderImage(item: ImmersiveIllustrationItem, animate: boolean): void {
        current = item;
        const image = byId<HTMLImageElement>("immersiveStageImage");
        const placeholder = byId<HTMLElement>("immersiveStagePlaceholder");
        if (!image) return;
        image.alt = item.scene ? `场景：${item.scene}` : "实时场景画面";
        if (animate) image.classList.remove("is-visible");
        image.onload = () => {
            image.hidden = false;
            image.classList.add("is-visible");
            if (placeholder) placeholder.hidden = true;
        };
        image.onerror = () => {
            image.hidden = true;
            if (placeholder) {
                placeholder.hidden = false;
                placeholder.classList.add("is-error");
            }
            const placeholderText = byId<HTMLElement>("immersiveStagePlaceholderText");
            if (placeholderText) placeholderText.textContent = "画面加载失败，叙事不受影响";
            setText("immersiveStageCaption", "");
        };
        image.src = item.url;
        image.hidden = false;
        if (placeholder) {
            placeholder.hidden = true;
            placeholder.classList.remove("is-error", "is-loading");
        }
        updateStatus(item);
        const parts: string[] = [];
        if (item.scene) parts.push(item.scene);
        const provider = PROVIDER_LABELS[item.provider || ""] || item.provider || "";
        if (provider) parts.push(provider);
        setText("immersiveStageCaption", parts.join(" · "));
        if (item.scene) lastScene = item.scene;
        updateSceneLabel();
        updateButtons();
    }

    function updateStatus(item: ImmersiveIllustrationItem): void {
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (!status) return;
        if (item.degraded) {
            status.hidden = false;
            status.textContent = item.error
                ? `已回落程序化场景图（${shortReason(item.error)}）`
                : "已回落程序化场景图";
            status.dataset.level = "warning";
            return;
        }
        status.hidden = true;
        status.textContent = "";
        delete status.dataset.level;
    }

    function shortReason(value: string): string {
        const text = String(value || "").trim();
        return text.length > 42 ? `${text.slice(0, 42)}…` : text;
    }

    function showPending(scene: string): void {
        clearStageHintTimer();
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (status) {
            status.hidden = false;
            status.textContent = scene && scene !== lastScene ? "正在切换场景并配图…" : "正在配图…";
            status.dataset.level = "pending";
        }
        const placeholder = byId<HTMLElement>("immersiveStagePlaceholder");
        const placeholderText = byId<HTMLElement>("immersiveStagePlaceholderText");
        const image = byId<HTMLImageElement>("immersiveStageImage");
        if (image && !current) image.hidden = true;
        if (!current && placeholder && placeholderText) {
            placeholder.hidden = false;
            placeholder.classList.add("is-loading");
            placeholder.classList.remove("is-error");
            placeholderText.textContent = scene ? `正在配图：${scene}` : "正在配图…";
        }
        if (scene) setText("immersiveStageScene", `${scene} · 配图中`);
    }

    function showError(message: string, retryable = true): void {
        clearStageHintTimer();
        const status = byId<HTMLElement>("immersiveStageStatus");
        if (status) {
            status.hidden = false;
            status.textContent = retryable && canManage
                ? `配图失败：${shortReason(message)}（可点击「重新配图」重试）`
                : `配图失败：${shortReason(message)}`;
            status.dataset.level = "error";
        }
        if (!current) {
            const placeholder = byId<HTMLElement>("immersiveStagePlaceholder");
            const placeholderText = byId<HTMLElement>("immersiveStagePlaceholderText");
            if (placeholder && placeholderText) {
                placeholder.hidden = false;
                placeholder.classList.add("is-error");
                placeholder.classList.remove("is-loading");
                placeholderText.textContent = "配图失败，叙事继续";
            }
        }
    }

    function updateSceneLabel(): void {
        const scene = lastScene || "未知场景";
        const profile = PROFILE_LABELS[lastProfile] || lastProfile;
        const intensity = INTENSITY_LABELS[lastIntensity] || lastIntensity;
        setText("immersiveStageScene", `${scene} · ${profile}/${intensity}`);
        const musicLabel = byId<HTMLElement>("immersiveStageHeadMusic");
        if (musicLabel) musicLabel.textContent = `配乐 · ${profile}/${intensity}`;
    }

    function updateButtons(): void {
        const regenerate = byId<HTMLButtonElement>("immersiveRegenerateBtn");
        if (regenerate) {
            regenerate.hidden = !canManage || !switches.illustration;
            regenerate.disabled = busy;
            regenerate.title = busy ? "正在生成…" : "按当前场景立即生成一张配图";
        }
        const historyButton = byId<HTMLButtonElement>("immersiveHistoryBtn");
        if (historyButton) {
            historyButton.hidden = history.length === 0;
            historyButton.textContent = albumOpen ? "收起历史" : `历史 ${history.length}`;
        }
        const historyList = byId<HTMLElement>("immersiveHistory");
        if (historyList) historyList.hidden = !albumOpen || history.length === 0;
        const download = byId<HTMLButtonElement>("immersiveDownloadBtn");
        if (download) {
            download.hidden = !current?.url;
            download.disabled = !roomId;
        }
        const musicButton = byId<HTMLButtonElement>("immersiveMusicToggle");
        if (musicButton) {
            const source = window.TrpgImmersive?.getMusicSource?.() || "hybrid";
            const muted = Boolean(window.TrpgImmersiveMusic?.isMuted());
            if (source === "bgm") {
                musicButton.textContent = "配乐未启用";
                musicButton.dataset.muted = "true";
                musicButton.disabled = true;
                musicButton.title = "当前音乐来源为「仅 BGM」；可在个人设置中切换";
            } else {
                musicButton.textContent = muted ? "配乐已静音" : "配乐开";
                musicButton.dataset.muted = muted ? "true" : "false";
                musicButton.disabled = false;
                musicButton.title = muted ? "点击恢复实时配乐" : "点击静音实时配乐";
            }
        }
        const ambientButton = byId<HTMLButtonElement>("immersiveAmbientToggle");
        if (ambientButton) {
            const enabled = Boolean(window.TrpgImmersiveAmbient?.isEnabled());
            ambientButton.textContent = enabled ? "环境音开" : "环境音关";
            ambientButton.dataset.enabled = enabled ? "true" : "false";
            ambientButton.title = enabled ? "点击关闭环境音（仅影响自己）" : "点击开启环境音（仅影响自己）";
        }
        // 输入区与右侧栏窄条上的两个开关：配乐 / 环境音各自独立。
        const musicMuted = Boolean(window.TrpgImmersiveMusic?.isMuted());
        const musicSource = window.TrpgImmersive?.getMusicSource?.() || "hybrid";
        const ambientEnabled = Boolean(window.TrpgImmersiveAmbient?.isEnabled());
        for (const id of ["homeRoomMusicToggle", "homeRoomMembersRailMusic"]) {
            const button = byId<HTMLButtonElement>(id);
            if (!button) continue;
            button.dataset.muted = musicMuted ? "true" : "false";
            button.dataset.enabled = musicMuted ? "false" : "true";
            button.setAttribute("aria-pressed", musicMuted ? "false" : "true");
            button.disabled = musicSource === "bgm";
            button.title = musicSource === "bgm"
                ? "当前音乐来源为「仅 BGM」，实时配乐未启用"
                : musicMuted ? "点击恢复实时配乐（仅影响自己）" : "点击静音实时配乐（仅影响自己）";
        }
        for (const id of ["homeRoomAmbientToggle", "homeRoomMembersRailAmbient"]) {
            const button = byId<HTMLButtonElement>(id);
            if (!button) continue;
            button.dataset.enabled = ambientEnabled ? "true" : "false";
            button.setAttribute("aria-pressed", ambientEnabled ? "true" : "false");
            button.title = ambientEnabled ? "点击关闭环境音（仅影响自己）" : "点击开启环境音（仅影响自己）";
        }
    }

    export function syncMusicButton(): void {
        updateButtons();
    }

    function renderHistory(): void {
        const container = byId<HTMLElement>("immersiveHistory");
        if (!container) return;
        container.innerHTML = "";
        const recent = history.slice(-8).reverse();
        for (const item of recent) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "immersive-history-thumb";
            button.title = item.scene || "场景画面";
            const image = document.createElement("img");
            image.loading = "lazy";
            image.decoding = "async";
            image.alt = item.scene || "场景缩略图";
            image.src = item.url;
            button.appendChild(image);
            button.addEventListener("click", () => openLightbox(item));
            container.appendChild(button);
        }
        container.hidden = !albumOpen || recent.length === 0;
    }

    function toggleHistory(): void {
        albumOpen = !albumOpen;
        renderHistory();
        updateButtons();
    }

    async function downloadItem(item: ImmersiveIllustrationItem): Promise<void> {
        if (!roomId || !item.url) {
            window.showNotification?.("当前没有可下载的场景图", "warning");
            return;
        }
        const params = new URLSearchParams({ url: item.url });
        if (item.scene) params.set("scene", item.scene);
        try {
            const response = await fetch(
                `/api/immersive/rooms/${encodeURIComponent(roomId)}/illustration/download?${params.toString()}`,
                { credentials: "same-origin" },
            );
            if (!response.ok) {
                let message = `下载失败（HTTP ${response.status}）`;
                try {
                    const payload = await response.json() as { message?: string; error?: string };
                    message = payload.message || payload.error || message;
                } catch {
                    // 响应不是 JSON 时保留 HTTP 状态提示。
                }
                window.showNotification?.(message, "error");
                return;
            }
            const blob = await response.blob();
            if (!blob.size) {
                window.showNotification?.("下载失败：服务端返回空文件", "error");
                return;
            }
            const disposition = response.headers.get("Content-Disposition") || "";
            const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
            let filename = match && match[1] ? decodeURIComponent(match[1]) : "";
            if (!filename) {
                const suffix = item.url.split(".").pop()?.split("?")[0] || "png";
                filename = `场景画面-${new Date().toISOString().slice(0, 19).replace(/[^\d]/g, "")}.${suffix}`;
            }
            const objectUrl = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = objectUrl;
            anchor.download = filename;
            anchor.rel = "noopener";
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            window.setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            window.showNotification?.(`下载失败：${message}`, "error");
        }
    }

    function openLightbox(item: ImmersiveIllustrationItem): void {
        closeLightbox();
        lightboxReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        lightboxDownloadItem = item;
        const overlay = document.createElement("div");
        overlay.className = "immersive-lightbox";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "场景大图");

        const figure = document.createElement("figure");
        const image = document.createElement("img");
        image.src = item.url;
        image.alt = item.scene || "场景大图";
        image.decoding = "async";
        const caption = document.createElement("figcaption");
        const captionParts: string[] = [];
        if (item.scene) captionParts.push(item.scene);
        if (item.provider) captionParts.push(PROVIDER_LABELS[item.provider] || item.provider);
        if (item.created_at) captionParts.push(item.created_at);
        const captionText = document.createElement("span");
        captionText.textContent = captionParts.join(" · ") || "场景大图";
        const actions = document.createElement("div");
        actions.className = "immersive-lightbox-actions";
        const download = document.createElement("button");
        download.type = "button";
        download.textContent = "下载原图";
        download.addEventListener("click", () => {
            if (lightboxDownloadItem) void downloadItem(lightboxDownloadItem);
        });
        actions.appendChild(download);
        caption.appendChild(captionText);
        caption.appendChild(actions);
        figure.appendChild(image);
        figure.appendChild(caption);

        const close = document.createElement("button");
        close.type = "button";
        close.className = "immersive-lightbox-close";
        close.setAttribute("aria-label", "关闭");
        close.innerHTML = "&times;";
        close.addEventListener("click", () => closeLightbox());

        overlay.appendChild(close);
        overlay.appendChild(figure);
        overlay.addEventListener("click", (event) => {
            if (event.target === overlay) closeLightbox();
        });
        document.body.appendChild(overlay);
        document.body.classList.add("immersive-lightbox-open");
        lightbox = overlay;
        escapeHandler = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                closeLightbox();
                return;
            }
            if (event.key === "Tab") {
                // 灯箱只有「关闭 / 下载」两个焦点，循环 Tab 避免焦点跑到背后页面。
                const buttons = Array.from(overlay.querySelectorAll<HTMLElement>("button")).filter(
                    (button) => !button.hasAttribute("disabled"),
                );
                if (!buttons.length) return;
                const first = buttons[0];
                const last = buttons[buttons.length - 1];
                if (!first || !last) return;
                const activeIndex = buttons.indexOf(document.activeElement as HTMLElement);
                if (event.shiftKey && (activeIndex === 0 || activeIndex === -1)) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && (activeIndex === buttons.length - 1 || activeIndex === -1)) {
                    event.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener("keydown", escapeHandler);
        download.focus();
    }

    function closeLightbox(): void {
        if (!lightbox) return;
        lightbox.remove();
        lightbox = null;
        lightboxDownloadItem = null;
        document.body.classList.remove("immersive-lightbox-open");
        if (escapeHandler) {
            document.removeEventListener("keydown", escapeHandler);
            escapeHandler = null;
        }
        const target = lightboxReturnFocus;
        lightboxReturnFocus = null;
        if (target && target.isConnected) target.focus();
    }
}
