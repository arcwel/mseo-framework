"""The seal: generated agent settings, the checks it records, drift detection, re-sealing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mseo import seal, workspace
from tests.conftest import FX
from tests.fakes import FakeKeychain

WP_ITEM = ("mseo.fx.wordpress", "fx")


def passing_live_test(ws, siblings):
    return {"status": "pass", "rows": [{"row": n, "ok": True} for n in range(1, 8)]}


def failing_live_test(ws, siblings):
    rows = [{"row": n, "ok": True} for n in range(1, 8)]
    rows[2] = {"row": 3, "ok": False, "why": "python read the sibling's canary"}
    return {"status": "fail", "rows": rows}


@pytest.fixture
def chrome_state(tmp_path):
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}, "Profile 4": {"name": "mseo-fy"}}}}))
    return path


def sealed(root, chrome_state, live=passing_live_test, kc=None):
    ws = workspace.open("fx", keychain=kc or FakeKeychain({WP_ITEM: {"site": FX["cms"]["site"], "user": "u", "app_password": "p"}}))
    return ws, seal.seal(ws, live_test=live, sync=lambda: None, chrome_state=chrome_state)


def test_settings_deny_every_sibling_in_both_spellings(root, tmp_path):
    link = tmp_path / "link-root"
    link.symlink_to(root)
    s = seal.generate_settings("fx", ["fy", "fz"], root.resolve(), raw_root=link, framework=Path("/opt/mseo"))
    deny = s["permissions"]["deny"]
    for spelling in (root.resolve(), link):
        assert f"Read(/{spelling}/fy/**)" in deny and f"Edit(/{spelling}/fz/**)" in deny
        assert str(spelling / "fy") in s["sandbox"]["filesystem"]["denyRead"]
        assert str(spelling / "fx") in s["sandbox"]["filesystem"]["allowWrite"]
    own = [r for r in deny if "/fx/" in r]
    assert all(r.startswith("Edit(") and (any(f"/fx/{d}/" in r for d in seal.RECORD_DIRS) or r.endswith("/fx/customer.json)"))
               for r in own)  # M2 step 6; customer.json since the M2b audit
    assert "Bash(security *)" in deny and "Read(~/.claude/projects/**)" in deny
    assert "Edit(//opt/mseo/**)" in deny
    assert s["permissions"]["defaultMode"] == "dontAsk"
    assert s["sandbox"]["enabled"] and s["sandbox"]["failIfUnavailable"] and not s["sandbox"]["allowUnsandboxedCommands"]
    assert "/opt/mseo" in s["sandbox"]["filesystem"]["denyWrite"]


def test_siblings_are_every_other_folder(root):
    (root / "zz").mkdir()
    (root / "notes.txt").write_text("x")
    assert seal.siblings("fx", root) == ["fy", "zz"]


def test_seal_writes_settings_and_record(root, chrome_state):
    ws, result = sealed(root, chrome_state)
    assert result.code == 0, result.record
    settings = json.loads((root / "fx" / ".claude" / "settings.json").read_text())
    assert any("/fy/**" in r for r in settings["permissions"]["deny"])
    record = json.loads((root / "fx" / ".mseo" / "seal.json").read_text())
    assert record["siblings"] == [seal.CANARY_DIR, "fy"]
    assert record["live_test"]["status"] == "pass"
    assert record["settings_sha256"] == seal.sha256_file(root / "fx" / ".claude" / "settings.json")
    assert seal.check_seal(ws) == []


def test_failed_live_test_exits_3(root, chrome_state):
    _, result = sealed(root, chrome_state, live=failing_live_test)
    assert result.code == 3
    assert "Alternative H" in " ".join(result.messages)


def test_missing_credential_and_profile_are_check_failures(root, tmp_path):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {}}}))
    _, result = sealed(root, state, kc=FakeKeychain())
    assert result.code == 1
    failed = {c["check"] for c in result.record["checks"] if not c["ok"]}
    assert "Keychain item mseo.fx.wordpress" in failed
    assert any("Chrome profile" in c for c in failed)


def test_skipped_live_test_is_recorded_and_seal_is_not_current(root, chrome_state):
    ws = workspace.open("fx", keychain=FakeKeychain({WP_ITEM: {}}))
    result = seal.seal(ws, live_test=None, sync=lambda: None, chrome_state=chrome_state)
    assert result.record["live_test"]["status"] == "skipped"
    assert any("live agent test" in p for p in seal.check_seal(ws))


def test_hand_edited_settings_are_stale(root, chrome_state):
    ws, _ = sealed(root, chrome_state)
    path = root / "fx" / ".claude" / "settings.json"
    path.write_text(path.read_text().replace('"dontAsk"', '"bypassPermissions"'))
    assert any("settings" in p for p in seal.check_seal(ws))


def test_new_sibling_makes_the_seal_stale(root, chrome_state):
    ws, _ = sealed(root, chrome_state)
    (root / "fz").mkdir()
    assert any("sibling" in p for p in seal.check_seal(ws))


def test_missing_seal(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert seal.check_seal(ws) == ["no seal: run mseo workspace seal --workspace fx"]
    with pytest.raises(seal.StaleSeal):
        seal.require_current(ws)


def test_origin_remote_must_be_unique(root, chrome_state):
    for slug in ("fx", "fy"):
        subprocess.run(["git", "init", "-q", str(root / slug)], check=True)
        subprocess.run(["git", "-C", str(root / slug), "remote", "add", "origin", "git@example.invalid:o/same.git"], check=True)
    _, result = sealed(root, chrome_state)
    origin = next(c for c in result.record["checks"] if c["check"] == "origin remote unique")
    assert not origin["ok"] and "fy" in origin["detail"]


def test_origin_must_not_be_the_framework(root, chrome_state, monkeypatch):
    subprocess.run(["git", "init", "-q", str(root / "fx")], check=True)
    subprocess.run(["git", "-C", str(root / "fx"), "remote", "add", "origin", "git@example.invalid:o/fw.git"], check=True)
    monkeypatch.setattr(seal, "framework_origin", lambda: "git@example.invalid:o/fw.git")
    _, result = sealed(root, chrome_state)
    origin = next(c for c in result.record["checks"] if c["check"] == "origin remote unique")
    assert not origin["ok"]


def test_chrome_profile_matches_folder_or_display_name(chrome_state):
    assert seal.chrome_profile_exists("mseo-fy", chrome_state)
    assert seal.chrome_profile_exists("Profile 3", chrome_state)
    assert not seal.chrome_profile_exists("Default", chrome_state)
    assert not seal.chrome_profile_exists("x", chrome_state.parent / "missing")


def test_reseal_all_reseals_every_valid_workspace(root, chrome_state):
    (root / "broken").mkdir()
    (root / "broken" / "customer.json").write_text("{}")
    results = seal.reseal_all(exclude="zz", keychain=FakeKeychain(), live_test=passing_live_test,
                              sync=lambda: None, chrome_state=chrome_state)
    assert set(results) == {"fx", "fy", "broken"}
    assert results["broken"]["code"] == 3
    assert json.loads((root / "fy" / ".mseo" / "seal.json").read_text())["siblings"] == [seal.CANARY_DIR, "broken", "fx"]


def test_live_prompt_quotes_a_framework_path_with_a_space(root, monkeypatch, tmp_path):
    import shlex

    from mseo import live_test

    spaced = tmp_path / "HD Projects" / "mseo" / "bin" / "mseo"
    monkeypatch.setattr(live_test, "mseo_bin", lambda: spaced)
    prompt = live_test.build_prompt(workspace.open("fx", keychain=FakeKeychain()), "fy", tmp_path / "c.txt",
                                    "drafts/sample-draft.md")
    assert f"Bash: {shlex.quote(str(spaced))} doctor --workspace fx" in prompt
    assert f"Bash: {shlex.quote(str(spaced))} content check drafts/sample-draft.md" in prompt
