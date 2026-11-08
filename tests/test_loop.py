"""`mseo loop verify-live` and `mseo loop measure`: after-publish checks and the 28-day measurement, written back
through the content log. The network is never touched: check_url is replaced."""

from __future__ import annotations

import json

import pytest

from mseo import cli, loop, workspace
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FX
from tests.fakes import FakeKeychain
from tests.test_content_topics import PAGE_FIELDS, RANK_FIELDS, make_run

TODAY = "2026-11-07"
SITE = FX["business"]["site"]
URL = f"{SITE}/good-article/"


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    return workspace.open("fx", keychain=FakeKeychain())


def read_log(ws):
    return json.loads(ws.file("content_log").read_text())


def set_published(ws, date="2026-11-01", url=URL, slug="good-article", **more):
    log = read_log(ws)
    for a in log["articles"]:
        if a["slug"] == slug:
            a.update({"status": "published", "url": url, "date": date, **more})
    ws.file("content_log").write_text(json.dumps(log))


def run(*argv):
    return cli.main(list(argv), keychain=FakeKeychain())


# ---------------------------------------------------------------- verify-live

def test_live_article_gets_a_live_check_and_exit_0(fx, monkeypatch):
    seen = []
    monkeypatch.setattr(loop, "check_url", lambda u, timeout=20: seen.append(u) or {"url": u, "status": 200, "ok": True})
    set_published(fx)
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_OK
    assert seen == [URL]
    entry = next(a for a in read_log(fx)["articles"] if a["slug"] == "good-article")
    assert entry["live_check"] == {"date": TODAY, "status": 200}


def test_non_200_is_exit_1_and_recorded(fx, monkeypatch):
    monkeypatch.setattr(loop, "check_url", lambda u, timeout=20: {"url": u, "status": 404, "ok": False})
    set_published(fx)
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_FAIL
    assert next(a for a in read_log(fx)["articles"] if a["slug"] == "good-article")["live_check"]["status"] == 404


@pytest.mark.parametrize("date", ["2026-11-07", "2026-10-20", "2026-11-09"])
def test_only_posts_published_1_to_14_days_ago_are_fetched(fx, monkeypatch, date):
    monkeypatch.setattr(loop, "check_url", lambda u, timeout=20: pytest.fail("fetched"))
    set_published(fx, date=date)
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_OK


def test_drafts_are_never_fetched(fx, monkeypatch):
    monkeypatch.setattr(loop, "check_url", lambda u, timeout=20: pytest.fail("fetched"))
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_OK


def test_published_gbp_posts_wait_for_gbp_seen(fx, monkeypatch, capsys):
    monkeypatch.setattr(loop, "check_url", lambda u, timeout=20: pytest.fail("fetched"))
    log = read_log(fx)
    log["gbp"][0]["status"] = "published"
    fx.file("content_log").write_text(json.dumps(log))
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_OK
    assert read_log(fx)["gbp"][0]["live_check"] == loop.NEEDS_CHECK and "needs_check" in capsys.readouterr().out
    g = read_log(fx)["gbp"][0]
    assert run("loop", "verify-live", "--workspace", "fx", "--gbp-seen", g["date"], g["headline"].upper()) == EXIT_OK
    assert read_log(fx)["gbp"][0]["live_check"] == {"date": TODAY, "status": "seen"}


def test_unreadable_log_is_3(fx):
    fx.file("content_log").write_text("{nope")
    assert run("loop", "verify-live", "--workspace", "fx") == EXIT_COULD_NOT_RUN


# ---------------------------------------------------------------- measure

def pages(url, clicks, impressions):
    return [{"page": url, "clicks": clicks, "impressions": impressions, "ctr": 0.1, "position": 5.0},
            {"page": f"{SITE}/other/", "clicks": 99, "impressions": 999, "ctr": 0.1, "position": 5.0}]


def test_measure_sums_the_four_weeks_after_publish(fx):
    set_published(fx, date="2026-10-01")  # ISO week 2026-W40 -> W41..W44 count
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 1, 10))}, week="2026-W40")  # publish week: not counted
    for n, week in enumerate(("2026-W41", "2026-W42", "2026-W43", "2026-W44"), 1):
        make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL.rstrip("/") if n % 2 else URL, n, 10 * n))}, week=week, minute=n)
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 50, 500))}, week="2026-W45", minute=9)  # the fifth week: not counted
    assert run("loop", "measure", "--workspace", "fx", "--week", "2026-W45") == EXIT_OK
    entry = next(a for a in read_log(fx)["articles"] if a["slug"] == "good-article")
    m = entry["measured_28d"]
    assert (m["clicks"], m["impressions"], m["weeks_counted"], m["status"]) == (10, 100, 4, "complete")
    assert m["weeks"] == ["2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"]


