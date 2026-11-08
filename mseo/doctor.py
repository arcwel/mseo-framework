"""mseo doctor: is this workspace ready to run? Lists every failed check; never invents a value.

The weekly doctor's checks (no placeholders, an ID per source, writable data folder, tools installed)
and adds the workspace checks: schema valid (open() already did it), seal current, sibling set unchanged, a Keychain
item for each credential the config needs, each Chrome profile present, and no sibling sharing this identity's Chrome
profile or email (refused with exit 1). Writes data/weekly/<period>/preflight.run.json.
Exit 0 all checks pass, 1 any failed, 3 the workspace could not be opened.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from mseo import seal as seal_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.run import record as run_record

CHROME_LOCAL_STATE = seal_mod.CHROME_LOCAL_STATE
TOOLS = ("deslop", "uv", "git")


def today() -> dt.date:
    env = os.environ.get("MSEO_TODAY")
    return dt.date.fromisoformat(env) if env else dt.date.today()


def period(on: dt.date | None = None) -> str:
    year, week, _ = (on or today()).isocalendar()
    return f"{year}-W{week:02d}"


def tool_path(name: str) -> str | None:
    return shutil.which(name)


def _placeholders(value, path="") -> list:
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if not str(k).startswith("_"):
                found += _placeholders(v, f"{path}.{k}" if path else k)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found += _placeholders(v, f"{path}[{i}]")
    elif isinstance(value, str) and ("TO CONFIRM" in value or re.search(r"<[^<>]+>", value)):
        found.append(path)
    return found


def _writable(folder: Path) -> tuple[bool, str]:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".mseo-write-test"
        probe.write_text("ok")
        probe.unlink()
        return True, str(folder)
    except OSError as exc:
        return False, f"{folder}: {exc}"


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    period: str = ""

    @property
    def code(self) -> int:
        if any(c["check"] == "seal current" and not c["ok"] for c in self.checks):
            return EXIT_COULD_NOT_RUN  # no current seal: the workspace is not safe to run, not merely misconfigured
        return EXIT_OK if all(c["ok"] for c in self.checks) else EXIT_FAIL


def _add(report: Report, check: str, ok: bool, detail: str) -> None:
    report.checks.append({"check": check, "ok": bool(ok), "detail": detail})


def run(ws, *, chrome_state: Path | None = None) -> Report:
    report = Report(period=period())
    chrome_state = chrome_state or CHROME_LOCAL_STATE
    _add(report, "customer.json valid", True, f"schema {ws.config['schema_version']}")
    holes = _placeholders(dict(ws.config))
    _add(report, "customer.json has no TO CONFIRM or <...> values", not holes, ", ".join(holes) or "clean")
    for source in ws.enabled_sources():
        acct = ws.config["accounts"][source]
        _add(report, f"{source} ID set", bool(acct.get("id")), acct.get("id") or "missing")
    from mseo.identity import LISTERS

    for source in ws.enabled_sources():
        acct = ws.config["accounts"][source]
        if source in LISTERS:
            _add(report, f"{source} identity check", True, "lister available: mseo identity check")
        else:
            manual = acct.get("check") == "manual"
            _add(report, f"{source} identity check", manual,
                 "check: manual (verified by hand)" if manual else "unchecked: no lister; set check: manual")
        over = acct.get("identity_override")
        if over:
            _add(report, f"{source} identity override", True,
                 f"temporary: runs as {over['email']} (shared) until {over['until']}")
    budget = (ws.config.get("budget") or {}).get("weekly_tokens")
    _add(report, "weekly token budget", bool(budget),
         f"{budget} tokens per weekly job" if budget else "missing: set budget.weekly_tokens in customer.json")
    for c in seal_mod.keychain_checks(ws):
        source = c["check"].rsplit(".", 1)[-1]
        _add(report, f"{'WordPress' if source == 'wordpress' else source} credential mseo.{ws.slug}.{source}", c["ok"],
             "present" if c["ok"] else f"missing: mseo secret set --workspace {ws.slug} --source {source}")
    for c in seal_mod.chrome_checks(ws, chrome_state):
        _add(report, c["check"], c["ok"], "found in Chrome's Local State" if c["ok"] else f"not in {chrome_state}")
    clashes = seal_mod.identity_clashes(ws)  # read-only: the siblings' customer.json files
    _add(report, "identity unique across workspaces", not clashes,
         "; ".join(clashes) + "; give each workspace its own Chrome profile and identity" if clashes else "no sibling shares it")
    problems = seal_mod.check_seal(ws)
    _add(report, "seal current", not problems, "; ".join(problems) or "current")
    ok, detail = _writable(ws.path("data", "weekly", report.period))
    _add(report, "data folder writable", ok, detail)
    for tool in TOOLS:
        found = tool_path(tool)
        _add(report, f"{tool} installed", bool(found), found or "not on PATH")
    return report


def write_record(ws, report: Report) -> dict:
    failed = [c for c in report.checks if not c["ok"]]
    now = dt.datetime.now(dt.UTC)
    record = run_record.build(
        run_id=run_record.new_run_id("preflight", now=now), workspace=ws.slug, source="preflight",
        adapter="preflight", identity=None, period=run_record.week_period(report.period, "local"),
        account_id_expected=None, account_id_seen=None, seen_via="none", status="ok" if not failed else "error",
        complete={"value": False, "reason": "a preflight is not a data pull"}, files=[], started=now, finished=now,
        checks=report.checks, error="; ".join(f"{c['check']}: {c['detail']}" for c in failed) or None)
    try:
        write_json_atomic(ws.path("data", "weekly", report.period, "preflight.run.json"), record)
    except OSError:
        pass  # the writable check already reports why
    return record


def cmd_doctor(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    report = run(ws, chrome_state=CHROME_LOCAL_STATE)
    record = write_record(ws, report)
    msgs = [f"{'ok ' if c['ok'] else 'NO '} {c['check']} ({c['detail']})" for c in report.checks]
    msgs.append(f"preflight {report.period}: {record['status']}")
    ctx.emit({**record, "messages": msgs})
    return report.code


def register(sub, parents) -> None:
    p = sub.add_parser("doctor", parents=[parents.workspace, parents.json], help="Check a workspace is ready to run")
    p.set_defaults(func=cmd_doctor, command_id="doctor")
