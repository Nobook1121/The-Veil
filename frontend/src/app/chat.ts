interface ChatRoleConfig {
    id: string;
    name: string;
    wake_words?: string[];
    avatar?: string;
}

interface PendingAIMessage {
    sender: string;
    content: string;
    role: ChatRoleConfig;
    time: string;
}

interface KnowledgeUsage {
    used: boolean;
    total_chunks: number;
    cached?: boolean;
    ruleset: {
        called: boolean;
        calls: number;
        chunks: number;
        sources: number;
        ruleset_ids?: string[];
        knowledge_versions?: string[];
        topics?: string[];
        citations?: string[];
    };
    scenario: { chunks: number };
}

interface SendAIOptions {
    scenarioStart?: boolean;
}

interface CommandDefinition {
    name: string;
    usage: string;
    description: string;
}

interface ChatApiResponse {
    content?: string;
    error?: string;
    message?: string;
    token_count?: number;
    direct_message?: ChatMessage;
    direct_messages?: ChatMessage[];
    tool_messages?: ChatMessage[];
    prompt_tokens?: number;
    completion_tokens?: number;
    cached_tokens?: number;
    cache_hit_rate?: number;
    elapsed_ms?: number;
    cache_key?: string;
    knowledge_usage?: KnowledgeUsage;
    scenario_started_at?: string;
    ending_reached?: boolean;
    archived?: boolean;
    suggestions?: string[];
    suggestions_owner?: string | number;
}

interface IncomingSocketMessage {
    room_id?: string;
    message?: ChatMessage;
    type?: "ai_thinking_start" | "ai_thinking_end" | string;
    content?: string;
    aiRequestId?: string;
    roleName?: string;
    startedAt?: number;
    stage?: string;
    label?: string;
    suggestions?: string[];
    ownerId?: string | number | null;
}

let isAIThinking = false;
let chatReadOnly = false;
let messageTimestamps: number[] = [];
let pendingMessages: PendingAIMessage[] = [];
let aiName = "KP";
let aiRoles: ChatRoleConfig[] = [{ id: "kp", name: "KP", wake_words: ["@KP"] }];
let socket: SocketLike | null = null;
let activeSuggestions: string[] | null = null;
// 恢复持久化的思考状态时，记录该请求是否为「开启剧本」。只有它才允许在
// 断线重连后带上 scenarioStart 重发，避免普通消息误触发开场。
let restoredScenarioStart = false;
const thinkingTimers = new Map<string, number>();
const THINKING_STORAGE_KEY = "trpg_ai_thinking";
// 其他玩家「正在输入」状态：key 为发送者标识，value 为显示名与超时定时器。
const typingUsers = new Map<string, { name: string; timer: number }>();
const TYPING_EMIT_INTERVAL_MS = 1800;
const TYPING_IDLE_TIMEOUT_MS = 4000;
let lastTypingEmitAt = 0;

const COMMAND_DEFINITIONS: CommandDefinition[] = [
    { name: "/dice", usage: "/dice {dice}", description: "掷骰" },
    { name: "/check", usage: "/check {*玩家名} {*技能/属性名} {困难/极难} {调整值}", description: "属性或技能鉴定" },
    { name: "/sc", usage: "/sc {玩家名} {成功变化/失败变化}", description: "理智检定并结算 SAN" },
    { name: "/record", usage: "/record {damage/san} {username} {int} {reason?}", description: "管理员记录房间角色伤害或 San 损失" },
    { name: "/trigger", usage: "/trigger {触发器编号}", description: "触发场景触发器" },
];

function getCurrentUsername(): string {
    const username = document.getElementById("userName")?.textContent?.trim();
    if (username && username !== "未登录") {
        return username;
    }
    return window.currentUser?.username || "用户";
}

function getCurrentUserId(): string | number | null {
    return window.currentUser?.user_id || null;
}

function getCurrentRoom(): Room | null {
    return window.currentRoom || null;
}

/** 点击聊天消息的头像时，在该头像旁弹出对应成员的资料卡（KP / 系统消息不弹） */
function bindChatAvatarCards(): void {
    document.addEventListener("click", (event) => {
        const target = event.target as HTMLElement | null;
        const avatar = target?.closest<HTMLElement>(".message-avatar");
        if (!avatar || !avatar.closest("#chatHistory")) return;
        const senderId = avatar.closest<HTMLElement>(".message")?.dataset.senderId;
        const room = getCurrentRoom();
        if (!senderId || !room) return;
        const member = (room.members || []).find((entry) => String(entry.user_id) === String(senderId));
        if (!member) return;
        AuthModule.openMemberProfileCard(room, member, avatar);
    });
}

function initChat(): void {
    const chatInput = document.getElementById("chatInput") as HTMLInputElement | null;
    const sendButton = document.getElementById("sendButton") as HTMLButtonElement | null;
    if (!chatInput || !sendButton) return;
    const activeChatInput = chatInput;
    const activeSendButton = sendButton;

    updateAIHint();
    updateInputState(activeChatInput, activeSendButton);
    void loadAIRoles();
    initWebSocket();
    initCommandPalette(activeChatInput);
    initTypingIndicator(activeChatInput);
    restoreThinkingState();
    bindChatAvatarCards();

    async function sendMessage(): Promise<void> {
        const rawMessage = activeChatInput.value.trim();
        if (!rawMessage) return;
        if (chatReadOnly) {
            showNotification("隐身查看时不能发送消息", "error");
            return;
        }
        if (!window.currentUser) {
            showNotification("请先登录后再发送消息", "error");
            showAuthModal?.();
            return;
        }
        if (!getCurrentRoom()) {
            showNotification("请先加入房间后再发送消息", "error");
            return;
        }

        const lockInput = document.getElementById("enableAIResponseLock") as HTMLInputElement | null;
        if (isAIThinking && lockInput?.checked) {
            showNotification("请等待 AI 回复完成后再发送消息", "error");
            return;
        }

        const rateLimitInput = document.getElementById("messageRateLimit") as HTMLInputElement | null;
        const rateLimit = Number.parseInt(rateLimitInput?.value || "0", 10) || 0;
        if (rateLimit > 0 && isRateLimited(rateLimit)) {
            showNotification(`消息发送过于频繁，请稍后再试（限制 ${rateLimit} 条/分钟）`, "error");
            return;
        }

        const matchedRole = findRoleForMessage(rawMessage);
        const isAIMessage = Boolean(matchedRole);
        const message = rawMessage;
        if (!message) {
            showNotification("请输入消息内容", "error");
            return;
        }

        if (message.toLowerCase().startsWith("/record")) {
            activeChatInput.value = "";
            await sendVisibleMessage("player", message);
            const recordResult = await handleRecordCommand(message);
            await sendVisibleMessage("system", recordResult);
            return;
        }

        if (message.toLowerCase().startsWith("/trigger")) {
            activeChatInput.value = "";
            await sendVisibleMessage("player", message);
            const triggerResult = await handleTriggerCommand(message);
            if (triggerResult) {
                renderRoomMessage(triggerResult);
                broadcastMessage(triggerResult);
            }
            return;
        }

        const commandResult = window.toolManager?.handleCommand(message) || null;
        activeChatInput.value = "";

        if (commandResult) {
            await sendVisibleMessage("player", message);
            const lowerMessage = message.toLowerCase();
            const commandType = lowerMessage.startsWith("/dice") || lowerMessage.startsWith("/check") || lowerMessage.startsWith("/sc") ? "dice" : "system";
            window.setTimeout(() => {
                void sendVisibleMessage(commandType, commandResult);
            }, 300);
            return;
        }

        await sendVisibleMessage("player", message);
        if (!isAIMessage || !matchedRole) return;

        const currentRoomForSend = getCurrentRoom();
        if (currentRoomForSend?.archived) {
            showNotification("该房间已归档，AI 已关闭；仍可使用骰娘等房间工具", "info");
            return;
        }

        pendingMessages.push({
            sender: getCurrentUsername(),
            content: message,
            role: matchedRole,
            time: new Date().toLocaleTimeString(),
        });

        if (isAIThinking) {
            showNotification("消息已加入队列，请等待 AI 回复完成后发送", "info");
            return;
        }

        await sendToAI(activeChatInput, activeSendButton);
    }

    activeSendButton.addEventListener("click", () => {
        void sendMessage();
    });
    activeChatInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void sendMessage();
        }
    });
}

