/** 简易骰子动画：纯 DOM + CSS 抖动，作为 3D 不可用时的中间档位。 */
class DiceSimpleRenderer {
    private container: HTMLElement;
    private stage: HTMLElement;
    private plan: DicePlan;
    private faceUrls: Record<string, string>;
    private disposed = false;

    constructor(container: HTMLElement, plan: DicePlan, faceUrls: Record<string, string>) {
        this.container = container;
        this.plan = plan;
        this.faceUrls = faceUrls || {};
        this.stage = document.createElement("div");
        this.stage.className = "dice-simple-stage";
        this.container.appendChild(this.stage);
    }

    private groups(snapshot: DiceResultSnapshot | null): DiceGroupSpec[] {
        // 简易档位负责兜底任意面数：不支持的骰型用通用方块 + 数字呈现，
        // 真正的数量上限由调用方按 DICE_SIMPLE_RENDER_LIMIT 提示。
        return diceSnapshotGroups(this.plan, snapshot);
    }

    private clearStage(): void {
        this.disposed = false;
        this.stage.innerHTML = "";
    }

    private static pipsFor(value: number): number[] {
        const layouts: Record<number, number[]> = {
            1: [4],
            2: [0, 8],
            3: [0, 4, 8],
            4: [0, 2, 6, 8],
            5: [0, 2, 4, 6, 8],
            6: [0, 2, 3, 5, 6, 8],
        };
        return layouts[value] || [];
    }

    private renderFaceContent(die: HTMLElement, type: string, label: string, value: number): void {
        die.innerHTML = "";
        if (type === "d6") {
            const grid = document.createElement("div");
            grid.className = "dice-pips";
            const active = new Set(DiceSimpleRenderer.pipsFor(value));
            for (let index = 0; index < 9; index += 1) {
                const pip = document.createElement("span");
                pip.className = active.has(index) ? "dice-pip on" : "dice-pip";
                grid.appendChild(pip);
            }
            die.appendChild(grid);
        } else {
            const span = document.createElement("span");
            span.className = "dice-face-value";
            span.textContent = label;
            die.appendChild(span);
        }
    }

    private applyFace(die: HTMLElement, type: string, label: string, value: number): void {
        this.renderFaceContent(die, type, label, value);
        const imageUrl = this.faceUrls[String(value)] || "";
        if (imageUrl) {
            die.classList.add("has-face-texture");
            die.style.setProperty("--dice-face-image", `url("${imageUrl}")`);
        } else {
            die.classList.remove("has-face-texture");
            die.style.removeProperty("--dice-face-image");
        }
    }

    private renderDice(groups: DiceGroupSpec[], rolling: boolean, selected: DiceSelection | null | undefined): Array<{ die: HTMLElement; group: DiceGroupSpec; value: number; index: number }> {
        const dice: Array<{ die: HTMLElement; group: DiceGroupSpec; value: number; index: number }> = [];
        groups.forEach((group) => {
            const groupElement = document.createElement("div");
            groupElement.className = `dice-simple-group dice-simple-group-${group.role || "normal"}`;
            const roleLabel = diceGroupRoleLabel(group);
            if (roleLabel && group.role !== "normal") {
                const caption = document.createElement("span");
                caption.className = "dice-simple-group-label";
                caption.textContent = roleLabel;
                groupElement.appendChild(caption);
            }
            const row = document.createElement("div");
            row.className = "dice-simple-row";
            for (let index = 0; index < group.count; index += 1) {
                const die = document.createElement("div");
                die.className = "dice-simple-die";
                die.classList.add(`dice-type-${diceRenderType(group.type) || "unknown"}`);
                die.dataset.type = group.type;
                const value = Number(group.faces[index] ?? 0);
                const label = group.labels[index] ?? String(value);
                if (rolling) {
                    const span = document.createElement("span");
                    span.className = "dice-face-value";
                    span.textContent = "?";
                    die.appendChild(span);
                } else {
                    this.applyFace(die, group.type, label, value);
                }
                row.appendChild(die);
                dice.push({ die, group, value, index });
            }
            groupElement.appendChild(row);
            this.stage.appendChild(groupElement);
        });
        // 选中高亮：选中的骰发亮上浮，其余降低存在感。
        const selectedIndex = diceSelectedIndex(groups, selected);
        dice.forEach((item, index) => {
            if (index === selectedIndex) item.die.classList.add("dice-selected");
            else if (selectedIndex >= 0) item.die.classList.add("dice-dimmed");
        });
        return dice;
    }

