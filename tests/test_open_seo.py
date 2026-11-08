"""M2 step 9: Open-SEO through its own free CLI (`openseo-client`), with the project checked before data (D1)."""

import datetime as dt
import json
import subprocess

import pytest

from mseo import workspace
from mseo.adapters import open_seo
from mseo.run import record
from tests.conftest import FX
from tests.fakes import FakeKeychain
from tests.test_export_pull import W44

PROJECT = FX["accounts"]["open_seo"]["id"]
FOREIGN = "9e9e9e9e-aaaa-4bbb-8ccc-ddddeeeeffff"
NAME = "Fx Synthetic Shop"
NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)
ROWS = [{"keyword": "synthetic repair", "position": 4, "previousPosition": 6, "url": "https://fx.test/a",
         "hasLocalPack": True, "mapPackPosition": 2, "mapsPosition": 1},
        {"keyword": "synthetic paint", "position": None, "previousPosition": 12, "url": None, "hasLocalPack": False}]


def listing(*projects):
    return {"projects": [{"id": i, "name": n, "domain": None, "locationCode": 2840, "languageCode": "en"}
                         for i, n in projects]}


def status(name=NAME, checked="2026-11-03T04:15:02.608Z", error=None, rows=ROWS):
    return {"project": name, "runStatus": "completed", "checkedAt": checked, "runError": error, "rows": rows}


class FakeCli:
    def __init__(self, listed=None, stat=None, code=0):
        self.listed = listed if listed is not None else listing((PROJECT, NAME), (FOREIGN, "Other Co"))
        self.stat = stat if stat is not None else status()
        self.code = code
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = self.listed if argv[1] == "list" else self.stat
        return subprocess.CompletedProcess(argv, self.code, json.dumps(out), "boom" if self.code else "")


@pytest.fixture
def ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


def _pull(ws, cli, **kw):
    return open_seo.pull(ws, W44, runner=cli, now=NOW, **kw)


def test_ok_pull_writes_rankings_and_checks_the_project(ws):
    cli = FakeCli()
    code, rec, _ = _pull(ws, cli)
    assert code == 0 and rec["status"] == "ok" and rec["adapter"] == "api"
    assert rec["seen_via"] == "api:project" and rec["account_id_seen"] == PROJECT
    assert rec["identity"] is None and rec["complete"] == {"value": True, "reason": None}
    assert [c[1:3] for c in cli.calls] == [["list", "--json"], ["track", "status"]]
    assert PROJECT in cli.calls[1]
    run = ws.path("data", "runs", rec["run_id"])
    assert [f["path"] for f in rec["files"]] == ["rankings.csv"]
    text = (run / "rankings.csv").read_text()
    assert text.splitlines()[0] == "keyword,position,previous,url,local_pack,map_pack,maps,as_of,maps_as_of"
    assert "synthetic repair,4,6" in text
    assert record.load(run / "record.json")["status"] == "ok"


def test_no_foreign_project_is_stored(ws):
    code, rec, _ = _pull(ws, FakeCli())
    run = ws.path("data", "runs", rec["run_id"])
    blob = json.dumps(rec) + "".join(p.read_text() for p in run.iterdir())
    assert FOREIGN not in blob and "Other Co" not in blob


def test_project_not_listed_is_wrong_account(ws):
    code, rec, _ = _pull(ws, FakeCli(listed=listing((FOREIGN, "Other Co"))))
    assert code == 1 and rec["status"] == "wrong_account" and rec["files"] == []
    assert FOREIGN not in json.dumps(rec)


def test_status_for_another_project_name_is_wrong_account(ws):
    code, rec, _ = _pull(ws, FakeCli(stat=status(name="Other Co")))
    assert rec["status"] == "wrong_account" and rec["files"] == []


def test_two_projects_with_the_same_name_is_unverified(ws):
    code, rec, _ = _pull(ws, FakeCli(listed=listing((PROJECT, NAME), (FOREIGN, NAME))))
    assert rec["status"] == "unverified_account" and rec["files"] == []


def test_stale_rank_check_is_ok_but_incomplete(ws):
    code, rec, _ = _pull(ws, FakeCli(stat=status(checked="2026-10-01T00:00:00Z")))
    assert rec["status"] == "ok" and rec["complete"]["value"] is False
    assert "2026-10-01" in rec["complete"]["reason"]


def test_partial_rank_check_is_ok_but_incomplete(ws):
    code, rec, _ = _pull(ws, FakeCli(stat=status(error="Checked 12 of 15 keyword(s): UPSTREAM_UNAVAILABLE")))
    assert rec["status"] == "ok" and rec["complete"] == {"value": False, "reason": "3 of 15 keywords not checked"}


def test_no_rows_is_empty(ws):
    code, rec, _ = _pull(ws, FakeCli(stat=status(rows=[])))
    assert code == 0 and rec["status"] == "empty"


def test_cli_failure_is_an_error(ws):
    code, rec, _ = _pull(ws, FakeCli(code=2))
    assert code == 1 and rec["status"] == "error" and "boom" in rec["error"]


def test_missing_cli_is_an_error(ws, monkeypatch):
    def gone(argv, **kw):
        raise FileNotFoundError(argv[0])

    code, rec, _ = _pull(ws, gone)
    assert rec["status"] == "error" and "openseo-client" in rec["error"]


def test_duplicate_period_exits_2_unless_forced(ws):
    _pull(ws, FakeCli())
    assert _pull(ws, FakeCli())[0] == 2
    assert _pull(ws, FakeCli(), force=True)[0] == 0


def test_cli_pull_runs_open_seo_without_a_file(ws, monkeypatch):
    from mseo import cli

    monkeypatch.setattr(open_seo, "DEFAULT_RUNNER", FakeCli())
    monkeypatch.setenv("MSEO_TODAY", "2026-11-09")
    assert cli.main(["pull", "--workspace", "fx", "--source", "open_seo", "--week", "2026-W44"],
                    keychain=FakeKeychain()) == 0


def test_map_pack_and_maps_places_are_kept(ws):
    stat = dict(status(), mapsCheckedAt="2026-11-08T05:10:42.625Z")
    code, rec, _ = _pull(ws, FakeCli(stat=stat))
    lines = (ws.path("data", "runs", rec["run_id"]) / "rankings.csv").read_text().splitlines()
    assert lines[1].endswith(",True,2,1,2026-11-03,2026-11-08")
    assert lines[2].endswith(",False,,,2026-11-03,2026-11-08")
