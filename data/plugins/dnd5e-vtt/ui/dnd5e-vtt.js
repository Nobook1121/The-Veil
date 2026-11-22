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
 *
 * 地图骰桌（M4）：所有掷骰汇聚为带 ``metadata.dice_roll`` 的房间消息
 * （``new_message`` socket，兼容 {room_id, message} 与 AI 工具裸消息两种
 * 形态）+ ``combat_dice`` 广播；两路共用去重键（request_id 缺省时
 * 表达式/骰面/总数/掷骰者复合键，TTL ~8s）。有地图时在掷骰者棋子（无则
 * 视口中心）落一桌 3D 骰子动画（注入 /js/dice/dice3d.js，不重编译；
 * 不传 seed，bundle 翻滚后把服务端 faces 贴到朝上面 → 动画即权威值），
 * 同时上浮结果飘字（nat20 金色、nat1 低亮、多骰合并 total+expression）。
 * DM 工具栏齿轮可关动画与调骰数上限（POST 仅 DM，广播
 * ``dnd5e_vtt_dice_settings``）；底栏 d20 dock 给全员快捷掷骰入口
 * （表达式限 NdM 加法，复用 TrpgDiceTable.handleDiceTableCommand，本地
 * 掷骰者以 persistLocalDiceMessage 包装捕获喂地图动画）。无地图时地图层
 * 整体跳过，聊天掷骰卡管线零改动。
 *
 * 规则速查（kiwee 第三刀的 UI 消费面）：工具组尾部书图标打开浮层，检索法术/
 * 物品/专长/职业等**玩家侧**类目，点条目看解析后正文、结构化摘要与 kiwi 站
 * 深链。数据只走 ``/api/plugins/dnd5e/rules/search`` 与 ``.../entry``（服务端
 * 已按观察者裁剪，怪物/掷表/变体规则不下发），未建库时面板显示「规则深库
 * 未建」+ 建库指引而不是报错。快捷键 Ctrl/⌘+K 与 ``/``。
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


/* ==========================================================================
   M4 地图骰桌公共层：dice3d bundle 按需注入（不重编译）、外观移植
   （localStorage trpg_dice_appearance + 主题 CSS 变量 + /api/dice-textures，
   照 diceAppearance.ts 精简移植）、DiceRollMeta → plan/snapshot 逐字段平移
   （照 diceTable.ts planFromMeta / snapshotFromMeta）。
   ========================================================================== */

var DICE_BUNDLE_URL = "/js/dice/dice3d.js";
var DICE_APPEARANCE_KEY = "trpg_dice_appearance";
var DICE_TEXTURES_API = "/api/dice-textures";
// 同时存活的地图 3D 骰盘上限：超限把最早一桌冻结成静态图，防 WebGL context 耗尽。
var MAP_DICE_MAX_LIVE = 2;
// new_message 与 combat_dice 双路同播的去重窗口。
var DICE_DEDUP_TTL = 8000;
// 骰盘落定后的停留时长（随后淡出移除）。
var MAP_DICE_TRAY_MS = 3200;
// 结果飘字总时长（含上浮与淡出）。
var DICE_POP_MS = 2400;

var diceBundlePromise = null;

function loadDice3DBundle() {
    if (window.TrpgDice3D) return Promise.resolve(true);
    if (diceBundlePromise) return diceBundlePromise;
    diceBundlePromise = new Promise(function (resolve) {
        var script = document.createElement("script");
        script.src = DICE_BUNDLE_URL;
        script.async = true;
        script.onload = function () { resolve(Boolean(window.TrpgDice3D)); };
        script.onerror = function () { resolve(false); };
        document.head.appendChild(script);
    });
    return diceBundlePromise;
}

/* ------------------------------------------------------------ 外观（本机偏好，照 diceAppearance.ts 精简移植） */

function diceLoadAppearance() {
    var defaults = { body: "", text: "", table: "", edge: "", tableSkinId: "", diceSkinIds: {} };
    try {
        var raw = window.localStorage.getItem(DICE_APPEARANCE_KEY);
        var config = raw ? JSON.parse(raw) : {};
        if (!config || typeof config !== "object") return defaults;
        return {
            body: String(config.body || ""),
            text: String(config.text || ""),
            table: String(config.table || ""),
            edge: String(config.edge || ""),
            tableSkinId: String(config.tableSkinId || ""),
            diceSkinIds: config.diceSkinIds && typeof config.diceSkinIds === "object" ? config.diceSkinIds : {}
        };
    } catch (error) {
        return defaults;
    }
}

function diceCssVariable(name, fallback) {
    // 必须从 body 读：主题变量定义在 body.theme-* 上，:root 别名在默认主题下求值。
    var scopes = [document.body, document.documentElement];
    for (var index = 0; index < scopes.length; index += 1) {
        if (!scopes[index]) continue;
        var value = window.getComputedStyle(scopes[index]).getPropertyValue(name).trim();
        if (value) return value;
    }
    return fallback;
}

function diceParseColor(value) {
    var text = String(value || "").trim();
    var hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
    if (hex) {
        var body = hex[1];
        if (body.length === 3) {
            return [
                parseInt(body.charAt(0) + body.charAt(0), 16),
                parseInt(body.charAt(1) + body.charAt(1), 16),
                parseInt(body.charAt(2) + body.charAt(2), 16)
            ];
        }
        return [
            parseInt(body.slice(0, 2), 16),
            parseInt(body.slice(2, 4), 16),
            parseInt(body.slice(4, 6), 16)
        ];
    }
    var rgb = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i.exec(text);
    if (rgb) return [parseFloat(rgb[1]), parseFloat(rgb[2]), parseFloat(rgb[3])];
    return null;
}

