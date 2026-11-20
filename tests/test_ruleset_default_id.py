"""``[ruleset] default_id`` 配置链验收（审计 §0 结论 3 / §4.4 第 1 条）。

覆盖「默认规则集可配置」的四条要求：

1. 默认配置（模板 ``default_id = "coc7"``）下统一入口取到 coc7——与收敛前
   逐位一致（其余既有套件原样通过即为全链路锚点）；
2. 配置成 dnd5e 后，房间创建 / 剧本归一化 / 角色卡导出 / AI 规则知识检索
   四个「缺省」入口全部取到 dnd5e；
3. 配置成不存在的 id 且注册表有其他插件时，回退第一个可用插件并记录
   WARNING（指认配置值与回退值，每个注册表实例只记一次）；
4. 显式置空配置 → 「未配置」语义（第一个可用插件；空注册表错误文案
   区分「未配置」与「已配置但不可用」——该分支的空注册表断言在
   ``test_ruleset_p0_coc7_absent.py`` 扩展）。

配置注入统一走配置链的 env 层（``AI_TRPG_CFG_RULESET_DEFAULT_ID``）或
用户配置文件层（``set_config_dir_override`` + 临时 general.toml），不 mock
业务函数；注册表状态经 ``reset_builtin_registry()`` 重建后恢复。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from trpg_server import character_card_io, scenario_service
from trpg_server.agents import ruleset_knowledge
from trpg_server.app_factory import create_app
from trpg_server.config import invalidate_settings_cache, set_config_dir_override
from trpg_server.ruleset import (
    RulesetSelectionError,
    builtin_registry,
    default_ruleset_id,
    reset_builtin_registry,
)
from trpg_server.ruleset.bootstrap import default_ruleset_hint
from trpg_server.ruleset.contracts import RULESET_API_VERSION
from trpg_server.ruleset.registry import RulesetRegistry

CONFIG_ENV_KEY = "AI_TRPG_CFG_RULESET_DEFAULT_ID"
HINT_ENV_KEY = "TRPG_RULESET_DEFAULT"


class FakeManager:
    role = "ADMIN"

    def is_session_current(self, user_id, token):
        return True

    def get_user_by_id(self, user_id):
        return {"id": user_id, "username": "tester", "role": self.role}


class _ScenarioActor:
    user_id = 11
    username = "owner"

    def has(self, *_args, **_kwargs) -> bool:
        return True


class _StubPlugin:
    """满足注册表契约的最小插件；只提供元数据，不实现任何能力。"""

    api_version = RULESET_API_VERSION
    ruleset_version = "1"
    schema_version = 1
    supported_locales = ("zh-CN",)
    capabilities = frozenset()
    description = ""

    def __init__(self, ruleset_id: str, display_name: str = "") -> None:
        self.id = ruleset_id
        self.display_name = display_name or ruleset_id


class _RecordingStore:
    """``ruleset_knowledge.search_ruleset`` 的最小鸭子类型替身。"""

    rooms_dir = None

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registry = RulesetRegistry()

    def search(self, ruleset_id, query, version=None, top_k=3):
        self.calls.append(ruleset_id)
        return []


@pytest.fixture(autouse=True)
def _clean_hint_env(monkeypatch):
    """隔离全局提示链：测试进程外不应残留两个默认规则集覆盖源。"""
    monkeypatch.delenv(HINT_ENV_KEY, raising=False)
    monkeypatch.delenv(CONFIG_ENV_KEY, raising=False)


@pytest.fixture
def restored_registry():
    """测试后强制重建注册表，避免 dnd5e 默认态泄漏进其他套件。"""
    yield
    reset_builtin_registry()


def _app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "USER_MANAGER": FakeManager(),
            "CONFIG_DIR": tmp_path / "config",
            "ROOMS_DIR": tmp_path / "rooms",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOM_ARCHIVES_DIR": tmp_path / "room-archives",
            "HISTORY_DIR": tmp_path / "history",
        }
    )


def _login(client) -> None:
    with client.session_transaction() as session:
        session.update(
            user_id=1,
            username="tester",
            role="ADMIN",
            session_token="token",
            csrf_token="csrf",
        )


# -- 配置链（hint） --------------------------------------------------------


def test_hint_prefers_env_over_config(monkeypatch):
    """``TRPG_RULESET_DEFAULT`` 是运维临时覆盖，优先级高于配置文件。"""
    monkeypatch.setenv(HINT_ENV_KEY, " DnD5E ")
    assert default_ruleset_hint() == "dnd5e"


def test_hint_defaults_to_configured_coc7():
    """默认配置（模板 L2 = 代码默认 L1）下提示值就是 coc7。"""
    assert default_ruleset_hint() == "coc7"


def test_hint_reads_user_config_layer(tmp_path, monkeypatch):
    """用户 general.toml 的 ``[ruleset] default_id`` 覆盖模板默认。"""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "general.toml").write_text(
        '[ruleset]\ndefault_id = "dnd5e"\n', encoding="utf-8"
    )
    set_config_dir_override(config_dir)
    invalidate_settings_cache()
    try:
        assert default_ruleset_hint() == "dnd5e"
    finally:
        set_config_dir_override(None)
        invalidate_settings_cache()


# -- 注册表回退链（单元级，直接构造） --------------------------------------


def test_registry_default_prefers_configured_available():
    registry = RulesetRegistry([_StubPlugin("alpha"), _StubPlugin("beta")], default_id="beta")
    assert registry.default_hint() == "beta"
    assert registry.default_id() == "beta"


def test_registry_default_without_config_uses_first_plugin(caplog):
    registry = RulesetRegistry([_StubPlugin("alpha"), _StubPlugin("beta")])
    assert registry.default_hint() == ""
    with caplog.at_level(logging.WARNING, logger="trpg_server.ruleset.registry"):
        assert registry.default_id() == "alpha"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_registry_unavailable_default_falls_back_with_single_warning(caplog):
    """配置默认不可用 → 回退第一个可用插件，WARNING 只记一次并指认双方。"""
    registry = RulesetRegistry([_StubPlugin("alpha"), _StubPlugin("beta")], default_id="ghost")
    with caplog.at_level(logging.WARNING, logger="trpg_server.ruleset.registry"):
        assert registry.default_id() == "alpha"
        assert registry.default_id() == "alpha"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ghost" in message
    assert "alpha" in message


def test_registry_empty_with_configured_default_names_it():
    registry = RulesetRegistry([], default_id="ghost")
    assert registry.default_id() is None
    with pytest.raises(RulesetSelectionError) as excinfo:
        registry.resolve(None)
    message = str(excinfo.value)
    assert "已配置默认" in message
    assert "ghost" in message
    assert "没有可用的规则集" in message


def test_registry_empty_unconfigured_reports_unconfigured():
    registry = RulesetRegistry([])
    assert registry.default_hint() == ""
    with pytest.raises(RulesetSelectionError) as excinfo:
        registry.resolve(None)
    message = str(excinfo.value)
    assert "未配置" in message
    assert "没有可用的规则集" in message
    assert "已配置默认" not in message


# -- 进程级注册表（真插件装配） --------------------------------------------


def test_default_config_yields_coc7(tmp_path, restored_registry):
    """默认配置下统一入口与房间创建缺省值都保持 coc7（收敛前现状）。"""
    assert default_ruleset_hint() == "coc7"
    assert default_ruleset_id() == "coc7"
    assert builtin_registry().default_id() == "coc7"

    client = _app(tmp_path).test_client()
    _login(client)
    response = client.post(
        "/api/rooms",
        json={"name": "默认规则自由房", "room_mode": "freeform"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "coc7"
    stored = json.loads(
        (tmp_path / "rooms" / data["id"] / "info.json").read_text(encoding="utf-8")
    )
    assert stored["ruleset_id"] == "coc7"


def test_configured_dnd5e_default_reaches_all_entry_points(tmp_path, monkeypatch, restored_registry):
    """配置默认 dnd5e 后：房间 / 剧本 / 角色卡 / AI 规则知识检索全部取到 dnd5e。

    dnd5e 走真实第三方插件（data/plugins 注册），配置经配置链 env 层注入。
    """
    monkeypatch.setenv(CONFIG_ENV_KEY, "dnd5e")
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)

    # 统一入口
    assert default_ruleset_hint() == "dnd5e"
    assert default_ruleset_id() == "dnd5e"

    # 房间创建：未显式指定规则集的自由房取默认。
    response = client.post(
        "/api/rooms",
        json={"name": "默认 5e 自由房", "room_mode": "freeform"},
        headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["ruleset_id"] == "dnd5e"
    stored = json.loads(
        (tmp_path / "rooms" / data["id"] / "info.json").read_text(encoding="utf-8")
    )
    assert stored["ruleset_id"] == "dnd5e"

    # 剧本归一化 + 角色卡导出都在 app 上下文内读取目录配置。
    with app.app_context():
        created = scenario_service.apply_create(
            _ScenarioActor(),
            {"title": "默认 5e 剧本", "author": "作者", "scenario_version": "1.0.0"},
        )
        assert created["ruleset_id"] == "dnd5e"

        # 角色卡导出：缺省 rulesetId 的卡补默认值。
        payload = character_card_io.build_export_payload({})
        assert payload["rulesetId"] == "dnd5e"

    # AI 运行时：规则知识检索未指定规则集时解析到默认。
    recording = _RecordingStore()
    ruleset_knowledge.search_ruleset("room-1", "死亡豁免", store=recording, ruleset_id="")
    assert recording.calls == ["dnd5e"]


def test_unavailable_configured_default_falls_back_to_first_available(monkeypatch, restored_registry, caplog):
    """配置成不存在的 id：回退第一个可用插件（coc7）并记录 WARNING。"""
    monkeypatch.setenv(CONFIG_ENV_KEY, "not-a-ruleset")
    reset_builtin_registry()
    try:
        with caplog.at_level(logging.WARNING, logger="trpg_server.ruleset.registry"):
            assert default_ruleset_id() == "coc7"
        fallback_warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "not-a-ruleset" in record.getMessage()
            and "coc7" in record.getMessage()
        ]
        assert fallback_warnings, "回退必须记录指认配置值与回退值的 WARNING"

        # 每个注册表实例只告警一次，热路径重复解析不再刷屏。
        with caplog.at_level(logging.WARNING, logger="trpg_server.ruleset.registry"):
            assert default_ruleset_id() == "coc7"
        assert (
            len(
                [
                    record
                    for record in caplog.records
                    if record.levelno == logging.WARNING
                    and "not-a-ruleset" in record.getMessage()
                ]
            )
            == 1
        )
    finally:
        reset_builtin_registry()
