import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useAgent } from "./AgentProvider";
import { AgentChat } from "./AgentChat";
import { agentApi } from "./agent-api";

interface UsageSummary {
    window_hours?: number;
    total_tokens?: number;
    prompt_tokens?: number;
    completion_tokens?: number;
    turns?: number;
    requests?: number;
    users?: number;
}

function formatTime(value: string): string {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function AgentPage() {
    const {
        status,
        statusLoaded,
        canUse,
        conversations,
        activeConversation,
        ballVisible,
        setBallVisible,
        selectConversation,
        newConversation,
        deleteConversation,
        refreshConversations,
        refreshStatus,
    } = useAgent();
    const [usage, setUsage] = useState<UsageSummary | null>(null);
    const [usageVisible, setUsageVisible] = useState(false);
    const [usageLoading, setUsageLoading] = useState(false);
    const target = document.getElementById("react-agent-page-root");
    const canViewUsage = Boolean(status?.permissions?.includes("settings.general"));

    useEffect(() => {
        if (!canUse) return;
        void refreshStatus();
        void refreshConversations();
    }, [canUse, refreshConversations, refreshStatus]);

    useEffect(() => {
        if (!canUse || activeConversation) return;
        const first = conversations[0];
        if (!first) return;
        void selectConversation(first.id);
    }, [activeConversation, canUse, conversations, selectConversation]);

    useEffect(() => {
        if (!usageVisible || !canViewUsage) return;
        setUsageLoading(true);
        agentApi
            .usage(24)
            .then((data) => setUsage(data as UsageSummary))
            .catch((err) => console.warn("加载 Agent 用量失败", err))
            .finally(() => setUsageLoading(false));
    }, [canViewUsage, usageVisible]);

    if (!target) return null;

    return createPortal(
        <div className="agent-page">
            <aside className="agent-page-sidebar">
                <div className="agent-page-sidebar-head">
                    <h2>
                        <i className="fa fa-magic" aria-hidden="true" /> 网站 Agent
                    </h2>
                    <p>查询与操作由服务端按权限校验，写操作需你确认。</p>
                </div>
                <button type="button" className="btn btn-sm btn-primary agent-page-new" onClick={() => void newConversation()} disabled={!canUse}>
                    <i className="fa fa-plus" aria-hidden="true" /> 新建会话
                </button>
                <div className="agent-conversation-list">
                    {conversations.length === 0 ? (
                        <p className="agent-placeholder">还没有会话。发送第一条消息即可开始。</p>
                    ) : (
                        conversations.map((conversation) => (
                            <button
                                type="button"
                                key={conversation.id}
                                className={`agent-conversation${activeConversation?.id === conversation.id ? " active" : ""}`}
                                onClick={() => void selectConversation(conversation.id)}
                            >
                                <span className="agent-conversation-title">{conversation.title || "新会话"}</span>
                                <span className="agent-conversation-meta">
                                    {formatTime(conversation.updated_at)}
                                    {conversation.message_count ? ` · ${conversation.message_count} 条` : ""}
                                </span>
                                <span
                                    role="button"
                                    tabIndex={0}
                                    className="agent-conversation-delete"
                                    title="删除会话"
                                    aria-label={`删除会话 ${conversation.title || ""}`}
                                    onClick={(event) => {
                                        event.stopPropagation();
                                        void deleteConversation(conversation.id);
                                    }}
                                    onKeyDown={(event) => {
                                        if (event.key === "Enter" || event.key === " ") {
                                            event.preventDefault();
                                            event.stopPropagation();
                                            void deleteConversation(conversation.id);
                                        }
                                    }}
                                >
                                    <i className="fa fa-trash-o" aria-hidden="true" />
                                </span>
                            </button>
                        ))
                    )}
                </div>
                <div className="agent-page-sidebar-foot">
                    <label className="agent-ball-toggle">
                        <input
                            type="checkbox"
                            checked={ballVisible}
                            onChange={(event) => setBallVisible(event.target.checked)}
                        />
                        <span>显示悬浮球</span>
                    </label>
                    {canViewUsage ? (
                        <button
                            type="button"
                            className="btn btn-sm btn-outline-secondary"
                            onClick={() => setUsageVisible((visible) => !visible)}
                        >
                            用量统计
                        </button>
                    ) : null}
                </div>
                {usageVisible && canViewUsage ? (
                    <div className="agent-usage-panel">
                        <h4>近 24 小时全站用量</h4>
                        {usageLoading ? (
                            <p className="agent-placeholder">正在加载…</p>
                        ) : usage ? (
                            <dl>
                                <div><dt>总 tokens</dt><dd>{usage.total_tokens || 0}</dd></div>
                                <div><dt>输入 / 输出</dt><dd>{usage.prompt_tokens || 0} / {usage.completion_tokens || 0}</dd></div>
                                <div><dt>请求数</dt><dd>{usage.requests || 0}</dd></div>
                                <div><dt>活跃用户</dt><dd>{usage.users || 0}</dd></div>
                            </dl>
                        ) : (
                            <p className="agent-placeholder">暂无用量记录</p>
                        )}
                    </div>
                ) : null}
            </aside>
            <section className="agent-page-main">
                <header className="agent-page-header">
                    <div>
                        <h3>{activeConversation?.title || "新的对话"}</h3>
                        <p>
                            {canUse
                                ? "Agent 会读取服务端授权范围内的数据；请不要在对话中粘贴密码或令牌。"
                                : statusLoaded
                                  ? "网站 Agent 当前不可用。"
                                  : "正在检查 Agent 状态…"}
                        </p>
                    </div>
                    {status ? (
                        <span className="agent-role-pill">
                            {status.role} · {status.tools.length + status.room_tools.length} 项能力
                        </span>
                    ) : null}
                </header>
                <AgentChat variant="page" />
            </section>
        </div>,
        target,
    );
}