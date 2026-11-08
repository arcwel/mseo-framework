"""M2 step 9: browser-driven Search Console and GA4 exports through `mseo pull` (D1, Agent pulls and Adapters).

The fake agent leaves what a real run leaves in data/inbox/: the page snapshot that shows the account, and the
report's export file. Python checks the account from the snapshot, never from the URL or the agent's words."""

import datetime as dt
import json

import pytest

from mseo import seal, workspace
from mseo.adapters import browser_pull
from mseo.browser import extract
from tests.conftest import FX, FY, SYNTHETIC
from tests.fake_agent import ACCOUNT_PAGE, FakeRunner
from tests.fakes import FakeKeychain
from tests.test_export_pull import DAYS, W44, sc_zip

NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)
SC, GA4 = FX["accounts"]["search_console"]["id"], FX["accounts"]["ga4"]["id"]
SIGN_IN = {"snapshot-account.txt": ACCOUNT_PAGE.format(name="Agent", email=FX["identity"]["email"])}


SC_PAGE = "https://search.google.com/search-console/performance/search-analytics"


def sc_snapshot(prop):
    return f'URL: https://search.google.com/search-console/performance/search-analytics\ncombobox "{prop}" [ref_38]\n'


def ga4_snapshot(selected, other="555666777"):
    return ('URL: https://analytics.google.com/analytics/web/\nlistbox "Properties & Apps" [ref_734]\n'
            f' option [ref_1]\n  link [ref_2]\n   generic "other site" [ref_3]\n   generic "{other}" [ref_4]\n'
            f' option [ref_5]\n  link [ref_6]\n   generic "shop site" [ref_7]\n   generic "{selected}" [ref_8]\n'
            '   img "check" [ref_9]\n')


class Seq:
    def __init__(self, *runs):
        self.runs = list(runs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return FakeRunner(self.runs.pop(0) if self.runs else {})(argv, **kw)


@pytest.fixture
def ws(root, tmp_path, monkeypatch):
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": "mseo-fx"}}}}))
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []},
              pull_test=lambda *_: {"status": "pass", "rows": []}, sync=None, chrome_state=state)
    monkeypatch.setattr("mseo.doctor.today", lambda: dt.date(2026, 11, 9))
    return w


def _zip_bytes(tmp_path, days=DAYS):
    return sc_zip(tmp_path / "z.zip", days).read_bytes()


def _ga4_bytes():
    return (SYNTHETIC / "fx" / "exports" / "ga4-traffic-2026-W44.csv").read_bytes()


# ------------------------------------------------------------------ extractors

def test_sc_selected_property():
    assert extract.sc_selected_property(sc_snapshot(SC)) == (SC, None)
    assert extract.sc_selected_property("URL: https://x.test/\nnothing")[1] == "layout_changed"


def test_ga4_selected_property_is_the_checked_row():
    assert extract.ga4_selected_property(ga4_snapshot(GA4)) == (GA4, None)
    assert extract.ga4_selected_property("URL: https://analytics.google.com/\nlistbox")[1] == "layout_changed"


# ------------------------------------------------------------------ pulls

def test_search_console_pull_ok(ws, tmp_path):
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC),
                           "Performance-search.zip": _zip_bytes(tmp_path)})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert code == 0 and rec["status"] == "ok", rec
    assert rec["adapter"] == "export" and rec["seen_via"] == "page:sc_property_selector"
    assert rec["account_id_seen"] == SC and rec["complete"]["value"] is True
    names = [f["path"] for f in rec["files"]]
    assert "snapshot-search_console.txt" in names and "export.zip" in names and "dates.csv" in names
    assert rec["usage"]["turns"] > 0
    assert not any(ws.path("data", "inbox").iterdir())
    assert len(runner.calls) == 2


def test_wrong_property_saves_nothing(ws, tmp_path):
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot("sc-domain:other.test"),
                           "Performance-search.zip": _zip_bytes(tmp_path)})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert code == 1 and rec["status"] == "wrong_account" and rec["files"] == []
    assert rec["account_id_seen"] == "sc-domain:other.test"
    assert not any(ws.path("data", "inbox").iterdir())
    assert not any(p.name != "record.json" for p in ws.path("data", "runs", rec["run_id"]).iterdir())


