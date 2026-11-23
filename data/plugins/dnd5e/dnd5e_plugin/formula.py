"""D&D 5e 安全公式求值器（FVTT 公式字段 / 效果引擎 / AI 工具共用）。

设计口径：

- **只用 AST 白名单求值**：``ast.parse(mode="eval")`` 之后逐节点按白名单判类型，
  自己走一遍树；全程不 ``eval`` / ``exec`` / ``compile`` 外部代码，也不读文件、
  网络或环境变量，更不触及 ``__import__`` / ``getattr`` / ``open``。
- 支持的子集只有：数字、字符串、布尔、``+ - * / // %``、``== != < <= > >=``、
  ``and or not``、括号、一元 ``+/-``、骰式 ``NdM`` / ``dM``、``@path.to.value``
  引用、白名单函数。属性访问（``x.y``）、下标、``lambda``、推导式、f-string、
  赋值、幂运算一律按「不支持」拒绝，并给出可读中文错误。
- 越界即报错，不静默截断：公式长度 ≤ 256、AST 节点 ≤ 200、递归深度 ≤ 32、
  单骰式骰数 ≤ 100、骰面 ≤ 1000、全公式总骰数 ≤ 200。
- 错误统一返回 ``{"ok": False, "code": "formula.xxx", "error": "中文可读"}``，
  不抛异常（:func:`evaluate_number` 例外：它把错误以 :class:`FormulaError` 抛出，
  方便引擎侧 ``try/except`` 回退）。

典型用法::

    >>> evaluate("1d6 + @abilities.str.mod", {"abilities": {"str": 16}})
    {'ok': True, 'value': 6, 'rolls': [...], 'formula': ..., 'resolved': '3 + 3'}
    >>> evaluate_number("max(30, 30 + @bonuses.speed)", {"bonuses": {"speed": 10}})
    40
"""

from __future__ import annotations

import ast
import math
import random
import re
from typing import Any, Callable, Mapping

__all__ = [
    "FormulaError",
    "MAX_DEPTH",
    "MAX_DICE_COUNT",
    "MAX_DIE_SIDES",
    "MAX_LENGTH",
    "MAX_NODES",
    "MAX_TOTAL_DICE",
    "evaluate",
    "evaluate_number",
    "is_formula",
    "roll_data_from_sheet",
]

# ---------------------------------------------------------------- 安全护栏

#: 公式原文长度上限（字符）。
MAX_LENGTH = 256
#: 单次求值允许的 AST 节点数上限（含运算符节点）。
MAX_NODES = 200
#: 表达树递归深度上限。
MAX_DEPTH = 32
#: 单个骰式允许的最大骰数（``100d6`` 可以，``101d6`` 拒绝）。
MAX_DICE_COUNT = 100
#: 单颗骰子的面数上限。
MAX_DIE_SIDES = 1000
#: 一条公式里所有骰式的骰数总和上限。
MAX_TOTAL_DICE = 200

#: 骰子随机源：``rng(sides) -> int``，返回 1..sides（与核心检定工具的 rng 同签名）。
Rng = Callable[[int], int]


class FormulaError(Exception):
    """公式求值失败；``code`` 是稳定错误码，``message`` 是中文可读说明。"""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)

    def __str__(self) -> str:  # pragma: no cover - 只影响打印
        return self.message


def _error(code: str, message: str) -> FormulaError:
    return FormulaError(code, message)


# ---------------------------------------------------------------- 词法预处理

# ``@a.b.c`` —— 只认标识符段，遇到运算符/空格即停。
_REF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# ``NdM`` / ``dM``（大小写不敏感）；骰数省略表示 1。
_DICE_RE = re.compile(r"(\d{0,3})[dD](\d{1,4})")
# 普通数字字面量（小数可省尾）。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 标识符（函数名）。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# ``is_formula`` 用的轻量判据。
_PURE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_LOOSE_DICE_RE = re.compile(r"\d{0,3}[dD]\d+")
_FUNCTION_CALL_RE = re.compile(r"\b(?:max|min|floor|ceil|round|abs|clamp|sign|if)\s*\(")
_PLAIN_EXPR_RE = re.compile(r"^[-+*/%().\d\s]+$")

