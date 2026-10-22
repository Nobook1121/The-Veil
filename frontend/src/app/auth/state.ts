interface Window {
    currentUser: AuthModule.CurrentUser | null;
    initAuth: () => Promise<boolean>;
    reconnectSocket?: () => void;
    disconnectSocket?: () => void;
    clearCurrentRoom?: () => void;
    clearChatMessages?: () => void;
    clearCharacterManagement?: () => void;
    reloadCharacterManagement?: () => Promise<void>;
    autoLoadLastRoom?: () => Promise<void>;
    setCurrentEditingScenarioId?: (id: string | number | null) => void;
    loadUserSettings?: () => Promise<void>;
}

namespace AuthModule {
    export interface CurrentUser {
        user_id: number;
        username: string;
        role: string;
        email?: string;
        avatar?: string;
        nickname?: string;
        presence?: "online" | "dnd" | "invisible";
        two_factor_enabled?: boolean;
        must_change_password?: boolean;
        csrf_token?: string;
        impersonation_mode?: boolean;
        impersonated_by?: number | string;
        impersonated_by_username?: string;
    }

    export let currentEditingScenarioId: string | number | null = null;

    export function setCurrentEditingScenarioId(id: string | number | null): void {
        currentEditingScenarioId = id;
    }

    export function setCurrentUser(user: CurrentUser | null): void {
        window.currentUser = user;
        const name = document.getElementById("userName");
        const avatar = document.querySelector<HTMLImageElement>(".user-avatar img");
        const cardName = document.getElementById("userCardName");
        const cardRole = document.getElementById("userCardRole");
        const cardImpersonation = document.getElementById("userCardImpersonation");
        const cardImpersonationText = document.getElementById("userCardImpersonationText");
        const cardAvatar = document.getElementById("userCardAvatar") as HTMLImageElement | null;
        const homeAvatar = document.getElementById("personalHomeAvatar") as HTMLImageElement | null;
        const homeName = document.getElementById("personalHomeName");
        const homeMeta = document.getElementById("personalHomeMeta");
        const homeRole = document.getElementById("personalHomeRole");
        const homePresence = document.getElementById("personalHomePresence");
        const homePresenceDot = document.getElementById("personalHomePresenceDot");
        const presenceButtonLabel = document.querySelector<HTMLElement>("#presence-menu-button .presence-current-label");
        if (user) {
            if (name) name.textContent = user.username;
            if (avatar) avatar.src = TrpgAvatar.resolveAvatarSrc(user.username, user.avatar);
            if (cardName) cardName.textContent = user.username;
            if (cardRole) cardRole.textContent = user.role || "USER";
            if (cardImpersonation) cardImpersonation.hidden = !user.impersonation_mode;
            if (cardImpersonationText) {
                cardImpersonationText.textContent = user.impersonation_mode
                    ? `${authText("auth.status.impersonating", "模拟登录中")}${user.impersonated_by_username ? ` · ${authText("auth.status.original_account", "原账号 {name}", { name: user.impersonated_by_username })}` : ""}`
                    : "";
            }
            if (cardAvatar) cardAvatar.src = TrpgAvatar.resolveAvatarSrc(user.username, user.avatar);
            if (homeAvatar) homeAvatar.src = TrpgAvatar.resolveAvatarSrc(user.username, user.avatar);
            if (homeName) homeName.textContent = user.nickname || user.username;
            if (homeRole) homeRole.textContent = user.role || "USER";
            if (homeMeta) {
                const baseMeta = user.email || authText("auth.status.email_missing", "未设置邮箱");
                homeMeta.textContent = user.impersonation_mode
                    ? `${baseMeta} · ${authText("auth.status.impersonating", "模拟登录中")}`
                    : baseMeta;
            }
            if (presenceButtonLabel) presenceButtonLabel.textContent = presenceLabel(user.presence || "online");
        } else {
            if (name) name.textContent = authText("auth.status.guest", "未登录");
            if (avatar) avatar.src = TrpgAvatar.avatarTextDataUri("?");
            if (cardName) cardName.textContent = authText("auth.status.guest", "未登录");
            if (cardRole) cardRole.textContent = "USER";
            if (cardImpersonation) cardImpersonation.hidden = true;
            if (cardImpersonationText) cardImpersonationText.textContent = "";
            if (cardAvatar) cardAvatar.src = TrpgAvatar.avatarTextDataUri("?");
            if (homeAvatar) homeAvatar.src = TrpgAvatar.avatarTextDataUri("?");
            if (homeName) homeName.textContent = authText("auth.status.guest", "未登录");
            if (homeRole) homeRole.textContent = "USER";
            if (homeMeta) homeMeta.textContent = authText("auth.status.guest", "未登录");
            if (presenceButtonLabel) presenceButtonLabel.textContent = authText("presence.status", "在线状态");
        }
        const presence = window.currentUser?.presence || "online";
        if (homePresence) homePresence.textContent = window.currentUser ? presenceLabel(presence) : authText("presence.status", "在线状态");
        if (homePresenceDot) homePresenceDot.className = window.currentUser ? `fa ${presenceIconClass(presence)} presence-dot ${presence}` : "fa fa-circle presence-dot";
        document.querySelectorAll<HTMLElement>("#user-settings .settings-presence-option[data-presence]").forEach((button) => {
            button.classList.toggle("active", button.dataset.presence === presence);
        });
        window.refreshAdminNavigation?.();
        window.refreshScenarioManagement?.();
        // 未登录时标记 body，供剧本/角色卡页的「登录后可创建」提示条显隐（CSS 控制）。
        document.body.classList.toggle("is-guest", !user);
    }

    function presenceLabel(presence: CurrentUser["presence"]): string {
        switch (presence) {
            case "dnd":
                return authText("presence.dnd", "请勿打扰");
            case "invisible":
                return authText("presence.invisible", "隐身");
            default:
                return authText("presence.online", "在线");
        }
    }

    function presenceIconClass(presence: CurrentUser["presence"]): string {
        switch (presence) {
            case "dnd":
                return "fa-minus-circle";
            case "invisible":
                return "fa-circle-o";
            default:
                return "fa-circle";
        }
    }

    export function authText(key: string, fallback: string, values: Record<string, string | number> = {}): string {
        return window.TrpgI18n?.t(key, fallback, values) || fallback;
    }

    export function showAuthModal(): void {
        const modal = document.getElementById("auth-modal");
        if (modal) modal.style.display = "flex";
    }

    export function closeAuthModal(): void {
        const modal = document.getElementById("auth-modal");
        if (modal) modal.style.display = "none";
    }

    export function showMessage(elementId: string, message: string, isError = false): void {
        const element = document.getElementById(elementId);
        if (!element) return;
        element.textContent = message;
        element.style.color = isError ? "red" : "green";
    }
}

window.addEventListener("trpg:locale-changed", () => {
    AuthModule.setCurrentUser(window.currentUser || null);
});
