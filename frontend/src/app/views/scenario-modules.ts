// 剧本模块编辑器：模块数据模型与归一化、编辑器渲染、字段采集与模块级小工具。

function normalizeScenarioModules(scenario: Scenario): ScenarioModule[] {
    if (Array.isArray(scenario.modules) && scenario.modules.length > 0) {
        const typeCounts = new Map<ScenarioModuleType, number>();
        return scenario.modules.map((module, index) => {
            const typeIndex = (typeCounts.get(module.module_type) || 0) + 1;
            typeCounts.set(module.module_type, typeIndex);
            return ensureModuleDefaults(module, index + 1, typeIndex);
        });
    }
    return [];
}

function createModule(type: ScenarioModuleType, index: number, existing?: ScenarioModule[], patch: Partial<ScenarioModule> = {}): ScenarioModule {
    const module: ScenarioModule = {
        id: createModuleId(type, index),
        module_type: type,
        title: patch.title || defaultModuleTitle(type, index),
        summary: patch.summary || "",
        content: patch.content || "",
        notes: patch.notes || "",
        visibility: patch.visibility || moduleDefaultVisibility(type),
        send_to_ai: patch.send_to_ai !== undefined ? patch.send_to_ai : type !== "custom",
        code: patch.code || "",
        scene_id: patch.scene_id,
        ending_id: patch.ending_id,
        open_ending: patch.open_ending,
        inputs: patch.inputs || [],
        timeline_entries: patch.timeline_entries || [],
        attributes: patch.attributes || {},
        battle: patch.battle || {},
        skills: patch.skills || [],
        weapons: patch.weapons || [],
        triggers: patch.triggers || [],
    };

    if (type === "scene") {
        module.scene_id = patch.scene_id || index;
    } else if (type === "ending") {
        module.ending_id = patch.ending_id || index;
    } else if (type === "monster" || type === "npc") {
        module.code = patch.code || generateModuleCode(type, existing || []);
        module.attributes = normalizeAttributeMap(patch.attributes);
        module.battle = normalizeBattleMap(patch.battle);
        module.weapons = normalizeWeaponRows(patch.weapons);
        module.skills = normalizeSkillRows(patch.skills);
    } else if (type === "custom") {
        module.inputs = normalizeInputRows(patch.inputs);
    } else if (type === "timeline") {
        module.timeline_entries = normalizeTimelineEntries(patch.timeline_entries);
    }

    return ensureModuleDefaults(module, index);
}

function ensureModuleDefaults(module: ScenarioModule, index: number, titleIndex = index): ScenarioModule {
    const base = cloneModule(module, index, titleIndex);
    base.summary = base.summary || base.content || "";
    base.visibility = base.visibility || moduleDefaultVisibility(base.module_type);
    base.send_to_ai = base.send_to_ai !== false;

    if (base.module_type === "scene") {
        base.scene_id = Number.isFinite(Number(base.scene_id)) && Number(base.scene_id) > 0 ? Number(base.scene_id) : index;
        base.triggers = normalizeTriggers(base.triggers);
    } else if (base.module_type === "ending") {
        base.ending_id = Number.isFinite(Number(base.ending_id)) && Number(base.ending_id) > 0 ? Number(base.ending_id) : index;
    } else if (base.module_type === "monster" || base.module_type === "npc") {
        base.code = base.code || generateModuleCode(base.module_type, [base]);
        // 兼容旧数据把技能/武器嵌在 battle 里的写法，打开即保存时不能丢数值。
        const nestedBattle = (base.battle || {}) as unknown as Record<string, unknown>;
        if (!(base.skills || []).some((skill) => skill.name) && Array.isArray(nestedBattle.skills)) {
            base.skills = nestedBattle.skills as ScenarioModuleSkillRow[];
        }
        if (!(base.weapons || []).some((weapon) => weapon.name) && Array.isArray(nestedBattle.weapons)) {
            base.weapons = nestedBattle.weapons as ScenarioModuleWeaponRow[];
        }
        base.attributes = normalizeAttributeMap(base.attributes);
        base.battle = normalizeBattleMap(base.battle);
        base.weapons = normalizeWeaponRows(base.weapons);
        base.skills = normalizeSkillRows(base.skills);
    } else if (base.module_type === "timeline") {
        base.timeline_entries = normalizeTimelineEntries(base.timeline_entries);
    } else if (base.module_type === "custom") {
        base.inputs = normalizeInputRows(base.inputs);
    }

    return base;
}

function cloneModule(module: ScenarioModule, index: number, titleIndex = index): ScenarioModule {
    return {
        id: module.id || createModuleId(module.module_type, index),
        module_type: module.module_type,
        title: normalizeModuleTitle(module.module_type, module.title || "", titleIndex),
        summary: module.summary || module.content || "",
        content: module.content || "",
        notes: module.notes || "",
        visibility: module.visibility || moduleDefaultVisibility(module.module_type),
        send_to_ai: module.send_to_ai !== false,
        code: module.code || "",
        scene_id: module.scene_id,
        ending_id: module.ending_id,
        open_ending: module.open_ending,
        inputs: module.inputs || [],
        timeline_entries: module.timeline_entries || [],
        attributes: module.attributes || {},
        battle: module.battle || {},
        skills: module.skills || [],
        weapons: module.weapons || [],
        triggers: module.triggers || [],
    };
}

function defaultModuleTitle(type: ScenarioModuleType, index: number): string {
    return `${moduleTypeLabel(type)}${Math.max(1, index)}`;
}

function normalizeModuleTitle(type: ScenarioModuleType, title: string, index: number): string {
    const normalized = title.trim();
    const typeLabel = moduleTypeLabel(type);
    return !normalized
        || /^\u6a21\u5757\s*\d*$/.test(normalized)
        || normalized === typeLabel
        || new RegExp(`^${escapeRegExp(typeLabel)}\\s*\\d+$`).test(normalized)
        ? defaultModuleTitle(type, index)
        : normalized;
}

