"""FVTT Active Effect 兼容的「按路径改数值」通用效果引擎。

与 :mod:`dnd5e_plugin.effects` 的分工（两者互不替代）：

- ``effects.py``：**条件/战斗内效果**。认 5e 条件名（charmed、exhaustion…），
  把规则事实整理成核心 ``trpg_server.ruleset.effects`` 的效果实例，负责
  持续时间推进、回合 tick、结束豁免、专注连带移除。它回答「角色现在处于
  什么状态」。
- ``effect_changes.py``（本模块）：**数值改写引擎**。认 Foundry Active Effect
  的 ``changes`` 数据形态（``{key, mode, value}``），把「背着这枚戒指 AC +1」
  这类声明式修正算进派生值。它回答「算这个数的时候要加多少」。

数据形态（导入器与手工写入共用，``statuses``/``duration`` 之外的键都可有可无）::

    {
      "id": "1QDjbtS7gIrWu5ks",
      "name": "Fighting Style: Defense",
      "transfer": true,           # 源自物品且应随物品生效
      "disabled": false,          # 物品效果自身被关掉
      "changes": [
        {"key": "system.attributes.ac.bonus", "mode": 2, "value": "+1"}
      ],
      "origin": {"item_id": "…", "source": "feat", "equipped": true}
    }

挂载位置：

- 角色卡 ``sheet.effect_changes``：不来自装备容器的物品效果（专长、种族特性、
  法术残留）以及手工施加的效果；
- 装备条目 ``equipment[].effects``：来自 weapon/equipment/consumable/loot/
  tool/container 这些物品的效果，激活判定看同一行的 ``equipped``。

语义（与 Foundry ``ActiveEffect`` 同款）：

===========  ==========================================================
mode         含义
===========  ==========================================================
0 ``CUSTOM``     不自动应用（原文留给宏/人工），只计数
1 ``MULTIPLY``   现有值 × value
2 ``ADD``        现有值 + value
3 ``DOWNGRADE``  取较小值（min）
4 ``UPGRADE``    取较大值（max）
5 ``OVERRIDE``   直接覆盖（永远最后应用）
===========  ==========================================================

同一个 key 上的多条 change 按 ``MULTIPLY → ADD → DOWNGRADE → UPGRADE →
OVERRIDE`` 的固定顺序结算（同级保留源顺序），这样结果与效果挂载顺序无关。

列表型 key（``system.traits.dr.value`` 抗性、``traits.ci.value`` 条件免疫等）
只支持「加值/覆盖」：``ADD`` 逐项追加去重，``OVERRIDE`` 整体替换。

路径：change 的 ``key`` 是 FVTT 文档路径，``system.`` 前缀对应卡片 sheet 的
镜像路径（``system.attributes.ac.bonus`` → 镜像 doc 的
``attributes.ac.bonus``）。引擎只认下表中的路径族，其余（``flags.*``、
``ATL.*``、``activities[…]`` 等模块/前端字段）一律计入 ``unsupported``，
不报错也不改动任何数值。

**回归铁律**：没有任何可用效果时 :func:`effective_sheet` 返回 ``empty=True``
的计算视图，所有接入点都短路走原实现，派生值逐字节一致。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# --- 模式常量（数值与 Foundry ActiveEffect.MODES 一致） ---------------------

MODE_CUSTOM = 0
MODE_MULTIPLY = 1
MODE_ADD = 2
MODE_DOWNGRADE = 3
MODE_UPGRADE = 4
MODE_OVERRIDE = 5

MODE_LABELS = {
    MODE_CUSTOM: "custom",
    MODE_MULTIPLY: "multiply",
    MODE_ADD: "add",
    MODE_DOWNGRADE: "downgrade",
    MODE_UPGRADE: "upgrade",
    MODE_OVERRIDE: "override",
}

#: 同 key 多条 change 的应用优先级（override 永远最后）。
MODE_ORDER = {
    MODE_MULTIPLY: 0,
    MODE_ADD: 1,
    MODE_DOWNGRADE: 2,
    MODE_UPGRADE: 3,
    MODE_OVERRIDE: 4,
}

# --- 路径族 ----------------------------------------------------------------

#: ``system.`` 前缀 → sheet 镜像路径的前缀。
SYSTEM_PREFIX = "system."

#: 属性键（与 ``rules.ABILITY_KEYS`` 同口径，单列一份避免循环导入）。
ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

#: 精确支持的数值路径（镜像 doc 路径，不带 ``system.``）。**表里每一行都有
#: 真实读值点**（括号里是消费方），没有消费方的路径一律归 unsupported，避免
#: 「看起来支持、其实不影响任何数」。
NUMBER_PATHS: dict[str, str] = {
    "attributes.ac.bonus": "AC 加值（schema.derive / combat / statblock）",
    "attributes.init.bonus": "先攻加值（schema.derive）",
    "bonuses.abilities.save": "全豁免加值（checks）",
    "bonuses.abilities.check": "全检定加值（checks / 被动察觉）",
    "bonuses.abilities.ability_check": "属性检定加值（checks）",
    "bonuses.abilities.saving_throw": "豁免加值（checks）",
    "bonuses.mwak.attack": "近战武器攻击加值（combat）",
    "bonuses.mwak.damage": "近战武器伤害加值（combat）",
    "bonuses.rwak.attack": "远程武器攻击加值（combat）",
    "bonuses.rwak.damage": "远程武器伤害加值（combat）",
    "bonuses.msak.attack": "法术攻击加值（combat 施法档案）",
    "bonuses.rsak.attack": "法术攻击加值（combat 施法档案，msak 缺席时）",
    "bonuses.spell.dc": "法术豁免 DC 加值（schema.derive）",
}

#: 参数化支持的数值路径族（正则 → 说明）。同样每一族都有读值点。
NUMBER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^abilities\.(str|dex|con|int|wis|cha)\.check\.roll\.mode$"),
        "属性检定 d20 模式（checks：1 优势 / -1 劣势）",
    ),
    (
        re.compile(r"^abilities\.(str|dex|con|int|wis|cha)\.save\.roll\.mode$"),
        "豁免 d20 模式（checks：1 优势 / -1 劣势）",
    ),
    (
        re.compile(r"^bonuses\.abilities\.(save|saving_throw)\.(str|dex|con|int|wis|cha)$"),
        "单项豁免加值（checks）",
    ),
    (
        re.compile(r"^bonuses\.abilities\.(check|ability_check)\.(str|dex|con|int|wis|cha)$"),
        "单项属性检定加值（checks）",
    ),
    (
        re.compile(r"^bonuses\.skills\.[a-z-]+\.check$"),
        "单项技能检定加值（checks）",
    ),
)

#: 列表型路径：ADD 追加、OVERRIDE 替换（其余模式忽略并计数）。
LIST_PATHS: dict[str, str] = {
    "traits.dr.value": "伤害抗性",
    "traits.dv.value": "伤害易伤",
    "traits.di.value": "伤害免疫",
    "traits.ci.value": "条件免疫",
    "traits.languages.value": "语言",
}

#: 文本型路径：``ac.calc`` 真的改写无甲 AC 算法；``name`` 只记录（物品改名
#: 对派生值无意义，留着是为了「别把它当未知字段报警」）。
TEXT_PATHS: dict[str, str] = {
    "attributes.ac.calc": "AC 算法（default/natural/mage/flat，schema.derive）",
    "name": "改名（仅记录）",
}

#: 参考表：已知但不支持的 FVTT 字段族 → 说明（只用于告警文案，不参与计算）。
KNOWN_UNSUPPORTED_PREFIXES: dict[str, str] = {
    "flags.": "模块/系统标志（midi-qol、dnd5e flags）",
    "ATL.": "Token 外观（Tokenizer/ATL）",
    "activities[": "物品活动（dnd5e v6 Activities）",
    "items[": "物品数组",
    "effects[": "嵌套效果",
}

_DICE_RE = re.compile(r"\d*\s*d\s*\d", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def parse_number(value: Any) -> tuple[float | None, str]:
    """把 change 的 ``value`` 转成数字。

    返回 ``(数字, 原因)``；第二个值是空串表示成功，否则为不能结算的原因
    （``"formula"`` 骰式/公式、``"empty"`` 空值、``"text"`` 非数字文本）。
    """
    if isinstance(value, bool):
        return (1.0 if value else 0.0), ""
    if isinstance(value, (int, float)):
        return float(value), ""
    raw = _text(value)
    if not raw:
        return None, "empty"
    if "@" in raw or _DICE_RE.search(raw):
        return None, "formula"
    try:
        return float(raw.replace("+", "", 1) if raw.startswith("+") else raw), ""
    except ValueError:
        return None, "text"


def _as_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    if not text:
        return []
    # FVTT 里列表 key 也可能写成可解析的 JSON 数组或逗号分隔串。
    if text.startswith("[") and text.endswith("]"):
        try:
            import json

            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return [_text(item) for item in parsed if _text(item)]
    return [part.strip() for part in text.split(",") if part.strip()]


# --- 点分路径 get/set ------------------------------------------------------


def mirror_path(key: Any) -> str:
    """把 FVTT 文档路径转成 sheet 镜像路径（只去掉 ``system.`` 前缀）。"""
    raw = _text(key)
    if raw.casefold().startswith(SYSTEM_PREFIX):
        return raw[len(SYSTEM_PREFIX) :]
    return raw


def resolve_path(document: Mapping[str, Any], path: Any) -> Any:
    """按点分路径取值；中途缺失返回 ``None``（不抛异常）。"""
    node: Any = document
    for part in _parts(path):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def set_path(document: dict[str, Any], path: Any, value: Any) -> bool:
    """按点分路径写值（缺层自动补 dict）；路径非法返回 ``False``。"""
    parts = _parts(path)
    if not parts:
        return False
    node: dict[str, Any] = document
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    return True


def _parts(path: Any) -> list[str]:
    raw = _text(path)
    if not raw:
        return []
    return [part for part in raw.split(".") if part]


def classify_path(key: Any) -> tuple[str, str]:
    """返回 ``(种类, 镜像路径)``。

    种类：``number`` / ``list`` / ``text`` / ``unsupported``。
    """
    raw = _text(key)
    if not raw:
        return "unsupported", ""
    mirror = mirror_path(raw)
    if mirror in NUMBER_PATHS:
        return "number", mirror
    if mirror in LIST_PATHS:
        return "list", mirror
    if mirror in TEXT_PATHS:
        return "text", mirror
    for pattern, _label in NUMBER_PATTERNS:
        if pattern.match(mirror):
            return "number", mirror
    return "unsupported", mirror


def unsupported_hint(key: Any) -> str:
    """给不支持的 key 一句出处说明（告警文案用）。"""
    raw = _text(key)
    for prefix, hint in KNOWN_UNSUPPORTED_PREFIXES.items():
        if raw.startswith(prefix):
            return hint
    return ""


# --- 数据整理 --------------------------------------------------------------


def normalize_change(raw: Any) -> dict[str, Any] | None:
    """单条 change → ``{key, mode, value}``；结构不认识返回 ``None``。"""
    if not isinstance(raw, Mapping):
        return None
    key = _text(raw.get("key"))
    if not key:
        return None
    mode = _as_int(raw.get("mode"), MODE_ADD)
    if mode not in MODE_LABELS:
        mode = MODE_ADD
    return {"key": key, "mode": mode, "value": raw.get("value")}


def normalize_effect(raw: Any) -> dict[str, Any] | None:
    """效果文档 → 引擎可消费的稳定形态；没有可用 changes 时仍返回（便于计数）。"""
    if not isinstance(raw, Mapping):
        return None
    changes = [
        change
        for change in (normalize_change(item) for item in raw.get("changes") or [])
        if change is not None
    ]
    origin = raw.get("origin") if isinstance(raw.get("origin"), Mapping) else {}
    duration = raw.get("duration") if isinstance(raw.get("duration"), Mapping) else {}
    return {
        "id": _text(raw.get("id") or raw.get("_id")),
        "name": _text(raw.get("name")),
        "transfer": bool(raw.get("transfer")),
        "disabled": bool(raw.get("disabled")),
        # 非 transfer 的效果只能手动施加：``applied`` 必须原样带过来，
        # 否则 :func:`is_active` 里那条分支永远走不到。
        "applied": bool(raw.get("applied")),
        "changes": changes,
        "origin": {
            "item_id": _text(origin.get("item_id")),
            "source": _text(origin.get("source")),
            "equipped": origin.get("equipped"),
        },
        "duration": {
            key: duration.get(key)
            for key in ("seconds", "rounds", "turns", "startTime")
            if duration.get(key) is not None
        },
    }


def effect_entries(sheet: Mapping[str, Any] | None) -> list[tuple[dict[str, Any], bool | None]]:
    """收集卡面所有效果，返回 ``[(效果, 来源装备是否已装备)]``。

    第二项为 ``None`` 表示来自 ``sheet.effect_changes``（角色级，不跟装备行）。
    """
    source = sheet if isinstance(sheet, Mapping) else {}
    entries: list[tuple[dict[str, Any], bool | None]] = []
    for raw in source.get("effect_changes") or []:
        effect = normalize_effect(raw)
        if effect is not None:
            entries.append((effect, None))
    equipment = source.get("equipment")
    for row in equipment if isinstance(equipment, list) else []:
        if not isinstance(row, Mapping):
            continue
        equipped = bool(row.get("equipped"))
        for raw in row.get("effects") or []:
            effect = normalize_effect(raw)
            if effect is not None:
                entries.append((effect, equipped))
    return entries


def is_active(effect: Mapping[str, Any], parent_equipped: bool | None) -> bool:
    """激活判定。

    - 自身 ``disabled`` → 不生效；
    - 来自装备条目：必须 ``transfer``、装备行 ``equipped`` 且效果没被显式断开；
    - 来自 ``sheet.effect_changes``：``transfer`` 的直接生效（专长/种族特性），
      非 ``transfer``（只能手动施加）要显式 ``applied: true`` 才算数。
    """
    if effect.get("disabled"):
        return False
    origin = effect.get("origin") if isinstance(effect.get("origin"), Mapping) else {}
    if parent_equipped is not None:
        if not effect.get("transfer"):
            return False
        if not parent_equipped:
            return False
        return origin.get("equipped") is not False
    if origin.get("equipped") is False:
        return False
    if effect.get("transfer"):
        return True
    return bool(effect.get("applied"))


# --- 计算视图 --------------------------------------------------------------


class EffectiveSheet:
    """``effective_sheet(sheet)`` 的返回值：一张「改完数」的镜像投影。

    所有接入点只读这个视图，不自己堆加值逻辑：

    - :meth:`armor_class` / :attr:`initiative_bonus` / :meth:`check_bonus` /
      :meth:`attack_bonus` / :meth:`damage_bonus` / :meth:`spell_attack_bonus`
    - :meth:`traits`、:meth:`apply_attacks`、:meth:`extend_traits`
    """

    def __init__(self) -> None:
        self.doc: dict[str, Any] = {}
        self.applied: list[dict[str, Any]] = []
        self.unsupported: list[str] = []
        self.deferred: list[dict[str, Any]] = []
        self.custom: list[str] = []
        self.effects: list[dict[str, Any]] = []
        self.stats: dict[str, int] = {
            "documents": 0,
            "active": 0,
            "changes": 0,
            "applied": 0,
            "unsupported": 0,
            "deferred": 0,
            "custom": 0,
        }

    # -- 空视图判定 -------------------------------------------------------
    @property
    def empty(self) -> bool:
        """没有任何实际生效的 change（接入点据此短路走原实现）。"""
        return not self.applied

    # -- 读值 -------------------------------------------------------------
    def value(self, path: Any, default: Any = 0) -> Any:
        found = resolve_path(self.doc, mirror_path(path))
        return default if found is None else found

    def number(self, path: Any, default: int = 0) -> int:
        found = resolve_path(self.doc, mirror_path(path))
        return default if found is None else _as_int(found, default)

    @property
    def ac_bonus(self) -> int:
        return self.number("attributes.ac.bonus")

    @property
    def ac_calc(self) -> str:
        return _text(self.value("attributes.ac.calc", "")).casefold()

    @property
    def initiative_bonus(self) -> int:
        return self.number("attributes.init.bonus")

    @property
    def max_hp_bonus(self) -> int:
        return self.number("attributes.hp.bonus")

    def save_bonus(self, ability: Any = "") -> int:
        total = self.number("bonuses.abilities.save") + self.number("bonuses.abilities.saving_throw")
        key = _text(ability).casefold()
        if key:
            total += self.number(f"bonuses.abilities.save.{key}")
        return total

    def check_bonus(self, ability: Any = "", skill: Any = "") -> int:
        total = self.number("bonuses.abilities.check") + self.number("bonuses.abilities.ability_check")
        key = _text(ability).casefold()
        if key:
            total += self.number(f"bonuses.abilities.check.{key}")
        skill_key = _text(skill).casefold().replace(" ", "-")
        if skill_key:
            total += self.number(f"bonuses.skills.{skill_key}.check")
        return total

    def attack_bonus(self, kind: Any = "mwak") -> int:
        key = _text(kind).casefold() or "mwak"
        return self.number(f"bonuses.{key}.attack")

    def damage_bonus(self, kind: Any = "mwak") -> int:
        key = _text(kind).casefold() or "mwak"
        return self.number(f"bonuses.{key}.damage")

    def spell_attack_bonus(self) -> int:
        """法术攻击加值：优先 ``bonuses.msak.attack``，缺席时用 ``rsak``。"""
        melee = self.number("bonuses.msak.attack")
        ranged = self.number("bonuses.rsak.attack")
        return melee if melee else ranged

    def spell_save_dc_bonus(self) -> int:
        return self.number("bonuses.spell.dc")

    def roll_mode(self, ability: Any, kind: str = "check") -> int:
        """d20 模式：1 优势 / -1 劣势 / 0 普通（FVTT dnd5e 口径）。"""
        key = _text(ability).casefold()
        if not key:
            return 0
        return _as_int(self.number(f"abilities.{key}.{kind}.roll.mode"), 0)

    def traits(self, kind: Any) -> list[str]:
        raw = resolve_path(self.doc, f"traits.{_text(kind)}.value")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    # -- 派生接入 ---------------------------------------------------------
    def armor_class(self, base: int, *, explicit: bool, dex_mod: int = 0) -> int:
        """基础 AC → 效果处理后的 AC。

        ``ac.calc`` 覆盖（``mage`` = 13 + 敏捷、``natural`` = 10 + 敏捷）只在
        卡面没有显式 AC 时改写基础值：导入卡把算好的 AC 写进了
        ``sheet.armorClass``，那已经包含护甲，不能再被无甲公式推翻。
        """
        value = _as_int(base, 10)
        if not explicit:
            calc = self.ac_calc
            if calc == "mage":
                value = 13 + dex_mod
            elif calc == "natural":
                value = 10 + dex_mod
        return max(0, value + self.ac_bonus)

    def apply_attacks(self, attacks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """给攻击表补上攻击/伤害加值（近战 mwak、远程 rwak）。"""
        rows: list[dict[str, Any]] = []
        for attack in attacks or []:
            row = dict(attack) if isinstance(attack, Mapping) else None
            if row is None:
                continue
            kind = "rwak" if _text(row.get("kind")).casefold() == "ranged" else "mwak"
            attack_delta = self.attack_bonus(kind)
            damage_delta = self.damage_bonus(kind)
            if attack_delta:
                row["attack_bonus"] = _as_int(row.get("attack_bonus"), 0) + attack_delta
            if damage_delta:
                row["damage_bonus"] = _as_int(row.get("damage_bonus"), 0) + damage_delta
            rows.append(row)
        return rows

    def extend_traits(self, values: Any, kind: Any) -> list[str]:
        """合并列表型修正（抗性/免疫/易伤/语言），保序去重。"""
        merged: list[str] = []
        for source in (values, self.traits(kind)):
            for item in source if isinstance(source, (list, tuple, set)) else []:
                text = _text(item)
                if text and text not in merged:
                    merged.append(text)
        return merged

    def report(self) -> dict[str, Any]:
        """给 UI/日志/告警用的小结。"""
        return {
            "stats": dict(self.stats),
            "unsupported": sorted(set(self.unsupported))[:20],
            "deferred": [dict(item) for item in self.deferred[:20]],
            "custom": sorted(set(self.custom))[:20],
            "applied": [dict(item) for item in self.applied[:40]],
        }


def _apply_number(document: dict[str, Any], path: str, change: Mapping[str, Any]) -> str:
    """数值路径结算；返回 ``""`` 表示已应用，否则是不能应用的原因。"""
    number, reason = parse_number(change.get("value"))
    if number is None:
        return reason
    mode = _as_int(change.get("mode"), MODE_ADD)
    current = resolve_path(document, path)
    base = float(_as_int(current, 0)) if mode != MODE_OVERRIDE else 0.0
    if mode == MODE_MULTIPLY:
        # Foundry 的 multiply 在没有原值时按 1 起乘，避免把值乘成 0。
        base = float(_as_int(current, 1)) if current is not None else 1.0
        result = base * number
    elif mode == MODE_ADD:
        result = base + number
    elif mode == MODE_DOWNGRADE:
        result = min(base, number) if current is not None else number
    elif mode == MODE_UPGRADE:
        result = max(base, number) if current is not None else number
    else:
        result = number
    set_path(document, path, int(round(result)))
    return ""


def _apply_list(document: dict[str, Any], path: str, change: Mapping[str, Any]) -> str:
    mode = _as_int(change.get("mode"), MODE_ADD)
    values = _as_values(change.get("value"))
    if not values:
        return "empty"
    current = resolve_path(document, path)
    existing = [str(item) for item in current] if isinstance(current, list) else []
    if mode == MODE_ADD:
        for value in values:
            if value not in existing:
                existing.append(value)
    elif mode == MODE_OVERRIDE:
        existing = list(values)
    else:
        return f"mode-{mode}"
    set_path(document, path, existing)
    return ""


def _apply_text(document: dict[str, Any], path: str, change: Mapping[str, Any]) -> str:
    if _as_int(change.get("mode"), MODE_ADD) != MODE_OVERRIDE:
        return "mode-not-override"
    text = _text(change.get("value"))
    if not text:
        return "empty"
    set_path(document, path, text)
    return ""


def effective_sheet(sheet: Mapping[str, Any] | None) -> EffectiveSheet:
    """把卡面上的效果结算成一张计算视图（无效果时 ``empty=True``）。"""
    view = EffectiveSheet()
    source = sheet if isinstance(sheet, Mapping) else {}
    entries = effect_entries(source)
    if not entries:
        return view

    view.stats["documents"] = len(entries)
    pending: list[tuple[dict[str, Any], tuple[str, str], dict[str, Any]]] = []
    for effect, parent_equipped in entries:
        view.stats["changes"] += len(effect["changes"])
        if not is_active(effect, parent_equipped):
            continue
        view.stats["active"] += 1
        view.effects.append(effect)
        for change in effect["changes"]:
            pending.append((effect, classify_path(change["key"]), change))

    # 固定顺序：multiply → add → downgrade → upgrade → override（同级保序）。
    pending.sort(key=lambda row: MODE_ORDER.get(_as_int(row[2].get("mode"), MODE_ADD), 1))

    for effect, (kind, path), change in pending:
        mode = _as_int(change.get("mode"), MODE_ADD)
        record = {
            "key": change["key"],
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, str(mode)),
            "value": change.get("value"),
            "effect": effect.get("name"),
        }
        if mode == MODE_CUSTOM:
            view.stats["custom"] += 1
            view.custom.append(change["key"])
            continue
        if kind == "unsupported" or not path:
            view.stats["unsupported"] += 1
            view.unsupported.append(change["key"])
            continue
        if kind == "number":
            reason = _apply_number(view.doc, path, change)
        elif kind == "list":
            reason = _apply_list(view.doc, path, change)
        else:
            reason = _apply_text(view.doc, path, change)
        if reason == "formula":
            # 骰式加值（bless 的 +1d4 这类）不进派生值，单独记下来给 UI/日志。
            view.stats["deferred"] += 1
            record["reason"] = "formula"
            view.deferred.append(record)
            continue
        if reason:
            view.stats["deferred"] += 1
            record["reason"] = reason
            view.deferred.append(record)
            continue
        view.stats["applied"] += 1
        view.applied.append(record)
    return view


def effect_dice_bonus(view: EffectiveSheet, path: Any) -> str:
    """把某个 key 上被推迟的骰式加值拼成展示串（如 ``"+1d4"``）。"""
    target = mirror_path(path)
    parts = [
        _text(item.get("value"))
        for item in view.deferred
        if mirror_path(item.get("key")) == target and item.get("reason") == "formula"
    ]
    return " + ".join(part for part in parts if part)


__all__ = [
    "ABILITY_KEYS",
    "EffectiveSheet",
    "KNOWN_UNSUPPORTED_PREFIXES",
    "LIST_PATHS",
    "MODE_ADD",
    "MODE_CUSTOM",
    "MODE_DOWNGRADE",
    "MODE_LABELS",
    "MODE_MULTIPLY",
    "MODE_ORDER",
    "MODE_OVERRIDE",
    "MODE_UPGRADE",
    "NUMBER_PATHS",
    "TEXT_PATHS",
    "classify_path",
    "effect_dice_bonus",
    "effect_entries",
    "effective_sheet",
    "is_active",
    "mirror_path",
    "normalize_change",
    "normalize_effect",
    "parse_number",
    "resolve_path",
    "set_path",
    "unsupported_hint",
]
