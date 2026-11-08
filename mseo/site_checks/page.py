"""Per-page checks. A finding is {"level", "check", "url", "detail"}; levels: fail, warn, info."""

from __future__ import annotations

import urllib.parse

from mseo.site_checks import html as html_mod

TITLE_LEN = (15, 60)
META_LEN = (70, 160)
POST_TYPES = ("Article", "FAQPage")


def finding(level: str, check: str, url: str, detail: str) -> dict:
    return {"level": level, "check": check, "url": url, "detail": detail}


def same_url(a: str, b: str) -> bool:
    """Equal after lower-casing scheme and host and ignoring a trailing slash and a fragment."""
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    norm = lambda p: (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", p.query)  # noqa: E731
    return norm(pa) == norm(pb)


def is_internal(href: str, base: str, site_host: str) -> str | None:
    """The absolute URL when `href` points at a page on the site (http(s), same bare host); else None."""
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urllib.parse.urljoin(base, href.strip())
    parts = urllib.parse.urlsplit(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    return absolute if host == site_host.lower().removeprefix("www.") else None


def check_page(url: str, status: int, headers: dict, body: str, *, kind: str, schema_type: str) -> tuple[list[dict], html_mod.Page]:
    """Findings for one fetched page. kind: home, post or page. `url` is the final URL after redirects."""
    out: list[dict] = []
    if not (200 <= status < 300):
        out.append(finding("fail", "page loads", url, f"HTTP {status}"))
        return out, html_mod.Page()
    page = html_mod.read(body)
    lower_headers = {k.lower(): v for k, v in (headers or {}).items()}

    if not page.title:
        out.append(finding("fail", "title present", url, "no <title>"))
    elif not TITLE_LEN[0] <= len(page.title) <= TITLE_LEN[1]:
        out.append(finding("warn", "title length", url, f"{len(page.title)} chars, want {TITLE_LEN[0]}-{TITLE_LEN[1]}"))

    desc = (page.metas.get("description") or "").strip()
    if not desc:
        out.append(finding("fail", "meta description present", url, "no <meta name=description>"))
    elif not META_LEN[0] <= len(desc) <= META_LEN[1]:
        out.append(finding("warn", "meta description length", url, f"{len(desc)} chars, want {META_LEN[0]}-{META_LEN[1]}"))

    if len(page.h1s) != 1:
        out.append(finding("fail", "exactly one H1", url, f"{len(page.h1s)} H1 element(s)"))

    if not page.canonical:
        out.append(finding("fail", "canonical present", url, "no <link rel=canonical>"))
    elif not same_url(urllib.parse.urljoin(url, page.canonical), url):
        out.append(finding("fail", "canonical self-referencing", url, f"canonical is {page.canonical}"))

    if "viewport" not in page.metas:
        out.append(finding("warn", "viewport meta", url, "no <meta name=viewport>"))

    missing_alt = [i["src"] or "(no src)" for i in page.images if not (i["alt"] or "").strip()]
    if missing_alt:
        out.append(finding("fail", "image alt text", url, f"{len(missing_alt)} image(s) without alt: "
                                                          + ", ".join(missing_alt[:5])))

    types, errors = page.schema_types()
    for e in errors:
        out.append(finding("fail", "JSON-LD parses", url, e))
    if kind == "home" and schema_type not in types:
        out.append(finding("fail", "business schema on the home page", url,
                           f"no JSON-LD @type {schema_type} (found {', '.join(sorted(types)) or 'none'})"))
    if kind == "post":
        missing = [t for t in POST_TYPES if t not in types]
        if missing:
            out.append(finding("fail", "article schema", url, f"JSON-LD lacks {', '.join(missing)} "
                                                              f"(found {', '.join(sorted(types)) or 'none'})"))

    robots = " ".join(filter(None, [page.robots_meta or "", lower_headers.get("x-robots-tag", "")])).lower()
    if "noindex" in robots:
        out.append(finding("fail", "indexable", url, f"noindex ({robots.strip()})"))

    out.append(finding("info", "page size", url, f"{len(body.encode('utf-8', 'replace'))} bytes, "
                                                 f"{page.inline_scripts} inline script(s), {page.external_scripts} external"))
    return out, page