function isRateLimited(rateLimit: number): boolean {
    const now = Date.now();
    messageTimestamps = messageTimestamps.filter((timestamp) => now - timestamp < 60000);
    if (messageTimestamps.length >= rateLimit) return true;
    messageTimestamps.push(now);
    return false;
}

async function sendToAI(chatInput: HTMLInputElement, sendButton: HTMLButtonElement, options: SendAIOptions = {}): Promise<void> {
    if (pendingMessages.length === 0) return;
    const requestMessages = pendingMessages.slice();

    isAIThinking = true;
    updateInputState(chatInput, sendButton);

    const aiRequestId = `ai-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const thinkingMessageId = aiRequestId;
    const startTime = Date.now();
    const role = pendingMessages[0]?.role || aiRoles[0] || { id: "kp", name: "KP" };
    addThinkingMessage(thinkingMessageId, role.name || "KP", startTime);
    updateThinkingStage(aiRequestId, "正在准备上下文", "preparing");
    persistThinkingState({ id: thinkingMessageId, roomId: getCurrentRoom()?.id || null, roleName: role.name || "KP", roleId: role.id || "kp", content: pendingMessages.map((message) => message.content).join("\n"), startedAt: startTime, scenarioStart: options.scenarioStart === true });
    broadcastAIThinkingStart(aiRequestId, role.name || "KP", startTime);

    try {
        const { response, data } = await TrpgApi.requestWithResponse<ChatApiResponse>("/api/chat", {
            method: "POST",
            body: {
                content: requestMessages.map((message) => message.content).join("\n"),
                messages: requestMessages,
                role_id: role.id || "kp",
                user_id: getCurrentUserId(),
                room_id: getCurrentRoom()?.id || null,
                ai_request_id: aiRequestId,
                scenario_start: options.scenarioStart === true,
            },
        });
        if (!response.ok) {
            throw new Error(data.message || chatT("chat.error.api", "API 请求失败: {status}", { status: response.status }));
        }

        const processingTime = Math.round((Date.now() - startTime) / 1000);
        const tokenCount = data.token_count ?? null;
        const messageContent = data.content || data.error || chatT("chat.error.unknown", "AI 回复失败: 未知错误");

        pendingMessages = pendingMessages.slice(requestMessages.length);
        const toolMessages = data.tool_messages || [];
        const directMessages = data.direct_messages
            || (data.direct_message ? [data.direct_message] : []);
        for (const toolMessage of toolMessages) {
            const visibleMessage = await persistDirectRoomMessage(toolMessage);
            if (visibleMessage) {
                renderRoomMessage(visibleMessage);
                broadcastMessage(visibleMessage);
            }
        }
        if (toolMessages.length > 0) {
            moveThinkingMessageToEnd(thinkingMessageId);
        }
        replaceThinkingMessage(thinkingMessageId, messageContent, processingTime, tokenCount, data.cache_hit_rate ?? null, data.knowledge_usage);
        broadcastAIThinkingEnd(aiRequestId);
        clearPersistedThinkingState(aiRequestId);

        const persisted = await persistRoomMessage("kp", messageContent, {
            processingTime,
            tokenCount,
            roleId: role.id,
            aiRequestId,
            senderName: role.name || "KP",
            promptTokens: data.prompt_tokens,
            completionTokens: data.completion_tokens,
            cachedTokens: data.cached_tokens,
            cacheHitRate: data.cache_hit_rate,
            cacheKey: data.cache_key,
            knowledgeUsage: data.knowledge_usage,
        });
        if (persisted) {
            persisted.sender_name = role.name || "KP";
            broadcastMessage(persisted);
        }

        // Action suggestions returned by the KP tool are transient UI state: the
        // requesting player may click one to auto-send it, everyone else in the
        // room sees the same buttons read-only. Never persisted as a message.
        if (data.suggestions && data.suggestions.length > 0) {
            showActionSuggestions(data.suggestions, data.suggestions_owner ?? null);
            broadcastActionSuggestions(data.suggestions, data.suggestions_owner ?? null);
        } else {
            clearActionSuggestions(true);
        }

        if (options.scenarioStart && data.scenario_started_at) {
            const room = getCurrentRoom();
            if (room) room.scenario_started_at = data.scenario_started_at;
            const startButton = document.getElementById("startScenario") as HTMLButtonElement | null;
            if (startButton) startButton.hidden = true;
        }

        if (data.ending_reached) {
            const room = getCurrentRoom();
            if (room) {
                room.completed_at = room.completed_at || new Date().toISOString();
                room.archived = room.archived === true;
                window.refreshRoomArchiveUi?.();
            }
            if (!room?.archived) {
                showNotification("剧本已到达结局，房主/管理员可点击“归档房间”进行归档", "success");
            }
        }

        for (const directMessage of directMessages) {
            const persistedDirectMessage = await persistDirectRoomMessage(directMessage);
            if (persistedDirectMessage) {
                renderRoomMessage(persistedDirectMessage);
                broadcastMessage(persistedDirectMessage);
            }
        }

    } catch (error) {
        const processingTime = Math.round((Date.now() - startTime) / 1000);
        replaceThinkingMessage(thinkingMessageId, chatT("chat.error.reply", "AI 回复失败: {detail}", { detail: chatErrorMessage(error) }), processingTime, null);
        broadcastAIThinkingEnd(aiRequestId);
        clearPersistedThinkingState(aiRequestId);
        pendingMessages = [];
    } finally {
        isAIThinking = false;
        updateInputState(chatInput, sendButton);
        if (pendingMessages.length > 0) {
            void sendToAI(chatInput, sendButton);
        }
    }
}

async function startScenario(): Promise<void> {
    const room = getCurrentRoom();
    if (!room || room.invisible_view) {
        showNotification("请先进入可操作的房间", "error");
        return;
    }
    if (!canManageCurrentRoom()) {
        showNotification("只有房主或房间管理员可以开启剧本", "error");
        return;
    }
    if (room.scenario_started_at) return;
    if (isAIThinking || pendingMessages.length > 0) {
        showNotification("请等待当前 AI 回复完成后再开启剧本", "info");
        return;
    }

    const chatInput = document.getElementById("chatInput") as HTMLInputElement | null;
    const sendButton = document.getElementById("sendButton") as HTMLButtonElement | null;
    const startButton = document.getElementById("startScenario") as HTMLButtonElement | null;
    if (!chatInput || !sendButton) return;

    const role = aiRoles.find((item) => item.id === "kp") || aiRoles[0] || { id: "kp", name: "KP" };
    pendingMessages.push({
        sender: "系统",
        content: "@KP 开启当前房间剧本",
        role,
        time: new Date().toLocaleTimeString(),
    });
    if (startButton) startButton.disabled = true;
    await sendToAI(chatInput, sendButton, { scenarioStart: true });
    if (startButton && !room.scenario_started_at) startButton.disabled = false;
}

function updateInputState(chatInput: HTMLInputElement, sendButton: HTMLButtonElement): void {
    const lockInput = document.getElementById("enableAIResponseLock") as HTMLInputElement | null;
    const locked = isAIThinking && Boolean(lockInput?.checked);
    chatInput.disabled = chatReadOnly || locked;
    sendButton.disabled = chatReadOnly || locked;
}

function setChatReadOnly(readOnly: boolean): void {
    chatReadOnly = readOnly;
    const chatInput = document.getElementById("chatInput") as HTMLInputElement | null;
    const sendButton = document.getElementById("sendButton") as HTMLButtonElement | null;
    if (chatInput && sendButton) updateInputState(chatInput, sendButton);
}

function isCurrentUserAdmin(): boolean {
    return ["ADMIN", "OWNER"].includes(window.currentUser?.role || "");
}

function canManageCurrentRoom(): boolean {
    if (isCurrentUserAdmin()) return true;
    const room = getCurrentRoom();
    if (!room) return false;
    if (String(room.creator_id ?? room.owner_id ?? "") === String(getCurrentUserId() ?? "")) return true;
    const member = room.members?.find((item) => String(item.user_id) === String(getCurrentUserId() ?? ""));
    return member?.room_role === "owner" || member?.room_role === "admin";
}

async function handleTriggerCommand(command: string): Promise<ChatMessage | null> {
    if (!canManageCurrentRoom()) {
        showNotification("只有房主或房间管理员可以触发场景内容", "error");
        return null;
    }

    const room = getCurrentRoom();
    if (!room?.id) {
        showNotification("请先进入房间", "error");
        return null;
    }

    const parts = command.trim().split(/\s+/);
    const triggerId = Number.parseInt(parts[1] || "", 10);
    if (!Number.isInteger(triggerId) || triggerId < 1 || parts[1] !== String(triggerId)) {
        showNotification("用法：/trigger {正整数触发器编号}", "error");
        return null;
    }

    try {
        const response = await TrpgApi.post<ApiResponse<ChatMessage>>(`/api/rooms/${encodeURIComponent(room.id)}/triggers`, {
            trigger_id: triggerId,
        });
        if (!response.success || !response.data) {
            showNotification(response.error || response.message || "触发器执行失败", "error");
            return null;
        }
        return response.data;
    } catch (error) {
        showNotification(`触发器执行失败：${chatErrorMessage(error)}`, "error");
        return null;
    }
}

async function handleRecordCommand(command: string): Promise<string> {
    if (!isCurrentUserAdmin()) {
        return "只有管理员可以使用 /record 命令";
    }
    const currentRoom = getCurrentRoom();
    if (!currentRoom?.name) {
        return "请先进入房间后再使用 /record 命令";
    }
    const parts = command.trim().split(/\s+/);
    const type = (parts[1] || "").toLowerCase();
    const username = parts[2] || "";
    const valueText = parts[3] || "";
    const validationError = validateRecordCommandParts(type, username, valueText);
    if (validationError) return validationError;
    const value = Number.parseInt(valueText, 10);
    const reason = parts.slice(4).join(" ") || "未知";

    const result = await window.recordCharacterChange?.({
        roomName: currentRoom.name,
        username,
        type,
        value,
        reason,
    });
    if (!result) return "记录失败";
    return `已记录 ${username} ${type} ${value}，原因：${reason}`;
}

function validateRecordCommandParts(type: string, username: string, valueText: string): string | null {
    if (type !== "damage" && type !== "san") {
        return "第 1 个参数必须是 {damage/san}";
    }
    if (!username) {
        return "第 2 个参数必须是 {username}";
    }
    if (!/^[1-9]\d*$/.test(valueText)) {
        return "第 3 个参数必须是正整数 {int}";
    }
    return null;
}

function getRecordCommandDraftHint(value: string): string {
    const parts = value.trim().split(/\s+/);
    const type = (parts[1] || "").toLowerCase();
    const username = parts[2] || "";
    const valueText = parts[3] || "";
    if (!parts[1]) return "下一项：{damage/san}";
    if (type !== "damage" && type !== "san") return "第 1 个参数必须是 {damage/san}";
    if (!parts[2]) return "下一项：{username}";
    if (!username) return "第 2 个参数必须是 {username}";
    if (!parts[3]) return "下一项：{int}";
    if (!/^[1-9]\d*$/.test(valueText)) return "第 3 个参数必须是正整数 {int}";
    return "可选：{reason?}";
}

function initCommandPalette(chatInput: HTMLInputElement): void {
    if (document.getElementById("commandPalette")) return;
    const palette = document.createElement("div");
    palette.id = "commandPalette";
    palette.className = "command-palette list-group shadow-sm";
    palette.style.display = "none";
    chatInput.parentElement?.appendChild(palette);

    chatInput.addEventListener("input", () => showCommandPalette(chatInput, palette));
    chatInput.addEventListener("blur", () => window.setTimeout(() => {
        palette.style.display = "none";
    }, 120));
}

function showCommandPalette(chatInput: HTMLInputElement, palette: HTMLElement): void {
    const value = chatInput.value.trimStart();
    if (!value.startsWith("/")) {
        palette.style.display = "none";
        return;
    }
    const commandName = value.split(/\s+/)[0]?.toLowerCase() || "";
    const matches = COMMAND_DEFINITIONS.filter((command) => command.name.startsWith(commandName));
    if (matches.length === 0) {
        palette.style.display = "none";
        return;
    }
    palette.innerHTML = matches.map((command) => {
        const hint = command.name === "/record" ? getRecordCommandDraftHint(value) : "";
        const hintHtml = hint ? window.TrpgTemplates.render("chat-command-palette-hint", { hint }) : "";
        return window.TrpgTemplates.render("chat-command-palette-item", {
            commandName: command.name,
            usage: command.usage,
            description: command.description,
            hintHtml,
        });
    }).join("");
    palette.querySelectorAll<HTMLButtonElement>("[data-command]").forEach((button) => {
        button.addEventListener("mousedown", (event) => {
            event.preventDefault();
            chatInput.value = `${button.dataset.command || ""} `;
            palette.style.display = "none";
            chatInput.focus();
        });
    });
    palette.style.display = "block";
}

async function sendVisibleMessage(
    type: string,
    content: string,
    metadata: Record<string, unknown> = {},
): Promise<ChatMessage | null> {
    const room = getCurrentRoom();
    if (room) {
        if (room.invisible_view) return null;
        const message = await persistRoomMessage(type, content, metadata);
        if (message) {
            renderRoomMessage(message);
            broadcastMessage(message);
        }
        return message;
    }

    const sender = type === "player" ? getCurrentUsername() : defaultSenderName(type);
    addMessage(type, sender, content);
    broadcastMessage({
        type,
        sender_name: sender,
        content,
        // 头像留空：由各客户端按发送者名称统一渲染文字头像，避免把生成的 data URI 写进消息记录。
        avatar: "",
    });
    return null;
}

async function persistDirectRoomMessage(message: ChatMessage): Promise<ChatMessage | null> {
    if (!message.content) return null;
    const room = getCurrentRoom();
    if (!room) return message;
    if (room.invisible_view) return null;
    const response = await TrpgApi.post<ApiResponse<ChatMessage>>(`/api/rooms/${room.id}/messages`, {
        type: message.type || "trigger",
        content: message.content,
        sender_name: message.sender_name || message.sender || "触发器",
        avatar: message.avatar,
        metadata: message.metadata || {},
    });
    if (!response.success) throw new Error(response.message || "保存直接消息失败");
    return response.data || null;
}

async function persistRoomMessage(type: string, content: string, metadata: Record<string, unknown> = {}): Promise<ChatMessage | null> {
    const room = getCurrentRoom();
    if (!room || room.invisible_view) return null;

    const payload: ChatMessage = {
        type,
        content,
        metadata,
    };
    const senderName = typeof metadata.senderName === "string" ? metadata.senderName : null;
    if (senderName) payload.sender_name = senderName;

    const data = await TrpgApi.post<ApiResponse<ChatMessage>>(`/api/rooms/${room.id}/messages`, payload);
    if (!data.success) throw new Error(data.message || "保存房间消息失败");
    return data.data || null;
}

function updateAIHint(): void {
    const aiHint = document.getElementById("aiHint");
    if (aiHint) aiHint.textContent = `@${aiName}`;
}

async function loadAIRoles(): Promise<void> {
    try {
        const response = await TrpgApi.get<ApiResponse<{ roles: ChatRoleConfig[] }>>("/api/config/roles");
        if (response.success && response.data?.roles?.length) {
            aiRoles = response.data.roles;
            aiName = aiRoles[0]?.name || "KP";
            updateAIHint();
        }
    } catch (error) {
        console.warn("加载 AI 角色配置失败，使用默认 KP 角色:", error);
    }
}

function findRoleForMessage(message: string): ChatRoleConfig | undefined {
    const trimmedMessage = message.trim();
    return aiRoles.find((role) => (role.wake_words || []).some((wakeWord) => trimmedMessage.startsWith(wakeWord)));
}

// 消息正文的净化策略：保留 Markdown 排版、图片与视频预览需要的标签，
// 其余可承载脚本的标签一律剔除，所有 on* 事件属性由净化库默认拦截。
// 说明：净化只作用于「显示」，消息原文仍原样入库并原样送入 AI，
// 因此不会改变提示词注入的判定，也不会影响 KP 的正常输出。
const CHAT_SANITIZE_CONFIG: Record<string, unknown> = {
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "base", "meta", "link"],
    ADD_ATTR: ["controls", "preload", "poster", "playsinline", "target", "rel", "loading"],
};

function renderMarkdown(content: string): string {
    const parser = window.marked;
    const purifier = window.DOMPurify;
    if (!parser || !purifier) {
        // 渲染或净化组件不可用时降级为纯文本：宁可丢掉排版，也不放行未经净化的 HTML。
        console.warn("Markdown 渲染或净化组件未就绪，消息以纯文本显示");
        return chatEscapeHtml(content);
    }
    try {
        parser.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
        return purifier.sanitize(parser.parse(content), CHAT_SANITIZE_CONFIG);
    } catch (error) {
        console.error("Markdown 渲染失败:", error);
        return chatEscapeHtml(content);
    }
}

// 头像解析：优先用消息自带头像；玩家消息若属于当前登录用户，沿用导航栏头像，
// 其余情况一律回退为「发送者名称首字」文字头像，项目已不再有内置默认头像图。
function getAvatarSrc(type: string, message: ChatMessage | null, sender: string): string {
    if (message?.avatar) return message.avatar;
    if (type === "player" && sender === getCurrentUsername()) {
        const currentSrc = document.querySelector<HTMLImageElement>(".user-avatar img")?.src;
        if (currentSrc) return currentSrc;
    }
    return TrpgAvatar.avatarTextDataUri(sender);
}

function defaultSenderName(type: string): string {
    switch (type) {
        case "kp": return "KP";
        case "dice": return "骰娘";
        case "system": return "系统";
        default: return getCurrentUsername();
    }
}

function getMessageClass(type: string): string {
    switch (type) {
        case "player": return "player-message";
        case "kp": return "kp-message";
        case "dice": return "dice-message";
        case "system": return "other-message";
        case "other": return "other-message";
        default: return "other-message";
    }
}

const CHAT_VIDEO_EXTENSIONS = new Set(["mp4", "webm", "ogg", "ogv", "mov", "m4v", "mkv"]);
const CHAT_IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "avif"]);

/** 根据 URL 后缀判断媒体类型，用于把聊天里的图片/视频渲染成缩略图或内嵌播放器。 */
function chatMediaKind(url: string): "video" | "image" | null {
    const clean = (url || "").split(/[?#]/)[0]?.toLowerCase() ?? "";
    const extension = clean.includes(".") ? clean.slice(clean.lastIndexOf(".") + 1) : "";
    if (CHAT_VIDEO_EXTENSIONS.has(extension)) return "video";
    if (CHAT_IMAGE_EXTENSIONS.has(extension)) return "image";
    return null;
}

let chatMediaLightbox: HTMLElement | null = null;

/** 网页内媒体预览浮层：图片/视频都在当前页面内放大显示，不跳转、不下载。 */
function ensureChatMediaLightbox(): HTMLElement {
    if (chatMediaLightbox && document.body.contains(chatMediaLightbox)) return chatMediaLightbox;
    const overlay = document.createElement("div");
    overlay.className = "chat-media-lightbox";
    overlay.hidden = true;
    overlay.innerHTML = `<button type="button" class="chat-media-lightbox-close" aria-label="关闭预览">&times;</button>`
        + `<div class="chat-media-lightbox-body"></div>`;
    const handleClose = (): void => closeChatMediaLightbox();
    overlay.addEventListener("click", (event) => {
        const target = event.target as HTMLElement | null;
        if (event.target === overlay || target?.classList.contains("chat-media-lightbox-close")) handleClose();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !overlay.hidden) handleClose();
    });
    document.body.appendChild(overlay);
    chatMediaLightbox = overlay;
    return overlay;
}

function openChatMediaLightbox(url: string, kind: "video" | "image"): void {
    if (!url) return;
    const overlay = ensureChatMediaLightbox();
    const body = overlay.querySelector<HTMLElement>(".chat-media-lightbox-body");
    if (!body) return;
    body.innerHTML = "";
    if (kind === "video") {
        const video = document.createElement("video");
        video.src = url;
        video.controls = true;
        video.autoplay = true;
        video.playsInline = true;
        video.className = "chat-media-lightbox-video";
        body.appendChild(video);
    } else {
        const image = document.createElement("img");
        image.src = url;
        image.alt = "图片预览";
        image.className = "chat-media-lightbox-image";
        body.appendChild(image);
    }
    overlay.hidden = false;
    document.body.classList.add("chat-media-lightbox-open");
}

function closeChatMediaLightbox(): void {
    const overlay = chatMediaLightbox;
    if (!overlay) return;
    const body = overlay.querySelector<HTMLElement>(".chat-media-lightbox-body");
    if (body) body.innerHTML = "";
    overlay.hidden = true;
    document.body.classList.remove("chat-media-lightbox-open");
}

/** 后处理消息 HTML：图片显示为缩略图、视频链接替换为内嵌播放器，并统一绑定放大预览。 */
function enhanceMessageMedia(container: HTMLElement): void {
    container.querySelectorAll<HTMLImageElement>("img").forEach((image) => {
        image.classList.add("chat-media-thumb");
        image.loading = "lazy";
        if (image.dataset.mediaBound === "1") return;
        image.dataset.mediaBound = "1";
        image.addEventListener("click", () => openChatMediaLightbox(image.currentSrc || image.src, "image"));
    });

    container.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((anchor) => {
        const href = anchor.getAttribute("href") || "";
        if (chatMediaKind(href) !== "video" || anchor.dataset.mediaBound === "1") return;
        anchor.dataset.mediaBound = "1";
        const wrapper = document.createElement("div");
        wrapper.className = "chat-media-video";
        const video = document.createElement("video");
        video.src = href;
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.className = "chat-media-video-player";
        const expand = document.createElement("button");
        expand.type = "button";
        expand.className = "chat-media-video-expand";
        expand.title = "放大播放";
        expand.setAttribute("aria-label", "放大播放");
        expand.textContent = "放大";
        expand.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            openChatMediaLightbox(href, "video");
        });
        wrapper.appendChild(video);
        wrapper.appendChild(expand);
        anchor.replaceWith(wrapper);
    });
}

function addMessage(
    type: string,
    sender: string,
    content: string,
    messageId: string | number | null = null,
    isThinking = false,
    processingTime: number | null = null,
    tokenCount: number | null = null,
    cacheHitRate: number | null = null,
    message: ChatMessage | null = null,
): string | number {
    const resolvedMessageId = messageId || Date.now();
    const chatHistory = document.getElementById("chatHistory");
    if (!chatHistory) return resolvedMessageId;

    hideWelcomeText();
    let messageClass = getMessageClass(type);
    if (isThinking) messageClass += " thinking";

    const messageDiv = document.createElement("div");
    messageDiv.className = `message ${messageClass}`;
    messageDiv.setAttribute("data-id", String(resolvedMessageId));
    if (message?.sender_id !== undefined && message.sender_id !== null) {
        messageDiv.setAttribute("data-sender-id", String(message.sender_id));
    }
    if (message?.avatar) messageDiv.setAttribute("data-avatar", message.avatar);

    const displayTime = message?.time || message?.timestamp || new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const renderedContent = isThinking ? renderThinkingContent(content) : renderMarkdown(content);
    const avatarSrc = getAvatarSrc(type, message, sender);

    messageDiv.innerHTML = window.TrpgTemplates.render("chat-message", {
        avatarSrc,
        sender,
        displayTime,
        contentHtml: renderedContent,
        processingHtml: renderProcessingTime(type, processingTime, tokenCount, cacheHitRate),
        knowledgeHtml: type === "kp" ? renderKnowledgeUsage(message?.metadata?.knowledgeUsage as KnowledgeUsage | undefined) : "",
    });
    const renderedContentElement = messageDiv.querySelector<HTMLElement>(".message-content");
    if (renderedContentElement) enhanceMessageMedia(renderedContentElement);
    chatHistory.appendChild(messageDiv);
    chatHistory.scrollTop = chatHistory.scrollHeight;
    return resolvedMessageId;
}

function renderProcessingTime(type: string, processingTime: number | null, tokenCount: number | null, cacheHitRate: number | null = null): string {
    if (processingTime === null || type !== "kp") return "";
    return window.TrpgTemplates.render("chat-processing-time", { text: processingTimeText(processingTime, tokenCount, cacheHitRate) });
}

function addThinkingMessage(messageId: string | number, roleName = "KP", startedAt = Date.now()): void {
    addMessage("kp", roleName, "AI 正在思考中...", messageId, true, 0);
    const messageElement = document.querySelector<HTMLElement>(`.message[data-id="${String(messageId)}"]`);
    if (messageElement) {
        messageElement.setAttribute("data-ai-request-id", String(messageId));
        messageElement.setAttribute("data-started-at", String(startedAt));
    }
    startThinkingElapsedTimer(String(messageId), startedAt);
}

function renderKnowledgeUsage(usage?: KnowledgeUsage): string {
    if (!usage) return "";
    const parts: string[] = [];
    if (usage.ruleset.called) parts.push(`规则书 ${usage.ruleset.chunks} 段`);
    else parts.push("规则书未调用");
    if (usage.scenario.chunks > 0) parts.push(`剧本 ${usage.scenario.chunks} 段`);
    if (!usage.used && !usage.ruleset.called) parts.splice(0, parts.length, "本次未调用知识库");
    if (usage.cached) parts.push("缓存结果");
    const citations = usage.ruleset.citations || [];
    return window.TrpgTemplates.render("chat-knowledge-usage", {
        text: `知识库：${parts.join(" · ")}`,
        title: citations.length > 0 ? `规则书来源：${citations.join("；")}` : "显示本次回复实际检索的知识库用量",
    });
}

function renderThinkingContent(label: string): string {
    return `<div class="thinking-content" aria-live="polite" data-thinking-content>`
        + `<span class="thinking-animation" aria-hidden="true"><span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></span>`
        + `<span class="thinking-text" data-thinking-label>${chatEscapeHtml(label)}</span>`
        + `</div>`;
}

// KP 回复的渐进显现：内容已一次性生成，这里把渲染好的 HTML 逐字显示，
// 避免整段回复「一下子蹦出来」。受管理员「启用 AI 流式输出」开关控制，
// 并尊重系统的「减少动态效果」偏好。
const kpRevealFrames = new WeakMap<HTMLElement, number>();

function kpStreamRevealEnabled(): boolean {
    if (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        return false;
    }
    return window.configManager?.get<boolean>("general", "ai", "stream_output", true) !== false;
}

function revealKpContent(container: HTMLElement): void {
    if (!kpStreamRevealEnabled()) return;
    const pendingFrame = kpRevealFrames.get(container);
    if (pendingFrame !== undefined) window.cancelAnimationFrame(pendingFrame);

    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const textNodes: Text[] = [];
    let totalLength = 0;
    while (walker.nextNode()) {
        const node = walker.currentNode as Text;
        textNodes.push(node);
        totalLength += (node.nodeValue || "").length;
    }
    if (textNodes.length === 0 || totalLength === 0) return;

    const fullTexts = textNodes.map((node) => node.nodeValue || "");
    textNodes.forEach((node) => { node.nodeValue = ""; });

    // 短文本快速显现，长文本限速，整体时长控制在 0.3s ~ 2.6s。
    const duration = Math.min(2600, Math.max(300, totalLength * 12));
    const chatHistory = document.getElementById("chatHistory");
    const startedAt = performance.now();
    let revealed = 0;

    const paint = (count: number): void => {
        let remaining = count;
        for (let index = 0; index < textNodes.length; index += 1) {
            const node = textNodes[index];
            if (!node) continue;
            const text = fullTexts[index] || "";
            node.nodeValue = text.slice(0, Math.max(0, Math.min(text.length, remaining)));
            remaining -= text.length;
        }
    };

    const tick = (): void => {
        const elapsed = performance.now() - startedAt;
        const target = Math.min(totalLength, Math.round((elapsed / duration) * totalLength));
        if (target > revealed) {
            revealed = target;
            paint(revealed);
            if (chatHistory) chatHistory.scrollTop = chatHistory.scrollHeight;
        }
        if (revealed >= totalLength) {
            kpRevealFrames.delete(container);
            return;
        }
        kpRevealFrames.set(container, window.requestAnimationFrame(tick));
    };
    kpRevealFrames.set(container, window.requestAnimationFrame(tick));
}

function updateThinkingStage(aiRequestId: string, label: string, stage = ""): void {
    const messageElement = document.querySelector<HTMLElement>(`.message.thinking.kp-message[data-ai-request-id="${aiRequestId}"]`);
    if (!messageElement) return;
    const labelElement = messageElement.querySelector<HTMLElement>("[data-thinking-label]");
    if (labelElement && label) labelElement.textContent = label;
    if (stage) messageElement.setAttribute("data-thinking-stage", stage);
}

function replaceThinkingMessage(messageId: string | number, newContent: string, processingTime: number, tokenCount: number | null, cacheHitRate: number | null = null, knowledgeUsage?: KnowledgeUsage): void {
    stopThinkingElapsedTimer(String(messageId));
    const targetMessage = document.querySelector<HTMLElement>(`.message.thinking.kp-message[data-ai-request-id="${String(messageId)}"]`)
        || document.querySelector<HTMLElement>(`.message[data-id="${messageId}"]`);

    if (!targetMessage) {
        addMessage("kp", "KP", newContent, null, false, processingTime, tokenCount, cacheHitRate, { content: newContent, metadata: { knowledgeUsage } });
        return;
    }

    const contentDiv = targetMessage.querySelector<HTMLElement>(".message-content");
    if (contentDiv) {
        contentDiv.innerHTML = renderMarkdown(newContent);
        contentDiv.className = "message-content markdown-body";
        enhanceMessageMedia(contentDiv);
        revealKpContent(contentDiv);
    }
    targetMessage.classList.remove("thinking");

    const metaContainer = targetMessage.querySelector<HTMLElement>(".message-meta")
        || targetMessage.querySelector<HTMLElement>(".message-content-container");
    let processingTimeDiv = targetMessage.querySelector<HTMLElement>(".processing-time");
    if (!processingTimeDiv) {
        processingTimeDiv = document.createElement("div");
        processingTimeDiv.className = "processing-time";
        metaContainer?.appendChild(processingTimeDiv);
    }

    processingTimeDiv.textContent = processingTimeText(processingTime, tokenCount, cacheHitRate);
    targetMessage.querySelector(".knowledge-usage")?.remove();
    const knowledgeHtml = renderKnowledgeUsage(knowledgeUsage);
    if (knowledgeHtml) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = knowledgeHtml;
        const knowledgeElement = wrapper.firstElementChild;
        if (knowledgeElement) metaContainer?.appendChild(knowledgeElement);
    }

    const chatHistory = document.getElementById("chatHistory");
    if (chatHistory) chatHistory.scrollTop = chatHistory.scrollHeight;
}

function processingTimeText(processingTime: number, tokenCount: number | null = null, cacheHitRate: number | null = null): string {
    let displayText = `已耗时: ${processingTime}秒`;
    if (tokenCount !== null) displayText += ` 消耗Token：${tokenCount}`;
    if (cacheHitRate !== null) displayText += ` 缓存命中率：${cacheHitRate}%`;
    return displayText;
}

function startThinkingElapsedTimer(aiRequestId: string, startedAt: number): void {
    stopThinkingElapsedTimer(aiRequestId);
    updateThinkingElapsed(aiRequestId, startedAt);
    const timerId = window.setInterval(() => updateThinkingElapsed(aiRequestId, startedAt), 1000);
    thinkingTimers.set(aiRequestId, timerId);
}

function stopThinkingElapsedTimer(aiRequestId: string): void {
    const timerId = thinkingTimers.get(aiRequestId);
    if (timerId !== undefined) window.clearInterval(timerId);
    thinkingTimers.delete(aiRequestId);
}

function updateThinkingElapsed(aiRequestId: string, startedAt: number): void {
    const messageElement = document.querySelector<HTMLElement>(`.message.thinking.kp-message[data-ai-request-id="${aiRequestId}"]`);
    if (!messageElement) {
        stopThinkingElapsedTimer(aiRequestId);
        return;
    }
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    let processingTimeDiv = messageElement.querySelector<HTMLElement>(".processing-time");
    if (!processingTimeDiv) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = renderProcessingTime("kp", elapsedSeconds, null);
        processingTimeDiv = wrapper.firstElementChild as HTMLElement | null;
        if (processingTimeDiv) messageElement.querySelector(".message-content-container")?.appendChild(processingTimeDiv);
    } else {
        processingTimeDiv.textContent = processingTimeText(elapsedSeconds);
    }
}

function clearThinkingMessage(aiRequestId: string): void {
    stopThinkingElapsedTimer(aiRequestId);
    document.querySelector<HTMLElement>(`.message.thinking.kp-message[data-ai-request-id="${aiRequestId}"]`)?.remove();
    clearPersistedThinkingState(aiRequestId);
}

function persistThinkingState(state: { id: string; roomId: string | null; roleName: string; roleId?: string; content?: string; startedAt: number; scenarioStart?: boolean }): void {
    try { localStorage.setItem(THINKING_STORAGE_KEY, JSON.stringify(state)); } catch { /* storage may be unavailable */ }
}

function clearPersistedThinkingState(id?: string): void {
    try {
        const raw = localStorage.getItem(THINKING_STORAGE_KEY);
        if (!id || !raw || JSON.parse(raw)?.id === id) localStorage.removeItem(THINKING_STORAGE_KEY);
    } catch { localStorage.removeItem(THINKING_STORAGE_KEY); }
}

function restoreThinkingState(): void {
    try {
        const state = JSON.parse(localStorage.getItem(THINKING_STORAGE_KEY) || "null");
        const roomId = getCurrentRoom()?.id || null;
        if (!state?.id || state.roomId !== roomId) return;
        // 一并恢复 scenarioStart 标志，供断线重连时决定是否按「开启剧本」重发。
        restoredScenarioStart = state.scenarioStart === true;
        if (state.content && pendingMessages.length === 0) {
            const role = aiRoles.find((item) => item.id === state.roleId) || aiRoles[0] || { id: state.roleId || "kp", name: state.roleName || "KP" };
            pendingMessages.push({ sender: getCurrentUsername(), content: String(state.content), role, time: new Date().toLocaleTimeString() });
        }
        addThinkingMessage(state.id, state.roleName || "KP", Number(state.startedAt) || Date.now());
    } catch { /* ignore malformed persisted state */ }
}

function resumePendingAIRequest(): void {
    if (isAIThinking || pendingMessages.length === 0) return;
    // 仅当被持久化的确实是剧本开始请求时，才允许带 scenarioStart 重发；
    // 普通消息恢复后保持原样，避免误触发开场。
    const scenarioStart = restoredScenarioStart;
    if (scenarioStart && !canManageCurrentRoom()) {
        // 同一浏览器共享 localStorage：非房主/管理员（如新加入的玩家）不能凭
        // 残留状态开启剧本，直接清除并提示。
        clearPersistedThinkingState();
        restoredScenarioStart = false;
        pendingMessages = [];
        showNotification("只有房主或管理员可以开启剧本", "error");
        return;
    }
    try {
        const state = JSON.parse(localStorage.getItem(THINKING_STORAGE_KEY) || "null");
        if (state?.id) clearThinkingMessage(String(state.id));
    } catch { /* ignore */ }
    const chatInput = document.getElementById("chatInput") as HTMLInputElement | null;
    const sendButton = document.getElementById("sendButton") as HTMLButtonElement | null;
    if (chatInput && sendButton && getCurrentRoom()) void sendToAI(chatInput, sendButton, scenarioStart ? { scenarioStart: true } : {});
}

function moveThinkingMessageToEnd(messageId: string | number): void {
    const chatHistory = document.getElementById("chatHistory");
    const targetMessage = document.querySelector<HTMLElement>(`.message.thinking.kp-message[data-ai-request-id="${String(messageId)}"]`);
    if (!chatHistory || !targetMessage || targetMessage.parentElement !== chatHistory) return;
    chatHistory.appendChild(targetMessage);
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

function getCurrentChatMessages(): ChatMessage[] {
    const chatHistory = document.getElementById("chatHistory");
    if (!chatHistory) return [];

    return Array.from(chatHistory.querySelectorAll<HTMLElement>(".message")).map((messageElement) => {
        const sender = messageElement.querySelector(".message-sender")?.textContent || "未知";
        const content = messageElement.querySelector(".message-content")?.innerHTML || "";
        const time = messageElement.querySelector(".message-time")?.textContent || "";
        const avatar = messageElement.getAttribute("data-avatar");
        let role = "user";
        if (messageElement.classList.contains("kp-message")) role = "assistant";
        if (messageElement.classList.contains("dice-message")) role = "system";
        const message: ChatMessage = {
            role,
            sender,
            sender_id: messageElement.getAttribute("data-sender-id"),
            content,
            time,
        };
        if (avatar) message.avatar = avatar;
        return message;
    });
}

function renderChatMessages(messages: ChatMessage[]): void {
    const chatHistory = document.getElementById("chatHistory");
    if (!chatHistory) return;

    chatHistory.innerHTML = "";
    hideWelcomeText();
    clearActionSuggestions(false);
    messages.forEach((message) => renderRoomMessage(message));
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

function renderRoomMessage(message: ChatMessage | null, animate = false): void {
    if (!message) return;
    if (message.id) {
        const duplicate = Array.from(document.querySelectorAll<HTMLElement>("#chatHistory .message[data-id]"))
            .some((element) => element.dataset.id === String(message.id));
        if (duplicate) return;
    }
    const aiRequestId = typeof message.metadata?.aiRequestId === "string" ? message.metadata.aiRequestId : null;
    if (aiRequestId) clearThinkingMessage(aiRequestId);
    const isOwnPlayerMessage = message.type === "player" && message.sender_id === getCurrentUserId();
    const type = message.type === "player" && !isOwnPlayerMessage ? "other" : message.type || "other";
    const metadata = message.metadata || {};
    if (metadata.check_type === "sanity" && metadata.player_name && window.currentRoom?.members) {
        const member = window.currentRoom.members.find((item) => String(item.username || "").toLocaleLowerCase() === String(metadata.player_name).toLocaleLowerCase());
        if (member) {
            member.character_state = { ...(member.character_state || {}), current_san: Number(metadata.current_san), max_san: Number(metadata.max_san) };
            if (member.character_card) {
                member.character_card.currentSan = Number(metadata.current_san);
                (member.character_card as Partial<COC7CharacterCard> & { current_san?: number }).current_san = Number(metadata.current_san);
            }
        }
    }
    const displayMessage = message.type === "trigger" && metadata.asset_url && message.content && !message.content.includes(String(metadata.asset_url))
        ? { ...message, content: `${message.content}\n\n[${metadata.asset_name || "附件"}](${metadata.asset_url})` }
        : message;
    const renderedId = addMessage(
        type,
        displayMessage.sender_name || displayMessage.sender || defaultSenderName(type),
        displayMessage.content,
        displayMessage.id || null,
        false,
        numericMetadata(metadata, "processingTime") ?? numericMetadata(metadata, "processing_time"),
        numericMetadata(metadata, "tokenCount") ?? numericMetadata(metadata, "token_count"),
        numericMetadata(metadata, "cacheHitRate") ?? numericMetadata(metadata, "cache_hit_rate"),
        displayMessage,
    );
    // 仅对实时到达的 KP 消息做渐进显现；历史记录一次性铺满，避免回看时跳动。
    if (animate && type === "kp") {
        const contentElement = document.querySelector<HTMLElement>(`#chatHistory .message[data-id="${String(renderedId)}"] .message-content`);
        if (contentElement) revealKpContent(contentElement);
    }
}

