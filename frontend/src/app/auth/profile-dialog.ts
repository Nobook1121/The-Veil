namespace AuthModule {
    const USER_THEME_COOKIE = "trpg_user_theme";
    const ADMIN_GRADIENT_COOKIE = "trpg_admin_name_gradient";
    const DEFAULT_ADMIN_NAME_FROM = "#84ff42";
    const DEFAULT_ADMIN_NAME_TO = "#0ebeff";
    const HEX_COLOR_PATTERN = /^#[0-9a-fA-F]{6}$/;
    let initialProfileTheme = "";
    let initialAdminNameFrom = DEFAULT_ADMIN_NAME_FROM;
    let initialAdminNameTo = DEFAULT_ADMIN_NAME_TO;

    export function openUserSettings(): void {
        window.switchMainTab?.("user-settings", { clearNav: true });
        switchProfileSection("profile-account-info", false);
        resetProfileThemeSetting();
    }

    export async function loadUserSettings(): Promise<void> {
        try {
            const response = await TrpgApi.get<ApiResponse<CurrentUser>>("/api/user/profile");
            if (!response.success || !response.data) return;
            const user = response.data;
            const username = document.getElementById("editUsername") as HTMLInputElement | null;
            const email = document.getElementById("editEmail") as HTMLInputElement | null;
            const nickname = document.getElementById("editNickname") as HTMLInputElement | null;
            const avatar = document.getElementById("avatarPreview") as HTMLImageElement | null;
            if (username) username.value = user.username || "";
            if (email) email.value = user.email || "";
            if (nickname) nickname.value = user.nickname || "";
            if (avatar) avatar.src = TrpgAvatar.resolveAvatarSrc(user.username || "", user.avatar);
        } catch (error) {
            console.error("获取用户资料失败:", error);
        }
    }

    export function resetUserSettings(): void {
        clearPasswordFields();
        showMessage("passwordMessage", "");
        showMessage("presenceMessage", "");
        showMessage("settingsMessage", "");
    }

    export function bindProfileNavigation(): void {
        document.querySelectorAll<HTMLButtonElement>(".profile-section-tab[data-profile-target]").forEach((tab) => {
            tab.addEventListener("click", () => switchProfileSection(tab.dataset.profileTarget || "profile-account-info"));
        });
        bindProfileThemeSettings();
    }

    export function bindAvatarPreview(): void {
        const avatarInput = document.getElementById("avatarUpload") as HTMLInputElement | null;
        const avatarPreview = document.getElementById("avatarPreview") as HTMLImageElement | null;
        if (!avatarInput || !avatarPreview) return;
        avatarInput.addEventListener("change", () => {
            const file = avatarInput.files?.[0];
            if (!file) return;
            avatarPreview.src = URL.createObjectURL(file);
        });
    }

    export async function saveUserSettings(): Promise<void> {
        const username = (document.getElementById("editUsername") as HTMLInputElement | null)?.value.trim() || "";
        const email = (document.getElementById("editEmail") as HTMLInputElement | null)?.value.trim() || "";
        const nickname = (document.getElementById("editNickname") as HTMLInputElement | null)?.value.trim() || "";
        const avatarFile = (document.getElementById("avatarUpload") as HTMLInputElement | null)?.files?.[0];
        if (!username || !email) {
            showMessage("settingsMessage", authText("profile.error.username_email_required", "请输入用户名和电子邮件"), true);
            return;
        }
        try {
            const formData = new FormData();
            formData.append("username", username);
            formData.append("email", email);
            formData.append("nickname", nickname);
            if (avatarFile) formData.append("avatar", avatarFile);

            const response = await TrpgApi.post<ApiResponse<CurrentUser>>("/api/auth/update", formData);
            if (!response.success || !response.data) {
                showMessage("settingsMessage", apiMessage(response, authText("profile.error.save_failed", "保存失败")), true);
                return;
            }
            setCurrentUser({ ...(window.currentUser as CurrentUser), ...response.data });
            showMessage("settingsMessage", authText("profile.success.saved", "设置已保存"));
        } catch (error) {
            console.error("更新用户资料失败:", error);
            showMessage("settingsMessage", authText("profile.error.save_failed_retry", "保存失败，请稍后重试"), true);
        }
    }

    export async function changePassword(): Promise<void> {
        const currentPassword = (document.getElementById("passwordCurrentPassword") as HTMLInputElement | null)?.value || "";
        const newPassword = (document.getElementById("passwordNewPassword") as HTMLInputElement | null)?.value || "";
        const confirmPassword = (document.getElementById("passwordConfirmPassword") as HTMLInputElement | null)?.value || "";
        if (!currentPassword || !newPassword || !confirmPassword) {
            showMessage("passwordMessage", authText("profile.error.password_required", "请完整填写密码信息"), true);
            return;
        }
        if (newPassword !== confirmPassword) {
            showMessage("passwordMessage", authText("auth.error.password_mismatch", "两次输入的新密码不一致"), true);
            return;
        }
        try {
            const response = await TrpgApi.post<ApiResponse>("/api/auth/password/change", {
                current_password: currentPassword,
                new_password: newPassword,
                confirm_password: confirmPassword,
            });
            if (!response.success) {
                showMessage("passwordMessage", apiMessage(response, authText("profile.error.password_change_failed", "密码修改失败")), true);
                return;
            }
            clearPasswordFields();
            showMessage("passwordMessage", authText("profile.success.password_changed", "密码已修改"));
        } catch (error) {
            console.error("密码修改失败:", error);
            showMessage("passwordMessage", authText("profile.error.password_change_failed_retry", "密码修改失败，请稍后重试"), true);
        }
    }

    function switchProfileSection(targetId: string, smooth = true): void {
        document.querySelectorAll<HTMLElement>(".profile-panel-section").forEach((section) => {
            section.classList.toggle("active", section.id === targetId);
        });
        document.querySelectorAll<HTMLElement>(".profile-section-tab[data-profile-target]").forEach((tab) => {
            const isActive = tab.dataset.profileTarget === targetId;
            tab.classList.toggle("active", isActive);
            tab.closest("details")?.setAttribute("open", "");
        });
        if (smooth) {
            document.querySelector<HTMLElement>(".profile-settings-content")?.scrollTo({ top: 0, behavior: "smooth" });
        }
    }

    function clearPasswordFields(): void {
        ["passwordCurrentPassword", "passwordNewPassword", "passwordConfirmPassword"].forEach((id) => {
            const input = document.getElementById(id) as HTMLInputElement | null;
            if (input) input.value = "";
        });
    }

    function bindProfileThemeSettings(): void {
        const themeSelect = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        if (!themeSelect || themeSelect.dataset.bound === "true") return;
        themeSelect.dataset.bound = "true";
        themeSelect.addEventListener("change", updateProfilePendingState);
        document.getElementById("profileAdminNameFrom")?.addEventListener("input", updateProfilePendingState);
        document.getElementById("profileAdminNameTo")?.addEventListener("input", updateProfilePendingState);
        document.getElementById("saveProfilePendingChanges")?.addEventListener("click", saveProfilePendingChanges);
        document.getElementById("cancelProfilePendingChanges")?.addEventListener("click", cancelProfilePendingChanges);
        resetProfileThemeSetting();
    }

    function resetProfileThemeSetting(): void {
        initialProfileTheme = getUserThemePreference();
        const themeSelect = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        if (themeSelect) {
            themeSelect.value = initialProfileTheme;
        }
        const gradient = getAdminNameGradientPreference();
        initialAdminNameFrom = gradient.from;
        initialAdminNameTo = gradient.to;
        const fromInput = document.getElementById("profileAdminNameFrom") as HTMLInputElement | null;
        const toInput = document.getElementById("profileAdminNameTo") as HTMLInputElement | null;
        if (fromInput) fromInput.value = initialAdminNameFrom;
        if (toInput) toInput.value = initialAdminNameTo;
        updateProfilePendingState();
    }

    function updateProfilePendingState(): void {
        const themeSelect = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        const pendingBar = document.getElementById("profilePendingSaveBar");
        const count = document.getElementById("profilePendingChangeCount");
        const themeSetting = document.getElementById("profileThemeSetting");
        const gradientSetting = document.getElementById("profileAdminGradientSetting");
        if (!themeSelect || !pendingBar || !count) return;

        const themeDirty = themeSelect.value !== initialProfileTheme;
        const gradientDirty = readColorInput("profileAdminNameFrom", initialAdminNameFrom) !== initialAdminNameFrom
            || readColorInput("profileAdminNameTo", initialAdminNameTo) !== initialAdminNameTo;
        const pendingCount = (themeDirty ? 1 : 0) + (gradientDirty ? 1 : 0);
        pendingBar.hidden = pendingCount === 0;
        count.textContent = String(pendingCount);
        themeSetting?.classList.toggle("profile-setting-dirty", themeDirty);
        gradientSetting?.classList.toggle("profile-setting-dirty", gradientDirty);
    }

    function readColorInput(id: string, fallback: string): string {
        const input = document.getElementById(id) as HTMLInputElement | null;
        const value = input ? input.value.trim().toLowerCase() : "";
        return HEX_COLOR_PATTERN.test(value) ? value : fallback;
    }

    function saveProfilePendingChanges(): void {
        const themeSelect = document.getElementById("profileThemeSelect") as HTMLSelectElement | null;
        if (!themeSelect) return;
        setUserThemePreference(themeSelect.value);
        initialProfileTheme = themeSelect.value;

        const from = readColorInput("profileAdminNameFrom", DEFAULT_ADMIN_NAME_FROM);
        const to = readColorInput("profileAdminNameTo", DEFAULT_ADMIN_NAME_TO);
        setAdminNameGradientPreference(from, to);
        initialAdminNameFrom = from;
        initialAdminNameTo = to;

        updateProfilePendingState();
        window.configManager?.applyTheme();
        window.configManager?.applyAdminNameGradient();
    }

    function cancelProfilePendingChanges(): void {
        resetProfileThemeSetting();
    }

    function getUserThemePreference(): string {
        const prefix = `${USER_THEME_COOKIE}=`;
        const item = document.cookie
            .split(";")
            .map((part) => part.trim())
            .find((part) => part.startsWith(prefix));
        return item ? decodeURIComponent(item.slice(prefix.length)) : "";
    }

    function setUserThemePreference(theme: string): void {
        if (!theme) {
            document.cookie = `${USER_THEME_COOKIE}=; Max-Age=0; Path=/; SameSite=Lax`;
            return;
        }
        const maxAge = 365 * 24 * 60 * 60;
        document.cookie = `${USER_THEME_COOKIE}=${encodeURIComponent(theme)}; Max-Age=${maxAge}; Path=/; SameSite=Lax`;
    }

    function getAdminNameGradientPreference(): { from: string; to: string } {
        const prefix = `${ADMIN_GRADIENT_COOKIE}=`;
        const item = document.cookie
            .split(";")
            .map((part) => part.trim())
            .find((part) => part.startsWith(prefix));
        if (!item) return { from: DEFAULT_ADMIN_NAME_FROM, to: DEFAULT_ADMIN_NAME_TO };
        const [rawFrom = "", rawTo = ""] = decodeURIComponent(item.slice(prefix.length)).split(",");
        return {
            from: HEX_COLOR_PATTERN.test(rawFrom) ? rawFrom.toLowerCase() : DEFAULT_ADMIN_NAME_FROM,
            to: HEX_COLOR_PATTERN.test(rawTo) ? rawTo.toLowerCase() : DEFAULT_ADMIN_NAME_TO,
        };
    }

    function setAdminNameGradientPreference(from: string, to: string): void {
        const maxAge = 365 * 24 * 60 * 60;
        document.cookie = `${ADMIN_GRADIENT_COOKIE}=${encodeURIComponent(`${from},${to}`)}; Max-Age=${maxAge}; Path=/; SameSite=Lax`;
    }

    export async function updatePresence(presence: "online" | "dnd" | "invisible"): Promise<void> {
        try {
            const response = await TrpgApi.put<ApiResponse<CurrentUser>>("/api/user/presence", { presence });
            if (response.success && response.data) {
                setCurrentUser({ ...(window.currentUser as CurrentUser), ...response.data });
                closeUserCard();
                return;
            }
            showMessage("presenceMessage", apiMessage(response, authText("profile.error.presence_update_failed", "在线状态更新失败")), true);
        } catch (error) {
            console.error("在线状态更新失败:", error);
            showMessage("presenceMessage", authText("profile.error.presence_update_failed_retry", "在线状态更新失败，请稍后重试"), true);
        }
    }
}
