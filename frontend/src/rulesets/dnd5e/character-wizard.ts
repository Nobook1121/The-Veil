// D&D 5e 建卡向导（购点 / 掷骰 / 标准数组 + 种族/职业/背景 + 速览四盒）。
//
// 四步：概念 → 属性 → 技能/装备 → 预览。第 4 步「创建」直接把 sheet 交给
// 现有的 pack / apply_save 往返（PUT /api/characters/<id>，level=1、xp=0），
// 之后卡面显示的一切派生值都由插件 schema.derive 重算，向导只负责给出同口径的
// 输入和**同口径的预览**：
//   - 计算口径的权威实现是插件的 dnd5e_plugin/builder.py（有 pytest 对照
//     schema.derive 的用例），本文件里的算法是它在浏览器里的镜像；
//   - 预览与保存共用 buildDraftSheet()，所以「预览看到的」就是「写进卡的」。
//
// 数据源：优先 kiwee 深库（/api/plugins/dnd5e/rules/search|entry，种族 160 /
// 背景 171 条，带中文名与来源码），深库未建时回落到本文件内置的 SRD 5.1 表
// （9 种族 / 12 职业 / 13 背景），并在界面明确标注「深库未建」。
//
// 这是规则集私有模块：对外只暴露 window.TrpgDnd5eCharacterWizard.open()，
// 由 rulesets/dnd5e/character-sheet.ts 在「新建角色卡」时调用。

type Dnd5eWizardAbility = "str" | "dex" | "con" | "int" | "wis" | "cha";
type Dnd5eWizardMode = "pointbuy" | "roll" | "standard";
type Dnd5eWizardStep = 1 | 2 | 3 | 4;

interface Dnd5eWizardRace {
    id: string;
    name: string;
    source: string;
    speed: number;
    bonuses: Partial<Record<Dnd5eWizardAbility, number>>;
    note?: string;
}

interface Dnd5eWizardClass {
    id: string;
    name: string;
    hitDie: number;
    saves: Dnd5eWizardAbility[];
    /** 技能熟练可选项数量（职业规定）。 */
    skillCount: number;
    skillPool: string[];
    /** 施法属性；空串表示非施法者。 */
    casting: Dnd5eWizardAbility | "";
    /** 1 级法术位数：完整施法者 2、契约魔法 1、1 级尚无法术位 0。 */
    slots1: number;
    armor: string[];
    weapons: string[];
    kit: string[];
}

interface Dnd5eWizardBackground {
    id: string;
    name: string;
    source: string;
    skills: string[];
}

interface Dnd5eWizardDeepItem {
    uid: string;
    name: string;
    engName: string;
    source: string;
    category: string;
}

interface Dnd5eWizardOptions {
    /** 落库成功后回传保存后的原始卡，由规则集模块刷新列表并打开详情。 */
    onCreated?: (card: Record<string, unknown>) => void;
    /** 「高级：直接填写表单」入口，交回旧通用表单。 */
    onAdvanced?: () => void;
}

interface Dnd5eWizardApi {
    open: (options?: Dnd5eWizardOptions) => void;
    close: () => void;
    isOpen: () => boolean;
}

interface Window {
    TrpgDnd5eCharacterWizard?: Dnd5eWizardApi;
}