function clearChatMessages(): void {
    const chatHistory = document.getElementById("chatHistory");
    if (!chatHistory) return;
    chatHistory.innerHTML = window.TrpgTemplates.render("chat-welcome");
    clearActionSuggestions(false);
}

function hideWelcomeText(): void {
    const welcomeText = document.querySelector<HTMLElement>(".welcome-text");
    if (welcomeText) welcomeText.style.display = "none";
}

function initWebSocket(): void {
    if (!window.currentUser) return;
    if (socket?.connected) return;
    try {
        socket = io();
        socket.on("connect", () => {
            const room = getCurrentRoom();
            if (room && !room.invisible_view) socket?.emit("join_room", { room_id: room.id });
        });
        socket.on("session_expired", () => {
            showNotification("当前账号已在其他会话登录，请重新登录。", "error");
            disconnectSocket();
            window.clearCurrentRoom?.();
            window.clearChatMessages?.();
            window.currentUser = null;
            showAuthModal();
        });
        socket.on("new_message", (data) => handleIncomingMessage(data));
        socket.on("user_typing", (data) => handleUserTyping(data));
        socket.on("room_members_changed", () => { void window.refreshCurrentRoomMembers?.(); });
        socket.on("room_autosave_created", () => { void window.refreshRoomNodes?.(); });
        socket.on("role_changed", (data) => handleRoleChanged(data));
        // 平台自动切换等仅面向管理员的提示：服务端只推给在线管理员，前端以通知条展示（不用 alert）。
        socket.on("admin_notification", (payload: unknown) => {
            const data = (payload || {}) as { message?: unknown; level?: unknown };
            const message = typeof data.message === "string" ? data.message : "";
            if (message) showNotification(message, data.level === "error" ? "error" : "warning");
        });
    } catch (error) {
        console.warn("WebSocket 连接失败，消息同步不可用:", error);
    }
}

