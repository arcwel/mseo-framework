"""Fixes for the second security audit (browser pulls and the runner)."""

import ssl
import sys

import pytest

from mseo import cli, seal, watchdog, workspace
from mseo.runner import schedule, wrap
from tests.fakes import FakeKeychain


@pytest.fixture
def ws(root, tmp_path, monkeypatch):
    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    return workspace.open("fx", keychain=FakeKeychain())


# H1: a module the agent plants in the workspace must never shadow the framework.

def test_the_job_child_runs_with_safe_path(ws):
    argv = wrap.child_argv(ws, job_id="J", week=None, scheduled_for=None)
    assert argv[:4] == [sys.executable, "-P", "-m", "mseo.runner.child"]


def test_the_job_child_env_sets_pythonsafepath(ws):
    assert wrap.child_env({"PATH": "/usr/bin"})["PYTHONSAFEPATH"] == "1"


def test_launchd_jobs_start_outside_every_workspace_with_safe_path(ws):
    for plist in (schedule.weekly_plist(ws), schedule.watchdog_plist()):
        assert plist["ProgramArguments"][1] == "-P"
        assert plist["EnvironmentVariables"]["PYTHONSAFEPATH"] == "1"
        wd = plist.get("WorkingDirectory")
        assert wd and not wd.startswith(str(workspace.workspaces_root()))


# M: customer.json holds the expected account IDs; the agent must not edit it.

def test_customer_json_is_closed_to_agent_edits(root):
    s = seal.generate_settings("fx", ["fy"], root)
    assert any(r.startswith("Edit(/") and r.endswith("/fx/customer.json)") for r in s["permissions"]["deny"])
    assert str(root / "fx" / "customer.json") in s["sandbox"]["filesystem"]["denyWrite"]


# M: the alert email goes over verified TLS.

def test_smtp_uses_a_verifying_tls_context(monkeypatch, tmp_path):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            seen["context"] = context

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(watchdog.smtplib, "SMTP_SSL", FakeSMTP)
    kc = FakeKeychain()
    kc.set(watchdog.KEYCHAIN_SERVICE, "a@ops.example", {"app_password": "x"})
    send = watchdog.smtp_sender(keychain=kc, cfg={"to": "b@ops.example", "from": "a@ops.example"})
    send(watchdog._message({"to": "b@ops.example", "from": "a@ops.example"}, "s", "b"))
    ctx = seen["context"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


# needs_validation, confirmed: argparse took `--approve` as `--approved`, past the Bash(*--approved*) deny rule.

def test_abbreviated_flags_are_refused(root, capsys):
    code = cli.main(["publish", "drafts/good_article.md", "--workspace", "fx", "--kind", "article",
                     "--slug", "good-article", "--approve", "Agent 2026-11-07"], keychain=FakeKeychain())
    assert code == 3


# L: the live isolation test must not hand API keys to its agent.

def test_live_test_env_drops_api_keys(monkeypatch):
    from mseo import live_test

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    env = live_test.agent_env("/tmp/x")
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


# ------------------------------------------------------------------ pentest findings on the runner

def test_lock_names_cannot_leave_the_lock_folder(tmp_path, monkeypatch):
    from mseo.runner import lock

    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    for bad in ("../x", "a/b", "", "ws-../../y"):
        with pytest.raises(ValueError):
            with lock.workspace_lock(bad):
                pass


def test_a_symlinked_lock_file_is_never_followed(tmp_path, monkeypatch):
    from mseo.runner import lock

    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    (tmp_path / "state" / "locks").mkdir(parents=True)
    (tmp_path / "state" / "locks" / "ws-fx.lock").symlink_to(target)
    with pytest.raises(OSError):
        with lock.workspace_lock("fx"):
            pass
    assert target.read_text() == "keep me"


def test_a_plist_label_needs_a_valid_slug():
    for bad in ("../../tmp/evil", "a/b", "A"):
        with pytest.raises(ValueError):
            schedule.label(bad)


def test_the_job_child_refuses_abbreviated_flags(ws):
    from mseo.runner import child

    with pytest.raises(SystemExit):
        child.main(["--work", "fx", "--job", "J"])


def test_watchdog_state_that_is_not_a_dict_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("MSEO_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "alerts.json").write_text("[]")
    assert watchdog.load_state() == {"keys": {}, "cleared": [], "summary_sent": None}


def test_alert_subject_has_no_line_breaks():
    msg = watchdog.compose_alert({"to": "b@ops.example", "from": "a@ops.example"},
                                 [{"workspace": "fx\r\nBcc: x@evil.example", "condition": "failed", "source": None,
                                   "detail": "d", "alert": True}])
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]


def test_children_get_the_whole_grace_period_after_the_leader_exits(ws, tmp_path):
    """The leader dies on SIGTERM; a child that needs 0.5 s to clean up must not be SIGKILLed first."""
    import time

    done = tmp_path / "cleaned"
    child_code = (f"import signal, sys, time\n"
                  f"def bye(*a):\n    time.sleep(0.5); open({str(done)!r}, 'w').write('ok'); sys.exit(0)\n"
                  "signal.signal(signal.SIGTERM, bye)\ntime.sleep(60)")
    code = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            "time.sleep(60)")
    wrap.run_wrapped(ws, job_id="20261109T140000Z-weekly-9999", argv=[sys.executable, "-c", code],
                     timeout_s=1.0, grace_s=3.0)
    time.sleep(0.2)
    assert done.exists()


def test_a_seal_whose_security_checks_failed_is_not_current(root, monkeypatch, tmp_path):
    import json as _json

    ws_ = workspace.open("fx", keychain=FakeKeychain())
    monkeypatch.setattr(seal, "check_seal", seal.check_seal)
    path = ws_.path(".mseo", "seal.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"checks": [{"check": "identifiers unique across workspaces", "ok": False}]}))
    assert any("identifiers unique" in p for p in seal.check_seal(ws_))


def test_a_missing_operator_deny_file_is_not_current(root, monkeypatch, tmp_path):
    import json as _json

    ws_ = workspace.open("fx", keychain=FakeKeychain())
    monkeypatch.setenv("MSEO_OPERATOR_DENY", str(tmp_path / "gone.txt"))
    path = ws_.path(".mseo", "seal.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"operator_deny_sha256": None, "checks": []}))
    assert any("operator deny list" in p for p in seal.check_seal(ws_))


def test_a_malformed_run_record_never_stops_the_watchdog(ws):
    import datetime as dt

    bad = ws.path("data", "runs", "20261107T000000Z-ga4-dead")
    bad.mkdir(parents=True)
    (bad / "record.json").write_text('{"schema_version": "mseo.run/2", "source": "ga4"}')
    (ws.path("data", "runs", "20261107T000000Z-ga4-beef")).mkdir()
    (ws.path("data", "runs", "20261107T000000Z-ga4-beef", "record.json")).write_text("[1, 2]")
    assert watchdog.scan_workspace(ws, dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc), scheduled=False) == []
