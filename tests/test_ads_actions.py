"""Plan v2 3.3 items 3-4: `mseo actions propose | approve | apply | status`. The approval record lives outside the
workspace (never inside the workspaces root, and only from a terminal); a batch item changed after approval is
refused; apply writes the approved_changes CSV for the Ads Script with a formula guard; status reads the script's
applied_log back. Approving, confirming the paste, status and measure never change the batch content hash."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json

import pytest

from mseo import cli, loop, workspace
from mseo.adapters import google_ads_sheet as sheet
from mseo.ads import batch as batch_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_FAIL, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.run import record
from tests.conftest import edit_config, seal_for_tests
from tests.fakes import FakeKeychain
from tests.test_ads_sheet import W, seed_tables, with_sheet

NOW = dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc)
SCRIPT_TYPES = batch_mod.SCRIPT_TYPES
REPAIRS, PAINT = "Synthetic Repairs", "Synthetic Paint"  # the fx fixture's two enabled campaigns


RUNS: dict[str, str] = {}  # slug -> the seeded run id of the fixture workspace


class Tty:
    """stdin that says it is a terminal (an approval is typed by a person)."""

    def isatty(self):
        return True


def approve(ws, batch_id, items, by="Operator"):
    return batch_mod.approve(ws, batch_id, items, by=by, now=NOW, require_tty=False)


def rewrite_table(ws, run_id: str, name: str, rows: list[dict]) -> None:
    """Replace one table of a seeded run (adding it when absent) and fix the record's hash, as the pull would have."""
    run_dir = ws.path("data", "runs", run_id)
    entry = sheet.write_table(run_dir, f"{name}.csv", sheet.fields(name), rows)
    rec = json.loads((run_dir / "record.json").read_text())
    rec["files"] = [f for f in rec["files"] if f["path"] != entry["path"]] + [entry]
    write_json_atomic(run_dir / "record.json", rec)


