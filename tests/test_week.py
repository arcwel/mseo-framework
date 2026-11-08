"""M2 step 12 (build plan A4): `mseo week assemble` writes data/weeks/<W>.json (mseo.week/1) from ok records only."""

import csv
import datetime as dt
import json

import pytest

from mseo import cli, week, workspace
from mseo.fsutil import sha256_file
from mseo.run import record
from tests.conftest import FX
from tests.fakes import FakeKeychain

W = "2026-W44"
PERIOD = record.week_period(W, FX["timezone"])
NOW = dt.datetime(2026, 11, 7, 16, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


def _csv(path, fields, rows):
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _run(ws, source, status, files: dict, *, period=PERIOD, minute=0, shared=False):
    started = NOW + dt.timedelta(minutes=minute)
    run_id = record.new_run_id(source, now=started)
    folder = ws.path("data", "runs", run_id)
    folder.mkdir(parents=True)
    entries = []
    for name, (fields, rows) in files.items():
        _csv(folder / name, fields, rows)
        entries.append({"path": name, "sha256": sha256_file(folder / name), "rows": len(rows), "truncated": False})
    ok = status == "ok"
    rec = record.build(run_id=run_id, workspace="fx", source=source, adapter="export", identity=FX["identity"]["email"],
                       identity_shared=shared, period=period, account_id_expected=ws.account(source),
                       account_id_seen=ws.account(source) if ok else None, seen_via="page:test_header" if ok else "none",
                       status=status, complete={"value": ok, "reason": None if ok else status}, files=entries if ok else [],
                       started=started, finished=started, error=None if ok else "for the test")
    record.write(folder, rec)
    return run_id


SC_DATES = [{"date": "2026-10-26", "clicks": 2, "impressions": 100, "ctr": 0.02, "position": 10.0},
            {"date": "2026-10-27", "clicks": 3, "impressions": 300, "ctr": 0.01, "position": 20.0}]
GA4 = [{"date": "2026-10-26", "channel": "Organic Search", "sessions": 5, "totalUsers": "", "engagedSessions": 2,
        "keyEvents": 1},
       {"date": "2026-10-27", "channel": "Organic Search", "sessions": 4, "totalUsers": "", "engagedSessions": 1,
        "keyEvents": 0},
       {"date": "2026-10-27", "channel": "Direct", "sessions": 3, "totalUsers": "", "engagedSessions": 3,
        "keyEvents": 0}]
ADS = [{"campaign": "Campaign A", "campaign_type": "Search", "status": "Eligible", "currency": "USD", "cost": 10.5,
        "clicks": 4, "impressions": 200, "conversions": 1.5, "calls": 2},
       {"campaign": "Campaign B", "campaign_type": "Search", "status": "Eligible", "currency": "USD", "cost": 1.25,
        "clicks": 1, "impressions": 50, "conversions": 0, "calls": 0}]


def _all_ok(ws):
    return {"search_console": _run(ws, "search_console", "ok", {"dates.csv": (list(SC_DATES[0]), SC_DATES)}),
            "ga4": _run(ws, "ga4", "ok", {"traffic.csv": (list(GA4[0]), GA4)}),
            "google_ads": _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)})}


def test_metrics_are_week_totals_from_the_normalized_files(ws):
    ids = _all_ok(ws)
    out = week.assemble(ws, W)
    sc, ga, ads = (out["sources"][s] for s in ("search_console", "ga4", "google_ads"))
    assert sc["run_id"] == ids["search_console"] and sc["status"] == "ok" and sc["complete"] is True
    assert sc["metrics"] == {"clicks": 5, "impressions": 400, "ctr": pytest.approx(5 / 400),
                             "position": pytest.approx((10 * 100 + 20 * 300) / 400)}
    assert ga["metrics"]["sessions"] == 12 and ga["metrics"]["engagedSessions"] == 6 and ga["metrics"]["keyEvents"] == 1
    assert ga["metrics"]["totalUsers"] is None  # not in the export, and not additive across days and channels
    assert ga["by_channel"]["Organic Search"]["sessions"] == 9 and ga["by_channel"]["Direct"]["sessions"] == 3
    assert ads["metrics"] == {"cost": pytest.approx(11.75), "clicks": 5, "impressions": 250,
                              "conversions": pytest.approx(1.5), "calls": 2}
    assert ads["by_campaign"]["Campaign A"]["cost"] == pytest.approx(10.5)


def test_the_file_is_written_where_agents_cannot_write(ws):
    _all_ok(ws)
    week.assemble(ws, W)
    data = json.loads(ws.path("data", "weeks", f"{W}.json").read_text())
    assert data["schema_version"] == "mseo.week/1" and data["workspace"] == "fx" and data["week"] == W


def test_a_source_without_an_ok_record_is_not_available_never_zero(ws):
    _run(ws, "ga4", "layout_changed", {})
    out = week.assemble(ws, W)
    ga = out["sources"]["ga4"]
    assert (ga["status"], ga["run_id"], ga["metrics"]) == ("not available", None, {})
    assert "no ok record" in ga["reason"]
    assert out["sources"]["search_console"]["status"] == "not available"


def test_the_latest_ok_record_for_the_week_wins(ws):
    _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS[:1])}, minute=0)
    newer = _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)}, minute=5)
    _run(ws, "google_ads", "error", {}, minute=9)
    assert week.assemble(ws, W)["sources"]["google_ads"]["run_id"] == newer


def test_a_record_for_another_week_is_ignored(ws):
    _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)}, period=record.week_period("2026-W43", FX["timezone"]))
    assert week.assemble(ws, W)["sources"]["google_ads"]["status"] == "not available"


def test_incomplete_and_shared_are_carried(ws):
    rid = _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)}, shared=True)
    path = ws.path("data", "runs", rid, "record.json")
    rec = json.loads(path.read_text())
    rec["complete"] = {"value": False, "reason": "within reporting lag"}
    path.write_text(json.dumps(rec))
    ads = week.assemble(ws, W)["sources"]["google_ads"]
    assert ads["complete"] is False and ads["identity_shared"] is True


def test_a_changed_file_is_refused(ws):
    rid = _run(ws, "google_ads", "ok", {"campaigns.csv": (list(ADS[0]), ADS)})
    with ws.path("data", "runs", rid, "campaigns.csv").open("a") as fh:
        fh.write("Campaign C,Search,Eligible,USD,999,1,1,0,0\n")
    ads = week.assemble(ws, W)["sources"]["google_ads"]
    assert ads["status"] == "not available" and "sha256" in ads["reason"]


def test_cli_week_assemble(ws, capsys):
    _all_ok(ws)
    assert cli.main(["week", "assemble", "--workspace", "fx", "--week", W, "--json"], keychain=FakeKeychain()) == 0
    assert json.loads(capsys.readouterr().out)["sources"]["google_ads"]["status"] == "ok"
