"""cms/wordpress: `mseo wp ...`, WordPress REST helpers (ported from the earlier house WordPress helper).

The site is the workspace's cms.site and nothing else. The credential is Keychain item mseo.<slug>.wordpress
(payload {"site", "user", "app_password"}, stored with `mseo secret set`); if its site differs from cms.site the
command stops with exit 3 before any request. No env file, no environment variables, no default site.

    mseo wp verify --workspace S
    mseo wp post-create FILE --workspace S --slug SLUG [--publish] [--replace] [--dry-run]
    mseo wp page-create FILE --workspace S --slug SLUG --title T [--publish] [--replace] [--dry-run]
    mseo wp snippet-deploy FILE --workspace S [--title T] [--location body-end] [--replace] [--dry-run]
    mseo wp schema-deploy --workspace S [--dry-run]          # site-wide business JSON-LD from customer.json + facts
    mseo wp posts-refresh --workspace S [--dry-run]

The password is never printed or included in --json output; only a masked fingerprint is shown.
Exit codes: 0 ok · 1 refused (placeholders, read-back problems) · 2 already exists (pass --replace) ·
3 could not run (missing or mismatched credential, site unreachable, file outside the workspace).

What is writable (verified on a live site): wp/v2/pages, wp/v2/posts and
wp/v2/elementor_snippet. wp/v2/elementor_library (footer and header templates) is read-only.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mseo import http as http_mod
from mseo.content import md as md_mod
from mseo.content import places
from mseo.fsutil import write_json_atomic
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_FAIL, EXIT_OK
from mseo.workspace import WorkspaceError

TIMEOUT = 30
USER_AGENT = "mseo/1.0"
SOURCE = "wordpress"
PAGE_PLACEHOLDERS = ("###VERIFY###", "###PHONE###", "###PHONE_DISPLAY###")
ELEMENTOR_LOCATIONS = {"head": "elementor_head", "body-start": "elementor_body_start", "body-end": "elementor_body_end"}
WRITE_TARGETS = [("pages", "wp/v2/pages", "Landing pages"), ("posts", "wp/v2/posts", "Blog posts"),
                 ("elementor_snippet", "wp/v2/elementor_snippet", "Elementor Custom Code")]
CAPABILITIES = ("edit_posts", "publish_posts", "edit_pages", "publish_pages", "unfiltered_html")


class WPError(Exception):
    """A REST call failed. The message is safe to print (never carries the secret)."""


class SiteMismatch(WorkspaceError):
    """The credential's site is not the workspace's cms.site. Maps to exit 3."""


# ----------------------------------------------------------------- credentials

def site_of(ws) -> str:
    return ws.config["cms"]["site"].rstrip("/")


def credentials(ws) -> tuple[str, str, str]:
    """(site, user, app_password) for the workspace. MissingCredential or SiteMismatch (both exit 3)."""
    payload = ws.secret(SOURCE)
    site = site_of(ws)
    if str(payload.get("site", "")).rstrip("/") != site:
        raise SiteMismatch(f"Keychain item mseo.{ws.slug}.{SOURCE} is for {payload.get('site')!r}, but cms.site is "
                           f"{site!r}: nothing was sent. Re-store it with mseo secret set --replace")
    # WordPress prints application passwords space-separated; the spaces are cosmetic.
    return site, payload["user"], str(payload["app_password"]).replace(" ", "")


def fingerprint(secret: str) -> str:
    return "****" if len(secret) < 6 else f"{secret[:2]}{'*' * 8}{secret[-2:]}"


# ----------------------------------------------------------------- transport

class NoRedirect(http_mod.NoRedirect):
    """A site that redirects its REST API must be fixed in cms.site instead."""

    error_type = WPError
    hint = "Set cms.site to the final address."


OPENER = http_mod.opener(NoRedirect)


def request(site: str, user: str, pw: str, method: str, path: str,
            payload: dict | None = None, query: dict | None = None) -> tuple[int, object]:
    if not site.lower().startswith("https://"):
        raise WPError(f"refusing to send credentials to {site}: cms.site must use https:// to protect credentials")
    url = f"{site}/wp-json/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with OPENER.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return _http_error(exc, site, method, path)
    except urllib.error.URLError as exc:
        raise WPError(f"could not reach {site}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise WPError(f"could not reach {site}: {exc}") from None
    try:
        return resp.status, (json.loads(raw) if raw else None)
    except ValueError:
        raise WPError(f"{method} {path} on {site} did not answer JSON: {raw[:120]!r}") from None


def _http_error(exc, site, method, path):
    raw = exc.read().decode("utf-8", "replace")
    try:
        detail = json.loads(raw).get("message", raw[:300])
    except (json.JSONDecodeError, AttributeError):
        detail = raw[:300]
    if exc.code == 401:
        raise WPError("401 unauthorized: the application password was rejected. Check the stored user is the "
                      "username (not the email) and the password has not been revoked.") from None
    if exc.code == 403:
        raise WPError(f"403 forbidden: that user lacks the capability. {detail}") from None
    raise WPError(f"HTTP {exc.code} on {method} {path}: {detail}") from None


def public_status(url: str) -> int | None:
    if not (url.startswith("https://") or url.startswith("http://")):
        return None
    # No credentials are sent here, so redirects are followed (urllib follows http/https/ftp only); a site that
    # moved to www or added a trailing slash still reports its real status.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return None


def fetch_published(site: str) -> list[dict]:
    """Every published post from the public REST API (no credentials needed), all pages."""
    items, page = [], 1
    while True:
        q = urllib.parse.urlencode({"status": "publish", "per_page": 100, "page": page,
                                    "_fields": "id,slug,date,status,title,content,link"})
        req = urllib.request.Request(f"{site}/wp-json/wp/v2/posts?{q}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
                batch, pages = json.load(r), int(r.headers.get("X-WP-TotalPages", "1"))
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and page > 1:  # WordPress answers past the last page with 400
                return items
            raise
        items += batch
        if not batch or page >= pages:
            return items
        page += 1


# ----------------------------------------------------------------- helpers

def result(code: int, status: str, messages, **extra) -> tuple[int, dict]:
    return code, {"status": status, "messages": list(messages), **extra}


def _source(ws, file) -> Path:
    path = ws.contains(file)
    if not path.is_file():
        raise WPError(f"{path} not found")
    return path


def _existing(site, user, pw, route: str, slug: str, dry_run: bool):
    """(existing item or None, checked). A dry run tolerates an unreachable site; a real run does not."""
    try:
        _, found = request(site, user, pw, "GET", route, query={"slug": slug, "status": "any", "context": "edit"})
    except WPError:
        if not dry_run:
            raise
        return None, False
    return (found[0] if found else None), True


_JS_LINE_COMMENT = re.compile(r"^[ \t]*//.*\n?", re.M)


def strip_comments(markup: str) -> str:
    """Drop HTML comments and whole-line // comments inside <script> blocks; '//' inside strings is never touched."""
    out = re.sub(r"<!--.*?-->[ \t]*\n?", "", markup, flags=re.S)
    out = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)",
                 lambda m: m.group(1) + _JS_LINE_COMMENT.sub("", m.group(2)) + m.group(3), out, flags=re.S | re.I)
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def _plain(markup: str) -> str:
    text = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html_mod.unescape(text).replace("\xa0", " ")).strip()


