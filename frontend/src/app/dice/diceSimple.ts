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

    private dieCount(): number {
        if (this.plan.mode === "percentile") return 2;
        return Math.max(1, Math.min(20, Number(this.plan.count) || 1));
    }

    private labelsFor(faces: number[], labels: string[] | undefined): string[] {
        if (labels && labels.length === faces.length) return labels.map(String);
        if (this.plan.mode === "percentile" && faces.length === 2) {
            return [String((Number(faces[0]) || 0) * 10).padStart(2, "0"), String(Number(faces[1]) || 0)];
        }
        return faces.map((face) => String(face));
    }

    private clearStage(): void {
        this.disposed = false;
        this.stage.innerHTML = "";
    }

    private renderDice(labels: string[]): HTMLElement[] {
        const dice: HTMLElement[] = [];
        const count = this.dieCount();
        for (let index = 0; index < count; index += 1) {
            const die = document.createElement("div");
            die.className = "dice-simple-die";
            const face = labels[index] ?? "";
            die.dataset.face = face;
            const value = document.createElement("span");
            value.textContent = face;
            die.appendChild(value);
            if (this.plan.mode === "percentile" && index === 0) die.classList.add("dice-simple-tens");
            this.applyFace(die, face);
            this.stage.appendChild(die);
            dice.push(die);
        }
        return dice;
    }

    private applyFace(die: HTMLElement, face: string): void {
        const imageUrl = this.faceUrls[face] || this.faceUrls[String(Number(face))] || "";
        if (imageUrl) {
            die.classList.add("has-face-texture");
            die.style.setProperty("--dice-face-image", `url("${imageUrl}")`);
        }
    }

    static(snapshot: DiceResultSnapshot): void {
        this.clearStage();
        this.renderDice(this.labelsFor(snapshot.faces || [], snapshot.labels));
    }

    async play(snapshot: DiceResultSnapshot): Promise<void> {
        this.clearStage();
        const labels = this.labelsFor(snapshot.faces || [], snapshot.labels);
        const dice = this.renderDice(labels.map(() => "?"));
        dice.forEach((die) => die.classList.add("rolling"));
        const sides = Math.max(2, Number(this.plan.sides) || 6);
        const scramble = window.setInterval(() => {
            if (this.disposed) return;
            dice.forEach((die) => {
                const span = die.querySelector<HTMLElement>("span");
                if (span) span.textContent = String(1 + Math.floor(Math.random() * sides));
            });
        }, 70);
        await new Promise((resolve) => window.setTimeout(resolve, 620));
        window.clearInterval(scramble);
        if (this.disposed) return;
        dice.forEach((die, index) => {
            die.classList.remove("rolling");
            die.classList.add("settled");
            const face = labels[index] ?? "";
            die.dataset.face = face;
            const span = die.querySelector<HTMLElement>("span");
            if (span) span.textContent = face;
            this.applyFace(die, face);
        });
        await new Promise((resolve) => window.setTimeout(resolve, 280));
    }

    dispose(): void {
        this.disposed = true;
        window.setTimeout(() => this.stage?.remove(), 0);
    }
}
