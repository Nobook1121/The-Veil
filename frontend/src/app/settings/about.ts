/**
 * 关于页：应用信息、更新检查、仓库入口、赞助与致谢、许可与声明。
 *
 * 只做「检查版本 + 下载发布包」，安装仍需用户手动替换目录后重启。
 * 数据全部来自 `GET /api/app/info` 与 `GET /api/app/update`，权限相关的
 * 开关由 `syncAdminOnly()` 在每次渲染后统一处理。
 */
(function initializeAbout(global: Window): void {
    "use strict";

    const POLL_INTERVAL_MS = 1500;
    /** 下载是后台线程；最多轮询 40 次（约 60s），超时后停止并提示，避免无限请求。 */
    const MAX_POLL_ATTEMPTS = 40;

    let initialized = false;
    let infoLoaded = false;
    let updatePollTimer: number | null = null;

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string): string {
        return global.TrpgI18n?.t(key, fallback) || fallback;
    }

    function setText(id: string, value: string): void {
        const element = byId(id);
        if (element) element.textContent = value;
    }

    function setHref(id: string, url: string | undefined, reveal = true): void {
        const element = byId<HTMLAnchorElement>(id);
        if (!element) return;
        element.href = url || "#";
        if (!url) {
            element.hidden = true;
            return;
        }
        element.hidden = !reveal;
    }

    function canManage(): boolean {
        const role = global.currentUser?.role;
        return role === "ADMIN" || role === "OWNER";
    }

    function shortSha(sha: string): string {
        return /^[0-9a-f]{7,40}$/i.test(sha) ? sha.slice(0, 7) : sha;
    }

    /** 动态文本进入 innerHTML 前必须转义；所有结构化渲染仍优先用 DOM API。 */
    function escapeHtml(value: string): string {
        return value
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ---------- 应用信息 ----------

    function renderCredits(info: AboutAppInfo): void {
        const credits = info.credits;
        const contributors = byId("creditsContributors");
        const specialThanks = byId("creditsSpecialThanks");
        const dependencies = byId("creditsDependencies");
        const summary = byId("aboutCreditsSummary");

        const appendPeople = (host: HTMLElement | null, people: AboutCreditPerson[]): number => {
            if (!host) return 0;
            host.replaceChildren();
            let count = 0;
            people.forEach((person) => {
                const name = String(person.name || "").trim();
                if (!name) return;
                const node = person.url ? document.createElement("a") : document.createElement("div");
                node.className = "credits-person";
                if (person.url) {
                    (node as HTMLAnchorElement).href = person.url;
                    (node as HTMLAnchorElement).target = "_blank";
                    (node as HTMLAnchorElement).rel = "noopener noreferrer";
                }
                const label = document.createElement("strong");
                label.className = "credit-name";
                label.textContent = name;
                const role = document.createElement("span");
                role.className = "credit-role";
                role.textContent = String(person.role || person.note || "");
                node.append(label, role);
                host.appendChild(node);
                count += 1;
            });
            return count;
        };

        const contributorCount = appendPeople(contributors, credits.contributors || []);
        const specialCount = appendPeople(specialThanks, credits.special_thanks || []);

        if (dependencies) {
            dependencies.replaceChildren();
            (credits.dependencies || []).slice(0, 12).forEach((dependency) => {
                const name = String(dependency.name || "").trim();
                if (!name) return;
                const chip = dependency.url ? document.createElement("a") : document.createElement("span");
                chip.className = "credits-dep";
                const version = String(dependency.version || "").trim();
                const license = String(dependency.license || "").trim();
                chip.textContent = [name + (version ? ` ${version}` : ""), license].filter(Boolean).join(" · ");
                if (dependency.url) {
                    (chip as HTMLAnchorElement).href = dependency.url;
                    (chip as HTMLAnchorElement).target = "_blank";
                    (chip as HTMLAnchorElement).rel = "noopener noreferrer";
                }
                chip.title = [name, version, license, String(dependency.usage || "")].filter(Boolean).join(" · ");
                dependencies.appendChild(chip);
            });
        }

        if (summary) {
            const empty = contributorCount + specialCount === 0;
            summary.hidden = !empty;
            summary.textContent = t("settings.about.credits.empty", "暂无致谢名单。");
        }

        const thirdPartyLink = byId<HTMLAnchorElement>("thirdPartyLink");
        if (thirdPartyLink) {
            const url = info.repo?.third_party || "";
            thirdPartyLink.hidden = !url;
            if (url) thirdPartyLink.href = url;
        }
    }

    function renderSponsors(info: AboutAppInfo): void {
        const channelsHost = byId("sponsorChannels");
        const list = byId("sponsorList");
        const empty = byId("sponsorEmptyState");
        const channels = info.credits.sponsor_channels || [];
        const sponsors = info.credits.sponsors || [];

        if (channelsHost) {
            channelsHost.replaceChildren();
            channels.forEach((channel) => {
                const url = String(channel.url || "").trim();
                if (!url) return;
                const link = document.createElement("a");
                link.className = "btn btn-outline-secondary sponsor-channel";
                link.href = url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                const label = escapeHtml(String(channel.label || channel.id || "").trim());
                const note = String(channel.note || "").trim();
                // label/note 来自站点配置，先转义再拼进受限的 strong/small。
                link.innerHTML = note ? `<strong>${label}</strong><small>${escapeHtml(note)}</small>` : `<strong>${label}</strong>`;
                channelsHost.appendChild(link);
            });
            channelsHost.hidden = channelsHost.childElementCount === 0;
        }

        if (list) {
            list.replaceChildren();
            sponsors.forEach((sponsor) => {
                const name = String(sponsor.name || "").trim();
                if (!name) return;
                const item = sponsor.url ? document.createElement("a") : document.createElement("div");
                item.className = "sponsor-item";
                if (sponsor.url) {
                    (item as HTMLAnchorElement).href = sponsor.url;
                    (item as HTMLAnchorElement).target = "_blank";
                    (item as HTMLAnchorElement).rel = "noopener noreferrer";
                }
                const tier = String(sponsor.tier || "").trim();
                if (tier) {
                    const badge = document.createElement("span");
                    badge.className = "sponsor-tier";
                    badge.dataset.tier = tier;
                    badge.textContent = tier;
                    item.appendChild(badge);
                }
                const label = document.createElement("span");
                label.className = "sponsor-name";
                label.textContent = name;
                item.appendChild(label);
                const since = String(sponsor.since || "").trim();
                if (since) {
                    const sinceNode = document.createElement("span");
                    sinceNode.className = "sponsor-since";
                    sinceNode.textContent = since;
                    item.appendChild(sinceNode);
                }
                const message = String(sponsor.message || "").trim();
                if (message) {
                    const messageNode = document.createElement("p");
                    messageNode.className = "sponsor-message";
                    messageNode.textContent = message;
                    item.appendChild(messageNode);
                }
                list.appendChild(item);
            });
        }

        if (empty) empty.hidden = (channelsHost?.childElementCount || 0) + (list?.childElementCount || 0) > 0;
    }

    function renderRepo(info: AboutAppInfo): void {
        const repo = info.repo;
        const repoUrl = repo?.url || "";
        setText("repoUrlValue", repoUrl || "—");

        const copyButton = byId<HTMLButtonElement>("copyRepoUrlButton");
        if (copyButton) copyButton.disabled = !repoUrl;

        setHref("starRepoButton", repoUrl);
        setHref("repoWikiButton", repo?.wiki);
        setHref("repoReleasesButton", repo?.releases);
        setHref("repoWikiLink", repo?.wiki);
        setHref("repoReleasesLink", repo?.releases);
        setHref("repoIssuesLink", repo?.issues);
        setHref("repoDiscussionsLink", repo?.discussions);

        const starLink = byId<HTMLAnchorElement>("repoStarLink");
        if (starLink) {
            starLink.href = repoUrl || "#";
            const hasStars = typeof repo?.stars === "number" && repo.stars >= 0;
            starLink.hidden = !repoUrl || !hasStars;
            setText("repoStarCount", hasStars ? String(repo.stars) : "");
        }

        setHref("licenseLink", info.license.url);
        // 隐私说明没有独立接口字段，优先指向仓库文档（wiki），退回仓库首页。
        setHref("privacyLink", repo?.wiki || repoUrl);
        setHref("thirdPartyPolicyLink", repo?.third_party);
    }

    function renderInfo(info: AboutAppInfo): void {
        setText("aboutAppName", info.name || "The Veil");
        setText("aboutVersionValue", `v${info.version}`);
        const channel = String(info.build.channel || "").trim();
        const channelChip = byId("aboutChannelValue");
        if (channelChip) {
            channelChip.textContent = channel;
            channelChip.hidden = !channel;
        }
        setText("aboutLicenseValue", info.license.spdx);
        setText("aboutRuntimeValue", `${info.runtime.python} · ${info.runtime.platform}`);

        setText("aboutBuildVersion", info.version);
        setText("aboutBuildMode", info.build.mode);
        setText("aboutBuildSha", info.build.sha);
        setText("aboutBuildTime", info.build.built_at || "—");
        setText("aboutBuildChannel", channel || "—");
        setText("aboutRuntimePython", info.runtime.python);
        setText("aboutRuntimePlatform", info.runtime.platform);
        setText("aboutLicenseName", info.license.name);
        setText("aboutLicenseSpdx", info.license.spdx);

        renderRepo(info);
        renderCredits(info);
        renderSponsors(info);
    }

    async function loadInfo(force = false): Promise<void> {
        if (infoLoaded && !force) return;
        const error = byId("aboutInfoError");
        const retry = byId("aboutInfoRetry");
        try {
            const response = await TrpgApi.get<ApiResponse<AboutAppInfo>>("/api/app/info");
            if (!response?.success || !response.data) {
                throw new Error(response?.message || response?.error || "无法读取应用信息");
            }
            renderInfo(response.data);
            infoLoaded = true;
            if (error) error.hidden = true;
            if (retry) retry.hidden = true;
        } catch (exc) {
            if (error) {
                error.hidden = false;
                error.textContent = `${t("settings.about.info_failed", "应用信息加载失败")}：${(exc as Error).message}`;
            }
            if (retry) retry.hidden = false;
        }
    }

    // ---------- 更新检查 ----------

    function updateLabel(state: string): { text: string; tone: string } {
        switch (state) {
            case "checking":
                return { text: t("settings.about.update.state_checking", "检查中…"), tone: "muted" };
            case "available":
                return { text: t("settings.about.update.state_available", "发现新版本"), tone: "warn" };
            case "up_to_date":
                return { text: t("settings.about.update.state_latest", "已是最新版本"), tone: "ok" };
            case "downloading":
                return { text: t("settings.about.update.state_downloading", "正在下载"), tone: "info" };
            case "ready":
                return { text: t("settings.about.update.state_downloaded", "已下载，待手动安装"), tone: "ok" };
            case "error":
                return { text: t("settings.about.update.state_error", "检查失败"), tone: "error" };
            case "disabled":
                return { text: t("settings.about.update.state_disabled", "更新检查已停用"), tone: "muted" };
            default:
                return { text: t("settings.about.update.state_idle", "尚未检查"), tone: "muted" };
        }
    }

    function formatCheckedAt(value: string): string {
        if (!value) return "—";
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return value;
        const pad = (part: number): string => String(part).padStart(2, "0");
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
    }

    function applyBadge(state: string): void {
        const badge = byId("updateStateBadge");
        if (!badge) return;
        const label = updateLabel(state);
        badge.textContent = label.text;
        badge.dataset.state = state;
        badge.classList.remove("ok", "warn", "error", "info", "muted");
        if (label.tone) badge.classList.add(label.tone);
    }

    function setCheckingBusy(busy: boolean): void {
        const button = byId<HTMLButtonElement>("updateCheckButton");
        if (button) {
            button.disabled = busy;
            button.classList.toggle("is-spinning", busy);
        }
    }

    function renderUpdate(state: AboutUpdateState): void {
        applyBadge(state.state);
        setText("updateCurrentVersion", state.current ? `v${state.current}` : "v—");
        setText("updateLatestVersion", state.latest ? `v${state.latest}` : "—");
        setText("updateCheckedAt", formatCheckedAt(state.checked_at));

        const message = byId("updateMessage");
        if (message) {
            message.hidden = !state.message;
            message.textContent = state.message || "";
        }

        const progressWrap = byId("updateProgress");
        const progressFill = byId("updateProgressFill");
        const progressText = byId("updateProgressText");
        const downloading = state.state === "downloading";
        const progress = Math.max(0, Math.min(state.progress || 0, 100));
        if (progressWrap) progressWrap.hidden = !downloading;
        if (progressFill) progressFill.style.width = `${progress}%`;
        if (progressText) progressText.textContent = downloading ? `${progress}%` : "";

        const checkButton = byId<HTMLButtonElement>("updateCheckButton");
        if (checkButton) {
            checkButton.disabled = !state.enabled || !canManage() || downloading;
            checkButton.classList.toggle("is-spinning", false);
        }

        const downloadButton = byId<HTMLButtonElement>("updateDownloadButton");
        if (downloadButton) {
            downloadButton.hidden = state.state !== "available" || !canManage();
            downloadButton.disabled = downloading;
        }

        const releaseLink = byId<HTMLAnchorElement>("updateReleaseLink");
        if (releaseLink) {
            releaseLink.hidden = !state.release_url;
            if (state.release_url) releaseLink.href = state.release_url;
        }

        const notes = byId("updateNotes");
        const notesToggle = byId<HTMLButtonElement>("updateNotesToggle");
        if (notes) {
            notes.textContent = state.notes || "";
            if (!state.notes) notes.hidden = true;
        }
        if (notesToggle) {
            notesToggle.hidden = !state.notes;
            const expanded = Boolean(notes && !notes.hidden);
            notesToggle.setAttribute("aria-expanded", String(expanded));
        }

        const instructions = byId("updateInstructions");
        if (instructions) instructions.hidden = state.state !== "ready";

        const readonlyNote = byId("updateReadonlyNote");
        if (readonlyNote) readonlyNote.hidden = canManage() || !state.enabled;

        syncAdminOnly();
    }

    async function refreshUpdate(): Promise<AboutUpdateState | null> {
        try {
            const response = await TrpgApi.get<ApiResponse<AboutUpdateState>>("/api/app/update");
            if (!response?.success || !response.data) return null;
            renderUpdate(response.data);
            return response.data;
        } catch {
            return null;
        }
    }

    function stopPolling(): void {
        if (updatePollTimer !== null) {
            global.clearTimeout(updatePollTimer);
            updatePollTimer = null;
        }
    }

    function showPollTimeout(): void {
        const message = byId("updateMessage");
        if (message) {
            message.hidden = false;
            message.textContent = t("settings.about.update.poll_timeout", "更新状态同步超时，请稍后刷新页面查看结果。");
        }
        setCheckingBusy(false);
        global.showNotification?.(t("settings.about.update.poll_timeout", "更新状态同步超时，请稍后刷新页面查看结果。"), "warning");
    }

    /** 下载在服务端后台线程执行，前端按 1.5s 轮询直到状态离开 downloading（最多 40 次）。 */
    function pollWhileDownloading(state: AboutUpdateState | null, attempts = 0): void {
        stopPolling();
        if (!state || state.state !== "downloading") return;
        if (attempts >= MAX_POLL_ATTEMPTS) {
            showPollTimeout();
            return;
        }
        updatePollTimer = global.setTimeout(async () => {
            updatePollTimer = null;
            const next = await refreshUpdate();
            pollWhileDownloading(next, attempts + 1);
        }, POLL_INTERVAL_MS);
    }

    async function checkForUpdate(): Promise<void> {
        applyBadge("checking");
        setCheckingBusy(true);
        try {
            const response = await TrpgApi.post<ApiResponse<AboutUpdateState>>("/api/app/update/check");
            if (response?.success && response.data) {
                renderUpdate(response.data);
                global.showNotification?.(response.data.message || t("settings.about.update.checked", "更新检查完成"), "info");
            } else {
                const message = response?.message || response?.error || t("settings.about.update.failed", "检查更新失败");
                global.showNotification?.(message, "error");
                const state = await refreshUpdate();
                pollWhileDownloading(state);
            }
        } catch (exc) {
            global.showNotification?.(`${t("settings.about.update.failed", "检查更新失败")}：${(exc as Error).message}`, "error");
        } finally {
            setCheckingBusy(false);
            const state = await refreshUpdate();
            pollWhileDownloading(state);
        }
    }

    async function downloadUpdate(): Promise<void> {
        const button = byId<HTMLButtonElement>("updateDownloadButton");
        if (button) button.disabled = true;
        try {
            const response = await TrpgApi.post<ApiResponse<AboutUpdateState>>("/api/app/update/download");
            if (response?.success) {
                global.showNotification?.(t("settings.about.update.downloading", "开始下载更新包"), "info");
            } else {
                global.showNotification?.(response?.message || response?.error || t("settings.about.update.failed", "下载失败"), "error");
            }
        } catch (exc) {
            global.showNotification?.(`${t("settings.about.update.failed", "下载失败")}：${(exc as Error).message}`, "error");
        } finally {
            if (button) button.disabled = false;
            const state = await refreshUpdate();
            pollWhileDownloading(state);
        }
    }

    // ---------- 交互 ----------

    function copyRepoUrl(): void {
        const value = byId("repoUrlValue")?.textContent?.trim() || "";
        if (!value) return;
        const fallback = (): void => {
            const node = byId("repoUrlValue");
            const selection = global.getSelection();
            if (node && selection) {
                const range = document.createRange();
                range.selectNodeContents(node);
                selection.removeAllRanges();
                selection.addRange(range);
            }
            global.showNotification?.(t("settings.about.repo.copy_failed", "已选中，请手动复制"), "info");
        };
        const clipboard = global.navigator?.clipboard;
        if (!clipboard?.writeText) {
            fallback();
            return;
        }
        void clipboard.writeText(value).then(
            () => global.showNotification?.(t("settings.about.repo.copied", "已复制仓库地址"), "info"),
            fallback,
        );
    }

    function toggleNotes(): void {
        const notes = byId("updateNotes");
        const toggle = byId<HTMLButtonElement>("updateNotesToggle");
        if (!notes || !toggle) return;
        const open = notes.hidden;
        notes.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
    }

    function syncAdminOnly(): void {
        const admin = canManage();
        document.querySelectorAll<HTMLElement>("#appUpdatePanel [data-admin-only]").forEach((element) => {
            element.hidden = !admin;
        });
    }

    function bindEvents(): void {
        byId<HTMLButtonElement>("updateCheckButton")?.addEventListener("click", () => void checkForUpdate());
        byId<HTMLButtonElement>("updateDownloadButton")?.addEventListener("click", () => void downloadUpdate());
        byId<HTMLButtonElement>("aboutInfoRetry")?.addEventListener("click", () => void loadInfo(true));
        byId<HTMLButtonElement>("copyRepoUrlButton")?.addEventListener("click", copyRepoUrl);
        byId<HTMLButtonElement>("updateNotesToggle")?.addEventListener("click", toggleNotes);
        global.addEventListener("trpg:locale-changed", () => {
            void refresh(true);
        });
        global.addEventListener("trpg:user-changed", () => {
            syncAdminOnly();
        });
    }

    async function refresh(forceInfo = false): Promise<void> {
        await Promise.all([
            loadInfo(forceInfo),
            refreshUpdate().then((state) => pollWhileDownloading(state)),
        ]);
    }

    function init(): void {
        if (initialized || !byId("about-settings-content")) return;
        initialized = true;
        bindEvents();
        void refresh();
    }

    global.TrpgAbout = { init, refresh: () => refresh(false) };
})(window);
