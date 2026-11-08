"""`mseo ads tracking`: the conversion-tracking gate, over the sheet's
conversion_actions table and the campaigns' call counts. Optimizing on bad conversion data is worse than not
optimizing, so a fail here blocks the action plan.

Every rule comes from customer.json `ads.tracking`: phone_calls, ga4_import, enhanced_conversions, primary_actions.
Enhanced conversions cannot be read from any table the script writes; that check is `unchecked` with the UI path."""

from __future__ import annotations

import datetime as dt

from mseo.ads import tables
from mseo.fsutil import write_json_atomic
from mseo.run import record as record_mod

SCHEMA_VERSION = "mseo.tracking/1"
ENABLED = "ENABLED"
GA4_ORIGIN = "GOOGLE_ANALYTICS"  # conversion_action.origin values for GA4 imports start with this
CALL_MARK = "CALL"  # AD_CALL, WEBSITE_CALL, CLICK_TO_CALL types and PHONE_CALL_LEAD category all carry it
TRUE_WORDS = ("true", "1", "yes")
ENHANCED_UI = "Goals > Conversions > Settings > Enhanced conversions (web and leads) in the Ads UI"


def _true(v) -> bool:
    return str(v or "").strip().lower() in TRUE_WORDS


def _enabled(actions: list[dict]) -> list[dict]:
    return [a for a in actions if (a.get("status") or "").upper() == ENABLED]


def _primary(actions: list[dict]) -> list[dict]:
    return [a for a in _enabled(actions) if _true(a.get("primary"))]


def _is_call(a: dict) -> bool:
    return CALL_MARK in f"{a.get('type', '')} {a.get('category', '')}".upper()


def _is_ga4(a: dict) -> bool:
    return (a.get("origin") or "").upper().startswith(GA4_ORIGIN)


def _check(name: str, status: str, detail: str, evidence: list[dict] | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "evidence": evidence or []}


def checks(actions: list[dict], campaigns: list[dict], cfg: dict) -> list[dict]:
    out = []
    primary = _primary(actions)
    # 1. a primary phone-call action
    if cfg["phone_calls"]:
        calls = [a for a in primary if _is_call(a)]
        out.append(_check("phone_call_primary", "pass" if calls else "fail",
                          f"{len(calls)} primary phone-call action(s)" if calls else
                          "no enabled primary phone-call conversion action; calls from ads are not counted as conversions",
                          calls))
    else:
        out.append(_check("phone_call_primary", "pass", "phone calls are not a conversion for this account (ads.tracking.phone_calls)"))
    # 2. GA4 imports are secondary
    if cfg["ga4_import"]:
        ga4 = [a for a in _enabled(actions) if _is_ga4(a)]
        ga4_primary = [a for a in ga4 if _true(a.get("primary"))]
        if not ga4:
            out.append(_check("ga4_imports_secondary", "fail", "no enabled GA4-imported conversion action (origin GOOGLE_ANALYTICS*)"))
        elif ga4_primary:
            out.append(_check("ga4_imports_secondary", "fail",
                              f"{len(ga4_primary)} GA4 import(s) are primary and double count with the Ads actions",
                              ga4_primary))
        else:
            out.append(_check("ga4_imports_secondary", "pass", f"{len(ga4)} GA4 import(s), all secondary", ga4))
    else:
        out.append(_check("ga4_imports_secondary", "pass", "no GA4 import expected (ads.tracking.ga4_import)"))
    # 3. configured names are primary
    names = list(cfg["primary_actions"])
    if names:
        by_name = {a["name"]: a for a in _enabled(actions)}
        wrong = [n for n in names if n not in by_name or not _true(by_name[n].get("primary"))]
        out.append(_check("primary_actions", "fail" if wrong else "pass",
                          "not primary or not enabled: " + ", ".join(wrong) if wrong else
                          f"all {len(names)} configured action(s) are primary", [by_name[n] for n in names if n in by_name]))
    else:
        out.append(_check("primary_actions", "pass", "no names configured (ads.tracking.primary_actions)"))
    # 4. one primary per category
    by_cat: dict[str, list[dict]] = {}
    for a in primary:
        by_cat.setdefault((a.get("category") or "").upper(), []).append(a)
    doubled = {c: rows for c, rows in by_cat.items() if len(rows) > 1}
    # two primary call actions (a call asset and a website call, say) count the same lead class twice at worst, and
    # Google counts calls once per click: a warning. Any other shared category double counts and fails.
    hard = {c for c in doubled if CALL_MARK not in c}
    if not doubled:
        status, detail = "pass", f"{len(by_cat)} primary categor{'y' if len(by_cat) == 1 else 'ies'}, one action each"
    elif hard:
        status, detail = "fail", "two or more primary actions share a category (double counting): " + ", ".join(sorted(doubled))
    else:
        status, detail = "warn", "two or more primary phone-call actions share a category: " + ", ".join(sorted(doubled)) + \
            "; calls may be counted by more than one action"
    out.append(_check("one_primary_per_category", status, detail, [a for rows in doubled.values() for a in rows]))
    # 5. enhanced conversions: not in any table
    out.append(_check("enhanced_conversions", "unchecked" if cfg["enhanced_conversions"] else "pass",
                      f"not readable from the sheet; confirm by hand at {ENHANCED_UI}" if cfg["enhanced_conversions"]
                      else "not expected (ads.tracking.enhanced_conversions)"))
    # 6. calls reported this week
    total_calls = sum(c.get("calls") or 0 for c in campaigns)
    if cfg["phone_calls"] and total_calls == 0:
        out.append(_check("call_volume", "warn", "campaigns report 0 phone calls this week: check the call asset is live"))
    else:
        out.append(_check("call_volume", "pass", f"{int(total_calls)} phone call(s) reported by the campaigns"))
    return out


def gate(ws, week: str, *, now: dt.datetime | None = None) -> dict:
    data = tables.load_week(ws, week)
    cfg = ws.config["ads"]["tracking"]
    result = checks(data["tables"]["conversion_actions"], data["tables"]["campaigns"], cfg)
    status = "fail" if any(c["status"] == "fail" for c in result) else "pass"
    out = {"schema_version": SCHEMA_VERSION, "workspace": ws.slug, "week": week, "account": ws.account(tables.sheet.SOURCE),
           "run_id": data["run_id"], "checked": record_mod.utc(now or dt.datetime.now(dt.timezone.utc)),
           "config": dict(cfg), "checks": result, "status": status}
    write_json_atomic(ws.path("data", "weeks", f"{week}.tracking.json"), out)
    return out


def lines(out: dict) -> list[str]:
    head = [f"tracking gate {out['workspace']} {out['week']}: {out['status'].upper()} (run {out['run_id']})"]
    return head + [f"- {c['name']}: {c['status']} - {c['detail']}" for c in out["checks"]]
