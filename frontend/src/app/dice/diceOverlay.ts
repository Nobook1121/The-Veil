/**
 * 聊天内嵌骰桌：作为一条聊天内容插入聊天记录，所有玩家可见，
 * 但只有目标玩家会看到「投掷」按钮。结算后保留骰桌与骰子，结果文字显示在骰桌下方。
 */
class DiceOverlayView {
    readonly root: HTMLElement;
    readonly stage: HTMLElement;
    readonly result: HTMLElement;
    readonly requestId: string;
    private labelElement: HTMLElement;
    private statusElement: HTMLElement;
    private actionElement: HTMLElement;
    private plan: DicePlan;

    constructor(host: HTMLElement, plan: DicePlan, requestId: string, attach = true) {
        this.plan = plan;
        this.requestId = requestId;
        this.root = document.createElement("div");
        this.root.className = "dice-inline";
        this.root.dataset.requestId = requestId;
        this.root.innerHTML = `
            <div class="dice-inline-head">
                <span class="dice-inline-label"></span>
            </div>
            <div class="dice-inline-surface">
                <div class="dice-inline-stage"></div>
            </div>
            <div class="dice-inline-result" aria-live="polite"></div>
            <div class="dice-inline-status"></div>
            <div class="dice-inline-actions"></div>
        `;
        this.labelElement = this.root.querySelector(".dice-inline-label") as HTMLElement;
        this.stage = this.root.querySelector(".dice-inline-stage") as HTMLElement;
        this.result = this.root.querySelector(".dice-inline-result") as HTMLElement;
        this.statusElement = this.root.querySelector(".dice-inline-status") as HTMLElement;
        this.actionElement = this.root.querySelector(".dice-inline-actions") as HTMLElement;
        diceApplyAppearance(this.root, diceLoadAppearance());
        this.updateLabel();
        if (attach) host.appendChild(this.root);
    }

    private updateLabel(): void {
        this.labelElement.textContent = dicePlanLabel(this.plan);
    }

    setWaiting(name: string, canThrow: boolean, onThrow: () => void): void {
        this.root.dataset.state = "pending";
        this.statusElement.textContent = canThrow
            ? `轮到你投掷：点击「投掷」或输入 /roll`
            : `等待 ${name || "玩家"} 投掷骰子…`;
        this.statusElement.className = "dice-inline-status waiting";
        this.actionElement.innerHTML = "";
        this.stage.innerHTML = "";
        this.result.innerHTML = "";
        if (canThrow) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-sm btn-primary dice-inline-throw";
            button.textContent = "投掷";
            button.addEventListener("click", () => {
                button.disabled = true;
                onThrow();
            });
            this.actionElement.appendChild(button);
        }
    }

    setRolling(): void {
        this.root.dataset.state = "rolling";
        this.statusElement.textContent = "";
        this.statusElement.className = "dice-inline-status";
        this.actionElement.innerHTML = "";
        // 动画期间不展示结果数字：结果必须等于动画结束时朝上的骰面。
        this.result.innerHTML = "";
    }

    setResult(snapshot: DiceResultSnapshot): void {
        this.result.innerHTML = diceResultHtml(snapshot);
    }

    /** 骰桌区域提示：骰型不支持或数量过多时，用文字替代骰子画面，结果仍在下方展示。 */
    showStageNotice(text: string): void {
        this.stage.innerHTML = "";
        const notice = document.createElement("div");
        notice.className = "dice-stage-notice";
        notice.textContent = text;
        this.stage.appendChild(notice);
    }

    setStatus(text: string, className = ""): void {
        this.statusElement.textContent = text;
        this.statusElement.className = `dice-inline-status ${className}`.trim();
    }

    showRerollControls(reroll: DiceRerollState | null | undefined, onReroll: () => void, onConfirm: () => void): void {
        this.actionElement.innerHTML = "";
        if (!reroll || !reroll.enabled) {
            // 重投关闭时不显示重投/确定按钮，动画结束后保留骰桌并自动完成。
            return;
        }
        const rerollButton = document.createElement("button");
        rerollButton.type = "button";
        rerollButton.className = "btn btn-sm btn-outline-light dice-inline-reroll";
        const remaining = Number(reroll.remaining);
        rerollButton.textContent = remaining < 0 ? "重投（无限）" : `重投（剩余 ${Math.max(0, remaining)}）`;
        rerollButton.disabled = remaining === 0;
        rerollButton.addEventListener("click", () => {
            rerollButton.disabled = true;
            onReroll();
        });
        const confirmButton = document.createElement("button");
        confirmButton.type = "button";
        confirmButton.className = "btn btn-sm btn-primary dice-inline-confirm";
        confirmButton.textContent = "确定";
        confirmButton.addEventListener("click", () => {
            confirmButton.disabled = true;
            onConfirm();
        });
        this.actionElement.appendChild(rerollButton);
        this.actionElement.appendChild(confirmButton);
    }

    /** 结算完成：清空操作区并保留当前骰桌画面与结果文字，不重播动画。 */
    freeze(): void {
        this.root.dataset.state = "finished";
        this.root.classList.add("is-finished");
        this.actionElement.innerHTML = "";
    }

    close(): void {
        this.root.remove();
    }
}

