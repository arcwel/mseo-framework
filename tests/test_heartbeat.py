"""Heartbeat ping (audit 31) and the weekly sections of the summary email (plan 3.4 item 5)."""

from __future__ import annotations

import datetime as dt
import json


from mseo import cli, watchdog, workspace
from mseo.fsutil import write_json_atomic
from tests.conftest import FX
from tests.fakes import FakeKeychain
from tests.test_watchdog import FROM, NOW, TO, _healthy, _schedule, env  # noqa: F401 - fixture and helpers

HB_URL = "https://hc.example/ping/0123456789abcdef"
DAY = dt.date(2026, 11, 10)  # a Tuesday: the last complete week is 2026-W45
W = "2026-W45"


def _cfg(env, **extra):
    cfg = env["root"].parent / "alerts.toml"
    cfg.write_text(f'to = "{TO}"\nfrom = "{FROM}"\ntimezone = "{FX["timezone"]}"\n'
                   + "".join(f'{k} = "{v}"\n' for k, v in extra.items()))
    return cfg


class Fetch:
    def __init__(self, code=200, raise_=None):
        self.code, self.raise_, self.calls = code, raise_, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.raise_:
            raise self.raise_
        return self.code


# ------------------------------------------------------------------ heartbeat

def test_heartbeat_gets_the_url_with_a_10s_timeout():
    f = Fetch()
    hb = watchdog.heartbeat({"heartbeat_url": HB_URL}, fetcher=f)
    assert f.calls == [(HB_URL, 10)]
    assert hb == {"status": "ok", "host": "hc.example", "http": 200}


def test_heartbeat_failure_is_logged_never_raised():
    hb = watchdog.heartbeat({"heartbeat_url": HB_URL}, fetcher=Fetch(raise_=OSError("boom")))
    assert hb["status"] == "failed" and "boom" in hb["error"]
    assert HB_URL not in json.dumps(hb)  # the token never appears in the status
    assert watchdog.heartbeat({"heartbeat_url": HB_URL}, fetcher=Fetch(code=500))["status"] == "failed"


def test_heartbeat_without_a_url_is_not_configured():
    assert watchdog.heartbeat({}, fetcher=Fetch())["status"] == "not configured"


def test_run_pings_after_a_successful_run_only(env, monkeypatch):
    _cfg(env, heartbeat_url=HB_URL)
    f = Fetch()
    sent = []
    result = watchdog.run(NOW, sender=sent.append, fetcher=f)
    assert result["heartbeat"]["status"] == "ok" and f.calls == [(HB_URL, 10)]
    f2 = Fetch()
    assert "heartbeat" not in watchdog.run(NOW, sender=sent.append, dry_run=True, fetcher=f2) and f2.calls == []


def test_run_survives_a_failed_ping(env):
    _cfg(env, heartbeat_url=HB_URL)
    result = watchdog.run(NOW, sender=lambda m: None, fetcher=Fetch(raise_=TimeoutError("slow")))
    assert result["heartbeat"]["status"] == "failed"


def test_heartbeat_cli_dry_run_prints_the_host_only(env, capsys):
    _cfg(env, heartbeat_url=HB_URL)
    assert cli.main(["heartbeat", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "hc.example" in out and "0123456789abcdef" not in out
    assert cli.main(["heartbeat", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == HB_URL


def test_heartbeat_cli_without_a_url_is_3(env, capsys):
    assert cli.main(["heartbeat", "--dry-run"]) == 3
    assert "not configured" in capsys.readouterr().out


# ------------------------------------------------------------------ weekly sections

def test_last_complete_week():
    assert watchdog.last_complete_week(DAY) == W
    assert watchdog.last_complete_week(dt.date(2026, 11, 9)) == W  # a Monday still reports the week just ended


def _ws():
    return workspace.open("fx", keychain=FakeKeychain())


def test_summary_without_weekly_files_has_no_sections(env):
    assert watchdog.workspace_sections(_ws(), DAY) == []
    msg = watchdog.compose_summary({"to": TO, "from": FROM}, [], [], DAY)
    assert "week 2026-W45" not in msg.get_content()


def test_summary_sections_from_synthetic_files(env):
    ws = _ws()
    write_json_atomic(ws.path("data", "weeks", f"{W}.shadow.json"), {"status": "pass", "rows": []})
    write_json_atomic(ws.path("data", "weeks", f"{W}.json"), {"sources": {
        "ga4": {"status": "ok", "metrics": {"sessions": 120, "keyEvents": 4, "totalUsers": None}},
        "search_console": {"status": "not available"}}})
    write_json_atomic(ws.path("data", "weeks", f"{W}.ads-audit.json"),
                      {"checks": [{"status": "fail"}, {"status": "warn"}, {"status": "warn"}, {"status": "pass"}]})
    write_json_atomic(ws.path("data", "batches", "1.json"),
                      {"items": [{"status": "proposed"}, {"status": "approved"}, {"status": "proposed"}]})
    write_json_atomic(ws.path("data", "audits", "2026-11-08-site.json"), {"counts": {"fail": 2, "warn": 5, "info": 1}})
    write_json_atomic(ws.path("drafts", "good_article.md.check.json"), {"result": "pass", "slug": "good-article"})
    log_path = ws.file("content_log")
    log = json.loads(log_path.read_text())
    log["articles"].append({"slug": "older-piece", "status": "published", "date": "2026-10-01",
                            "measured_28d": {"updated": "2026-11-08", "clicks": 12}})
    log["articles"].append({"slug": "stale-piece", "status": "published", "date": "2026-09-01",
                            "measured_28d": {"updated": "2026-10-01", "clicks": 3}})
    log_path.write_text(json.dumps(log))
    lines = watchdog.workspace_sections(ws, DAY)
    assert f"shadow verdict {W}: pass" in lines
    assert "  ga4: sessions 120, keyEvents 4" in lines and "  search_console: not available" in lines
    assert f"Ads audit {W}: 1 fail, 2 warn (4 checks)" in lines
    assert "open action batch: 2 item(s) proposed and not approved" in lines
    assert "drafts awaiting approval: good-article" in lines
    assert "measured items (last 7 days): 1" in lines
    assert "site audit 2026-11-08: 2 fail, 5 warn" in lines
    body = watchdog.compose_summary({"to": TO, "from": FROM}, [], [], DAY).get_content()
    assert f"fx, week {W}:" in body and "shadow verdict" in body


def test_damaged_weekly_files_are_tolerated(env):
    ws = _ws()
    for name in (f"{W}.shadow.json", f"{W}.json", f"{W}.ads-audit.json"):
        ws.path("data", "weeks").mkdir(parents=True, exist_ok=True)
        (ws.path("data", "weeks") / name).write_text("{not json")
    (ws.path("data", "batches")).mkdir(parents=True)
    (ws.path("data", "batches") / "x.json").write_text("[1, 2]")
    assert watchdog.workspace_sections(ws, DAY) == []


def test_a_draft_without_a_passing_check_is_not_awaiting_approval(env):
    ws = _ws()
    write_json_atomic(ws.path("drafts", "good_article.md.check.json"), {"result": "fail", "slug": "good-article"})
    assert not any("awaiting" in l for l in watchdog.workspace_sections(ws, DAY))
