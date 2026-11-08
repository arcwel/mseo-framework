"""M2 step 7: the agent runner, page extractors, the sign-in check and mseo identity profile (D1)."""

import datetime as dt
import json

import pytest

from mseo import seal, workspace
from mseo.browser import extract
from mseo.browser import run as browser_run
from mseo.run import record
from tests.conftest import FX
from tests.fake_agent import ACCOUNT_PAGE, SIGNED_OUT_PAGE, FakeRunner
from tests.fakes import FakeKeychain

NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.UTC)
PERIOD = {"start": "2026-11-02", "end": "2026-11-08", "tz": "America/Los_Angeles"}


@pytest.fixture
def ws(root, tmp_path):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []},
              pull_test=lambda *_: {"status": "pass", "rows": []}, sync=None, chrome_state=state)
    return w


# ------------------------------------------------------------------ extractors

def test_signed_in_email_reads_the_one_address_on_the_account_page():
    page = ACCOUNT_PAGE.format(name="Agent Fx", email=FX["identity"]["email"])
    assert extract.signed_in_email(page) == (FX["identity"]["email"], None)


def test_signed_in_email_reports_signed_out():
    assert extract.signed_in_email(SIGNED_OUT_PAGE) == (None, "signed_out")


def test_signed_in_email_refuses_a_page_with_two_addresses():
    page = ACCOUNT_PAGE.format(name="a", email="a@one.example\nb@two.example")
    assert extract.signed_in_email(page) == (None, "ambiguous")


def test_signed_in_email_refuses_an_unknown_page():
    assert extract.signed_in_email("Some other page entirely") == (None, "layout_changed")
    other = "Title: Inbox\nURL: https://mail.google.com/mail/u/0/\n---\na@one.example\n"
    assert extract.signed_in_email(other) == (None, "layout_changed")


def test_signed_in_email_needs_the_account_page_url_not_only_its_title():
    page = ACCOUNT_PAGE.format(name="x", email="a@one.example").replace("URL: https://myaccount.google.com/", "URL: https://evil.example/")
    assert extract.signed_in_email(page) == (None, "layout_changed")


# ------------------------------------------------------------------ runner

def test_runner_uses_the_pull_flags_and_strips_api_keys(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test-not-real")
    fake = FakeRunner()
    res = browser_run.run_agent(ws, "do it", runner=fake)
    call = fake.calls[0]
    assert call["argv"][:3] == ["claude", "-p", "do it"]
    for flag in seal.pull_flags(ws):
        assert flag in call["argv"]
    assert "--max-turns" in call["argv"] and "--no-session-persistence" in call["argv"]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert call["cwd"] == ws.root
    assert call["timeout"] == browser_run.TIMEOUT_S == 45 * 60
    assert res.ok and res.error is None


def test_runner_records_usage_as_tokens_and_minutes(ws):
    res = browser_run.run_agent(ws, "x", runner=FakeRunner(turns=6))
    assert res.usage == {"tokens_in": 1200, "tokens_out": 300, "cache_read": 40000, "cache_write": 5000,
                         "turns": 6, "minutes": 1.5, "model": "claude-haiku-5-5"}
    assert "usd" not in json.dumps(res.usage)


def test_runner_refuses_anything_but_the_subscription(ws):
    res = browser_run.run_agent(ws, "x", runner=FakeRunner(api_key_source="ANTHROPIC_API_KEY"))
    assert not res.ok and res.status == "auth_failed" and "subscription" in res.error


def test_runner_timeout_is_an_error_not_a_hang(ws):
    res = browser_run.run_agent(ws, "x", runner=FakeRunner(hang=True))
    assert not res.ok and res.status == "error" and "timed out" in res.error


def test_runner_nonzero_exit_is_an_error(ws):
    res = browser_run.run_agent(ws, "x", runner=FakeRunner(exit_code=1))
    assert not res.ok and res.status == "error"


def test_runner_refuses_a_stale_seal(ws):
    ws.path(".claude", "pull-settings.json").write_text("{}")
    with pytest.raises(seal.StaleSeal):
        browser_run.run_agent(ws, "x", runner=FakeRunner())


def test_runner_refuses_when_the_pull_test_has_not_passed(root, tmp_path):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []}, sync=None, chrome_state=state)
    with pytest.raises(seal.StaleSeal, match="pull test"):
        browser_run.run_agent(w, "x", runner=FakeRunner())


def test_prompts_are_generic_and_filled_from_config(ws):
    text = browser_run.prompt("sign_in", ws, source="ga4", snapshot="snapshot-account.txt")
    assert "snapshot-account.txt" in text and "data/inbox/" in text
    raw = (browser_run.PROMPTS / "sign_in.md").read_text()
    assert FX["identity"]["email"] not in raw


# ------------------------------------------------------------------ sign-in check

def _check(ws, page):
    from mseo import identity

    return identity.sign_in_check(ws, "search_console", period=PERIOD, runner=FakeRunner({"snapshot-account.txt": page}),
                                  now=NOW)


