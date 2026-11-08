"""`mseo ads audit`: data-driven checks over the week's Scripts Sheet tables.

Each check returns {name, status: pass|warn|fail|unchecked, detail, evidence: rows}. `fail` means money is leaving
now (wasted spend, dead hours, out-of-area spend, a capped converting campaign, no calls where calls are expected);
`warn` is a quality signal to act on next; `unchecked` names what the tables cannot show. Thresholds come from
customer.json `ads.*`; the share limits below are the framework's own and are reported in the output."""

from __future__ import annotations

import datetime as dt
import re

from mseo.ads import tables
from mseo.fsutil import write_json_atomic
from mseo.run import record as record_mod

SCHEMA_VERSION = "mseo.ads_audit/1"
CHECKS = ("wasted_spend", "broad_match_leakage", "ai_max_share", "rsa_assets", "rsa_pins", "ad_schedule", "locations",
          "budget_pacing", "brand_exclusions", "call_asset")
BROAD_LEAK_WARN = 0.2  # share of search-term cost on broad-match terms with no conversion
AI_MAX_WARN = 0.2  # share of search-term cost on AI Max matches and the "Other" bucket
LOST_IS_BUDGET = 0.2  # search budget lost impression share above this is a capped campaign
BUDGET_RAISE = 1.2  # the proposed budget step for a capped, converting campaign
DEAD_HOUR_CLICKS_DIVISOR = 4  # a dead hour needs ads.min_clicks_before_pause / this many clicks besides the spend
BROAD = "BROAD"
AI_MAX = "AI_MAX"
OTHER_BUCKET = "other search terms"  # Google's aggregate row: no negative can target it
LOW, LEARNING = "LOW", "LEARNING"
HEADLINE = "HEADLINE"
NO_PIN = ("", "UNSPECIFIED", "UNKNOWN")
GEO_RESOURCE = "geoTargetConstants/"  # the script's city segment is a resource name unless a state name is added
STATE_IN_ADDRESS = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$")
STATE_IN_PLACE = re.compile(r",\s*([A-Z]{2})\s*$")
EVIDENCE_LINES = 8


def _n(v) -> float:
    return float(v or 0)


def _check(name: str, status: str, detail: str, evidence: list[dict] | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "evidence": evidence or []}


# ---------------------------------------------------------------- finders (pure; `batch.propose` reuses them)

def wasted_terms(search_terms: list[dict], threshold: float) -> list[dict]:
    return [r for r in search_terms if _n(r["cost"]) >= threshold and _n(r["conversions"]) == 0
            and (r["search_term"] or "").strip().lower() != OTHER_BUCKET]


def broad_leak(search_terms: list[dict]) -> tuple[float, list[dict]]:
    total = sum(_n(r["cost"]) for r in search_terms)
    rows = [r for r in search_terms if BROAD in (r["match_type"] or "").upper() and _n(r["conversions"]) == 0]
    return (sum(_n(r["cost"]) for r in rows) / total if total else 0.0), rows


def ai_max(search_terms: list[dict]) -> tuple[float, list[dict]]:
    total = sum(_n(r["cost"]) for r in search_terms)
    rows = [r for r in search_terms if AI_MAX in (r["match_type"] or "").upper()
            or (r["search_term"] or "").strip().lower() == OTHER_BUCKET]
    return (sum(_n(r["cost"]) for r in rows) / total if total else 0.0), rows


def low_assets(assets: list[dict]) -> tuple[list[dict], float]:
    labelled = [a for a in assets if (a["performance_label"] or "").upper() in (LOW, LEARNING, "GOOD", "BEST")]
    low = [a for a in assets if (a["performance_label"] or "").upper() == LOW]
    learning = [a for a in assets if (a["performance_label"] or "").upper() == LEARNING]
    return low, (len(learning) / len(labelled) if labelled else 0.0)


