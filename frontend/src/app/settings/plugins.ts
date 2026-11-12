/**
 * 设置页「插件」分类：第三方插件清单、启用/禁用、重新扫描与卸载。
 *
 * 数据全部来自 `/api/plugins`（仅 ADMIN/OWNER 可访问），前端不猜测插件状态：
 * - 状态、失败原因码与可读原因按接口原样展示；
 * - 启用/禁用接口返回的 `restart_required` 表示「代码插件已禁用，但已注册的
 *   HTTP 路由仍在当前进程里」，需要重启服务才能彻底回滚，这里原样提示；
 * - 卸载必须第一次确认后手动输入插件 id，DELETE 请求再带回 `{confirm: id}`。
 */
interface SettingsPluginManifest {
    id: string;
    name: string;
    version: string;
    description: string;
    author: string;
}

interface SettingsPluginRecord {
    id: string;
    state: string;
    enabled: boolean;
    reason: string;
    reason_code: string;
    /** 系统内置插件为只读条目，没有插件目录。 */
    dir?: string;
    /** 发现位置，由后端给出：plugin_dir | template_pack | system。 */
    source?: string;
    /** 系统内置（随包发布的规则集），不可启用/禁用/卸载。 */
    system?: boolean;
    capabilities: string[];
    pending_restart?: boolean;
    manifest: SettingsPluginManifest | null;
}

interface SettingsPluginSummary {
    root: string;
    app_version: string;
    api_version: number;
    disabled_all: boolean;
    loaded_at: string;
    counts: Record<string, number>;
    /** data/plugins 下的第三方插件。 */
    plugins: SettingsPluginRecord[];
    /** 随包发布的系统内置插件（只读，不计入 counts）。 */
    system_plugins?: SettingsPluginRecord[];
}

interface SettingsPluginMutation {
    plugin: SettingsPluginRecord | null;
    restart_required: boolean;
    enabled_ids: string[];
}

interface TrpgPluginsApi {
    init: () => void;
}

