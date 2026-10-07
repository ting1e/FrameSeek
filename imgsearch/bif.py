from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAGIC = b"\x89BIF\r\n\x1a\n"
MAX_FRAMES = 5_000_000


class BifError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    index: int
    time_ms: int
    offset: int
    length: int


def parse_stream(stream: BinaryIO, size: int) -> list[Frame]:
    header = stream.read(64)
    if len(header) != 64 or header[:8] != MAGIC:
        raise BifError("Invalid or truncated BIF header")
    version, count, multiplier = struct.unpack_from("<III", header, 8)
    if version != 0:
        raise BifError(f"Unsupported BIF version {version}")
    table_end = 64 + (count + 1) * 8
    if count > MAX_FRAMES or table_end > size:
        raise BifError("Invalid frame count/index boundary")
    table = stream.read((count + 1) * 8)
    if len(table) != (count + 1) * 8:
        raise BifError("Truncated BIF index")
    entries = list(struct.iter_unpack("<II", table))
    if entries[-1][0] != 0xFFFFFFFF or entries[-1][1] > size:
        raise BifError("Invalid end-of-data sentinel")
    result: list[Frame] = []
    previous_time = -1
    for index, (timestamp, offset) in enumerate(entries[:-1]):
        end = entries[index + 1][1]
        if timestamp == 0xFFFFFFFF or timestamp < previous_time:
            raise BifError("Non-monotonic frame timestamp")
        if offset < table_end or end <= offset or end > size:
            raise BifError("Invalid JPEG offset/length")
        result.append(Frame(index, timestamp * (multiplier or 1000), offset, end - offset))
        previous_time = timestamp
    return result


def parse(path: Path) -> list[Frame]:
    with path.open("rb") as stream:
        return parse_stream(stream, path.stat().st_size)


def read_frame(path: Path, offset: int, length: int) -> bytes:
    if offset < 64 or length <= 0 or length > 64 * 1024 * 1024:
        raise BifError("Invalid JPEG span")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    if len(data) != length:
        raise BifError("JPEG span is truncated")
    return data
