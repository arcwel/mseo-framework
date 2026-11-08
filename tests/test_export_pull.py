"""M2 step 4: export adapter core (inbox pickup, move and hash, Search Console zip, GA4 CSV) and `mseo pull --file`."""

import csv
import io
import os
import time
import zipfile

import pytest

from mseo import cli, workspace
from mseo.adapters import export, ga4, search_console
from mseo.run import record
from tests.fakes import FakeKeychain

W44 = {"start": "2026-10-26", "end": "2026-11-01", "tz": "America/Los_Angeles"}
DAYS = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]


def sc_zip(path, days=DAYS, queries=5, dates_member="Dates.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(dates_member, "Date,Clicks,Impressions,CTR,Position\n" +
                   "".join(f"{d},{3 + i},{100 + i},{(3 + i) / (100 + i) * 100:.2f}%,{8.5 + i / 10}\n"
                           for i, d in enumerate(days)))
        z.writestr("Queries.csv", "Top queries,Clicks,Impressions,CTR,Position\n" +
                   "".join(f"query {n},{n},{n * 10},10%,{n}\n" for n in range(queries)))
        z.writestr("Pages.csv", "Top pages,Clicks,Impressions,CTR,Position\nhttps://a.test/,4,40,10%,3\n")
        z.writestr("Filters.csv", "Filter,Value\nSearch type,Web\n")
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def fx(root):
    return workspace.open("fx", keychain=FakeKeychain())


# ------------------------------------------------------------------ inbox pickup

