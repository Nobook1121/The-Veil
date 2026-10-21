/**
 * 3D 骰子舞台（独立 bundle）：正交垂直俯视 + cannon-es 物理翻滚。
 *
 * 骰型严格按 `groups` 渲染（d4/d6/d8/d10/d12/d20，d100 拆成两枚 d10），
 * 绝不把不支持的骰型退化成 d6；无法渲染时抛出 `unsupported_dice:*`，
 * 由降级控制器切到简易动画。服务端结果是权威值，物理只负责视觉表现。
 */
import * as CANNON from "cannon-es";
import * as THREE from "three";
import { ConvexHull } from "three/examples/jsm/math/ConvexHull.js";

interface Dice3DGroup {
    role: string;
    type: string;
    count: number;
    faces: number[];
    labels: string[];
}

interface Dice3DSelection {
    group: number;
    index: number;
}

interface Dice3DPlan {
    mode: string;
    expression: string;
    count: number;
    sides: number;
    bonus_dice: number;
    penalty_dice: number;
    candidates: number[];
    groups?: Dice3DGroup[];
    selected?: Dice3DSelection | null;
}

interface Dice3DSnapshot {
    faces?: number[];
    labels?: string[];
    groups?: Dice3DGroup[];
    selected?: Dice3DSelection | null;
    roll?: number | null;
    rolls?: number[];
    total?: number | null;
    mode?: string;
    expression?: string;
    count?: number;
    sides?: number;
    candidates?: number[];
}

interface Dice3DOptions {
    plan: Dice3DPlan;
    faceUrls?: Record<string, string>;
    tableSkinUrl?: string;
    colors?: { body?: string; text?: string; table?: string; edge?: string };
}

interface Dice3DInstance {
    /** 骰子落定后返回各骰子朝上面的原始点数（按骰组顺序）。 */
    roll(snapshot: Dice3DSnapshot, seed?: number | null): Promise<number[]>;
    setStatic(snapshot: Dice3DSnapshot): void;
    showTable(): void;
    freeze(): string | null;
    dispose(): void;
}

interface FaceInfo {
    normal: THREE.Vector3;
    up: THREE.Vector3;
    u: THREE.Vector3;
    value: number;
    centroid: THREE.Vector3;
}

interface DieGeometry {
    buffer: THREE.BufferGeometry;
    faces: FaceInfo[];
    polygons: THREE.Vector3[][];
    vertices: THREE.Vector3[];
    inradius: number;
}

interface DiceSlot {
    role: string;
    type: string;
    value: number;
    label: string;
    x: number;
    z: number;
}

interface DiceLayout {
    slots: DiceSlot[];
    width: number;
    depth: number;
    selectedIndex: number;
}

interface DieActor {
    mesh: THREE.Mesh;
    body: CANNON.Body;
    materials: THREE.MeshStandardMaterial[];
    target: THREE.Quaternion;
    type: string;
    role: string;
    value: number;
}

const DIE_RADIUS = 0.62;
/** 物理模拟固定步数上限：按渲染帧计数而非墙钟，保证各客户端落定状态一致。 */
const MAX_ROLL_STEPS = 480;
const RENDER_TYPES = new Set(["d4", "d6", "d8", "d10", "d12", "d20"]);
const FACE_COUNTS: Record<string, number> = { d4: 4, d6: 6, d8: 8, d10: 10, d12: 12, d20: 20 };

function color(value: string | undefined, fallback: string): string {
    return value && value.trim() ? value : fallback;
}

function normalizeType(type: string): string {
    const normalized = String(type || "").toLowerCase();
    if (normalized === "d100") return "d10";
    return RENDER_TYPES.has(normalized) ? normalized : "";
}

function average(points: THREE.Vector3[]): THREE.Vector3 {
    const sum = new THREE.Vector3();
    points.forEach((point) => sum.add(point));
    return sum.divideScalar(points.length || 1);
}

// ---------------------------------------------------------------- 几何构建

/** 各骰型的顶点（外接球半径归一化为 1）。 */
function polyPoints(type: string): THREE.Vector3[] | null {
    const points: Array<[number, number, number]> = [];
    const phi = (1 + Math.sqrt(5)) / 2;
    switch (type) {
        case "d4":
            return [[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]].map((v) => new THREE.Vector3(...v).normalize());
        case "d6":
            for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) points.push([x, y, z]);
            break;
        case "d8":
            return [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]].map((v) => new THREE.Vector3(...v));
        case "d10": {
            // 五角偏方面体：上下顶点 + 交错旋转 36° 的两圈五边形。
            // 环高度由「十个风筝面共面」的几何条件推导，保证每个面是真正的平面。
            const height = 1;
            const half = height / (1 + (2 * Math.cos(Math.PI / 5)) / (1 - Math.cos(Math.PI / 5)));
            points.push([0, height, 0], [0, -height, 0]);
            for (let i = 0; i < 5; i += 1) {
                const upper = THREE.MathUtils.degToRad(i * 72 + 36);
                const lower = THREE.MathUtils.degToRad(i * 72);
                points.push([Math.cos(upper), half, Math.sin(upper)]);
                points.push([Math.cos(lower), -half, Math.sin(lower)]);
            }
            break;
        }
        case "d12":
            for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) points.push([x, y, z]);
            for (const a of [-1, 1]) for (const b of [-1, 1]) {
                points.push([0, a / phi, b * phi]);
                points.push([a / phi, b * phi, 0]);
                points.push([a * phi, 0, b / phi]);
            }
            break;
        case "d20":
            for (const sign of [-1, 1]) {
                points.push([0, sign, sign * phi], [sign, sign * phi, 0], [sign * phi, 0, sign]);
                points.push([0, sign, -sign * phi], [sign, -sign * phi, 0], [sign * phi, 0, -sign]);
            }
            break;
        default:
            return null;
    }
    const result = points.map((value) => new THREE.Vector3(...value).normalize());
    return result;
}

