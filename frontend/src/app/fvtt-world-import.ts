/**
 * FVTT 世界包导入弹层（M5 收官：前端 UI 胶水）。
 *
 * 同一套「选择 zip → 审计预览 → 落库 → 结果摘要」的 Bootstrap 弹窗服务两个入口：
 * - 剧本管理页（登记台）：POST /scenario/import 自动建剧本（幂等：同 world id
 *   重导原位升版），成功后提供「用此剧本开团」（复用现有
 *   window.openCreateRoomWithScenario，剧本预填的建房弹窗）。
 * - 房间详情页（DM 视图）：勾选若干步后依次调 rooms/<id>/run（场景→地图）、
 *   rooms/<id>/actors/run（角色卡+NPC）、rooms/<id>/journal/run（场景文本+世界树）。
 *
 * 后端契约见 data/plugins/fvtt-world-import/entry.py（全部已实现并有测试）；
 * 本文件只做前端接线，不引入新依赖，样式沿用页面的 Bootstrap 体系。
 */

const FVTT_IMPORT_BASE = "/api/plugins/fvtt-world-import";

interface FvttWorldAudit {
    id?: string;
    title?: string;
    version?: string;
    system?: string;
    /** 预估剧本条目（登记台）：journal 可转换页 ≈ 场景文本模块数。 */
    scenario_would_create?: { title?: string; version?: string; modules?: number };
    scenes?: number;
    convertible_scenes?: number;
    tokens?: number;
    walls?: number;
    lights?: number;
    characters?: number;
    npcs?: number;
    vehicles?: number;
    journals?: number;
    journal_pages?: number;
    journal_convertible?: number;
    warnings?: string[];
}

interface FvttScenarioImportResult {
    scenario_id: number;
    title: string;
    version: string;
    /** false = 同源剧本原位升版（幂等重导）。 */
    created?: boolean;
    counts?: Record<string, number>;
    narrative?: { written?: boolean; nodes?: number; edges?: number };
    warnings?: string[];
    world?: { scenes?: number; characters?: number; npcs?: number };
}

interface FvttRoomScenesResult {
    scenes?: Array<{ title?: string; walls?: number; lights?: number; tokens?: number }>;
    skipped?: number;
    warnings?: string[];
    totals?: Record<string, number>;
}

interface FvttRoomActorsResult {
    characters?: unknown[];
    npcs?: unknown[];
    vehicles?: unknown[];
    warnings?: string[];
    totals?: Record<string, number>;
}

interface FvttRoomJournalResult {
    journals?: unknown[];
    warnings?: string[];
    totals?: Record<string, number>;
    scenario?: { narrative_nodes?: number; narrative_edges?: number };
}

interface FvttRoomImportTarget {
    id: string;
    title: string;
}

function fvttEscape(value: unknown): string {
    return window.TrpgTemplates ? window.TrpgTemplates.escapeHtml(value) : String(value ?? "");
}

function fvttCount(value: unknown): number {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function fvttErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

/** multipart 上传世界包；后端返回 {success, message, data}，失败抛可读中文错误。 */
async function fvttPostForm<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
    const form = new FormData();
    form.append("file", file, file.name);
    Object.entries(fields || {}).forEach(([key, value]) => form.append(key, value));
    const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<T>>(FVTT_IMPORT_BASE + path, {
        method: "POST",
        body: form,
    });
    if (!response.ok || !data.success || data.data === undefined || data.data === null) {
        throw new Error(data.message || data.error || `请求失败（HTTP ${response.status}）`);
    }
    return data.data;
}

function fvttWarningList(warnings: string[]): string {
    const rows = warnings.map((item) => `<li>${fvttEscape(item)}</li>`).join("");
    return `<ul class="small text-muted mb-0 fvtt-warning-list">${rows}</ul>`;
}

