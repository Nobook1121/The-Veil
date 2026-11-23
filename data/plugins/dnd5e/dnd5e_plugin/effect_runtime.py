"""FVTT workflow 钩子的**安全等价物**：声明式 DSL + 纯执行器。

背景：现实里的 FVTT 世界包把大量战斗行为塞进模块运行时钩子——midi-qol 的
``flags.midi-qol.onUseMacroName`` / ``onHitMacroName``、DAE 的
``flags.dae.macro.command``、ItemMacro、ActiveAuras、Conditional 等。这些都是
**任意 JS**：导入世界包时如果照抄执行，等于让第三方 zip 在我们的进程里跑代码
（读写文件、发网络请求、动全局状态，还能读到环境变量）。本模块给出的是它的
**安全等价物**：把「钩子想干什么」重新表述成一份**封闭的 JSON DSL**，由本模块
用一支只认白名单的纯执行器来解释。

安全边界（硬约束，逐条对应代码）
--------------------------------
1. **绝不执行任意 JS**：本模块没有 ``eval`` / ``exec`` / ``compile`` /
   ``__import__``，也不加载外部脚本、不读文件、不访问网络与环境变量。宏原文
   （``flags.dae.macro.command`` 之类）只作为 ``raw`` 字符串**保存**，从不解释。
2. **DSL 只能调用白名单 action**：:data:`ACTION_SPECS` 是唯一的动作全集；不在表
   里的 ``type`` 一律拒绝（``macro.execute`` / ``script`` / ``flags.set`` 这类
   "越权名" 自然也在拒绝之列）。每个参数都有类型与取值范围校验
   （骰式 :func:`parse_dice`、枚举、整数区间、slug 长度），未知参数直接拒绝，
   不做"忽略掉继续跑"。
3. **规模上限**：单次触发最多 :data:`MAX_ACTIONS_PER_TRIGGER` 个 action、
   嵌套深度最多 :data:`MAX_NESTING_DEPTH` 层。上限同时在校验期（写坏的数据进
   不来）和执行期（多个钩子叠加也超不过）生效。
4. **可重放**：所有随机性都来自注入的 ``rng``（``random.Random(seed)`` 或任何
   返回 ``[0, 1)`` 的可调用对象）。同一个 seed + 同一份 context + 同一批钩子
   ⇒ 同一份 ``actions`` / ``events``。执行器不写任何全局状态：结果由调用方决定
   怎么落到自己的战斗状态里。

条件表达式同样是封闭的：:func:`_eval_condition` 自带词法/语法分析（不支持
``__`` 名称、不支持函数以外的任何调用），只认识 ``target.hp_pct < 0.5``、
``self.has_condition('raging')``、``weapon.properties has 'finesse'`` 这三类
写法及其 ``and`` / ``or`` / ``not`` 组合。``formula.py`` 落地后由它接管
（见 :func:`eval_condition` 的 try/except 回退）。

DSL 形状::

    {"trigger": "onUse",
     "condition": "target.hp_pct < 0.5",
     "actions": [
       {"type": "damage.add", "dice": "1d6", "damage_type": "fire", "target": "hit"},
       {"type": "condition.add", "condition": "prone", "duration": 1},
       {"type": "resource.spend", "key": "uses", "amount": 1}
     ]}

``trigger`` ∈ :data:`TRIGGERS`；``actions`` 里每一项的 ``type`` ∈
:data:`ACTION_SPECS`。嵌套用可选的 ``then``（子 action 列表）表达，深度上限 3。

本模块**只**提供解析 / 校验 / 执行三件事：不改 ``combat.py``，不写全局状态。
接线（把 ``run(...)`` 的结果应用到战斗状态）是下一刀，接口形状见
``docs/plans/workflow-hooks-audit.md``。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Iterable, Mapping, Sequence

# -- 常量：触发点 / 状态 ----------------------------------------------------

#: 七个触发点（DSL ``trigger`` 的合法取值）。
TRIGGERS = (
    "onUse",
    "onHit",
    "onMiss",
    "onSaveFail",
    "onSaveSuccess",
    "onTurnStart",
    "onDamaged",
)

#: 钩子三态 + ``native``（引擎已实现的语义，这里只标记，不重复实现）。
STATUS_CONVERTED = "converted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_IGNORED = "ignored"
STATUS_NATIVE = "native"

#: 导入器要写进角色卡的两种状态（ignored / native 不进卡面）。
KEPT_STATUSES = (STATUS_CONVERTED, STATUS_NEEDS_REVIEW)

#: 单次触发的 action 上限（含嵌套子 action）。
MAX_ACTIONS_PER_TRIGGER = 20
#: ``then`` 嵌套深度上限。
MAX_NESTING_DEPTH = 3
#: 一条条件表达式的 token 上限（防病态输入把解析器拖死）。
MAX_CONDITION_TOKENS = 128
#: 条件表达式的括号/逻辑嵌套上限。
MAX_CONDITION_DEPTH = 8

# -- 常量：白名单数值域 ----------------------------------------------------

#: 伤害类型（srd ``equipment`` / 战斗层同口径）。
DAMAGE_TYPES = (
    "acid",
    "bludgeoning",
    "cold",
    "fire",
    "force",
    "lightning",
    "necrotic",
    "piercing",
    "poison",
    "psychic",
    "radiant",
    "slashing",
    "thunder",
)

#: action 的 ``target`` 取值。``hit`` = 本次命中的目标，``target`` = 施法/攻击
#: 的显式目标，``self`` = 发起者，``all`` = 全部战斗者（由调用方解释）。
TARGETS = ("hit", "target", "self", "all")

#: 豁免对伤害的影响（对应 dnd5e ``activities.*.damage.onSave``）。
ON_SAVE_MODES = ("full", "half", "none")

#: 骰子的面数白名单（d4..d100 里的常用档；d3/d7 这类不存在于 5e）。
ALLOWED_DIE_SIDES = frozenset({4, 6, 8, 10, 12, 20, 100})
MAX_DICE_COUNT = 20
MAX_DICE_MODIFIER = 100

#: 资源键长度上限（``uses`` / ``charges`` / ``superiority`` 之类）。
MAX_RESOURCE_KEY = 32
#: 效果名长度上限。
MAX_EFFECT_NAME = 80
#: 状态名长度上限。
MAX_CONDITION_NAME = 40
#: ``resource.spend`` 单次上限。
MAX_RESOURCE_AMOUNT = 20
#: ``temp_hp`` 单次上限。
MAX_TEMP_HP = 500
#: ``condition.add`` / ``effect.apply`` 的持续时间上限（回合）。
MAX_DURATION_ROUNDS = 1000

#: action 白名单：``type`` → 参数表。参数表里 ``kind`` 决定校验方式：
#:
#: - ``dice``：骰式字符串（:func:`parse_dice`）；
#: - ``int``：整数，夹在 ``lo..hi``；
#: - ``choice``：枚举 ``choices``；
#: - ``slug``：``[a-z0-9_\\-]`` 且长度 ≤ ``hi``；
#: - ``text``：去空白的字符串，长度 ≤ ``hi``。
#:
#: ``required=False`` 的参数有 ``default``；表里没有的参数名一律视为「未知参数」
#: 拒绝——这是 "不许夹带私货" 的一半（另一半是 ``type`` 必须在表里）。
ACTION_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "damage.add": {
        "dice": {"kind": "dice", "required": True},
        "damage_type": {"kind": "choice", "choices": DAMAGE_TYPES, "required": True},
        "target": {"kind": "choice", "choices": TARGETS, "default": "hit"},
        "on_save": {"kind": "choice", "choices": ON_SAVE_MODES, "default": "full"},
    },
    "damage.reduce": {
        "dice": {"kind": "dice", "required": True},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "heal": {
        "amount": {"kind": "dice", "required": True},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "temp_hp": {
        "amount": {"kind": "int", "lo": 0, "hi": MAX_TEMP_HP, "required": True},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "attack.advantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "attack.disadvantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "save.advantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "hit"},
    },
    "save.disadvantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "hit"},
    },
    "check.advantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "check.disadvantage": {
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "condition.add": {
        "condition": {"kind": "slug", "required": True, "hi": MAX_CONDITION_NAME},
        "duration": {"kind": "int", "lo": 0, "hi": MAX_DURATION_ROUNDS, "default": 1},
        "target": {"kind": "choice", "choices": TARGETS, "default": "hit"},
    },
    "condition.remove": {
        "condition": {"kind": "slug", "required": True, "hi": MAX_CONDITION_NAME},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "resource.spend": {
        "key": {"kind": "slug", "required": True, "hi": MAX_RESOURCE_KEY},
        "amount": {"kind": "int", "lo": 1, "hi": MAX_RESOURCE_AMOUNT, "default": 1},
    },
    "effect.apply": {
        "name": {"kind": "text", "required": True, "hi": MAX_EFFECT_NAME},
        "duration_rounds": {"kind": "int", "lo": 0, "hi": MAX_DURATION_ROUNDS, "default": 0},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
    "effect.remove": {
        "name": {"kind": "text", "required": True, "hi": MAX_EFFECT_NAME},
        "target": {"kind": "choice", "choices": TARGETS, "default": "self"},
    },
}

#: 已知的"危险动作名"——留一份显式清单，报错信息里能说清为什么不收。
#: 这些名字**不在** :data:`ACTION_SPECS` 里，本来就通不过校验；单列出来是为了
#: 让 ``reason`` 明确写成「越权动作」而不是泛泛的「未知动作」，也方便测试断言。
FORBIDDEN_ACTION_NAMES = (
    "macro.execute",
    "macro.run",
    "script",
    "script.run",
    "js",
    "javascript",
    "eval",
    "exec",
    "execute",
    "compile",
    "import",
    "item.update",
    "actor.update",
    "flags.set",
    "flags.write",
    "game.exec",
    "ui.notifications",
    "dialog.prompt",
    "socket.emit",
    "fetch",
    "file.read",
    "file.write",
    "process.spawn",
)


class HookError(ValueError):
    """钩子/DSL 校验失败（调用方应当**拒绝执行**，而不是降级跑半截）。"""

    def __init__(self, message: str, code: str = "hook.invalid") -> None:
        super().__init__(message)
        self.code = code


class ConditionError(ValueError):
    """条件表达式语法/求值失败。"""


# -- 骰式 ------------------------------------------------------------------


def parse_dice(text: Any) -> tuple[int, int, int]:
    """``"2d4"`` / ``"1d6+2"`` / ``"5"`` → ``(次数, 面数, 加值)``。

    面数为 ``0`` 表示"平值"（无骰，只有加值），此时次数固定 1。

    只接受这一种写法：``[次数]d[面数][+/-加值]`` 或纯整数。任何多余字符
    （``"1d6 fire"``、``"eval(...)"``、``"2d6*3"``）都拒收——骰式不是表达式
    语言，不需要也没有第二个语法。
    """
    raw = str(text or "").strip().lower().replace(" ", "")
    if not raw:
        raise HookError("骰式为空", "dice.empty")
    modifier = 0
    body = raw
    for sep in ("+", "-"):
        index = raw.find(sep, 1)
        if index > 0:
            body = raw[:index]
            tail = raw[index + 1 :]
            if not tail.isdigit():
                raise HookError(f"骰式加值非法：{text!r}", "dice.modifier")
            modifier = int(tail) * (1 if sep == "+" else -1)
            break
    if abs(modifier) > MAX_DICE_MODIFIER:
        raise HookError(f"骰式加值越界：{text!r}", "dice.modifier")
    if "d" not in body:
        if not body.isdigit():
            raise HookError(f"骰式非法：{text!r}", "dice.shape")
        return 1, 0, int(body) + modifier
    count_text, _, sides_text = body.partition("d")
    count_text = count_text or "1"
    if not count_text.isdigit() or not sides_text.isdigit():
        raise HookError(f"骰式非法：{text!r}", "dice.shape")
    count, sides = int(count_text), int(sides_text)
    if not 1 <= count <= MAX_DICE_COUNT:
        raise HookError(f"骰子个数越界（1..{MAX_DICE_COUNT}）：{text!r}", "dice.count")
    if sides not in ALLOWED_DIE_SIDES:
        raise HookError(f"骰子面数不在白名单：{text!r}", "dice.sides")
    return count, sides, modifier


def _roll_dice(dice: str, rng: Callable[[], float]) -> tuple[list[int], int]:
    """按骰式掷骰：返回 ``([每颗点数], 合计)``。"""
    count, sides, modifier = parse_dice(dice)
    if sides == 0:
        return [], count + modifier
    rolls = [1 + int(rng() * sides) for _ in range(count)]
    return rolls, sum(rolls) + modifier


def _rng_of(rng: Any) -> Callable[[], float]:
    """把 ``rng`` 归一成 ``() -> float``。

    - ``None`` → 一个新的 ``random.Random()``（**不可重放**，只是"能跑"）；
    - ``random.Random`` 之类带 ``.random()`` 的对象 → 用它的 ``random``；
    - 其他可调用对象 → 原样当随机源（测试用固定序列）。
    """
    if rng is None:
        return random.Random().random
    if callable(rng):
        return rng
    method = getattr(rng, "random", None)
    if callable(method):
        return method
    raise HookError("rng 必须是可调用对象或带 .random() 的对象", "rng.invalid")


# -- action 校验 -----------------------------------------------------------


def _check_int(name: str, value: Any, spec: Mapping[str, Any]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HookError(f"参数 {name} 必须是整数", "param.type")
    lo = spec.get("lo")
    hi = spec.get("hi")
    if lo is not None and value < lo:
        raise HookError(f"参数 {name}={value} 小于下限 {lo}", "param.range")
    if hi is not None and value > hi:
        raise HookError(f"参数 {name}={value} 大于上限 {hi}", "param.range")
    return value


def _check_slug(name: str, value: Any, spec: Mapping[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        raise HookError(f"参数 {name} 不能为空", "param.empty")
    limit = int(spec.get("hi") or 32)
    if len(text) > limit:
        raise HookError(f"参数 {name} 过长（≤{limit}）", "param.length")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-'")
    if any(char not in allowed for char in text.casefold()):
        raise HookError(f"参数 {name} 含非法字符：{text!r}", "param.charset")
    return text.casefold()


def _check_text(name: str, value: Any, spec: Mapping[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        raise HookError(f"参数 {name} 不能为空", "param.empty")
    limit = int(spec.get("hi") or 64)
    if len(text) > limit:
        raise HookError(f"参数 {name} 过长（≤{limit}）", "param.length")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise HookError(f"参数 {name} 不能含换行/控制字符", "param.charset")
    return text


def _check_choice(name: str, value: Any, spec: Mapping[str, Any]) -> str:
    choices = spec.get("choices") or ()
    text = str(value or "").strip()
    lowered = text.casefold()
    for choice in choices:
        if lowered == str(choice).casefold():
            return choice
    raise HookError(f"参数 {name}={text!r} 不在取值表内", "param.choice")


def validate_action(action: Any, *, depth: int = 1) -> dict[str, Any]:
    """校验并归一化一个 action；不合法直接抛 :class:`HookError`。

    归一化的含义：补默认值、枚举大小写统一、只保留白名单里的参数键。
    """
    if depth > MAX_NESTING_DEPTH:
        raise HookError(f"嵌套深度超过 {MAX_NESTING_DEPTH} 层", "nesting.depth")
    if not isinstance(action, Mapping):
        raise HookError("action 必须是对象", "action.type")
    kind = str(action.get("type") or "").strip()
    if not kind:
        raise HookError("action 缺少 type", "action.type")
    if kind in FORBIDDEN_ACTION_NAMES:
        raise HookError(f"越权动作被拒绝：{kind}", "action.forbidden")
    spec_table = ACTION_SPECS.get(kind)
    if spec_table is None:
        raise HookError(f"未知动作（不在白名单）：{kind}", "action.unknown")

    known = set(spec_table) | {"type", "then"}
    extra = [key for key in action if key not in known]
    if extra:
        raise HookError(f"动作 {kind} 带未知参数：{sorted(extra)}", "param.unknown")

    result: dict[str, Any] = {"type": kind}
    for name, spec in spec_table.items():
        present = name in action and action[name] is not None
        if not present:
            if spec.get("required"):
                raise HookError(f"动作 {kind} 缺少必填参数 {name}", "param.missing")
            if "default" in spec:
                result[name] = spec["default"]
            continue
        value = action[name]
        kind_of = spec["kind"]
        if kind_of == "int":
            result[name] = _check_int(name, value, spec)
        elif kind_of == "choice":
            result[name] = _check_choice(name, value, spec)
        elif kind_of == "slug":
            result[name] = _check_slug(name, value, spec)
        elif kind_of == "text":
            result[name] = _check_text(name, value, spec)
        elif kind_of == "dice":
            parse_dice(value)  # 只校验，原样保留（写出可读的骰式）
            result[name] = str(value).strip()
        else:  # pragma: no cover - 规格表写错了才会到这里
            raise HookError(f"内部错误：未知参数类型 {kind_of}", "param.spec")
    if "then" in action:
        result["then"] = _validate_action_list(action["then"], depth + 1)
    return result


def _validate_action_list(actions: Any, depth: int = 1) -> list[dict[str, Any]]:
    """校验 action 列表：条数、嵌套深度、逐个参数。"""
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes, Mapping)):
        raise HookError("actions 必须是数组", "actions.shape")
    rows = list(actions)
    total = _count_actions(rows)
    if total > MAX_ACTIONS_PER_TRIGGER:
        raise HookError(
            f"单个触发点的 action 数超过上限 {MAX_ACTIONS_PER_TRIGGER}（{total}）",
            "actions.limit",
        )
    return [validate_action(action, depth=depth) for action in rows]


def _count_actions(actions: Iterable[Any], depth: int = 1) -> int:
    """递归数 action（含 ``then`` 子列表）；同时守住嵌套深度。"""
    if depth > MAX_NESTING_DEPTH:
        raise HookError(f"嵌套深度超过 {MAX_NESTING_DEPTH} 层", "nesting.depth")
    total = 0
    for action in actions:
        total += 1
        if isinstance(action, Mapping) and "then" in action:
            nested = action["then"]
            if not isinstance(nested, Sequence) or isinstance(nested, (str, bytes, Mapping)):
                raise HookError("then 必须是数组", "actions.shape")
            total += _count_actions(list(nested), depth + 1)
    return total


def validate_dsl(dsl: Any) -> dict[str, Any]:
    """校验并归一化一份 DSL；不合法抛 :class:`HookError`。"""
    if not isinstance(dsl, Mapping):
        raise HookError("DSL 必须是对象", "dsl.shape")
    trigger = str(dsl.get("trigger") or "").strip()
    if trigger not in TRIGGERS:
        raise HookError(f"未知触发点：{trigger!r}", "trigger.unknown")
    extra = [key for key in dsl if key not in ("trigger", "condition", "actions", "notes")]
    if extra:
        raise HookError(f"DSL 带未知字段：{sorted(extra)}", "dsl.unknown_field")
    condition = dsl.get("condition")
    if condition not in (None, ""):
        if not isinstance(condition, str):
            raise HookError("condition 必须是字符串", "condition.type")
        try:
            parse_condition(condition)  # 语法校验（不求值）
        except ConditionError as exc:
            # 对外只抛一种错误类型：DSL 校验失败 = HookError（含空白串）。
            raise HookError(f"条件表达式非法：{exc}", "condition.invalid") from exc
    actions = _validate_action_list(dsl.get("actions") or [])
    result: dict[str, Any] = {"trigger": trigger, "actions": actions}
    if isinstance(condition, str) and condition.strip():
        result["condition"] = condition.strip()
    return result


# -- 条件表达式 ------------------------------------------------------------
#
# 封闭的小语言，照抄不了 JS：
#
#   expr    := and ('or' and)*
#   and     := unary ('and' unary)*
#   unary   := 'not' unary | compare
#   compare := atom (('<'|'<='|'>'|'>='|'=='|'!='|'has'|'in') atom)?
#   atom    := 数字 | 字符串 | true | false | null | 路径 | 路径 '(' 参数表 ')' | '(' expr ')'
#
# 路径只能从 context 的既有键往下走（``target.hp``），函数只有 has_condition /
# has_property / has 三个，且不允许 ``__`` 开头的名字（挡住 ``__class__`` 这类
# 反射噪音）。

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_BODY = _IDENT_START | set("0123456789")
_DIGITS = set("0123456789")
_COMPARE_OPS = ("<=", ">=", "==", "!=", "<", ">")
_KEYWORDS = {"and", "or", "not", "has", "in", "true", "false", "null"}
_ALLOWED_FUNCS = {"has_condition", "has_property", "has"}


def _tokenize(expression: str) -> list[tuple[str, Any]]:
    text = str(expression or "")
    tokens: list[tuple[str, Any]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char in "()" :
            tokens.append((char, char))
            index += 1
            continue
        if char == ",":
            tokens.append((",", char))
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            buffer: list[str] = []
            while index < length and text[index] != quote:
                if text[index] == "\\" and index + 1 < length:
                    index += 1
                buffer.append(text[index])
                index += 1
            if index >= length:
                raise ConditionError("字符串未闭合")
            index += 1
            tokens.append(("string", "".join(buffer)))
            continue
        matched_op = next((op for op in _COMPARE_OPS if text.startswith(op, index)), None)
        if matched_op:
            tokens.append(("op", matched_op))
            index += len(matched_op)
            continue
        if char in _DIGITS or (char == "." and index + 1 < length and text[index + 1] in _DIGITS):
            start = index
            while index < length and (text[index] in _DIGITS or text[index] == "."):
                index += 1
            chunk = text[start:index]
            try:
                tokens.append(("number", float(chunk) if "." in chunk else int(chunk)))
            except ValueError as exc:  # pragma: no cover - 词法上不可能出现
                raise ConditionError(f"数字非法：{chunk!r}") from exc
            continue
        if char in _IDENT_START:
            start = index
            while index < length and (text[index] in _IDENT_BODY or text[index] == "."):
                index += 1
            word = text[start:index]
            lowered = word.casefold()
            if lowered in _KEYWORDS:
                tokens.append(("keyword", lowered))
            else:
                tokens.append(("name", word))
            continue
        raise ConditionError(f"非法字符：{char!r}")
    if len(tokens) > MAX_CONDITION_TOKENS:
        raise ConditionError(f"条件过长（> {MAX_CONDITION_TOKENS} token）")
    if not tokens:
        raise ConditionError("条件为空")
    return tokens


class _ConditionParser:
    """递归下降解析器（纯函数，无副作用、无 ``eval``）。"""

    def __init__(self, tokens: list[tuple[str, Any]]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.depth = 0

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, Any]:
        token = self.peek()
        if token is None:
            raise ConditionError("表达式意外结束")
        self.pos += 1
        return token

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise ConditionError(f"多余 token：{self.peek()!r}")
        return node

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_CONDITION_DEPTH:
            raise ConditionError(f"条件嵌套超过 {MAX_CONDITION_DEPTH} 层")

    def parse_or(self) -> Any:
        self._enter()
        node = self.parse_and()
        while True:
            token = self.peek()
            if token and token[0] == "keyword" and token[1] == "or":
                self.take()
                node = ("or", node, self.parse_and())
                continue
            break
        self.depth -= 1
        return node

    def parse_and(self) -> Any:
        node = self.parse_unary()
        while True:
            token = self.peek()
            if token and token[0] == "keyword" and token[1] == "and":
                self.take()
                node = ("and", node, self.parse_unary())
                continue
            break
        return node

    def parse_unary(self) -> Any:
        token = self.peek()
        if token and token[0] == "keyword" and token[1] == "not":
            self.take()
            self._enter()
            node = ("not", self.parse_unary())
            self.depth -= 1
            return node
        return self.parse_compare()

    def parse_compare(self) -> Any:
        left = self.parse_atom()
        token = self.peek()
        if token is None:
            return left
        kind, value = token
        if kind == "op":
            self.take()
            return ("cmp", value, left, self.parse_atom())
        if kind == "keyword" and value in ("has", "in"):
            self.take()
            return ("cmp", value, left, self.parse_atom())
        return left

    def parse_atom(self) -> Any:
        token = self.take()
        kind, value = token
        if kind == "number":
            return ("number", value)
        if kind == "string":
            return ("string", value)
        if kind == "keyword" and value in ("true", "false"):
            return ("bool", value == "true")
        if kind == "keyword" and value == "null":
            return ("null", None)
        if kind == "(":
            self._enter()
            node = self.parse_or()
            self.depth -= 1
            closing = self.take()
            if closing[0] != ")":
                raise ConditionError("括号未闭合")
            return node
        if kind == "name":
            path = value.split(".")
            if any(part.startswith("__") or not part for part in path):
                raise ConditionError(f"非法名称：{value!r}")
            nxt = self.peek()
            if nxt and nxt[0] == "(":
                self.take()
                return ("call", path, self.parse_args())
            return ("path", path)
        raise ConditionError(f"意外的 token：{token!r}")

    def parse_args(self) -> list[Any]:
        args: list[Any] = []
        if self.peek() and self.peek()[0] == ")":
            self.take()
            return args
        while True:
            args.append(self.parse_atom())
            token = self.take()
            if token[0] == ")":
                return args
            if token[0] != ",":
                raise ConditionError("参数表格式错误")


def parse_condition(expression: Any) -> Any:
    """把条件表达式解析成 AST（语法校验用；不求值）。"""
    if not isinstance(expression, str) or not str(expression).strip():
        raise ConditionError("条件表达式必须是非空字符串")
    return _ConditionParser(_tokenize(expression)).parse()


def _plain(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    return value


def _lookup(node: Any, key: str) -> Any:
    """从一个节点里取值；只认 Mapping / 序列，绝不碰属性（无反射）。"""
    if isinstance(node, Mapping):
        if key in node:
            return node[key]
        # 便捷派生：hp_pct 允许从 hp / hp_max 现场算（纯读，不改 context）。
        if key == "hp_pct":
            current = node.get("hp")
            maximum = node.get("hp_max", node.get("max_hp"))
            try:
                current_value = float(current)  # type: ignore[arg-type]
                max_value = float(maximum)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ConditionError("无法派生 hp_pct（缺 hp / hp_max）")
            if max_value <= 0:
                raise ConditionError("hp_max 必须为正数")
            return max(0.0, min(1.0, current_value / max_value))
        return _MISSING
    if isinstance(node, (list, tuple)):
        if key.isdigit():
            index = int(key)
            return node[index] if 0 <= index < len(node) else _MISSING
        return _MISSING
    return _MISSING


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<missing>"


_MISSING = _Missing()


def _resolve_path(path: Sequence[str], context: Mapping[str, Any]) -> Any:
    if not path:
        raise ConditionError("空路径")
    root = path[0]
    if root not in context:
        raise ConditionError(f"未知上下文根：{root!r}")
    node: Any = context[root]
    for part in path[1:]:
        node = _lookup(node, part)
        if node is _MISSING:
            return _MISSING
    return node


def _resolve_call(node: tuple, context: Mapping[str, Any]) -> Any:
    _kind, path, args = node
    name = path[-1]
    if name not in _ALLOWED_FUNCS:
        raise ConditionError(f"不支持的函数：{name}")
    base_path = list(path[:-1])
    base = _resolve_path(base_path, context) if base_path else context
    if base is _MISSING or base is None:
        return False
    if len(args) != 1:
        raise ConditionError(f"{name} 只接受一个参数")
    arg = _eval_node(args[0], context)
    if name in ("has_condition", "has"):
        wanted = str(arg).casefold()
        conditions = _lookup(base, "conditions")
        if conditions is _MISSING:
            statuses = _lookup(base, "statuses")
            conditions = () if statuses is _MISSING else statuses
        if isinstance(conditions, str):
            conditions = [conditions]
        if not isinstance(conditions, (list, tuple, set, frozenset)):
            return False
        return any(str(item).casefold() == wanted for item in conditions)
    wanted = str(arg).casefold()
    properties = _lookup(base, "properties")
    if properties is _MISSING:
        return False
    if isinstance(properties, str):
        properties = [properties]
    if not isinstance(properties, (list, tuple, set, frozenset)):
        return False
    return any(str(item).casefold() == wanted for item in properties)


def _eval_node(node: Any, context: Mapping[str, Any]) -> Any:
    if not isinstance(node, tuple):
        raise ConditionError("AST 节点损坏")
    kind = node[0]
    if kind == "number":
        return node[1]
    if kind == "string":
        return node[1]
    if kind == "bool":
        return node[1]
    if kind == "null":
        return None
    if kind == "path":
        value = _resolve_path(node[1], context)
        return None if value is _MISSING else value
    if kind == "call":
        return _resolve_call(node, context)
    if kind == "not":
        return not bool(_eval_node(node[1], context))
    if kind == "and":
        return bool(_eval_node(node[1], context)) and bool(_eval_node(node[2], context))
    if kind == "or":
        return bool(_eval_node(node[1], context)) or bool(_eval_node(node[2], context))
    if kind == "cmp":
        return _compare(node[1], _eval_node(node[2], context), _eval_node(node[3], context))
    raise ConditionError(f"未知 AST 节点：{kind}")


def _compare(op: str, left: Any, right: Any) -> bool:
    if op in ("has", "in"):
        container = left if op == "has" else right
        item = right if op == "has" else left
        if isinstance(container, str):
            return str(item).casefold() in container.casefold()
        if isinstance(container, Mapping):
            return item in container
        if isinstance(container, (list, tuple, set, frozenset)):
            wanted = str(item).casefold()
            return any(str(entry).casefold() == wanted for entry in container)
        return False
    if op == "==":
        return _plain(left) == _plain(right)
    if op == "!=":
        return _plain(left) != _plain(right)
    try:
        lhs, rhs = float(left), float(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConditionError(f"比较运算需要数值：{left!r} {op} {right!r}")
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    raise ConditionError(f"未知运算符：{op}")


#: ``formula.py`` 里可能出现的「条件求值」函数名（按顺序探测）。
#:
#: 刻意**不**包含 ``evaluate`` / ``evaluate_number``：那两个是**公式**求值器
#: （``@path`` 引用、返回 ``{"ok": …, "value": …}``），语义与条件表达式不同，
#: 混用会把「公式算不出来」错当成「条件成立」。
_FORMULA_CONDITION_FUNCS = ("eval_condition", "evaluate_condition")


def _formula_condition(expression: str, context: Mapping[str, Any]) -> Any:
    """``formula.py`` 的兼容桥：有专门的条件求值就借它，没有就回退本地实现。

    ``formula.py`` 由另一支并行改动落地，本模块**不修改它**，只按名字探测
    （:data:`_FORMULA_CONDITION_FUNCS`）。返回值约定：

    - 布尔 → 直接当条件结果；
    - ``{"ok": True, "value": …}`` 形态 → ``bool(value)``；
    - ``{"ok": False, …}`` / 签名不符 / 模块缺席 → :data:`_MISSING`（回退本地），
      **不允许**出现"解析失败却当成真"的路径。
    """
    try:  # pragma: no cover - 依赖并行落地的模块，正常路径由本地实现兜底
        from dnd5e_plugin import formula as _formula  # type: ignore
    except ImportError:
        return _MISSING
    for name in _FORMULA_CONDITION_FUNCS:
        func = getattr(_formula, name, None)
        if not callable(func):
            continue
        try:
            result = func(expression, context)
        except TypeError:
            continue
        except Exception:  # noqa: BLE001 - 外来模块的异常不该炸掉导入/战斗
            return _MISSING
        if isinstance(result, Mapping):
            if not result.get("ok", True):
                return _MISSING
            return bool(result.get("value"))
        if isinstance(result, bool):
            return result
        return _MISSING
    return _MISSING


def _eval_condition(expression: str, context: Mapping[str, Any]) -> bool:
    """本地条件求值（``formula.py`` 缺席时的完整实现）。"""
    ast = parse_condition(expression)
    return bool(_eval_node(ast, dict(context or {})))


def eval_condition(expression: Any, context: Mapping[str, Any] | None) -> bool:
    """条件表达式的**唯一入口**：``formula.py`` 优先，本地实现兜底。"""
    if not isinstance(expression, str) or not expression.strip():
        return True
    scope = dict(context or {})
    external = _formula_condition(expression, scope)
    if external is not _MISSING:
        return bool(external)
    return _eval_condition(expression, scope)


# -- 执行器 ----------------------------------------------------------------


def _hook_dsl_trigger(hook: Any) -> str:
    dsl = hook.get("dsl") if isinstance(hook, Mapping) and "dsl" in hook else hook
    if isinstance(dsl, Mapping):
        return str(dsl.get("trigger") or "")
    return ""


def _hook_status(hook: Any) -> str:
    if isinstance(hook, Mapping):
        return str(hook.get("status") or STATUS_CONVERTED)
    return STATUS_CONVERTED


def _hook_source(hook: Any, index: int) -> str:
    if isinstance(hook, Mapping):
        source = hook.get("source")
        if source:
            return str(source)
    return f"hook#{index}"


def _exec_actions(
    actions: Sequence[Mapping[str, Any]],
    *,
    source: str,
    context: Mapping[str, Any],
    rng: Callable[[], float],
    out: dict[str, Any],
    budget: list[int],
    depth: int = 1,
) -> None:
    """顺序执行 action 列表（含 ``then`` 嵌套）；写进 ``out``。"""
    for action in actions:
        if budget[0] >= MAX_ACTIONS_PER_TRIGGER:
            out["notes"].append(
                f"单次触发 action 已达上限 {MAX_ACTIONS_PER_TRIGGER}，剩余动作未执行（{source}）"
            )
            return
        budget[0] += 1
        kind = str(action.get("type") or "")
        target = str(action.get("target") or "self")
        payload: dict[str, Any] = {"type": kind, "source": source}
        if "target" in action:
            payload["target"] = target
        text = f"{source}: {kind}"
        if kind in ("damage.add", "damage.reduce"):
            dice = str(action.get("dice"))
            rolls, amount = _roll_dice(dice, rng)
            payload.update({"dice": dice, "rolls": rolls, "amount": amount})
            if kind == "damage.add":
                payload["damage_type"] = str(action.get("damage_type") or "")
                payload["on_save"] = str(action.get("on_save") or "full")
                text = (
                    f"{source}: 伤害 {dice}={amount} {payload['damage_type']} → {target}"
                    f"（豁免 {payload['on_save']}）"
                )
            else:
                text = f"{source}: 减伤 {dice}={amount} → {target}"
        elif kind == "heal":
            dice = str(action.get("amount"))
            rolls, amount = _roll_dice(dice, rng)
            payload.update({"dice": dice, "rolls": rolls, "amount": amount})
            text = f"{source}: 治疗 {dice}={amount} → {target}"
        elif kind == "temp_hp":
            payload["amount"] = int(action.get("amount") or 0)
            text = f"{source}: 临时生命 +{payload['amount']} → {target}"
        elif kind == "condition.add" or kind == "condition.remove":
            payload["condition"] = str(action.get("condition") or "")
            if kind == "condition.add":
                payload["duration"] = int(action.get("duration") or 0)
                text = f"{source}: 附加状态 {payload['condition']}（{payload['duration']} 回合）→ {target}"
            else:
                text = f"{source}: 移除状态 {payload['condition']} → {target}"
        elif kind == "resource.spend":
            payload["key"] = str(action.get("key") or "")
            payload["amount"] = int(action.get("amount") or 0)
            text = f"{source}: 消耗资源 {payload['key']} ×{payload['amount']}"
        elif kind.startswith("effect."):
            payload["name"] = str(action.get("name") or "")
            if kind == "effect.apply":
                payload["duration_rounds"] = int(action.get("duration_rounds") or 0)
                text = (
                    f"{source}: 施加效果「{payload['name']}」"
                    f"（{payload['duration_rounds']} 回合）→ {target}"
                )
            else:
                text = f"{source}: 移除效果「{payload['name']}」→ {target}"
        else:
            # attack./save./check. 的优势劣势：只是把 d20 模式告诉调用方。
            text = f"{source}: {kind}（{target}）"
        out["actions"].append(payload)
        out["events"].append({"kind": kind, "source": source, "text": text, "target": target})
        nested = action.get("then")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)) and nested:
            _exec_actions(
                nested,
                source=source,
                context=context,
                rng=rng,
                out=out,
                budget=budget,
                depth=depth + 1,
            )


def run(
    trigger: str,
    context: Mapping[str, Any] | None,
    hooks: Iterable[Any] | None,
    rng: Any = None,
) -> dict[str, Any]:
    """纯执行器：跑 ``trigger`` 命中的钩子，返回 ``{actions, events, notes}``。

    - ``hooks`` 既可以是 :func:`parse_workflow` 的输出（每项带 ``dsl`` /
      ``status`` / ``source``），也可以直接是 DSL 列表；
    - ``status`` 为 ``needs_review`` / ``ignored`` / ``native`` 的条目**不执行**
      （前一个是"人还没拍板"，后两个是"不该由 DSL 做"），只留一条 note；
    - 每个 DSL 在执行**前**再校验一次（防御性重复校验：调用方可能把坏数据存
      进了角色卡）；
    - 条件不成立 → 跳过（不是错误）；条件语法错 / 求值错 → 跳过 + note，
      **绝不**把"条件坏了"当成"条件成立"；
    - 所有随机都来自 ``rng``（见 :func:`_rng_of`），同一 seed 可重放；
    - 不改 ``context``、不改 ``hooks``、不写全局状态。
    """
    normalized_trigger = str(trigger or "").strip()
    if normalized_trigger not in TRIGGERS:
        raise HookError(f"未知触发点：{trigger!r}", "trigger.unknown")
    scope = dict(context or {})
    source_of_random = _rng_of(rng)
    out: dict[str, Any] = {"actions": [], "events": [], "notes": []}
    budget = [0]
    for index, hook in enumerate(list(hooks or [])):
        status = _hook_status(hook)
        source = _hook_source(hook, index)
        if status not in (STATUS_CONVERTED,):
            if status in KEPT_STATUSES:
                out["notes"].append(f"{source}: 状态 {status}，未执行（待人确认）")
            else:
                out["notes"].append(f"{source}: 状态 {status}，不属于 DSL 执行范围")
            continue
        dsl = hook.get("dsl") if isinstance(hook, Mapping) and "dsl" in hook else hook
        try:
            validated = validate_dsl(dsl)
        except HookError as exc:
            out["notes"].append(f"{source}: 校验失败，已拒绝执行（{exc}）")
            continue
        if validated["trigger"] != normalized_trigger:
            continue
        condition = validated.get("condition")
        if condition:
            try:
                if not eval_condition(condition, scope):
                    continue
            except (ConditionError, HookError) as exc:
                out["notes"].append(f"{source}: 条件求值失败，已跳过（{exc}）")
                continue
        _exec_actions(
            validated["actions"],
            source=source,
            context=scope,
            rng=source_of_random,
            out=out,
            budget=budget,
        )
    return out


# -- 导入期的钩子分诊 ------------------------------------------------------

#: midi-qol 的「宏字段」→ DSL 触发点。字段名按 midi-qol 的 item flags 口径。
MIDI_MACRO_FIELDS: dict[str, str] = {
    "onUseMacroName": "onUse",
    "onHitMacroName": "onHit",
    "onMissMacroName": "onMiss",
    "onSaveFailMacroName": "onSaveFail",
    "onSaveSuccessMacroName": "onSaveSuccess",
    "onTurnStartMacroName": "onTurnStart",
    "onDamagedMacroName": "onDamaged",
}

#: midi-qol 宏字段里的「pass 前缀」→ 触发点（``[preActiveEffects]ItemMacro``）。
MACRO_PASS_TRIGGERS: dict[str, str] = {
    "onuse": "onUse",
    "preitemroll": "onUse",
    "preactiveeffects": "onUse",
    "postactiveeffects": "onUse",
    "postitemroll": "onUse",
    "onhit": "onHit",
    "onmiss": "onMiss",
    "onsavefail": "onSaveFail",
    "onsavesuccess": "onSaveSuccess",
    "onturnstart": "onTurnStart",
    "ondamaged": "onDamaged",
}

#: midi-qol 的「引擎已实现」子集（``effect_changes`` 消费）：这里只标 ``native``。
MIDI_NATIVE_PREFIXES: tuple[str, ...] = (
    "advantage.",
    "disadvantage.",
    "critical.",
    "criticalThreshold",
)

#: midi-qol 的**纯配置**字段：没有钩子语义，标 ``ignored``（附中文理由）。
MIDI_CONFIG_KEYS: dict[str, str] = {
    "rollAttackPerTarget": "midi-qol 掷骰设置（每目标单独攻击），本引擎按单次攻击结算",
    "removeAttackDamageButtons": "midi-qol 聊天卡按钮外观，无规则语义",
    "fumbleThreshold": "midi-qol 大失败阈值（空/默认即 1），未作为钩子使用",
    "autoTarget": "midi-qol 自动选择目标设置，无规则语义",
    "AoETargetType": "midi-qol 范围目标类型设置，无规则语义",
    "AoETargetTypeIncludeSelf": "midi-qol 范围目标设置（是否含自身），无规则语义",
    "rangeOverride": "midi-qol 射程覆盖设置，由 activities.range 承担",
    "distanceOverride": "midi-qol 距离覆盖设置，由 activities.range 承担",
    "ignoreTotalCover": "midi-qol 掩护设置，本引擎未做掩护判定",
    "magicDamage": "midi-qol 魔法伤害标记，抗性口径由 effect_changes 承担",
}

#: midi-qol 的 **条件字段**（值是 JS 表达式，midi-qol 自己 ``eval``）→ 只要非空
#: 就必须 ``needs_review``：我们能读懂的是 DSL 条件，不是 JS。
MIDI_CONDITION_FIELDS: tuple[str, ...] = (
    "itemCondition",
    "effectCondition",
    "reactionCondition",
    "otherCondition",
)

#: ``flags.midiProperties.*``（midi-qol 旧命名空间）的已知配置项。
MIDI_PROPERTIES_KEYS: dict[str, str] = {
    "autoFailFriendly": "midiProperties 友军自动失败设置，无规则语义",
    "autoSaveFriendly": "midiProperties 友军自动成功设置，无规则语义",
    "magicdam": "midiProperties 魔法伤害标记，抗性口径由 effect_changes 承担",
    "magiceffect": "midiProperties 魔法效果标记，视觉/抗性口径，不进 DSL",
    "noConcentrationCheck": "midiProperties 专注检定开关，本引擎专注规则在 effects",
    "toggleEffect": "midiProperties 效果开关，由 effects 承担",
    "ignoreTotalCover": "midiProperties 掩护设置，本引擎未做掩护判定",
    "confirmTargets": "midiProperties 目标确认 UI 设置，无规则语义",
    "critOther": "midiProperties 暴击范围设置，本引擎暴击按 effect_changes 阈值",
    "offHandWeapon": "midiProperties 副手标记，由 combat 的武器口径承担",
    "idr": "midiProperties 大失败标记（id*），无独立规则语义",
    "idi": "midiProperties 大成功标记（id*），无独立规则语义",
    "idv": "midiProperties 大成功/大失败值，无独立规则语义",
    "ida": "midiProperties 大成功/大失败开关，无独立规则语义",
    "concentration": "midiProperties 专注标记，由 activities.duration 承担",
    "saveDamage": "midiProperties 豁免伤害口径，与 activities.*.damage.onSave 同义",
    "bonusSaveDamage": "midiProperties 加值伤害豁免口径，与 activities 同义",
    "otherSaveDamage": "midiProperties 旁支伤害豁免口径，与 activities 同义",
    "noDamageOnSave": "midiProperties 豁免成功不受伤开关，与 activities.*.damage.onSave 同义",
}

#: 纯「钩子型」模块命名空间：内部找不到宏字段也仍要人看一眼（模块行为未知）。
HOOK_NAMESPACES: tuple[str, ...] = (
    "ActiveAuras",
    "conditional-effects",
    "autoanimations",
)

#: 完全没有钩子语义的命名空间（合著/迁移/排序/同步元数据）→ ``ignored``，
#: 每个命名空间只记一条（不逐叶子刷屏），理由是中文可读的。
METADATA_NAMESPACES: dict[str, str] = {
    "dnd5e": "系统元数据（sourceId / 迁移标记 / 进阶来源）",
    "core": "FVTT 核心元数据",
    "arcanedesk": "索引模块同步戳（requestId / sourceUuid）",
    "arcane-dnd5e-2014-automation": "第三方自动化模块的编译产物（声明式计划，不是 JS 宏）",
    "babele": "Babele 翻译原文副本",
    "entityorder": "卡面排序",
    "sheetshare-mobile": "分享/移动端设置",
    "foundryvtt-actor-studio": "角色工坊的法术清单元数据",
    "_sheetTab": "卡面页签状态",
    "dynamiceffects": "DAE 旧版效果配置（effects 数组为空）",
    "magic-items-2": "魔法物品充能配置",
    "rest-recovery": "休整恢复配置",
    "exportSource": "导出溯源",
}

#: 按**路径分段**匹配的"钩子形态"片段——不做整串子串匹配，否则
#: ``description`` 会因为含 ``script`` 被误判（真实样本里踩过）。
HOOKISH_SEGMENT_TOKENS: tuple[str, ...] = (
    "macro",
    "macroname",
    "macrocommand",
    "macroexecute",
    "script",
    "hook",
    "hooks",
    "callback",
    "command",
    "runmacro",
    "itemmacro",
    "onusemacroname",
    "onhitmacroname",
    "onmissmacroname",
    "onsavefailmacroname",
    "onsavesuccessmacroname",
    "onturnstartmacroname",
    "ondamagedmacroname",
)

#: 已知宏模式表：宏名/来源命中哪一类，决定能不能安全等价。
KNOWN_MACRO_PATTERNS: dict[str, str] = {
    "itemmacro": "item",
    "dae": "dae",
    "activeauras": "aura",
    "conditional": "conditional",
    "conditional-effects": "conditional",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _entry(
    *,
    source: str,
    status: str,
    reason: str,
    trigger: str = "",
    dsl: Mapping[str, Any] | None = None,
    raw: Any = None,
    macro: str = "",
    macro_pass: str = "",
) -> dict[str, Any]:
    """构造一条分诊结果（``parse_workflow`` 的输出单元）。"""
    entry: dict[str, Any] = {
        "source": source,
        "trigger": trigger,
        "status": status,
        "dsl": dict(dsl) if dsl else None,
        "reason": reason,
        "raw": raw,
    }
    if macro:
        entry["macro"] = macro
    if macro_pass:
        entry["macro_pass"] = macro_pass
    return entry


def _split_pass(value: str) -> tuple[str, str]:
    """``"[preActiveEffects]ItemMacro"`` → ``("preActiveEffects", "ItemMacro")``。"""
    text = _text(value)
    if text.startswith("[") and "]" in text:
        head, _, tail = text[1:].partition("]")
        return head.strip(), tail.strip()
    return "", text


def trigger_for_pass(pass_name: str) -> str:
    return MACRO_PASS_TRIGGERS.get(_text(pass_name).casefold(), "")


def macro_kind(name: str) -> str:
    """宏名 → 已知模式键（``item`` / ``dae`` / ``aura`` / ``conditional`` / ``""``）。"""
    text = _text(name).casefold()
    if not text:
        return ""
    for pattern, kind in KNOWN_MACRO_PATTERNS.items():
        if pattern in text:
            return kind
    if text.startswith("compendium.") or text.startswith("macro."):
        return "external"
    return ""


def _item_damage_parts(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """物品自带的伤害档（dnd5e 3.x ``system.damage`` 与 4.x ``activities``）。

    返回 ``[{"dice": "5d8", "damage_type": "poison", "on_save": "half", "activity": "…"}]``；
    解析不出合法骰式的档直接跳过（调用方据此决定能不能转 DSL）。
    """
    system = _as_mapping(doc.get("system"))
    parts: list[dict[str, Any]] = []

    def push(dice: Any, damage_type: Any, on_save: Any, activity: str) -> None:
        try:
            parse_dice(dice)
        except HookError:
            return
        dtype = _text(damage_type).casefold() or "bludgeoning"
        if dtype not in DAMAGE_TYPES:
            return
        mode = _text(on_save).casefold()
        parts.append(
            {
                "dice": _text(dice),
                "damage_type": dtype,
                "on_save": mode if mode in ON_SAVE_MODES else "full",
                "activity": activity,
            }
        )

    # dnd5e 3.x：``system.damage.parts = [["1d8", "slashing"], …]``
    legacy = _as_mapping(system.get("damage"))
    for part in legacy.get("parts") or []:
        if isinstance(part, (list, tuple)) and len(part) >= 2:
            push(part[0], part[1], "full", "")
    # dnd5e 4.x：``system.activities.<id>.damage.parts = [{number, denomination, types}]``
    for activity_id, activity in sorted(_as_mapping(system.get("activities")).items()):
        rows = _as_mapping(_as_mapping(activity).get("damage"))
        on_save = rows.get("onSave")
        name = _text(_as_mapping(activity).get("name")) or str(activity_id)
        for part in rows.get("parts") or []:
            if not isinstance(part, Mapping):
                continue
            number = part.get("number")
            denomination = part.get("denomination")
            bonus = _text(part.get("bonus"))
            if not isinstance(number, int) or not isinstance(denomination, int):
                continue
            dice = f"{number}d{denomination}{bonus}" if bonus else f"{number}d{denomination}"
            types = part.get("types") or []
            dtype = types[0] if isinstance(types, (list, tuple)) and types else ""
            push(dice, dtype, on_save, name)
    return parts


def _damage_dsl(trigger: str, parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for part in parts[:MAX_ACTIONS_PER_TRIGGER]:
        actions.append(
            {
                "type": "damage.add",
                "dice": part["dice"],
                "damage_type": part["damage_type"],
                "target": "hit",
                "on_save": part["on_save"],
            }
        )
    return {"trigger": trigger, "actions": actions}


def _convert_midi_macro(
    *,
    field: str,
    default_trigger: str,
    value: Any,
    doc: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """``flags.midi-qol.onUseMacroName`` 之类 → 分诊结果（可能是多条）。"""
    raw_text = _text(value)
    source = f"flags.midi-qol.{field}"
    if not raw_text:
        return [
            _entry(
                source=source,
                status=STATUS_IGNORED,
                reason="宏字段为空字符串（没有挂宏）",
                raw=value,
            )
        ]
    entries: list[dict[str, Any]] = []
    parts = _item_damage_parts(doc)
    # midi-qol 允许 "；" 分隔多个宏，每个宏自带 [pass] 前缀。
    for chunk in [piece for piece in raw_text.split(";") if piece.strip()]:
        pass_name, macro_name = _split_pass(chunk)
        trigger = trigger_for_pass(pass_name) or default_trigger
        kind = macro_kind(macro_name)
        if kind == "item" and parts:
            dsl = _damage_dsl(trigger, parts)
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_CONVERTED,
                    trigger=trigger,
                    dsl=dsl,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason=(
                        "ItemMacro + 物品自带伤害档 → damage.add"
                        f"（{len(dsl['actions'])} 段，活动：{parts[0]['activity'] or 'system.damage'}）；"
                        "钩子的其余副作用仍需人工确认，见同源 needs_review 条目"
                    ),
                )
            )
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    trigger=trigger,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason=(
                        "ItemMacro 的脚本副作用（模板/区域/对话框/资源经济）不在 DSL 白名单内；"
                        "伤害部分已单独转为 damage.add，其余需人工确认"
                    ),
                )
            )
            continue
        if kind == "item":
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    trigger=trigger,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason=(
                        "ItemMacro 没有可静态映射的伤害档（副作用是资源经济/对话框/区域光环），"
                        "DSL 无法等价表达"
                    ),
                )
            )
            continue
        if kind == "aura":
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    trigger=trigger,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason="ActiveAuras 模板光环：DSL 没有区域/模板语义（只能 effect.apply 到单体）",
                )
            )
            continue
        if kind == "conditional":
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    trigger=trigger,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason="Conditional 宏：条件本身可能已能用 DSL condition 表达，但宏体是 JS，需人工翻译",
                )
            )
            continue
        if kind == "external":
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    trigger=trigger,
                    raw=chunk,
                    macro=macro_name,
                    macro_pass=pass_name,
                    reason="宏名指向外部文档（Compendium/Macro），包里没有它的正文，无法安全等价",
                )
            )
            continue
        entries.append(
            _entry(
                source=source,
                status=STATUS_NEEDS_REVIEW,
                trigger=trigger,
                raw=chunk,
                macro=macro_name,
                macro_pass=pass_name,
                reason=f"宏名「{macro_name}」不在已知宏模式表（ItemMacro/DAE/ActiveAuras/Conditional），需人工确认",
            )
        )
    return entries


def _triage_midi_flags(flags: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``flags.midi-qol.*`` 的**非宏**字段分诊（native / ignored / needs_review）。"""
    entries: list[dict[str, Any]] = []
    for key in sorted(flags):
        if key in MIDI_MACRO_FIELDS:
            continue
        value = flags[key]
        source = f"flags.midi-qol.{key}"
        if key in MIDI_CONDITION_FIELDS:
            text = _text(value)
            if not text:
                entries.append(
                    _entry(source=source, status=STATUS_IGNORED, reason="条件字段为空（没启用）", raw=value)
                )
            else:
                entries.append(
                    _entry(
                        source=source,
                        status=STATUS_NEEDS_REVIEW,
                        raw=value,
                        reason="midi-qol 条件字段是 JS 表达式，需人工改写成 DSL condition",
                    )
                )
            continue
        if any(key.startswith(prefix) for prefix in MIDI_NATIVE_PREFIXES):
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NATIVE,
                    raw=value,
                    reason="advantage/disadvantage/critical 子集已由 effect_changes 消费（勿重复实现）",
                )
            )
            continue
        reason = MIDI_CONFIG_KEYS.get(key)
        if reason:
            entries.append(_entry(source=source, status=STATUS_IGNORED, reason=reason, raw=value))
            continue
        if isinstance(value, Mapping):
            for leaf, leaf_value in sorted(value.items()):
                leaf_source = f"{source}.{leaf}"
                leaf_reason = MIDI_CONFIG_KEYS.get(str(leaf))
                if leaf_reason:
                    entries.append(
                        _entry(source=leaf_source, status=STATUS_IGNORED, reason=leaf_reason, raw=leaf_value)
                    )
                elif any(str(leaf).startswith(prefix) for prefix in MIDI_NATIVE_PREFIXES):
                    entries.append(
                        _entry(
                            source=leaf_source,
                            status=STATUS_NATIVE,
                            raw=leaf_value,
                            reason="midi-qol grants/优势子集：由 effect_changes 消费",
                        )
                    )
                else:
                    entries.append(
                        _entry(
                            source=leaf_source,
                            status=STATUS_NEEDS_REVIEW,
                            raw=leaf_value,
                            reason=f"未识别的 midi-qol 字段「{leaf}」，需人工确认是否有规则语义",
                        )
                    )
            continue
        entries.append(
            _entry(
                source=source,
                status=STATUS_NEEDS_REVIEW,
                raw=value,
                reason=f"未识别的 midi-qol 字段「{key}」，需人工确认是否有规则语义",
            )
        )
    return entries


