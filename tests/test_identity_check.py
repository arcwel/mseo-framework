"""M2 step 8: mseo identity check, the page listers, allow_shared and the doctor rows (D1, The agent identity)."""

import datetime as dt
import json

import pytest

from mseo import doctor, identity, seal, workspace
from mseo.browser import extract
from mseo.run import record
from tests.conftest import FX, FY
from tests.fake_agent import (
    ACCOUNT_PAGE,
    SIGNED_OUT_PAGE,
    FakeRunner,
    ads_list,
    ga4_list,
    sc_list,
)
from tests.fakes import FakeKeychain

NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.UTC)
EMAIL = FX["identity"]["email"]
SC, GA4, ADS = FX["accounts"]["search_console"]["id"], FX["accounts"]["ga4"]["id"], FX["accounts"]["google_ads"]["id"]
OTHER_ADS = FY["accounts"]["google_ads"]["id"]  # another workspace's account: what a mixed-up identity would see


@pytest.fixture
def ws(root, tmp_path):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []},
              pull_test=lambda *_: {"status": "pass", "rows": []}, sync=None, chrome_state=state)
    return w


def pages(sc=(SC,), ga4_props=(("Shop site", GA4),), ads=(("Shop", ADS),), email=EMAIL, account=None):
    return {"snapshot-account.txt": account or ACCOUNT_PAGE.format(name="Agent", email=email),
            "snapshot-search_console-list.txt": sc_list(*sc),
            "snapshot-ga4-list.txt": ga4_list([("Shop", "100200300")], list(ga4_props)),
            "snapshot-google_ads-list.txt": ads_list(email, *ads)}


