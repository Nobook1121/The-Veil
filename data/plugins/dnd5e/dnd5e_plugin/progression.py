"""5e 升级规划：plan 只读生成选择槽位，apply 一次性校验并落库。

数据全部来自 ``dnd5e-srd`` 的 ``progression`` / ``classes`` / ``spells`` 数据包；
核心只负责房间与角色卡的通用存取，不认识等级、法术位或专长。

契约
----
- 只支持从当前等级提升 1 级；越级、跨职业、未知职业都返回明确错误。
- plan 结果带服务端过期时间与角色卡指纹；apply 前重新读取角色卡比对指纹。
- apply 先完整校验所有槽位，全部合法后返回新 sheet；任何一项不合法都不写盘。
"""

from __future__ import annotations

import copy
import hashlib
import json
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
) -> dict[str, Any]:
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
    xp_after = _as_int(row.get("xp"), 0)

    if target in ASI_LEVELS:
        ability_candidates = [
            {
                "value": key,
                "label": f"{ABILITY_LABELS[key]} +2（或与其他属性各 +1）",
                "detail": {"ability": key},
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

    if not class_entry.get("features"):
        warnings.append(
            "classes 数据包没有声明等级特性，本次计划不生成职业特性槽位。"
        )
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
        "xp": {"before": _as_int(sheet.get("xp"), 0), "after": xp_after},
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
    for key in values:
        abilities[key] = min(30, _as_int(abilities.get(key), 10) + amount)


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
    casting = new_sheet.get("spellcasting")
    if not isinstance(casting, dict):
        casting = {}
        new_sheet["spellcasting"] = casting

    for slot in plan.get("slots") or []:
        kind = slot.get("kind")
        values = picked.get(slot.get("id"))
        if values is None:
            if kind == "spell_prepared":
                continue
            if kind == "spell_slots" and slot.get("default"):
                casting["slots"] = copy.deepcopy((slot.get("detail") or {}).get("after") or {})
            continue
        if kind == "asi":
            _apply_asi(new_sheet, slot, values)
        elif kind == "proficiency_bonus":
            if values and values != ["apply"]:
                raise ProgressionError("progression.invalid_option", "熟练加值槽位不接受其它选项。")
        elif kind == "spell_slots":
            if values and values != ["apply"]:
                raise ProgressionError("progression.invalid_option", "法术位槽位不接受其它选项。")
            casting["slots"] = copy.deepcopy((slot.get("detail") or {}).get("after") or {})
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
        else:
            raise ProgressionError(
                "progression.unknown_slot_kind",
                f"不认识的槽位类型：{kind}。",
            )

    new_sheet["level"] = _as_int(plan.get("target_level"), new_sheet.get("level", 1))
    new_sheet["xp"] = max(_as_int(new_sheet.get("xp"), 0), _as_int((plan.get("xp") or {}).get("after"), 0))
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
    return {
        "level": _as_int(sheet.get("level"), 1),
        "xp": _as_int(sheet.get("xp"), 0),
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
