"""The account check reads the Chrome tool's own output, not only the agent's file.

The agent saves the page text it read to data/inbox/, and an agent can write any text there. A value Python takes from
that file counts only when the harness's own record of the run (the Chrome tool calls and results in the `claude -p`
stream, which the agent cannot write) backs it:

* The last Chrome page read (read_page or get_page_text) that the extractor can parse yields the same value.
* That page's URL comes from the harness: the read's tab in the JSON of the next tabs_context_mcp result, with no page
  action between them. The agent's file and the page text are never the source of the URL, and the harness URL is put
  first, so a `URL:` line inside the page text is not the one an extractor sees.
* No page action (a click, typing, navigation) follows that read, so the account cannot be switched after it.
* Every URL the run reached is on HOSTS: tool inputs, page headers, navigate and tab results, typed text that looks
  like an address. A page on any other host, or a Google host that serves user content, could show any account.

The reasons are in REASONS; callers record them as `unverified_account`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable

from mseo.browser.run import CHROME

HOSTS = {"accounts.google.com", "myaccount.google.com", "search.google.com", "analytics.google.com",
         "ads.google.com", "business.google.com"}
REASONS = {"not_in_tool_output": "no page the Chrome tool returned shows it",
           "differs_from_tool_output": "it differs from the last page the Chrome tool returned",
           "no_page_url": "the Chrome tool gave no URL for that page (tabs_context_mcp right after the read)",
           "acted_after_read": "the agent acted on the page after the read that shows it",
           "off_site": "the run reached a page outside the Google hosts it needs",
           "two_browsers": "the agent selected a browser again after it started work",
           "two_tabs": "the run used more than one tab, so the read may not be the export's tab"}
READS = {CHROME + "read_page", CHROME + "get_page_text"}
INERT = READS | {CHROME + n for n in ("find", "tabs_context_mcp", "list_connected_browsers",
                                      "read_console_messages", "read_network_requests",
                                      "tabs_close_mcp")}  # closing the tab cannot switch the account
PAGE_CALLS = READS | {CHROME + n for n in ("computer", "navigate", "find", "form_input")}
SELECTS = {CHROME + "select_browser", CHROME + "switch_browser"}  # either one picks the browser (Sonnet low 4)
SETUP = {CHROME + "list_connected_browsers"} | SELECTS
INERT_COMPUTER = {"screenshot", "wait", "zoom", "scroll", "scroll_to", "hover"}
URLISH = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://\S+|(?:data|javascript|file|about|blob|view-source):\S+"
                    r"|(?:[a-z0-9-]+\.)+[a-z]{2,}\b(?:[/:]\S*)?)")
URL_IN_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>)\]]+")
HEADER_URL = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
SAFE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]!$&'()*+,;=%]+$")


NEUTRAL = {"chrome://newtab/", "about:blank"}  # a blank tab shows no account


def allowed_url(url: str) -> bool:
    if url in NEUTRAL:
        return True
    if not isinstance(url, str) or not SAFE.match(url) or "\\" in url or "@" in url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return (parts.scheme == "https" and port in (None, 443) and parts.username is None and parts.password is None
            and host.isascii() and host in HOSTS and parts.netloc.lower().split(":")[0] == host)


def _is_action(call: dict) -> bool:
    if call["name"] in INERT:
        return False
    if call["name"] == CHROME + "computer":
        inp = call["input"]
        action = inp.get("action")
        return not (action in INERT_COMPUTER or (action == "key" and str(inp.get("text")) == "Escape"))
    return True


def tab_list(text: str | None) -> dict[int, str] | None:
    """{tabId: url} from a tabs_context_mcp result. The tool puts its JSON first, and JSON escapes every character a
    page title could use to fake another entry, so this is the one URL source a page cannot write (an in-band
    `URL:` header can be faked with a title that holds `---`)."""
    if not text:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return None
    tabs = obj.get("availableTabs") if isinstance(obj, dict) else None
    if not isinstance(tabs, list):
        return None
    out = {t["tabId"]: t["url"] for t in tabs
           if isinstance(t, dict) and type(t.get("tabId")) is int and isinstance(t.get("url"), str)}
    return out if len(out) == len(tabs) else None


def reached_urls(calls: list[dict]) -> list[str]:
    out = []
    for c in calls:
        inp, short = c["input"], c["name"].removeprefix(CHROME)
        if isinstance(inp.get("url"), str):
            out.append(inp["url"])
        if c["name"] == CHROME + "get_page_text" and c["text"]:
            # every `URL:` line that names an address, header or not: a page cannot hide the real one; a table row
            # such as `URL: best repair near me` names none
            out += [u for u in HEADER_URL.findall(c["text"]) if URLISH.match(u)]
        elif short.startswith(("navigate", "tabs_")) and c["text"]:
            out += list((tab_list(c["text"]) or {}).values()) + URL_IN_TEXT.findall(c["text"])
        if short == "computer" and inp.get("action") == "type":
            out += URLISH.findall(str(inp.get("text") or ""))
    return out


def _second_browser(calls: list[dict]) -> bool:
    """A browser may be selected once (select_browser or switch_browser), before any other work: after it, a read could come from another browser."""
    selects = [i for i, c in enumerate(calls) if c["name"] in SELECTS]
    first_work = next((i for i, c in enumerate(calls) if c["name"] not in SETUP), len(calls))
    return len(selects) > 1 or any(i > first_work for i in selects)


def _second_tab(calls: list[dict]) -> bool:
    """One tab per run: at most one tabs_create_mcp, and every call that names a tab names the same one."""
    creates = sum(c["name"] == CHROME + "tabs_create_mcp" for c in calls)
    tabs = {c["input"].get("tabId") for c in calls if c["input"].get("tabId") is not None}
    unnamed = any(c["name"] in PAGE_CALLS and c["input"].get("tabId") is None for c in calls)
    return creates > 1 or len(tabs) > 1 or (bool(tabs) and unnamed)  # an unnamed call uses the default tab


def page_url(calls: list[dict], i: int) -> str | None:
    """The read's URL: its tab in the next tabs_context_mcp list, with no page action between the read and it."""
    tab = calls[i]["input"].get("tabId")
    for c in calls[i + 1:]:
        tabs = tab_list(c["text"]) if c["name"] == CHROME + "tabs_context_mcp" else None
        if tabs is not None:
            if tab is not None:
                return tabs.get(tab)
            return next(iter(tabs.values())) if len(tabs) == 1 else None
        if _is_action(c):
            return None
    return None