# 不允许出现的控制字符（换行/制表符一律当语法错误，避免多行藏东西）。
_FORBIDDEN_CHARS = ("\n", "\r", "\t", "\f", "\v", "\0")


class _Compiled:
    """预处理结果：改写后的表达式 + 引用/骰式登记表 + 原文替换位置。"""

    __slots__ = ("expression", "refs", "dice", "spans", "source")

    def __init__(
        self,
        expression: ast.Expression,
        refs: dict[str, tuple[str, int, int]],
        dice: dict[str, tuple[int, int]],
        spans: list[tuple[int, int, str]],
        source: str,
    ) -> None:
        self.expression = expression
        self.refs = refs
        self.dice = dice
        self.spans = spans
        self.source = source


def _scan_string(text: str, start: int) -> int:
    """返回字符串字面量的结束下标（不含）；未闭合时报错。"""
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    raise _error("formula.syntax", "公式里的字符串引号没有闭合。")


def _scan(text: str) -> tuple[str, dict[str, tuple[str, int, int]], dict[str, tuple[int, int]], list[tuple[int, int, str]]]:
    """把 ``@ref`` 与骰式换成占位标识符，产出可交给 ``ast.parse`` 的纯表达式。"""
    chunks: list[str] = []
    refs: dict[str, tuple[str, int, int]] = {}
    dice: dict[str, tuple[int, int]] = {}
    spans: list[tuple[int, int, str]] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char in ("'", '"'):
            end = _scan_string(text, index)
            chunks.append(text[index:end])
            index = end
            continue
        if char == "@":
            match = _REF_RE.match(text, index + 1)
            if match is None:
                raise _error(
                    "formula.ref_syntax",
                    "「@」后面要跟引用路径，例如 @abilities.str.mod。",
                )
            path = match.group(0)
            key = f"__ref{len(refs)}__"
            refs[key] = (path, index, match.end())
            spans.append((index, match.end(), key))
            # 尾随空格：占位符不能和相邻标识符粘成一个名字（如 ``1d6@2``）。
            chunks.append(key + " ")
            index = match.end()
            continue
        if char in "dD" or char in "0123456789":
            match = _DICE_RE.match(text, index)
            if match is not None:
                count_text, sides_text = match.group(1), match.group(2)
                count = int(count_text) if count_text else 1
                sides = int(sides_text)
                if count < 1 or count > MAX_DICE_COUNT:
                    raise _error(
                        "formula.dice_count",
                        f"骰式的骰数必须在 1 到 {MAX_DICE_COUNT} 之间，收到 {count}。",
                    )
                if sides < 1 or sides > MAX_DIE_SIDES:
                    raise _error(
                        "formula.dice_sides",
                        f"骰子面数必须在 1 到 {MAX_DIE_SIDES} 之间，收到 {sides}。",
                    )
                key = f"__die{len(dice)}__"
                dice[key] = (count, sides)
                spans.append((index, match.end(), key))
                chunks.append(key + " ")
                index = match.end()
                continue
            if char in "0123456789":
                number = _NUMBER_RE.match(text, index)
                assert number is not None  # 当前位置就是数字
                tail = text[number.end() : number.end() + 1]
                if tail in ("d", "D"):
                    raise _error(
                        "formula.dice_count",
                        f"骰式写法不正确：骰数必须是 1 到 {MAX_DICE_COUNT} 之间的整数（如 2d6）。",
                    )
                chunks.append(number.group(0))
                index = number.end()
                continue
            ident = _IDENT_RE.match(text, index)
            if ident is not None and ident.group(0) not in ("d", "D"):
                chunks.append(ident.group(0))
                index = ident.end()
                continue
            raise _error("formula.dice_syntax", "骰式要写成 NdM（例如 2d6）或 d20。")
        if char.isalpha() or char == "_":
            ident = _IDENT_RE.match(text, index)
            if ident is None:
                raise _error(
                    "formula.syntax",
                    f"公式里有不支持的字符「{char}」；字段名请写成 @路径 的形式。",
                )
            name = ident.group(0)
            # ``if(...)`` 在白名单函数里，但 if 是 Python 关键字，先改名再解析。
            chunks.append("__fn_if" if name == "if" else name)
            index = ident.end()
            continue
        chunks.append(char)
        index += 1
    return "".join(chunks), refs, dice, spans


