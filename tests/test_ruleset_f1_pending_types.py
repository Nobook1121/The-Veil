"""F1：apply_room_changes 变更类型参数化。

资源类队列 ``type``（san / hp / mp）改按卡所属规则集的 ``resource_slots``
声明解析（与 D1 战斗侧槽位驱动同源）：

- coc7 声明三槽位 → 写回镜像键与旧固定枚举逐位一致，历史队列重放零丢写回；
- dnd5e 只声明 hp → san / mp 条目优雅跳过，不再把 CoC 镜像键写到 5e 卡上；
- 插件解析失败（空注册表 / 未知 id 的等价形态）→ ``resource_slots`` 回落
  核心默认槽位，旧行为逐位不变；
- 结构类 type（skill / attr / item / weapon / item_remove / weapon_remove /
  sheet / status / background / extensions / credit / assets / armor）保留
  通用分支：核心职责，语义上是中性结构操作，不算 CoC 债务。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server import character_service
from trpg_server.app_factory import create_app
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import reset_builtin_registry
from trpg_server.site_agent_auth import Actor, permission_set_for_role

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"


class _AdminActor:
    user_id = 7
    role = "ADMIN"

    def has(self, *_args, **_kwargs) -> bool:
        return True


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def plugins():
    """真插件装配（coc7 + dnd5e + 内容包），与 D1 / C1 测试同源。"""
    registry = _load_real_plugins()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


def _admin() -> Actor:
    return Actor(
        user_id=1,
        username="tester",
        role="ADMIN",
        permissions=permission_set_for_role("ADMIN"),
        room_ids=frozenset(),
    )


def _app(tmp_path: Path, **extra):
    config = {
        "TESTING": True,
        "CONFIG_DIR": tmp_path / "config",
        "ROOMS_DIR": tmp_path / "rooms",
        "CHARACTERS_DIR": tmp_path / "characters",
    }
    config.update(extra)
    return create_app(config)


def _stored_card_path(tmp_path: Path, card_id: str = "card-2") -> Path:
    """coc7 旧卡（test 卡形状）：技能 / 资源 / 状态与既有写回测试同构。"""
    card_dir = tmp_path / "characters" / card_id
    card_dir.mkdir(parents=True)
    stored = {
        "id": card_id,
        "name": "Bob",
        "playerId": "2",
        "rulesetId": "coc7",
        "attributes": {key: 50 for key in ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")},
        "deriveAttributes": {
            "sanity": {"start": 40, "current": 40, "max": 40},
            "hp": {"current": 10, "max": 10},
            "mp": {"current": 10, "max": 10},
        },
        "skillGroups": {
            "explore": [
                {"name": "侦查", "base": 60, "job": 0, "interest": 0, "growth": 0}
            ]
        },
        "equipment": [],
        "weapons": [],
        "background": {},
        "characterStatus": {"bodyStates": {}, "mentalStates": {}},
        "battleAttributes": {"armor": "0"},
    }
    path = card_dir / "character.json"
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return path


# 历史队列：逐条取自真实生产方的既有形态（record_id / source / reason 齐全），
# 覆盖旧固定枚举的每一个 type，外加「同 record_id 重复条目」的兼容队列形态。
_HISTORICAL_QUEUE = [
    # room_rules.apply_san_change：SAN 与理智归零状态共用 record_id（dedupe 按 (record_id, type)）
    {"type": "san", "value": 35, "source": "rule", "reason": "剧情理智损失", "record_id": "san-1699999999999-0"},
    {"type": "status", "value": {"indefiniteInsanity": True}, "source": "rule", "reason": "理智归零", "record_id": "san-1699999999999-0"},
    # agents/tools/combat：无 record_id 的战斗结算条目
    {"type": "hp", "value": 3, "source": "combat", "reason": "战斗结算"},
    {"type": "mp", "value": 2, "source": "combat", "reason": "战斗结算"},
    {"type": "armor", "value": 4, "source": "combat", "reason": "战斗护甲"},
    # ruleset/coc7/sanity：神话技能上升
    {"type": "skill", "name": "克苏鲁神话", "value": 12, "source": "ai_tool", "reason": "目击神话"},
    # room_rules.apply_attribute_change
    {"type": "attr", "key": "STR", "value": 65, "source": "ai_tool", "reason": "KP 调整", "record_id": "attr-1699999999999-0"},
    # room_rules.add_item + 兼容队列里同 record_id 重复列出（物品相对增量，重复应用会翻倍）
    {"type": "item", "name": "手电筒", "amount": 2, "fields": {"name": "手电筒", "quantity": 2, "weight": "", "volume": "", "notes": ""}, "note": "", "source": "rule", "reason": "获得", "record_id": "item-f1a"},
    {"type": "item", "name": "手电筒", "amount": 2, "fields": {"name": "手电筒", "quantity": 2, "weight": "", "volume": "", "notes": ""}, "note": "", "source": "rule", "reason": "获得", "record_id": "item-f1a"},
    {"type": "weapon", "name": "Knife", "amount": 1, "fields": {"name": "Knife", "damage": "1D4", "skill": "格斗（斗殴）", "note": ""}, "source": "rule", "reason": "获得"},
    # room_rules.remove_item
    {"type": "item_remove", "name": "手电筒", "amount": 1, "source": "rule", "reason": "消耗"},
    # room_sanity / ruleset/coc7/sanity：extensions.mental 结构化精神状态
    {"type": "extensions", "value": {"mental": {"phobias": [{"name": "蜘蛛恐惧", "tag": "spider"}]}}, "source": "rule", "reason": "心理治疗"},
    # room_interlude：信用评级与现金刷新
    {"type": "credit", "value": 40, "source": "interlude", "reason": "幕间信用评级：演讲"},
    {"type": "assets", "value": {"cash": 100, "spendingLevel": 30}, "source": "interlude", "reason": "幕间现金刷新：演讲"},
    # room_rules._pending_write_changes 的 pending_persist 兼容回退（无 record_id 整卡条目）
    {"type": "background", "value": {"story": "从阿卡姆来"}, "source": "rule"},
]


def test_coc7_historical_queue_replays_without_loss(tmp_path):
    """coc7 旧形态队列（全部历史 type）重放：资源键逐位一致、零丢写回、零崩。"""
    card_path = _stored_card_path(tmp_path)
    app = _app(tmp_path)
    with app.app_context():
        character_service.apply_room_changes(_admin(), "card-2", [dict(row) for row in _HISTORICAL_QUEUE])

    raw = json.loads(card_path.read_text(encoding="utf-8"))
    # 资源类：镜像键与旧固定枚举一致（currentSan / currentHp / currentMp 经
    # coc7 槽位声明解析），数值经派生刷新夹紧后保持写入值。
    assert int(raw["deriveAttributes"]["sanity"]["current"]) == 35
    assert int(raw["deriveAttributes"]["hp"]["current"]) == 3
    assert int(raw["deriveAttributes"]["mp"]["current"]) == 2
    # 结构类：护甲 / 技能 / 属性 / 物品 / 武器 / 状态 / 背景 / 扩展 / 经济全部落卡。
    assert int(raw["battleAttributes"]["armor"]) == 4
    assert raw["characterStatus"]["mentalStates"]["不定期疯狂"] is True
    assert raw["sheet"]["stories"]["desc"] == "从阿卡姆来"
    assert raw["sheet"]["assets"]["cash"] == "100"
    assert raw["sheet"]["assets"]["consumption"] == "30"
    assert raw["sheet"]["extensions"]["mental"]["phobias"][0]["name"] == "蜘蛛恐惧"

    with app.app_context():
        saved = character_service.get_for_actor(_admin(), "card-2")
    skills = {row["name"]: int(row["value"]) for row in saved["skills"]}
    assert skills["侦查"] == 60
    assert skills["克苏鲁神话"] == 12
    assert int(saved["attributes"]["STR"]) == 65
    # 同 record_id 重复条目只应用一次：2 支手电筒再消耗 1 支 → 数量 1。
    equipment = next(row for row in saved["equipment"] if str(row["name"]).startswith("手电筒"))
    assert int(equipment["quantity"]) == 1
    weapon = next(row for row in saved["weapons"] if str(row["name"]) == "Knife")
    assert weapon["damage"] == "1D4"


def _legacy_fixed_enum_resource_payload(changes):
    """改造前固定枚举的资源写回语义（san/hp/mp 镜像键 + 0-999 夹取）。

    作为对拍基准逐位复刻 pre-F1 行为：先按旧固定枚举分派，再按既有派生
    刷新块夹紧（该块 F1 未触碰——对 coc7 派生后 HP/MP 上限 10、SAN 上限
    99、缺省回落卡面当前值 40/10/10）。coc7 槽位声明的镜像键与之一致，
    任何漂移都会在这里露馅。
    """
    expected: dict[str, int] = {}
    for change in changes:
        kind = str(change.get("type") or "").strip().lower()
        try:
            value = int(change.get("value"))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(999, value))
        if kind == "san":
            expected["currentSan"] = value
        elif kind == "hp":
            expected["currentHp"] = value
        elif kind == "mp":
            expected["currentMp"] = value

    def _clamp(value: int, cap: int) -> int:
        return max(0, min(cap, int(value)))

    expected["currentSan"] = _clamp(expected.get("currentSan", 40), 99)
    expected["currentHp"] = _clamp(expected.get("currentHp", 10), 10)
    expected["currentMp"] = _clamp(expected.get("currentMp", 10), 10)
    return expected


@pytest.mark.parametrize(
    "changes",
    [
        # 顺序交错：资源条目穿插在结构条目之间
        [
            {"type": "skill", "name": "侦查", "value": 65},
            {"type": "san", "value": 35},
            {"type": "hp", "value": 8},
            {"type": "attr", "key": "STR", "value": 60},
            {"type": "mp", "value": 6},
        ],
        # 越界与非法值：0-999 夹取、非法值兜底 0、派生刷新再夹到上限
        [
            {"type": "san", "value": 1200},
            {"type": "hp", "value": -5},
            {"type": "mp", "value": "not-a-number"},
        ],
        # 同类多次写入：后到者覆盖（队列按序消费）
        [
            {"type": "san", "value": 30},
            {"type": "san", "value": 33},
            {"type": "hp", "value": 9},
        ],
        # 同 record_id 不同 type：不互斥（技能与 SAN 夹紧可共用 ID）
        [
            {"type": "san", "value": 28, "record_id": "rec-1"},
            {"type": "hp", "value": 7, "record_id": "rec-1"},
        ],
        # 未知 type：静默忽略；未写资源回落卡面当前值再经刷新写回
        [
            {"type": "chaos", "value": 3},
            {"type": "san", "value": 26},
        ],
    ],
)
def test_coc7_resource_dispatch_matches_legacy_fixed_enum(tmp_path, monkeypatch, changes):
    """对拍：coc7 资源写回与改造前固定枚举逐键逐值一致。"""
    _stored_card_path(tmp_path)
    app = _app(tmp_path)
    captured: dict[str, object] = {}

    def spy_apply_save(actor, character_id, payload):
        captured.update(payload)
        return {"id": character_id, **payload}

    monkeypatch.setattr(character_service, "apply_save", spy_apply_save)
    with app.app_context():
        character_service.apply_room_changes(_admin(), "card-2", [dict(row) for row in changes])

    expected = _legacy_fixed_enum_resource_payload(changes)
    for key, value in expected.items():
        assert captured.get(key) == value, f"{key} 漂移：期望 {value}"


def test_dnd5e_undeclared_resource_entries_skip_without_coc_keys(plugins, tmp_path):
    """dnd5e 只声明 hp 槽位：san / mp 条目优雅跳过，不落任何 CoC 镜像键。"""
    app = _app(tmp_path, PLUGIN_DIR=str(PLUGIN_ROOT))
    with app.app_context():
        character_service.apply_save(
            _AdminActor(),
            "dnd-card-f1",
            {
                "name": "艾拉",
                "rulesetId": "dnd5e",
                "sheet": {
                    "level": 3,
                    "class": "Fighter",
                    "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
                    "proficiencies": {"saves": ["con"], "skills": ["athletics"]},
                    "hp": {"current": 20, "max": 24},
                },
            },
        )
        # 旧形态队列（含跨规则集漂移进来的 san / mp 条目）重放：
        character_service.apply_room_changes(
            _AdminActor(),
            "dnd-card-f1",
            [
                {"type": "san", "value": 30, "source": "ai_tool", "reason": "理智检定"},
                {"type": "mp", "value": 5, "source": "rule_command", "reason": "按天恢复 MP"},
                {"type": "hp", "value": 15, "source": "combat", "reason": "战斗结算"},
            ],
        )

    raw = json.loads((tmp_path / "characters" / "dnd-card-f1" / "character.json").read_text(encoding="utf-8"))
    coc_keys = ("currentSan", "current_san", "maxSan", "currentMp", "current_mp", "magicPoints", "maxMp")
    for key in coc_keys:
        assert key not in raw, f"5e 卡上不应出现 CoC 镜像键：{key}"
        assert key not in raw.get("sheet", {}), f"5e sheet 上不应出现 CoC 镜像键：{key}"


def test_dnd5e_dispatch_uses_declared_hp_slot_only(plugins, tmp_path, monkeypatch):
    """分派对拍（5e）：hp 走声明镜像键，san / mp 不进 payload。"""
    app = _app(tmp_path, PLUGIN_DIR=str(PLUGIN_ROOT))
    with app.app_context():
        character_service.apply_save(
            _AdminActor(),
            "dnd-card-f1",
            {
                "name": "艾拉",
                "rulesetId": "dnd5e",
                "sheet": {
                    "level": 3,
                    "class": "Fighter",
                    "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
                    "proficiencies": {"saves": ["con"], "skills": ["athletics"]},
                    "hp": {"current": 20, "max": 24},
                },
            },
        )

    captured: dict[str, object] = {}

    def spy_apply_save(actor, character_id, payload):
        captured.update(payload)
        return {"id": character_id, **payload}

    monkeypatch.setattr(character_service, "apply_save", spy_apply_save)
    with app.app_context():
        character_service.apply_room_changes(
            _AdminActor(),
            "dnd-card-f1",
            [
                {"type": "san", "value": 30, "source": "ai_tool", "reason": "理智检定"},
                {"type": "mp", "value": 5, "source": "rule_command", "reason": "按天恢复 MP"},
                {"type": "hp", "value": 15, "source": "combat", "reason": "战斗结算"},
            ],
        )

    assert captured.get("currentHp") == 15  # hp 按声明的镜像键写回
    assert "currentSan" not in captured and "currentMp" not in captured
    assert "sanity" not in captured


def test_plugin_resolution_failure_falls_back_to_default_slots(tmp_path, monkeypatch):
    """插件解析失败（空注册表 / 未知 id 的等价形态）：回落核心默认槽位，旧行为不变。"""
    from trpg_server.ruleset.capabilities import resource_slot
    from trpg_server.ruleset.registry import RulesetSelectionError

    # 槽位访问层的缺席降级：None 插件 → 核心默认槽位（hp/san/mp，CoC 形状）。
    assert resource_slot(None, "san")["mirror"] == "currentSan"
    assert resource_slot(None, "hp")["mirror"] == "currentHp"
    assert resource_slot(None, "mp")["mirror"] == "currentMp"
    assert resource_slot(None, "luck") is None

    _stored_card_path(tmp_path)
    app = _app(tmp_path)

    def broken_get_plugin(ruleset_id=None):
        raise RulesetSelectionError(str(ruleset_id or ""), "空注册表", "unavailable")

    captured: dict[str, object] = {}

    def spy_apply_save(actor, character_id, payload):
        captured.update(payload)
        return {"id": character_id, **payload}

    monkeypatch.setattr(character_service, "get_plugin", broken_get_plugin)
    monkeypatch.setattr(character_service, "apply_save", spy_apply_save)
    with app.app_context():
        # 不抛错：类型分派按默认槽位解析，其余分支照常消费。
        character_service.apply_room_changes(
            _admin(),
            "card-2",
            [
                {"type": "san", "value": 35, "source": "rule", "reason": "理智检定"},
                {"type": "hp", "value": 3, "source": "combat", "reason": "战斗结算"},
                {"type": "mp", "value": 2, "source": "rule_command", "reason": "按天恢复 MP"},
                {"type": "armor", "value": 4, "source": "combat", "reason": "战斗护甲"},
            ],
        )

    # 与改造前固定枚举逐键逐值一致（默认槽位镜像键 = 旧硬编码键）。
    assert captured.get("currentSan") == 35
    assert captured.get("currentHp") == 3
    assert captured.get("currentMp") == 2
    assert captured.get("armor") == 4
