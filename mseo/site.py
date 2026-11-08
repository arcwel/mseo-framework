"""`mseo site audit`: technical SEO checks, schema validation and IndexNow.

    mseo site audit --workspace S [--pages path,...] [--json]     # report + data/audits/<date>-site.json; exit 1 on a fail
    mseo site indexnow --workspace S URL                          # ping IndexNow by hand (site_audit.indexnow_key)

Fetched (through one injectable fetcher, so tests use fakes): the home page, robots.txt, the sitemaps robots names or
/sitemap.xml and /sitemap_index.xml, the configured site_audit.pages, --pages, and the newest 5 published posts from
the wp_posts list. Classification: fail = missing title, meta description, H1, canonical, alt text or schema; broken
internal link; noindex; sitemap missing or without the newest post. warn = lengths, stale lastmod, no https redirect,
www inconsistency, a Core Web Vital over its threshold. info = AI crawler blocks, page size, PSI unchecked.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mseo import doctor
from mseo.content.links import USER_AGENT, check_url
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.site_checks import indexnow, page as page_mod, sitewide
from mseo.site_checks.page import finding

NEWEST_POSTS = 5
LINK_CAP = 50
REDIRECT_HOPS = 5
TIMEOUT_S = 20
LEVELS = ("fail", "warn", "info")


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoFollow, urllib.request.HTTPSHandler(context=ssl.create_default_context()))


MAX_BODY = 5_000_000  # bytes kept from any one response


def urllib_fetch(url: str, timeout: int = TIMEOUT_S) -> tuple[int, dict, str]:
    """GET without following redirects: (status, headers, body text). The audit follows Location itself so the
    http -> https and www checks can see the redirect."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(MAX_BODY + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(MAX_BODY + 1).decode("utf-8", "replace")


WEB_SCHEMES = ("http", "https")


def web_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in WEB_SCHEMES and bool(parts.netloc)


def follow(fetch, url: str, hops: int = REDIRECT_HOPS):
    """(final_url, status, headers, body) after following up to `hops` redirects through the fetcher. Only http(s)
    URLs are fetched: anything else is status 0 with an empty body."""
    for _ in range(hops + 1):
        if not web_url(url):
            return url, 0, {}, ""
        status, headers, body = fetch(url)
        if isinstance(body, str) and len(body) > MAX_BODY:
            body = body[:MAX_BODY]
        location = next((v for k, v in (headers or {}).items() if k.lower() == "location"), None)
        if 300 <= status < 400 and location:
            url = urllib.parse.urljoin(url, location)
            continue
        return url, status, headers or {}, body if isinstance(body, str) else bytes(body).decode("utf-8", "replace")
    return url, status, headers or {}, ""


def link_via_fetch(fetch):
    """A check_url-shaped checker that goes through the fetcher (tests); the default audit uses check_url itself."""
    def check(url: str) -> dict:
        try:
            final, status, _, _ = follow(fetch, url)
        except Exception as exc:  # noqa: BLE001 - same contract as check_url: a failure is status None
            return {"url": url, "status": None, "ok": False, "error": str(exc)}
        return {"url": url, "status": status, "ok": 200 <= status < 300, "final": final if final != url else None}
    return check