def test_latest_ok_record_per_week_wins(fx):
    set_published(fx, date="2026-10-01")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 1, 10))}, week="2026-W41")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 7, 70))}, week="2026-W41", minute=2)
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 100, 1000))}, week="2026-W41", minute=4, status="error")
    results = loop.measure(fx, "2026-W45")
    assert results[0]["measured_28d"]["clicks"] == 7 and results[0]["measured_28d"]["status"] == "incomplete"


def test_fewer_than_four_weeks_is_incomplete_and_a_future_publish_is_before_data(fx):
    set_published(fx, date="2026-10-01")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 2, 20))}, week="2026-W41")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 3, 30))}, week="2026-W43", minute=1)
    m = loop.measure(fx, "2026-W45")[0]["measured_28d"]
    assert (m["clicks"], m["weeks_counted"], m["status"]) == (5, 2, "incomplete")
    set_published(fx, date="2026-11-01")  # after the newest record (W43 ends 2026-10-25)
    m = loop.measure(fx, "2026-W45")[0]["measured_28d"]
    assert m["status"] == "before_data" and m["weeks_counted"] == 0


def test_records_after_the_measured_week_are_ignored(fx):
    set_published(fx, date="2026-10-01")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 2, 20))}, week="2026-W41")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 3, 30))}, week="2026-W43", minute=1)
    assert loop.measure(fx, "2026-W41")[0]["measured_28d"]["weeks_counted"] == 1


def test_rank_for_the_entrys_query_comes_from_rankings(fx):
    set_published(fx, date="2026-10-01", query="where to get your car fixed")
    make_run(fx, "search_console", {"pages.csv": (PAGE_FIELDS, pages(URL, 2, 20))}, week="2026-W41")
    rows = [{"keyword": "Where to get your car fixed", "position": 4, "previous": 6, "url": URL, "local_pack": "", "map_pack": "",
             "maps": "", "as_of": "2026-10-30", "maps_as_of": ""}]
    rid = make_run(fx, "open_seo", {"rankings.csv": (RANK_FIELDS, rows)}, week="2026-W44", minute=1)
    fields = loop.measure(fx, "2026-W45")[0]
    assert fields["rank"] == {"query": "where to get your car fixed", "position": 4, "run_id": rid, "as_of": "2026-10-30"}
    entry = next(a for a in read_log(fx)["articles"] if a["slug"] == "good-article")
    assert entry["rank"]["position"] == 4 and entry["measured_28d"]["clicks"] == 2


def test_unpublished_articles_are_not_measured(fx):
    assert loop.measure(fx, "2026-W45") == [] and "measured_28d" not in read_log(fx)["articles"][0]


def test_same_url_is_trailing_slash_and_www_tolerant():
    assert loop.same_url(URL, URL.rstrip("/")) and loop.same_url(URL, URL.replace("https://", "https://www."))
    assert not loop.same_url(URL, f"{SITE}/other/")


# ------------------------------------------------------------------ applied Ads items: four weeks before and after

def test_applied_batch_items_are_measured_before_and_after(root):
    import json

    from mseo import loop, week, workspace
    from mseo.ads import batch as batch_mod
    from mseo.run import record
    from tests.conftest import FX
    from tests.fakes import FakeKeychain
    from tests.test_week import ADS, _run

    ws = workspace.open("fx", keychain=FakeKeychain())
    tz = FX["timezone"]
    for k, wk in enumerate(["2026-W41", "2026-W42", "2026-W43", "2026-W44", "2026-W46", "2026-W47", "2026-W48", "2026-W49"]):
        _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)}, period=record.week_period(wk, tz), minute=k)
        week.assemble(ws, wk)
    batch_id = "20261102T090000Z-fx-ab12"
    item = {"n": 1, "type": "negative_keyword", "target": {"campaign": "Campaign A", "text": "free kit", "match_type": "exact"},
            "before": None, "after": "free kit", "evidence": [], "expected_metric": "cost", "item_hash": "0" * 64,
            "status": "applied", "applied_at": "2026-11-03"}  # W45: before = W41-W44, after = W46-W49
    batch = {"schema_version": "mseo.batch/1", "batch_id": batch_id, "workspace": "fx", "week": "2026-W44",
             "created": "2026-11-02T09:00:00Z", "items": [item]}
    batch_mod.save_batch(ws, batch)
    out = loop.measure_batches(ws, "2026-W49")
    assert len(out) == 1
    m = out[0]["measured"]
    assert (m["weeks_before"], m["weeks_after"], m["status"]) == (4, 4, "complete")
    assert m["before"]["cost"] == 42.0 and m["after"]["cost"] == 42.0 and m["before"]["conversions"] == 6.0
    saved = json.loads(ws.path("data", "batches", f"{batch_id}.json").read_text())
    assert saved["items"][0]["measured"]["campaigns"] == ["Campaign A"]
    # measured as of an earlier week: fewer after-weeks exist
    assert loop.measure_batches(ws, "2026-W47")[0]["measured"]["status"] == "incomplete"
