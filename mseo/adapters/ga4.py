"""GA4 by UI export: the Traffic acquisition report as CSV, by date and session primary channel group.

Extra reports, when `accounts.ga4.tables` names them: landing_pages (the Landing page report: Landing page, Sessions,
Engaged sessions, Key events) and key_events (the Events report filtered to key events: Event name, Event count,
Key events). Each is its own prompt and CSV; the week file reads traffic.csv only.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from mseo.adapters.export import ExportError, LayoutChanged, number, write_table

SOURCE = "ga4"
LAG_DAYS = 2
COLUMNS = {
    "date": ("Date",),
    "channel": ("Session primary channel group (Default Channel Group)", "Session default channel group"),
    "sessions": ("Sessions",),
    "engagedSessions": ("Engaged sessions",),
    "keyEvents": ("Key events",),
    "totalUsers": ("Total users", "Users"),
}
OPTIONAL = {"totalUsers"}  # the live report has no users column; users do not add up across days
FIELDS = ["date", "channel", "sessions", "totalUsers", "engagedSessions", "keyEvents"]


def _date(text: str) -> str:
    return dt.datetime.strptime(text.strip(), "%Y%m%d").date().isoformat()


def _read_export(path: Path, columns: dict, optional: set):
    """(meta, DictReader rows, picked column names) for any GA4 UI export: '# Start date' / '# End date' lines, then
    the table. LayoutChanged when a required column is missing."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"{path.name} could not be read ({exc})") from None
    meta, body = {}, []
    for line in lines:
        if line.startswith("#"):
            key, sep, value = line[1:].partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip()
        elif line.strip() or body:
            body.append(line)
    if "start date" not in meta or "end date" not in meta:
        raise ExportError(f"{path.name} has no '# Start date' / '# End date' lines; it is not a GA4 export")
    reader = csv.DictReader(body)
    cols = reader.fieldnames or []
    pick = {}
    for name, options in columns.items():
        found = next((c for c in options if c in cols), None)
        if not found and name in optional:
            continue
        if not found:
            raise LayoutChanged(f"{path.name} has no {options[0]!r} column (columns: {cols})")
        pick[name] = found
    return meta, reader, pick


def parse_csv(path: Path) -> dict:
    meta, reader, pick = _read_export(path, COLUMNS, OPTIONAL)
    rows = []
    for row in reader:
        if not row.get(pick["date"]) or not row[pick["date"]].strip().isdigit():
            continue  # totals or blank lines
        rows.append({"date": _date(row[pick["date"]]), "channel": row[pick["channel"]],
                     **{k: number(row[pick[k]]) if k in pick else None
                        for k in ("sessions", "engagedSessions", "keyEvents", "totalUsers")}})
    return {"start": _date(meta["start date"]), "end": _date(meta["end date"]), "rows": rows}


def write(parsed: dict, run_dir: Path) -> list[dict]:
    return [write_table(run_dir, "traffic.csv", FIELDS, [{k: r[k] for k in FIELDS} for r in parsed["rows"]])]


# ---------------------------------------------------------------- extra tables (accounts.ga4.tables)

LANDING_COLUMNS = {"landingPage": ("Landing page", "Landing page + query string"), "sessions": ("Sessions",),
                   "engagedSessions": ("Engaged sessions",), "keyEvents": ("Key events",)}
KEY_EVENT_COLUMNS = {"eventName": ("Event name",), "eventCount": ("Event count",), "keyEvents": ("Key events",)}
LANDING_FIELDS = ["landingPage", "sessions", "engagedSessions", "keyEvents"]
KEY_EVENT_FIELDS = ["eventName", "eventCount", "keyEvents"]


def _parse_dimension_table(path: Path, columns: dict, dimension: str) -> dict:
    meta, reader, pick = _read_export(path, columns, set())
    rows = []
    for row in reader:
        label = (row.get(pick[dimension]) or "").strip()
        if not label or label.lower() in ("grand total", "totals", "total"):
            continue
        rows.append({dimension: label, **{k: number(row[pick[k]]) for k in columns if k != dimension}})
    return {"start": _date(meta["start date"]), "end": _date(meta["end date"]), "rows": rows}


def parse_landing_pages(path: Path) -> dict:
    """The Landing page report export: one row per landing page."""
    return _parse_dimension_table(path, LANDING_COLUMNS, "landingPage")


def parse_key_events(path: Path) -> dict:
    """The Events report export filtered to key events: one row per event name. Rows with zero key events are kept
    (the filter is the agent's; Python records what arrived)."""
    return _parse_dimension_table(path, KEY_EVENT_COLUMNS, "eventName")


def write_landing_pages(parsed: dict, run_dir: Path) -> list[dict]:
    return [write_table(run_dir, "landing_pages.csv", LANDING_FIELDS, parsed["rows"])]


def write_key_events(parsed: dict, run_dir: Path) -> list[dict]:
    return [write_table(run_dir, "key_events.csv", KEY_EVENT_FIELDS, parsed["rows"])]


# table name -> prompt, parser, writer and the GA4 explorer report id (`r=` in the report URL; a starting point
# the agent checks on the page, like the traffic report's lifecycle-traffic-acquisition-v2).
EXTRA = {
    "landing_pages": {"prompt": "export_ga4_landing_pages", "parse": parse_landing_pages, "write": write_landing_pages,
                      "report": "lifecycle-landing-page"},
    "key_events": {"prompt": "export_ga4_key_events", "parse": parse_key_events, "write": write_key_events,
                   "report": "lifecycle-events"},
}


def extra_tables(ws) -> list[str]:
    """The extra reports this workspace pulls (accounts.ga4.tables ∩ EXTRA), in EXTRA order."""
    wanted = set((ws.config["accounts"].get(SOURCE) or {}).get("tables") or ())
    return [t for t in EXTRA if t in wanted]


def coverage(parsed: dict) -> tuple[str | None, str | None]:
    return parsed["start"], parsed["end"]


parse = parse_csv
