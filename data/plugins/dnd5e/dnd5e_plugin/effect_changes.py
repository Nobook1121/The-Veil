"""FVTT Active Effect 兼容的「按路径改数值」通用效果引擎（schema 驱动）。

与 :mod:`dnd5e_plugin.effects` 的分工（两者互不替代）：

- ``effects.py``：**条件/战斗内效果**。认 5e 条件名（charmed、exhaustion…），
  把规则事实整理成核心 ``trpg_server.ruleset.effects`` 的效果实例，负责
  持续时间推进、回合 tick、结束豁免、专注连带移除。它回答「角色现在处于
  什么状态」。
- ``effect_changes.py``（本模块）：**数值改写引擎**。认 Foundry Active Effect
  的 ``changes`` 数据形态（``{key, mode, value}``），把「背着这枚戒指 AC +1」
  这类声明式修正算进派生值。它回答「算这个数的时候要加多少」。

## 设计：完整镜像 + 通用路径求值（不做白名单）

引擎内声明一份 **FVTT actor / item 数据模型镜像**（:data:`MIRROR_SCHEMA`），
路径求值就是在镜像上 ``get`` / ``set``：change 的 ``key`` 是 FVTT 文档路径，
去掉 ``[...]`` 下标后按点分段在 schema 上匹配。**命中 schema 就算「接住了」**，
不再是「在白名单里才算支持」：

- 命中 → 按 schema 声明的类型（number / list / text）结算并写进镜像；
- 未命中 → ``unsupported``（真的外来命名空间，我们连它的形状都不知道）。

### 三档消费分级（如实分类，不假装生效）

每个 change 结算后落在且只落在一个桶里：

===========  ============================================================
tier         含义
===========  ============================================================
``full``     计算结果真的进了我们的派生值（AC / 先攻 / HP / 豁免 / 检定 /
             攻击 / 伤害 / 抗性 / **属性值 / 熟练加值 / 分速 / 减伤 /
             语言 / 物品活动覆盖 / midi-qol 优势与暴击阈值** / 速度 /
             武器属性…），由 schema 节点的 ``note`` 标注消费方。
``stored``   镜像里记下了（名字 / 来源 / 路径 / 值都在），**当前没有数值
             消费方**：模块 ``flags.*`` 的非 midi-qol 子集、``ATL.*``
             前端字段、``activities[...]`` 未识别的子路径、骰式（``1d4``）
             加值、custom 模式、``movement.all`` 别名与 ``units`` 单位…
``unsupported`` 连镜像 schema 都没有的路径（外来命名空间）。
===========  ============================================================

``stored`` 的 ``reason`` 进一步说明「为什么没生效」：``unknown-path`` /
``no-consumer`` / ``no-item-scope`` / ``inactive`` / ``custom`` / ``formula``。

### 数据形态（导入器与手工写入共用，``statuses``/``duration`` 之外的键都可选）::

    {
      "id": "1QDjbtS7gIrWu5ks",
      "name": "Fighting Style: Defense",
      "transfer": true,           # 源自物品且应随物品生效（= 改 actor）
      "disabled": false,          # 物品效果自身被关掉
      "changes": [
        {"key": "system.attributes.ac.bonus", "mode": 2, "value": "+1"}
      ],
      "origin": {"item_id": "…", "source": "feat", "equipped": true}
    }

挂载位置：

- 角色卡 ``sheet.effect_changes``：不来自装备容器的物品效果（专长、种族特性、
  法术残留）以及手工施加的效果 → 改 **actor 镜像**；
- 装备条目 ``equipment[].effects``：来自 weapon/equipment/consumable/loot/
  tool/container 这些物品的效果 → ``transfer`` 的改 **actor 镜像**，
  其余（``system.properties`` / ``system.damage.base.*`` / ``activities[…]``）
  改 **该物品自己的镜像**（FVTT 语义：非 transfer 效果改的是物品文档本身）。

### 五种模式（与 Foundry ``ActiveEffect.MODES`` 同号）

===========  ==========================================================
mode         含义
===========  ==========================================================
0 ``CUSTOM``     不自动应用（原文留给宏/人工），记入 ``stored``
1 ``MULTIPLY``   现有值 × value
2 ``ADD``        现有值 + value
3 ``DOWNGRADE``  取较小值（min）
4 ``UPGRADE``    取较大值（max）
5 ``OVERRIDE``   直接覆盖（永远最后应用）
===========  ==========================================================

同一个 key 上的多条 change 按 ``MULTIPLY → ADD → DOWNGRADE → UPGRADE →
OVERRIDE`` 的固定顺序结算（同级保留源顺序），结果与效果挂载顺序无关。

列表型 key（``system.traits.dr.value`` 抗性、``traits.ci.value`` 条件免疫等）
只支持「加值/覆盖」：``ADD`` 逐项追加去重，``OVERRIDE`` 整体替换。

**回归铁律**：没有任何 ``full`` 级 change 时 ``empty=True``，所有接入点都短路
走原实现，派生值逐字节一致。
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

# --- 三档消费分级 ----------------------------------------------------------

TIER_FULL = "full"
TIER_STORED = "stored"
TIER_UNSUPPORTED = "unsupported"

#: 路径族作用域：``actor`` 改角色文档，``item`` 改物品文档（需要装备行）。
SCOPE_ACTOR = "actor"
SCOPE_ITEM = "item"

#: ``system.`` 前缀（FVTT actor/item 文档字段都在它下面）。
SYSTEM_PREFIX = "system."

#: 属性键（与 ``rules.ABILITY_KEYS`` 同口径，单列一份避免循环导入）。
ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")

#: 分速模型的速度键（``walk`` 继续映射到旧的单一速度字段，其余只在新模型里）。
SPEED_KEYS = ("walk", "fly", "swim", "climb", "burrow", "jump")

#: 分速里除 ``walk`` 之外的键（``walk`` 走 :meth:`EffectiveSheet.speed`）。
EXTRA_SPEED_KEYS = SPEED_KEYS[1:]

#: 只在 ``OVERRIDE``（mode 5）语义下才有消费方的数值路径。
#:
#: ``system.attributes.hp.value`` 是「当前生命值」：ADD/UPGRADE 这类增量在
#: 当前值上算不出稳定的绝对值（房间结算自己会写 HP），所以只有效果显式
#: **覆盖**时才当作当前 HP 使用，其余模式如实落 ``stored``。
OVERRIDE_ONLY_PATHS = ("system.attributes.hp.value",)

#: 攻击类别（与 dnd5e ``bonuses.*`` 同口径）。
ATTACK_KINDS = ("all", "mwak", "rwak", "msak", "rsak")

# --- midi-qol 条件语义子集（模块级唯一映射表，勿在别处再抄一份） ------------

_MIDI_PREFIX = "flags.midi-qol."

#: ``flags.midi-qol.{advantage|disadvantage}.*`` → ``(作用点, 子类, d20 模式)``。
#:
#: 作用点：``attack``（攻击掷骰）/ ``check``（属性检定）/ ``save``（豁免）；
#: 子类：攻击类别（``all``/``mwak``/``rwak``/``msak``/``rsak``）或属性键；
#: 模式：``1`` 优势 / ``-1`` 劣势。值按 FVTT 习惯是布尔（``true``/``false``）。
MIDI_QOL_ROLL_MODES: dict[str, tuple[str, str, int]] = {}
for _side, _side_mode in (("advantage", 1), ("disadvantage", -1)):
    for _attack_kind in ATTACK_KINDS:
        MIDI_QOL_ROLL_MODES[f"{_MIDI_PREFIX}{_side}.attack.{_attack_kind}"] = (
            "attack",
            _attack_kind,
            _side_mode,
        )
    for _where in ("check", "save"):
        MIDI_QOL_ROLL_MODES[f"{_MIDI_PREFIX}{_side}.ability.{_where}.all"] = (
            _where,
            "all",
            _side_mode,
        )
        for _ability in ABILITY_KEYS:
            MIDI_QOL_ROLL_MODES[f"{_MIDI_PREFIX}{_side}.ability.{_where}.{_ability}"] = (
                _where,
                _ability,
                _side_mode,
            )
del _side, _side_mode, _attack_kind, _where, _ability

#: ``flags.midi-qol.critical.*`` → ``(攻击类别, 暴击阈值)``：置真即 19+ 暴击。
MIDI_QOL_CRITICAL_KEYS: dict[str, tuple[str, int]] = {
    f"{_MIDI_PREFIX}critical.{kind}": (kind, 19) for kind in ATTACK_KINDS
}

#: 显式暴击阈值（``flags.midi-qol.criticalThreshold``，数字）。
MIDI_QOL_CRITICAL_THRESHOLD_KEY = f"{_MIDI_PREFIX}criticalThreshold"

#: 阈值夹取范围：2 以下没有意义（不可能），20 就是原版口径。
MIDI_QOL_CRITICAL_MIN = 2
MIDI_QOL_CRITICAL_MAX = 20

#: 镜像文档的顶层键：这些开头的路径已经是「文档根路径」，不再补 ``system.``。
_ROOT_KEYS = (
    "system",
    "flags",
    "ATL",
    "name",
    "activities",
    "_id",
    "img",
    "type",
    "items",
    "effects",
    "prototypeToken",
    "ownership",
    "folder",
    "sort",
    "delta",
)


# --- 镜像 schema（声明式） -------------------------------------------------


class Node:
    """镜像 schema 的一个节点。

    ``kind``：``object``（容器）/ ``number`` / ``list`` / ``text`` / ``any``。
    ``tier``：命中后默认落哪一档（``full`` 表示有真实消费方，``note`` 写明）。
    ``scope``：``actor`` / ``item``（物品作用域路径需要挂在装备行上）。
    ``children``：子节点；``"*"`` 是通配（``abilities.*``、``bonuses.skills.*``）。

    同一个节点既可以当叶子也可以当容器（``bonuses.abilities.save`` 既是
    「全豁免加值」数字，也有 ``save.str`` 这样的单项子路径）。
    """

    __slots__ = ("kind", "tier", "note", "scope", "children")

    def __init__(
        self,
        kind: str = "object",
        tier: str = TIER_STORED,
        note: str = "",
        scope: str = SCOPE_ACTOR,
        children: Mapping[str, "Node"] | None = None,
    ) -> None:
        self.kind = kind
        self.tier = tier
        self.note = note
        self.scope = scope
        self.children: dict[str, Node] = dict(children or {})

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Node({self.kind!r}, {self.tier!r}, children={len(self.children)})"


def _obj(children: Mapping[str, Any], *, note: str = "", scope: str = SCOPE_ACTOR) -> Node:
    return Node("object", TIER_STORED, note, scope, dict(children))


def _num(
    tier: str = TIER_FULL,
    note: str = "",
    *,
    scope: str = SCOPE_ACTOR,
    children: Mapping[str, Any] | None = None,
) -> Node:
    return Node("number", tier, note, scope, children)


def _lst(tier: str = TIER_FULL, note: str = "", *, scope: str = SCOPE_ACTOR) -> Node:
    return Node("list", tier, note, scope)


def _txt(tier: str = TIER_STORED, note: str = "", *, scope: str = SCOPE_ACTOR) -> Node:
    return Node("text", tier, note, scope)


def _any(tier: str = TIER_STORED, note: str = "", *, scope: str = SCOPE_ACTOR) -> Node:
    return Node("any", tier, note, scope)


def _per_ability(**kwargs: Any) -> Node:
    """``{"*": node}``：``abilities.*`` / ``bonuses.skills.*`` 这类通配子节点。"""
    return Node("object", TIER_STORED, children={"*": _obj(kwargs)})


#: ``bonuses.abilities.{save,check}`` 既是全量数字、也有单项子路径。
def _ability_bonus(note: str) -> Node:
    return _num(
        TIER_FULL,
        note,
        children={"*": _num(TIER_FULL, "单项加值（同上的单项版本）")},
    )


def _ability_node() -> Node:
    return _obj(
        {
            "value": _num(
                TIER_FULL,
                "属性值合并：EffectiveSheet.merged_abilities 并进派生口径"
                "（豁免/检定/攻击/法术 DC/AC(DEX)/HP(CON)/先攻/被动感知），"
                "卡面显示合并值、原始值留 base_abilities",
            ),
            "mod": _num(
                TIER_STORED,
                "调整值叠加：与 value（底层属性值）不能混写，需要在每个取值点分别接，本刀只接 value",
            ),
            "min": _num(TIER_STORED),
            "max": _num(TIER_STORED),
            "prof": _num(TIER_STORED, "熟练标记：派生只看卡面 proficiencies"),
            "proficient": _num(TIER_STORED, "同上（FVTT 官方字段）"),
            "dc": _obj(
                {
                    "value": _num(TIER_STORED),
                    "bonus": _num(TIER_STORED, "能力 DC 加值：房间没有独立的能力 DC 取值点"),
                }
            ),
            "check": _obj(
                {
                    "bonus": _num(TIER_FULL, "checks.check_bonus（属性检定加值）"),
                    "roll": _obj(
                        {
                            "mode": _num(TIER_FULL, "checks.roll_mode：d20 检定模式（1 优势 / -1 劣势）"),
                            "min": _num(TIER_STORED),
                            "max": _num(TIER_STORED),
                        }
                    ),
                }
            ),
            "save": _obj(
                {
                    "bonus": _num(TIER_FULL, "checks.save_bonus（属性豁免加值）"),
                    "roll": _obj(
                        {
                            "mode": _num(TIER_FULL, "checks.roll_mode：豁免 d20 模式（1 优势 / -1 劣势）"),
                            "min": _num(TIER_STORED),
                            "max": _num(TIER_STORED),
                        }
                    ),
                }
            ),
        }
    )


def _skill_node() -> Node:
    return _obj(
        {
            "ability": _txt(TIER_STORED, "技能关联属性：检定路由由卡面 skill 表决定"),
            "value": _num(TIER_STORED, "技能总加值（派生字段）：改它等于改 proficiency，未接"),
            "proficient": _num(TIER_STORED, "熟练标记：派生只看卡面 proficiencies"),
            "expertise": _num(TIER_STORED),
            "bonus": _num(TIER_FULL, "checks.check_bonus（system.skills.*.bonus）"),
            "roll": _obj(
                {
                    "mode": _num(TIER_STORED, "技能级 d20 模式：checks 目前只按属性取 roll.mode"),
                    "min": _num(TIER_STORED),
                    "max": _num(TIER_STORED),
                }
            ),
            "passive": _num(TIER_STORED, "被动值由 10 + 检定加值派生"),
        }
    )


def _movement_node() -> Node:
    return _obj(
        {
            "walk": _num(TIER_FULL, "单一速度模型：combat.speed_ft / speeds.walk（EffectsSheet.speed）"),
            "fly": _num(TIER_FULL, "分速模型：EffectsSheet.speeds → combat.speeds / 卡面详情速度行 / KP 摘要"),
            "swim": _num(TIER_FULL, "分速模型：同上（combat.speeds / 卡面 / KP 摘要）"),
            "climb": _num(TIER_FULL, "分速模型：同上"),
            "burrow": _num(TIER_FULL, "分速模型：同上"),
            "jump": _num(TIER_FULL, "分速模型：同上（跳跃距离按 speeds.jump 输出）"),
            "hover": _num(TIER_STORED, "悬停是标志位（有/无），房间移动不需要它"),
            "all": _num(
                TIER_FULL,
                "所有速度别名：确定性公式（如 max(30, 30+@level)）作为分速基础值，"
                "见 EffectsSheet.speed/speeds；带骰式的公式留待使用时机",
            ),
            "units": _txt(TIER_STORED, "单位换算（mi/km）：房间按英尺结算"),
            "special": _txt(TIER_STORED),
        }
    )


def _damage_reduction_node() -> Node:
    """``system.traits.dm``（``{amount, type}`` 伤害减免）。"""
    return _obj(
        {
            "amount": _num(
                TIER_FULL,
                "减伤阶段：combat._apply_damage 在抗性/易伤之后扣减（不扣成负数）",
            ),
            "type": _txt(
                TIER_FULL,
                "减伤适用的伤害类型（空 = 所有类型）：combat._apply_damage 按伤害类型匹配",
            ),
            "bypasses": _lst(TIER_STORED, "伤害穿透（magical/silver）：伤害管线没有材质模型"),
        }
    )


def _traits_node() -> Node:
    return _obj(
        {
            "dr": _obj(
                {
                    "value": _lst(TIER_FULL, "combat._apply_damage 伤害抗性"),
                    "custom": _txt(TIER_STORED),
                    "bypasses": _lst(TIER_STORED, "伤害穿透（magical/silver）：伤害管线没有材质模型"),
                }
            ),
            "dv": _obj({"value": _lst(TIER_FULL, "combat._apply_damage 伤害易伤")}),
            "di": _obj({"value": _lst(TIER_FULL, "combat._apply_damage 伤害免疫")}),
            "dm": _damage_reduction_node(),
            "ci": _obj(
                {
                    "value": _lst(TIER_FULL, "conditions：免疫的条件不会被施加（effects.apply）"),
                    "custom": _txt(TIER_STORED),
                }
            ),
            "languages": _obj(
                {
                    "value": _lst(
                        TIER_FULL,
                        "语言清单：schema.pack 并进卡面 proficiencies.languages（详情页签）+ KP 摘要行",
                    ),
                    "custom": _txt(
                        TIER_FULL,
                        "自定义语言文本：同上（按逗号/顿号拆成条目）",
                    ),
                    "communication": _txt(TIER_STORED),
                }
            ),
            "size": _txt(TIER_STORED, "体型在房间结算里由模块决定"),
            "armorProf": _obj({"value": _lst(TIER_STORED, "护甲熟练：只有力量需求影响速度，未接")}),
            "weaponProf": _obj({"value": _lst(TIER_STORED, "武器熟练由卡面 proficiencies.weapons 决定")}),
            "toolProf": _obj({"value": _lst(TIER_STORED)}),
            "saves": _obj({"value": _lst(TIER_STORED, "豁免熟练：check_bonus 只看卡面 proficiencies.saves")}),
        }
    )


def _damage_node() -> Node:
    return _obj(
        {
            "base": _obj(
                {
                    "number": _num(
                        TIER_FULL,
                        "武器基础伤害骰数：物品作用域下并入装备行（combat._weapon_attack）",
                        scope=SCOPE_ITEM,
                    ),
                    "denomination": _num(
                        TIER_FULL,
                        "武器基础伤害骰面：同上",
                        scope=SCOPE_ITEM,
                    ),
                    "bonus": _num(TIER_STORED, "基础伤害固定加值：伤害加值走 bonuses.*.damage", scope=SCOPE_ITEM),
                    "types": _lst(TIER_STORED, "伤害类型改写：物品作用域下未接", scope=SCOPE_ITEM),
                    "custom": _obj({"enabled": _num(TIER_STORED), "formula": _txt(TIER_STORED)}, scope=SCOPE_ITEM),
                    "scaling": _obj({"mode": _txt(TIER_STORED), "number": _num(TIER_STORED)}, scope=SCOPE_ITEM),
                },
                scope=SCOPE_ITEM,
            ),
            "versatile": _obj(
                {
                    "number": _num(TIER_STORED, "双手伤害骰：装备行 versatile_damage 未接效果", scope=SCOPE_ITEM),
                    "denomination": _num(TIER_STORED, "同上", scope=SCOPE_ITEM),
                },
                scope=SCOPE_ITEM,
            ),
        },
        scope=SCOPE_ITEM,
    )


def _activity_node() -> Node:
    """dnd5e v6 物品活动（``activities[...]``）。

    注意路径归一：``activities[attack].attack.bonus`` 去掉下标后是
    ``activities.attack.bonus``——活动 id 与字段名在点分路径上重合，所以
    ``activities`` 下的第一段一律按「某个活动」处理（:data:`_ACTIVITY_WILDCARD`），
    多活动同名字段会互相覆盖（记录内容仍在，只是镜像只有一格）。

    认得的子路径并进武器行（:meth:`EffectiveSheet.weapon_row`）；
    其余子路径如实 ``stored``（活动运行时我们不模拟：消耗、靶向、触发条件…）。
    """
    return _obj(
        {
            "*": _obj(
                {
                    "attack": _obj(
                        {
                            "bonus": _num(
                                TIER_FULL,
                                "物品活动攻击加值：weapon_row → 武器行 attack_bonus（整体覆盖）",
                                scope=SCOPE_ITEM,
                            ),
                            "ability": _txt(
                                TIER_FULL,
                                "物品活动攻击属性：weapon_row → 武器行 attack_ability（spellcasting 表示施法属性）",
                                scope=SCOPE_ITEM,
                            ),
                            "critical": _obj({"threshold": _num(TIER_STORED, scope=SCOPE_ITEM)}, scope=SCOPE_ITEM),
                        },
                        scope=SCOPE_ITEM,
                    ),
                    "damage": _obj(
                        {
                            "parts": _lst(
                                TIER_FULL,
                                "物品活动伤害骰：weapon_row → 武器行 damage.dice（取第一条的骰式）",
                                scope=SCOPE_ITEM,
                            ),
                            "types": _lst(TIER_STORED, "伤害类型改写：武器行的伤害类型由数据包决定", scope=SCOPE_ITEM),
                        },
                        scope=SCOPE_ITEM,
                    ),
                    "range": _obj(
                        {
                            "value": _num(
                                TIER_FULL,
                                "物品活动射程：weapon_row → 武器行 range.normal_ft（战斗摘要/AI 读取）",
                                scope=SCOPE_ITEM,
                            ),
                            "long": _num(
                                TIER_FULL,
                                "物品活动远程射程：weapon_row → 武器行 range.long_ft",
                                scope=SCOPE_ITEM,
                            ),
                            "units": _txt(TIER_STORED, "射程单位：房间按英尺结算", scope=SCOPE_ITEM),
                        },
                        scope=SCOPE_ITEM,
                    ),
                    "*": _any(
                        TIER_STORED,
                        "其余物品活动字段（save/consumption/target/activation…）：运行时不模拟，整条记录",
                        scope=SCOPE_ITEM,
                    ),
                },
                scope=SCOPE_ITEM,
            )
        },
        scope=SCOPE_ITEM,
    )


def _flags_node() -> Node:
    """``flags.*``：midi-qol 的优势/暴击子集有消费方，其余只记录。

    映射表就是 :data:`MIDI_QOL_ROLL_MODES` / :data:`MIDI_QOL_CRITICAL_KEYS`
    （模块级唯一一份，schema 节点由它们生成，避免两处漂移）。
    """
    def midi_parts(key: str) -> list[str]:
        """``flags.midi-qol.advantage.attack.all`` → ``["advantage","attack","all"]``。"""
        return key[len(_MIDI_PREFIX) :].split(".")

    roll_nodes: list[tuple[list[str], Node]] = []
    for key, (_where, _sub, mode) in MIDI_QOL_ROLL_MODES.items():
        side = "优势" if mode > 0 else "劣势"
        roll_nodes.append(
            (
                midi_parts(key),
                _num(TIER_FULL, f"midi-qol {side}：checks.roll_mode / combat 掷骰 d20 模式"),
            )
        )
    critical_nodes = [
        (
            midi_parts(key),
            _num(TIER_FULL, "midi-qol 暴击：combat/checks 的暴击阈值（19+）"),
        )
        for key in MIDI_QOL_CRITICAL_KEYS
    ]
    threshold_nodes = [
        (
            midi_parts(MIDI_QOL_CRITICAL_THRESHOLD_KEY),
            _num(TIER_FULL, "midi-qol 暴击阈值：combat/checks 的暴击阈值（夹在 2..20）"),
        )
    ]
    # midi_parts 返回的是去掉 "flags.midi-qol." 前缀的段。
    grant_nodes = [
        (
            midi_parts("flags.midi-qol.grants.advantage.attack.all"),
            _num(TIER_FULL, "目标侧：所有攻击者对持有者的攻击有优势（combat 结算时读）"),
        ),
        (
            midi_parts("flags.midi-qol.grants.disadvantage.attack.all"),
            _num(TIER_FULL, "目标侧：指定类型攻击者对持有者的攻击有劣势（值是类型列表）"),
        ),
    ]
    midi = _node_of_tree(
        _path_tree([*roll_nodes, *critical_nodes, *threshold_nodes, *grant_nodes])
    )
    midi.children["*"] = _any(
        TIER_STORED,
        "其余 midi-qol 标志（grants/optional/ActivityOverTime…）：只记录，不模拟模块运行时",
    )
    dnd5e_flags = _obj(
        {
            "halflingLucky": _num(
                TIER_FULL,
                "半身人幸运：攻击 d20 掷出 1 时重掷一次（combat.resolve_attack_action）",
            ),
            "halflingNimbleness": _num(
                TIER_STORED,
                "半身人灵巧：可穿过大生物空间——本作没有按生物体积做占位阻挡，无所可改",
            ),
            "*": _any(TIER_STORED, "其余 dnd5e flags：只记录"),
        }
    )
    return _obj(
        {
            "midi-qol": midi,
            "dnd5e": dnd5e_flags,
            "*": _any(TIER_STORED, "模块/系统标志（dnd5e flags）：只记录，不模拟模块运行时"),
        }
    )


def _path_tree(entries: Iterable[tuple[list[str], Node]]) -> dict[str, Any]:
    """``[(["flags","midi-qol",…], node), …]`` → 嵌套 dict（叶子是 :class:`Node`）。"""
    tree: dict[str, Any] = {}
    for parts, node in entries:
        cursor = tree
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = node
    return tree


def _node_of_tree(tree: Mapping[str, Any]) -> Node:
    """把 :func:`_path_tree` 的嵌套 dict 递建成 schema 节点树。"""
    children: dict[str, Node] = {}
    for key, value in tree.items():
        children[key] = _node_of_tree(value) if isinstance(value, dict) else value
    return _obj(children)


#: FVTT actor（``system.`` 下）+ 物品（``system.`` 下）的完整镜像模型。
MIRROR_SCHEMA: Node = Node(
    "object",
    TIER_STORED,
    "FVTT actor 文档镜像根",
    SCOPE_ACTOR,
    {
        "system": _obj(
            {
                # --- actor：属性 / 衍生 -------------------------------------------------
                "abilities": _per_ability(**_ability_node().children),
                "attributes": _obj(
                    {
                        "ac": _obj(
                            {
                                "bonus": _num(
                                    TIER_FULL,
                                    "schema._effective_armor_class / combat / statblock 的 AC 加值",
                                ),
                                "calc": _txt(
                                    TIER_FULL,
                                    "无甲 AC 算法覆盖（mage/natural），schema.derive",
                                ),
                                "flat": _num(TIER_STORED, "固定 AC 覆盖：卡面显式 armorClass 优先，未接"),
                                "formula": _txt(TIER_STORED),
                                "min": _num(TIER_STORED, "AC 下限未接"),
                                "cover": _num(TIER_STORED),
                            }
                        ),
                        "hp": _obj(
                            {
                                "bonus": _num(
                                    TIER_FULL,
                                    "最大生命加值：schema.pack/derive 的 maxHp、combat.max_hp",
                                ),
                                "max": _num(
                                    TIER_FULL,
                                    "最大生命：EffectiveSheet.max_hp 先取它（override 覆盖），再叠 hp.bonus；"
                                    "卡面 hp.max 仍存基础值（不重复叠加）",
                                ),
                                "value": _num(
                                    TIER_FULL,
                                    "当前生命：只有 override（mode 5）才直接写 pack.currentHp / combat.hp，"
                                    "其余模式落 stored；结果夹在 [0, maxHp]",
                                ),
                                "temp": _num(TIER_STORED),
                                "tempmax": _num(TIER_STORED),
                                "tempMax": _num(TIER_STORED),
                                "formula": _txt(TIER_STORED),
                            }
                        ),
                        "init": _obj(
                            {
                                "bonus": _num(TIER_FULL, "schema.pack/derive 的 initiative"),
                                "ability": _txt(TIER_STORED, "先攻属性切换：导入时记备注"),
                                "roll": _obj(
                                    {
                                        "mode": _num(TIER_STORED, "先攻 d20 模式：房间先攻不掷优势/劣势"),
                                        "min": _num(TIER_STORED),
                                        "max": _num(TIER_STORED),
                                    }
                                ),
                            }
                        ),
                        "movement": _movement_node(),
                        "speed": _obj(
                            {
                                "value": _num(TIER_FULL, "旧版速度字段：等同 movement.walk（EffectsSheet.speed）"),
                                "units": _txt(TIER_STORED),
                                "special": _txt(TIER_STORED),
                            }
                        ),
                        "prof": _num(
                            TIER_FULL,
                            "熟练加值：EffectiveSheet.proficiency_bonus → schema 派生 proficiencyBonus/"
                            "法术 DC、checks 豁免与检定、combat 武器攻击加值",
                        ),
                        "spellcasting": _num(TIER_STORED, "施法属性调整值覆盖：施法档案读卡面 spellcasting.ability"),
                        "death": _obj(
                            {
                                "success": _num(TIER_STORED),
                                "failure": _num(TIER_STORED),
                                "bonus": _num(TIER_STORED),
                                "roll": _num(TIER_STORED, "死亡豁免：房间没有濒死流程"),
                            }
                        ),
                        "attunement": _obj({"max": _num(TIER_STORED, "同调上限：装备行不校验同调")}),
                        "senses": _obj(
                            {
                                "darkvision": _num(TIER_STORED),
                                "blindsight": _num(TIER_STORED),
                                "tremorsense": _num(TIER_STORED),
                                "truesight": _num(TIER_STORED),
                                "units": _txt(TIER_STORED),
                                "special": _txt(TIER_STORED),
                            }
                        ),
                        "hd": _obj({"spent": _num(TIER_STORED), "max": _num(TIER_STORED)}),
                        "exhaustion": _num(TIER_STORED, "力竭等级由 conditions 子系统管理"),
                        "inspiration": _num(TIER_STORED),
                        "loyalty": _num(TIER_STORED),
                        "concentration": _obj({"limit": _num(TIER_STORED), "save": _num(TIER_STORED)}),
                        "encumbrance": _obj({"max": _num(TIER_STORED), "value": _num(TIER_STORED)}),
                        "currency": _obj({"value": _num(TIER_STORED)}),
                    }
                ),
                "bonuses": _obj(
                    {
                        "mwak": _obj(
                            {
                                "attack": _num(TIER_FULL, "combat 近战武器攻击加值（attack_bonus）"),
                                "damage": _num(TIER_FULL, "combat 近战武器伤害加值（damage_bonus）"),
                            }
                        ),
                        "rwak": _obj(
                            {
                                "attack": _num(TIER_FULL, "combat 远程武器攻击加值"),
                                "damage": _num(TIER_FULL, "combat 远程武器伤害加值"),
                            }
                        ),
                        "msak": _obj(
                            {
                                "attack": _num(TIER_FULL, "combat 施法攻击加值（近战法术）"),
                                "damage": _num(TIER_STORED, "法术伤害加值：法术伤害走 spells.py 的骰式"),
                            }
                        ),
                        "rsak": _obj(
                            {
                                "attack": _num(TIER_FULL, "combat 施法攻击加值（远程法术，msak 缺席时）"),
                                "damage": _num(TIER_STORED, "同上"),
                            }
                        ),
                        "spell": _obj(
                            {
                                "dc": _num(TIER_FULL, "schema.derive 的 spellSaveDC"),
                                "attack": _num(TIER_STORED, "法术攻击整体加值未接（读 msak/rsak）"),
                            }
                        ),
                        "abilities": _obj(
                            {
                                "save": _ability_bonus("checks.save_bonus：全豁免加值"),
                                "saving_throw": _ability_bonus("同上（别名）"),
                                "check": _ability_bonus("checks.check_bonus：全检定加值"),
                                "ability_check": _ability_bonus("同上（别名）"),
                            }
                        ),
                        "skills": _per_ability(check=_num(TIER_FULL, "checks.check_bonus 单项技能检定加值")),
                        "weapon": _obj({"attack": _num(TIER_STORED), "damage": _num(TIER_STORED)}),
                    }
                ),
                "skills": _per_ability(**_skill_node().children),
                "traits": _traits_node(),
                "details": _obj(
                    {
                        "level": _num(TIER_STORED),
                        "xp": _obj({"value": _num(TIER_STORED)}),
                        "biography": _obj(
                            {"value": _txt(TIER_STORED), "public": _txt(TIER_STORED)}
                        ),
                        "alignment": _txt(TIER_STORED),
                        "race": _txt(TIER_STORED),
                        "background": _txt(TIER_STORED),
                        "originalClass": _txt(TIER_STORED),
                        "appearance": _txt(TIER_STORED),
                        "trait": _txt(TIER_STORED),
                        "ideal": _txt(TIER_STORED),
                        "bond": _txt(TIER_STORED),
                        "flaw": _txt(TIER_STORED),
                        "gender": _txt(TIER_STORED),
                        "age": _txt(TIER_STORED),
                        "height": _txt(TIER_STORED),
                        "weight": _txt(TIER_STORED),
                        "eyes": _txt(TIER_STORED),
                        "hair": _txt(TIER_STORED),
                        "skin": _txt(TIER_STORED),
                    }
                ),
                "currency": _obj(
                    {
                        "pp": _num(TIER_STORED),
                        "gp": _num(TIER_STORED),
                        "ep": _num(TIER_STORED),
                        "sp": _num(TIER_STORED),
                        "cp": _num(TIER_STORED),
                    }
                ),
                "resources": _obj(
                    {
                        "legact": _obj({"max": _num(TIER_STORED), "spent": _num(TIER_STORED)}),
                        "legres": _obj({"max": _num(TIER_STORED), "spent": _num(TIER_STORED)}),
                        "lair": _obj({"value": _num(TIER_STORED), "initiative": _num(TIER_STORED)}),
                    }
                ),
                # --- item：物品文档自己的字段（需要物品作用域） -------------------------
                "properties": _lst(
                    TIER_FULL,
                    "武器属性（finesse / ammunition / versatile / mgc…）：并入装备行后影响攻击属性与远程判定",
                    scope=SCOPE_ITEM,
                ),
                "activities": _activity_node(),
                "damage": _damage_node(),
                "equipped": _num(TIER_STORED, "装备开关本身由装备行 equipped 决定", scope=SCOPE_ITEM),
                "identified": _num(TIER_STORED, scope=SCOPE_ITEM),
                "quantity": _num(TIER_STORED, scope=SCOPE_ITEM),
                "weight": _obj({"value": _num(TIER_STORED), "units": _txt(TIER_STORED)}, scope=SCOPE_ITEM),
                "price": _obj({"value": _num(TIER_STORED), "denomination": _txt(TIER_STORED)}, scope=SCOPE_ITEM),
                "armor": _obj(
                    {
                        "value": _num(TIER_STORED, "护甲 AC 基数：装备行 base_ac 来自导入", scope=SCOPE_ITEM),
                        "dex": _num(TIER_STORED, scope=SCOPE_ITEM),
                        "magicalBonus": _num(TIER_STORED, scope=SCOPE_ITEM),
                    },
                    scope=SCOPE_ITEM,
                ),
                "weaponType": _txt(TIER_STORED, scope=SCOPE_ITEM),
                "proficiency": _txt(TIER_STORED, scope=SCOPE_ITEM),
                "uses": _obj({"value": _num(TIER_STORED), "max": _num(TIER_STORED)}, scope=SCOPE_ITEM),
                "attunement": _num(TIER_STORED, scope=SCOPE_ITEM),
            }
        ),
        "name": _txt(
            TIER_FULL,
            "物品改名（如 Shillelagh）：weapon_row 把模板里的 {} 换成原名后用于攻击行名",
            scope=SCOPE_ITEM,
        ),
        "activities": _activity_node(),
        "_id": _txt(TIER_STORED),
        "img": _txt(TIER_STORED),
        "type": _txt(TIER_STORED),
        "folder": _txt(TIER_STORED),
        "sort": _num(TIER_STORED),
        "delta": _any(TIER_STORED, "增量数据（delta）：不展开"),
        "flags": _flags_node(),
        "ATL": _any(TIER_STORED, "Token 外观（Tokenizer/ATL）：本引擎不渲染 Token"),
        "prototypeToken": _any(TIER_STORED, "原型 Token 数据：房间用地图棋子，不读"),
        "ownership": _any(TIER_STORED, "权限表：房间权限走核心 ACL"),
        "items": _any(TIER_STORED, "物品数组：整条记录保留"),
        "effects": _any(TIER_STORED, "嵌套效果：整条记录保留"),
    },
)


#: 参考表：外部命名空间 → 出处说明（只用于文案，不参与计算）。
KNOWN_UNSUPPORTED_PREFIXES: dict[str, str] = {
    "flags.": "模块/系统标志（midi-qol、dnd5e flags）",
    "ATL.": "Token 外观（Tokenizer/ATL）",
    "activities[": "物品活动（dnd5e v6 Activities）",
    "items[": "物品数组",
    "effects[": "嵌套效果",
}

_DICE_RE = re.compile(r"\d*\s*d\s*\d", re.IGNORECASE)
#: ``activities[attack]`` → ``activities`` + ``attack``：下标单独成段，活动 id
#: 才不会被字段名吃掉（``activities[attack].attack.bonus`` 是四段而不是三段）。
_INDEX_RE = re.compile(r"^(.*?)\[([^\]]*)\]$")


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
    # FVTT 的布尔标志（midi-qol 的 ``advantage.*`` / ``critical.*``）常以字符串
    # 形式落盘（``"true"``）；真/假按 1/0 结算，其余文本仍走 ``text`` 原因。
    if raw.casefold() in {"true", "false"}:
        return (1.0 if raw.casefold() == "true" else 0.0), ""
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
    """把 FVTT 文档路径归一成镜像根路径（``[...]`` 下标去掉，空段丢弃）。

    已经是根路径（``flags.*`` / ``ATL.*`` / ``name``…）原样返回；
    ``attributes.ac.bonus`` 这类相对路径补上 ``system.``。
    """
    parts = _path_parts(key)
    return ".".join(parts)


def _path_parts(path: Any) -> list[str]:
    raw = _text(path)
    if not raw:
        return []
    parts: list[str] = []
    for part in raw.split("."):
        part = part.strip()
        if not part:
            continue
        # ``activities[attack].attack.ability`` → ``activities.attack.attack.ability``
        match = _INDEX_RE.match(part)
        if match:
            base = match.group(1).strip()
            index = match.group(2).strip()
            if base:
                parts.append(base)
            if index:
                parts.append(index)
            continue
        parts.append(part)
    return parts


def _norm(path: Any) -> str:
    """读取用的路径归一：不是根路径就补 ``system.``。"""
    parts = _path_parts(path)
    if not parts:
        return ""
    if parts[0] in _ROOT_KEYS:
        return ".".join(parts)
    return ".".join([SYSTEM_PREFIX.rstrip("."), *parts])


def resolve_path(document: Mapping[str, Any], path: Any) -> Any:
    """按点分路径取值；中途缺失返回 ``None``（不抛异常）。"""
    node: Any = document
    for part in _path_parts(path):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def set_path(document: dict[str, Any], path: Any, value: Any) -> bool:
    """按点分路径写值（缺层自动补 dict）；路径非法返回 ``False``。"""
    parts = _path_parts(path)
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


# --- schema 匹配 -----------------------------------------------------------


def _child(node: Node | None, part: str) -> Node | None:
    if node is None or not node.children:
        return None
    found = node.children.get(part)
    if found is not None:
        return found
    return node.children.get("*")


def _walk(parts: Iterable[str], root: Node | None = None) -> Node | None:
    """在镜像 schema 上走路径；任一段没声明返回 ``None``。

    ``any`` 节点是**终止节点**（``flags.*`` / ``ATL.*`` / ``activities[…].*``）：
    命中它之后整条路径都算接住了，再往下走就是模块自己的命名空间。
    """
    node: Node | None = root or MIRROR_SCHEMA
    for part in parts:
        if node is None:
            return None
        if node.kind == "any":
            return node
        node = _child(node, part)
    return node


def classify_path(key: Any, mount: str = SCOPE_ACTOR) -> tuple[str, str, str, str, str]:
    """返回 ``(种类, 镜像路径, 分级, 说明, 路径作用域)``。

    种类：``number`` / ``list`` / ``text`` / ``unsupported``。
    分级：``full`` / ``stored`` / ``unsupported``（见模块文档的三档分类）。
    ``mount`` 是效果挂载位置（``actor`` / ``item``）：物品作用域路径只有挂在
    装备行上才有消费方，挂在角色级时降级为 ``stored``（如实记录、不算生效）。
    """
    raw = _text(key)
    if not raw:
        return "unsupported", "", TIER_UNSUPPORTED, "空路径", SCOPE_ACTOR
    parts = _path_parts(raw)
    path = ".".join(parts)
    node = _walk(parts)
    if node is None or node.kind == "object":
        return (
            "unsupported",
            path,
            TIER_UNSUPPORTED,
            "镜像 schema 里没有这段路径（外来命名空间）",
            SCOPE_ACTOR,
        )
    kind = node.kind if node.kind in {"number", "list", "text"} else "text"
    tier = node.tier
    note = node.note
    if node.scope == SCOPE_ITEM and mount != SCOPE_ITEM:
        tier = TIER_STORED
        scope_note = "物品作用域的路径：效果得挂在装备行上才能改到那件武器（这里是角色级效果）"
        note = f"{note}；{scope_note}" if note else scope_note
    return kind, path, tier, note, node.scope


def unsupported_hint(key: Any) -> str:
    """给外部命名空间的 key 一句出处说明（文案用）。"""
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


def _row_key(row: Any) -> str:
    """装备行的稳定标识：``id`` 优先，否则名字（大小写归一的裸名）。"""
    if not isinstance(row, Mapping):
        return ""
    for key in ("id", "name"):
        text = _text(row.get(key))
        if text:
            return f"{_text(key)}:{text.casefold()}"
    return ""


def _row_keys(row: Any) -> list[str]:
    """一行的所有可用键（``id:`` / ``name:``），匹配时全部试一遍。"""
    keys: list[str] = []
    row_key = _row_key(row)
    if row_key:
        keys.append(row_key)
    if isinstance(row, Mapping):
        name = _text(row.get("name"))
        if name:
            candidate = f"name:{name.casefold()}"
            if candidate not in keys:
                keys.append(candidate)
    return keys


# --- 规范化物品文档（``row["item"]``） -------------------------------------
#
# 导入器把 FVTT 的物品文档规范化后随装备行保留：``{id, name, type, img,
# system, effects, flags, activities}``（``system`` 原样、``activities`` 在
# 规范位置）。引擎这一侧对它的读取优先级是
# **文档 > 条目级效果 > 扁平字段**：文档缺的字段才回落到行上的扁平值，
# 没有 ``item`` 的旧卡走到的是同一条「没有任何文档」分支，行为逐字节不变。


def row_item_document(row: Any) -> dict[str, Any]:
    """装备行上的规范化物品文档（``row["item"]``）；缺失/形态不对给 ``{}``。"""
    if not isinstance(row, Mapping):
        return {}
    document = row.get("item")
    return dict(document) if isinstance(document, Mapping) else {}


def _deep_overlay(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """效果镜像文档深合并到物品文档上（dict 递归；列表/标量以镜像为准）。"""
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(value, Mapping):
            base[key] = _deep_overlay(dict(current) if isinstance(current, Mapping) else {}, value)
        else:
            base[key] = value
    return base


def _numeric_or_none(value: Any) -> int | None:
    """``3`` / ``"3"`` / ``3.0`` → ``3``；公式串（``@scale…``）→ ``None``。"""
    if isinstance(value, bool) or value is None:
        return None
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _item_uses_from_document(system: Mapping[str, Any]) -> dict[str, Any] | None:
    """``system.uses`` / ``system.charges`` → ``{value,max,recovery}``（只展示不消耗）。

    与导入器 ``convert_actors._item_uses`` 同口径（那边直接写行；这里兜住
    手工粘贴 / 旧导入没有 ``uses`` 健的卡）。``max`` 是公式串（``@scale…``）
    时原样保留——公式由另一刀结算，这里不猜数值。
    """
    for key in ("uses", "charges"):
        raw = system.get(key)
        if isinstance(raw, Mapping):
            max_value = raw.get("max")
            spent = raw.get("spent")
            current = raw.get("value")
            if max_value in (None, "") and spent in (None, "") and current in (None, ""):
                continue
            max_int = _numeric_or_none(max_value)
            value: int | None = None
            if _numeric_or_none(current) is not None:
                value = max(0, _numeric_or_none(current) or 0)
            elif max_int is not None:
                value = max(0, max_int - _as_int(spent, 0))
            recovery = [
                _text(item.get("period") or item.get("type"))
                if isinstance(item, Mapping)
                else _text(item)
                for item in (raw.get("recovery") or [])
                if isinstance(item, Mapping) or _text(item)
            ]
            return {
                "value": value if value is not None else 0,
                "max": max_int if max_int is not None else _text(max_value),
                "recovery": [row for row in recovery if row],
                "source": key,
            }
        number = _numeric_or_none(raw)
        if number is not None:
            return {"value": max(0, number), "max": max(0, number), "recovery": [], "source": key}
    return None


def _item_activity_kind(doc: Mapping[str, Any]) -> str:
    """第一个活动的 ``attack.type.value``（``melee`` / ``ranged``）；没有给空串。"""
    activity = _first_activity(doc.get("activities"))
    if activity is None:
        system = doc.get("system") if isinstance(doc.get("system"), Mapping) else {}
        activity = _first_activity(system.get("activities"))
    if activity is None:
        return ""
    attack = activity.get("attack") if isinstance(activity.get("attack"), Mapping) else {}
    attack_type = attack.get("type")
    kind = _text(
        (attack_type.get("value") if isinstance(attack_type, Mapping) else attack_type)
    ).casefold()
    return kind if kind in {"melee", "ranged"} else ""


def effect_entries(sheet: Mapping[str, Any] | None) -> list[tuple[dict[str, Any], bool | None]]:
    """收集卡面所有效果，返回 ``[(效果, 来源装备是否已装备)]``。

    第二项为 ``None`` 表示来自 ``sheet.effect_changes``（角色级，不跟装备行）。
    效果字典上额外带三个引擎内部字段：``mount``（``actor``/``item``）、
    ``row_key``（来源装备行键）、``row_is_weapon``（该行是不是武器）。
    """
    source = sheet if isinstance(sheet, Mapping) else {}
    entries: list[tuple[dict[str, Any], bool | None]] = []
    for raw in source.get("effect_changes") or []:
        effect = normalize_effect(raw)
        if effect is not None:
            effect["mount"] = SCOPE_ACTOR
            effect["row_key"] = ""
            effect["row_is_weapon"] = False
            entries.append((effect, None))
    equipment = source.get("equipment")
    for row in equipment if isinstance(equipment, list) else []:
        if not isinstance(row, Mapping):
            continue
        equipped = bool(row.get("equipped"))
        row_key = _row_key(row)
        # 导入的装备行不带 ``category``（种类由库条目决定），只有卡面显式写了
        # 非 weapon 时才认定「这件物品不会被战斗读取」。没写就按可读处理。
        category = _text(row.get("category")).casefold()
        row_is_weapon = category in {"", "weapon"}
        for raw in row.get("effects") or []:
            effect = normalize_effect(raw)
            if effect is not None:
                effect["mount"] = SCOPE_ITEM
                effect["row_key"] = row_key
                effect["row_is_weapon"] = row_is_weapon
                entries.append((effect, equipped))
    return entries


def is_active(effect: Mapping[str, Any], parent_equipped: bool | None) -> bool:
    """激活判定（actor 作用域的路径）。

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


