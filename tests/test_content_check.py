"""mseo content check: one group per gate, then whole-run behavior (ported from the legacy test_jj_check.py).

Runs on a fresh copy of fx. Each test edits one file so the change that moves a gate is visible. The network is never
touched: check_url is replaced, today is pinned.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from mseo import cli, fsutil, workspace
from mseo.content import check
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FX
from tests.fakes import FakeKeychain

TODAY = dt.date(2026, 11, 4)
OK = {"status": 200, "ok": True}
PHONE = FX["business"]["phone"]
SITE = FX["business"]["site"]


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, **OK})
    monkeypatch.setattr(check, "deslop_version", lambda: "test")
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    return workspace.open("fx", keychain=FakeKeychain())


def article(ws):
    return ws.path("drafts", "good_article.md")


def gbp(ws):
    return ws.path("drafts", "good_gbp.txt")


def receipt(path, **changes):
    rp = path.with_name(path.name + ".deslop.json")
    data = json.loads(rp.read_text())
    data.update(changes)
    rp.write_text(json.dumps(data))


def edit(path, old, new, rehash=True):
    """Change the draft and, by default, refresh its humanizer receipt so only the intended gate moves."""
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    if rehash:
        receipt(path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def draft(ws, kind="article", slug="good-article", path=None):
    path = path or (article(ws) if kind == "article" else gbp(ws))
    return check.Draft.load(ws, path, kind, slug=slug if kind == "article" else "")


def gate(ws, name, d=None):
    return check.run(d or draft(ws), ws, only=name)[0]


def edit_json(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------- good drafts

def test_good_article_passes_every_gate(fx):
    gates = check.run(draft(fx), fx)
    assert [g.name for g in gates] == check.GATE_NAMES
    assert {g.name: g.status for g in gates} == {g.name: "pass" for g in gates}, [(g.name, g.detail) for g in gates]


def test_good_gbp_passes_and_skips_article_only_gates(fx):
    gates = check.run(draft(fx, kind="gbp"), fx)
    names = [g.name for g in gates]
    assert "sections" not in names and "faq" not in names
    assert {g.status for g in gates} == {"pass"}, [(g.name, g.detail) for g in gates]


# ---------------------------------------------------------------- humanizer

def test_stale_hash_fails(fx):
    edit(article(fx), "slow bump", "gentle bump", rehash=False)
    g = gate(fx, "humanizer")
    assert g.status == "fail" and "changed" in g.detail


@pytest.mark.parametrize("change", [{"rewritten": False}, {"after": 4}, {"humanizer_brief": "fallback"}])
def test_weak_receipts_fail(fx, change):
    receipt(article(fx), **change)
    assert gate(fx, "humanizer").status == "fail"


def test_missing_receipt_fails(fx):
    article(fx).with_name("good_article.md.deslop.json").unlink()
    assert gate(fx, "humanizer").status == "fail"


def test_unreadable_receipt_is_error(fx):
    article(fx).with_name("good_article.md.deslop.json").write_text("{not json")
    assert gate(fx, "humanizer").status == "error"


# ---------------------------------------------------------------- wording: the rule comes from customer.json

def test_banned_wording_fails_with_line(fx):
    edit(article(fx), "Request a complimentary estimate", "Request a free estimate")
    g = gate(fx, "wording")
    assert (g.status, g.line) == ("fail", 16)
    assert FX["content"]["wording"]["always"][FX["content"]["wording"]["never"][0]] in g.detail


def test_wording_rule_is_the_workspaces_own(root, monkeypatch):
    """fy's rule bans other words: fx's banned phrase passes in fy, fy's banned word fails."""
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    fy = workspace.open("fy", keychain=FakeKeychain())
    note = fy.path("drafts", "note.txt")
    note.parent.mkdir(exist_ok=True)
    note.write_text("Book a free estimate today.\n")
    assert check.run(check.Draft.load(fy, note, "gbp"), fy, only="wording")[0].status == "pass"
    note.write_text("Our cleanings are painless.\n")
    assert check.run(check.Draft.load(fy, note, "gbp"), fy, only="wording")[0].status == "fail"


# ---------------------------------------------------------------- duplicates

def test_same_topic_in_window_fails(fx):
    edit_json(fx.file("content_log"), lambda log: log["articles"].append(
        {"type": "article", "slug": "older", "title": "Where to Get Your Car Fixed in Testville", "date": "2026-10-20",
         "status": "draft"}))
    g = gate(fx, "duplicates")
    assert (g.status, g.blocking) == ("fail", True)


