"""`mseo report monthly`: the customer-facing month report from the week files, batches, content log and audits.

Gaps are "not available this month", never zero; deltas only when both months have every week complete; no internal
word reaches the customer; the workspace's wording rules are applied to the text.
"""

import datetime as dt
import json
import re

import pytest

from mseo import cli, report, week, workspace
from mseo.run import record
from tests.conftest import FX, edit_config
from tests.fakes import FakeKeychain
from tests.test_week import ADS, GA4, SC_DATES, _run

MONTH, PREV = "2026-10", "2026-09"
SITE = FX["business"]["site"]
INTERNAL = re.compile(r"\b(mseo|run record|shadow|seal|agent|pull|watchdog|batch id|run id|sha)\b", re.I)


@pytest.fixture
def ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


def _weeks(month: str) -> list[str]:
    return report.weeks_of_month(month)


def _fill(ws, month: str, *, sources=("search_console", "ga4", "google_ads"), skip: set[str] = frozenset(),
          incomplete: set[str] = frozenset(), minute: int = 0, scale: int = 1) -> int:
    """Write every week file touching `month`; `skip` weeks get no file, `incomplete` weeks carry complete=false."""
    sc = [{**r, "clicks": r["clicks"] * scale, "impressions": r["impressions"] * scale} for r in SC_DATES]
    files = {"search_console": {"dates.csv": (list(sc[0]), sc)}, "ga4": {"traffic.csv": (list(GA4[0]), GA4)},
             "google_ads": {"campaigns.csv": (list(ADS[0]), ADS)}}
    for w in _weeks(month):
        if w in skip:
            continue
        period = record.week_period(w, FX["timezone"])
        for s in sources:
            rid = _run(ws, s, "ok", files[s], period=period, minute=minute)
            minute += 1
            if w in incomplete:
                path = ws.path("data", "runs", rid, "record.json")
                rec = json.loads(path.read_text())
                rec["complete"] = {"value": False, "reason": "within reporting lag"}
                path.write_text(json.dumps(rec))
        week.assemble(ws, w)
    return minute


def _log(ws, *, articles=(), gbp=()):
    path = ws.file("content_log")
    log = json.loads(path.read_text())
    log["articles"] = log.get("articles", []) + list(articles)
    log["gbp"] = log.get("gbp", []) + list(gbp)
    path.write_text(json.dumps(log))


def _batch(ws, items, *, created="2026-10-14T09:00:00Z", week="2026-W42"):
    folder = ws.path("data", "batches")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "20261014T090000Z-fx-ab12.json").write_text(json.dumps(
        {"schema_version": "mseo.batch/1", "batch_id": "20261014T090000Z-fx-ab12", "workspace": "fx", "week": week,
         "created": created, "source_run_ids": [], "items": items}))


ITEMS = [{"n": 1, "type": "negative_keyword", "target": {"campaign": "Campaign A", "text": "jobs", "match_type": "exact"},
          "before": {"cost": 4.5}, "after": {"negative": "[jobs]"}, "evidence": [], "expected_metric": "cost on this term to 0",
          "reversible": True, "status": "applied", "item_hash": "0" * 64, "applied_at": "2026-10-15",
          "measured": {"before": {"cost": 40.0, "clicks": 20, "conversions": 3}, "after": {"cost": 31.5, "clicks": 18, "conversions": 4},
                       "weeks_before": 4, "weeks_after": 2, "as_of": "2026-W44", "status": "incomplete", "campaigns": ["Campaign A"]}},
         {"n": 2, "type": "budget", "target": {"campaign": "Campaign A"}, "before": {"budget": 20.0}, "after": {"budget": 24.0},
          "evidence": [], "expected_metric": "more conversions at the same cost per conversion", "reversible": True,
          "status": "proposed", "item_hash": "1" * 64}]


