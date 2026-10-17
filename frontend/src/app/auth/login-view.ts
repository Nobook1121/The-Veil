namespace AuthModule {
    export function showLoginView(): void {
        document.getElementById("login-view")?.classList.add("active");
        document.getElementById("register-view")?.classList.remove("active");
        const title = document.querySelector<HTMLHeadingElement>(".auth-modal-header h2");
        if (title) title.textContent = authText("auth.login.title", "登录");
    }

    export function prefillRememberedUsername(): void {
        const rememberedUsername = TrpgCookies.get("trpg_last_username");
        const loginUsername = document.getElementById("loginUsername") as HTMLInputElement | null;
        if (rememberedUsername && loginUsername) {
            loginUsername.value = rememberedUsername;
        }
    }

    export async function checkAuthStatus(): Promise<boolean> {
        try {
            const response = await TrpgApi.get<ApiResponse<CurrentUser>>("/api/auth/status");
            if (response.success && response.data) {
                setCurrentUser(response.data);
                closeAuthModal();
                maybeShowForcePasswordModal(response.data);
                return true;
            }
        } catch (error) {
            console.debug("未登录或会话已失效", error);
        }
        setCurrentUser(null);
        window.clearCharacterManagement?.();
        showAuthModal();
        return false;
    }

    export async function login(): Promise<void> {
        const identifier = (document.getElementById("loginUsername") as HTMLInputElement | null)?.value.trim() || "";
        const password = (document.getElementById("loginPassword") as HTMLInputElement | null)?.value || "";
        const rememberMe = (document.getElementById("rememberMe") as HTMLInputElement | null)?.checked || false;
        const stayLoggedIn = (document.getElementById("stayLoggedIn") as HTMLInputElement | null)?.checked || false;
        const autoLogin = (document.getElementById("autoLogin") as HTMLInputElement | null)?.checked || false;
        if (!identifier || !password) {
            showMessage("loginMessage", authText("auth.error.missing_credentials", "请输入用户名或邮箱和密码"), true);
            return;
        }

        let response: ApiResponse<CurrentUser>;
        try {
            response = await TrpgApi.post<ApiResponse<CurrentUser>>("/api/auth/login", {
                identifier,
                password,
                remember_me: rememberMe,
                stay_logged_in: stayLoggedIn,
                auto_login: autoLogin,
            });
        } catch (error) {
            console.error("登录失败:", error);
            showMessage("loginMessage", authText("auth.error.login_failed_retry", "登录失败，请稍后重试"), true);
            return;
        }

        if (!response.success || !response.data) {
            showMessage("loginMessage", localizedAuthMessage(response, authText("auth.error.login_failed", "登录失败")), true);
            return;
        }

        setCurrentUser(response.data);
        closeAuthModal();
        maybeShowForcePasswordModal(response.data);
        TrpgCookies.set("trpg_last_username", response.data.username);
        await restorePostLoginState();
    }

    export function maybeShowForcePasswordModal(user: CurrentUser | null): void {
        const modal = document.getElementById("force-password-modal");
        if (!modal) return;
        if (!user?.must_change_password) {
            modal.style.display = "none";
            return;
        }
        modal.style.display = "flex";
        const message = document.getElementById("forcePasswordMessage");
        if (message) message.textContent = "";
        (document.getElementById("forceNewPassword") as HTMLInputElement | null)?.focus();
    }

    export async function submitForcedPasswordChange(): Promise<void> {
        const currentPassword = (document.getElementById("forceCurrentPassword") as HTMLInputElement | null)?.value || "";
        const newPassword = (document.getElementById("forceNewPassword") as HTMLInputElement | null)?.value || "";
        const confirmPassword = (document.getElementById("forceConfirmPassword") as HTMLInputElement | null)?.value || "";
        if (!currentPassword || !newPassword || !confirmPassword) {
            showMessage("forcePasswordMessage", authText("profile.error.password_required", "请完整填写密码信息"), true);
            return;
        }
        if (newPassword !== confirmPassword) {
            showMessage("forcePasswordMessage", authText("auth.error.password_mismatch", "两次输入的新密码不一致"), true);
            return;
        }
        try {
            const response = await TrpgApi.post<ApiResponse>("/api/auth/password/change", {
                current_password: currentPassword,
                new_password: newPassword,
                confirm_password: confirmPassword,
            });
            if (!response.success) {
                showMessage("forcePasswordMessage", localizedAuthMessage(response, authText("profile.error.password_change_failed", "密码修改失败")), true);
                return;
            }
            const updated = window.currentUser ? { ...window.currentUser, must_change_password: false } : null;
            setCurrentUser(updated);
            maybeShowForcePasswordModal(updated);
            ["forceCurrentPassword", "forceNewPassword", "forceConfirmPassword"].forEach((id) => {
                const input = document.getElementById(id) as HTMLInputElement | null;
                if (input) input.value = "";
            });
            showNotification(authText("password.force.success", "密码已修改，可继续使用"), "success");
        } catch (error) {
            console.error("强制修改密码失败:", error);
            showMessage("forcePasswordMessage", authText("profile.error.password_change_failed_retry", "密码修改失败，请稍后重试"), true);
        }
    }

    async function restorePostLoginState(): Promise<void> {
        try {
            await window.reloadRoleConfigs?.();
            window.reconnectSocket?.();
            window.clearCurrentRoom?.();
            window.clearChatMessages?.();
            await window.reloadCharacterManagement?.();
            await window.autoLoadLastRoom?.();
        } catch (error) {
            console.warn("登录后的页面状态恢复失败:", error);
        }
    }
}
