/**
 * 网站内置背景音乐播放器。
 *
 * 个人 BGM（多首曲目 + 播放模式）与播放优先级只保存在当前浏览器（localStorage）；
 * 房间 BGM 由房间规则（house_rules.bgm_urls / bgm_mode）下发，只有在玩家加入房间
 * （window.currentRoom 有值）时才会播放。播放优先级默认「房间优先」。
 *
 * 播放模式：
 * - list   ：列表循环，播完最后一首回到第一首
 * - single ：单曲循环
 * - shuffle：随机播放（尽量不连续重复同一首）
 */
namespace TrpgBgmModule {
    const PERSONAL_URLS_KEY = "trpg_bgm_personal_urls";
    const LEGACY_PERSONAL_URL_KEY = "trpg_bgm_personal_url";
    const PERSONAL_MODE_KEY = "trpg_bgm_personal_mode";
    const PRIORITY_KEY = "trpg_bgm_priority";
    const PERSONAL_EXPANDED_KEY = "trpg_bgm_personal_expanded";
    const DEFAULT_PRIORITY: BgmPlaybackPriority = "room";
    const DEFAULT_MODE: BgmPlaybackMode = "list";
    const MAX_TRACKS = 20;
    const AUDIO_EXTENSION_PATTERN = /\.(mp3|m4a|aac|ogg|oga|wav|flac|webm|opus)$/i;
    // 站内音乐库地址（内置音乐/管理员上传）无需完整域名，直接由服务端本地提供。
    const LOCAL_BGM_PREFIX = "/assets/bgm/";

    let audio: HTMLAudioElement | null = null;
    let initialized = false;
    let currentUrl = "";
    let currentSource: BgmTrackSource | null = null;
    let activePlaylist: string[] = [];
    let activeMode: BgmPlaybackMode = DEFAULT_MODE;
    let activeUrlsSignature = "";
    let trackIndex = 0;
    let roomContext: { id: string; urls: string[]; mode: BgmPlaybackMode } | null = null;
    let pausedByUser = false;
    let awaitingGesture = false;
    let gestureArmed = false;
    let cachedTracks: BgmTrack[] = [];

    function isSupportedUrl(value: string): boolean {
        const url = value.trim();
        if (!url || url.length > 2048) return false;
        if (url.toLowerCase().startsWith(LOCAL_BGM_PREFIX)) {
            return !url.includes("..") && !url.includes("\\");
        }
        const lowered = url.toLowerCase();
        return lowered.startsWith("http://") || lowered.startsWith("https://");
    }

    function normalizeUrls(values: unknown[]): string[] {
        const urls: string[] = [];
        for (const item of values) {
            if (typeof item !== "string") continue;
            const url = item.trim();
            if (!isSupportedUrl(url) || urls.includes(url)) continue;
            urls.push(url);
            if (urls.length >= MAX_TRACKS) break;
        }
        return urls;
    }

    function isPlaybackMode(value: unknown): value is BgmPlaybackMode {
        return value === "list" || value === "single" || value === "shuffle";
    }

    function readStorage(key: string): string {
        try {
            return window.localStorage.getItem(key) || "";
        } catch {
            return "";
        }
    }

    function writeStorage(key: string, value: string): void {
        try {
            if (value) window.localStorage.setItem(key, value);
            else window.localStorage.removeItem(key);
        } catch (error) {
            console.warn("保存背景音乐设置失败", error);
        }
    }

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function readPersonalUrls(): string[] {
        const raw = readStorage(PERSONAL_URLS_KEY);
        if (raw) {
            try {
                const parsed: unknown = JSON.parse(raw);
                if (Array.isArray(parsed)) return normalizeUrls(parsed);
            } catch {
                /* 解析失败时回退到旧版单曲设置 */
            }
        }
        // 旧版只保存了一首 personal_url，升级后自动迁移。
        const legacy = readStorage(LEGACY_PERSONAL_URL_KEY).trim();
        return legacy && isSupportedUrl(legacy) ? [legacy] : [];
    }