function reconnectSocket(): void {
    disconnectSocket();
    initWebSocket();
}

function disconnectSocket(): void {
    if (socket) {
        socket.disconnect();
        socket = null;
    }
}

function joinSocketRoom(roomId: string): void {
    if (socket?.connected && roomId && !getCurrentRoom()?.invisible_view) socket.emit("join_room", { room_id: roomId });
}

function leaveSocketRoom(roomId: string): void {
    clearTypingIndicator();
    if (socket?.connected && roomId) socket.emit("leave_room", { room_id: roomId });
}

function broadcastMessage(message: ChatMessage | null): void {
    if (!socket?.connected || !message) return;
    socket.emit("send_message", {
        room_id: getCurrentRoom()?.id || null,
        message,
    });
}

function broadcastAIThinkingStart(aiRequestId: string, roleName: string, startedAt: number): void {
    if (!socket?.connected) return;
    socket.emit("send_message", {
        room_id: getCurrentRoom()?.id || null,
        type: "ai_thinking_start",
        aiRequestId,
        roleName,
        startedAt,
    });
}

function broadcastAIThinkingEnd(aiRequestId: string): void {
    if (!socket?.connected) return;
    socket.emit("send_message", {
        room_id: getCurrentRoom()?.id || null,
        type: "ai_thinking_end",
        aiRequestId,
    });
}

