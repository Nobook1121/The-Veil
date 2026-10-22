interface RoomNode {
    filename: string;
    created_at?: string;
    message_count?: number;
    automatic?: boolean;
}

interface RoomNodeList {
    nodes: RoomNode[];
}

interface RoomRecordResponse {
    room?: Room;
    record?: CharacterRuntimeRecord;
}

interface RoomEntrySelection {
    action: "join" | "create" | "invisible";
    createCharacter?: boolean;
    characterCard?: Partial<COC7CharacterCard>;
}

interface RoomInviteTokenResponse {
    invite_token: string;
    can_rotate?: boolean;
}

let currentRoom: Room | null = null;
let previewNodeFilename: string | null = null;
let roomEntryCharacterModal: BootstrapModalInstance | null = null;
let roomEntrySelectionResolver: ((result: RoomEntrySelection | null) => void) | null = null;
let roomEntrySelectionSettled = false;
let roomEntrySelectionAction: RoomEntrySelection["action"] = "join";
let roomEntrySelectionRoomId: string | null = null;
// 目标房间的房规技能基础值上限（进入房间选择角色卡时用于校验）。
let roomEntrySkillBases: Record<string, number> = {};
const roomActionLocks = new Set<string>();

function initRoomManagement(): void {
    window.currentRoom = currentRoom;
    const roomEntryModalElement = document.getElementById("roomEntryCharacterModal");
    roomEntryCharacterModal = roomEntryModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(roomEntryModalElement) : null;
    roomEntryModalElement?.addEventListener("hidden.bs.modal", () => {
        settleRoomEntrySelection(null);
    });

    document.getElementById("createSave")?.addEventListener("click", () => {
        void openCreateRoomModal();
    });
    document.getElementById("confirmCreateSave")?.addEventListener("click", () => {
        void createRoom();
    });
    document.getElementById("joinRoom")?.addEventListener("click", () => {
        void joinRoomByCode();
    });
    document.getElementById("backToSaveList")?.addEventListener("click", showRoomListView);
    document.getElementById("deleteSave")?.addEventListener("click", () => {
        void deleteCurrentRoom();
    });
    document.getElementById("archiveRoom")?.addEventListener("click", () => {
        void archiveCurrentRoom();
    });
    document.getElementById("editHouseRules")?.addEventListener("click", () => {
        void openHouseRulesModal();
    });
    document.getElementById("switchScenario")?.addEventListener("click", () => {
        void openSwitchScenarioModal();
    });
    document.getElementById("saveHouseRules")?.addEventListener("click", () => {
        void saveHouseRules();
    });
    document.getElementById("openRoomSkillBaseSettings")?.addEventListener("click", () => {
        window.COC7CharacterSheet?.openSkillBaseSettings?.("room");
    });
    document.getElementById("startScenario")?.addEventListener("click", () => {
        void window.startScenario?.();
    });
    document.getElementById("createSaveNode")?.addEventListener("click", () => {
        void createRoomNode();
    });
    document.getElementById("loadNodeFromPreviewBtn")?.addEventListener("click", () => {
        if (!previewNodeFilename) return;
        bootstrap.Modal.getInstance(document.getElementById("saveNodePreviewModal"))?.hide();
        void restoreRoomNode(previewNodeFilename);
    });
    document.getElementById("confirmRoomCharacterBind")?.addEventListener("click", () => {
        void confirmRoomCharacterBinding();
    });
    document.getElementById("confirmRoomEntryCharacter")?.addEventListener("click", () => {
        void confirmRoomEntryCharacterSelection();
    });
    document.getElementById("roomEntryInvisible")?.addEventListener("click", () => {
        settleRoomEntrySelection({ action: "invisible" });
        roomEntryCharacterModal?.hide();
    });
    document.getElementById("roomEntryCreateCharacter")?.addEventListener("click", () => {
        createCharacterFromRoomEntry();
    });

    // 点击右侧成员栏中的成员，在该成员旁弹出资料卡（参考 Discord）
    document.addEventListener("click", (event) => {
        const target = event.target as HTMLElement | null;
        const item = target?.closest<HTMLElement>("#roomMemberList .home-room-member");
        if (!item || !currentRoom) return;
        const userId = item.dataset.userId;
        if (!userId) return;
        const member = (currentRoom.members || []).find((entry) => String(entry.user_id) === String(userId));
        if (!member) return;
        AuthModule.openMemberProfileCard(currentRoom, member, item);
    });

    // 点击房间码即可复制（房间列表卡片与房间详情都使用同一套委托处理）
    document.addEventListener("click", (event) => {
        const target = event.target as HTMLElement | null;
        const trigger = target?.closest<HTMLElement>("[data-copy-room-code], #copyRoomDetailCode");
        if (!trigger) return;
        const code = trigger.dataset.copyRoomCode
            || document.getElementById("roomDetailCode")?.textContent
            || "";
        void copyRoomCodeToClipboard(code);
    });

    // 房间分享：统一的「邀请玩家」入口（弹窗内展示链接 + 二维码，并可复制/刷新）。
    document.getElementById("roomInvitePlayersBtn")?.addEventListener("click", () => {
        void openRoomInviteModal();
    });
    document.getElementById("roomInviteCopyBtn")?.addEventListener("click", () => {
        void copyRoomInviteLink();
    });
    document.getElementById("roomInviteRotateBtn")?.addEventListener("click", () => {
        void rotateRoomInviteToken();
    });

    populateCharacterSelectors();
    window.loadRoomsList = loadRoomsList;
    window.addEventListener("trpg:locale-changed", () => renderRoomMemberList(currentRoom));
    void loadRoomsList();
}

function getLastRoomStorageKey(): string {
    return `trpg_last_room_${window.currentUser?.user_id}`;
}

function getLastRoomCharacterStorageKey(roomId?: string): string {
    const scopedRoomId = roomId || TrpgCookies.get(getLastRoomStorageKey()) || "";
    return `trpg_last_room_character_${window.currentUser?.user_id}_${scopedRoomId}`;
}

function isElevatedUser(): boolean {
    return ["ADMIN", "OWNER"].includes(window.currentUser?.role || "");
}

/** 房规中的技能基础值覆盖表（未配置时为空对象）。 */
function roomSkillBaseOverrides(room: Room | null | undefined): Record<string, number> {
    const bases = room?.house_rules?.skill_bases;
    return bases && typeof bases === "object" ? bases : {};
}

/**
 * 校验角色卡技能基础值是否超过房规上限：超上限时提示，用户确认后返回按上限裁剪后的卡片。
 * 返回 null 表示用户放弃使用该角色卡；无上限或无超限时原样返回。
 */
async function enforceSkillBaseLimits(
    characterCard: Partial<COC7CharacterCard> | null | undefined,
    overrides: Record<string, number>,
    actionLabel: string
): Promise<Partial<COC7CharacterCard> | null> {
    if (!characterCard) return null;
    if (!Object.keys(overrides).length) return characterCard;
    const card = characterCard as COC7CharacterCard;
    const overflows = window.COC7CharacterSheet?.collectCardSkillBaseOverflows?.(card, overrides) || [];
    if (!overflows.length) return characterCard;
    const confirmed = await window.TrpgDialog?.confirm({
        title: actionLabel,
        message: `以下技能基础值超过房间规则上限：${overflows.join("、")}\n继续将自动把超出部分降低为房间规则上限。`,
        confirmText: "继续",
    });
    if (!confirmed) return null;
    return window.COC7CharacterSheet?.clampCardSkillBases?.(card, overrides) || characterCard;
}

function getCharacterCards(): COC7CharacterCard[] {
    return window.COC7CharacterSheet?.listCharacterCards?.() || [];
}

function getRoomEntryCards(): COC7CharacterCard[] {
    const cards = getCharacterCards();
    if (isElevatedUser()) return cards;
    const user = window.currentUser;
    if (!user) return [];
    return cards.filter((card) => {
        const playerId = String(card.playerId || "");
        return playerId === String(user.user_id) || playerId === user.username;
    });
}

function isActiveRoomMember(member: RoomMember): boolean {
    return member.is_active !== false && member.status !== "removed";
}

function activeRoomMembers(room: Room): RoomMember[] {
    return (room.members || []).filter(isActiveRoomMember);
}

function populateCharacterSelect(selectId: string): void {
    const select = document.getElementById(selectId) as HTMLSelectElement | null;
    if (!select) return;
    const currentValue = select.value;
    const cards = getCharacterCards();
    select.innerHTML = window.TrpgTemplates.render("select-placeholder-option", { label: "请选择角色卡" });
    cards.forEach((card) => {
        const option = document.createElement("option");
        option.value = card.id;
        option.textContent = card.name;
        select.appendChild(option);
    });
    if (cards.some((card) => card.id === currentValue)) select.value = currentValue;
}

function populateCharacterSelectors(): void {
    populateCharacterSelect("roomBindCharacterSelect");
}

function populateRoomEntryCharacterSelect(): void {
    const select = document.getElementById("roomEntryCharacterSelect") as HTMLSelectElement | null;
    const emptyMessage = document.getElementById("roomEntryCharacterEmpty") as HTMLElement | null;
    const confirmButton = document.getElementById("confirmRoomEntryCharacter") as HTMLButtonElement | null;
    if (!select) return;

    const currentValue = select.value;
    const cards = getRoomEntryCards();
    select.innerHTML = window.TrpgTemplates.render("select-placeholder-option", { label: "请选择角色卡" });
    cards.forEach((card) => {
        const option = document.createElement("option");
        option.value = card.id;
        option.textContent = card.name;
        select.appendChild(option);
    });
    const rememberedCardId = roomEntrySelectionRoomId ? TrpgCookies.get(getLastRoomCharacterStorageKey(roomEntrySelectionRoomId)) : "";
    const nextValue = cards.some((card) => card.id === rememberedCardId)
        ? rememberedCardId
        : (cards.some((card) => card.id === currentValue) ? currentValue : "");
    if (nextValue) select.value = nextValue;
    if (emptyMessage) emptyMessage.hidden = cards.length > 0;
    if (confirmButton) confirmButton.disabled = cards.length === 0;
    const invisibleButton = document.getElementById("roomEntryInvisible") as HTMLButtonElement | null;
    if (invisibleButton) invisibleButton.hidden = !isElevatedUser() || roomEntrySelectionAction === "create";
}

