"""5e 升级规划：plan 只读生成选择槽位，apply 一次性校验并落库。

数据全部来自 ``dnd5e-srd`` 的 ``progression`` / ``classes`` / ``spells`` 数据包；
核心只负责房间与角色卡的通用存取，不认识等级、法术位或专长。

契约
----
- 只支持从当前等级提升 1 级；越级、跨职业、未知职业都返回明确错误。
- plan 结果带服务端过期时间与角色卡指纹；apply 前重新读取角色卡比对指纹。
- apply 先完整校验所有槽位，全部合法后返回新 sheet；任何一项不合法都不写盘。
- 两档口径：默认（房间内的旧入口）只算属性提升 / 熟练加值 / 法术；卡面手动
  升级入口传 ``class_advancement=True``，额外生成生命值、职业特性、子职槽位，
  并保持 XP 不变（手动升级不按等级表补经验）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

from trpg_server.plugins import runtime

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")
ABILITY_LABELS = {
    "str": "力量",
    "dex": "敏捷",
    "con": "体质",
    "int": "智力",
    "wis": "感知",
    "cha": "魅力",
}
ASI_LEVELS = {4, 8, 12, 16, 19}
# SRD 5.1：属性提升不能把单项属性推过 20（史诗恩赐类能力不在本模块范围内）。
ABILITY_CAP = 20
MAX_SUBCLASS_NAME = 60
PLAN_TTL_SECONDS = 600
_WIZARD_SPELLS_PER_LEVEL = 2
_FEATS = (
    {
        "value": "feat:grappler",
        "label": "专长：Grappler（擒抱者）",
        "detail": {"feat": "grappler", "source": "SRD 5.1"},
    },
)

_PLANS: dict[str, dict[str, Any]] = {}
_PLAN_LOCK = threading.RLock()


class ProgressionError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _sheet_of(card: Mapping[str, Any]) -> dict[str, Any]:
    sheet = card.get("sheet") if isinstance(card.get("sheet"), Mapping) else {}
    if sheet:
        return copy.deepcopy(dict(sheet))
    keys = (
        "level",
        "class",
        "subclass",
        "race",
        "background",
        "alignment",
        "xp",
        "abilities",
        "attributes",
        "proficiencies",
        "features",
        "spellcasting",
        "hp",
        "speed",
        "armorClass",
        "equipment",
        "notes",
    )
    return {key: copy.deepcopy(card[key]) for key in keys if key in card}


def _entry_names(entry: Mapping[str, Any]) -> set[str]:
    names = {str(entry.get("id") or ""), str(entry.get("name") or "")}
    locales = entry.get("locales") if isinstance(entry.get("locales"), Mapping) else {}
    for value in locales.values():
        if isinstance(value, Mapping) and value.get("name"):
            names.add(str(value["name"]))
    return {name.strip().casefold() for name in names if name and name.strip()}


def _localized_name(entry: Mapping[str, Any]) -> str:
    locales = entry.get("locales") if isinstance(entry.get("locales"), Mapping) else {}
    zh = locales.get("zh-CN") if isinstance(locales.get("zh-CN"), Mapping) else {}
    name = str(zh.get("name") or "").strip()
    return name or str(entry.get("name") or entry.get("id") or "")


def _spell_label(entry: Mapping[str, Any]) -> str:
    base = str(entry.get("name") or entry.get("id") or "")
    zh = _localized_name(entry)
    if zh and zh.casefold() != base.casefold():
        base = f"{base}（{zh}）"
    level = _as_int(entry.get("level"), 0)
    return f"{base} · {'戏法' if level <= 0 else f'{level} 环'}"


def _fingerprint(sheet: Mapping[str, Any]) -> str:
    raw = json.dumps(sheet, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_class(library: Any, sheet: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(sheet.get("class") or "").strip()
    if not raw:
        raise ProgressionError(
            "progression.class_missing",
            "角色卡没有填写职业，无法生成升级计划。",
        )
    entries = library.entries("dnd5e", "class")
    for entry in entries:
        if raw.casefold() in _entry_names(entry):
            return entry
    raise ProgressionError(
        "progression.class_unknown",
        f"数据包里找不到职业「{raw}」；当前可用职业："
        + "、".join(str(item.get("name") or item.get("id")) for item in entries)
        + "。",
    )


def _progression_row(library: Any, level: int) -> dict[str, Any]:
    for row in library.entries("dnd5e", "progression"):
        if _as_int(row.get("level"), 0) == level:
            return row
    raise ProgressionError("progression.level_unknown", f"数据包缺少 {level} 级的熟练与经验表。")


def _hit_die(class_entry: Mapping[str, Any]) -> int:
    """职业生命骰面数；数据包缺失按 d8 兜底。"""
    die = _as_int(class_entry.get("hit_die"), 8)
    return die if 4 <= die <= 20 else 8


def _feature_rows(class_entry: Mapping[str, Any], level: int) -> list[dict[str, Any]]:
    """职业表在该等级授予的特性行（classes.json 的 features 表）。"""
    rows = class_entry.get("features")
    granted: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or _as_int(row.get("level"), 0) != level:
            continue
        granted.append(
            {
                "id": str(row.get("id") or ""),
                "level": level,
                "kind": str(row.get("kind") or "feature"),
                "name": str(row.get("name") or ""),
                "label": _localized_name(row) or str(row.get("name") or ""),
            }
        )
    return granted


def _subclass_candidates(class_entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """子职候选：classes.json 的 subclasses 表（SRD 5.1 每个职业只列一条）。"""
    rows = class_entry.get("subclasses")
    candidates: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        base = str(row.get("name") or row.get("id") or "")
        zh = _localized_name(row)
        label = f"{base}（{zh}）" if zh and zh.casefold() != base.casefold() else base
        candidates.append(
            {
                "value": str(row.get("id") or base),
                "label": label,
                "detail": {"name": base, "label_zh": zh, "source": "SRD 5.1"},
            }
        )
    return candidates


def _hit_points_block(
    sheet: Mapping[str, Any], class_entry: Mapping[str, Any], roll: int | None = None
) -> dict[str, Any]:
    """升级生命值：掷骰（服务端真随机）与平均值两个候选，都加 CON 调整值。"""
    die = _hit_die(class_entry)
    rolled = roll if isinstance(roll, int) and 1 <= roll <= die else secrets.randbelow(die) + 1
    average = die // 2 + 1
    con = _ability_modifier(sheet, "con")
    hp = sheet.get("hp") if isinstance(sheet.get("hp"), Mapping) else {}
    before_max = max(1, _as_int(hp.get("max"), 1))
    return {
        "die": die,
        "roll": rolled,
        "average": average,
        "con_modifier": con,
        "roll_total": max(1, rolled + con),
        "average_total": max(1, average + con),
        "before_max": before_max,
        "before_current": max(0, _as_int(hp.get("current"), before_max)),
    }


def _hit_points_slot(
    sheet: Mapping[str, Any], class_entry: Mapping[str, Any], level: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    block = _hit_points_block(sheet, class_entry)
    die = block["die"]
    con = block["con_modifier"]
    con_note = f"，CON {con:+d}" if con else ""
    candidates = [
        {
            "value": "roll",
            "label": f"掷生命骰 1d{die} = {block['roll']}{con_note} → +{block['roll_total']}",
            "detail": {
                "die": die,
                "roll": block["roll"],
                "con_modifier": con,
                "total": block["roll_total"],
            },
        },
        {
            "value": "average",
            "label": f"取平均值（{die}/2+1）= {block['average']}{con_note} → +{block['average_total']}",
            "detail": {
                "die": die,
                "roll": block["average"],
                "con_modifier": con,
                "total": block["average_total"],
                "averaged": True,
            },
        },
    ]
    slot = _slot(
        f"level-{level}:hp",
        "hp",
        level,
        "生命值上限",
        candidates=candidates,
        required=True,
        min_picks=1,
        max_picks=1,
        default="average",
        detail={
            "die": die,
            "con_modifier": con,
            "before_max": block["before_max"],
            "before_current": block["before_current"],
        },
        not_selected="生命值必须选择「掷骰」或「平均值」其中一项。",
    )
    return slot, block


def _slot_table_row(class_entry: Mapping[str, Any], level: int) -> list[int]:
    spellcasting = class_entry.get("spellcasting")
    table = spellcasting.get("slot_table") if isinstance(spellcasting, Mapping) else None
    if not isinstance(table, list) or not table:
        return []
    row = table[min(max(level, 1), 20) - 1]
    return [_as_int(item, 0) for item in row] if isinstance(row, list) else []


def _slots_from_row(row: list[int], previous: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    saved = previous.get("slots") if isinstance(previous.get("slots"), Mapping) else {}
    slots: dict[str, dict[str, int]] = {}
    for index, maximum in enumerate(row):
        if maximum <= 0:
            continue
        key = str(index + 1)
        declared = saved.get(key) if isinstance(saved.get(key), Mapping) else {}
        used = max(0, min(maximum, _as_int(declared.get("used"), 0)))
        slots[key] = {"max": maximum, "used": used}
    return slots


def _max_spell_level(row: list[int]) -> int:
    for index in range(len(row) - 1, -1, -1):
        if row[index] > 0:
            return index + 1
    return 0


def _spell_candidates(
    library: Any, class_id: str, max_level: int, *, exclude: set[str]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for entry in library.entries("dnd5e", "spell"):
        classes = entry.get("classes") or []
        if class_id not in classes:
            continue
        if _as_int(entry.get("level"), 0) > max_level:
            continue
        names = _entry_names(entry)
        if names & exclude:
            continue
        candidates.append(
            {
                "value": str(entry.get("id") or ""),
                "label": _spell_label(entry),
                "detail": {
                    "level": _as_int(entry.get("level"), 0),
                    "school": str(entry.get("school") or ""),
                },
            }
        )
    candidates.sort(
        key=lambda item: (
            _as_int((item.get("detail") or {}).get("level"), 0),
            str(item.get("label") or ""),
        )
    )
    return candidates


def _known_names(sheet: Mapping[str, Any]) -> set[str]:
    raw = sheet.get("spellcasting")
    raw = raw if isinstance(raw, Mapping) else {}
    values = raw.get("known") or raw.get("spells_known") or sheet.get("spellsKnown") or []
    return {str(item).strip().casefold() for item in values if str(item).strip()}


def _prepared_names(sheet: Mapping[str, Any]) -> list[str]:
    raw = sheet.get("spellcasting")
    raw = raw if isinstance(raw, Mapping) else {}
    values = raw.get("prepared") or raw.get("spells_prepared") or []
    return [str(item) for item in values if str(item).strip()]


def _slot(
    slot_id: str,
    kind: str,
    level: int,
    title: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    required: bool = False,
    min_picks: int = 0,
    max_picks: int = 0,
    default: Any = None,
    detail: dict[str, Any] | None = None,
    not_selected: str = "",
) -> dict[str, Any]:
    return {
        "id": slot_id,
        "kind": kind,
        "level": level,
        "title": title,
        "required": required,
        "blocking": required,
        "min_picks": min_picks,
        "max_picks": max_picks,
        "candidates": candidates or [],
        "default": default,
        "detail": detail or {},
        "not_selected": not_selected
        or ("不选则本次升级无法完成。" if required else "不选则保持当前值。"),
    }


def create_plan(
    card: Mapping[str, Any],
    target_level: Any,
    *,
    owner_key: str,
    target_user_id: str = "",
    class_id: str = "",
    class_advancement: bool = False,
) -> dict[str, Any]:
    """生成升级计划。

    ``class_advancement=False``（默认）保持房间内旧入口的口径：属性提升 / 熟练
    加值 / 法术位与法术。``class_advancement=True`` 是卡面手动升级入口：额外给出
    生命值槽位（掷骰 / 平均值）、该等级的职业特性、子职选择，并把 XP 冻结在升级
    前的值（手动升级不按等级表补经验）。
    """
    library = runtime.content_packs_for("dnd5e")
    sheet = _sheet_of(card)
    current = _as_int(sheet.get("level"), 1)
    target = _as_int(target_level, 0)
    if target != current + 1:
        raise ProgressionError(
            "progression.level_jump",
            f"一次只能提升一级：当前 {current} 级，目标必须是 {current + 1} 级（收到 {target or '空'}）。",
        )
    if target > 20:
        raise ProgressionError("progression.level_cap", "角色等级不能超过 20 级。")
    class_entry = _resolve_class(library, sheet)
    resolved_class = str(class_entry.get("id") or "")
    requested = str(class_id or "").strip()
    if requested and requested.casefold() not in _entry_names(class_entry):
        raise ProgressionError(
            "progression.class_mismatch",
            f"角色当前职业是「{class_entry.get('name')}」，不能按「{requested}」升级；暂不支持跨职业。",
        )

    slots: list[dict[str, Any]] = []
    warnings: list[str] = []
    row = _progression_row(library, target)
    current_row = _progression_row(library, current)
    xp_before = _as_int(sheet.get("xp"), 0)
    # 手动升级入口不补 XP：升级后经验值保持原值，只有房间内的旧口径才对齐等级表。
    xp_after = xp_before if class_advancement else _as_int(row.get("xp"), 0)

    granted = _feature_rows(class_entry, target)
    subclass_level = _as_int(class_entry.get("subclass_level"), 0)
    subclass_now = str(sheet.get("subclass") or "").strip()
    hit_points: dict[str, Any] = {}

    if class_advancement:
        hp_slot, hit_points = _hit_points_slot(sheet, class_entry, target)
        slots.append(hp_slot)

    if target in ASI_LEVELS:
        abilities_before = sheet.get("abilities") or sheet.get("attributes") or {}
        abilities_before = abilities_before if isinstance(abilities_before, Mapping) else {}
        ability_candidates = [
            {
                "value": key,
                "label": (
                    f"{ABILITY_LABELS[key]} {_as_int(abilities_before.get(key), 10)}"
                    f" → {min(ABILITY_CAP, _as_int(abilities_before.get(key), 10) + 2)}（+2）"
                ),
                "detail": {
                    "ability": key,
                    "current": _as_int(abilities_before.get(key), 10),
                    "cap": ABILITY_CAP,
                },
            }
            for key in ABILITY_KEYS
        ]
        slots.append(
            _slot(
                f"level-{target}:asi",
                "asi",
                target,
                "属性提升或专长",
                candidates=ability_candidates + [dict(item) for item in _FEATS],
                required=True,
                min_picks=1,
                max_picks=2,
                not_selected="属性提升是必选项；不选则无法完成升级。",
            )
        )

    if subclass_level:
        label = str(
            class_entry.get("subclass_name_zh")
            or class_entry.get("subclass_name")
            or "子职"
        )
        if not subclass_now and target == subclass_level:
            slots.append(
                _slot(
                    f"level-{target}:subclass",
                    "subclass",
                    target,
                    f"{label}（子职）",
                    candidates=_subclass_candidates(class_entry),
                    required=True,
                    min_picks=1,
                    max_picks=1,
                    detail={
                        "subclass_level": subclass_level,
                        "allow_custom": True,
                        "source": "dnd5e-srd/classes",
                    },
                    not_selected=f"本职业在 {subclass_level} 级必须选定{label}。",
                )
            )
        elif not subclass_now and target > subclass_level:
            warnings.append(
                f"角色卡还没有子职：{class_entry.get('name')} 按 SRD 应在 {subclass_level} 级选定{label}，"
                "本次升级不会补选（可在卡面直接填写，或先补到该等级）。"
            )

    pb_before = _as_int(current_row.get("proficiency_bonus"), 2)
    pb_after = _as_int(row.get("proficiency_bonus"), pb_before)
    if pb_after != pb_before:
        slots.append(
            _slot(
                f"level-{target}:proficiency",
                "proficiency_bonus",
                target,
                "熟练加值",
                candidates=[
                    {
                        "value": "apply",
                        "label": f"熟练加值 +{pb_before} → +{pb_after}",
                    }
                ],
                default="apply",
                detail={"from": pb_before, "to": pb_after},
            )
        )

    spellcasting = class_entry.get("spellcasting")
    spellcasting = spellcasting if isinstance(spellcasting, Mapping) else {}
    row_slots = _slot_table_row(class_entry, target)
    if row_slots:
        casting = sheet.get("spellcasting")
        casting = casting if isinstance(casting, Mapping) else {}
        before_slots = _slots_at_level(casting, class_entry, current)
        after_slots = _slots_from_row(row_slots, casting)
        if after_slots != before_slots:
            slots.append(
                _slot(
                    f"level-{target}:spell_slots",
                    "spell_slots",
                    target,
                    "法术位",
                    candidates=[{"value": "apply", "label": "按职业表更新法术位上限"}],
                    default="apply",
                    detail={"before": before_slots, "after": after_slots},
                )
            )
        max_level = _max_spell_level(row_slots)
        if spellcasting.get("spellbook") is True and max_level > 0:
            candidates = _spell_candidates(
                library,
                resolved_class,
                max_level,
                exclude=_known_names(sheet),
            )
            picks = min(_WIZARD_SPELLS_PER_LEVEL, len(candidates))
            if len(candidates) < _WIZARD_SPELLS_PER_LEVEL:
                warnings.append("可用新法术少于 2 个，法术书本次无法按常规增加 2 个法术。")
            slots.append(
                _slot(
                    f"level-{target}:spell_known",
                    "spell_known",
                    target,
                    "法术书新增法术",
                    candidates=candidates,
                    required=bool(candidates),
                    min_picks=picks,
                    max_picks=picks,
                    not_selected="法术书升级必须选择新法术；不选则无法完成升级。",
                )
            )
        elif max_level > 0:
            prepared = _prepared_names(sheet)
            candidates = _spell_candidates(
                library, resolved_class, max_level, exclude=set()
            )
            capacity = max(1, _ability_modifier(sheet, str(spellcasting.get("ability") or "")) + target)
            slots.append(
                _slot(
                    f"level-{target}:spell_prepared",
                    "spell_prepared",
                    target,
                    "准备法术",
                    candidates=candidates,
                    min_picks=0,
                    max_picks=capacity,
                    default=prepared,
                    detail={"capacity": capacity, "before": prepared},
                    not_selected="不选则保持当前准备列表；长休时仍可重新准备。",
                )
            )

    if not granted:
        warnings.append("该等级没有新获得职业特性（职业表在 classes.json）。")
    if class_advancement:
        if hit_points:
            warnings.append(
                f"生命值按 1d{hit_points['die']} + CON 调整值结算，升级后经验值保持不变（手动升级入口）。"
            )
    else:
        warnings.append("生命值上限未自动结算，请按职业生命骰另行处理。")

    plan_id = f"plan-{uuid4().hex}"
    expires_epoch = time.time() + PLAN_TTL_SECONDS
    plan = {
        "plan_id": plan_id,
        "ruleset": "dnd5e",
        "class": resolved_class,
        "class_name": _localized_name(class_entry),
        "current_level": current,
        "target_level": target,
        "xp": {
            "before": xp_before,
            "after": xp_after,
            "manual": bool(class_advancement),
        },
        # 卡面升级入口用：HP 候选与职业表命中项（不含选择槽位的纯展示信息）。
        "advancement": bool(class_advancement),
        "hit_die": _hit_die(class_entry),
        "hit_points": hit_points,
        "features": granted,
        "subclass": {
            "level": subclass_level,
            "current": subclass_now,
            "label": str(class_entry.get("subclass_name_zh") or class_entry.get("subclass_name") or ""),
            "options": _subclass_candidates(class_entry),
            "level_due": bool(subclass_level and not subclass_now and target == subclass_level),
        },
        "slots": slots,
        "warnings": warnings,
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_epoch)),
        "expires_in": PLAN_TTL_SECONDS,
        "source": {
            "progression": "dnd5e-srd/progression",
            "classes": "dnd5e-srd/classes",
        },
    }
    record = {
        "plan": plan,
        "fingerprint": _fingerprint(sheet),
        "owner_key": str(owner_key or ""),
        "target_user_id": str(target_user_id or ""),
        "expires_epoch": expires_epoch,
    }
    with _PLAN_LOCK:
        _cleanup_expired()
        _PLANS[plan_id] = record
    return copy.deepcopy(plan)


def _slots_at_level(
    casting: Mapping[str, Any], class_entry: Mapping[str, Any], level: int
) -> dict[str, dict[str, int]]:
    """当前等级的法术位：卡面显式值优先，否则按职业表。"""
    row = _slot_table_row(class_entry, level)
    saved = casting.get("slots") if isinstance(casting.get("slots"), Mapping) else {}
    slots: dict[str, dict[str, int]] = {}
    for index in range(max(len(row), len(saved))):
        key = str(index + 1)
        declared = saved.get(key) if isinstance(saved.get(key), Mapping) else {}
        maximum = _as_int(declared.get("max"), row[index] if index < len(row) else 0)
        if maximum <= 0:
            continue
        slots[key] = {
            "max": maximum,
            "used": max(0, min(maximum, _as_int(declared.get("used"), 0))),
        }
    return slots


def _ability_modifier(sheet: Mapping[str, Any], ability: str) -> int:
    abilities = sheet.get("abilities") or sheet.get("attributes") or {}
    score = _as_int((abilities or {}).get(ability), 10) if isinstance(abilities, Mapping) else 10
    return (score - 10) // 2


def plan_target(plan_id: str) -> str:
    with _PLAN_LOCK:
        record = _PLANS.get(str(plan_id or ""))
        return str((record or {}).get("target_user_id") or "")


def _cleanup_expired() -> None:
    # 过期计划保留一天，让 apply 能返回明确的「已过期」而不是「不存在」。
    horizon = time.time() - 86400
    for key in [key for key, value in _PLANS.items() if value.get("expires_epoch", 0) <= horizon]:
        _PLANS.pop(key, None)


def _choice_map(choices: Any) -> dict[str, list[str]]:
    if choices is None:
        return {}
    if not isinstance(choices, list):
        raise ProgressionError("progression.invalid_choices", "choices 必须是数组。")
    result: dict[str, list[str]] = {}
    for item in choices:
        if not isinstance(item, Mapping):
            raise ProgressionError("progression.invalid_choices", "每个选项必须是对象。")
        slot_id = str(item.get("slot_id") or "").strip()
        if not slot_id:
            raise ProgressionError("progression.invalid_choices", "选项缺少 slot_id。")
        if slot_id in result:
            raise ProgressionError(
                "progression.invalid_choices", f"槽位「{slot_id}」重复提交。"
            )
        values = item.get("values")
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ProgressionError(
                "progression.invalid_choices", f"槽位「{slot_id}」的 values 必须是数组。"
            )
        result[slot_id] = [str(value).strip() for value in values if str(value).strip()]
    return result


def _apply_asi(sheet: dict[str, Any], slot: Mapping[str, Any], values: list[str]) -> None:
    allowed = {str(item.get("value")) for item in slot.get("candidates") or []}
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise ProgressionError(
            "progression.invalid_option",
            f"属性提升不支持：{'、'.join(invalid)}。",
        )
    feat_values = [value for value in values if value.startswith("feat:")]
    if feat_values:
        if len(values) != 1:
            raise ProgressionError(
                "progression.invalid_option", "选择专长时不能再同时提升属性。"
            )
        feat = next(
            item for item in slot.get("candidates") or []
            if str(item.get("value")) == feat_values[0]
        )
        features = sheet.setdefault("features", [])
        if not isinstance(features, list):
            features = []
            sheet["features"] = features
        label = str((feat.get("detail") or {}).get("feat") or feat_values[0])
        if label not in features:
            features.append(label)
        return
    if not values or len(values) > 2:
        raise ProgressionError(
            "progression.invalid_option", "属性提升需要选择 1 项（+2）或 2 项不同属性（各 +1）。"
        )
    if len(set(values)) != len(values):
        raise ProgressionError("progression.invalid_option", "属性提升不能重复选择同一属性。")
    abilities = sheet.get("abilities")
    if not isinstance(abilities, dict):
        abilities = dict(sheet.get("attributes") or {})
        sheet["abilities"] = abilities
    amount = 2 if len(values) == 1 else 1
    # 先整体校验上限（SRD：属性提升不能把单项推过 20），任何一项越界都不改卡。
    overflow = [
        f"{ABILITY_LABELS.get(key, key)} {_as_int(abilities.get(key), 10)}"
        for key in values
        if _as_int(abilities.get(key), 10) + amount > ABILITY_CAP
    ]
    if overflow:
        raise ProgressionError(
            "progression.ability_cap",
            f"属性提升上限是 {ABILITY_CAP}：{'、'.join(overflow)} 再加 +{amount} 会超过上限。",
        )
    for key in values:
        abilities[key] = _as_int(abilities.get(key), 10) + amount


def _apply_hit_points(
    sheet: dict[str, Any], slot: Mapping[str, Any], values: list[str], level: int
) -> None:
    """升级生命值：上限 + 掷骰/平均值 + CON；当前 HP 同步加同额（5e 常规口径）。"""
    if len(values) != 1:
        raise ProgressionError(
            "progression.invalid_option", "生命值需要选择「掷骰」或「平均值」其中一项。"
        )
    candidate = next(
        (
            item
            for item in slot.get("candidates") or []
            if str(item.get("value")) == values[0]
        ),
        None,
    )
    if candidate is None:
        raise ProgressionError(
            "progression.invalid_option", f"生命值不支持：{'、'.join(values)}。"
        )
    detail = candidate.get("detail") or {}
    gain = max(1, _as_int(detail.get("total"), 0))
    hp = sheet.get("hp") if isinstance(sheet.get("hp"), dict) else {}
    before_max = max(1, _as_int(hp.get("max"), 1))
    before_current = max(0, min(before_max, _as_int(hp.get("current"), before_max)))
    hp["max"] = before_max + gain
    hp["current"] = min(hp["max"], before_current + gain)
    hp["temp"] = max(0, _as_int(hp.get("temp"), 0))
    sheet["hp"] = hp
    # 生命骰池 +1（rest 模块读 sheet.hit_dice；首刀卡面也按这个形状显示）。
    die = _as_int(detail.get("die"), 8) or 8
    dice = sheet.get("hit_dice")
    dice = dict(dice) if isinstance(dice, Mapping) else {}
    dice["die"] = die
    dice["max"] = _as_int(dice.get("max"), max(0, level - 1)) + 1
    dice["used"] = max(0, min(dice["max"], _as_int(dice.get("used"), 0)))
    sheet["hit_dice"] = dice


def _apply_subclass(
    sheet: dict[str, Any], slot: Mapping[str, Any], values: list[str]
) -> None:
    """子职落地：命中职业表候选就用中文名，否则接受手填（深库/非 SRD 子职）。"""
    if len(values) != 1:
        raise ProgressionError("progression.invalid_option", "子职只能选择一项。")
    raw = values[0].strip()
    if not raw:
        raise ProgressionError("progression.invalid_option", "子职不能为空。")
    candidate = next(
        (
            item
            for item in slot.get("candidates") or []
            if str(item.get("value")) == raw
        ),
        None,
    )
    if candidate is not None:
        detail = candidate.get("detail") or {}
        label = str(detail.get("label_zh") or detail.get("name") or candidate.get("label") or raw)
    else:
        if len(raw) > MAX_SUBCLASS_NAME:
            raise ProgressionError(
                "progression.invalid_option",
                f"子职名称最多 {MAX_SUBCLASS_NAME} 个字符。",
            )
        label = raw
    sheet["subclass"] = label


def apply_plan(plan_id: str, choices: Any, card: Mapping[str, Any]) -> dict[str, Any]:
    with _PLAN_LOCK:
        _cleanup_expired()
        record = _PLANS.get(str(plan_id or ""))
        if record is None:
            raise ProgressionError(
                "progression.plan_not_found",
                "升级计划不存在或已使用，请重新生成。",
                404,
            )
        if record.get("expires_epoch", 0) <= time.time():
            raise ProgressionError(
                "progression.plan_expired", "升级计划已过期，请重新生成。", 410
            )
        if record.get("claimed"):
            raise ProgressionError(
                "progression.plan_busy", "该计划正在应用中，请勿重复提交。", 409
            )
    sheet = _sheet_of(card)
    if _fingerprint(sheet) != record.get("fingerprint"):
        raise ProgressionError(
            "progression.plan_stale",
            "角色卡在计划生成后已变化，请重新生成升级计划。",
            409,
        )
    plan = record["plan"]
    slot_map = {slot["id"]: slot for slot in plan.get("slots") or []}
    picked = _choice_map(choices)
    unknown = sorted(set(picked) - set(slot_map))
    if unknown:
        raise ProgressionError(
            "progression.unknown_slot",
            f"计划中没有这些槽位：{'、'.join(unknown)}。",
        )
    missing = [
        slot["title"]
        for slot in plan.get("slots") or []
        if slot.get("required") and slot["id"] not in picked
    ]
    if missing:
        raise ProgressionError(
            "progression.missing_choice",
            f"还缺少必选槽位：{'、'.join(missing)}。",
        )

    new_sheet = copy.deepcopy(sheet)
    # 施法块按需写入：非施法职业升级后不该凭空多出一个空 spellcasting。
    casting = new_sheet.get("spellcasting")
    casting = casting if isinstance(casting, dict) else {}

    for slot in plan.get("slots") or []:
        kind = slot.get("kind")
        values = picked.get(slot.get("id"))
        if values is None:
            if kind == "spell_prepared":
                continue
            if kind == "spell_slots" and slot.get("default"):
                casting["slots"] = copy.deepcopy((slot.get("detail") or {}).get("after") or {})
                new_sheet["spellcasting"] = casting
            continue
        if kind == "asi":
            _apply_asi(new_sheet, slot, values)
        elif kind == "hp":
            _apply_hit_points(
                new_sheet, slot, values, _as_int(plan.get("target_level"), 1)
            )
        elif kind == "subclass":
            _apply_subclass(new_sheet, slot, values)
        elif kind == "proficiency_bonus":
            if values and values != ["apply"]:
                raise ProgressionError("progression.invalid_option", "熟练加值槽位不接受其它选项。")
        elif kind == "spell_slots":
            if values and values != ["apply"]:
                raise ProgressionError("progression.invalid_option", "法术位槽位不接受其它选项。")
            casting["slots"] = copy.deepcopy((slot.get("detail") or {}).get("after") or {})
            new_sheet["spellcasting"] = casting
        elif kind in {"spell_known", "spell_prepared"}:
            allowed = {str(item.get("value")) for item in slot.get("candidates") or []}
            invalid = [value for value in values if value not in allowed]
            if invalid:
                raise ProgressionError(
                    "progression.invalid_option",
                    f"{slot.get('title')}不支持：{'、'.join(invalid)}。",
                )
            maximum = _as_int(slot.get("max_picks"), 0)
            if len(values) > maximum:
                raise ProgressionError(
                    "progression.invalid_option",
                    f"{slot.get('title')}最多选择 {maximum} 项。",
                )
            if kind == "spell_known":
                if len(values) != maximum:
                    raise ProgressionError(
                        "progression.invalid_option",
                        f"{slot.get('title')}需要选择 {maximum} 项。",
                    )
                known = casting.get("known")
                known = list(known) if isinstance(known, list) else []
                for value in values:
                    if value not in known:
                        known.append(value)
                casting["known"] = known
            else:
                casting["prepared"] = list(values)
            new_sheet["spellcasting"] = casting
        else:
            raise ProgressionError(
                "progression.unknown_slot_kind",
                f"不认识的槽位类型：{kind}。",
            )

    if plan.get("advancement"):
        # 职业表在该等级授予的特性：没有选择项，直接并入 features（去重）。
        features = new_sheet.get("features")
        features = list(features) if isinstance(features, list) else []
        for item in plan.get("features") or []:
            if str(item.get("kind") or "") == "subclass":
                continue
            label = str(item.get("label") or item.get("name") or "").strip()
            if label and label not in features:
                features.append(label)
        new_sheet["features"] = features

    new_sheet["level"] = _as_int(plan.get("target_level"), new_sheet.get("level", 1))
    if (plan.get("xp") or {}).get("manual"):
        # 手动升级入口：经验值原样保留，不按等级表补齐。
        new_sheet["xp"] = _as_int(new_sheet.get("xp"), 0)
    else:
        new_sheet["xp"] = max(
            _as_int(new_sheet.get("xp"), 0),
            _as_int((plan.get("xp") or {}).get("after"), 0),
        )
    before = _summary(sheet)
    after = _summary(new_sheet)
    changes = _changes(sheet, new_sheet, plan)
    with _PLAN_LOCK:
        # 校验全部通过后才占用计划；落库成功由调用方 commit，失败可 abort。
        record["claimed"] = True
    return {
        "plan_id": str(plan_id),
        "sheet": new_sheet,
        "before": before,
        "after": after,
        "changes": changes,
    }


def commit_plan(plan_id: str) -> None:
    with _PLAN_LOCK:
        _PLANS.pop(str(plan_id or ""), None)


def abort_plan(plan_id: str) -> None:
    with _PLAN_LOCK:
        record = _PLANS.get(str(plan_id or ""))
        if record is not None:
            record["claimed"] = False


def _summary(sheet: Mapping[str, Any]) -> dict[str, Any]:
    casting = sheet.get("spellcasting")
    casting = casting if isinstance(casting, Mapping) else {}
    hp = sheet.get("hp") if isinstance(sheet.get("hp"), Mapping) else {}
    dice = sheet.get("hit_dice") if isinstance(sheet.get("hit_dice"), Mapping) else {}
    return {
        "level": _as_int(sheet.get("level"), 1),
        "xp": _as_int(sheet.get("xp"), 0),
        "class": str(sheet.get("class") or ""),
        "subclass": str(sheet.get("subclass") or ""),
        "hp": copy.deepcopy(hp),
        "hit_dice": copy.deepcopy(dice),
        "abilities": dict(sheet.get("abilities") or sheet.get("attributes") or {}),
        "features": list(sheet.get("features") or []),
        "proficiencies": copy.deepcopy(sheet.get("proficiencies") or {}),
        "spellcasting": {
            "known": list(casting.get("known") or casting.get("spells_known") or []),
            "prepared": list(casting.get("prepared") or casting.get("spells_prepared") or []),
            "slots": copy.deepcopy(casting.get("slots") or {}),
        },
    }


def _changes(
    before: Mapping[str, Any], after: Mapping[str, Any], plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if _as_int(before.get("level"), 1) != _as_int(after.get("level"), 1):
        changes.append(
            {
                "field": "level",
                "before": _as_int(before.get("level"), 1),
                "after": _as_int(after.get("level"), 1),
            }
        )
    if _as_int(before.get("xp"), 0) != _as_int(after.get("xp"), 0):
        changes.append(
            {
                "field": "xp",
                "before": _as_int(before.get("xp"), 0),
                "after": _as_int(after.get("xp"), 0),
            }
        )
    for field in ("class", "subclass"):
        old = str(before.get(field) or "")
        new = str(after.get(field) or "")
        if old != new:
            changes.append({"field": field, "before": old, "after": new})
    old_hp = _as_int((before.get("hp") or {}).get("max"), 0)
    new_hp = _as_int((after.get("hp") or {}).get("max"), 0)
    if old_hp != new_hp:
        changes.append({"field": "hp.max", "before": old_hp, "after": new_hp})
    if (before.get("hp") or {}) != (after.get("hp") or {}):
        changes.append(
            {
                "field": "hp",
                "before": copy.deepcopy(before.get("hp") or {}),
                "after": copy.deepcopy(after.get("hp") or {}),
            }
        )
    old_dice = _as_int((before.get("hit_dice") or {}).get("max"), 0)
    new_dice = _as_int((after.get("hit_dice") or {}).get("max"), 0)
    if old_dice != new_dice:
        changes.append({"field": "hit_dice.max", "before": old_dice, "after": new_dice})
    before_abilities = before.get("abilities") or {}
    after_abilities = after.get("abilities") or {}
    for key in ABILITY_KEYS:
        old = _as_int(before_abilities.get(key), 10)
        new = _as_int(after_abilities.get(key), 10)
        if old != new:
            changes.append({"field": f"abilities.{key}", "before": old, "after": new})
    if list(before.get("features") or []) != list(after.get("features") or []):
        changes.append(
            {
                "field": "features",
                "before": list(before.get("features") or []),
                "after": list(after.get("features") or []),
            }
        )
    for field in ("known", "prepared", "slots"):
        old = (before.get("spellcasting") or {}).get(field)
        new = (after.get("spellcasting") or {}).get(field)
        if old != new:
            changes.append({"field": f"spellcasting.{field}", "before": old, "after": new})
    for slot in plan.get("slots") or []:
        if slot.get("kind") == "proficiency_bonus":
            detail = slot.get("detail") or {}
            changes.append(
                {
                    "field": "proficiency_bonus",
                    "before": _as_int(detail.get("from"), 2),
                    "after": _as_int(detail.get("to"), 2),
                }
            )
    return changes