function showActionSuggestions(actions: string[], ownerId: string | number | null): void {
    const container = document.getElementById("aiSuggestions");
    if (!container) return;
    const cleaned = (Array.isArray(actions) ? actions : [])
        .map((item) => String(item ?? "").trim())
        .filter((item) => item.length > 0);
    if (cleaned.length === 0) {
        clearActionSuggestions(false);
        return;
    }
    const isOwner = ownerId === null || ownerId === undefined
        || String(ownerId) === String(getCurrentUserId() ?? "");
    container.innerHTML = "";
    cleaned.forEach((action) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-sm chat-suggestion-btn";
        button.textContent = action;
        if (isOwner) {
            button.addEventListener("click", () => applyActionSuggestion(action));
        } else {
            button.disabled = true;
            button.classList.add("chat-suggestion-btn-readonly");
            button.title = "仅发起本次 AI 请求的玩家可以选择";
        }
        container.appendChild(button);
    });
    container.hidden = false;
    activeSuggestions = cleaned;
}

function clearActionSuggestions(broadcast: boolean): void {
    const container = document.getElementById("aiSuggestions");
    if (container) {
        container.innerHTML = "";
        container.hidden = true;
    }
    const hadSuggestions = activeSuggestions !== null;
    activeSuggestions = null;
    if (broadcast && hadSuggestions) broadcastActionSuggestionsClear();
}

