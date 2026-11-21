"""只读 LevelDB（FVTT v10+ 世界包数据目录）。

FVTT 世界包的 ``data/<collection>/`` 是一个 LevelDB 库：``CURRENT`` 指向
``MANIFEST-*``（VersionSet 日志），活动数据在 ``*.ldb`` 表文件与当前 ``*.log``
里。本模块实现只读遍历：不写、不 compaction、不加锁，适合导入器一次性消费。

实现范围与取舍：

- 完整解析 MANIFEST 得到活动表文件集合（防止把已被 compaction 淘汰的旧表
  误当新数据）；
- 同一 user_key 出现多个版本时按 internal key 的 (seq, type) 取最新，删除
  标记生效；
- CRC 校验默认关闭（纯 Python crc32c 对大块慢一个数量级），可开；
- 设文档数 / 键值长度上限，超限报可读错误——导入器面对不可信 zip。

键格式（FVTT 世界库实测）：``!<collection>!<docId>``，值是文档 JSON；
journal 的页是独立键 ``!journal.pages!<journalId>.<pageId>``。
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from typing import Iterator

from fvtt_world_import.snappy import SnappyFormatError, decompress, read_uvarint

TABLE_MAGIC = 0xDB4775248E807249
# 奥法地图发布的 FVTT 世界包里，.ldb 尾部 magic 被替换成这个变体（其余
# 表结构与标准 LevelDB 完全一致：footer 句柄逐字节可解）。为了能同时读
# 官方 LevelDB 与这类导出包，两个 magic 都放行。
TABLE_MAGIC_VARIANT = 0xDB4775248B80FB57
TABLE_MAGICS = frozenset({TABLE_MAGIC, TABLE_MAGIC_VARIANT})
BLOCK_TYPE_NONE = 0
BLOCK_TYPE_SNAPPY = 1
LOG_BLOCK_SIZE = 32768
LOG_HEADER_SIZE = 7

# VersionEdit tag（LevelDB dbformat.cc）。
_TAG_COMPARATOR = 1
_TAG_LOG_NUMBER = 2
_TAG_NEXT_FILE_NUMBER = 3
_TAG_LAST_SEQUENCE = 4
_TAG_COMPACT_POINTER = 5
_TAG_DELETED_FILE = 6
_TAG_NEW_FILE = 7
_TAG_PREV_LOG_NUMBER = 9

_VALUE_TYPE_DELETION = 0
_VALUE_TYPE_VALUE = 1


class LevelDbError(ValueError):
    """库结构非法或超限。"""


@dataclass
class DbLimits:
    """只读防御上限（防解析型 DoS）。"""

    max_docs: int = 200_000
    max_key_bytes: int = 4096
    max_value_bytes: int = 64 * 1024 * 1024


def _crc32c(data: bytes, seed: int = 0) -> int:
    """表驱动 crc32c（Castagnoli），仅用于可选校验。"""
    poly = 0x82F63B78
    table = _crc32c._table  # type: ignore[attr-defined]
    crc = seed ^ 0xFFFFFFFF
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _build_crc32c_table() -> list[int]:
    poly = 0x82F63B78
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ (poly if crc & 1 else 0)
        table.append(crc)
    return table


_crc32c._table = _build_crc32c_table()  # type: ignore[attr-defined]


def _mask_crc(crc: int) -> int:
    return (((crc >> 15) | ((crc << 17) & 0xFFFFFFFF)) + 0xA282EAD8) & 0xFFFFFFFF


class _LogRecordReader:
    """LevelDB 日志（MANIFEST 与 .log 共用）的记录迭代器。"""

    def __init__(self, path: str, *, verify_crc: bool = False, label: str = ""):
        with open(path, "rb") as handle:
            self._data = handle.read()
        self._verify = verify_crc
        self._label = label or os.path.basename(path)

    def records(self) -> Iterator[bytes]:
        data = self._data
        pos = 0
        length = len(data)
        pending: list[bytes] | None = None
        while pos < length:
            remaining = length - pos
            if remaining < LOG_HEADER_SIZE:
                # 尾部不足一个头：写入端的填充，忽略。
                break
            record_start = pos
            stored_crc = struct.unpack_from("<I", data, record_start)[0]
            record_len = struct.unpack_from("<H", data, record_start + 4)[0]
            record_type = data[record_start + 6]
            payload_start = record_start + LOG_HEADER_SIZE
            if payload_start + record_len > length:
                raise LevelDbError(f"{self._label}：日志记录越界")
            payload = data[payload_start : payload_start + record_len]
            pos = payload_start + record_len
            if self._verify:
                covered = data[record_start + 6 : payload_start + record_len]
                actual = _mask_crc(_crc32c(covered))
                if actual != stored_crc:
                    raise LevelDbError(f"{self._label}：日志记录 CRC 不匹配")
            if record_type == 0:  # kZeroType：预分配的空区，跳过
                continue
            if record_type == 1:  # FULL
                yield payload
            elif record_type == 2:  # FIRST
                pending = [payload]
            elif record_type == 3:  # MIDDLE
                if pending is None:
                    raise LevelDbError(f"{self._label}：MIDDLE 记录缺 FIRST")
                pending.append(payload)
            elif record_type == 4:  # LAST
                if pending is None:
                    raise LevelDbError(f"{self._label}：LAST 记录缺 FIRST")
                pending.append(payload)
                yield b"".join(pending)
                pending = None
            else:
                raise LevelDbError(f"{self._label}：未知日志记录类型 {record_type}")


def _read_len_prefixed(data: bytes, pos: int) -> tuple[bytes, int]:
    size, pos = read_uvarint(data, pos)
    if pos + size > len(data):
        raise LevelDbError("长度前缀字段越界")
    return data[pos : pos + size], pos + size


def _iter_write_batch(payload: bytes) -> Iterator[tuple[bytes, int, int, bytes | None]]:
    """解析一条 WriteBatch，yield (user_key, seq, vtype, value)。

    ``.log`` 文件里每条记录的 payload 是一个 WriteBatch（write_batch.cc）::

        seq(小端 8B) + count(小端 4B) + count 个条目
        条目: type(1B) + varstring key [+ varstring value (type==1)]

    第 i 个条目的 internal seq 是 ``seq + i``。type 0 是删除（value 为 None）。
    """
    if len(payload) < 12:
        raise LevelDbError(f"WriteBatch 头不完整（{len(payload)} 字节）")
    seq = struct.unpack_from("<Q", payload, 0)[0]
    count = struct.unpack_from("<I", payload, 8)[0]
    pos = 12
    for i in range(count):
        if pos >= len(payload):
            raise LevelDbError("WriteBatch 条目数与内容不符")
        vtype = payload[pos]
        pos += 1
        key, pos = _read_len_prefixed(payload, pos)
        if vtype == _VALUE_TYPE_VALUE:
            value, pos = _read_len_prefixed(payload, pos)
        elif vtype == _VALUE_TYPE_DELETION:
            value = None
        else:
            raise LevelDbError(f"WriteBatch 未知条目类型 {vtype}")
        yield key, seq + i, vtype, value


@dataclass
class _VersionState:
    """按 MANIFEST 顺序应用 VersionEdit 后的活动文件集合。"""

    live_files: dict[int, dict[int, int]]  # level -> {file number: size}
    log_numbers: list[int]


def _parse_manifest(path: str, *, verify_crc: bool = False) -> _VersionState:
    state = _VersionState(live_files={}, log_numbers=[])
    for payload in _LogRecordReader(path, verify_crc=verify_crc).records():
        pos = 0
        while pos < len(payload):
            tag, pos = read_uvarint(payload, pos)
            if tag == _TAG_COMPARATOR:
                _, pos = _read_len_prefixed(payload, pos)
            elif tag in (_TAG_LOG_NUMBER, _TAG_PREV_LOG_NUMBER, _TAG_NEXT_FILE_NUMBER, _TAG_LAST_SEQUENCE):
                value, pos = read_uvarint(payload, pos)
                if tag == _TAG_LOG_NUMBER:
                    state.log_numbers.append(value)
            elif tag == _TAG_COMPACT_POINTER:
                _, pos = read_uvarint(payload, pos)
                _, pos = _read_len_prefixed(payload, pos)
            elif tag == _TAG_DELETED_FILE:
                level, pos = read_uvarint(payload, pos)
                number, pos = read_uvarint(payload, pos)
                state.live_files.get(level, {}).pop(number, None)
            elif tag == _TAG_NEW_FILE:
                level, pos = read_uvarint(payload, pos)
                number, pos = read_uvarint(payload, pos)
                size, pos = read_uvarint(payload, pos)
                _, pos = _read_len_prefixed(payload, pos)  # smallest key
                _, pos = _read_len_prefixed(payload, pos)  # largest key
                state.live_files.setdefault(level, {})[number] = size
            else:
                # 未知 tag：保守终止该记录（越界解释比漏读更危险）。
                break
    return state


class _TableFile:
    """只读遍历一个 ``.ldb`` 表文件。"""

    def __init__(self, path: str, *, verify_crc: bool = False):
        with open(path, "rb") as handle:
            self._data = handle.read()
        self._verify = verify_crc
        self._name = os.path.basename(path)
        if len(self._data) < 48:
            raise LevelDbError(f"{self._name}：表文件过小")
        footer = self._data[-48:]
        if struct.unpack_from("<Q", footer, 40)[0] not in TABLE_MAGICS:
            raise LevelDbError(f"{self._name}：表文件 magic 不匹配")

    def _block(self, offset: int, size: int) -> bytes:
        data = self._data
        if offset + size + 5 > len(data):
            raise LevelDbError(f"{self._name}：块句柄越界")
        raw = data[offset : offset + size]
        block_type = data[offset + size]
        if self._verify:
            stored_crc = struct.unpack_from("<I", data, offset + size + 1)[0]
            actual = _mask_crc(_crc32c(raw + bytes([block_type])))
            if actual != stored_crc:
                raise LevelDbError(f"{self._name}：块 CRC 不匹配（offset={offset}）")
        if block_type == BLOCK_TYPE_NONE:
            return raw
        if block_type == BLOCK_TYPE_SNAPPY:
            try:
                return decompress(raw)
            except SnappyFormatError as exc:
                raise LevelDbError(f"{self._name}：snappy 解压失败（offset={offset}）：{exc}") from exc
        raise LevelDbError(f"{self._name}：未知块压缩类型 {block_type}")

    def _block_entries(self, block: bytes) -> Iterator[tuple[bytes, bytes]]:
        if len(block) < 8:
            return
        num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
        if num_restarts == 0 or len(block) < 4 + 4 * num_restarts:
            raise LevelDbError(f"{self._name}：restart 数组非法")
        entries_end = len(block) - 4 - 4 * num_restarts
        restarts = struct.unpack_from(f"<{num_restarts}I", block, entries_end + 4)
        valid_starts = set(restarts)
        pos = 0
        prev_key = b""
        while pos < entries_end:
            entry_start = pos
            shared, pos = read_uvarint(block, pos)
            non_shared, pos = read_uvarint(block, pos)
            value_len, pos = read_uvarint(block, pos)
            if shared > len(prev_key) or pos + non_shared + value_len > entries_end:
                raise LevelDbError(f"{self._name}：entry 头非法")
            key = prev_key[:shared] + block[pos : pos + non_shared]
            pos += non_shared
            value = block[pos : pos + value_len]
            pos += value_len
            if entry_start in valid_starts:
                prev_key = key  # restart 点重新建立前缀基线
            else:
                prev_key = key
            yield key, value

    def entries(self) -> Iterator[tuple[bytes, bytes]]:
        footer = self._data[-48:]
        pos = 0
        _, pos = read_uvarint(footer, pos)  # metaindex offset
        _, pos = read_uvarint(footer, pos)  # metaindex size
        index_offset, pos = read_uvarint(footer, pos)
        index_size, pos = read_uvarint(footer, pos)
        index_block = self._block(index_offset, index_size)
        for _, handle in self._block_entries(index_block):
            hpos = 0
            block_offset, hpos = read_uvarint(handle, hpos)
            block_size, hpos = read_uvarint(handle, hpos)
            data_block = self._block(block_offset, block_size)
            yield from self._block_entries(data_block)


def _split_internal_key(key: bytes) -> tuple[bytes, int, int]:
    """internal key = user_key + 8 字节 (seq<<8 | type)，小端。"""
    if len(key) < 8:
        raise LevelDbError(f"internal key 过短：{len(key)}")
    tag = struct.unpack_from("<Q", key, len(key) - 8)[0]
    return key[:-8], tag >> 8, tag & 0xFF


class LevelDbReader:
    """一个 FVTT 世界集合库（``data/<collection>/`` 目录）的只读入口。"""

    def __init__(self, db_dir: str, *, limits: DbLimits | None = None, verify_crc: bool = False):
        self._dir = db_dir
        self._limits = limits or DbLimits()
        self._verify = verify_crc
        if not os.path.isdir(db_dir):
            raise LevelDbError(f"目录不存在：{db_dir}")

    # -- 底层遍历 ---------------------------------------------------------

    def iter_entries(self) -> Iterator[tuple[bytes, bytes]]:
        """全库 (user_key, value)，按最新版本去重、删除生效。

        数据源是 MANIFEST 认定的活动表文件 + 非空 ``*.log``（每条记录是一个
        WriteBatch，条目 seq = 批头 seq + 序号）。user_key 保持原始字节（含
        ``!collection!`` 前缀），value 为原始字节。
        """
        state = self._version_state()
        latest: dict[bytes, tuple[int, int, bytes | None]] = {}
        doc_count = 0

        def consider(user_key: bytes, seq: int, vtype: int, value: bytes | None) -> None:
            nonlocal doc_count
            if len(user_key) > self._limits.max_key_bytes:
                raise LevelDbError(f"user_key 超限：{len(user_key)} 字节")
            if value is not None and len(value) > self._limits.max_value_bytes:
                raise LevelDbError(f"文档超限：user_key={user_key[:64]!r} 值 {len(value)} 字节")
            current = latest.get(user_key)
            if current is None or (seq, vtype) > (current[0], current[1]):
                latest[user_key] = (seq, vtype, value)
                doc_count += 1
                if doc_count > self._limits.max_docs:
                    raise LevelDbError(f"文档数超限：>{self._limits.max_docs}")

        for level_files in state.live_files.values():
            for number in sorted(level_files):
                path = self._table_path(number)
                if path:
                    for key, value in _TableFile(path, verify_crc=self._verify).entries():
                        user_key, seq, vtype = _split_internal_key(key)
                        consider(user_key, seq, vtype, value if vtype == _VALUE_TYPE_VALUE else None)
        for path in self._log_paths(state):
            for payload in _LogRecordReader(path, verify_crc=self._verify, label=path).records():
                for user_key, seq, vtype, value in _iter_write_batch(payload):
                    consider(user_key, seq, vtype, value)

        for user_key, (_, vtype, value) in latest.items():
            if vtype == _VALUE_TYPE_VALUE and value is not None:
                yield user_key, value

    def _version_state(self) -> _VersionState:
        current_path = os.path.join(self._dir, "CURRENT")
        manifest: str | None = None
        if os.path.isfile(current_path):
            with open(current_path, "rb") as handle:
                manifest_name = handle.read(64).decode("utf-8", errors="strict").strip()
            candidate = os.path.join(self._dir, manifest_name)
            if os.path.isfile(candidate):
                manifest = candidate
        if manifest is None:
            # CURRENT 缺失或指向不存在的 MANIFEST：退化为读全部表文件。
            state = _VersionState(live_files={}, log_numbers=[])
            for level in (0,):
                files: dict[int, int] = {}
                for name in os.listdir(self._dir):
                    base, ext = os.path.splitext(name)
                    if ext.lower() in (".ldb", ".sst") and base.isdigit():
                        files[int(base)] = os.path.getsize(os.path.join(self._dir, name))
                state.live_files[level] = files
            return state
        return _parse_manifest(manifest, verify_crc=self._verify)

    def _table_path(self, number: int) -> str | None:
        for ext in (".ldb", ".sst"):
            path = os.path.join(self._dir, f"{number:06d}{ext}")
            if os.path.isfile(path):
                return path
        return None

    def _log_paths(self, state: _VersionState) -> list[str]:
        paths: list[str] = []
        for name in sorted(os.listdir(self._dir)):
            base, ext = os.path.splitext(name)
            if ext.lower() == ".log" and base.isdigit():
                path = os.path.join(self._dir, name)
                if os.path.getsize(path) > 0:
                    paths.append(path)
        return paths

    # -- 集合级 API -------------------------------------------------------

    @staticmethod
    def split_key(user_key: bytes) -> tuple[str, str] | None:
        """``!collection!docId`` -> (collection, doc_id)；不合法返回 None。"""
        if not user_key.startswith(b"!"):
            return None
        try:
            text = user_key.decode("utf-8")
        except UnicodeDecodeError:
            return None
        second = text.find("!", 1)
        if second < 0:
            return None
        return text[1:second], text[second + 1 :]

    def collections(self) -> list[str]:
        seen: set[str] = set()
        for user_key, _ in self.iter_entries():
            parts = self.split_key(user_key)
            if parts:
                seen.add(parts[0])
        return sorted(seen)

    def iter_collection(self, name: str) -> Iterator[tuple[str, dict]]:
        """迭代一个集合：yield (doc_id, doc_json)。值必须是 JSON 对象。"""
        prefix = f"!{name}!".encode("utf-8")
        for user_key, value in self.iter_entries():
            if not user_key.startswith(prefix):
                continue
            parts = self.split_key(user_key)
            if parts is None or parts[0] != name:
                continue
            try:
                doc = json.loads(value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LevelDbError(f"集合 {name} 文档 {parts[1]} 不是合法 JSON：{exc}") from exc
            if not isinstance(doc, dict):
                continue
            yield parts[1], doc

    def dump(self) -> dict[str, list[dict]]:
        """全库导出：{collection: [doc, ...]}（doc 附加 _id）。"""
        result: dict[str, list[dict]] = {}
        for name in self.collections():
            bucket = result.setdefault(name, [])
            for doc_id, doc in self.iter_collection(name):
                enriched = dict(doc)
                enriched["_id"] = doc_id
                bucket.append(enriched)
        return result


__all__ = ["DbLimits", "LevelDbError", "LevelDbReader"]
