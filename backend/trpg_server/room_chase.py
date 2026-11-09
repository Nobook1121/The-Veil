"""房间级追逐会话：位置、行动点与阻碍的状态存取。

规则数学全部在 :mod:`trpg_server.ruleset.coc7.chase`；本模块只负责：
读取房间信息、维护 ``info["chase"]`` 会话段、搬运成员卡数值、留痕与落盘。

会话结构（只增字段，旧房间没有该段时视为没有追逐）::

    {
      "active": True,
      "round": 1,
      "locations": ["地点 1", ...],
      "obstacles": [{"from": 1, "name": "高围栏", "skill": "攀爬",
                     "difficulty": "regular", "durability": 10,
                     "max_durability": 10, "destroyed": False, "passed": False}],
      "participants": [{"name": "哈维", "role": "quarry", "mov": 5, "dex": 55,
                        "build": 0, "vehicle": False, "position": 2,
                        "ap": 1, "ap_debt": 0, "status": "active"}],
      "engaged": False,
      "outcome": "",
      "log": []
    }
"""
from __future__ import annotations

from typing import Any

from trpg_server import room_rules
from trpg_server.room_log import log_room_event
from trpg_server.ruleset.coc7 import chase as chase_rules

CHASE_KEY = "chase"
DEFAULT_LOCATION_COUNT = 6
MAX_LOG = 40


def _error(message: str, code: str = "chase_error") -> room_rules.RuleOperationError:
    return room_rules.RuleOperationError(message, code=code)


def get_chase(info: dict[str, Any] | None) -> dict[str, Any] | None:
    """读取当前追逐会话；没有、已结束或结构损坏时返回 ``None``。"""
    data = (info or {}).get(CHASE_KEY)
    if not isinstance(data, dict) or not data.get("active"):
        return None
    return data


def save_chase(room_dir: Any, info: dict[str, Any], chase: dict[str, Any]) -> None:
    info[CHASE_KEY] = chase
    room_rules.save_room_info(room_dir, info)


def _member_metrics(member: dict[str, Any]) -> dict[str, Any]:
    card = room_rules.member_card(member)
    derived = room_rules.derive_card(card)
    attributes = card.get("attributes") if isinstance(card.get("attributes"), dict) else {}

    def pick(key: str, fallback: int) -> int:
        try:
            value = derived.get(key)
            if value in (None, ""):
                value = card.get(key)
            return int(value)
        except (TypeError, ValueError):
            return fallback

    try:
        dex = int(attributes.get("dex", card.get("dex", 0)))
    except (TypeError, ValueError):
        dex = 0
    return {"mov": pick("mov", 6), "dex": dex, "build": pick("build", 0)}


def participant_from_member(member: dict[str, Any], *, role: str = "pursuer") -> dict[str, Any]:
    """从房间成员生成参与者条目；卡面缺省时按 MOV 6 / DEX 0 处理。"""
    metrics = _member_metrics(member)
    return {
        "name": room_rules.member_display_name(member),
        "ref": room_rules.member_ref(member),
        "member": True,
        "role": role,
        "mov": metrics["mov"],
        "dex": metrics["dex"],
        "build": metrics["build"],
        "vehicle": False,
        "position": 0,
        "ap": 1,
        "ap_debt": 0,
        "status": "active",
    }


def participant_from_spec(spec: dict[str, Any], *, role: str = "pursuer") -> dict[str, Any]:
    """NPC / 载具等非成员参与者：显式给 MOV / DEX / 体格。"""
    name = str(spec.get("name") or "").strip()
    if not name:
        raise _error("参与者需要 name", code="invalid_participant")

    def number(key: str, fallback: int) -> int:
        try:
            return int(spec.get(key))
        except (TypeError, ValueError):
            return fallback

    return {
        "name": name,
        "ref": name,
        "member": bool(spec.get("member")),
        "role": str(spec.get("role") or role),
        "mov": number("mov", 6),
        "dex": number("dex", 50),
        "build": number("build", 0),
        "vehicle": bool(spec.get("vehicle")),
        "position": 0,
        "ap": 1,
        "ap_debt": 0,
        "status": "active",
    }


