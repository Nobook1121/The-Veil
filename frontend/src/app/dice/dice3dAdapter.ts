/** 3D 渲染适配层：按需加载独立 bundle，失败时交给上层降级。 */
class Dice3DAdapter {
    private container: HTMLElement;
    private plan: DicePlan;
    private faceUrls: Record<string, string>;
    private instance: {
        roll: (snapshot: DiceResultSnapshot) => Promise<void>;
        setStatic: (snapshot: DiceResultSnapshot) => void;
        dispose: () => void;
    } | null = null;
    private static bundlePromise: Promise<boolean> | null = null;

    constructor(container: HTMLElement, plan: DicePlan, faceUrls: Record<string, string>) {
        this.container = container;
        this.plan = plan;
        this.faceUrls = faceUrls || {};
    }

    static loadBundle(): Promise<boolean> {
        if ((window as unknown as { TrpgDice3D?: unknown }).TrpgDice3D) return Promise.resolve(true);
        if (Dice3DAdapter.bundlePromise) return Dice3DAdapter.bundlePromise;
        Dice3DAdapter.bundlePromise = new Promise((resolve) => {
            const script = document.createElement("script");
            script.src = "/js/dice/dice3d.js";
            script.async = true;
            script.onload = () => resolve(Boolean((window as unknown as { TrpgDice3D?: unknown }).TrpgDice3D));
            script.onerror = () => resolve(false);
            document.head.appendChild(script);
        });
        return Dice3DAdapter.bundlePromise;
    }

    private ensureInstance(): void {
        if (this.instance) return;
        const factory = (window as unknown as {
            TrpgDice3D?: {
                create: (
                    container: HTMLElement,
                    options: Record<string, unknown>,
                ) => { roll: (snapshot: DiceResultSnapshot) => Promise<void>; setStatic: (snapshot: DiceResultSnapshot) => void; dispose: () => void };
            };
        }).TrpgDice3D;
        if (!factory) throw new Error("dice3d_bundle_missing");
        const colors = diceResolvedColors(diceLoadAppearance());
        this.instance = factory.create(this.container, {
            plan: this.plan,
            faceUrls: this.faceUrls,
            colors,
        });
    }

    async play(snapshot: DiceResultSnapshot): Promise<void> {
        this.ensureInstance();
        await this.instance!.roll(snapshot);
    }

    static(snapshot: DiceResultSnapshot): void {
        this.ensureInstance();
        this.instance!.setStatic(snapshot);
    }

    dispose(): void {
        try {
            this.instance?.dispose();
        } catch (error) {
            console.warn("销毁 3D 骰子失败", error);
        }
        this.instance = null;
    }
}
