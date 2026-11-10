"""CoC7 结构化精神状态（纯规则，无 I/O）。

本模块只做数据形状与数学，不读写房间、不落盘：

- ``mentality`` 结构固定在角色卡 ``extensions.mental``（房间内同时镜像到
  ``character_state["mental"]``），旧卡缺字段时由 :func:`normalize` 补默认值；
- 恐惧症 / 躁狂症按条目数组保存，发作 ``bout`` 只保留最近一次，历史放在
  ``bouts``（有上限）；
- 习惯恐惧按来源记录「历史最大损失」：再次遭遇同一来源时，损失不超过该值
  则免疫；幕间每个来源 −1（由 :func:`decay_habituations` 提供，时钟接入后调用）；
- 现实认知免疫 ``realityImmunity`` 在下一次理智损失时由调用方清除。

所有时间使用服务器本地时间字符串 ``YYYY-mm-dd HH:MM:SS``，与房间其它记录一致。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

# 结构版本：只在形状发生不兼容变化时递增。
MENTAL_VERSION = 2
BOUT_HISTORY_LIMIT = 10
TRAIT_LIMIT = 40
REALITY_CHECK_LIMIT = 10
# 发作持续时间上限（规则书为 1D10 小时）。
BOUT_MAX_HOURS = 10

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


def now_text(timestamp: float | None = None) -> str:
    """服务器本地时间字符串，与房间日志保持一致。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp if timestamp is not None else time.time()))


def add_hours(start_text: str, hours: int) -> str:
    """在时间字符串上增加小时；解析失败时按当前时间计算。"""
    parsed = parse_ts(start_text)
    base = parsed if parsed is not None else time.time()
    return now_text(base + max(0, int(hours)) * 3600)


def parse_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except ValueError:
            continue
    return None


