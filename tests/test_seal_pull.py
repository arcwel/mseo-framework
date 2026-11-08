"""M2 step 6: the pull settings (one narrow Chrome allow) and the record folders closed to agent writes (D1)."""

import json

import pytest

from mseo import live_test, seal

RECORD_DIRS = ("data/runs", "data/outcomes", "data/weeks")


@pytest.fixture
def chrome_state(tmp_path):
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}, "Profile 4": {"name": "mseo-fy"}}}}))
    return path


def _settings(root):
    return seal.generate_settings("fx", ["fy"], root)


# ------------------------------------------------------------------ record folders

def test_workspace_settings_deny_agent_edits_to_record_folders(root):
    deny = _settings(root)["permissions"]["deny"]
    for d in RECORD_DIRS:
        assert f"Edit(/{root / 'fx' / d}/**)" in deny


def test_sandbox_denies_writes_to_record_folders(root):
    deny_write = _settings(root)["sandbox"]["filesystem"]["denyWrite"]
    for d in RECORD_DIRS:
        assert str(root / "fx" / d) in deny_write


def test_inbox_stays_writable(root):
    s = _settings(root)
    assert not any("data/inbox" in r for r in s["permissions"]["deny"])
    assert not any("data/inbox" in p for p in s["sandbox"]["filesystem"]["denyWrite"])


# ------------------------------------------------------------------ pull settings

def test_pull_settings_drop_only_the_blanket_mcp_deny(root):
    base = _settings(root)
    pull = seal.generate_pull_settings(base)
    assert "mcp__*" not in pull["permissions"]["deny"]
    assert set(base["permissions"]["deny"]) - {"mcp__*"} <= set(pull["permissions"]["deny"])
    assert pull["sandbox"] == base["sandbox"]
    assert pull["permissions"]["defaultMode"] == "dontAsk"
    assert base["permissions"]["deny"].count("mcp__*") == 1  # the interactive file is unchanged


def test_pull_settings_allow_only_named_chrome_tools(root):
    pull = seal.generate_pull_settings(_settings(root))
    allow = pull["permissions"]["allow"]
    tools = [a for a in allow if a.startswith("mcp__")]
    assert tools == list(seal.CHROME_ALLOW)
    assert all(t.startswith("mcp__claude-in-chrome__") and not t.endswith("*") for t in tools)


def test_pull_settings_allow_writes_only_to_the_inbox(root):
    pull = seal.generate_pull_settings(_settings(root))
    edits = [a for a in pull["permissions"]["allow"] if not a.startswith("mcp__")]
    assert edits and all(a.startswith("Edit(/") and a.endswith("/fx/data/inbox/**)") for a in edits)


def test_pull_settings_deny_chrome_tools_that_move_files_or_switch_browsers(root):
    pull = seal.generate_pull_settings(_settings(root))
    for tool in ("file_upload", "upload_image", "switch_browser", "javascript_tool"):
        assert f"mcp__claude-in-chrome__{tool}" in pull["permissions"]["deny"]
        assert f"mcp__claude-in-chrome__{tool}" not in pull["permissions"]["allow"]


def test_pull_flags_load_only_the_pull_file(root, chrome_state):
    from mseo import workspace
    from tests.fakes import FakeKeychain

    ws = workspace.open("fx", keychain=FakeKeychain())
    flags = seal.pull_flags(ws)
    assert flags[flags.index("--setting-sources") + 1] == ""
    assert flags[flags.index("--settings") + 1] == str(ws.path(".claude", "pull-settings.json"))
    assert "--strict-mcp-config" in flags and "--chrome" in flags
    assert "--mcp-config" not in flags
    assert flags[flags.index("--permission-mode") + 1] == "dontAsk"


# ------------------------------------------------------------------ the seal writes and checks both files

def _seal(root, chrome_state, **kw):
    from mseo import workspace
    from tests.fakes import FakeKeychain

    ws = workspace.open("fx", keychain=FakeKeychain())
    return ws, seal.seal(ws, live_test=lambda *_: {"status": "pass", "rows": []}, sync=None,
                         chrome_state=chrome_state, **kw)


