"""`mseo content new`: the template for the recipe's shape, every {{token}} filled from the workspace, markers kept,
the recipe logged. Exit 2 when the draft exists, 3 on an unknown token or a missing fact."""

from __future__ import annotations

import json
import re

import pytest

from mseo import cli, workspace
from mseo.content import new as new_mod
from mseo.content import recipes
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_OK
from tests.conftest import FX, FY
from tests.fakes import FakeKeychain

TODAY = "2026-11-04"  # ISO week 2026-W45: the fixtures' research file


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    return workspace.open("fx", keychain=FakeKeychain())


def run(*argv):
    return cli.main(list(argv), keychain=FakeKeychain())


def new(slug, *extra, ws="fx", kind="article", service="synthetic service", area="Testville"):
    return run("content", "new", "--workspace", ws, "--kind", kind, "--slug", slug, "--service", service, "--area", area, *extra)


def facts(ws):
    return json.loads(ws.file("facts").read_text())


def read_log(ws):
    return json.loads(ws.file("content_log").read_text())


def test_article_draft_is_the_shape_template_with_tokens_filled(fx, capsys):
    assert new("first-post", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    draft = fx.path("drafts", "first-post.md")
    text = draft.read_text()
    assert out["recipe"]["shape"] in recipes.shapes() and f"Template: {out['recipe']['shape']}" in text
    assert "{{" not in text and "###WRITE###" in text
    author = facts(fx)["author"]
    assert author["name"] in text and author["role"] in text and author["url"] in text
    assert FX["business"]["name"] in text and FX["locations"][0]["footer"] in text
    assert TODAY in text and out["tokens"]["research.file"] == "research/local/2026-W45.md"
    assert "Harvest Fair parking-lot dings" in text  # research.used from the Decision line
    markers = re.findall(r"<!-- block: ([\w-]+) -->", text)
    assert set(out["recipe"]["blocks"]) <= set(markers)


def test_sidecar_skeleton_and_log_entry_are_written(fx):
    assert new("first-post") == EXIT_OK
    side = json.loads(fx.path("drafts", "first-post.md.research.json").read_text())
    assert side == {"file": "research/local/2026-W45.md", "used": "Harvest Fair parking-lot dings", "event_dates": []}
    entry = next(a for a in read_log(fx)["articles"] if a["slug"] == "first-post")
    assert entry["status"] == "draft" and entry["date"] == TODAY and entry["title"] == "synthetic service in Testville"
    assert entry["blocks"] == recipes.shapes()[entry["shape"]]
    for axis in recipes.ARTICLE_AXES:
        assert axis in entry


def test_faq_block_gets_the_recipes_question_count(fx, capsys):
    assert new("first-post", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    text = fx.path("drafts", "first-post.md").read_text()
    faq = text.split("<!-- block: faq -->", 1)[1].split("<!-- block:", 1)[0]
    assert faq.count("### ###WRITE###") == out["recipe"]["faq_n"]


def test_secondary_cta_follows_the_recipe(fx, capsys):
    seen = set()
    for n in range(4):
        assert new(f"post-{n}", "--json") == EXIT_OK
        out = json.loads(capsys.readouterr().out)
        text = fx.path("drafts", f"post-{n}.md").read_text()
        markers = re.findall(r"<!-- block: ([\w-]+) -->", text)
        seen.add(out["recipe"]["cta"])
        assert markers.count("cta") == (1 if out["recipe"]["cta"] == "end" else 2)
        if out["recipe"]["cta"] == "after-answer-and-end":
            assert markers[:2] == ["answer", "cta"]
        if out["recipe"]["cta"] == "mid-and-end":
            assert markers.index("cta") < markers.index("faq") and markers[1] != "cta"
    assert len(seen) == 3


def test_links_default_to_service_contact_and_the_newest_post(fx):
    assert new("first-post") == EXIT_OK
    text = fx.path("drafts", "first-post.md").read_text()
    posts = json.loads(fx.file("wp_posts").read_text())
    service, contact = fx.config["content"]["link_paths"]["service"][0], fx.config["content"]["link_paths"]["contact"][0]
    assert f"({service})" in text and f"({contact})" in text and f"({FX['business']['site']}/{posts[0]['slug']}/)" in text


def test_link_paths_come_from_the_workspace_config(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    fy = workspace.open("fy", keychain=FakeKeychain())
    assert new("tooth-post", ws="fy") == EXIT_OK
    text = fy.path("drafts", "tooth-post.md").read_text()
    service = FY["content"]["link_paths"]["service"][0]
    assert f"({service})" in text and "(/services/)" not in text


def test_related_default_from_config_when_no_post_is_listed(fx, root):
    fx.file("wp_posts").write_text("[]")
    assert new("first-post") == EXIT_COULD_NOT_RUN
    from tests.conftest import edit_config
    edit_config(root, "fx", lambda c: c["content"].setdefault("link_paths", {}).update(related_default="/blog/"))
    assert new("second-post") == EXIT_OK
    assert "(/blog/)" in workspace.open("fx", keychain=FakeKeychain()).path("drafts", "second-post.md").read_text()


def test_area_defaults_to_the_first_locations_city(fx, capsys):
    from mseo.content import places
    assert run("content", "new", "--workspace", "fx", "--kind", "article", "--slug", "no-area", "--service", "synthetic service", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["tokens"]["area"] == places.city_of_address(FX["locations"][0]["address"])


def test_a_bad_slug_is_3_before_any_file_is_written(fx, capsys):
    for slug in ("../escape", "Bad_Slug", "x"):
        assert new(slug) == EXIT_COULD_NOT_RUN
        assert "slug" in capsys.readouterr().err
    assert not list(fx.path("drafts").glob("*escape*")) and not fx.path("drafts", "Bad_Slug.md").exists()
    assert not any(a.get("slug") in ("../escape", "Bad_Slug", "x") for a in read_log(fx)["articles"])


def test_link_and_used_flags_win(fx):
    assert new("first-post", "--service-url", "/fix/", "--contact-url", "/talk/", "--related-url", "/other/", "--used", "the fair") == EXIT_OK
    text = fx.path("drafts", "first-post.md").read_text()
    assert "(/fix/)" in text and "(/talk/)" in text and "(/other/)" in text and "(the fair)" in text


def test_forced_recipe_picks_that_template(fx):
    assert new("first-post", "--recipe", "compare-table") == EXIT_OK
    assert "Template: compare-table" in fx.path("drafts", "first-post.md").read_text()


def test_existing_draft_is_exit_2_and_untouched(fx):
    assert new("first-post") == EXIT_OK
    before = fx.path("drafts", "first-post.md").read_text()
    assert new("first-post") == EXIT_EXISTS
    assert fx.path("drafts", "first-post.md").read_text() == before


def test_missing_author_fact_is_3_and_names_it(fx, capsys):
    data = facts(fx)
    data["author"].pop("url")
    fx.file("facts").write_text(json.dumps(data))
    assert new("first-post") == EXIT_COULD_NOT_RUN
    assert "author.url" in capsys.readouterr().err
    assert not fx.path("drafts", "first-post.md").exists()


def test_missing_research_file_is_3(fx, capsys):
    assert new("first-post", "--week", "2026-W40") == EXIT_COULD_NOT_RUN
    assert "research/local/2026-W40.md" in capsys.readouterr().err


def test_unknown_token_is_3_and_named():
    with pytest.raises(new_mod.NewError, match=r"unknown token \{\{nope\}\}"):
        new_mod.fill("# {{service}} and {{nope}}", {"service": "x"})


def test_decision_line_is_split_by_kind():
    text = "## Decision\n\n- Article: the fair. Business Profile post: the rain checklist.\n"
    assert new_mod.decision_used(text, "article") == "the fair"
    assert new_mod.decision_used(text, "gbp") == "the rain checklist"
    assert new_mod.decision_used("# no decision\n", "article") == ""


def test_gbp_draft_needs_neither_author_nor_a_related_post(fx):
    data = facts(fx)
    data.pop("author")
    fx.file("facts").write_text(json.dumps(data))
    fx.file("wp_posts").write_text("[]")
    assert new("rain-post", kind="gbp") == EXIT_OK
    text = fx.path("drafts", "rain-post.txt").read_text()
    assert "{{" not in text
    assert new("an-article") == EXIT_COULD_NOT_RUN  # the article template still needs the author block


def test_gbp_draft_fills_the_axes_and_logs_the_type(fx, capsys):
    assert new("rain-post", "--json", kind="gbp") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    text = fx.path("drafts", "rain-post.txt").read_text()
    assert f"template: {out['recipe']['gbp_type']}" in text
    assert f"Opening pattern: {out['recipe']['opening']}" in text and f"Length band: {out['recipe']['length_band']}" in text
    assert "{{" not in text and "###WRITE###" in text and "synthetic service" in text
    entry = next(g for g in read_log(fx)["gbp"] if g.get("slug") == "rain-post")
    assert entry["gbp_type"] == out["recipe"]["gbp_type"] and entry["theme"] == "synthetic service" and entry["status"] == "scheduled"


def test_fy_draft_uses_fys_facts(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    fy = workspace.open("fy", keychain=FakeKeychain())
    assert new("tooth-post", ws="fy", area="Sampleton") == EXIT_OK
    text = fy.path("drafts", "tooth-post.md").read_text()
    assert facts(fy)["author"]["name"] in text and FY["locations"][0]["footer"] in text
    assert FX["business"]["name"] not in text and "chipped tooth first aid" in text