# ---------------------------------------------------------------- AST 白名单

_ALLOWED_NODES = frozenset(
    {
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.Call,
        ast.Name,
        ast.Constant,
        ast.Load,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    }
)

_NODE_LABELS: dict[type, str] = {
    ast.Attribute: "属性访问（x.y）",
    ast.Subscript: "下标（x[0]）",
    ast.Lambda: "lambda",
    ast.ListComp: "列表推导式",
    ast.SetComp: "集合推导式",
    ast.DictComp: "字典推导式",
    ast.GeneratorExp: "生成器表达式",
    ast.List: "列表字面量",
    ast.Tuple: "元组字面量",
    ast.Dict: "字典字面量",
    ast.Set: "集合字面量",
    ast.JoinedStr: "f-string",
    ast.FormattedValue: "f-string",
    ast.IfExp: "条件表达式（用 if(cond, a, b) 代替）",
    ast.NamedExpr: "海象运算符",
    ast.Starred: "星号展开",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield from",
    ast.Pow: "幂运算",
    ast.MatMult: "矩阵乘法",
    ast.LShift: "位移",
    ast.RShift: "位移",
    ast.BitAnd: "位运算",
    ast.BitOr: "位运算",
    ast.BitXor: "位运算",
    ast.Invert: "按位取反",
}

