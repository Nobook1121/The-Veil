"""CoC7 的纯规则数学。

本模块只做计算，不读写房间 / 角色卡，也不依赖骰子工具，便于：
- 房间快照、持久卡与 AI 工具共用同一套派生规则；
- 通用层只负责搬运数据，规则口径集中在本插件内。

口径（7 版规则书）：
- 初始理智值 = POW；理智上限 = 99 − 克苏鲁神话值。
- 最大耐久 = ⌊(CON + SIZ) / 10⌋；最大魔法值 = ⌊POW / 5⌋。
- 体格 / 伤害加值由 STR + SIZ 查表；移动力由 STR/DEX/SIZ 与年龄推导。
- 技能值上限 99（100 永远失败）。一次 SAN 检定掷出 100 时取该次损失的最大值。
- 所需检定值 ≥50 时只有 100 才算大失败；否则沿用（房规）大失败阈值。
- 单日累计理智损失达到当日起始理智的 1/5 时判定不定性疯狂。
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping

SKILL_MAX = 99
# 属性上限：7 版规则书里人类属性以 99 封顶；旧卡可能存有历史超限值，
# 读路径不报错也不改写，只在写入时按此上限钳制。
ATTRIBUTE_MAX = 99
SAN_CAP_BASE = 99
MYTHOS_SKILL_NAMES = ("克苏鲁神话", "cthulhumythos", "cthulhu mythos", "mythos")


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: Any, minimum: int, maximum: int, fallback: int = 0) -> int:
    return max(minimum, min(maximum, _as_int(value, fallback)))


def attribute_value(attributes: Mapping[str, Any] | None, key: str, fallback: int = 0) -> int:
    """按大小写不敏感的方式读取属性（STR/str/力量 之外的别名由调用方归一化）。"""
    data = attributes if isinstance(attributes, Mapping) else {}
    if key in data and data.get(key) not in (None, ""):
        return _as_int(data.get(key), fallback)
    upper = str(key or "").upper()
    for name, value in data.items():
        if str(name).strip().upper() == upper and value not in (None, ""):
            return _as_int(value, fallback)
    return fallback


def _skill_rows(sheet: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    skills = sheet.get("skills") if isinstance(sheet, Mapping) else None
    if not isinstance(skills, list):
        groups = sheet.get("skillGroups") if isinstance(sheet, Mapping) else None
        rows: list[dict[str, Any]] = []
        if isinstance(groups, Mapping):
            for items in groups.values():
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        value = item.get("value")
                        if value is None:
                            value = _as_int(item.get("base"), 0) + sum(
                                _as_int(item.get(key), 0)
                                for key in ("job", "interest", "growth")
                            )
                        rows.append({"name": item.get("name"), "skillKey": item.get("skillKey"), "value": value})
        return rows
    return [row for row in skills if isinstance(row, dict)]


def skill_value(sheet: Mapping[str, Any] | None, names: tuple[str, ...] | list[str]) -> int | None:
    """按名称 / 技能键查找技能值；找不到返回 ``None``。"""
    expected = {str(name).strip().casefold() for name in names if str(name).strip()}
    if not expected:
        return None
    for row in _skill_rows(sheet):
        candidates = {
            str(row.get("name") or "").strip().casefold(),
            str(row.get("skillKey") or "").strip().casefold(),
        }
        if candidates & expected:
            return _clamp(row.get("value"), 0, 999, 0)
    return None


def skill_rows(sheet: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """公开的技能行读取入口，供成长等插件内模块复用。"""
    return _skill_rows(sheet)


def mythos_value(sheet: Mapping[str, Any] | None) -> int:
    value = skill_value(sheet, MYTHOS_SKILL_NAMES)
    return max(0, int(value or 0))


def san_cap(sheet: Mapping[str, Any] | None) -> int:
    """理智上限 = 99 − 克苏鲁神话值（最低 0）。"""
    return max(0, SAN_CAP_BASE - mythos_value(sheet))


def initial_san(sheet: Mapping[str, Any] | None) -> int:
    """初始理智值 = POW。"""
    attributes = sheet.get("attributes") if isinstance(sheet, Mapping) else None
    return max(0, attribute_value(attributes, "POW", 0))


def max_hp(sheet: Mapping[str, Any] | None) -> int:
    attributes = sheet.get("attributes") if isinstance(sheet, Mapping) else None
    con = max(0, attribute_value(attributes, "CON", 0))
    siz = max(0, attribute_value(attributes, "SIZ", 0))
    return max(1, (con + siz) // 10)


def max_mp(sheet: Mapping[str, Any] | None) -> int:
    attributes = sheet.get("attributes") if isinstance(sheet, Mapping) else None
    pow_value = max(0, attribute_value(attributes, "POW", 0))
    return max(0, pow_value // 5)


def build_and_damage_bonus(strength: int, size: int) -> tuple[int, str]:
    """按 7 版体格表由 STR+SIZ 推导（体格, 伤害加值文本）。"""
    total = max(0, int(strength)) + max(0, int(size))
    if total <= 64:
        return -2, "-2"
    if total <= 84:
        return -1, "-1"
    if total <= 124:
        return 0, "0"
    if total <= 164:
        return 1, "+1D4"
    if total <= 204:
        return 2, "+1D6"
    extra = max(0, (total - 205) // 80)
    return 3 + extra, f"+{2 + extra}D6"


def mov_value(sheet: Mapping[str, Any] | None) -> int:
    """移动力：基础 8，按 STR/DEX 与 SIZ 比较修正，再按年龄扣减（最低 1）。"""
    attributes = sheet.get("attributes") if isinstance(sheet, Mapping) else None
    strength = attribute_value(attributes, "STR", 0)
    dex = attribute_value(attributes, "DEX", 0)
    size = attribute_value(attributes, "SIZ", 0)
    age = attribute_value(attributes, "AGE", _as_int(sheet.get("age") if isinstance(sheet, Mapping) else 0, 0))
    mov = 8
    if strength < size and dex < size:
        mov = 7
    if strength > size and dex > size:
        mov = 9
    if age >= 40:
        mov -= max(0, (min(age, 89) - 30) // 10)
    if age >= 90:
        mov -= 6
    return max(1, mov)


def derive_sheet(sheet: Mapping[str, Any] | None) -> dict[str, Any]:
    """按当前属性 / 克苏鲁神话值重算派生值，并夹紧现有资源。

    只修改派生字段，不触碰技能明细、装备等其它数据；房间快照与持久卡共用本函数。
    """
    data = dict(sheet or {})
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    strength = max(0, attribute_value(attributes, "STR", 0))
    size = max(0, attribute_value(attributes, "SIZ", 0))
    cap = san_cap(data)
    init = initial_san(data)
    if init <= 0:
        # 兼容旧数据：没有 POW 时沿用卡片原有初始值（旧版把 maxSan 当 POW），避免归零。
        legacy = data.get("initialSan", data.get("initial_san"))
        if legacy in (None, ""):
            legacy = data.get("maxSan", data.get("max_san"))
        init = _clamp(legacy, 0, cap, cap)
    hp_max = max_hp(data)
    if not (attribute_value(attributes, "CON", 0) or attribute_value(attributes, "SIZ", 0)):
        # 兼容没有属性明细的旧卡 / 测试卡：保留原有上限，避免被算成 1。
        hp_max = max(1, _clamp(data.get("maxHp", data.get("max_hp")), 1, 999, hp_max))
    mp_max = max_mp(data)
    if not attribute_value(attributes, "POW", 0):
        mp_max = max(0, _clamp(data.get("maxMp", data.get("max_mp")), 0, 999, mp_max))
    build, damage_bonus = build_and_damage_bonus(strength, size)
    if not (strength or size):
        build = _clamp(data.get("build"), -2, 99, build)
        damage_bonus = str(data.get("damageBonus") or damage_bonus)[:20]

    data["maxHp"] = hp_max
    data["max_hp"] = hp_max
    data["currentHp"] = _clamp(data.get("currentHp", data.get("current_hp")), 0, hp_max, hp_max)
    data["current_hp"] = data["currentHp"]
    data["maxSan"] = cap
    data["max_san"] = cap
    data["initialSan"] = init
    previous_san = data.get("currentSan", data.get("current_san"))
    if previous_san is None:
        previous_san = min(init, cap)
    data["currentSan"] = _clamp(previous_san, 0, cap, min(init, cap))
    data["current_san"] = data["currentSan"]
    data["maxMp"] = mp_max
    data["max_mp"] = mp_max
    data["currentMp"] = _clamp(data.get("currentMp", data.get("magicPoints", data.get("current_mp"))), 0, mp_max, mp_max)
    data["current_mp"] = data["currentMp"]
    data["magicPoints"] = data["currentMp"]
    mov = mov_value(data)
    if not (strength or size or attribute_value(attributes, "DEX", 0)):
        mov = _clamp(data.get("mov"), 1, 99, mov)
    data["mov"] = mov
    data["build"] = build
    data["damageBonus"] = damage_bonus
    data["armor"] = _clamp(data.get("armor"), 0, 99, 0)
    # 技能封顶（7 版：任何技能不得超过 99%）。
    data["skillCap"] = SKILL_MAX
    # 属性封顶由插件声明，核心写路径只读这个键，不再写死具体数值。
    data["attributeCap"] = ATTRIBUTE_MAX
    return data


def clamp_skill(value: Any) -> int:
    return _clamp(value, 0, SKILL_MAX, 0)


def max_of_expression(expression: Any) -> int:
    """一次 SAN 变化的可能最大绝对值；用于「掷出 100 取最大损失」。"""
    text = str(expression or "").strip()
    if text[:1] in {"+", "-"}:
        text = text[1:]
    match = re.fullmatch(r"(\d+)d(\d+)", text.lower())
    if match:
        return max(0, int(match.group(1)) * int(match.group(2)))
    return max(0, _as_int(text, 0))


def fumble_threshold_for(target: int, configured: int) -> int:
    """大失败阈值。

    7 版规则：所需检定值 ≥50 时只有 100 算大失败。优先级：
    房规 / 管理员显式配置的阈值（<96）优先，其次才是书内派生规则。
    """
    configured = _clamp(configured, 0, 100, 96)
    if int(target) >= 50 and configured >= 96:
        return 100
    return configured


def day_key(explicit: Any = None, now: float | None = None) -> str:
    """游戏内日期键；调用方没给（例如房间没有游戏日）时回退到本地日期。"""
    text = str(explicit or "").strip()
    if text:
        return text[:32]
    return time.strftime("%Y-%m-%d", time.localtime(now))


def record_daily_loss(state: dict[str, Any], *, day: str, before: int, loss: int) -> dict[str, Any]:
    """记录单个角色当日的累计理智损失，并判断不定性疯狂阈值。

    跨日滚动：日期变化时把当日累计清零，并以本次损失前的理智作为新的基准值。
    返回 ``{"date", "loss", "base", "threshold_met"}``，便于调用方写摘要。
    """
    loss = max(0, int(loss))
    before = max(0, int(before))
    record = state.get("san_daily") if isinstance(state.get("san_daily"), dict) else {}
    if str(record.get("date") or "") != str(day):
        record = {"date": str(day), "loss": 0, "base": before}
    total = max(0, _as_int(record.get("loss"), 0)) + loss
    base = max(0, _as_int(record.get("base"), before))
    if base <= 0:
        base = before
    record["date"] = str(day)
    record["loss"] = total
    record["base"] = base
    # 达到当日起始理智的 1/5（20%）即判不定性疯狂；结果落盘，供后续疯狂结算读取。
    reached = loss > 0 and total * 5 >= base > 0
    record["threshold_met"] = reached
    state["san_daily"] = record
    return {"date": str(day), "loss": total, "base": base, "threshold_met": reached}


def daily_loss_summary(state: dict[str, Any]) -> dict[str, Any]:
    record = state.get("san_daily") if isinstance(state, dict) else None
    return dict(record) if isinstance(record, dict) else {}
