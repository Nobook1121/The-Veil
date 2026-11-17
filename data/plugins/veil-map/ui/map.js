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

    function apiRequest(method, path, body) {
        var api = window.TrpgApi;
        if (api) {
            if (method === "GET") return api.get(API_BASE + path);
            if (method === "PUT") return api.put(API_BASE + path, body);
            if (method === "POST") return api.post(API_BASE + path, body);
            if (method === "DELETE") return api.del(API_BASE + path);
        }
        var options = { method: method, headers: { Accept: "application/json" } };
        if (body !== undefined) {
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

        function notify(message, type) {
            ctx.notify(String(message || ""), type || "info");
        }

        function gridSize() {
            return clamp(asInt(mapState && mapState.grid_size, ctx.getGridSize()), MIN_GRID, MAX_GRID);
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

        function draw() {
            if (!context || destroyed) return;
            var size = resizeCanvas();
            var camera = ctx.getCamera();
            var list = activeTokens();
            context.clearRect(0, 0, size.width, size.height);
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
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" + encodeURIComponent(token.id) + "/move", {
                x: token.x,
                y: token.y,
            }).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    token.x = previousX;
                    token.y = previousY;
                    scheduleDraw();
                    notify((response && response.message) || "棋子移动失败。", "error");
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
            apiRequest("DELETE", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens/" + encodeURIComponent(token.id)).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    notify((response && response.message) || "删除棋子失败。", "error");
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
            if (mode === "token" && panelName) panelName.focus();
        }

        function saveMapSettings() {
            var grid = clamp(asInt(panelGrid.value, gridSize()), MIN_GRID, MAX_GRID);
            var bounds = clamp(asInt(panelBounds.value, DEFAULT_BOUNDS), MIN_BOUNDS, MAX_BOUNDS);
            apiRequest("PUT", "/rooms/" + encodeURIComponent(ctx.room.id) + "/map", {
                grid_size: grid,
                bounds_cells: bounds,
                background: String(panelBackground.value || "").trim(),
            }).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    notify((response && response.message) || "地图设置保存失败。", "error");
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
            apiRequest("POST", "/rooms/" + encodeURIComponent(ctx.room.id) + "/tokens", {
                name: name,
                x: Math.round(world.x / grid) * grid,
                y: Math.round(world.y / grid) * grid,
                w: clamp(asInt(panelWidth.value, 1), 1, 4),
                h: clamp(asInt(panelHeight.value, 1), 1, 4),
            }).then(function (response) {
                if (destroyed) return;
                if (!response || response.success !== true) {
                    notify((response && response.message) || "添加棋子失败。", "error");
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
                var nextX = Math.round((world.x - dragging.offsetX) / grid) * grid;
                var nextY = Math.round((world.y - dragging.offsetY) / grid) * grid;
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
