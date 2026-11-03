/**
 * 「深渊帷幕」主题 · 触手层
 *
 * 目标是让触手“读得出是活体”，而不是一根上色的管子：
 * - 轮廓：整根沿幂函数连续收细，根部饱满、末端自然收成细尖，
 *   全程没有“前段不变、末段突然收尖”的折点；边缘叠加低频起伏，避免直线。
 * - 质感：不画吸盘。改由六道不规则折痕、皮下色斑、纵向肌理、
 *   连续羽化的腹面浅带与湿膜反光共同承担皮肤的观感。
 * - 光照：迎光面的高光峰值放在腕体约 3/5 处而不是轮廓边缘，
 *   背光侧压三层暗面，读起来是圆柱体积而非金属高光。
 * - 构图：宽屏十三根、窄屏三根。锚点、粗细、长度、朝向全部带随机抖动，
 *   左右不做镜像；相邻肢体允许少量穿插，但正中央始终留一口气，
 *   因此看起来是“一群腕足从巢穴里探出来”，而不是一对对称的触手。
 * - 动态：振幅与速度都调低，只保留极缓的抽动，避免“乱动”。
 */
namespace AbyssTheme {
    type Side = "bottom" | "top" | "left" | "right";

    interface Tentacle {
        side: Side;
        /** 沿边缘的锚点位置（0 → 1）。 */
        anchor: number;
        /** 长度相对视口短边的比例。 */
        lengthRatio: number;
        /** 根部半宽相对视口短边的比例。 */
        widthRatio: number;
        /** 整体倾斜方向与幅度，-1 → 1。 */
        lean: number;
        /** 尖端钩曲量：让末端卷起，像卷曲的活体触手。 */
        curl: number;
        /** 中段膨起程度，制造粗细不均的生物感。 */
        bulge: number;
        /** 波动幅度（相对长度）。 */
        amplitude: number;
        phase: number;
        speed: number;
        /** 视差强度，越大越靠近镜头、移动越明显。 */
        depth: number;
        /** 体色倾向：0 肉粉 / 1 黄铜 / 2 瘀紫。 */
        tone: number;
        xs: number[];
        ys: number[];
        nx: number[];
        ny: number[];
        /** 每段半宽缓存：shape() 每帧算一次，后续所有绘制只读数组。 */
        hw: number[];
        /**
         * 整根触手的迎光侧（1 / -1）。
         *
         * 逐段用法线点积判断受光面时，法线在弯曲处翻向会让明暗带瞬间跳到
         * 另一侧，观感上就像肢体被切成一段一段。整根统一取平均方向后，
         * 亮带、暗带、湿膜反光与纵纹都贴在同一条侧线上，读起来是一整根。
         */
        lightSign: number;
    }

    const SEGMENTS = 36;
    const TAU = Math.PI * 2;
    const WIDE_BREAKPOINT = 900;

    /**
     * 触手大小的渲染保护区间。
     *
     * 「触手大小」只缩放根部半宽（widthRatio），不改长度、摆动与密度。
     * 参数本身限制在 0.7 → 1.3，这里再对最终比例做一次钳制：
     * - 下限 0.048 ≈ 短边 1080px 时根部直径约 52px，细但不会细到看不见；
     * - 上限 0.175 ≈ 短边 1080px 时根部直径约 189px，配合最多 9 根底部触手
     *   仍能把画面中央的法阵与聊天区留出来，不会糊成一团。
     */
    const MIN_WIDTH_RATIO = 0.048;
    const MAX_WIDTH_RATIO = 0.175;

    /**
     * 高度基准（相对视口短边）。
     *
     * 方差为 0 时，同位置类型的触手严格取同一个基准高度，因此底部一排
     * 会读作平齐；随机性只通过 tentacleHeightVariance 叠加在高度方向上，
     * 不再由其他字段（宽度、摆动相位、seed 分支）间接扰动高度。
     */
    const BASE_HEIGHT = {
        bottomInner: 0.57,
        bottomOuter: 0.62,
        sideTall: 0.76,
        sideShort: 0.51,
        sideMidShort: 0.49,
        sideMidTall: 0.78,
        topTall: 0.32,
        topMid: 0.30,
        topShort: 0.28,
    };

    /** 面板改参数时通过事件刷新缓存，避免每根触手、每帧都重建设置对象。 */
    let cachedFx: AbyssTheme.FxSettings | null = null;
    function fxSettings(): AbyssTheme.FxSettings {
        if (cachedFx === null) {
            cachedFx = AbyssFx.settings();
        }
        return cachedFx;
    }
    function syncFxSettings(): void {
        cachedFx = AbyssFx.settings();
    }
    window.addEventListener("theme-settings-change", syncFxSettings);
    window.addEventListener("abyss-fx-settings", syncFxSettings);

    /** 统一光照方向（左上方），与法阵、光柱、眼睛同源。 */
    const LIGHT_X = -0.55;
    const LIGHT_Y = -0.84;

    /** 深渊肉体的明暗阶梯：从几乎全黑的剪影到迎光处的骨白。 */
    const VOID: Rgb = { r: 5, g: 4, b: 8 };
    const BODY_DARK: Rgb = { r: 26, g: 18, b: 24 };
    const BODY_BASE: Rgb = { r: 92, g: 70, b: 74 };
    const BODY_MID: Rgb = { r: 120, g: 90, b: 92 };
    const BODY_WARM: Rgb = { r: 172, g: 132, b: 122 };
    const BODY_TIP: Rgb = { r: 224, g: 198, b: 176 };
    const BODY_LIT: Rgb = { r: 244, g: 228, b: 208 };

