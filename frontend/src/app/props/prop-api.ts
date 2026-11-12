/**
 * 道具工坊接口封装（设计稿 §5.2 / §8.1）。
 *
 * 后端返回统一为 `{ success, message, error, data }`；本文件只负责把
 * `data` 拆出来，并把 `error` 码保留在 `PropApiError.code` 里供界面做 i18n。
 */

interface PropTemplateField {
    key: string;
    label?: string;
    type?: "text" | "textarea" | string;
    max?: number;
    required?: boolean;
}

interface PropTemplate {
    id: string;
    name?: string;
    category?: string;
    description?: string;
    fields?: PropTemplateField[];
    styles?: { paper?: string[]; ink?: string[] };
    wear_supported?: string[];
    page?: { size?: string; margin?: number[] };
    ai_hints?: Record<string, unknown>;
}

interface PropRedaction {
    field: string;
    start: number;
    end: number;
    mode?: "burn" | "ink" | string;
}

interface PropVisibility {
    mode?: "hidden" | "revealed" | "partial";
    revealed_to?: Array<number | string>;
}

interface PropSource {
    kind?: "ai" | "manual" | "imported" | string;
    clue_id?: string | number | null;
    scenario_id?: string | number | null;
    ai_request_id?: string | null;
}

interface PropSpec {
    schema_version?: number;
    id: string;
    room_id?: string;
    template_id: string;
    title: string;
    fields: Record<string, string>;
    style?: Record<string, string>;
    wear?: Record<string, number>;
    redaction?: PropRedaction[];
    visibility?: PropVisibility;
    source?: PropSource;
    assets?: Record<string, string>;
    created_by?: number | string | null;
    created_at?: string;
    updated_at?: string;
    redacted?: boolean;
}

interface PropIndexEntry {
    id: string;
    template_id: string;
    title: string;
    visibility?: PropVisibility;
    created_at?: string;
    updated_at?: string;
}

interface PropCreateBody {
    template_id: string;
    title?: string;
    fields?: Record<string, string>;
    style?: Record<string, string>;
    wear?: Record<string, number>;
    redaction?: PropRedaction[];
    visibility?: PropVisibility;
    source?: PropSource;
}

interface PropDraftInput {
    template_id: string;
    era?: string;
    place?: string;
    clues?: string;
    tone?: string;
}

interface PropDraftResult {
    title?: string;
    fields: Record<string, string>;
    wear?: Record<string, number>;
    style?: Record<string, string>;
}

interface PropRevealResult {
    prop: PropSpec;
    message?: ChatMessage;
}

class PropApiError extends Error {
    constructor(message: string, public readonly code: string) {
        super(message);
        this.name = "PropApiError";
    }
}

interface TrpgPropApiRuntime {
    PropApiError: typeof PropApiError;
    templates(roomId: string): Promise<PropTemplate[]>;
    cachedTemplates(roomId?: string): PropTemplate[];
    findTemplate(roomId: string, templateId: string): PropTemplate | null;
    list(roomId: string): Promise<PropIndexEntry[]>;
    get(roomId: string, propId: string): Promise<PropSpec>;
    create(roomId: string, body: PropCreateBody): Promise<PropSpec>;
    draft(roomId: string, body: PropDraftInput): Promise<PropDraftResult>;
    update(roomId: string, propId: string, patch: Partial<PropCreateBody>): Promise<PropSpec>;
    remove(roomId: string, propId: string): Promise<void>;
    reveal(
        roomId: string,
        propId: string,
        payload: { to?: Array<number | string>; all?: boolean; message?: string },
    ): Promise<PropRevealResult>;
    redact(roomId: string, propId: string, items: PropRedaction[]): Promise<PropSpec>;
    renderUrl(roomId: string, propId: string, format: "pdf" | "png", version?: string | number): string;
    templateIcon(templateId: string, category?: string): string;
    errorText(error: unknown, fallback: string): string;
    errorCode(error: unknown): string;
}

interface Window {
    TrpgPropApi?: TrpgPropApiRuntime;
}

