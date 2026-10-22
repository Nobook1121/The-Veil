type COC7CoreAttributeKey = "STR" | "DEX" | "SIZ" | "APP" | "CON" | "INT" | "POW" | "EDU" | "LUC";
type COC7AttributeKey = COC7CoreAttributeKey | "AGE";
type SkillCategory = "特殊" | "探索" | "社交" | "战斗" | "医疗" | "运动" | "知识" | "技术" | "操纵" | "其他";
type InvestigatorGender = "male" | "female" | "unknown";
type NameRegion = "china" | "japan" | "korea" | "western" | "russia" | "india" | "france" | "germany" | "spain" | "italy";

interface COC7Attributes {
    STR: number;
    CON: number;
    SIZ: number;
    DEX: number;
    APP: number;
    INT: number;
    POW: number;
    EDU: number;
    LUC: number;
    AGE: number;
}

interface LegacyAttributesInput extends Partial<COC7Attributes> {
    LUK?: number;
}

interface COC7Skill {
    id: string;
    skillKey?: string;
    specialtyKey?: string;
    name: string;
    value: number;
    base: number;
    category: SkillCategory | string;
    checked: boolean;
    occupation?: boolean;
    isProfessional?: boolean;
    occupationPoints?: number;
    interestPoints?: number;
    growthPoints?: number;
    customBase?: boolean;
}

interface SkillSpecialtyCatalogEntry {
    key: string;
    labelKey: string;
    base?: number;
}

interface SkillCatalogEntry {
    key: string;
    labelKey: string;
    category: SkillCategory;
    base: number;
    repeatable: number;
    specialties: SkillSpecialtyCatalogEntry[];
    eraLimited?: boolean;
    userDefinedBase?: boolean;
}

interface OccupationPointFormulaTerm {
    attribute?: COC7AttributeKey;
    /** 「或」项：取候选属性中的最高值参与计算。 */
    choose?: COC7AttributeKey[];
    multiplier: number;
}

interface SkillSuccessLimits {
    occupation: number;
    other: number;
}

type SkillBaseSettingsMode = "admin" | "room" | "user";

type OccupationPointFormula = Array<COC7AttributeKey | OccupationPointFormulaTerm>;

type OccupationSkillEntry = {
    skillKey?: string;
    specialtyKey?: string;
    chooseOne?: OccupationSkillEntry[];
    freeChoice?: string;
};

interface COC7EquipmentItem {
    name: string;
    quantity: number;
    weight: number;
    volume?: number;
    notes?: string;
}

interface COC7Weapon {
    name: string;
    skill: string;
    skillKey?: string;
    specialtyKey?: string;
    damage: string;
    range: string;
    impale: boolean | null;
    attacks: string;
    ammo: string;
    malfunction: string;
    weight?: string;
    note?: string;
}

interface WeaponCatalogPayload {
    id: string;
    name: string;
    skill: {
        skillKey?: string;
        specialtyKey?: string;
        label?: string;
    };
    damage: string;
    attacks: string;
    impale: boolean;
    range: string;
    ammo: string;
    malfunction: string;
    eras: string[];
    price: string;
}

interface COC7Assets {
    cash: number;
    spendingLevel: number;
    assetsText: string;
}

interface COC7Relationship {
    name: string;
    description: string;
    player: string;
}

interface COC7ExperiencedScenario {
    name: string;
    experience: string;
    sanChange?: string;
    otherChanges?: string;
    san_change?: string;
    other_changes?: string;
}

interface COC7Background {
    appearance: string;
    ideology: string;
    significantPeople: string;
    meaningfulLocations: string;
    treasuredPossessions: string;
    traits: string;
    injuriesScars: string;
    phobiasManias: string;
    arcaneTomes: string;
    spells: string;
    encounters: string;
    story: string;
    education: string;
    raceType: string;
}

interface COC7Occupation {
    id: string;
    name: string;
    nameKey?: string;
    categoryKey?: string;
    category?: string;
    order?: number;
    creditRating: [number, number];
    /** 仅确定本职技能键（打勾判定用）；模糊条目见 occupationSkillEntries。 */
    occupationSkills: string[];
    pointsFormula: OccupationPointFormula;
    occupationSkillEntries?: OccupationSkillEntry[];
    skillBases: Record<string, number>;
}

interface OccupationCatalogPayload {
    id: string;
    nameKey?: string;
    name?: string;
    categoryKey?: string;
    category?: string;
    order?: number;
    creditRating?: { min?: number; max?: number };
    occupationSkillPoints?: {
        formula?: string;
        terms?: OccupationPointFormulaTerm[];
    };
    occupationSkills?: OccupationSkillEntry[];
    skillBases?: Record<string, number>;
}

interface SkillCatalogPayload {
    version?: number;
    defaultLocale?: string;
    skills?: SkillCatalogEntry[];
    locales?: Record<string, Record<string, string>>;
}

interface COC7HalfAndFifth {
    half: number;
    fifth: number;
}

interface AttributeDisplayValues {
    half: number;
    ratio: number;
}

interface AttributeRollFormula {
    dice: number;
    sides: number;
    bonus: number;
    multiplier: number;
}

type AttributeRollFormulaMap = Record<COC7CoreAttributeKey, string>;

interface CharacterRuleSettings {
    attributeRatioPercent: number;
    maxCardsPerUser: number;
    weaponSlotCount: number;
    allowSkillBaseEdit: boolean;
    attributeRolls: AttributeRollFormulaMap;
}

interface CharacterRuleSettingsInput {
    attributeRatioPercent?: unknown;
    maxCardsPerUser?: unknown;
    weaponSlotCount?: unknown;
    allowSkillBaseEdit?: unknown;
    attributeRolls?: Partial<Record<COC7CoreAttributeKey, string>>;
}

interface CharacterAssignableUser {
    id: string | number;
    username: string;
    role?: string;
    status?: string;
}

interface CharacterStatusFlags {
    majorWound: boolean;
    unconscious: boolean;
    dead: boolean;
    temporaryInsanity: boolean;
    permanentInsanity: boolean;
    indefiniteInsanity: boolean;
}

interface COC7SkillAllocationSummary {
    selectedOccupationSkills: number;
    requiredOccupationSkills: number;
    creditRatingValid: boolean;
    occupationPoints: number;
    personalInterestPoints: number;
}

interface COC7CharacterCard {
    id: string;
    public_id?: string;
    publisher_id?: string | number | null;
    publisher_name?: string;
    name: string;
    playerId: string;
    era: string;
    gender: string;
    age: number;
    avatar: string;
    occupationId: string;
    occupationName: string;
    creditRating: number;
    residence: string;
    birthplace: string;
    attributes: COC7Attributes;
    maxHp: number;
    currentHp: number;
    maxSan: number;
    initialSan: number;
    currentSan: number;
    magicPoints: number;
    currentMp: number;
    maxMp: number;
    status: CharacterStatusFlags;
    occupationSkillPoints: number;
    personalInterestPoints: number;
    skillSuccessLimits: SkillSuccessLimits;
    mov: number;
    build: number;
    damageBonus: string;
    armor: number;
    skills: COC7Skill[];
    weapons: COC7Weapon[];
    equipment: COC7EquipmentItem[];
    assets: COC7Assets;
    background: COC7Background;
    relationships: COC7Relationship[];
    experiencedScenarios: COC7ExperiencedScenario[];
    createdAt: string;
    updatedAt: string;
}

type COC7CharacterCardInput = Partial<Omit<COC7CharacterCard, "attributes" | "gender">> & {
    attributes?: LegacyAttributesInput;
    gender?: string;
    public_id?: string;
    publisher_id?: string | number | null;
    publisher_name?: string;
};

interface TestCharacterJson {
    [key: string]: unknown;
    id?: string;
    name?: string;
    playerId?: string;
    playerName?: string;
    time?: string;
    job?: string;
    age?: string | number;
    gender?: string;
    location?: string;
    hometown?: string;
    attributes?: Record<string, unknown>;
    deriveAttributes?: {
        sanity?: Record<string, unknown>;
        hp?: Record<string, unknown>;
        mp?: Record<string, unknown>;
    };
    battleAttributes?: Record<string, unknown>;
    characterStatus?: {
        bodyStates?: Record<string, unknown>;
        mentalStates?: Record<string, unknown>;
    };
    pointValues?: unknown;
    proSkills?: unknown;
    skillPoints?: unknown;
    weapons?: Array<Record<string, unknown>>;
    stories?: Record<string, unknown>;
    assets?: Record<string, unknown>;
    experiencedModules?: string | Array<Record<string, unknown>>;
    friends?: string | Array<Record<string, unknown>>;
    skillGroups?: Record<string, Array<Record<string, unknown>>>;
    isEditable?: boolean;
    createdAt?: string;
    updatedAt?: string;
}

interface AttributeCheckResult {
    roll: number;
    target: number;
    success: boolean;
    level: string;
}

interface CharacterApi {
    ATTRIBUTE_KEYS: COC7CoreAttributeKey[];
    BASE_SKILLS: COC7Skill[];
    PRESET_OCCUPATIONS: COC7Occupation[];
    calculateHalfAndFifth: (value: number) => COC7HalfAndFifth;
    calculateAttributeDisplayValues: (value: number, ratioPercent?: number) => AttributeDisplayValues;
    calculateAttributeBaseTotal: (attributes: COC7Attributes) => number;
    calculateMaxHp: (attributes: COC7Attributes) => number;
    calculateMaxSan: (attributes: COC7Attributes) => number;
    calculateMaxMp: (attributes: COC7Attributes) => number;
    calculateMov: (attributes: COC7Attributes) => number;
    calculateOccupationSkillPoints: (attributes: COC7Attributes, occupationId: string) => number;
    calculatePersonalInterestPoints: (attributes: COC7Attributes) => number;
    calculateBuildAndDamageBonus: (attributes: Pick<COC7Attributes, "STR" | "SIZ">) => { build: number; damageBonus: string };
    calculateEquipmentLoad: (equipment: COC7EquipmentItem[]) => { totalWeight: number; totalVolume: number };
    groupSkillsByCategory: (skills: COC7Skill[]) => Record<string, number>;
    countSelectedOccupationSkills: (skills: COC7Skill[]) => number;
    validateOccupationSkillSelection: (card: COC7CharacterCard) => COC7SkillAllocationSummary;
    autoAllocateOccupationSkills: (card: COC7CharacterCard) => COC7CharacterCard;
    applySkillSpecialty: (rowId: string, specialtyKey: string) => void;
    openSkillBaseSettings: (mode: SkillBaseSettingsMode) => void;
    reloadSkillBases: () => Promise<void>;
    collectCardSkillBaseOverflows: (card: COC7CharacterCard, roomBases?: Record<string, number>) => string[];
    clampCardSkillBases: (card: COC7CharacterCard, roomBases?: Record<string, number>) => COC7CharacterCard;
    rollAttributeCheck: (attributes: COC7Attributes, attributeKey: COC7AttributeKey, roller?: () => number) => AttributeCheckResult;
    generateInvestigatorName: (gender?: InvestigatorGender, random?: () => number) => string;
    generateRegionalName: (region?: NameRegion, gender?: InvestigatorGender, random?: () => number) => string;
    parseAttributeRollFormula: (formulaText: string) => AttributeRollFormula | null;
    rollAttributeFormula: (formula: AttributeRollFormula, random?: () => number) => number;
    randomizeAttributes: (random?: () => number, settings?: CharacterRuleSettings) => COC7Attributes;
    createCharacterCard: (input?: COC7CharacterCardInput) => COC7CharacterCard;
    listCharacterCards: () => COC7CharacterCard[];
    getCharacterCardSnapshot: (cardId: string) => Partial<COC7CharacterCard> | null;
    renderCharacterDetail: (card: COC7CharacterCard) => string;
    clearCharacterManagement: () => void;
    reloadCharacterManagement: () => Promise<void>;
    initCharacterSheet: () => void;
}

interface Window {
    COC7CharacterSheet?: CharacterApi;
}

