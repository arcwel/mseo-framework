"""The eeat gate (articles only): author from facts.json with a bio link, a primary source off the site, an image or
a reason for none, and a review date under 400 days old. Runs on a fresh copy of fx (and fy once)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import FX

from mseo import workspace
from mseo.content import check
from tests.fakes import FakeKeychain

TODAY = dt.date(2026, 11, 4)


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, "status": 200, "ok": True})
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    return workspace.open("fx", keychain=FakeKeychain())


def article(ws):
    return ws.path("drafts", "good_article.md")


def facts(ws):
    return json.loads(ws.file("facts").read_text())


def edit(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def gate(ws, kind="article", path=None):
    d = check.Draft.load(ws, path or article(ws), kind, slug="good-article")
    return check.run(d, ws, only="eeat")[0]


def test_good_article_passes_and_names_the_author(fx):
    g = gate(fx)
    assert (g.status, g.blocking) == ("pass", True)
    assert facts(fx)["author"]["name"] in g.detail


def test_eeat_is_article_only(fx):
    assert "eeat" not in check.gates_for("gbp") and "eeat" in check.gates_for("article")


def test_missing_author_block_fails(fx):
    edit(article(fx), "<!-- block: author -->", "<!-- block: why -->")
    g = gate(fx)
    assert g.status == "fail" and "author" in g.detail


def test_author_must_be_the_facts_author(fx):
    name = facts(fx)["author"]["name"]
    article(fx).write_text(article(fx).read_text().replace(name, "Someone Else"))
    g = gate(fx)
    assert g.status == "fail" and name in g.detail


def test_author_needs_the_bio_link(fx):
    name = facts(fx)["author"]["name"]
    edit(article(fx), f"[About {name}](/about/)", f"About {name}")
    g = gate(fx)
    assert g.status == "fail" and facts(fx)["author"]["url"] in g.detail


def test_absolute_bio_link_is_accepted(fx):
    edit(article(fx), "(/about/)", f"({facts(fx)['author']['url']})")
    assert gate(fx).status == "pass"


def test_a_source_on_the_business_site_is_not_primary(fx):
    edit(article(fx), "https://testville.example.gov/fair", f"{FX['business']['site']}/fair")
    g = gate(fx)
    assert g.status == "fail" and "sources" in g.detail


def test_a_source_url_only_in_the_research_sidecar_does_not_count(fx):
    """README item 10: the source is cited in the sources block; the sidecar naming a URL is not a citation."""
    edit(article(fx), "(https://testville.example.gov/fair)", "")
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data["used"] = "Harvest Fair https://testville.example.gov/fair"
    side.write_text(json.dumps(data))
    g = gate(fx)
    assert g.status == "fail" and "sources" in g.detail


def test_the_used_item_must_appear_in_the_sources_block(fx):
    edit(article(fx), "- Testville Harvest Fair listing, County Fairgrounds (https://testville.example.gov/fair)",
         "- State repair rules (https://state.example.gov/rules)")
    g = gate(fx)
    assert g.status == "fail" and "used item" in g.detail
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data["used"] = "State repair rules https://state.example.gov/rules"
    side.write_text(json.dumps(data))
    assert gate(fx).status == "pass"


def test_own_site_source_counts_only_when_the_sidecar_marks_it_own_confirmed(fx):
    own = f"{FX['business']['site']}/fair"
    edit(article(fx), "https://testville.example.gov/fair", own)
    assert gate(fx).status == "fail"
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data["source_kind"] = "own-confirmed"
    side.write_text(json.dumps(data))
    g = gate(fx)
    assert g.status == "pass" and "1 primary source" in g.detail


def test_no_image_needs_a_reason_in_the_sidecar(fx):
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data.pop("image_reason")
    side.write_text(json.dumps(data))
    g = gate(fx)
    assert g.status == "fail" and "image" in g.detail
    data.pop("image")
    side.write_text(json.dumps(data))
    assert gate(fx).status == "fail"


def test_image_markdown_or_a_labelled_illustration_satisfies_the_rule(fx):
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data.pop("image"), data.pop("image_reason")
    side.write_text(json.dumps(data))
    edit(article(fx), "Nobody is hurt", "![A scuffed bumper in the fair lot](/img/bumper.jpg) Nobody is hurt")
    assert gate(fx).status == "pass"
    edit(article(fx), "![A scuffed bumper in the fair lot](/img/bumper.jpg) ", "![](/img/bumper.jpg) ")
    assert gate(fx).status == "fail"  # an image without alt text is not evidence a reader or a crawler can use
    edit(article(fx), "![](/img/bumper.jpg) ", "")
    edit(article(fx), "Nobody is hurt", "Nobody is hurt. Illustration: the fair-lot parking plan.")
    assert gate(fx).status == "fail"  # the word mid-sentence is not a caption
    edit(article(fx), "Nobody is hurt. Illustration: the fair-lot parking plan.", "Nobody is hurt.\n\nIllustration: the fair-lot parking plan.")
    assert gate(fx).status == "pass"
    edit(article(fx), "Illustration: the fair-lot parking plan.", "*Illustration: the fair-lot parking plan.*")
    assert gate(fx).status == "pass"


def test_old_review_date_fails(fx):
    edit(article(fx), "Reviewed 2026-11-02", "Reviewed 2025-09-01")
    g = gate(fx)
    assert g.status == "fail" and "400" in g.detail


def test_missing_review_date_fails(fx):
    edit(article(fx), "Reviewed 2026-11-02.", "")
    assert "date" in gate(fx).detail


def test_facts_without_author_is_an_error(fx):
    data = facts(fx)
    data.pop("author")
    fx.file("facts").write_text(json.dumps(data))
    assert gate(fx).status == "error"


def test_fy_article_passes_with_fys_author(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    fy = workspace.open("fy", keychain=FakeKeychain())
    d = check.Draft.load(fy, fy.path("drafts", "good_article.md"), "article", slug="chipped-tooth")
    g = check.run(d, fy, only="eeat")[0]
    assert g.status == "pass" and facts(fy)["author"]["name"] in g.detail and "illustration" in g.detail
