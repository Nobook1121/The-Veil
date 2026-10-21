/**
 * 3D 骰子舞台（独立 bundle）：俯视角桌面 + cannon-es 物理翻滚。
 * 服务端结果是权威值；物理只负责视觉表现，停止后按结果值摆正骰面。
 */
import * as CANNON from "cannon-es";
import * as THREE from "three";

interface Dice3DPlan {
    mode: "percentile" | "standard";
    expression: string;
    count: number;
    sides: number;
    bonus_dice: number;
    penalty_dice: number;
    candidates: number[];
    check?: { name?: string | null; difficulty?: string | null; target?: number | null } | null;
}

interface Dice3DSnapshot {
    faces: number[];
    labels: string[];
    roll?: number | null;
    rolls: number[];
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
    colors?: { body?: string; text?: string; table?: string; edge?: string };
}

interface Dice3DInstance {
    roll(snapshot: Dice3DSnapshot): Promise<void>;
    setStatic(snapshot: Dice3DSnapshot): void;
    dispose(): void;
}

interface DieActor {
    mesh: THREE.Mesh;
    body: CANNON.Body;
    value: number;
    restQuaternion: THREE.Quaternion;
}

const MAX_DICE = 10;
const ROLL_MS = 1500;
const STAGE_HEIGHT = 132;

// 立方体六个面依次为 +x、-x、+y、-y、+z、-z；用旋转把目标面转到朝上。
const UP_ROTATIONS: Record<number, THREE.Euler> = {
    1: new THREE.Euler(0, 0, Math.PI / 2),
    6: new THREE.Euler(0, 0, -Math.PI / 2),
    2: new THREE.Euler(0, 0, 0),
    5: new THREE.Euler(Math.PI, 0, 0),
    3: new THREE.Euler(-Math.PI / 2, 0, 0),
    4: new THREE.Euler(Math.PI / 2, 0, 0),
};

function color(value: string | undefined, fallback: string): string {
    return value && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(value) ? value : fallback;
}

function createGeometry(sides: number): THREE.BufferGeometry {
    switch (sides) {
        case 4: return new THREE.TetrahedronGeometry(0.78);
        case 8: return new THREE.OctahedronGeometry(0.74);
        case 12: return new THREE.DodecahedronGeometry(0.72);
        case 20: return new THREE.IcosahedronGeometry(0.74);
        default: return new THREE.BoxGeometry(1, 1, 1);
    }
}

function createMaterials(geometry: THREE.BufferGeometry, faceUrls: Record<string, string>, bodyColor: string, edgeColor: string, faceKeys: string[]): THREE.Material | THREE.Material[] {
    if (geometry instanceof THREE.BoxGeometry) {
        const loader = new THREE.TextureLoader();
        const loaded = faceKeys.slice(0, 6).map((key) => {
            const url = faceUrls[key];
            if (!url) return null;
            const texture = loader.load(url);
            texture.colorSpace = THREE.SRGBColorSpace;
            return new THREE.MeshStandardMaterial({ map: texture, roughness: 0.62, metalness: 0.08 });
        });
        // 保证始终有 6 个材质，缺失的面使用纯色。
        const materials: THREE.Material[] = [];
        for (let index = 0; index < 6; index += 1) {
            materials.push(loaded[index] || new THREE.MeshStandardMaterial({ color: bodyColor, roughness: 0.55, metalness: 0.12, emissive: edgeColor, emissiveIntensity: 0.08 }));
        }
        return materials;
    }
    return new THREE.MeshStandardMaterial({ color: bodyColor, roughness: 0.5, metalness: 0.16, emissive: edgeColor, emissiveIntensity: 0.12, flatShading: true });
}

function faceKeysFor(mode: string, sides: number): string[] {
    if (mode === "percentile" || sides === 10 || sides === 100) return ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"];
    return Array.from({ length: Math.min(Math.max(sides, 1), 6) }, (_, index) => String(index + 1));
}

