"""`mseo content topics`: the query-gap shortlist from the week's ok Search Console and Open-SEO records, brand and
covered topics dropped, questions first, a local tie-in attached, `--pick` logged with a recipe. Also the run-record
helper that test_loop.py reuses."""

from __future__ import annotations

import csv
import datetime as dt
import json

import pytest

from mseo import cli, workspace
from mseo.content import recipes, topics
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import sha256_file
from mseo.run import record
from tests.conftest import FX

BRAND = FX["business"]["name"].lower()
from tests.fakes import FakeKeychain

W = "2026-W45"
TODAY = "2026-11-07"
NOW = dt.datetime(2026, 11, 8, 16, 0, tzinfo=dt.timezone.utc)
SC_FIELDS = ["query", "clicks", "impressions", "ctr", "position"]
PAGE_FIELDS = ["page", "clicks", "impressions", "ctr", "position"]
RANK_FIELDS = ["keyword", "position", "previous", "url", "local_pack", "map_pack", "maps", "as_of", "maps_as_of"]
CITY = FX["locations"][0]["address"].split(",")[1].strip()


def make_run(ws, source: str, files: dict, *, week: str = W, status: str = "ok", minute: int = 0) -> str:
    """Write data/runs/<id>/record.json plus its CSV files; `files` is {name: (fields, rows)}."""
    started = NOW + dt.timedelta(minutes=minute)
    run_id = record.new_run_id(source, now=started)
    folder = ws.path("data", "runs", run_id)
    folder.mkdir(parents=True)
    entries = []
    for name, (fields, rows) in files.items():
        with (folder / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        entries.append({"path": name, "sha256": sha256_file(folder / name), "rows": len(rows), "truncated": False})
    ok = status == "ok"
    acct = ws.config["accounts"].get(source, {}).get("id", "acct")
    rec = record.build(run_id=run_id, workspace=ws.slug, source=source, adapter="export", identity=ws.config["identity"]["email"],
                       period=record.week_period(week, ws.config["timezone"]), account_id_expected=acct,
                       account_id_seen=acct if ok else None, seen_via="page:test_header" if ok else "none", status=status,
                       complete={"value": ok, "reason": None if ok else status}, files=entries if ok else [],
                       started=started, finished=started, error=None if ok else "for the test")
    record.write(folder, rec)
    return run_id


QUERIES = [
    {"query": "how long does a bumper repair take", "clicks": 3, "impressions": 120, "ctr": 0.025, "position": 8.1},
    {"query": "body shop", "clicks": 1, "impressions": 400, "ctr": 0.0025, "position": 14.0},
    {"query": f"paintless dent removal {CITY.lower()}", "clicks": 2, "impressions": 90, "ctr": 0.02, "position": 6.0},
    {"query": f"{BRAND} hours", "clicks": 9, "impressions": 50, "ctr": 0.18, "position": 1.2},
    {"query": f"where to get your car fixed {CITY.lower()}", "clicks": 4, "impressions": 80, "ctr": 0.05, "position": 4.0},
    {"query": "can a scratch be buffed out", "clicks": 0, "impressions": 60, "ctr": 0, "position": 11.0},
]
RANKS = [{"keyword": "can a scratch be buffed out", "position": 9, "previous": 12, "url": "", "local_pack": "", "map_pack": "",
          "maps": "", "as_of": "2026-11-06", "maps_as_of": ""},
         {"keyword": "frame straightening cost", "position": 15, "previous": "", "url": "", "local_pack": "", "map_pack": "",
          "maps": "", "as_of": "2026-11-06", "maps_as_of": ""}]


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    ws = workspace.open("fx", keychain=FakeKeychain())
    return ws


def with_records(ws):
    sc = make_run(ws, "search_console", {"queries.csv": (SC_FIELDS, QUERIES)})
    os_ = make_run(ws, "open_seo", {"rankings.csv": (RANK_FIELDS, RANKS)}, minute=5)
    return sc, os_


def run(*argv):
    return cli.main(list(argv), keychain=FakeKeychain())


def test_shortlist_drops_brand_and_covered_queries_and_ranks_questions_first(fx):
    sc, os_ = with_records(fx)
    out = topics.shortlist(fx, W, on=dt.date.fromisoformat(TODAY))
    queries = [t["query"] for t in out["topics"]]
    assert f"{BRAND} hours" not in queries  # business.brand_terms
    assert f"where to get your car fixed {CITY.lower()}" not in queries  # covered by the logged article
    why = {d["query"]: d["why"] for d in out["dropped"]}
    assert why[f"{BRAND} hours"] == "brand term" and "covered by" in why[f"where to get your car fixed {CITY.lower()}"]
    assert queries[:2] == ["how long does a bumper repair take", "can a scratch be buffed out"]  # questions, by impressions
    assert queries[-1] == "body shop" and out["topics"][-1]["class"] == 2
    assert out["records"] == {"search_console": sc, "open_seo": os_}
    merged = next(t for t in out["topics"] if t["query"] == "can a scratch be buffed out")
    assert merged["rank"] == 9 and merged["impressions"] == 60 and sorted(merged["sources"]) == ["open_seo", "search_console"]
    assert any(t["query"] == "frame straightening cost" for t in out["topics"])  # Open-SEO only


def test_tie_in_is_attached_only_when_the_query_names_no_place(fx):
    with_records(fx)
    out = topics.shortlist(fx, W, on=dt.date.fromisoformat(TODAY))
    by = {t["query"]: t for t in out["topics"]}
    research = fx.path("research", "local", f"{W}.md").read_text()
    first_event = topics.tie_in_line(research)
    assert first_event and "Harvest Fair" in first_event
    assert by["how long does a bumper repair take"]["tie_in"] == first_event
    assert by[f"paintless dent removal {CITY.lower()}"]["tie_in"] is None and by[f"paintless dent removal {CITY.lower()}"]["place_named"]


def test_titles_are_questions(fx):
    assert topics.as_question("how long does a bumper repair take") == "How long does a bumper repair take?"
    assert topics.as_question("frame straightening cost") == "What should you know about frame straightening cost?"


def test_command_writes_the_week_file_and_prints_a_numbered_list(fx, capsys):
    with_records(fx)
    assert run("content", "topics", "--workspace", "fx", "--week", W, "--top", "3") == EXIT_OK
    out = capsys.readouterr().out
    assert "  1. [question] how long does a bumper repair take" in out and "  4." not in out
    data = json.loads(fx.path("data", "weeks", f"{W}.topics.json").read_text())
    assert data["schema_version"] == topics.SCHEMA_VERSION and data["week"] == W and len(data["topics"]) == 5


def test_pick_logs_a_draft_entry_with_the_recipe(fx, capsys):
    with_records(fx)
    assert run("content", "topics", "--workspace", "fx", "--week", W, "--pick", "1", "--slug", "bumper-time", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    log = json.loads(fx.file("content_log").read_text())
    entry = next(a for a in log["articles"] if a["slug"] == "bumper-time")
    assert entry["title"] == "How long does a bumper repair take?" and entry["query"] == "how long does a bumper repair take"
    assert entry["status"] == "draft" and entry["date"] == TODAY and entry["tie_in"]
    assert entry["shape"] == out["picked"]["recipe"]["shape"] and entry["blocks"] == recipes.shapes()[entry["shape"]]
    for axis in recipes.ARTICLE_AXES:
        assert entry[axis] == out["picked"]["recipe"][axis]


def test_pick_off_the_list_or_without_slug_is_3(fx):
    with_records(fx)
    assert run("content", "topics", "--workspace", "fx", "--week", W, "--pick", "9", "--slug", "x") == EXIT_COULD_NOT_RUN
    assert run("content", "topics", "--workspace", "fx", "--week", W, "--pick", "1") == EXIT_COULD_NOT_RUN


def test_no_ok_record_for_the_week_is_3(fx, capsys):
    make_run(fx, "search_console", {"queries.csv": (SC_FIELDS, QUERIES)}, status="error")
    make_run(fx, "search_console", {"queries.csv": (SC_FIELDS, QUERIES)}, week="2026-W44", minute=3)
    assert run("content", "topics", "--workspace", "fx", "--week", W) == EXIT_COULD_NOT_RUN
    assert W in capsys.readouterr().err


def test_only_the_latest_ok_record_counts(fx):
    make_run(fx, "search_console", {"queries.csv": (SC_FIELDS, QUERIES[:1])})
    make_run(fx, "search_console", {"queries.csv": (SC_FIELDS, QUERIES[-1:])}, minute=9)
    out = topics.shortlist(fx, W, on=dt.date.fromisoformat(TODAY))
    assert [t["query"] for t in out["topics"]] == ["can a scratch be buffed out"]


def test_covered_uses_wp_posts_too(fx):
    make_run(fx, "search_console", {"queries.csv": (SC_FIELDS, [{"query": "how often should you rotate your tires", "clicks": 1,
                                                                 "impressions": 10, "ctr": 0.1, "position": 3}])})
    out = topics.shortlist(fx, W, on=dt.date.fromisoformat(TODAY))
    assert out["topics"] == [] and "covered by" in out["dropped"][0]["why"]
