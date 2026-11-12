"""房间路由入口。

实现已按域拆分：纯逻辑在 :mod:`trpg_server.room_ops`，各域路由在
``routes/rooms_*.py``。本模块只做三件事：

1. 提供 ``bp``：所有子模块共享同一个蓝图对象，因此 endpoint 仍是
   ``rooms.<view_func>``，URL 与规则完全不变；
2. 重新导出全部历史符号：``from trpg_server.routes.rooms import _find_member``
   等既有调用方（room_*_commands、routes.combat 等）无需改动；
3. 导入各域路由模块以触发注册。
"""
from trpg_server.room_ops.blueprint import bp
from trpg_server.room_ops.log import logger
from trpg_server.room_ops.constants import (
    DEFAULT_ROOM_HOUSE_RULES,
    HOUSE_RULE_DEFINITIONS,
    ROOM_MEMBER_ACTIVE,
    ROOM_MEMBER_REMOVED,
    ROOM_ROLE_ADMIN,
    ROOM_ROLE_MEMBER,
    ROOM_ROLE_OWNER,
    ROOM_VISIBILITIES,
    ROOM_VISIBILITY_PRIVATE,
    ROOM_VISIBILITY_PUBLIC,
    USER_ROOM_LIMIT,
    _BGM_AUDIO_SUFFIXES,
    _BGM_LOCAL_PREFIXES,
)

from trpg_server.room_ops.storage import (
    _autosave_node_limit,
    _autosave_settings,
    _configured_role,
    _created_room_count,
    _ensure_invite_token,
    _find_room,
    _find_room_by_code,
    _find_room_by_invite_token,
    _find_room_by_name,
    _get_ai_platform_dir,
    _get_archives_dir,
    _get_config_dir,
    _get_kp_prompt_file,
    _get_role_config_file,
    _get_rooms_dir,
    _get_scenarios_dir,
    _global_hints_enabled,
    _iter_room_dirs,
    _messages_file,
    _new_invite_token,
    _new_room_code,
    _normalize_room_name,
    _read_messages,
    _read_room,
    _room_cleanup_days,
    _room_dir,
    _room_name_exists,
    _timestamp,
    _write_messages,
    _write_room,
)

from trpg_server.room_ops.house_rules import (
    _admin_dice_thresholds,
    _is_local_bgm_path,
    _normalize_audio_blocks,
    _normalize_bgm_url,
    _normalize_bgm_url_list,
    _normalize_nullable_int,
    _normalize_skill_bases,
    _normalize_user_ids,
    _room_dice_animation,
    _room_dice_thresholds,
    _room_house_rules,
)

from trpg_server.room_ops.cards import (
    _bind_character,
    _bounded_int,
    _card_in_use_response,
    _derive_character_card,
    _ensure_character_state,
    _fallback_ruleset_id,
    _find_card_conflict,
    _initial_character_state,
    _pending_room_changes,
    _recalculate_character_state,
    _ruleset_plugin,
    _sanitize_character_card,
)

from trpg_server.room_ops.access import (
    _assign_room_owner,
    _can_access,
    _can_bind_character_card,
    _can_manage,
    _can_manage_members,
    _current_member,
    _denied,
    _earliest_active_member,
    _emit_room_members_changed,
    _find_member,
    _is_active_member,
    _is_elevated,
    _is_member_online,
    _joined_at_timestamp,
    _live_member_role,
    _normalize_members,
    _require_login,
    _room_has_online_member,
    _room_permission,
    _room_permission_label,
    world_override_allowed,
)

from trpg_server.room_ops.views import (
    _append_character_archive_records,
    _archive_visible,
    _build_room_archive,
    _change_record_text,
    _character_delta,
    _invisible_room_view,
    _room_ruleset_id,
    _room_summary,
    _room_usage_for,
    _room_visibility,
    _scenario_finished,
)

from trpg_server.room_ops.visibility import (
    filter_payload_messages,
    viewer_is_kp,
    visible_messages,
)

from trpg_server.room_ops.autosave import (
    _keyframe_for_autosave,
    _write_room_autosave,
    run_inactive_room_cleanup,
    run_scheduled_autosaves,
    save_all_rooms_on_shutdown,
)

# 导入各域路由模块（导入即注册路由），同时重新导出视图函数。
from trpg_server.routes.rooms_house_rules import (
    get_room_house_rules,
    get_world_override_status,
    update_room_house_rules,
)

from trpg_server.routes.rooms_interlude import (
    _emit_interlude_updated,
    _interlude_payload,
    add_room_interlude_review,
    end_room_interlude,
    get_room_interlude,
    load_room_autosave,
    request_room_interlude,
    save_room_autosave,
    settle_room_interlude,
    start_room_interlude,
)

from trpg_server.routes.rooms_lifecycle import (
    _first_scene,
    archive_room,
    create_room,
    delete_room,
    get_room,
    get_room_archive,
    get_room_invite_token,
    get_room_usage,
    join_room_by_code,
    list_room_archives,
    list_rooms,
    lookup_room_by_code,
    migrate_room_scenario,
    rotate_room_invite_token,
    spectate_room,
    spectate_room_by_code,
    switch_room_scenario,
)

from trpg_server.routes.rooms_members import (
    bind_room_member_character,
    delete_room_member,
    leave_room,
    transfer_room_owner,
    update_room_member_role,
)

from trpg_server.routes.rooms_messages import (
    _create_character_record_for_room,
    create_character_record,
    create_character_record_by_room_name,
    create_room_message,
    create_room_node,
    delete_character_record,
    delete_room_node,
    get_room_messages,
    get_room_node,
    list_room_nodes,
    restore_room_node,
    trigger_room_scenario,
)