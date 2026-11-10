/**
 * 关于页：应用信息、构建信息、仓库入口、致谢与更新检查。
 *
 * 只做「检查版本 + 下载发布包」，安装仍需用户手动替换目录后重启；
 * 服务端接口对所有人公开，但下载/检查写入由 settings.general 权限保护。
 */
interface AboutAppInfo {
    name: string;
    version: string;
    build: { mode: string; sha: string; built_at?: string | null; channel: string };
    license: { spdx: string; name: string; url: string };
    repo: {
        url: string;
        github: string;
        issues: string;
        discussions: string;
        wiki: string;
        releases: string;
        license: string;
        third_party: string;
        stars: number | null;
    } | null;
    runtime: { python: string; platform: string };
    credits: {
        contributors: AboutCreditPerson[];
        special_thanks: AboutCreditPerson[];
        dependencies: AboutCreditDependency[];
        sponsor_channels: AboutSponsor[];
        sponsors: AboutSponsor[];
    };
}

interface AboutCreditPerson {
    name?: string;
    role?: string;
    url?: string;
}

interface AboutCreditDependency {
    name?: string;
    license?: string;
    url?: string;
}

interface AboutSponsor {
    name?: string;
    description?: string;
    url?: string;
}

interface AboutUpdateState {
    state: string;
    current: string;
    latest: string;
    notes: string;
    release_url: string;
    published_at: string;
    checked_at: string;
    message: string;
    progress: number;
    enabled: boolean;
}

interface TrpgAboutApi {
    init: () => void;
}

