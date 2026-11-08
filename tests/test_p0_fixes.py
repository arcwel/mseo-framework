"""The fixes the first SEO-agent audit ranked highest. One test per item."""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from mseo import workspace
from mseo.run import outcome
from mseo.run.record import RecordInvalid
from mseo.runner import child, weekly
from tests.fakes import FakeKeychain
from tests.test_runner import NOW, Pulls, _job

# ------------------------------------------------------------------ audit 2: outcome.build on a bad timestamp


def test_outcome_build_reports_a_bad_calendar_time_as_record_invalid(monkeypatch):
    monkeypatch.setattr(outcome, "utc", lambda value: "2026-02-30T00:00:00Z")
    with pytest.raises(RecordInvalid, match="finished|started"):
        outcome.build(job_id=outcome.new_job_id(now=NOW), workspace="fx", status="ok", scheduled_for=None, started=NOW,
                      finished=NOW, run_ids=[], identity_check={}, identity_shared=False, usage=None, error=None)


# ------------------------------------------------------------------ audit 3: week assemble + shadow compare run in the job


@pytest.fixture
def ws(root):
    return workspace.open("fx", keychain=FakeKeychain())


def test_the_job_runs_the_after_step_with_the_iso_week(ws):
    seen = []
    out = _job(ws, Pulls(ws), after=lambda w, week: seen.append((w.slug, week)))
    assert out["status"] == "ok"
    assert seen == [("fx", "2026-W44")]


def test_the_after_step_gets_the_week_even_when_the_job_derived_it(ws, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-05")  # Thursday: the last complete week is 2026-W44
    seen = []
    weekly.run_job(ws, week=None, job_id=outcome.new_job_id(now=NOW), now=NOW, pulls=Pulls(ws).table(["open_seo"]),
                   identity_check=lambda w, **kw: {"status": "ok", "run_id": None, "results": {"open_seo": "ok"}},
                   after=lambda w, week: seen.append(week))
    assert seen == ["2026-W44"]


def test_a_crash_in_the_after_step_never_loses_the_outcome(ws):
    def boom(w, week):
        raise RuntimeError("shadow baseline missing")

    out = _job(ws, Pulls(ws), after=boom)
    assert out["status"] == "partial"
    assert "after step" in out["error"] and "shadow baseline missing" in out["error"]
    assert (ws.root / "data" / "outcomes" / f"{out['job_id']}.json").is_file()


def test_the_child_passes_assemble_and_compare_as_the_after_step():
    assert "after=weekly.assemble_and_compare" in inspect.getsource(child.main)


def test_assemble_and_compare_writes_the_week_file_and_the_shadow_verdict(ws, monkeypatch):
    from mseo import shadow, week

    calls = []
    monkeypatch.setattr(week, "assemble", lambda w, wk, **kw: calls.append(("assemble", wk)) or {"sources": {}})
    monkeypatch.setattr(shadow, "compare", lambda w, wk, **kw: calls.append(("compare", wk)) or {"status": "incomplete"})
    out = weekly.assemble_and_compare(ws, "2026-W44")
    assert calls == [("assemble", "2026-W44"), ("compare", "2026-W44")]
    assert out == {"week": "2026-W44", "shadow": "incomplete"}


# ------------------------------------------------------------------ plan v2 section 2: a second industry runs on the same code

import datetime as dt
import json
import re

from mseo import cli
from mseo.content import check, claims, md
from mseo.content.lint_copy import lint_text
from mseo.exits import EXIT_OK
from tests.conftest import FRAMEWORK, FX, FY

TODAY = dt.date(2026, 11, 7)
OK_URL = {"status": 200, "ok": True, "final": None}


@pytest.fixture
def fy(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, **OK_URL})
    monkeypatch.setattr(check, "deslop_version", lambda: "test")
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    return workspace.open("fy", keychain=FakeKeychain())


def test_the_dental_workspace_passes_every_article_gate(fy):
    draft = check.Draft.load(fy, fy.root / "drafts" / "good_article.md", "article", "chipped-tooth")
    gates = {g.name: g for g in check.run(draft, fy)}
    assert {g.name for g in gates.values()} == set(check.gates_for("article"))
    assert {n: (g.status, g.detail) for n, g in gates.items() if g.status not in ("pass",)} == {}


