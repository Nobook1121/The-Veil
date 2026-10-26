"""剧本结构抽取的确定性测试（§7.2-§7.4）。"""
from trpg_server.agents.narrative.extract import _document_from_text, detect_sections, extract_narrative
from trpg_server.agents.narrative.model import VISIBILITY_KP_ONLY
from trpg_server.scenario_documents import DocumentBlock, ParsedDocument

SAMPLE = """# 第一幕 雨夜庄园
## 第一层 大厅区域
### 场景 1 玄关
雨夜，湿冷的空气扑面而来。
- 门：厚重的橡木门，门缝里透出微光。
- 衣帽架：挂着湿透的斗篷。

### 场景 2 大厅
中央有一座古老的雕像，空气中弥漫着灰尘。
- 雕像：底座布满苔藓。
  - 侦查：观察雕像底座。
    成功：发现底座刻着「奈亚的五个房间」。
    失败：只看到磨损的纹路。
- 书架：五层书架，书脊崭新。

线索：血迹一直延伸到楼梯。
NPC：管家陈伯，他似乎知道些什么。

# 第二幕 真相
### 场景 3 地下室
暗门藏在楼梯下方，需要找到钥匙才能打开。
实际上，管家并不是真正的管家。

# 结局 逃出庄园
"""


def _doc(text: str) -> ParsedDocument:
    blocks = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        level = len(line) - len(line.lstrip("#")) if line.lstrip().startswith("#") else None
        blocks.append(DocumentBlock(text=line.strip(), heading_level=level))
    return ParsedDocument(filename="sample.md", markdown=text, blocks=blocks)


def test_sections_and_nodes_are_extracted():
    document = _doc(SAMPLE)
    sections = detect_sections(document)
    kinds = [section.kind for section in sections]
    assert kinds.count("act") == 2
    assert "area" in kinds
    assert kinds.count("location") == 3
    assert "ending" in kinds

    extracted = extract_narrative(document, scenario_id="1")
    locations = [node for node in extracted.nodes if node.kind == "location"]
    assert [node.title for node in locations] == ["场景 1 玄关", "场景 2 大厅", "场景 3 地下室"]
    assert all(node.parent for node in locations)

    hall = next(node for node in locations if node.title == "场景 2 大厅")
    statue = next(item for item in hall.objects if item["name"] == "雕像")
    interaction = statue["interactions"][0]
    assert interaction["skill"] == "侦查"
    assert "奈亚" in interaction["outcomes"]["success"]["text"]
    assert "磨损" not in interaction["outcomes"]["success"]["text"]
    assert "磨损" in interaction["outcomes"]["fail"]["text"]
    assert "奈亚" not in interaction["outcomes"]["fail"]["text"]


def test_clue_npc_and_ending_visibility():
    extracted = extract_narrative(_doc(SAMPLE), scenario_id="1")
    clues = [node for node in extracted.nodes if node.kind == "clue"]
    npcs = [node for node in extracted.nodes if node.kind == "npc"]
    endings = [node for node in extracted.nodes if node.kind == "ending"]
    assert any("血迹" in node.title for node in clues)
    assert any("管家" in node.title for node in npcs)
    assert len(endings) == 1
    assert endings[0].visibility == VISIBILITY_KP_ONLY


def test_plain_text_without_markdown():
    text = "\n".join(
        [
            "第一章 雨夜",
            "SC 1 玄关",
            "门锁着，钥匙孔里有划痕。",
            "SC 2 大厅",
            "大厅中央有一尊雕像。",
            "第三章 结局",
            "调查员逃出了庄园。",
        ]
    )
    extracted = extract_narrative(text, scenario_id="2")
    titles = [node.title for node in extracted.nodes if node.kind == "location"]
    assert titles == ["玄关", "大厅"]
    assert any(node.kind == "ending" for node in extracted.nodes)


def test_sc_ambiguity_is_not_a_location():
    text = "\n".join(
        [
            "第一章 雨夜",
            "SC 检定：图书馆使用",
            "调查员需要先做 SC 检定，失败会损失理智。",
            "SC 1 玄关",
            "门锁着。",
        ]
    )
    sections = detect_sections(_document_from_text(text))
    location_titles = [section.title for section in sections if section.kind == "location"]
    assert location_titles == ["玄关"]


def test_background_module_is_kp_only_and_not_a_location_node():
    modules = [
        {"id": "m1", "module_type": "background", "title": "幕后黑手", "content": "实际上，管家才是幕后黑手。"},
        {"id": "m2", "module_type": "scene", "title": "场景 4 阁楼", "content": "阁楼里堆满旧箱子。"},
    ]
    extracted = extract_narrative(_doc(SAMPLE), scenario_id="1", modules=modules)
    titles = [node.title for node in extracted.nodes]
    assert "幕后黑手" not in titles
    assert "场景 4 阁楼" in titles
    assert all(node.kind != "location" or node.visibility != VISIBILITY_KP_ONLY for node in extracted.nodes)
