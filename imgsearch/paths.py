from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def canonical_mtime(stat) -> int:
    # NTFS stores 100ns ticks; use the same resolution on Linux for NAS mapping.
    return stat.st_mtime_ns // 100 * 100


def escaped_part(part: str) -> str:
    result = "".join(f"%{ord(c):02X}" if c in '%<>:"/\\|?*' or ord(c) < 32 else c for c in part)
    if RESERVED.match(result):
        result = f"%{ord(result[0]):02X}" + result[1:]
    while result.endswith((".", " ")):
        result = result[:-1] + f"%{ord(result[-1]):02X}"
    return result


def media_path(root: Path, relative: str, escaped: bool = False) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in {"..", "."} or "\\" in p for p in parts):
        raise ValueError("Invalid relative media path")
    candidate = root.joinpath(*(escaped_part(p) if escaped else p for p in parts))
    base = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("Media path escapes source root")
    return resolved


def original_relative(root: Path, path: Path, escaped: bool) -> str:
    parts = path.relative_to(root).parts
    return "/".join(unquote(p) if escaped else p for p in parts)
