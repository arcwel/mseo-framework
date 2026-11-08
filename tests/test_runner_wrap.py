"""M2 step 11 (build plan A3): the wrapper (12-hour kill, workspace lock, one outcome whatever happens),
`mseo lock run --browser` and `mseo schedule generate` (D1, Runner, locks and kill switch)."""

import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import pytest

from mseo import cli, workspace
from mseo.run import outcome
from mseo.runner import lock, schedule, wrap
from tests.conftest import FX, edit_config
from tests.fakes import FakeKeychain

PY = sys.executable


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def ws(root, state):
    return workspace.open("fx", keychain=FakeKeychain())


def _child(code: str) -> list[str]:
    return [PY, "-c", code]


def _write_outcome_code(ws, job_id: str, status: str = "ok") -> str:
    return ("import datetime as dt\nfrom mseo.run import outcome\nfrom pathlib import Path\n"
            "now = dt.datetime.now(dt.timezone.utc)\n"
            f"out = outcome.build(job_id={job_id!r}, workspace='fx', status={status!r}, scheduled_for=None, "
            "started=now, finished=now, run_ids=[], identity_check={}, identity_shared=False, usage=None, "
            f"error=None)\noutcome.write(Path({str(ws.path('data', 'outcomes'))!r}), out)\n")


# ------------------------------------------------------------------ the child's outcome is the job's outcome

def test_a_child_that_writes_its_outcome_is_returned_as_is(ws):
    job = "20261109T140000Z-weekly-aaaa"
    out = wrap.run_wrapped(ws, job_id=job, argv=_child(_write_outcome_code(ws, job)), timeout_s=30)
    assert out["status"] == "ok" and out["job_id"] == job
    assert outcome.load(ws.path("data", "outcomes", f"{job}.json"))["status"] == "ok"


def test_a_child_that_dies_without_an_outcome_gets_failed(ws):
    job = "20261109T140000Z-weekly-bbbb"
    out = wrap.run_wrapped(ws, job_id=job, argv=_child("import sys; print('boom', file=sys.stderr); sys.exit(4)"),
                           timeout_s=30)
    assert out["status"] == "failed"
    assert "exited with code 4" in out["error"] and "boom" in out["error"]
    assert ws.path("data", "outcomes", f"{job}.json").is_file()


def test_the_child_log_lands_in_the_workspace(ws):
    job = "20261109T140000Z-weekly-cccc"
    wrap.run_wrapped(ws, job_id=job, argv=_child("print('hello from the job')"), timeout_s=30)
    assert "hello from the job" in ws.path("data", "logs", f"{job}.log").read_text()


# ------------------------------------------------------------------ kill switch

def test_a_hung_job_is_killed_and_gets_a_killed_outcome(ws):
    job = "20261109T140000Z-weekly-dddd"
    t = time.monotonic()
    out = wrap.run_wrapped(ws, job_id=job, argv=_child("import time; time.sleep(60)"), timeout_s=0.5, grace_s=0.5)
    assert time.monotonic() - t < 10
    assert out["status"] == "killed"
    assert "SIGTERM" in out["error"]


def test_a_job_that_ignores_sigterm_gets_sigkill(ws, tmp_path):
    job = "20261109T140000Z-weekly-eeee"
    code = ("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\n"
            "time.sleep(60)")
    out = wrap.run_wrapped(ws, job_id=job, argv=_child(code), timeout_s=1, grace_s=0.5)
    assert out["status"] == "killed" and "SIGKILL" in out["error"]


def test_the_kill_reaches_grandchildren(ws, tmp_path):
    """The whole process group goes: a claude -p started by the job must not outlive it."""
    job = "20261109T140000Z-weekly-ffff"
    pidfile = tmp_path / "grandchild.pid"
    code = (f"import subprocess, sys, time\np = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\ntime.sleep(60)")
    wrap.run_wrapped(ws, job_id=job, argv=_child(code), timeout_s=1.5, grace_s=0.5)
    pid = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild outlived the kill")