    export function getPersonalSettings(): BgmPersonalSettings {
        const storedMode = readStorage(PERSONAL_MODE_KEY);
        const priority = readStorage(PRIORITY_KEY) === "personal" ? "personal" : DEFAULT_PRIORITY;
        return {
            urls: readPersonalUrls(),
            mode: isPlaybackMode(storedMode) ? storedMode : DEFAULT_MODE,
            priority,
        };
    }

    export function savePersonalSettings(settings: Partial<BgmPersonalSettings>): BgmPersonalSettings {
        const current = getPersonalSettings();
        const urls = settings.urls === undefined ? current.urls : normalizeUrls(settings.urls);
        const mode = isPlaybackMode(settings.mode) ? settings.mode : current.mode;
        const priority = settings.priority === "personal" || settings.priority === "room" ? settings.priority : current.priority;
        writeStorage(PERSONAL_URLS_KEY, JSON.stringify(urls));
        writeStorage(PERSONAL_MODE_KEY, mode);
        writeStorage(PRIORITY_KEY, priority);
        // 迁移完成后清理旧键，避免以后又读到过期单曲。
        writeStorage(LEGACY_PERSONAL_URL_KEY, "");
        return { urls, mode, priority };
    }

    function deriveTitle(url: string): string {
        const known = cachedTracks.find((track) => track.url === url);
        if (known) return known.name;
        try {
            const parsed = new URL(url, window.location.origin);
            const segments = parsed.pathname.split("/").filter(Boolean);
            const file = segments.pop() || parsed.hostname;
            const decoded = decodeURIComponent(file);
            const title = decoded
                .replace(AUDIO_EXTENSION_PATTERN, "")
                // 管理员上传的存储名形如 `abcd1234efgh__夜航.wav`，展示时去掉前缀。
                .replace(/^[0-9a-f]{12}__/i, "")
                .replace(/^\d+[-_]/, "")
                .trim();
            return (title || decoded || parsed.hostname).slice(0, 60);
        } catch {
            return url;
        }
    }

    function updateToggleButton(): void {
        const button = document.getElementById("bgmPlayerToggle") as HTMLButtonElement | null;
        const icon = button?.querySelector<HTMLElement>("i") || null;
        const paused = !audio || audio.paused;
        const label = paused ? t("bgm.player.play", "播放") : t("bgm.player.pause", "暂停");
        if (button) {
            button.title = label;
            button.setAttribute("aria-label", label);
        }
        if (icon) icon.className = paused ? "fa fa-play" : "fa fa-pause";
        document.getElementById("bgmPlayer")?.classList.toggle("is-paused", paused);
    }

    function setText(id: string, value: string): void {
        const element = document.getElementById(id);
        if (element) element.textContent = value;
    }

    function updatePlayerLabels(): void {
        const count = activePlaylist.length;
        const sourceLabel = currentSource === "room"
            ? t("bgm.source.room", "房间 BGM")
            : t("bgm.source.personal", "个人 BGM");
        setText("bgmPlayerSource", count > 1 ? `${sourceLabel} · ${trackIndex + 1}/${count}` : sourceLabel);
        setText("bgmPlayerTitle", currentUrl ? deriveTitle(currentUrl) : "");
    }

    function markLoadError(): void {
        setText("bgmPlayerTitle", t("bgm.player.error", "音乐加载失败，请检查外链地址"));
    }

    function armGestureResume(): void {
        if (gestureArmed) return;
        gestureArmed = true;
        const resume = (): void => {
            gestureArmed = false;
            if (awaitingGesture && !pausedByUser) {
                awaitingGesture = false;
                attemptPlay();
            }
        };
        document.addEventListener("pointerdown", resume, { once: true, capture: true });
    }

    function attemptPlay(): void {
        if (!audio || !currentUrl || pausedByUser) return;
        const result = audio.play();
        if (result && typeof result.then === "function") {
            result
                .then(() => {
                    awaitingGesture = false;
                    updateToggleButton();
                })
                .catch((error: unknown) => {
                    if ((error as DOMException)?.name === "NotAllowedError") {
                        awaitingGesture = true;
                        armGestureResume();
                    } else {
                        markLoadError();
                    }
                    updateToggleButton();
                });
        }
    }

