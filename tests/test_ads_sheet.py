"""Plan v2 3.1 item 3: Google Ads through the Scripts Sheet. The adapter parses the tabs the Ads Script writes (four
header lines, then the GAQL column header), checks the customer ID and period from the header, attests each download
run by the sheet ID in every docs.google.com URL the agent navigated to, and writes one normalized CSV per table. The
pull has two phases: the `_manifest` tab on gid 0, then every table by the gid the manifest gave."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from mseo import seal, week, workspace
from mseo.adapters import browser_pull, export, google_ads_sheet as sheet
from mseo.fsutil import sha256_file
from mseo.run import record
from tests.conftest import FX, FY, SYNTHETIC, edit_config
from tests.fake_agent import ACCOUNT_PAGE, FakeRunner
from tests.fakes import FakeKeychain

W = "2026-W44"
W44 = {"start": "2026-10-26", "end": "2026-11-01", "tz": FX["timezone"]}
NOW = dt.datetime(2026, 11, 9, 14, 0, tzinfo=dt.timezone.utc)
SHEET_ID = "1SyntheticSheetId_fx_0000000000000000000000"
OTHER_SHEET = "1SyntheticSheetId_other_000000000000000000000"
ADS = FX["accounts"]["google_ads"]["id"]
SIGN_IN = {"snapshot-account.txt": ACCOUNT_PAGE.format(name="Agent", email=FX["identity"]["email"])}
FIXTURES = {slug: SYNTHETIC / slug / "exports" / "ads-sheet" for slug in ("fx", "fy")}


def tab_bytes(slug: str, name: str) -> bytes:
    return (FIXTURES[slug] / f"{name}.csv").read_bytes()


PHASE_TWO = [*sheet.TABLES, sheet.APPLIED_LOG, sheet.ERRORS]  # what the fixture manifest lists besides itself


def all_tabs(slug: str = "fx", prefix: str = "", names=PHASE_TWO) -> dict[str, bytes]:
    """Every tab of phase two as Chrome would name the download (the prefix stands for Chrome's own naming)."""
    return {f"{prefix}{name}.csv": tab_bytes(slug, name) for name in names}


def manifest(slug: str = "fx", prefix: str = "") -> dict[str, bytes]:
    return {f"{prefix}_manifest.csv": tab_bytes(slug, sheet.MANIFEST)}


def gids(slug: str = "fx") -> dict[str, int]:
    return sheet.table_gids(sheet.parse_tab(FIXTURES[slug] / "_manifest.csv")["rows"])


def tab_urls(sheet_id: str = SHEET_ID, slug: str = "fx") -> list[str]:
    return [sheet.tab_url(sheet_id, g) for g in gids(slug).values()]


def manifest_url(sheet_id: str = SHEET_ID) -> list[str]:
    return [sheet.tab_url(sheet_id, sheet.MANIFEST_GID)]


def two_phase(slug: str = "fx", *, sheet_id: str = SHEET_ID, tabs: dict | None = None, manifest_files: dict | None = None):
    """The runner of a full pull: sign-in, the manifest run, the tables run, each attested by its own URLs."""
    return Seq(SIGN_IN, (manifest_files or manifest(slug), manifest_url(sheet_id)),
               (tabs if tabs is not None else all_tabs(slug), tab_urls(sheet_id, slug)))


def with_sheet(root, slug="fx", sheet_id=SHEET_ID):
    edit_config(root, slug, lambda c: c["accounts"]["google_ads"].__setitem__("scripts_sheet_id", sheet_id))


class Seq:
    """One FakeRunner per agent run: each run is a files dict, or (files, urls) when that run must name its URLs;
    `urls=` names the last run's URLs."""

    def __init__(self, *runs, urls=None):
        self.runs = list(runs)
        self.urls = urls
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        run = self.runs.pop(0) if self.runs else {}
        files, urls = run if isinstance(run, tuple) else (run, None)
        if urls is None:
            urls = self.urls if (self.urls is not None and not self.runs) else None  # the export run is the last one
        return FakeRunner(files, urls=urls)(argv, **kw)


def seed_tables(ws, slug: str = "fx", *, period=W44, minute: int = 0, status: str = "ok") -> str:
    """A run record holding the normalized tables, as a sheet pull leaves them (no agent involved)."""
    started = NOW + dt.timedelta(minutes=minute)
    run_id = record.new_run_id(sheet.SOURCE, now=started)
    run_dir = ws.path("data", "runs", run_id)
    run_dir.mkdir(parents=True)
    parsed = sheet.parse_all([FIXTURES[slug] / f"{n}.csv" for n in PHASE_TWO])
    files = sheet.write(parsed, run_dir)
    acct = ws.account(sheet.SOURCE)
    rec = record.build(run_id=run_id, workspace=ws.slug, source=sheet.SOURCE, adapter="export",
                       identity=ws.config["identity"]["email"], period=period, account_id_expected=acct,
                       account_id_seen=acct, seen_via=f"sheet:{SHEET_ID}", status=status,
                       complete={"value": True, "reason": None}, files=files, started=started, finished=started)
    record.write(run_dir, rec)
    return run_id


@pytest.fixture
def ws(root, tmp_path, monkeypatch):
    with_sheet(root)
    state = tmp_path / "Local State"
    state.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": FX["identity"]["chrome_profile"]}}}}))
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []},
              pull_test=lambda *_: {"status": "pass", "rows": []}, sync=None, chrome_state=state)
    monkeypatch.setattr("mseo.doctor.today", lambda: dt.date(2026, 11, 9))
    return w


