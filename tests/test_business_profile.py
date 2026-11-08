"""M2 step 10 (build plan A2): the Business Profile location list, built on synthetic fixtures.

The page shape is NOT verified: the identity has no Business Profile access yet, and the operator's own locations
page lists other customers, so no recon was possible (2026-11-07). Recon replaces these fixtures once access lands.
"""

import datetime as dt
import json

import pytest

from mseo import identity, workspace
from mseo.adapters import business_profile
from mseo.browser import extract
from tests.conftest import FX, FY
from tests.fake_agent import ACCOUNT_PAGE, FakeRunner
from tests.fakes import FakeKeychain
from tests.test_export_pull import W44

LOC = FX["accounts"]["business_profile"]["id"]
OTHER = "9000800070006000500"
NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)
SIGN_IN = {"snapshot-account.txt": ACCOUNT_PAGE.format(name="Agent", email=FX["identity"]["email"])}


def row(lid, name, address, state):
    return (f'  row "{name}"\n    link "{name}" href="https://business.google.com/dashboard/l/{lid}"\n'
            f'    generic "{address}"\n    generic "{state}"\n')


def locations_page(*rows):
    return "URL: https://business.google.com/locations\ntable \"Locations\"\n" + "".join(rows)


ADDR = FX["locations"][0]["address"]
OTHER_ADDR = FY["locations"][0]["address"]
MINE = row(LOC, "Fx Synthetic Shop", ADDR, "Verified")
THEIRS = row(OTHER, "Other Co", OTHER_ADDR, "Verified")


class Seq:
    def __init__(self, *runs):
        self.runs, self.calls = list(runs), []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return FakeRunner(self.runs.pop(0) if self.runs else {})(argv, **kw)


@pytest.fixture
def ws(root, monkeypatch):
    from mseo import seal

    monkeypatch.setattr(seal, "require_pull_ready", lambda ws: None)
    return workspace.open("fx", keychain=FakeKeychain())


def _unmanual(ws):
    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg["accounts"]["business_profile"].pop("check")
    path.write_text(json.dumps(cfg))
    return workspace.open("fx", keychain=FakeKeychain())


def test_locations_extractor_reads_rows_by_link_id():
    rows, reason = extract.bp_locations(locations_page(MINE, THEIRS))
    assert reason is None
    assert rows == [{"id": LOC, "name": "Fx Synthetic Shop", "address": ADDR,
                     "verification": "Verified"},
                    {"id": OTHER, "name": "Other Co", "address": OTHER_ADDR,
                     "verification": "Verified"}]
    assert extract.bp_location_ids(locations_page(MINE, THEIRS)) == ([LOC, OTHER], None)


def test_locations_extractor_signed_out_and_unknown_pages():
    assert extract.bp_locations("URL: https://accounts.google.com/v3/signin/x\n")[1] == "signed_out"
    assert extract.bp_locations("URL: https://business.google.com/locations\nno rows")[1] == "layout_changed"


def test_pull_keeps_only_the_customers_location(ws):
    code, rec, _ = business_profile.pull(ws, W44, runner=Seq(SIGN_IN, {"snapshot-business_profile.txt":
                                                                      locations_page(MINE)}), now=NOW)
    assert code == 0 and rec["status"] == "ok" and rec["seen_via"] == "page:bp_locations"
    assert rec["account_id_seen"] == LOC
    assert [f["path"] for f in rec["files"]] == ["snapshot-business_profile.txt", "locations.csv"]
    text = ws.path("data", "runs", rec["run_id"], "locations.csv").read_text()
    assert "Fx Synthetic Shop" in text and "Verified" in text


def test_pull_with_other_locations_stores_none_of_them(ws):
    code, rec, _ = business_profile.pull(ws, W44, runner=Seq(SIGN_IN, {"snapshot-business_profile.txt":
                                                                      locations_page(MINE, THEIRS)}), now=NOW)
    assert rec["status"] == "ok"
    run = ws.path("data", "runs", rec["run_id"])
    assert [f["path"] for f in rec["files"]] == ["locations.csv"]
    blob = json.dumps(rec) + "".join(p.read_text() for p in run.iterdir())
    assert OTHER not in blob and "Other Co" not in blob
    assert "1 other location" in rec["complete"]["reason"]
    assert not list(ws.path("data", "inbox").iterdir())


def test_pull_without_the_location_is_wrong_account(ws):
    code, rec, _ = business_profile.pull(ws, W44, runner=Seq(SIGN_IN, {"snapshot-business_profile.txt":
                                                                      locations_page(THEIRS)}), now=NOW)
    assert code == 1 and rec["status"] == "wrong_account" and rec["files"] == []
    assert OTHER not in json.dumps(rec)


def test_manual_check_skips_the_lister(ws):
    out = identity.identity_check(ws, sources=["business_profile"], runner=Seq(), now=NOW)
    assert out["results"] == {"business_profile": "manual"}


def test_lister_runs_when_not_manual(ws):
    ws = _unmanual(ws)
    out = identity.identity_check(ws, sources=["business_profile"],
                                  runner=Seq(SIGN_IN, {"snapshot-business_profile-list.txt": locations_page(MINE)}),
                                  now=NOW)
    assert out["results"] == {"business_profile": "ok"}


def test_cli_pull_dispatches_business_profile(ws, monkeypatch):
    from mseo import cli

    monkeypatch.setattr(business_profile, "DEFAULT_RUNNER",
                        Seq(SIGN_IN, {"snapshot-business_profile.txt": locations_page(MINE)}))
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    assert cli.main(["pull", "--workspace", "fx", "--source", "business_profile", "--week", "2026-W44"],
                    keychain=FakeKeychain()) == 0
