"""CLI for the runner: `mseo run weekly`, `mseo lock run --browser -- CMD`, `mseo schedule generate|watchdog`."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys

from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.run import outcome
from mseo.runner import lock, schedule, wrap


def _now() -> dt.datetime:
    fixed = os.environ.get("MSEO_NOW")  # tests only: a fixed clock
    return dt.datetime.fromisoformat(fixed) if fixed else dt.datetime.now(dt.timezone.utc)


def cmd_run_weekly(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    now = _now()
    job_id = outcome.new_job_id(now=now)
    scheduled = None if args.unscheduled else schedule.last_slot(ws, now)
    argv = wrap.child_argv(ws, job_id=job_id, week=args.week, scheduled_for=scheduled)
    out = wrap.run_wrapped(ws, job_id=job_id, argv=argv, scheduled_for=scheduled,
                           timeout_s=args.timeout_h * 3600)
    ctx.emit({**out, "messages": [f"{out['job_id']} {out['status']}" + (f": {out['error']}" if out.get("error") else "")]})
    return EXIT_OK if out["status"] == "ok" else EXIT_FAIL


def cmd_lock_run(args, ctx) -> int:
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("error: give the command after --", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    with lock.browser_lock(wait_s=args.wait_s) as held:
        if not held:
            print(f"error: the browser lock stayed held for {args.wait_s:.0f} s (holder pid {lock.holder('browser')})",
                  file=sys.stderr)
            return EXIT_FAIL
        return subprocess.run(cmd).returncode


def _mode(args) -> str:
    return "dry-run" if args.dry_run else "load" if args.load else "unload" if args.unload else "write"


def cmd_schedule_generate(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    path, msg = schedule.apply(schedule.weekly_plist(ws), mode=_mode(args))
    ctx.emit({"status": "ok", "path": str(path), "messages": [msg]})
    return EXIT_OK


def cmd_schedule_watchdog(args, ctx) -> int:
    path, msg = schedule.apply(schedule.watchdog_plist(), mode=_mode(args))
    ctx.emit({"status": "ok", "path": str(path), "messages": [msg]})
    return EXIT_OK


def _modes(p) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Print the plist; write nothing")
    g.add_argument("--load", action="store_true", help="Write the plist and launchctl bootstrap it")
    g.add_argument("--unload", action="store_true", help="launchctl bootout and remove the plist")


def register(sub, parents) -> None:
    run = sub.add_parser("run", help="Run a scheduled job now")
    rsub = run.add_subparsers(dest="run_command", required=True, parser_class=type(run))
    wk = rsub.add_parser("weekly", parents=[parents.workspace, parents.json],
                         help="The weekly job under the wrapper: workspace lock, 12-hour kill, one outcome record")
    wk.add_argument("--week", help="ISO week (default: the last complete week)")
    wk.add_argument("--unscheduled", action="store_true", help="A hand-started run: no scheduled_for")
    wk.add_argument("--timeout-h", type=float, default=wrap.KILL_AFTER_S / 3600, help=argparse.SUPPRESS)
    wk.set_defaults(func=cmd_run_weekly, command_id="run weekly")

    lk = sub.add_parser("lock", help="Machine-wide locks")
    lsub = lk.add_subparsers(dest="lock_command", required=True, parser_class=type(lk))
    lr = lsub.add_parser("run", help="Run a command while holding the machine-wide browser lock")
    lr.add_argument("--browser", action="store_true", required=True, help="Hold the machine-wide browser lock")
    lr.add_argument("--wait-s", type=float, default=lock.BROWSER_WAIT_S, help="How long to wait for the lock")
    lr.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command")
    lr.set_defaults(func=cmd_lock_run, command_id="lock run")

    sc = sub.add_parser("schedule", help="launchd jobs")
    ssub = sc.add_subparsers(dest="schedule_command", required=True, parser_class=type(sc))
    gen = ssub.add_parser("generate", parents=[parents.workspace, parents.json],
                          help="Write the workspace's weekly LaunchAgent from customer.json")
    _modes(gen)
    gen.set_defaults(func=cmd_schedule_generate, command_id="schedule generate")
    wd = ssub.add_parser("watchdog", parents=[parents.json], help="Write the operator-level watchdog LaunchAgent")
    _modes(wd)
    wd.set_defaults(func=cmd_schedule_watchdog, command_id="schedule watchdog")
