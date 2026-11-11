import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_browser_sources_live_under_app_directory():
    assert not (ROOT / "frontend/src/js").exists()
    assert (ROOT / "frontend/src/app/main.ts").is_file()
    assert (ROOT / "frontend/src/app/auth/api.ts").is_file()
    assert (ROOT / "frontend/src/app/controllers/ScenarioController.ts").is_file()


def test_frontend_source_tree_separates_types_and_build_artifacts():
    assert (ROOT / "frontend/src/types/global.d.ts").is_file()
    assert not (ROOT / "frontend/src/app/types.d.ts").exists()
    assert not (ROOT / "frontend/dist").exists()
    assert not (ROOT / "js").exists()
    assert not (ROOT / "data/tools").exists()
    assert (ROOT / "frontend/src/tools/checkTool.ts").is_file()


def test_frontend_compiler_reads_app_sources_and_writes_dist_public():
    tsconfig = json.loads((ROOT / "tsconfig.frontend.json").read_text(encoding="utf-8"))

    assert "frontend/src/app/**/*.ts" in tsconfig["include"]
    assert "frontend/src/js/**/*.ts" not in tsconfig["include"]
    assert tsconfig["compilerOptions"]["outDir"] == "dist/public"


def test_frontend_theme_tokens_are_loaded_before_component_styles():
    app_css = (ROOT / "frontend/src/react/app.css").read_text(encoding="utf-8")
    first_import = next(line for line in app_css.splitlines() if line.startswith("@import"))

    assert first_import == '@import "../styles/00-theme-tokens.css";'
    assert (ROOT / "frontend/src/styles/00-theme-tokens.css").is_file()


def test_frontend_theme_selector_exposes_cyber_theme():
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")

    assert '<option value="pattern_cyber_2" data-i18n="profile.theme.cyber">赛博档案</option>' in settings_fragment


def test_frontend_theme_manager_uses_canonical_body_theme_classes():
    config_manager = (ROOT / "frontend/src/app/config/ConfigManager.ts").read_text(encoding="utf-8")

    assert 'themeClassNames = ["theme-light", "theme-dark", "theme-cyber-2", "theme-tome", "theme-coc", "light-theme", "dark-theme"]' in config_manager
    assert 'body.classList.add("theme-cyber-2")' in config_manager
    assert 'body.classList.add("theme-tome")' in config_manager


def test_frontend_sidebar_uses_theme_tokens_instead_of_bootstrap_color_utilities():
    sidebar = (ROOT / "frontend/src/react/shell/Sidebar.tsx").read_text(encoding="utf-8")

    assert "bg-dark" not in sidebar
    assert "text-white" not in sidebar


def test_login_checkboxes_use_auth_specific_rendering_and_no_focus_glow():
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    auth_css = (ROOT / "frontend/src/styles/04-auth-profile-overrides.css").read_text(encoding="utf-8")

    assert 'class="auth-check-input"' in settings_fragment
    assert ".auth-check-input:checked::after" in auth_css
    assert ".auth-check-input:focus" in auth_css
    assert "box-shadow: none;" in auth_css
    assert "transition: border-color 0.16s ease, background-color 0.16s ease, transform 0.16s ease;" in auth_css
    assert "inset: 50% auto auto 50%;" in auth_css
    assert "transform: translate(-50%, -58%) rotate(45deg);" in auth_css


def test_scenario_preview_long_text_wraps_in_preview_sections():
    scenario_css = (ROOT / "frontend/src/styles/02-scenario-character.css").read_text(encoding="utf-8")
    scenario_template = (ROOT / "frontend/src/templates/scenario.html").read_text(encoding="utf-8")

    assert ".scenario-preview" in scenario_css
    assert "overflow-wrap: anywhere;" in scenario_css
    assert "white-space: pre-wrap;" in scenario_css
    assert ".scenario-preview-segment" in scenario_css
    assert "margin: 0 0 8px;" in scenario_css
    assert 'class="scenario-preview-segment mb-3"' not in scenario_template
    assert 'class="scenario-preview-segment"' in scenario_template


