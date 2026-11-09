"""CoC7 判定的纯规则入口。

数学仍来自 ``agents/tools/dice.py``，插件只提供稳定接口，避免把同一套规则复制两份。
REST、房间控制台与 AI 工具都经由 ``check_service`` 调用这里；``dice.py`` 不再自己
决定成功等级或难度阈值。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.contracts import CheckRequest, CheckResolution

# 属性英文键 / 中文别名 → 规范属性键（判定名解析口径）。
ATTRIBUTE_ALIASES = {
    "STR": "STR",
    "力量": "STR",
    "CON": "CON",
    "体质": "CON",
    "SIZ": "SIZ",
    "体型": "SIZ",
    "DEX": "DEX",
    "敏捷": "DEX",
    "APP": "APP",
    "外貌": "APP",
    "INT": "INT",
    "智力": "INT",
    "灵感": "INT",
    "灵感检定": "INT",
    "智力检定": "INT",
    "POW": "POW",
    "意志": "POW",
    "EDU": "EDU",
    "教育": "EDU",
    "LUC": "LUC",
    "幸运": "LUC",
    "AGE": "AGE",
    "年龄": "AGE",
}
# 房间控制台的属性修改口径：额外接受 luck，且不做「检定」后缀剥离。
COMMAND_ATTRIBUTE_ALIASES = {
    "力量": "STR",
    "敏捷": "DEX",
    "体质": "CON",
    "外貌": "APP",
    "意志": "POW",
    "体型": "SIZ",
    "教育": "EDU",
    "智力": "INT",
    "灵感": "INT",
    "幸运": "LUC",
    "luck": "LUC",
}
COMMAND_ATTRIBUTE_KEYS = ("STR", "DEX", "CON", "APP", "POW", "SIZ", "EDU", "INT", "LUC")
# 属性英文键到中文展示名的映射，用于把本地化展示写回检定结果。
ATTRIBUTE_LABELS = {
    "STR": "力量",
    "CON": "体质",
    "SIZ": "体型",
    "DEX": "敏捷",
    "APP": "外貌",
    "INT": "智力",
    "POW": "意志",
    "EDU": "教育",
    "LUC": "幸运",
    "AGE": "年龄",
}
# 常见 COC7 技能英文键到中文名的回退映射（键统一小写）。
# 角色卡上若带有本地化名称则优先使用角色卡名称，这里仅处理传入英文键的情况。
SKILL_KEY_LABELS = {
    "stealth": "潜行",
    "spothidden": "侦察",
    "listen": "聆听",
    "track": "追踪",
    "lipread": "读唇",
    "libraryuse": "图书馆使用",
    "navigate": "导航",
    "computeruse": "计算机使用",
    "charm": "取悦",
    "fasttalk": "话术",
    "intimidate": "恐吓",
    "persuade": "说服",
    "psychology": "心理学",
    "languageown": "母语",
    "languageother": "外语",
    "fighting": "格斗",
    "firearms": "射击",
    "dodge": "闪避",
    "throw": "投掷",
    "demolitions": "爆破",
    "artillery": "炮术",
    "firstaid": "急救",
    "medicine": "医学",
    "psychoanalysis": "精神分析",
    "hypnosis": "催眠",
    "climb": "攀爬",
    "jump": "跳跃",
    "swim": "游泳",
    "diving": "潜水",
    "appraise": "估价",
    "anthropology": "人类学",
    "accounting": "会计",
    "law": "法律",
    "history": "历史",
    "archaeology": "考古学",
    "naturalworld": "博物学",
    "occult": "神秘学",
    "electronics": "电子学",
    "science": "科学",
    "disguise": "乔装",
    "survival": "生存",
    "artcraft": "技艺",
    "creditrating": "信用评级",
    "cthulhumythos": "克苏鲁神话",
    "locksmith": "开锁",
    "sleightofhand": "妙手",
    "driveauto": "汽车驾驶",
    "ride": "骑术",
}
# 常见的中文技能名变体 -> 技能目录中的正式名称（大小写/字形差异的兼容）。
CHECK_NAME_ALIASES = {
    "侦查": "侦察",
    "观察": "侦察",
    "搜索": "侦察",
    "调查": "侦察",
    "倾听": "聆听",
    "图书馆": "图书馆使用",
    "驾驶": "汽车驾驶",
    "驾车": "汽车驾驶",
    "隐藏": "潜行",
    "躲藏": "潜行",
    "电脑使用": "计算机使用",
}


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _dice_module():
    from trpg_server.agents.tools import dice

    return dice


class Coc7Checks:
    ruleset_id = "coc7"

    def __init__(self, catalogs: Any = None) -> None:
        self._catalogs = catalogs
        self._name_cache: tuple[str, float, set[str]] | None = None

    # -- 目录 / 名称解析 ---------------------------------------------------
    def _catalog_provider(self) -> Any:
        if self._catalogs is not None:
            return self._catalogs
        from trpg_server.ruleset.coc7.catalogs import Coc7Catalogs

        return Coc7Catalogs()

    def catalog_names(self) -> set[str]:
        """技能目录可接受的名称集合（英文键 + 本地化名 + 专精），按文件 mtime 缓存。"""
        catalogs = self._catalog_provider()
        path = catalogs.skills_path() if hasattr(catalogs, "skills_path") else None
        try:
            mtime = path.stat().st_mtime if path is not None else 0.0
        except OSError:
            mtime = 0.0
        if path is not None and self._name_cache is not None:
            if self._name_cache[0] == str(path) and self._name_cache[1] == mtime:
                return self._name_cache[2]
        catalog = catalogs.skills() if hasattr(catalogs, "skills") else {}
        names: set[str] = set()
        if isinstance(catalog, dict):
            locales = catalog.get("locales") if isinstance(catalog.get("locales"), dict) else {}
            locale = str(catalog.get("defaultLocale") or "zh-CN")
            labels = locales.get(locale) if isinstance(locales.get(locale), dict) else {}
            skills = catalog.get("skills") if isinstance(catalog.get("skills"), list) else []
            for item in skills:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if key:
                    names.add(key.casefold())
                label = labels.get(str(item.get("labelKey") or ""))
                if label:
                    names.add(str(label).casefold())
                specialties = item.get("specialties") if isinstance(item.get("specialties"), list) else []
                for specialty in specialties:
                    if not isinstance(specialty, dict):
                        continue
                    specialty_key = str(specialty.get("key") or "").strip()
                    if specialty_key:
                        names.add(specialty_key.casefold())
                    specialty_label = labels.get(str(specialty.get("labelKey") or ""))
                    if specialty_label:
                        names.add(str(specialty_label).casefold())
        names.update(name.casefold() for name in SKILL_KEY_LABELS)
        names.update(label.casefold() for label in SKILL_KEY_LABELS.values())
        if path is not None:
            self._name_cache = (str(path), mtime, names)
        return names

    def attribute_key(self, value: Any) -> str:
        """房间控制台 / 状态写回使用的属性键（接受 luck 等常用别名）。"""
        raw = str(value or "").strip()
        if not raw:
            return ""
        upper = raw.upper()
        if upper in COMMAND_ATTRIBUTE_KEYS:
            return upper
        return COMMAND_ATTRIBUTE_ALIASES.get(raw.casefold(), COMMAND_ATTRIBUTE_ALIASES.get(raw, ""))

    def is_known_check_name(self, check_name: str) -> bool:
        text = str(check_name or "").strip()
        if not text:
            return False
        if text.endswith("检定"):
            text = text[:-2].strip()
        if ATTRIBUTE_ALIASES.get(text) or ATTRIBUTE_ALIASES.get(text.upper()):
            return True
        text = CHECK_NAME_ALIASES.get(text, text)
        return text.casefold() in self.catalog_names()

    def localize_label(self, name: str) -> str:
        raw = str(name or "").strip()
        key = ATTRIBUTE_ALIASES.get(raw) or ATTRIBUTE_ALIASES.get(raw.upper())
        if key:
            return ATTRIBUTE_LABELS.get(key, raw)
        return SKILL_KEY_LABELS.get(raw.casefold(), raw)

    def display_name(self, card: Mapping[str, Any], check_name: str) -> str:
        raw = str(check_name or "").strip()
        key = ATTRIBUTE_ALIASES.get(raw) or ATTRIBUTE_ALIASES.get(raw.upper())
        if key:
            return ATTRIBUTE_LABELS.get(key, raw)
        localized = SKILL_KEY_LABELS.get(raw.casefold(), raw)
        expected = {raw.casefold(), localized.casefold()}
        skills = card.get("skills") if isinstance(card, Mapping) else None
        for skill in skills if isinstance(skills, list) else []:
            if not isinstance(skill, Mapping):
                continue
            candidates = {str(skill.get(field) or "").casefold() for field in ("name", "skillKey", "id")}
            if candidates & expected:
                return str(skill.get("name") or "").strip() or localized
        return localized

    def success_level(self, roll: int, target: int) -> str:
        dice = _dice_module()
        return dice._success_level(
            int(roll),
            int(target),
            dice.DEFAULT_CRITICAL_THRESHOLD,
            dice.DEFAULT_FUMBLE_THRESHOLD,
        )

    def threshold(self, target: int, difficulty: str = "regular") -> int:
        return _dice_module()._threshold(int(target), difficulty)

    def list_check_names(self, card: Mapping[str, Any]) -> list[str]:
        """属性 + 卡上技能名；优先级与 ``dice._resolve_check_name`` 保持一致。"""
        names: list[str] = []
        attributes = card.get("attributes") if isinstance(card, Mapping) else None
        if isinstance(attributes, Mapping):
            names.extend(str(key).upper() for key in attributes)
        skills = card.get("skills") if isinstance(card, Mapping) else None
        if isinstance(skills, list):
            for skill in skills:
                if isinstance(skill, Mapping):
                    label = str(skill.get("name") or skill.get("skillKey") or "").strip()
                    if label:
                        names.append(label)
        return names

    def lookup_target(self, card: Mapping[str, Any], name: str) -> int | None:
        data = dict(card or {})
        normalized_name = str(name or "").strip()
        key = ATTRIBUTE_ALIASES.get(normalized_name) or ATTRIBUTE_ALIASES.get(normalized_name.upper())
        if not key and normalized_name.endswith("检定"):
            base_name = normalized_name[:-2].strip()
            key = ATTRIBUTE_ALIASES.get(base_name) or ATTRIBUTE_ALIASES.get(base_name.upper())
        attrs = data.get("attributes")
        if isinstance(attrs, dict) and key and attrs.get(key) is not None:
            return _as_int(attrs.get(key), -1)
        # 技能匹配同时接受原始名称、英文键与其本地化中文名（例如 stealth / 潜行）。
        localized = SKILL_KEY_LABELS.get(normalized_name.casefold(), normalized_name)
        expected = {normalized_name.casefold(), localized.casefold()}
        skills = data.get("skills")
        for skill in skills if isinstance(skills, list) else []:
            if not isinstance(skill, dict):
                continue
            if any(str(skill.get(k) or "").casefold() in expected for k in ("name", "skillKey", "id")):
                return _as_int(skill.get("value"), -1)
        return None

    def resolve_target(self, card: Mapping[str, Any], request: CheckRequest) -> int:
        value = self.lookup_target(card, request.name)
        return int(value) if value is not None else 1

    def roll(self, card: Mapping[str, Any], request: CheckRequest, rng: Any) -> CheckResolution:
        dice = _dice_module()
        base_target = (
            request.target if request.target is not None else self.lookup_target(card, request.name)
        )
        if base_target is None:
            raise ValueError(f"{request.name} was not found on character card")
        base_target = max(1, min(100, int(base_target)))
        difficulty = dice._normalize_difficulty(request.difficulty) or "regular"
        threshold = self.threshold(base_target, difficulty)
        extra = request.extra if isinstance(request.extra, Mapping) else {}
        threshold = dice._adjust_threshold(threshold, extra.get("adjustment"))

        critical = extra.get("critical_threshold")
        critical = dice.DEFAULT_CRITICAL_THRESHOLD if critical is None else int(critical)
        fumble = extra.get("fumble_threshold")
        fumble = dice.DEFAULT_FUMBLE_THRESHOLD if fumble is None else int(fumble)

        bonus = max(0, min(10, int(request.bonus or 0)))
        penalty = max(0, min(10, int(request.penalty or 0)))
        bonus, penalty = dice._normalize_bonus_penalty(bonus, penalty)
        faces = extra.get("faces")
        if faces is not None:
            roll, candidates = dice._percentile_from_faces(faces, bonus, penalty)
        else:
            roll, candidates = dice._percentile_roll(rng, bonus, penalty)

        level = dice._success_level(int(roll), base_target, critical, fumble)
        success = (int(roll) <= int(threshold) or level == "critical") and level != "fumble"
        return CheckResolution(
            roll=int(roll),
            target=int(threshold),
            level=level,
            success=success,
            detail={
                "base_target": base_target,
                "candidates": list(candidates or []),
                "critical_threshold": critical,
                "fumble_threshold": fumble,
                "difficulty": difficulty,
                "bonus_dice": bonus,
                "penalty_dice": penalty,
                "display_name": dice._resolve_check_display_name(dict(card or {}), request.name),
            },
        )

    # --- 5.9 / 5.10 / 5.6 / 5.13 / 5.12 的运行时能力 -------------------------
    # 纯数学都在各自模块里，这里只做「掷骰 + 调用」的薄适配，供 check_service 调度。

    def social_difficulty(
        self,
        card: Mapping[str, Any],
        *,
        target_skill: int | None = None,
        target_card: Mapping[str, Any] | None = None,
        attitude: str = "neutral",
        leverage: bool = False,
    ) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.social import social_difficulty

        return social_difficulty(
            target_card,
            target_skill=target_skill,
            attitude=attitude,
            leverage=leverage,
        )

    def combined_check(
        self,
        card: Mapping[str, Any],
        names: list[str] | tuple[str, ...],
        *,
        mode: str = "all",
        difficulty: str = "regular",
        bonus: int = 0,
        penalty: int = 0,
        rng: Any = None,
        faces: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.combined import resolve_combined_check

        dice = _dice_module()
        bonus, penalty = dice._normalize_bonus_penalty(
            max(0, min(10, int(bonus or 0))), max(0, min(10, int(penalty or 0)))
        )
        if faces is not None:
            roll, candidates = dice._percentile_from_faces(faces, bonus, penalty)
        else:
            roll, candidates = dice._percentile_roll(
                rng or (lambda sides: dice.random.randint(1, sides)), bonus, penalty
            )
        result = resolve_combined_check(
            card,
            names,
            roll=int(roll),
            mode=mode,
            difficulty=difficulty,
            checks=self,
        )
        result["candidates"] = list(candidates or [])
        result["bonus_dice"] = bonus
        result["penalty_dice"] = penalty
        return result

    def group_luck(
        self,
        members: Any,
        *,
        rng: Any = None,
        faces: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.group import group_luck_check

        dice = _dice_module()
        if faces is not None:
            roll, _ = dice._percentile_from_faces(faces)
        else:
            roll, _ = dice._percentile_roll(rng or (lambda sides: dice.random.randint(1, sides)))
        return group_luck_check(members, roll=int(roll), checks=self)

    def contact_plan(self, card: Mapping[str, Any], name: str, **kwargs: Any) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.contacts import contact_check

        return contact_check(card, name, checks=self, **kwargs)

    def contact_outcome(self, success: bool, *, pushed: bool = False) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.contacts import contact_outcome

        return contact_outcome(bool(success), pushed=pushed)

    def economy_profile(self, cr: Any, era: str = "1920s") -> dict[str, Any]:
        from trpg_server.ruleset.coc7.economy import credit_profile

        return credit_profile(cr, era)

    def economy_expense(self, **kwargs: Any) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.economy import apply_expense

        return apply_expense(**kwargs)

    def age_adjustment_preview(self, age: Any) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.creation import creation_age_effects

        return creation_age_effects(age)

    def apply_age_adjustment(self, age: Any, **kwargs: Any) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.creation import apply_creation_age_adjustment

        return apply_creation_age_adjustment(age, **kwargs)

    def edu_enhancement_step(self, edu: Any, *, rng: Any = None) -> dict[str, Any]:
        from trpg_server.ruleset.coc7.creation import resolve_edu_enhancement

        dice = _dice_module()
        result = resolve_edu_enhancement(
            edu,
            attempts=1,
            rng=rng or (lambda sides: dice.random.randint(1, sides)),
        )
        records = result.get("attempts") or []
        return {"from": result["from"], "to": result["to"], **(records[0] if records else {"roll": None, "gain": 0})}
