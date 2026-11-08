"""`mseo pull --workspace S --source X [--week W | --start D --end D] [--file PATH] [--force]`: one adapter, one record.

With --file (relative to the workspace root), Python ingests a report export the operator saved inside the workspace; the record is
`unverified_account` (seen_via operator) because no page snapshot shows the account. Agent-driven pulls come with the
browser runner. Exit 0 recorded (ok, empty or a manual export), 1 the pull failed its checks,
2 a record for that source and period exists (--force pulls again under a new run id), 3 could not run.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from mseo.adapters import export, ga4, search_console
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.run import record as run_record

EXIT_EXISTS = 2
EXPORT_ADAPTERS = {"search_console": search_console, "ga4": ga4}
MANUAL_NOTE = "manual export: no page snapshot shows the account, so it was not checked"


def _covers(adapter, parsed: dict, period: dict) -> tuple[bool, str | None]:
    """(covered fully, error). Data outside the period is an error; missing days only make it incomplete."""
    first, last = adapter.coverage(parsed)
    if first is None:
        return False, None
    if adapter is ga4 and (first, last) != (period["start"], period["end"]):
        return False, f"the export covers {first} to {last}, not {period['start']} to {period['end']}"
    if first < period["start"] or last > period["end"]:
        return False, f"the export covers {first} to {last}, outside {period['start']} to {period['end']}"
    return (first, last) == (period["start"], period["end"]), None


def pull_file(ws, source: str, src, period: dict, *, force: bool = False, job_id: str | None = None,
              now: dt.datetime | None = None) -> tuple[int, dict | None, str]:
    adapter = EXPORT_ADAPTERS[source]
    now = now or dt.datetime.now(dt.timezone.utc)
    dup = export.existing(ws, source, period)
    if dup and not force:
        return EXIT_EXISTS, None, f"{source} {period['start']}..{period['end']} already recorded ({', '.join(dup)})"
    real = ws.contains(src)
    run_id = run_record.new_run_id(source, now=now)
    run_dir = ws.path("data", "runs", run_id)
    ident = ws.identity_for(source)
    base = dict(run_id=run_id, job_id=job_id, workspace=ws.slug, source=source, adapter="export",
                identity=ident["email"], identity_shared=ident["allow_shared"], period=period,
                account_id_expected=ws.account(source), account_id_seen=None, seen_via="operator", started=now)
    original = export.ingest(ws, real, run_dir, name=export.export_name(real))
    status, error, files, complete = "unverified_account", MANUAL_NOTE, [], {"value": False, "reason": "manual export"}
    try:
        parsed = adapter.parse_csv(run_dir / original["path"]) if adapter is ga4 else \
            adapter.parse_zip(run_dir / original["path"])
        full, wrong = _covers(adapter, parsed, period)
        if wrong:
            raise export.ExportError(wrong)
        files = [original, *adapter.write(parsed, run_dir)]
        if not full:
            error = f"{MANUAL_NOTE}; the export does not cover every day of the period"
    except export.LayoutChanged as exc:
        status, error = "layout_changed", str(exc)
    except export.ExportError as exc:
        status, error = "error", str(exc)
    if status != "unverified_account":
        os.replace(run_dir / original["path"], real)  # give the operator's file back untouched
        complete = {"value": False, "reason": status}
    rec = run_record.build(**base, status=status, error=error, files=files, complete=complete,
                           finished=dt.datetime.now(dt.timezone.utc))
    run_record.write(run_dir, rec)
    code = EXIT_OK if status == "unverified_account" else EXIT_FAIL
    return code, rec, f"{status}: {source} {period['start']}..{period['end']} -> {run_dir}"


def _cmd_agent_pull(args, ctx, ws) -> int:
    from mseo import seal
    from mseo.adapters import browser_pull

    from mseo.adapters import business_profile, open_seo

    if args.source not in browser_pull.SOURCES and args.source not in (open_seo.SOURCE, business_profile.SOURCE):
        ctx.emit({"status": "error", "messages": [f"error: {args.source} has no adapter yet"]})
        return EXIT_COULD_NOT_RUN
    try:
        period = export.resolve_period(ws, week=args.week, start=args.start, end=args.end)
        if args.source == open_seo.SOURCE:
            code, rec, line = open_seo.pull(ws, period, force=args.force)
        elif args.source == business_profile.SOURCE:
            code, rec, line = business_profile.pull(ws, period, force=args.force)
        else:
            code, rec, line = browser_pull.pull(ws, args.source, period, force=args.force)
    except (export.ExportError, ValueError, seal.StaleSeal) as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    ctx.emit({**(rec or {"status": "exists"}), "messages": [line]})
    return code


def cmd_pull(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    if args.source not in ws.enabled_sources():
        ctx.emit({"status": "error", "messages": [f"error: {ws.slug} has no enabled account for {args.source!r}"]})
        return EXIT_COULD_NOT_RUN
    if not args.file:
        return _cmd_agent_pull(args, ctx, ws)
    if args.source not in EXPORT_ADAPTERS:
        ctx.emit({"status": "error", "messages": [f"error: {args.source} has no export parser"]})
        return EXIT_COULD_NOT_RUN
    try:
        period = export.resolve_period(ws, week=args.week, start=args.start, end=args.end)
    except (export.ExportError, ValueError) as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    src = Path(args.file)
    src = ws.contains(src) if src.is_absolute() else ws.path(src)  # relative paths are relative to the workspace
    if not src.is_file():
        ctx.emit({"status": "error", "messages": [f"error: {args.file} is not a file in {ws.slug}"]})
        return EXIT_COULD_NOT_RUN
    code, rec, line = pull_file(ws, args.source, src, period, force=args.force)
    ctx.emit({**(rec or {"status": "exists"}), "messages": [line]})
    return code


def register(sub, parents) -> None:
    p = sub.add_parser("pull", parents=[parents.workspace, parents.json],
                       help="Run one source's adapter for one period and write its run record")
    p.add_argument("--source", required=True, help="Account source, e.g. search_console")
    p.add_argument("--week", help="ISO week, e.g. 2026-W45 (default: the last complete week)")
    p.add_argument("--start", help="First day, YYYY-MM-DD (with --end)")
    p.add_argument("--end", help="Last day, YYYY-MM-DD (with --start)")
    p.add_argument("--file", help="A saved report export inside the workspace (manual export)")
    p.add_argument("--force", action="store_true", help="Pull again when a record for the period exists")
    p.set_defaults(func=cmd_pull, command_id="pull")
