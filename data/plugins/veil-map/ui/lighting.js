/**
 * veil-map 光照层（纯 canvas 逻辑，不碰房间/权限/接口）：
 *
 *   map.js（世界坐标：光源 / 墙体 / 棋子）──▶ VeilLighting.render() ──▶ 主画布
 *
 * 渲染管线：
 *   1) 光源筛选：视口裁剪 → 大半径优先 → 只留前 MAX_LIGHTS 个；
 *   2) 离屏光照层：半分辨率画布，逐个光源以 globalCompositeOperation="lighter"
 *      叠加「多段柔和衰减 + 墙体可见多边形裁剪 + 锥形光角度羽化」；
 *   3) 合成：整层一次性 lighter 贴回主画布。此前是每个光源各画一个
 *      半透明 radialGradient 直接叠在底图上，多个光源重叠时会互相「兑灰」、
 *      明暗界出现硬圈，颜色也几乎看不出来；先合成后一次性叠加能保留色相与
 *      高光核心。
 *   4) 暗视：token 明确带 darkvision 时，在该 token 的视野多边形内做去饱和 +
 *      冷调提亮（不伪造：没有 darkvision 数据就不画）。
 *
 * 墙体遮挡（视线投影）按「以光源为中心的可见多边形」实现：角向扫描所有候选
 * 墙段端点，向每个角度投射射线求最近交点。多边形按「光源位置 + 半径 + 墙体
 * 指纹」缓存，墙体不变时整帧零投影开销（房间级缓存）；墙体/光源变动只让相关
 * 光源失效。单个光源最多对最近 MAX_OCCLUDERS 段墙做投影。
 */