/** 单例弹窗：剧本管理页与房间详情页共用，任何时候只开一个。 */
function openFvttWorldImportModal(mode: "scenario" | "room", room?: FvttRoomImportTarget): void {
    if (!window.currentUser) {
        showNotification("请先登录后再导入世界包", "warning");
        AuthModule.showAuthModal();
        return;
    }
    const isRoom = mode === "room";
    if (isRoom && !room) return;

    const modal = document.createElement("div");
    modal.className = "modal fade";
    modal.innerHTML = `<div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content">
        <div class="modal-header">
            <h5 class="modal-title">${isRoom ? `导入世界包到此房间：${fvttEscape(room?.title || "")}` : "导入世界包（Foundry VTT）"}</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
        </div>
        <div class="modal-body">
            <div data-fvtt-stage="pick">
                <p class="text-muted small mb-2">支持 Foundry VTT v10+ 世界包 zip（含 LevelDB 数据）。先审计预览，确认后才写入。</p>
                <input type="file" class="form-control" accept=".zip,application/zip,application/x-zip-compressed" data-fvtt-file>
                <p class="form-text mb-0">${isRoom
                    ? "数据去向：场景挂房间地图，角色卡落你的卡库，journal 写入房间绑定剧本的场景文本与世界树。各步独立勾选。"
                    : "数据去向：journal 直接产出剧本文本与世界树图谱；场景与角色卡是房间运行时数据，开团后在房间页「导入世界包」一步落地。"}</p>
            </div>
            <div data-fvtt-stage="busy" hidden>
                <div class="d-flex align-items-center gap-2 mb-2">
                    <div class="spinner-border spinner-border-sm text-primary" role="status" aria-hidden="true"></div>
                    <span data-fvtt-busy-text>正在审计世界包…</span>
                </div>
                <ul class="list-unstyled small text-muted mb-0" data-fvtt-busy-log></ul>
            </div>
            <div data-fvtt-stage="preview" hidden></div>
            <div data-fvtt-stage="result" hidden></div>
        </div>
        <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-fvtt-restart hidden>重新选择文件</button>
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">关闭</button>
            <button type="button" class="btn btn-primary" data-fvtt-run hidden>${isRoom ? "开始导入" : "导入为剧本"}</button>
            <button type="button" class="btn btn-primary" data-fvtt-open-room hidden>用此剧本开团</button>
        </div>
    </div></div>`;
    document.body.appendChild(modal);
    const instance = new bootstrap.Modal(modal);
    modal.addEventListener("hidden.bs.modal", () => modal.remove());

    const stages = Array.from(modal.querySelectorAll<HTMLElement>("[data-fvtt-stage]"));
    const busyText = modal.querySelector<HTMLElement>("[data-fvtt-busy-text]");
    const busyLog = modal.querySelector<HTMLElement>("[data-fvtt-busy-log]");
    const previewStage = stages.find((stage) => stage.dataset.fvttStage === "preview");
    const resultStage = stages.find((stage) => stage.dataset.fvttStage === "result");
    const fileInput = modal.querySelector<HTMLInputElement>("[data-fvtt-file]");
    const runButton = modal.querySelector<HTMLButtonElement>("[data-fvtt-run]");
    const restartButton = modal.querySelector<HTMLButtonElement>("[data-fvtt-restart]");
    const openRoomButton = modal.querySelector<HTMLButtonElement>("[data-fvtt-open-room]");

    const showStage = (name: "pick" | "busy" | "preview" | "result"): void => {
        stages.forEach((stage) => {
            stage.hidden = stage.dataset.fvttStage !== name;
        });
    };
    const setFooter = (state: "pick" | "busy" | "preview" | "result"): void => {
        if (restartButton) restartButton.hidden = state !== "result";
        if (runButton) {
            runButton.hidden = state !== "preview";
            runButton.disabled = state === "busy";
        }
        if (openRoomButton) openRoomButton.hidden = state !== "result" || isRoom;
    };
    const inlineError = (message: string): void => {
        if (!previewStage) return;
        const host = previewStage.querySelector<HTMLElement>("[data-fvtt-error]");
        if (host) {
            host.hidden = false;
            host.textContent = message;
        }
    };

    let zipFile: File | null = null;
    let audit: FvttWorldAudit | null = null;
    let scenarioId = 0;

    /** 渲染审计预览卡：世界摘要 + 计数 + （房间模式）分步勾选。 */
    const renderPreview = (): void => {
        if (!previewStage || !audit) return;
        const report = audit;
        const rows: Array<[string, string]> = [
            ["场景", `${fvttCount(report.scenes)} 个（可导入 ${fvttCount(report.convertible_scenes)} 个）`],
            ["角色 / NPC / 载具", `${fvttCount(report.characters)} / ${fvttCount(report.npcs)} / ${fvttCount(report.vehicles)}`],
            ["journal", `${fvttCount(report.journals)} 条 · ${fvttCount(report.journal_pages)} 页（可转换 ${fvttCount(report.journal_convertible)} 页）`],
        ];
        if (isRoom) {
            rows.push(["落库目标", `房间「${fvttEscape(room?.title || "")}」绑定的剧本与地图`]);
        } else {
            rows.push([
                "预估剧本",
                `${fvttEscape(report.scenario_would_create?.title || report.title || "未命名")}（v${fvttEscape(report.scenario_would_create?.version || report.version || "1.0.0")}，约 ${fvttCount(report.scenario_would_create?.modules)} 条场景文本）`,
            ]);
        }
        const stepsHtml = isRoom
            ? `<div class="mb-3" data-fvtt-steps>
                <div class="form-check"><input class="form-check-input" type="checkbox" id="fvttStepScenes" data-fvtt-step="scenes"><label class="form-check-label" for="fvttStepScenes">场景（含墙体 / 光源 / 棋子）→ 房间地图</label></div>
                <div class="form-check"><input class="form-check-input" type="checkbox" id="fvttStepActors" data-fvtt-step="actors"><label class="form-check-label" for="fvttStepActors">角色卡 + NPC 模块 → 我的角色卡库 / 剧本</label></div>
                <div class="form-check"><input class="form-check-input" type="checkbox" id="fvttStepJournal" data-fvtt-step="journal"><label class="form-check-label" for="fvttStepJournal">journal → 剧本场景文本 + 世界树图谱</label></div>
                <p class="form-text mb-0">勾选哪几步就执行哪几步；各步独立成败，之后可再导入补齐。</p>
            </div>`
            : "";
        const warnings = (report.warnings || []).map((item) => String(item)).filter(Boolean);
        previewStage.innerHTML = `
            <div class="card mb-3">
                <div class="card-body py-3">
                    <h5 class="card-title mb-1">「${fvttEscape(report.title || report.id || zipFile?.name || "未命名世界")}」
                        <span class="badge bg-secondary align-middle ms-1">v${fvttEscape(report.version || "?")}</span></h5>
                    <p class="text-muted small mb-2">world id：${fvttEscape(report.id || "-")}${report.system ? ` · system：${fvttEscape(report.system)}` : ""} · ${fvttEscape(zipFile?.name || "")}</p>
                    ${rows.map(([label, value]) => `<div class="row small align-items-center py-1"><div class="col-4 text-muted">${label}</div><div class="col-8">${value}</div></div>`).join("")}
                </div>
            </div>
            ${stepsHtml}
            ${warnings.length ? `<details class="mb-3"><summary class="small text-muted">警告（${warnings.length} 条，不阻断导入）</summary>${fvttWarningList(warnings)}</details>` : ""}
            <div class="alert alert-danger small mb-0" data-fvtt-error hidden></div>`;
        if (isRoom) {
            const scenes = previewStage.querySelector<HTMLInputElement>("[data-fvtt-step='scenes']");
            const actors = previewStage.querySelector<HTMLInputElement>("[data-fvtt-step='actors']");
            const journal = previewStage.querySelector<HTMLInputElement>("[data-fvtt-step='journal']");
            if (scenes) {
                scenes.checked = fvttCount(report.convertible_scenes) > 0;
                scenes.disabled = fvttCount(report.scenes) === 0;
            }
            if (actors) {
                actors.checked = fvttCount(report.characters) + fvttCount(report.npcs) > 0;
                actors.disabled = fvttCount(report.characters) + fvttCount(report.npcs) === 0;
            }
            if (journal) {
                journal.checked = fvttCount(report.journal_convertible) > 0;
                journal.disabled = fvttCount(report.journals) === 0;
            }
        }
    };

    const inspectZip = async (): Promise<void> => {
        if (!zipFile) return;
        showStage("busy");
        setFooter("busy");
        if (busyText) busyText.textContent = `正在审计世界包「${zipFile.name}」…`;
        if (busyLog) busyLog.innerHTML = "";
        try {
            audit = await fvttPostForm<FvttWorldAudit>("/inspect", zipFile);
            renderPreview();
            showStage("preview");
            setFooter("preview");
        } catch (error) {
            showStage("pick");
            setFooter("pick");
            showNotification(`世界包审计失败：${fvttErrorMessage(error)}`, "error");
        }
    };

    const runScenarioImport = async (): Promise<void> => {
        if (!zipFile) return;
        showStage("busy");
        setFooter("busy");
        if (busyText) busyText.textContent = "正在导入为剧本…";
        try {
            const result = await fvttPostForm<FvttScenarioImportResult>("/scenario/import", zipFile);
            scenarioId = fvttCount(result.scenario_id);
            // 剧本库可能新增/升版条目，先重载列表再展示结果卡。
            window.refreshScenarioManagement?.();
            const warnings = (result.warnings || []).map((item) => String(item)).filter(Boolean);
            const counts = result.counts || {};
            const narrative = result.narrative || {};
            const narrativeLine = narrative.written !== false
                ? `场景文本 ${fvttCount(counts.modules)} 条 · 世界树图谱 ${fvttCount(narrative.nodes)} 节点 / ${fvttCount(narrative.edges)} 边`
                : "journal 为空库或集合缺席，未产出场景文本（剧本条目已生成，可正常开团）";
            if (resultStage) {
                resultStage.innerHTML = `
                    <div class="alert ${result.created === false ? "alert-warning" : "alert-success"} py-2 small mb-2">
                        <div class="mb-1"><strong>${result.created === false ? "已更新剧本（同源世界包，原位升版）" : "已创建剧本"}</strong>「${fvttEscape(result.title)}」v${fvttEscape(result.version)}</div>
                        <div class="mb-1">${narrativeLine}</div>
                        ${result.created === false ? `<div class="mb-0 text-muted">幂等：同一世界包重复导入会定位既有剧本升版覆盖，不会产生重复剧本。</div>` : ""}
                    </div>
                    <p class="small text-muted mb-2">包内还有 ${fvttCount(result.world?.scenes)} 个场景、${fvttCount(result.world?.characters)} 张角色卡、${fvttCount(result.world?.npcs)} 个 NPC——这些是房间运行时数据，开团后在房间页点「导入世界包」即可挂进房间。</p>
                    ${warnings.length ? `<details class="mb-1"><summary class="small text-muted">警告（${warnings.length} 条）</summary>${fvttWarningList(warnings)}</details>` : ""}`;
            }
            showStage("result");
            setFooter("result");
            showNotification(result.created === false
                ? `剧本已原位升版：${result.title}（v${result.version}）`
                : `剧本已创建：${result.title}（v${result.version}）`, "success");
        } catch (error) {
            showStage("preview");
            setFooter("preview");
            inlineError(`导入失败：${fvttErrorMessage(error)}`);
            showNotification(`世界包导入失败：${fvttErrorMessage(error)}`, "error");
        }
    };

    const runRoomImport = async (): Promise<void> => {
        if (!zipFile || !room) return;
        const steps = Array.from(modal.querySelectorAll<HTMLInputElement>("[data-fvtt-step]"))
            .filter((input) => input.checked)
            .map((input) => String(input.dataset.fvttStep || ""));
        if (steps.length === 0) {
            inlineError("请至少勾选一个导入步骤。");
            return;
        }
        const labels: Record<string, string> = {
            scenes: "场景",
            actors: "角色卡与 NPC",
            journal: "journal 场景文本",
        };
        const summaryRows: string[] = [];
        const warnings: string[] = [];
        const roomId = encodeURIComponent(room.id);
        showStage("busy");
        setFooter("busy");
        const log = (text: string): void => {
            if (busyText) busyText.textContent = text;
            if (busyLog) {
                busyLog.innerHTML = summaryRows.map((row) => `<li>${row}</li>`).join("");
            }
        };
        try {
            for (const step of steps) {
                log(`正在导入${labels[step] || step}…`);
                if (step === "scenes") {
                    const data = await fvttPostForm<FvttRoomScenesResult>(`/rooms/${roomId}/run`, zipFile);
                    const totals = data.totals || {};
                    summaryRows.push(`✓ 场景：${fvttCount(totals.scenes)} 个（墙 ${fvttCount(totals.walls)} / 光 ${fvttCount(totals.lights)} / 棋子 ${fvttCount(totals.tokens)}${fvttCount(totals.skipped) ? `，跳过 ${fvttCount(totals.skipped)} 个` : ""}）`);
                    warnings.push(...(data.warnings || []).map((item) => String(item)));
                } else if (step === "actors") {
                    const data = await fvttPostForm<FvttRoomActorsResult>(`/rooms/${roomId}/actors/run`, zipFile);
                    summaryRows.push(`✓ 角色卡：${(data.characters || []).length} 张 · NPC 模块：${(data.npcs || []).length} 个`);
                    warnings.push(...(data.warnings || []).map((item) => String(item)));
                } else if (step === "journal") {
                    const data = await fvttPostForm<FvttRoomJournalResult>(`/rooms/${roomId}/journal/run`, zipFile);
                    const nodes = fvttCount(data.scenario?.narrative_nodes);
                    const edges = fvttCount(data.scenario?.narrative_edges);
                    summaryRows.push(`✓ 场景文本：${(data.journals || []).length} 条${nodes || edges ? `（图谱 ${nodes} 节点 / ${edges} 边）` : ""}`);
                    warnings.push(...(data.warnings || []).map((item) => String(item)));
                }
            }
            const uniqueWarnings = warnings.filter(Boolean);
            if (resultStage) {
                resultStage.innerHTML = `
                    <div class="alert alert-success py-2 small mb-2"><div class="mb-1"><strong>世界包已导入到房间「${fvttEscape(room.title)}」</strong></div>
                        ${summaryRows.map((row) => `<div>${fvttEscape(row)}</div>`).join("")}
                    </div>
                    ${uniqueWarnings.length ? `<details><summary class="small text-muted">警告（${uniqueWarnings.length} 条）</summary>${fvttWarningList(uniqueWarnings)}</details>` : ""}`;
            }
            showStage("result");
            setFooter("result");
            showNotification("世界包已导入到房间", "success");
        } catch (error) {
            const message = fvttErrorMessage(error);
            if (resultStage) {
                resultStage.innerHTML = `
                    <div class="alert alert-danger py-2 small mb-2"><div class="mb-1"><strong>导入中断：${fvttEscape(message)}</strong></div>
                        <div class="text-muted">各步独立落库：已完成的步骤保留在房间与卡库，重新导入同一世界包即可补齐剩余步骤。</div>
                    </div>
                    ${summaryRows.length ? `<div class="small mb-2">${summaryRows.map((row) => `<div>${fvttEscape(row)}</div>`).join("")}</div>` : ""}`;
            }
            showStage("result");
            setFooter("result");
            showNotification(`世界包导入失败：${message}`, "error");
        }
    };

    fileInput?.addEventListener("change", (event) => {
        const input = event.target as HTMLInputElement | null;
        const file = input?.files?.[0] || null;
        if (!file) return;
        if (!file.name.toLowerCase().endsWith(".zip")) {
            showNotification("请选择 Foundry VTT 世界包 zip 文件", "error");
            fileInput.value = "";
            return;
        }
        zipFile = file;
        void inspectZip();
    });

    runButton?.addEventListener("click", () => {
        if (!zipFile) return;
        if (isRoom) void runRoomImport();
        else void runScenarioImport();
    });

    restartButton?.addEventListener("click", () => {
        zipFile = null;
        audit = null;
        if (fileInput) fileInput.value = "";
        showStage("pick");
        setFooter("pick");
    });

    openRoomButton?.addEventListener("click", () => {
        instance.hide();
        if (scenarioId > 0) void window.openCreateRoomWithScenario?.(scenarioId);
    });

    showStage("pick");
    setFooter("pick");
    instance.show();
}