    // 迎光面的五层亮带：偏移、宽度倍数、增益。颜色与透明度沿根部→尖端
    // 预先烘焙进渐变，整条亮带一次填充完成，不再逐块变宽变亮。
    const LIGHT_BAND_DEFS: number[][] = [
        // [偏移, 宽度倍数, 增益]
        [0.16, 2.35, 0.085],
        [0.34, 1.95, 0.12],
        [0.5, 1.55, 0.145],
        [0.64, 1.1, 0.15],
        [0.76, 0.62, 0.115],
    ];
    const LIGHT_STOP_POSITIONS = [0, 0.36, 0.5, 0.72, 0.86, 1];

    interface GradientStop {
        at: number;
        color: string;
    }

    interface LightBand {
        offset: number;
        widthFactor: number;
        stops: GradientStop[];
    }

    function lightTint(s: number): Rgb {
        if (s < 0.5) {
            return BODY_MID;
        }
        if (s < 0.8) {
            return BODY_WARM;
        }
        return BODY_TIP;
    }

    const LIGHT_BANDS: LightBand[] = LIGHT_BAND_DEFS.map((def) => {
        const gain = def[2] ?? 0;
        // 0.48 是原实现沿肢体平均的迎光系数，把“逐块明暗抖动”收敛为整条渐变。
        const stops = LIGHT_STOP_POSITIONS.map((at) => ({
            at,
            color: rgba(lightTint(at), (0.012 + Math.pow(at, 1.25) * gain) * 0.48),
        }));
        return { offset: def[0] ?? 0, widthFactor: def[1] ?? 1, stops };
    });

    /** 每帧复用的一套相对透明度剖面（真实强度由 globalAlpha 统一缩放）。 */
    const GRADIENT_STOP_POSITIONS = [0, 0.4, 0.7, 1];

    function buildStops(tint: Rgb, profile: (s: number) => number): GradientStop[] {
        return GRADIENT_STOP_POSITIONS.map((at) => ({
            at,
            color: rgba(tint, clamp(profile(at), 0, 1)),
        }));
    }

    const WET_STOPS = buildStops(BODY_LIT, (s) => 0.11 * Math.pow(s, 1.6));
    const LANE_STOPS = buildStops(BODY_MID, (s) => 0.05 * (1 - s * 0.55));
    const RIM_STOPS: GradientStop[][] = [BODY_TIP, BRASS, PLUM].map((tint) =>
        buildStops(tint, (s) => 0.008 + 0.075 * Math.pow(s, 1.6)),
    );
    const LANES = [0.34, 0.06, -0.24];

    let tentacles: Tentacle[] = [];
    let viewWidth = 0;
    let viewHeight = 0;

    /** 皮下色斑贴图：一块边缘极柔的椭圆斑，用于打破平涂的肉体。 */
    const blobCache = new Map<string, HTMLCanvasElement>();