(function (global) {
    "use strict";

    var TAU = Math.PI * 2;

    // 光照层分辨率：0.5 = 半分辨率离屏渲染后再放大（柔化 + 省一半像素）。
    var LIGHT_LAYER_SCALE = 0.5;
    // 单帧最多参与合成的光源数（按覆盖半径挑最大的若干个）。
    var MAX_LIGHTS = 24;
    // 单个光源最多参与的墙体段数（离光源最近的这些）。
    var MAX_OCCLUDERS = 80;
    // 可见多边形缓存条数（光源数上限的 2~3 倍，够覆盖拖拽时的历史位置）。
    var POLYGON_CACHE_LIMIT = 72;
    // 一帧里多边形重算的时间预算：超预算的光源先用上一帧的多边形，下一帧补齐。
    var POLYGON_BUDGET_MS = 4;
    // 光斑贴图边长（px）与缓存条数：颜色 × 峰值 × 亮界 两两组合，同场景通常
    // 只有个位数，缓存几十张足够。
    var SPRITE_SIZE = 256;
    var SPRITE_CACHE_LIMIT = 48;
    // 射线角度抖动（弧度）：端点前后各取一个角度，才能看到「墙角后面」。
    var ANGLE_EPSILON = 1e-4;

    // 亮度曲线：FVTT / UVTT 的 alpha（intensity）常在 0.05~0.2，
    // 直接当不透明度画会几乎看不见（酒馆场景 31 个光源都是 0.08）。
    // 这里给一个下限再按 sqrt 放大，既保留强弱差别又让火把真的照亮桌面。
    var STRENGTH_FLOOR = 0.5;
    // 单个光源中心的叠加峰值（alpha）；多个光源 lighter 叠加会自然过曝成
    // 暖白核心，所以单灯峰值不宜过高。
    var PEAK_ALPHA = 0.2;

    // 锥形光边缘羽化：三档由外到内收窄，alpha 累加为核心 1.0，
    // 最外 1 档（+2 段羽化角）只剩 0.25，于是边界是渐隐而不是硬切。
    var CONE_FEATHER_DEG = 5;
    var CONE_BANDS = [
        { widen: 2, alpha: 0.25 },
        { widen: 1, alpha: 0.33 },
        { widen: 0, alpha: 0.42 }
    ];

    function timeNow() {
        if (global.performance && typeof global.performance.now === "function") {
            return global.performance.now();
        }
        return Date.now();
    }

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function parseColor(value) {
        var text = String(value || "").replace("#", "");
        if (!/^[0-9a-fA-F]{6}$/.test(text)) text = "ffaa00";
        return {
            r: parseInt(text.slice(0, 2), 16),
            g: parseInt(text.slice(2, 4), 16),
            b: parseInt(text.slice(4, 6), 16)
        };
    }

    function rgba(color, alpha) {
        return "rgba(" + color.r + ", " + color.g + ", " + color.b + ", " +
            clamp(alpha, 0, 1).toFixed(3) + ")";
    }

    /** intensity → 0..1 的亮度权重；0 表示这个光源根本不亮。 */
    function lightStrength(intensity) {
        var raw = Number(intensity);
        if (!isFinite(raw) || raw <= 0) return 0;
        return clamp(STRENGTH_FLOOR + (1 - STRENGTH_FLOOR) * Math.sqrt(clamp(raw, 0, 1)), 0, 1);
    }

    // ------------------------------------------------------------------
    // 墙体指纹 / 候选墙段
    // ------------------------------------------------------------------

    function mix(hash, value) {
        hash = (hash ^ (value | 0)) >>> 0;
        return ((hash * 16777619) >>> 0);
    }

    /**
     * 墙体几何指纹：只关心影响遮挡的部分（端点、是否挡光、门是否开着）。
     * 指纹不变 → 所有光源的可见多边形缓存继续有效（房间级缓存）。
     */
    function wallFingerprint(walls) {
        var hash = 2166136261;
        var blockedCode = 0;
        hash = mix(hash, walls.length);
        for (var index = 0; index < walls.length; index += 1) {
            var wall = walls[index] || {};
            var points = Array.isArray(wall.points) ? wall.points : [];
            hash = mix(hash, points.length);
            for (var pointIndex = 0; pointIndex < points.length; pointIndex += 1) {
                var point = points[pointIndex] || {};
                hash = mix(hash, Math.round((Number(point.x) || 0) * 10));
                hash = mix(hash, Math.round((Number(point.y) || 0) * 10));
            }
            blockedCode = wall.blocks_light === false ? 1 : 0;
            if (String(wall.kind || "") === "door") {
                blockedCode = String(wall.state || "closed") === "open" ? 2 : 3;
            }
            hash = mix(hash, blockedCode);
        }
        return String(hash >>> 0);
    }

    /** 挡不挡光：显式 blocks_light=false（窗）与开着的门不挡。 */
    function blocksLight(wall) {
        if (!wall) return false;
        if (wall.blocks_light === false) return false;
        if (String(wall.kind || "") === "door" && String(wall.state || "closed") === "open") return false;
        return true;
    }

    function pointSegmentDistance(cx, cy, ax, ay, bx, by) {
        var dx = bx - ax;
        var dy = by - ay;
        var lengthSq = dx * dx + dy * dy;
        var t = lengthSq > 1e-9 ? ((cx - ax) * dx + (cy - ay) * dy) / lengthSq : 0;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        var px = ax + dx * t - cx;
        var py = ay + dy * t - cy;
        return Math.sqrt(px * px + py * py);
    }

    /**
     * 光源附近真正可能挡光的墙段：先按「到光源距离 ≤ 半径」粗筛，再取最近
     * MAX_OCCLUDERS 段。半径外的墙对光斑形状没有影响，不参与投影。
     */
    function collectOccluders(walls, cx, cy, radius) {
        var found = [];
        for (var index = 0; index < walls.length; index += 1) {
            var wall = walls[index];
            if (!blocksLight(wall)) continue;
            var points = Array.isArray(wall.points) ? wall.points : [];
            for (var pointIndex = 1; pointIndex < points.length; pointIndex += 1) {
                var from = points[pointIndex - 1] || {};
                var to = points[pointIndex] || {};
                var ax = Number(from.x) || 0;
                var ay = Number(from.y) || 0;
                var bx = Number(to.x) || 0;
                var by = Number(to.y) || 0;
                var distance = pointSegmentDistance(cx, cy, ax, ay, bx, by);
                if (distance > radius) continue;
                found.push({ ax: ax, ay: ay, bx: bx, by: by, distance: distance });
            }
        }
        if (found.length > MAX_OCCLUDERS) {
            found.sort(function (left, right) { return left.distance - right.distance; });
            found.length = MAX_OCCLUDERS;
        }
        return found;
    }

    // ------------------------------------------------------------------
    // 可见多边形（视线投影）
    // ------------------------------------------------------------------

    /**
     * 以 (cx, cy) 为中心、radius 为半径的可见多边形。
     *
     * 经典角向扫描：所有墙段端点各自取「角度 ± ε」两个方向投射射线，取射线与
     * 最近墙段/半径圆的交点，按角度排序即为可见区域边界。ε 让射线分别落在
     * 端点两侧，于是墙角背后也能正确留出阴影；多个端点角度近似相同时合并。
     *
     * 返回扁平数组 [x0, y0, x1, y1, …]（世界坐标），墙段极近时返回 null。
     */
    function visiblePolygon(cx, cy, radius, segments) {
        if (!segments.length || !(radius > 0)) return null;
        var angles = [];
        var index;
        for (index = 0; index < segments.length; index += 1) {
            var segment = segments[index];
            var first = Math.atan2(segment.ay - cy, segment.ax - cx);
            var second = Math.atan2(segment.by - cy, segment.bx - cx);
            angles.push(first - ANGLE_EPSILON, first + ANGLE_EPSILON,
                second - ANGLE_EPSILON, second + ANGLE_EPSILON);
        }
        angles.sort(function (left, right) { return left - right; });

        var points = [];
        var previousAngle = NaN;
        for (index = 0; index < angles.length; index += 1) {
            var angle = angles[index];
            if (isFinite(previousAngle) && Math.abs(angle - previousAngle) < 1e-9) continue;
            previousAngle = angle;
            var dx = Math.cos(angle);
            var dy = Math.sin(angle);
            var best = radius;
            for (var slot = 0; slot < segments.length; slot += 1) {
                var item = segments[slot];
                var ex = item.bx - item.ax;
                var ey = item.by - item.ay;
                var denominator = dx * ey - dy * ex;
                if (Math.abs(denominator) < 1e-9) continue;
                var wx = item.ax - cx;
                var wy = item.ay - cy;
                var t = (wx * ey - wy * ex) / denominator;
                if (t <= 1e-6 || t >= best) continue;
                var u = (wx * dy - wy * dx) / denominator;
                if (u < -1e-6 || u > 1 + 1e-6) continue;
                best = t;
            }
            points.push(cx + dx * best, cy + dy * best);
        }
        return points.length >= 6 ? points : null;
    }

    // ------------------------------------------------------------------
    // 引擎
    // ------------------------------------------------------------------

    function createEngine() {
        var layerCanvas = null;
        var layerContext = null;
        var polygonCache = Object.create(null);
        var polygonKeys = [];
        var lastPolygon = Object.create(null);
        var spriteCache = Object.create(null);
        var spriteKeys = [];
        // 当前帧视口（CSS 像素）：每个光源按它裁剪，半径远大于视口的大光源
        // 不会去光栅化画布外的像素。
        var viewport = { width: 1, height: 1 };
        var stats = {
            lights: 0,
            drawn: 0,
            occluders: 0,
            polygons: 0,
            computed: 0,
            reused: 0,
            deferred: 0,
            darkvision: 0,
            ms: 0
        };

        function cacheGet(key) {
            // 注意：缓存值可以是 null（「这个光源没有被任何墙挡住」也是结论），
            // 所以命中判定用 hasOwnProperty，不能用真值判断。
            if (!Object.prototype.hasOwnProperty.call(polygonCache, key)) return undefined;
            return polygonCache[key];
        }

        function cachePut(key, value) {
            if (!polygonCache[key]) {
                polygonKeys.push(key);
                if (polygonKeys.length > POLYGON_CACHE_LIMIT) {
                    var evicted = polygonKeys.shift();
                    delete polygonCache[evicted];
                }
            }
            polygonCache[key] = value;
        }

        function resetCache() {
            polygonCache = Object.create(null);
            polygonKeys = [];
            lastPolygon = Object.create(null);
            spriteCache = Object.create(null);
            spriteKeys = [];
        }

        /**
         * 取（必要时计算）某个光源的可见多边形。
         * 墙体指纹与光源位置都进 key：墙不动、光不动 → 永久命中。
         */
        function polygonFor(target, walls, fingerprint, deadline) {
            var key = (target.id || "light") + "|" +
                Math.round(target.x) + "," + Math.round(target.y) + "|" +
                Math.round(target.radiusWorld) + "|" + fingerprint;
            var cached = cacheGet(key);
            if (cached !== undefined) {
                stats.reused += 1;
                return cached;
            }
            // 超预算：沿用同一光源上一次的多边形（墙刚改时形状只是略旧），
            // 本帧不会出现「光突然穿墙」，下一帧再补齐。
            if (timeNow() > deadline) {
                stats.deferred += 1;
                var previous = lastPolygon[target.id || "light"];
                return previous || null;
            }
            var segments = collectOccluders(walls, target.x, target.y, target.radiusWorld + 1);
            stats.occluders = Math.max(stats.occluders, segments.length);
            var entry = {
                polygon: visiblePolygon(target.x, target.y, target.radiusWorld, segments),
                // 屏幕多边形只在相机（缩放/平移）变化时重建。
                pathSignature: "",
                path: null
            };
            stats.computed += 1;
            cachePut(key, entry);
            lastPolygon[target.id || "light"] = entry;
            return entry;
        }

        function ensureLayer(width, height) {
            if (!layerCanvas) {
                layerCanvas = document.createElement("canvas");
                layerContext = layerCanvas.getContext("2d");
            }
            if (!layerContext) return null;
            // 半分辨率：像素量降到 1/4，再靠放大 + 平滑补回柔边。
            var targetWidth = Math.max(1, Math.ceil(width * LIGHT_LAYER_SCALE));
            var targetHeight = Math.max(1, Math.ceil(height * LIGHT_LAYER_SCALE));
            if (layerCanvas.width !== targetWidth || layerCanvas.height !== targetHeight) {
                layerCanvas.width = targetWidth;
                layerCanvas.height = targetHeight;
            }
            layerContext.setTransform(1, 0, 0, 1, 0, 0);
            layerContext.globalCompositeOperation = "source-over";
            layerContext.globalAlpha = 1;
            layerContext.clearRect(0, 0, targetWidth, targetHeight);
            // 之后所有绘制都用「CSS 像素」坐标，缩放交给 transform。
            layerContext.setTransform(LIGHT_LAYER_SCALE, 0, 0, LIGHT_LAYER_SCALE, 0, 0);
            layerContext.globalCompositeOperation = "lighter";
            layerContext.imageSmoothingEnabled = true;
            return layerContext;
        }

        /**
         * 光斑贴图：把「多段衰减」预先烘焙成一张小圆图，按光源颜色/峰值/亮界
         * 缓存（同场景通常只有几种组合）。每帧只做一次缩放贴图，而不是为
         * 每个光源重新光栅化一次大径向渐变——软件光栅化下这是主要开销。
         */
        function lightSprite(color, peak, brightRatio) {
            var peakStep = Math.round(peak * 200);
            var brightStep = Math.round(clamp(brightRatio, 0.05, 1) * 20);
            var key = color.r + ":" + color.g + ":" + color.b + ":" + peakStep + ":" + brightStep;
            var sprite = spriteCache[key];
            if (sprite) return sprite;
            var canvas = document.createElement("canvas");
            canvas.width = SPRITE_SIZE;
            canvas.height = SPRITE_SIZE;
            var layer = canvas.getContext("2d");
            if (!layer) return null;
            var radius = SPRITE_SIZE / 2;
            var alpha = peakStep / 200;
            var bright = clamp(brightStep / 20, 0.05, 0.82);
            var gradient = layer.createRadialGradient(radius, radius, 0, radius, radius, radius);
            // 多段衰减：内圈 1.0 → 亮界 ~0.85 → 昏暗界 ~0.35 → 0。
            gradient.addColorStop(0, rgba(color, alpha));
            gradient.addColorStop(bright * 0.55, rgba(color, alpha * 0.97));
            gradient.addColorStop(bright, rgba(color, alpha * 0.85));
            gradient.addColorStop(0.9, rgba(color, alpha * 0.35));
            gradient.addColorStop(1, rgba(color, 0));
            layer.fillStyle = gradient;
            layer.beginPath();
            layer.arc(radius, radius, radius, 0, TAU);
            layer.fill();
            spriteCache[key] = canvas;
            spriteKeys.push(key);
            if (spriteKeys.length > SPRITE_CACHE_LIMIT) delete spriteCache[spriteKeys.shift()];
            return canvas;
        }

        /** 可见多边形 → 屏幕路径；相机没动就复用上一帧的 Path2D。 */
        function pathFor(entry, scale, tx, ty) {
            if (!entry || !entry.polygon) return null;
            var signature = scale.toFixed(4) + "|" + Math.round(tx * 2) + "|" + Math.round(ty * 2);
            if (entry.pathSignature === signature) return entry.path;
            entry.path = polygonPath(entry.polygon, scale, tx, ty);
            entry.pathSignature = signature;
            return entry.path;
        }

        function polygonPath(points, scale, tx, ty) {
            if (!points) return null;
            var screen = [];
            var lastX = NaN;
            var lastY = NaN;
            var index;
            for (index = 0; index < points.length; index += 2) {
                var x = points[index] * scale + tx;
                var y = points[index + 1] * scale + ty;
                // 亚像素合并：角点周围的一串微小边不参与描边，省一半顶点。
                if (Math.abs(x - lastX) < 0.6 && Math.abs(y - lastY) < 0.6) continue;
                screen.push(x, y);
                lastX = x;
                lastY = y;
            }
            if (screen.length < 6) return null;
            if (typeof global.Path2D === "function") {
                var path = new global.Path2D();
                path.moveTo(screen[0], screen[1]);
                for (index = 2; index < screen.length; index += 2) {
                    path.lineTo(screen[index], screen[index + 1]);
                }
                path.closePath();
                return path;
            }
            return screen;
        }

        function applyShape(layer, target, path) {
            // 墙体可见多边形 → 锥形光角度 → 圆盘。裁剪逐层收窄。
            if (global.Path2D && path instanceof global.Path2D) {
                layer.clip(path);
            } else if (Array.isArray(path)) {
                layer.beginPath();
                layer.moveTo(path[0], path[1]);
                for (var index = 2; index < path.length; index += 2) {
                    layer.lineTo(path[index], path[index + 1]);
                }
                layer.closePath();
                layer.clip();
            }
            if (target.cone) {
                layer.beginPath();
                layer.moveTo(target.sx, target.sy);
                layer.arc(target.sx, target.sy, target.radiusPx + 1, target.cone.start, target.cone.end);
                layer.closePath();
                layer.clip();
            }
        }

        /** 把光斑贴图按光源半径缩放着色画到光照层上（已被裁剪到锥形/可见区）。 */
        function blitLight(layer, target, sprite) {
            var radius = target.radiusPx + 1;
            layer.drawImage(sprite, target.sx - radius, target.sy - radius, radius * 2, radius * 2);
        }

        function clipViewport(layer) {
            // 大半径光源（例如 100ft 的魔法光）在视口里只占一部分：先按视口裁掉
            // 画布外的像素，别去光栅化它们。
            layer.beginPath();
            layer.rect(0, 0, viewport.width, viewport.height);
            layer.clip();
        }

        function paintLight(layer, target, walls, fingerprint, deadline, useShadows) {
            var path = null;
            if (useShadows && walls.length) {
                var entry = polygonFor(target, walls, fingerprint, deadline);
                if (entry && entry.polygon) {
                    path = pathFor(entry, target.scale, target.tx, target.ty);
                    stats.polygons += 1;
                }
            }
            var sprite = lightSprite(target.color, target.peak, target.brightRatio);
            if (!sprite) return;
            if (!target.cone) {
                layer.save();
                clipViewport(layer);
                if (path) applyShape(layer, target, path);
                blitLight(layer, target, sprite);
                layer.restore();
                return;
            }
            // 锥形光：三档由宽到窄叠三次，边界羽化 CONE_FEATHER_DEG×2 度。
            for (var bandIndex = 0; bandIndex < CONE_BANDS.length; bandIndex += 1) {
                var band = CONE_BANDS[bandIndex];
                var widen = CONE_FEATHER_DEG * band.widen * Math.PI / 180;
                layer.save();
                clipViewport(layer);
                if (path) applyShape(layer, target, path);
                layer.beginPath();
                layer.moveTo(target.sx, target.sy);
                layer.arc(target.sx, target.sy, target.radiusPx + 1,
                    target.cone.start - widen, target.cone.end + widen);
                layer.closePath();
                layer.clip();
                layer.globalAlpha = band.alpha;
                blitLight(layer, target, sprite);
                layer.restore();
            }
        }

        function normalizeLight(raw, scale, tx, ty, grid, width, height) {
            if (!raw) return null;
            var brightCells = Math.max(0, Number(raw.bright_cells) || 0);
            var dimCells = Math.max(0, Number(raw.dim_cells) || 0);
            if (dimCells < brightCells) dimCells = brightCells;
            if (dimCells <= 0) return null;
            var strength = lightStrength(raw.intensity);
            if (strength <= 0) return null;
            var radiusWorld = dimCells * grid;
            var radiusPx = radiusWorld * scale;
            if (radiusPx < 2) return null;
            var worldX = Number(raw.x) || 0;
            var worldY = Number(raw.y) || 0;
            var sx = worldX * scale + tx;
            var sy = worldY * scale + ty;
            if (sx + radiusPx < -4 || sx - radiusPx > width + 4) return null;
            if (sy + radiusPx < -4 || sy - radiusPx > height + 4) return null;
            var angle = Number(raw.angle);
            if (!isFinite(angle) || angle <= 0) angle = 360;
            angle = clamp(angle, 0, 360);
            var target = {
                id: String(raw.id || "light"),
                x: worldX,
                y: worldY,
                sx: sx,
                sy: sy,
                scale: scale,
                tx: tx,
                ty: ty,
                radiusWorld: radiusWorld,
                radiusPx: radiusPx,
                brightRatio: brightCells > 0 ? clamp(brightCells / dimCells, 0.05, 1) : 0.35,
                peak: PEAK_ALPHA * strength,
                color: parseColor(raw.color),
                onScreen: sx >= 0 && sx <= width && sy >= 0 && sy <= height ? 1 : 0,
                cone: null,
                polygon: null
            };
            if (angle < 359.5) {
                var rotation = Number(raw.rotation);
                if (!isFinite(rotation)) rotation = 0;
                var center = ((rotation % 360) + 360) % 360 * Math.PI / 180;
                var half = angle * Math.PI / 360;
                target.cone = { start: center - half, end: center + half };
            }
            return target;
        }

        /**
         * 画一层光照。
         *
         * @param {CanvasRenderingContext2D} context 目标画布（CSS 像素坐标系）
         * @param {object} options
         *        width/height  视口 CSS 像素尺寸
         *        scale         世界 → 屏幕缩放（px/世界 px）
         *        tx/ty         世界原点在屏幕上的位置（世界*scale + t = 屏幕）
         *        grid          每格世界尺寸（px）
         *        lights        [{id,x,y,bright_cells,dim_cells,color,intensity,angle,rotation,shadows}]
         *        walls         [{points:[{x,y}],kind,state,blocks_light}]
         *        darkvision    [{id,x,y,radius_cells}]（可选，已有暗视数据的 token）
         */
        function render(context, options) {
            var started = timeNow();
            stats.lights = 0;
            stats.drawn = 0;
            stats.occluders = 0;
            stats.polygons = 0;
            stats.computed = 0;
            stats.reused = 0;
            stats.deferred = 0;
            stats.darkvision = 0;
            stats.ms = 0;
            if (!context || !options) return stats;

            var width = Math.max(1, Math.round(Number(options.width) || 0));
            var height = Math.max(1, Math.round(Number(options.height) || 0));
            var scale = Number(options.scale) > 0 ? Number(options.scale) : 1;
            var tx = Number(options.tx) || 0;
            var ty = Number(options.ty) || 0;
            var grid = Math.max(1, Number(options.grid) || 1);
            var walls = Array.isArray(options.walls) ? options.walls : [];
            var source = Array.isArray(options.lights) ? options.lights : [];

            var targets = [];
            for (var index = 0; index < source.length; index += 1) {
                var target = normalizeLight(source[index], scale, tx, ty, grid, width, height);
                if (target) targets.push(target);
            }
            stats.lights = targets.length;
            targets.sort(function (left, right) {
                if (left.onScreen !== right.onScreen) return right.onScreen - left.onScreen;
                return right.radiusPx - left.radiusPx;
            });
            if (targets.length > MAX_LIGHTS) {
                targets.length = MAX_LIGHTS;
                stats.lights = MAX_LIGHTS;
            }

            var darkvision = Array.isArray(options.darkvision) ? options.darkvision : [];
            if (!targets.length && !darkvision.length) {
                stats.ms = timeNow() - started;
                return stats;
            }

            var fingerprint = wallFingerprint(walls);
            var deadline = started + POLYGON_BUDGET_MS;
            if (targets.length) {
                var layer = ensureLayer(width, height);
                if (layer) {
                    for (index = 0; index < targets.length; index += 1) {
                        var item = targets[index];
                        var useShadows = item.shadows !== false && options.shadows !== false;
                        paintLight(layer, item, walls, fingerprint, deadline, useShadows);
                        stats.drawn += 1;
                    }
                    context.save();
                    context.globalCompositeOperation = "lighter";
                    context.imageSmoothingEnabled = true;
                    context.drawImage(
                        layerCanvas,
                        0, 0, layerCanvas.width, layerCanvas.height,
                        0, 0, width, height
                    );
                    context.restore();
                }
            }
            if (darkvision.length) {
                stats.darkvision = renderDarkvision(context, {
                    darkvision: darkvision,
                    walls: walls,
                    scale: scale,
                    tx: tx,
                    ty: ty,
                    grid: grid
                });
            }
            stats.ms = timeNow() - started;
            return stats;
        }

        /**
         * 暗视（夜视）：只对明确带暗视数据的 token 生效 —— 在其视野多边形内
         * 先去饱和、再冷调提亮，做出「灰白视界」而不是简单加亮。
         */
        function renderDarkvision(context, options) {
            var list = Array.isArray(options && options.darkvision) ? options.darkvision : [];
            if (!list.length) return 0;
            var scale = Number(options.scale) > 0 ? Number(options.scale) : 1;
            var tx = Number(options.tx) || 0;
            var ty = Number(options.ty) || 0;
            var grid = Math.max(1, Number(options.grid) || 1);
            var walls = Array.isArray(options.walls) ? options.walls : [];
            var fingerprint = wallFingerprint(walls);
            var deadline = timeNow() + POLYGON_BUDGET_MS;
            var painted = 0;
            for (var index = 0; index < list.length; index += 1) {
                var raw = list[index] || {};
                var radiusCells = Math.max(0, Number(raw.radius_cells) || 0);
                if (radiusCells <= 0) continue;
                var target = {
                    id: "darkvision:" + String(raw.id || ""),
                    x: Number(raw.x) || 0,
                    y: Number(raw.y) || 0,
                    radiusWorld: radiusCells * grid
                };
                var entry = polygonFor(target, walls, fingerprint, deadline);
                var points = entry && entry.polygon ? entry.polygon : null;
                if (!points) {
                    // 没有墙也照样提亮：用圆盘代替多边形。
                    var cx = target.x * scale + tx;
                    var cy = target.y * scale + ty;
                    var radius = target.radiusWorld * scale;
                    if (radius < 2) continue;
                    context.save();
                    context.beginPath();
                    context.arc(cx, cy, radius, 0, TAU);
                    context.clip();
                    paintDarkvision(context, cx - radius, cy - radius, radius * 2, radius * 2);
                    context.restore();
                    painted += 1;
                    continue;
                }
                // 缓存条目统一走 pathFor（按相机签名复用 Path2D），别直接
                // 把数组当 polygon 传进 polygonPath。
                var path = pathFor(entry, scale, tx, ty);
                if (!path) continue;
                var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
                for (var slot = 0; slot < points.length; slot += 2) {
                    var px = points[slot] * scale + tx;
                    var py = points[slot + 1] * scale + ty;
                    if (px < minX) minX = px;
                    if (px > maxX) maxX = px;
                    if (py < minY) minY = py;
                    if (py > maxY) maxY = py;
                }
                context.save();
                if (global.Path2D && path instanceof global.Path2D) {
                    context.clip(path);
                } else if (Array.isArray(path)) {
                    context.beginPath();
                    context.moveTo(path[0], path[1]);
                    for (var pointIndex = 2; pointIndex < path.length; pointIndex += 2) {
                        context.lineTo(path[pointIndex], path[pointIndex + 1]);
                    }
                    context.closePath();
                    context.clip();
                }
                paintDarkvision(context, minX, minY, maxX - minX, maxY - minY);
                context.restore();
                painted += 1;
            }
            return painted;
        }

        function paintDarkvision(context, x, y, width, height) {
            if (!(width > 0 && height > 0)) return;
            // saturation 混合把目标色相归零（保留亮度），再用冷灰 lighter 提亮。
            context.globalCompositeOperation = "saturation";
            context.fillStyle = "rgba(128, 128, 128, 1)";
            context.fillRect(x, y, width, height);
            context.globalCompositeOperation = "lighter";
            context.fillStyle = "rgba(96, 118, 150, 0.16)";
            context.fillRect(x, y, width, height);
            context.globalCompositeOperation = "source-over";
        }

        return {
            render: render,
            renderDarkvision: renderDarkvision,
            stats: function () {
                return stats;
            },
            clearCache: resetCache
        };
    }

    global.VeilLighting = {
        createEngine: createEngine,
        lightStrength: lightStrength,
        visiblePolygon: visiblePolygon,
        wallFingerprint: wallFingerprint,
        limits: {
            maxLights: MAX_LIGHTS,
            maxOccluders: MAX_OCCLUDERS,
            layerScale: LIGHT_LAYER_SCALE,
            peakAlpha: PEAK_ALPHA
        }
    };
})(typeof window !== "undefined" ? window : this);
