"""5etools datatag 解析器（kiwee 中文数据同构，无外部依赖）。

5etools 正文是 ``{@tag …}`` 富文本：骰公式、豁免、跨实体引用都是机器可
解析的内联 tag（约 40–50 种，全量列表见上游 wiki.tercept.net）。这里先做
v1 高频 ~12 类 + 未知 tag 兜底，供规则引擎（骰公式结算）与后续刀消费：

    {@damage 8d6}              → 文本 "8d6"，mark {type: damage, expr}
    {@scaledamage 8d6|3-9|1d6} → mark {type: scaled_damage, base/slot_range/per_level}
    {@dice 1d20+5|先攻}        → mark {type: dice, expr, label}
    {@d20 1d20+2}              → 同 dice
    {@creature 地精|MM}        → mark {type: ref, category: monster, name, source}
    {@spell 火球术|PHB}         → mark {type: ref, category: spell, …}
    {@condition 中毒}          → mark {type: ref, category: condition, …}
    {@item 长剑|PHB} / {@skill 察觉|PHB}
    {@save dex}                → 文本 "敏捷"，mark {type: save, ability}（中文入参也认）
    {@hit 5}                   → 文本 "+5"，mark {type: hit}
    {@atk mw}                  → 文本 "近战武器攻击"，mark {type: attack_mode}
    {@dc 15}                   → 文本 "DC 15"，mark {type: dc}
    {@recharge 5-6}            → 文本 "（充能 5-6）"，mark {type: recharge}
    {@b …}/{@i …}              → 纯样式，只保留内容（嵌套 tag 照常解析）
    未知 tag                   → 内容照常进纯文本，mark {type: unknown, tag, raw}

entries 树（str 与 dict 混合递归）→ ``{"text": 纯文本, "marks": 标记列表}``。
marks 按文档序扁平排列，类型字段稳定，消费方按 type 分派即可。
"""

from __future__ import annotations

from typing import Any, Callable

# --- 属性中英对照（{@save dex} 渲染为"敏捷"，也接受中文入参）---------------

_ABILITY_ZH: dict[str, str] = {
    "str": "力量",
    "dex": "敏捷",
    "con": "体质",
    "int": "智力",
    "wis": "感知",
    "cha": "魅力",
}
_ABILITY_EN: dict[str, str] = {v: k for k, v in _ABILITY_ZH.items()}

# {@atk mw} 的字母含义：m 近战 / r 远程 / w 武器 / s 法术。
_ATK_MODE_ZH: dict[str, str] = {"m": "近战", "r": "远程", "w": "武器", "s": "法术"}

# 纯样式 tag：不出 mark，内容递归解析（嵌套 {@hit} 等照常收集）。
_STYLE_TAGS = frozenset({"b", "i", "u", "s", "em", "strong", "code"})


def _first(params: list[str], index: int = 0, default: str = "") -> str:
    try:
        return str(params[index]).strip()
    except (IndexError, TypeError):
        return default


# --- 单 tag 渲染 ------------------------------------------------------------


def _t_damage(params: list[str]) -> tuple[str, dict[str, Any]]:
    expr = _first(params)
    return expr, {"type": "damage", "expr": expr}


def _t_scaledamage(params: list[str]) -> tuple[str, dict[str, Any]]:
    base = _first(params)
    slot_range = _first(params, 1)
    per_level = _first(params, 2)
    return base, {
        "type": "scaled_damage",
        "base": base,
        "slot_range": slot_range,
        "per_level": per_level,
    }


def _t_dice(params: list[str]) -> tuple[str, dict[str, Any]]:
    expr = _first(params)
    label = _first(params, 1)
    text = f"{expr}（{label}）" if label else expr
    return text, {"type": "dice", "expr": expr, "label": label}


