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
            '<div class="vmap-section-label">地图</div>',
            '<label class="vmap-field">网格尺寸（px）',
            '<input type="number" min="' + MIN_GRID + '" max="' + MAX_GRID + '" step="8" data-vmap-grid></label>',
            '<label class="vmap-field">地图边界（格）',
            '<input type="number" min="' + MIN_BOUNDS + '" max="' + MAX_BOUNDS + '" step="1" data-vmap-bounds></label>',
            '<label class="vmap-field">背景图地址',
            '<input type="url" maxlength="2048" placeholder="https://… 或 /assets/…" data-vmap-bg></label>',
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
        var panelCalibration = panel.querySelector("[data-vmap-calibration]");
        var panelCalibrationInfo = panel.querySelector("[data-vmap-calibration-info]");
        var panelCellsAcross = panel.querySelector("[data-vmap-cells-across]");
        var panelOffsetX = panel.querySelector("[data-vmap-offset-x]");
        var panelOffsetY = panel.querySelector("[data-vmap-offset-y]");

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
            return String(token.owner_user_id || "") === String(ctx.user.id || "");
        }

        function tokenRect(token) {
            var grid = gridSize();
            var camera = ctx.getCamera();
            var point = ctx.worldToScreen(Number(token.x) || 0, Number(token.y) || 0);
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
            var lights = Array.isArray(mapState.lights) ? mapState.lights : [];
            context.save();
            lights.forEach(function (light) {
                var point = ctx.worldToScreen(Number(light.x) || 0, Number(light.y) || 0);
                var radius = Math.max(1, Number(light.range_cells) || 0) * grid * camera.zoom;
                if (radius <= 2) return;
                var color = String(light.color || "#ffaa00");
                var intensity = clamp(Number(light.intensity) || 0, 0, 1.5);
                var gradient = context.createRadialGradient(point.x, point.y, 0, point.x, point.y, radius);
                gradient.addColorStop(0, hexToRgba(color, 0.24 * intensity));
                gradient.addColorStop(0.55, hexToRgba(color, 0.10 * intensity));
                gradient.addColorStop(1, hexToRgba(color, 0));
                context.fillStyle = gradient;
                context.beginPath();
                context.arc(point.x, point.y, radius, 0, Math.PI * 2);
                context.fill();
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

        function draw() {
            if (!context || destroyed) return;
            var size = resizeCanvas();
            var camera = ctx.getCamera();
            var list = activeTokens();
            context.clearRect(0, 0, size.width, size.height);
            drawSceneLayers(camera);
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

        function applyPayload(data) {
            if (!data || !data.map) return;
            // 拖拽期间不替换地图状态：松手后用写回执（成功或冲突）统一对齐，
            // 避免正在拖的棋子从列表里消失。
            if (dragging) return;
            mapState = data.map;
            if (data.combat && typeof data.combat === "object") {
                combat = data.combat.active ? data.combat : { active: false };
            }
            if (typeof data.can_manage === "boolean") serverCanManage = data.can_manage;
            if (!tokens().some(function (token) { return String(token.id) === selectedId; })) selectedId = "";
            ctx.setBaseStage({ background: mapState.background || "", gridSize: gridSize() });
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

        function persistMove(token, previousX, previousY) {
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" + encodeURIComponent(token.id) + "/move", writeBody({
                x: token.x,
                y: token.y,
            })).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    if (!response || !response.data || !response.data.map) {
                        token.x = previousX;
                        token.y = previousY;
                        scheduleDraw();
                    }
                    applyWriteFailure(response, "棋子移动失败。");
                    return;
                }
                applyPayload(response.data);
            }).catch(function (error) {
                console.error("veil-map move failed", error);
                token.x = previousX;
                token.y = previousY;
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
                var meta = mapState && mapState.background_meta;
                renderImportInfo(meta && meta.import ? meta : null);
            }
            if (mode === "token" && panelName) panelName.focus();
        }

        function saveMapSettings() {
            var grid = clamp(asInt(panelGrid.value, gridSize()), MIN_GRID, MAX_GRID);
            var bounds = clamp(asInt(panelBounds.value, DEFAULT_BOUNDS), MIN_BOUNDS, MAX_BOUNDS);
            apiRequest("PUT", "/rooms/" + encodeURIComponent(ctx.room.id) + "/map", writeBody({
                grid_size: grid,
                bounds_cells: bounds,
                background: String(panelBackground.value || "").trim(),
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
                x: offset.x + Math.round((world.x - offset.x) / grid) * grid,
                y: offset.y + Math.round((world.y - offset.y) / grid) * grid,
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
                    notify("你只能移动自己绑定的棋子。", "error");
                    return;
                }
                if (combat && combat.active === true && combatantForToken(token) && !isCurrentActor(token)) {
                    notify("现在轮到「" + String(combat.current || "其他角色") + "」行动，不能移动「" + String(token.name || "该棋子") + "」。", "error");
                    return;
                }
                var world = ctx.screenToWorld(point.x, point.y);
                dragging = {
                    token: token,
                    pointerId: event.pointerId,
                    offsetX: world.x - (Number(token.x) || 0),
                    offsetY: world.y - (Number(token.y) || 0),
                    previousX: Number(token.x) || 0,
                    previousY: Number(token.y) || 0,
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
                var nextX = offset.x + Math.round((world.x - dragging.offsetX - offset.x) / grid) * grid;
                var nextY = offset.y + Math.round((world.y - dragging.offsetY - offset.y) / grid) * grid;
                if (nextX !== dragging.token.x || nextY !== dragging.token.y) {
                    dragging.token.x = nextX;
                    dragging.token.y = nextY;
                    dragging.moved = nextX !== dragging.previousX || nextY !== dragging.previousY;
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
                var previousX = dragging.previousX;
                var previousY = dragging.previousY;
                dragging = null;
                releasePointer(event.pointerId);
                if (moved) persistMove(token, previousX, previousY);
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
            applyPayload({ map: payload.map, combat: combat, can_manage: serverCanManage });
            var move = payload.move;
            if (move && move.source === "ai") {
                var target = move.to || {};
                var text = String(move.actor || "KP") + " 将「" + String(move.token_name || "棋子") +
                    "」移动到 (" + asInt(target.cell_x, 0) + ", " + asInt(target.cell_y, 0) + ")";
                if (move.reason) text += "：" + String(move.reason);
                notify(text, "info");
            }
        }

        function attachSocket() {
            socket = window.TrpgSocket || null;
            if (!socket) return;
            socketHandler = onSocketMapChanged;
            socket.on("room_map_changed", socketHandler);
        }

        function detachSocket() {
            if (socket && socketHandler && typeof socket.off === "function") {
                socket.off("room_map_changed", socketHandler);
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
            if (target.closest("[data-vmap-add-token]")) {
                addToken();
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

        unsubscribers.push(ctx.events.on("camera", function () {
            scheduleDraw();
        }));
        unsubscribers.push(ctx.events.on("resize", function () {
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
            if (!window.TrpgSocket) loadMap(true);
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
                canvas.remove();
                host.classList.remove("ws-plugin-map");
            },
        };
    }

    window.TrpgStagePlugins = window.TrpgStagePlugins || {};
    window.TrpgStagePlugins["veil-map"] = window.TrpgStagePlugins["veil-map"] || {};
    window.TrpgStagePlugins["veil-map"].mountMap = mountMap;
})();
