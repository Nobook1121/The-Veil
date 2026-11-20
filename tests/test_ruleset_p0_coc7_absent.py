"""阶段 4 P0 验收：coc7 从注册表拿掉后进程可启动、普通掷骰可用。

场景构造（对现有测试基建侵入最小）：``TRPG_RULESET_PLUGINS`` 显式置空 →
:func:`trpg_server.ruleset.bootstrap.plugin_module_paths` 返回空清单 →
``build_registry()`` 构造空注册表。coc7 物理包仍在磁盘上——本测试验证的是
「注册来源不再靠 import coc7 存在」；import 期耦合的静态红线由
``tests/test_ruleset_boundary_guard.py`` 的机械守卫保证。

coc7 正常注册（默认场景）的行为等价性不由本文件负责：全部既有测试原样
通过即为锚点（ruleset / combat / agent_tools / turn_pipeline 套件）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trpg_server.agents.context import AgentRequestContext
from trpg_server.agents.tools import default_tool_registry
from trpg_server.agents.tools.chase import CHASE_TOOL_NAMES
from trpg_server.agents.tools.coc7 import COC7_EXTRA_TOOL_NAMES
from trpg_server.agents.tools.magic import MAGIC_CATALOG_TOOL_NAMES, MAGIC_TOOL_NAMES
from trpg_server.agents.tools.sanity import SANITY_TOOL_NAMES
from trpg_server.app_factory import create_app
from trpg_server.combat.registry import CombatRulesetUnavailable, get_ruleset
from trpg_server.plugins import runtime
from trpg_server.ruleset import (
    RulesetSelectionError,
    builtin_registry,
    default_ruleset_id,
    reset_builtin_registry,
)


class _FakeManager:
    """与 tests/test_app_info.py 同款：跳过真实用户服务初始化。"""

    role = "ADMIN"

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


# _coc7_tools() 会挂载的全部 CoC 命名工具（缺席断言用；工具名常量来自各
# 工具模块本身，后续批次改名时这里自动跟随）。
COC7_MOUNTED_TOOL_NAMES = (
    "dice.roll_coc_check",
    "check.roll_room_check",
    "san.roll_sanity_check",
    "sanity.roll_sanity_check",
    "san.roll_madness",
    "sanity.roll_madness",
    *COC7_EXTRA_TOOL_NAMES,
    *CHASE_TOOL_NAMES,
    *MAGIC_TOOL_NAMES,
    *MAGIC_CATALOG_TOOL_NAMES,
    *SANITY_TOOL_NAMES,
)


@pytest.fixture
def coc7_absent(monkeypatch):
    """coc7 缺席场景：插件模块清单显式置空 + 清掉第三方插件运行时残余。"""
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    yield builtin_registry()
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": _FakeManager(),
            # 第三方插件（data/plugins 的 dnd5e 等）默认启用；本测试只考察
            # 「内置 coc7 缺席」，显式关掉让注册表可预期为空。
            "PLUGINS_DISABLED": True,
            "CONFIG_DIR": tmp_path / "config",
            "LOGS_DIR": tmp_path / "logs",
            "KNOWLEDGE_BASES_DIR": tmp_path / "knowledge-bases",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "SCENARIO_IMPORTS_DIR": tmp_path / "scenario-imports",
            "NARRATIVE_JOBS_DIR": tmp_path / "narrative-jobs",
            "UPDATE_DIR": tmp_path / "update",
            "RUNTIME_DIR": tmp_path / "runtime",
        }
    )


def test_empty_registry_reports_clear_error(coc7_absent):
    """插件全空时注册表是空表；解析 API 给明确错误，不是 import 崩溃。"""
    registry = coc7_absent
    assert registry.ids() == []
    assert registry.default_id() is None
    assert registry.describe() == []

    with pytest.raises(RulesetSelectionError) as excinfo:
        registry.resolve(None)
    assert "没有可用的规则集" in str(excinfo.value)

    with pytest.raises(RuntimeError):
        default_ruleset_id()


def test_empty_registry_error_names_configured_default(coc7_absent):
    """默认配置链（[ruleset] default_id = "coc7"）不可用时文案指认配置值。

    「已配置默认 <id> 但不可用」是空注册表场景最常见的成因（默认值指向
    未注册的插件），错误必须把它说出来，而不是笼统的「没有规则集」。
    """
    registry = coc7_absent
    configured = registry.default_hint()
    assert configured, "默认配置链应给出非空 default_id（模板默认 coc7）"

    with pytest.raises(RulesetSelectionError) as excinfo:
        registry.resolve(None)
    message = str(excinfo.value)
    assert "没有可用的规则集" in message
    assert "已配置默认" in message
    assert configured in message

    with pytest.raises(RuntimeError) as runtime_exc:
        default_ruleset_id()
    runtime_message = str(runtime_exc.value)
    assert "已配置默认" in runtime_message
    assert configured in runtime_message
    assert "没有可用的规则集" in runtime_message


def test_empty_registry_error_distinguishes_unconfigured(coc7_absent, monkeypatch):
    """显式置空 default_id 后错误换成「未配置且无可用规则集」分支。"""
    # 配置 env 层（AI_TRPG_CFG_*）显式置空：走「未配置」文案分支。
    monkeypatch.setenv("AI_TRPG_CFG_RULESET_DEFAULT_ID", "")
    monkeypatch.delenv("TRPG_RULESET_DEFAULT", raising=False)
    reset_builtin_registry()
    registry = builtin_registry()
    assert registry.ids() == []
    assert registry.default_hint() == ""

    with pytest.raises(RulesetSelectionError) as excinfo:
        registry.resolve(None)
    message = str(excinfo.value)
    assert "没有可用的规则集" in message
    assert "未配置" in message
    assert "已配置默认" not in message

    with pytest.raises(RuntimeError) as runtime_exc:
        default_ruleset_id()
    runtime_message = str(runtime_exc.value)
    assert "未配置" in runtime_message
    assert "没有可用的规则集" in runtime_message


def test_combat_registry_reports_unavailable_without_coc7(coc7_absent):
    """战斗解析给「没有可用的战斗实现」语义，而不是 AttributeError。"""
    with pytest.raises(CombatRulesetUnavailable) as excinfo:
        get_ruleset("coc7")
    assert "没有可用的战斗实现" in str(excinfo.value)

    with pytest.raises(CombatRulesetUnavailable):
        get_ruleset()


def test_app_bootstraps_without_coc7(coc7_absent, tmp_path):
    """进程 import 全通（create_app 注册全部路由即证明）且 app 可创建。"""
    app = _app(tmp_path)
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username="tester", role="ADMIN", session_token="t", csrf_token="c")

    resp = client.get("/api/rulesets")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["data"] == []


def test_neutral_dice_path_works_without_coc7(coc7_absent, tmp_path):
    """普通掷骰可用：中性工具在场可执行，CoC 命名工具整组不挂载。"""
    registry = default_tool_registry()

    assert registry.get("dice.roll") is not None
    assert registry.get("dice.roll_dice") is not None
    for name in COC7_MOUNTED_TOOL_NAMES:
        assert registry.get(name) is None, f"coc7 缺席时不应挂载 {name}"

    context = AgentRequestContext(room_id="room-1", room_dir=tmp_path)
    result = registry.get("dice.roll").handler({"expression": "1d6"}, context)
    assert "error" not in result
    assert 1 <= int(result["total"]) <= 6
    assert result["expression"] == "1d6"
