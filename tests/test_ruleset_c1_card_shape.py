"""C1：CoC 旧卡形状转换下沉为插件能力后的回归锚点。

前置：三形状函数（检测 / 旧卡→运行时 / 运行时→旧卡）已从
``routes/characters.py`` 整块下沉到 ``ruleset/coc7/card_shape.py``，
核心只保留按 ``raw.rulesetId`` 分派的信封外壳，经 ``ruleset.capabilities``
的三个访问器（``is_legacy_card_shape`` / ``legacy_card_to_runtime`` /
``runtime_card_to_legacy``）触达插件。

本文件锁五件事：

1. 真实旧卡样本「读 → 存 → 读」全链零漂移（首轮即定点，多 era 覆盖）；
2. 转换幂等：已定点的卡在两个方向重复转换逐位不变；
3. dnd5e 卡经信封外壳后不再被 CoC 形状改写（M-H 污染修正。物证：
   ``data/runtime/characters/dnd5e-mwm8iakb-hvif/character.json`` 顶摊曾被
   ``_runtime_to_test_character`` 污染全套 CoC 键，现行保存链已修正）；
4. 最老形态（无 rulesetId）按默认插件读取，maxSan / id 兜底口径不变；
5. 能力层降级：未声明旧卡形状能力的对象走 False / None / 原样返回。

fixtures 说明：``coc7_legacy_card_1920s.json`` 逐字节拷贝自
``data/runtime/characters/char-1/character.json``；
``coc7_legacy_card_1890s.json`` 以另一张真实旧卡为底，仅把 era（顶层
``time`` 与 ``sheet.time`` 两处镜像）改为 1890s 以覆盖 era 透传——仓库内
真实样本的 era 全部是 1920s，故该字段为受控派生，其余字段逐位保留；
``test_character.json`` 为无 rulesetId 的最老形态（仓库既有 fixture）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trpg_server import character_service
from trpg_server.app_factory import create_app
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, reset_builtin_registry
from trpg_server.ruleset.capabilities import (
    is_legacy_card_shape,
    legacy_card_to_runtime,
    runtime_card_to_legacy,
)
from trpg_server.routes.characters import (
    _character_from_storage,
    _is_test_character_shape,
    _runtime_to_test_character,
    _test_character_to_runtime,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"

# CoC 卡面（test 卡）形状键；dnd5e 卡的存储 / 响应 / 读取结果不得出现。
COC_FACE_KEYS = (
    "time",
    "job",
    "location",
    "hometown",
    "gender",
    "age",
    "era",
    "occupationName",
    "residence",
    "birthplace",
    "deriveAttributes",
    "battleAttributes",
    "characterStatus",
    "pointValues",
    "proSkills",
    "skillPoints",
    "skillGroups",
    "stories",
    "assets",
    "experiencedModules",
    "friends",
    "maxSan",
    "initialSan",
    "currentSan",
    "maxMp",
    "currentMp",
    "magicPoints",
    "damageBonus",
    "build",
    "mov",
    "armor",
)

REAL_LEGACY_FIXTURES = (
    ("coc7_legacy_card_1920s.json", "1920s"),
    ("coc7_legacy_card_1890s.json", "1890s"),
)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _dump(card) -> str:
    return json.dumps(card, ensure_ascii=False, sort_keys=True)


class _AdminActor:
    user_id = 9
    role = "ADMIN"

    def has(self, *_args, **_kwargs) -> bool:
        return True


class _BarePlugin:
    """未声明旧卡形状能力的最小插件对象。"""

    id = "bare"


@pytest.fixture
def real_plugins():
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    registry = PluginRegistry(PLUGIN_ROOT).load()
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


# -- 1. 真实旧卡样本：读 → 存 → 读 零漂移 -------------------------------


@pytest.mark.parametrize("fixture_name,expected_era", REAL_LEGACY_FIXTURES)
def test_real_legacy_card_roundtrip_has_zero_drift(fixture_name: str, expected_era: str):
    stored_card = _load_fixture(fixture_name)
    assert stored_card["time"] == expected_era

    runtime_1 = _character_from_storage(stored_card, stored_card["id"])
    assert runtime_1["rulesetId"] == "coc7"
    assert runtime_1["era"] == expected_era
    assert runtime_1["sheet"]["time"] == expected_era

    stored_1 = _runtime_to_test_character(runtime_1)
    runtime_2 = _character_from_storage(stored_1, stored_card["id"])
    stored_2 = _runtime_to_test_character(runtime_2)
    runtime_3 = _character_from_storage(stored_2, stored_card["id"])

    # 首轮往返即定点：再读再存逐位不变。
    assert _dump(runtime_1) == _dump(runtime_2) == _dump(runtime_3)
    assert _dump(stored_1) == _dump(stored_2)
    # era 在两个方向都原样透传。
    assert stored_1["time"] == expected_era


# -- 2. 转换幂等 ---------------------------------------------------------


@pytest.mark.parametrize("fixture_name,expected_era", REAL_LEGACY_FIXTURES)
def test_conversion_idempotent_at_fixpoint(fixture_name: str, expected_era: str):
    stored_card = _load_fixture(fixture_name)
    runtime_card = _test_character_to_runtime(stored_card)

    # 存方向幂等：转换结果再走一轮「读 → 存」逐位不变。
    stored_once = _runtime_to_test_character(runtime_card)
    stored_twice = _runtime_to_test_character(_test_character_to_runtime(stored_once))
    assert _dump(stored_once) == _dump(stored_twice)

    # 读方向幂等：定点卡的重复读取逐位不变。
    runtime_once = _test_character_to_runtime(stored_once)
    runtime_twice = _test_character_to_runtime(_runtime_to_test_character(runtime_once))
    assert _dump(runtime_once) == _dump(runtime_twice)
    assert runtime_once["era"] == expected_era


def test_coc7_schema_exposes_shape_capability():
    plugin = builtin_registry().resolve("coc7")
    legacy_card = _load_fixture("coc7_legacy_card_1920s.json")

    assert plugin.schema.is_legacy_shape(legacy_card) is True
    runtime_card = plugin.schema.legacy_to_runtime(legacy_card, "fb")
    assert runtime_card["id"] == legacy_card["id"]
    assert runtime_card["rulesetId"] == "coc7"
    back_to_legacy = plugin.schema.runtime_to_legacy(runtime_card)
    assert back_to_legacy["skillGroups"] == legacy_card["skillGroups"]
    assert plugin.schema.skill_key_from_name("侦察") == "spotHidden"
    assert plugin.schema.skill_key_from_name("不存在的技能", "fallback") == "fallback"


def test_shape_capability_degrades_for_plugins_without_declaration():
    legacy_card = _load_fixture("coc7_legacy_card_1920s.json")
    bare = _BarePlugin()

    # 缺能力三件套的降级：False / None / 原样返回。
    assert is_legacy_card_shape(bare, legacy_card) is False
    assert legacy_card_to_runtime(bare, legacy_card, "fb") is None
    assert _dump(runtime_card_to_legacy(bare, legacy_card)) == _dump(legacy_card)


# -- 3. dnd5e 卡经外壳后无 CoC 键（M-H 污染修正）------------------------


def _dnd5e_payload() -> dict:
    return {
        "name": "艾拉",
        "rulesetId": "dnd5e",
        "sheet": {
            "level": 3,
            "class": "Fighter",
            "abilities": {"str": 16, "dex": 14, "con": 12, "int": 10, "wis": 13, "cha": 8},
            "proficiencies": {"saves": ["con"], "skills": ["athletics"]},
            "hp": {"current": 20, "max": 24},
            "armorClass": 16,
        },
    }


def test_dnd5e_save_chain_leaves_no_coc_keys(real_plugins, tmp_path: Path):
    """保存链对 dnd5e 卡不再跑 CoC 转换：存储 / 响应 / 回读均无 CoC 卡面键。

    修正物证：本批之前 ``data/runtime/characters/dnd5e-mwm8iakb-hvif``
    顶摊曾被 CoC 形状污染（battleAttributes / skillGroups / stories 等）。
    """
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "PLUGIN_DIR": str(PLUGIN_ROOT),
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOMS_DIR": tmp_path / "rooms",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "WEAPONS_DIR": tmp_path / "weapons",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
        }
    )
    with app.app_context():
        saved = character_service.apply_save(_AdminActor(), "dnd-c1-1001", _dnd5e_payload())
        read_back = character_service.get_for_actor(_AdminActor(), "dnd-c1-1001")

    assert saved["rulesetId"] == "dnd5e"
    assert saved["sheet"]["level"] == 3
    assert read_back["sheet"]["hp"]["max"] == 24
    for card in (saved, read_back):
        assert not [key for key in COC_FACE_KEYS if key in card]

    stored = json.loads(
        (tmp_path / "characters" / "dnd-c1-1001" / "character.json").read_text(encoding="utf-8")
    )
    assert stored["rulesetId"] == "dnd5e"
    assert stored["sheet"]["level"] == 3
    assert stored["maxHp"] == 24 and stored["armorClass"] == 16
    assert not [key for key in COC_FACE_KEYS if key in stored]


def test_dnd5e_plugin_declares_no_legacy_shape(real_plugins):
    dnd5e = builtin_registry().resolve("dnd5e")
    polluted_shape = _dnd5e_payload() | {
        # M-H 物证形态：dnd5e 卡被历史保存链写入的 CoC 键。
        "skillGroups": {"explore": [{"name": "侦查", "base": 25}]},
        "deriveAttributes": {"sanity": {"max": "99"}},
        "battleAttributes": {},
        "characterStatus": {},
        "stories": {},
        "friends": "[]",
        "experiencedModules": "[]",
        "time": "1920s",
        "job": "记者",
    }

    # dnd5e 未声明旧卡形状能力：检测 False，两个方向的外壳都原样透传。
    assert is_legacy_card_shape(dnd5e, polluted_shape) is False
    assert _is_test_character_shape(polluted_shape) is False
    assert legacy_card_to_runtime(dnd5e, polluted_shape, "fb") is None
    assert _test_character_to_runtime(polluted_shape, "fb") is polluted_shape
    assert _dump(_runtime_to_test_character(polluted_shape)) == _dump(polluted_shape)

    # 已污染的存储卡读取时不再被 CoC 转换改写：原样返回（仅补 id 缺省）。
    read_as_is = _character_from_storage(polluted_shape, "fb")
    assert _dump(read_as_is) == _dump(polluted_shape | {"id": "fb"})


# -- 3b. CARD_FIELD_SPECS 拆分：dnd5e 走「信封 + 原样兼容」----------------


def test_dnd5e_export_import_roundtrip_keeps_sheet_and_class(real_plugins, tmp_path: Path):
    """dnd5e 导出 / 导入不再被 CoC 白名单静默丢弃 sheet / level / class。

    拆分前 ``CARD_FIELD_SPECS`` 只覆盖 CoC 卡面字段：dnd5e 导出时
    sheet / level / class 全部被白名单过滤掉，导入时同样丢失（或因
    缺 CoC 必填字段直接 400）。拆分后 coc7 按插件声明的规格走
    （见下方逐位回归），dnd5e 未声明规格 → 信封 + 原样兼容。
    """
    from trpg_server import character_card_io

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "PLUGIN_DIR": str(PLUGIN_ROOT),
            "CHARACTERS_DIR": tmp_path / "characters",
            "ROOMS_DIR": tmp_path / "rooms",
            "CONFIG_DIR": tmp_path / "config",
            "SCENARIOS_DIR": tmp_path / "scenarios",
            "USERS_DIR": tmp_path / "users",
            "LOGS_DIR": tmp_path / "logs",
            "WEAPONS_DIR": tmp_path / "weapons",
            "OCCUPATIONS_DIR": tmp_path / "occupations",
        }
    )
    with app.app_context():
        character_service.apply_save(_AdminActor(), "dnd-c1-2001", _dnd5e_payload())
        # 真实导出链路：先从存储读回（与 GET /export 路由一致）。
        stored_card = character_service.get_for_actor(_AdminActor(), "dnd-c1-2001")
        exported = character_card_io.build_export_payload(stored_card)
        imported = character_card_io.validate_import_payload(json.loads(json.dumps(exported)))

    # 导出：sheet / level / class 原样带出（不再被白名单丢弃）。
    assert exported["rulesetId"] == "dnd5e"
    assert exported["level"] == 3
    assert exported["sheet"]["class"] == "Fighter"
    assert not [key for key in COC_FACE_KEYS if key in exported]

    # 导入：信封 + 原样兼容，sheet / level / class 原样保留。
    assert imported["rulesetId"] == "dnd5e"
    assert imported["name"] == "艾拉"
    assert imported["level"] == 3
    assert imported["sheet"]["hp"] == {"current": 20, "max": 24, "temp": 0}
    assert not [key for key in COC_FACE_KEYS if key in imported]

    # 信封校验仍然生效：非法信封字段照常拒绝。
    broken = json.loads(json.dumps(exported))
    broken["name"] = 123
    with pytest.raises(character_card_io.CharacterCardIOError) as excinfo:
        character_card_io.validate_import_payload(broken)
    assert excinfo.value.code == "invalid_field_type"

    # 信封缺 name 仍然拒绝。
    nameless = json.loads(json.dumps(exported))
    nameless.pop("name")
    with pytest.raises(character_card_io.CharacterCardIOError) as excinfo:
        character_card_io.validate_import_payload(nameless)
    assert excinfo.value.code == "missing_field"


def test_coc7_declared_specs_compose_to_legacy_whitelist(real_plugins):
    """coc7 声明的卡面规格与拆分前的核心白名单逐位一致（导出 JSON 不变）。"""
    from trpg_server import character_card_io
    from trpg_server.ruleset.coc7.schema import Coc7CharacterSchema

    specs = character_card_io._card_field_specs("coc7")
    legacy_card = _load_fixture("coc7_legacy_card_1920s.json")
    runtime_card = _test_character_to_runtime(legacy_card)

    # 组合顺序：信封前段 → CoC 卡面 → createdAt / updatedAt 收尾。
    assert [spec.name for spec in specs[:9]] == [
        "id", "rulesetId", "public_id", "publisher_id", "publisher_name",
        "avatar", "name", "playerId", "playerName",
    ]
    assert [spec.name for spec in specs[-2:]] == ["createdAt", "updatedAt"]
    assert "isEditable" not in [spec.name for spec in specs]
    # CoC 卡面缺省口径原样迁移。
    face = {spec.name: spec for spec in Coc7CharacterSchema().card_field_specs()}
    assert face["era"].default == "1920s"
    assert face["maxSan"].default == 99
    assert face["age"].default == 25
    assert {face[key].name for key in ("attributes", "skills", "weapons", "equipment") if face[key].required} == {
        "attributes", "skills", "weapons", "equipment"
    }
    # 未声明规格的规则集解析为 None（信封 + 原样兼容分支）。
    assert character_card_io._card_field_specs("dnd5e") is None
    del runtime_card  # 仅消费上述断言，不落盘


# -- 4. 最老形态（无 rulesetId）按默认插件读取 ---------------------------


def test_oldest_shape_reads_via_default_plugin_with_coc_defaults():
    oldest = _load_fixture("test_character.json")
    assert "rulesetId" not in oldest

    assert _is_test_character_shape(oldest) is True
    runtime_card = _character_from_storage(oldest, "fallback-id")
    assert runtime_card["rulesetId"] == "coc7"
    assert runtime_card["id"] == "fallback-id"
    # 历史缺省口径随迁移：maxSan 99；time 为空串时原样透传而非改写。
    assert runtime_card["maxSan"] == 99
    assert runtime_card["era"] == ""
    assert runtime_card["age"] == 18

    # C1 只下沉不改动行为：最老形态往返的唯一漂移是历史遗留的 items 文本
    # 复合（equipment → sheet 镜像，新旧实现逐位一致的既有行为），
    # 其余键（信封 / 属性 / 技能 / 背景 / 状态）零变化。
    stored_1 = _runtime_to_test_character(runtime_card)
    runtime_2 = _character_from_storage(stored_1, "fallback-id")
    drift = {
        key
        for key in set(runtime_card) | set(runtime_2)
        if _dump(runtime_card.get(key, "<absent>")) != _dump(runtime_2.get(key, "<absent>"))
    }
    assert drift == {"equipment", "sheet"}
    for key in ("id", "name", "rulesetId", "era", "age", "maxSan", "attributes", "skills", "status", "background", "relationships"):
        assert _dump(runtime_card.get(key)) == _dump(runtime_2.get(key)), key