def test_sign_in_ok_keeps_the_snapshot_in_the_run_folder(ws):
    out = _check(ws, ACCOUNT_PAGE.format(name="Agent Fx", email=FX["identity"]["email"].upper()))
    assert out["status"] == "ok"
    rec = record.load(ws.path("data", "runs", out["run_id"], "record.json"))
    assert rec["source"] == "sign_in" and rec["seen_via"] == "page:google_account"
    assert rec["account_id_seen"] == FX["identity"]["email"] and rec["identity_shared"] is False
    assert [f["path"] for f in rec["files"]] == ["snapshot-account.txt"]
    assert not any(ws.path("data", "inbox").iterdir())
    assert rec["usage"]["turns"] == 4


def test_sign_in_with_another_account_is_wrong_account_and_keeps_nothing(ws):
    out = _check(ws, ACCOUNT_PAGE.format(name="Operator", email="operator@agency.example"))
    assert out["status"] == "wrong_account"
    rec = record.load(ws.path("data", "runs", out["run_id"], "record.json"))
    assert rec["files"] == [] and rec["account_id_seen"] == "operator@agency.example"
    assert not any(ws.path("data", "inbox").iterdir())
    assert not ws.path("data", "runs", out["run_id"], "snapshot-account.txt").exists()


def test_signed_out_is_auth_failed(ws):
    assert _check(ws, SIGNED_OUT_PAGE)["status"] == "auth_failed"


def test_missing_snapshot_is_error(ws):
    from mseo import identity

    out = identity.sign_in_check(ws, "search_console", period=PERIOD, runner=FakeRunner({}), now=NOW)
    assert out["status"] == "error" and "snapshot" in out["error"]


def test_sign_in_uses_the_source_override(ws):
    from mseo import identity

    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg["accounts"]["google_ads"]["identity_override"] = {
        "email": "operator@agency.example", "chrome_profile": "Default", "allow_shared": True, "temporary": True,
        "until": "grant lands"}
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    page = ACCOUNT_PAGE.format(name="Operator", email="operator@agency.example")
    out = identity.sign_in_check(w, "google_ads", period=PERIOD, runner=FakeRunner({"snapshot-account.txt": page}),
                                 now=NOW, require_seal=False)
    assert out["status"] == "ok"
    assert record.load(w.path("data", "runs", out["run_id"], "record.json"))["identity_shared"] is True


# ------------------------------------------------------------------ mseo identity profile

def _chrome(tmp_path, running=False):
    base = tmp_path / "Chrome"
    (base / "Profile 3").mkdir(parents=True)
    (base / "Profile 3" / "Preferences").write_text(json.dumps({"download": {"default_directory": "/Users/x/Downloads"},
                                                                "other": 1}))
    (base / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    return base


def test_identity_profile_sets_the_download_folder_to_the_inbox(ws, tmp_path):
    from mseo import identity

    base = _chrome(tmp_path)
    out = identity.set_profile_downloads(ws, chrome_dir=base, chrome_running=lambda: False)
    prefs = json.loads((base / "Profile 3" / "Preferences").read_text())
    assert prefs["download"]["default_directory"] == str(ws.path("data", "inbox"))
    assert prefs["download"]["prompt_for_download"] is False and prefs["other"] == 1
    assert out["changed"] is True and out["profile_folder"] == "Profile 3"


def test_identity_profile_does_not_write_while_chrome_runs(ws, tmp_path):
    from mseo import identity

    base = _chrome(tmp_path)
    before = (base / "Profile 3" / "Preferences").read_text()
    out = identity.set_profile_downloads(ws, chrome_dir=base, chrome_running=lambda: True)
    assert out["changed"] is False and "quit Chrome" in out["message"]
    assert (base / "Profile 3" / "Preferences").read_text() == before


def test_identity_profile_dry_run_writes_nothing(ws, tmp_path):
    from mseo import identity

    base = _chrome(tmp_path)
    before = (base / "Profile 3" / "Preferences").read_text()
    out = identity.set_profile_downloads(ws, chrome_dir=base, chrome_running=lambda: False, dry_run=True)
    assert out["changed"] is False and (base / "Profile 3" / "Preferences").read_text() == before


def test_identity_profile_missing_profile_is_reported(ws, tmp_path):
    from mseo import identity

    base = _chrome(tmp_path)
    (base / "Local State").write_text(json.dumps({"profile": {"info_cache": {}}}))
    with pytest.raises(identity.ProfileMissing, match="mseo-fx"):
        identity.set_profile_downloads(ws, chrome_dir=base, chrome_running=lambda: False)


def test_identity_profile_cli_json(ws, tmp_path, monkeypatch):
    from mseo import cli, identity

    base = _chrome(tmp_path)
    monkeypatch.setattr(identity, "CHROME_DIR", base)
    monkeypatch.setattr(identity, "chrome_running", lambda: False)
    code = cli.main(["identity", "profile", "--workspace", "fx", "--json"], keychain=FakeKeychain())
    assert code == 0


def test_unrecognised_page_names_its_title_and_url_but_not_its_body(ws):
    page = "Title: Loading\nURL: https://myaccount.google.com/?pli=1\nSource element: <div>\n---\nsecret body text"
    out = _check(ws, page)
    assert out["status"] == "layout_changed"
    assert "Title: Loading" in out["error"] and "URL: https://myaccount.google.com/" in out["error"]
    assert "pli=1" not in out["error"] and "secret body" not in out["error"]


def test_unrecognised_page_without_header_lines_says_so(ws):
    out = _check(ws, "just some words")
    assert out["status"] == "layout_changed" and "no Title/URL lines" in out["error"]
