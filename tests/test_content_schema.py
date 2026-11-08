"""md.convert with options(ws): Article (author Person from facts, publisher Organization from customer.json) and
BreadcrumbList ship next to the FAQPage, each in its own script tag; without options only the FAQ ships."""

from __future__ import annotations

import json
import re

import pytest

from mseo import workspace
from mseo.content import md
from tests.conftest import FX, FY
from tests.fakes import FakeKeychain

TODAY = "2026-11-04"
SCRIPT_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def blocks(html: str) -> list[dict]:
    return [json.loads(b.replace("<\\/", "</")) for b in SCRIPT_RE.findall(html)]


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    return workspace.open("fx", keychain=FakeKeychain())


def facts(ws):
    return json.loads(ws.file("facts").read_text())


def article_text(ws):
    return ws.path("drafts", "good_article.md").read_text()


def test_rendered_html_carries_exactly_three_jsonld_blocks_with_valid_types(fx):
    doc = md.convert(article_text(fx), **md.options(fx, slug="good-article"))
    found = blocks(doc.html)
    assert [b["@type"] for b in found] == ["Article", "BreadcrumbList", "FAQPage"]
    assert all(b["@context"] == "https://schema.org" for b in found)
    assert doc.html.count("</script>") == 3 and len(doc.schemas) == 3
    assert doc.schema["@type"] == "FAQPage"  # the old field still names the FAQ block


def test_article_names_the_facts_author_and_the_configured_business(fx):
    article = blocks(md.convert(article_text(fx), **md.options(fx, slug="good-article")).html)[0]
    author = facts(fx)["author"]
    assert article["headline"] == md.convert(article_text(fx)).title
    assert article["author"] == {"@type": "Person", "name": author["name"], "url": author["url"]}
    assert article["publisher"] == {"@type": "Organization", "name": FX["business"]["name"], "url": FX["business"]["site"] + "/"}
    assert article["datePublished"] == TODAY and article["dateModified"] == TODAY
    assert article["mainEntityOfPage"] == f"{FX['business']['site']}/good-article/"
    assert "description" not in article  # fx's draft has no Meta Description comment


def test_dates_and_image_come_from_the_options(fx):
    opts = md.options(fx, slug="x", date="2026-10-01", modified="2026-11-01", image="https://img.test/a.jpg")
    article = blocks(md.convert(article_text(fx), **opts).html)[0]
    assert (article["datePublished"], article["dateModified"], article["image"]) == ("2026-10-01", "2026-11-01", "https://img.test/a.jpg")


def test_breadcrumb_runs_site_blog_post(fx):
    crumbs = blocks(md.convert(article_text(fx), **md.options(fx, slug="good-article")).html)[1]["itemListElement"]
    site = FX["business"]["site"]
    assert [c["position"] for c in crumbs] == [1, 2, 3]
    assert crumbs[0] == {"@type": "ListItem", "position": 1, "name": FX["business"]["name"], "item": site + "/"}
    assert crumbs[1]["item"] == site + "/blog/"
    assert crumbs[2]["item"] == site + "/good-article/" and crumbs[2]["name"] == md.convert(article_text(fx)).title


def test_without_a_slug_the_post_crumb_has_a_name_only(fx):
    crumbs = blocks(md.convert(article_text(fx), **md.options(fx)).html)[1]["itemListElement"]
    assert "item" not in crumbs[2] and crumbs[2]["name"]


def test_without_options_only_the_faq_ships(fx):
    doc = md.convert(article_text(fx))
    assert [b["@type"] for b in blocks(doc.html)] == ["FAQPage"] and doc.schemas == (doc.schema,)


def test_options_keep_the_faq_headings(fx):
    assert md.options(fx)["faq_headings"] == md.faq_headings(fx)


def test_missing_author_drops_the_person_but_keeps_the_article(fx):
    data = facts(fx)
    data.pop("author")
    fx.file("facts").write_text(json.dumps(data))
    article = blocks(md.convert(article_text(fx), **md.options(fx)).html)[0]
    assert article["@type"] == "Article" and "author" not in article and article["publisher"]["name"] == FX["business"]["name"]


def test_fy_schema_uses_fys_author_and_site(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    fy = workspace.open("fy", keychain=FakeKeychain())
    article, crumbs, faq = blocks(md.convert(article_text(fy), **md.options(fy, slug="chipped-tooth")).html)
    assert article["author"]["name"] == facts(fy)["author"]["name"] and FX["business"]["name"] not in json.dumps(article)
    assert article["publisher"]["url"] == FY["business"]["site"] + "/"
    assert crumbs["itemListElement"][2]["item"] == FY["business"]["site"] + "/chipped-tooth/"
    assert faq["@type"] == "FAQPage"
    assert article["description"] == md.convert(article_text(fy)).excerpt  # fy's draft carries a Meta Description


def test_new_config_keys_get_their_defaults(fx, root):
    assert fx.config["business"]["country"] == "US"
    paths = fx.config["content"]["link_paths"]
    assert {k: list(v) if isinstance(v, tuple) else v for k, v in paths.items()} == {
        "service": ["/services/"], "contact": ["/contact/", "/estimate/", "/book/", "/appointment/"], "related_default": None}
    assert "city" not in fx.config["locations"][0]
    fy = workspace.open("fy", keychain=FakeKeychain())
    assert list(fy.config["content"]["link_paths"]["service"]) == FY["content"]["link_paths"]["service"]
    assert fy.config["content"]["link_paths"]["contact"][0] == "/contact/"  # the sibling key keeps its default
    from tests.conftest import edit_config
    with pytest.raises(workspace.ConfigInvalid, match="country"):
        edit_config(root, "fx", lambda c: c["business"].update(country="usa")); workspace.open("fx", keychain=FakeKeychain())
    edit_config(root, "fx", lambda c: c["business"].update(country="US"))
    with pytest.raises(workspace.ConfigInvalid, match="link_paths"):
        edit_config(root, "fx", lambda c: c["content"].update(link_paths={"service": ["services"]})); workspace.open("fx", keychain=FakeKeychain())


def test_jsonld_blocks_cannot_close_their_script_tag(fx):
    text = article_text(fx).replace("# Where to", "# </script><script>alert(1) Where to")
    html = md.convert(text, **md.options(fx)).html
    assert html.count("</script>") == 3