function applyActionSuggestion(action: string): void {
    const chatInput = document.getElementById("chatInput") as HTMLInputElement | null;
    const sendButton = document.getElementById("sendButton") as HTMLButtonElement | null;
    if (!chatInput || !sendButton) return;
    const wakeWord = aiRoles[0]?.wake_words?.[0] || "@KP";
    chatInput.value = `${wakeWord} ${action}`;
    clearActionSuggestions(true);
    sendButton.click();
}

function broadcastActionSuggestions(actions: string[], ownerId: string | number | null): void {
    if (!socket?.connected) return;
    socket.emit("send_message", {
        room_id: getCurrentRoom()?.id || null,
        type: "ai_suggestions",
        suggestions: actions,
        ownerId,
    });
}

function broadcastActionSuggestionsClear(): void {
    if (!socket?.connected) return;
    socket.emit("send_message", {
        room_id: getCurrentRoom()?.id || null,
        type: "ai_suggestions_clear",
    });
}

function handleIncomingMessage(data: unknown): void {
    const incoming = normalizeIncomingMessage(data);
    if (!incoming) return;
    const room = getCurrentRoom();
    if (incoming.room_id && room?.id !== incoming.room_id) return;
    if (incoming.type === "ai_thinking_start" || incoming.type === "ai_thinking_stage" || incoming.type === "ai_thinking_end") {
        handleAIThinkingEvent(incoming);
        return;
    }
    if (incoming.type === "ai_suggestions") {
        showActionSuggestions(incoming.suggestions || [], incoming.ownerId ?? null);
        return;
    }
    if (incoming.type === "ai_suggestions_clear") {
        clearActionSuggestions(false);
        return;
    }
    if (incoming.message) {
        const aiRequestId = typeof incoming.message.metadata?.aiRequestId === "string" ? incoming.message.metadata.aiRequestId : null;
        if (aiRequestId) clearThinkingMessage(aiRequestId);
        renderRoomMessage(incoming.message, true);
        return;
    }
    if (incoming.type && incoming.content) renderRoomMessage(incoming as ChatMessage, true);
}

