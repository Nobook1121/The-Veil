"""通用内容壳（E0 内容契约）。

包含三块：

1. **角色卡描述**：``CharacterCardDescriptor``（v2）把插件 schema 原样转成
   sections / fields / 类型 / i18n 标签；核心不解释字段语义。
2. **成长事件**：``ProgressionEvent`` 支持 XP、等级、里程碑、点数消费与自定义，
   核心不假设只有 XP/等级。
3. **房规 schema**：``HouseRuleSchema`` 声明字段与校验；存储按
   ``core`` + ``by_ruleset`` 命名空间，旧扁平 CoC 键仍可原样读出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from trpg_server.ruleset.resources import normalize_slots

CARD_DESCRIPTOR_VERSION = 2
PROGRESSION_EVENT_VERSION = 1
HOUSE_RULE_SCHEMA_VERSION = 1
HOUSE_RULES_CORE = "core"
HOUSE_RULES_BY_RULESET = "by_ruleset"

FIELD_TYPES = frozenset(
    {
        "string",
        "text",
        "integer",
        "number",
        "boolean",
        "enum",
        "string_list",
        "counter",
        "resource",
        "derived",
        "table",
        "json",
    }
)


def _read(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001 - 插件声明异常按缺省处理
            return default
    return value


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value]


@dataclass(frozen=True)
class CardField:
    key: str
    type: str = "json"
    label_i18n: str = ""
    section: str = ""
    min: int | float | None = None
    max: int | float | None = None
    options: tuple[Any, ...] = ()
    required: bool = False
    read_only: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "type": self.type if self.type in FIELD_TYPES else "json",
            "label_i18n": self.label_i18n,
            "section": self.section,
            "required": self.required,
            "read_only": self.read_only,
        }
        if self.min is not None:
            data["min"] = self.min
        if self.max is not None:
            data["max"] = self.max
        if self.options:
            data["options"] = list(self.options)
        if self.detail:
            data["detail"] = dict(self.detail)
        return data


@dataclass(frozen=True)
class CardSection:
    key: str
    title_i18n: str = ""
    layout: str = "grid"
    fields: tuple[CardField, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title_i18n": self.title_i18n,
            "layout": self.layout,
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass(frozen=True)
class CharacterCardDescriptor:
    """角色卡描述 v2；``to_dict()`` 同时保留旧的平铺键。"""

    ruleset_id: str = ""
    schema_version: int = 0
    envelope_fields: tuple[str, ...] = ()
    sheet_fields: tuple[str, ...] = ()
    attribute_keys: tuple[str, ...] = ()
    resource_keys: tuple[str, ...] = ()
    derived_keys: tuple[str, ...] = ()
    derived_card_keys: tuple[str, ...] = ()
    resource_slots: tuple[dict[str, Any], ...] = ()
    sections: tuple[CardSection, ...] = ()
    status_groups: tuple[dict[str, Any], ...] = ()
    resources: tuple[str, ...] = ()
    conditions: dict[str, Any] = field(default_factory=dict)
    progression: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        sections = [section.to_dict() for section in self.sections]
        return {
            "descriptor_version": CARD_DESCRIPTOR_VERSION,
            "ruleset_id": self.ruleset_id,
            "schema_version": self.schema_version,
            "envelope_fields": list(self.envelope_fields),
            "sheet_fields": list(self.sheet_fields),
            "attribute_keys": list(self.attribute_keys),
            "resource_keys": list(self.resource_keys),
            "derived_keys": list(self.derived_keys),
            "derived_card_keys": list(self.derived_card_keys),
            "resource_slots": [dict(slot) for slot in self.resource_slots],
            "sections": sections,
            "fields": [
                {
                    "key": item["key"],
                    "type": item["type"],
                    "label_i18n": item["label_i18n"],
                    "section": section["key"],
                }
                for section in sections
                for item in section["fields"]
            ],
            "status_groups": [dict(group) for group in self.status_groups],
            "resources": list(self.resources),
            "conditions": dict(self.conditions),
            "progression": dict(self.progression),
            "ui": dict(self.ui),
            "detail": dict(self.detail),
        }


def _sections_from_schema(schema: Any, ruleset_id: str, sheet_fields: tuple[str, ...]) -> tuple[CardSection, ...]:
    """优先读插件声明的 sections；未声明时按 sheet_fields 生成一个通用分组。"""
    declared = _read(schema, "sections")
    sections: list[CardSection] = []
    if isinstance(declared, (list, tuple)):
        for raw_section in declared:
            if not isinstance(raw_section, Mapping):
                continue
            key = str(raw_section.get("key") or "").strip()
            if not key:
                continue
            fields: list[CardField] = []
            raw_fields = raw_section.get("fields")
            for raw_field in raw_fields if isinstance(raw_fields, (list, tuple)) else ():
                if not isinstance(raw_field, Mapping):
                    continue
                field_key = str(raw_field.get("key") or raw_field.get("name") or "").strip()
                if not field_key:
                    continue
                fields.append(
                    CardField(
                        key=field_key,
                        type=str(raw_field.get("type") or "json"),
                        label_i18n=str(raw_field.get("label_i18n") or raw_field.get("label") or ""),
                        section=key,
                        min=raw_field.get("min"),
                        max=raw_field.get("max"),
                        options=tuple(raw_field.get("options") or raw_field.get("values") or ()),
                        required=bool(raw_field.get("required")),
                        read_only=bool(raw_field.get("read_only")),
                        detail=dict(raw_field.get("detail") or {}),
                    )
                )
            sections.append(
                CardSection(
                    key=key,
                    title_i18n=str(raw_section.get("title_i18n") or raw_section.get("title") or ""),
                    layout=str(raw_section.get("layout") or "grid"),
                    fields=tuple(fields),
                )
            )
    if sections:
        return tuple(sections)
    if not sheet_fields:
        return ()
    return (
        CardSection(
            key="sheet",
            title_i18n=f"{ruleset_id}.sheet",
            fields=tuple(
                CardField(key=key, type="json", label_i18n=f"{ruleset_id}.sheet.{key}", section="sheet")
                for key in sheet_fields
            ),
        ),
    )


def character_card_descriptor(plugin: Any) -> CharacterCardDescriptor:
    """把插件 schema 转成描述；无 schema 时返回全空描述（不报错）。"""
    schema = getattr(plugin, "schema", None)
    ruleset_id = str(getattr(plugin, "id", "") or "")
    if schema is None:
        return CharacterCardDescriptor(
            ruleset_id=ruleset_id,
            schema_version=int(getattr(plugin, "schema_version", 0) or 0),
        )
    sheet_fields = tuple(_strings(_read(schema, "sheet_fields")))
    resource_slots = normalize_slots(_read(schema, "resource_slots"))
    status_groups = _read(schema, "status_groups")
    return CharacterCardDescriptor(
        ruleset_id=str(_read(schema, "ruleset_id") or ruleset_id),
        schema_version=int(_read(schema, "schema_version") or 0),
        envelope_fields=tuple(_strings(_read(schema, "envelope_fields"))),
        sheet_fields=sheet_fields,
        attribute_keys=tuple(_strings(_read(schema, "attribute_keys"))),
        resource_keys=tuple(_strings(_read(schema, "resource_keys"))),
        derived_keys=tuple(_strings(_read(schema, "derived_keys"))),
        derived_card_keys=tuple(_strings(_read(schema, "derived_card_keys"))),
        resource_slots=resource_slots,
        sections=_sections_from_schema(schema, ruleset_id, sheet_fields),
        status_groups=tuple(dict(group) for group in status_groups if isinstance(group, Mapping))
        if isinstance(status_groups, (list, tuple))
        else (),
        resources=tuple(_strings(_read(schema, "resources"))),
        conditions=dict(_read(schema, "conditions") or {}),
        progression=dict(_read(schema, "progression") or {}),
        ui=dict(_read(schema, "ui") or {}),
    )


# ---------------------------------------------------------------- 成长事件


@dataclass(frozen=True)
class ProgressionEvent:
    """通用成长事件；核心只记录与展示，规则解释由插件负责。"""

    kind: str = "xp"  # xp / level / milestone / point_spend / custom
    amount: int = 0
    from_level: int | None = None
    to_level: int | None = None
    currency: str = ""
    choices: tuple[dict[str, Any], ...] = ()
    source: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "progression_event_version": PROGRESSION_EVENT_VERSION,
            "kind": self.kind,
            "amount": self.amount,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "currency": self.currency,
            "choices": [dict(item) for item in self.choices],
            "source": self.source,
            "detail": dict(self.detail),
        }


def normalize_progression_event(raw: Mapping[str, Any]) -> ProgressionEvent:
    return ProgressionEvent(
        kind=str(raw.get("kind") or "xp"),
        amount=_as_int(raw.get("amount")),
        from_level=_optional_int(raw.get("from_level")),
        to_level=_optional_int(raw.get("to_level")),
        currency=str(raw.get("currency") or ""),
        choices=tuple(dict(item) for item in (raw.get("choices") or ()) if isinstance(item, Mapping)),
        source=str(raw.get("source") or ""),
        detail=dict(raw.get("detail") or {}),
    )


# ---------------------------------------------------------------- 房规 schema


@dataclass(frozen=True)
class HouseRuleField:
    key: str
    type: str = "json"
    default: Any = None
    values: tuple[Any, ...] = ()
    min: int | float | None = None
    max: int | float | None = None
    label_i18n: str = ""
    help_i18n: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "key": self.key,
            "type": self.type if self.type in FIELD_TYPES else "json",
            "default": self.default,
            "label_i18n": self.label_i18n,
            "help_i18n": self.help_i18n,
        }
        if self.values:
            data["values"] = list(self.values)
        if self.min is not None:
            data["min"] = self.min
        if self.max is not None:
            data["max"] = self.max
        if self.detail:
            data["detail"] = dict(self.detail)
        return data


@dataclass(frozen=True)
class HouseRuleSection:
    key: str
    title_i18n: str = ""
    fields: tuple[HouseRuleField, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title_i18n": self.title_i18n,
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass(frozen=True)
class HouseRuleSchema:
    """插件声明的房规 schema；核心只做容器、类型校验与命名空间。"""

    ruleset_id: str = ""
    schema_version: int = HOUSE_RULE_SCHEMA_VERSION
    sections: tuple[HouseRuleSection, ...] = ()

    def field_definitions(self) -> dict[str, HouseRuleField]:
        return {item.key: item for section in self.sections for item in section.fields}

    def defaults(self) -> dict[str, Any]:
        return {
            key: item.default
            for key, item in self.field_definitions().items()
            if item.default is not None
        }

    def validate(self, values: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """返回问题列表；空列表表示通过。核心不猜测跨字段规则。"""
        data = values if isinstance(values, Mapping) else {}
        definitions = self.field_definitions()
        issues: list[dict[str, Any]] = []
        for key, value in data.items():
            if key not in definitions:
                issues.append(
                    {"path": str(key), "code": "unknown_field", "severity": "error", "message": f"未知房规字段：{key}"}
                )
        for key, definition in definitions.items():
            if key not in data:
                continue
            issue = _validate_field(key, definition, data[key])
            if issue:
                issues.append(issue)
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "house_rule_schema_version": self.schema_version,
            "ruleset_id": self.ruleset_id,
            "sections": [section.to_dict() for section in self.sections],
        }


def _validate_field(key: str, definition: HouseRuleField, value: Any) -> dict[str, Any] | None:
    field_type = definition.type
    if field_type in {"integer", "counter", "resource"}:
        if not isinstance(value, int) or isinstance(value, bool):
            return _issue(key, "type_mismatch", f"{key} 必须是整数")
        if definition.min is not None and value < definition.min:
            return _issue(key, "out_of_range", f"{key} 不能小于 {definition.min}")
        if definition.max is not None and value > definition.max:
            return _issue(key, "out_of_range", f"{key} 不能大于 {definition.max}")
    elif field_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _issue(key, "type_mismatch", f"{key} 必须是数字")
        if definition.min is not None and value < definition.min:
            return _issue(key, "out_of_range", f"{key} 不能小于 {definition.min}")
        if definition.max is not None and value > definition.max:
            return _issue(key, "out_of_range", f"{key} 不能大于 {definition.max}")
    elif field_type == "boolean":
        if not isinstance(value, bool):
            return _issue(key, "type_mismatch", f"{key} 必须是布尔值")
    elif field_type == "string":
        if not isinstance(value, str):
            return _issue(key, "type_mismatch", f"{key} 必须是字符串")
    elif field_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return _issue(key, "type_mismatch", f"{key} 必须是字符串数组")
    elif field_type == "enum":
        if definition.values and value not in definition.values:
            return _issue(key, "invalid_value", f"{key} 的取值不在允许范围内")
    return None


def _issue(key: str, code: str, message: str, severity: str = "error") -> dict[str, Any]:
    return {"path": key, "code": code, "severity": severity, "message": message}


def split_house_rules(stored: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """把房规拆成 ``(core, by_ruleset)``；旧扁平结构返回空命名空间。"""
    data = stored if isinstance(stored, Mapping) else {}
    core = data.get(HOUSE_RULES_CORE)
    by_ruleset = data.get(HOUSE_RULES_BY_RULESET)
    return (
        dict(core) if isinstance(core, Mapping) else {},
        {
            str(key): dict(value)
            for key, value in (by_ruleset.items() if isinstance(by_ruleset, Mapping) else ())
            if isinstance(value, Mapping)
        },
    )


def merge_house_rules(
    core: Mapping[str, Any] | None,
    by_ruleset: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    return {
        HOUSE_RULES_CORE: dict(core or {}),
        HOUSE_RULES_BY_RULESET: {
            str(key): dict(value) for key, value in (by_ruleset or {}).items()
        },
    }


def active_house_rules(stored: Mapping[str, Any] | None, ruleset_id: str) -> dict[str, Any]:
    """读取活动规则集的房规。

    新结构按命名空间取；旧扁平结构原样返回，保持 CoC7 现有读取路径不变。
    """
    data = stored if isinstance(stored, Mapping) else {}
    if HOUSE_RULES_CORE in data or HOUSE_RULES_BY_RULESET in data:
        _, by_ruleset = split_house_rules(data)
        return dict(by_ruleset.get(str(ruleset_id or "").strip().lower(), {}))
    return dict(data)


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CARD_DESCRIPTOR_VERSION",
    "FIELD_TYPES",
    "HOUSE_RULES_BY_RULESET",
    "HOUSE_RULES_CORE",
    "HOUSE_RULE_SCHEMA_VERSION",
    "PROGRESSION_EVENT_VERSION",
    "CardField",
    "CardSection",
    "CharacterCardDescriptor",
    "HouseRuleField",
    "HouseRuleSchema",
    "HouseRuleSection",
    "ProgressionEvent",
    "active_house_rules",
    "character_card_descriptor",
    "merge_house_rules",
    "normalize_progression_event",
    "split_house_rules",
]
