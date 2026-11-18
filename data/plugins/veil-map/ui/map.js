/**
 * veil-map 插件前端：棋子绘制、拖拽、网格吸附、多格占位、先攻回合条与
 * 房主地图编辑。核心只提供 canvas 宿主与相机/事件通道，本文件自行请求
 * 插件自己的 HTTP 接口，并通过房间广播同步。
 */
(function () {
    "use strict";

    var API_BASE = "/api/plugins/veil-map";
    var FEET_PER_CELL = 5;
    var MIN_GRID = 16;
    var MAX_GRID = 256;
    var MIN_BOUNDS = 5;
    var MAX_BOUNDS = 200;
    var DEFAULT_BOUNDS = 50;
    var POLL_INTERVAL = 12000;

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function asInt(value, fallback) {
        var number = parseInt(value, 10);
        return Number.isFinite(number) ? number : fallback;
    }

    function hexToRgba(value, alpha) {
        var text = String(value || "").replace("#", "");
        if (!/^[0-9a-fA-F]{6}$/.test(text)) text = "ffaa00";
        var red = parseInt(text.slice(0, 2), 16);
        var green = parseInt(text.slice(2, 4), 16);
        var blue = parseInt(text.slice(4, 6), 16);
        return "rgba(" + red + ", " + green + ", " + blue + ", " + clamp(alpha, 0, 1) + ")";
    }

    function apiRequest(method, path, body) {
        var api = window.TrpgApi;
        if (api) {
            if (method === "GET") return api.get(API_BASE + path);
            if (method === "PUT") return api.put(API_BASE + path, body);
            if (method === "POST") return api.post(API_BASE + path, body);
            if (method === "DELETE") return api.del(API_BASE + path);
        }
        var isForm = typeof FormData !== "undefined" && body instanceof FormData;
        var options = { method: method, headers: { Accept: "application/json" } };
        var csrf = window.currentUser && window.currentUser.csrf_token;
        if (csrf && method !== "GET") options.headers["X-CSRF-Token"] = csrf;
        if (isForm) {
            options.body = body;
        } else if (body !== undefined) {
            options.headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }
        return fetch(API_BASE + path, options).then(function (response) {
            return response.json().catch(function () {
                return { success: false, message: "服务器返回了无效响应。" };
            });
        });
    }

    function roundRect(context, x, y, width, height, radius) {
        var r = Math.max(0, Math.min(radius, width / 2, height / 2));
        context.beginPath();
        context.moveTo(x + r, y);
        context.lineTo(x + width - r, y);
        context.quadraticCurveTo(x + width, y, x + width, y + r);
        context.lineTo(x + width, y + height - r);
        context.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
        context.lineTo(x + r, y + height);
        context.quadraticCurveTo(x, y + height, x, y + height - r);
        context.lineTo(x, y + r);
        context.quadraticCurveTo(x, y, x + r, y);
        context.closePath();
    }

    function mountMap(host, ctx) {
        if (!host || !ctx || !ctx.room || !ctx.room.id) return { destroy: function () {} };

        var permissions = Array.isArray(ctx.permissions) ? ctx.permissions : [];
        var canManage = permissions.indexOf("token.move.dm") >= 0 || permissions.indexOf("map.edit") >= 0;
        var destroyed = false;
        var mapState = null;
        var combat = { active: false };
        var serverCanManage = false;
        // 房间级开关：默认允许玩家移动自己的棋子；KP 关闭后只有房主/房管能动。
        var playerMoveAllowed = true;
        var selectedId = "";
        var dragging = null;
        var panning = null;
        var drawScheduled = false;
        var loadSeq = 0;
        var memberKey = "";
        var memberTimer = 0;
        var pollTimer = 0;
        var hintTimer = 0;
        var unsubscribers = [];
        var socket = null;
        var socketHandler = null;
        var fogState = { enabled: false };
        var fogMask = null;
        var fogMaskCanvas = document.createElement("canvas");
        var fogSignature = "";
        var fogHidden = false;

        host.classList.add("ws-plugin-map");

        var canvas = document.createElement("canvas");
        canvas.className = "vmap-canvas";
        canvas.setAttribute("aria-label", "地图棋子层");
        host.appendChild(canvas);

        var turnbar = document.createElement("div");
        turnbar.className = "vmap-turnbar";
        turnbar.hidden = true;
        host.appendChild(turnbar);

        var hint = document.createElement("div");
        hint.className = "vmap-hint";
        hint.hidden = true;
        host.appendChild(hint);

        var fogToggle = document.createElement("button");
        fogToggle.type = "button";
        fogToggle.className = "vmap-fog-toggle";
        fogToggle.hidden = true;
        host.appendChild(fogToggle);

        var menu = document.createElement("div");
        menu.className = "vmap-menu";
        menu.hidden = true;
        menu.setAttribute("role", "menu");
        document.body.appendChild(menu);

        var panel = document.createElement("div");
        panel.className = "vmap-panel";
        panel.hidden = true;
        panel.innerHTML = [
            '<div class="vmap-panel-title"><span data-vmap-panel-title>地图设置</span>',
            '<button type="button" data-vmap-close aria-label="关闭">×</button></div>',
            '<div data-vmap-section="scene">',
            '<div class="vmap-section-label">场景</div>',
            '<label class="vmap-field">当前场景',
            '<select data-vmap-scene-select></select></label>',
            '<label class="vmap-field">新场景名称',
            '<input type="text" maxlength="80" placeholder="例如：地下室" data-vmap-scene-title></label>',
            '<div class="vmap-actions">',
            '<button type="button" data-vmap-switch-scene>切换场景</button>',
            '<button type="button" class="secondary" data-vmap-create-scene>新建空场景</button>',
            '<button type="button" class="secondary" data-vmap-copy-scene>复制当前场景</button>',
            '</div>',
            '<div class="vmap-divider"></div>',
            '<div class="vmap-section-label">地图</div>',
            '<label class="vmap-field">网格尺寸（px）',
            '<input type="number" min="' + MIN_GRID + '" max="' + MAX_GRID + '" step="8" data-vmap-grid></label>',
            '<label class="vmap-field">地图边界（格）',
            '<input type="number" min="' + MIN_BOUNDS + '" max="' + MAX_BOUNDS + '" step="1" data-vmap-bounds></label>',
            '<label class="vmap-field">背景图地址',
            '<input type="url" maxlength="2048" placeholder="https://… 或 /assets/…" data-vmap-bg></label>',
            '<label class="vmap-check"><input type="checkbox" data-vmap-fog-enabled> 启用战争迷雾</label>',
            '<label class="vmap-field">默认视野半径（格）',
            '<input type="number" min="1" max="400" step="1" data-vmap-fog-vision></label>',
            '<label class="vmap-field">探索记录范围',
            '<select data-vmap-fog-scope>',
            '<option value="observer">按观察者（默认，各自记录）</option>',
            '<option value="party">队伍共享</option>',
            '</select></label>',
            '<div class="vmap-note" data-vmap-fog-info hidden></div>',
            '<div class="vmap-divider"></div>',
            '<div class="vmap-section-label">棋子权限</div>',
            '<label class="vmap-check"><input type="checkbox" data-vmap-player-move> 玩家可移动自己的棋子</label>',
            '<div class="vmap-note">关闭后只有房主或房管可以移动棋子；服务端会拒绝玩家的移动请求。</div>',
            '<div class="vmap-divider"></div>',
            '<div class="vmap-section-label">场景光源</div>',
            '<div class="vmap-light-list" data-vmap-light-list></div>',
            '<div class="vmap-actions"><button type="button" data-vmap-save-lights>保存光源开关</button></div>',
            '<div class="vmap-actions">',
            '<button type="button" data-vmap-save-map>保存地图</button>',
            '<button type="button" class="secondary" data-vmap-close>关闭</button>',
            "</div>",
            '<div class="vmap-divider"></div>',
            '<div class="vmap-section-label">导入地图</div>',
            '<label class="vmap-field">地图文件（PNG / JPG / WEBP / BMP、UVTT / dd2vtt、Foundry 场景 JSON，最大 64MB）',
            '<input type="file" accept=".png,.jpg,.jpeg,.webp,.bmp,.uvtt,.dd2vtt,.json,image/png,image/jpeg,image/webp,image/bmp,application/json" data-vmap-import></label>',
            '<div class="vmap-note" data-vmap-import-help>UVTT / Foundry 会一并带出墙体、门与光源；Foundry 仅支持内嵌背景图的场景。</div>',
            '<div class="vmap-actions"><button type="button" data-vmap-import-map>导入地图</button></div>',
            '<div class="vmap-note" data-vmap-import-info hidden></div>',
            '<div class="vmap-calibration" data-vmap-calibration hidden>',
            '<div class="vmap-note" data-vmap-calibration-info></div>',
            '<label class="vmap-field">横向格数',
            '<input type="number" min="1" max="400" step="1" data-vmap-cells-across></label>',
            '<div class="vmap-row">',
            '<label class="vmap-field">格线横向偏移（px）<input type="number" min="0" step="1" data-vmap-offset-x></label>',
            '<label class="vmap-field">格线纵向偏移（px）<input type="number" min="0" step="1" data-vmap-offset-y></label>',
            "</div>",
            '<div class="vmap-actions"><button type="button" data-vmap-save-calibration>保存标定</button></div>',
            "</div>",
            "</div>",
            '<div data-vmap-section="token">',
            '<div class="vmap-divider"></div>',
            '<div class="vmap-section-label">添加棋子</div>',
            '<label class="vmap-field">名称',
            '<input type="text" maxlength="40" placeholder="NPC / 怪物名称" data-vmap-token-name></label>',
            '<div class="vmap-row">',
            '<label class="vmap-field">宽（格）<input type="number" min="1" max="4" value="1" data-vmap-token-w></label>',
            '<label class="vmap-field">高（格）<input type="number" min="1" max="4" value="1" data-vmap-token-h></label>',
            "</div>",
            '<div class="vmap-actions"><button type="button" data-vmap-add-token>添加棋子</button></div>',
            "</div>",
        ].join("");
        host.appendChild(panel);

        var context = canvas.getContext("2d");
        var panelGrid = panel.querySelector("[data-vmap-grid]");
        var panelBounds = panel.querySelector("[data-vmap-bounds]");
        var panelBackground = panel.querySelector("[data-vmap-bg]");
        var panelName = panel.querySelector("[data-vmap-token-name]");
        var panelWidth = panel.querySelector("[data-vmap-token-w]");
        var panelHeight = panel.querySelector("[data-vmap-token-h]");
        var panelTitle = panel.querySelector("[data-vmap-panel-title]");
        var panelSections = panel.querySelectorAll("[data-vmap-section]");
        var panelImport = panel.querySelector("[data-vmap-import]");
        var panelImportInfo = panel.querySelector("[data-vmap-import-info]");
        var panelFogEnabled = panel.querySelector("[data-vmap-fog-enabled]");
        var panelFogVision = panel.querySelector("[data-vmap-fog-vision]");
        var panelFogScope = panel.querySelector("[data-vmap-fog-scope]");
        var panelPlayerMove = panel.querySelector("[data-vmap-player-move]");
        var panelFogInfo = panel.querySelector("[data-vmap-fog-info]");
        var panelLightList = panel.querySelector("[data-vmap-light-list]");
        var panelCalibration = panel.querySelector("[data-vmap-calibration]");
        var panelCalibrationInfo = panel.querySelector("[data-vmap-calibration-info]");
        var panelCellsAcross = panel.querySelector("[data-vmap-cells-across]");
        var panelOffsetX = panel.querySelector("[data-vmap-offset-x]");
        var panelOffsetY = panel.querySelector("[data-vmap-offset-y]");
        var panelSceneSelect = panel.querySelector("[data-vmap-scene-select]");
        var panelSceneTitle = panel.querySelector("[data-vmap-scene-title]");

        function notify(message, type) {
            ctx.notify(String(message || ""), type || "info");
        }

        function gridSize() {
            return clamp(asInt(mapState && mapState.grid_size, ctx.getGridSize()), MIN_GRID, MAX_GRID);
        }

        function gridOffset() {
            var grid = gridSize();
            var offset = mapState && mapState.grid_offset;
            return {
                x: clamp(asInt(offset && offset.x, 0), -grid, grid),
                y: clamp(asInt(offset && offset.y, 0), -grid, grid),
            };
        }

        // 迷雾掩码是 base64 位图，按 (row * bounds + col) 排列；服务端是唯一
        // 计算方，这里只负责解码和绘制，不做任何视线裁剪。
        function decodeMask(text, size) {
            var mask = new Uint8Array(size);
            var raw;
            try {
                raw = window.atob(String(text || ""));
            } catch (error) {
                return mask;
            }
            for (var index = 0; index < raw.length; index += 1) {
                var byte = raw.charCodeAt(index);
                if (!byte) continue;
                for (var bit = 0; bit < 8; bit += 1) {
                    var slot = index * 8 + bit;
                    if (slot >= size) break;
                    if (byte & (1 << bit)) mask[slot] = 1;
                }
            }
            return mask;
        }

        function applyFog(fog) {
            if (!fog || fog.enabled !== true) {
                fogState = { enabled: false };
                fogMask = null;
                fogSignature = "";
                updateFogToggle();
                return;
            }
            var bounds = asInt(fog.bounds_cells, DEFAULT_BOUNDS);
            var size = Math.max(1, bounds) * Math.max(1, bounds);
            fogState = fog;
            fogMask = {
                bounds: bounds,
                explored: decodeMask(fog.explored, size),
                visible: decodeMask(fog.visible, size),
                bright: decodeMask(fog.bright, size),
                dim: decodeMask(fog.dim, size),
            };
            fogSignature = "";
            updateFogToggle();
        }

        function updateFogToggle() {
            var show = (canManage || serverCanManage) && fogState.enabled === true;
            fogToggle.hidden = !show;
            fogToggle.textContent = fogHidden ? "显示迷雾" : "隐藏迷雾";
        }

        function ensureFogMaskCanvas() {
            if (!fogMask) return null;
            var bounds = fogMask.bounds;
            var signature = [
                bounds,
                fogState.explored || "",
                fogState.visible || "",
                fogState.bright || "",
                fogState.dim || "",
            ].join("|");
            if (signature === fogSignature) return fogMaskCanvas;
            fogSignature = signature;
            // 一格一像素：大场景也只有 bounds²，绘制时再放大，不在每帧重建。
            fogMaskCanvas.width = bounds;
            fogMaskCanvas.height = bounds;
            var maskContext = fogMaskCanvas.getContext("2d");
            if (!maskContext) return null;
            var image = maskContext.createImageData(bounds, bounds);
            var data = image.data;
            var total = bounds * bounds;
            for (var index = 0; index < total; index += 1) {
                // 未见 → 全黑；探索过但不在视野 → 半暗；视野内按明/暗区分层。
                var alpha = 242;
                if (fogMask.visible[index]) {
                    if (fogMask.bright[index]) alpha = 0;
                    else if (fogMask.dim[index]) alpha = 112;
                    else alpha = 178;
                } else if (fogMask.explored[index]) {
                    alpha = 148;
                }
                var offset = index * 4;
                data[offset] = 6;
                data[offset + 1] = 8;
                data[offset + 2] = 12;
                data[offset + 3] = alpha;
            }
            maskContext.putImageData(image, 0, 0);
            return fogMaskCanvas;
        }

        function drawFog(camera) {
            if (fogState.enabled !== true || fogHidden || !fogMask) return;
            var maskCanvas = ensureFogMaskCanvas();
            if (!maskCanvas) return;
            var grid = asInt(fogState.grid_size, gridSize());
            var half = asInt(fogState.half, Math.floor(fogMask.bounds / 2));
            var offset = fogState.grid_offset || { x: 0, y: 0 };
            var topLeft = ctx.worldToScreen(
                (Number(offset.x) || 0) - half * grid,
                (Number(offset.y) || 0) - half * grid
            );
            var cellPx = grid * camera.zoom;
            context.save();
            context.imageSmoothingEnabled = false;
            context.drawImage(
                maskCanvas,
                topLeft.x,
                topLeft.y,
                fogMask.bounds * cellPx,
                fogMask.bounds * cellPx
            );
            context.restore();
        }

        function newRequestId() {
            if (window.crypto && typeof window.crypto.randomUUID === "function") {
                return window.crypto.randomUUID();
            }
            return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
        }

        function writeBody(extra) {
            var body = extra || {};
            body.request_id = newRequestId();
            body.revision = asInt(mapState && mapState.revision, 0);
            return body;
        }

        function applyWriteFailure(response, fallback) {
            if (response && response.data && response.data.map) {
                applyPayload(response.data);
            }
            var message = (response && response.message) || fallback;
            notify(String(message), "error");
        }

        function tokens() {
            return mapState && Array.isArray(mapState.tokens) ? mapState.tokens : [];
        }

        function activeTokens() {
            return tokens();
        }

        function resizeCanvas() {
            var viewport = ctx.getViewport();
            var dpr = Math.min(window.devicePixelRatio || 1, 2);
            var width = Math.max(1, Math.round(viewport.width));
            var height = Math.max(1, Math.round(viewport.height));
            if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
                canvas.width = Math.round(width * dpr);
                canvas.height = Math.round(height * dpr);
            }
            if (context) context.setTransform(dpr, 0, 0, dpr, 0, 0);
            return { width: width, height: height };
        }

        function combatantForToken(token) {
            if (!combat || combat.active !== true) return null;
            var order = Array.isArray(combat.order) ? combat.order : [];
            var candidates = [String(token.combatant || "").toLowerCase(), String(token.name || "").toLowerCase()];
            var owner = String(token.owner_user_id || "");
            for (var i = 0; i < order.length; i += 1) {
                var item = order[i] || {};
                var name = String(item.name || "").toLowerCase();
                if (candidates.indexOf(name) >= 0) return item;
                if (owner && String(item.user_id || "") === owner) return item;
            }
            return null;
        }

        function isCurrentActor(token) {
            var item = combatantForToken(token);
            return Boolean(item && String(item.name || "") === String(combat.current || ""));
        }

        function canMove(token) {
            if (canManage || serverCanManage) return true;
            if (playerMoveAllowed === false) return false;
            return String(token.owner_user_id || "") === String(ctx.user.id || "");
        }

        function moveBlockedMessage() {
            return playerMoveAllowed === false
                ? "KP 已关闭「玩家可移动自己的棋子」，请联系房主或房管移动。"
                : "你只能移动自己绑定的棋子。";
        }

        function tokenWorld(token) {
            var grid = gridSize();
            var origin = gridOffset();
            var cellX = Number(token.cell_x);
            var cellY = Number(token.cell_y);
            if (!Number.isFinite(cellX) || !Number.isFinite(cellY)) {
                cellX = Math.floor(((Number(token.x) || 0) - origin.x) / grid + 1e-9);
                cellY = Math.floor(((Number(token.y) || 0) - origin.y) / grid + 1e-9);
            }
            var fractionX = Number(token.offset_x);
            var fractionY = Number(token.offset_y);
            if (!Number.isFinite(fractionX)) fractionX = 0;
            if (!Number.isFinite(fractionY)) fractionY = 0;
            return {
                x: origin.x + (cellX + fractionX) * grid,
                y: origin.y + (cellY + fractionY) * grid,
            };
        }

        function setTokenCell(token, cellX, cellY) {
            token.cell_x = cellX;
            token.cell_y = cellY;
            token.offset_x = 0;
            token.offset_y = 0;
            var world = tokenWorld(token);
            token.x = Math.round(world.x);
            token.y = Math.round(world.y);
        }

        function tokenRect(token) {
            var grid = gridSize();
            var camera = ctx.getCamera();
            var world = tokenWorld(token);
            var point = ctx.worldToScreen(world.x, world.y);
            var width = Math.max(1, asInt(token.w, 1)) * grid * camera.zoom;
            var height = Math.max(1, asInt(token.h, 1)) * grid * camera.zoom;
            var inset = Math.max(1.5, 2 * camera.zoom);
            return {
                x: point.x + inset,
                y: point.y + inset,
                width: Math.max(4, width - inset * 2),
                height: Math.max(4, height - inset * 2),
            };
        }

        function hitToken(localX, localY) {
            var list = activeTokens();
            for (var i = list.length - 1; i >= 0; i -= 1) {
                var token = list[i];
                var rect = tokenRect(token);
                if (localX >= rect.x && localX <= rect.x + rect.width && localY >= rect.y && localY <= rect.y + rect.height) {
                    return token;
                }
            }
            return null;
        }

        function scheduleDraw() {
            if (drawScheduled || destroyed) return;
            drawScheduled = true;
            window.requestAnimationFrame(function () {
                drawScheduled = false;
                draw();
            });
        }

        function drawSceneLayers(camera) {
            if (!mapState) return;
            var grid = gridSize();

            function paintLight(x, y, dimCells, brightCells, color, intensity) {
                var point = ctx.worldToScreen(Number(x) || 0, Number(y) || 0);
                var radius = Math.max(1, Number(dimCells) || 0) * grid * camera.zoom;
                if (radius <= 2) return;
                var strength = clamp(Number(intensity) || 1, 0, 1.5);
                var brightStop = brightCells > 0
                    ? clamp(Number(brightCells) / Math.max(1, Number(dimCells)), 0.05, 1)
                    : 1;
                var gradient = context.createRadialGradient(point.x, point.y, 0, point.x, point.y, radius);
                gradient.addColorStop(0, hexToRgba(color, 0.24 * strength));
                gradient.addColorStop(brightStop * 0.92, hexToRgba(color, 0.13 * strength));
                gradient.addColorStop(1, hexToRgba(color, 0));
                context.fillStyle = gradient;
                context.beginPath();
                context.arc(point.x, point.y, radius, 0, Math.PI * 2);
                context.fill();
            }

            var lights = Array.isArray(mapState.lights) ? mapState.lights : [];
            context.save();
            lights.forEach(function (light) {
                if (light.enabled === false) return;
                paintLight(
                    light.x,
                    light.y,
                    light.range_cells,
                    light.bright_cells,
                    String(light.color || "#ffaa00"),
                    light.intensity
                );
            });
            // 棋子自带光源：跟随棋子 footprint 中心；几何遮挡以服务端掩码为准。
            activeTokens().forEach(function (token) {
                var light = token.light;
                if (!light || light.enabled === false) return;
                var anchor = tokenWorld(token);
                paintLight(
                    anchor.x + (Number(token.w) || 1) * grid / 2,
                    anchor.y + (Number(token.h) || 1) * grid / 2,
                    light.dim_cells,
                    light.bright_cells,
                    String(light.color || "#ffaa00"),
                    light.intensity
                );
            });
            context.restore();

            var walls = Array.isArray(mapState.walls) ? mapState.walls : [];
            context.save();
            walls.forEach(function (wall) {
                var points = Array.isArray(wall.points) ? wall.points : [];
                if (points.length < 2) return;
                context.beginPath();
                points.forEach(function (item, index) {
                    var point = ctx.worldToScreen(Number(item.x) || 0, Number(item.y) || 0);
                    if (index === 0) context.moveTo(point.x, point.y);
                    else context.lineTo(point.x, point.y);
                });
                var kind = String(wall.kind || "wall");
                var openDoor = kind === "door" && String(wall.state || "closed") === "open";
                if (kind === "door") {
                    context.strokeStyle = openDoor ? "rgba(147, 196, 125, 0.9)" : "rgba(224, 102, 102, 0.92)";
                } else if (kind === "window") {
                    context.strokeStyle = "rgba(111, 168, 220, 0.92)";
                } else if (kind === "terrain") {
                    context.strokeStyle = "rgba(246, 178, 107, 0.68)";
                } else {
                    context.strokeStyle = "rgba(200, 163, 90, 0.78)";
                }
                context.lineWidth = Math.max(1.5, (kind === "door" ? 3 : 2) * camera.zoom);
                context.setLineDash(openDoor ? [6 * camera.zoom, 4 * camera.zoom] : []);
                context.stroke();
            });
            context.restore();
        }

        /** 横纵坐标统一用数字：左上角为 1；原点左侧 / 上方为 0、-1…，保持连续。 */
        function axisLabel(index) {
            return String(index >= 0 ? index + 1 : index);
        }

        /** 地图边界以世界原点为中心，左上角格索引是 -half，把它平移成 0 起算。 */
        function coordinateHalf() {
            var bounds = asInt(mapState && mapState.bounds_cells, 0);
            return bounds > 0 ? Math.floor(bounds / 2) : 0;
        }

        /**
         * 视口坐标尺：上沿列号、左沿行号，均为数字，与网格标定（grid_size/grid_offset）同源。
         * 画在棋子之前，棋子会盖住标签边缘，不会出现浮层挡住棋子。
         * 格子太小时按固定列间隔抽样，避免标签糊成一片。
         */
        function drawCoordinates(camera) {
            var viewport = ctx.getViewport();
            if (!viewport || viewport.gridVisible === false) return;
            var grid = gridSize();
            var offset = gridOffset();
            var cellPx = grid * (Number(camera && camera.zoom) || 0);
            if (!(cellPx >= 10)) return;
            var topLeft = ctx.screenToWorld(0, 0);
            var bottomRight = ctx.screenToWorld(viewport.width, viewport.height);
            if (!topLeft || !bottomRight) return;
            var half = coordinateHalf();
            var firstCol = Math.floor((topLeft.x - offset.x) / grid);
            var lastCol = Math.floor((bottomRight.x - offset.x) / grid);
            var firstRow = Math.floor((topLeft.y - offset.y) / grid);
            var lastRow = Math.floor((bottomRight.y - offset.y) / grid);
            var step = 1;
            while (cellPx * step < 26) step += 1;

            context.font = '700 10px "Segoe UI", sans-serif';
            context.textAlign = "center";
            context.textBaseline = "middle";

            function paintChip(text, centerX, centerY) {
                var chipWidth = Math.ceil(context.measureText(text).width) + 8;
                if (centerX - chipWidth / 2 < 1 || centerX + chipWidth / 2 > viewport.width - 1) return;
                if (centerY - 7 < 1 || centerY + 7 > viewport.height - 1) return;
                roundRect(context, centerX - chipWidth / 2, centerY - 7, chipWidth, 14, 3);
                context.fillStyle = "rgba(8, 11, 14, 0.68)";
                context.fill();
                context.fillStyle = "rgba(242, 234, 216, 0.92)";
                context.fillText(text, centerX, centerY + 0.5);
            }

            for (var col = firstCol; col <= lastCol; col += 1) {
                if (((col % step) + step) % step !== 0) continue;
                var screenX = ctx.worldToScreen(offset.x + col * grid, 0).x + cellPx / 2;
                paintChip(axisLabel(col + half), screenX, 10);
            }
            for (var row = firstRow; row <= lastRow; row += 1) {
                if (((row % step) + step) % step !== 0) continue;
                var screenY = ctx.worldToScreen(0, offset.y + row * grid).y + cellPx / 2;
                var text = axisLabel(row + half);
                var chipWidth = Math.ceil(context.measureText(text).width) + 8;
                paintChip(text, 4 + chipWidth / 2, screenY);
            }
        }

        function draw() {
            if (!context || destroyed) return;
            var size = resizeCanvas();
            var camera = ctx.getCamera();
            var list = activeTokens();
            context.clearRect(0, 0, size.width, size.height);
            drawSceneLayers(camera);
            drawFog(camera);
            // 坐标尺画在棋子之前：棋子永远在标签之上，不会被遮住。
            drawCoordinates(camera);
            list.forEach(function (token) {
                var rect = tokenRect(token);
                var current = isCurrentActor(token);
                context.save();
                if (current) {
                    context.shadowColor = "rgba(200, 163, 90, 0.9)";
                    context.shadowBlur = 12 + 6 * camera.zoom;
                }
                roundRect(context, rect.x, rect.y, rect.width, rect.height, Math.min(10, rect.width * 0.16));
                context.fillStyle = String(token.color || "#c8a35a");
                context.globalAlpha = 0.92;
                context.fill();
                context.globalAlpha = 1;
                context.lineWidth = String(token.id) === selectedId ? Math.max(2, 2.4 * camera.zoom) : Math.max(1, 1.4 * camera.zoom);
                context.strokeStyle = String(token.id) === selectedId ? "#f5f0e2" : "rgba(0, 0, 0, 0.5)";
                context.stroke();
                context.restore();

                var name = String(token.name || "?");
                var initial = name.slice(0, 1) || "?";
                var fontSize = clamp(rect.width * 0.42, 10, 30);
                context.fillStyle = "rgba(16, 16, 16, 0.88)";
                context.font = "700 " + Math.round(fontSize) + "px \"Segoe UI\", sans-serif";
                context.textAlign = "center";
                context.textBaseline = "middle";
                context.fillText(initial, rect.x + rect.width / 2, rect.y + rect.height / 2);
                if (camera.zoom >= 0.8) {
                    var labelSize = Math.round(clamp(11 * camera.zoom, 9, 13));
                    context.font = "600 " + labelSize + "px \"Segoe UI\", sans-serif";
                    var labelX = rect.x + rect.width / 2;
                    var labelY = rect.y + rect.height + labelSize + 3;
                    var labelWidth = context.measureText(name).width + 8;
                    context.fillStyle = "rgba(10, 13, 16, 0.72)";
                    roundRect(context, labelX - labelWidth / 2, labelY - labelSize / 2 - 2, labelWidth, labelSize + 4, 4);
                    context.fill();
                    context.fillStyle = "rgba(245, 240, 226, 0.92)";
                    context.fillText(name, labelX, labelY);
                }
            });
        }

        function renderTurnBar() {
            turnbar.replaceChildren();
            if (!combat || combat.active !== true) {
                turnbar.hidden = true;
                return;
            }
            var order = Array.isArray(combat.order) ? combat.order : [];
            var round = document.createElement("span");
            round.className = "vmap-turn-round";
            round.textContent = "第 " + asInt(combat.round, 1) + " 轮";
            turnbar.appendChild(round);
            order.forEach(function (item) {
                var name = String((item && item.name) || "");
                if (!name) return;
                var chip = document.createElement("span");
                chip.className = "vmap-turn-item" + (name === String(combat.current || "") ? " is-current" : "");
                chip.textContent = name;
                if (name === String(combat.current || "")) {
                    var budget = item && item.budget && typeof item.budget.movement === "number" ? item.budget.movement : null;
                    if (budget !== null) {
                        var tag = document.createElement("span");
                        tag.className = "vmap-turn-budget";
                        tag.textContent = "· 移动 " + budget + " ft";
                        chip.appendChild(tag);
                    }
                }
                turnbar.appendChild(chip);
            });
            turnbar.hidden = order.length === 0;
        }

        function showHint(message, timeout) {
            hint.textContent = message;
            hint.hidden = false;
            window.clearTimeout(hintTimer);
            if (timeout) {
                hintTimer = window.setTimeout(function () {
                    hint.hidden = true;
                }, timeout);
            }
        }

        function hideMenu() {
            menu.hidden = true;
            menu.replaceChildren();
        }

        function hidePanel() {
            panel.hidden = true;
        }

        function menuButton(label, danger, action) {
            var button = document.createElement("button");
            button.type = "button";
            button.textContent = label;
            if (danger) button.className = "is-danger";
            button.addEventListener("click", function () {
                hideMenu();
                action();
            });
            return button;
        }

        function showMenu(token, clientX, clientY) {
            menu.replaceChildren();
            menu.appendChild(menuButton("取消选中", false, function () {
                selectedId = "";
                scheduleDraw();
            }));
            if (canMove(token)) {
                var tokenLight = token.light;
                if (tokenLight) {
                    var nextEnabled = tokenLight.enabled === false;
                    menu.appendChild(menuButton(nextEnabled ? "点亮自带光源" : "熄灭火把", false, function () {
                        updateTokenLight(token, tokenLightCopy(tokenLight, nextEnabled));
                    }));
                    menu.appendChild(menuButton("移除自带光源", true, function () {
                        updateTokenLight(token, null, true);
                    }));
                } else {
                    menu.appendChild(menuButton("添加火把（明亮4/昏暗8格）", false, function () {
                        updateTokenLight(token, {
                            bright_cells: 4,
                            dim_cells: 8,
                            color: "#ffaa00",
                            enabled: true,
                        });
                    }));
                }
            }
            if (canManage || serverCanManage) {
                menu.appendChild(menuButton("删除棋子", true, function () {
                    deleteToken(token);
                }));
            }
            menu.hidden = false;
            menu.style.left = clientX + "px";
            menu.style.top = clientY + "px";
            var rect = menu.getBoundingClientRect();
            menu.style.left = clamp(clientX, 8, Math.max(8, window.innerWidth - rect.width - 8)) + "px";
            menu.style.top = clamp(clientY, 8, Math.max(8, window.innerHeight - rect.height - 8)) + "px";
        }

        function renderScenes(data) {
            if (!panelSceneSelect) return;
            var list = data && Array.isArray(data.scenes) ? data.scenes : [];
            var current = String((data && data.scene_id) || (mapState && mapState.scene_id) || "");
            panelSceneSelect.textContent = "";
            list.forEach(function (scene) {
                if (!scene || !scene.scene_id) return;
                var option = document.createElement("option");
                option.value = String(scene.scene_id);
                option.textContent = String(scene.title || scene.scene_id) +
                    (scene.active ? "（当前）" : "");
                panelSceneSelect.appendChild(option);
            });
            if (current) panelSceneSelect.value = current;
        }

        function applyPayload(data) {
            if (!data || !data.map) return;
            // 拖拽期间不替换地图状态：松手后用写回执（成功或冲突）统一对齐，
            // 避免正在拖的棋子从列表里消失。
            if (dragging) return;
            mapState = data.map;
            renderScenes(data);
            if (data.combat && typeof data.combat === "object") {
                combat = data.combat.active ? data.combat : { active: false };
            }
            if (typeof data.can_manage === "boolean") serverCanManage = data.can_manage;
            if (typeof data.player_move_enabled === "boolean") playerMoveAllowed = data.player_move_enabled;
            if (data.fog && typeof data.fog === "object") applyFog(data.fog);
            if (!tokens().some(function (token) { return String(token.id) === selectedId; })) selectedId = "";
            ctx.setBaseStage({
                background: mapState.background || "",
                gridSize: gridSize(),
                gridOffset: gridOffset(),
            });
            renderTurnBar();
            scheduleDraw();
        }

        function loadMap(silent) {
            var seq = ++loadSeq;
            return apiRequest("GET", "/rooms/" + encodeURIComponent(ctx.room.id) + "/map").then(function (response) {
                if (destroyed || seq !== loadSeq) return;
                if (!response || response.success !== true) {
                    if (!silent) notify((response && response.message) || "地图加载失败。", "error");
                    return;
                }
                applyPayload(response.data);
            }).catch(function (error) {
                console.error("veil-map load failed", error);
                if (!silent && !destroyed) notify("地图加载失败，请稍后重试。", "error");
            });
        }

        function persistMove(token, previous) {
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" + encodeURIComponent(token.id) + "/move", writeBody({
                cell_x: token.cell_x,
                cell_y: token.cell_y,
                offset_x: Number(token.offset_x) || 0,
                offset_y: Number(token.offset_y) || 0,
            })).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    if (!response || !response.data || !response.data.map) {
                        token.cell_x = previous.cell_x;
                        token.cell_y = previous.cell_y;
                        token.offset_x = previous.offset_x;
                        token.offset_y = previous.offset_y;
                        scheduleDraw();
                    }
                    applyWriteFailure(response, "棋子移动失败。");
                    return;
                }
                applyPayload(response.data);
            }).catch(function (error) {
                console.error("veil-map move failed", error);
                token.cell_x = previous.cell_x;
                token.cell_y = previous.cell_y;
                token.offset_x = previous.offset_x;
                token.offset_y = previous.offset_y;
                scheduleDraw();
                notify("棋子移动失败，请稍后重试。", "error");
            });
        }

        function deleteToken(token) {
            var body = writeBody({});
            var path = "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" + encodeURIComponent(token.id) +
                "?request_id=" + encodeURIComponent(body.request_id) +
                "&revision=" + encodeURIComponent(String(body.revision));
            apiRequest("DELETE", path).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "删除棋子失败。");
                    return;
                }
                if (String(token.id) === selectedId) selectedId = "";
                applyPayload(response.data);
                notify("棋子已删除。", "success");
            }).catch(function (error) {
                console.error("veil-map delete failed", error);
                notify("删除棋子失败，请稍后重试。", "error");
            });
        }

        function updateTokenLight(token, lightPayload, remove) {
            var body = writeBody(remove ? { remove: true } : { light: lightPayload });
            apiRequest(
                "PUT",
                "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" +
                    encodeURIComponent(token.id) + "/light",
                body
            ).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "更新棋子光源失败。");
                    return;
                }
                applyPayload(response.data);
                notify(remove ? "已移除棋子自带光源。" : "棋子光源已更新。", "success");
            }).catch(function (error) {
                console.error("veil-map token light failed", error);
                notify("更新棋子光源失败，请稍后重试。", "error");
            });
        }

        function tokenLightCopy(light, enabled) {
            return {
                bright_cells: asInt(light.bright_cells, 4),
                dim_cells: asInt(light.dim_cells, 8),
                color: String(light.color || "#ffaa00"),
                intensity: Number(light.intensity) || 1,
                shadows: light.shadows !== false,
                enabled: Boolean(enabled),
            };
        }

        function renderSceneLights() {
            if (!panelLightList) return;
            panelLightList.replaceChildren();
            var lights = mapState && Array.isArray(mapState.lights) ? mapState.lights : [];
            if (!lights.length) {
                var empty = document.createElement("div");
                empty.className = "vmap-note";
                empty.textContent = "暂无场景光源；导入 UVTT 地图会自动带出。";
                panelLightList.appendChild(empty);
                return;
            }
            lights.forEach(function (light, index) {
                var row = document.createElement("label");
                row.className = "vmap-light-row";
                var box = document.createElement("input");
                box.type = "checkbox";
                box.checked = light.enabled !== false;
                box.setAttribute("data-vmap-light-toggle", String(index));
                var dot = document.createElement("span");
                dot.className = "vmap-light-dot";
                dot.style.background = String(light.color || "#ffaa00");
                var text = document.createElement("span");
                text.textContent = "光源 " + (index + 1) + " · 明亮 " +
                    asInt(light.bright_cells, 0) + " / 昏暗 " +
                    asInt(light.range_cells, 0) + " 格";
                row.appendChild(box);
                row.appendChild(dot);
                row.appendChild(text);
                panelLightList.appendChild(row);
            });
        }

        function saveSceneLights() {
            var lights = (mapState && Array.isArray(mapState.lights) ? mapState.lights : []).map(function (light, index) {
                var box = panelLightList
                    ? panelLightList.querySelector('[data-vmap-light-toggle="' + index + '"]')
                    : null;
                var copy = {};
                Object.keys(light).forEach(function (key) {
                    copy[key] = light[key];
                });
                copy.enabled = Boolean(box && box.checked);
                return copy;
            });
            apiRequest(
                "PUT",
                "/rooms/" + encodeURIComponent(ctx.room.id) + "/lights",
                writeBody({ lights: lights })
            ).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "光源开关保存失败。");
                    return;
                }
                applyPayload(response.data);
                renderSceneLights();
                notify("光源开关已保存。", "success");
            }).catch(function (error) {
                console.error("veil-map lights failed", error);
                notify("光源开关保存失败，请稍后重试。", "error");
            });
        }

        function openPanel(mode) {
            if (!canManage && !serverCanManage) {
                notify("只有房主或房管可以编辑地图。", "info");
                return;
            }
            panel.hidden = false;
            panelTitle.textContent = mode === "token" ? "添加棋子" : "地图设置";
            panelSections.forEach(function (section) {
                section.hidden = section.getAttribute("data-vmap-section") !== mode;
            });
            if (mapState) {
                panelGrid.value = String(gridSize());
                panelBounds.value = String(clamp(asInt(mapState.bounds_cells, DEFAULT_BOUNDS), MIN_BOUNDS, MAX_BOUNDS));
                panelBackground.value = String(mapState.background || "");
            }
            if (mode === "scene") {
                renderCalibration();
                if (panelPlayerMove) panelPlayerMove.checked = playerMoveAllowed !== false;
                var meta = mapState && mapState.background_meta;
                renderImportInfo(meta && meta.import ? meta : null);
                renderSceneLights();
                if (panelFogEnabled) panelFogEnabled.checked = fogState.enabled === true;
                if (panelFogVision) {
                    panelFogVision.value = String(asInt(fogState.vision_cells, 12));
                }
                if (panelFogScope) {
                    panelFogScope.value = fogState.scope === "party" ? "party" : "observer";
                }
                if (panelFogInfo) {
                    var explored = asInt(fogState.explored_count, 0);
                    var shared = fogState.scope === "party";
                    panelFogInfo.hidden = fogState.enabled !== true;
                    panelFogInfo.textContent = (shared
                        ? "当前按队伍共享记录：已记录 " + explored + " 格，所有玩家看到同一片区域。"
                        : "当前按观察者记录：已记录 " + explored + " 格；每名玩家只看到自己探索过的区域，无绑定棋子的玩家退回队伍视野。");
                }
            }
            if (mode === "token" && panelName) panelName.focus();
        }

        function requestScene(path, body, successMessage) {
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + path, body).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "场景操作失败。");
                    return;
                }
                applyPayload(response.data);
                notify(successMessage, "success");
            }).catch(function (error) {
                console.error("veil-map scene failed", error);
                notify("场景操作失败，请稍后重试。", "error");
            });
        }

        function switchScene() {
            var sceneId = panelSceneSelect && panelSceneSelect.value;
            if (!sceneId) return;
            requestScene(
                "/map/scenes/" + encodeURIComponent(sceneId) + "/activate",
                writeBody({}),
                "已切换场景。"
            );
        }

        function createScene(copy) {
            var title = panelSceneTitle ? String(panelSceneTitle.value || "").trim() : "";
            requestScene(
                "/map/scenes",
                writeBody({ title: title, copy: Boolean(copy) }),
                copy ? "已复制并切换到新场景。" : "已新建并切换到新场景。"
            );
        }

        function saveMapSettings() {
            var grid = clamp(asInt(panelGrid.value, gridSize()), MIN_GRID, MAX_GRID);
            var bounds = clamp(asInt(panelBounds.value, DEFAULT_BOUNDS), MIN_BOUNDS, MAX_BOUNDS);
            apiRequest("PUT", "/rooms/" + encodeURIComponent(ctx.room.id) + "/map", writeBody({
                grid_size: grid,
                bounds_cells: bounds,
                background: String(panelBackground.value || "").trim(),
                fog_enabled: Boolean(panelFogEnabled && panelFogEnabled.checked),
                fog_vision_cells: clamp(asInt(panelFogVision && panelFogVision.value, 12), 1, 400),
                fog_scope: panelFogScope && panelFogScope.value === "party" ? "party" : "observer",
            })).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "地图设置保存失败。");
                    return;
                }
                applyPayload(response.data);
                hidePanel();
                notify("地图设置已保存。", "success");
            }).catch(function (error) {
                console.error("veil-map map settings failed", error);
                notify("地图设置保存失败，请稍后重试。", "error");
            });
        }

        function savePlayerMovePermission(enabled) {
            apiRequest(
                "PUT",
                "/rooms/" + encodeURIComponent(ctx.room.id) + "/map/permissions",
                { player_move_enabled: Boolean(enabled) }
            ).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    if (panelPlayerMove) panelPlayerMove.checked = playerMoveAllowed !== false;
                    applyWriteFailure(response, "棋子移动权限保存失败。");
                    return;
                }
                if (response.data && typeof response.data.player_move_enabled === "boolean") {
                    playerMoveAllowed = response.data.player_move_enabled;
                }
                if (panelPlayerMove) panelPlayerMove.checked = playerMoveAllowed !== false;
                scheduleDraw();
                notify(
                    playerMoveAllowed === false
                        ? "已关闭玩家自行移动棋子，只有房主/房管可以移动。"
                        : "已允许玩家移动自己的棋子。",
                    "success"
                );
            }).catch(function (error) {
                console.error("veil-map permission failed", error);
                if (panelPlayerMove) panelPlayerMove.checked = playerMoveAllowed !== false;
                notify("棋子移动权限保存失败，请稍后重试。", "error");
            });
        }

        function calibrationGrid(cellsAcross) {
            var meta = mapState && mapState.background_meta;
            if (!meta || !meta.width) return gridSize();
            return clamp(Math.round(meta.width / Math.max(1, cellsAcross)), MIN_GRID, MAX_GRID);
        }

        function renderCalibration() {
            var meta = mapState && mapState.background_meta;
            var calibration = mapState && mapState.calibration;
            var hasImage = Boolean(meta && meta.width > 0 && meta.height > 0);
            panelCalibration.hidden = !hasImage;
            if (!hasImage) return;
            var across = calibration && calibration.cells_across
                ? calibration.cells_across
                : Math.max(1, Math.round(meta.width / gridSize()));
            panelCellsAcross.value = String(across);
            panelOffsetX.value = String(calibration && calibration.offset_x ? calibration.offset_x : 0);
            panelOffsetY.value = String(calibration && calibration.offset_y ? calibration.offset_y : 0);
            var derived = calibrationGrid(across);
            panelOffsetX.max = String(derived - 1);
            panelOffsetY.max = String(derived - 1);
            panelCalibrationInfo.textContent = "已导入：" + String(meta.name || "地图图片") +
                "（" + meta.width + "×" + meta.height + "px，每格 " + derived + "px）";
        }

        function renderImportInfo(details) {
            if (!panelImportInfo) return;
            var info = details && details.import ? details.import : details;
            if (!info || !info.format) {
                panelImportInfo.hidden = true;
                panelImportInfo.textContent = "";
                return;
            }
            var parts = [String(info.label || info.format)];
            if (typeof info.walls === "number") parts.push("墙体 " + info.walls + " 段");
            if (typeof info.doors === "number") parts.push("门/窗 " + info.doors + " 个");
            if (typeof info.lights === "number") parts.push("光源 " + info.lights + " 个");
            if (info.needs_review) parts.push("需要复核网格");
            var warnings = Array.isArray(info.warnings) ? info.warnings : [];
            var text = parts.join(" · ");
            if (warnings.length) text += "。" + warnings.join(" ");
            panelImportInfo.textContent = text;
            panelImportInfo.hidden = false;
        }

        function importMap() {
            var file = panelImport && panelImport.files ? panelImport.files[0] : null;
            if (!file) {
                notify("请先选择要导入的地图文件。", "error");
                return;
            }
            var envelope = writeBody({});
            var form = new FormData();
            form.append("file", file);
            form.append("request_id", envelope.request_id);
            form.append("revision", String(envelope.revision));
            apiRequest(
                "POST",
                "/rooms/" + encodeURIComponent(ctx.room.id) + "/map/import",
                form
            ).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "地图导入失败。");
                    return;
                }
                applyPayload(response.data);
                var hint = response.data && response.data.calibration_hint;
                if (hint) {
                    panelCellsAcross.value = String(hint.cells_across);
                    panelOffsetX.value = String(hint.offset_x || 0);
                    panelOffsetY.value = String(hint.offset_y || 0);
                }
                if (panelImport) panelImport.value = "";
                if (panelBackground) panelBackground.value = String(response.data.map.background || "");
                renderImportInfo(response.data);
                renderCalibration();
                notify(response.message || "地图已导入，请调整网格标定。", hint && hint.needs_review ? "info" : "success");
            }).catch(function (error) {
                console.error("veil-map import failed", error);
                notify("地图导入失败，请稍后重试。", "error");
            });
        }

        function saveCalibration() {
            var across = clamp(asInt(panelCellsAcross.value, 0), 1, 400);
            var grid = calibrationGrid(across);
            var offsetX = clamp(asInt(panelOffsetX.value, 0), 0, Math.max(0, grid - 1));
            var offsetY = clamp(asInt(panelOffsetY.value, 0), 0, Math.max(0, grid - 1));
            apiRequest(
                "PUT",
                "/rooms/" + encodeURIComponent(ctx.room.id) + "/map/calibration",
                writeBody({
                    cells_across: across,
                    offset_x: offsetX,
                    offset_y: offsetY,
                })
            ).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "网格标定保存失败。");
                    return;
                }
                applyPayload(response.data);
                renderCalibration();
                notify(response.message || "网格标定已保存。", "success");
            }).catch(function (error) {
                console.error("veil-map calibration failed", error);
                notify("网格标定保存失败，请稍后重试。", "error");
            });
        }

        function addToken() {
            var name = String(panelName.value || "").trim();
            if (!name) {
                notify("请先填写棋子名称。", "error");
                panelName.focus();
                return;
            }
            var viewport = ctx.getViewport();
            var world = ctx.screenToWorld(viewport.width / 2, viewport.height / 2);
            var grid = gridSize();
            var offset = gridOffset();
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens", writeBody({
                name: name,
                cell_x: Math.round((world.x - offset.x) / grid),
                cell_y: Math.round((world.y - offset.y) / grid),
                offset_x: 0,
                offset_y: 0,
                w: clamp(asInt(panelWidth.value, 1), 1, 4),
                h: clamp(asInt(panelHeight.value, 1), 1, 4),
            })).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    applyWriteFailure(response, "添加棋子失败。");
                    return;
                }
                applyPayload(response.data);
                panelName.value = "";
                notify("棋子已添加。", "success");
            }).catch(function (error) {
                console.error("veil-map add token failed", error);
                notify("添加棋子失败，请稍后重试。", "error");
            });
        }

        function localPoint(event) {
            var rect = canvas.getBoundingClientRect();
            return { x: event.clientX - rect.left, y: event.clientY - rect.top };
        }

        function onPointerDown(event) {
            if (event.button !== 0) return;
            hideMenu();
            var point = localPoint(event);
            var token = hitToken(point.x, point.y);
            if (token) {
                selectedId = String(token.id);
                scheduleDraw();
                if (!canMove(token)) {
                    notify(moveBlockedMessage(), "error");
                    return;
                }
                if (combat && combat.active === true && combatantForToken(token) && !isCurrentActor(token)) {
                    notify("现在轮到「" + String(combat.current || "其他角色") + "」行动，不能移动「" + String(token.name || "该棋子") + "」。", "error");
                    return;
                }
                var world = ctx.screenToWorld(point.x, point.y);
                var tokenWorldPoint = tokenWorld(token);
                dragging = {
                    token: token,
                    pointerId: event.pointerId,
                    offsetX: world.x - tokenWorldPoint.x,
                    offsetY: world.y - tokenWorldPoint.y,
                    previous: {
                        cell_x: token.cell_x,
                        cell_y: token.cell_y,
                        offset_x: token.offset_x,
                        offset_y: token.offset_y,
                    },
                    moved: false,
                };
                canvas.classList.remove("is-panning");
                canvas.setPointerCapture(event.pointerId);
                return;
            }
            selectedId = "";
            scheduleDraw();
            panning = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
            canvas.classList.add("is-panning");
            canvas.setPointerCapture(event.pointerId);
        }

        function onPointerMove(event) {
            if (dragging && dragging.pointerId === event.pointerId) {
                var point = localPoint(event);
                var world = ctx.screenToWorld(point.x, point.y);
                var grid = gridSize();
                var offset = gridOffset();
                var nextCellX = Math.round((world.x - dragging.offsetX - offset.x) / grid);
                var nextCellY = Math.round((world.y - dragging.offsetY - offset.y) / grid);
                if (nextCellX !== Number(dragging.token.cell_x) || nextCellY !== Number(dragging.token.cell_y)) {
                    setTokenCell(dragging.token, nextCellX, nextCellY);
                    dragging.moved = nextCellX !== dragging.previous.cell_x || nextCellY !== dragging.previous.cell_y;
                    scheduleDraw();
                }
                return;
            }
            if (panning && panning.pointerId === event.pointerId) {
                var dx = event.clientX - panning.x;
                var dy = event.clientY - panning.y;
                panning.x = event.clientX;
                panning.y = event.clientY;
                if (dx || dy) ctx.panBy(dx, dy);
                return;
            }
            var hover = localPoint(event);
            var token = hitToken(hover.x, hover.y);
            canvas.style.cursor = token ? (canMove(token) ? "grab" : "not-allowed") : "default";
        }

        function releasePointer(pointerId) {
            try {
                canvas.releasePointerCapture(pointerId);
            } catch (error) {
                // 浏览器可能已经隐式释放；忽略即可。
            }
        }

        function endPointer(event) {
            if (dragging && dragging.pointerId === event.pointerId) {
                var token = dragging.token;
                var moved = dragging.moved;
                var previous = dragging.previous;
                dragging = null;
                releasePointer(event.pointerId);
                if (moved) persistMove(token, previous);
                else scheduleDraw();
                return;
            }
            if (panning && panning.pointerId === event.pointerId) {
                panning = null;
                releasePointer(event.pointerId);
                canvas.classList.remove("is-panning");
            }
        }

        function onWheel(event) {
            event.preventDefault();
            var factor = event.deltaY < 0 ? 1.12 : 0.89;
            ctx.zoomAt(factor, event.clientX, event.clientY);
        }

        function onContextMenu(event) {
            event.preventDefault();
            var point = localPoint(event);
            var token = hitToken(point.x, point.y);
            if (!token) {
                hideMenu();
                return;
            }
            selectedId = String(token.id);
            scheduleDraw();
            showMenu(token, event.clientX, event.clientY);
        }

        function onDocumentPointerDown(event) {
            if (!menu.hidden && !menu.contains(event.target)) hideMenu();
        }

        function onKeyDown(event) {
            if (event.key !== "Escape") return;
            hideMenu();
            hidePanel();
            selectedId = "";
            scheduleDraw();
        }

        function onSocketMapChanged(payload) {
            if (destroyed || !payload || !payload.map) return;
            if (String(payload.room_id || "") !== String(ctx.room.id)) return;
            applyPayload({
                map: payload.map,
                fog: payload.fog,
                combat: combat,
                can_manage: typeof payload.can_manage === "boolean" ? payload.can_manage : serverCanManage,
                player_move_enabled: payload.player_move_enabled,
            });
            var move = payload.move;
            if (move && move.source === "ai") {
                var target = move.to || {};
                var text = String(move.actor || "KP") + " 将「" + String(move.token_name || "棋子") +
                    "」移动到 (" + asInt(target.cell_x, 0) + ", " + asInt(target.cell_y, 0) + ")";
                if (move.reason) text += "：" + String(move.reason);
                notify(text, "info");
            }
        }

        function onSocketReconnect() {
            // 断线期间地图/迷雾/场景可能已经变化：重连后必须重新拉取，
            // 不能沿用断线前的内存副本。
            if (destroyed) return;
            loadMap(true);
        }

        function attachSocket() {
            socket = window.TrpgSocket || null;
            if (!socket) return;
            socketHandler = onSocketMapChanged;
            socket.on("room_map_changed", socketHandler);
            if (typeof socket.on === "function") {
                socket.on("connect", onSocketReconnect);
                socket.on("reconnect", onSocketReconnect);
            }
        }

        function detachSocket() {
            if (socket && socketHandler && typeof socket.off === "function") {
                socket.off("room_map_changed", socketHandler);
            }
            if (socket && typeof socket.off === "function") {
                socket.off("connect", onSocketReconnect);
                socket.off("reconnect", onSocketReconnect);
            }
            socket = null;
            socketHandler = null;
        }

        function memberSignature(room) {
            var members = room && Array.isArray(room.members) ? room.members : [];
            return members.map(function (member) {
                return String((member && member.user_id) || "");
            }).sort().join(",");
        }

        function onMembers(room) {
            var next = memberSignature(room);
            if (next === memberKey) return;
            memberKey = next;
            window.clearTimeout(memberTimer);
            memberTimer = window.setTimeout(function () {
                loadMap(true);
            }, 500);
        }

        canvas.addEventListener("pointerdown", onPointerDown);
        canvas.addEventListener("pointermove", onPointerMove);
        canvas.addEventListener("pointerup", endPointer);
        canvas.addEventListener("pointercancel", endPointer);
        canvas.addEventListener("wheel", onWheel, { passive: false });
        canvas.addEventListener("contextmenu", onContextMenu);
        document.addEventListener("pointerdown", onDocumentPointerDown);
        document.addEventListener("keydown", onKeyDown);
        fogToggle.addEventListener("click", function () {
            fogHidden = !fogHidden;
            updateFogToggle();
            scheduleDraw();
        });

        panel.addEventListener("click", function (event) {
            var target = event.target;
            if (!(target instanceof HTMLElement)) return;
            if (target.closest("[data-vmap-close]")) {
                hidePanel();
                return;
            }
            if (target.closest("[data-vmap-save-map]")) {
                saveMapSettings();
                return;
            }
            if (target.closest("[data-vmap-switch-scene]")) {
                switchScene();
                return;
            }
            if (target.closest("[data-vmap-create-scene]")) {
                createScene(false);
                return;
            }
            if (target.closest("[data-vmap-copy-scene]")) {
                createScene(true);
                return;
            }
            if (target.closest("[data-vmap-add-token]")) {
                addToken();
                return;
            }
            if (target.closest("[data-vmap-save-lights]")) {
                saveSceneLights();
                return;
            }
            if (target.closest("[data-vmap-import-map]")) {
                importMap();
                return;
            }
            if (target.closest("[data-vmap-save-calibration]")) {
                saveCalibration();
            }
        });
        panel.addEventListener("change", function (event) {
            var target = event.target;
            if (target instanceof HTMLElement && target.closest("[data-vmap-player-move]")) {
                savePlayerMovePermission(Boolean(target.checked));
            }
        });

        unsubscribers.push(ctx.events.on("camera", function () {
            scheduleDraw();
        }));
        unsubscribers.push(ctx.events.on("resize", function () {
            scheduleDraw();
        }));
        unsubscribers.push(ctx.events.on("grid", function () {
            scheduleDraw();
        }));
        unsubscribers.push(ctx.events.on("combat", function (state) {
            combat = state && state.active ? state : { active: false };
            renderTurnBar();
            scheduleDraw();
        }));
        unsubscribers.push(ctx.events.on("members", function (room) {
            onMembers(room);
        }));
        unsubscribers.push(ctx.events.on("tool", function (detail) {
            var tool = detail && detail.tool;
            if (tool === "scene" || tool === "token") openPanel(tool === "token" ? "token" : "scene");
        }));

        if (typeof ResizeObserver !== "undefined") {
            var observer = new ResizeObserver(function () {
                scheduleDraw();
            });
            observer.observe(host);
            unsubscribers.push(function () {
                observer.disconnect();
            });
        }

        resizeCanvas();
        attachSocket();
        memberKey = memberSignature(ctx.room);
        loadMap(false).then(function () {
            showHint("拖动棋子移动；房主可用顶部场景按钮编辑地图。", 6000);
        });
        renderTurnBar();
        scheduleDraw();
        pollTimer = window.setInterval(function () {
            if (destroyed || document.visibilityState === "hidden") return;
            if (!socket && window.TrpgSocket) {
                attachSocket();
                loadMap(true);
                return;
            }
            // socket 存在但已断开时，轮询同样要重新拉取服务端状态。
            if (!window.TrpgSocket || (socket && socket.connected === false)) loadMap(true);
        }, POLL_INTERVAL);

        return {
            destroy: function () {
                destroyed = true;
                window.clearInterval(pollTimer);
                window.clearTimeout(memberTimer);
                window.clearTimeout(hintTimer);
                detachSocket();
                unsubscribers.forEach(function (unsubscribe) {
                    try {
                        if (typeof unsubscribe === "function") unsubscribe();
                    } catch (error) {
                        // 插件卸载失败不能影响核心布局切换。
                    }
                });
                unsubscribers = [];
                canvas.removeEventListener("pointerdown", onPointerDown);
                canvas.removeEventListener("pointermove", onPointerMove);
                canvas.removeEventListener("pointerup", endPointer);
                canvas.removeEventListener("pointercancel", endPointer);
                canvas.removeEventListener("wheel", onWheel);
                canvas.removeEventListener("contextmenu", onContextMenu);
                document.removeEventListener("pointerdown", onDocumentPointerDown);
                document.removeEventListener("keydown", onKeyDown);
                menu.remove();
                panel.remove();
                turnbar.remove();
                hint.remove();
                fogToggle.remove();
                canvas.remove();
                host.classList.remove("ws-plugin-map");
            },
        };
    }

    window.TrpgStagePlugins = window.TrpgStagePlugins || {};
    window.TrpgStagePlugins["veil-map"] = window.TrpgStagePlugins["veil-map"] || {};
    window.TrpgStagePlugins["veil-map"].mountMap = mountMap;
})();
