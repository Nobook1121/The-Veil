/**
 * 统一配乐播放器（右侧栏沉浸区）。
 *
 * 设计要点：
 * - 只做「播放器状态机 + 界面」，不生成任何音符：程序化配乐仍归 music-engine，
 *   BGM 仍归 bgm.ts，两边都通过窄接口暴露播放/暂停/切歌/进度。
 * - 播放器状态归本地（每个玩家自己的暂停/进度/切歌），服务端的共享小节位置只在
 *   「正常播放」时用于对齐；用户手动操作不会被立即回拉，下一次叙事配乐事件才重新对齐。
 * - 音源交接规则（见 resolveSnapshot）：
 *   realtime → 只有程序化配乐；bgm → 只有 BGM；hybrid → BGM 当底、程序化叠在上面。
 *   切换档位由沉浸模块统一 setSuppressed/setEngineMode，播放器只负责显示实际在响的那一轨。
 * - 程序化配乐没有音轨：不提供拖动进度，只提供暂停与「下一段」。
 */
namespace TrpgImmersivePlayerModule {
    type MusicSource = "realtime" | "bgm" | "hybrid";
    type Primary = "procedural" | "bgm" | "none";

    interface Snapshot {
        visible: boolean;
        source: MusicSource;
        primary: Primary;
        badge: string;
        title: string;
        subtitle: string;
        playing: boolean;
        draggable: boolean;
        position: number;
        duration: number;
        canSeek: boolean;
    }

    const PROFILE_LABEL: Record<string, string> = {
        daily: "日常",
        suspense: "悬疑",
        unease: "不安",
        horror: "恐怖",
        urgency: "紧迫",
        sorrow: "悲伤",
        awe: "壮丽",
        aftermath: "余韵",
    };

    let element: HTMLElement | null = null;
    let mountedHostId = "";
    let mountRetry = 0;
    let dragging = false;
    let lastRoomId = "";
    let refreshTimer: number | null = null;
    let bound = false;

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function currentRoomId(): string {
        const room = window.currentRoom;
        return room?.id ? String(room.id) : "";
    }

    function musicSource(): MusicSource {
        const source = window.TrpgImmersive?.getMusicSource?.();
        return source === "realtime" || source === "bgm" ? source : "hybrid";
    }

    function bgmState(): BgmPlaybackState | null {
        try {
            return window.TrpgBgm?.getPlaybackState?.() ?? null;
        } catch {
            return null;
        }
    }

    function plan(): ImmersiveMusicPlan | null {
        try {
            return window.TrpgImmersiveMusic?.currentPlan?.() ?? null;
        } catch {
            return null;
        }
    }

    function proceduralPaused(): boolean {
        try {
            return Boolean(window.TrpgImmersiveMusic?.isPaused?.());
        } catch {
            return false;
        }
    }

    function proceduralMuted(): boolean {
        try {
            return Boolean(window.TrpgImmersiveMusic?.isMuted?.());
        } catch {
            return false;
        }
    }

    function formatTime(seconds: number): string {
        if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
        const total = Math.floor(seconds);
        const minutes = Math.floor(total / 60);
        const rest = total % 60;
        return `${minutes}:${String(rest).padStart(2, "0")}`;
    }

