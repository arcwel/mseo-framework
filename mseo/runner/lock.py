"""Locks: one per workspace (never waited for) and one machine-wide browser lock (waited for).

Both are `fcntl.flock` on files under `$MSEO_STATE_DIR/locks/` (default `~/.local/state/mseo/locks/`); the file holds
the holder's pid. The kernel drops a flock when its holder dies, so a crash never leaves a stale lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from pathlib import Path

DEFAULT_STATE = "~/.local/state/mseo"
BROWSER_WAIT_S = 2 * 60 * 60
POLL_S = 5.0


def state_dir() -> Path:
    return Path(os.environ.get("MSEO_STATE_DIR") or DEFAULT_STATE).expanduser()


NAME = re.compile(r"^(browser|ws-[a-z0-9][a-z0-9-]*)$")


def _path(name: str) -> Path:
    if not NAME.match(name):
        raise ValueError(f"not a lock name: {name!r}")
    folder = state_dir() / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.lock"


def holder(name: str) -> int | None:
    """The pid written by the current or last holder of a lock, if any."""
    try:
        text = _path(name).read_text().strip()
    except (OSError, ValueError):
        return None
    return int(text) if text.isdigit() else None


@contextlib.contextmanager
def _flock(name: str, wait_s: float, poll_s: float):
    path = _path(name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())) or poll_s)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def workspace_lock(slug: str):
    """Yields True when held, False at once when another job holds it."""
    return _flock(f"ws-{slug}", 0, POLL_S)


def browser_lock(*, wait_s: float = BROWSER_WAIT_S, poll_s: float = POLL_S):
    """Yields True when held, False after waiting wait_s."""
    return _flock("browser", wait_s, poll_s)
