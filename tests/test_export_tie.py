"""Sonnet pentest 2026-11-07, Medium 2: the account check proved the last read, not the export. An export now counts
only when its totals match a totals read in the Chrome tool output on a page of the attested account (same account
key in the tab list URL)."""

from mseo.adapters import browser_pull
from mseo.browser import tie
from tests.fake_agent import FakeRunner, export_metrics, keyed, tab_list, totals_text
from tests.test_attest import body, call, comp, read
from tests.test_browser_pull import NOW, SC, SC_PAGE, SIGN_IN, _zip_bytes, sc_snapshot, ws  # noqa: F401
from tests.test_export_pull import W44


def test_each_source_has_an_account_key_in_its_url():
    assert tie.account_key("search_console", f"{SC_PAGE}?resource_id=sc-domain%3Ax.test&start_date=1") == \
        "sc-domain:x.test"
    assert tie.account_key("ga4", "https://analytics.google.com/analytics/web/#/a12p345/reports/x?params=y") == "345"
    assert tie.account_key("google_ads", "https://ads.google.com/aw/campaigns?ocid=77&__c=1") == "77"
    assert tie.account_key("search_console", SC_PAGE) is None
    assert tie.account_key("google_ads", "https://ads.google.com/aw/x?ocid=1&ocid=2") is None


def _pull(ws, tmp_path, calls):
    export = _zip_bytes(tmp_path)
    files = {"snapshot-search_console.txt": sc_snapshot(SC), "Performance-search.zip": export}
    run = FakeRunner(files, calls=calls)
    seq = iter([FakeRunner(SIGN_IN), run])
    return browser_pull.pull(ws, "search_console", W44, runner=lambda argv, **kw: next(seq)(argv, **kw), now=NOW)[1]


def _totals(tmp_path, scale=1):
    m = export_metrics("search_console", "p.zip", _zip_bytes(tmp_path))
    return totals_text("search_console", {**m, "clicks": m["clicks"] * scale})


def _ours():
    url = keyed("search_console", SC_PAGE)
    return [c if c["name"].endswith("read_page") else tab_list(url) for c in read(body(SC))]


def test_totals_on_the_attested_account_tie_the_export(ws, tmp_path):
    calls = [call("read_page", _totals(tmp_path)), tab_list(keyed("search_console", SC_PAGE))] + _ours()
    assert _pull(ws, tmp_path, calls)["status"] == "ok"


def test_an_export_from_another_property_is_refused(ws, tmp_path):
    # the totals were read on another property, then the agent went back to ours for the account read
    other = f"{SC_PAGE}?resource_id=sc-domain%3Aother.test"
    calls = [call("read_page", _totals(tmp_path)), tab_list(other), comp(action="left_click", coordinate=[1, 1])]
    rec = _pull(ws, tmp_path, calls + _ours())
    assert rec["status"] == "unverified_account" and "attested account" in rec["error"]
    assert rec["files"] == [] and not list(ws.path("data", "inbox").glob("*"))


def test_an_export_whose_totals_differ_from_the_page_is_refused(ws, tmp_path):
    calls = [call("read_page", _totals(tmp_path, scale=3)), tab_list(keyed("search_console", SC_PAGE))] + _ours()
    rec = _pull(ws, tmp_path, calls)
    assert rec["status"] == "unverified_account" and "differ" in rec["error"]


def test_no_totals_read_in_the_stream_is_refused(ws, tmp_path):
    rec = _pull(ws, tmp_path, _ours())
    assert rec["status"] == "unverified_account" and "no totals" in rec["error"]


def test_a_ga4_key_ignores_leading_zeros():
    # Sonnet review low 5: p0345 and p345 name the same property
    assert tie.account_key("ga4", "https://analytics.google.com/analytics/web/#/a1p0345/x") == "345"


GA4_URL = "https://analytics.google.com/analytics/web/#/a1p345/reports/explorer"


def _split_reads(source):
    # live 2026-11-07: the agent reads the header row and the totals row with two read_page calls, then the page text
    text = totals_text(source, {"sessions": 10, "engagedSessions": 5, "keyEvents": 1.0,
                                "conversions": 2.0, "impressions": 100, "cost": 9.5})
    head, _, total = text.partition("\n\n")
    return call("read_page", head), call("read_page", total)


