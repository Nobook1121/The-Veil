/**
 * 虚拟投掷桌：在骰娘消息内嵌桌面、播放当前档位动画，并驱动玩家投掷流程。
 * 只影响本机渲染；服务端结果是权威值。
 */
class DiceTableHandle {
    readonly requestId: string;
    readonly plan: DicePlan;
    meta: DiceRollMeta | null = null;
    readonly card: HTMLElement;
    private stageElement: HTMLElement;
    private resultElement: HTMLElement;
    private labelElement: HTMLElement;
    private statusElement: HTMLElement;
    private actionElement: HTMLElement;
    private renderer: DiceSimpleRenderer | Dice3DAdapter | null = null;
    played = false;
    destroyed = false;

    constructor(plan: DicePlan, requestId: string, meta: DiceRollMeta | null = null) {
        this.plan = plan;
        this.requestId = requestId;
        this.meta = meta;
        this.card = document.createElement("div");
        this.card.className = "dice-table-card";
        this.card.dataset.requestId = requestId;
        this.card.innerHTML = `
            <div class="dice-table-surface">
                <div class="dice-table-label"></div>
                <div class="dice-table-stage"></div>
                <div class="dice-table-result" aria-live="polite"></div>
            </div>
            <div class="dice-table-status"></div>
            <div class="dice-table-actions"></div>
        `;
        this.labelElement = this.card.querySelector(".dice-table-label") as HTMLElement;
        this.stageElement = this.card.querySelector(".dice-table-stage") as HTMLElement;
        this.resultElement = this.card.querySelector(".dice-table-result") as HTMLElement;
        this.statusElement = this.card.querySelector(".dice-table-status") as HTMLElement;
        this.actionElement = this.card.querySelector(".dice-table-actions") as HTMLElement;
        diceApplyAppearance(this.card, diceLoadAppearance());
        this.updateLabel();
    }

    private updateLabel(): void {
        const check = this.plan.check || {};
        const parts: string[] = [];
        if (check.name) parts.push(String(check.name));
        if (check.difficulty === "hard") parts.push("困难");
        if (check.difficulty === "extreme") parts.push("极难");
        if (check.target !== null && check.target !== undefined) parts.push(`目标 ${check.target}`);
        if (this.plan.mode === "standard") parts.push(String(this.plan.expression || ""));
        this.labelElement.textContent = parts.join(" · ") || "投掷";
    }

    attachTo(container: HTMLElement): void {
        if (!this.destroyed) container.appendChild(this.card);
    }

    attachToMessage(messageElement: HTMLElement): void {
        const content = messageElement.querySelector<HTMLElement>(".message-content") || messageElement;
        this.attachTo(content);
    }

    private faceUrls(): Record<string, string> {
        const appearance = diceLoadAppearance();
        const dieType = diceDieTypeFor(Number(this.plan.sides) || 6, this.plan.mode);
        return diceFaceUrls(dieType, appearance.diceSkinIds);
    }

