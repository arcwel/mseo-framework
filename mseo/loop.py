"""`mseo loop verify-live | measure`: after-publish checks and the 28-day measurement per piece.

    mseo loop verify-live --workspace S [--gbp-seen DATE HEADLINE]
    mseo loop measure --workspace S --week W

verify-live: every content-log article published 1-14 days ago with a URL gets a GET (content/links.check_url) and
must answer 200; the entry records live_check {date, status}. Business Profile posts cannot be fetched, so a
published one stays live_check "needs_check" until `--gbp-seen DATE HEADLINE` records that someone saw it.
Exit 0 all live · 1 a URL did not answer 200 · 3 the log could not be read.

measure: for each published article, the Search Console pages.csv rows whose page equals the entry's URL (trailing
slash aside) are summed across the ok records of the four ISO weeks after the publish date into
measured_28d {clicks, impressions, weeks_counted, status}: "complete" with four weeks, "incomplete" with fewer,
"before_data" when the publish date is after the newest record. The Open-SEO rank for the entry's `query` is
recorded from the newest rankings.csv up to week W. Writes through content/log.upsert_entry (which locks).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import urllib.parse

from mseo.content import log as log_mod
from mseo.content import variation
from mseo.content.links import check_url
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.run import record as record_mod

LIVE_MIN_DAYS, LIVE_MAX_DAYS = 1, 14
MEASURE_WEEKS = 4
NEEDS_CHECK = "needs_check"
PAGES, RANKINGS = "pages.csv", "rankings.csv"


def today() -> dt.date:
    from mseo.doctor import today as doctor_today

    return doctor_today()


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def same_url(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlparse(a.strip()), urllib.parse.urlparse(b.strip())
    return ((pa.hostname or "").lower().removeprefix("www."), pa.path.rstrip("/") or "/") == \
           ((pb.hostname or "").lower().removeprefix("www."), pb.path.rstrip("/") or "/")


# ---------------------------------------------------------------- verify-live

def verify_live(ws, on: dt.date, gbp_seen=None, fetch=None) -> dict:
    fetch = fetch or check_url
    log = variation.read_log(ws)
    checked, failed, pending = [], [], []
    for a in log.get("articles", []):
        when = _date(a.get("date"))
        if a.get("status") != "published" or not a.get("url") or when is None:
            continue
        if not LIVE_MIN_DAYS <= (on - when).days <= LIVE_MAX_DAYS:
            continue
        res = fetch(a["url"])
        status = res.get("status")
        log_mod.upsert_entry(ws, "article", {"slug": a["slug"], "live_check": {"date": on.isoformat(), "status": status}})
        (checked if status == 200 else failed).append({"slug": a["slug"], "url": a["url"], "status": status, "error": res.get("error")})
    seen_key = (gbp_seen[0], gbp_seen[1].lower()) if gbp_seen else None
    for g in log.get("gbp", []):
        if g.get("status") != "published":
            continue
        key = (g.get("date"), g.get("headline", "").lower())
        if seen_key and key == seen_key:
            log_mod.upsert_entry(ws, "gbp", {"date": g["date"], "headline": g["headline"], "live_check": {"date": on.isoformat(), "status": "seen"}})
            checked.append({"gbp": key, "status": "seen"})
        elif not isinstance(g.get("live_check"), dict):
            log_mod.upsert_entry(ws, "gbp", {"date": g["date"], "headline": g["headline"], "live_check": NEEDS_CHECK})
            pending.append({"gbp": key})
    return {"checked": checked, "failed": failed, "needs_check": pending}


def cmd_verify_live(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = verify_live(ws, today(), gbp_seen=args.gbp_seen)
    except (ValueError, log_mod.LogError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    msgs = [f"live  {c.get('url') or c.get('gbp')}" for c in out["checked"]]
    msgs += [f"FAIL  {f['url']} returned {f['status']}" + (f" ({f['error']})" if f.get("error") else "") for f in out["failed"]]
    msgs += [f"needs_check  Business Profile post {p['gbp'][0]} {p['gbp'][1]!r}: mark it with --gbp-seen DATE HEADLINE" for p in out["needs_check"]]
    msgs.append(f"result: {'fail' if out['failed'] else 'pass'} ({len(out['checked'])} live, {len(out['failed'])} failed, {len(out['needs_check'])} to check)")
    ctx.emit({**out, "result": "fail" if out["failed"] else "pass", "messages": msgs})
    return EXIT_FAIL if out["failed"] else EXIT_OK


# ---------------------------------------------------------------- measure

def _ok_records(ws) -> list[dict]:
    folder = ws.path("data", "runs")
    out = []
    for path in sorted(folder.glob("*/record.json")) if folder.is_dir() else []:
        try:
            rec = record_mod.load(path)
        except (OSError, ValueError, record_mod.RecordInvalid):
            continue
        if rec.get("status") == record_mod.OK:
            out.append(rec)
    return out


def _rows(ws, rec: dict, name: str) -> list[dict]:
    if not any(f["path"] == name for f in rec["files"]):
        return []
    path = ws.path("data", "runs", rec["run_id"], name)
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _latest(recs: list[dict], source: str, name: str, period: dict | None = None, until: str | None = None):
    mine = [r for r in recs if r["source"] == source and r.get("period") and any(f["path"] == name for f in r["files"])]
    if period:
        mine = [r for r in mine if (r["period"]["start"], r["period"]["end"]) == (period["start"], period["end"])]
    if until:
        mine = [r for r in mine if r["period"]["end"] <= until]
    return max(mine, key=lambda r: r["started"], default=None)


def weeks_after(published: dt.date, tz: str) -> list[dict]:
    year, week, _ = published.isocalendar()
    monday = dt.date.fromisocalendar(year, week, 1)
    out = []
    for k in range(1, MEASURE_WEEKS + 1):
        y, w, _ = (monday + dt.timedelta(days=7 * k)).isocalendar()
        out.append(record_mod.week_period(f"{y}-W{w:02d}", tz))
    return out


def weeks_before(day: dt.date, tz: str) -> list[dict]:
    """The MEASURE_WEEKS ISO weeks before the week that holds `day`, oldest first."""
    year, week, _ = day.isocalendar()
    monday = dt.date.fromisocalendar(year, week, 1)
    out = []
    for k in range(MEASURE_WEEKS, 0, -1):
        y, w, _ = (monday - dt.timedelta(days=7 * k)).isocalendar()
        out.append(record_mod.week_period(f"{y}-W{w:02d}", tz))
    return out


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def measure_article(ws, entry: dict, recs: list[dict], week: str) -> dict | None:
    published = _date(entry.get("date"))
    url = entry.get("url")
    if entry.get("status") != "published" or not url or published is None:
        return None
    tz = ws.config["timezone"]
    until = record_mod.week_period(week, tz)["end"]
    sc = [r for r in recs if r["source"] == "search_console" and r.get("period") and r["period"]["end"] <= until]
    newest = max((r["period"]["end"] for r in sc), default=None)
    out = {"clicks": 0, "impressions": 0, "weeks_counted": 0, "weeks": [], "as_of": week}
    if newest is None or published.isoformat() > newest:
        out["status"] = "before_data"
    else:
        for period in weeks_after(published, tz):
            rec = _latest(sc, "search_console", PAGES, period)
            if rec is None:
                continue
            rows = [r for r in _rows(ws, rec, PAGES) if same_url(r.get("page", ""), url)]
            out["clicks"] += int(sum(_num(r.get("clicks")) for r in rows))
            out["impressions"] += int(sum(_num(r.get("impressions")) for r in rows))
            out["weeks_counted"] += 1
            out["weeks"].append(period["start"])
        out["status"] = "complete" if out["weeks_counted"] >= MEASURE_WEEKS else "incomplete"
    fields = {"slug": entry["slug"], "measured_28d": out}
    query = entry.get("query")
    if query:
        rec = _latest(recs, "open_seo", RANKINGS, until=until)
        if rec is not None:
            row = next((r for r in _rows(ws, rec, RANKINGS) if variation.norm(r.get("keyword", "")) == variation.norm(query)), None)
            if row is not None:
                pos = row.get("position")
                fields["rank"] = {"query": query, "position": int(_num(pos)) if pos not in (None, "") else None,
                                  "run_id": rec["run_id"], "as_of": row.get("as_of") or rec["period"]["end"]}
    return fields


def measure(ws, week: str) -> list[dict]:
    log = variation.read_log(ws)
    recs = _ok_records(ws)
    results = []
    for a in log.get("articles", []):
        fields = measure_article(ws, a, recs, week)
        if fields:
            log_mod.upsert_entry(ws, "article", fields)
            results.append(fields)
    return results


# ---------------------------------------------------------------- applied Ads items

ITEM_METRICS = ("cost", "clicks", "conversions")


def _week_label(period: dict) -> str:
    y, w, _ = dt.date.fromisoformat(period["start"]).isocalendar()
    return f"{y}-W{w:02d}"


def _campaign_totals(ws, periods: list[dict], campaigns: list[str]) -> tuple[dict, int]:
    """Sum by_campaign metrics from the week files of `periods` for `campaigns`; (totals, weeks found)."""
    from mseo import week as week_mod

    totals, found = {m: 0.0 for m in ITEM_METRICS}, 0
    for period in periods:
        try:
            data = week_mod.load(ws, _week_label(period))
        except week_mod.WeekError:
            continue
        ads = (data.get("sources") or {}).get("google_ads") or {}
        if ads.get("status") != "ok":
            continue
        found += 1
        for name, row in (ads.get("by_campaign") or {}).items():
            if campaigns and name not in campaigns:
                continue
            for m in ITEM_METRICS:
                totals[m] += _num(row.get(m))
    return {m: round(v, 2) for m, v in totals.items()}, found


def measure_item(ws, item: dict, week: str) -> dict | None:
    """Metrics for the item's campaign(s) over the 4 ISO weeks before and after applied_at."""
    applied = _date(item.get("applied_at"))
    if item.get("status") != "applied" or applied is None:
        return None
    tz = ws.config["timezone"]
    target = item.get("target") or {}
    campaigns = target.get("campaigns") or ([target["campaign"]] if target.get("campaign") and target["campaign"] != "*" else [])
    after_periods = weeks_after(applied, tz)
    before_periods = weeks_before(applied, tz)
    until = record_mod.week_period(week, tz)["end"]
    after_periods = [pp for pp in after_periods if pp["end"] <= until]
    before, nb = _campaign_totals(ws, before_periods, campaigns)
    after, na = _campaign_totals(ws, after_periods, campaigns)
    return {"before": before, "after": after, "weeks_before": nb, "weeks_after": na, "as_of": week,
            "status": "complete" if na >= MEASURE_WEEKS else ("before_data" if na == 0 else "incomplete"),
            "campaigns": campaigns or ["*"]}