def read_rows(ws, run_id: str, name: str) -> list[dict]:
    with ws.path("data", "runs", run_id, f"{name}.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def fx(root, tmp_path, monkeypatch):
    with_sheet(root, "fx")
    monkeypatch.setenv("MSEO_APPROVALS_DIR", str(tmp_path / "approvals"))
    ws = workspace.open("fx", keychain=FakeKeychain())
    RUNS["fx"] = seed_tables(ws, "fx")
    return ws


@pytest.fixture
def fy(root, tmp_path, monkeypatch):
    with_sheet(root, "fy")
    monkeypatch.setenv("MSEO_APPROVALS_DIR", str(tmp_path / "approvals"))
    ws = workspace.open("fy", keychain=FakeKeychain())
    seed_tables(ws, "fy")
    return ws


def propose(ws):
    return batch_mod.propose(ws, W, now=NOW)


def types(b):
    return {i["type"] for i in b["items"]}


def test_propose_writes_a_valid_numbered_batch(fx):
    b = propose(fx)
    path = fx.path("data", "batches", f"{b['batch_id']}.json")
    assert path.is_file()
    record.check_schema("batch.v1.json", json.loads(path.read_text()))
    assert b["batch_id"].split("-")[1:-1] == ["fx"] and b["week"] == W and b["workspace"] == "fx"
    assert [i["n"] for i in b["items"]] == list(range(1, len(b["items"]) + 1))
    assert types(b) >= {"negative_keyword", "pause_keyword", "budget", "ad_schedule"}
    for item in b["items"]:
        assert item["status"] == "proposed" and item["evidence"] is not None
        assert item["item_hash"] == batch_mod.item_hash(item)


def test_propose_items_carry_evidence_and_before_after(fx):
    b = propose(fx)
    negs = [i for i in b["items"] if i["type"] == "negative_keyword" and i["target"].get("campaign") != "*"]
    assert sorted(i["target"]["text"] for i in negs) == ["free bumper repair kit", "paint shop jobs"]
    assert all(i["target"]["match_type"] == "exact" and i["before"]["conversions"] == 0 and i["evidence"] for i in negs)
    pause = next(i for i in b["items"] if i["type"] == "pause_keyword")
    assert pause["target"]["text"] == "bumper repair" and pause["before"]["clicks"] >= 30 and pause["after"]["status"] == "PAUSED"
    budget = next(i for i in b["items"] if i["type"] == "budget")
    assert budget["target"]["campaign"] == "Synthetic Repairs" and budget["before"]["budget"] == 25.0 and budget["after"]["budget"] == 30.0
    sched = next(i for i in b["items"] if i["type"] == "ad_schedule")
    assert sched["target"]["hour"] == 3 and sched["after"]["exclude_hour"] == 3
    seeds = [i for i in b["items"] if i["type"] == "negative_keyword" and i["target"].get("campaign") == "*"]
    assert seeds and all("cannot be read" in i["note"] for i in seeds)
    seed_texts = {i["target"]["text"] for i in seeds}
    assert {"free", "jobs"} <= seed_texts  # from the generic list the fixture's default config selects


def test_seed_list_follows_the_configured_name_and_its_extends(root, fx):
    edit_config(root, "fx", lambda c: c.setdefault("ads", {}).update(negative_seed_list="auto-body"))
    b = propose(workspace.open("fx", keychain=FakeKeychain()))
    seed_texts = {i["target"]["text"] for i in b["items"] if i["target"].get("campaign") == "*"}
    assert {"free", "jobs", "parts", "wrap"} <= seed_texts


def test_propose_cli_prints_the_numbered_list(fx, capsys):
    assert cli.main(["actions", "propose", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[1].startswith("1.") or any(line.startswith("1.") for line in out.splitlines())
    assert cli.main(["actions", "propose", "--workspace", "fx", "--week", W, "--json"], keychain=FakeKeychain()) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["items"] and "messages" not in data


def test_parse_items():
    assert batch_mod.parse_items("1,3-5,9") == [1, 3, 4, 5, 9]
    with pytest.raises(ValueError):
        batch_mod.parse_items("1,x")


def test_approve_writes_the_record_outside_the_workspace(fx, tmp_path):
    b = propose(fx)
    rec = approve(fx, b["batch_id"], [1, 2])
    path = batch_mod.approval_path(fx, b["batch_id"])
    assert path.is_file() and fx.root not in path.parents and str(path).startswith(str(tmp_path / "approvals"))
    record.check_schema("approval.v1.json", rec)
    assert rec["approved_by"] == "Operator" and [i["n"] for i in rec["items"]] == [1, 2]
    assert rec["items"][0]["item_hash"] == b["items"][0]["item_hash"]
    saved = json.loads(fx.path("data", "batches", f"{b['batch_id']}.json").read_text())
    assert [i["status"] for i in saved["items"][:3]] == ["approved", "approved", "proposed"]
    assert rec["batch_sha256"] == batch_mod.batch_content_sha256(saved)  # statuses are not part of the hash


def test_default_approvals_dir_is_under_the_home_config(monkeypatch):
    monkeypatch.delenv("MSEO_APPROVALS_DIR", raising=False)
    assert batch_mod.approvals_dir().as_posix().endswith(".config/mseo/approvals")


def test_approve_refuses_a_changed_item(fx):
    b = propose(fx)
    path = fx.path("data", "batches", f"{b['batch_id']}.json")
    data = json.loads(path.read_text())
    data["items"][0]["after"] = {"tampered": True}
    path.write_text(json.dumps(data))
    with pytest.raises(batch_mod.BatchError, match="hash"):
        approve(fx, b["batch_id"], [1])
    assert not batch_mod.approval_path(fx, b["batch_id"]).exists()


def test_approve_unknown_item_or_batch_is_refused(fx):
    b = propose(fx)
    with pytest.raises(batch_mod.BatchError, match="no item"):
        approve(fx, b["batch_id"], [999])
    with pytest.raises(batch_mod.BatchError, match="no batch"):
        approve(fx, "20260101T000000Z-fx-dead", [1])


def test_approve_twice_exits_2(fx, capsys):
    b = propose(fx)
    argv = ["actions", "approve", "--workspace", "fx", "--batch", b["batch_id"], "--items", "1", "--by", "Operator"]
    assert cli.main(argv, keychain=FakeKeychain(), stdin=Tty()) == EXIT_OK
    assert cli.main(argv, keychain=FakeKeychain(), stdin=Tty()) == EXIT_EXISTS


# ------------------------------------------------------------------ audit M1: an agent cannot mint an approval

def test_approve_needs_a_terminal_on_stdin(fx, capsys):
    b = propose(fx)
    argv = ["actions", "approve", "--workspace", "fx", "--batch", b["batch_id"], "--items", "1", "--by", "Operator"]
    assert cli.main(argv, keychain=FakeKeychain(), stdin=io.StringIO("")) == EXIT_COULD_NOT_RUN
    assert "terminal" in capsys.readouterr().err and not batch_mod.approval_path(fx, b["batch_id"]).exists()
    with pytest.raises(batch_mod.BatchError, match="terminal"):
        batch_mod.approve(fx, b["batch_id"], [1], by="Operator", now=NOW, stdin=io.StringIO(""))
    rec = batch_mod.approve(fx, b["batch_id"], [1], by="Operator", now=NOW, stdin=Tty())
    assert rec["tty"] is True


def test_approvals_dir_inside_the_workspaces_root_is_refused(fx, root, monkeypatch):
    b = propose(fx)
    for inside in (root / "fx" / ".a", root / "approvals", root):
        monkeypatch.setenv("MSEO_APPROVALS_DIR", str(inside))
        with pytest.raises(batch_mod.BatchError, match="inside the workspaces root"):
            approve(fx, b["batch_id"], [1])
        with pytest.raises(batch_mod.BatchError, match="inside the workspaces root"):
            batch_mod.apply(fx, b["batch_id"], dry_run=True)
    assert not list(root.rglob("*.json")) or not any("approvals" in p.parts or ".a" in p.parts for p in root.rglob("*.json"))
    monkeypatch.setenv("MSEO_APPROVALS_DIR", str(root.parent / "elsewhere"))
    assert batch_mod.approvals_dir() == root.parent / "elsewhere"


def test_apply_round_trip(root, fx, capsys):
    b = propose(fx)
    n_items = [i["n"] for i in b["items"] if i["type"] in ("negative_keyword", "pause_keyword", "budget", "ad_schedule")]
    approve(fx, b["batch_id"], n_items)
    # dry run needs no seal and writes nothing
    out = batch_mod.apply(fx, b["batch_id"], dry_run=True)
    assert out["tab"] == "approved_changes" and out["rows"] and not fx.path("data", "batches", f"{b['batch_id']}.approved_changes.csv").exists()
    seal_for_tests(root, ("fx",))
    ws = workspace.open("fx", keychain=FakeKeychain())
    out = batch_mod.apply(ws, b["batch_id"])
    csv_path = ws.path("data", "batches", f"{b['batch_id']}.approved_changes.csv")
    with csv_path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["batch_id", "n", "item_hash", "type", "campaign", "ad_group", "text", "match_type", "value"]
    assert {r["type"] for r in rows} == {"negative_keyword", "pause_keyword", "budget"}  # ad_schedule is not script-applied
    assert all(r["batch_id"] == b["batch_id"] for r in rows)
    budget_row = next(r for r in rows if r["type"] == "budget")
    assert budget_row["value"] == "30.0" and budget_row["campaign"] == "Synthetic Repairs"
    seed_rows = [r for r in rows if r["type"] == "negative_keyword" and r["text"] == "free"]
    assert {r["campaign"] for r in seed_rows} == {"Synthetic Repairs", "Synthetic Paint"}  # one row per enabled campaign
    saved = json.loads(ws.path("data", "batches", f"{b['batch_id']}.json").read_text())
    assert all(i["status"] == "approved" for i in saved["items"] if i["n"] in n_items)  # not yet pasted
    out = batch_mod.apply(ws, b["batch_id"], confirm_pasted=True, now=NOW)
    saved = json.loads(ws.path("data", "batches", f"{b['batch_id']}.json").read_text())
    by_n = {i["n"]: i for i in saved["items"]}
    # G: the paste is recorded; the items stay approved until the script's applied_log says they were applied
    assert all(by_n[n]["status"] == "approved" and by_n[n]["pasted_at"] == "2026-11-09" for n in n_items if by_n[n]["type"] != "ad_schedule")
    assert not any("applied_at" in by_n[n] for n in n_items)
    sched = next(i for i in saved["items"] if i["type"] == "ad_schedule")
    assert sched["status"] == "approved" and "D3" in sched["note"] and "pasted_at" not in sched
    assert out["applied"] and out["deferred"]


def test_csv_rows_carry_ad_group_and_match_type_for_pauses_and_guard_formulas(fx):
    b = propose(fx)
    pause = next(i for i in b["items"] if i["type"] == "pause_keyword")
    rows = batch_mod._rows(pause)
    assert len(rows) == 1 and rows[0]["ad_group"] == pause["target"]["ad_group"] == "Bumper Repair"
    assert rows[0]["match_type"] == "broad" and rows[0]["campaign"] == REPAIRS and rows[0]["text"] == "bumper repair"
    # L6: a cell that starts with = + - or @ is prefixed with a single quote; anything else is written as is
    assert batch_mod.guard_cell("+crown") == "'+crown" and batch_mod.guard_cell("=SUM(A1)") == "'=SUM(A1)"
    assert batch_mod.guard_cell("-x") == "'-x" and batch_mod.guard_cell("@me") == "'@me"
    assert batch_mod.guard_cell("crown repair") == "crown repair" and batch_mod.guard_cell(30.0) == "30.0" and batch_mod.guard_cell(None) == ""


def test_apply_writes_a_formula_starting_term_with_a_text_prefix(root, fx):
    rows = read_rows(fx, RUNS["fx"], "search_terms")
    for r in rows:
        if r["search_term"] == "free bumper repair kit":
            r["search_term"] = "+crown bumper repair"
    rewrite_table(fx, RUNS["fx"], "search_terms", rows)
    b = propose(fx)
    item = next(i for i in b["items"] if i["target"].get("text") == "+crown bumper repair")
    approve(fx, b["batch_id"], [item["n"]])
    seal_for_tests(root, ("fx",))
    ws = workspace.open("fx", keychain=FakeKeychain())
    batch_mod.apply(ws, b["batch_id"])
    text = ws.path("data", "batches", f"{b['batch_id']}.approved_changes.csv").read_text()
    assert "'+crown bumper repair" in text and "\n+crown" not in text and ",+crown" not in text


def test_shared_budget_is_not_proposed(fx):
    rows = read_rows(fx, RUNS["fx"], "budgets")
    for r in rows:
        if r["campaign"] == REPAIRS:
            r["shared"] = "true"
    rewrite_table(fx, RUNS["fx"], "budgets", rows)
    b = propose(fx)
    assert not [i for i in b["items"] if i["type"] == "budget"]  # L8: raising a shared budget raises every campaign on it


# ------------------------------------------------------------------ F: the tracking gate comes first

def test_propose_refuses_when_the_tracking_gate_fails_unless_overridden(fy, capsys):
    with pytest.raises(batch_mod.BatchError, match="tracking gate"):
        batch_mod.propose(fy, W, now=NOW)
    assert not list(fy.path("data", "batches").glob("*.json")) if fy.path("data", "batches").is_dir() else True
    code = cli.main(["actions", "propose", "--workspace", "fy", "--week", W], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN and "tracking gate" in capsys.readouterr().err
    code = cli.main(["actions", "propose", "--workspace", "fy", "--week", W, "--ignore-tracking", "--json"], keychain=FakeKeychain())
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["tracking_overridden"] is True
    saved = json.loads(fy.path("data", "batches", f"{data['batch_id']}.json").read_text())
    assert saved["tracking_overridden"] is True


def test_propose_records_no_override_when_the_gate_passes(fx):
    b = propose(fx)
    assert "tracking_overridden" not in b
    assert "tracking_overridden" not in batch_mod.propose(fx, W, now=NOW, ignore_tracking=True)


# ------------------------------------------------------------------ B + G: approve, paste, status, measure, apply again

def log_row(batch_id, n, type_, target, result, when="2026-11-10T03:05:00Z"):
    return {"applied_at": when, "batch_id": batch_id, "n": n, "type": type_, "target": target, "result": result}


def test_status_marks_items_from_the_applied_log_and_nothing_breaks_the_approval(root, fx, capsys):
    b = propose(fx)
    neg = next(i for i in b["items"] if i["type"] == "negative_keyword" and i["target"]["campaign"] != "*")
    pause = next(i for i in b["items"] if i["type"] == "pause_keyword")
    budget = next(i for i in b["items"] if i["type"] == "budget")
    sched = next(i for i in b["items"] if i["type"] == "ad_schedule")
    rec = approve(fx, b["batch_id"], [neg["n"], pause["n"], budget["n"], sched["n"]])
    seal_for_tests(root, ("fx",))
    ws = workspace.open("fx", keychain=FakeKeychain())
    batch_mod.apply(ws, b["batch_id"], confirm_pasted=True, now=NOW)
    # nothing in the applied_log yet: every pasted item is pending, exit 0
    assert cli.main(["actions", "status", "--workspace", "fx", "--week", W, "--json"], keychain=FakeKeychain()) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert sorted(p["n"] for p in out["pending"]) == sorted([neg["n"], pause["n"], budget["n"]]) and out["applied"] == []
    # the script ran: the negative ok (twice: the first ok row wins), the pause failed, the budget has no row yet
    rewrite_table(ws, RUNS["fx"], "applied_log", [
        log_row(b["batch_id"], neg["n"], "negative_keyword", f"{REPAIRS} /  / x", "ok: negative [x]", "2026-11-10T03:05:00Z"),
        log_row(b["batch_id"], pause["n"], "pause_keyword", f"{REPAIRS} / Bumper Repair / bumper repair", "failed: no keyword"),
        log_row(b["batch_id"], neg["n"], "negative_keyword", f"{REPAIRS} /  / x", "ok: negative [x]", "2026-11-17T03:05:00Z"),
        log_row("20260101T000000Z-fx-dead", 1, "budget", "other", "ok: budget 1"),  # another batch: ignored
    ])
    assert cli.main(["actions", "status", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_FAIL
    text = capsys.readouterr().out
    assert "FAILED" in text and "no keyword" in text and "1 applied, 1 failed, 1 pending" in text
    saved = json.loads(ws.path("data", "batches", f"{b['batch_id']}.json").read_text())
    by_n = {i["n"]: i for i in saved["items"]}
    assert by_n[neg["n"]]["status"] == "applied" and by_n[neg["n"]]["applied_at"] == "2026-11-10"
    assert by_n[pause["n"]]["status"] == "failed" and by_n[pause["n"]]["note"] == "failed: no keyword"
    assert by_n[budget["n"]]["status"] == "approved" and by_n[budget["n"]]["pasted_at"] == "2026-11-09"
    assert by_n[sched["n"]]["status"] == "approved"  # not a script item: the log never speaks of it
    # B: the content hash ignores status, note, pasted_at, applied_at and measured
    assert batch_mod.batch_content_sha256(saved) == rec["batch_sha256"]
    measured = loop.measure_batches(ws, "2026-W50")
    assert [m["n"] for m in measured] == [neg["n"]]  # measurement starts from the log's applied_at
    saved = json.loads(ws.path("data", "batches", f"{b['batch_id']}.json").read_text())
    assert saved["items"][neg["n"] - 1]["measured"]["as_of"] == "2026-W50"
    assert batch_mod.batch_content_sha256(saved) == rec["batch_sha256"]
    out = batch_mod.apply(ws, b["batch_id"])  # apply again still verifies
    assert out["approved_by"] == "Operator"
    # the retried pause then succeeds: the item flips to applied
    rewrite_table(ws, RUNS["fx"], "applied_log", read_rows(ws, RUNS["fx"], "applied_log") + [
        log_row(b["batch_id"], pause["n"], "pause_keyword", "t", "ok: paused keyword", "2026-11-17T03:05:00Z")])
    out = batch_mod.status(ws, W)
    assert {a["n"] for a in out["applied"]} == {neg["n"], pause["n"]} and out["failed"] == []
    assert json.loads(ws.path("data", "batches", f"{b['batch_id']}.json").read_text())["items"][pause["n"] - 1]["applied_at"] == "2026-11-17"


def test_status_without_an_applied_log_tab_is_could_not_run(root, fx, capsys):
    run_dir = fx.path("data", "runs", RUNS["fx"])
    rec = json.loads((run_dir / "record.json").read_text())
    rec["files"] = [f for f in rec["files"] if f["path"] != "applied_log.csv"]
    write_json_atomic(run_dir / "record.json", rec)
    assert cli.main(["actions", "status", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
    assert "applied_log" in capsys.readouterr().err
    assert cli.main(["actions", "status", "--workspace", "fx", "--week", "2026-W40"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN


def test_apply_without_a_current_seal_exits_3(fx, capsys):
    b = propose(fx)
    approve(fx, b["batch_id"], [1])
    code = cli.main(["actions", "apply", "--workspace", "fx", "--batch", b["batch_id"]], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN and "seal" in capsys.readouterr().err
    code = cli.main(["actions", "apply", "--workspace", "fx", "--batch", b["batch_id"], "--dry-run", "--json"],
                    keychain=FakeKeychain())
    assert code == EXIT_OK and json.loads(capsys.readouterr().out)["tab"] == "approved_changes"


def test_apply_refuses_an_item_changed_after_approval(root, fx, monkeypatch):
    b = propose(fx)
    approve(fx, b["batch_id"], [1])
    path = fx.path("data", "batches", f"{b['batch_id']}.json")
    data = json.loads(path.read_text())
    data["items"][0]["after"] = {"tampered": True}
    data["items"][0]["item_hash"] = batch_mod.item_hash(data["items"][0])  # even a recomputed hash is refused
    path.write_text(json.dumps(data))
    # the batch-level hash catches it first ...
    with pytest.raises(batch_mod.BatchError, match="changed after its approval"):
        batch_mod.apply(fx, b["batch_id"], dry_run=True)
    # ... and the item-level check catches it on its own when the batch hash is made to agree
    monkeypatch.setattr(batch_mod, "batch_content_sha256", lambda batch: batch_mod.load_approval(fx, b["batch_id"])["batch_sha256"])
    with pytest.raises(batch_mod.BatchError, match="item 1 differs from its approval record"):
        batch_mod.apply(fx, b["batch_id"], dry_run=True)


def test_apply_without_an_approval_is_refused(fx):
    b = propose(fx)
    with pytest.raises(batch_mod.BatchError, match="no approval"):
        batch_mod.apply(fx, b["batch_id"], dry_run=True)


def test_approval_sha_covers_the_batch_file_not_its_statuses(fx):
    b = propose(fx)
    rec = approve(fx, b["batch_id"], [1])
    path = fx.path("data", "batches", f"{b['batch_id']}.json")
    data = json.loads(path.read_text())
    assert batch_mod.batch_content_sha256(data) == rec["batch_sha256"]
    # the keys the flow writes later change nothing
    for item in data["items"]:
        item.update(status="applied", note="x", pasted_at="2026-11-09", applied_at="2026-11-10", measured={"before": {}})
    assert batch_mod.batch_content_sha256(data) == rec["batch_sha256"]
    # one field outside them does
    data["week"] = "2026-W01"
    assert batch_mod.batch_content_sha256(data) != rec["batch_sha256"]
    path.write_text(json.dumps(data))
    with pytest.raises(batch_mod.BatchError, match="changed"):
        batch_mod.apply(fx, b["batch_id"], dry_run=True)


def test_status_marks_a_seed_negative_applied_only_when_every_campaign_row_is_ok():
    from mseo.ads.batch import log_campaign

    assert log_campaign('{"campaign": "A / B", "ad_group": "", "text": "x"}') == "A / B"
    assert log_campaign("Old / style / row") == "Old"
    assert log_campaign("") == ""


def test_a_seed_negative_is_applied_only_when_every_campaign_row_is_ok(root, fx):
    """Pentest 2026-11-08 low 10: one failed campaign row kept the seed marked applied."""
    b = propose(fx)
    seed = next(i for i in b["items"] if i["type"] == "negative_keyword" and i["target"]["campaign"] == "*")
    campaigns = seed["target"]["campaigns"]
    assert len(campaigns) >= 2
    ws = fx
    rec = approve(fx, b["batch_id"], [seed["n"]])
    seal_for_tests(root, ("fx",))
    batch_mod.apply(ws, b["batch_id"], confirm_pasted=True)
    rows = [log_row(b["batch_id"], seed["n"], "negative_keyword", json.dumps({"campaign": c, "ad_group": "", "text": "x"}),
                    "ok: negative" if k == 0 else "failed: no campaign") for k, c in enumerate(campaigns)]
    rewrite_table(ws, RUNS["fx"], "applied_log", rows)
    out = batch_mod.status(ws, W)
    assert seed["n"] in {f["n"] for f in out["failed"]} and seed["n"] not in {a["n"] for a in out["applied"]}
    rewrite_table(ws, RUNS["fx"], "applied_log", rows + [
        log_row(b["batch_id"], seed["n"], "negative_keyword", json.dumps({"campaign": c, "ad_group": "", "text": "x"}), "ok: negative", "2026-11-17T03:05:00Z")
        for c in campaigns[1:]])
    out = batch_mod.status(ws, W)
    assert seed["n"] in {a["n"] for a in out["applied"]}
    assert rec["batch_sha256"] == batch_mod.batch_content_sha256(batch_mod.load_batch(ws, b["batch_id"]))
