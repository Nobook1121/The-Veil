/**
 * 个人主页「我收藏的剧本」：读取本机收藏，拉取剧本基础信息并渲染卡片。
 * 卡片支持直接游玩与取消收藏。
 */
namespace AuthModule {
    function buildProfileFavoriteCard(scenario: Scenario): HTMLElement {
        const card = document.createElement("article");
        card.className = "profile-favorite-card";
        card.dataset.scenarioId = String(scenario.id);
        const author = scenario.author || scenario.creator_username || "未知作者";
        card.innerHTML = `
            <div class="profile-favorite-cover">${scenarioCoverHtml(scenario.title, scenario.cover)}</div>
            <div class="profile-favorite-body">
                <h5 class="profile-favorite-title" data-user-content="true"></h5>
                <p class="profile-favorite-meta" data-user-content="true"></p>
                <div class="profile-favorite-actions">
                    <button type="button" class="btn btn-sm btn-success" data-favorite-play="${scenario.id}">游玩</button>
                    <button type="button" class="btn btn-sm btn-outline-warning" data-favorite-remove="${scenario.id}">取消收藏</button>
                </div>
            </div>`;
        card.querySelector<HTMLElement>(".profile-favorite-title")!.textContent = scenario.title;
        card.querySelector<HTMLElement>(".profile-favorite-meta")!.textContent = `作者：${author} · 推荐人数：${scenario.playerCount}`;
        return card;
    }

    export async function renderProfileFavorites(): Promise<void> {
        const host = document.getElementById("personal-home");
        const list = document.getElementById("profileFavoriteList");
        const empty = document.getElementById("profileFavoriteEmpty");
        if (!host || !list || !empty || !host.classList.contains("active")) return;

        list.innerHTML = "";
        const ids = ScenarioFavorites.list();
        if (!ids.length) {
            empty.hidden = false;
            return;
        }

        try {
            const response = await TrpgApi.get<ApiResponse<Scenario[]>>("/api/scenarios");
            const scenarios = response.success && response.data ? response.data : [];
            ScenarioFavorites.prune(scenarios.map((scenario) => scenario.id));
            const favorites = scenarios.filter((scenario) => ids.includes(scenario.id));
            empty.hidden = favorites.length > 0;
            favorites.forEach((scenario) => list.appendChild(buildProfileFavoriteCard(scenario)));
        } catch (error) {
            console.error("加载收藏剧本失败:", error);
            empty.hidden = false;
        }
    }

    export function bindProfileFavorites(): void {
        document.getElementById("profileFavoritesLink")?.addEventListener("click", () => {
            document.getElementById("profileFavoriteSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
            void renderProfileFavorites();
        });
        document.getElementById("profileFavoritesBrowse")?.addEventListener("click", () => {
            window.switchMainTab?.("scenario");
        });
        document.getElementById("profileFavoriteList")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            const play = target.closest<HTMLButtonElement>("[data-favorite-play]");
            if (play) {
                const id = Number.parseInt(play.dataset.favoritePlay || "", 10);
                if (Number.isFinite(id)) {
                    window.switchMainTab?.("save");
                    void window.openCreateRoomWithScenario?.(id);
                }
                return;
            }
            const remove = target.closest<HTMLButtonElement>("[data-favorite-remove]");
            if (remove) {
                const id = Number.parseInt(remove.dataset.favoriteRemove || "", 10);
                if (Number.isFinite(id)) {
                    ScenarioFavorites.remove(id);
                    void renderProfileFavorites();
                }
            }
        });
        window.addEventListener(ScenarioFavorites.CHANGED_EVENT, () => {
            void renderProfileFavorites();
        });
    }
}
