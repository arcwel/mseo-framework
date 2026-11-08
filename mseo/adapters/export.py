"""Export adapter core: take a report's own export file, move it into the run folder, parse it, record it.

A browser-driven pull finds the file the agent exported in <ws>/data/inbox/ with pick_up; a manual
export is passed with `mseo pull --file` and recorded `unverified_account` (seen_via operator), usable for the shadow
comparison but never as account-attested evidence.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import stat
from pathlib import Path

from mseo import doctor
from mseo.fsutil import sha256_file
from mseo.run import record as run_record

PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")


class ExportError(Exception):
    """No usable export: nothing arrived, too many files, unreadable, or the wrong period."""


class LayoutChanged(ExportError):
    """The export's columns are not the ones this parser knows: the report changed."""


def inbox(ws) -> Path:
    return ws.path("data", "inbox")


def pick_up(ws, since: float, exclude: set[str] = frozenset()) -> Path:
    """The one complete file that arrived in the inbox at or after `since` (epoch seconds). A browser pull passes its
    own snapshot names in `exclude`: the agent's snapshot-*.txt files are not the export."""
    folder = inbox(ws)
    new = sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and not p.name.endswith(PARTIAL_SUFFIXES)
                 and not p.name.startswith((".", "snapshot-")) and p.name not in exclude
                 and p.stat().st_mtime >= since) if folder.is_dir() else []
    if not new:
        raise ExportError(f"no export arrived in {folder}")
    if len(new) > 1:
        raise ExportError(f"{len(new)} new files in {folder} ({', '.join(p.name for p in new)}); expected one")
    return new[0]


def export_name(src: Path) -> str:
    """The fixed name an export is kept under, so it can never take the name of a file the run writes later
    (record.json, dates.csv) and be overwritten."""
    suffix = Path(src).suffix.lower()
    return "export" + (suffix if suffix[1:].isalnum() else "")


def ingest(ws, src: Path, run_dir: Path, *, name: str | None = None) -> dict:
    """Move src (a real file inside the workspace, never a symlink) into run_dir, as `name` when given; return its
    file entry. A name already in run_dir is refused, never overwritten."""
    src = Path(src)
    if src.is_symlink():  # a link would move its target (customer.json) into the run
        raise ExportError(f"{src.name} is a symlink; only a real file is taken")
    real = ws.contains(src)
    st = os.lstat(real)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ExportError(f"{src.name} is not a plain file with one link; not taken")
    run_dir = ws.path(run_dir.relative_to(ws.root)) if run_dir.is_absolute() else ws.path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / (name or real.name)
    if dest.exists() or dest.is_symlink():
        raise ExportError(f"{dest.name} is already in the run folder; not overwritten")
    os.replace(real, dest)
    return {"path": dest.name, "sha256": sha256_file(dest), "rows": None, "truncated": False}


def write_table(run_dir: Path, name: str, fields: list[str], rows: list[dict], truncated: bool = False) -> dict:
    path = run_dir / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return {"path": name, "sha256": sha256_file(path), "rows": len(rows), "truncated": truncated}


def number(text: str) -> float | int:
    """'1,234' -> 1234, '3.20%' -> 0.032, '8.5' -> 8.5."""
    t = text.strip().replace(",", "")
    if t.endswith("%"):
        return round(float(t[:-1]) / 100, 6)
    try:
        return int(t)
    except ValueError:
        return float(t)


def resolve_period(ws, *, week: str | None, start: str | None, end: str | None) -> dict:
    """--week, or --start/--end, or the last complete ISO week (Monday to Sunday) before today."""
    tz = ws.config["timezone"]
    if week:
        return run_record.week_period(week, tz)
    if start and end:
        if dt.date.fromisoformat(end) < dt.date.fromisoformat(start):
            raise ExportError(f"--end {end} is before --start {start}")
        return {"start": start, "end": end, "tz": tz}
    if start or end:
        raise ExportError("give both --start and --end, or --week")
    today = doctor.today()
    last_monday = today - dt.timedelta(days=today.weekday() + 7)
    return {"start": last_monday.isoformat(), "end": (last_monday + dt.timedelta(days=6)).isoformat(), "tz": tz}


def existing(ws, source: str, period: dict) -> list[str]:
    """Run ids of records already holding this source and period (a failed pull holds nothing, so a retry runs)."""
    out = []
    runs = ws.path("data", "runs")
    if not runs.is_dir():
        return out
    for p in sorted(runs.glob("*/record.json")):
        try:
            rec = run_record.load(p)
        except Exception:  # noqa: BLE001 - an unreadable record is reported by the watchdog, not here
            continue
        holds_data = bool(rec.get("files")) or rec["status"] == "empty"
        if holds_data and rec["source"] == source and rec["period"] and rec["period"]["start"] == period["start"] \
                and rec["period"]["end"] == period["end"]:
            out.append(rec["run_id"])
    return out


def within_lag(period: dict, lag_days: int) -> bool:
    return doctor.today() < dt.date.fromisoformat(period["end"]) + dt.timedelta(days=lag_days)