# ``if`` 在 Python 里是关键字，扫描期已改名，展示时再还原。
_CALL_NAMES = {
    "max": "max",
    "min": "min",
    "floor": "floor",
    "ceil": "ceil",
    "round": "round",
    "abs": "abs",
    "clamp": "clamp",
    "sign": "sign",
    "__fn_if": "if",
}
_ALIASED = {"__fn_if": "if"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_number(value: Any, *, what: str) -> int | float:
    if not _is_number(value):
        raise _error("formula.type", f"{what}需要数字，实际拿到 {type(value).__name__}。")
    if isinstance(value, float) and not math.isfinite(value):
        raise _error("formula.value", "公式算出了无穷大或非数值，请检查除数与数字量级。")
    return value


def _as_int(value: Any, *, what: str) -> int:
    number = _as_number(value, what=what)
    if isinstance(number, float):
        if not number.is_integer():
            raise _error("formula.type", f"{what}需要整数，实际拿到 {number}。")
        return int(number)
    return number


def _round_half_away(value: int | float) -> int:
    """四舍五入（0.5 远离零），与 FVTT 的 ``Math.round`` 口径一致。"""
    number = float(value)
    if number >= 0:
        return int(math.floor(number + 0.5))
    return int(math.ceil(number - 0.5))


def _fn_clamp(args: list[Any]) -> Any:
    low = _as_number(args[1], what="clamp() 的下限")
    high = _as_number(args[2], what="clamp() 的上限")
    value = _as_number(args[0], what="clamp() 的第一个参数")
    if low > high:
        low, high = high, low
    return max(low, min(high, value))


def _fn_sign(args: list[Any]) -> int:
    value = _as_number(args[0], what="sign() 的参数")
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _fn_if(args: list[Any]) -> Any:
    return args[1] if args[0] else args[2]


def _fn_abs(args: list[Any]) -> Any:
    return abs(_as_number(args[0], what="abs() 的参数"))


#: 白名单函数：名字 → (最少参数, 最多参数, 实现)
_FUNCTIONS: dict[str, tuple[int, int, Callable[[list[Any]], Any]]] = {
    "max": (1, 8, lambda args: max(_as_number(item, what="max() 的参数") for item in args)),
    "min": (1, 8, lambda args: min(_as_number(item, what="min() 的参数") for item in args)),
    "floor": (1, 1, lambda args: int(math.floor(_as_number(args[0], what="floor() 的参数")))),
    "ceil": (1, 1, lambda args: int(math.ceil(_as_number(args[0], what="ceil() 的参数")))),
    "round": (1, 1, lambda args: _round_half_away(_as_number(args[0], what="round() 的参数"))),
    "abs": (1, 1, _fn_abs),
    "clamp": (3, 3, _fn_clamp),
    "sign": (1, 1, _fn_sign),
    "__fn_if": (3, 3, _fn_if),
}


def _validate(node: ast.AST, *, depth: int, counter: list[int], refs: dict[str, Any], dice: dict[str, Any]) -> None:
    """白名单 + 规模校验；不通过直接抛 :class:`FormulaError`。"""
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise _error(
            "formula.too_complex",
            f"公式太复杂（AST 节点超过 {MAX_NODES} 个），请拆成多条公式。",
        )
    if depth > MAX_DEPTH:
        raise _error(
            "formula.too_deep",
            f"公式嵌套太深（超过 {MAX_DEPTH} 层），请化简括号。",
        )
    kind = type(node)
    if kind not in _ALLOWED_NODES:
        label = _NODE_LABELS.get(kind, kind.__name__)
        raise _error("formula.unsupported", f"公式不支持{label}；只允许算术、比较、逻辑、骰式、@ 引用与白名单函数。")
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            if isinstance(value, float) and not math.isfinite(value):
                raise _error("formula.value", "公式里的数字不是有限值。")
            return
        raise _error("formula.unsupported", f"公式不支持这种字面量：{value!r}。")
    if isinstance(node, ast.Name):
        if node.id in refs or node.id in dice:
            return
        name = _ALIASED.get(node.id, node.id)
        if node.id in _CALL_NAMES:
            raise _error("formula.unknown_name", f"函数 {name} 要写成调用形式，例如 {name}(...)。")
        raise _error(
            "formula.unknown_name",
            f"公式里有不认识的标识符「{name}」；引用上下文请写成 @路径，函数只能用 "
            "max/min/floor/ceil/round/abs/clamp/sign/if。",
        )
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _error("formula.unsupported", "公式只能调用白名单函数，不能调用表达式的返回值。")
        key = node.func.id
        if key not in _FUNCTIONS:
            shown = _ALIASED.get(key, key)
            raise _error(
                "formula.unknown_function",
                f"公式不支持函数「{shown}」；可用 max/min/floor/ceil/round/abs/clamp/sign/if。",
            )
        if node.keywords:
            raise _error("formula.unsupported", "公式里的函数调用不支持关键字参数。")
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                raise _error("formula.unsupported", "公式里的函数调用不支持 * 展开。")
        low, high, _ = _FUNCTIONS[key]
        if not (low <= len(node.args) <= high):
            shown = _ALIASED.get(key, key)
            expected = f"{low}" if low == high else f"{low}–{high}"
            raise _error(
                "formula.bad_arity",
                f"{shown}() 需要 {expected} 个参数，收到 {len(node.args)} 个。",
            )
        # 只下钻参数：``node.func`` 是函数名本身，不是取值表达式。
        for argument in node.args:
            _validate(argument, depth=depth + 1, counter=counter, refs=refs, dice=dice)
        return
    for child in ast.iter_child_nodes(node):
        _validate(child, depth=depth + 1, counter=counter, refs=refs, dice=dice)


def _compile_formula(formula: Any) -> _Compiled:
    if not isinstance(formula, str):
        raise _error("formula.type", "公式必须是字符串。")
    text = formula.strip()
    if not text:
        raise _error("formula.empty", "公式是空的。")
    if len(formula) > MAX_LENGTH:
        raise _error(
            "formula.too_long",
            f"公式太长（{len(formula)} 字符，上限 {MAX_LENGTH}）。",
        )
    for bad in _FORBIDDEN_CHARS:
        if bad in text:
            raise _error("formula.syntax", "公式里不能有换行或控制字符。")
    if "#" in text:
        raise _error("formula.syntax", "公式里不能有注释（#）。")
    rewritten, refs, dice, spans = _scan(text)
    if sum(count for count, _ in dice.values()) > MAX_TOTAL_DICE:
        raise _error(
            "formula.dice_total",
            f"一条公式里最多掷 {MAX_TOTAL_DICE} 颗骰子，请拆成多次投掷。",
        )
    try:
        expression = ast.parse(rewritten, mode="eval")
    except SyntaxError as exc:  # 语法错误：给位置但不要说「Python」太多
        offset = (exc.offset or 1) - 1
        snippet = text[max(0, offset - 8) : offset + 8]
        raise _error(
            "formula.syntax",
            f"公式语法无法解析（位置约在第 {offset + 1} 个字符「{snippet}」），请检查括号与运算符。",
        ) from None
    except (ValueError, MemoryError) as exc:  # pragma: no cover - 极端输入
        raise _error("formula.syntax", f"公式无法解析：{exc}") from None
    counter = [0]
    _validate(expression, depth=0, counter=counter, refs=refs, dice=dice)
    return _Compiled(expression, refs, dice, spans, text)


# ---------------------------------------------------------------- 引用解析


def _lookup_key(node: Mapping[str, Any], part: str) -> tuple[bool, Any]:
    """大小写不敏感兜底的单层取值。"""
    if part in node:
        return True, node[part]
    folded = str(part).casefold()
    matches = [value for key, value in node.items() if str(key).casefold() == folded]
    if len(matches) == 1:
        return True, matches[0]
    return False, None


def _derive_modifier(node: Any) -> int | None:
    """按 ``(value-10)//2`` 补出缺失的 ``mod``（5e 属性调整值口径）。"""
    if _is_number(node):
        return (int(node) - 10) // 2
    if isinstance(node, Mapping):
        for key in ("value", "score", "total"):
            candidate = node.get(key)
            if _is_number(candidate):
                return (int(candidate) - 10) // 2
    return None


def _resolve_ref(path: str, context: Mapping[str, Any]) -> Any:
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        raise _error("formula.unknown_ref", f"引用「@{path}」没有路径。")
    node: Any = context
    for offset, part in enumerate(parts):
        # ``.mod`` 缺字段时按 (value-10)//2 自动补：abilities.dex 可能是 14（分值）
        # 也可能是 {value: 14}；两种情况都能取到调整值。
        derived = _derive_modifier(node) if part.casefold() == "mod" else None
        if isinstance(node, Mapping):
            found, value = _lookup_key(node, part)
            if found:
                node = value
                continue
            if derived is not None:
                node = derived
                continue
            available = "、".join(sorted(str(key) for key in list(node)[:8])) or "（空）"
            prefix = ".".join(parts[:offset])
            where = f"「{prefix}」" if prefix else "上下文顶层"
            raise _error(
                "formula.unknown_ref",
                f"引用「@{path}」不存在：{where}没有「{part}」；可用字段：{available}。",
            )
        if derived is not None:
            node = derived
            continue
        raise _error(
            "formula.unknown_ref",
            f"引用「@{path}」在「{'.'.join(parts[:offset]) or '<上下文>'}」处不是对象，取不到「{part}」。",
        )
    if isinstance(node, (list, tuple, set)):
        raise _error(
            "formula.unknown_ref",
            f"引用「@{path}」指向的是列表，公式不支持下标取值。",
        )
    if isinstance(node, (Mapping,)):
        raise _error(
            "formula.unknown_ref",
            f"引用「@{path}」指向的是对象，请取到具体字段（例如 @{path}.value 或 @{path}.mod）。",
        )
    if node is None or isinstance(node, (bytes, bytearray)):
        raise _error("formula.unknown_ref", f"引用「@{path}」的值不可用于公式。")
    return node


# ---------------------------------------------------------------- 求值

_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: left // right,
    ast.Mod: lambda left, right: left % right,
}