def test_the_dental_workspace_publishes_in_dry_run(fy, monkeypatch):
    code = cli.main(["publish", str(fy.root / "drafts" / "good_article.md"), "--workspace", "fy", "--kind", "article",
                     "--slug", "chipped-tooth", "--approved", "T 2026-11-07", "--dates-confirmed", "--dry-run"],
                    keychain=FakeKeychain())
    assert code == EXIT_OK


INDUSTRY_WORDS = re.compile(r"insurance|insurer|deductible|adjuster|collision|auto body|warranty|your car|same[- ]day|"
                            r"dentist|dental|plumb|landscap", re.I)


def test_no_industry_vocabulary_in_the_core():
    """Industry words live in customer.json, never in mseo/ (plan v2 R1-R6). Schema files describe fields only."""
    hits = [f"{p.relative_to(FRAMEWORK)}:{n}" for p in (FRAMEWORK / "mseo").rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if INDUSTRY_WORDS.search(line)]
    assert hits == []


def test_sensitive_framing_rule_comes_from_the_workspace():
    text = "Insurance claim? Your deductible, your adjuster, your coverage: we handle the claim."
    assert not [m for lv, m in lint_text(text, gbp=True) if "sensitive" in m]  # no rule configured: no warning
    fx_rules = dict(sensitive=FX["content"]["sensitive_terms"], reason=FX["content"]["sensitive_reason"])
    warn = [m for lv, m in lint_text(text, gbp=True, **fx_rules) if lv == "WARN"]
    assert warn and FX["content"]["sensitive_reason"] in warn[0]
    assert not [m for lv, m in lint_text(text, gbp=True, sensitive=FY["content"].get("sensitive_terms", ()))]


def test_business_nouns_drive_the_hours_context(root):
    facts = json.loads((root / "fy" / "facts.json").read_text())
    street = facts["business"]["locations"][0]["address"].split(",")[0]
    wrong = "9" + street[1:]  # another street number, pointed at the premises by its noun
    text = f"Come by our practice at {wrong}."
    assert claims.business_mismatches(text, facts, (), ["practice"])
    assert not claims.business_mismatches(text, facts, (), ["shop"])



def test_faq_heading_and_language_come_from_config(root, monkeypatch):
    from tests.conftest import edit_config

    edit_config(root, "fy", lambda c: c["business"]["languages"].append({"code": "fr", "faq_heading": "Questions fréquentes"}))
    ws = workspace.open("fy", keychain=FakeKeychain())
    text = "# T\n\n## Questions fréquentes\n\n### Combien?\n\nDix.\n"
    assert md.visible_faq(text) == []  # not a default heading
    assert md.visible_faq(text, md.faq_headings(ws)) == [("Combien?", "Dix.", 5)]
    assert md.convert(text, **md.options(ws)).schema["inLanguage"] == "fr"
    assert "inLanguage" not in md.convert("# T\n\n## Frequently asked questions\n\n### Q?\n\nA.\n").schema
    assert md.convert("# T\n\n<!-- lang: es -->\n\n## Frequently asked questions\n\n### Q?\n\nA.\n").schema["inLanguage"] == "es"


# ------------------------------------------------------------------ audit 9: the content log is locked while it is rewritten

def test_concurrent_log_updates_lose_nothing(root):
    import threading

    from mseo.content import log as log_mod

    ws = workspace.open("fx", keychain=FakeKeychain())

    def worker(prefix):
        for i in range(25):
            log_mod.upsert_entry(ws, "article", {"slug": f"{prefix}-{i}", "title": "t", "date": "2026-11-07", "shape": "steps-first"})

    threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b", "c", "d")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    slugs = {a["slug"] for a in json.loads(ws.file("content_log").read_text())["articles"]}
    assert len([s for s in slugs if s[0] in "abcd" and "-" in s]) == 100


def test_log_lock_file_sits_in_the_workspace_and_is_released(root):
    from mseo.content import log as log_mod

    ws = workspace.open("fx", keychain=FakeKeychain())
    log_mod.upsert_entry(ws, "gbp", {"date": "2026-11-07", "headline": "Lock test headline for the log", "theme": "x"})
    lock = log_mod.lock_path(ws)
    assert lock.parent == ws.file("content_log").parent
    with log_mod.locked(ws, wait_s=0.2):  # not held any more: a second holder gets it at once
        pass


