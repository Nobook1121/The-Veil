// D&D 5e 角色卡面（E7 第一批）。
//
// 只做卡面：按 E0 内容壳（/api/rulesets 的 schema.sections）渲染六属性、熟练、
// HP/AC/速度、法术位、装备与状态，并提供创建 / 编辑 / 保存的通往返。
// 房间整体布局、骰桌与战斗面板不在本文件内。

interface Dnd5eAbilityKey {
    str: "STR";
    dex: "DEX";
    con: "CON";
    int: "INT";
    wis: "WIS";
    cha: "CHA";
}

type Dnd5eAbility = keyof Dnd5eAbilityKey;

interface Dnd5eProficiencies {
    saves?: string[];
    skills?: string[];
    expertise?: string[];
    armor?: string[];
    weapons?: string[];
    tools?: string[];
    languages?: string[];
}

interface Dnd5eSlotRow {
    max?: number;
    used?: number;
}

interface Dnd5eSpellcasting {
    ability?: string;
    known?: string[];
    prepared?: string[];
    slots?: Record<string, Dnd5eSlotRow>;
}

interface Dnd5eEquipmentItem {
    name?: string;
    quantity?: number;
    weight?: number;
    notes?: string;
}

interface Dnd5eSheet {
    level?: number;
    class?: string;
    subclass?: string;
    race?: string;
    background?: string;
    alignment?: string;
    xp?: number;
    abilities?: Partial<Record<Dnd5eAbility, number>>;
    proficiencies?: Dnd5eProficiencies;
    hp?: { current?: number; max?: number; temp?: number };
    armorClass?: number;
    speed?: number;
    hitDice?: string;
    spellcasting?: Dnd5eSpellcasting;
    equipment?: Dnd5eEquipmentItem[];
    features?: string[];
    notes?: string;
    bio?: string;
    status?: string[] | string;
}

interface Dnd5eCardInput {
    id?: string;
    name?: string;
    rulesetId?: string;
    playerId?: string;
    avatar?: string;
    updatedAt?: string;
    sheet?: Dnd5eSheet;
    level?: number;
    class?: string;
    race?: string;
    currentHp?: number;
    maxHp?: number;
    tempHp?: number;
    armorClass?: number;
    proficiencyBonus?: number;
    initiative?: number;
    spellSaveDC?: number;
    spellAttackBonus?: number;
    [key: string]: unknown;
}

interface Dnd5eCard extends Dnd5eCardInput {
    id: string;
    name: string;
    sheet: Dnd5eSheet;
}

interface Dnd5eSheetFieldDescriptor {
    key: string;
    type?: string;
    read_only?: boolean;
}

interface Dnd5eSheetSectionDescriptor {
    key: string;
    fields?: Dnd5eSheetFieldDescriptor[];
}

interface Dnd5eRulesetRow {
    id: string;
    display_name?: string;
    schema?: { sections?: Dnd5eSheetSectionDescriptor[] };
}

interface Dnd5eModuleApi {
    createCharacterCard: (input?: Dnd5eCardInput) => Dnd5eCard;
    listCharacterCards: () => Dnd5eCard[];
    getCharacterCardSnapshot: (cardId: string) => Record<string, unknown> | null;
    renderCharacterDetail: (card: Dnd5eCard, roomDiff?: unknown) => string;
    initCharacterSheet: () => void;
}

