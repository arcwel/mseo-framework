"""Browser-driven export pulls: an agent opens the report in the identity's browser, saves the
snapshot that shows the account, and clicks Export; Python checks the account from the snapshot, then ingests the
export with the same parsers as `mseo pull --file`.

Account check before data: a snapshot showing another account, or none, means nothing is kept: the export and the
snapshot are deleted, and the record says why. The URL is never evidence of the account.
"""

from __future__ import annotations

import datetime as dt
import time
import urllib.parse

from mseo import identity
from mseo.adapters import export, ga4, google_ads, google_ads_sheet, search_console
from mseo.browser import attest, tie, extract
from mseo.browser import run as browser_run
from mseo.exits import EXIT_FAIL, EXIT_OK
from mseo.run import record as run_record

EXIT_EXISTS = 2
DEFAULT_RUNNER = browser_run.subprocess_runner
SOURCES = {
    "search_console": {"adapter": search_console, "prompt": "export_search_console",
                       "seen_via": "page:sc_property_selector", "extract": extract.sc_selected_property},
    "ga4": {"adapter": ga4, "prompt": "export_ga4", "seen_via": "page:ga4_property_picker",
            "extract": extract.ga4_selected_property},
    "google_ads": {"adapter": google_ads, "prompt": "export_google_ads", "seen_via": "page:ads_account_header",
                   "extract": extract.ads_header_customer_id},
}
# A source with two paths: the resolver names the one the workspace's config selects. Google Ads
# goes through the Scripts Sheet when accounts.google_ads.scripts_sheet_id is set, else through Report Editor.
RESOLVERS = {google_ads_sheet.SOURCE: lambda ws: "sheet" if google_ads_sheet.configured(ws) else "report_editor"}
SHEET_PULLS = {google_ads_sheet.SOURCE: google_ads_sheet.pull}


def resolve(ws, source: str) -> str:
    """Which path `pull` takes for this source in this workspace: 'sheet', 'report_editor' or 'page'."""
    resolver = RESOLVERS.get(source)
    return resolver(ws) if resolver else "page"


def _ymd(day: str) -> str:
    return day.replace("-", "")


def _human(day: str) -> str:
    return dt.date.fromisoformat(day).strftime("%b %-d, %Y")