/** 剧本管理页入口：世界包 → 登记台自动建剧本（幂等升版）。 */
function importWorldPackageAsScenario(): void {
    openFvttWorldImportModal("scenario");
}

/** 房间详情页（DM 视图）入口：世界包 → 场景 / 角色 / journal 依次落库到房间。 */
function importWorldPackageToRoom(roomId: string, roomTitle: string): void {
    if (!roomId) return;
    openFvttWorldImportModal("room", { id: roomId, title: roomTitle });
}

window.TrpgWorldImport = {
    importWorldPackageAsScenario,
    importWorldPackageToRoom,
};

/* ------------------------------------------------------------------ *
 * 进房自动装配（auto-provision）
 *
 * 从世界包剧本开房后，房主/房管首次进房静默调一次
 * POST /rooms/<id>/auto-provision：世界包的初始地图（墙/光/棋子）与
 * NPC 模块自动落库，全员经 veil-map 广播看到地图。后端幂等（装配过/
 * 房间已有场景 → skipped 零写入），玩家端不触发；失败静默，手动
 * 「导入世界包」入口仍可兜底。
 * ------------------------------------------------------------------ */

interface FvttAutoProvisionResult {
    status?: string;
    reason?: string;
    scenes?: number;
    npcs?: number;
}

const fvttAutoProvisionTried = new Set<string>();

