"""content/links: find the URLs in content and check that each resolves (ported from the earlier house link checker).

`check_url` is a GET following redirects; a network failure has status None, never ok. Only http:// and https://
are fetched (file:// and ftp:// are refused before any request, audit L2) and at most READ_CAP bytes are read.
"""
from __future__ import annotations

import http.client
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
TRAILING = ".,;:!?*`"
SKIP_HOSTS = {"schema.org", "www.w3.org"}  # identifiers in JSON-LD, not links
USER_AGENT = "Mozilla/5.0 (mseo link check)"
ALLOWED_SCHEMES = ("http", "https")
READ_CAP = 2 * 1024 * 1024  # bytes read from a checked page: enough to settle the status, never the whole site


def extract_urls(text: str, only_domain: str | None = None) -> list[str]:
    """Unique URLs in first-seen order, trailing punctuation trimmed."""
    seen, out = set(), []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(TRAILING)
        host = urllib.parse.urlparse(url).hostname or ""
        if host in SKIP_HOSTS:
            continue
        if only_domain and not (host == only_domain or host.endswith("." + only_domain)):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def check_url(url: str, timeout: int = 20) -> dict:
    """GET (HEAD is often blocked by WordPress hosts), following redirects."""
    if urllib.parse.urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
        return {"url": url, "status": None, "ok": False, "error": "scheme not allowed"}
    try:
        req = urllib.request.Request(
            urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~"),
            headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as resp:
            code, final = resp.status, resp.geturl()
            resp.read(READ_CAP)
    except urllib.error.HTTPError as exc:
        code, final = exc.code, url
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
            ValueError) as exc:
        return {"url": url, "status": None, "ok": False,
                "error": str(getattr(exc, "reason", exc))}
    if not str(final).lower().startswith(("http://", "https://")):  # a redirect off the web is not a live page
        return {"url": url, "status": None, "ok": False, "error": "redirected to a non-web scheme", "final": final}
    return {"url": url, "status": code, "ok": 200 <= code < 300,
            "final": final if final != url else None}