(function initializeSettingsPlugins(global: Window) {
    "use strict";

    const STATE_KEYS: Record<string, string> = {
        loaded: "settings.plugins.state.loaded",
        disabled: "settings.plugins.state.disabled",
        failed: "settings.plugins.state.failed",
        available: "settings.plugins.state.available",
        pending_restart: "settings.plugins.state.pending_restart",
    };

    let initialized = false;
    let loading = false;
    let lastSummary: SettingsPluginSummary | null = null;
    const busy = new Set<string>();

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string, values: Record<string, string | number> = {}): string {
        return global.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function errorText(error: unknown): string {
        if (error instanceof Error && error.message) return error.message;
        return String(error ?? "");
    }

    function apiError(response: ApiResponse<unknown> | null | undefined, fallback: string): string {
        return response?.message || response?.error || fallback;
    }

    function setMessage(text: string, tone: "" | "success" | "error"): void {
        const message = byId("pluginsMessage");
        if (!message) return;
        message.textContent = text;
        message.className = `settings-message${tone ? ` ${tone}` : ""}`;
    }

    function stateLabel(state: string): string {
        const key = STATE_KEYS[state];
        return key ? t(key, state) : t("settings.plugins.state.unknown", "未知状态");
    }

    /** 来源只读后端给出的发现位置，前端不做任何推断。 */
    const SOURCE_LABELS: Record<string, [string, string]> = {
        plugin_dir: ["settings.plugins.source.directory", "插件目录"],
        template_pack: ["settings.plugins.source.pack", "模板包"],
        system: ["settings.plugins.source.system", "系统内置"],
    };

    function sourceLabel(record: SettingsPluginRecord): string {
        const labels = record.source ? SOURCE_LABELS[record.source] : undefined;
        if (!labels) return t("settings.plugins.source.unknown", "未知来源");
        return t(labels[0], labels[1]);
    }

    function renderSummary(summary: SettingsPluginSummary | null): void {
        const host = byId("pluginsSummary");
        if (!host) return;
        if (!summary) {
            host.textContent = "";
            return;
        }
        const counts = summary.counts || {};
        const total = Array.isArray(summary.plugins) ? summary.plugins.length : 0;
        const parts = [
            `<span>${escapeHtml(t("settings.plugins.summary", "第三方插件目录：{root} · 共 {count} 个", { root: summary.root, count: total }))}</span>`,
            `<span>${escapeHtml(t("settings.plugins.counts", "第三方：已加载 {loaded} · 已禁用 {disabled} · 失败 {failed}", {
                loaded: counts.loaded || 0,
                disabled: counts.disabled || 0,
                failed: counts.failed || 0,
            }))}</span>`,
        ];
        if (summary.disabled_all) {
            parts.push(
                `<span class="plugin-summary-warning">${escapeHtml(t("settings.plugins.disabled_all", "运维参数已禁用全部第三方插件（--no-plugins / TRPG_PLUGINS_DISABLED），重启前不会加载任何插件。"))}</span>`,
            );
        }
        host.innerHTML = parts.join("");
    }

    function renderCard(record: SettingsPluginRecord): string {
        const manifest = record.manifest;
        const isSystem = Boolean(record.system);
        const name = manifest?.name || record.id;
        const version = manifest?.version || "—";
        const author = manifest?.author || "—";
        const description = manifest?.description || t("settings.plugins.no_description", "该插件未提供说明。");
        const isBusy = busy.has(record.id);
        const checked = record.enabled || record.state === "loaded";
        const failure = record.state === "failed"
            ? `<p class="plugin-card-error"><span>${escapeHtml(t("settings.plugins.reason_code_label", "失败码"))}</span>`
                + `<code class="plugin-card-error-code">${escapeHtml(record.reason_code || "unknown")}</code>`
                + `<span>${escapeHtml(record.reason || t("settings.plugins.reason_unknown", "插件加载失败，详情见服务端日志。"))}</span></p>`
            : "";
        // 代码插件禁用后蓝图仍存活到重启；后端把标记持久化在条目上，
        // 刷新页面也要继续提示「需重启」，不能只靠一次性提示条。
        const pendingRestart = Boolean(record.pending_restart) && record.state !== "loaded";
        const displayState = pendingRestart ? "pending_restart" : record.state;
        const restartNote = pendingRestart
            ? `<p class="plugin-card-error"><span>${escapeHtml(
                record.reason || t("settings.plugins.restart_required", "已禁用。该插件注册的 HTTP 路由仍在当前进程中，需重启服务才能完全回滚。")
            )}</span></p>`
            : "";
        // 系统内置插件随包发布，不可启用/禁用/卸载，只展示状态与来源。
        const actions = isSystem
            ? `<span class="plugin-state-badge" data-state="${escapeHtml(displayState)}">${escapeHtml(stateLabel(displayState))}</span>`
                + `<span class="plugin-readonly">${escapeHtml(t("settings.plugins.builtin_readonly", "内置只读"))}</span>`
            : `<span class="plugin-state-badge" data-state="${escapeHtml(displayState)}">${escapeHtml(stateLabel(displayState))}</span>
                        <label class="plugin-switch">
                            <input type="checkbox" data-plugin-toggle="${escapeHtml(record.id)}"${checked ? " checked" : ""}${isBusy ? " disabled" : ""}>
                            <span>${escapeHtml(t("settings.plugins.enable_label", "启用"))}</span>
                        </label>
                        <button type="button" class="btn btn-sm btn-outline-danger" data-plugin-uninstall="${escapeHtml(record.id)}"${isBusy ? " disabled" : ""}>
                            <i class="fa fa-trash-o" aria-hidden="true"></i> ${escapeHtml(t("settings.plugins.uninstall", "卸载"))}
                        </button>`;
        return `
            <article class="plugin-card" data-state="${escapeHtml(displayState)}"${isSystem ? ' data-system="true"' : ""} data-plugin-id="${escapeHtml(record.id)}">
                <div class="plugin-card-top">
                    <div class="plugin-card-heading">
                        <h4 class="plugin-card-name">${escapeHtml(name)}<code class="plugin-card-id">${escapeHtml(record.id)}</code></h4>
                        <p class="settings-section-desc">${escapeHtml(description)}</p>
                    </div>
                    <div class="plugin-card-actions">
                        ${actions}
                    </div>
                </div>
                <dl class="plugin-facts">
                    <div><dt>${escapeHtml(t("settings.plugins.version", "版本"))}</dt><dd>${escapeHtml(version)}</dd></div>
                    <div><dt>${escapeHtml(t("settings.plugins.author", "作者"))}</dt><dd>${escapeHtml(author)}</dd></div>
                    <div><dt>${escapeHtml(t("settings.plugins.source", "来源"))}</dt><dd>${escapeHtml(sourceLabel(record))}</dd></div>
                </dl>
                ${failure}
                ${restartNote}
            </article>`;
    }

    function renderGroup(
        key: string,
        title: string,
        records: SettingsPluginRecord[],
        emptyText: string,
    ): string {
        const body = records.length
            ? records.map(renderCard).join("")
            : `<div class="plugin-empty plugin-empty--group"><p>${escapeHtml(emptyText)}</p></div>`;
        return `
            <section class="plugin-group" data-plugin-group="${escapeHtml(key)}">
                <header class="plugin-group-head">
                    <div class="plugin-group-heading">
                        <h4 class="plugin-group-title">${escapeHtml(title)}<span class="plugin-group-count">${records.length}</span></h4>
                        <p class="settings-section-desc">${escapeHtml(
                            key === "system"
                                ? t("settings.plugins.group.system_note", "随程序发布的规则集，只读展示，不支持启用、禁用或卸载。")
                                : t("settings.plugins.group.third_party_note", "放在插件目录中、由站点管理员安装的插件，默认不启用。")
                        )}</p>
                    </div>
                </header>
                <div class="plugin-group-body">${body}</div>
            </section>`;
    }

    function renderList(summary: SettingsPluginSummary): void {
        const list = byId("pluginsList");
        if (!list) return;
        const thirdParty = Array.isArray(summary.plugins) ? summary.plugins : [];
        const system = Array.isArray(summary.system_plugins) ? summary.system_plugins : [];
        if (thirdParty.length === 0 && system.length === 0) {
            list.innerHTML = `<div class="plugin-empty"><i class="fa fa-plug" aria-hidden="true"></i>`
                + `<p>${escapeHtml(t("settings.plugins.empty", "当前版本没有内置插件，也没有安装第三方插件。"))}</p></div>`;
            return;
        }
        list.innerHTML = [
            renderGroup("system", t("settings.plugins.group.system", "系统插件"), system,
                t("settings.plugins.empty_system", "当前版本没有可用的系统插件。")),
            renderGroup("third_party", t("settings.plugins.group.third_party", "第三方插件"), thirdParty,
                t("settings.plugins.empty_third_party", "还没有安装第三方插件。把插件文件夹放进上面的插件目录后，点「重新扫描」。")),
        ].join("");
    }

    function renderAll(): void {
        renderSummary(lastSummary);
        if (lastSummary) renderList(lastSummary);
    }

    async function load(showLoading: boolean): Promise<void> {
        if (loading) return;
        loading = true;
        const list = byId("pluginsList");
        if (showLoading) {
            setMessage("", "");
            if (list) {
                list.innerHTML = `<div class="plugin-empty"><i class="fa fa-spinner fa-spin" aria-hidden="true"></i>`
                    + `<p>${escapeHtml(t("settings.plugins.loading", "正在读取插件列表…"))}</p></div>`;
            }
        }
        try {
            const response = await TrpgApi.get<ApiResponse<SettingsPluginSummary>>("/api/plugins");
            if (!response?.success || !response.data) {
                throw new Error(apiError(response, t("settings.plugins.load_failed", "插件列表加载失败")));
            }
            lastSummary = response.data;
            renderAll();
        } catch (error) {
            lastSummary = null;
            renderSummary(null);
            const message = `${t("settings.plugins.load_failed", "插件列表加载失败")}：${errorText(error)}`;
            if (list) {
                list.innerHTML = `<div class="plugin-empty plugin-empty-error"><i class="fa fa-exclamation-triangle" aria-hidden="true"></i>`
                    + `<p>${escapeHtml(message)}</p>`
                    + `<button type="button" class="btn btn-sm btn-outline-secondary mt-2" data-plugin-retry>${escapeHtml(t("settings.plugins.retry", "重试"))}</button></div>`;
            }
            setMessage(message, "error");
        } finally {
            loading = false;
        }
    }

    async function setEnabled(id: string, enabled: boolean, input: HTMLInputElement | null): Promise<void> {
        if (!id || busy.has(id)) return;
        busy.add(id);
        if (input) input.disabled = true;
        try {
            const action = enabled ? "enable" : "disable";
            const response = await TrpgApi.post<ApiResponse<SettingsPluginMutation>>(
                `/api/plugins/${encodeURIComponent(id)}/${action}`,
            );
            if (!response?.success) {
                throw new Error(apiError(response, t("settings.plugins.toggle_failed", "操作失败")));
            }
            if (response.data?.restart_required) {
                setMessage(t("settings.plugins.restart_required", "已禁用。该插件注册的 HTTP 路由仍在当前进程中，需重启服务才能完全回滚。"), "success");
            } else {
                setMessage(
                    enabled
                        ? t("settings.plugins.enable_done", "已启用该插件。")
                        : t("settings.plugins.disable_done", "已禁用该插件。"),
                    "success",
                );
            }
        } catch (error) {
            setMessage(`${t("settings.plugins.toggle_failed", "操作失败")}：${errorText(error)}`, "error");
        } finally {
            busy.delete(id);
            if (input) input.disabled = false;
            await load(false);
        }
    }

    async function rescan(): Promise<void> {
        const button = byId<HTMLButtonElement>("pluginsRescanButton");
        if (button) button.disabled = true;
        try {
            const response = await TrpgApi.post<ApiResponse<SettingsPluginSummary>>("/api/plugins/rescan");
            if (!response?.success || !response.data) {
                throw new Error(apiError(response, t("settings.plugins.rescan_failed", "重新扫描失败")));
            }
            lastSummary = response.data;
            renderAll();
            setMessage(t("settings.plugins.rescan_done", "已重新扫描插件目录。"), "success");
        } catch (error) {
            setMessage(`${t("settings.plugins.rescan_failed", "重新扫描失败")}：${errorText(error)}`, "error");
        } finally {
            if (button) button.disabled = false;
        }
    }

    async function uninstall(id: string): Promise<void> {
        if (!id || busy.has(id)) return;
        const record = lastSummary?.plugins.find((item) => item.id === id) || null;
        if (record && (record.enabled || record.state === "loaded")) {
            setMessage(t("settings.plugins.uninstall_need_disable", "请先禁用该插件再卸载。"), "error");
            return;
        }
        const dialog = global.TrpgDialog;
        if (!dialog) return;
        const answer = await dialog.prompt({
            title: t("settings.plugins.uninstall_title", "卸载插件"),
            message: t("settings.plugins.uninstall_message", "卸载会删除插件目录 {id}，操作不可恢复。请输入插件 id 以确认。", { id }),
            confirmText: t("settings.plugins.uninstall_confirm", "确认卸载"),
            danger: true,
        });
        if (answer === null) return;
        if (answer.trim() !== id) {
            setMessage(t("settings.plugins.uninstall_mismatch", "输入的 id 与插件 id 不一致，已取消卸载。"), "error");
            return;
        }
        busy.add(id);
        try {
            const response = await TrpgApi.del<ApiResponse<{ id: string; removed: boolean }>>(
                `/api/plugins/${encodeURIComponent(id)}`,
                { body: { confirm: id } },
            );
            if (!response?.success) {
                throw new Error(apiError(response, t("settings.plugins.uninstall_failed", "卸载失败")));
            }
            setMessage(t("settings.plugins.uninstall_done", "已卸载 {id}。", { id }), "success");
        } catch (error) {
            setMessage(`${t("settings.plugins.uninstall_failed", "卸载失败")}：${errorText(error)}`, "error");
        } finally {
            busy.delete(id);
            await load(false);
        }
    }

    function bindOnce(): void {
        if (initialized) return;
        initialized = true;
        byId<HTMLButtonElement>("pluginsRescanButton")?.addEventListener("click", () => void rescan());
        const list = byId("pluginsList");
        list?.addEventListener("change", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLInputElement)) return;
            const id = target.dataset.pluginToggle;
            if (!id) return;
            void setEnabled(id, target.checked, target);
        });
        list?.addEventListener("click", (event) => {
            const target = event.target;
            if (!(target instanceof Element)) return;
            const uninstallButton = target.closest<HTMLElement>("[data-plugin-uninstall]");
            if (uninstallButton) {
                void uninstall(uninstallButton.dataset.pluginUninstall || "");
                return;
            }
            if (target.closest("[data-plugin-retry]")) void load(true);
        });
        global.addEventListener("trpg:locale-changed", () => {
            if (lastSummary) renderAll();
        });
    }

    function init(): void {
        if (!byId("plugins-settings-content")) return;
        bindOnce();
        void load(true);
    }

    global.TrpgPlugins = { init };
})(window);