def test_failed_sign_in_skips_the_export(ws):
    bad = {"snapshot-account.txt": ACCOUNT_PAGE.format(name="x", email="operator@agency.example")}
    runner = Seq(bad)
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert rec["status"] == "wrong_account" and "sign-in" in rec["error"] and len(runner.calls) == 1


def test_skip_sign_in_when_the_job_already_checked(ws, tmp_path):
    runner = Seq({"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": _zip_bytes(tmp_path)})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW, signed_in=True)
    assert rec["status"] == "ok" and len(runner.calls) == 1


def test_no_export_is_an_error(ws):
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC)})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert rec["status"] == "error" and "no export" in rec["error"]


def test_missing_snapshot_is_unverified_and_keeps_nothing(ws, tmp_path):
    runner = Seq(SIGN_IN, {"Performance-search.zip": _zip_bytes(tmp_path)})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert rec["status"] == "unverified_account" and rec["files"] == []
    assert not any(ws.path("data", "inbox").iterdir())


def test_within_lag_is_ok_but_incomplete(ws, tmp_path, monkeypatch):
    monkeypatch.setattr("mseo.doctor.today", lambda: dt.date(2026, 11, 2))
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": _zip_bytes(tmp_path)})
    _, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert rec["status"] == "ok" and rec["complete"] == {"value": False, "reason": "within reporting lag"}


def test_ga4_pull_ok(ws):
    runner = Seq(SIGN_IN, {"snapshot-ga4.txt": ga4_snapshot(GA4), "traffic.csv": _ga4_bytes()})
    code, rec, _ = browser_pull.pull(ws, "ga4", W44, runner=runner, now=NOW)
    assert rec["status"] == "ok" and rec["seen_via"] == "page:ga4_property_picker" and rec["account_id_seen"] == GA4


def test_duplicate_period_exits_2_unless_forced(ws, tmp_path):
    files = {"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": _zip_bytes(tmp_path)}
    browser_pull.pull(ws, "search_console", W44, runner=Seq(SIGN_IN, dict(files)), now=NOW)
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=Seq(SIGN_IN, dict(files)), now=NOW)
    assert code == 2 and rec is None
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=Seq(SIGN_IN, dict(files)), now=NOW,
                                     force=True)
    assert code == 0


def test_prompt_carries_the_period_and_property(ws):
    from mseo.browser import run as browser_run

    text = browser_run.prompt("export_search_console", ws, source="search_console", **browser_pull.values(
        ws, "search_console", W44))
    assert "20261026" in text and "20261101" in text and "snapshot-search_console.txt" in text
    raw = (browser_run.PROMPTS / "export_search_console.md").read_text()
    assert SC not in raw


def test_cli_pull_without_file_runs_the_agent(ws, tmp_path, monkeypatch):
    from mseo import cli

    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": _zip_bytes(tmp_path)})
    monkeypatch.setattr(browser_pull, "DEFAULT_RUNNER", runner)
    code = cli.main(["pull", "--workspace", "fx", "--source", "search_console", "--week", "2026-W44", "--json"],
                    keychain=FakeKeychain())
    assert code == 0 and len(runner.calls) == 2


def test_a_failed_pull_does_not_block_a_retry(ws, tmp_path):
    browser_pull.pull(ws, "search_console", W44, runner=Seq(SIGN_IN, {}), now=NOW)
    files = {"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": _zip_bytes(tmp_path)}
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=Seq(SIGN_IN, files), now=NOW)
    assert code == 0 and rec["status"] == "ok"


def test_the_agents_failure_reply_is_the_error(ws):
    class Failing(Seq):
        def __call__(self, argv, **kw):
            self.calls.append(argv)
            if len(self.calls) == 1:
                return FakeRunner(SIGN_IN)(argv, **kw)
            return FakeRunner({"Performance-search.zip": b"zip"}, reply="failed: browser not connected")(argv, **kw)

    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=Failing(), now=NOW)
    assert code != 0 and rec["status"] == "error"
    assert "browser not connected" in rec["error"]
    assert "discarded Performance-search.zip" in rec["error"]
    assert not list(ws.path("data", "inbox").iterdir())


# ---------------------------------------------------------------- Google Ads (Report Editor CSV, step 10)