function dicePlanLabel(plan: DicePlan | null | undefined): string {
    if (!plan) return "投掷";
    const check = plan.check || {};
    const parts: string[] = [];
    if (check.name) parts.push(String(check.name));
    if (check.difficulty === "hard") parts.push("困难");
    if (check.difficulty === "extreme") parts.push("极难");
    if (check.target !== null && check.target !== undefined) parts.push(`目标 ${check.target}`);
    if (plan.mode === "standard") parts.push(String(plan.expression || ""));
    return parts.join(" · ") || "投掷";
}

function diceVerdictText(snapshot: DiceResultSnapshot): string {
    const labels: Record<string, string> = {
        critical: "大成功",
        extreme: "极难成功",
        hard: "困难成功",
        regular: "成功",
        failure: "失败",
        fumble: "大失败",
    };
    const level = String(snapshot.success_level || "");
    return labels[level] || (snapshot.success ? "成功" : "失败");
}

function diceRollerName(roller: DiceResultSnapshot["roller"]): string {
    if (!roller) return "";
    if (typeof roller === "string") return roller;
    const name = (roller as { username?: unknown }).username;
    return name === undefined || name === null ? "" : String(name);
}

function diceResultHtml(snapshot: DiceResultSnapshot): string {
    const value = snapshot.mode === "percentile" ? snapshot.roll : snapshot.total;
    const parts: string[] = [`<span class="dice-total">${String(value ?? "")}</span>`];
    if (snapshot.success !== null && snapshot.success !== undefined) {
        const level = String(snapshot.success_level || "");
        const ok = Boolean(snapshot.success);
        const critical = level === "critical" || level === "fumble";
        parts.push(`<span class="dice-verdict ${ok ? "success" : "failure"} ${critical ? "critical" : ""}">${diceVerdictText(snapshot)}</span>`);
    }
    if (snapshot.target !== null && snapshot.target !== undefined) {
        parts.push(`<span class="dice-target">目标 ${snapshot.target}</span>`);
    }
    const rollerName = diceRollerName(snapshot.roller);
    if (rollerName) {
        parts.push(`<span class="dice-roller">${rollerName}</span>`);
    }
    if (snapshot.auto) {
        parts.push(`<span class="dice-auto">等待超时，系统自动投掷</span>`);
    }
    const labels = (snapshot.labels || []).filter(Boolean).map(String);
    if (labels.length > 1 && snapshot.mode !== "percentile") {
        parts.push(`<span class="dice-faces">${labels.join(" / ")}</span>`);
    }
    return parts.join("");
}