    function stopAudio(): void {
        if (!audio) return;
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
        currentUrl = "";
        currentSource = null;
        activePlaylist = [];
        activeUrlsSignature = "";
        pausedByUser = false;
        awaitingGesture = false;
        updateToggleButton();
    }

    function loadTrackAt(index: number, autoPlay: boolean): void {
        if (!audio || !activePlaylist.length) return;
        const count = activePlaylist.length;
        trackIndex = ((index % count) + count) % count;
        currentUrl = activePlaylist[trackIndex] || "";
        if (autoPlay) {
            pausedByUser = false;
            awaitingGesture = false;
        }
        audio.src = currentUrl;
        audio.load();
        updatePlayerLabels();
        updateToggleButton();
        if (autoPlay) attemptPlay();
    }

    /** 按「来源 + 曲目列表」应用播放列表；同一列表只更新模式，不打断当前播放。 */
    function applyPlaylist(source: BgmTrackSource | null, urls: string[], mode: BgmPlaybackMode): void {
        const widget = document.getElementById("bgmPlayer") as HTMLElement | null;
        if (!widget || !audio) return;
        if (!source || urls.length === 0) {
            stopAudio();
            widget.hidden = true;
            return;
        }

        widget.hidden = false;
        activeMode = mode;
        const signature = `${source}|${urls.join("\n")}`;
        if (signature === activeUrlsSignature) {
            currentSource = source;
            updatePlayerLabels();
            return;
        }

        activeUrlsSignature = signature;
        activePlaylist = urls.slice();
        currentSource = source;
        loadTrackAt(0, true);
    }

    function handleEnded(): void {
        if (!audio || !activePlaylist.length) return;
        if (activeMode === "single") {
            audio.currentTime = 0;
            attemptPlay();
            return;
        }
        let next = trackIndex + 1;
        if (activeMode === "shuffle" && activePlaylist.length > 1) {
            next = Math.floor(Math.random() * activePlaylist.length);
            if (next === trackIndex) next = (next + 1) % activePlaylist.length;
        }
        loadTrackAt(next, true);
    }

    export function refresh(): void {
        if (!audio) return;
        const personal = getPersonalSettings();
        const roomUrls = roomContext?.urls || [];
        const roomMode = roomContext?.mode || DEFAULT_MODE;
        const roomFirst = personal.priority === "room";
        let source: BgmTrackSource | null = null;
        let urls: string[] = [];
        if (roomFirst) {
            if (roomUrls.length) {
                source = "room";
                urls = roomUrls;
            } else if (personal.urls.length) {
                source = "personal";
                urls = personal.urls;
            }
        } else {
            if (personal.urls.length) {
                source = "personal";
                urls = personal.urls;
            } else if (roomUrls.length) {
                source = "room";
                urls = roomUrls;
            }
        }
        applyPlaylist(source, urls, source === "room" ? roomMode : personal.mode);
    }

    /** 玩家进入/离开房间时同步房间 BGM 上下文；只有 currentRoom 有值时才会取房间 BGM。 */
    export function setRoom(room: Room | null): void {
        const rawUrls = room?.house_rules?.bgm_urls;
        const urls = Array.isArray(rawUrls) ? normalizeUrls(rawUrls) : [];
        const mode = isPlaybackMode(room?.house_rules?.bgm_mode) ? room.house_rules!.bgm_mode : DEFAULT_MODE;
        roomContext = room ? { id: String(room.id || ""), urls, mode } : null;
        refresh();
    }

    /** 服务端音乐库（内置 + 管理员上传）；失败时返回空列表，不影响外链设置。 */
    export async function fetchTracks(): Promise<BgmTrack[]> {
        try {
            const response = await TrpgApi.get<ApiResponse<BgmTracksPayload>>("/api/bgm/tracks");
            cachedTracks = response?.data?.tracks || [];
        } catch (error) {
            console.warn("读取音乐库失败", error);
            cachedTracks = [];
        }
        return cachedTracks;
    }

    // ------------------------------------------------------------------
    // 播放列表面板（个人主页与房间管理页共用）
    // ------------------------------------------------------------------

