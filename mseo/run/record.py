"""mseo.run/2: one module builds, validates, writes and loads every run record.

Rules beyond the JSON schema:
    files       only `ok` keeps files, except a manual export (`seen_via: operator`), which keeps them unverified
                for the shadow comparison only; `empty` and every failed status keep none
    error       null for `ok` and `empty`, a short reason for every other status
    complete    true only for `ok` and `empty`
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from mseo.fsutil import write_json_atomic

SCHEMA_VERSION = "mseo.run/2"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
RECORD_FILE = "record.json"
OK, EMPTY = "ok", "empty"
FAILED = ("wrong_account", "unverified_account", "layout_changed", "auth_failed", "budget_exceeded", "error")
STATUSES = (OK, EMPTY, *FAILED)


class RecordInvalid(ValueError):
    """A run or outcome record breaks its schema or the rules above."""


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def check_schema(name: str, rec: dict) -> None:
    errors = sorted(validator(name).iter_errors(rec), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(top level)"
        raise RecordInvalid(f"{rec.get('schema_version', '?')} record invalid at {where}: {e.message}")


def utc(moment: dt.datetime | str) -> str:
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        raise RecordInvalid("record times must be timezone-aware")
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(now: dt.datetime | None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_id(source: str, *, now: dt.datetime | None = None, rand: str | None = None) -> str:
    return f"{_stamp(now)}-{source}-{rand or secrets.token_hex(2)}"


def validate(rec: dict) -> None:
    check_schema("run.v2.json", rec)
    status = rec["status"]
    manual_export = status == "unverified_account" and rec["seen_via"] == "operator"
    if rec["files"] and status != OK and not manual_export:
        raise RecordInvalid(f"status {status} saves no files, but the record lists {len(rec['files'])}")
    if status in (OK, EMPTY) and rec["error"] is not None:
        raise RecordInvalid(f"status {status} has no error, but error is {rec['error']!r}")
    if status not in (OK, EMPTY) and not rec["error"]:
        raise RecordInvalid(f"status {status} needs an error saying why")
    if rec["complete"]["value"] and status not in (OK, EMPTY):
        raise RecordInvalid(f"complete cannot be true for status {status}")


def build(*, run_id: str, workspace: str, source: str, adapter: str, identity: str | None, period: dict | None,
          account_id_expected: str | None, account_id_seen: str | None, seen_via: str, status: str,
          complete: dict, files: list, started, finished, job_id: str | None = None, identity_shared: bool = False,
          usage: dict | None = None, imported: bool = False, error: str | None = None,
          checks: list | None = None, captures: list | None = None) -> dict:
    rec = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "job_id": job_id, "workspace": workspace,
           "source": source, "adapter": adapter, "identity": identity, "identity_shared": identity_shared,
           "period": period, "account_id_expected": account_id_expected, "account_id_seen": account_id_seen,
           "seen_via": seen_via, "status": status, "complete": complete, "files": files,
           "started": utc(started), "finished": utc(finished), "usage": usage, "imported": imported, "error": error}
    if checks is not None:
        rec["checks"] = checks
    if captures is not None:
        rec["captures"] = captures
    validate(rec)
    return rec


def write(run_dir: Path, rec: dict, *, name: str = RECORD_FILE) -> Path:
    validate(rec)
    path = Path(run_dir) / name
    write_json_atomic(path, rec)
    return path


def load(path: Path) -> dict:
    rec = json.loads(Path(path).read_text(encoding="utf-8"))
    if rec.get("schema_version") != SCHEMA_VERSION:
        found = rec.get("schema_version") or rec.get("schema") or "none"
        raise RecordInvalid(f"{path} is {found}, not {SCHEMA_VERSION}; old records are never read as evidence")
    validate(rec)
    return rec


def week_period(week: str, tz: str) -> dict:
    """'2026-W45' -> Monday to Sunday of that ISO week."""
    year, _, num = week.partition("-W")
    monday = dt.date.fromisocalendar(int(year), int(num), 1)
    return {"start": monday.isoformat(), "end": (monday + dt.timedelta(days=6)).isoformat(), "tz": tz}