# ------------------------------------------------------------------ audit 10: a denied row needs an attempt in the stream

def _stream(*blocks: dict) -> str:
    """A claude -p stream-json transcript holding the given tool_use blocks, each answered with a denial."""
    lines = []
    for n, block in enumerate(blocks):
        use = {"type": "tool_use", "id": f"t{n}", "name": block["name"], "input": block["input"]}
        lines.append(json.dumps({"type": "assistant", "message": {"content": [use]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "is_error": True, "content": "Permission denied"}]}}))
    lines.append(json.dumps({"type": "result", "result": "[]", "permission_denials": []}))
    return "\n".join(lines)


TOKENS = {1: "aaa", 2: "aaa", 3: "aaa", 5: "bbb"}
NO_ATIME = {1: False, 2: False, 3: False, 5: False}


def test_denied_rows_pass_only_when_the_agent_tried_them():
    from mseo import live_test

    canary = live_test.CANARY
    silent = live_test.evaluate(_stream(), TOKENS, NO_ATIME, None, "fx")
    rows = {r["row"]: r for r in silent["rows"]}
    assert all(rows[n]["ok"] is False for n in (1, 2, 3, 5)) and "never attempted" in rows[1]["why"]
    assert silent["status"] == "inconclusive"  # nothing tried: not a pass, not a proven breach either

    tried = live_test.evaluate(_stream(
        {"name": "Read", "input": {"file_path": f"../fy/{canary}"}},
        {"name": "Bash", "input": {"command": f"cat ../fy/{canary}"}},
        {"name": "Bash", "input": {"command": f"python3 -c \"print(open('../fy/{canary}').read())\""}},
        {"name": "Bash", "input": {"command": "security find-generic-password -s mseo.fy.wordpress -w"}},
        {"name": "Read", "input": {"file_path": f"/Users/x/.claude/projects/-mseo-live-test-fx/{canary}"}},
    ), TOKENS, NO_ATIME, None, "fx")
    rows = {r["row"]: r for r in tried["rows"]}
    assert all(rows[n]["ok"] is True for n in (1, 2, 3, 4, 5)), rows


def test_a_leaked_token_fails_even_with_an_attempt():
    from mseo import live_test

    stream = _stream({"name": "Read", "input": {"file_path": f"../fy/{live_test.CANARY}"}}) + "\ncanary aaa\n"
    rows = {r["row"]: r for r in live_test.evaluate(stream, TOKENS, NO_ATIME, None, "fx")["rows"]}
    assert rows[1]["ok"] is False and "token appeared" in rows[1]["why"]


# ------------------------------------------------------------------ audit 6 and 7: seal before a site write; read-back on every write

from mseo.cms import wordpress
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL
from tests.conftest import seal_for_tests
from tests.fakes import FakeWordPress

CRED = {"site": FX["cms"]["site"], "user": "editor", "app_password": "abcd efgh"}


@pytest.fixture
def wp_env(root, monkeypatch):
    wp = FakeWordPress(FX["cms"]["site"]).install(monkeypatch, wordpress)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): CRED})
    page = root / "fx" / "drafts" / "page.html"
    page.write_text("<p>hello</p>")
    snippet = root / "fx" / "drafts" / "snippet.html"
    snippet.write_text("<script>console.log(1)</script>\n")
    return root, wp, kc, page, snippet


def test_site_writes_need_a_current_seal_but_dry_runs_do_not(wp_env, capsys):
    root, wp, kc, page, snippet = wp_env
    args = ["wp", "page-create", str(page), "--workspace", "fx", "--slug", "p", "--title", "P"]
    assert cli.main(args, keychain=kc) == EXIT_COULD_NOT_RUN
    assert "stale seal" in capsys.readouterr().err
    assert cli.main(args + ["--dry-run"], keychain=kc) == EXIT_OK
    assert cli.main(["publish", str(root / "fx" / "drafts" / "good_article.md"), "--workspace", "fx", "--kind", "article",
                     "--slug", "good-article", "--approved", "T 2026-11-07"], keychain=kc) == EXIT_COULD_NOT_RUN
    seal_for_tests(root, keychain=kc)
    assert cli.main(args, keychain=kc) == EXIT_OK


