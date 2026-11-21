/**
 * dnd5e-vtt 插件前端：D&D 5e 战桌右侧栏六面板 + 画布左上工具组。
 *
 * 挂载：E0 ``room.rail.right`` 槽。核心 workspace 把本脚本作为同源资源加载，
 * 调用 ``mountRail(host, ctx)``；布局沿用 dnd5e 规则集声明的 dnd5e-canvas。
 *
 * 六面板（FVTT 顺序：聊天 → 战斗 → 场景 → 角色 → 物品 → 日志）：
 * - 聊天：桥接核心聊天 DOM（data-room-panel="chat" 时核心 CSS 把现有
 *   ``.home-chat-main`` 停靠进侧栏），不另写一套消息协议；
 * - 战斗：``ctx.events`` 的 combat 事件 + ``/panels`` 快照渲染先攻列表，
 *   未开战给空态；掷骰与操作沿用房间现有战斗接口；
 * - 场景：DM 视图占位（服务端不下发给玩家 → 玩家端 tab 不渲染）；
 * - 角色：玩家角色目录（成员卡）+ NPC/怪物分组（仅 DM 响应携带 npc 键）；
 * - 物品：占位目录；
 * - 日志：房间事件流尾部（玩家侧为服务端过滤后的公开事件）。
 *
 * 画布工具组（M3 第一段，``mountRail`` 成功拉到 /panels 后挂载到
 * ``#wsStageHost``）：选择/移动（默认）↔ 测距尺 ↔ 模板四型（圆/锥/线/方）
 * + 清空。学 FVTT 交互：点击定起点、拖出尺寸、松手成型；吸附格点；
 * 拖动中显示尺数标注；Shift+滚轮旋转、Ctrl+滚轮微调（锥/线）；已放置
 * 模板可拖动、右键删除。受影响格高亮优先调 veil-map 公开空间端点
 * （圆形/线形取格、测距判墙），锥形（53.13°）与方形无对应原语、按本地
 * 几何近似并明说。**模板按钮与清空只对 DM 渲染**（观察者角色来自服务端
 * /panels 裁剪）；玩家可测距、只读展示模板；选中棋子弹操作卡：DM 拉动作
 * 清单（服务端 token-actions），玩家响应不含 actions 键 → 按钮不渲染。
 *
 * 面板清单从 ``/api/plugins/dnd5e-vtt/panels?room=…`` 拉取，观察者裁剪
 * 全部在服务端完成，前端只消费；veil-map 缺失时面板照常工作、模板按
 * 本地几何近似渲染。
 */