def test_a_killed_outcome_lists_the_job_s_run_records(ws):
    from mseo.run import record

    job = "20261109T140000Z-weekly-abab"
    now = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)
    run_id = record.new_run_id("search_console", now=now)
    rec = record.build(run_id=run_id, job_id=job, workspace="fx", source="search_console", adapter="export",
                       identity=FX["identity"]["email"], identity_shared=False,
                       period=record.week_period("2026-W45", FX["timezone"]),
                       account_id_expected=FX["accounts"]["search_console"]["id"], account_id_seen=None,
                       seen_via="none", status="error", complete={"value": False, "reason": "error"}, files=[],
                       started=now, finished=now, error="interrupted")
    record.write(ws.path("data", "runs", run_id), rec)
    out = wrap.run_wrapped(ws, job_id=job, argv=_child("import time; time.sleep(60)"), timeout_s=0.3, grace_s=0.3)
    assert out["run_ids"] == [run_id]


# ------------------------------------------------------------------ workspace lock and environment

def test_a_second_job_on_the_same_workspace_is_not_run(ws, state):
    job = "20261109T140000Z-weekly-1111"
    with lock.workspace_lock("fx") as held:
        assert held
        out = wrap.run_wrapped(ws, job_id=job, argv=_child("raise SystemExit(0)"), timeout_s=30)
    assert out["status"] == "not_run"
    assert f"pid {os.getpid()}" in out["error"]


def test_api_keys_never_reach_the_job(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test-not-real")
    dump = tmp_path / "env.json"
    code = f"import json, os; json.dump(dict(os.environ), open({str(dump)!r}, 'w'))"
    wrap.run_wrapped(ws, job_id="20261109T140000Z-weekly-2222", argv=_child(code), timeout_s=30)
    env = json.loads(dump.read_text())
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("MSEO_STATE_DIR") == os.environ["MSEO_STATE_DIR"]


def test_the_default_child_runs_the_weekly_job_module(ws):
    argv = wrap.child_argv(ws, job_id="J", week="2026-W45", scheduled_for=None)
    assert argv[:4] == [PY, "-P", "-m", "mseo.runner.child"]
    assert argv[argv.index("--workspace") + 1] == "fx" and argv[argv.index("--week") + 1] == "2026-W45"


# ------------------------------------------------------------------ CLI: run weekly

def test_cli_run_weekly_exit_codes(root, state, monkeypatch):
    seen = {}

    def fake(ws, *, job_id, argv, scheduled_for=None, **kw):
        seen["argv"], seen["scheduled_for"] = argv, scheduled_for
        return {"status": "partial", "job_id": job_id, "error": "1 source(s) not ok"}

    monkeypatch.setattr(wrap, "run_wrapped", fake)
    monkeypatch.setenv("MSEO_NOW", "2026-11-09T14:30:00+00:00")
    code = cli.main(["run", "weekly", "--workspace", "fx", "--json"], keychain=FakeKeychain())
    assert code == 1
    assert "mseo.runner.child" in seen["argv"]
    assert seen["scheduled_for"] == schedule.last_slot(workspace.open("fx", keychain=FakeKeychain()),
                                                       dt.datetime(2026, 11, 9, 14, 30, tzinfo=dt.timezone.utc))


# ------------------------------------------------------------------ lock run --browser

def test_lock_run_holds_the_browser_lock_for_the_command(state, tmp_path):
    probe = tmp_path / "probe.txt"
    code = ("from mseo.runner import lock\n"
            f"with lock.browser_lock(wait_s=0) as h: open({str(probe)!r}, 'w').write('held' if h else 'busy')")
    rc = cli.main(["lock", "run", "--browser", "--", PY, "-c", code], keychain=FakeKeychain())
    assert rc == 0 and probe.read_text() == "busy"
    with lock.browser_lock(wait_s=0) as again:
        assert again


def test_lock_run_passes_the_command_s_exit_code(state):
    assert cli.main(["lock", "run", "--browser", "--", PY, "-c", "raise SystemExit(7)"], keychain=FakeKeychain()) == 7


def test_lock_run_gives_up_when_the_lock_stays_held(state):
    with lock.browser_lock(wait_s=0):
        code = subprocess.run([PY, "-m", "mseo", "lock", "run", "--browser", "--wait-s", "0.2", "--", PY, "-c", "pass"],
                              env=os.environ, capture_output=True, text=True)
    assert code.returncode == 1 and "browser lock" in code.stderr + code.stdout


# ------------------------------------------------------------------ schedule generate

def test_slot_staggers_by_slug_order(root):
    fx = workspace.open("fx", keychain=FakeKeychain())
    fy = workspace.open("fy", keychain=FakeKeychain())
    a, b = schedule.slot(fx), schedule.slot(fy)
    assert (b - a) == dt.timedelta(minutes=schedule.GAP_MIN)  # fx sorts first, fy one gap later


def test_last_slot_is_the_latest_occurrence_not_after_now(root):
    fx = workspace.open("fx", keychain=FakeKeychain())
    tz = ZoneInfo(FX["timezone"])
    now = dt.datetime(2026, 11, 10, 12, 0, tzinfo=tz)  # a Tuesday
    got = schedule.last_slot(fx, now)
    assert got.astimezone(tz).strftime("%a") == FX["schedule"]["weekly"]["weekday"]
    assert got <= now and now - got < dt.timedelta(days=7)
    assert got.astimezone(tz).strftime("%H:%M") == FX["schedule"]["weekly"]["time"]


def test_plist_runs_mseo_run_weekly_at_the_slot(root, state):
    fx = workspace.open("fx", keychain=FakeKeychain())
    plist = schedule.weekly_plist(fx, local_tz=ZoneInfo(FX["timezone"]))
    assert plist["Label"] == "com.mseo.fx.weekly"
    assert plist["ProgramArguments"][-4:] == ["run", "weekly", "--workspace", "fx"]
    hh, mm = map(int, FX["schedule"]["weekly"]["time"].split(":"))
    weekday = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].index(FX["schedule"]["weekly"]["weekday"])
    assert plist["StartCalendarInterval"] == {"Weekday": weekday, "Hour": hh, "Minute": mm}
    assert "ANTHROPIC_API_KEY" not in plist.get("EnvironmentVariables", {})
    assert plist["EnvironmentVariables"]["MSEO_WORKSPACES"] == str(root)