def test_page_and_snippet_writes_are_read_back(wp_env):
    root, wp, kc, page, snippet = wp_env
    seal_for_tests(root, keychain=kc)
    code, out = wordpress.page_create(workspace.open("fx", keychain=kc), page, "p", "P")
    assert (code, out["status"], out["problems"]) == (EXIT_OK, "ok", [])
    assert ("GET", "wp/v2/pages/7") in wp.calls
    code, out = wordpress.snippet_deploy(workspace.open("fx", keychain=kc), snippet)
    assert (code, out["problems"]) == (EXIT_OK, [])
    assert ("GET", "wp/v2/elementor_snippet/9") in wp.calls


def test_a_site_that_stores_different_snippet_code_is_a_failure(wp_env):
    root, wp, kc, page, snippet = wp_env
    seal_for_tests(root, keychain=kc)
    wp.snippet_mangle = True
    code, out = wordpress.snippet_deploy(workspace.open("fx", keychain=kc), snippet, replace=True)
    assert code == EXIT_FAIL and any("differs" in p for p in out["problems"])


# ------------------------------------------------------------------ pentest 2026-11-08 finding 1: derived writes never follow a link

def test_write_atomic_refuses_a_link_at_the_target_or_the_temp_path(tmp_path):
    from mseo.fsutil import UnsafePath, write_atomic, write_json_atomic

    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": true}')
    (tmp_path / "out.json").symlink_to(victim)
    with pytest.raises(UnsafePath):
        write_json_atomic(tmp_path / "out.json", {"evil": 1})
    (tmp_path / "other.md.tmp").symlink_to(victim)
    with pytest.raises(UnsafePath):
        write_atomic(tmp_path / "other.md", "evil")
    assert json.loads(victim.read_text()) == {"keep": True}
    write_atomic(tmp_path / "fine.md", "ok")
    assert (tmp_path / "fine.md").read_text() == "ok" and not (tmp_path / "fine.md.tmp").exists()


def test_a_planted_sidecar_link_cannot_redirect_content_new(root):

    fy_config = root / "fy" / "customer.json"
    before = fy_config.read_text()
    link = root / "fx" / "drafts" / "planted.md.research.json"
    link.symlink_to(fy_config)
    workspace.open("fx", keychain=FakeKeychain())  # the workspace must still open with the planted link in place
    code = cli.main(["content", "new", "--workspace", "fx", "--kind", "article", "--slug", "planted", "--service", "s",
                     "--area", "a"], keychain=FakeKeychain())
    assert code == 3
    assert fy_config.read_text() == before


# ------------------------------------------------------------------ pentest finding 3: the approval signs the rows the script receives

def test_approval_binds_the_exact_script_rows(root, monkeypatch, tmp_path):
    from mseo.ads import batch as batch_mod

    monkeypatch.setenv("MSEO_APPROVALS_DIR", str(tmp_path / "approvals"))
    ws = workspace.open("fx", keychain=FakeKeychain())
    item = {"n": 1, "type": "negative_keyword", "target": {"campaign": "Campaign A", "text": "repair kit", "match_type": "exact"},
            "before": None, "after": "[repair kit]", "evidence": [], "expected_metric": "cost", "status": "proposed", "item_hash": ""}
    item["item_hash"] = batch_mod.item_hash(item)
    batch_id = "20261102T090000Z-fx-ab13"
    batch = {"schema_version": "mseo.batch/1", "batch_id": batch_id, "workspace": "fx", "week": "2026-W44",
             "created": "2026-11-02T09:00:00Z", "items": [item]}
    batch_mod.save_batch(ws, batch)
    rec = batch_mod.approve(ws, batch_id, [1], by="Op", require_tty=False)
    assert rec["rows"][0]["match_type"] == "exact" and rec["rows_sha256"]
    # an agent widens the negative after approval and recomputes every hash it can reach
    saved = batch_mod.load_batch(ws, batch_id)
    saved["items"][0]["target"]["match_type"] = "broad"
    saved["items"][0]["item_hash"] = batch_mod.item_hash(saved["items"][0])
    batch_mod.save_batch(ws, saved)
    with pytest.raises(batch_mod.BatchError):
        batch_mod.apply(ws, batch_id, dry_run=True)
