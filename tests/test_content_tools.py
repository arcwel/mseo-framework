"""The content modules under `mseo content`: md, links, claims, lint, wording, dupes, log (ported from the content
parts of the legacy test_jj_tools.py and test_jj_claims.py). Customer values come from FX/FY, never literals."""

from __future__ import annotations

import json

import pytest

from mseo import cli, fsutil, workspace
from mseo.content import claims, dupes, links, log as log_mod, md, shapes
from mseo.content.lint_copy import lint_text
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FX, FY
from tests.fakes import FakeKeychain

SITE = FX["business"]["site"]
NAME = FX["business"]["name"]
SCHEMA = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
    {"@type": "Question", "name": "Do you paint?", "acceptedAnswer": {"@type": "Answer", "text": "Yes."}}]}
MD = (f"<!-- internal only -->\n# Will Insurance Cover It?\n\n<!-- Meta Description: Short description for the excerpt. -->\n\n"
      f"**Short answer: yes, usually.**\n\n## Section & Heading\n\nSee [our guide]({SITE}/x/).\n\n"
      f"- first *point*\n- second point\n\n1. one\n2. two\n\n| a & b | c |\n|---|---|\n| a & b | c |\n\n---\n\n"
      f"**{NAME}**  \nline two\n\n<!-- FAQPage\n{json.dumps(SCHEMA)}\n-->\n")


def run(*argv):
    return cli.main(list(argv), keychain=FakeKeychain())


# ---------------------------------------------------------------- md

@pytest.fixture(scope="module")
def doc():
    return md.convert(MD)


def test_title_and_excerpt_lifted_from_source(doc):
    assert (doc.title, doc.excerpt) == ("Will Insurance Cover It?", "Short description for the excerpt.")


def test_no_html_comments_reach_output(doc):
    assert "<!--" not in doc.html and "internal only" not in doc.html


def test_block_markers_never_reach_html():
    out = md.convert(MD.replace("## Section & Heading", "<!-- block: decisions -->\n## Section & Heading")).html
    assert "block:" not in out and "<!--" not in out


def test_faq_becomes_real_jsonld_and_no_h1(doc):
    blob = doc.html.split('application/ld+json">')[1].split("</script>")[0]
    assert json.loads(blob)["@type"] == "FAQPage"
    assert "<h1>" not in doc.html


def test_blocks_and_inline(doc):
    h = doc.html
    assert "<h2>Section &amp; Heading</h2>" in h
    assert "<strong>Short answer: yes, usually.</strong>" in h
    assert f'<a href="{SITE}/x/">our guide</a>' in h
    assert "<ul><li>first <em>point</em></li><li>second point</li></ul>" in h
    assert "<ol><li>one</li><li>two</li></ol>" in h
    assert "<td>a &amp; b</td>" in h and "<hr>" in h


def test_placeholders_and_errors():
    assert "TODO" in md.convert("# T\n\nTODO write this\n").placeholders
    with pytest.raises(md.MarkdownError):
        md.convert("# T\n\n<!-- FAQPage {not json} -->\n")
    with pytest.raises(md.MarkdownError):
        md.convert("no title here\n")


def test_jsonld_cannot_close_its_script_tag():
    schema = dict(SCHEMA, mainEntity=[{"@type": "Question", "name": "Q?",
                                       "acceptedAnswer": {"@type": "Answer", "text": "x</script><script>alert(1)"}}])
    out = md.convert(f"# T\n\nbody\n\n<!-- FAQPage\n{json.dumps(schema)}\n-->\n").html
    assert out.count("</script>") == 1


def test_unsafe_link_schemes_render_as_text():
    out = md.convert("# T\n\n[bad](javascript:alert) [rel](/x/)\n").html
    assert "javascript:" not in out and '<a href="/x/">rel</a>' in out


def test_schema_is_built_from_the_visible_faq_and_wins_over_a_stale_comment():
    stale = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": "Open late?", "acceptedAnswer": {"@type": "Answer", "text": "Old answer."}}]}
    text = f"# T\n\n## Frequently asked questions\n\n### Open late?\n\nNo.\n\n<!-- FAQPage\n{json.dumps(stale)}\n-->\n"
    assert md.convert(text).schema["mainEntity"][0]["acceptedAnswer"]["text"] == "No."


# ---------------------------------------------------------------- links

def test_extract_urls_dedupes_trims_and_filters():
    text = "See https://a.test/x. and (https://a.test/x) then https://b.test/y, plus [link](https://c.test/z)"
    assert links.extract_urls(text) == ["https://a.test/x", "https://b.test/y", "https://c.test/z"]
    assert links.extract_urls('"@context": "https://schema.org"') == []
    host = SITE.split("//")[1]
    assert links.extract_urls(f"{SITE}/a/ https://www.{host}/b/ https://x.test/", host) == [f"{SITE}/a/", f"https://www.{host}/b/"]


def test_bad_url_is_reported_not_raised():
    r = links.check_url("https://exa mple.invalid/\x00", timeout=2)
    assert not r["ok"] and "error" in r