def item_scope_active(effect: Mapping[str, Any], parent_equipped: bool | None) -> bool:
    """装备行上「物品作用域」路径的激活判定。

    FVTT 里非 ``transfer`` 的物品效果改的是**物品文档自己**（Shillelagh 改棍子
    的属性/伤害骰就是这个形态），只要这件物品装备着就成立。角色作用域的路径
    仍走 :func:`is_active`（``transfer`` 才改 actor），语义不变。
    """
    if effect.get("disabled"):
        return False
    if parent_equipped is None:
        # 角色级效果没有物品作用域，交给 classify_path 降级成 stored。
        return False
    if not parent_equipped:
        return False
    origin = effect.get("origin") if isinstance(effect.get("origin"), Mapping) else {}
    return origin.get("equipped") is not False


# --- 计算视图 --------------------------------------------------------------

_TIER_REASONS = {
    "unknown-path": "镜像 schema 里没有这段路径",
    "no-consumer": "路径已记录，但当前没有数值消费方",
    "no-item-scope": "需要物品作用域（挂在装备行的效果才能改到那件武器）",
    "inactive": "效果未激活（disabled / 未装备 / 非 transfer 未手动施加）",
    "custom": "custom 模式：Foundry 交给宏，不自动应用",
    "formula": "骰式/公式加值不猜数字，原文保留",
    "empty": "值为空，没有可结算的内容",
    "text": "值不是数字（原文保留）",
    "mode-not-override": "文本字段只接受 override 模式（其余模式原文保留）",
    "override-only": "该路径只在 override（mode 5）下才有消费方，其余模式如实记录不生效",
}