class Seq:
    """One FakeRunner per agent run, in order (sign-in first, then the listers)."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return FakeRunner(self.files)(argv, **kw)


def check(ws, files, **kw):
    return identity.identity_check(ws, runner=Seq(files), now=NOW, **kw)


# ------------------------------------------------------------------ extractors

def test_sc_lister_reads_options_and_skips_add_property():
    assert extract.sc_properties(sc_list("sc-domain:a.test", "https://b.test/")) == (["sc-domain:a.test", "https://b.test/"], None)


def test_ga4_lister_reads_account_and_property_numbers():
    text = ga4_list([("A", "111111111"), ("B", "222222222")], [("site", "333333333")])
    assert extract.ga4_ids(text) == ({"accounts": ["111111111", "222222222"], "properties": ["333333333"]}, None)


def test_ads_lister_reads_customer_ids():
    assert extract.ads_customer_ids(ads_list(EMAIL, ("A", ADS), ("B", OTHER_ADS))) == ([ADS, OTHER_ADS], None)


@pytest.mark.parametrize("fn", [extract.sc_properties, extract.ga4_ids, extract.ads_customer_ids])
def test_listers_refuse_an_unknown_page(fn):
    assert fn("Title: Something\nURL: https://example.test/\n---\nnothing")[1] == "layout_changed"


def test_ads_lister_reports_a_signed_out_page():
    assert extract.ads_customer_ids(SIGNED_OUT_PAGE)[1] == "signed_out"


# ------------------------------------------------------------------ results

def test_all_ok(ws):
    out = check(ws, pages())
    assert out["status"] == "ok"
    assert out["results"] == {"google_ads": "ok", "ga4": "ok", "search_console": "ok", "open_seo": "manual",
                              "business_profile": "manual"}
    rec = record.load(ws.path("data", "runs", out["run_id"], "record.json"))
    assert rec["source"] == "identity_check" and rec["status"] == "ok" and rec["identity_shared"] is False
    assert {c["check"] for c in rec["checks"]} == {"google_ads", "ga4", "search_console", "open_seo", "business_profile"}
    assert sorted(f["path"] for f in rec["files"]) == ["snapshot-ga4-list.txt", "snapshot-google_ads-list.txt",
                                                       "snapshot-search_console-list.txt"]
    assert not any(ws.path("data", "inbox").iterdir())


def test_a_foreign_property_refuses_and_drops_that_snapshot(ws):
    out = check(ws, pages(sc=(SC, "sc-domain:other-customer.test")))
    assert out["status"] == "refused" and out["results"]["search_console"] == "foreign"
    rec = record.load(ws.path("data", "runs", out["run_id"], "record.json"))
    assert rec["status"] == "wrong_account" and rec["files"] == []
    assert "other-customer" not in json.dumps(rec)  # foreign IDs are counted, never stored
    assert not any(ws.path("data", "inbox").iterdir())


def test_missing_expected_account(ws):
    out = check(ws, pages(ads=(("Other", OTHER_ADS),)))
    assert out["results"]["google_ads"] in ("missing", "foreign")
    out = check(ws, pages(ads=()))
    assert out["results"]["google_ads"] == "missing" and out["status"] == "refused"


def test_ga4_compares_properties(ws):
    out = check(ws, pages(ga4_props=(("Shop site", GA4), ("Other site", "555666777"))))
    assert out["results"]["ga4"] == "foreign"


def test_wrong_sign_in_stops_before_listing(ws):
    runner = Seq(pages(email="operator@agency.example"))
    out = identity.identity_check(ws, runner=runner, now=NOW)
    assert out["status"] == "refused" and out["sign_in"] == "wrong_account"
    assert out["results"] == {"google_ads": "auth_failed", "ga4": "auth_failed", "search_console": "auth_failed",
                              "open_seo": "manual", "business_profile": "manual"}
    assert len(runner.calls) == 1


def test_signed_out_is_auth_failed(ws):
    out = check(ws, pages(account=SIGNED_OUT_PAGE))
    assert out["sign_in"] == "auth_failed" and out["status"] == "refused"


def test_allow_shared_proceeds_and_marks_shared(ws):
    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg["identity"]["allow_shared"] = True
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    out = identity.identity_check(w, runner=Seq(pages(sc=(SC, "sc-domain:other.test"))), now=NOW, require_seal=False)
    assert out["status"] == "shared" and out["results"]["search_console"] == "foreign"
    rec = record.load(w.path("data", "runs", out["run_id"], "record.json"))
    assert rec["identity_shared"] is True and rec["status"] == "ok"


def test_source_without_a_lister_is_unchecked(ws):
    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg["accounts"]["open_seo"].pop("check")
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    out = identity.identity_check(w, runner=Seq(pages()), now=NOW, require_seal=False)
    assert out["results"]["open_seo"] == "unchecked" and out["status"] == "refused"
    cfg["accounts"]["open_seo"]["check"] = "manual"
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    out = identity.identity_check(w, runner=Seq(pages()), now=NOW, require_seal=False)
    assert out["results"]["open_seo"] == "manual" and out["status"] == "ok"


def test_source_filter(ws):
    runner = Seq(pages())
    out = identity.identity_check(ws, sources=["ga4"], runner=runner, now=NOW)
    assert out["results"] == {"ga4": "ok"}


def test_prompts_name_the_identity_and_no_customer_text(ws):
    from mseo.browser import run as browser_run

    for name in ("list_search_console", "list_ga4", "list_google_ads"):
        raw = (browser_run.PROMPTS / f"{name}.md").read_text()
        assert "{email}" in raw and EMAIL not in raw and ADS not in raw


# ------------------------------------------------------------------ CLI and doctor

def test_cli_exit_codes(ws, monkeypatch):
    from mseo import cli

    monkeypatch.setattr(identity, "DEFAULT_RUNNER", Seq(pages()))
    assert cli.main(["identity", "check", "--workspace", "fx", "--json"], keychain=FakeKeychain()) == 0
    monkeypatch.setattr(identity, "DEFAULT_RUNNER", Seq(pages(sc=(SC, "sc-domain:x.test"))))
    assert cli.main(["identity", "check", "--workspace", "fx", "--json"], keychain=FakeKeychain()) == 1


def test_doctor_flags_unchecked_sources_and_overrides(ws, tmp_path):
    path = ws.path("customer.json")
    cfg = json.loads(path.read_text())
    cfg["accounts"]["open_seo"].pop("check")
    cfg["accounts"]["google_ads"]["identity_override"] = {
        "email": "operator@agency.example", "chrome_profile": "Default", "allow_shared": True, "temporary": True,
        "until": "grant lands"}
    path.write_text(json.dumps(cfg))
    w = workspace.open("fx", keychain=FakeKeychain())
    rows = {c["check"]: c for c in doctor.run(w, chrome_state=tmp_path / "none").checks}
    assert rows["open_seo identity check"]["ok"] is False
    assert rows["google_ads identity override"]["ok"] is True
    assert "temporary" in rows["google_ads identity override"]["detail"]
    assert rows["ga4 identity check"]["ok"] is True