def test_seal_writes_the_pull_file_and_records_its_hash(root, chrome_state):
    ws, res = _seal(root, chrome_state)
    path = ws.path(".claude", "pull-settings.json")
    assert json.loads(path.read_text())["permissions"]["allow"][:len(seal.CHROME_ALLOW)] == list(seal.CHROME_ALLOW)
    assert res.record["pull_settings_sha256"] == seal.sha256_file(path)


def test_hand_edited_pull_file_is_stale(root, chrome_state):
    ws, _ = _seal(root, chrome_state)
    path = ws.path(".claude", "pull-settings.json")
    data = json.loads(path.read_text())
    data["permissions"]["allow"].append("mcp__claude-in-chrome__file_upload")
    path.write_text(json.dumps(data))
    assert any("pull-settings.json" in p for p in seal.check_seal(ws))


def test_pull_live_test_failure_fails_the_seal(root, chrome_state):
    from mseo.exits import EXIT_COULD_NOT_RUN

    _, res = _seal(root, chrome_state, pull_test=lambda *_: {"status": "fail", "rows": [{"row": 8, "ok": False}]})
    assert res.code == EXIT_COULD_NOT_RUN
    assert res.record["pull_test"]["status"] == "fail"


def test_pull_live_test_skipped_by_default(root, chrome_state):
    _, res = _seal(root, chrome_state)
    assert res.record["pull_test"]["status"] == "skipped"


# ------------------------------------------------------------------ pull live test rows (stream evaluation)

def _init(tools):
    return json.dumps({"type": "system", "subtype": "init", "tools": tools})


def _result():
    return json.dumps({"type": "result", "result": "[]", "permission_denials": []})


def test_pull_rows_pass_when_only_chrome_mcp_loads_and_records_stay_shut():
    stream = "\n".join([_init(["Read", "Bash", "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__tabs_context_mcp"]),
                        _result()])
    out = live_test.evaluate_pull(stream, record_written=False, inbox_written=True)
    rows = {r["row"]: r for r in out["rows"]}
    assert rows[8]["ok"] and rows[9]["ok"] and rows[10]["ok"] and rows[11]["ok"]
    assert out["status"] == "pass"


def test_pull_rows_fail_on_a_record_write_or_a_foreign_mcp_tool():
    stream = "\n".join([_init(["mcp__claude-in-chrome__navigate", "mcp__desktop-commander__write_file"]), _result()])
    out = live_test.evaluate_pull(stream, record_written=True, inbox_written=True)
    rows = {r["row"]: r for r in out["rows"]}
    assert rows[8]["ok"] is False and rows[10]["ok"] is False
    assert out["status"] == "fail"


def test_pull_rows_incomplete_when_chrome_is_not_loaded():
    stream = "\n".join([_init(["Read", "Bash"]), _result()])
    out = live_test.evaluate_pull(stream, record_written=False, inbox_written=True)
    rows = {r["row"]: r for r in out["rows"]}
    assert rows[9]["ok"] is False
    assert out["status"] == "incomplete"


def test_pull_rows_flag_a_denied_chrome_tool_that_loaded():
    stream = "\n".join([_init(["mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__file_upload"]), _result()])
    out = live_test.evaluate_pull(stream, record_written=False, inbox_written=True)
    # Loaded is fine (deny refuses the call); row 10 only fails for tools of another server.
    assert {r["row"]: r for r in out["rows"]}[10]["ok"] is True


def test_cli_runs_the_pull_test_only_when_asked(root, monkeypatch):
    from mseo import cli
    from tests.fakes import FakeKeychain

    calls = []
    monkeypatch.setattr(live_test, "run_pull", lambda ws, sibs: calls.append(ws.slug) or {"status": "pass", "rows": []})
    monkeypatch.setitem(seal.seal.__kwdefaults__, "sync", None)
    base = ["workspace", "seal", "--workspace", "fx", "--skip-live-test", "--json"]
    cli.main(base, keychain=FakeKeychain())
    assert calls == []
    rec = json.loads((root / "fx" / ".mseo" / "seal.json").read_text())
    assert rec["pull_test"]["status"] == "skipped"
    cli.main(base + ["--pull-test"], keychain=FakeKeychain())
    assert calls == ["fx"]
    rec = json.loads((root / "fx" / ".mseo" / "seal.json").read_text())
    assert rec["pull_test"]["status"] == "pass"
