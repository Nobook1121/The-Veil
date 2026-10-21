/** 3D 渲染适配层：按需加载独立 bundle，失败时交给上层降级。 */
class Dice3DAdapter {
    private container: HTMLElement;
    private plan: DicePlan;
    private faceUrls: Record<string, string>;
    private instance: {
        roll: (snapshot: DiceResultSnapshot, seed?: number | null) => Promise<number[]>;
        setStatic: (snapshot: DiceResultSnapshot) => void;
        showTable: () => void;
        freeze: () => string | null;
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
                ) => { roll: (snapshot: DiceResultSnapshot, seed?: number | null) => Promise<number[]>; setStatic: (snapshot: DiceResultSnapshot) => void; showTable: () => void; freeze: () => string | null; dispose: () => void };
            };
        }).TrpgDice3D;
        if (!factory) throw new Error("dice3d_bundle_missing");
        const appearance = diceLoadAppearance();
        const colors = diceResolvedColors(appearance);
        this.instance = factory.create(this.container, {
            plan: this.plan,
            faceUrls: this.faceUrls,
            tableSkinUrl: diceTableSkinUrl(appearance.tableSkinId),
            colors,
        });
    }

    /** 播放物理投掷；提供 seed 时所有客户端轨迹一致，并返回落定后的骰面。 */
    async play(snapshot: DiceResultSnapshot, seed?: number | null): Promise<number[] | null> {
        this.ensureInstance();
        return this.instance!.roll(snapshot, seed);
    }

    static(snapshot: DiceResultSnapshot): void {
        this.ensureInstance();
        this.instance!.setStatic(snapshot);
    }

    /** 空骰桌预览：等待玩家投掷时就显示完整桌面。 */
    showTable(): void {
        this.ensureInstance();
        this.instance!.showTable();
    }

    /** 冻结为静态图：截图后销毁 3D 实例并释放 WebGL context。 */
    freeze(): void {
        const instance = this.instance;
        if (!instance) return;
        let snapshot: string | null = null;
        try {
            snapshot = instance.freeze();
        } catch (error) {
            console.warn("3D 骰桌截图失败", error);
        }
        this.dispose();
        if (!snapshot) return;
        const image = document.createElement("img");
        image.className = "dice3d-canvas dice3d-static";
        image.alt = "骰子结果";
        image.src = snapshot;
        this.container.appendChild(image);
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
