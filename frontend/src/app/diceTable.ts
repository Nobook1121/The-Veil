/**
 * 虚拟投掷桌：居中浮层播放当前档位动画，驱动玩家投掷、重投与确认流程。
 * 只影响本机渲染；服务端结果是权威值。
 */
const DiceTable = (() => {
    interface ActiveRoll {
        requestId: string;
        plan: DicePlan;
        overlay: DiceOverlayView;
        renderer: DiceSimpleRenderer | Dice3DAdapter | null;
        snapshot: DiceResultSnapshot | null;
        reroll: DiceRerollState | null;
        state: string;
        playKey: string;
        playedKey: string;
        playback: Promise<void> | null;
        // 物理结果模式：本次投掷是否由骰面决定结果、共享种子与已完成的动画。
        physics: boolean;
        auto: boolean;
        rollerUserId: string;
        seed: number | null;
        animatedSeed: number | null;
        animation: Promise<void> | null;
        settledFaces: number[] | null;
    }

    const handledRequests = new Set<string>();
    const rollBacklog = new Map<string, DiceRolledPayload>();
    const queue: DiceRequestPayload[] = [];
    let active: ActiveRoll | null = null;
    let pending: DiceRequestPayload | null = null;
    let inputBound = false;
    let rolling = false;
    let lastRoomId = "";

    /** 同时存活的 3D 实例上限：超出后把最早的骰桌改为平铺 2D 结果，及时释放 WebGL context。 */
    const MAX_LIVE_3D = 4;
    const live3D = new Map<Dice3DAdapter, ActiveRoll>();
    /** 各客户端的 rolling 广播携带同一 seed；实时消息补播动画时复用它，保证画面与投掷者一致。 */
    const rollingSeeds = new Map<string, number>();

    function roomId(): string {
        return String(window.currentRoom?.id || "");
    }

    function baseLevel(): DiceAnimationLevel {
        return (window.currentRoom?.dice_animation?.level || "3d") as DiceAnimationLevel;
    }

    function effectiveLevel(): DiceAnimationLevel {
        return DiceDegradation.effectiveLevel(roomId(), baseLevel());
    }

    /** 房间是否启用现代化骰子点数生成器（客户端读朝上的面，服务端只结算）；缺省视为开启。 */
    function physicsEnabled(): boolean {
        return window.currentRoom?.dice_animation?.physics_results !== false;
    }

    function isTarget(payload: DiceRequestPayload | null): boolean {
        if (!payload?.roller) return false;
        return String(payload.roller.user_id ?? "") === String(window.currentUser?.user_id ?? "");
    }

    function canManage(): boolean {
        const role = String(window.currentUser?.role || "");
        if (role === "ADMIN" || role === "OWNER") return true;
        const room = window.currentRoom;
        if (!room) return false;
        const self = (room.members || []).find((member) => String(member.user_id) === String(window.currentUser?.user_id));
        return Boolean(self && (self.room_role === "owner" || self.room_role === "admin"));
    }

    function overlayHost(): HTMLElement {
        return document.getElementById("chatHistory")
            || document.querySelector<HTMLElement>(".home-chat-main")
            || document.body;
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
            groups: meta.groups || [],
            selected: meta.selected ?? null,
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
            groups: meta.groups || [],
            selected: meta.selected ?? null,
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
            reroll: meta.reroll ?? null,
        };
    }

    /** 只有结果快照（没有请求体）时还原骰桌与骰型，用于重连补一张静态骰桌。 */
    function planFromSnapshot(snapshot: DiceResultSnapshot): DicePlan {
        return {
            mode: snapshot.mode || "standard",
            expression: snapshot.expression || "",
            count: Number(snapshot.count) || 1,
            sides: Number(snapshot.sides) || 6,
            bonus_dice: 0,
            penalty_dice: 0,
            candidates: snapshot.candidates || [],
            groups: snapshot.groups || [],
            selected: snapshot.selected ?? null,
            check: {
                name: snapshot.check_name || "",
                difficulty: snapshot.difficulty || null,
                target: snapshot.target ?? null,
            },
        };
    }

    function snapshotFromPayload(payload: DiceRolledPayload): DiceResultSnapshot {
        if (payload.result) return { ...payload.result, auto: Boolean(payload.result.auto || payload.auto) };
        const snapshot = snapshotFromMeta(payload as Partial<DiceRollMeta>);
        snapshot.auto = Boolean(payload.auto);
        snapshot.reroll = payload.reroll ?? snapshot.reroll ?? null;
        return snapshot;
    }

    /** 投掷者可能是字符串，也可能是 `{user_id, username}` 对象；统一取显示名，避免出现 `[object Object]`。 */
    function rollerLabel(roller: DiceResultSnapshot["roller"] | DiceRollMeta["roller"]): string {
        if (!roller) return "";
        if (typeof roller === "string") return roller;
        const name = (roller as { username?: unknown }).username;
        return name === undefined || name === null ? "" : String(name);
    }

    function notifyText(payload: DiceRequestPayload): void {
        if (isTarget(payload)) {
            showNotification(`轮到你投掷骰子了：输入 /roll 或点击聊天框旁的「投掷」按钮。`, "warning");
        } else {
            showNotification(`等待 ${payload.roller?.username || "玩家"} 投掷骰子…`, "info");
        }
    }

    function updateInputButton(): void {
        const button = document.getElementById("diceRollButton") as HTMLButtonElement | null;
        if (!button) return;
        const visible = Boolean(pending) && isTarget(pending);
        button.hidden = !visible;
        if (visible && pending) button.title = `投掷骰子（等待 ${pending.roller?.username || "玩家"}）`;
    }

    /** 按请求 ID 找到聊天里的内嵌骰桌，避免同一次投掷重复生成第二张结果卡。 */
    function findPanel(requestId: string): HTMLElement | null {
        if (!requestId) return null;
        return Array.from(document.querySelectorAll<HTMLElement>(".dice-inline")).find(
            (node) => node.dataset.requestId === requestId,
        ) || null;
    }

    function scrollChatToEnd(): void {
        const history = document.getElementById("chatHistory");
        if (history) history.scrollTop = history.scrollHeight;
    }

    /** 历史或重连消息：静态还原骰桌与结果，不重播动画。 */
    function staticPanel(plan: DicePlan, snapshot: DiceResultSnapshot, requestId: string, before?: HTMLElement): HTMLElement {
        const host = overlayHost();
        const panel = new DiceOverlayView(host, plan, requestId, false);
        if (before && before.parentElement === host) host.insertBefore(panel.root, before);
        else host.appendChild(panel.root);
        const groups = diceSnapshotGroups(plan, snapshot);
        const total = diceTotalCount(groups);
        if (total > DICE_SIMPLE_RENDER_LIMIT) {
            panel.showStageNotice(`骰子数量过多（${total} 枚，渲染上限 ${DICE_SIMPLE_RENDER_LIMIT} 枚），无法在骰桌上渲染，仅显示下方结果。`);
        } else {
            // 简易渲染器支持任意面数，历史还原不再因为骰型不常见而整桌空白。
            try {
                new DiceSimpleRenderer(panel.stage, plan, faceUrlsFor(plan, groups)).static(snapshot);
            } catch (error) {
                // 静态还原失败时只保留结果文字，不影响聊天阅读。
                console.warn("骰桌静态还原失败", error);
            }
        }
        panel.setResult(snapshot);
        panel.freeze();
        if (!before) scrollChatToEnd();
        return panel.root;
    }

    function setRenderer(target: ActiveRoll, renderer: DiceSimpleRenderer | Dice3DAdapter | null): void {
        const previous = target.renderer;
        if (previous && previous !== renderer) {
            untrack3D(previous);
            previous.dispose();
        }
        target.renderer = renderer;
        if (renderer instanceof Dice3DAdapter) track3D(renderer, target);
    }

    /** 先释放旧渲染器再创建简易渲染器：3D 实例销毁时会清理自己的 canvas，顺序反了会把新舞台一起清掉。 */
    function ensureSimpleRenderer(target: ActiveRoll, faceUrls: Record<string, string>): DiceSimpleRenderer {
        if (target.renderer instanceof DiceSimpleRenderer) return target.renderer;
        setRenderer(target, null);
        const simple = new DiceSimpleRenderer(target.overlay.stage, target.plan, faceUrls);
        setRenderer(target, simple);
        return simple;
    }

    function ensure3DRenderer(target: ActiveRoll, faceUrls: Record<string, string>): Dice3DAdapter {
        if (target.renderer instanceof Dice3DAdapter) return target.renderer;
        setRenderer(target, null);
        const adapter = new Dice3DAdapter(target.overlay.stage, target.plan, faceUrls);
        setRenderer(target, adapter);
        return adapter;
    }

    function track3D(adapter: Dice3DAdapter, target: ActiveRoll): void {
        live3D.set(adapter, target);
        trim3D();
    }

    function untrack3D(renderer: DiceSimpleRenderer | Dice3DAdapter | null | undefined): void {
        if (renderer instanceof Dice3DAdapter) live3D.delete(renderer);
    }

    /**
     * 超出上限时把最早的 3D 骰桌替换为平铺的 2D 结果，释放 WebGL context。
     * 正在投掷的骰桌不处理；刷新后所有历史骰桌本来就按 2D 静态还原。
     */
    function trim3D(): void {
        if (live3D.size <= MAX_LIVE_3D) return;
        Array.from(live3D.entries()).forEach(([adapter, target]) => {
            if (live3D.size <= MAX_LIVE_3D) return;
            if (active && adapter === active.renderer) return;
            live3D.delete(adapter);
            adapter.dispose();
            if (target.renderer === adapter) target.renderer = null;
            const snapshot = target.snapshot;
            if (!snapshot) return;
            const groups = diceSnapshotGroups(target.plan, snapshot);
            if (diceTotalCount(groups) > DICE_SIMPLE_RENDER_LIMIT) return;
            try {
                target.overlay.stage.innerHTML = "";
                const simple = new DiceSimpleRenderer(target.overlay.stage, target.plan, faceUrlsFor(target.plan, groups));
                simple.static(snapshot);
                target.renderer = simple;
                target.overlay.setResult(snapshot);
            } catch (error) {
                console.warn("3D 骰桌降级为平铺结果失败", error);
            }
        });
    }

    function faceUrlsFor(plan: DicePlan, groups: DiceGroupSpec[]): Record<string, string> {
        const appearance = diceLoadAppearance();
        const urls: Record<string, string> = {};
        groups.forEach((group) => {
            const type = diceRenderType(group.type);
            if (!type) return;
            Object.assign(urls, diceFaceUrls(type, appearance.diceSkinIds));
        });
        // 兼容按整型 sides 取贴图的旧皮肤。
        Object.assign(urls, diceFaceUrls(diceDieTypeFor(Number(plan.sides) || 6, plan.mode), appearance.diceSkinIds));
        return urls;
    }

    function unsupportedReason(groups: DiceGroupSpec[]): string {
        return diceUnsupportedReason(groups);
    }

    function createActive(requestId: string, plan: DicePlan): ActiveRoll {
        // 历史消息可能已经静态还原过同一次投掷，先移除再创建，避免两张骰桌。
        findPanel(requestId)?.remove();
        const overlay = new DiceOverlayView(overlayHost(), plan, requestId);
        scrollChatToEnd();
        return {
            requestId,
            plan,
            overlay,
            renderer: null,
            snapshot: null,
            reroll: null,
            state: "pending",
            playKey: "",
            playedKey: "",
            playback: null,
            physics: false,
            auto: false,
            rollerUserId: "",
            seed: null,
            animatedSeed: null,
            animation: null,
            settledFaces: null,
        };
    }

    /** 等待投掷时先把空骰桌铺出来，避免玩家点击后才看到桌子。 */
    async function showIdleTable(target: ActiveRoll): Promise<void> {
        if (effectiveLevel() !== "3d" || !DiceDegradation.webglAvailable()) return;
        if (target.snapshot !== null || target.animatedSeed !== null) return;
        try {
            const loaded = await Dice3DAdapter.loadBundle();
            if (!loaded || active !== target || target.snapshot !== null || target.animatedSeed !== null) return;
            const adapter = ensure3DRenderer(target, faceUrlsFor(target.plan, dicePlanGroups(target.plan)));
            adapter.showTable();
        } catch (error) {
            console.warn("骰桌预览失败", error);
        }
    }

    /** 同一份结果的重复广播（socket + REST）只播放一次，避免二次动画打乱最终朝上的骰面。 */
    function snapshotPlayKey(snapshot: DiceResultSnapshot): string {
        return JSON.stringify({
            roll: snapshot.roll ?? null,
            total: snapshot.total ?? null,
            faces: snapshot.faces || [],
            labels: snapshot.labels || [],
            selected: snapshot.selected ?? null,
            used: snapshot.reroll?.used ?? 0,
        });
    }

    async function playSnapshot(target: ActiveRoll, snapshot: DiceResultSnapshot, animate = true): Promise<void> {
        const key = snapshotPlayKey(snapshot);
        // 动画进行中收到同一结果的重复事件：复用同一次播放，等它结束后再继续。
        if (target.playKey === key && target.playback) return target.playback;
        if (target.playedKey === key && !target.playback) return;
        target.playKey = key;
        target.snapshot = snapshot;
        // 补播（没赶上 rolling 广播）时复用共享种子，轨迹才能与投掷者一致。
        if (animate && target.seed === null) {
            const shared = rollingSeeds.get(target.requestId);
            if (shared !== undefined) target.seed = shared;
        }
        const run = renderSnapshot(target, snapshot, animate);
        target.playback = run;
        try {
            await run;
        } finally {
            target.playedKey = key;
            if (target.playback === run) target.playback = null;
        }
    }

    async function renderSnapshot(target: ActiveRoll, snapshot: DiceResultSnapshot, animate: boolean): Promise<void> {
        target.overlay.setRolling();
        const groups = diceSnapshotGroups(target.plan, snapshot);
        const unsupported = unsupportedReason(groups);
        const total = diceTotalCount(groups);
        const room = roomId();
        const level0 = effectiveLevel();
        let level: DiceAnimationLevel = level0;
        if (unsupported && level === "3d") {
            DiceDegradation.report(room, "3d", "simple", unsupported.split(":")[0] || "unsupported_dice", {
                scope: "roll",
                dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                requestId: target.requestId,
            });
            level = "simple";
        }
        if (level === "text") {
            target.overlay.setResult(snapshot);
            target.overlay.setStatus(snapshot.summary || `投掷结果：${snapshot.roll ?? snapshot.total ?? ""}`, "");
            return;
        }
        // 骰子过多：不再尝试渲染，释放已有骰桌并在桌面区域提示，结果仍显示在下方。
        if (total > DICE_SIMPLE_RENDER_LIMIT) {
            DiceDegradation.report(room, level, "text", "dice_count_exceeded", {
                scope: "roll",
                error: `${total} > ${DICE_SIMPLE_RENDER_LIMIT}`,
                dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                requestId: target.requestId,
            });
            setRenderer(target, null);
            target.overlay.showStageNotice(
                `骰子数量过多（${total} 枚，渲染上限 ${DICE_SIMPLE_RENDER_LIMIT} 枚），无法在骰桌上渲染，仅显示下方结果。`,
            );
            target.overlay.setResult(snapshot);
            target.overlay.setStatus(snapshot.summary || "", "");
            return;
        }
        const faceUrls = faceUrlsFor(target.plan, groups);
        if (level === "3d") {
            if (!DiceDegradation.webglAvailable()) {
                DiceDegradation.report(room, "3d", "simple", "webgl_unavailable", {
                    scope: "session",
                    dice: { expression: target.plan.expression },
                    requestId: target.requestId,
                });
                level = "simple";
            } else {
                try {
                    const loaded = await Dice3DAdapter.loadBundle();
                    if (!loaded) throw new Error("dice3d_bundle_missing");
                    const adapter = ensure3DRenderer(target, faceUrls);
                    // 有共享 seed 时复用同一段物理轨迹，实时补播才能与投掷者看到的一致。
                    if (animate) await adapter.play(snapshot, target.seed);
                    else adapter.static(snapshot);
                    // 结果必须在动画结束后才出现，且与最终朝上的骰面一致。
                    target.overlay.setResult(snapshot);
                    return;
                } catch (error) {
                    const message = error instanceof Error ? error.message : String(error);
                    const reason = message.includes("dice_count_exceeded")
                        ? "dice_count_exceeded"
                        : message.includes("bundle_missing")
                            ? "bundle_load_failed"
                            : message.includes("webgl_unavailable") || message.includes("context")
                                ? "webgl_unavailable"
                                : "render_error";
                    const sessionFailure = reason === "bundle_load_failed" || reason === "webgl_unavailable";
                    DiceDegradation.report(room, "3d", "simple", reason, {
                        scope: sessionFailure ? "session" : "roll",
                        error: message,
                        dice: { expression: target.plan.expression },
                        requestId: target.requestId,
                    });
                    // 先销毁失败的 3D 实例，再创建简易渲染器，避免旧 canvas 覆盖新舞台。
                    setRenderer(target, null);
                    level = "simple";
                }
            }
        }
        if (level === "simple") {
            try {
                const simple = ensureSimpleRenderer(target, faceUrls);
                if (animate) await simple.play(snapshot);
                else simple.static(snapshot);
                target.overlay.setResult(snapshot);
                return;
            } catch (error) {
                DiceDegradation.report(room, "simple", "text", "geometry_error", {
                    scope: "roll",
                    error: error instanceof Error ? error.message : String(error),
                    dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                    requestId: target.requestId,
                });
            }
        }
        target.overlay.setResult(snapshot);
        target.overlay.setStatus(snapshot.summary || (snapshot.labels || []).join(" / "), "");
    }

    /** 请求体里只有骰型/骰组，没有点数：用于所有客户端播放同一段物理动画。 */
    function planSnapshot(plan: DicePlan): DiceResultSnapshot {
        return {
            faces: [],
            labels: [],
            groups: [],
            selected: plan.selected ?? null,
            roll: null,
            rolls: [],
            total: null,
            success: null,
            success_level: null,
            summary: "",
            check_name: plan.check?.name ?? null,
            difficulty: plan.check?.difficulty ?? null,
            target: plan.check?.target ?? null,
            roller: null,
            mode: plan.mode,
            expression: plan.expression,
            count: plan.count,
            sides: plan.sides,
            candidates: plan.candidates,
            auto: false,
            reroll: null,
        };
    }

    async function beginRequest(requestId: string, reroll = false): Promise<{ seed: number; rerollIndex: number } | null> {
        try {
            const response = await TrpgApi.post<ApiResponse<DiceRequestPayload>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/begin`,
                reroll ? { reroll: true } : {},
            );
            if (!response?.success || !response.data || typeof response.data.seed !== "number") {
                if (response && !response.success) {
                    showNotification(response.message || response.error || "开始投掷失败", "error");
                }
                return null;
            }
            return { seed: Number(response.data.seed), rerollIndex: Number(response.data.reroll_index) || 0 };
        } catch (error) {
            showNotification(`开始投掷失败：${error instanceof Error ? error.message : String(error)}`, "error");
            return null;
        }
    }

    async function submitFaces(requestId: string, faces: number[], fallback = false): Promise<void> {
        try {
            const response = await TrpgApi.post<ApiResponse<unknown>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/faces`,
                { faces, fallback },
            );
            if (!response?.success) {
                showNotification(response?.message || response?.error || "骰面结算失败", "error");
            }
        } catch (error) {
            showNotification(`骰面结算失败：${error instanceof Error ? error.message : String(error)}`, "error");
        }
    }

    /** 播放一次 seed 同步的动画；只有投掷者本人会在落定后上报骰面。 */
    function startAnimation(target: ActiveRoll, seed: number, rerollIndex: number): Promise<void> {
        if (target.animatedSeed === seed && target.animation) return target.animation;
        target.animatedSeed = seed;
        target.seed = seed;
        target.settledFaces = null;
        const run = runSeededAnimation(target, seed).then(async () => {
            if (target.rollerUserId !== String(window.currentUser?.user_id ?? "")) return;
            const faces = target.settledFaces;
            if (faces && faces.length) await submitFaces(target.requestId, faces);
            else await submitFaces(target.requestId, [], true);
        });
        target.animation = run;
        return run.finally(() => {
            if (target.animation === run) target.animation = null;
        });
    }

    async function runSeededAnimation(target: ActiveRoll, seed: number): Promise<void> {
        target.overlay.setRolling();
        const planOnly = planSnapshot(target.plan);
        const groups = diceSnapshotGroups(target.plan, planOnly);
        const unsupported = unsupportedReason(groups);
        const total = diceTotalCount(groups);
        const room = roomId();
        const level0 = effectiveLevel();
        let level: DiceAnimationLevel = level0;
        if (unsupported && level === "3d") {
            DiceDegradation.report(room, "3d", "simple", unsupported.split(":")[0] || "unsupported_dice", {
                scope: "roll",
                dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                requestId: target.requestId,
            });
            level = "simple";
        }
        if (level === "text") return;
        if (total > DICE_SIMPLE_RENDER_LIMIT) {
            DiceDegradation.report(room, level, "text", "dice_count_exceeded", {
                scope: "roll",
                error: `${total} > ${DICE_SIMPLE_RENDER_LIMIT}`,
                dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                requestId: target.requestId,
            });
            setRenderer(target, null);
            target.overlay.showStageNotice(
                `骰子数量过多（${total} 枚，渲染上限 ${DICE_SIMPLE_RENDER_LIMIT} 枚），无法在骰桌上渲染，仅显示下方结果。`,
            );
            return;
        }
        const faceUrls = faceUrlsFor(target.plan, groups);
        if (level === "3d") {
            if (!DiceDegradation.webglAvailable()) {
                DiceDegradation.report(room, "3d", "simple", "webgl_unavailable", {
                    scope: "session",
                    dice: { expression: target.plan.expression },
                    requestId: target.requestId,
                });
                level = "simple";
            } else {
                try {
                    const loaded = await Dice3DAdapter.loadBundle();
                    if (!loaded) throw new Error("dice3d_bundle_missing");
                    const adapter = ensure3DRenderer(target, faceUrls);
                    target.settledFaces = await adapter.play(planOnly, seed);
                    return;
                } catch (error) {
                    const message = error instanceof Error ? error.message : String(error);
                    const reason = message.includes("bundle_missing")
                        ? "bundle_load_failed"
                        : message.includes("webgl_unavailable") || message.includes("context")
                            ? "webgl_unavailable"
                            : "render_error";
                    const sessionFailure = reason === "bundle_load_failed" || reason === "webgl_unavailable";
                    DiceDegradation.report(room, "3d", "simple", reason, {
                        scope: sessionFailure ? "session" : "roll",
                        error: message,
                        dice: { expression: target.plan.expression },
                        requestId: target.requestId,
                    });
                    setRenderer(target, null);
                    level = "simple";
                }
            }
        }
        if (level === "simple") {
            try {
                const simple = ensureSimpleRenderer(target, faceUrls);
                target.settledFaces = await simple.play(planOnly, seed);
            } catch (error) {
                DiceDegradation.report(room, "simple", "text", "geometry_error", {
                    scope: "roll",
                    error: error instanceof Error ? error.message : String(error),
                    dice: { expression: target.plan.expression, groups: groups.map((group) => group.type) },
                    requestId: target.requestId,
                });
            }
        }
    }

    /** AI 自动投掷：目标玩家在线时直接开始，其他客户端等待 rolling 广播。 */
    function autoBegin(target: ActiveRoll): void {
        if (!target.physics || !target.auto) return;
        if (target.rollerUserId !== String(window.currentUser?.user_id ?? "")) return;
        if (target.seed !== null) return;
        if (effectiveLevel() === "text") {
            // 本机无法渲染：直接让服务端旧生成器兜底，避免 AI 一直等待。
            void submitFaces(target.requestId, [], true);
            return;
        }
        void beginRequest(target.requestId).then((started) => {
            if (started) void startAnimation(target, started.seed, started.rerollIndex);
        });
    }

    function bindOverlayActions(target: ActiveRoll): void {
        target.overlay.root.dataset.state = target.state;
    }

    function showRerollControls(target: ActiveRoll): void {
        // 重投/确定只能由触发这次投掷的玩家操作；其他房间成员只看到结果，
        // 不显示对他们无效的按钮。
        if (target.rollerUserId !== String(window.currentUser?.user_id ?? "")) {
            target.overlay.showRerollControls(null, () => {}, () => {});
            if (target.reroll?.enabled) target.overlay.setStatus("等待投掷者确认结果", "waiting");
            return;
        }
        target.overlay.showRerollControls(
            target.reroll,
            () => { void rerollRequest(target.requestId); },
            () => { void confirmRequest(target.requestId); },
        );
    }

    /** 结束本次投掷：保留聊天里的骰桌与骰子画面，只清空操作按钮。 */
    function finishActive(_collapse: boolean, snapshot?: DiceResultSnapshot): void {
        if (!active) return;
        const target = active;
        const result = snapshot || target.snapshot;
        if (result) target.overlay.setResult(result);
        target.overlay.freeze();
        active = null;
        pending = null;
        updateInputButton();
        pump();
    }

    function pump(): void {
        if (active || !queue.length) return;
        const payload = queue.shift()!;
        if (handledRequests.has(payload.request_id)) {
            pump();
            return;
        }
        const target = createActive(payload.request_id, payload.plan);
        active = target;
        applyRequestMeta(target, payload);
        target.reroll = payload.reroll ?? null;
        if (payload.state === "awaiting_confirm" && payload.result) {
            // 重连恢复：直接还原已投掷结果并显示重投/确定。
            target.snapshot = payload.result;
            target.overlay.setResult(payload.result);
            void playSnapshot(target, payload.result, false).then(() => {
                target.state = "awaiting_confirm";
                showRerollControls(target);
            });
            return;
        }
        target.overlay.setWaiting(payload.roller?.username || "玩家", isTarget(payload), () => {
            void rollRequest(payload.request_id);
        });
        void showIdleTable(target);
        armExpiry(target, payload);
        if (payload.seed !== null && payload.seed !== undefined) {
            // 重连时已在投掷中：用同一 seed 重播，保证画面与其他客户端一致。
            void startAnimation(target, Number(payload.seed), Number(payload.reroll_index) || 0);
        } else {
            autoBegin(target);
        }
        const backlog = rollBacklog.get(payload.request_id);
        if (backlog) {
            rollBacklog.delete(payload.request_id);
            void applyRolledPayload(target, backlog);
        }
    }

    /** 兜底：请求已过期却迟迟没有结果时冻结骰桌，避免留下空白面板。 */
    function armExpiry(target: ActiveRoll, payload: DiceRequestPayload): void {
        const expiresAt = Number(payload.expires_at || 0) * 1000;
        if (!expiresAt) return;
        const delay = Math.min(Math.max(expiresAt - Date.now() + 5000, 1000), 10 * 60 * 1000);
        window.setTimeout(() => {
            if (active !== target || target.snapshot !== null) return;
            target.overlay.setStatus("投掷请求已结束", "cancelled");
            finishActive(false);
        }, delay);
    }

    function applyRequestMeta(target: ActiveRoll, payload: DiceRequestPayload): void {
        target.state = String(payload.state || "pending");
        target.physics = Boolean(payload.physics ?? physicsEnabled());
        target.auto = Boolean(payload.auto);
        target.rollerUserId = String(payload.roller?.user_id ?? "");
        if (payload.reroll) target.reroll = payload.reroll;
    }

    function enqueue(payload: DiceRequestPayload): void {
        pending = payload;
        updateInputButton();
        if (effectiveLevel() === "text") {
            notifyText(payload);
            // 纯文本档位仍需响应 AI 自动投掷：让服务端旧生成器直接结算。
            if (payload.physics && payload.auto && isTarget(payload)) {
                void submitFaces(payload.request_id, [], true);
            }
            return;
        }
        if (active?.requestId === payload.request_id) {
            applyRequestMeta(active, payload);
            active.overlay.setWaiting(payload.roller?.username || "玩家", isTarget(payload), () => {
                void rollRequest(payload.request_id);
            });
            if (payload.seed !== null && payload.seed !== undefined) {
                void startAnimation(active, Number(payload.seed), Number(payload.reroll_index) || 0);
            } else {
                autoBegin(active);
            }
            return;
        }
        queue.push(payload);
        pump();
    }

    async function applyRolledPayload(target: ActiveRoll, payload: DiceRolledPayload): Promise<void> {
        const snapshot = snapshotFromPayload(payload);
        target.reroll = payload.reroll ?? target.reroll;
        target.state = String(payload.state || (target.reroll?.enabled ? "awaiting_confirm" : "rolled"));
        if (target.physics) {
            // 所有客户端已在 rolling 广播里同步播放过动画：这里只显示结算结果，
            // 绝不再旋转/抬起/重投骰子，避免落定后的点数发生变化。
            if (target.animation) await target.animation;
            const sharedSeed = rollingSeeds.get(target.requestId);
            if (target.animatedSeed === null || (sharedSeed !== undefined && target.animatedSeed !== sharedSeed)) {
                // 没赶上 rolling 广播（或重投的 rolling 丢失）：补播结果动画，
                // 有共享 seed 时轨迹与投掷者一致，而不是直接摆出静态画面。
                await playSnapshot(target, snapshot, true);
            } else {
                target.snapshot = snapshot;
                target.overlay.setResult(snapshot);
            }
        } else {
            await playSnapshot(target, snapshot, true);
        }
        if (payload.auto) target.overlay.setStatus("等待超时，系统已自动投掷", "auto");
        if (payload.state === "awaiting_confirm" || (target.reroll && target.reroll.enabled && payload.state !== "forced")) {
            showRerollControls(target);
            return;
        }
        await completeRequest(target.requestId);
        finishActive(true, snapshot);
    }

    function handleRequest(payload: DiceRequestPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        if (active && active.requestId !== payload.request_id && active.snapshot === null) {
            // 上一个请求在没有任何结果的情况下被服务端结束：冻结旧骰桌，
            // 避免聊天里残留一张永远等不到结果的空白骰桌。
            active.overlay.setStatus("该投掷已结束", "cancelled");
            finishActive(false);
        }
        handledRequests.delete(payload.request_id);
        rollBacklog.delete(payload.request_id);
        enqueue(payload);
    }

    async function handleRolled(payload: DiceRolledPayload): Promise<void> {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        if (requestId && handledRequests.has(requestId)) return;
        if (requestId) handledRequests.add(requestId);
        if (effectiveLevel() === "text") {
            const snapshot = snapshotFromPayload(payload);
            showNotification(snapshot.summary || `投掷结果：${snapshot.roll ?? snapshot.total ?? ""}`, "info");
            pending = null;
            updateInputButton();
            return;
        }
        if (active && active.requestId === requestId) {
            await applyRolledPayload(active, payload);
            return;
        }
        // 浮层尚未创建（例如刷新后收到广播）：先排队，等浮层启动时补播。
        if (requestId) rollBacklog.set(requestId, payload);
        const payloadRequest = payload.request as DiceRequestPayload | null;
        if (payloadRequest) {
            enqueue(payloadRequest);
        } else if (requestId) {
            const plan = planFromMeta(payload as unknown as DiceRollMeta);
            enqueue({
                room_id: roomId(),
                request_id: requestId,
                plan,
                roller: { user_id: "", username: rollerLabel(payload.roller) },
                reroll: payload.reroll ?? null,
                state: payload.state || "pending",
                result: payload.result ?? null,
            });
        } else {
            pump();
        }
    }

    async function handleRerolled(payload: DiceRolledPayload): Promise<void> {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        if (!active || active.requestId !== requestId) {
            await handleRolled(payload);
            return;
        }
        const snapshot = snapshotFromPayload(payload);
        active.reroll = payload.reroll ?? active.reroll;
        active.state = "awaiting_confirm";
        if (active.physics) {
            // 物理模式的重投在 rolling 广播里已经播放过，直接展示新结果。
            if (active.animation) await active.animation;
            const sharedSeed = rollingSeeds.get(active.requestId);
            if (active.animatedSeed === null || (sharedSeed !== undefined && active.animatedSeed !== sharedSeed)) {
                await playSnapshot(active, snapshot, true);
            } else {
                active.snapshot = snapshot;
                active.overlay.setResult(snapshot);
            }
        } else {
            await playSnapshot(active, snapshot, true);
        }
        showRerollControls(active);
    }

    /** 物理结果：所有客户端收到同一 seed 后播放完全一致的动画。 */
    function handleRolling(payload: DiceRequestPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        const seed = Number(payload.seed);
        if (!requestId || !Number.isFinite(seed)) return;
        // 记住共享种子：若实时结果消息比动画更晚渲染，补播动画仍能复现同一段轨迹。
        rollingSeeds.set(requestId, seed);
        let target = active && active.requestId === requestId ? active : null;
        if (!target) {
            const index = queue.findIndex((item) => item.request_id === requestId);
            const request = index >= 0 ? queue.splice(index, 1)[0] : null;
            const plan = request?.plan || payload.plan;
            if (!plan) return;
            if (active && active.snapshot === null && active.requestId !== requestId) {
                active.overlay.setStatus("该投掷已结束", "cancelled");
                finishActive(false);
            }
            target = createActive(requestId, plan);
            active = target;
            applyRequestMeta(target, request || payload);
        }
        target.physics = true;
        target.auto = Boolean(payload.auto);
        target.rollerUserId = String(payload.roller?.user_id ?? target.rollerUserId);
        void startAnimation(target, seed, Number(payload.reroll_index) || 0);
    }

    function handleConfirmed(payload: DiceRolledPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        if (requestId) handledRequests.add(requestId);
        if (active && active.requestId === requestId) {
            if (payload.result) {
                active.snapshot = payload.result;
                active.overlay.setResult(payload.result);
            }
            finishActive(true, payload.result || active.snapshot || undefined);
            return;
        }
        // 没有活动骰桌（例如中途重连）：只在缺失时补一张静态骰桌，避免重复结果。
        if (payload.result && !findPanel(requestId)) {
            staticPanel(planFromSnapshot(payload.result), payload.result, requestId || `confirmed-${Date.now()}`);
        }
    }

    function handleCancelled(payload: DiceRolledPayload): void {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        const reason = payload.reason === "cancelled" ? `${payload.operator || "管理员"} 取消了投掷` : "投掷已取消";
        if (active && active.requestId === requestId) {
            active.overlay.setStatus(reason, "cancelled");
            window.setTimeout(() => finishActive(false), 900);
        }
        showNotification(reason, "warning");
        if (pending?.request_id === requestId) pending = null;
        updateInputButton();
    }

    async function handleForced(payload: DiceRolledPayload): Promise<void> {
        if (String(payload.room_id || "") !== roomId()) return;
        const requestId = String(payload.request_id || "");
        if (requestId) handledRequests.add(requestId);
        if (effectiveLevel() === "text") {
            const snapshot = snapshotFromPayload(payload);
            showNotification(snapshot.summary || "管理员已强制投掷", "info");
            return;
        }
        let target = active && active.requestId === requestId ? active : null;
        if (!target) {
            const plan = payload.request?.plan || planFromMeta(payload as unknown as DiceRollMeta);
            target = createActive(requestId || `forced-${Date.now()}`, plan);
            active = target;
        }
        target.overlay.setStatus(String(payload.operator || "管理员") + " 强制投掷", "forced");
        // 强制投掷使用服务端旧生成器：静态摆出结果，避免物理骰面与服务端点数不一致。
        await playSnapshot(target, snapshotFromPayload(payload), false);
        await completeRequest(requestId);
        finishActive(true, target.snapshot || undefined);
    }

    function renderForMessage(messageElement: HTMLElement, message: ChatMessage, animate: boolean): void {
        const metadata = (message.metadata || {}) as Record<string, unknown>;
        const meta = metadata.dice_roll as DiceRollMeta | undefined;
        if (!meta || !meta.show_table) return;
        if (effectiveLevel() === "text") return;
        // 旧格式消息没有骰组信息，保持纯文本；新格式才还原骰桌。
        if (!Array.isArray(meta.groups) || !meta.groups.length) return;
        const requestId = String(meta.request_id || "");
        if (findPanel(requestId)) {
            // 实时投掷已经由内嵌骰桌承载，隐藏重复的持久化消息。
            messageElement.classList.add("dice-message-consumed");
            if (requestId === pending?.request_id) {
                pending = null;
                updateInputButton();
            }
            return;
        }
        if (requestId && active?.requestId === requestId) {
            void playSnapshot(active, snapshotFromMeta(meta), animate);
            messageElement.classList.add("dice-message-consumed");
            return;
        }
        if (animate && !active) {
            // 实时消息：本机可能没赶上 rolling 广播（中途加入、网络抖动、事件丢失）。
            // 这里补建骰桌并播放动画，保证房间内所有玩家看到与投掷者一致的过程；历史消息仍走静态还原。
            const tableId = requestId || `message-${message.id}`;
            const target = createActive(tableId, planFromMeta(meta));
            active = target;
            target.state = "rolled";
            target.physics = true;
            target.auto = Boolean(meta.auto);
            target.rollerUserId = String(
                meta.roller_user_id
                    ?? (typeof meta.roller === "object" && meta.roller ? meta.roller.user_id : "")
                    ?? "",
            );
            const seed = requestId ? rollingSeeds.get(requestId) : undefined;
            if (seed !== undefined) target.seed = seed;
            // 结果事件可能比这条消息更晚到达：标记已处理，避免再创建第二张骰桌。
            if (requestId) {
                handledRequests.add(requestId);
                rollBacklog.delete(requestId);
            }
            void playSnapshot(target, snapshotFromMeta(meta), true).then(() => {
                if (active !== target) return;
                finishActive(true, target.snapshot || undefined);
            });
            messageElement.classList.add("dice-message-consumed");
            if (requestId === pending?.request_id) {
                pending = null;
                updateInputButton();
            }
            return;
        }
        // 历史/重连消息：在消息位置插入静态骰桌，数字结果位于骰桌下方，不重播动画。
        staticPanel(planFromMeta(meta), snapshotFromMeta(meta), requestId || `message-${message.id}`, messageElement);
        messageElement.classList.add("dice-message-consumed");
        if (requestId === pending?.request_id) {
            pending = null;
            updateInputButton();
        }
    }

    async function requestRoll(requestId: string): Promise<DiceResultSnapshot | null> {
        const response = await TrpgApi.post<ApiResponse<DiceResultSnapshot & { request_id: string; state?: string; reroll?: DiceRerollState; remaining?: number }>>(
            `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/roll`,
            {},
        );
        if (!response?.success || !response.data) {
            showNotification(response?.message || response?.error || "投掷失败", "error");
            return null;
        }
        const data = response.data;
        return {
            ...data,
            reroll: data.reroll ?? { enabled: true, mode: "limited", limit: 0, used: 0, remaining: data.remaining ?? 0 },
        } as DiceResultSnapshot;
    }

    async function rollRequest(requestId: string): Promise<void> {
        if (rolling) return;
        if (handledRequests.has(requestId)) {
            await completeRequest(requestId);
            return;
        }
        rolling = true;
        try {
            const current = active && active.requestId === requestId ? active : null;
            const usePhysics = current ? current.physics : Boolean(pending?.physics ?? physicsEnabled());
            if (usePhysics) {
                let target = current;
                if (!target && pending?.plan) {
                    target = createActive(requestId, pending.plan);
                    active = target;
                    applyRequestMeta(target, pending);
                }
                if (!target) {
                    showNotification("投掷信息缺失，无法开始动画", "error");
                    return;
                }
                target.physics = true;
                if (effectiveLevel() === "text") {
                    await submitFaces(requestId, [], true);
                    return;
                }
                const started = await beginRequest(requestId);
                if (started) await startAnimation(target, started.seed, started.rerollIndex);
                return;
            }
            const snapshot = await requestRoll(requestId);
            if (!snapshot) return;
            handledRequests.add(requestId);
            if (effectiveLevel() === "text") {
                await completeRequest(requestId);
                showNotification(snapshot.summary || `投掷结果：${snapshot.roll ?? snapshot.total ?? ""}`, "info");
                pending = null;
                updateInputButton();
                return;
            }
            if (!active || active.requestId !== requestId) {
                // 没有浮层时补建，保证玩家点击按钮后也能看到动画。
                const plan = snapshot.groups ? planFromMeta(snapshot as unknown as DiceRollMeta) : (pending?.plan || planFromMeta(snapshot as unknown as DiceRollMeta));
                active = createActive(requestId, plan);
            }
            active.reroll = snapshot.reroll ?? null;
            const rerollEnabled = Boolean(snapshot.reroll?.enabled);
            active.state = rerollEnabled ? "awaiting_confirm" : "rolled";
            await playSnapshot(active, snapshot, true);
            if (rerollEnabled) {
                showRerollControls(active);
            } else {
                await completeRequest(requestId);
                finishActive(true, snapshot);
            }
        } catch (error) {
            showNotification(`投掷失败：${error instanceof Error ? error.message : String(error)}`, "error");
        } finally {
            rolling = false;
        }
    }

    async function rerollRequest(requestId: string): Promise<void> {
        if (rolling) return;
        rolling = true;
        try {
            if (active && active.requestId === requestId && active.physics) {
                // 物理模式重投：重新开始一次动画，落定后由投掷者上报新骰面。
                const started = await beginRequest(requestId, true);
                if (!started) {
                    showRerollControls(active);
                    return;
                }
                await startAnimation(active, started.seed, started.rerollIndex);
                return;
            }
            const response = await TrpgApi.post<ApiResponse<DiceResultSnapshot & { request_id: string; state?: string; reroll?: DiceRerollState; remaining?: number }>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/reroll`,
                {},
            );
            if (!response?.success || !response.data) {
                showNotification(response?.message || response?.error || "重投失败", "error");
                if (active) showRerollControls(active);
                return;
            }
            const snapshot = response.data as DiceResultSnapshot;
            if (!active || active.requestId !== requestId) return;
            active.reroll = snapshot.reroll ?? active.reroll;
            await playSnapshot(active, snapshot, true);
            showRerollControls(active);
        } catch (error) {
            showNotification(`重投失败：${error instanceof Error ? error.message : String(error)}`, "error");
        } finally {
            rolling = false;
        }
    }

    async function confirmRequest(requestId: string): Promise<void> {
        try {
            const response = await TrpgApi.post<ApiResponse<DiceResultSnapshot & { request_id: string }>>(
                `/api/rooms/${encodeURIComponent(roomId())}/dice-requests/${encodeURIComponent(requestId)}/confirm`,
                {},
            );
            if (!response?.success || !response.data) {
                showNotification(response?.message || response?.error || "确认失败", "error");
                return;
            }
            if (active && active.requestId === requestId) {
                finishActive(true, response.data as DiceResultSnapshot);
            }
        } catch (error) {
            showNotification(`确认失败：${error instanceof Error ? error.message : String(error)}`, "error");
        }
    }

    async function completeRequest(requestId: string): Promise<void> {
        if (!requestId) return;
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
        if (found && found.state !== "finished") enqueue(found);
    }

    async function handleRollCommand(mode: "roll" | "force" | "confirm"): Promise<boolean> {
        let request = pending;
        if (!request) {
            request = await fetchPending();
            if (request) pending = request;
        }
        if (!request) {
            showNotification("当前没有等待中的投掷", "info");
            return true;
        }
        if (mode === "force") {
            if (!canManage()) {
                showNotification("只有管理员可以强制投掷", "warning");
                return true;
            }
            return forceRequest(request.request_id);
        }
        if (mode === "confirm") {
            if (!isTarget(request) && !canManage()) {
                showNotification(`当前等待 ${request.roller?.username || "其他玩家"} 确认`, "warning");
                return true;
            }
            await confirmRequest(request.request_id);
            return true;
        }
        if (!isTarget(request)) {
            showNotification(`当前等待 ${request.roller?.username || "其他玩家"} 投掷`, "warning");
            return true;
        }
        await rollRequest(request.request_id);
        return true;
    }

    /** 本地投掷种子：仅用于让骰桌动画可复现，不参与最终点数计算。 */
    function localSeed(): number {
        try {
            const buffer = new Uint32Array(1);
            window.crypto?.getRandomValues(buffer);
            const value = buffer[0];
            if (value) return value;
        } catch (error) {
            // 忽略，退回 Math.random。
        }
        return (Math.random() * 0x100000000) >>> 0;
    }

    /** /dice <expr> --table：本地投掷并召唤骰桌，位置不限。 */
    async function handleDiceTableCommand(expression: string): Promise<boolean> {
        if (effectiveLevel() === "text") {
            showNotification("纯文本档位下不显示骰桌；已忽略 --table 参数。", "info");
            return false;
        }
        let plan: DicePlan;
        try {
            plan = new DiceTool().localPlan(expression);
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
            return false;
        }
        const requestId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
        const target = createActive(requestId, plan);
        active = target;
        let local: { plan: DicePlan; snapshot: DiceResultSnapshot };
        if (physicsEnabled()) {
            // 与 AI 投掷一致：先播放动画，落定后读取朝上的面作为结果，不再用预设随机数。
            active.state = "rolling";
            await runSeededAnimation(target, localSeed());
            try {
                local = new DiceTool().rollLocal(expression, target.settledFaces);
            } catch (error) {
                showNotification(error instanceof Error ? error.message : String(error), "error");
                setRenderer(target, null);
                active = null;
                target.overlay.freeze();
                updateInputButton();
                pump();
                return false;
            }
            // 动画已在落定位置，不能再重排骰子；只把结算结果写到骰桌下方。
            finishActive(true, local.snapshot);
        } else {
            // 关闭现代化生成器：沿用旧随机数生成器，动画只负责表现。
            try {
                local = new DiceTool().rollLocal(expression);
            } catch (error) {
                showNotification(error instanceof Error ? error.message : String(error), "error");
                setRenderer(target, null);
                active = null;
                target.overlay.freeze();
                updateInputButton();
                pump();
                return false;
            }
            active.state = "rolled";
            await playSnapshot(target, local.snapshot, true);
            finishActive(true, local.snapshot);
        }
        // 本地骰桌同样写入房间历史，刷新或重连时才能按 metadata 静态还原。
        const roller = {
            user_id: window.currentUser?.user_id ?? null,
            username: window.currentUser?.username || "",
        };
        try {
            await window.persistLocalDiceMessage?.(`投掷 ${local.plan.expression} = ${local.snapshot.total}`, {
                dice_roll: {
                    show_table: true,
                    request_id: requestId,
                    mode: local.snapshot.mode,
                    expression: local.snapshot.expression,
                    count: local.snapshot.count,
                    sides: local.snapshot.sides,
                    faces: local.snapshot.faces,
                    labels: local.snapshot.labels,
                    groups: local.snapshot.groups,
                    selected: local.snapshot.selected,
                    roll: local.snapshot.roll,
                    rolls: local.snapshot.rolls,
                    total: local.snapshot.total,
                    candidates: local.snapshot.candidates,
                    check_name: null,
                    difficulty: null,
                    target: null,
                    success: null,
                    success_level: null,
                    roller,
                },
            });
        } catch (error) {
            console.warn("保存本地骰子记录失败", error);
        }
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
        if (active) {
            active.renderer?.dispose();
            active.overlay.close();
            active = null;
        }
        // 离开房间时释放所有存活的 3D 实例，避免 WebGL context 跨房间累积。
        live3D.forEach((_target, adapter) => adapter.dispose());
        live3D.clear();
        rollingSeeds.clear();
        queue.length = 0;
        rollBacklog.clear();
        handledRequests.clear();
        pending = null;
        DiceDegradation.resetSession();
        updateInputButton();
        void diceTextureLibrary().then(() => undefined);
        if (id) void restorePending();
    }

    function init(): void {
        lastRoomId = roomId();
        bindInput();
        diceWatchTheme();
        void restorePending();
    }

    return {
        init,
        onRoomChanged,
        renderForMessage,
        handleRequest,
        handleRolling,
        handleRolled,
        handleRerolled,
        handleConfirmed,
        handleCancelled,
        handleForced,
        handleRollCommand,
        handleDiceTableCommand,
        restorePending,
        rollRequest,
        rerollRequest,
        confirmRequest,
        forceRequest,
        effectiveLevel,
        baseLevel,
    };
})();

window.TrpgDiceTable = DiceTable;
