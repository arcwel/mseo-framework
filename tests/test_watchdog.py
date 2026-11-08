"""M2 step 12 (build plan A4): the watchdog table, alert keys and the daily summary (D1, Watchdog and alerts)."""

import datetime as dt
import json

import pytest

from mseo import cli, watchdog, workspace
from mseo.run import outcome, record
from mseo.runner import schedule
from tests.conftest import FX
from tests.fakes import FakeKeychain

FROM, TO = "alerts@ops.example", "operator@ops.example"


@pytest.fixture
def env(root, tmp_path, monkeypatch):
    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("MSEO_LAUNCH_AGENTS", str(agents))
    cfg = tmp_path / "alerts.toml"
    cfg.write_text(f'to = "{TO}"\nfrom = "{FROM}"\ntimezone = "{FX["timezone"]}"\n')
    monkeypatch.setenv("MSEO_ALERTS_CONFIG", str(cfg))
    return {"root": root, "agents": agents, "state": tmp_path / "state"}


def _ws(slug="fx"):
    return workspace.open(slug, keychain=FakeKeychain())


def _schedule(env, slug="fx"):
    env["agents"].mkdir(exist_ok=True)
    (env["agents"] / f"{schedule.label(slug)}.plist").write_text("<plist/>")


def _outcome(ws, status, *, started, scheduled_for=None, shared=False, job=None):
    out = outcome.build(job_id=job or outcome.new_job_id(now=started), workspace=ws.slug, status=status,
                        scheduled_for=scheduled_for, started=started, finished=started, run_ids=[],
                        identity_check={}, identity_shared=shared, usage=None,
                        error=None if status == "ok" else f"{status} for the test")
    outcome.write(ws.path("data", "outcomes"), out)
    return out


def _record(ws, source, status, *, started, end: dt.date | None = None):
    run_id = record.new_run_id(source, now=started)
    end = end or (started.date() - dt.timedelta(days=1))
    period = {"start": (end - dt.timedelta(days=6)).isoformat(), "end": end.isoformat(), "tz": FX["timezone"]}
    rec = record.build(run_id=run_id, workspace=ws.slug, source=source, adapter="browser",
                       identity=FX["identity"]["email"], period=period,
                       account_id_expected=ws.account(source), account_id_seen=ws.account(source) if status == "ok" else None,
                       seen_via="page:test_header" if status == "ok" else "none", status=status,
                       complete={"value": status == "ok", "reason": None if status == "ok" else status}, files=[],
                       started=started, finished=started, error=None if status in ("ok", "empty") else "for the test")
    record.write(ws.path("data", "runs", run_id), rec)
    return rec


def _healthy(ws, now):
    slot = schedule.last_slot(ws, now)
    _outcome(ws, "ok", started=slot + dt.timedelta(minutes=2), scheduled_for=slot)
    for s in watchdog.scheduled_sources(ws):
        _record(ws, s, "ok", started=slot + dt.timedelta(minutes=5))
    return slot


def _conds(findings, slug="fx"):
    return {(f["condition"], f["source"]) for f in findings if f["workspace"] == slug}


NOW = dt.datetime(2026, 11, 10, 17, 0, tzinfo=dt.timezone.utc)  # a Tuesday


# ------------------------------------------------------------------ the table

def test_a_healthy_scheduled_workspace_has_no_findings(env):
    _schedule(env)
    _healthy(_ws(), NOW)
    assert _conds(watchdog.scan(NOW)) == set()


def test_an_unscheduled_workspace_is_never_not_run_or_stale(env):
    assert _conds(watchdog.scan(NOW)) == set()


def test_no_outcome_six_hours_after_the_slot_is_not_run(env):
    _schedule(env)
    ws = _ws()
    slot = schedule.last_slot(ws, NOW)
    assert ("not_run", None) in _conds(watchdog.scan(slot + dt.timedelta(hours=6, minutes=1)))
    assert ("not_run", None) not in _conds(watchdog.scan(slot + dt.timedelta(hours=5)))


def test_an_outcome_with_status_not_run_is_not_run(env):
    _schedule(env)
    ws = _ws()
    slot = _healthy(ws, NOW)
    _outcome(ws, "not_run", started=slot + dt.timedelta(hours=2), scheduled_for=slot)
    assert ("not_run", None) in _conds(watchdog.scan(NOW))