    interface PlaylistEditorState {
        key: string;
        listId: string;
        selectId: string;
        customId: string;
        addId: string;
        modeId: string;
        messageId: string;
        urls: string[];
        mode: BgmPlaybackMode;
        editable: boolean;
        onChange: (urls: string[], mode: BgmPlaybackMode) => void | Promise<void>;
    }

    const playlistEditors = new Map<string, PlaylistEditorState>();

    function setEditorMessage(state: PlaylistEditorState, text: string, isError: boolean): void {
        const element = document.getElementById(state.messageId);
        if (!element) return;
        element.textContent = text;
        element.classList.toggle("text-danger", isError);
    }

    function renderPlaylistRows(state: PlaylistEditorState): void {
        const list = document.getElementById(state.listId);
        if (!list) return;
        list.textContent = "";
        if (!state.urls.length) {
            const empty = document.createElement("p");
            empty.className = "profile-favorite-empty";
            empty.textContent = state.editable
                ? t("bgm.panel.empty", "还没有曲目，先添加一首吧。")
                : t("bgm.panel.empty_readonly", "房间还没有设置背景音乐。");
            list.appendChild(empty);
            return;
        }
        state.urls.forEach((url, index) => {
            const row = document.createElement("div");
            row.className = "bgm-track-row";
            const order = document.createElement("span");
            order.className = "bgm-track-order";
            order.textContent = String(index + 1);
            const name = document.createElement("span");
            name.className = "bgm-track-name";
            name.textContent = deriveTitle(url);
            row.append(order, name);
            if (state.editable) {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "btn btn-outline-secondary btn-sm bgm-track-remove";
                remove.textContent = t("common.delete", "删除");
                remove.addEventListener("click", () => {
                    const next = state.urls.filter((_item, position) => position !== index);
                    void commitPlaylist(state, next, state.mode);
                });
                row.appendChild(remove);
            }
            list.appendChild(row);
        });
    }

    async function commitPlaylist(state: PlaylistEditorState, urls: string[], mode: BgmPlaybackMode): Promise<void> {
        state.urls = urls;
        state.mode = mode;
        renderPlaylistRows(state);
        const modeSelect = document.getElementById(state.modeId) as HTMLSelectElement | null;
        if (modeSelect) modeSelect.value = mode;
        try {
            await state.onChange(urls.slice(), mode);
        } catch (error) {
            console.warn("保存背景音乐失败", error);
            setEditorMessage(state, t("bgm.panel.save_failed", "保存失败，请稍后重试"), true);
        }
    }

    async function populateLibrarySelect(state: PlaylistEditorState): Promise<void> {
        const select = document.getElementById(state.selectId) as HTMLSelectElement | null;
        if (!select) return;
        const tracks = await fetchTracks();
        select.textContent = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = tracks.length
            ? t("bgm.panel.pick_library", "从音乐库选择…")
            : t("bgm.panel.library_empty", "音乐库暂无曲目");
        select.appendChild(placeholder);
        tracks.forEach((track) => {
            const option = document.createElement("option");
            option.value = track.url;
            option.textContent = track.builtin
                ? `${track.name}${t("room.house_rules.bgm_builtin_suffix", "（内置）")}`
                : track.name;
            select.appendChild(option);
        });
    }

    function bindPlaylistEditor(state: PlaylistEditorState): void {
        const addButton = document.getElementById(state.addId) as HTMLButtonElement | null;
        if (addButton && addButton.dataset.bgmBound !== "true") {
            addButton.dataset.bgmBound = "true";
            addButton.addEventListener("click", () => void handleAddTrack(state));
        }
        const modeSelect = document.getElementById(state.modeId) as HTMLSelectElement | null;
        if (modeSelect && modeSelect.dataset.bgmBound !== "true") {
            modeSelect.dataset.bgmBound = "true";
            modeSelect.addEventListener("change", () => {
                const mode = isPlaybackMode(modeSelect.value) ? modeSelect.value : DEFAULT_MODE;
                void commitPlaylist(state, state.urls.slice(), mode);
            });
        }
    }