_COMPARE_OPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda left, right: left == right,
    ast.NotEq: lambda left, right: left != right,
    ast.Lt: lambda left, right: left < right,
    ast.LtE: lambda left, right: left <= right,
    ast.Gt: lambda left, right: left > right,
    ast.GtE: lambda left, right: left >= right,
}

_DIVIDING_OPS = (ast.Div, ast.FloorDiv, ast.Mod)


class _Evaluator:
    """白名单 AST 求值；骰子走注入的 rng，引用走 roll data。"""

    __slots__ = ("context", "rng", "refs", "dice", "rolls", "values", "total_dice")

    def __init__(
        self,
        *,
        context: Mapping[str, Any],
        rng: Rng | None,
        refs: dict[str, tuple[str, int, int]],
        dice: dict[str, tuple[int, int]],
    ) -> None:
        self.context = context
        self.rng = rng or _default_rng
        self.refs = refs
        self.dice = dice
        self.rolls: list[dict[str, Any]] = []
        self.values: dict[str, Any] = {}
        self.total_dice = 0

    # -- 骰子 ---------------------------------------------------------
    def _roll(self, count: int, sides: int) -> int:
        self.total_dice += count
        if self.total_dice > MAX_TOTAL_DICE:
            raise _error(
                "formula.dice_total",
                f"一条公式里最多掷 {MAX_TOTAL_DICE} 颗骰子。",
            )
        faces: list[int] = []
        for _ in range(count):
            try:
                face = self.rng(sides)
            except Exception as exc:  # noqa: BLE001 - 随机源故障按公式错误上报
                raise _error("formula.rng", f"投骰失败：{exc}") from None
            face = _as_int(face, what="骰子点数")
            if face < 1 or face > sides:
                raise _error(
                    "formula.rng",
                    f"随机源返回了越界的点数 {face}（d{sides} 应在 1 到 {sides} 之间）。",
                )
            faces.append(face)
        total = sum(faces)
        self.rolls.append(
            {
                "formula": f"{count}d{sides}",
                "count": count,
                "sides": sides,
                "values": faces,
                "total": total,
            }
        )
        return total

    # -- 节点 ---------------------------------------------------------
    def eval(self, node: ast.AST) -> Any:  # noqa: A003 - 与 ast 对称的命名
        kind = type(node)
        if kind is ast.Expression:
            return self.eval(node.body)  # type: ignore[attr-defined]
        if kind is ast.Constant:
            return node.value  # type: ignore[attr-defined]
        if kind is ast.Name:
            return self._name(node.id)  # type: ignore[attr-defined]
        if kind is ast.BinOp:
            return self._bin_op(node)  # type: ignore[attr-defined]
        if kind is ast.UnaryOp:
            return self._unary_op(node)  # type: ignore[attr-defined]
        if kind is ast.BoolOp:
            return self._bool_op(node)  # type: ignore[attr-defined]
        if kind is ast.Compare:
            return self._compare(node)  # type: ignore[attr-defined]
        if kind is ast.Call:
            return self._call(node)  # type: ignore[attr-defined]
        # _validate 已经拦过；这里是防御性兜底。
        raise _error("formula.unsupported", "公式里有不支持的语法。")

    def _name(self, name: str) -> Any:
        if name in self.values:
            return self.values[name]
        if name in self.refs:
            value = _resolve_ref(self.refs[name][0], self.context)
            self.values[name] = value
            return value
        if name in self.dice:
            count, sides = self.dice[name]
            value = self._roll(count, sides)
            self.values[name] = value
            return value
        raise _error("formula.unknown_name", f"公式里有不认识的标识符「{_ALIASED.get(name, name)}」。")

    def _bin_op(self, node: ast.BinOp) -> Any:
        op_kind = type(node.op)
        handler = _BIN_OPS.get(op_kind)
        if handler is None:  # pragma: no cover - _validate 已拦
            raise _error("formula.unsupported", "公式里有不支持的运算符。")
        left = self.eval(node.left)
        right = self.eval(node.right)
        if op_kind in _DIVIDING_OPS:
            if not _is_number(right) or not _is_number(left):
                raise _error("formula.type", "除法两边都必须是数字。")
            if right == 0:
                raise _error("formula.div_zero", "公式里出现了除以 0。")
        try:
            result = handler(left, right)
        except ZeroDivisionError:
            raise _error("formula.div_zero", "公式里出现了除以 0。") from None
        except TypeError:
            raise _error(
                "formula.type",
                "公式里类型不匹配（数字和字符串不能直接相加/相乘）。",
            ) from None
        except (OverflowError, ValueError):
            raise _error("formula.value", "公式计算溢出，请检查数字量级。") from None
        if isinstance(result, float) and not math.isfinite(result):
            raise _error("formula.value", "公式算出了无穷大，请检查除数与数字量级。")
        return result

    def _unary_op(self, node: ast.UnaryOp) -> Any:
        value = self.eval(node.operand)
        op_kind = type(node.op)
        if op_kind is ast.USub:
            return -_as_number(value, what="负号")
        if op_kind is ast.UAdd:
            return +_as_number(value, what="正号")
        if op_kind is ast.Not:
            return not value
        raise _error("formula.unsupported", "公式里有不支持的一元运算符。")  # pragma: no cover

    def _bool_op(self, node: ast.BoolOp) -> Any:
        is_and = isinstance(node.op, ast.And)
        for index, item in enumerate(node.values):
            value = self.eval(item)
            last = index == len(node.values) - 1
            if is_and:
                if not value:
                    return value
            else:
                if value:
                    return value
            if last:
                return value
        raise _error("formula.syntax", "逻辑运算缺少操作数。")  # pragma: no cover

    def _compare(self, node: ast.Compare) -> bool:
        left = self.eval(node.left)
        for op_kind, comparator in zip(node.ops, node.comparators):
            handler = _COMPARE_OPS.get(type(op_kind))
            if handler is None:  # pragma: no cover - _validate 已拦
                raise _error("formula.unsupported", "公式里有不支持的比较运算符。")
            right = self.eval(comparator)
            try:
                if not handler(left, right):
                    return False
            except TypeError:
                raise _error("formula.type", "比较的两边类型不可比较。") from None
            left = right
        return True

    def _call(self, node: ast.Call) -> Any:
        assert isinstance(node.func, ast.Name)  # _validate 已保证
        key = node.func.id
        entry = _FUNCTIONS.get(key)
        if entry is None:  # pragma: no cover - _validate 已拦
            raise _error("formula.unknown_function", "公式里有不支持的函数。")
        _, _, handler = entry
        args = [self.eval(argument) for argument in node.args]
        try:
            return handler(args)
        except FormulaError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise _error("formula.type", f"{_ALIASED.get(key, key)}() 的参数类型不对。") from None