def test_check_url_fetches_only_http_and_https(tmp_path, monkeypatch):
    """Audit L2: file:// and ftp:// are refused before any request (a local file is never an existence oracle)."""
    local = tmp_path / "b.html"
    local.write_text("<p>secret</p>")
    monkeypatch.setattr(links.urllib.request, "urlopen", lambda *a, **k: pytest.fail("a request was made"))
    for url in (local.as_uri(), "ftp://example.test/x", "FILE:///etc/hosts", "javascript:alert(1)"):
        assert links.check_url(url) == {"url": url, "status": None, "ok": False, "error": "scheme not allowed"}
    assert links.READ_CAP <= 2 * 1024 * 1024


def test_comment_regexes_stay_linear_on_repeated_openers():
    """Audit L3: 80 KB of '<!--' must convert in well under a second (it took 4.5 s with a greedy-crossing body)."""
    import time

    from mseo.content import variation
    text = "# T\n\n" + "<!--" * 20000 + "\n"
    start = time.perf_counter()
    md.convert(text)
    md.faq_language(text)
    variation.strip_comments(text)
    assert time.perf_counter() - start < 1.0
    assert md.convert("# T\n\n<!-- a --> b <!-- c -->\n").html == "<p>b</p>"


# ---------------------------------------------------------------- claims

def test_numbers_guarantees_and_superlatives_are_flagged():
    """The generic patterns are code; the industry phrase ("lifetime warranty") is found only through the
    workspace's own content.claim_patterns (plan v2 R2)."""
    text = "We finish most repairs in 5-10 business days with a lifetime warranty. The best shop, 25 years strong."
    found = [c.lower() for c in claims.unverified_claims(text, [])]
    for expected in ("5-10 business days", "best", "25 years"):
        assert expected in found
    assert "lifetime warranty" not in found
    with_customer = [c.lower() for c in claims.unverified_claims(text, [], FX["content"]["claim_patterns"])]
    assert "lifetime warranty" in with_customer


def test_verified_and_covering_facts_pass():
    assert claims.unverified_claims("Repairs take 5-10 business days. Lifetime warranty included.",
                                    ["5-10 business days", "lifetime warranty"], FX["content"]["claim_patterns"]) == []
    assert claims.unverified_claims("Ready in 5 days.", ["ready in 5 days"]) == []
    assert claims.unverified_claims("Ready in 9 days.", ["ready in 5 days"])