    /** 决定「此刻实际在响的是哪一轨」，并给出播放器要显示的内容。 */
    function resolveSnapshot(): Snapshot {
        const source = musicSource();
        const bgm = bgmState();
        const currentPlan = plan();
        const roomId = currentRoomId();
        const bgmUsable = Boolean(bgm?.active);
        const procUsable = Boolean(currentPlan);

        let primary: Primary = "none";
        // 「仅 BGM」档下程序化引擎是关的：没有 BGM 就不该假装在放实时配乐。
        if (source === "bgm") primary = bgmUsable ? "bgm" : "none";
        else if (source === "realtime") primary = procUsable ? "procedural" : "none";
        else primary = bgmUsable ? "bgm" : procUsable ? "procedural" : "none";

        const visible = Boolean(roomId) && primary !== "none";
        if (primary === "bgm" && bgm) {
            const hybrid = source === "hybrid";
            return {
                visible,
                source,
                primary,
                badge: hybrid ? "混合 · 背景音乐" : "背景音乐",
                title: bgm.title || "未命名曲目",
                subtitle: hybrid
                    ? [
                        procUsable ? "实时配乐叠加" : "实时配乐未在播放",
                        `${bgm.index + 1}/${bgm.count}`,
                        modeLabel(bgm.mode),
                    ].join(" · ")
                    : `${bgm.index + 1}/${bgm.count} · ${modeLabel(bgm.mode)}${bgm.source === "personal" ? " · 个人" : " · 房间"}`,
                playing: bgm.playing,
                draggable: true,
                position: bgm.current,
                duration: bgm.duration,
                canSeek: bgm.duration > 0,
            };
        }
        if (primary === "procedural") {
            const timing = window.TrpgImmersiveMusic?.barTiming?.() ?? null;
            const bar = timing ? Math.max(1, Math.round(timing.positionBars) + 1) : 0;
            const profile = PROFILE_LABEL[String(currentPlan?.profile || "")] || "实时配乐";
            return {
                visible,
                source,
                primary,
                badge: source === "hybrid" ? "混合 · 实时配乐" : "实时配乐",
                title: `${profile}配乐`,
                subtitle: `Web Audio 实时生成${bar > 0 ? ` · 第 ${bar} 小节` : ""} · 无法拖动进度`,
                playing: !proceduralPaused() && !proceduralMuted(),
                draggable: false,
                position: 0,
                duration: 0,
                canSeek: false,
            };
        }
        return {
            visible: false,
            source,
            primary: "none",
            badge: "",
            title: "",
            subtitle: "",
            playing: false,
            draggable: false,
            position: 0,
            duration: 0,
            canSeek: false,
        };
    }

    function modeLabel(mode: BgmPlaybackMode): string {
        if (mode === "single") return "单曲循环";
        if (mode === "shuffle") return "随机";
        return "列表循环";
    }

    function ensureElement(): HTMLElement {
        if (element) return element;
        const section = document.createElement("section");
        section.className = "immersive-player";
        section.id = "immersivePlayer";
        section.hidden = true;
        section.setAttribute("aria-label", "配乐播放器");
        section.innerHTML = `
            <div class="immersive-player-head">
                <span class="immersive-player-kicker">MUSIC</span>
                <span class="immersive-player-badge" id="immersivePlayerBadge">配乐</span>
            </div>
            <div class="immersive-player-body">
                <div class="immersive-player-meta">
                    <strong class="immersive-player-title" id="immersivePlayerTitle">—</strong>
                    <span class="immersive-player-sub" id="immersivePlayerSub"></span>
                </div>
                <div class="immersive-player-controls">
                    <button type="button" class="immersive-player-btn" id="immersivePlayerPrev" title="上一首 / 上一段" aria-label="上一首或上一段">
                        <i class="fa fa-step-backward" aria-hidden="true"></i>
                    </button>
                    <button type="button" class="immersive-player-btn is-primary" id="immersivePlayerToggle" title="播放 / 暂停" aria-label="播放或暂停">
                        <i class="fa fa-play" aria-hidden="true"></i>
                    </button>
                    <button type="button" class="immersive-player-btn" id="immersivePlayerNext" title="下一首 / 下一段" aria-label="下一首或下一段">
                        <i class="fa fa-step-forward" aria-hidden="true"></i>
                    </button>
                </div>
            </div>
            <div class="immersive-player-progress">
                <input type="range" id="immersivePlayerRange" class="immersive-player-range" min="0" max="1000" step="1" value="0" aria-label="播放进度">
                <div class="immersive-player-livebar" id="immersivePlayerLive" hidden>
                    <span class="immersive-player-livedot"></span>
                    <span class="immersive-player-livetext">实时生成中</span>
                </div>
                <div class="immersive-player-times">
                    <span id="immersivePlayerElapsed">0:00</span>
                    <span id="immersivePlayerDuration">--:--</span>
                </div>
            </div>
        `;
        document.body.appendChild(section);
        element = section;
        return section;
    }

