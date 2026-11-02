/**
 * 「赛博档案」主题 · WebGL 体积氛围层
 *
 * 位于 2D 画布之下，负责“空间本身”：冷色体积雾、竖向数据导管、
 * 细密扫描线、缓慢下移的扫描带、指针位置的冷光、暗角与颗粒。
 * GPU 承担大部分像素成本，主线程只负责一次 drawArrays。
 *
 * 容错策略与深渊主题一致：WebGL 不可用、着色器编译失败或上下文丢失时
 * 静默移除画布，完全回退到 CSS 渐变 + 2D 层，不影响页面功能。
 */
namespace CyberTheme {
    const VERTEX_SOURCE = [
        "attribute vec2 aPos;",
        "varying vec2 vUv;",
        "void main() {",
        "    vUv = aPos * 0.5 + 0.5;",
        "    gl_Position = vec4(aPos, 0.0, 1.0);",
        "}",
    ].join("\n");

    const FRAGMENT_SOURCE = [
        "precision highp float;",
        "uniform float uTime;",
        "uniform vec2 uRes;",
        "uniform vec2 uPointer;",
        "uniform float uIntensity;",
        "varying vec2 vUv;",
        "",
        "float hash(vec2 p) {",
        "    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);",
        "}",
        "",
        "float noise(vec2 p) {",
        "    vec2 i = floor(p);",
        "    vec2 f = fract(p);",
        "    f = f * f * (3.0 - 2.0 * f);",
        "    float a = hash(i);",
        "    float b = hash(i + vec2(1.0, 0.0));",
        "    float c = hash(i + vec2(0.0, 1.0));",
        "    float d = hash(i + vec2(1.0, 1.0));",
        "    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);",
        "}",
        "",
        "float fbm(vec2 p) {",
        "    float value = 0.0;",
        "    float amp = 0.5;",
        "    for (int i = 0; i < 4; i++) {",
        "        value += amp * noise(p);",
        "        p = p * 2.06 + vec2(13.1, 7.7);",
        "        amp *= 0.5;",
        "    }",
        "    return value;",
        "}",
        "",
        "void main() {",
        "    vec2 uv = vUv;",
        "    float aspect = uRes.x / max(uRes.y, 1.0);",
        "    vec2 p = vec2(uv.x * aspect, uv.y);",
        "    float t = uTime;",
        "",
        "    // 冷色体积雾：两层低频噪声缓慢对流，形成有纵深的档案库空气。",
        "    float fog = fbm(p * 1.35 + vec2(t * 0.011, -t * 0.017));",
        "    float fog2 = fbm(p * 3.2 - vec2(t * 0.006, t * 0.009));",
        "",
        "    // 竖向数据导管：稀疏的列里有一段段自上而下流动的冷光。",
        "    float cols = 104.0;",
        "    float columnIndex = floor(uv.x * cols);",
        "    float columnSeed = hash(vec2(columnIndex, 3.0));",
        "    float activeColumn = step(0.64, hash(vec2(columnIndex, 11.0)));",
        "    float flow = fract(uv.y * 1.7 + t * (0.05 + columnSeed * 0.16) + columnSeed * 9.0);",
        "    float packet = smoothstep(0.0, 0.34, flow) * smoothstep(1.0, 0.58, flow);",
        "    float column = packet * activeColumn;",
        "",
        "    // 极细横向扫描线：只在低透明度下参与明暗，让画面像老式监视器。",
        "    float scan = 0.5 + 0.5 * sin(uv.y * 1080.0);",
        "    scan = pow(scan, 26.0) * 0.55;",
        "",
        "    // 周期性扫描带：约 30 秒从上到下掠过一次，带一条短拖尾。",
        "    float bandPos = fract(t * 0.033);",
        "    float band = exp(-abs(uv.y - bandPos) * 34.0);",
        "    band += smoothstep(bandPos, 0.0, uv.y) * 0.05 * (1.0 - bandPos);",
        "",
        "    // 指针冷光：跟随鼠标形成一小片局部照明，让背景“有反应”。",
        "    vec2 pointerPos = uPointer * 0.5 + 0.5;",
        "    vec2 pointerDelta = (uv - pointerPos) * vec2(aspect, 1.0);",
        "    float pointerLight = exp(-dot(pointerDelta, pointerDelta) * 7.5);",
        "",
        "    // 数据网格：非常克制的细线，只在画面边缘可辨。",
        "    vec2 gridUv = uv * vec2(aspect, 1.0) * 42.0;",
        "    float gridLine = smoothstep(0.485, 0.5, abs(fract(gridUv.x) - 0.5))",
        "        + smoothstep(0.485, 0.5, abs(fract(gridUv.y) - 0.5));",
        "    gridLine = clamp(gridLine, 0.0, 1.0) * 0.5;",
        "",
        "    // 偶发的横向数据残影：低频率、低强度，避免抢正文注意力。",
        "    float glitchRow = floor(uv.y * 86.0);",
        "    float glitch = step(0.985, hash(vec2(glitchRow, floor(t * 1.6))));",
        "    glitch *= 0.16 * step(0.55, hash(vec2(floor(t * 5.0), glitchRow)));",
        "",
        "    vec3 deep = vec3(0.012, 0.030, 0.040);",
        "    vec3 teal = vec3(0.075, 0.520, 0.420);",
        "    vec3 blue = vec3(0.090, 0.250, 0.580);",
        "    vec3 amber = vec3(0.560, 0.360, 0.110);",
        "",
        "    vec3 col = deep;",
        "    col = mix(col, blue, fog * 0.42);",
        "    col = mix(col, teal, fog2 * 0.30);",
        "    col = mix(col, teal, column * 0.55);",
        "    col = mix(col, blue, gridLine * 0.22);",
        "    col = mix(col, amber, glitch * 0.35);",
        "    col += vec3(0.10, 0.32, 0.28) * band * 0.12;",
        "    col += vec3(0.14, 0.44, 0.40) * pointerLight * 0.16;",
        "    col += scan * 0.015;",
        "",
        "    // 暗角：四周并入深色，正文与卡片更突出。",
        "    float vig = smoothstep(0.34, 0.96, distance(uv, vec2(0.5, 0.46)));",
        "    col *= 1.0 - vig * 0.72;",
        "",
        "    float grain = hash(gl_FragCoord.xy + floor(t * 26.0)) - 0.5;",
        "    col += grain * 0.014;",
        "",
        "    float alpha = 0.15 + fog * 0.05 + column * 0.05 + band * 0.06",
        "        + pointerLight * 0.05 + vig * 0.07;",
        "    alpha = clamp(alpha, 0.0, 0.52) * uIntensity;",
        "",
        "    gl_FragColor = vec4(col * alpha, alpha);",
        "}",
    ].join("\n");

