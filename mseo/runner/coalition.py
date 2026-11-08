"""macOS process coalitions, read through libproc : detached processes must not escape the kill.

launchd starts each job in its own resource coalition, and every process the job forks stays in it, even after
setsid() and a double fork moves it out of the process group and reparents it to launchd. So the coalition, not the
process group, is the set of processes a job owns. On other platforms, or when libproc refuses, these return None or
an empty list and the caller falls back to the group kill alone.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

PROC_PIDCOALITIONINFO = 20  # sys/proc_info.h
RESOURCE = 0  # COALITION_TYPE_RESOURCE: coalition_id[0]
MAX_PIDS = 1 << 16


class _CoalitionInfo(ctypes.Structure):
    _fields_ = [("coalition_id", ctypes.c_uint64 * 2), ("reserved", ctypes.c_uint64 * 3)]


def _lib():
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("proc")
    return ctypes.CDLL(path, use_errno=True) if path else None


_LIBPROC = _lib()


def coalition_id(pid: int) -> int | None:
    if _LIBPROC is None:
        return None
    info = _CoalitionInfo()
    n = _LIBPROC.proc_pidinfo(int(pid), PROC_PIDCOALITIONINFO, ctypes.c_uint64(0), ctypes.byref(info),
                              ctypes.sizeof(info))
    return int(info.coalition_id[RESOURCE]) if n == ctypes.sizeof(info) and info.coalition_id[RESOURCE] else None


def all_pids() -> list[int]:
    if _LIBPROC is None:
        return []
    buf = (ctypes.c_int * MAX_PIDS)()
    n = _LIBPROC.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:max(n, 0)] if p > 0]


def members(cid: int, *, exclude: set[int]) -> list[int]:
    """Every live process in coalition `cid`, except the pids in `exclude` (the wrapper itself)."""
    return [p for p in all_pids() if p not in exclude and coalition_id(p) == cid]