_JSONLD_SCRIPT_RE = re.compile(r'<script type="application/ld\+json">.*?</script>\s*', re.S)


def _without_jsonld(markup: str) -> str:
    """The post body with its JSON-LD blocks removed: what decides whether the content changed (the Article block's
    own dates must not make every re-publish look like an edit)."""
    return _JSONLD_SCRIPT_RE.sub("", markup or "").strip()


def _log_entry(ws, slug: str) -> dict:
    """The article's content-log entry, or {} when the log or the entry is missing (the dates then fall back)."""
    try:
        log = json.loads(ws.file("content_log").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = log.get("articles", []) if isinstance(log, dict) else []
    return next((a for a in reversed(entries) if isinstance(a, dict) and a.get("slug") == slug), {}) or {}


def article_dates(entry: dict, existing: dict | None, changed: bool, today_iso: str) -> tuple[str, str]:
    """(datePublished, dateModified) for the Article block. Published: the existing WordPress post's date on an
    update, else the content-log entry's date, else today. Modified: today only when the body changed (or the post is
    new and has no earlier date); an unchanged re-publish keeps the post's modified date (or its publish date)."""
    existing = existing or {}
    published = str(existing.get("date") or "")[:10] or str(entry.get("date") or "")[:10] or today_iso
    if existing:
        if changed:
            return published, today_iso
        return published, str(existing.get("modified") or "")[:10] or published
    return published, published


def posts_from_api(items: list[dict]) -> list[dict]:
    """REST API posts -> the list the duplicate check reads (files.wp_posts)."""
    return [{"id": i["id"], "slug": i["slug"], "date": i["date"][:10], "status": i["status"],
             "title": _plain(i["title"]["rendered"]), "text": _plain(i["content"]["rendered"]), "link": i["link"]}
            for i in items]


# ----------------------------------------------------------------- commands (in-process API)

def verify(ws) -> tuple[int, dict]:
    site, user, pw = credentials(ws)
    _, me = request(site, user, pw, "GET", "wp/v2/users/me", query={"context": "edit"})
    caps = (me or {}).get("capabilities", {}) or {}
    checks = [{"capability": c, "granted": bool(caps.get(c))} for c in CAPABILITIES]
    targets = []
    for name, route, _ in WRITE_TARGETS:
        try:
            status, _ = request(site, user, pw, "GET", route, query={"per_page": 1})
            reachable = status == 200
        except WPError:
            reachable = False
        targets.append({"name": name, "route": route, "reachable": reachable})
    msgs = [f"Site       {site}", f"User       {user}", f"Credential {fingerprint(pw)}", "",
            f"Authenticated as: {(me or {}).get('name')} (id {(me or {}).get('id')})",
            f"Roles: {', '.join((me or {}).get('roles', [])) or 'unknown'}", ""]
    msgs += [f"  [{'x' if c['granted'] else ' '}] {c['capability']}" for c in checks]
    msgs += ["", "Write targets:"] + [f"  [{'x' if t['reachable'] else ' '}] {label:24} {route}"
                                      for t, (_, route, label) in zip(targets, WRITE_TARGETS, strict=True)]
    msgs += ["", "  [ ] Elementor footer template   wp/v2/elementor_library  (read-only, no POST endpoint)"]
    return result(EXIT_OK, "ok", msgs, site=site, user=user, credential_fingerprint=fingerprint(pw),
                  capabilities=checks, targets=targets)


def post_create(ws, file, slug: str, *, title=None, excerpt=None, category=None, publish=False, replace=False,
                dry_run=False, allow_placeholders=False, data: bytes | None = None) -> tuple[int, dict]:
    """Publish a blog post from AEO Markdown (or raw HTML) to cms.site, then read it back. `data`: the exact bytes
    to post instead of re-reading the file (publish passes the bytes it checked)."""
    from mseo.doctor import today

    src = _source(ws, file)
    text = data.decode("utf-8") if data is not None else src.read_text(encoding="utf-8")
    is_md = src.suffix.lower() in (".md", ".markdown")
    entry = _log_entry(ws, slug) if is_md else {}

    def render(date: str, modified: str):
        return md_mod.convert(text, **md_mod.options(ws, slug=slug, date=date, modified=modified,
                                                      image=entry.get("image") or None))

    try:
        if is_md:
            doc = render(today().isoformat(), today().isoformat())  # the dates are settled once the site answered
            t, ex, content, has_schema = doc.title, doc.excerpt, doc.html, doc.schema is not None
            placeholders = list(doc.placeholders)
        else:
            if not title:
                return result(EXIT_FAIL, "error", ["--title is required for HTML input"])
            t, ex, content = title, "", text
            has_schema = "application/ld+json" in text
            placeholders = [m for m in md_mod.PLACEHOLDER_MARKERS if m in text]
    except md_mod.MarkdownError as exc:
        return result(EXIT_FAIL, "error", [f"error: {exc}"])
    title, excerpt = title or t, excerpt or ex
    if placeholders and not allow_placeholders:
        return result(EXIT_FAIL, "error", [f"Refusing to publish: unresolved markers {', '.join(placeholders)}"],
                      placeholders=placeholders)
    if "<!--" in content:
        return result(EXIT_FAIL, "error", ["content still contains an HTML comment; comments are served to crawlers "
                                           "and must not ship"])
    site, user, pw = credentials(ws)
    existing, checked = _existing(site, user, pw, "wp/v2/posts", slug, dry_run)
    action = "update" if existing else "create"
    if existing and not replace:
        return result(EXIT_EXISTS, "exists", [f"Post /{slug}/ exists (id {existing.get('id')}). Use --replace to overwrite."],
                      slug=slug, id=existing.get("id"))
    state = "publish" if publish else "draft"
    dates = {}
    if is_md:
        old_body = _without_jsonld(((existing or {}).get("content") or {}).get("raw", ""))
        changed = old_body != _without_jsonld(content)
        published, modified = article_dates(entry, existing, changed, today().isoformat())
        content = render(published, modified).html
        dates = {"date_published": published, "date_modified": modified, "content_changed": changed}
    summary = {"site": site, "action": action, "slug": slug, "title": title, "excerpt": excerpt, "bytes": len(content),
               "schema": has_schema, "publish_state": state, **dates}
    if dry_run:
        return result(EXIT_OK, "dry-run", [f"DRY RUN: would {action} post /{slug}/ on {site}", f"  title   {title}",
                                           f"  excerpt {excerpt[:90]}",
                                           f"  bytes   {len(content)}   schema {'yes' if has_schema else 'no'}",
                                           f"  status  {state}"], existence_checked=checked, **summary)
    payload = {"title": title, "slug": slug, "content": content, "status": state}
    if excerpt:
        payload["excerpt"] = excerpt
    if category:
        payload["categories"] = [category]
    _, res = request(site, user, pw, "POST", f"wp/v2/posts/{existing['id']}" if existing else "wp/v2/posts", payload)
    post_id = (res or {}).get("id")
    # Read back: a 200 on write proves nothing.
    try:
        _, back = request(site, user, pw, "GET", f"wp/v2/posts/{post_id}", query={"context": "edit"})
    except WPError as exc:  # the post exists: report its id so publish can log it and resume
        return result(EXIT_FAIL, "error", [f"{action.title()}d post /{slug}/ (id {post_id}) on {site}, but the read-back "
                                           f"failed: {exc}"], id=post_id, link=(res or {}).get("link"),
                      problems=[f"read-back failed: {exc}"], **summary)
    problems = []
    raw = ((back or {}).get("content") or {}).get("raw", "")
    if not raw.strip():
        problems.append("read-back content is empty")
    if has_schema and "application/ld+json" not in raw:
        problems.append("FAQ schema was stripped on save (user may lack unfiltered_html)")
    if (back or {}).get("status") != state:
        problems.append(f"status is {(back or {}).get('status')!r}, expected {state!r}")
    link = (back or {}).get("link")
    public = public_status(link) if state == "publish" and link else None
    if state == "publish" and public != 200:
        problems.append(f"public URL returned {public}")
    msgs = [f"{action.title()}d post /{slug}/ (id {post_id}) on {site}: {state}", f"  {link}"]
    msgs += ([f"  live check: HTTP {public}"] if public else []) + [f"  PROBLEM: {p}" for p in problems]
    return result(EXIT_FAIL if problems else EXIT_OK, "error" if problems else "ok", msgs, id=post_id, link=link,
                  public_status=public, problems=problems, **summary)


def page_create(ws, file, slug: str, title: str, *, publish=False, replace=False, dry_run=False,
                allow_placeholders=False) -> tuple[int, dict]:
    src = _source(ws, file)
    content = src.read_text(encoding="utf-8")
    leftovers = [m for m in PAGE_PLACEHOLDERS if m in content]
    if leftovers and not allow_placeholders:
        return result(EXIT_FAIL, "error", [f"Refusing to publish: unresolved placeholders {', '.join(leftovers)}"],
                      placeholders=leftovers)
    site, user, pw = credentials(ws)
    existing, checked = _existing(site, user, pw, "wp/v2/pages", slug, dry_run)
    action = "update" if existing else "create"
    if existing and not replace:
        return result(EXIT_EXISTS, "exists", [f"Page /{slug}/ exists (id {existing.get('id')}). Use --replace to overwrite."],
                      slug=slug, id=existing.get("id"))
    state = "publish" if publish else "draft"
    if dry_run:
        return result(EXIT_OK, "dry-run", [f"DRY RUN: would {action} page /{slug}/ on {site}", f"  title  {title}",
                                           f"  bytes  {len(content)}", f"  status {state}"],
                      site=site, action=action, slug=slug, title=title, bytes=len(content), publish_state=state,
                      existence_checked=checked)
    payload = {"title": title, "slug": slug, "content": content, "status": state}
    _, res = request(site, user, pw, "POST", f"wp/v2/pages/{existing['id']}" if existing else "wp/v2/pages", payload)
    page_id = (res or {}).get("id")
    # Read back: a 200 on write proves nothing .
    try:
        _, back = request(site, user, pw, "GET", f"wp/v2/pages/{page_id}", query={"context": "edit"})
    except WPError as exc:
        return result(EXIT_FAIL, "error", [f"{action.title()}d page /{slug}/ (id {page_id}) on {site}, but the read-back failed: {exc}"],
                      id=page_id, link=(res or {}).get("link"), problems=[f"read-back failed: {exc}"], site=site, action=action,
                      slug=slug, publish_state=state)
    problems = []
    if not (((back or {}).get("content") or {}).get("raw", "")).strip():
        problems.append("read-back content is empty")
    if (back or {}).get("status") != state:
        problems.append(f"status is {(back or {}).get('status')!r}, expected {state!r}")
    link = (back or {}).get("link") or (res or {}).get("link")
    public = public_status(link) if state == "publish" and link else None
    if state == "publish" and public != 200:
        problems.append(f"public URL returned {public}")
    msgs = [f"{action.title()}d page /{slug}/ (id {page_id}) on {site}", f"  {link}", f"  status {state}"]
    msgs += ([f"  live check: HTTP {public}"] if public else []) + [f"  PROBLEM: {p}" for p in problems]
    return result(EXIT_FAIL if problems else EXIT_OK, "error" if problems else "ok", msgs,
                  site=site, action=action, id=page_id, slug=slug, link=link, publish_state=state,
                  public_status=public, problems=problems)


def _find_snippet(site, user, pw, title):
    # status=any: WordPress omits drafts from listings by default, which made repeat deploys create duplicates.
    _, items = request(site, user, pw, "GET", "wp/v2/elementor_snippet",
                       query={"search": title, "per_page": 20, "context": "edit", "status": "any"})
    for item in items or []:
        t = item.get("title") or {}
        if (t.get("raw") or t.get("rendered")) == title:
            return item
    return None


def snippet_deploy(ws, file, *, title=None, location="body-end", publish=False, priority=10, replace=False,
                   dry_run=False, keep_comments=False, content: str | None = None) -> tuple[int, dict]:
    """Deploy an Elementor Custom Code snippet from `file`, or from `content` (then `title` is required and no file is
    read: schema-deploy renders its markup in memory so --dry-run writes nothing)."""
    if content is None:
        src = _source(ws, file)
        content = src.read_text(encoding="utf-8")
        title = title or src.stem
    elif not title:
        raise WPError("a snippet deployed from rendered content needs a title")
    if not keep_comments:
        content = strip_comments(content)
    site, user, pw = credentials(ws)
    existing, checked = None, True
    try:
        existing = _find_snippet(site, user, pw, title)
    except WPError:
        if not dry_run:
            raise
        checked = False
    action = "update" if existing else "create"
    if existing and not replace:
        return result(EXIT_EXISTS, "exists", [f"Snippet '{title}' already exists (id {existing.get('id')}). "
                                              "Use --replace to overwrite."], title=title, id=existing.get("id"))
    if dry_run:
        return result(EXIT_OK, "dry-run", [f"DRY RUN: would {action} snippet '{title}' on {site}", f"  bytes    {len(content)}",
                                           f"  location {location}", f"  status   {'publish' if publish else 'draft'}"],
                      site=site, action=action, title=title, location=location, bytes=len(content),
                      existing_id=(existing or {}).get("id"), existence_checked=checked)
    payload = {"title": title, "status": "publish" if publish else "draft",
               "meta": {"_elementor_code": content, "_elementor_location": ELEMENTOR_LOCATIONS[location],
                        "_elementor_priority": priority}}
    route = f"wp/v2/elementor_snippet/{existing['id']}" if existing else "wp/v2/elementor_snippet"
    _, res = request(site, user, pw, "POST", route, payload)
    sid = (res or {}).get("id")
    # Read back: the code and location must be what was sent .
    try:
        _, back = request(site, user, pw, "GET", f"wp/v2/elementor_snippet/{sid}", query={"context": "edit"})
    except WPError as exc:
        return result(EXIT_FAIL, "error", [f"{action.title()}d snippet '{title}' (id {sid}) on {site}, but the read-back failed: {exc}"],
                      id=sid, title=title, location=location, published=publish, problems=[f"read-back failed: {exc}"], site=site, action=action)
    meta = (back or {}).get("meta") or {}
    problems = []
    if (meta.get("_elementor_code") or "") != content:
        problems.append("read-back code differs from what was sent")
    if meta.get("_elementor_location") != ELEMENTOR_LOCATIONS[location]:
        problems.append(f"read-back location is {meta.get('_elementor_location')!r}, expected {ELEMENTOR_LOCATIONS[location]!r}")
    wanted = "publish" if publish else "draft"
    if (back or {}).get("status") != wanted:
        problems.append(f"status is {(back or {}).get('status')!r}, expected {wanted!r}")
    msgs = [f"{action.title()}d snippet '{title}' (id {sid}) on {site}", f"  location {location}",
            f"  status   {'published' if publish else 'draft'}"] + [f"  PROBLEM: {p}" for p in problems]
    return result(EXIT_FAIL if problems else EXIT_OK, "error" if problems else "ok", msgs,
                  site=site, action=action, id=sid, title=title, location=location, published=publish,
                  link=(res or {}).get("link"), problems=problems)


# ----------------------------------------------------------------- site-wide business schema

SCHEMA_SNIPPET_TITLE = "mseo business schema"
DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
             "sat": "Saturday", "sun": "Sunday"}
