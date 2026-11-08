"""Search Console by UI export: the Performance report's zip (Dates, Queries, Pages, Countries, Devices, Search appearance).

The "Search generative AI" performance report (AI Overviews and AI Mode impressions by page, country, date and device;
launched 2026-06, rolling out; no queries, no clicks) is a second export when `accounts.search_console.tables` holds
"generative_ai": its zip is parsed by the same reader into generative_ai_*.csv. The week file reads dates.csv only.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from mseo.adapters.export import ExportError, LayoutChanged, number, write_table

SOURCE = "search_console"
LAG_DAYS = 3
UI_ROW_CAP = 1000  # the UI export holds at most this many rows per table
TABLES = {"dates": ("Dates.csv", "Date", "date"), "queries": ("Queries.csv", "Top queries", "query"),
          "pages": ("Pages.csv", "Top pages", "page"), "countries": ("Countries.csv", "Country", "country"),
          "devices": ("Devices.csv", "Device", "device"),
          "search_appearance": ("Search appearance.csv", "Search Appearance", "search_appearance")}
# Extra reports the operator can turn on in accounts.search_console.tables: table name -> (prompt, file prefix).
# The generative AI report has no fixed deep link in Google's help; the prompt opens Performance and
# picks the report by name. Its export is impressions-led: Clicks/CTR/Position columns may be absent, so the
# reader accepts any subset of METRICS there.
EXTRA = {"generative_ai": ("export_search_console_generative_ai", "generative_ai_")}
DAILY_MEMBERS = ("Chart.csv", "Dates.csv")  # the live export says Chart.csv; older ones Dates.csv
METRICS = {"Clicks": "clicks", "Impressions": "impressions", "CTR": "ctr", "Position": "position"}


def _read(z: zipfile.ZipFile, member: str, key_col: str, key: str, *, partial: bool = False) -> list[dict]:
    reader = csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8-sig"))
    cols = reader.fieldnames or []
    present = {k: v for k, v in METRICS.items() if k in cols}
    if key_col not in cols or (not partial and len(present) < len(METRICS)) or (partial and "Impressions" not in cols):
        raise LayoutChanged(f"{member} columns {cols} are not {[key_col, *METRICS]}")
    return [{key: row[key_col], **{v: number(row[k]) if k in present else None for k, v in METRICS.items()}}
            for row in reader]


def parse_zip(path: Path, *, partial: bool = False) -> dict:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExportError(f"{path.name} is not a Search Console export zip ({exc})") from None
    with z:
        names = set(z.namelist())
        daily = next((m for m in DAILY_MEMBERS if m in names), None)
        if daily is None:
            raise ExportError(f"{path.name} has no daily table ({' or '.join(DAILY_MEMBERS)}; it holds "
                              f"{', '.join(sorted(names))}); it is not a Performance report export")
        members = {**TABLES, "dates": (daily, *TABLES["dates"][1:])}
        tables = {t: _read(z, member, col, key, partial=partial) for t, (member, col, key) in members.items()
                  if member in names}
    return {"tables": tables, "truncated": {t: len(rows) >= UI_ROW_CAP and t != "dates" for t, rows in tables.items()},
            "dates": sorted(r["date"] for r in tables["dates"])}


def write(parsed: dict, run_dir: Path, *, prefix: str = "") -> list[dict]:
    return [write_table(run_dir, f"{prefix}{t}.csv", [TABLES[t][2], *METRICS.values()], rows, parsed["truncated"][t])
            for t, rows in parsed["tables"].items()]


def extra_tables(ws) -> list[str]:
    """The extra reports this workspace pulls (accounts.search_console.tables ∩ EXTRA), in EXTRA order."""
    wanted = set((ws.config["accounts"].get(SOURCE) or {}).get("tables") or ())
    return [t for t in EXTRA if t in wanted]


def parse_generative_ai(path: Path) -> dict:
    """The Search generative AI report's zip: the same tables, impressions required, the other metrics optional."""
    return parse_zip(path, partial=True)


def write_generative_ai(parsed: dict, run_dir: Path) -> list[dict]:
    return write(parsed, run_dir, prefix=EXTRA["generative_ai"][1])


def extra_prompt(table: str) -> str:
    return EXTRA[table][0]


def coverage(parsed: dict) -> tuple[str | None, str | None]:
    return (parsed["dates"][0], parsed["dates"][-1]) if parsed["dates"] else (None, None)


parse = parse_zip
