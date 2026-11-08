"""mseo wp and mseo publish (ported from the legacy test_jj_publish.py and the wp parts of test_jj_tools.py).

The fix under test: WordPress gets the workspace's cms.site and its own Keychain credential, never an env file or a
default site, and a credential for another site stops the command with exit 3 before any request.
WordPress and every URL fetch are faked: nothing leaves the machine.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from mseo import cli
from mseo import publish as publish_mod
from mseo.cms import wordpress
from mseo.content import check
from mseo.content import md
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_FAIL, EXIT_OK
from tests.conftest import FX, FY, SYNTHETIC, edit_config, seal_for_tests
from tests.fakes import FakeKeychain, FakeWordPress

FX_FACTS = json.loads((SYNTHETIC / "fx" / "facts.json").read_text())
SCRIPT_RE = __import__("re").compile(r'<script type="application/ld\+json">(.*?)</script>', __import__("re").S)

TODAY = dt.date(2026, 11, 4)
SITE = FX["cms"]["site"]
LIVE = f"{SITE}/good-article/"
CRED = {"site": SITE, "user": "editor", "app_password": "abcd efgh ijkl"}
APPROVED = ["--approved", "Operator 2026-11-05"]


def ok(url, timeout=20):
    return {"url": url, "status": 200, "ok": True}


@pytest.fixture
def env(root, monkeypatch, tmp_path):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", ok)
    monkeypatch.setattr(check, "deslop_version", lambda: "test")
    monkeypatch.setattr(publish_mod, "check_url", ok)
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    # a stale legacy-style env file pointing at another site: it must never be read
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wp = FakeWordPress(SITE).install(monkeypatch, wordpress)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): CRED})
    seal_for_tests(root, ("fx",), keychain=kc)  # writes to the site need a current seal (audit L1)
    kc.calls.clear()
    return root, wp, kc


def run(kc, *argv):
    return cli.main(list(argv), keychain=kc)


# ---------------------------------------------------------------- the site and credential fix

def test_wp_talks_only_to_cms_site_with_the_workspace_credential(env):
    root, wp, kc = env
    assert run(kc, "wp", "verify", "--workspace", "fx") == EXIT_OK
    assert set(wp.sites) == {SITE}
    assert ("get", "mseo.fx.wordpress", "fx") in kc.calls
    assert not [c for c in kc.calls if "fy" in c[1]]


def test_credential_for_another_site_is_exit_3_and_sends_nothing(env, capsys):
    root, wp, kc = env
    kc.items[("mseo.fx.wordpress", "fx")] = json.dumps({**CRED, "site": FY["cms"]["site"]})
    assert run(kc, "wp", "verify", "--workspace", "fx") == EXIT_COULD_NOT_RUN
    assert wp.sites == []
    assert "cms.site" in capsys.readouterr().err


def test_missing_credential_is_exit_3(env):
    root, wp, kc = env
    kc.items.clear()
    assert run(kc, "wp", "verify", "--workspace", "fx") == EXIT_COULD_NOT_RUN
    assert wp.sites == []


def test_unreachable_site_is_exit_3(env):
    root, wp, kc = env
    wp.fail_with = wordpress.WPError("could not reach the site")
    assert run(kc, "wp", "verify", "--workspace", "fx") == EXIT_COULD_NOT_RUN


def test_fy_publishes_to_fys_site_only(env, monkeypatch):
    root, wp, kc = env
    fy_site = FY["cms"]["site"]
    kc.items[("mseo.fy.wordpress", "fy")] = json.dumps({**CRED, "site": fy_site})
    seal_for_tests(root, ("fy",), keychain=kc)
    page = root / "fy" / "drafts" / "page.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text("<p>hello</p>")
    assert run(kc, "wp", "page-create", str(page), "--workspace", "fy", "--slug", "p", "--title", "P") == EXIT_OK
    assert set(wp.sites) == {fy_site}


# ---------------------------------------------------------------- wp commands

def test_post_create_dry_run_refuses_todo_before_the_network(env):
    root, wp, kc = env
    src = root / "fx" / "drafts" / "todo.md"
    src.write_text("# T\n\nTODO body\n")
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "t", "--dry-run") == EXIT_FAIL
    assert wp.sites == []


def test_post_create_creates_reads_back_and_refuses_an_existing_slug(env):
    root, wp, kc = env
    src = str(root / "fx" / "drafts" / "good_article.md")
    assert run(kc, "wp", "post-create", src, "--workspace", "fx", "--slug", "good-article", "--publish") == EXIT_OK
    assert wp.posts["good-article"]["status"] == "publish"
    assert run(kc, "wp", "post-create", src, "--workspace", "fx", "--slug", "good-article") == EXIT_EXISTS
    assert run(kc, "wp", "post-create", src, "--workspace", "fx", "--slug", "good-article", "--publish",
               "--replace") == EXIT_OK


def article_block(wp, slug="good-article"):
    blocks = [json.loads(b) for b in SCRIPT_RE.findall(wp.posts[slug]["content"]["raw"])]
    return next(b for b in blocks if b["@type"] == "Article")


def test_article_dates_come_from_the_log_and_move_only_when_the_body_changes(env, capsys):
    root, wp, kc = env
    src = root / "fx" / "drafts" / "good_article.md"
    log_date = next(a for a in json.loads((root / "fx" / "data" / "content_log.json").read_text())["articles"]
                    if a["slug"] == "good-article")["date"]
    assert log_date != TODAY.isoformat()
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    block = article_block(wp)
    assert (block["datePublished"], block["dateModified"]) == (log_date, log_date)
    assert block["mainEntityOfPage"] == f"{SITE}/good-article/" and (out["date_published"], out["content_changed"]) == (log_date, True)
    # an unchanged re-publish keeps both dates
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish", "--replace", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    block = article_block(wp)
    assert (block["datePublished"], block["dateModified"], out["content_changed"]) == (log_date, log_date, False)
    # an edit moves dateModified to today and leaves datePublished alone
    src.write_text(src.read_text().replace("slow bump", "gentle bump"))
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish", "--replace", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    block = article_block(wp)
    assert (block["datePublished"], block["dateModified"], out["content_changed"]) == (log_date, TODAY.isoformat(), True)


def test_existing_post_date_wins_on_update_and_an_unchanged_body_keeps_its_modified_date(env):
    root, wp, kc = env
    src = root / "fx" / "drafts" / "good_article.md"
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish") == EXIT_OK
    wp.posts["good-article"].update(date="2026-09-01T09:00:00", modified="2026-09-20T09:00:00")
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish", "--replace") == EXIT_OK
    block = article_block(wp)
    assert (block["datePublished"], block["dateModified"]) == ("2026-09-01", "2026-09-20")
    assert wordpress.article_dates({}, None, True, "2026-11-04") == ("2026-11-04", "2026-11-04")
    assert wordpress.article_dates({"date": "2026-10-01"}, None, True, "2026-11-04") == ("2026-10-01", "2026-10-01")
    assert wordpress.article_dates({"date": "2026-10-01"}, {"date": "2026-10-02T00:00:00"}, True, "2026-11-04") == ("2026-10-02", "2026-11-04")


def test_log_image_lands_in_the_article_block(env):
    root, wp, kc = env
    data = json.loads((root / "fx" / "data" / "content_log.json").read_text())
    next(a for a in data["articles"] if a["slug"] == "good-article")["image"] = f"{SITE}/img/bumper.jpg"
    (root / "fx" / "data" / "content_log.json").write_text(json.dumps(data))
    src = root / "fx" / "drafts" / "good_article.md"
    assert run(kc, "wp", "post-create", str(src), "--workspace", "fx", "--slug", "good-article", "--publish") == EXIT_OK
    assert article_block(wp)["image"] == f"{SITE}/img/bumper.jpg"


def test_post_create_dry_run_writes_nothing(env, capsys):
    root, wp, kc = env
    src = str(root / "fx" / "drafts" / "good_article.md")
    assert run(kc, "wp", "post-create", src, "--workspace", "fx", "--slug", "good-article", "--dry-run", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry-run" and out["site"] == SITE and "app_password" not in json.dumps(out)
    assert not wp.posts


def test_a_file_outside_the_workspace_is_refused(env):
    root, wp, kc = env
    assert run(kc, "wp", "page-create", str(root / "fy" / "RULES.md"), "--workspace", "fx", "--slug", "p",
               "--title", "P") == EXIT_COULD_NOT_RUN
    assert wp.sites == []


def test_strip_comments_keeps_code():
    markup = "<!-- note -->\n<script>\n// why\nvar u = 'https://x.test//y';\n</script>\n"
    out = wordpress.strip_comments(markup)
    assert "note" not in out and "why" not in out and "https://x.test//y" in out


# ---------------------------------------------------------------- business schema (audit M3, country)

def test_jsonld_escapes_script_closers_and_stays_json():
    payload = {"name": "x</script><script>alert(1)</script>", "note": "a & b > c"}
    markup = wordpress.schema_markup(payload)
    assert markup.count("</script>") == 1 and "<script>alert" not in markup and "&" not in markup.split("\n", 1)[1]
    assert json.loads(SCRIPT_RE.search(markup).group(1)) == payload
    assert "\\u003c" in md.jsonld_text({"a": "<"})


def test_business_schema_refuses_markup_and_bad_clocks_in_facts(env):
    root, wp, kc = env
    ws = __import__("mseo.workspace", fromlist=["open"]).open("fx", keychain=kc)
    bad_hours = json.loads(json.dumps(FX_FACTS))
    bad_hours["business"]["hours"][0]["opens"] = "8am"
    with pytest.raises(wordpress.FactsInvalid, match="HH:MM"):
        wordpress.business_schema(ws, bad_hours)
    bad_days = json.loads(json.dumps(FX_FACTS))
    bad_days["business"]["hours"][0]["days"] = "Mon</script><script>alert(1)"
    with pytest.raises(wordpress.FactsInvalid, match="markup"):
        wordpress.business_schema(ws, bad_days)
    (root / "fx" / "facts.json").write_text(json.dumps(bad_days))
    assert run(kc, "wp", "schema-deploy", "--workspace", "fx", "--dry-run") == EXIT_FAIL  # refused before the site is asked
    assert not wp.snippets and wp.sites == []


def test_address_country_comes_from_config(root, env):
    root, wp, kc = env
    ws = __import__("mseo.workspace", fromlist=["open"]).open("fx", keychain=kc)
    assert ws.config["business"]["country"] == "US"
    assert wordpress.business_schema(ws, {})["address"][0]["addressCountry"] == "US"
    edit_config(root, "fx", lambda c: c["business"].update(country="CA"))
    edit_config(root, "fx", lambda c: c["locations"][0].update(city="Maple Hollow"))
    ws = __import__("mseo.workspace", fromlist=["open"]).open("fx", keychain=kc)
    data = wordpress.business_schema(ws, {})
    assert data["address"][0]["addressCountry"] == "CA" and data["address"][0]["addressLocality"] == "Maple Hollow"
    assert "Maple Hollow" in {c["name"] for c in data["areaServed"]}


def test_posts_refresh_writes_the_list_atomically_from_cms_site(env):
    root, wp, kc = env
    assert run(kc, "wp", "posts-refresh", "--workspace", "fx") == EXIT_OK
    posts = json.loads((root / "fx" / "data" / "wp_posts.json").read_text())
    assert posts == [{"id": 1, "slug": "a-post", "date": "2026-11-01", "status": "publish", "title": "A & post",
                      "text": "Body text", "link": f"{SITE}/a-post/"}]
    assert wp.sites == [SITE]


def test_an_empty_fetch_never_overwrites_the_list(env, monkeypatch):
    root, wp, kc = env
    before = (root / "fx" / "data" / "wp_posts.json").read_text()
    monkeypatch.setattr(wordpress, "fetch_published", lambda site: [])
    assert run(kc, "wp", "posts-refresh", "--workspace", "fx") == EXIT_COULD_NOT_RUN
    assert (root / "fx" / "data" / "wp_posts.json").read_text() == before


# ---------------------------------------------------------------- publish

def pub(kc, root, *extra, kind="article"):
    path = root / "fx" / "drafts" / ("good_article.md" if kind == "article" else "good_gbp.txt")
    slug = ["--slug", "good-article"] if kind == "article" else []
    return run(kc, "publish", str(path), "--workspace", "fx", "--kind", kind, *slug, *extra)


def log(root):
    return json.loads((root / "fx" / "data" / "content_log.json").read_text())


def article_entry(root):
    return next(a for a in log(root)["articles"] if a["slug"] == "good-article")


def set_entry(root, **fields):
    data = log(root)
    next(a for a in data["articles"] if a["slug"] == "good-article").update(fields)
    (root / "fx" / "data" / "content_log.json").write_text(json.dumps(data))


def research(root):
    return (root / "fx" / "research" / "local" / "2026-W45.md").read_text()


def test_no_approval_refuses(env):
    root, wp, kc = env
    assert pub(kc, root, "--dates-confirmed") == EXIT_FAIL
    assert wp.sites == [] and article_entry(root)["status"] == "draft"


def test_failing_check_refuses(env):
    root, wp, kc = env
    p = root / "fx" / "drafts" / "good_article.md"
    p.write_text(p.read_text().replace("complimentary estimate", "free estimate", 1))
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_FAIL
    assert wp.sites == []


def test_event_dates_need_confirmation(env, capsys):
    root, wp, kc = env
    assert pub(kc, root, *APPROVED) == EXIT_FAIL
    assert "2026-11-15" in capsys.readouterr().out and wp.sites == []


def test_no_log_entry_refuses(env):
    root, wp, kc = env
    assert run(kc, "publish", str(root / "fx" / "drafts" / "good_article.md"), "--workspace", "fx", "--kind", "article",
               "--slug", "not-logged", *APPROVED, "--dates-confirmed") == EXIT_FAIL


def test_publishing_then_published_with_the_checked_bytes(env):
    root, wp, kc = env
    checked = (root / "fx" / "drafts" / "good_article.md").read_bytes()
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_OK
    entry = article_entry(root)
    assert (entry["status"], entry["url"], entry["wp_id"]) == ("published", LIVE, 42)
    assert entry["check_sha"] == hashlib.sha256(checked).hexdigest() and entry["approved"] == APPROVED[1]
    assert set(wp.sites) == {SITE}
    line = next(l for l in research(root).splitlines() if "2026-11-15" in l)
    assert f"rechecked: {TODAY.isoformat()}" in line


def test_publish_with_a_mismatched_credential_is_3_and_logs_nothing(env):
    root, wp, kc = env
    kc.items[("mseo.fx.wordpress", "fx")] = json.dumps({**CRED, "site": FY["cms"]["site"]})
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_COULD_NOT_RUN
    assert wp.sites == [] and article_entry(root)["status"] == "draft"


def test_slug_taken_by_another_post_refuses(env):
    root, wp, kc = env
    wp.posts["good-article"] = {"id": 7, "slug": "good-article", "status": "publish", "link": LIVE}
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_FAIL
    assert article_entry(root)["status"] == "publishing" and wp.posts["good-article"]["id"] == 7


def test_resume_updates_only_our_post(env, monkeypatch):
    root, wp, kc = env
    monkeypatch.setattr(publish_mod, "check_url", lambda url, timeout=20: {"url": url, "status": None, "ok": False, "error": "offline"})
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_FAIL
    assert (article_entry(root)["status"], article_entry(root)["wp_id"]) == ("publishing", 42)
    monkeypatch.setattr(publish_mod, "check_url", ok)
    assert pub(kc, root, *APPROVED) == EXIT_OK  # no --dates-confirmed: step 2 is done
    assert article_entry(root)["status"] == "published" and len(wp.posts) == 1


def test_live_url_on_another_host_refuses(env):
    root, wp, kc = env
    wp.link_base = FY["cms"]["site"]
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_FAIL
    assert article_entry(root)["status"] == "publishing"


def test_already_published_does_nothing_and_an_edit_after_is_refused(env):
    root, wp, kc = env
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_OK
    calls = len(wp.calls)
    assert pub(kc, root) == EXIT_OK and len(wp.calls) == calls
    p = root / "fx" / "drafts" / "good_article.md"
    p.write_text(p.read_text().replace("slow bump", "gentle bump"))
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_FAIL
    assert article_entry(root)["status"] == "published" and len(wp.calls) == calls


def test_dry_run_writes_nothing_to_the_log(env):
    root, wp, kc = env
    before = (log(root), research(root))
    assert pub(kc, root, *APPROVED, "--dates-confirmed", "--dry-run") == EXIT_OK
    assert (log(root), research(root)) == before and wp.sites == []


def test_gbp_awaiting_live_then_confirm_live(env):
    root, wp, kc = env
    assert pub(kc, root, *APPROVED, kind="gbp") == EXIT_OK
    assert log(root)["gbp"][0]["status"] == "publishing" and wp.sites == []
    url = "https://g.page/r/abc123"
    assert pub(kc, root, *APPROVED, "--confirm-live", url, kind="gbp") == EXIT_OK
    assert (log(root)["gbp"][0]["status"], log(root)["gbp"][0]["url"]) == ("published", url)


def test_confirm_live_must_be_a_google_url(env):
    root, wp, kc = env
    pub(kc, root, *APPROVED, kind="gbp")
    for url in (f"{SITE}/post/", "https://google.com.evil.test/x"):
        assert pub(kc, root, *APPROVED, "--confirm-live", url, kind="gbp") == EXIT_FAIL
        assert log(root)["gbp"][0]["status"] == "publishing"


def test_google_hosts():
    for url in ("https://business.google.com/p/1", "https://g.page/r/x", "https://maps.app.goo.gl/x", "https://www.google.com/maps/x"):
        assert publish_mod.is_google_post_url(url)


def test_the_logged_date_sets_the_slot():
    tz = publish_mod.ZoneInfo(FX["timezone"])
    now = dt.datetime(2026, 11, 2, 9, 0, tzinfo=tz)
    slots = FX["posting"]["business_profile_slots"]
    when, note = publish_mod.post_slot("2026-11-09", slots, FX["timezone"], now=now)
    assert (when.isoformat(), note) == ("2026-11-09T10:00:00-08:00", None)
    for logged in ("2026-11-10", "2026-10-29", "", None):
        when, note = publish_mod.post_slot(logged, slots, FX["timezone"], now=now)
        assert when.isoformat() == "2026-11-02T10:00:00-08:00" and (note is None) == (not logged)


def test_request_refuses_http_scheme():
    with pytest.raises(wordpress.WPError, match="must use https://"):
        wordpress.request("http://insecure-site.test", "user", "pass", "GET", "wp/v2/posts")


def test_public_status_refuses_non_http_schemes():
    assert wordpress.public_status("file:///etc/passwd") is None
    assert wordpress.public_status("ftp://example.com/resource") is None