function create(container: HTMLElement, options: Dice3DOptions): Dice3DInstance {
    const plan = options.plan;
    const colors = options.colors || {};
    const bodyColor = color(colors.body, "#f6f1e7");
    const edgeColor = color(colors.edge, "#d8b56d");
    const tableColor = color(colors.table, "#1d3a34");

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    if (!renderer.getContext()) throw new Error("webgl_unavailable");
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.innerHTML = "";
    container.appendChild(renderer.domElement);
    renderer.domElement.className = "dice3d-canvas";

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
    camera.position.set(0, 5.4, 4.6);
    camera.lookAt(0, 0, 0);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x333333, 1.15));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
    keyLight.position.set(3, 6, 4);
    scene.add(keyLight);
    const rimLight = new THREE.DirectionalLight(edgeColor, 0.6);
    rimLight.position.set(-4, 3, -3);
    scene.add(rimLight);

    const tableMaterial = new THREE.MeshStandardMaterial({ color: tableColor, roughness: 0.9, metalness: 0.05 });
    const table = new THREE.Mesh(new THREE.PlaneGeometry(16, 10), tableMaterial);
    table.rotation.x = -Math.PI / 2;
    table.position.y = -0.5;
    scene.add(table);

    const world = new CANNON.World({ gravity: new CANNON.Vec3(0, -20, 0) });
    world.broadphase = new CANNON.SAPBroadphase(world);
    world.allowSleep = true;

    const groundMaterial = new CANNON.Material("dice-ground");
    const diceMaterial = new CANNON.Material("dice-body");
    const contact = new CANNON.ContactMaterial(groundMaterial, diceMaterial, { friction: 0.38, restitution: 0.38 });
    world.addContactMaterial(contact);
    const groundBody = new CANNON.Body({ mass: 0, shape: new CANNON.Plane(), material: groundMaterial });
    groundBody.quaternion.setFromEuler(-Math.PI / 2, 0, 0);
    groundBody.position.y = -0.5;
    world.addBody(groundBody);

    const actors: DieActor[] = [];
    let disposed = false;
    let frame = 0;

    function dieCount(snapshot: Dice3DSnapshot | null): number {
        if (plan.mode === "percentile") return 2;
        const count = snapshot?.faces?.length || plan.count || 1;
        return Math.max(1, Math.min(MAX_DICE, Math.round(count)));
    }

    function valuesFor(snapshot: Dice3DSnapshot | null, count: number): number[] {
        const source = (snapshot?.faces?.length ? snapshot.faces : snapshot?.rolls || []).slice(0, count).map((value) => Number(value) || 0);
        while (source.length < count) source.push(1);
        return source;
    }

    function restQuaternionFor(mode: string, sides: number, value: number): THREE.Quaternion {
        if (mode === "percentile" || sides === 10 || sides === 100) {
            // 十面骰用立方体近似，按 0-9 轮转六个面的朝向。
            const face = (Math.abs(Math.round(value)) % 6) + 1;
            return new THREE.Quaternion().setFromEuler(UP_ROTATIONS[face] || UP_ROTATIONS[1]!);
        }
        const face = ((Math.abs(Math.round(value)) - 1) % 6) + 1;
        return new THREE.Quaternion().setFromEuler(UP_ROTATIONS[face] || UP_ROTATIONS[1]!);
    }

    function rebuild(snapshot: Dice3DSnapshot | null): void {
        actors.forEach((actor) => {
            scene.remove(actor.mesh);
            actor.mesh.geometry.dispose();
            const materials = Array.isArray(actor.mesh.material) ? actor.mesh.material : [actor.mesh.material];
            materials.forEach((material) => {
                const map = (material as THREE.MeshStandardMaterial).map;
                map?.dispose();
                material.dispose();
            });
            world.removeBody(actor.body);
        });
        actors.length = 0;

        const sides = Math.max(2, Math.min(100, Math.round(plan.sides || 6)));
        const count = dieCount(snapshot);
        const values = valuesFor(snapshot, count);
        const geometry = createGeometry(plan.mode === "percentile" ? 6 : sides);
        const faceKeys = faceKeysFor(plan.mode, sides);
        const materials = createMaterials(geometry, options.faceUrls || {}, bodyColor, edgeColor, faceKeys);
        const spacing = plan.mode === "percentile" ? 1.35 : Math.max(1.25, 2.4 / Math.max(1, count));

        for (let index = 0; index < count; index += 1) {
            const mesh = new THREE.Mesh(geometry, materials);
            mesh.position.set((index - (count - 1) / 2) * spacing, 0.5, 0);
            mesh.quaternion.copy(restQuaternionFor(plan.mode, sides, values[index] || 1));
            scene.add(mesh);

            const body = new CANNON.Body({
                mass: 1,
                shape: new CANNON.Box(new CANNON.Vec3(0.5, 0.5, 0.5)),
                material: diceMaterial,
                linearDamping: 0.18,
                angularDamping: 0.22,
                allowSleep: true,
                sleepSpeedLimit: 0.22,
                sleepTimeLimit: 0.35,
            });
            body.position.set(mesh.position.x, 0.5, 0);
            world.addBody(body);

            actors.push({
                mesh,
                body,
                value: values[index] || 1,
                restQuaternion: restQuaternionFor(plan.mode, sides, values[index] || 1),
            });
        }
    }

    function resize(): void {
        const width = Math.max(120, container.clientWidth || 320);
        renderer.setSize(width, STAGE_HEIGHT, false);
        camera.aspect = width / STAGE_HEIGHT;
        camera.updateProjectionMatrix();
        renderer.render(scene, camera);
    }

    function syncMeshes(): void {
        actors.forEach((actor) => {
            actor.mesh.position.set(actor.body.position.x, actor.body.position.y, actor.body.position.z);
            actor.mesh.quaternion.set(actor.body.quaternion.x, actor.body.quaternion.y, actor.body.quaternion.z, actor.body.quaternion.w);
        });
    }

    function snapToRest(): void {
        const count = actors.length;
        actors.forEach((actor, index) => {
            const targetX = (index - (count - 1) / 2) * (plan.mode === "percentile" ? 1.35 : Math.max(1.25, 2.4 / Math.max(1, count)));
            actor.mesh.position.set(targetX, 0, 0);
            actor.mesh.quaternion.copy(actor.restQuaternion);
            actor.body.position.set(targetX, 0.5, 0);
            actor.body.quaternion.set(actor.mesh.quaternion.x, actor.mesh.quaternion.y, actor.mesh.quaternion.z, actor.mesh.quaternion.w);
            actor.body.velocity.setZero();
            actor.body.angularVelocity.setZero();
        });
        renderer.render(scene, camera);
    }

    function roll(snapshot: Dice3DSnapshot): Promise<void> {
        if (disposed) return Promise.resolve();
        rebuild(snapshot);
        actors.forEach((actor, index) => {
            actor.body.position.set((index - (actors.length - 1) / 2) * 1.1, 2.2 + index * 0.05, 0);
            actor.body.velocity.set((Math.random() - 0.5) * 4, 2.2 + Math.random(), (Math.random() - 0.5) * 3);
            actor.body.angularVelocity.set((Math.random() - 0.5) * 14, (Math.random() - 0.5) * 14, (Math.random() - 0.5) * 14);
            actor.body.wakeUp();
        });

        const started = performance.now();
        return new Promise((resolve) => {
            const step = (): void => {
                if (disposed) {
                    resolve();
                    return;
                }
                const elapsed = performance.now() - started;
                const stillRunning = elapsed < ROLL_MS;
                if (stillRunning) {
                    world.step(1 / 60, 1 / 120, 4);
                    syncMeshes();
                } else {
                    snapToRest();
                }
                renderer.render(scene, camera);
                if (stillRunning) {
                    frame = requestAnimationFrame(step);
                } else {
                    resolve();
                }
            };
            frame = requestAnimationFrame(step);
        });
    }

    function setStatic(snapshot: Dice3DSnapshot): void {
        if (disposed) return;
        rebuild(snapshot);
        snapToRest();
    }

    function dispose(): void {
        if (disposed) return;
        disposed = true;
        cancelAnimationFrame(frame);
        actors.forEach((actor) => {
            scene.remove(actor.mesh);
            actor.mesh.geometry.dispose();
            const materials = Array.isArray(actor.mesh.material) ? actor.mesh.material : [actor.mesh.material];
            materials.forEach((material) => {
                (material as THREE.MeshStandardMaterial).map?.dispose();
                material.dispose();
            });
            world.removeBody(actor.body);
        });
        actors.length = 0;
        observer.disconnect();
        renderer.dispose();
        renderer.domElement.remove();
        container.innerHTML = "";
    }

    const observer = new ResizeObserver(() => resize());
    observer.observe(container);
    resize();
    setStatic({ faces: [], labels: [], rolls: [] });

    return { roll, setStatic, dispose };
}

(window as unknown as { TrpgDice3D?: { create: typeof create } }).TrpgDice3D = { create };