def _last_read(extractor: Callable[[str], tuple], calls: list[dict]) -> tuple | None:
    """(index, value, url) of the last Chrome read the extractor can parse, or None."""
    last, run = None, []  # run: read_page texts since the last page action (one page, read in parts)
    for i, c in enumerate(calls):
        if _is_action(c):
            run = []
        if c["name"] not in READS or c["text"] is None:
            continue
        if c["name"] == CHROME + "read_page":
            run.append(c["text"])
        url = page_url(calls, i)
        joined = ["\n".join(run)] if c["name"] == CHROME + "read_page" else []
        for text in dict.fromkeys([c["text"], *joined]):
            v, r = extractor(f"URL: {url}\n{text}" if url else text)
            if r is None:
                last = (i, v, url)
                break
    return last


def attested_url(extractor: Callable[[str], tuple], res) -> str | None:
    """The page URL of the read `attested` checked: callers tie other reads of the run to its account (`tie`)."""
    last = _last_read(extractor, res.calls)
    return last[2] if last else None


def attested(extractor: Callable[[str], tuple], file_text: str, res) -> tuple:
    """(value, None) when the file and the harness record agree; else (None, reason). A reason the extractor finds
    in the file itself (signed_out, layout_changed) is returned unchanged."""
    value, reason = extractor(file_text)
    if reason:
        return value, reason
    calls = res.calls
    if any(not allowed_url(u) for u in reached_urls(calls)):
        return None, "off_site"
    if _second_browser(calls):
        return None, "two_browsers"
    if _second_tab(calls):
        return None, "two_tabs"
    last = _last_read(extractor, calls)
    if last is None:
        return None, "not_in_tool_output"
    i, seen, url = last
    if url is None:
        return None, "no_page_url"
    if seen != value:
        return None, "differs_from_tool_output"
    if any(_is_action(c) for c in calls[i + 1:]):
        return None, "acted_after_read"
    return value, None


def why(reason: str, what: str) -> str:
    return f"the {what} in the agent's file was not taken: {REASONS[reason]} (Chrome tool output is the evidence)"