ADS = FX["accounts"]["google_ads"]["id"]


def ads_snapshot(cid, name="Fx Synthetic Shop"):
    return f'URL: https://ads.google.com/aw/campaigns?ocid=1\ngeneric "{cid} {name}" [ref_24]\n'


def ads_csv(rng="October 26, 2026 - November 1, 2026", rows=None):
    rows = rows if rows is not None else [
        'Enabled,Synthetic Repairs,USD,16.00,Eligible,Smart,18.99,37.24%,4.83,"2,276",51,2.24%,91.72',
        'Enabled,Synthetic Paint,USD,10.00,Eligible,Search,1.00,10.00%,8.00,100,10,10.00%,8.00']
    return ("Untitled report\n" f'"{rng}"\n'
            "Campaign state,Campaign,Currency code,Budget,Campaign status,Campaign type,Conversions,Conv. rate,"
            "Cost / conv.,Impr.,Clicks,CTR,Cost\n" + "".join(r + "\n" for r in rows))


def test_ads_header_customer_id():
    assert extract.ads_header_customer_id(ads_snapshot(ADS)) == (ADS, None)
    assert extract.ads_header_customer_id("URL: https://accounts.google.com/v3/signin/x\n")[1] == "signed_out"
    assert extract.ads_header_customer_id("URL: https://ads.google.com/aw/x\nnothing")[1] == "layout_changed"


def test_ads_csv_parses_campaigns_and_its_range(tmp_path):
    from mseo.adapters import google_ads

    p = tmp_path / "Untitled report.csv"
    p.write_text(ads_csv(rows=['Enabled,Synthetic Repairs,USD,16.00,Eligible,Smart,18.99,37.24%,4.83,"2,276",51,'
                               '2.24%,91.72', "Total: Account,,,,,,18.99,,,2276,51,,91.72"]))
    parsed = google_ads.parse(p)
    assert google_ads.coverage(parsed) == ("2026-10-26", "2026-11-01")
    assert parsed["rows"] == [{"campaign": "Synthetic Repairs", "campaign_type": "Smart", "status": "Eligible",
                               "currency": "USD", "cost": 91.72, "clicks": 51, "impressions": 2276,
                               "conversions": 18.99, "calls": None}]


def test_ads_csv_without_its_range_line_is_an_error(tmp_path):
    from mseo.adapters import export, google_ads

    p = tmp_path / "r.csv"
    p.write_text("Campaign,Cost\nx,1\n")
    with pytest.raises(export.ExportError):
        google_ads.parse(p)


def test_ads_pull_ok(ws):
    files = {"snapshot-google_ads.txt": ads_snapshot(ADS), "Untitled report.csv": ads_csv()}
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=Seq(SIGN_IN, files), now=NOW)
    assert code == 0 and rec["status"] == "ok", rec["error"]
    assert rec["seen_via"] == "page:ads_account_header" and rec["account_id_seen"] == ADS
    assert [f["path"] for f in rec["files"]] == ["snapshot-google_ads.txt", "export.csv", "campaigns.csv"]
    assert rec["files"][2]["rows"] == 2 and rec["complete"]["value"] is True


def test_ads_other_account_saves_nothing(ws):
    files = {"snapshot-google_ads.txt": ads_snapshot(FY["accounts"]["google_ads"]["id"]), "Untitled report.csv": ads_csv()}
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=Seq(SIGN_IN, files), now=NOW)
    assert rec["status"] == "wrong_account" and rec["files"] == []
    assert not list(ws.path("data", "inbox").iterdir())


def test_ads_export_for_another_range_is_an_error(ws):
    files = {"snapshot-google_ads.txt": ads_snapshot(ADS),
             "Untitled report.csv": ads_csv(rng="October 31, 2026 - November 6, 2026")}
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=Seq(SIGN_IN, files), now=NOW)
    assert rec["status"] == "error" and "outside" in rec["error"]


def test_ads_prompt_names_the_customer_id_and_dates(ws):
    from mseo.browser import run as browser_run

    text = browser_run.prompt("export_google_ads", ws, source="google_ads",
                              **browser_pull.values(ws, "google_ads", W44))
    assert ADS in text and "10/26/2026" in text and "11/1/2026" in text
    assert "Report Editor" in text


