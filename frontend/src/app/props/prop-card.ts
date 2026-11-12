/**
 * 消息里的道具卡片与查看器（设计稿 §8.4）。
 *
 * `type: "prop"` 消息由 chat.ts 直接交给本文件渲染；未在 `metadata.revealed_to`
 * 名单内的玩家（非 KP）不渲染任何东西，避免泄露存在性。标题与模板名通过
 * `GET /props/<id>` 补齐，接口 404 时降级为不可查看。
 */
interface PropViewerOptions {
    roomId: string;
    propId: string;
    title?: string;
    templateId?: string;
    templateName?: string;
    version?: string | number;
}

interface TrpgPropCardRuntime {
    render: (message: ChatMessage) => HTMLElement | null;
    openViewer: (options: PropViewerOptions) => void;
}

interface Window {
    TrpgPropCard?: TrpgPropCardRuntime;
}

(function initializePropCard(global: Window): void {
    "use strict";

    const specCache = new Map<string, PropSpec>();
    let viewer: BootstrapModalInstance | null = null;
    let delegated = false;

    function api(): TrpgPropApiRuntime | null {
        return global.TrpgPropApi || null;
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

    function isKp(): boolean {
        if (["ADMIN", "OWNER"].includes(String(global.currentUser?.role || ""))) return true;
        const room = global.currentRoom;
        if (!room) return false;
        const userId = String(global.currentUser?.user_id ?? "");
        const member = (room.members || []).find((item) => String(item.user_id) === userId);
        if (member?.room_role === "owner" || member?.room_role === "admin") return true;
        return String(room.creator_id ?? "") === userId;
    }

    /** `revealed_to` 为空表示全体；非空则只有名单内玩家与 KP 可见。 */
    function isRecipient(metadata: Record<string, unknown>): boolean {
        const raw = metadata.revealed_to;
        if (!Array.isArray(raw) || raw.length === 0) return true;
        if (isKp()) return true;
        const me = String(global.currentUser?.user_id ?? "");
        return raw.map((item) => String(item)).includes(me);
    }

    function resolveTitle(spec: PropSpec | null, fallbackTitle: string): string {
        return String(spec?.title || fallbackTitle || t("prop.card.untitled", "一件道具"));
    }

    function templateName(templateId: string): string {
        const runtime = api();
        const roomId = String(global.currentRoom?.id || "");
        const template = runtime && roomId ? runtime.findTemplate(roomId, templateId) : null;
        return template?.name || templateId;
    }

    function cardMarkup(propId: string, title: string, templateId: string, time: string): string {
        const icon = api()?.templateIcon(templateId) || "fa fa-file-text-o";
        return `<button type="button" class="prop-card" data-prop-card="${escapeHtml(propId)}">
            <span class="prop-card-seal" aria-hidden="true"><i class="${escapeHtml(icon)}"></i></span>
            <span class="prop-card-main">
                <span class="prop-card-title">${escapeHtml(title)}</span>
                <span class="prop-card-meta">
                    <span class="prop-card-template">${escapeHtml(templateName(templateId) || t("prop.card.template_fallback", "道具"))}</span>
                    <span class="prop-card-time">${escapeHtml(time)}</span>
                </span>
            </span>
            <span class="prop-card-open">${escapeHtml(t("prop.action.expand", "展开"))} <i class="fa fa-angle-right" aria-hidden="true"></i></span>
        </button>`;
    }

    async function hydrate(card: HTMLElement, propId: string): Promise<void> {
        const runtime = api();
        const roomId = String(global.currentRoom?.id || "");
        if (!runtime || !roomId) return;
        try {
            await runtime.templates(roomId).catch(() => [] as PropTemplate[]);
            const spec = specCache.get(propId) || await runtime.get(roomId, propId);
            specCache.set(propId, spec);
            const title = card.querySelector<HTMLElement>(".prop-card-title");
            if (title) title.textContent = resolveTitle(spec, title.textContent || "");
            const template = card.querySelector<HTMLElement>(".prop-card-template");
            if (template) template.textContent = templateName(spec.template_id);
            const seal = card.querySelector<HTMLElement>(".prop-card-seal i");
            if (seal) seal.className = runtime.templateIcon(spec.template_id, runtime.findTemplate(roomId, spec.template_id)?.category);
        } catch {
            card.classList.add("is-unavailable");
            const title = card.querySelector<HTMLElement>(".prop-card-title");
            if (title) title.textContent = t("prop.card.unavailable", "这件道具暂不可查看。");
        }
    }

    function render(message: ChatMessage): HTMLElement | null {
        const metadata = message.metadata || {};
        const propId = typeof metadata.prop_id === "string" ? metadata.prop_id : "";
        if (!propId || !isRecipient(metadata)) return null;

        const templateId = typeof metadata.preview === "string" ? metadata.preview : "";
        const displayTime = message.time || message.timestamp || "";
        const contentHtml = cardMarkup(
            propId,
            t("prop.card.untitled", "一件道具"),
            templateId,
            displayTime,
        );
        const html = global.TrpgTemplates.render("chat-message", {
            avatarSrc: message.avatar || "/assets/avatars/default_kp.jpg",
            sender: message.sender_name || message.sender || "KP",
            displayTime,
            contentHtml,
            processingHtml: "",
            knowledgeHtml: "",
        });
        const element = document.createElement("div");
        element.className = "message prop-message";
        if (message.id) element.setAttribute("data-id", String(message.id));
        element.innerHTML = html;
        const card = element.querySelector<HTMLElement>("[data-prop-card]");
        if (!card) return null;
        void hydrate(card, propId);
        return element;
    }

    function ensureViewer(): BootstrapModalInstance | null {
        const element = document.getElementById("propViewerModal");
        if (!element) return null;
        if (!viewer) viewer = new bootstrap.Modal(element);
        return viewer;
    }

    function openViewer(options: PropViewerOptions): void {
        const instance = ensureViewer();
        const element = document.getElementById("propViewerModal");
        if (!instance || !element) return;
        const runtime = api();
        const title = element.querySelector<HTMLElement>("#propViewerTitle");
        if (title) title.textContent = options.title || t("prop.card.untitled", "一件道具");
        const meta = element.querySelector<HTMLElement>("#propViewerTemplate");
        if (meta) {
            const name = options.templateName || templateName(options.templateId || "");
            meta.textContent = name || t("prop.card.template_fallback", "道具");
        }
        const stage = element.querySelector<HTMLElement>("#propViewerStage");
        stage?.classList.remove("is-zoomed");
        const image = element.querySelector<HTMLImageElement>("#propViewerImage");
        if (image) {
            image.classList.remove("is-broken");
            image.alt = options.title || "";
            image.src = runtime ? runtime.renderUrl(options.roomId, options.propId, "png", options.version ?? Date.now()) : "";
        }
        const pdf = element.querySelector<HTMLAnchorElement>("#propViewerPdf");
        if (pdf) {
            pdf.href = runtime ? runtime.renderUrl(options.roomId, options.propId, "pdf", options.version ?? Date.now()) : "#";
            pdf.target = "_blank";
            pdf.rel = "noopener";
        }
        instance.show();
    }

    function bind(): void {
        if (delegated) return;
        delegated = true;
        const handler = (event: Event): void => {
            const target = event.target as Element | null;
            const card = target?.closest<HTMLElement>("[data-prop-card]");
            if (!card) return;
            if (card.classList.contains("is-unavailable")) {
                global.showNotification?.(t("prop.card.unavailable", "这件道具暂不可查看。"), "warning");
                return;
            }
            const propId = card.dataset.propCard || "";
            const spec = specCache.get(propId);
            const roomId = String(global.currentRoom?.id || "");
            if (!propId || !roomId) return;
            openViewer({
                roomId,
                propId,
                title: spec?.title || card.querySelector<HTMLElement>(".prop-card-title")?.textContent || "",
                templateId: spec?.template_id || "",
                version: Date.now(),
            });
        };
        document.addEventListener("click", handler);
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            const target = event.target as Element | null;
            if (!target?.matches("[data-prop-card]")) return;
            event.preventDefault();
            handler(event);
        });

        const element = document.getElementById("propViewerModal");
        if (element) {
            element.querySelector<HTMLImageElement>("#propViewerImage")?.addEventListener("click", () => {
                element.querySelector<HTMLElement>("#propViewerStage")?.classList.toggle("is-zoomed");
            });
            element.querySelector<HTMLImageElement>("#propViewerImage")?.addEventListener("error", (event) => {
                const image = event.target as HTMLImageElement;
                image.classList.add("is-broken");
                global.showNotification?.(t("prop.error.render_failed", "渲染失败，请稍后重试。"), "error");
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bind);
    } else {
        bind();
    }

    global.TrpgPropCard = { render, openViewer };
})(window);
