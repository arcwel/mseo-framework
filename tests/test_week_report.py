"""`mseo week report`: one markdown page from whatever the week's commands wrote; gaps are named, never zero."""

import datetime as dt
import json

from mseo import cli, week, week_report, workspace
from tests.fakes import FakeKeychain
from tests.test_week import GA4, SC_DATES, _run

W = "2026-W44"
TODAY = dt.date(2026, 11, 7)


def _ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


def test_report_names_every_gap_and_every_file_it_read(root):
    ws = _ws(root)
    _run(ws, "search_console", "ok", {"dates.csv": (["date", "clicks", "impressions", "ctr", "position"], SC_DATES)})
    _run(ws, "ga4", "ok", {"traffic.csv": (["date", "channel", "sessions", "totalUsers", "engagedSessions", "keyEvents"], GA4)})
    week.assemble(ws, W)
    weeks = ws.path("data", "weeks")
    (weeks / f"{W}.shadow.json").write_text(json.dumps({"status": "fail", "rows": [
        {"source": "ga4", "metric": "sessions", "ours": 12, "page": 13, "ok": False, "note": "exact"}]}))
    (weeks / f"{W}.ads-audit.json").write_text(json.dumps({"checks": [
        {"name": "wasted_spend", "status": "fail", "detail": "2 terms"}, {"name": "call_asset", "status": "pass", "detail": ""}]}))
    text = week_report.report(ws, W, today=TODAY)
    assert "| search_console | ok |" in text and "| ga4 | ok |" in text
    assert "| google_ads | not available |" in text and " 0 |" not in text.split("## Sources")[1].split("## Shadow")[0]
    assert "result: **fail**" in text and "ga4 sessions: ours 12, page 13" in text
    assert "tracking gate: not run" in text and "audit: 1 fail, 0 warn, 1 pass" in text
    assert "none (mseo actions propose)" in text and "not run (mseo content topics)" in text
    assert "awaiting approval:" in text and "article good-article: check not checked" in text
    assert "not run (mseo site audit)" in text


def test_cli_writes_the_report_file(root, capsys):
    ws = _ws(root)
    _run(ws, "ga4", "ok", {"traffic.csv": (["date", "channel", "sessions", "totalUsers", "engagedSessions", "keyEvents"], GA4)})
    week.assemble(ws, W)
    assert cli.main(["week", "report", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == 0
    out = ws.path("data", "weeks", f"{W}.report.md")
    assert out.is_file() and out.read_text().startswith("# ")
    assert "week 2026-W44" in capsys.readouterr().out


def test_no_week_file_is_could_not_run(root, capsys):
    assert cli.main(["week", "report", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == 3
