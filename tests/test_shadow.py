"""M2 step 12 (build plan A4/A6): `mseo shadow compare`: the week file against the totals each report page showed at
pull time, within tolerances.json; two passing weeks in a row accept M2b."""

import json

import pytest

from mseo import cli, shadow, workspace
from tests.fakes import FakeKeychain
from tests.test_week import W, _all_ok

PAGE = {"search_console": {"clicks": 5, "impressions": 400, "ctr": 0.0125, "position": 17.5},
        "ga4": {"sessions": 12, "engagedSessions": 6, "keyEvents": 1},
        "google_ads": {"cost": 11.75, "clicks": 5, "impressions": 250, "conversions": 1.5, "calls": 2}}


@pytest.fixture
def ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


@pytest.fixture
def week(ws):
    from mseo import week as week_mod

    _all_ok(ws)
    return week_mod.assemble(ws, W)


def _baseline(ws, data):
    path = ws.path("baseline.json")
    path.write_text(json.dumps(data))
    return path


def test_within_tolerance_passes(ws, week):
    out = shadow.compare(ws, W, baseline=PAGE)
    assert out["status"] == "pass", [r for r in out["rows"] if not r["ok"]]
    assert {(r["source"], r["metric"]) for r in out["rows"]} >= {("google_ads", "cost"), ("ga4", "sessions")}


def test_a_metric_off_by_more_than_its_tolerance_fails(ws, week):
    page = json.loads(json.dumps(PAGE))
    page["search_console"]["clicks"] = 6
    out = shadow.compare(ws, W, baseline=page)
    bad = [r for r in out["rows"] if not r["ok"]]
    assert out["status"] == "fail" and [(r["source"], r["metric"]) for r in bad] == [("search_console", "clicks")]


def test_rounding_on_the_page_is_inside_tolerance(ws, week):
    page = json.loads(json.dumps(PAGE))
    page["search_console"]["ctr"] = 0.013  # the page shows 1.3%
    page["search_console"]["position"] = 17.5
    page["google_ads"]["cost"] = 11.75
    assert shadow.compare(ws, W, baseline=page)["status"] == "pass"


def test_a_source_without_a_baseline_is_not_a_pass(ws, week):
    page = {k: v for k, v in PAGE.items() if k != "ga4"}
    out = shadow.compare(ws, W, baseline=page)
    assert out["status"] == "incomplete"
    assert any(r["source"] == "ga4" and r["note"] == "no page totals" for r in out["rows"])


def test_a_source_not_available_in_the_week_fails(ws, root):
    from mseo import week as week_mod

    week_mod.assemble(ws, W)  # no records at all
    out = shadow.compare(ws, W, baseline=PAGE)
    assert out["status"] == "fail" and all(r["note"] == "not available" for r in out["rows"])


def test_the_result_is_kept_and_two_passing_weeks_in_a_row_accept(ws, week):
    shadow.compare(ws, W, baseline=PAGE)
    assert json.loads(ws.path("data", "weeks", f"{W}.shadow.json").read_text())["status"] == "pass"
    prev = ws.path("data", "weeks", "2026-W43.shadow.json")
    prev.write_text(json.dumps({"week": "2026-W43", "status": "pass"}))
    assert shadow.accepted(ws) == ["2026-W43", W]
    prev.write_text(json.dumps({"week": "2026-W43", "status": "fail"}))
    assert shadow.accepted(ws) is None


def test_weeks_must_be_consecutive(ws, week):
    shadow.compare(ws, W, baseline=PAGE)
    ws.path("data", "weeks", "2026-W41.shadow.json").write_text(json.dumps({"week": "2026-W41", "status": "pass"}))
    assert shadow.accepted(ws) is None


def test_cli_exit_codes(ws, week, tmp_path, capsys):
    base = ["shadow", "compare", "--workspace", "fx", "--week", W, "--baseline", str(_baseline(ws, PAGE))]
    assert cli.main(base, keychain=FakeKeychain()) == 0
    page = json.loads(json.dumps(PAGE))
    page["google_ads"]["clicks"] = 99
    base[-1] = str(_baseline(ws, page))
    assert cli.main(base, keychain=FakeKeychain()) == 1
    assert "google_ads" in capsys.readouterr().out


def test_a_baseline_outside_the_workspace_is_refused(ws, week, root):
    outside = root / "fy" / "baseline.json"
    outside.write_text(json.dumps(PAGE))
    assert cli.main(["shadow", "compare", "--workspace", "fx", "--week", W, "--baseline", str(outside)],
                    keychain=FakeKeychain()) == 3


def test_a_metric_the_page_did_not_show_leaves_the_week_incomplete(ws, week):
    page = json.loads(json.dumps(PAGE))
    del page["ga4"]["sessions"]
    out = shadow.compare(ws, W, baseline=page)
    assert out["status"] == "incomplete"
    assert any(r["metric"] == "sessions" and r["note"] == "not on the page" for r in out["rows"])


def test_a_metric_the_page_never_totals_does_not_block_the_week(ws, week):
    page = json.loads(json.dumps(PAGE))
    del page["google_ads"]["calls"], page["google_ads"]["clicks"]
    out = shadow.compare(ws, W, baseline=page)
    assert out["status"] == "pass"
    assert any(r["metric"] == "calls" and r["note"] == "the page shows no total" for r in out["rows"])
