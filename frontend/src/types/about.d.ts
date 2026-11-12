/**
 * 关于页（设置 → 关于）的数据契约。
 *
 * 数据来自 `GET /api/app/info` 与 `GET /api/app/update`；字段与
 * `backend/trpg_server/app_info.py`、`backend/trpg_server/update_service.py`
 * 的公开返回保持一致，前端不自行扩展服务端未提供的字段。
 */
interface AboutBuildInfo {
    mode: string;
    sha: string;
    built_at?: string | null;
    channel: string;
}

interface AboutLicenseInfo {
    spdx: string;
    name: string;
    url: string;
}

/**
 * 仓库信息。`repo` 为 null 表示当前构建无法确定公开仓库（例如本地开发），
 * 此时关于页不显示仓库区块。
 */
interface AboutRepoInfo {
    url: string;
    github: string;
    issues: string;
    discussions: string;
    wiki: string;
    releases: string;
    license: string;
    third_party: string;
    stars: number | null;
}

interface AboutCreditPerson {
    name?: string;
    role?: string;
    url?: string;
    note?: string;
    /** 可选 i18n key：存在时优先于 name / role 的字面文案。 */
    name_key?: string;
    role_key?: string;
}

interface AboutCreditDependency {
    name?: string;
    version?: string;
    license?: string;
    url?: string;
    usage?: string;
}

/** 赞助渠道（credits.json → sponsor_channels，由后端过滤占位条目后返回）。 */
interface AboutSponsorChannel {
    id?: string;
    label?: string;
    url?: string;
    note?: string;
}

/** 赞助人（credits.json → sponsors）。tier 仅影响徽章文案，不展示金额。 */
interface AboutSponsor {
    name?: string;
    tier?: string;
    since?: string;
    url?: string;
    message?: string;
}

interface AboutCredits {
    contributors: AboutCreditPerson[];
    special_thanks: AboutCreditPerson[];
    dependencies: AboutCreditDependency[];
    sponsor_channels: AboutSponsorChannel[];
    sponsors: AboutSponsor[];
}

interface AboutAppInfo {
    name: string;
    version: string;
    build: AboutBuildInfo;
    license: AboutLicenseInfo;
    repo: AboutRepoInfo | null;
    runtime: { python: string; platform: string };
    credits: AboutCredits;
}

/**
 * 更新状态机（后端 `_public_state()`）：
 * unknown → up_to_date | available | error；available → downloading → ready | error；
 * disabled 表示更新检查被站点配置关闭。`progress` 为 0-100 的整数。
 * 注意：后端当前不返回下载产物路径，ready 态由前端展示安装指引。
 */
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
    /** 幂等初始化；首次进入「关于」标签时由 switchSettingsTab 调用。 */
    init: () => void;
    /** 重新拉取应用信息与更新状态（供设置页刷新按钮/事件使用）。 */
    refresh: () => Promise<void>;
}
