"""M2 step 2: run record mseo.run/2 and outcome record mseo.outcome/1 (D1, Run record and Outcome record)."""

import datetime as dt
import json
import re

import pytest

from mseo import cli, doctor, workspace
from mseo.run import outcome, record
from tests.fakes import FakeKeychain

NOW = dt.datetime(2026, 11, 9, 14, 30, 5, tzinfo=dt.timezone.utc)
PERIOD = {"start": "2026-11-02", "end": "2026-11-08", "tz": "America/Los_Angeles"}


def ok_record(**over):
    base = dict(workspace="fx", source="search_console", adapter="export", identity="agent@fx.example",
                period=PERIOD, account_id_expected="sc-domain:fx.example", account_id_seen="sc-domain:fx.example",
                seen_via="page:sc_property_selector", status="ok", complete={"value": True, "reason": None},
                files=[{"path": "dates.csv", "sha256": "a" * 64, "rows": 7, "truncated": False}],
                started=NOW, finished=NOW, run_id=record.new_run_id("search_console", now=NOW, rand="beef"))
    base.update(over)
    return record.build(**base)


# ------------------------------------------------------------------ run ids

def test_run_id_format():
    rid = record.new_run_id("ga4", now=NOW, rand="0a1b")
    assert rid == "20261109T143005Z-ga4-0a1b"
    assert re.fullmatch(r"\d{8}T\d{6}Z-ga4-[0-9a-f]{4}", record.new_run_id("ga4"))


def test_job_id_format():
    assert outcome.new_job_id(now=NOW, rand="c0de") == "20261109T143005Z-weekly-c0de"


# ------------------------------------------------------------------ build and validate

def test_a_complete_ok_record_validates_with_every_field():
    rec = ok_record()
    record.validate(rec)
    assert rec["schema_version"] == "mseo.run/2"
    assert rec["imported"] is False and rec["identity_shared"] is False and rec["error"] is None
    assert rec["usage"] is None and rec["job_id"] is None
    assert rec["started"].endswith("Z")


@pytest.mark.parametrize("status", ["wrong_account", "unverified_account", "layout_changed", "auth_failed",
                                    "budget_exceeded", "error"])
def test_failed_statuses_save_no_files_and_need_an_error(status):
    with pytest.raises(record.RecordInvalid, match="files"):
        ok_record(status=status, error="x")
    with pytest.raises(record.RecordInvalid, match="error"):
        ok_record(status=status, files=[])
    record.validate(ok_record(status=status, files=[], error="why", complete={"value": False, "reason": status}))


def test_empty_keeps_no_files():
    record.validate(ok_record(status="empty", files=[]))
    with pytest.raises(record.RecordInvalid, match="files"):
        ok_record(status="empty")


def test_manual_export_may_keep_files_while_unverified():
    rec = ok_record(status="unverified_account", seen_via="operator", account_id_seen=None, error="manual export",
                    complete={"value": False, "reason": "manual export"})
    record.validate(rec)


def test_unknown_status_and_unknown_field_are_rejected():
    with pytest.raises(record.RecordInvalid):
        ok_record(status="failed")
    rec = ok_record()
    rec["surprise"] = 1
    with pytest.raises(record.RecordInvalid):
        record.validate(rec)


def test_seen_via_must_name_how():
    with pytest.raises(record.RecordInvalid):
        ok_record(seen_via="url")


def test_complete_is_never_true_for_a_failed_pull():
    with pytest.raises(record.RecordInvalid, match="complete"):
        ok_record(status="error", files=[], error="x")


def test_usage_is_tokens_not_dollars():
    rec = ok_record(usage={"tokens_in": 10, "tokens_out": 5, "cache_read": 100, "cache_write": 50, "turns": 3,
                           "minutes": 0.5, "model": "claude-haiku-5-5"})
    record.validate(rec)
    bad = dict(rec, usage={**rec["usage"], "usd": 1.08})
    with pytest.raises(record.RecordInvalid):
        record.validate(bad)


# ------------------------------------------------------------------ write and load

def test_write_and_load_round_trip(tmp_path):
    rec = ok_record()
    path = record.write(tmp_path / rec["run_id"], rec)
    assert path.name == "record.json"
    assert record.load(path) == rec


def test_load_refuses_v1(tmp_path):
    p = tmp_path / "old.run.json"
    p.write_text(json.dumps({"schema": "mseo.run/1", "status": "ok"}))
    with pytest.raises(record.RecordInvalid, match="mseo.run/2"):
        record.load(p)


# ------------------------------------------------------------------ outcome

def test_outcome_statuses_and_fields(tmp_path):
    job = outcome.new_job_id(now=NOW, rand="c0de")
    out = outcome.build(job_id=job, workspace="fx", status="partial", scheduled_for=NOW, started=NOW, finished=NOW,
                        run_ids=["r1"], identity_check={"ga4": "ok"}, identity_shared=False, usage=None, error="ga4: error")
    path = outcome.write(tmp_path, out)
    assert path == tmp_path / f"{job}.json"
    assert outcome.load(path)["status"] == "partial"
    with pytest.raises(record.RecordInvalid):
        outcome.build(job_id=job, workspace="fx", status="weird", scheduled_for=NOW, started=NOW, finished=NOW,
                      run_ids=[], identity_check={}, identity_shared=False, usage=None, error=None)


# ------------------------------------------------------------------ M1 writers move to v2

def test_doctor_preflight_record_is_v2(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    report = doctor.Report(period="2026-W45")
    report.checks.append({"check": "x", "ok": True, "detail": ""})
    doctor.write_record(ws, report)
    rec = json.loads(ws.path("data", "weekly", "2026-W45", "preflight.run.json").read_text())
    record.validate(rec)
    assert rec["source"] == "preflight" and rec["status"] == "ok"
    assert rec["period"]["start"] == "2026-11-02" and rec["period"]["end"] == "2026-11-08"
    assert "schema" not in rec


def test_capture_verify_record_is_v2(root):
    cli.main(["capture", "verify", str(root / "fx" / "captures" / "ok"), "--workspace", "fx",
              "--source", "google_ads", "--json"], keychain=FakeKeychain())
    rec = json.loads((root / "fx" / "captures" / "ok" / "google_ads.run.json").read_text())
    record.validate(rec)
    assert rec["adapter"] == "capture" and rec["seen_via"] == "page:capture_meta"
