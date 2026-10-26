/**
 * 房间世界树运行态面板（设计文档 §8.2 / §8.5）。
 *
 * 依赖已实现的后端 API：
 * - GET /api/rooms/<room_id>/narrative → 当前地点、队伍分布、运行状态
 * - GET /api/rooms/<room_id>/triggers → 当前地点可用触发器（仅 id/label/binding）
 * - POST /api/rooms/<room_id>/triggers/<trigger_id>/fire → 房主/管理员手动触发
 *
 * 进入房间时由 rooms.ts 调用 load()，离开/切换时调用 clear()。
 * 面板只读展示运行态；手动触发入口仅对房主/管理员可见。
 */
(function initializeRoomNarrative(global: Window) {
    "use strict";

    const PollIntervalMs = 4000;

    let currentRoomId = "";
    let canManage = false;
    let loading = false;
    let pollTimer: number | null = null;

    interface NarrativeLocation {
        id: string;
        title: string;
        kind: string;
        visible: boolean;
        visited: boolean;
    }

    interface PartyLocation {
        pc_id: string;
        location_id: string;
        location_title: string;
    }

    interface PartyMember {
        user_id: string;
        username: string;
        room_role?: string;
        active?: boolean;
    }

    interface NarrativeParty {
        split: boolean;
        locations: PartyLocation[];
        members: PartyMember[];
    }

    interface NarrativeClock {
        id: string;
        title: string;
        value: number;
        max: number;
    }

    interface NarrativeStatus {
        mode: string | null;
        topology: string;
        scenario_version: string;
        turn: number;
        clocks: Record<string, NarrativeClock>;
        flags: Record<string, unknown>;
        node_status: Record<string, unknown>;
        trigger_fires: number;
        revealed_clues: string[];
        visit_count: number;
    }

    interface NarrativeSnapshot {
        room_id: string;
        scenario_id: number | string | null;
        location: NarrativeLocation;
        party: NarrativeParty;
        status: NarrativeStatus;
        permissions: { can_manage: boolean };
    }

    interface AvailableTrigger {
        trigger_id: string;
        label: string;
        binding: string;
    }

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function stopPolling(): void {
        if (pollTimer !== null) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function renderLocation(location: NarrativeLocation, canManageRoom: boolean): void {
        const host = byId("roomNarrativeLocation");
        if (!host) return;
        if (!location || !location.id) {
            host.innerHTML = `<p class="text-muted mb-0">${escapeHtml(t("room.narrative.no_location", "尚未确定当前地点。"))}</p>`;
        } else {
            const visibility = location.visible
                ? `<span class="badge bg-info text-dark">${escapeHtml(t("room.narrative.visible", "已揭示"))}</span>`
                : `<span class="badge bg-danger">${escapeHtml(t("room.narrative.kp_only", "仅 KP"))}</span>`;
            const visited = location.visited
                ? `<span class="badge bg-primary">${escapeHtml(t("room.narrative.visited", "已访问"))}</span>`
                : `<span class="badge bg-light text-dark">${escapeHtml(t("room.narrative.unvisited", "未访问"))}</span>`;
            host.innerHTML = `
                <div class="d-flex justify-content-between align-items-center">
                    <span><i class="fa fa-map-marker-alt me-1" aria-hidden="true"></i>${escapeHtml(location.title || location.id)}</span>
                    <span>${visibility} ${visited}</span>
                </div>
                <div class="small text-muted mt-1">${escapeHtml(location.kind || "")}</div>`;
        }
        const kpPanel = byId("roomNarrativeKpOnly");
        if (kpPanel) kpPanel.hidden = !(canManageRoom && location && location.id && !location.visible);
    }

    function renderParty(party: NarrativeParty): void {
        const host = byId("roomNarrativeParty");
        if (!host) return;
        const members = Array.isArray(party.members) ? party.members : [];
        const byIdMap = new Map(members.map((member) => [String(member.user_id), member]));
        const locations = Array.isArray(party.locations) ? party.locations : [];
        if (!party.split || !locations.length) {
            const names = members.map((member) => member.username || String(member.user_id)).filter(Boolean);
            host.innerHTML = names.length
                ? `<p class="mb-0">${escapeHtml(t("room.narrative.together", "全员同处一室。"))}</p>
                   <div class="small text-muted">${escapeHtml(names.join("、"))}</div>`
                : `<p class="text-muted mb-0">${escapeHtml(t("room.narrative.no_members", "房间内暂无玩家。"))}</p>`;
            return;
        }
        const groups = new Map<string, string[]>();
        for (const entry of locations) {
            const member = byIdMap.get(String(entry.pc_id));
            const name = member?.username || String(entry.pc_id);
            const title = entry.location_title || entry.location_id;
            const list = groups.get(title) || [];
            list.push(name);
            groups.set(title, list);
        }
        host.innerHTML = Array.from(groups.entries()).map(([title, names]) =>
            `<div class="mb-1"><strong>${escapeHtml(title)}</strong>：${escapeHtml(names.join("、"))}</div>`
        ).join("");
    }

    function renderClocks(status: NarrativeStatus): void {
        const host = byId("roomNarrativeClocks");
        if (!host) return;
        const clocks = status.clocks && typeof status.clocks === "object"
            ? Object.values(status.clocks) as NarrativeClock[]
            : [];
        host.innerHTML = clocks.length
            ? clocks.map((clock) => {
                const max = Math.max(1, Number(clock.max) || 1);
                const value = Math.min(max, Math.max(0, Number(clock.value) || 0));
                return `
                    <li class="list-group-item">
                        <div class="d-flex justify-content-between align-items-center mb-1">
                            <span>${escapeHtml(clock.title || clock.id)}</span>
                            <span class="small text-muted">${value} / ${max}</span>
                        </div>
                        <div class="progress" style="height: 6px;">
                            <div class="progress-bar" role="progressbar" style="width: ${Math.round((value / max) * 100)}%;"
                                aria-valuenow="${value}" aria-valuemin="0" aria-valuemax="${max}"></div>
                        </div>
                    </li>`;
            }).join("")
            : `<p class="text-muted mb-0">${escapeHtml(t("room.narrative.no_clocks", "当前没有活动时钟。"))}</p>`;
    }

    function renderStatus(status: NarrativeStatus): void {
        const host = byId("roomNarrativeStatus");
        if (!host) return;
        const flags = status.flags && typeof status.flags === "object" ? Object.keys(status.flags).length : 0;
        const reveals = Array.isArray(status.revealed_clues) ? status.revealed_clues.length : 0;
        const parts = [
            `${t("room.narrative.turn", "回合")} ${status.turn ?? 0}`,
            `${t("room.narrative.trigger_fires", "已触发")} ${status.trigger_fires ?? 0}`,
            `${t("room.narrative.revealed_clues", "已揭示线索")} ${reveals}`,
            `${t("room.narrative.flags", "旗标")} ${flags}`,
            `${t("room.narrative.visits", "访问记录")} ${status.visit_count ?? 0}`,
        ];
        host.textContent = parts.join(" · ");
    }

    function renderTriggers(items: AvailableTrigger[]): void {
        const host = byId("roomNarrativeTriggers");
        if (!host) return;
        const visible = items.filter((item) => item && item.trigger_id);
        if (!visible.length) {
            host.innerHTML = `<p class="text-muted mb-0">${escapeHtml(t("room.narrative.no_triggers", "当前地点没有可用触发器。"))}</p>`;
            return;
        }
        host.innerHTML = visible.map((item) => {
            const manual = item.binding === "manual";
            return `
                <div class="room-narrative-trigger">
                    <div>
                        <span class="fw-semibold">${escapeHtml(item.label || item.trigger_id)}</span>
                        <span class="badge bg-light text-dark ms-1">${escapeHtml(item.binding || "-")}</span>
                    </div>
                    ${manual && canManage
                        ? `<button type="button" class="btn btn-sm btn-outline-primary roomNarrativeFireTrigger" data-trigger-id="${escapeHtml(item.trigger_id)}" data-trigger-label="${escapeHtml(item.label || item.trigger_id)}">${escapeHtml(t("room.narrative.fire", "触发"))}</button>`
                        : ""}
                </div>`;
        }).join("");
    }

    async function refresh(silent = false): Promise<void> {
        if (!currentRoomId || loading) return;
        loading = true;
        try {
            const { response, data } = await window.TrpgApi.requestWithResponse<{ success: boolean; data?: NarrativeSnapshot; message?: string }>(
                `/api/rooms/${encodeURIComponent(currentRoomId)}/narrative`,
                { method: "GET" },
            );
            if (!response.ok || !data?.success || !data.data) {
                if (response.status === 404) {
                    const panel = byId("roomNarrativePanel");
                    if (panel) panel.hidden = true;
                } else if (!silent) {
                    showNotification(data?.message || `HTTP ${response.status}`, "error");
                }
                return;
            }
            const panel = byId("roomNarrativePanel");
            if (panel) panel.hidden = false;
            const snapshot = data.data;
            canManage = Boolean(snapshot.permissions?.can_manage);
            renderLocation(snapshot.location, canManage);
            renderParty(snapshot.party);
            renderStatus(snapshot.status);
            renderClocks(snapshot.status);
            if (canManage) await loadTriggers();
        } catch (error) {
            if (!silent) {
                const message = error instanceof Error ? error.message : String(error);
                showNotification(message, "error");
            }
        } finally {
            loading = false;
        }
    }

    async function loadTriggers(): Promise<void> {
        if (!currentRoomId || !canManage) return;
        try {
            const { response, data } = await window.TrpgApi.requestWithResponse<{ success: boolean; data?: { triggers?: AvailableTrigger[] } }>(
                `/api/rooms/${encodeURIComponent(currentRoomId)}/triggers`,
                { method: "GET" },
            );
            if (!response.ok || !data?.success || !data.data) return;
            renderTriggers(Array.isArray(data.data.triggers) ? data.data.triggers : []);
        } catch {
            // 静默失败：下一次轮询会重试，避免打断房间主流程。
        }
    }

    async function fireTrigger(triggerId: string, label: string): Promise<void> {
        if (!currentRoomId || !canManage) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: t("room.narrative.confirm_title", "手动触发"),
            message: t("room.narrative.confirm_message", "确认手动触发「{label}」？该操作会立即更新房间运行态。").replace("{label}", label),
            confirmText: t("room.narrative.fire", "触发"),
            cancelText: t("common.cancel", "取消"),
        });
        if (!confirmed) return;
        try {
            const { response, data } = await window.TrpgApi.requestWithResponse<{ success: boolean; message?: string; error?: string }>(
                `/api/rooms/${encodeURIComponent(currentRoomId)}/triggers/${encodeURIComponent(triggerId)}/fire`,
                { method: "POST" },
            );
            if (!response.ok || !data?.success) {
                throw new Error(data?.message || data?.error || `HTTP ${response.status}`);
            }
            showNotification(t("room.narrative.fired", "触发器已手动触发。"), "success");
            await refresh(true);
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            showNotification(message || t("room.narrative.fire_failed", "触发器触发失败。"), "error");
        }
    }

    function bindActions(): void {
        byId("roomNarrativeRefresh")?.addEventListener("click", () => { void refresh(); });
        byId("roomNarrativeTriggers")?.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".roomNarrativeFireTrigger");
            if (!button) return;
            const triggerId = String(button.dataset.triggerId || "");
            const label = String(button.dataset.triggerLabel || triggerId);
            if (triggerId) void fireTrigger(triggerId, label);
        });
    }

    async function load(roomId: string): Promise<void> {
        clear();
        currentRoomId = String(roomId || "");
        if (!currentRoomId) return;
        await refresh(true);
        stopPolling();
        pollTimer = window.setInterval(() => { void refresh(true); }, PollIntervalMs);
    }

    function clear(): void {
        stopPolling();
        currentRoomId = "";
        canManage = false;
        loading = false;
        for (const id of ["roomNarrativeLocation", "roomNarrativeParty", "roomNarrativeStatus", "roomNarrativeClocks", "roomNarrativeTriggers"]) {
            const host = byId(id);
            if (host) host.innerHTML = "";
        }
        const kpPanel = byId("roomNarrativeKpOnly");
        if (kpPanel) kpPanel.hidden = true;
        const panel = byId("roomNarrativePanel");
        if (panel) panel.hidden = true;
    }

    function init(): void {
        bindActions();
    }

    global.RoomNarrative = { load, clear, init };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