def test_duplicate_warning_blocks(fx):
    edit_json(fx.file("content_log"), lambda log: log["articles"].append(
        {"type": "article", "slug": "older", "title": "Where to Get Your Car Fixed in Testville", "date": "2026-01-20",
         "status": "draft"}))
    g = gate(fx, "duplicates")
    assert (g.status, g.blocking) == ("warn", True)


def test_missing_log_is_error(fx):
    fx.file("content_log").unlink()
    assert gate(fx, "duplicates").status == "error"


# ---------------------------------------------------------------- rules

def test_gbp_phone_is_a_failure(fx):
    edit(gbp(fx), "on our website.", f"on our website or call {PHONE}.")
    assert gate(fx, "rules", draft(fx, kind="gbp")).status == "fail"


def test_tone_warning_does_not_block(fx):
    edit(gbp(fx), "quick checklist", "quick checklist (other shops will rip you off)")
    g = gate(fx, "rules", draft(fx, kind="gbp"))
    assert (g.status, g.blocking) == ("warn", False)


@pytest.mark.parametrize("name", ["rules", "claims"])
def test_missing_facts_is_error(fx, name):
    fx.file("facts").unlink()
    assert gate(fx, name).status == "error"


# ---------------------------------------------------------------- claims

def test_unverified_claim_fails(fx):
    edit(article(fx), "Nobody is hurt", "Repairs take 12 days. Nobody is hurt")
    g = gate(fx, "claims")
    assert (g.status, g.line) == ("fail", 10) and "12 days" in g.detail


def test_denylisted_claim_fails_even_when_listed_as_fact(fx):
    banned = FX["content"]["denylist_claims"][1]
    edit_json(fx.file("facts"), lambda f: f["claims"].append(banned))
    edit(article(fx), "The choice is yours.", f"The choice is yours, and we lend a {banned}.")
    assert banned in gate(fx, "claims").detail


def test_negated_denylist_phrase_is_allowed(fx):
    banned = FX["content"]["denylist_claims"][1]
    edit(article(fx), "The choice is yours.", f"The choice is yours. We do not offer a {banned}.")
    assert gate(fx, "claims").status == "pass"


def test_wrong_phone_fails(fx):
    wrong = PHONE[:-1] + ("9" if PHONE[-1] != "9" else "8")
    edit(article(fx), PHONE, wrong)
    assert wrong[-4:] in gate(fx, "claims").detail


def test_wrong_address_fails(fx):
    number = FX["locations"][0]["address"].split()[0]
    edit(article(fx), f"{number} Oak Road", f"{int(number) + 2} Oak Road")
    assert gate(fx, "claims").status == "fail"


def test_wrong_hours_fail(fx):
    edit(article(fx), "8am–5pm", "7am–6pm")
    assert gate(fx, "claims").status == "fail"


def test_missing_business_block_is_error(fx):
    edit_json(fx.file("facts"), lambda f: f.pop("business"))
    assert gate(fx, "claims").status == "error"


# ---------------------------------------------------------------- links: the workspace's own site

def test_relative_and_own_site_links_are_checked(fx, monkeypatch):
    seen = []
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: seen.append(u) or {"url": u, **OK})
    assert gate(fx, "links").status == "pass"
    assert seen == [f"{SITE}/guide/", f"{SITE}/tire-rotation-basics/", f"{SITE}/estimate/", f"{SITE}/about/", f"{SITE}/services/"]


def test_404_fails(fx, monkeypatch):
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, "status": 404, "ok": False})
    g = gate(fx, "links")
    assert g.status == "fail" and "404" in g.detail


def test_no_network_is_error(fx, monkeypatch):
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, "status": None, "ok": False, "error": "offline"})
    assert gate(fx, "links").status == "error"


# ---------------------------------------------------------------- sections

def test_missing_marker_fails(fx):
    edit(article(fx), "<!-- block: decisions -->\n", "")
    g = gate(fx, "sections")
    assert g.status == "fail" and "marker" in g.detail


def test_swapped_order_fails(fx):
    p = article(fx)
    p.write_text(p.read_text().replace("block: decisions", "block: TMP").replace("block: checklist", "block: decisions")
                 .replace("block: TMP", "block: checklist"))
    assert gate(fx, "sections").status == "fail"