    function buildBlobSprite(key: string, r: number, g: number, b: number): HTMLCanvasElement {
        const cached = blobCache.get(key);
        if (cached !== undefined) {
            return cached;
        }
        const size = 128;
        const sprite = document.createElement("canvas");
        sprite.width = size;
        sprite.height = size;
        const ctx = sprite.getContext("2d");
        if (ctx) {
            const c = size / 2;
            const grad = ctx.createRadialGradient(c, c, 0, c, c, c);
            grad.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.9)`);
            grad.addColorStop(0.46, `rgba(${r}, ${g}, ${b}, 0.52)`);
            grad.addColorStop(0.76, `rgba(${r}, ${g}, ${b}, 0.14)`);
            grad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, size, size);
        }
        blobCache.set(key, sprite);
        return sprite;
    }

    function makeTentacle(side: Side, anchor: number, seed: number, rand: (min: number, max: number) => number): Tentacle {
        const short = side !== "bottom";
        return {
            side,
            anchor,
            lengthRatio: short ? rand(0.28, 0.38) : rand(0.56, 0.68),
            widthRatio: short ? rand(0.078, 0.106) : rand(0.098, 0.126),
            lean: (seed % 2 === 0 ? -1 : 1) * rand(0.10, 0.26),
            curl: (seed % 2 === 0 ? -1 : 1) * rand(0.10, 0.34),
            bulge: rand(0.12, 0.26),
            amplitude: rand(0.05, 0.095),
            phase: rand(0, TAU),
            speed: rand(0.18, 0.32),
            depth: rand(0.35, 1),
            tone: seed % 6 === 1 ? 1 : seed % 8 === 3 ? 2 : 0,
            xs: new Array<number>(SEGMENTS + 1).fill(0),
            ys: new Array<number>(SEGMENTS + 1).fill(0),
            nx: new Array<number>(SEGMENTS + 1).fill(0),
            ny: new Array<number>(SEGMENTS + 1).fill(0),
            hw: new Array<number>(SEGMENTS + 1).fill(0),
            lightSign: 1,
        };
    }

    function rebuild(viewport: Viewport): void {
        viewWidth = viewport.width;
        viewHeight = viewport.height;
        const wide = viewport.width >= WIDE_BREAKPOINT;
        const list: Tentacle[] = [];
        // 会话种子：每次 rebuild 都从同一颗种子重新开始，窗口缩放不会重排构图。
        const nextRandom = createSessionRandom(0x7e11);
        const rand = (min: number, max: number): number => min + nextRandom() * (max - min);

        // 造肢：粗细、长度、远近都随机，朝向以基础方向为主、再叠一点抖动。
        // 朝向不做全随机，否则会长出横贯整幅画面的「长矛」，把法阵切成两半。
        const arm = (
            side: Side,
            anchor: number,
            width: number,
            length: number,
            depth: number,
            lean: number,
            curl: number,
        ): Tentacle => {
            const t = makeTentacle(side, anchor, Math.floor(rand(0, 997)), rand);
            const fx = fxSettings();
            // 「触手大小」只作用于粗细/体量；widthRatio 是根部半宽比例，长度不受影响。
            t.widthRatio = clamp(
                width * clamp(fx.tentacleSize, 0.7, 1.3),
                MIN_WIDTH_RATIO,
                MAX_WIDTH_RATIO,
            );
            // 高度随机性：基准高度 + 固定种子驱动的高度扰动。
            // 每次都消耗一次随机数，保证切换方差时其他维度的随机序列不被打乱；
            // 方差为 0 时扰动严格为 0，同类触手完全平齐。
            const heightNoise = rand(-1, 1) * clamp(fx.tentacleHeightVariance, 0, 0.5);
            t.lengthRatio = clamp(length * fx.tentacleHeight * (1 + heightNoise), 0.14, 0.98);
            t.depth = depth;
            t.lean = lean + rand(-0.07, 0.07);
            t.curl = curl + rand(-0.09, 0.09);
            // 侧肢更长，需要更明显的行进波，否则读起来还是一根直杆。
            t.amplitude = side === "bottom" ? rand(0.05, 0.09) : rand(0.06, 0.11);
            return t;
        };

        // 底部触手数量由密度决定：默认 6 根（窄屏 3 根），密度越高越密。
        // 不再是固定数组——真实巢穴里也不会每次都伸出同样数量的腕足。
        const density = fxSettings().tentacleDensity;
        const baseCount = wide ? 6 : 3;
        const bottomCount = clamp(Math.round(baseCount * density), wide ? 3 : 2, wide ? 9 : 5);
        const bottomAnchors = Array.from(
            { length: bottomCount },
            (_value, index) => 0.08 + (0.84 * index) / Math.max(1, bottomCount - 1),
        );
        for (const base of bottomAnchors) {
            const anchor = clamp(base + rand(-0.025, 0.025), 0.04, 0.96);
            const inner = Math.abs(anchor - 0.5) < 0.16;
            const outward = anchor < 0.5 ? -1 : 1;
            // 钩曲方向独立随机：否则一整排触手会朝同一个方向斜过去，像一把叉子。
            const curl = rand(-0.3, 0.3);
            const t = arm(
                "bottom",
                anchor,
                inner ? rand(0.068, 0.096) : rand(0.076, 0.132),
                inner ? BASE_HEIGHT.bottomInner : BASE_HEIGHT.bottomOuter,
                inner ? rand(0.32, 0.5) : rand(0.4, 1),
                inner ? outward * rand(0.12, 0.2) : outward * rand(0.04, 0.14),
                curl,
            );
            list.push(t);
        }

        if (wide && density >= 0.6) {
            // 侧肢左右各两根：像从画面外侧「探」进来，方向带一点朝上的坡，
            // 让它们在法阵外围浅浅交叉，而不是从圆心上方横切过去。
            list.push(arm("left", 0.44, 0.088, BASE_HEIGHT.sideTall, rand(0.78, 1), -0.1, 0.2));
            list.push(arm("left", 0.68, 0.06, BASE_HEIGHT.sideShort, rand(0.45, 0.7), 0.12, -0.16));
            list.push(arm("right", 0.34, 0.058, BASE_HEIGHT.sideMidShort, rand(0.4, 0.62), -0.14, 0.18));
            list.push(arm("right", 0.58, 0.084, BASE_HEIGHT.sideMidTall, rand(0.82, 1), 0.1, -0.2));
            // 顶部三根：垂下的长短不一、位置不对齐。它们必须有足够的宽度与钩曲，
            // 否则从暗处垂下来只会是一根尖刺——现在根部会先露出一段厚实的腕体。
            list.push(arm("top", 0.16, 0.072, BASE_HEIGHT.topTall, 0.72, -0.08, 0.3));
            list.push(arm("top", 0.5, rand(0.058, 0.07), BASE_HEIGHT.topMid, 0.8, 0.12, -0.34));
            list.push(arm("top", 0.82, 0.062, BASE_HEIGHT.topShort, 0.66, -0.07, 0.26));
        }
        // 视差小的先画：远的更暗、近的更亮，纵深由绘制顺序自然形成。
        list.sort((a, b) => a.depth - b.depth);
        tentacles = list;
    }

    /** 逐帧计算触手中线与法线。 */
    function shape(t: Tentacle, scene: Scene): void {
        const min = Math.min(viewWidth, viewHeight);
        const length = t.lengthRatio * min * tide(scene.time);
        const amp = t.amplitude * length;
        const clock = scene.time * t.speed * fxSettings().swaySpeed + t.phase;
        const parX = scene.pointer.x * 13 * t.depth;
        const parY = scene.pointer.y * 8 * t.depth;

        for (let i = 0; i <= SEGMENTS; i += 1) {
            const s = i / SEGMENTS;
            // 行进波：相位沿肢体从根到尖依次延迟，动作像活物抽动而不是匀速摇摆。
            const travel = clock - s * 2.2;
            const swell = Math.pow(s, 1.6);
            const wave = (Math.sin(travel) + Math.sin(travel * 2.05 + 0.9) * 0.26) * amp * swell;
            const curl = t.curl * length * Math.pow(s, 2.6);
            const lag = Math.sin(clock * 0.5 - s * 1.2) * amp * 0.1 * s;
            // 整体弓形：中段向外鼓出一道弧，避免肢体是一条斜直线。
            const bow = Math.sin(s * Math.PI) * t.lean * length * 0.3;
            let x: number;
            let y: number;
            if (t.side === "bottom") {
                x = t.anchor * viewWidth + t.lean * length * s + bow + curl + wave + lag + parX * s;
                y = viewHeight + min * 0.1 - length * s + parY * s;
            } else if (t.side === "top") {
                x = t.anchor * viewWidth + t.lean * length * s + bow + curl + wave + lag + parX * s;
                y = -min * 0.1 + length * s + parY * s;
            } else if (t.side === "left") {
                x = -min * 0.06 + length * s + parX * s;
                y = t.anchor * viewHeight + t.lean * length * s + bow + curl + wave + lag + parY * s;
            } else {
                x = viewWidth + min * 0.06 - length * s + parX * s;
                y = t.anchor * viewHeight + t.lean * length * s + bow + curl + wave + lag + parY * s;
            }
            t.xs[i] = x;
            t.ys[i] = y;
        }

        for (let i = 0; i <= SEGMENTS; i += 1) {
            const prev = i > 0 ? i - 1 : 0;
            const next = i < SEGMENTS ? i + 1 : SEGMENTS;
            const tx = (t.xs[next] ?? 0) - (t.xs[prev] ?? 0);
            const ty = (t.ys[next] ?? 0) - (t.ys[prev] ?? 0);
            const len = Math.hypot(tx, ty) || 1;
            t.nx[i] = -ty / len;
            t.ny[i] = tx / len;
        }

        // 整根触手的迎光侧只取一次：逐段判断会让明暗带在法线翻向处横跳，
        // 视觉上就表现为肢体被切成几截。
        let shadeSum = 0;
        for (let i = 0; i <= SEGMENTS; i += 1) {
            shadeSum += shadeAt(t, i);
        }
        t.lightSign = shadeSum >= 0 ? 1 : -1;

        // 半宽剖面每帧只算一次并缓存：旧实现在描边/填充循环里反复调用
        // halfWidthAt（含 Math.pow / Math.exp / 多次 Math.sin），单帧累计上千次，
        // 是触手层最主要的 CPU 热点。
        for (let i = 0; i <= SEGMENTS; i += 1) {
            t.hw[i] = halfWidthAt(t, i / SEGMENTS, min, scene.time);
        }
    }

    /** 读取缓存的半宽（允许非整数段号，按线性插值）。 */
    function halfWidthCached(t: Tentacle, s: number): number {
        const pos = clamp(s, 0, 1) * SEGMENTS;
        const index = Math.floor(pos);
        const next = index < SEGMENTS ? index + 1 : SEGMENTS;
        const ratio = pos - index;
        return (t.hw[index] ?? 0) * (1 - ratio) + (t.hw[next] ?? 0) * ratio;
    }

    /** 该段法线与统一光照方向的点积：正值表示迎光，负值表示背光。 */
    function shadeAt(t: Tentacle, index: number): number {
        return (t.nx[index] ?? 0) * LIGHT_X + (t.ny[index] ?? 0) * LIGHT_Y;
    }

    /**
     * 某一段的半宽。
     *
     * 旧剖面在前 2/3 几乎保持同一宽度（只掉 8%），最后三分之一再用 smoothstep
     * 突然收到零，两段曲率在分界处对不上，看上去就是“一根圆柱接一个圆锥”。
     *
     * 现在用 (1 - s^2.2)^0.85 这一条连续曲线：根部前约三成仍保持饱满的体积，
     * 随后收细量从 0 开始平滑加速，横跨整根触手一直过渡到尖端，最后在 s=1
     * 收敛到 0。它没有分段、没有折点，也就不会出现“圆柱 + 圆锥”的拼缝。
     */
    function halfWidthAt(t: Tentacle, s: number, min: number, time: number): number {
        const base = t.widthRatio * min * 0.5;
        const taper = Math.pow(1 - Math.pow(s, 2.2), 0.85);
        // 中段轻微膨起，让腕体有肉感；振幅压到 0.7，避免在收细段鼓出一个节。
        const swell = 1 + Math.sin(s * Math.PI * 1.3 + t.phase * 0.2) * t.bulge * 0.7;
        // 腕足膨结：靠近 s≈0.68 处极轻的加粗，只作为皮肤起伏，不打断收尖曲线。
        const club = 1 + 0.06 * Math.exp(-Math.pow((s - 0.68) / 0.18, 2));
        // 有机起伏：低频为主，高频只留一点点，避免轮廓变成锯齿状的“鳞边”。
        const ripple = 1 + 0.05 * Math.sin(s * 6.8 + t.phase * 1.7) + 0.02 * Math.sin(s * 12.6 + t.phase * 0.9);
        return Math.max(0, base * taper * swell * club * ripple * tide(time));
    }

    /** 沿中线和法线描出肢体轮廓路径。 */
    function traceOutline(ctx: CanvasRenderingContext2D, t: Tentacle): void {
        ctx.beginPath();
        for (let i = 0; i <= SEGMENTS; i += 1) {
            const half = t.hw[i] ?? 0;
            const x = (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * half;
            const y = (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * half;
            if (i === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
        // 末端收口：半宽已经平滑收到接近零，这里只补一个极小的圆点，
        // 既让多边形闭合、又不会冒出一截固定半径的圆头（那正是“圆柱端面”的来源）。
        const tipHalf = Math.max(0.5, t.hw[SEGMENTS] ?? 0);
        const tipX = t.xs[SEGMENTS] ?? 0;
        const tipY = t.ys[SEGMENTS] ?? 0;
        const tx = (t.xs[SEGMENTS] ?? 0) - (t.xs[SEGMENTS - 1] ?? 0);
        const ty = (t.ys[SEGMENTS] ?? 0) - (t.ys[SEGMENTS - 1] ?? 0);
        const tLen = Math.hypot(tx, ty) || 1;
        if (tipHalf > 1.5) {
            for (let k = 1; k < 6; k += 1) {
                const ang = (Math.PI * k) / 6;
                ctx.lineTo(
                    tipX + (t.nx[SEGMENTS] ?? 0) * tipHalf * Math.cos(ang) + (tx / tLen) * tipHalf * Math.sin(ang),
                    tipY + (t.ny[SEGMENTS] ?? 0) * tipHalf * Math.cos(ang) + (ty / tLen) * tipHalf * Math.sin(ang),
                );
            }
        } else {
            // 细尖：沿切线补不到 1px 的一小段，收口是一条锋利的尖，而不是端面。
            ctx.lineTo(tipX + (tx / tLen) * 0.6, tipY + (ty / tLen) * 0.6);
        }
        for (let i = SEGMENTS; i >= 0; i -= 1) {
            const half = t.hw[i] ?? 0;
            ctx.lineTo((t.xs[i] ?? 0) - (t.nx[i] ?? 0) * half, (t.ys[i] ?? 0) - (t.ny[i] ?? 0) * half);
        }
        ctx.closePath();
    }

    /**
     * 沿法线偏移填充一条连续的明暗带。
     *
     * 旧实现逐段 stroke()：相邻线段的圆端互相叠压，在背光面留下一串等距的
     * 淡黑色椭圆；按块描边虽然去掉了椭圆，却让块的线宽/颜色出现阶跃，
     * 触手因此看起来「一段一段」。这里改为一次性构造上缘 + 下缘多边形，
     * 宽度随腕体半宽连续变化，一根触手上的明暗过渡是整条连续的。
     */
    function fillBand(
        ctx: CanvasRenderingContext2D,
        t: Tentacle,
        direction: number,
        offset: number,
        widthFactor: number,
    ): void {
        ctx.beginPath();
        for (let i = 0; i <= SEGMENTS; i += 1) {
            const half = t.hw[i] ?? 0;
            // 只取受光/背光的“程度”，侧别由整根触手统一决定，带子不会中途换边。
            const off = half * offset * Math.abs(shadeAt(t, i)) * t.lightSign * direction;
            const thick = half * widthFactor * 0.5;
            const px = (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * (off + thick);
            const py = (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * (off + thick);
            if (i === 0) {
                ctx.moveTo(px, py);
            } else {
                ctx.lineTo(px, py);
            }
        }
        for (let i = SEGMENTS; i >= 0; i -= 1) {
            const half = t.hw[i] ?? 0;
            const off = half * offset * Math.abs(shadeAt(t, i)) * t.lightSign * direction;
            const thick = half * widthFactor * 0.5;
            ctx.lineTo(
                (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * (off - thick),
                (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * (off - thick),
            );
        }
        ctx.closePath();
        ctx.fill();
    }

    /** 以根部 → 尖端为轴的线性渐变，用于沿肢体自然衰减的描边与填充。 */
    function bandGradient(
        ctx: CanvasRenderingContext2D,
        t: Tentacle,
        stops: GradientStop[],
    ): CanvasGradient {
        const gradient = ctx.createLinearGradient(
            t.xs[0] ?? 0,
            t.ys[0] ?? 0,
            t.xs[SEGMENTS] ?? 0,
            t.ys[SEGMENTS] ?? 0,
        );
        for (const stop of stops) {
            gradient.addColorStop(stop.at, stop.color);
        }
        return gradient;
    }

    /**
     * 沿整根触手的迎光侧描一条连续折线。
     *
     * 湿膜反光与轮廓高光原本按段描（单根触手合计约 60 次 stroke），
     * 中途还会因为逐段侧别翻转而断开。这里整根只描一次、侧别固定，
     * 因此线条从头到尾不断，绘制调用也降到每根一次。
     */
    function strokeSide(
        ctx: CanvasRenderingContext2D,
        t: Tentacle,
        startIndex: number,
        endIndex: number,
        offsetFactor: number,
        widthFactor: number,
    ): void {
        if (endIndex < startIndex) {
            return;
        }
        let halfSum = 0;
        let count = 0;
        ctx.beginPath();
        for (let i = startIndex; i <= endIndex; i += 1) {
            const half = t.hw[i] ?? 0;
            const off = half * offsetFactor * t.lightSign;
            const px = (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * off;
            const py = (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * off;
            if (i === startIndex) {
                ctx.moveTo(px, py);
            } else {
                ctx.lineTo(px, py);
            }
            halfSum += half;
            count += 1;
        }
        // widthFactor 传 0 时取最小 1px，用于轮廓高光。
        ctx.lineWidth = Math.max(1, (halfSum / Math.max(1, count)) * widthFactor);
        ctx.stroke();
    }

    /**
     * 本体绘制：平涂剪影 → 裁剪 → 内缘阴影 → 背光侧暗面 → 迎光侧柔光 → 皮下散射。
     * 所有光影都逐段沿法线偏移，弯曲的肢体才不会落进单条线性渐变的端点色。
     */
    function drawBody(ctx: CanvasRenderingContext2D, t: Tentacle, min: number, scene: Scene): void {
        const presence = scene.intensity * (0.5 + 0.5 * t.depth);
        const detail = 0.5 + 0.5 * t.depth;

        // 1) 平涂剪影 + 2) 内缘阴影：轮廓路径只描一次。
        //    旧实现三次 traceOutline（每次数十次 halfWidthAt 计算），现在描一次后
        //    依次 fill / stroke / clip，半宽全部取自 shape() 的缓存数组。
        traceOutline(ctx, t);
        ctx.save();
        ctx.globalAlpha = presence;
        ctx.fillStyle = rgba(BODY_BASE, 0.96);
        ctx.fill();

        // 先裁剪再描内缘阴影：描边只向内收一圈，不会在轮廓外留下一圈暗边。
        ctx.clip();
        ctx.globalAlpha = 0.5 * presence;
        ctx.lineWidth = Math.max(2.4, t.widthRatio * min * 0.13);
        ctx.strokeStyle = rgba(VOID, 1);
        ctx.stroke();

        ctx.lineCap = "round";
        ctx.lineJoin = "round";

        // 3) 背光侧：三层由外向内收窄的暗面，压出明暗交界线。
        //    用整条多边形填充（fillBand）替代逐段描边：带的宽度随腕体半宽
        //    连续变化，不会在块与块之间出现线宽阶跃，触手不再“一段一段”。
        ctx.globalCompositeOperation = "source-over";
        ctx.fillStyle = rgba(VOID, 1);
        ctx.globalAlpha = clamp(0.4 * presence, 0, 1);
        fillBand(ctx, t, -1, 0.5, 1.55);
        ctx.fillStyle = rgba(BODY_DARK, 1);
        ctx.globalAlpha = clamp(0.34 * presence, 0, 1);
        fillBand(ctx, t, -1, 0.72, 1.0);
        ctx.fillStyle = rgba(VOID, 1);
        ctx.globalAlpha = clamp(0.26 * presence, 0, 1);
        fillBand(ctx, t, -1, 0.9, 0.44);

        // 4) 迎光侧：把高光峰值从轮廓边缘移到腕体约 3/5 处，
        //    于是亮部是一条有宽度的“圆柱受光面”，而不是贴着边的一线刀锋反光。
        //    五层亮带各自一次连续填充，颜色与明暗沿长度渐变，不再逐块跳变。
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = clamp(presence * detail, 0, 1);
        for (const band of LIGHT_BANDS) {
            ctx.fillStyle = bandGradient(ctx, t, band.stops);
            fillBand(ctx, t, 1, band.offset, band.widthFactor);
        }

        // 5) 皮下散射：背光边缘透出一点暗红，肉体因此有半透明感。
        ctx.globalCompositeOperation = "lighter";
        ctx.fillStyle = "rgba(132, 52, 58, 1)";
        ctx.globalAlpha = clamp(0.18 * presence * detail * 0.9, 0, 1);
        fillBand(ctx, t, -1, 0.9, 0.36);

        // 6) 迎光侧最外缘的湿膜反光：只在中后段出现，很淡，提示表面是湿的。
        //    按光照侧别合并成连续折线，透明度由整根触手的渐变承担。
        ctx.globalAlpha = clamp(presence * detail, 0, 1);
        ctx.strokeStyle = bandGradient(ctx, t, WET_STOPS);
        strokeSide(ctx, t, Math.round(SEGMENTS * 0.24), SEGMENTS - 1, 0.92, 0.06);
        // 7) 皮下色斑：几块边缘极柔的暗斑，打破平涂的色块，
        //    让皮肤看起来是长出来的组织，而不是一条上了色带形状的描边。
        const darkBlob = buildBlobSprite("dark", 10, 6, 12);
        const warmBlob = buildBlobSprite("warm", 150, 98, 88);
        ctx.globalCompositeOperation = "source-over";
        for (let i = 0; i < 13; i += 1) {
            const s = 0.06 + i * 0.062;
            const index = clamp(Math.round(s * SEGMENTS), 0, SEGMENTS);
            const half = halfWidthCached(t, s);
            if (half < 5) {
                continue;
            }
            const nx = t.nx[index] ?? 0;
            const ny = t.ny[index] ?? 0;
            const side = Math.sin(i * 5.3 + t.phase * 2.1) * 0.58;
            const cx = (t.xs[index] ?? 0) + nx * half * side;
            const cy = (t.ys[index] ?? 0) + ny * half * side;
            const radius = half * (1.05 + 0.75 * Math.abs(Math.cos(i * 2.7 + t.phase)));
            ctx.globalAlpha = clamp(0.18 * presence * detail, 0, 1);
            ctx.drawImage(darkBlob, cx - radius, cy - radius * 0.85, radius * 2, radius * 1.7);
        }
        // 迎光侧几块略暖的斑：像皮肤下的血色素，只在受光面出现。
        for (let i = 0; i < 5; i += 1) {
            const s = 0.2 + i * 0.14;
            const index = clamp(Math.round(s * SEGMENTS), 0, SEGMENTS);
            const half = halfWidthCached(t, s);
            const nx = t.nx[index] ?? 0;
            const ny = t.ny[index] ?? 0;
            const shade = nx * LIGHT_X + ny * LIGHT_Y;
            if (half < 5 || Math.abs(shade) < 0.2) {
                continue;
            }
            const cx = (t.xs[index] ?? 0) + nx * half * 0.6 * t.lightSign;
            const cy = (t.ys[index] ?? 0) + ny * half * 0.6 * t.lightSign;
            const radius = half * (0.7 + 0.5 * Math.abs(Math.sin(i * 3.1)));
            ctx.globalAlpha = clamp(0.1 * presence * detail, 0, 1);
            ctx.drawImage(warmBlob, cx - radius, cy - radius * 0.8, radius * 2, radius * 1.6);
        }
        // 8) 纵向肌理：三条纵纹各自合并为一条连续折线（旧实现每 2 段描一次，
        //    单根触手要 stroke 四十多次），透明度沿尖端方向渐隐。
        ctx.globalCompositeOperation = "lighter";
        ctx.globalAlpha = clamp(presence * detail, 0, 1);
        ctx.strokeStyle = bandGradient(ctx, t, LANE_STOPS);
        const laneStart = Math.round(SEGMENTS * 0.12);
        const laneEnd = SEGMENTS - 2;
        let laneHalfSum = 0;
        for (let i = laneStart; i <= laneEnd; i += 1) {
            laneHalfSum += t.hw[i] ?? 0;
        }
        const laneWidth = Math.max(1, (laneHalfSum / Math.max(1, laneEnd - laneStart + 1)) * 0.1);
        ctx.lineWidth = laneWidth;
        for (const lane of LANES) {
            ctx.beginPath();
            for (let i = laneStart; i <= laneEnd; i += 1) {
                const half = t.hw[i] ?? 0;
                // 同样固定迎光侧：纵纹不会在弯曲处跳到另一侧。
                const off = half * lane * Math.abs(shadeAt(t, i)) * t.lightSign;
                const px = (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * off;
                const py = (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * off;
                if (i === laneStart) {
                    ctx.moveTo(px, py);
                } else {
                    ctx.lineTo(px, py);
                }
            }
            ctx.stroke();
        }
        ctx.restore();
    }

    /**
     * 体节褶皱：几道间距不等、深浅不一的横向折痕。
     *
     * 等距的横纹会让细腕足变成一截「软管」；只留 5 道不规则的浅折，
     * 再在迎光侧补一线脊光，读起来才像活体表面的肌肉折叠。
     */
    function drawFolds(ctx: CanvasRenderingContext2D, t: Tentacle, scene: Scene): void {
        const strength = scene.intensity * (0.45 + 0.55 * t.depth);
        const slots = [0.1, 0.24, 0.38, 0.52, 0.66, 0.78];
        ctx.save();
        ctx.lineCap = "round";
        for (let n = 0; n < slots.length; n += 1) {
            const slot = slots[n] ?? 0;
            const s = slot + Math.sin(n * 2.4 + t.phase) * 0.018;
            const i = clamp(Math.round(s * SEGMENTS), 0, SEGMENTS);
            const half = halfWidthCached(t, s);
            if (half < 4) {
                continue;
            }
            const x = t.xs[i] ?? 0;
            const y = t.ys[i] ?? 0;
            const nx = t.nx[i] ?? 0;
            const ny = t.ny[i] ?? 0;
            const tx = -ny;
            const ty = nx;
            const bend = half * 0.06;
            const push = half * 0.1 * -t.lightSign;
            const weight = 0.6 + 0.4 * Math.abs(Math.sin(n * 1.9 + t.phase * 0.7));

            // 折痕本体：一道很浅的暗线。
            ctx.globalAlpha = clamp(0.24 * weight * strength, 0, 1);
            ctx.lineWidth = Math.max(1, half * 0.038);
            ctx.strokeStyle = rgba(VOID, 0.9);
            ctx.beginPath();
            ctx.moveTo(x + nx * half * 0.94, y + ny * half * 0.94);
            ctx.quadraticCurveTo(x + tx * bend, y + ty * bend, x - nx * half * 0.94, y - ny * half * 0.94);
            ctx.stroke();

            // 迎光侧的脊光：折痕上方隆起的一线反光。
            ctx.globalAlpha = clamp(0.1 * weight * strength, 0, 1);
            ctx.lineWidth = Math.max(1, half * 0.02);
            ctx.strokeStyle = rgba(BODY_WARM, 0.9);
            ctx.beginPath();
            ctx.moveTo(x + nx * half * 0.86 + nx * push, y + ny * half * 0.86 + ny * push);
            ctx.quadraticCurveTo(
                x + tx * bend + nx * push,
                y + ty * bend + ny * push,
                x - nx * half * 0.86 + nx * push,
                y - ny * half * 0.86 + ny * push,
            );
            ctx.stroke();
        }
        ctx.restore();
    }

    /**
     * 腹面朝向：整根触手只取一次方向，由「沿肢体平均的背光程度」决定。
     *
     * 逐段判断腹面时，法线一旦在弯曲处翻向，浅色腹面就会从中途跳到另一侧，
     * 看起来像散落的斑点。改为整根统一后，腹面始终贴在同一条侧线上。
     */
    function ventralSign(t: Tentacle, start: number, end: number): number {
        let sum = 0;
        let count = 0;
        const from = Math.round(start * SEGMENTS);
        const to = Math.round(end * SEGMENTS);
        for (let i = from; i <= to; i += 1) {
            const nx = t.nx[i] ?? 0;
            const ny = t.ny[i] ?? 0;
            sum += nx * LIGHT_X + ny * LIGHT_Y;
            count += 1;
        }
        const average = count > 0 ? sum / count : 0;
        // 背光侧更暗，正好托住浅色的吸盘；几乎侧对光时也给出一个固定方向。
        return average >= 0 ? -1 : 1;
    }

    /**
     * 腹面浅色带。
     *
     * 细腕足上再排两列亮环吸盘，无论怎么调都会像一串珠子贴在边缘；
     * 这里放弃「画吸盘」，改用一条随根部到尖端渐窄渐淡的浅色腹面，
     * 让肢体有正腹/背两面，表面质感交给褶皱、色斑与湿反光。
     */
    function drawVentralSheen(ctx: CanvasRenderingContext2D, t: Tentacle, scene: Scene): void {
        const start = 0.18;
        const end = 0.78;
        const ventral = ventralSign(t, start, end);
        const from = Math.round(start * SEGMENTS);
        const to = Math.round(end * SEGMENTS);
        const strength = clamp(scene.intensity * (0.4 + 0.6 * t.depth), 0, 1);

        // 三层由宽到窄的填充带。
        // 上一版用圆头端点逐段描边，相邻描边的圆形端头互相叠加，会在腹面留下
        // 一串等距亮点——那正是“吸盘依旧怪怪的”的来源。现在改成一次性填充的
        // 带状多边形：带子连续，宽度在两端渐收，因此没有任何周期性亮点。
        const LAYERS: number[][] = [
            [0.32, 0.03],
            [0.19, 0.042],
            [0.085, 0.04],
        ];
        const centerInset = 0.4;
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        ctx.fillStyle = rgba(BODY_WARM, 0.9);
        for (const layer of LAYERS) {
            const spread = layer[0] ?? 0;
            const alpha = layer[1] ?? 0;
            ctx.globalAlpha = clamp(alpha * strength, 0, 1);
            ctx.beginPath();
            // 上缘：沿腹面前进，偏移量由 centerInset 扩张到 centerInset + spread。
            for (let i = from; i <= to; i += 1) {
                const s = i / SEGMENTS;
                const half = t.hw[i] ?? 0;
                const fade = Math.min(1, (s - start) / 0.14) * Math.min(1, (end - s) / 0.18);
                const off = clamp((centerInset + spread * fade) * ventral, -0.9, 0.9);
                const x = (t.xs[i] ?? 0) + (t.nx[i] ?? 0) * half * off;
                const y = (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * half * off;
                if (i === from) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            }
            // 下缘：反向回来，偏移量收缩到 centerInset - spread。
            for (let i = to; i >= from; i -= 1) {
                const s = i / SEGMENTS;
                const half = t.hw[i] ?? 0;
                const fade = Math.min(1, (s - start) / 0.14) * Math.min(1, (end - s) / 0.18);
                const off = clamp((centerInset - spread * fade) * ventral, -0.9, 0.9);
                ctx.lineTo((t.xs[i] ?? 0) + (t.nx[i] ?? 0) * half * off, (t.ys[i] ?? 0) + (t.ny[i] ?? 0) * half * off);
            }
            ctx.closePath();
            ctx.fill();
        }
        ctx.restore();
    }

    /** 迎光侧轮廓的细高光：从根部几乎不可见，到尖端逐渐亮起。 */
    function drawRimLight(ctx: CanvasRenderingContext2D, t: Tentacle, scene: Scene): void {
        const toneIndex = t.tone === 1 ? 1 : t.tone === 2 ? 2 : 0;
        const stops = RIM_STOPS[toneIndex] ?? RIM_STOPS[0] ?? [];
        ctx.save();
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = bandGradient(ctx, t, stops);
        ctx.globalAlpha = clamp(scene.intensity * (0.5 + 0.5 * t.depth), 0, 1);
        // 宽度固定 1px：widthFactor 传 0，strokeSide 会取 Math.max(1, 0)。
        strokeSide(ctx, t, 3, SEGMENTS, 0.95, 0);
        ctx.restore();
    }

    /** 尖端：平滑收尖后只留一团极淡的荧光，不再画分叉细须。 */
    function drawTip(ctx: CanvasRenderingContext2D, t: Tentacle, min: number, scene: Scene): void {
        const tipX = t.xs[SEGMENTS] ?? 0;
        const tipY = t.ys[SEGMENTS] ?? 0;
        const tone = t.tone === 1 ? BRASS : t.tone === 2 ? PLUM : BODY_TIP;
        const pulse = 0.7 + 0.3 * Math.sin(scene.time * 0.9 + t.phase * 2);
        drawGlow(ctx, tone, tipX, tipY, min * 0.036, 0.1 * pulse * scene.intensity * (0.4 + 0.6 * t.depth));
    }

    function draw(scene: Scene): void {
        if (scene.intensity <= 0.004) {
            return;
        }
        const ctx = scene.ctx;
        const min = Math.min(viewWidth, viewHeight);
        for (const t of tentacles) {
            shape(t, scene);
            drawBody(ctx, t, min, scene);
            drawFolds(ctx, t, scene);
            drawVentralSheen(ctx, t, scene);
            drawRimLight(ctx, t, scene);
            drawTip(ctx, t, min, scene);
        }
    }

    export const Tentacles: CanvasLayerApi = { rebuild, draw };
}
