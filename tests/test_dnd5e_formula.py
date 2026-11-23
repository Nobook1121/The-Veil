"""dnd5e 安全公式求值器（``dnd5e_plugin/formula.py``）与 AI 工具接线。

覆盖三块：

- 语法子集：算术/比较/逻辑、骰式（rng 注入固定值）、``@`` 引用与自动 ``mod``、
  白名单函数与 clamp/if；
- 安全护栏：``__import__``/属性访问/下标/lambda/超长/超节点/超深/未知引用/除零
  一律返回可读中文错误且**不抛异常**，未知引用不静默成 0；
- 接线：插件清单声明 + registry 真激活 + ``dnd5e.formula_eval`` 处理函数
  （最小 context 与 character_id 两条路径）。

另外把 5 条 FVTT 真实风格公式当对账样本，防止以后改口径时悄悄漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
DND5E_DIR = PLUGIN_ROOT / "dnd5e"
if str(DND5E_DIR) not in sys.path:
    sys.path.insert(0, str(DND5E_DIR))

from dnd5e_plugin import formula  # noqa: E402  (需先准备插件路径)
from dnd5e_plugin import tools as dnd5e_tools  # noqa: E402
from trpg_server.agents.profiles import plugin_tool_names  # noqa: E402
from trpg_server.plugins import runtime as plugin_runtime  # noqa: E402
from trpg_server.plugins.contracts import STATE_LOADED  # noqa: E402
from trpg_server.plugins.registry import PluginRegistry  # noqa: E402
from trpg_server.ruleset import reset_builtin_registry  # noqa: E402

CTX: dict[str, Any] = {
    "level": 5,
    "prof": 3,
    "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
    "bonuses": {"speed": 10, "attack": 1},
}

CARD: dict[str, Any] = {
    "id": "char-formula-1",
    "name": "艾莉丝",
    "rulesetId": "dnd5e",
    "sheet": {
        "level": 5,
        "class": "战士",
        "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
        "proficiencies": {"saves": ["str"], "skills": ["athletics"]},
        "hp": {"current": 40, "max": 44},
        "armorClass": 16,
        "speed": 30,
    },
}


def _max_rng(sides: int) -> int:
    """固定投出最大点：骰式断言不依赖真随机。"""
    return sides


def _min_rng(sides: int) -> int:
    return 1


def _value(expression: str, context: Any = None, **kwargs: Any) -> Any:
    result = formula.evaluate(expression, CTX if context is None else context, **kwargs)
    assert result["ok"] is True, result
    return result["value"]


def _error(expression: str, context: Any = None) -> dict[str, Any]:
    result = formula.evaluate(expression, CTX if context is None else context)
    assert result["ok"] is False, result
    assert set(result) == {"ok", "code", "error"}
    assert result["code"].startswith("formula."), result
    # 错误必须是中文可读文案，而不是 Python 异常类名。
    assert any("\u4e00" <= char <= "\u9fff" for char in result["error"]), result
    return result


# ---------------------------------------------------------------- 算术/比较/逻辑


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("7 // 2", 3),
        ("7 % 4", 3),
        ("7 / 2", 3.5),
        ("6 / 2", 3),
        ("-(3 + 4)", -7),
        ("+5", 5),
        ("1 - -2", 3),
        ("0.1 + 0.2", 0.1 + 0.2),
    ],
)
def test_arithmetic(expression: str, expected: Any) -> None:
    assert _value(expression) == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 == 1", True),
        ("2 != 3", True),
        ("1 < 2", True),
        ("2 <= 2", True),
        ("3 > 4", False),
        ("4 >= 4", True),
        ("1 < 2 < 3", True),
        ("1 < 3 < 2", False),
        ("1 < 2 and 3 >= 3", True),
        ("1 > 2 or 2 > 1", True),
        ("not 0", True),
        ("not (1 > 2)", True),
        ("1 > 2 and 3 > 2", False),
        ('"剑" == "剑"', True),
        ("True and 1", 1),
        ("0 or 7", 7),
    ],
)
def test_comparison_and_logic(expression: str, expected: Any) -> None:
    assert _value(expression) == expected


def test_string_literals_are_values_not_code() -> None:
    assert _value('"abc" == "abc"') is True
    assert _value("if(1 > 2, 'a', 'b')") == "b"


# ---------------------------------------------------------------- 骰式


def test_dice_uses_injected_rng() -> None:
    result = formula.evaluate("2d6 + 3", CTX, rng=_max_rng)
    assert result["ok"] is True
    assert result["value"] == 15
    assert result["rolls"] == [
        {"formula": "2d6", "count": 2, "sides": 6, "values": [6, 6], "total": 12}
    ]
    assert result["resolved"] == "12 + 3"

    low = formula.evaluate("2d6 + 3", CTX, rng=_min_rng)
    assert low["value"] == 5
    assert low["rolls"][0]["values"] == [1, 1]


@pytest.mark.parametrize(
    ("expression", "count", "sides"),
    [
        ("1d6", 1, 6),
        ("d20", 1, 20),
        ("D20", 1, 20),
        ("2D6", 2, 6),
        ("100d6", 100, 6),
        ("1d1000", 1, 1000),
    ],
)
def test_dice_forms_are_case_insensitive(expression: str, count: int, sides: int) -> None:
    result = formula.evaluate(expression, CTX, rng=_max_rng)
    assert result["ok"] is True
    assert result["rolls"][0]["count"] == count
    assert result["rolls"][0]["sides"] == sides
    assert result["value"] == count * sides


def test_dice_range_with_real_rng() -> None:
    for _ in range(30):
        result = formula.evaluate("3d6", CTX)
        assert result["ok"] is True
        assert 3 <= result["value"] <= 18
        assert len(result["rolls"][0]["values"]) == 3


def test_dice_guards() -> None:
    assert _error("101d6")["code"] == "formula.dice_count"
    assert _error("1d2000")["code"] == "formula.dice_sides"
    assert _error("100d6 + 100d6 + 1d6")["code"] == "formula.dice_total"
    assert _error("2d")["code"] == "formula.dice_count"
    # 随机源越界不许悄悄当成合法点数。
    bad = formula.evaluate("1d6", CTX, rng=lambda sides: sides + 1)
    assert bad["ok"] is False and bad["code"] == "formula.rng"
    broken = formula.evaluate("1d6", CTX, rng=lambda sides: 1 / 0)
    assert broken["ok"] is False and broken["code"] == "formula.rng"


# ---------------------------------------------------------------- @ 引用


def test_references_and_auto_modifier() -> None:
    assert _value("@level") == 5
    assert _value("(2 * @level) + @prof") == 13
    # 上下文只有属性分值：mod 按 (值-10)//2 自动补。
    assert _value("@abilities.str.mod") == 3
    assert _value("@abilities.dex.mod") == 2
    assert _value("@abilities.cha.mod") == -1
    # 大小写不敏感兜底。
    assert _value("@LEVEL") == 5
    # 上下文是 {value: N} 形态时同样补 mod。
    assert formula.evaluate("@abilities.dex.mod", {"abilities": {"dex": {"value": 15}}})["value"] == 2
    assert formula.evaluate("@abilities.dex.mod", {"abilities": {"dex": {"score": 8}}})["value"] == -1


def test_reference_resolution_in_resolved_text() -> None:
    result = formula.evaluate("1d6 + @abilities.str.mod", CTX, rng=_max_rng)
    assert result["ok"] is True
    assert result["value"] == 9
    assert result["resolved"] == "6 + 3"
    assert result["formula"] == "1d6 + @abilities.str.mod"


def test_unknown_reference_is_loud_not_zero() -> None:
    missing = _error("@abilities.foo.mod")
    assert missing["code"] == "formula.unknown_ref"
    assert "abilities" in missing["error"]
    assert _error("@nope.deep.missing")["code"] == "formula.unknown_ref"
    # 上下文直接给分值（FVTT 卡上 attributes.abilities.str 就是数字）：取到的就是分值本身。
    assert _value("@abilities.str") == 16
    # 只给到对象层：要求继续指明字段，而不是猜一个数字。
    sheet_ctx = formula.roll_data_from_sheet(CARD["sheet"])
    assert _error("@abilities.str", sheet_ctx)["code"] == "formula.unknown_ref"
    # 列表引用不支持下标取值。
    assert _error("@b", {"b": [1, 2]})["code"] == "formula.unknown_ref"


# ---------------------------------------------------------------- 白名单函数


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("max(30, 30 + 10)", 40),
        ("max(1, 9, 4)", 9),
        ("min(4, 2, 8)", 2),
        ("floor(2.7)", 2),
        ("floor(-2.5)", -3),
        ("ceil(2.1)", 3),
        ("ceil(-2.5)", -2),
        ("round(2.5)", 3),
        ("round(-2.5)", -3),
        ("round(2.4)", 2),
        ("abs(-3)", 3),
        ("abs(3)", 3),
        ("sign(-9)", -1),
        ("sign(0)", 0),
        ("sign(4)", 1),
        ("clamp(5, 1, 3)", 3),
        ("clamp(-1, 1, 3)", 1),
        ("clamp(2, 1, 3)", 2),
        ("clamp(2, 3, 1)", 2),
        ("if(1 > 2, 10, 20)", 20),
        ("if(@abilities.str.mod > 2, 'high', 'low')", "high"),
        ("floor(1d6 / 2)", 3),
    ],
)
def test_whitelisted_functions(expression: str, expected: Any) -> None:
    assert formula.evaluate(expression, CTX, rng=_max_rng)["value"] == expected


def test_function_guard_rails() -> None:
    assert _error("clamp(1, 2)")["code"] == "formula.bad_arity"
    assert _error("_secret()")["code"] == "formula.unknown_function"


# ---------------------------------------------------------------- 安全护栏


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ('__import__("os")', "formula.unknown_function"),
        ("getattr(1, 'x')", "formula.unknown_function"),
        ("open('/etc/passwd')", "formula.unknown_function"),
        ("a.b", "formula.unsupported"),
        ("1 .real", "formula.unsupported"),
        ("[1, 2]", "formula.unsupported"),
        ("{1: 2}", "formula.unsupported"),
        ("(1, 2)", "formula.unsupported"),
        ("lambda x: x", "formula.unsupported"),
        ("[x for x in 1]", "formula.unsupported"),
        ("2 ** 3", "formula.unsupported"),
        # 三元表达式里的 if 会被当成函数名，因此直接是语法错误（同样可读）。
        ("1 if 1 else 2", "formula.syntax"),
        ("f'{1}'", "formula.unsupported"),
        ("x[0]", "formula.unsupported"),
        ("@nope", "formula.unknown_ref"),
        ("1 / 0", "formula.div_zero"),
        ("1 // 0", "formula.div_zero"),
        ("1 % 0", "formula.div_zero"),
        ("", "formula.empty"),
        ("   ", "formula.empty"),
        ("1 +", "formula.syntax"),
        ('"未闭合', "formula.syntax"),
    ],
)
def test_rejected_syntax_returns_readable_error(expression: str, code: str) -> None:
    assert _error(expression)["code"] == code


def test_non_string_formula_is_rejected() -> None:
    for bad in (None, 12, ["1d6"], {"a": 1}):
        result = formula.evaluate(bad, CTX)
        assert result["ok"] is False
        assert result["code"] == "formula.type"


def test_length_node_and_depth_limits() -> None:
    assert _error("1" * 300)["code"] == "formula.too_long"
    # 长比较链：节点超限但深度很浅，专门打节点上限（101 个常量 + 100 个比较符）。
    chain = "<".join(["1"] * 101)
    assert len(chain) <= formula.MAX_LENGTH
    assert _error(chain)["code"] == "formula.too_complex"
    # 深嵌套调用：深度超限。
    nested = "max(1," * 34 + "1" + ")" * 34
    assert len(nested) <= formula.MAX_LENGTH
    assert _error(nested)["code"] == "formula.too_deep"


def test_no_dunder_or_file_network_touching_names_resolve() -> None:
    for expression in (
        "__builtins__",
        "__class__",
        "__import__('os').system('echo hi')",
        "os.environ",
        "open",
    ):
        result = formula.evaluate(expression, CTX)
        assert result["ok"] is False
        assert result["code"].startswith("formula.")


def test_evaluator_never_raises() -> None:
    nasty = [
        "@" * 20,
        "((((((((((((((((((((1))))))))))))))))))))",
        "1d6 + " * 40,
        "\u0000",
        "1\n+2",
        "max(",
        "1 ! 2",
    ]
    for expression in nasty:
        result = formula.evaluate(expression, CTX)
        assert isinstance(result, dict) and "ok" in result


# ---------------------------------------------------------------- FVTT 风格对账


@pytest.mark.parametrize(
    ("expression", "expected", "resolved"),
    [
        # 近战武器伤害：1d6 + 力量调整值。
        ("1d6 + @abilities.str.mod", 9, "6 + 3"),
        # 速度公式：地板 30。
        ("max(30, 30 + 10)", 40, "max(30, 30 + 10)"),
        # 灵巧武器用敏捷调整值。
        ("1d8 + @abilities.dex.mod", 10, "8 + 2"),
        # 等级相关公式（熟练加值 = 2 + (等级-1)//4）。
        ("(2 * @level) + @prof", 13, "(2 * 5) + 3"),
        # 效果引擎写法的加值公式。
        ("clamp(30 + @bonuses.speed, 0, 60)", 40, "clamp(30 + 10, 0, 60)"),
        # 攻击加值：属性调整值 + 熟练。
        ("@abilities.str.mod + @prof", 6, "3 + 3"),
    ],
)
def test_fvtt_style_formulas(expression: str, expected: Any, resolved: str) -> None:
    result = formula.evaluate(expression, CTX, rng=_max_rng)
    assert result["ok"] is True, result
    assert result["value"] == expected
    assert result["resolved"] == resolved
    assert result["formula"] == expression


def test_rolls_are_reported_for_every_dice_term() -> None:
    result = formula.evaluate("1d6 + 2d8 + 3", CTX, rng=_min_rng)
    assert result["ok"] is True
    assert [item["formula"] for item in result["rolls"]] == ["1d6", "2d8"]
    assert result["value"] == 1 + 2 + 3


# ---------------------------------------------------------------- is_formula / evaluate_number


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30", False),
        ("-5", False),
        ("1.5", False),
        (12, False),
        (1.5, False),
        (None, False),
        ("", False),
        ("  ", False),
        ("火球术", False),
        ("1d6", True),
        ("2d6 + 3", True),
        ("d20", True),
        ("@abilities.str.mod", True),
        ("max(30, 30 + 10)", True),
        ("if(1 > 2, 1, 2)", True),
        ("30 + 10", True),
        ("30 * 2", True),
    ],
)
def test_is_formula(value: Any, expected: bool) -> None:
    assert formula.is_formula(value) is expected


def test_evaluate_number_happy_path() -> None:
    assert formula.evaluate_number("max(30, 30 + 10)", CTX) == 40
    assert formula.evaluate_number("1d6", CTX, rng=_max_rng) == 6
    # 纯数字原样返回：效果引擎同一个字段既能是公式也能是数字。
    assert formula.evaluate_number(30, CTX) == 30
    assert formula.evaluate_number(-2.5, CTX) == -2.5


def test_evaluate_number_rejects_non_numbers() -> None:
    with pytest.raises(formula.FormulaError) as non_number:
        formula.evaluate_number("1 > 2", CTX)
    assert non_number.value.code == "formula.not_number"

    with pytest.raises(formula.FormulaError) as missing:
        formula.evaluate_number("@nope", CTX)
    assert missing.value.code == "formula.unknown_ref"

    with pytest.raises(formula.FormulaError):
        formula.evaluate_number(True, CTX)


# ---------------------------------------------------------------- roll data


def test_roll_data_from_sheet() -> None:
    data = formula.roll_data_from_sheet(CARD["sheet"])
    assert data["level"] == 5
    assert data["prof"] == 3  # 2 + (5-1)//4
    assert data["abilities"]["str"]["value"] == 16
    assert data["abilities"]["str"]["mod"] == 3
    assert data["abilities"]["str"]["proficient"] is True
    assert data["abilities"]["str"]["save"] == 6
    assert data["abilities"]["dex"]["proficient"] is False
    assert data["abilities"]["dex"]["save"] == 2
    assert data["attributes"]["prof"] == 3
    assert data["attributes"]["hp"] == {"value": 40, "max": 44}
    assert data["attributes"]["ac"]["value"] == 16
    assert data["attributes"]["movement"]["walk"] == 30
    assert data["bonuses"] == {}

    assert formula.evaluate_number("1d6 + @abilities.str.save", data, rng=_max_rng) == 12
    assert formula.evaluate_number("@attributes.movement.walk", data) == 30
    assert formula.evaluate_number("max(30, @attributes.movement.walk)", data) == 30


def test_roll_data_from_sheet_tolerates_empty_input() -> None:
    data = formula.roll_data_from_sheet(None)
    assert data["level"] == 1
    assert data["prof"] == 2
    assert data["abilities"]["str"]["value"] == 10
    assert data["abilities"]["str"]["mod"] == 0
    assert formula.evaluate_number("@abilities.str.mod", data) == 0


# ---------------------------------------------------------------- AI 工具接线


@pytest.fixture()
def plugins():
    """装载真实插件根：dnd5e 的 agent 工具真激活。"""
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    plugin_runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _room_context(tmp_path: Path) -> SimpleNamespace:
    info = {
        "id": "room-formula",
        "ruleset_id": "dnd5e",
        "members": [
            {
                "user_id": "u1",
                "username": "alice",
                "room_role": "owner",
                "is_active": True,
                "character_card": CARD,
            }
        ],
    }
    return SimpleNamespace(room_dir=tmp_path, room_info=lambda: info)


def test_tool_declared_in_manifest_and_registered(plugins) -> None:
    record = plugins.get("dnd5e")
    assert record is not None
    assert record.state == STATE_LOADED
    declared = list(record.manifest.capabilities["agent_tools"]["tools"])
    assert "dnd5e.formula_eval" in declared

    tools = {str(tool.name): tool for tool in plugin_runtime.extra_agent_tools()}
    tool = tools["dnd5e.formula_eval"]
    # 权限口径沿用既有插件工具：只在 dnd5e 房间下发，KP / AI 玩家同一门闸。
    assert tool.rulesets == ("dnd5e",)
    assert getattr(tool.handler, "__name__", "") == "_evaluate_formula"
    assert tool.parameters["required"] == ["formula"]
    assert {"formula", "character_id", "context"} <= set(tool.parameters["properties"])
    # 工具描述说清「何时用」与「不能执行任意代码」。
    assert "1d6" in tool.description or "骰式" in tool.description
    assert "不能执行任意代码" in tool.description
    assert "公式" in tool.description

    assert "dnd5e.formula_eval" in plugin_tool_names("dnd5e")
    assert "dnd5e.formula_eval" not in plugin_tool_names("coc7")


def test_entry_factory_exposes_both_tools(plugins) -> None:
    """清单走 entry.create_tools()：新工具与既有遭遇工具一起下发，不抢名不覆盖。"""
    names = {str(tool.name) for tool in plugin_runtime.extra_agent_tools()}
    assert {"dnd5e.formula_eval", "dnd5e.start_encounter"} <= names
    module = sys.modules.get("trpg_plugin_dnd5e_entry")
    if module is not None:  # registry 装载过 entry.py 时直接对账工厂产出
        assert "dnd5e.formula_eval" in {tool.name for tool in module.create_tools()}


def test_tool_evaluates_with_explicit_context(tmp_path) -> None:
    handler = dnd5e_tools.FORMULA_EVAL_TOOL.handler
    result = handler(
        {
            "formula": "max(30, 30 + @bonuses.speed) + @abilities.str.mod",
            "context": {"bonuses": {"speed": 10}, "abilities": {"str": 16}},
        },
        _room_context(tmp_path),
    )
    assert result["ok"] is True
    assert result["value"] == 43
    assert result["rolls"] == []
    assert result["context_source"] == "context"


def test_tool_evaluates_from_character_sheet(tmp_path) -> None:
    handler = dnd5e_tools.FORMULA_EVAL_TOOL.handler
    result = handler(
        {"formula": "1d6 + @abilities.str.mod", "character_id": CARD["id"]},
        _room_context(tmp_path),
    )
    assert result["ok"] is True
    assert result["character_id"] == CARD["id"]
    assert result["context_source"] == "character"
    assert result["value"] >= 4  # 1d6 + 3
    assert result["rolls"][0]["sides"] == 6

    prof = handler({"formula": "@prof + @level", "character_id": CARD["id"]}, _room_context(tmp_path))
    assert prof["ok"] is True and prof["value"] == 8  # 熟练 3 + 等级 5


def test_tool_context_overrides_character_values(tmp_path) -> None:
    handler = dnd5e_tools.FORMULA_EVAL_TOOL.handler
    result = handler(
        {
            "formula": "@level + @bonuses.attack",
            "character_id": CARD["id"],
            "context": {"level": 9, "bonuses": {"attack": 2}},
        },
        _room_context(tmp_path),
    )
    assert result["ok"] is True
    assert result["value"] == 11
    assert result["context_source"] == "character+context"


def test_tool_ignores_context_keys_outside_whitelist(tmp_path) -> None:
    handler = dnd5e_tools.FORMULA_EVAL_TOOL.handler
    result = handler(
        {"formula": "@level", "context": {"level": 4, "secrets": {"token": "x"}}},
        _room_context(tmp_path),
    )
    assert result["ok"] is True
    assert result["value"] == 4
    # 白名单外的键不会进求值上下文：引用它必须报未知引用。
    blocked = handler({"formula": "@secrets.token", "context": {"secrets": {"token": "x"}}}, _room_context(tmp_path))
    assert blocked["ok"] is False
    assert blocked["code"] == "formula.unknown_ref"


def test_tool_reports_missing_inputs(tmp_path) -> None:
    handler = dnd5e_tools.FORMULA_EVAL_TOOL.handler
    context = _room_context(tmp_path)

    missing_formula = handler({}, context)
    assert missing_formula["ok"] is False
    assert missing_formula["code"] == "dnd5e.formula_required"

    bad_context = handler({"formula": "1 + 1", "context": "level=3"}, context)
    assert bad_context["ok"] is False
    assert bad_context["code"] == "dnd5e.context_invalid"

    unknown_card = handler({"formula": "1 + 1", "character_id": "nope"}, context)
    assert unknown_card["ok"] is False
    assert unknown_card["code"] == "dnd5e.character_not_found"

    unsafe = handler({"formula": '__import__("os").system("dir")'}, context)
    assert unsafe["ok"] is False
    assert unsafe["code"] in {"formula.unknown_function", "formula.unsupported"}
    assert "error" in unsafe
