import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useAgent } from "./AgentProvider";
import { AgentActionCard } from "./AgentActionCard";
import type { AgentMessage } from "./agent-types";

interface MarkdownBridge {
    marked?: { parse: (text: string) => string };
    DOMPurify?: { sanitize: (html: string) => string };
}

function escapeHtml(text: string): string {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/** 助手消息按 Markdown 渲染；缺少第三方库或被禁用时退回纯文本，绝不放行原始 HTML。 */
function renderMarkdown(content: string): string {
    const bridge = window as unknown as MarkdownBridge;
    if (bridge.marked && bridge.DOMPurify) {
        try {
            return bridge.DOMPurify.sanitize(bridge.marked.parse(content));
        } catch {
            // 解析失败时使用纯文本降级。
        }
    }
    return escapeHtml(content).replace(/\n/g, "<br>");
}

function formatTime(value: string): string {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function MessageBubble({ message }: { message: AgentMessage }) {
    if (message.role === "tool") return null;
    const isUser = message.role === "user";
    const attachments = message.attachments || [];
    const usage = message.usage?.total_tokens;
    return (
        <div className={`agent-message ${isUser ? "agent-message-user" : "agent-message-assistant"}`}>
            <div className="agent-message-meta">
                <span className="agent-message-author">{isUser ? "我" : "Agent"}</span>
                <span className="agent-message-time">{formatTime(message.created_at)}</span>
                {!isUser && usage ? <span className="agent-message-usage">{usage} tokens</span> : null}
            </div>
            {isUser ? (
                <div className="agent-message-body agent-message-plain">{message.content}</div>
            ) : (
                <div
                    className="agent-message-body agent-markdown"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content) }}
                />
            )}
            {!isUser && message.notice ? <p className="agent-message-notice">{message.notice}</p> : null}
            {attachments.length ? (
                <div className="agent-attachment-list">
                    {attachments.map((attachment, index) => (
                        <MessageAttachment key={`${message.id}-${attachment.action_id || index}`} attachment={attachment} />
                    ))}
                </div>
            ) : null}
        </div>
    );
}

function MessageAttachment({ attachment }: { attachment: NonNullable<AgentMessage["attachments"]>[number] }) {
    const { sending, confirmAction, rejectAction } = useAgent();
    if (attachment.kind !== "pending_action" && !attachment.action_id) return null;
    return (
        <AgentActionCard
            attachment={attachment}
            disabled={sending}
            onConfirm={confirmAction}
            onReject={rejectAction}
        />
    );
}

const QUICK_PROMPTS = [
    "帮我按当前剧本创建一名调查员",
    "列出我的角色卡",
    "当前房间有哪些成员和角色？",
];

export function AgentChat({ variant = "window" }: { variant?: "window" | "page" }) {
    const {
        status,
        canUse,
        activeConversation,
        conversations,
        sending,
        error,
        clearError,
        sendMessage,
        stopSending,
        statusLoaded,
        pendingPrompt,
        consumePendingPrompt,
    } = useAgent();
    const [draft, setDraft] = useState("");
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const inputRef = useRef<HTMLTextAreaElement | null>(null);
    const messages = activeConversation?.messages || [];
    const maxChars = status?.limits.max_message_chars || 4000;
    const toolCount = useMemo(
        () => (status ? status.tools.length + status.room_tools.length : 0),
        [status],
    );

    useEffect(() => {
        const element = scrollRef.current;
        if (element) element.scrollTop = element.scrollHeight;
    }, [messages.length, sending]);

    useEffect(() => {
        const element = inputRef.current;
        if (!element) return;
        element.style.height = "auto";
        element.style.height = `${Math.min(element.scrollHeight, 140)}px`;
    }, [draft]);

    // 外部（如剧本校验面板）带着提示打开 Agent 时，把提示预填进输入框，
    // 让用户先确认 / 修改再发送，避免直接替用户发起请求。
    useEffect(() => {
        if (!pendingPrompt) return;
        setDraft(pendingPrompt);
        consumePendingPrompt();
        requestAnimationFrame(() => inputRef.current?.focus());
    }, [pendingPrompt, consumePendingPrompt]);

    const submit = (): void => {
        const text = draft.trim();
        if (!text || sending || !canUse) return;
        setDraft("");
        void sendMessage(text);
    };

    const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
        }
    };

    return (
        <div className={`agent-chat agent-chat-${variant}`}>
            <div className="agent-message-list" ref={scrollRef}>
                {!statusLoaded ? <p className="agent-placeholder">正在读取 Agent 状态…</p> : null}
                {statusLoaded && !canUse ? (
                    <p className="agent-placeholder">
                        {status?.enabled
                            ? "你的账号没有使用网站 Agent 的权限，请联系管理员。"
                            : "网站 Agent 已被管理员关闭。"}
                    </p>
                ) : null}
                {canUse && messages.length === 0 ? (
                    <div className="agent-empty">
                        <div className="agent-empty-icon">
                            <i className="fa fa-magic" aria-hidden="true" />
                        </div>
                        <h3>网站 Agent</h3>
                        <p>可以协助查询角色卡、剧本与房间信息，也能在你确认后写入数据。写操作不会自动保存。</p>
                        <div className="agent-quick-prompts">
                            {QUICK_PROMPTS.map((prompt) => (
                                <button
                                    key={prompt}
                                    type="button"
                                    className="agent-quick-prompt"
                                    onClick={() => setDraft(prompt)}
                                >
                                    {prompt}
                                </button>
                            ))}
                        </div>
                    </div>
                ) : null}
                {messages.map((message) => (
                    <MessageBubble key={message.id} message={message} />
                ))}
                {sending ? (
                    <div className="agent-pending">
                        <span className="agent-typing-dot" />
                        <span className="agent-typing-dot" />
                        <span className="agent-typing-dot" />
                        <span>Agent 正在处理…</span>
                    </div>
                ) : null}
            </div>

            {error ? (
                <div className="agent-error-bar" role="alert">
                    <span>{error}</span>
                    <button type="button" className="btn btn-sm btn-link" onClick={clearError}>
                        关闭
                    </button>
                </div>
            ) : null}

            <div className="agent-composer">
                <textarea
                    ref={inputRef}
                    className="agent-composer-input"
                    rows={1}
                    value={draft}
                    maxLength={maxChars}
                    placeholder={canUse ? "向 Agent 描述你的需求，Enter 发送，Shift+Enter 换行" : "Agent 当前不可用"}
                    disabled={!canUse}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={handleKeyDown}
                    aria-label="Agent 消息输入框"
                />
                <div className="agent-composer-actions">
                    {sending ? (
                        <button type="button" className="btn btn-sm btn-outline-secondary" onClick={stopSending}>
                            停止等待
                        </button>
                    ) : null}
                    <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        onClick={submit}
                        disabled={!canUse || sending || !draft.trim()}
                    >
                        <i className="fa fa-paper-plane" aria-hidden="true" /> 发送
                    </button>
                </div>
            </div>
            <div className="agent-composer-footer">
                <span>
                    当前身份：{status?.role || "-"} · 可用 {toolCount} 项能力
                </span>
                {status ? (
                    <span>
                        今日 {status.usage_today?.total_tokens || 0} tokens · 近一小时 {status.turns_last_hour}/
                        {status.limits.max_turns_per_hour} 轮
                    </span>
                ) : null}
            </div>
        </div>
    );
}