@pytest.mark.parametrize("status", ["refused", "killed", "failed", "partial"])
def test_a_bad_latest_outcome_is_reported(env, status):
    ws = _ws()
    _outcome(ws, "ok", started=NOW - dt.timedelta(days=8))
    _outcome(ws, status, started=NOW - dt.timedelta(hours=3))
    assert (status, None) in _conds(watchdog.scan(NOW))


def test_only_the_latest_outcome_counts(env):
    ws = _ws()
    _outcome(ws, "failed", started=NOW - dt.timedelta(days=8))
    _outcome(ws, "ok", started=NOW - dt.timedelta(hours=3))
    assert _conds(watchdog.scan(NOW)) == set()


@pytest.mark.parametrize("status", ["auth_failed", "wrong_account", "unverified_account", "layout_changed",
                                    "budget_exceeded", "error"])
def test_a_bad_latest_record_is_reported_per_source(env, status):
    ws = _ws()
    _record(ws, "ga4", "ok", started=NOW - dt.timedelta(days=8))
    _record(ws, "ga4", status, started=NOW - dt.timedelta(hours=3))
    assert (status, "ga4") in _conds(watchdog.scan(NOW))


def test_a_source_with_no_recent_ok_record_is_stale(env):
    _schedule(env)
    ws = _ws()
    slot = schedule.last_slot(ws, NOW)
    _outcome(ws, "ok", started=slot + dt.timedelta(minutes=2), scheduled_for=slot)
    for s in watchdog.scheduled_sources(ws):
        old = (NOW - dt.timedelta(days=9)).date() if s == "search_console" else None
        _record(ws, s, "ok", started=slot + dt.timedelta(minutes=5), end=old)
    found = _conds(watchdog.scan(NOW))
    assert ("stale", "search_console") in found and ("stale", "ga4") not in found


def test_late_and_shared_identity_are_summary_only(env):
    _schedule(env)
    ws = _ws()
    slot = schedule.last_slot(ws, NOW)
    for s in watchdog.scheduled_sources(ws):
        _record(ws, s, "ok", started=slot + dt.timedelta(hours=2))
    _outcome(ws, "ok", started=slot + dt.timedelta(hours=2), scheduled_for=slot, shared=True)
    found = watchdog.scan(NOW)
    assert _conds(found) == {("late", None), ("shared_identity", None)}
    assert not any(f["alert"] for f in found)


def test_scan_never_touches_the_keychain(env, monkeypatch):
    """The watchdog holds no customer credentials: opening a workspace for it must not reach the Keychain."""
    from mseo import secrets

    def refuse(*a, **kw):
        raise AssertionError("the watchdog reached the Keychain")

    monkeypatch.setattr(secrets, "Keychain", refuse)
    _outcome(_ws(), "failed", started=NOW - dt.timedelta(hours=3))
    assert ("failed", None) in _conds(watchdog.scan(NOW))


# ------------------------------------------------------------------ alert keys and the summary

class Outbox(list):
    def __call__(self, msg):
        self.append(msg)


def test_a_new_key_is_emailed_once_then_daily(env):
    ws = _ws()
    _outcome(ws, "failed", started=NOW - dt.timedelta(hours=3))
    box = Outbox()
    watchdog.run(NOW, sender=box)
    alerts = [m for m in box if "alert" in m["Subject"].lower()]
    assert len(alerts) == 1 and "fx" in alerts[0]["Subject"] and "failed" in alerts[0].get_content()
    box.clear()
    watchdog.run(NOW + dt.timedelta(hours=12), sender=box)
    assert not [m for m in box if "alert" in m["Subject"].lower()]
    watchdog.run(NOW + dt.timedelta(hours=25), sender=box)
    assert len([m for m in box if "alert" in m["Subject"].lower()]) == 1


def test_a_cleared_key_goes_in_the_next_summary(env):
    ws = _ws()
    bad = _outcome(ws, "failed", started=NOW - dt.timedelta(hours=3))
    watchdog.run(NOW, sender=Outbox())
    ws.path("data", "outcomes", f"{bad['job_id']}.json").unlink()
    _outcome(ws, "ok", started=NOW)
    box = Outbox()
    watchdog.run(NOW + dt.timedelta(days=1), sender=box)
    summary = [m for m in box if "summary" in m["Subject"].lower()]
    assert summary and "cleared" in summary[0].get_content() and "failed" in summary[0].get_content()