def _slash(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return f"{d.month}/{d.day}/{d.year}"


def report_url(ws, source: str, period: dict) -> str:
    """The report's URL with the period filled in (a starting point; the agent checks the dates on the page)."""
    acct = ws.account(source)
    if source == "google_ads":
        return "https://ads.google.com/nav/selectaccount"
    if source == "search_console":
        q = urllib.parse.urlencode({"resource_id": acct, "start_date": _ymd(period["start"]),
                                    "end_date": _ymd(period["end"])})
        return f"https://search.google.com/search-console/performance/search-analytics?{q}"
    params = urllib.parse.quote(f"_u.date00={_ymd(period['start'])}&_u.date01={_ymd(period['end'])}")
    return (f"https://analytics.google.com/analytics/web/#/p{acct}/reports/explorer?params={params}"
            "&r=lifecycle-traffic-acquisition-v2")


def values(ws, source: str, period: dict) -> dict:
    return {"url": report_url(ws, source, period), "email": ws.identity_for(source)["email"],
            "snapshot": f"snapshot-{source}.txt", "totals_snapshot": f"totals-{source}.txt", "start_human": _human(period["start"]),
            "end_human": _human(period["end"]), "start": _ymd(period["start"]), "end": _ymd(period["end"]),
            "start_slash": _slash(period["start"]), "end_slash": _slash(period["end"]),
            "customer_id": ws.account(source)}


def _discard(paths) -> None:
    for p in paths:
        if p is not None:
            p.unlink(missing_ok=True)


def _not_tied(source: str, spec: dict, res, run_dir, expected) -> str | None:
    """Why the export cannot be tied to the attested account, or None."""
    from mseo import shadow, week

    ours = week.METRICS[source](week._read(run_dir / week.FILES[source]))["metrics"]
    reason, detail = tie.check(source, res.calls, attest.attested_url(spec["extract"], res), ours,
                               shadow.tolerances(), shadow.within, expected=expected)
    return detail if reason else None


def pull(ws, source: str, period: dict, *, runner=None, force: bool = False, job_id: str | None = None,
         now: dt.datetime | None = None, signed_in: bool = False, require_seal: bool = True):
    """(exit code, record or None, one line). signed_in=True when the job already ran the sign-in check."""
    from mseo.capture.account import normalize

    if resolve(ws, source) == "sheet":
        return SHEET_PULLS[source](ws, period, runner=runner or DEFAULT_RUNNER, force=force, job_id=job_id, now=now,
                                   signed_in=signed_in, require_seal=require_seal)
    spec = SOURCES[source]
    adapter = spec["adapter"]
    runner = runner or DEFAULT_RUNNER
    started = now or dt.datetime.now(dt.timezone.utc)
    dup = export.existing(ws, source, period)
    if dup and not force:
        return EXIT_EXISTS, None, f"{source} {period['start']}..{period['end']} already recorded ({', '.join(dup)})"
    ident = ws.identity_for(source)
    run_id = run_record.new_run_id(source, now=started)
    run_dir = ws.path("data", "runs", run_id)
    expected = ws.account(source)
    base = dict(run_id=run_id, job_id=job_id, workspace=ws.slug, source=source, adapter="export",
                identity=ident["email"], identity_shared=bool(ident["allow_shared"]), period=period,
                account_id_expected=expected, seen_via=spec["seen_via"], started=started)

    def finish(status, error, *, seen=None, files=(), usage=None, complete=None):
        rec = run_record.build(**base, account_id_seen=seen, status=status, error=error, files=list(files),
                               usage=usage, complete=complete or {"value": False, "reason": status},
                               finished=now or dt.datetime.now(dt.timezone.utc))
        run_record.write(run_dir, rec)
        code = EXIT_OK if status in ("ok", "empty") else EXIT_FAIL
        return code, rec, f"{status}: {source} {period['start']}..{period['end']} -> {run_dir}"

    if not signed_in:
        sign = identity.sign_in_check(ws, source, period=period, runner=runner, now=now, job_id=job_id,
                                      require_seal=require_seal)
        if sign["status"] != "ok":
            return finish(sign["status"], f"sign-in check: {sign['error']}")
    inbox = export.inbox(ws)
    inbox.mkdir(parents=True, exist_ok=True)
    vals = values(ws, source, period)
    snap = inbox / vals["snapshot"]
    totals = inbox / vals["totals_snapshot"]  # the page's own totals: the shadow baseline
    snap.unlink(missing_ok=True)
    totals.unlink(missing_ok=True)
    since = time.time() - 1
    res = browser_run.run_agent(ws, browser_run.prompt(spec["prompt"], ws, source=source, **vals), runner=runner,
                                require_seal=require_seal)
    snap_ok = snap.is_file()
    try:
        exported = next((p for p in [export.pick_up(ws, since, {snap.name, totals.name})] if p), None)
        pick_error = None
    except export.ExportError as exc:
        exported, pick_error = None, str(exc)
    if not res.ok:
        _discard([snap, totals, exported])
        return finish("auth_failed" if res.status == "auth_failed" else "error", res.error, usage=res.usage)
    if res.reply.lower().startswith("failed"):
        _discard([snap, totals, exported])
        gone = f"; discarded {exported.name}" if exported else "; no export arrived"
        return finish("error", f"the agent replied: {res.reply[:300]}{gone}", usage=res.usage)
    linked = [p.name for p in (snap, totals) if p.is_symlink()]
    if linked:  # a link could point anywhere, and ingest refuses it
        _discard([snap, totals, exported])
        return finish("error", f"the agent saved {', '.join(linked)} as a link, so nothing was taken", usage=res.usage)
    if not snap_ok:
        _discard([totals, exported])
        return finish("unverified_account", f"the agent saved no {snap.name}, so the account was not checked",
                      usage=res.usage)
    seen, reason = attest.attested(spec["extract"], snap.read_text(encoding="utf-8", errors="replace"), res)
    if reason in attest.REASONS:
        _discard([snap, totals, exported])
        return finish("unverified_account", attest.why(reason, "account"), usage=res.usage)
    if reason or normalize(seen) != normalize(expected):
        _discard([snap, totals, exported])
        if reason == "signed_out":
            return finish("auth_failed", "the report page asked to sign in", usage=res.usage)
        if reason:
            return finish("layout_changed", f"account not found on the page ({reason})", usage=res.usage)
        return finish("wrong_account", f"the page showed {seen}, expected {expected}; nothing was saved", seen=seen,
                      usage=res.usage)
    if exported is None:
        _discard([snap, totals])
        return finish("error", pick_error or "no export arrived", seen=seen, usage=res.usage)
    snap_entry = export.ingest(ws, snap, run_dir)
    original = export.ingest(ws, exported, run_dir, name=export.export_name(exported))
    try:
        parsed = adapter.parse(run_dir / original["path"])
        first, last = adapter.coverage(parsed)
        if first is None:
            raise export.ExportError("the export holds no days")
        if first < period["start"] or last > period["end"]:
            raise export.ExportError(f"the export covers {first} to {last}, outside {period['start']} to {period['end']}")
        tables = adapter.write(parsed, run_dir)
    except export.LayoutChanged as exc:
        _discard([run_dir / snap_entry["path"], run_dir / original["path"], totals])
        return finish("layout_changed", str(exc), seen=seen, usage=res.usage)
    except export.ExportError as exc:
        _discard([run_dir / snap_entry["path"], run_dir / original["path"], totals])
        return finish("error", str(exc), seen=seen, usage=res.usage)
    why = _not_tied(source, spec, res, run_dir, expected)
    if why:
        _discard([run_dir / snap_entry["path"], run_dir / original["path"], *(run_dir / t["path"] for t in tables),
                  totals])
        return finish("unverified_account", f"the export was not taken: {why} (Chrome tool output is the evidence)",
                      seen=seen, usage=res.usage)
    full = (first, last) == (period["start"], period["end"])
    if not full:
        complete = {"value": False, "reason": "period not covered"}
    elif export.within_lag(period, adapter.LAG_DAYS):
        complete = {"value": False, "reason": "within reporting lag"}
    else:
        complete = {"value": True, "reason": None}
    extra = [export.ingest(ws, totals, run_dir)] if totals.is_file() else []
    more, checks = _extra_tables(ws, source, spec, period, run_dir, runner, expected, require_seal, vals)
    rec_usage = res.usage
    for c in checks:
        _add_usage(rec_usage, c.pop("usage", None))
    base["checks"] = checks or None
    return finish("ok", None, seen=seen, files=[snap_entry, original, *tables, *extra, *more], usage=rec_usage,
                  complete=complete)


def _add_usage(total, part) -> None:
    if not total or not part:
        return
    for k, v in part.items():
        if isinstance(v, (int, float)) and isinstance(total.get(k), (int, float)):
            total[k] = total[k] + v


def extra_specs(ws, source: str, vals: dict) -> list[dict]:
    """The extra report tables this workspace pulls for `source` (accounts.<source>.tables): prompt, parse, write, url."""
    out = []
    if source == "search_console":
        for t in search_console.extra_tables(ws):
            out.append({"table": t, "prompt": search_console.extra_prompt(t), "parse": search_console.parse_generative_ai,
                        "write": search_console.write_generative_ai, "url": vals["url"]})
    elif source == "ga4":
        for t in ga4.extra_tables(ws):
            e = ga4.EXTRA[t]
            url = vals["url"].split("&r=")[0] + "&r=" + e["report"]
            out.append({"table": t, "prompt": e["prompt"], "parse": e["parse"], "write": e["write"], "url": url})
    return out


def _extra_tables(ws, source, spec, period, run_dir, runner, expected, require_seal, vals) -> tuple[list, list]:
    """One more agent run per extra table, after the main export passed. Each is attested on its own snapshot; a
    failure is recorded in the record's `checks` and never fails the main pull. Extras are not shadow-compared."""
    from mseo.capture.account import normalize

    files, checks = [], []
    inbox = export.inbox(ws)
    for e in extra_specs(ws, source, vals):
        snap = inbox / f"snapshot-{source}-{e['table']}.txt"
        totals = inbox / f"totals-{source}-{e['table']}.txt"
        snap.unlink(missing_ok=True)
        totals.unlink(missing_ok=True)
        since = time.time() - 1
        res = browser_run.run_agent(ws, browser_run.prompt(e["prompt"], ws, source=source, **{**vals, "url": e["url"],
                                    "snapshot": snap.name, "totals_snapshot": totals.name}), runner=runner,
                                    require_seal=require_seal)
        check = {"table": e["table"], "status": "error", "detail": None, "usage": res.usage}
        try:
            exported = export.pick_up(ws, since, {snap.name, totals.name})
        except export.ExportError as exc:
            exported = None
            check["detail"] = str(exc)
        if not res.ok or res.reply.lower().startswith("failed"):
            _discard([snap, totals, exported])
            check["detail"] = res.error or res.reply[:200]
            checks.append(check)
            continue
        if not snap.is_file() or snap.is_symlink():
            _discard([snap, totals, exported])
            check.update(status="unverified_account", detail=f"no {snap.name}")
            checks.append(check)
            continue
        seen, reason = attest.attested(spec["extract"], snap.read_text(encoding="utf-8", errors="replace"), res)
        if reason or normalize(seen) != normalize(expected) or exported is None:
            _discard([snap, totals, exported])
            check.update(status="unverified_account" if reason or exported is None else "wrong_account",
                         detail=check["detail"] or (attest.why(reason, "account") if reason in attest.REASONS else reason or f"page showed {seen}"))
            checks.append(check)
            continue
        snap_entry = export.ingest(ws, snap, run_dir, name=f"snapshot-{e['table']}.txt")
        original = export.ingest(ws, exported, run_dir, name=f"export-{e['table']}{exported.suffix.lower()}")
        try:
            parsed = e["parse"](run_dir / original["path"])
            written = e["write"](parsed, run_dir)
        except export.ExportError as exc:
            _discard([run_dir / snap_entry["path"], run_dir / original["path"], totals])
            check.update(status="layout_changed" if isinstance(exc, export.LayoutChanged) else "error", detail=str(exc))
            checks.append(check)
            continue
        totals.unlink(missing_ok=True)
        files += [snap_entry, original, *written]
        check.update(status="ok", detail=f"{len(written)} table(s)")
        checks.append(check)
    return files, checks