function diceLuminance(value) {
    var rgb = diceParseColor(value);
    if (!rgb) return null;
    var normalized = rgb.map(function (channel) {
        var unit = Math.max(0, Math.min(255, channel)) / 255;
        return unit <= 0.03928 ? unit / 12.92 : Math.pow((unit + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * normalized[0] + 0.7152 * normalized[1] + 0.0722 * normalized[2];
}

function diceContrastRatio(foreground, background) {
    var front = diceLuminance(foreground);
    var back = diceLuminance(background);
    if (front === null || back === null) return 0;
    return (Math.max(front, back) + 0.05) / (Math.min(front, back) + 0.05);
}

function diceMixColor(source, target, weight) {
    var first = diceParseColor(source);
    var second = diceParseColor(target);
    if (!first || !second) return null;
    var mixed = first.map(function (channel, index) {
        return Math.round(channel * (1 - weight) + second[index] * weight);
    });
    return "#" + mixed.map(function (channel) {
        var clamped = Math.max(0, Math.min(255, channel));
        var hex = clamped.toString(16);
        return hex.length < 2 ? "0" + hex : hex;
    }).join("");
}

function diceReadableColor(background, preferred) {
    if (diceContrastRatio(preferred, background) >= 4.5) return preferred;
    var weights = [0.2, 0.4, 0.6, 0.8];
    for (var wi = 0; wi < weights.length; wi += 1) {
        var targets = ["#ffffff", "#000000"];
        for (var ti = 0; ti < targets.length; ti += 1) {
            var mixed = diceMixColor(preferred, targets[ti], weights[wi]);
            if (mixed && diceContrastRatio(mixed, background) >= 4.5) return mixed;
        }
    }
    return diceContrastRatio("#f6f1e7", background) >= diceContrastRatio("#1f2933", background) ? "#f6f1e7" : "#1f2933";
}

function diceResolvedColors(config) {
    var body = config.body || diceCssVariable("--theme-dice-body", "#f6f1e7");
    var themeText = diceCssVariable("--theme-text-primary", "#2b2a33");
    return {
        body: body,
        text: config.text || diceReadableColor(body, themeText),
        table: config.table || diceCssVariable("--theme-dice-table", diceCssVariable("--theme-bg-panel-muted", "#1d3a34")),
        edge: config.edge || diceCssVariable("--theme-dice-accent", diceCssVariable("--theme-accent-primary", "#d8b56d"))
    };
}

var diceTextureLibraryCache = null;
var diceTextureLibraryPromise = null;

function diceTextureLibrary() {
    if (diceTextureLibraryCache) return Promise.resolve(diceTextureLibraryCache);
    if (diceTextureLibraryPromise) return diceTextureLibraryPromise;
    diceTextureLibraryPromise = apiRequest("GET", DICE_TEXTURES_API)
        .then(function (response) {
            diceTextureLibraryCache =
                (response && response.success && response.data) ||
                { assets: [], table_skins: [], dice_skins: [] };
            return diceTextureLibraryCache;
        })
        .catch(function (error) {
            // 贴图库缺席是合法形态：Canvas 面兜底，不阻塞骰盘。
            console.debug("dnd5e-vtt dice texture library unavailable", error);
            diceTextureLibraryCache = { assets: [], table_skins: [], dice_skins: [] };
            return diceTextureLibraryCache;
        });
    return diceTextureLibraryPromise;
}

function diceAssetUrl(assetId) {
    return "/api/dice-textures/assets/" + encodeURIComponent(assetId);
}

function diceDieTypeFor(sides, mode) {
    if (mode === "percentile") return "d10";
    var map = { 4: "d4", 6: "d6", 8: "d8", 10: "d10", 12: "d12", 20: "d20", 100: "d10" };
    return map[Number(sides)] || "";
}

function diceFaceUrls(dieType, skinIds) {
    var skinId = skinIds && skinIds[dieType];
    var library = diceTextureLibraryCache;
    if (!skinId || !library) return {};
    var skin = null;
    (library.dice_skins || []).forEach(function (item) {
        if (item && String(item.id) === String(skinId) && item.enabled !== false) skin = item;
    });
    if (!skin) return {};
    var urls = {};
    Object.keys(skin.faces || {}).forEach(function (face) {
        var assetId = skin.faces[face];
        if (assetId) urls[face] = diceAssetUrl(assetId);
    });
    return urls;
}

function diceTableSkinUrl(tableSkinId) {
    var library = diceTextureLibraryCache;
    if (!tableSkinId || !library) return "";
    var skin = null;
    (library.table_skins || []).forEach(function (item) {
        if (item && String(item.id) === String(tableSkinId) && item.enabled !== false) skin = item;
    });
    return skin ? diceAssetUrl(skin.asset_id) : "";
}

function diceFaceUrlsForPlan(plan) {
    var appearance = diceLoadAppearance();
    var urls = {};
    var types = [];
    (plan.groups || []).forEach(function (group) {
        var type = group && group.type ? String(group.type) : "";
        if (type && types.indexOf(type) < 0) types.push(type);
    });
    if (!types.length) {
        var fallback = diceDieTypeFor(Number(plan.sides) || 6, plan.mode);
        if (fallback) types.push(fallback);
    }
    types.forEach(function (type) {
        var faceUrls = diceFaceUrls(type, appearance.diceSkinIds);
        Object.keys(faceUrls).forEach(function (face) { urls[face] = faceUrls[face]; });
    });
    return urls;
}

/* ------------------------------------------------------------ DiceRollMeta 转换（照 diceTable.ts 平移） */

function dicePlanFromMeta(meta) {
    return {
        mode: meta.mode || "standard",
        expression: meta.expression || "1d100",
        count: Number(meta.count) || 1,
        sides: Number(meta.sides) || 6,
        bonus_dice: Number(meta.bonus_dice) || 0,
        penalty_dice: Number(meta.penalty_dice) || 0,
        candidates: meta.candidates || [],
        groups: meta.groups || [],
        selected: meta.selected == null ? null : meta.selected,
        check: {
            name: meta.check_name || "",
            difficulty: meta.difficulty || null,
            target: meta.target == null ? null : meta.target
        }
    };
}

function diceSnapshotFromMeta(meta) {
    return {
        faces: meta.faces || [],
        labels: meta.labels || [],
        groups: meta.groups || [],
        selected: meta.selected == null ? null : meta.selected,
        roll: meta.roll == null ? null : meta.roll,
        rolls: meta.rolls || [],
        total: meta.total == null ? null : meta.total,
        success: meta.success == null ? null : meta.success,
        success_level: meta.success_level == null ? null : meta.success_level,
        summary: "",
        check_name: meta.check_name == null ? null : meta.check_name,
        difficulty: meta.difficulty == null ? null : meta.difficulty,
        target: meta.target == null ? null : meta.target,
        roller: meta.roller == null ? null : meta.roller,
        mode: meta.mode || "standard",
        expression: meta.expression || "",
        count: meta.count == null ? 0 : meta.count,
        sides: meta.sides == null ? 0 : meta.sides,
        candidates: meta.candidates || [],
        natural: meta.natural == null ? null : meta.natural,
        advantage: Boolean(meta.advantage),
        disadvantage: Boolean(meta.disadvantage),
        reroll: meta.reroll == null ? null : meta.reroll
    };
}

function diceRollerLabel(roller) {
    if (!roller) return "";
    if (typeof roller === "string") return roller;
    var name = roller.username;
    return name === undefined || name === null ? "" : String(name);
}

function diceRollerUserId(meta) {
    var roller = meta.roller;
    if (roller && typeof roller === "object") return String(roller.user_id || meta.roller_user_id || "");
    return String(meta.roller_user_id || "");
}

function diceCountFor(meta) {
    var groups = meta.groups;
    if (Array.isArray(groups) && groups.length) {
        var total = 0;
        groups.forEach(function (group) {
            if (group && typeof group === "object") total += Math.max(0, asInt(group.count, 0));
        });
        if (total > 0) return total;
    }
    if (Array.isArray(meta.faces) && meta.faces.length) return meta.faces.length;
    var count = asInt(meta.count, 0);
    if (count > 0) return count;
    // 兜底：从表达式粗算各 NdM 项的 N 之和。
    var estimated = 0;
    String(meta.expression || "").toLowerCase().split("+").forEach(function (term) {
        var match = /^(\d+)?d\d+$/.exec(term.trim());
        if (match) estimated += Math.max(1, parseInt(match[1] || "1", 10));
    });
    return estimated || 1;
}

function diceGroupsHaveHighlight(groups, kind) {
    if (!Array.isArray(groups)) return false;
    for (var index = 0; index < groups.length; index += 1) {
        var highlights = groups[index] && groups[index].highlights;
        if (!Array.isArray(highlights)) continue;
        for (var slot = 0; slot < highlights.length; slot += 1) {
            if (highlights[slot] === kind) return true;
        }
    }
    return false;
}

function diceBundleDiceLimit() {
    // dice3d bundle 的硬上限（main.ts rebuild）：窄屏 10、宽屏 20。
    return window.innerWidth < 720 ? 10 : 20;
}

function diceTraySize() {
    return window.innerWidth < 720 ? { width: 190, height: 128 } : { width: 264, height: 168 };
}

function validQuickRollExpression(expression) {
    // 快捷掷骰限定 NdM 加法：项是 NdM（N 1–99 缺省 1，M 2–100）或 0–999 常数。
    var text = String(expression || "").replace(/\s+/g, "").toLowerCase();
    if (!text || text.length > 60) return false;
    if (!/^[0-9d+]+$/.test(text)) return false;
    var terms = text.split("+");
    for (var index = 0; index < terms.length; index += 1) {
        var term = terms[index];
        if (!term) return false;
        var die = /^(\d{1,2})?d(\d{1,3})$/.exec(term);
        if (die) {
            var count = parseInt(die[1] || "1", 10);
            var sides = parseInt(die[2], 10);
            if (count < 1 || count > 99) return false;
            if (sides < 2 || sides > 100) return false;
            continue;
        }
        if (!/^\d{1,3}$/.test(term)) return false;
        if (parseInt(term, 10) > 999) return false;
    }
    return true;
}

/* ==========================================================================
   规则速查（kiwee 深库，玩家侧）

   入口位置：**左上工具组尾部书图标**。右侧栏 tab 条已有六个面板 + 核心设置 +
   折叠/浮层共 8 个槽位，再加一个会挤出或折行；工具组是横向图标条、剩余空间
   充裕，且「速查」是跨面板的全局工具，不是第七个面板。

   数据只走玩家侧端点（权限裁剪全在服务端完成，前端不做权限推测）：
   - ``GET /api/plugins/dnd5e/rules/search``：名称/英文名/别名检索。类目目录
     取响应里的 ``categories[]``（服务端已剔除怪物/DMG 掷表/变体规则）；未建库
     时回 200 + ``available:false`` + ``hint``，面板显示「规则深库未建」引导，
     不弹错误框。
   - ``GET /api/plugins/dnd5e/rules/entry/<category>/<uid>``：详情（解析后正文 +
     ``structured`` + ``deep_link``）。玩家侧类目任何登录用户可读；本面板不列
     怪物类目，因此不会撞 403。

   性能与竞态：面板按需打开（首次打开才发请求）；输入防抖 250ms；检索用请求
   序号守卫丢弃过期响应（后发先至也不会把旧结果画上去）；结果固定 limit=20，
   列表是普通 DOM（不上虚拟化）。快捷键 Ctrl/⌘+K（核心只占用 Alt+B 与
   Ctrl+Shift+C）与 ``/``（焦点在输入框/可编辑区时不抢键）。
   ========================================================================== */

var RULES_CATEGORY_LABELS = {
    spell: "法术",
    item: "物品",
    baseitem: "基础物品",
    feat: "专长",
    race: "种族",
    class: "职业",
    subclass: "子职业",
    background: "背景",
    condition: "状态",
    disease: "疾病"
};

// 服务端 categories[] 已剔除这些类目；前端再兜一层，避免上游改动把 DM 内容
// 漏进玩家界面（怪物图鉴 / DMG 掷表 / 变体规则）。
var RULES_DM_ONLY = { monster: true, variantrule: true, table: true };

var RULES_AREA_LABELS = {
    sphere: "球状",
    cone: "锥形",
    line: "线形",
    cube: "立方",
    cylinder: "柱状",
    rectangle: "矩形",
    hemisphere: "半球"
};

// 非法术类目的 structured 字段中文名（对象键序＝渲染顺序）。
var RULES_FIELD_LABELS = {
    level: "等级",
    school: "学派",
    rarity: "稀有度",
    attunement: "同调",
    type: "类型",
    size: "体型",
    cr: "挑战等级",
    ac: "护甲等级",
    hp: "生命值",
    time: "使用时间",
    range: "距离",
    components: "成分",
    duration: "持续时间",
    prerequisite: "前置条件",
    weaponCategory: "武器类别",
    property: "属性",
    dmg1: "伤害",
    dmg2: "附加伤害",
    dmgType: "伤害类型",
    damageInflict: "伤害类型",
    savingThrow: "豁免",
    value: "价值",
    weight: "重量"
};

var RULES_DEBOUNCE = 250;
var RULES_RESULT_LIMIT = 20;
var RULES_SEARCH_PATH = "/api/plugins/dnd5e/rules/search";
var RULES_ENTRY_PATH = "/api/plugins/dnd5e/rules/entry/";

function rulesCategoryLabel(id) {
    return RULES_CATEGORY_LABELS[id] || String(id || "");
}

function rulesValueText(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.map(rulesValueText).filter(Boolean).join("、");
    if (typeof value === "object") return ""; // 复杂对象不猜语义
    if (typeof value === "boolean") return value ? "是" : "否";
    return String(value).trim();
}

/**
 * 正文转段落：空行分段、段内换行保留为 ``<br>``（服务端 text 已解析 datatag）。
 */
function rulesParagraphs(text) {
    var normalized = String(text || "").replace(/\r\n/g, "\n").trim();
    if (!normalized) return '<p class="vtt-rules-text-empty">这条记录没有正文。</p>';
    return normalized
        .split(/\n{2,}/)
        .map(function (block) {
            return "<p>" + escapeHtml(block).replace(/\n/g, "<br>") + "</p>";
        })
        .join("");
}

/**
 * 结构化摘要行：法术按机制字段渲染（环阶/施法时间/距离/成分/持续时间/豁免/
 * 伤害/区域/升环），其余类目按 ``entry_fields`` 的字段白名单渲染，未覆盖的
 * 字段不猜语义、直接不显示。
 */
function rulesSummaryRows(category, structured) {
    var data = structured && typeof structured === "object" ? structured : {};
    var rows = [];
    var seen = {};
    function push(label, value) {
        var text = rulesValueText(value);
        if (!text || seen[label]) return;
        seen[label] = true;
        rows.push({ label: label, value: text });
    }
    if (category === "spell") {
        var level = typeof data.level === "number" ? data.level : null;
        if (level !== null) push("环阶", level === 0 ? "戏法" : level + " 环");
        push("学派", data.school);
        push("施法时间", data.casting_time);
        push("距离", data.range_text);
        push("成分", data.components);
        var duration = rulesValueText(data.duration);
        if (data.concentration) duration = duration ? duration + "（专注）" : "专注";
        if (data.ritual) duration = duration ? duration + " · 仪式" : "仪式";
        push("持续时间", duration);
        push("豁免", data.save_ability_zh || data.save_ability);
        var damage = Array.isArray(data.damage) ? data.damage : [];
        if (damage.length) {
            push(
                "伤害",
                damage
                    .map(function (item) {
                        var dice = rulesValueText(item && item.dice);
                        var type = rulesValueText((item && (item.type_zh || item.type)) || "");
                        if (!dice) return "";
                        return type ? dice + "（" + type + "）" : dice;
                    })
                    .filter(Boolean)
                    .join(" + ")
            );
        }
        var area = data.area && typeof data.area === "object" ? data.area : {};
        if (area.shape) {
            var shape = RULES_AREA_LABELS[String(area.shape)] || String(area.shape);
            push("区域", area.size_ft ? shape + " " + area.size_ft + " 尺" : shape);
        }
        var scaling = data.scaling && typeof data.scaling === "object" ? data.scaling : {};
        var slotRange = rulesValueText(scaling.slot_range);
        var perLevel = rulesValueText(scaling.per_level);
        var scalingLine = [slotRange ? slotRange + " 环" : "", perLevel ? "每环 " + perLevel : ""].filter(Boolean).join(" · ");
        if (scalingLine) push("升环", scalingLine);
        return rows;
    }
    Object.keys(RULES_FIELD_LABELS).forEach(function (key) {
        if (!(key in data)) return;
        var value = data[key];
        if (key === "attunement") {
            if (value === true) push(RULES_FIELD_LABELS[key], "需要同调");
            else if (value !== false) push(RULES_FIELD_LABELS[key], rulesValueText(value));
            return;
        }
        push(RULES_FIELD_LABELS[key], value);
    });
    return rows;
}

/**
 * 规则速查浮层。``host`` 为挂在 ``#wsStageHost`` 里的工具组根；
 * ``options.onChange(isOpen)`` 用于同步入口按钮的选中态。
 * 首次打开才发请求；返回 ``{open, close, toggle, isOpen, destroy}``。
 */
function createRulesLookup(host, options) {
    var settings = options || {};
    var destroyed = false;
    var open = false;
    var el = null;
    var inputEl = null;
    var chipsEl = null;
    var bodyEl = null;
    var meta = { available: null, reason: "", hint: "", categories: [] };
    var chipsSignature = "";
    var category = "";
    var query = "";
    var seq = 0;
    var detailSeq = 0;
    var debounceTimer = 0;
    var view = "idle";     // idle | loading | results | empty | unavailable | error | detail
    var listView = "idle";
    var results = [];
    var detail = null;     // {loading, error, unavailable, request, item}
    var lastError = "";

    /* ---------------------------------------------------------- DOM（懒建） */

    function root() {
        if (el) return el;
        el = document.createElement("div");
        el.className = "vtt-rules-window";
        el.hidden = true;
        el.setAttribute("role", "dialog");
        el.setAttribute("aria-label", "规则速查");
        el.innerHTML =
            '<header class="vtt-rules-head">' +
            '<strong><i class="fa fa-book" aria-hidden="true"></i> 规则速查</strong>' +
            '<button type="button" class="vtt-rules-close" data-vtt-rules-close title="关闭（Esc）" aria-label="关闭规则速查">' +
            '<i class="fa fa-times" aria-hidden="true"></i></button>' +
            "</header>" +
            '<label class="vtt-rules-search">' +
            '<i class="fa fa-search" aria-hidden="true"></i>' +
            '<input type="search" data-vtt-rules-input placeholder="搜法术 / 物品 / 专长 / 职业…" autocomplete="off" spellcheck="false">' +
            "</label>" +
            '<div class="vtt-rules-chips" data-vtt-rules-chips hidden></div>' +
            '<div class="vtt-rules-body" data-vtt-rules-body></div>';
        host.appendChild(el);
        inputEl = el.querySelector("[data-vtt-rules-input]");
        chipsEl = el.querySelector("[data-vtt-rules-chips]");
        bodyEl = el.querySelector("[data-vtt-rules-body]");
        inputEl.addEventListener("input", onInput);
        el.addEventListener("click", onClick);
        return el;
    }

    function notifyChange() {
        if (typeof settings.onChange === "function") {
            try {
                settings.onChange(open);
            } catch (error) {
                // 入口态同步失败不影响面板本身。
            }
        }
    }

    /* ---------------------------------------------------------- 状态与渲染 */

    function stateBox(icon, title, hint, extra) {
        return (
            '<div class="vtt-rules-state">' +
            '<i class="' + escapeHtml(icon) + '" aria-hidden="true"></i>' +
            "<strong>" + escapeHtml(title) + "</strong>" +
            (hint ? '<p class="vtt-rules-hint">' + escapeHtml(hint) + "</p>" : "") +
            (extra || "") +
            "</div>"
        );
    }

    function renderChips() {
        if (!chipsEl) return;
        var list = [{ id: "", label: "全部" }].concat(
            (meta.categories || []).map(function (item) {
                var id = String((item && item.id) || "");
                return { id: id, label: rulesCategoryLabel(id) };
            }).filter(function (item) {
                return item.id;
            })
        );
        chipsEl.hidden = list.length <= 1;
        if (chipsEl.hidden) return;
        var signature = JSON.stringify(list) + "|" + category;
        if (signature === chipsSignature) return;
        chipsSignature = signature;
        chipsEl.innerHTML = list
            .map(function (item) {
                var active = item.id === category;
                return (
                    '<button type="button" class="vtt-rules-chip' + (active ? " is-active" : "") + '"' +
                    ' data-vtt-rules-chip="' + escapeHtml(item.id) + '"' +
                    ' aria-pressed="' + (active ? "true" : "false") + '">' +
                    escapeHtml(item.label) +
                    "</button>"
                );
            })
            .join("");
    }

    function resultRow(item, index) {
        var machine = item && item.is_machine_translated;
        return (
            '<li><button type="button" class="vtt-rules-item" data-vtt-rules-open="' + index + '">' +
            '<span class="vtt-rules-item-name"><strong>' + escapeHtml(item.name || "（无名称）") + "</strong>" +
            (item.eng_name ? "<em>" + escapeHtml(item.eng_name) + "</em>" : "") +
            "</span>" +
            '<span class="vtt-rules-item-meta">' +
            '<span class="vtt-rules-cat">' + escapeHtml(rulesCategoryLabel(item.category)) + "</span>" +
            (item.page_hint ? '<span class="vtt-rules-src">' + escapeHtml(item.page_hint) + "</span>" : "") +
            (item.rules_version ? '<span class="vtt-rules-era">' + escapeHtml(item.rules_version) + "</span>" : "") +
            (machine
                ? '<span class="vtt-rules-flag" title="' + escapeHtml(item.translator_note || "机翻，未校对") + '">⚠️ 机翻</span>'
                : "") +
            "</span></button></li>"
        );
    }

    function listMarkup() {
        if (meta.available === false || view === "unavailable") {
            return (
                '<div class="vtt-rules-state">' +
                '<i class="fa fa-database" aria-hidden="true"></i>' +
                "<strong>规则深库未建</strong>" +
                '<p class="vtt-rules-hint">' + escapeHtml(meta.reason || "深库未建") + "</p>" +
                (meta.hint ? '<p class="vtt-rules-hint vtt-rules-hint-code">' + escapeHtml(meta.hint) + "</p>" : "") +
                "</div>"
            );
        }
        if (view === "loading") return stateBox("fa fa-spinner fa-spin", "检索中…", "");
        if (view === "error") {
            return stateBox(
                "fa fa-plug",
                "规则速查暂时不可用",
                lastError || "网络异常，请稍后重试。",
                '<div class="vtt-rules-actions"><button type="button" data-vtt-rules-retry>重试</button></div>'
            );
        }
        if (view === "empty") return stateBox("fa fa-search", "没有匹配的条目", "换个关键词试试，或把类目切回「全部」。");
        if (view === "results") {
            return (
                '<ul class="vtt-rules-list">' + results.map(resultRow).join("") + "</ul>" +
                '<p class="vtt-rules-footnote">共 ' + results.length + " 条（上限 " + RULES_RESULT_LIMIT + "）· 点条目看正文与深链</p>"
            );
        }
        return stateBox(
            "fa fa-book",
            "查法术、物品、专长与职业",
            "输入中文或英文名开始检索；列表带来源页码，机翻条目带 ⚠️ 徽章。",
            '<p class="vtt-rules-footnote">数据来源：kiwee 5etools 中文镜像（CC BY-NC-SA 4.0）· 仅玩家侧类目</p>'
        );
    }

    function detailBackButton() {
        return (
            '<div class="vtt-rules-detail-head">' +
            '<button type="button" class="vtt-rules-back" data-vtt-rules-back>' +
            '<i class="fa fa-angle-left" aria-hidden="true"></i> 返回列表</button>' +
            "</div>"
        );
    }

    function detailMarkup() {
        var state = detail || {};
        var item = state.item || {};
        if (state.loading) return detailBackButton() + stateBox("fa fa-spinner fa-spin", "读取条目…", "");
        if (state.error) {
            return (
                detailBackButton() +
                stateBox(
                    "fa fa-plug",
                    "条目读取失败",
                    state.error,
                    '<div class="vtt-rules-actions"><button type="button" data-vtt-rules-retry-detail>重试</button></div>'
                )
            );
        }
        if (state.unavailable) return detailBackButton() + stateBox("fa fa-database", "规则深库未建", meta.reason || "深库未建");

        var rows = rulesSummaryRows(String(item.category || ""), item.structured);
        var source = String(item.source || "");
        var pageHint = String(item.page_hint || "");
        var metaLine = [
            rulesCategoryLabel(item.category),
            // page_hint 形如「PHB 241」已含来源码，重复的 source 不再显示。
            source && pageHint.indexOf(source) !== 0 ? source : "",
            pageHint,
            item.rules_version ? "规则版本 " + item.rules_version : ""
        ]
            .filter(Boolean)
            .map(escapeHtml)
            .join(" · ");
        var deepLink = /^https?:\/\//i.test(String(item.deep_link || "")) ? String(item.deep_link) : "";
        return (
            detailBackButton() +
            '<h4 class="vtt-rules-title">' + escapeHtml(item.name || "（无名称）") +
            (item.eng_name ? ' <small>' + escapeHtml(item.eng_name) + "</small>" : "") + "</h4>" +
            '<p class="vtt-rules-detail-meta">' + metaLine + "</p>" +
            (item.is_machine_translated
                ? '<p class="vtt-rules-flagline"><span class="vtt-rules-flag">⚠️ 机翻</span>' +
                  escapeHtml(item.translator_note || "机翻，未校对") + "</p>"
                : "") +
            (rows.length
                ? '<dl class="vtt-rules-summary">' +
                  rows.map(function (row) {
                      return "<dt>" + escapeHtml(row.label) + "</dt><dd>" + escapeHtml(row.value) + "</dd>";
                  }).join("") +
                  "</dl>"
                : "") +
            '<div class="vtt-rules-text">' + rulesParagraphs(item.text) + "</div>" +
            (deepLink
                ? '<a class="vtt-rules-link" href="' + escapeHtml(deepLink) + '" target="_blank" rel="noopener">' +
                  '<i class="fa fa-external-link" aria-hidden="true"></i> 在 kiwi 站打开</a>'
                : '<p class="vtt-rules-footnote">这条记录没有站点深链。</p>')
        );
    }

    function render() {
        if (!el || destroyed) return;
        renderChips();
        el.setAttribute("data-vtt-rules-view", view);
        bodyEl.innerHTML = view === "detail" ? detailMarkup() : listMarkup();
        if (view !== "detail") bodyEl.scrollTop = 0;
    }

    /* ---------------------------------------------------------- 请求 */

    function applyMeta(data) {
        if (!data || typeof data !== "object") return;
        if (typeof data.available === "boolean") meta.available = data.available;
        if (typeof data.reason === "string" && data.reason) meta.reason = data.reason;
        if (typeof data.hint === "string" && data.hint) meta.hint = data.hint;
        if (Array.isArray(data.categories)) {
            meta.categories = data.categories.filter(function (entry) {
                var id = String((entry && entry.id) || "");
                return id && !RULES_DM_ONLY[id];
            });
        }
    }

    function bootstrap() {
        // 首次打开先探可用性（空查询也给准确的 available/hint 与类目目录）。
        if (meta.available !== null) {
            render();
            return;
        }
        var mine = ++seq;
        view = "loading";
        render();
        apiRequest("GET", RULES_SEARCH_PATH + "?q=&limit=1")
            .then(function (response) {
                if (destroyed || mine !== seq) return;
                if (!response || response.success !== true) {
                    lastError = String((response && response.message) || "检索接口没有响应。");
                    view = "error";
                    render();
                    return;
                }
                applyMeta(response.data);
                view = meta.available === false ? "unavailable" : "idle";
                render();
            })
            .catch(function () {
                if (destroyed || mine !== seq) return;
                lastError = "网络异常，未能连到规则深库。";
                view = "error";
                render();
            });
    }

    function runSearch() {
        if (destroyed) return;
        if (!query.trim()) {
            seq += 1; // 让在途响应作废
            results = [];
            view = listView = "idle";
            render();
            return;
        }
        if (meta.available === false) {
            view = listView = "unavailable";
            render();
            return;
        }
        var mine = ++seq;
        view = "loading";
        render();
        var url = RULES_SEARCH_PATH + "?q=" + encodeURIComponent(query.trim()) + "&limit=" + RULES_RESULT_LIMIT;
        if (category) url += "&category=" + encodeURIComponent(category);
        apiRequest("GET", url)
            .then(function (response) {
                if (destroyed || mine !== seq) return; // 竞态：旧响应丢弃
                if (!response || response.success !== true) {
                    lastError = String((response && response.message) || "检索失败，请稍后重试。");
                    view = "error";
                    render();
                    return;
                }
                applyMeta(response.data);
                if (meta.available === false) {
                    view = listView = "unavailable";
                    render();
                    return;
                }
                results = Array.isArray(response.data && response.data.results) ? response.data.results : [];
                view = listView = results.length ? "results" : "empty";
                render();
            })
            .catch(function () {
                if (destroyed || mine !== seq) return;
                lastError = "网络异常，检索未能完成。";
                view = "error";
                render();
            });
    }

    function openEntry(item) {
        if (destroyed || !item || !item.category || !item.uid) return;
        var mine = ++detailSeq;
        detail = { loading: true, request: item, item: item };
        view = "detail";
        render();
        var url = RULES_ENTRY_PATH + encodeURIComponent(String(item.category)) + "/" + encodeURIComponent(String(item.uid));
        apiRequest("GET", url)
            .then(function (response) {
                if (destroyed || mine !== detailSeq) return;
                if (!response || response.success !== true) {
                    detail = { loading: false, request: item, item: item, error: String((response && response.message) || "读取失败，请稍后重试。") };
                    render();
                    return;
                }
                var data = response.data || {};
                if (data.available === false) {
                    applyMeta(data);
                    detail = { loading: false, request: item, item: item, unavailable: true };
                    render();
                    return;
                }
                detail = { loading: false, request: item, item: data };
                render();
            })
            .catch(function () {
                if (destroyed || mine !== detailSeq) return;
                detail = { loading: false, request: item, item: item, error: "网络异常，条目未能读取。" };
                render();
            });
    }

    function backToList() {
        detail = null;
        view = listView;
        render();
    }

    /* ---------------------------------------------------------- 交互 */

    function onInput(event) {
        var value = String((event.target && event.target.value) || "");
        if (value === query) return;
        query = value;
        window.clearTimeout(debounceTimer);
        if (!query.trim()) {
            runSearch();
            return;
        }
        debounceTimer = window.setTimeout(runSearch, RULES_DEBOUNCE);
    }

    function onClick(event) {
        var target = event.target;
        if (!target || typeof target.closest !== "function") return;
        if (target.closest("[data-vtt-rules-close]")) {
            close();
            return;
        }
        var openButton = target.closest("[data-vtt-rules-open]");
        if (openButton) {
            var index = asInt(openButton.getAttribute("data-vtt-rules-open"), -1);
            if (index >= 0 && results[index]) openEntry(results[index]);
            return;
        }
        var chip = target.closest("[data-vtt-rules-chip]");
        if (chip) {
            category = String(chip.getAttribute("data-vtt-rules-chip") || "");
            chipsSignature = "";
            window.clearTimeout(debounceTimer);
            runSearch();
            return;
        }
        if (target.closest("[data-vtt-rules-back]")) {
            backToList();
            return;
        }
        if (target.closest("[data-vtt-rules-retry]")) {
            if (query.trim()) runSearch();
            else bootstrap();
            return;
        }
        if (target.closest("[data-vtt-rules-retry-detail]")) {
            if (detail && detail.request) openEntry(detail.request);
        }
    }

    function focusInput() {
        if (!inputEl) return;
        window.setTimeout(function () {
            if (!destroyed && inputEl) {
                inputEl.focus();
                inputEl.select();
            }
        }, 0);
    }

    function openPanel() {
        if (destroyed) return;
        root();
        var wasOpen = open;
        el.hidden = false;
        open = true;
        if (!wasOpen) {
            bootstrap();
            notifyChange();
        }
        focusInput();
    }

    function close() {
        window.clearTimeout(debounceTimer);
        if (!open && (!el || el.hidden)) return;
        open = false;
        if (el) el.hidden = true;
        notifyChange();
    }

    function isOpen() {
        return open && !!el && !el.hidden;
    }

    function toggle() {
        if (isOpen()) close();
        else openPanel();
    }

    return {
        open: openPanel,
        close: close,
        toggle: toggle,
        isOpen: isOpen,
        destroy: function () {
            destroyed = true;
            window.clearTimeout(debounceTimer);
            if (el) {
                el.remove();
                el = null;
            }
            inputEl = null;
            chipsEl = null;
            bodyEl = null;
        }
    };
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
    var sceneMaskTimer = 0;
    var drawScheduled = false;
    var pollTimer = 0;
    var socket = null;
    var socketHandlers = [];
    var unsubscribers = [];
    // M4 地图骰桌：设置快照、去重键、存活骰盘与飘字错位计数。
    var diceSettings = { enabled: true, max_dice: 20 };
    var diceRecentKeys = {};
    var diceTrays = [];
    var dicePopCursor = 0;
    var diceSettingsModal = null;

    /* ------------------------------------------------------------ DOM */

    var root = document.createElement("div");
    root.className = "vtt-stage-tools";

    var toolbar = document.createElement("div");
    toolbar.className = "vtt-toolbar";
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "战桌工具");
    root.appendChild(toolbar);

    // 规则速查浮层（玩家侧）：懒建 DOM，入口按钮创建后再同步选中态。
    var rulesButton = null;
    var rulesLookup = createRulesLookup(root, {
        onChange: function (isOpen) {
            if (rulesButton) rulesButton.classList.toggle("is-active", isOpen);
        }
    });

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
        // M4：骰桌动画设置（开关 + 骰数上限），仅 DM。
        var diceSettingsButton = document.createElement("button");
        diceSettingsButton.type = "button";
        diceSettingsButton.className = "vtt-tool-btn";
        diceSettingsButton.title = "骰桌动画设置（仅 DM）";
        diceSettingsButton.setAttribute("aria-label", "骰桌动画设置");
        diceSettingsButton.innerHTML = '<i class="fa fa-cog" aria-hidden="true"></i>';
        diceSettingsButton.addEventListener("click", openDiceSettings);
        toolbar.appendChild(diceSettingsButton);
    }

    // 规则速查入口：挂在工具组尾部（右侧栏 tab 条已有 8 个槽位，再加会挤出/
    // 折行；工具组是横向图标条，剩余空间充裕，且速查是跨面板的全局工具）。
    // 未登录（无 currentUser）时不渲染——房间内正常有会话，此处只做兜底。
    if (window.currentUser) {
        var rulesSeparator = document.createElement("span");
        rulesSeparator.className = "vtt-tool-sep";
        rulesSeparator.setAttribute("aria-hidden", "true");
        toolbar.appendChild(rulesSeparator);
        rulesButton = document.createElement("button");
        rulesButton.type = "button";
        rulesButton.className = "vtt-tool-btn vtt-tool-rules";
        rulesButton.title = "规则速查（Ctrl+K 或 /）";
        rulesButton.setAttribute("aria-label", "规则速查");
        rulesButton.setAttribute("data-vtt-tool", "rules");
        rulesButton.innerHTML = '<i class="fa fa-book" aria-hidden="true"></i>';
        rulesButton.addEventListener("click", function () {
            rulesLookup.toggle();
        });
        toolbar.appendChild(rulesButton);
    }

    var canvas = document.createElement("canvas");
    canvas.className = "vtt-canvas";
    canvas.setAttribute("aria-label", "测量模板与测距尺层");
    root.appendChild(canvas);

    var hintBar = document.createElement("div");
    hintBar.className = "vtt-tool-hint";
    root.appendChild(hintBar);

    // M4：底栏 d20 dock（全员可用）：快捷掷骰入口（d20/d6/d100 芯片 + 表达式输入）。
    var diceDock = document.createElement("div");
    diceDock.className = "vtt-dice-dock";
    diceDock.innerHTML =
        '<div class="vtt-dice-dock-panel" data-vtt-dice-panel hidden>' +
        '<div class="vtt-dice-chips">' +
        '<button type="button" data-vtt-chip="1d20">d20</button>' +
        '<button type="button" data-vtt-chip="1d6">d6</button>' +
        '<button type="button" data-vtt-chip="1d100">d100</button>' +
        "</div>" +
        '<form class="vtt-dice-expr" data-vtt-dice-form>' +
        '<input type="text" data-vtt-dice-input placeholder="如 2d6+1d4，仅支持 NdM 加法" autocomplete="off" spellcheck="false">' +
        '<button type="submit">掷骰</button>' +
        "</form>" +
        '<p class="vtt-dice-dock-hint">表达式仅支持 NdM 加法（如 1d20+3、2d6+1d4）；结果广播全员并落在地图上。</p>' +
        "</div>" +
        '<button type="button" class="vtt-dice-dock-btn" data-vtt-dice-toggle title="快捷掷骰" aria-label="快捷掷骰">d20</button>';
    root.appendChild(diceDock);
    var diceDockPanel = diceDock.querySelector("[data-vtt-dice-panel]");
    diceDock.querySelector("[data-vtt-dice-toggle]").addEventListener("click", function () {
        var hidden = diceDockPanel.hidden;
        diceDockPanel.hidden = !hidden;
        if (!hidden) return;
        var input = diceDock.querySelector("[data-vtt-dice-input]");
        if (input) input.focus();
    });
    Array.prototype.forEach.call(diceDock.querySelectorAll("[data-vtt-chip]"), function (chip) {
        chip.addEventListener("click", function () {
            quickRoll(chip.getAttribute("data-vtt-chip") || "1d20");
        });
    });
    diceDock.querySelector("[data-vtt-dice-form]").addEventListener("submit", function (event) {
        event.preventDefault();
        var input = diceDock.querySelector("[data-vtt-dice-input]");
        var expression = String((input && input.value) || "").trim();
        if (!validQuickRollExpression(expression)) {
            notify("表达式仅支持 NdM 加法（如 1d20+3、2d6+1d4）。", "info");
            return;
        }
        quickRoll(expression);
    });

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
        // 规则速查浮层上的按下不算画布操作（否则会顺手选中底下的棋子）。
        if (event.target instanceof Element && event.target.closest(".vtt-rules-window")) return;
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

    function isTypingTarget(target) {
        // 焦点在输入框/可编辑区时不抢键（聊天输入、表单输入都算）。
        if (!target || !target.tagName) return false;
        var tag = String(target.tagName).toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") return true;
        return target.isContentEditable === true;
    }

    function handleRulesShortcut(event) {
        // 核心按键占用：Alt+B（侧栏折叠）、Ctrl+Shift+C（聊天浮层）、Esc、场景内
        // Ctrl+S。「/」与 Ctrl/⌘+K 未被占用，取作速查唤起键。
        if (destroyed) return false;
        var key = String(event.key || "");
        if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && key.toLowerCase() === "k") {
            event.preventDefault();
            rulesLookup.toggle();
            return true;
        }
        if (!event.ctrlKey && !event.metaKey && !event.altKey && key === "/" && !isTypingTarget(event.target)) {
            event.preventDefault();
            rulesLookup.open();
            return true;
        }
        return false;
    }

    function onKeyDown(event) {
        if (handleRulesShortcut(event)) return;
        if (event.key !== "Escape") return;
        if (rulesLookup.isOpen()) {
            rulesLookup.close();
            return;
        }
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
        if (diceSettingsModal) {
            closeDiceSettings();
            return;
        }
        if (diceDockPanel && !diceDockPanel.hidden) {
            diceDockPanel.hidden = true;
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
        // 规则速查：点浮层外（入口按钮除外）收起，Esc 也可关。
        if (rulesLookup.isOpen() && event.target instanceof Element) {
            if (!event.target.closest(".vtt-rules-window") && !event.target.closest(".vtt-tool-rules")) {
                rulesLookup.close();
            }
        }
        // M4：dock 面板外点关闭（在棋子卡判定之前独立处理）。
        if (diceDockPanel && !diceDockPanel.hidden) {
            if (!(event.target instanceof Element && event.target.closest(".vtt-dice-dock"))) {
                diceDockPanel.hidden = true;
            }
        }
        if (cardEl.hidden) return;
        if (event.target instanceof Element && event.target.closest(".vtt-token-card")) return;
        closeTokenCard();
    }

    /* ------------------------------------------------------------ M4 地图骰桌 */

    function applyDiceSettings(data) {
        if (!data || typeof data !== "object") return;
        if (typeof data.enabled === "boolean") diceSettings.enabled = data.enabled;
        if (data.max_dice != null) {
            diceSettings.max_dice = clamp(asInt(data.max_dice, 20), 1, 20);
        }
    }

    function loadDiceSettings() {
        return apiRequest("GET", "/rooms/" + encodeURIComponent(roomId) + "/dice-settings")
            .then(function (response) {
                if (destroyed || !response || !response.success) return;
                applyDiceSettings(response.data);
            })
            .catch(function (error) {
                console.debug("dnd5e-vtt dice settings unavailable", error);
            });
    }

    function hasMapStage() {
        // 无地图（veil-map 缺席或场景无背景）：地图动画与飘字整体跳过，
        // 聊天掷骰卡路径不受影响。
        return Boolean(mapState && String(mapState.background || "").trim());
    }

    function diceDedupKey(meta) {
        var request = String(meta.request_id || "").trim();
        if (request) return "id|" + request;
        var roller = meta.roller;
        var rollerId = roller && typeof roller === "object" ? String(roller.user_id || "") : String(roller || "");
        return [
            "expr", meta.expression || "",
            "faces", (meta.faces || []).join(","),
            "total", meta.total == null ? "" : String(meta.total),
            "roller", rollerId
        ].join("|");
    }

    function diceKeySeen(key) {
        var now = Date.now();
        Object.keys(diceRecentKeys).forEach(function (item) {
            if (!diceRecentKeys[item] || diceRecentKeys[item] < now) delete diceRecentKeys[item];
        });
        if (diceRecentKeys[key]) return true;
        diceRecentKeys[key] = now + DICE_DEDUP_TTL;
        return false;
    }

    function diceRollerToken(meta) {
        var rollerId = diceRollerUserId(meta);
        if (!rollerId) return null;
        var tokens = (mapState && Array.isArray(mapState.tokens)) ? mapState.tokens : [];
        for (var index = 0; index < tokens.length; index += 1) {
            var token = tokens[index];
            if (token && String(token.owner_user_id || "") === rollerId) return token;
        }
        return null;
    }

    function mapDiceAnchor(meta) {
        // 落点：掷骰者有可见棋子 → 棋子位置；否则视口中心。
        var token = diceRollerToken(meta);
        if (token) {
            var world = tokenWorld(token);
            var point = ctx.worldToScreen(world.x, world.y);
            return { x: point.x, y: point.y, onToken: true };
        }
        var viewport = ctx.getViewport() || {};
        return {
            x: Math.max(0, asFloat(viewport.width, 320)) / 2,
            y: Math.max(0, asFloat(viewport.height, 240)) / 2,
            onToken: false
        };
    }

    function clampDiceAnchor(x, y, width, height) {
        // 骰盘整体钳制在舞台可视区内。
        var viewport = ctx.getViewport() || {};
        var stageWidth = Math.max(width, asFloat(viewport.width, width));
        var stageHeight = Math.max(height, asFloat(viewport.height, height));
        return {
            x: clamp(x, width / 2, stageWidth - width / 2),
            y: clamp(y, height / 2, stageHeight - height / 2)
        };
    }

    function handleDiceMeta(meta) {
        if (destroyed || !meta || typeof meta !== "object") return;
        // new_message 与 combat_dice 双路同播：共用去重键短窗去重。
        if (diceKeySeen(diceDedupKey(meta))) return;
        if (!hasMapStage()) return;
        var anchor = mapDiceAnchor(meta);
        spawnDicePop(anchor, meta);
        if (!diceSettings.enabled) return; // DM 关闭：只留结果飘字。
        // 动画骰数上限取 DM 设置与 dice3d bundle 硬上限的较小值。
        var limit = Math.min(diceSettings.max_dice, diceBundleDiceLimit());
        if (diceCountFor(meta) > limit) return; // 超上限：只飘字。
        void spawnMapDiceTray(anchor, meta);
    }

    function spawnDicePop(anchor, meta) {
        var total = meta.total;
        var firstFace = Array.isArray(meta.faces) && meta.faces.length ? meta.faces[0] : null;
        var value = total != null ? total : firstFace != null ? firstFace : "—";
        var labelParts = [];
        if (meta.check_name) labelParts.push(String(meta.check_name));
        if (meta.expression) labelParts.push(String(meta.expression)); // 多骰合并显示 total + expression。
        var roller = diceRollerLabel(meta.roller);
        var natural = meta.natural == null ? null : asInt(meta.natural, 0);
        var classes = ["vtt-dice-pop"];
        if (natural === 20 || diceGroupsHaveHighlight(meta.groups, "critical")) {
            classes.push("is-crit");
        } else if (natural === 1 || diceGroupsHaveHighlight(meta.groups, "fumble")) {
            classes.push("is-fumble");
        }
        var pop = document.createElement("div");
        pop.className = classes.join(" ");
        pop.innerHTML =
            '<strong class="vtt-pop-value">' + escapeHtml(String(value)) + "</strong>" +
            '<span class="vtt-pop-label">' + escapeHtml(labelParts.join(" · ")) + "</span>" +
            (roller ? '<span class="vtt-pop-roller">' + escapeHtml(roller) + "</span>" : "");
        // 同屏多枚飘字按序错位，避免完全重叠。
        var jitter = ((dicePopCursor % 5) - 2) * 26;
        dicePopCursor += 1;
        var viewport = ctx.getViewport() || {};
        var stageWidth = Math.max(160, asFloat(viewport.width, 320));
        pop.style.left = Math.round(clamp(anchor.x + jitter, 84, stageWidth - 84)) + "px";
        pop.style.top = Math.round(Math.max(70, anchor.y - 26)) + "px";
        root.appendChild(pop);
        window.setTimeout(function () { pop.remove(); }, DICE_POP_MS);
    }

    function removeDiceTray(record) {
        if (!record) return;
        (record.timers || []).forEach(function (timer) { window.clearTimeout(timer); });
        record.timers = [];
        if (record.instance) {
            try { record.instance.dispose(); } catch (error) { console.debug("dnd5e-vtt tray dispose failed", error); }
            record.instance = null;
        }
        record.el.remove();
        var index = diceTrays.indexOf(record);
        if (index >= 0) diceTrays.splice(index, 1);
    }

    function freezeDiceTray(record) {
        // 超出同时存活上限：把最早一桌导出成静态图并释放 WebGL context。
        if (!record || record.frozen || !record.instance) return;
        record.frozen = true;
        var snapshot = null;
        try {
            snapshot = record.instance.freeze();
        } catch (error) {
            console.debug("dnd5e-vtt tray freeze failed", error);
        }
        try {
            record.instance.dispose();
        } catch (error) { /* 已判定不可用 */ }
        record.instance = null;
        if (!snapshot) {
            removeDiceTray(record);
            return;
        }
        var image = document.createElement("img");
        image.className = "dice3d-canvas dice3d-static";
        image.alt = "骰子结果";
        image.src = snapshot;
        record.el.innerHTML = "";
        record.el.appendChild(image);
    }

    function enforceDiceTrayLimit() {
        var live = diceTrays.filter(function (record) { return record.instance && !record.frozen; });
        while (live.length > MAP_DICE_MAX_LIVE) {
            freezeDiceTray(live.shift());
        }
    }

    function scheduleDiceTrayRemoval(record, delay) {
        (record.timers || []).forEach(function (timer) { window.clearTimeout(timer); });
        record.timers = [];
        record.timers.push(
            window.setTimeout(function () {
                record.el.classList.add("is-fading");
                record.timers.push(
                    window.setTimeout(function () { removeDiceTray(record); }, 420)
                );
            }, Math.max(0, delay))
        );
    }

    function spawnMapDiceTray(anchor, meta) {
        var plan = dicePlanFromMeta(meta);
        var snapshot = diceSnapshotFromMeta(meta);
        return Promise.all([loadDice3DBundle(), diceTextureLibrary()]).then(function (results) {
            if (destroyed || !results[0]) return; // bundle 缺席：静默降级，聊天卡照播。
            var size = diceTraySize();
            var clamped = clampDiceAnchor(anchor.x, anchor.y, size.width, size.height);
            var tray = document.createElement("div");
            tray.className = "vtt-map-dice";
            tray.setAttribute("aria-label", "地图骰盘");
            tray.style.width = size.width + "px";
            tray.style.height = size.height + "px";
            tray.style.left = Math.round(clamped.x - size.width / 2) + "px";
            tray.style.top = Math.round(clamped.y - size.height / 2) + "px";
            root.appendChild(tray);
            var record = { el: tray, instance: null, frozen: false, timers: [] };
            diceTrays.push(record);
            enforceDiceTrayLimit();
            // 安全兜底：动画即便卡死也按时移除。
            scheduleDiceTrayRemoval(record, MAP_DICE_TRAY_MS + 5200);
            var appearance = diceLoadAppearance();
            try {
                record.instance = window.TrpgDice3D.create(tray, {
                    plan: plan,
                    faceUrls: diceFaceUrlsForPlan(plan),
                    tableSkinUrl: diceTableSkinUrl(appearance.tableSkinId),
                    colors: diceResolvedColors(appearance)
                });
            } catch (error) {
                console.debug("dnd5e-vtt map dice create failed", error);
                removeDiceTray(record);
                return;
            }
            try {
                // 不传 seed：bundle 翻滚后把服务端 faces 贴到朝上面，动画即权威结果。
                Promise.resolve(record.instance.roll(snapshot)).then(
                    function () {
                        if (destroyed) return;
                        tray.classList.add("is-settled");
                        scheduleDiceTrayRemoval(record, MAP_DICE_TRAY_MS);
                    },
                    function (error) {
                        console.debug("dnd5e-vtt map dice roll failed", error);
                        removeDiceTray(record);
                    }
                );
            } catch (error) {
                // dice_count_exceeded / unsupported_dice / webgl_unavailable：静默降级仅飘字。
                console.debug("dnd5e-vtt map dice roll rejected", error);
                removeDiceTray(record);
            }
        }).catch(function (error) {
            console.debug("dnd5e-vtt map dice spawn failed", error);
        });
    }

    function closeDiceSettings() {
        if (!diceSettingsModal) return;
        diceSettingsModal.remove();
        diceSettingsModal = null;
    }

    function openDiceSettings() {
        if (diceSettingsModal) return;
        var modal = document.createElement("div");
        modal.className = "vtt-dice-settings";
        modal.innerHTML =
            '<div class="vtt-dice-settings-mask" data-vtt-dice-close></div>' +
            '<div class="vtt-dice-settings-card" role="dialog" aria-modal="true" aria-label="骰桌动画设置">' +
            "<header><i class=\"fa fa-cog\" aria-hidden=\"true\"></i><strong>骰桌动画</strong></header>" +
            '<label class="vtt-dice-toggle">' +
            '<input type="checkbox" data-vtt-dice-enabled>' +
            "<span>在地图上播放 3D 骰子动画<small>关闭后地图仅保留结果飘字；聊天掷骰卡不受影响。</small></span>" +
            "</label>" +
            '<label class="vtt-dice-limit">' +
            "<span>单次动画骰数上限</span>" +
            '<input type="number" min="1" max="20" step="1" data-vtt-dice-max>' +
            "<small>1–20；超过上限的掷骰只显示结果飘字。</small>" +
            "</label>" +
            "<footer>" +
            '<button type="button" data-vtt-dice-close>取消</button>' +
            '<button type="button" class="is-primary" data-vtt-dice-save>保存</button>' +
            "</footer>" +
            "</div>";
        var enabledInput = modal.querySelector("[data-vtt-dice-enabled]");
        var maxInput = modal.querySelector("[data-vtt-dice-max]");
        enabledInput.checked = diceSettings.enabled !== false;
        maxInput.value = String(clamp(asInt(diceSettings.max_dice, 20), 1, 20));
        modal.querySelectorAll("[data-vtt-dice-close]").forEach(function (element) {
            element.addEventListener("click", closeDiceSettings);
        });
        modal.querySelector("[data-vtt-dice-save]").addEventListener("click", function () {
            var maxDice = clamp(asInt(maxInput.value, diceSettings.max_dice), 1, 20);
            void saveDiceSettings(enabledInput.checked, maxDice);
        });
        document.body.appendChild(modal);
        diceSettingsModal = modal;
    }

    function saveDiceSettings(enabled, maxDice) {
        return apiRequest(
            "POST",
            "/rooms/" + encodeURIComponent(roomId) + "/dice-settings",
            { enabled: enabled, max_dice: maxDice }
        )
            .then(function (response) {
                if (destroyed) return response;
                if (!response || response.success !== true) {
                    notify(String((response && response.message) || "骰桌动画设置保存失败。"), "error");
                    if (response && response.data) applyDiceSettings(response.data);
                    return response;
                }
                applyDiceSettings(response.data);
                notify("骰桌动画设置已更新。", "success");
                closeDiceSettings();
                return response;
            })
            .catch(function (error) {
                console.error("dnd5e-vtt dice settings failed", error);
                if (!destroyed) notify("骰桌动画设置保存失败，请稍后重试。", "error");
                return null;
            });
    }

    function quickRoll(expression) {
        // 玩家快捷掷骰：复用现有 TrpgDiceTable.handleDiceTableCommand（与
        // /dice <expr> --table 同款：本地投掷 → persistLocalDiceMessage 落库
        // → 广播全员）。掷骰者自己收不到 send_message 回显（include_self=False），
        // 作用域内临时包一层 persistLocalDiceMessage 捕获 metadata.dice_roll
        // 喂地图动画；失败静默降级，仅聊天卡。
        var diceTable = window.TrpgDiceTable;
        if (!diceTable || typeof diceTable.handleDiceTableCommand !== "function") {
            notify("骰桌组件不可用，快捷掷骰未执行。", "error");
            return;
        }
        var original = window.persistLocalDiceMessage;
        var restored = false;
        function restorePersist() {
            if (restored) return;
            restored = true;
            if (window.persistLocalDiceMessage === wrapper) {
                window.persistLocalDiceMessage = original;
            }
        }
        function wrapper(content, metadata) {
            restorePersist();
            var meta = metadata && metadata.dice_roll;
            if (meta && typeof meta === "object") {
                try {
                    handleDiceMeta(meta);
                } catch (error) {
                    console.debug("dnd5e-vtt local dice feed failed", error);
                }
            }
            if (typeof original === "function") return original(content, metadata);
            return Promise.resolve(null);
        }
        window.persistLocalDiceMessage = wrapper;
        Promise.resolve()
            .then(function () { return diceTable.handleDiceTableCommand(expression); })
            .then(function () { restorePersist(); })
            .catch(function (error) {
                restorePersist();
                console.warn("dnd5e-vtt quick roll failed", error);
                notify("快捷掷骰失败。", "error");
            });
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

    function onSocketDiceSettings(payload) {
        if (destroyed || !payload) return;
        if (String(payload.room_id || "") !== roomId) return;
        applyDiceSettings(payload);
    }

    function onSocketCombatDice(payload) {
        // 服务端（NPC）掷骰广播：payload {room_id, meta}；与 new_message 双路
        // 同播由 handleDiceMeta 的去重键兜住。
        if (destroyed || !payload) return;
        if (String(payload.room_id || "") !== roomId) return;
        var meta = payload.meta;
        if (meta && typeof meta === "object") handleDiceMeta(meta);
    }

    function onSocketNewMessage(payload) {
        // 兼容两种形态：客户端中继 {room_id, message}；AI 工具裸消息
        // （字段平铺、room_id 顶层）。room_id 缺席时按 socket 房间归属放行。
        if (destroyed || !payload || typeof payload !== "object") return;
        var message = payload.message && typeof payload.message === "object" ? payload.message : payload;
        var messageRoom = String(message.room_id || payload.room_id || "");
        if (messageRoom && messageRoom !== roomId) return;
        var metadata = message.metadata;
        var meta = metadata && typeof metadata === "object" ? metadata.dice_roll : null;
        if (meta && typeof meta === "object") handleDiceMeta(meta);
    }

    function onSocketReconnect() {
        // 断线期间模板/地图/骰桌设置可能已变：重连后重新拉取。
        if (destroyed) return;
        loadTemplates();
        loadMapTokens();
        loadDiceSettings();
    }

    function onSocketSceneSwitch(payload) {
        // AI KP 切场景（dnd5e-vtt.switch_scene）：底图刷新走 veil-map 的
        // room_map_changed（broadcast_state 已推），这里只给 #wsStageHost 盖
        // 一层短暂遮罩，盖住背景重绘与棋子重排的闪变，淡入淡出 ~1s。
        if (destroyed || !payload) return;
        if (String(payload.room_id || "") !== roomId) return;
        var host = document.getElementById("wsStageHost");
        if (!host || !host.appendChild) return;
        var mask = host.querySelector(".vtt-scene-mask");
        if (!mask) {
            mask = document.createElement("div");
            mask.className = "vtt-scene-mask";
            mask.textContent = "场景切换中…";
            host.appendChild(mask);
        }
        mask.classList.remove("is-fading");
        window.clearTimeout(sceneMaskTimer);
        sceneMaskTimer = window.setTimeout(function () {
            mask.classList.add("is-fading");
            sceneMaskTimer = window.setTimeout(function () {
                if (mask.parentNode) mask.parentNode.removeChild(mask);
            }, 450);
        }, 600);
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
        bind("dnd5e_vtt_scene_switch", onSocketSceneSwitch);
        bind("dnd5e_vtt_dice_settings", onSocketDiceSettings);
        bind("combat_dice", onSocketCombatDice);
        bind("new_message", onSocketNewMessage);
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
    void loadDiceSettings();
    scheduleDraw();
    pollTimer = window.setInterval(function () {
        if (destroyed || document.visibilityState === "hidden") return;
        if (!socket && window.TrpgSocket) {
            attachSocket();
            void loadTemplates();
            void loadMapTokens();
            void loadDiceSettings();
            return;
        }
        if (!window.TrpgSocket || (socket && socket.connected === false)) {
            void loadTemplates();
            void loadMapTokens();
            void loadDiceSettings();
        }
    }, TEMPLATE_POLL);

    return {
        observer: roomObserver,
        destroy: function () {
            destroyed = true;
            window.clearInterval(pollTimer);
            window.clearTimeout(rulerQueryTimer);
            window.clearTimeout(rotateTimer);
            window.clearTimeout(sceneMaskTimer);
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
            // M4：骰盘逐个释放（WebGL context 不能泄漏），设置弹层收掉。
            diceTrays.slice().forEach(removeDiceTray);
            closeDiceSettings();
            rulesLookup.destroy();
            cardEl.remove();
            root.remove();
        }
    };
}

    window.TrpgStagePlugins = window.TrpgStagePlugins || {};
    window.TrpgStagePlugins["dnd5e-vtt"] = window.TrpgStagePlugins["dnd5e-vtt"] || {};
    window.TrpgStagePlugins["dnd5e-vtt"].mountRail = mountRail;
})();
