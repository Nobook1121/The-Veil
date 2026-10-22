"""模板与 schema 键集合同步：模板每个键都有规格，每个规格（除显式豁免）都进模板。

只校验键集合，不校验值 —— 模板值（L2）有意可不同于代码默认值（L1），
例如 ``ai_request_timeout = 1800``（模板） vs 300（代码）。
"""

import tomlkit

from trpg_server.config.paths import template_general_path
from trpg_server.config.schema import SMALL_MODEL_TASK_KEYS, SMALL_MODELS_SECTION, SPECS
from trpg_server.config.sources import flatten_sections

# 不进模板的规格键（schema 注释：skill_bases 由前端按需写回，默认不进模板）。
TEMPLATE_EXEMPT = {("character_rules", "skill_bases")}


def _template_flat():
    doc = tomlkit.parse(template_general_path().read_text(encoding="utf-8"))
    return flatten_sections(doc.unwrap())


def _present_keys():
    flat = _template_flat()
    return {(section, key) for section, values in flat.items() for key in values}


def test_every_template_key_has_spec():
    known = {(spec.section, key) for spec in SPECS for key in spec.keys}
    known |= {(SMALL_MODELS_SECTION, task) for task in SMALL_MODEL_TASK_KEYS}
    for section, key in _present_keys():
        assert (section, key) in known, f"模板键 {section}.{key} 未在 schema 注册"


def test_every_spec_key_in_template():
    present = _present_keys()
    for spec in SPECS:
        if (spec.section, spec.keys[0]) in TEMPLATE_EXEMPT:
            continue
        assert any((spec.section, key) in present for key in spec.keys), \
            f"schema 键 {spec.section}.{spec.keys[0]} 未进模板"
    for task in SMALL_MODEL_TASK_KEYS:
        assert (SMALL_MODELS_SECTION, task) in present


def test_template_uses_canonical_key_names():
    # 模板只写主键名，不写历史别名（show_kp_hints / reply_hints 等）。
    present = _present_keys()
    for spec in SPECS:
        for alias in spec.keys[1:]:
            assert (spec.section, alias) not in present