def _full_setup(ws):
    m = _fill(ws, PREV)
    _fill(ws, MONTH, minute=m + 1, scale=2)
    _log(ws, articles=[{"type": "article", "slug": "rainy-season", "title": "Rainy-season prep for Testville drivers",
                        "date": "2026-10-08", "status": "published", "url": f"{SITE}/rainy-season/",
                        "measured_28d": {"clicks": 17, "impressions": 900, "weeks_counted": 4, "status": "complete"}}],
         gbp=[{"date": "2026-10-12", "status": "published", "theme": "fair", "headline": "harvest fair parking tips",
               "url": "https://g.co/x/abc"}])
    _batch(ws, ITEMS)
    audits = ws.path("data", "audits")
    audits.mkdir(parents=True)
    (audits / "2026-10-30-site.json").write_text(json.dumps({"schema": "mseo.site-audit/1", "date": "2026-10-30", "findings": [
        {"level": "fail", "check": "missing_title", "detail": "no <title>", "url": f"{SITE}/about/"},
        {"level": "warn", "check": "image_alt", "detail": "2 images", "url": f"{SITE}/"}], "counts": {"fail": 1, "warn": 1, "info": 0}}))


def test_month_with_two_complete_months_has_every_section_and_the_deltas(ws):
    _full_setup(ws)
    out = report.build(ws, MONTH)
    text = out["report"]
    for h in ("## Summary", "## Search", "## Website visits", "## Ads", "## Content", "## Site health", "## AI answers",
              "## Next month plan", "## Notes"):
        assert h in text, h
    n = len(_weeks(MONTH))
    shared = len(set(_weeks(MONTH)) & set(_weeks(PREV)))  # a week shared by both months counts in both, at the newer scale
    assert out["search"]["clicks"] == 5 * 2 * n
    assert out["search"]["previous"]["clicks"] == 5 * (len(_weeks(PREV)) - shared) + 10 * shared
    assert out["search"]["complete"] and out["search"]["previous"]["complete"]
    assert "last month" in text.split("## Search")[1].split("## Website")[0]
    assert out["visits"]["sessions"] == 12 * n and out["visits"]["top_channels"][0]["channel"] == "Organic Search"
    assert out["ads"]["cost"] == pytest.approx(11.75 * n) and out["ads"]["calls"] == 2 * n
    assert out["ads"]["cost_per_conversion"] == pytest.approx(11.75 / 1.5, abs=0.01)
    ads = text.split("## Ads")[1].split("## Content")[0]
    assert "search term 'jobs'" in ads and "Campaign A" in ads and "0" * 64 not in text and "cost on this term" in ads
    assert "cost 40.00 -> 31.50" in ads and "2 week(s) after so far" in ads
    assert f"{SITE}/rainy-season/" in text and "17 clicks" in text
    assert "harvest fair parking tips" in text
    assert "1 check" in text.split("## Site health")[1].split("## AI answers")[0]
    assert "not available this month" in text.split("## AI answers")[1].split("## Next month")[0]
    plan = text.split("## Next month plan")[1].split("## Notes")[0]
    assert "budget" in plan and "Campaign A" in plan and "good-article" not in plan  # drafts are named by title
    assert "missing title" in plan
    assert "TODO:" not in text


def test_no_internal_words_reach_the_customer(ws):
    _full_setup(ws)
    text = report.build(ws, MONTH)["report"]
    assert not INTERNAL.search(text), INTERNAL.search(text).group(0)


def test_comparison_needs_both_months_complete(ws):
    weeks = _weeks(MONTH)
    m = _fill(ws, PREV, skip={_weeks(PREV)[0]})  # a week file missing in the previous month
    _fill(ws, MONTH, minute=m + 1, incomplete={weeks[-1]})  # the last week still within reporting lag
    out = report.build(ws, MONTH)
    text = out["report"]
    assert out["search"]["complete"] is False and out["search"]["previous"]["complete"] is False
    assert "comparison not available" in text.split("## Search")[1].split("## Website")[0]
    assert "last month" not in text.split("## Search")[1].split("## Website")[0]
    notes = text.split("## Notes")[1]
    assert weeks[-1] in notes or "week" in notes
    assert out["search"]["weeks_complete"] == len(weeks) - 1


