"""mseo.outcome/1: exactly one per scheduled job, written by the runner wrapper whatever happens."""

from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path

from mseo.fsutil import write_json_atomic
from mseo.run.record import RecordInvalid, _stamp, check_schema, utc

SCHEMA_VERSION = "mseo.outcome/1"
STATUSES = ("ok", "partial", "refused", "not_run", "killed", "failed")


def new_job_id(*, now: dt.datetime | None = None, rand: str | None = None) -> str:
    return f"{_stamp(now)}-weekly-{rand or secrets.token_hex(2)}"


def build(*, job_id: str, workspace: str, status: str, scheduled_for, started, finished, run_ids: list,
          identity_check: dict, identity_shared: bool, usage: dict | None, error: str | None) -> dict:
    out = {"schema_version": SCHEMA_VERSION, "job_id": job_id, "workspace": workspace, "status": status,
           "scheduled_for": utc(scheduled_for) if scheduled_for is not None else None, "started": utc(started),
           "finished": utc(finished), "run_ids": run_ids, "identity_check": identity_check,
           "identity_shared": identity_shared, "usage": usage, "error": error}
    check_schema("outcome.v1.json", out)
    for field in ("scheduled_for", "started", "finished"):  # the schema checks the shape; this checks the calendar
        value = out.get(field)
        if value is None and field == "scheduled_for":
            continue
        try:
            dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            raise RecordInvalid(f"outcome {job_id}: {field} {value!r} is not a real UTC time") from None
    return out


def write(outcomes_dir: Path, out: dict) -> Path:
    check_schema("outcome.v1.json", out)
    path = Path(outcomes_dir) / f"{out['job_id']}.json"
    write_json_atomic(path, out)
    return path


def load(path: Path) -> dict:
    out = json.loads(Path(path).read_text(encoding="utf-8"))
    if out.get("schema_version") != SCHEMA_VERSION:
        raise RecordInvalid(f"{path} is not {SCHEMA_VERSION}")
    check_schema("outcome.v1.json", out)
    for field in ("scheduled_for", "started", "finished"):  # the schema checks the shape; this checks the calendar
        value = out.get(field)
        if value is None and field == "scheduled_for":
            continue
        try:
            dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            raise RecordInvalid(f"{path}: {field} {value!r} is not a real UTC time") from None
    return out