def unpinned_groups(assets: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for a in assets:
        if (a["field_type"] or "").upper() == HEADLINE:
            groups.setdefault((a["campaign"], a["ad_group"]), []).append(a)
    return [{"campaign": c, "ad_group": g, "headlines": len(rows)} for (c, g), rows in sorted(groups.items())
            if all((a["pinned_field"] or "").upper() in NO_PIN for a in rows)]


def dead_hour_min_clicks(min_clicks: int) -> float:
    return min_clicks / DEAD_HOUR_CLICKS_DIVISOR


def dead_hours(ad_schedule: list[dict], threshold: float, min_clicks: int) -> list[dict]:
    """Hours of day that spent >= threshold with >= min_clicks / DEAD_HOUR_CLICKS_DIVISOR clicks and no conversion
    over the week: enough traffic to say the hour does not convert, not just an expensive click or two."""
    by_hour: dict[int, dict] = {}
    for r in ad_schedule:
        if r["hour"] is None:
            continue
        h = int(r["hour"])
        slot = by_hour.setdefault(h, {"hour": h, "cost": 0.0, "clicks": 0.0, "conversions": 0.0, "campaigns": set()})
        slot["cost"] += _n(r["cost"])
        slot["clicks"] += _n(r["clicks"])
        slot["conversions"] += _n(r["conversions"])
        slot["campaigns"].add(r["campaign"])
    out = []
    for h in sorted(by_hour):
        s = by_hour[h]
        if s["cost"] >= threshold and s["clicks"] >= dead_hour_min_clicks(min_clicks) and s["conversions"] == 0:
            out.append({"hour": h, "cost": round(s["cost"], 2), "clicks": s["clicks"], "conversions": 0,
                        "campaigns": sorted(s["campaigns"])})
    return out


def configured_states(ws) -> set[str]:
    states = set()
    for loc in ws.config["locations"]:
        m = STATE_IN_ADDRESS.search(loc["address"])
        if m:
            states.add(m.group(1))
    return states


US_STATES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
             "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
             "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
             "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
             "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
             "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
             "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
             "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
             "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
             "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC"}


def row_state(r: dict) -> str | None:
    """The two-letter state of a locations row: the resolved state name first, else a 'City, ST' place text."""
    name = (r.get("state") or "").strip().lower()
    if name in US_STATES:
        return US_STATES[name]
    if re.fullmatch(r"[A-Z]{2}", (r.get("state") or "").strip()):
        return r["state"].strip()
    place = (r.get("location") or "").strip()
    if not place or place.startswith(GEO_RESOURCE):
        return None
    m = STATE_IN_PLACE.search(place)
    return m.group(1) if m else None


def outside_locations(locations: list[dict], states: set[str]) -> tuple[list[dict] | None, int]:
    """(rows with spend outside the configured states, readable row count); None when no row names a state."""
    readable, outside = 0, []
    for r in locations:
        if _n(r["cost"]) <= 0:
            continue
        st = row_state(r)
        if st is None:
            continue
        readable += 1
        if st not in states:
            outside.append(r)
    return (outside if readable else None), readable


def capped_campaigns(campaigns: list[dict]) -> list[dict]:
    return [c for c in campaigns if c["lost_is_budget"] is not None and _n(c["lost_is_budget"]) > LOST_IS_BUDGET
            and (c["status"] or "").upper() == "ENABLED"]


# ---------------------------------------------------------------- the audit

