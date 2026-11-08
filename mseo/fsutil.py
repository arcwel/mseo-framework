"""File helpers shared by every module: one atomic writer (JSON or text) and one file hash.

Every derived file (records, sidecars, reports, logs) is written through `write_atomic`: the temp file is opened
with O_CREAT|O_EXCL|O_NOFOLLOW so a symlink planted at the temp or final path is never followed, and the final path
is refused when it is a symlink: a link planted inside a workspace must never redirect an operator-side write to
another customer's file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class UnsafePath(OSError):
    """The path to write is, or would be written through, a symbolic link."""


def write_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to a temp file next to `path` (never through a link), then os.replace it over `path`."""
    path = Path(path)
    if path.is_symlink():
        raise UnsafePath(f"{path} is a symbolic link; refusing to write through it")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink():
        raise UnsafePath(f"{tmp} is a symbolic link; refusing to write through it")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as fh:
                fh.write(text)
        except Exception:
            raise
        if path.is_symlink():  # raced in after the first check
            raise UnsafePath(f"{path} became a symbolic link; refusing to replace it")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json_atomic(path: Path, data: object, *, indent: int | None = 2, ensure_ascii: bool = True) -> None:
    """Write JSON through write_atomic: a crash or a bad value leaves the previous file whole."""
    write_atomic(path, json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n")


def sha256_file(path: Path) -> str | None:
    """Hex SHA-256 of the file's bytes, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
