"""mseo doctor and mseo workspace new."""

from __future__ import annotations

import json
import subprocess

import pytest

from mseo import cli, doctor, seal, workspace
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_OK
from tests.conftest import FX
from tests.fakes import FakeKeychain
from tests.test_seal import passing_live_test

WP = {"site": FX["cms"]["site"], "user": "u", "app_password": "p"}


@pytest.fixture
def chrome_state(tmp_path):
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    return path


@pytest.fixture
def healthy(root, chrome_state, monkeypatch):
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): WP})
    ws = workspace.open("fx", keychain=kc)
    seal.seal(ws, live_test=passing_live_test, sync=lambda: None, chrome_state=chrome_state)
    monkeypatch.setattr(doctor, "tool_path", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setenv("MSEO_TODAY", "2026-11-04")
    return ws, kc


def test_doctor_passes_on_a_healthy_sealed_workspace(healthy, chrome_state):
    ws, _ = healthy
    report = doctor.run(ws, chrome_state=chrome_state)
    assert report.code == EXIT_OK, [c for c in report.checks if not c["ok"]]
    doctor.write_record(ws, report)
    record = json.loads(ws.path("data", "weekly", "2026-W45", "preflight.run.json").read_text())
    assert record["status"] == "ok" and record["source"] == "preflight"


def test_doctor_lists_each_failed_check(healthy, chrome_state):
    ws, kc = healthy
    kc.items.clear()
    (ws.root / "zz").mkdir()  # not a sibling: inside fx; sibling drift comes next
    (ws.root.parent / "fz").mkdir()
    report = doctor.run(ws, chrome_state=chrome_state)
    assert report.code == EXIT_COULD_NOT_RUN  # a stale seal is exit 3, not 1
    failed = {c["check"] for c in report.checks if not c["ok"]}
    assert "WordPress credential mseo.fx.wordpress" in failed
    assert "seal current" in failed


def test_doctor_without_a_seal(root, chrome_state, monkeypatch):
    monkeypatch.setattr(doctor, "tool_path", lambda name: None)
    ws = workspace.open("fx", keychain=FakeKeychain())
    failed = {c["check"] for c in doctor.run(ws, chrome_state=chrome_state).checks if not c["ok"]}
    assert {"seal current", "deslop installed"} <= failed


def test_doctor_needs_an_account_id_per_enabled_source(healthy, chrome_state):
    ws, _ = healthy
    names = {c["check"] for c in doctor.run(ws, chrome_state=chrome_state).checks}
    assert {"google_ads ID set", "ga4 ID set", "search_console ID set"} <= names


def test_doctor_cli_bad_config_is_3(root):
    (root / "fx" / "customer.json").write_text("{}")
    assert cli.main(["doctor", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN


def test_doctor_cli_json(healthy, capsys, monkeypatch, chrome_state):
    _, kc = healthy
    monkeypatch.setattr(doctor, "CHROME_LOCAL_STATE", chrome_state)
    assert cli.main(["doctor", "--workspace", "fx", "--json"], keychain=kc) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


# ---------------------------------------------------------------- workspace new

@pytest.fixture
def no_reseal(monkeypatch):
    calls = []
    monkeypatch.setattr(seal, "reseal_all", lambda **kw: calls.append(kw) or {})
    return calls


def test_new_creates_a_repo_from_the_template(root, no_reseal):
    assert cli.main(["workspace", "new", "acme-dental"]) == EXIT_OK
    new = root / "acme-dental"
    assert (new / ".git").is_dir() and (new / "customer.json").is_file() and (new / "data" / "content_log.json").is_file()
    assert json.loads((new / "customer.json").read_text())["slug"] == "acme-dental"
    assert subprocess.run(["git", "-C", str(new), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert no_reseal and no_reseal[0]["exclude"] == "acme-dental"


def test_an_unfilled_template_cannot_run(root, no_reseal):
    cli.main(["workspace", "new", "acme-dental"])
    with pytest.raises(workspace.ConfigInvalid):
        workspace.open("acme-dental")


def test_new_refuses_an_existing_slug(root, no_reseal):
    assert cli.main(["workspace", "new", "fx"]) == EXIT_EXISTS
    assert not no_reseal


@pytest.mark.parametrize("slug", ["Bad", "../x", "a"])
def test_new_refuses_bad_slugs(root, no_reseal, slug):
    assert cli.main(["workspace", "new", slug]) == EXIT_COULD_NOT_RUN


def test_new_inside_a_workspace_is_refused(root, no_reseal, monkeypatch):
    monkeypatch.chdir(root / "fx")
    assert cli.main(["workspace", "new", "acme-dental"]) == EXIT_COULD_NOT_RUN
    assert not (root / "acme-dental").exists()
