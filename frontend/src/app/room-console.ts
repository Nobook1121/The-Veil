/**
 * 房间内嵌 Web 控制台。
 *
 * 仅显示当前房间的日志（日志存储在该房间目录内，随房间一起归档），并允许房间
 * 所有者/管理员执行仅在房间范围内生效的命令。历史日志通过 REST 端点拉取，新增
 * 条目通过 Socket.IO 的 `room_console_log` 事件实时追加。
 */
(function initializeRoomConsole(global: Window) {
    "use strict";

    const MAX_RENDER_ENTRIES = 500;

    let activeRoomId = "";
    let canExecute = false;
    let bound = false;
    let entries: RoomConsoleEntry[] = [];

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function levelClass(level?: string): string {
        switch ((level || "info").toLowerCase()) {
            case "error":
                return "text-danger";
            case "warning":
                return "text-warning";
            case "debug":
                return "text-secondary";
            default:
                return "";
        }
    }

    function formatDetails(details?: Record<string, unknown>): string {
        if (!details) return "";
        const parts = Object.keys(details).map((key) => `${key}=${String(details[key])}`);
        return parts.length ? `  ${parts.join(" ")}` : "";
    }

    function renderEntry(entry: RoomConsoleEntry): void {
        const output = byId("roomConsoleOutput");
        if (!output) return;
        const line = document.createElement("div");
        line.className = `room-console-line ${levelClass(entry.level)}`.trim();
        const actor = entry.actor ? `${entry.actor}: ` : "";
        line.textContent = `[${entry.ts || ""}] ${actor}${entry.message || ""}${formatDetails(entry.details)}`;
        output.appendChild(line);
        output.scrollTop = output.scrollHeight;
    }

    function renderAll(): void {
        const output = byId("roomConsoleOutput");
        if (!output) return;
        output.innerHTML = "";
        const empty = byId("roomConsoleEmpty");
        if (empty) empty.hidden = entries.length > 0;
        for (const entry of entries) renderEntry(entry);
    }

    function nowStamp(): string {
        const now = new Date();
        const pad = (value: number): string => String(value).padStart(2, "0");
        return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} `
            + `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    }

    // 客户端本地提示：仅在请求本身失败（网络/HTTP 错误）时使用。命令的正常结果
    // （包括 warning / error 级别）由服务端广播到房间控制台，不使用弹窗提示。
    function appendLocal(message: string, level: "info" | "warning" | "error" = "info"): void {
        const entry: RoomConsoleEntry = { ts: nowStamp(), level, source: "client", message };
        entries.push(entry);
        if (entries.length > MAX_RENDER_ENTRIES) entries = entries.slice(-MAX_RENDER_ENTRIES);
        const empty = byId("roomConsoleEmpty");
        if (empty) empty.hidden = true;
        renderEntry(entry);
    }

    // 只有会改动数据的房间命令才需要「预演 → 确认」。依据命令本身判断，绝不根据
    // 返回文本猜测，否则 help 帮助文本里的「预演」二字会误触发确认弹窗。
    const DESTRUCTIVE_COMMANDS = ["log clear", "archive"];
    function isDestructiveCommand(command: string): boolean {
        const normalized = command.trim().replace(/\s+/g, " ").toLowerCase();
        if (/(?:^|\s)(?:--yes|-y)(?=\s|$)/.test(normalized)) return false;
        return DESTRUCTIVE_COMMANDS.some(
            (name) => normalized === name || normalized.startsWith(`${name} `)
        );
    }

    function applyPermissions(): void {
        const input = byId<HTMLInputElement>("roomConsoleInput");
        if (input) input.disabled = !canExecute;
        const send = byId<HTMLButtonElement>("roomConsoleSend");
        if (send) send.hidden = !canExecute;
        const clear = byId<HTMLButtonElement>("roomConsoleClear");
        if (clear) clear.hidden = !canExecute;
        const hint = byId("roomConsoleReadOnlyHint");
        if (hint) hint.hidden = canExecute;
    }

    async function loadLog(roomId: string): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<{ entries?: RoomConsoleEntry[] }>>(
                `/api/rooms/${encodeURIComponent(roomId)}/console/log?limit=200`
            );
            entries = response.success && response.data?.entries ? response.data.entries : [];
        } catch {
            entries = [];
        }
        renderAll();
    }

    async function submitCommand(command: string, extraFlag = ""): Promise<void> {
        let response: ApiResponse<{ ok: boolean; message: string }>;
        try {
            response = await TrpgApi.post<ApiResponse<{ ok: boolean; message: string }>>(
                `/api/rooms/${encodeURIComponent(activeRoomId)}/console/command`,
                { command: `${command}${extraFlag}` }
            );
        } catch (error) {
            appendLocal((error as Error).message || "命令执行失败", "error");
            return;
        }
        if (!response.success || !response.data) {
            appendLocal(response.message || "命令执行失败", "error");
            return;
        }
        const result = response.data;
        // 命令结果（含 warning / error）统一显示在房间控制台内，不再弹窗提示。
        if (!result.ok) return;
        // 破坏性命令缺省返回「预演」文本，确认后再带 --yes 真正执行。
        if (isDestructiveCommand(command) && result.message.includes("预演")) {
            const confirmed = await global.TrpgDialog?.confirm({
                title: "确认执行",
                message: result.message,
                confirmText: "执行",
                danger: true,
            });
            if (confirmed) await submitCommand(command, " --yes");
        }
    }

    async function handleSend(): Promise<void> {
        const input = byId<HTMLInputElement>("roomConsoleInput");
        const command = (input?.value || "").trim();
        if (!command || !activeRoomId || !canExecute) return;
        if (input) input.value = "";
        await submitCommand(command);
    }

    async function handleClear(): Promise<void> {
        if (!activeRoomId || !canExecute) return;
        const confirmed = await global.TrpgDialog?.confirm({
            title: "清空日志",
            message: "确认清空本房间控制台日志？该操作不可恢复。",
            confirmText: "清空",
            danger: true,
        });
        if (!confirmed) return;
        const response = await TrpgApi.del<ApiResponse<{ cleared: number }>>(
            `/api/rooms/${encodeURIComponent(activeRoomId)}/console/log`
        );
        if (!response.success) {
            appendLocal(response.message || "清空失败", "error");
            return;
        }
        entries = [];
        renderAll();
    }

    function bindEvents(): void {
        if (bound) return;
        bound = true;
        byId("roomConsoleSend")?.addEventListener("click", () => { void handleSend(); });
        byId("roomConsoleClear")?.addEventListener("click", () => { void handleClear(); });
        byId<HTMLInputElement>("roomConsoleInput")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                void handleSend();
            }
        });
    }

    global.openRoomConsole = (roomId: string, options?: RoomConsoleOpenOptions): void => {
        if (!roomId) return;
        bindEvents();
        const panel = byId("roomConsolePanel");
        // 房间控制台只对网站管理员/所有者与房间所有者/管理员显示，
        // 普通房间成员既看不到面板也无法通过接口读取。
        if (panel) panel.hidden = options?.invisibleView === true || options?.canExecute !== true;
        canExecute = options?.canExecute === true;
        applyPermissions();
        if (activeRoomId !== roomId) {
            activeRoomId = roomId;
            entries = [];
            renderAll();
            void loadLog(roomId);
        }
    };

    global.appendRoomConsoleEntry = (payload?: RoomConsoleSocketPayload): void => {
        const entry = payload?.entry;
        if (!entry) return;
        if (activeRoomId && payload?.room_id && payload.room_id !== activeRoomId) return;
        entries.push(entry);
        if (entries.length > MAX_RENDER_ENTRIES) entries = entries.slice(-MAX_RENDER_ENTRIES);
        const empty = byId("roomConsoleEmpty");
        if (empty) empty.hidden = true;
        renderEntry(entry);
    };

    global.closeRoomConsole = (): void => {
        activeRoomId = "";
        entries = [];
        renderAll();
        const panel = byId("roomConsolePanel");
        if (panel) panel.hidden = false;
    };
})(window);