def measure_batches(ws, week: str) -> list[dict]:
    """Write `measured` on every applied batch item; returns [{batch_id, n, measured}]."""
    from mseo.ads import batch as batch_mod

    folder = ws.path("data", "batches")
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if batch.get("schema_version") != "mseo.batch/1":
            continue
        changed = False
        for item in batch.get("items", []):
            m = measure_item(ws, item, week)
            if m:
                item["measured"] = m
                changed = True
                out.append({"batch_id": batch["batch_id"], "n": item["n"], "type": item["type"], "measured": m})
        if changed:
            batch_mod.save_batch(ws, batch)
    return out


def cmd_measure(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        results = measure(ws, args.week)
    except (ValueError, log_mod.LogError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    msgs = []
    for r in results:
        m = r["measured_28d"]
        rank = f", rank {r['rank']['position']}" if r.get("rank") else ""
        msgs.append(f"{r['slug']:<30} {m['status']:<12} {m['clicks']} clicks, {m['impressions']} impressions over {m['weeks_counted']} week(s){rank}")
    msgs.append(f"measured {len(results)} published article(s) as of {args.week}")
    items = measure_batches(ws, args.week)
    for it in items:
        m = it["measured"]
        msgs.append(f"batch {it['batch_id']} item {it['n']} ({it['type']}): {m['status']}; cost {m['before']['cost']} -> {m['after']['cost']}, "
                    f"conversions {m['before']['conversions']} -> {m['after']['conversions']} over {m['weeks_before']}/{m['weeks_after']} week(s)")
    msgs.append(f"measured {len(items)} applied Ads item(s)")
    ctx.emit({"week": args.week, "articles": results, "items": items, "messages": msgs})
    return EXIT_OK


def register(sub, parents) -> None:
    p = sub.add_parser("loop", help="After-publish checks: verify-live and the 28-day measurement per piece")
    lsub = p.add_subparsers(dest="loop_command", required=True, parser_class=type(p))
    v = lsub.add_parser("verify-live", parents=[parents.workspace, parents.json],
                        help="GET every article published 1-14 days ago; Business Profile posts wait for --gbp-seen")
    v.add_argument("--gbp-seen", nargs=2, metavar=("DATE", "HEADLINE"), help="Record that this Business Profile post was seen live")
    v.set_defaults(func=cmd_verify_live, command_id="loop verify-live")
    m = lsub.add_parser("measure", parents=[parents.workspace, parents.json],
                        help="Sum the four weeks of Search Console page rows after each publish into measured_28d")
    m.add_argument("--week", required=True, help="Measure as of this ISO week, e.g. 2026-W49")
    m.set_defaults(func=cmd_measure, command_id="loop measure")
