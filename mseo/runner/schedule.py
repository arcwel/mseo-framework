"""`mseo schedule generate`: one launchd LaunchAgent per workspace from customer.json, plus the
operator-level watchdog job.

The slot is `schedule.weekly` ({weekday, time}, default Mon 06:00) in the customer's `timezone`, moved later by one
gap per workspace before it in slug order, so two jobs never want the browser at the same time. Only folder names
under the workspaces root are read for the order; no other workspace's files are opened. launchd fires in the
machine's zone, so the slot is converted to it (a zone pair with different DST dates drifts by an hour for a few
weeks a year; the outcome's `scheduled_for` is computed from the customer zone, so the watchdog stays right).
"""

from __future__ import annotations

import datetime as dt
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from mseo import workspace as workspace_mod
from mseo.runner import lock

GAP_MIN = 90  # minutes between two workspaces' jobs; wider than the longest measured job
DEFAULT_SLOT = {"weekday": "Mon", "time": "06:00"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
REFERENCE_MONDAY = dt.date(2024, 1, 1)  # any Monday: slot() is a weekday and a time, placed in this week
WATCHDOG_TIMES = ((9, 0), (21, 0))
PATH = "~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PASS_ENV = ("MSEO_WORKSPACES", "MSEO_STATE_DIR", "MSEO_OPERATOR_DENY")


def agents_dir() -> Path:
    return Path(os.environ.get("MSEO_LAUNCH_AGENTS") or "~/Library/LaunchAgents").expanduser()


def _order(slug: str) -> int:
    root = workspace_mod.workspaces_root()
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    return names.index(slug) if slug in names else 0


def slot(ws) -> dt.datetime:
    """The staggered slot as a datetime in the customer zone, placed in a fixed reference week."""
    weekly = (ws.config.get("schedule") or {}).get("weekly") or DEFAULT_SLOT
    hh, mm = (int(x) for x in weekly["time"].split(":"))
    day = REFERENCE_MONDAY + dt.timedelta(days=WEEKDAYS.index(weekly["weekday"]))
    base = dt.datetime.combine(day, dt.time(hh, mm), tzinfo=ZoneInfo(ws.config["timezone"]))
    return base + dt.timedelta(minutes=GAP_MIN * _order(ws.slug))


def _occurrence(ws, now: dt.datetime, weeks_back: int) -> dt.datetime:
    s = slot(ws)
    tz = ZoneInfo(ws.config["timezone"])
    local_now = now.astimezone(tz)
    monday = local_now.date() - dt.timedelta(days=local_now.weekday()) - dt.timedelta(weeks=weeks_back)
    offset = s.replace(tzinfo=None) - dt.datetime.combine(REFERENCE_MONDAY, dt.time())
    return (dt.datetime.combine(monday, dt.time()) + offset).replace(tzinfo=tz)


def last_slot(ws, now: dt.datetime) -> dt.datetime:
    """The latest slot at or before now: what a job started now was scheduled for."""
    got = _occurrence(ws, now, 0)
    return got if got <= now else _occurrence(ws, now, 1)


def next_slot(ws, now: dt.datetime) -> dt.datetime:
    got = _occurrence(ws, now, 0)
    return got if got > now else _occurrence(ws, now, -1)


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def label(slug: str) -> str:
    if not SLUG.match(slug):
        raise ValueError(f"not a workspace slug: {slug!r}")
    return f"com.mseo.{slug}.weekly"


def _env() -> dict:
    env = {"PATH": os.path.expanduser(PATH), "PYTHONSAFEPATH": "1"}
    env.update({k: os.environ[k] for k in PASS_ENV if os.environ.get(k)})
    if "MSEO_WORKSPACES" not in env:
        env["MSEO_WORKSPACES"] = str(workspace_mod.workspaces_root())
    return env


def _logs(name: str) -> dict:
    folder = lock.state_dir() / "logs"
    return {"StandardOutPath": str(folder / f"{name}.out.log"), "StandardErrorPath": str(folder / f"{name}.err.log")}


def weekly_plist(ws, *, local_tz: dt.tzinfo | None = None, now: dt.datetime | None = None) -> dict:
    local_tz = local_tz or dt.datetime.now().astimezone().tzinfo
    fire = next_slot(ws, now or dt.datetime.now(dt.timezone.utc)).astimezone(local_tz)
    return {"Label": label(ws.slug),
            "ProgramArguments": [sys.executable, "-P", "-m", "mseo", "run", "weekly", "--workspace", ws.slug],
            "WorkingDirectory": str(lock.state_dir()),  # never the agent-writable workspace
            "StartCalendarInterval": {"Weekday": (fire.weekday() + 1) % 7, "Hour": fire.hour, "Minute": fire.minute},
            "EnvironmentVariables": _env(), "ProcessType": "Background", **_logs(ws.slug)}


def watchdog_plist() -> dict:
    return {"Label": "com.mseo.watchdog", "ProgramArguments": [sys.executable, "-P", "-m", "mseo", "watchdog"],
            "WorkingDirectory": str(lock.state_dir()),
            "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in WATCHDOG_TIMES],
            "EnvironmentVariables": _env(), "ProcessType": "Background", **_logs("watchdog")}


def launchctl(*args: str) -> int:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True).returncode


def apply(plist: dict, *, mode: str) -> tuple[Path, str]:
    """mode: dry-run (print only), write (file only), load (write + bootstrap) or unload (bootout + remove)."""
    path = agents_dir() / f"{plist['Label']}.plist"
    domain = f"gui/{os.getuid()}"
    if mode == "dry-run":
        return path, plistlib.dumps(plist).decode()
    if mode == "unload":
        launchctl("bootout", f"{domain}/{plist['Label']}")
        path.unlink(missing_ok=True)
        return path, f"unloaded {plist['Label']} and removed {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(plist["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    Path(plist["WorkingDirectory"]).mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    if mode != "load":
        return path, f"wrote {path}"
    launchctl("bootout", f"{domain}/{plist['Label']}")  # a stale copy may be loaded; ignore "not loaded"
    code = launchctl("bootstrap", domain, str(path))
    if code:
        raise RuntimeError(f"launchctl bootstrap {domain} {path} exited {code}")
    return path, f"loaded {plist['Label']} from {path}"