DAY_ORDER = list(DAY_NAMES)
DEFAULT_COUNTRY = "US"
CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class FactsInvalid(ValueError):
    """A business fact cannot go into site-wide markup: a clock that is not HH:MM, or text that carries '<'."""


def postal_address(text: str, country: str = DEFAULT_COUNTRY, city: str | None = None) -> dict:
    """'<street>, <city>, <ST> <zip>' -> PostalAddress parts; anything else keeps the whole line as streetAddress.
    `city` (customer.json locations[].city) wins over the derived locality."""
    out = {"@type": "PostalAddress"}
    parts = places.address_parts(text)
    if not parts:
        out["streetAddress"] = text.strip()
        if city:
            out["addressLocality"] = city
        return out
    out.update(streetAddress=parts["street"], addressLocality=city or parts["city"].strip(), addressRegion=parts["region"],
               postalCode=parts["postal"], addressCountry=country)
    return out


def _clean_text(value, what: str) -> str:
    """A fact as plain text for markup: no '<' or '>' (a value that could open a tag is refused)."""
    text = str(value if value is not None else "").strip()
    if "<" in text or ">" in text:
        raise FactsInvalid(f"{what} contains markup characters and cannot go into the business schema: {text[:60]!r}")
    return text


def days_of_week(spec: str) -> list[str]:
    """'Mon-Thu' -> Monday..Thursday; 'Sat-Sun', 'Fri', 'Mon, Wed' all work. Unknown names are skipped."""
    out: list[str] = []
    for part in re.split(r"[,/]", spec):
        part = part.strip().lower()
        if "-" in part:
            a, b = (p.strip()[:3] for p in part.split("-", 1))
            if a in DAY_ORDER and b in DAY_ORDER:
                i, j = DAY_ORDER.index(a), DAY_ORDER.index(b)
                span = DAY_ORDER[i:j + 1] if i <= j else DAY_ORDER[i:] + DAY_ORDER[:j + 1]
                out += [DAY_NAMES[d] for d in span]
        elif part[:3] in DAY_NAMES:
            out.append(DAY_NAMES[part[:3]])
    return out


