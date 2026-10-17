// 剧本通用助手：国际化、HTML 转义、权限判断、弹窗与通用表单工具。

// 剧本封面地址：合法的上传封面直接用，否则回退为「剧本名」文字封面。
function safeScenarioCover(title: string, cover?: string): string {
    if (cover && (cover.startsWith("/assets/scenarios/") || cover.startsWith("/assets/scenario_covers/"))) {
        return cover;
    }
    return TrpgAvatar.scenarioCoverTextDataUri(title);
}

// 剧本卡封面 HTML：模板引擎不支持条件分支，因此在这里预先拼好再整段注入。
function scenarioCoverHtml(title: string, cover?: string): string {
    return `<img src="${scenarioEscapeHtml(safeScenarioCover(title, cover))}" alt="${scenarioEscapeHtml(title)}" data-user-content="true">`;
}

/**
 * 保存并发布按钮的冷却保护：触发后立即禁用按钮并在冷却期内忽略后续点击，
 * 避免连点导致短时间内重复创建 / 发布剧本。
 */
function runSaveButtonCooldown(button: HTMLElement | null, action: () => Promise<void>, cooldownMs = 3000): Promise<void> {
    if (!button) return action();
    if (button.dataset.saveCooldown === "true") return Promise.resolve();
    button.dataset.saveCooldown = "true";
    const control = button as HTMLButtonElement;
    const wasDisabled = control.disabled;
    control.disabled = true;
    return (async () => {
        try {
            await action();
        } finally {
            window.setTimeout(() => {
                control.disabled = wasDisabled;
                button.dataset.saveCooldown = "false";
            }, cooldownMs);
        }
    })();
}

function updateScenarioModalTitle(key: string, fallback: string): void {
    const label = document.getElementById("scenarioModalLabel");
    if (label) label.textContent = scenarioT(key, fallback);
}

function scenarioT(key: string, fallback: string, values: Record<string, string | number> = {}): string {
    return window.TrpgI18n?.t(key, fallback, values) || fallback;
}

function input(id: string): HTMLInputElement {
    return requiredElement(id) as HTMLInputElement;
}

function textarea(id: string): HTMLTextAreaElement {
    return requiredElement(id) as HTMLTextAreaElement;
}

function checkbox(id: string): HTMLInputElement {
    return requiredElement(id) as HTMLInputElement;
}

function image(id: string): HTMLImageElement {
    return requiredElement(id) as HTMLImageElement;
}

function requiredElement(id: string): HTMLElement {
    const element = document.getElementById(id);
    if (!element) throw new Error(`missing DOM element: ${id}`);
    return element;
}

