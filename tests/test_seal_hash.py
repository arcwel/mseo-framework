"""The seal hashes the trust anchors an agent could edit (customer.json, facts.json; audit 32 / L4) and doctor refuses
a Chrome profile or identity email shared with a sibling workspace."""

from __future__ import annotations

import json

from mseo import doctor, seal, workspace
from mseo.exits import EXIT_FAIL
from tests.conftest import FX, FY, edit_config
from tests.fakes import FakeKeychain
from tests.test_doctor_and_new import WP, chrome_state, healthy  # noqa: F401 - fixtures
from tests.test_seal import passing_live_test


def _sealed(root, chrome_state):
    ws = workspace.open("fx", keychain=FakeKeychain({("mseo.fx.wordpress", "fx"): WP}))
    return ws, seal.seal(ws, live_test=passing_live_test, sync=lambda: None, chrome_state=chrome_state)


def test_seal_records_the_customer_and_facts_hashes(root, chrome_state):
    ws, result = _sealed(root, chrome_state)
    record = json.loads(ws.path(".mseo", "seal.json").read_text())
    assert len(record["customer_sha256"]) == 64 and len(record["facts_sha256"]) == 64
    assert result.record["customer_sha256"] == record["customer_sha256"]
    assert seal.check_seal(ws) == []


def test_an_edited_customer_json_makes_the_seal_stale(root, chrome_state):
    ws, _ = _sealed(root, chrome_state)
    edit_config(root, "fx", lambda c: c["accounts"]["ga4"].update(id=FY["accounts"]["ga4"]["id"]))
    problems = seal.check_seal(ws)
    assert any("customer.json changed" in p for p in problems), problems


def test_an_edited_facts_json_makes_the_seal_stale(root, chrome_state):
    ws, _ = _sealed(root, chrome_state)
    facts = root / "fx" / FX["files"]["facts"]
    data = json.loads(facts.read_text())
    data["claims"].append("a claim the agent added")
    facts.write_text(json.dumps(data))
    assert any(FX["files"]["facts"] in p and "changed" in p for p in seal.check_seal(ws))


def test_a_seal_without_the_hashes_is_stale(root, chrome_state):
    ws, _ = _sealed(root, chrome_state)
    path = ws.path(".mseo", "seal.json")
    record = json.loads(path.read_text())
    del record["customer_sha256"]
    path.write_text(json.dumps(record))
    assert any("predates" in p for p in seal.check_seal(ws))


def test_doctor_refuses_a_shared_chrome_profile(healthy, chrome_state, root):
    ws, _ = healthy
    edit_config(root, "fy", lambda c: c["identity"].update(chrome_profile=FX["identity"]["chrome_profile"]))
    report = doctor.run(ws, chrome_state=chrome_state)
    check = next(c for c in report.checks if c["check"] == "identity unique across workspaces")
    assert not check["ok"] and "fy" in check["detail"] and FX["identity"]["chrome_profile"] in check["detail"]
    assert report.code == EXIT_FAIL
    assert json.loads((root / "fx" / "customer.json").read_text()) == FX  # doctor writes nothing to customer.json


def test_doctor_refuses_a_shared_identity_email(healthy, chrome_state, root):
    ws, _ = healthy
    edit_config(root, "fy", lambda c: c["identity"].update(email=FX["identity"]["email"]))
    report = doctor.run(ws, chrome_state=chrome_state)
    check = next(c for c in report.checks if c["check"] == "identity unique across workspaces")
    assert not check["ok"] and "email" in check["detail"]
    assert report.code == EXIT_FAIL


def test_doctor_passes_when_identities_differ(healthy, chrome_state):
    ws, _ = healthy
    report = doctor.run(ws, chrome_state=chrome_state)
    assert next(c for c in report.checks if c["check"] == "identity unique across workspaces")["ok"]
    assert report.code == 0, [c for c in report.checks if not c["ok"]]


def test_identity_clashes_reads_siblings_only(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert seal.identity_clashes(ws) == []
    edit_config(root, "fy", lambda c: c["identity"].update(chrome_profile=FX["identity"]["chrome_profile"]))
    assert seal.identity_clashes(ws) == [f"fy: chrome profile {FX['identity']['chrome_profile']}"]
