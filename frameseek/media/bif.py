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
    header = stream.read(16)
    if len(header) != 16 or header[:8] != MAGIC:
        raise BifError("Invalid or truncated BIF header")
    version, count = struct.unpack_from("<II", header, 8)
    if version != 0:
        raise BifError(f"Unsupported BIF version {version}")
    if count > MAX_FRAMES:
        raise BifError("Invalid frame count/index boundary")
    probe = stream.read(8)
    # Compact BIF: 16-byte header, millisecond timestamps, no sentinel.
    compact = count > 0 and len(probe) == 8 and struct.unpack('<II', probe) == (0, 16 + count * 8)
    table_start = 16 if compact else 64
    table_end = table_start + (count if compact else count + 1) * 8
    if table_end > size:
        raise BifError("Invalid frame count/index boundary")
    multiplier = 1 if compact else (struct.unpack('<I', probe[:4])[0] if len(probe) == 8 else 1000)
    stream.seek(table_start)
    if compact:
        table = stream.read(count * 8)
        entries = list(struct.iter_unpack('<II', table)) + [(0xFFFFFFFF, size)]
    else:
        entries = _standard_entries(stream, count, size)
    return _frames(entries, table_end, size, multiplier)


def _standard_entries(stream, count, size):
    table = stream.read((count + 1) * 8)
    if len(table) != (count + 1) * 8:
        raise BifError("Truncated BIF index")
    entries = list(struct.iter_unpack("<II", table))
    if entries[-1][0] != 0xFFFFFFFF or entries[-1][1] > size:
        raise BifError("Invalid end-of-data sentinel")
    return entries


def _frames(entries, table_end, size, multiplier):
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
    if offset < 16 or length <= 0 or length > 64 * 1024 * 1024:
        raise BifError("Invalid JPEG span")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    if len(data) != length:
        raise BifError("JPEG span is truncated")
    return data