    setWaiting(payload: DiceRequestPayload | null): void {
        const name = payload?.roller?.username || "玩家";
        this.statusElement.textContent = `等待 ${name} 投掷骰子…`;
        this.statusElement.className = "dice-table-status waiting";
        this.actionElement.innerHTML = "";
        const isTarget = Boolean(payload && String(payload.roller?.user_id ?? "") === String(window.currentUser?.user_id ?? ""));
        if (isTarget && payload) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-sm btn-primary dice-table-throw";
            button.textContent = "投掷";
            button.addEventListener("click", () => {
                button.disabled = true;
                void DiceTable.rollRequest(payload.request_id);
            });
            this.actionElement.appendChild(button);
        }
    }

    setCancelled(reason: string): void {
        this.statusElement.textContent = reason || "投掷被管理员取消";
        this.statusElement.className = "dice-table-status cancelled";
        this.actionElement.innerHTML = "";
        this.renderer?.dispose();
        this.renderer = null;
    }

    private setRenderer(renderer: DiceSimpleRenderer | Dice3DAdapter): void {
        this.renderer?.dispose();
        this.renderer = renderer;
    }

    private renderStatic(snapshot: DiceResultSnapshot): void {
        this.renderer?.dispose();
        this.renderer = null;
        this.stageElement.innerHTML = "";
        this.updateLabel();
        this.setResult(snapshot);
        try {
            const simple = new DiceSimpleRenderer(this.stageElement, this.plan, this.faceUrls());
            this.setRenderer(simple);
            simple.static(snapshot);
        } catch (error) {
            console.warn("静态骰面渲染失败", error);
            this.renderTextFallback(snapshot);
        }
    }

    private renderTextFallback(snapshot: DiceResultSnapshot): void {
        this.stageElement.innerHTML = "";
        const text = document.createElement("div");
        text.className = "dice-text-fallback";
        text.textContent = (snapshot.labels || []).join(" / ") || String(snapshot.total ?? snapshot.roll ?? "");
        this.stageElement.appendChild(text);
    }

    private setResult(snapshot: DiceResultSnapshot): void {
        const level = String(snapshot.success_level || "");
        const labels: Record<string, string> = {
            critical: "大成功",
            extreme: "极难成功",
            hard: "困难成功",
            regular: "成功",
            failure: "失败",
            fumble: "大失败",
        };
        const value = snapshot.mode === "percentile" ? snapshot.roll : snapshot.total;
        const parts: string[] = [];
        if (snapshot.mode === "percentile") {
            parts.push(`<span class="dice-total">${String(value ?? "")}</span>`);
        } else {
            parts.push(`<span class="dice-total">${String(value ?? "")}</span>`);
        }
        if (snapshot.success !== null && snapshot.success !== undefined) {
            const ok = Boolean(snapshot.success);
            parts.push(`<span class="dice-verdict ${ok ? "success" : "failure"} ${level === "critical" || level === "fumble" ? "critical" : ""}">${labels[level] || (ok ? "成功" : "失败")}</span>`);
        }
        if (snapshot.target !== null && snapshot.target !== undefined) {
            parts.push(`<span class="dice-target">目标 ${snapshot.target}</span>`);
        }
        if (snapshot.roller) {
            parts.push(`<span class="dice-roller">${String(snapshot.roller)}</span>`);
        }
        if (snapshot.auto) {
            parts.push(`<span class="dice-auto">等待超时，系统自动投掷</span>`);
        }
        this.resultElement.innerHTML = parts.join("");
    }

    async play(snapshot: DiceResultSnapshot, animate = true): Promise<void> {
        this.played = true;
        this.actionElement.innerHTML = "";
        this.statusElement.textContent = "";
        this.statusElement.className = "dice-table-status";
        this.setResult(snapshot);
        if (!animate) {
            this.renderStatic(snapshot);
            return;
        }
        const roomId = String(window.currentRoom?.id || "");
        const baseLevel = (window.currentRoom?.dice_animation?.level || "3d") as DiceAnimationLevel;
        let level = DiceDegradation.effectiveLevel(roomId, baseLevel);
        if (level === "text") {
            this.renderTextFallback(snapshot);
            return;
        }
        const faceUrls = this.faceUrls();
        if (level === "3d") {
            if (!DiceDegradation.webglAvailable()) {
                DiceDegradation.report(roomId, "3d", "simple", "webgl_unavailable", { scope: "session", dice: { expression: this.plan.expression } });
                level = "simple";
            } else {
                try {
                    const loaded = await Dice3DAdapter.loadBundle();
                    if (!loaded) throw new Error("dice3d_bundle_missing");
                    const adapter = new Dice3DAdapter(this.stageElement, this.plan, faceUrls);
                    this.setRenderer(adapter);
                    await adapter.play(snapshot);
                    return;
                } catch (error) {
                    const message = error instanceof Error ? error.message : String(error);
                    const sessionFailure = message.includes("bundle_missing") || !DiceDegradation.webglAvailable();
                    DiceDegradation.report(roomId, "3d", "simple", sessionFailure ? "bundle_load_failed" : "render_error", {
                        scope: sessionFailure ? "session" : "roll",
                        error: message,
                        dice: { expression: this.plan.expression },
                        requestId: this.requestId,
                    });
                    this.setRenderer(new DiceSimpleRenderer(this.stageElement, this.plan, faceUrls));
                    level = "simple";
                }
            }
        }
        if (level === "simple") {
            try {
                const simple = this.renderer instanceof DiceSimpleRenderer
                    ? this.renderer
                    : new DiceSimpleRenderer(this.stageElement, this.plan, faceUrls);
                this.setRenderer(simple);
                await simple.play(snapshot);
                return;
            } catch (error) {
                DiceDegradation.report(roomId, "simple", "text", "animation_error", {
                    scope: "roll",
                    error: error instanceof Error ? error.message : String(error),
                    dice: { expression: this.plan.expression },
                    requestId: this.requestId,
                });
            }
        }
        this.renderTextFallback(snapshot);
    }

    setGameForced(snapshot: DiceResultSnapshot, operator: string): void {
        this.statusElement.textContent = operator ? `${operator} 强制投掷` : "管理员强制投掷";
        this.statusElement.className = "dice-table-status forced";
    }

    dispose(): void {
        this.destroyed = true;
        this.renderer?.dispose();
        this.renderer = null;
        this.card.remove();
    }
}