def _t_save(params: list[str]) -> tuple[str, dict[str, Any]]:
    raw = _first(params).lower()
    ability = _ABILITY_EN.get(raw, raw)
    if ability not in _ABILITY_ZH:
        # 未知的属性缩写（如豁免写成全名）原样保留，不猜。
        return raw, {"type": "save", "ability": raw}
    return _ABILITY_ZH[ability], {"type": "save", "ability": ability}


def _t_hit(params: list[str]) -> tuple[str, dict[str, Any]]:
    value = _first(params)
    if value and not value.startswith(("+", "-")):
        value = f"+{value}"
    return value, {"type": "hit", "value": value}


def _t_dc(params: list[str]) -> tuple[str, dict[str, Any]]:
    value = _first(params)
    ability = _first(params, 1).lower()
    ability = _ABILITY_EN.get(ability, ability)
    text = f"DC {value}"
    return text, {"type": "dc", "value": value, "ability": ability}


def _t_atk(params: list[str]) -> tuple[str, dict[str, Any]]:
    modes = [ch for ch in _first(params).lower() if ch.isalpha()]
    parts = [_ATK_MODE_ZH[ch] for ch in modes if ch in _ATK_MODE_ZH]
    text = "".join(parts) + "攻击" if parts else "攻击"
    return text, {"type": "attack_mode", "modes": modes}


def _t_recharge(params: list[str]) -> tuple[str, dict[str, Any]]:
    rng = _first(params) or "5-6"
    return f"（充能 {rng}）", {"type": "recharge", "range": rng}


def _ref_handler(category: str, default_source: str = "") -> Callable[[list[str]], tuple[str, dict[str, Any]]]:
    def handler(params: list[str]) -> tuple[str, dict[str, Any]]:
        name = _first(params)
        source = _first(params, 1, default_source)
        return name, {"type": "ref", "category": category, "name": name, "source": source}

    return handler


_HANDLERS: dict[str, Callable[[list[str]], tuple[str, dict[str, Any]]]] = {
    "damage": _t_damage,
    "scaledamage": _t_scaledamage,
    "dice": _t_dice,
    "d20": _t_dice,
    "hit": _t_hit,
    "save": _t_save,
    "dc": _t_dc,
    "atk": _t_atk,
    "recharge": _t_recharge,
    "creature": _ref_handler("monster"),
    "monster": _ref_handler("monster"),
    "spell": _ref_handler("spell"),
    "condition": _ref_handler("condition", "PHB"),
    "disease": _ref_handler("disease"),
    "item": _ref_handler("item"),
    "baseitem": _ref_handler("baseitem"),
    "skill": _ref_handler("skill"),
    "feat": _ref_handler("feat"),
    "race": _ref_handler("race"),
    "background": _ref_handler("background"),
    "class": _ref_handler("class"),
    "subclass": _ref_handler("subclass"),
    "table": _ref_handler("table"),
    "variantrule": _ref_handler("variantrule"),
}


# --- 文本层扫描 -------------------------------------------------------------


def _matching_brace(text: str, open_at: int) -> int:
    """``text[open_at]`` 是 '{'，返回配对 '}' 下标；未闭合返回 -1。"""
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _tag_and_params(inner: str) -> tuple[str, str]:
    """'{@spell 火球术|PHB}' 的内容 → ('spell', '火球术|PHB')。"""
    stripped = inner.strip()
    if not stripped:
        return "", ""
    name, _, rest = stripped.partition(" ")
    return name.strip(), rest.strip()


def parse_text(text: str) -> dict[str, Any]:
    """扫一段文本：剥离 ``{@tag}``，产出 ``{"text", "marks"}``。

    嵌套 tag（``{@b {@hit +5}}``）靠括号深度配对 + 递归处理。
    """
    out: list[str] = []
    marks: list[dict[str, Any]] = []
    text = str(text or "")
    i = 0
    while i < len(text):
        open_at = text.find("{@", i)
        if open_at < 0:
            out.append(text[i:])
            break
        close_at = _matching_brace(text, open_at)
        if close_at < 0:
            out.append(text[i:])
            break
        out.append(text[i:open_at])
        inner = text[open_at + 2 : close_at]
        out.append(_render_tag(inner, marks))
        i = close_at + 1
    return {"text": "".join(out), "marks": marks}