def newest_posts(ws, site: str) -> list[str]:
    """URLs of the newest published posts in the wp_posts list, newest first."""
    try:
        posts = json.loads(ws.file("wp_posts").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    live = [p for p in posts if isinstance(p, dict) and p.get("slug") and p.get("status", "publish") == "publish"]
    live.sort(key=lambda p: str(p.get("date", "")), reverse=True)
    return [p.get("link") or f"{site}/{p['slug'].strip('/')}/" for p in live[:NEWEST_POSTS]]


def targets(ws, extra_pages=None) -> list[tuple[str, str]]:
    """[(kind, url)]: home, configured pages, --pages, newest posts; duplicates dropped, first kind wins."""
    site = ws.config["business"]["site"].rstrip("/")
    out: list[tuple[str, str]] = [("home", site + "/")]
    for rel in list(ws.config["site_audit"].get("pages") or []) + list(extra_pages or []):
        out.append(("page", urllib.parse.urljoin(site + "/", str(rel).lstrip("/"))))
    out += [("post", u) for u in newest_posts(ws, site)]
    seen, unique = set(), []
    for kind, url in out:
        if not any(page_mod.same_url(url, s) for s in seen):
            seen.add(url)
            unique.append((kind, url))
    return unique


def audit(ws, *, fetch=None, pages=None, env=None, today=None) -> dict:
    env = os.environ if env is None else env
    today = today or doctor.today()
    site = ws.config["business"]["site"].rstrip("/")
    host = urllib.parse.urlparse(site).hostname or ""
    linker = check_url if fetch is None else link_via_fetch(fetch)
    fetch = fetch or urllib_fetch

    def get(url):
        return follow(fetch, url)

    schema_type = ws.config["business"].get("schema_type") or "LocalBusiness"
    findings: list[dict] = []
    pages_out: list[dict] = []
    links: dict[str, str] = {}  # link -> first page that carries it
    pages_to_check = targets(ws, pages)
    for kind, url in pages_to_check:
        try:
            final, status, headers, body = get(url)
        except Exception as exc:  # noqa: BLE001 - the page's finding says why
            findings.append(finding("fail", "page loads", url, f"{type(exc).__name__}: {exc}"))
            pages_out.append({"kind": kind, "url": url, "final": None, "status": None})
            continue
        try:
            found, parsed = page_mod.check_page(final, status, headers, body, kind=kind, schema_type=schema_type)
        except Exception as exc:  # noqa: BLE001 - hostile HTML or JSON-LD on one page is a finding, not a crash
            findings.append(finding("fail", "page parses", url, f"{type(exc).__name__}: {str(exc)[:160]}"))
            pages_out.append({"kind": kind, "url": url, "final": final, "status": status})
            continue
        findings += found
        for href in parsed.links:
            target = page_mod.is_internal(href, final, host)
            if target and target not in links and not any(page_mod.same_url(target, u) for _, u in pages_to_check):
                links.setdefault(target, final)
        pages_out.append({"kind": kind, "url": url, "final": final, "status": status, "title": parsed.title,
                          "h1": parsed.h1s[:1], "schema_types": sorted(parsed.schema_types()[0])})
    checked = list(links)[:LINK_CAP]
    broken = []
    for link in checked:
        res = linker(link)
        if not res.get("ok"):
            broken.append(f"{link} ({res.get('status') or res.get('error')}) on {links[link]}")
    if broken:
        findings.append(finding("fail", "internal links resolve", site, f"{len(broken)} broken: " + "; ".join(broken[:10])))
    else:
        findings.append(finding("info", "internal links resolve", site,
                                f"{len(checked)} checked" + (f" (capped at {LINK_CAP} of {len(links)})" if len(links) > LINK_CAP else "")))

    robots_url = site + "/robots.txt"
    robots_sitemaps: list[str] = []
    try:
        _, status, _, text = get(robots_url)
        findings += sitewide.robots_findings(robots_url, status, text)
        if 200 <= status < 300:
            robots_sitemaps = sitewide.parse_robots(text)["sitemaps"]
    except Exception as exc:  # noqa: BLE001
        findings.append(finding("warn", "robots.txt present", robots_url, f"could not fetch ({type(exc).__name__})"))
    newest = next((u for k, u in pages_to_check if k == "post"), None)
    findings += sitewide.sitemap_findings(get, site, robots_sitemaps, newest, today)
    findings += sitewide.redirect_findings(fetch, site)
    key_env = ws.config["site_audit"].get("psi_key_env") or "MSEO_PSI_KEY"
    findings += sitewide.cwv_findings(fetch, [site + "/"] + ([newest] if newest else []), env.get(key_env))

    counts = {level: sum(1 for f in findings if f["level"] == level) for level in LEVELS}
    return {"schema": "mseo.site-audit/1", "workspace": ws.slug, "site": site, "date": today.isoformat(),
            "pages": pages_out, "findings": findings, "counts": counts, "result": "fail" if counts["fail"] else "pass"}


def markdown(report: dict) -> str:
    lines = [f"# Site audit {report['date']}: {report['site']}", "",
             f"Result: **{report['result']}** ({report['counts']['fail']} fail, {report['counts']['warn']} warn, "
             f"{report['counts']['info']} info); {len(report['pages'])} page(s) checked.", ""]
    for level in LEVELS:
        rows = [f for f in report["findings"] if f["level"] == level]
        if rows:
            lines += [f"## {level} ({len(rows)})", ""] + [f"- {f['check']}: {f['detail']} ({f['url']})" for f in rows] + [""]
    return "\n".join(lines)


def write_report(ws, report: dict) -> Path:
    path = ws.path("data", "audits", f"{report['date']}-site.json")
    write_json_atomic(path, report)
    return path


# ---------------------------------------------------------------- CLI

def cmd_audit(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    pages = [p.strip() for p in (args.pages or "").split(",") if p.strip()]
    report = audit(ws, pages=pages)
    path = write_report(ws, report)
    ctx.emit({**report, "path": str(path), "messages": markdown(report).splitlines() + [f"written: {path}"]})
    return EXIT_FAIL if report["counts"]["fail"] else EXIT_OK


def cmd_indexnow(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    key = indexnow.key_for(ws)
    if not key:
        ctx.emit({"status": None, "messages": ["site_audit.indexnow_key is not set in customer.json"]})
        return EXIT_COULD_NOT_RUN
    res = indexnow.ping(ws.config["business"]["site"], key, [args.url])
    ctx.emit({**res, "url": args.url, "messages": [f"indexnow {args.url}: {res['status']}"
                                                   + (f" ({res['error']})" if res.get("error") else "")]})
    return EXIT_OK if indexnow.ok(res) else EXIT_FAIL


def register(sub, parents) -> None:
    p = sub.add_parser("site", help="Technical checks of the workspace's site")
    ssub = p.add_subparsers(dest="site_command", required=True, parser_class=type(p))
    a = ssub.add_parser("audit", parents=[parents.workspace, parents.json],
                        help="Audit the home page, configured pages and newest posts; write data/audits/<date>-site.json")
    a.add_argument("--pages", help="Extra site paths to check, comma-separated (e.g. contact/,services/)")
    a.set_defaults(func=cmd_audit, command_id="site audit")
    i = ssub.add_parser("indexnow", parents=[parents.workspace, parents.json], help="Ping IndexNow with one URL")
    i.add_argument("url")
    i.set_defaults(func=cmd_indexnow, command_id="site indexnow")