    function isNarrowLayout(): boolean {
        return window.matchMedia("(max-width: 767.98px)").matches;
    }

    /** 与场景面板共用宿主：桌面在右侧栏沉浸区，窄屏移回聊天区。 */
    function mount(): void {
        const node = ensureElement();
        const hostId = isNarrowLayout() ? "immersiveChatStageHost" : "immersiveSidebarStageHost";
        const host = document.getElementById(hostId);
        if (!host) {
            if (mountRetry < 20) {
                mountRetry += 1;
                window.requestAnimationFrame(mount);
            }
            return;
        }
        mountRetry = 0;
        mountedHostId = hostId;
        if (node.parentElement !== host) host.appendChild(node);
    }

    function bindEvents(): void {
        if (bound) return;
        bound = true;
        byId<HTMLButtonElement>("immersivePlayerToggle")?.addEventListener("click", () => togglePlayback());
        byId<HTMLButtonElement>("immersivePlayerPrev")?.addEventListener("click", () => skip(-1));
        byId<HTMLButtonElement>("immersivePlayerNext")?.addEventListener("click", () => skip(1));

        const range = byId<HTMLInputElement>("immersivePlayerRange");
        if (range) {
            const startDrag = (): void => {
                dragging = true;
            };
            const endDrag = (): void => {
                if (!dragging) return;
                dragging = false;
                const duration = Number(range.dataset.duration || 0);
                if (duration > 0) {
                    window.TrpgBgm?.seek?.((Number(range.value) / 1000) * duration);
                }
            };
            range.addEventListener("pointerdown", startDrag);
            range.addEventListener("pointerup", endDrag);
            range.addEventListener("pointercancel", endDrag);
            range.addEventListener("change", endDrag);
            range.addEventListener("input", () => {
                if (!dragging) return;
                const duration = Number(range.dataset.duration || 0);
                const elapsed = byId<HTMLElement>("immersivePlayerElapsed");
                if (elapsed && duration > 0) {
                    elapsed.textContent = formatTime((Number(range.value) / 1000) * duration);
                }
            });
        }

        document.addEventListener("trpg:bgm-state", () => refresh());
        document.addEventListener("trpg:music-state", () => refresh());
        window.addEventListener("resize", () => {
            const nextHost = isNarrowLayout() ? "immersiveChatStageHost" : "immersiveSidebarStageHost";
            if (nextHost !== mountedHostId) mount();
        });
    }

    function togglePlayback(): void {
        const snapshot = resolveSnapshot();
        if (snapshot.primary === "bgm") {
            if (snapshot.playing) {
                window.TrpgBgm?.pause?.();
                // 混合档：暂停要连同叠加的实时配乐一起停，避免只停一半。
                if (snapshot.source === "hybrid") window.TrpgImmersiveMusic?.pause?.();
            } else {
                window.TrpgBgm?.play?.();
                if (snapshot.source === "hybrid") window.TrpgImmersiveMusic?.resume?.();
            }
        } else if (snapshot.primary === "procedural") {
            const music = window.TrpgImmersiveMusic;
            if (snapshot.playing) {
                music?.pause?.();
            } else {
                // 之前被静音过：点播放先解除静音，再从暂停位置继续。
                if (proceduralMuted()) music?.setMuted?.(false);
                music?.resume?.();
            }
        }
    }

    function skip(direction: number): void {
        const snapshot = resolveSnapshot();
        if (snapshot.primary === "bgm") {
            if (direction >= 0) window.TrpgBgm?.next?.();
            else window.TrpgBgm?.prev?.();
        } else if (snapshot.primary === "procedural") {
            window.TrpgImmersiveMusic?.skipSection?.(direction);
        }
    }

