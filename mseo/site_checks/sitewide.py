"""Site-wide checks: robots.txt, sitemaps, the http -> https redirect, www consistency and Core Web Vitals through
PageSpeed Insights v5. Everything is fetched through the audit's injectable fetcher; the PSI key never reaches a
finding (the stored URL carries no key)."""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from mseo.site_checks.page import finding, same_url

GOOGLEBOT = "googlebot"
AI_CRAWLERS = ("Google-Extended", "GPTBot", "ClaudeBot", "PerplexityBot")
ASSET_PATH = re.compile(r"\.(css|js)(\?|$)|/(css|js|assets|static|wp-includes|wp-content/(themes|plugins|uploads))(/|$)", re.I)
SITEMAP_DEFAULTS = ("/sitemap.xml", "/sitemap_index.xml")
SITEMAP_CHILD_CAP = 20
LASTMOD_WITHIN = dt.timedelta(days=60)
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CWV = {"LCP": ("LARGEST_CONTENTFUL_PAINT_MS", "largest-contentful-paint", 2500, "ms"),
       "INP": ("INTERACTION_TO_NEXT_PAINT", "interaction-to-next-paint", 200, "ms"),
       "CLS": ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "cumulative-layout-shift", 0.1, "")}


# ---------------------------------------------------------------- robots.txt

def parse_robots(text: str) -> dict:
    """{"groups": {agent_lower: {"allow": [...], "disallow": [...]}}, "sitemaps": [...]}."""
    groups: dict[str, dict] = {}
    sitemaps: list[str] = []
    current: list[str] = []
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            sitemaps.append(value)
        elif key == "user-agent":
            if not last_was_agent:
                current = []
            current.append(value.lower())
            groups.setdefault(value.lower(), {"allow": [], "disallow": []})
            last_was_agent = True
        elif key in ("allow", "disallow"):
            for agent in current:
                groups[agent][key].append(value)
            last_was_agent = False
        else:
            last_was_agent = False
    return {"groups": groups, "sitemaps": sitemaps}


def group_for(robots: dict, agent: str) -> dict | None:
    groups = robots["groups"]
    return groups.get(agent.lower()) or groups.get("*")


def blocks_root(group: dict | None) -> bool:
    return bool(group) and "/" in group["disallow"] and "/" not in group["allow"]


def robots_findings(url: str, status: int, text: str) -> list[dict]:
    out = []
    if not (200 <= status < 300):
        return [finding("warn", "robots.txt present", url, f"HTTP {status}: every crawler may fetch everything")]
    robots = parse_robots(text)
    g = group_for(robots, GOOGLEBOT)
    if blocks_root(g):
        out.append(finding("fail", "robots allows Googlebot", url, "Disallow: / applies to Googlebot"))
    assets = [d for d in (g or {}).get("disallow", []) if ASSET_PATH.search(d)]
    if assets:
        out.append(finding("warn", "robots does not block CSS/JS", url, "Googlebot may not fetch " + ", ".join(assets[:5])))
    blocked = [a for a in AI_CRAWLERS if blocks_root(robots["groups"].get(a.lower()))]
    out.append(finding("info", "AI crawlers", url, ("blocked: " + ", ".join(blocked)) if blocked else "none blocked"))
    return out


# ---------------------------------------------------------------- sitemaps

def _strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(text: str) -> tuple[str, list[tuple[str, str | None]]]:
    """("index" | "urlset", [(loc, lastmod)]). ValueError when it is not sitemap XML."""
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise ValueError(f"not XML ({exc})") from None
    kind = _strip(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"root element is <{kind}>, not <urlset> or <sitemapindex>")
    entries = []
    for child in root:
        loc = next((c.text.strip() for c in child if _strip(c.tag) == "loc" and c.text), None)
        lastmod = next((c.text.strip() for c in child if _strip(c.tag) == "lastmod" and c.text), None)
        if loc:
            entries.append((loc, lastmod))
    return ("index" if kind == "sitemapindex" else "urlset"), entries


def _lastmod_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None


def collect_sitemap(get, candidates: list[str]) -> tuple[dict[str, str | None], list[str], list[str]]:
    """Fetch the candidate sitemaps (and an index's children, capped): (loc -> lastmod, fetched URLs, problems)."""
    urls: dict[str, str | None] = {}
    fetched: list[str] = []
    problems: list[str] = []
    queue = list(candidates)
    while queue and len(fetched) < SITEMAP_CHILD_CAP:
        sm = queue.pop(0)
        if sm in fetched:
            continue
        fetched.append(sm)
        try:
            final, status, _, body = get(sm)
        except Exception as exc:  # noqa: BLE001 - reported, the audit goes on
            problems.append(f"{sm}: {type(exc).__name__}: {exc}")
            continue
        if not (200 <= status < 300):
            problems.append(f"{sm}: HTTP {status}")
            continue
        try:
            kind, entries = parse_sitemap(body)
        except ValueError as exc:
            problems.append(f"{sm}: {exc}")
            continue
        if kind == "index":
            queue += [loc for loc, _ in entries]
        else:
            urls.update(dict(entries))
    return urls, fetched, problems


