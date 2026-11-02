/**
 * 「深渊帷幕」主题 · WebGL 体积氛围层
 *
 * 位于 2D 画布之下、页面底色之上，只负责“水”本身：
 * 低频体积雾、上缘垂下的光柱、缓慢干涉的焦散、远处巨物掠过的阴影、暗角与细颗粒。
 * 这些如果用 2D Canvas 逐帧绘制会非常昂贵，交给 GPU 后几乎不占主线程。
 *
 * 为了在低端设备与无 GPU 的无头浏览器上也不出错：
 * - 以约 0.5 倍分辨率渲染，再由 CSS 拉伸（氛围层本来就该是柔的）；
 * - WebGL 不可用、着色器编译失败或上下文丢失时，静默移除画布，完全回退到纯 CSS / 2D；
 * - 只在主题激活时随主循环绘制，页面隐藏时随之停摆。
 */
namespace AbyssTheme {
    const ATMOSPHERE_CLASS = "theme-abyss-atmosphere";

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
        "    for (int i = 0; i < 5; i++) {",
        "        value += amp * noise(p);",
        "        p = p * 2.03 + vec2(17.3, 9.1);",
        "        amp *= 0.5;",
        "    }",
        "    return value;",
        "}",
        "",
        "void main() {",
        "    vec2 uv = vUv;",
        "    float aspect = uRes.x / max(uRes.y, 1.0);",
        "    vec2 p = vec2(uv.x * aspect, uv.y) + uPointer * 0.05;",
        "    float t = uTime;",
        "",
        "    // 体积雾：两层低频噪声形成缓慢移动的暗色水体。",
        "    float fog = fbm(p * 1.1 + vec2(t * 0.012, -t * 0.02));",
        "    float fog2 = fbm(p * 2.3 - vec2(t * 0.008, t * 0.011));",
        "",
        "    // 焦散：干涉条纹只保留最窄的亮峰，避免整屏发白。",
        "    float c = fbm(p * 3.4 + vec2(t * 0.03, -t * 0.025) + fog * 0.6);",
        "    float caustic = pow(0.5 + 0.5 * sin(c * 9.0 + fog2 * 5.0 + t * 0.35), 8.0);",
        "",
        "    // 自上方垂下的光柱。",
        "    float raySeed = fbm(vec2(p.x * 1.7 + t * 0.018, p.y * 0.4 - t * 0.006));",
        "    float rays = pow(max(0.0, raySeed * 1.5 - 0.45), 2.2);",
        "    rays *= smoothstep(1.05, 0.05, uv.y);",
        "    rays *= 0.62 + 0.38 * sin(uv.y * 6.0 + t * 0.2);",
        "",
        "    // 远处某个巨大物体缓慢掠过，压低局部亮度。",
        "    float mass = smoothstep(0.52, 0.95, fbm(p * 0.55 + vec2(t * 0.008, 0.0)));",
        "",
        "    vec3 deep = vec3(0.020, 0.015, 0.032);",
        "    vec3 brass = vec3(0.58, 0.46, 0.26);",
        "    vec3 plum = vec3(0.30, 0.20, 0.42);",
        "    vec3 bone = vec3(0.85, 0.80, 0.68);",
        "",
        "    vec3 col = deep;",
        "    col = mix(col, plum, fog * 0.55);",
        "    col = mix(col, brass, caustic * 0.30);",
        "    col = mix(col, bone, rays * 0.55);",
        "    col = mix(col, deep * 0.35, mass * 0.75);",
        "",
        "    // 暗角：画面四周并入黑暗，正文与卡片因此更突出。",
        "    float vig = smoothstep(0.35, 0.95, distance(uv, vec2(0.5, 0.45)));",
        "    col *= 1.0 - vig * 0.78;",
        "",
        "    float grain = hash(gl_FragCoord.xy + floor(t * 24.0)) - 0.5;",
        "",
        "    float alpha = 0.16 + fog * 0.06 + caustic * 0.12 + rays * 0.17 + vig * 0.1;",
        "    alpha = clamp(alpha, 0.0, 0.6) * uIntensity;",
        "    col += grain * 0.018;",
        "",
        "    gl_FragColor = vec4(col * alpha, alpha);",
        "}",
    ].join("\n");

    let atmosphereCanvas: HTMLCanvasElement | null = null;
    let atmosphereGl: WebGLRenderingContext | null = null;
    let atmosphereProgram: WebGLProgram | null = null;
    let atmosphereBuffer: WebGLBuffer | null = null;
    let uniformTime: WebGLUniformLocation | null = null;
    let uniformRes: WebGLUniformLocation | null = null;
    let uniformPointer: WebGLUniformLocation | null = null;
    let uniformIntensity: WebGLUniformLocation | null = null;
    let atmosphereBroken = false;

    function compileShader(
        gl: WebGLRenderingContext,
        type: number,
        source: string,
    ): WebGLShader | null {
        const shader = gl.createShader(type);
        if (!shader) {
            return null;
        }
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            gl.deleteShader(shader);
            return null;
        }
        return shader;
    }

    function initContext(): boolean {
        const element = document.createElement("canvas");
        element.className = ATMOSPHERE_CLASS;
        element.setAttribute("aria-hidden", "true");
        const gl = element.getContext("webgl", {
            alpha: true,
            premultipliedAlpha: true,
            antialias: false,
            depth: false,
            stencil: false,
            powerPreference: "low-power",
        });
        if (!gl) {
            return false;
        }

        const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SOURCE);
        const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SOURCE);
        if (!vertex || !fragment) {
            return false;
        }
        const program = gl.createProgram();
        if (!program) {
            return false;
        }
        gl.attachShader(program, vertex);
        gl.attachShader(program, fragment);
        gl.linkProgram(program);
        gl.deleteShader(vertex);
        gl.deleteShader(fragment);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            return false;
        }

        const buffer = gl.createBuffer();
        if (!buffer) {
            return false;
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(
            gl.ARRAY_BUFFER,
            new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
            gl.STATIC_DRAW,
        );
        const position = gl.getAttribLocation(program, "aPos");
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

        gl.useProgram(program);
        gl.clearColor(0, 0, 0, 0);

        atmosphereGl = gl;
        atmosphereProgram = program;
        atmosphereBuffer = buffer;
        uniformTime = gl.getUniformLocation(program, "uTime");
        uniformRes = gl.getUniformLocation(program, "uRes");
        uniformPointer = gl.getUniformLocation(program, "uPointer");
        uniformIntensity = gl.getUniformLocation(program, "uIntensity");
        atmosphereCanvas = element;
        element.addEventListener("webglcontextlost", (event) => {
            event.preventDefault();
            atmosphereBroken = true;
        });
        document.body.appendChild(element);
        return true;
    }

    function boot(): void {
        if (atmosphereCanvas !== null || atmosphereBroken) {
            return;
        }
        atmosphereBroken = !initContext();
    }

    function resize(viewport: Viewport): void {
        if (atmosphereCanvas === null || atmosphereGl === null) {
            return;
        }
        // 低分辨率渲染：氛围层只需要柔和的明暗，把像素省下来给触手与法阵。
        // 它本来就是一层被 CSS 拉柔的雾，0.36 倍与 0.45 倍在观感上几乎没有差别，
        // 但片元着色器的像素量少掉约三成，弱显卡上的掉帧会明显缓解。
        const width = clamp(Math.round(viewport.width * 0.36), 320, 760);
        const height = Math.max(180, Math.round(width * (viewport.height / Math.max(1, viewport.width))));
        atmosphereCanvas.width = width;
        atmosphereCanvas.height = height;
        atmosphereGl.viewport(0, 0, width, height);
    }

    function draw(time: number, pointer: Pointer, intensity: number): void {
        if (atmosphereGl === null || atmosphereCanvas === null || atmosphereBroken) {
            return;
        }
        const gl = atmosphereGl;
        if (gl.isContextLost()) {
            return;
        }
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.useProgram(atmosphereProgram);
        if (uniformTime !== null) {
            gl.uniform1f(uniformTime, time);
        }
        if (uniformRes !== null) {
            gl.uniform2f(uniformRes, atmosphereCanvas.width, atmosphereCanvas.height);
        }
        if (uniformPointer !== null) {
            gl.uniform2f(uniformPointer, pointer.x * 0.6, pointer.y * 0.4);
        }
        if (uniformIntensity !== null) {
            gl.uniform1f(uniformIntensity, clamp(intensity, 0, 1));
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, atmosphereBuffer);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    export const Atmosphere = { boot, resize, draw };
}
