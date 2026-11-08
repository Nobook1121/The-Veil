/** 战斗轮独立面板：先攻顺序、当前行动者、战斗日志与动作按钮。
 *
 * 动作通过 REST 提交，战斗检定会转成一次「玩家自己掷骰」的请求（复用骰桌 UI），
 * 结算结果通过 socket 的 ``combat_updated`` 广播回填到本面板。
 */
(function () {
    const COLLAPSED_KEY = "trpg_combat_collapsed";
    type FormAction = "attack" | "shoot" | "maneuver";

    let roomId = "";
    let state: CombatStatePayload | null = null;
    let formAction: FormAction | null = null;
    let awaitingRoll = false;
    let awaitingActor = "";
    let busy = false;
    let initialized = false;
    // 轮询去重：状态没变时不重绘 DOM，避免每 3 秒整块 innerHTML 重建。
    let lastPayloadKey = "";

    function byId<T extends HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function safeNum(value: unknown, fallback: number): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function notify(message: string, type: "success" | "error" | "info" | "warning" = "error"): void {
        window.showNotification?.(message, type);
    }

    function statusLabel(status: string): string {
        const labels: Record<string, string> = {
            active: "正常",
            unconscious: "昏迷",
            dying: "濒死",
            dead: "死亡",
        };
        return labels[status] || status;
    }

    function applyCollapsed(): void {
        const panel = byId("combatPanel");
        const body = byId("combatPanelBody");
        const toggle = byId("combatPanelToggle");
        if (!panel || !body || !toggle) return;
        const collapsed = window.localStorage.getItem(COLLAPSED_KEY) === "1";
        panel.classList.toggle("is-collapsed", collapsed);
        body.hidden = collapsed;
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        const icon = toggle.querySelector("i");
        if (icon) icon.className = collapsed ? "fa fa-chevron-down" : "fa fa-chevron-up";
    }

    function toggleCollapsed(): void {
        const collapsed = window.localStorage.getItem(COLLAPSED_KEY) !== "1";
        window.localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
        applyCollapsed();
    }

    function hidePanel(): void {
        formAction = null;
        awaitingActor = "";
        lastPayloadKey = "";
        const panel = byId("combatPanel");
        if (panel) panel.hidden = true;
        renderForm();
    }

    function renderOrder(): void {
        const container = byId("combatOrder");
        if (!container) return;
        const order = state?.order || [];
        if (order.length === 0) {
            container.innerHTML = "";
            return;
        }
        container.innerHTML = order
            .map((item) => {
                const hp = safeNum(item.hp, 0);
                const maxHp = Math.max(1, safeNum(item.max_hp, hp || 1));
                const percent = Math.max(0, Math.min(100, Math.round((hp / maxHp) * 100)));
                const status = String(item.status || "active");
                const isCurrent = state?.current === item.name;
                const sideClass = item.side === "enemy" ? "is-enemy" : item.is_player ? "is-player" : "is-npc";
                const tags: string[] = [];
                if (status !== "active") tags.push(`<span class="combat-tag is-status">${escapeHtml(statusLabel(status))}</span>`);
                if (item.major_wound) tags.push('<span class="combat-tag is-wound">重伤</span>');
                if (item.surprised) tags.push('<span class="combat-tag">被突袭</span>');
                if (safeNum(item.actions_per_round, 1) > 1) {
                    tags.push(`<span class="combat-tag">行动 ${safeNum(item.actions_remaining, 0)}/${safeNum(item.actions_per_round, 1)}</span>`);
                }
                if (item.ready_weapon) tags.push('<span class="combat-tag">已准备武器</span>');
                if (item.disengaged) tags.push('<span class="combat-tag">已脱离</span>');
                if (item.dying) tags.push('<span class="combat-tag is-status">濒死</span>');
                if (item.used_dodge) tags.push('<span class="combat-tag">已闪避</span>');
                if (item.used_counter) tags.push('<span class="combat-tag">已反击</span>');
                return `
                    <div class="combat-order-row ${isCurrent ? "is-current" : ""} ${sideClass}">
                        <div class="combat-order-head">
                            <span class="combat-order-name">${escapeHtml(item.name)}</span>
                            ${isCurrent ? '<span class="combat-order-turn">当前</span>' : ""}
                        </div>
                        <div class="combat-order-hp" role="progressbar" aria-valuemin="0" aria-valuemax="${maxHp}" aria-valuenow="${hp}">
                            <span style="width: ${percent}%"></span>
                        </div>
                        <div class="combat-order-meta">
                            <span class="combat-order-number">HP ${hp}/${maxHp}</span>
                            ${tags.join("")}
                        </div>
                    </div>`;
            })
            .join("");
    }

    function renderCurrent(): void {
        const container = byId("combatCurrent");
        if (!container) return;
        if (!state?.active) {
            container.innerHTML = "";
            return;
        }
        const round = safeNum(state.round, 1);
        const current = String(state.current || "");
        const surprised = (state.surprised || []).length > 0 && round === 1
            ? '<span class="combat-current-note">首轮突袭：仅修正攻击检定，不跳过行动</span>'
            : "";
        container.innerHTML = `
            <div class="combat-current-round">第 ${round} 轮</div>
            <div class="combat-current-actor">
                <span>当前行动者</span>
                <strong>${escapeHtml(current || "等待中")}</strong>
            </div>
            ${surprised}`;
    }

    function renderActions(): void {
        const container = byId("combatActions");
        if (!container) return;
        if (!state?.active) {
            container.innerHTML = "";
            return;
        }
        if (awaitingRoll) {
            container.innerHTML = '<p class="combat-waiting"><i class="fa fa-hourglass-half" aria-hidden="true"></i> 请完成骰桌上的战斗检定</p>';
            return;
        }
        if (!state.can_act) {
            const waiting = state.current ? escapeHtml(state.current) : "其他行动者";
            container.innerHTML = `<p class="combat-waiting">等待 <strong>${waiting}</strong> 行动</p>`;
            return;
        }
        const manager = state.is_manager === true;
        const nextLabel = state.my_combatant && state.my_combatant === state.current ? "跳过本回合" : "推进回合";
        container.innerHTML = [
            '<button type="button" class="combat-action-btn" data-combat-action="attack"><i class="fa fa-crosshairs" aria-hidden="true"></i><span>攻击</span></button>',
            '<button type="button" class="combat-action-btn" data-combat-action="shoot"><i class="fa fa-bullseye" aria-hidden="true"></i><span>射击</span></button>',
            '<button type="button" class="combat-action-btn" data-combat-action="maneuver"><i class="fa fa-hand-paper-o" aria-hidden="true"></i><span>战技</span></button>',
            '<button type="button" class="combat-action-btn is-ghost" data-combat-action="delay"><i class="fa fa-hourglass-half" aria-hidden="true"></i><span>延迟</span></button>',
            '<button type="button" class="combat-action-btn is-ghost" data-combat-action="disengage"><i class="fa fa-sign-out" aria-hidden="true"></i><span>脱离</span></button>',
            '<button type="button" class="combat-action-btn is-ghost" data-combat-action="reload"><i class="fa fa-refresh" aria-hidden="true"></i><span>装填</span></button>',
            '<button type="button" class="combat-action-btn is-ghost" data-combat-action="ready"><i class="fa fa-flag" aria-hidden="true"></i><span>准备武器</span></button>',
            state.can_advance
                ? `<button type="button" class="combat-action-btn is-ghost" data-combat-action="next"><i class="fa fa-forward" aria-hidden="true"></i><span>${escapeHtml(nextLabel)}</span></button>`
                : "",
            manager
                ? '<button type="button" class="combat-action-btn is-danger" data-combat-action="end"><i class="fa fa-flag-checkered" aria-hidden="true"></i><span>结束战斗</span></button>'
                : "",
        ].join("");
    }

    function renderForm(): void {
        const container = byId("combatForm");
        if (!container) return;
        if (!formAction || !state?.can_act) {
            formAction = null;
            container.hidden = true;
            container.innerHTML = "";
            return;
        }
        const weapons = state.weapons || [];
        const targets = state.targets || [];
        const action = formAction;
        const relevantWeapons = weapons.filter((weapon) => (action === "shoot" ? weapon.ranged : !weapon.ranged));
        const weaponOptions = relevantWeapons
            .map(
                (weapon) =>
                    `<option value="${escapeHtml(weapon.name)}">${escapeHtml(weapon.name)} · ${escapeHtml(weapon.skill)} · ${escapeHtml(weapon.damage)}</option>`,
            )
            .join("");
        const targetOptions = targets
            .map(
                (target) =>
                    `<option value="${escapeHtml(target.name)}">${escapeHtml(target.name)}（HP ${safeNum(target.hp, 0)}）</option>`,
            )
            .join("");
        const title = action === "attack" ? "近战攻击" : action === "shoot" ? "射击" : "战技";
        const weaponField = action === "maneuver"
            ? ""
            : `<label class="combat-form-field"><span>武器</span><select class="form-select form-select-sm" data-combat-field="weapon">${weaponOptions}</select></label>`;
        const defenseField = action === "attack"
            ? `<label class="combat-form-field"><span>目标应对</span><select class="form-select form-select-sm" data-combat-field="defense">
                    <option value="dodge">闪避（服务端掷骰）</option>
                    <option value="counter">反击（服务端掷骰）</option>
                    <option value="none">不防御</option>
               </select></label>`
            : "";
        const maneuverField = action === "maneuver"
            ? `<label class="combat-form-field"><span>战技效果</span><select class="form-select form-select-sm" data-combat-field="maneuver">
                    <option value="disarm">缴械</option>
                    <option value="knock_down">击倒</option>
                    <option value="push">推撞</option>
               </select></label>`
            : "";
        const diceField = `<div class="combat-form-dice">
                <label class="combat-form-field"><span>奖励骰</span><select class="form-select form-select-sm" data-combat-field="bonus">
                    <option value="0">无</option><option value="1">1</option><option value="2">2</option>
                </select></label>
                <label class="combat-form-field"><span>惩罚骰</span><select class="form-select form-select-sm" data-combat-field="penalty">
                    <option value="0">无</option><option value="1">1</option><option value="2">2</option>
                </select></label>
            </div>`;
        const missingWeapon = action !== "maneuver" && relevantWeapons.length === 0
            ? '<p class="combat-form-empty">角色卡上没有可用于该动作的武器。</p>'
            : "";
        const disabled = targets.length === 0 || missingWeapon ? "disabled" : "";
        container.hidden = false;
        container.innerHTML = `
            <div class="combat-form-head">
                <span><i class="fa fa-bolt" aria-hidden="true"></i> ${title}</span>
                <button type="button" class="combat-form-cancel" data-combat-form="cancel" aria-label="取消">
                    <i class="fa fa-times" aria-hidden="true"></i>
                </button>
            </div>
            <div class="combat-form-grid">
                <label class="combat-form-field"><span>目标</span><select class="form-select form-select-sm" data-combat-field="target">${targetOptions}</select></label>
                ${weaponField}
                ${defenseField}
                ${maneuverField}
                ${diceField}
            </div>
            ${missingWeapon}
            <div class="combat-form-actions">
                <button type="button" class="btn btn-sm btn-primary" data-combat-form="submit" ${disabled}>
                    <i class="fa fa-hand-pointer-o" aria-hidden="true"></i> 掷骰并行动
                </button>
            </div>`;
    }

    function renderLog(): void {
        const container = byId("combatLog");
        if (!container) return;
        const entries = state?.log || [];
        if (entries.length === 0) {
            container.innerHTML = '<p class="combat-log-empty">战斗日志会在这里滚动显示。</p>';
            return;
        }
        const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 32;
        container.innerHTML = entries
            .map(
                (entry) =>
                    `<div class="combat-log-entry"><span class="combat-log-time">${escapeHtml(entry.time)}</span><span class="combat-log-text">${escapeHtml(entry.text)}</span></div>`,
            )
            .join("");
        if (atBottom) container.scrollTop = container.scrollHeight;
    }

    function render(payload: CombatStatePayload): void {
        state = payload;
        const panel = byId("combatPanel");
        if (!panel) return;
        if (!payload.active) {
            hidePanel();
            return;
        }
        panel.hidden = false;
        // 服务端已推进到别的行动者：本次等待已结束，避免面板卡在“请完成骰桌上的战斗检定”。
        if (awaitingRoll && String(payload.current || "") !== awaitingActor) {
            awaitingRoll = false;
            awaitingActor = "";
        }
        applyCollapsed();
        const round = byId("combatRound");
        if (round) round.textContent = `第 ${safeNum(payload.round, 1)} 轮`;
        renderOrder();
        renderCurrent();
        renderActions();
        renderForm();
        renderLog();
    }

    function setBusyUi(value: boolean): void {
        const container = byId("combatActions");
        if (!container) return;
        container.querySelectorAll<HTMLButtonElement>(".combat-action-btn").forEach((button) => {
            button.disabled = value;
        });
    }

    function readFormPayload(): CombatActionRequest | null {
        const container = byId("combatForm");
        if (!container || !formAction || !state) return null;
        const read = (name: string): string => {
            const field = container.querySelector<HTMLSelectElement>(`[data-combat-field="${name}"]`);
            return field?.value || "";
        };
        const payload: CombatActionRequest = { action: formAction };
        const actor = String(state.current || "");
        if (actor) payload.actor = actor;
        const target = read("target");
        if (target) payload.target = target;
        if (formAction === "attack" || formAction === "shoot") {
            const weapon = read("weapon");
            if (weapon) payload.weapon = weapon;
        }
        if (formAction === "attack") {
            const defense = read("defense");
            payload.defense = defense === "counter" || defense === "none" ? defense : "dodge";
        }
        if (formAction === "maneuver") payload.maneuver = read("maneuver") || "disarm";
        const bonus = Number.parseInt(read("bonus") || "0", 10);
        const penalty = Number.parseInt(read("penalty") || "0", 10);
        if (bonus > 0) payload.bonus_dice = bonus;
        if (penalty > 0) payload.penalty_dice = penalty;
        return payload;
    }

    async function submitAction(payload: CombatActionRequest): Promise<void> {
        if (!roomId || busy) return;
        busy = true;
        setBusyUi(true);
        try {
            const response = await TrpgApi.post<ApiResponse<CombatActionResponse>>(
                `/api/rooms/${encodeURIComponent(roomId)}/combat/action`,
                payload,
            );
            if (!response.success || !response.data) {
                notify(response.message || response.error || "战斗操作失败");
                return;
            }
            if (response.data.error) {
                notify(response.data.error);
                return;
            }
            formAction = null;
            if (payload.action === "end" || response.data.active === false) {
                hidePanel();
                notify("战斗结束", "success");
                return;
            }
            if (response.data.immediate) {
                render({ active: true, ...response.data } as CombatStatePayload);
                return;
            }
            if (!response.data.request_id) {
                // 推进回合等不产生投掷请求的操作：直接采用返回的新状态，
                // 不能进入「等待骰桌检定」，否则面板会卡在等待提示。
                awaitingRoll = false;
                awaitingActor = "";
                render({ active: true, ...response.data } as CombatStatePayload);
                void refresh();
                return;
            }
            awaitingRoll = true;
            awaitingActor = String(state?.current || "");
            renderActions();
            renderForm();
            const label = response.data.skill_label || "战斗检定";
            const target = response.data.skill_value;
            notify(target ? `等待你掷骰：${label}（技能值 ${target}）` : `等待你掷骰：${label}`, "info");
        } catch (error) {
            notify(`战斗操作失败：${error instanceof Error ? error.message : String(error)}`);
        } finally {
            busy = false;
            setBusyUi(false);
        }
    }

    async function handleActionClick(action: string): Promise<void> {
        if (!state || busy) return;
        if (action === "next") {
            await submitAction({ action: "next" });
            return;
        }
        if (action === "end") {
            const confirmed = await window.TrpgDialog?.confirm({
                title: "结束战斗",
                message: "确定结束当前战斗轮？",
                confirmText: "结束",
                danger: true,
            });
            if (!confirmed) return;
            await submitAction({ action: "end" });
            return;
        }
        if (action === "delay" || action === "disengage" || action === "reload" || action === "ready") {
            await submitAction({ action: action as CombatActionRequest["action"] });
            return;
        }
        if (action === "attack" || action === "shoot" || action === "maneuver") {
            formAction = action;
            renderForm();
        }
    }

    async function refresh(): Promise<void> {
        const current = String(window.currentRoom?.id || "");
        if (current !== roomId) {
            roomId = current;
            state = null;
            formAction = null;
            awaitingRoll = false;
            lastPayloadKey = "";
        }
        if (!roomId || window.currentRoom?.invisible_view) {
            hidePanel();
            return;
        }
        const requested = roomId;
        try {
            const response = await TrpgApi.get<ApiResponse<CombatStatePayload>>(
                `/api/rooms/${encodeURIComponent(requested)}/combat`,
            );
            if (requested !== roomId) return;
            if (!response.success || !response.data) return;
            if (response.data.active) {
                // 状态未变化时跳过重绘（轮询最常见的路径）。
                const payloadKey = JSON.stringify(response.data);
                if (payloadKey === lastPayloadKey) return;
                lastPayloadKey = payloadKey;
                const pending = response.data.pending_roll;
                // 只有仍在等玩家投掷（pending）时才显示「请完成骰桌上的战斗检定」。
                // 已经掷出（rolled/awaiting_confirm）时战斗结算已完成，面板不应继续等待。
                const waitingThrow = Boolean(pending)
                    && String(pending?.state || "pending") === "pending"
                    && String(pending?.tool_name || "").startsWith("combat.");
                if (waitingThrow) {
                    awaitingRoll = true;
                    if (!awaitingActor) awaitingActor = String(response.data.current || "");
                } else {
                    awaitingRoll = false;
                    awaitingActor = "";
                }
                render(response.data);
            } else {
                hidePanel();
            }
        } catch {
            // 房间刚切换或暂时离线时静默失败，等下一次刷新。
        }
    }

    function onUpdated(payload: CombatStatePayload): void {
        const incoming = String(payload.room_id || "");
        if (!roomId || (incoming && incoming !== roomId)) return;
        awaitingRoll = false;
        render(payload);
        // socket 广播是房间级摘要（不含 can_act / 武器 / 目标），再取一次完整状态，
        // 让当前行动者的按钮和表单能正确出现。
        void refresh();
    }

    function onRoomChanged(): void {
        roomId = String(window.currentRoom?.id || "");
        state = null;
        formAction = null;
        awaitingRoll = false;
        awaitingActor = "";
        hidePanel();
        void refresh();
    }

    /** 骰子已经投出/确认：立刻拉一次完整战斗状态，避免面板停在等待检定。 */
    function onDiceResolved(): void {
        if (!state?.active) return;
        awaitingRoll = false;
        awaitingActor = "";
        void refresh();
    }

    function init(): void {
        if (initialized) return;
        const panel = byId("combatPanel");
        if (!panel) return;
        initialized = true;
        applyCollapsed();
        byId("combatPanelToggle")?.addEventListener("click", toggleCollapsed);
        byId("combatActions")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const button = target?.closest<HTMLElement>("[data-combat-action]");
            if (!button) return;
            void handleActionClick(button.dataset.combatAction || "");
        });
        byId("combatForm")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            const trigger = target?.closest<HTMLElement>("[data-combat-form]");
            if (!trigger) return;
            if (trigger.dataset.combatForm === "cancel") {
                formAction = null;
                renderForm();
                return;
            }
            const payload = readFormPayload();
            if (payload) void submitAction(payload);
        });
        void refresh();
        // 兜底轮询：漏掉 socket 广播时也能把面板从“等待检定/旧回合”拉回真实状态。
        window.setInterval(() => { if (state?.active) void refresh(); }, 3000);
    }

    window.TrpgCombat = { init, refresh: () => void refresh(), onRoomChanged, onUpdated, onDiceResolved };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
