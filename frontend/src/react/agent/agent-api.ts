// 网站 Agent 的 REST 封装：统一处理 CSRF 头、错误码与 JSON 解包。
import type {
    AgentConversation,
    AgentStatus,
    AgentTurnPayload,
} from "./agent-types";

interface ApiEnvelope<T> {
    success?: boolean;
    data?: T;
    message?: string;
    error?: string;
}

export class AgentApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
        super(message);
        this.name = "AgentApiError";
        this.status = status;
    }
}

function currentUserBridge(): { csrf_token?: string } | null {
    return (window as unknown as { currentUser?: { csrf_token?: string } | null }).currentUser || null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
        Accept: "application/json",
        ...(init.headers as Record<string, string> | undefined),
    };
    if (init.body && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
    }
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
        const token = currentUserBridge()?.csrf_token;
        if (token) headers["X-CSRF-Token"] = token;
    }

    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    let payload: ApiEnvelope<T> = {};
    try {
        payload = (await response.json()) as ApiEnvelope<T>;
    } catch {
        // 非 JSON 响应（例如反向代理错误页）走下面的统一错误。
    }
    if (!response.ok || payload.success === false) {
        const message = payload.message || payload.error || `请求失败（HTTP ${response.status}）`;
        throw new AgentApiError(message, response.status);
    }
    return payload.data as T;
}

export const agentApi = {
    status(): Promise<AgentStatus> {
        return request<AgentStatus>("/api/agent/status");
    },

    conversations(): Promise<AgentConversation[]> {
        return request<AgentConversation[]>("/api/agent/conversations");
    },

    createConversation(): Promise<AgentConversation> {
        return request<AgentConversation>("/api/agent/conversations", { method: "POST", body: "{}" });
    },

    conversation(conversationId: string): Promise<AgentConversation> {
        return request<AgentConversation>(`/api/agent/conversations/${encodeURIComponent(conversationId)}`);
    },

    deleteConversation(conversationId: string): Promise<{ deleted: boolean }> {
        return request<{ deleted: boolean }>(
            `/api/agent/conversations/${encodeURIComponent(conversationId)}`,
            { method: "DELETE" },
        );
    },

    sendTurn(
        content: string,
        conversationId: string | null,
        roomId: string | null,
        signal?: AbortSignal,
    ): Promise<AgentTurnPayload> {
        const init: RequestInit = {
            method: "POST",
            body: JSON.stringify({ content, conversation_id: conversationId, room_id: roomId }),
        };
        if (signal) init.signal = signal;
        return request<AgentTurnPayload>("/api/agent/turn", init);
    },

    confirmAction(actionId: string): Promise<{ status: string; result?: unknown }> {
        return request<{ status: string; result?: unknown }>(
            `/api/agent/actions/${encodeURIComponent(actionId)}/confirm`,
            { method: "POST", body: "{}" },
        );
    },

    rejectAction(actionId: string): Promise<{ status: string }> {
        return request<{ status: string }>(
            `/api/agent/actions/${encodeURIComponent(actionId)}/reject`,
            { method: "POST", body: "{}" },
        );
    },

    usage(windowHours = 24): Promise<Record<string, unknown>> {
        return request<Record<string, unknown>>(`/api/agent/usage?window_hours=${encodeURIComponent(String(windowHours))}`);
    },
};