def _log(chase: dict[str, Any], text: str, *, room_dir: Any = None, actor: Any = None) -> None:
    record = {"round": int(chase.get("round") or 1), "text": str(text)[:300]}
    items = chase.get("log")
    if not isinstance(items, list):
        items = []
    items.append(record)
    chase["log"] = items[-MAX_LOG:]
    if room_dir is not None:
        try:
            log_room_event(room_dir=room_dir, message=str(text), actor=actor, source="chase")
        except Exception:  # noqa: BLE001 - 留痕失败不能影响追逐结算
            pass


def _lowest_mov(chase: dict[str, Any]) -> int:
    return chase_rules.lowest_mov(chase.get("participants") or [])


def _refresh_ap(chase: dict[str, Any]) -> None:
    """按本轮最低 MOV 重算行动点，并扣除上一轮欠下的延迟消耗。"""
    lowest = _lowest_mov(chase)
    for item in chase.get("participants") or []:
        base = chase_rules.action_points(item.get("mov"), lowest)
        debt = max(0, int(item.get("ap_debt") or 0))
        item["ap"] = max(0, base - debt)
        item["ap_debt"] = max(0, debt - base)


def start_chase(
    room_dir: Any,
    info: dict[str, Any],
    participants: list[dict[str, Any]],
    *,
    locations: list[str] | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    """开始一场追逐：追逐者在 0、逃离者在初始距离 2（p.112-113）。"""
    if get_chase(info):
        raise _error("本房间已有进行中的追逐，请先结束当前追逐", code="chase_active")
    cleaned = [item for item in participants if isinstance(item, dict)]
    if len(cleaned) < 2:
        raise _error("追逐至少需要追逐者与逃离者各一方", code="invalid_participant")
    names = [str(item.get("name") or "") for item in cleaned]
    if len(set(names)) != len(names):
        raise _error("参与者名称不能重复", code="invalid_participant")
    track = [str(item) for item in (locations or []) if str(item).strip()]
    if not track:
        track = [f"地点 {index + 1}" for index in range(DEFAULT_LOCATION_COUNT)]
    if len(track) < 3:
        raise _error("追逐地点至少需要 3 个", code="invalid_track")

    quarries = [item for item in cleaned if item.get("role") == "quarry"]
    if not quarries:
        cleaned[0]["role"] = "quarry"
        quarries = [cleaned[0]]
    for item in cleaned:
        item["position"] = 0
        item["ap"] = 1
        item["ap_debt"] = 0
        item["status"] = "active"
    for item in quarries:
        item["position"] = min(chase_rules.INITIAL_DISTANCE, len(track) - 1)

    chase = {
        "active": True,
        "round": 1,
        "locations": track,
        "obstacles": [],
        "participants": cleaned,
        "engaged": False,
        "outcome": "",
        "log": [],
    }
    _refresh_ap(chase)
    _log(
        chase,
        f"追逐开始：{'、'.join(names)}；共 {len(track)} 个地点，初始距离 {chase_rules.INITIAL_DISTANCE}。",
        room_dir=room_dir,
        actor=actor,
    )
    save_chase(room_dir, info, chase)
    return chase


def start_round(room_dir: Any, info: dict[str, Any], *, actor: Any = None) -> dict[str, Any]:
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    chase["round"] = int(chase.get("round") or 1) + 1
    _refresh_ap(chase)
    _log(chase, f"第 {chase['round']} 轮开始，行动点已重置。", room_dir=room_dir, actor=actor)
    save_chase(room_dir, info, chase)
    return chase


def _participant(chase: dict[str, Any], name: Any) -> dict[str, Any]:
    needle = str(name or "").strip()
    for item in chase.get("participants") or []:
        if needle and needle in {str(item.get("name")), str(item.get("ref"))}:
            return item
    raise _error(f"参与者 {needle or '?'} 不在当前追逐中", code="unknown_participant")


def _obstacle_between(chase: dict[str, Any], start: int) -> dict[str, Any] | None:
    for item in chase.get("obstacles") or []:
        if int(item.get("from", -1)) == int(start):
            if item.get("passed") or item.get("destroyed"):
                continue
            return item
    return None


def _spend_ap(item: dict[str, Any], amount: int) -> None:
    """扣行动点；不足的部分记入下一轮的延迟消耗（p.114）。"""
    cost = max(0, int(amount))
    available = max(0, int(item.get("ap") or 0))
    if cost <= available:
        item["ap"] = available - cost
        return
    item["ap"] = 0
    item["ap_debt"] = max(0, int(item.get("ap_debt") or 0)) + (cost - available)


def _resolve_escape(chase: dict[str, Any]) -> None:
    """逃离者抵达轨道末端且与所有追逐者相距 ≥2 时脱身。"""
    track_end = len(chase.get("locations") or []) - 1
    for quarry in (item for item in chase.get("participants") or [] if item.get("role") == "quarry"):
        if int(quarry.get("position") or 0) < track_end:
            continue
        pursuers = [item for item in chase.get("participants") or [] if item.get("role") != "quarry"]
        if pursuers and all(track_end - int(item.get("position") or 0) >= chase_rules.INITIAL_DISTANCE for item in pursuers):
            chase["active"] = False
            chase["outcome"] = "escaped"
            _log(chase, f"{quarry.get('name')} 抵达轨道末端并甩开了追逐者，追逐结束。")
            return


def move_participant(
    room_dir: Any,
    info: dict[str, Any],
    name: Any,
    *,
    locations: Any = 1,
    accelerate: bool = False,
    actor: Any = None,
) -> dict[str, Any]:
    """移动 1 个地点（1 行动点）；载具可「猛踩油门」走 2-5 个地点（p.113、p.117）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    item = _participant(chase, name)
    if item.get("status") not in (None, "active"):
        raise _error(f"{item.get('name')} 当前无法行动（{item.get('status')}）", code="chase_inactive")

    if accelerate:
        if not item.get("vehicle"):
            raise _error("只有载具可以加速移动", code="not_vehicle")
        count = chase_rules.accelerate_locations(locations)
        cost = 1
    else:
        try:
            count = max(1, int(locations))
        except (TypeError, ValueError):
            count = 1
        cost = chase_rules.movement_cost(count)

    track_end = len(chase.get("locations") or []) - 1
    start = int(item.get("position") or 0)
    for step in range(start, min(start + count, track_end)):
        obstacle = _obstacle_between(chase, step)
        if obstacle is not None:
            raise _error(
                f"{obstacle.get('name') or '障碍'} 阻挡在前，需要先翻越或破坏",
                code="chase_blocked",
            )
    if int(item.get("ap") or 0) < cost:
        raise _error(
            f"{item.get('name')} 行动点不足（需要 {cost}，剩余 {int(item.get('ap') or 0)}）",
            code="chase_no_ap",
        )

    _spend_ap(item, cost)
    item["position"] = min(track_end, start + count)
    _log(
        chase,
        f"{item.get('name')} 前进 {count} 个地点（消耗 {cost} 行动点），现在位于 {chase['locations'][item['position']]}。",
        room_dir=room_dir,
        actor=actor,
    )
    _check_contact(chase)
    _resolve_escape(chase)
    save_chase(room_dir, info, chase)
    return chase


def _check_contact(chase: dict[str, Any]) -> None:
    quarries = [item for item in chase.get("participants") or [] if item.get("role") == "quarry"]
    pursuers = [item for item in chase.get("participants") or [] if item.get("role") != "quarry"]
    chase["engaged"] = any(
        int(quarry.get("position") or 0) == int(pursuer.get("position") or 0)
        for quarry in quarries
        for pursuer in pursuers
    )


def attempt_hazard(
    room_dir: Any,
    info: dict[str, Any],
    name: Any,
    *,
    success_level: Any,
    ap_spent: Any = 0,
    ap_loss: Any = 1,
    damage: Any = 0,
    note: str = "",
    actor: Any = None,
) -> dict[str, Any]:
    """结算一次险境：无论成败都前进 1 个地点；失败再损失 1D3 行动点（p.114）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    item = _participant(chase, name)
    spent = chase_rules.hazard_bonus_dice(ap_spent)
    if int(item.get("ap") or 0) < spent:
        raise _error("行动点不足以换取奖励骰", code="chase_no_ap")
    if spent:
        _spend_ap(item, spent)
    level = str(success_level or "").lower()
    success = level in {"regular", "hard", "extreme", "critical"}
    _spend_ap(item, chase_rules.movement_cost(1))
    track_end = len(chase.get("locations") or []) - 1
    item["position"] = min(track_end, int(item.get("position") or 0) + 1)
    payload = {
        "name": item.get("name"),
        "success": success,
        "success_level": level,
        "bonus_dice": spent,
        "damage": max(0, int(damage or 0)) if not success else 0,
        "ap_loss": 0,
        "position": item["position"],
        "note": str(note or ""),
    }
    if not success:
        loss = chase_rules.hazard_failure_ap_loss(ap_loss)
        _spend_ap(item, loss)
        payload["ap_loss"] = loss
    _log(
        chase,
        f"{item.get('name')} 通过险境{'成功' if success else '失败'}"
        + (f"（伤害 {payload['damage']}）" if payload["damage"] else "")
        + (f"（额外损失 {payload['ap_loss']} 行动点）" if payload["ap_loss"] else "")
        + f"，位于 {chase['locations'][item['position']]}。",
        room_dir=room_dir,
        actor=actor,
    )
    _check_contact(chase)
    _resolve_escape(chase)
    save_chase(room_dir, info, chase)
    return payload


def add_obstacle(
    room_dir: Any,
    info: dict[str, Any],
    *,
    start: Any,
    name: str = "障碍",
    skill: str = "",
    difficulty: str = "regular",
    durability: Any = None,
    kind: Any = None,
    actor: Any = None,
) -> dict[str, Any]:
    """在 ``start`` 与 ``start+1`` 地点之间放置一个障碍（p.115-116）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    try:
        index = int(start)
    except (TypeError, ValueError):
        raise _error("障碍需要起始地点序号", code="invalid_obstacle")
    track_end = len(chase.get("locations") or []) - 1
    if index < 0 or index >= track_end:
        raise _error(f"障碍序号必须在 0-{track_end - 1} 之间", code="invalid_obstacle")
    value = chase_rules.obstacle_durability(kind, durability)
    obstacle = {
        "from": index,
        "name": str(name or "障碍")[:40],
        "skill": str(skill or "")[:40],
        "difficulty": str(difficulty or "regular"),
        "durability": value,
        "max_durability": value,
        "destroyed": False,
        "passed": False,
    }
    chase.setdefault("obstacles", []).append(obstacle)
    _log(
        chase,
        f"在 {chase['locations'][index]} 与 {chase['locations'][index + 1]} 之间放置障碍：{obstacle['name']}（耐久 {value}）。",
        room_dir=room_dir,
        actor=actor,
    )
    save_chase(room_dir, info, chase)
    return obstacle


def attempt_obstacle(
    room_dir: Any,
    info: dict[str, Any],
    name: Any,
    *,
    start: Any,
    success_level: Any,
    damage: Any = 0,
    actor: Any = None,
) -> dict[str, Any]:
    """尝试翻越障碍：成功则通过并前进；失败停留在原地（p.115-116）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    item = _participant(chase, name)
    try:
        index = int(start)
    except (TypeError, ValueError):
        raise _error("障碍需要起始地点序号", code="invalid_obstacle")
    obstacle = _obstacle_between(chase, index)
    if obstacle is None:
        raise _error("该位置没有需要翻越的障碍", code="no_obstacle")
    if int(item.get("ap") or 0) < 1:
        raise _error(f"{item.get('name')} 行动点不足", code="chase_no_ap")
    _spend_ap(item, 1)
    level = str(success_level or "").lower()
    success = level in {"regular", "hard", "extreme", "critical"}
    if success:
        obstacle["passed"] = True
        track_end = len(chase.get("locations") or []) - 1
        item["position"] = min(track_end, int(item.get("position") or 0) + 1)
    _log(
        chase,
        f"{item.get('name')} {'翻越' if success else '未能翻越'}障碍 {obstacle['name']}"
        + (f"，受到 {int(damage or 0)} 点伤害" if damage else "")
        + "。",
        room_dir=room_dir,
        actor=actor,
    )
    _check_contact(chase)
    _resolve_escape(chase)
    save_chase(room_dir, info, chase)
    return {"obstacle": obstacle, "success": success, "position": item.get("position")}


def damage_obstacle(
    room_dir: Any,
    info: dict[str, Any],
    *,
    start: Any,
    damage: Any,
    actor: Any = None,
) -> dict[str, Any]:
    """破坏障碍：耐久归零后不再阻挡；载具撞毁后按耐久一半受反冲（p.115-116）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    try:
        index = int(start)
    except (TypeError, ValueError):
        raise _error("障碍需要起始地点序号", code="invalid_obstacle")
    obstacle = _obstacle_between(chase, index)
    if obstacle is None:
        raise _error("该位置没有需要破坏的障碍", code="no_obstacle")
    try:
        value = max(0, int(damage))
    except (TypeError, ValueError):
        value = 0
    before = int(obstacle.get("durability") or 0)
    obstacle["durability"] = max(0, before - value)
    if obstacle["durability"] == 0:
        obstacle["destroyed"] = True
    _log(
        chase,
        f"障碍 {obstacle['name']} 受到 {value} 点伤害，剩余耐久 {obstacle['durability']}/{obstacle.get('max_durability')}。",
        room_dir=room_dir,
        actor=actor,
    )
    save_chase(room_dir, info, chase)
    return {"obstacle": obstacle, "recoil": chase_rules.vehicle_recoil_damage(before)}


def resolve_conflict(
    room_dir: Any,
    info: dict[str, Any],
    attacker: Any,
    target: Any,
    *,
    has_firearm: bool = False,
    maneuver_success: bool = False,
    ap_loss: Any = 1,
    note: str = "",
    actor: Any = None,
) -> dict[str, Any]:
    """冲突门槛：同地点（或火器）才能攻击，攻击消耗 1 行动点（p.116）。"""
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    source = _participant(chase, attacker)
    victim = _participant(chase, target)
    if source is victim:
        raise _error("攻击者与目标不能是同一人", code="invalid_target")
    same = int(source.get("position") or 0) == int(victim.get("position") or 0)
    if not chase_rules.can_attack(same, has_firearm):
        raise _error("除火器外，只能攻击同一地点的目标", code="chase_out_of_range")
    if int(source.get("ap") or 0) < 1:
        raise _error(f"{source.get('name')} 行动点不足", code="chase_no_ap")
    _spend_ap(source, 1)
    payload = {
        "attacker": source.get("name"),
        "target": victim.get("name"),
        "same_location": same,
        "maneuver_success": bool(maneuver_success),
        "target_ap_loss": 0,
        "note": str(note or ""),
    }
    if maneuver_success:
        loss = chase_rules.conflict_ap_loss(ap_loss)
        _spend_ap(victim, loss)
        payload["target_ap_loss"] = loss
    _log(
        chase,
        f"{source.get('name')} 对 {victim.get('name')} 发起冲突"
        + (f"，目标损失 {payload['target_ap_loss']} 行动点" if payload["target_ap_loss"] else "")
        + "。",
        room_dir=room_dir,
        actor=actor,
    )
    save_chase(room_dir, info, chase)
    return payload


def end_chase(
    room_dir: Any,
    info: dict[str, Any],
    *,
    outcome: str = "",
    actor: Any = None,
) -> dict[str, Any]:
    chase = get_chase(info)
    if not chase:
        raise _error("当前没有进行中的追逐", code="chase_missing")
    chase["active"] = False
    chase["outcome"] = str(outcome or "ended")
    _log(chase, f"追逐结束（{chase['outcome']}）。", room_dir=room_dir, actor=actor)
    save_chase(room_dir, info, chase)
    return chase


def chase_snapshot(chase: dict[str, Any] | None) -> dict[str, Any] | None:
    """给房间快照 / 工具返回的精简结构；不暴露内部日志全文。"""
    if not isinstance(chase, dict):
        return None
    return {
        "round": int(chase.get("round") or 1),
        "locations": list(chase.get("locations") or []),
        "engaged": bool(chase.get("engaged")),
        "outcome": str(chase.get("outcome") or ""),
        "participants": [
            {
                "name": item.get("name"),
                "role": item.get("role"),
                "position": item.get("position"),
                "location": _location(chase, item.get("position")),
                "ap": item.get("ap"),
                "ap_debt": item.get("ap_debt"),
                "mov": item.get("mov"),
                "vehicle": bool(item.get("vehicle")),
            }
            for item in chase.get("participants") or []
        ],
        "obstacles": [
            {
                "from": item.get("from"),
                "name": item.get("name"),
                "skill": item.get("skill"),
                "difficulty": item.get("difficulty"),
                "durability": item.get("durability"),
                "destroyed": bool(item.get("destroyed")),
                "passed": bool(item.get("passed")),
            }
            for item in chase.get("obstacles") or []
        ],
    }


def _location(chase: dict[str, Any], position: Any) -> str:
    try:
        track = chase.get("locations") or []
        return str(track[int(position)])
    except (IndexError, TypeError, ValueError):
        return ""


def format_chase(chase: dict[str, Any] | None) -> str:
    """控制台 / 工具用的文字摘要。"""
    if not isinstance(chase, dict):
        return "当前没有进行中的追逐。"
    lines = [
        f"追逐第 {chase.get('round')} 轮 · {'进行中' if chase.get('active') else '已结束'}"
        + ("（已接触）" if chase.get("engaged") else ""),
        "地点：" + " → ".join(str(item) for item in chase.get("locations") or []),
    ]
    for item in chase.get("participants") or []:
        lines.append(
            f"  {item.get('name')}（{item.get('role')}）：{_location(chase, item.get('position'))}"
            f" · 行动点 {int(item.get('ap') or 0)}"
            + (f"（欠 {int(item.get('ap_debt') or 0)}）" if item.get("ap_debt") else "")
            + (f" · MOV {item.get('mov')}" if item.get("mov") is not None else "")
            + (" · 载具" if item.get("vehicle") else "")
        )
    obstacles = [
        item
        for item in chase.get("obstacles") or []
        if not item.get("destroyed") and not item.get("passed")
    ]
    for item in obstacles:
        lines.append(
            f"  障碍：{int(item.get('from') or 0) + 1}→{int(item.get('from') or 0) + 2} "
            f"{item.get('name')}（{item.get('skill') or '任意'} / {item.get('difficulty')}，"
            f"耐久 {item.get('durability')}/{item.get('max_durability')}）"
        )
    if chase.get("outcome"):
        lines.append(f"结果：{chase['outcome']}")
    return "\n".join(lines)