function handleAIThinkingEvent(incoming: IncomingSocketMessage): void {
    const aiRequestId = incoming.aiRequestId;
    if (!aiRequestId) return;
    if (incoming.type === "ai_thinking_end") {
        clearThinkingMessage(aiRequestId);
        return;
    }
    if (incoming.type === "ai_thinking_stage") {
        updateThinkingStage(aiRequestId, incoming.label || "AI 正在思考中...", incoming.stage || "");
        return;
    }
    const selector = `.message.thinking.kp-message[data-ai-request-id="${aiRequestId}"]`;
    if (document.querySelector(selector)) {
        updateThinkingStage(aiRequestId, incoming.label || "AI 正在思考中...", incoming.stage || "");
        return;
    }
    // Use the receiving browser's clock. Sender clocks can differ and produce
    // negative or wildly inflated elapsed times for other players.
    addThinkingMessage(aiRequestId, incoming.roleName || "KP", Date.now());
}

function normalizeIncomingMessage(data: unknown): IncomingSocketMessage | null {
    if (typeof data !== "object" || data === null) return null;
    return data as IncomingSocketMessage;
}

/** 监听输入框，节流广播本机用户的「正在输入」事件。 */
function initTypingIndicator(chatInput: HTMLInputElement): void {
    chatInput.addEventListener("input", () => emitTyping(chatInput.value));
    chatInput.addEventListener("blur", () => { lastTypingEmitAt = 0; });
}