def test_a_source_without_any_week_is_not_available_never_zero(ws):
    _fill(ws, MONTH, sources=("search_console",))
    out = report.build(ws, MONTH)
    text = out["report"]
    assert out["visits"] is None and out["ads"] is None
    visits = text.split("## Website visits")[1].split("## Ads")[0]
    assert "not available this month" in visits and " 0 " not in visits
    assert "not available this month" in text.split("## Ads")[1].split("## Content")[0]
    assert "comparison not available" in text.split("## Search")[1].split("## Website")[0]


def test_banned_wording_is_replaced_with_the_house_phrase(ws):
    _fill(ws, MONTH)
    _log(ws, articles=[{"type": "article", "slug": "free-estimates", "title": "Free estimates for small dents",
                        "date": "2026-10-20", "status": "published", "url": f"{SITE}/free-estimates/"}])
    never = FX["content"]["wording"]["never"][0]
    fix = FX["content"]["wording"]["always"][never]
    text = report.build(ws, MONTH)["report"]
    assert fix in text and not re.search(never, text, re.I)


def test_a_banned_phrase_without_a_replacement_is_exit_1_naming_the_line(ws, root, capsys):
    _fill(ws, MONTH)
    edit_config(root, "fx", lambda c: c["content"]["wording"]["never"].append(r"\bbargain repairs\b"))
    _log(ws, articles=[{"type": "article", "slug": "bargain", "title": "Bargain repairs this autumn", "date": "2026-10-20",
                        "status": "published", "url": f"{SITE}/bargain/"}])
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", MONTH], keychain=FakeKeychain()) == 1
    err = capsys.readouterr()
    assert re.search(r"line \d+", err.out + err.err) and "bargain repairs" in (err.out + err.err).lower()
    assert not ws.path("data", "reports", f"{MONTH}.md").exists()


def test_cli_writes_the_file_and_json_carries_the_numbers(ws, capsys):
    _full_setup(ws)
    code = cli.main(["report", "monthly", "--workspace", "fx", "--month", MONTH, "--goal", "more calls from Testville",
                     "--json"], keychain=FakeKeychain())
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    path = ws.path("data", "reports", f"{MONTH}.md")
    assert path.is_file() and out["path"] == str(path) and not path.with_name(path.name + ".tmp").exists()
    assert out["search"]["clicks"] == 10 * len(_weeks(MONTH)) and out["ads"]["cost_per_conversion"] > 0
    assert out["goal"] == "more calls from Testville" and "more calls from Testville" in path.read_text()
    assert out["content"]["articles"][0]["measured_28d"]["clicks"] == 17
    assert out["status"] == "ok" and "messages" not in out


def test_skeleton_adds_todo_markers_and_passes_while_a_leftover_todo_fails(ws, capsys):
    _fill(ws, MONTH)
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", MONTH, "--skeleton"], keychain=FakeKeychain()) == 0
    text = ws.path("data", "reports", f"{MONTH}.md").read_text()
    assert text.count("TODO:") >= 4 and "## Summary" in text
    _log(ws, articles=[{"type": "article", "slug": "todo", "title": "TODO: rename me", "date": "2026-10-20",
                        "status": "published", "url": f"{SITE}/todo/"}])
    capsys.readouterr()
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", MONTH], keychain=FakeKeychain()) == 1
    assert "fill the notes" in capsys.readouterr().out


def test_no_week_touching_the_month_is_could_not_run(ws, capsys):
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", MONTH], keychain=FakeKeychain()) == 3
    assert not ws.path("data", "reports").exists()
    _fill(ws, PREV)
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", "2026-12"], keychain=FakeKeychain()) == 3
    assert cli.main(["report", "monthly", "--workspace", "fx", "--month", "2026-13"], keychain=FakeKeychain()) == 3


def test_weeks_of_month_are_the_iso_weeks_touching_it():
    weeks = report.weeks_of_month("2026-10")
    assert weeks[0] == "2026-W40" and weeks[-1] == "2026-W44" and len(weeks) == 5
    assert report.month_period("2026-10") == (dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert report.previous_month("2026-01") == "2025-12"
