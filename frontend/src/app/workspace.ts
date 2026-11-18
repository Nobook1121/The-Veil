/**
 * 房间 workspace 布局仲裁与 canvas 舞台宿主。
 *
 * 主视图归属只读规则集插件在 ``/api/rulesets`` 暴露的 ``workspace`` 声明；
 * 核心不写死 5e 的 380/48/720，也不为未声明 canvas 的规则集创建地图 DOM。
 * 布局切换只改 data 属性与 CSS 变量，不重挂载聊天节点，聊天滚动与地图镜头都保留。
 */
(function () {
    type Layout = "canvas" | "conversation";
    type Panel = "chat" | "character" | "settings";
    type Scope = "session" | "room" | "default";
    type SizeMode = "wide" | "medium" | "compact" | "narrow";
    type DrawerState = "collapsed" | "half" | "full";

    interface Camera {
        x: number;
        y: number;
        zoom: number;
    }

    interface StageView {
        background: string;
        gridSize: number;
        // 格线原点（世界像素）；旧状态没有该字段时按 (0, 0) 渲染。
        gridOffset: { x: number; y: number };
        markers: RoomStageMarker[];
    }

    interface ChatScrollSnapshot {
        top: number;
        bottom: boolean;
    }

    const sessionLayouts = new Map<string, Layout>();
    const cameras = new Map<string, Camera>();
    const stageViews = new Map<string, StageView>();
    const chatScroll = new Map<string, ChatScrollSnapshot>();

    let rows: RulesetWorkspaceRow[] = [];
    let rowsRequest: Promise<void> | null = null;
    let initialized = false;
    let spec: RulesetWorkspaceSpec | null = null;
    let layout: Layout = "conversation";
    let roomId = "";
    let panel: Panel = "chat";
    let sizeMode: SizeMode = "wide";
    let sidebarCollapsed = false;
    let sidebarTouched = false;
    let overlayOpen = false;
    let drawer: DrawerState = "collapsed";
    let activeScope: Scope = "session";
    let userSidebarWidth = 0;
    let activeTool = "select";
    let showGrid = true;
    let combatState: CombatStatePayload | null = null;
    let resizeTimer = 0;

    interface ActiveStagePlugin {
        pluginId: string;
        el: HTMLElement;
        instance: TrpgPluginInstance | null;
    }

    const pluginHandlers = new Map<string, Set<(payload: unknown) => void>>();
    const pluginAssets = new Map<string, Promise<void>>();
    const pluginNotified = new Set<string>();
    let stagePlugin: ActiveStagePlugin | null = null;
    let pluginRequestSeq = 0;

    let canvas: HTMLCanvasElement | null = null;
    let stageContext: CanvasRenderingContext2D | null = null;
    let stageObserver: ResizeObserver | null = null;
    let backgroundImage: HTMLImageElement | null = null;
    let backgroundUrl = "";
    let dragging = false;
    let dragStartX = 0;
    let dragStartY = 0;
    let dragCameraX = 0;
    let dragCameraY = 0;

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function bodyElement(): HTMLElement | null {
        return byId<HTMLElement>("homeChatBody");
    }

    function translate(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function clamp(value: number, minimum: number, maximum: number): number {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function normalize(value: unknown): string {
        return String(value ?? "").trim().toLowerCase();
    }

    function currentUserId(): string {
        return String(window.currentUser?.user_id || "anon");
    }

    function readStore(key: string): string {
        try {
            return window.localStorage.getItem(key) || "";
        } catch {
            return "";
        }
    }

    function writeStore(key: string, value: string): void {
        try {
            window.localStorage.setItem(key, value);
        } catch {
            // 隐私模式/存储不可用：仅本次会话生效。
        }
    }

    function removeStore(key: string): void {
        try {
            window.localStorage.removeItem(key);
        } catch {
            // 同上。
        }
    }

    function roomLayoutKey(id = roomId): string {
        return `trpg:workspace:room:${currentUserId()}:${id}`;
    }

    function defaultLayoutKey(): string {
        return `trpg:workspace:default:${currentUserId()}`;
    }

    function sidebarWidthKey(): string {
        return `trpg:workspace:sidebar:${currentUserId()}`;
    }

    function stageStorageKey(id = roomId): string {
        return `trpg:workspace:stage:${id}`;
    }

    function activeRoom(): Room | null {
        return window.currentRoom || null;
    }

    function memberCard(member: RoomMember | undefined): Partial<COC7CharacterCard> & { rulesetId?: string; ruleset_id?: string; sheet_ui?: string } {
        return (member?.character_card || {}) as Partial<COC7CharacterCard> & {
            rulesetId?: string;
            ruleset_id?: string;
            sheet_ui?: string;
        };
    }

    function selfMember(room: Room | null): RoomMember | undefined {
        if (!room) return undefined;
        return (room.members || []).find((member) => String(member.user_id) === String(window.currentUser?.user_id));
    }

    function roomRulesetId(room: Room | null): string {
        if (!room) return "";
        const explicit = normalize(room.ruleset_id || room.scenario?.ruleset_id);
        if (explicit) return explicit;
        // 角色卡规则集只在房间/剧本都缺失时作为最低优先级的归属提示。
        const card = memberCard(selfMember(room));
        return normalize(card.rulesetId || card.ruleset_id || card.sheet_ui);
    }

    function isDm(): boolean {
        const role = String(window.currentUser?.role || "").toUpperCase();
        if (role === "ADMIN" || role === "OWNER") return true;
        const self = selfMember(activeRoom());
        return self?.room_role === "owner" || self?.room_role === "admin";
    }

    async function loadWorkspaces(): Promise<void> {
        if (rowsRequest) return rowsRequest;
        rowsRequest = (async () => {
            try {
                const response = await TrpgApi.get<ApiResponse<RulesetWorkspaceRow[]>>("/api/rulesets");
                if (response?.success && Array.isArray(response.data)) rows = response.data;
            } catch {
                // 接口失败时保持上一次声明；没有声明就回退 conversation。
            } finally {
                rowsRequest = null;
            }
        })();
        return rowsRequest;
    }

    function specFor(rulesetId: string): RulesetWorkspaceSpec | null {
        const key = normalize(rulesetId);
        if (!key) return null;
        const row = rows.find((item) => normalize(item.id) === key);
        return row?.workspace || null;
    }

    function supportsRuleset(candidate: RulesetWorkspaceSpec | null, rulesetId: string): boolean {
        if (!candidate) return false;
        // 空数组表示适用所有规则集；与后端 select_workspace 的语义保持一致。
        if (candidate.rulesets.length === 0) return true;
        return candidate.rulesets.map(normalize).includes(normalize(rulesetId));
    }

    function canvasCapable(candidate: RulesetWorkspaceSpec | null, rulesetId: string): boolean {
        return Boolean(candidate && candidate.primary === "canvas" && supportsRuleset(candidate, rulesetId));
    }

    function compatibleLayout(value: string, candidate: RulesetWorkspaceSpec | null, rulesetId: string): Layout | null {
        if (value === "conversation") return "conversation";
        if (value === "canvas" && canvasCapable(candidate, rulesetId)) return "canvas";
        return null;
    }

    function resolveLayout(room: Room | null): Layout {
        if (!room?.id) return "conversation";
        const rulesetId = roomRulesetId(room);
        const candidate = specFor(rulesetId);

        const sessionChoice = sessionLayouts.get(room.id);
        if (sessionChoice) {
            return compatibleLayout(sessionChoice, candidate, rulesetId) || "conversation";
        }

        const roomChoice = readStore(roomLayoutKey(room.id));
        if (roomChoice) {
            const allowed = compatibleLayout(roomChoice, candidate, rulesetId);
            if (allowed) return allowed;
        }

        if (room.ui_layout) {
            const allowed = compatibleLayout(room.ui_layout, candidate, rulesetId);
            if (allowed) return allowed;
        }

        const defaultChoice = readStore(defaultLayoutKey());
        if (defaultChoice) {
            const allowed = compatibleLayout(defaultChoice, candidate, rulesetId);
            if (allowed) return allowed;
        }

        if (canvasCapable(candidate, rulesetId)) return "canvas";

        if (room.scenario?.ui_layout) {
            const allowed = compatibleLayout(room.scenario.ui_layout, candidate, rulesetId);
            if (allowed) return allowed;
        }

        return "conversation";
    }

    function detectSizeMode(): SizeMode {
        const width = window.innerWidth;
        if (width >= 1440) return "wide";
        if (width >= 1080) return "medium";
        if (width >= 768) return "compact";
        return "narrow";
    }

    function applyWorkspaceVariables(): void {
        const body = bodyElement();
        if (!body || !spec) return;
        const baseWidth = userSidebarWidth > 0 ? userSidebarWidth : spec.sidebar.width;
        body.style.setProperty("--ws-sidebar-width", `${baseWidth}px`);
        body.style.setProperty("--ws-sidebar-min", `${spec.sidebar.min_width}px`);
        body.style.setProperty("--ws-sidebar-max", `${spec.sidebar.max_width}px`);
        body.style.setProperty("--ws-sidebar-collapsed", `${spec.sidebar.collapsed_width}px`);
        // 中等宽度按声明基准收紧 40px（380 → 340），不把 340 写进核心样式。
        body.style.setProperty("--ws-sidebar-medium", `${Math.max(spec.sidebar.min_width - 20, baseWidth - 40)}px`);
        body.style.setProperty("--ws-sidebar-overlay", `${spec.sidebar.min_width}px`);
        body.style.setProperty("--ws-topbar-height", `${spec.topbar.height}px`);
        body.style.setProperty("--ws-topbar-compact", `${Math.max(0, spec.topbar.height - 4)}px`);
        body.style.setProperty("--ws-rail-width", `${spec.left_rail.visible ? spec.left_rail.width : 0}px`);
        body.style.setProperty("--ws-primary-min", `${spec.primary_min_width}px`);
    }

    function syncResponsiveState(): void {
        const body = bodyElement();
        if (!body) return;
        sizeMode = detectSizeMode();
        const narrowScreen = window.innerWidth < 1200;
        if (sizeMode === "compact" || sizeMode === "narrow" || narrowScreen) {
            sidebarCollapsed = true;
        } else if (sizeMode === "wide" || sizeMode === "medium") {
            if (!sidebarTouched) sidebarCollapsed = false;
        }

        const baseWidth = userSidebarWidth > 0 ? userSidebarWidth : (spec?.sidebar.width || 260);
        let activeWidth = baseWidth;
        if (layout === "canvas" && spec) {
            if (sizeMode === "medium") activeWidth = Math.max(spec.sidebar.min_width - 20, baseWidth - 40);
            else if (sizeMode === "compact") activeWidth = spec.sidebar.min_width;
        }
        if (sidebarCollapsed && (sizeMode === "wide" || sizeMode === "medium")) {
            activeWidth = spec?.sidebar.collapsed_width || 48;
        }
        body.style.setProperty("--ws-sidebar-active", `${activeWidth}px`);

        const docked = layout === "canvas" && (sizeMode === "wide" || sizeMode === "medium") && !sidebarCollapsed;
        body.dataset.wsSize = sizeMode;
        body.dataset.wsSidebar = sidebarCollapsed ? "collapsed" : "expanded";
        body.dataset.wsDock = docked ? "true" : "false";
        body.dataset.wsChat = sizeMode === "narrow" ? "drawer" : docked ? "dock" : "overlay";
        body.dataset.wsOverlay = overlayOpen && layout === "canvas" ? "true" : "false";
        body.dataset.wsDrawer = drawer;
    }

    function captureChatScroll(): void {
        const history = byId<HTMLElement>("chatHistory");
        if (!history || !roomId) return;
        chatScroll.set(roomId, {
            top: history.scrollTop,
            bottom: history.scrollHeight - history.scrollTop - history.clientHeight < 48,
        });
    }

    function restoreChatScroll(): void {
        const snapshot = chatScroll.get(roomId);
        const history = byId<HTMLElement>("chatHistory");
        if (!snapshot || !history) return;
        window.requestAnimationFrame(() => {
            if (snapshot.bottom) history.scrollTop = history.scrollHeight;
            else history.scrollTop = snapshot.top;
            scheduleMessageDecoration();
        });
    }

    function updateLayoutFloat(): void {
        const floatButton = byId<HTMLButtonElement>("wsLayoutFloat");
        if (!floatButton) return;
        const capable = canvasCapable(spec, roomRulesetId(activeRoom()));
        floatButton.hidden = !(capable && layout === "conversation");
    }

    function updateLayoutControls(): void {
        const capable = canvasCapable(spec, roomRulesetId(activeRoom()));
        document.querySelectorAll<HTMLElement>("[data-ws-layout]").forEach((button) => {
            const value = button.dataset.wsLayout;
            if (value === "canvas" || value === "conversation") {
                (button as HTMLButtonElement).disabled = value === "canvas" && !capable;
                button.classList.toggle("is-active", value === layout);
                button.setAttribute("aria-pressed", value === layout ? "true" : "false");
            }
        });
        document.querySelectorAll<HTMLElement>("[data-ws-scope]").forEach((button) => {
            const value = button.dataset.wsScope;
            button.classList.toggle("is-active", value === activeScope);
            button.setAttribute("aria-pressed", value === activeScope ? "true" : "false");
        });
        const hint = byId<HTMLElement>("wsLayoutScopeHint");
        if (hint) {
            const messages: Record<Scope, string> = {
                session: "仅本次会话：刷新或重进房间后失效。",
                room: "当前房间：只影响你在这个房间的视图，刷新后保留。",
                default: "我的默认：只影响你自己，房间标记优先于它。",
            };
            hint.textContent = messages[activeScope];
        }
        const widthInput = byId<HTMLInputElement>("wsSidebarWidth");
        if (widthInput && spec) {
            widthInput.min = String(spec.sidebar.min_width);
            widthInput.max = String(spec.sidebar.max_width);
            widthInput.disabled = !spec.sidebar.collapsible && spec.sidebar.position === "none";
            widthInput.value = String(userSidebarWidth > 0 ? userSidebarWidth : spec.sidebar.width);
        }
        const widthValue = byId<HTMLElement>("wsSidebarWidthValue");
        if (widthValue) {
            widthValue.textContent = `${userSidebarWidth > 0 ? userSidebarWidth : (spec?.sidebar.width || 0)}px`;
        }
        const overlayButton = byId<HTMLButtonElement>("wsChatOverlayButton");
        if (overlayButton) overlayButton.hidden = layout !== "canvas";
        const sidebarToggle = byId<HTMLButtonElement>("wsSidebarToggle");
        if (sidebarToggle) sidebarToggle.hidden = layout !== "canvas";
        const resizer = byId<HTMLElement>("wsSidebarResizer");
        if (resizer) resizer.hidden = !(layout === "canvas" && (sizeMode === "wide" || sizeMode === "medium") && !sidebarCollapsed);
    }

    function applyLayout(next: Layout): void {
        const body = bodyElement();
        if (!body) return;
        captureChatScroll();
        layout = next;
        body.dataset.roomLayout = next;

        if (next === "conversation") {
            destroyStage();
            overlayOpen = false;
            drawer = "collapsed";
            panel = "chat";
            body.dataset.roomPanel = "chat";
            updatePanelTabs();
            syncResponsiveState();
            updateLayoutFloat();
            updateLayoutControls();
            restoreChatScroll();
            window.dispatchEvent(new CustomEvent("trpg:workspace-layout", { detail: { layout, roomId } }));
            return;
        }

        applyWorkspaceVariables();
        syncResponsiveState();
        ensureStage();
        refreshTopbar(activeRoom());
        updateLayoutFloat();
        updateLayoutControls();
        updatePanelTabs();
        restoreChatScroll();
        // 聊天消息可能晚于布局就绪才渲染；在几个关键时间点补一次折叠增强。
        [120, 500, 1200, 2500, 5000].forEach((delay) => {
            window.setTimeout(scheduleMessageDecoration, delay);
        });
        window.requestAnimationFrame(() => {
            resizeStage();
            drawStage();
            scheduleMessageDecoration();
        });
        window.dispatchEvent(new CustomEvent("trpg:workspace-layout", { detail: { layout, roomId } }));
    }

    function setLayout(choice: WorkspaceLayoutChoice): void {
        if (!choice || (choice.layout !== "canvas" && choice.layout !== "conversation")) return;
        if (choice.layout === "canvas" && !canvasCapable(spec, roomRulesetId(activeRoom()))) {
            window.showNotification?.("当前规则集没有声明画布布局。", "error");
            return;
        }
        if (roomId) {
            if (choice.scope === "session") {
                sessionLayouts.set(roomId, choice.layout);
            } else if (choice.scope === "room") {
                sessionLayouts.delete(roomId);
                writeStore(roomLayoutKey(), choice.layout);
            } else {
                sessionLayouts.delete(roomId);
                removeStore(roomLayoutKey());
                writeStore(defaultLayoutKey(), choice.layout);
            }
        }
        applyLayout(choice.layout);
        closeLayoutPopover();
    }

    function setPanel(next: Panel): void {
        if (layout !== "canvas") return;
        if (panel === next) {
            if (next === "chat" && bodyElement()?.dataset.wsChat !== "dock") {
                overlayOpen = true;
                syncResponsiveState();
                restoreChatScroll();
            }
            return;
        }
        if (panel === "chat") captureChatScroll();
        panel = next;
        if (next !== "chat" && sidebarCollapsed && (sizeMode === "wide" || sizeMode === "medium")) {
            // 折叠态点击角色/设置 tab 时展开侧栏，避免面板被 48px 图标栏挤没。
            sidebarCollapsed = false;
            sidebarTouched = true;
        }
        const body = bodyElement();
        if (body) body.dataset.roomPanel = next;
        updatePanelTabs();
        if (next === "chat" && (sizeMode === "compact" || sizeMode === "narrow" || sidebarCollapsed)) {
            overlayOpen = true;
        }
        syncResponsiveState();
        updateLayoutControls();
        if (next === "chat") restoreChatScroll();
        else scheduleMessageDecoration();
    }

    function updatePanelTabs(): void {
        document.querySelectorAll<HTMLElement>("[data-ws-panel]").forEach((button) => {
            const active = button.dataset.wsPanel === panel;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
        });
    }

    function toggleSidebar(): void {
        if (layout !== "canvas") return;
        sidebarTouched = true;
        if (sizeMode === "compact" || sizeMode === "narrow") {
            overlayOpen = !overlayOpen;
            if (overlayOpen) {
                panel = "chat";
                const body = bodyElement();
                if (body) body.dataset.roomPanel = "chat";
                updatePanelTabs();
            }
        } else if (sizeMode === "medium" && window.innerWidth < 1200) {
            sidebarCollapsed = !sidebarCollapsed;
            if (!sidebarCollapsed) {
                overlayOpen = false;
            } else {
                overlayOpen = false;
            }
        } else {
            sidebarCollapsed = !sidebarCollapsed;
            overlayOpen = false;
        }
        syncResponsiveState();
        updateLayoutControls();
        if (panel === "chat") restoreChatScroll();
    }

    function toggleChatOverlay(force?: boolean): void {
        if (layout !== "canvas") return;
        overlayOpen = typeof force === "boolean" ? force : !overlayOpen;
        if (overlayOpen) {
            panel = "chat";
            const body = bodyElement();
            if (body) body.dataset.roomPanel = "chat";
            updatePanelTabs();
        }
        syncResponsiveState();
        updateLayoutControls();
        if (overlayOpen) restoreChatScroll();
    }

    function renderAvatars(room: Room | null): void {
        const container = byId<HTMLElement>("wsTopbarPlayers");
        if (!container) return;
        const members = (room?.members || []).filter((member) => member.status !== "removed");
        if (!members.length) {
            container.innerHTML = `<span class="ws-topbar-empty">${escapeHtml(translate("workspace.players.empty", "暂无玩家"))}</span>`;
            return;
        }
        const shown = members.slice(0, 8);
        const html = shown.map((member) => {
            const card = memberCard(member);
            const label = String(card.name || member.username || "玩家");
            const avatar = String((card as { avatar?: string }).avatar || member.avatar || "");
            const initial = escapeHtml(label.slice(0, 1) || "?");
            const image = avatar
                ? `<img src="${escapeHtml(avatar)}" alt="${escapeHtml(label)}" loading="lazy">`
                : `<span class="ws-avatar-initial">${initial}</span>`;
            const online = member.is_online === true ? " is-online" : "";
            return `<span class="ws-avatar${online}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${image}</span>`;
        }).join("");
        const overflow = members.length > shown.length
            ? `<span class="ws-avatar ws-avatar-more" title="还有 ${members.length - shown.length} 位玩家">+${members.length - shown.length}</span>`
            : "";
        container.innerHTML = html + overflow;
    }

    function renderOnline(room: Room | null, presence?: RoomPresencePayload): void {
        const target = byId<HTMLElement>("wsOnlineCount");
        if (!target) return;
        const members = room?.members || [];
        const online = Number.isFinite(Number(presence?.online_count))
            ? Number(presence?.online_count)
            : (Number.isFinite(Number(room?.online_count)) ? Number(room?.online_count) : members.filter((member) => member.is_online === true).length);
        const total = Number.isFinite(Number(presence?.member_count))
            ? Number(presence?.member_count)
            : members.length;
        target.textContent = `在线 ${Math.max(0, online)}/${Math.max(0, total)}`;
    }

    function renderTurnCapsule(): void {
        const capsule = byId<HTMLElement>("wsTurnCapsule");
        if (!capsule) return;
        if (!combatState?.active) {
            capsule.hidden = true;
            capsule.textContent = "";
            return;
        }
        const round = Number(combatState.round || 1);
        const current = String(combatState.current || "");
        const order = combatState.order || [];
        const index = order.findIndex((item) => String(item.name || "") === current);
        const next = index >= 0 && order.length > 1 ? String(order[(index + 1) % order.length]?.name || "") : "";
        capsule.textContent = `第 ${round} 轮${current ? ` · ${current}` : ""}${next ? ` → ${next}` : ""}`;
        capsule.hidden = false;
    }

    function refreshTopbar(room: Room | null): void {
        const scene = byId<HTMLElement>("wsSceneName");
        if (scene) scene.textContent = room?.name || translate("home.not_joined", "未加入房间");
        renderAvatars(room);
        renderOnline(room);
        renderTurnCapsule();
        const dm = isDm();
        document.querySelectorAll<HTMLElement>("[data-ws-dm-only]").forEach((button) => {
            button.hidden = !dm;
        });
    }

    function parseStageState(raw: string): RoomStageState | null {
        if (!raw) return null;
        try {
            const parsed = JSON.parse(raw) as RoomStageState;
            return parsed && typeof parsed === "object" ? parsed : null;
        } catch {
            return null;
        }
    }

    function normalizeGridOffset(raw: unknown): { x: number; y: number } {
        const value = raw && typeof raw === "object" ? (raw as { x?: unknown; y?: unknown }) : {};
        const x = Number(value.x);
        const y = Number(value.y);
        return { x: Number.isFinite(x) ? x : 0, y: Number.isFinite(y) ? y : 0 };
    }

    function illustrationBackground(): string {
        const image = byId<HTMLImageElement>("immersiveStageImage");
        if (image && !image.hidden && image.getAttribute("src")) return image.getAttribute("src") || "";
        return "";
    }

    function buildMemberMarkers(room: Room | null): RoomStageMarker[] {
        const members = (room?.members || []).filter((member) => Boolean(memberCard(member).name || member.username));
        const palette = ["#c8a35a", "#6fa8dc", "#93c47d", "#e06666", "#8e7cc3", "#76a5af", "#f6b26b", "#a64d79"];
        return members.slice(0, 12).map((member, index) => {
            const card = memberCard(member);
            return {
                id: String(member.user_id ?? index),
                label: String(card.name || member.username || `玩家 ${index + 1}`),
                x: (index % 4 - 1.5) * 112,
                y: (Math.floor(index / 4) - 1) * 112,
                color: palette[index % palette.length] || "#c8a35a",
                avatar: String((card as { avatar?: string }).avatar || member.avatar || ""),
                member: true,
            };
        });
    }

    function stageView(): StageView {
        const key = roomId || "default";
        const cached = stageViews.get(key);
        if (cached) return cached;
        const room = activeRoom();
        const stored = parseStageState(readStore(stageStorageKey()));
        const declared = room?.stage && typeof room.stage === "object" ? room.stage : null;
        const markers = Array.isArray(declared?.markers) && declared.markers.length
            ? declared.markers
            : Array.isArray(stored?.markers) && stored.markers.length
                ? stored.markers
                : buildMemberMarkers(room);
        const view: StageView = {
            background: String(declared?.background || stored?.background || illustrationBackground() || ""),
            gridSize: Math.max(16, Math.min(256, Number(declared?.grid_size || stored?.grid_size || 64))),
            gridOffset: normalizeGridOffset(declared?.grid_offset || stored?.grid_offset),
            markers,
        };
        stageViews.set(key, view);
        return view;
    }

    function invalidateStageView(): void {
        if (roomId) stageViews.delete(roomId);
    }

    function camera(): Camera {
        const key = roomId || "default";
        const existing = cameras.get(key);
        if (existing) return existing;
        const next: Camera = { x: 0, y: 0, zoom: 1 };
        cameras.set(key, next);
        return next;
    }

    function loadBackground(url: string): void {
        if (backgroundUrl === url) return;
        backgroundUrl = url;
        backgroundImage = null;
        if (!url) {
            drawStage();
            return;
        }
        const image = new Image();
        image.onload = () => {
            backgroundImage = image;
            drawStage();
        };
        image.onerror = () => {
            backgroundImage = null;
            drawStage();
        };
        image.src = url;
    }

    function drawGrid(
        context: CanvasRenderingContext2D,
        width: number,
        height: number,
        cam: Camera,
        gridSize: number,
        gridOffset: { x: number; y: number }
    ): void {
        const left = (-width / 2 - cam.x) / cam.zoom;
        const right = (width / 2 - cam.x) / cam.zoom;
        const top = (-height / 2 - cam.y) / cam.zoom;
        const bottom = (height / 2 - cam.y) / cam.zoom;
        // 格线以标定原点为基准，而不是固定从世界 (0, 0) 起算。
        const startX = gridOffset.x + Math.floor((left - gridOffset.x) / gridSize) * gridSize;
        const startY = gridOffset.y + Math.floor((top - gridOffset.y) / gridSize) * gridSize;
        const columns = Math.min(400, Math.ceil((right - left) / gridSize) + 2);
        const rowsCount = Math.min(400, Math.ceil((bottom - top) / gridSize) + 2);
        context.lineWidth = 1 / cam.zoom;
        context.strokeStyle = "rgba(255, 255, 255, 0.10)";
        context.beginPath();
        for (let index = 0; index <= columns; index += 1) {
            const x = startX + index * gridSize;
            context.moveTo(x, top);
            context.lineTo(x, bottom);
        }
        for (let index = 0; index <= rowsCount; index += 1) {
            const y = startY + index * gridSize;
            context.moveTo(left, y);
            context.lineTo(right, y);
        }
        context.stroke();
    }

    function drawMarkers(context: CanvasRenderingContext2D, markers: RoomStageMarker[], zoom: number): void {
        markers.forEach((marker) => {
            const x = Number(marker.x) || 0;
            const y = Number(marker.y) || 0;
            const radius = 22;
            context.beginPath();
            context.arc(x, y, radius, 0, Math.PI * 2);
            context.fillStyle = marker.color || "#c8a35a";
            context.fill();
            context.lineWidth = 2 / zoom;
            context.strokeStyle = "rgba(0, 0, 0, 0.45)";
            context.stroke();
            context.fillStyle = "#101010";
            context.font = `700 ${Math.round(13 / Math.max(0.6, zoom))}px "Segoe UI", sans-serif`;
            context.textAlign = "center";
            context.textBaseline = "middle";
            context.fillText(String(marker.label || "?").slice(0, 1), x, y);
            if (zoom >= 0.7) {
                context.font = `600 ${Math.round(11 / zoom)}px "Segoe UI", sans-serif`;
                context.fillStyle = "rgba(245, 240, 226, 0.92)";
                context.fillText(String(marker.label || ""), x, y + radius + 14 / zoom);
            }
        });
    }

    function drawStage(): void {
        if (!canvas || !stageContext) return;
        const host = byId<HTMLElement>("wsStageHost");
        if (!host) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const width = canvas.width / dpr;
        const height = canvas.height / dpr;
        if (width <= 0 || height <= 0) return;
        const cam = camera();
        const view = stageView();
        loadBackground(view.background);

        stageContext.setTransform(dpr, 0, 0, dpr, 0, 0);
        stageContext.clearRect(0, 0, width, height);
        stageContext.fillStyle = "#0d1114";
        stageContext.fillRect(0, 0, width, height);

        stageContext.save();
        stageContext.translate(width / 2 + cam.x, height / 2 + cam.y);
        stageContext.scale(cam.zoom, cam.zoom);

        if (backgroundImage && backgroundImage.complete && backgroundImage.naturalWidth > 0) {
            stageContext.globalAlpha = 0.92;
            stageContext.drawImage(backgroundImage, -backgroundImage.naturalWidth / 2, -backgroundImage.naturalHeight / 2);
            stageContext.globalAlpha = 1;
        }
        if (showGrid) drawGrid(stageContext, width, height, cam, view.gridSize, view.gridOffset);
        // 插件挂载后棋子由插件绘制；核心只保留背景与网格，避免占位标记与 token 重影。
        if (!stagePlugin) drawMarkers(stageContext, view.markers, cam.zoom);
        stageContext.restore();

        const zoomLabel = byId<HTMLElement>("wsZoomLabel");
        if (zoomLabel) zoomLabel.textContent = `${Math.round(cam.zoom * 100)}%`;
    }

    function resizeStage(): void {
        if (!canvas) return;
        const host = byId<HTMLElement>("wsStageHost");
        if (!host) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const width = Math.max(1, Math.round(host.clientWidth));
        const height = Math.max(1, Math.round(host.clientHeight));
        if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
            canvas.width = Math.round(width * dpr);
            canvas.height = Math.round(height * dpr);
        }
        drawStage();
        emitPluginEvent("resize", {
            width,
            height,
            ...camera(),
            gridSize: stageView().gridSize,
            gridOffset: { ...stageView().gridOffset },
        });
    }

    function ensureStage(): void {
        if (layout !== "canvas") return;
        const host = byId<HTMLElement>("wsStageHost");
        if (!host) return;
        if (!canvas) {
            canvas = document.createElement("canvas");
            canvas.className = "ws-stage-canvas";
            canvas.setAttribute("aria-label", "地图画布");
            host.insertBefore(canvas, host.firstChild);
            stageContext = canvas.getContext("2d");
            bindStageEvents();
            if (typeof ResizeObserver !== "undefined") {
                stageObserver = new ResizeObserver(() => resizeStage());
                stageObserver.observe(host);
            }
        }
        resizeStage();
        void ensureStagePlugin();
    }

    function destroyStage(): void {
        destroyStagePlugin();
        if (stageObserver) {
            stageObserver.disconnect();
            stageObserver = null;
        }
        if (canvas) canvas.remove();
        canvas = null;
        stageContext = null;
        backgroundImage = null;
        backgroundUrl = "";
    }

    // ---------------------------------------------------------------- 插件宿主
    function emitPluginEvent(type: string, payload: unknown): void {
        const handlers = pluginHandlers.get(type);
        if (!handlers) return;
        handlers.forEach((handler) => {
            try {
                handler(payload);
            } catch (error) {
                console.warn("workspace plugin handler failed", type, error);
            }
        });
    }

    function clearPluginHandlers(): void {
        pluginHandlers.clear();
    }

    function notifyCamera(): void {
        emitPluginEvent("camera", { ...camera() });
    }

    function destroyStagePlugin(invalidate = true): void {
        if (invalidate) pluginRequestSeq += 1;
        if (stagePlugin?.instance?.destroy) {
            try {
                stagePlugin.instance.destroy();
            } catch (error) {
                console.warn("workspace plugin destroy failed", error);
            }
        }
        if (stagePlugin?.el) stagePlugin.el.remove();
        stagePlugin = null;
        clearPluginHandlers();
    }

    function findAssetByUrl(url: string): boolean {
        return Array.from(document.querySelectorAll<HTMLElement>("[data-plugin-asset]"))
            .some((node) => node.getAttribute("data-plugin-asset") === url);
    }

    function loadStyleAsset(url: string): Promise<void> {
        return new Promise((resolve) => {
            if (!url || findAssetByUrl(url)) {
                resolve();
                return;
            }
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = url;
            link.dataset.pluginAsset = url;
            link.onload = () => resolve();
            // 单个样式失败不阻断脚本挂载；插件自行负责视觉降级。
            link.onerror = () => resolve();
            document.head.appendChild(link);
        });
    }

    function loadScriptAsset(url: string): Promise<void> {
        return new Promise((resolve, reject) => {
            if (!url) {
                resolve();
                return;
            }
            if (findAssetByUrl(url)) {
                resolve();
                return;
            }
            const script = document.createElement("script");
            script.src = url;
            script.async = false;
            script.dataset.pluginAsset = url;
            script.onload = () => resolve();
            script.onerror = () => reject(new Error(`plugin asset failed: ${url}`));
            document.head.appendChild(script);
        });
    }

    function loadPluginAssets(pluginId: string, assets: RoomUiAsset[]): Promise<void> {
        const cached = pluginAssets.get(pluginId);
        if (cached) return cached;
        const task = (async () => {
            for (const asset of assets) {
                if (asset.type === "style") await loadStyleAsset(asset.url);
            }
            for (const asset of assets) {
                if (asset.type !== "style") await loadScriptAsset(asset.url);
            }
        })();
        pluginAssets.set(pluginId, task);
        task.catch(() => pluginAssets.delete(pluginId));
        return task;
    }

    function setBaseStage(view: {
        background?: string;
        gridSize?: number;
        gridOffset?: { x?: number; y?: number };
    }): void {
        const current = stageView();
        let changed = false;
        if (typeof view.background === "string" && view.background !== current.background) {
            current.background = view.background;
            changed = true;
        }
        if (Number.isFinite(view.gridSize)) {
            const grid = clamp(Number(view.gridSize), 16, 256);
            if (grid !== current.gridSize) {
                current.gridSize = grid;
                changed = true;
            }
        }
        if (view.gridOffset !== undefined) {
            const offset = normalizeGridOffset(view.gridOffset);
            if (offset.x !== current.gridOffset.x || offset.y !== current.gridOffset.y) {
                current.gridOffset = offset;
                changed = true;
            }
        }
        if (!changed) return;
        writeStore(stageStorageKey(), JSON.stringify({
            background: current.background,
            grid_size: current.gridSize,
            grid_offset: current.gridOffset,
            markers: current.markers,
        }));
        // 背景地址可能相同（同一 URL 换了内容），清空缓存强制重新加载。
        backgroundUrl = "";
        drawStage();
        emitPluginEvent("stage", {
            gridSize: current.gridSize,
            background: current.background,
            gridOffset: { ...current.gridOffset },
        });
    }

    function stageSize(): { width: number; height: number } {
        const host = byId<HTMLElement>("wsStageHost");
        return { width: host?.clientWidth || 0, height: host?.clientHeight || 0 };
    }

    function createPluginContext(payload: RoomUiPayload, slot: RoomUiSlot): StagePluginContext {
        const room = activeRoom();
        return {
            room: {
                id: roomId,
                name: room?.name,
                ruleset_id: payload.ruleset_id,
                members: room?.members || [],
            },
            ruleset_id: payload.ruleset_id,
            user: {
                id: currentUserId(),
                name: String(window.currentUser?.username || ""),
                role: String(window.currentUser?.role || "USER"),
            },
            permissions: Array.isArray(payload.permissions) ? [...payload.permissions] : [],
            assets: Array.isArray(slot.assets) ? [...slot.assets] : [],
            events: {
                on(type: string, handler: (payload: unknown) => void): () => void {
                    let handlers = pluginHandlers.get(type);
                    if (!handlers) {
                        handlers = new Set();
                        pluginHandlers.set(type, handlers);
                    }
                    handlers.add(handler);
                    return () => {
                        handlers?.delete(handler);
                    };
                },
            },
            getCamera: () => ({ ...camera() }),
            setCamera: (next: StagePluginCamera) => {
                const cam = camera();
                if (Number.isFinite(next.x)) cam.x = next.x;
                if (Number.isFinite(next.y)) cam.y = next.y;
                if (Number.isFinite(next.zoom)) cam.zoom = clamp(next.zoom, 0.25, 3);
                drawStage();
                notifyCamera();
            },
            panBy: (dx: number, dy: number) => {
                if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
                const cam = camera();
                cam.x += dx;
                cam.y += dy;
                drawStage();
                notifyCamera();
            },
            zoomAt: (factor: number, clientX?: number, clientY?: number) => {
                if (!Number.isFinite(factor) || factor <= 0) return;
                zoomStage(camera().zoom * factor, clientX, clientY);
            },
            worldToScreen: (x: number, y: number): StagePluginPoint => {
                const cam = camera();
                const size = stageSize();
                return {
                    x: size.width / 2 + cam.x + x * cam.zoom,
                    y: size.height / 2 + cam.y + y * cam.zoom,
                };
            },
            screenToWorld: (x: number, y: number): StagePluginPoint => {
                const cam = camera();
                const size = stageSize();
                return {
                    x: (x - size.width / 2 - cam.x) / cam.zoom,
                    y: (y - size.height / 2 - cam.y) / cam.zoom,
                };
            },
            getViewport: (): StagePluginViewport => {
                const cam = camera();
                const size = stageSize();
                const view = stageView();
                return {
                    width: size.width,
                    height: size.height,
                    zoom: cam.zoom,
                    gridSize: view.gridSize,
                    gridOffset: { ...view.gridOffset },
                    gridVisible: showGrid,
                    camera: { ...cam },
                };
            },
            getGridSize: () => stageView().gridSize,
            setBaseStage,
            notify: (message: string, type?: string) => {
                window.showNotification?.(message, type || "info");
            },
        };
    }

    async function ensureStagePlugin(): Promise<void> {
        if (layout !== "canvas" || !roomId) {
            destroyStagePlugin();
            return;
        }
        const seq = ++pluginRequestSeq;
        try {
            const response = await TrpgApi.get<ApiResponse<RoomUiPayload>>(
                `/api/rooms/${encodeURIComponent(roomId)}/ui`
            );
            if (seq !== pluginRequestSeq) return;
            const payload = response?.success ? response.data : null;
            const slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
            const slot = slots
                .filter((item) => item.slot === "room.stage.primary")
                .sort((left, right) => Number(right.priority || 0) - Number(left.priority || 0))[0];
            if (!payload || !slot) {
                destroyStagePlugin(false);
                return;
            }
            if (stagePlugin?.pluginId === slot.plugin_id && stagePlugin.el.isConnected) {
                return;
            }
            destroyStagePlugin(false);
            await loadPluginAssets(slot.plugin_id, Array.isArray(slot.assets) ? slot.assets : []);
            if (seq !== pluginRequestSeq || layout !== "canvas") return;
            const host = byId<HTMLElement>("wsStageHost");
            if (!host) return;
            const el = document.createElement("div");
            el.className = "ws-plugin-stage";
            el.dataset.pluginId = slot.plugin_id;
            host.appendChild(el);
            stagePlugin = { pluginId: slot.plugin_id, el, instance: null };
            const module = window.TrpgStagePlugins?.[slot.plugin_id];
            const mount = module?.[slot.mount] || module?.mount;
            if (typeof mount !== "function") {
                throw new Error(`plugin mount missing: ${slot.plugin_id}.${slot.mount}`);
            }
            const instance = mount(el, createPluginContext(payload, slot)) || null;
            stagePlugin = { pluginId: slot.plugin_id, el, instance };
            drawStage();
            emitPluginEvent("combat", combatState);
            emitPluginEvent("members", activeRoom());
            emitPluginEvent("resize", {
                width: stageSize().width,
                height: stageSize().height,
                ...camera(),
            });
        } catch (error) {
            console.warn("workspace stage plugin failed", error);
            if (seq === pluginRequestSeq) {
                destroyStagePlugin(false);
                drawStage();
                if (!pluginNotified.has(roomId)) {
                    pluginNotified.add(roomId);
                    window.showNotification?.("地图插件加载失败，已回退到基础画布。", "error");
                }
            }
        }
    }

    function zoomStage(next: number, clientX?: number, clientY?: number): void {
        const cam = camera();
        const host = byId<HTMLElement>("wsStageHost");
        const clamped = clamp(next, 0.25, 3);
        if (host && Number.isFinite(clientX) && Number.isFinite(clientY)) {
            const rect = host.getBoundingClientRect();
            const width = rect.width;
            const height = rect.height;
            const localX = Number(clientX) - rect.left - width / 2;
            const localY = Number(clientY) - rect.top - height / 2;
            const ratio = clamped / cam.zoom;
            cam.x = localX - (localX - cam.x) * ratio;
            cam.y = localY - (localY - cam.y) * ratio;
        }
        cam.zoom = clamped;
        drawStage();
        notifyCamera();
    }

    function fitStage(): void {
        const host = byId<HTMLElement>("wsStageHost");
        const view = stageView();
        const cam = camera();
        if (!host || view.markers.length === 0) {
            cam.x = 0;
            cam.y = 0;
            cam.zoom = 1;
            drawStage();
            return;
        }
        const xs = view.markers.map((marker) => Number(marker.x) || 0);
        const ys = view.markers.map((marker) => Number(marker.y) || 0);
        const minX = Math.min(...xs) - 96;
        const maxX = Math.max(...xs) + 96;
        const minY = Math.min(...ys) - 96;
        const maxY = Math.max(...ys) + 96;
        const centerX = (minX + maxX) / 2;
        const centerY = (minY + maxY) / 2;
        const zoom = clamp(Math.min(host.clientWidth / (maxX - minX), host.clientHeight / (maxY - minY)), 0.25, 1.6);
        cam.zoom = zoom;
        cam.x = -centerX * zoom;
        cam.y = -centerY * zoom;
        drawStage();
        notifyCamera();
    }

    function bindStageEvents(): void {
        if (!canvas) return;
        canvas.addEventListener("wheel", (event) => {
            event.preventDefault();
            const factor = event.deltaY < 0 ? 1.12 : 0.89;
            zoomStage(camera().zoom * factor, event.clientX, event.clientY);
        }, { passive: false });
        canvas.addEventListener("pointerdown", (event) => {
            if (event.button !== 0) return;
            dragging = true;
            dragStartX = event.clientX;
            dragStartY = event.clientY;
            const cam = camera();
            dragCameraX = cam.x;
            dragCameraY = cam.y;
            canvas?.setPointerCapture(event.pointerId);
            canvas?.classList.add("is-dragging");
        });
        canvas.addEventListener("pointermove", (event) => {
            if (!dragging) return;
            const cam = camera();
            cam.x = dragCameraX + (event.clientX - dragStartX);
            cam.y = dragCameraY + (event.clientY - dragStartY);
            drawStage();
            notifyCamera();
        });
        const endDrag = (event: PointerEvent): void => {
            if (!dragging) return;
            dragging = false;
            canvas?.releasePointerCapture?.(event.pointerId);
            canvas?.classList.remove("is-dragging");
        };
        canvas.addEventListener("pointerup", endDrag);
        canvas.addEventListener("pointercancel", endDrag);
    }

    function ensureLayoutPopover(): HTMLElement {
        const existing = byId<HTMLElement>("wsLayoutPopover");
        if (existing) return existing;
        const popover = document.createElement("div");
        popover.id = "wsLayoutPopover";
        popover.className = "ws-layout-popover";
        popover.hidden = true;
        popover.innerHTML = `
            <strong class="ws-popover-title">主视图布局</strong>
            <div class="ws-layout-choices" role="group" aria-label="主视图布局">
                <button type="button" data-ws-layout="canvas">画布</button>
                <button type="button" data-ws-layout="conversation">聊天</button>
            </div>
            <div class="ws-layout-scopes" role="group" aria-label="布局覆盖范围">
                <button type="button" data-ws-scope="session">仅本次会话</button>
                <button type="button" data-ws-scope="room">当前房间</button>
                <button type="button" data-ws-scope="default">我的默认</button>
            </div>
            <p class="ws-popover-hint" id="wsLayoutPopoverHint">仅本次会话：刷新或重进房间后失效。</p>
        `;
        document.body.appendChild(popover);
        popover.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const layoutButton = target?.closest<HTMLElement>("[data-ws-layout]");
            if (layoutButton?.dataset.wsLayout) {
                setLayout({ layout: layoutButton.dataset.wsLayout as Layout, scope: activeScope });
                updateLayoutControls();
                return;
            }
            const scopeButton = target?.closest<HTMLElement>("[data-ws-scope]");
            if (scopeButton?.dataset.wsScope) {
                activeScope = scopeButton.dataset.wsScope as Scope;
                updateLayoutControls();
                const hint = byId<HTMLElement>("wsLayoutPopoverHint");
                if (hint) hint.textContent = byId<HTMLElement>("wsLayoutScopeHint")?.textContent || "";
            }
        });
        return popover;
    }

    function openLayoutPopover(anchor: HTMLElement): void {
        const popover = ensureLayoutPopover();
        updateLayoutControls();
        const hint = byId<HTMLElement>("wsLayoutPopoverHint");
        if (hint) hint.textContent = byId<HTMLElement>("wsLayoutScopeHint")?.textContent || "";
        popover.hidden = false;
        const rect = anchor.getBoundingClientRect();
        const width = 260;
        const height = popover.offsetHeight || 170;
        const below = rect.bottom + 8;
        const top = below + height > window.innerHeight - 8
            ? Math.max(8, rect.top - height - 8)
            : below;
        popover.style.top = `${top}px`;
        popover.style.left = `${clamp(rect.right - width, 8, Math.max(8, window.innerWidth - width - 8))}px`;
    }

    function closeLayoutPopover(): void {
        const popover = byId<HTMLElement>("wsLayoutPopover");
        if (popover) popover.hidden = true;
    }

    function scheduleMessageDecoration(): void {
        if (layout !== "canvas") return;
        window.requestAnimationFrame(decorateMessages);
    }

    function decorateMessages(): void {
        if (layout !== "canvas") return;
        const history = byId<HTMLElement>("chatHistory");
        if (!history) return;
        history.querySelectorAll<HTMLElement>(".message").forEach((message) => {
            const content = message.querySelector<HTMLElement>(".message-content");
            if (!content) return;
            const thinking = message.classList.contains("thinking") || message.dataset.messageType === "thinking";
            const lineHeight = parseFloat(window.getComputedStyle(content).lineHeight) || 26;
            const container = message.querySelector<HTMLElement>(".message-content-container") || message;
            const longMessage = !thinking && content.scrollHeight > lineHeight * 10 + 8;
            const collapsible = thinking || longMessage;
            if (!collapsible) {
                content.classList.remove("ws-long-message", "ws-thinking-content");
                content.classList.remove("is-expanded");
                container.querySelector<HTMLElement>(":scope > .ws-message-toggle")?.remove();
                return;
            }
            content.classList.toggle("ws-long-message", longMessage);
            content.classList.toggle("ws-thinking-content", thinking);
            let toggle = container.querySelector<HTMLButtonElement>(":scope > .ws-message-toggle");
            if (!toggle) {
                toggle = document.createElement("button");
                toggle.type = "button";
                toggle.className = "ws-message-toggle";
                container.appendChild(toggle);
                toggle.addEventListener("click", () => {
                    content.classList.toggle("is-expanded");
                    toggle!.textContent = content.classList.contains("is-expanded")
                        ? (thinking ? "收起思考" : "收起全文")
                        : (thinking ? "展开思考" : "展开全文");
                    const snapshot = chatScroll.get(roomId);
                    if (snapshot?.bottom) {
                        const chat = byId<HTMLElement>("chatHistory");
                        if (chat) chat.scrollTop = chat.scrollHeight;
                    }
                });
            }
            toggle.textContent = content.classList.contains("is-expanded")
                ? (thinking ? "收起思考" : "收起全文")
                : (thinking ? "展开思考" : "展开全文");
        });
    }

    async function onRoomChanged(room: Room | null): Promise<void> {
        const nextRoomId = String(room?.id || "");
        if (nextRoomId !== roomId) {
            destroyStagePlugin();
            roomId = nextRoomId;
            sidebarTouched = false;
            panel = "chat";
            overlayOpen = false;
            drawer = "collapsed";
            activeScope = "session";
            combatState = null;
            const body = bodyElement();
            if (body) body.dataset.roomPanel = "chat";
            updatePanelTabs();
        }

        if (!room) {
            spec = null;
            applyLayout("conversation");
            refreshTopbar(null);
            return;
        }

        await loadWorkspaces();
        userSidebarWidth = Number(readStore(sidebarWidthKey())) || 0;
        spec = specFor(roomRulesetId(room));
        applyLayout(resolveLayout(room));
        refreshTopbar(room);
    }

    async function refresh(): Promise<void> {
        await onRoomChanged(activeRoom());
    }

    function bindStaticEvents(): void {
        window.addEventListener("trpg:room-changed", (event) => {
            destroyStagePlugin();
            const detail = (event as CustomEvent<{ room?: Room | null }>).detail;
            void onRoomChanged(detail?.room || null);
        });
        window.addEventListener("trpg:room-meta", (event) => {
            const detail = (event as CustomEvent<{ room?: Room | null }>).detail;
            if (layout === "canvas") {
                refreshTopbar(detail?.room || activeRoom());
                emitPluginEvent("members", detail?.room || activeRoom());
            }
        });
        window.addEventListener("trpg:room-members-rendered", () => {
            invalidateStageView();
            if (layout === "canvas") {
                refreshTopbar(activeRoom());
                drawStage();
                emitPluginEvent("members", activeRoom());
            }
        });
        window.addEventListener("trpg:room-presence", (event) => {
            const detail = (event as CustomEvent<RoomPresencePayload>).detail;
            if (layout === "canvas") {
                renderOnline(activeRoom(), detail);
                emitPluginEvent("members", activeRoom());
            }
        });
        window.addEventListener("trpg:combat-changed", (event) => {
            const detail = (event as CustomEvent<{ state?: CombatStatePayload }>).detail;
            combatState = detail?.state || null;
            if (layout === "canvas") {
                renderTurnCapsule();
                emitPluginEvent("combat", combatState);
            }
        });
        window.addEventListener("resize", () => {
            if (layout !== "canvas") return;
            window.clearTimeout(resizeTimer);
            resizeTimer = window.setTimeout(() => {
                syncResponsiveState();
                updateLayoutControls();
                resizeStage();
            }, 120);
        });
        window.addEventListener("keydown", (event) => {
            if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "c") {
                if (layout !== "canvas") return;
                event.preventDefault();
                toggleChatOverlay();
                return;
            }
            if (event.key === "Escape") {
                closeLayoutPopover();
                if (layout !== "canvas") return;
                if (overlayOpen) {
                    overlayOpen = false;
                    sidebarTouched = true;
                    syncResponsiveState();
                }
            }
        });

        byId<HTMLElement>("wsLayoutControl")?.addEventListener("click", (event) => {
            openLayoutPopover(event.currentTarget as HTMLElement);
        });
        byId<HTMLElement>("wsLayoutFloat")?.addEventListener("click", (event) => {
            openLayoutPopover(event.currentTarget as HTMLElement);
        });
        byId<HTMLElement>("wsSettingsButton")?.addEventListener("click", () => setPanel("settings"));
        byId<HTMLElement>("wsFullscreenButton")?.addEventListener("click", () => {
            if (document.fullscreenElement) void document.exitFullscreen();
            else void document.documentElement.requestFullscreen?.();
        });
        byId<HTMLElement>("wsSidebarToggle")?.addEventListener("click", () => toggleSidebar());
        byId<HTMLElement>("wsChatOverlayButton")?.addEventListener("click", () => toggleChatOverlay());
        byId<HTMLElement>("wsChatBackdrop")?.addEventListener("click", () => {
            overlayOpen = false;
            if (sizeMode === "narrow") drawer = "collapsed";
            syncResponsiveState();
        });
        byId<HTMLElement>("wsChatDrawerHandle")?.addEventListener("click", () => {
            drawer = drawer === "collapsed" ? "half" : drawer === "half" ? "full" : "collapsed";
            syncResponsiveState();
        });
        byId<HTMLElement>("wsChatDrawerHandle")?.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            drawer = drawer === "collapsed" ? "half" : drawer === "half" ? "full" : "collapsed";
            syncResponsiveState();
        });
        byId<HTMLElement>("wsSceneButton")?.addEventListener("click", () => {
            if (stagePlugin) {
                emitPluginEvent("tool", { tool: "scene" });
                return;
            }
            window.showNotification?.("场景切换将在地图插件接入后开放。", "info");
        });
        byId<HTMLElement>("wsZoomIn")?.addEventListener("click", () => zoomStage(camera().zoom * 1.15));
        byId<HTMLElement>("wsZoomOut")?.addEventListener("click", () => zoomStage(camera().zoom / 1.15));
        byId<HTMLElement>("wsZoomFit")?.addEventListener("click", fitStage);
        byId<HTMLElement>("wsStageFullscreen")?.addEventListener("click", () => {
            const host = byId<HTMLElement>("wsStageHost");
            if (document.fullscreenElement) void document.exitFullscreen();
            else void host?.requestFullscreen?.();
        });

        activeTool = "select";
        document.querySelectorAll<HTMLElement>("[data-ws-tool]").forEach((button) => {
            button.addEventListener("click", () => {
                const tool = String(button.dataset.wsTool || "");
                if (tool === "grid") {
                    showGrid = !showGrid;
                    button.classList.toggle("is-active", showGrid);
                    drawStage();
                    // 插件层（棋子/坐标）与网格可见性联动，需要主动重绘一次。
                    emitPluginEvent("grid", { visible: showGrid });
                    return;
                }
                if (tool !== "select" && tool !== "measure") {
                    if (stagePlugin && (tool === "token" || tool === "scene")) {
                        emitPluginEvent("tool", { tool });
                        return;
                    }
                    window.showNotification?.("该地图工具将在地图插件接入后开放。", "info");
                    return;
                }
                activeTool = tool;
                document.querySelectorAll<HTMLElement>("[data-ws-tool]").forEach((item) => {
                    if (item.dataset.wsTool !== "grid") item.classList.toggle("is-active", item === button);
                });
            });
        });

        document.querySelectorAll<HTMLElement>("[data-ws-panel]").forEach((button) => {
            button.addEventListener("click", () => {
                const target = button.dataset.wsPanel;
                if (target === "chat" || target === "character" || target === "settings") setPanel(target);
            });
        });

        document.querySelectorAll<HTMLElement>("#wsLayoutSettings [data-ws-layout], #wsSidebarSettings [data-ws-layout]").forEach((button) => {
            button.addEventListener("click", () => {
                const target = button.dataset.wsLayout;
                if (target === "canvas" || target === "conversation") {
                    setLayout({ layout: target, scope: activeScope });
                }
            });
        });
        document.querySelectorAll<HTMLElement>("#wsLayoutScopes [data-ws-scope]").forEach((button) => {
            button.addEventListener("click", () => {
                const target = button.dataset.wsScope;
                if (target === "session" || target === "room" || target === "default") {
                    activeScope = target;
                    updateLayoutControls();
                    const popoverHint = byId<HTMLElement>("wsLayoutPopoverHint");
                    if (popoverHint) popoverHint.textContent = byId<HTMLElement>("wsLayoutScopeHint")?.textContent || "";
                }
            });
        });
        byId<HTMLInputElement>("wsSidebarWidth")?.addEventListener("input", (event) => {
            const input = event.target as HTMLInputElement;
            if (!spec) return;
            userSidebarWidth = clamp(Number(input.value) || spec.sidebar.width, spec.sidebar.min_width, spec.sidebar.max_width);
            writeStore(sidebarWidthKey(), String(userSidebarWidth));
            applyWorkspaceVariables();
            syncResponsiveState();
            updateLayoutControls();
        });

        const resizer = byId<HTMLElement>("wsSidebarResizer");
        if (resizer) {
            resizer.addEventListener("pointerdown", (event) => {
                if (!spec || layout !== "canvas") return;
                event.preventDefault();
                const startX = event.clientX;
                const startWidth = userSidebarWidth > 0 ? userSidebarWidth : spec.sidebar.width;
                resizer.setPointerCapture(event.pointerId);
                const move = (moveEvent: PointerEvent): void => {
                    const next = clamp(startWidth + (startX - moveEvent.clientX), spec!.sidebar.min_width, spec!.sidebar.max_width);
                    userSidebarWidth = next;
                    applyWorkspaceVariables();
                    syncResponsiveState();
                    updateLayoutControls();
                };
                const up = (): void => {
                    writeStore(sidebarWidthKey(), String(userSidebarWidth));
                    resizer.removeEventListener("pointermove", move);
                    resizer.removeEventListener("pointerup", up);
                };
                resizer.addEventListener("pointermove", move);
                resizer.addEventListener("pointerup", up);
            });
        }

        document.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const popover = byId<HTMLElement>("wsLayoutPopover");
            if (!popover || popover.hidden) return;
            if (popover.contains(target)) return;
            if (target?.closest("#wsLayoutControl, #wsLayoutFloat")) return;
            popover.hidden = true;
        });

        const history = byId<HTMLElement>("chatHistory");
        if (history && typeof MutationObserver !== "undefined") {
            const observer = new MutationObserver(() => scheduleMessageDecoration());
            observer.observe(history, { childList: true, subtree: true, characterData: true });
        }
    }

    function init(): void {
        if (initialized) return;
        initialized = true;
        bindStaticEvents();
        void (async () => {
            await loadWorkspaces();
            await onRoomChanged(activeRoom());
        })();
    }

    window.TrpgWorkspace = {
        init,
        refresh: () => void refresh(),
        setLayout,
        toggleSidebar,
        toggleChatOverlay,
        setPanel,
        isCanvas: () => layout === "canvas",
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