def _default_rng(sides: int) -> int:
    return random.randint(1, sides)


def _substitute(source: str, spans: list[tuple[int, int, str]], values: dict[str, Any]) -> str:
    if not spans:
        return source
    pieces: list[str] = []
    cursor = 0
    for start, end, key in spans:
        pieces.append(source[cursor:start])
        value = values.get(key)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        pieces.append(str(value))
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces)


def _normalize(value: Any) -> Any:
    """整数值的 float 收敛成 int，其余原样返回。"""
    if isinstance(value, float) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise _error("formula.value", "公式算出了无穷大。")
        if value.is_integer():
            return int(value)
    return value


def evaluate(formula: Any, context: Mapping[str, Any] | None = None, *, rng: Rng | None = None) -> dict[str, Any]:
    """求值一条公式，返回结果字典；任何失败都以 ``ok: False`` 返回，不抛异常。

    成功：``{"ok": True, "value": int|float|str|bool, "rolls": [...],
    "formula": 原文, "resolved": 代入后的可读文本}``。

    失败：``{"ok": False, "code": "formula.xxx", "error": "中文可读"}``。
    """
    roll_data = context if isinstance(context, Mapping) else {}
    try:
        compiled = _compile_formula(formula)
        evaluator = _Evaluator(
            context=roll_data,
            rng=rng,
            refs=compiled.refs,
            dice=compiled.dice,
        )
        value = _normalize(evaluator.eval(compiled.expression))
        return {
            "ok": True,
            "value": value,
            "rolls": evaluator.rolls,
            "formula": compiled.source,
            "resolved": _substitute(compiled.source, compiled.spans, evaluator.values),
        }
    except FormulaError as exc:
        return {"ok": False, "code": exc.code, "error": exc.message}
    except RecursionError:  # pragma: no cover - 深度护栏之外的最后一道
        return {"ok": False, "code": "formula.too_deep", "error": "公式嵌套太深，请化简括号。"}
    except MemoryError:  # pragma: no cover
        return {"ok": False, "code": "formula.too_complex", "error": "公式太大，请拆成多条。"}
    except Exception:  # noqa: BLE001 - 求值器承诺不把异常抛给调用方（KP 回合不 500）
        return {
            "ok": False,
            "code": "formula.error",
            "error": "公式求值遇到未预期的错误，请把公式改写得更简单后重试。",
        }