def test_ads_phone_calls_column_is_read_when_present(tmp_path):
    """Recon 2026-11-07: Report Editor offers a "Phone calls" metric; the pull adds it as the last column."""
    from mseo.adapters import google_ads

    p = tmp_path / "r.csv"
    text = ads_csv(rows=['Enabled,Synthetic Repairs,USD,16.00,Eligible,Smart,18.99,37.24%,4.83,"2,276",51,2.24%,'
                         '91.72,14'])
    p.write_text(text.replace("CTR,Cost\n", "CTR,Cost,Phone calls\n"))
    assert google_ads.parse(p)["rows"][0]["calls"] == 14


def test_ads_without_phone_calls_has_calls_empty(tmp_path):
    from mseo.adapters import google_ads

    p = tmp_path / "r.csv"
    p.write_text(ads_csv())
    assert google_ads.parse(p)["rows"][0]["calls"] is None


def test_ads_prompt_adds_phone_calls(ws):
    from mseo.browser import run as browser_run

    text = browser_run.prompt("export_google_ads", ws, source="google_ads",
                              **browser_pull.values(ws, "google_ads", W44))
    assert "Phone calls" in text


def test_the_page_totals_snapshot_is_kept_with_the_run(ws, tmp_path):
    from tests.fake_agent import export_metrics, totals_text

    export = _zip_bytes(tmp_path)
    cards = totals_text("search_console", export_metrics("search_console", "p.zip", export))
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC),
                           "totals-search_console.txt": f"URL: {SC_PAGE}\n{cards}", "Performance-search.zip": export})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert code == 0 and "totals-search_console.txt" in [f["path"] for f in rec["files"]]
    assert not (ws.path("data", "inbox") / "totals-search_console.txt").exists()


def test_the_totals_snapshot_is_never_taken_for_the_export(ws, tmp_path):
    runner = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC), "totals-search_console.txt": "URL: x"})
    code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW)
    assert rec["status"] == "error" and not (ws.path("data", "inbox") / "totals-search_console.txt").exists()


def test_an_export_named_like_a_run_file_is_ingested_under_a_fixed_name(ws):
    # Sonnet pentest low 8: an export called record.json was overwritten by the record, so its hash was wrong
    files = {"snapshot-ga4.txt": ga4_snapshot(GA4), "record.json": _ga4_bytes()}
    code, rec, _ = browser_pull.pull(ws, "ga4", W44, runner=Seq(SIGN_IN, files), now=NOW)
    assert rec["status"] == "ok", rec["error"]
    from mseo.fsutil import sha256_file

    run = ws.path("data", "runs", rec["run_id"])
    for f in rec["files"]:
        assert sha256_file(run / f["path"]) == f["sha256"], f["path"]
    assert "export.json" in [f["path"] for f in rec["files"]]


def test_a_linked_snapshot_or_totals_file_is_refused_without_a_crash(ws, tmp_path):
    # Sonnet review low 2: ingest refused the link but outside any try, so pull crashed and left a run folder behind
    from tests.fake_agent import export_metrics, totals_text

    export = _zip_bytes(tmp_path)
    cards = totals_text("search_console", export_metrics("search_console", "p.zip", export))
    for linked in ("snapshot-search_console.txt", "totals-search_console.txt"):
        seq = Seq(SIGN_IN, {"snapshot-search_console.txt": sc_snapshot(SC),
                            "totals-search_console.txt": f"URL: {SC_PAGE}\n{cards}", "Performance-search.zip": export})

        def runner(argv, *, cwd, env, timeout, seq=seq, linked=linked):
            out = seq(argv, cwd=cwd, env=env, timeout=timeout)
            path = cwd / "data" / "inbox" / linked
            if path.exists():
                real = tmp_path / f"real-{linked}"
                real.write_bytes(path.read_bytes())
                path.unlink()
                path.symlink_to(real)
            return out

        code, rec, _ = browser_pull.pull(ws, "search_console", W44, runner=runner, now=NOW, force=True)
        assert code != 0 and rec["status"] in ("error", "unverified_account"), (linked, rec)
        assert rec["files"] == [] and not any(ws.path("data", "inbox").iterdir()), linked