def opening_hours(hours: list) -> list[dict]:
    out = []
    for h in hours or []:
        if not isinstance(h, dict) or h.get("closed") or not (h.get("opens") and h.get("closes")):
            continue
        opens, closes = str(h["opens"]).strip(), str(h["closes"]).strip()
        for clock in (opens, closes):
            if not CLOCK_RE.match(clock):
                raise FactsInvalid(f"facts.json business.hours has {clock!r}; opens and closes must be HH:MM")
        days = days_of_week(_clean_text(h.get("days", ""), "business.hours.days"))
        if days:
            out.append({"@type": "OpeningHoursSpecification", "dayOfWeek": days, "opens": opens, "closes": closes})
    return out


def business_schema(ws, facts: dict | None = None) -> dict:
    """The site-wide JSON-LD for business.schema_type from customer.json (name, site, phone, locations) and facts.json
    (business.hours). No field is invented: a missing part is left out."""
    biz = ws.config["business"]
    country = biz.get("country") or DEFAULT_COUNTRY
    locations = [dict(l) for l in ws.config.get("locations") or []]
    addresses = [postal_address(_clean_text(l["address"], "locations[].address"), country,
                                _clean_text(l.get("city"), "locations[].city") or None)
                 for l in locations if l.get("address")]
    cities = []
    for a in addresses:
        city = a.get("addressLocality")
        if city and city not in cities:
            cities.append(city)
    out = {"@context": "https://schema.org", "@type": biz.get("schema_type") or "LocalBusiness",
           "name": _clean_text(biz["name"], "business.name"), "url": biz["site"].rstrip("/") + "/",
           "telephone": _clean_text(biz["phone"], "business.phone")}
    if addresses:
        out["address"] = addresses[0] if len(addresses) == 1 else addresses
    if cities:
        out["areaServed"] = [{"@type": "City", "name": c} for c in cities]
    hours = opening_hours(((facts or {}).get("business") or {}).get("hours") or [])
    if hours:
        out["openingHoursSpecification"] = hours
    return out


