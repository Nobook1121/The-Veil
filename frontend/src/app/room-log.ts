/**
 * 团录导出 PDF（设计稿 §6，P3 前端）。
 *
 * 依赖后端契约（设计稿 §5.2）：
 * - GET  /api/rooms/<room_id>/log/preview              → RoomLogDocument
 * - POST /api/rooms/<room_id>/log/exports              → { export_id, status }
 * - GET  /api/rooms/<room_id>/log/exports/<id>         → { status, progress, error, file }
 * - GET  /api/rooms/<room_id>/log/exports/<id>/file    → PDF（as_attachment）
 *
 * 后端尚在并行开发，接口 404 时只做可读降级，不抛未捕获异常。
 * 权限：房主 / 房管 / 站点管理员可见入口；上帝视角不是房间成员，同样隐藏。
 */

(function initializeRoomLog(global: Window): void {
    "use strict";

    const POLL_INTERVAL_MS = 800;
    const POLL_MAX_ATTEMPTS = 60;
    const PREVIEW_DEBOUNCE_MS = 300;
    /** 预览只渲染首章，限制条目数避免超长章节拖慢 iframe。 */
    const PREVIEW_ENTRY_LIMIT = 80;

    type RoomLogTemplate = "archive" | "letter" | "minimal";
    type JobStatus = "queued" | "running" | "done" | "failed" | "timeout";
    type StatusTone = "idle" | "running" | "success" | "error";
    type ExportBlockReason = "unavailable" | "forbidden" | "members_disabled" | "empty" | "font_missing" | null;

    interface RoomLogTemplateInfo {
        id?: string;
        name?: string;
        description?: string;
        available?: boolean;
        reason?: string | null;
    }

    interface RoomLogEntry {
        kind?: string;
        speaker?: string | null;
        character?: string | null;
        time?: string;
        html?: string;
        url?: string;
        caption?: string;
        roll?: { expr?: string; total?: number | string };
    }

    interface RoomLogChapter {
        index?: number;
        title?: string;
        subtitle?: string;
        started_at?: string;
        entries?: RoomLogEntry[];
    }

    interface RoomLogStats {
        messages_total?: number;
        messages_exported?: number;
        chapters?: number;
        dice_rolls?: number;
        images?: number;
        duration_days?: number;
        estimated_pages?: number;
    }

    interface RoomLogDocument {
        schema_version?: number;
        room?: { id?: string; name?: string; scenario_title?: string; creator_name?: string };
        stats?: RoomLogStats;
        chapters?: RoomLogChapter[];
        can_export?: boolean;
        can_export_reason?: string | null;
        templates?: RoomLogTemplateInfo[];
    }

    interface RoomLogOptions {
        template: RoomLogTemplate;
        include_kp_only: boolean;
        include_dice: boolean;
        include_ooc: boolean;
        include_images: boolean;
        date_from: string | null;
        date_to: string | null;
    }

    interface ExportJob {
        export_id?: string;
        id?: string;
        status?: string;
        progress?: number;
        error?: string | null;
        file?: { bytes?: number; pages?: number } | null;
    }

    interface JobState {
        roomId: string;
        exportId: string;
        status: JobStatus;
        progress: number;
        pages: number | null;
        bytes: number | null;
        errorCode: string;
    }

    let initialized = false;
    let activeRoomId = "";
    let previewDoc: RoomLogDocument | null = null;
    let jobState: JobState | null = null;
    let pollTimer: number | null = null;
    let pollAttempts = 0;
    let pollInFlight = false;
    let previewTimer: number | null = null;
    let previewInFlight = false;
    let pendingPreview = false;
    let lastPreviewSignature = "";
    let exportBlockReason: ExportBlockReason = null;
    let lastTemplates: RoomLogTemplateInfo[] | undefined;
    let entrySyncScheduled = false;

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

    function toFiniteNumber(value: unknown): number | null {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : null;
    }

    /**
     * 入口可见性：只要能看到房间就给出入口，是否可导出完全由 preview 的
     * can_export / can_export_reason 决定（普通成员由 members_can_export 控制）。
     * 上帝视角不是房间成员，仍然隐藏。
     */
    function canOpenExport(room: Room | null | undefined): boolean {
        return Boolean(room?.id) && room?.invisible_view !== true;
    }

    function syncEntryVisibility(): void {
        const button = byId<HTMLButtonElement>("roomLogExportBtn");
        if (!button) return;
        // 换房间后清掉上一个房间的禁用原因，避免入口一直灰着。
        const roomId = String(global.currentRoom?.id || "");
        if (roomId && activeRoomId && roomId !== activeRoomId) exportBlockReason = null;
        button.hidden = !canOpenExport(global.currentRoom);
        syncEntryBlocked(button);
    }

    /** 预览返回不可导出时，入口与生成按钮一起禁用，并带上原因。 */
    function syncEntryBlocked(button?: HTMLButtonElement | null): void {
        const target = button || byId<HTMLButtonElement>("roomLogExportBtn");
        if (!target) return;
        const blocked = exportBlockReason === "font_missing"
            || exportBlockReason === "members_disabled"
            || exportBlockReason === "forbidden";
        target.disabled = blocked;
        target.title = blocked ? blockMessage(exportBlockReason) : "";
        if (blocked) target.setAttribute("aria-disabled", "true");
        else target.removeAttribute("aria-disabled");
    }

    /** 房间详情由 rooms.ts 更新，这里只观察标题与详情容器的显隐，避免耦合其内部状态。 */
    function scheduleEntrySync(): void {
        if (entrySyncScheduled) return;
        entrySyncScheduled = true;
        window.setTimeout(() => {
            entrySyncScheduled = false;
            syncEntryVisibility();
        }, 0);
    }

    function setStatus(text: string, tone: StatusTone): void {
        const status = byId<HTMLElement>("roomLogStatus");
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("is-running", tone === "running");
        status.classList.toggle("is-success", tone === "success");
        status.classList.toggle("is-error", tone === "error");
    }

    function setStats(text: string): void {
        const stats = byId<HTMLElement>("roomLogStats");
        if (stats) stats.textContent = text;
    }

    function formatBytes(bytes: number): string {
        if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
        return `${Math.round(bytes)} B`;
    }

    function readDateInput(id: string): string | null {
        const value = byId<HTMLInputElement>(id)?.value?.trim() || "";
        return value || null;
    }

    function readCheckbox(id: string, fallback: boolean): boolean {
        const input = byId<HTMLInputElement>(id);
        return input ? input.checked : fallback;
    }

    function readOptions(): RoomLogOptions {
        const templateValue = byId<HTMLSelectElement>("roomLogTemplate")?.value || "archive";
        const template: RoomLogTemplate = templateValue === "letter" || templateValue === "minimal"
            ? templateValue
            : "archive";
        return {
            template,
            include_kp_only: readCheckbox("roomLogIncludeKpOnly", false),
            include_dice: readCheckbox("roomLogIncludeDice", true),
            include_ooc: readCheckbox("roomLogIncludeOoc", true),
            include_images: readCheckbox("roomLogIncludeImages", true),
            date_from: readDateInput("roomLogDateFrom"),
            date_to: readDateInput("roomLogDateTo"),
        };
    }

    function optionsQuery(options: RoomLogOptions): string {
        const params = new URLSearchParams();
        params.set("template", options.template);
        params.set("include_kp_only", options.include_kp_only ? "1" : "0");
        params.set("include_dice", options.include_dice ? "1" : "0");
        params.set("include_ooc", options.include_ooc ? "1" : "0");
        params.set("include_images", options.include_images ? "1" : "0");
        if (options.date_from) params.set("date_from", options.date_from);
        if (options.date_to) params.set("date_to", options.date_to);
        return params.toString();
    }

    function unwrapPayload<T extends object>(payload: unknown): T | null {
        if (!payload || typeof payload !== "object") return null;
        const record = payload as Record<string, unknown>;
        if (record.success === false) return null;
        const data = record.data;
        if (data && typeof data === "object") return data as T;
        return payload as T;
    }

    /** 兼容 {"error":"log.empty"} 与 {"error":{"code":"log.empty"}} 两种错误体。 */
    function extractErrorCode(payload: unknown): string {
        if (!payload || typeof payload !== "object") return "";
        const record = payload as Record<string, unknown>;
        const nested = record.error && typeof record.error === "object"
            ? record.error as Record<string, unknown>
            : null;
        const raw = nested?.code ?? record.error ?? record.code ?? "";
        return typeof raw === "string" ? raw : "";
    }

    function jobFailureCode(status: number): string {
        if (status === 403) return "log.forbidden";
        if (status === 404) return "log.export_not_found";
        if (status === 409) return "log.not_ready";
        if (status === 503) return "log.font_missing";
        return "log.render_failed";
    }

    /** can_export_reason → 面向用户的提示。 */
    function blockMessage(reason: ExportBlockReason): string {
        switch (reason) {
            case "font_missing":
                return t("roomLog.error.font_missing", "服务器缺少中文字体，暂时无法生成 PDF。");
            case "members_disabled":
                return t("roomLog.error.members_disabled", "当前房间未开放成员导出，请联系房主或房管。");
            case "forbidden":
                return t("roomLog.error.forbidden", "当前账号没有导出团录的权限。");
            case "empty":
                return t("roomLog.error.empty", "这个房间还没有可导出的消息。");
            case "unavailable":
                return t("roomLog.error.unavailable", "导出服务暂未就绪，请稍后再试。");
            default:
                return "";
        }
    }

    function localizedError(code: string): string {
        switch (code) {
            case "log.empty":
                return t("log.empty", blockMessage("empty"));
            case "log.font_missing":
                return t("log.font_missing", blockMessage("font_missing"));
            case "log.render_failed":
                return t("log.render_failed", "生成 PDF 时出错，请稍后重试。");
            case "log.forbidden":
            case "forbidden":
                return t("log.forbidden", blockMessage("forbidden"));
            case "log.invalid_option":
                return t("log.invalid_option", "导出选项不合法，请检查版式与日期范围。");
            case "log.export_not_found":
                return t("log.export_not_found", "导出记录不存在或已过期，请重新生成。");
            case "log.not_ready":
                return t("log.not_ready", "导出文件还没准备好，请稍后重试。");
            case "log.unavailable":
                return blockMessage("unavailable");
            case "log.invalid_response":
                return t("roomLog.error.invalid_response", "导出服务返回了无法识别的内容。");
            default:
                return t("roomLog.error.generic", "导出失败，请稍后重试。");
        }
    }

    function hasActiveJobStatus(): boolean {
        if (!jobState) return false;
        return jobState.status === "queued"
            || jobState.status === "running"
            || jobState.status === "done";
    }

    /** 任务状态优先于预览错误：已有任务结论时不覆盖底部状态。 */
    function setPreviewError(message: string): void {
        if (hasActiveJobStatus()) return;
        setStatus(message, "error");
    }

    function refreshExportButton(): void {
        const button = byId<HTMLButtonElement>("roomLogExport");
        if (!button) return;
        const busy = jobState !== null && (jobState.status === "queued" || jobState.status === "running");
        button.disabled = busy || exportBlockReason !== null;
        button.setAttribute("aria-busy", busy ? "true" : "false");
        syncEntryBlocked();
    }

    function exportStatusUrl(roomId: string, exportId: string): string {
        return `/api/rooms/${encodeURIComponent(roomId)}/log/exports/${encodeURIComponent(exportId)}`;
    }

    function exportFileUrl(roomId: string, exportId: string): string {
        return `${exportStatusUrl(roomId, exportId)}/file`;
    }

    function renderDoneStatus(state: JobState): void {
        const pages = state.pages && state.pages > 0 ? state.pages : 0;
        const size = state.bytes && state.bytes > 0 ? formatBytes(state.bytes) : "";
        if (pages && size) {
            setStatus(t("roomLog.status.done", "已生成 · {pages} 页 · {size}", { pages, size }), "success");
            return;
        }
        if (pages) {
            setStatus(t("roomLog.status.done_pages", "已生成 · {pages} 页", { pages }), "success");
            return;
        }
        setStatus(t("roomLog.status.done_plain", "已生成"), "success");
    }

    function syncJobUi(): void {
        const download = byId<HTMLAnchorElement>("roomLogDownload");
        if (!download) return;
        download.hidden = true;
        if (!jobState) {
            setStatus("", "idle");
            refreshExportButton();
            return;
        }
        switch (jobState.status) {
            case "queued":
                setStatus(t("roomLog.status.queued", "已排队…"), "running");
                break;
            case "running":
                if (jobState.progress > 0) {
                    setStatus(
                        t("roomLog.status.running_progress", "正在生成… {progress}%", { progress: jobState.progress }),
                        "running"
                    );
                } else {
                    setStatus(t("roomLog.status.running", "正在生成…"), "running");
                }
                break;
            case "done":
                download.hidden = false;
                download.href = exportFileUrl(jobState.roomId, jobState.exportId);
                renderDoneStatus(jobState);
                break;
            case "failed":
                setStatus(localizedError(jobState.errorCode), "error");
                break;
            case "timeout":
                setStatus(t("roomLog.error.timeout", "任务仍在后台生成，稍后重新打开这个窗口即可下载。"), "error");
                break;
        }
        refreshExportButton();
    }

    function renderStats(doc: RoomLogDocument | null): void {
        if (!doc) {
            setStats("—");
            return;
        }
        const stats = doc.stats || {};
        const messages = Math.max(0, Math.round(
            toFiniteNumber(stats.messages_exported ?? stats.messages_total) ?? 0
        ));
        const chapters = Math.max(0, Math.round(
            toFiniteNumber(stats.chapters ?? doc.chapters?.length) ?? 0
        ));
        const dice = Math.max(0, Math.round(toFiniteNumber(stats.dice_rolls) ?? 0));
        // 页数以后端统计为准，前端不再按「消息数 / 33 + 2」估算。
        const pages = Math.max(0, Math.round(toFiniteNumber(stats.estimated_pages) ?? 0));
        if (pages > 0) {
            setStats(t(
                "roomLog.stats.summary",
                "{messages} 条消息 · {chapters} 章 · {dice} 次骰子 · 约 {pages} 页",
                { messages, chapters, dice, pages }
            ));
            return;
        }
        setStats(t(
            "roomLog.stats.summary_no_pages",
            "{messages} 条消息 · {chapters} 章 · {dice} 次骰子",
            { messages, chapters, dice }
        ));
    }

    /** 版式下拉以后端 templates 为准；不可用项置灰并给出原因。 */
    function renderTemplates(templates: RoomLogTemplateInfo[] | undefined, preferred: string): void {
        const select = byId<HTMLSelectElement>("roomLogTemplate");
        if (!select || !Array.isArray(templates) || templates.length === 0) return;
        lastTemplates = templates;
        const current = preferred || select.value || "archive";
        select.textContent = "";
        templates.forEach((template) => {
            const id = String(template.id || "").trim();
            if (!id) return;
            const option = document.createElement("option");
            option.value = id;
            const name = String(template.name || id);
            const available = template.available !== false;
            const reasonText = template.reason === "font_missing"
                ? blockMessage("font_missing")
                : t("roomLog.template.unavailable", "暂不可用");
            option.textContent = available ? name : `${name}（${reasonText}）`;
            option.disabled = !available;
            if (!available) option.title = reasonText;
            select.appendChild(option);
        });
        const options = Array.from(select.options);
        const wanted = options.find((option) => option.value === current && !option.disabled)
            || options.find((option) => !option.disabled)
            || options[0];
        if (wanted) select.value = wanted.value;
    }

    function renderPreviewEntry(entry: RoomLogEntry): string {
        const html = typeof entry.html === "string" ? entry.html : "";
        const kind = entry.kind || "system";
        const time = escapeHtml(entry.time || "");
        const speaker = escapeHtml(entry.speaker || "");
        const character = escapeHtml(entry.character || "");
        switch (kind) {
            case "narration":
                return `<div class="entry narration">${html}</div>`;
            case "dialogue":
                return `<div class="entry dialogue">${html}</div>`;
            case "speaker": {
                const name = character || speaker;
                const meta = name || time
                    ? `<div class="entry-meta"><strong>${name}</strong><span>${time}</span></div>`
                    : "";
                return `<div class="entry speaker">${meta}${html}</div>`;
            }
            case "dice":
                return `<div class="entry dice">${html}</div>`;
            case "image": {
                if (!entry.url) {
                    return `<div class="entry system">${html || escapeHtml(entry.caption || "")}</div>`;
                }
                const caption = escapeHtml(entry.caption || "");
                return `<figure class="entry image"><img src="${escapeHtml(entry.url)}" alt="${caption}"><figcaption>${caption}</figcaption></figure>`;
            }
            default:
                return `<div class="entry system">${html || escapeHtml(entry.caption || "")}</div>`;
        }
    }

    function previewDocument(titleHtml: string, subtitleHtml: string, bodyHtml: string): string {
        return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: #ffffff; color: #1f2933; }
  body { font-family: "Noto Serif SC", "Songti SC", SimSun, Georgia, serif; font-size: 14px; line-height: 1.78; }
  .page { padding: 40px 46px 56px; }
  h1 { margin: 0 0 6px; font-size: 24px; font-weight: 600; letter-spacing: 0.02em; }
  .subtitle { margin: 0 0 26px; color: #697386; font-size: 13px; }
  .entry { margin: 0 0 12px; }
  .entry p { margin: 0 0 8px; }
  .entry.narration { color: #1c2b26; border-left: 3px solid #d8dee9; padding-left: 12px; }
  .entry.dialogue { color: #3a4a44; padding-left: 12px; border-left: 2px solid #e3e8ef; }
  .entry.speaker { padding-left: 10px; border-left: 2px solid #d8dee9; }
  .entry-meta { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 4px; color: #3c4a56; font-size: 12px; }
  .entry-meta span { color: #8a94a3; }
  .entry.dice { padding: 8px 12px; border-radius: 4px; background: #f2f0ea; color: #3a4a44; font-size: 13px; }
  .entry.system { color: #8a94a3; font-size: 12px; text-align: center; }
  .entry.image { margin: 16px 0; text-align: center; }
  .entry.image img { max-width: 100%; border-radius: 4px; }
  .entry.image figcaption { margin-top: 6px; color: #8a94a3; font-size: 12px; }
  .empty { margin: 0; padding: 48px 0; color: #8a94a3; text-align: center; }
  hr { border: 0; border-top: 1px solid #e3e8ef; margin: 20px 0; }
  blockquote { margin: 12px 0; padding-left: 12px; border-left: 3px solid #d8dee9; color: #3a4a44; }
  ul, ol { margin: 0 0 10px; padding-left: 22px; }
</style>
</head>
<body>
<article class="page">${bodyHtml ? `<h1>${titleHtml}</h1><p class="subtitle">${subtitleHtml}</p>${bodyHtml}` : `<p class="empty">${titleHtml}</p>`}</article>
</body>
</html>`;
    }

    function renderPreview(doc: RoomLogDocument): void {
        const frame = byId<HTMLIFrameElement>("roomLogPreviewFrame");
        if (!frame) return;
        // 防御性重设：预览始终运行在无脚本的沙箱文档里。
        frame.setAttribute("sandbox", "");
        const chapters = Array.isArray(doc.chapters) ? doc.chapters : [];
        const chapter = chapters.find((item) => Array.isArray(item.entries) && item.entries.length > 0) || chapters[0];
        if (!chapter) {
            frame.srcdoc = previewDocument(
                escapeHtml(t("roomLog.preview.empty", "暂无可预览的内容。")),
                "",
                ""
            );
            return;
        }
        const entries = (chapter.entries || []).slice(0, PREVIEW_ENTRY_LIMIT).map(renderPreviewEntry).join("");
        frame.srcdoc = previewDocument(
            escapeHtml(chapter.title || doc.room?.name || ""),
            escapeHtml(chapter.subtitle || ""),
            entries
        );
    }

    function resetPreviewFrame(): void {
        const frame = byId<HTMLIFrameElement>("roomLogPreviewFrame");
        if (frame) frame.srcdoc = "";
    }

    function applyPreviewFailure(status: number, payload: unknown, initial: boolean): void {
        const code = extractErrorCode(payload);
        if (code === "log.forbidden" || status === 403) {
            exportBlockReason = "forbidden";
            setPreviewError(blockMessage("forbidden"));
        } else if (status === 404 || status === 0) {
            // 后端尚未部署或网络中断：保留模态，给出可读提示并禁用生成。
            exportBlockReason = "unavailable";
            setPreviewError(blockMessage("unavailable"));
        } else if (code === "log.empty") {
            exportBlockReason = "empty";
            setPreviewError(blockMessage("empty"));
        } else if (code === "log.font_missing" || status === 503) {
            exportBlockReason = "font_missing";
            setPreviewError(blockMessage("font_missing"));
        } else if (code === "log.invalid_option") {
            exportBlockReason = "forbidden";
            setPreviewError(localizedError(code));
        } else if (code === "log.render_failed") {
            exportBlockReason = null;
            setPreviewError(localizedError(code));
        } else {
            exportBlockReason = null;
            setPreviewError(
                code
                    ? localizedError(code)
                    : t("roomLog.error.invalid_response", "导出服务返回了无法识别的内容。")
            );
        }
        if (initial) {
            previewDoc = null;
            setStats("—");
        }
        refreshExportButton();
    }

    /** preview 200 但仍不可导出：字体缺失 / 成员导出被关闭等。 */
    function applyPreviewBlocked(doc: RoomLogDocument, initial: boolean): boolean {
        if (doc.can_export !== false) {
            exportBlockReason = null;
            return false;
        }
        const reason = String(doc.can_export_reason || "");
        exportBlockReason = reason === "members_disabled"
            ? "members_disabled"
            : reason === "font_missing"
                ? "font_missing"
                : "forbidden";
        setPreviewError(blockMessage(exportBlockReason));
        if (initial) {
            previewDoc = doc;
            renderStats(doc);
            renderPreview(doc);
        }
        refreshExportButton();
        return true;
    }

    async function loadPreview(initial: boolean): Promise<void> {
        const room = global.currentRoom;
        if (!room?.id) return;
        const roomId = String(room.id);
        const options = readOptions();
        const signature = `${roomId}|${JSON.stringify(options)}`;
        if (previewInFlight) {
            pendingPreview = true;
            return;
        }
        if (signature === lastPreviewSignature) return;
        lastPreviewSignature = signature;
        previewInFlight = true;
        if (initial) setStats(t("roomLog.stats.loading", "正在统计…"));
        try {
            const { response, data } = await global.TrpgApi.requestWithResponse<unknown>(
                `/api/rooms/${encodeURIComponent(roomId)}/log/preview?${optionsQuery(options)}`,
                { method: "GET" }
            );
            if (activeRoomId !== roomId) return;
            if (!response.ok) {
                applyPreviewFailure(response.status, data, initial);
                return;
            }
            const doc = unwrapPayload<RoomLogDocument>(data);
            if (!doc || !Array.isArray(doc.chapters)) {
                applyPreviewFailure(200, null, initial);
                return;
            }
            previewDoc = doc;
            const preferred = byId<HTMLSelectElement>("roomLogTemplate")?.value || "archive";
            renderTemplates(doc.templates, preferred);
            renderStats(doc);
            // 200 也可能 can_export=false（字体缺失 / 成员导出关闭），统一按返回值禁用。
            const blocked = applyPreviewBlocked(doc, initial);
            if (initial && !blocked) {
                renderPreview(doc);
                if (!jobState) setStatus("", "idle");
            }
            if (!blocked) exportBlockReason = null;
            refreshExportButton();
        } catch {
            if (activeRoomId === roomId) applyPreviewFailure(0, null, initial);
        } finally {
            previewInFlight = false;
            if (pendingPreview) {
                pendingPreview = false;
                void loadPreview(false);
            }
        }
    }

    function schedulePreviewRefresh(): void {
        if (exportBlockReason !== null) return;
        if (previewTimer !== null) window.clearTimeout(previewTimer);
        previewTimer = window.setTimeout(() => {
            previewTimer = null;
            void loadPreview(false);
        }, PREVIEW_DEBOUNCE_MS);
    }

    function normalizeJobStatus(value: unknown): JobStatus {
        const status = String(value || "");
        if (status === "done" || status === "failed" || status === "running") return status;
        return "queued";
    }

    function stopPolling(): void {
        if (pollTimer !== null) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
        pollInFlight = false;
    }

    function failJob(code: string): void {
        stopPolling();
        if (!jobState) return;
        jobState.status = "failed";
        jobState.errorCode = code || "log.render_failed";
        syncJobUi();
    }

    function applyJob(job: ExportJob | null): void {
        if (!jobState || !job) return;
        const status = normalizeJobStatus(job.status);
        jobState.status = status;
        const progress = toFiniteNumber(job.progress);
        if (progress !== null) {
            jobState.progress = Math.max(0, Math.min(100, Math.round(progress)));
        }
        if (status === "done") {
            const pages = toFiniteNumber(job.file?.pages);
            const bytes = toFiniteNumber(job.file?.bytes);
            jobState.pages = pages === null ? null : Math.round(pages);
            jobState.bytes = bytes === null ? null : Math.round(bytes);
            stopPolling();
        } else if (status === "failed") {
            jobState.errorCode = String(job.error || "log.render_failed");
            stopPolling();
        }
        syncJobUi();
    }

    async function pollOnce(): Promise<void> {
        const state = jobState;
        if (!state || !state.exportId || pollInFlight) return;
        if (pollAttempts >= POLL_MAX_ATTEMPTS) {
            stopPolling();
            state.status = "timeout";
            syncJobUi();
            return;
        }
        pollAttempts += 1;
        pollInFlight = true;
        try {
            const { response, data } = await global.TrpgApi.requestWithResponse<unknown>(
                exportStatusUrl(state.roomId, state.exportId),
                { method: "GET" }
            );
            if (!jobState || jobState.exportId !== state.exportId) return;
            if (!response.ok) {
                if (response.status === 403) exportBlockReason = "forbidden";
                if (response.status === 503) exportBlockReason = "font_missing";
                failJob(jobFailureCode(response.status));
                return;
            }
            applyJob(unwrapPayload<ExportJob>(data));
        } catch {
            // 网络抖动：保留任务，继续轮询直到上限。
        } finally {
            pollInFlight = false;
        }
    }

    function startPolling(): void {
        if (!jobState) return;
        if (jobState.status === "done" || jobState.status === "failed" || jobState.status === "timeout") return;
        stopPolling();
        pollAttempts = 0;
        pollTimer = window.setInterval(() => { void pollOnce(); }, POLL_INTERVAL_MS);
        void pollOnce();
    }

    async function startExport(): Promise<void> {
        const room = global.currentRoom;
        const button = byId<HTMLButtonElement>("roomLogExport");
        if (!room?.id || !button || button.disabled) return;
        const roomId = String(room.id);
        stopPolling();
        const state: JobState = {
            roomId,
            exportId: "",
            status: "queued",
            progress: 0,
            pages: null,
            bytes: null,
            errorCode: "",
        };
        jobState = state;
        syncJobUi();
        try {
            const { response, data } = await global.TrpgApi.requestWithResponse<unknown>(
                `/api/rooms/${encodeURIComponent(roomId)}/log/exports`,
                { method: "POST", body: readOptions() }
            );
            if (!response.ok) {
                const code = extractErrorCode(data);
                if (response.status === 403) exportBlockReason = "forbidden";
                if (response.status === 503) exportBlockReason = "font_missing";
                state.status = "failed";
                state.errorCode = code || jobFailureCode(response.status);
                syncJobUi();
                return;
            }
            const created = unwrapPayload<ExportJob>(data);
            const exportId = String(created?.export_id || created?.id || "");
            if (!exportId) {
                state.status = "failed";
                state.errorCode = "log.invalid_response";
                syncJobUi();
                return;
            }
            state.exportId = exportId;
            state.status = normalizeJobStatus(created?.status);
            syncJobUi();
            startPolling();
        } catch {
            state.status = "failed";
            state.errorCode = "log.unavailable";
            syncJobUi();
        }
    }

    function openModal(): void {
        const room = global.currentRoom;
        if (!room?.id || !canOpenExport(room)) return;
        const roomId = String(room.id);
        if (activeRoomId !== roomId) {
            activeRoomId = roomId;
            previewDoc = null;
            jobState = null;
            exportBlockReason = null;
            resetPreviewFrame();
            setStats("—");
        }
        // 重新打开时允许重试预览（上次可能是后端未就绪或网络抖动）。
        lastPreviewSignature = "";
        const modalElement = byId<HTMLElement>("roomLogExportModal");
        if (!modalElement) return;
        const modal = bootstrap.Modal.getInstance(modalElement) || new bootstrap.Modal(modalElement);
        modal.show();
        syncJobUi();
        if (!jobState) {
            setStatus(t("roomLog.status.loading", "正在读取团录信息…"), "running");
        }
        void loadPreview(true);
        // 轮询超时后重新打开：继续跟进同一个任务，用户才能拿到“稍后可下载”的产物。
        if (jobState?.status === "timeout") {
            jobState.status = "running";
            syncJobUi();
        }
        if (jobState && (jobState.status === "queued" || jobState.status === "running")) {
            startPolling();
        }
    }

    function bindInfoToggle(): void {
        const toggle = byId<HTMLButtonElement>("roomLogInfoToggle");
        const note = byId<HTMLElement>("roomLogInfoNote");
        if (!toggle || !note) return;
        toggle.addEventListener("click", () => {
            const open = note.hidden;
            note.hidden = !open;
            toggle.setAttribute("aria-expanded", open ? "true" : "false");
        });
    }

    function bindOptionInputs(): void {
        const ids = [
            "roomLogTemplate",
            "roomLogIncludeKpOnly",
            "roomLogIncludeDice",
            "roomLogIncludeOoc",
            "roomLogIncludeImages",
            "roomLogDateFrom",
            "roomLogDateTo",
        ];
        ids.forEach((id) => {
            byId<HTMLElement>(id)?.addEventListener("change", schedulePreviewRefresh);
        });
    }

    function bindActions(): void {
        byId<HTMLButtonElement>("roomLogExportBtn")?.addEventListener("click", openModal);
        byId<HTMLButtonElement>("roomLogExport")?.addEventListener("click", () => { void startExport(); });
    }

    function bindRoomDetailObserver(): void {
        const detail = byId<HTMLElement>("save-detail-view");
        if (!detail) return;
        const observer = new MutationObserver(scheduleEntrySync);
        observer.observe(detail, { attributes: true, attributeFilter: ["style"] });
        const title = byId<HTMLElement>("saveDetailTitle");
        if (title) observer.observe(title, { childList: true, characterData: true, subtree: true });
    }

    function handleLocaleChange(): void {
        syncEntryVisibility();
        if (previewDoc) renderStats(previewDoc);
        // 版式下拉里含「不可用原因」，切语言后按最近一次返回值重绘。
        if (lastTemplates) {
            renderTemplates(lastTemplates, byId<HTMLSelectElement>("roomLogTemplate")?.value || "archive");
        }
        // 无任务时保留预览错误文案，避免切语言把提示清空。
        if (jobState) syncJobUi();
    }

    function init(): void {
        if (initialized) return;
        initialized = true;
        bindActions();
        bindOptionInputs();
        bindInfoToggle();
        bindRoomDetailObserver();
        syncEntryVisibility();
        global.addEventListener("trpg:locale-changed", handleLocaleChange);
        global.addEventListener("trpg:user-changed", syncEntryVisibility);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
