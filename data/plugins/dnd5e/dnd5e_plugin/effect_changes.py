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
             攻击 / 伤害 / 抗性 / 速度 / 武器属性…），由 schema 节点的
             ``note`` 标注消费方。
``stored``   镜像里记下了（名字 / 来源 / 路径 / 值都在），**当前没有数值
             消费方**：模块 ``flags.*``、``ATL.*`` 前端字段、物品
             ``activities[...]``、骰式（``1d4``）加值、custom 模式、
             ``movement.fly`` 这类还没有分速模型的字段…
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
                TIER_STORED,
                "能力值调整：需要把效果并进 ability_score（AC/先攻/HP/攻击/豁免全从它派生），本次未接",
            ),
            "mod": _num(TIER_STORED, "同上（派生调整值覆盖）"),
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
            "walk": _num(TIER_FULL, "单一速度模型：combat.speed_ft（EffectsSheet.speed）"),
            "fly": _num(TIER_STORED, "分速模型未建：镜像里记下，卡面「已记录未生效」列出"),
            "swim": _num(TIER_STORED, "同上（分速模型未建）"),
            "climb": _num(TIER_STORED, "同上（分速模型未建）"),
            "burrow": _num(TIER_STORED, "同上（分速模型未建）"),
            "jump": _num(TIER_STORED, "跳跃距离不是房间的移动值，未接"),
            "hover": _num(TIER_STORED),
            "all": _num(TIER_STORED, "movement.all 是「所有速度」别名，展开需要分速模型"),
            "units": _txt(TIER_STORED, "单位换算（mi/km）：房间按英尺结算"),
            "special": _txt(TIER_STORED),
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
            "dm": _obj(
                {
                    "amount": _num(TIER_STORED, "伤害减免数值：伤害管线还没有减伤阶段"),
                    "bypasses": _lst(TIER_STORED),
                }
            ),
            "ci": _obj(
                {
                    "value": _lst(TIER_FULL, "conditions：免疫的条件不会被施加（effects.apply）"),
                    "custom": _txt(TIER_STORED),
                }
            ),
            "languages": _obj(
                {
                    "value": _lst(TIER_STORED, "语言只做卡面展示，不进任何数值"),
                    "custom": _txt(TIER_STORED),
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
    return _obj(
        {"*": _any(TIER_STORED, "dnd5e v6 物品活动（attack/save/damage…）：运行时不模拟，整条记录")},
        scope=SCOPE_ITEM,
    )


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
                                "max": _num(TIER_STORED, "生命上限由卡面 hp.max 决定（覆盖会让治疗/掉落难对齐）"),
                                "value": _num(TIER_STORED, "当前生命由房间结算写，不参与派生"),
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
                            TIER_STORED,
                            "熟练加值覆盖：checks/combat 直接用 proficiency_bonus(level)，未接",
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
        "name": _txt(TIER_STORED, "改名：'{0}, 法术名' 这类模板不套用，原文保留"),
        "activities": _activity_node(),
        "_id": _txt(TIER_STORED),
        "img": _txt(TIER_STORED),
        "type": _txt(TIER_STORED),
        "folder": _txt(TIER_STORED),
        "sort": _num(TIER_STORED),
        "delta": _any(TIER_STORED, "增量数据（delta）：不展开"),
        "flags": _any(TIER_STORED, "模块/系统标志（midi-qol、dnd5e flags）：只记录，不模拟模块运行时"),
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
_INDEX_RE = re.compile(r"\[[^\]]*\]$")


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
        # ``activities[attack].attack.ability`` → ``activities.attack.ability``
        base = _INDEX_RE.sub("", part).strip()
        parts.append(base or part)
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
        """基础生命上限 → 效果处理后的上限（``hp.bonus`` 加值或覆盖）。"""
        value = max(1, _as_int(base, 1))
        if not self.empty:
            bonus = self.max_hp_bonus
            if self.modes.get(_norm("attributes.hp.bonus")) == MODE_OVERRIDE and bonus:
                return max(1, bonus)
            value += max(0, bonus)
        return max(1, value)

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
        return max(0, value)

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
        """d20 模式：1 优势 / -1 劣势 / 0 普通（FVTT dnd5e 口径）。"""
        key = _text(ability).casefold()
        if not key:
            return 0
        return _as_int(self.number(f"abilities.{key}.{kind}.roll.mode"), 0)

    def traits(self, kind: Any) -> list[str]:
        raw = resolve_path(self.doc, _norm(f"traits.{_text(kind)}.value"))
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def condition_immunities(self) -> set[str]:
        """``traits.ci.value`` → 归一的条件 id 集合（小写）。"""
        return {_text(item).casefold() for item in self.traits("ci") if _text(item)}

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
        """装备行 → 应用了物品作用域效果（属性/伤害骰）的副本。

        物品作用域的路径改的是**该装备行自己的物品文档**；这里把它并回行上，
        让 ``combat._weapon_attack`` 按正常字段算攻击属性与伤害骰。
        """
        if not self.items or not isinstance(row, Mapping):
            return dict(row) if isinstance(row, Mapping) else {}
        doc: dict[str, Any] | None = None
        for key in _row_keys(row):
            found = self.items.get(key)
            if isinstance(found, Mapping):
                doc = found
                break
        if not doc:
            return dict(row)
        out = dict(row)
        system = doc.get("system") if isinstance(doc.get("system"), Mapping) else {}
        extra_props = _as_values(system.get("properties"))
        if extra_props:
            current = [_text(item).casefold() for item in out.get("properties") or []]
            merged = list(out.get("properties") or [])
            for prop in extra_props:
                if prop.casefold() not in current:
                    merged.append(prop)
                    current.append(prop.casefold())
            out["properties"] = merged
        base = system.get("damage") if isinstance(system.get("damage"), Mapping) else {}
        base = base.get("base") if isinstance(base.get("base"), Mapping) else {}
        number = base.get("number")
        denomination = base.get("denomination")
        if number is not None or denomination is not None:
            damage = dict(out.get("damage") or {}) if isinstance(out.get("damage"), Mapping) else {}
            cur_number, cur_denom = _split_dice(damage.get("dice"))
            new_number = _as_int(number, cur_number) if number is not None else cur_number
            new_denom = _as_int(denomination, cur_denom) if denomination is not None else cur_denom
            if new_number and new_denom:
                damage["dice"] = f"{new_number}d{new_denom}"
            out["damage"] = damage
        return out

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


def _split_dice(value: Any) -> tuple[int, int]:
    """``"1d8"`` → ``(1, 8)``；解析不了给 ``(1, 4)``（与 combat 的兜底一致）。"""
    text = _text(value).casefold()
    match = re.match(r"^\s*(\d+)\s*d\s*(\d+)\s*$", text)
    if not match:
        return 1, 4
    return max(1, _as_int(match.group(1), 1)), max(1, _as_int(match.group(2), 4))


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
        reason = _apply_number(document, path, change)
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
    "EffectiveSheet",
    "KNOWN_UNSUPPORTED_PREFIXES",
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
