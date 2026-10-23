import type { AgentAttachment } from "./agent-types";

const TOOL_LABELS: Record<string, string> = {
    "characters.save": "保存角色卡",
    "scenarios.save": "保存剧本",
    "permissions.set_role_permission": "调整角色权限",
    "users.set_role": "修改用户组",
    "users.set_status": "修改账号状态",
    "users.force_password_reset": "强制重置密码",
    "site.update_general_settings": "修改网站设置",
    "profile.update_my_profile": "修改个人资料",
    "room.delete_message": "删除房间消息",
    "room.clear_messages": "清空房间消息",
};

const FIELD_LABELS: Record<string, string> = {
    is_new: "新建",
    name: "名称",
    title: "标题",
    changed_fields: "变更字段",
    content_fields: "内容字段",
    before: "变更前",
    after: "变更后",
    role: "角色",
    node_id: "权限节点",
    user_id: "用户 ID",
    username: "用户名",
    status: "状态",
    playerId: "归属玩家",
    preview: "预览",
    section: "配置段",
    key: "配置项",
    value: "新值",
    changes: "变更内容",
    field: "字段",
};

const HIDDEN_DIFF_KEYS = new Set(["summary"]);

function formatValue(value: unknown): string {
    if (value === null || value === undefined) return "-";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (Array.isArray(value)) {
        return value.length ? value.map((item) => String(item)).join("、") : "无";
    }
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
}

function statusLabel(status?: string): string {
    if (status === "applied") return "已执行";
    if (status === "rejected") return "已取消";
    if (status === "expired") return "已过期";
    return "待确认";
}

interface Props {
    attachment: AgentAttachment;
    disabled?: boolean;
    onConfirm: (actionId: string) => Promise<void>;
    onReject: (actionId: string) => Promise<void>;
}

export function AgentActionCard({ attachment, disabled = false, onConfirm, onReject }: Props) {
    const actionId = attachment.action_id || "";
    if (!actionId) return null;
    const status = attachment.status || "pending";
    const pending = status === "pending";
    const toolLabel = TOOL_LABELS[attachment.tool || ""] || attachment.tool || "写操作";
    const diff = attachment.diff || {};
    const rows = Object.entries(diff).filter(
        ([key, value]) => !HIDDEN_DIFF_KEYS.has(key) && value !== undefined && value !== null && value !== "",
    );

    return (
        <div className={`agent-action-card agent-action-${status}`}>
            <div className="agent-action-head">
                <span className="agent-action-kind">{toolLabel}</span>
                <span className={`agent-action-status status-${status}`}>{statusLabel(status)}</span>
            </div>
            {attachment.summary ? <p className="agent-action-summary">{attachment.summary}</p> : null}
            {rows.length ? (
                <dl className="agent-action-diff">
                    {rows.slice(0, 10).map(([key, value]) => (
                        <div className="agent-action-row" key={key}>
                            <dt>{FIELD_LABELS[key] || key}</dt>
                            <dd>{formatValue(value)}</dd>
                        </div>
                    ))}
                </dl>
            ) : null}
            {pending ? (
                <div className="agent-action-buttons">
                    <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        disabled={disabled}
                        onClick={() => void onConfirm(actionId)}
                    >
                        确认执行
                    </button>
                    <button
                        type="button"
                        className="btn btn-sm btn-outline-secondary"
                        disabled={disabled}
                        onClick={() => void onReject(actionId)}
                    >
                        取消
                    </button>
                </div>
            ) : null}
            {pending && attachment.expires_at ? (
                <p className="agent-action-expiry">请在 {attachment.expires_at} 前处理，过期后需重新生成。</p>
            ) : null}
        </div>
    );
}