function faceBasis(normal: THREE.Vector3): { u: THREE.Vector3; v: THREE.Vector3 } {
    const n = normal.clone().normalize();
    const v = Math.abs(n.y) > 0.999
        ? new THREE.Vector3(0, 0, n.y > 0 ? -1 : 1)
        : new THREE.Vector3(0, 1, 0).projectOnPlane(n).normalize();
    const u = new THREE.Vector3().crossVectors(v, n).normalize();
    return { u, v };
}

function orderPolygon(points: THREE.Vector3[], normal: THREE.Vector3): THREE.Vector3[] {
    const centroid = average(points);
    const basis = faceBasis(normal);
    const ordered = [...points].sort((a, b) => {
        const angleA = Math.atan2(a.clone().sub(centroid).dot(basis.v), a.clone().sub(centroid).dot(basis.u));
        const angleB = Math.atan2(b.clone().sub(centroid).dot(basis.v), b.clone().sub(centroid).dot(basis.u));
        return angleA - angleB;
    });
    // 确认绕向：多边形右手法线应与外法线一致。
    const cross = new THREE.Vector3();
    for (let index = 0; index < ordered.length; index += 1) {
        const current = ordered[index]!.clone().sub(ordered[0]!);
        const next = ordered[(index + 1) % ordered.length]!.clone().sub(ordered[0]!);
        cross.add(new THREE.Vector3().crossVectors(current, next));
    }
    if (cross.dot(normal) < 0) ordered.reverse();
    return ordered;
}

/** 用凸包把顶点转成多边形面，并把共面三角形合并成完整骰面。 */
function hullFaces(points: THREE.Vector3[]): Array<{ points: THREE.Vector3[]; normal: THREE.Vector3 }> {
    const hull = new ConvexHull().setFromPoints(points);
    const triangles: Array<{ points: THREE.Vector3[]; normal: THREE.Vector3; offset: number }> = [];
    hull.faces.forEach((face) => {
        const polygon: THREE.Vector3[] = [];
        const start = face.edge;
        let edge = start;
        do {
            polygon.push(edge.head().point.clone());
            edge = edge.next;
        } while (edge !== start);
        if (polygon.length < 3) return;
        const normal = face.normal.clone().normalize();
        triangles.push({ points: polygon, normal, offset: average(polygon).dot(normal) });
    });

    const planes: Array<{ normal: THREE.Vector3; offset: number; points: THREE.Vector3[] }> = [];
    triangles.forEach((triangle) => {
        const plane = planes.find((candidate) => candidate.normal.dot(triangle.normal) > 0.9995
            && Math.abs(candidate.offset - triangle.offset) < 2e-3);
        const target = plane || (() => {
            const created = { normal: triangle.normal.clone(), offset: triangle.offset, points: [] as THREE.Vector3[] };
            planes.push(created);
            return created;
        })();
        triangle.points.forEach((point) => {
            if (!target.points.some((existing) => existing.distanceToSquared(point) < 1e-8)) {
                target.points.push(point.clone());
            }
        });
    });

    return planes.map((plane) => ({
        points: orderPolygon(plane.points, plane.normal),
        normal: plane.normal,
    }));
}

function faceValue(type: string, index: number): number {
    return type === "d10" ? index : index + 1;
}

/** 32 位确定性伪随机数：同一 seed 在所有客户端产生完全相同的投掷序列。 */
function mulberry32(seed: number): () => number {
    let state = seed >>> 0;
    return () => {
        state = (state + 0x6d2b79f5) >>> 0;
        let value = state;
        value = Math.imul(value ^ (value >>> 15), value | 1);
        value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
        return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
    };
}

