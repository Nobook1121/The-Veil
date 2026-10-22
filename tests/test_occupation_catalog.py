import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OCCUPATION_DIR = ROOT / "data" / "occupations" / "builtin"
SKILL_CATALOG_PATH = ROOT / "data" / "character_skills.json"

ATTRIBUTE_KEYS = {"STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUC"}
CATEGORY_SLUGS = {
    "literary",
    "industry",
    "whiteCollar",
    "academic",
    "medical",
    "sports",
    "service",
    "religion",
    "gray",
    "criminal",
    "authority",
}


def _occupation_files():
    return sorted(OCCUPATION_DIR.glob("*.json"))


def _skills_by_key():
    catalog = json.loads(SKILL_CATALOG_PATH.read_text(encoding="utf-8"))
    return {skill["key"]: skill for skill in catalog["skills"]}


def _load_occupations():
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in _occupation_files()]


def _walk_skill_keys(entries):
    """展开本职技能条目，产出 (skillKey, specialtyKey)。"""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("skillKey"):
            yield str(entry["skillKey"]), str(entry.get("specialtyKey") or "")
        for nested in entry.get("chooseOne") or []:
            yield from _walk_skill_keys([nested])


def test_occupation_catalog_has_all_builtin_files():
    files = _occupation_files()
    assert len(files) >= 1
    ids = [json.loads(path.read_text(encoding="utf-8"))["id"] for path in files]
    assert len(ids) == len(set(ids)), "职业 id 必须全局唯一"


def test_occupation_required_fields_and_file_name():
    for path, occupation in _load_occupations():
        assert occupation["id"] == path.stem, f"{path.name} 的 id 必须与文件名一致"
        for field in ("id", "nameKey", "name", "categoryKey", "category", "order"):
            assert occupation.get(field) not in (None, ""), f"{path.name} 缺少字段 {field}"
        assert isinstance(occupation["order"], int) and occupation["order"] >= 1
        assert occupation["categoryKey"] in {f"occupationCategories.{slug}" for slug in CATEGORY_SLUGS}
        assert occupation["nameKey"] == f"occupations.{occupation['id']}"


def test_occupation_credit_rating_range():
    for path, occupation in _load_occupations():
        rating = occupation["creditRating"]
        minimum, maximum = rating["min"], rating["max"]
        assert 0 <= minimum <= maximum <= 99, f"{path.name} 信用评级越界: {minimum}~{maximum}"


def test_occupation_skill_points_terms_are_valid():
    for path, occupation in _load_occupations():
        terms = occupation["occupationSkillPoints"]["terms"]
        assert isinstance(terms, list) and terms, f"{path.name} 职业点数 terms 不能为空"
        for term in terms:
            multiplier = term["multiplier"]
            assert isinstance(multiplier, int) and multiplier > 0, f"{path.name} multiplier 必须为正整数"
            if "attribute" in term:
                assert term["attribute"] in ATTRIBUTE_KEYS, f"{path.name} 非法属性 {term['attribute']}"
            else:
                candidates = term.get("choose") or []
                assert candidates, f"{path.name} choose 项不能为空"
                assert set(candidates) <= ATTRIBUTE_KEYS, f"{path.name} 非法属性候选 {candidates}"


def test_occupation_skills_exist_in_skill_catalog():
    skills = _skills_by_key()
    for path, occupation in _load_occupations():
        for skill_key, specialty_key in _walk_skill_keys(occupation["occupationSkills"]):
            assert skill_key in skills, f"{path.name} 引用了不存在的技能 {skill_key}"
            if specialty_key:
                specialties = {item["key"] for item in skills[skill_key]["specialties"]}
                assert specialty_key in specialties, f"{path.name} 的 {skill_key} 不存在专精 {specialty_key}"


def test_occupation_skill_bases_exist_in_skill_catalog():
    skills = _skills_by_key()
    for path, occupation in _load_occupations():
        for key, value in occupation.get("skillBases", {}).items():
            assert isinstance(value, int) and 0 <= value <= 99, f"{path.name} 基础值非法 {key}={value}"
            base_key, _, specialty_key = key.partition(".")
            assert base_key in skills, f"{path.name} 的 skillBases 引用了不存在的技能 {key}"
            if specialty_key:
                specialties = {item["key"] for item in skills[base_key]["specialties"]}
                assert specialty_key in specialties, f"{path.name} 的 skillBases 不存在专精 {key}"