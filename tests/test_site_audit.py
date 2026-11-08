"""`mseo site audit` and IndexNow (plan 3.2 item 5, audit 24). Every fetch is a fake: nothing leaves the machine."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from mseo import cli
from mseo import publish as publish_mod
from mseo.cms import wordpress
from mseo.content import check
from mseo.exits import EXIT_FAIL, EXIT_OK
from mseo.site_checks import indexnow
from tests.conftest import FX, edit_config, seal_for_tests
from tests.fakes import FakeKeychain, FakeWordPress
from tests.test_wordpress_publish import APPROVED, CRED, LIVE, SITE, TODAY, article_entry, ok, pub

KEY = "a1b2c3d4e5f6a7b8c9d0"


class Sender:
    def __init__(self, status=202, raise_=None):
        self.status, self.raise_, self.calls = status, raise_, []

    def __call__(self, url, body):
        self.calls.append((url, body))
        if self.raise_:
            raise self.raise_
        return self.status


# ---------------------------------------------------------------- IndexNow

def test_indexnow_payload_names_host_key_location_and_urls():
    body = indexnow.payload(SITE, KEY, [LIVE])
    host = SITE.split("//", 1)[1]
    assert body == {"host": host, "key": KEY, "keyLocation": f"https://{host}/{KEY}.txt", "urlList": [LIVE]}


def test_indexnow_ping_records_status_and_time():
    s = Sender()
    now = dt.datetime(2026, 11, 7, 12, 0, tzinfo=dt.timezone.utc)
    res = indexnow.ping(SITE, KEY, [LIVE], sender=s, now=now)
    assert res == {"status": 202, "at": "2026-11-07T12:00:00+00:00"} and indexnow.ok(res)
    assert s.calls[0][0] == indexnow.ENDPOINT and s.calls[0][1]["urlList"] == [LIVE]


def test_indexnow_ping_never_raises():
    res = indexnow.ping(SITE, KEY, [LIVE], sender=Sender(raise_=OSError("down")))
    assert res["status"] is None and "down" in res["error"] and not indexnow.ok(res)
    assert "error" in indexnow.ping(SITE, KEY, [LIVE], sender=Sender(status=403))
    assert indexnow.ping(SITE, KEY, ["https://elsewhere.test/x/"], sender=Sender())["status"] is None


@pytest.fixture
def env(root, monkeypatch, tmp_path):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", ok)
    monkeypatch.setattr(check, "deslop_version", lambda: "test")
    monkeypatch.setattr(publish_mod, "check_url", ok)
    # the content gates belong to another suite: here every draft passes, so only the IndexNow step is under test
    monkeypatch.setattr(check, "check_and_write", lambda draft, ws: {"result": "pass", "gates": []})
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wp = FakeWordPress(SITE).install(monkeypatch, wordpress)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): CRED})
    sender = Sender()
    monkeypatch.setattr(indexnow, "urllib_sender", sender)
    return root, wp, kc, sender


def test_publish_pings_indexnow_when_the_key_is_set(env):
    root, wp, kc, sender = env
    edit_config(root, "fx", lambda c: c.setdefault("site_audit", {}).update(indexnow_key=KEY))
    seal_for_tests(root, ("fx",), keychain=kc)
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_OK
    assert [b["urlList"] for _, b in sender.calls] == [[LIVE]] and sender.calls[0][1]["key"] == KEY
    entry = article_entry(root)
    assert entry["status"] == "published" and entry["indexnow"]["status"] == 202 and entry["indexnow"]["at"]


def test_publish_without_a_key_never_pings(env):
    root, wp, kc, sender = env
    seal_for_tests(root, ("fx",), keychain=kc)
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_OK
    assert sender.calls == [] and "indexnow" not in article_entry(root)


def test_a_failed_ping_never_changes_the_publish_result(env, capsys):
    root, wp, kc, sender = env
    sender.raise_ = OSError("indexnow down")
    edit_config(root, "fx", lambda c: c.setdefault("site_audit", {}).update(indexnow_key=KEY))
    seal_for_tests(root, ("fx",), keychain=kc)
    assert pub(kc, root, *APPROVED, "--dates-confirmed") == EXIT_OK
    out = capsys.readouterr().out
    assert "published:" in out and "indexnow: None (OSError: indexnow down)" in out
    entry = article_entry(root)
    assert entry["status"] == "published" and entry["indexnow"]["status"] is None


# ---------------------------------------------------------------- the audit itself

from mseo import site as site_mod  # noqa: E402
from mseo import workspace  # noqa: E402
from mseo.site_checks import html as html_mod, page as page_mod, sitewide  # noqa: E402

HOST = SITE.split("//", 1)[1]
TODAY_AUDIT = dt.date(2026, 11, 7)
POST = f"{SITE}/tire-rotation-basics/"  # the fixture's newest post (data/wp_posts.json)
CONTACT = f"{SITE}/contact/"
TITLE = "A good page title of fair length"  # 32 chars
DESC = "A meta description long enough for the audit to accept, written in plain words about the page itself."  # 103


def page_html(url, *, title=TITLE, desc=DESC, h1s=("One heading",), canonical=None, viewport=True, imgs=(("a.jpg", "a photo"),),
              schema=(), links=(CONTACT,), robots=None, extra=""):
    canonical = url if canonical is None else canonical
    head = [f"<title>{title}</title>" if title else "", f'<meta name="description" content="{desc}">' if desc else "",
            f'<link rel="canonical" href="{canonical}">' if canonical else "",
            '<meta name="viewport" content="width=device-width">' if viewport else "",
            f'<meta name="robots" content="{robots}">' if robots else ""]
    head += [f'<script type="application/ld+json">{json.dumps(s)}</script>' for s in schema]
    body = "".join(f"<h1>{h}</h1>" for h in h1s) + "".join(
        f'<img src="{src}" alt="{alt}">' if alt is not None else f'<img src="{src}">' for src, alt in imgs)
    body += "".join(f'<a href="{l}">link</a>' for l in links) + '<a href="mailto:x@y.test">m</a><script>var a=1;</script>' + extra
    return f"<!doctype html><html><head>{''.join(head)}</head><body>{body}</body></html>"


LOCAL = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": FX["business"]["name"]}
ARTICLE = {"@context": "https://schema.org", "@graph": [{"@type": "Article", "headline": "x"}, {"@type": "FAQPage"}]}
SITEMAP = (f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"<url><loc>{SITE}/</loc><lastmod>2026-11-01</lastmod></url><url><loc>{POST}</loc><lastmod>2026-11-02T09:00:00+00:00</lastmod></url>"
           f"<url><loc>{CONTACT}</loc></url></urlset>")
ROBOTS = f"User-agent: *\nDisallow: /wp-admin/\nUser-agent: GPTBot\nDisallow: /\nSitemap: {SITE}/sitemap.xml\n"


class FakeSite:
    """fetch(url) -> (status, headers, body) from a table; every URL asked is recorded; unknown URLs are 404."""

    def __init__(self, table: dict):
        self.table, self.calls = dict(table), []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.table:
            return self.table[url]
        return 404, {}, "not here"


def good_site(**over):
    table = {
        f"{SITE}/": (200, {"Content-Type": "text/html"}, page_html(f"{SITE}/", schema=[LOCAL])),
        POST: (200, {}, page_html(POST, schema=[ARTICLE])),
        CONTACT: (200, {}, page_html(CONTACT, links=())),
        f"{SITE}/robots.txt": (200, {}, ROBOTS),
        f"{SITE}/sitemap.xml": (200, {}, SITEMAP),
        f"http://{HOST}/": (301, {"Location": f"{SITE}/"}, ""),
        f"https://www.{HOST}/": (301, {"Location": f"{SITE}/"}, ""),
    }
    table.update(over)
    return FakeSite(table)


def _ws():
    return workspace.open("fx", keychain=FakeKeychain())


def by_check(report, check):
    return [f for f in report["findings"] if f["check"] == check]


def test_a_clean_site_passes_with_only_info(root):
    fake = good_site()
    report = site_mod.audit(_ws(), fetch=fake, today=TODAY_AUDIT)
    assert report["counts"]["fail"] == 0 and report["counts"]["warn"] == 0, [f for f in report["findings"] if f["level"] != "info"]
    assert report["result"] == "pass"
    assert [p["kind"] for p in report["pages"]] == ["home", "post"]
    assert by_check(report, "AI crawlers")[0]["detail"] == "blocked: GPTBot"
    assert by_check(report, "Core Web Vitals")[0]["detail"] == "unchecked (no PSI key)"
    assert by_check(report, "internal links resolve")[0]["detail"] == "1 checked"
    assert by_check(report, "sitemap lists the newest post")[0]["level"] == "info"
    assert f"http://{HOST}/" in fake.calls and f"https://www.{HOST}/" in fake.calls
    assert all(u.startswith(("https://", "http://")) and HOST in u for u in fake.calls)  # nothing else is fetched


def test_page_failures_are_classified(root):
    bad_home = page_html(f"{SITE}/", title="", desc="too short", h1s=("a", "b"), canonical=f"{SITE}/other/", viewport=False,
                         imgs=(("x.jpg", None), ("y.jpg", "")), schema=[{"@type": "WebSite"}], links=(f"{SITE}/missing/",),
                         robots="noindex, follow")
    bad_post = page_html(POST, schema=[{"@type": "Article"}], title="x" * 70, extra='<script type="application/ld+json">{bad</script>')
    report = site_mod.audit(_ws(), fetch=good_site(**{f"{SITE}/": (200, {}, bad_home), POST: (200, {}, bad_post)}), today=TODAY_AUDIT)
    fails = {f["check"] for f in report["findings"] if f["level"] == "fail"}
    assert fails >= {"title present", "exactly one H1", "canonical self-referencing", "image alt text",
                     "business schema on the home page", "indexable", "internal links resolve", "article schema", "JSON-LD parses"}
    warns = {f["check"] for f in report["findings"] if f["level"] == "warn"}
    assert {"meta description length", "viewport meta", "title length"} <= warns
    assert "FAQPage" in by_check(report, "article schema")[0]["detail"]
    assert "2 image(s)" in by_check(report, "image alt text")[0]["detail"]
    assert "/missing/ (404)" in by_check(report, "internal links resolve")[0]["detail"]
    assert report["result"] == "fail"


def test_sitewide_warnings_and_sitemap_failures(root):
    stale = SITEMAP.replace(POST, f"{SITE}/elsewhere/").replace("2026-11-01", "2026-08-01").replace("2026-11-02T09:00:00+00:00", "2026-07-01")
    robots = f"User-agent: *\nDisallow: /wp-content/themes/\nUser-agent: Googlebot\nDisallow: /\nSitemap: {SITE}/sitemap.xml\n"
    fake = good_site(**{f"{SITE}/sitemap.xml": (200, {}, stale), f"{SITE}/robots.txt": (200, {}, robots),
                        f"http://{HOST}/": (200, {}, "plain http"), f"https://www.{HOST}/": (200, {}, "a copy")})
    report = site_mod.audit(_ws(), fetch=fake, today=TODAY_AUDIT)
    assert by_check(report, "sitemap lists the newest post")[0]["level"] == "fail"
    assert by_check(report, "sitemap lastmod")[0]["level"] == "warn"
    assert by_check(report, "robots allows Googlebot")[0]["level"] == "fail"
    assert by_check(report, "http redirects to https")[0]["level"] == "warn"
    assert by_check(report, "www consistency")[0]["level"] == "warn"
    assert by_check(report, "robots does not block CSS/JS") == []  # the Googlebot group, not *, applies
    assert f"{SITE}/sitemap.xml" in fake.calls and f"{SITE}/sitemap_index.xml" not in fake.calls  # robots named it


def test_without_robots_sitemaps_both_defaults_are_tried_and_an_index_is_followed(root):
    index = (f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{SITE}/post-sitemap.xml</loc>'
             f"</sitemap></sitemapindex>")
    fake = good_site(**{f"{SITE}/robots.txt": (404, {}, ""), f"{SITE}/sitemap.xml": (404, {}, ""),
                        f"{SITE}/sitemap_index.xml": (200, {}, index), f"{SITE}/post-sitemap.xml": (200, {}, SITEMAP)})
    report = site_mod.audit(_ws(), fetch=fake, today=TODAY_AUDIT)
    assert by_check(report, "robots.txt present")[0]["level"] == "warn"
    assert by_check(report, "sitemap lists the newest post")[0]["level"] == "info"
    assert f"{SITE}/post-sitemap.xml" in fake.calls
    fake2 = good_site(**{f"{SITE}/robots.txt": (404, {}, ""), f"{SITE}/sitemap.xml": (404, {}, "")})
    assert by_check(site_mod.audit(_ws(), fetch=fake2, today=TODAY_AUDIT), "sitemap present")[0]["level"] == "fail"


def test_redirected_pages_are_checked_at_their_final_url(root):
    fake = good_site(**{POST: (301, {"Location": f"{SITE}/tire-rotation-basics-2/"}, ""),
                        f"{SITE}/tire-rotation-basics-2/": (200, {}, page_html(f"{SITE}/tire-rotation-basics-2/", schema=[ARTICLE]))})
    report = site_mod.audit(_ws(), fetch=fake, today=TODAY_AUDIT)
    post = next(p for p in report["pages"] if p["kind"] == "post")
    assert post["final"] == f"{SITE}/tire-rotation-basics-2/" and by_check(report, "canonical self-referencing") == []


def test_configured_and_extra_pages_are_audited(root):
    edit_config(root, "fx", lambda c: c.setdefault("site_audit", {}).update(pages=["contact/"]))
    fake = good_site(**{f"{SITE}/services/": (200, {}, page_html(f"{SITE}/services/", links=()))})
    report = site_mod.audit(_ws(), fetch=fake, pages=["services/"], today=TODAY_AUDIT)
    assert [p["url"] for p in report["pages"] if p["kind"] == "page"] == [CONTACT, f"{SITE}/services/"]
    assert by_check(report, "internal links resolve")[0]["detail"] == "0 checked"  # contact is a page now, not a link


def test_internal_link_checks_are_capped_at_50(root):
    links = tuple(f"{SITE}/p{n}/" for n in range(60))
    fake = good_site(**{f"{SITE}/": (200, {}, page_html(f"{SITE}/", schema=[LOCAL], links=links))})
    report = site_mod.audit(_ws(), fetch=fake, today=TODAY_AUDIT)
    assert "50 broken" in by_check(report, "internal links resolve")[0]["detail"]
    assert sum(1 for u in fake.calls if "/p" in u) == 50


PSI = {"loadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100},
                                         "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 4}}},
       "lighthouseResult": {"audits": {"interaction-to-next-paint": {"numericValue": 150}}}}


def test_psi_runs_only_with_the_key_and_the_key_never_reaches_the_report(root, monkeypatch):
    fake = good_site()
    key = "psi-secret-key-123"
    psi_calls = []

    def fetch(url):
        if url.startswith(sitewide.PSI_ENDPOINT):
            psi_calls.append(url)
            return 200, {}, json.dumps(PSI)
        return fake(url)

    env = {FX.get("site_audit", {}).get("psi_key_env", "MSEO_PSI_KEY"): key}
    report = site_mod.audit(_ws(), fetch=fetch, env=env, today=TODAY_AUDIT)
    assert len(psi_calls) == 2 and all(key in u for u in psi_calls)  # home and the newest post
    assert key not in json.dumps(report)
    lcp = [f for f in by_check(report, "CWV LCP") if f["url"] == f"{SITE}/"][0]
    assert lcp["level"] == "warn" and "3.10s over 2.50s (field)" in lcp["detail"]
    assert "150ms within 200ms (lab)" in [f for f in by_check(report, "CWV INP")][0]["detail"]
    assert "0.040 within 0.100 (field)" in by_check(report, "CWV CLS")[0]["detail"]
    assert by_check(site_mod.audit(_ws(), fetch=fetch, env={}, today=TODAY_AUDIT), "Core Web Vitals")[0]["detail"] == "unchecked (no PSI key)"


def test_cli_writes_the_json_report_and_exits_1_on_a_fail(root, monkeypatch, capsys):
    bad = good_site(**{POST: (200, {}, page_html(POST, schema=[]))})
    monkeypatch.setattr(site_mod, "urllib_fetch", bad)
    monkeypatch.setattr(site_mod, "check_url", site_mod.link_via_fetch(bad))
    monkeypatch.setenv("MSEO_TODAY", TODAY_AUDIT.isoformat())
    assert cli.main(["site", "audit", "--workspace", "fx", "--json"], keychain=FakeKeychain()) == EXIT_FAIL
    out = json.loads(capsys.readouterr().out)
    path = root / "fx" / "data" / "audits" / f"{TODAY_AUDIT.isoformat()}-site.json"
    assert path.is_file() and out["path"] == str(path.resolve()) and json.loads(path.read_text())["counts"]["fail"] == 1
    good = good_site()
    monkeypatch.setattr(site_mod, "urllib_fetch", good)
    monkeypatch.setattr(site_mod, "check_url", site_mod.link_via_fetch(good))
    assert cli.main(["site", "audit", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_OK
    text = capsys.readouterr().out
    assert text.startswith("# Site audit") and "Result: **pass**" in text


def test_cli_indexnow_pings_with_the_configured_key(root, monkeypatch, capsys):
    sender = Sender()
    monkeypatch.setattr(indexnow, "urllib_sender", sender)
    assert cli.main(["site", "indexnow", "--workspace", "fx", LIVE], keychain=FakeKeychain()) == 3  # no key configured
    edit_config(root, "fx", lambda c: c.setdefault("site_audit", {}).update(indexnow_key=KEY))
    assert cli.main(["site", "indexnow", "--workspace", "fx", LIVE], keychain=FakeKeychain()) == EXIT_OK
    assert sender.calls[0][1]["urlList"] == [LIVE]
    sender.status = 422
    assert cli.main(["site", "indexnow", "--workspace", "fx", LIVE], keychain=FakeKeychain()) == EXIT_FAIL


def test_html_reader_and_helpers():
    page = html_mod.read('<title> T </title><meta property="og:title" content="o"><h1>A <b>b</b></h1><link rel="stylesheet canonical" href="/c">'
                         '<img src="1.png" alt=""><a href="#top">x</a><script src="x.js"></script>')
    assert page.title == "T" and page.h1s == ["A b"] and page.canonical == "/c" and page.external_scripts == 1
    assert page.images[0]["alt"] == "" and page.links == ["#top"]
    assert page_mod.same_url("https://a.test/x/", "HTTPS://A.test/x#frag") and not page_mod.same_url("https://a.test/x", "https://a.test/y")
    assert page_mod.is_internal("/p/", "https://www.a.test/", "a.test") == "https://www.a.test/p/"
    assert page_mod.is_internal("https://b.test/", "https://a.test/", "a.test") is None
    robots = sitewide.parse_robots("User-agent: a\nUser-agent: b\nDisallow: /x\n\nUser-agent: *\nAllow: /\nSitemap: https://a.test/s.xml")
    assert robots["groups"]["a"]["disallow"] == ["/x"] == robots["groups"]["b"]["disallow"] and robots["sitemaps"] == ["https://a.test/s.xml"]
    assert sitewide.cwv_from_psi({}) == {"LCP": (None, "none"), "INP": (None, "none"), "CLS": (None, "none")}


def test_a_page_that_breaks_the_parser_is_a_finding_not_a_crash(root, monkeypatch):
    from mseo import site, workspace
    from mseo.site_checks import page as page_mod
    from tests.fakes import FakeKeychain

    ws = workspace.open("fx", keychain=FakeKeychain())

    def fetch(url):
        return 200, {"Content-Type": "text/html"}, "<html><head><title>ok</title></head><body><a href='http://[bad'>x</a></body></html>"

    def boom(*a, **k):
        raise RecursionError("deep")

    monkeypatch.setattr(page_mod, "check_page", boom)
    out = site.audit(ws, fetch=fetch, pages=[])
    assert any(f["check"] == "page parses" and f["level"] == "fail" for f in out["findings"])
