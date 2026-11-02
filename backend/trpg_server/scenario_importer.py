"""Scenario document extraction and conversion helpers."""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

try:
    import olefile
except ImportError:  # pragma: no cover
    olefile = None

logger = logging.getLogger(__name__)
_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|第\s*[一二三四五六七八九十百0-9]+\s*[章节幕场景、:.：]|[0-9]+[、.．]\s*)(.*?)\s*$", re.I)
_ANGLE_HEADING_RE = re.compile(r"^\s*[<《【].{1,80}[>》】].*$")
_NAMED_HEADING_RE = re.compile(r"^(?:概要|导入|背景|前言|准备|游戏准备|公开信息|时间线|幕后黑手|角色数据|NPC|人物|怪物|敌人|结局|Ending|True End|Bad End|Crazy End)\s*[：:：]?\s*[A-Za-z0-9一二三四五六七八九十百]*$", re.I)

_SCENE_WORDS = ("场景", "scene", "地点", "房间", "车厢", "車廂", "驾驶室", "駕駛室", "大厅", "大廳")
_ENDING_WORDS = ("结局", "ending", "true end", "bad end", "crazy end", "happy end", "normal end")
_SECTION_TYPES = {
    "导入模块": "opening", "导入": "opening", "开场": "opening", "opening": "opening",
    "背景": "background", "概要": "background", "导入": "background", "前言": "background", "幕后黑手": "background", "background": "background",
    "公开信息": "public_info", "公开资料": "public_info", "public info": "public_info",
    "游戏准备": "preparation", "准备": "preparation", "preparation": "preparation",
    "时间线": "timeline", "timeline": "timeline",
    "结局": "ending", "ending": "ending", "true end": "ending", "bad end": "ending", "crazy end": "ending", "happy end": "ending", "normal end": "ending",
    "npc": "npc", "人物": "npc", "角色数据": "npc", "怪物": "monster", "敌人": "monster", "monster": "monster",
}


def _clean_doc_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_legacy_doc(raw: bytes) -> str:
    if olefile is None:
        raise ValueError("Legacy .doc files require the olefile package")
    try:
        ole = olefile.OleFileIO(io.BytesIO(raw))
        word = ole.openstream(["WordDocument"]).read()
    except Exception as exc:
        raise ValueError("Unable to read legacy .doc document") from exc
    fc_min = int.from_bytes(word[24:28], "little") if len(word) >= 28 else 0
    fc_mac = int.from_bytes(word[28:32], "little") if len(word) >= 32 else len(word)
    fc_min = max(0, min(fc_min, len(word))); fc_mac = max(fc_min, min(fc_mac, len(word)))
    # For the Word binary files used by the project Fib.fcMac is a byte offset
    # into WordDocument's main text stream. Keep the bounded range so binary
    # padding after the document does not become phantom paragraphs.
    payload = word[fc_min:fc_mac]
    utf16 = payload.decode("utf-16le", errors="ignore")
    text = utf16 if sum(1 for c in utf16 if "\u4e00" <= c <= "\u9fff") >= 20 else payload.decode("gb18030", errors="replace")
    text = re.sub(r"HYPERLINK\s+\"[^\"]*\"[^\r\n]*", "", text, flags=re.I)
    return _clean_doc_text(text)


def extract_script_text(source: str | Path | bytes, filename: str | None = None, *, ocr_provider: Any = None) -> str:
    if isinstance(source, bytes):
        raw, name = source, filename or "script.txt"
    else:
        path = Path(source)
        if isinstance(source, str) and ("\n" in source or "\r" in source) and not path.exists(): return _clean_doc_text(source)
        name, raw = path.name, path.read_bytes()
    suffix = Path(name).suffix.casefold()
    logger.debug("scenario_import.extract filename=%s suffix=%s bytes=%d", name, suffix, len(raw))
    if suffix == ".doc": return _extract_legacy_doc(raw)
    if suffix in {".docx", ".pdf", ".txt", ".md", ".markdown", ".text"} or not suffix:
        from trpg_server.scenario_documents import ScenarioDocumentError, parse_scenario_document
        try:
            return parse_scenario_document(raw, name, ocr_provider=ocr_provider).markdown
        except ScenarioDocumentError as exc:
            raise ValueError(str(exc)) from exc
    raise ValueError(f"Unsupported script format: {suffix or 'unknown'}; use pdf, txt, md, docx, or doc")


