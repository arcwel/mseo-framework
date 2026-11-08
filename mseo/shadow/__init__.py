"""`mseo shadow compare`: the week file against the totals each report page showed at pull time.

The baseline is the platform's own numbers: each export pull saves the page's totals (`totals-<source>.txt`) beside
the export, and `page_totals` reads them. `--baseline FILE` gives them by hand instead ({source: {metric: value}}).
An optional validator: two consecutive weeks that pass show the pulls match what the reports showed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from mseo.fsutil import sha256_file, write_json_atomic
from mseo.run import record as record_mod

TOLERANCES = Path(__file__).with_name("tolerances.json")


def tolerances() -> dict:
    return {k: v for k, v in json.loads(TOLERANCES.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def within(a: float, b: float, tol: dict) -> bool:
    diff = abs(float(a) - float(b))
    if tol["kind"] == "exact":
        return diff == 0
    if tol["kind"] == "absolute":
        return diff <= tol["value"] + 1e-9
    return diff <= tol["value"] * abs(float(b)) + 1e-9


def page_totals(ws, source: str, run_id: str) -> dict | None:
    """The totals the report page showed when run_id was pulled, or None when that run saved none."""
    from mseo.browser import extract

    name = f"totals-{source}.txt"
    try:
        rec = record_mod.load(ws.path("data", "runs", run_id, "record.json"))
    except (OSError, ValueError, record_mod.RecordInvalid):
        return None
    entry = next((f for f in rec["files"] if f["path"] == name), None)
    path = ws.path("data", "runs", run_id, name)
    if entry is None or not path.is_file() or sha256_file(path) != entry["sha256"]:
        return None
    reader = extract.PAGE_TOTALS.get(source)
    return reader(path.read_text(encoding="utf-8", errors="replace")) if reader else None


def _row(source, metric, ours, page, tol, ok, note) -> dict:
    return {"source": source, "metric": metric, "mseo": ours, "page": page, "tolerance": tol, "ok": ok, "note": note}


def compare(ws, week: str, *, baseline: dict | None = None) -> dict:
    from mseo import week as week_mod

    data = week_mod.load(ws, week)
    tols = tolerances()
    rows = []
    for source, entry in data["sources"].items():
        if entry["status"] != "ok":
            rows.append(_row(source, None, None, None, None, False, "not available"))
            continue
        page = (baseline or {}).get(source) if baseline is not None else page_totals(ws, source, entry["run_id"])
        if not page:
            rows.append(_row(source, None, None, None, None, None, "no page totals"))
            continue
        for metric, ours in entry["metrics"].items():
            tol = tols.get(f"{source}.{metric}")
            if tol is None or ours is None:
                continue
            if page.get(metric) is None:
                note = "the page shows no total" if tol.get("page") == "optional" else "not on the page"
                rows.append(_row(source, metric, ours, None, tol, None, note))
                continue
            ok = within(ours, page[metric], tol)
            rows.append(_row(source, metric, ours, page[metric], tol, ok, None if ok else "outside tolerance"))
    if any(r["ok"] is False for r in rows):
        status = "fail"
    elif any(r["note"] in ("no page totals", "not on the page") for r in rows) or not any(r["ok"] for r in rows):
        status = "incomplete"
    else:
        status = "pass"
    out = {"workspace": ws.slug, "week": week, "status": status, "rows": rows,
           "baseline": "file" if baseline is not None else "page totals",
           "compared": record_mod.utc(dt.datetime.now(dt.timezone.utc))}
    write_json_atomic(ws.path("data", "weeks", f"{week}.shadow.json"), out)
    return out


def _next_week(week: str) -> str:
    year, _, num = week.partition("-W")
    y, w, _ = (dt.date.fromisocalendar(int(year), int(num), 1) + dt.timedelta(days=7)).isocalendar()
    return f"{y}-W{w:02d}"


def accepted(ws) -> list[str] | None:
    """The latest two consecutive ISO weeks that both passed, or None."""
    folder = ws.path("data", "weeks")
    results = {}
    for path in folder.glob("*.shadow.json") if folder.is_dir() else []:
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        results[got.get("week")] = got.get("status")
    passing = sorted(w for w, s in results.items() if w and s == "pass")
    pairs = [[w, _next_week(w)] for w in passing if _next_week(w) in passing]
    return pairs[-1] if pairs else None


def cmd_compare(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    baseline = None
    if args.baseline:
        src = Path(args.baseline)
        src = ws.contains(src) if src.is_absolute() else ws.path(src)  # a file inside this workspace only
        baseline = json.loads(src.read_text(encoding="utf-8"))
    out = compare(ws, args.week, baseline=baseline)
    lines = [f"{r['source']:<15} {r['metric'] or '-':<16} mseo={r['mseo']} page={r['page']} "
             f"{'ok' if r['ok'] else r['note']}" for r in out["rows"]]
    acc = accepted(ws)
    lines.append(f"{out['status']}" + (f"; accepted on {acc[0]} and {acc[1]}" if acc else ""))
    ctx.emit({**out, "accepted": acc, "messages": lines})
    return 0 if out["status"] == "pass" else 1


def register(sub, parents) -> None:
    p = sub.add_parser("shadow", help="Compare a week file with the totals the report pages showed (optional validator)")
    ssub = p.add_subparsers(dest="shadow_command", required=True, parser_class=type(p))
    c = ssub.add_parser("compare", parents=[parents.workspace, parents.json],
                        help="Compare data/weeks/<W>.json with the report pages' own totals")
    c.add_argument("--week", required=True)
    c.add_argument("--baseline", help="JSON {source: {metric: value}} given by hand instead of the page totals")
    c.set_defaults(func=cmd_compare, command_id="shadow compare")
