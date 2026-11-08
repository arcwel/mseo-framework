"""`mseo week assemble`: <ws>/data/weeks/<W>.json, schema mseo.week/1.

Each source's entry comes from its latest `ok` record whose period is the week, and only after the file's sha256
still matches the record. A source without one is `"status": "not available"`, never zero. Metrics are week totals
computed from the adapter's normalized file; GA4 totalUsers stays null because the export has no such column and
daily per-channel user counts do not add up to a week's users.
"""

from __future__ import annotations

import csv
import datetime as dt
import json

from mseo.fsutil import sha256_file, write_json_atomic
from mseo.run import record as record_mod

SCHEMA_VERSION = "mseo.week/1"
FILES = {"search_console": "dates.csv", "ga4": "traffic.csv", "google_ads": "campaigns.csv"}
GA4_SUMS = ("sessions", "engagedSessions", "keyEvents")
ADS_SUMS = ("cost", "clicks", "impressions", "conversions")


class WeekError(Exception):
    pass


def _num(text) -> float | None:
    if text in (None, ""):
        return None
    return float(str(text).replace(",", ""))


def _tidy(v: float | None) -> float | int | None:
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(v, 6)


def _sum(rows: list[dict], col: str) -> float | int | None:
    vals = [_num(r.get(col)) for r in rows]
    vals = [v for v in vals if v is not None]
    return _tidy(sum(vals)) if vals else None


def _read(path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def search_console(rows: list[dict]) -> dict:
    clicks, impressions = _sum(rows, "clicks") or 0, _sum(rows, "impressions") or 0
    weighted = sum((_num(r["position"]) or 0) * (_num(r["impressions"]) or 0) for r in rows)
    return {"metrics": {"clicks": clicks, "impressions": impressions,
                        "ctr": round(clicks / impressions, 6) if impressions else None,
                        "position": round(weighted / impressions, 6) if impressions else None}}


def ga4(rows: list[dict]) -> dict:
    channels: dict[str, list[dict]] = {}
    for r in rows:
        channels.setdefault(r["channel"], []).append(r)
    return {"metrics": {**{m: _sum(rows, m) or 0 for m in GA4_SUMS}, "totalUsers": None},
            "by_channel": {c: {m: _sum(rs, m) or 0 for m in GA4_SUMS} for c, rs in sorted(channels.items())}}


def google_ads(rows: list[dict]) -> dict:
    sums = list(ADS_SUMS) + (["calls"] if rows and "calls" in rows[0] else [])
    metrics = {m: _sum(rows, m) or 0 for m in sums}
    return {"metrics": metrics, "by_campaign": {r["campaign"]: {m: _tidy(_num(r.get(m))) for m in sums} for r in rows}}


METRICS = {"search_console": search_console, "ga4": ga4, "google_ads": google_ads}


def _records(ws) -> list[dict]:
    folder = ws.path("data", "runs")
    out = []
    for path in sorted(folder.glob("*/record.json")) if folder.is_dir() else []:
        try:
            out.append(record_mod.load(path))
        except (OSError, ValueError, record_mod.RecordInvalid):
            continue
    return out


def _not_available(reason: str) -> dict:
    return {"status": "not available", "run_id": None, "metrics": {}, "reason": reason}


def _entry(ws, source: str, recs: list[dict], period: dict) -> dict:
    mine = sorted((r for r in recs if r["source"] == source and r["status"] == "ok" and r.get("period")
                   and (r["period"]["start"], r["period"]["end"]) == (period["start"], period["end"])),
                  key=lambda r: r["started"])
    if not mine:
        return _not_available(f"no ok record for {period['start']}..{period['end']}")
    rec = mine[-1]
    name = FILES[source]
    entry = next((f for f in rec["files"] if f["path"] == name), None)
    path = ws.path("data", "runs", rec["run_id"], name)
    if entry is None or not path.is_file():
        return _not_available(f"{rec['run_id']} has no {name}")
    if sha256_file(path) != entry["sha256"]:
        return _not_available(f"{rec['run_id']}/{name} no longer matches its sha256 in the record")
    out = {"status": "ok", "run_id": rec["run_id"], "file": name, "complete": bool(rec["complete"]["value"]),
           "identity_shared": bool(rec.get("identity_shared")), **METRICS[source](_read(path))}
    if source == "google_ads":
        out.update(_ads_tables(ws, rec))
    return out


def _ads_tables(ws, rec: dict) -> dict:
    """The Scripts Sheet tables a Google Ads run holds beyond campaigns.csv, each still matching
    its sha256 in the record: {"tables": {name: {"file", "rows"}}}; nothing for a Report Editor pull."""
    from mseo.adapters import google_ads_sheet

    tables = {}
    for name, file in google_ads_sheet.TABLE_FILES.items():
        entry = next((f for f in rec["files"] if f["path"] == file), None)
        path = ws.path("data", "runs", rec["run_id"], file)
        if entry is not None and path.is_file() and sha256_file(path) == entry["sha256"]:
            tables[name] = {"file": file, "rows": entry["rows"]}
    return {"tables": tables} if len(tables) > 1 else {}  # campaigns.csv alone is the Report Editor path


def assemble(ws, week: str, *, now: dt.datetime | None = None) -> dict:
    period = record_mod.week_period(week, ws.config["timezone"])
    recs = _records(ws)
    sources = [s for s in METRICS if s in ws.enabled_sources()]
    out = {"schema_version": SCHEMA_VERSION, "workspace": ws.slug, "week": week, "period": period,
           "assembled": record_mod.utc(now or dt.datetime.now(dt.timezone.utc)),
           "sources": {s: _entry(ws, s, recs, period) for s in sources}}
    record_mod.check_schema("week.v1.json", out)
    write_json_atomic(ws.path("data", "weeks", f"{week}.json"), out)
    return out


def load(ws, week: str) -> dict:
    path = ws.path("data", "weeks", f"{week}.json")
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WeekError(f"{path} could not be read ({exc}); run `mseo week assemble` first") from exc
    record_mod.check_schema("week.v1.json", out)
    return out


def cmd_assemble(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    out = assemble(ws, args.week)
    lines = [f"{s}: {e['status']}" + (f" ({e['run_id']})" if e["run_id"] else f" ({e.get('reason')})")
             for s, e in out["sources"].items()]
    ctx.emit({**out, "messages": lines})
    return 0


def register(sub, parents) -> None:
    p = sub.add_parser("week", help="The weekly data file")
    wsub = p.add_subparsers(dest="week_command", required=True, parser_class=type(p))
    a = wsub.add_parser("assemble", parents=[parents.workspace, parents.json],
                        help="Write data/weeks/<W>.json from the week's ok run records")
    a.add_argument("--week", required=True, help="ISO week, e.g. 2026-W44")
    a.set_defaults(func=cmd_assemble, command_id="week assemble")
    from mseo import week_report

    week_report.register(wsub, parents)