(function initializePropApi(global: Window): void {
    "use strict";

    const templateCache = new Map<string, PropTemplate[]>();

    function api(): TrpgApiClient {
        const client = global.TrpgApi;
        if (!client) throw new PropApiError("API client is not ready", "prop.api_unavailable");
        return client;
    }

    function unwrap<T>(payload: ApiResponse<T> | null | undefined, fallback: string): T {
        if (!payload || payload.success !== true) {
            const code = String(payload?.error || "prop.request_failed");
            const message = String(payload?.message || fallback);
            throw new PropApiError(message, code);
        }
        if (payload.data === undefined) {
            throw new PropApiError(fallback, "prop.invalid_response");
        }
        return payload.data;
    }

    function base(roomId: string): string {
        return `/api/rooms/${encodeURIComponent(roomId)}/props`;
    }

    function propPath(roomId: string, propId: string): string {
        return `${base(roomId)}/${encodeURIComponent(propId)}`;
    }

    async function templates(roomId: string): Promise<PropTemplate[]> {
        const cached = templateCache.get(roomId);
        if (cached) return cached;
        const payload = await api().get<ApiResponse<{ templates?: PropTemplate[] }>>(
            `${base(roomId)}/templates`,
        );
        const data = unwrap(payload, "load templates failed");
        const list = Array.isArray(data.templates) ? data.templates : [];
        templateCache.set(roomId, list);
        return list;
    }

    function cachedTemplates(roomId?: string): PropTemplate[] {
        if (roomId) return templateCache.get(roomId) || [];
        for (const value of templateCache.values()) return value;
        return [];
    }

    function findTemplate(roomId: string, templateId: string): PropTemplate | null {
        return cachedTemplates(roomId).find((item) => item.id === templateId) || null;
    }

    async function list(roomId: string): Promise<PropIndexEntry[]> {
        const payload = await api().get<ApiResponse<{ props?: PropIndexEntry[] }>>(base(roomId));
        const data = unwrap(payload, "load props failed");
        return Array.isArray(data.props) ? data.props : [];
    }

    async function get(roomId: string, propId: string): Promise<PropSpec> {
        const payload = await api().get<ApiResponse<PropSpec>>(propPath(roomId, propId));
        return unwrap(payload, "load prop failed");
    }

    async function create(roomId: string, body: PropCreateBody): Promise<PropSpec> {
        const payload = await api().post<ApiResponse<PropSpec>>(base(roomId), body);
        return unwrap(payload, "create prop failed");
    }

    async function draft(roomId: string, body: PropDraftInput): Promise<PropDraftResult> {
        const payload = await api().post<ApiResponse<PropDraftResult>>(`${base(roomId)}/draft`, body);
        return unwrap(payload, "draft prop failed");
    }

    async function update(roomId: string, propId: string, patch: Partial<PropCreateBody>): Promise<PropSpec> {
        const payload = await api().request<ApiResponse<PropSpec>>(propPath(roomId, propId), {
            method: "PATCH",
            body: patch,
        });
        return unwrap(payload, "update prop failed");
    }

    async function remove(roomId: string, propId: string): Promise<void> {
        const payload = await api().del<ApiResponse<{ deleted?: boolean }>>(propPath(roomId, propId));
        unwrap(payload, "delete prop failed");
    }

    async function reveal(
        roomId: string,
        propId: string,
        payloadBody: { to?: Array<number | string>; all?: boolean; message?: string },
    ): Promise<PropRevealResult> {
        const payload = await api().post<ApiResponse<PropRevealResult>>(
            `${propPath(roomId, propId)}/reveal`,
            payloadBody,
        );
        return unwrap(payload, "reveal prop failed");
    }

    async function redact(roomId: string, propId: string, items: PropRedaction[]): Promise<PropSpec> {
        const payload = await api().post<ApiResponse<PropSpec>>(
            `${propPath(roomId, propId)}/redact`,
            { redactions: items },
        );
        return unwrap(payload, "redact prop failed");
    }

    function renderUrl(
        roomId: string,
        propId: string,
        format: "pdf" | "png",
        version?: string | number,
    ): string {
        const params = new URLSearchParams({ format });
        if (version !== undefined && version !== null && String(version) !== "") {
            params.set("v", String(version));
        }
        return `${propPath(roomId, propId)}/render?${params.toString()}`;
    }

    function errorCode(error: unknown): string {
        if (error instanceof PropApiError) return error.code;
        return "prop.request_failed";
    }

    /** 模板图标（Font Awesome 4，项目现有版本）。 */
    function templateIcon(templateId: string, category?: string): string {
        const id = String(templateId || "").toLowerCase();
        if (id.includes("letter")) return "fa fa-envelope-o";
        if (id.includes("newspaper") || category === "newspaper") return "fa fa-newspaper-o";
        if (id.includes("photo")) return "fa fa-camera";
        if (id.includes("invitation")) return "fa fa-paper-plane-o";
        if (id.includes("ticket")) return "fa fa-ticket";
        return "fa fa-file-text-o";
    }

    function errorText(error: unknown, fallback: string): string {
        if (error instanceof PropApiError && error.message) return error.message;
        if (error instanceof Error && error.message) return error.message;
        return fallback;
    }

    global.TrpgPropApi = {
        PropApiError,
        templates,
        cachedTemplates,
        findTemplate,
        list,
        get,
        create,
        draft,
        update,
        remove,
        reveal,
        redact,
        renderUrl,
        templateIcon,
        errorText,
        errorCode,
    };
})(window);
