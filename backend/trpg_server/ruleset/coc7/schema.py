"""CoC7 角色卡 schema。

存储形态统一为「公共信封 + sheet」：信封字段（id/name/avatar/playerId…）所有规则通用，
``sheet`` 是规则自有结构。本文件只负责整理与校验，不负责落盘。
"""

from __future__ import annotations

from typing import Any, Mapping

# 所有规则通用的信封字段。
ENVELOPE_FIELDS = (
    "id",
    "public_id",
    "publisher_id",
    "publisher_name",
    "avatar",
    "name",
    "playerId",
    "playerName",
    "isEditable",
    "createdAt",
    "updatedAt",
)

# CoC7 的卡面数据；与现有前端（character-sheet.ts）和历史存储保持一致。
SHEET_FIELDS = (
    "time",
    "job",
    "age",
    "gender",
    "location",
    "hometown",
    "attributes",
    "deriveAttributes",
    "battleAttributes",
    "characterStatus",
    "pointValues",
    "proSkills",
    "skillPoints",
    "weapons",
    "stories",
    "assets",
    "experiencedModules",
    "friends",
    "skillGroups",
)

ATTRIBUTE_KEYS = ("str", "dex", "con", "app", "pow", "siz", "edu", "int", "luc")
RESOURCE_KEYS = ("sanity", "hp", "mp")
DERIVED_KEYS = ("db", "build", "mov")
STATUS_BODY_KEYS = ("majorWound", "unconscious", "dead")
STATUS_MENTAL_KEYS = ("indefiniteInsanity", "permanentInsanity", "temporaryInsanity")
# 派生值写回卡的键；核心按此列表同步，不再写死 CoC 派生字段。
CARD_DERIVED_KEYS = ("maxHp", "maxSan", "initialSan", "maxMp", "mov", "build", "damageBonus", "armor")
# 运行时资源槽位：快照键 / 卡面镜像键 / 上限来源，由本插件声明。
# ``mirror_aliases`` 兼容旧卡的蛇形 / 历史字段；``initial_*`` 表示理智从 POW 起算。
RESOURCE_SLOTS = (
    {
        "kind": "hp",
        "state": "current_hp",
        "mirror": "currentHp",
        "mirror_aliases": ("current_hp",),
        "cap_state": "max_hp",
        "cap_derived": "maxHp",
        "cap_min": 1,
    },
    {
        "kind": "san",
        "state": "current_san",
        "mirror": "currentSan",
        "mirror_aliases": ("current_san",),
        "cap_state": "max_san",
        "cap_derived": "maxSan",
        "cap_min": 0,
        "initial_state": "initial_san",
        "initial_mirror": "initialSan",
    },
    {
        "kind": "mp",
        "state": "current_mp",
        "mirror": "currentMp",
        "mirror_aliases": ("magicPoints",),
        "cap_state": "max_mp",
        "cap_derived": "maxMp",
        "cap_min": 0,
    },
)


class Coc7CharacterSchema:
    """CoC7 的角色卡结构声明 + 旧卡打包。"""

    ruleset_id = "coc7"
    ruleset_version = "1.0"
    schema_version = 1
    envelope_fields = ENVELOPE_FIELDS
    sheet_fields = SHEET_FIELDS
    attribute_keys = ATTRIBUTE_KEYS
    resource_keys = RESOURCE_KEYS
    derived_keys = DERIVED_KEYS
    status_body_keys = STATUS_BODY_KEYS
    status_mental_keys = STATUS_MENTAL_KEYS

    def derived_card_keys(self) -> tuple[str, ...]:
        """需要从派生值写回卡面的键。"""
        return CARD_DERIVED_KEYS

    def resource_slots(self) -> list[dict[str, Any]]:
        """运行时资源槽位（当前值 / 上限 / 卡面镜像）。"""
        return [dict(slot) for slot in RESOURCE_SLOTS]

    def pack(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        """把当前卡整理成信封 + sheet；旧顶层字段保留为镜像，避免前端读取断档。"""
        data = dict(stored or {})
        data["rulesetId"] = self.ruleset_id
        data["rulesetVersion"] = self.ruleset_version
        data["rulesetSchemaVersion"] = self.schema_version
        data["sheet"] = {
            key: data[key] for key in SHEET_FIELDS if key in data
        }
        return data

    def validate(self, sheet: Mapping[str, Any]) -> list[str]:
        """返回告警列表。核心只记录日志，不阻断历史卡保存。"""
        data = sheet if isinstance(sheet, Mapping) else {}
        warnings: list[str] = []
        attributes = data.get("attributes")
        if isinstance(attributes, Mapping):
            for key in ATTRIBUTE_KEYS:
                value = attributes.get(key, attributes.get(key.upper()))
                if value is None:
                    warnings.append(f"missing attribute: {key}")
        else:
            warnings.append("missing attributes")
        skills = data.get("skillGroups", data.get("skills"))
        if skills is not None and not isinstance(skills, (list, Mapping)):
            warnings.append("skillGroups must be a list or mapping")
        return warnings

    def derive(self, sheet: Mapping[str, Any]) -> dict[str, Any]:
        """按 CoC7 规则重算派生值（HP / MP / 理智上限 / MOV / 体格 / 伤害加值）。

        房间快照与持久卡都调用这里，避免两处各算一套；纯计算见 ``coc7/rules.py``。
        """
        from trpg_server.ruleset.coc7.rules import derive_sheet

        return derive_sheet(sheet)

    def max_san_loss(self, expression: Any) -> int:
        """一次 SAN 变化的最大损失；用于「掷出 100 取最大值」。"""
        from trpg_server.ruleset.coc7.rules import max_of_expression

        return max_of_expression(expression)

    def record_san_loss(self, state: dict[str, Any], *, day: Any, before: int, loss: int) -> dict[str, Any]:
        """累计单个角色当日的理智损失，并判断不定性疯狂阈值。"""
        from trpg_server.ruleset.coc7.rules import record_daily_loss

        return record_daily_loss(state, day=day, before=before, loss=loss)

    def fumble_threshold(self, target: int, configured: int) -> int:
        """大失败阈值：所需值 ≥50 时只有 100 算大失败（派生规则优先）。"""
        from trpg_server.ruleset.coc7.rules import fumble_threshold_for

        return fumble_threshold_for(target, configured)