def _triage_legacy_midi_properties(flags: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key in sorted(flags):
        value = flags[key]
        source = f"flags.midiProperties.{key}"
        reason = MIDI_PROPERTIES_KEYS.get(key)
        if reason:
            entries.append(_entry(source=source, status=STATUS_IGNORED, reason=reason, raw=value))
        else:
            entries.append(
                _entry(
                    source=source,
                    status=STATUS_NEEDS_REVIEW,
                    raw=value,
                    reason=f"未识别的 midiProperties 字段「{key}」，需人工确认",
                )
            )
    return entries


def _triage_dae(flags: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``flags.dae.macro.*``：DAE 内嵌宏，正文是 JS，只保存不执行。"""
    macro = _as_mapping(flags.get("macro"))
    if not macro:
        return []
    command = _text(macro.get("command"))
    name = _text(macro.get("name"))
    if not command:
        return [
            _entry(
                source="flags.dae.macro",
                status=STATUS_IGNORED,
                reason="DAE 宏没有正文（command 为空）",
                raw={"name": name},
            )
        ]
    entries = [
        _entry(
            source="flags.dae.macro.command",
            status=STATUS_NEEDS_REVIEW,
            raw=command,
            macro=name,
            reason=(
                f"DAE 内嵌 JS 宏（{len(command)} 字符，type={_text(macro.get('type')) or '?'}）；"
                "本引擎不执行 JS，原文保留供人工翻译成 DSL"
            ),
        )
    ]
    for key in ("name", "type", "scope", "img"):
        if key in macro:
            entries.append(
                _entry(
                    source=f"flags.dae.macro.{key}",
                    status=STATUS_IGNORED,
                    reason="DAE 宏元数据（不参与规则结算）",
                    raw=macro.get(key),
                )
            )
    return entries


def _triage_item_macro(flags: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``flags.itemacro.*``（Item Macro 模块）：宏正文同样是 JS。"""
    rows = _as_mapping(flags.get("macro")) or flags
    command = _text(rows.get("command") if isinstance(rows, Mapping) else "")
    if not command:
        return [
            _entry(
                source="flags.itemacro",
                status=STATUS_IGNORED,
                reason="Item Macro 没有正文（command 为空）",
                raw=None,
            )
        ]
    return [
        _entry(
            source="flags.itemacro.macro.command",
            status=STATUS_NEEDS_REVIEW,
            raw=command,
            reason=f"Item Macro 内嵌 JS 宏（{len(command)} 字符）；不执行，原文保留供人工翻译",
        )
    ]


def _path_segments(path: str) -> list[str]:
    """``"flags.dae.macro.command"`` → ``["flags", "dae", "macro", "command"]``。

    列表下标（``rules[0]``）在比较前摘掉，避免 ``[0]`` 把 ``ondamaged`` 之类
    的分段切坏（防呆；真实样本里没有这种形态）。
    """
    segments: list[str] = []
    for part in str(path).split("."):
        text = part.split("[", 1)[0].strip().casefold()
        if text:
            segments.append(text)
    return segments


def is_hookish_path(path: str) -> bool:
    """路径里是否有"钩子形态"的分段（宏/脚本/命令/回调）。

    只按**分段**判等/前缀/后缀，不做整串子串匹配：``description`` 里恰好含
    ``script``，整串匹配会把它误判成钩子（真实样本踩过这个坑）。
    """
    for segment in _path_segments(path):
        for token in HOOKISH_SEGMENT_TOKENS:
            if segment == token or segment.startswith(token) or segment.endswith(token):
                return True
    return False


def _triage_hookish_leaf(path: str, value: Any) -> dict[str, Any] | None:
    """未知命名空间里的"像钩子"的叶子 → needs_review；不像的返回 ``None``。"""
    if not is_hookish_path(path):
        return None
    return _entry(
        source=path,
        status=STATUS_NEEDS_REVIEW,
        raw=value,
        reason="键名像钩子（macro/script/command/callback）但命名空间未收录，需人工确认",
    )


def _triage_activity_macros(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """dnd5e 4.x ``system.activities.<id>.macroData``：活动自带宏（JS）。

    有空宏的活动只记一条汇总（heroes 包 100+ 个活动宏全是空串，逐条记录只会
    把计数灌成噪音）；有正文的逐条 ``needs_review`` 并保留原文。
    """
    entries: list[dict[str, Any]] = []
    activities = _as_mapping(_as_mapping(doc.get("system")).get("activities"))
    empty_count = 0
    for activity_id, activity in sorted(activities.items()):
        rows = _as_mapping(_as_mapping(activity).get("macroData"))
        if not rows:
            continue
        command = _text(rows.get("command"))
        name = _text(rows.get("name"))
        if command:
            entries.append(
                _entry(
                    source=f"system.activities.{activity_id}.macroData.command",
                    status=STATUS_NEEDS_REVIEW,
                    raw=command,
                    macro=name,
                    reason=f"活动内嵌 JS 宏（{len(command)} 字符）；不执行，原文保留供人工翻译",
                )
            )
        elif name:
            entries.append(
                _entry(
                    source=f"system.activities.{activity_id}.macroData",
                    status=STATUS_IGNORED,
                    reason=f"活动宏只填了名字「{name}」、没有正文（不影响结算）",
                    raw=name,
                )
            )
        else:
            empty_count += 1
    if empty_count:
        entries.append(
            _entry(
                source="system.activities.*.macroData",
                status=STATUS_IGNORED,
                reason=f"{empty_count} 个活动自带空宏（command/name 均为空串）",
                raw=None,
            )
        )
    return entries


def parse_workflow(doc: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """把一个 FVTT item 文档上的 workflow 钩子分诊成 DSL / 待确认清单。

    识别范围（真实样本都命中过）：

    - ``flags.midi-qol.{onUse,onHit,onMiss,onSaveFail,onSaveSuccess,onTurnStart,
      onDamaged}MacroName``：命中 :data:`KNOWN_MACRO_PATTERNS` 且物品自带伤害档
      → 转 ``damage.add``（``converted``）；宏体其余副作用 → ``needs_review``；
    - ``flags.midi-qol.advantage.*`` / ``disadvantage.*`` / ``critical.*`` /
      ``criticalThreshold`` → ``native``（``effect_changes`` 已实现，**不重复实现**）；
    - ``flags.midi-qol.{item,effect,reaction,other}Condition`` 非空 → ``needs_review``
      （JS 表达式）；空 → ``ignored``；
    - ``flags.midi-qol.*`` 的纯配置项 → ``ignored``（附中文理由）；不认识的 → ``needs_review``；
    - ``flags.midiProperties.*``（旧命名空间）→ 已知配置 ``ignored``，其余 ``needs_review``；
    - ``flags.dae.macro.command`` / ``flags.itemacro`` / ``system.activities.*.macroData.command``
      → ``needs_review``（**原文照抄保存**，绝不执行）；
    - 其他命名空间里"像钩子"的叶子（键名含 macro/script/command/callback）→ ``needs_review``；
    - 合著/迁移/排序类元数据命名空间 → ``ignored``。

    返回列表元素的形状见 :func:`_entry`；``needs_review`` 条目**一定**带 ``raw``
    （原文），供报告与用户拍板，不丢弃。
    """
    flags = _as_mapping(_as_mapping(doc).get("flags"))
    entries: list[dict[str, Any]] = []
    if not flags:
        # 没有 flags 也别急着返回：``system.activities.*.macroData`` 是 dnd5e
        # 自带的宏位，可以单独挂宏。
        return _triage_activity_macros(_as_mapping(doc))

    midi = _as_mapping(flags.get("midi-qol"))
    for field, default_trigger in MIDI_MACRO_FIELDS.items():
        if field in midi:
            entries.extend(
                _convert_midi_macro(
                    field=field,
                    default_trigger=default_trigger,
                    value=midi.get(field),
                    doc=_as_mapping(doc),
                )
            )
    entries.extend(_triage_midi_flags(midi))

    legacy = _as_mapping(flags.get("midiProperties"))
    if legacy:
        entries.extend(_triage_legacy_midi_properties(legacy))

    dae = _as_mapping(flags.get("dae"))
    if dae:
        entries.extend(_triage_dae(dae))

    if "itemacro" in flags:
        entries.extend(_triage_item_macro(_as_mapping(flags.get("itemacro"))))

    for namespace in sorted(flags):
        if namespace in ("midi-qol", "midiProperties", "dae", "itemacro"):
            continue
        value = flags[namespace]
        if not value:
            # ``flags.core: {}`` / ``flags.dae: {}`` 这类空命名空间没有可记的东西
            # （FVTT 自己会留空壳），记一条 ignored 只会污染统计。
            continue
        leaves = _leaf_rows(value, namespace)
        hookish_rows = [
            entry
            for entry in (_triage_hookish_leaf(leaf, leaf_value) for leaf, leaf_value in leaves)
            if entry is not None
        ]
        if hookish_rows:
            entries.extend(hookish_rows)
            continue
        # 没有"钩子形态"的叶子：按命名空间归类记账，不逐叶子刷屏
        # （heroes 包 3000+ 个 flags 叶子，逐条记录只会把报告淹掉）。
        if namespace in HOOK_NAMESPACES:
            entries.append(
                _entry(
                    source=f"flags.{namespace}",
                    status=STATUS_NEEDS_REVIEW,
                    raw=_compact(value),
                    reason=f"钩子型模块「{namespace}」的语义未收录，需人工确认",
                )
            )
            continue
        reason = METADATA_NAMESPACES.get(namespace)
        entries.append(
            _entry(
                source=f"flags.{namespace}",
                status=STATUS_IGNORED,
                raw=None,
                reason=(
                    f"{reason}（{len(leaves)} 个字段，无钩子形态）"
                    if reason
                    else f"未收录的模块命名空间「{namespace}」（{len(leaves)} 个字段，无钩子形态）"
                ),
            )
        )

    entries.extend(_triage_activity_macros(_as_mapping(doc)))
    return entries


def _leaf_rows(value: Any, prefix: str) -> list[tuple[str, Any]]:
    """``{"a": {"b": {"c": 1}}}`` → ``[("flags.ns.a.b.c", 1)]``（标量叶子）。"""
    rows: list[tuple[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key in sorted(node, key=lambda item: str(item)):
                walk(node[key], f"{path}.{key}" if path else str(key))
            if not node:
                rows.append((path, node))
            return
        if isinstance(node, (list, tuple)):
            rows.append((path, node))
            return
        rows.append((path, node))

    walk(value, prefix)
    return rows


def _compact(value: Any, limit: int = 240) -> Any:
    """把任意 JSON 值压成**可写进角色卡**的短形态（长文本截断并标注长度）。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…（{len(value)} 字符，全文见审计报告）"
    if isinstance(value, Mapping):
        return {str(key): _compact(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(item, limit) for item in value]
    return value


def hook_stats(entries: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按状态计数（导入器的 audit 用），固定键、缺项补 0。"""
    stats = {
        STATUS_CONVERTED: 0,
        STATUS_NEEDS_REVIEW: 0,
        STATUS_NATIVE: 0,
        STATUS_IGNORED: 0,
        "total": 0,
    }
    for entry in entries:
        status = str(entry.get("status") or "")
        if status in stats:
            stats[status] += 1
        stats["total"] += 1
    return stats


def kept_hooks(entries: Iterable[Mapping[str, Any]], *, raw_limit: int = 240) -> list[dict[str, Any]]:
    """筛出要写进角色卡的条目（``converted`` / ``needs_review``），压缩 ``raw``。

    ``ignored`` / ``native`` 不落卡：前者没有行为、后者已由 ``effect_changes``
    另行承担，重复写只会让卡片变胖。
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        status = str(entry.get("status") or "")
        if status not in KEPT_STATUSES:
            continue
        rows.append(
            {
                "source": str(entry.get("source") or ""),
                "trigger": str(entry.get("trigger") or ""),
                "status": status,
                "reason": str(entry.get("reason") or ""),
                "dsl": entry.get("dsl"),
                "raw": _compact(entry.get("raw"), raw_limit),
                **(
                    {"macro": entry["macro"]}
                    if isinstance(entry.get("macro"), str) and entry.get("macro")
                    else {}
                ),
                **(
                    {"macro_pass": entry["macro_pass"]}
                    if isinstance(entry.get("macro_pass"), str) and entry.get("macro_pass")
                    else {}
                ),
            }
        )
    return rows


__all__ = [
    "ACTION_SPECS",
    "ALLOWED_DIE_SIDES",
    "DAMAGE_TYPES",
    "FORBIDDEN_ACTION_NAMES",
    "HOOK_NAMESPACES",
    "KEPT_STATUSES",
    "KNOWN_MACRO_PATTERNS",
    "MACRO_PASS_TRIGGERS",
    "MAX_ACTIONS_PER_TRIGGER",
    "MAX_NESTING_DEPTH",
    "MIDI_MACRO_FIELDS",
    "ConditionError",
    "HookError",
    "STATUS_CONVERTED",
    "STATUS_IGNORED",
    "STATUS_NEEDS_REVIEW",
    "STATUS_NATIVE",
    "TARGETS",
    "TRIGGERS",
    "eval_condition",
    "hook_stats",
    "kept_hooks",
    "macro_kind",
    "parse_condition",
    "parse_dice",
    "parse_workflow",
    "run",
    "trigger_for_pass",
    "validate_action",
    "validate_dsl",
]