def evaluate_number(
    value: Any,
    context: Mapping[str, Any] | None = None,
    *,
    rng: Rng | None = None,
) -> int | float:
    """求值并要求结果是数字；失败或非数字都抛 :class:`FormulaError`。

    非字符串（``int`` / ``float``）原样返回，方便效果引擎把一个字段无论
    是公式还是纯数字都走同一条路径。
    """
    if isinstance(value, bool):
        raise _error("formula.not_number", "公式结果是布尔值，这里需要数字。")
    if _is_number(value):
        return value
    result = evaluate(value, context, rng=rng)
    if not result.get("ok"):
        raise FormulaError(str(result.get("code") or "formula.error"), str(result.get("error") or "公式求值失败。"))
    number = result["value"]
    if isinstance(number, bool) or not _is_number(number):
        raise _error("formula.not_number", f"公式结果是 {type(number).__name__}，这里需要数字。")
    return number


# ---------------------------------------------------------------- 判别与 roll data


def is_formula(value: Any) -> bool:
    """判断一个字段值是不是「需要求值的公式」，而不是纯数字。

    纯数字字面量（含符号与小数）返回 ``False``，调用方可以直接当数值用；
    含骰式、``@`` 引用、白名单函数调用，或本身就是四则运算式的字符串返回
    ``True``。其它普通文本返回 ``False``（避免把描述文字当公式去算）。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if _PURE_NUMBER_RE.match(text):
        return False
    if "@" in text:
        return True
    if _LOOSE_DICE_RE.search(text):
        return True
    if _FUNCTION_CALL_RE.search(text):
        return True
    if _PLAIN_EXPR_RE.match(text) and any(op in text for op in "+*/%"):
        return True
    return False


_ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")


def _int_of(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _score_of(raw: Any) -> int:
    if isinstance(raw, Mapping):
        for key in ("value", "score", "total"):
            if key in raw:
                return _int_of(raw.get(key), 10)
        return 10
    return _int_of(raw, 10)


def roll_data_from_sheet(sheet: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 5e 卡面（``sheet`` 片段）摊成公式求值用的 roll data。

    产出 FVTT 风格的路径：``@level``、``@prof``、``@abilities.dex.mod``、
    ``@attributes.movement.walk``、``@attributes.hp.value`` 等。纯函数：不落盘、
    不依赖插件其它模块，效果引擎与 AI 工具可以共用。
    """
    data = sheet if isinstance(sheet, Mapping) else {}
    level = max(1, min(20, _int_of(data.get("level"), 1)))
    prof = 2 + (level - 1) // 4

    abilities_raw = data.get("abilities")
    if not isinstance(abilities_raw, Mapping):
        abilities_raw = data.get("attributes") if isinstance(data.get("attributes"), Mapping) else {}
    proficiencies = data.get("proficiencies") if isinstance(data.get("proficiencies"), Mapping) else {}
    saves = {
        str(item or "").strip().casefold()
        for item in (proficiencies.get("saves") or [])
        if str(item or "").strip()
    }

    abilities: dict[str, dict[str, Any]] = {}
    for key in _ABILITY_KEYS:
        raw = abilities_raw.get(key) if isinstance(abilities_raw, Mapping) else None
        if raw is None and isinstance(abilities_raw, Mapping):
            for candidate, value in abilities_raw.items():
                if str(candidate).strip().casefold() == key:
                    raw = value
                    break
        score = _score_of(raw)
        modifier = (score - 10) // 2
        proficient = key in saves
        abilities[key] = {
            "value": score,
            "score": score,
            "mod": modifier,
            "proficient": proficient,
            "save": modifier + (prof if proficient else 0),
        }

    hp_raw = data.get("hp") if isinstance(data.get("hp"), Mapping) else {}
    hp_current = _int_of(hp_raw.get("current"), _int_of(data.get("currentHp"), 0))
    hp_max = _int_of(hp_raw.get("max"), _int_of(data.get("maxHp"), 0))
    speed = max(0, _int_of(data.get("speed"), _int_of(data.get("base_speed"), 30)))
    speeds_raw = data.get("base_speeds") if isinstance(data.get("base_speeds"), Mapping) else {}
    movement = {"walk": speed}
    for key in ("fly", "swim", "climb", "burrow", "jump"):
        if key in speeds_raw:
            movement[key] = max(0, _int_of(speeds_raw.get(key), 0))
    armor_class = _int_of(data.get("armorClass"), _int_of(data.get("armor_class"), 10))

    attributes = {
        "prof": prof,
        "profBonus": prof,
        "ac": {"value": armor_class},
        "hp": {"value": hp_current, "max": hp_max},
        "movement": movement,
        "speed": {"value": speed},
    }
    return {
        "level": level,
        "prof": prof,
        "abilities": abilities,
        "attributes": attributes,
        "bonuses": {},
    }