def test_pick_up_takes_exactly_one_new_file(fx):
    inbox = fx.path("data", "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    old = inbox / "old.csv"
    old.write_text("x")
    os.utime(old, (1, 1))
    since = time.time() - 1
    (inbox / "new.csv").write_text("y")
    (inbox / "half.csv.crdownload").write_text("z")
    assert export.pick_up(fx, since).name == "new.csv"
    (inbox / "second.csv").write_text("w")
    with pytest.raises(export.ExportError, match="2 new files"):
        export.pick_up(fx, since)


def test_pick_up_with_nothing_new_is_an_error(fx):
    fx.path("data", "inbox").mkdir(parents=True, exist_ok=True)
    with pytest.raises(export.ExportError, match="no export"):
        export.pick_up(fx, time.time())


def test_ingest_refuses_a_file_outside_the_workspace(fx, root, tmp_path):
    outside = tmp_path / "elsewhere.csv"
    outside.write_text("x")
    with pytest.raises(workspace.WorkspaceError):
        export.ingest(fx, outside, fx.path("data", "runs", "r"))
    sibling = root / "fy" / "exports" / "ga4-traffic-2026-W44.csv"
    with pytest.raises(workspace.WorkspaceError):
        export.ingest(fx, sibling, fx.path("data", "runs", "r"))
    assert sibling.exists()


def test_ingest_moves_and_hashes(fx):
    src = fx.path("exports", "ga4-traffic-2026-W44.csv")
    body = src.read_bytes()
    entry = export.ingest(fx, src, fx.path("data", "runs", "r1"))
    assert not src.exists()
    assert (fx.path("data", "runs", "r1") / entry["path"]).read_bytes() == body
    assert len(entry["sha256"]) == 64


# ------------------------------------------------------------------ parsers

def test_search_console_zip_tables_and_truncation(tmp_path):
    parsed = search_console.parse_zip(sc_zip(tmp_path / "e.zip", queries=1000))
    assert [r["date"] for r in parsed["tables"]["dates"]] == DAYS
    assert parsed["tables"]["dates"][0] == {"date": "2026-10-26", "clicks": 3, "impressions": 100, "ctr": 0.03,
                                            "position": 8.5}
    assert parsed["truncated"] == {"dates": False, "queries": True, "pages": False}


def test_search_console_zip_without_dates_is_refused(tmp_path):
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Queries.csv", "Top queries,Clicks\n")
    with pytest.raises(export.ExportError, match="Dates.csv"):
        search_console.parse_zip(p)


def test_ga4_csv_reads_dates_and_rows(fx):
    parsed = ga4.parse_csv(fx.path("exports", "ga4-traffic-2026-W44.csv"))
    assert (parsed["start"], parsed["end"]) == ("2026-10-26", "2026-11-01")
    first = parsed["rows"][0]
    assert first == {"date": "2026-10-26", "channel": "Organic Search", "sessions": 20, "engagedSessions": 17,
                     "keyEvents": 1, "totalUsers": 19}


def test_ga4_csv_with_unknown_columns_is_layout_changed(tmp_path):
    p = tmp_path / "g.csv"
    p.write_text("# Start date: 20261026\n# End date: 20261101\n\nDay,Visits\n20261026,3\n")
    with pytest.raises(export.LayoutChanged):
        ga4.parse_csv(p)


# ------------------------------------------------------------------ mseo pull --file

def pull(*argv):
    return cli.main(["pull", "--workspace", "fx", *argv, "--json"], keychain=FakeKeychain())


def runs(fx, source):
    out = []
    for p in sorted(fx.path("data", "runs").glob("*/record.json")):
        rec = record.load(p)
        if rec["source"] == source:
            out.append((p.parent, rec))
    return out


def test_manual_ga4_export_is_recorded_unverified_with_files(fx, monkeypatch, capsys):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    code = pull("--source", "ga4", "--week", "2026-W44", "--file", "exports/ga4-traffic-2026-W44.csv")
    assert code == 0, capsys.readouterr()
    [(run_dir, rec)] = runs(fx, "ga4")
    assert rec["status"] == "unverified_account" and rec["seen_via"] == "operator"
    assert rec["adapter"] == "export" and rec["identity"] == fx.config["identity"]["email"]
    assert rec["period"] == W44 and rec["complete"]["value"] is False
    names = {f["path"] for f in rec["files"]}
    assert {"export.csv", "traffic.csv"} <= names
    rows = list(csv.DictReader((run_dir / "traffic.csv").open()))
    assert rows[0]["channel"] == "Organic Search" and len(rows) == 14


def test_export_for_another_period_saves_nothing(fx, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    code = pull("--source", "ga4", "--week", "2026-W43", "--file", "exports/ga4-traffic-2026-W44.csv")
    assert code == 1
    [(run_dir, rec)] = runs(fx, "ga4")
    assert rec["status"] == "error" and "2026-10-26" in rec["error"] and rec["files"] == []
    assert not any(p.suffix == ".csv" for p in run_dir.iterdir())


def test_manual_search_console_zip(fx, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    sc_zip(fx.path("exports") / "sc.zip")
    assert pull("--source", "search_console", "--week", "2026-W44", "--file", "exports/sc.zip") == 0
    [(run_dir, rec)] = runs(fx, "search_console")
    files = {f["path"]: f for f in rec["files"]}
    assert files["dates.csv"]["rows"] == 7 and files["queries.csv"]["truncated"] is False


def test_same_week_again_exits_2_unless_forced(fx, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    src = fx.path("exports", "ga4-traffic-2026-W44.csv")
    body = src.read_bytes()
    assert pull("--source", "ga4", "--week", "2026-W44", "--file", "exports/ga4-traffic-2026-W44.csv") == 0
    src.write_bytes(body)
    assert pull("--source", "ga4", "--week", "2026-W44", "--file", "exports/ga4-traffic-2026-W44.csv") == 2
    assert src.exists()
    assert pull("--source", "ga4", "--week", "2026-W44", "--file", "exports/ga4-traffic-2026-W44.csv", "--force") == 0
    assert len(runs(fx, "ga4")) == 2


def test_unknown_or_disabled_source_could_not_run(fx):
    assert pull("--source", "open_seo", "--file", "exports/ga4-traffic-2026-W44.csv") == 3


def test_agent_pull_is_not_built_yet(fx):
    assert pull("--source", "ga4", "--week", "2026-W44") == 3


def test_lag_days():
    assert search_console.LAG_DAYS == 3 and ga4.LAG_DAYS == 2


def test_default_period_is_last_complete_week(fx, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-04")
    assert export.resolve_period(fx, week=None, start=None, end=None) == W44


def test_search_console_names_the_daily_table_chart_csv(tmp_path):
    """The live export (2026-11-07) holds Chart.csv, not Dates.csv, for the daily table."""
    from mseo.adapters import search_console

    parsed = search_console.parse_zip(sc_zip(tmp_path / "live.zip", dates_member="Chart.csv"))
    assert parsed["dates"] == sorted(DAYS)


def test_ga4_live_columns_without_users_parse_with_users_empty(tmp_path):
    """The live Traffic acquisition export (2026-11-07) has no users column; users are not additive anyway."""
    p = tmp_path / "live.csv"
    p.write_text("# Start date: 20261026\n# End date: 20261101\n\n"
                 "Session primary channel group (Default Channel Group),Date,Sessions,Engaged sessions,Engagement rate,"
                 "Average engagement time per session,Events per session,Event count,Key events,Session key event rate,"
                 "Total revenue\nOrganic Search,20261026,20,17,0.85,61.2,5.1,102,1,0.05,0\n")
    row = ga4.parse_csv(p)["rows"][0]
    assert row["sessions"] == 20 and row["engagedSessions"] == 17 and row["keyEvents"] == 1
    assert row["totalUsers"] is None


# ------------------------------------------------------------------ Sonnet pentest 2026-11-07, lows 7 and 8

def test_a_symlink_in_the_inbox_is_never_picked_up_or_ingested(fx):
    inbox = fx.path("data", "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    target = fx.path("customer.json")
    before = target.read_bytes()
    since = time.time() - 1
    os.utime(target)
    link = inbox / "Export.csv"
    link.symlink_to(target)
    for pick in (lambda: export.pick_up(fx, since), lambda: export.pick_up(fx, since, set())):
        with pytest.raises(export.ExportError, match="no export"):
            pick()
    with pytest.raises(export.ExportError, match="symlink"):
        export.ingest(fx, link, fx.path("data", "runs", "r"))
    assert target.read_bytes() == before


def test_ingest_can_take_a_fixed_name_and_never_overwrites(fx):
    run = fx.path("data", "runs", "r2")
    src = fx.path("exports", "ga4-traffic-2026-W44.csv")
    entry = export.ingest(fx, src, run, name="export.csv")
    assert entry["path"] == "export.csv" and (run / "export.csv").is_file()
    other = fx.path("data", "inbox", "export.csv")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("second")
    with pytest.raises(export.ExportError, match="already"):
        export.ingest(fx, other, run, name="export.csv")
    assert other.exists()