    export function refresh(): void {
        const node = element || ensureElement();
        if (!node) return;
        const roomId = currentRoomId();
        if (roomId !== lastRoomId) {
            lastRoomId = roomId;
            // 换房间时重新挂载，保证窄屏/桌面切换后仍在正确的宿主里。
            mount();
        }
        const snapshot = resolveSnapshot();
        // 宿主可能比脚本晚挂载（例如从个人主页切到房间）：一旦可见就补一次挂载。
        // 注意：节点创建时先挂在 body 上，所以不能用 parentElement 是否为空来判断。
        const desiredHost = isNarrowLayout() ? "immersiveChatStageHost" : "immersiveSidebarStageHost";
        if (node.parentElement?.id !== desiredHost) {
            mountRetry = 0;
            mount();
        }
        const mounted = node.parentElement?.id === desiredHost;
        // 只在真正挂进宿主时显示：不在聊天页时宿主不存在，不能让播放器裸挂在 body 末尾。
        node.hidden = !snapshot.visible || !mounted;
        if (!snapshot.visible || !mounted) return;

        const badge = byId<HTMLElement>("immersivePlayerBadge");
        if (badge) badge.textContent = snapshot.badge;
        const title = byId<HTMLElement>("immersivePlayerTitle");
        if (title) title.textContent = snapshot.title;
        const sub = byId<HTMLElement>("immersivePlayerSub");
        if (sub) sub.textContent = snapshot.subtitle;

        const toggle = byId<HTMLButtonElement>("immersivePlayerToggle");
        const icon = toggle?.querySelector<HTMLElement>("i") ?? null;
        if (icon) icon.className = snapshot.playing ? "fa fa-pause" : "fa fa-play";
        if (toggle) {
            const label = snapshot.playing ? "暂停" : "播放";
            toggle.title = label;
            toggle.setAttribute("aria-label", label);
        }

        const range = byId<HTMLInputElement>("immersivePlayerRange");
        const live = byId<HTMLElement>("immersivePlayerLive");
        const elapsed = byId<HTMLElement>("immersivePlayerElapsed");
        const duration = byId<HTMLElement>("immersivePlayerDuration");
        if (range) {
            range.hidden = !snapshot.draggable;
            range.disabled = !snapshot.canSeek;
            range.dataset.duration = String(snapshot.duration || 0);
            if (!dragging && snapshot.duration > 0) {
                const ratio = Math.max(0, Math.min(1, snapshot.position / snapshot.duration));
                range.value = String(Math.round(ratio * 1000));
            }
        }
        if (live) live.hidden = snapshot.draggable;
        if (elapsed) {
            elapsed.hidden = !snapshot.draggable;
            if (snapshot.draggable && !dragging) elapsed.textContent = formatTime(snapshot.position);
        }
        if (duration) {
            duration.hidden = !snapshot.draggable;
            duration.textContent = snapshot.canSeek ? formatTime(snapshot.duration) : "--:--";
        }

        // 程序化配乐没有音轨：进度区退化为「实时生成中」指示。
        node.classList.toggle("is-live", !snapshot.draggable);
    }

    function startTicker(): void {
        if (refreshTimer !== null) return;
        refreshTimer = window.setInterval(() => {
            // 即使当前隐藏也要算一次：进房/换档位后播放器需要自己能出现。
            if (element) refresh();
        }, 1000);
    }

    export function init(): void {
        ensureElement();
        bindEvents();
        mount();
        mountRetry = 0;
        refresh();
        startTicker();
    }
}

window.TrpgImmersivePlayer = {
    init: TrpgImmersivePlayerModule.init,
    refresh: TrpgImmersivePlayerModule.refresh,
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => TrpgImmersivePlayerModule.init());
} else {
    TrpgImmersivePlayerModule.init();
}