def test_totals_read_in_pieces_since_the_last_page_action_are_joined():
    head, total = _split_reads("ga4")
    calls = [head, total, call("get_page_text", f"Title: x\nURL: {GA4_URL}\n---\nx"), tab_list(GA4_URL)]
    assert tie.stream_totals("ga4", calls, "345") == {"sessions": 10, "engagedSessions": 5, "keyEvents": 1.0}
    head, total = _split_reads("google_ads")
    ads = "https://ads.google.com/aw/reporteditor/view?ocid=77"
    assert tie.stream_totals("google_ads", [head, total, tab_list(ads)], "77") == \
        {"conversions": 2.0, "impressions": 100, "cost": 9.5}


def test_pieces_are_not_joined_across_a_page_action():
    head, total = _split_reads("ga4")
    calls = [head, comp(action="left_click", coordinate=[1, 1]), total, tab_list(GA4_URL)]
    assert tie.stream_totals("ga4", calls, "345") is None


def test_a_page_of_another_account_is_refused_even_when_both_reads_agree(ws, tmp_path):
    # Sonnet review low 4: the keys were only compared with each other, never with the customer's account
    other = f"{SC_PAGE}?resource_id=sc-domain%3Aother.test"
    calls = [call("read_page", _totals(tmp_path)), tab_list(other)]
    calls += [c if c["name"].endswith("read_page") else tab_list(other) for c in read(body(SC))]
    rec = _pull(ws, tmp_path, calls)
    assert rec["status"] == "unverified_account" and "customer's" in rec["error"], rec["error"]


def test_the_key_is_compared_with_the_customers_account():
    assert tie.check("ga4", [], GA4_URL, {}, {}, None, expected="999")[0] == "other_account"
    assert tie.check("search_console", [], f"{SC_PAGE}?resource_id=sc-domain%3AX.test", {}, {}, None,
                     expected="sc-domain:x.test")[0] == "no_page_totals"  # same account: on to the totals


def test_every_metric_the_page_must_show_is_compared():
    # Sonnet review medium 1 (residual): one matching metric was enough; now each non-optional metric must match
    from mseo import shadow

    head, total = _split_reads("ga4")
    calls = [head, total, tab_list(GA4_URL)]
    ours = {"sessions": 10, "engagedSessions": 5, "keyEvents": 1.0}
    assert tie.check("ga4", calls, GA4_URL, ours, shadow.tolerances(), shadow.within) == (None, None)
    full = totals_text("ga4", {"sessions": 10, "engagedSessions": 5, "keyEvents": 1.0})
    partial = full.replace(' columnheader "Key events" [ref_h]\n', "").rsplit(" gridcell", 1)[0]
    calls = [call("read_page", partial), tab_list(GA4_URL)]
    reason, detail = tie.check("ga4", calls, GA4_URL, ours, shadow.tolerances(), shadow.within)
    assert reason == "no_page_totals" and "keyEvents" in detail, detail
    ads = "https://ads.google.com/aw/reporteditor/view?ocid=77"
    head, total = _split_reads("google_ads")  # no clicks or calls on the page: both are optional there
    assert tie.check("google_ads", [head, total, tab_list(ads)], ads,
                     {"conversions": 2.0, "impressions": 100, "cost": 9.5, "clicks": 4, "calls": None},
                     shadow.tolerances(), shadow.within) == (None, None)


def test_a_later_partial_read_does_not_replace_a_full_one():
    # Sonnet review 2, medium 1: a re-read fragment on the same page left the required metrics missing
    head, total = _split_reads("ga4")
    fragment = ('row [ref_1]\n columnheader "Sessions" [ref_h]\n\nrow [ref_9]\n gridcell [ref_a]\n'
                '  generic "Total" [ref_b]\n gridcell [ref_c]\n  generic "10" [ref_d]')
    calls = [head, total, tab_list(GA4_URL), comp(action="left_click", coordinate=[1, 1]), call("read_page", fragment),
             tab_list(GA4_URL)]
    assert tie.stream_totals("ga4", calls, "345") == {"sessions": 10, "engagedSessions": 5, "keyEvents": 1.0}


def test_a_ga4_url_without_the_account_segment_has_a_key():
    # Sonnet review 2, medium 2: GA4 also serves #/p<property>/...
    assert tie.account_key("ga4", "https://analytics.google.com/analytics/web/#/p345/reports/x") == "345"
