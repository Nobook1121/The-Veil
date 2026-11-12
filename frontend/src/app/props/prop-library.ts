/**
 * 道具袋（设计稿 §8.2）。
 *
 * 网格卡片 + 懒加载缩略图 + 全部/未揭示/已揭示筛选。KP 可编辑、投递、
 * 复制、删除；玩家只看到后端已过滤的已揭示道具，且只有「查看」。
 */
interface Window {
    TrpgPropLibrary?: { reopen?: () => void };
}

(function initializePropLibrary(global: Window): void {
    "use strict";

    type PropFilter = "all" | "hidden" | "revealed";

    let initialized = false;
    let modal: BootstrapModalInstance | null = null;
    let roomId = "";
    let filter: PropFilter = "all";
    let entries: PropIndexEntry[] = [];
    let templates: PropTemplate[] = [];
    let renderVersion = Date.now();
    let loading = false;

    function api(): TrpgPropApiRuntime {
        const runtime = global.TrpgPropApi;
        if (!runtime) throw new Error("TrpgPropApi is not ready");
        return runtime;
    }

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string, values?: Record<string, string | number>): string {
        return global.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function currentMember(): RoomMember | null {
        const room = global.currentRoom;
        const userId = String(global.currentUser?.user_id ?? "");
        if (!room || !userId) return null;
        return (room.members || []).find((member) => String(member.user_id) === userId) || null;
    }

    function isKp(): boolean {
        if (["ADMIN", "OWNER"].includes(String(global.currentUser?.role || ""))) return true;
        const room = global.currentRoom;
        if (!room) return false;
        const member = currentMember();
        if (member?.room_role === "owner" || member?.room_role === "admin") return true;
        return String(room.creator_id ?? "") === String(global.currentUser?.user_id ?? "");
    }

    function canOpenLibrary(): boolean {
        const room = global.currentRoom;
        return Boolean(room?.id) && room?.invisible_view !== true;
    }

    function syncEntryVisibility(): void {
        const button = byId<HTMLButtonElement>("roomPropsBtn");
        if (button) button.hidden = !canOpenLibrary();
        const createButton = byId<HTMLButtonElement>("propLibraryNew");
        if (createButton) createButton.hidden = !isKp();
    }

    function setStatus(text: string, tone: "idle" | "ok" | "error" = "idle"): void {
        const status = byId<HTMLElement>("propLibraryStatus");
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("is-error", tone === "error");
        status.classList.toggle("is-ok", tone === "ok");
    }

    function notifyError(error: unknown): void {
        const runtime = api();
        const fallback = t("prop.error.generic", "操作失败，请稍后重试。");
        const message = t(`prop.error.${runtime.errorCode(error)}`, runtime.errorText(error, fallback));
        global.showNotification?.(message, "error");
        setStatus(message, "error");
    }

    function templateOf(entry: PropIndexEntry): PropTemplate | null {
        return templates.find((item) => item.id === entry.template_id) || null;
    }

    function visibilityMode(entry: PropIndexEntry): "hidden" | "revealed" | "partial" {
        const mode = entry.visibility?.mode;
        return mode === "revealed" || mode === "partial" ? mode : "hidden";
    }

    function matchesFilter(entry: PropIndexEntry): boolean {
        const mode = visibilityMode(entry);
        if (filter === "hidden") return mode === "hidden";
        if (filter === "revealed") return mode !== "hidden";
        return true;
    }

    function thumbnailUrl(entry: PropIndexEntry): string {
        return api().renderUrl(roomId, entry.id, "png", `${renderVersion}`);
    }

    function tileHtml(entry: PropIndexEntry): string {
        const template = templateOf(entry);
        const mode = visibilityMode(entry);
        const icon = api().templateIcon(entry.template_id, template?.category);
        const templateName = template?.name || entry.template_id;
        const actions: string[] = [];
        if (isKp()) {
            actions.push(`<button type="button" class="btn btn-outline-secondary btn-sm" data-action="edit"><i class="fa fa-pencil" aria-hidden="true"></i> ${escapeHtml(t("prop.action.edit", "编辑"))}</button>`);
            actions.push(`<button type="button" class="btn btn-outline-secondary btn-sm" data-action="reveal"><i class="fa fa-paper-plane-o" aria-hidden="true"></i> ${escapeHtml(t("prop.action.reveal", "发给玩家"))}</button>`);
            actions.push(`<button type="button" class="btn btn-outline-secondary btn-sm" data-action="copy" title="${escapeHtml(t("prop.action.copy", "复制"))}" aria-label="${escapeHtml(t("prop.action.copy", "复制"))}"><i class="fa fa-copy" aria-hidden="true"></i></button>`);
            actions.push(`<button type="button" class="btn btn-outline-danger btn-sm" data-action="delete" title="${escapeHtml(t("prop.action.delete", "删除"))}" aria-label="${escapeHtml(t("prop.action.delete", "删除"))}"><i class="fa fa-trash" aria-hidden="true"></i></button>`);
        }
        return `<article class="prop-tile" data-prop-id="${escapeHtml(entry.id)}">
            <button type="button" class="prop-tile-thumb" data-action="view" aria-label="${escapeHtml(t("prop.action.view", "查看"))}">
                <img loading="lazy" src="${escapeHtml(thumbnailUrl(entry))}" alt="">
                <span class="prop-tile-fallback"><i class="${escapeHtml(icon)}" aria-hidden="true"></i></span>
            </button>
            <div class="prop-tile-info">
                <h6 class="prop-tile-title" title="${escapeHtml(entry.title)}">${escapeHtml(entry.title)}</h6>
                <div class="prop-tile-meta">
                    <i class="${escapeHtml(icon)}" aria-hidden="true"></i>
                    <span>${escapeHtml(templateName)}</span>
                    <span class="prop-badge prop-badge-${mode}">${escapeHtml(t(`prop.visibility.${mode}`, mode))}</span>
                </div>
            </div>
            ${actions.length ? `<div class="prop-tile-actions">${actions.join("")}</div>` : ""}
        </article>`;
    }

    function bindThumbFallbacks(): void {
        const grid = byId<HTMLElement>("propLibraryGrid");
        grid?.querySelectorAll<HTMLImageElement>(".prop-tile-thumb img").forEach((image) => {
            image.addEventListener("error", () => {
                image.classList.add("is-broken");
                image.closest(".prop-tile-thumb")?.classList.add("is-broken");
            }, { once: true });
        });
    }

    function renderGrid(): void {
        const grid = byId<HTMLElement>("propLibraryGrid");
        if (!grid) return;
        const visible = entries.filter(matchesFilter);
        if (loading) {
            grid.innerHTML = `<div class="prop-empty">${escapeHtml(t("prop.status.loading", "加载中…"))}</div>`;
            return;
        }
        if (!visible.length) {
            grid.innerHTML = `<div class="prop-empty"><i class="fa fa-folder-open-o" aria-hidden="true"></i><p>${escapeHtml(t("prop.status.empty", "道具袋还是空的。"))}</p></div>`;
            return;
        }
        grid.innerHTML = visible.map(tileHtml).join("");
        bindThumbFallbacks();
    }

    async function refresh(): Promise<void> {
        if (!roomId) return;
        loading = true;
        renderGrid();
        try {
            templates = await api().templates(roomId).catch(() => [] as PropTemplate[]);
            entries = await api().list(roomId);
            renderVersion = Date.now();
            setStatus("");
        } catch (error) {
            entries = [];
            notifyError(error);
        } finally {
            loading = false;
            renderGrid();
        }
    }

    function entryById(propId: string): PropIndexEntry | null {
        return entries.find((entry) => entry.id === propId) || null;
    }

    function openViewer(entry: PropIndexEntry): void {
        const template = templateOf(entry);
        // 查看器由 P3 的 prop-card.js 提供；这里只做可选调用，避免 P2 单独构建时强依赖。
        const viewerApi = (global as unknown as {
            TrpgPropCard?: { openViewer: (options: {
                roomId: string;
                propId: string;
                title: string;
                templateId: string;
                templateName: string;
                version: number;
            }) => void };
        }).TrpgPropCard;
        viewerApi?.openViewer({
            roomId,
            propId: entry.id,
            title: entry.title,
            templateId: entry.template_id,
            templateName: template?.name || entry.template_id,
            version: Date.now(),
        });
    }

    function openEditor(entry: PropIndexEntry): void {
        modal?.hide();
        global.TrpgPropEditor?.open({
            roomId,
            propId: entry.id,
            templateId: entry.template_id,
            returnToLibrary: true,
        });
    }

    async function copyProp(entry: PropIndexEntry): Promise<void> {
        try {
            const spec = await api().get(roomId, entry.id);
            await api().create(roomId, {
                template_id: spec.template_id,
                title: `${entry.title}${t("prop.action.copy_suffix", "（副本）")}`,
                fields: spec.fields,
                style: spec.style || {},
                wear: spec.wear || {},
                redaction: spec.redaction || [],
                visibility: { mode: "hidden", revealed_to: [] },
            });
            global.showNotification?.(t("prop.copy.success", "已复制道具。"), "success");
            await refresh();
        } catch (error) {
            notifyError(error);
        }
    }

    async function deleteProp(entry: PropIndexEntry): Promise<void> {
        const message = t("prop.delete.confirm", "确定删除「{title}」？该操作不可撤销。", { title: entry.title });
        const confirmed = await global.TrpgDialog?.confirm({
            title: t("prop.delete.title", "删除道具"),
            message,
            confirmText: t("prop.action.delete", "删除"),
            cancelText: t("prop.action.cancel", "取消"),
            danger: true,
        });
        if (!confirmed) return;
        try {
            await api().remove(roomId, entry.id);
            global.showNotification?.(t("prop.delete.success", "道具已删除。"), "success");
            await refresh();
        } catch (error) {
            notifyError(error);
        }
    }

    function openReveal(entry: PropIndexEntry): void {
        const members = (global.currentRoom?.members || []).filter((member) =>
            member.is_active !== false && member.status !== "removed" && member.user_id !== undefined && member.user_id !== null,
        );
        const modalElement = document.createElement("div");
        modalElement.className = "modal fade";
        modalElement.tabIndex = -1;
        modalElement.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${escapeHtml(t("prop.reveal.title", "发给玩家"))}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                    </div>
                    <div class="modal-body">
                        <p class="small text-muted">${escapeHtml(entry.title)}</p>
                        <div class="form-check">
                            <input class="form-check-input" type="checkbox" id="propRevealAll">
                            <label class="form-check-label" for="propRevealAll">${escapeHtml(t("prop.reveal.all", "全体成员"))}</label>
                        </div>
                        <hr>
                        <div class="prop-reveal-members">
                            ${members.map((member) => `<div class="form-check">
                                <input class="form-check-input" type="checkbox" value="${escapeHtml(member.user_id)}" id="propRevealMember${escapeHtml(member.user_id)}">
                                <label class="form-check-label" for="propRevealMember${escapeHtml(member.user_id)}">${escapeHtml(member.username || String(member.user_id))}</label>
                            </div>`).join("")}
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">${escapeHtml(t("prop.action.cancel", "取消"))}</button>
                        <button type="button" class="btn btn-primary" data-reveal-confirm>${escapeHtml(t("prop.action.reveal", "发给玩家"))}</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modalElement);
        const instance = new bootstrap.Modal(modalElement);
        const allCheckbox = modalElement.querySelector<HTMLInputElement>("#propRevealAll");
        const memberChecks = Array.from(modalElement.querySelectorAll<HTMLInputElement>(".prop-reveal-members input"));
        allCheckbox?.addEventListener("change", () => {
            memberChecks.forEach((checkbox) => { checkbox.disabled = Boolean(allCheckbox.checked); });
        });
        modalElement.querySelector("[data-reveal-confirm]")?.addEventListener("click", () => {
            void (async () => {
                const all = Boolean(allCheckbox?.checked);
                const to = memberChecks.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value);
                if (!all && !to.length) {
                    global.showNotification?.(t("prop.reveal.empty", "请至少选择一位玩家。"), "warning");
                    return;
                }
                try {
                    await api().reveal(roomId, entry.id, {
                        ...(all ? { all: true } : { to }),
                        message: t("prop.reveal.message", "递来一件道具：{title}", { title: entry.title }),
                    });
                    instance.hide();
                    global.showNotification?.(t("prop.reveal.success", "已发给玩家。"), "success");
                    await refresh();
                } catch (error) {
                    notifyError(error);
                }
            })();
        });
        modalElement.addEventListener("hidden.bs.modal", () => modalElement.remove());
        instance.show();
    }

    function handleGridClick(event: Event): void {
        const target = event.target as Element | null;
        const action = target?.closest<HTMLElement>("[data-action]")?.dataset.action;
        const tile = target?.closest<HTMLElement>(".prop-tile");
        if (!action || !tile?.dataset.propId) return;
        const entry = entryById(tile.dataset.propId);
        if (!entry) return;
        switch (action) {
            case "view": openViewer(entry); break;
            case "edit": openEditor(entry); break;
            case "reveal": openReveal(entry); break;
            case "copy": void copyProp(entry); break;
            case "delete": void deleteProp(entry); break;
        }
    }

    function setFilter(next: PropFilter): void {
        filter = next;
        document.querySelectorAll<HTMLElement>("#propLibraryFilter [data-filter]").forEach((button) => {
            button.classList.toggle("is-active", button.dataset.filter === next);
        });
        renderGrid();
    }

    function bind(): void {
        if (initialized) return;
        initialized = true;
        const modalElement = byId<HTMLElement>("propLibraryModal");
        if (!modalElement) return;
        modal = new bootstrap.Modal(modalElement);
        byId<HTMLElement>("roomPropsBtn")?.addEventListener("click", () => { open(); });
        byId<HTMLElement>("propLibraryGrid")?.addEventListener("click", handleGridClick);
        byId<HTMLElement>("propLibraryFilter")?.addEventListener("click", (event) => {
            const button = (event.target as Element | null)?.closest<HTMLElement>("[data-filter]");
            const value = button?.dataset.filter as PropFilter | undefined;
            if (value) setFilter(value);
        });
        byId<HTMLElement>("propLibraryRefresh")?.addEventListener("click", () => { void refresh(); });
        byId<HTMLElement>("propLibraryNew")?.addEventListener("click", () => {
            modal?.hide();
            global.TrpgPropEditor?.open({ roomId, returnToLibrary: true });
        });
        global.addEventListener("trpg:props-changed", () => { void refresh(); });
        global.addEventListener("trpg:locale-changed", () => { if (entries.length) renderGrid(); });
        global.addEventListener("trpg:user-changed", syncEntryVisibility);
        bindRoomDetailObserver();
        syncEntryVisibility();
    }

    function bindRoomDetailObserver(): void {
        const detail = byId<HTMLElement>("save-detail-view");
        if (!detail) return;
        const observer = new MutationObserver(syncEntryVisibility);
        observer.observe(detail, { attributes: true, attributeFilter: ["style"] });
    }

    function open(): void {
        bind();
        const id = String(global.currentRoom?.id || "");
        if (!id) return;
        roomId = id;
        syncEntryVisibility();
        modal?.show();
        void refresh();
    }

    function reopen(): void {
        bind();
        modal?.show();
        void refresh();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bind);
    } else {
        bind();
    }

    global.TrpgPropLibrary = { reopen };
})(window);
