"""纯 Python snappy raw 格式解码（只做解压方向）。

LevelDB 数据块默认用 snappy 压缩；FVTT 世界包的 ``.ldb`` 实测带压缩块。
本模块只实现 raw snappy 的解码（compressor 场景不存在），零第三方依赖。

格式参考 snappy 的 ``format_description.txt``：

- 流首是一个 uvarint 的未压缩长度；
- 之后是 tag 序列，tag 低 2 位是元素类型，高 6 位是长度/偏移参数：

  - ``0`` literal：高 6 位是 ``len-1``；若该值 >= 60，则实际长度在后续
    1..4 个小端字节里（``len = 值 + 1``）；
  - ``1`` copy1（1 字节偏移）：位 2-4 是 ``len-4``（长度 4..11），
    位 5-7 是偏移的高 3 位（偏移 1..2047）；
  - ``2`` copy2（2 字节小端偏移）：高 6 位整体是 ``len-1``（长度 1..64）；
  - ``3`` copy4（4 字节小端偏移）：同 copy2 的 6 位长度（长度 1..64）。

- copy 的 offset 是回看距离（1 起），允许与当前输出重叠。
"""

from __future__ import annotations

from typing import List


class SnappyFormatError(ValueError):
    """snappy 流结构非法（越界、长度字段超限等）。"""


def read_uvarint(data: bytes, pos: int) -> tuple[int, int]:
    """读一个 uvarint，返回 (value, next_pos)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise SnappyFormatError("uvarint 越界（流被截断）")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise SnappyFormatError("uvarint 超长")


def decompress(data: bytes, *, max_output: int = 512 * 1024 * 1024) -> bytes:
    """解压一段 raw snappy 流。

    ``max_output`` 是未压缩上限（默认 512 MiB），防恶意流的内存放大。
    """
    if not data:
        raise SnappyFormatError("空流")
    expected, pos = read_uvarint(data, 0)
    if expected > max_output:
        raise SnappyFormatError(f"未压缩长度超限：{expected} > {max_output}")

    out = bytearray()
    data_len = len(data)
    while pos < data_len:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            code = (tag >> 2) & 0x3F
            if code < 60:
                length = code + 1
            else:
                extra = code - 59  # 1..4 个长度字节
                if pos + extra > data_len:
                    raise SnappyFormatError("literal 长度字段越界")
                length = int.from_bytes(data[pos : pos + extra], "little") + 1
                pos += extra
            if pos + length > data_len:
                raise SnappyFormatError("literal 数据越界")
            out += data[pos : pos + length]
            pos += length
        else:
            if kind == 1:  # copy, 1 字节偏移
                if pos >= data_len:
                    raise SnappyFormatError("copy1 偏移越界")
                length = 4 + ((tag >> 2) & 0x07)
                offset = ((tag >> 5) & 0x07) << 8 | data[pos]
                pos += 1
            elif kind == 2:  # copy, 2 字节偏移
                if pos + 2 > data_len:
                    raise SnappyFormatError("copy2 偏移越界")
                # 长度占 tag 的全部高 6 位（(len-1)，范围 1..64）。写成
                # ``1 + ((tag >> 2) & 0x07)`` 是经典笔误——那会把长度截成
                # 1..8，输出越来越落后，最终表现为 copy 偏移越界或总长
                # 不匹配。
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[pos : pos + 2], "little")
                pos += 2
            else:  # kind == 3, copy, 4 字节偏移
                if pos + 4 > data_len:
                    raise SnappyFormatError("copy4 偏移越界")
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[pos : pos + 4], "little")
                pos += 4
            if offset == 0 or offset > len(out):
                raise SnappyFormatError(f"copy 偏移非法：{offset}（输出当前 {len(out)} 字节）")
            # 逐字节回拷以支持重叠 copy。
            src = len(out) - offset
            for _ in range(length):
                out.append(out[src])
                src += 1
        if len(out) > max_output:
            raise SnappyFormatError("解压输出超限")

    if len(out) != expected:
        raise SnappyFormatError(f"解压长度不匹配：得到 {len(out)}，声明 {expected}")
    return bytes(out)


def try_decompress(data: bytes) -> bytes:
    """尽力解压；输入不是合法 snappy 流时原样返回。

    LevelDB 块压缩与否由块尾标志决定，这个函数用于调用方已确认压缩的场景。
    """
    try:
        return decompress(data)
    except (SnappyFormatError, ValueError):
        return data


__all__ = ["SnappyFormatError", "decompress", "try_decompress", "read_uvarint"]