function emitTyping(value: string): void {
    if (!socket?.connected || !value.trim()) return;
    const room = getCurrentRoom();
    if (!room?.id || room.invisible_view) return;
    const now = Date.now();
    if (now - lastTypingEmitAt < TYPING_EMIT_INTERVAL_MS) return;
    lastTypingEmitAt = now;
    socket.emit("typing", {
        room_id: room.id,
        user_id: getCurrentUserId(),
        username: getCurrentUsername(),
    });
}

function handleUserTyping(data: unknown): void {
    const payload = (data || {}) as { room_id?: string; user_id?: string | number; username?: string };
    const room = getCurrentRoom();
    if (payload.room_id && room?.id !== payload.room_id) return;
    const senderKey = String(payload.user_id ?? payload.username ?? "");
    if (!senderKey || senderKey === String(getCurrentUserId() ?? "")) return;
    const name = payload.username || "某人";
    const existing = typingUsers.get(senderKey);
    if (existing) window.clearTimeout(existing.timer);
    const timer = window.setTimeout(() => {
        typingUsers.delete(senderKey);
        renderTypingIndicator();
    }, TYPING_IDLE_TIMEOUT_MS);
    typingUsers.set(senderKey, { name, timer });
    renderTypingIndicator();
}

function renderTypingIndicator(): void {
    const indicator = document.getElementById("typingIndicator");
    if (!indicator) return;
    const names = Array.from(typingUsers.values()).map((entry) => entry.name);
    if (names.length === 0) {
        indicator.hidden = true;
        indicator.textContent = "";
        return;
    }
    indicator.hidden = false;
    indicator.textContent = names.length === 1
        ? `${names[0]} 正在输入…`
        : `${names.slice(0, 2).join("、")}${names.length > 2 ? " 等" : ""}正在输入…`;
}

function clearTypingIndicator(): void {
    typingUsers.forEach((entry) => window.clearTimeout(entry.timer));
    typingUsers.clear();
    renderTypingIndicator();
}

/** 当前账号角色被管理员调整后，重新拉取资料并刷新权限相关 UI。 */
function handleRoleChanged(data: unknown): void {
    const payload = (data || {}) as { user_id?: string | number; role?: string };
    if (String(payload.user_id ?? "") !== String(getCurrentUserId() ?? "")) return;
    void refreshCurrentUserProfile();
}

async function refreshCurrentUserProfile(): Promise<void> {
    try {
        const response = await TrpgApi.get<ApiResponse<AuthModule.CurrentUser>>("/api/user/profile");
        if (!response.success || !response.data) return;
        AuthModule.setCurrentUser(response.data);
        showNotification(`你的账号权限已更新为 ${response.data.role || "USER"}`, "info");
    } catch {
        /* 资料刷新失败时静默忽略，下次请求会再次刷新 session 角色 */
    }
}

function numericMetadata(metadata: Record<string, unknown>, key: string): number | null {
    const value = metadata[key];
    return typeof value === "number" ? value : null;
}

function chatEscapeHtml(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function chatErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

/** 聊天框内的用户可见文案走 i18n；缺失时回退到中文原文。 */
function chatT(key: string, fallback: string, values: Record<string, string | number> = {}): string {
    return window.TrpgI18n?.t(key, fallback, values) || fallback;
}

window.renderChatMessages = renderChatMessages;
window.getCurrentChatMessages = getCurrentChatMessages;
window.clearChatMessages = clearChatMessages;
window.setChatReadOnly = setChatReadOnly;
window.joinSocketRoom = joinSocketRoom;
window.leaveSocketRoom = leaveSocketRoom;
window.reconnectSocket = reconnectSocket;
window.disconnectSocket = disconnectSocket;
window.loadAIRoles = loadAIRoles;
window.restoreThinkingState = restoreThinkingState;
window.startScenario = startScenario;
window.resumePendingAIRequest = resumePendingAIRequest;
