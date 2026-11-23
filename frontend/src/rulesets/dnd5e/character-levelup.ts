// D&D 5e 升级向导（FVTT advancement 的四步形态）。
//
// 入口在卡面头区：等级徽章旁的「升级」按钮（rulesets/dnd5e/character-sheet.ts）
// 调 window.TrpgDnd5eLevelUp.open({ card, onSaved })。四步沿用建卡向导的步进
// 风格与样式体系（dnd5e-wiz-*），不引入新样式表：
//   ① 确认：Lv N → N+1，单职业锁定（多职业数据不支持，界面直说）
//   ② HP：掷生命骰（后端真随机）/ 取平均值 + CON，二选一
//   ③ 提升：该等级新特性 / 子职 / 属性提升（+2 一项或两项各 +1，上限 20）
//   ④ 法术 + 预览：新增已知/准备名额、法术位变化，预览后确认保存
//
// 规则数学全部在后端（dnd5e_plugin/progression.py）；本文件只做选择与预览：
// `POST /api/plugins/dnd5e/characters/<id>/level-up`
//   - 不带 plan_id：生成升级计划（含 HP 掷骰结果、特性表命中、子职与法术槽位）
//   - 带 plan_id + choices：后端校验全部槽位后走 apply_save 落库
// 预览用计划里的 detail 推算，所以「预览看到的」与「保存后的」同源。
//
// 升级后 XP 保持不变（手动升级入口，不按等级表补经验），界面显式标注。

type Dnd5eLevelUpAbility = "str" | "dex" | "con" | "int" | "wis" | "cha";
type Dnd5eLevelUpStep = 1 | 2 | 3 | 4;
type Dnd5eLevelUpHpMode = "roll" | "average";
type Dnd5eLevelUpAsiMode = "plus2" | "plus1";

interface Dnd5eLevelUpCandidate {
    value: string;
    label: string;
    /** 主名（SRD 为英文名，扩展库为中文名）。 */
    name?: string;
    /** 中文名（SRD 有 zh-CN 本地化时才给）。 */
    name_zh?: string;
    /** 一行简介（扩展库子职＝「等级 特性」清单；SRD 无描述时为空）。 */
    summary?: string;
    /** 来源标记：``srd``（dnd5e-srd 数据包）/ ``kiwee``（kiwee 深库）。 */
    source?: string;
    detail?: Record<string, unknown>;
}

interface Dnd5eLevelUpSlot {
    id: string;
    kind: string;
    level: number;
    title: string;
    required?: boolean;
    min_picks?: number;
    max_picks?: number;
    candidates?: Dnd5eLevelUpCandidate[];
    default?: unknown;
    detail?: Record<string, unknown>;
    not_selected?: string;
}

interface Dnd5eLevelUpHitPoints {
    die: number;
    roll: number;
    average: number;
    con_modifier: number;
    roll_total: number;
    average_total: number;
    before_max: number;
    before_current: number;
}

interface Dnd5eLevelUpFeature {
    id: string;
    level: number;
    kind: string;
    name: string;
    label: string;
}

interface Dnd5eLevelUpSubclass {
    level: number;
    current: string;
    label: string;
    options: Dnd5eLevelUpCandidate[];
    /** 候选来源计数：``srd``（dnd5e-srd 数据包）/ ``kiwee``（扩展库）。 */
    counts?: { srd?: number; kiwee?: number };
    level_due: boolean;
}

interface Dnd5eLevelUpPlan {
    plan_id: string;
    character_id?: string;
    class: string;
    class_name: string;
    current_level: number;
    target_level: number;
    hit_die: number;
    xp: { before: number; after: number; manual?: boolean };
    hit_points: Dnd5eLevelUpHitPoints;
    features: Dnd5eLevelUpFeature[];
    subclass: Dnd5eLevelUpSubclass;
    slots: Dnd5eLevelUpSlot[];
    warnings: string[];
    expires_in?: number;
}

interface Dnd5eLevelUpChange {
    field: string;
    before: unknown;
    after: unknown;
}

interface Dnd5eLevelUpApplyResult {
    plan_id: string;
    character_id: string;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
    changes: Dnd5eLevelUpChange[];
    sheet: Record<string, unknown>;
    card?: Record<string, unknown>;
}

interface Dnd5eLevelUpOptions {
    /** 当前卡（至少要有 id 与 sheet）；职业/等级以服务端读取的卡为准。 */
    card: Record<string, unknown>;
    /** 落库成功后把新卡交回卡面模块刷新（等级徽章、页签、HP 一起更新）。 */
    onSaved?: (card: Record<string, unknown>) => void;
}

interface Dnd5eLevelUpApi {
    open: (options: Dnd5eLevelUpOptions) => void;
    close: () => void;
    isOpen: () => boolean;
}

interface Window {
    TrpgDnd5eLevelUp?: Dnd5eLevelUpApi;
}

