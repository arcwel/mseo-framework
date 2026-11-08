"""The week's Scripts Sheet tables, read back from the latest ok Google Ads run that holds them all.

Every file must still match its sha256 in the run record. Numbers come back typed (the adapter wrote them as
currency units and plain numbers); text stays text. The script's optional tabs (applied_log, _errors) come along
under their names when the run holds them."""

from __future__ import annotations

import csv
from pathlib import Path

from mseo.adapters import google_ads_sheet as sheet
from mseo.fsutil import sha256_file
from mseo.run import record as record_mod


class NoTables(Exception):
    """No ok run of the week holds the sheet tables. Maps to exit 3."""


def _typed(name: str, row: dict) -> dict:
    out = {}
    for _, col, kind in sheet.ALL_TABLES[name]:
        v = row.get(col)
        if kind in sheet.NUMERIC_KINDS:
            out[col] = None if v in (None, "") else float(v)
        else:
            out[col] = v if v is not None else ""
    return out


def read_table(path: Path, name: str) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return [_typed(name, r) for r in csv.DictReader(fh)]


def _records(ws) -> list[dict]:
    folder = ws.path("data", "runs")
    out = []
    for path in sorted(folder.glob("*/record.json")) if folder.is_dir() else []:
        try:
            out.append(record_mod.load(path))
        except (OSError, ValueError, record_mod.RecordInvalid):
            continue
    return out


def load_week(ws, week: str) -> dict:
    """{"run_id", "period", "week", "tables": {name: rows}} for the latest ok google_ads run of the week that holds
    every sheet table with a matching hash."""
    period = record_mod.week_period(week, ws.config["timezone"])
    mine = sorted((r for r in _records(ws) if r["source"] == sheet.SOURCE and r["status"] == "ok" and r.get("period")
                   and (r["period"]["start"], r["period"]["end"]) == (period["start"], period["end"])),
                  key=lambda r: r["started"])
    why = f"no ok {sheet.SOURCE} run for {week} holds the sheet tables; run `mseo pull --workspace {ws.slug} --source " \
          f"{sheet.SOURCE} --week {week}` with accounts.{sheet.SOURCE}.scripts_sheet_id set"
    for rec in reversed(mine):
        tables = {}
        for name, file in {**sheet.TABLE_FILES, **sheet.OPTIONAL_FILES}.items():
            entry = next((f for f in rec["files"] if f["path"] == file), None)
            if entry is None and name in sheet.OPTIONAL_TABLES:
                continue
            path = ws.path("data", "runs", rec["run_id"], file)
            if entry is None or not path.is_file() or sha256_file(path) != entry["sha256"]:
                break
            tables[name] = read_table(path, name)
        else:
            return {"run_id": rec["run_id"], "period": period, "week": week, "tables": tables,
                    "complete": bool(rec["complete"]["value"])}
    raise NoTables(why)