    async function handleAddTrack(state: PlaylistEditorState): Promise<void> {
        const select = document.getElementById(state.selectId) as HTMLSelectElement | null;
        const input = document.getElementById(state.customId) as HTMLInputElement | null;
        const custom = (input?.value || "").trim();
        const url = custom || (select?.value || "").trim();
        if (!url) {
            setEditorMessage(state, t("bgm.panel.pick_first", "请先选择一首音乐或填写外链地址"), true);
            return;
        }
        if (!isSupportedUrl(url)) {
            setEditorMessage(state, t("bgm.panel.invalid_url", "请输入音乐库曲目或 http(s) 外链地址"), true);
            return;
        }
        if (state.urls.includes(url)) {
            setEditorMessage(state, t("bgm.panel.duplicate", "这首音乐已经在列表里了"), true);
            return;
        }
        if (state.urls.length >= MAX_TRACKS) {
            setEditorMessage(
                state,
                t("bgm.panel.full", "最多只能添加 {count} 首音乐").replace("{count}", String(MAX_TRACKS)),
                true,
            );
            return;
        }
        if (input) input.value = "";
        if (select) select.value = "";
        await commitPlaylist(state, state.urls.concat(url), state.mode);
        setEditorMessage(state, "", false);
    }

    async function renderPlaylistEditor(config: Omit<PlaylistEditorState, "key">): Promise<void> {
        const key = config.listId;
        const existing = playlistEditors.get(key);
        const state: PlaylistEditorState = existing || { ...config, key };
        Object.assign(state, config);
        playlistEditors.set(key, state);
        bindPlaylistEditor(state);
        const modeSelect = document.getElementById(state.modeId) as HTMLSelectElement | null;
        if (modeSelect) {
            modeSelect.value = state.mode;
            modeSelect.disabled = !state.editable;
        }
        const addButton = document.getElementById(state.addId) as HTMLButtonElement | null;
        if (addButton) addButton.disabled = !state.editable;
        const customInput = document.getElementById(state.customId) as HTMLInputElement | null;
        if (customInput) customInput.disabled = !state.editable;
        renderPlaylistRows(state);
        await populateLibrarySelect(state);
    }

    /** 个人主页「我的背景音乐」面板。 */
    export async function renderPersonalSettings(): Promise<void> {
        const settings = getPersonalSettings();
        const prioritySelect = document.getElementById("personalBgmPriority") as HTMLSelectElement | null;
        if (prioritySelect) prioritySelect.value = settings.priority;
        await renderPlaylistEditor({
            listId: "personalBgmList",
            selectId: "personalBgmLibrarySelect",
            customId: "personalBgmCustomUrl",
            addId: "personalBgmAdd",
            modeId: "personalBgmMode",
            messageId: "personalBgmMessage",
            urls: settings.urls,
            mode: settings.mode,
            editable: true,
            onChange: (urls, mode) => {
                savePersonalSettings({ urls, mode });
                refresh();
                setPersonalMessage(t("bgm.panel.saved", "已保存，正在播放最新列表"), false);
            },
        });
        setPersonalMessage("", false);
    }

    function setPersonalMessage(text: string, isError: boolean): void {
        const element = document.getElementById("personalBgmMessage");
        if (!element) return;
        element.textContent = text;
        element.classList.toggle("text-danger", isError);
    }

