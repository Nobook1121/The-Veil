"""CoC7 结构化精神状态（纯规则，无 I/O）。

依据规则书第八章（理智，p.130-143）与附录术语表：

- 结构固定在角色卡 ``extensions.mental``（房间内同时镜像到
  ``character_state["mental"]``），旧卡缺字段时由 :func:`normalize` 补默认值；
- **发作 bout** 分两种呈现方式：即时症状 ``immediate``（1D10 个战斗轮）与
  总结症状 ``summary``（1D10 小时）；``duration_rounds`` / ``duration_hours``
  分别记录，只有 ``summary`` 有挂钟结束时间；
- **疯狂阶段 insanity**：临时性疯狂 1D10 小时（到期自动结束）；不定性疯狂
  持续到被治愈（当前没有自动到期路径，需治疗 / KP 显式结束）；永久性疯狂不结束；
- 恐惧症 / 躁狂症按条目数组保存；只有 KP 显式指认的暴露才给惩罚骰（不猜关键词）；
- 习惯恐惧按来源累计理智损失，上限是该神话存在单次遭遇的最大损失；累计达到上限
  后不再损失，幕间每个来源 −1（:func:`decay_habituations`，时钟接入后调用）；
- 现实认知免疫 ``realityImmunity`` 是对「幻觉」的抗性，持续到下一次理智损失。

计时口径：本模块只接受调用方显式传入的 ``now``（epoch 秒），不会自行读取墙钟；
墙钟 / 游戏时钟的选择统一由房间层（``room_clock.san_now``）决定。记录上的
``clock`` 标记（``game`` / ``wall``）用于兼容旧数据：缺失时按墙钟记录处理。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any, Mapping

# 服务器本地时区与 epoch 基准：用纯日期运算换算，兼容 Windows 上 1970 年前的
# 游戏时间（``time.localtime`` / ``time.mktime`` 对负时间戳会抛 OSError）。
_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# 结构版本：只在形状发生不兼容变化时递增。
MENTAL_VERSION = 2
BOUT_HISTORY_LIMIT = 10
TRAIT_LIMIT = 40
REALITY_CHECK_LIMIT = 10
TREATMENT_LIMIT = 20
# 规则书：发作即时症状 1D10 战斗轮，总结症状 1D10 小时。
BOUT_MAX_HOURS = 10
BOUT_MAX_ROUNDS = 10
# 规则书：临时性疯狂持续 1D10 小时；不定性疯狂需 1D6 个月的系统照料。
INSANITY_MAX_HOURS = 10
INDEFINITE_MIN_MONTHS = 6
BOUT_MODES = ("immediate", "summary")
# 计时记录使用的时钟口径：房间游戏时钟 / 服务器墙钟。缺失视为旧数据的墙钟记录。
CLOCK_KINDS = ("game", "wall")
# 即时症状没有按战斗轮推进的时钟；超过 1 小时仍未收尾时视为已结束。
# 这是防止 KP 遗忘收尾后角色永久免疫理智损失的安全阀，不是规则书数值
# （规则书只规定即时症状持续 1D10 个战斗轮）。
BOUT_SAFETY_MINUTES = 60
INSANITY_TYPES = ("none", "temporary", "indefinite", "permanent")

# 每条发作的「汇总症状」关键词：供 KP 快速描述，不改变发作表原文。
BOUT_SYMPTOMS: dict[str, tuple[str, ...]] = {
    "失忆": ("记忆断片", "时间感缺失", "无法回忆发作经过"),
    "心身障碍": ("突发失明或失聪", "肢体无力", "症状无器质原因"),
    "暴力": ("无差别攻击", "破坏身边物品", "难以沟通"),
    "偏执": ("怀疑所有人", "认为被监视", "拒绝合作"),
    "重要之人": ("错认熟人", "情绪剧烈波动", "执着寻找某人"),
    "昏厥": ("突然倒地", "短暂失去意识", "醒后虚弱"),
    "惊恐逃离": ("不顾一切逃跑", "抛下装备", "拒绝返回现场"),
    "歇斯底里": ("无法控制地大笑或哭泣", "尖叫", "呼吸急促"),
    "恐惧症": ("回避恐怖源", "幻想恐怖源出现", "所有行动承受惩罚骰"),
    "躁狂症": ("沉溺强迫冲动", "难以集中注意", "所有行动承受惩罚骰"),
}


def _local_text(timestamp: float) -> str:
    moment = (_EPOCH + timedelta(seconds=float(timestamp))).astimezone(_LOCAL_TZ)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def now_text(timestamp: float | None = None) -> str:
    """服务器本地时间字符串，与房间日志保持一致。"""
    if timestamp is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _local_text(timestamp)


def add_hours(start_text: str, hours: int) -> str:
    """在时间字符串上增加小时；解析失败时按当前时间计算。"""
    parsed = parse_ts(start_text)
    base = parsed if parsed is not None else datetime.now().timestamp()
    return now_text(base + max(0, int(hours)) * 3600)


def parse_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            moment = datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        return moment.replace(tzinfo=_LOCAL_TZ).timestamp()
    return None


def default_block() -> dict[str, Any]:
    """旧卡 / 新建结构的默认值；所有键都可安全读写。"""
    return {
        "version": MENTAL_VERSION,
        "phobias": [],
        "manias": [],
        "bout": {},
        "bouts": [],
        "insanity": {},
        "habituations": {},
        "realityImmunity": False,
        "realityChecks": [],
        "treatment": [],
        "mythosMadnessCount": 0,
        "lastLossAt": "",
    }


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append(dict(item))
        elif isinstance(item, str) and item.strip():
            rows.append({"name": item.strip()})
    return rows


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def normalize_insanity(raw: Any) -> dict[str, Any]:
    """疯狂阶段：只保留合法类型与时间；非法值按「无」处理。"""
    data = raw if isinstance(raw, Mapping) else {}
    kind = str(data.get("type") or "none").strip().lower()
    if kind not in INSANITY_TYPES:
        kind = "none"
    if kind == "none":
        return {}
    return {
        "type": kind,
        "startedAt": str(data.get("startedAt") or ""),
        "endsAt": str(data.get("endsAt") or ""),
        "durationHours": _as_int(data.get("durationHours"), 0) or None,
        "source": str(data.get("source") or "")[:120],
        # 计时口径；缺失表示旧数据，按墙钟处理。
        "clock": _clock_kind(data),
    }


def normalize(raw: Any) -> dict[str, Any]:
    """补齐缺省字段并收敛类型；不抛异常，旧卡直接可用。"""
    block = default_block()
    data = raw if isinstance(raw, Mapping) else {}
    block["version"] = MENTAL_VERSION
    block["phobias"] = _dict_list(data.get("phobias"))[-TRAIT_LIMIT:]
    block["manias"] = _dict_list(data.get("manias"))[-TRAIT_LIMIT:]
    bout = data.get("bout")
    block["bout"] = dict(bout) if isinstance(bout, Mapping) else {}
    block["bouts"] = _dict_list(data.get("bouts"))[-BOUT_HISTORY_LIMIT:]
    block["insanity"] = normalize_insanity(data.get("insanity"))
    habituations = data.get("habituations")
    cleaned: dict[str, dict[str, Any]] = {}
    if isinstance(habituations, Mapping):
        for key, row in habituations.items():
            source = str(key or "").strip()
            if not source or not isinstance(row, Mapping):
                continue
            # total_loss = 已累计损失；max_loss = 该来源单次遭遇的最大损失（上限）。
            total = _as_int(row.get("total_loss"), 0)
            cap = _as_int(row.get("max_loss"), 0)
            if "total_loss" not in row and cap > 0:
                # 兼容早期结构化数据：此前只记了单个值，按已累计处理。
                total, cap = cap, 0
            cleaned[source[:80]] = {
                "name": str(row.get("name") or source)[:80],
                "total_loss": max(0, total),
                "max_loss": max(0, cap),
                "updated_at": str(row.get("updated_at") or ""),
            }
    block["habituations"] = cleaned
    block["realityImmunity"] = bool(data.get("realityImmunity"))
    block["realityChecks"] = _dict_list(data.get("realityChecks"))[-REALITY_CHECK_LIMIT:]
    # 批次 3：心理治疗记录（extensions.mental.treatment）；旧卡缺省为空。
    block["treatment"] = _dict_list(data.get("treatment"))[-TREATMENT_LIMIT:]
    try:
        block["mythosMadnessCount"] = max(0, int(data.get("mythosMadnessCount") or 0))
    except (TypeError, ValueError):
        block["mythosMadnessCount"] = 0
    block["lastLossAt"] = str(data.get("lastLossAt") or "")[:32]
    return block


def _safety_seconds(safety_minutes: int | None) -> int:
    """把房规分钟数转成秒；``None`` 用默认 60，0 表示关闭兜底。"""
    minutes = BOUT_SAFETY_MINUTES if safety_minutes is None else safety_minutes
    try:
        return max(0, int(minutes)) * 60
    except (TypeError, ValueError):
        return BOUT_SAFETY_MINUTES * 60


def _clock_kind(record: Mapping[str, Any]) -> str:
    """记录的计时口径；缺失 / 非法值按旧数据的墙钟处理。"""
    kind = str(record.get("clock") or "").strip().lower()
    return kind if kind in CLOCK_KINDS else "wall"


def _shift_text(text: Any, delta_seconds: int) -> str:
    """在时间字符串上平移；空值或不可解析时原样返回。"""
    raw = str(text or "")
    parsed = parse_ts(raw)
    if not raw or parsed is None:
        return raw
    return now_text(parsed + delta_seconds)


def has_active_bout(
    block: Mapping[str, Any] | None,
    timestamp: float | None = None,
    *,
    safety_minutes: int | None = None,
) -> bool:
    """当前是否有进行中的发作。

    ``timestamp`` 必须与写入 ``started_at`` 使用同一个时钟口径（由房间层统一
    解析后传入）；拿不到时钟时不把脏数据当作进行中，避免永久免疫。

    即时症状按战斗轮推进（没有时钟结束时间，由 KP / 追逐轮结束），但超过房规
    ``coc7_sanity_bout_safety_minutes`` 后视为失效，防止遗忘收尾造成永久免疫；
    该值设 0 时关闭兜底，回到「只能显式收尾」的原逻辑。总结症状按 ``expires_at`` 判断。
    """
    bout = (block or {}).get("bout")
    if not isinstance(bout, Mapping) or not bout.get("active") or timestamp is None:
        return False
    now = timestamp
    # 即时症状不看 ``expires_at``：早期数据可能残留该字段，房规关闭兜底后必须忽略它，
    # 否则会出现「已关闭却仍按旧时间戳超时」的残留行为。
    if bout_mode(bout) == "immediate":
        started = parse_ts(bout.get("started_at"))
        # 开始时间不可解析时按无活动发作处理，不让脏数据变成永久免疫。
        if started is None:
            return False
        safety = _safety_seconds(safety_minutes)
        if safety > 0 and now - started >= safety:
            return False
        return True
    expires = parse_ts(bout.get("expires_at"))
    if expires is None:
        return False
    return expires > now


def bout_mode(bout: Mapping[str, Any]) -> str:
    mode = str(bout.get("mode") or "").strip().lower()
    if mode in BOUT_MODES:
        return mode
    # 兼容早期数据：有挂钟结束时间视为总结症状，否则按即时症状。
    return "summary" if bout.get("expires_at") else "immediate"


def bout_summary(bout: Mapping[str, Any]) -> str:
    """发作的一句话汇总：表现 + 症状 + 持续时长（区分轮 / 小时）。"""
    name = str(bout.get("name") or "疯狂发作")
    symptoms = bout.get("symptoms") if isinstance(bout.get("symptoms"), list) else []
    parts = [f"{name}"]
    if symptoms:
        parts.append("症状：" + "、".join(str(item) for item in symptoms if str(item).strip()))
    if bout_mode(bout) == "summary":
        hours = bout.get("duration_hours")
        if hours not in (None, ""):
            parts.append(f"持续 {int(hours)} 小时")
        if bout.get("expires_at"):
            parts.append(f"预计 {bout['expires_at']} 结束")
    else:
        rounds = bout.get("duration_rounds")
        if rounds not in (None, ""):
            parts.append(f"持续 {int(rounds)} 轮")
    return "；".join(parts)


def start_bout(
    block: dict[str, Any],
    *,
    madness_type: str,
    name: str,
    description: str,
    mode: str = "immediate",
    duration_rounds: int | None = None,
    duration_hours: int | None = None,
    trait: str = "",
    source: str = "",
    san_loss: int = 0,
    bout_roll: int | None = None,
    started_at: str | None = None,
    clock: str = "wall",
) -> dict[str, Any]:
    """写入最近一次发作并压入历史；返回新发作字典。

    ``immediate`` 对应表Ⅶ（1D10 个战斗轮），``summary`` 对应表Ⅷ
    （1D10 小时）；两种时长都保留，展示以 ``mode`` 为准。
    ``started_at`` / ``clock`` 必须由调用方按房间统一口径给出（游戏时钟或墙钟）。
    """
    started = started_at or now_text()
    declared = str(mode or "").strip().lower()
    chosen = declared if declared in BOUT_MODES else "immediate"
    rounds = max(1, min(BOUT_MAX_ROUNDS, _as_int(duration_rounds, 1)))
    hours = max(1, min(BOUT_MAX_HOURS, _as_int(duration_hours, 1)))
    symptoms = list(BOUT_SYMPTOMS.get(str(name), ()))
    bout = {
        "active": True,
        "mode": chosen,
        "type": str(madness_type or "temporary"),
        "name": str(name)[:40],
        "description": str(description or "")[:200],
        "bout_roll": bout_roll,
        "duration_rounds": rounds,
        "duration_hours": hours,
        "started_at": started,
        "expires_at": add_hours(started, hours) if chosen == "summary" else "",
        "symptoms": symptoms,
        "trait": str(trait or "")[:80],
        "source": str(source or "")[:120],
        "san_loss": max(0, int(san_loss or 0)),
        "clock": _clock_kind({"clock": clock}),
    }
    bout["summary"] = bout_summary(bout)
    block["bout"] = bout
    history = block.get("bouts") if isinstance(block.get("bouts"), list) else []
    history.append(dict(bout))
    block["bouts"] = history[-BOUT_HISTORY_LIMIT:]
    return bout


def _archive_bout(block: dict[str, Any], bout: dict[str, Any]) -> None:
    history = block.get("bouts") if isinstance(block.get("bouts"), list) else []
    if history:
        history[-1] = dict(bout)
        block["bouts"] = history[-BOUT_HISTORY_LIMIT:]


def clear_bout(
    block: dict[str, Any],
    *,
    ended_at: str | None = None,
    reason: str = "",
    keep: bool = False,
) -> dict[str, Any] | None:
    """结束当前发作；历史保留，返回被结束的发作。

    ``keep=False`` 时清空 ``bout`` 字段（最近一次发作仍可在 ``bouts`` 查到）。
    """
    bout = block.get("bout")
    if not isinstance(bout, dict) or not bout:
        return None
    bout["active"] = False
    bout["ended_at"] = ended_at or now_text()
    bout["end_reason"] = str(reason or "")
    _archive_bout(block, bout)
    block["bout"] = dict(bout) if keep else {}
    return bout


def expire_bout(
    block: dict[str, Any],
    timestamp: float | None = None,
    *,
    safety_minutes: int | None = None,
) -> dict[str, Any] | None:
    """到点收敛：总结症状超过 ``expires_at``、或即时症状超过安全阀时收尾。幂等。

    ``timestamp`` 必须与记录写入时使用同一个时钟口径；拿不到时钟时不做任何
    到期判断（返回 ``None``），由调用方补齐后再收敛。``safety_minutes`` 来自
    房规；0 表示关闭即时症状的超时兜底，仅保留 KP / 命令显式收尾。
    """
    bout = block.get("bout")
    if not isinstance(bout, dict) or not bout.get("active") or timestamp is None:
        return None
    now = timestamp
    if bout_mode(bout) == "summary":
        expires = parse_ts(bout.get("expires_at"))
        if expires is None:
            # 结束时间脏数据无法判断，按已到期收尾，避免永久免疫。
            return clear_bout(block, ended_at=now_text(now), reason="到期")
        if expires > now:
            return None
        return clear_bout(block, ended_at=now_text(expires), reason="到期")
    # 即时症状默认由 KP 按轮显式收尾；只有房规安全阀开启（>0）时才在超时后兜底清空。
    safety = _safety_seconds(safety_minutes)
    if safety <= 0:
        return None
    started = parse_ts(bout.get("started_at"))
    if started is None:
        return clear_bout(block, ended_at=now_text(now), reason="超时")
    if now - started < safety:
        return None
    return clear_bout(block, ended_at=now_text(started + safety), reason="超时")


def start_insanity(
    block: dict[str, Any],
    kind: str,
    *,
    duration_hours: int | None = None,
    source: str = "",
    started_at: str | None = None,
    clock: str = "wall",
) -> dict[str, Any]:
    """写入疯狂阶段：临时性 1D10 小时；不定性 / 永久没有挂钟结束时间。

    ``started_at`` / ``clock`` 由调用方按房间统一口径给出。
    """
    value = str(kind or "").strip().lower()
    if value not in INSANITY_TYPES or value == "none":
        return {}
    started = started_at or now_text()
    hours = None
    ends_at = ""
    if value == "temporary":
        hours = max(1, min(INSANITY_MAX_HOURS, _as_int(duration_hours, 1)))
        ends_at = add_hours(started, hours)
    record = {
        "type": value,
        "startedAt": started,
        "endsAt": ends_at,
        "durationHours": hours,
        "source": str(source or "")[:120],
        "clock": _clock_kind({"clock": clock}),
    }
    block["insanity"] = record
    return record


def end_insanity(block: dict[str, Any], *, ended_at: str | None = None) -> dict[str, Any] | None:
    """结束疯狂阶段（临时性到期 / 治疗或 KP 显式结束）；不定性没有自动路径。"""
    record = block.get("insanity")
    if not isinstance(record, dict) or not record:
        return None
    record = dict(record)
    record["endedAt"] = ended_at or now_text()
    block["insanity"] = {}
    return record


def expire_insanity(block: dict[str, Any], timestamp: float | None = None) -> dict[str, Any] | None:
    """临时性疯狂到期自动结束；不定性 / 永久不在此处理。幂等。

    ``timestamp`` 必须与记录写入时使用同一个时钟口径；拿不到时钟时不做判断。
    """
    record = block.get("insanity") or {}
    if str(record.get("type") or "") != "temporary" or timestamp is None:
        return None
    ends = parse_ts(record.get("endsAt"))
    if ends is None:
        # 结束时间脏数据无法判断，按已到期收尾，避免永久卡在临时性疯狂。
        return end_insanity(block, ended_at=now_text(timestamp))
    if ends > timestamp:
        return None
    return end_insanity(block, ended_at=now_text(ends))


def shift_timers(block: dict[str, Any], delta_seconds: int) -> bool:
    """时间校正后平移 ``game`` 口径的活动计时，保留剩余时长；返回是否改动。

    只在房间游戏时钟被 ``time correct`` 改写时调用：把已存在的发作 / 临时性疯狂
    的起止时间一起平移同样的秒数，这样回拨到历史日期后它们仍会按游戏时间到期，
    而不是因为「开始时间在未来」永远不到期。
    """
    try:
        delta = int(delta_seconds or 0)
    except (TypeError, ValueError):
        return False
    if not delta:
        return False
    changed = False
    bout = block.get("bout")
    if isinstance(bout, dict) and bout.get("active") and _clock_kind(bout) == "game":
        bout["started_at"] = _shift_text(bout.get("started_at"), delta)
        if bout_mode(bout) == "summary":
            bout["expires_at"] = _shift_text(bout.get("expires_at"), delta)
        bout["summary"] = bout_summary(bout)
        changed = True
    record = block.get("insanity")
    if isinstance(record, dict) and record and _clock_kind(record) == "game":
        record["startedAt"] = _shift_text(record.get("startedAt"), delta)
        if record.get("endsAt"):
            record["endsAt"] = _shift_text(record.get("endsAt"), delta)
        changed = True
    return changed


def rebase_timers(
    block: dict[str, Any],
    *,
    now: float,
    target_clock: str,
    safety_minutes: int | None = None,
    wall_now: float | None = None,
) -> bool:
    """把活动计时统一到目标时钟口径；返回是否有改动。

    - 记录自带的 ``clock`` 与目标一致时不动；
    - 旧记录没有 ``clock`` 时按墙钟处理（历史版本用墙钟写入）；
    - 墙钟 → 游戏时钟：按墙钟已流逝量折算剩余时长并锚定到当前游戏时间，
      所以时钟回拨 / 超前都不会变成永久免疫或立刻结束；
    - 游戏时钟 → 墙钟：无法反推游戏流逝量，保留完整时长后重新锚定。
    """
    target = "game" if str(target_clock or "").strip().lower() == "game" else "wall"
    reference = wall_now if wall_now is not None else datetime.now().timestamp()
    changed = False

    bout = block.get("bout")
    if (
        isinstance(bout, dict)
        and bout.get("active")
        and _clock_kind(bout) != target
    ):
        mode = bout_mode(bout)
        source = _clock_kind(bout)
        window = (
            max(0, _as_int(bout.get("duration_hours"), 0)) * 3600
            if mode == "summary"
            else _safety_seconds(safety_minutes)
        )
        elapsed = 0.0
        started = parse_ts(bout.get("started_at"))
        if target == "game" and source != "game" and started is not None:
            elapsed = max(0.0, reference - started)
        if window > 0:
            elapsed = min(elapsed, float(window))
        anchor = now - elapsed
        bout["started_at"] = now_text(anchor)
        bout["expires_at"] = now_text(anchor + window) if mode == "summary" and window > 0 else ""
        bout["clock"] = target
        bout["summary"] = bout_summary(bout)
        changed = True

    record = block.get("insanity")
    if isinstance(record, dict) and record and _clock_kind(record) != target:
        if str(record.get("type") or "") == "temporary":
            source = _clock_kind(record)
            window = max(0, _as_int(record.get("durationHours"), 0)) * 3600
            if window <= 0:
                started = parse_ts(record.get("startedAt"))
                ends = parse_ts(record.get("endsAt"))
                if started is not None and ends is not None:
                    window = max(0, int(ends - started))
            elapsed = 0.0
            started = parse_ts(record.get("startedAt"))
            if target == "game" and source != "game" and started is not None:
                elapsed = max(0.0, reference - started)
            if window > 0:
                elapsed = min(elapsed, float(window))
            anchor = now - elapsed
            record["startedAt"] = now_text(anchor)
            record["endsAt"] = now_text(anchor + window) if window > 0 else ""
        record["clock"] = target
        changed = True
    return changed


def active_bouts(
    block: Mapping[str, Any] | None,
    timestamp: float | None = None,
    *,
    safety_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """当前仍在持续的发作（只支持单条，返回列表便于统一展示）。"""
    if has_active_bout(block, timestamp, safety_minutes=safety_minutes):
        bout = (block or {}).get("bout")
        return [dict(bout)] if isinstance(bout, Mapping) else []
    return []


def add_trait(
    block: dict[str, Any],
    *,
    kind: str,
    name: str,
    tag: str = "",
    en: str = "",
    source: str = "",
) -> dict[str, Any]:
    """登记恐惧症 / 躁狂症；同名去重，保留最近一次来源。"""
    key = "phobias" if str(kind).lower().startswith("phob") else "manias"
    rows = block.get(key) if isinstance(block.get(key), list) else []
    entry = {
        "name": str(name or "").strip()[:60],
        "tag": str(tag or "").strip()[:40],
        "en": str(en or "").strip()[:60],
        "source": str(source or "").strip()[:120],
        "ts": now_text(),
    }
    if not entry["name"]:
        return {}
    for existing in rows:
        if isinstance(existing, dict) and existing.get("name") == entry["name"]:
            existing.update(entry)
            block[key] = rows[-TRAIT_LIMIT:]
            return existing
    rows.append(entry)
    block[key] = rows[-TRAIT_LIMIT:]
    return entry


def note_habituation(
    block: dict[str, Any],
    source: str,
    loss: int,
    max_loss: int | None = None,
) -> dict[str, Any]:
    """习惯恐惧：记录同一来源的累计理智损失并判断本次实际损失。

    规则书 8.5 / p.169：同一神话存在造成的理智损失累计达到「该存在单次遭遇
    可能造成的最大损失」（例如深潜者 0/1D6 → 6）后，不再因它损失理智；
    幕间成长时每个来源的累计值 −1。
    """
    key = " ".join(str(source or "").split())[:80]
    rows = block.get("habituations") if isinstance(block.get("habituations"), dict) else {}
    row = rows.get(key) if key else None
    previous_total = _as_int((row or {}).get("total_loss"), 0) if isinstance(row, dict) else 0
    previous_cap = max(0, _as_int((row or {}).get("max_loss"), 0)) if isinstance(row, dict) else 0
    value = max(0, int(loss or 0))
    cap = max(0, _as_int(max_loss, 0)) if max_loss is not None else previous_cap
    # 同一来源的上限取已知最大值；规则书里的上限是固定值，不会随遭遇降低。
    cap = max(previous_cap, cap)
    immune = bool(key) and value > 0 and cap > 0 and previous_total >= cap
    applied = value
    if immune:
        applied = 0
    elif cap > 0:
        # 累计不超过上限：例如累计 5 / 上限 6 时再损失 1D6=3，只损失 1。
        applied = max(0, min(value, cap - previous_total))
    if key:
        rows[key] = {
            "name": str((row or {}).get("name") or key)[:80],
            "total_loss": min(cap, previous_total + applied) if cap > 0 else previous_total + applied,
            "max_loss": cap,
            "updated_at": now_text(),
        }
    block["habituations"] = rows
    return {
        "source": key,
        "previous_total": previous_total,
        "cap": cap,
        "loss": value,
        "applied": applied,
        "immune": immune,
    }


def decay_habituations(block: dict[str, Any], steps: int = 1) -> list[str]:
    """幕间成长：每个来源的累计损失 −``steps``，归零的条目直接移除。"""
    rows = block.get("habituations") if isinstance(block.get("habituations"), dict) else {}
    removed: list[str] = []
    step = max(1, int(steps or 1))
    for key in list(rows.keys()):
        row = rows.get(key)
        if not isinstance(row, dict):
            rows.pop(key, None)
            continue
        remaining = _as_int(row.get("total_loss"), 0) - step
        if remaining <= 0:
            rows.pop(key, None)
            removed.append(str(key))
        else:
            row["total_loss"] = remaining
            row["updated_at"] = now_text()
    block["habituations"] = rows
    return removed


def record_treatment(
    block: dict[str, Any],
    *,
    level: str,
    months: int,
    delta: int,
    note: str = "",
    source: str = "",
) -> dict[str, Any]:
    """记录一次心理治疗（extensions.mental.treatment，批次 3）。

    只负责留痕；SAN 数字由调用方按 ``treatment.monthly_san_delta`` 结算后传入。
    """
    rows = block.get("treatment") if isinstance(block.get("treatment"), list) else []
    record = {
        "id": f"trt-{int(time.time() * 1000)}",
        "level": str(level or ""),
        "months": max(1, _as_int(months, 1)),
        "delta": _as_int(delta, 0),
        "note": str(note or "")[:120],
        "source": str(source or "")[:60],
        "at": now_text(),
    }
    rows.insert(0, record)
    del rows[TREATMENT_LIMIT:]
    block["treatment"] = rows
    return record


def _match_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def fear_penalty(block: Mapping[str, Any] | None, exposure: str) -> dict[str, Any]:
    """恐惧 / 躁狂暴露：KP 显式指认条目时给 1 颗惩罚骰。

    规则书要求由守秘人判断角色是否「直接接触恐惧源 / 直面躁狂诱因」，
    所以这里只接受与角色已有条目**精确同名**（或 id / 英文名）的指认，
    不做关键词猜测；KP 可先用 ``san.traits`` 查询准确名称。
    """
    needle = _match_key(exposure)
    if not needle:
        return {"penalty": 0, "matched": "", "kind": ""}
    for kind, key in (("phobia", "phobias"), ("mania", "manias")):
        rows = (block or {}).get(key) if isinstance((block or {}).get(key), list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            candidates = (row.get("name"), row.get("en"), row.get("id"))
            if any(needle == _match_key(item) for item in candidates if str(item or "").strip()):
                return {
                    "penalty": 1,
                    "matched": str(row.get("name") or row.get("en") or exposure),
                    "kind": kind,
                }
    return {"penalty": 0, "matched": "", "kind": ""}


def describe(
    block: Mapping[str, Any] | None,
    *,
    timestamp: float | None = None,
    safety_minutes: int | None = None,
) -> str:
    """给人看的完整状态摘要（控制台 / 工具共用）；``timestamp`` 与记录同一时钟口径。"""
    data = normalize(block)
    lines = [
        "恐惧症：" + ("、".join(str(row.get("name") or "") for row in data["phobias"]) or "无"),
        "躁狂症：" + ("、".join(str(row.get("name") or "") for row in data["manias"]) or "无"),
    ]
    if data["bout"]:
        state = "进行中" if has_active_bout(data, timestamp, safety_minutes=safety_minutes) else "已结束"
        lines.append(f"最近发作（{state}）：{bout_summary(data['bout'])}")
    else:
        lines.append("最近发作：无")
    insanity = data["insanity"]
    if insanity:
        label = {"temporary": "临时性", "indefinite": "不定性", "permanent": "永久性"}.get(
            str(insanity.get("type")), str(insanity.get("type"))
        )
        tail = f"，预计 {insanity['endsAt']} 恢复" if insanity.get("endsAt") else ""
        lines.append(f"疯狂阶段：{label}（{insanity.get('startedAt') or '-'} 起{tail}）")
    else:
        lines.append("疯狂阶段：无")
    if data["habituations"]:
        habits = "、".join(
            f"{row.get('name') or key} {row.get('total_loss')}/{row.get('max_loss') or '?'}"
            for key, row in data["habituations"].items()
        )
        lines.append("习惯恐惧（累计/上限）：" + habits)
    else:
        lines.append("习惯恐惧：无")
    lines.append(f"现实认知免疫：{'是' if data['realityImmunity'] else '否'}")
    lines.append(f"因神话疯狂次数：{data['mythosMadnessCount']}")
    return "\n".join(lines)


def trait_names(block: Mapping[str, Any] | None, kind: str) -> list[str]:
    key = "phobias" if str(kind).lower().startswith("phob") else "manias"
    rows = (block or {}).get(key)
    return [str(row.get("name") or "") for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