function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function moduleTypeLabel(type: ScenarioModuleType): string {
    switch (type) {
        case "background":
            return scenarioT("scenario.module.type.background", "\u80cc\u666f");
        case "public_info":
            return scenarioT("scenario.module.type.public_info", "\u516c\u5f00\u4fe1\u606f");
        case "preparation":
            return scenarioT("scenario.module.type.preparation", "\u6e38\u620f\u51c6\u5907");
        case "timeline":
            return scenarioT("scenario.module.type.timeline", "\u65f6\u95f4\u7ebf");
        case "scene":
            return scenarioT("scenario.module.type.scene", "\u573a\u666f");
        case "ending":
            return scenarioT("scenario.module.type.ending", "\u7ed3\u5c40");
        case "monster":
            return scenarioT("scenario.module.type.monster", "\u602a\u7269\u4fe1\u606f");
        case "npc":
            return scenarioT("scenario.module.type.npc", "NPC\u4fe1\u606f");
        case "custom":
            return scenarioT("scenario.module.type.custom", "\u81ea\u5b9a\u4e49\u6a21\u5757");
        default:
            return scenarioT("scenario.module.group", "\u6a21\u5757");
    }
}

function moduleDescription(type: ScenarioModuleType): string {
    switch (type) {
        case "background":
            return scenarioT("scenario.module.description.background", "\u4ec5\u5b88\u79d8\u4eba\u53ef\u89c1\u7684\u80cc\u666f\u4fe1\u606f\uff0c\u542f\u52a8\u65f6\u53d1\u9001\u7ed9 AI\u3002");
        case "public_info":
            return scenarioT("scenario.module.description.public_info", "\u53ef\u76f4\u63a5\u5c55\u793a\u7ed9\u73a9\u5bb6\u7684\u516c\u5f00\u4fe1\u606f\u3002");
        case "preparation":
            return scenarioT("scenario.module.description.preparation", "\u5b88\u79d8\u4eba\u7684\u5f00\u56e2\u51c6\u5907\u8bb0\u5f55\u3002");
        case "timeline":
            return scenarioT("scenario.module.description.timeline", "\u4e8b\u4ef6\u65f6\u95f4\u7ebf\u4e0e\u63a8\u8fdb\u63d0\u793a\u3002");
        case "scene":
            return scenarioT("scenario.module.description.scene", "\u573a\u666f\u6b63\u6587\u3001\u6458\u8981\u3001\u89e6\u53d1\u5668\u4e0e\u5907\u6ce8\u3002");
        case "ending":
            return scenarioT("scenario.module.description.ending", "\u5267\u672c\u7ed3\u5c40\u6a21\u5757\u3002");
        case "monster":
            return scenarioT("scenario.module.description.monster", "\u602a\u7269\u5361\u7247\u5f0f\u6a21\u5757\u3002");
        case "npc":
            return scenarioT("scenario.module.description.npc", "NPC \u5361\u7247\u5f0f\u6a21\u5757\u3002");
        case "custom":
            return scenarioT("scenario.module.description.custom", "\u53ef\u81ea\u5b9a\u4e49\u5b57\u6bb5\u5e76\u51b3\u5b9a\u662f\u5426\u53d1\u7ed9 AI \u7684\u6a21\u5757\u3002");
        default:
            return "";
    }
}

function moduleDefaultVisibility(type: ScenarioModuleType): "public" | "kp" {
    return type === "background" ? "kp" : "public";
}

function createModuleId(type: ScenarioModuleType, index: number): string {
    return `module-${type}-${Date.now()}-${index}-${Math.random().toString(36).slice(2, 8)}`;
}

function generateModuleCode(type: "monster" | "npc", existing: ScenarioModule[] | string[] = []): string {
    const prefix = type === "monster" ? "M" : "N";
    const existingCodes = new Set(
        existing.map((item) => typeof item === "string" ? item : item.code || "").filter(Boolean),
    );
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    for (let attempt = 0; attempt < 200; attempt += 1) {
        let suffix = "";
        for (let i = 0; i < 6; i += 1) suffix += alphabet[Math.floor(Math.random() * alphabet.length)];
        const code = `${prefix}-${suffix}`;
        if (!existingCodes.has(code)) return code;
    }
    return `${prefix}-${Date.now().toString(36).slice(-6).padStart(6, "0")}`;
}

function normalizeCardValue(value: unknown): string {
    if (value === null || value === undefined) return "N/A";
    const text = String(value).trim();
    return text || "N/A";
}

function readCardField(source: Record<string, string> | undefined, key: string): unknown {
    if (!source) return undefined;
    const record = source as unknown as Record<string, unknown>;
    for (const candidate of new Set([key, key.toUpperCase(), key.toLowerCase()])) {
        const value = record[candidate];
        if (value !== undefined && value !== null && value !== "") return value;
    }
    return undefined;
}

function normalizeAttributeMap(attributes?: Record<string, string>): Record<string, string> {
    const keys = ["STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC"];
    return keys.reduce((acc, key) => {
        acc[key] = normalizeCardValue(readCardField(attributes, key));
        return acc;
    }, {} as Record<string, string>);
}

function normalizeBattleMap(battle?: Record<string, string>): Record<string, string> {
    const keys = ["hp", "max_hp", "dex", "str", "siz", "con", "db", "build", "mov", "armor"];
    return keys.reduce((acc, key) => {
        acc[key] = normalizeCardValue(readCardField(battle, key));
        return acc;
    }, {} as Record<string, string>);
}

function normalizeSkillRows(skills?: ScenarioModuleSkillRow[]): ScenarioModuleSkillRow[] {
    if (!Array.isArray(skills) || skills.length === 0) return [{ name: "", base: "" }];
    return skills.map((skill) => {
        const source = skill as unknown as Record<string, unknown>;
        const raw = skill.base ?? source.value;
        return {
            name: String(skill.name || "").trim(),
            base: raw === null || raw === undefined || raw === "N/A" ? "" : String(raw).trim(),
        };
    });
}

function normalizeWeaponRows(weapons?: ScenarioModuleWeaponRow[]): ScenarioModuleWeaponRow[] {
    if (!Array.isArray(weapons) || weapons.length === 0) return [blankWeaponRow()];
    return weapons.map((weapon) => ({
        name: weapon.name || "",
        skill: weapon.skill || "",
        damage: weapon.damage || "",
        range: weapon.range || "",
        attacks: weapon.attacks || "",
        ammo: weapon.ammo || "",
        malfunction: weapon.malfunction || "",
        note: weapon.note || "",
    }));
}

