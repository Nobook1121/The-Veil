interface NavLinkItem {
    href: string;
    tab: string;
    label: string;
    labelKey?: string;
    active?: boolean;
    adminOnly?: boolean;
}

interface NavGroup {
    label: string;
    labelKey?: string;
    links: NavLinkItem[];
    adminOnly?: boolean;
}

const primaryLinks: NavLinkItem[] = [
    { href: "#home", tab: "home", label: "主页", labelKey: "nav.home", active: true },
    { href: "#character", tab: "character", label: "角色卡", labelKey: "nav.character" },
    { href: "#save", tab: "save", label: "房间", labelKey: "nav.rooms" },
    { href: "#agent", tab: "agent", label: "网站 Agent", labelKey: "nav.agent" },
];

const groupedLinks: NavGroup[] = [
    {
        label: "探索发现",
        labelKey: "nav.explore",
        links: [
            { href: "#scenario", tab: "scenario", label: "剧本", labelKey: "nav.scenarios" },
            { href: "#character-gallery", tab: "character-gallery", label: "角色卡广场", labelKey: "nav.character_gallery" },
        ],
    },
];

const settingsLink: NavLinkItem = {
    href: "#settings",
    tab: "settings",
    label: "设置",
    labelKey: "nav.settings",
    adminOnly: true,
};

function NavLink({ href, tab, label, labelKey, active = false, adminOnly = false }: NavLinkItem) {
    return (
        <a className={`nav-link${active ? " active" : ""}`} href={href} data-tab={tab} data-admin-only={adminOnly ? "true" : undefined}>
            <span data-i18n={labelKey}>{label}</span>
        </a>
    );
}

function NavGroupSection({ group }: { group: NavGroup }) {
    return (
        <li className="nav-item" data-admin-only={group.adminOnly ? "true" : undefined}>
            <button className="dropdown-btn" type="button">
                <span data-i18n={group.labelKey}>{group.label}</span>
                <i className="fa fa-caret-down" aria-hidden="true" />
            </button>
            <div className="dropdown-container">
                {group.links.map((link) => (
                    <NavLink key={link.href} {...link} />
                ))}
            </div>
        </li>
    );
}

export function Sidebar() {
    return (
        <div className="col-2 sidebar-expanded" id="sidebar">
            <div className="d-flex flex-column h-100 p-3">
                <div className="sidebar-header">
                    <h2 className="sidebar-title" data-i18n="app.title">
                        帷幕
                    </h2>
                    <button
                        type="button"
                        className="sidebar-toggle"
                        id="sidebarToggle"
                        title="收起侧边栏"
                        aria-label="收起侧边栏"
                        data-i18n-title="nav.collapse_sidebar"
                        data-i18n-aria-label="nav.collapse_sidebar"
                        aria-expanded="true"
                    >
                        <i className="fa fa-angle-double-left" aria-hidden="true" />
                    </button>
                </div>
                <ul className="nav flex-column mt-4">
                    {primaryLinks.map((link) => (
                        <li className="nav-item" key={link.href}>
                            <NavLink {...link} />
                        </li>
                    ))}
                    {groupedLinks.map((group) => (
                        <NavGroupSection key={group.label} group={group} />
                    ))}
                    <li className="nav-item" data-admin-only="true">
                        <NavLink {...settingsLink} />
                    </li>
                </ul>
                <div className="user-info mt-auto" id="userInfo" role="button" tabIndex={0} aria-label="打开用户设置" data-i18n-aria-label="nav.open_user_settings">
                    <div className="user-avatar" id="userAvatar">
                        <img src="https://via.placeholder.com/40" alt="用户头像" data-i18n-alt="nav.user_avatar" width="40" height="40" />
                    </div>
                    <div className="user-details">
                        <div className="user-name" id="userName" data-i18n="auth.status.guest">
                            未登录
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
