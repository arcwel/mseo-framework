"""`mseo ads script install`: prints the Ads Script with the workspace's Sheet ID filled in. It never contacts Google."""

from __future__ import annotations

import json

from mseo import cli, workspace
from mseo.ads import script
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from tests.fakes import FakeKeychain
from tests.test_ads_sheet import SHEET_ID, with_sheet


def test_install_prints_the_script_with_the_sheet_id(root, capsys):
    with_sheet(root, "fx")
    code = cli.main(["ads", "script", "install", "--workspace", "fx", "--dry-run"], keychain=FakeKeychain())
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert f'var SHEET_ID = "{SHEET_ID}";' in out and "__SHEET_ID__" not in out
    assert "Tools" in out and "Scripts" in out  # the install steps follow the script


def test_install_json(root, capsys):
    with_sheet(root, "fx")
    assert cli.main(["ads", "script", "install", "--workspace", "fx", "--json"], keychain=FakeKeychain()) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["sheet_id"] == SHEET_ID and SHEET_ID in data["script"] and isinstance(data["steps"], list) and data["steps"]


def test_install_without_a_sheet_id_exits_3_with_the_instruction(root, capsys):
    code = cli.main(["ads", "script", "install", "--workspace", "fx"], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN
    assert "scripts_sheet_id" in capsys.readouterr().err


def test_render_never_contacts_anything(root):
    with_sheet(root, "fx")
    out = script.render(workspace.open("fx", keychain=FakeKeychain()))
    assert out["sheet_id"] == SHEET_ID and "AdsApp" in out["script"]
    assert "the Scripts tab" in " ".join(out["steps"]) or any("Scripts" in s for s in out["steps"])


def test_script_header_states_the_trust_boundary_and_the_layout(root):
    with_sheet(root, "fx")
    text = script.render(workspace.open("fx", keychain=FakeKeychain()))["script"]
    head = " ".join(text[:text.index("var SHEET_ID")].replace("\n *", " ").split())  # comment lines re-flowed
    assert "checks no approval record" in head and "the Sheet tab is the gate" in head  # C: not "whose batch has an approval record"
    assert "Auction insights" in head and "no GAQL view" in head  # J
    assert "FIRST sheet" in head and "gid 0" in head  # E
    assert 'setNumberFormat("@")' in text and text.index('setNumberFormat("@")') < text.index("range.setValues(all)")  # A
    assert "batch#n#campaign" in head and 'indexOf("failed:") !== 0' in text  # C: idempotency key and retry
    assert "change_event.campaign FROM change_event" in text and "campaign.name FROM change_event" not in text  # D
    assert "try {\n      t = builders[name](period);" in text and "_errors" in text  # D: every builder guarded
    assert 'replace(/\\\\/g, "\\\\\\\\").replace(/\'/g' in text  # L7: backslash before quote
    assert "isExplicitlyShared()" in text  # L8
    assert "ad_group.name = " in text and "getMatchType()" in text  # C: pause by ad group and match type
    assert text.count("writeTable_(") >= 4  # tables, applied_log, _errors, manifest