function scenarioEscapeHtml(value: unknown): string {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function scenarioViewErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

function renderKnowledgeSection(section: KnowledgeSectionInfo): string {
    const option = (value: string, label: string, current: string): string =>
        `<option value="${value}"${value === current ? " selected" : ""}>${label}</option>`;
    const numberField = (label: string, field: string, value: number): string =>
        `<label class="form-group"><span>${label}</span><input type="number" class="form-control" data-field="${field}" value="${value}"></label>`;
    return `
        <details class="scenario-knowledge-item" data-section-key="${scenarioEscapeHtml(section.section_key)}">
            <summary>
                <span class="scenario-knowledge-title">${scenarioEscapeHtml(section.title)}</span>
                <span class="scenario-knowledge-meta">${section.chunk_ids.length} 块 · ${scenarioEscapeHtml(section.tier)}${section.is_constant ? " · 常驻" : ""}</span>
            </summary>
            <div class="scenario-knowledge-body">
                <p class="text-muted small">${scenarioEscapeHtml(section.preview)}</p>
                <div class="scenario-knowledge-grid">
                    <label class="form-group"><span>触发词（逗号分隔）</span><textarea class="form-control" rows="2" data-field="keywords">${scenarioEscapeHtml(section.keywords.join("、"))}</textarea></label>
                    <label class="form-group"><span>副键（逗号分隔）</span><textarea class="form-control" rows="2" data-field="secondary_keywords">${scenarioEscapeHtml(section.secondary_keywords.join("、"))}</textarea></label>
                    <label class="form-group"><span>副键逻辑</span><select class="form-select" data-field="secondary_logic">
                        ${option("and_any", "任一命中", section.secondary_logic)}
                        ${option("and_all", "全部命中", section.secondary_logic)}
                        ${option("not_any", "全不命中", section.secondary_logic)}
                        ${option("not_all", "非全命中", section.secondary_logic)}
                    </select></label>
                    <label class="form-group"><span>分层</span><select class="form-select" data-field="tier">
                        ${option("core", "core（优先）", section.tier)}
                        ${option("background", "background", section.tier)}
                        ${option("archived", "archived（排除）", section.tier)}
                    </select></label>
                    <label class="form-group"><span>分组</span><input type="text" class="form-control" data-field="group" value="${scenarioEscapeHtml(section.group)}"></label>
                    ${numberField("分组权重", "group_weight", section.group_weight)}
                    ${numberField("优先级", "priority", section.priority)}
                    ${numberField("顺序", "order", section.order)}
                    ${numberField("概率 %", "probability", section.probability)}
                    ${numberField("粘滞轮", "sticky_rounds", section.sticky_rounds)}
                    ${numberField("冷却轮", "cooldown_rounds", section.cooldown_rounds)}
                    ${numberField("延迟轮", "delay_rounds", section.delay_rounds)}
                </div>
                <div class="scenario-knowledge-flags">
                    <label class="form-check"><input class="form-check-input" type="checkbox" data-field="is_constant"${section.is_constant ? " checked" : ""}><span>常驻注入</span></label>
                    <label class="form-check"><input class="form-check-input" type="checkbox" data-field="trigger_chunks"${section.trigger_chunks ? " checked" : ""}><span>允许递归触发</span></label>
                </div>
                <button type="button" class="btn btn-sm btn-primary" data-save-knowledge>保存本章节</button>
            </div>
        </details>`;
}

function canUseScenarioPermission(nodeId: string): boolean {
    const role = window.currentUser?.role || "USER";
    if (role === "OWNER" || role === "ADMIN") return true;
    return ["scenarios.preview", "scenarios.edit", "scenarios.delete"].includes(nodeId);
}

function canModifyScenario(scenario: Scenario): boolean {
    const role = window.currentUser?.role || "USER";
    if (role === "ADMIN" || role === "OWNER") return true;
    return String(scenario.owner_id ?? scenario.user_id ?? "") === String(window.currentUser?.user_id ?? "");
}

async function confirmScenarioSpoilerAccess(scenarioId: number): Promise<boolean> {
    const currentRoom = window.currentRoom;
    const playing = Boolean(
        currentRoom
        && !currentRoom.invisible_view
        && String(currentRoom.scenario_id || "") === String(scenarioId),
    );
    if (!playing) return true;
    return showLongPressConfirm("你正在进行这个剧本，继续查看可能会看到剧透。");
}

function showLongPressConfirm(message: string): Promise<boolean> {
    return new Promise((resolve) => {
        const modal = document.createElement("div");
        modal.className = "modal fade";
        modal.tabIndex = -1;
        modal.innerHTML = `
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${scenarioT("scenario.preview.warning_title", "防剧透提示")}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body"><p>${scenarioEscapeHtml(message)}</p></div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${scenarioT("scenario.preview.cancel", "取消")}</button>
                        <button type="button" class="btn btn-danger long-press-confirm">${scenarioT("scenario.preview.confirm", "长按确认")}</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        const instance = new bootstrap.Modal(modal);
        let timer: number | null = null;
        let completed = false;
        const button = modal.querySelector<HTMLButtonElement>(".long-press-confirm");
        const clearTimer = () => {
            if (timer !== null) window.clearTimeout(timer);
            timer = null;
            button?.classList.remove("holding");
        };
        button?.addEventListener("pointerdown", () => {
            button.classList.add("holding");
            timer = window.setTimeout(() => {
                completed = true;
                instance.hide();
            }, 1200);
        });
        ["pointerup", "pointerleave", "pointercancel"].forEach((eventName) => {
            button?.addEventListener(eventName, clearTimer);
        });
        modal.addEventListener("hidden.bs.modal", () => {
            clearTimer();
            modal.remove();
            resolve(completed);
        });
        instance.show();
    });
}
