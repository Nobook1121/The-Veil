"""角色卡与成员运行状态：卡清洗、规则集派生、资源状态初始化与写回判定。"""
from trpg_server import weapon_catalog
from trpg_server.responses import error_response
from trpg_server.room_card_occupancy import card_id_of
from trpg_server.room_card_occupancy import find_card_occupancy
from trpg_server.room_ops.storage import _get_rooms_dir
from trpg_server.ruleset import default_ruleset_id

def _fallback_ruleset_id() -> str:
    """当前默认插件 id；无可用插件时返回空串，避免读取旧房间快照时抛错。"""
    try:
        return default_ruleset_id()
    except RuntimeError:
        return ""

def _card_in_use_response(conflict):
    """角色卡已被其它活跃房间占用时的统一拒绝响应。

    带上占用房间与角色卡名，前端据此给出「换卡 / 去那个房间退出」的具体提示，
    而不是只显示一句通用失败。
    """
    room_name = conflict.get("room_name") or conflict.get("room_id") or "另一个房间"
    card_name = conflict.get("card_name") or "该角色卡"
    return error_response(
        f"{card_name}正在《{room_name}》中使用，请更换角色卡或先退出那个房间",
        409,
        "character_card_in_use",
        conflict=conflict,
    )

def _find_card_conflict(character_card, *, exclude_room_id=None):
    return find_card_occupancy(
        _get_rooms_dir(),
        card_id_of(character_card),
        exclude_room_id=exclude_room_id,
    )

def _pending_room_changes(member):
    """成员在房间内尚未归档、会随归档写回的改动数量；0 表示没有。

    与归档写回的判断保持一致：优先看统一队列 ``pending_changes``，
    旧房间再兼容 ``item_changes`` 与 ``pending_persist`` 标记。
    """
    from trpg_server import room_rules

    state = member.get("character_state") if isinstance(member.get("character_state"), dict) else {}
    queued = room_rules.pending_changes(state)
    if queued:
        return len(queued)
    legacy_items = [
        record
        for record in (state.get("item_changes") or [])
        if isinstance(record, dict) and not record.get("persisted")
    ]
    if legacy_items:
        return len(legacy_items)
    return 1 if state.get("pending_persist") else 0

def _sanitize_character_card(character_card):
    if not isinstance(character_card, dict):
        return None

    name = str(character_card.get("name", "")).strip()
    card_id = str(character_card.get("id", "")).strip()
    if not name or not card_id:
        return None

    attributes = character_card.get("attributes") if isinstance(character_card.get("attributes"), dict) else {}
    max_hp = _bounded_int(character_card.get("maxHp", character_card.get("max_hp")), 1, 999, 1)
    max_san = _bounded_int(character_card.get("maxSan", character_card.get("max_san")), 0, 999, 0)
    max_mp = _bounded_int(character_card.get("maxMp", character_card.get("max_mp")), 0, 999, 0)
    status = character_card.get("status") if isinstance(character_card.get("status"), dict) else {}
    sanitized = {
        "id": card_id[:80],
        "name": name[:80],
        "rulesetId": str(character_card.get("rulesetId") or _fallback_ruleset_id())[:40],
        "occupationId": str(character_card.get("occupationId", character_card.get("occupation_id", "")))[:80],
        "attributes": attributes,
        "maxHp": max_hp,
        "currentHp": _bounded_int(character_card.get("currentHp", character_card.get("current_hp")), 0, max_hp, max_hp),
        "maxSan": max_san,
        "initialSan": _bounded_int(character_card.get("initialSan"), 0, 999, max_san),
        "currentSan": _bounded_int(character_card.get("currentSan", character_card.get("current_san")), 0, max_san, max_san),
        "maxMp": max_mp,
        "currentMp": _bounded_int(character_card.get("currentMp", character_card.get("magicPoints")), 0, max_mp, max_mp),
        "mov": _bounded_int(character_card.get("mov"), 0, 99, 0),
        "build": _bounded_int(character_card.get("build"), -2, 99, 0),
        "damageBonus": str(character_card.get("damageBonus") or "")[:20],
        "armor": _bounded_int(character_card.get("armor"), 0, 99, 0),
        "skills": character_card.get("skills", []) if isinstance(character_card.get("skills", []), list) else [],
        "weapons": weapon_catalog.sanitize_character_weapons(character_card.get("weapons")),
        "customWeapons": weapon_catalog.sanitize_character_weapons(character_card.get("customWeapons")),
        "equipment": character_card.get("equipment", []) if isinstance(character_card.get("equipment", []), list) else [],
        "background": character_card.get("background", {}) if isinstance(character_card.get("background", {}), dict) else {},
        "status": status,
    }
    # 规则扩展命名空间（如 extensions.mental）：旧卡没有时保持缺省，不写空键。
    extensions = character_card.get("extensions")
    if isinstance(extensions, dict) and extensions:
        sanitized["extensions"] = extensions
    return _derive_character_card(sanitized)

