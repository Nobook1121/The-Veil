/**
 * dnd5e-vtt 插件前端：D&D 5e 战桌右侧栏六面板（M2 骨架）。
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
 * 面板清单从 ``/api/plugins/dnd5e-vtt/panels?room=…`` 拉取，观察者裁剪
 * 全部在服务端完成，前端只消费；veil-map 缺失时本面板照常工作。
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
        var api = window.TrpgApi;
        if (api) {
            if (method === "GET") return api.get(API_BASE + path);
        }
        var options = { method: method, headers: { Accept: "application/json" } };
        return fetch(API_BASE + path, options).then(function (response) {
            return response.json().catch(function () {
                return { success: false, message: "服务器返回了无效响应。" };
            });
        });
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

        host.classList.add("vtt-rail-host");

        var shell = document.createElement("div");
        shell.className = "vtt-rail";
        shell.innerHTML = '<div class="vtt-loading">面板数据加载中…</div>';
        host.appendChild(shell);

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

    window.TrpgStagePlugins = window.TrpgStagePlugins || {};
    window.TrpgStagePlugins["dnd5e-vtt"] = window.TrpgStagePlugins["dnd5e-vtt"] || {};
    window.TrpgStagePlugins["dnd5e-vtt"].mountRail = mountRail;
})();