def checks(ws, data: dict) -> list[dict]:
    t = data["tables"]
    cfg = ws.config["ads"]
    threshold = float(cfg["waste_threshold_usd"])
    min_clicks = int(cfg["min_clicks_before_pause"])
    out = []
    waste = wasted_terms(t["search_terms"], threshold)
    out.append(_check("wasted_spend", "fail" if waste else "pass",
                      f"{len(waste)} search term(s) with >= {threshold:g} spend and no conversion over the search-term window"
                      f" ({round(sum(_n(r['cost']) for r in waste), 2)} total)" if waste else
                      f"no search term reached {threshold:g} without a conversion", waste))
    share, rows = broad_leak(t["search_terms"])
    out.append(_check("broad_match_leakage", "warn" if share > BROAD_LEAK_WARN else "pass",
                      f"{share:.0%} of search-term cost went to broad-match terms with no conversion (warn above "
                      f"{BROAD_LEAK_WARN:.0%})", rows))
    share, rows = ai_max(t["search_terms"])
    out.append(_check("ai_max_share", "warn" if share > AI_MAX_WARN else "pass",
                      f"{share:.0%} of search-term cost came through AI Max matches or the '{OTHER_BUCKET}' bucket (warn "
                      f"above {AI_MAX_WARN:.0%})", rows))
    low, learning_share = low_assets(t["assets"])
    out.append(_check("rsa_assets", "warn" if low else "pass",
                      f"{len(low)} asset(s) rated {LOW}; {learning_share:.0%} of rated assets still {LEARNING}", low))
    pins = unpinned_groups(t["assets"])
    out.append(_check("rsa_pins", "warn" if pins else "pass",
                      f"{len(pins)} ad group(s) with headlines and no pinned field" if pins else
                      "every ad group with headlines pins at least one", pins))
    dead = dead_hours(t["ad_schedule"], threshold, min_clicks)
    floor = f">= {threshold:g} spend and >= {dead_hour_min_clicks(min_clicks):g} clicks (min_clicks_before_pause / " \
            f"{DEAD_HOUR_CLICKS_DIVISOR})"
    out.append(_check("ad_schedule", "fail" if dead else "pass",
                      f"{len(dead)} hour(s) of day with {floor} over the week and no conversion" if dead else
                      f"no hour of day reached {floor} without a conversion", dead))
    states = configured_states(ws)
    outside, readable = outside_locations(t["locations"], states)
    if not states or outside is None:
        why = ("no state in customer.json locations addresses" if not states else
               f"the location rows carry {GEO_RESOURCE} IDs and no resolved state name; update the Ads Script to the current template")
        out.append(_check("locations", "unchecked", why))
    else:
        out.append(_check("locations", "fail" if outside else "pass",
                          f"{len(outside)} location row(s) with spend outside {', '.join(sorted(states))} "
                          f"({round(sum(_n(r['cost']) for r in outside), 2)} total) of {readable} readable" if outside else
                          f"all {readable} readable location rows are in {', '.join(sorted(states))}", outside))
    capped = capped_campaigns(t["campaigns"])
    converting = [c for c in capped if _n(c["conversions"]) > 0]
    out.append(_check("budget_pacing", "fail" if converting else ("warn" if capped else "pass"),
                      f"{len(capped)} enabled campaign(s) lose more than {LOST_IS_BUDGET:.0%} impression share to budget, "
                      f"{len(converting)} of them converting" if capped else
                      f"no enabled campaign loses more than {LOST_IS_BUDGET:.0%} impression share to budget", capped))
    out.append(_check("brand_exclusions", "unchecked" if cfg["brand_exclusions_expected"] else "pass",
                      "the sheet has no negatives table; confirm the brand exclusion list in the account by hand"
                      if cfg["brand_exclusions_expected"] else "not expected (ads.brand_exclusions_expected)"))
    calls = sum(_n(c["calls"]) for c in t["campaigns"])
    if calls > 0:
        out.append(_check("call_asset", "pass", f"{int(calls)} phone call(s) reported this week"))
    else:
        expected = bool(cfg["tracking"]["phone_calls"])
        out.append(_check("call_asset", "fail" if expected else "warn",
                          "no phone calls reported" + (": the call asset or call reporting is not live" if expected else "")))
    return out


def thresholds(ws) -> dict:
    cfg = ws.config["ads"]
    return {"waste_threshold_usd": cfg["waste_threshold_usd"], "min_clicks_before_pause": cfg["min_clicks_before_pause"],
            "broad_leak_share_warn": BROAD_LEAK_WARN, "ai_max_share_warn": AI_MAX_WARN, "lost_is_budget": LOST_IS_BUDGET,
            "budget_raise": BUDGET_RAISE, "dead_hour_min_clicks": dead_hour_min_clicks(int(cfg["min_clicks_before_pause"])),
            "dead_hour_clicks_divisor": DEAD_HOUR_CLICKS_DIVISOR}


def overall(result: list[dict]) -> str:
    if any(c["status"] == "fail" for c in result):
        return "fail"
    return "warn" if any(c["status"] == "warn" for c in result) else "pass"


def audit(ws, week: str, *, now: dt.datetime | None = None) -> dict:
    data = tables.load_week(ws, week)
    result = checks(ws, data)
    out = {"schema_version": SCHEMA_VERSION, "workspace": ws.slug, "week": week, "run_id": data["run_id"],
           "period": data["period"], "audited": record_mod.utc(now or dt.datetime.now(dt.timezone.utc)),
           "thresholds": thresholds(ws), "checks": result, "status": overall(result)}
    write_json_atomic(ws.path("data", "weeks", f"{week}.ads-audit.json"), out)
    ws.path("data", "weeks", f"{week}.ads-audit.md").write_text("\n".join(markdown(out)) + "\n", encoding="utf-8")
    return out


def _row_text(row: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "") and k not in ("campaigns",))


def markdown(out: dict) -> list[str]:
    lines = [f"# Ads audit {out['workspace']} {out['week']}: {out['status'].upper()}", "",
             f"Run {out['run_id']}, period {out['period']['start']} to {out['period']['end']}.", "",
             "| check | status | detail |", "|---|---|---|"]
    lines += [f"| {c['name']} | {c['status']} | {c['detail']} |" for c in out["checks"]]
    for c in out["checks"]:
        if not c["evidence"]:
            continue
        lines += ["", f"## {c['name']} evidence ({len(c['evidence'])} rows)"]
        lines += [f"- {_row_text(r)}" for r in c["evidence"][:EVIDENCE_LINES]]
        if len(c["evidence"]) > EVIDENCE_LINES:
            lines.append(f"- ... {len(c['evidence']) - EVIDENCE_LINES} more in the JSON file")
    return lines
