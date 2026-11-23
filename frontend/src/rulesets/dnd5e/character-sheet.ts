// D&D 5e 角色卡面（FVTT 式页签卡）。
//
// 展示 + 编辑一刀：卡面从「平铺表单」重构成 FVTT dnd5e 系统的页签结构
// （details / inventory / features / spells / effects / biography），
// 左侧栏复刻 character-sidebar（portrait↔token 切换、AC 徽章、HP 计、生命骰、
// 死亡豁免卡、疲惫 pips），头区复刻 character-header（等级徽章、灵感、
// 短休/长休、XP 条）。
//
// 对外仍是同一个模块契约：createCharacterCard / listCharacterCards /
// getCharacterCardSnapshot / renderCharacterDetail / initCharacterSheet，
// 并通过 TrpgCharacterSheetRegistry 注册（registry 与页面外壳不需要改）。
//
// 保存语义沿用原来的 pack / apply_save 往返：页签里编辑的是本卡草稿，
// 「保存」统一 PUT /api/characters/<id>，由插件 schema 做 normalize + 派生。
// 建卡向导与升级入口不在本刀范围内，新建仍走原来的弹窗表单。

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
    /** 是否装备；后端 pack 不理解这个键，会原样保留（见页签说明）。 */
    equipped?: boolean;
    /** 该装备条目上的 FVTT Active Effect（引擎按路径结算）。 */
    effects?: Dnd5eEffect[];
}

/**
 * 引擎对单条 change 的分级结论（``sheet.effect_changes[].changes[].applied``）：
 * ``full`` = 真的进了派生值；``stored`` = 已记录、当前没有数值消费方；
 * ``unsupported`` = 连镜像模型都没有的路径（原文保留）。
 */
type Dnd5eEffectTier = "full" | "stored" | "unsupported";

interface Dnd5eEffectChange {
    key?: string;
    mode?: number;
    value?: unknown;
    applied?: Dnd5eEffectTier | string;
    active?: boolean;
    reason?: string;
    note?: string;
}

interface Dnd5eEffect {
    id?: string;
    name?: string;
    transfer?: boolean;
    disabled?: boolean;
    origin?: { item_id?: string; source?: string; equipped?: boolean };
    changes?: Dnd5eEffectChange[];
}

interface Dnd5eDeathSaves {
    success?: number;
    failure?: number;
}

