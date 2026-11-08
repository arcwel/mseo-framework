"""IndexNow: tell Bing and its partners about a URL right after it is published. Google ignores IndexNow;
Bing feeds ChatGPT's browsing, so the ping matters for AI answers. The key is `site_audit.indexnow_key` and must be
hosted at https://<host>/<key>.txt. A failed ping is recorded and logged, never a publish failure."""

from __future__ import annotations

import datetime as dt
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.indexnow.org/indexnow"
TIMEOUT_S = 20
USER_AGENT = "mseo indexnow"


def key_for(ws) -> str | None:
    return (ws.config.get("site_audit") or {}).get("indexnow_key") or None


def payload(site: str, key: str, urls: list[str]) -> dict:
    host = urllib.parse.urlparse(site).hostname or site
    return {"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": list(urls)}


def urllib_sender(url: str, body: dict, timeout: int = TIMEOUT_S) -> int:
    """POST the JSON body; the HTTP status (IndexNow answers 200 or 202 on success, 4xx on a bad key or URL)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def ping(site: str, key: str, urls: list[str], *, sender=None, now: dt.datetime | None = None) -> dict:
    """{status, at} plus `error` when the request itself failed (status None). Never raises. `sender` is the
    injectable `send(url, body) -> status`; None means the real HTTPS POST (resolved at call time, so tests can
    replace urllib_sender)."""
    sender = sender or urllib_sender
    at = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    own = [u for u in urls if (urllib.parse.urlparse(u).hostname or "").lower().removeprefix("www.")
           == (urllib.parse.urlparse(site).hostname or "").lower().removeprefix("www.")]
    if not own:
        return {"status": None, "at": at, "error": "no URL on the site to submit"}
    try:
        status = sender(ENDPOINT, payload(site, key, own))
    except Exception as exc:  # noqa: BLE001 - the ping is best effort
        return {"status": None, "at": at, "error": f"{type(exc).__name__}: {exc}"[:200]}
    out = {"status": status, "at": at}
    if not (isinstance(status, int) and 200 <= status < 300):
        out["error"] = f"IndexNow answered {status}"
    return out


def ok(result: dict) -> bool:
    return isinstance(result.get("status"), int) and 200 <= result["status"] < 300