def _render_tag(inner: str, marks: list[dict[str, Any]]) -> str:
    name, rest = _tag_and_params(inner)
    if not name:
        # "{@}" 空壳或畸形，原样吞掉，正文不留残骸。
        return rest
    if name in _STYLE_TAGS:
        sub = parse_text(rest)
        marks.extend(sub["marks"])
        return sub["text"]
    handler = _HANDLERS.get(name)
    if handler is not None:
        piece, mark = handler(rest.split("|")) if rest else handler([""])
        if mark:
            marks.append(mark)
        return piece
    # 未知 tag：正文取第一个参数（上游渲染口径，如 {@quickref 施法|PHB|1}
    # 只显示"施法"），内部嵌套 tag 递归剥掉；原始内容留诊断 mark。
    display = rest.split("|")[0] if rest else ""
    sub = parse_text(display)
    marks.append({"type": "unknown", "tag": name, "raw": inner.strip()})
    return sub["text"]


# --- entries 树递归 ---------------------------------------------------------


def parse_entries(entries: Any) -> dict[str, Any]:
    """entries 树（str/dict/list 任意混合）→ ``{"text", "marks"}``。

    常见块类型全走同一递归：entries/inset/readaloud/quote/item 的 name 行
    +entries，list 的 items，table 的 caption+rows（单元格可以是嵌套 entries）。
    未知 dict 只保留其 text 字段兜底，不做猜测渲染。
    """
    marks: list[dict[str, Any]] = []
    text = _collect(entries, marks)
    return {"text": text, "marks": marks}


def render_plain_text(entries: Any) -> str:
    """便利函数：只要纯文本（{@damage 8d6} → "8d6"）。"""
    return parse_entries(entries)["text"]


def extract_dice(entries: Any) -> list[dict[str, Any]]:
    """便利函数：给战斗工具抽可结算骰公式（damage/scaled_damage/dice）。"""
    marks = parse_entries(entries)["marks"]
    return [m for m in marks if m.get("type") in {"damage", "scaled_damage", "dice"}]


def _collect(node: Any, marks: list[dict[str, Any]]) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        sub = parse_text(node)
        marks.extend(sub["marks"])  # 树递归时把叶子层的标记汇进总列表
        return sub["text"]
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, list):
        return "\n".join(part for part in (_collect(item, marks) for item in node) if part)
    if isinstance(node, dict):
        return _collect_dict(node, marks)
    return str(node)


def _collect_dict(block: dict[str, Any], marks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    header = block.get("name") or block.get("caption") or ""
    if isinstance(header, str) and header.strip():
        header_sub = parse_text(header)
        marks.extend(header_sub["marks"])
        parts.append(header_sub["text"])
    if isinstance(block.get("rows"), list):
        row_texts: list[str] = []
        for row in block["rows"]:
            cells = row if isinstance(row, list) else [row]
            joined = " | ".join(cell for cell in (_collect(c, marks) for c in cells) if cell)
            if joined:
                row_texts.append(joined)
        if row_texts:
            parts.append("\n".join(row_texts))
    if isinstance(block.get("tables"), list):
        parts.append(_collect(block["tables"], marks))
    if isinstance(block.get("entries"), list):
        parts.append(_collect(block["entries"], marks))
    if isinstance(block.get("items"), list):
        parts.append(_collect(block["items"], marks))
    if not parts:
        fallback = block.get("text")
        if isinstance(fallback, str) and fallback.strip():
            parts.append(parse_text(fallback)["text"])
    return "\n".join(part for part in parts if part)


__all__ = [
    "extract_dice",
    "parse_entries",
    "parse_text",
    "render_plain_text",
]