    let canvas: HTMLCanvasElement | null = null;
    let gl: WebGLRenderingContext | null = null;
    let program: WebGLProgram | null = null;
    let buffer: WebGLBuffer | null = null;
    let uniformTime: WebGLUniformLocation | null = null;
    let uniformRes: WebGLUniformLocation | null = null;
    let uniformPointer: WebGLUniformLocation | null = null;
    let uniformIntensity: WebGLUniformLocation | null = null;
    let broken = false;

    function compileShader(
        context: WebGLRenderingContext,
        type: number,
        source: string,
    ): WebGLShader | null {
        const shader = context.createShader(type);
        if (shader === null) {
            return null;
        }
        context.shaderSource(shader, source);
        context.compileShader(shader);
        if (context.getShaderParameter(shader, context.COMPILE_STATUS) !== true) {
            context.deleteShader(shader);
            return null;
        }
        return shader;
    }

    function initContext(): boolean {
        const element = document.createElement("canvas");
        element.className = ATMOSPHERE_CLASS;
        element.setAttribute("aria-hidden", "true");
        const context = element.getContext("webgl", {
            alpha: true,
            premultipliedAlpha: true,
            antialias: false,
            depth: false,
            stencil: false,
            powerPreference: "low-power",
        });
        if (context === null) {
            return false;
        }

        const vertex = compileShader(context, context.VERTEX_SHADER, VERTEX_SOURCE);
        const fragment = compileShader(context, context.FRAGMENT_SHADER, FRAGMENT_SOURCE);
        if (vertex === null || fragment === null) {
            return false;
        }

        const linked = context.createProgram();
        if (linked === null) {
            return false;
        }
        context.attachShader(linked, vertex);
        context.attachShader(linked, fragment);
        context.linkProgram(linked);
        context.deleteShader(vertex);
        context.deleteShader(fragment);
        if (context.getProgramParameter(linked, context.LINK_STATUS) !== true) {
            return false;
        }

        const quad = context.createBuffer();
        if (quad === null) {
            return false;
        }
        context.bindBuffer(context.ARRAY_BUFFER, quad);
        context.bufferData(
            context.ARRAY_BUFFER,
            new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
            context.STATIC_DRAW,
        );
        const position = context.getAttribLocation(linked, "aPos");
        context.enableVertexAttribArray(position);
        context.vertexAttribPointer(position, 2, context.FLOAT, false, 0, 0);

        context.useProgram(linked);
        context.clearColor(0, 0, 0, 0);

        gl = context;
        program = linked;
        buffer = quad;
        uniformTime = context.getUniformLocation(linked, "uTime");
        uniformRes = context.getUniformLocation(linked, "uRes");
        uniformPointer = context.getUniformLocation(linked, "uPointer");
        uniformIntensity = context.getUniformLocation(linked, "uIntensity");
        canvas = element;
        element.addEventListener("webglcontextlost", (event) => {
            event.preventDefault();
            broken = true;
        });
        document.body.appendChild(element);
        return true;
    }

    function boot(): void {
        if (canvas !== null || broken) {
            return;
        }
        broken = !initContext();
    }

    function resize(viewport: Viewport): void {
        if (canvas === null || gl === null) {
            return;
        }
        // 0.38 倍分辨率渲染后由 CSS 拉伸：氛围层只需柔和明暗，像素成本大幅下降。
        const width = clamp(Math.round(viewport.width * 0.38), 320, 820);
        const height = Math.max(180, Math.round(width * (viewport.height / Math.max(1, viewport.width))));
        canvas.width = width;
        canvas.height = height;
        gl.viewport(0, 0, width, height);
    }

    function draw(time: number, pointer: Pointer, intensity: number): void {
        if (gl === null || canvas === null || program === null || broken) {
            return;
        }
        if (gl.isContextLost()) {
            return;
        }
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.useProgram(program);
        if (uniformTime !== null) {
            gl.uniform1f(uniformTime, time);
        }
        if (uniformRes !== null) {
            gl.uniform2f(uniformRes, canvas.width, canvas.height);
        }
        if (uniformPointer !== null) {
            gl.uniform2f(uniformPointer, pointer.x * 0.7, pointer.y * 0.5);
        }
        if (uniformIntensity !== null) {
            gl.uniform1f(uniformIntensity, clamp(intensity, 0, 1));
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    export const Atmosphere = { boot, resize, draw };
}