(function () {
    "use strict";

    var API_BASE = "/api/plugins/dnd5e-vtt";
    var POLL_INTERVAL = 30000;
    // FVTT 右栏顺序；scenes 只在服务端下发时才渲染 tab。
    var PANEL_ORDER = ["chat", "combat", "scenes", "characters", "items", "journal"];
    var PANEL_TITLES = {
        chat: "聊天",
        combat: "战斗",
        scenes: "场景",
        characters: "角色",
        items: "物品",
        journal: "日志"
    };
    var PANEL_ICONS = {
        chat: "fa fa-comment",
        combat: "fa fa-shield",
        scenes: "fa fa-map-o",
        characters: "fa fa-users",
        items: "fa fa-briefcase",
        journal: "fa fa-book"
    };
    var KIND_LABELS = {
        player_decision: "玩家决定",
        state_update: "状态更新",
        event: "事件"
    };

    function apiRequest(method, path, body) {
        // path 以 /api/ 开头时按绝对地址请求（跨插件公开端点），否则拼本插件前缀。
        var url = path.indexOf("/api/") === 0 ? path : API_BASE + path;
        var api = window.TrpgApi;
        if (api) {
            if (method === "GET") return api.get(url);
            if (method === "PUT") return api.put(url, body);
            if (method === "POST") return api.post(url, body);
            if (method === "DELETE") return api.del(url);
        }
        var options = { method: method, headers: { Accept: "application/json" } };
        var csrf = window.currentUser && window.currentUser.csrf_token;
        if (csrf && method !== "GET") options.headers["X-CSRF-Token"] = csrf;
        if (body !== undefined) {
            options.headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () {
                return { success: false, message: "服务器返回了无效响应。" };
            });
        });
    }

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function asInt(value, fallback) {
        var number = parseInt(value, 10);
        return Number.isFinite(number) ? number : fallback;
    }

    function asFloat(value, fallback) {
        var number = parseFloat(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function hexToRgba(value, alpha) {
        var text = String(value || "").replace("#", "");
        if (!/^[0-9a-fA-F]{6}$/.test(text)) text = "e06666";
        var red = parseInt(text.slice(0, 2), 16);
        var green = parseInt(text.slice(2, 4), 16);
        var blue = parseInt(text.slice(4, 6), 16);
        return "rgba(" + red + ", " + green + ", " + blue + ", " + clamp(alpha, 0, 1) + ")";
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

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function mountRail(host, ctx) {
        if (!host || !ctx || !ctx.room || !ctx.room.id) return { destroy: function () {} };
        var body = document.getElementById("homeChatBody");
        var tabStrip = document.getElementById("wsSidebarTabs");
        if (!body || !tabStrip) return { destroy: function () {} };

        var destroyed = false;
        var roomId = String(ctx.room.id);
        var panelsData = null;
        var panelsSignature = "";
        var combatState = null;
        var activePanel = "chat";
        var tabs = {};
        var panelBodies = {};
        var unsubscribers = [];
        var pollTimer = 0;
        var observer = null;
        var stageTools = null;

        host.classList.add("vtt-rail-host");

        var shell = document.createElement("div");
        shell.className = "vtt-rail";
        shell.innerHTML = '<div class="vtt-loading">面板数据加载中…</div>';
        host.appendChild(shell);

        function ensureStageTools(roomObserver) {
            // 工具组的观察者角色由服务端 /panels 裁剪结果决定；角色变化
            // （例如成员被提为房管）时重建一次。
            if (destroyed) return;
            if (stageTools) {
                if (stageTools.observer === roomObserver) return;
                try {
                    stageTools.destroy();
                } catch (error) {
                    console.warn("dnd5e-vtt stage tools remount failed", error);
                }
                stageTools = null;
            }
            stageTools = mountStageTools(ctx, roomObserver);
        }

        function findPanel(id) {
            var panels = (panelsData && panelsData.panels) || [];
            for (var index = 0; index < panels.length; index += 1) {
                if (panels[index] && panels[index].id === id) return panels[index];
            }
            return null;
        }

        function roomPanelValue() {
            return String(body.dataset.roomPanel || "chat");
        }

        function ensurePanelBody(id) {
            if (panelBodies[id]) return panelBodies[id];
            var el = document.createElement("section");
            el.className = "vtt-panel";
            el.setAttribute("data-vtt-panel-body", id);
            el.hidden = true;
            shell.appendChild(el);
            panelBodies[id] = el;
            return el;
        }

        function panelHeader(title, extraHtml) {
            return '<header class="vtt-panel-head"><h3>' + escapeHtml(title) + "</h3>" + (extraHtml || "") + "</header>";
        }

        function noteBox(text) {
            return '<p class="vtt-note">' + escapeHtml(text) + "</p>";
        }

        function emptyState(icon, title, hint) {
            return (
                '<div class="vtt-empty">' +
                '<i class="' + escapeHtml(icon) + '" aria-hidden="true"></i>' +
                "<strong>" + escapeHtml(title) + "</strong>" +
                "<p>" + escapeHtml(hint) + "</p>" +
                "</div>"
            );
        }

        function sidebarCollapsed() {
            return body.dataset.wsSidebar === "collapsed";
        }

        function wideMode() {
            var size = body.dataset.wsSize || "wide";
            return size === "wide" || size === "medium";
        }

        function expandSidebarIfNeeded() {
            // 折叠成 48px 图标栏时点面板：先展开侧栏（学核心角色/设置 tab 的行为）。
            if (wideMode() && sidebarCollapsed() && window.TrpgWorkspace && typeof window.TrpgWorkspace.toggleSidebar === "function") {
                window.TrpgWorkspace.toggleSidebar();
            }
        }

        function activateChat() {
            // 聊天面板 = 核心聊天 DOM：走核心 setPanel 拿到停靠/浮层/抽屉逻辑，
            // 再兜底写一次 data-room-panel（核心早退分支不写该属性）。
            if (window.TrpgWorkspace && typeof window.TrpgWorkspace.setPanel === "function") {
                try {
                    window.TrpgWorkspace.setPanel("chat");
                } catch (error) {
                    // 核心 API 异常时仍直接写属性，保证面板可切换。
                }
            }
            body.dataset.roomPanel = "chat";
        }

        function activatePanel(id) {
            if (id === "chat") {
                activateChat();
                return;
            }
            var value = "vtt:" + id;
            if (roomPanelValue() === value) {
                expandSidebarIfNeeded();
                return;
            }
            expandSidebarIfNeeded();
            body.dataset.roomPanel = value;
        }

        function syncFromRoomPanel() {
            var value = roomPanelValue();
            var next = "chat";
            if (value.indexOf("vtt:") === 0) {
                var candidate = value.slice(4);
                if (tabs[candidate]) next = candidate;
            }
            activePanel = next;
            Object.keys(tabs).forEach(function (id) {
                var active = id === activePanel;
                tabs[id].classList.toggle("is-active", active);
                tabs[id].setAttribute("aria-selected", active ? "true" : "false");
            });
            Object.keys(panelBodies).forEach(function (id) {
                panelBodies[id].hidden = id !== activePanel;
            });
            shell.setAttribute("data-vtt-active", activePanel);
        }

        function combatView() {
            // 优先用核心战斗事件（含先攻/HP/当前回合），回落 /panels 的目录快照。
            if (combatState && combatState.active) {
                var order = Array.isArray(combatState.order) ? combatState.order : [];
                return {
                    active: true,
                    round: combatState.round || 1,
                    current: String(combatState.current || ""),
                    order: order
                };
            }
            var panel = findPanel("combat");
            if (panel && panel.state && panel.state.active) return panel.state;
            return { active: false };
        }

        function combatRow(item, current) {
            var name = typeof item === "string" ? item : String((item && item.name) || "");
            var initiative = "";
            var hp = "";
            if (item && typeof item === "object") {
                if (item.initiative != null) initiative = String(item.initiative);
                if (item.hp != null) hp = String(item.hp) + (item.max_hp != null ? "/" + String(item.max_hp) : "");
            }
            var classes = ["vtt-init-item"];
            if (current && name && name === current) classes.push("is-current");
            if (item && item.is_player) classes.push("is-player");
            return (
                '<li class="' + classes.join(" ") + '">' +
                '<span class="vtt-init-roll">' + escapeHtml(initiative || "–") + "</span>" +
                '<span class="vtt-init-name">' + escapeHtml(name || "？") + "</span>" +
                '<span class="vtt-init-hp">' + escapeHtml(hp) + "</span>" +
                "</li>"
            );
        }

        function renderCombat() {
            var el = ensurePanelBody("combat");
            var view = combatView();
            if (!view.active) {
                el.innerHTML =
                    panelHeader("战斗") +
                    emptyState(
                        "fa fa-shield",
                        "尚未开始战斗",
                        "战斗由 AI DM 或房主发起后，先攻次序、当前回合与 HP 会显示在这里；掷骰与回合操作沿用房间现有战斗流程。"
                    ) +
                    noteBox("接线点：面板数据来自现有战斗状态接口，后续刀接入掷先攻与回合步进按钮。");
                return;
            }
            var rows = (view.order || []).map(function (item) {
                return combatRow(item, view.current);
            }).join("");
            el.innerHTML =
                panelHeader("战斗 · 第 " + escapeHtml(view.round || 1) + " 轮") +
                '<ol class="vtt-initiative">' + rows + "</ol>" +
                noteBox("当前行动：" + escapeHtml(view.current || "—"));
        }

        function playerRow(player) {
            var initial = String(player.name || player.username || "?").slice(0, 1) || "?";
            var image = player.avatar
                ? '<img src="' + escapeHtml(player.avatar) + '" alt="" loading="lazy">'
                : "<span>" + escapeHtml(initial) + "</span>";
            var meta = [player.race, player.class ? player.class + " · Lv." + (player.level || "1") : ""]
                .filter(Boolean)
                .join(" · ");
            return (
                '<li class="vtt-member" data-vtt-user="' + escapeHtml(player.user_id || "") + '"' +
                (player.online ? ' data-online="true"' : "") + ">" +
                '<span class="vtt-member-avatar">' + image + "</span>" +
                '<span class="vtt-member-main"><strong>' + escapeHtml(player.name || "（未建卡）") + "</strong>" +
                "<small>" + escapeHtml(meta || player.username || "") + "</small></span>" +
                '<i class="vtt-member-dot" aria-hidden="true"></i>' +
                "</li>"
            );
        }

        function npcRow(item) {
            return (
                '<li class="vtt-member vtt-npc-item">' +
                '<span class="vtt-member-avatar"><i class="fa fa-user-secret" aria-hidden="true"></i></span>' +
                '<span class="vtt-member-main"><strong>' + escapeHtml(item.title || "未命名") + "</strong>" +
                "<small>" + escapeHtml(item.summary || "") + "</small></span>" +
                "</li>"
            );
        }

        function renderCharacters() {
            var el = ensurePanelBody("characters");
            var panel = findPanel("characters") || { players: [] };
            var html = panelHeader("角色");
            var players = panel.players || [];
            if (!players.length) {
                html += emptyState("fa fa-users", "暂无玩家角色", "房间成员建卡并携带角色卡后，会列在这里。");
            } else {
                html += '<ul class="vtt-directory">' + players.map(playerRow).join("") + "</ul>";
            }
            // npc 分组只存在于 DM 响应；玩家响应没有这个键（服务端裁剪）。
            if (panel.npc) {
                html += '<h4 class="vtt-group-title">NPC 与怪物<span>仅 DM 可见</span></h4>';
                if (!(panel.npc.items || []).length) {
                    html += emptyState("fa fa-user-secret", "暂无 NPC 模块", panel.npc.hint || "导入世界包后，怪物与 NPC 会列在这里。");
                } else {
                    html += '<ul class="vtt-directory">' + (panel.npc.items || []).map(npcRow).join("") + "</ul>";
                }
            }
            el.innerHTML = html;
        }

        function renderScenes() {
            var el = ensurePanelBody("scenes");
            var panel = findPanel("scenes");
            if (!panel) {
                // 防御性兜底：玩家响应本就不该出现该 tab，更不该能打开面板。
                el.innerHTML = panelHeader("场景") + emptyState("fa fa-map-o", "场景目录不可用", "场景面板仅对 DM 开放。");
                return;
            }
            el.innerHTML =
                panelHeader("场景") +
                emptyState("fa fa-map-o", "场景列表待接入", panel.hint || "场景列表将在地图插件接入后列出。") +
                noteBox("接线点：M3 从地图插件（veil-map）场景存储拉取列表，支持切换场景与把玩家拉入场景。");
        }

        function renderItems() {
            var el = ensurePanelBody("items");
            var panel = findPanel("items") || {};
            el.innerHTML =
                panelHeader("物品") +
                emptyState("fa fa-briefcase", "物品目录待接入", panel.hint || "物品与战利品目录将在角色卡装备与剧本数据接入后列出。");
        }

        function renderJournal() {
            var el = ensurePanelBody("journal");
            var panel = findPanel("journal") || { entries: [] };
            var entries = panel.entries || [];
            var html = panelHeader("日志");
            if (!entries.length) {
                html += emptyState("fa fa-book", "暂无事件记录", "跑团过程中的地图、战斗与检定事件会按观察者权限记录在这里。");
            } else {
                html +=
                    '<ul class="vtt-log">' +
                    entries
                        .map(function (entry) {
                            var kind = KIND_LABELS[entry.kind] || entry.kind || "事件";
                            return (
                                '<li class="vtt-log-item">' +
                                '<span class="vtt-log-time">' + escapeHtml(entry.created_at || "") + "</span>" +
                                '<span class="vtt-log-kind">' + escapeHtml(kind) + "</span>" +
                                '<span class="vtt-log-content">' + escapeHtml(entry.content || "") + "</span>" +
                                "</li>"
                            );
                        })
                        .join("") +
                    "</ul>";
            }
            el.innerHTML = html;
        }

        function renderAllPanels() {
            renderCombat();
            renderCharacters();
            renderScenes();
            renderItems();
            renderJournal();
        }

        function buildTabs() {
            var available = (panelsData && panelsData.panels || []).map(function (panel) {
                return panel && panel.id;
            });
            PANEL_ORDER.forEach(function (id) {
                if (tabs[id]) return;
                if (id !== "chat" && available.indexOf(id) < 0) return;
                var button = document.createElement("button");
                button.type = "button";
                button.className = "ws-sidebar-tab vtt-tab";
                button.setAttribute("data-vtt-panel", id);
                button.setAttribute("role", "tab");
                button.setAttribute("aria-selected", "false");
                button.title = PANEL_TITLES[id];
                button.innerHTML =
                    '<i class="' + PANEL_ICONS[id] + '" aria-hidden="true"></i><span>' + PANEL_TITLES[id] + "</span>";
                button.addEventListener("click", function () {
                    activatePanel(id);
                });
                tabStrip.appendChild(button);
                tabs[id] = button;
            });
            // 面板 tab 就绪后才接管核心 tab（聊天/角色由本插件同名面板替代，
            // 设置保留给核心），失败时不加该标记、核心三 tab 照常可用。
            body.dataset.vttMounted = "true";
        }

        function renderLoadError() {
            // innerHTML 整体替换会移除旧面板容器，引用一并作废。
            panelBodies = {};
            shell.innerHTML =
                panelHeader("战桌") +
                emptyState("fa fa-television", "面板数据加载失败", "战桌以空态运行；地图与聊天不受影响，可稍后重试。") +
                '<div class="vtt-actions"><button type="button" class="vtt-retry">重试</button></div>';
            var retry = shell.querySelector(".vtt-retry");
            if (retry) {
                retry.addEventListener("click", function () {
                    void loadPanels();
                });
            }
        }

        function loadPanels() {
            return apiRequest("GET", "/panels?room=" + encodeURIComponent(roomId)).then(function (response) {
                if (destroyed) return;
                if (!response || !response.success || !response.data) {
                    renderLoadError();
                    return;
                }
                var signature = JSON.stringify(response.data);
                if (signature === panelsSignature) return;
                panelsSignature = signature;
                panelsData = response.data;
                var loading = shell.querySelector(".vtt-loading");
                if (loading) loading.remove();
                buildTabs();
                renderAllPanels();
                syncFromRoomPanel();
                ensureStageTools(String(response.data.observer || "player"));
            }).catch(function () {
                if (!destroyed) renderLoadError();
            });
        }

        function syncOnline(payload) {
            var members = (payload && payload.members) || (payload && payload.room && payload.room.members) || [];
            if (!members.length) return;
            var map = {};
            members.forEach(function (member) {
                map[String(member.user_id)] = member.is_online === true;
            });
            shell.querySelectorAll(".vtt-member[data-vtt-user]").forEach(function (el) {
                if (map[el.getAttribute("data-vtt-user")] === true) {
                    el.setAttribute("data-online", "true");
                } else {
                    el.removeAttribute("data-online");
                }
            });
        }

        // 房间切换时核心会把当前战斗快照与成员重新推给插件。
        unsubscribers.push(
            ctx.events.on("combat", function (payload) {
                combatState = payload || null;
                renderCombat();
            })
        );
        unsubscribers.push(
            ctx.events.on("members", function (payload) {
                syncOnline(payload);
            })
        );

        observer = new MutationObserver(function () {
            syncFromRoomPanel();
        });
        observer.observe(body, { attributes: true, attributeFilter: ["data-room-panel"] });

        // 挂载时归一：核心「角色」tab 已被本插件接管，若正处于该面板则回到聊天。
        if (roomPanelValue() === "character") activateChat();
        syncFromRoomPanel();
        void loadPanels();
        pollTimer = window.setInterval(function () {
            if (destroyed || document.visibilityState === "hidden") return;
            void loadPanels();
        }, POLL_INTERVAL);

        return {
            destroy: function () {
                destroyed = true;
                window.clearInterval(pollTimer);
                if (stageTools) {
                    try {
                        stageTools.destroy();
                    } catch (error) {
                        console.warn("dnd5e-vtt stage tools destroy failed", error);
                    }
                    stageTools = null;
                }
                if (observer) observer.disconnect();
                unsubscribers.forEach(function (unsubscribe) {
                    try {
                        if (typeof unsubscribe === "function") unsubscribe();
                    } catch (error) {
                        // 插件卸载失败不能影响核心布局切换。
                    }
                });
                unsubscribers = [];
                Object.keys(tabs).forEach(function (id) {
                    tabs[id].remove();
                    delete tabs[id];
                });
                panelBodies = {};
                delete body.dataset.vttMounted;
                shell.remove();
                host.classList.remove("vtt-rail-host");
            }
        };
    }


/* ==========================================================================
   dnd5e-vtt 画布左上工具组 + 模板/测距尺/棋子操作卡（M3 第一段）。

   由 ``mountRail`` 在面板数据加载成功后调用 ``ensureStageTools``——观察者
   角色（DM/玩家）来自服务端 ``/panels`` 裁剪结果，工具组据此决定渲染哪些
   按钮：**玩家视图只有选择与测距尺，模板四型与清空按钮不渲染（DOM 不出现，
   不是 CSS 隐藏）**；已放置模板对全员只读展示。

   空间几何职责划分（约束：veil-map 零改动、不复制它的几何实现）：

   - 圆形/线形受影响格 → 调 veil-map 公开端点
     ``POST /api/plugins/veil-map/rooms/<id>/spatial/range`` 取格；
     调用失败时本地按几何近似并提示一次（「空间服务不可用」）。
   - 锥形（默认 53.13°，FVTT 同款圆头张角）与方形没有对应的 veil-map
     原语（它的锥固定 90°、无方形形状），**本来就是本地几何近似**，
     放置锥形时明说一次。
   - 测距尺复用 ``POST /api/plugins/veil-map/rooms/<id>/spatial/line``
     判墙与读距离（玩家迷雾受限时服务端会拒绝，前端降级为直线读数）。
   ========================================================================== */

var VEIL_API_BASE = "/api/plugins/veil-map";
var FEET_PER_CELL = 5;
var MIN_GRID = 16;
var MAX_GRID = 256;
var MAX_TEMPLATE_CELLS = 100;
var CONE_DEFAULT_ANGLE = 53.13;
var TEMPLATE_COLORS = {
    circle: "#e06666",
    cone: "#f6b26b",
    line: "#6fa8dc",
    rect: "#93c47d"
};
var TEMPLATE_POLL = 20000;
var HIGHLIGHT_CACHE_LIMIT = 200;

var TOOL_DEFS = [
    { id: "select", icon: "fa fa-mouse-pointer", title: "选择 / 移动", dm: false },
    { id: "ruler", icon: "fa fa-arrows-h", title: "测距尺", dm: false },
    { id: "circle", icon: "fa fa-circle-o", title: "圆形模板", dm: true },
    { id: "cone", icon: "fa fa-play", title: "锥形模板", dm: true },
    { id: "line", icon: "fa fa-long-arrow-right", title: "线形模板", dm: true },
    { id: "rect", icon: "fa fa-square-o", title: "方形模板", dm: true }
];

var TOOL_HINTS = {
    select: "点击棋子查看操作卡，拖动移动；点选模板后可拖动/右键删除，Esc 取消。",
    ruler: "点击定起点，拖出测距；松手保留读数，Esc 清除。",
    circle: "点击定圆心，拖出半径；松手放置模板。",
    cone: "点击定顶点，拖出方向与距离；Shift+滚轮旋转，Ctrl+滚轮微调。",
    line: "点击定起点，拖出方向与长度；Shift+滚轮旋转，Ctrl+滚轮微调。",
    rect: "点击定角点，拖出宽高；松手放置模板。"
};

var CATEGORY_LABELS = {
    attacks: "攻击",
    spells: "法术",
    features: "特性",
    abilities: "能力",
    items: "物品"
};
var CATEGORY_ORDER = ["attacks", "spells", "features", "abilities", "items"];
var CATEGORY_EMPTY_HINTS = {
    attacks: "暂无攻击数据；开战后会从战斗状态生成攻击清单。",
    spells: "暂无法术数据。",
    features: "暂无特性数据；剧本 NPC 的特性文本尚未结构化。",
    abilities: "暂无能力数据。",
    items: "暂无物品数据。"
};

function rotateUnit(unit, degrees) {
    var radians = (degrees * Math.PI) / 180;
    var cos = Math.cos(radians);
    var sin = Math.sin(radians);
    return {
        x: unit.x * cos - unit.y * sin,
        y: unit.x * sin + unit.y * cos
    };
}

function normalizeUnit(x, y) {
    var length = Math.hypot(x, y);
    if (!(length > 1e-9)) return { x: 1, y: 0 };
    return { x: x / length, y: y / length };
}

function templateUnit(template) {
    var direction = template && template.direction;
    return normalizeUnit(asFloat(direction && direction.x, 1), asFloat(direction && direction.y, 0));
}

function formatFeet(cells) {
    return String(Math.max(0, Math.round(cells)) * FEET_PER_CELL) + " 尺";
}

/**
 * 画布工具组挂载。返回 ``{observer, destroy}``；宿主缺失时返回 ``null``。
 */
function mountStageTools(ctx, roomObserver) {
    var stageHost = document.getElementById("wsStageHost");
    if (!stageHost) return null;
    var isDm = roomObserver === "dm";
    var roomId = String(ctx.room.id);
    var destroyed = false;

    var tool = "select";
    var templates = [];
    var templatesRevision = -1;
    var selectedTemplateId = "";
    var lastPlacedId = "";
    var placing = null;          // 拖出中的模板草稿
    var movingTemplate = null;   // 选中模板的拖动
    var ruler = null;            // 测距尺状态
    var mapState = null;         // veil-map 公开载荷的棋子副本
    var highlightCache = {};     // 已放置模板 → 受影响格
    var warnedSpatialOffline = false;
    var warnedConeLocal = false;
    var card = null;             // 棋子操作卡状态
    var pendingClick = null;
    var rotateTimer = 0;
    var rulerQueryTimer = 0;
    var drawScheduled = false;
    var pollTimer = 0;
    var socket = null;
    var socketHandlers = [];
    var unsubscribers = [];

    /* ------------------------------------------------------------ DOM */

    var root = document.createElement("div");
    root.className = "vtt-stage-tools";

    var toolbar = document.createElement("div");
    toolbar.className = "vtt-toolbar";
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "战桌工具");
    root.appendChild(toolbar);

    var toolButtons = {};
    TOOL_DEFS.forEach(function (def) {
        if (def.dm && !isDm) return; // 玩家视图：模板按钮不渲染
        var button = document.createElement("button");
        button.type = "button";
        button.className = "vtt-tool-btn";
        button.title = def.title;
        button.setAttribute("aria-label", def.title);
        button.setAttribute("data-vtt-tool", def.id);
        button.innerHTML = '<i class="' + def.icon + '" aria-hidden="true"></i>';
        button.addEventListener("click", function () {
            activateTool(def.id);
        });
        toolbar.appendChild(button);
        toolButtons[def.id] = button;
    });
    if (isDm) {
        var separator = document.createElement("span");
        separator.className = "vtt-tool-sep";
        separator.setAttribute("aria-hidden", "true");
        toolbar.appendChild(separator);
        var clearButton = document.createElement("button");
        clearButton.type = "button";
        clearButton.className = "vtt-tool-btn vtt-tool-danger";
        clearButton.title = "清空模板（仅 DM）";
        clearButton.setAttribute("aria-label", "清空模板");
        clearButton.innerHTML = '<i class="fa fa-trash-o" aria-hidden="true"></i>';
        clearButton.addEventListener("click", clearTemplates);
        toolbar.appendChild(clearButton);
    }

    var canvas = document.createElement("canvas");
    canvas.className = "vtt-canvas";
    canvas.setAttribute("aria-label", "测量模板与测距尺层");
    root.appendChild(canvas);

    var hintBar = document.createElement("div");
    hintBar.className = "vtt-tool-hint";
    root.appendChild(hintBar);

    stageHost.appendChild(root);
    var context = canvas.getContext("2d");

    var cardEl = document.createElement("div");
    cardEl.className = "vtt-token-card";
    cardEl.hidden = true;
    document.body.appendChild(cardEl);

    /* ------------------------------------------------------------ 基础换算 */

    function gridInfo() {
        var viewport = ctx.getViewport();
        var grid = clamp(asInt(viewport && viewport.gridSize, 64), MIN_GRID, MAX_GRID);
        var offset = (viewport && viewport.gridOffset) || {};
        return {
            grid: grid,
            x: clamp(asInt(offset.x, 0), -grid, grid),
            y: clamp(asInt(offset.y, 0), -grid, grid)
        };
    }

    function vertexFromScreen(screenX, screenY) {
        var info = gridInfo();
        var world = ctx.screenToWorld(screenX, screenY);
        return {
            cell_x: Math.round((world.x - info.x) / info.grid),
            cell_y: Math.round((world.y - info.y) / info.grid)
        };
    }

    function vertexToScreen(cellX, cellY) {
        var info = gridInfo();
        return ctx.worldToScreen(info.x + cellX * info.grid, info.y + cellY * info.grid);
    }

    function localPoint(event) {
        var rect = canvas.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function stagePoint(event) {
        var rect = stageHost.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    /* ------------------------------------------------------------ 网络请求 */

    function newRequestId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    }

    function applyTemplates(data) {
        if (!data || typeof data !== "object") return;
        if (Array.isArray(data.templates)) templates = data.templates;
        if (typeof data.revision === "number") templatesRevision = data.revision;
        if (selectedTemplateId && !findTemplate(selectedTemplateId)) selectedTemplateId = "";
        scheduleDraw();
    }

    function writeTemplates(action, extra, successMessage) {
        var body = extra || {};
        body.request_id = newRequestId();
        body.revision = Math.max(0, templatesRevision);
        body.action = action;
        return apiRequest("POST", "/rooms/" + encodeURIComponent(roomId) + "/templates", body)
            .then(function (response) {
                if (destroyed) return response;
                if (!response || response.success !== true) {
                    if (response && response.data && Array.isArray(response.data.templates)) {
                        applyTemplates(response.data);
                    }
                    notify(String((response && response.message) || "模板操作失败。"), "error");
                    return response;
                }
                applyTemplates(response.data);
                if (successMessage) notify(successMessage, "success");
                return response;
            })
            .catch(function (error) {
                console.error("dnd5e-vtt templates failed", error);
                if (!destroyed) notify("模板操作失败，请稍后重试。", "error");
                return null;
            });
    }

    function loadTemplates() {
        return apiRequest("GET", "/rooms/" + encodeURIComponent(roomId) + "/templates")
            .then(function (response) {
                if (destroyed || !response || !response.success) return;
                applyTemplates(response.data);
            })
            .catch(function (error) {
                console.error("dnd5e-vtt load templates failed", error);
            });
    }

    function loadMapTokens() {
        // veil-map 的公开 GET（观察者裁剪后的载荷）：只取棋子目录用于点选
        // 操作卡；模板绘制不依赖它。缺失是合法形态，静默降级。
        return apiRequest("GET", VEIL_API_BASE + "/rooms/" + encodeURIComponent(roomId) + "/map")
            .then(function (response) {
                if (destroyed || !response || !response.success || !response.data) return;
                var map = response.data.map;
                if (map && typeof map === "object") mapState = map;
            })
            .catch(function (error) {
                console.debug("dnd5e-vtt map payload unavailable", error);
            });
    }

    function notify(message, type) {
        ctx.notify(String(message || ""), type || "info");
    }

    /* ------------------------------------------------------------ 受影响格 */

    function templateHighlightKey(template) {
        return [
            template.kind,
            template.cell_x,
            template.cell_y,
            template.size_cells || 0,
            Math.abs(asInt(template.w_cells, 0)),
            Math.abs(asInt(template.h_cells, 0)),
            template.angle || 0,
            asFloat(template.direction && template.direction.x, 0).toFixed(4),
            asFloat(template.direction && template.direction.y, 0).toFixed(4)
        ].join("|");
    }

    function localCells(template) {
        // 本地几何近似（兜底与无原语形状专用）：格心相对模板原点的极坐标判定。
        var size = clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS);
        if (template.kind === "rect") {
            // 方形从角点拖出：格子本身就是精确的，无近似误差。
            var cells = [];
            var width = Math.abs(clamp(asInt(template.w_cells, 1), 1, MAX_TEMPLATE_CELLS));
            var height = Math.abs(clamp(asInt(template.h_cells, 1), 1, MAX_TEMPLATE_CELLS));
            var baseX = template.cell_x + Math.min(asInt(template.w_cells, 1), 0);
            var baseY = template.cell_y + Math.min(asInt(template.h_cells, 1), 0);
            for (var dx = 0; dx < width; dx += 1) {
                for (var dy = 0; dy < height; dy += 1) {
                    cells.push({ cell_x: baseX + dx, cell_y: baseY + dy });
                }
            }
            return cells;
        }
        if (template.kind === "cone") {
            // 锥（默认 53.13°）：veil-map 锥原语固定 90°，几何不匹配 → 本地近似。
            var half = ((asFloat(template.angle, CONE_DEFAULT_ANGLE) || CONE_DEFAULT_ANGLE) / 2) * Math.PI / 180;
            var unit = templateUnit(template);
            var coneCells = [];
            var bound = size + 1;
            for (var cx = template.cell_x - bound; cx <= template.cell_x + bound; cx += 1) {
                for (var cy = template.cell_y - bound; cy <= template.cell_y + bound; cy += 1) {
                    var rx = cx + 0.5 - template.cell_x;
                    var ry = cy + 0.5 - template.cell_y;
                    var distance = Math.hypot(rx, ry);
                    if (distance > size + 0.5) continue;
                    var projection = rx * unit.x + ry * unit.y;
                    if (projection <= 0.15) continue;
                    var perpendicular = Math.abs(rx * unit.y - ry * unit.x);
                    if (perpendicular > distance * Math.sin(half) + 0.25) continue;
                    coneCells.push({ cell_x: cx, cell_y: cy });
                }
            }
            return coneCells;
        }
        if (template.kind === "line") {
            var lineUnit = templateUnit(template);
            var lineCells = [];
            var reach = size + 1;
            for (var lx = template.cell_x - reach; lx <= template.cell_x + reach; lx += 1) {
                for (var ly = template.cell_y - reach; ly <= template.cell_y + reach; ly += 1) {
                    var lrx = lx + 0.5 - template.cell_x;
                    var lry = ly + 0.5 - template.cell_y;
                    var lineProjection = lrx * lineUnit.x + lry * lineUnit.y;
                    if (lineProjection <= 0 || lineProjection > size + 0.5) continue;
                    var linePerpendicular = Math.abs(lrx * lineUnit.y - lry * lineUnit.x);
                    if (linePerpendicular > 0.75) continue;
                    lineCells.push({ cell_x: lx, cell_y: ly });
                }
            }
            return lineCells;
        }
        // 圆形本地近似（服务不可用时的兜底）：格心到模板圆心 ≤ 半径 + 半格。
        var circleCells = [];
        for (var ox = template.cell_x - size; ox <= template.cell_x + size; ox += 1) {
            for (var oy = template.cell_y - size; oy <= template.cell_y + size; oy += 1) {
                var crx = ox + 0.5 - template.cell_x;
                var cry = oy + 0.5 - template.cell_y;
                if (Math.hypot(crx, cry) > size + 0.5) continue;
                circleCells.push({ cell_x: ox, cell_y: oy });
            }
        }
        return circleCells;
    }

    function highlightCells(template) {
        var key = templateHighlightKey(template);
        var cached = highlightCache[key];
        if (cached) return cached;
        var entry = { cells: [], approx: true, resolved: false };
        highlightCache[key] = entry;
        if (Object.keys(highlightCache).length > HIGHLIGHT_CACHE_LIMIT) {
            highlightCache = {};
            highlightCache[key] = entry;
        }
        if (template.kind === "cone" || template.kind === "rect") {
            // veil-map 无对应原语：本地几何近似（约束内明说的分支）。
            entry.cells = localCells(template);
            entry.resolved = true;
            if (template.kind === "cone" && isDm && !warnedConeLocal) {
                warnedConeLocal = true;
                notify("锥形模板的受影响格按本地几何近似计算（地图插件的锥原语为固定 90°）。", "info");
            }
            return entry;
        }
        var body = {
            origin: { cell_x: template.cell_x, cell_y: template.cell_y },
            shape: template.kind === "circle" ? "circle" : "line",
            size_cells: clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS),
            metric: template.kind === "circle" ? "euclidean" : "chebyshev"
        };
        if (template.kind === "line") {
            var unit = templateUnit(template);
            body.direction = { dx: unit.x, dy: unit.y };
        }
        apiRequest(
            "POST",
            VEIL_API_BASE + "/rooms/" + encodeURIComponent(roomId) + "/spatial/range",
            body
        ).then(function (response) {
            if (destroyed) return;
            if (!response || !response.success || !response.data) {
                throw new Error("spatial unavailable");
            }
            entry.cells = (response.data.cells || []).map(function (cell) {
                return { cell_x: cell.cell_x, cell_y: cell.cell_y };
            });
            entry.approx = false;
            entry.resolved = true;
            scheduleDraw();
        }).catch(function () {
            if (destroyed) return;
            entry.cells = localCells(template);
            entry.approx = true;
            entry.resolved = true;
            if (!warnedSpatialOffline) {
                warnedSpatialOffline = true;
                notify("地图空间服务不可用，受影响格按本地几何近似。", "error");
            }
            scheduleDraw();
        });
        return entry;
    }

    /* ------------------------------------------------------------ 绘制 */

    function scheduleDraw() {
        if (drawScheduled || destroyed) return;
        drawScheduled = true;
        window.requestAnimationFrame(function () {
            drawScheduled = false;
            draw();
        });
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

    function paintChip(text, x, y, color) {
        if (!text) return;
        context.save();
        context.font = '700 11px "Segoe UI", sans-serif';
        context.textAlign = "center";
        context.textBaseline = "middle";
        var width = context.measureText(text).width + 12;
        roundRect(context, x - width / 2, y - 9, width, 18, 5);
        context.fillStyle = "rgba(9, 12, 15, 0.82)";
        context.fill();
        context.strokeStyle = hexToRgba(color, 0.55);
        context.lineWidth = 1;
        context.stroke();
        context.fillStyle = "rgba(246, 240, 224, 0.95)";
        context.fillText(text, x, y + 0.5);
        context.restore();
    }

    function cellRect(cell) {
        var info = gridInfo();
        var camera = ctx.getCamera();
        var point = ctx.worldToScreen(info.x + cell.cell_x * info.grid, info.y + cell.cell_y * info.grid);
        var size = info.grid * (Number(camera && camera.zoom) || 1);
        return { x: point.x, y: point.y, size: size };
    }

    function drawHighlightCells(cells, color) {
        if (!cells || !cells.length) return;
        context.save();
        context.fillStyle = hexToRgba(color, 0.3);
        context.strokeStyle = hexToRgba(color, 0.5);
        context.lineWidth = 1;
        cells.forEach(function (cell) {
            var rect = cellRect(cell);
            context.fillRect(rect.x + 0.5, rect.y + 0.5, rect.size - 1, rect.size - 1);
            context.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.size - 1, rect.size - 1);
        });
        context.restore();
    }

    function drawTemplateShape(template, color, selected, dashed) {
        var info = gridInfo();
        var camera = ctx.getCamera();
        var zoom = Number(camera && camera.zoom) || 1;
        var scale = info.grid * zoom;
        var origin = vertexToScreen(template.cell_x, template.cell_y);
        var unit = templateUnit(template);
        var size = clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS);
        context.save();
        context.beginPath();
        if (template.kind === "circle") {
            context.arc(origin.x, origin.y, size * scale, 0, Math.PI * 2);
        } else if (template.kind === "cone") {
            var half = ((asFloat(template.angle, CONE_DEFAULT_ANGLE) || CONE_DEFAULT_ANGLE) / 2) * Math.PI / 180;
            var base = Math.atan2(unit.y, unit.x);
            context.moveTo(origin.x, origin.y);
            context.arc(origin.x, origin.y, size * scale, base - half, base + half);
            context.closePath();
        } else if (template.kind === "line") {
            var endX = origin.x + unit.x * size * scale;
            var endY = origin.y + unit.y * size * scale;
            var normalX = -unit.y * (scale / 2);
            var normalY = unit.x * (scale / 2);
            context.moveTo(origin.x + normalX, origin.y + normalY);
            context.lineTo(endX + normalX, endY + normalY);
            context.lineTo(endX - normalX, endY - normalY);
            context.lineTo(origin.x - normalX, origin.y - normalY);
            context.closePath();
        } else {
            var width = clamp(asInt(template.w_cells, 1), 1, MAX_TEMPLATE_CELLS) * scale;
            var height = clamp(asInt(template.h_cells, 1), 1, MAX_TEMPLATE_CELLS) * scale;
            context.rect(origin.x, origin.y, width, height);
        }
        context.fillStyle = hexToRgba(color, selected ? 0.3 : 0.2);
        context.fill();
        context.setLineDash(dashed ? [7, 5] : []);
        context.strokeStyle = hexToRgba(color, selected ? 1 : 0.85);
        context.lineWidth = selected ? 2.5 : 1.8;
        context.stroke();
        context.setLineDash([]);
        // 原点把手：锥/线在顶点，圆在圆心，方在角点。
        context.beginPath();
        context.arc(origin.x, origin.y, Math.max(3, 3 * zoom), 0, Math.PI * 2);
        context.fillStyle = color;
        context.fill();
        context.restore();
    }

    function templateCenter(template) {
        var info = gridInfo();
        var camera = ctx.getCamera();
        var zoom = Number(camera && camera.zoom) || 1;
        var scale = info.grid * zoom;
        var origin = vertexToScreen(template.cell_x, template.cell_y);
        var unit = templateUnit(template);
        if (template.kind === "circle") return origin;
        if (template.kind === "rect") {
            return {
                x: origin.x + (clamp(asInt(template.w_cells, 1), 1, MAX_TEMPLATE_CELLS) * scale) / 2,
                y: origin.y + (clamp(asInt(template.h_cells, 1), 1, MAX_TEMPLATE_CELLS) * scale) / 2
            };
        }
        var size = clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS);
        return {
            x: origin.x + unit.x * (size * scale) / 2,
            y: origin.y + unit.y * (size * scale) / 2
        };
    }

    function templateLabel(template) {
        if (template.kind === "circle") {
            return "圆 " + formatFeet(asInt(template.size_cells, 1));
        }
        if (template.kind === "cone") {
            return "锥 " + formatFeet(asInt(template.size_cells, 1));
        }
        if (template.kind === "line") {
            return "线 " + formatFeet(asInt(template.size_cells, 1));
        }
        return (
            "方 " +
            formatFeet(asInt(template.w_cells, 1)) +
            "×" +
            formatFeet(asInt(template.h_cells, 1))
        );
    }

    function draw() {
        if (!context || destroyed) return;
        var size = resizeCanvas();
        context.clearRect(0, 0, size.width, size.height);
        // 已放置模板：受影响格走 veil-map 端点（失败时近似），结果缓存。
        templates.forEach(function (template) {
            if (!template || !template.id) return;
            var color = template.color || TEMPLATE_COLORS[template.kind] || "#e06666";
            var selected = isDm && String(template.id) === String(selectedTemplateId);
            var entry = highlightCells(template);
            if (entry.resolved) drawHighlightCells(entry.cells, color);
            drawTemplateShape(template, color, selected, false);
            var center = templateCenter(template);
            paintChip(templateLabel(template), center.x, center.y - 14, color);
        });
        // 拖动草稿：本地几何直接算（每帧一个 key，不走网络请求）。
        if (placing && placing.kind) {
            var draftColor = TEMPLATE_COLORS[placing.kind] || "#e06666";
            drawHighlightCells(localCells(placing), draftColor);
            drawTemplateShape(placing, draftColor, false, true);
        }
        // 测距尺：虚线 + 端点 + 读数（复用 veil-map /spatial/line 的判定结果）。
        if (ruler) {
            var from = vertexToScreen(ruler.from.cell_x, ruler.from.cell_y);
            var to = vertexToScreen(ruler.to.cell_x, ruler.to.cell_y);
            var color = ruler.blocked === true ? "#e06666" : "#8fd3a8";
            context.save();
            context.beginPath();
            context.moveTo(from.x, from.y);
            context.lineTo(to.x, to.y);
            context.setLineDash([9, 6]);
            context.strokeStyle = color;
            context.lineWidth = 2;
            context.stroke();
            context.setLineDash([]);
            [from, to].forEach(function (point) {
                context.beginPath();
                context.arc(point.x, point.y, 3.5, 0, Math.PI * 2);
                context.fillStyle = color;
                context.fill();
            });
            context.restore();
            var cells = asInt(ruler.cells, 0);
            var text = cells + " 格 · " + formatFeet(cells);
            if (ruler.blocked === true) text += " · 被墙体阻挡";
            else if (ruler.blocked === null) text += " · 无法判定墙体";
            paintChip(
                text,
                (from.x + to.x) / 2,
                (from.y + to.y) / 2 - 16,
                ruler.blocked === true ? "#e06666" : "#8fd3a8"
            );
        }
    }

    /* ------------------------------------------------------------ 模板命中与操作 */

    function findTemplate(id) {
        if (!id) return null;
        for (var index = 0; index < templates.length; index += 1) {
            if (String(templates[index].id) === String(id)) return templates[index];
        }
        return null;
    }

    function hitTemplate(screenX, screenY) {
        var info = gridInfo();
        var camera = ctx.getCamera();
        var zoom = Number(camera && camera.zoom) || 1;
        var world = ctx.screenToWorld(screenX, screenY);
        var slack = 6 / zoom;
        for (var index = templates.length - 1; index >= 0; index -= 1) {
            var template = templates[index];
            if (!template) continue;
            var ox = info.x + template.cell_x * info.grid;
            var oy = info.y + template.cell_y * info.grid;
            if (template.kind === "circle") {
                var radius = clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS) * info.grid;
                if (Math.hypot(world.x - ox, world.y - oy) <= radius + slack) return template;
            } else if (template.kind === "cone") {
                var unit = templateUnit(template);
                var rx = world.x - ox;
                var ry = world.y - oy;
                var coneLength = Math.hypot(rx, ry);
                if (coneLength > clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS) * info.grid + slack) continue;
                var half = ((asFloat(template.angle, CONE_DEFAULT_ANGLE) || CONE_DEFAULT_ANGLE) / 2) * Math.PI / 180;
                if (rx * unit.x + ry * unit.y < -slack) continue;
                if (coneLength > 1e-9 && Math.abs(rx * unit.y - ry * unit.x) > coneLength * Math.sin(half) + slack) continue;
                return template;
            } else if (template.kind === "line") {
                var lineUnit = templateUnit(template);
                var lrx = world.x - ox;
                var lry = world.y - oy;
                var lineProjection = lrx * lineUnit.x + lry * lineUnit.y;
                if (lineProjection < -slack || lineProjection > clamp(asInt(template.size_cells, 1), 1, MAX_TEMPLATE_CELLS) * info.grid + slack) continue;
                if (Math.abs(lrx * lineUnit.y - lry * lineUnit.x) > info.grid / 2 + slack) continue;
                return template;
            } else {
                var width = clamp(asInt(template.w_cells, 1), 1, MAX_TEMPLATE_CELLS) * info.grid;
                var height = clamp(asInt(template.h_cells, 1), 1, MAX_TEMPLATE_CELLS) * info.grid;
                if (world.x >= ox - slack && world.x <= ox + width + slack && world.y >= oy - slack && world.y <= oy + height + slack) {
                    return template;
                }
            }
        }
        return null;
    }

    /* ------------------------------------------------------------ 工具切换 */

    function activateTool(next) {
        tool = next;
        placing = null;
        selectedTemplateId = "";
        Object.keys(toolButtons).forEach(function (id) {
            toolButtons[id].classList.toggle("is-active", id === tool);
        });
        hintBar.textContent = TOOL_HINTS[tool] || "";
        updateOverlayPointer();
        scheduleDraw();
    }

    function updateOverlayPointer() {
        var active = tool !== "select";
        canvas.style.pointerEvents = active ? "auto" : "none";
        canvas.style.cursor = active ? "crosshair" : "default";
        root.classList.toggle("is-tool-active", active);
    }

    /* ------------------------------------------------------------ 模板放置（画布层） */

    function onCanvasPointerDown(event) {
        if (event.button === 2) {
            cancelPlacing();
            return;
        }
        if (event.button !== 0) return;
        var point = localPoint(event);
        var vertex = vertexFromScreen(point.x, point.y);
        if (tool === "ruler") {
            // DM 与玩家都可用：格点吸附的直线测距，判定复用 veil-map 端点。
            ruler = { from: vertex, to: vertex, cells: 0, blocked: undefined };
            queryRuler();
            try { canvas.setPointerCapture(event.pointerId); } catch (error) { /* 忽略 */ }
            scheduleDraw();
            return;
        }
        if (tool === "select") return;
        placing = {
            kind: tool,
            cell_x: vertex.cell_x,
            cell_y: vertex.cell_y,
            origin_cell_x: vertex.cell_x,
            origin_cell_y: vertex.cell_y,
            size_cells: 0,
            w_cells: 0,
            h_cells: 0,
            angle: CONE_DEFAULT_ANGLE
        };
        try { canvas.setPointerCapture(event.pointerId); } catch (error) { /* 忽略 */ }
        scheduleDraw();
    }

    function onCanvasPointerMove(event) {
        if (tool === "ruler" && ruler && event.buttons) {
            var rulerPoint = localPoint(event);
            ruler.to = vertexFromScreen(rulerPoint.x, rulerPoint.y);
            ruler.cells = Math.max(
                Math.abs(ruler.to.cell_x - ruler.from.cell_x),
                Math.abs(ruler.to.cell_y - ruler.from.cell_y)
            );
            scheduleRulerQuery();
            scheduleDraw();
            return;
        }
        if (!placing || !placing.kind || !event.buttons) return;
        var point = localPoint(event);
        var vertex = vertexFromScreen(point.x, point.y);
        if (placing.kind === "rect") {
            placing.w_cells = vertex.cell_x - placing.origin_cell_x;
            placing.h_cells = vertex.cell_y - placing.origin_cell_y;
        } else {
            var dx = vertex.cell_x - placing.origin_cell_x;
            var dy = vertex.cell_y - placing.origin_cell_y;
            var distance = Math.hypot(dx, dy);
            placing.size_cells = clamp(Math.round(distance), 0, MAX_TEMPLATE_CELLS);
            if (distance > 1e-9) {
                placing.direction = { dx: dx / distance, dy: dy / distance };
            }
        }
        scheduleDraw();
    }

    function onCanvasPointerUp(event) {
        if (tool === "ruler" && ruler) {
            try { canvas.releasePointerCapture(event.pointerId); } catch (error) { /* 忽略 */ }
            queryRuler();
            return;
        }
        if (!placing) return;
        try { canvas.releasePointerCapture(event.pointerId); } catch (error) { /* 忽略 */ }
        var draft = finalizeDraft(placing);
        placing = null;
        scheduleDraw();
        if (!draft) return;
        if (!isDm) return; // 玩家不该走到这里：模板工具按钮未渲染
        writeTemplates(
            "place",
            {
                id: "tpl-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
                template: draft
            },
            "已放置" + templateLabel(draft) + "。"
        ).then(function (response) {
            if (!response || response.success !== true) return;
            var latest = templates.length ? templates[templates.length - 1] : null;
            if (latest && latest.id) {
                lastPlacedId = String(latest.id);
                selectedTemplateId = lastPlacedId; // 便于随后 Shift+滚轮旋转
                scheduleDraw();
            }
        });
    }

    function finalizeDraft(draft) {
        if (draft.kind === "rect") {
            var rawWidth = asInt(draft.w_cells, 0);
            var rawHeight = asInt(draft.h_cells, 0);
            if (Math.abs(rawWidth) < 1 || Math.abs(rawHeight) < 1) {
                notify("方形至少要 1×1 格；拖动距离太短，未放置。", "info");
                return null;
            }
            // 角点归一：任意方向拖出都折到左上角原点，存正宽高。
            return {
                kind: "rect",
                cell_x: draft.origin_cell_x + Math.min(rawWidth, 0),
                cell_y: draft.origin_cell_y + Math.min(rawHeight, 0),
                w_cells: Math.abs(rawWidth),
                h_cells: Math.abs(rawHeight)
            };
        }
        var size = asInt(draft.size_cells, 0);
        if (size < 1) {
            notify("模板至少 1 格（5 尺）；拖动距离太短，未放置。", "info");
            return null;
        }
        var template = {
            kind: draft.kind,
            cell_x: draft.origin_cell_x,
            cell_y: draft.origin_cell_y,
            size_cells: size
        };
        if (draft.kind === "cone" || draft.kind === "line") {
            var unit = draft.direction
                ? normalizeUnit(draft.direction.dx, draft.direction.dy)
                : templateUnit(draft);
            template.direction = { dx: unit.x, dy: unit.y };
        }
        if (draft.kind === "cone") {
            template.angle = asFloat(draft.angle, CONE_DEFAULT_ANGLE) || CONE_DEFAULT_ANGLE;
        }
        return template;
    }

    function cancelPlacing() {
        placing = null;
        scheduleDraw();
    }

    /* ------------------------------------------------------------ 测距尺（复用 veil-map 空间判定） */

    function scheduleRulerQuery() {
        window.clearTimeout(rulerQueryTimer);
        rulerQueryTimer = window.setTimeout(queryRuler, 160);
    }

    function queryRuler() {
        if (!ruler) return;
        var info = gridInfo();
        var body = {
            from: { x: info.x + ruler.from.cell_x * info.grid, y: info.y + ruler.from.cell_y * info.grid },
            to: { x: info.x + ruler.to.cell_x * info.grid, y: info.y + ruler.to.cell_y * info.grid },
            mode: "movement"
        };
        apiRequest(
            "POST",
            VEIL_API_BASE + "/rooms/" + encodeURIComponent(roomId) + "/spatial/line",
            body
        ).then(function (response) {
            if (destroyed || !ruler) return;
            if (!response || !response.success) {
                // 玩家迷雾受限或服务缺席：仍显示直线读数，标注无法判定墙体。
                ruler.blocked = null;
                ruler.cells = Math.max(
                    Math.abs(ruler.to.cell_x - ruler.from.cell_x),
                    Math.abs(ruler.to.cell_y - ruler.from.cell_y)
                );
                scheduleDraw();
                return;
            }
            var data = response.data || {};
            if (typeof data.distance_cells === "number") ruler.cells = data.distance_cells;
            ruler.blocked = data.resolvable === false ? null : Boolean(data.blocked);
            scheduleDraw();
        }).catch(function () {
            if (destroyed || !ruler) return;
            ruler.blocked = null;
            scheduleDraw();
        });
    }

    /* ------------------------------------------------------------ 滚轮旋转（Shift 大步 / Ctrl 微调） */

    function rotateDraftOrSelected(delta) {
        if (placing && (placing.kind === "cone" || placing.kind === "line") && placing.direction) {
            var unit = rotateUnit(normalizeUnit(placing.direction.dx, placing.direction.dy), delta);
            placing.direction = { dx: unit.x, dy: unit.y };
            scheduleDraw();
            return;
        }
        if (!isDm) return;
        var target = findTemplate(selectedTemplateId) || findTemplate(lastPlacedId);
        if (!target || (target.kind !== "cone" && target.kind !== "line")) return;
        var nextUnit = rotateUnit(templateUnit(target), delta);
        var updated = {};
        Object.keys(target).forEach(function (key) { updated[key] = target[key]; });
        updated.direction = { dx: nextUnit.x, dy: nextUnit.y };
        var index = templates.indexOf(target);
        if (index >= 0) templates[index] = updated;
        selectedTemplateId = String(updated.id);
        scheduleDraw();
        window.clearTimeout(rotateTimer);
        rotateTimer = window.setTimeout(function () {
            writeTemplates("move", { id: updated.id, template: updated }, "");
        }, 600);
    }

    function onCanvasWheel(event) {
        event.preventDefault();
        if (placing || tool === "cone" || tool === "line") {
            if (event.shiftKey) {
                rotateDraftOrSelected(event.deltaY < 0 ? 15 : -15);
                return;
            }
            if (event.ctrlKey) {
                rotateDraftOrSelected(event.deltaY < 0 ? 3 : -3);
                return;
            }
        }
        var factor = event.deltaY < 0 ? 1.12 : 0.89;
        ctx.zoomAt(factor, event.clientX, event.clientY);
    }

    /* ------------------------------------------------------------ 选择层：模板拖动 / 右键删除 / 棋子卡 */

    function tokenWorld(token) {
        var info = gridInfo();
        return {
            x: info.x + (asFloat(token.cell_x, 0) + asFloat(token.offset_x, 0)) * info.grid,
            y: info.y + (asFloat(token.cell_y, 0) + asFloat(token.offset_y, 0)) * info.grid
        };
    }

    function tokenRect(token) {
        var camera = ctx.getCamera();
        var zoom = Number(camera && camera.zoom) || 1;
        var info = gridInfo();
        var point = ctx.worldToScreen(tokenWorld(token).x, tokenWorld(token).y);
        var width = Math.max(1, asInt(token.w, 1)) * info.grid * zoom;
        var height = Math.max(1, asInt(token.h, 1)) * info.grid * zoom;
        var inset = Math.max(1.5, 2 * zoom);
        return {
            x: point.x + inset,
            y: point.y + inset,
            width: Math.max(4, width - inset * 2),
            height: Math.max(4, height - inset * 2)
        };
    }

    function hitToken(screenX, screenY) {
        var tokens = (mapState && Array.isArray(mapState.tokens)) ? mapState.tokens : [];
        for (var index = tokens.length - 1; index >= 0; index -= 1) {
            var token = tokens[index];
            if (!token) continue;
            var rect = tokenRect(token);
            if (
                screenX >= rect.x && screenX <= rect.x + rect.width &&
                screenY >= rect.y && screenY <= rect.y + rect.height
            ) {
                return token;
            }
        }
        return null;
    }

    function onStagePointerDown(event) {
        if (tool !== "select") return; // 画布层已接管（模板/测距工具激活）
        var point = stagePoint(event);
        if (isDm) {
            var template = hitTemplate(point.x, point.y);
            if (template) {
                // 捕获阶段截住：veil-map 不再把这次按下当成平移画布。
                event.stopPropagation();
                event.preventDefault();
                selectedTemplateId = String(template.id);
                movingTemplate = {
                    id: template.id,
                    origin_cell_x: template.cell_x,
                    origin_cell_y: template.cell_y,
                    start_vertex: vertexFromScreen(point.x, point.y),
                    moved: false
                };
                scheduleDraw();
                return;
            }
        }
        var token = hitToken(point.x, point.y);
        if (token) {
            pendingClick = { token: token, x: event.clientX, y: event.clientY };
        } else {
            pendingClick = null;
            if (isDm) {
                selectedTemplateId = "";
                scheduleDraw();
            }
        }
    }

    function onWindowPointerMove(event) {
        if (movingTemplate && event.buttons) {
            var point = stagePoint(event);
            var vertex = vertexFromScreen(point.x, point.y);
            var template = findTemplate(movingTemplate.id);
            if (!template) {
                movingTemplate = null;
                return;
            }
            var nextX = movingTemplate.origin_cell_x + (vertex.cell_x - movingTemplate.start_vertex.cell_x);
            var nextY = movingTemplate.origin_cell_y + (vertex.cell_y - movingTemplate.start_vertex.cell_y);
            if (nextX !== template.cell_x || nextY !== template.cell_y) {
                template.cell_x = nextX;
                template.cell_y = nextY;
                movingTemplate.moved = true;
                scheduleDraw();
            }
        }
    }

    function onWindowPointerUp(event) {
        if (movingTemplate) {
            var moved = movingTemplate.moved;
            var template = findTemplate(movingTemplate.id);
            movingTemplate = null;
            if (moved && template) {
                writeTemplates("move", { id: template.id, template: template }, "模板已移动。");
            } else {
                scheduleDraw();
            }
            return;
        }
        if (!pendingClick || event.button !== 0) return;
        var distance = Math.hypot(event.clientX - pendingClick.x, event.clientY - pendingClick.y);
        var token = hitToken(stagePoint(event).x, stagePoint(event).y);
        if (distance < 5 && token && String(token.id) === String(pendingClick.token.id)) {
            openTokenCard(token, event.clientX, event.clientY);
        }
        pendingClick = null;
    }

    function onStageContextMenu(event) {
        if (!isDm || tool !== "select") return;
        var point = stagePoint(event);
        var template = hitTemplate(point.x, point.y);
        if (!template) return; // 没点中模板 → 交给 veil-map 的棋子菜单
        event.preventDefault();
        event.stopPropagation();
        writeTemplates("delete", { id: template.id }, "模板已删除。");
        if (String(selectedTemplateId) === String(template.id)) selectedTemplateId = "";
    }

    function onKeyDown(event) {
        if (event.key !== "Escape") return;
        if (placing) {
            cancelPlacing();
            return;
        }
        if (movingTemplate) {
            var template = findTemplate(movingTemplate.id);
            if (template) {
                template.cell_x = movingTemplate.origin_cell_x;
                template.cell_y = movingTemplate.origin_cell_y;
            }
            movingTemplate = null;
            scheduleDraw();
            return;
        }
        if (cardEl && !cardEl.hidden) {
            closeTokenCard();
            return;
        }
        if (ruler) {
            ruler = null;
            scheduleDraw();
            return;
        }
        if (selectedTemplateId) {
            selectedTemplateId = "";
            scheduleDraw();
            return;
        }
        if (tool !== "select") activateTool("select");
    }

    /* ------------------------------------------------------------ 棋子操作卡 */

    function tokenHpView(token, data) {
        var combat = token && token.combat;
        var hp = asInt(combat && combat.hp, -1);
        var maxHp = asInt(combat && combat.max_hp, -1);
        var cardData = data && data.card;
        if (hp < 0 && cardData) hp = asInt(cardData.hp, 0);
        if (maxHp < 0 && cardData) maxHp = asInt(cardData.max_hp, 0);
        return { hp: Math.max(0, hp), max_hp: Math.max(0, maxHp) };
    }

    function subtitleFor(cardData) {
        if (!cardData) return "";
        if (cardData.kind === "player") {
            var parts = [];
            if (cardData.class) parts.push(cardData.class);
            if (cardData.level) parts.push("Lv." + cardData.level);
            if (cardData.race) parts.push(cardData.race);
            return parts.join(" · ");
        }
        if (cardData.kind === "npc") {
            return cardData.cr ? "怪物 · CR " + cardData.cr : "剧本 NPC";
        }
        if (cardData.kind === "combatant") return "战斗参战者";
        return "未绑定角色";
    }

    function closeTokenCard() {
        cardEl.hidden = true;
        card = null;
    }

    function actionDetailRow(entry) {
        var hit = entry.hit ? '<span class="vtt-action-hit">命中 ' + escapeHtml(entry.hit) + "</span>" : "";
        var damage = entry.damage ? '<span class="vtt-action-damage">' + escapeHtml(entry.damage) + "</span>" : "";
        var detail = entry.detail ? "<small>" + escapeHtml(entry.detail) + "</small>" : "";
        return (
            '<li class="vtt-action-row">' +
            '<span class="vtt-action-name">' + escapeHtml(entry.name) + "</span>" +
            hit +
            damage +
            detail +
            "</li>"
        );
    }

    function renderActionCategory(category) {
        var actions = (card && card.actions) || {};
        var entries = actions[category] || [];
        var detailEl = cardEl.querySelector("[data-vtt-card-detail]");
        if (!detailEl) return;
        if (!entries.length) {
            detailEl.innerHTML =
                '<div class="vtt-card-empty"><strong>' + escapeHtml(CATEGORY_LABELS[category]) + "</strong>" +
                "<p>" + escapeHtml(CATEGORY_EMPTY_HINTS[category]) + "</p></div>";
            return;
        }
        detailEl.innerHTML =
            '<h4 class="vtt-detail-title">' + escapeHtml(CATEGORY_LABELS[category]) +
            "<span>" + entries.length + " 项</span></h4>" +
            '<ul class="vtt-action-list">' + entries.map(actionDetailRow).join("") + "</ul>";
    }

    function renderTokenCard(data) {
        if (destroyed || !card) return;
        var token = card.token;
        var cardData = (data && data.card) || {};
        var hp = tokenHpView(token, data);
        var avatarImage = (token && token.avatar) || cardData.avatar || "";
        var initial = String((token && token.name) || cardData.name || "?").slice(0, 1) || "?";
        var avatarHtml = avatarImage
            ? '<img src="' + escapeHtml(avatarImage) + '" alt="" loading="lazy">'
            : "<span>" + escapeHtml(initial.toUpperCase()) + "</span>";
        var subtitle = subtitleFor(cardData);
        var hpPercent = hp.max_hp > 0 ? Math.round((hp.hp / hp.max_hp) * 100) : 0;
        var head =
            '<div class="vtt-card-head">' +
            '<span class="vtt-card-avatar">' + avatarHtml + "</span>" +
            '<span class="vtt-card-title"><strong>' + escapeHtml((token && token.name) || cardData.name || "棋子") + "</strong>" +
            "<small>" + escapeHtml(subtitle) + "</small></span>" +
            '<button type="button" data-vtt-card-close aria-label="关闭">×</button>' +
            "</div>";
        var hpRow =
            '<div class="vtt-card-hp">' +
            '<div class="vtt-hp-bar"><i style="width:' + clamp(hpPercent, 0, 100) + '%"></i></div>' +
            '<span>' + escapeHtml(hp.max_hp > 0 ? hp.hp + " / " + hp.max_hp : "—") + "</span>" +
            (cardData.ac ? '<span class="vtt-card-ac">AC ' + escapeHtml(cardData.ac) + "</span>" : "") +
            "</div>";
        var actionsHtml;
        if (!data) {
            actionsHtml = '<div class="vtt-card-hint">棋子数据加载中…</div>';
        } else if (data.actions) {
            // 玩家响应没有 actions 键 → 按钮整块不渲染（服务端裁剪，拍板决策）。
            var buttons = CATEGORY_ORDER.map(function (category) {
                var count = (data.actions[category] || []).length;
                return (
                    '<button type="button" data-vtt-category="' + category + '"' +
                    (count ? "" : ' data-vtt-empty="true"') + ">" +
                    escapeHtml(CATEGORY_LABELS[category]) +
                    "<em>" + count + "</em></button>"
                );
            }).join("");
            actionsHtml = '<div class="vtt-card-buttons">' + buttons + "</div>";
            var hint = data.actions.hint || "";
            var total = CATEGORY_ORDER.reduce(function (sum, category) {
                return sum + (data.actions[category] || []).length;
            }, 0);
            if (!total) {
                actionsHtml +=
                    '<div class="vtt-card-hint">' +
                    escapeHtml(hint || "该棋子暂无动作数据；开战后会从战斗状态生成动作清单。") +
                    "</div>";
            }
        } else {
            actionsHtml = '<div class="vtt-card-hint">玩家视图：操作由 DM 执行；此处仅展示角色状态。</div>';
        }
        cardEl.innerHTML =
            head +
            hpRow +
            actionsHtml +
            '<div class="vtt-card-detail" data-vtt-card-detail hidden></div>';
        var close = cardEl.querySelector("[data-vtt-card-close]");
        if (close) close.addEventListener("click", closeTokenCard);
        var categoryButtons = cardEl.querySelectorAll("[data-vtt-category]");
        Array.prototype.forEach.call(categoryButtons, function (button) {
            button.addEventListener("click", function () {
                var detailEl = cardEl.querySelector("[data-vtt-card-detail]");
                if (detailEl) detailEl.hidden = false;
                renderActionCategory(button.getAttribute("data-vtt-category"));
            });
        });
    }

    function openTokenCard(token, clientX, clientY) {
        var ownerUserId = String(token.owner_user_id || "");
        if (!isDm && ownerUserId !== String(ctx.user.id || "")) {
            notify("玩家只能查看自己角色的棋子卡。", "info");
            return;
        }
        card = { token: token, actions: null };
        cardEl.hidden = false;
        renderTokenCard(null);
        var cardWidth = 280;
        var cardHeight = 260;
        cardEl.style.left = clamp(clientX + 14, 8, Math.max(8, window.innerWidth - cardWidth - 8)) + "px";
        cardEl.style.top = clamp(clientY - 20, 8, Math.max(8, window.innerHeight - cardHeight - 8)) + "px";
        var params =
            "name=" + encodeURIComponent(String(token.name || "")) +
            "&owner=" + encodeURIComponent(ownerUserId) +
            "&combatant=" + encodeURIComponent(String(token.combatant || "")) +
            "&combatant_name=" + encodeURIComponent(String(token.combatant_name || ""));
        apiRequest("GET", "/rooms/" + encodeURIComponent(roomId) + "/token-actions?" + params)
            .then(function (response) {
                if (destroyed || !card || String(card.token.id) !== String(token.id)) return;
                if (!response || !response.success) {
                    renderTokenCard(null);
                    return;
                }
                card.actions = response.data && response.data.actions;
                renderTokenCard(response.data);
            })
            .catch(function (error) {
                console.error("dnd5e-vtt token card failed", error);
            });
    }

    function onDocumentPointerDown(event) {
        if (cardEl.hidden) return;
        if (event.target instanceof Element && event.target.closest(".vtt-token-card")) return;
        closeTokenCard();
    }

    /* ------------------------------------------------------------ 清空（仅 DM） */

    function clearTemplates() {
        if (!templates.length) {
            notify("画布上没有模板。", "info");
            return;
        }
        writeTemplates("clear", {}, "已清空全部模板。");
        selectedTemplateId = "";
        lastPlacedId = "";
    }

    /* ------------------------------------------------------------ socket 同步 */

    function onSocketTemplates(payload) {
        if (destroyed || !payload) return;
        if (String(payload.room_id || "") !== roomId) return;
        applyTemplates({ templates: payload.templates, revision: payload.revision });
    }

    function onSocketMapChanged(payload) {
        if (destroyed || !payload) return;
        if (String(payload.room_id || "") !== roomId) return;
        if (payload.map && typeof payload.map === "object") mapState = payload.map;
    }

    function onSocketReconnect() {
        // 断线期间模板/地图可能已变：重连后重新拉取。
        if (destroyed) return;
        loadTemplates();
        loadMapTokens();
    }

    function attachSocket() {
        socket = window.TrpgSocket || null;
        if (!socket) return;
        function bind(name, handler) {
            socket.on(name, handler);
            socketHandlers.push([name, handler]);
        }
        bind("dnd5e_vtt_templates", onSocketTemplates);
        bind("room_map_changed", onSocketMapChanged);
        if (typeof socket.on === "function") {
            bind("connect", onSocketReconnect);
            bind("reconnect", onSocketReconnect);
        }
    }

    function detachSocket() {
        if (socket && typeof socket.off === "function") {
            socketHandlers.forEach(function (pair) {
                socket.off(pair[0], pair[1]);
            });
        }
        socketHandlers = [];
        socket = null;
    }

    /* ------------------------------------------------------------ 事件接线 */

    canvas.addEventListener("pointerdown", onCanvasPointerDown);
    canvas.addEventListener("pointermove", onCanvasPointerMove);
    canvas.addEventListener("pointerup", onCanvasPointerUp);
    canvas.addEventListener("pointercancel", onCanvasPointerUp);
    canvas.addEventListener("wheel", onCanvasWheel, { passive: false });
    stageHost.addEventListener("pointerdown", onStagePointerDown, true);
    stageHost.addEventListener("contextmenu", onStageContextMenu, true);
    document.addEventListener("pointermove", onWindowPointerMove, true);
    document.addEventListener("pointerup", onWindowPointerUp, true);
    document.addEventListener("pointerdown", onDocumentPointerDown);
    document.addEventListener("keydown", onKeyDown);

    unsubscribers.push(ctx.events.on("camera", scheduleDraw));
    unsubscribers.push(ctx.events.on("resize", scheduleDraw));
    unsubscribers.push(
        ctx.events.on("grid", function () {
            highlightCache = {}; // 网格重标定后重算受影响格。
            scheduleDraw();
        })
    );

    if (typeof ResizeObserver !== "undefined") {
        var resizeObserver = new ResizeObserver(scheduleDraw);
        resizeObserver.observe(stageHost);
        unsubscribers.push(function () {
            resizeObserver.disconnect();
        });
    }

    activateTool("select");
    resizeCanvas();
    attachSocket();
    void loadTemplates();
    void loadMapTokens();
    scheduleDraw();
    pollTimer = window.setInterval(function () {
        if (destroyed || document.visibilityState === "hidden") return;
        if (!socket && window.TrpgSocket) {
            attachSocket();
            void loadTemplates();
            void loadMapTokens();
            return;
        }
        if (!window.TrpgSocket || (socket && socket.connected === false)) {
            void loadTemplates();
            void loadMapTokens();
        }
    }, TEMPLATE_POLL);

    return {
        observer: roomObserver,
        destroy: function () {
            destroyed = true;
            window.clearInterval(pollTimer);
            window.clearTimeout(rulerQueryTimer);
            window.clearTimeout(rotateTimer);
            detachSocket();
            unsubscribers.forEach(function (unsubscribe) {
                try {
                    if (typeof unsubscribe === "function") unsubscribe();
                } catch (error) {
                    // 卸载失败不影响核心布局。
                }
            });
            canvas.removeEventListener("pointerdown", onCanvasPointerDown);
            canvas.removeEventListener("pointermove", onCanvasPointerMove);
            canvas.removeEventListener("pointerup", onCanvasPointerUp);
            canvas.removeEventListener("pointercancel", onCanvasPointerUp);
            canvas.removeEventListener("wheel", onCanvasWheel);
            stageHost.removeEventListener("pointerdown", onStagePointerDown, true);
            stageHost.removeEventListener("contextmenu", onStageContextMenu, true);
            document.removeEventListener("pointermove", onWindowPointerMove, true);
            document.removeEventListener("pointerup", onWindowPointerUp, true);
            document.removeEventListener("pointerdown", onDocumentPointerDown);
            document.removeEventListener("keydown", onKeyDown);
            closeTokenCard();
            cardEl.remove();
            root.remove();
        }
    };
}

    window.TrpgStagePlugins = window.TrpgStagePlugins || {};
    window.TrpgStagePlugins["dnd5e-vtt"] = window.TrpgStagePlugins["dnd5e-vtt"] || {};
    window.TrpgStagePlugins["dnd5e-vtt"].mountRail = mountRail;
})();