def schema_markup(data: dict) -> str:
    """The head snippet. '<', '>' and '&' in any value are \\u-escaped (still JSON), so a fact can never close the
    script tag."""
    return '<script type="application/ld+json">\n' + md_mod.jsonld_text(data, indent=2) + "\n</script>\n"


def schema_deploy(ws, *, dry_run=False) -> tuple[int, dict]:
    """Render the business JSON-LD and deploy it as the published head snippet SCHEMA_SNIPPET_TITLE (replacing the
    earlier one), with snippet_deploy's read-back. The markup is in the result so a dry run shows what would ship."""
    try:
        facts = json.loads(ws.file("facts").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return result(EXIT_COULD_NOT_RUN, "error", [f"facts file could not be read: {exc}"])
    try:
        data = business_schema(ws, facts if isinstance(facts, dict) else {})
    except FactsInvalid as exc:
        return result(EXIT_FAIL, "error", [f"refused: {exc}; nothing was sent"])
    markup = schema_markup(data)
    code, payload = snippet_deploy(ws, None, title=SCHEMA_SNIPPET_TITLE, location="head", publish=True, replace=True,
                                   dry_run=dry_run, keep_comments=True, content=markup)
    address = data.get("address")
    n_addresses = len(address) if isinstance(address, list) else (1 if address else 0)
    payload["schema_type"] = data["@type"]
    payload["markup"] = markup
    payload["messages"] = [f"schema {data['@type']} for {ws.config['business']['name']}: {n_addresses} address(es), "
                           f"{len(data.get('openingHoursSpecification', []))} hours rule(s)"] + payload["messages"]
    return code, payload


def posts_refresh(ws, *, dry_run=False) -> tuple[int, dict]:
    site, out = site_of(ws), ws.file("wp_posts")
    try:
        posts = posts_from_api(fetch_published(site))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return result(EXIT_COULD_NOT_RUN, "error", [f"could not fetch posts from {site}: {exc}"])
    if not posts:  # an empty answer is a fault, never a reason to wipe the list
        return result(EXIT_COULD_NOT_RUN, "error", [f"{site} returned no published posts; the list was not changed"])
    newest = max(p["date"] for p in posts)
    if not dry_run:
        write_json_atomic(out, posts, indent=1, ensure_ascii=False)
    return result(EXIT_OK, "dry-run" if dry_run else "ok",
                  [f"{'would write' if dry_run else 'wrote'} {len(posts)} post(s) from {site}, newest {newest}, to {out}"],
                  site=site, posts=len(posts), newest=newest, path=str(out))


# ----------------------------------------------------------------- CLI

def _cli(fn, writes: bool = False):
    def handler(args, ctx) -> int:
        ws = ctx.open(args.workspace)
        if writes and not getattr(args, "dry_run", False):
            from mseo import seal as seal_mod

            seal_mod.require_current(ws)  # a stale or missing seal stops a write to the customer's site (audit L1)
        try:
            code, payload = fn(ws, args)
        except WPError as exc:
            code, payload = result(EXIT_COULD_NOT_RUN, "error", [f"error: {exc}"])
        ctx.emit(payload)
        return code
    return handler


def register(sub, parents) -> None:
    p = sub.add_parser("wp", help="Deploy content to the workspace's WordPress site over the REST API")
    wsub = p.add_subparsers(dest="wp_command", required=True, parser_class=type(p))
    common = [parents.workspace, parents.json]
    writes = common + [parents.dry_run]

    v = wsub.add_parser("verify", parents=common, help="Check the credential, capabilities and writable targets")
    v.set_defaults(func=_cli(lambda ws, a: verify(ws)), command_id="wp verify")

    pc = wsub.add_parser("post-create", parents=writes, help="Create or update a blog post from AEO Markdown or HTML")
    pc.add_argument("file")
    pc.add_argument("--slug", required=True)
    pc.add_argument("--title")
    pc.add_argument("--excerpt")
    pc.add_argument("--category", type=int)
    pc.add_argument("--publish", action="store_true", help="Publish (default: create as draft)")
    pc.add_argument("--replace", action="store_true", help="Overwrite an existing post with that slug")
    pc.add_argument("--allow-placeholders", action="store_true")
    pc.set_defaults(func=_cli(lambda ws, a: post_create(
        ws, a.file, a.slug, title=a.title, excerpt=a.excerpt, category=a.category, publish=a.publish, replace=a.replace,
        dry_run=a.dry_run, allow_placeholders=a.allow_placeholders), writes=True), command_id="wp post-create")

    g = wsub.add_parser("page-create", parents=writes, help="Create or update a page from an HTML file")
    g.add_argument("file")
    g.add_argument("--slug", required=True)
    g.add_argument("--title", required=True)
    g.add_argument("--publish", action="store_true")
    g.add_argument("--replace", action="store_true")
    g.add_argument("--allow-placeholders", action="store_true")
    g.set_defaults(func=_cli(lambda ws, a: page_create(
        ws, a.file, a.slug, a.title, publish=a.publish, replace=a.replace, dry_run=a.dry_run,
        allow_placeholders=a.allow_placeholders), writes=True), command_id="wp page-create")

    s = wsub.add_parser("snippet-deploy", parents=writes, help="Create or update an Elementor Custom Code snippet")
    s.add_argument("file")
    s.add_argument("--title")
    s.add_argument("--location", default="body-end", choices=list(ELEMENTOR_LOCATIONS))
    s.add_argument("--publish", action="store_true")
    s.add_argument("--priority", type=int, default=10)
    s.add_argument("--replace", action="store_true")
    s.add_argument("--keep-comments", action="store_true")
    s.set_defaults(func=_cli(lambda ws, a: snippet_deploy(
        ws, a.file, title=a.title, location=a.location, publish=a.publish, priority=a.priority, replace=a.replace,
        dry_run=a.dry_run, keep_comments=a.keep_comments), writes=True), command_id="wp snippet-deploy")

    sd = wsub.add_parser("schema-deploy", parents=writes,
                         help="Deploy the site-wide business JSON-LD (business.schema_type) as a head snippet")
    sd.set_defaults(func=_cli(lambda ws, a: schema_deploy(ws, dry_run=a.dry_run), writes=True), command_id="wp schema-deploy")

    pr = wsub.add_parser("posts-refresh", parents=writes, help="Refresh the published-posts list the duplicate check reads")
    pr.set_defaults(func=_cli(lambda ws, a: posts_refresh(ws, dry_run=a.dry_run)), command_id="wp posts-refresh")
