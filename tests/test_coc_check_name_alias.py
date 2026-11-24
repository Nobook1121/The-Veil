"""CoC7 检定名别名：角色卡写「侦察」，AI 传「侦查」也必须能命中。"""

from __future__ import annotations

from trpg_server.ruleset.coc7.checks import Coc7Checks


def _card() -> dict:
    return {
        "attributes": {"STR": 50, "DEX": 60},
        "skills": [
            {"name": "侦察", "skillKey": "spothidden", "value": 45},
            {"name": "聆听", "skillKey": "listen", "value": 40},
        ],
    }


def test_lookup_target_accepts_common_chinese_alias():
    checks = Coc7Checks()
    card = _card()

    assert checks.lookup_target(card, "侦察") == 45
    # 「侦查」是玩家/AI 最常写的错别字，规则里就是「侦察」。
    assert checks.lookup_target(card, "侦查") == 45
    assert checks.lookup_target(card, "侦查检定") == 45


def test_lookup_target_still_accepts_english_key_and_attribute():
    checks = Coc7Checks()
    card = _card()

    assert checks.lookup_target(card, "spot hidden") == 45
    assert checks.lookup_target(card, "STR") == 50
    assert checks.lookup_target(card, "不存在") is None