(function initializeAbout(global: Window) {
    "use strict";

    const POLL_INTERVAL_MS = 1500;

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

    // ---------- 应用信息 ----------

    function renderCredits(info: AboutAppInfo): void {
        const contributors = byId("aboutContributors");
        const dependencies = byId("aboutDependencies");
        const summary = byId("aboutCreditsSummary");
        const credits = info.credits;

        if (contributors) {
            contributors.replaceChildren();
            const people = [...(credits.contributors || []), ...(credits.special_thanks || [])];
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
                label.textContent = name;
                const role = document.createElement("span");
                role.textContent = String(person.role || "");
                node.append(label, role);
                contributors.appendChild(node);
            });
            if (summary) {
                summary.hidden = people.length > 0;
                summary.textContent = t("settings.about.credits.empty", "暂无致谢名单。");
            }
        }

        if (dependencies) {
            dependencies.replaceChildren();
            (credits.dependencies || []).forEach((dependency) => {
                const name = String(dependency.name || "").trim();
                if (!name) return;
                const chip = dependency.url ? document.createElement("a") : document.createElement("span");
                chip.className = "credits-dep";
                chip.textContent = dependency.license ? `${name} · ${dependency.license}` : name;
                if (dependency.url) {
                    (chip as HTMLAnchorElement).href = dependency.url;
                    (chip as HTMLAnchorElement).target = "_blank";
                    (chip as HTMLAnchorElement).rel = "noopener noreferrer";
                }
                chip.title = dependency.license || name;
                dependencies.appendChild(chip);
            });
        }
    }

    function renderSponsors(info: AboutAppInfo): void {
        const section = byId("sponsorSection");
        const list = byId("sponsorList");
        if (!section || !list) return;
        const channels = info.credits.sponsor_channels || [];
        const sponsors = info.credits.sponsors || [];
        list.replaceChildren();

        const append = (item: AboutSponsor): void => {
            const name = String(item.name || "").trim();
            if (!name) return;
            const card = item.url ? document.createElement("a") : document.createElement("div");
            card.className = "sponsor-card";
            if (item.url) {
                (card as HTMLAnchorElement).href = item.url;
                (card as HTMLAnchorElement).target = "_blank";
                (card as HTMLAnchorElement).rel = "noopener noreferrer";
            }
            const title = document.createElement("strong");
            title.textContent = name;
            const desc = document.createElement("small");
            desc.textContent = String(item.description || "");
            card.append(title, desc);
            list.appendChild(card);
        };
        channels.forEach(append);
        sponsors.forEach(append);
        section.hidden = list.childElementCount === 0;
    }

    function renderInfo(info: AboutAppInfo): void {
        setText("aboutAppName", info.name || "The Veil");
        setText("aboutVersionValue", `v${info.version}`);
        setText("aboutBuildValue", `${info.build.mode} · ${shortSha(info.build.sha)}`);
        setText("aboutLicenseValue", info.license.spdx);

        const repo = info.repo;
        const group = byId("repoLinkGroup");
        if (group) group.hidden = !repo;
        if (repo) {
            setHref("repoGithub", repo.url);
            setHref("repoIssues", repo.issues);
            setHref("repoDiscussions", repo.discussions);
            setHref("repoWikiLink", repo.wiki);
            setHref("repoReleases", repo.releases);
            const starLink = byId<HTMLAnchorElement>("repoStarLink");
            if (starLink) {
                starLink.href = repo.url;
                const hasStars = typeof repo.stars === "number" && repo.stars >= 0;
                starLink.hidden = !hasStars;
                setText("repoStarCount", hasStars ? String(repo.stars) : "");
            }
        }

        setText("aboutBuildVersion", info.version);
        setText("aboutBuildMode", info.build.mode);
        setText("aboutBuildSha", info.build.sha);
        setText("aboutBuildTime", info.build.built_at || "—");
        setText("aboutBuildChannel", info.build.channel);
        setText("aboutRuntimePython", info.runtime.python);
        setText("aboutRuntimePlatform", info.runtime.platform);
        setText("aboutLicenseName", info.license.name);
        setText("aboutLicenseSpdx", info.license.spdx);
        setHref("aboutLicenseLink", info.license.url);
        setHref("aboutThirdPartyLink", repo?.third_party);

        renderCredits(info);
        renderSponsors(info);
    }

    async function loadInfo(force = false): Promise<void> {
        if (infoLoaded && !force) return;
        const error = byId("aboutInfoError");
        const retry = byId("aboutInfoRetry");
        retry?.addEventListener("click", () => void loadInfo(true), { once: true });
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
            case "available":
                return { text: t("settings.about.update.state_available", "发现新版本"), tone: "warn" };
            case "up_to_date":
                return { text: t("settings.about.update.state_latest", "已是最新版本"), tone: "ok" };
            case "downloading":
                return { text: t("settings.about.update.state_downloading", "正在下载"), tone: "warn" };
            case "ready":
                return { text: t("settings.about.update.state_downloaded", "已下载，待手动安装"), tone: "ok" };
            case "error":
                return { text: t("settings.about.update.state_error", "检查失败"), tone: "error" };
            case "disabled":
                return { text: t("settings.about.update.state_disabled", "更新检查已停用"), tone: "" };
            default:
                return { text: t("settings.about.update.state_idle", "尚未检查"), tone: "" };
        }
    }

    function formatCheckedAt(value: string): string {
        if (!value) return "—";
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return value;
        const pad = (part: number): string => String(part).padStart(2, "0");
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
    }

    function renderUpdate(state: AboutUpdateState): void {
        const pill = byId("updateStateBadge");
        if (pill) {
            const label = updateLabel(state.state);
            pill.textContent = label.text;
            pill.dataset.state = state.state;
            pill.classList.remove("ok", "warn", "error");
            if (label.tone) pill.classList.add(label.tone);
        }
        setText("updateCurrentVersion", state.current ? `v${state.current}` : "v—");
        setText("updateLatestVersion", state.latest ? `v${state.latest}` : "—");
        setText("updateCheckedAt", formatCheckedAt(state.checked_at));

        const message = byId("updateMessage");
        if (message) {
            message.hidden = !state.message;
            message.textContent = state.message || "";
        }

        const progressWrap = byId("updateProgressWrap");
        const progressFill = byId("updateProgressFill");
        const downloading = state.state === "downloading";
        if (progressWrap) progressWrap.hidden = !downloading;
        if (progressFill) progressFill.style.width = `${Math.max(0, Math.min(state.progress || 0, 100))}%`;

        const checkButton = byId<HTMLButtonElement>("updateCheckButton");
        if (checkButton) {
            checkButton.disabled = !state.enabled || !canManage() || state.state === "downloading";
        }

        const downloadButton = byId<HTMLButtonElement>("updateDownloadButton");
        if (downloadButton) {
            downloadButton.hidden = state.state !== "available" || !canManage();
            downloadButton.disabled = state.state === "downloading";
        }

        const releaseLink = byId<HTMLAnchorElement>("updateReleaseLink");
        if (releaseLink) {
            releaseLink.hidden = !state.release_url;
            if (state.release_url) releaseLink.href = state.release_url;
        }

        const notes = byId("updateNotes");
        if (notes) {
            notes.hidden = !state.notes;
            notes.textContent = state.notes || "";
        }

        const readonlyNote = byId("updateReadonlyNote");
        if (readonlyNote) {
            readonlyNote.hidden = canManage() || !state.enabled;
        }
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
            window.clearTimeout(updatePollTimer);
            updatePollTimer = null;
        }
    }

    /** 下载是后台线程，前端按 1.5s 轮询直到状态离开 downloading。 */
    function pollWhileDownloading(state: AboutUpdateState | null): void {
        stopPolling();
        if (!state || state.state !== "downloading") return;
        updatePollTimer = window.setTimeout(async () => {
            const next = await refreshUpdate();
            pollWhileDownloading(next);
        }, POLL_INTERVAL_MS);
    }

    async function checkForUpdate(): Promise<void> {
        const button = byId<HTMLButtonElement>("updateCheckButton");
        if (button) button.disabled = true;
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
            if (button) button.disabled = false;
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

    function bindOnce(): void {
        if (initialized) return;
        initialized = true;
        byId<HTMLButtonElement>("updateCheckButton")?.addEventListener("click", () => void checkForUpdate());
        byId<HTMLButtonElement>("updateDownloadButton")?.addEventListener("click", () => void downloadUpdate());
        byId<HTMLButtonElement>("aboutInfoRetry")?.addEventListener("click", () => void loadInfo(true));
        global.addEventListener("trpg:locale-changed", () => {
            if (infoLoaded) void loadInfo(true);
            void refreshUpdate();
        });
    }

    function init(): void {
        if (!byId("about-settings-content")) return;
        bindOnce();
        void loadInfo();
        void refreshUpdate().then(pollWhileDownloading);
    }

    global.TrpgAbout = { init };
})(window);
