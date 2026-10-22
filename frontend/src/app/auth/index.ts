namespace AuthModule {
    export async function initAuth(): Promise<boolean> {
        bindAuthEvents();
        prefillRememberedUsername();
        bindFloatingFields();
        return checkAuthStatus();
    }

    function bindAuthEvents(): void {
        document.getElementById("show-register-view")?.addEventListener("click", showRegisterView);
        document.getElementById("show-login-view")?.addEventListener("click", showLoginView);
        document.getElementById("loginButton")?.addEventListener("click", login);
        document.getElementById("registerButton")?.addEventListener("click", register);
        document.getElementById("close-auth-modal")?.addEventListener("click", closeAuthModal);
        document.getElementById("logoutButton")?.addEventListener("click", logout);
        document.getElementById("switchAccountButton")?.addEventListener("click", switchAccount);
        document.getElementById("exitImpersonationButton")?.addEventListener("click", stopImpersonation);
        document.getElementById("open-personal-home")?.addEventListener("click", () => {
            closeUserCard();
            window.switchMainTab?.("personal-home", { clearNav: true });
        });
        document.getElementById("open-user-settings")?.addEventListener("click", openUserSettings);
        document.getElementById("personalHomeSettingsLink")?.addEventListener("click", openUserSettings);
        document.getElementById("saveUserSettings")?.addEventListener("click", saveUserSettings);
        document.getElementById("changePasswordButton")?.addEventListener("click", changePassword);
        document.getElementById("forcePasswordSubmit")?.addEventListener("click", submitForcedPasswordChange);
        document.querySelectorAll<HTMLButtonElement>(".profile-quick-link[data-target-tab]").forEach((button) => {
            button.addEventListener("click", () => {
                window.switchMainTab?.(button.dataset.targetTab || "", { clearNav: false });
            });
        });
        document.querySelectorAll<HTMLButtonElement>("[data-guest-login]").forEach((button) => {
            button.addEventListener("click", () => showAuthModal());
        });
        document.getElementById("userInfo")?.addEventListener("click", toggleUserCard);
        closeUserCardOnOutsideClick();
        bindProfileNavigation();
        bindAvatarPreview();
        document.querySelectorAll<HTMLButtonElement>("[data-presence]").forEach((button) => {
            button.addEventListener("click", () => updatePresence(button.dataset.presence as "online" | "dnd" | "invisible"));
        });
    }
}

window.initAuth = AuthModule.initAuth;
window.setCurrentEditingScenarioId = AuthModule.setCurrentEditingScenarioId;
window.loadUserSettings = AuthModule.loadUserSettings;
