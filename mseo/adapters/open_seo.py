"""Open-SEO rank tracking through its own CLI, free commands only: `openseo-client list --json` and
`openseo-client track status <project> --json`. It never starts a paid check.

Project check before data: the project ID from customer.json must be listed exactly once by name, and the status the
CLI returns must name that same project. Other projects in the operator's Open-SEO install are never stored.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

from mseo.adapters import export
from mseo.exits import EXIT_FAIL, EXIT_OK
from mseo.run import record as run_record

SOURCE = "open_seo"
EXIT_EXISTS = 2
FRESH_DAYS = 8  # a rank check further than this from the period's last day does not describe that week
TIMEOUT_S = 180
# map_pack: place in the 3-business box on the results page; maps: place in the Google Maps list
# (top 20, checked from the client's "maps:" point); maps_as_of: when that Maps check ran.
FIELDS = ["keyword", "position", "previous", "url", "local_pack", "map_pack", "maps", "as_of", "maps_as_of"]
SHORT = re.compile(r"Checked (\d+) of (\d+) keyword")


def command() -> str:
    return os.environ.get("MSEO_OPENSEO_CMD", str(Path.home() / ".local/bin/openseo-client"))


def subprocess_runner(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL, **kw)


DEFAULT_RUNNER = subprocess_runner


class CliError(Exception):
    pass


def _call(runner, *args) -> dict:
    argv = [command(), *args, "--json"]
    try:
        proc = runner(argv)
    except FileNotFoundError:
        raise CliError(f"openseo-client not found at {argv[0]} (set MSEO_OPENSEO_CMD)") from None
    except subprocess.TimeoutExpired:
        raise CliError(f"openseo-client {args[0]} timed out after {TIMEOUT_S} s") from None
    if proc.returncode != 0:
        raise CliError(f"openseo-client {args[0]} exited {proc.returncode}: "
                       f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise CliError(f"openseo-client {args[0]} did not return JSON") from None


def _completeness(stat: dict, period: dict) -> dict:
    checked = (stat.get("checkedAt") or "")[:10]
    if not checked:
        return {"value": False, "reason": "no rank check has run"}
    gap = abs((dt.date.fromisoformat(period["end"]) - dt.date.fromisoformat(checked)).days)
    if gap > FRESH_DAYS:
        return {"value": False, "reason": f"the last rank check was {checked}, {gap} days from {period['end']}"}
    short = SHORT.search(stat.get("runError") or "")
    if short and int(short.group(1)) < int(short.group(2)):
        missing = int(short.group(2)) - int(short.group(1))
        return {"value": False, "reason": f"{missing} of {short.group(2)} keywords not checked"}
    return {"value": True, "reason": None}


def pull(ws, period: dict, *, runner=None, force: bool = False, job_id: str | None = None,
         now: dt.datetime | None = None):
    """(exit code, record or None, one line)."""
    runner = runner or DEFAULT_RUNNER
    started = now or dt.datetime.now(dt.timezone.utc)
    dup = export.existing(ws, SOURCE, period)
    if dup and not force:
        return EXIT_EXISTS, None, f"{SOURCE} {period['start']}..{period['end']} already recorded ({', '.join(dup)})"
    expected = ws.account(SOURCE)
    run_id = run_record.new_run_id(SOURCE, now=started)
    run_dir = ws.path("data", "runs", run_id)
    base = dict(run_id=run_id, job_id=job_id, workspace=ws.slug, source=SOURCE, adapter="api", identity=None,
                identity_shared=False, period=period, account_id_expected=expected, seen_via="api:project",
                started=started)

    def finish(status, error, *, seen=None, files=(), complete=None):
        rec = run_record.build(**base, account_id_seen=seen, status=status, error=error, files=list(files),
                               complete=complete or {"value": False, "reason": status},
                               finished=now or dt.datetime.now(dt.timezone.utc))
        run_record.write(run_dir, rec)
        code = EXIT_OK if status in ("ok", "empty") else EXIT_FAIL
        return code, rec, f"{status}: {SOURCE} {period['start']}..{period['end']} -> {run_dir}"

    try:
        projects = _call(runner, "list").get("projects") or []
        mine = [p for p in projects if p.get("id") == expected]
        if not mine:
            return finish("wrong_account", f"project {expected} is not in this Open-SEO install")
        name = mine[0].get("name")
        if sum(1 for p in projects if p.get("name") == name) > 1:
            return finish("unverified_account", "two Open-SEO projects share this project's name, so the status "
                                                "output cannot be matched to it")
        stat = _call(runner, "track", "status", expected)
    except CliError as exc:
        return finish("error", str(exc))
    if stat.get("project") != name:
        return finish("wrong_account", "the status output names another project; nothing was saved")
    as_of = (stat.get("checkedAt") or "")[:10] or None
    maps_as_of = (stat.get("mapsCheckedAt") or "")[:10] or None
    rows = [{"keyword": r.get("keyword"), "position": r.get("position"), "previous": r.get("previousPosition"),
             "url": r.get("url"), "local_pack": r.get("hasLocalPack"), "map_pack": r.get("mapPackPosition"),
             "maps": r.get("mapsPosition"), "as_of": as_of, "maps_as_of": maps_as_of}
            for r in stat.get("rows") or []]
    if not rows:
        return finish("empty", None, seen=expected, complete={"value": True, "reason": None})
    run_dir.mkdir(parents=True, exist_ok=True)
    table = export.write_table(run_dir, "rankings.csv", FIELDS, rows)
    return finish("ok", None, seen=expected, files=[table], complete=_completeness(stat, period))