function buildGeometry(type: string): DieGeometry {
    const points = polyPoints(type);
    if (!points) throw new Error(`unsupported_dice:${type}`);
    const polygons = hullFaces(points);
    const expected = FACE_COUNTS[type] || 0;
    if (polygons.length !== expected) {
        throw new Error(`geometry_face_mismatch:${type}:${polygons.length}`);
    }

    const faces: FaceInfo[] = [];
    const positions: number[] = [];
    const normals: number[] = [];
    const uvs: number[] = [];
    const groups: Array<{ start: number; count: number }> = [];
    let vertexStart = 0;

    polygons.forEach((polygon, index) => {
        const normal = polygon.normal.clone().normalize();
        const centroid = average(polygon.points);
        const basis = faceBasis(normal);
        let extent = 0;
        polygon.points.forEach((point) => {
            const relative = point.clone().sub(centroid);
            extent = Math.max(extent, Math.abs(relative.dot(basis.u)), Math.abs(relative.dot(basis.v)));
        });
        const span = Math.max(extent * 1.26, 1e-3);
        let count = 0;
        for (let corner = 1; corner < polygon.points.length - 1; corner += 1) {
            [polygon.points[0]!, polygon.points[corner]!, polygon.points[corner + 1]!].forEach((point) => {
                const relative = point.clone().sub(centroid);
                positions.push(point.x, point.y, point.z);
                normals.push(normal.x, normal.y, normal.z);
                uvs.push(relative.dot(basis.u) / span + 0.5, relative.dot(basis.v) / span + 0.5);
                count += 1;
            });
        }
        groups.push({ start: vertexStart, count });
        vertexStart += count;
        faces.push({
            normal,
            u: basis.u,
            up: basis.v,
            value: faceValue(type, index),
            centroid,
        });
    });

    const buffer = new THREE.BufferGeometry();
    buffer.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    buffer.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
    buffer.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
    groups.forEach((group, index) => buffer.addGroup(group.start, group.count, index));
    buffer.computeBoundingSphere();

    const vertices: THREE.Vector3[] = [];
    polygons.forEach((polygon) => {
        polygon.points.forEach((point) => {
            if (!vertices.some((existing) => existing.distanceToSquared(point) < 1e-8)) {
                vertices.push(point.clone());
            }
        });
    });
    const inradius = faces.reduce((minimum, face) => Math.min(minimum, Math.abs(face.centroid.dot(face.normal))), 1);

    return { buffer, faces, polygons: polygons.map((polygon) => polygon.points), vertices, inradius };
}

// ---------------------------------------------------------------- 面贴图