def test_unknown_block_name_fails(fx):
    edit(article(fx), "block: checklist", "block: listicle")
    assert "listicle" in gate(fx, "sections").detail


def test_no_log_entry_warns_without_blocking(fx):
    g = gate(fx, "sections", draft(fx, slug="never-logged"))
    assert (g.status, g.blocking) == ("warn", False)


def test_strict_run_blocks_on_missing_section_log(fx):
    d = draft(fx, slug="never-logged")
    d.strict = True
    g = check.run(d, fx, only="sections")[0]
    assert (g.status, g.blocking) == ("fail", True)


# ---------------------------------------------------------------- faq

def test_extra_schema_question_fails(fx):
    edit(article(fx), '"mainEntity": [', '"mainEntity": [\n  {"@type": "Question", "name": "Is it open late?", '
                                         '"acceptedAnswer": {"@type": "Answer", "text": "No."}},')
    assert "Is it open late?" in gate(fx, "faq").detail


def test_visible_question_missing_from_schema_fails(fx):
    edit(article(fx), "### Do I need an appointment?", "### Can I wait while you look?\n\nYes.\n\n### Do I need an appointment?")
    assert "Can I wait while you look?" in gate(fx, "faq").detail


def test_changed_answer_fails_and_says_how_to_fix_it(fx):
    edit(article(fx), "You can pick any licensed shop.\n", "You can pick any shop.\n")
    g = gate(fx, "faq")
    assert g.status == "fail" and "answer" in g.detail and "delete the FAQPage comment" in g.detail


def test_no_schema_comment_passes_because_the_schema_is_built_from_the_page(fx):
    p = article(fx)
    text = p.read_text()
    p.write_text(text[:text.index("<!-- FAQPage")])
    g = gate(fx, "faq")
    assert g.status == "pass" and "built from" in g.detail


def test_no_visible_faq_fails(fx):
    p = article(fx)
    p.write_text(p.read_text().replace("## Frequently asked questions", "## Questions people ask"))
    assert gate(fx, "faq").status == "fail"


# ---------------------------------------------------------------- research

def test_expired_file_fails(fx, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: dt.date(2026, 11, 9))
    g = gate(fx, "research")
    assert g.status == "fail" and "expire" in g.detail


def test_event_date_not_in_file_fails(fx):
    article(fx).with_name("good_article.md.research.json").write_text(
        json.dumps({"file": "research/local/2026-W45.md", "used": "fair", "event_dates": ["2026-11-16"]}))
    assert "2026-11-16" in gate(fx, "research").detail


def test_missing_sidecar_fails(fx):
    article(fx).with_name("good_article.md.research.json").unlink()
    assert gate(fx, "research").status == "fail"


def test_missing_research_file_fails(fx):
    fx.path("research", "local", "2026-W45.md").unlink()
    assert gate(fx, "research").status == "fail"


def test_research_file_outside_the_workspace_is_error(fx):
    article(fx).with_name("good_article.md.research.json").write_text(
        json.dumps({"file": "../fy/research/local/2026-W45.md", "used": "fair", "event_dates": []}))
    assert gate(fx, "research").status == "error"


# ---------------------------------------------------------------- whole run

def run_cli(ws, *extra, path=None):
    return cli.main(["content", "check", str(path or article(ws)), "--workspace", "fx", "--kind", "article",
                     "--slug", "good-article", *extra], keychain=FakeKeychain())


def result_file(ws):
    return article(ws).with_name("good_article.md.check.json")


def test_pass_writes_result_and_exits_0(fx):
    assert run_cli(fx) == EXIT_OK
    data = json.loads(result_file(fx).read_text())
    assert data["result"] == "pass" and data["workspace"] == "fx"
    assert data["sha256"] == hashlib.sha256(article(fx).read_bytes()).hexdigest()
    assert set(data["inputs"]) == {"facts.json", "RULES.md", "customer.json", "log_entry", "research_sidecar",
                                   "research_file", "deslop_receipt"}
    assert set(check.TOOL_FILES) <= set(data["tools"]) and all(data["tools"][t] for t in check.TOOL_FILES)
    assert (data["kind"], data["slug"]) == ("article", "good-article")