class EffectiveSheet:
    """``effective_sheet(sheet)`` 的返回值：一张「改完数」的镜像投影。

    所有接入点只读这个视图，不自己堆加值逻辑：

    - :meth:`armor_class` / :attr:`initiative_bonus` / :meth:`check_bonus` /
      :meth:`attack_bonus` / :meth:`damage_bonus` / :meth:`spell_attack_bonus`
    - :meth:`speed` / :meth:`max_hp` / :meth:`condition_immunities`
    - :meth:`traits`、:meth:`apply_attacks`、:meth:`extend_traits`、
      :meth:`weapon_row`
    """

    def __init__(self) -> None:
        self.doc: dict[str, Any] = {}
        #: 物品作用域镜像：``{row_key: {system: {...}}}``
        self.items: dict[str, dict[str, Any]] = {}
        # 公式求值用的 roll data（卡面摊平后的 ``@levels/@abilities…``）；
        # 没有效果时为空字典，公式路径不参与。
        self.roll_data: dict[str, Any] = {}
        #: 生效（``full``）的 change 记录。
        self.applied: list[dict[str, Any]] = []
        #: 已记录但没有数值消费方的 change 记录。
        self.stored: list[dict[str, Any]] = []
        #: 镜像 schema 都没命中的 key。
        self.unsupported: list[str] = []
        #: 骰式/公式（``1d4``）加值：不进派生整数，单独给 UI/日志。
        self.deferred: list[dict[str, Any]] = []
        #: custom 模式的 key。
        self.custom: list[str] = []
        #: 所有 change 的结算记录（顺序 = :func:`effect_entries` 的 change 顺序）。
        self.records: list[dict[str, Any]] = []
        self.effects: list[dict[str, Any]] = []
        #: 每条路径最后一次写入用的模式（解释 override / delta 用）。
        self.modes: dict[str, int] = {}
        self.stats: dict[str, int] = {
            "documents": 0,
            "active": 0,
            "changes": 0,
            "applied": 0,
            "stored": 0,
            "unsupported": 0,
            "deferred": 0,
            "custom": 0,
        }

    # -- 空视图判定 -------------------------------------------------------
    @property
    def empty(self) -> bool:
        """没有任何 ``full`` 级 change（接入点据此短路走原实现）。"""
        return not self.applied

    # -- 读值 -------------------------------------------------------------
    def value(self, path: Any, default: Any = 0) -> Any:
        found = resolve_path(self.doc, _norm(path))
        return default if found is None else found

    def number(self, path: Any, default: int = 0) -> int:
        found = resolve_path(self.doc, _norm(path))
        return default if found is None else _as_int(found, default)

    def _delta(self, path: Any) -> int:
        """按写入模式解释镜像值：override → 绝对值；其余 → 增量。"""
        found = resolve_path(self.doc, _norm(path))
        if found is None:
            return 0
        if self.modes.get(_norm(path)) == MODE_OVERRIDE:
            return _as_int(found, 0)
        return _as_int(found, 0)

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

    def max_hp(self, base: int) -> int:
        """基础生命上限 → 效果处理后的上限。

        顺序：``attributes.hp.max``（override 直接覆盖，其余模式当增量/上下限）
        → ``attributes.hp.bonus``（加值或覆盖）。两条路径都只在有 ``full``
        级 change 时参与（空视图原样返回 ``base``）。
        """
        value = max(1, _as_int(base, 1))
        if not self.empty:
            value = self._apply_numeric("attributes.hp.max", value, floor=1)
            bonus = self.max_hp_bonus
            if self.modes.get(_norm("attributes.hp.bonus")) == MODE_OVERRIDE and bonus:
                return max(1, bonus)
            value += max(0, bonus)
        return max(1, value)

    def hp_value(self) -> int | None:
        """``attributes.hp.value`` 覆盖后的当前生命；没有 override 时为 ``None``。"""
        path = _norm("attributes.hp.value")
        found = resolve_path(self.doc, path)
        if found is None or self.modes.get(path) != MODE_OVERRIDE:
            return None
        return max(0, _as_int(found, 0))

    def _apply_numeric(self, path: str, base: int, *, floor: int = 0) -> int:
        """把镜像里某条数值 change 按它自己的模式解释到 ``base`` 上。

        override → 绝对值；upgrade/downgrade → 取高/取低；multiply → 乘；
        其余（add）→ 增量。镜像里没有这条路径时原样返回 ``base``。
        """
        normalized = _norm(path)
        found = resolve_path(self.doc, normalized)
        if found is None:
            return base
        value = _as_int(found, 0)
        mode = self.modes.get(normalized)
        if mode == MODE_OVERRIDE:
            result = value
        elif mode == MODE_DOWNGRADE:
            result = min(base, value)
        elif mode == MODE_UPGRADE:
            result = max(base, value)
        elif mode == MODE_MULTIPLY:
            result = int(round(base * value)) if value else base
        else:
            result = base + value
        return max(floor, result)

    # -- 属性值合并（``abilities.*.value``） -------------------------------
    def merged_abilities(self, base: Mapping[str, Any] | None) -> dict[str, int]:
        """基础属性值 → 合并了 ``abilities.*.value`` 的属性值。

        ``value`` 是**底层属性值**（0-30），``mod`` 是加值叠加——两者不混写。
        没有属性值 change 时返回基础值的整数副本（空视图时逐字节等价）。
        """
        source = base if isinstance(base, Mapping) else {}
        result: dict[str, int] = {}
        for key in ABILITY_KEYS:
            raw = source.get(key)
            if raw is None:
                for alt, alt_value in source.items():
                    if _text(alt).casefold() == key:
                        raw = alt_value
                        break
            result[key] = _as_int(raw, 10)
        if self.empty:
            return result
        for key in ABILITY_KEYS:
            path = _norm(f"abilities.{key}.value")
            if resolve_path(self.doc, path) is None:
                continue
            result[key] = min(30, self._apply_numeric(f"abilities.{key}.value", result[key], floor=0))
        return result

    def merged_sheet(self, sheet: Mapping[str, Any] | None) -> dict[str, Any]:
        """卡面副本：``abilities`` 换成合并后的值（供 checks/派生统一口径）。"""
        source = dict(sheet) if isinstance(sheet, Mapping) else {}
        if self.empty:
            return source
        abilities = self.merged_abilities(source.get("abilities") or source.get("attributes"))
        if isinstance(source.get("abilities"), Mapping):
            source["abilities"] = abilities
        elif abilities:
            source["abilities"] = abilities
        return source

    # -- 熟练加值（``attributes.prof``） -----------------------------------
    def proficiency_bonus(self, level: Any) -> int:
        """等级 → 熟练加值（``attributes.prof`` 的覆盖/加值参与结算）。"""
        from dnd5e_plugin.rules import proficiency_bonus as _base_proficiency

        base = _base_proficiency(level)
        if self.empty:
            return base
        return self._apply_numeric("attributes.prof", base, floor=0)

    # -- 分速模型（``attributes.movement.*``） -----------------------------
    def speeds(self, base: int, default: int = 30, base_speeds: Mapping[str, Any] | None = None) -> dict[str, int]:
        """基础速度 → 分速表 ``{walk, fly, swim, climb, burrow, jump}``。

        ``walk`` 继续走单一速度模型（:meth:`speed`，含旧 ``attributes.speed.value``
        别名），其余键只在新模型里出现：没有 change 的键不写进结果（卡面据此
        只显示有值的分速）。
        """
        known: dict[str, int] = {}
        for key, value in (base_speeds or {}).items():
            name = _text(key).casefold()
            if name in SPEED_KEYS:
                known[name] = max(0, _as_int(value, 0))
        # walk 只从调用方给的**基础**速度算；``base_speeds`` 必须是效果前的
        # 原始值（卡面 ``base_speeds``），否则二次派生会把加值叠两次。
        out: dict[str, int] = {"walk": self.speed(_as_int(base, default), default)}
        if self.empty:
            return out
        has_all = resolve_path(self.doc, _norm("attributes.movement.all")) is not None
        for key in EXTRA_SPEED_KEYS:
            path = _norm(f"attributes.movement.{key}")
            if resolve_path(self.doc, path) is None:
                if has_all:
                    # 只有 ``movement.all`` 时，各分速以它为基线。
                    out[key] = out["walk"]
                continue
            base_value = _as_int(known.get(key), out["walk"] if has_all else 0)
            out[key] = self._apply_numeric(path, base_value, floor=0)
        return out

    def speed(self, base: int, default: int = 30) -> int:
        """基础速度 → 效果处理后的速度（``movement.walk`` / 旧 ``speed.value``）。"""
        value = max(0, _as_int(base, default))
        if self.empty:
            return value
        walk = resolve_path(self.doc, _norm("attributes.movement.walk"))
        if walk is not None:
            if self.modes.get(_norm("attributes.movement.walk")) == MODE_OVERRIDE:
                value = _as_int(walk, value)
            else:
                value += _as_int(walk, 0)
        legacy = resolve_path(self.doc, _norm("attributes.speed.value"))
        if legacy is not None and walk is None:
            value = _as_int(legacy, value)
        if walk is None:
            # ``movement.all``：所有分速的别名（确定性公式已结算成数值）。
            all_value = resolve_path(self.doc, _norm("attributes.movement.all"))
            if all_value is not None:
                computed, reason = formula_number(all_value, self.roll_data)
                if computed is None:
                    computed = _as_int(all_value, value)
                value = max(0, int(computed))
        return max(0, value)

    # -- 减伤（``traits.dm``） ---------------------------------------------
    def damage_reduction(self, damage_type: Any = "") -> int:
        """``traits.dm`` → 本次伤害的减伤数值（类型不匹配时 0）。

        ``type`` 为空 = 所有伤害类型；否则只对该类型（大小写不敏感）生效。
        """
        amount = self.number("traits.dm.amount")
        if amount <= 0:
            return 0
        declared = _text(self.value("traits.dm.type", "")).casefold()
        dtype = _text(damage_type).casefold()
        if declared and declared not in {"all", "any", "所有"} and declared != dtype:
            return 0
        return max(0, int(amount))

    def damage_reduction_row(self, base: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """战斗者字段 ``damage_reduction = {amount, type}``；没有减伤时 ``None``。"""
        amount = max(0, _as_int((base or {}).get("amount"), 0))
        declared = _text(self.value("traits.dm.type", "")).casefold()
        if not self.empty:
            amount = self.number("traits.dm.amount") or amount
        if amount <= 0:
            return None
        return {"amount": int(amount), "type": declared}

    def save_bonus(self, ability: Any = "") -> int:
        total = self.number("bonuses.abilities.save") + self.number("bonuses.abilities.saving_throw")
        key = _text(ability).casefold()
        if key:
            total += self.number(f"bonuses.abilities.save.{key}")
            total += self.number(f"bonuses.abilities.saving_throw.{key}")
            total += self.number(f"abilities.{key}.save.bonus")
        return total

    def check_bonus(self, ability: Any = "", skill: Any = "") -> int:
        total = self.number("bonuses.abilities.check") + self.number("bonuses.abilities.ability_check")
        key = _text(ability).casefold()
        if key:
            total += self.number(f"bonuses.abilities.check.{key}")
            total += self.number(f"bonuses.abilities.ability_check.{key}")
            total += self.number(f"abilities.{key}.check.bonus")
        skill_key = _text(skill).casefold().replace(" ", "-").replace("_", "-")
        if skill_key:
            total += self.number(f"bonuses.skills.{skill_key}.check")
            total += self.number(f"skills.{skill_key}.bonus")
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
        """d20 模式：1 优势 / -1 劣势 / 0 普通（FVTT dnd5e 口径）。

        ``abilities.*.{check,save}.roll.mode`` 与 midi-qol 的
        ``flags.midi-qol.{advantage,disadvantage}.ability.*`` 相加：两边同时
        给相反方向时互相抵消（和为 0），与「优势劣势抵消」的规则一致。
        """
        key = _text(ability).casefold()
        where = "save" if _text(kind).casefold() == "save" else "check"
        mode = _as_int(self.number(f"abilities.{key}.{where}.roll.mode"), 0) if key else 0
        return mode + self.midi_roll_mode(where, key or "all")

    def midi_roll_mode(self, where: str, sub: Any = "all") -> int:
        """midi-qol 子集 → d20 模式（``where`` = attack/check/save）。"""
        scope = _text(where).casefold()
        if not scope:
            return 0
        target = _text(sub).casefold() or "all"
        total = 0
        for key, (node_where, node_sub, mode) in MIDI_QOL_ROLL_MODES.items():
            if node_where != scope:
                continue
            if node_sub != "all" and node_sub != target:
                continue
            if _as_int(resolve_path(self.doc, _norm(key)), 0) <= 0:
                continue
            total += mode
        return total

    def attack_roll_mode(self, kind: Any = "mwak") -> int:
        """攻击掷骰的 d20 模式（``kind`` = mwak/rwak/msak/rsak/all）。"""
        return self.midi_roll_mode("attack", _text(kind).casefold() or "all")

    def critical_threshold(self, kind: Any = "mwak") -> int:
        """暴击阈值（默认 20）：midi-qol ``critical.*`` 给 19、``criticalThreshold`` 覆盖。"""
        target = _text(kind).casefold() or "all"
        threshold = MIDI_QOL_CRITICAL_MAX
        for key, (node_kind, value) in MIDI_QOL_CRITICAL_KEYS.items():
            if node_kind != "all" and node_kind != target:
                continue
            if _as_int(resolve_path(self.doc, _norm(key)), 0) > 0:
                threshold = min(threshold, value)
        raw = resolve_path(self.doc, _norm(MIDI_QOL_CRITICAL_THRESHOLD_KEY))
        if raw is not None:
            number = _as_int(raw, threshold)
            if number > 0:
                threshold = min(threshold, max(MIDI_QOL_CRITICAL_MIN, min(MIDI_QOL_CRITICAL_MAX, number)))
        return threshold

    def traits(self, kind: Any) -> list[str]:
        raw = resolve_path(self.doc, _norm(f"traits.{_text(kind)}.value"))
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def condition_immunities(self) -> set[str]:
        """``traits.ci.value`` → 归一的条件 id 集合（小写）。"""
        return {_text(item).casefold() for item in self.traits("ci") if _text(item)}

    def languages(self) -> list[str]:
        """``traits.languages.value`` + ``traits.languages.custom`` → 语言清单。"""
        merged: list[str] = []
        for item in self.traits("languages"):
            text = _text(item)
            if text and text not in merged:
                merged.append(text)
        custom = _text(self.value("traits.languages.custom", ""))
        for part in re.split(r"[,，、;；/]", custom):
            text = part.strip()
            if text and text not in merged:
                merged.append(text)
        return merged

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

    def weapon_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """装备行 → 应用了物品作用域效果 + 规范化物品文档的副本。

        读取优先级：**行上的物品文档（``row["item"]``）> 条目级效果 > 扁平字段**。

        - 物品文档来自导入器（``convert_actors._item_document``）：``system``
          原样、``activities`` 在规范位置；没有 ``item`` 的旧卡行为不变。
        - 物品作用域的效果改的是**该装备行自己的物品文档**（Shillelagh 改棍子
          就是这形态），所以效果先深合并到文档上（效果赢在有值的路径上），
          再由文档统一投影到行的扁平字段。
        - 文档/效果都没有的字段保留行上原值（扁平字段兜底）。

        消费的字段：

        - ``system.properties`` → 行 ``properties``（灵巧/弹药/双手…）
        - ``system.damage.base.{number,denomination,types}`` → 行 ``damage.{dice,type}``
        - ``system.damage.versatile.{number,denomination}`` → 行 ``versatile_damage``
        - ``system.uses`` / ``system.charges`` → 行 ``uses``（``{value,max,recovery}``，
          只展示不消耗；行上已有则保留行上的）
        - ``activities[…].attack.bonus`` → 行 ``attack_bonus``（整体覆盖攻击加值）；
          公式串（非纯数字）解析不出来时原样留在 ``attack_bonus_formula``
        - ``activities[…].attack.ability`` → 行 ``attack_ability``（``spellcasting`` = 施法属性）
        - ``activities[…].damage.parts`` → 行 ``damage.dice``（取第一条的骰式）
        - ``activities[…].range.{value,long}`` → 行 ``range`` + ``range_override``
        """
        if not isinstance(row, Mapping):
            return {}
        mirror: dict[str, Any] | None = None
        for key in _row_keys(row):
            found = self.items.get(key)
            if isinstance(found, Mapping):
                mirror = found
                break
        document = row_item_document(row)
        if not mirror and not document:
            # 旧卡（没有 item）且没有物品作用域效果：逐字节原样返回。
            return dict(row)
        merged = _deep_overlay(dict(document), mirror) if mirror else dict(document)
        return self._merge_item_document(dict(row), merged)

    @staticmethod
    def _merge_item_document(row: dict[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
        """物品文档 → 行扁平字段（文档优先，行上原值兜底）。"""
        # 物品改名（Shillelagh 之类）：``name`` 路径写进物品镜像后与原名不同，
        # 模板里的 ``{}`` / ``{0}`` 换成原名，供攻击行展示。
        doc_name = _text(doc.get("name"))
        row_name = _text(row.get("name")) or doc_name
        if doc_name and row_name and doc_name != row_name:
            display = doc_name.replace("{}", row_name).replace("{0}", row_name).strip()
            if display:
                row["name"] = display
        system = doc.get("system") if isinstance(doc.get("system"), Mapping) else {}
        extra_props = _as_values(system.get("properties"))
        if extra_props:
            current = [_text(item).casefold() for item in row.get("properties") or []]
            merged = list(row.get("properties") or [])
            for prop in extra_props:
                if prop.casefold() not in current:
                    merged.append(prop)
                    current.append(prop.casefold())
            row["properties"] = merged
        damage_doc = system.get("damage") if isinstance(system.get("damage"), Mapping) else {}
        base = damage_doc.get("base") if isinstance(damage_doc.get("base"), Mapping) else {}
        number = base.get("number")
        denomination = base.get("denomination")
        types = _as_values(base.get("types"))
        if number is not None or denomination is not None or types:
            damage = dict(row.get("damage") or {}) if isinstance(row.get("damage"), Mapping) else {}
            if number is not None or denomination is not None:
                cur_number, cur_denom = _split_dice(damage.get("dice"))
                new_number = _as_int(number, cur_number) if number is not None else cur_number
                new_denom = _as_int(denomination, cur_denom) if denomination is not None else cur_denom
                if new_number and new_denom:
                    damage["dice"] = f"{new_number}d{new_denom}"
            if types and not _text(damage.get("type")):
                damage["type"] = types[0]
            row["damage"] = damage
        if not _text(row.get("versatile_damage")):
            versatile = damage_doc.get("versatile") if isinstance(damage_doc.get("versatile"), Mapping) else {}
            v_number = _as_int(versatile.get("number"), 0)
            v_denom = _as_int(versatile.get("denomination"), 0)
            if v_number and v_denom:
                row["versatile_damage"] = f"{v_number}d{v_denom}"
        if not isinstance(row.get("uses"), Mapping):
            uses = _item_uses_from_document(system)
            if uses is not None:
                row["uses"] = uses
        if not _text(row.get("kind")):
            kind = _item_activity_kind(doc)
            if kind:
                row["kind"] = kind
        # 物品级射程（``system.range.{value,long}``）先投影；活动若也给了射程，
        # 由下面 ``_merge_activity`` 覆盖（活动优先于物品级）。
        doc_range = system.get("range") if isinstance(system.get("range"), Mapping) else {}
        doc_normal = doc_range.get("value")
        doc_long = doc_range.get("long")
        if doc_normal is not None or doc_long is not None:
            current_range = dict(row.get("range") or {}) if isinstance(row.get("range"), Mapping) else {}
            if doc_normal is not None:
                current_range["normal_ft"] = max(0, _as_int(doc_normal, _as_int(current_range.get("normal_ft"), 0)))
            if doc_long is not None:
                current_range["long_ft"] = max(0, _as_int(doc_long, _as_int(current_range.get("long_ft"), 0)))
            row["range"] = current_range
            row["range_override"] = True
        # ``activities[attack].attack.bonus`` 去掉下标后是根路径（没有 ``system.``
        # 前缀），``system.activities[...]`` 的写法带前缀：两处都认。
        activity = _first_activity(system.get("activities")) or _first_activity(doc.get("activities"))
        if activity is not None:
            row = EffectiveSheet._merge_activity(row, activity)
        return row

    @staticmethod
    def _merge_activity(row: dict[str, Any], activity: Mapping[str, Any]) -> dict[str, Any]:
        """把一件物品活动的 ``attack`` / ``damage`` / ``range`` 覆盖并进武器行。"""
        attack = activity.get("attack") if isinstance(activity.get("attack"), Mapping) else {}
        if attack.get("bonus") is not None:
            number, reason = parse_number(attack.get("bonus"))
            if number is not None and not reason:
                row["attack_bonus"] = int(round(number))
            else:
                # 公式串（``@prof + @abilities.dex.mod`` 之类）：公式由另一刀结算，
                # 这里只把原文留下（不猜数值，也不覆盖行上的既有加值）。
                raw = _text(attack.get("bonus"))
                if raw:
                    row["attack_bonus_formula"] = raw
        ability = _text(attack.get("ability")).casefold()
        if ability:
            row["attack_ability"] = ability
        damage = activity.get("damage") if isinstance(activity.get("damage"), Mapping) else {}
        dice = _first_dice(damage.get("parts"))
        if dice:
            current = dict(row.get("damage") or {}) if isinstance(row.get("damage"), Mapping) else {}
            current["dice"] = dice
            row["damage"] = current
        attack_range = activity.get("range") if isinstance(activity.get("range"), Mapping) else {}
        normal = attack_range.get("value")
        long_range = attack_range.get("long")
        if normal is not None or long_range is not None:
            current_range = dict(row.get("range") or {}) if isinstance(row.get("range"), Mapping) else {}
            existing_normal = _as_int(current_range.get("normal_ft"), 0)
            existing_long = _as_int(current_range.get("long_ft"), 0)
            if normal is not None:
                current_range["normal_ft"] = max(0, _as_int(normal, existing_normal))
            if long_range is not None:
                current_range["long_ft"] = max(0, _as_int(long_range, existing_long))
            row["range"] = current_range
            # 武器行的射程只有被文档/效果给过才随攻击行输出（旧卡输出逐字节不变）。
            row["range_override"] = True
        return row

    def merged_languages(self, values: Any) -> list[str]:
        """卡面语言清单 + ``traits.languages.value/custom``（保序去重）。

        列表型加法天然幂等：重复打包不会把同一门语言记两遍。
        """
        merged: list[str] = []
        for source in (values, self.languages()):
            for item in source if isinstance(source, (list, tuple, set)) else []:
                text = _text(item)
                if text and text not in merged:
                    merged.append(text)
        return merged

    def extend_traits(self, values: Any, kind: Any) -> list[str]:
        """合并列表型修正（抗性/免疫/易伤/条件免疫/语言），保序去重。"""
        merged: list[str] = []
        for source in (values, self.traits(kind)):
            for item in source if isinstance(source, (list, tuple, set)) else []:
                text = _text(item)
                if text and text not in merged:
                    merged.append(text)
        return merged

    def item_doc(self, row_key: str) -> dict[str, Any]:
        """取（必要时建）某装备行的物品作用域镜像文档。"""
        key = _text(row_key)
        doc = self.items.get(key)
        if not isinstance(doc, dict):
            doc = {}
            if key:
                self.items[key] = doc
        return doc

    def report(self) -> dict[str, Any]:
        """给 UI/日志/告警用的小结。"""
        return {
            "stats": dict(self.stats),
            "unsupported": sorted(set(self.unsupported))[:20],
            "stored": [dict(item) for item in self.stored[:40]],
            "deferred": [dict(item) for item in self.deferred[:20]],
            "custom": sorted(set(self.custom))[:20],
            "applied": [dict(item) for item in self.applied[:40]],
        }


def weapon_row(row: Mapping[str, Any], view: EffectiveSheet | None = None) -> dict[str, Any]:
    """装备行 → 应用了物品文档（与可选效果视图）的副本（模块级入口）。

    ``combat._player_attacks`` 用它：没有效果视图时也要把
    ``row["item"]`` 的 ``activities`` / ``damage`` / ``range`` / ``uses``
    投影到扁平字段上。``view`` 为空时等价于「只有文档、没有 effect」。
    """
    if view is not None:
        return view.weapon_row(row)
    return EffectiveSheet().weapon_row(row)


def _first_activity(activities: Any) -> dict[str, Any] | None:
    """物品镜像里的第一个活动（``{activity_id: {attack/damage/range…}}``）。"""
    if not isinstance(activities, Mapping):
        return None
    for value in activities.values():
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _first_dice(parts: Any) -> str:
    """``["2d6 + @mod", …]`` → ``"2d6"``（取第一条里的骰式，公式部分不猜）。"""
    rows = parts if isinstance(parts, (list, tuple)) else [parts]
    for row in rows:
        text = _text(row)
        if not text:
            continue
        match = re.search(r"\d*\s*d\s*\d+", text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", "", match.group(0)).casefold()
    return ""


def _split_dice(value: Any) -> tuple[int, int]:
    """``"1d8"`` → ``(1, 8)``；解析不了给 ``(1, 4)``（与 combat 的兜底一致）。"""
    text = _text(value).casefold()
    match = re.match(r"^\s*(\d+)\s*d\s*(\d+)\s*$", text)
    if not match:
        return 1, 4
    return max(1, _as_int(match.group(1), 1)), max(1, _as_int(match.group(2), 4))


_DICE_FORMULA = re.compile(r"\d*\s*d\s*\d+", re.IGNORECASE)


def formula_number(
    value: Any, roll_data: Mapping[str, Any] | None
) -> tuple[int | None, str]:
    """公式串 → 数值（只结算**确定性**公式）。

    - 不含骰式的公式（``@abilities.dex.mod + 2``、``max(30, 30 + @level)``）：
      用 :mod:`dnd5e_plugin.formula` 求值后返回数字（四舍五入）。
    - 含骰式的公式（``+1d4``）：FVTT 是每次使用时掷，不在派生阶段掷——
      返回 ``(None, "formula")`` 如实记成「使用时机结算」，避免把随机数
      烘进卡面。
    - 公式模块缺席/求值失败：``(None, "formula")``。
    """
    if not isinstance(value, str) or not value.strip():
        return None, "formula"
    if _DICE_FORMULA.search(value):
        return None, "formula"
    try:
        from dnd5e_plugin import formula as _formula
    except Exception:  # noqa: BLE001 - 模块缺席按不可结算处理
        return None, "formula"
    if not _formula.is_formula(value):
        return None, "formula"
    result = _formula.evaluate(value, dict(roll_data or {}))
    if not result.get("ok") or not isinstance(result.get("value"), (int, float)):
        return None, "formula"
    return int(round(float(result["value"]))), ""


def _apply_number(
    document: dict[str, Any],
    path: str,
    change: Mapping[str, Any],
    roll_data: Mapping[str, Any] | None = None,
) -> str:
    """数值路径结算；返回 ``""`` 表示已应用，否则是不能应用的原因。"""
    number, reason = parse_number(change.get("value"))
    if number is None:
        # 纯数字解析不了时再看是不是公式（确定性公式在这里结算）。
        number, reason = formula_number(change.get("value"), roll_data)
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
    """文本路径：只接受覆盖（其余模式原样记录，不改值）。"""
    if _as_int(change.get("mode"), MODE_ADD) != MODE_OVERRIDE:
        return "mode-not-override"
    text = _text(change.get("value"))
    if not text:
        return "empty"
    set_path(document, path, text)
    return ""


def _apply_any(document: dict[str, Any], path: str, change: Mapping[str, Any]) -> str:
    """``any`` 路径（flags/ATL/activities…）：原样记下值，不做运算。"""
    set_path(document, path, change.get("value"))
    return ""


def effective_sheet(sheet: Mapping[str, Any] | None) -> EffectiveSheet:
    """把卡面上的效果结算成一张计算视图（无 ``full`` 级 change 时 ``empty=True``）。"""
    view = EffectiveSheet()
    source = sheet if isinstance(sheet, Mapping) else {}
    try:
        from dnd5e_plugin.formula import roll_data_from_sheet

        view.roll_data = roll_data_from_sheet(source)
    except Exception:  # noqa: BLE001 - 公式模块缺席不影响效果结算
        view.roll_data = {}
    entries = effect_entries(source)
    if not entries:
        return view

    view.stats["documents"] = len(entries)
    pending: list[tuple[int, dict[str, Any], bool | None, dict[str, Any]]] = []
    for effect, parent_equipped in entries:
        view.stats["changes"] += len(effect["changes"])
        if is_active(effect, parent_equipped) or item_scope_active(effect, parent_equipped):
            view.stats["active"] += 1
            view.effects.append(effect)
        for change in effect["changes"]:
            pending.append((len(pending), effect, parent_equipped, change))

    # 结算顺序固定：multiply → add → downgrade → upgrade → override（同级保序）；
    # 记录仍按卡面原有顺序落位，README/UI 才能逐条对齐（``annotate_sheet``）。
    pending.sort(key=lambda row: MODE_ORDER.get(_as_int(row[3].get("mode"), MODE_ADD), 1))
    records: list[dict[str, Any]] = [{} for _ in pending]

    for slot, effect, parent_equipped, change in pending:
        record = _settle(view, effect, parent_equipped, change)
        records[slot] = record
        view.records.append(record)
        tier = record["tier"]
        if tier == TIER_FULL:
            view.stats["applied"] += 1
            view.applied.append(record)
        elif tier == TIER_STORED:
            view.stats["stored"] += 1
            view.stored.append(record)
            if record["reason"] == "formula":
                view.stats["deferred"] += 1
                view.deferred.append(record)
            elif record["reason"] == "custom":
                view.stats["custom"] += 1
                view.custom.append(str(record["key"]))
        else:
            view.stats["unsupported"] += 1
            view.unsupported.append(str(record["key"]))
    view.records = records
    return view


def _settle(
    view: EffectiveSheet,
    effect: Mapping[str, Any],
    parent_equipped: bool | None,
    change: Mapping[str, Any],
) -> dict[str, Any]:
    """结算一条 change，返回带分级结论的记录（不抛异常）。"""
    mode = _as_int(change.get("mode"), MODE_ADD)
    mount = _text(effect.get("mount")) or SCOPE_ACTOR
    kind, path, tier, note, path_scope = classify_path(change.get("key"), mount)
    record: dict[str, Any] = {
        "key": change.get("key"),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, str(mode)),
        "value": change.get("value"),
        "effect": effect.get("name"),
        "mount": mount,
        "path": path,
        "scope": path_scope,
        "tier": tier,
        "active": False,
        "reason": "",
        "note": note,
    }
    if kind == "unsupported":
        record["tier"] = TIER_UNSUPPORTED
        record["reason"] = "unknown-path"
        return record

    # 物品作用域的路径必须挂在武器行上才有消费方。
    if path_scope == SCOPE_ITEM and not effect.get("row_is_weapon"):
        record["tier"] = TIER_STORED
        record["reason"] = "no-consumer"
        record["note"] = note or "物品作用域的路径：只有武器行会被战斗读取"
        return record

    if mode == MODE_CUSTOM:
        record["tier"] = TIER_STORED
        record["reason"] = "custom"
        return record

    active = (
        item_scope_active(effect, parent_equipped)
        if path_scope == SCOPE_ITEM
        else is_active(effect, parent_equipped)
    )
    record["active"] = bool(active)
    if not active:
        record["tier"] = TIER_STORED
        # 「没有消费方」比「效果没激活」更根本：schema 就说这条路径没人读。
        record["reason"] = "no-consumer" if tier != TIER_FULL else "inactive"
        if record["reason"] == "no-consumer":
            record["note"] = note or _TIER_REASONS["no-consumer"]
        return record

    document = view.item_doc(_text(effect.get("row_key"))) if path_scope == SCOPE_ITEM else view.doc
    node = _walk(_path_parts(change.get("key")))
    if kind == "number":
        reason = _apply_number(document, path, change, getattr(view, "roll_data", None))
    elif kind == "list":
        reason = _apply_list(document, path, change)
    elif node is not None and node.kind == "any":
        reason = _apply_any(document, path, change)
    else:
        reason = _apply_text(document, path, change)
    if reason:
        record["tier"] = TIER_STORED
        record["reason"] = reason
        record["note"] = _TIER_REASONS.get(reason, reason)
        return record

    view.modes[_norm(change.get("key"))] = mode
    if tier != TIER_FULL:
        record["tier"] = TIER_STORED
        record["reason"] = "no-consumer"
        record["note"] = note or _TIER_REASONS["no-consumer"]
        return record
    if _norm(change.get("key")) in OVERRIDE_ONLY_PATHS and mode != MODE_OVERRIDE:
        # 当前生命值这类路径只在 override 下是「绝对值」；增量模式算不出稳定的
        # 当前值（房间结算自己会写 HP），如实落 stored 而不是假装生效。
        record["tier"] = TIER_STORED
        record["reason"] = "override-only"
        record["note"] = _TIER_REASONS["override-only"]
        return record
    record["tier"] = TIER_FULL
    record["reason"] = ""
    return record


def annotate_sheet(sheet: Mapping[str, Any] | None, view: EffectiveSheet | None = None) -> dict[str, Any]:
    """把每条 change 的分级结论写回卡面（卡面效果页签按 ``applied`` 分组）。

    只在卡面本来就有 ``effect_changes`` / ``equipment[].effects`` 时才写，
    没有效果的卡逐字节不变（回归铁律）。写回字段：

    - ``applied``：``full`` / ``stored`` / ``unsupported``（三档分级）
    - ``active``：效果当前是否激活
    - ``reason`` / ``note``：为什么没生效 / 生效在哪
    """
    source = sheet if isinstance(sheet, Mapping) else {}
    view = view if view is not None else effective_sheet(source)
    records = view.records
    cursor = {"index": 0}

    def annotate_list(raw_effects: Any) -> list[Any]:
        rows: list[Any] = []
        for raw in raw_effects or []:
            if not isinstance(raw, Mapping):
                rows.append(raw)
                continue
            effect = dict(raw)
            changes: list[Any] = []
            for change in raw.get("changes") or []:
                index = cursor["index"]
                if index < len(records) and isinstance(change, Mapping):
                    cursor["index"] = index + 1
                    record = records[index]
                    item = dict(change)
                    item["applied"] = record["tier"]
                    item["active"] = bool(record["active"])
                    if record.get("reason"):
                        item["reason"] = record["reason"]
                    if record.get("note"):
                        item["note"] = record["note"]
                    changes.append(item)
                else:
                    changes.append(change)
            effect["changes"] = changes
            rows.append(effect)
        return rows

    out = dict(source)
    if "effect_changes" in source:
        out["effect_changes"] = annotate_list(source.get("effect_changes"))
    equipment = source.get("equipment")
    if isinstance(equipment, list):
        rows: list[Any] = []
        for row in equipment:
            if not isinstance(row, Mapping):
                rows.append(row)
                continue
            entry = dict(row)
            if entry.get("effects"):
                entry["effects"] = annotate_list(entry.get("effects"))
            rows.append(entry)
        out["equipment"] = rows
    return out


def effect_dice_bonus(view: EffectiveSheet, path: Any) -> str:
    """把某个 key 上被推迟的骰式加值拼成展示串（如 ``"+1d4"``）。"""
    target = _norm(path)
    parts = [
        _text(item.get("value"))
        for item in view.deferred
        if _norm(item.get("key")) == target and item.get("reason") == "formula"
    ]
    return " + ".join(part for part in parts if part)


__all__ = [
    "ABILITY_KEYS",
    "ATTACK_KINDS",
    "EXTRA_SPEED_KEYS",
    "EffectiveSheet",
    "KNOWN_UNSUPPORTED_PREFIXES",
    "MIDI_QOL_CRITICAL_KEYS",
    "MIDI_QOL_CRITICAL_MAX",
    "MIDI_QOL_CRITICAL_MIN",
    "MIDI_QOL_CRITICAL_THRESHOLD_KEY",
    "MIDI_QOL_ROLL_MODES",
    "MIRROR_SCHEMA",
    "MODE_ADD",
    "MODE_CUSTOM",
    "MODE_DOWNGRADE",
    "MODE_LABELS",
    "MODE_MULTIPLY",
    "MODE_ORDER",
    "MODE_OVERRIDE",
    "MODE_UPGRADE",
    "Node",
    "OVERRIDE_ONLY_PATHS",
    "SPEED_KEYS",
    "SCOPE_ACTOR",
    "SCOPE_ITEM",
    "SYSTEM_PREFIX",
    "TIER_FULL",
    "TIER_STORED",
    "TIER_UNSUPPORTED",
    "annotate_sheet",
    "classify_path",
    "effect_dice_bonus",
    "effect_entries",
    "effective_sheet",
    "is_active",
    "item_scope_active",
    "mirror_path",
    "normalize_change",
    "normalize_effect",
    "parse_number",
    "resolve_path",
    "set_path",
    "unsupported_hint",
]
