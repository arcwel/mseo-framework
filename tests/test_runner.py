"""M2 step 11 (build plan A3): locks, the weekly job, the budget and the outcome record (D1, Runner)."""

import datetime as dt
import json
import os
import subprocess
import sys
import time

import pytest

from mseo import workspace
from mseo.run import outcome, record
from mseo.runner import lock, weekly
from tests.fakes import FakeKeychain
from tests.test_export_pull import W44

NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def ws(root, state, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    path = root / "fx" / "customer.json"
    cfg = json.loads(path.read_text())
    cfg["budget"] = {"weekly_tokens": 1000}
    path.write_text(json.dumps(cfg))
    return workspace.open("fx", keychain=FakeKeychain())


# ------------------------------------------------------------------ locks

def test_workspace_lock_is_exclusive_and_names_the_holder(state):
    with lock.workspace_lock("fx") as held:
        assert held
        assert (state / "locks" / "ws-fx.lock").read_text().strip() == str(os.getpid())
        holder = subprocess.run([sys.executable, "-c", "from mseo.runner import lock\n"
                                 "import sys\nwith lock.workspace_lock('fx') as h: sys.exit(0 if h else 9)"],
                                env=os.environ, capture_output=True)
        assert holder.returncode == 9
    with lock.workspace_lock("fx") as again:
        assert again


def test_browser_lock_waits_then_gives_up(state):
    code = ("from mseo.runner import lock\nimport time\n"
            "with lock.browser_lock(wait_s=0) as h:\n    print('held', flush=True)\n    time.sleep(3)")
    child = subprocess.Popen([sys.executable, "-c", code], env=os.environ, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    t = time.monotonic()
    with lock.browser_lock(wait_s=0.5, poll_s=0.1) as h:
        assert not h and time.monotonic() - t >= 0.5
        assert lock.holder("browser") == child.pid
    child.wait()
    with lock.browser_lock(wait_s=0.5) as h:
        assert h


# ------------------------------------------------------------------ the weekly job

def rec(ws, source, status="ok", tokens=100):
    run_id = record.new_run_id(source, now=NOW)
    r = record.build(run_id=run_id, workspace=ws.slug, source=source, adapter="export", identity=None, period=W44,
                     account_id_expected="x", account_id_seen="x" if status == "ok" else None, seen_via="none",
                     status=status, complete={"value": status == "ok", "reason": None if status == "ok" else status},
                     files=[], started=NOW, finished=NOW, error=None if status in ("ok", "empty") else status,
                     usage={"tokens_in": tokens, "tokens_out": 0, "cache_read": 0, "cache_write": 0, "turns": 1,
                            "minutes": 0.1, "model": "test"})
    record.write(ws.path("data", "runs", run_id), r)
    return 0 if status in ("ok", "empty") else 1, r, status


class Pulls:
    def __init__(self, ws, statuses=None, tokens=100):
        self.ws, self.statuses, self.tokens, self.calls = ws, statuses or {}, tokens, []

    def __call__(self, source):
        def pull(ws, period, **kw):
            self.calls.append((source, kw.get("signed_in"), kw.get("job_id")))
            return rec(ws, source, self.statuses.get(source, "ok"), self.tokens)
        return pull

    def table(self, sources):
        return {s: self(s) for s in sources}


def ident(status="ok", results=None):
    def check(ws, **kw):
        res = results or {s: "ok" for s in kw["sources"]}
        return {"status": status, "run_id": None, "results": res, "details": {}, "sign_in": "ok"}
    return check


SOURCES = ["search_console", "ga4", "google_ads", "open_seo"]


def _job(ws, pulls, **kw):
    kw.setdefault("identity_check", ident())
    return weekly.run_job(ws, week="2026-W44", job_id=outcome.new_job_id(now=NOW), now=NOW,
                          pulls=pulls.table(SOURCES), **kw)


def test_all_ok_writes_an_ok_outcome_in_the_fixed_order(ws):
    p = Pulls(ws)
    out = _job(ws, p)
    assert out["status"] == "ok" and len(out["run_ids"]) == 4
    assert [c[0] for c in p.calls] == ["search_console", "ga4", "google_ads", "open_seo"]
    assert all(c[2] == out["job_id"] for c in p.calls)
    assert out["usage"]["tokens_in"] == 400
    saved = outcome.load(ws.path("data", "outcomes", f"{out['job_id']}.json"))
    assert saved == out


def test_one_failure_is_partial_and_the_rest_still_run(ws):
    p = Pulls(ws, statuses={"ga4": "layout_changed"})
    out = _job(ws, p)
    assert out["status"] == "partial" and len(p.calls) == 4


def test_refused_identity_check_runs_no_pulls(ws):
    p = Pulls(ws)
    out = _job(ws, p, identity_check=ident("refused", {"search_console": "foreign"}))
    assert out["status"] == "refused" and p.calls == []
    assert out["identity_check"] == {"search_console": "foreign"}


def test_budget_spent_marks_the_rest_budget_exceeded(ws):
    p = Pulls(ws, tokens=600)
    out = _job(ws, p)
    assert [c[0] for c in p.calls] == ["search_console", "ga4"]
    assert out["status"] == "partial"
    recs = [record.load(ws.path("data", "runs", r, "record.json")) for r in out["run_ids"]]
    assert [r["status"] for r in recs] == ["ok", "ok", "budget_exceeded", "budget_exceeded"]


def test_missing_budget_fails_the_job(ws):
    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg.pop("budget")
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    p = Pulls(w)
    out = _job(w, p)
    assert out["status"] == "failed" and "budget" in out["error"] and p.calls == []


def test_sources_checked_by_hand_sign_in_themselves(ws):
    p = Pulls(ws)
    _job(ws, p, identity_check=ident(results={"search_console": "ok", "ga4": "ok", "google_ads": "ok",
                                              "open_seo": "manual"}))
    assert {c[0]: c[1] for c in p.calls} == {"search_console": True, "ga4": True, "google_ads": True,
                                             "open_seo": False}


def test_browser_lock_held_is_not_run(ws, state):
    code = ("from mseo.runner import lock\nimport time\n"
            "with lock.browser_lock(wait_s=0) as h:\n    print('held', flush=True)\n    time.sleep(3)")
    child = subprocess.Popen([sys.executable, "-c", code], env=os.environ, stdout=subprocess.PIPE, text=True)
    child.stdout.readline()
    p = Pulls(ws)
    out = _job(ws, p, browser_wait_s=0.3)
    child.wait()
    assert out["status"] == "not_run" and str(child.pid) in out["error"] and p.calls == []


def test_existing_record_for_the_week_is_skipped_not_failed(ws):
    p = Pulls(ws)

    def exists(ws, period, **kw):
        p.calls.append(("ga4", None, None))
        return 2, None, "already recorded"

    pulls = p.table(SOURCES)
    pulls["ga4"] = exists
    out = weekly.run_job(ws, week="2026-W44", job_id=outcome.new_job_id(now=NOW), now=NOW, pulls=pulls,
                         identity_check=ident())
    assert out["status"] == "ok" and len(out["run_ids"]) == 3


def test_doctor_fails_without_a_budget(root, state):
    path = root / "fx" / "customer.json"
    cfg = json.loads(path.read_text())
    cfg.pop("budget")
    path.write_text(json.dumps(cfg))
    from mseo import doctor

    w = workspace.open("fx", keychain=FakeKeychain())
    rows = {c["check"]: c for c in doctor.run(w).checks}
    assert rows["weekly token budget"]["ok"] is False