    static(snapshot: DiceResultSnapshot): void {
        this.clearStage();
        this.renderDice(this.groups(snapshot), false, snapshot.selected ?? null);
    }

    /** 32 位确定性伪随机数，与 3D 物理使用同一算法，保证各客户端点数一致。 */
    private static seededRandom(seed: number): () => number {
        let state = seed >>> 0;
        return () => {
            state = (state + 0x6d2b79f5) >>> 0;
            let value = state;
            value = Math.imul(value ^ (value >>> 15), value | 1);
            value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
            return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
        };
    }

    private static labelFor(type: string, role: string, value: number): string {
        if (type === "d10" && (role === "tens" || role === "candidate")) return String(value * 10).padStart(2, "0");
        return String(value);
    }

    /** 按 seed 推导每个骰子的最终面值；d10 在百分骰里为 0-9，普通骰为 1-10。 */
    private seededGroups(groups: DiceGroupSpec[], random: () => number): { groups: DiceGroupSpec[]; values: number[] } {
        const values: number[] = [];
        const seeded = groups.map((group) => {
            const faces: number[] = [];
            const labels: string[] = [];
            const sides = Math.max(2, diceTypeSides(group.type) || 6);
            for (let index = 0; index < group.count; index += 1) {
                let value: number;
                if (group.type === "d10") {
                    value = group.role === "normal" ? 1 + Math.floor(random() * 10) : Math.floor(random() * 10);
                } else {
                    value = 1 + Math.floor(random() * sides);
                }
                faces.push(value);
                labels.push(DiceSimpleRenderer.labelFor(group.type, group.role, value));
                values.push(value);
            }
            return { ...group, faces, labels };
        });
        return { groups: seeded, values };
    }

    async play(snapshot: DiceResultSnapshot, seed?: number | null): Promise<number[] | null> {
        this.clearStage();
        const groups = this.groups(snapshot);
        const seeded = typeof seed === "number" && Number.isFinite(seed)
            ? this.seededGroups(groups, DiceSimpleRenderer.seededRandom(seed))
            : null;
        const dice = this.renderDice(seeded ? seeded.groups : groups, true, snapshot.selected ?? null);
        dice.forEach((item) => item.die.classList.add("rolling"));
        const scramble = window.setInterval(() => {
            if (this.disposed) return;
            dice.forEach((item) => {
                const sides = Math.max(2, diceTypeSides(item.group.type) || 6);
                const value = item.group.type === "d10" ? Math.floor(Math.random() * sides) : 1 + Math.floor(Math.random() * sides);
                const span = item.die.querySelector<HTMLElement>(".dice-face-value");
                if (span) span.textContent = String(value);
            });
        }, 70);
        await new Promise((resolve) => window.setTimeout(resolve, 620));
        window.clearInterval(scramble);
        if (this.disposed) return seeded ? seeded.values : null;
        dice.forEach((item) => {
            item.die.classList.remove("rolling");
            // 骰子停止抖动后不再做任何位移/缩放动画，直接显示最终点数。
            const value = Number(item.group.faces[item.index] ?? 0);
            const label = item.group.labels[item.index] ?? String(value);
            this.applyFace(item.die, item.group.type, label, value);
        });
        return seeded ? seeded.values : null;
    }

    dispose(): void {
        this.disposed = true;
        this.stage.remove();
    }
}
