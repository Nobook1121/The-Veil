/** 世界意志直执行：权限判定、入口按钮与一句话弹窗。
 *
 * 是否可用由后端房规决定（管理员始终可用；房主/房管默认可用；白名单成员额外可用）。
 * 前端只负责隐藏入口，真正执行时后端会再次校验。
 */
(function () {
    const TAU = Math.PI * 2;
    /** 世界意志专属的“法阵层”类名：固定在遮罩之上、弹窗之下。 */
    const VEIL_CLASS = "world-override-veil";
    let currentRoomId = "";
    let status: WorldOverrideStatus | null = null;
    let busy = false;
    let initialized = false;
    let veil: HTMLDivElement | null = null;

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function notify(message: string, type: "success" | "error" | "info" | "warning" = "info"): void {
        window.showNotification?.(message, type);
    }

    function setMessage(text: string, isError = false): void {
        const element = byId("worldOverrideMessage");
        if (!element) return;
        element.textContent = text;
        element.classList.toggle("is-error", isError);
    }

    const RUNE_TEXT = "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟ";

    /** 每层法阵使用独立的渐变 id：跨 SVG 的 url(#id) 不会互相覆盖。 */
    function ringGradient(id: string): string {
        return `
            <defs>
                <linearGradient id="${id}" x1="118" y1="74" x2="884" y2="926" gradientUnits="userSpaceOnUse">
                    <stop offset="0" stop-color="#f2dfa8" />
                    <stop offset=".36" stop-color="#c7a25c" />
                    <stop offset=".68" stop-color="#8fe6d6" />
                    <stop offset="1" stop-color="#e0c082" />
                </linearGradient>
            </defs>`;
    }

    /** 方形环上的四枚菱形刻记，给法阵一点“刻度”的方向感。 */
    function cardinalMarks(radius: number, gradientId: string): string {
        return [0, 90, 180, 270].map((degree) => {
            const angle = (Math.PI * degree) / 180;
            const x = 500 + Math.cos(angle) * radius;
            const y = 500 + Math.sin(angle) * radius;
            return `<path d="M ${x.toFixed(1)} ${(y - 7).toFixed(1)} L ${(x + 7).toFixed(1)} ${y.toFixed(1)} L ${x.toFixed(1)} ${(y + 7).toFixed(1)} L ${(x - 7).toFixed(1)} ${y.toFixed(1)} Z" stroke="none" fill="url(#${gradientId})" opacity=".68" />`;
        }).join("");
    }

    /** 外环上依次亮起的刻点，像法阵在缓慢地“呼吸”。 */
    function pulseNodes(radius: number, count: number, gradientId: string): string {
        return Array.from({ length: count }, (_, index) => {
            const angle = (TAU * index) / count - Math.PI / 2;
            const x = (500 + Math.cos(angle) * radius).toFixed(1);
            const y = (500 + Math.sin(angle) * radius).toFixed(1);
            const delay = ((index / count) * 3.6).toFixed(2);
            return `<circle class="world-override-node" cx="${x}" cy="${y}" r="2.2" stroke="none" fill="url(#${gradientId})" style="animation-delay:${delay}s" />`;
        }).join("");
    }

    /**
     * 沿圆环排布的符文；textLength 让符文均匀铺满整圈，不受字数影响。
     * idPrefix 用来区分同半径的多份符文环（实线层与柔光层各用各的路径 id）。
     */
    function runeRing(
        radius: number,
        fontSize: number,
        opacity: number,
        gradientId: string,
        idPrefix: string,
        darkOutline = true,
    ): string {
        const pathId = `${idPrefix}-rune-${radius}`;
        const circumference = Math.round(TAU * radius);
        // 极细的深色描边垫在字形下面：环带自发光再亮，符文边缘也不会糊进光里。
        const outline = darkOutline
            ? 'stroke="#05070b" stroke-width=".9" stroke-opacity=".55" paint-order="stroke fill"'
            : 'stroke="none"';
        return `
            <defs>
                <path id="${pathId}" d="M 500 ${(500 - radius).toFixed(1)} A ${radius} ${radius} 0 1 1 499.9 ${(500 - radius).toFixed(1)}" />
            </defs>
            <circle cx="500" cy="500" r="${(radius - fontSize * 0.66).toFixed(1)}" stroke-width=".8" opacity=".26" />
            <text font-size="${fontSize}" letter-spacing=".24em" fill="url(#${gradientId})" ${outline} opacity="${opacity}"
                style="font-family:'Cinzel','Noto Serif SC',serif">
                <textPath href="#${pathId}" startOffset="0" textLength="${circumference}" lengthAdjust="spacing">${RUNE_TEXT.repeat(3)}</textPath>
            </text>`;
    }

    /** {7/3} 星形：每次前进三个顶点，七个顶点连成一枚七芒星。 */
    function heptagram(radius: number, opacity: number): string {
        const points: string[] = [];
        let index = 0;
        for (let step = 0; step < 7; step += 1) {
            const angle = -Math.PI / 2 + (TAU * index) / 7;
            points.push(`${step === 0 ? "M" : "L"}${(500 + Math.cos(angle) * radius).toFixed(1)} ${(500 + Math.sin(angle) * radius).toFixed(1)}`);
            index = (index + 3) % 7;
        }
        points.push("Z");
        return `<path d="${points.join(" ")}" stroke-width="1.2" opacity="${opacity}" />`;
    }

    /**
     * 三层同心法阵，分别以不同速度、不同方向旋转：
     * 外层符文环最慢，中层星盘反向，内层细环稍快。视觉上就会「套」在
     * 世界意志窗口外面，而不是压在屏幕底部。
     */
    function buildLayer(kind: "outer" | "mid" | "inner"): string {
        const gradientId = `wo-gradient-${kind}`;
        const defs = ringGradient(gradientId);
        if (kind === "outer") {
            return `
                <svg viewBox="0 0 1000 1000" fill="none" stroke="url(#${gradientId})" aria-hidden="true">
                    ${defs}
                    <circle cx="500" cy="500" r="486" stroke-width="1.2" opacity=".62" />
                    <circle cx="500" cy="500" r="472" stroke-width=".9" stroke-dasharray="1 9" opacity=".4" />
                    ${runeRing(456, 22, 0.8, gradientId, "wo-outer")}
                    <circle cx="500" cy="500" r="436" stroke-width="8" stroke-dasharray="2 12" opacity=".3" />
                    ${cardinalMarks(486, gradientId)}
                    ${pulseNodes(486, 14, gradientId)}
                </svg>`;
        }
        if (kind === "mid") {
            return `
                <svg viewBox="0 0 1000 1000" fill="none" stroke="url(#${gradientId})" aria-hidden="true">
                    ${defs}
                    <circle cx="500" cy="500" r="416" stroke-width="1.35" opacity=".62" />
                    <circle cx="500" cy="500" r="404" stroke-width=".9" stroke-dasharray="3 10" opacity=".36" />
                    <circle cx="500" cy="500" r="386" stroke-width="1.1" stroke-dasharray="18 12" opacity=".36" />
                    ${heptagram(366, 0.62)}
                    ${cardinalMarks(416, gradientId)}
                </svg>`;
        }
        return `
            <svg viewBox="0 0 1000 1000" fill="none" stroke="url(#${gradientId})" aria-hidden="true">
                ${defs}
                <circle cx="500" cy="500" r="352" stroke-width="1.1" opacity=".6" />
                ${runeRing(336, 16, 0.66, gradientId, "wo-inner")}
                <circle cx="500" cy="500" r="312" stroke-width=".8" stroke-dasharray="1 8" opacity=".28" />
                ${cardinalMarks(352, gradientId)}
            </svg>`;
    }

    /**
     * 静态柔光层：与三层法阵同形，但只模糊、不旋转，栅格化一次即可复用。
     * 里面同样排一圈符文——模糊后不再可读，只作为「环带自发光」的底光。
     */
    function buildHaloLayer(kind: "halo" | "aura"): string {
        const gradientId = `wo-gradient-${kind}`;
        return `
            <svg viewBox="0 0 1000 1000" fill="none" stroke="url(#${gradientId})" aria-hidden="true">
                ${ringGradient(gradientId)}
                <circle cx="500" cy="500" r="486" stroke-width="2.2" opacity=".6" />
                <circle cx="500" cy="500" r="456" stroke-width="2" opacity=".5" />
                ${runeRing(456, 22, 0.9, gradientId, `wo-${kind}`, false)}
                <circle cx="500" cy="500" r="416" stroke-width="2.4" opacity=".52" />
                <circle cx="500" cy="500" r="386" stroke-width="2" opacity=".4" />
                <circle cx="500" cy="500" r="352" stroke-width="2" opacity=".46" />
                ${runeRing(336, 16, 0.7, gradientId, `wo-${kind}`, false)}
                <circle cx="500" cy="500" r="312" stroke-width="1.6" opacity=".32" />
                ${heptagram(366, 0.5)}
            </svg>`;
    }

    /** 创建法阵层；重复打开时直接复用，避免堆叠多个 SVG。 */
    function ensureVeil(): HTMLDivElement {
        if (veil !== null) {
            return veil;
        }
        const element = document.createElement("div");
        element.className = VEIL_CLASS;
        element.setAttribute("aria-hidden", "true");
        element.innerHTML = `
            <div class="world-override-veil-glow"></div>
            <div class="world-override-veil-core"></div>
            <div class="world-override-sigil" aria-hidden="true">
                <div class="world-override-sigil-aura">${buildHaloLayer("aura")}</div>
                <div class="world-override-sigil-halo">${buildHaloLayer("halo")}</div>
                <div class="world-override-sigil-layer world-override-sigil-layer--outer">${buildLayer("outer")}</div>
                <div class="world-override-sigil-layer world-override-sigil-layer--mid">${buildLayer("mid")}</div>
                <div class="world-override-sigil-layer world-override-sigil-layer--inner">${buildLayer("inner")}</div>
            </div>`;
        document.body.appendChild(element);
        veil = element;
        return element;
    }

    /** Bootstrap 的遮罩是运行时插到 body 上的，只能等它出现后再打标记。 */
    function markBackdrop(active: boolean): void {
        document.querySelectorAll<HTMLElement>(".modal-backdrop").forEach((element) => {
            element.classList.toggle("world-override-backdrop", active);
        });
    }

    /** 法阵只属于「深渊帷幕」：其它主题打开世界意志时保持干净弹窗，不生成任何背景特效。 */
    function veilEnabled(): boolean {
        return document.body.classList.contains("theme-coc");
    }

    function showVeil(): void {
        // 遮罩样式各主题共用（深渊/赛博各自有配色），标记始终打上。
        // 遮罩在 show.bs.modal 之后才插入 body，用微任务等它出现，避免这一帧漏标。
        queueMicrotask(() => markBackdrop(true));
        if (!veilEnabled()) {
            return;
        }
        const element = ensureVeil();
        // 先强制一次布局，让浏览器记住 opacity:0 的起点；同一帧内再加可见类，
        // 法阵就会和遮罩、弹窗一起淡入，而不是慢一帧才开始。
        void element.offsetWidth;
        element.classList.add("is-visible");
    }

    function hideVeil(): void {
        const element = veil;
        veil = null;
        if (element === null) {
            return;
        }
        // 与弹窗收起同步淡出，避免法阵在弹窗消失后继续停留。
        element.classList.remove("is-visible");
        window.setTimeout(() => element.remove(), 360);
    }

    function applyVisibility(): void {
        const button = byId<HTMLButtonElement>("worldOverrideButton");
        if (!button) return;
        const room = window.currentRoom;
        const allowed = status?.allowed === true
            && Boolean(room?.id)
            && room?.invisible_view !== true
            && room?.archived !== true;
        button.hidden = !allowed;
    }

    async function refresh(room?: Room | null): Promise<void> {
        const target = room !== undefined ? room : window.currentRoom;
        const roomId = String(target?.id || "");
        currentRoomId = roomId;
        status = null;
        if (!roomId || target?.invisible_view === true || target?.archived === true) {
            applyVisibility();
            return;
        }
        try {
            const response = await TrpgApi.get<ApiResponse<WorldOverrideStatus>>(
                `/api/rooms/${encodeURIComponent(roomId)}/world-override`,
            );
            if (roomId !== currentRoomId) return;
            if (response.success && response.data) status = response.data;
        } catch {
            status = null;
        } finally {
            applyVisibility();
        }
    }

    async function open(): Promise<void> {
        const room = window.currentRoom;
        if (!room?.id) {
            notify("请先加入房间后再使用世界意志", "error");
            return;
        }
        if (room.invisible_view || room.archived) {
            notify("当前房间无法使用世界意志", "error");
            return;
        }
        await refresh(room);
        if (status?.allowed !== true) {
            notify("你没有使用世界意志的权限", "error");
            return;
        }
        if (status.rate && status.rate.allowed === false) {
            notify(`世界意志冷却中：${status.rate.reason}，请 ${status.rate.retry_after} 秒后再试`, "info");
            return;
        }
        const modal = byId("worldOverrideModal");
        if (!modal) return;
        setMessage("");
        const limits = byId("worldOverrideLimits");
        if (limits && status.rate) {
            const cooldown = status.rate.cooldown_seconds > 0 ? `同一玩家 ${status.rate.cooldown_seconds} 秒一次` : "同一玩家无冷却";
            const quota = status.rate.hourly_limit > 0
                ? `本房间每小时剩余 ${status.rate.hourly_remaining}/${status.rate.hourly_limit} 次`
                : "本房间每小时不限次数";
            limits.textContent = `${cooldown} · ${quota} · 使用记录会写入房间日志`;
        }
        const input = byId<HTMLTextAreaElement>("worldOverrideInput");
        if (input) input.value = "";
        new bootstrap.Modal(modal).show();
        window.setTimeout(() => input?.focus(), 120);
    }

    async function execute(): Promise<void> {
        if (busy) return;
        const input = byId<HTMLTextAreaElement>("worldOverrideInput");
        const text = input?.value.trim() || "";
        if (!text) {
            setMessage("请先用一句话描述要直接达成的结果。", true);
            return;
        }
        if (typeof window.sendWorldOverride !== "function") {
            setMessage("聊天模块尚未就绪，请稍后重试。", true);
            return;
        }
        busy = true;
        const confirmButton = byId<HTMLButtonElement>("worldOverrideConfirm");
        if (confirmButton) confirmButton.disabled = true;
        try {
            const modal = byId("worldOverrideModal");
            bootstrap.Modal.getInstance(modal)?.hide();
            if (input) input.value = "";
            setMessage("");
            await window.sendWorldOverride(text);
        } catch (error) {
            setMessage(`执行失败：${error instanceof Error ? error.message : String(error)}`, true);
        } finally {
            busy = false;
            if (confirmButton) confirmButton.disabled = false;
        }
    }

    function init(): void {
        if (initialized) return;
        if (!byId("worldOverrideButton")) return;
        initialized = true;
        byId("worldOverrideButton")?.addEventListener("click", () => void open());
        byId("worldOverrideConfirm")?.addEventListener("click", () => void execute());
        byId("worldOverrideInput")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void execute();
            }
        });
        // 在 show 事件里同步挂法阵：它与遮罩、弹窗处于同一帧。
        // 层级靠 z-index 保证（遮罩 1040 < 法阵 1049 < 弹窗 1050），不依赖插入顺序。
        const modal = byId("worldOverrideModal");
        modal?.addEventListener("show.bs.modal", showVeil);
        // 收起一开始就淡出法阵，和弹窗、遮罩使用同一段时间轴。
        modal?.addEventListener("hide.bs.modal", hideVeil);
        // 遮罩被移除后再撤销标记，避免淡出阶段闪回普通遮罩配色。
        modal?.addEventListener("hidden.bs.modal", () => markBackdrop(false));
        void refresh();
    }

    window.TrpgWorldOverride = { init, refresh: (room?: Room | null) => void refresh(room), open: () => void open() };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