def test_agent_page_replaces_tools_tab():
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    main_ts = (ROOT / "frontend/src/app/main.ts").read_text(encoding="utf-8")
    sidebar = (ROOT / "frontend/src/react/shell/Sidebar.tsx").read_text(encoding="utf-8")

    assert 'id="agent" class="tab-content"' in settings_fragment
    assert 'id="react-agent-page-root"' in settings_fragment
    assert 'data-tool=' not in settings_fragment
    assert 'id="dice-tool-content"' not in settings_fragment

    assert "initToolTabs" not in main_ts
    assert "initCommandToolPanels" not in main_ts
    assert "submitCocCheck" not in main_ts
    assert "submitScenarioContext" not in main_ts
    assert "submitRememberFact" not in main_ts

    assert 'tab: "agent"' in sidebar
    assert 'tab: "tools"' not in sidebar


def test_scenario_editor_exposes_module_summary_action_and_role_description():
    scenario_view = (ROOT / "frontend/src/app/views/ScenarioView.ts").read_text(encoding="utf-8")
    scenario_modules = (ROOT / "frontend/src/app/views/scenario-modules.ts").read_text(encoding="utf-8")
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    platform_ui = (ROOT / "frontend/src/app/platform-ui.ts").read_text(encoding="utf-8")
    styles = (ROOT / "frontend/src/styles/03-tools-settings-platform.css").read_text(encoding="utf-8")

    assert 'data-generate-module-summary' in scenario_modules
    assert 'scenario-module-summary-button' in scenario_modules
    assert '/api/scenarios/module-summary' in scenario_view
    assert 'id="roleConfigList"' in settings_fragment
    assert 'role-description-input' in platform_ui
    assert '.role-config-card' in styles


def test_room_restore_keeps_last_selected_character_card_for_room():
    rooms_source = (ROOT / "frontend/src/app/rooms.ts").read_text(encoding="utf-8")

    assert 'getLastRoomCharacterStorageKey' in rooms_source
    assert 'syncRoomCharacterSelection' in rooms_source
    assert 'promptRoomEntryCharacterSelection("join", roomId, roomSkillBaseOverrides(' in rooms_source
    assert 'const selfMember = activeRoomMembers(room).find' in rooms_source


def test_chat_tool_messages_render_before_final_ai_reply():
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")

    assert "const toolMessages = data.tool_messages || [];" in chat_source
    assert "const directMessages = data.direct_messages" in chat_source
    assert "if (toolMessages.length > 0) {" in chat_source
    assert "moveThinkingMessageToEnd(thinkingMessageId);" in chat_source
    assert chat_source.index("for (const toolMessage of toolMessages)") < chat_source.index("replaceThinkingMessage(")


def test_room_scenario_start_control_uses_hidden_ai_start_flow():
    room_html = (ROOT / "frontend/src/index/fragments/02-main-tabs.html").read_text(encoding="utf-8")
    rooms_source = (ROOT / "frontend/src/app/rooms.ts").read_text(encoding="utf-8")
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")

    assert 'id="startScenario"' in room_html
    assert 'window.startScenario?.()' in rooms_source
    assert "scenario_start: options.scenarioStart === true" in chat_source
    assert "async function startScenario" in chat_source


def test_chat_resume_only_reopens_scenario_for_room_managers():
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")

    # 持久化的思考状态必须记录 scenarioStart，恢复时写回模块级标志。
    assert "scenarioStart: options.scenarioStart === true" in chat_source
    assert "restoredScenarioStart = state.scenarioStart === true;" in chat_source
    # 重发脚本开始前必须校验房主/管理员权限，否则清除状态并提示。
    resume_block = chat_source.split("function resumePendingAIRequest(", 1)[1]
    assert "canManageCurrentRoom()" in resume_block
    assert "只有房主或管理员可以开启剧本" in resume_block
    assert "scenarioStart ? { scenarioStart: true } : {}" in resume_block