def _summary(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip(); return compact if len(compact) <= limit else compact[:limit - 1].rstrip() + "…"


def _heading_title(line: str) -> str:
    value = line.strip().replace("車廂", "车厢").replace("車頭", "车头").replace("駕駛室", "驾驶室").replace("號", "号")
    match = _HEADING_RE.match(value)
    if match: return match.group(1).strip()
    if _ANGLE_HEADING_RE.match(value):
        value = re.sub(r"^[<《【]\s*", "", value)
        value = re.sub(r"[>》】].*$", "", value)
        return value.strip()
    return re.sub(r"^[#\s]+", "", value).strip()


def _looks_like_heading(line: str) -> bool:
    plain = line.strip().replace("車廂", "车厢").replace("車頭", "车头").replace("駕駛室", "驾驶室").replace("號", "号")
    if not plain or len(plain) > 80: return False
    if plain in {"侦查", "偵查", "灵感", "靈感", "医学", "醫學", "急救", "话术", "話術", "潜行", "潛行", "敏捷", "力量", "体质", "體質", "幸运", "幸運"}: return False
    # A check/result line such as “《侦查》成功……” is prose, not a
    # section boundary.  Only accept bracket headings when the complete line
    # is the heading (optionally followed by “开始地点”).
    if re.search(r"(?:SC|检定|成功|失败|对抗|的话|则|可以|1d\d)", plain, re.I): return False
    if _HEADING_RE.match(plain) or _NAMED_HEADING_RE.match(plain): return True
    if re.match(r"^[<《【].{1,40}[>》】]\s*(?:开始地点|起始地点)?\s*$", plain): return True
    if re.match(r"^(?:第\s*)?[0-9一二三四五六七八九十百]+\s*(?:号|號)?\s*(?:车厢|車廂)(?:开始地点|開始地點)?\s*$", plain): return True
    if re.match(r"^(?:结局|Ending)\s*[A-C一二三0-9]?\s*$", plain, re.I): return True
    return False


def _module_type(title: str) -> str:
    normalized = re.sub(r"[：:（）()<>《》【】]", " ", title.replace("車廂", "车厢").replace("號", "号").casefold()).strip()
    for key, value in _SECTION_TYPES.items():
        if normalized == key.casefold() or normalized.startswith(key.casefold() + " "): return value
    if any(word in normalized for word in _ENDING_WORDS): return "ending"
    if any(word in normalized for word in ("npc", "人物", "角色")): return "npc"
    if any(word in normalized for word in ("怪物", "敌人", "monster")): return "monster"
    if any(word in normalized for word in _SCENE_WORDS) or re.search(r"[0-9一二三四五六七八九十百]+\s*(?:号|號)", normalized): return "scene"
    return "background"


def _split_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []; title = "背景"; body: list[str] = []
    for line in text.splitlines():
        heading = _heading_title(line) if _looks_like_heading(line) else ""
        if heading:
            if body: sections.append((title, "\n".join(body).strip()))
            title, body = heading, []
        else: body.append(line)
    if body or not sections: sections.append((title, "\n".join(body).strip()))
    # Preserve every explicit source heading as its own module.  In
    # particular, do not merge overview/import/timeline sections into one
    # background block: those sections have different visibility and editor
    # purposes, and merging them is what previously produced “one huge
    # background plus one scene”.
    return [(t, c) for t, c in sections if c.strip()]


def analyze_script_structure(text: str) -> dict[str, Any]:
    source = _clean_doc_text(str(text or ""))
    if not source: raise ValueError("script text is empty")
    sections = _split_sections(source); headings = [title for title, _ in sections]; warnings: list[str] = []
    if len(sections) <= 1: warnings.append("未检测到明确章节标题，已将全文作为单个模块保留")
    if not any(_module_type(title) == "scene" for title, _ in sections): warnings.append("未检测到场景标题，请在编辑器中检查模块边界")
    if len(source) > 200_000: warnings.append("文档较长，建议发布前检查模块边界")
    return {"line_count": len(source.splitlines()), "character_count": len(source), "heading_count": len(headings), "section_count": len(sections), "headings": headings, "detected_types": [_module_type(t) for t, _ in sections], "warnings": warnings}


def _empty_module(module_id: str, module_type: str, title: str, content: str, order: int) -> dict[str, Any]:
    module: dict[str, Any] = {"id": module_id, "module_type": module_type, "title": title, "summary": _summary(content), "content": content, "source_order": order, "source_preserved": True, "send_to_ai": True, "notes": "", "visibility": "kp" if module_type in {"background", "opening"} else "public", "inputs": [], "timeline_entries": [], "attributes": {}, "battle": {}, "skills": [], "weapons": [], "triggers": []}
    if module_type == "opening": module["fixed_opening"] = False
    if module_type == "scene": module["scene_id"] = order
    if module_type == "ending": module.update({"ending_id": order, "open_ending": False})
    return module


def convert_script_to_scenario(text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _clean_doc_text(str(text or ""))
    if not source: raise ValueError("script text is empty")
    result: dict[str, Any] = dict(metadata or {}); result.setdefault("title", "Imported scenario"); result.setdefault("author", "Imported"); result.setdefault("playerCount", 1); result.setdefault("notes", "")
    analysis = analyze_script_structure(source)
    sections = _split_sections(source)
    modules = [_empty_module(f"{_module_type(title)}-{index}", _module_type(title), title, content, index) for index, (title, content) in enumerate(sections, 1)]
    if not any(item["module_type"] == "scene" for item in modules): modules.insert(0, _empty_module("scene-1", "scene", "场景 1", source, 1))
    if not any(item["module_type"] == "ending" for item in modules): modules.append(_empty_module(f"ending-{len(modules) + 1}", "ending", "结局", "", len(modules) + 1))
    result["modules"] = modules; result["conversion"] = {"version": 3, "source_preserved": True, "module_count": len(modules), "warnings": analysis["warnings"], "analysis": analysis, "stages": ["extract", "analyze", "classify", "build_modules", "validate"]}
    return result


def build_ai_conversion_prompt(text: str, sections: list[tuple[str, str]] | None = None) -> str:
    source_sections = sections or _split_sections(text)
    blocks = "\n\n".join(f"--- SOURCE_SECTION {i} ---\nTITLE: {title}\nCONTENT:\n{content}" for i, (title, content) in enumerate(source_sections, 1))
    return f"""你是“剧本广场”的 TRPG 剧本文档结构化转换器。你的任务是忠实整理原文，不是续写、摘要改写或设计新剧情。

【编辑器模块的真实用途】
1. background：仅放 KP 才应预先知道的背景、幕后真相、作者说明、运行建议、改编说明。它不是默认垃圾桶。
2. preparation：人数、游戏时长、难度、推荐技能、角色创建限制、道具限制。
3. public_info：开局即可告诉玩家的世界观、任务目标、玩家可知设定。
4. timeline：按“多久以前/当前/之后”排列的事件时间线；没有明确时间顺序时不要使用。
5. scene：玩家可以抵达、调查或行动的具体地点/空间/阶段。一个地点一个模块：每个车厢、房间必须独立；站台、驾驶室、大厅、血池、其他区域也都必须独立。标题变化或地点变化就是边界。地点内的描述、可调查物、检定成功/失败文本、NPC、战斗、线索全部留在该地点 scene 中，不要拆成 background。
6. ending：明确标记为 True End、Bad End、Crazy End、死亡结局、逃生结局、其他结局的分支；每一种结局必须独立模块，不能合并。
7. npc / monster：只有原文提供了独立角色卡、数值、能力或战斗数据时才使用；角色在某个地点的对白和行为仍留在该 scene，同时可在该模块建立引用。
8. npc / monster 模块必须填写角色卡字段：attributes（STR/DEX/CON/SIZ/POW/EDU/INT/APP/LUC 整数）、battle（dex/str/siz/con/hp/max_hp/armor 整数）、skills（[{{"name":"斗殴","base":45}}]）、weapons（[{{"name":"手杖","damage":"1D6","skill":"斗殴"}}]）。原文没有的字段直接省略，不要编造；其它类型模块这几项留空。
9. triggers：只用于“满足某条件后才揭示”的线索。把检定条件写入 condition，把玩家看到的原文写入 content；不要把普通段落变成 trigger。

【按样本《常暗之厢》执行的判定示例】
- “概要/导入/游戏准备”是背景、准备或公开信息，按标题分别保留，不能把它们和场景正文合成一个背景。
- “<6号车厢>、<7号车厢>、<5号车厢>、<4号车厢>、<3号车厢>、<2号车厢>、<车头车厢>”各是独立 scene；即使某车厢很短，也不能并入前后车厢。
- “《侦查》成功……/失败……”是所属车厢中的检定分支，不是新模块标题。
- “True End”“BAD END”“Crazy End”各是独立 ending。
- 角色数据、Clicker、管理员等只有在原文以独立角色/敌人数据出现时才建立 npc/monster；不要因出现一个人名就把整段地点剧情移走。

【边界和忠实性】
- 输入已被服务端按检测到的原文标题切成 SOURCE_SECTION。每个 SOURCE_SECTION 必须且只能对应一个输出 module，source_order 必须完全相同，不能重排、拆分、合并、丢失。
- 不得发明原文没有的地点、驾驶室、道具、人物、线索、结局或因果关系。无法确定时，保留为当前 SOURCE_SECTION 的 scene/custom，不要猜测。
- 不要输出 content；服务端会按 source_order 原样保留该段全文。你只能返回模块类型、标题、摘要、可见性、结构化字段和线索元数据。
- summary 只描述该段已有内容，最多 180 字；不要把多个地点概括成一个摘要。
- 只输出合法 JSON，不要 Markdown、解释、前后缀文本。

【输出 JSON】
{{"title":"剧本标题","author":"作者（未知则 Imported）","playerCount":1,"notes":"仅根据原文填写","modules":[{{"source_order":1,"module_type":"scene","title":"原文标题","summary":"该段内容摘要","visibility":"public","triggers":[{{"display_name":"线索显示名","keyword":"内部关键词","condition":"原文条件","content_mode":"text"}}],"attributes":{{}},"battle":{{}},"skills":[],"weapons":[],"inputs":[],"timeline_entries":[]}},{{"source_order":2,"module_type":"npc","title":"管家","summary":"旧宅管家，态度傲慢","visibility":"kp","attributes":{{"STR":60,"DEX":55,"CON":50}},"battle":{{"dex":55,"str":60,"siz":65,"con":50,"hp":10,"max_hp":10,"armor":0}},"skills":[{{"name":"斗殴","base":45}}],"weapons":[{{"name":"手杖","damage":"1D6","skill":"斗殴"}}]}}]}}

【原始文档分段】
{blocks}
"""


def _merge_ai_module(local: dict[str, Any], candidate: dict[str, Any]) -> None:
    if candidate.get("module_type") in {"background", "public_info", "preparation", "timeline", "scene", "ending", "npc", "monster", "custom"}: local["module_type"] = candidate["module_type"]
    for key in ("title", "summary", "visibility", "notes", "triggers", "attributes", "battle", "skills", "weapons", "inputs", "timeline_entries", "open_ending"):
        if key in candidate and candidate[key] is not None: local[key] = candidate[key]
    if local["module_type"] == "scene": local.setdefault("scene_id", local.get("source_order"))
    if local["module_type"] == "ending": local.setdefault("ending_id", local.get("source_order"))


def convert_with_ai(requester: Callable[[dict[str, Any]], dict[str, Any]], text: str, model: str = "local-model", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    sections = _split_sections(text)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是严格遵守原文边界的 TRPG 剧本结构化转换器。只输出合法 JSON，不创作、不合并、不丢失任何 SOURCE_SECTION。",
            },
            {"role": "user", "content": build_ai_conversion_prompt(text, sections)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    # 完整的转换请求/响应含整段原文与模型输出，只有开启调试模式才写日志；
    # 正常导入只保留体积与耗时这类摘要行。
    from trpg_server.agents.config import ai_debug_enabled
    from trpg_server.logging_config import redact_sensitive
    debug = ai_debug_enabled()
    logger.info("scenario_conversion.ai_request model=%s sections=%d chars=%d", model, len(sections), len(text))
    if debug:
        logger.info("scenario_conversion.ai_request_full=%s", json.dumps(redact_sensitive(payload), ensure_ascii=False, default=str))
    response = requester(payload)
    if debug:
        logger.info("scenario_conversion.ai_response_full=%s", json.dumps(redact_sensitive(response), ensure_ascii=False, default=str))
    try: content = response["choices"][0]["message"]["content"]; ai_payload = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc: logger.exception("scenario_conversion.ai_invalid_json"); raise ValueError("AI converter returned invalid JSON") from exc
    if not isinstance(ai_payload, dict) or not isinstance(ai_payload.get("modules"), list): raise ValueError("AI converter response must contain modules")
    converted = convert_script_to_scenario(text, {**(metadata or {}), **{k: ai_payload[k] for k in ("title", "author", "playerCount", "notes") if k in ai_payload}})
    by_order = {int(item.get("source_order", 0)): item for item in ai_payload["modules"] if isinstance(item, dict) and str(item.get("source_order", "")).isdigit()}
    for item in converted["modules"]:
        candidate = by_order.get(int(item.get("source_order", 0)))
        if candidate: _merge_ai_module(item, candidate)
    converted["conversion"]["ai"] = {"model": model, "response_module_count": len(ai_payload["modules"]), "merged_module_count": len(by_order)}
    logger.info("scenario_conversion.ai_complete response_modules=%d merged=%d local_modules=%d", len(ai_payload["modules"]), len(by_order), len(converted["modules"]))
    return converted


convert_script = convert_script_to_scenario
parse_script = convert_script_to_scenario
load_script_text = extract_script_text