def test_gate_that_does_not_apply_is_an_error(fx):
    assert cli.main(["content", "check", str(gbp(fx)), "--workspace", "fx", "--kind", "gbp", "--gate", "faq"],
                    keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN


def test_checks_and_hashes_the_bytes_it_read(fx):
    d = draft(fx)
    loaded = hashlib.sha256(article(fx).read_bytes()).hexdigest()
    article(fx).write_text(article(fx).read_text() + "\nA free estimate, always.\n")  # changed after load
    gates = check.run(d, fx)
    payload = check.payload_for(d, fx, gates)
    assert payload["sha256"] == loaded
    by = {g.name: g.status for g in gates}
    assert by["wording"] == "pass" and by["humanizer"] == "pass"


def test_one_gate_raising_does_not_stop_the_others(fx, monkeypatch):
    def boom(d, ws):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(check, "GATES", [(n, boom if n == "wording" else f, k) for n, f, k in check.GATES])
    by = {g.name: g for g in check.run(draft(fx), fx)}
    assert by["wording"].status == "error" and "tool exploded" in by["wording"].detail
    assert by["faq"].status == "pass" and len(by) == len(check.GATES)


def test_fail_plus_error_exits_3(fx):
    edit(article(fx), "Request a complimentary estimate", "Request a free estimate")  # wording fail
    fx.file("facts").unlink()                                                        # claims error
    assert run_cli(fx) == EXIT_COULD_NOT_RUN
    assert json.loads(result_file(fx).read_text())["result"] == "error"


def test_fail_exits_1(fx):
    edit(article(fx), "Request a complimentary estimate", "Request a free estimate")
    assert run_cli(fx) == EXIT_FAIL


def test_non_blocking_lint_warning_still_passes(fx):
    edit(article(fx), "Nobody is hurt", "Some shops will rip you off. Nobody is hurt")
    assert run_cli(fx) == EXIT_OK
    gates = {g["name"]: g for g in json.loads(result_file(fx).read_text())["gates"]}
    assert (gates["rules"]["status"], gates["rules"]["blocking"]) == ("warn", False)


def test_gate_option_writes_no_file(fx):
    assert run_cli(fx, "--gate", "wording") == EXIT_OK
    assert not result_file(fx).exists()


def test_result_is_replaced_never_half_written(fx, monkeypatch):
    assert run_cli(fx) == EXIT_OK
    before = result_file(fx).read_text()
    edit(article(fx), "Request a complimentary estimate", "Request a free estimate")

    def full(*a):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", full)
    assert run_cli(fx) == EXIT_COULD_NOT_RUN  # the CLI turns the crash into exit 3
    assert result_file(fx).read_text() == before
    assert not result_file(fx).with_name("good_article.md.check.json.tmp").exists()


def test_missing_draft_exits_3(fx):
    assert run_cli(fx, path=fx.path("drafts", "nope.md")) == EXIT_COULD_NOT_RUN


def test_draft_outside_the_workspace_exits_3(fx, root):
    other = root / "fy" / "RULES.md"
    assert run_cli(fx, path=other) == EXIT_COULD_NOT_RUN


def test_text_output_ends_with_the_result_line(fx, capsys):
    run_cli(fx)
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("result: pass")


def test_fy_gbp_passes_with_fys_own_rules(root, monkeypatch):
    """The same gates, run on the dental workspace: its wording, facts, log and site, nothing from fx."""
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, **OK})
    fy = workspace.open("fy", keychain=FakeKeychain())
    gates = check.run(check.Draft.load(fy, fy.path("drafts", "good_gbp.txt"), "gbp"), fy)
    assert {g.status for g in gates} == {"pass"}, [(g.name, g.detail) for g in gates]


def test_sidecars_symlinked_outside_workspace_are_refused(fx, root):
    draft_path = fx.path("drafts", "good_article.md")
    outside_secret = root / "fy" / "customer.json"

    res_sidecar = draft_path.with_name(draft_path.name + ".research.json")
    des_sidecar = draft_path.with_name(draft_path.name + ".deslop.json")

    if res_sidecar.exists() or res_sidecar.is_symlink():
        res_sidecar.unlink()
    if des_sidecar.exists() or des_sidecar.is_symlink():
        des_sidecar.unlink()

    res_sidecar.symlink_to(outside_secret)
    des_sidecar.symlink_to(outside_secret)

    assert check._research_path(draft_path, fx) is None
    hashes = check.input_hashes(fx, draft_path)
    assert hashes["research_sidecar"] is None
    assert hashes["deslop_receipt"] is None
