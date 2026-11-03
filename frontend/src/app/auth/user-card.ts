namespace AuthModule {
    /*
     * 用户卡片原来嵌在 #mainContent 里。赛博档案主题给 .col-10 加了
     * position: relative + z-index: 1，形成层叠上下文，卡片（position: fixed）
     * 就被困在其中，被同级、z-index: 1000 的左侧栏整块盖住；深渊主题没有这层
     * 上下文，卡片 9000 的 z-index 直接生效，所以同一个控件表现正常。
     * 把它挂到 body 上——与 React 的 createPortal 等价——即可脱离主内容区的
     * 层叠上下文，且不影响其它主题的层级关系。
     */
    const initialUserCard = document.getElementById("user-card-popover");
    if (initialUserCard && initialUserCard.parentElement !== document.body) {
        document.body.appendChild(initialUserCard);
    }

    let memberCardAnchor: HTMLElement | null = null;
    let memberCardDismissBound = false;

    export function toggleUserCard(): void {
        if (!window.currentUser) {
            showLoginView();
            showAuthModal();
            return;
        }
        const popover = document.getElementById("user-card-popover");
        if (!popover) return;
        popover.classList.toggle("open");
        popover.setAttribute("aria-hidden", popover.classList.contains("open") ? "false" : "true");
    }

    export function closeUserCard(): void {
        const popover = document.getElementById("user-card-popover");
        popover?.classList.remove("open");
        popover?.setAttribute("aria-hidden", "true");
    }

    export function closeUserCardOnOutsideClick(): void {
        document.addEventListener("click", (event) => {
            const target = event.target as Node | null;
            const popover = document.getElementById("user-card-popover");
            const trigger = document.getElementById("userInfo");
            if (!target || !popover?.classList.contains("open")) return;
            if (popover.contains(target) || trigger?.contains(target)) return;
            closeUserCard();
        });
    }

    export async function logout(): Promise<void> {
        try {
            await TrpgApi.post<ApiResponse>("/api/auth/logout");
        } catch (error) {
            console.error("登出失败:", error);
        }
        window.disconnectSocket?.();
        window.clearCurrentRoom?.();
        window.clearChatMessages?.();
        window.clearCharacterManagement?.();
        setCurrentUser(null);
        closeUserCard();
        resetUserSettings();
        showLoginView();
        showAuthModal();
    }

    export async function switchAccount(): Promise<void> {
        await logout();
    }

    export async function stopImpersonation(): Promise<void> {
        try {
            await TrpgApi.post<ApiResponse>("/api/auth/impersonation/stop");
            window.location.reload();
        } catch (error) {
            console.error("退出模拟失败:", error);
            showNotification("退出模拟失败", "error");
        }
    }

    function memberCardText(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    /** 判定成员在房间内的权限等级，供名字配色与徽标复用 */
    export function memberRoleKind(room: Room | null, member: RoomMember): "owner" | "admin" | "member" {
        if (["ADMIN", "OWNER"].includes(member.role || "")) return "admin";
        if (!room) return "member";
        if (String(member.user_id) === String(room.creator_id) || member.room_role === "owner") return "owner";
        if (member.room_role === "admin") return "admin";
        return "member";
    }

    function memberRoleLabel(room: Room | null, member: RoomMember): string {
        if (member.permission_label) return member.permission_label;
        const kind = memberRoleKind(room, member);
        if (kind === "owner") return "房主";
        if (kind === "admin") return "管理员";
        return "成员";
    }

    /**
     * 把资料卡摆到锚点（头像/成员行）旁边：优先右侧，放不下改左侧，再夹紧到视口内。
     * 卡片关闭时仍保留布局尺寸（visibility: hidden），因此可在显示前测量。
     */
    function positionMemberProfileCard(card: HTMLElement, anchor: HTMLElement): void {
        const margin = 12;
        const gap = 10;
        const cardWidth = card.offsetWidth;
        const cardHeight = card.offsetHeight;
        const anchorRect = anchor.getBoundingClientRect();

        let left = anchorRect.right + gap;
        if (left + cardWidth > window.innerWidth - margin) {
            left = anchorRect.left - gap - cardWidth;
        }
        left = Math.min(Math.max(left, margin), Math.max(margin, window.innerWidth - margin - cardWidth));

        let top = anchorRect.top;
        top = Math.min(Math.max(top, margin), Math.max(margin, window.innerHeight - margin - cardHeight));

        card.style.left = `${Math.round(left)}px`;
        card.style.top = `${Math.round(top)}px`;
    }

    export function openMemberProfileCard(room: Room | null, member: RoomMember, anchor: HTMLElement): void {
        const card = document.getElementById("memberProfileCard");
        if (!card) return;
        if (memberCardAnchor === anchor && card.classList.contains("open")) {
            closeMemberProfileCard();
            return;
        }
        memberCardAnchor = anchor;

        const avatar = document.getElementById("memberProfileAvatar") as HTMLImageElement | null;
        if (avatar) {
            avatar.src = TrpgAvatar.resolveAvatarSrc(member.username || "", member.avatar);
            avatar.alt = member.username || "";
        }
        const nameText = document.getElementById("memberProfileName");
        if (nameText) nameText.textContent = member.username || "-";

        const kind = memberRoleKind(room, member);
        const nameRow = document.getElementById("memberProfileNameRow");
        if (nameRow) {
            nameRow.className = `home-room-member-name member-profile-name-row${kind === "member" ? "" : ` is-${kind}`}`;
        }
        const roleBadge = document.getElementById("memberProfileRole");
        if (roleBadge) {
            roleBadge.className = `home-room-member-role is-${kind}`;
            roleBadge.textContent = memberRoleLabel(room, member);
        }

        const isOnline = member.is_online === true;
        const presence = document.getElementById("memberProfilePresence");
        if (presence) presence.className = `member-profile-presence ${isOnline ? "is-online" : "is-offline"}`;
        const status = document.getElementById("memberProfileStatus");
        if (status) status.textContent = memberCardText(isOnline ? "home.members.online" : "home.members.offline", isOnline ? "在线" : "离线");

        const character = document.getElementById("memberProfileCharacter");
        if (character) character.textContent = member.character_card?.name || memberCardText("home.members.unbound", "未绑定角色卡");

        card.classList.add("open");
        card.setAttribute("aria-hidden", "false");
        positionMemberProfileCard(card, anchor);
        bindMemberCardDismiss();
    }

    export function closeMemberProfileCard(): void {
        const card = document.getElementById("memberProfileCard");
        if (!card) return;
        card.classList.remove("open");
        card.setAttribute("aria-hidden", "true");
        memberCardAnchor = null;
    }

    function bindMemberCardDismiss(): void {
        if (memberCardDismissBound) return;
        memberCardDismissBound = true;

        document.addEventListener("click", (event) => {
            const card = document.getElementById("memberProfileCard");
            if (!card?.classList.contains("open")) return;
            const target = event.target as Node | null;
            if (!target) return;
            if (card.contains(target) || memberCardAnchor?.contains(target)) return;
            closeMemberProfileCard();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeMemberProfileCard();
        });

        const reposition = (): void => {
            const card = document.getElementById("memberProfileCard");
            if (card?.classList.contains("open") && memberCardAnchor?.isConnected) {
                positionMemberProfileCard(card, memberCardAnchor);
            }
        };
        window.addEventListener("resize", reposition);
        // 捕获阶段监听，内部滚动容器（聊天历史/成员列表）滚动时也能重新定位
        window.addEventListener("scroll", reposition, true);
    }
}