function pipsFor(value: number): number[] {
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

function drawFacePips(context: CanvasRenderingContext2D, value: number, textColor: string): void {
    const active = new Set(pipsFor(value));
    const cell = 56;
    const offset = (256 - cell * 3) / 2 + cell / 2;
    context.fillStyle = textColor;
    for (let index = 0; index < 9; index += 1) {
        if (!active.has(index)) continue;
        const column = index % 3;
        const row = Math.floor(index / 3);
        context.beginPath();
        context.arc(offset + column * cell, offset + row * cell, cell * 0.31, 0, Math.PI * 2);
        context.fill();
    }
}

function faceCanvas(type: string, label: string, value: number, bodyColor: string, textColor: string): HTMLCanvasElement {
    const canvas = document.createElement("canvas");
    canvas.width = 256;
    canvas.height = 256;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("canvas_unavailable");
    context.fillStyle = bodyColor;
    context.fillRect(0, 0, 256, 256);
    context.strokeStyle = "rgba(0, 0, 0, 0.16)";
    context.lineWidth = 10;
    context.strokeRect(5, 5, 246, 246);

    if (type === "d6") {
        drawFacePips(context, value, textColor);
        return canvas;
    }
    context.fillStyle = textColor;
    const fontSize = type === "d10" ? 112 : 118;
    context.font = `700 ${fontSize}px "Segoe UI", system-ui, sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(label, 128, 136);
    return canvas;
}

function labelForValue(type: string, role: string, value: number): string {
    if (type === "d10" && (role === "tens" || role === "candidate")) return String(value * 10).padStart(2, "0");
    // 百分骰的个位面显示 0-9；通用 d10 与后端 1-10 的取值保持一致。
    if (type === "d10" && role === "normal") return String(value === 0 ? 10 : value);
    return String(value);
}

// ---------------------------------------------------------------- 布局

function baseGroups(plan: Dice3DPlan, snapshot: Dice3DSnapshot | null): Dice3DGroup[] {
    const source = snapshot?.groups?.length ? snapshot.groups : plan.groups;
    if (source?.length) {
        return source.map((group) => ({
            role: String(group.role || "normal"),
            type: String(group.type || ""),
            count: Math.max(1, Number(group.count) || 1),
            faces: (group.faces || []).map(Number),
            labels: (group.labels || []).map(String),
        }));
    }
    if (plan.mode === "percentile") {
        return [
            { role: "tens", type: "d10", count: 1, faces: [], labels: [] },
            { role: "ones", type: "d10", count: 1, faces: [], labels: [] },
            { role: "candidate", type: "d10", count: Math.max(0, Number(plan.bonus_dice) || 0), faces: [], labels: [] },
            { role: "candidate", type: "d10", count: Math.max(0, Number(plan.penalty_dice) || 0), faces: [], labels: [] },
        ].filter((group) => group.count > 0);
    }
    const groups: Dice3DGroup[] = [];
    String(plan.expression || "").replace(/\s+/g, "").toLowerCase().split("+").forEach((term) => {
        const match = /^(\d+)d(\d+)$/.exec(term);
        if (!match) return;
        const count = Number.parseInt(match[1] || "1", 10);
        const sides = Number.parseInt(match[2] || "6", 10);
        if (sides === 100) {
            for (let index = 0; index < count; index += 1) {
                groups.push({ role: "tens", type: "d10", count: 1, faces: [], labels: [] });
                groups.push({ role: "ones", type: "d10", count: 1, faces: [], labels: [] });
            }
            return;
        }
        groups.push({ role: "normal", type: `d${sides}`, count, faces: [], labels: [] });
    });
    if (!groups.length) {
        groups.push({ role: "normal", type: `d${Math.max(2, Number(plan.sides) || 20)}`, count: Math.max(1, Number(plan.count) || 1), faces: [], labels: [] });
    }
    return groups;
}

function resolveGroups(plan: Dice3DPlan, snapshot: Dice3DSnapshot | null): Dice3DGroup[] {
    const groups = baseGroups(plan, snapshot);
    const faces = (snapshot?.faces || []).map(Number);
    const labels = (snapshot?.labels || []).map(String);
    let cursor = 0;
    groups.forEach((group) => {
        if (!group.faces.length) group.faces = faces.slice(cursor, cursor + group.count);
        if (!group.labels.length) group.labels = labels.slice(cursor, cursor + group.count);
        while (group.faces.length < group.count) group.faces.push(group.type === "d10" ? 0 : 1);
        while (group.labels.length < group.count) {
            group.labels.push(labelForValue(group.type, group.role, group.faces[group.labels.length] || 0));
        }
        cursor += group.count;
    });
    return groups;
}

function selectedIndexFor(groups: Dice3DGroup[], selected: Dice3DSelection | null | undefined): number {
    if (!selected) return -1;
    const groupIndex = Number(selected.group);
    const dieIndex = Number(selected.index);
    if (!Number.isFinite(groupIndex) || groupIndex < 0 || groupIndex >= groups.length) return -1;
    let offset = 0;
    for (let index = 0; index < groupIndex; index += 1) offset += groups[index]!.count;
    return offset + (Number.isFinite(dieIndex) ? dieIndex : 0);
}

function layoutDice(plan: Dice3DPlan, snapshot: Dice3DSnapshot | null, limit: number, containerAspect = 1.8): DiceLayout {
    const groups = resolveGroups(plan, snapshot);
    groups.forEach((group) => {
        const type = normalizeType(group.type);
        if (!type) throw new Error(`unsupported_dice:${group.type}`);
    });
    const total = groups.reduce((sum, group) => sum + group.count, 0);
    if (total > limit) throw new Error(`dice_count_exceeded:${total}`);

    const spacingX = DIE_RADIUS * 2.35;
    const spacingZ = DIE_RADIUS * 2.5;
    // 所有骰子共享同一张桌面与同一个物理世界：按接近容器比例的紧凑网格排布，
    // 让它们在同一片区域里互相碰撞，而不是每组各自占一小块区域。
    const aspect = Math.max(1, Math.min(3.2, containerAspect));
    const columns = Math.max(1, Math.min(total, Math.ceil(Math.sqrt(total * aspect * (spacingZ / spacingX)))));
    const rows = Math.ceil(total / columns);
    const gridWidth = columns * spacingX;
    const gridDepth = rows * spacingZ;

    const slots: DiceSlot[] = [];
    let flatIndex = 0;
    groups.forEach((group) => {
        const type = normalizeType(group.type);
        for (let index = 0; index < group.count; index += 1) {
            const column = flatIndex % columns;
            const row = Math.floor(flatIndex / columns);
            const rowCount = Math.min(columns, total - row * columns);
            const x = (column - (rowCount - 1) / 2) * spacingX;
            const z = (row - (rows - 1) / 2) * spacingZ;
            const value = Number(group.faces[index] ?? (group.type === "d10" ? 0 : 1));
            slots.push({
                role: group.role,
                type,
                value,
                label: group.labels[index] || labelForValue(type, group.role, value),
                x,
                z,
            });
            flatIndex += 1;
        }
    });

    return {
        slots,
        width: Math.max(4.6, gridWidth + 1.8),
        depth: Math.max(3.4, gridDepth + 1.8),
        selectedIndex: selectedIndexFor(groups, snapshot?.selected ?? plan.selected ?? null),
    };
}

// ---------------------------------------------------------------- 实例

function create(container: HTMLElement, options: Dice3DOptions): Dice3DInstance {
    const colors = options.colors || {};
    const bodyColor = color(colors.body, "#f6f1e7");
    const textColor = color(colors.text, "#2b2a33");
    const tableColor = color(colors.table, "#1d3a34");
    const edgeColor = color(colors.edge, "#d8b56d");

    const renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        powerPreference: "high-performance",
        // 实例超限时需要把当前画面导出成静态图，否则 toDataURL 会得到空图。
        preserveDrawingBuffer: true,
    });
    if (!renderer.getContext()) throw new Error("webgl_unavailable");
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.appendChild(renderer.domElement);
    renderer.domElement.className = "dice3d-canvas";

    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 60);
    camera.position.set(0, 14, 0);
    camera.up.set(0, 0, -1);
    camera.lookAt(0, 0, 0);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2a2a, 1.1));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.7);
    keyLight.position.set(2.2, 9, 2.6);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.set(1024, 1024);
    scene.add(keyLight);
    scene.add(keyLight.target);
    const fillLight = new THREE.DirectionalLight(0xffffff, 0.35);
    fillLight.position.set(-3, 6, -2);
    scene.add(fillLight);

    const geometryCache = new Map<string, DieGeometry>();
    const textureCache = new Map<string, THREE.Texture>();
    const textureLoader = new THREE.TextureLoader();
    const ownedTextures = new Set<THREE.Texture>();

    const world = new CANNON.World({ gravity: new CANNON.Vec3(0, -9.82, 0) });
    world.broadphase = new CANNON.SAPBroadphase(world);
    world.allowSleep = true;

    const diceMaterial = new CANNON.Material("dice-body");
    const groundMaterial = new CANNON.Material("dice-ground");
    world.addContactMaterial(new CANNON.ContactMaterial(diceMaterial, diceMaterial, {
        friction: 0.3,
        restitution: 0.4,
    }));
    world.addContactMaterial(new CANNON.ContactMaterial(diceMaterial, groundMaterial, {
        friction: 0.5,
        restitution: 0.3,
    }));

    let tableMaterial = new THREE.MeshStandardMaterial({ color: tableColor, roughness: 0.9, metalness: 0.05 });
    const table = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), tableMaterial);
    table.rotation.x = -Math.PI / 2;
    table.receiveShadow = true;
    scene.add(table);

    const groundBody = new CANNON.Body({ mass: 0, shape: new CANNON.Plane(), material: groundMaterial });
    groundBody.quaternion.setFromEuler(-Math.PI / 2, 0, 0);
    world.addBody(groundBody);

    const actors: DieActor[] = [];
    const walls: CANNON.Body[] = [];
    let layout: DiceLayout = { slots: [], width: 6, depth: 4, selectedIndex: -1 };
    let disposed = false;
    let frame = 0;

    function geometryFor(type: string): DieGeometry {
        const cached = geometryCache.get(type);
        if (cached) return cached;
        const built = buildGeometry(type);
        geometryCache.set(type, built);
        return built;
    }

    function textureForCanvas(key: string, createTexture: () => THREE.Texture): THREE.Texture {
        const cached = textureCache.get(key);
        if (cached) return cached;
        const texture = createTexture();
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.anisotropy = 4;
        texture.needsUpdate = true;
        textureCache.set(key, texture);
        ownedTextures.add(texture);
        return texture;
    }

    function faceTexture(type: string, role: string, value: number, body: string): THREE.Texture {
        const key = `canvas|${type}|${role}|${value}|${body}|${textColor}`;
        return textureForCanvas(key, () => new THREE.CanvasTexture(
            faceCanvas(type, labelForValue(type, role, value), value, body, textColor),
        ));
    }

    /** 皮肤贴图按 URL 缓存；加载失败保留 Canvas 面，仅记录警告。 */
    function loadSkinTexture(url: string, onReady: (texture: THREE.Texture) => void): void {
        const cached = textureCache.get(url);
        if (cached) {
            onReady(cached);
            return;
        }
        textureLoader.load(url, (texture) => {
            texture.colorSpace = THREE.SRGBColorSpace;
            textureCache.set(url, texture);
            ownedTextures.add(texture);
            onReady(texture);
        }, undefined, () => console.warn("texture_load_failed", url));
    }

    /** 取某个数值对应的面贴图：优先使用管理员上传的皮肤，否则用 Canvas 面。 */
    function faceMapFor(
        type: string,
        role: string,
        value: number,
        material: THREE.MeshStandardMaterial,
    ): THREE.Texture {
        const skin = options.faceUrls?.[String(value)];
        if (skin) {
            const cached = textureCache.get(skin);
            if (cached) return cached;
            loadSkinTexture(skin, (texture) => {
                material.map = texture;
                material.color.set(0xffffff);
                material.needsUpdate = true;
            });
        }
        return faceTexture(type, role, value, bodyColor);
    }

    function makeMaterials(type: string, role: string, geometry: DieGeometry): THREE.MeshStandardMaterial[] {
        return geometry.faces.map((face) => {
            const material = new THREE.MeshStandardMaterial({
                color: 0xffffff,
                roughness: 0.55,
                metalness: 0.08,
                emissive: new THREE.Color(edgeColor),
                emissiveIntensity: 0.06,
            });
            material.map = faceMapFor(type, role, face.value, material);
            return material;
        });
    }

    function makeBody(type: string, geometry: DieGeometry): CANNON.Body {
        if (type === "d6") {
            const half = DIE_RADIUS / Math.sqrt(3);
            return new CANNON.Body({
                mass: 1,
                shape: new CANNON.Box(new CANNON.Vec3(half, half, half)),
                material: diceMaterial,
                linearDamping: 0.22,
                angularDamping: 0.28,
                allowSleep: true,
                sleepSpeedLimit: 0.2,
                sleepTimeLimit: 0.3,
            });
        }
        const vertices = geometry.vertices.map((point) => new CANNON.Vec3(
            point.x * DIE_RADIUS,
            point.y * DIE_RADIUS,
            point.z * DIE_RADIUS,
        ));
        const faces = geometry.polygons.map((polygon) => polygon.map((point) => geometry.vertices.findIndex(
            (vertex) => vertex.distanceToSquared(point) < 1e-8,
        )));
        const normals = geometry.faces.map((face) => new CANNON.Vec3(face.normal.x, face.normal.y, face.normal.z));
        return new CANNON.Body({
            mass: 1,
            shape: new CANNON.ConvexPolyhedron({ vertices, faces, normals }),
            material: diceMaterial,
            linearDamping: 0.22,
            angularDamping: 0.28,
            allowSleep: true,
            sleepSpeedLimit: 0.2,
            sleepTimeLimit: 0.3,
        });
    }

    function targetQuaternion(face: FaceInfo): THREE.Quaternion {
        const local = new THREE.Matrix4().makeBasis(
            face.u.clone().normalize(),
            face.up.clone().normalize(),
            face.normal.clone().normalize(),
        );
        const worldBasis = new THREE.Matrix4().makeBasis(
            new THREE.Vector3(1, 0, 0),
            new THREE.Vector3(0, 0, -1),
            new THREE.Vector3(0, 1, 0),
        );
        const matrix = worldBasis.multiply(local.transpose());
        return new THREE.Quaternion().setFromRotationMatrix(matrix);
    }

    function faceFor(type: string, geometry: DieGeometry, value: number): FaceInfo {
        const found = geometry.faces.find((face) => face.value === value);
        if (found) return found;
        return geometry.faces[0]!;
    }

    function syncMesh(actor: DieActor): void {
        actor.mesh.position.set(actor.body.position.x, actor.body.position.y, actor.body.position.z);
        actor.mesh.quaternion.set(actor.body.quaternion.x, actor.body.quaternion.y, actor.body.quaternion.z, actor.body.quaternion.w);
    }

    /** 找到静止后代表结果的骰面：普通骰取朝上，d4 取朝下（静止时无朝上面）。 */
    function resultFaceIndex(actor: { type: string; mesh: { quaternion: THREE.Quaternion } }): number {
        const geometry = geometryFor(actor.type);
        const rotated = new THREE.Vector3();
        let upIndex = -1;
        // d4 没有朝上的面（静止时有一个面朝下），取朝下的面作为结果。
        let best = actor.type === "d4" ? Infinity : -Infinity;
        geometry.faces.forEach((face, index) => {
            rotated.copy(face.normal).applyQuaternion(actor.mesh.quaternion);
            if (actor.type === "d4" ? rotated.y < best : rotated.y > best) {
                best = rotated.y;
                upIndex = index;
            }
        });
        return upIndex;
    }

    /**
     * 读取每枚骰子静止后朝上的面，返回按骰组顺序排列的原始点数。
     * 不修改材质、不旋转、不抬起：骰子落定后画面就是最终结果。
     */
    function readUpFaceValues(): number[] {
        return actors.map((actor) => {
            const geometry = geometryFor(actor.type);
            const upIndex = resultFaceIndex(actor);
            if (upIndex < 0) throw new Error("dice_face_unreadable");
            const value = geometry.faces[upIndex]!.value;
            // d10 面值 0-9；普通（非百分骰）d10 按 1-10 输出，与通用骰表达式一致。
            if (actor.type === "d10" && actor.role !== "tens" && actor.role !== "ones" && actor.role !== "candidate") {
                return value === 0 ? 10 : value;
            }
            return value;
        });
    }

    /**
     * 旧随机数生成器模式：把服务端已生成的结果贴到静止后朝上的面。
     * 新的物理结果模式（传入 seed）不会调用这里，骰面即最终结果。
     */
    function applyLegacyResultFaces(): void {
        actors.forEach((actor) => {
            const upIndex = resultFaceIndex(actor);
            if (upIndex < 0) return;
            const material = actor.materials[upIndex];
            if (!material) return;
            material.map = faceMapFor(actor.type, actor.role, actor.value, material);
        });
    }

    function applySelection(): void {
        const selected = layout.selectedIndex;
        actors.forEach((actor, index) => {
            const isSelected = index === selected;
            actor.materials.forEach((material) => {
                if (selected < 0) {
                    material.transparent = false;
                    material.opacity = 1;
                    material.depthWrite = true;
                    material.emissiveIntensity = 0.06;
                    if (material.map) material.color.set(0xffffff);
                } else if (isSelected) {
                    material.transparent = false;
                    material.opacity = 1;
                    material.depthWrite = true;
                    material.emissiveIntensity = 0.75;
                    if (material.map) material.color.set(0xffffff);
                } else {
                    material.transparent = true;
                    material.opacity = 0.45;
                    material.depthWrite = false;
                    material.emissiveIntensity = 0.02;
                    if (material.map) material.color.set(0x8a8a8a);
                    else material.color.multiplyScalar(0.75);
                }
                material.needsUpdate = true;
            });
        });
        layout.slots.forEach((slot, index) => {
            const actor = actors[index];
            if (actor) actor.mesh.userData.selected = index === selected;
        });
    }

    function disposeActors(): void {
        actors.forEach((actor) => {
            scene.remove(actor.mesh);
            world.removeBody(actor.body);
            actor.materials.forEach((material) => material.dispose());
        });
        actors.length = 0;
    }

    function updateTable(width: number, depth: number): void {
        layout.width = width;
        layout.depth = depth;

        walls.forEach((wall) => world.removeBody(wall));
        walls.length = 0;
        const thickness = 0.4;
        const height = 3;
        const addWall = (x: number, z: number, sizeX: number, sizeZ: number): void => {
            const body = new CANNON.Body({
                mass: 0,
                shape: new CANNON.Box(new CANNON.Vec3(sizeX / 2, height / 2, sizeZ / 2)),
                material: groundMaterial,
            });
            body.position.set(x, height / 2, z);
            world.addBody(body);
            walls.push(body);
        };
        addWall(0, -depth / 2 - thickness / 2, width + thickness * 2, thickness);
        addWall(0, depth / 2 + thickness / 2, width + thickness * 2, thickness);
        addWall(-width / 2 - thickness / 2, 0, thickness, depth + thickness * 2);
        addWall(width / 2 + thickness / 2, 0, thickness, depth + thickness * 2);
    }

    function rebuild(plan: Dice3DPlan, snapshot: Dice3DSnapshot | null): void {
        disposeActors();
        const limit = window.innerWidth < 720 ? 10 : 20;
        const aspect = Math.max(160, container.clientWidth || 360) / Math.max(160, container.clientHeight || 180);
        layout = layoutDice(plan, snapshot, limit, aspect);
        updateTable(layout.width, layout.depth);

        layout.slots.forEach((slot) => {
            const geometry = geometryFor(slot.type);
            const materials = makeMaterials(slot.type, slot.role, geometry);
            const mesh = new THREE.Mesh(geometry.buffer, materials);
            mesh.scale.setScalar(DIE_RADIUS);
            mesh.castShadow = true;
            mesh.receiveShadow = true;
            mesh.position.set(slot.x, geometry.inradius * DIE_RADIUS, slot.z);
            const target = targetQuaternion(faceFor(slot.type, geometry, slot.value));
            mesh.quaternion.copy(target);
            scene.add(mesh);

            const bodyShape = makeBody(slot.type, geometry);
            bodyShape.position.set(slot.x, geometry.inradius * DIE_RADIUS + 2, slot.z);
            bodyShape.quaternion.set(mesh.quaternion.x, mesh.quaternion.y, mesh.quaternion.z, mesh.quaternion.w);
            world.addBody(bodyShape);

            actors.push({
                mesh,
                body: bodyShape,
                materials,
                target,
                type: slot.type,
                role: slot.role,
                value: slot.value,
            });
        });

        updateCamera();
        renderer.render(scene, camera);
    }

    function updateCamera(): void {
        const width = Math.max(160, container.clientWidth || 360);
        const height = Math.max(160, container.clientHeight || 180);
        renderer.setSize(width, height, false);
        const aspect = width / height;
        const viewHeight = Math.max(layout.depth + 1.6, (layout.width + 1.6) / aspect);
        const viewWidth = viewHeight * aspect;
        camera.left = -viewWidth / 2;
        camera.right = viewWidth / 2;
        camera.top = viewHeight / 2;
        camera.bottom = -viewHeight / 2;
        camera.updateProjectionMatrix();

        // 桌面平面始终铺满整个可视窗口，避免骰桌四周出现非桌面的边框/留白。
        table.geometry.dispose();
        table.geometry = new THREE.PlaneGeometry(viewWidth, viewHeight);
        table.position.set(0, 0, 0);

        keyLight.shadow.camera.left = -layout.width;
        keyLight.shadow.camera.right = layout.width;
        keyLight.shadow.camera.top = layout.depth;
        keyLight.shadow.camera.bottom = -layout.depth;
        keyLight.shadow.camera.updateProjectionMatrix();
    }

    function placeStatic(): void {
        actors.forEach((actor) => {
            actor.mesh.quaternion.copy(actor.target);
            actor.body.position.set(actor.mesh.position.x, actor.mesh.position.y, actor.mesh.position.z);
            actor.body.quaternion.set(actor.target.x, actor.target.y, actor.target.z, actor.target.w);
            actor.body.velocity.setZero();
            actor.body.angularVelocity.setZero();
            actor.body.sleep();
        });
        applySelection();
        renderer.render(scene, camera);
    }

    function animateRoll(): Promise<void> {
        let steps = 0;
        let calmFrames = 0;

        return new Promise((resolve) => {
            const step = (): void => {
                if (disposed) {
                    resolve();
                    return;
                }
                // 固定步长、每帧只推进一步：不使用墙钟时间，保证不同客户端
                // 在相同 seed 下推演出完全一致的物理轨迹。
                world.step(1 / 60);
                steps += 1;
                actors.forEach(syncMesh);
                const calm = actors.every((actor) => actor.body.velocity.length() < 0.18
                    && actor.body.angularVelocity.length() < 0.45);
                calmFrames = calm ? calmFrames + 1 : 0;
                if (calmFrames > 18 || steps >= MAX_ROLL_STEPS) {
                    // 骰子已静止：冻结物理，不再旋转、抬起或缩放，直接结束。
                    actors.forEach((actor) => actor.body.sleep());
                    applySelection();
                    renderer.render(scene, camera);
                    resolve();
                    return;
                }
                renderer.render(scene, camera);
                frame = requestAnimationFrame(step);
            };
            frame = requestAnimationFrame(step);
        });
    }

    function roll(snapshot: Dice3DSnapshot, seed?: number | null): Promise<number[]> {
        if (disposed) return Promise.resolve([]);
        rebuild(options.plan, snapshot);
        const seeded = typeof seed === "number" && Number.isFinite(seed);
        const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
        if (reducedMotion) {
            placeStatic();
            if (!seeded) {
                applyLegacyResultFaces();
                renderer.render(scene, camera);
            }
            return Promise.resolve(readUpFaceValues());
        }
        const random = seeded ? mulberry32(seed as number) : Math.random;
        actors.forEach((actor, index) => {
            const slot = layout.slots[index]!;
            // 抖动 + 朝向桌面中心的初速度：所有骰子在同一片桌面里相互碰撞，
            // 而不是每个骰子在自己的小格子里垂直落下。
            actor.body.position.set(
                slot.x + (random() - 0.5) * DIE_RADIUS * 3.2,
                2.1 + random() * 0.9,
                slot.z + (random() - 0.5) * DIE_RADIUS * 2.6,
            );
            actor.body.velocity.set(
                -slot.x * 0.55 + (random() - 0.5) * 2.8,
                -6 - random() * 2,
                -slot.z * 0.55 + (random() - 0.5) * 2.8,
            );
            actor.body.angularVelocity.set((random() - 0.5) * 22, (random() - 0.5) * 22, (random() - 0.5) * 22);
            const orientation = new THREE.Quaternion().setFromEuler(new THREE.Euler(
                random() * Math.PI * 2,
                random() * Math.PI * 2,
                random() * Math.PI * 2,
            ));
            actor.body.quaternion.set(orientation.x, orientation.y, orientation.z, orientation.w);
            actor.body.wakeUp();
        });
        return animateRoll().then(() => {
            if (!seeded) {
                applyLegacyResultFaces();
                // 贴面发生在最后一帧渲染之后，必须补一次渲染才能让旧生成器的结果可见。
                renderer.render(scene, camera);
            }
            return readUpFaceValues();
        });
    }

    /** 仅渲染空骰桌：玩家点击投掷前就能看到完整的桌子。 */
    function showTable(): void {
        if (disposed) return;
        disposeActors();
        layout = { slots: [], width: 6.4, depth: 4.4, selectedIndex: -1 };
        updateTable(layout.width, layout.depth);
        updateCamera();
        renderer.render(scene, camera);
    }

    function setStatic(snapshot: Dice3DSnapshot): void {
        if (disposed) return;
        rebuild(options.plan, snapshot);
        placeStatic();
    }

    /** 把当前画面导出为静态图，供实例数超限后释放 WebGL context 使用。 */
    function freeze(): string | null {
        if (disposed) return null;
        try {
            renderer.render(scene, camera);
            return renderer.domElement.toDataURL("image/png");
        } catch (error) {
            console.warn("dice3d_snapshot_failed", error);
            return null;
        }
    }

    function dispose(): void {
        if (disposed) return;
        disposed = true;
        cancelAnimationFrame(frame);
        disposeActors();
        walls.forEach((wall) => world.removeBody(wall));
        walls.length = 0;
        geometryCache.forEach((geometry) => geometry.buffer.dispose());
        geometryCache.clear();
        ownedTextures.forEach((texture) => texture.dispose());
        ownedTextures.clear();
        textureCache.clear();
        table.geometry.dispose();
        tableMaterial.dispose();
        observer.disconnect();
        renderer.dispose();
        // 主动释放 WebGL context：只 dispose 不会立刻释放，多个骰桌会耗尽浏览器上限。
        try {
            renderer.forceContextLoss();
        } catch {
            // 部分浏览器不支持 lose_context 扩展，忽略即可。
        }
        renderer.domElement.remove();
    }

    const observer = new ResizeObserver(() => {
        updateCamera();
        renderer.render(scene, camera);
    });
    observer.observe(container);

    if (options.tableSkinUrl) {
        const texture = textureLoader.load(options.tableSkinUrl, (loaded) => {
            loaded.colorSpace = THREE.SRGBColorSpace;
            tableMaterial.map = loaded;
            tableMaterial.needsUpdate = true;
            renderer.render(scene, camera);
        }, undefined, () => console.warn("texture_load_failed", options.tableSkinUrl));
        ownedTextures.add(texture);
        tableMaterial.map = texture;
        tableMaterial.color.set(0xffffff);
        tableMaterial.needsUpdate = true;
    }

    showTable();

    return { roll, setStatic, showTable, freeze, dispose };
}

(window as unknown as { TrpgDice3D?: { create: typeof create } }).TrpgDice3D = { create };
