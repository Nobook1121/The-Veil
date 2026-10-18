import { useRef, type ChangeEvent } from "react";

// 主页聊天框附件桥接：只暴露运行时用到的全局字段，避免依赖主脚本类型声明。
interface HomeChatBridge {
    currentRoom?: { id?: string; archived?: boolean } | null;
    currentUser?: { csrf_token?: string } | null;
    showNotification?: (message: string, type?: string) => void;
}

const HOME_UPLOAD_MAX_BYTES = 20 * 1024 * 1024;
// 与后端 /api/assets/upload 保持一致的允许类型。
const HOME_UPLOAD_EXTENSIONS = [
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp4", "webm", "ogg", "mov", "m4v",
    "pdf", "txt", "md", "markdown", "json", "csv", "zip", "rar", "7z",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "rtf",
];

// 清洗文件名，避免其中的方括号/括号破坏 Markdown 链接语法。
function sanitizeAttachmentName(name: string): string {
    const cleaned = name.replace(/[\[\]()\r\n]/g, " ").trim();
    return cleaned || "附件";
}

// 图片渲染为缩略图，视频与其它文件渲染为可点击链接。
function buildAttachmentMessage(name: string, url: string, mimeType: string): string {
    const safeName = sanitizeAttachmentName(name);
    return mimeType.startsWith("image/") ? `![${safeName}](${url})` : `[📎 ${safeName}](${url})`;
}

export function HomeChat() {
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    const notify = (message: string, type?: string): void => {
        const bridge = window as unknown as HomeChatBridge;
        bridge.showNotification?.(message, type);
    };

    // 复用现有聊天发送流程：把附件消息写入输入框后触发发送按钮。
    const sendAttachmentContent = (content: string): void => {
        const input = document.getElementById("chatInput") as HTMLInputElement | null;
        const button = document.getElementById("sendButton") as HTMLButtonElement | null;
        if (!input || !button) {
            notify("聊天输入框未就绪，无法发送附件", "error");
            return;
        }
        input.value = content;
        button.click();
    };

    const handleFileSelected = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file) return;

        const bridge = window as unknown as HomeChatBridge;
        if (!bridge.currentRoom) {
            notify("请先加入房间后再发送文件", "error");
            return;
        }
        if (file.size > HOME_UPLOAD_MAX_BYTES) {
            notify("文件过大，请选择 20MB 以内的文件", "error");
            return;
        }
        const extension = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".") + 1).toLowerCase() : "";
        if (!HOME_UPLOAD_EXTENSIONS.includes(extension)) {
            notify("不支持的文件类型", "error");
            return;
        }

        try {
            const formData = new FormData();
            formData.append("file", file);
            const headers: Record<string, string> = { Accept: "application/json" };
            const token = bridge.currentUser?.csrf_token;
            if (token) headers["X-CSRF-Token"] = token;
            const response = await fetch("/api/assets/upload", { method: "POST", body: formData, headers });
            const payload = await response.json() as {
                success?: boolean;
                data?: { url?: string; name?: string; content_type?: string };
                message?: string;
                error?: string;
            };
            if (!response.ok || !payload.success || !payload.data?.url) {
                notify(`上传失败：${payload.message || payload.error || "未知错误"}`, "error");
                return;
            }
            const mimeType = payload.data.content_type || file.type || "";
            sendAttachmentContent(buildAttachmentMessage(payload.data.name || file.name, payload.data.url, mimeType));
        } catch (error) {
            notify(`上传失败：${error instanceof Error ? error.message : String(error)}`, "error");
        }
    };

    return (
        <>
            <div className="home-chat-body">
                <div className="home-chat-main">
                    <header className="page-command-header chat-command-header" data-page-header="chat">
                        <div>
                            <h2 id="homeRoomTitle" data-i18n="home.not_joined" data-i18n-dynamic="true">未加入房间</h2>
                            <p className="home-room-meta" aria-live="polite">
                                <span className="home-room-scenario" id="homeRoomScenario" hidden>
                                    <i className="fa fa-book" aria-hidden="true" />
                                    <span data-i18n="room.status.scenario">剧本</span>
                                    <strong id="saveStatusScenario" data-i18n-dynamic="true">-</strong>
                                </span>
                            </p>
                        </div>
                    </header>

                    <div className="chat-history" id="chatHistory">
                        <div className="welcome-text" data-i18n="home.welcome">欢迎来到帷幕，请选择一个剧本开始游戏。</div>
                    </div>

                    <div id="aiSuggestions" className="chat-suggestions" hidden aria-live="polite" />

                    <div className="chat-input p-3">
                        <div id="typingIndicator" className="chat-typing-indicator" hidden aria-live="polite" />
                        <div className="input-group">
                            <button
                                className="btn btn-outline-secondary"
                                id="homeChatAttachButton"
                                type="button"
                                title="发送文件"
                                aria-label="发送文件"
                                onClick={() => fileInputRef.current?.click()}
                            >
                                <i className="fa fa-paperclip" aria-hidden="true" />
                            </button>
                            <input
                                type="text"
                                className="form-control"
                                id="chatInput"
                                placeholder="加入房间后可发送消息，使用 @KP 呼叫 AI"
                                data-i18n-placeholder="chat.placeholder.room_required"
                                autoComplete="off"
                                aria-autocomplete="none"
                            />
                            <button className="btn btn-primary" id="sendButton" type="button">
                                <i className="fa fa-paper-plane" aria-hidden="true" />{" "}
                                <span data-i18n="chat.action.send">发送</span>
                            </button>
                        </div>
                        <input
                            ref={fileInputRef}
                            type="file"
                            id="homeChatFileInput"
                            accept="image/*,video/*,.pdf,.txt,.md,.json,.csv,.zip,.rar,.7z,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.rtf"
                            style={{ display: "none" }}
                            onChange={handleFileSelected}
                        />
                    </div>
                </div>

                <aside className="home-room-members" id="homeRoomMembers" aria-labelledby="homeRoomMembersTitle">
                    <div className="home-room-members-header">
                        <span className="home-room-members-title" id="homeRoomMembersTitle" data-i18n="home.members.title">房间成员</span>
                        <span className="home-room-online" id="homeRoomOnlineCount" data-i18n="room.status.online_players" data-i18n-dynamic="true">在线玩家 0/0</span>
                    </div>
                    <div className="home-room-member-list" id="roomMemberList" data-i18n-dynamic="true">
                        <p className="home-room-members-empty" data-i18n="home.members.empty">加入房间后这里会显示房间成员。</p>
                    </div>
                </aside>
            </div>
        </>
    );
}
