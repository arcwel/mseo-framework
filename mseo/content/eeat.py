"""content/eeat: the `eeat` gate (articles only). An answer engine quotes a page it can attribute, so every article
names its author (who must exist in facts.json with a bio URL), cites a primary source in its sources block (off the
business's own site, unless the research sidecar marks the business's own confirmed page with "source_kind":
"own-confirmed"), names the sidecar's `used` item in that block, carries an image with alt text or a labelled
illustration caption (or the sidecar says why there is none), and shows a review date that is not stale.

Everything is read from the workspace: the author from facts.json `author`, the site from customer.json, the
sources count from recipes.json `sources_min`. Returns (passed, detail); ValueError when it cannot run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse

from mseo.content import recipes, variation

REVIEWED_RE = re.compile(r"(?:reviewed|updated|datemodified|date modified)\W{0,3}(?:on\s+)?(\d{4}-\d{2}-\d{2})", re.I)
MAX_REVIEW_AGE_DAYS = 400
NO_IMAGE = "none"
OWN_CONFIRMED = "own-confirmed"


def author_of(ws) -> dict:
    try:
        facts = json.loads(ws.file("facts").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"facts file could not be read ({exc})") from exc
    author = facts.get("author") if isinstance(facts, dict) else None
    if not isinstance(author, dict) or not author.get("name") or not author.get("url"):
        raise ValueError("facts.json has no author block with name and url; add it before an article can pass eeat")
    return author


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")


def link_targets(text: str) -> list[str]:
    return [m.group(2) for m in variation.LINK_RE.finditer(text)]


def links_to(text: str, url: str) -> bool:
    """True when the text links to `url`: the same absolute URL or its root-relative path (trailing slash aside)."""
    want_path = urllib.parse.urlparse(url).path.rstrip("/") or "/"
    for target in link_targets(text):
        if target.rstrip("/") == url.rstrip("/"):
            return True
        if target.startswith("/") and target.rstrip("/") == want_path.rstrip("/"):
            return True
    return False


def primary_sources(parsed, site: str, own_confirmed: bool = False) -> list[str]:
    """URLs in the sources block that count as primary: off the business's own host, or on it when the sidecar says
    the business's own page was confirmed ("source_kind": "own-confirmed")."""
    own = _host(site)
    return [u for u in variation.source_urls(parsed)
            if _host(u) and (own_confirmed or _host(u) != own) and _host(u) not in ("schema.org",)]


def image_evidence(text: str, sidecar: dict | None) -> tuple[bool, str]:
    found = variation.image_evidence_in(text)
    if found:
        return True, found
    if isinstance(sidecar, dict) and str(sidecar.get("image", "")).lower() == NO_IMAGE:
        reason = str(sidecar.get("image_reason") or sidecar.get("image_why") or "").strip()
        if reason:
            return True, f"no image, sidecar says why: {reason}"
        return False, 'sidecar says "image": "none" but gives no image_reason'
    return False, ("no image markdown with alt text, no caption line starting with 'Illustration:', and the research "
                   "sidecar does not say why")


def review_date(author_text: str, today: dt.date) -> tuple[bool, str]:
    m = REVIEWED_RE.search(author_text)
    if not m:
        return False, "author block has no 'Reviewed YYYY-MM-DD' (or dateModified) date"
    try:
        when = dt.date.fromisoformat(m.group(1))
    except ValueError:
        return False, f"review date {m.group(1)!r} is not a date"
    age = (today - when).days
    if age > MAX_REVIEW_AGE_DAYS:
        return False, f"reviewed {when}, {age} days ago; review and re-date it (limit {MAX_REVIEW_AGE_DAYS} days)"
    return True, f"reviewed {when}"


def check_draft(draft, ws, today: dt.date) -> tuple[bool, str]:
    author = author_of(ws)
    parsed = variation.parse(draft.text, variation.footers_of(ws))
    blocks = parsed.named("author")
    if not blocks:
        return False, "no <!-- block: author --> section"
    author_text = "\n".join(b.text for b in blocks)
    if author["name"].lower() not in variation.plain(author_text).lower():
        return False, f"author block does not name {author['name']} (facts.json author.name)"
    if not links_to(author_text, author["url"]):
        return False, f"author block has no link to {author['url']} (facts.json author.url)"
    sidecar = None
    used = ""
    try:
        sidecar = json.loads(ws.contains(draft.sidecar(".research.json")).read_text(encoding="utf-8"))
        used = str(sidecar.get("used", "")) if isinstance(sidecar, dict) else ""
    except Exception:  # noqa: BLE001 - the research gate reports a broken sidecar
        sidecar = None
    need = int(recipes.article().get("sources_min", 1))
    own_confirmed = isinstance(sidecar, dict) and str(sidecar.get("source_kind", "")).lower() == OWN_CONFIRMED
    sources = primary_sources(parsed, ws.config["business"]["site"], own_confirmed)
    if len(sources) < need:
        where = "in the sources block" if own_confirmed else "off the business's own site in the sources block"
        return False, f"sources: {len(sources)} primary source(s) {where}, needs {need}"
    if not variation.used_in_sources(parsed, used):
        return False, f"sources: the research sidecar's used item {used!r} is not in the sources block (README item 10)"
    ok, why = image_evidence(parsed.body_text, sidecar)
    if not ok:
        return False, f"image: {why}"
    ok, when = review_date(author_text, today)
    if not ok:
        return False, f"date: {when}"
    return True, f"author {author['name']}, {len(sources)} primary source(s), {why}, {when}"
