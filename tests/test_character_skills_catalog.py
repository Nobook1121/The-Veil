import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "character_skills.json"


def _skills_by_key():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {skill["key"]: skill for skill in catalog["skills"]}


def _specialty_bases(skill_key):
    entry = _skills_by_key()[skill_key]
    return {item["key"]: item.get("base") for item in entry["specialties"]}


def test_parent_bases_follow_coc7_document():
    skills = _skills_by_key()
    # 文档与代码共有、且修改前与官方值不符的技能。
    assert skills["lipRead"]["base"] == 1
    assert skills["computerUse"]["base"] == 5
    assert skills["demolitions"]["base"] == 1
    assert skills["artillery"]["base"] == 1
    assert skills["electronics"]["base"] == 1


def test_user_supplied_bases_are_applied():
    skills = _skills_by_key()
    assert skills["hypnosis"]["base"] == 1
    assert skills["diving"]["base"] == 1
    assert skills["animalHandling"]["base"] == 5


def test_fighting_specialty_bases():
    assert _specialty_bases("fighting") == {
        "brawl": 25,
        "sword": 20,
        "spear": 20,
        "axe": 15,
        "garrote": 15,
        "chainsaw": 10,
        "flail": 10,
        "whip": 5,
    }


def test_firearms_specialty_bases_include_sniper_rifle():
    assert _specialty_bases("firearms") == {
        "handgun": 20,
        "rifleShotgun": 25,
        "submachineGun": 15,
        "sniperRifle": 5,
        "bow": 15,
        "machineGun": 10,
        "heavyWeapon": 10,
    }


def test_custom_skill_base_is_user_defined():
    custom = _skills_by_key()["custom"]
    assert custom["base"] == 0
    assert custom["userDefinedBase"] is True
