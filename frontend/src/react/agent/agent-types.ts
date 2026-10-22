// 网站 Agent 的共享类型：与后端 /api/agent/* 返回结构保持一致。

export interface AgentToolInfo {
    name: string;
    risk: "read" | "write" | "danger";
    scope?: string;
}

export interface AgentLimits {
    max_tool_rounds: number;
    max_message_chars: number;
    max_turns_per_hour: number;
    daily_token_budget: number;
}

export interface AgentUsageToday {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
}

export interface AgentStatus {
    enabled: boolean;
    can_use: boolean;
    role: string;
    permissions: string[];
    tools: AgentToolInfo[];
    room_tools: AgentToolInfo[];
    limits: AgentLimits;
    usage_today: AgentUsageToday;
    turns_last_hour: number;
}

export interface AgentAttachmentDiff {
    // 角色卡/剧本写操作的服务端摘要，具体字段随工具变化。
    [key: string]: unknown;
}

export interface AgentAttachment {
    kind: string;
    action_id?: string;
    tool?: string;
    risk?: string;
    summary?: string;
    expires_at?: string;
    diff?: AgentAttachmentDiff;
    status?: string;
}

export interface AgentMessage {
    id: string;
    role: "user" | "assistant" | "tool";
    content: string;
    created_at: string;
    name?: string;
    attachments?: AgentAttachment[];
    usage?: AgentUsageToday;
    notice?: string;
}

export interface AgentConversation {
    id: string;
    title: string;
    created_at: string;
    updated_at: string;
    context?: { room_id?: string; scenario_id?: number };
    message_count?: number;
    last_message?: string;
    messages?: AgentMessage[];
}

export interface AgentTurnPayload {
    conversation: AgentConversation;
    message: AgentMessage;
}

export interface AgentToolEvent {
    name: string;
    risk: string;
    status: string;
    conversation_id?: string;
    action_id?: string;
}