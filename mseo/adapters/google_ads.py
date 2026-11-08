"""Google Ads by Report Editor CSV: the campaigns table for the period .

The identity's Ads UI shows no date picker on the campaigns page, so the agent opens the table in Report Editor, sets
the period there and downloads `.csv`. Line 1 is the report name, line 2 the date range ("October 26, 2026 -
November 1, 2026"), then the header and one row per campaign. The CSV carries no customer ID: the account is checked
from the page header snapshot before the file is kept.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from mseo.adapters.export import ExportError, LayoutChanged, number, write_table

SOURCE = "google_ads"
LAG_DAYS = 3  # conversions keep arriving for a few days
COLUMNS = {"campaign": "Campaign", "campaign_type": "Campaign type", "status": "Campaign status",
           "currency": "Currency code", "cost": "Cost", "clicks": "Clicks", "impressions": "Impr.",
           "conversions": "Conversions"}
OPTIONAL = {"calls": "Phone calls"}  # added by the prompt; an account without call reporting has no such column
TEXT = ("campaign", "campaign_type", "status", "currency")
FIELDS = [*COLUMNS, *OPTIONAL]


def _range(line: str) -> tuple[str, str]:
    parts = line.strip().strip('"').split(" - ")
    try:
        start, end = (dt.datetime.strptime(p.strip(), "%B %d, %Y").date().isoformat() for p in parts)
    except ValueError:
        raise ExportError(f"line 2 {line.strip()[:60]!r} is not a date range; not a Report Editor CSV") from None
    return start, end


def parse(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"{path.name} could not be read ({exc})") from None
    if len(lines) < 3:
        raise ExportError(f"{path.name} is too short to be a Report Editor CSV")
    start, end = _range(lines[1])
    reader = csv.DictReader(lines[2:])
    cols = reader.fieldnames or []
    missing = [c for c in COLUMNS.values() if c not in cols]
    if missing:
        raise LayoutChanged(f"{path.name} has no {missing} column(s) (columns: {cols})")
    rows = []
    for row in reader:
        first = (row.get(cols[0]) or "").strip()
        if not row.get("Campaign") or first.startswith("Total"):
            continue
        out = {k: (row[c] if k in TEXT else number(row[c] or "0")) for k, c in COLUMNS.items()}
        out.update({k: (number(row[c] or "0") if c in cols else None) for k, c in OPTIONAL.items()})
        rows.append(out)
    return {"start": start, "end": end, "rows": rows}


def write(parsed: dict, run_dir: Path) -> list[dict]:
    return [write_table(run_dir, "campaigns.csv", FIELDS, parsed["rows"])]


def coverage(parsed: dict) -> tuple[str | None, str | None]:
    return parsed["start"], parsed["end"]
