import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { createPortal } from "react-dom";

// 主页聊天框附件桥接：只暴露运行时用到的全局字段，避免依赖主脚本类型声明。
interface HomeChatBridge {
    currentRoom?: { id?: string; archived?: boolean } | null;
    currentUser?: { csrf_token?: string } | null;
    showNotification?: (message: string, type?: string) => void;
}

const HOME_UPLOAD_MAX_BYTES = 20 * 1024 * 1024;
// 右侧栏收起状态：刷新后保持；Alt+B 与窄条把手都可切换。
const SIDEBAR_COLLAPSED_STORAGE_KEY = "trpg.homeSidebar.collapsed";
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
    const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
        try {
            return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "1";
        } catch {
            return false;
        }
    });

    useEffect(() => {
        try {
            window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, sidebarCollapsed ? "1" : "0");
        } catch {
            // 隐私模式下存储不可用：仅本次会话生效。
        }
    }, [sidebarCollapsed]);

    useEffect(() => {
        const handleShortcut = (event: KeyboardEvent): void => {
            if (!event.altKey || event.key.toLowerCase() !== "b") return;
            // canvas workspace 下 Alt+B 控制右侧工具侧栏，不再复用旧成员栏状态。
            if (window.TrpgWorkspace?.isCanvas?.()) {
                event.preventDefault();
                window.TrpgWorkspace.toggleSidebar();
                return;
            }
            event.preventDefault();
            setSidebarCollapsed((value) => !value);
        };
        window.addEventListener("keydown", handleShortcut);
        return () => window.removeEventListener("keydown", handleShortcut);
    }, []);

    useEffect(() => {
        // 收起时右侧栏里的配乐按钮会被卸载，重新挂载后同步一次静音状态。
        window.TrpgImmersive?.syncMusicButton?.();
    }, [sidebarCollapsed]);

    const toggleSidebar = (): void => setSidebarCollapsed((value) => !value);
    const toggleRailMusic = (): void => window.TrpgImmersive?.toggleMusic?.();
    const toggleRoomAmbient = (): void => window.TrpgImmersive?.toggleAmbient?.();

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
            <div className="home-chat-body" id="homeChatBody" data-room-layout="conversation" data-room-panel="chat">
                {/*
                    canvas workspace 的空壳：始终渲染、默认隐藏，由 workspace 模块按规则集声明切换。
                    coc7 下这些节点不占空间、不创建 canvas，聊天/成员 DOM 保持原样。
                */}
                <header className="ws-chrome ws-topbar" id="wsTopbar" aria-label="房间桌面条">
                    <div className="ws-topbar-scene">
                        <button type="button" className="ws-topbar-icon" id="wsSceneButton" title="场景" aria-label="场景">
                            <i className="fa fa-map-o" aria-hidden="true" />
                        </button>
                        <strong className="ws-topbar-title" id="wsSceneName">未加入房间</strong>
                    </div>
                    <div className="ws-topbar-players" id="wsTopbarPlayers" aria-label="玩家头像" />
                    <div className="ws-topbar-actions">
                        <span className="ws-turn-capsule" id="wsTurnCapsule" hidden />
                        <span className="ws-online-count" id="wsOnlineCount">在线玩家 0/0</span>
                        <button type="button" className="ws-topbar-btn" id="wsLayoutControl" title="切换布局">
                            <i className="fa fa-columns" aria-hidden="true" />
                            <span>布局</span>
                        </button>
                        <button type="button" className="ws-topbar-icon" id="wsSettingsButton" title="界面与布局" aria-label="界面与布局">
                            <i className="fa fa-cog" aria-hidden="true" />
                        </button>
                        <button type="button" className="ws-topbar-icon" id="wsFullscreenButton" title="全屏" aria-label="全屏">
                            <i className="fa fa-expand" aria-hidden="true" />
                        </button>
                    </div>
                </header>

                <nav className="ws-chrome ws-left-rail" id="wsLeftRail" aria-label="场景工具">
                    <button type="button" className="ws-rail-btn is-active" data-ws-tool="select" title="选择" aria-label="选择">
                        <i className="fa fa-mouse-pointer" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-rail-btn" data-ws-tool="measure" title="测量" aria-label="测量">
                        <i className="fa fa-arrows-h" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-rail-btn" data-ws-tool="fog" title="迷雾（DM）" aria-label="迷雾" data-ws-dm-only="true">
                        <i className="fa fa-eye-slash" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-rail-btn" data-ws-tool="light" title="光照（DM）" aria-label="光照" data-ws-dm-only="true">
                        <i className="fa fa-lightbulb-o" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-rail-btn" data-ws-tool="wall" title="墙（DM）" aria-label="墙" data-ws-dm-only="true">
                        <i className="fa fa-th-large" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-rail-btn" data-ws-tool="token" title="添加标记（DM）" aria-label="添加标记" data-ws-dm-only="true">
                        <i className="fa fa-plus-square-o" aria-hidden="true" />
                    </button>
                    <span className="ws-rail-sep" aria-hidden="true" />
                    <button type="button" className="ws-rail-btn is-active" data-ws-tool="grid" title="网格" aria-label="网格">
                        <i className="fa fa-th" aria-hidden="true" />
                    </button>
                </nav>

                <section className="ws-chrome ws-stage" id="wsStageHost" aria-label="地图舞台">
                    <div className="ws-stage-empty" id="wsStageEmpty" hidden>
                        <i className="fa fa-map" aria-hidden="true" />
                        <span>等待场景载入</span>
                    </div>
                    <div className="ws-stage-controls" aria-label="地图缩放">
                        <button type="button" id="wsZoomOut" title="缩小">−</button>
                        <span id="wsZoomLabel">100%</span>
                        <button type="button" id="wsZoomIn" title="放大">＋</button>
                        <button type="button" id="wsZoomFit" title="适应窗口">适应</button>
                        <button type="button" id="wsStageFullscreen" title="全屏">全屏</button>
                    </div>
                </section>

                <div className="ws-chrome ws-sidebar-tabs" id="wsSidebarTabs" role="tablist" aria-label="工具侧栏">
                    <button type="button" role="tab" className="ws-sidebar-tab is-active" data-ws-panel="chat" aria-selected="true">
                        <i className="fa fa-comment" aria-hidden="true" />
                        <span>聊天</span>
                    </button>
                    <button type="button" role="tab" className="ws-sidebar-tab" data-ws-panel="character" aria-selected="false">
                        <i className="fa fa-users" aria-hidden="true" />
                        <span>角色</span>
                    </button>
                    <button type="button" role="tab" className="ws-sidebar-tab" data-ws-panel="settings" aria-selected="false">
                        <i className="fa fa-sliders" aria-hidden="true" />
                        <span>设置</span>
                    </button>
                    <button type="button" className="ws-sidebar-toggle" id="wsSidebarToggle" title="折叠 / 展开侧栏">
                        <i className="fa fa-chevron-right" aria-hidden="true" />
                    </button>
                    <button type="button" className="ws-sidebar-toggle" id="wsChatOverlayButton" title="展开聊天浮层（Ctrl+Shift+C）">
                        <i className="fa fa-expand" aria-hidden="true" />
                    </button>
                </div>

                <div className="ws-chrome ws-sidebar-settings" id="wsSidebarSettings">
                    <h3>界面与布局</h3>
                    <div className="ws-setting-block">
                        <span className="ws-setting-label">主视图</span>
                        <div className="ws-layout-choices" id="wsLayoutSettings" role="group" aria-label="主视图布局">
                            <button type="button" data-ws-layout="canvas">画布</button>
                            <button type="button" data-ws-layout="conversation">聊天</button>
                        </div>
                        <p className="ws-setting-hint">切换布局不会重载页面，聊天位置与地图镜头会保留。</p>
                    </div>
                    <label className="ws-setting-block" htmlFor="wsSidebarWidth">
                        <span className="ws-setting-label">侧栏宽度</span>
                        <input type="range" id="wsSidebarWidth" min="360" max="420" step="4" value="380" />
                        <span className="ws-setting-value" id="wsSidebarWidthValue">380px</span>
                    </label>
                    <div className="ws-setting-block">
                        <span className="ws-setting-label">覆盖范围</span>
                        <div className="ws-layout-scopes" id="wsLayoutScopes" role="group" aria-label="布局覆盖范围">
                            <button type="button" data-ws-scope="session" className="is-active">仅本次会话</button>
                            <button type="button" data-ws-scope="room">当前房间</button>
                            <button type="button" data-ws-scope="default">我的默认</button>
                        </div>
                        <p className="ws-setting-hint" id="wsLayoutScopeHint">仅本次会话：刷新或重进房间后失效。</p>
                    </div>
                </div>

                <div className="ws-chrome ws-sidebar-resizer" id="wsSidebarResizer" title="拖动调整侧栏宽度" aria-hidden="true" />
                <button type="button" className="ws-chrome ws-layout-float" id="wsLayoutFloat" title="切换回画布布局">
                    <i className="fa fa-columns" aria-hidden="true" />
                    <span>画布</span>
                </button>
                <div className="ws-chrome ws-chat-backdrop" id="wsChatBackdrop" aria-hidden="true" />
                <div
                    className="ws-chrome ws-chat-drawer-handle"
                    id="wsChatDrawerHandle"
                    role="button"
                    tabIndex={0}
                    aria-label="展开或收起聊天抽屉"
                >
                    <span aria-hidden="true" />
                </div>

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
                        <div className="room-usage-meter" id="roomUsageMeter" hidden aria-live="polite">
                            <span id="roomUsageTokens">0 tokens</span>
                            <span className="room-usage-sep" aria-hidden="true">·</span>
                            <span id="roomUsageCost">未配置单价</span>
                        </div>
                    </header>

                    <section className="combat-panel" id="combatPanel" hidden aria-live="polite">
                        <header className="combat-panel-head">
                            <div className="combat-panel-brand">
                                <i className="fa fa-shield" aria-hidden="true" />
                                <span className="combat-panel-kicker">COMBAT</span>
                                <strong data-i18n="combat.title">战斗轮</strong>
                                <span className="combat-panel-round" id="combatRound">第 1 轮</span>
                            </div>
                            <button
                                className="combat-panel-toggle"
                                id="combatPanelToggle"
                                type="button"
                                title="收起 / 展开战斗面板"
                                aria-expanded="true"
                                aria-controls="combatPanelBody"
                            >
                                <i className="fa fa-chevron-up" aria-hidden="true" />
                            </button>
                        </header>
                        <div className="combat-panel-body" id="combatPanelBody">
                            <div className="combat-order" id="combatOrder" />
                            <div className="combat-main">
                                <div className="combat-current" id="combatCurrent" />
                                <div className="combat-actions" id="combatActions" />
                                <div className="combat-form" id="combatForm" hidden />
                                <div className="combat-log" id="combatLog" aria-label="战斗日志" />
                            </div>
                        </div>
                    </section>

                    {/* 场景画面与配乐面板：桌面端停靠右侧栏底部；窄屏由脚本移回此宿主。 */}
                    <div className="immersive-chat-stage-host" id="immersiveChatStageHost" />

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
                            <button
                                className="btn btn-outline-primary"
                                id="diceRollButton"
                                type="button"
                                title="投掷骰子"
                                aria-label="投掷骰子"
                                data-i18n-title="chat.action.roll"
                                hidden
                            >
                                <i className="fa fa-hand-pointer-o" aria-hidden="true" />{" "}
                                <span data-i18n="chat.action.roll">投掷</span>
                            </button>
                            <button
                                className="btn btn-outline-warning"
                                id="worldOverrideButton"
                                type="button"
                                title="世界意志：直接改写结果"
                                aria-label="世界意志：直接改写结果"
                                hidden
                            >
                                <i className="fa fa-bolt" aria-hidden="true" />
                            </button>
                            <button
                                className="btn btn-outline-secondary immersive-audio-toggle"
                                id="homeRoomMusicToggle"
                                type="button"
                                title="配乐：仅切换自己的实时配乐"
                                aria-label="切换实时配乐"
                                aria-pressed="true"
                                data-enabled="true"
                                onClick={toggleRailMusic}
                            >
                                <i className="fa fa-music" aria-hidden="true" />
                            </button>
                            <button
                                className="btn btn-outline-secondary immersive-audio-toggle"
                                id="homeRoomAmbientToggle"
                                type="button"
                                title="环境音：仅切换自己的环境音"
                                aria-label="切换环境音"
                                aria-pressed="true"
                                data-enabled="true"
                                onClick={toggleRoomAmbient}
                            >
                                <i className="fa fa-volume-up" aria-hidden="true" />
                            </button>
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

                <aside
                    className={`home-room-members${sidebarCollapsed ? " is-collapsed" : ""}`}
                    id="homeRoomMembers"
                    aria-labelledby="homeRoomMembersTitle"
                >
                    <div className="home-room-members-content">
                        <div className="home-room-members-header">
                            <span className="home-room-members-title" id="homeRoomMembersTitle" data-i18n="home.members.title">房间成员</span>
                            <div className="home-room-members-header-actions">
                                <span className="home-room-online" id="homeRoomOnlineCount" data-i18n="room.status.online_players" data-i18n-dynamic="true">在线玩家 0/0</span>
                                <button
                                    type="button"
                                    className="home-room-members-collapse"
                                    id="homeRoomMembersCollapse"
                                    aria-expanded={!sidebarCollapsed}
                                    aria-controls="homeRoomMembersBody"
                                    title="收起右侧栏（Alt+B）"
                                    aria-label="收起右侧栏"
                                    onClick={toggleSidebar}
                                >
                                    <i className="fa fa-chevron-right" aria-hidden="true" />
                                </button>
                            </div>
                        </div>
                        <div className="home-room-members-body" id="homeRoomMembersBody">
                            <div className="home-room-member-list" id="roomMemberList" data-i18n-dynamic="true">
                                <p className="home-room-members-empty" data-i18n="home.members.empty">加入房间后这里会显示房间成员。</p>
                            </div>
                            {/* 沉浸面板停靠点：由沉浸模块把场景画面与配乐控制移到这里。 */}
                            <div className="immersive-sidebar-stage-host" id="immersiveSidebarStageHost" />
                        </div>
                    </div>
                    <div
                        className="home-room-members-rail"
                        id="homeRoomMembersRail"
                        aria-hidden={!sidebarCollapsed}
                    >
                        <button
                            type="button"
                            className="home-room-members-rail-btn home-room-members-rail-handle"
                            id="homeRoomMembersExpand"
                            title="展开右侧栏（Alt+B）"
                            aria-label="展开右侧栏"
                            onClick={toggleSidebar}
                        >
                            <i className="fa fa-chevron-left" aria-hidden="true" />
                        </button>
                        <button
                            type="button"
                            className="home-room-members-rail-btn"
                            id="homeRoomMembersRailMusic"
                            title="静音 / 恢复实时配乐"
                            aria-label="静音 / 恢复实时配乐"
                            onClick={toggleRailMusic}
                        >
                            <i className="fa fa-music" aria-hidden="true" />
                        </button>
                        <button
                            type="button"
                            className="home-room-members-rail-btn"
                            id="homeRoomMembersRailAmbient"
                            title="开启 / 关闭环境音（仅影响自己）"
                            aria-label="开启 / 关闭环境音"
                            onClick={toggleRoomAmbient}
                        >
                            <i className="fa fa-volume-up" aria-hidden="true" />
                        </button>
                    </div>
                </aside>
            </div>

            {/*
                挂到 body：模态必须留在页面最外层。
                主页标签页带 pageViewEnter 动画（animation-fill-mode: both），
                动画结束后元素仍构成层叠上下文，位于其中的 .modal 就算 z-index
                高于遮罩，也会被 body 上的 .modal-backdrop 整片盖住——
                表现就是点开世界意志后全屏变黑、弹窗却看不见。
            */}
            {createPortal(
            <div
                className="modal fade"
                id="worldOverrideModal"
                tabIndex={-1}
                aria-labelledby="worldOverrideModalLabel"
                aria-hidden="true"
            >
                <div className="modal-dialog modal-dialog-centered">
                    <div className="modal-content world-override-modal">
                        <div className="modal-header world-override-header">
                            <div className="world-override-heading">
                                <span className="world-override-kicker">DIRECT OVERRIDE</span>
                                <h5 className="modal-title" id="worldOverrideModalLabel">
                                    <i className="fa fa-bolt" aria-hidden="true" />
                                    <span data-i18n="world_override.title">世界意志</span>
                                </h5>
                            </div>
                            <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close" />
                        </div>
                        <div className="modal-body">
                            <p className="world-override-hint" data-i18n="world_override.hint">
                                写下结果，世界照做。
                            </p>
                            <p className="form-text world-override-limits" id="worldOverrideLimits" />
                            <textarea
                                className="form-control world-override-input"
                                id="worldOverrideInput"
                                rows={3}
                                placeholder="例如：门自行打开；我出现在地下室。"
                                data-i18n-placeholder="world_override.placeholder"
                            />
                            <p className="form-text world-override-message" id="worldOverrideMessage" aria-live="polite" />
                        </div>
                        <div className="modal-footer">
                            <button type="button" className="btn btn-secondary" data-bs-dismiss="modal" data-i18n="common.cancel">
                                取消
                            </button>
                            <button type="button" className="btn btn-danger" id="worldOverrideConfirm">
                                <i className="fa fa-bolt" aria-hidden="true" />{" "}
                                <span data-i18n="world_override.execute">执行</span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>,
            document.body,
            )}
        </>
    );
}
