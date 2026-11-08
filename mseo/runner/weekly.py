"""The weekly job: browser lock, identity check, every source in a fixed order, the token
budget, then the outcome record. One source failing never stops the next. The runner wrapper (`wrap.py`) holds the
workspace lock and the 12-hour kill around this.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable

from mseo import identity
from mseo.adapters import export
from mseo.run import outcome
from mseo.run import record as run_record
from mseo.runner import lock

ORDER = ("search_console", "ga4", "google_ads", "business_profile", "open_seo")
DONE = ("ok", "empty")
USAGE_KEYS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "turns", "minutes")
SELF_SIGN_IN = ("manual", "unchecked")  # sources the identity check did not sign in for


def default_pulls() -> dict[str, Callable]:
    from mseo.adapters import browser_pull, business_profile, open_seo

    def browser(source):
        return lambda ws, period, **kw: browser_pull.pull(ws, source, period, **kw)

    return {"search_console": browser("search_console"), "ga4": browser("ga4"),
            "google_ads": browser("google_ads"), "business_profile": business_profile.pull,
            "open_seo": lambda ws, period, signed_in=False, **kw: open_seo.pull(ws, period, **kw)}


def tokens(usage: dict | None) -> int:
    """What the budget counts: every token the agent processed, cache reads included."""
    if not usage:
        return 0
    return int(sum(usage.get(k) or 0 for k in ("tokens_in", "tokens_out", "cache_read", "cache_write")))


def _add(total: dict, usage: dict | None) -> None:
    for k in USAGE_KEYS:
        total[k] = round(total.get(k, 0) + ((usage or {}).get(k) or 0), 2)


def _budget_record(ws, source: str, period: dict, job_id: str, now, spent: int, budget: int) -> str:
    started = now or dt.datetime.now(dt.timezone.utc)
    run_id = run_record.new_run_id(source, now=started)
    ident = ws.identity_for(source) if source != "open_seo" else {"email": None, "allow_shared": False}
    rec = run_record.build(run_id=run_id, job_id=job_id, workspace=ws.slug, source=source,
                           adapter=ws.config["accounts"][source].get("adapter", "browser"), identity=ident["email"],
                           identity_shared=bool(ident["allow_shared"]), period=period,
                           account_id_expected=ws.account(source), account_id_seen=None, seen_via="none",
                           status="budget_exceeded", complete={"value": False, "reason": "budget_exceeded"},
                           files=[], started=started, finished=started,
                           error=f"the job's token budget is spent ({spent} of {budget})")
    run_record.write(ws.path("data", "runs", run_id), rec)
    return run_id


def run_job(ws, *, week: str | None = None, job_id: str, scheduled_for=None, now: dt.datetime | None = None,
            runner=None, pulls: dict | None = None, identity_check: Callable = identity.identity_check,
            browser_wait_s: float = lock.BROWSER_WAIT_S, after: Callable | None = None) -> dict:
    started = now or dt.datetime.now(dt.timezone.utc)
    pulls = pulls if pulls is not None else default_pulls()
    run_ids: list[str] = []
    usage: dict = {}
    checks: dict = {}

    def finish(status: str, error: str | None, shared: bool = False) -> dict:
        out = outcome.build(job_id=job_id, workspace=ws.slug, status=status, scheduled_for=scheduled_for,
                            started=started, finished=now or dt.datetime.now(dt.timezone.utc), run_ids=run_ids,
                            identity_check=checks, identity_shared=shared, usage=usage or None, error=error)
        outcome.write(ws.path("data", "outcomes"), out)
        return out

    budget = (ws.config.get("budget") or {}).get("weekly_tokens")
    if not budget:
        return finish("failed", "customer.json has no budget.weekly_tokens; set it before scheduling")
    period = export.resolve_period(ws, week=week, start=None, end=None)
    sources = [s for s in ORDER if s in ws.enabled_sources() and s in pulls]
    with lock.browser_lock(wait_s=browser_wait_s) as held:
        if not held:
            return finish("not_run", f"the browser lock stayed held for {browser_wait_s:.0f} s "
                                     f"(holder pid {lock.holder('browser')})")
        check = identity_check(ws, sources=sources, runner=runner, now=now, period=period, job_id=job_id) \
            if runner is not None else identity_check(ws, sources=sources, now=now, period=period, job_id=job_id)
        checks.update(check.get("results") or {})
        if check.get("run_id"):
            run_ids.append(check["run_id"])
        shared = check.get("status") == "shared"
        if check.get("status") not in ("ok", "shared"):
            return finish("refused", "the identity check refused: " + ", ".join(
                f"{s} {r}" for s, r in sorted(checks.items()) if r not in ("ok", "manual")))
        statuses, crashes = [], []
        for source in sources:
            spent = tokens(usage)
            if spent >= budget:
                run_ids.append(_budget_record(ws, source, period, job_id, now, spent, budget))
                statuses.append("budget_exceeded")
                continue
            kw = dict(job_id=job_id, now=now, signed_in=checks.get(source) not in SELF_SIGN_IN)
            if runner is not None:
                kw["runner"] = runner
            try:
                code, rec, line = pulls[source](ws, period, **kw)
            except Exception as exc:  # noqa: BLE001 - one source crashing must not stop the next
                statuses.append("error")
                crashes.append(f"{source} crashed: {type(exc).__name__}: {exc}"[:300])
                continue
            if rec is None:  # a record for this week already exists
                continue
            run_ids.append(rec["run_id"])
            _add(usage, rec.get("usage"))
            statuses.append(rec["status"])
    after_error = None
    if after is not None:
        try:
            after(ws, week or week_label(period))
        except Exception as exc:  # noqa: BLE001 - the week file is a convenience; the outcome and records are the truth
            after_error = f"after step failed: {type(exc).__name__}: {exc}"[:300]
    failed = [s for s in statuses if s not in DONE]
    parts = ([f"{len(failed)} source(s) not ok"] if failed else []) + crashes + ([after_error] if after_error else [])
    return finish("partial" if failed or after_error else "ok", "; ".join(parts) or None, shared)


def week_label(period: dict) -> str:
    """The ISO week of the period's first day, e.g. 2026-W44."""
    year, number, _ = dt.date.fromisoformat(period["start"]).isocalendar()
    return f"{year}-W{number:02d}"


def assemble_and_compare(ws, week_name: str) -> dict:
    """The scheduled job's after step: write data/weeks/<W>.json from the week's ok records, then the shadow verdict.
    Both are also CLI commands."""
    from mseo import shadow, week

    week.assemble(ws, week_name)
    verdict = shadow.compare(ws, week_name)
    return {"week": week_name, "shadow": verdict.get("status")}