/** 生命骰池；与插件 rest 模块写的 sheet.hit_dice 同形。 */
interface Dnd5eHitDice {
    die?: number;
    max?: number;
    used?: number;
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
    hit_dice?: Dnd5eHitDice;
    spellcasting?: Dnd5eSpellcasting;
    equipment?: Dnd5eEquipmentItem[];
    features?: string[];
    notes?: string;
    bio?: string;
    appearance?: string;
    token?: string;
    status?: string[] | string;
    /** FVTT 效果（数值改写引擎的载体）；``changes[].applied`` 是分级结论。 */
    effect_changes?: Dnd5eEffect[];
    deathSaves?: Dnd5eDeathSaves;
    exhaustion?: number;
    inspiration?: boolean;
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

interface Dnd5eModuleApi {
    createCharacterCard: (input?: Dnd5eCardInput) => Dnd5eCard;
    listCharacterCards: () => Dnd5eCard[];
    getCharacterCardSnapshot: (cardId: string) => Record<string, unknown> | null;
    renderCharacterDetail: (card: Dnd5eCard, roomDiff?: unknown) => string;
    initCharacterSheet: () => void;
}

type Dnd5eTabKey = "details" | "inventory" | "features" | "spells" | "effects" | "biography";

/** 卡面草稿：页签里编辑的本地态，保存时一次性提交。 */
interface Dnd5eDraft {
    cardId: string;
    name: string;
    avatar: string;
    sheet: Dnd5eSheet;
}

interface Dnd5eRestRow {
    name?: string;
    kind?: string;
    hp_before?: number;
    hp_after?: number;
    healed?: number;
    hit_dice_spent?: number;
    hit_dice_regained?: number;
    slots_restored?: boolean;
    exhaustion_level?: number;
    notes?: string[];
    error?: string;
}

interface Dnd5eRestPayload {
    kind?: string;
    members?: Dnd5eRestRow[];
}

interface Dnd5eRoomMember {
    username?: string;
    character_card?: { id?: string };
}

interface Dnd5eRoomInfo {
    id?: string;
    members?: Dnd5eRoomMember[];
}

(function installDnd5eCharacterSheet(global: Window & typeof globalThis): void {
    "use strict";

    const RULESET_ID = "dnd5e";
    const TAB_STORAGE_PREFIX = "trpg:dnd5e:tab:";

    const ABILITIES: Array<{ key: Dnd5eAbility; short: string; label: string }> = [
        { key: "str", short: "STR", label: "力量" },
        { key: "dex", short: "DEX", label: "敏捷" },
        { key: "con", short: "CON", label: "体质" },
        { key: "int", short: "INT", label: "智力" },
        { key: "wis", short: "WIS", label: "感知" },
        { key: "cha", short: "CHA", label: "魅力" },
    ];

    /** 与插件 rules.SKILLS 完全同源：存储值必须是英文 id，后端才认熟练。 */
    const SKILLS: Array<{ key: string; label: string; ability: Dnd5eAbility }> = [
        { key: "acrobatics", label: "体操", ability: "dex" },
        { key: "animal-handling", label: "驯兽", ability: "wis" },
        { key: "arcana", label: "奥秘", ability: "int" },
        { key: "athletics", label: "运动", ability: "str" },
        { key: "deception", label: "欺瞒", ability: "cha" },
        { key: "history", label: "历史", ability: "int" },
        { key: "insight", label: "洞悉", ability: "wis" },
        { key: "intimidation", label: "威吓", ability: "cha" },
        { key: "investigation", label: "调查", ability: "int" },
        { key: "medicine", label: "医药", ability: "wis" },
        { key: "nature", label: "自然", ability: "int" },
        { key: "perception", label: "察觉", ability: "wis" },
        { key: "performance", label: "表演", ability: "cha" },
        { key: "persuasion", label: "游说", ability: "cha" },
        { key: "religion", label: "宗教", ability: "int" },
        { key: "sleight-of-hand", label: "巧手", ability: "dex" },
        { key: "stealth", label: "隐匿", ability: "dex" },
        { key: "survival", label: "求生", ability: "wis" },
    ];

    /** 常见状态；存储英文 id（与 FVTT / 插件的条件 id 一致），显示中文。 */
    const CONDITIONS: Array<{ id: string; label: string }> = [
        { id: "poisoned", label: "中毒" },
        { id: "charmed", label: "魅惑" },
        { id: "frightened", label: "恐惧" },
        { id: "prone", label: "倒地" },
        { id: "stunned", label: "震慑" },
        { id: "blinded", label: "目盲" },
        { id: "deafened", label: "耳聋" },
        { id: "grappled", label: "擒抱" },
        { id: "incapacitated", label: "无力" },
        { id: "invisible", label: "隐形" },
        { id: "paralyzed", label: "麻痹" },
        { id: "petrified", label: "石化" },
        { id: "restrained", label: "束缚" },
        { id: "unconscious", label: "昏迷" },
        { id: "exhaustion", label: "力竭" },
    ];

    const TABS: Array<{ key: Dnd5eTabKey; label: string; icon: string }> = [
        { key: "details", label: "详情", icon: "fa-list-ul" },
        { key: "inventory", label: "物品", icon: "fa-suitcase" },
        { key: "features", label: "特性", icon: "fa-star" },
        { key: "spells", label: "法术", icon: "fa-magic" },
        { key: "effects", label: "效果", icon: "fa-flask" },
        { key: "biography", label: "传记", icon: "fa-book" },
    ];

    const TAB_KEYS = TABS.map((item) => item.key);

    /** 5e 升级经验门槛（PHB 表）：下标 = 等级。 */
    const XP_THRESHOLDS = [
        0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
        85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
    ];

    const EXHAUSTION_NOTES: Record<number, string> = {
        0: "无疲惫",
        1: "属性检定劣势",
        2: "速度减半",
        3: "攻击与豁免劣势",
        4: "生命上限减半",
        5: "速度归零",
        6: "死亡",
    };

    const ABILITY_ALIASES: Record<string, Dnd5eAbility> = {
        str: "str", strength: "str", 力量: "str",
        dex: "dex", dexterity: "dex", 敏捷: "dex",
        con: "con", constitution: "con", 体质: "con",
        int: "int", intelligence: "int", 智力: "int",
        wis: "wis", wisdom: "wis", 感知: "wis",
        cha: "cha", charisma: "cha", 魅力: "cha",
    };

    const SKILL_ALIASES: Record<string, string> = (() => {
        const map: Record<string, string> = {};
        SKILLS.forEach((item) => {
            map[item.key] = item.key;
            map[item.key.replace(/-/g, " ")] = item.key;
            map[item.label] = item.key;
        });
        return map;
    })();

    let cards: Dnd5eCard[] = [];
    let activeCardId = "";
    let active = false;
    let bound = false;
    let loading: Promise<void> | null = null;
    let initialLoad: Promise<void> | null = null;
    let editorModal: BootstrapModalInstance | null = null;
    let editorDraft: Dnd5eCard | null = null;

    // 页签卡运行时状态：草稿、脏标记、页签、头像/Token 显示模式。
    let draft: Dnd5eDraft | null = null;
    let draftDirty = false;
    let saving = false;
    let activeTab: Dnd5eTabKey = "details";
    let portraitMode: "avatar" | "token" = "avatar";
    let pendingUpload: "avatar" | "token" = "avatar";

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

    /** 合并两个字符串列表并去重（保持先后顺序，比较忽略大小写）。 */
    function unionList(first: unknown, second: unknown): string[] {
        const result: string[] = [];
        [...stringList(first), ...stringList(second)].forEach((item) => {
            if (!result.some((existing) => existing.toLowerCase() === item.toLowerCase())) result.push(item);
        });
        return result;
    }

    function statusList(value: unknown): string[] {
        if (Array.isArray(value)) return stringList(value);
        return splitList(value);
    }

    function canonicalAbility(value: unknown): Dnd5eAbility | "" {
        const raw = String(value ?? "").trim();
        if (!raw) return "";
        return ABILITY_ALIASES[raw.toLowerCase()] ?? ABILITY_ALIASES[raw] ?? "";
    }

    function canonicalSkill(value: unknown): string {
        const raw = String(value ?? "").trim();
        if (!raw) return "";
        return SKILL_ALIASES[raw.toLowerCase()] ?? SKILL_ALIASES[raw] ?? "";
    }

    /**
     * 熟练列表归一：旧卡里存的是中文技能名 / 属性名，后端只认英文 id，
     * 这里统一换成 id；无法识别的条目原样保留（例如自定义语言 / 工具）。
     */
    function normalizeSkillList(value: unknown): string[] {
        const seen = new Set<string>();
        const result: string[] = [];
        stringList(value).forEach((item) => {
            const canonical = canonicalSkill(item);
            const next = canonical || item;
            const key = next.toLowerCase();
            if (seen.has(key)) return;
            seen.add(key);
            result.push(next);
        });
        return result;
    }

    function normalizeSaveList(value: unknown): string[] {
        const seen = new Set<string>();
        const result: string[] = [];
        stringList(value).forEach((item) => {
            const canonical = canonicalAbility(item);
            const next = canonical || item;
            const key = next.toLowerCase();
            if (seen.has(key)) return;
            seen.add(key);
            result.push(next);
        });
        return result;
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
            appearance: "",
            token: "",
            status: [],
            deathSaves: { success: 0, failure: 0 },
            exhaustion: 0,
            inspiration: false,
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
            saves: normalizeSaveList(proficiencies.saves),
            skills: normalizeSkillList(proficiencies.skills),
            expertise: normalizeSkillList(proficiencies.expertise),
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
        if (source.hit_dice && typeof source.hit_dice === "object") {
            base.hit_dice = {
                die: Math.max(4, asInt(source.hit_dice.die, 8)),
                max: Math.max(0, asInt(source.hit_dice.max, base.level)),
                used: Math.max(0, asInt(source.hit_dice.used, 0)),
            };
        }
        const casting = source.spellcasting || {};
        const slots: Record<string, Dnd5eSlotRow> = {};
        Object.keys(casting.slots || {}).forEach((level) => {
            const row = casting.slots?.[level] || {};
            slots[level] = { max: Math.max(0, asInt(row.max, 0)), used: Math.max(0, asInt(row.used, 0)) };
        });
        base.spellcasting = {
            ability: canonicalAbility(casting.ability) || "",
            // 旧卡可能只写了 prepared：把准备表并进已知表，卡面才有唯一的法术行索引。
            known: unionList(casting.known, casting.prepared),
            prepared: stringList(casting.prepared),
            slots,
        };
        base.equipment = Array.isArray(source.equipment)
            ? source.equipment.map((item) => ({
                name: String(item?.name ?? ""),
                quantity: Math.max(1, asInt(item?.quantity, 1)),
                weight: Math.max(0, Number(item?.weight) || 0),
                notes: String(item?.notes ?? ""),
                equipped: item?.equipped === true,
            }))
            : [];
        base.features = stringList(source.features);
        base.notes = String(source.notes ?? "");
        base.bio = String(source.bio ?? base.bio ?? "");
        base.appearance = String(source.appearance ?? "");
        base.token = String(source.token ?? "");
        base.status = statusList(source.status);
        base.deathSaves = {
            success: clamp(source.deathSaves?.success, 0, 3, 0),
            failure: clamp(source.deathSaves?.failure, 0, 3, 0),
        };
        base.exhaustion = clamp(source.exhaustion, 0, 6, 0);
        base.inspiration = source.inspiration === true;
        return base;
    }

    function cloneSheet(sheet: Dnd5eSheet): Dnd5eSheet {
        // normalizeSheet 已经把所有嵌套结构复制成新对象，可安全当作深拷贝。
        return normalizeSheet(sheet);
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

    // ---------------------------------------------------------------- 派生

    function abilityScore(sheet: Dnd5eSheet, key: Dnd5eAbility): number {
        return asInt(sheet.abilities?.[key], 10);
    }

    function abilityModifier(score: unknown): number {
        return Math.floor((asInt(score, 10) - 10) / 2);
    }

    function signed(value: number): string {
        return value >= 0 ? `+${value}` : `${value}`;
    }

    function levelOf(sheet: Dnd5eSheet): number {
        return clamp(sheet.level, 1, 20, 1);
    }

    function proficiencyBonusOf(sheet: Dnd5eSheet): number {
        return 2 + Math.floor((levelOf(sheet) - 1) / 4);
    }

    function hasSave(sheet: Dnd5eSheet, key: Dnd5eAbility): boolean {
        return normalizeSaveList(sheet.proficiencies?.saves).includes(key);
    }

    function saveBonusOf(sheet: Dnd5eSheet, key: Dnd5eAbility): number {
        const mod = abilityModifier(abilityScore(sheet, key));
        return mod + (hasSave(sheet, key) ? proficiencyBonusOf(sheet) : 0);
    }

    function skillProficiency(sheet: Dnd5eSheet, key: string): { proficient: boolean; expertise: boolean } {
        const expertise = normalizeSkillList(sheet.proficiencies?.expertise).includes(key);
        const proficient = normalizeSkillList(sheet.proficiencies?.skills).includes(key) || expertise;
        return { proficient, expertise };
    }

    function skillBonusOf(sheet: Dnd5eSheet, key: string): number {
        const skill = SKILLS.find((item) => item.key === key);
        const mod = abilityModifier(abilityScore(sheet, skill?.ability ?? "dex"));
        const flags = skillProficiency(sheet, key);
        const bonus = proficiencyBonusOf(sheet) * (flags.expertise ? 2 : flags.proficient ? 1 : 0);
        return mod + bonus;
    }

    function passivePerceptionOf(sheet: Dnd5eSheet): number {
        return 10 + skillBonusOf(sheet, "perception");
    }

    function initiativeOf(sheet: Dnd5eSheet): number {
        return abilityModifier(abilityScore(sheet, "dex"));
    }

    function spellcastingOf(sheet: Dnd5eSheet): { ability: Dnd5eAbility | ""; dc: number; attack: number } | null {
        const key = canonicalAbility(sheet.spellcasting?.ability);
        if (!key) return null;
        const bonus = proficiencyBonusOf(sheet) + abilityModifier(abilityScore(sheet, key));
        return { ability: key, dc: 8 + bonus, attack: bonus };
    }

    function xpProgressOf(sheet: Dnd5eSheet): { floor: number; next: number; pct: number; maxed: boolean } {
        const level = levelOf(sheet);
        const xp = Math.max(0, asInt(sheet.xp, 0));
        const floor = XP_THRESHOLDS[level - 1] ?? 0;
        const nextRaw = XP_THRESHOLDS[level];
        const maxed = nextRaw === undefined;
        const next = maxed ? (XP_THRESHOLDS[XP_THRESHOLDS.length - 1] ?? floor) : nextRaw;
        const span = Math.max(1, next - floor);
        const pct = maxed ? 100 : Math.max(0, Math.min(100, Math.round(((xp - floor) / span) * 100)));
        return { floor, next, pct, maxed };
    }

    function hitDiceLabel(sheet: Dnd5eSheet): string {
        const pool = sheet.hit_dice;
        const declared = String(sheet.hitDice || "").trim();
        if (pool && asInt(pool.max, 0) > 0) {
            const die = Math.max(4, asInt(pool.die, 8));
            const max = asInt(pool.max, levelOf(sheet));
            const used = Math.max(0, Math.min(max, asInt(pool.used, 0)));
            const total = declared || `${max}d${die}`;
            return `${total}（已用 ${used}/${max}）`;
        }
        if (declared) return declared;
        return `${levelOf(sheet)}d8（未记录已用）`;
    }

    function conditionLabel(id: string): string {
        const found = CONDITIONS.find((item) => item.id === id.toLowerCase());
        if (found) return found.label;
        const foundByLabel = CONDITIONS.find((item) => item.label === id);
        return foundByLabel ? foundByLabel.label : id;
    }

    function sameCondition(left: string, right: string): boolean {
        return left.trim().toLowerCase() === right.trim().toLowerCase()
            || conditionLabel(left) === conditionLabel(right);
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

    function listCharacterCards(): Dnd5eCard[] {
        return cards;
    }

    function getCharacterCardSnapshot(cardId: string): Record<string, unknown> | null {
        const card = cards.find((item) => item.id === cardId);
        if (!card) return null;
        // 房间绑定读快照：草稿有未保存修改时按草稿返回，避免绑定到旧卡面。
        const name = draft && draft.cardId === cardId && draftDirty ? draft.name : card.name;
        const sheet = draft && draft.cardId === cardId && draftDirty ? draft.sheet : card.sheet;
        return {
            ...card,
            name,
            rulesetId: RULESET_ID,
            sheet,
            level: sheet.level,
            class: sheet.class,
            hp: sheet.hp,
            currentHp: sheet.hp?.current,
            maxHp: sheet.hp?.max,
            tempHp: sheet.hp?.temp,
            armorClass: card.armorClass,
            spellcasting: sheet.spellcasting,
        };
    }

    // ------------------------------------------------------------ 页签状态

    function normalizeTab(key: unknown): Dnd5eTabKey {
        const raw = String(key ?? "").trim().toLowerCase() as Dnd5eTabKey;
        return TAB_KEYS.includes(raw) ? raw : "details";
    }

    function storedTab(cardId: string): Dnd5eTabKey {
        try {
            return normalizeTab(global.localStorage?.getItem(`${TAB_STORAGE_PREFIX}${cardId}`));
        } catch {
            return "details";
        }
    }

    function rememberTab(cardId: string, key: Dnd5eTabKey): void {
        try {
            global.localStorage?.setItem(`${TAB_STORAGE_PREFIX}${cardId}`, key);
        } catch {
            // 隐私模式不可写；页签仍在本会话内记忆。
        }
    }

    function startDraft(card: Dnd5eCard): void {
        draft = {
            cardId: card.id,
            name: card.name,
            avatar: String(card.avatar || ""),
            sheet: cloneSheet(card.sheet),
        };
        draftDirty = false;
    }

    function dropDraft(): void {
        draft = null;
        draftDirty = false;
    }

    function activeCard(): Dnd5eCard | undefined {
        return cards.find((item) => item.id === activeCardId);
    }

    // ---------------------------------------------------------------- 渲染

    let uid = 0;
    function nextId(): string {
        uid += 1;
        return `dnd5e-ctl-${uid}`;
    }

    function readonlyAttr(editable: boolean): string {
        return editable ? "" : ' readonly aria-readonly="true"';
    }

    function bindAttrs(path: string, kind: "text" | "number" | "list" | "bool"): string {
        return `data-dnd5e-bind="${escapeHtml(path)}" data-dnd5e-bind-kind="${kind}"`;
    }

    function textInput(path: string, value: unknown, editable: boolean, options: { label?: string; cls?: string; placeholder?: string } = {}): string {
        const cls = options.cls ? ` ${options.cls}` : "";
        return `<input type="text" class="dnd5e-input${cls}" ${bindAttrs(path, "list")}
            value="${escapeHtml(String(value ?? ""))}"
            aria-label="${escapeHtml(options.label || path)}"
            placeholder="${escapeHtml(options.placeholder || "")}"${readonlyAttr(editable)}>`;
    }

    function numberInput(path: string, value: unknown, editable: boolean, options: { label?: string; min?: number; max?: number; cls?: string } = {}): string {
        const cls = options.cls ? ` ${options.cls}` : "";
        const min = options.min === undefined ? "" : ` min="${options.min}"`;
        const max = options.max === undefined ? "" : ` max="${options.max}"`;
        return `<input type="number" inputmode="numeric" class="dnd5e-input dnd5e-input-number${cls}" ${bindAttrs(path, "number")}
            value="${escapeHtml(String(value ?? ""))}"${min}${max}
            aria-label="${escapeHtml(options.label || path)}"${readonlyAttr(editable)}>`;
    }

    function renderHeader(card: Dnd5eCard, sheet: Dnd5eSheet, name: string, editable: boolean): string {
        const level = levelOf(sheet);
        const xp = xpProgressOf(sheet);
        const line = [sheet.class, sheet.subclass, sheet.race, sheet.background]
            .map((item) => String(item ?? "").trim())
            .filter(Boolean)
            .join(" · ");
        return `
            <header class="dnd5e-sheet-header">
                <div class="dnd5e-sheet-identity">
                    ${editable
                        ? `<input type="text" class="dnd5e-sheet-name" ${bindAttrs("name", "text")} value="${escapeHtml(name)}" aria-label="角色名">`
                        : `<h3 class="dnd5e-sheet-name-static">${escapeHtml(name)}</h3>`}
                    <div class="dnd5e-sheet-meta">
                        <span class="dnd5e-level-badge" title="等级">Lv ${level}</span>
                        ${editable
                            ? `<button type="button" class="dnd5e-chip dnd5e-levelup"
                                data-dnd5e-act="level-up" title="升级向导：HP / 特性 / 子职 / 属性提升"
                                ${level >= 20 ? "disabled" : ""}>
                                <i class="fa fa-level-up" aria-hidden="true"></i> 升级
                               </button>`
                            : ""}
                        <span class="dnd5e-sheet-line">${escapeHtml(line || "尚未填写职业 / 种族")}</span>
                    </div>
                </div>
                <div class="dnd5e-sheet-xp">
                    <div class="dnd5e-xp-head">
                        <span>经验值</span>
                        ${editable
                            ? numberInput("xp", sheet.xp, true, { min: 0, label: "经验值", cls: "dnd5e-xp-input" })
                            : `<strong data-dnd5e-live="xp">${asInt(sheet.xp, 0)}</strong>`}
                        <small data-dnd5e-live="xp-next">${xp.maxed ? "已满级" : `下一级 ${xp.next}`}</small>
                    </div>
                    <div class="dnd5e-xp-bar" role="progressbar" aria-label="经验值进度" aria-valuemin="0" aria-valuemax="100"
                        aria-valuenow="${xp.pct}"><i data-dnd5e-live="xp-bar" style="width:${xp.pct}%"></i></div>
                </div>
                <div class="dnd5e-sheet-actions">
                    <button type="button" class="dnd5e-chip ${sheet.inspiration ? "is-on" : ""}"
                        data-dnd5e-act="inspiration" aria-pressed="${sheet.inspiration ? "true" : "false"}"
                        ${editable ? "" : " disabled"} title="灵感（优势一次）">
                        <i class="fa fa-lightbulb-o" aria-hidden="true"></i> 灵感
                    </button>
                    <button type="button" class="dnd5e-chip dnd5e-rest" data-dnd5e-act="rest-short" title="短休：消耗生命骰恢复 HP">
                        <i class="fa fa-coffee" aria-hidden="true"></i> 短休
                    </button>
                    <button type="button" class="dnd5e-chip dnd5e-rest" data-dnd5e-act="rest-long" title="长休：回满 HP、法术位，力竭 -1">
                        <i class="fa fa-bed" aria-hidden="true"></i> 长休
                    </button>
                    ${editable ? `
                    <button type="button" class="dnd5e-btn dnd5e-btn-primary" data-dnd5e-act="save" ${saving ? "disabled" : ""}>
                        <i class="fa fa-floppy-o" aria-hidden="true"></i> ${saving ? "保存中…" : "保存"}
                    </button>
                    <button type="button" class="dnd5e-btn" data-dnd5e-act="reset" ${draftDirty ? "" : "disabled"} title="丢弃未保存修改">
                        <i class="fa fa-undo" aria-hidden="true"></i> 重置
                    </button>` : ""}
                </div>
            </header>
            <div class="dnd5e-sheet-status" data-dnd5e-dirty-banner ${draftDirty ? "" : "hidden"}>
                <i class="fa fa-pencil" aria-hidden="true"></i> 有未保存的修改
            </div>
        `;
    }

    function renderPortrait(card: Dnd5eCard, sheet: Dnd5eSheet, avatar: string, editable: boolean): string {
        const tokenSrc = String(sheet.token || "").trim();
        const usingToken = portraitMode === "token" && Boolean(tokenSrc);
        const src = usingToken ? tokenSrc : avatar;
        const resolved = TrpgAvatar.resolveAvatarSrc(card.name, src);
        return `
            <div class="dnd5e-portrait-shell">
                <button type="button" class="dnd5e-portrait" data-dnd5e-act="avatar-upload"
                    aria-label="更换${usingToken ? " Token" : "头像"}图片" ${editable ? "" : " disabled"}>
                    <img data-dnd5e-portrait-img src="${escapeHtml(resolved)}" alt="${escapeHtml(card.name)} 的${usingToken ? " Token" : "头像"}">
                    ${editable ? '<span class="dnd5e-portrait-hint"><i class="fa fa-camera" aria-hidden="true"></i></span>' : ""}
                </button>
                <div class="dnd5e-portrait-modes" role="group" aria-label="图像来源">
                    <button type="button" class="dnd5e-mini ${portraitMode === "avatar" ? "is-on" : ""}"
                        data-dnd5e-act="portrait-mode" data-mode="avatar" aria-pressed="${portraitMode === "avatar" ? "true" : "false"}">头像</button>
                    <button type="button" class="dnd5e-mini ${portraitMode === "token" ? "is-on" : ""}"
                        data-dnd5e-act="portrait-mode" data-mode="token" aria-pressed="${portraitMode === "token" ? "true" : "false"}"
                        ${tokenSrc ? "" : 'title="还没有 Token 图，点击图片上传即可"'}>Token</button>
                </div>
                <input type="file" accept="image/*" data-dnd5e-avatar-upload hidden aria-hidden="true" tabindex="-1">
            </div>
        `;
    }

    function renderRail(card: Dnd5eCard, sheet: Dnd5eSheet, avatar: string, editable: boolean): string {
        const hp = sheet.hp || {};
        const current = Math.max(0, asInt(hp.current, 0));
        const max = Math.max(1, asInt(hp.max, 1));
        const temp = Math.max(0, asInt(hp.temp, 0));
        const pct = Math.max(0, Math.min(100, Math.round((current / max) * 100)));
        const deathSaves = sheet.deathSaves || {};
        const success = clamp(deathSaves.success, 0, 3, 0);
        const failure = clamp(deathSaves.failure, 0, 3, 0);
        const deathActive = current === 0 || success > 0 || failure > 0;
        const exhaustion = clamp(sheet.exhaustion, 0, 6, 0);
        const ac = asInt(sheet.armorClass, 10);

        const pip = (side: "success" | "failure", index: number, filled: boolean): string => `
            <button type="button" class="dnd5e-pip dnd5e-pip-${side} ${index <= (side === "success" ? success : failure) ? "is-filled" : ""}"
                data-dnd5e-act="death-save" data-side="${side}" data-index="${index}"
                aria-pressed="${index <= (side === "success" ? success : failure) ? "true" : "false"}"
                aria-label="死亡豁免${side === "success" ? "成功" : "失败"} ${index}"
                ${editable ? "" : " disabled"}></button>`;

        const exhaustionPips = Array.from({ length: 7 }, (_value, level) => `
            <button type="button" class="dnd5e-pip dnd5e-pip-exhaustion ${level <= exhaustion ? "is-filled" : ""}"
                data-dnd5e-act="exhaustion" data-level="${level}"
                aria-pressed="${level === exhaustion ? "true" : "false"}"
                aria-label="疲惫等级 ${level}${level === 0 ? "（无）" : ""}"
                ${editable ? "" : " disabled"}>${level}</button>`).join("");

        return `
            <aside class="dnd5e-sheet-rail" aria-label="角色卡侧栏">
                ${renderPortrait(card, sheet, avatar, editable)}

                <div class="dnd5e-ac-badge">
                    <span class="dnd5e-ac-label">AC</span>
                    ${editable
                        ? `<input type="number" inputmode="numeric" class="dnd5e-ac-input" ${bindAttrs("armorClass", "number")}
                            value="${escapeHtml(String(ac))}" min="0" aria-label="护甲等级">`
                        : `<strong class="dnd5e-ac-value">${ac}</strong>`}
                    <small class="dnd5e-ac-note" title="后端派生只按 armorClass 字段计算，不读装备">护甲等级</small>
                </div>

                <div class="dnd5e-hp">
                    <div class="dnd5e-hp-head">
                        <span>生命值</span>
                        <span class="dnd5e-temp-hp">临时 <strong data-dnd5e-live="hp-temp">${temp}</strong></span>
                    </div>
                    <div class="dnd5e-hp-inputs">
                        <input type="number" inputmode="numeric" class="dnd5e-input dnd5e-input-number" ${bindAttrs("hp.current", "number")}
                            value="${escapeHtml(String(current))}" min="0" aria-label="当前生命值"${readonlyAttr(editable)}>
                        <span class="dnd5e-hp-slash">/</span>
                        <input type="number" inputmode="numeric" class="dnd5e-input dnd5e-input-number" ${bindAttrs("hp.max", "number")}
                            value="${escapeHtml(String(max))}" min="1" aria-label="生命上限"${readonlyAttr(editable)}>
                        <input type="number" inputmode="numeric" class="dnd5e-input dnd5e-input-number dnd5e-temp-input" ${bindAttrs("hp.temp", "number")}
                            value="${escapeHtml(String(temp))}" min="0" aria-label="临时生命值"${readonlyAttr(editable)}>
                    </div>
                    <div class="dnd5e-hp-bar" role="progressbar" aria-label="生命值"
                        aria-valuemin="0" aria-valuemax="${max}" aria-valuenow="${current}">
                        <i data-dnd5e-live="hp-bar" style="width:${pct}%"></i>
                    </div>
                    <div class="dnd5e-hp-meta">
                        <span data-dnd5e-live="hp-ratio">${current} / ${max}</span>
                        <span class="dnd5e-hitdice" title="生命骰">生命骰 ${escapeHtml(hitDiceLabel(sheet))}</span>
                    </div>
                </div>

                <div class="dnd5e-death-saves ${deathActive ? "is-active" : ""}">
                    <span class="dnd5e-rail-title">死亡豁免</span>
                    <div class="dnd5e-death-row"><span>成功</span>${[1, 2, 3].map((index) => pip("success", index, index <= success)).join("")}</div>
                    <div class="dnd5e-death-row"><span>失败</span>${[1, 2, 3].map((index) => pip("failure", index, index <= failure)).join("")}</div>
                    <small class="dnd5e-death-note">${deathActive ? "HP 归零：每回合死亡豁免" : "HP 大于 0 时不需要记录"}</small>
                </div>

                <div class="dnd5e-exhaustion">
                    <span class="dnd5e-rail-title">疲惫</span>
                    <div class="dnd5e-exhaustion-pips">${exhaustionPips}</div>
                    <small class="dnd5e-exhaustion-note" data-dnd5e-live="exhaustion-note">${escapeHtml(EXHAUSTION_NOTES[exhaustion] ?? "")}</small>
                </div>
            </aside>
        `;
    }

    function renderDetailsPanel(sheet: Dnd5eSheet, editable: boolean): string {
        const prof = proficiencyBonusOf(sheet);
        const abilities = ABILITIES.map((item) => {
            const score = abilityScore(sheet, item.key);
            const mod = abilityModifier(score);
            const isSave = hasSave(sheet, item.key);
            return `
                <div class="dnd5e-ability ${isSave ? "is-save-proficient" : ""}">
                    <div class="dnd5e-ability-head">
                        <strong>${escapeHtml(item.label)}</strong>
                        <small>${escapeHtml(item.short)}</small>
                    </div>
                    ${editable
                        ? numberInput(`abilities.${item.key}`, score, true, { min: 1, max: 30, label: `${item.label}属性值`, cls: "dnd5e-ability-score" })
                        : `<span class="dnd5e-ability-score-static">${score}</span>`}
                    <span class="dnd5e-ability-mod" data-dnd5e-live="mod.${item.key}">${signed(mod)}</span>
                    <label class="dnd5e-save-toggle">
                        <input type="checkbox" data-dnd5e-toggle="save" data-key="${item.key}" ${isSave ? "checked" : ""}
                            ${editable ? "" : " disabled"} aria-label="${escapeHtml(item.label)}豁免熟练">
                        <span>豁免 <strong data-dnd5e-live="save.${item.key}">${signed(saveBonusOf(sheet, item.key))}</strong></span>
                    </label>
                </div>
            `;
        }).join("");

        const skillRows = SKILLS.map((skill) => {
            const flags = skillProficiency(sheet, skill.key);
            const abilityLabel = ABILITIES.find((item) => item.key === skill.ability)?.short ?? "";
            return `
                <tr class="${flags.proficient ? "is-proficient" : ""}">
                    <td class="dnd5e-skill-name">${escapeHtml(skill.label)}</td>
                    <td class="dnd5e-skill-ability">${escapeHtml(abilityLabel)}</td>
                    <td class="dnd5e-skill-flag">
                        <input type="checkbox" data-dnd5e-toggle="skill" data-key="${skill.key}" ${flags.proficient ? "checked" : ""}
                            ${editable ? "" : " disabled"} aria-label="${escapeHtml(skill.label)}熟练">
                    </td>
                    <td class="dnd5e-skill-flag">
                        <input type="checkbox" data-dnd5e-toggle="expertise" data-key="${skill.key}" ${flags.expertise ? "checked" : ""}
                            ${editable && (flags.proficient || flags.expertise) ? "" : " disabled"} aria-label="${escapeHtml(skill.label)}专精">
                    </td>
                    <td class="dnd5e-skill-bonus" data-dnd5e-live="skill.${skill.key}">${signed(skillBonusOf(sheet, skill.key))}</td>
                </tr>
            `;
        }).join("");

        const specialityRow = (label: string, path: string, value: unknown): string => `
            <label class="dnd5e-field">
                <span>${escapeHtml(label)}</span>
                ${editable
                    ? textInput(path, stringList(value).join("，"), true, { label })
                    : `<span class="dnd5e-field-static">${escapeHtml(stringList(value).join("，") || "—")}</span>`}
            </label>`;

        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-dice-d20" aria-hidden="true"></i> 属性与豁免</h4>
                <div class="dnd5e-ability-grid">${abilities}</div>
            </div>

            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-compass" aria-hidden="true"></i> 派生数值</h4>
                <div class="dnd5e-derived-grid">
                    <div class="dnd5e-derived"><span>被动感知</span><strong data-dnd5e-live="passive">${passivePerceptionOf(sheet)}</strong></div>
                    <div class="dnd5e-derived"><span>先攻</span><strong data-dnd5e-live="initiative">${signed(initiativeOf(sheet))}</strong></div>
                    <div class="dnd5e-derived"><span>熟练加值</span><strong data-dnd5e-live="prof">${signed(prof)}</strong></div>
                    <div class="dnd5e-derived"><span>速度（尺）</span>
                        ${editable
                            ? numberInput("speed", sheet.speed, true, { min: 0, label: "速度", cls: "dnd5e-derived-input" })
                            : `<strong data-dnd5e-live="speed">${asInt(sheet.speed, 0)}</strong>`}
                    </div>
                    <div class="dnd5e-derived"><span>法术豁免 DC</span><strong data-dnd5e-live="spell-dc">${spellcastingOf(sheet)?.dc ?? "—"}</strong></div>
                    <div class="dnd5e-derived"><span>法术攻击</span><strong data-dnd5e-live="spell-attack">${spellcastingOf(sheet) ? signed(spellcastingOf(sheet)!.attack) : "—"}</strong></div>
                    <div class="dnd5e-derived dnd5e-derived-wide"><span>生命骰（如 5d8）</span>
                        ${editable
                            ? textInput("hitDice", sheet.hitDice, true, { label: "生命骰", placeholder: "5d8" })
                            : `<strong>${escapeHtml(sheet.hitDice || "—")}</strong>`}
                    </div>
                </div>
            </div>

            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-table" aria-hidden="true"></i> 技能</h4>
                <div class="dnd5e-skill-shell">
                    <table class="dnd5e-skill-table">
                        <thead>
                            <tr><th scope="col">技能</th><th scope="col">属性</th><th scope="col">熟练</th><th scope="col">专精</th><th scope="col">加值</th></tr>
                        </thead>
                        <tbody>${skillRows}</tbody>
                    </table>
                </div>
            </div>

            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-shield" aria-hidden="true"></i> 其它熟练</h4>
                <div class="dnd5e-field-grid">
                    ${specialityRow("护甲", "proficiencies.armor", sheet.proficiencies?.armor)}
                    ${specialityRow("武器", "proficiencies.weapons", sheet.proficiencies?.weapons)}
                    ${specialityRow("工具", "proficiencies.tools", sheet.proficiencies?.tools)}
                    ${specialityRow("语言", "proficiencies.languages", sheet.proficiencies?.languages)}
                </div>
            </div>
        `;
    }

    function renderInventoryPanel(sheet: Dnd5eSheet, editable: boolean): string {
        const equipment = Array.isArray(sheet.equipment) ? sheet.equipment : [];
        const totalWeight = equipment.reduce((sum, item) => sum + Math.max(0, Number(item.weight) || 0) * Math.max(1, asInt(item.quantity, 1)), 0);
        const rows = equipment.map((item, index) => `
            <tr class="${item.equipped ? "is-equipped" : ""}">
                <td>${editable
                    ? textInput(`equipment.${index}.name`, item.name, true, { label: "装备名称", placeholder: "名称" })
                    : escapeHtml(item.name || "未命名")}</td>
                <td>${editable
                    ? numberInput(`equipment.${index}.quantity`, item.quantity, true, { min: 1, label: "数量", cls: "dnd5e-qty-input" })
                    : asInt(item.quantity, 1)}</td>
                <td>${editable
                    ? numberInput(`equipment.${index}.weight`, item.weight, true, { min: 0, label: "重量", cls: "dnd5e-qty-input" })
                    : Math.max(0, Number(item.weight) || 0)}</td>
                <td class="dnd5e-equip-cell">
                    <input type="checkbox" ${bindAttrs(`equipment.${index}.equipped`, "bool")}
                        ${item.equipped ? "checked" : ""} ${editable ? "" : " disabled"} aria-label="是否装备">
                </td>
                <td>${editable
                    ? textInput(`equipment.${index}.notes`, item.notes, true, { label: "备注", placeholder: "备注" })
                    : escapeHtml(item.notes || "—")}</td>
                <td class="dnd5e-row-actions">
                    ${editable ? `<button type="button" class="dnd5e-icon-btn" data-dnd5e-act="del-item" data-index="${index}" aria-label="删除装备">
                        <i class="fa fa-trash" aria-hidden="true"></i></button>` : ""}
                </td>
            </tr>
        `).join("");
        const equippedCount = equipment.filter((item) => item.equipped).length;
        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-suitcase" aria-hidden="true"></i> 装备清单</h4>
                <p class="dnd5e-hint">
                    合计重量 ${totalWeight.toFixed(1)} 磅 · 已装备 ${equippedCount} 件。
                    <strong>AC 仍以左侧徽章（armorClass 字段）为准</strong>：后端派生只读这个字段，不按装备推算，
                    所以「是否装备」只作为物品状态记录，不会自动改 AC。
                </p>
                <div class="dnd5e-table-shell">
                    <table class="dnd5e-table">
                        <thead>
                            <tr><th scope="col">名称</th><th scope="col">数量</th><th scope="col">重量</th><th scope="col">装备</th><th scope="col">备注</th><th scope="col"><span class="visually-hidden">操作</span></th></tr>
                        </thead>
                        <tbody>${rows || `<tr><td colspan="6" class="dnd5e-empty-cell">还没有装备；点下方「添加物品」开始记录。</td></tr>`}</tbody>
                    </table>
                </div>
                ${editable ? `<button type="button" class="dnd5e-btn" data-dnd5e-act="add-item">
                    <i class="fa fa-plus" aria-hidden="true"></i> 添加物品</button>` : ""}
            </div>
        `;
    }

    function renderFeaturesPanel(sheet: Dnd5eSheet, editable: boolean): string {
        const features = stringList(sheet.features);
        const rows = features.map((item, index) => `
            <li class="dnd5e-list-row">
                <span class="dnd5e-list-index">${index + 1}</span>
                ${editable
                    ? textInput(`features.${index}`, item, true, { label: `特性 ${index + 1}` })
                    : `<span class="dnd5e-list-text">${escapeHtml(item)}</span>`}
                ${editable ? `<button type="button" class="dnd5e-icon-btn" data-dnd5e-act="del-feature" data-index="${index}" aria-label="删除特性">
                    <i class="fa fa-trash" aria-hidden="true"></i></button>` : ""}
            </li>
        `).join("");
        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-star" aria-hidden="true"></i> 特性与能力</h4>
                <p class="dnd5e-hint">职业特性、种族特性、专长等文本条目；每行一条，后端按纯文本列表保存。</p>
                <ul class="dnd5e-list">${rows || '<li class="dnd5e-empty-cell">还没有特性条目。</li>'}</ul>
                ${editable ? `<button type="button" class="dnd5e-btn" data-dnd5e-act="add-feature">
                    <i class="fa fa-plus" aria-hidden="true"></i> 添加特性</button>` : ""}
            </div>
        `;
    }

    function renderSpellsPanel(sheet: Dnd5eSheet, editable: boolean): string {
        const casting = sheet.spellcasting || {};
        const slots = casting.slots || {};
        const levels = Object.keys(slots).sort((a, b) => asInt(a) - asInt(b));
        const known = stringList(casting.known);
        const prepared = stringList(casting.prepared);
        // 已知里出现但准备表里没有的也一起列出（旧卡可能只写了 prepared）。
        const allSpells: string[] = [];
        [...known, ...prepared].forEach((item) => {
            if (!allSpells.some((existing) => existing.toLowerCase() === item.toLowerCase())) allSpells.push(item);
        });
        const spellcasting = spellcastingOf(sheet);
        const abilityOptions = ABILITIES.map((item) => `
            <option value="${item.key}" ${casting.ability === item.key ? "selected" : ""}>${escapeHtml(item.label)} (${item.short})</option>`).join("");

        const slotRows = levels.map((level) => {
            const row = slots[level] || {};
            const max = asInt(row.max, 0);
            const used = Math.max(0, asInt(row.used, 0));
            return `
                <tr>
                    <th scope="row">${escapeHtml(level)} 环</th>
                    <td>${editable ? numberInput(`spellcasting.slots.${level}.max`, max, true, { min: 0, label: `${level} 环上限` }) : max}</td>
                    <td>${editable ? numberInput(`spellcasting.slots.${level}.used`, used, true, { min: 0, label: `${level} 环已用` }) : used}</td>
                    <td class="dnd5e-row-actions">
                        ${editable ? `
                        <button type="button" class="dnd5e-icon-btn" data-dnd5e-act="slot-step" data-level="${level}" data-delta="-1" aria-label="${level} 环已用 -1">
                            <i class="fa fa-minus" aria-hidden="true"></i></button>
                        <button type="button" class="dnd5e-icon-btn" data-dnd5e-act="slot-step" data-level="${level}" data-delta="1" aria-label="${level} 环已用 +1">
                            <i class="fa fa-plus" aria-hidden="true"></i></button>` : ""}
                    </td>
                </tr>
            `;
        }).join("");

        const spellRows = allSpells.map((name, index) => {
            const isPrepared = prepared.some((item) => item.toLowerCase() === name.toLowerCase());
            return `
                <tr class="${isPrepared ? "is-proficient" : ""}">
                    <td>${editable
                        ? textInput(`spellcasting.known.${index}`, name, true, { label: `法术 ${index + 1}`, placeholder: "法术名" })
                        : escapeHtml(name)}</td>
                    <td class="dnd5e-skill-flag">
                        <input type="checkbox" data-dnd5e-toggle="prepared" data-key="${escapeHtml(name)}" ${isPrepared ? "checked" : ""}
                            ${editable ? "" : " disabled"} aria-label="${escapeHtml(name)}已准备">
                    </td>
                    <td class="dnd5e-row-actions">
                        ${editable ? `<button type="button" class="dnd5e-icon-btn" data-dnd5e-act="del-spell" data-key="${escapeHtml(name)}" aria-label="删除法术">
                            <i class="fa fa-trash" aria-hidden="true"></i></button>` : ""}
                    </td>
                </tr>
            `;
        }).join("");

        const missingLevels = [1, 2, 3, 4, 5, 6, 7, 8, 9].filter((level) => !levels.includes(String(level)));

        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-magic" aria-hidden="true"></i> 施法</h4>
                <div class="dnd5e-cast-grid">
                    <label class="dnd5e-field">
                        <span>施法属性</span>
                        ${editable
                            ? `<select class="dnd5e-input" data-dnd5e-select="spellcasting.ability" aria-label="施法属性">
                                <option value="" ${casting.ability ? "" : "selected"}>非施法者</option>${abilityOptions}</select>`
                            : `<span class="dnd5e-field-static">${casting.ability ? escapeHtml(casting.ability.toUpperCase()) : "非施法者"}</span>`}
                    </label>
                    <div class="dnd5e-derived"><span>法术豁免 DC</span><strong data-dnd5e-live="spell-dc">${spellcasting?.dc ?? "—"}</strong></div>
                    <div class="dnd5e-derived"><span>法术攻击加值</span><strong data-dnd5e-live="spell-attack">${spellcasting ? signed(spellcasting.attack) : "—"}</strong></div>
                </div>
            </div>

            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-sun-o" aria-hidden="true"></i> 法术位</h4>
                ${levels.length ? `
                <div class="dnd5e-table-shell">
                    <table class="dnd5e-table dnd5e-slot-table">
                        <thead><tr><th scope="col">环阶</th><th scope="col">上限</th><th scope="col">已用</th><th scope="col"><span class="visually-hidden">调整</span></th></tr></thead>
                        <tbody>${slotRows}</tbody>
                    </table>
                </div>` : '<p class="dnd5e-hint">还没有法术位。施法者职业首次保存后插件会自动补齐，也可以手动添加环阶。</p>'}
                ${editable && missingLevels.length ? `
                <div class="dnd5e-slot-add">
                    <span>添加环阶：</span>
                    ${missingLevels.map((level) => `<button type="button" class="dnd5e-mini" data-dnd5e-act="add-slot" data-level="${level}">${level}</button>`).join("")}
                </div>` : ""}
            </div>

            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-book" aria-hidden="true"></i> 已知 / 准备法术</h4>
                <div class="dnd5e-table-shell">
                    <table class="dnd5e-table">
                        <thead><tr><th scope="col">法术</th><th scope="col">已准备</th><th scope="col"><span class="visually-hidden">操作</span></th></tr></thead>
                        <tbody>${spellRows || '<tr><td colspan="3" class="dnd5e-empty-cell">还没有法术条目。</td></tr>'}</tbody>
                    </table>
                </div>
                ${editable ? `<button type="button" class="dnd5e-btn" data-dnd5e-act="add-spell">
                    <i class="fa fa-plus" aria-hidden="true"></i> 添加法术</button>` : ""}
            </div>
        `;
    }

    /** 效果清单一行：来自哪件效果 / 哪条路径 / 值 / 为什么（没）生效。 */
    interface EffectRow {
        effect: string;
        key: string;
        mode: number;
        value: string;
        tier: Dnd5eEffectTier;
        note: string;
        source: string;
    }

    const EFFECT_MODE_LABELS: Record<number, string> = {
        0: "custom",
        1: "×",
        2: "+",
        3: "取低",
        4: "取高",
        5: "覆盖",
    };

    /**
     * 卡面效果清单：按引擎的三档分级分组。
     *
     * 后端 ``pack`` 会把每条 change 标上 ``applied``（full / stored /
     * unsupported）+ ``active`` + ``reason``；没有分级的旧卡按 stored 处理，
     * 保存一次就会刷新（不猜、不假装生效）。
     */
    function effectRows(sheet: Dnd5eSheet): EffectRow[] {
        const rows: EffectRow[] = [];
        const collect = (effects: Dnd5eEffect[] | undefined, source: string): void => {
            (effects || []).forEach((effect) => {
                (effect.changes || []).forEach((change) => {
                    const tier: Dnd5eEffectTier =
                        change.applied === "full" && change.active !== false
                            ? "full"
                            : change.applied === "unsupported"
                                ? "unsupported"
                                : "stored";
                    rows.push({
                        effect: String(effect.name || "未命名效果"),
                        key: String(change.key || ""),
                        mode: Number(change.mode ?? 2),
                        value: String(change.value ?? ""),
                        tier,
                        note: String(change.note || change.reason || ""),
                        source,
                    });
                });
            });
        };
        collect(sheet.effect_changes, "角色");
        (sheet.equipment || []).forEach((item) => {
            if (item.effects && item.effects.length) {
                collect(item.effects, String(item.name || "装备"));
            }
        });
        return rows;
    }

    function renderEffectTable(rows: EffectRow[]): string {
        return `
            <div class="dnd5e-table-shell">
                <table class="dnd5e-table">
                    <thead><tr><th scope="col">效果</th><th scope="col">路径</th><th scope="col">值</th><th scope="col">说明</th></tr></thead>
                    <tbody>${rows.map((row) => `
                        <tr>
                            <td>${escapeHtml(row.effect)}<span class="dnd5e-hint">（${escapeHtml(row.source)}）</span></td>
                            <td><code>${escapeHtml(row.key)}</code></td>
                            <td>${escapeHtml(`${EFFECT_MODE_LABELS[row.mode] || row.mode} ${row.value}`)}</td>
                            <td>${escapeHtml(row.note || "—")}</td>
                        </tr>`).join("") || '<tr><td colspan="4" class="dnd5e-empty-cell">没有效果改动。</td></tr>'}</tbody>
                </table>
            </div>`;
    }

    function renderEffectsPanel(sheet: Dnd5eSheet, editable: boolean): string {
        const status = statusList(sheet.status);
        const rows = status.map((item, index) => `
            <li class="dnd5e-list-row">
                <span class="dnd5e-status-chip">${escapeHtml(conditionLabel(item))}</span>
                ${editable
                    ? textInput(`status.${index}`, item, true, { label: `状态 ${index + 1}` })
                    : `<span class="dnd5e-list-text">${escapeHtml(item)}</span>`}
                ${editable ? `<button type="button" class="dnd5e-icon-btn" data-dnd5e-act="del-condition" data-index="${index}" aria-label="删除状态">
                    <i class="fa fa-trash" aria-hidden="true"></i></button>` : ""}
            </li>
        `).join("");
        const allEffectRows = effectRows(sheet);
        const activeEffects = allEffectRows.filter((row) => row.tier === "full");
        const inactiveEffects = allEffectRows.filter((row) => row.tier !== "full");
        const quickAdd = CONDITIONS.map((item) => {
            const present = status.some((entry) => sameCondition(entry, item.id));
            return `<button type="button" class="dnd5e-chip ${present ? "is-on" : ""}" data-dnd5e-act="quick-condition"
                data-condition="${item.id}" aria-pressed="${present ? "true" : "false"}"
                ${editable ? "" : " disabled"}>${escapeHtml(item.label)}</button>`;
        }).join("");
        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-flask" aria-hidden="true"></i> 常见条件</h4>
                <p class="dnd5e-hint">点一下即可添加 / 移除；存储英文条件 id（与 FVTT、插件的条件表一致），页面显示中文。</p>
                <div class="dnd5e-chip-row">${quickAdd}</div>
            </div>
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-list" aria-hidden="true"></i> 当前状态</h4>
                <ul class="dnd5e-list">${rows || '<li class="dnd5e-empty-cell">当前没有状态 / 条件。</li>'}</ul>
                ${editable ? `<button type="button" class="dnd5e-btn" data-dnd5e-act="add-condition">
                    <i class="fa fa-plus" aria-hidden="true"></i> 添加自定义状态</button>` : ""}
            </div>
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-flask" aria-hidden="true"></i> 效果清单（已生效）</h4>
                <p class="dnd5e-hint">FVTT 效果按路径改写数值：这里的每一条都真的进了 AC / 先攻 / 豁免 / 检定 / 攻击 / 抗性 / 速度等派生值。</p>
                ${renderEffectTable(activeEffects)}
            </div>
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-archive" aria-hidden="true"></i> 已记录·暂未生效</h4>
                <p class="dnd5e-hint">路径已按 FVTT 数据模型记下（含模块 flags、Token 外观、物品活动、分速等），但当前没有对应的数值消费方，或效果本身未激活；数值不受影响，清单如实保留。</p>
                ${renderEffectTable(inactiveEffects)}
            </div>
        `;
    }

    function renderBiographyPanel(card: Dnd5eCard, sheet: Dnd5eSheet, editable: boolean): string {
        const textArea = (label: string, path: string, value: unknown, rows: number, placeholder: string): string => `
            <label class="dnd5e-field dnd5e-field-wide">
                <span>${escapeHtml(label)}</span>
                ${editable
                    ? `<textarea class="dnd5e-input dnd5e-textarea" rows="${rows}" ${bindAttrs(path, "text")}
                        aria-label="${escapeHtml(label)}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(String(value ?? ""))}</textarea>`
                    : `<div class="dnd5e-prose">${escapeHtml(String(value ?? "") || "—")}</div>`}
            </label>`;
        return `
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-book" aria-hidden="true"></i> 传记</h4>
                <div class="dnd5e-bio-grid">
                    ${textArea("角色简介", "bio", sheet.bio, 6, "来历、性格、目标……")}
                    ${textArea("外观描述", "appearance", sheet.appearance, 4, "身高、发色、标志性装束……")}
                    ${textArea("备注", "notes", sheet.notes, 4, "给 KP 的备注、道具线索、约定……")}
                </div>
            </div>
            <div class="dnd5e-panel-section">
                <h4 class="dnd5e-section-title"><i class="fa fa-id-card-o" aria-hidden="true"></i> 基础信息</h4>
                <div class="dnd5e-field-grid">
                    ${[["职业", "class", sheet.class], ["子职", "subclass", sheet.subclass], ["种族", "race", sheet.race],
                        ["背景", "background", sheet.background], ["阵营", "alignment", sheet.alignment]].map(([label, path, value]) => `
                        <label class="dnd5e-field">
                            <span>${escapeHtml(label as string)}</span>
                            ${editable
                                ? textInput(path as string, value, true, { label: label as string })
                                : `<span class="dnd5e-field-static">${escapeHtml(String(value ?? "") || "—")}</span>`}
                        </label>`).join("")}
                    <label class="dnd5e-field">
                        <span>等级</span>
                        ${editable
                            ? numberInput("level", sheet.level, true, { min: 1, max: 20, label: "等级" })
                            : `<span class="dnd5e-field-static">${levelOf(sheet)}</span>`}
                    </label>
                </div>
                <p class="dnd5e-hint">更新记录：${escapeHtml(String(card.updatedAt || "尚未保存"))}</p>
            </div>
        `;
    }

    function renderSheet(card: Dnd5eCard, editing: Dnd5eDraft | null): string {
        const isDraft = Boolean(editing && editing.cardId === card.id);
        const editable = isDraft && canEditCard(card);
        const sheet = isDraft && editing ? editing.sheet : normalizeSheet(card.sheet);
        const name = isDraft && editing ? editing.name : card.name;
        const avatar = isDraft && editing ? editing.avatar : String(card.avatar || "");

        // 同一页面可能同时存在工作区卡面与房间预览弹窗，id 必须按实例唯一。
        const scope = nextId();
        const tabId = (key: string): string => `${scope}-tab-${key}`;
        const panelId = (key: string): string => `${scope}-panel-${key}`;

        const tabButtons = TABS.map((tab) => `
            <button type="button" role="tab" class="dnd5e-tab" data-dnd5e-tab="${tab.key}"
                id="${tabId(tab.key)}" aria-controls="${panelId(tab.key)}"
                aria-selected="${tab.key === activeTab ? "true" : "false"}"
                tabindex="${tab.key === activeTab ? 0 : -1}">
                <i class="fa ${tab.icon}" aria-hidden="true"></i><span>${escapeHtml(tab.label)}</span>
            </button>`).join("");

        const panel = (key: Dnd5eTabKey, content: string): string => `
            <div role="tabpanel" class="dnd5e-tabpanel" data-dnd5e-panel="${key}" id="${panelId(key)}"
                ${key === activeTab ? "" : "hidden"}
                aria-labelledby="${tabId(key)}"
                aria-label="${escapeHtml(TABS.find((item) => item.key === key)?.label || key)}">${content}</div>`;

        const panels = [
            panel("details", renderDetailsPanel(sheet, editable)),
            panel("inventory", renderInventoryPanel(sheet, editable)),
            panel("features", renderFeaturesPanel(sheet, editable)),
            panel("spells", renderSpellsPanel(sheet, editable)),
            panel("effects", renderEffectsPanel(sheet, editable)),
            panel("biography", renderBiographyPanel(card, sheet, editable)),
        ].join("");

        return `
            <div class="dnd5e-sheet" data-dnd5e-sheet data-card-id="${escapeHtml(card.id)}" data-editable="${editable ? "true" : "false"}">
                ${renderHeader(card, sheet, name, editable)}
                <div class="dnd5e-sheet-body">
                    ${renderRail(card, sheet, avatar, editable)}
                    <section class="dnd5e-sheet-main" aria-label="角色卡页签">
                        <div class="dnd5e-tablist" role="tablist" aria-label="角色卡页签">${tabButtons}</div>
                        <div class="dnd5e-tabpanels">${panels}</div>
                    </section>
                </div>
                ${editable ? "" : `<p class="dnd5e-readonly-note"><i class="fa fa-lock" aria-hidden="true"></i> 只读视图：你没有这张角色卡的编辑权限。</p>`}
            </div>
        `;
    }

    /** 房间内公开卡预览（rooms.ts 的弹窗）用只读渲染；页签仍可切换。 */
    function renderCharacterDetail(card: Dnd5eCard, _roomDiff?: unknown): string {
        return renderSheet(card, null);
    }

    // ---------------------------------------------------- 局部刷新（实时派生）

    function setLiveText(root: HTMLElement, key: string, text: string): void {
        const node = root.querySelector<HTMLElement>(`[data-dnd5e-live="${key}"]`);
        if (node) node.textContent = text;
    }

    function syncLiveReadouts(root: HTMLElement): void {
        if (!draft) return;
        const sheet = draft.sheet;
        ABILITIES.forEach((item) => {
            setLiveText(root, `mod.${item.key}`, signed(abilityModifier(abilityScore(sheet, item.key))));
            setLiveText(root, `save.${item.key}`, signed(saveBonusOf(sheet, item.key)));
        });
        SKILLS.forEach((skill) => {
            setLiveText(root, `skill.${skill.key}`, signed(skillBonusOf(sheet, skill.key)));
        });
        setLiveText(root, "passive", String(passivePerceptionOf(sheet)));
        setLiveText(root, "initiative", signed(initiativeOf(sheet)));
        setLiveText(root, "prof", signed(proficiencyBonusOf(sheet)));
        const casting = spellcastingOf(sheet);
        setLiveText(root, "spell-dc", casting ? String(casting.dc) : "—");
        setLiveText(root, "spell-attack", casting ? signed(casting.attack) : "—");
        const hp = sheet.hp || {};
        const current = Math.max(0, asInt(hp.current, 0));
        const max = Math.max(1, asInt(hp.max, 1));
        setLiveText(root, "hp-ratio", `${current} / ${max}`);
        setLiveText(root, "hp-temp", String(Math.max(0, asInt(hp.temp, 0))));
        const bar = root.querySelector<HTMLElement>('[data-dnd5e-live="hp-bar"]');
        if (bar) bar.style.width = `${Math.max(0, Math.min(100, Math.round((current / max) * 100)))}%`;
        const hpBar = bar?.parentElement;
        if (hpBar) hpBar.setAttribute("aria-valuenow", String(current));
        const xp = xpProgressOf(sheet);
        setLiveText(root, "xp-next", xp.maxed ? "已满级" : `下一级 ${xp.next}`);
        const xpBar = root.querySelector<HTMLElement>('[data-dnd5e-live="xp-bar"]');
        if (xpBar) xpBar.style.width = `${xp.pct}%`;
        setLiveText(root, "exhaustion-note", EXHAUSTION_NOTES[clamp(sheet.exhaustion, 0, 6, 0)] ?? "");
    }

    // ------------------------------------------------------------ 草稿写入

    function draftRoot(): Record<string, unknown> | null {
        if (!draft) return null;
        return draft.sheet as unknown as Record<string, unknown>;
    }

    function setDraftValue(path: string, raw: string, kind: "text" | "number" | "list" | "bool", checked: boolean): void {
        if (!draft) return;
        const parts = path.split(".").filter(Boolean);
        const head = parts[0];
        if (!head) return;
        if (head === "name") {
            draft.name = raw;
            return;
        }
        let node = draftRoot();
        if (!node) return;
        for (let index = 0; index < parts.length - 1; index += 1) {
            const key = parts[index];
            if (key === undefined) return;
            const child = node[key];
            if (child && typeof child === "object") {
                node = child as Record<string, unknown>;
            } else {
                const created: Record<string, unknown> = {};
                node[key] = created;
                node = created;
            }
        }
        const leaf = parts[parts.length - 1];
        if (leaf === undefined) return;
        if (kind === "number") node[leaf] = asInt(raw, 0);
        else if (kind === "bool") node[leaf] = checked;
        else if (kind === "list") node[leaf] = splitList(raw);
        else node[leaf] = raw;
    }

    function toggleListValue(path: "skills" | "expertise" | "saves", key: string, on: boolean): void {
        if (!draft) return;
        const proficiencies = draft.sheet.proficiencies ?? {};
        if (path === "saves") {
            const next = normalizeSaveList(proficiencies.saves).filter((item) => item !== key);
            if (on) next.push(key);
            draft.sheet.proficiencies = { ...proficiencies, saves: next };
            return;
        }
        const current = normalizeSkillList(proficiencies[path]);
        const next = current.filter((item) => item !== key);
        if (on) next.push(key);
        const patch: Dnd5eProficiencies = { ...proficiencies, [path]: next };
        // 取消熟练时同步清掉专精，避免出现「无熟练却有专精」的非法组合。
        if (path === "skills" && !on) {
            patch.expertise = normalizeSkillList(proficiencies.expertise).filter((item) => item !== key);
        }
        if (path === "expertise" && on) {
            const skills = normalizeSkillList(proficiencies.skills);
            if (!skills.includes(key)) skills.push(key);
            patch.skills = skills;
        }
        draft.sheet.proficiencies = patch;
    }

    // ---------------------------------------------------------------- 保存

    function buildPayload(card: Dnd5eCard): Dnd5eCard | null {
        if (!draft) return null;
        const sheet = normalizeSheet(draft.sheet);
        const name = draft.name.trim() || "未命名冒险者";
        const avatar = draft.avatar.trim();
        return {
            ...card,
            name,
            sheet,
            rulesetId: RULESET_ID,
            ...(avatar ? { avatar } : {}),
        };
    }

    async function saveDraft(): Promise<void> {
        const card = activeCard();
        if (!card || !draft || saving) return;
        const payload = buildPayload(card);
        if (!payload) return;
        saving = true;
        renderDetailView();
        try {
            const response = await TrpgApi.put<ApiResponse<Dnd5eCardInput>>(
                `/api/characters/${encodeURIComponent(payload.id)}`,
                payload as unknown as Record<string, unknown>,
            );
            if (!response?.success) {
                notify(response?.message || response?.error || "保存角色卡失败", "error");
                return;
            }
            const saved = createCharacterCard(response.data || payload);
            cards = cards.some((item) => item.id === saved.id)
                ? cards.map((item) => item.id === saved.id ? saved : item)
                : [saved, ...cards];
            activeCardId = saved.id;
            startDraft(saved);
            notify("角色卡已保存", "success");
        } catch (error) {
            notify(`保存角色卡失败：${escapeHtml((error as Error)?.message || error)}`, "error");
        } finally {
            saving = false;
            renderDetailView();
        }
    }

    async function discardDraft(): Promise<void> {
        const card = activeCard();
        if (!card) return;
        if (draftDirty) {
            const confirmed = await confirmAction("丢弃未保存的修改？", "重置角色卡");
            if (!confirmed) return;
        }
        startDraft(card);
        renderDetailView();
    }

    /**
     * 升级向导（四步：确认 / 生命值 / 提升 / 法术与预览）。
     *
     * 与建卡向导对称：规则数学在后端 progression.py，向导只提交选择；落库走
     * `POST /api/plugins/dnd5e/characters/<id>/level-up`（内部复用 apply_save）。
     * 升级读的是服务端的卡，所以有未保存改动时先要求保存，避免「升级把草稿盖掉」。
     */
    async function openLevelUpWizard(root: HTMLElement): Promise<void> {
        const card = activeCard();
        if (!card) return;
        if (draftDirty) {
            notify("升级会重新读取服务端的角色卡，请先保存（或重置）当前修改。", "warning");
            return;
        }
        const wizard = global.TrpgDnd5eLevelUp;
        if (!wizard) {
            notify("升级向导未加载（character-levelup.js），已取消。", "error");
            return;
        }
        const level = levelOf(card.sheet);
        if (level >= 20) {
            notify("已经是 20 级，无法继续升级。", "warning");
            return;
        }
        void root;
        wizard.open({
            card: {
                id: card.id,
                name: card.name,
                rulesetId: RULESET_ID,
                sheet: card.sheet as unknown as Record<string, unknown>,
            },
            onSaved: (raw) => applyLevelUpCard(raw),
        });
    }

    /** 升级落库后：换成服务端返回的卡，重置草稿并重渲染（等级徽章 / 页签 / HP 一起更新）。 */
    function applyLevelUpCard(raw: Record<string, unknown>): void {
        const saved = createCharacterCard(raw as Dnd5eCardInput);
        cards = cards.some((item) => item.id === saved.id)
            ? cards.map((item) => item.id === saved.id ? saved : item)
            : [saved, ...cards];
        activeCardId = saved.id;
        startDraft(saved);
        renderDetailView();
        render();
        global.TrpgCharacterListBridge?.refresh?.();
    }

    async function confirmAction(message: string, title: string): Promise<boolean> {
        if (global.TrpgDialog?.confirm) {
            return global.TrpgDialog.confirm({ title, message, confirmText: "确定" });
        }
        return global.confirm(message);
    }

    // ---------------------------------------------------------------- 休息

    function roomMemberFor(card: Dnd5eCard): { roomId: string; member: Dnd5eRoomMember } | null {
        const room = (global as unknown as { currentRoom?: Dnd5eRoomInfo | null }).currentRoom;
        const roomId = String(room?.id || "").trim();
        if (!roomId || !room) return null;
        const member = (room.members || []).find(
            (item) => String(item?.character_card?.id || "") === card.id,
        );
        if (!member) return null;
        return { roomId, member };
    }

    async function askHitDice(): Promise<number | null> {
        const sheet = draft?.sheet;
        const pool = sheet?.hit_dice;
        const available = pool ? Math.max(0, asInt(pool.max, 0) - asInt(pool.used, 0)) : null;
        const hint = available === null ? "默认 1" : `可用 ${available} 颗，默认 1`;
        if (global.TrpgDialog?.prompt) {
            const answer = await global.TrpgDialog.prompt({
                title: "短休",
                message: `本次短休消耗几颗生命骰？（${hint}）`,
                defaultValue: "1",
                confirmText: "休息",
            });
            if (answer === null) return null;
            return Math.max(0, asInt(answer, 1));
        }
        const raw = global.prompt(`本次短休消耗几颗生命骰？（${hint}）`, "1");
        if (raw === null) return null;
        return Math.max(0, asInt(raw, 1));
    }

    function applyRestRow(kind: "short" | "long", row: Dnd5eRestRow): void {
        const sheet = draft?.sheet;
        if (!sheet) return;
        const hp = sheet.hp ?? {};
        const max = Math.max(1, asInt(hp.max, 1));
        const after = Math.max(0, asInt(row.hp_after, asInt(hp.current, 0)));
        sheet.hp = {
            max,
            current: Math.min(max, after),
            temp: kind === "long" ? 0 : Math.max(0, asInt(hp.temp, 0)),
        };
        // 生命骰池：后端把 {die,max,used} 写在房间卡的 sheet.hit_dice 上，这里同步一份。
        const pool = sheet.hit_dice ?? { die: parseHitDie(sheet.hitDice), max: levelOf(sheet), used: 0 };
        const spent = Math.max(0, asInt(row.hit_dice_spent, 0));
        const regained = Math.max(0, asInt(row.hit_dice_regained, 0));
        sheet.hit_dice = {
            die: Math.max(4, asInt(pool.die, 8)),
            max: Math.max(0, asInt(pool.max, levelOf(sheet))),
            used: Math.max(0, Math.min(asInt(pool.max, levelOf(sheet)), asInt(pool.used, 0) + spent - regained)),
        };
        if (row.slots_restored) {
            const slots = sheet.spellcasting?.slots ?? {};
            Object.keys(slots).forEach((level) => {
                const current = slots[level];
                if (current) slots[level] = { max: asInt(current.max, 0), used: 0 };
            });
        }
        if (kind === "long" && typeof row.exhaustion_level === "number") {
            sheet.exhaustion = clamp(row.exhaustion_level, 0, 6, 0);
        }
    }

    function parseHitDie(declared: unknown): number {
        const matched = String(declared ?? "").match(/d(\d+)/i);
        return matched?.[1] ? Math.max(4, asInt(matched[1], 8)) : 8;
    }

    async function requestRest(kind: "short" | "long"): Promise<void> {
        const card = activeCard();
        if (!card) return;
        const target = roomMemberFor(card);
        if (!target) {
            notify("短休 / 长休要在房间内结算：请先把这张角色卡绑定到房间成员。", "warning");
            return;
        }
        let hitDice = 1;
        if (kind === "short") {
            const answer = await askHitDice();
            if (answer === null) return;
            hitDice = answer;
        }
        try {
            const response = await TrpgApi.post<ApiResponse<Dnd5eRestPayload>>(
                `/api/rooms/${encodeURIComponent(target.roomId)}/rules/rest`,
                { kind, hit_dice: hitDice, player_name: String(target.member.username || "") },
            );
            if (!response?.success) {
                notify(response?.message || response?.error || "休息结算失败", "error");
                return;
            }
            const rows = response.data?.members || [];
            const row = rows[0];
            if (!row) {
                notify("休息没有返回结算结果", "error");
                return;
            }
            if (row.error) {
                notify(`休息失败：${row.error}`, "error");
                return;
            }
            if (draft) {
                applyRestRow(kind, row);
                draftDirty = true;
            }
            const label = kind === "long" ? "长休" : "短休";
            const parts = [`${label}结算：HP ${asInt(row.hp_before, 0)} → ${asInt(row.hp_after, 0)}`];
            if (asInt(row.hit_dice_spent, 0)) parts.push(`消耗 ${asInt(row.hit_dice_spent, 0)} 颗生命骰`);
            if (asInt(row.hit_dice_regained, 0)) parts.push(`恢复 ${asInt(row.hit_dice_regained, 0)} 颗生命骰`);
            if (row.slots_restored) parts.push("法术位已回满");
            if (typeof row.exhaustion_level === "number" && kind === "long") parts.push(`疲惫降至 ${row.exhaustion_level} 级`);
            notify(`${parts.join("，")}。结果已写入卡面草稿，记得保存。`, "success");
            (row.notes || []).forEach((note) => notify(note, "info"));
            global.refreshCurrentRoomMembers?.();
            renderDetailView();
        } catch (error) {
            notify(`休息结算失败：${escapeHtml((error as Error)?.message || error)}`, "error");
        }
    }

    // ---------------------------------------------------------------- 页签

    function activateTab(root: HTMLElement, key: string, options: { focus?: boolean; commit?: boolean } = {}): void {
        const tab = normalizeTab(key);
        const buttons = Array.from(root.querySelectorAll<HTMLElement>("[data-dnd5e-tab]"));
        buttons.forEach((button) => {
            const isActive = button.dataset.dnd5eTab === tab;
            button.setAttribute("aria-selected", isActive ? "true" : "false");
            button.tabIndex = isActive ? 0 : -1;
            button.classList.toggle("is-active", isActive);
            if (isActive && options.focus) button.focus();
        });
        Array.from(root.querySelectorAll<HTMLElement>("[data-dnd5e-panel]")).forEach((panel) => {
            panel.hidden = panel.dataset.dnd5ePanel !== tab;
        });
        if (options.commit !== false) {
            activeTab = tab;
            const cardId = String(root.dataset.cardId || "");
            if (cardId) rememberTab(cardId, tab);
        }
    }

    function moveTabFocus(root: HTMLElement, step: 1 | -1 | "home" | "end"): void {
        // 以当前 DOM 里选中的页签为准：预览弹窗与工作区可能同时存在，不能只看模块态。
        const selected = root.querySelector<HTMLElement>('[data-dnd5e-tab][aria-selected="true"]');
        const current = normalizeTab(selected?.dataset.dnd5eTab || activeTab);
        const index = Math.max(0, TAB_KEYS.indexOf(current));
        let next = index;
        if (step === "home") next = 0;
        else if (step === "end") next = TAB_KEYS.length - 1;
        else next = (index + step + TAB_KEYS.length) % TAB_KEYS.length;
        const nextKey = TAB_KEYS[next] ?? "details";
        activateTab(root, nextKey, { focus: true, commit: isInWorkspace(root) });
    }

    // -------------------------------------------------------------- 卡面挂载

    /** 重渲染前给当前焦点元素留一个稳定的找回键，避免勾选后焦点/滚动位置丢失。 */
    function focusTokenOf(element: HTMLElement | null): string {
        if (!element) return "";
        if (element.dataset.dnd5eToggle) return `t|${element.dataset.dnd5eToggle}|${element.dataset.key || ""}`;
        if (element.dataset.dnd5eAct) {
            const suffix = element.dataset.index ?? element.dataset.level ?? element.dataset.condition ?? element.dataset.mode ?? "";
            return `a|${element.dataset.dnd5eAct}|${suffix}`;
        }
        if (element.dataset.dnd5eTab) return `tab|${element.dataset.dnd5eTab}`;
        if (element.dataset.dnd5eBind) return `b|${element.dataset.dnd5eBind}`;
        return "";
    }

    function findByFocusToken(root: HTMLElement, token: string): HTMLElement | null {
        const [kind, name, suffix] = token.split("|");
        if (!kind || !name) return null;
        if (kind === "t") {
            return root.querySelector<HTMLElement>(`[data-dnd5e-toggle="${name}"][data-key="${suffix ?? ""}"]`);
        }
        if (kind === "a") {
            const selector = `[data-dnd5e-act="${name}"]`;
            const candidates = Array.from(root.querySelectorAll<HTMLElement>(selector));
            if (!suffix) return candidates[0] ?? null;
            return candidates.find((element) => {
                const dataset = element.dataset;
                return dataset.index === suffix || dataset.level === suffix
                    || dataset.condition === suffix || dataset.mode === suffix;
            }) ?? candidates[0] ?? null;
        }
        if (kind === "tab") return root.querySelector<HTMLElement>(`[data-dnd5e-tab="${name}"]`);
        return root.querySelector<HTMLElement>(`[data-dnd5e-bind="${name}"]`);
    }

    function renderDetailView(): void {
        const host = document.getElementById("characterDetailView");
        if (!host) return;
        const card = activeCard();
        if (!card) {
            host.innerHTML = "";
            return;
        }
        const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const token = host.contains(activeElement) ? focusTokenOf(activeElement) : "";
        const workspace = document.getElementById("characterWorkspace");
        const scrolls: Array<[HTMLElement, number]> = [];
        if (workspace) scrolls.push([workspace, workspace.scrollTop]);
        const documentScroll = global.scrollY;
        host.innerHTML = renderSheet(card, canEditCard(card) ? draft : null);
        // 只是就地重渲染，滚动位置与焦点应当保持原地，否则勾一个技能就跳回顶部。
        scrolls.forEach(([element, top]) => { element.scrollTop = top; });
        if (typeof documentScroll === "number") global.scrollTo({ top: documentScroll });
        if (token) findByFocusToken(host, token)?.focus({ preventScroll: true });
    }

    function showList(): void {
        const listView = document.getElementById("character-list-view");
        const detailPage = document.getElementById("character-detail-page");
        if (listView) listView.hidden = false;
        if (detailPage) detailPage.hidden = true;
        activeCardId = "";
        dropDraft();
        render();
    }

    function showDetail(cardId: string): void {
        const previous = activeCardId;
        activeCardId = cardId;
        const card = cards.find((item) => item.id === cardId);
        if (card) {
            if (previous !== cardId || !draft || draft.cardId !== cardId) {
                startDraft(card);
                activeTab = storedTab(cardId);
            }
        } else {
            dropDraft();
        }
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

    function render(): void {
        // 进入房间前的预加载只更新内存，不抢占当前模块的页面。
        if (!active) return;
        renderList();
        renderDetailView();
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
        if (activeCardId && !cards.some((item) => item.id === activeCardId)) {
            activeCardId = "";
            dropDraft();
        }
        render();
    }

    async function reloadCharacterSheet(): Promise<void> {
        await loadCards();
    }

    /** 离开当前卡前处理未保存修改；返回 false 表示用户取消。 */
    async function guardDirty(): Promise<boolean> {
        if (!draftDirty) return true;
        const confirmed = await confirmAction("当前角色卡有未保存的修改，确定离开吗？", "未保存的修改");
        if (confirmed) draftDirty = false;
        return confirmed;
    }

    // ---------------------------------------------------------- 新建 / 编辑弹窗
    // 建卡向导是后续刀：这里保留原来的弹窗表单，只服务「新建」。

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

    function renderCreateForm(card: Dnd5eCard): string {
        const sheet = card.sheet;
        const abilities = sheet.abilities || {};
        const proficiencies = sheet.proficiencies || {};
        const slots = sheet.spellcasting?.slots || {};
        const equipmentText = (sheet.equipment || [])
            .map((item) => `${item.name || ""} x${asInt(item.quantity, 1)} w${Math.max(0, Number(item.weight) || 0)}`)
            .join("\n");
        const abilityInputs = ABILITIES.map((item) => inputRow(`${item.label} ${item.short}`, `ability.${item.key}`, abilities[item.key], "number", 'min="1" max="30"')).join("");
        const avatarSrc = TrpgAvatar.resolveAvatarSrc(card.name, String(card.avatar || ""));
        const basicField = (label: string, name: string, value: unknown, type: string = "text"): string => `
            <label class="character-field">
                <span>${escapeHtml(label)}</span>
                <input type="${type}" class="form-control" data-dnd5e-field="${name}" value="${escapeHtml(value ?? "")}">
            </label>`;
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
                    <div class="character-basic-info-layout">
                        <div class="character-avatar-column">
                            <button type="button" class="character-avatar-preview" data-dnd5e-avatar-preview aria-label="上传角色头像">
                                <img data-dnd5e-avatar-image alt="角色头像预览" src="${escapeHtml(avatarSrc)}">
                                <i class="fa fa-camera" aria-hidden="true"></i>
                            </button>
                            <input type="file" class="character-avatar-file" data-dnd5e-avatar-upload accept="image/*">
                            <input type="hidden" data-dnd5e-field="avatar" value="${escapeHtml(String(card.avatar || ""))}">
                        </div>
                        <div class="character-basic-fields">
                            <div class="character-basic-row">
                                ${basicField("姓名", "name", card.name)}
                                ${basicField("种族", "race", sheet.race)}
                            </div>
                            <div class="character-basic-row">
                                ${basicField("职业", "class", sheet.class)}
                                ${basicField("等级", "level", sheet.level, "number")}
                            </div>
                            <div class="character-basic-row">
                                ${basicField("子职", "subclass", sheet.subclass)}
                                ${basicField("背景", "background", sheet.background)}
                            </div>
                            <div class="character-basic-row">
                                ${basicField("阵营", "alignment", sheet.alignment)}
                                ${basicField("经验值", "xp", sheet.xp, "number")}
                            </div>
                            <label class="character-field">
                                <span>角色简介</span>
                                <textarea class="form-control" rows="3" data-dnd5e-field="bio">${escapeHtml(sheet.bio || "")}</textarea>
                            </label>
                        </div>
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
                        ${inputRow("生命骰（如 3d8）", "hitDice", sheet.hitDice)}
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
                        ${textRow("状态（逗号分隔）", "status", statusList(sheet.status).join(", "), 2)}
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
            saves: normalizeSaveList(splitList(readField(root, "prof.saves"))),
            skills: normalizeSkillList(splitList(readField(root, "prof.skills"))),
            expertise: normalizeSkillList(splitList(readField(root, "prof.expertise"))),
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
            ability: canonicalAbility(readField(root, "spell.ability")),
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
                    equipped: false,
                };
            });
        sheet.features = readField(root, "features").split(/\n+/).map((line) => line.trim()).filter(Boolean);
        sheet.status = splitList(readField(root, "status"));
        sheet.notes = readField(root, "notes").trim();
        sheet.bio = readField(root, "bio").trim();
        return sheet;
    }

    function newCard(): Dnd5eCard {
        return createCharacterCard({
            id: `dnd5e-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
            name: "新角色",
            playerId: currentPlayerId(),
            sheet: emptySheet(),
        });
    }

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
                        <button type="button" class="btn-close" data-dnd5e-modal-action="cancel" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body" data-dnd5e-modal-body></div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-dnd5e-modal-action="cancel">取消</button>
                        <button type="button" class="btn btn-primary" data-dnd5e-modal-action="save">保存</button>
                    </div>
                </div>
            </div>`;
        element.addEventListener("click", (event) => {
            const target = event.target as HTMLElement | null;
            if (target?.closest("[data-dnd5e-avatar-preview]")) {
                element.querySelector<HTMLInputElement>("[data-dnd5e-avatar-upload]")?.click();
                return;
            }
            const button = target?.closest<HTMLElement>("[data-dnd5e-modal-action]");
            const action = button?.dataset.dnd5eModalAction || "";
            if (action === "cancel") {
                closeEditorModal();
                return;
            }
            if (action === "save") {
                const form = element.querySelector<HTMLElement>("[data-dnd5e-editor]");
                if (form) void saveCreatedCard(form);
            }
        });
        element.addEventListener("change", (event) => {
            const input = event.target as HTMLInputElement | null;
            if (!input?.matches("[data-dnd5e-avatar-upload]")) return;
            const file = input.files?.[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => {
                const value = String(reader.result || "");
                const field = element.querySelector<HTMLInputElement>('[data-dnd5e-field="avatar"]');
                if (field) field.value = value;
                const nameInput = element.querySelector<HTMLInputElement>('[data-dnd5e-field="name"]');
                const image = element.querySelector<HTMLImageElement>("[data-dnd5e-avatar-image]");
                if (image) image.src = TrpgAvatar.resolveAvatarSrc(nameInput?.value || "?", value);
            };
            reader.readAsDataURL(file);
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
        if (body) body.innerHTML = renderCreateForm(card);
        instance.show();
    }

    /** 新建落库（建卡向导是后续刀，这里只是把弹窗表单提交成一张卡）。 */
    async function saveCreatedCard(root: HTMLElement): Promise<void> {
        const existing = cards.find((item) => item.id === activeCardId) || editorDraft;
        if (!existing) return;
        const name = readField(root, "name").trim() || "未命名冒险者";
        const sheet = buildSheetFromEditor(existing, root);
        const avatar = readField(root, "avatar").trim() || String(existing.avatar || "");
        const payload: Dnd5eCard = {
            ...existing,
            name,
            sheet,
            rulesetId: RULESET_ID,
            ...(avatar ? { avatar } : {}),
        };
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

    /**
     * 「新建角色卡」入口：交给 dnd5e 专属建卡向导（四步：概念/属性/技能装备/速览）。
     * 向导缺席（脚本没加载）或用户点「高级：直接填写表单」时，仍回到旧的通用表单，
     * 所以两条路都能建卡，不会因为向导出问题把入口堵死。
     */
    function createCharacterFromList(): void {
        if (!global.currentUser) {
            notify("请先登录后再创建角色卡", "warning");
            return;
        }
        const wizard = global.TrpgDnd5eCharacterWizard;
        if (!wizard) {
            notify("建卡向导未加载，已切回直接填写表单", "warning");
            openEditorModal(newCard(), true);
            return;
        }
        wizard.open({
            onAdvanced: () => openEditorModal(newCard(), true),
            onCreated: (raw) => {
                const saved = createCharacterCard(raw as Dnd5eCardInput);
                cards = cards.some((item) => item.id === saved.id)
                    ? cards.map((item) => item.id === saved.id ? saved : item)
                    : [saved, ...cards];
                activeCardId = saved.id;
                showDetail(saved.id);
                global.TrpgCharacterListBridge?.refresh?.();
            },
        });
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

    async function openCharacter(raw: Record<string, unknown>, mode?: "detail" | "edit"): Promise<void> {
        if (initialLoad) await initialLoad.catch(() => undefined);
        const card = createCharacterCard(raw as Dnd5eCardInput);
        cards = cards.some((item) => item.id === card.id)
            ? cards.map((item) => item.id === card.id ? card : item)
            : [card, ...cards];
        if (activeCardId && activeCardId !== card.id && !(await guardDirty())) return;
        // 「编辑」也直接进页签卡：所有字段都能在页签内改，不再弹表单。
        showDetail(card.id);
        void mode;
    }

    // ---------------------------------------------------------------- 交互

    function sheetRootOf(target: HTMLElement | null): HTMLElement | null {
        return target?.closest<HTMLElement>("[data-dnd5e-sheet]") ?? null;
    }

    /** 卡面是否挂在工作区里（而不是房间预览弹窗等外部容器）。 */
    function isInWorkspace(root: HTMLElement): boolean {
        const workspace = document.getElementById("characterWorkspace");
        return Boolean(workspace?.contains(root));
    }

    function isEditableSheet(root: HTMLElement): boolean {
        return root.dataset.editable === "true" && isInWorkspace(root);
    }

    function handleAvatarChange(input: HTMLInputElement): void {
        const file = input.files?.[0];
        const target = draft;
        if (!file || !target) return;
        const mode = pendingUpload;
        const reader = new FileReader();
        reader.onload = () => {
            const value = String(reader.result || "");
            if (mode === "token") {
                target.sheet.token = value;
                portraitMode = "token";
            } else {
                target.avatar = value;
                portraitMode = "avatar";
            }
            draftDirty = true;
            renderDetailView();
            notify("图片已写入卡面草稿，记得保存", "info");
        };
        reader.readAsDataURL(file);
        input.value = "";
    }

    function onDocumentClick(event: Event): void {
        const target = event.target as HTMLElement | null;
        if (!target) return;

        // 返回列表：先过未保存提示（页签卡是就地编辑，这一点必须显式确认）。
        if (target.closest("#backToCharacterList")) {
            if (!active) return;
            void (async () => {
                if (!(await guardDirty())) return;
                showList();
            })();
            return;
        }

        const root = sheetRootOf(target);
        if (!root) return;
        const editable = isEditableSheet(root);

        const tabButton = target.closest<HTMLElement>("[data-dnd5e-tab]");
        if (tabButton) {
            // 工作区的页签要记住（重渲染后保持同一页），预览弹窗的临时切换不写回模块态。
            activateTab(root, tabButton.dataset.dnd5eTab || "details", { commit: isInWorkspace(root) });
            return;
        }

        const actionButton = target.closest<HTMLElement>("[data-dnd5e-act]");
        const action = actionButton?.dataset.dnd5eAct || "";
        if (!action) return;
        event.preventDefault();

        if (action === "portrait-mode") {
            const mode = actionButton?.dataset.mode === "token" ? "token" : "avatar";
            if (mode === "token" && !String(draft?.sheet.token || "").trim()) {
                pendingUpload = "token";
                root.querySelector<HTMLInputElement>("[data-dnd5e-avatar-upload]")?.click();
                return;
            }
            portraitMode = mode;
            renderDetailView();
            return;
        }

        // 升级入口在头区，和「保存」不同：它读的是服务端的卡，所以必须先保存草稿。
        if (action === "level-up") {
            if (!editable) return;
            void openLevelUpWizard(root);
            return;
        }

        if (!editable || !draft) return;

        switch (action) {
            case "avatar-upload": {
                pendingUpload = portraitMode;
                root.querySelector<HTMLInputElement>("[data-dnd5e-avatar-upload]")?.click();
                return;
            }
            case "save":
                void saveDraft();
                return;
            case "reset":
                void discardDraft();
                return;
            case "inspiration":
                draft.sheet.inspiration = !draft.sheet.inspiration;
                draftDirty = true;
                renderDetailView();
                return;
            case "rest-short":
                void requestRest("short");
                return;
            case "rest-long":
                void requestRest("long");
                return;
            case "death-save": {
                const side = actionButton?.dataset.side === "failure" ? "failure" : "success";
                const index = clamp(actionButton?.dataset.index, 0, 3, 0);
                const saves = draft.sheet.deathSaves ?? {};
                const current = clamp(side === "success" ? saves.success : saves.failure, 0, 3, 0);
                const next = index <= current && index === current ? index - 1 : index;
                draft.sheet.deathSaves = { ...saves, [side]: Math.max(0, next) };
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "exhaustion": {
                const level = clamp(actionButton?.dataset.level, 0, 6, 0);
                draft.sheet.exhaustion = draft.sheet.exhaustion === level ? Math.max(0, level - 1) : level;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "add-item": {
                const equipment = draft.sheet.equipment ?? [];
                equipment.push({ name: "", quantity: 1, weight: 0, notes: "", equipped: false });
                draft.sheet.equipment = equipment;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "del-item": {
                const index = asInt(actionButton?.dataset.index, -1);
                const equipment = draft.sheet.equipment ?? [];
                if (index >= 0 && index < equipment.length) equipment.splice(index, 1);
                draft.sheet.equipment = equipment;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "add-feature": {
                const features = stringList(draft.sheet.features);
                features.push("");
                draft.sheet.features = features;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "del-feature": {
                const index = asInt(actionButton?.dataset.index, -1);
                const features = stringList(draft.sheet.features);
                if (index >= 0 && index < features.length) features.splice(index, 1);
                draft.sheet.features = features;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "add-spell": {
                const known = stringList(draft.sheet.spellcasting?.known);
                known.push("");
                draft.sheet.spellcasting = { ...(draft.sheet.spellcasting ?? {}), known };
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "del-spell": {
                const name = String(actionButton?.dataset.key || "");
                const casting = draft.sheet.spellcasting ?? {};
                draft.sheet.spellcasting = {
                    ...casting,
                    known: stringList(casting.known).filter((item) => item.toLowerCase() !== name.toLowerCase()),
                    prepared: stringList(casting.prepared).filter((item) => item.toLowerCase() !== name.toLowerCase()),
                };
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "add-slot": {
                const level = String(clamp(actionButton?.dataset.level, 1, 9, 1));
                const slots = { ...(draft.sheet.spellcasting?.slots ?? {}) };
                if (!slots[level]) slots[level] = { max: 0, used: 0 };
                draft.sheet.spellcasting = { ...(draft.sheet.spellcasting ?? {}), slots };
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "slot-step": {
                const level = String(actionButton?.dataset.level || "");
                const delta = asInt(actionButton?.dataset.delta, 0);
                const slots = draft.sheet.spellcasting?.slots;
                const row = level ? slots?.[level] : undefined;
                if (row && slots) {
                    const max = Math.max(0, asInt(row.max, 0));
                    slots[level] = { max, used: Math.max(0, Math.min(max, asInt(row.used, 0) + delta)) };
                    draftDirty = true;
                    renderDetailView();
                }
                return;
            }
            case "quick-condition": {
                const id = String(actionButton?.dataset.condition || "");
                if (!id) return;
                const status = statusList(draft.sheet.status);
                const index = status.findIndex((entry) => sameCondition(entry, id));
                if (index >= 0) status.splice(index, 1);
                else status.push(id);
                draft.sheet.status = status;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "add-condition": {
                const status = statusList(draft.sheet.status);
                status.push("");
                draft.sheet.status = status;
                draftDirty = true;
                renderDetailView();
                return;
            }
            case "del-condition": {
                const index = asInt(actionButton?.dataset.index, -1);
                const status = statusList(draft.sheet.status);
                if (index >= 0 && index < status.length) status.splice(index, 1);
                draft.sheet.status = status;
                draftDirty = true;
                renderDetailView();
                return;
            }
            default:
                return;
        }
    }

    function onDocumentInput(event: Event): void {
        const target = event.target as HTMLElement | null;
        const root = sheetRootOf(target);
        if (!root || !isEditableSheet(root) || !draft) return;
        const input = target as HTMLInputElement | null;
        const path = input?.dataset.dnd5eBind;
        if (!input || !path) return;
        const kind = (input.dataset.dnd5eBindKind as "text" | "number" | "list" | "bool" | undefined) ?? "text";
        setDraftValue(path, input.value, kind, input.checked);
        draftDirty = true;
        markDirty(root);
        syncLiveReadouts(root);
    }

    function onDocumentChange(event: Event): void {
        const target = event.target as HTMLElement | null;
        const root = sheetRootOf(target);
        if (!root) return;
        const input = target as HTMLInputElement | null;

        if (input?.matches("[data-dnd5e-avatar-upload]")) {
            handleAvatarChange(input);
            return;
        }
        if (!isEditableSheet(root) || !draft) return;

        // 属性 / 技能 / 法术位这类「结构性」勾选要重渲染，才能同步联动状态。
        const toggle = input?.dataset.dnd5eToggle;
        if (toggle && input) {
            const key = String(input.dataset.key || "");
            if (toggle === "save") {
                toggleListValue("saves", key, input.checked);
            } else if (toggle === "skill") {
                toggleListValue("skills", key, input.checked);
            } else if (toggle === "expertise") {
                toggleListValue("expertise", key, input.checked);
            } else if (toggle === "prepared") {
                const casting = draft.sheet.spellcasting ?? {};
                const prepared = stringList(casting.prepared).filter((item) => item.toLowerCase() !== key.toLowerCase());
                if (input.checked) prepared.push(key);
                draft.sheet.spellcasting = { ...casting, prepared };
            }
            draftDirty = true;
            renderDetailView();
            return;
        }

        const selectPath = (target as HTMLSelectElement | null)?.dataset?.dnd5eSelect;
        if (selectPath) {
            const value = (target as HTMLSelectElement).value;
            setDraftValue(selectPath, value, "text", false);
            draftDirty = true;
            renderDetailView();
            return;
        }

        // checkbox 类字段（是否装备 / 灵感等）走这里。
        const path = input?.dataset.dnd5eBind;
        if (input && path && input.dataset.dnd5eBindKind === "bool") {
            setDraftValue(path, input.value, "bool", input.checked);
            draftDirty = true;
            renderDetailView();
        }
    }

    function markDirty(root: HTMLElement): void {
        const sheet = root.closest<HTMLElement>("[data-dnd5e-sheet]") ?? root;
        const banner = sheet.querySelector<HTMLElement>("[data-dnd5e-dirty-banner]");
        if (banner) banner.hidden = false;
    }

    function onDocumentKeydown(event: KeyboardEvent): void {
        const target = event.target as HTMLElement | null;
        const root = sheetRootOf(target);
        if (!root) return;
        if (event.key === "Enter" && target?.matches("[data-dnd5e-bind]") && target instanceof HTMLInputElement) {
            event.preventDefault();
            if (isEditableSheet(root)) void saveDraft();
            return;
        }
        // 页签键盘可达：左右方向键切换，Home / End 跳首尾。
        if (target?.matches("[data-dnd5e-tab]")) {
            if (event.key === "ArrowRight") {
                event.preventDefault();
                moveTabFocus(root, 1);
            } else if (event.key === "ArrowLeft") {
                event.preventDefault();
                moveTabFocus(root, -1);
            } else if (event.key === "Home") {
                event.preventDefault();
                moveTabFocus(root, "home");
            } else if (event.key === "End") {
                event.preventDefault();
                moveTabFocus(root, "end");
            } else if (event.key === " " || event.key === "Enter") {
                event.preventDefault();
                const key = (target as HTMLElement).dataset.dnd5eTab || "details";
                activateTab(root, key, { commit: isInWorkspace(root), focus: true });
            }
        }
    }

    function bind(): void {
        if (bound) return;
        bound = true;
        // 监听挂在 document 上：外壳切换规则集时会克隆 #characterWorkspace 节点，
        // 节点上的直接监听会丢，document 级委托不会。
        document.addEventListener("click", onDocumentClick);
        document.addEventListener("input", onDocumentInput);
        document.addEventListener("change", onDocumentChange);
        document.addEventListener("keydown", onDocumentKeydown);
    }

    function clearCharacterManagement(): void {
        active = false;
        editorDraft = null;
        dropDraft();
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
        label: "D&D 5e",
        tag: "DND",
        initCharacterSheet,
        clearCharacterManagement,
        reloadCharacterManagement: reloadCharacterSheet,
        supportsRuleset,
        describeCharacterCard,
        openCharacter,
        createCharacter: createCharacterFromList,
    });
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