function fvttRoomAllowsAutoProvision(room: unknown): boolean {
    const target = room as
        | {
              id?: string | number;
              ruleset_id?: string;
              scenario_id?: number;
              archived?: boolean;
              creator_id?: string | number;
              members?: Array<{ user_id?: string | number; room_role?: string }>;
          }
        | null;
    if (!target || target.id === undefined || target.id === null) return false;
    if (String(target.ruleset_id || "").toLowerCase() !== "dnd5e") return false;
    if (!target.scenario_id || target.archived) return false;
    // 与后端口径一致：房主/房管/站点管理员；玩家不触发（后端仍最终把关）。
    const role = String(window.currentUser?.role || "");
    if (role === "ADMIN" || role === "OWNER") return true;
    const userId = String(window.currentUser?.user_id ?? "");
    const member = (target.members || []).find((entry) => String(entry.user_id ?? "") === userId);
    return Boolean(member && (member.room_role === "owner" || member.room_role === "admin"))
        || String(target.creator_id ?? "") === userId;
}

async function fvttAutoProvisionRoom(room: unknown): Promise<void> {
    const target = room as { id?: string | number } | null;
    if (!target || target.id === undefined || target.id === null) return;
    if (!fvttRoomAllowsAutoProvision(room)) return;
    const roomId = String(target.id);
    if (fvttAutoProvisionTried.has(roomId)) return;
    fvttAutoProvisionTried.add(roomId);
    try {
        const { data } = await TrpgApi.requestWithResponse<ApiResponse<FvttAutoProvisionResult>>(
            `${FVTT_IMPORT_BASE}/rooms/${encodeURIComponent(roomId)}/auto-provision`,
            { method: "POST" },
        );
        const payload = data && data.success ? (data.data as FvttAutoProvisionResult | null) : null;
        if (payload && payload.status === "provisioned") {
            showNotification(
                `已自动载入世界地图与 NPC（场景 ${fvttCount(payload.scenes)} 个 · NPC ${fvttCount(payload.npcs)} 个）`,
                "success",
            );
        }
    } catch {
        // 静默降级：不打扰进房体验，房主可用房间页「导入世界包」手动兜底。
    }
}

window.addEventListener("trpg:room-changed", (event) => {
    const detail = (event as CustomEvent).detail as { room?: unknown } | undefined;
    if (detail && detail.room) void fvttAutoProvisionRoom(detail.room);
});