    /** 房间管理页「房间背景音乐」面板：仅房主/房间管理员可编辑。 */
    export async function renderRoomPlaylist(room: Room, options: BgmRoomPlaylistOptions = {}): Promise<void> {
        const card = document.getElementById("roomBgmPanelCard") as HTMLElement | null;
        if (!card) return;
        const editable = options.editable === true;
        const rules = room.house_rules;
        const urls = Array.isArray(rules?.bgm_urls) ? normalizeUrls(rules.bgm_urls) : [];
        const mode = isPlaybackMode(rules?.bgm_mode) ? rules.bgm_mode : DEFAULT_MODE;
        await renderPlaylistEditor({
            listId: "roomBgmList",
            selectId: "roomBgmLibrarySelect",
            customId: "roomBgmCustomUrl",
            addId: "roomBgmAdd",
            modeId: "roomBgmMode",
            messageId: "roomBgmMessage",
            urls,
            mode,
            editable,
            onChange: async (nextUrls, nextMode) => {
                const current = window.currentRoom;
                if (!current?.id) return;
                const response = await TrpgApi.put<ApiResponse<{ house_rules: RoomHouseRules }>>(
                    `/api/rooms/${encodeURIComponent(current.id)}/house-rules`,
                    { house_rules: { bgm_urls: nextUrls, bgm_mode: nextMode } },
                );
                if (!response?.success || !response.data?.house_rules) {
                    const state = playlistEditors.get("roomBgmList");
                    if (state) {
                        setEditorMessage(
                            state,
                            response?.message || response?.error || t("bgm.panel.save_failed", "保存失败，请稍后重试"),
                            true,
                        );
                    }
                    return;
                }
                current.house_rules = response.data.house_rules;
                setRoom(current);
                const state = playlistEditors.get("roomBgmList");
                if (state) setEditorMessage(state, t("bgm.panel.saved_room", "房间背景音乐已保存"), false);
            },
        });
    }

    // ------------------------------------------------------------------
    // 管理员音乐库（设置页）
    // ------------------------------------------------------------------

