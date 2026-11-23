/**
 * 全局更新提示（只对 OWNER 显示）。
 *
 * 应用启动后按 6 小时节流检查一次 GitHub Release：
 * - 有新版本 → 弹出「立即更新 / 稍后 / 忽略此版本」；
 * - 已下载完成 → 直接「立即更新并重启」；
 * - 「立即更新」会依次触发下载（如需要）→ 应用 → 等待服务重启 → 自动刷新页面。
 *
 * 忽略只记住版本号（localStorage），不关掉后续版本的提示。
 */
(function initializeUpdatePrompt(global: Window): void {
    "use strict";

    const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
    const LAST_CHECK_KEY = "veil.update.lastCheck";
    const DISMISSED_KEY = "veil.update.dismissed";
    const POLL_MS = 1500;
    const MAX_POLLS = 240;

    let prompted = false;
    let busy = false;

    interface UpdateState {
        state?: string;
        current?: string;
        latest?: string;
        message?: string;
        progress?: number;
        enabled?: boolean;
        owner?: boolean;
    }

    function isOwner(): boolean {
        const user = (global as unknown as { currentUser?: { role?: string } }).currentUser;
        return String(user?.role || "").toUpperCase() === "OWNER";
    }

    function store(key: string): string {
        try {
            return global.localStorage.getItem(key) || "";
        } catch {
            return "";
        }
    }

    function save(key: string, value: string): void {
        try {
            global.localStorage.setItem(key, value);
        } catch {
            /* 隐私模式下静默失败即可 */
        }
    }

    function notify(message: string, type = "info"): void {
        global.showNotification?.(message, type);
    }

    async function getState(): Promise<UpdateState | null> {
        try {
            const response = await global.TrpgApi.get<{ success?: boolean; data?: UpdateState }>("/api/app/update");
            return response?.success ? response.data || null : null;
        } catch {
            return null;
        }
    }

    async function post(path: string): Promise<UpdateState | null> {
        try {
            const response = await global.TrpgApi.post<{ success?: boolean; data?: UpdateState; message?: string }>(path);
            if (!response?.success) {
                notify(response?.message || "更新操作失败，请稍后重试", "error");
                return null;
            }
            return response.data || null;
        } catch (error) {
            notify(`更新操作失败：${(error as Error).message}`, "error");
            return null;
        }
    }

    function sleep(ms: number): Promise<void> {
        return new Promise((resolve) => global.setTimeout(resolve, ms));
    }

    // ---------------------------------------------------------------- 弹窗
    let dialog: HTMLElement | null = null;
    let statusNode: HTMLElement | null = null;
    let primaryButton: HTMLButtonElement | null = null;

    function ensureDialog(): void {
        if (dialog) return;
        const overlay = document.createElement("div");
        overlay.className = "veil-update-overlay";
        overlay.hidden = true;
        overlay.innerHTML = `
            <div class="veil-update-card" role="dialog" aria-modal="true" aria-labelledby="veilUpdateTitle">
                <h3 id="veilUpdateTitle"><i class="fa fa-cloud-download" aria-hidden="true"></i> 发现新版本</h3>
                <p class="veil-update-body" data-role="body"></p>
                <p class="veil-update-status" data-role="status" hidden></p>
                <div class="veil-update-actions">
                    <button type="button" class="btn btn-primary" data-role="primary">立即更新</button>
                    <button type="button" class="btn btn-outline-secondary" data-role="later">稍后</button>
                    <button type="button" class="btn btn-link" data-role="dismiss">忽略此版本</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        dialog = overlay;
        statusNode = overlay.querySelector<HTMLElement>('[data-role="status"]');
        primaryButton = overlay.querySelector<HTMLButtonElement>('[data-role="primary"]');
        overlay.querySelector<HTMLButtonElement>('[data-role="later"]')?.addEventListener("click", () => hideDialog());
        overlay.querySelector<HTMLButtonElement>('[data-role="dismiss"]')?.addEventListener("click", () => {
            const latest = overlay.dataset.latest || "";
            if (latest) save(DISMISSED_KEY, latest);
            hideDialog();
        });
        primaryButton?.addEventListener("click", () => void runUpdate());
    }

    function showDialog(state: UpdateState): void {
        ensureDialog();
        if (!dialog) return;
        dialog.hidden = false;
        dialog.dataset.latest = state.latest || "";
        const body = dialog.querySelector<HTMLElement>('[data-role="body"]');
        if (body) {
            body.textContent =
                state.state === "ready"
                    ? `更新包已下载并通过校验：v${state.current} → v${state.latest}。点击「立即更新」会替换程序文件并重启服务，房间与角色数据会保留。`
                    : `GitHub 上有新版本 v${state.latest}（当前 v${state.current}）。点击「立即更新」会自动下载、校验并重启服务，房间与角色数据会保留。`;
        }
        if (statusNode) {
            statusNode.hidden = true;
            statusNode.textContent = "";
        }
        if (primaryButton) {
            primaryButton.disabled = false;
            primaryButton.textContent = "立即更新";
        }
    }

    function hideDialog(): void {
        if (dialog) dialog.hidden = true;
    }

    function setStatus(text: string): void {
        if (!statusNode) return;
        statusNode.hidden = !text;
        statusNode.textContent = text;
    }

    // ---------------------------------------------------------------- 更新流程
    async function waitForReady(): Promise<UpdateState | null> {
        for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
            const state = await getState();
            if (!state) {
                await sleep(POLL_MS);
                continue;
            }
            if (state.state === "downloading") {
                setStatus(`正在下载更新包… ${Math.max(0, Math.min(state.progress || 0, 100))}%`);
            } else if (state.state === "ready") {
                return state;
            } else if (state.state === "error") {
                setStatus(state.message || "下载失败，请稍后重试");
                return null;
            }
            await sleep(POLL_MS);
        }
        setStatus("等待下载完成超时，请到「设置 → 关于」重试");
        return null;
    }

    /** 更新完成后等待服务重启：/api/app/info 拿到新版本号即刷新页面。 */
    async function waitForRestart(previous: string): Promise<void> {
        for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
            try {
                const response = await global.TrpgApi.get<{ success?: boolean; data?: { version?: string } }>("/api/app/info");
                const version = response?.data?.version || "";
                if (response?.success && version && version !== previous) {
                    setStatus(`已更新到 v${version}，正在刷新页面…`);
                    await sleep(800);
                    global.location.reload();
                    return;
                }
            } catch {
                /* 服务重启期间请求会失败，继续等 */
            }
            await sleep(POLL_MS);
        }
        setStatus("更新已应用，请手动刷新页面或重新打开应用");
    }

    async function runUpdate(): Promise<void> {
        if (busy) return;
        busy = true;
        if (primaryButton) {
            primaryButton.disabled = true;
            primaryButton.textContent = "更新中…";
        }
        const before = (await getState())?.current || "";
        let state = await getState();
        if (state?.state === "available") {
            setStatus("开始下载更新包…");
            state = await post("/api/app/update/download");
            if (!state) {
                busy = false;
                if (primaryButton) {
                    primaryButton.disabled = false;
                    primaryButton.textContent = "立即更新";
                }
                return;
            }
            state = await waitForReady();
            if (!state) {
                busy = false;
                if (primaryButton) {
                    primaryButton.disabled = false;
                    primaryButton.textContent = "立即更新";
                }
                return;
            }
        }
        setStatus("正在应用更新并重启服务…");
        const applied = await post("/api/app/update/apply");
        if (!applied) {
            busy = false;
            if (primaryButton) {
                primaryButton.disabled = false;
                primaryButton.textContent = "立即更新";
            }
            return;
        }
        await waitForRestart(before);
        busy = false;
    }

    // ---------------------------------------------------------------- 入口
    function shouldCheckNow(): boolean {
        if (!isOwner()) return false;
        if (document.body?.dataset.setupWizard === "1") return false;
        const last = Number(store(LAST_CHECK_KEY) || 0);
        return !last || Date.now() - last > CHECK_INTERVAL_MS;
    }

    async function checkOnce(): Promise<void> {
        if (prompted || !shouldCheckNow()) return;
        prompted = true;
        save(LAST_CHECK_KEY, String(Date.now()));
        let state = await getState();
        if (!state || !state.enabled) return;
        if (state.owner === false) return;
        if (state.state !== "available" && state.state !== "ready") return;
        if (!state.latest || state.latest === state.current) return;
        if (store(DISMISSED_KEY) === state.latest) return;
        showDialog(state);
    }

    function boot(): void {
        if (!isOwner()) return;
        global.setTimeout(() => void checkOnce(), 4000);
        global.addEventListener("trpg:user-changed", () => {
            prompted = false;
            void checkOnce();
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else {
        boot();
    }
})(window);