def _derive_character_card(character_card):
    """调用当前规则集插件重算派生值；插件不可用时原样返回。"""
    if not isinstance(character_card, dict):
        return character_card
    try:
        from trpg_server.ruleset import get_plugin
        from trpg_server.ruleset.capabilities import derive_card

        return derive_card(get_plugin(character_card.get("rulesetId")), character_card)
    except Exception:  # noqa: BLE001 - 插件异常不应阻断房间读取
        return dict(character_card)

def _ruleset_plugin(character_card):
    """卡所属规则集插件；缺失 / 不可用时返回 ``None``（回落到核心默认槽位）。"""
    if not isinstance(character_card, dict):
        return None
    try:
        from trpg_server.ruleset import get_plugin

        return get_plugin(character_card.get("rulesetId"))
    except Exception:  # noqa: BLE001 - 插件异常按默认槽位处理
        return None

def _bounded_int(value, minimum, maximum, fallback):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))

def _initial_character_state(character_card):
    """按规则集派生的上限初始化状态。

    资源键（上限 / 当前值 / 初始值）由插件 ``resource_slots`` 声明，核心不写死
    HP / SAN / MP 字段名；理智从「初始值 = POW」开始，而不是直接拉满上限。
    """
    from trpg_server.ruleset.capabilities import resource_state_defaults

    card = _derive_character_card(character_card or {})
    state = dict(resource_state_defaults(_ruleset_plugin(card), card))
    state.update(
        {
            "injury_records": [],
            "sanity_records": [],
            "skill_changes": [],
            "item_changes": [],
            "major_wound": False,
            "unconscious": False,
            "dying": False,
            "dead": False,
            "first_aid_used": False,
        }
    )
    return state

def _ensure_character_state(member):
    from trpg_server.ruleset.capabilities import clamp_resource_state, derived_card_keys

    if not member.get("character_state"):
        member["character_state"] = _initial_character_state(member.get("character_card") or {})
    state = member["character_state"]
    state.setdefault("injury_records", [])
    state.setdefault("sanity_records", [])
    state.setdefault("skill_changes", [])
    state.setdefault("item_changes", [])
    state.setdefault("major_wound", False)
    state.setdefault("unconscious", False)
    state.setdefault("dying", False)
    state.setdefault("dead", False)
    state.setdefault("first_aid_used", False)
    # 上限与当前值以规则集派生结果为准，旧房间读取时同步修正。
    card = _derive_character_card(member.get("character_card") or {})
    plugin = _ruleset_plugin(card)
    snapshot = member.get("character_card")
    if card and isinstance(snapshot, dict):
        for key in derived_card_keys(plugin):
            if key in card:
                snapshot[key] = card[key]
    clamp_resource_state(plugin, card, state)
    return state

def _bind_character(member, character_card):
    sanitized = _sanitize_character_card(character_card)
    if not sanitized:
        return False
    member["character_card"] = sanitized
    member["character_state"] = _initial_character_state(sanitized)
    return True

def _recalculate_character_state(state):
    damage = sum(_bounded_int(record.get("value", record.get("damage")), 0, 999, 0) for record in state.get("injury_records", []))
    san_loss = sum(_bounded_int(record.get("value", record.get("loss")), 0, 999, 0) for record in state.get("sanity_records", []))
    state["current_hp"] = max(0, state["max_hp"] - damage)
    # 理智从「初始值 = POW」起算，而不是从上限（99 − 克苏鲁神话）起算。
    san_base = _bounded_int(state.get("initial_san"), 0, state["max_san"], state["max_san"])
    state["current_san"] = max(0, min(state["max_san"], san_base - san_loss))
