"""fvtt-world-import 读取层：真实 FVTT 世界包（LevelDB + snappy）端到端校验。

样例是奥法地图发布的 FVTT v13 世界包（docs/样本/）：``.ldb`` 尾部 magic 被换成
变体、块用 snappy 压缩、journal/pages 走 ``!journal.pages!<journalId>.<pageId>``
子键、部分数据只在 ``.log``（WriteBatch）里。测试覆盖：

- snappy raw 解码：copy2/copy4 的长度占 tag 高 6 位（(len-1)，1..64）——
  读取层曾把长度截成 3 位导致所有 index 块解压失步；
- WriteBatch（``.log``）解析与删除生效：sixianyuchanmian 的 5 条消息在
  ``000019.log`` 里被 seq 6..10 删除，导出不得复活 ``.ldb`` 里 seq 1..5 的它们；
- 空库（sixianyuchanmian journal）不报错；
- heroes 的场景/token/journal 结构与两包 actors 文档形状。

样例 zip 缺失时 pytest.skip（包不入库）。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "data" / "plugins" / "fvtt-world-import"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fvtt_world_import import dump as dump_cli  # noqa: E402
from fvtt_world_import.leveldb import (  # noqa: E402
    DbLimits,
    LevelDbReader,
    _iter_write_batch,
)
from fvtt_world_import.snappy import SnappyFormatError, decompress  # noqa: E402

HEROES_ZIP = REPO_ROOT / "docs" / "样本" / "heroes-to-yuanshan-0.1.6.zip"
SIXIAN_ZIP = REPO_ROOT / "docs" / "样本" / "sixianyuchanmian-1.0.1.zip"


# -- 装置 -----------------------------------------------------------------


def _extract_world(zip_path: Path, factory: pytest.TempPathFactory) -> Path:
    """把世界包解到临时目录，返回世界根（含 world.json）。"""
    if not zip_path.exists():
        pytest.skip(f"样例包缺失：{zip_path}")
    world = factory.mktemp(zip_path.stem)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(world)
    return world


@pytest.fixture(scope="module")
def heroes_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _extract_world(HEROES_ZIP, tmp_path_factory)


@pytest.fixture(scope="module")
def sixian_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _extract_world(SIXIAN_ZIP, tmp_path_factory)


def _read_db(world: Path, collection: str) -> dict[str, list[dict]]:
    """读 world/data/<collection>/ 一个 LevelDB 库，返回 {集合名: [文档]}。"""
    db_dir = world / "data" / collection
    assert db_dir.is_dir(), f"包里没有 data/{collection}"
    return LevelDbReader(str(db_dir), limits=DbLimits()).dump()


# -- snappy 解码（不需要样例包，先钉死失步根因） ---------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def test_snappy_copy2_length_spans_six_bits():
    """tag=0x46：高 6 位 (len-1)=17 → copy 长度 18；按 3 位算会得 2 并失步。"""
    stream = _varint(20) + bytes([0x04]) + b"01" + bytes([0x46, 0x02, 0x00])
    assert decompress(stream) == b"01" * 10


def test_snappy_copy4_length_spans_six_bits():
    """tag=0x23：高 6 位 (len-1)=8 → copy 长度 9，4 字节小端偏移。"""
    stream = _varint(13) + bytes([0x0C]) + b"abcd" + bytes([0x23]) + (4).to_bytes(4, "little")
    assert decompress(stream) == b"abcd" + b"abcdabcda"


def test_snappy_literal_extended_length_and_rejects_broken_stream():
    # code=60：长度在后续 1 个小端字节里（len = 值 + 1）。
    payload = b"xyz" * 20
    stream = _varint(60) + bytes([60 << 2]) + (59).to_bytes(1, "little") + payload
    assert decompress(stream) == payload
    # copy 偏移 0 非法。
    with pytest.raises(SnappyFormatError):
        decompress(_varint(8) + bytes([0x0C]) + b"abcd" + bytes([0x23, 0, 0, 0, 0]))
    # 声明 8 字节只解出 4：总长不匹配。
    with pytest.raises(SnappyFormatError):
        decompress(_varint(8) + bytes([0x0C]) + b"abcd")


# -- WriteBatch（.log 记录） ----------------------------------------------


def test_write_batch_value_and_deletion_entries():
    """seq 在头 8 字节、count 紧随其后；条目 seq 依次 +1，type 0 是删除。"""
    payload = (
        (5).to_bytes(8, "little")
        + (2).to_bytes(4, "little")
        + b"\x01" + _varint(4) + b"!a!x" + _varint(4) + b"val1"
        + b"\x00" + _varint(4) + b"!a!y"
    )
    assert list(_iter_write_batch(payload)) == [
        (b"!a!x", 5, 1, b"val1"),
        (b"!a!y", 6, 0, None),
    ]


def test_write_batch_rejects_unknown_entry_type():
    payload = (1).to_bytes(8, "little") + (1).to_bytes(4, "little") + b"\x07" + _varint(1) + b"k"
    with pytest.raises(ValueError):
        list(_iter_write_batch(payload))


# -- heroes：场景 / token / journal ---------------------------------------


def test_heroes_scenes_grid_and_tokens(heroes_world: Path):
    data = _read_db(heroes_world, "scenes")
    scenes = data["scenes"]
    assert scenes

    for doc in scenes:
        grid = doc["grid"]
        assert grid["type"] == 1
        assert isinstance(grid["size"], int) and grid["size"] > 0
    # 标准方格 150px；heroes 另有两张大幅地图场景（192/282），断言“存在”而非“全部”。
    assert any(doc["grid"]["size"] == 150 for doc in scenes)

    # v13 场景文档的 tokens 是 token id 列表，token 本体在 scenes.tokens 子集合。
    with_tokens = [doc for doc in scenes if doc.get("tokens")]
    assert with_tokens
    token_docs = {doc["_id"]: doc for doc in data.get("scenes.tokens", [])}
    for scene in with_tokens:
        for token_id in scene["tokens"]:
            token = token_docs[f"{scene['_id']}.{token_id}"]
            assert "disposition" in token


def test_heroes_journal_entries_and_page_aggregation(heroes_world: Path):
    data = _read_db(heroes_world, "journal")
    entries = data.get("journal", [])
    pages = data.get("journal.pages", [])
    # 这个包的 journal 库共 9 条记录：2 个 JournalEntry + 7 个页面。
    assert len(entries) + len(pages) >= 9
    assert entries

    groups: dict[str, list[str]] = {}
    for page in pages:
        journal_id, _, page_id = page["_id"].partition(".")
        groups.setdefault(journal_id, []).append(page_id)
    entry_ids = {entry["_id"] for entry in entries}
    # 页面都能按 journalId 聚合，且每个 JournalEntry 至少挂一页。
    assert set(groups) <= entry_ids
    assert set(groups) == entry_ids
    assert all(len(page_ids) >= 1 for page_ids in groups.values())


@pytest.mark.parametrize("world_fixture", ["heroes_world", "sixian_world"])
def test_actor_documents_have_system(world_fixture: str, request: pytest.FixtureRequest):
    world = request.getfixturevalue(world_fixture)
    data = _read_db(world, "actors")
    actors = data["actors"]
    assert actors
    for doc in actors:
        assert isinstance(doc.get("system"), dict)


# -- sixianyuchanmian -----------------------------------------------------


def test_sixian_scenes_standard_grid(sixian_world: Path):
    data = _read_db(sixian_world, "scenes")
    scenes = data["scenes"]
    assert len(scenes) == 6
    for doc in scenes:
        assert doc["grid"]["size"] == 150
        assert doc["grid"]["type"] == 1


def test_sixian_empty_journal_db_is_ok(sixian_world: Path):
    """journal 是空库（只有 CURRENT/MANIFEST/空 log），读取不得报错。"""
    reader = LevelDbReader(str(sixian_world / "data" / "journal"))
    assert reader.collections() == []
    assert reader.dump() == {}


def test_sixian_log_deletions_override_table_values(sixian_world: Path):
    """.ldb 里有 5 条消息（seq 1..5），000019.log 的 WriteBatch 以 seq 6..10
    删除了同一批键——.log 删除必须生效，导出不得复活已删消息。"""
    reader = LevelDbReader(str(sixian_world / "data" / "messages"))
    assert reader.collections() == []
    assert reader.dump() == {}


# -- 导出 CLI -------------------------------------------------------------


def _assert_no_collection_errors(summary: dict) -> None:
    for name, entry in summary.items():
        assert not (isinstance(entry, dict) and "error" in entry), f"{name}: {entry}"


def test_dump_cli_exports_all_heroes_collections(tmp_path: Path):
    if not HEROES_ZIP.exists():
        pytest.skip(f"样例包缺失：{HEROES_ZIP}")
    out_dir = tmp_path / "out"
    assert dump_cli.main([str(HEROES_ZIP), str(out_dir)]) == 0
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    _assert_no_collection_errors(summary)
    assert summary["scenes"]["count"] == 4
    assert summary["journal"]["count"] == 2
    assert summary["journal.pages"]["count"] == 7
    assert summary["actors"]["count"] == 23


def test_dump_cli_exports_all_sixian_collections(tmp_path: Path):
    if not SIXIAN_ZIP.exists():
        pytest.skip(f"样例包缺失：{SIXIAN_ZIP}")
    out_dir = tmp_path / "out"
    assert dump_cli.main([str(SIXIAN_ZIP), str(out_dir)]) == 0
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    _assert_no_collection_errors(summary)
    assert summary["scenes"]["count"] == 6
    # messages 被 .log 删除、journal 是空库：都不该出现在导出里。
    assert "messages" not in summary
    assert "journal" not in summary