function normalizeInputRows(inputs?: ScenarioModuleInputItem[]): ScenarioModuleInputItem[] {
    if (!Array.isArray(inputs) || inputs.length === 0) {
        return [{ id: `input-${Date.now()}`, label: "", value: "", send_to_ai: false }];
    }
    return inputs.map((input) => ({
        id: input.id || `input-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        label: input.label || "",
        value: input.value || "",
        send_to_ai: Boolean(input.send_to_ai),
    }));
}

function normalizeTimelineEntries(entries?: ScenarioModuleTimelineEntry[]): ScenarioModuleTimelineEntry[] {
    if (!Array.isArray(entries) || entries.length === 0) {
        return [{ id: `timeline-${Date.now()}`, time_point: "", event: "" }];
    }
    return entries.map((entry, index) => ({
        id: entry.id || `timeline-${Date.now()}-${index + 1}`,
        time_point: entry.time_point || "",
        event: entry.event || "",
    }));
}

function normalizeTriggers(triggers?: ScenarioTrigger[]): ScenarioTrigger[] {
    if (!Array.isArray(triggers)) return [];
    return triggers.filter((trigger) => Boolean(trigger.keyword)).map((trigger, index) => ({
        id: Number.isFinite(Number(trigger.id)) && Number(trigger.id) > 0 ? Number(trigger.id) : index + 1,
        display_name: trigger.display_name || (trigger as ScenarioTrigger & { name?: string }).name || "",
        keyword: trigger.keyword || "",
        condition: trigger.condition || (trigger as ScenarioTrigger & { requirement?: string }).requirement || "",
        content_mode: trigger.content_mode || "text",
        content: trigger.content || "",
        asset_name: trigger.asset_name || "",
        asset_mime: trigger.asset_mime || "",
        asset_size: trigger.asset_size,
        asset_path: trigger.asset_path,
        asset_url: trigger.asset_url,
        asset_data_url: trigger.asset_data_url,
    }));
}

function blankWeaponRow(): ScenarioModuleWeaponRow {
    return {
        name: "",
        skill: "",
        damage: "",
        range: "",
        attacks: "",
        ammo: "",
        malfunction: "",
        note: "",
    };
}

function renderModuleList(container: HTMLElement, modules: ScenarioModule[]): void {
    const typeCounts = new Map<ScenarioModuleType, number>();
    container.innerHTML = modules.map((module, index) => {
        const typeIndex = (typeCounts.get(module.module_type) || 0) + 1;
        typeCounts.set(module.module_type, typeIndex);
        return renderModuleEditor(module, index + 1, typeIndex);
    }).join("");
    container.querySelectorAll<HTMLElement>(".scenario-module-item").forEach((card) => refreshModuleCard(card));
}

function renderModuleEditor(module: ScenarioModule, index: number, typeIndex: number): string {
    const typeOptions = ([
        "background",
        "public_info",
        "preparation",
        "timeline",
        "scene",
        "ending",
        "monster",
        "npc",
        "custom",
    ] as ScenarioModuleType[]).map((type) => `<option value="${type}" ${module.module_type === type ? "selected" : ""}>${moduleTypeLabel(type)}</option>`).join("");
    const defaultTitle = defaultModuleTitle(module.module_type, typeIndex);
    const titleValue = module.title || defaultTitle;

    return `
        <section class="scenario-module-item" data-module-item data-module-id="${scenarioEscapeHtml(module.id)}" data-module-default-title="${scenarioEscapeHtml(defaultTitle)}">
                <div class="scenario-module-header">
                    <div class="scenario-module-headline">
                <div class="scenario-module-index"><strong data-module-number>${scenarioEscapeHtml(defaultModuleTitle(module.module_type, typeIndex))}</strong></div>
                        <p class="scenario-module-description" data-module-description>${moduleDescription(module.module_type)}</p>
                    </div>
                    <div class="scenario-module-header-actions">
                        <div class="scenario-module-order-actions">
                            <button type="button" class="scenario-module-icon-button" data-move-module="up" data-i18n-title="scenario.module.move_up" aria-label="上移">
                                <i class="fa fa-arrow-up" aria-hidden="true"></i>
                            </button>
                            <button type="button" class="scenario-module-icon-button" data-move-module="down" data-i18n-title="scenario.module.move_down" aria-label="下移">
                            <i class="fa fa-arrow-down" aria-hidden="true"></i>
                        </button>
                    </div>
                    <button type="button" class="btn btn-sm btn-danger" data-remove-module>${scenarioT("scenario.module.remove", "删除")}</button>
                </div>
            </div>
            <div class="scenario-module-grid">
                <label class="scenario-module-field">
                    <span>类型</span>
                    <select class="form-select" data-module-type>${typeOptions}</select>
                </label>
                <label class="scenario-module-field">
                    <span>${scenarioT("scenario.module.title", "标题")}</span>
                    <input class="form-control" data-module-title value="${scenarioEscapeHtml(titleValue)}">
                </label>
                <label class="scenario-module-field">
                    <div class="scenario-module-field-head">
                        <span>${scenarioT("scenario.module.summary", "摘要")}</span>
                        <button type="button" class="btn btn-sm btn-outline-primary scenario-module-summary-button" data-generate-module-summary data-i18n-title="scenario.module.generate_summary" aria-label="自动摘要">
                            <i class="fa fa-magic" aria-hidden="true"></i>
                            <span>${scenarioT("scenario.module.generate_summary", "自动摘要")}</span>
                        </button>
                    </div>
                    <input class="form-control" data-module-summary value="${scenarioEscapeHtml(module.summary)}">
                </label>
                <label class="scenario-module-field scenario-module-toggle">
                    <span>${scenarioT("scenario.module.send_to_ai", "初始化时发送给 AI")}</span>
                    <input type="checkbox" data-module-send-to-ai ${module.send_to_ai !== false ? "checked" : ""}>
                </label>
            </div>
            <div class="scenario-module-content-panel" data-module-text-panel>
                <label class="scenario-module-field">
                    <span>${scenarioT("scenario.module.notes", "备注")}</span>
                    <textarea class="form-control" rows="2" data-module-notes>${scenarioEscapeHtml(module.notes || "")}</textarea>
                </label>
                <label class="scenario-module-field">
                    <span>${scenarioT("scenario.module.content", "内容")}</span>
                    <textarea class="form-control" rows="4" data-module-content placeholder="${scenarioT("scenario.module.content.placeholder", "模块内容")}">${scenarioEscapeHtml(module.content || "")}</textarea>
                </label>
            </div>
            <div class="scenario-module-extra scenario-module-scene-panel" data-module-scene-panel>
                <div class="scenario-module-panel-title">
                    <strong>${scenarioT("scenario.module.scene.title", "场景")}</strong>
                    <button type="button" class="btn btn-sm btn-secondary" data-add-trigger>${scenarioT("scenario.module.scene.add_trigger", "添加触发器")}</button>
                </div>
                <div class="scenario-module-triggers" data-trigger-list>
                    ${renderTriggerList(module.triggers || [])}
                </div>
            </div>
            <div class="scenario-module-extra scenario-module-timeline-panel" data-module-timeline-panel>
                <div class="scenario-module-panel-title">
                    <strong>${scenarioT("scenario.module.timeline.title", "时间线")}</strong>
                    <button type="button" class="btn btn-sm btn-secondary" data-add-timeline-entry>${scenarioT("scenario.module.timeline.add_entry", "添加时间点")}</button>
                </div>
                <div class="scenario-module-timeline-grid" data-timeline-entry-list>
                    ${renderTimelineEntries(module.timeline_entries || [])}
                </div>
            </div>
            <div class="scenario-module-extra scenario-module-entity-panel" data-module-entity-panel>
                <div class="scenario-module-entity-top">
                    <label class="scenario-module-field">
                        <span>${module.module_type === "monster" ? scenarioT("scenario.module.entity.monster_name", "怪物名称") : scenarioT("scenario.module.entity.npc_name", "NPC 名称")}</span>
                        <input class="form-control" data-entity-name value="${scenarioEscapeHtml(module.title)}">
                    </label>
                    <label class="scenario-module-field">
                        <span>${module.module_type === "monster" ? scenarioT("scenario.module.entity.monster_code", "怪物编号") : scenarioT("scenario.module.entity.npc_code", "NPC 编号")}</span>
                        <input class="form-control" data-entity-code placeholder="${module.module_type === "monster" ? "M-xxxxxx" : "N-xxxxxx"}" value="${scenarioEscapeHtml(module.code || "")}">
                    </label>
                </div>
                <div class="scenario-module-field">
                    <span>${module.module_type === "monster" ? scenarioT("scenario.module.entity.monster_summary", "怪物简介") : scenarioT("scenario.module.entity.npc_summary", "NPC 简介")}</span>
                    <textarea class="form-control" rows="4" data-entity-summary placeholder="${scenarioT("scenario.module.content.placeholder", "面向 AI 的内容摘要")}">${scenarioEscapeHtml(module.content || "")}</textarea>
                </div>
                <div class="scenario-module-section-title">${scenarioT("scenario.module.attributes", "属性")}</div>
                <div class="scenario-attribute-grid scenario-module-attribute-grid">
                    ${renderAttributeInputs(module.attributes || {})}
                </div>
                <div class="scenario-module-section-title">${scenarioT("scenario.module.combat", "战斗")}</div>
                <div class="scenario-module-battle-grid">
                    ${renderBattleInputs(module.battle || {})}
                </div>
                <div class="scenario-module-section-title">${scenarioT("scenario.module.skills", "技能")}</div>
                <div class="scenario-module-list" data-skill-list>${renderSkillRows(module.skills || [])}</div>
                <button type="button" class="btn btn-sm btn-secondary mt-2" data-add-skill>${scenarioT("scenario.module.add_skill", "添加技能")}</button>
                <div class="scenario-module-section-title">${scenarioT("scenario.module.weapons", "武器")}</div>
                <div class="scenario-module-list" data-weapon-list>${renderWeaponRows(module.weapons || [])}</div>
                <button type="button" class="btn btn-sm btn-secondary mt-2" data-add-weapon>${scenarioT("scenario.module.add_weapon", "添加武器")}</button>
            </div>
            <div class="scenario-module-extra scenario-module-custom-panel" data-module-custom-panel>
                <div class="scenario-module-panel-title">
                    <strong>${scenarioT("scenario.module.inputs", "输入项")}</strong>
                    <button type="button" class="btn btn-sm btn-secondary" data-add-custom-input>${scenarioT("scenario.module.add_input", "添加输入项")}</button>
                </div>
                <div class="scenario-module-custom-inputs" data-custom-input-list>
                    ${renderCustomInputRows(module.inputs || [])}
                </div>
            </div>
        </section>
    `;
}

function renderEntityPreview(module: ScenarioModule): string {
    const attributes = module.attributes || {};
    const battle = module.battle || {};
    const attrLine = ["STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC"]
        .map((key) => {
            const value = attributes[key];
            return value && value !== "N/A" ? `${key} ${scenarioEscapeHtml(value)}` : "";
        })
        .filter(Boolean)
        .join(" / ");
    const battleLine = [["hp", "HP"], ["max_hp", "Max HP"], ["armor", scenarioT("scenario.module.battle.armor", "护甲")]]
        .map(([key, label]) => {
            const value = battle[key || ""];
            return value && value !== "N/A" ? `${label} ${scenarioEscapeHtml(value)}` : "";
        })
        .filter(Boolean)
        .join(" / ");
    const skills = (module.skills || []).filter((skill) => skill.name);
    return [
        attrLine ? `<p data-user-content="true"><strong>${scenarioT("scenario.module.attributes", "属性")}：</strong>${attrLine}</p>` : "",
        battleLine ? `<p data-user-content="true"><strong>${scenarioT("scenario.module.combat", "战斗")}：</strong>${battleLine}</p>` : "",
        skills.length ? `<p data-user-content="true"><strong>${scenarioT("scenario.preview.skills", "技能")}：</strong>${skills.map((skill) => `${scenarioEscapeHtml(skill.name)} ${scenarioEscapeHtml(skill.base)}`).join(" / ")}</p>` : "",
    ].join("");
}

function renderPreviewModules(modules: ScenarioModule[]): string {
    return modules.map((module, index) => `
        <div class="scenario-preview-segment">
            <h5>${scenarioEscapeHtml(module.title || defaultModuleTitle(module.module_type, index + 1))}</h5>
            <p data-user-content="true"><strong>${scenarioEscapeHtml(module.title)}</strong></p>
            <p data-user-content="true">${scenarioEscapeHtml(module.summary || module.content || "")}</p>
            ${module.code ? `<p><strong>${scenarioT("scenario.preview.code", "编号")}：</strong>${scenarioEscapeHtml(module.code)}</p>` : ""}
            ${module.open_ending ? `<p><strong>${scenarioT("scenario.preview.open_ending", "开放结局")}：</strong>${scenarioT("scenario.preview.yes", "是")}</p>` : ""}
            ${module.module_type === "scene" ? `<div class="scenario-preview-triggers"><p><strong>${scenarioT("scenario.preview.triggers", "触发器")}：</strong>${(module.triggers || []).map((trigger) => scenarioEscapeHtml(trigger.display_name || `触发器${trigger.id}`)).join(" / ") || scenarioT("scenario.preview.none", "无")}</p>${(module.triggers || []).map((trigger) => {
                const url = trigger.asset_url || "";
                if (!url) return "";
                const label = scenarioEscapeHtml(trigger.display_name || trigger.asset_name || `触发器${trigger.id}`);
                return trigger.content_mode === "image"
                    ? `<figure class="scenario-trigger-preview"><img src="${scenarioEscapeHtml(url)}" alt="${label}" loading="lazy"><figcaption>${label}</figcaption></figure>`
                    : `<p class="scenario-trigger-preview"><a href="${scenarioEscapeHtml(url)}" target="_blank" rel="noopener">${label}</a></p>`;
            }).join("")}</div>` : ""}
             ${module.module_type === "timeline" && module.timeline_entries?.length ? `<div class="scenario-preview-timeline" data-user-content="true">${module.timeline_entries.map((entry) => `<p><strong>${scenarioEscapeHtml(entry.time_point)}</strong> ${scenarioEscapeHtml(entry.event)}</p>`).join("")}</div>` : ""}
            ${(module.module_type === "npc" || module.module_type === "monster") ? renderEntityPreview(module) : ""}
             ${module.content ? `<p data-user-content="true">${scenarioEscapeHtml(module.content)}</p>` : ""}
        </div>
    `).join("");
}

function renderTriggerList(triggers: ScenarioTrigger[]): string {
    return triggers.map((trigger, index) => renderTriggerRow(index + 1, trigger)).join("");
}

function renderTriggerRow(index: number, trigger?: ScenarioTrigger): string {
    const mode = trigger?.content_mode || "text";
    const option = (value: ScenarioTriggerContentMode, label: string) => `<option value="${value}" ${mode === value ? "selected" : ""}>${label}</option>`;
    const assetName = trigger?.asset_name || "";
    const assetUrl = trigger?.asset_url || "";
    const preview = assetUrl
        ? (mode === "image"
            ? `<a href="${scenarioEscapeHtml(assetUrl)}" target="_blank" rel="noopener"><img src="${scenarioEscapeHtml(assetUrl)}" alt="${scenarioEscapeHtml(assetName || "触发器附件")}" loading="lazy"></a>`
            : `<a href="${scenarioEscapeHtml(assetUrl)}" target="_blank" rel="noopener">${scenarioEscapeHtml(assetName || assetUrl)}</a>`)
        : "";
    return `
        <div class="scenario-trigger-item" data-trigger-item data-asset-url="${scenarioEscapeHtml(assetUrl)}" data-asset-name="${scenarioEscapeHtml(assetName)}" data-asset-mime="${scenarioEscapeHtml(trigger?.asset_mime || "")}" data-asset-size="${scenarioEscapeHtml(trigger?.asset_size || "")}">
            <div class="scenario-trigger-grid">
                <label>编号<input class="form-control" data-trigger-id type="number" min="1" value="${scenarioEscapeHtml(trigger?.id || index)}"></label>
                <label>${scenarioT("scenario.module.trigger.display_name", "显示名称")}<input class="form-control" data-trigger-name value="${scenarioEscapeHtml(trigger?.display_name || "")}" placeholder="${scenarioT("scenario.module.trigger.display_name_placeholder", "例如：纸条内容")}"></label>
                <label>${scenarioT("scenario.module.trigger.keyword", "关键词")}<input class="form-control" data-trigger-keyword value="${scenarioEscapeHtml(trigger?.keyword || "")}"></label>
                <label>模式<select class="form-select" data-trigger-mode>${option("text", "文本")}${option("richtext", "富文本")}${option("image", "图片")}${option("file", "文件")}</select></label>
                <button type="button" class="btn btn-sm btn-danger" data-remove-trigger>${scenarioT("scenario.module.remove", "删除")}</button>
            </div>
            <input class="form-control mt-2" data-trigger-condition value="${scenarioEscapeHtml(trigger?.condition || "")}" placeholder="${scenarioT("scenario.module.trigger.condition_placeholder", "触发条件，例如：检定侦察成功")}">
            <div class="scenario-trigger-grid mt-2">
                <label>剧透等级<input class="form-control" data-trigger-spoiler type="number" min="0" max="5" value="${scenarioEscapeHtml(trigger?.spoiler_level ?? 0)}"></label>
                <label>可见性<select class="form-select" data-trigger-visibility><option value="player_visible" ${(trigger?.visibility || "player_visible") === "player_visible" ? "selected" : ""}>玩家可见</option><option value="kp_only" ${trigger?.visibility === "kp_only" ? "selected" : ""}>仅 KP</option></select></label>
                <label class="scenario-module-toggle"><span>可重复</span><input type="checkbox" data-trigger-repeatable ${trigger?.repeatable ? "checked" : ""}></label>
                <label>优先级<input class="form-control" data-trigger-priority type="number" value="${scenarioEscapeHtml(trigger?.priority ?? 0)}"></label>
                <label class="scenario-module-toggle"><span>启用</span><input type="checkbox" data-trigger-enabled ${trigger?.enabled !== false ? "checked" : ""}></label>
            </div>
            <textarea class="form-control mt-2" data-trigger-content rows="3" placeholder="${scenarioT("scenario.module.trigger.placeholder", "触发内容")}">${scenarioEscapeHtml(trigger?.content || "")}</textarea>
            <div class="scenario-trigger-upload mt-2">
                <input class="form-control" data-trigger-file type="file">
                <div class="scenario-trigger-preview">${preview}</div>
            </div>
        </div>
    `;
}

function renderCustomInputRows(inputs: ScenarioModuleInputItem[]): string {
    return inputs.map((input, index) => renderCustomInputRow(index + 1, input)).join("");
}

function renderCustomInputRow(index: number, input?: ScenarioModuleInputItem): string {
    return `
        <div class="scenario-module-custom-row" data-custom-input-item data-input-id="${scenarioEscapeHtml(input?.id || `input-${index}`)}">
            <input class="form-control" data-custom-input-label placeholder="${scenarioT("scenario.module.input.label", "输入项标题")}" value="${scenarioEscapeHtml(input?.label || "")}">
            <textarea class="form-control" data-custom-input-value rows="2" placeholder="${scenarioT("scenario.module.input.content", "输入项内容")}">${scenarioEscapeHtml(input?.value || "")}</textarea>
            <label class="scenario-module-toggle">
                <span>${scenarioT("scenario.module.send_to_ai", "初始化时发送给 AI")}</span>
                <input type="checkbox" data-custom-input-send ${input?.send_to_ai ? "checked" : ""}>
            </label>
            <button type="button" class="btn btn-sm btn-danger" data-remove-custom-input>${scenarioT("scenario.module.remove", "删除")}</button>
        </div>
    `;
}

function renderTimelineEntries(entries: ScenarioModuleTimelineEntry[]): string {
    return entries.map((entry, index) => renderTimelineEntryRow(index + 1, entry)).join("");
}

function renderTimelineEntryRow(index: number, entry?: ScenarioModuleTimelineEntry): string {
    return `
        <div class="scenario-module-timeline-row" data-timeline-entry-item data-timeline-entry-id="${scenarioEscapeHtml(entry?.id || `timeline-${index}`)}">
            <input class="form-control" data-timeline-time-point placeholder="${scenarioT("scenario.module.timeline.placeholder.time_point", "如：1930-10-31 21:00")}" value="${scenarioEscapeHtml(entry?.time_point || "")}">
            <textarea class="form-control" data-timeline-event rows="2" placeholder="${scenarioT("scenario.module.timeline.placeholder.event", "该时间点发生的事件")}">${scenarioEscapeHtml(entry?.event || "")}</textarea>
            <button type="button" class="btn btn-sm btn-danger" data-remove-timeline-entry>${scenarioT("scenario.module.timeline.remove_entry", "删除列")}</button>
        </div>
    `;
}

function renderSkillRows(skills: ScenarioModuleSkillRow[]): string {
    return skills.map((skill, index) => renderSkillRow(index + 1, skill)).join("");
}

function renderSkillRow(index: number, skill?: ScenarioModuleSkillRow): string {
    return `
        <div class="scenario-module-stat-row" data-skill-item>
            <input class="form-control" data-skill-name placeholder="${scenarioT("scenario.module.skill.name", "技能名称")}" value="${scenarioEscapeHtml(skill?.name || "")}">
            <input class="form-control" data-skill-base placeholder="${scenarioT("scenario.module.skill.base", "基础%")}" value="${scenarioEscapeHtml(skill?.base || "")}">
            <button type="button" class="btn btn-sm btn-danger" data-remove-skill>${scenarioT("scenario.module.remove", "删除")}</button>
        </div>
    `;
}

function renderWeaponRows(weapons: ScenarioModuleWeaponRow[]): string {
    return weapons.map((weapon, index) => renderWeaponRow(index + 1, weapon)).join("");
}

function renderWeaponRow(index: number, weapon?: ScenarioModuleWeaponRow): string {
    const row = weapon || blankWeaponRow();
    return `
        <div class="scenario-module-weapon-row" data-weapon-item>
            <input class="form-control" data-weapon-name placeholder="${scenarioT("scenario.module.weapon.name", "武器名称")}" value="${scenarioEscapeHtml(row.name)}">
            <input class="form-control" data-weapon-skill placeholder="${scenarioT("scenario.module.weapon.skill", "技能")}" value="${scenarioEscapeHtml(row.skill)}">
            <input class="form-control" data-weapon-damage placeholder="${scenarioT("scenario.module.weapon.damage", "伤害")}" value="${scenarioEscapeHtml(row.damage)}">
            <input class="form-control" data-weapon-range placeholder="${scenarioT("scenario.module.weapon.range", "射程")}" value="${scenarioEscapeHtml(row.range)}">
            <input class="form-control" data-weapon-attacks placeholder="${scenarioT("scenario.module.weapon.attacks", "攻击次数")}" value="${scenarioEscapeHtml(row.attacks)}">
            <input class="form-control" data-weapon-ammo placeholder="${scenarioT("scenario.module.weapon.ammo", "弹药")}" value="${scenarioEscapeHtml(row.ammo)}">
            <input class="form-control" data-weapon-malfunction placeholder="${scenarioT("scenario.module.weapon.malfunction", "故障率")}" value="${scenarioEscapeHtml(row.malfunction)}">
            <input class="form-control" data-weapon-note placeholder="${scenarioT("scenario.module.weapon.note", "备注")}" value="${scenarioEscapeHtml(row.note)}">
            <button type="button" class="btn btn-sm btn-danger" data-remove-weapon>${scenarioT("scenario.module.remove", "删除")}</button>
        </div>
    `;
}

function readTriggerFile(inputElement: HTMLInputElement): Promise<void> {
    const file = inputElement.files?.[0];
    if (!file) return Promise.resolve();
    const maxSize = window.configManager?.get<number>("general", "scenario", "trigger_max_file_size", 5242880) || 5242880;
    if (file.size > maxSize) {
        inputElement.value = "";
        return Promise.reject(new Error(`触发器附件大小不能超过 ${Math.round(maxSize / 1024 / 1024)}MB`));
    }

    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            inputElement.dataset.dataUrl = String(reader.result || "");
            inputElement.dataset.assetName = file.name;
            inputElement.dataset.assetMime = file.type || "application/octet-stream";
            inputElement.dataset.assetSize = String(file.size);
            const preview = inputElement.closest("[data-trigger-item]")?.querySelector<HTMLElement>(".scenario-trigger-preview");
            if (preview) preview.textContent = file.name;
            resolve();
        };
        reader.onerror = () => reject(reader.error || new Error("读取触发器文件失败"));
        reader.readAsDataURL(file);
    });
}

function renderAttributeInputs(attributes: Record<string, string>): string {
    return ["STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC"].map((key) => `
        <label class="scenario-module-attribute">
            <span>${key}</span>
            <input class="form-control" data-attribute-key="${key}" value="${scenarioEscapeHtml(attributes[key] || "N/A")}">
        </label>
    `).join("");
}

function renderBattleInputs(battle: Record<string, string>): string {
    const fields: Array<[string, string]> = [
        ["hp", "HP"],
        ["max_hp", "Max HP"],
        ["dex", "DEX"],
        ["str", "STR"],
        ["siz", "SIZ"],
        ["con", "CON"],
        ["db", "DB"],
        ["build", "Build"],
        ["mov", "Move"],
        ["armor", "Armor"],
    ];
    return fields.map(([key, label]) => `
        <label class="scenario-module-battle-field">
            <span>${label}</span>
            <input class="form-control" data-battle-key="${key}" value="${scenarioEscapeHtml(battle[key] || "N/A")}">
        </label>
    `).join("");
}

function refreshModuleCard(card: HTMLElement): void {
    const type = (card.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene") as ScenarioModuleType;
    const description = card.querySelector<HTMLElement>("[data-module-description]");
    if (description) description.textContent = moduleDescription(type);

    const textPanel = card.querySelector<HTMLElement>("[data-module-text-panel]");
    const scenePanel = card.querySelector<HTMLElement>("[data-module-scene-panel]");
    const timelinePanel = card.querySelector<HTMLElement>("[data-module-timeline-panel]");
    const entityPanel = card.querySelector<HTMLElement>("[data-module-entity-panel]");
    const customPanel = card.querySelector<HTMLElement>("[data-module-custom-panel]");
    const entityNameLabel = card.querySelector<HTMLElement>("[data-entity-name]")?.closest(".scenario-module-field")?.querySelector("span");
    const entityCodeLabel = card.querySelector<HTMLElement>("[data-entity-code]")?.closest(".scenario-module-field")?.querySelector("span");
    const entitySummaryLabel = card.querySelector<HTMLElement>("[data-entity-summary]")?.closest(".scenario-module-field")?.querySelector("span");

    textPanel?.toggleAttribute("hidden", !["background", "public_info", "preparation", "scene", "ending", "custom"].includes(type));
    scenePanel?.toggleAttribute("hidden", type !== "scene");
    timelinePanel?.toggleAttribute("hidden", type !== "timeline");
    entityPanel?.toggleAttribute("hidden", !["monster", "npc"].includes(type));
    customPanel?.toggleAttribute("hidden", type !== "custom");

    if (entityNameLabel) entityNameLabel.textContent = type === "monster" ? "怪物名称" : "NPC 名称";
    if (entityCodeLabel) entityCodeLabel.textContent = type === "monster" ? "怪物编号" : "NPC 编号";
    if (entitySummaryLabel) entitySummaryLabel.textContent = type === "monster" ? "怪物简介" : "NPC 简介";

    if (type === "monster" || type === "npc") {
        const codeInput = card.querySelector<HTMLInputElement>("[data-entity-code]");
        if (codeInput && !codeInput.value.trim()) {
            codeInput.value = generateModuleCode(type, collectScenarioModules());
        }
    } else if (type === "timeline") {
        const timelineList = card.querySelector<HTMLElement>("[data-timeline-entry-list]");
        if (timelineList && timelineList.querySelectorAll("[data-timeline-entry-item]").length === 0) {
            timelineList.insertAdjacentHTML("beforeend", renderTimelineEntryRow(1));
        }
    }
}

function collectScenarioModules(): ScenarioModule[] {
    const cards = Array.from(document.querySelectorAll<HTMLElement>(".scenario-module-item"));
    let sceneIndex = 0;
    let endingIndex = 0;
    const typeCounts = new Map<ScenarioModuleType, number>();

    return cards.map((card, index) => {
        const type = (card.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene") as ScenarioModuleType;
        const typeIndex = (typeCounts.get(type) || 0) + 1;
        typeCounts.set(type, typeIndex);
        const module: ScenarioModule = {
            id: card.dataset.moduleId || createModuleId(type, index + 1),
            module_type: type,
            title: normalizeModuleTitle(type, card.querySelector<HTMLInputElement>("[data-module-title]")?.value || "", typeIndex),
            summary: card.querySelector<HTMLInputElement>("[data-module-summary]")?.value.trim() || "",
            content: card.querySelector<HTMLTextAreaElement>("[data-module-content]")?.value || "",
            notes: card.querySelector<HTMLTextAreaElement>("[data-module-notes]")?.value || "",
            visibility: moduleDefaultVisibility(type),
            send_to_ai: card.querySelector<HTMLInputElement>("[data-module-send-to-ai]")?.checked !== false,
            code: card.querySelector<HTMLInputElement>("[data-entity-code]")?.value.trim() || "",
            scene_id: undefined,
            ending_id: undefined,
            open_ending: undefined,
            inputs: [],
            timeline_entries: [],
            attributes: {},
            battle: {},
            skills: [],
            weapons: [],
            triggers: [],
        };

        if (type === "scene") {
            sceneIndex += 1;
            module.scene_id = sceneIndex;
            module.triggers = collectTriggers(card);
            if (!module.summary) module.summary = (module.content || "").slice(0, 120);
        } else if (type === "ending") {
            endingIndex += 1;
            module.ending_id = endingIndex;
            module.open_ending = Boolean(card.querySelector<HTMLInputElement>("[data-ending-open]")?.checked);
            if (!module.summary) module.summary = (module.content || "").slice(0, 120);
        } else if (type === "monster" || type === "npc") {
            module.title = card.querySelector<HTMLInputElement>("[data-entity-name]")?.value.trim() || module.title;
            module.summary = card.querySelector<HTMLTextAreaElement>("[data-entity-summary]")?.value.trim() || module.summary;
            module.content = module.summary || module.content;
            module.code = card.querySelector<HTMLInputElement>("[data-entity-code]")?.value.trim() || module.code;
            module.attributes = collectAttributeMap(card);
            module.battle = collectBattleMap(card);
            module.weapons = collectWeapons(card);
            module.skills = collectSkills(card);
        } else if (type === "timeline") {
            module.timeline_entries = collectTimelineEntries(card);
            module.content = module.timeline_entries.map((entry) => `${entry.time_point} ${entry.event}`.trim()).filter(Boolean).join("\n");
        } else if (type === "custom") {
            module.inputs = collectCustomInputs(card);
        }

        return module;
    });
}

function collectTriggers(card: HTMLElement): ScenarioTrigger[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-trigger-item]")).map((item, index) => {
        const id = Number.parseInt(item.querySelector<HTMLInputElement>("[data-trigger-id]")?.value || String(index + 1), 10);
        const mode = (item.querySelector<HTMLSelectElement>("[data-trigger-mode]")?.value || "text") as ScenarioTriggerContentMode;
        const fileInput = item.querySelector<HTMLInputElement>("[data-trigger-file]");
        const trigger: ScenarioTrigger = {
            id: Number.isFinite(id) && id > 0 ? id : index + 1,
            display_name: item.querySelector<HTMLInputElement>("[data-trigger-name]")?.value.trim() || "",
            keyword: item.querySelector<HTMLInputElement>("[data-trigger-keyword]")?.value.trim() || "",
            condition: item.querySelector<HTMLInputElement>("[data-trigger-condition]")?.value.trim() || "",
            content_mode: mode,
            spoiler_level: Number.parseInt(item.querySelector<HTMLInputElement>("[data-trigger-spoiler]")?.value || "0", 10) || 0,
            visibility: (item.querySelector<HTMLSelectElement>("[data-trigger-visibility]")?.value || "player_visible") as TriggerVisibility,
            repeatable: item.querySelector<HTMLInputElement>("[data-trigger-repeatable]")?.checked === true,
            priority: Number.parseInt(item.querySelector<HTMLInputElement>("[data-trigger-priority]")?.value || "0", 10) || 0,
            enabled: item.querySelector<HTMLInputElement>("[data-trigger-enabled]")?.checked !== false,
        };
        const content = item.querySelector<HTMLTextAreaElement>("[data-trigger-content]")?.value || "";
        if (mode === "text" || mode === "richtext") trigger.content = content;
        if (fileInput?.dataset.dataUrl) trigger.asset_data_url = fileInput.dataset.dataUrl;
        const assetName = fileInput?.dataset.assetName || item.dataset.assetName || "";
        const assetMime = fileInput?.dataset.assetMime || item.dataset.assetMime || "";
        const assetSize = fileInput?.dataset.assetSize || item.dataset.assetSize || "";
        if (assetName) trigger.asset_name = assetName;
        if (assetMime) trigger.asset_mime = assetMime;
        if (assetSize) trigger.asset_size = Number(assetSize);
        if (item.dataset.assetUrl) trigger.asset_url = item.dataset.assetUrl;
        return trigger;
    }).filter((trigger) => trigger.keyword);
}

function collectCustomInputs(card: HTMLElement): ScenarioModuleInputItem[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-custom-input-item]")).map((item, index) => ({
        id: item.dataset.inputId || `input-${index + 1}`,
        label: item.querySelector<HTMLInputElement>("[data-custom-input-label]")?.value.trim() || "",
        value: item.querySelector<HTMLTextAreaElement>("[data-custom-input-value]")?.value || "",
        send_to_ai: item.querySelector<HTMLInputElement>("[data-custom-input-send]")?.checked === true,
    })).filter((input) => input.label || input.value);
}

function collectTimelineEntries(card: HTMLElement): ScenarioModuleTimelineEntry[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-timeline-entry-item]")).map((item, index) => ({
        id: item.dataset.timelineEntryId || `timeline-${index + 1}`,
        time_point: item.querySelector<HTMLInputElement>("[data-timeline-time-point]")?.value.trim() || "",
        event: item.querySelector<HTMLTextAreaElement>("[data-timeline-event]")?.value.trim() || "",
    })).filter((entry) => entry.time_point || entry.event);
}

function collectSkills(card: HTMLElement): ScenarioModuleSkillRow[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-skill-item]")).map((item) => ({
        name: item.querySelector<HTMLInputElement>("[data-skill-name]")?.value.trim() || "",
        base: item.querySelector<HTMLInputElement>("[data-skill-base]")?.value.trim() || "",
    })).filter((skill) => skill.name || skill.base);
}

function collectWeapons(card: HTMLElement): ScenarioModuleWeaponRow[] {
    return Array.from(card.querySelectorAll<HTMLElement>("[data-weapon-item]")).map((item) => ({
        name: item.querySelector<HTMLInputElement>("[data-weapon-name]")?.value.trim() || "",
        skill: item.querySelector<HTMLInputElement>("[data-weapon-skill]")?.value.trim() || "",
        damage: item.querySelector<HTMLInputElement>("[data-weapon-damage]")?.value.trim() || "",
        range: item.querySelector<HTMLInputElement>("[data-weapon-range]")?.value.trim() || "",
        attacks: item.querySelector<HTMLInputElement>("[data-weapon-attacks]")?.value.trim() || "",
        ammo: item.querySelector<HTMLInputElement>("[data-weapon-ammo]")?.value.trim() || "",
        malfunction: item.querySelector<HTMLInputElement>("[data-weapon-malfunction]")?.value.trim() || "",
        note: item.querySelector<HTMLInputElement>("[data-weapon-note]")?.value.trim() || "",
    })).filter((weapon) => weapon.name || weapon.skill || weapon.damage || weapon.range || weapon.attacks || weapon.ammo || weapon.malfunction || weapon.note);
}

function collectAttributeMap(card: HTMLElement): Record<string, string> {
    return Array.from(card.querySelectorAll<HTMLInputElement>("[data-attribute-key]")).reduce((acc, input) => {
        const key = input.dataset.attributeKey || "";
        if (key) acc[key] = input.value.trim() || "N/A";
        return acc;
    }, {} as Record<string, string>);
}

function collectBattleMap(card: HTMLElement): Record<string, string> {
    return Array.from(card.querySelectorAll<HTMLInputElement>("[data-battle-key]")).reduce((acc, input) => {
        const key = input.dataset.battleKey || "";
        if (key) acc[key] = input.value.trim() || "N/A";
        return acc;
    }, {} as Record<string, string>);
}

function countModulesOfType(modules: ScenarioModule[], type: ScenarioModuleType): number {
    return modules.filter((module) => module.module_type === type).length;
}

function getModuleTypeOrdinal(card: HTMLElement, type: ScenarioModuleType): number {
    const cards = Array.from(document.querySelectorAll<HTMLElement>(".scenario-module-item")).filter((item) => {
        const itemType = (item.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene") as ScenarioModuleType;
        return itemType === type;
    });
    const index = cards.indexOf(card);
    return index >= 0 ? index + 1 : cards.length + 1;
}

function syncModuleDefaultsForTypeChange(card: HTMLElement): void {
    const titleInput = card.querySelector<HTMLInputElement>("[data-module-title]");
    if (!titleInput) return;
    const previousDefaultTitle = card.dataset.moduleDefaultTitle || "";
    const currentTitle = titleInput.value.trim();
    if (!currentTitle || currentTitle === previousDefaultTitle) {
        const type = (card.querySelector<HTMLSelectElement>("[data-module-type]")?.value || "scene") as ScenarioModuleType;
        titleInput.value = defaultModuleTitle(type, getModuleTypeOrdinal(card, type));
        card.dataset.moduleDefaultTitle = titleInput.value;
    }
}