def test_missing_or_broken_facts_file_raises(tmp_path):
    with pytest.raises(claims.FactsUnavailable):
        claims.load_facts(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(claims.FactsUnavailable):
        claims.load_facts(bad)
    good = tmp_path / "facts.json"
    good.write_text(json.dumps({"claims": ["  Lifetime Warranty ", {"claim": "5 days", "source": "site"}]}))
    assert claims.load_facts(good) == ["lifetime warranty", "5 days"]


def test_shop_mismatch_uses_the_workspaces_brand_terms(root):
    """A wrong phone next to fy's brand name is caught with fy's brand terms, and not with fx's."""
    facts = json.loads((root / "fy" / "facts.json").read_text())
    area = "212" if "212" not in FY["business"]["phone"] else "213"  # another area code: only the name links it
    text = f"{FY['business']['name']}: ({area}) 555-0100."
    assert claims.business_mismatches(text, facts, FY["business"]["brand_terms"])
    assert not claims.business_mismatches(text, facts, FX["business"]["brand_terms"])


# ---------------------------------------------------------------- lint and wording commands

def test_lint_text_checks_claims_only_with_facts():
    text = "Fast repairs, guaranteed."
    assert not [m for _, m in lint_text(text) if "unverified" in m]
    assert [m for _, m in lint_text(text, facts=[]) if "unverified" in m]


def test_lint_phone_rule_is_gbp_only(root):
    post = root / "fx" / "drafts" / "post.md"
    post.write_text(f"Collision repair.\n{FX['business']['phone']}\n")
    assert run("content", "lint", str(post), "--workspace", "fx") == EXIT_FAIL
    assert run("content", "lint", str(post), "--workspace", "fx", "--blog") == EXIT_OK


def test_lint_warns_but_does_not_fail_on_claims(root, capsys):
    post = root / "fx" / "drafts" / "post.md"
    post.write_text("We fix cars in 7 days, guaranteed.")
    assert run("content", "lint", str(post), "--workspace", "fx", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "warn" and any("unverified claim" in f["message"] for f in out["findings"])


def test_lint_without_facts_is_3(root):
    (root / "fx" / "facts.json").unlink()
    assert run("content", "lint", str(root / "fx" / "drafts" / "good_gbp.txt"), "--workspace", "fx") == EXIT_COULD_NOT_RUN


def test_wording_command_uses_each_workspaces_rule(root, capsys):
    for slug, cfg in (("fx", FX), ("fy", FY)):
        replacement = next(iter(cfg["content"]["wording"]["always"].values()))
        note = root / slug / "drafts" / "w.txt"
        note.parent.mkdir(exist_ok=True)
        note.write_text("Book a free estimate.\nOur cleanings are painless.\n")
        assert run("content", "wording", str(note), "--workspace", slug, "--json") == EXIT_FAIL
        found = json.loads(capsys.readouterr().out)["findings"]
        assert [f["use"] for f in found] == [replacement]


def test_a_file_in_another_workspace_is_refused(root):
    assert run("content", "wording", str(root / "fy" / "RULES.md"), "--workspace", "fx") == EXIT_COULD_NOT_RUN


# ---------------------------------------------------------------- dupes

def dupes_cli(root, slug, kind, rel, *extra):
    return run("content", "dupes", kind, str(root / slug / rel), "--workspace", slug, "--date", "2026-11-04", "--json", *extra)


def test_dupes_passes_the_fixture_article(root):
    assert dupes_cli(root, "fx", "article", "drafts/good_article.md", "--slug", "good-article") == EXIT_OK


def test_dupes_near_duplicate_text_fails_at_any_age(root):
    posts = json.loads((root / "fx" / "data" / "wp_posts.json").read_text())
    draft = root / "fx" / "drafts" / "copy.md"
    draft.write_text(f"# A New Title\n\n{posts[0]['text']}\n")
    assert dupes_cli(root, "fx", "article", "drafts/copy.md") == EXIT_FAIL


def test_dupes_stale_posts_list_cannot_run(root, capsys):
    path = root / "fx" / "data" / "content_log.json"
    log = json.loads(path.read_text())
    log["articles"].append({"slug": "newer", "title": "x", "date": "2026-10-30", "status": "published"})
    path.write_text(json.dumps(log))
    assert dupes_cli(root, "fx", "article", "drafts/good_article.md") == EXIT_COULD_NOT_RUN
    assert "stale" in capsys.readouterr().err


def test_dupes_certification_warning_reads_the_config(root, capsys):
    post = root / "fx" / "drafts" / "cert.txt"
    post.write_text(f"We are {FX['business']['certifications'][0]}.\n")
    assert dupes_cli(root, "fx", "gbp", "drafts/cert.txt") == EXIT_OK  # warn: exit 0, status "warn"
    assert json.loads(capsys.readouterr().out)["findings"][0]["repeated_claims"] == [dupes.CERT_CLAIM]
    post.write_text(" ".join(FX["content"]["subject_brands"][:dupes.MIN_BRANDS]) + "\n")
    assert dupes_cli(root, "fx", "gbp", "drafts/cert.txt") == EXIT_OK  # warn: exit 0, status "warn"


def test_dupes_strict_fails_on_warnings(root):
    post = root / "fx" / "drafts" / "cert.txt"
    post.write_text(f"We are {FX['business']['certifications'][0]}.\n")
    assert dupes_cli(root, "fx", "gbp", "drafts/cert.txt", "--strict") == EXIT_FAIL


# ---------------------------------------------------------------- log

def read_log(root):
    return json.loads((root / "fx" / "data" / "content_log.json").read_text())


def test_log_article_new_entry_gets_defaults_and_blocks(root):
    assert run("content", "log", "article", "--workspace", "fx", "--slug", "new-post", "--title", "T",
               "--date", "2026-11-12", "--shape", "steps-first") == EXIT_OK
    entry = read_log(root)["articles"][-1]
    assert entry["blocks"] == shapes.SHAPES["steps-first"] and entry["status"] == "draft" and entry["type"] == "article"


def test_log_relog_updates_only_given_fields(root):
    before = next(a for a in read_log(root)["articles"] if a["slug"] == "good-article")
    assert run("content", "log", "article", "--workspace", "fx", "--slug", "good-article", "--status", "published",
               "--url", f"{SITE}/good-article/", "--wp-id", "7", "--check-sha", "abc") == EXIT_OK
    after = [a for a in read_log(root)["articles"] if a["slug"] == "good-article"]
    assert len(after) == 1
    assert after[0] == {**before, "status": "published", "url": f"{SITE}/good-article/", "wp_id": 7, "check_sha": "abc"}


def test_log_new_entry_without_required_fields_is_3(root):
    assert run("content", "log", "article", "--workspace", "fx", "--slug", "nope") == EXIT_COULD_NOT_RUN
    assert all(a["slug"] != "nope" for a in read_log(root)["articles"])


def test_log_gbp_keys_on_date_and_lowercased_headline(root):
    assert run("content", "log", "gbp", "--workspace", "fx", "--date", "2026-11-05",
               "--headline", "Rain Is On The Way In Testville So Here Is A Quick Checklist", "--status", "published") == EXIT_OK
    gbp = read_log(root)["gbp"]
    assert len(gbp) == 1 and gbp[0]["status"] == "published" and gbp[0]["theme"] == "rain checklist"


def test_upsert_entry_writes_atomically(root, monkeypatch):
    ws = workspace.open("fx", keychain=FakeKeychain())
    before = ws.file("content_log").read_text()

    def full(*a):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", full)
    with pytest.raises(OSError):
        log_mod.upsert_entry(ws, "article", {"slug": "x", "title": "T", "date": "2026-11-12", "shape": "steps-first"})
    assert ws.file("content_log").read_text() == before
