"""边界收敛 D2：weapon_catalog 归属收口（CoC 武器表下沉插件）。

D2 迁移内容：核心 ``weapon_catalog`` 只留跨规则集的通用层——武器行结构
规范、角色卡武器行裁剪、每张卡自己的自定义武器库、战斗轮通用文本解析
（伤害骰 / 射程 / 每轮攻击数 / 装弹量）。CoC 专属部分下沉 coc7 插件：

1. ``malfunction``（故障值整数化）与 ``impale``（贯穿标记）两列的数据语义
   → ``combat/rulesets/coc7.py`` 的 ``weapon_field_rules`` 钩子，经
   ``combat/registry.weapon_field_rules`` 按本场战斗规则集取用，缺席退中性
   （``None`` 恒不卡壳、``False`` 恒不贯穿，与 D1 卡壳判定语义衔接）。
2. 预设武器目录读取器 + 数据文件 → ``ruleset/coc7/weapons.py`` 读
   ``data/rulesets/coc7/catalogs/weapons/``；老安装兼容回退旧位置
   ``data/weapons/``（保留至 v1.1.0）。核心的 ``character_service`` /
   ``site_agent_tools`` 不再直读武器数据路径，改经默认插件 catalogs 能力
   （与 ``character_ai.load_weapon_catalog`` 同一口径）；
   ``ruleset/coc7/catalogs.py`` 不再 import 核心 ``weapon_catalog``。
3. ``MELEE / RANGED / DODGE_SKILL_NAMES`` 三张 CoC 技能名回退表 →
   ``combat/rulesets/coc7.py`` 模块常量 + 钩子方法，经
   ``combat/registry.skill_names`` 取用，缺席退空表（不再向其他规则集
   泄漏 CoC 名称——B1 探针 ``test_dnd5e_room_shoot_and_treat_probe``
   已按此边界改写）。

本文件用真插件装配 + ``TRPG_RULESET_DEFAULT=dnd5e`` 复现最严苛环境（与
D1 ``test_ruleset_d1_combat_semantics.py`` 同源手法），验收：

1. CoC 专属取用逐值锚定：``parse_malfunction`` 逐例（``95``/``100``/
   ``N/A``/空串/无字段→``None`` 等）；``weapon_field_rules`` 对故障与贯穿
   两列的解读（含 D1 徒手条目 ``N/A``/``False``）；三张技能名表逐值等于
   迁移后的真源常量。
2. 通用函数跨规则集可用：``weapon_rules`` 只含通用四列（无 malfunction /
   impale 键）；伤害 / 射程 / 每轮 / 装弹量解析逐例；角色卡武器行裁剪对
   任意规则集卡面透传 ``impale`` / ``malfunction`` 字段（dnd5e 路径零破坏）；
   自定义武器库读写往返与「插件目录条目 → 核心行合并」契约。
3. 缺席中性降级：dnd5e 真插件与空注册表两种形态下 ``weapon_field_rules``
   退 ``{"malfunction": None, "impale": False}``、``skill_names`` 退空表；
   coc7 房间 NPC 的格斗兜底命中 CoC 名称表（45），非 coc7 房间退缺省 25。
4. 机械断言（AST / 文件扫描）：``ruleset/coc7/catalogs.py`` 无核心
   ``weapon_catalog`` 依赖；``load_preset_weapons`` 全仓只在
   ``ruleset/coc7/weapons.py`` 定义、``parse_malfunction`` 只在
   ``combat/rulesets/coc7.py`` 定义；核心 ``weapon_catalog`` 不再持有
   二者，核心模块对二者的属性取用清零；三张技能名表不再硬编码在
   ``agents/tools/combat.py`` / ``routes/combat.py``；CoC 武器数据文件
   物理位于插件目录且旧位置回退仍然可达（老安装兼容）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trpg_server import weapon_catalog
from trpg_server.agents.tools import combat
from trpg_server.combat.registry import (
    skill_names,
    unarmed_weapon,
    weapon_field_rules,
)
from trpg_server.combat.rulesets import coc7 as coc7_combat
from trpg_server.plugins import runtime
from trpg_server.plugins.registry import PluginRegistry
from trpg_server.ruleset import builtin_registry, default_ruleset_id, reset_builtin_registry
from trpg_server.ruleset.coc7 import weapons as coc7_weapons
from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "data" / "plugins"
BACKEND_ROOT = REPO_ROOT / "backend" / "trpg_server"
HINT_ENV_KEY = "TRPG_RULESET_DEFAULT"

RIFLE = {
    "name": "Rifle",
    "skill": "Firearms",
    "damage": "1D10",
    "range": "100码",
    "attacks": "1",
    "ammo": "5",
    "malfunction": "98",
    "impale": True,
}


def _load_real_plugins() -> PluginRegistry:
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    return PluginRegistry(PLUGIN_ROOT).load()


@pytest.fixture
def dnd5e_default_process(monkeypatch):
    """真插件装配 + 进程默认规则集注入 dnd5e（复现最严苛环境，D1 同源）。"""
    registry = _load_real_plugins()
    monkeypatch.setenv(HINT_ENV_KEY, "dnd5e")
    reset_builtin_registry()
    assert default_ruleset_id() == "dnd5e"
    yield registry
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


@pytest.fixture
def empty_ruleset_registry(monkeypatch):
    """空插件注册表：任何规则集缺席形态下取用函数必须退中性而不是抛错。"""
    monkeypatch.setenv("TRPG_RULESET_PLUGINS", "")
    runtime.clear_plugin_runtime()
    reset_builtin_registry()
    yield builtin_registry()
    runtime.clear_plugin_runtime()
    reset_builtin_registry()


class FakeContext:
    """战斗工具的最小房间上下文；``ruleset_id`` 决定房间规则集。"""

    def __init__(self, tmp_path: Path, ruleset_id: str):
        self.room_dir = tmp_path
        self.room_id = f"d2-{ruleset_id}"
        self.config_dir = tmp_path / "config"
        self.scenarios_dir = tmp_path / "scenarios"
        self.characters_dir = tmp_path / "characters"
        self._ruleset_id = ruleset_id

    def room_info(self) -> dict:
        return {
            "id": self.room_id,
            "ruleset_id": self._ruleset_id,
            "house_rules": {},
            "members": [],
        }


def _spec(name: str, **overrides) -> dict:
    spec = {
        "name": name,
        "is_npc": True,
        "dex": 50,
        "str": 50,
        "siz": 50,
        "con": 50,
        "hp": 10,
        "max_hp": 10,
        "skills": {},
        "weapons": [],
    }
    spec.update(overrides)
    return spec


# -- 1. CoC 专属取用逐值锚定 -----------------------------------------------


def test_coc7_parse_malfunction_value_by_value():
    """故障值解析逐例：D2 语义锚（迁移前后逐位不变）。"""
    parse = coc7_combat.parse_malfunction
    assert parse("95") == 95
    assert parse("100") == 100
    assert parse(" 98 ") == 98
    assert parse(96) == 96
    assert parse("约 96 码故障") == 96
    assert parse("N/A") is None
    assert parse("n/a") is None
    assert parse("NA") is None
    assert parse("NONE") is None
    assert parse("-") is None
    assert parse("—") is None
    assert parse("") is None
    assert parse(None) is None
    assert parse("abc") is None
    assert parse({}) is None


def test_coc7_weapon_field_rules_interpret_malfunction_and_impale(dnd5e_default_process):
    """武器行故障/贯穿两列的解读归 coc7：显式规则集 id 下逐值锚定。"""
    rules = weapon_field_rules(RIFLE, "coc7")
    assert rules == {"malfunction": 98, "impale": True}
    # 字符串真值沿用核心行层口径：1/true/yes 为真。
    assert weapon_field_rules({"malfunction": "100", "impale": "yes"}, "coc7") == {
        "malfunction": 100,
        "impale": True,
    }
    assert weapon_field_rules({"malfunction": "N/A", "impale": "0"}, "coc7") == {
        "malfunction": None,
        "impale": False,
    }
    # 缺列 / 非字典输入：中性（不卡壳、不贯穿）。
    assert weapon_field_rules({}, "coc7") == {"malfunction": None, "impale": False}
    assert weapon_field_rules(None, "coc7") == {"malfunction": None, "impale": False}
    # D1 徒手条目：N/A / False 两条 CoC 语义原样可读。
    unarmed = unarmed_weapon("coc7")
    assert weapon_field_rules(unarmed, "coc7") == {"malfunction": None, "impale": False}


def test_coc7_skill_name_tables_value_by_value(dnd5e_default_process):
    """三张 CoC 技能名回退表逐值等于迁移后的真源常量。"""
    assert coc7_combat.MELEE_SKILL_NAMES == (
        "格斗（斗殴）", "格斗（斗殴技能）", "斗殴", "格斗", "fencing", "brawl", "fighting",
    )
    assert coc7_combat.RANGED_SKILL_NAMES == (
        "射击（手枪）", "射击（步枪）", "射击（霰弹枪）", "射击", "枪械", "firearms",
    )
    assert coc7_combat.DODGE_SKILL_NAMES == ("闪避", "dodge")
    # 经 registry 显式解析 coc7（进程默认 dnd5e 不影响显式 id）。
    assert skill_names("melee", "coc7") == coc7_combat.MELEE_SKILL_NAMES
    assert skill_names("ranged", "coc7") == coc7_combat.RANGED_SKILL_NAMES
    assert skill_names("dodge", "coc7") == coc7_combat.DODGE_SKILL_NAMES


def test_coc7_catalog_reads_its_own_weapon_data():
    """插件目录读取器端到端：真数据在插件路径下可读、形状即核心行规范。"""
    catalogs = Coc7Catalogs()
    weapons = catalogs.weapons()
    assert weapons, "CoC 预设武器目录不应为空"
    names = {item["name"] for item in weapons}
    assert "燧发手枪" in names
    handguns = next(item for item in weapons if item["name"] == "燧发手枪")
    assert handguns["category"] == "手枪"
    assert handguns["malfunction"] == "95"
    assert handguns["impale"] is True
    # 行结构与核心 normalize_weapon 的输出键逐位一致（前后端行契约）。
    assert set(handguns) == {
        "id", "name", "category", "categoryOrder", "skill",
        "damage", "attacks", "impale", "range", "ammo", "malfunction", "eras", "price",
    }
    # 排序：按目录 categoryOrder，常规（10）在手枪（20）之前。
    orders = [item["categoryOrder"] for item in weapons]
    assert orders == sorted(orders)


# -- 2. 通用函数跨规则集可用 -----------------------------------------------


def test_weapon_rules_keeps_only_generic_fields():
    """weapon_rules 只含跨规则集四列：故障/贯穿不在通用层。"""
    rules = weapon_catalog.weapon_rules(RIFLE)
    assert set(rules) == {"damage", "rangeYards", "attacks", "ammoCapacity"}
    assert rules["rangeYards"] == 100.0
    assert rules["ammoCapacity"] == 5
    assert rules["attacks"] == {"raw": "1", "count": 1, "burst": None}
    assert rules["damage"]["raw"] == "1D10"
    assert rules["damage"]["segments"] == [{"terms": [(1, 10)], "fixed": 0}]


def test_generic_damage_parsing_value_by_value():
    """伤害骰表达式解析逐例（战斗轮 / damage / 毒剂共用）。"""
    parsed = weapon_catalog.parse_damage("1D4+2")
    assert parsed["raw"] == "1D4+2"
    assert parsed["segments"] == [{"terms": [(1, 4)], "fixed": 2}]
    assert parsed["db"] is False and parsed["half_db"] is False

    parsed = weapon_catalog.parse_damage("1D6+DB")
    assert parsed["db"] is True and parsed["half_db"] is False
    assert parsed["segments"] == [{"terms": [(1, 6)], "fixed": 0}]

    # 半 DB：斜杠属于「1/2DB」，不是霰弹枪分段；「1」随半 DB 语法被整段归一，
    # 不再计入固定加值（\bDB\b 在「1/2DB」数字与 D 间无词边界）。
    parsed = weapon_catalog.parse_damage("2D6+1/2DB")
    assert parsed["db"] is False and parsed["half_db"] is True
    assert parsed["segments"] == [{"terms": [(2, 6)], "fixed": 0}]

    # 霰弹枪按距离分段。
    parsed = weapon_catalog.parse_damage("4D6/2D6/1D6")
    assert parsed["segments"] == [
        {"terms": [(4, 6)], "fixed": 0},
        {"terms": [(2, 6)], "fixed": 0},
        {"terms": [(1, 6)], "fixed": 0},
    ]

    # 缺省安全值：1D3。
    parsed = weapon_catalog.parse_damage(None)
    assert parsed["raw"] == "1D3"
    assert parsed["segments"] == [{"terms": [(1, 3)], "fixed": 0}]


def test_generic_range_attacks_ammo_parsing_value_by_value():
    """射程 / 每轮攻击数 / 装弹量解析逐例。"""
    assert weapon_catalog.parse_range_yards("15码") == 15.0
    assert weapon_catalog.parse_range_yards("30米") == 32.808
    assert weapon_catalog.parse_range_yards("10") == 10.0
    assert weapon_catalog.parse_range_yards("6英尺") == 2.0
    assert weapon_catalog.parse_range_yards("接触") == 0.0
    assert weapon_catalog.parse_range_yards("近战") == 0.0
    assert weapon_catalog.parse_range_yards("N/A") is None
    assert weapon_catalog.parse_range_yards("") is None

    assert weapon_catalog.parse_attacks("1") == {"raw": "1", "count": 1, "burst": None}
    assert weapon_catalog.parse_attacks("1(3)") == {"raw": "1(3)", "count": 1, "burst": 3}
    assert weapon_catalog.parse_attacks("3") == {"raw": "3", "count": 3, "burst": None}
    assert weapon_catalog.parse_attacks("N/A") == {"raw": "N/A", "count": 1, "burst": None}

    assert weapon_catalog.parse_ammo("5") == 5
    assert weapon_catalog.parse_ammo("N/A") is None
    assert weapon_catalog.parse_ammo("—") is None
    assert weapon_catalog.parse_ammo(None) is None


def test_sanitize_character_weapons_passthrough_any_ruleset():
    """角色卡武器行裁剪是通用层：impale/malfunction 字段对任意规则集透传。

    dnd5e 卡面同样走这条路径（room_ops / routes 保存快照），字段集合与
    默认值（damage 1D3、长度裁剪）逐位不变是 dnd5e 武器路径零破坏的锚。
    """
    rows = [
        {
            "name": "Longsword",
            "skill": "Melee Weapons",
            "damage": "1D8+3",
            "impale": False,
            "malfunction": "N/A",
        },
        {
            "name": "Pistol",
            "damage": "1D10",
            "impale": True,
            "malfunction": "95",
        },
        "不是字典的行",
        {"damage": "1D6"},
    ]
    sanitized = weapon_catalog.sanitize_character_weapons(rows)
    assert sanitized == [
        {
            "name": "Longsword", "skill": "Melee Weapons", "skillKey": "", "specialtyKey": "",
            "damage": "1D8+3", "range": "", "impale": False, "attacks": "",
            "ammo": "", "malfunction": "N/A", "weight": "", "note": "",
        },
        {
            "name": "Pistol", "skill": "", "skillKey": "", "specialtyKey": "",
            "damage": "1D10", "range": "", "impale": True, "attacks": "",
            "ammo": "", "malfunction": "95", "weight": "", "note": "",
        },
    ]
    # 无名行被丢弃、damage 缺省 1D3、超限截断。
    assert weapon_catalog.sanitize_character_weapons([{"damage": "1D6"}]) == []
    long_name = "名" * 80
    assert len(weapon_catalog.sanitize_character_weapons([{"name": long_name}])[0]["name"]) == 60
    assert weapon_catalog.sanitize_character_weapons([{"name": "x", "damage": ""}])[0]["damage"] == "1D3"


def test_custom_weapons_library_round_trip(tmp_path):
    """每张卡的自定义武器库读写往返（通用层，任何规则集的卡都在用）。"""
    rows = [{"name": "自制撬棍", "damage": "1D6", "impale": True, "malfunction": "N/A"}]
    written = weapon_catalog.write_custom_weapons(tmp_path, rows)
    assert written[0]["name"] == "自制撬棍"
    assert written[0]["impale"] is True
    assert written[0]["category"] == "自定义"
    read_back = weapon_catalog.read_custom_weapons(tmp_path)
    assert read_back == written
    stored = json.loads((tmp_path / "weapons.json").read_text(encoding="utf-8"))
    assert stored["weapons"] == written


def test_plugin_preset_entry_merges_into_character_row():
    """插件目录条目 → 核心行合并契约：插件读取器输出可直接喂核心合并层。"""
    preset = next(
        item for item in Coc7Catalogs().weapons() if item["name"] == "燧发手枪"
    )
    row = {"name": "燧发手枪", "damage": "1D6+2"}
    merged = weapon_catalog.merge_character_weapon_row(row, preset)
    assert merged["name"] == "燧发手枪"
    # 显式覆盖优先，其余取预设。
    assert merged["damage"] == "1D6+2"
    assert merged["skill"] == "射击(手枪)"
    assert merged["skillKey"] == "firearms"
    assert merged["specialtyKey"] == "handgun"
    assert merged["impale"] is True
    assert merged["malfunction"] == "95"
    # 纯匹配器：按 id / 名称在任意目录列表里找条目（同一份目录列表内 identity 成立）。
    catalog = Coc7Catalogs().weapons()
    found = weapon_catalog.find_preset_weapon(catalog, "燧发手枪")
    assert found is not None and found["name"] == "燧发手枪"
    assert weapon_catalog.find_preset_weapon(catalog, "燧发手枪") is found


# -- 3. 缺席中性降级 -------------------------------------------------------


def test_weapon_field_rules_neutral_without_coc7(dnd5e_default_process):
    """dnd5e 真插件：故障/贯穿两列无语义 → 恒不卡壳、恒不贯穿。"""
    assert weapon_field_rules(RIFLE, "dnd5e") == {"malfunction": None, "impale": False}
    assert weapon_field_rules({"malfunction": "95", "impale": True}, "dnd5e") == {
        "malfunction": None,
        "impale": False,
    }


def test_weapon_field_rules_neutral_with_empty_registry(empty_ruleset_registry):
    """空注册表：显式 coc7 也不可解析时退中性，不抛错、不贯穿、不卡壳。"""
    assert weapon_field_rules(RIFLE, "coc7") == {"malfunction": None, "impale": False}
    assert weapon_field_rules(RIFLE, None) == {"malfunction": None, "impale": False}


def test_skill_names_neutral_without_coc7(dnd5e_default_process):
    """dnd5e 真插件：技能名回退表空——不再向其他规则集泄漏 CoC 名称。"""
    assert skill_names("melee", "dnd5e") == ()
    assert skill_names("ranged", "dnd5e") == ()
    assert skill_names("dodge", "dnd5e") == ()
    # 空表下的技能解析：不命中名称兜底，落缺省值与「格斗」占位名。
    value, label = combat._resolve_skill_value({"skills": {"Firearms": 50}}, None, names=(), default=25)
    assert (value, label) == (25, "格斗")


def test_skill_names_neutral_with_empty_registry(empty_ruleset_registry):
    assert skill_names("melee", "coc7") == ()
    assert skill_names("melee", None) == ()


def test_melee_fallback_follows_room_ruleset(tmp_path, dnd5e_default_process, monkeypatch):
    """开战缺省格斗值按房间规则集解析：coc7 命中名称表，dnd5e 退缺省。"""
    # coc7 房间：CoC 名称表命中「格斗（斗殴）」→ 45。
    context = FakeContext(tmp_path / "coc7", "coc7")
    (tmp_path / "coc7").mkdir()
    started = combat.start_combat(
        {"participants": [_spec("拳师", skills={"格斗（斗殴）": 45})]}, context
    )
    assert "error" not in started
    combatant = combat._load_combat(context)["combatants"]["拳师"]
    assert combatant["fighting"] == 45

    # dnd5e 房间（真插件声明了自己的 prepare_combatant，不落核心兜底）：
    # 核心兜底路径对 dnd5e 取空表，技能值退缺省 25。
    assert skill_names("melee", "dnd5e") == ()
    value, label = combat._resolve_skill_value(
        {"skills": {"格斗（斗殴）": 45}}, None,
        names=skill_names("melee", "dnd5e"), default=25,
    )
    # 空表不命中名称兜底，落缺省值与「格斗」占位名（非 CoC 全名）。
    assert (value, label) == (25, "格斗")


# -- 4. 机械断言：归属收口（AST / 文件扫描） -------------------------------


def _module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
                if node.module:
                    imported.add(node.module)
    return imported


def test_coc7_catalogs_module_has_no_core_weapon_catalog_dependency():
    """核账指认的反向依赖已消除：catalogs.py 不再 import 核心 weapon_catalog。"""
    imports = _module_names(BACKEND_ROOT / "ruleset" / "coc7" / "catalogs.py")
    assert not any(
        name == "trpg_server.weapon_catalog" or name.endswith("weapon_catalog")
        for name in imports
    ), sorted(imports)
    # 代码体内也不再有对核心 weapon_catalog 的属性取用。
    source = (BACKEND_ROOT / "ruleset" / "coc7" / "catalogs.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "load_preset_weapons":
            # 只允许经本包自己的读取器模块调用。
            value = node.value
            assert isinstance(value, ast.Name) and value.id == "coc7_weapons", ast.dump(node)


def test_coc7_only_readers_are_defined_in_the_plugin():
    """CoC 专属读取器全仓唯一定义点：load_preset_weapons → 插件包。"""
    allowed = {
        BACKEND_ROOT / "ruleset" / "coc7" / "weapons.py",
    }
    for path in BACKEND_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "load_preset_weapons":
                assert path in allowed, f"load_preset_weapons 定义越界: {path}"
    assert (allowed.pop()).exists()


def test_coc7_only_malfunction_parser_is_defined_in_the_plugin():
    """故障值解析全仓唯一定义点：parse_malfunction → combat/rulesets/coc7.py。"""
    allowed = {
        BACKEND_ROOT / "combat" / "rulesets" / "coc7.py",
    }
    for path in BACKEND_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "parse_malfunction":
                assert path in allowed, f"parse_malfunction 定义越界: {path}"
    assert (allowed.pop()).exists()


def test_core_modules_no_longer_call_coc7_readers():
    """核心层（除插件包外）对 CoC 读取器的属性取用清零。"""
    plugin_packages = {"ruleset", "coc7"}
    for path in BACKEND_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(BACKEND_ROOT).parts
        in_plugin_package = (
            ("ruleset" in parts and "coc7" in parts)
            or ("combat" in parts and "rulesets" in parts and "coc7" in parts)
        )
        if in_plugin_package:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "load_preset_weapons",
                "parse_malfunction",
            }:
                pytest.fail(f"核心模块残留 CoC 读取器调用: {path}:{node.lineno}")


def test_core_weapon_catalog_dropped_coc7_semantics():
    """核心 weapon_catalog 的模块级定义不再含 CoC 目录读取器 / 故障解析。"""
    tree = ast.parse(
        (BACKEND_ROOT / "weapon_catalog.py").read_text(encoding="utf-8"),
        filename="weapon_catalog.py",
    )
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "load_preset_weapons" not in defined
    assert "parse_malfunction" not in defined
    # 通用层函数原位保留（dnd5e 路径逐位不变的字段锚）。
    assert {
        "normalize_weapon", "sanitize_character_weapons", "weapon_rules",
        "parse_damage", "parse_range_yards", "parse_attacks", "parse_ammo",
        "read_custom_weapons", "write_custom_weapons", "upsert_custom_weapons",
        "merge_character_weapon_row", "find_preset_weapon",
    } <= defined


def test_skill_name_tables_not_hardcoded_in_core_combat():
    """三张 CoC 技能名表不再硬编码在核心战斗工具与路由。"""
    for relative in ("agents/tools/combat.py", "routes/combat.py"):
        path = BACKEND_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = {
                    target.id for target in node.targets if isinstance(target, ast.Name)
                }
                leaked = targets & {
                    "MELEE_SKILL_NAMES", "RANGED_SKILL_NAMES", "DODGE_SKILL_NAMES",
                }
                assert not leaked, f"{relative} 仍硬编码 CoC 技能名表: {sorted(leaked)}"


def test_weapon_data_files_live_in_plugin_catalog_dir():
    """CoC 武器数据文件物理位于插件目录（9 个分类文件随迁）。"""
    builtin_dir = REPO_ROOT / "data" / "rulesets" / "coc7" / "catalogs" / "weapons" / "builtin"
    files = sorted(item.name for item in builtin_dir.glob("*.json"))
    assert files == [
        "common.json", "fullAutoRifles.json", "handguns.json", "machineGuns.json",
        "other.json", "semiAutoRifles.json", "shotguns.json", "sniperRifles.json",
        "submachineGuns.json",
    ]


def test_legacy_weapons_dir_fallback_still_reachable(tmp_path, monkeypatch):
    """老安装兼容回退：插件路径缺失时仍可读旧位置 data/weapons/。"""
    legacy_dir = tmp_path / "legacy-weapons"
    (legacy_dir / "builtin").mkdir(parents=True)
    (legacy_dir / "builtin" / "old.json").write_text(
        json.dumps(
            {
                "id": "old-pistol",
                "name": "旧式手枪",
                "skill": {"skillKey": "firearms", "specialtyKey": "handgun", "label": "射击(手枪)"},
                "damage": "1D6",
                "range": "10码",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    import trpg_server.ruleset.coc7.catalogs as catalogs_module

    monkeypatch.setattr(catalogs_module, "WEAPONS_DIR", legacy_dir)
    catalogs = Coc7Catalogs(catalogs_dir=tmp_path / "missing-plugin-path")
    weapons = catalogs.weapons()
    assert [item["name"] for item in weapons] == ["旧式手枪"]
    assert weapons[0]["id"] == "old-pistol"
    assert weapons[0]["malfunction"] == "N/A"
    # 旧版“单武器一个文件”格式仍然兼容。
    assert coc7_weapons.load_preset_weapons(legacy_dir)[0]["damage"] == "1D6"