# ------------------------------------------------------------------ parsing one tab

def test_parse_tab_reads_the_header_lines_and_converts_micros():
    parsed = sheet.parse_tab(FIXTURES["fx"] / "campaigns.csv")
    assert parsed["table"] == "campaigns" and parsed["customer_id"] == ADS
    assert (parsed["start"], parsed["end"]) == (W44["start"], W44["end"])
    row = parsed["rows"][0]
    assert row["campaign"] and row["cost"] == 182.35 and row["budget"] == 25.0 and row["clicks"] == 61
    assert row["calls"] == 9 and row["lost_is_budget"] == 0.3105 and row["conversions"] == 6.5


def test_parse_tab_identifies_the_table_by_its_columns_not_its_name(tmp_path):
    p = tmp_path / "Download (3).csv"
    p.write_bytes(tab_bytes("fx", "keywords"))
    parsed = sheet.parse_tab(p)
    assert parsed["table"] == "keywords"
    assert parsed["rows"][0]["average_cpc"] == pytest.approx(3.18, abs=0.01) and parsed["rows"][0]["quality_score"] == 8


def test_unknown_first_line_is_layout_changed(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("campaign.name,metrics.cost_micros\nA,1\n")
    with pytest.raises(export.LayoutChanged):
        sheet.parse_tab(p)


def test_renamed_column_is_layout_changed(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(tab_bytes("fx", "campaigns").decode().replace("metrics.cost_micros", "metrics.spend_micros"))
    with pytest.raises(export.LayoutChanged, match="no table"):
        sheet.parse_tab(p)


def test_parse_all_needs_every_table_once(tmp_path):
    paths = [FIXTURES["fx"] / f"{n}.csv" for n in sheet.TABLES]
    assert set(sheet.parse_all(paths)) == set(sheet.TABLES)
    with pytest.raises(export.ExportError, match="missing"):
        sheet.parse_all(paths[:-1])
    with pytest.raises(export.ExportError, match="twice"):
        sheet.parse_all(paths + [paths[0]])
    # the optional tabs come along when they arrived and are never required
    both = sheet.parse_all([FIXTURES["fy"] / f"{n}.csv" for n in PHASE_TWO])
    assert set(both) == set(PHASE_TWO) and both[sheet.ERRORS]["rows"][0]["table"] == "change_history"
    assert both[sheet.APPLIED_LOG]["rows"][0]["n"] == 1 and both[sheet.APPLIED_LOG]["rows"][0]["result"].startswith("ok:")


def test_manifest_gives_every_table_a_gid():
    g = gids("fx")
    assert set(g) == set(PHASE_TWO) and len(set(g.values())) == len(g) and all(isinstance(v, int) for v in g.values())
    rows = sheet.parse_tab(FIXTURES["fx"] / "_manifest.csv")["rows"]
    with pytest.raises(export.ExportError, match="no gid for: campaigns"):
        sheet.table_gids([r for r in rows if r["table"] != "campaigns"])
    with pytest.raises(export.LayoutChanged, match="twice"):
        sheet.table_gids(rows + [rows[0]])
    with pytest.raises(export.LayoutChanged, match="share a gid"):
        sheet.table_gids([{**r, "gid": 7} for r in rows])


def test_parse_manifest_wants_exactly_the_manifest(tmp_path):
    with pytest.raises(export.ExportError, match="not the manifest"):
        sheet.parse_manifest([FIXTURES["fx"] / "campaigns.csv"])
    with pytest.raises(export.ExportError, match="expected one"):
        sheet.parse_manifest([FIXTURES["fx"] / "_manifest.csv", FIXTURES["fx"] / "campaigns.csv"])
    assert sheet.parse_manifest([FIXTURES["fx"] / "_manifest.csv"])["table"] == sheet.MANIFEST


def test_a_keyword_starting_with_a_formula_character_survives_as_text(tmp_path):
    """The script writes every cell as text (setNumberFormat("@")), so `+crown`, `=x`, `-y`, `@z` come back verbatim."""
    p = tmp_path / "search_terms.csv"
    text = tab_bytes("fx", "search_terms").decode()
    text = text.replace('"free bumper repair kit"', '"+crown repair"').replace('"paint shop jobs"', '"=paint shop jobs"')
    p.write_text(text)
    terms = [r["search_term"] for r in sheet.parse_tab(p)["rows"]]
    assert "+crown repair" in terms and "=paint shop jobs" in terms


HUGE_FIELD = b'"' + b"x" * 140_000 + b'"'  # over csv.field_size_limit (128 KB): csv.Error, not a crash


def test_a_broken_csv_is_layout_changed_not_a_crash(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(tab_bytes("fx", "keywords").replace(b'"bumper repair"', HUGE_FIELD))
    with pytest.raises(export.LayoutChanged, match="not a CSV"):
        sheet.parse_tab(p)


def test_tables_from_two_accounts_are_refused(tmp_path):
    paths = [FIXTURES["fx"] / f"{n}.csv" for n in sheet.TABLES if n != "budgets"] + [FIXTURES["fy"] / "budgets.csv"]
    with pytest.raises(export.ExportError, match="customer"):
        sheet.parse_all(paths)


def test_fy_fixture_carries_its_own_account():
    assert sheet.parse_tab(FIXTURES["fy"] / "campaigns.csv")["customer_id"] == FY["accounts"]["google_ads"]["id"]


# ------------------------------------------------------------------ attestation from the tool call stream

def _nav(url):
    return {"name": "mcp__claude-in-chrome__navigate", "input": {"url": url}, "text": "Navigated"}


def test_attest_accepts_only_the_configured_sheet():
    assert sheet.attest_sheet([_nav(u) for u in tab_urls()], SHEET_ID) is None
    assert sheet.attest_sheet([_nav(u) for u in manifest_url()], SHEET_ID) is None
    assert sheet.attest_sheet([_nav(u) for u in tab_urls(OTHER_SHEET)], SHEET_ID) == "other_sheet"
    mixed = [_nav(u) for u in tab_urls()] + [_nav(sheet.tab_url(OTHER_SHEET, 0))]
    assert sheet.attest_sheet(mixed, SHEET_ID) == "other_sheet"
    assert sheet.attest_sheet([_nav("https://example.test/campaigns.csv")], SHEET_ID) == "off_site"
    assert sheet.attest_sheet([], SHEET_ID) == "no_navigation"


def test_tab_url_is_the_csv_export_by_gid_not_gviz():
    url = sheet.tab_url(SHEET_ID, 1004)
    assert url == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=1004"
    assert "gviz" not in url and "tq" not in url.split("/")[-1].split("?")[0]
    assert sheet.tab_url(SHEET_ID, sheet.MANIFEST_GID).endswith("gid=0")


# ------------------------------------------------------------------ the pull

def test_sheet_pull_ok(ws):
    runner = two_phase("fx", manifest_files=manifest("fx", prefix="mseo report - "), tabs=all_tabs("fx", prefix="mseo report - "))
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=runner, now=NOW)
    assert code == 0 and rec["status"] == "ok", rec["error"]
    assert len(runner.calls) == 3  # sign-in, manifest, tables
    assert rec["seen_via"] == f"sheet:{SHEET_ID}" and rec["account_id_seen"] == ADS
    names = [f["path"] for f in rec["files"]]
    assert set(sheet.TABLE_FILES.values()) <= set(names)
    assert {"export-_manifest.csv", "applied_log.csv", "_errors.csv"} <= set(names)
    assert rec["complete"]["value"] is True
    assert rec["checks"] == [{"name": "script_errors", "status": "pass", "detail": "every table was written by the Ads Script",
                              "evidence": []}]
    assert not any(ws.path("data", "inbox").iterdir())
    run = ws.path("data", "runs", rec["run_id"])
    for f in rec["files"]:
        assert sha256_file(run / f["path"]) == f["sha256"]
    camp = next(f for f in rec["files"] if f["path"] == "campaigns.csv")
    assert camp["rows"] == 3


def test_sheet_pull_records_the_script_errors_and_is_incomplete(ws):
    tabs = all_tabs("fx")
    tabs["_errors.csv"] = tab_bytes("fx", "_errors") + b'"ad_schedule","Error: quota"\n'
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", tabs=tabs), now=NOW)
    assert code == 0 and rec["status"] == "ok"
    assert rec["checks"][0]["status"] == "fail" and rec["checks"][0]["evidence"] == [{"table": "ad_schedule", "error": "Error: quota"}]
    assert rec["complete"]["value"] is False and "ad_schedule" in rec["complete"]["reason"]


def test_sheet_pull_without_the_optional_tabs_is_still_ok(ws):
    """An older script build writes no applied_log or _errors; the ten report tables are enough."""
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", tabs=all_tabs("fx", names=list(sheet.TABLES))), now=NOW)
    assert code == 0 and rec["status"] == "ok" and rec["checks"] == []
    assert "applied_log.csv" not in [f["path"] for f in rec["files"]]


def test_sheet_pull_wrong_account_saves_nothing(ws):
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fy"), now=NOW)
    assert code == 1 and rec["status"] == "wrong_account" and rec["files"] == []
    assert rec["account_id_seen"] == FY["accounts"]["google_ads"]["id"]
    assert not any(ws.path("data", "inbox").iterdir())


def test_sheet_pull_from_another_sheet_is_unverified(ws):
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", sheet_id=OTHER_SHEET), now=NOW)
    assert rec["status"] == "unverified_account" and rec["files"] == [] and "sheet" in rec["error"]
    assert not any(ws.path("data", "inbox").iterdir())
    # the manifest phase alone reaching another sheet is enough to refuse
    runner = Seq(SIGN_IN, (manifest("fx"), manifest_url(OTHER_SHEET)), (all_tabs("fx"), tab_urls()))
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=runner, now=NOW)
    assert rec["status"] == "unverified_account" and len(runner.calls) == 2


def test_sheet_pull_manifest_missing_a_table_is_an_error(ws):
    files = {"_manifest.csv": tab_bytes("fx", "_manifest").replace(b'"budgets","0","1010"\n', b"")}
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", manifest_files=files), now=NOW)
    assert rec["status"] == "error" and "no gid for: budgets" in rec["error"] and rec["files"] == []
    assert not any(ws.path("data", "inbox").iterdir())


def test_sheet_pull_gid_0_that_is_not_the_manifest_is_an_error(ws):
    files = {"_manifest.csv": tab_bytes("fx", "campaigns")}
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", manifest_files=files), now=NOW)
    assert rec["status"] == "error" and "not the manifest" in rec["error"]


def test_sheet_pull_layout_changed(ws):
    files = all_tabs("fx")
    files["campaigns.csv"] = files["campaigns.csv"].replace(b"# mseo.ads_sheet/1", b"# mseo.ads_sheet/2")
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", tabs=files), now=NOW)
    assert rec["status"] == "layout_changed" and rec["files"] == []
    assert not any(ws.path("data", "inbox").iterdir())
    files = all_tabs("fx")
    files["keywords.csv"] = files["keywords.csv"].replace(b'"bumper repair"', HUGE_FIELD)  # csv.Error
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", tabs=files), now=NOW)
    assert rec["status"] == "layout_changed" and not any(ws.path("data", "inbox").iterdir())


def test_sheet_pull_for_another_period_is_an_error(ws):
    files = all_tabs("fx")
    files["campaigns.csv"] = files["campaigns.csv"].replace(b'"period","2026-10-26","2026-11-01"',
                                                            b'"period","2026-10-19","2026-10-25"')
    code, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx", tabs=files), now=NOW)
    assert rec["status"] == "error" and "period" in rec["error"] and rec["files"] == []


def test_sheet_pull_within_lag_is_incomplete(ws, monkeypatch):
    monkeypatch.setattr("mseo.doctor.today", lambda: dt.date(2026, 11, 2))
    _, rec, _ = browser_pull.pull(ws, "google_ads", W44, runner=two_phase("fx"), now=NOW)
    assert rec["status"] == "ok" and rec["complete"] == {"value": False, "reason": "within reporting lag"}


def test_without_a_sheet_id_the_report_editor_path_is_used(root):
    assert browser_pull.resolve(workspace.open("fx", keychain=FakeKeychain()), "google_ads") == "report_editor"
    with_sheet(root)
    assert browser_pull.resolve(workspace.open("fx", keychain=FakeKeychain()), "google_ads") == "sheet"
    edit_config(root, "fx", lambda c: c["accounts"]["google_ads"].pop("scripts_sheet_id"))
    assert browser_pull.resolve(workspace.open("fx", keychain=FakeKeychain()), "google_ads") == "report_editor"


def test_prompt_has_two_phases_and_no_customer_text(ws):
    from mseo.browser import run as browser_run

    first = browser_run.prompt(sheet.PROMPT, ws, source="google_ads", **sheet.values(ws, W44))
    assert sheet.tab_url(SHEET_ID, sheet.MANIFEST_GID) in first and "Download 1 tab" in first
    assert not any(sheet.tab_url(SHEET_ID, g) in first for g in gids().values())
    second = browser_run.prompt(sheet.PROMPT, ws, source="google_ads", **sheet.values(ws, W44, gids()))
    for g in gids().values():
        assert sheet.tab_url(SHEET_ID, g) in second
    assert f"Download {len(gids())} tab" in second and "gviz" not in second and "sheet=" not in second
    assert FX["identity"]["email"] in first
    raw = (browser_run.PROMPTS / f"{sheet.PROMPT}.md").read_text()
    assert SHEET_ID not in raw and FX["business"]["name"] not in raw


# ------------------------------------------------------------------ the week file

def test_week_assemble_reads_sheet_campaigns_and_lists_the_tables(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    run_id = seed_tables(ws)
    out = week.assemble(ws, W)
    ads = out["sources"]["google_ads"]
    assert ads["status"] == "ok" and ads["run_id"] == run_id
    assert ads["metrics"]["cost"] == pytest.approx(223.55) and ads["metrics"]["calls"] == 9
    assert ads["metrics"]["clicks"] == 79 and ads["metrics"]["conversions"] == 6.5
    assert set(ads["tables"]) == set(sheet.TABLES) and ads["tables"]["search_terms"]["rows"] == 6
    assert ads["by_campaign"]["Synthetic Repairs"]["cost"] == 182.35