(function installDnd5eLevelUp(global: Window & typeof globalThis): void {
    "use strict";

    const RULESET_ID = "dnd5e";
    const RULES_API = "/api/plugins/dnd5e/rules";
    const ASI_CAP = 20;

    const ABILITIES: Array<{ key: Dnd5eLevelUpAbility; label: string }> = [
        { key: "str", label: "力量" },
        { key: "dex", label: "敏捷" },
        { key: "con", label: "体质" },
        { key: "int", label: "智力" },
        { key: "wis", label: "感知" },
        { key: "cha", label: "魅力" },
    ];

    const STEP_TITLES: Array<{ step: Dnd5eLevelUpStep; label: string; icon: string }> = [
        { step: 1, label: "确认", icon: "fa-arrow-up" },
        { step: 2, label: "生命值", icon: "fa-heart" },
        { step: 3, label: "提升", icon: "fa-star" },
        { step: 4, label: "法术与预览", icon: "fa-magic" },
    ];

    interface LevelUpDeepItem {
        uid: string;
        name: string;
        engName: string;
        source: string;
    }

    interface LevelUpState {
        step: Dnd5eLevelUpStep;
        cardId: string;
        cardName: string;
        options: Dnd5eLevelUpOptions;
        plan: Dnd5eLevelUpPlan | null;
        loading: boolean;
        error: string;
        hpMode: Dnd5eLevelUpHpMode;
        asiMode: Dnd5eLevelUpAsiMode;
        asiPicks: string[];
        subclassPick: string;
        subclassCustom: string;
        /** 子职候选的客户端筛选关键字（>8 条候选时才出现输入框）。 */
        subclassFilter: string;
        spellKnown: string[];
        spellPrepared: string[];
        saving: boolean;
        deepAvailable: boolean | null;
        deepQuery: string;
        deepResults: LevelUpDeepItem[];
        deepLoading: boolean;
        deepMessage: string;
    }

    let state: LevelUpState | null = null;
    let modal: HTMLElement | null = null;
    let instance: BootstrapModalInstance | null = null;
    let deepProbe: Promise<boolean> | null = null;

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function notify(message: string, type: string = "info"): void {
        const notifier = global.showNotification;
        if (typeof notifier === "function") notifier(message, type);
    }

    function signed(value: number): string {
        return value >= 0 ? `+${value}` : `${value}`;
    }

    function asInt(value: unknown, fallback: number = 0): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
    }

    function proficiencyBonus(level: number): number {
        return 2 + Math.floor((Math.max(1, Math.min(20, level)) - 1) / 4);
    }

    function slotOf(current: LevelUpState, kind: string): Dnd5eLevelUpSlot | null {
        const slot = (current.plan?.slots || []).find((item) => item.kind === kind);
        return slot || null;
    }

    function candidateLabel(candidate: Dnd5eLevelUpCandidate | undefined, fallback: string = ""): string {
        return candidate ? String(candidate.label || candidate.value) : fallback;
    }

    function totalFor(mode: Dnd5eLevelUpHpMode, hitPoints: Dnd5eLevelUpHitPoints): number {
        return mode === "roll" ? hitPoints.roll_total : hitPoints.average_total;
    }

    // --- 状态 -----------------------------------------------------------------

    function freshState(options: Dnd5eLevelUpOptions): LevelUpState {
        const card = options.card || {};
        return {
            step: 1,
            cardId: String(card.id || ""),
            cardName: String(card.name || ""),
            options,
            plan: null,
            loading: true,
            error: "",
            hpMode: "roll",
            asiMode: "plus2",
            asiPicks: [],
            subclassPick: "",
            subclassCustom: "",
            subclassFilter: "",
            spellKnown: [],
            spellPrepared: [],
            saving: false,
            deepAvailable: null,
            deepQuery: "",
            deepResults: [],
            deepLoading: false,
            deepMessage: "",
        };
    }

    function applyPlan(plan: Dnd5eLevelUpPlan): void {
        const current = state;
        if (!current) return;
        current.plan = plan;
        current.loading = false;
        current.error = "";
        current.spellKnown = [];
        const preparedSlot = (plan.slots || []).find((slot) => slot.kind === "spell_prepared");
        const preset = preparedSlot?.default;
        current.spellPrepared = Array.isArray(preset) ? preset.map((item) => String(item)) : [];
        const subclassSlot = (plan.slots || []).find((slot) => slot.kind === "subclass");
        if (subclassSlot && !current.subclassPick) {
            const first = (subclassSlot.candidates || [])[0];
            current.subclassPick = first ? String(first.value) : "";
        }
    }

    // --- 渲染 -----------------------------------------------------------------

    function renderStepper(current: LevelUpState): string {
        return `
            <ol class="dnd5e-wiz-steps" aria-label="升级进度">
                ${STEP_TITLES.map((item) => `
                    <li class="dnd5e-wiz-step ${current.step === item.step ? "is-active" : ""} ${current.step > item.step ? "is-done" : ""}"
                        ${current.step === item.step ? 'aria-current="step"' : ""}>
                        <span class="dnd5e-wiz-step-index">${current.step > item.step ? '<i class="fa fa-check" aria-hidden="true"></i>' : item.step}</span>
                        <span class="dnd5e-wiz-step-label"><i class="fa ${item.icon}" aria-hidden="true"></i> ${escapeHtml(item.label)}</span>
                    </li>`).join("")}
            </ol>`;
    }

    function renderWarnings(plan: Dnd5eLevelUpPlan): string {
        if (!plan.warnings || plan.warnings.length === 0) return "";
        return `
            <ul class="dnd5e-wiz-hint">
                ${plan.warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
            </ul>`;
    }

    function renderStepConfirm(current: LevelUpState): string {
        const plan = current.plan;
        if (!plan) {
            return `
                <section class="dnd5e-wiz-section">
                    <h4><i class="fa fa-circle-o-notch fa-spin" aria-hidden="true"></i> 正在读取升级计划…</h4>
                    <p class="dnd5e-wiz-hint">计划由后端按 SRD 5.1 职业表（生命骰 / 特性等级 / 法术进度）生成。</p>
                </section>`;
        }
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-arrow-up" aria-hidden="true"></i> 升级确认</h4>
                <div class="dnd5e-wiz-class-grid">
                    <div class="dnd5e-wiz-class is-selected">
                        <strong>Lv ${plan.current_level} → Lv ${plan.target_level}</strong>
                        <small>${escapeHtml(plan.class_name || plan.class)}</small>
                    </div>
                    <div class="dnd5e-wiz-class">
                        <strong>1d${plan.hit_die}</strong>
                        <small>生命骰（HP 在第 ② 步结算）</small>
                    </div>
                    <div class="dnd5e-wiz-class">
                        <strong>${proficiencyBonus(plan.current_level)} → ${proficiencyBonus(plan.target_level)}</strong>
                        <small>熟练加值</small>
                    </div>
                </div>
                <p class="dnd5e-wiz-hint">
                    只支持<b>单职业</b>逐级提升（一次一级）；多职业与跨职业数据暂不支持，职业由角色卡上的
                    「${escapeHtml(String(plan.class_name || plan.class))}」锁定。
                </p>
                <p class="dnd5e-wiz-hint">
                    <i class="fa fa-info-circle" aria-hidden="true"></i>
                    这是<b>手动升级</b>入口：升级后经验值保持不变（当前 ${asInt(plan.xp?.before, 0)} XP，不按等级表补 XP）。
                </p>
                ${renderWarnings(plan)}
            </section>`;
    }

    function renderStepHitPoints(current: LevelUpState): string {
        const plan = current.plan;
        if (!plan) return renderStepConfirm(current);
        const hp = plan.hit_points;
        const con = asInt(hp.con_modifier, 0);
        const rollTotal = asInt(hp.before_max, 0) + asInt(hp.roll_total, 0);
        const averageTotal = asInt(hp.before_max, 0) + asInt(hp.average_total, 0);
        const option = (mode: Dnd5eLevelUpHpMode, title: string, body: string, tail: string): string => `
            <button type="button" class="dnd5e-wiz-class ${current.hpMode === mode ? "is-selected" : ""}"
                data-lvl-action="hp-mode" data-mode="${mode}" aria-pressed="${current.hpMode === mode ? "true" : "false"}">
                <strong>${escapeHtml(title)}</strong>
                <small>${escapeHtml(body)}</small>
                <small>${escapeHtml(tail)}</small>
            </button>`;
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-heart" aria-hidden="true"></i> 生命值上限</h4>
                <p class="dnd5e-wiz-hint">
                    当前上限 <b>${asInt(hp.before_max, 0)}</b>（当前 HP ${asInt(hp.before_current, 0)}）；
                    CON 调整值 ${signed(con)}。掷骰结果由后端真随机产生。
                </p>
                <div class="dnd5e-wiz-class-grid">
                    ${option("roll", `掷生命骰 1d${hp.die} = ${hp.roll}`, `${hp.roll} ${signed(con)} = ${signed(asInt(hp.roll_total, 0))}`, `新上限 ${rollTotal}`)}
                    ${option("average", `取平均值 = ${hp.average}`, `${hp.average} ${signed(con)} = ${signed(asInt(hp.average_total, 0))}`, `新上限 ${averageTotal}`)}
                </div>
                <div class="dnd5e-wiz-picked">
                    已选收益：<strong>${signed(totalFor(current.hpMode, hp))}</strong> HP
                    <small>（骰面/2+1 上整再 + CON；升级后当前 HP 同步加同额）</small>
                    <button type="button" class="dnd5e-wiz-link" data-lvl-action="reroll">
                        <i class="fa fa-refresh" aria-hidden="true"></i> 重掷（重新向后端掷一次）
                    </button>
                </div>
                <p class="dnd5e-wiz-hint">生命骰池同步 +1（d${hp.die}），短休时可用。</p>
            </section>`;
    }

    function abilitiesOf(current: LevelUpState): Record<string, number> {
        const sheet = (current.options.card?.sheet && typeof current.options.card.sheet === "object")
            ? current.options.card.sheet as Record<string, unknown>
            : {};
        const raw = (sheet.abilities && typeof sheet.abilities === "object")
            ? sheet.abilities as Record<string, unknown>
            : {};
        const result: Record<string, number> = {};
        ABILITIES.forEach((item) => { result[item.key] = asInt(raw[item.key], 10); });
        return result;
    }

    function asiAmount(current: LevelUpState): number {
        return current.asiMode === "plus2" ? 2 : 1;
    }

    function asiLimit(current: LevelUpState): number {
        return current.asiMode === "plus2" ? 1 : 2;
    }

    function asiAvailable(current: LevelUpState, key: Dnd5eLevelUpAbility): boolean {
        const base = abilitiesOf(current)[key] ?? 10;
        const boost = current.asiMode === "plus2"
            ? (current.asiPicks[0] === key ? 0 : 2)
            : (current.asiPicks.includes(key) ? 0 : 1);
        return base + boost <= ASI_CAP;
    }

    function asiProjection(current: LevelUpState, key: Dnd5eLevelUpAbility): string {
        const base = abilitiesOf(current)[key] ?? 10;
        const amount = current.asiPicks.includes(key) ? asiAmount(current) : 0;
        const next = Math.min(ASI_CAP, base + amount);
        return `${base} → ${next}`;
    }

    function renderAbilityScoreImprovement(current: LevelUpState): string {
        const slot = slotOf(current, "asi");
        if (!slot) return "";
        const amount = asiAmount(current);
        const left = asiLimit(current) - current.asiPicks.length;
        const chips = ABILITIES.map((item) => {
            const picked = current.asiPicks.includes(item.key);
            const blocked = !picked && !asiAvailable(current, item.key);
            return `
                <button type="button" class="dnd5e-wiz-chip-btn ${picked ? "is-selected" : ""}"
                    data-lvl-action="asi-toggle" data-ability="${item.key}"
                    aria-pressed="${picked ? "true" : "false"}" ${blocked ? "disabled" : ""}>
                    ${escapeHtml(item.label)} <small>${escapeHtml(asiProjection(current, item.key))}</small>
                </button>`;
        }).join("");
        const modeButton = (mode: Dnd5eLevelUpAsiMode, title: string): string => `
            <button type="button" class="dnd5e-wiz-mode ${current.asiMode === mode ? "is-active" : ""}"
                data-lvl-action="asi-mode" data-mode="${mode}" aria-pressed="${current.asiMode === mode ? "true" : "false"}">
                <strong>${escapeHtml(title)}</strong>
                <small>${mode === "plus2" ? "单一属性 +2" : "两项属性各 +1"}</small>
            </button>`;
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-star" aria-hidden="true"></i> 属性提升（上限 ${ASI_CAP}）</h4>
                <div class="dnd5e-wiz-mode-tabs">
                    ${modeButton("plus2", "+2 一项")}
                    ${modeButton("plus1", "+1 两项")}
                </div>
                <div class="dnd5e-wiz-skill-grid">${chips}</div>
                <p class="dnd5e-wiz-hint">
                    ${left > 0
                        ? `还可以选 ${left} 项（每项 +${amount}）；达到 ${ASI_CAP} 的属性不能再加。`
                        : `已选 ${current.asiPicks.length} 项（每项 +${amount}）。`}
                    属性提升是必选项，也可以改选专长。
                </p>
                <div class="dnd5e-wiz-field">
                    <label for="dnd5eLvlFeat">或选专长</label>
                    <select id="dnd5eLvlFeat" data-lvl-field="feat">
                        <option value="">— 不选专长 —</option>
                        ${(slot.candidates || [])
                            .filter((item) => String(item.value).startsWith("feat:"))
                            .map((item) => `<option value="${escapeHtml(item.value)}" ${current.asiPicks.includes(item.value) ? "selected" : ""}>${escapeHtml(item.label)}</option>`)
                            .join("")}
                    </select>
                </div>
            </section>`;
    }

    /** 候选来源：``srd``（dnd5e-srd 数据包）/ ``kiwee``（扩展库深库）。 */
    function subclassSource(item: Dnd5eLevelUpCandidate): "srd" | "kiwee" {
        const detail = item.detail || {};
        const raw = String(item.source || detail.source_code || "").toLowerCase();
        return raw === "kiwee" ? "kiwee" : "srd";
    }

    function subclassSourceLabel(item: Dnd5eLevelUpCandidate): string {
        const detail = item.detail || {};
        const label = String(detail.source || "");
        return label || (subclassSource(item) === "kiwee" ? "扩展库" : "SRD 5.1");
    }

    function subclassSearchText(item: Dnd5eLevelUpCandidate): string {
        const detail = item.detail || {};
        return [
            item.value,
            item.label,
            item.name,
            item.name_zh,
            item.summary,
            detail.eng_name,
            detail.source,
            detail.page_hint,
            detail.source_code,
        ]
            .map((part) => String(part ?? ""))
            .join(" ")
            .toLowerCase();
    }

    function subclassGroups(current: LevelUpState): {
        srd: Dnd5eLevelUpCandidate[];
        kiwee: Dnd5eLevelUpCandidate[];
        total: number;
    } {
        const list = slotOf(current, "subclass")?.candidates || [];
        return {
            srd: list.filter((item) => subclassSource(item) === "srd"),
            kiwee: list.filter((item) => subclassSource(item) === "kiwee"),
            total: list.length,
        };
    }

    function renderSubclassCard(current: LevelUpState, item: Dnd5eLevelUpCandidate): string {
        const selected = current.subclassPick === item.value && !current.subclassCustom.trim();
        const summary = String(item.summary || "").trim();
        const fromKiwee = subclassSource(item) === "kiwee";
        // 选中项额外挂来源小标签（其余项的来源在下方 small 行里）。
        const badge = selected
            ? `<span class="dnd5e-wiz-badge${fromKiwee ? " is-muted" : ""}">${fromKiwee ? "扩展库" : "SRD"}</span>`
            : "";
        const cardClass = selected ? "dnd5e-wiz-class is-selected" : "dnd5e-wiz-class";
        return `
            <button type="button" class="${cardClass}"
                data-lvl-action="subclass-pick" data-value="${escapeHtml(item.value)}"
                data-lvl-subclass-item data-search="${escapeHtml(subclassSearchText(item))}"
                aria-pressed="${selected ? "true" : "false"}">
                <strong>${escapeHtml(candidateLabel(item, item.value))}</strong>
                ${badge}
                <small>${escapeHtml(subclassSourceLabel(item))}</small>
                ${summary ? `<small class="dnd5e-wiz-subclass-summary">${escapeHtml(summary)}</small>` : ""}
            </button>`;
    }

    function renderSubclassGroup(
        current: LevelUpState,
        key: "srd" | "kiwee",
        title: string,
        items: Dnd5eLevelUpCandidate[]
    ): string {
        if (items.length === 0) return "";
        return `
            <div class="dnd5e-wiz-group" data-lvl-subclass-group="${key}">
                <h5 class="dnd5e-wiz-group-title">
                    ${escapeHtml(title)}
                    <small data-lvl-subclass-count>${items.length}</small>
                </h5>
                <div class="dnd5e-wiz-class-grid">
                    ${items.map((item) => renderSubclassCard(current, item)).join("")}
                </div>
            </div>`;
    }

    function renderSubclass(current: LevelUpState): string {
        const plan = current.plan;
        const slot = slotOf(current, "subclass");
        if (!plan || !slot) {
            if (plan && plan.subclass?.level_due === false && !plan.subclass?.current) {
                return `
                    <section class="dnd5e-wiz-section">
                        <h4><i class="fa fa-sitemap" aria-hidden="true"></i> 子职</h4>
                        <p class="dnd5e-wiz-hint">
                            ${escapeHtml(plan.class_name)} 的子职在 ${asInt(plan.subclass?.level, 0)} 级选定，
                            本等级（Lv ${plan.target_level}）不再选子职；角色卡当前没有填写子职，可在卡面直接补。
                        </p>
                    </section>`;
            }
            return "";
        }
        const groups = subclassGroups(current);
        const counts = plan.subclass?.counts || {};
        const srdCount = asInt(counts.srd, groups.srd.length);
        const kiweeCount = asInt(counts.kiwee, groups.kiwee.length);
        const searchable = groups.total > 8;
        const deepBadge = kiweeCount > 0
            ? `<span class="dnd5e-wiz-badge">候选含扩展库子职</span>`
            : `<span class="dnd5e-wiz-badge is-muted">${
                current.deepAvailable === false ? "深库未建" : "暂无扩展子职"
            } · 仅 SRD</span>`;
        const results = current.deepResults.length
            ? `<div class="dnd5e-wiz-deep-list">
                   ${current.deepResults.map((item) => `
                       <button type="button" class="dnd5e-wiz-deep-item"
                           data-lvl-action="subclass-custom" data-value="${escapeHtml(item.name)}">
                           <span>${escapeHtml(item.name)}</span>
                           <small>${escapeHtml(item.engName || "")} ${escapeHtml(item.source || "")}</small>
                       </button>`).join("")}
               </div>`
            : "";
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-sitemap" aria-hidden="true"></i> ${escapeHtml(slot.title)} ${deepBadge}</h4>
                <p class="dnd5e-wiz-hint">
                    候选来自 dnd5e-srd（${srdCount} 条）与 kiwee 扩展库（${kiweeCount} 条）；SRD 排在前面，
                    同名的扩展库条目已去重。深库未建时只剩 SRD 子职。
                </p>
                ${searchable
                    ? `<div class="dnd5e-wiz-field dnd5e-wiz-subclass-filter">
                           <label for="dnd5eLvlSubclassFilter">筛选候选（${groups.total} 条）</label>
                           <input id="dnd5eLvlSubclassFilter" type="search" data-lvl-field="subclass-filter"
                               placeholder="名称 / 英文 / 来源，如 塑能 / Evocation / PHB"
                               value="${escapeHtml(current.subclassFilter)}">
                       </div>`
                    : ""}
                ${renderSubclassGroup(current, "srd", "SRD 基础规则", groups.srd)}
                ${renderSubclassGroup(current, "kiwee", "扩展库（kiwee 深库）", groups.kiwee)}
                ${groups.kiwee.length === 0
                    ? `<p class="dnd5e-wiz-hint">扩展库里没有本职业的子职（深库未建，或该职业暂无扩展子职）。</p>`
                    : ""}
                <p class="dnd5e-wiz-hint" data-lvl-subclass-empty hidden>没有匹配的候选，换个关键字，或用下面的手填。</p>
                <div class="dnd5e-wiz-deep">
                    <div class="dnd5e-wiz-deep-row">
                        <input type="search" data-lvl-field="deep" placeholder="全库检索子职（含其他职业，如 塑能 / Evocation）"
                            value="${escapeHtml(current.deepQuery)}" aria-label="全库检索子职">
                        <button type="button" class="dnd5e-wiz-link" data-lvl-action="deep-search">
                            ${current.deepLoading ? "检索中…" : "检索"}
                        </button>
                    </div>
                    ${current.deepMessage ? `<p class="dnd5e-wiz-hint">${escapeHtml(current.deepMessage)}</p>` : ""}
                    ${results}
                </div>
                <div class="dnd5e-wiz-field">
                    <label for="dnd5eLvlSubclass">或手填子职名（须是 SRD 或 kiwee 深库里有的子职）</label>
                    <input id="dnd5eLvlSubclass" type="text" data-lvl-field="subclass-custom"
                        placeholder="例如 战争魔法" value="${escapeHtml(current.subclassCustom)}">
                </div>
                <p class="dnd5e-wiz-hint">已选：<strong>${escapeHtml(currentLabel(current))}</strong>${escapeHtml(slot.not_selected || "")}</p>
            </section>`;
    }

    /** 客户端筛选：只切 DOM 显隐，不整块重渲染（不会丢输入框焦点）。 */
    function applySubclassFilter(query: string): void {
        if (!modal) return;
        const needle = query.trim().toLowerCase();
        let visibleTotal = 0;
        modal.querySelectorAll<HTMLElement>("[data-lvl-subclass-group]").forEach((group) => {
            let visible = 0;
            group.querySelectorAll<HTMLElement>("[data-lvl-subclass-item]").forEach((item) => {
                const haystack = String(item.dataset.search || "");
                const hit = !needle || haystack.includes(needle);
                item.style.display = hit ? "" : "none";
                if (hit) visible += 1;
            });
            group.style.display = visible > 0 ? "" : "none";
            const counter = group.querySelector<HTMLElement>("[data-lvl-subclass-count]");
            if (counter) counter.textContent = String(visible);
            visibleTotal += visible;
        });
        const empty = modal.querySelector<HTMLElement>("[data-lvl-subclass-empty]");
        if (empty) empty.hidden = !needle || visibleTotal > 0;
    }

    function currentLabel(current: LevelUpState): string {
        if (current.subclassCustom.trim()) return current.subclassCustom.trim();
        const slot = slotOf(current, "subclass");
        const candidate = (slot?.candidates || []).find((item) => item.value === current.subclassPick);
        if (candidate) {
            const detail = candidate.detail || {};
            return String(detail.label_zh || detail.name || candidate.label || candidate.value);
        }
        return current.subclassPick ? current.subclassPick : "（未选）";
    }

    function renderFeatures(current: LevelUpState): string {
        const plan = current.plan;
        if (!plan) return "";
        const rows = (plan.features || []).filter((item) => item.kind !== "subclass");
        if (rows.length === 0) {
            return `
                <section class="dnd5e-wiz-section">
                    <h4><i class="fa fa-star-o" aria-hidden="true"></i> 新职业特性</h4>
                    <p class="dnd5e-wiz-hint">本等级没有新获得的职业特性（职业表见 classes.json）。</p>
                </section>`;
        }
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-star-o" aria-hidden="true"></i> 新职业特性（自动获得）</h4>
                <ul class="dnd5e-wiz-preview-list">
                    ${rows.map((item) => `<li>${escapeHtml(item.label || item.name)}
                        <small>${escapeHtml(item.name)} · Lv ${item.level}</small></li>`).join("")}
                </ul>
            </section>`;
    }

    function renderSpells(current: LevelUpState): string {
        const plan = current.plan;
        if (!plan) return "";
        const slotSlots = slotOf(current, "spell_slots");
        const known = slotOf(current, "spell_known");
        const prepared = slotOf(current, "spell_prepared");
        if (!slotSlots && !known && !prepared) {
            return `
                <section class="dnd5e-wiz-section">
                    <h4><i class="fa fa-book" aria-hidden="true"></i> 法术</h4>
                    <p class="dnd5e-wiz-hint">本职业本等级没有法术位或已知/准备名额变化（非施法职业或不晋级）。</p>
                </section>`;
        }
        const slotDiff = slotSlots
            ? (() => {
                const before = (slotSlots.detail?.before || {}) as Record<string, { max?: number }>;
                const after = (slotSlots.detail?.after || {}) as Record<string, { max?: number }>;
                const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]))
                    .sort((left, right) => asInt(left) - asInt(right));
                return `
                    <ul class="dnd5e-wiz-preview-list">
                        ${keys.map((key) => {
                            const oldMax = asInt(before[key]?.max, 0);
                            const newMax = asInt(after[key]?.max, 0);
                            return `<li>${escapeHtml(key)} 环：${oldMax} → <strong>${newMax}</strong></li>`;
                        }).join("")}
                    </ul>`;
            })()
            : `<p class="dnd5e-wiz-hint">法术位不变。</p>`;
        const knownList = known
            ? `
                <div class="dnd5e-wiz-field">
                    <label>新增已知法术（需选 ${asInt(known.max_picks, 0)} 项）</label>
                    <div class="dnd5e-wiz-skill-grid">
                        ${(known.candidates || []).map((item) => `
                            <label class="dnd5e-wiz-chip">
                                <input type="checkbox" data-lvl-field="spell-known" value="${escapeHtml(item.value)}"
                                    ${current.spellKnown.includes(item.value) ? "checked" : ""}>
                                ${escapeHtml(item.label)}
                            </label>`).join("") || `<p class="dnd5e-wiz-hint">没有可选的新法术。</p>`}
                    </div>
                </div>`
            : "";
        const preparedList = prepared
            ? `
                <div class="dnd5e-wiz-field">
                    <label>准备法术（最多 ${asInt(prepared.max_picks, 0)} 项，可留空保持原有列表）</label>
                    <div class="dnd5e-wiz-skill-grid">
                        ${(prepared.candidates || []).map((item) => `
                            <label class="dnd5e-wiz-chip">
                                <input type="checkbox" data-lvl-field="spell-prepared" value="${escapeHtml(item.value)}"
                                    ${current.spellPrepared.includes(item.value) ? "checked" : ""}>
                                ${escapeHtml(item.label)}
                            </label>`).join("")}
                    </div>
                </div>`
            : "";
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-book" aria-hidden="true"></i> 法术</h4>
                <p class="dnd5e-wiz-hint">法术位变化</p>
                ${slotDiff}
                ${knownList}
                ${preparedList}
            </section>`;
    }

    function renderPreview(current: LevelUpState): string {
        const plan = current.plan;
        if (!plan) return "";
        const hp = plan.hit_points;
        const total = totalFor(current.hpMode, hp);
        const newMax = asInt(hp.before_max, 0) + total;
        const pbSlot = slotOf(current, "proficiency_bonus");
        const pbBefore = asInt((pbSlot?.detail || {}).from, proficiencyBonus(plan.current_level));
        const pbAfter = asInt((pbSlot?.detail || {}).to, proficiencyBonus(plan.target_level));
        const abilities = abilitiesOf(current);
        const asiRows = current.asiPicks
            .filter((key) => key in abilities)
            .map((key) => {
                const label = ABILITIES.find((item) => item.key === key)?.label || key;
                const score = asInt(abilities[key], 10);
                return `<li>${escapeHtml(label)}：${score} → <strong>${Math.min(ASI_CAP, score + asiAmount(current))}</strong></li>`;
            }).join("");
        const featureRows = (plan.features || [])
            .filter((item) => item.kind !== "subclass")
            .map((item) => `<li>${escapeHtml(item.label || item.name)}</li>`).join("");
        const subclassRow = slotOf(current, "subclass")
            ? `<li>子职：<strong>${escapeHtml(currentLabel(current))}</strong></li>`
            : "";
        const feats = current.asiPicks.filter((item) => String(item).startsWith("feat:"))
            .map((item) => {
                const candidate = (slotOf(current, "asi")?.candidates || []).find((entry) => entry.value === item);
                return `<li>专长：<strong>${escapeHtml(candidateLabel(candidate, item))}</strong></li>`;
            }).join("");
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-id-badge" aria-hidden="true"></i> 升级预览</h4>
                <ul class="dnd5e-wiz-preview-list">
                    <li>等级：${plan.current_level} → <strong>${plan.target_level}</strong></li>
                    <li>HP 上限：${asInt(hp.before_max, 0)} → <strong>${newMax}</strong>（${signed(total)}）</li>
                    <li>熟练加值：${signed(pbBefore)} → <strong>${signed(pbAfter)}</strong></li>
                    ${subclassRow}
                    ${asiRows}
                    ${feats}
                    ${featureRows}
                    <li>经验值：${asInt(plan.xp?.before, 0)} → <strong>${asInt(plan.xp?.after ?? plan.xp?.before, 0)}</strong>（手动升级不动 XP）</li>
                </ul>
                <p class="dnd5e-wiz-hint">
                    确认后由后端再校验一次全部槽位（属性上限 ${ASI_CAP}、子职合法、法术名额），
                    通过后走 apply_save 写卡；验证失败不会留下半成品。
                </p>
                ${renderWarnings(plan)}
            </section>`;
    }

    function renderStepBody(current: LevelUpState): string {
        if (current.error) {
            const errorBox = `
                <section class="dnd5e-wiz-section">
                    <h4><i class="fa fa-exclamation-triangle" aria-hidden="true"></i> 无法升级</h4>
                    <p class="dnd5e-wiz-hint">${escapeHtml(current.error)}</p>
                </section>`;
            if (current.step === 1 || !current.plan) return errorBox;
        }
        if (!current.plan) return renderStepConfirm(current);
        if (current.step === 1) return renderStepConfirm(current);
        if (current.step === 2) return renderStepHitPoints(current);
        if (current.step === 3) {
            return `${renderFeatures(current)}${renderSubclass(current)}${renderAbilityScoreImprovement(current)}`;
        }
        return `${renderStepConfirm(current)}${renderSpells(current)}${renderPreview(current)}`;
    }

    function renderFooter(current: LevelUpState): string {
        const isFirst = current.step === 1;
        const isLast = current.step === 4;
        const blocked = !current.plan || Boolean(current.error);
        return `
            <button type="button" class="btn btn-secondary" data-lvl-action="cancel">取消</button>
            <div class="dnd5e-wiz-footer-right">
                ${isFirst ? "" : `<button type="button" class="btn btn-outline-secondary" data-lvl-action="prev">上一步</button>`}
                ${isLast
                    ? `<button type="button" class="btn btn-primary" data-lvl-action="apply" ${current.saving || blocked ? "disabled" : ""}>
                           ${current.saving ? "正在升级…" : "确认升级"}</button>`
                    : `<button type="button" class="btn btn-primary" data-lvl-action="next" ${blocked ? "disabled" : ""}>下一步</button>`}
            </div>`;
    }

    function renderBody(): void {
        const current = state;
        if (!current || !modal) return;
        const steps = modal.querySelector<HTMLElement>("[data-lvl-steps]");
        const body = modal.querySelector<HTMLElement>("[data-lvl-body]");
        const footer = modal.querySelector<HTMLElement>("[data-lvl-footer]");
        const title = modal.querySelector<HTMLElement>(".modal-title");
        if (title) {
            title.textContent = current.plan
                ? `升级：${current.cardName || "角色卡"} · Lv ${current.plan.current_level} → ${current.plan.target_level}`
                : `升级：${current.cardName || "角色卡"}`;
        }
        if (steps) steps.innerHTML = renderStepper(current);
        if (body) body.innerHTML = renderStepBody(current);
        if (footer) footer.innerHTML = renderFooter(current);
        // 重渲染后按已存的关键字恢复筛选（输入框内容与显隐同源）。
        applySubclassFilter(current.subclassFilter);
    }

    // --- 后端 -----------------------------------------------------------------

    function endpoint(current: LevelUpState): string {
        return `/api/plugins/dnd5e/characters/${encodeURIComponent(current.cardId)}/level-up`;
    }

    async function requestPlan(current: LevelUpState, silent: boolean = false): Promise<void> {
        if (!current.cardId) {
            current.loading = false;
            current.error = "角色卡没有 id，无法升级。";
            renderBody();
            return;
        }
        const target = current.plan ? current.plan.target_level : levelOfCard(current) + 1;
        current.loading = true;
        if (!silent) renderBody();
        try {
            const response = await TrpgApi.post<ApiResponse<Dnd5eLevelUpPlan>>(endpoint(current), {
                target_level: target,
            });
            if (!response?.success || !response.data) {
                current.loading = false;
                current.error = String(response?.message || response?.error || "生成升级计划失败。");
                renderBody();
                return;
            }
            applyPlan(response.data);
        } catch (error) {
            current.loading = false;
            current.error = `生成升级计划失败：${String((error as Error)?.message || error)}`;
        }
        renderBody();
    }

    function levelOfCard(current: LevelUpState): number {
        const sheet = (current.options.card?.sheet && typeof current.options.card.sheet === "object")
            ? current.options.card.sheet as Record<string, unknown>
            : {};
        return asInt(sheet.level, 1);
    }

    async function probeDeepDb(current: LevelUpState): Promise<void> {
        if (current.deepAvailable !== null) return;
        if (!deepProbe) {
            deepProbe = (async () => {
                try {
                    const response = await TrpgApi.get<ApiResponse<{ available?: boolean }>>(`${RULES_API}/status`);
                    return Boolean(response?.data?.available);
                } catch {
                    return false;
                }
            })();
        }
        current.deepAvailable = await deepProbe;
        renderBody();
    }

    async function runDeepSearch(current: LevelUpState): Promise<void> {
        const query = current.deepQuery.trim();
        if (!query) {
            current.deepResults = [];
            current.deepMessage = "先输入关键字（例如 塑能 / Evocation）。";
            renderBody();
            return;
        }
        current.deepLoading = true;
        renderBody();
        try {
            // 深库检索：(rules/search) 的列表键是 results，条目带 name / eng_name / source。
            const response = await TrpgApi.get<ApiResponse<{
                available?: boolean;
                results?: Array<Record<string, unknown>>;
                total?: number;
            }>>(`${RULES_API}/search?category=subclass&limit=8&q=${encodeURIComponent(query)}`);
            const data = response?.data || {};
            const rows = Array.isArray(data.results) ? data.results : [];
            if (data.available === false) {
                current.deepAvailable = false;
                current.deepResults = [];
                current.deepMessage = "深库未建：子职候选回落到 dnd5e-srd 的 classes.json。";
            } else {
                current.deepAvailable = true;
                current.deepResults = rows.map((row) => ({
                    uid: String(row.uid || ""),
                    name: String(row.name || ""),
                    engName: String(row.eng_name || row.engName || ""),
                    source: String(row.source || ""),
                })).filter((item) => item.name);
                current.deepMessage = current.deepResults.length
                    ? `深库命中 ${current.deepResults.length} 条，点击即用（写入子职名）。`
                    : "深库没有匹配条目，可用下方手填。";
            }
        } catch (error) {
            current.deepResults = [];
            current.deepMessage = `深库检索失败：${String((error as Error)?.message || error)}`;
        }
        current.deepLoading = false;
        renderBody();
    }

    function buildChoices(current: LevelUpState): Array<{ slot_id: string; values: string[] }> {
        const plan = current.plan;
        const choices: Array<{ slot_id: string; values: string[] }> = [];
        if (!plan) return choices;
        const asiPicks = current.asiPicks.filter((item) => !String(item).startsWith("feat:"))
            .slice(0, asiLimit(current));
        for (const slot of plan.slots || []) {
            if (slot.kind === "hp") {
                choices.push({ slot_id: slot.id, values: [current.hpMode] });
            } else if (slot.kind === "asi") {
                const featPick = current.asiPicks.find((item) => String(item).startsWith("feat:"));
                const values = featPick ? [String(featPick)] : asiPicks.map((item) => String(item));
                if (values.length) choices.push({ slot_id: slot.id, values });
            } else if (slot.kind === "subclass") {
                const custom = current.subclassCustom.trim();
                choices.push({ slot_id: slot.id, values: [custom || current.subclassPick] });
            } else if (slot.kind === "spell_known") {
                choices.push({ slot_id: slot.id, values: current.spellKnown.slice(0, asInt(slot.max_picks, 0)) });
            } else if (slot.kind === "spell_prepared") {
                // 准备列表是整表替换：没改动就不提交，别把卡上的准备法术清空。
                const preset = Array.isArray(slot.default) ? slot.default.map((item) => String(item)) : [];
                const picks = current.spellPrepared.slice(0, asInt(slot.max_picks, 0));
                const same = picks.length === preset.length && picks.every((item, index) => item === preset[index]);
                if (!same) choices.push({ slot_id: slot.id, values: picks });
            } else if (slot.kind === "spell_slots" || slot.kind === "proficiency_bonus") {
                choices.push({ slot_id: slot.id, values: ["apply"] });
            }
        }
        return choices;
    }

    function validateStep(current: LevelUpState, step: Dnd5eLevelUpStep): string {
        const plan = current.plan;
        if (!plan) return "升级计划还没就绪。";
        if (step === 3) {
            const asi = slotOf(current, "asi");
            if (asi) {
                const featPick = current.asiPicks.find((item) => String(item).startsWith("feat:"));
                const picks = current.asiPicks.filter((item) => !String(item).startsWith("feat:"));
                if (!featPick) {
                    if (picks.length === 0) return "属性提升是必选：选一项 +2、两项各 +1，或改选专长。";
                    if (current.asiMode === "plus2" && picks.length !== 1) return "「+2 一项」只能选一项属性。";
                    if (current.asiMode === "plus1" && picks.length !== 2) return "「+1 两项」需要选两项不同属性。";
                }
            }
            if (slotOf(current, "subclass") && !current.subclassCustom.trim() && !current.subclassPick) {
                return "这个等级必须选定子职。";
            }
        }
        if (step === 4) {
            const known = slotOf(current, "spell_known");
            if (known && current.spellKnown.length !== asInt(known.max_picks, 0)) {
                return `新增已知法术需要选择 ${asInt(known.max_picks, 0)} 项。`;
            }
        }
        return "";
    }

    function gotoStep(step: Dnd5eLevelUpStep): void {
        const current = state;
        if (!current) return;
        if (step > current.step) {
            for (let index = current.step; index < step; index += 1) {
                const message = validateStep(current, index as Dnd5eLevelUpStep);
                if (message) {
                    notify(message, "warning");
                    current.step = index as Dnd5eLevelUpStep;
                    renderBody();
                    return;
                }
            }
        }
        current.step = step;
        renderBody();
        modal?.querySelector<HTMLElement>("[data-lvl-body]")?.scrollTo?.({ top: 0 });
    }

    async function applyUpgrade(): Promise<void> {
        const current = state;
        if (!current || current.saving) return;
        const plan = current.plan;
        if (!plan) return;
        const message = validateStep(current, 3) || validateStep(current, 4);
        if (message) {
            notify(message, "warning");
            renderBody();
            return;
        }
        current.saving = true;
        renderBody();
        try {
            const response = await TrpgApi.post<ApiResponse<Dnd5eLevelUpApplyResult>>(endpoint(current), {
                plan_id: plan.plan_id,
                choices: buildChoices(current),
            });
            if (!response?.success) {
                notify(response?.message || response?.error || "升级失败", "error");
                return;
            }
            const data = response.data;
            const saved = data?.card || (data ? { ...current.options.card, sheet: data.sheet } : null);
            notify(
                `升级完成：Lv ${plan.current_level} → ${plan.target_level}${data?.changes?.length ? `（${data.changes.length} 项变化）` : ""}`,
                "success"
            );
            current.saving = false;
            close();
            if (saved) current.options.onSaved?.(saved as Record<string, unknown>);
        } catch (error) {
            notify(`升级失败：${String((error as Error)?.message || error)}`, "error");
        } finally {
            current.saving = false;
            if (modal?.isConnected) renderBody();
        }
    }

    // --- 交互 -----------------------------------------------------------------

    function toggleAsi(ability: Dnd5eLevelUpAbility): void {
        const current = state;
        if (!current) return;
        const picked = current.asiPicks.includes(ability);
        if (picked) {
            current.asiPicks = current.asiPicks.filter((item) => item !== ability);
            renderBody();
            return;
        }
        if (!asiAvailable(current, ability)) {
            notify(`「${ABILITIES.find((item) => item.key === ability)?.label || ability}」加到上限 ${ASI_CAP} 会超限。`, "warning");
            return;
        }
        const limit = asiLimit(current);
        current.asiPicks = [...current.asiPicks, ability].slice(0, limit);
        renderBody();
    }

    function onInput(event: Event): void {
        const current = state;
        if (!current) return;
        const target = event.target as HTMLInputElement | null;
        if (!target) return;
        const field = target.dataset.lvlField || "";
        if (field === "deep") {
            current.deepQuery = target.value;
            return;
        }
        if (field === "subclass-custom") {
            current.subclassCustom = target.value;
            return;
        }
        if (field === "subclass-filter") {
            current.subclassFilter = target.value;
            applySubclassFilter(current.subclassFilter);
            return;
        }
        if (field === "spell-known" || field === "spell-prepared") {
            const property: "spellKnown" | "spellPrepared" = field === "spell-known" ? "spellKnown" : "spellPrepared";
            const values = new Set(current[property]);
            if (target.checked) values.add(target.value);
            else values.delete(target.value);
            const slot = slotOf(current, field === "spell-known" ? "spell_known" : "spell_prepared");
            const limit = asInt(slot?.max_picks, 0);
            const list = Array.from(values);
            if (limit > 0 && list.length > limit) {
                notify(`最多选择 ${limit} 项。`, "warning");
                target.checked = false;
                return;
            }
            current[property] = list;
            renderBody();
        }
    }

    function onChange(event: Event): void {
        const current = state;
        if (!current) return;
        const target = event.target as HTMLInputElement | HTMLSelectElement | null;
        const field = target?.dataset?.lvlField || "";
        if (field === "feat" && target) {
            const value = (target as HTMLSelectElement).value;
            current.asiPicks = value ? [value as Dnd5eLevelUpAbility] : [];
            renderBody();
        }
    }

    function onClick(event: Event): void {
        const current = state;
        if (!current) return;
        const trigger = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-lvl-action]");
        if (!trigger) return;
        const action = trigger.dataset.lvlAction || "";
        event.preventDefault();
        if (action === "cancel") {
            close();
            return;
        }
        if (action === "next") {
            gotoStep((current.step + 1) as Dnd5eLevelUpStep);
            return;
        }
        if (action === "prev") {
            gotoStep((current.step - 1) as Dnd5eLevelUpStep);
            return;
        }
        if (action === "apply") {
            void applyUpgrade();
            return;
        }
        if (action === "hp-mode") {
            current.hpMode = trigger.dataset.mode === "average" ? "average" : "roll";
            renderBody();
            return;
        }
        if (action === "reroll") {
            void requestPlan(current);
            return;
        }
        if (action === "asi-mode") {
            current.asiMode = trigger.dataset.mode === "plus1" ? "plus1" : "plus2";
            const limit = asiLimit(current);
            current.asiPicks = current.asiPicks.slice(0, limit);
            renderBody();
            return;
        }
        if (action === "asi-toggle") {
            const ability = String(trigger.dataset.ability || "") as Dnd5eLevelUpAbility;
            if (ability) toggleAsi(ability);
            return;
        }
        if (action === "subclass-pick") {
            current.subclassPick = String(trigger.dataset.value || "");
            current.subclassCustom = "";
            renderBody();
            return;
        }
        if (action === "subclass-custom") {
            current.subclassCustom = String(trigger.dataset.value || "");
            renderBody();
            return;
        }
        if (action === "deep-search") {
            void runDeepSearch(current);
        }
    }

    function onKeydown(event: KeyboardEvent): void {
        const current = state;
        if (!current) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            close();
            return;
        }
        if (event.key !== "Enter") return;
        const target = event.target as HTMLElement | null;
        const tag = (target?.tagName || "").toLowerCase();
        if (tag === "textarea" || tag === "button" || tag === "a") return;
        if ((target as HTMLInputElement | null)?.dataset?.lvlField === "deep") {
            event.preventDefault();
            void runDeepSearch(current);
            return;
        }
        if ((target as HTMLInputElement | null)?.dataset?.lvlField === "subclass-filter") {
            // 筛选框里的回车只收键盘焦点，不当作「下一步」。
            event.preventDefault();
            return;
        }
        event.preventDefault();
        if (current.step < 4) gotoStep((current.step + 1) as Dnd5eLevelUpStep);
        else void applyUpgrade();
    }

    // --- 生命周期 --------------------------------------------------------------

    function ensureModal(): { element: HTMLElement; instance: BootstrapModalInstance } | null {
        if (modal && instance) return { element: modal, instance };
        if (typeof bootstrap === "undefined") return null;
        const element = document.createElement("div");
        element.id = "dnd5eLevelUpModal";
        element.className = "modal fade dnd5e-wiz-modal";
        element.tabIndex = -1;
        element.innerHTML = `
            <div class="modal-dialog modal-lg modal-dialog-scrollable modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">升级</h5>
                        <button type="button" class="btn-close" data-lvl-action="cancel" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div data-lvl-steps></div>
                        <div data-lvl-body></div>
                    </div>
                    <div class="modal-footer" data-lvl-footer></div>
                </div>
            </div>`;
        element.addEventListener("click", onClick);
        element.addEventListener("change", onChange);
        element.addEventListener("input", onInput);
        element.addEventListener("keydown", onKeydown);
        element.addEventListener("hidden.bs.modal", () => {
            const body = element.querySelector<HTMLElement>("[data-lvl-body]");
            if (body) body.innerHTML = "";
        });
        document.body.appendChild(element);
        modal = element;
        instance = new bootstrap.Modal(element);
        return { element, instance };
    }

    function open(options: Dnd5eLevelUpOptions): void {
        const built = ensureModal();
        if (!built) {
            notify("升级向导窗口不可用（bootstrap 未加载）", "error");
            return;
        }
        state = freshState(options);
        deepProbe = null;
        renderBody();
        built.instance.show();
        void requestPlan(state);
        void probeDeepDb(state);
        global.setTimeout(() => {
            modal?.querySelector<HTMLButtonElement>('[data-lvl-action="next"]')?.focus();
        }, 180);
    }

    function close(): void {
        instance?.hide();
    }

    global.TrpgDnd5eLevelUp = {
        open,
        close,
        isOpen: () => Boolean(state),
    };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