async function promptRoomEntryCharacterSelection(
    action: RoomEntrySelection["action"],
    roomId: string | null = null,
    skillBases: Record<string, number> = {}
): Promise<RoomEntrySelection | null> {
    roomEntrySelectionAction = action;
    roomEntrySelectionRoomId = roomId;
    roomEntrySkillBases = skillBases;
    // Character management loads cards asynchronously during application
    // startup. Refreshing the room list can happen before that request ends.
    await window.reloadCharacterManagement?.();
    populateRoomEntryCharacterSelect();
    roomEntrySelectionSettled = false;
    setText("roomEntryCharacterMessage", action === "create" ? "创建房间前请选择要使用的角色卡。" : "进入房间前请选择要使用的角色卡。");
    if (!roomEntryCharacterModal) return Promise.resolve(null);

    return new Promise((resolve) => {
        roomEntrySelectionResolver = resolve;
        roomEntryCharacterModal?.show();
    });
}

function settleRoomEntrySelection(result: RoomEntrySelection | null): void {
    if (roomEntrySelectionSettled || !roomEntrySelectionResolver) return;
    roomEntrySelectionSettled = true;
    const resolve = roomEntrySelectionResolver;
    roomEntrySelectionResolver = null;
    resolve(result);
}

async function confirmRoomEntryCharacterSelection(): Promise<void> {
    const selectedCard = getSelectedCharacterCardSnapshot("roomEntryCharacterSelect");
    if (!selectedCard) {
        showNotification("请选择要使用的角色卡", "error");
        return;
    }
    // 超出房规上限时提示；用户取消则停留在选择弹窗，可改选其他角色卡。
    const characterCard = await enforceSkillBaseLimits(selectedCard, roomEntrySkillBases, "该角色卡的基础值超过本房间规则上限。");
    if (!characterCard) return;
    settleRoomEntrySelection({ action: roomEntrySelectionAction, characterCard });
    if (roomEntrySelectionRoomId) {
        syncRoomCharacterSelection(roomEntrySelectionRoomId, characterCard);
    }
    roomEntryCharacterModal?.hide();
}

function createCharacterFromRoomEntry(): void {
    settleRoomEntrySelection({ action: roomEntrySelectionAction, createCharacter: true });
    roomEntryCharacterModal?.hide();
    window.switchMainTab?.("characters");
    document.getElementById("createCharacter")?.click();
}

function getSelectedCharacterCardSnapshot(selectId: string): Partial<COC7CharacterCard> | null {
    const cardId = (document.getElementById(selectId) as HTMLSelectElement | null)?.value;
    if (!cardId) return null;
    return window.COC7CharacterSheet?.getCharacterCardSnapshot?.(cardId) || null;
}

function syncRoomCharacterSelection(roomId: string, characterCard: Partial<COC7CharacterCard> | null): void {
    if (!window.currentUser?.user_id || !roomId) return;
    const storageKey = getLastRoomCharacterStorageKey(roomId);
    const characterId = String(characterCard?.id || "").trim();
    if (!characterId) {
        TrpgCookies.remove(storageKey);
        return;
    }
    TrpgCookies.set(storageKey, characterId);
}

async function openCreateRoomModal(): Promise<void> {
    populateCharacterSelectors();
    const scenarioSelect = document.getElementById("roomScenarioSelect") as HTMLSelectElement | null;
    if (scenarioSelect) {
        scenarioSelect.innerHTML = window.TrpgTemplates.render("select-placeholder-option", { label: "请选择剧本" });
        try {
            const data = await TrpgApi.get<ApiResponse<Scenario[]>>("/api/scenarios");
            if (data.success && data.data) {
                data.data.forEach((scenario) => {
                    const option = document.createElement("option");
                    option.value = String(scenario.id);
                    option.textContent = scenario.title;
                    option.dataset.title = scenario.title;
                    scenarioSelect.appendChild(option);
                });
            }
        } catch (error) {
            console.error("加载剧本列表失败:", error);
        }
    }

    const roomNameInput = document.getElementById("saveName") as HTMLInputElement | null;
    if (roomNameInput) roomNameInput.value = "";
    // 每次打开创建窗口都重置为默认的私人房间
    const visibilitySelect = document.getElementById("roomVisibilitySelect") as HTMLSelectElement | null;
    if (visibilitySelect) visibilitySelect.value = "private";
    const modalElement = document.getElementById("createSaveModal");
    if (modalElement) new bootstrap.Modal(modalElement).show();
}

async function createRoom(): Promise<void> {
    return runRoomAction("create-room", "confirmCreateSave", async () => {
        await createRoomUnlocked();
    });
}

async function createRoomUnlocked(): Promise<void> {
    const roomName = (document.getElementById("saveName") as HTMLInputElement | null)?.value.trim() || "";
    const scenarioSelect = document.getElementById("roomScenarioSelect") as HTMLSelectElement | null;
    const scenarioId = scenarioSelect?.value || "";
    const selectedOption = scenarioSelect?.options[scenarioSelect.selectedIndex];
    const scenarioTitle = selectedOption?.dataset.title || "";

    if (!roomName) {
        showNotification("请输入房间名称", "error");
        return;
    }
    if (!scenarioId) {
        showNotification("请选择剧本", "error");
        return;
    }

    bootstrap.Modal.getInstance(document.getElementById("createSaveModal"))?.hide();
    const roomEntrySelection = await promptRoomEntryCharacterSelection("create");
    if (!roomEntrySelection || roomEntrySelection.createCharacter || roomEntrySelection.action === "invisible") return;

    try {
        const data = await TrpgApi.post<ApiResponse<Room>>("/api/rooms", {
            name: roomName,
            scenario_id: Number.parseInt(scenarioId, 10),
            scenario_title: scenarioTitle,
            visibility: (document.getElementById("roomVisibilitySelect") as HTMLSelectElement | null)?.value || "private",
            character_card: roomEntrySelection.characterCard,
        });
        if (!data.success || !data.data) {
            showNotification(`创建房间失败：${data.message || data.error || "未知错误"}`, "error");
            return;
        }

        bootstrap.Modal.getInstance(document.getElementById("createSaveModal"))?.hide();
        await enterRoom(data.data);
        await loadRoomsList();
        showNotification(`房间已创建：${data.data.room_code || data.data.code || data.data.id}`, "success");
    } catch (error) {
        showNotification(`创建房间失败：${roomErrorMessage(error)}`, "error");
    }
}

async function joinRoomByCode(): Promise<void> {
    return runRoomAction("join-room", "joinRoom", async () => {
        await joinRoomByCodeUnlocked();
    });
}