def default_block() -> dict[str, Any]:
    """旧卡 / 新建结构的默认值；所有键都可安全读写。"""
    return {
        "version": MENTAL_VERSION,
        "phobias": [],
        "manias": [],
        "bout": {},
        "bouts": [],
        "habituations": {},
        "realityImmunity": False,
        "realityChecks": [],
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
    habituations = data.get("habituations")
    cleaned: dict[str, dict[str, Any]] = {}
    if isinstance(habituations, Mapping):
        for key, row in habituations.items():
            source = str(key or "").strip()
            if not source or not isinstance(row, Mapping):
                continue
            try:
                max_loss = max(0, int(row.get("max_loss") or 0))
            except (TypeError, ValueError):
                max_loss = 0
            cleaned[source[:80]] = {
                "name": str(row.get("name") or source)[:80],
                "max_loss": max_loss,
                "updated_at": str(row.get("updated_at") or ""),
            }
    block["habituations"] = cleaned
    block["realityImmunity"] = bool(data.get("realityImmunity"))
    block["realityChecks"] = _dict_list(data.get("realityChecks"))[-REALITY_CHECK_LIMIT:]
    try:
        block["mythosMadnessCount"] = max(0, int(data.get("mythosMadnessCount") or 0))
    except (TypeError, ValueError):
        block["mythosMadnessCount"] = 0
    block["lastLossAt"] = str(data.get("lastLossAt") or "")[:32]
    return block


def has_active_bout(block: Mapping[str, Any] | None, timestamp: float | None = None) -> bool:
    bout = (block or {}).get("bout")
    if not isinstance(bout, Mapping) or not bout.get("active"):
        return False
    expires = parse_ts(bout.get("expires_at"))
    if expires is None:
        return True
    now = timestamp if timestamp is not None else time.time()
    return expires > now


def bout_summary(bout: Mapping[str, Any]) -> str:
    """发作的一句话汇总：表现 + 症状 + 持续时长。"""
    name = str(bout.get("name") or "疯狂发作")
    hours = bout.get("duration_hours")
    symptoms = bout.get("symptoms") if isinstance(bout.get("symptoms"), list) else []
    parts = [f"{name}"]
    if symptoms:
        parts.append("症状：" + "、".join(str(item) for item in symptoms if str(item).strip()))
    if hours not in (None, ""):
        parts.append(f"持续 {int(hours)} 小时")
    if bout.get("expires_at"):
        parts.append(f"预计 {bout['expires_at']} 结束")
    return "；".join(parts)


def start_bout(
    block: dict[str, Any],
    *,
    madness_type: str,
    name: str,
    description: str,
    duration_hours: int,
    duration_rounds: int | None = None,
    trait: str = "",
    source: str = "",
    san_loss: int = 0,
    bout_roll: int | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """写入最近一次发作并压入历史；返回新发作字典。"""
    started = started_at or now_text()
    hours = max(1, min(BOUT_MAX_HOURS, int(duration_hours or 1)))
    symptoms = list(BOUT_SYMPTOMS.get(str(name), ()))
    bout = {
        "active": True,
        "type": str(madness_type or "temporary"),
        "name": str(name)[:40],
        "description": str(description or "")[:200],
        "bout_roll": bout_roll,
        "duration_hours": hours,
        "duration_rounds": int(duration_rounds) if duration_rounds is not None else None,
        "started_at": started,
        "expires_at": add_hours(started, hours),
        "symptoms": symptoms,
        "trait": str(trait or "")[:80],
        "source": str(source or "")[:120],
        "san_loss": max(0, int(san_loss or 0)),
    }
    bout["summary"] = bout_summary(bout)
    block["bout"] = bout
    history = block.get("bouts") if isinstance(block.get("bouts"), list) else []
    history.append(dict(bout))
    block["bouts"] = history[-BOUT_HISTORY_LIMIT:]
    return bout


def clear_bout(block: dict[str, Any], *, ended_at: str | None = None) -> dict[str, Any] | None:
    """结束当前发作；历史保留，返回被结束的发作。"""
    bout = block.get("bout")
    if not isinstance(bout, dict) or not bout:
        return None
    bout["active"] = False
    bout["ended_at"] = ended_at or now_text()
    block["bout"] = bout
    history = block.get("bouts") if isinstance(block.get("bouts"), list) else []
    if history:
        history[-1] = dict(bout)
        block["bouts"] = history[-BOUT_HISTORY_LIMIT:]
    return bout


def active_bouts(block: Mapping[str, Any] | None, timestamp: float | None = None) -> list[dict[str, Any]]:
    """当前仍在持续的发作（只支持单条，返回列表便于统一展示）。"""
    if has_active_bout(block, timestamp):
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
) -> dict[str, Any]:
    """习惯恐惧：记录并判断本次是否免疫。

    规则：同一来源此前造成的最大理智损失记在案；再次遭遇时损失 ≤ 记录值
    则不再损失，否则更新记录值并照常损失。
    """
    key = " ".join(str(source or "").split())[:80]
    rows = block.get("habituations") if isinstance(block.get("habituations"), dict) else {}
    row = rows.get(key) if key else None
    previous = int(row.get("max_loss") or 0) if isinstance(row, dict) else 0
    value = max(0, int(loss or 0))
    immune = bool(key) and value > 0 and value <= previous
    if key and value > previous:
        rows[key] = {"name": key, "max_loss": value, "updated_at": now_text()}
    block["habituations"] = rows
    return {"source": key, "previous": previous, "loss": value, "immune": immune}


def decay_habituations(block: dict[str, Any], steps: int = 1) -> list[str]:
    """幕间：每个来源的历史最大损失 −``steps``，归零的条目直接移除。"""
    rows = block.get("habituations") if isinstance(block.get("habituations"), dict) else {}
    removed: list[str] = []
    step = max(1, int(steps or 1))
    for key in list(rows.keys()):
        row = rows.get(key)
        if not isinstance(row, dict):
            rows.pop(key, None)
            continue
        remaining = int(row.get("max_loss") or 0) - step
        if remaining <= 0:
            rows.pop(key, None)
            removed.append(str(key))
        else:
            row["max_loss"] = remaining
            row["updated_at"] = now_text()
    block["habituations"] = rows
    return removed


# 恐惧症 / 躁狂症名常见后缀，去掉后剩下的核心词用于匹配暴露来源。
_TRAIT_SUFFIXES = ("恐惧症", "恐惧", "躁狂症", "躁狂", "癖", "症", "狂")


def _trait_keywords(row: Mapping[str, Any]) -> list[str]:
    """把一条恐惧 / 躁狂条目拆成可匹配的关键词（名称、英文、标签、核心词）。"""
    name = str(row.get("name") or "").strip()
    words = [name, str(row.get("en") or ""), str(row.get("tag") or "")]
    core = name
    for suffix in _TRAIT_SUFFIXES:
        if core.endswith(suffix) and len(core) - len(suffix) >= 2:
            core = core[: -len(suffix)]
            break
    if core and core != name:
        words.append(core)
    return [" ".join(word.split()).casefold() for word in words if str(word).strip()]


def fear_penalty(block: Mapping[str, Any] | None, source: str) -> dict[str, Any]:
    """恐惧 / 躁狂暴露：命中来源时给 1 颗惩罚骰。

    双向包含匹配：来源「巨型蜘蛛」能命中条目「蜘蛛恐惧」（核心词「蜘蛛」），
    来源「蜘蛛」也能命中完整的「蜘蛛恐惧」。
    """
    needle = " ".join(str(source or "").split()).casefold()
    if not needle:
        return {"penalty": 0, "matched": "", "kind": ""}
    for kind, key in (("phobia", "phobias"), ("mania", "manias")):
        rows = (block or {}).get(key) if isinstance((block or {}).get(key), list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if any(word in needle or needle in word for word in _trait_keywords(row)):
                return {
                    "penalty": 1,
                    "matched": str(row.get("name") or row.get("en") or source),
                    "kind": kind,
                }
    return {"penalty": 0, "matched": "", "kind": ""}


def describe(block: Mapping[str, Any] | None) -> str:
    """给人看的完整状态摘要（控制台 / 工具共用）。"""
    data = normalize(block)
    lines = [
        "恐惧症：" + ("、".join(str(row.get("name") or "") for row in data["phobias"]) or "无"),
        "躁狂症：" + ("、".join(str(row.get("name") or "") for row in data["manias"]) or "无"),
    ]
    if data["bout"]:
        state = "进行中" if has_active_bout(data) else "已结束"
        lines.append(f"最近发作（{state}）：{bout_summary(data['bout'])}")
    else:
        lines.append("最近发作：无")
    if data["habituations"]:
        habits = "、".join(
            f"{row.get('name') or key}（{row.get('max_loss')}）" for key, row in data["habituations"].items()
        )
        lines.append("习惯恐惧：" + habits)
    else:
        lines.append("习惯恐惧：无")
    lines.append(f"现实认知免疫：{'是' if data['realityImmunity'] else '否'}")
    lines.append(f"因神话疯狂次数：{data['mythosMadnessCount']}")
    return "\n".join(lines)


def trait_names(block: Mapping[str, Any] | None, kind: str) -> list[str]:
    key = "phobias" if str(kind).lower().startswith("phob") else "manias"
    rows = (block or {}).get(key)
    return [str(row.get("name") or "") for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