(function installDnd5eCharacterSheet(global: Window & typeof globalThis): void {
    "use strict";

    const RULESET_ID = "dnd5e";
    const ABILITIES: Array<{ key: Dnd5eAbility; short: string; label: string }> = [
        { key: "str", short: "STR", label: "力量" },
        { key: "dex", short: "DEX", label: "敏捷" },
        { key: "con", short: "CON", label: "体质" },
        { key: "int", short: "INT", label: "智力" },
        { key: "wis", short: "WIS", label: "感知" },
        { key: "cha", short: "CHA", label: "魅力" },
    ];
    const FIELD_LABELS: Record<string, string> = {
        level: "等级",
        class: "职业",
        subclass: "子职",
        race: "种族",
        background: "背景",
        alignment: "阵营",
        xp: "经验值",
        str: "力量",
        dex: "敏捷",
        con: "体质",
        int: "智力",
        wis: "感知",
        cha: "魅力",
        saves: "豁免熟练",
        skills: "技能熟练",
        expertise: "专精",
        armor: "护甲熟练",
        weapons: "武器熟练",
        tools: "工具熟练",
        languages: "语言",
        hp: "生命值",
        armorClass: "护甲等级",
        speed: "速度",
        initiative: "先攻",
        hitDice: "生命骰",
        spellcasting: "施法",
        spellSaveDC: "法术豁免 DC",
        spellAttackBonus: "法术攻击加值",
        equipment: "装备",
        features: "特性",
        notes: "备注",
    };
    const FALLBACK_SECTIONS: Dnd5eSheetSectionDescriptor[] = [
        { key: "identity", fields: [{ key: "level" }, { key: "class" }, { key: "subclass" }, { key: "race" }, { key: "background" }, { key: "alignment" }, { key: "xp" }] },
        { key: "abilities", fields: ABILITIES.map((item) => ({ key: item.key })) },
        { key: "proficiencies", fields: [{ key: "saves" }, { key: "skills" }, { key: "expertise" }, { key: "armor" }, { key: "weapons" }, { key: "tools" }, { key: "languages" }] },
        { key: "combat", fields: [{ key: "hp" }, { key: "armorClass" }, { key: "speed" }, { key: "initiative" }, { key: "hitDice" }] },
        { key: "spellcasting", fields: [{ key: "spellcasting" }, { key: "spellSaveDC" }, { key: "spellAttackBonus" }] },
        { key: "inventory", fields: [{ key: "equipment" }, { key: "features" }, { key: "notes" }] },
    ];

    let cards: Dnd5eCard[] = [];
    let activeCardId = "";
    let active = false;
    let loading: Promise<void> | null = null;
    let initialLoad: Promise<void> | null = null;
    let sections: Dnd5eSheetSectionDescriptor[] = FALLBACK_SECTIONS;
    let editorModal: BootstrapModalInstance | null = null;
    let editorDraft: Dnd5eCard | null = null;

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

    function asInt(value: unknown, fallback = 0): number {
        const number = Number(value);
        return Number.isFinite(number) ? Math.trunc(number) : fallback;
    }

    function clamp(value: unknown, minimum: number, maximum: number, fallback: number): number {
        return Math.max(minimum, Math.min(maximum, asInt(value, fallback)));
    }

    function stringList(value: unknown): string[] {
        if (!Array.isArray(value)) return [];
        return value.map((item) => String(item ?? "").trim()).filter(Boolean);
    }

    function splitList(raw: unknown): string[] {
        return String(raw ?? "")
            .split(/[,，;；\n]+/)
            .map((item) => item.trim())
            .filter(Boolean);
    }

    function emptySheet(): Dnd5eSheet {
        const abilities: Partial<Record<Dnd5eAbility, number>> = {};
        ABILITIES.forEach((item) => { abilities[item.key] = 10; });
        return {
            level: 1,
            class: "",
            race: "",
            background: "",
            alignment: "",
            xp: 0,
            abilities,
            proficiencies: { saves: [], skills: [], expertise: [], armor: [], weapons: [], tools: [], languages: [] },
            hp: { current: 8, max: 8, temp: 0 },
            armorClass: 10,
            speed: 30,
            hitDice: "",
            spellcasting: { ability: "", known: [], prepared: [], slots: {} },
            equipment: [],
            features: [],
            notes: "",
            bio: "一位初出茅庐的冒险者。",
            status: [],
        };
    }

    function normalizeSheet(raw: Dnd5eSheet | null | undefined): Dnd5eSheet {
        const base = emptySheet();
        const source = raw && typeof raw === "object" ? raw : {};
        base.level = clamp(source.level, 1, 20, 1);
        base.class = String(source.class ?? "");
        base.subclass = String(source.subclass ?? "");
        base.race = String(source.race ?? "");
        base.background = String(source.background ?? "");
        base.alignment = String(source.alignment ?? "");
        base.xp = Math.max(0, asInt(source.xp, 0));
        base.abilities = { ...base.abilities };
        ABILITIES.forEach((item) => {
            base.abilities![item.key] = clamp(source.abilities?.[item.key], 1, 30, 10);
        });
        const proficiencies = source.proficiencies || {};
        base.proficiencies = {
            saves: stringList(proficiencies.saves),
            skills: stringList(proficiencies.skills),
            expertise: stringList(proficiencies.expertise),
            armor: stringList(proficiencies.armor),
            weapons: stringList(proficiencies.weapons),
            tools: stringList(proficiencies.tools),
            languages: stringList(proficiencies.languages),
        };
        const hp = source.hp || {};
        const max = Math.max(1, asInt(hp.max, 8));
        base.hp = {
            max,
            current: clamp(hp.current, 0, max, max),
            temp: Math.max(0, asInt(hp.temp, 0)),
        };
        base.armorClass = Math.max(0, asInt(source.armorClass, 10));
        base.speed = Math.max(0, asInt(source.speed, 30));
        base.hitDice = String(source.hitDice ?? "");
        const casting = source.spellcasting || {};
        const slots: Record<string, Dnd5eSlotRow> = {};
        Object.keys(casting.slots || {}).forEach((level) => {
            const row = casting.slots?.[level] || {};
            slots[level] = { max: Math.max(0, asInt(row.max, 0)), used: Math.max(0, asInt(row.used, 0)) };
        });
        base.spellcasting = {
            ability: String(casting.ability ?? ""),
            known: stringList(casting.known),
            prepared: stringList(casting.prepared),
            slots,
        };
        base.equipment = Array.isArray(source.equipment)
            ? source.equipment.map((item) => ({
                name: String(item?.name ?? ""),
                quantity: Math.max(1, asInt(item?.quantity, 1)),
                weight: Math.max(0, Number(item?.weight) || 0),
                notes: String(item?.notes ?? ""),
            }))
            : [];
        base.features = stringList(source.features);
        base.notes = String(source.notes ?? "");
        base.bio = String(source.bio ?? base.bio ?? "");
        base.status = Array.isArray(source.status) ? stringList(source.status) : String(source.status ?? "");
        return base;
    }

    function createCharacterCard(input: Dnd5eCardInput = {}): Dnd5eCard {
        const sheet = normalizeSheet((input.sheet || input) as Dnd5eSheet);
        const card: Dnd5eCard = {
            ...input,
            id: String(input.id || `dnd5e-${Date.now().toString(36)}`),
            name: String(input.name || "未命名冒险者"),
            rulesetId: RULESET_ID,
            sheet,
        };
        card.level = sheet.level ?? 1;
        card.class = sheet.class ?? "";
        card.race = sheet.race ?? "";
        card.currentHp = sheet.hp?.current ?? 0;
        card.maxHp = sheet.hp?.max ?? 1;
        card.tempHp = sheet.hp?.temp ?? 0;
        card.armorClass = input.armorClass ?? sheet.armorClass ?? 0;
        return card;
    }

    function abilityModifier(score: unknown): number {
        return Math.floor((asInt(score, 10) - 10) / 2);
    }

    function signed(value: number): string {
        return value >= 0 ? `+${value}` : `${value}`;
    }

    function currentPlayerId(): string {
        const user = global.currentUser;
        if (!user) return "";
        return String(user.user_id ?? user.username ?? "");
    }

    function canEditCard(card: Dnd5eCard): boolean {
        if (!card.playerId) return true;
        const user = global.currentUser;
        if (!user) return false;
        const role = String(user.role || "").toUpperCase();
        if (role === "ADMIN" || role === "OWNER") return true;
        return card.playerId === String(user.user_id ?? "") || card.playerId === String(user.username ?? "");
    }

    function supportsRuleset(rulesetId?: string | null): boolean {
        const key = String(rulesetId ?? "").trim().toLowerCase();
        return !key || key === RULESET_ID;
    }

    async function loadRulesetSections(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<Dnd5eRulesetRow[]>>("/api/rulesets");
            const rows = response?.success && Array.isArray(response.data) ? response.data : [];
            const row = rows.find((item) => String(item?.id || "").toLowerCase() === RULESET_ID);
            const declared = row?.schema?.sections;
            if (Array.isArray(declared) && declared.length) sections = declared;
        } catch {
            // 接口不可达时用本地声明表；标签与字段键仍与插件内容壳保持一致。
            sections = FALLBACK_SECTIONS;
        }
    }

    function listCharacterCards(): Dnd5eCard[] {
        return cards;
    }

    function getCharacterCardSnapshot(cardId: string): Record<string, unknown> | null {
        const card = cards.find((item) => item.id === cardId);
        if (!card) return null;
        // 房间成员保存的是快照本身；返回完整卡才能保留 5e 的 sheet 结构。
        return {
            ...card,
            rulesetId: RULESET_ID,
            level: card.sheet.level,
            class: card.sheet.class,
            hp: card.sheet.hp,
            currentHp: card.sheet.hp?.current,
            maxHp: card.sheet.hp?.max,
            tempHp: card.sheet.hp?.temp,
            armorClass: card.armorClass,
            spellcasting: card.sheet.spellcasting,
        };
    }

    function statCard(label: string, value: unknown, temp?: boolean): string {
        const className = temp ? "character-stat-card is-room-temp" : "character-stat-card";
        const text = value === null || value === undefined || String(value).trim() === "" ? "未填写" : String(value);
        return `<div class="${className}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text)}</strong></div>`;
    }

    function renderIdentity(card: Dnd5eCard): string {
        const sheet = card.sheet;
        return `
            <section class="character-detail-band">
                <h4>基础信息</h4>
                <div class="character-detail-info-grid">
                    ${statCard("姓名", card.name)}
                    ${statCard("等级", sheet.level)}
                    ${statCard("职业", sheet.class)}
                    ${statCard("子职", sheet.subclass)}
                    ${statCard("种族", sheet.race)}
                    ${statCard("背景", sheet.background)}
                    ${statCard("阵营", sheet.alignment)}
                    ${statCard("经验值", sheet.xp)}
                </div>
            </section>
        `;
    }

    function renderAbilities(card: Dnd5eCard): string {
        const abilities = card.sheet.abilities || {};
        const items = ABILITIES.map((item) => {
            const score = asInt(abilities[item.key], 10);
            return `
                <div class="character-stat-card">
                    <span>${escapeHtml(`${item.label} ${item.short}`)}</span>
                    <strong>${score}</strong>
                    <small>${escapeHtml(signed(abilityModifier(score)))}</small>
                </div>
            `;
        }).join("");
        return `
            <section class="character-detail-band">
                <h4>属性</h4>
                <div class="character-attribute-grid">${items}</div>
            </section>
        `;
    }

    function renderProficiencies(card: Dnd5eCard): string {
        const proficiencies = card.sheet.proficiencies || {};
        const groups: Array<[string, string[]]> = [
            ["豁免", stringList(proficiencies.saves)],
            ["技能", stringList(proficiencies.skills)],
            ["专精", stringList(proficiencies.expertise)],
            ["护甲", stringList(proficiencies.armor)],
            ["武器", stringList(proficiencies.weapons)],
            ["工具", stringList(proficiencies.tools)],
            ["语言", stringList(proficiencies.languages)],
        ];
        const rows = groups.map(([label, values]) => `
            <tr>
                <td>${escapeHtml(label)}</td>
                <td>${values.length ? values.map((value) => `<span class="badge bg-secondary me-1">${escapeHtml(value)}</span>`).join("") : "—"}</td>
            </tr>
        `).join("");
        return `
            <section class="character-detail-band character-detail-band-wide">
                <h4>熟练</h4>
                <div class="character-detail-table-shell">
                    <table class="character-detail-table"><tbody>${rows}</tbody></table>
                </div>
            </section>
        `;
    }

    function renderCombat(card: Dnd5eCard, roomDiff: unknown): string {
        const sheet = card.sheet;
        const hp = sheet.hp || {};
        const diff = roomDiff as { vitals?: { hp?: { current?: number; saved?: number } } } | null;
        const roomHp = diff?.vitals?.hp;
        const currentHp = roomHp?.current ?? hp.current;
        const proficiency = card.proficiencyBonus ?? (2 + Math.floor((asInt(sheet.level, 1) - 1) / 4));
        const armorClass = card.armorClass ?? sheet.armorClass;
        const initiative = card.initiative ?? abilityModifier(sheet.abilities?.dex);
        return `
            <section class="character-detail-band character-detail-band-vitals">
                <h4>战斗</h4>
                <div class="character-vital-grid">
                    ${statCard("生命值", `${currentHp} / ${hp.max}${hp.temp ? ` (+${hp.temp})` : ""}`, Boolean(roomHp))}
                    ${statCard("护甲等级", armorClass)}
                    ${statCard("速度", sheet.speed ? `${sheet.speed} 尺` : "")}
                    ${statCard("先攻", signed(initiative))}
                    ${statCard("熟练加值", signed(proficiency))}
                    ${statCard("生命骰", sheet.hitDice)}
                </div>
            </section>
        `;
    }

    function renderSpellcasting(card: Dnd5eCard): string {
        const casting = card.sheet.spellcasting || {};
        const slots = casting.slots || {};
        const levels = Object.keys(slots).sort((a, b) => asInt(a) - asInt(b));
        const slotRows = levels.map((level) => {
            const row = slots[level] || {};
            const used = asInt(row.used, 0);
            const max = asInt(row.max, 0);
            return `<span class="badge bg-secondary me-1">${escapeHtml(level)} 环 ${used}/${max}</span>`;
        }).join("");
        const known = stringList(casting.known);
        const prepared = stringList(casting.prepared);
        const abilityKey = String(casting.ability || "").toLowerCase() as Dnd5eAbility;
        const hasCasting = Boolean(casting.ability) && ABILITIES.some((item) => item.key === abilityKey);
        const abilityMod = abilityModifier(card.sheet.abilities?.[abilityKey]);
        const proficiency = card.proficiencyBonus ?? 2 + Math.floor((asInt(card.sheet.level, 1) - 1) / 4);
        const saveDC = hasCasting ? card.spellSaveDC ?? 8 + proficiency + abilityMod : card.spellSaveDC;
        const attackBonus = hasCasting ? card.spellAttackBonus ?? proficiency + abilityMod : card.spellAttackBonus;
        return `
            <section class="character-detail-band">
                <h4>施法</h4>
                <div class="character-detail-info-grid">
                    ${statCard("施法属性", casting.ability ? String(casting.ability).toUpperCase() : "")}
                    ${statCard("法术豁免 DC", saveDC)}
                    ${statCard("法术攻击加值", attackBonus === null || attackBonus === undefined ? "" : signed(asInt(attackBonus, 0)))}
                </div>
                <div class="mt-2"><strong>法术位：</strong>${slotRows || "尚未生成（施法者职业首次保存后自动补齐）"}</div>
                <div class="mt-2"><strong>已知：</strong>${known.length ? known.map((item) => `<span class="badge bg-secondary me-1">${escapeHtml(item)}</span>`).join("") : "—"}</div>
                <div class="mt-2"><strong>准备：</strong>${prepared.length ? prepared.map((item) => `<span class="badge bg-secondary me-1">${escapeHtml(item)}</span>`).join("") : "—"}</div>
            </section>
        `;
    }

    function renderInventory(card: Dnd5eCard): string {
        const sheet = card.sheet;
        const equipment = Array.isArray(sheet.equipment) ? sheet.equipment : [];
        const rows = equipment.map((item) => `
            <tr>
                <td>${escapeHtml(item.name || "未命名")}</td>
                <td>${asInt(item.quantity, 1)}</td>
                <td>${Math.max(0, Number(item.weight) || 0)}</td>
                <td>${escapeHtml(item.notes || "")}</td>
            </tr>
        `).join("");
        const features = stringList(sheet.features);
        return `
            <section class="character-detail-band character-detail-band-wide">
                <h4>装备与特性</h4>
                <div class="character-detail-table-shell">
                    <table class="character-detail-table">
                        <thead><tr><th>装备</th><th>数量</th><th>重量</th><th>备注</th></tr></thead>
                        <tbody>${rows || '<tr><td colspan="4">暂无装备</td></tr>'}</tbody>
                    </table>
                </div>
                <div class="mt-2"><strong>特性：</strong>${features.length ? features.map((item) => `<span class="badge bg-secondary me-1">${escapeHtml(item)}</span>`).join("") : "—"}</div>
                <div class="mt-2"><strong>备注：</strong>${escapeHtml(sheet.notes || "—")}</div>
            </section>
        `;
    }

    function renderStatus(card: Dnd5eCard): string {
        const status = card.sheet.status;
        const values = Array.isArray(status) ? status : String(status || "").split(/[,，;；]+/).filter(Boolean);
        const hp = card.sheet.hp || {};
        return `
            <section class="character-detail-band">
                <h4>状态</h4>
                <div class="character-vital-grid">
                    ${statCard("当前生命", `${asInt(hp.current, 0)} / ${asInt(hp.max, 1)}`)}
                    ${statCard("临时生命", asInt(hp.temp, 0))}
                    ${statCard("状态", values.length ? values.join("、") : "正常")}
                </div>
            </section>
        `;
    }

    function renderSection(card: Dnd5eCard, section: Dnd5eSheetSectionDescriptor, roomDiff: unknown): string {
        switch (section.key) {
            case "identity": return renderIdentity(card);
            case "abilities": return renderAbilities(card);
            case "proficiencies": return renderProficiencies(card);
            case "combat": return renderCombat(card, roomDiff);
            case "spellcasting": return renderSpellcasting(card);
            case "inventory": return renderInventory(card);
            default: return "";
        }
    }

    function renderCharacterDetail(card: Dnd5eCard, roomDiff?: unknown): string {
        const body = sections.map((section) => renderSection(card, section, roomDiff)).join("");
        return `
            <header class="character-inspector-header">
                <div><h3>${escapeHtml(card.name)}</h3></div>
                <div class="character-inline-actions">
                    ${canEditCard(card) ? '<button type="button" data-dnd5e-action="edit">编辑角色卡</button>' : ""}
                </div>
            </header>
            <div class="character-detail-dashboard">${body}${renderStatus(card)}</div>
        `;
    }

    function inputRow(label: string, name: string, value: unknown, type: string = "text", extra: string = ""): string {
        return `
            <label class="dnd5e-field">
                <span>${escapeHtml(label)}</span>
                <input type="${type}" data-dnd5e-field="${escapeHtml(name)}" value="${escapeHtml(value ?? "")}" ${extra}>
            </label>
        `;
    }

    function textRow(label: string, name: string, value: unknown, rows: number = 3): string {
        return `
            <label class="dnd5e-field dnd5e-field-wide">
                <span>${escapeHtml(label)}</span>
                <textarea data-dnd5e-field="${escapeHtml(name)}" rows="${rows}">${escapeHtml(value ?? "")}</textarea>
            </label>
        `;
    }

    function renderEditor(card: Dnd5eCard): string {
        const sheet = card.sheet;
        const abilities = sheet.abilities || {};
        const proficiencies = sheet.proficiencies || {};
        const slots = sheet.spellcasting?.slots || {};
        const equipmentText = (sheet.equipment || [])
            .map((item) => `${item.name || ""} x${asInt(item.quantity, 1)} w${Math.max(0, Number(item.weight) || 0)}`)
            .join("\n");
        const abilityInputs = ABILITIES.map((item) => inputRow(`${item.label} ${item.short}`, `ability.${item.key}`, abilities[item.key], "number", 'min="1" max="30"')).join("");
        const slotInputs = Object.keys(slots).sort((a, b) => asInt(a) - asInt(b)).map((level) => `
            <div class="dnd5e-slot-row">
                <span>${escapeHtml(level)} 环</span>
                <label><span>上限</span><input type="number" min="0" data-dnd5e-slot-max="${escapeHtml(level)}" value="${asInt(slots[level]?.max, 0)}"></label>
                <label><span>已用</span><input type="number" min="0" data-dnd5e-slot-used="${escapeHtml(level)}" value="${asInt(slots[level]?.used, 0)}"></label>
            </div>
        `).join("");
        return `
            <form class="character-detail-dashboard dnd5e-editor" data-dnd5e-editor>
                <section class="character-detail-band">
                    <h4>基础信息</h4>
                    <div class="dnd5e-field-grid">
                        ${inputRow("姓名", "name", card.name)}
                        ${inputRow("等级", "level", sheet.level, "number", 'min="1" max="20"')}
                        ${inputRow("职业", "class", sheet.class)}
                        ${inputRow("子职", "subclass", sheet.subclass)}
                        ${inputRow("种族", "race", sheet.race)}
                        ${inputRow("背景", "background", sheet.background)}
                        ${inputRow("阵营", "alignment", sheet.alignment)}
                        ${inputRow("经验值", "xp", sheet.xp, "number", 'min="0"')}
                        ${textRow("角色简介", "bio", sheet.bio, 3)}
                    </div>
                </section>
                <section class="character-detail-band">
                    <h4>属性</h4>
                    <div class="dnd5e-field-grid">${abilityInputs}</div>
                </section>
                <section class="character-detail-band">
                    <h4>熟练</h4>
                    <div class="dnd5e-field-grid">
                        ${inputRow("豁免熟练（逗号分隔）", "prof.saves", stringList(proficiencies.saves).join(", "))}
                        ${inputRow("技能熟练（逗号分隔）", "prof.skills", stringList(proficiencies.skills).join(", "))}
                        ${inputRow("专精（逗号分隔）", "prof.expertise", stringList(proficiencies.expertise).join(", "))}
                        ${inputRow("护甲熟练", "prof.armor", stringList(proficiencies.armor).join(", "))}
                        ${inputRow("武器熟练", "prof.weapons", stringList(proficiencies.weapons).join(", "))}
                        ${inputRow("工具熟练", "prof.tools", stringList(proficiencies.tools).join(", "))}
                        ${inputRow("语言", "prof.languages", stringList(proficiencies.languages).join(", "))}
                    </div>
                </section>
                <section class="character-detail-band">
                    <h4>战斗</h4>
                    <div class="dnd5e-field-grid">
                        ${inputRow("当前 HP", "hp.current", sheet.hp?.current, "number", 'min="0"')}
                        ${inputRow("HP 上限", "hp.max", sheet.hp?.max, "number", 'min="1"')}
                        ${inputRow("临时 HP", "hp.temp", sheet.hp?.temp, "number", 'min="0"')}
                        ${inputRow("速度（尺）", "speed", sheet.speed, "number", 'min="0"')}
                        ${inputRow("生命骰", "hitDice", sheet.hitDice)}
                    </div>
                </section>
                <section class="character-detail-band">
                    <h4>施法</h4>
                    <div class="dnd5e-field-grid">
                        ${inputRow("施法属性（str/dex/con/int/wis/cha）", "spell.ability", sheet.spellcasting?.ability)}
                        ${inputRow("已知法术（逗号分隔）", "spell.known", stringList(sheet.spellcasting?.known).join(", "))}
                        ${inputRow("准备法术（逗号分隔）", "spell.prepared", stringList(sheet.spellcasting?.prepared).join(", "))}
                    </div>
                    <div class="dnd5e-slot-list">${slotInputs || "没有法术位；施法者职业保存后会自动生成。"}</div>
                </section>
                <section class="character-detail-band character-detail-band-wide">
                    <h4>装备与特性</h4>
                    <div class="dnd5e-field-grid">
                        ${textRow("装备（每行：名称 x数量 w重量）", "equipment", equipmentText, 5)}
                        ${textRow("特性（每行一条）", "features", stringList(sheet.features).join("\n"), 4)}
                        ${textRow("状态（逗号分隔）", "status", Array.isArray(sheet.status) ? sheet.status.join(", ") : String(sheet.status || ""), 2)}
                        ${textRow("备注", "notes", sheet.notes, 3)}
                    </div>
                </section>
            </form>
        `;
    }

    function readField(root: HTMLElement, name: string): string {
        const element = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[data-dnd5e-field="${name}"]`);
        return element ? element.value : "";
    }

    function buildSheetFromEditor(card: Dnd5eCard, root: HTMLElement): Dnd5eSheet {
        const sheet = normalizeSheet(card.sheet);
        sheet.level = clamp(readField(root, "level"), 1, 20, 1);
        sheet.class = readField(root, "class").trim();
        sheet.subclass = readField(root, "subclass").trim();
        sheet.race = readField(root, "race").trim();
        sheet.background = readField(root, "background").trim();
        sheet.alignment = readField(root, "alignment").trim();
        sheet.xp = Math.max(0, asInt(readField(root, "xp"), 0));
        ABILITIES.forEach((item) => {
            sheet.abilities![item.key] = clamp(readField(root, `ability.${item.key}`), 1, 30, 10);
        });
        sheet.proficiencies = {
            saves: splitList(readField(root, "prof.saves")),
            skills: splitList(readField(root, "prof.skills")),
            expertise: splitList(readField(root, "prof.expertise")),
            armor: splitList(readField(root, "prof.armor")),
            weapons: splitList(readField(root, "prof.weapons")),
            tools: splitList(readField(root, "prof.tools")),
            languages: splitList(readField(root, "prof.languages")),
        };
        const max = Math.max(1, asInt(readField(root, "hp.max"), 1));
        sheet.hp = {
            max,
            current: clamp(readField(root, "hp.current"), 0, max, max),
            temp: Math.max(0, asInt(readField(root, "hp.temp"), 0)),
        };
        sheet.speed = Math.max(0, asInt(readField(root, "speed"), 0));
        sheet.hitDice = readField(root, "hitDice").trim();
        const slots: Record<string, Dnd5eSlotRow> = {};
        root.querySelectorAll<HTMLInputElement>("[data-dnd5e-slot-max]").forEach((input) => {
            const level = input.dataset.dnd5eSlotMax || "";
            if (!level) return;
            const usedInput = root.querySelector<HTMLInputElement>(`[data-dnd5e-slot-used="${level}"]`);
            slots[level] = { max: Math.max(0, asInt(input.value, 0)), used: Math.max(0, asInt(usedInput?.value, 0)) };
        });
        sheet.spellcasting = {
            ability: readField(root, "spell.ability").trim().toLowerCase(),
            known: splitList(readField(root, "spell.known")),
            prepared: splitList(readField(root, "spell.prepared")),
            slots,
        };
        sheet.equipment = readField(root, "equipment")
            .split(/\n+/)
            .map((line) => line.trim())
            .filter(Boolean)
            .map((line) => {
                const matched = line.match(/^(.*?)\s*x(\d+)?\s*(?:w([\d.]+))?$/i);
                return {
                    name: String(matched?.[1] || line).trim(),
                    quantity: Math.max(1, asInt(matched?.[2], 1)),
                    weight: Math.max(0, Number(matched?.[3]) || 0),
                };
            });
        sheet.features = readField(root, "features").split(/\n+/).map((line) => line.trim()).filter(Boolean);
        sheet.status = splitList(readField(root, "status"));
        sheet.notes = readField(root, "notes").trim();
        sheet.bio = readField(root, "bio").trim();
        return sheet;
    }

    function showList(): void {
        const listView = document.getElementById("character-list-view");
        const detailPage = document.getElementById("character-detail-page");
        if (listView) listView.hidden = false;
        if (detailPage) detailPage.hidden = true;
        activeCardId = "";
        render();
    }

    function showDetail(cardId: string): void {
        activeCardId = cardId;
        const listView = document.getElementById("character-list-view");
        const detailPage = document.getElementById("character-detail-page");
        const empty = document.querySelector<HTMLElement>("#characterInspector .character-empty-state");
        const detail = document.getElementById("characterDetailView");
        if (listView) listView.hidden = true;
        if (detailPage) detailPage.hidden = false;
        if (empty) empty.hidden = true;
        if (detail) detail.hidden = false;
        render();
    }

    /** 统一角色卡列表由页面外壳渲染；本模块只在没有外壳时给出说明。 */
    function unifiedListBridge(): { render: () => void } | null {
        const bridge = (global as unknown as { TrpgCharacterListBridge?: { enabled?: boolean; render?: () => void } }).TrpgCharacterListBridge;
        if (!bridge?.enabled || typeof bridge.render !== "function") return null;
        return { render: () => bridge.render?.() };
    }

    function renderList(): void {
        const bridge = unifiedListBridge();
        if (bridge) {
            bridge.render();
            return;
        }
        const list = document.getElementById("characterList");
        if (!list) return;
        list.innerHTML = `<div class="character-empty-filter">角色卡列表需要页面外壳提供统一渲染。</div>`;
    }

    function renderDetail(): void {
        const host = document.getElementById("characterDetailView");
        if (!host) return;
        const card = cards.find((item) => item.id === activeCardId);
        if (!card) {
            host.innerHTML = "";
            return;
        }
        host.innerHTML = renderCharacterDetail(card);
    }

    function render(): void {
        // 进入房间前的预加载只更新内存，不抢占当前模块的页面。
        if (!active) return;
        renderList();
        renderDetail();
    }

    async function loadCards(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<Dnd5eCardInput[]>>("/api/characters");
            const rows = response?.success && Array.isArray(response.data) ? response.data : [];
            cards = rows
                .filter((row) => String(row?.rulesetId || "").toLowerCase() === RULESET_ID)
                .map((row) => createCharacterCard(row));
        } catch (error) {
            notify(`加载 5e 角色卡失败：${escapeHtml((error as Error)?.message || error)}`, "error");
            cards = [];
        }
        render();
    }

    async function reloadCharacterSheet(): Promise<void> {
        await loadRulesetSections();
        await loadCards();
    }

    function newCard(): Dnd5eCard {
        return createCharacterCard({
            id: `dnd5e-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
            name: "新角色",
            playerId: currentPlayerId(),
            sheet: emptySheet(),
        });
    }

    /** 与 CoC 一致的弹窗新建 / 编辑：卡面字段不同，交互流程相同。 */
    function ensureEditorModal(): BootstrapModalInstance | null {
        if (editorModal) return editorModal;
        if (typeof bootstrap === "undefined") return null;
        const element = document.createElement("div");
        element.id = "dnd5eCharacterModal";
        element.className = "modal fade";
        element.tabIndex = -1;
        element.innerHTML = `
            <div class="modal-dialog modal-xl modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">编辑角色卡</h5>
                        <button type="button" class="btn-close" data-dnd5e-action="cancel" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body" data-dnd5e-modal-body></div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-dnd5e-action="cancel">取消</button>
                        <button type="button" class="btn btn-primary" data-dnd5e-action="save">保存</button>
                    </div>
                </div>
            </div>`;
        element.addEventListener("click", (event) => {
            const button = (event.target as HTMLElement | null)?.closest<HTMLElement>("[data-dnd5e-action]");
            const action = button?.dataset.dnd5eAction || "";
            if (action === "cancel") {
                closeEditorModal();
                return;
            }
            if (action === "save") {
                const form = element.querySelector<HTMLElement>("[data-dnd5e-editor]");
                if (form) void saveActiveCard(form);
            }
        });
        element.addEventListener("hidden.bs.modal", () => {
            editorDraft = null;
            const body = element.querySelector<HTMLElement>("[data-dnd5e-modal-body]");
            if (body) body.innerHTML = "";
        });
        document.body.appendChild(element);
        editorModal = new bootstrap.Modal(element);
        return editorModal;
    }

    function closeEditorModal(): void {
        editorModal?.hide();
    }

    function openEditorModal(card: Dnd5eCard, isNew: boolean): void {
        const instance = ensureEditorModal();
        const element = document.getElementById("dnd5eCharacterModal");
        if (!instance || !element) {
            notify("角色卡编辑窗口不可用", "error");
            return;
        }
        editorDraft = isNew ? card : null;
        activeCardId = card.id;
        const title = element.querySelector<HTMLElement>(".modal-title");
        if (title) title.textContent = isNew ? "创建角色卡" : `编辑 ${card.name}`;
        const body = element.querySelector<HTMLElement>("[data-dnd5e-modal-body]");
        if (body) body.innerHTML = renderEditor(card);
        instance.show();
    }

    function openEditorForCard(cardId: string): void {
        const card = cards.find((item) => item.id === cardId);
        if (!card || !canEditCard(card)) {
            notify("没有权限编辑这张角色卡", "error");
            return;
        }
        openEditorModal(card, false);
    }

    async function saveActiveCard(root: HTMLElement): Promise<void> {
        const existing = cards.find((item) => item.id === activeCardId) || editorDraft;
        if (!existing) return;
        const name = readField(root, "name").trim() || "未命名冒险者";
        const sheet = buildSheetFromEditor(existing, root);
        const payload: Dnd5eCard = { ...existing, name, sheet, rulesetId: RULESET_ID };
        try {
            const response = await TrpgApi.put<ApiResponse<Dnd5eCardInput>>(`/api/characters/${encodeURIComponent(payload.id)}`, payload);
            if (!response?.success) {
                notify(response?.message || response?.error || "保存角色卡失败", "error");
                return;
            }
            const saved = createCharacterCard(response.data || payload);
            cards = cards.some((item) => item.id === saved.id)
                ? cards.map((item) => item.id === saved.id ? saved : item)
                : [saved, ...cards];
            activeCardId = saved.id;
            editorDraft = null;
            closeEditorModal();
            showDetail(saved.id);
            notify("角色卡已保存", "success");
        } catch (error) {
            notify(`保存角色卡失败：${escapeHtml((error as Error)?.message || error)}`, "error");
        }
    }

    function handleAction(event: Event): void {
        const target = event.target as HTMLElement | null;
        const button = target?.closest<HTMLElement>("[data-dnd5e-action]");
        const action = button?.dataset.dnd5eAction || "";
        if (action === "edit") openEditorForCard(activeCardId);
    }

    function describeCharacterCard(raw: Record<string, unknown>): CharacterSheetCardSummary {
        const card = createCharacterCard(raw as Dnd5eCardInput);
        const sheet = card.sheet;
        const bio = String(sheet.bio || "").trim();
        return {
            tag: "DND",
            name: card.name,
            avatar: String(card.avatar || ""),
            subtitle: `${sheet.level || 1} 级 ${sheet.class || "未知职业"}${sheet.race ? ` · ${sheet.race}` : ""}`,
            bio: bio || "一位初出茅庐的冒险者。",
        };
    }

    function createCharacterFromList(): void {
        if (!global.currentUser) {
            notify("请先登录后再创建角色卡", "warning");
            return;
        }
        openEditorModal(newCard(), true);
    }

    async function openCharacter(raw: Record<string, unknown>, mode?: "detail" | "edit"): Promise<void> {
        if (initialLoad) await initialLoad.catch(() => undefined);
        const card = createCharacterCard(raw as Dnd5eCardInput);
        cards = cards.some((item) => item.id === card.id)
            ? cards.map((item) => item.id === card.id ? card : item)
            : [card, ...cards];
        if (mode === "edit") {
            openEditorModal(card, false);
            return;
        }
        showDetail(card.id);
    }

    function bind(): void {
        const workspace = document.getElementById("characterWorkspace");
        if (!workspace || workspace.dataset.dnd5eBound === "true") return;
        workspace.dataset.dnd5eBound = "true";
        // 创建入口由页面外壳统一分派；这里只处理详情页内的编辑与返回列表。
        workspace.addEventListener("click", (event) => {
            if (!active) return;
            const target = event.target as HTMLElement | null;
            if (target?.closest("#backToCharacterList")) {
                showList();
                return;
            }
            handleAction(event);
        });
    }

    function clearCharacterManagement(): void {
        active = false;
        editorDraft = null;
        closeEditorModal();
        showList();
    }

    function initCharacterSheet(): void {
        active = true;
        bind();
        showList();
        const previous = loading;
        const request = (async () => {
            if (previous) await previous.catch(() => undefined);
            await reloadCharacterSheet();
        })();
        loading = request;
        initialLoad = request;
        void request.finally(() => {
            if (loading === request) loading = null;
        });
    }

    const api: Dnd5eModuleApi = {
        createCharacterCard,
        listCharacterCards,
        getCharacterCardSnapshot,
        renderCharacterDetail,
        initCharacterSheet,
    };

    (global as unknown as { DND5ECharacterSheet?: Dnd5eModuleApi }).DND5ECharacterSheet = api;
    global.TrpgCharacterSheetRegistry?.register?.(RULESET_ID, {
        ...api,
        id: RULESET_ID,
        initCharacterSheet,
        clearCharacterManagement,
        reloadCharacterManagement: reloadCharacterSheet,
        supportsRuleset,
        describeCharacterCard,
        openCharacter,
        createCharacter: createCharacterFromList,
    });
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
