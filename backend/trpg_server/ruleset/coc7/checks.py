"""CoC7 判定的纯规则入口。

数学仍来自 ``agents/tools/dice.py``，插件只提供稳定接口，避免把同一套规则复制两份。
REST、房间控制台与 AI 工具都经由 ``check_service`` 调用这里；``dice.py`` 不再自己
决定成功等级或难度阈值。
"""

from __future__ import annotations

from typing import Any, Mapping

from trpg_server.ruleset.contracts import CheckRequest, CheckResolution


def _dice_module():
    from trpg_server.agents.tools import dice

    return dice


class Coc7Checks:
    ruleset_id = "coc7"

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
        return _dice_module()._lookup_check_value(dict(card or {}), name)

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