(function installDnd5eCharacterWizard(global: Window & typeof globalThis): void {
    "use strict";

    const RULESET_ID = "dnd5e";
    const RULES_API = "/api/plugins/dnd5e/rules";

    const ABILITIES: Array<{ key: Dnd5eWizardAbility; short: string; label: string }> = [
        { key: "str", short: "STR", label: "力量" },
        { key: "dex", short: "DEX", label: "敏捷" },
        { key: "con", short: "CON", label: "体质" },
        { key: "int", short: "INT", label: "智力" },
        { key: "wis", short: "WIS", label: "感知" },
        { key: "cha", short: "CHA", label: "魅力" },
    ];

    const ABILITY_KEYS = ABILITIES.map((item) => item.key);

    /** 与插件 rules.SKILLS 同源：熟练列表存储英文 id（中文写法后端不认）。 */
    const SKILLS: Array<{ key: string; label: string; ability: Dnd5eWizardAbility }> = [
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

    const ALL_SKILL_IDS = SKILLS.map((item) => item.key);

    function skillLabel(key: string): string {
        return SKILLS.find((item) => item.key === key)?.label || key;
    }

    // --- 规则数字（与 dnd5e_plugin/builder.py 逐项一致）----------------------

    const POINT_BUY_COST: Record<number, number> = { 8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9 };
    const POINT_BUY_BUDGET = 27;
    const POINT_BUY_FLOOR = 8;
    const POINT_BUY_CEIL = 15;
    const STANDARD_ARRAY = [15, 14, 13, 12, 10, 8];

    // --- 内置 SRD 5.1 数据（深库未建时的回落，也是「深库检索」的默认值来源）---

    const SRD_RACES: Dnd5eWizardRace[] = [
        { id: "dwarf", name: "矮人", source: "SRD 5.1", speed: 25, bonuses: { con: 2 } },
        { id: "elf", name: "精灵", source: "SRD 5.1", speed: 30, bonuses: { dex: 2 } },
        { id: "halfling", name: "半身人", source: "SRD 5.1", speed: 25, bonuses: { dex: 2 } },
        { id: "human", name: "人类", source: "SRD 5.1", speed: 30, bonuses: { str: 1, dex: 1, con: 1, int: 1, wis: 1, cha: 1 } },
        { id: "dragonborn", name: "龙裔", source: "SRD 5.1", speed: 30, bonuses: { str: 2, cha: 1 } },
        { id: "gnome", name: "侏儒", source: "SRD 5.1", speed: 25, bonuses: { int: 2 } },
        {
            id: "half-elf",
            name: "半精灵",
            source: "SRD 5.1",
            speed: 30,
            bonuses: { cha: 2 },
            note: "魅力 +2，另选两项各 +1（可在属性步手动补）",
        },
        { id: "half-orc", name: "半兽人", source: "SRD 5.1", speed: 30, bonuses: { str: 2, con: 1 } },
        { id: "tiefling", name: "提夫林", source: "SRD 5.1", speed: 30, bonuses: { cha: 2, int: 1 } },
    ];

    const SRD_CLASSES: Dnd5eWizardClass[] = [
        {
            id: "barbarian",
            name: "野蛮人",
            hitDie: 12,
            saves: ["str", "con"],
            skillCount: 2,
            skillPool: ["animal-handling", "athletics", "intimidation", "nature", "perception", "survival"],
            casting: "",
            slots1: 0,
            armor: ["light", "medium", "shield"],
            weapons: ["simple", "martial"],
            kit: ["巨斧（或任意军用武器）", "两支手斧", "探索套组", "四支标枪"],
        },
        {
            id: "bard",
            name: "吟游诗人",
            hitDie: 8,
            saves: ["dex", "cha"],
            skillCount: 3,
            skillPool: [...ALL_SKILL_IDS],
            casting: "cha",
            slots1: 2,
            armor: ["light"],
            weapons: ["simple", "hand-crossbow", "longsword", "rapier", "shortsword"],
            kit: ["皮甲", "匕首", "乐器（鲁特琴）", "艺人套组"],
        },
        {
            id: "cleric",
            name: "牧师",
            hitDie: 8,
            saves: ["wis", "cha"],
            skillCount: 2,
            skillPool: ["history", "insight", "medicine", "persuasion", "religion"],
            casting: "wis",
            slots1: 2,
            armor: ["light", "medium", "shield"],
            weapons: ["simple"],
            kit: ["鳞甲", "钉头锤", "盾牌", "圣徽", "祭司套组"],
        },
        {
            id: "druid",
            name: "德鲁伊",
            hitDie: 8,
            saves: ["int", "wis"],
            skillCount: 2,
            skillPool: ["arcana", "animal-handling", "insight", "medicine", "nature", "perception", "religion", "survival"],
            casting: "wis",
            slots1: 2,
            armor: ["light", "medium", "shield"],
            weapons: ["simple"],
            kit: ["皮甲", "德鲁伊法器", "探索套组"],
        },
        {
            id: "fighter",
            name: "战士",
            hitDie: 10,
            saves: ["str", "con"],
            skillCount: 2,
            skillPool: ["acrobatics", "animal-handling", "athletics", "history", "insight", "intimidation", "perception", "survival"],
            casting: "",
            slots1: 0,
            armor: ["light", "medium", "heavy", "shield"],
            weapons: ["simple", "martial"],
            kit: ["链甲", "军用武器", "盾牌", "探索套组"],
        },
        {
            id: "monk",
            name: "武僧",
            hitDie: 8,
            saves: ["str", "dex"],
            skillCount: 2,
            skillPool: ["acrobatics", "athletics", "history", "insight", "religion", "stealth"],
            casting: "",
            slots1: 0,
            armor: [],
            weapons: ["simple", "shortsword"],
            kit: ["短剑", "十支飞镖", "探索套组"],
        },
        {
            id: "paladin",
            name: "圣武士",
            hitDie: 10,
            saves: ["wis", "cha"],
            skillCount: 2,
            skillPool: ["athletics", "insight", "intimidation", "medicine", "persuasion", "religion"],
            casting: "cha",
            slots1: 0,
            armor: ["light", "medium", "heavy", "shield"],
            weapons: ["simple", "martial"],
            kit: ["链甲", "战锤", "盾牌", "圣徽", "探索套组"],
        },
        {
            id: "ranger",
            name: "游侠",
            hitDie: 10,
            saves: ["str", "dex"],
            skillCount: 3,
            skillPool: ["animal-handling", "athletics", "insight", "investigation", "nature", "perception", "stealth", "survival"],
            casting: "wis",
            slots1: 0,
            armor: ["light", "medium", "shield"],
            weapons: ["simple", "martial"],
            kit: ["鳞甲", "短弓", "两支短剑", "探索套组"],
        },
        {
            id: "rogue",
            name: "游荡者",
            hitDie: 8,
            saves: ["dex", "int"],
            skillCount: 4,
            skillPool: [
                "acrobatics", "athletics", "deception", "insight", "intimidation", "investigation",
                "perception", "performance", "persuasion", "sleight-of-hand", "stealth",
            ],
            casting: "",
            slots1: 0,
            armor: ["light"],
            weapons: ["simple", "hand-crossbow", "longsword", "rapier", "shortsword"],
            kit: ["皮甲", "匕首", "盗贼工具", "探索套组"],
        },
        {
            id: "sorcerer",
            name: "术士",
            hitDie: 6,
            saves: ["con", "cha"],
            skillCount: 2,
            skillPool: ["arcana", "deception", "insight", "intimidation", "persuasion", "religion"],
            casting: "cha",
            slots1: 2,
            armor: [],
            weapons: ["dagger", "dart", "sling", "quarterstaff", "light-crossbow"],
            kit: ["匕首", "奥术法器", "地城套组"],
        },
        {
            id: "warlock",
            name: "邪术师",
            hitDie: 8,
            saves: ["wis", "cha"],
            skillCount: 2,
            skillPool: ["arcana", "deception", "history", "intimidation", "investigation", "nature", "religion"],
            casting: "cha",
            slots1: 1,
            armor: ["light"],
            weapons: ["simple"],
            kit: ["皮甲", "奥术法器", "学者套组"],
        },
        {
            id: "wizard",
            name: "法师",
            hitDie: 6,
            saves: ["int", "wis"],
            skillCount: 2,
            skillPool: ["arcana", "history", "insight", "investigation", "medicine", "religion"],
            casting: "int",
            slots1: 2,
            armor: [],
            weapons: ["dagger", "dart", "sling", "quarterstaff", "light-crossbow"],
            kit: ["法术书", "材料包", "学者套组"],
        },
    ];

    const SRD_BACKGROUNDS: Dnd5eWizardBackground[] = [
        { id: "acolyte", name: "侍僧", source: "SRD 5.1", skills: ["insight", "religion"] },
        { id: "charlatan", name: "骗子", source: "SRD 5.1", skills: ["deception", "sleight-of-hand"] },
        { id: "criminal", name: "罪犯", source: "SRD 5.1", skills: ["deception", "stealth"] },
        { id: "entertainer", name: "艺人", source: "SRD 5.1", skills: ["acrobatics", "performance"] },
        { id: "folk-hero", name: "民间英雄", source: "SRD 5.1", skills: ["animal-handling", "survival"] },
        { id: "guild-artisan", name: "公会工匠", source: "SRD 5.1", skills: ["insight", "persuasion"] },
        { id: "hermit", name: "隐士", source: "SRD 5.1", skills: ["medicine", "religion"] },
        { id: "noble", name: "贵族", source: "SRD 5.1", skills: ["history", "persuasion"] },
        { id: "outlander", name: "化外之民", source: "SRD 5.1", skills: ["athletics", "survival"] },
        { id: "sage", name: "智者", source: "SRD 5.1", skills: ["arcana", "history"] },
        { id: "sailor", name: "水手", source: "SRD 5.1", skills: ["athletics", "perception"] },
        { id: "soldier", name: "士兵", source: "SRD 5.1", skills: ["athletics", "intimidation"] },
        { id: "urchin", name: "街头顽童", source: "SRD 5.1", skills: ["sleight-of-hand", "stealth"] },
    ];

    const ARMOR_LABELS: Record<string, string> = { light: "轻甲", medium: "中甲", heavy: "重甲", shield: "盾牌" };
    const WEAPON_LABELS: Record<string, string> = {
        simple: "简易武器",
        martial: "军用武器",
        dagger: "匕首",
        dart: "飞镖",
        sling: "投石索",
        quarterstaff: "长棍",
        "light-crossbow": "轻弩",
        "hand-crossbow": "手弩",
        longsword: "长剑",
        shortsword: "短剑",
        rapier: "刺剑",
    };

    /** 起始装备默认重量（取自 dnd5e-srd equipment.json；没有就记 0）。 */
    const EQUIPMENT_WEIGHTS: Record<string, number> = {
        巨斧: 7, 巨剑: 6, 长剑: 3, 短剑: 2, 刺剑: 2, 战锤: 2, 钉头锤: 4, 短棒: 2, 长棍: 4,
        匕首: 1, 手斧: 2, 标枪: 2, 矛: 3, 短弓: 2, 长弓: 2, 轻弩: 5, 飞镖: 0.25,
        皮甲: 10, 镶钉皮甲: 13, 锁子衫: 20, 鳞甲: 45, 链甲: 55, 胸甲: 20, 盾牌: 6,
        背包: 5, 铺盖: 7, 口粮: 2, 水袋: 5, 火把: 1, 盗贼工具: 1, 医疗包: 3, 法术书: 3,
        材料包: 2, 奥术法器: 1, 圣徽: 1,
    };

    const STEP_TITLES: Array<{ step: Dnd5eWizardStep; label: string; icon: string }> = [
        { step: 1, label: "概念", icon: "fa-bookmark" },
        { step: 2, label: "属性", icon: "fa-dice-d20" },
        { step: 3, label: "技能与装备", icon: "fa-suitcase" },
        { step: 4, label: "速览", icon: "fa-id-badge" },
    ];

    // --- 状态 -----------------------------------------------------------------

    interface WizardState {
        step: Dnd5eWizardStep;
        name: string;
        race: Dnd5eWizardRace | null;
        klass: Dnd5eWizardClass | null;
        background: Dnd5eWizardBackground | null;
        mode: Dnd5eWizardMode;
        /** 购点模式：基础值（8–15），三模式各自持有自己的状态，切换不丢。 */
        pointBuy: Record<Dnd5eWizardAbility, number>;
        /** 标准数组：每个属性占用的数组值。 */
        standard: Record<Dnd5eWizardAbility, number>;
        /** 掷骰：六组 4d6 去最低的成绩池。 */
        rolled: number[];
        /** 掷骰：属性 → 成绩池下标（null 表示还没分配）。 */
        rollAssign: Record<Dnd5eWizardAbility, number | null>;
        /** 种族加值（默认取种族表，可手动改：半精灵自选、深库无数据时补）。 */
        raceBonuses: Partial<Record<Dnd5eWizardAbility, number>>;
        /** 职业技能熟练（英文 id）。 */
        skillPicks: string[];
        /** 起始装备（每行：名称 x数量）。 */
        equipmentText: string;
        /** 深库检索：当前类目与关键字/结果。 */
        deepCategory: "race" | "background";
        deepQuery: string;
        deepResults: Dnd5eWizardDeepItem[];
        deepLoading: boolean;
        deepMessage: string;
    }

    let state: WizardState | null = null;
    let options: Dnd5eWizardOptions = {};
    let modal: HTMLElement | null = null;
    let instance: BootstrapModalInstance | null = null;
    let saving = false;
    let deepAvailable: boolean | null = null;
    let deepProbe: Promise<boolean> | null = null;
    let deepTimer: number | null = null;
    /** 重渲染后要恢复焦点的元素 token（键盘用户不丢位置）。 */
    let focusToken = "";

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

    function abilityModifier(score: number): number {
        return Math.floor((score - 10) / 2);
    }

    function proficiencyBonus(level: number): number {
        return 2 + Math.floor((Math.max(1, Math.min(20, level)) - 1) / 4);
    }

    function clampPointBuy(value: number): number {
        if (!Number.isFinite(value)) return POINT_BUY_FLOOR;
        return Math.max(POINT_BUY_FLOOR, Math.min(POINT_BUY_CEIL, Math.round(value)));
    }

    function pointBuyCost(score: number): number {
        return POINT_BUY_COST[clampPointBuy(score)] ?? 0;
    }

    function pointBuySpent(scores: Record<Dnd5eWizardAbility, number>): number {
        return ABILITY_KEYS.reduce((sum, key) => sum + pointBuyCost(scores[key]), 0);
    }

    function pointBuyRemaining(scores: Record<Dnd5eWizardAbility, number>): number {
        return POINT_BUY_BUDGET - pointBuySpent(scores);
    }

    function canRaiseAbility(scores: Record<Dnd5eWizardAbility, number>, key: Dnd5eWizardAbility): boolean {
        if (clampPointBuy(scores[key]) >= POINT_BUY_CEIL) return false;
        const next: Record<Dnd5eWizardAbility, number> = { ...scores };
        next[key] = clampPointBuy(scores[key]) + 1;
        return pointBuySpent(next) <= POINT_BUY_BUDGET;
    }

    /** 4d6 去最低：入参四颗骰子，返回最高三颗之和（与 builder.roll_4d6_drop_lowest 同）。 */
    function roll4d6DropLowest(dice: number[]): number {
        const sorted = [...dice].sort((left, right) => right - left);
        return sorted.slice(0, 3).reduce((sum, value) => sum + value, 0);
    }

    function rollPool(): number[] {
        const pool: number[] = [];
        for (let index = 0; index < 6; index += 1) {
            const dice: number[] = [];
            for (let die = 0; die < 4; die += 1) dice.push(1 + Math.floor(Math.random() * 6));
            pool.push(roll4d6DropLowest(dice));
        }
        return pool;
    }

    function emptyAbilities(): Record<Dnd5eWizardAbility, number> {
        const scores = {} as Record<Dnd5eWizardAbility, number>;
        ABILITY_KEYS.forEach((key) => { scores[key] = POINT_BUY_FLOOR; });
        return scores;
    }

    function standardBaseline(): Record<Dnd5eWizardAbility, number> {
        const scores = {} as Record<Dnd5eWizardAbility, number>;
        ABILITY_KEYS.forEach((key, index) => { scores[key] = STANDARD_ARRAY[index] ?? POINT_BUY_FLOOR; });
        return scores;
    }

    function emptyRollAssign(): Record<Dnd5eWizardAbility, number | null> {
        const assign = {} as Record<Dnd5eWizardAbility, number | null>;
        ABILITY_KEYS.forEach((key) => { assign[key] = null; });
        return assign;
    }

    function freshState(): WizardState {
        return {
            step: 1,
            name: "",
            race: null,
            klass: null,
            background: null,
            mode: "pointbuy",
            pointBuy: emptyAbilities(),
            standard: standardBaseline(),
            rolled: rollPool(),
            rollAssign: emptyRollAssign(),
            raceBonuses: {},
            skillPicks: [],
            equipmentText: "",
            deepCategory: "race",
            deepQuery: "",
            deepResults: [],
            deepLoading: false,
            deepMessage: "",
        };
    }

    /** 当前模式下的**基础值**（不含种族加值）。掷骰未分配记 0，并在 UI 标注。 */
    function baseScores(current: WizardState): Record<Dnd5eWizardAbility, number> {
        if (current.mode === "standard") return { ...current.standard };
        if (current.mode === "roll") {
            const scores = {} as Record<Dnd5eWizardAbility, number>;
            ABILITY_KEYS.forEach((key) => {
                const index = current.rollAssign[key];
                scores[key] = index === null || index === undefined ? 0 : (current.rolled[index] ?? 0);
            });
            return scores;
        }
        return { ...current.pointBuy };
    }

    /** 基础值 + 种族加值 = 总值（与 builder.with_race_bonuses 同口径）。 */
    function totalScores(current: WizardState): Record<Dnd5eWizardAbility, number> {
        const base = baseScores(current);
        const totals = {} as Record<Dnd5eWizardAbility, number>;
        ABILITY_KEYS.forEach((key) => {
            const bonus = Number(current.raceBonuses[key]) || 0;
            totals[key] = Math.max(1, Math.min(30, base[key] + bonus));
        });
        return totals;
    }

    function unassignedCount(current: WizardState): number {
        if (current.mode !== "roll") return 0;
        return ABILITY_KEYS.filter((key) => current.rollAssign[key] === null).length;
    }

    function rollPoolUsed(current: WizardState, index: number): boolean {
        return ABILITY_KEYS.some((key) => current.rollAssign[key] === index);
    }

    // --- 草稿 sheet 与预览 -----------------------------------------------------

    function normalizeSkillId(value: unknown): string {
        const raw = String(value ?? "").trim().toLowerCase().replace(/_/g, " ").replace(/-/g, " ");
        const key = raw.split(/\s+/).filter(Boolean).join("-");
        if (ALL_SKILL_IDS.includes(key)) return key;
        const byLabel = SKILLS.find((item) => item.label === String(value ?? "").trim());
        return byLabel ? byLabel.key : key;
    }

    function parseEquipment(text: string): Array<{ name: string; quantity: number; weight: number; equipped: boolean }> {
        return text
            .split(/\n+/)
            .map((line) => line.trim())
            .filter(Boolean)
            .map((line) => {
                // 「名称 x数量」；重量按内置表兜底（dnd5e-srd equipment 里有值的都在表里）。
                const matched = line.match(/^(.*?)\s*[x×]\s*(\d+)$/i);
                const name = String(matched?.[1] || line).trim();
                const quantity = Math.max(1, Number(matched?.[2]) || 1);
                const weight = EQUIPMENT_WEIGHTS[name] ?? 0;
                return { name, quantity, weight, equipped: false };
            });
    }

    /** 技能熟练 = 职业选择 + 背景自带（背景熟练不可取消，与规则一致）。 */
    function proficientSkills(current: WizardState): string[] {
        const result: string[] = [];
        const push = (value: string): void => {
            const key = normalizeSkillId(value);
            if (key && !result.includes(key)) result.push(key);
        };
        (current.background?.skills || []).forEach(push);
        current.skillPicks.forEach(push);
        return result;
    }

    function buildDraftSheet(current: WizardState): Record<string, unknown> {
        const totals = totalScores(current);
        const hitDie = current.klass?.hitDie ?? 8;
        const conMod = abilityModifier(totals.con);
        const maxHp = Math.max(1, hitDie + conMod);
        const klass = current.klass;
        const slots: Record<string, { max: number; used: number }> = {};
        if (klass && klass.slots1 > 0) slots["1"] = { max: klass.slots1, used: 0 };
        return {
            level: 1,
            class: klass?.name || "",
            subclass: "",
            race: current.race?.name || "",
            background: current.background?.name || "",
            alignment: "",
            xp: 0,
            abilities: totals,
            proficiencies: {
                saves: klass ? [...klass.saves] : [],
                skills: proficientSkills(current),
                expertise: [],
                armor: (klass?.armor || []).map((id) => ARMOR_LABELS[id] || id),
                weapons: (klass?.weapons || []).map((id) => WEAPON_LABELS[id] || id),
                tools: [],
                languages: [],
            },
            hp: { max: maxHp, current: maxHp, temp: 0 },
            speed: current.race?.speed ?? 30,
            hitDice: `1d${hitDie}`,
            hit_dice: { die: hitDie, max: 1, used: 0 },
            spellcasting: {
                ability: klass?.casting || "",
                known: [],
                prepared: [],
                slots,
            },
            equipment: parseEquipment(current.equipmentText),
            features: [],
            notes: "",
            bio: "",
            appearance: "",
            token: "",
            status: [] as string[],
            deathSaves: { success: 0, failure: 0 },
            exhaustion: 0,
            inspiration: false,
        };
    }

    interface WizardPreview {
        abilities: Record<Dnd5eWizardAbility, number>;
        modifiers: Record<Dnd5eWizardAbility, number>;
        maxHp: number;
        armorClass: number;
        proficiencyBonus: number;
        initiative: number;
        passivePerception: number;
        spellSaveDC: number | null;
        spellAttackBonus: number | null;
        slots: Record<string, { max: number; used: number }>;
        casterPending: boolean;
    }

    /** 只读速览：口径与 builder.preview / schema.derive 一致。 */
    function previewOf(sheet: Record<string, unknown>): WizardPreview {
        const abilities = (sheet.abilities || {}) as Record<Dnd5eWizardAbility, number>;
        const modifiers = {} as Record<Dnd5eWizardAbility, number>;
        ABILITY_KEYS.forEach((key) => { modifiers[key] = abilityModifier(Number(abilities[key]) || 10); });
        const proficiencies = (sheet.proficiencies || {}) as { skills?: string[] };
        const skills = new Set((proficiencies.skills || []).map((item) => normalizeSkillId(item)));
        const hp = (sheet.hp || {}) as { max?: number };
        const casting = (sheet.spellcasting || {}) as { ability?: string; slots?: Record<string, { max: number; used: number }> };
        const castingAbility = String(casting.ability || "").toLowerCase();
        const prof = proficiencyBonus(1);
        const spellBonus = castingAbility ? prof + (modifiers[castingAbility as Dnd5eWizardAbility] ?? 0) : null;
        return {
            abilities,
            modifiers,
            maxHp: Math.max(1, Number(hp.max) || 1),
            armorClass: 10 + modifiers.dex,
            proficiencyBonus: prof,
            initiative: modifiers.dex,
            passivePerception: 10 + modifiers.wis + (skills.has("perception") ? prof : 0),
            spellSaveDC: spellBonus === null ? null : 8 + spellBonus,
            spellAttackBonus: spellBonus,
            slots: casting.slots || {},
            casterPending: Boolean(castingAbility) && Object.keys(casting.slots || {}).length === 0,
        };
    }

    // --- 深库（kiwee）探针与检索 -----------------------------------------------

    async function probeDeepDb(): Promise<boolean> {
        if (deepAvailable !== null) return deepAvailable;
        if (deepProbe) return deepProbe;
        deepProbe = (async () => {
            try {
                const response = await TrpgApi.get<ApiResponse<{ available?: boolean }>>(
                    `${RULES_API}/search?category=race&limit=1`
                );
                deepAvailable = response?.success === true && response?.data?.available === true;
            } catch {
                deepAvailable = false;
            }
            return deepAvailable;
        })();
        return deepProbe;
    }

    function deepItemFromRow(row: Record<string, unknown>): Dnd5eWizardDeepItem {
        return {
            uid: String(row.uid || ""),
            name: String(row.name || "").trim(),
            engName: String(row.eng_name || "").trim(),
            source: String(row.source || "").trim(),
            category: String(row.category || "").trim(),
        };
    }

    async function runDeepSearch(query: string): Promise<void> {
        const current = state;
        if (!current) return;
        const keyword = query.trim();
        if (!keyword) {
            current.deepResults = [];
            current.deepMessage = "";
            current.deepLoading = false;
            renderBody();
            return;
        }
        current.deepLoading = true;
        current.deepMessage = "";
        renderBody();
        try {
            const response = await TrpgApi.get<ApiResponse<{ available?: boolean; results?: Record<string, unknown>[] }>>(
                `${RULES_API}/search?category=${encodeURIComponent(current.deepCategory)}&q=${encodeURIComponent(keyword)}&limit=10`
            );
            if (!state || state !== current) return;
            if (!response?.success) {
                current.deepResults = [];
                current.deepMessage = response?.message || "深库检索失败";
            } else if (response.data?.available === false) {
                deepAvailable = false;
                current.deepResults = [];
                current.deepMessage = "深库未建，已回落到内置 SRD 表。";
            } else {
                deepAvailable = true;
                current.deepResults = (response.data?.results || []).map(deepItemFromRow).filter((item) => item.uid && item.name);
                current.deepMessage = current.deepResults.length ? "" : "深库中没有匹配条目。";
            }
        } catch (error) {
            if (!state || state !== current) return;
            current.deepResults = [];
            current.deepMessage = `深库检索失败：${String((error as Error)?.message || error)}`;
        } finally {
            if (state === current) {
                current.deepLoading = false;
                renderBody();
            }
        }
    }

    function raceFromDeep(item: Dnd5eWizardDeepItem): Dnd5eWizardRace {
        return { id: item.uid, name: item.name, source: item.source || "深库", speed: 30, bonuses: {} };
    }

    function backgroundFromDeep(item: Dnd5eWizardDeepItem): Dnd5eWizardBackground {
        return { id: item.uid, name: item.name, source: item.source || "深库", skills: [] };
    }

    /** 从深库 raw 里读种族属性加值与速度；读不到就留空让玩家手填。 */
    function parseRaceRaw(raw: Record<string, unknown>): { bonuses: Partial<Record<Dnd5eWizardAbility, number>>; speed: number | null } {
        const bonuses: Partial<Record<Dnd5eWizardAbility, number>> = {};
        const ability = raw?.ability;
        const entries = Array.isArray(ability) ? ability : [];
        entries.forEach((entry) => {
            if (!entry || typeof entry !== "object") return;
            Object.entries(entry as Record<string, unknown>).forEach(([key, value]) => {
                const normalized = String(key).toLowerCase();
                if (!ABILITY_KEYS.includes(normalized as Dnd5eWizardAbility)) return;
                const amount = Number(value);
                if (Number.isFinite(amount) && amount !== 0) {
                    bonuses[normalized as Dnd5eWizardAbility] = (bonuses[normalized as Dnd5eWizardAbility] || 0) + amount;
                }
            });
        });
        const speed = Number(raw?.speed);
        return { bonuses, speed: Number.isFinite(speed) && speed > 0 ? speed : null };
    }

    async function pickDeepRace(item: Dnd5eWizardDeepItem): Promise<void> {
        const current = state;
        if (!current) return;
        const race = raceFromDeep(item);
        current.race = race;
        current.raceBonuses = {};
        current.deepResults = [];
        current.deepQuery = "";
        current.deepMessage = "";
        renderBody();
        try {
            const response = await TrpgApi.get<ApiResponse<{ raw?: Record<string, unknown> }>>(
                `${RULES_API}/entry/race/${encodeURIComponent(item.uid)}`
            );
            if (!state || state !== current || current.race?.id !== race.id) return;
            const raw = response?.data?.raw;
            if (!response?.success || !raw) {
                current.race.note = "深库没有给出属性加值，请在第 2 步手动填写种族加值。";
                renderBody();
                return;
            }
            const parsed = parseRaceRaw(raw);
            current.raceBonuses = parsed.bonuses;
            if (parsed.speed) current.race.speed = parsed.speed;
            if (!Object.keys(parsed.bonuses).length) {
                current.race.note = "该条目没有固定属性加值（可能是自选型），请在第 2 步手动补。";
            }
            renderBody();
        } catch {
            if (!state || state !== current) return;
            current.race.note = "深库条目读取失败，请在第 2 步手动填写种族加值。";
            renderBody();
        }
    }

    async function pickDeepBackground(item: Dnd5eWizardDeepItem): Promise<void> {
        const current = state;
        if (!current) return;
        current.background = backgroundFromDeep(item);
        current.deepResults = [];
        current.deepQuery = "";
        current.deepMessage = "深库背景条目不解析技能熟练；如需熟练请在第 3 步手动勾选。";
        renderBody();
    }

    // --- 渲染 -----------------------------------------------------------------

    function renderStepper(current: WizardState): string {
        return `
            <ol class="dnd5e-wiz-steps" aria-label="建卡进度">
                ${STEP_TITLES.map((item) => `
                    <li class="dnd5e-wiz-step ${current.step === item.step ? "is-active" : ""} ${current.step > item.step ? "is-done" : ""}"
                        ${current.step === item.step ? 'aria-current="step"' : ""}>
                        <span class="dnd5e-wiz-step-index">${current.step > item.step ? '<i class="fa fa-check" aria-hidden="true"></i>' : item.step}</span>
                        <span class="dnd5e-wiz-step-label"><i class="fa ${item.icon}" aria-hidden="true"></i> ${escapeHtml(item.label)}</span>
                    </li>`).join("")}
            </ol>`;
    }

    function renderRaceSelect(current: WizardState): string {
        const selected = current.race;
        const optionsHtml = SRD_RACES.map((race) => `
            <option value="${escapeHtml(race.id)}" ${selected?.id === race.id ? "selected" : ""}>
                ${escapeHtml(race.name)}（${escapeHtml(race.source)}）
            </option>`).join("");
        const deepBadge = deepAvailable === false
            ? `<span class="dnd5e-wiz-badge is-muted">深库未建 · 使用内置 SRD 表</span>`
            : `<span class="dnd5e-wiz-badge">深库可检索</span>`;
        const custom = selected && !SRD_RACES.some((race) => race.id === selected.id)
            ? `<div class="dnd5e-wiz-picked">已选：<strong>${escapeHtml(selected.name)}</strong>
                 <small>${escapeHtml(selected.source)}</small>
                 <button type="button" class="dnd5e-wiz-link" data-wiz-action="clear-race">改回 SRD 种族</button>
               </div>
               ${selected.note ? `<p class="dnd5e-wiz-hint">${escapeHtml(selected.note)}</p>` : ""}`
            : "";
        return `
            <div class="dnd5e-wiz-field">
                <label for="dnd5eWizRace">种族 ${deepBadge}</label>
                <select id="dnd5eWizRace" data-wiz-field="race">
                    <option value="">— 请选择种族 —</option>
                    ${optionsHtml}
                </select>
                ${custom}
                ${renderDeepSearch(current, "race")}
            </div>`;
    }

    function renderBackgroundSelect(current: WizardState): string {
        const selected = current.background;
        const optionsHtml = SRD_BACKGROUNDS.map((item) => `
            <option value="${escapeHtml(item.id)}" ${selected && selected.id === item.id ? "selected" : ""}>
                ${escapeHtml(item.name)}（${escapeHtml(item.source)}）
            </option>`).join("");
        const picked = selected && !SRD_BACKGROUNDS.some((item) => item.id === selected.id)
            ? `<div class="dnd5e-wiz-picked">已选：<strong>${escapeHtml(selected.name)}</strong>
                 <small>${escapeHtml(selected.source)}</small>
                 <button type="button" class="dnd5e-wiz-link" data-wiz-action="clear-background">改回 SRD 背景</button>
               </div>`
            : "";
        return `
            <div class="dnd5e-wiz-field">
                <label for="dnd5eWizBackground">背景</label>
                <select id="dnd5eWizBackground" data-wiz-field="background">
                    <option value="">— 不选背景 —</option>
                    ${optionsHtml}
                </select>
                ${picked}
                ${renderDeepSearch(current, "background")}
            </div>`;
    }

    function renderDeepSearch(current: WizardState, category: "race" | "background"): string {
        if (deepAvailable !== true) return "";
        const active = current.deepCategory === category;
        const panel = active
            ? `<div class="dnd5e-wiz-deep">
                   <div class="dnd5e-wiz-deep-row">
                       <input type="search" data-wiz-field="deepQuery" value="${escapeHtml(current.deepQuery)}"
                           placeholder="${category === "race" ? "检索深库种族（中文/英文名）" : "检索深库背景"}"
                           aria-label="深库检索关键字">
                       <button type="button" class="dnd5e-btn" data-wiz-action="deep-search">检索</button>
                   </div>
                   ${current.deepLoading ? `<p class="dnd5e-wiz-hint">正在检索深库…</p>` : ""}
                   ${current.deepMessage ? `<p class="dnd5e-wiz-hint">${escapeHtml(current.deepMessage)}</p>` : ""}
                   ${current.deepResults.length ? `
                       <ul class="dnd5e-wiz-deep-list">
                           ${current.deepResults.map((item) => `
                               <li>
                                   <button type="button" class="dnd5e-wiz-deep-item" data-wiz-action="deep-pick"
                                       data-wiz-uid="${escapeHtml(item.uid)}" data-wiz-category="${escapeHtml(item.category)}">
                                       <span>${escapeHtml(item.name)}</span>
                                       <small>${escapeHtml(item.engName || "")} · ${escapeHtml(item.source || "")}</small>
                                   </button>
                               </li>`).join("")}
                       </ul>` : ""}
               </div>`
            : "";
        return `
            <div class="dnd5e-wiz-deep-toggle">
                <button type="button" class="dnd5e-wiz-link" data-wiz-action="deep-open" data-wiz-category="${category}">
                    <i class="fa fa-database" aria-hidden="true"></i> 从深库${category === "race" ? "检索种族" : "检索背景"}
                </button>
            </div>
            ${panel}`;
    }

    function renderClassPicker(current: WizardState): string {
        const selected = current.klass;
        return `
            <div class="dnd5e-wiz-field">
                <label>职业（12 职，SRD 5.1）</label>
                <div class="dnd5e-wiz-class-grid" role="radiogroup" aria-label="选择职业">
                    ${SRD_CLASSES.map((item) => `
                        <button type="button" role="radio" aria-checked="${selected?.id === item.id}"
                            class="dnd5e-wiz-class ${selected?.id === item.id ? "is-selected" : ""}"
                            data-wiz-action="class" data-wiz-class="${escapeHtml(item.id)}">
                            <strong>${escapeHtml(item.name)}</strong>
                            <small>生命骰 d${item.hitDie} · ${item.skillCount} 项技能${item.casting ? ` · 施法（${escapeHtml(item.casting.toUpperCase())}）` : ""}</small>
                        </button>`).join("")}
                </div>
                ${selected ? `<p class="dnd5e-wiz-hint">${escapeHtml(selected.name)}：豁免熟练
                    ${selected.saves.map((key) => escapeHtml(key.toUpperCase())).join(" / ")}，可用技能 ${selected.skillCount} 项。</p>` : ""}
            </div>`;
    }

    function renderStepConcept(current: WizardState): string {
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-user" aria-hidden="true"></i> 你是谁</h4>
                <div class="dnd5e-wiz-field">
                    <label for="dnd5eWizName">角色名 <span class="dnd5e-wiz-required">必填</span></label>
                    <input id="dnd5eWizName" type="text" data-wiz-field="name" maxlength="40"
                        value="${escapeHtml(current.name)}" placeholder="例如：银月游侠 艾拉" autocomplete="off">
                </div>
                ${renderRaceSelect(current)}
                ${renderClassPicker(current)}
                ${renderBackgroundSelect(current)}
            </section>`;
    }

    function renderModeTabs(current: WizardState): string {
        const modes: Array<{ key: Dnd5eWizardMode; label: string; hint: string }> = [
            { key: "pointbuy", label: "购点 27", hint: "8–15，可随时加减" },
            { key: "roll", label: "掷骰 4d6", hint: "六组去最低，可重掷" },
            { key: "standard", label: "标准数组", hint: "15/14/13/12/10/8" },
        ];
        return `
            <div class="dnd5e-wiz-mode-tabs" role="tablist" aria-label="属性生成方式">
                ${modes.map((mode) => `
                    <button type="button" role="tab" aria-selected="${current.mode === mode.key}"
                        class="dnd5e-wiz-mode ${current.mode === mode.key ? "is-active" : ""}"
                        data-wiz-action="mode" data-wiz-mode="${mode.key}">
                        <strong>${escapeHtml(mode.label)}</strong>
                        <small>${escapeHtml(mode.hint)}</small>
                    </button>`).join("")}
            </div>`;
    }

    function abilityRow(current: WizardState, key: Dnd5eWizardAbility, label: string, short: string, control: string, bonusCell: string): string {
        const base = baseScores(current)[key];
        const total = totalScores(current)[key];
        const bonus = Number(current.raceBonuses[key]) || 0;
        const missing = current.mode === "roll" && base === 0;
        return `
            <div class="dnd5e-wiz-ability" data-wiz-ability="${key}">
                <div class="dnd5e-wiz-ability-head">
                    <strong>${escapeHtml(label)}</strong><small>${escapeHtml(short)}</small>
                </div>
                <div class="dnd5e-wiz-ability-control">${control}</div>
                <div class="dnd5e-wiz-ability-bonus">${bonusCell}</div>
                <div class="dnd5e-wiz-ability-total">
                    <span class="dnd5e-wiz-total">${missing ? "—" : total}</span>
                    <small>${missing ? "未分配" : `基础 ${base}${bonus ? ` + 种族 ${bonus}` : ""} = ${total}`}</small>
                    <span class="dnd5e-wiz-mod">${missing ? "—" : signed(abilityModifier(total))}</span>
                </div>
            </div>`;
    }

    function renderPointBuyControls(current: WizardState): string {
        return ABILITIES.map((item) => {
            const value = current.pointBuy[item.key];
            const control = `
                <div class="dnd5e-wiz-stepper">
                    <button type="button" class="dnd5e-wiz-stepper-btn" data-wiz-action="pb-dec" data-wiz-ability="${item.key}"
                        aria-label="${escapeHtml(item.label)}减 1" ${value <= POINT_BUY_FLOOR ? "disabled" : ""}>−</button>
                    <output class="dnd5e-wiz-stepper-value">${value}</output>
                    <button type="button" class="dnd5e-wiz-stepper-btn" data-wiz-action="pb-inc" data-wiz-ability="${item.key}"
                        aria-label="${escapeHtml(item.label)}加 1" ${canRaiseAbility(current.pointBuy, item.key) ? "" : "disabled"}>+</button>
                </div>`;
            return abilityRow(current, item.key, item.label, item.short, control, renderBonusCell(current, item.key));
        }).join("");
    }

    function renderRollControls(current: WizardState): string {
        return ABILITIES.map((item) => {
            const assigned = current.rollAssign[item.key];
            const control = `
                <select class="dnd5e-wiz-roll-select" data-wiz-field="roll-assign" data-wiz-ability="${item.key}"
                    aria-label="${escapeHtml(item.label)}分配骰值">
                    <option value="">未分配</option>
                    ${current.rolled.map((value, index) => `
                        <option value="${index}" ${assigned === index ? "selected" : ""} ${rollPoolUsed(current, index) && assigned !== index ? "disabled" : ""}>
                            ${value}${rollPoolUsed(current, index) && assigned !== index ? "（已用）" : ""}
                        </option>`).join("")}
                </select>`;
            return abilityRow(current, item.key, item.label, item.short, control, renderBonusCell(current, item.key));
        }).join("");
    }

    function renderStandardControls(current: WizardState): string {
        return ABILITIES.map((item) => {
            const value = current.standard[item.key];
            const control = `
                <select class="dnd5e-wiz-roll-select" data-wiz-field="standard-assign" data-wiz-ability="${item.key}"
                    aria-label="${escapeHtml(item.label)}分配标准数组">
                    ${STANDARD_ARRAY.map((option) => `
                        <option value="${option}" ${value === option ? "selected" : ""}>${option}</option>`).join("")}
                </select>`;
            return abilityRow(current, item.key, item.label, item.short, control, renderBonusCell(current, item.key));
        }).join("");
    }

    function renderBonusCell(current: WizardState, key: Dnd5eWizardAbility): string {
        const bonus = Number(current.raceBonuses[key]) || 0;
        return `
            <label class="dnd5e-wiz-bonus-input">
                <span>种族 +</span>
                <input type="number" min="-5" max="10" step="1" data-wiz-field="race-bonus" data-wiz-ability="${key}"
                    value="${bonus}" aria-label="${escapeHtml(ABILITIES.find((item) => item.key === key)?.label || key)}种族加值">
            </label>`;
    }

    function renderStepAbilities(current: WizardState): string {
        const remaining = pointBuyRemaining(current.pointBuy);
        const header = current.mode === "pointbuy"
            ? `<div class="dnd5e-wiz-budget ${remaining < 0 ? "is-over" : ""}">
                   剩余点数 <strong>${remaining}</strong> / ${POINT_BUY_BUDGET}
                   ${remaining < 0 ? '<small>已超支，请降低属性</small>' : ""}
               </div>`
            : current.mode === "roll"
                ? `<div class="dnd5e-wiz-budget">
                       <button type="button" class="dnd5e-btn" data-wiz-action="roll">
                           <i class="fa fa-dice-d20" aria-hidden="true"></i> ${current.rolled.length ? "重掷六组" : "掷六组"}
                       </button>
                       <span>成绩池：${current.rolled.map((value, index) => `
                           <span class="dnd5e-wiz-chip ${rollPoolUsed(current, index) ? "is-used" : ""}">${value}</span>`).join("")}</span>
                       ${unassignedCount(current) ? `<small>还有 ${unassignedCount(current)} 项未分配</small>` : ""}
                   </div>`
                : `<div class="dnd5e-wiz-budget">
                       标准数组 15/14/13/12/10/8 已按序填入，改动一项会与占用它的属性互换。
                   </div>`;
        const rows = current.mode === "pointbuy"
            ? renderPointBuyControls(current)
            : current.mode === "roll" ? renderRollControls(current) : renderStandardControls(current);
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-dice-d20" aria-hidden="true"></i> 属性生成</h4>
                ${renderModeTabs(current)}
                ${header}
                <div class="dnd5e-wiz-ability-table">
                    <div class="dnd5e-wiz-ability-head-row">
                        <span>属性</span><span>${current.mode === "pointbuy" ? "购点" : current.mode === "roll" ? "掷骰分配" : "数组分配"}</span>
                        <span>种族加值</span><span>总值 / 调整值</span>
                    </div>
                    ${rows}
                </div>
                <p class="dnd5e-wiz-hint">
                    三套分配各自独立保存，切换模式不会丢已分配的点数。
                    ${current.race ? `种族加值来自「${escapeHtml(current.race.name)}」，可手动微调（半精灵这类自选加值）。` : "未选种族时加值为 0，可在第 1 步先选种族。"}
                </p>
                <div class="dnd5e-wiz-quick">
                    <button type="button" class="dnd5e-wiz-link" data-wiz-action="race-bonus-reset">恢复种族默认加值</button>
                    <button type="button" class="dnd5e-wiz-link" data-wiz-action="pb-reset">购点归零（全 8）</button>
                </div>
            </section>`;
    }

    function renderStepSkills(current: WizardState): string {
        const klass = current.klass;
        const pool = klass?.skillPool || ALL_SKILL_IDS;
        const required = klass?.skillCount ?? 0;
        const picks = current.skillPicks;
        const backgroundSkills = current.background?.skills || [];
        const rows = pool.map((key) => {
            const fromBackground = backgroundSkills.includes(key);
            const picked = picks.includes(key);
            const disabled = fromBackground || (!picked && klass !== null && picks.length >= required);
            return `
                <label class="dnd5e-wiz-skill ${picked || fromBackground ? "is-on" : ""} ${fromBackground ? "is-locked" : ""}">
                    <input type="checkbox" data-wiz-field="skill" data-wiz-skill="${escapeHtml(key)}"
                        ${picked || fromBackground ? "checked" : ""} ${disabled ? "disabled" : ""}>
                    <span>${escapeHtml(skillLabel(key))}</span>
                    <small>${escapeHtml((SKILLS.find((item) => item.key === key)?.ability || "").toUpperCase())}</small>
                    ${fromBackground ? '<em class="dnd5e-wiz-tag">背景</em>' : ""}
                </label>`;
        }).join("");
        const kit = klass ? klass.kit : [];
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-check-square-o" aria-hidden="true"></i> 技能熟练</h4>
                ${klass
                    ? `<p class="dnd5e-wiz-hint">${escapeHtml(klass.name)} 可选 <strong>${required}</strong> 项（已选 ${picks.length}）；背景熟练自动叠加且不可取消。</p>`
                    : `<p class="dnd5e-wiz-hint">还没选职业，先按 SRD 全技能自由勾选；回第 1 步选职业后会收敛到职业技能表。</p>`}
                <div class="dnd5e-wiz-skill-grid">${rows}</div>
            </section>
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-suitcase" aria-hidden="true"></i> 初始装备</h4>
                <p class="dnd5e-wiz-hint">
                    dnd5e-srd 的 equipment 包里没有职业起始包（只有单件物品），因此这里给出按职业整理的**常用建议**，
                    点一下追加一行；每行写「名称 x数量」，重量按内置表补齐，卡面里还能再改。
                </p>
                ${kit.length ? `<div class="dnd5e-wiz-kit">
                    ${kit.map((item) => `<button type="button" class="dnd5e-wiz-chip dnd5e-wiz-chip-btn" data-wiz-action="kit" data-wiz-kit="${escapeHtml(item)}">+ ${escapeHtml(item)}</button>`).join("")}
                </div>` : ""}
                <label class="dnd5e-wiz-field">
                    <span>装备（每行：名称 x数量）</span>
                    <textarea data-wiz-field="equipment" rows="5" placeholder="链甲 x1">${escapeHtml(current.equipmentText)}</textarea>
                </label>
            </section>`;
    }

    function renderPreview(current: WizardState): string {
        const sheet = buildDraftSheet(current);
        const quick = previewOf(sheet);
        const proficiencies = (sheet.proficiencies || {}) as {
            saves?: string[]; skills?: string[]; armor?: string[]; weapons?: string[];
        };
        const slotRows = Object.keys(quick.slots).sort((left, right) => Number(left) - Number(right));
        const equipment = (sheet.equipment || []) as Array<{ name: string; quantity: number }>;
        return `
            <section class="dnd5e-wiz-section">
                <h4><i class="fa fa-address-card" aria-hidden="true"></i> 速览</h4>
                <p class="dnd5e-wiz-identity">
                    <strong>${escapeHtml(current.name.trim() || "（未命名）")}</strong>
                    <span>1 级 ${escapeHtml(current.klass?.name || "未选职业")}</span>
                    <span>${escapeHtml(current.race?.name || "未选种族")}</span>
                    ${current.background ? `<span>${escapeHtml(current.background.name)}</span>` : ""}
                </p>
                <div class="dnd5e-wiz-quad">
                    <div class="dnd5e-wiz-card">
                        <h5>六属性</h5>
                        <ul class="dnd5e-wiz-preview-abilities">
                            ${ABILITIES.map((item) => `
                                <li><span>${escapeHtml(item.label)} <small>${item.short}</small></span>
                                    <strong>${quick.abilities[item.key]}</strong>
                                    <em>${signed(quick.modifiers[item.key])}</em></li>`).join("")}
                        </ul>
                    </div>
                    <div class="dnd5e-wiz-card">
                        <h5>战斗数值</h5>
                        <ul class="dnd5e-wiz-preview-list">
                            <li><span>生命值上限</span><strong>${quick.maxHp}</strong></li>
                            <li><span>护甲等级（10 + 敏捷）</span><strong>${quick.armorClass}</strong></li>
                            <li><span>熟练加值</span><strong>${signed(quick.proficiencyBonus)}</strong></li>
                            <li><span>先攻</span><strong>${signed(quick.initiative)}</strong></li>
                            <li><span>被动感知</span><strong>${quick.passivePerception}</strong></li>
                            <li><span>生命骰</span><strong>${escapeHtml(String(sheet.hitDice || "—"))}</strong></li>
                        </ul>
                        <p class="dnd5e-wiz-hint">HP = 生命骰满值 + 体质调整值；AC 先按 10 + 敏捷调整值（护甲效果在卡面里调）。</p>
                    </div>
                    <div class="dnd5e-wiz-card">
                        <h5>熟练</h5>
                        <ul class="dnd5e-wiz-preview-list">
                            <li><span>豁免</span><strong>${(proficiencies.saves || []).map((key) => escapeHtml(key.toUpperCase())).join(" / ") || "—"}</strong></li>
                            <li><span>技能</span><strong>${(proficiencies.skills || []).map((key) => escapeHtml(skillLabel(key))).join("、") || "—"}</strong></li>
                            <li><span>护甲</span><strong>${(proficiencies.armor || []).map((item) => escapeHtml(item)).join("、") || "—"}</strong></li>
                            <li><span>武器</span><strong>${(proficiencies.weapons || []).map((item) => escapeHtml(item)).join("、") || "—"}</strong></li>
                        </ul>
                    </div>
                    <div class="dnd5e-wiz-card">
                        <h5>施法</h5>
                        ${quick.spellSaveDC === null
                            ? `<p class="dnd5e-wiz-hint">非施法职业：本卡没有法术豁免 DC 与法术攻击。</p>`
                            : `<ul class="dnd5e-wiz-preview-list">
                                   <li><span>法术豁免 DC</span><strong>${quick.spellSaveDC}</strong></li>
                                   <li><span>法术攻击加值</span><strong>${signed(quick.spellAttackBonus ?? 0)}</strong></li>
                                   <li><span>1 级法术位</span><strong>${slotRows.length ? slotRows.map((key) => `${key} 环 ×${quick.slots[key]?.max || 0}`).join("、") : "—"}</strong></li>
                               </ul>
                               ${quick.casterPending ? `<p class="dnd5e-wiz-hint">1 级还没有法术位（2 级起获得），保存后卡面会按职业表补。</p>` : ""}`}
                        <p class="dnd5e-wiz-hint">装备：${equipment.length ? equipment.map((item) => `${escapeHtml(item.name)}×${item.quantity}`).join("、") : "空"}</p>
                    </div>
                </div>
                <p class="dnd5e-wiz-hint">
                    创建后卡面的派生值由 dnd5e 插件 schema 重算，与这里的速览同口径（规则数字的权威实现在
                    <code>dnd5e_plugin/builder.py</code>）。
                </p>
            </section>`;
    }

    function renderStepBody(current: WizardState): string {
        if (current.step === 1) return renderStepConcept(current);
        if (current.step === 2) return renderStepAbilities(current);
        if (current.step === 3) return renderStepSkills(current);
        return renderPreview(current);
    }

    function renderFooter(current: WizardState): string {
        const isFirst = current.step === 1;
        const isLast = current.step === 4;
        return `
            <button type="button" class="btn btn-secondary" data-wiz-action="cancel">取消</button>
            <div class="dnd5e-wiz-footer-right">
                ${options.onAdvanced ? `<button type="button" class="dnd5e-wiz-link" data-wiz-action="advanced">高级：直接填写表单</button>` : ""}
                ${isFirst ? "" : `<button type="button" class="btn btn-outline-secondary" data-wiz-action="prev">上一步</button>`}
                ${isLast
                    ? `<button type="button" class="btn btn-primary" data-wiz-action="create" ${saving ? "disabled" : ""}>
                           ${saving ? "正在创建…" : "创建角色卡"}</button>`
                    : `<button type="button" class="btn btn-primary" data-wiz-action="next">下一步</button>`}
            </div>`;
    }

    function renderBody(): void {
        const current = state;
        if (!current || !modal) return;
        const body = modal.querySelector<HTMLElement>("[data-wiz-body]");
        const footer = modal.querySelector<HTMLElement>("[data-wiz-footer]");
        const steps = modal.querySelector<HTMLElement>("[data-wiz-steps]");
        if (steps) steps.innerHTML = renderStepper(current);
        if (body) body.innerHTML = renderStepBody(current);
        if (footer) footer.innerHTML = renderFooter(current);
        restoreFocus();
    }

    function restoreFocus(): void {
        if (!modal || !focusToken) return;
        const target = modal.querySelector<HTMLElement>(`[data-wiz-token="${focusToken}"]`);
        focusToken = "";
        target?.focus();
    }

    // --- 校验与推进 -------------------------------------------------------------

    function validateStep(current: WizardState, step: Dnd5eWizardStep): string {
        if (step === 1) {
            if (!current.name.trim()) return "请先填写角色名（必填）。";
            if (!current.race) return "请选择种族。";
            if (!current.klass) return "请选择职业。";
        }
        if (step === 2 && unassignedCount(current) > 0) {
            return `掷骰模式还有 ${unassignedCount(current)} 项属性未分配。`;
        }
        return "";
    }

    function gotoStep(step: Dnd5eWizardStep, settings?: { silent?: boolean }): void {
        const current = state;
        if (!current) return;
        if (step > current.step) {
            for (let index = current.step; index < step; index += 1) {
                const message = validateStep(current, index as Dnd5eWizardStep);
                if (message) {
                    if (!settings?.silent) notify(message, "warning");
                    current.step = index as Dnd5eWizardStep;
                    renderBody();
                    return;
                }
            }
        }
        current.step = step;
        renderBody();
        const body = modal?.querySelector<HTMLElement>("[data-wiz-body]");
        body?.scrollTo?.({ top: 0 });
    }

    // --- 保存 -------------------------------------------------------------------

    function nextCardId(): string {
        return `${RULESET_ID}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    }

    async function createCard(): Promise<void> {
        const current = state;
        if (!current || saving) return;
        if (!current.name.trim()) {
            current.step = 1;
            renderBody();
            notify("请先填写角色名（必填）。", "warning");
            return;
        }
        if (!current.klass || !current.race) {
            current.step = 1;
            renderBody();
            notify("请先选择种族与职业。", "warning");
            return;
        }
        saving = true;
        renderBody();
        const payload: Record<string, unknown> = {
            id: nextCardId(),
            name: current.name.trim().slice(0, 40),
            rulesetId: RULESET_ID,
            playerId: String((global as unknown as { currentUser?: { id?: string | number } }).currentUser?.id ?? ""),
            sheet: buildDraftSheet(current),
        };
        try {
            const response = await TrpgApi.put<ApiResponse<Record<string, unknown>>>(
                `/api/characters/${encodeURIComponent(String(payload.id))}`,
                payload
            );
            if (!response?.success) {
                notify(response?.message || response?.error || "创建角色卡失败", "error");
                return;
            }
            const saved = (response.data || payload) as Record<string, unknown>;
            saving = false;
            close();
            notify("角色卡已创建", "success");
            options.onCreated?.(saved);
        } catch (error) {
            notify(`创建角色卡失败：${String((error as Error)?.message || error)}`, "error");
        } finally {
            saving = false;
            if (modal?.isConnected) renderBody();
        }
    }

    // --- 交互 -------------------------------------------------------------------

    function applyRace(raceId: string): void {
        const current = state;
        if (!current) return;
        const race = SRD_RACES.find((item) => item.id === raceId) || null;
        current.race = race;
        current.raceBonuses = race ? { ...race.bonuses } : {};
        current.deepResults = [];
        current.deepMessage = "";
    }

    function applyBackground(backgroundId: string): void {
        const current = state;
        if (!current) return;
        const background = SRD_BACKGROUNDS.find((item) => item.id === backgroundId) || null;
        current.background = background;
        current.deepResults = [];
        current.deepMessage = "";
        // 背景熟练自动生效：之前手动勾的重叠项交给 proficientSkills 去重。
        if (background) {
            current.skillPicks = current.skillPicks.filter((key) => !background.skills.includes(key));
        }
    }

    function toggleSkill(key: string, on: boolean): void {
        const current = state;
        if (!current) return;
        const background = current.background?.skills || [];
        if (background.includes(key)) return;
        const required = current.klass?.skillCount ?? 0;
        if (on) {
            if (!current.skillPicks.includes(key)) {
                if (required && current.skillPicks.length >= required) {
                    notify(`「${current.klass?.name}」只能选 ${required} 项技能熟练，先取消一项。`, "warning");
                    renderBody();
                    return;
                }
                current.skillPicks.push(key);
            }
        } else {
            current.skillPicks = current.skillPicks.filter((item) => item !== key);
        }
        renderBody();
    }

    function swapStandard(key: Dnd5eWizardAbility, value: number): void {
        const current = state;
        if (!current) return;
        const previous = current.standard[key];
        const holder = ABILITY_KEYS.find((item) => item !== key && current.standard[item] === value);
        current.standard[key] = value;
        // 与占用该值的属性互换，保证六值始终唯一且全部用上。
        if (holder) current.standard[holder] = previous;
        renderBody();
    }

    function assignRoll(key: Dnd5eWizardAbility, index: number | null): void {
        const current = state;
        if (!current) return;
        if (index !== null) {
            ABILITY_KEYS.forEach((other) => {
                if (other !== key && current.rollAssign[other] === index) current.rollAssign[other] = null;
            });
        }
        current.rollAssign[key] = index;
        renderBody();
    }

    function setRaceBonus(key: Dnd5eWizardAbility, value: string): void {
        const current = state;
        if (!current) return;
        const amount = Number(value);
        if (!Number.isFinite(amount) || amount === 0) delete current.raceBonuses[key];
        else current.raceBonuses[key] = Math.max(-5, Math.min(10, Math.round(amount)));
        // 只刷新总值/调整值，避免打断正在输入的 number 框。
        syncAbilityTotals();
    }

    function syncAbilityTotals(): void {
        const current = state;
        if (!current || !modal) return;
        ABILITIES.forEach((item) => {
            const base = baseScores(current)[item.key];
            const total = totalScores(current)[item.key];
            const bonus = Number(current.raceBonuses[item.key]) || 0;
            const row = modal?.querySelector<HTMLElement>(`[data-wiz-ability="${item.key}"]`);
            if (!row) return;
            const missing = current.mode === "roll" && base === 0;
            const totalNode = row.querySelector<HTMLElement>(".dnd5e-wiz-total");
            const modNode = row.querySelector<HTMLElement>(".dnd5e-wiz-mod");
            const noteNode = row.querySelector<HTMLElement>(".dnd5e-wiz-ability-total small");
            if (totalNode) totalNode.textContent = missing ? "—" : String(total);
            if (modNode) modNode.textContent = missing ? "—" : signed(abilityModifier(total));
            if (noteNode) {
                noteNode.textContent = missing ? "未分配" : `基础 ${base}${bonus ? ` + 种族 ${bonus}` : ""} = ${total}`;
            }
        });
    }

    function appendKit(item: string): void {
        const current = state;
        if (!current) return;
        const lines = current.equipmentText.split(/\n/).map((line) => line.trim()).filter(Boolean);
        const name = item.replace(/（.*?）/g, "").trim();
        const existing = lines.findIndex((line) => line.replace(/\s*[x×]\s*\d+$/i, "").trim() === name);
        if (existing >= 0) {
            const line = lines[existing] ?? "";
            const quantity = Number(line.match(/[x×]\s*(\d+)$/i)?.[1] || 1) + 1;
            lines[existing] = `${name} x${quantity}`;
        } else {
            lines.push(`${name} x1`);
        }
        current.equipmentText = lines.join("\n");
        renderBody();
    }

    async function confirmDiscard(): Promise<boolean> {
        const dialog = global.TrpgDialog?.confirm;
        if (!dialog) return global.confirm("放弃这次建卡？已填写的内容不会保存。");
        return dialog({
            title: "放弃建卡",
            message: "放弃这次建卡？已填写的内容不会保存。",
            confirmText: "放弃",
            cancelText: "继续填写",
            danger: true,
        });
    }

    function close(): void {
        state = null;
        saving = false;
        instance?.hide();
    }

    async function requestClose(): Promise<void> {
        if (!state) return;
        const touched = Boolean(state.name.trim() || state.klass || state.race);
        if (touched && !(await confirmDiscard())) return;
        close();
    }

    function onClick(event: Event): void {
        const target = event.target as HTMLElement | null;
        const button = target?.closest<HTMLElement>("[data-wiz-action]");
        const current = state;
        if (!current) return;
        if (!button) return;
        const action = button.dataset.wizAction || "";
        switch (action) {
            case "next":
                gotoStep((current.step + 1) as Dnd5eWizardStep);
                break;
            case "prev":
                gotoStep((current.step - 1) as Dnd5eWizardStep);
                break;
            case "cancel":
                void requestClose();
                break;
            case "advanced": {
                const advanced = options.onAdvanced;
                close();
                advanced?.();
                break;
            }
            case "create":
                void createCard();
                break;
            case "mode":
                current.mode = (button.dataset.wizMode as Dnd5eWizardMode) || "pointbuy";
                if (current.mode === "roll" && !current.rolled.length) current.rolled = rollPool();
                renderBody();
                break;
            case "pb-inc":
            case "pb-dec": {
                const key = button.dataset.wizAbility as Dnd5eWizardAbility | undefined;
                if (!key || !ABILITY_KEYS.includes(key)) return;
                const step = action === "pb-inc" ? 1 : -1;
                const next = clampPointBuy(current.pointBuy[key] + step);
                const candidate: Record<Dnd5eWizardAbility, number> = { ...current.pointBuy };
                candidate[key] = next;
                if (step > 0 && pointBuySpent(candidate) > POINT_BUY_BUDGET) {
                    notify("购点余额不足，先降低别的属性。", "warning");
                    return;
                }
                current.pointBuy = candidate;
                focusToken = "";
                renderBody();
                modal?.querySelector<HTMLElement>(`[data-wiz-action="${action}"][data-wiz-ability="${key}"]`)?.focus();
                break;
            }
            case "pb-reset":
                current.pointBuy = emptyAbilities();
                renderBody();
                break;
            case "roll":
                current.rolled = rollPool();
                current.rollAssign = emptyRollAssign();
                renderBody();
                break;
            case "race-bonus-reset":
                current.raceBonuses = current.race ? { ...current.race.bonuses } : {};
                renderBody();
                break;
            case "class":
                current.klass = SRD_CLASSES.find((item) => item.id === button.dataset.wizClass) || null;
                current.skillPicks = [];
                renderBody();
                break;
            case "kit":
                appendKit(button.dataset.wizKit || "");
                break;
            case "clear-race":
                current.race = null;
                current.raceBonuses = {};
                renderBody();
                break;
            case "clear-background":
                current.background = null;
                renderBody();
                break;
            case "deep-open": {
                const category = (button.dataset.wizCategory as "race" | "background") || "race";
                current.deepCategory = category;
                current.deepResults = [];
                current.deepMessage = "";
                renderBody();
                modal?.querySelector<HTMLInputElement>('[data-wiz-field="deepQuery"]')?.focus();
                break;
            }
            case "deep-search":
                void runDeepSearch(current.deepQuery);
                break;
            case "deep-pick": {
                const uid = button.dataset.wizUid || "";
                const item = current.deepResults.find((row) => row.uid === uid);
                if (!item) return;
                if (item.category === "background") void pickDeepBackground(item);
                else void pickDeepRace(item);
                break;
            }
            default:
                break;
        }
    }

    function onChange(event: Event): void {
        const target = event.target as HTMLInputElement | HTMLSelectElement | null;
        const current = state;
        if (!target || !current) return;
        const field = target.dataset.wizField || "";
        switch (field) {
            case "race":
                applyRace((target as HTMLSelectElement).value);
                renderBody();
                break;
            case "background":
                applyBackground((target as HTMLSelectElement).value);
                renderBody();
                break;
            case "skill":
                toggleSkill(target.dataset.wizSkill || "", (target as HTMLInputElement).checked);
                break;
            case "roll-assign": {
                const key = target.dataset.wizAbility as Dnd5eWizardAbility | undefined;
                if (!key) return;
                const value = (target as HTMLSelectElement).value;
                assignRoll(key, value === "" ? null : Number(value));
                break;
            }
            case "standard-assign": {
                const key = target.dataset.wizAbility as Dnd5eWizardAbility | undefined;
                if (!key) return;
                swapStandard(key, Number((target as HTMLSelectElement).value));
                break;
            }
            default:
                break;
        }
    }

    function onInput(event: Event): void {
        const target = event.target as HTMLInputElement | HTMLTextAreaElement | null;
        const current = state;
        if (!target || !current) return;
        const field = target.dataset.wizField || "";
        if (field === "name") {
            current.name = target.value;
            return;
        }
        if (field === "equipment") {
            current.equipmentText = target.value;
            return;
        }
        if (field === "race-bonus") {
            const key = target.dataset.wizAbility as Dnd5eWizardAbility | undefined;
            if (key) setRaceBonus(key, target.value);
            return;
        }
        if (field === "deepQuery") {
            current.deepQuery = target.value;
            if (deepTimer !== null) global.clearTimeout(deepTimer);
            deepTimer = global.setTimeout(() => {
                deepTimer = null;
                void runDeepSearch(current.deepQuery);
            }, 320);
        }
    }

    function onKeydown(event: KeyboardEvent): void {
        if (!state) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            void requestClose();
            return;
        }
        if (event.key !== "Enter") return;
        const target = event.target as HTMLElement | null;
        const tag = (target?.tagName || "").toLowerCase();
        // 文本域/检索框里 Enter 是换行或触发检索，不当「下一步」。
        if (tag === "textarea") return;
        if (target?.getAttribute("data-wiz-field") === "deepQuery") {
            event.preventDefault();
            void runDeepSearch(state.deepQuery);
            return;
        }
        if (tag === "button" || tag === "a") return;
        event.preventDefault();
        if (state.step < 4) gotoStep((state.step + 1) as Dnd5eWizardStep);
        else void createCard();
    }

    function ensureModal(): { element: HTMLElement; instance: BootstrapModalInstance } | null {
        if (modal && instance) return { element: modal, instance };
        if (typeof bootstrap === "undefined") return null;
        const element = document.createElement("div");
        element.id = "dnd5eWizardModal";
        element.className = "modal fade dnd5e-wiz-modal";
        element.tabIndex = -1;
        element.innerHTML = `
            <div class="modal-dialog modal-lg modal-dialog-scrollable modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">创建 D&D 5e 角色卡</h5>
                        <button type="button" class="btn-close" data-wiz-action="cancel" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div data-wiz-steps></div>
                        <div data-wiz-body></div>
                    </div>
                    <div class="modal-footer" data-wiz-footer></div>
                </div>
            </div>`;
        element.addEventListener("click", onClick);
        element.addEventListener("change", onChange);
        element.addEventListener("input", onInput);
        element.addEventListener("keydown", onKeydown);
        element.addEventListener("hidden.bs.modal", () => {
            // 关闭后清空体内容，避免下次打开闪一帧旧内容。
            const body = element.querySelector<HTMLElement>("[data-wiz-body]");
            if (body) body.innerHTML = "";
        });
        document.body.appendChild(element);
        modal = element;
        instance = new bootstrap.Modal(element);
        return { element, instance };
    }

    function open(openOptions: Dnd5eWizardOptions = {}): void {
        const built = ensureModal();
        if (!built) {
            notify("建卡向导窗口不可用（bootstrap 未加载）", "error");
            return;
        }
        options = openOptions;
        state = freshState();
        saving = false;
        focusToken = "";
        renderBody();
        built.instance.show();
        void probeDeepDb().then(() => {
            if (!state) return;
            renderBody();
        });
        // 焦点落在角色名输入框：键盘用户打开即可直接打字。
        global.setTimeout(() => {
            modal?.querySelector<HTMLInputElement>('[data-wiz-field="name"]')?.focus();
        }, 180);
    }

    global.TrpgDnd5eCharacterWizard = {
        open,
        close,
        isOpen: () => Boolean(state),
    };
})(typeof window !== "undefined" ? window : globalThis as Window & typeof globalThis);