def test_the_summary_goes_once_a_day_even_when_all_is_well(env):
    tz = watchdog.operator_tz()
    morning = dt.datetime.combine(NOW.astimezone(tz).date(), dt.time(9, 0), tzinfo=tz)
    box = Outbox()
    watchdog.run(morning, sender=box)
    assert [m["Subject"] for m in box if "summary" in m["Subject"].lower()]
    box.clear()
    watchdog.run(morning + dt.timedelta(hours=12), sender=box)
    assert box == []
    watchdog.run(morning - dt.timedelta(hours=2) + dt.timedelta(days=1), sender=box)  # 07:00 next day: not yet
    assert box == []


def test_emails_go_from_and_to_the_configured_addresses(env):
    box = Outbox()
    watchdog.run(dt.datetime(2026, 11, 10, 9, 30, tzinfo=watchdog.operator_tz()), sender=box)
    assert box and all(m["From"] == FROM and m["To"] == TO for m in box)


def test_dry_run_sends_nothing_and_keeps_state(env):
    ws = _ws()
    _outcome(ws, "failed", started=NOW - dt.timedelta(hours=3))
    result = watchdog.run(NOW, sender=None, dry_run=True)
    assert result["alerts"] and not (env["state"] / "alerts.json").exists()


def test_missing_password_fails_before_anything_is_sent(env):
    with pytest.raises(watchdog.AlertsNotConfigured, match="mseo.alerts.smtp"):
        watchdog.smtp_sender(keychain=FakeKeychain())


def test_cli_dry_run_prints_findings(env, capsys):
    _outcome(_ws(), "killed", started=NOW - dt.timedelta(hours=3))
    code = cli.main(["watchdog", "--dry-run", "--json"], keychain=FakeKeychain())
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and any(f["condition"] == "killed" for f in out["findings"])


def test_alerts_password_reads_stdin_and_stores_it(env):
    import io

    kc = FakeKeychain()
    assert cli.main(["alerts", "password"], keychain=kc, stdin=io.StringIO("abcd efgh ijkl mnop\n")) == 0
    assert kc.get(watchdog.KEYCHAIN_SERVICE, FROM) == {"app_password": "abcd efgh ijkl mnop"}
    send = watchdog.smtp_sender(keychain=kc)
    assert callable(send)


# ------------------------------------------------------------------ Sonnet pentest 2026-11-07, Medium 3

def _raw_outcome(ws, started):
    out = _outcome(ws, "ok", started=NOW - dt.timedelta(hours=1))
    path = ws.path("data", "outcomes", f"{out['job_id']}.json")
    path.write_text(json.dumps({**out, "started": started}))
    return path


def test_an_outcome_with_an_impossible_date_is_invalid(env):
    for bad in ("2026-13-45T99:99:99Z", "2026-02-30T10:00:00Z"):
        with pytest.raises(record.RecordInvalid):
            outcome.load(_raw_outcome(_ws(), bad))


def test_an_impossible_date_in_one_workspace_does_not_silence_another(env):
    _raw_outcome(_ws("fx"), "2026-13-45T99:99:99Z")
    _outcome(_ws("fy"), "failed", started=NOW - dt.timedelta(hours=2))
    found = watchdog.scan(NOW)
    assert ("failed", None) in _conds(found, "fy")


def test_a_workspace_that_crashes_the_scan_is_reported_and_the_rest_still_scanned(env, monkeypatch):
    real = watchdog.scan_workspace

    def boom(ws, now, **kw):
        if ws.slug == "fx":
            raise ValueError("broken")
        return real(ws, now, **kw)
    monkeypatch.setattr(watchdog, "scan_workspace", boom)
    _outcome(_ws("fy"), "failed", started=NOW - dt.timedelta(hours=2))
    found = watchdog.scan(NOW)
    assert ("unreadable", None) in _conds(found, "fx") and ("failed", None) in _conds(found, "fy")


def test_a_damaged_last_sent_in_the_alert_state_resends_instead_of_crashing(env):
    _outcome(_ws(), "failed", started=NOW - dt.timedelta(hours=3))
    k = "fx|failed|-"
    for bad in ("2026-13-45T99:99:99+00:00", 42, "x", None):
        env["state"].mkdir(exist_ok=True)
        watchdog.save_state({"keys": {k: {"last_sent": bad}, "fy|x|-": "not a dict"}, "cleared": []})
        box = Outbox()
        watchdog.run(NOW, sender=box)
        assert [m for m in box if "alert" in m["Subject"].lower()], bad


def test_ts_returns_none_for_anything_but_an_aware_iso_time():
    for bad in ("2026-13-45T99:99:99Z", 42, "", None, "2026-11-07T10:00:00"):
        assert watchdog._ts(bad) is None, bad
    assert watchdog._ts("2026-11-07T10:00:00Z") == dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc)