def test_plist_converts_the_customer_zone_to_the_machine_zone(root):
    fx = workspace.open("fx", keychain=FakeKeychain())
    customer = ZoneInfo(FX["timezone"])
    other = ZoneInfo("America/New_York") if FX["timezone"] != "America/New_York" else ZoneInfo("Europe/London")
    plist = schedule.weekly_plist(fx, local_tz=other)
    nxt = schedule.next_slot(fx, dt.datetime.now(customer)).astimezone(other)
    assert (plist["StartCalendarInterval"]["Hour"], plist["StartCalendarInterval"]["Minute"]) == (nxt.hour, nxt.minute)


def test_generate_dry_run_writes_nothing(root, state, tmp_path, monkeypatch, capsys):
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("MSEO_LAUNCH_AGENTS", str(agents))
    assert cli.main(["schedule", "generate", "--workspace", "fx", "--dry-run"], keychain=FakeKeychain()) == 0
    assert not agents.exists()
    assert "com.mseo.fx.weekly" in capsys.readouterr().out


def test_generate_load_writes_and_bootstraps(root, state, tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setenv("MSEO_LAUNCH_AGENTS", str(agents))
    calls = []
    monkeypatch.setattr(schedule, "launchctl", lambda *a: calls.append(a) or 0)
    assert cli.main(["schedule", "generate", "--workspace", "fx", "--load"], keychain=FakeKeychain()) == 0
    path = agents / "com.mseo.fx.weekly.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == "com.mseo.fx.weekly"
    assert calls[-1][0] == "bootstrap" and calls[-1][-1] == str(path)
    calls.clear()
    assert cli.main(["schedule", "generate", "--workspace", "fx", "--unload"], keychain=FakeKeychain()) == 0
    assert calls[0][0] == "bootout" and not path.exists()


def test_watchdog_plist_runs_twice_a_day(state):
    plist = schedule.watchdog_plist()
    assert plist["Label"] == "com.mseo.watchdog"
    assert plist["ProgramArguments"][-1] == "watchdog"
    assert plist["StartCalendarInterval"] == [{"Hour": 9, "Minute": 0}, {"Hour": 21, "Minute": 0}]


def test_schedule_without_a_weekly_entry_uses_monday_6(root):
    edit_config(root, "fx", lambda c: c.pop("schedule", None))
    fx = workspace.open("fx", keychain=FakeKeychain())
    s = schedule.slot(fx)
    assert (s.strftime("%a"), s.strftime("%H:%M")) == ("Mon", "06:00")