def test_chat_persists_and_renders_knowledge_usage_below_kp_message():
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")
    chat_template = (ROOT / "frontend/src/templates/chat.html").read_text(encoding="utf-8")

    assert "knowledge_usage?: KnowledgeUsage" in chat_source
    assert "knowledgeUsage: data.knowledge_usage" in chat_source
    assert "renderKnowledgeUsage" in chat_source
    assert 'id="chat-knowledge-usage"' in chat_template
    assert chat_source.index("replaceThinkingMessage(") < chat_source.index("for (const directMessage of directMessages)")


def test_chat_thinking_state_reports_backend_stages_with_reduced_motion_support():
    chat_source = (ROOT / "frontend/src/app/chat.ts").read_text(encoding="utf-8")
    styles = (ROOT / "frontend/src/styles/04-auth-profile-overrides.css").read_text(encoding="utf-8")

    assert 'ai_request_id: aiRequestId' in chat_source
    assert 'incoming.type === "ai_thinking_stage"' in chat_source
    assert "function updateThinkingStage(" in chat_source
    assert 'data-thinking-label' in chat_source
    assert 'aria-live="polite"' in chat_source
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_main_tabs_have_decorated_page_headers():
    fragments = [
        (ROOT / "frontend/src/index/fragments/02-main-tabs.html").read_text(encoding="utf-8"),
        (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8"),
    ]
    combined = "\n".join(fragments)

    for tab_id in ["chat", "save", "scenario", "characters", "agent", "settings"]:
        assert f'data-page-header="{tab_id}"' in combined
    assert "page-command-header" in combined


def test_dark_theme_text_tokens_are_less_luminous():
    tokens = (ROOT / "frontend/src/styles/00-theme-tokens.css").read_text(encoding="utf-8")

    assert "--theme-text-primary: #d7dde3;" in tokens
    assert "--theme-text-secondary: #b8c0ca;" in tokens
    assert "--theme-text-primary: #c7d8d1;" in tokens
    assert "--theme-text-secondary: #aebfb8;" in tokens


def test_settings_exposes_data_driven_permission_matrix():
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    tabs_ts = (ROOT / "frontend/src/app/tabs.ts").read_text(encoding="utf-8")
    styles = (ROOT / "frontend/src/styles/03-tools-settings-platform.css").read_text(encoding="utf-8")

    # 权限与用户管理合并到「用户与权限」分类。
    assert 'data-settings="access"' in settings_fragment
    assert 'id="access-settings-content"' in settings_fragment
    assert 'id="permissionMatrix"' in settings_fragment
    assert "/api/config/permissions" in tabs_ts
    assert "renderPermissionMatrix" in tabs_ts
    assert "savePermissionConfig" in tabs_ts
    assert "permission-node-card" in styles

    # Embedding 状态条由 API 驱动并自绘；旧分类只允许在深链接迁移表中出现。
    assert 'class="embedding-status"' in settings_fragment
    assert 'id="embeddingStatusRefresh"' in settings_fragment
    assert "initSettingsDeepLinks" in tabs_ts
    assert "LEGACY_SETTINGS_TAB_NAMES" in tabs_ts


def test_user_changed_event_fires_after_admin_navigation_refresh():
    state_ts = (ROOT / "frontend/src/app/auth/state.ts").read_text(encoding="utf-8")

    # 深链接监听 trpg:user-changed；必须等管理员导航刷新后再派发，否则管理员页签仍是 hidden。
    assert state_ts.index("window.refreshAdminNavigation?.()") < state_ts.index('dispatchEvent(new CustomEvent("trpg:user-changed"')


def test_improvement_sidebar_exposes_discovery_and_admin_gated_settings():
    sidebar = (ROOT / "frontend/src/react/shell/Sidebar.tsx").read_text(encoding="utf-8")

    assert 'label: "角色卡"' in sidebar
    assert 'label: "房间"' in sidebar
    assert 'label: "剧本管理"' not in sidebar
    assert 'label: "角色卡管理"' not in sidebar
    assert 'label: "房间管理"' not in sidebar
    assert 'label: "探索发现"' in sidebar
    assert 'label: "角色卡广场"' in sidebar
    assert "data-admin-only" in sidebar
    assert '"true"' in sidebar


def test_improvement_frontend_exposes_gallery_and_profile_tabs():
    main_tabs = (ROOT / "frontend/src/index/fragments/02-main-tabs.html").read_text(encoding="utf-8")
    auth_settings = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    character_source = (ROOT / "frontend/src/rulesets/coc7/character-sheet.ts").read_text(encoding="utf-8")
    tabs_source = (ROOT / "frontend/src/app/tabs.ts").read_text(encoding="utf-8")

    assert 'id="character-gallery"' in main_tabs
    assert 'id="personal-home"' in main_tabs
    assert 'id="openCharacterGallery"' in main_tabs
    assert 'id="open-personal-home"' in auth_settings
    assert "/api/character-gallery" in character_source
    assert "applyGalleryCharacter" in character_source
    assert "switchMainTab" in tabs_source


def test_character_gallery_cards_have_search_summary_and_public_ids():
    main_tabs = (ROOT / "frontend/src/index/fragments/02-main-tabs.html").read_text(encoding="utf-8")
    character_source = (ROOT / "frontend/src/rulesets/coc7/character-sheet.ts").read_text(encoding="utf-8")
    character_css = (ROOT / "frontend/src/styles/02-scenario-character.css").read_text(encoding="utf-8")

    assert 'id="characterGallerySearch"' in main_tabs
    assert "filteredGalleryCards" in character_source
    assert "character-card-summary" in character_source
    assert 'data-gallery-action="preview"' in character_source
    assert "card.public_id || card.id" in character_source
    assert "publisher_name" in character_source
    assert ".character-gallery-toolbar" in character_css


def test_character_attribute_base_total_omits_limit_suffix_and_box():
    editor_fragment = (ROOT / "frontend/src/index/fragments/04-editor-modals.html").read_text(encoding="utf-8")
    character_source = (ROOT / "frontend/src/rulesets/coc7/character-sheet.ts").read_text(encoding="utf-8")
    character_css = (ROOT / "frontend/src/styles/02-scenario-character.css").read_text(encoding="utf-8")

    assert '<strong id="attributeBaseTotal">0</strong>' in editor_fragment
    assert "calculateAttributeBaseTotal(card.attributes)} / 900" not in character_source
    assert 'setText("attributeBaseTotal", `${calculateAttributeBaseTotal(attributes)} / 900`)' not in character_source
    assert ".character-attribute-summary" in character_css
    assert "background: transparent;" in character_css


def test_improvement_scenario_cards_expose_permissions_and_public_ids():
    scenario_template = (ROOT / "frontend/src/templates/scenario.html").read_text(encoding="utf-8")
    scenario_view = (ROOT / "frontend/src/app/views/ScenarioView.ts").read_text(encoding="utf-8")
    editor_fragment = (ROOT / "frontend/src/index/fragments/04-editor-modals.html").read_text(encoding="utf-8")

    assert "play-scenario" in scenario_template
    assert "createdBy" in scenario_template
    assert "publicId" in scenario_template
    assert "scenario-id-chip" in scenario_template
    assert "保存并发布" in editor_fragment
    assert "confirmScenarioSpoilerAccess" in scenario_view


def test_home_title_is_bound_to_current_room_name():
    home_chat = (ROOT / "frontend/src/react/home/HomeChat.tsx").read_text(encoding="utf-8")
    rooms = (ROOT / "frontend/src/app/rooms.ts").read_text(encoding="utf-8")

    assert 'id="homeRoomTitle"' in home_chat
    assert "跑团频道" not in home_chat
    assert 'setText("homeRoomTitle", room.name)' in rooms
    assert 'setText("homeRoomTitle", "未加入房间")' in rooms


def test_profile_settings_contains_personal_theme_with_pending_save_controls():
    settings_fragment = (ROOT / "frontend/src/index/fragments/03-room-tools-auth-settings.html").read_text(encoding="utf-8")
    profile_source = (ROOT / "frontend/src/app/auth/profile-dialog.ts").read_text(encoding="utf-8")
    user_card_source = (ROOT / "frontend/src/app/auth/user-card.ts").read_text(encoding="utf-8")
    config_source = (ROOT / "frontend/src/app/config/ConfigManager.ts").read_text(encoding="utf-8")

    assert "编辑个人资料" not in settings_fragment
    assert "用户设置" in settings_fragment
    assert "网站设置" in settings_fragment
    assert "fa-cog" in settings_fragment
    assert 'id="profileThemeSelect"' in settings_fragment
    assert "网站主题切换：" in settings_fragment
    assert 'id="profilePendingSaveBar"' in settings_fragment
    assert 'id="saveProfilePendingChanges"' in settings_fragment
    assert 'id="cancelProfilePendingChanges"' in settings_fragment
    assert "profile-setting-dirty" in profile_source
    assert "trpg_user_theme" in profile_source
    assert "closeUserCardOnOutsideClick" in user_card_source
    assert "getEffectiveTheme" in config_source


def test_frontend_localization_catalogs_are_complete_and_utf8_clean():
    locale_dir = ROOT / "frontend/src/locales"
    zh_path = locale_dir / "zh_cn.json"
    en_path = locale_dir / "en_us.json"
    assert zh_path.is_file()
    assert en_path.is_file()

    zh = json.loads(zh_path.read_text(encoding="utf-8"))
    en = json.loads(en_path.read_text(encoding="utf-8"))
    assert zh["locale"] == "zh_cn"
    assert en["locale"] == "en_us"
    assert set(zh["translations"]) == set(en["translations"])

    for path in (zh_path, en_path):
        raw = path.read_text(encoding="utf-8")
        assert "\ufffd" not in raw
        json.loads(raw)


def test_explicit_frontend_translation_keys_exist_in_both_catalogs():
    catalogs = [
        json.loads((ROOT / "frontend/src/locales/zh_cn.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "frontend/src/locales/en_us.json").read_text(encoding="utf-8")),
    ]
    keys = set().union(*(set(catalog["translations"]) for catalog in catalogs))
    source_files = list((ROOT / "frontend/src").rglob("*.html"))
    source_files.extend((ROOT / "frontend/src/react").rglob("*.tsx"))
    source_files.extend((ROOT / "frontend/src/app").rglob("*.ts"))
    pattern = re.compile(r'data-i18n(?:-(?:placeholder|title|aria-label|alt))?="([^"]+)"')
    referenced = set()
    for path in source_files:
        referenced.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert referenced <= keys | {"Language", "Simplified Chinese"}


def test_localization_runtime_loads_catalogs_and_protects_user_content():
    runtime = (ROOT / "frontend/src/app/i18n.ts").read_text(encoding="utf-8")
    assert "fetch(`/locales/${locale}.json`" in runtime
    assert "setLocale" in runtime
    assert 'parent.closest("[data-user-content], [data-i18n-dynamic]")' in runtime
    assert "data-i18n-alt" in runtime
    assert "observer.observe(document.body, { childList: true, subtree: true })" in runtime
    assert "characterData: true" not in runtime


def test_build_script_copies_localization_catalogs():
    build_script = (ROOT / "scripts/build-frontend.mjs").read_text(encoding="utf-8")
    assert "frontend" in build_script
    assert "locales" in build_script
    assert "dist" in build_script


def test_character_sheet_treats_missing_ruleset_id_as_default_plugin():
    """历史卡可能没有 rulesetId：必须按默认插件（coc7）打开，不能被当成外部卡阻断。"""
    source = (ROOT / "frontend/src/rulesets/coc7/character-sheet.ts").read_text(encoding="utf-8")

    # 唯一的归一入口：缺字段 / 空串 / 纯空白都落到默认插件。
    assert 'const RULESET_ID = "coc7";' in source
    assert "function normalizeRulesetId(raw: unknown): string {" in source
    assert 'return String(raw ?? "").trim().toLowerCase() || RULESET_ID;' in source

    # 卡数据 → 视图模型、schema 归一、导入、归属判定与注册声明全部走同一入口。
    assert "rulesetId: normalizeRulesetId(input.rulesetId)," in source
    assert "const rulesetId = normalizeRulesetId(input.rulesetId);" in source
    assert "return normalizeRulesetId(card?.rulesetId) === RULESET_ID;" in source
    assert "supportsRuleset: (rulesetId?: string | null) => normalizeRulesetId(rulesetId) === RULESET_ID," in source
    assert 'normalizeRulesetId(payload.rulesetId)' in source

    # 编辑/详情只拦截「明确声明的外部规则集」；缺 id 的卡照常进入编辑。
    assert "if (card && !supportsCard(card)) {" in source
    assert "if (!supportsCard(card)) {" in source
    assert 'rulesetId !== "coc7"' not in source
    assert 'rulesetId === "coc7"' not in source


def test_unified_immersive_player_is_loaded_and_controls_both_sources():
    scripts = (ROOT / "frontend/src/index/fragments/05-platform-modals-and-scripts.html").read_text(encoding="utf-8")
    player = (ROOT / "frontend/src/app/immersive/player.ts").read_text(encoding="utf-8")

    # 脚本必须在沉浸模块之后加载：播放器依赖 TrpgImmersiveMusic / TrpgImmersive。
    assert '<script src="js/immersive/player.js"></script>' in scripts
    assert scripts.index("js/immersive/index.js") < scripts.index("js/immersive/player.js")

    # 同一套控件驱动两套音源。
    assert "window.TrpgBgm?.getPlaybackState" in player
    assert "window.TrpgBgm?.next" in player
    assert "window.TrpgImmersiveMusic?.skipSection" in player
    assert "window.TrpgImmersiveMusic?.pause" in player
    assert "window.TrpgImmersiveMusic?.resume" in player

    # 挂载到右侧栏沉浸区（窄屏回落到聊天区），与配图/配乐面板共用宿主。
    assert '"immersiveSidebarStageHost"' in player
    assert '"immersiveChatStageHost"' in player

    # 程序化配乐没有音轨：进度条必须按快照隐藏，不能给用户拖一个假进度。
    assert "range.hidden = !snapshot.draggable;" in player
    assert "draggable: false" in player


def test_immersive_player_styles_use_theme_tokens():
    styles = (ROOT / "frontend/src/styles/11-immersive.css").read_text(encoding="utf-8")

    assert ".immersive-player {" in styles
    assert ".immersive-player-btn.is-primary" in styles
    assert ".immersive-player-livedot" in styles
    assert "accent-color: var(--theme-accent-primary);" in styles
    assert "background: var(--theme-bg-panel-muted);" in styles
    assert "color: var(--theme-text-inverse);" in styles
    # 所有主题共用 token，不允许为某套主题单独写播放器颜色补丁。
    for theme in ["theme-dark", "theme-tome", "theme-cyber-2", "theme-light"]:
        assert f".{theme} .immersive-player" not in styles


def test_bgm_entry_binds_without_hardcoded_audio_node():
    bgm = (ROOT / "frontend/src/app/bgm.ts").read_text(encoding="utf-8")
    tabs = (ROOT / "frontend/src/app/tabs.ts").read_text(encoding="utf-8")

    # 回归点：旧 init 在缺少 #bgmAudio 时提前 return，导致「我的背景音乐」点不开。
    assert "if (!audioElement) return;" not in bgm
    assert "function ensureAudio(): HTMLAudioElement {" in bgm
    assert "const audioElement = ensureAudio();" in bgm

    # 个人面板的展开/收起必须幂等且不依赖音频节点。
    assert "let personalCollapseBound = false;" in bgm
    assert "if (personalCollapseBound) return;" in bgm
    init_block = bgm.split("export function init(", 1)[1]
    assert "bindPersonalCollapse();" in init_block

    # 个人主页标签被打开时才渲染曲目列表，入口文案与面板都在同一个 section 内。
    assert "window.TrpgBgm?.renderPersonalSettings()" in tabs
    fragment = (ROOT / "frontend/src/index/fragments/02-main-tabs.html").read_text(encoding="utf-8")
    assert 'id="personalBgmSection"' in fragment
    assert 'id="personalBgmToggle"' in fragment


def test_procedural_music_pause_keeps_position_and_stops_scheduling():
    engine = (ROOT / "frontend/src/app/immersive/music-engine.ts").read_text(encoding="utf-8")

    # 暂停：记住「真正出声的小节」、停掉调度、清空总线（避免双总线叠响）。
    pause_block = engine.split("export function pause(): void {", 1)[1].split("export function resume(", 1)[0]
    assert "paused = true;" in pause_block
    assert "pausedPositionBars = live.audibleBars();" in pause_block
    assert "stopScheduler();" in pause_block
    assert "liveBuses = [];" in pause_block

    # 恢复：从记录的小节重建单条总线，不从头开始。
    resume_block = engine.split("export function resume(): void {", 1)[1].split("export function isPaused", 1)[0]
    assert "paused = false;" in resume_block
    assert "switchBus(activePlan, null, pausedPositionBars);" in resume_block

    # 调度与 ducking 都必须尊重暂停，否则 tick 会在暂停期间偷偷补排。
    assert "audibleBars(): number {" in engine
    assert "if (paused) return;" in engine


def test_external_track_player_exposes_unified_playback_controls():
    external = (ROOT / "frontend/src/app/immersive/external-player.ts").read_text(encoding="utf-8")
    types = (ROOT / "frontend/src/types/immersive.d.ts").read_text(encoding="utf-8")

    # 统一播放器需要的播放态接口。
    assert "export function getPlaybackState(): ImmersiveExternalPlaybackState {" in external
    assert "export function play(): void {" in external
    assert "export function pause(): void {" in external
    assert "export function resume(): void {" in external
    assert "export function next(): void {" in external
    assert "export function prev(): void {" in external
    assert "export function seek(seconds: number): void {" in external
    assert "getPlaybackState: TrpgImmersiveExternalModule.getPlaybackState" in external
    assert "next: TrpgImmersiveExternalModule.next" in external
    assert "seek: TrpgImmersiveExternalModule.seek" in external
    assert "interface ImmersiveExternalPlaybackState {" in types
    assert "getPlaybackState: () => ImmersiveExternalPlaybackState;" in types

    # 切歌池来自服务端下发的 alternatives，暂停/恢复不重置播放位置。
    assert "payload.alternatives || []" in external
    assert "element.currentTime" in external
    assert 'mode === "paused"' in external
    assert "pausedByUser" in external


def test_external_player_is_wired_into_unified_player_and_not_muted_by_engine_gain():
    player = (ROOT / "frontend/src/app/immersive/player.ts").read_text(encoding="utf-8")
    engine = (ROOT / "frontend/src/app/immersive/music-engine.ts").read_text(encoding="utf-8")
    external = (ROOT / "frontend/src/app/immersive/external-player.ts").read_text(encoding="utf-8")
    immersive_index = (ROOT / "frontend/src/app/immersive/index.ts").read_text(encoding="utf-8")

    # 统一播放器把外链接入同一套按钮，且不改动既有档位优先级。
    assert 'type Primary = "procedural" | "external" | "bgm" | "none";' in player
    assert 'primary = extUsable ? "external"' in player
    assert 'primary = bgmUsable ? "bgm" : extUsable ? "external"' in player
    assert "window.TrpgImmersiveExternal?.next" in player
    assert "window.TrpgImmersiveExternal?.prev" in player
    assert "window.TrpgImmersiveExternal?.pause" in player
    assert "window.TrpgImmersiveExternal?.resume" in player
    assert "window.TrpgImmersiveExternal?.seek" in player

    # 外链事件本身就是引擎交接信号：否则 external 档永远不会开始。
    assert 'payload.engine === "external"' in external
    assert "applyEngine({" in external
    assert 'if (engineMode === "external") {' in engine

    # 回归点：external 档会把 engineGain 淡到 0，外链不能再挂到那里，否则整首静音。
    external_input = engine.split("export function externalInput(): GainNode | null {", 1)[1].split("}", 1)[0]
    assert "musicMuteGain" in external_input
    assert "engineGain" not in external_input

    # 切到仅 BGM / 离开房间时必须停掉外链，避免引擎销毁后还在后台播放。
    assert "window.TrpgImmersiveExternal?.stop?.(600);" in immersive_index


def test_hybrid_overlay_row_controls_external_track_without_taking_over_bgm():
    player = (ROOT / "frontend/src/app/immersive/player.ts").read_text(encoding="utf-8")
    external = (ROOT / "frontend/src/app/immersive/external-player.ts").read_text(encoding="utf-8")
    styles = (ROOT / "frontend/src/styles/11-immersive.css").read_text(encoding="utf-8")

    # 只在这个条件下出现：混合档 + BGM 仍是主轨 + 外链引擎在跑 + 外链有队列。
    assert 'if (source !== "hybrid" || engineMode() !== "external") return null;' in player
    assert "if (!external?.active || external.count <= 0) return null;" in player
    assert 'overlay: overlayInfo(source, external)' in player
    assert "overlay: null," in player

    # 次要行：显示曲名，两个按钮复用外链已有接口，不新开状态。
    assert 'id="immersivePlayerOverlay"' in player
    assert 'id="immersivePlayerOverlayTitle"' in player
    assert 'id="immersivePlayerOverlayPrev"' in player
    assert 'id="immersivePlayerOverlayNext"' in player
    assert 'byId<HTMLButtonElement>("immersivePlayerOverlayPrev")?.addEventListener("click", () => skipOverlay(-1));' in player
    assert 'byId<HTMLButtonElement>("immersivePlayerOverlayNext")?.addEventListener("click", () => skipOverlay(1));' in player
    assert "window.TrpgImmersiveExternal?.next?.();" in player
    assert "window.TrpgImmersiveExternal?.prev?.();" in player
    assert 'document.addEventListener("trpg:external-state", () => refresh());' in player

    # 外链切歌/停止时主动通知，保证曲名即时更新。
    assert "function notifyState(): void {" in external
    assert 'document.dispatchEvent(new CustomEvent("trpg:external-state"));' in external

    # 样式复用播放器 token，没有为单套主题写死颜色。
    assert ".immersive-player-overlay {" in styles
    assert "background: var(--theme-bg-panel-muted);" in styles
    assert "color: var(--theme-accent-primary);" in styles
    assert ".immersive-player-btn.is-compact {" in styles
    assert ".immersive-player-overlay[hidden] {" in styles


def test_external_idle_hands_engine_back_and_player_reads_engine_mode():
    player = (ROOT / "frontend/src/app/immersive/player.ts").read_text(encoding="utf-8")
    external = (ROOT / "frontend/src/app/immersive/external-player.ts").read_text(encoding="utf-8")

    # 引擎交还的判定表：只有「引擎在 external + 队列空」才切走。
    assert "export function nextEngineForIdle(" in external
    table = external.split("export function nextEngineForIdle(", 1)[1].split("\n    }", 1)[0]
    assert 'if (engine !== "external") return "keep";' in table
    assert 'if (hasQueue) return "keep";' in table
    assert 'return hasPlan ? "procedural" : "off";' in table
    assert '): "keep" | "procedural" | "off" {' in table

    # 收尾必须真的调用引擎切换，并让底床跟回来；restoreBed 的 external 守卫保留。
    settle = external.split("function settleEngine(", 1)[1].split("\n    }", 1)[0]
    assert "api?.setEngineMode?.(decision, fadeMs);" in settle
    assert "restoreBed(Math.max(0.6, fadeMs / 1000));" in settle
    assert 'if (window.TrpgImmersiveMusic?.currentEngineMode?.() === "external") return;' in external

    # 自然播完：队列作废并交还引擎，避免停在 external 无声。
    ended = external.split("function handleEnded(index: number): void {", 1)[1].split("\n    }", 1)[0]
    assert "queue = [];" in ended
    assert "settleEngine(1200);" in ended
    # 加载失败：剔除失败曲目后同样走收尾；有备选曲时判定表会保留 external。
    degrade = external.split("function degrade(reason: string): void {", 1)[1]
    assert "queue = queue.filter((item) => item.track_key !== failedKey);" in degrade
    assert "settleEngine(900);" in degrade

    # 播放器主轨按「实际引擎模式」判定，不再只看 currentPlan。
    assert 'const extUsable = Boolean(external?.active) && engine === "external";' in player
    assert 'const procUsable = Boolean(currentPlan) && engine === "procedural";' in player