const DiceTable = (() => {
    const handles = new Map<string, DiceTableHandle>();
    const playedRequests = new Set<string>();
    let pending: DiceRequestPayload | null = null;
    let inputBound = false;
    let rolling = false;
    let lastRoomId = "";

    function roomId(): string {
        return String(window.currentRoom?.id || "");
    }

    function baseLevel(): DiceAnimationLevel {
        return (window.currentRoom?.dice_animation?.level || "3d") as DiceAnimationLevel;
    }

    function effectiveLevel(): DiceAnimationLevel {
        return DiceDegradation.effectiveLevel(roomId(), baseLevel());
    }

    function isTarget(payload: DiceRequestPayload | null): boolean {
        if (!payload?.roller) return false;
        return String(payload.roller.user_id ?? "") === String(window.currentUser?.user_id ?? "");
    }

    function planFromMeta(meta: DiceRollMeta): DicePlan {
        return {
            mode: meta.mode || "standard",
            expression: meta.expression || "1d100",
            count: Number(meta.count) || 1,
            sides: Number(meta.sides) || 6,
            bonus_dice: Number(meta.bonus_dice) || 0,
            penalty_dice: Number(meta.penalty_dice) || 0,
            candidates: meta.candidates || [],
            check: {
                name: meta.check_name || "",
                difficulty: meta.difficulty || null,
                target: meta.target ?? null,
            },
        };
    }

    function snapshotFromMeta(meta: Partial<DiceRollMeta>): DiceResultSnapshot {
        return {
            faces: meta.faces || [],
            labels: meta.labels || [],
            roll: meta.roll ?? null,
            rolls: meta.rolls || [],
            total: meta.total ?? null,
            success: meta.success ?? null,
            success_level: meta.success_level ?? null,
            summary: "",
            check_name: meta.check_name ?? null,
            difficulty: meta.difficulty ?? null,
            target: meta.target ?? null,
            roller: meta.roller ?? null,
            mode: meta.mode || "standard",
            expression: meta.expression || "",
            count: meta.count ?? 0,
            sides: meta.sides ?? 0,
            candidates: meta.candidates || [],
        };
    }

    function getOrCreateHandle(requestId: string, plan: DicePlan, meta: DiceRollMeta | null): DiceTableHandle {
        const key = requestId || `local-${Date.now()}`;
        const existing = handles.get(key);
        if (existing) {
            if (meta) existing.meta = meta;
            return existing;
        }
        const handle = new DiceTableHandle(plan, key, meta);
        handles.set(key, handle);
        return handle;
    }

    function appendStandalone(handle: DiceTableHandle): void {
        const history = document.getElementById("chatHistory");
        if (!history) return;
        const wrapper = document.createElement("div");
        wrapper.className = "message dice-table-message";
        wrapper.appendChild(handle.card);
        history.appendChild(wrapper);
        history.scrollTop = history.scrollHeight;
    }

    function updateInputButton(): void {
        const button = document.getElementById("diceRollButton") as HTMLButtonElement | null;
        if (!button) return;
        const visible = Boolean(pending) && isTarget(pending);
        button.hidden = !visible;
        if (visible && pending) button.title = `投掷骰子（等待 ${pending.roller?.username || "玩家"}）`;
    }

    function textWaitingNotice(payload: DiceRequestPayload): void {
        if (isTarget(payload)) {
            showNotification(`轮到你投掷骰子了：输入 /roll 或点击聊天框旁的「投掷」按钮。等待 ${payload.roller?.username || "玩家"}。`, "warning");
        } else {
            showNotification(`等待 ${payload.roller?.username || "玩家"} 投掷骰子…`, "info");
        }
    }

    function handleRequest(payload: DiceRequestPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        pending = payload;
        playedRequests.delete(payload.request_id);
        updateInputButton();
        if (effectiveLevel() === "text") {
            textWaitingNotice(payload);
            return;
        }
        const handle = getOrCreateHandle(payload.request_id, payload.plan, null);
        if (!handle.card.isConnected) appendStandalone(handle);
        handle.setWaiting(payload);
        const history = document.getElementById("chatHistory");
        if (history) history.scrollTop = history.scrollHeight;
    }

    async function playAndComplete(payload: DiceRolledPayload): Promise<void> {
        const snapshot: DiceResultSnapshot = payload.result ? { ...payload.result } : {
            faces: payload.faces || [],
            labels: payload.labels || [],
            roll: payload.roll ?? null,
            rolls: payload.rolls || [],
            total: payload.total ?? null,
            success: payload.success ?? null,
            success_level: payload.success_level ?? null,
            summary: payload.summary || "",
            check_name: payload.check_name ?? null,
            difficulty: payload.difficulty ?? null,
            target: payload.target ?? null,
            roller: payload.roller ?? null,
            mode: payload.mode || "standard",
            expression: payload.expression || "",
            count: payload.count ?? 0,
            sides: payload.sides ?? 0,
            candidates: payload.candidates || [],
        };
        if (payload.auto) snapshot.auto = true;
        const requestId = String(payload.request_id || "");
        let handle = requestId ? handles.get(requestId) : undefined;
        if (!handle && effectiveLevel() !== "text") {
            const meta = payload as unknown as DiceRollMeta;
            const plan: DicePlan = meta.expression ? planFromMeta(meta) : {
                mode: "standard", expression: "1d100", count: 1, sides: 100, bonus_dice: 0, penalty_dice: 0, candidates: [], check: null,
            };
            handle = getOrCreateHandle(requestId || `local-${Date.now()}`, plan, meta);
            appendStandalone(handle);
        }
        if (requestId) playedRequests.add(requestId);
        if (handle && effectiveLevel() !== "text") {
            if (!handle.card.isConnected) appendStandalone(handle);
            await handle.play(snapshot, true);
        }
        if (requestId) await completeRequest(requestId);
        if (effectiveLevel() === "text") {
            showNotification(snapshot.summary || `投掷结果：${snapshot.roll ?? snapshot.total ?? ""}`, "info");
        }
        pending = null;
        updateInputButton();
    }

    function handleRolled(payload: DiceRolledPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        // 本机已通过 /roll 或按钮完成结算时，忽略自己触发的广播，避免重复播放。
        if (payload.request_id && playedRequests.has(String(payload.request_id))) return;
        void playAndComplete(payload);
    }

    function handleCancelled(payload: DiceRolledPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        const handle = requestId ? handles.get(requestId) : undefined;
        const reason = payload.reason === "cancelled" ? `${payload.operator || "管理员"} 取消了投掷` : "投掷已取消";
        handle?.setCancelled(reason);
        showNotification(reason, "warning");
        if (pending?.request_id === requestId) pending = null;
        updateInputButton();
    }

    function handleForced(payload: DiceRolledPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        const snapshot = payload.result || {
            faces: payload.faces || [], labels: payload.labels || [], roll: payload.roll ?? null, rolls: payload.rolls || [],
            total: payload.total ?? null, success: payload.success ?? null, success_level: payload.success_level ?? null,
            summary: "", check_name: payload.check_name ?? null, difficulty: payload.difficulty ?? null, target: payload.target ?? null,
            roller: payload.roller ?? null, mode: payload.mode || "standard", expression: payload.expression || "",
            count: payload.count ?? 0, sides: payload.sides ?? 0,
        };
        const requestId = String(payload.request_id || "");
        const handle = requestId ? handles.get(requestId) : undefined;
        if (handle) {
            handle.setGameForced(snapshot, String(payload.operator || ""));
            void handle.play(snapshot, true);
        }
        if (pending?.request_id === requestId) pending = null;
        updateInputButton();
    }

    function renderForMessage(messageElement: HTMLElement, message: ChatMessage, animate: boolean): void {
        const metadata = (message.metadata || {}) as Record<string, unknown>;
        const meta = metadata.dice_roll as DiceRollMeta | undefined;
        if (!meta || !meta.show_table) return;
        if (effectiveLevel() === "text") return;
        const requestId = String(meta.request_id || "");
        if (requestId && handles.has(requestId)) {
            const handle = handles.get(requestId)!;
            handle.attachToMessage(messageElement);
            if (!handle.played && meta.faces && meta.faces.length) {
                handle.played = true;
                void handle.play(snapshotFromMeta(meta), animate);
            }
            if (requestId === pending?.request_id) {
                pending = null;
                updateInputButton();
            }
            return;
        }
        const handle = getOrCreateHandle(requestId || `msg-${messageElement.dataset.id || Date.now()}`, planFromMeta(meta), meta);
        handle.attachToMessage(messageElement);
        handle.played = true;
        void handle.play(snapshotFromMeta(meta), animate);
    }

    async function rollRequest(requestId: string): Promise<void> {
        if (rolling) return;
        if (playedRequests.has(requestId)) {
            await completeRequest(requestId);
            return;
        }
        rolling = true;
        try {
            const response = await TrpgApi.post<ApiResponse<DiceResultSnapshot & { request_id: string }>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/roll`,
                {},
            );
            if (!response?.success || !response.data) {
                showNotification(response?.message || response?.error || "投掷失败", "error");
                return;
            }
            const snapshot = response.data as DiceResultSnapshot;
            playedRequests.add(requestId);
            const handle = handles.get(requestId);
            if (effectiveLevel() === "text") {
                await completeRequest(requestId);
                if (handle) handle.setCancelled("");
                return;
            }
            if (handle) {
                await handle.play(snapshot, true);
            }
            await completeRequest(requestId);
        } catch (error) {
            showNotification(`投掷失败：${error instanceof Error ? error.message : String(error)}`, "error");
        } finally {
            rolling = false;
        }
    }

    async function completeRequest(requestId: string): Promise<void> {
        try {
            await TrpgApi.post<ApiResponse<unknown>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/complete`,
                {},
            );
        } catch (error) {
            console.warn("完成投掷回调失败", error);
        }
    }

    async function forceRequest(requestId: string): Promise<boolean> {
        try {
            const response = await TrpgApi.post<ApiResponse<DiceResultSnapshot & { request_id: string; operator: string }>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/force`,
                {},
            );
            if (!response?.success || !response.data) {
                showNotification(response?.message || response?.error || "强制投掷失败", "error");
                return false;
            }
            return true;
        } catch (error) {
            showNotification(`强制投掷失败：${error instanceof Error ? error.message : String(error)}`, "error");
            return false;
        }
    }

    async function fetchPending(): Promise<DiceRequestPayload | null> {
        if (!roomId()) return null;
        try {
            const response = await TrpgApi.get<ApiResponse<{ pending: DiceRequestPayload | null }>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests`,
            );
            if (!response?.success) return null;
            return response.data?.pending || null;
        } catch {
            return null;
        }
    }

    async function restorePending(): Promise<void> {
        const found = await fetchPending();
        if (found && found.state !== "finished") handleRequest(found);
    }

    async function handleRollCommand(force: boolean): Promise<boolean> {
        let request = pending;
        if (!request) {
            request = await fetchPending();
            if (request) pending = request;
        }
        if (!request) {
            showNotification("当前没有等待中的投掷", "info");
            return true;
        }
        if (force) {
            return forceRequest(request.request_id);
        }
        if (!isTarget(request)) {
            showNotification(`当前等待 ${request.roller?.username || "其他玩家"} 投掷`, "warning");
            return true;
        }
        await rollRequest(request.request_id);
        return true;
    }

    function bindInput(): void {
        if (inputBound) return;
        inputBound = true;
        document.getElementById("diceRollButton")?.addEventListener("click", () => {
            if (!pending) {
                showNotification("当前没有等待中的投掷", "info");
                return;
            }
            void rollRequest(pending.request_id);
        });
        updateInputButton();
    }

    function onRoomChanged(): void {
        const id = roomId();
        if (id === lastRoomId) return;
        lastRoomId = id;
        handles.forEach((handle) => handle.dispose());
        handles.clear();
        playedRequests.clear();
        pending = null;
        DiceDegradation.resetSession();
        updateInputButton();
        void diceTextureLibrary().then(() => undefined);
        if (id) void restorePending();
    }

    function init(): void {
        lastRoomId = roomId();
        bindInput();
        void restorePending();
    }

    return {
        init,
        onRoomChanged,
        renderForMessage,
        handleRequest,
        handleRolled,
        handleCancelled,
        handleForced,
        handleRollCommand,
        restorePending,
        rollRequest,
        forceRequest,
        effectiveLevel,
        baseLevel,
        _handles: handles,
    };
})();

window.TrpgDiceTable = DiceTable;