async function joinRoomByCodeUnlocked(): Promise<void> {
    const roomCode = (document.getElementById("roomCodeInput") as HTMLInputElement | null)?.value.trim() || "";
    if (!roomCode) {
        showNotification("请输入房间码", "error");
        return;
    }

    const roomEntrySelection = await promptRoomEntryCharacterSelection("join");
    if (!roomEntrySelection) return;
    if (roomEntrySelection.createCharacter || roomEntrySelection.action === "create") return;

    try {
        const endpoint = roomEntrySelection.action === "invisible" ? "/api/rooms/spectate" : "/api/rooms/join";
        const data = await TrpgApi.post<ApiResponse<Room>>(endpoint, {
            room_code: roomCode,
            ...(roomEntrySelection.action === "invisible" ? {} : { character_card: roomEntrySelection.characterCard }),
        });
        if (!data.success || !data.data) {
            showNotification(`加入房间失败：${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        await enterRoom(data.data);
        await loadRoomsList();
        showNotification(roomEntrySelection.action === "invisible" ? "已隐身进入房间" : "已加入房间", "success");
    } catch (error) {
        showNotification(`加入房间失败：${roomErrorMessage(error)}`, "error");
    }
}

async function loadRoomsList(): Promise<void> {
    try {
        const data = await TrpgApi.get<ApiResponse<Room[]>>("/api/rooms");
        if (data.success && data.data) {
            // Archived rooms are kept in place (still returned by /api/rooms with
            // archived=true), so only add legacy archive-only entries that are not
            // already present to avoid duplicates.
            const roomIds = new Set(data.data.map((room) => room.id));
            const archives = await TrpgApi.get<ApiResponse<Room[]>>("/api/room-archives");
            const archived = (archives.success ? archives.data || [] : [])
                .filter((item) => !roomIds.has(item.id))
                .map((item) => ({ ...item, archived: true, invisible_view: true, members: [] } as Room));
            renderRoomsList([...data.data, ...archived]);
        }
    } catch (error) {
        console.error("加载房间列表失败:", error);
    }
}

function renderRoomsList(rooms: Room[]): void {
    const roomListContainer = document.getElementById("saveList");
    if (!roomListContainer) return;

    if (rooms.length === 0) {
        roomListContainer.innerHTML = window.TrpgTemplates.render("room-empty-list");
        return;
    }

    roomListContainer.innerHTML = window.TrpgTemplates.render("room-card-grid", {
        cardsHtml: rooms.map((room) => renderRoomCard(room)).join(""),
    });

    roomListContainer.querySelectorAll<HTMLButtonElement>(".view-room-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            const card = button.closest<HTMLElement>(".save-card");
            const roomId = card?.dataset.roomId;
            if (roomId) await openRoomDetail(roomId);
        });
    });
}

function renderRoomCard(room: Room): string {
    const isActive = currentRoom?.id === room.id;
    const members = activeRoomMembers(room).map((member) => member.username).join(", ") || "-";
    return window.TrpgTemplates.render("room-card", {
        roomId: room.id,
        activeClass: isActive ? "border-primary border-2 shadow-lg" : "",
        activeHeaderHtml: isActive ? window.TrpgTemplates.render("room-active-header") : "",
        archivedBadgeHtml: room.archived || room.archived_at ? window.TrpgTemplates.render("room-archived-badge", { archivedAt: room.archived_at || "" }) : "",
        name: room.name,
        visibilityBadgeHtml: roomVisibilityBadge(room),
        roomCode: room.room_code || room.code || "-",
        scenarioTitle: room.scenario_title || "未知",
        members,
        actionLabel: isActive ? "管理房间" : "进入房间",
    });
}

async function openRoomDetail(roomId: string): Promise<void> {
    try {
        // Prefer the live room so that archived rooms (kept in place) retain all
        // non-AI features (dice, history, tools). Fall back to the archive
        // snapshot only for legacy rooms that were moved/removed.
        const data = await TrpgApi.get<ApiResponse<Room>>(`/api/rooms/${roomId}`);
        if (!data.success || !data.data) {
            const archiveResponse = await TrpgApi.get<ApiResponse<Room>>(`/api/room-archives/${encodeURIComponent(roomId)}`);
            if (archiveResponse.success && archiveResponse.data) {
                const legacyArchive = { ...archiveResponse.data, archived: true, invisible_view: true } as Room;
                if (archiveResponse.data.archived_at) legacyArchive.archived_at = archiveResponse.data.archived_at;
                await enterRoom(legacyArchive);
                return;
            }
            showNotification(`加载房间失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }

        // A refresh restores the room from its cookie. If the server already
        // has an active member record with a bound card, re-enter directly.
        // Prompting here races character-card loading and can incorrectly
        // present an empty selector, while also rebinding the card.
        const currentUserId = String(window.currentUser?.user_id || "");
        const existingMember = currentUserId
            ? activeRoomMembers(data.data).find((member) => String(member.user_id) === currentUserId)
            : undefined;
        if (existingMember?.character_card || (isElevatedUser() && !existingMember)) {
            await enterRoom(data.data);
            return;
        }

        const roomEntrySelection = await promptRoomEntryCharacterSelection("join", roomId, roomSkillBaseOverrides(data.data));
        if (!roomEntrySelection || roomEntrySelection.createCharacter || roomEntrySelection.action === "create") return;

        if (roomEntrySelection.action === "invisible") {
            const spectateResponse = await TrpgApi.get<ApiResponse<Room>>(`/api/rooms/${encodeURIComponent(roomId)}/spectate`);
            if (!spectateResponse.success || !spectateResponse.data) {
                showNotification(spectateResponse.message || spectateResponse.error || "隐身进入房间失败", "error");
                return;
            }
            await enterRoom(spectateResponse.data);
            showNotification("已隐身进入房间", "success");
            return;
        }

        const userId = currentUserId;
        if (!userId) return;

        if (!existingMember) {
            const joinResponse = await TrpgApi.post<ApiResponse<Room>>("/api/rooms/join", {
                room_code: data.data.room_code || data.data.code,
                character_card: roomEntrySelection.characterCard,
            });
            if (!joinResponse.success || !joinResponse.data) {
                showNotification(joinResponse.message || joinResponse.error || "加入房间失败", "error");
                return;
            }
            await enterRoom(joinResponse.data);
            showNotification("已加入房间", "success");
            return;
        }

        const bindResponse = await TrpgApi.put<ApiResponse<Room>>(
            `/api/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(userId)}/character`,
            { character_card: roomEntrySelection.characterCard },
        );
        if (!bindResponse.success || !bindResponse.data) {
            showNotification(bindResponse.message || bindResponse.error || "绑定角色卡失败", "error");
            return;
        }
        await enterRoom(bindResponse.data);
    } catch (error) {
        showNotification(`加载房间失败: ${roomErrorMessage(error)}`, "error");
    }
}

async function enterRoom(room: Room): Promise<void> {
    if (currentRoom?.id) window.leaveSocketRoom?.(currentRoom.id);

    currentRoom = room;
    window.currentRoom = currentRoom;
    // 房间规则优先级最高：切换房间后重建技能基础值，使新房规即时生效。
    void window.COC7CharacterSheet?.reloadSkillBases?.();
    window.restoreThinkingState?.();
    window.resumePendingAIRequest?.();
    const invisibleView = room.invisible_view === true;
    if (invisibleView) TrpgCookies.remove(getLastRoomStorageKey());
    else TrpgCookies.set(getLastRoomStorageKey(), room.id);

    showRoomDetailView();
    updateRoomDetail(room);
    updateHomeRoomMeta();
    const messages = Array.isArray(room.messages)
        ? room.messages
        : window.getCurrentChatMessages?.() || [];
    window.renderChatMessages?.(messages);
    setText("homeRoomTitle", room.name);
    setText("homeRoomOnlineCount", formatRoomOnlineCount(room));
    const selfMember = activeRoomMembers(room).find((member) => String(member.user_id) === String(window.currentUser?.user_id));
    if (selfMember?.character_card) {
        syncRoomCharacterSelection(room.id, selfMember.character_card);
    }
    window.setChatReadOnly?.(invisibleView);
    applyInvisibleRoomView(invisibleView);
    if (invisibleView) {
        renderRoomNodeList([]);
        return;
    }
    await enforceSelfCardSkillBaseLimit(room);
    window.joinSocketRoom?.(room.id);
    await loadRoomNodes();
}

/**
 * 进入房间后兜底校验当前用户角色卡：超过房规上限时提示，用户确认后按上限裁剪并回写服务器。
 * 覆盖「按房间码加入」这类进入前拿不到房规的场景。
 */
async function enforceSelfCardSkillBaseLimit(room: Room): Promise<void> {
    const overrides = roomSkillBaseOverrides(room);
    if (!Object.keys(overrides).length) return;
    const userId = String(window.currentUser?.user_id || "");
    if (!userId) return;
    const card = (room.members || []).find((member) => String(member.user_id) === userId)?.character_card;
    if (!card) return;
    const clamped = await enforceSkillBaseLimits(card, overrides, "该角色卡的基础值超过本房间规则上限。");
    if (!clamped || clamped === card) return;
    const response = await TrpgApi.put<ApiResponse<Room>>(
        `/api/rooms/${encodeURIComponent(room.id)}/members/${encodeURIComponent(userId)}/character`,
        { character_card: clamped }
    );
    if (!response.success || !response.data) return;
    currentRoom = response.data;
    window.currentRoom = currentRoom;
    updateRoomDetail(currentRoom);
}

function applyInvisibleRoomView(invisible: boolean): void {
    document.querySelectorAll<HTMLElement>("[data-room-sensitive]").forEach((element) => {
        element.hidden = invisible;
    });
    const onlineCount = document.getElementById("homeRoomOnlineCount") as HTMLElement | null;
    if (onlineCount) onlineCount.hidden = invisible;
    const memberPanel = document.getElementById("homeRoomMembers") as HTMLElement | null;
    if (memberPanel) memberPanel.hidden = invisible;
    updateHomeRoomMeta();
    const deleteButton = document.getElementById("deleteSave") as HTMLButtonElement | null;
    const createNodeButton = document.getElementById("createSaveNode") as HTMLButtonElement | null;
    if (deleteButton) deleteButton.hidden = invisible;
    if (createNodeButton) createNodeButton.hidden = invisible;
}

function showRoomListView(): void {
    setDisplay("save-list-view", "block");
    setDisplay("save-detail-view", "none");
    void loadRoomsList();
}

function showRoomDetailView(): void {
    setDisplay("save-list-view", "none");
    setDisplay("save-detail-view", "block");
}

function updateRoomDetail(room: Room): void {
    setText("saveDetailTitle", room.name);
    setText("saveCreatedAt", room.created_at || "-");
    setText("saveScenarioTitle", room.scenario_title || "-");
    setText("saveParticipants", activeRoomMembers(room).map((member) => member.username).join(", ") || "-");
    setText("roomDetailCode", room.room_code || room.code || "-");
    const visibilityBadge = document.getElementById("roomDetailVisibility") as HTMLElement | null;
    if (visibilityBadge) {
        const isPublic = roomVisibility(room) === "public";
        visibilityBadge.textContent = roomVisibilityLabel(room);
        visibilityBadge.className = `badge ms-1 ${isPublic ? "bg-info text-dark" : "bg-secondary"}`;
        visibilityBadge.style.display = "inline-block";
    }
    setText("homeRoomOnlineCount", formatRoomOnlineCount(room));
    renderRoomCharacterBindings(room);
    renderRoomMemberList(room);
    updateStartScenarioButton(room);
    const archiveButton = document.getElementById("archiveRoom") as HTMLButtonElement | null;
    if (archiveButton) {
        archiveButton.hidden = Boolean(room.archived || !room.completed_at || !canManageRoom(room));
        archiveButton.textContent = "归档房间";
    }
    const houseRulesButton = document.getElementById("editHouseRules") as HTMLButtonElement | null;
    if (houseRulesButton) houseRulesButton.hidden = Boolean(room.archived) || !canManageRoom(room);
    // 「切换剧本」仅管理员/房主可见，且已归档房间不再允许换绑。
    const switchScenarioButton = document.getElementById("switchScenario") as HTMLButtonElement | null;
    if (switchScenarioButton) switchScenarioButton.hidden = Boolean(room.archived) || !canManageRoom(room);
    const deleteButton = document.getElementById("deleteSave") as HTMLButtonElement | null;
    if (deleteButton) deleteButton.hidden = Boolean(room.archived);
    const archivedBadge = document.getElementById("roomDetailArchivedBadge") as HTMLElement | null;
    if (archivedBadge) archivedBadge.style.display = room.archived ? "inline-block" : "none";
    // 房间内嵌控制台：仅显示本房间日志，房主/管理员可执行房间域命令。
    window.openRoomConsole?.(room.id, {
        canExecute: canManageRoom(room),
        archived: Boolean(room.archived),
        invisibleView: room.invisible_view === true,
    });
}

window.refreshRoomArchiveUi = (): void => {
    if (currentRoom) updateRoomDetail(currentRoom);
};

/**
 * 收到「房间成员变化」事件时重新拉取当前房间，刷新在线状态、角色配色与成员列表。
 * 只更新成员相关 UI，不重绘聊天记录，避免打断正在阅读的消息。
 */
async function refreshCurrentRoomMembers(): Promise<void> {
    const room = currentRoom;
    if (!room?.id || room.invisible_view) return;
    try {
        const response = await TrpgApi.get<ApiResponse<Room>>(`/api/rooms/${encodeURIComponent(room.id)}`);
        if (!response.success || !response.data) return;
        const nextRoom: Room = { ...response.data, messages: room.messages ?? [] };
        currentRoom = nextRoom;
        window.currentRoom = nextRoom;
        updateRoomDetail(nextRoom);
        setText("homeRoomOnlineCount", formatRoomOnlineCount(nextRoom));
        renderRoomMemberList(nextRoom);
    } catch {
        /* 网络异常时静默忽略，等待下一次事件刷新 */
    }
}

window.refreshCurrentRoomMembers = refreshCurrentRoomMembers;
window.refreshRoomNodes = loadRoomNodes;

function canManageRoom(room: Room): boolean {
    if (isElevatedUser()) return true;
    const self = activeRoomMembers(room).find((member) => String(member.user_id) === String(window.currentUser?.user_id));
    return Boolean(self && (self.room_role === "owner" || self.room_role === "admin"));
}

async function archiveCurrentRoom(): Promise<void> {
    if (!currentRoom?.id || currentRoom.archived) return;
    const confirmed = await window.TrpgDialog?.confirm({
        title: "归档房间",
        message: "归档后房间将关闭 AI，但仍保留在房间管理列表中，可继续使用骰娘等工具并查看历史记录。",
        confirmText: "归档",
    });
    if (!confirmed) return;
    try {
        const response = await TrpgApi.post<ApiResponse<{ archived_at?: string }>>(`/api/rooms/${encodeURIComponent(currentRoom.id)}/archive`, {});
        if (!response.success) throw new Error(response.message || response.error || "归档失败");
        currentRoom.archived = true;
        const archivedAt = response.data?.archived_at;
        if (archivedAt) currentRoom.archived_at = archivedAt;
        currentRoom.completed_at = currentRoom.completed_at || currentRoom.archived_at || new Date().toISOString();
        updateRoomDetail(currentRoom);
        applyInvisibleRoomView(false);
        await loadRoomsList();
        showNotification("房间已归档，仍停留在房间内；AI 已关闭，骰娘等工具可继续使用", "success");
    } catch (error) {
        showNotification(`归档失败：${roomErrorMessage(error)}`, "error");
    }
}

/**
 * 管理员/房主强制把房间切换到另一个剧本（或同一剧本的最新版本）。
 * 高风险：默认会重置剧情进度，因此弹窗内先给出明确的文字风险提示并要求勾选确认。
 * 若选择的是同一剧本（例如升级到最新版本），可勾选「继承剧情进度」保留线索/物品/任务等。
 */
async function openSwitchScenarioModal(): Promise<void> {
    const room = currentRoom;
    if (!room?.id) return;
    let scenarios: Scenario[];
    try {
        const response = await TrpgApi.get<ApiResponse<Scenario[]>>("/api/scenarios");
        if (!response.success || !Array.isArray(response.data)) throw new Error(response.message || response.error || "加载剧本列表失败");
        // 保留当前剧本：便于切换到同一剧本的最新版本并继承剧情进度。
        scenarios = response.data;
    } catch (error) {
        showNotification(`加载剧本列表失败：${roomErrorMessage(error)}`, "error");
        return;
    }
    if (!scenarios.length) {
        showNotification("没有可切换的剧本", "error");
        return;
    }

    const modal = document.createElement("div");
    modal.className = "modal fade";
    modal.id = "roomSwitchScenarioModal";
    modal.tabIndex = -1;
    modal.innerHTML = `
        <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title">切换房间剧本</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                </div>
                <div class="modal-body">
                    <div class="alert alert-danger" role="alert">
                        <strong><i class="fa fa-exclamation-triangle" aria-hidden="true"></i> 高风险操作，请谨慎确认</strong>
                        <ul class="mb-0 mt-2 ps-3">
                            <li>聊天记录会<b>保留</b>，但其中涉及旧剧本的内容可能与新剧本冲突，导致<b>剧本错乱</b>。</li>
                            <li><b>不勾选</b>「继承剧情进度」时，线索、物品、任务、已触发事件、场景指针、滚动摘要会被<b>全部重置</b>，且无法撤回，切换后需重新点击「开启剧本」。</li>
                            <li><b>勾选</b>「继承剧情进度」时（建议用于切换到同一剧本的最新版本），将<b>保留</b>现有剧情进度；若旧场景在新剧本中已不存在，场景指针会回退到新剧本的开场。</li>
                        </ul>
                    </div>
                    <label class="form-label" for="roomSwitchScenarioSelect">切换到</label>
                    <select class="form-select" id="roomSwitchScenarioSelect"></select>
                    <div class="form-check mt-3">
                        <input class="form-check-input" type="checkbox" id="roomSwitchScenarioInheritProgress">
                        <label class="form-check-label" for="roomSwitchScenarioInheritProgress">继承剧情进度（切换到同一剧本的最新版本时建议勾选）</label>
                    </div>
                    <div class="form-check mt-2">
                        <input class="form-check-input" type="checkbox" id="roomSwitchScenarioConfirmRisk">
                        <label class="form-check-label" for="roomSwitchScenarioConfirmRisk">我已了解上述风险，确认强制切换</label>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button>
                    <button type="button" class="btn btn-danger" id="roomSwitchScenarioConfirm" disabled>强制切换</button>
                </div>
            </div>
        </div>`;
    document.body.appendChild(modal);

    const select = modal.querySelector<HTMLSelectElement>("#roomSwitchScenarioSelect");
    // 用 DOM API 填充剧本选项，避免标题中的特殊字符被当成 HTML。
    scenarios.forEach((scenario) => {
        const isCurrent = String(scenario.id) === String(room.scenario_id);
        const option = document.createElement("option");
        option.value = String(scenario.id);
        // 当前剧本标注为「最新版本」，避免与其它剧本混淆。
        option.textContent = isCurrent ? `${scenario.title}（当前剧本·最新版本）` : scenario.title;
        option.dataset.title = scenario.title;
        option.dataset.current = isCurrent ? "1" : "0";
        select?.appendChild(option);
    });

    const confirmButton = modal.querySelector<HTMLButtonElement>("#roomSwitchScenarioConfirm");
    const riskCheckbox = modal.querySelector<HTMLInputElement>("#roomSwitchScenarioConfirmRisk");
    const inheritCheckbox = modal.querySelector<HTMLInputElement>("#roomSwitchScenarioInheritProgress");
    // 选到同一剧本时默认勾选「继承剧情进度」，切到其它剧本时默认取消。
    const syncDefaultInherit = (): void => {
        const option = select?.options[select.selectedIndex];
        if (inheritCheckbox) inheritCheckbox.checked = option?.dataset.current === "1";
    };
    const syncConfirmState = (): void => {
        if (confirmButton) confirmButton.disabled = !(riskCheckbox?.checked && select?.value);
    };
    riskCheckbox?.addEventListener("change", syncConfirmState);
    select?.addEventListener("change", () => {
        syncDefaultInherit();
        syncConfirmState();
    });
    confirmButton?.addEventListener("click", () => {
        if (!select?.value || !riskCheckbox?.checked) return;
        const option = select.options[select.selectedIndex];
        void switchRoomScenario(
            modal,
            Number.parseInt(select.value, 10),
            option?.dataset.title || "",
            inheritCheckbox?.checked === true,
        );
    });

    const instance = new bootstrap.Modal(modal);
    modal.addEventListener("hidden.bs.modal", () => modal.remove());
    syncDefaultInherit();
    syncConfirmState();
    instance.show();
}

async function switchRoomScenario(
    modal: HTMLElement,
    scenarioId: number,
    scenarioTitle: string,
    preserveProgress: boolean,
): Promise<void> {
    const room = currentRoom;
    if (!room?.id || !Number.isFinite(scenarioId)) return;
    const confirmButton = modal.querySelector<HTMLButtonElement>("#roomSwitchScenarioConfirm");
    if (confirmButton) confirmButton.disabled = true;
    try {
        const response = await TrpgApi.post<ApiResponse<Room>>(`/api/rooms/${encodeURIComponent(room.id)}/scenario-switch`, {
            scenario_id: scenarioId,
            scenario_title: scenarioTitle,
            preserve_progress: preserveProgress,
        });
        if (!response.success || !response.data) throw new Error(response.message || response.error || "切换剧本失败");
        currentRoom = { ...room, ...response.data, messages: room.messages ?? [] };
        window.currentRoom = currentRoom;
        bootstrap.Modal.getInstance(modal)?.hide();
        updateRoomDetail(currentRoom);
        await loadRoomsList();
        const suffix = preserveProgress ? "，剧情进度已继承" : "，请重新开启剧本";
        showNotification(`已切换到剧本：${currentRoom.scenario_title || scenarioTitle}${suffix}`, "success");
    } catch (error) {
        if (confirmButton) confirmButton.disabled = false;
        showNotification(`切换剧本失败：${roomErrorMessage(error)}`, "error");
    }
}

interface RoomHouseRulesPayload {
    house_rules: RoomHouseRules;
    global_hints_enabled: boolean;
    visibility?: "public" | "private";
    dice_thresholds?: { critical: number; fumble: number };
    dice_threshold_defaults?: { critical: number; fumble: number };
    dice_animation_defaults?: { level: "text" | "simple" | "3d"; player_roll: boolean; reroll_mode: "off" | "limited" | "unlimited"; reroll_limit: number };
    dice_animation?: {
        level: "text" | "simple" | "3d";
        player_roll: boolean;
        timeout: number;
        reroll_mode: "off" | "limited" | "unlimited";
        reroll_limit: number;
        rerolls: number;
        level_source: "room" | "admin";
        player_roll_source: "room" | "admin";
        reroll_source: "room" | "admin";
        physics_results: boolean;
        result_source: "physics" | "server";
    };
}

/** 读取房规弹窗中的重投次数输入；留空返回 null 表示继承管理员默认值。 */
function readHouseRuleDiceRerollLimit(): number | null {
    const input = document.getElementById("houseRuleDiceRerollLimit") as HTMLInputElement | null;
    const raw = input?.value.trim() || "";
    if (!raw) return null;
    const value = Number.parseInt(raw, 10);
    if (!Number.isFinite(value)) return null;
    return Math.max(1, Math.min(20, value));
}

/** 房规里只有「指定次数」时才允许编辑重投次数，跟随默认/关闭/无限均置灰。 */
function syncHouseRuleDiceRerollLimit(): void {
    const mode = document.getElementById("houseRuleDiceRerollMode") as HTMLSelectElement | null;
    const limit = document.getElementById("houseRuleDiceRerollLimit") as HTMLInputElement | null;
    if (!mode || !limit) return;
    if (mode.dataset.rerollBound !== "1") {
        mode.dataset.rerollBound = "1";
        mode.addEventListener("change", syncHouseRuleDiceRerollLimit);
    }
    const editable = mode.value === "limited";
    limit.disabled = !editable;
    limit.closest<HTMLElement>(".form-group")?.classList.toggle("setting-disabled", !editable);
}

/** 读取房规弹窗中的大成功/大失败阈值输入；留空返回 null 表示沿用管理员默认值。 */
function readHouseRuleDiceThreshold(elementId: string): number | null {
    const input = document.getElementById(elementId) as HTMLInputElement | null;
    const raw = input?.value.trim() || "";
    if (!raw) return null;
    const value = Number.parseInt(raw, 10);
    if (!Number.isFinite(value)) return null;
    return Math.max(0, Math.min(100, value));
}

async function openHouseRulesModal(): Promise<void> {
    if (!currentRoom?.id) return;
    const modalElement = document.getElementById("roomHouseRulesModal");
    const checkbox = document.getElementById("houseRuleActionSuggestions") as HTMLInputElement | null;
    const globalHint = document.getElementById("houseRuleGlobalHint") as HTMLElement | null;
    const visibilitySelect = document.getElementById("houseRuleVisibility") as HTMLSelectElement | null;
    const criticalInput = document.getElementById("houseRuleDiceCritical") as HTMLInputElement | null;
    const fumbleInput = document.getElementById("houseRuleDiceFumble") as HTMLInputElement | null;
    const diceHint = document.getElementById("houseRuleDiceDefaultHint") as HTMLElement | null;
    if (!modalElement) return;
    try {
        const response = await TrpgApi.get<ApiResponse<RoomHouseRulesPayload>>(`/api/rooms/${encodeURIComponent(currentRoom.id)}/house-rules`);
        if (!response.success || !response.data) {
            showNotification(response.message || response.error || "加载房规失败", "error");
            return;
        }
        const globalEnabled = response.data.global_hints_enabled !== false;
        if (checkbox) {
            checkbox.checked = Boolean(response.data.house_rules?.action_suggestions_enabled);
            checkbox.disabled = !globalEnabled;
        }
        if (globalHint) globalHint.hidden = globalEnabled;
        if (visibilitySelect) visibilitySelect.value = response.data.visibility === "public" ? "public" : "private";
        // 留空代表沿用管理员设置页配置的默认阈值，占位符展示当前默认值。
        const defaults = response.data.dice_threshold_defaults;
        const critical = response.data.house_rules?.dice_critical_threshold;
        const fumble = response.data.house_rules?.dice_fumble_threshold;
        if (criticalInput) {
            criticalInput.value = critical === null || critical === undefined ? "" : String(critical);
            criticalInput.placeholder = defaults ? `留空使用默认值（${defaults.critical}）` : "留空使用默认值";
        }
        if (fumbleInput) {
            fumbleInput.value = fumble === null || fumble === undefined ? "" : String(fumble);
            fumbleInput.placeholder = defaults ? `留空使用默认值（${defaults.fumble}）` : "留空使用默认值";
        }
        if (diceHint && defaults) {
            diceHint.textContent = `留空表示使用管理员设置的默认阈值（大成功 ≤ ${defaults.critical}，大失败 ≥ ${defaults.fumble}）。`;
        }
        const animationSelect = document.getElementById("houseRuleDiceAnimationLevel") as HTMLSelectElement | null;
        const playerRollSelect = document.getElementById("houseRulePlayerDiceRoll") as HTMLSelectElement | null;
        const rerollModeSelect = document.getElementById("houseRuleDiceRerollMode") as HTMLSelectElement | null;
        const rerollLimitInput = document.getElementById("houseRuleDiceRerollLimit") as HTMLInputElement | null;
        const animationHint = document.getElementById("houseRuleDiceAnimationHint") as HTMLElement | null;
        if (animationSelect) animationSelect.value = response.data.house_rules?.dice_animation_level || "";
        const playerRule = response.data.house_rules?.player_dice_roll;
        if (playerRollSelect) playerRollSelect.value = playerRule === null || playerRule === undefined ? "" : playerRule ? "on" : "off";
        const rerollMode = response.data.house_rules?.dice_reroll_mode;
        const rerollLimit = response.data.house_rules?.dice_reroll_limit;
        if (rerollModeSelect) rerollModeSelect.value = rerollMode || "";
        if (rerollLimitInput) {
            rerollLimitInput.value = rerollLimit === null || rerollLimit === undefined ? "" : String(rerollLimit);
            const defaultsReroll = response.data.dice_animation_defaults;
            rerollLimitInput.placeholder = defaultsReroll ? `留空继承默认值（${defaultsReroll.reroll_limit}）` : "留空继承默认值";
        }
        syncHouseRuleDiceRerollLimit();
        if (animationHint) {
            const defaultsAnim = response.data.dice_animation_defaults;
            const effective = response.data.dice_animation;
            const levelLabel = { text: "纯文本", simple: "简易动画", "3d": "3D 骰子" } as const;
            const rerollLabel = { off: "关闭", limited: "指定次数", unlimited: "无限" } as const;
            const rerollText = (mode: "off" | "limited" | "unlimited", limit: number): string =>
                mode === "limited" ? `${rerollLabel[mode]}（${limit} 次）` : rerollLabel[mode];
            const parts: string[] = [];
            if (defaultsAnim) parts.push(`管理员默认：${levelLabel[defaultsAnim.level]} / 玩家投掷${defaultsAnim.player_roll ? "开启" : "关闭"} / 重投${rerollText(defaultsAnim.reroll_mode, defaultsAnim.reroll_limit)}`);
            if (effective) parts.push(`当前生效：${levelLabel[effective.level]} / 玩家投掷${effective.player_roll ? "开启" : "关闭"} / 重投${rerollText(effective.reroll_mode, effective.reroll_limit)}（等待超时 ${effective.timeout} 秒）`);
            animationHint.textContent = parts.join("；");
        }
        new bootstrap.Modal(modalElement).show();
    } catch (error) {
        showNotification(`加载房规失败：${roomErrorMessage(error)}`, "error");
    }
}

async function saveHouseRules(): Promise<void> {
    if (!currentRoom?.id) return;
    const checkbox = document.getElementById("houseRuleActionSuggestions") as HTMLInputElement | null;
    const visibilitySelect = document.getElementById("houseRuleVisibility") as HTMLSelectElement | null;
    if (!checkbox) return;
    const animationSelect = document.getElementById("houseRuleDiceAnimationLevel") as HTMLSelectElement | null;
    const playerRollSelect = document.getElementById("houseRulePlayerDiceRoll") as HTMLSelectElement | null;
    const rerollModeSelect = document.getElementById("houseRuleDiceRerollMode") as HTMLSelectElement | null;
    const playerRuleValue = playerRollSelect?.value || "";
    const rerollModeValue = rerollModeSelect?.value || "";
    try {
        const response = await TrpgApi.put<ApiResponse<RoomHouseRulesPayload>>(`/api/rooms/${encodeURIComponent(currentRoom.id)}/house-rules`, {
            house_rules: {
                action_suggestions_enabled: checkbox.checked,
                dice_critical_threshold: readHouseRuleDiceThreshold("houseRuleDiceCritical"),
                dice_fumble_threshold: readHouseRuleDiceThreshold("houseRuleDiceFumble"),
                dice_animation_level: animationSelect?.value || null,
                player_dice_roll: playerRuleValue === "" ? null : playerRuleValue === "on",
                dice_reroll_mode: rerollModeValue || null,
                // 非「指定次数」时不写入次数，避免留下与模式无关的覆盖值。
                dice_reroll_limit: rerollModeValue === "limited" ? readHouseRuleDiceRerollLimit() : null,
            },
            visibility: visibilitySelect?.value || "private",
        });
        if (!response.success || !response.data) {
            showNotification(response.message || response.error || "保存房规失败", "error");
            return;
        }
        currentRoom.house_rules = response.data.house_rules;
        if (response.data.visibility) currentRoom.visibility = response.data.visibility;
        if (response.data.dice_thresholds) currentRoom.dice_thresholds = response.data.dice_thresholds;
        if (response.data.dice_animation) currentRoom.dice_animation = response.data.dice_animation;
        updateRoomDetail(currentRoom);
        void window.COC7CharacterSheet?.reloadSkillBases?.();
        bootstrap.Modal.getInstance(document.getElementById("roomHouseRulesModal"))?.hide();
        showNotification("房规已保存", "success");
    } catch (error) {
        showNotification(`保存房规失败：${roomErrorMessage(error)}`, "error");
    }
}

function canStartRoomScenario(room: Room): boolean {
    if (isElevatedUser()) return true;
    const userId = String(window.currentUser?.user_id || "");
    if (String(room.creator_id ?? room.owner_id ?? "") === userId) return true;
    const member = activeRoomMembers(room).find((item) => String(item.user_id) === userId);
    return member?.room_role === "owner" || member?.room_role === "admin";
}

function updateStartScenarioButton(room: Room): void {
    const button = document.getElementById("startScenario") as HTMLButtonElement | null;
    if (!button) return;
    button.hidden = room.invisible_view === true || Boolean(room.scenario_started_at) || !canStartRoomScenario(room);
    button.disabled = false;
}

function renderRoomCharacterBindings(room: Room): void {
    const container = document.getElementById("roomCharacterBindings");
    if (!container) return;
    const members = room.members || [];
    if (members.length === 0) {
        container.innerHTML = window.TrpgTemplates.render("room-character-empty");
        return;
    }

    container.innerHTML = window.TrpgTemplates.render("room-character-binding-table", {
        rowsHtml: members.map((member) => renderRoomMemberBindingRow(room, member)).join(""),
    });

    container.querySelectorAll<HTMLButtonElement>("[data-bind-user-id]").forEach((button) => {
        button.addEventListener("click", () => openRoomCharacterBindModal(button.dataset.bindUserId || ""));
    });
    container.querySelectorAll<HTMLButtonElement>("[data-remove-user-id]").forEach((button) => {
        button.addEventListener("click", () => void removeRoomMember(button.dataset.removeUserId || ""));
    });
    container.querySelectorAll<HTMLButtonElement>("[data-promote-user-id]").forEach((button) => {
        button.addEventListener("click", () => void promoteRoomMember(button.dataset.promoteUserId || ""));
    });
    container.querySelectorAll<HTMLButtonElement>("[data-room-character-preview]").forEach((button) => {
        button.addEventListener("click", () => previewRoomCharacter(button.dataset.roomCharacterPreview || ""));
    });
}

function renderRoomMemberBindingRow(room: Room, member: RoomMember): string {
    const card = member.character_card;
    const canManage = canManageRoomMembers(room);
    const isSelf = String(member.user_id) === String(window.currentUser?.user_id);
    const isActive = isActiveRoomMember(member);
    const isOnline = member.is_online === true;
    const canChangeCard = isActive && (isElevatedUser() || isSelf || canManage);
    const canRemove = isActive && canManage && String(member.user_id) !== String(room.creator_id);
    const canPromote = isActive && canManage && String(member.user_id) !== String(room.creator_id) && member.room_role !== "admin";
    const cardPreviewHtml = card
        ? `<button type="button" class="btn btn-link p-0 room-character-preview" data-room-character-preview="${encodeURIComponent(JSON.stringify(card))}">${escapeRoomHtml(card.name || "未命名角色卡")}</button>`
        : "未绑定";
    return window.TrpgTemplates.render("room-character-binding-row", {
        userId: member.user_id || "",
        username: `${member.username || "-"}（${isOnline ? "在线" : "离线"}）`,
        rowClass: isActive ? "" : "room-member-removed",
        cardName: card ? card.name || "未命名角色卡" : "未绑定",
        cardPreviewHtml,
        permission: member.permission_label || roomPermissionLabel(room, member),
        changeButtonHtml: canChangeCard ? window.TrpgTemplates.render("room-bind-character-button", { userId: member.user_id || "" }) : "",
        removeButtonHtml: canRemove ? window.TrpgTemplates.render("room-remove-member-button", { userId: member.user_id || "" }) : "",
        promoteButtonHtml: canPromote ? window.TrpgTemplates.render("room-promote-member-button", { userId: member.user_id || "" }) : "",
    });
}

function roomText(key: string, fallback: string, values?: Record<string, string | number>): string {
    return window.TrpgI18n?.t(key, fallback, values) || fallback;
}

function renderRoomMemberList(room: Room | null): void {
    const container = document.getElementById("roomMemberList") as HTMLElement | null;
    if (!container) return;
    const emptyHtml = `<p class="home-room-members-empty">${escapeRoomHtml(roomText("home.members.empty", "加入房间后这里会显示房间成员。"))}</p>`;
    const visibleRoom = room && room.invisible_view !== true ? room : null;
    const members = visibleRoom ? activeRoomMembers(visibleRoom) : [];
    if (!visibleRoom || members.length === 0) {
        container.innerHTML = emptyHtml;
        return;
    }
    const groups: Array<{ key: string; fallback: string; members: RoomMember[] }> = [
        {
            key: "home.members.group.online",
            fallback: "在线 {count}",
            members: members.filter((member) => member.is_online === true),
        },
        {
            key: "home.members.group.offline",
            fallback: "离线 {count}",
            members: members.filter((member) => member.is_online !== true),
        },
    ];
    container.innerHTML = groups
        .filter((group) => group.members.length > 0)
        .map((group) => window.TrpgTemplates.render("room-member-group", {
            title: roomText(group.key, group.fallback, { count: group.members.length }),
            itemsHtml: group.members.map((member) => renderRoomMemberListItem(visibleRoom, member)).join(""),
        }))
        .join("");
}

function renderRoomMemberListItem(room: Room, member: RoomMember): string {
    const isOnline = member.is_online === true;
    const isSelf = String(member.user_id) === String(window.currentUser?.user_id);
    const hasCard = Boolean(member.character_card?.name);
    const selfBadgeHtml = isSelf
        ? `<span class="home-room-member-self">${escapeRoomHtml(roomText("home.members.you", "你"))}</span>`
        : "";
    const roleKind = AuthModule.memberRoleKind(room, member);
    const roleLabel = member.permission_label || roomPermissionLabel(room, member);
    const roleBadgeHtml = `<span class="home-room-member-role is-${roleKind}">${escapeRoomHtml(roleLabel)}</span>`;
    return window.TrpgTemplates.render("room-member-item", {
        userId: String(member.user_id || ""),
        selfClass: isSelf ? " is-self" : "",
        roleClass: roleKind === "member" ? "" : ` is-${roleKind}`,
        avatar: TrpgAvatar.resolveAvatarSrc(member.username || "", member.avatar),
        presenceClass: isOnline ? "is-online" : "is-offline",
        presenceTitle: roomText(isOnline ? "home.members.online" : "home.members.offline", isOnline ? "在线" : "离线"),
        username: member.username || "-",
        roleBadgeHtml,
        selfBadgeHtml,
        cardClass: hasCard ? "" : " is-unbound",
        cardName: hasCard ? member.character_card?.name : roomText("home.members.unbound", "未绑定角色卡"),
    });
}

function canManageRoomMembers(room: Room): boolean {
    if (isElevatedUser()) return true;
    const currentMember = activeRoomMembers(room).find((member) => String(member.user_id) === String(window.currentUser?.user_id));
    return currentMember?.room_role === "owner" || currentMember?.room_role === "admin" || String(room.creator_id) === String(window.currentUser?.user_id);
}

function roomPermissionLabel(room: Room, member: RoomMember): string {
    let label = "\u6210\u5458";
    if (["ADMIN", "OWNER"].includes(member.role || "")) label = "\u7ba1\u7406\u5458";
    else if (String(member.user_id) === String(room.creator_id) || member.room_role === "owner") label = "\u623f\u4e3b";
    else if (member.room_role === "admin") label = "\u7ba1\u7406\u5458";
    return isActiveRoomMember(member) ? label : `${label}\uff08\u5df2\u79fb\u9664\uff09`;
}

function openRoomCharacterBindModal(userId: string, message = "\u8bf7\u9009\u62e9\u8981\u7ed1\u5b9a\u5230\u8be5\u73a9\u5bb6\u7684\u89d2\u8272\u5361\u3002"): void {
    populateCharacterSelect("roomBindCharacterSelect");
    const targetInput = document.getElementById("roomBindTargetUserId") as HTMLInputElement | null;
    if (targetInput) targetInput.value = userId;
    setText("roomCharacterBindMessage", message);
    const modalElement = document.getElementById("roomCharacterBindModal");
    if (modalElement) new bootstrap.Modal(modalElement).show();
}

async function confirmRoomCharacterBinding(): Promise<void> {
    if (!currentRoom?.id) return;
    const userId = roomInputValue("roomBindTargetUserId") || String(window.currentUser?.user_id || "");
    return runRoomAction(`bind-character:${currentRoom.id}:${userId}`, "confirmRoomCharacterBind", async () => {
        await confirmRoomCharacterBindingUnlocked(userId);
    });
}

async function confirmRoomCharacterBindingUnlocked(userId: string): Promise<void> {
    if (!currentRoom?.id) return;
    const selectedCard = getSelectedCharacterCardSnapshot("roomBindCharacterSelect");
    if (!selectedCard) {
        showNotification("请选择要绑定的角色卡", "error");
        return;
    }
    // 超出房规上限时提示；用户取消则中止本次绑定，可先调整角色卡。
    const characterCard = await enforceSkillBaseLimits(selectedCard, roomSkillBaseOverrides(currentRoom), "该角色卡的基础值超过本房间规则上限。");
    if (!characterCard) {
        showNotification("已取消绑定：角色卡基础值超过房间规则上限", "info");
        return;
    }
    const response = await TrpgApi.put<ApiResponse<Room>>(`/api/rooms/${currentRoom.id}/members/${encodeURIComponent(userId)}/character`, {
        character_card: characterCard,
    });
    if (!response.success || !response.data) {
        showNotification(response.message || response.error || "绑定角色卡失败", "error");
        return;
    }
    if (String(userId) === String(window.currentUser?.user_id)) {
        syncRoomCharacterSelection(currentRoom.id, characterCard);
    }
    bootstrap.Modal.getInstance(document.getElementById("roomCharacterBindModal"))?.hide();
    await enterRoom(response.data);
}

async function removeRoomMember(userId: string): Promise<void> {
    if (!currentRoom?.id || !userId) return;
    const response = await TrpgApi.del<ApiResponse<Room>>(`/api/rooms/${currentRoom.id}/members/${encodeURIComponent(userId)}`);
    if (!response.success || !response.data) {
        showNotification(response.message || response.error || "删除玩家失败", "error");
        return;
    }
    await enterRoom(response.data);
}

async function promoteRoomMember(userId: string): Promise<void> {
    if (!currentRoom?.id || !userId) return;
    const response = await TrpgApi.put<ApiResponse<Room>>(`/api/rooms/${currentRoom.id}/members/${encodeURIComponent(userId)}/role`, { room_role: "admin" });
    if (!response.success || !response.data) {
        showNotification(response.message || response.error || "提权失败", "error");
        return;
    }
    await enterRoom(response.data);
}

async function recordCharacterChange(payload: CharacterRecordPayload): Promise<unknown> {
    const roomName = String(payload.roomName || payload.roomId || "");
    const username = String(payload.username || "");
    const type = payload.type === "san" ? "san" : "damage";
    const value = typeof payload.value === "number" ? payload.value : Number(payload.value || 1);
    const reason = String(payload.reason || "未知");

    if (!roomName) {
        showNotification("请指定房间名", "error");
        return null;
    }
    if (!username) {
        showNotification("请填写用户名", "error");
        return null;
    }

    const data = await TrpgApi.post<ApiResponse<RoomRecordResponse>>(`/api/rooms/by-name/${encodeURIComponent(roomName)}/character-records`, {
        username,
        type,
        value,
        reason,
    });
    if (!data.success) {
        showNotification(data.message || data.error || "记录失败", "error");
        return null;
    }
    setText("recordToolResult", "记录已保存");
    if (currentRoom?.name === roomName && currentRoom.id) await openRoomDetail(currentRoom.id);
    return data.data || null;
}

function updateHomeRoomMeta(): void {
    const scenario = document.getElementById("homeRoomScenario") as HTMLElement | null;
    if (!scenario) return;
    const inRoom = Boolean(currentRoom) && currentRoom?.invisible_view !== true;
    scenario.hidden = !inRoom;
    setText("saveStatusScenario", currentRoom?.scenario_title || "-");
}

async function deleteCurrentRoom(): Promise<void> {
    if (!currentRoom) return;
    const confirmed = await window.TrpgDialog?.confirm({
        title: "删除房间",
        message: "确定要删除这个房间吗？此操作不可恢复。",
        confirmText: "删除",
        danger: true,
    });
    if (!confirmed) return;

    try {
        const deletingRoomId = currentRoom.id;
        const data = await TrpgApi.del<ApiResponse>(`/api/rooms/${deletingRoomId}`);
        if (!data.success) {
            showNotification(`删除房间失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }

        window.leaveSocketRoom?.(deletingRoomId);
        currentRoom = null;
        window.currentRoom = null;
        TrpgCookies.remove(getLastRoomStorageKey());
        window.clearChatMessages?.();
        showRoomListView();
        showNotification("房间已删除", "success");
    } catch (error) {
        showNotification(`删除房间失败: ${roomErrorMessage(error)}`, "error");
    }
}

async function loadRoomNodes(): Promise<void> {
    if (!currentRoom) return;
    try {
        const data = await TrpgApi.get<ApiResponse<RoomNodeList>>(`/api/rooms/${currentRoom.id}/nodes`);
        if (data.success) renderRoomNodeList(data.data?.nodes || []);
    } catch (error) {
        console.error("加载回档节点失败:", error);
    }
}

function renderRoomNodeList(nodes: RoomNode[]): void {
    const nodeListContainer = document.getElementById("saveNodeList");
    if (!nodeListContainer) return;

    if (nodes.length === 0) {
        nodeListContainer.innerHTML = window.TrpgTemplates.render("room-node-empty");
        return;
    }

    nodeListContainer.innerHTML = window.TrpgTemplates.render("room-node-list", {
        nodesHtml: nodes.map(renderRoomNodeItem).join(""),
    });
    nodeListContainer.querySelectorAll<HTMLButtonElement>(".preview-node-btn").forEach((button) => {
        button.addEventListener("click", () => void previewRoomNode(button.dataset.nodeFilename || ""));
    });
    nodeListContainer.querySelectorAll<HTMLButtonElement>(".load-node-btn").forEach((button) => {
        button.addEventListener("click", () => void restoreRoomNode(button.dataset.nodeFilename || ""));
    });
    nodeListContainer.querySelectorAll<HTMLButtonElement>(".delete-node-btn").forEach((button) => {
        button.addEventListener("click", () => void deleteRoomNode(button.dataset.nodeFilename || ""));
    });
}

function renderRoomNodeItem(node: RoomNode): string {
    return window.TrpgTemplates.render("room-node-item", {
        filename: node.filename,
        createdAt: node.created_at || "-",
        messageCount: node.message_count || 0,
        nodeLabel: node.automatic ? "自动存档" : "回档节点",
    });
}

async function previewRoomNode(nodeFilename: string): Promise<void> {
    if (!currentRoom || !nodeFilename) return;
    try {
        const data = await TrpgApi.get<ApiResponse<{ messages: ChatMessage[] }>>(`/api/rooms/${currentRoom.id}/nodes/${nodeFilename}`);
        if (!data.success || !data.data) {
            showNotification(`预览回档节点失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        previewNodeFilename = nodeFilename;
        renderPreviewContent(data.data.messages || []);
        const modalElement = document.getElementById("saveNodePreviewModal");
        if (modalElement) new bootstrap.Modal(modalElement).show();
    } catch (error) {
        showNotification(`预览回档节点失败: ${roomErrorMessage(error)}`, "error");
    }
}

function renderPreviewContent(messages: ChatMessage[]): void {
    const previewContent = document.getElementById("saveNodePreviewContent");
    if (!previewContent) return;

    previewContent.innerHTML = window.TrpgTemplates.render("room-preview-list", {
        messagesHtml: messages.map((message) => window.TrpgTemplates.render("room-preview-message", {
            sender: message.sender_name || message.sender || "未知",
            content: message.content,
        })).join(""),
    });
}

async function createRoomNode(): Promise<void> {
    if (!currentRoom) {
        showNotification("请先进入一个房间", "error");
        return;
    }

    try {
        const data = await TrpgApi.post<ApiResponse>(`/api/rooms/${currentRoom.id}/nodes`);
        if (!data.success) {
            showNotification(`创建回档节点失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        await loadRoomNodes();
        showNotification("回档节点已创建", "success");
    } catch (error) {
        showNotification(`创建回档节点失败: ${roomErrorMessage(error)}`, "error");
    }
}

async function restoreRoomNode(nodeFilename: string): Promise<void> {
    if (!currentRoom || !nodeFilename) return;
    try {
        const data = await TrpgApi.post<ApiResponse<{ messages: ChatMessage[] }>>(`/api/rooms/${currentRoom.id}/nodes/${nodeFilename}/restore`);
        if (!data.success || !data.data) {
            showNotification(`回档失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        window.renderChatMessages?.(data.data.messages || []);
        await loadRoomNodes();
        showNotification("已回档到选定节点", "success");
    } catch (error) {
        showNotification(`回档失败: ${roomErrorMessage(error)}`, "error");
    }
}

async function deleteRoomNode(nodeFilename: string): Promise<void> {
    if (!currentRoom || !nodeFilename) return;
    const confirmed = await window.TrpgDialog?.confirm({
        title: "删除回档节点",
        message: "确定要删除这个回档节点吗？",
        confirmText: "删除",
        danger: true,
    });
    if (!confirmed) return;

    try {
        const data = await TrpgApi.del<ApiResponse>(`/api/rooms/${currentRoom.id}/nodes/${nodeFilename}`);
        if (!data.success) {
            showNotification(`删除回档节点失败: ${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        await loadRoomNodes();
        showNotification("回档节点已删除", "success");
    } catch (error) {
        showNotification(`删除回档节点失败: ${roomErrorMessage(error)}`, "error");
    }
}

async function autoLoadLastRoom(): Promise<void> {
    if (!window.currentUser?.user_id) return;
    // 通过邀请链接（?join=房间码）进入时优先处理邀请，避免被上次房间覆盖。
    if (await tryJoinRoomFromInvite()) return;
    const lastRoomId = TrpgCookies.get(getLastRoomStorageKey());
    if (!lastRoomId) return;
    await openRoomDetail(lastRoomId);
}

function clearCurrentRoom(): void {
    if (currentRoom?.id) window.leaveSocketRoom?.(currentRoom.id);
    currentRoom = null;
    window.currentRoom = null;
    window.TrpgDiceTable?.onRoomChanged();
    window.setChatReadOnly?.(false);
    applyInvisibleRoomView(false);
    window.closeRoomConsole?.();
    setText("homeRoomTitle", "未加入房间");
    setText("homeRoomOnlineCount", "在线玩家 0/0");
    renderRoomMemberList(null);
    showRoomListView();
}

function previewRoomCharacter(encodedCard: string): void {
    try {
        const rawCard = JSON.parse(decodeURIComponent(encodedCard)) as Partial<COC7CharacterCard>;
        const card = window.COC7CharacterSheet?.createCharacterCard?.(rawCard as COC7CharacterCardInput) || (rawCard as COC7CharacterCard);
        const previewHtml = window.COC7CharacterSheet?.renderCharacterDetail?.(card) || `<h4>${escapeRoomHtml(card.name || "未命名角色卡")}</h4>`;
        const modalElement = document.createElement("div");
        modalElement.className = "modal fade";
        modalElement.innerHTML = `<div class="modal-dialog modal-xl"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">角色卡预览</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body">${previewHtml}</div></div></div>`;
        document.body.appendChild(modalElement);
        const instance = new bootstrap.Modal(modalElement);
        instance.show();
        modalElement.addEventListener("hidden.bs.modal", () => modalElement.remove());
    } catch {
        showNotification("角色卡预览失败", "error");
    }
}

function formatRoomOnlineCount(room: Room): string {
    const members = room.members || [];
    const online = members.filter((member) => member.is_online === true).length;
    const total = members.filter((member) => member.status !== "removed").length;
    return window.TrpgI18n?.t("room.status.online_players.count", `在线玩家 ${online}/${total}`, { online, total })
        || `在线玩家 ${online}/${total}`;
}

function escapeRoomHtml(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function roomInputValue(id: string): string {
    return (document.getElementById(id) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null)?.value.trim() || "";
}

function setInput(id: string, value: string): void {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.value = value;
}

function setText(id: string, value: string): void {
    const element = document.getElementById(id);
    if (!element) return;
    const fallbackKeys: Record<string, [string, string]> = {
        homeRoomTitle: ["home.not_joined", "未加入房间"],
        homeRoomOnlineCount: ["room.status.online_players", "在线玩家 0/0"],
    };
    const fallback = fallbackKeys[id];
    const nextValue = fallback && value === fallback[1]
        ? window.TrpgI18n?.t(fallback[0], value) || value
        : value;
    if (fallback) element.removeAttribute("data-i18n");
    element.textContent = nextValue;
}

async function runRoomAction(actionKey: string, buttonId: string, action: () => Promise<void>): Promise<void> {
    if (roomActionLocks.has(actionKey)) return;
    roomActionLocks.add(actionKey);
    setRoomButtonBusy(buttonId, true);
    try {
        await action();
    } finally {
        roomActionLocks.delete(actionKey);
        setRoomButtonBusy(buttonId, false);
    }
}

function setRoomButtonBusy(buttonId: string, busy: boolean): void {
    const button = document.getElementById(buttonId) as HTMLButtonElement | null;
    if (!button) return;
    button.disabled = busy;
    button.dataset.busy = busy ? "true" : "false";
}

function setDisplay(id: string, value: string): void {
    const element = document.getElementById(id) as HTMLElement | null;
    if (element) element.style.display = value;
}

function roomErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

/** 房间可见性：默认私人（仅房间码可加入），公开房间展示给所有玩家。 */
function roomVisibility(room: Room): "public" | "private" {
    return room.visibility === "public" ? "public" : "private";
}

function roomVisibilityLabel(room: Room): string {
    return roomVisibility(room) === "public" ? "公开" : "私人";
}

function roomVisibilityBadge(room: Room): string {
    if (roomVisibility(room) !== "public") return "";
    return `<span class="badge bg-info text-dark ms-1">公开</span>`;
}

async function writeClipboardText(text: string): Promise<void> {
    if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
    }
    // 兼容不支持异步剪贴板 API 的环境
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
}

async function copyRoomCodeToClipboard(code: string): Promise<void> {
    const value = String(code || "").trim();
    if (!value || value === "-") {
        showNotification("暂无可复制的房间码", "error");
        return;
    }
    try {
        await writeClipboardText(value);
        showNotification(`房间码已复制：${value}`, "success");
    } catch {
        showNotification("复制房间码失败，请手动复制", "error");
    }
}

/** 解析生成邀请链接/二维码时使用的对外基础地址：优先管理员配置，否则回退到访问者当前 origin。 */
function resolveShareBaseUrl(): string {
    const configured = typeof getShareBaseUrl === "function" ? getShareBaseUrl().trim() : "";
    const base = configured || window.location.origin;
    return base.replace(/\/+$/, "");
}

/** 用可轮换的邀请令牌拼出邀请链接（?invite=<token>）。 */
function buildRoomInviteUrl(token: string): string {
    const normalized = String(token || "").trim();
    return `${resolveShareBaseUrl()}/?invite=${encodeURIComponent(normalized)}`;
}

/** 获取当前房间的邀请令牌（无则后端惰性生成），并返回是否允许刷新。 */
async function fetchRoomInviteToken(): Promise<{ token: string; canRotate: boolean }> {
    if (!currentRoom) {
        throw new Error("请先进入一个房间");
    }
    const response = await TrpgApi.get<ApiResponse<RoomInviteTokenResponse>>(
        `/api/rooms/${encodeURIComponent(currentRoom.id)}/invite-token`,
    );
    const token = String(response?.data?.invite_token || "").trim();
    if (!response?.success || !token) {
        throw new Error(response?.message || response?.error || "未能获取邀请链接");
    }
    return { token, canRotate: response.data?.can_rotate !== false };
}

/** 刷新弹窗内的邀请链接与二维码；不具备刷新权限时隐藏刷新按钮。 */
function renderRoomInviteModal(link: string, canRotate: boolean): void {
    const input = document.getElementById("roomInviteLinkInput") as HTMLInputElement | null;
    if (input) input.value = link;
    const container = document.getElementById("roomQrCodeContainer");
    if (container) {
        try {
            const svg = window.TrpgQr?.renderSvg(link, { cellSize: 5, margin: 20, alt: "房间邀请二维码" });
            container.innerHTML = svg || `<div class="text-danger small p-4">${escapeRoomHtml("二维码生成失败")}</div>`;
        } catch (error) {
            container.innerHTML = `<div class="text-danger small p-4">${escapeRoomHtml(roomErrorMessage(error))}</div>`;
        }
    }
    setDisplay("roomInviteRotateBtn", canRotate ? "" : "none");
}

/** 打开「邀请玩家」弹窗：展示邀请链接与二维码（链接即为可轮换的邀请令牌）。 */
async function openRoomInviteModal(): Promise<void> {
    const modalElement = document.getElementById("roomQrModal");
    if (modalElement && typeof bootstrap !== "undefined") {
        const instance = bootstrap.Modal.getInstance(modalElement) || new bootstrap.Modal(modalElement);
        instance.show();
    }
    try {
        const { token, canRotate } = await fetchRoomInviteToken();
        renderRoomInviteModal(buildRoomInviteUrl(token), canRotate);
    } catch (error) {
        showNotification(roomErrorMessage(error), "error");
    }
}

function currentRoomInviteLink(): string {
    const input = document.getElementById("roomInviteLinkInput") as HTMLInputElement | null;
    return input ? input.value.trim() : "";
}

async function copyRoomInviteLink(): Promise<void> {
    const link = currentRoomInviteLink();
    if (!link) {
        showNotification("邀请链接尚未就绪", "error");
        return;
    }
    try {
        await writeClipboardText(link);
        showNotification("邀请链接已复制，发给队友即可直接进入房间", "success");
    } catch {
        showNotification("复制邀请链接失败，请手动复制", "error");
    }
}

/** 刷新邀请令牌：生成新链接并让此前分享出去的旧链接立即失效。 */
async function rotateRoomInviteToken(): Promise<void> {
    if (!currentRoom) return;
    const confirmed = await window.TrpgDialog?.confirm({
        title: "刷新邀请链接",
        message: "刷新后将生成新的邀请链接，之前分享出去的旧链接会立即失效。",
        confirmText: "刷新",
        danger: true,
    });
    if (!confirmed) return;
    try {
        const response = await TrpgApi.post<ApiResponse<RoomInviteTokenResponse>>(
            `/api/rooms/${encodeURIComponent(currentRoom.id)}/invite-token`,
            {},
        );
        const token = String(response?.data?.invite_token || "").trim();
        if (!response?.success || !token) {
            throw new Error(response?.message || response?.error || "刷新失败，请稍后重试");
        }
        renderRoomInviteModal(buildRoomInviteUrl(token), true);
        showNotification("邀请链接已刷新，旧链接已失效", "success");
    } catch (error) {
        showNotification(`刷新邀请链接失败：${roomErrorMessage(error)}`, "error");
    }
}

/** 读取 URL 中的邀请参数：优先可轮换令牌 ?invite=，其次兼容固定房间码 ?join=。 */
function readRoomInviteFromUrl(): { token: string; code: string } {
    try {
        const params = new URLSearchParams(window.location.search);
        return {
            token: (params.get("invite") || "").trim(),
            code: (params.get("join") || "").trim().toUpperCase(),
        };
    } catch {
        return { token: "", code: "" };
    }
}

/** 消费邀请链接中的 ?invite= / ?join= 参数，避免刷新或登录后重复触发。 */
function stripRoomInviteFromUrl(): void {
    try {
        const url = new URL(window.location.href);
        url.searchParams.delete("join");
        url.searchParams.delete("invite");
        window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    } catch {
        // 忽略 URL 解析异常
    }
}

/** 通过邀请令牌加入房间（链接失效时后端返回 404）。 */
async function joinRoomByInviteToken(token: string): Promise<void> {
    const roomEntrySelection = await promptRoomEntryCharacterSelection("join");
    if (!roomEntrySelection) return;
    if (roomEntrySelection.createCharacter || roomEntrySelection.action === "create") return;
    if (roomEntrySelection.action === "invisible") {
        showNotification("邀请链接不支持隐身进入，请改用房间码", "error");
        return;
    }
    try {
        const data = await TrpgApi.post<ApiResponse<Room>>("/api/rooms/join", {
            invite_token: token,
            character_card: roomEntrySelection.characterCard,
        });
        if (!data.success || !data.data) {
            showNotification(`加入房间失败：${data.message || data.error || "未知错误"}`, "error");
            return;
        }
        await enterRoom(data.data);
        await loadRoomsList();
        showNotification("已通过邀请链接加入房间", "success");
    } catch (error) {
        showNotification(`加入房间失败：${roomErrorMessage(error)}`, "error");
    }
}

/**
 * 处理通过邀请链接进入的场景（优先 ?invite=令牌，兼容 ?join=房间码）。
 * 未登录时不消费参数，等登录后由 autoLoadLastRoom 再次处理。返回是否命中邀请。
 */
async function tryJoinRoomFromInvite(): Promise<boolean> {
    const { token, code } = readRoomInviteFromUrl();
    if (!token && !code) return false;
    if (!window.currentUser?.user_id) return false;
    stripRoomInviteFromUrl();
    window.switchMainTab?.("save");
    if (token) {
        await runRoomAction("join-room", "joinRoom", async () => {
            await joinRoomByInviteToken(token);
        });
    } else {
        setInput("roomCodeInput", code);
        await runRoomAction("join-room", "joinRoom", async () => {
            await joinRoomByCodeUnlocked();
        });
    }
    return true;
}

window.initRoomManagement = initRoomManagement;
window.autoLoadLastRoom = autoLoadLastRoom;
window.clearCurrentRoom = clearCurrentRoom;
window.recordCharacterChange = recordCharacterChange;