    function createLibraryRow(track: BgmTrack): HTMLElement {
        const row = document.createElement("div");
        row.className = "bgm-library-row";
        const meta = document.createElement("div");
        meta.className = "bgm-library-meta";
        const name = document.createElement("span");
        name.className = "bgm-library-name";
        name.textContent = track.name;
        const size = document.createElement("span");
        size.className = "bgm-library-size";
        size.textContent = `${track.builtin ? t("profile.bgm.library_builtin", "内置") : t("profile.bgm.library_uploaded", "已上传")} · ${(track.size / 1024 / 1024).toFixed(1)} MB`;
        meta.append(name, size);
        row.appendChild(meta);
        if (!track.builtin) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn-outline-secondary btn-sm";
            remove.textContent = t("common.delete", "删除");
            remove.addEventListener("click", () => void removeTrack(track));
            row.appendChild(remove);
        }
        return row;
    }

    export async function loadLibraryPanel(): Promise<void> {
        const list = document.getElementById("bgmLibraryList");
        if (!list) return;
        const message = document.getElementById("bgmLibraryMessage");
        if (message) message.textContent = "";
        const tracks = await fetchTracks();
        list.textContent = "";
        if (!tracks.length) {
            const empty = document.createElement("p");
            empty.className = "profile-favorite-empty";
            empty.textContent = t("profile.bgm.library_empty", "音乐库为空，先上传一首音乐吧。");
            list.appendChild(empty);
            return;
        }
        tracks.forEach((track) => list.appendChild(createLibraryRow(track)));
    }

    function setLibraryMessage(text: string, isError: boolean): void {
        const element = document.getElementById("bgmLibraryMessage");
        if (!element) return;
        element.textContent = text;
        element.classList.toggle("text-danger", isError);
    }

    async function uploadTrack(): Promise<void> {
        const input = document.getElementById("bgmUploadInput") as HTMLInputElement | null;
        const file = input?.files?.[0];
        if (!file) {
            setLibraryMessage(t("profile.bgm.library_select_file", "请先选择音频文件"), true);
            return;
        }
        const formData = new FormData();
        formData.append("file", file);
        try {
            const response = await TrpgApi.post<ApiResponse<BgmTrack>>("/api/bgm/tracks", formData);
            if (!response?.success) {
                setLibraryMessage(response?.message || response?.error || t("profile.bgm.library_upload_failed", "上传失败"), true);
                return;
            }
            if (input) input.value = "";
            setLibraryMessage(t("profile.bgm.library_uploaded_done", "音乐已上传"), false);
            await loadLibraryPanel();
        } catch (error) {
            console.warn("上传音乐失败", error);
            setLibraryMessage(t("profile.bgm.library_upload_failed", "上传失败"), true);
        }
    }

    async function removeTrack(track: BgmTrack): Promise<void> {
        const confirmed = await window.TrpgDialog?.confirm({
            message: t("profile.bgm.library_delete_confirm", "删除这首音乐？使用它的房间会自动移除该曲目。"),
            danger: true,
        });
        if (!confirmed) return;
        const filename = track.url.split("/").pop() || "";
        try {
            const response = await TrpgApi.del<ApiResponse<unknown>>(`/api/bgm/tracks/${encodeURIComponent(filename)}`);
            if (!response?.success) {
                setLibraryMessage(response?.message || response?.error || t("profile.bgm.library_delete_failed", "删除失败"), true);
                return;
            }
            setLibraryMessage(t("profile.bgm.library_deleted", "音乐已删除"), false);
            await loadLibraryPanel();
        } catch (error) {
            console.warn("删除音乐失败", error);
            setLibraryMessage(t("profile.bgm.library_delete_failed", "删除失败"), true);
        }
    }

    function handleToggle(): void {
        if (!audio || !currentUrl) return;
        if (audio.paused) {
            pausedByUser = false;
            attemptPlay();
        } else {
            pausedByUser = true;
            audio.pause();
        }
        updateToggleButton();
    }

    /** 个人主页音乐面板默认收起，点标题栏展开；状态记在本地，切换标签后保持。 */
    function bindPersonalCollapse(): void {
        const panel = document.getElementById("personalBgmSection");
        const toggle = document.getElementById("personalBgmToggle");
        if (!panel || !toggle) return;
        const apply = (expanded: boolean): void => {
            panel.classList.toggle("is-collapsed", !expanded);
            toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
            try {
                localStorage.setItem(PERSONAL_EXPANDED_KEY, expanded ? "1" : "0");
            } catch {
                // 忽略隐私模式下的写入失败
            }
        };
        let expanded = false;
        try {
            expanded = localStorage.getItem(PERSONAL_EXPANDED_KEY) === "1";
        } catch {
            expanded = false;
        }
        apply(expanded);
        toggle.addEventListener("click", () => {
            apply(panel.classList.contains("is-collapsed"));
        });
    }

    function handlePriorityChange(): void {
        const select = document.getElementById("personalBgmPriority") as HTMLSelectElement | null;
        if (!select) return;
        savePersonalSettings({ priority: select.value === "personal" ? "personal" : "room" });
        refresh();
        setPersonalMessage(t("bgm.panel.priority_saved", "播放优先级已保存"), false);
    }

    export function init(): void {
        if (initialized) return;
        const audioElement = document.getElementById("bgmAudio") as HTMLAudioElement | null;
        if (!audioElement) return;
        initialized = true;
        audio = audioElement;
        // 循环策略由播放模式控制（ended 事件里切歌），不再使用浏览器原生 loop。
        audio.loop = false;
        audio.addEventListener("play", updateToggleButton);
        audio.addEventListener("pause", updateToggleButton);
        audio.addEventListener("ended", handleEnded);
        audio.addEventListener("error", () => {
            if (currentUrl) markLoadError();
        });
        document.getElementById("bgmPlayerToggle")?.addEventListener("click", handleToggle);
        document.getElementById("bgmUploadButton")?.addEventListener("click", () => void uploadTrack());
        document.getElementById("personalBgmPriority")?.addEventListener("change", handlePriorityChange);
        bindPersonalCollapse();
        window.addEventListener("storage", (event) => {
            if (event.key === PERSONAL_URLS_KEY || event.key === PERSONAL_MODE_KEY || event.key === PRIORITY_KEY) {
                void renderPersonalSettings();
                refresh();
            }
        });
        updateToggleButton();
        refresh();
    }
}

window.TrpgBgm = {
    init: TrpgBgmModule.init,
    setRoom: TrpgBgmModule.setRoom,
    refresh: TrpgBgmModule.refresh,
    getPersonalSettings: TrpgBgmModule.getPersonalSettings,
    savePersonalSettings: TrpgBgmModule.savePersonalSettings,
    renderPersonalSettings: TrpgBgmModule.renderPersonalSettings,
    renderRoomPlaylist: TrpgBgmModule.renderRoomPlaylist,
    fetchTracks: TrpgBgmModule.fetchTracks,
    loadLibraryPanel: TrpgBgmModule.loadLibraryPanel,
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => TrpgBgmModule.init());
} else {
    TrpgBgmModule.init();
}