def sitemap_findings(get, site: str, robots_sitemaps: list[str], newest_post: str | None, today: dt.date) -> list[dict]:
    candidates = robots_sitemaps or [urllib.parse.urljoin(site + "/", p.lstrip("/")) for p in SITEMAP_DEFAULTS]
    urls, fetched, problems = collect_sitemap(get, candidates)
    label = ", ".join(fetched) or site
    if not urls:
        return [finding("fail", "sitemap present", label, "no sitemap URLs found; " + ("; ".join(problems) or "empty"))]
    out = [finding("info", "sitemap", label, f"{len(urls)} URL(s) in {len(fetched)} file(s)"
                                             + (f"; skipped: {'; '.join(problems)}" if problems else ""))]
    if newest_post:
        if any(same_url(u, newest_post) for u in urls):
            out.append(finding("info", "sitemap lists the newest post", newest_post, "listed"))
        else:
            out.append(finding("fail", "sitemap lists the newest post", newest_post, "not in the sitemap"))
    dates = [d for d in (_lastmod_date(v) for v in urls.values()) if d]
    if not dates:
        out.append(finding("warn", "sitemap lastmod", label, "no lastmod dates"))
    elif today - max(dates) > LASTMOD_WITHIN:
        out.append(finding("warn", "sitemap lastmod", label, f"newest lastmod {max(dates)} is more than "
                                                             f"{LASTMOD_WITHIN.days} days old"))
    return out


# ---------------------------------------------------------------- redirects

def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _redirect_target(fetch, url: str) -> tuple[int, str | None] | None:
    try:
        status, headers, _ = fetch(url)
    except Exception:  # noqa: BLE001 - an unreachable variant is a warning below
        return None
    location = next((v for k, v in (headers or {}).items() if k.lower() == "location"), None)
    return status, (urllib.parse.urljoin(url, location) if location else None)


def redirect_findings(fetch, site: str) -> list[dict]:
    host = _host(site)
    out = []
    http_url = f"http://{host}/"
    res = _redirect_target(fetch, http_url)
    if res is None:
        out.append(finding("warn", "http redirects to https", http_url, "could not fetch"))
    else:
        status, target = res
        if 300 <= status < 400 and target and target.startswith("https://"):
            out.append(finding("info", "http redirects to https", http_url, f"{status} -> {target}"))
        else:
            out.append(finding("warn", "http redirects to https", http_url, f"HTTP {status}, no https redirect"))
    other = host[4:] if host.startswith("www.") else "www." + host
    other_url = f"https://{other}/"
    res = _redirect_target(fetch, other_url)
    if res is None:
        out.append(finding("warn", "www consistency", other_url, f"could not fetch; {host} is configured"))
    else:
        status, target = res
        if 300 <= status < 400 and target and _host(target) == host:
            out.append(finding("info", "www consistency", other_url, f"{status} -> {target}"))
        else:
            out.append(finding("warn", "www consistency", other_url,
                               f"HTTP {status}: does not redirect to the configured host {host}"))
    return out


# ---------------------------------------------------------------- Core Web Vitals (PageSpeed Insights)

def psi_url(page: str, key: str) -> str:
    return PSI_ENDPOINT + "?" + urllib.parse.urlencode({"url": page, "key": key, "strategy": "mobile", "category": "performance"})


def cwv_from_psi(data: dict) -> dict[str, tuple[float | None, str]]:
    """{metric: (value, "field"|"lab"|"none")}; LCP and INP in ms, CLS as a score."""
    field = ((data.get("loadingExperience") or {}).get("metrics") or {})
    lab = ((data.get("lighthouseResult") or {}).get("audits") or {})
    out = {}
    for name, (field_key, lab_key, _, _) in CWV.items():
        if isinstance(field.get(field_key), dict) and field[field_key].get("percentile") is not None:
            value = float(field[field_key]["percentile"])
            out[name] = ((value / 100 if name == "CLS" else value), "field")
        elif isinstance(lab.get(lab_key), dict) and lab[lab_key].get("numericValue") is not None:
            out[name] = (float(lab[lab_key]["numericValue"]), "lab")
        else:
            out[name] = (None, "none")
    return out


def _fmt(name: str, value: float) -> str:
    return f"{value / 1000:.2f}s" if name == "LCP" else f"{value:.0f}ms" if name == "INP" else f"{value:.3f}"


def cwv_findings(fetch, pages: list[str], key: str | None) -> list[dict]:
    if not key:
        return [finding("info", "Core Web Vitals", pages[0] if pages else "-", "unchecked (no PSI key)")]
    out = []
    for page in pages:
        try:
            status, _, body = fetch(psi_url(page, key))
            data = json.loads(body) if 200 <= status < 300 else None
        except Exception as exc:  # noqa: BLE001 - PSI is optional
            out.append(finding("info", "Core Web Vitals", page, f"PageSpeed Insights failed ({type(exc).__name__})"))
            continue
        if not isinstance(data, dict):
            out.append(finding("info", "Core Web Vitals", page, f"PageSpeed Insights answered HTTP {status}"))
            continue
        for name, (value, source) in cwv_from_psi(data).items():
            limit = CWV[name][2]
            if value is None:
                out.append(finding("info", f"CWV {name}", page, "unchecked (not in field or lab data)"))
            elif value > limit:
                out.append(finding("warn", f"CWV {name}", page, f"{_fmt(name, value)} over {_fmt(name, limit)} ({source})"))
            else:
                out.append(finding("info", f"CWV {name}", page, f"{_fmt(name, value)} within {_fmt(name, limit)} ({source})"))
    return out