(function initializeCharacterSheet(global: Window & typeof globalThis): void {
    "use strict";

    const STORAGE_KEY = "ai-trpg:coc7-character-cards";
    const ACTIVE_STORAGE_KEY = "ai-trpg:coc7-active-character";
    const RULE_SETTINGS_STORAGE_KEY = "ai-trpg:coc7-character-rule-settings";
    const USER_SKILL_BASES_STORAGE_KEY = "ai-trpg:coc7-skill-base-settings";
    const ATTRIBUTE_KEYS: COC7CoreAttributeKey[] = ["STR", "DEX", "SIZ", "APP", "CON", "INT", "POW", "EDU", "LUC"];
    const PLAYER_UNBOUND_LABEL = "未绑定玩家";
    const ATTRIBUTE_LABELS: Record<COC7CoreAttributeKey, string> = {
        STR: "力量",
        DEX: "敏捷",
        SIZ: "体型",
        APP: "外貌",
        CON: "体质",
        INT: "智力",
        POW: "意志",
        EDU: "教育",
        LUC: "幸运"
    };
    const DEFAULT_ATTRIBUTE_ROLLS: AttributeRollFormulaMap = {
        STR: "3d6x5",
        DEX: "3d6x5",
        SIZ: "(2d6+6)x5",
        APP: "3d6x5",
        CON: "3d6x5",
        INT: "(2d6+6)x5",
        POW: "3d6x5",
        EDU: "(2d6+6)x5",
        LUC: "3d6x5"
    };
    const DEFAULT_RULE_SETTINGS: CharacterRuleSettings = {
        attributeRatioPercent: 20,
        maxCardsPerUser: 5,
        weaponSlotCount: 5,
        allowSkillBaseEdit: false,
        attributeRolls: DEFAULT_ATTRIBUTE_ROLLS
    };
    const STATUS_FIELD_IDS: Record<keyof CharacterStatusFlags, string> = {
        majorWound: "characterStatusMajorWound",
        unconscious: "characterStatusUnconscious",
        dead: "characterStatusDead",
        temporaryInsanity: "characterStatusTemporaryInsanity",
        permanentInsanity: "characterStatusPermanentInsanity",
        indefiniteInsanity: "characterStatusIndefiniteInsanity"
    };
    const TEST_CHARACTER_SKILL_GROUPS: Record<string, SkillCategory> = {
        special: "特殊",
        explore: "探索",
        social: "社交",
        combat: "战斗",
        medical: "医疗",
        move: "运动",
        knowledge: "知识",
        tech: "技术",
        drive: "操纵",
        other: "其他"
    };
    const TEST_CHARACTER_CATEGORY_GROUPS = Object.entries(TEST_CHARACTER_SKILL_GROUPS).reduce((index, [group, category]) => {
        index[category] = group;
        return index;
    }, {} as Record<string, string>);
    const TEST_CHARACTER_BODY_STATUS: Record<keyof Pick<CharacterStatusFlags, "majorWound" | "unconscious" | "dead">, string> = {
        majorWound: "重伤",
        unconscious: "昏迷",
        dead: "死亡"
    };
    const TEST_CHARACTER_MENTAL_STATUS: Record<keyof Pick<CharacterStatusFlags, "indefiniteInsanity" | "permanentInsanity" | "temporaryInsanity">, string> = {
        indefiniteInsanity: "不定期疯狂",
        permanentInsanity: "永久疯狂",
        temporaryInsanity: "临时疯狂"
    };
    const SKILL_FILTER_CATEGORIES: Array<SkillCategory | "全部技能"> = ["全部技能", "特殊", "探索", "社交", "战斗", "医疗", "运动", "知识", "技术", "操纵", "其他"];
    const REGIONAL_NAMES: Record<NameRegion, { family: string[]; male: string[]; female: string[]; neutral: string[]; westernOrder?: boolean }> = {
        china: {
            family: ["林", "陈", "顾", "沈", "周", "陆", "许", "梁", "赵", "钱", "孙", "李", "王", "吴", "郑", "冯", "蒋", "韩", "杨", "朱", "秦", "何", "吕", "罗", "宋", "谢", "唐", "杜", "程", "苏", "魏", "叶"],
            male: ["雨衡", "明远", "怀瑾", "景行", "子昂", "修文", "亦舟", "远航", "启明", "望舒", "云起", "砚清", "书珩", "临川", "君泽", "予安", "星野", "知白", "立言", "鹤鸣", "清越", "元恺", "文昊", "慕辰", "南烛", "纪行", "守拙", "斯年"],
            female: ["若宁", "清荷", "知遥", "南枝", "书瑶", "映雪", "芷晴", "以沫", "予棠", "云舒", "念真", "采薇", "夕颜", "月白", "景澜", "安歌", "诗涵", "沐晴", "婉仪", "明玥", "洛笙", "令仪", "素问", "清欢", "青黛", "语桐", "初夏", "晚照"],
            neutral: ["安和", "知远", "星河", "沐川", "云深", "青岚", "长风", "一白", "宁川", "砚秋", "归鸿", "明岑", "溪亭", "望川", "逐光", "南星"]
        },
        japan: { family: ["藤原", "佐藤", "高桥", "田中", "渡边", "伊藤"], male: ["悠真", "莲", "翔太", "拓海"], female: ["香里", "美咲", "结衣", "葵"], neutral: ["遥", "光", "律"] },
        korea: { family: ["金", "李", "朴", "崔", "郑", "韩"], male: ["俊浩", "民载", "道允", "志勋"], female: ["素贤", "智雅", "恩彩", "瑞妍"], neutral: ["贤宇", "智安", "夏仁"] },
        western: { family: ["Carter", "Miller", "Bennett", "Morgan", "Reed", "Howard"], male: ["Arthur", "Edward", "Henry", "Victor"], female: ["Eleanor", "Clara", "Grace", "Helen"], neutral: ["Alex", "Robin", "Taylor"], westernOrder: true },
        russia: { family: ["Ivanov", "Petrov", "Sokolov", "Volkov", "Morozov"], male: ["Dmitri", "Nikolai", "Alexei", "Viktor"], female: ["Anastasia", "Irina", "Svetlana", "Katerina"], neutral: ["Sasha", "Valya", "Zhenya"], westernOrder: true },
        india: { family: ["Sharma", "Patel", "Iyer", "Nair", "Kapoor"], male: ["Arjun", "Rahul", "Vikram", "Dev"], female: ["Anika", "Priya", "Meera", "Kavya"], neutral: ["Kiran", "Adi", "Arya"], westernOrder: true },
        france: { family: ["Dubois", "Moreau", "Lefevre", "Laurent", "Bernard"], male: ["Louis", "Henri", "Luc", "Etienne"], female: ["Claire", "Camille", "Elise", "Juliette"], neutral: ["Claude", "Dominique", "Noel"], westernOrder: true },
        germany: { family: ["Muller", "Schmidt", "Weber", "Fischer", "Wagner"], male: ["Karl", "Otto", "Lukas", "Felix"], female: ["Anna", "Greta", "Lena", "Marta"], neutral: ["Alex", "Toni", "Mika"], westernOrder: true },
        spain: { family: ["Garcia", "Lopez", "Martinez", "Sanchez", "Romero"], male: ["Diego", "Mateo", "Javier", "Rafael"], female: ["Lucia", "Sofia", "Isabel", "Carmen"], neutral: ["Cruz", "Angel", "Sol"], westernOrder: true },
        italy: { family: ["Rossi", "Bianchi", "Romano", "Ricci", "Marino"], male: ["Marco", "Luca", "Giovanni", "Matteo"], female: ["Giulia", "Sofia", "Elena", "Bianca"], neutral: ["Andrea", "Noa", "Vale"], westernOrder: true }
    };

    // 职业数据全部来自 /api/character-catalogs/occupations（data/occupations/builtin/*.json）。
    let PRESET_OCCUPATIONS: COC7Occupation[] = [];

    /** 职业类别展示顺序（与 data/occupations 的 11 个 slug 对应）。 */
    const OCCUPATION_CATEGORY_ORDER = [
        "literary", "industry", "whiteCollar", "academic", "medical",
        "sports", "service", "religion", "gray", "criminal", "authority"
    ];

    /** 目录未加载时的空职业兜底，避免取值崩溃。 */
    const EMPTY_OCCUPATION: COC7Occupation = {
        id: "",
        name: "",
        nameKey: "",
        categoryKey: "",
        category: "",
        order: 0,
        creditRating: [0, 99],
        occupationSkills: [],
        pointsFormula: [],
        occupationSkillEntries: [],
        skillBases: {}
    };

    let SKILL_CATALOG: SkillCatalogEntry[] = [];
    let SKILL_LOCALE_MAP: Record<string, string> = {};
    let SKILL_KEY_BY_LABEL: Record<string, string> = {};
    let BASE_SKILLS: COC7Skill[] = [
        { id: "artCraft", skillKey: "artCraft", name: "技艺", base: 5, value: 5, category: "技术", checked: false },
        { id: "history", skillKey: "history", name: "历史", base: 5, value: 5, category: "知识", checked: false },
        { id: "libraryUse", skillKey: "libraryUse", name: "图书馆使用", base: 20, value: 20, category: "探索", checked: false },
        { id: "naturalWorld", skillKey: "naturalWorld", name: "博物学", base: 10, value: 10, category: "知识", checked: false },
        { id: "occult", skillKey: "occult", name: "神秘学", base: 5, value: 5, category: "知识", checked: false },
        { id: "languageOwn", skillKey: "languageOwn", name: "母语", base: 0, value: 0, category: "社交", checked: false },
        { id: "languageOther", skillKey: "languageOther", name: "外语", base: 1, value: 1, category: "社交", checked: false },
        { id: "psychology", skillKey: "psychology", name: "心理学", base: 10, value: 10, category: "社交", checked: false },
        { id: "creditRating", skillKey: "creditRating", name: "信用评级", base: 0, value: 0, category: "特殊", checked: false },
        { id: "cthulhuMythos", skillKey: "cthulhuMythos", name: "克苏鲁神话", base: 0, value: 0, category: "特殊", checked: false }
    ];

    let cards: COC7CharacterCard[] = [];
    let galleryCards: COC7CharacterCard[] = [];
    let activeCardId = "";
    let activeGallerySearchTerm = "";
    let activeGalleryEditId = "";
    let assignableUsers: CharacterAssignableUser[] = [];
    let modal: BootstrapModalInstance | null = null;
    let nameGeneratorModal: BootstrapModalInstance | null = null;
    let occupationTemplateModal: BootstrapModalInstance | null = null;
    let weaponPickerModal: BootstrapModalInstance | null = null;
    let skillSpecialtyModal: BootstrapModalInstance | null = null;
    let skillBaseSettingsModal: BootstrapModalInstance | null = null;
    let pendingSkillSpecialtyRowId = "";
    let pendingSkillSpecialtyKey = "";
    let skillBaseSettingsMode: SkillBaseSettingsMode = "user";
    let pendingGeneratedName = "";
    let pendingWeaponPickerTarget = "";
    let activeSkillCategoryFilter = "全部技能";
    let editorSkills: COC7Skill[] = [];
    let WEAPON_CATALOG: WeaponCatalogPayload[] = [];
    let occupationSkillPointsManuallyEdited = false;
    let personalInterestPointsManuallyEdited = false;
    let combatStatsManuallyEdited = false;
    let characterSaveInFlight = false;

    /** 批量管理：个人角色卡列表与角色卡广场各自独立维护模式与选中集合。 */
    type CharacterBatchTarget = "card" | "gallery";
    interface CharacterBatchState {
        mode: boolean;
        selected: Set<string>;
    }
    const characterBatchStates: Record<CharacterBatchTarget, CharacterBatchState> = {
        card: { mode: false, selected: new Set<string>() },
        gallery: { mode: false, selected: new Set<string>() }
    };
    const CHARACTER_BATCH_DOM: Record<CharacterBatchTarget, {
        toolbar: string; count: string; selectAll: string; deleteBtn: string; exitBtn: string; toggle: string; listId: string;
    }> = {
        card: { toolbar: "characterBatchToolbar", count: "characterBatchCount", selectAll: "characterBatchSelectAll", deleteBtn: "characterBatchDelete", exitBtn: "characterBatchExit", toggle: "characterBatchToggle", listId: "characterList" },
        gallery: { toolbar: "characterGalleryBatchToolbar", count: "characterGalleryBatchCount", selectAll: "characterGalleryBatchSelectAll", deleteBtn: "characterGalleryBatchDelete", exitBtn: "characterGalleryBatchExit", toggle: "characterGalleryBatchToggle", listId: "characterGalleryList" }
    };

    function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return fallback;
        return Math.min(max, Math.max(min, Math.round(parsed)));
    }

    function rollDie(sides: number, random: () => number): number {
        return Math.floor(random() * sides) + 1;
    }

    function parseAttributeRollFormula(formulaText: string): AttributeRollFormula | null {
        const normalized = formulaText.trim().toLowerCase().replace(/\s+/g, "");
        const match = normalized.match(/^\(?(?<dice>\d+)d(?<sides>\d+)(?<bonus>[+-]\d+)?\)?(?:x(?<multiplier>\d+))?$/);
        if (!match?.groups) return null;
        const dice = clampNumber(match.groups.dice, 1, 20, 3);
        const sides = clampNumber(match.groups.sides, 2, 100, 6);
        const bonus = clampNumber(match.groups.bonus || 0, -100, 100, 0);
        const multiplier = clampNumber(match.groups.multiplier || 1, 1, 100, 5);
        return { dice, sides, bonus, multiplier };
    }

    function formatAttributeRollFormula(formula: AttributeRollFormula): string {
        const bonusText = formula.bonus > 0 ? `+${formula.bonus}` : formula.bonus < 0 ? String(formula.bonus) : "";
        const base = `${formula.dice}d${formula.sides}${bonusText}`;
        const wrapped = formula.bonus === 0 ? base : `(${base})`;
        return `${wrapped}x${formula.multiplier}`;
    }

    function rollAttributeFormula(formula: AttributeRollFormula, random: () => number = Math.random): number {
        let total = formula.bonus;
        for (let index = 0; index < formula.dice; index += 1) {
            total += rollDie(formula.sides, random);
        }
        return clampNumber(total * formula.multiplier, 1, 999, 50);
    }

    function normalizeRuleSettings(input?: CharacterRuleSettingsInput): CharacterRuleSettings {
        const rawRolls = input?.attributeRolls || DEFAULT_ATTRIBUTE_ROLLS;
        const attributeRolls = ATTRIBUTE_KEYS.reduce((settings, key) => {
            const parsed = parseAttributeRollFormula(rawRolls[key] || DEFAULT_ATTRIBUTE_ROLLS[key]);
            settings[key] = parsed ? formatAttributeRollFormula(parsed) : DEFAULT_ATTRIBUTE_ROLLS[key];
            return settings;
        }, {} as AttributeRollFormulaMap);
        return {
            attributeRatioPercent: clampNumber(input?.attributeRatioPercent, 1, 100, DEFAULT_RULE_SETTINGS.attributeRatioPercent),
            maxCardsPerUser: clampNumber(input?.maxCardsPerUser, 1, 999, DEFAULT_RULE_SETTINGS.maxCardsPerUser),
            weaponSlotCount: clampNumber(input?.weaponSlotCount, 1, 20, DEFAULT_RULE_SETTINGS.weaponSlotCount),
            allowSkillBaseEdit: Boolean(input?.allowSkillBaseEdit ?? DEFAULT_RULE_SETTINGS.allowSkillBaseEdit),
            attributeRolls
        };
    }

    function getConfigSection(name: string): Record<string, unknown> | null {
        const section = global.configManager?.getSection("general", name);
        return section && typeof section === "object" && !Array.isArray(section) ? section as Record<string, unknown> : null;
    }

    function loadRuleSettings(): CharacterRuleSettings {
        const configSection = getConfigSection("character_rules");
        const configRolls = ATTRIBUTE_KEYS.reduce((rolls, key) => {
            const value = configSection?.[`attribute_roll_${key.toLowerCase()}`];
            if (typeof value === "string") rolls[key] = value;
            return rolls;
        }, { ...DEFAULT_ATTRIBUTE_ROLLS } as AttributeRollFormulaMap);
        const storage = safeStorage();
        const localSettings = storage ? parseRuleSettingsFromStorage(storage.getItem(RULE_SETTINGS_STORAGE_KEY)) : null;
        return normalizeRuleSettings({
            attributeRatioPercent: localSettings?.attributeRatioPercent ?? configSection?.attribute_ratio_percent ?? DEFAULT_RULE_SETTINGS.attributeRatioPercent,
            maxCardsPerUser: configSection?.max_cards_per_user ?? localSettings?.maxCardsPerUser ?? DEFAULT_RULE_SETTINGS.maxCardsPerUser,
            weaponSlotCount: configSection?.weapon_slot_count ?? localSettings?.weaponSlotCount ?? DEFAULT_RULE_SETTINGS.weaponSlotCount,
            allowSkillBaseEdit: configSection?.allow_skill_base_edit ?? localSettings?.allowSkillBaseEdit ?? DEFAULT_RULE_SETTINGS.allowSkillBaseEdit,
            attributeRolls: localSettings?.attributeRolls || configRolls
        });
    }

    function parseRuleSettingsFromStorage(raw: string | null): CharacterRuleSettings | null {
        if (!raw) return null;
        try {
            const parsed = JSON.parse(raw) as CharacterRuleSettingsInput;
            return normalizeRuleSettings(parsed);
        } catch {
            return null;
        }
    }

    function persistRuleSettings(settings: CharacterRuleSettings): void {
        const storage = safeStorage();
        if (storage) storage.setItem(RULE_SETTINGS_STORAGE_KEY, JSON.stringify(normalizeRuleSettings(settings)));
    }

    function randomizeAttributes(random: () => number = Math.random, settings: CharacterRuleSettings = loadRuleSettings()): COC7Attributes {
        const normalizedSettings = normalizeRuleSettings(settings);
        const roll = (key: COC7CoreAttributeKey): number => {
            const formula = parseAttributeRollFormula(normalizedSettings.attributeRolls[key]) || parseAttributeRollFormula(DEFAULT_ATTRIBUTE_ROLLS[key]);
            return formula ? rollAttributeFormula(formula, random) : 50;
        };
        return {
            STR: roll("STR"),
            DEX: roll("DEX"),
            SIZ: roll("SIZ"),
            APP: roll("APP"),
            CON: roll("CON"),
            INT: roll("INT"),
            POW: roll("POW"),
            EDU: roll("EDU"),
            LUC: roll("LUC"),
            AGE: 25
        };
    }

    function calculateMaxHp(attributes: COC7Attributes): number {
        return Math.floor((attributes.CON + attributes.SIZ) / 10);
    }

    function calculateMaxSan(attributes: COC7Attributes): number {
        return attributes.POW;
    }

    function calculateMaxMp(attributes: COC7Attributes): number {
        return Math.floor(attributes.POW / 5);
    }

    function calculateHalfAndFifth(value: number): COC7HalfAndFifth {
        const normalized = clampNumber(value, 0, 999, 0);
        return {
            half: Math.floor(normalized / 2),
            fifth: Math.floor(normalized / 5)
        };
    }

    function calculateAttributeDisplayValues(value: number, ratioPercent: number = loadRuleSettings().attributeRatioPercent): AttributeDisplayValues {
        const normalized = clampNumber(value, 0, 999, 0);
        return {
            half: Math.floor(normalized / 2),
            ratio: Math.floor(normalized * clampNumber(ratioPercent, 1, 100, 20) / 100)
        };
    }

    function calculateAttributeBaseTotal(attributes: COC7Attributes): number {
        return ATTRIBUTE_KEYS.reduce((total, key) => total + clampNumber(attributes[key], 0, 99, 0), 0);
    }

    function calculateOccupationSkillPoints(attributes: COC7Attributes, occupationId: string): number {
        const occupation = getOccupationById(occupationId);
        return occupation.pointsFormula.reduce((total, term) => {
            if (typeof term === "string") return total + attributes[term];
            // 「或」项取候选属性中的最高值（房规约定）。
            if (term.choose?.length) {
                const best = term.choose.reduce((max, key) => Math.max(max, attributes[key] ?? 0), 0);
                return total + best * term.multiplier;
            }
            if (!term.attribute) return total;
            return total + attributes[term.attribute] * term.multiplier;
        }, 0);
    }

    function calculatePersonalInterestPoints(attributes: COC7Attributes): number {
        return attributes.INT * 2;
    }

    function calculateMov(attributes: COC7Attributes): number {
        let mov = 8;
        if (attributes.STR < attributes.SIZ && attributes.DEX < attributes.SIZ) mov = 7;
        if (attributes.STR > attributes.SIZ && attributes.DEX > attributes.SIZ) mov = 9;
        if (attributes.AGE >= 40) mov -= Math.floor((Math.min(attributes.AGE, 89) - 30) / 10);
        if (attributes.AGE >= 90) mov -= 6;
        return Math.max(1, mov);
    }

    function calculateBuildAndDamageBonus(attributes: Pick<COC7Attributes, "STR" | "SIZ">): { build: number; damageBonus: string } {
        const total = attributes.STR + attributes.SIZ;
        if (total <= 64) return { build: -2, damageBonus: "-2" };
        if (total <= 84) return { build: -1, damageBonus: "-1" };
        if (total <= 124) return { build: 0, damageBonus: "0" };
        if (total <= 164) return { build: 1, damageBonus: "+1D4" };
        if (total <= 204) return { build: 2, damageBonus: "+1D6" };
        const extra = Math.floor((total - 205) / 80);
        return { build: 3 + extra, damageBonus: `+${2 + extra}D6` };
    }

    function calculateEquipmentLoad(equipment: COC7EquipmentItem[]): { totalWeight: number; totalVolume: number } {
        return equipment.reduce((summary, item) => ({
            totalWeight: roundMetric(summary.totalWeight + item.weight * item.quantity),
            totalVolume: roundMetric(summary.totalVolume + (item.volume || 0) * item.quantity)
        }), { totalWeight: 0, totalVolume: 0 });
    }

    function roundMetric(value: number): number {
        return Math.round(value * 100) / 100;
    }

    function groupSkillsByCategory(skills: COC7Skill[]): Record<string, number> {
        return skills.reduce((summary, skill) => {
            summary[skill.category] = (summary[skill.category] || 0) + 1;
            return summary;
        }, {} as Record<string, number>);
    }

    function countSelectedOccupationSkills(skills: COC7Skill[]): number {
        return skills.filter((skill) => skill.checked || skill.occupation).length;
    }

    function getOccupationById(occupationId: string): COC7Occupation {
        return PRESET_OCCUPATIONS.find((occupation) => occupation.id === occupationId) || PRESET_OCCUPATIONS[0] || EMPTY_OCCUPATION;
    }

    function resolveOccupationFromInput(value: string): COC7Occupation {
        const normalized = value.trim();
        if (!normalized) return EMPTY_OCCUPATION;
        return PRESET_OCCUPATIONS.find((occupation) => (
            occupation.id === normalized
            || occupation.name === normalized
            || occupation.nameKey === normalized
            || occupationDisplayName(occupation) === normalized
        )) || EMPTY_OCCUPATION;
    }

    function resolveOccupationIdFromInput(value: string): string {
        return resolveOccupationFromInput(value).id;
    }

    function resolveOccupationNameFromInput(value: string): string {
        return value.trim() || resolveOccupationFromInput(value).name;
    }

    function getOccupation(card: COC7CharacterCard): COC7Occupation {
        return getOccupationById(card.occupationId);
    }

    function validateOccupationSkillSelection(card: COC7CharacterCard): COC7SkillAllocationSummary {
        const occupation = getOccupation(card);
        return {
            selectedOccupationSkills: countSelectedOccupationSkills(card.skills),
            requiredOccupationSkills: occupation.occupationSkills.length,
            creditRatingValid: card.creditRating >= occupation.creditRating[0] && card.creditRating <= occupation.creditRating[1],
            occupationPoints: card.occupationSkillPoints,
            personalInterestPoints: card.personalInterestPoints
        };
    }

    function autoAllocateOccupationSkills(card: COC7CharacterCard): COC7CharacterCard {
        const occupation = getOccupation(card);
        const skills = card.skills.map((skill) => {
            const skillKey = resolveSkillKey(skill);
            const occupationSkill = occupation.occupationSkills.includes(skillKey);
            // 职业可指定技能专精：写入专精并按专精重算基础值（保持与专精弹窗一致的低耦合逻辑）。
            const occupationSpecialty = getOccupationSpecialtyKey(occupation, skillKey);
            const specialtyKey = occupationSpecialty || String(skill.specialtyKey || "").trim();
            const base = occupationSpecialty ? resolveEffectiveBase(skillKey, occupationSpecialty, card.attributes, occupation) : skill.base;
            const value = clampNumber(skill.value + (base - skill.base), 0, 99, skill.value);
            const result: COC7Skill = {
                ...skill,
                checked: skill.checked || occupationSkill,
                occupation: skill.occupation || occupationSkill,
                base,
                value
            };
            if (specialtyKey) result.specialtyKey = specialtyKey;
            else delete result.specialtyKey;
            return result;
        });
        return createCharacterCard({ ...card, skills });
    }

    function rollAttributeCheck(attributes: COC7Attributes, attributeKey: COC7AttributeKey, roller: () => number = () => Math.floor(Math.random() * 100) + 1): AttributeCheckResult {
        const target = attributes[attributeKey];
        const roll = clampNumber(roller(), 1, 100, 100);
        let level = "失败";
        if (roll === 1) level = "大成功";
        else if (roll <= Math.floor(target / 5)) level = "极难成功";
        else if (roll <= Math.floor(target / 2)) level = "困难成功";
        else if (roll <= target) level = "普通成功";
        else if (roll >= 96) level = "大失败";
        return { roll, target, success: roll <= target || roll === 1, level };
    }

    function generateInvestigatorName(gender: InvestigatorGender = "male", random: () => number = Math.random): string {
        return generateRegionalName("china", gender, random);
    }

    function generateRegionalName(region: NameRegion = "china", gender: InvestigatorGender = "unknown", random: () => number = Math.random): string {
        const source = REGIONAL_NAMES[region] || REGIONAL_NAMES.china;
        const givenPool = gender === "male" ? source.male : gender === "female" ? source.female : [...source.male, ...source.female, ...source.neutral];
        const family = pickRandom(source.family, random);
        const given = pickRandom(givenPool.length ? givenPool : source.neutral, random);
        return source.westernOrder ? `${given} ${family}` : `${family}${given}`;
    }

    function pickRandom(pool: string[], random: () => number): string {
        return pool[Math.floor(random() * pool.length)] || pool[0] || "";
    }

    function normalizeAttributes(input?: LegacyAttributesInput): COC7Attributes {
        return {
            STR: clampNumber(input?.STR, 1, 99, 50),
            DEX: clampNumber(input?.DEX, 1, 99, 50),
            SIZ: clampNumber(input?.SIZ, 1, 99, 50),
            APP: clampNumber(input?.APP, 1, 99, 50),
            CON: clampNumber(input?.CON, 1, 99, 50),
            INT: clampNumber(input?.INT, 1, 99, 50),
            POW: clampNumber(input?.POW, 1, 99, 50),
            EDU: clampNumber(input?.EDU, 1, 99, 50),
            LUC: clampNumber(input?.LUC ?? input?.LUK, 1, 99, 50),
            AGE: clampNumber(input?.AGE, 15, 99, 25)
        };
    }

    function normalizeNameGender(value: string | undefined): InvestigatorGender {
        return value === "male" || value === "female" || value === "unknown" ? value : "unknown";
    }

    function createCharacterCard(input: COC7CharacterCardInput = {}): COC7CharacterCard {
        const attributes = normalizeAttributes(input.attributes);
        const maxHp = calculateMaxHp(attributes);
        const maxSan = calculateMaxSan(attributes);
        const maxMp = calculateMaxMp(attributes);
        const damage = calculateBuildAndDamageBonus(attributes);
        const occupationId = input.occupationId || resolveOccupationIdFromInput(input.occupationName || "");
        const occupation = getOccupationById(occupationId);
        const now = new Date().toISOString();
        return {
            id: input.id || `investigator-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            public_id: input.public_id || "",
            publisher_id: input.publisher_id ?? null,
            publisher_name: input.publisher_name || "",
            name: input.name || generateInvestigatorName(normalizeNameGender(input.gender)),
            playerId: input.playerId || "",
            era: input.era || "1920s",
            gender: input.gender || "",
            age: attributes.AGE,
            avatar: input.avatar || "",
            occupationId,
            occupationName: input.occupationName || occupation.name,
            creditRating: clampNumber(input.creditRating, 0, 99, 0),
            residence: input.residence || "",
            birthplace: input.birthplace || "",
            attributes,
            maxHp,
            currentHp: clampNumber(input.currentHp, 0, maxHp, maxHp),
            maxSan,
            initialSan: clampNumber(input.initialSan, 0, maxSan, maxSan),
            currentSan: clampNumber(input.currentSan, 0, maxSan, maxSan),
            magicPoints: clampNumber(input.magicPoints ?? input.currentMp, 0, maxMp, maxMp),
            currentMp: clampNumber(input.currentMp ?? input.magicPoints, 0, maxMp, maxMp),
            maxMp,
            status: normalizeStatus(input.status),
            occupationSkillPoints: clampNumber(input.occupationSkillPoints, 0, 999, calculateOccupationSkillPoints(attributes, occupationId)),
            personalInterestPoints: clampNumber(input.personalInterestPoints, 0, 999, calculatePersonalInterestPoints(attributes)),
            skillSuccessLimits: normalizeSkillSuccessLimits(input.skillSuccessLimits),
            mov: clampNumber(input.mov, 0, 99, calculateMov(attributes)),
            build: clampNumber(input.build, -2, 99, damage.build),
            damageBonus: input.damageBonus || damage.damageBonus,
            armor: clampNumber(input.armor, 0, 99, 0),
            skills: normalizeSkills(input.skills, attributes, occupation),
            weapons: normalizeWeapons(input.weapons),
            equipment: normalizeEquipment(input.equipment),
            assets: {
                cash: clampNumber(input.assets?.cash, 0, 999999, 0),
                spendingLevel: clampNumber(input.assets?.spendingLevel, 0, 999999, 0),
                assetsText: input.assets?.assetsText || ""
            },
            background: normalizeBackground(input.background),
            relationships: normalizeRelationships(input.relationships),
            experiencedScenarios: normalizeExperiencedScenarios(input.experiencedScenarios),
            createdAt: input.createdAt || now,
            updatedAt: now
        };
    }

    function normalizeSkills(skills?: COC7Skill[], attributes?: COC7Attributes, occupation?: COC7Occupation): COC7Skill[] {
        const hasSource = Boolean(skills && skills.length);
        const source = hasSource ? mergeSkillCatalog(skills as COC7Skill[]) : BASE_SKILLS;
        return source.map((skill) => {
            const skillKey = resolveSkillKey(skill);
            const specialtyKey = String(skill.specialtyKey || "").trim();
            // 已存在 / 导入的角色卡以卡片自身存的基础值为准；新建或空技能使用有效基础值。
            const base = hasSource && Object.prototype.hasOwnProperty.call(skill, "base")
                ? clampNumber(skill.base, 0, 99, resolveEffectiveBase(skillKey, specialtyKey, attributes, occupation))
                : resolveEffectiveBase(skillKey, specialtyKey, attributes, occupation);
            const occupationPoints = clampNumber(skill.occupationPoints, 0, 99, 0);
            const interestPoints = clampNumber(skill.interestPoints, 0, 99, 0);
            const growthPoints = clampNumber(skill.growthPoints, 0, 99, 0);
            const value = clampNumber(skill.value, 0, 99, base + occupationPoints + interestPoints + growthPoints);
            const occupationSkill = occupation?.occupationSkills.includes(skillKey) || Boolean(skill.occupation);
            const result: COC7Skill = {
                id: skill.id || slugify(skill.name),
                skillKey,
                name: String(skill.name || "未命名技能").slice(0, 40),
                base,
                value,
                category: skill.category || "知识",
                checked: Boolean(skill.checked || occupationSkill),
                occupation: occupationSkill,
                isProfessional: occupationSkill,
                occupationPoints,
                interestPoints,
                growthPoints
            };
            if (specialtyKey) result.specialtyKey = specialtyKey;
            if (isUserDefinedBaseSkill(skillKey)) result.customBase = true;
            return result;
        });
    }

    function mergeSkillCatalog(skills: COC7Skill[]): COC7Skill[] {
        const byId = new Map(skills.map((skill) => [skill.id, skill]));
        const byKey = skills.reduce((index, skill) => {
            const skillKey = resolveSkillKey(skill);
            const list = index.get(skillKey) || [];
            list.push(skill);
            index.set(skillKey, list);
            return index;
        }, new Map<string, COC7Skill[]>());
        const merged = BASE_SKILLS.map((base) => {
            const skillKey = resolveSkillKey(base);
            const exact = byId.get(base.id);
            const fallback = byKey.get(skillKey)?.shift();
            return { ...base, ...(exact || fallback || {}) };
        });
        // Keep imported repeatable/custom skills that do not have a matching
        // catalog slot instead of silently dropping their numeric values.
        const consumed = new Set(merged.map((skill) => skill.id));
        for (const skill of skills) {
            if (!consumed.has(skill.id)) merged.push(skill);
        }
        return merged;
    }

    /** 技能基础值覆盖表键：无专精为 `skillKey`，有专精为 `skillKey.specialtyKey`。 */
    function skillBaseOverrideKey(skillKey: string, specialtyKey?: string): string {
        const specialty = String(specialtyKey || "").trim();
        return specialty ? `${skillKey}.${specialty}` : skillKey;
    }

    function normalizeSkillBaseMap(value: unknown): Record<string, number> {
        if (typeof value === "string") {
            try {
                return normalizeSkillBaseMap(JSON.parse(value));
            } catch {
                return {};
            }
        }
        if (!value || typeof value !== "object" || Array.isArray(value)) return {};
        return Object.entries(value as Record<string, unknown>).reduce((map, [rawKey, rawValue]) => {
            const key = String(rawKey || "").trim();
            if (!key || rawValue === null || rawValue === "") return map;
            const parsed = Number(rawValue);
            if (!Number.isFinite(parsed)) return map;
            map[key] = clampNumber(parsed, 0, 99, 0);
            return map;
        }, {} as Record<string, number>);
    }

    /** 管理员设置中的技能基础值（`general.toml` → `[character_rules].skill_bases`，JSON 字符串）。 */
    function getAdminSkillBases(): Record<string, number> {
        return normalizeSkillBaseMap(getConfigSection("character_rules")?.skill_bases);
    }

    /** 用户个性化设置的技能基础值（localStorage）。 */
    function getUserSkillBases(): Record<string, number> {
        const storage = safeStorage();
        return normalizeSkillBaseMap(storage?.getItem(USER_SKILL_BASES_STORAGE_KEY));
    }

    /** 当前房间房规中的技能基础值（进入房间后生效）。 */
    function getRoomSkillBases(): Record<string, number> {
        const houseRules = (global.currentRoom as { house_rules?: { skill_bases?: unknown } } | undefined)?.house_rules;
        return normalizeSkillBaseMap(houseRules?.skill_bases);
    }

    /** 标记为「用户自填」的技能（如自定义技能）不参与默认值解析。 */
    function isUserDefinedBaseSkill(skillKey: string): boolean {
        const entry = SKILL_CATALOG.find((item) => item.key === skillKey);
        return Boolean(entry?.userDefinedBase) || skillKey === "custom";
    }

    function getCatalogSkillEntry(skillKey: string): SkillCatalogEntry | undefined {
        return SKILL_CATALOG.find((entry) => entry.key === skillKey);
    }

    function getCatalogDefaultBase(skillKey: string, specialtyKey?: string): number | null {
        const entry = getCatalogSkillEntry(skillKey);
        if (!entry || entry.userDefinedBase) return null;
        const specialty = String(specialtyKey || "").trim();
        if (specialty) {
            const match = entry.specialties.find((item) => item.key === specialty);
            if (match && typeof match.base === "number") return clampNumber(match.base, 0, 99, entry.base);
        }
        return clampNumber(entry.base, 0, 99, 0);
    }

    /**
     * 有效基础值：房间规则 > 用户个性化 > 管理员设置 > 技能目录默认 > 动态值。
     * 动态值（闪避=DEX/2、母语=EDU）可被上级覆盖；职业文件 skillBases 保持最高优先。
     */
    function resolveEffectiveBase(skillKey: string, specialtyKey?: string, attributes?: COC7Attributes, occupation?: COC7Occupation): number {
        if (isUserDefinedBaseSkill(skillKey)) return 0;
        const specialty = String(specialtyKey || "").trim();
        const overrideKey = skillBaseOverrideKey(skillKey, specialty);
        if (occupation?.skillBases) {
            if (Object.prototype.hasOwnProperty.call(occupation.skillBases, overrideKey)) {
                return clampNumber(occupation.skillBases[overrideKey], 0, 99, 0);
            }
            if (specialty && Object.prototype.hasOwnProperty.call(occupation.skillBases, skillKey)) {
                return clampNumber(occupation.skillBases[skillKey], 0, 99, 0);
            }
        }
        for (const overrides of [getRoomSkillBases(), getUserSkillBases(), getAdminSkillBases()]) {
            if (Object.prototype.hasOwnProperty.call(overrides, overrideKey)) return overrides[overrideKey] ?? 0;
            if (specialty && Object.prototype.hasOwnProperty.call(overrides, skillKey)) return overrides[skillKey] ?? 0;
        }
        if (attributes) {
            if (skillKey === "dodge") return Math.floor(attributes.DEX / 2);
            if (skillKey === "languageOwn") return clampNumber(attributes.EDU, 0, 99, 0);
        }
        const catalogBase = getCatalogDefaultBase(skillKey, specialty);
        if (catalogBase !== null) return catalogBase;
        if (attributes && skillKey === "dodge") return Math.floor(attributes.DEX / 2);
        if (attributes && skillKey === "languageOwn") return clampNumber(attributes.EDU, 0, 99, 0);
        return 0;
    }

    function resolveSkillKey(skill: Pick<COC7Skill, "id" | "skillKey">): string {
        return skill.skillKey || skill.id.split("__")[0] || skill.id;
    }

    function getOccupationSpecialtyKey(occupation: COC7Occupation | undefined, skillKey: string): string {
        return (occupation?.occupationSkillEntries || []).filter((entry) => entry.skillKey === skillKey).map((entry) => entry.specialtyKey || "")[0] || "";
    }

    function normalizeWeapons(weapons?: COC7Weapon[]): COC7Weapon[] {
        return (weapons || []).map((weapon) => {
            const name = String(weapon.name || "").trim();
            if (!name || ["选择武器", "未选择武器", "未命名武器"].includes(name)) return createEmptyWeapon();
            return {
                name,
                skill: weapon.skill || "",
                skillKey: weapon.skillKey || "",
                specialtyKey: weapon.specialtyKey || "",
                damage: weapon.damage || "",
                range: weapon.range || "",
                impale: typeof weapon.impale === "boolean" ? weapon.impale : null,
                attacks: String(weapon.attacks || ""),
                ammo: String(weapon.ammo || ""),
                malfunction: String(weapon.malfunction || ""),
                weight: String(weapon.weight || ""),
                note: String(weapon.note || "")
            };
        });
    }

    function normalizeEquipment(equipment?: COC7EquipmentItem[]): COC7EquipmentItem[] {
        return (equipment || []).map((item) => ({
            name: item.name || "未命名装备",
            quantity: clampNumber(item.quantity, 1, 999, 1),
            weight: Math.max(0, Number(item.weight) || 0),
            volume: Math.max(0, Number(item.volume) || 0),
            notes: item.notes || ""
        }));
    }

    function normalizeBackground(background?: Partial<COC7Background>): COC7Background {
        return {
            appearance: background?.appearance || "",
            ideology: background?.ideology || "",
            significantPeople: background?.significantPeople || "",
            meaningfulLocations: background?.meaningfulLocations || "",
            treasuredPossessions: background?.treasuredPossessions || "",
            traits: background?.traits || "",
            injuriesScars: background?.injuriesScars || "",
            phobiasManias: background?.phobiasManias || "",
            arcaneTomes: background?.arcaneTomes || "",
            spells: background?.spells || "",
            encounters: background?.encounters || "",
            story: background?.story || "",
            education: background?.education || "",
            raceType: background?.raceType || "人类"
        };
    }

    function normalizeStatus(status?: Partial<CharacterStatusFlags>): CharacterStatusFlags {
        return {
            majorWound: Boolean(status?.majorWound),
            unconscious: Boolean(status?.unconscious),
            dead: Boolean(status?.dead),
            temporaryInsanity: Boolean(status?.temporaryInsanity),
            permanentInsanity: Boolean(status?.permanentInsanity),
            indefiniteInsanity: Boolean(status?.indefiniteInsanity)
        };
    }

    function normalizeSkillSuccessLimits(limits?: Partial<SkillSuccessLimits>): SkillSuccessLimits {
        return {
            occupation: clampNumber(limits?.occupation, 0, 99, 75),
            other: clampNumber(limits?.other, 0, 99, 50)
        };
    }

    function normalizeRelationships(relationships?: COC7Relationship[]): COC7Relationship[] {
        return (relationships || []).map((item) => ({
            name: item.name || "未命名关系",
            description: item.description || "",
            player: item.player || ""
        }));
    }

    function normalizeExperiencedScenarios(scenarios?: COC7ExperiencedScenario[]): COC7ExperiencedScenario[] {
        return (scenarios || []).map((item) => ({
            name: item.name || "",
            experience: item.experience || "",
            ...(item.sanChange || item.san_change ? { sanChange: item.sanChange || item.san_change } : {}),
            ...(item.otherChanges || item.other_changes ? { otherChanges: item.otherChanges || item.other_changes } : {})
        }));
    }

    function slugify(value: string): string {
        return value.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9\-\u4e00-\u9fa5]/g, "");
    }

    function cloneCard(card: COC7CharacterCard): COC7CharacterCard {
        return JSON.parse(JSON.stringify(card)) as COC7CharacterCard;
    }

    function safeStorage(): Storage | null {
        try { return global.localStorage || null; } catch { return null; }
    }

    function currentPlayerId(): string {
        return String(global.currentUser?.user_id ?? "");
    }

    function isBoundToCurrentPlayer(playerId: string): boolean {
        if (!playerId) return true;
        const user = global.currentUser;
        if (!user) return false;
        return playerId === String(user.user_id) || playerId === user.username;
    }

    function isCurrentUserElevated(): boolean {
        return ["ADMIN", "OWNER"].includes(global.currentUser?.role || "");
    }

    async function loadAssignableUsers(): Promise<void> {
        if (!isCurrentUserElevated()) {
            assignableUsers = [];
            return;
        }
        try {
            const response = await TrpgApi.get<ApiResponse<CharacterAssignableUser[]>>("/api/users");
            assignableUsers = response.success && Array.isArray(response.data) ? response.data : [];
            hydratePlayerOptions();
        } catch (error) {
            assignableUsers = [];
            console.warn("加载玩家列表失败:", error);
        }
    }

    function hydratePlayerOptions(): void {
        const list = byId<HTMLDataListElement>("characterPlayerOptions");
        if (!list) return;
        list.innerHTML = assignableUsers
            .filter((user) => user.status !== "banned")
            .map((user) => {
                const id = String(user.id);
                const username = String(user.username || user.id);
                return `<option value="${escapeHtml(username)}" label="${escapeHtml(id)}"></option>`;
            })
            .join("");
    }

    function playerDisplayName(playerId: string): string {
        if (!playerId || playerId === PLAYER_UNBOUND_LABEL) return PLAYER_UNBOUND_LABEL;
        const matchedUser = assignableUsers.find((user) => String(user.id) === playerId || user.username === playerId);
        if (matchedUser) return matchedUser.username || String(matchedUser.id);
        const currentUser = global.currentUser;
        if (currentUser && (String(currentUser.user_id ?? "") === playerId || currentUser.username === playerId)) {
            return currentUser.username || playerId;
        }
        return playerId;
    }

    function setPlayerBindingInputValue(playerId: string): void {
        setInputValue("characterBoundPlayer", playerDisplayName(playerId));
        const input = byId<HTMLInputElement>("characterBoundPlayer");
        if (input) {
            input.readOnly = !isCurrentUserElevated();
            input.placeholder = isCurrentUserElevated() ? "输入玩家 ID，或留空表示未绑定" : "";
        }
        updateUnbindButton(playerId);
    }

    function currentUserCharacterCards(): COC7CharacterCard[] {
        return cards.filter((card) => Boolean(card.playerId) && isBoundToCurrentPlayer(card.playerId));
    }

    function visibleCharacterCards(): COC7CharacterCard[] {
        return isCurrentUserElevated() ? cards : currentUserCharacterCards();
    }

    /** 未登录时目录接口返回 401（职业为空、技能只剩内置兜底），创建/导入必须先登录。 */
    function requireLoginForCharacterAction(action: string): boolean {
        if (global.currentUser) return true;
        showNotification(`请先登录后再${action}`, "warning");
        AuthModule.showAuthModal();
        return false;
    }

    function canCreateCharacterCard(): boolean {
        if (isCurrentUserElevated()) return true;
        const limit = loadRuleSettings().maxCardsPerUser;
        if (currentUserCharacterCards().length < limit) return true;
        notify(`普通用户最多只能拥有 ${limit} 张角色卡，请删除旧角色卡或联系管理员调整上限。`, "error");
        return false;
    }

    async function loadOccupationCatalogs(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<OccupationCatalogPayload[]>>("/api/character-catalogs/occupations");
            if (response.success && Array.isArray(response.data) && response.data.length) {
                PRESET_OCCUPATIONS = response.data.map(normalizeOccupationCatalog);
                hydrateOccupationSelect();
            }
        } catch (error) {
            console.warn("加载职业目录失败:", error);
        }
    }

    function normalizeOccupationCatalog(payload: OccupationCatalogPayload): COC7Occupation {
        const entries = payload.occupationSkills || [];
        const skillKeys = Array.from(new Set(entries.flatMap(occupationSkillKeys)));
        const minCredit = clampNumber(payload.creditRating?.min, 0, 99, 9);
        const maxCredit = clampNumber(payload.creditRating?.max, minCredit, 99, 30);
        return {
            id: payload.id || "",
            name: String(payload.name || "").trim() || localizeOccupationName(payload.nameKey, payload.id),
            nameKey: payload.nameKey || "",
            categoryKey: payload.categoryKey || "",
            category: String(payload.category || "").trim(),
            order: Number.isFinite(payload.order) ? Number(payload.order) : 0,
            creditRating: [minCredit, maxCredit],
            occupationSkills: skillKeys,
            pointsFormula: normalizeOccupationFormula(payload.occupationSkillPoints?.terms),
            occupationSkillEntries: entries,
            skillBases: normalizeSkillBases(payload.skillBases)
        };
    }

    function normalizeSkillBases(skillBases?: Record<string, number>): Record<string, number> {
        return Object.entries(skillBases || {}).reduce((bases, [skillKey, value]) => {
            const parsed = Number(value);
            if (Number.isFinite(parsed)) bases[skillKey] = clampNumber(parsed, 0, 99, 0);
            return bases;
        }, {} as Record<string, number>);
    }

    async function loadSkillCatalog(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<SkillCatalogPayload>>("/api/character-catalogs/skills");
            if (!response.success || !response.data) return;
            const payload = response.data;
            SKILL_CATALOG = normalizeSkillCatalog(payload.skills || []);
            SKILL_LOCALE_MAP = payload.locales?.[payload.defaultLocale || "zh-CN"] || payload.locales?.["zh-CN"] || {};
            SKILL_KEY_BY_LABEL = SKILL_CATALOG.reduce((index, entry) => {
                const sourceLabel = SKILL_LOCALE_MAP[entry.labelKey] || entry.labelKey.split(".").pop() || entry.key;
                const label = localizeCatalogText(sourceLabel);
                index[sourceLabel] = entry.key;
                index[label] = entry.key;
                return index;
            }, {} as Record<string, string>);
            BASE_SKILLS = flattenSkillCatalog(SKILL_CATALOG, SKILL_LOCALE_MAP);
        } catch (error) {
            console.warn("加载技能目录失败:", error);
        }
    }

    async function loadWeaponCatalog(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<WeaponCatalogPayload[]>>("/api/character-catalogs/weapons");
            WEAPON_CATALOG = response.success && Array.isArray(response.data)
                ? response.data.map(normalizeWeaponCatalog).filter((weapon) => Boolean(weapon.id))
                : [];
        } catch (error) {
            WEAPON_CATALOG = [];
            console.warn("加载武器目录失败:", error);
        }
    }

    function normalizeWeaponCatalog(payload: WeaponCatalogPayload): WeaponCatalogPayload {
        return {
            id: String(payload.id || "").trim(),
            name: String(payload.name || "未命名武器").slice(0, 60),
            skill: {
                skillKey: String(payload.skill?.skillKey || "").trim(),
                specialtyKey: String(payload.skill?.specialtyKey || "").trim(),
                label: String(payload.skill?.label || "").trim()
            },
            damage: String(payload.damage || "1D3"),
            attacks: String(payload.attacks || "1"),
            impale: Boolean(payload.impale),
            range: String(payload.range || "接触"),
            ammo: String(payload.ammo || "N/A"),
            malfunction: String(payload.malfunction || "N/A"),
            eras: Array.isArray(payload.eras) ? payload.eras.map((era) => String(era)) : [],
            price: String(payload.price || "N/A")
        };
    }

    function normalizeSkillCatalog(entries: SkillCatalogEntry[]): SkillCatalogEntry[] {
        return entries.map((entry) => ({
            key: String(entry.key || "").trim(),
            labelKey: String(entry.labelKey || "").trim(),
            category: normalizeSkillCategory(entry.category),
            base: clampNumber(entry.base, 0, 99, 0),
            repeatable: clampNumber(entry.repeatable, 1, 9, 1),
            specialties: Array.isArray(entry.specialties)
                ? entry.specialties.map((specialty) => {
                    const normalized: SkillSpecialtyCatalogEntry = {
                        key: String(specialty.key || "").trim(),
                        labelKey: String(specialty.labelKey || "").trim()
                    };
                    if (typeof specialty.base === "number") normalized.base = clampNumber(specialty.base, 0, 99, 0);
                    return normalized;
                }).filter((specialty) => Boolean(specialty.key))
                : [],
            eraLimited: Boolean(entry.eraLimited),
            userDefinedBase: Boolean(entry.userDefinedBase)
        })).filter((entry) => Boolean(entry.key));
    }

    function normalizeSkillCategory(value: SkillCategory | string): SkillCategory {
        const allowed: SkillCategory[] = ["特殊", "探索", "社交", "战斗", "医疗", "运动", "知识", "技术", "操纵", "其他"];
        return allowed.includes(value as SkillCategory) ? value as SkillCategory : "其他";
    }

    function flattenSkillCatalog(entries: SkillCatalogEntry[], locales: Record<string, string>): COC7Skill[] {
        const flattened: COC7Skill[] = [];
        entries.forEach((entry) => {
            const label = localizeCatalogText(locales[entry.labelKey] || entry.labelKey.split(".").pop() || entry.key);
            const repeatCount = Math.max(1, entry.repeatable || 1);
            for (let index = 0; index < repeatCount; index += 1) {
                const suffix = repeatCount > 1 ? index + 1 : 0;
                const base = resolveEffectiveBase(entry.key, "", undefined, undefined);
                flattened.push({
                    id: repeatCount > 1 ? `${entry.key}__${suffix}` : entry.key,
                    skillKey: entry.key,
                    name: label,
                    base,
                    value: base,
                    category: entry.category,
                    checked: false
                });
            }
        });
        return flattened.length ? flattened : BASE_SKILLS;
    }

    function normalizeOccupationFormula(terms?: OccupationPointFormulaTerm[]): OccupationPointFormula {
        const validTerms: OccupationPointFormulaTerm[] = [];
        (terms || []).forEach((term) => {
            if (Array.isArray(term.choose) && term.choose.length) {
                const choices = term.choose.filter((key) => ATTRIBUTE_KEYS.includes(key as COC7CoreAttributeKey));
                if (!choices.length) return;
                const multiplier = Number(term.multiplier);
                validTerms.push({ choose: choices, multiplier: Number.isFinite(multiplier) ? multiplier : 1 });
                return;
            }
            if (term.attribute === "AGE" || ATTRIBUTE_KEYS.includes(term.attribute as COC7CoreAttributeKey)) {
                const multiplier = Number(term.multiplier);
                validTerms.push({ attribute: term.attribute as COC7AttributeKey, multiplier: Number.isFinite(multiplier) ? multiplier : 1 });
            }
        });
        return validTerms.length ? validTerms : [{ attribute: "EDU", multiplier: 4 }];
    }

    function occupationSkillKeys(entry: OccupationSkillEntry): string[] {
        // 仅确定技能参与打勾；「或」「任意 N 项」等模糊条目由 occupationSkillEntries 保留展示。
        return entry.skillKey ? [entry.skillKey] : [];
    }

    function occupationSkillEntryLabel(entry: OccupationSkillEntry): string {
        if (entry.skillKey) return formatSkillLabel(entry.skillKey, entry.specialtyKey);
        if (entry.chooseOne) return entry.chooseOne.map(occupationSkillEntryLabel).join(localizeCatalogText("或"));
        if (entry.freeChoice) return localizeCatalogText(entry.freeChoice);
        return localizeCatalogText("自定义本职技能");
    }

    function formatSkillLabel(skillKey: string, specialtyKey?: string): string {
        const skillName = skillNameById(skillKey);
        if (!specialtyKey) return skillName;
        return `${skillName}(${localizeSkillSpecialty(skillKey, specialtyKey)})`;
    }

    function localizeSkillSpecialty(skillKey: string, specialtyKey: string): string {
        const source = SKILL_LOCALE_MAP[`skillSpecialties.${skillKey}.${specialtyKey}`] || specialtyKey;
        return localizeCatalogText(source);
    }

    function localizeOccupationName(nameKey?: string, fallback?: string): string {
        const fallbackText = fallback || nameKey || "未命名职业";
        const source = nameKey ? (window.TrpgI18n?.t(nameKey, fallbackText) || fallbackText) : fallbackText;
        return localizeCatalogText(source);
    }

    function localizeOccupationCategory(categoryKey?: string, fallback?: string): string {
        const fallbackText = fallback || "";
        const source = categoryKey ? (window.TrpgI18n?.t(categoryKey, fallbackText) || fallbackText) : fallbackText;
        return source ? localizeCatalogText(source) : "";
    }

    function localizeCatalogText(value: string): string {
        return window.TrpgI18n?.t("", value) || value;
    }

    async function loadCards(): Promise<void> {
        cards = [];
        try {
            const response = await TrpgApi.get<ApiResponse<COC7CharacterCardInput[]>>("/api/characters");
            if (response.success && Array.isArray(response.data)) {
                cards = response.data.map((card) => createCharacterCard(card));
            }
        } catch (error) {
            console.warn("加载角色卡失败：", error);
        }

        const storage = safeStorage();
        if (storage) {
            try {
                const parsed = JSON.parse(storage.getItem(STORAGE_KEY) || "[]") as unknown;
                if (Array.isArray(parsed) && parsed.length) {
                    const migratedCards = parsed.map((card) => createCharacterCard(card as COC7CharacterCardInput));
                    for (const card of migratedCards) {
                        if (!cards.some((item) => item.id === card.id)) {
                            cards.push(card);
                            await saveCardToServer(card);
                        }
                    }
                    storage.removeItem(STORAGE_KEY);
                    storage.removeItem(ACTIVE_STORAGE_KEY);
                }
            } catch {
                storage.removeItem(STORAGE_KEY);
            }
        }
        activeCardId = cards[0]?.id || "";
    }

    async function loadCharacterGallery(): Promise<void> {
        galleryCards = [];
        try {
            const response = await TrpgApi.get<ApiResponse<COC7CharacterCardInput[]>>("/api/character-gallery");
            if (response.success && Array.isArray(response.data)) {
                galleryCards = response.data.map((card) => createCharacterCard(card));
            }
        } catch (error) {
            console.warn("加载角色卡广场失败:", error);
        }
        renderCharacterGallery();
    }

    function renderCharacterGallery(): void {
        const list = byId("characterGalleryList");
        if (!list) return;
        const batch = characterBatchStates.gallery;
        pruneCharacterBatchSelection("gallery");
        if (!galleryCards.length) {
            list.innerHTML = `<div class="character-empty-filter">角色卡广场暂无公开角色卡。</div>`;
            updateCharacterBatchToolbar("gallery");
            return;
        }
        const visibleCards = filteredGalleryCards();
        if (!visibleCards.length) {
            list.innerHTML = `<div class="character-empty-filter">没有符合筛选条件的公开角色卡。</div>`;
            updateCharacterBatchToolbar("gallery");
            return;
        }
        list.innerHTML = visibleCards.map((card) => {
            const selectable = batch.mode && canModifyGalleryCard(card);
            const selected = selectable && batch.selected.has(card.id);
            return `
            <article class="character-card${batch.mode ? " batch-mode" : ""}${selected ? " batch-selected" : ""}" data-gallery-character-id="${escapeHtml(card.id)}"${selectable ? ' data-batch-selectable="true"' : ""}>
                ${selectable ? `<span class="batch-card-check" aria-hidden="true"><i class="fa ${selected ? "fa-check-square-o" : "fa-square-o"}"></i></span>` : ""}
                <div class="character-card-avatar"><img src="${escapeHtml(TrpgAvatar.resolveAvatarSrc(card.name, card.avatar))}" alt=""></div>
                <h5>${escapeHtml(card.name)}</h5>
                <p>${escapeHtml(getOccupation(card).name)} · ${escapeHtml(card.residence || "未知居住地")}</p>
                <p class="character-card-summary">${escapeHtml(card.background.story || card.background.appearance || "暂无角色简介")}</p>
                <div class="character-card-meta">
                    <span>ID ${escapeHtml(card.public_id || card.id)}</span>
                    <span>${escapeHtml(card.publisher_name ? `发布者 ${card.publisher_name}` : "公开角色卡")}</span>
                </div>
                <div class="character-card-actions">
                    ${batch.mode ? "" : `<button type="button" data-gallery-action="preview">预览</button><button type="button" data-gallery-action="apply">应用</button>${canModifyGalleryCard(card) ? `<button type="button" data-gallery-action="edit">编辑</button><button type="button" data-gallery-action="delete">删除</button>` : ""}`}
                </div>
            </article>
        `;
        }).join("");
        updateCharacterBatchToolbar("gallery");
    }

    function filteredGalleryCards(): COC7CharacterCard[] {
        const search = activeGallerySearchTerm.trim().toLowerCase();
        return galleryCards.filter((card) => {
            if (!search) return true;
            return [card.name, getOccupation(card).name, card.residence, card.public_id || card.id, card.publisher_name || ""]
                .some((value) => String(value || "").toLowerCase().includes(search));
        });
    }

    function handleGallerySearchInput(event: Event): void {
        activeGallerySearchTerm = (event.target as HTMLInputElement | null)?.value || "";
        renderCharacterGallery();
    }

    function handleGalleryClick(event: Event): void {
        if (characterBatchStates.gallery.mode) {
            const cardElement = (event.target as HTMLElement).closest<HTMLElement>("[data-gallery-character-id]");
            const batchId = cardElement?.dataset.galleryCharacterId;
            if (cardElement && batchId && cardElement.dataset.batchSelectable === "true") {
                toggleCharacterBatchSelection("gallery", batchId, cardElement);
            }
            return;
        }
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-gallery-action]");
        if (!button) return;
        const card = button.closest<HTMLElement>("[data-gallery-character-id]");
        const id = card?.dataset.galleryCharacterId;
        if (!id) return;
        if (button.dataset.galleryAction === "apply") applyGalleryCharacter(id);
        if (button.dataset.galleryAction === "preview") previewGalleryCharacter(id);
        if (button.dataset.galleryAction === "edit") editGalleryCharacter(id);
        if (button.dataset.galleryAction === "delete") void deleteGalleryCharacter(id);
    }

    function canModifyGalleryCard(card: COC7CharacterCard): boolean {
        const role = window.currentUser?.role || "USER";
        return role === "ADMIN" || role === "OWNER" || String(card.publisher_id || "") === String(currentPlayerId());
    }

    function previewGalleryCharacter(id: string): void {
        const card = galleryCards.find((item) => item.id === id);
        if (!card) return;
        const modalElement = document.createElement("div");
        modalElement.className = "modal fade";
        modalElement.innerHTML = `<div class="modal-dialog modal-xl"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">公开角色卡预览</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body">${renderCharacterDetail(card)}</div></div></div>`;
        document.body.appendChild(modalElement);
        const instance = new bootstrap.Modal(modalElement);
        instance.show();
        modalElement.addEventListener("hidden.bs.modal", () => modalElement.remove());
    }

    function editGalleryCharacter(id: string): void {
        const card = galleryCards.find((item) => item.id === id);
        if (card && canModifyGalleryCard(card)) {
            activeGalleryEditId = id;
            openEditor(card);
        }
    }

    async function deleteGalleryCharacter(id: string): Promise<void> {
        const card = galleryCards.find((item) => item.id === id);
        if (!card || !canModifyGalleryCard(card)) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: "删除公开角色卡",
            message: "确定要删除这张公开角色卡吗？",
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;
        const response = await TrpgApi.del<ApiResponse>(`/api/character-gallery/${encodeURIComponent(id)}`);
        if (!response.success) {
            notify(response.error || response.message || "删除公开角色卡失败", "error");
            return;
        }
        galleryCards = galleryCards.filter((item) => item.id !== id);
        renderCharacterGallery();
    }

    function applyGalleryCharacter(galleryCharacterId: string): void {
        const source = galleryCards.find((card) => card.id === galleryCharacterId);
        if (!source || !canCreateCharacterCard()) return;
        const {
            public_id: _publicId,
            publisher_id: _publisherId,
            publisher_name: _publisherName,
            ...sourceCard
        } = cloneCard(source);
        openEditor(createCharacterCard({
            ...sourceCard,
            id: `gallery-${Date.now()}`,
            playerId: currentPlayerId()
        }));
    }

    function clearCharacterSheetState(): void {
        cards = [];
        galleryCards = [];
        activeCardId = "";
        activeGallerySearchTerm = "";
        activeGalleryEditId = "";
        characterBatchStates.card.mode = false;
        characterBatchStates.card.selected.clear();
        characterBatchStates.gallery.mode = false;
        characterBatchStates.gallery.selected.clear();
        editorSkills = [];
        pendingGeneratedName = "";
        pendingWeaponPickerTarget = "";
        modal?.hide();
        occupationTemplateModal?.hide();
        weaponPickerModal?.hide();
        render();
    }

    async function reloadCharacterSheet(): Promise<void> {
        // 登录后重新拉取目录：页面初始化时若尚未登录，目录请求会 401，
        // 只在登录后刷新角色卡列表而不重载目录会导致职业/技能一直为空。
        await Promise.all([loadSkillCatalog(), loadOccupationCatalogs(), loadWeaponCatalog()]);
        await loadCards();
        await loadAssignableUsers();
        render();
    }

    async function saveCardToServer(card: COC7CharacterCard): Promise<COC7CharacterCard | null> {
        try {
            const response = await TrpgApi.put<ApiResponse<COC7CharacterCard>>(`/api/characters/${encodeURIComponent(card.id)}`, card);
            if (response.success && response.data) return createCharacterCard(response.data);
            notify(response.message || response.error || "保存角色卡失败", "error");
        } catch (error) {
            notify(`保存角色卡失败：${characterErrorMessage(error)}`, "error");
        }
        return null;
    }

    async function saveGalleryCardToServer(card: COC7CharacterCard): Promise<COC7CharacterCard | null> {
        try {
            const publicId = card.public_id || card.id;
            const response = await TrpgApi.put<ApiResponse<COC7CharacterCard>>(`/api/character-gallery/${encodeURIComponent(publicId)}`, card);
            if (response.success && response.data) return createCharacterCard(response.data);
            notify(response.message || response.error || "保存广场角色卡失败", "error");
        } catch (error) {
            notify(`保存广场角色卡失败：${characterErrorMessage(error)}`, "error");
        }
        return null;
    }

    function persistCards(): void {
        const card = cards.find((item) => item.id === activeCardId);
        if (!card) return;
        void saveCardToServer(card).then((savedCard) => {
            if (!savedCard) return;
            cards = cards.map((item) => item.id === savedCard.id ? savedCard : item);
            renderList();
        });
    }

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return typeof document === "undefined" ? null : document.getElementById(id) as T | null;
    }

    function initCharacterSheet(): void {
        if (typeof document === "undefined") return;
        const workspace = byId("characterWorkspace");
        if (!workspace || workspace.dataset.initialized === "true") return;
        workspace.dataset.initialized = "true";
        const modalElement = byId("characterModal");
        modal = modalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(modalElement) : null;
        const nameModalElement = byId("nameGeneratorModal");
        nameGeneratorModal = nameModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(nameModalElement) : null;
        const occupationModalElement = byId("occupationTemplateModal");
        occupationTemplateModal = occupationModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(occupationModalElement) : null;
        const weaponPickerModalElement = byId("characterWeaponPickerModal");
        weaponPickerModal = weaponPickerModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(weaponPickerModalElement) : null;
        const skillSpecialtyModalElement = byId("skillSpecialtyModal");
        skillSpecialtyModal = skillSpecialtyModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(skillSpecialtyModalElement) : null;
        const skillBaseSettingsModalElement = byId("skillBaseSettingsModal");
        skillBaseSettingsModal = skillBaseSettingsModalElement && typeof bootstrap !== "undefined" ? new bootstrap.Modal(skillBaseSettingsModalElement) : null;
        hydrateOccupationSelect();
        bindEvents();
        void Promise.all([loadSkillCatalog(), loadOccupationCatalogs(), loadWeaponCatalog()]).then(() => {
            hydrateOccupationSelect();
            void Promise.all([loadCards(), loadCharacterGallery()]).then(render);
        });
        void loadAssignableUsers().then(render);
    }

    function bindEvents(): void {
        byId("createCharacter")?.addEventListener("click", () => {
            if (!requireLoginForCharacterAction("创建角色卡")) return;
            if (!canCreateCharacterCard()) return;
            openEditor();
        });
        byId("saveCharacter")?.addEventListener("click", () => {
            void saveFromEditor();
        });
        byId("importCharacter")?.addEventListener("click", () => {
            if (!requireLoginForCharacterAction("导入角色卡")) return;
            byId<HTMLInputElement>("importCharacterFile")?.click();
        });
        byId<HTMLInputElement>("importCharacterFile")?.addEventListener("change", importCharacterFiles);
        byId("exportCharacter")?.addEventListener("click", exportActiveCard);
        // 「角色卡广场」按钮此前没有绑定任何事件，点击无反应；这里切到广场标签页。
        byId("openCharacterGallery")?.addEventListener("click", () => {
            window.switchMainTab?.("character-gallery");
        });
        byId("backToCharacterList")?.addEventListener("click", showCharacterList);
        byId("randomizeCharacterName")?.addEventListener("click", openNameGenerator);
        byId("regenerateName")?.addEventListener("click", regenerateNamePreview);
        byId("confirmGeneratedName")?.addEventListener("click", confirmGeneratedName);
        byId("cancelGeneratedName")?.addEventListener("click", () => nameGeneratorModal?.hide());
        byId("nameRegionSelect")?.addEventListener("change", regenerateNamePreview);
        byId("nameGenderSelect")?.addEventListener("change", regenerateNamePreview);
        byId("openOccupationTemplatePicker")?.addEventListener("click", openOccupationTemplatePicker);
        byId("createGalleryCharacter")?.addEventListener("click", () => {
            if (!requireLoginForCharacterAction("创建角色卡")) return;
            if (canCreateCharacterCard()) openEditor();
        });
        byId("characterGallerySearch")?.addEventListener("input", handleGallerySearchInput);
        byId("characterGalleryList")?.addEventListener("click", handleGalleryClick);
        bindCharacterBatchControls("card");
        bindCharacterBatchControls("gallery");
        byId("characterAvatarPreview")?.addEventListener("click", () => byId<HTMLInputElement>("characterAvatarUpload")?.click());
        byId("unbindCharacterPlayer")?.addEventListener("click", unbindCharacterPlayerFromEditor);
        byId("randomizeAttributes")?.addEventListener("click", () => {
            const attributes = randomizeAttributes();
            ATTRIBUTE_KEYS.forEach((key) => setInputValue(`attribute${key}`, attributes[key]));
            setInputValue("characterAge", attributes.AGE);
            refreshEditorRuleSummary();
        });
        byId("saveCharacterRuleSettings")?.addEventListener("click", () => {
            void saveRuleSettingsFromPanel();
        });
        byId("openAdminSkillBaseSettings")?.addEventListener("click", () => openSkillBaseSettings("admin"));
        byId("openUserSkillBaseSettings")?.addEventListener("click", () => openSkillBaseSettings("user"));
        byId("characterOccupation")?.addEventListener("input", () => {
            occupationSkillPointsManuallyEdited = false;
            editorSkills = readChecklistSkills();
            hydrateSkillChecklist(editorSkills);
            refreshEditorRuleSummary();
            renderOccupationHint();
        });
        byId("characterCreditRating")?.addEventListener("input", syncEditorCreditRating);
        byId("characterOccupationSkillPoints")?.addEventListener("input", () => {
            occupationSkillPointsManuallyEdited = true;
            refreshSkillTableCalculations();
        });
        byId("characterPersonalInterestPoints")?.addEventListener("input", () => {
            personalInterestPointsManuallyEdited = true;
            refreshSkillTableCalculations();
        });
        ["characterDamageBonus", "characterBuild", "characterArmor", "characterMov"].forEach((fieldId) => {
            byId(fieldId)?.addEventListener("input", () => {
                combatStatsManuallyEdited = true;
            });
        });
        byId("generateOccupationSkillPoints")?.addEventListener("click", () => {
            occupationSkillPointsManuallyEdited = false;
            setGeneratedOccupationSkillPoints();
            refreshSkillTableCalculations();
        });
        byId("generatePersonalInterestPoints")?.addEventListener("click", () => {
            personalInterestPointsManuallyEdited = false;
            setGeneratedPersonalInterestPoints();
            refreshSkillTableCalculations();
        });
        ["characterOccupationSkillLimit", "characterOtherSkillLimit"].forEach((fieldId) => {
            // 输入过程中只重算派生阈值，避免把尚未输入完整的中间值（如先输入“9”）当作上限写回所有技能
            byId(fieldId)?.addEventListener("input", refreshSkillTableThresholds);
            // 输入完成（失焦/确认）后，再按最终有效上限裁剪各技能点数
            byId(fieldId)?.addEventListener("change", refreshSkillTableCalculations);
        });
        byId("characterSkillCategoryFilters")?.addEventListener("click", handleSkillCategoryFilterClick);
        byId("characterSkillTableBody")?.addEventListener("input", handleSkillTableInput);
        byId("characterSkillTableBody")?.addEventListener("change", handleSkillTableInput);
        byId("characterSkillTableBody")?.addEventListener("click", handleSkillTableClick);
        byId("skillSpecialtyList")?.addEventListener("click", handleSkillSpecialtyListClick);
        byId("cancelSkillSpecialty")?.addEventListener("click", () => skillSpecialtyModal?.hide());
        byId("confirmSkillSpecialty")?.addEventListener("click", confirmSkillSpecialtySelection);
        byId("saveSkillBaseSettings")?.addEventListener("click", () => {
            void saveSkillBaseSettings();
        });
        byId("characterWeaponTableBody")?.addEventListener("input", handleWeaponTableInput);
        byId("characterWeaponTableBody")?.addEventListener("change", handleWeaponTableInput);
        byId("characterWeaponTableBody")?.addEventListener("click", handleWeaponTableClick);
        byId("characterWeaponCatalogList")?.addEventListener("click", handleWeaponCatalogClick);
        byId("createCustomWeapon")?.addEventListener("click", createCustomWeaponRow);
        byId("removeCurrentWeapon")?.addEventListener("click", removeCurrentWeaponRow);
        byId("addCharacterCompanion")?.addEventListener("click", () => addRelationshipRow());
        byId("characterCompanionList")?.addEventListener("click", handleRelationshipRowClick);
        byId("addCharacterScenario")?.addEventListener("click", () => addExperiencedScenarioRow());
        byId("characterScenarioList")?.addEventListener("click", handleExperiencedScenarioRowClick);
        document.querySelectorAll<HTMLInputElement>(".character-attribute-input").forEach((input) => {
            input.addEventListener("input", () => {
                refreshEditorRuleSummary();
            });
        });
        byId("characterAge")?.addEventListener("input", () => {
            refreshEditorRuleSummary();
        });
        ["characterCurrentHp", "characterCurrentMp", "characterCurrentSan", "characterInitialSan"].forEach((fieldId) => {
            byId(fieldId)?.addEventListener("input", refreshEditorRuleSummary);
        });
        byId<HTMLInputElement>("characterAvatarUpload")?.addEventListener("change", handleAvatarUpload);
        hydrateRuleSettingsPanel();
    }

    function hydrateOccupationSelect(): void {
        const list = byId<HTMLDataListElement>("characterOccupationOptions");
        if (!list) return;
        list.innerHTML = PRESET_OCCUPATIONS.map((occupation) => `<option value="${escapeHtml(occupationDisplayName(occupation))}"></option>`).join("");
    }

    function hydrateSkillChecklist(skills: COC7Skill[] = BASE_SKILLS): void {
        renderSkillTable(skills);
        syncEditorCreditRating();
    }

    function renderSkillTable(skills: COC7Skill[] = BASE_SKILLS): void {
        const body = byId("characterSkillTableBody");
        if (!body) return;
        const occupation = resolveOccupationFromInput(getInputValue("characterOccupation"));
        const normalized = normalizeSkills(skills, readAttributes(), occupation);
        const availableCategories = new Set<string>(SKILL_FILTER_CATEGORIES);
        body.innerHTML = normalized.map((skill) => {
            const skillKey = resolveSkillKey(skill);
            const rowId = escapeHtml(skill.id);
            const category = escapeHtml(skill.category || "其他");
            const allowed = availableCategories.has(skill.category || "其他");
            const successLimit = skill.occupation ? getSkillLimit("occupation") : getSkillLimit("other");
            const occupationPoints = skill.occupationPoints || 0;
            const interestPoints = skill.interestPoints || 0;
            const growthPoints = skill.growthPoints || 0;
            const success = clampNumber(skill.base + occupationPoints + interestPoints + growthPoints, 0, successLimit, skill.base);
            const occupationDisabled = skill.occupation ? "" : "disabled";
            const typeButton = buildSkillSpecialtyButton(skill);
            const customNameInput = buildCustomSkillNameInput(skill, rowId);
            const displayName = isCustomSkill(skillKey) ? (skill.name || skillNameById(skillKey)) : skill.name;
            return `
                <tr data-skill-row-id="${rowId}" data-skill-key="${escapeHtml(skillKey)}" data-skill-category="${category}" data-skill-occupation="${skill.occupation ? "1" : "0"}" ${allowed ? "" : 'hidden="hidden"'}>
                    <td><input type="checkbox" class="form-check-input" data-skill-occupation-checkbox="${rowId}" ${skill.occupation ? "checked" : ""}></td>
                    <td>
                        <div class="character-skill-name-cell">
                            <span data-skill-display-name="${rowId}">${escapeHtml(displayName)}</span>
                            ${customNameInput}
                            ${typeButton}
                        </div>
                    </td>
                    <td>${buildSkillBaseCell(skill, rowId)}</td>
                    <td><input type="number" class="form-control form-control-sm character-skill-points-input" min="0" max="${getSkillLimit("occupation")}" value="${occupationPoints}" data-skill-occupation-points="${rowId}" ${occupationDisabled}></td>
                    <td><input type="number" class="form-control form-control-sm character-skill-points-input" min="0" max="${getSkillLimit("other")}" value="${interestPoints}" data-skill-interest-points="${rowId}"></td>
                    <td><input type="number" class="form-control form-control-sm character-skill-points-input" min="0" max="${getSkillLimit("other")}" value="${growthPoints}" data-skill-growth-points="${rowId}"></td>
                    <td><span class="character-skill-value-cell" data-skill-success="${rowId}">${success}</span></td>
                    <td><span class="character-skill-value-cell" data-skill-hard="${rowId}">${Math.floor(success / 2)}</span></td>
                    <td><span class="character-skill-value-cell" data-skill-extreme="${rowId}">${Math.floor(success / 5)}</span></td>
                </tr>
            `;
        }).join("");
        refreshSkillTableVisibility();
        refreshSkillPointSummary();
        syncEditorCreditRating();
    }

    /** 自定义技能（用户自填基础值）渲染为可输入控件，其余展示只读基础值。 */
    function buildSkillBaseCell(skill: COC7Skill, rowId: string): string {
        const skillKey = resolveSkillKey(skill);
        if (isUserDefinedBaseSkill(skillKey)) {
            return `<input type="number" class="form-control form-control-sm character-skill-points-input" min="0" max="99" value="${skill.base}" data-skill-base="${rowId}">`;
        }
        return `<span class="character-skill-value-cell" data-skill-base="${rowId}">${skill.base}</span>`;
    }

    function buildCustomSkillNameInput(skill: COC7Skill, rowId: string): string {
        const skillKey = resolveSkillKey(skill);
        if (!isCustomSkill(skillKey)) return "";
        const defaultName = skillNameById(skillKey);
        const customName = skill.name && skill.name !== defaultName ? skill.name : "";
        return `<input type="text" class="form-control form-control-sm character-custom-skill-name-input" maxlength="40" value="${escapeHtml(customName)}" placeholder="输入自定义技能" data-custom-skill-name="${rowId}">`;
    }

    function isCustomSkill(skillKey: string): boolean {
        return skillKey === "custom";
    }

    function buildSkillSpecialtyButton(skill: COC7Skill): string {
        const skillKey = resolveSkillKey(skill);
        if (!getSkillSpecialties(skillKey).length) return "";
        const specialtyKey = String(skill.specialtyKey || "").trim();
        const label = specialtyKey
            ? localizeSkillSpecialty(skillKey, specialtyKey)
            : (window.TrpgI18n?.t("character.skill.specialty.choose", "选择{name}", { name: skill.name }) || `选择${skill.name}`);
        return `<button type="button" class="character-skill-specialty-button" data-skill-specialty-trigger="${escapeHtml(skill.id)}">${escapeHtml(label)}</button>`;
    }

    function getSkillSpecialties(skillKey: string): SkillSpecialtyCatalogEntry[] {
        return getCatalogSkillEntry(skillKey)?.specialties || [];
    }

    function handleSkillTableClick(event: Event): void {
        const trigger = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-skill-specialty-trigger]");
        if (!trigger) return;
        openSkillSpecialtyPicker(trigger.dataset.skillSpecialtyTrigger || "");
    }

    function openSkillSpecialtyPicker(rowId: string): void {
        const skill = editorSkills.find((item) => item.id === rowId);
        const skillKey = skill ? resolveSkillKey(skill) : (rowId.split("__")[0] || rowId);
        const specialties = getSkillSpecialties(skillKey);
        if (!specialties.length) return;
        pendingSkillSpecialtyRowId = rowId;
        pendingSkillSpecialtyKey = String(skill?.specialtyKey || "").trim();
        setInputValue("skillSpecialtyTargetRow", rowId);
        const list = byId("skillSpecialtyList");
        if (list) {
            list.innerHTML = specialties.map((specialty) => {
                const active = specialty.key === pendingSkillSpecialtyKey;
                return `<button type="button" class="character-skill-specialty-option${active ? " is-active" : ""}" data-skill-specialty-option="${escapeHtml(specialty.key)}">${escapeHtml(localizeSkillSpecialty(skillKey, specialty.key))}</button>`;
            }).join("");
        }
        skillSpecialtyModal?.show();
    }

    function handleSkillSpecialtyListClick(event: Event): void {
        const option = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-skill-specialty-option]");
        if (!option) return;
        pendingSkillSpecialtyKey = option.dataset.skillSpecialtyOption || "";
        document.querySelectorAll<HTMLElement>("#skillSpecialtyList [data-skill-specialty-option]").forEach((item) => {
            item.classList.toggle("is-active", item === option);
        });
    }

    function confirmSkillSpecialtySelection(): void {
        if (pendingSkillSpecialtyRowId) applySkillSpecialty(pendingSkillSpecialtyRowId, pendingSkillSpecialtyKey);
        skillSpecialtyModal?.hide();
    }

    /**
     * 低耦合：把专精写入指定技能行并重算基础值。
     * 专精弹窗与「选择职业」等流程均可直接调用，无需关心行的渲染细节。
     */
    function applySkillSpecialty(rowId: string, specialtyKey: string): void {
        editorSkills = readChecklistSkills();
        const index = editorSkills.findIndex((item) => item.id === rowId);
        const skill = index < 0 ? undefined : editorSkills[index];
        if (!skill) return;
        const skillKey = resolveSkillKey(skill);
        const nextSpecialty = String(specialtyKey || "").trim();
        const base = resolveEffectiveBase(skillKey, nextSpecialty, readAttributes(), resolveOccupationFromInput(getInputValue("characterOccupation")));
        const next: COC7Skill = { ...skill, base };
        if (nextSpecialty) next.specialtyKey = nextSpecialty;
        else delete next.specialtyKey;
        next.value = clampNumber(base + (skill.occupationPoints || 0) + (skill.interestPoints || 0) + (skill.growthPoints || 0), 0, 99, base);
        editorSkills[index] = next;
        hydrateSkillChecklist(editorSkills);
    }

    function formatDefaultBaseLabel(skillKey: string, value: number | null): string {
        if (skillKey === "dodge") return "DEX/2";
        if (skillKey === "languageOwn") return "EDU";
        return value === null ? "—" : String(value);
    }

    function renderSkillBaseSettingsRow(key: string, label: string, defaultLabel: string, overrides: Record<string, number>, editable: boolean): string {
        const current = Object.prototype.hasOwnProperty.call(overrides, key) ? String(overrides[key]) : "";
        const control = editable
            ? `<input type="number" class="form-control form-control-sm character-skill-base-input" min="0" max="99" value="${escapeHtml(current)}" placeholder="留空=默认" data-skill-base-key="${escapeHtml(key)}">`
            : `<span class="character-skill-base-readonly">用户自填</span>`;
        return `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(defaultLabel)}</td><td>${control}</td></tr>`;
    }

    function renderSkillBaseSettingsTable(overrides: Record<string, number>): void {
        const body = byId("skillBaseSettingsTableBody");
        if (!body) return;
        const rows: string[] = [];
        SKILL_CATALOG.forEach((entry) => {
            const skillName = localizeCatalogText(SKILL_LOCALE_MAP[entry.labelKey] || entry.labelKey.split(".").pop() || entry.key);
            if (entry.userDefinedBase) {
                rows.push(renderSkillBaseSettingsRow(entry.key, skillName, "用户自填", overrides, false));
                return;
            }
            rows.push(renderSkillBaseSettingsRow(entry.key, skillName, formatDefaultBaseLabel(entry.key, getCatalogDefaultBase(entry.key, "")), overrides, true));
            entry.specialties.forEach((specialty) => {
                const label = `${skillName}(${localizeSkillSpecialty(entry.key, specialty.key)})`;
                const key = skillBaseOverrideKey(entry.key, specialty.key);
                rows.push(renderSkillBaseSettingsRow(key, label, formatDefaultBaseLabel(entry.key, getCatalogDefaultBase(entry.key, specialty.key)), overrides, true));
            });
        });
        body.innerHTML = rows.join("");
    }

    function collectSkillBaseOverridesFromModal(): Record<string, number> {
        const body = byId("skillBaseSettingsTableBody");
        if (!body) return {};
        return Array.from(body.querySelectorAll<HTMLInputElement>("[data-skill-base-key]")).reduce((map, input) => {
            const key = input.dataset.skillBaseKey || "";
            const raw = input.value.trim();
            if (!key || raw === "") return map;
            const parsed = Number(raw);
            if (!Number.isFinite(parsed)) return map;
            map[key] = clampNumber(parsed, 0, 99, 0);
            return map;
        }, {} as Record<string, number>);
    }

    function skillBaseSettingsHint(mode: SkillBaseSettingsMode): string {
        if (mode === "admin") return "管理员默认值仅作兜底：房间规则与用户个性化未填写时才生效。";
        if (mode === "room") return "房间规则优先级最高，进入房间后覆盖用户个性化与管理员默认值。";
        return "用户个性化基础值用于新建角色卡，进入房间后以房规为准。";
    }

    function openSkillBaseSettings(mode: SkillBaseSettingsMode): void {
        skillBaseSettingsMode = mode;
        setText("skillBaseSettingsMessage", "");
        setText("skillBaseSettingsHint", skillBaseSettingsHint(mode));
        const overrides = mode === "admin" ? getAdminSkillBases() : mode === "room" ? getRoomSkillBases() : getUserSkillBases();
        renderSkillBaseSettingsTable(overrides);
        skillBaseSettingsModal?.show();
    }

    async function saveSkillBaseSettings(): Promise<void> {
        const overrides = collectSkillBaseOverridesFromModal();
        setText("skillBaseSettingsMessage", "");
        if (skillBaseSettingsMode === "user") {
            const storage = safeStorage();
            if (storage) storage.setItem(USER_SKILL_BASES_STORAGE_KEY, JSON.stringify(overrides));
            await reloadSkillBases();
            setText("skillBaseSettingsMessage", "已保存");
            return;
        }
        if (skillBaseSettingsMode === "admin") {
            const generalConfig = global.configManager?.getConfig("general") || {};
            const characterRules = { ...(getConfigSection("character_rules") || {}), skill_bases: JSON.stringify(overrides) };
            const saved = await global.configManager?.saveConfig("general", { ...generalConfig, character_rules: characterRules });
            await reloadSkillBases();
            setText("skillBaseSettingsMessage", saved === false ? "技能基础值保存失败" : "技能基础值已保存");
            return;
        }
        const roomId = String((global.currentRoom as { id?: string } | undefined)?.id || "");
        if (!roomId) {
            setText("skillBaseSettingsMessage", "尚未进入房间，无法保存房间规则");
            return;
        }
        try {
            const response = await TrpgApi.put<ApiResponse<{ house_rules?: { skill_bases?: Record<string, number> } }>>(
                `/api/rooms/${encodeURIComponent(roomId)}/house-rules`,
                { house_rules: { skill_bases: overrides } }
            );
            if (!response.success) {
                setText("skillBaseSettingsMessage", response.message || "技能基础值保存失败");
                return;
            }
            const room = global.currentRoom as { house_rules?: Record<string, unknown> } | undefined;
            if (room) room.house_rules = { ...(room.house_rules || {}), skill_bases: response.data?.house_rules?.skill_bases ?? overrides };
            await reloadSkillBases();
            setText("skillBaseSettingsMessage", "技能基础值已保存");
        } catch (error) {
            console.error("保存房间技能基础值失败:", error);
            setText("skillBaseSettingsMessage", "技能基础值保存失败，请稍后重试");
        }
    }

    /** 重新加载技能目录并刷新当前编辑中的技能表（房间切换 / 保存基础值后调用）。 */
    async function reloadSkillBases(): Promise<void> {
        await loadSkillCatalog();
        if (editorSkills.length) hydrateSkillChecklist(editorSkills);
    }

    function getSkillLimit(type: "occupation" | "other"): number {
        return clampNumber(getInputValue(type === "occupation" ? "characterOccupationSkillLimit" : "characterOtherSkillLimit"), 0, 99, type === "occupation" ? 75 : 50);
    }

    function handleSkillCategoryFilterClick(event: Event): void {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-skill-category]");
        if (!button) return;
        activeSkillCategoryFilter = button.dataset.skillCategory || "全部技能";
        document.querySelectorAll<HTMLElement>("#characterSkillCategoryFilters [data-skill-category]").forEach((item) => {
            item.classList.toggle("is-active", item === button);
        });
        refreshSkillTableVisibility();
    }

    function handleSkillTableInput(event: Event): void {
        const row = (event.target as HTMLElement).closest<HTMLTableRowElement>("tr[data-skill-row-id]");
        if (!row) return;
        const target = event.target as HTMLElement;
        if (target.closest("[data-custom-skill-name]")) {
            syncCustomSkillDisplayName(row);
        }
        refreshSkillTableCalculations();
        syncEditorCreditRating();
    }

    function hydrateWeaponTable(weapons: COC7Weapon[] = []): void {
        renderWeaponTable(weapons);
    }

    function renderWeaponTable(weapons: COC7Weapon[] = []): void {
        const body = byId("characterWeaponTableBody");
        if (!body) return;
        const normalized = ensureWeaponSlots(normalizeWeapons(weapons));
        body.innerHTML = normalized.map((weapon, index) => renderWeaponRow(weapon, index)).join("");
        refreshWeaponSuccessRates();
    }

    function getWeaponSlotCount(): number {
        return loadRuleSettings().weaponSlotCount;
    }

    function ensureWeaponSlots(weapons: COC7Weapon[]): COC7Weapon[] {
        const slots = Math.max(getWeaponSlotCount(), weapons.length);
        return Array.from({ length: slots }, (_, index) => weapons[index] || createEmptyWeapon());
    }

    function renderWeaponRow(weapon: COC7Weapon, index: number): string {
        const rowId = `weapon-${index}`;
        const skillOptions = weaponSkillOptions(weapon);
        const success = findSkillSuccessByWeaponSkill(weapon);
        return `
            <tr data-weapon-row-id="${rowId}">
                <td>
                    <div class="character-input-shell">
                        <input type="text" class="form-control form-control-sm character-weapon-name-input" data-weapon-name="${rowId}" value="${escapeHtml(weapon.name)}" placeholder="选择或输入武器">
                        <button type="button" class="character-input-action" data-weapon-picker-trigger="${rowId}" title="从武器库选择" aria-label="从武器库选择">
                            <i class="fa fa-list" aria-hidden="true"></i>
                        </button>
                    </div>
                </td>
                <td>
                    <select class="form-select form-select-sm character-weapon-skill-select" data-weapon-skill="${rowId}">
                        ${skillOptions}
                    </select>
                </td>
                <td><span class="character-skill-value-cell" data-weapon-success="${rowId}">${success}</span></td>
                <td><input type="text" class="form-control form-control-sm character-weapon-input" data-weapon-damage="${rowId}" value="${escapeHtml(weapon.damage)}"></td>
                <td><input type="text" class="form-control form-control-sm character-weapon-input" data-weapon-range="${rowId}" value="${escapeHtml(weapon.range)}"></td>
                <td>
                    <select class="form-select form-select-sm character-weapon-impale-select" data-weapon-impale="${rowId}">
                        <option value="" ${weapon.impale === null ? "selected" : ""}>-</option>
                        <option value="false" ${weapon.impale === false ? "selected" : ""}>否</option>
                        <option value="true" ${weapon.impale ? "selected" : ""}>是</option>
                    </select>
                </td>
                <td><input type="text" class="form-control form-control-sm character-weapon-input character-weapon-input-narrow" data-weapon-attacks="${rowId}" value="${escapeHtml(weapon.attacks)}"></td>
                <td><input type="text" class="form-control form-control-sm character-weapon-input character-weapon-input-narrow" data-weapon-ammo="${rowId}" value="${escapeHtml(weapon.ammo)}"></td>
                <td><input type="text" class="form-control form-control-sm character-weapon-input character-weapon-input-narrow" data-weapon-malfunction="${rowId}" value="${escapeHtml(weapon.malfunction)}"></td>
            </tr>
        `;
    }

    function createEmptyWeapon(): COC7Weapon {
        return {
            name: "",
            skill: "",
            skillKey: "",
            specialtyKey: "",
            damage: "",
            range: "",
            impale: null,
            attacks: "",
            ammo: "",
            malfunction: "",
            weight: "",
            note: ""
        };
    }

    function weaponSkillOptions(selectedWeapon: COC7Weapon): string {
        const options = getWeaponSkillChoices();
        const selectedValue = weaponSkillValue(selectedWeapon);
        return `<option value="">-</option>` + options.map((option) => {
            const selected = option.value === selectedValue || (!selectedValue && option.label === selectedWeapon.skill);
            return `<option value="${escapeHtml(option.value)}" ${selected ? "selected" : ""}>${escapeHtml(option.label)}</option>`;
        }).join("");
    }

    function getWeaponSkillChoices(): Array<{ value: string; label: string; skillKey: string; specialtyKey: string }> {
        const choices: Array<{ value: string; label: string; skillKey: string; specialtyKey: string }> = [];
        SKILL_CATALOG.forEach((catalog) => {
            if (!isWeaponSkillKey(catalog.key)) return;
            if (catalog.specialties.length) {
                catalog.specialties.forEach((specialty) => {
                    choices.push({
                        value: `${catalog.key}|${specialty.key}`,
                        label: formatWeaponSkillChoiceLabel(catalog.key, specialty.key),
                        skillKey: catalog.key,
                        specialtyKey: specialty.key
                    });
                });
                return;
            }
            choices.push({
                value: `${catalog.key}|`,
                label: skillNameById(catalog.key),
                skillKey: catalog.key,
                specialtyKey: ""
            });
        });
        const fixed = [
            { value: "throw|", label: skillNameById("throw"), skillKey: "throw", specialtyKey: "" },
            { value: "demolitions|", label: skillNameById("demolitions"), skillKey: "demolitions", specialtyKey: "" },
            { value: "artillery|", label: skillNameById("artillery"), skillKey: "artillery", specialtyKey: "" }
        ];
        return dedupeWeaponSkillChoices([...choices, ...fixed]);
    }

    function isWeaponSkillKey(skillKey: string): boolean {
        return ["fighting", "firearms", "throw", "demolitions", "artillery"].includes(skillKey);
    }

    function dedupeWeaponSkillChoices(choices: Array<{ value: string; label: string; skillKey: string; specialtyKey: string }>): Array<{ value: string; label: string; skillKey: string; specialtyKey: string }> {
        const seen = new Set<string>();
        return choices.filter((choice) => {
            if (seen.has(choice.value)) return false;
            seen.add(choice.value);
            return true;
        });
    }

    function formatWeaponSkillLabel(skill: COC7Skill): string {
        return formatWeaponSkillChoiceLabel(resolveSkillKey(skill), "");
    }

    function formatWeaponSkillChoiceLabel(skillKey: string, specialtyKey: string): string {
        const baseName = skillNameById(skillKey);
        const specialtyName = specialtyKey ? localizeSkillSpecialty(skillKey, specialtyKey) : "";
        return specialtyName ? `${baseName}(${specialtyName})` : baseName;
    }

    function weaponSkillValue(weapon: Pick<COC7Weapon, "skillKey" | "specialtyKey" | "skill">): string {
        if (weapon.skillKey) return `${weapon.skillKey}|${weapon.specialtyKey || ""}`;
        const matched = getWeaponSkillChoices().find((choice) => choice.label === weapon.skill);
        return matched?.value || "";
    }

    function handleWeaponTableInput(): void {
        refreshWeaponSuccessRates();
    }

    function handleWeaponTableClick(event: Event): void {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-weapon-picker-trigger]");
        if (!button) return;
        openWeaponPicker(button.dataset.weaponPickerTrigger || "");
    }

    function openWeaponPicker(rowId: string): void {
        pendingWeaponPickerTarget = rowId;
        setInputValue("weaponPickerTargetRow", rowId);
        renderWeaponCatalog();
        weaponPickerModal?.show();
    }

    function renderWeaponCatalog(): void {
        const container = byId("characterWeaponCatalogList");
        if (!container) return;
        container.innerHTML = WEAPON_CATALOG.map((weapon) => `
            <article class="character-weapon-catalog-card">
                <div>
                    <strong>${escapeHtml(weapon.name)}</strong>
                    <span>${escapeHtml(weapon.skill.label || formatWeaponCatalogSkillLabel(weapon))}</span>
                </div>
                <small>${escapeHtml(weapon.damage)} · ${escapeHtml(weapon.range)} · ${weapon.impale ? "贯穿" : "非贯穿"}</small>
                <small>次数 ${escapeHtml(weapon.attacks)} · 装弹 ${escapeHtml(weapon.ammo)} · 故障 ${escapeHtml(weapon.malfunction)}</small>
                <small>年代 ${escapeHtml(weapon.eras.join("、") || "不限")} · 价格 ${escapeHtml(weapon.price)}</small>
                <button type="button" class="btn btn-sm btn-primary" data-equip-weapon="${escapeHtml(weapon.id)}">装备</button>
            </article>
        `).join("") || `<div class="background-note">暂无预设武器</div>`;
    }

    function formatWeaponCatalogSkillLabel(weapon: WeaponCatalogPayload): string {
        const baseName = skillNameById(weapon.skill.skillKey || "");
        const specialty = weapon.skill.skillKey && weapon.skill.specialtyKey
            ? localizeSkillSpecialty(weapon.skill.skillKey, weapon.skill.specialtyKey)
            : "";
        return specialty ? `${baseName}(${specialty})` : baseName;
    }

    function handleWeaponCatalogClick(event: Event): void {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-equip-weapon]");
        if (!button) return;
        const preset = WEAPON_CATALOG.find((weapon) => weapon.id === button.dataset.equipWeapon);
        if (!preset) return;
        applyWeaponToRow(presetWeaponToCharacterWeapon(preset));
        weaponPickerModal?.hide();
    }

    function presetWeaponToCharacterWeapon(preset: WeaponCatalogPayload): COC7Weapon {
        return {
            name: preset.name,
            skill: preset.skill.label || formatWeaponCatalogSkillLabel(preset),
            skillKey: preset.skill.skillKey || "",
            specialtyKey: preset.skill.specialtyKey || "",
            damage: preset.damage,
            range: preset.range,
            impale: preset.impale,
            attacks: preset.attacks,
            ammo: preset.ammo,
            malfunction: preset.malfunction,
            weight: "",
            note: ""
        };
    }

    function applyWeaponToRow(weapon: COC7Weapon): void {
        const row = findWeaponRow(pendingWeaponPickerTarget || getInputValue("weaponPickerTargetRow"));
        if (!row) return;
        const rows = readWeaponRows();
        const rowIndex = Array.from(row.parentElement?.children || []).indexOf(row);
        rows[rowIndex] = weapon;
        renderWeaponTable(rows);
    }

    function createCustomWeaponRow(): void {
        applyWeaponToRow(createEmptyWeapon());
        weaponPickerModal?.hide();
    }

    function removeCurrentWeaponRow(): void {
        const row = findWeaponRow(pendingWeaponPickerTarget || getInputValue("weaponPickerTargetRow"));
        if (!row) return;
        const rows = readWeaponRows();
        const rowIndex = Array.from(row.parentElement?.children || []).indexOf(row);
        rows[rowIndex] = createEmptyWeapon();
        renderWeaponTable(rows);
        weaponPickerModal?.hide();
    }

    function findWeaponRow(rowId: string): HTMLTableRowElement | null {
        const body = byId("characterWeaponTableBody");
        if (!body || !rowId) return null;
        return body.querySelector<HTMLTableRowElement>(`tr[data-weapon-row-id="${CSS.escape(rowId)}"]`);
    }

    function readWeaponRows(): COC7Weapon[] {
        const body = byId("characterWeaponTableBody");
        if (!body) return [];
        return Array.from(body.querySelectorAll<HTMLTableRowElement>("tr[data-weapon-row-id]")).map((row) => {
            const name = String(row.querySelector<HTMLInputElement>("[data-weapon-name]")?.value || "").trim();
            if (!name || ["选择武器", "未选择武器", "未命名武器"].includes(name)) return createEmptyWeapon();
            const select = row.querySelector<HTMLSelectElement>("[data-weapon-skill]");
            const selected = parseWeaponSkillSelectValue(select?.value || "");
            return {
                name,
                skill: select?.selectedOptions[0]?.textContent?.trim() || "",
                skillKey: selected.skillKey,
                specialtyKey: selected.specialtyKey,
                damage: readWeaponField(row, "[data-weapon-damage]", ""),
                range: readWeaponField(row, "[data-weapon-range]", ""),
                impale: readWeaponImpale(row),
                attacks: readWeaponField(row, "[data-weapon-attacks]", ""),
                ammo: readWeaponField(row, "[data-weapon-ammo]", ""),
                malfunction: readWeaponField(row, "[data-weapon-malfunction]", ""),
                weight: "",
                note: ""
            };
        });
    }

    function readWeaponImpale(row: HTMLTableRowElement): boolean | null {
        const value = row.querySelector<HTMLSelectElement>("[data-weapon-impale]")?.value;
        if (value === "true") return true;
        if (value === "false") return false;
        return null;
    }

    function readWeaponField(row: HTMLTableRowElement, selector: string, fallback: string): string {
        const value = row.querySelector<HTMLInputElement>(selector)?.value.trim();
        return value || fallback;
    }

    function parseWeaponSkillSelectValue(value: string): { skillKey: string; specialtyKey: string } {
        const [skillKey, specialtyKey = ""] = value.split("|");
        return { skillKey: skillKey || "", specialtyKey };
    }

    function refreshWeaponSuccessRates(): void {
        const body = byId("characterWeaponTableBody");
        if (!body) return;
        body.querySelectorAll<HTMLTableRowElement>("tr[data-weapon-row-id]").forEach((row) => {
            const rowId = row.dataset.weaponRowId || "";
            const select = row.querySelector<HTMLSelectElement>("[data-weapon-skill]");
            const selected = parseWeaponSkillSelectValue(select?.value || "");
            const success = findSkillSuccessByWeaponSkill({
                skill: select?.selectedOptions[0]?.textContent?.trim() || "",
                skillKey: selected.skillKey,
                specialtyKey: selected.specialtyKey
            });
            const output = row.querySelector<HTMLElement>(`[data-weapon-success="${CSS.escape(rowId)}"]`);
            if (output) output.textContent = String(success);
        });
    }

    function findSkillSuccessByWeaponSkill(weapon: Pick<COC7Weapon, "skill" | "skillKey" | "specialtyKey">): number | "" {
        if (!weapon.skillKey) return "";
        const skills = readChecklistSkills();
        const exact = skills.find((skill) => resolveSkillKey(skill) === weapon.skillKey);
        if (exact) return exact.value;
        const byLabel = skills.find((skill) => formatWeaponSkillLabel(skill) === weapon.skill || skill.name === weapon.skill);
        if (byLabel) return byLabel.value;
        return weapon.skillKey ? weaponSkillBaseFallback(weapon.skillKey) : 0;
    }

    function weaponSkillBaseFallback(skillKey: string): number {
        if (skillKey === "fighting") return 1;
        const catalog = SKILL_CATALOG.find((entry) => entry.key === skillKey);
        return catalog ? catalog.base : 0;
    }

    function openEditor(card?: COC7CharacterCard): void {
        if (card?.playerId && !isCurrentUserElevated() && !isBoundToCurrentPlayer(card.playerId)) {
            notify("该角色卡已绑定其他玩家，当前玩家不能编辑。", "error");
            return;
        }
        const target = card ? cloneCard(card) : createCharacterCard();
        setInputValue("characterEditingId", card?.id || "");
        setInputValue("characterName", target.name);
        const boundPlayerId = target.playerId && target.playerId !== PLAYER_UNBOUND_LABEL
            ? target.playerId
            : (isCurrentUserElevated() ? "" : currentPlayerId());
        setPlayerBindingInputValue(boundPlayerId);
        setInputValue("characterEra", target.era);
        setInputValue("characterOccupation", target.occupationName || getOccupation(target).name);
        setInputValue("characterCreditRating", target.creditRating || 0);
        setInputValue("characterAge", target.age);
        setInputValue("characterGender", target.gender);
        setInputValue("raceType", target.background.raceType);
        setInputValue("characterResidence", target.residence);
        setInputValue("characterBirthplace", target.birthplace);
        ATTRIBUTE_KEYS.forEach((key) => setInputValue(`attribute${key}`, target.attributes[key]));
        setInputValue("appearance", target.background.appearance);
        setInputValue("education", target.background.education);
        setInputValue("characterBio", target.background.story);
        setInputValue("traits", target.background.traits);
        setInputValue("characterArcaneTomes", target.background.arcaneTomes);
        setInputValue("characterSpells", target.background.spells);
        setInputValue("characterEncounters", target.background.encounters);
        setInputValue("characterIdeology", target.background.ideology);
        setInputValue("characterSignificantPeople", target.background.significantPeople);
        setInputValue("characterMeaningfulLocations", target.background.meaningfulLocations);
        setInputValue("characterTreasuredPossessions", target.background.treasuredPossessions);
        setInputValue("characterInjuriesScars", target.background.injuriesScars);
        setInputValue("characterPhobiasManias", target.background.phobiasManias);
        hydrateRelationshipRows(target.relationships);
        hydrateExperiencedScenarioRows(target.experiencedScenarios);
        setInputValue("characterEquipment", formatEquipment(target.equipment));
        setInputValue("characterCash", target.assets.cash);
        setInputValue("characterSpendingLevel", target.assets.spendingLevel);
        setInputValue("characterAssets", target.assets.assetsText);
        setInputValue("characterCurrentHp", target.currentHp);
        setInputValue("characterMaxHp", target.maxHp);
        setInputValue("characterCurrentMp", target.currentMp);
        setInputValue("characterMaxMp", target.maxMp);
        setInputValue("characterCurrentSan", target.currentSan);
        setInputValue("characterInitialSan", target.initialSan);
        setInputValue("characterMaxSan", target.maxSan);
        setInputValue("characterOccupationSkillPoints", target.occupationSkillPoints);
        setInputValue("characterPersonalInterestPoints", target.personalInterestPoints);
        setInputValue("characterOccupationSkillLimit", target.skillSuccessLimits.occupation);
        setInputValue("characterOtherSkillLimit", target.skillSuccessLimits.other);
        setInputValue("characterDamageBonus", target.damageBonus);
        setInputValue("characterBuild", target.build);
        setInputValue("characterArmor", target.armor);
        setInputValue("characterMov", target.mov);
        editorSkills = target.skills;
        occupationSkillPointsManuallyEdited = target.occupationSkillPoints !== calculateOccupationSkillPoints(target.attributes, target.occupationId);
        personalInterestPointsManuallyEdited = target.personalInterestPoints !== calculatePersonalInterestPoints(target.attributes);
        combatStatsManuallyEdited = false;
        setEditorStatus(target.status);
        updateUnbindButton(boundPlayerId);
        updateAvatarPreview(target.avatar);
        hydrateSkillChecklist(editorSkills);
        hydrateWeaponTable(target.weapons);
        refreshEditorRuleSummary();
        syncEditorCreditRating();
        renderOccupationHint();
        modal?.show();
    }

    function openNameGenerator(): void {
        const gender = normalizeNameGender(getInputValue("characterGender"));
        setInputValue("nameGenderSelect", gender);
        regenerateNamePreview();
        nameGeneratorModal?.show();
    }

    function regenerateNamePreview(): void {
        const region = (getInputValue("nameRegionSelect") || "china") as NameRegion;
        const gender = (getInputValue("nameGenderSelect") || "unknown") as InvestigatorGender;
        pendingGeneratedName = generateRegionalName(region, gender);
        setText("generatedNamePreview", pendingGeneratedName);
    }

    function confirmGeneratedName(): void {
        if (pendingGeneratedName) setInputValue("characterName", pendingGeneratedName);
        nameGeneratorModal?.hide();
    }

    function occupationDisplayName(occupation: COC7Occupation): string {
        return localizeOccupationName(occupation.nameKey, occupation.name) || occupation.name;
    }

    function occupationCategorySlug(categoryKey: string): string {
        return categoryKey.split(".").pop() || categoryKey;
    }

    function occupationCategorySortIndex(categoryKey: string): number {
        const index = OCCUPATION_CATEGORY_ORDER.indexOf(occupationCategorySlug(categoryKey));
        return index < 0 ? OCCUPATION_CATEGORY_ORDER.length : index;
    }

    function occupationCategoryGroups(): Array<{ key: string; label: string; occupations: COC7Occupation[] }> {
        const groups = new Map<string, COC7Occupation[]>();
        PRESET_OCCUPATIONS.forEach((occupation) => {
            const key = occupation.categoryKey || occupation.category || "other";
            const list = groups.get(key) || [];
            list.push(occupation);
            groups.set(key, list);
        });
        return Array.from(groups.entries()).map(([key, list]) => ({
            key,
            label: localizeOccupationCategory(key, list[0]?.category || key),
            occupations: [...list].sort((a, b) => (a.order || 0) - (b.order || 0) || a.name.localeCompare(b.name))
        })).sort((a, b) => occupationCategorySortIndex(a.key) - occupationCategorySortIndex(b.key));
    }

    function occupationSkillSummary(occupation: COC7Occupation): string {
        // 延迟到渲染时求值：此时技能目录已加载，技能名可正确本地化。
        const labels = occupation.occupationSkillEntries?.length
            ? occupation.occupationSkillEntries.map(occupationSkillEntryLabel)
            : occupation.occupationSkills.map((skillKey) => formatSkillLabel(skillKey));
        return labels.join(localizeCatalogText("、"));
    }

    function renderOccupationTemplateCard(occupation: COC7Occupation): string {
        const t = (key: string, fallback: string) => window.TrpgI18n?.t(key, fallback) || fallback;
        const [minCredit, maxCredit] = occupation.creditRating;
        return `
            <article class="occupation-template-card">
                <div class="occupation-template-card__head">
                    <strong class="occupation-template-card__name">${escapeHtml(occupationDisplayName(occupation))}</strong>
                    <button type="button" class="occupation-template-card__apply" data-occupation-id="${escapeHtml(occupation.id)}">${escapeHtml(t("occupation.card.apply", "就职"))}</button>
                </div>
                <p class="occupation-template-card__row"><span class="occupation-template-card__label">${escapeHtml(t("occupation.card.occupation_skills", "本职技能"))}</span>${escapeHtml(occupationSkillSummary(occupation))}</p>
                <p class="occupation-template-card__row"><span class="occupation-template-card__label">${escapeHtml(t("occupation.card.credit_rating", "信用评级"))}</span>${minCredit}~${maxCredit}</p>
                <p class="occupation-template-card__row"><span class="occupation-template-card__label">${escapeHtml(t("occupation.card.skill_points", "职业点数"))}</span>${escapeHtml(formatOccupationPointFormula(occupation.pointsFormula))}</p>
            </article>
        `;
    }

    function toggleOccupationCategory(header: HTMLButtonElement): void {
        const grid = header.nextElementSibling;
        if (!(grid instanceof HTMLElement)) return;
        const willOpen = grid.hidden;
        grid.hidden = !willOpen;
        header.classList.toggle("is-open", willOpen);
        header.setAttribute("aria-expanded", willOpen ? "true" : "false");
    }

    function openOccupationTemplatePicker(): void {
        const container = byId("occupationTemplateList");
        if (!container) return;
        container.innerHTML = occupationCategoryGroups().map((group) => `
            <section class="occupation-category">
                <button type="button" class="occupation-category-header" aria-expanded="false">
                    <span class="occupation-category-name">${escapeHtml(group.label)}</span>
                    <span class="occupation-category-count">${group.occupations.length}</span>
                    <i class="fa fa-chevron-right occupation-category-arrow" aria-hidden="true"></i>
                </button>
                <div class="occupation-category-grid" hidden>
                    ${group.occupations.map(renderOccupationTemplateCard).join("")}
                </div>
            </section>
        `).join("");
        container.querySelectorAll<HTMLButtonElement>(".occupation-category-header").forEach((header) => {
            header.addEventListener("click", () => toggleOccupationCategory(header));
        });
        container.querySelectorAll<HTMLButtonElement>("[data-occupation-id]").forEach((button) => {
            button.addEventListener("click", () => applyOccupationTemplate(button.dataset.occupationId || ""));
        });
        occupationTemplateModal?.show();
    }

    /** 就职：写职业名 → 自动勾选确定本职技能 → 写专精 → 生成职业点数 → 刷新提示行。 */
    function applyOccupationTemplate(occupationId: string): void {
        const occupation = getOccupationById(occupationId);
        if (!occupation.id) return;
        setInputValue("characterOccupation", occupationDisplayName(occupation));
        occupationSkillPointsManuallyEdited = false;
        // 清空所有打勾与专精，随后由 normalizeSkills 按「确定本职技能」重新勾选。
        const cleared = readChecklistSkills().map((skill) => {
            const next: COC7Skill = { ...skill, checked: false, occupation: false, isProfessional: false };
            delete next.specialtyKey;
            return next;
        });
        activeSkillCategoryFilter = "全部技能";
        document.querySelectorAll<HTMLElement>("#characterSkillCategoryFilters [data-skill-category]").forEach((item) => {
            item.classList.toggle("is-active", item.dataset.skillCategory === activeSkillCategoryFilter);
        });
        editorSkills = normalizeSkills(mergeSkillCatalog(cleared), readAttributes(), occupation);
        hydrateSkillChecklist(editorSkills);
        // 带专精的确定条目：复用专精接口写入并重算基础值（模糊条目不打勾、不写专精）。
        (occupation.occupationSkillEntries || [])
            .filter((entry) => entry.skillKey && entry.specialtyKey)
            .forEach((entry) => {
                const row = editorSkills.find((skill) => resolveSkillKey(skill) === entry.skillKey);
                if (row) applySkillSpecialty(row.id, entry.specialtyKey || "");
            });
        refreshEditorRuleSummary();
        renderOccupationHint();
        occupationTemplateModal?.hide();
    }

    /** 技能列表上方的一行提示：职业名 / 信用评级 / 全部本职技能（含模糊表述）。 */
    function renderOccupationHint(): void {
        const hint = byId("characterOccupationHint");
        if (!hint) return;
        const occupation = resolveOccupationFromInput(getInputValue("characterOccupation"));
        if (!occupation.id) {
            hint.hidden = true;
            hint.innerHTML = "";
            return;
        }
        const t = (key: string, fallback: string) => window.TrpgI18n?.t(key, fallback) || fallback;
        const separator = `<span class="character-occupation-hint__sep">|</span>`;
        hint.innerHTML = [
            `<strong class="character-occupation-hint__title">${escapeHtml(t("occupation.hint.title", "【提示】"))}</strong>`,
            `<span>${escapeHtml(t("occupation.hint.occupation", "职业名"))}: ${escapeHtml(occupationDisplayName(occupation))}</span>`,
            separator,
            `<span>${escapeHtml(t("occupation.hint.credit_rating", "信用评级"))}: ${occupation.creditRating[0]}~${occupation.creditRating[1]}</span>`,
            separator,
            `<span>${escapeHtml(t("occupation.hint.skills", "本职技能"))}: ${escapeHtml(occupationSkillSummary(occupation))}</span>`
        ].join("");
        hint.hidden = false;
    }

    function hydrateRuleSettingsPanel(): void {
        const settings = loadRuleSettings();
        setInputValue("attributeRatioPercent", settings.attributeRatioPercent);
        setInputValue("maxCardsPerUser", settings.maxCardsPerUser);
        setInputValue("weaponSlotCount", settings.weaponSlotCount);
        setCheckboxValue("allowSkillBaseEdit", settings.allowSkillBaseEdit);
        ATTRIBUTE_KEYS.forEach((key) => setInputValue(`attributeRoll${key}`, settings.attributeRolls[key]));
        setText("characterRuleSettingsMessage", "");
    }

    async function saveRuleSettingsFromPanel(): Promise<void> {
        const nextSettings = normalizeRuleSettings({
            attributeRatioPercent: Number(getInputValue("attributeRatioPercent")),
            maxCardsPerUser: Number(getInputValue("maxCardsPerUser")),
            weaponSlotCount: Number(getInputValue("weaponSlotCount")),
            allowSkillBaseEdit: getCheckboxValue("allowSkillBaseEdit"),
            attributeRolls: ATTRIBUTE_KEYS.reduce((rolls, key) => {
                rolls[key] = getInputValue(`attributeRoll${key}`) || DEFAULT_ATTRIBUTE_ROLLS[key];
                return rolls;
            }, {} as AttributeRollFormulaMap)
        });
        persistRuleSettings(nextSettings);
        const generalConfig = global.configManager?.getConfig("general") || {};
        const characterRules = {
            ...(getConfigSection("character_rules") || {}),
            attribute_ratio_percent: nextSettings.attributeRatioPercent,
            max_cards_per_user: nextSettings.maxCardsPerUser,
            weapon_slot_count: nextSettings.weaponSlotCount,
            allow_skill_base_edit: nextSettings.allowSkillBaseEdit,
            ...ATTRIBUTE_KEYS.reduce((rolls, key) => {
                rolls[`attribute_roll_${key.toLowerCase()}`] = nextSettings.attributeRolls[key];
                return rolls;
            }, {} as Record<string, string>)
        };
        const saved = await global.configManager?.saveConfig("general", {
            ...generalConfig,
            character_rules: characterRules
        });
        setText("characterRuleSettingsMessage", saved === false ? "角色卡规则保存失败" : "角色卡规则已保存");
        refreshEditorRuleSummary();
    }

    function skillNameById(skillId: string): string {
        const base = BASE_SKILLS.find((skill) => skill.id === skillId || (skill.skillKey || skill.id) === skillId);
        if (base) return base.name;
        const catalog = SKILL_CATALOG.find((entry) => entry.key === skillId);
        return catalog ? SKILL_LOCALE_MAP[catalog.labelKey] || skillId : skillId;
    }

    function skillKeyByName(name: string, fallback = ""): string {
        const text = String(name || "").trim();
        if (!text) return fallback;
        const exact = SKILL_KEY_BY_LABEL[text];
        if (exact) return exact;
        // Legacy exports store specialties in the display name, e.g.
        // "格斗(斗殴)" or "母语:汉语". Resolve those to the catalog's base key.
        const baseName = text.replace(/[(:：].*$/, "").trim();
        if (baseName && SKILL_KEY_BY_LABEL[baseName]) return SKILL_KEY_BY_LABEL[baseName];
        const prefixMatch = Object.entries(SKILL_KEY_BY_LABEL).find(([label]) => Boolean(label) && (
            text.startsWith(`${label}(`) || text.startsWith(`${label}:`) || text.startsWith(`${label}：`)
        ));
        return prefixMatch?.[1] || fallback || text;
    }

    function skillCategoryByKey(skillKey: string): SkillCategory {
        const catalog = SKILL_CATALOG.find((entry) => entry.key === skillKey);
        return catalog?.category || "其他";
    }

    function skillBaseByKey(skillKey: string, fallbackGroup = ""): number {
        const catalog = SKILL_CATALOG.find((entry) => entry.key === skillKey);
        if (catalog) return catalog.base;
        const group = TEST_CHARACTER_SKILL_GROUPS[fallbackGroup];
        return typeof group === "string" ? 0 : 0;
    }

    function formatOccupationPointFormula(formula: OccupationPointFormula): string {
        return formula.map((term) => {
            if (typeof term === "string") return term;
            // 「或」项：按候选属性展示为「DEX或POW * 2」。
            if (term.choose?.length) return `${term.choose.join(localizeCatalogText("或"))} * ${term.multiplier}`;
            return `${term.attribute} * ${term.multiplier}`;
        }).join(" + ");
    }

    function handleAvatarUpload(event: Event): void {
        const input = event.target as HTMLInputElement | null;
        const file = input?.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            input.dataset.avatar = String(reader.result || "");
            updateAvatarPreview(input.dataset.avatar);
        };
        reader.readAsDataURL(file);
    }

    function updateAvatarPreview(src: string): void {
        const image = byId<HTMLImageElement>("characterAvatarPreviewImage");
        if (!image) return;
        // 未设置头像时回退为「角色名首字」文字头像，项目已不再有内置默认头像图。
        const nameField = byId<HTMLInputElement>("characterName");
        image.src = TrpgAvatar.resolveAvatarSrc(nameField?.value || "?", src);
    }

    function setInputValue(id: string, value: unknown): void {
        const field = byId<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(id);
        if (field) field.value = String(value ?? "");
    }

    function setCheckboxValue(id: string, checked: boolean): void {
        const field = byId<HTMLInputElement>(id);
        if (field) field.checked = checked;
    }

    function setText(id: string, value: string): void {
        const element = byId(id);
        if (element) element.textContent = value;
    }

    function setButtonBusy(id: string, busy: boolean): void {
        const button = byId<HTMLButtonElement>(id);
        if (!button) return;
        button.disabled = busy;
        button.dataset.busy = busy ? "true" : "false";
    }

    function getInputValue(id: string): string {
        return byId<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(id)?.value.trim() || "";
    }

    function getCheckboxValue(id: string): boolean {
        return byId<HTMLInputElement>(id)?.checked || false;
    }

    function readAttributes(): COC7Attributes {
        const coreAttributes = Object.fromEntries(ATTRIBUTE_KEYS.map((key) => [key, Number(getInputValue(`attribute${key}`))])) as LegacyAttributesInput;
        coreAttributes.AGE = Number(getInputValue("characterAge"));
        return normalizeAttributes(coreAttributes);
    }

    function setEditorStatus(status: CharacterStatusFlags): void {
        (Object.keys(STATUS_FIELD_IDS) as Array<keyof CharacterStatusFlags>).forEach((key) => {
            setCheckboxValue(STATUS_FIELD_IDS[key], status[key]);
        });
    }

    function readEditorStatus(): CharacterStatusFlags {
        return (Object.keys(STATUS_FIELD_IDS) as Array<keyof CharacterStatusFlags>).reduce((status, key) => {
            status[key] = getCheckboxValue(STATUS_FIELD_IDS[key]);
            return status;
        }, normalizeStatus());
    }

    function updateUnbindButton(playerId: string): void {
        const button = byId<HTMLButtonElement>("unbindCharacterPlayer");
        if (!button) return;
        button.hidden = !isCurrentUserElevated() || !playerId || playerId === PLAYER_UNBOUND_LABEL;
    }

    function unbindCharacterPlayerFromEditor(): void {
        if (!isCurrentUserElevated()) {
            notify("只有管理员可以解绑角色卡玩家。", "error");
            return;
        }
        setPlayerBindingInputValue("");
    }

    function syncAttributeDerivedFields(attributes: COC7Attributes = readAttributes()): void {
        const ratioPercent = loadRuleSettings().attributeRatioPercent;
        ATTRIBUTE_KEYS.forEach((key) => {
            const derived = calculateAttributeDisplayValues(attributes[key], ratioPercent);
            setInputValue(`attribute${key}Half`, derived.half);
            setInputValue(`attribute${key}Ratio`, derived.ratio);
        });
    }

    function syncEditorResourceLimits(attributes: COC7Attributes = readAttributes()): void {
        const maxHp = calculateMaxHp(attributes);
        const maxMp = calculateMaxMp(attributes);
        const maxSan = calculateMaxSan(attributes);
        setInputValue("characterMaxHp", maxHp);
        setInputValue("characterMaxMp", maxMp);
        setInputValue("characterMaxSan", maxSan);
        setInputValue("characterCurrentHp", clampNumber(getInputValue("characterCurrentHp"), 0, maxHp, maxHp));
        setInputValue("characterCurrentMp", clampNumber(getInputValue("characterCurrentMp"), 0, maxMp, maxMp));
        setInputValue("characterCurrentSan", clampNumber(getInputValue("characterCurrentSan"), 0, maxSan, maxSan));
        setInputValue("characterInitialSan", clampNumber(getInputValue("characterInitialSan"), 0, maxSan, maxSan));
    }

    function syncEditorCombatStats(attributes: COC7Attributes = readAttributes(), force = false): void {
        if (combatStatsManuallyEdited && !force) return;
        const damage = calculateBuildAndDamageBonus(attributes);
        setInputValue("characterDamageBonus", damage.damageBonus);
        setInputValue("characterBuild", damage.build);
        setInputValue("characterMov", calculateMov(attributes));
        if (!getInputValue("characterArmor")) setInputValue("characterArmor", 0);
    }

    function updateAttributeRollHints(): void {
        const settings = loadRuleSettings();
        document.querySelectorAll<HTMLElement>("[data-attribute-roll]").forEach((element) => {
            const key = element.dataset.attributeRoll as COC7CoreAttributeKey | undefined;
            if (key && ATTRIBUTE_KEYS.includes(key)) element.textContent = settings.attributeRolls[key];
        });
    }

    function readChecklistSkills(): COC7Skill[] {
        const body = byId("characterSkillTableBody");
        if (!body) return editorSkills.length ? editorSkills : normalizeSkills(undefined, readAttributes(), resolveOccupationFromInput(getInputValue("characterOccupation")));
        const rows = Array.from(body.querySelectorAll<HTMLTableRowElement>("tr[data-skill-row-id]"));
        if (!rows.length) return normalizeSkills(editorSkills.length ? editorSkills : undefined, readAttributes(), resolveOccupationFromInput(getInputValue("characterOccupation")));
        return rows.map((row) => {
            const rowId = row.dataset.skillRowId || "";
            const skillKey = row.dataset.skillKey || rowId.split("__")[0] || rowId;
            const baseSkill = BASE_SKILLS.find((skill) => skill.id === rowId) || BASE_SKILLS.find((skill) => (skill.skillKey || skill.id) === skillKey);
            const occupation = row.querySelector<HTMLInputElement>("[data-skill-occupation-checkbox]")?.checked || false;
            const base = readSkillRowNumber(row, "base", 0);
            const occupationPoints = readSkillRowNumber(row, "occupationPoints", 0);
            const interestPoints = readSkillRowNumber(row, "interestPoints", 0);
            const growthPoints = readSkillRowNumber(row, "growthPoints", 0);
            const value = clampNumber(base + occupationPoints + interestPoints + growthPoints, 0, 99, base);
            const name = isCustomSkill(skillKey) ? readCustomSkillName(row) || skillNameById(skillKey) : baseSkill?.name || skillNameById(skillKey);
            const result: COC7Skill = {
                ...(baseSkill || {}),
                id: rowId,
                skillKey,
                name,
                base,
                value,
                category: row.dataset.skillCategory || baseSkill?.category || "其他",
                checked: occupation,
                occupation,
                isProfessional: occupation,
                occupationPoints,
                interestPoints,
                growthPoints
            };
            const specialtyKey = String(editorSkills.find((item) => item.id === rowId)?.specialtyKey || "").trim();
            if (specialtyKey) result.specialtyKey = specialtyKey;
            if (isUserDefinedBaseSkill(skillKey)) result.customBase = true;
            return result;
        });
    }

    function readCustomSkillName(row: HTMLTableRowElement): string {
        return String(row.querySelector<HTMLInputElement>("[data-custom-skill-name]")?.value || "").trim().slice(0, 40);
    }

    function syncCustomSkillDisplayName(row: HTMLTableRowElement): void {
        const skillKey = row.dataset.skillKey || "";
        if (!isCustomSkill(skillKey)) return;
        const display = row.querySelector<HTMLElement>("[data-skill-display-name]");
        if (display) display.textContent = readCustomSkillName(row) || skillNameById(skillKey);
    }

    function readSkillRowNumber(row: HTMLTableRowElement, kind: "base" | "occupationPoints" | "interestPoints" | "growthPoints", fallback: number): number {
        const selectors = {
            base: "[data-skill-base]",
            occupationPoints: "[data-skill-occupation-points]",
            interestPoints: "[data-skill-interest-points]",
            growthPoints: "[data-skill-growth-points]"
        };
        const element = row.querySelector<HTMLInputElement | HTMLElement>(selectors[kind]);
        const value = element instanceof HTMLInputElement ? element.value : element?.textContent;
        return clampNumber(value, 0, 99, fallback);
    }

    function refreshSkillTableVisibility(): void {
        const body = byId("characterSkillTableBody");
        if (!body) return;
        body.querySelectorAll<HTMLTableRowElement>("tr[data-skill-row-id]").forEach((row) => {
            const category = row.dataset.skillCategory || "其他";
            row.hidden = activeSkillCategoryFilter !== "全部技能" && category !== activeSkillCategoryFilter;
        });
    }

    function refreshSkillTableCalculations(): void {
        const body = byId("characterSkillTableBody");
        if (!body) return;
        body.querySelectorAll<HTMLTableRowElement>("tr[data-skill-row-id]").forEach((row) => refreshSkillRowCalculations(row));
        refreshSkillPointSummary();
        refreshWeaponSuccessRates();
        syncEditorCreditRating();
    }

    // 技能上限输入框正在输入（input 事件）时使用：只根据每个技能自身的数值重算派生阈值，
    // 不使用尚未输入完整的中间上限去裁剪各技能点数，避免“输入 9 后所有技能上限都变成 9 且无法恢复”。
    function refreshSkillTableThresholds(): void {
        const body = byId("characterSkillTableBody");
        if (!body) return;
        body.querySelectorAll<HTMLTableRowElement>("tr[data-skill-row-id]").forEach((row) => refreshSkillRowCalculations(row, false));
        refreshSkillPointSummary();
        refreshWeaponSuccessRates();
        syncEditorCreditRating();
    }

    function refreshSkillRowCalculations(row: HTMLTableRowElement, applyLimitCaps = true): void {
        const occupationCheckbox = row.querySelector<HTMLInputElement>("[data-skill-occupation-checkbox]");
        const occupationInput = row.querySelector<HTMLInputElement>("[data-skill-occupation-points]");
        const interestInput = row.querySelector<HTMLInputElement>("[data-skill-interest-points]");
        const growthInput = row.querySelector<HTMLInputElement>("[data-skill-growth-points]");
        const successOutput = row.querySelector<HTMLElement>("[data-skill-success]");
        const hardOutput = row.querySelector<HTMLElement>("[data-skill-hard]");
        const extremeOutput = row.querySelector<HTMLElement>("[data-skill-extreme]");
        const base = readSkillRowNumber(row, "base", 0);
        const isOccupation = Boolean(occupationCheckbox?.checked);
        row.dataset.skillOccupation = isOccupation ? "1" : "0";
        if (occupationInput) {
            occupationInput.disabled = !isOccupation;
            if (!isOccupation && Number(occupationInput.value || 0) > 0) {
                occupationInput.value = "0";
                notify("只有本职技能才能添加点数", "error");
            }
            // 仅在提交（applyLimitCaps）时用上限裁剪点数，输入过程中保留玩家已填写的点数
            if (applyLimitCaps) {
                occupationInput.value = String(clampNumber(occupationInput.value, 0, getSkillLimit("occupation"), 0));
            }
        }
        if (applyLimitCaps && interestInput) interestInput.value = String(clampNumber(interestInput.value, 0, getSkillLimit("other"), 0));
        if (applyLimitCaps && growthInput) growthInput.value = String(clampNumber(growthInput.value, 0, getSkillLimit("other"), 0));
        enforceSkillPointBudgets();
        const occupationPoints = readSkillRowNumber(row, "occupationPoints", 0);
        const interestPoints = readSkillRowNumber(row, "interestPoints", 0);
        const growthPoints = readSkillRowNumber(row, "growthPoints", 0);
        const total = base + occupationPoints + interestPoints + growthPoints;
        // 输入过程中按技能自身数值展示成功率；提交后再套用技能上限裁剪
        const success = applyLimitCaps ? clampNumber(total, 0, getSkillLimit("occupation"), base) : clampNumber(total, 0, 99, base);
        if (successOutput) successOutput.textContent = String(success);
        if (hardOutput) hardOutput.textContent = String(Math.floor(success / 2));
        if (extremeOutput) extremeOutput.textContent = String(Math.floor(success / 5));
        refreshSkillPointSummary();
        syncEditorCreditRating();
    }

    function enforceSkillPointBudgets(): void {
        const body = byId("characterSkillTableBody");
        if (!body) return;
        const occupationTotal = clampNumber(getInputValue("characterOccupationSkillPoints"), 0, 999, 0);
        const interestTotal = clampNumber(getInputValue("characterPersonalInterestPoints"), 0, 999, 0);
        clampColumnToBudget(Array.from(body.querySelectorAll<HTMLInputElement>("[data-skill-occupation-points]")), occupationTotal);
        clampColumnToBudget(Array.from(body.querySelectorAll<HTMLInputElement>("[data-skill-interest-points]")), interestTotal);
    }

    function clampColumnToBudget(inputs: HTMLInputElement[], budget: number): void {
        let total = 0;
        inputs.forEach((input) => {
            const value = clampNumber(input.value, 0, 99, 0);
            const allowed = Math.max(0, budget - total);
            const nextValue = Math.min(value, allowed);
            if (nextValue !== value) input.value = String(nextValue);
            total += nextValue;
        });
    }

    function refreshEditorRuleSummary(): void {
        const attributes = readAttributes();
        setText("attributeBaseTotal", String(calculateAttributeBaseTotal(attributes)));
        syncAttributeDerivedFields(attributes);
        syncEditorResourceLimits(attributes);
        syncEditorCombatStats(attributes);
        syncGeneratedSkillPointInputs(false);
        refreshSkillTableCalculations();
        refreshSkillPointSummary();
        updateAttributeRollHints();
        syncEditorCreditRating();
    }

    function syncGeneratedSkillPointInputs(force: boolean): void {
        if (force || !occupationSkillPointsManuallyEdited) {
            setGeneratedOccupationSkillPoints();
        }
        if (force || !personalInterestPointsManuallyEdited) {
            setGeneratedPersonalInterestPoints();
        }
        refreshSkillPointSummary();
    }

    function setGeneratedOccupationSkillPoints(): void {
        setInputValue("characterOccupationSkillPoints", calculateOccupationSkillPoints(readAttributes(), resolveOccupationIdFromInput(getInputValue("characterOccupation"))));
    }

    function setGeneratedPersonalInterestPoints(): void {
        setInputValue("characterPersonalInterestPoints", calculatePersonalInterestPoints(readAttributes()));
    }

    function refreshSkillPointSummary(): void {
        const occupation = resolveOccupationFromInput(getInputValue("characterOccupation"));
        const spent = calculateEditorSkillPointSpending(occupation);
        const occupationTotal = clampNumber(getInputValue("characterOccupationSkillPoints"), 0, 999, 0);
        const personalTotal = clampNumber(getInputValue("characterPersonalInterestPoints"), 0, 999, 0);
        setText("characterOccupationSkillPointsRemaining", `剩余 ${Math.max(0, occupationTotal - spent.occupation)}`);
        setText("characterPersonalInterestPointsRemaining", `剩余 ${Math.max(0, personalTotal - spent.personal)}`);
    }

    function calculateEditorSkillPointSpending(occupation: COC7Occupation): { occupation: number; personal: number } {
        const body = byId("characterSkillTableBody");
        if (!body) return { occupation: 0, personal: 0 };
        const rows = Array.from(body.querySelectorAll<HTMLTableRowElement>("tr[data-skill-row-id]"));
        return rows.reduce((summary, row) => {
            const occupationInput = row.querySelector<HTMLInputElement>("[data-skill-occupation-points]");
            const interestInput = row.querySelector<HTMLInputElement>("[data-skill-interest-points]");
            summary.occupation += clampNumber(occupationInput?.value, 0, 99, 0);
            summary.personal += clampNumber(interestInput?.value, 0, 99, 0);
            return summary;
        }, { occupation: 0, personal: 0 });
    }

    function resolveEditorPlayerId(existing?: COC7CharacterCard): string {
        const boundDisplayValue = getInputValue("characterBoundPlayer").trim();
        if (isCurrentUserElevated()) {
            if (!boundDisplayValue || boundDisplayValue === PLAYER_UNBOUND_LABEL) return "";
            const matchedUser = assignableUsers.find((user) => {
                const userId = String(user.id);
                return userId === boundDisplayValue || user.username === boundDisplayValue;
            });
            return matchedUser ? String(matchedUser.id) : boundDisplayValue;
        }
        if (existing?.playerId && !isBoundToCurrentPlayer(existing.playerId)) return existing.playerId;
        return currentPlayerId();
    }

    function validatePlayerBinding(cardId: string, playerId: string, existing?: COC7CharacterCard): boolean {
        if (!playerId) return true;
        if (!isCurrentUserElevated() && existing?.playerId && !isBoundToCurrentPlayer(existing.playerId)) {
            notify("该角色卡已绑定其他玩家，当前玩家不能使用。", "error");
            return false;
        }
        if (!isCurrentUserElevated() && existing?.playerId && existing.playerId !== playerId) {
            notify("该角色卡已绑定其他玩家，只有管理员可以解绑后重新绑定。", "error");
            return false;
        }
        return true;
    }

    async function saveFromEditor(): Promise<void> {
        if (characterSaveInFlight) return;
        const editingId = getInputValue("characterEditingId");
        const existing = cards.find((card) => card.id === editingId) || galleryCards.find((card) => card.id === editingId);
        const attributes = readAttributes();
        const maxHp = calculateMaxHp(attributes);
        const maxMp = calculateMaxMp(attributes);
        const maxSan = calculateMaxSan(attributes);
        const nextCardId = editingId || `investigator-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const playerId = resolveEditorPlayerId(existing);
        if (!validatePlayerBinding(nextCardId, playerId, existing)) return;
        const avatarInput = byId<HTMLInputElement>("characterAvatarUpload");
        const age = Number(getInputValue("characterAge") || attributes.AGE);
        attributes.AGE = clampNumber(age, 15, 99, attributes.AGE);
        const occupationName = resolveOccupationNameFromInput(getInputValue("characterOccupation"));
        const cardInput: Partial<COC7CharacterCard> = {
            ...existing,
            id: nextCardId,
            name: getInputValue("characterName") || generateInvestigatorName(),
            playerId,
            era: getInputValue("characterEra") || "1920s",
            gender: getInputValue("characterGender"),
            occupationId: resolveOccupationIdFromInput(occupationName),
            occupationName,
            creditRating: readEditorCreditRating(),
            avatar: avatarInput?.dataset.avatar || existing?.avatar || "",
            residence: getInputValue("characterResidence"),
            birthplace: getInputValue("characterBirthplace"),
            attributes,
            currentHp: clampNumber(getInputValue("characterCurrentHp"), 0, maxHp, maxHp),
            currentMp: clampNumber(getInputValue("characterCurrentMp"), 0, maxMp, maxMp),
            magicPoints: clampNumber(getInputValue("characterCurrentMp"), 0, maxMp, maxMp),
            maxMp,
            initialSan: clampNumber(getInputValue("characterInitialSan"), 0, maxSan, maxSan),
            currentSan: clampNumber(getInputValue("characterCurrentSan"), 0, maxSan, maxSan),
            status: readEditorStatus(),
            occupationSkillPoints: clampNumber(getInputValue("characterOccupationSkillPoints"), 0, 999, calculateOccupationSkillPoints(attributes, resolveOccupationIdFromInput(occupationName))),
            personalInterestPoints: clampNumber(getInputValue("characterPersonalInterestPoints"), 0, 999, calculatePersonalInterestPoints(attributes)),
            skillSuccessLimits: {
                occupation: clampNumber(getInputValue("characterOccupationSkillLimit"), 0, 99, 75),
                other: clampNumber(getInputValue("characterOtherSkillLimit"), 0, 99, 50)
            },
            skills: readChecklistSkills(),
            weapons: readWeaponRows(),
            damageBonus: getInputValue("characterDamageBonus") || calculateBuildAndDamageBonus(attributes).damageBonus,
            build: clampNumber(getInputValue("characterBuild"), -2, 99, calculateBuildAndDamageBonus(attributes).build),
            armor: clampNumber(getInputValue("characterArmor"), 0, 99, 0),
            mov: clampNumber(getInputValue("characterMov"), 0, 99, calculateMov(attributes)),
            equipment: parseEquipment(getInputValue("characterEquipment")),
            assets: readEditorAssets(),
            background: {
                ...normalizeBackground(existing?.background),
                appearance: getInputValue("appearance"),
                education: getInputValue("education"),
                story: getInputValue("characterBio"),
                traits: getInputValue("traits"),
                arcaneTomes: getInputValue("characterArcaneTomes"),
                spells: getInputValue("characterSpells"),
                encounters: getInputValue("characterEncounters"),
                ideology: getInputValue("characterIdeology"),
                significantPeople: getInputValue("characterSignificantPeople"),
                meaningfulLocations: getInputValue("characterMeaningfulLocations"),
                treasuredPossessions: getInputValue("characterTreasuredPossessions"),
                injuriesScars: getInputValue("characterInjuriesScars"),
                phobiasManias: getInputValue("characterPhobiasManias"),
                raceType: getInputValue("raceType") || "人类"
            },
            relationships: readRelationshipRows(),
            experiencedScenarios: readExperiencedScenarioRows()
        };
        const card = createCharacterCard(cardInput);
        if (!validatePlayerBinding(card.id, card.playerId, existing)) return;
        characterSaveInFlight = true;
        setButtonBusy("saveCharacter", true);
        let savedCard: COC7CharacterCard | null = null;
        try {
            savedCard = activeGalleryEditId ? await saveGalleryCardToServer(card) : await saveCardToServer(card);
        } finally {
            characterSaveInFlight = false;
            setButtonBusy("saveCharacter", false);
        }
        if (!savedCard) return;
        if (activeGalleryEditId) {
            galleryCards = galleryCards.map((item) => item.id === activeGalleryEditId ? savedCard as COC7CharacterCard : item);
            activeGalleryEditId = "";
            renderCharacterGallery();
            modal?.hide();
            return;
        }
        cards = existing ? cards.map((item) => item.id === editingId ? savedCard : item) : [savedCard, ...cards];
        activeCardId = savedCard.id;
        renderList();
        openCharacterDetail(savedCard.id);
        modal?.hide();
    }

    function render(): void {
        renderList();
        renderCharacterGallery();
        showCharacterList();
    }

    function renderList(): void {
        const list = byId("characterList");
        if (!list) return;
        pruneCharacterBatchSelection("card");
        const visibleCards = visibleCharacterCards();
        if (visibleCards.length === 0) {
            list.innerHTML = `<div class="character-empty-filter">没有符合筛选条件的角色卡。</div>`;
            updateCharacterBatchToolbar("card");
            return;
        }
        list.innerHTML = visibleCards.map(renderCharacterCardSummary).join("");
        list.querySelectorAll<HTMLElement>(".character-card").forEach((cardElement) => {
            cardElement.addEventListener("click", (event) => {
                const id = cardElement.dataset.characterId || "";
                if (characterBatchStates.card.mode) {
                    toggleCharacterBatchSelection("card", id, cardElement);
                    return;
                }
                const action = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-action]")?.dataset.action || "detail";
                const card = visibleCards.find((item) => item.id === id);
                if (!card) return;
                if (action === "edit") openEditor(card);
                else if (action === "delete") void deleteCard(id);
                else openCharacterDetail(id);
            });
        });
        updateCharacterBatchToolbar("card");
    }

    function renderCharacterCardSummary(card: COC7CharacterCard): string {
        const batch = characterBatchStates.card;
        const selected = batch.mode && batch.selected.has(card.id);
        return `
            <article class="character-card ${card.id === activeCardId ? "active" : ""}${batch.mode ? " batch-mode" : ""}${selected ? " batch-selected" : ""}" data-character-id="${escapeHtml(card.id)}"${batch.mode ? ' data-batch-selectable="true"' : ""}>
                ${batch.mode ? `<span class="batch-card-check" aria-hidden="true"><i class="fa ${selected ? "fa-check-square-o" : "fa-square-o"}"></i></span>` : ""}
                <div class="character-card-avatar"><img src="${escapeHtml(TrpgAvatar.resolveAvatarSrc(card.name, card.avatar))}" alt=""></div>
                <h5>${escapeHtml(card.name)}</h5>
                <p>${escapeHtml(getOccupation(card).name)} · ${escapeHtml(card.residence || "未知居住地")}</p>
                <div class="character-card-metrics">
                    <span>HP 上限 ${card.maxHp}</span>
                    <span>SAN 上限 ${card.maxSan}</span>
                    <span>MOV ${card.mov}</span>
                </div>
                <div class="character-card-actions">
                    ${batch.mode ? "" : `<button type="button" data-action="detail">详情</button><button type="button" data-action="edit">编辑</button><button type="button" data-action="delete">删除</button>`}
                </div>
            </article>
        `;
    }

    /**
     * 批量管理通用逻辑：个人角色卡列表（card）与角色卡广场（gallery）复用同一套
     * 工具栏更新、选中切换与批量删除流程，仅数据源与权限判定不同。
     */
    function characterBatchText(key: string, fallback: string, values: Record<string, string | number> = {}): string {
        return window.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function characterBatchElements(target: CharacterBatchTarget) {
        const dom = CHARACTER_BATCH_DOM[target];
        return {
            dom,
            toolbar: byId(dom.toolbar),
            count: byId(dom.count),
            selectAll: byId<HTMLInputElement>(dom.selectAll),
            deleteBtn: byId<HTMLButtonElement>(dom.deleteBtn),
            exitBtn: byId<HTMLButtonElement>(dom.exitBtn),
            toggle: byId<HTMLButtonElement>(dom.toggle),
            list: byId(dom.listId)
        };
    }

    function pruneCharacterBatchSelection(target: CharacterBatchTarget): void {
        const state = characterBatchStates[target];
        const source = target === "gallery" ? galleryCards : cards;
        [...state.selected].forEach((id) => {
            if (!source.some((card) => card.id === id)) state.selected.delete(id);
        });
    }

    function updateCharacterBatchToolbar(target: CharacterBatchTarget): void {
        const { toolbar, count, selectAll, deleteBtn, toggle, list } = characterBatchElements(target);
        const state = characterBatchStates[target];
        toggle?.classList.toggle("active", state.mode);
        if (!toolbar) return;
        toolbar.hidden = !state.mode;
        const selectedCount = state.selected.size;
        if (count) {
            count.textContent = selectedCount > 0
                ? characterBatchText("common.batch.selected", `已选 ${selectedCount} 项`, { count: selectedCount })
                : "";
        }
        if (selectAll) {
            const selectable = list ? list.querySelectorAll("[data-batch-selectable='true']").length : 0;
            selectAll.checked = selectable > 0 && selectedCount === selectable;
            selectAll.indeterminate = selectedCount > 0 && selectedCount < selectable;
            selectAll.disabled = selectable === 0;
        }
        if (deleteBtn) deleteBtn.disabled = selectedCount === 0;
    }

    function setCharacterBatchMode(target: CharacterBatchTarget, enabled: boolean): void {
        const state = characterBatchStates[target];
        if (state.mode === enabled) return;
        state.mode = enabled;
        if (!enabled) state.selected.clear();
        if (target === "gallery") renderCharacterGallery(); else renderList();
    }

    function toggleCharacterBatchSelection(target: CharacterBatchTarget, id: string, cardElement: HTMLElement): void {
        if (!id) return;
        const state = characterBatchStates[target];
        const nowSelected = !state.selected.has(id);
        if (nowSelected) state.selected.add(id); else state.selected.delete(id);
        cardElement.classList.toggle("batch-selected", nowSelected);
        const icon = cardElement.querySelector<HTMLElement>(".batch-card-check i");
        if (icon) icon.className = `fa ${nowSelected ? "fa-check-square-o" : "fa-square-o"}`;
        updateCharacterBatchToolbar(target);
    }

    function setAllCharacterBatchSelected(target: CharacterBatchTarget, select: boolean): void {
        const { list } = characterBatchElements(target);
        const state = characterBatchStates[target];
        list?.querySelectorAll<HTMLElement>("[data-batch-selectable='true']").forEach((cardElement) => {
            const id = cardElement.dataset.characterId || cardElement.dataset.galleryCharacterId || "";
            if (!id) return;
            if (select) state.selected.add(id); else state.selected.delete(id);
            cardElement.classList.toggle("batch-selected", select);
            const icon = cardElement.querySelector<HTMLElement>(".batch-card-check i");
            if (icon) icon.className = `fa ${select ? "fa-check-square-o" : "fa-square-o"}`;
        });
        updateCharacterBatchToolbar(target);
    }

    async function deleteSelectedCharacterBatch(target: CharacterBatchTarget): Promise<void> {
        const state = characterBatchStates[target];
        const ids = [...state.selected];
        if (!ids.length) {
            notify(characterBatchText("common.batch.none_selected", "请先选择要删除的项目"), "error");
            return;
        }
        const confirmMessage = characterBatchText(
            target === "gallery" ? "common.batch.delete_confirm_gallery" : "common.batch.delete_confirm",
            target === "gallery"
                ? `确定要删除选中的 ${ids.length} 张公开角色卡吗？此操作不可恢复。`
                : `确定要删除选中的 ${ids.length} 张角色卡吗？此操作不可恢复。`
        );
        const confirmed = await window.TrpgDialog?.confirm({
            title: "批量删除",
            message: confirmMessage,
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;

        const deletedIds = new Set<string>();
        const failures: string[] = [];
        for (const id of ids) {
            try {
                const url = target === "gallery"
                    ? `/api/character-gallery/${encodeURIComponent(id)}`
                    : `/api/characters/${encodeURIComponent(id)}`;
                const response = await TrpgApi.del<ApiResponse>(url);
                if (response.success) deletedIds.add(id);
                else failures.push(response.message || response.error || "删除失败");
            } catch (error) {
                failures.push(characterErrorMessage(error));
            }
        }

        state.mode = false;
        state.selected.clear();
        if (target === "gallery") {
            galleryCards = galleryCards.filter((card) => !deletedIds.has(card.id));
            renderCharacterGallery();
        } else {
            cards = cards.filter((card) => !deletedIds.has(card.id));
            if (!cards.some((card) => card.id === activeCardId)) activeCardId = cards[0]?.id || "";
            render();
        }

        const deleted = deletedIds.size;
        if (failures.length === 0) {
            notify(characterBatchText("common.batch.deleted", `已删除 ${deleted} 项`, { count: deleted }), "success");
        } else {
            notify(`已删除 ${deleted} 项，${failures.length} 项失败`, "error");
        }
    }

    function bindCharacterBatchControls(target: CharacterBatchTarget): void {
        const { toggle, exitBtn, deleteBtn, selectAll } = characterBatchElements(target);
        toggle?.addEventListener("click", () => setCharacterBatchMode(target, !characterBatchStates[target].mode));
        exitBtn?.addEventListener("click", () => setCharacterBatchMode(target, false));
        deleteBtn?.addEventListener("click", () => { void deleteSelectedCharacterBatch(target); });
        selectAll?.addEventListener("change", () => setAllCharacterBatchSelected(target, selectAll.checked));
    }

    function openCharacterDetail(cardId: string): void {
        const visibleCards = visibleCharacterCards();
        const card = visibleCards.find((item) => item.id === cardId) || visibleCards[0];
        const listView = byId("character-list-view");
        const detailPage = byId("character-detail-page");
        const detail = byId("characterDetailView");
        const empty = document.querySelector<HTMLElement>(".character-empty-state");
        if (!detail || !card) return;
        activeCardId = card.id;
        persistCards();
        if (listView) listView.hidden = true;
        if (detailPage) detailPage.hidden = false;
        if (empty) empty.hidden = true;
        detail.hidden = false;
        detail.innerHTML = renderCharacterDetail(card);
        document.querySelectorAll(".character-card").forEach((item) => item.classList.toggle("active", (item as HTMLElement).dataset.characterId === card.id));
        detail.querySelector<HTMLButtonElement>("[data-character-edit-active]")?.addEventListener("click", () => openEditor(card));
    }

    function showCharacterList(): void {
        const listView = byId("character-list-view");
        const detailPage = byId("character-detail-page");
        const detail = byId("characterDetailView");
        const empty = document.querySelector<HTMLElement>(".character-empty-state");
        if (listView) listView.hidden = false;
        if (detailPage) detailPage.hidden = true;
        if (detail) detail.hidden = true;
        if (empty) empty.hidden = false;
        document.querySelectorAll(".character-card").forEach((item) => item.classList.remove("active"));
    }

    function renderCharacterDetail(card: COC7CharacterCard): string {
        return `
            <header class="character-inspector-header">
                <div>
                    <h3>${escapeHtml(card.name)}</h3>
                </div>
                <div class="character-inline-actions"><button type="button" data-character-edit-active>编辑角色卡</button></div>
            </header>
            <div class="character-detail-dashboard">
                ${renderCharacterBasicInfo(card)}
                <section class="character-detail-band">
                    <div class="character-detail-band-head">
                        <h4>属性</h4>
                        <div class="character-attribute-summary character-attribute-summary-inline">
                            <span>基础总和</span>
                            <strong>${calculateAttributeBaseTotal(card.attributes)}</strong>
                        </div>
                    </div>
                    <div class="character-attribute-grid">${ATTRIBUTE_KEYS.map((key) => renderAttributeChip(key, card.attributes[key])).join("")}</div>
                </section>
                ${renderCharacterVitals(card)}
                ${renderCharacterStatusSection(card)}
                ${renderCharacterSkillSection(card)}
                ${renderCharacterWeaponsSection(card)}
                ${renderCharacterCombatSection(card)}
                ${renderCharacterPossessionsSection(card)}
                ${renderCharacterAssetsSection(card)}
                ${renderCharacterBackgroundSections(card)}
            </div>
        `;
    }

    function statCard(label: string, value: unknown): string {
        return `<div class="character-stat-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
    }

    function renderCharacterBasicInfo(card: COC7CharacterCard): string {
        const occupation = getOccupation(card);
        const fields: Array<[string, unknown]> = [
            ["姓名", card.name],
            ["绑定玩家", playerDisplayName(card.playerId)],
            ["时代", card.era],
            ["种族类型", card.background.raceType],
            ["职业", card.occupationName || occupation.name],
            ["年龄", card.age],
            ["性别", card.gender],
            ["现居地", card.residence],
            ["出生地", card.birthplace]
        ];
        return `
            <section class="character-detail-band">
                <h4>基础信息</h4>
                <div class="character-detail-info-grid">${fields.map(([label, value]) => statCard(label, value || "未填写")).join("")}</div>
            </section>
        `;
    }

    function renderCharacterVitals(card: COC7CharacterCard): string {
        const fields: Array<[string, unknown]> = [
            ["生命值", `${card.currentHp} / ${card.maxHp}`],
            ["理智值", `${card.currentSan} / ${card.initialSan} / ${card.maxSan}`],
            ["魔法值", `${card.currentMp} / ${card.maxMp}`],
            ["幸运", card.attributes.LUC],
            ["移动速度", card.mov]
        ];
        return `
            <section class="character-detail-band character-detail-band-vitals">
                <h4>生命（HP）</h4>
                <div class="character-vital-grid">${fields.map(([label, value]) => statCard(label, value)).join("")}</div>
            </section>
        `;
    }

    function renderCharacterStatusSection(card: COC7CharacterCard): string {
        return `
            <section class="character-detail-band">
                <h4>人物状态</h4>
                <div class="character-vital-grid">${statCard("当前状态", statusSummary(card.status))}</div>
            </section>
        `;
    }

    function renderCharacterSkillSection(card: COC7CharacterCard): string {
        const rows = card.skills.map((skill) => {
            const occupationPoints = skill.occupationPoints || 0;
            const interestPoints = skill.interestPoints || 0;
            const growthPoints = skill.growthPoints || 0;
            const success = clampNumber(skill.value, 0, 99, skill.base);
            return `
                <tr>
                    <td>${skill.occupation ? "是" : "否"}</td>
                    <td>${escapeHtml(skill.name)}</td>
                    <td>${skill.base}</td>
                    <td>${occupationPoints}</td>
                    <td>${interestPoints}</td>
                    <td>${growthPoints}</td>
                    <td>${success}</td>
                    <td>${Math.floor(success / 2)}</td>
                    <td>${Math.floor(success / 5)}</td>
                </tr>
            `;
        }).join("");
        return `
            <section class="character-detail-band character-detail-band-wide">
                <h4>技能</h4>
                <div class="character-detail-table-shell">
                    <table class="character-detail-table">
                        <thead>
                            <tr>
                                <th>专职</th>
                                <th>技能名称</th>
                                <th>基础%</th>
                                <th>职业%</th>
                                <th>兴趣%</th>
                                <th>成长%</th>
                                <th>成功率%</th>
                                <th>困难50%</th>
                                <th>噩梦20%</th>
                            </tr>
                        </thead>
                        <tbody>${rows || `<tr><td colspan="9">暂无技能</td></tr>`}</tbody>
                    </table>
                </div>
            </section>
        `;
    }

    function renderCharacterWeaponsSection(card: COC7CharacterCard): string {
        const rows = card.weapons.map((weapon) => {
            const success = findSkillSuccessByWeaponSkill(weapon);
            const impale = weapon.impale === null ? "-" : weapon.impale ? "是" : "否";
            return `
                <tr>
                    <td>${escapeHtml(weapon.name)}</td>
                    <td>${escapeHtml(weapon.skill)}</td>
                    <td>${success}</td>
                    <td>${escapeHtml(weapon.damage)}</td>
                    <td>${escapeHtml(weapon.range)}</td>
                    <td>${impale}</td>
                    <td>${escapeHtml(weapon.attacks)}</td>
                    <td>${escapeHtml(weapon.ammo)}</td>
                    <td>${escapeHtml(weapon.malfunction)}</td>
                </tr>
            `;
        }).join("");
        return `
            <section class="character-detail-band character-detail-band-wide">
                <h4>武器</h4>
                <div class="character-detail-table-shell">
                    <table class="character-detail-table">
                        <thead>
                            <tr>
                                <th>武器名称</th>
                                <th>使用技能</th>
                                <th>成功率</th>
                                <th>伤害</th>
                                <th>射程</th>
                                <th>贯穿</th>
                                <th>次数</th>
                                <th>装弹量</th>
                                <th>故障率</th>
                            </tr>
                        </thead>
                        <tbody>${rows || `<tr><td colspan="9">暂无武器</td></tr>`}</tbody>
                    </table>
                </div>
            </section>
        `;
    }

    function renderCharacterCombatSection(card: COC7CharacterCard): string {
        const fields: Array<[string, unknown]> = [
            ["伤害加值 DB", card.damageBonus],
            ["体格", card.build],
            ["护甲", card.armor],
            ["移动力", card.mov]
        ];
        return `
            <section class="character-detail-band">
                <h4>战斗</h4>
                <div class="character-vital-grid">${fields.map(([label, value]) => statCard(label, value)).join("")}</div>
            </section>
        `;
    }

    function renderCharacterPossessionsSection(card: COC7CharacterCard): string {
        return `
            <section class="character-detail-band">
                <h4>物品与装备</h4>
                <div class="background-grid">${card.equipment.map(renderEquipmentDetail).join("") || `<div class="background-note">未填写</div>`}</div>
            </section>
        `;
    }

    function renderCharacterAssetsSection(card: COC7CharacterCard): string {
        const fields: Array<[string, unknown]> = [
            ["信用评级", card.creditRating],
            ["现金", card.assets.cash],
            ["消费水平", card.assets.spendingLevel]
        ];
        return `
            <section class="character-detail-band">
                <h4>资产</h4>
                <div class="character-vital-grid">${fields.map(([label, value]) => statCard(label, value)).join("")}</div>
                <div class="background-grid background-grid-single">${backgroundNote("资产", card.assets.assetsText)}</div>
            </section>
        `;
    }

    function renderCharacterBackgroundSections(card: COC7CharacterCard): string {
        return `
            <section class="character-detail-band"><h4>克苏鲁神话</h4><div class="background-grid">${backgroundNote("魔法物品与典籍", card.background.arcaneTomes)}${backgroundNote("法术", card.background.spells)}${backgroundNote("第三类接触", card.background.encounters)}</div></section>
            <section class="character-detail-band"><h4>背景故事</h4><div class="background-grid">${backgroundNote("个人介绍", card.background.story)}${backgroundNote("形象描述", card.background.appearance)}${backgroundNote("思想与信念", card.background.ideology)}${backgroundNote("重要之人", card.background.significantPeople)}${backgroundNote("意义非凡之地", card.background.meaningfulLocations)}${backgroundNote("宝贵之物", card.background.treasuredPossessions)}${backgroundNote("特质", card.background.traits)}${backgroundNote("伤口与疤痕", card.background.injuriesScars)}${backgroundNote("精神症状", card.background.phobiasManias)}</div></section>
            <section class="character-detail-band"><h4>人际关系</h4><div class="background-grid">${card.relationships.map(renderRelationshipDetail).join("") || `<div class="background-note">暂无人际关系</div>`}</div></section>
            <section class="character-detail-band"><h4>经历过的模组</h4><div class="background-grid">${card.experiencedScenarios.map(renderExperiencedScenarioDetail).join("") || `<div class="background-note">暂无经历过的模组</div>`}</div></section>
        `;
    }

    function statusSummary(status: CharacterStatusFlags): string {
        const labels: Array<[keyof CharacterStatusFlags, string]> = [
            ["majorWound", "重伤"],
            ["unconscious", "昏迷"],
            ["dead", "死亡"],
            ["temporaryInsanity", "临时疯狂"],
            ["permanentInsanity", "永久疯狂"],
            ["indefiniteInsanity", "不定期疯狂"]
        ];
        const active = labels.filter(([key]) => status[key]).map(([, label]) => label);
        return active.length ? active.join("、") : "正常";
    }

    function renderAttributeChip(key: COC7CoreAttributeKey, value: number): string {
        const derived = calculateAttributeDisplayValues(value);
        return `<div class="attribute-chip"><span>${ATTRIBUTE_LABELS[key]} (${key})</span><strong>${value}</strong><small>半 ${derived.half} / 比 ${derived.ratio}</small></div>`;
    }

    function renderEquipmentDetail(item: COC7EquipmentItem): string {
        const details = [`数量 ${item.quantity}`, `重量 ${item.weight} kg`, item.notes || ""].filter(Boolean).join("；");
        return backgroundNote(item.name || "未命名物品", details);
    }

    function backgroundNote(label: string, value: string): string {
        return `<div class="background-note"><strong>${escapeHtml(label)}</strong><p>${escapeHtml(value || "未填写")}</p></div>`;
    }

    function renderRelationshipDetail(item: COC7Relationship): string {
        const parts = [item.description, item.player ? `玩家：${item.player}` : ""].filter(Boolean).join("；");
        return backgroundNote(item.name || "未命名关系", parts);
    }

    function renderExperiencedScenarioDetail(item: COC7ExperiencedScenario): string {
        const lines = [item.name || "未命名模组"];
        const sanLine = item.sanChange || item.san_change || "";
        const otherLine = item.otherChanges || item.other_changes || "";
        if (item.experience && !sanLine && !otherLine) lines.push(item.experience);
        if (sanLine) lines.push(sanLine);
        if (otherLine) lines.push(otherLine);
        const name = escapeHtml(lines.shift() || "");
        const body = lines.filter(Boolean).map((part) => escapeHtml(part || "")).join("<br>");
        return `<div class="background-note"><strong>${name}</strong><p>${body || "未填写"}</p></div>`;
    }

    async function deleteCard(id: string): Promise<void> {
        const card = cards.find((item) => item.id === id);
        if (!card) return;
        const confirmed = await window.TrpgDialog?.confirm({
            title: "删除角色卡",
            message: `确定要删除角色卡“${card.name || "未命名角色卡"}”吗？此操作不可恢复。`,
            confirmText: "删除",
            danger: true,
        });
        if (!confirmed) return;
        try {
            const response = await TrpgApi.del<ApiResponse>(`/api/characters/${encodeURIComponent(id)}`);
            if (!response.success) {
                notify(response.message || response.error || "删除角色卡失败", "error");
                return;
            }
            cards = cards.filter((item) => item.id !== id);
            activeCardId = cards[0]?.id || "";
            render();
            notify("角色卡已删除", "success");
        } catch (error) {
            notify(`删除角色卡失败：${characterErrorMessage(error)}`, "error");
        }
    }

    function formatEquipment(equipment: COC7EquipmentItem[]): string {
        return equipment.map((item) => `${item.name}:${item.quantity}:${item.weight}:${item.notes || ""}`).join("；");
    }

    function parseEquipment(raw: string): COC7EquipmentItem[] {
        return raw.split(/[;\n；]+/).map((line) => line.trim()).filter(Boolean).map((line) => {
            const [name, quantity, weight, notes] = line.split(/[:：]/).map((part) => part.trim());
            return { name: name || "未命名装备", quantity: clampNumber(quantity, 1, 999, 1), weight: Math.max(0, Number(weight) || 0), volume: 0, notes: notes || "" };
        });
    }

    function readEditorCreditRating(): number {
        return clampNumber(readSkillCreditRating(), 0, 99, 0);
    }

    function readSkillCreditRating(): number {
        const body = byId("characterSkillTableBody");
        if (!body) return 0;
        const row = body.querySelector<HTMLTableRowElement>('tr[data-skill-key="creditRating"]');
        if (!row) return 0;
        return clampNumber(row.querySelector<HTMLElement>("[data-skill-success]")?.textContent, 0, 99, 0);
    }

    function syncEditorCreditRating(): void {
        setInputValue("characterCreditRating", readEditorCreditRating());
    }

    function readEditorAssets(): COC7Assets {
        return {
            cash: clampNumber(getInputValue("characterCash"), 0, 999999, 0),
            spendingLevel: clampNumber(getInputValue("characterSpendingLevel"), 0, 999999, 0),
            assetsText: getInputValue("characterAssets")
        };
    }

    function createRelationshipRow(item: Partial<COC7Relationship> = {}, index = 0): string {
        const suffix = index === 0 ? "" : `-${index}`;
        return `
            <div class="character-repeatable-row" data-companion-row>
                <input type="text" class="form-control" id="characterCompanionRole${suffix}" data-companion-role placeholder="角色" value="${escapeHtml(item.name || "")}">
                <input type="text" class="form-control" id="characterCompanionRelationship${suffix}" data-companion-relationship placeholder="关系" value="${escapeHtml(item.description || "")}">
                <input type="text" class="form-control" id="characterCompanionPlayer${suffix}" data-companion-player placeholder="玩家" value="${escapeHtml(item.player || "")}">
                <button type="button" class="character-icon-button" data-remove-companion title="删除人际关系" aria-label="删除人际关系">
                    <i class="fa fa-trash" aria-hidden="true"></i>
                </button>
            </div>
        `;
    }

    function hydrateRelationshipRows(relationships: COC7Relationship[] = []): void {
        const list = byId("characterCompanionList");
        if (!list) return;
        const rows = relationships.length ? relationships : [{}];
        list.innerHTML = rows.map((item, index) => createRelationshipRow(item, index)).join("");
    }

    function addRelationshipRow(item: Partial<COC7Relationship> = {}): void {
        const list = byId("characterCompanionList");
        if (!list) return;
        list.insertAdjacentHTML("beforeend", createRelationshipRow(item, list.querySelectorAll("[data-companion-row]").length));
    }

    function handleRelationshipRowClick(event: Event): void {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-remove-companion]");
        if (!button) return;
        const list = byId("characterCompanionList");
        const row = button.closest<HTMLElement>("[data-companion-row]");
        if (!list || !row) return;
        if (list.querySelectorAll("[data-companion-row]").length <= 1) {
            row.querySelectorAll<HTMLInputElement>("input").forEach((input) => { input.value = ""; });
            return;
        }
        row.remove();
    }

    function readRelationshipRows(): COC7Relationship[] {
        const list = byId("characterCompanionList");
        if (!list) return [];
        return Array.from(list.querySelectorAll<HTMLElement>("[data-companion-row]")).map((row) => {
            const name = row.querySelector<HTMLInputElement>("[data-companion-role]")?.value.trim() || "";
            const description = row.querySelector<HTMLInputElement>("[data-companion-relationship]")?.value.trim() || "";
            const player = row.querySelector<HTMLInputElement>("[data-companion-player]")?.value.trim() || "";
            return { name, description, player };
        }).filter((item) => item.name || item.description || item.player);
    }

    function createExperiencedScenarioRow(item: Partial<COC7ExperiencedScenario> = {}, index = 0): string {
        const suffix = index === 0 ? "" : `-${index}`;
        return `
            <div class="character-repeatable-row character-scenario-row" data-scenario-row>
                <input type="text" class="form-control" id="characterScenarioName${suffix}" data-scenario-name placeholder="模组" value="${escapeHtml(item.name || "")}">
                <textarea class="form-control character-medium-textarea" id="characterScenarioExperience${suffix}" data-scenario-experience rows="2" placeholder="经历">${escapeHtml(item.experience || "")}</textarea>
                <button type="button" class="character-icon-button" data-remove-scenario title="删除经历过的模组" aria-label="删除经历过的模组">
                    <i class="fa fa-trash" aria-hidden="true"></i>
                </button>
            </div>
        `;
    }

    function hydrateExperiencedScenarioRows(scenarios: COC7ExperiencedScenario[] = []): void {
        const list = byId("characterScenarioList");
        if (!list) return;
        const rows = scenarios.length ? scenarios : [{}];
        list.innerHTML = rows.map((item, index) => createExperiencedScenarioRow(item, index)).join("");
    }

    function addExperiencedScenarioRow(item: Partial<COC7ExperiencedScenario> = {}): void {
        const list = byId("characterScenarioList");
        if (!list) return;
        list.insertAdjacentHTML("beforeend", createExperiencedScenarioRow(item, list.querySelectorAll("[data-scenario-row]").length));
    }

    function handleExperiencedScenarioRowClick(event: Event): void {
        const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-remove-scenario]");
        if (!button) return;
        const list = byId("characterScenarioList");
        const row = button.closest<HTMLElement>("[data-scenario-row]");
        if (!list || !row) return;
        if (list.querySelectorAll("[data-scenario-row]").length <= 1) {
            row.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea").forEach((input) => { input.value = ""; });
            return;
        }
        row.remove();
    }

    function readExperiencedScenarioRows(): COC7ExperiencedScenario[] {
        const list = byId("characterScenarioList");
        if (!list) return [];
        return Array.from(list.querySelectorAll<HTMLElement>("[data-scenario-row]")).map((row) => {
            const name = row.querySelector<HTMLInputElement>("[data-scenario-name]")?.value.trim() || "";
            const experience = row.querySelector<HTMLTextAreaElement>("[data-scenario-experience]")?.value.trim() || "";
            return { name, experience };
        }).filter((item) => item.name || item.experience);
    }

    function isRecord(value: unknown): value is Record<string, unknown> {
        return Boolean(value && typeof value === "object" && !Array.isArray(value));
    }

    function stringField(value: unknown, fallback = ""): string {
        return value === undefined || value === null ? fallback : String(value);
    }

    function recordField(value: unknown): Record<string, unknown> {
        return isRecord(value) ? value : {};
    }

    function jsonArrayField(value: unknown): Array<Record<string, unknown>> {
        if (Array.isArray(value)) return value.filter(isRecord);
        if (typeof value !== "string" || !value.trim()) return [];
        try {
            const parsed = JSON.parse(value) as unknown;
            return Array.isArray(parsed) ? parsed.filter(isRecord) : [];
        } catch {
            return [];
        }
    }

    function jsonArrayText(items: Array<Record<string, unknown>>): string {
        return JSON.stringify(items);
    }

    function testAttribute(attributes: Record<string, unknown>, upperKey: COC7CoreAttributeKey, lowerKey: string, fallback = 50): number {
        return clampNumber(attributes[upperKey] ?? attributes[lowerKey], 1, 99, fallback);
    }

    function equipmentToItemsText(equipment: COC7EquipmentItem[]): string {
        return equipment.map((item) => {
            const base = `${item.name} x${Math.max(1, item.quantity)}`;
            return item.notes ? `${base}：${item.notes}` : base;
        }).join("\n");
    }

    function skillGroupKey(skill: COC7Skill): string {
        return TEST_CHARACTER_CATEGORY_GROUPS[String(skill.category || "")] || "other";
    }

    function convertCardToTestCharacterJson(card: COC7CharacterCard): TestCharacterJson {
        const skillGroups = Object.keys(TEST_CHARACTER_SKILL_GROUPS).reduce((groups, group) => {
            groups[group] = [];
            return groups;
        }, {} as Record<string, Array<Record<string, unknown>>>);
        card.skills.forEach((skill) => {
            const group = skillGroups[skillGroupKey(skill)] ?? skillGroups.other!;
            group.push({
                name: skill.name,
                base: skill.base,
                job: skill.occupationPoints || 0,
                interest: skill.interestPoints || 0,
                growth: skill.growthPoints || 0,
                isProfessional: Boolean(skill.isProfessional ?? skill.occupation ?? skill.checked)
            });
        });
        return {
            id: card.id,
            name: card.name,
            playerName: playerDisplayName(card.playerId),
            playerId: card.playerId,
            time: card.era,
            job: card.occupationName,
            age: String(card.age),
            gender: card.gender,
            location: card.residence,
            hometown: card.birthplace,
            attributes: {
                str: card.attributes.STR,
                dex: card.attributes.DEX,
                con: card.attributes.CON,
                app: card.attributes.APP,
                pow: card.attributes.POW,
                siz: card.attributes.SIZ,
                edu: card.attributes.EDU,
                int: card.attributes.INT,
                luc: card.attributes.LUC
            },
            deriveAttributes: {
                sanity: { current: String(card.currentSan), start: String(card.initialSan), max: String(card.maxSan) },
                hp: { current: String(card.currentHp), max: String(card.maxHp) },
                mp: { current: String(card.currentMp), max: String(card.maxMp) }
            },
            battleAttributes: {
                db: card.damageBonus,
                build: String(card.build),
                mov: String(card.mov),
                movNote: "",
                armor: String(card.armor)
            },
            characterStatus: {
                bodyStates: {
                    [TEST_CHARACTER_BODY_STATUS.majorWound]: card.status.majorWound,
                    [TEST_CHARACTER_BODY_STATUS.unconscious]: card.status.unconscious,
                    [TEST_CHARACTER_BODY_STATUS.dead]: card.status.dead
                },
                mentalStates: {
                    [TEST_CHARACTER_MENTAL_STATUS.indefiniteInsanity]: card.status.indefiniteInsanity,
                    [TEST_CHARACTER_MENTAL_STATUS.permanentInsanity]: card.status.permanentInsanity,
                    [TEST_CHARACTER_MENTAL_STATUS.temporaryInsanity]: card.status.temporaryInsanity
                }
            },
            pointValues: {},
            proSkills: [],
            skillPoints: [],
            weapons: card.weapons.map((weapon) => ({
                name: weapon.name || "",
                skill: weapon.skill || "",
                damage: weapon.damage || "",
                range: weapon.range || "",
                round: weapon.attacks || "",
                tho: "",
                num: "",
                err: "",
                weight: weapon.weight || "",
                note: weapon.note || ""
            })),
            stories: {
                app: card.background.appearance,
                belief: card.background.ideology,
                IPerson: card.background.significantPeople,
                IPlace: card.background.meaningfulLocations,
                IItem: card.background.treasuredPossessions,
                trait: card.background.traits,
                scar: card.background.injuriesScars,
                mad: card.background.phobiasManias,
                desc: card.background.story
            },
            assets: {
                cash: String(card.assets.cash),
                consumption: String(card.assets.spendingLevel),
                assets: card.assets.assetsText,
                items: equipmentToItemsText(card.equipment),
                magicItems: card.background.arcaneTomes,
                magics: card.background.spells,
                touches: card.background.encounters
            },
            experiencedModules: jsonArrayText(card.experiencedScenarios.map((item) => ({ name: item.name, experience: item.experience, san_change: item.sanChange || item.san_change, other_changes: item.otherChanges || item.other_changes }))),
            friends: jsonArrayText(card.relationships.map((item) => ({ character: item.name, relationship: item.description, player: item.player }))),
            skillGroups,
            isEditable: true,
            createdAt: card.createdAt,
            updatedAt: card.updatedAt
        };
    }

    function convertTestCharacterJsonToCardInput(payload: TestCharacterJson): COC7CharacterCardInput {
        const attributes = recordField(payload.attributes);
        const derived = recordField(payload.deriveAttributes);
        const sanity = recordField(derived.sanity);
        const hp = recordField(derived.hp);
        const mp = recordField(derived.mp);
        const battle = recordField(payload.battleAttributes);
        const characterStatus = recordField(payload.characterStatus);
        const bodyStates = recordField(characterStatus.bodyStates);
        const mentalStates = recordField(characterStatus.mentalStates);
        const stories = recordField(payload.stories);
        const assets = recordField(payload.assets);
        const age = clampNumber(payload.age, 15, 99, 25);
        const skillOccurrences = new Map<string, number>();
        const skills: COC7Skill[] = Object.entries(recordField(payload.skillGroups)).flatMap(([group, items]) => {
            if (!Array.isArray(items)) return [];
            return items.filter(isRecord).map((item, index) => {
                const importedBase = clampNumber(item.base, 0, 99, 0);
                const occupationPoints = clampNumber(item.job, 0, 99, 0);
                const interestPoints = clampNumber(item.interest, 0, 99, 0);
                const growthPoints = clampNumber(item.growth, 0, 99, 0);
                const name = stringField(item.name, "未命名技能");
                const skillKey = skillKeyByName(name, stringField(item.key || item.id || item.skillKey || `${slugify(name)}-${index}`));
                const occurrence = (skillOccurrences.get(skillKey) || 0) + 1;
                skillOccurrences.set(skillKey, occurrence);
                // 导入时以角色卡中的基础值为主；缺失时才回退到目录默认值。
                const hasImportedBase = item.base !== undefined && item.base !== null && item.base !== "";
                const base = hasImportedBase ? importedBase : clampNumber(item.base, 0, 99, skillBaseByKey(skillKey, group));
                const value = clampNumber(item.value, 0, 99, base + occupationPoints + interestPoints + growthPoints);
                const skill: COC7Skill = {
                    id: occurrence === 1 ? skillKey : `${skillKey}__${occurrence}`,
                    skillKey,
                    name,
                    base,
                    value,
                    category: stringField(item.category || TEST_CHARACTER_SKILL_GROUPS[group] || skillCategoryByKey(skillKey)),
                    checked: Boolean(item.checked ?? item.isProfessional),
                    occupation: Boolean(item.isProfessional),
                    isProfessional: Boolean(item.isProfessional),
                    occupationPoints,
                    interestPoints,
                    growthPoints
                };
                const specialtyKey = stringField(item.specialtyKey);
                if (specialtyKey) skill.specialtyKey = specialtyKey;
                return skill;
            });
        });
        return {
            name: stringField(payload.name, "导入角色卡"),
            playerId: stringField(payload.playerId || payload.playerName),
            era: stringField(payload.time, "1920s"),
            gender: stringField(payload.gender),
            age,
            occupationName: stringField(payload.job),
            residence: stringField(payload.location),
            birthplace: stringField(payload.hometown),
            attributes: {
                STR: testAttribute(attributes, "STR", "str"),
                DEX: testAttribute(attributes, "DEX", "dex"),
                CON: testAttribute(attributes, "CON", "con"),
                APP: testAttribute(attributes, "APP", "app"),
                POW: testAttribute(attributes, "POW", "pow"),
                SIZ: testAttribute(attributes, "SIZ", "siz"),
                EDU: testAttribute(attributes, "EDU", "edu"),
                INT: testAttribute(attributes, "INT", "int"),
                LUC: testAttribute(attributes, "LUC", "luc"),
                AGE: age
            },
            maxHp: clampNumber(hp.max, 0, 999, 0),
            currentHp: clampNumber(hp.current, 0, 999, 0),
            maxSan: clampNumber(sanity.max, 0, 999, 99),
            initialSan: clampNumber(sanity.start, 0, 999, 0),
            currentSan: clampNumber(sanity.current, 0, 999, 0),
            maxMp: clampNumber(mp.max, 0, 999, 0),
            currentMp: clampNumber(mp.current, 0, 999, 0),
            magicPoints: clampNumber(mp.current, 0, 999, 0),
            damageBonus: stringField(battle.db),
            build: clampNumber(battle.build, -2, 99, 0),
            mov: clampNumber(battle.mov, 0, 99, 0),
            armor: clampNumber(battle.armor, 0, 99, 0),
            status: {
                majorWound: Boolean(bodyStates[TEST_CHARACTER_BODY_STATUS.majorWound]),
                unconscious: Boolean(bodyStates[TEST_CHARACTER_BODY_STATUS.unconscious]),
                dead: Boolean(bodyStates[TEST_CHARACTER_BODY_STATUS.dead]),
                indefiniteInsanity: Boolean(mentalStates[TEST_CHARACTER_MENTAL_STATUS.indefiniteInsanity]),
                permanentInsanity: Boolean(mentalStates[TEST_CHARACTER_MENTAL_STATUS.permanentInsanity]),
                temporaryInsanity: Boolean(mentalStates[TEST_CHARACTER_MENTAL_STATUS.temporaryInsanity])
            },
            skills,
            weapons: (payload.weapons || []).filter(isRecord).map((weapon) => ({
                name: stringField(weapon.name),
                skill: stringField(weapon.skill),
                skillKey: skillKeyByName(stringField(weapon.skill), stringField(weapon.skillKey)),
                specialtyKey: "",
                damage: stringField(weapon.damage),
                range: stringField(weapon.range),
                impale: typeof weapon.impale === "boolean" ? weapon.impale : null,
                attacks: stringField(weapon.attacks || weapon.round),
                ammo: stringField(weapon.ammo || weapon.num),
                malfunction: stringField(weapon.malfunction || weapon.err),
                weight: stringField(weapon.weight),
                note: stringField(weapon.note)
            })),
            equipment: parseEquipment(stringField(assets.items)),
            assets: {
                cash: clampNumber(assets.cash, 0, 999999, 0),
                spendingLevel: clampNumber(assets.consumption, 0, 999999, 0),
                assetsText: stringField(assets.assets)
            },
            background: {
                appearance: stringField(stories.app),
                ideology: stringField(stories.belief),
                significantPeople: stringField(stories.IPerson),
                meaningfulLocations: stringField(stories.IPlace),
                treasuredPossessions: stringField(stories.IItem),
                traits: stringField(stories.trait),
                injuriesScars: stringField(stories.scar),
                phobiasManias: stringField(stories.mad),
                story: stringField(stories.desc),
                arcaneTomes: stringField(assets.magicItems),
                spells: stringField(assets.magics),
                encounters: stringField(assets.touches),
                education: "",
                raceType: "人类"
            },
            relationships: jsonArrayField(payload.friends).map((item) => ({
                name: stringField(item.character),
                description: stringField(item.relationship),
                player: stringField(item.player)
            })),
            experiencedScenarios: jsonArrayField(payload.experiencedModules).map((item) => ({
                name: stringField(item.name),
                experience: stringField(item.experience),
                sanChange: stringField(item.san_change || item.sanChange),
                otherChanges: stringField(item.other_changes || item.otherChanges)
            })),
            ...(payload.createdAt ? { createdAt: payload.createdAt } : {}),
            ...(payload.updatedAt ? { updatedAt: payload.updatedAt } : {})
        };
    }

    function isTestCharacterJsonPayload(value: unknown): value is TestCharacterJson {
        return isRecord(value) && ["deriveAttributes", "battleAttributes", "characterStatus", "skillGroups", "stories", "friends", "experiencedModules"].some((key) => key in value);
    }

    function readCharacterImportFile(file: File): Promise<unknown> {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                try {
                    resolve(JSON.parse(String(reader.result || "")) as unknown);
                } catch (error) {
                    reject(error);
                }
            };
            reader.onerror = () => reject(reader.error || new Error("Failed to read character file"));
            reader.readAsText(file);
        });
    }

    /** 收集基础值超过有效上限（房间 > 用户个性化 > 管理员 > 目录默认）的技能名。 */
    function collectOverLimitSkillNames(card: COC7CharacterCard): string[] {
        const occupation = getOccupation(card);
        return card.skills.reduce((names, skill) => {
            const skillKey = resolveSkillKey(skill);
            if (isUserDefinedBaseSkill(skillKey)) return names;
            const ceiling = resolveEffectiveBase(skillKey, skill.specialtyKey || "", card.attributes, occupation);
            if (skill.base > ceiling) names.push(skill.name);
            return names;
        }, [] as string[]);
    }

    /** 房规技能基础值上限（键为 `skillKey` 或 `skillKey.specialtyKey`）。 */
    function resolveRoomSkillBaseCeiling(
        skillKey: string,
        specialtyKey: string,
        overrides: Record<string, number>
    ): number | null {
        const overrideKey = skillBaseOverrideKey(skillKey, specialtyKey);
        if (Object.prototype.hasOwnProperty.call(overrides, overrideKey)) return overrides[overrideKey] ?? null;
        if (specialtyKey && Object.prototype.hasOwnProperty.call(overrides, skillKey)) return overrides[skillKey] ?? null;
        return null;
    }

    /** 角色卡基础值超过房规上限的技能名列表（绑定前提示用）。 */
    function collectCardSkillBaseOverflows(card: COC7CharacterCard, roomBases?: Record<string, number>): string[] {
        const overrides = roomBases || getRoomSkillBases();
        if (!Object.keys(overrides).length) return [];
        return card.skills.reduce((names, skill) => {
            const skillKey = resolveSkillKey(skill);
            if (isUserDefinedBaseSkill(skillKey)) return names;
            const ceiling = resolveRoomSkillBaseCeiling(skillKey, String(skill.specialtyKey || "").trim(), overrides);
            if (ceiling !== null && skill.base > ceiling) names.push(skill.name);
            return names;
        }, [] as string[]);
    }

    /** 进入房间绑定角色卡时，将超出房规上限的基础值裁剪为上限并重算成功率。 */
    function clampCardSkillBases(card: COC7CharacterCard, roomBases?: Record<string, number>): COC7CharacterCard {
        const overrides = roomBases || getRoomSkillBases();
        const skills = card.skills.map((skill) => {
            const skillKey = resolveSkillKey(skill);
            if (isUserDefinedBaseSkill(skillKey)) return skill;
            const ceiling = resolveRoomSkillBaseCeiling(skillKey, String(skill.specialtyKey || "").trim(), overrides);
            if (ceiling === null || skill.base <= ceiling) return skill;
            return {
                ...skill,
                base: ceiling,
                value: clampNumber(skill.value - (skill.base - ceiling), 0, 99, ceiling)
            };
        });
        return createCharacterCard({ ...card, skills });
    }

    async function importCharacterData(data: unknown): Promise<number> {
        const payloads = Array.isArray(data) ? data : [data];
        let imported = 0;
        const overLimitSkills = new Set<string>();
        for (const payload of payloads) {
            if (!isRecord(payload)) continue;
            if (!canCreateCharacterCard()) break;
            const input = isTestCharacterJsonPayload(payload)
                ? convertTestCharacterJsonToCardInput(payload)
                : payload as COC7CharacterCardInput;
            const { id: _importedId, ...importInput } = input;
            const card = createCharacterCard({
                ...importInput,
                playerId: isCurrentUserElevated() ? stringField(input.playerId) : currentPlayerId()
            });
            collectOverLimitSkillNames(card).forEach((name) => overLimitSkills.add(name));
            const savedCard = await saveCardToServer(card);
            if (!savedCard) continue;
            cards = [savedCard, ...cards.filter((item) => item.id !== savedCard.id)];
            activeCardId = savedCard.id;
            imported += 1;
        }
        if (overLimitSkills.size) {
            notify(`导入的角色卡中以下技能基础值超过设定上限，已按导入值保留：${Array.from(overLimitSkills).join("、")}`, "info");
        }
        return imported;
    }

    async function saveImportedCharacterFile(file: File): Promise<void> {
        try {
            const imported = await importCharacterData(await readCharacterImportFile(file));
            if (imported > 0) {
                renderList();
                openCharacterDetail(activeCardId);
                notify(`已导入 ${imported} 张角色卡`, "success");
            } else {
                notify(`未能从 ${file.name} 导入角色卡`, "error");
            }
        } catch (error) {
            notify(`导入角色卡失败：${characterErrorMessage(error)}`, "error");
        }
    }

    function importCharacterFiles(event: Event): void {
        const input = event.target as HTMLInputElement | null;
        const files = Array.from(input?.files || []);
        void files.reduce((chain, file) => chain.then(() => saveImportedCharacterFile(file)), Promise.resolve()).finally(() => {
            if (input) input.value = "";
        });
    }

    function exportActiveCard(): void {
        const card = visibleCharacterCards().find((item) => item.id === activeCardId);
        if (!card) return;
        const payload = JSON.stringify(convertCardToTestCharacterJson(card), null, 2);
        if (navigator.clipboard?.writeText) {
            void navigator.clipboard.writeText(payload).then(() => notify("角色卡 JSON 已复制到剪贴板。", "success"));
        } else {
            console.log(payload);
            notify("当前浏览器不支持剪贴板写入，角色卡 JSON 已输出到控制台。", "error");
        }
    }

    function listCharacterCards(): COC7CharacterCard[] {
        return visibleCharacterCards().map(cloneCard);
    }

    function getCharacterCardSnapshot(cardId: string): Partial<COC7CharacterCard> | null {
        const card = visibleCharacterCards().find((item) => item.id === cardId);
        return card ? cloneCard(card) : null;
    }

    function notify(message: string, type = "info"): void {
        const notifier = (global as unknown as { showNotification?: (message: string, type?: string) => void }).showNotification;
        if (typeof notifier === "function") notifier(message, type);
    }

    function characterErrorMessage(error: unknown): string {
        return error instanceof Error ? error.message : String(error);
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    const api: CharacterApi = {
        ATTRIBUTE_KEYS,
        BASE_SKILLS,
        PRESET_OCCUPATIONS,
        calculateHalfAndFifth,
        calculateAttributeDisplayValues,
        calculateAttributeBaseTotal,
        calculateMaxHp,
        calculateMaxSan,
        calculateMaxMp,
        calculateMov,
        calculateOccupationSkillPoints,
        calculatePersonalInterestPoints,
        calculateBuildAndDamageBonus,
        calculateEquipmentLoad,
        groupSkillsByCategory,
        countSelectedOccupationSkills,
        validateOccupationSkillSelection,
        autoAllocateOccupationSkills,
        applySkillSpecialty,
        openSkillBaseSettings,
        reloadSkillBases,
        collectCardSkillBaseOverflows,
        clampCardSkillBases,
        rollAttributeCheck,
        generateInvestigatorName,
        generateRegionalName,
        parseAttributeRollFormula,
        rollAttributeFormula,
        randomizeAttributes,
        createCharacterCard,
        listCharacterCards,
        getCharacterCardSnapshot,
        renderCharacterDetail,
        clearCharacterManagement: clearCharacterSheetState,
        reloadCharacterManagement: reloadCharacterSheet,
        initCharacterSheet
    };

    global.COC7CharacterSheet = api;
    global.clearCharacterManagement = clearCharacterSheetState;
    global.reloadCharacterManagement = reloadCharacterSheet;
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);

