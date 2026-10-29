import {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { AgentApiError, agentApi } from "./agent-api";
import type { AgentConversation, AgentMessage, AgentStatus } from "./agent-types";

const BALL_PREFERENCE_KEY = "trpg.agent.ball";

interface AgentContextValue {
    status: AgentStatus | null;
    statusLoaded: boolean;
    conversations: AgentConversation[];
    activeConversation: AgentConversation | null;
    sending: boolean;
    error: string | null;
    windowOpen: boolean;
    ballVisible: boolean;
    unread: boolean;
    canUse: boolean;
    openWindow: () => void;
    closeWindow: () => void;
    toggleWindow: () => void;
    setBallVisible: (visible: boolean) => void;
    openPage: () => void;
    /** 从页面其他位置带着一条待发送提示打开 Agent（如剧本校验的「AI 分析」）。 */
    openWithPrompt: (prompt: string) => void;
    pendingPrompt: string | null;
    consumePendingPrompt: () => void;
    refreshStatus: () => Promise<void>;
    refreshConversations: () => Promise<void>;
    selectConversation: (conversationId: string) => Promise<void>;
    newConversation: () => Promise<void>;
    deleteConversation: (conversationId: string) => Promise<void>;
    sendMessage: (content: string) => Promise<void>;
    stopSending: () => void;
    confirmAction: (actionId: string) => Promise<void>;
    rejectAction: (actionId: string) => Promise<void>;
    clearError: () => void;
}

const AgentContext = createContext<AgentContextValue | null>(null);

function readBallPreference(): boolean {
    try {
        return window.localStorage.getItem(BALL_PREFERENCE_KEY) !== "off";
    } catch {
        return true;
    }
}

function writeBallPreference(visible: boolean): void {
    try {
        if (visible) {
            window.localStorage.removeItem(BALL_PREFERENCE_KEY);
        } else {
            window.localStorage.setItem(BALL_PREFERENCE_KEY, "off");
        }
    } catch {
        // 隐私模式下 localStorage 可能不可写，只影响偏好持久化。
    }
}

function notify(message: string, type?: string): void {
    const bridge = window as unknown as { showNotification?: (text: string, kind?: string) => void };
    bridge.showNotification?.(message, type);
}

function summarize(conversation: AgentConversation): AgentConversation {
    const messages = conversation.messages || [];
    const last = messages[messages.length - 1];
    return {
        id: conversation.id,
        title: conversation.title || "新会话",
        created_at: conversation.created_at,
        updated_at: conversation.updated_at,
        context: conversation.context || {},
        message_count: messages.length,
        last_message: last ? String(last.content || "").slice(0, 120) : "",
    };
}

function agentPageVisible(): boolean {
    return Boolean(document.getElementById("agent")?.classList.contains("active"));
}

function currentRoomId(): string | null {
    const room = (window as unknown as { currentRoom?: { id?: string } | null }).currentRoom;
    return room?.id ? String(room.id) : null;
}

export function AgentProvider({ children }: { children: ReactNode }) {
    const [status, setStatus] = useState<AgentStatus | null>(null);
    const [statusLoaded, setStatusLoaded] = useState(false);
    const [conversations, setConversations] = useState<AgentConversation[]>([]);
    const [activeConversation, setActiveConversation] = useState<AgentConversation | null>(null);
    const [sending, setSending] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [windowOpen, setWindowOpen] = useState(false);
    const [ballVisible, setBallVisibleState] = useState<boolean>(readBallPreference);
    const [unread, setUnread] = useState(false);
    const [pendingPrompt, setPendingPrompt] = useState<string | null>(null);
    const abortRef = useRef<AbortController | null>(null);
    const lastUserIdRef = useRef<number | null>(null);
    // 会话详情请求可能乱序返回，记录最后一次选择的会话，避免旧响应覆盖新选择。
    const selectionRef = useRef<string | null>(null);

    const canUse = Boolean(status?.enabled && status.can_use);

    const refreshConversations = useCallback(async () => {
        try {
            const items = await agentApi.conversations();
            setConversations(items);
        } catch (err) {
            if (!(err instanceof AgentApiError && err.status === 401)) {
                console.warn("加载 Agent 会话列表失败", err);
            }
        }
    }, []);

    const refreshStatus = useCallback(async () => {
        const currentUser = (window as unknown as { currentUser?: { user_id?: number } | null }).currentUser;
        if (!currentUser?.user_id) {
            setStatus(null);
            setStatusLoaded(true);
            setConversations([]);
            setActiveConversation(null);
            return;
        }
        try {
            const payload = await agentApi.status();
            setStatus(payload);
            if (payload.enabled && payload.can_use) {
                await refreshConversations();
            }
        } catch (err) {
            // 未登录或权限不足时静默隐藏入口；其他错误保留可诊断信息。
            if (err instanceof AgentApiError && (err.status === 401 || err.status === 403)) {
                setStatus(null);
            } else {
                console.warn("加载 Agent 状态失败", err);
                setError(err instanceof Error ? err.message : String(err));
            }
        } finally {
            setStatusLoaded(true);
        }
    }, [refreshConversations]);

    // 首次加载与登录状态切换：React 可能先于传统脚本初始化，因此轮询用户变化。
    useEffect(() => {
        void refreshStatus();
        const timer = window.setInterval(() => {
            const user = (window as unknown as { currentUser?: { user_id?: number } | null }).currentUser;
            const userId = user?.user_id ?? null;
            if (userId !== lastUserIdRef.current) {
                lastUserIdRef.current = userId;
                void refreshStatus();
            }
        }, 4000);
        return () => window.clearInterval(timer);
    }, [refreshStatus]);

    useEffect(() => {
        if (!canUse) {
            setUnread(false);
        }
    }, [canUse]);

    const setBallVisible = useCallback((visible: boolean) => {
        setBallVisibleState(visible);
        writeBallPreference(visible);
    }, []);

    const openWindow = useCallback(() => {
        if (!canUse) {
            notify("网站 Agent 当前不可用", "warning");
            return;
        }
        setWindowOpen(true);
        setUnread(false);
        void refreshStatus();
    }, [canUse, refreshStatus]);

    const closeWindow = useCallback(() => setWindowOpen(false), []);

    const toggleWindow = useCallback(() => {
        if (windowOpen) {
            setWindowOpen(false);
        } else {
            openWindow();
        }
    }, [openWindow, windowOpen]);

    const openPage = useCallback(() => {
        setWindowOpen(false);
        const bridge = window as unknown as { switchMainTab?: (tabId: string) => void };
        bridge.switchMainTab?.("agent");
    }, []);

    const consumePendingPrompt = useCallback(() => setPendingPrompt(null), []);

    /** 供普通脚本（剧本编辑器校验面板）调用：打开 Agent 并把提示预填到输入框。 */
    const openWithPrompt = useCallback(
        (prompt: string) => {
            if (!canUse) {
                notify("网站 Agent 当前不可用", "warning");
                return;
            }
            setPendingPrompt(prompt);
            openWindow();
        },
        [canUse, openWindow],
    );

    useEffect(() => {
        const bridge = window as unknown as { TrpgAgent?: { openWithPrompt: (prompt: string) => void } };
        bridge.TrpgAgent = { openWithPrompt };
        return () => {
            if (bridge.TrpgAgent?.openWithPrompt === openWithPrompt) delete bridge.TrpgAgent;
        };
    }, [openWithPrompt]);

    const applyConversation = useCallback((conversation: AgentConversation) => {
        setActiveConversation(conversation);
        const summary = summarize(conversation);
        setConversations((previous) => {
            const next = previous.filter((item) => item.id !== summary.id);
            next.unshift(summary);
            next.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
            return next;
        });
    }, []);

    const selectConversation = useCallback(async (conversationId: string) => {
        selectionRef.current = conversationId;
        try {
            const conversation = await agentApi.conversation(conversationId);
            if (selectionRef.current !== conversationId) return;
            setActiveConversation(conversation);
            setUnread(false);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    }, []);

    const newConversation = useCallback(async () => {
        if (!canUse) return;
        try {
            const conversation = await agentApi.createConversation();
            selectionRef.current = conversation.id;
            setActiveConversation(conversation);
            await refreshConversations();
            setUnread(false);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    }, [canUse, refreshConversations]);

    const deleteConversation = useCallback(
        async (conversationId: string) => {
            const dialog = (window as unknown as {
                TrpgDialog?: { confirm: (options: Record<string, unknown>) => Promise<boolean> };
            }).TrpgDialog;
            if (!dialog) {
                notify("确认对话框未就绪，请刷新页面后重试", "error");
                return;
            }
            const confirmed = await dialog.confirm({
                title: "删除会话",
                message: "确认删除该 Agent 会话？此操作不可恢复。",
                confirmText: "删除",
                danger: true,
            });
            if (!confirmed) return;
            try {
                await agentApi.deleteConversation(conversationId);
                setConversations((previous) => previous.filter((item) => item.id !== conversationId));
                setActiveConversation((previous) => {
                    if (previous?.id !== conversationId) return previous;
                    selectionRef.current = null;
                    return null;
                });
            } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
            }
        },
        [],
    );

    const sendMessage = useCallback(
        async (content: string) => {
            const text = content.trim();
            if (!text || sending) return;
            if (!canUse) {
                notify("网站 Agent 当前不可用", "warning");
                return;
            }
            if (status && text.length > status.limits.max_message_chars) {
                notify(`消息不能超过 ${status.limits.max_message_chars} 个字符`, "error");
                return;
            }

            const roomId = currentRoomId();
            const conversationId = activeConversation?.id || null;
            const localMessage: AgentMessage = {
                id: `local-${Date.now()}`,
                role: "user",
                content: text,
                created_at: new Date().toISOString(),
            };
            if (activeConversation) {
                setActiveConversation({
                    ...activeConversation,
                    messages: [...(activeConversation.messages || []), localMessage],
                });
            } else {
                // 新会话尚未落盘：先展示本地草稿，后端会在首轮请求时创建会话。
                const now = new Date().toISOString();
                setActiveConversation({
                    id: "",
                    title: "新会话",
                    created_at: now,
                    updated_at: now,
                    messages: [localMessage],
                });
            }

            const controller = new AbortController();
            abortRef.current = controller;
            setSending(true);
            setError(null);
            try {
                const payload = await agentApi.sendTurn(text, conversationId, roomId, controller.signal);
                selectionRef.current = payload.conversation.id;
                applyConversation(payload.conversation);
                if (!windowOpen && !agentPageVisible() && payload.message) {
                    setUnread(true);
                }
                if (payload.message?.notice) {
                    notify(payload.message.notice, "warning");
                }
            } catch (err) {
                if (controller.signal.aborted) {
                    notify("已停止等待回复；模型请求可能仍在后台完成。", "warning");
                } else {
                    setError(err instanceof Error ? err.message : String(err));
                    // 请求失败时回滚乐观追加的本轮用户消息，避免界面出现未保存内容。
                    setActiveConversation((previous) => {
                        if (!previous) return previous;
                        return {
                            ...previous,
                            messages: (previous.messages || []).filter((item) => item.id !== localMessage.id),
                        };
                    });
                }
            } finally {
                if (abortRef.current === controller) abortRef.current = null;
                setSending(false);
            }
        },
        [activeConversation, applyConversation, canUse, sending, status, windowOpen],
    );

    const stopSending = useCallback(() => {
        abortRef.current?.abort();
    }, []);

    const syncActionStatus = useCallback((actionId: string, nextStatus: string) => {
        setActiveConversation((previous) => {
            if (!previous) return previous;
            const messages = (previous.messages || []).map((message) => {
                if (!message.attachments?.some((item) => item.action_id === actionId)) return message;
                return {
                    ...message,
                    attachments: message.attachments.map((item) =>
                        item.action_id === actionId ? { ...item, status: nextStatus } : item,
                    ),
                };
            });
            return { ...previous, messages };
        });
    }, []);

    const confirmAction = useCallback(
        async (actionId: string) => {
            try {
                await agentApi.confirmAction(actionId);
                syncActionStatus(actionId, "applied");
                notify("操作已执行", "success");
                // 写操作落地后刷新对应的传统页面数据。
                const bridge = window as unknown as {
                    reloadCharacterManagement?: () => Promise<void>;
                    refreshScenarioManagement?: () => void;
                };
                void bridge.reloadCharacterManagement?.();
                bridge.refreshScenarioManagement?.();
            } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
            }
        },
        [syncActionStatus],
    );

    const rejectAction = useCallback(
        async (actionId: string) => {
            try {
                await agentApi.rejectAction(actionId);
                syncActionStatus(actionId, "rejected");
            } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
            }
        },
        [syncActionStatus],
    );

    const clearError = useCallback(() => setError(null), []);

    const value = useMemo<AgentContextValue>(
        () => ({
            status,
            statusLoaded,
            conversations,
            activeConversation,
            sending,
            error,
            windowOpen,
            ballVisible,
            unread,
            canUse,
            openWindow,
            closeWindow,
            toggleWindow,
            setBallVisible,
            openPage,
            openWithPrompt,
            pendingPrompt,
            consumePendingPrompt,
            refreshStatus,
            refreshConversations,
            selectConversation,
            newConversation,
            deleteConversation,
            sendMessage,
            stopSending,
            confirmAction,
            rejectAction,
            clearError,
        }),
        [
            activeConversation,
            ballVisible,
            canUse,
            clearError,
            closeWindow,
            confirmAction,
            conversations,
            deleteConversation,
            error,
            newConversation,
            openPage,
            openWindow,
            refreshConversations,
            refreshStatus,
            rejectAction,
            selectConversation,
            sendMessage,
            sending,
            setBallVisible,
            status,
            statusLoaded,
            stopSending,
            toggleWindow,
            unread,
            windowOpen,
            openWithPrompt,
            pendingPrompt,
            consumePendingPrompt,
        ],
    );

    return <AgentContext.Provider value={value}>{children}</AgentContext.Provider>;
}

export function useAgent(): AgentContextValue {
    const value = useContext(AgentContext);
    if (!value) {
        throw new Error("useAgent 必须在 AgentProvider 内使用");
    }
    return value;
}