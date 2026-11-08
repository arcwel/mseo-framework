"""The export must come from the attested account .

The account check (`attest`) proves which account the last page read showed. It does not prove where the export file
came from: the agent could export from another property and navigate back before that read, or write the file itself
into the inbox. So an export counts only when two things in the harness's own record of the run agree with it:

* a read of the report's totals (read_page or get_page_text) on a page whose URL, from the tab list JSON, carries the
  same account key as the attested read's page (Search Console `resource_id`, the GA4 property in `#/a<n>p<n>/`, the
  Ads `ocid`), so the totals are that account's; and
* the export's own totals, summed from the tables Python wrote, match those page totals within the shadow tolerances.

What is left: an agent that writes a whole made-up export whose totals equal the real page's. That needs a deliberate
forgery of every row, and the result can only hold this account's own totals, not another customer's data.
"""

from __future__ import annotations

import re
import urllib.parse

from mseo.browser import attest, extract
from mseo.browser.run import CHROME

GA4_KEY = re.compile(r"#/(?:a\d+)?p(\d+)(?:[/?]|$)")  # #/a<account>p<property>/ or #/p<property>/
REASONS = {"no_page_totals": "no totals read in the Chrome tool output is from the attested account's page",
           "export_differs": "the export's totals differ from the totals the attested account's page showed",
           "other_account": "the attested page's URL names an account other than the customer's"}
KEYED = {"search_console", "ga4"}  # their URL key is the account ID itself; an Ads ocid is not the customer ID


def _query(url: str, name: str) -> str | None:
    vals = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(name) or []
    return vals[0] if len(set(vals)) == 1 and vals[0] else None


def account_key(source: str, url: str | None) -> str | None:
    """The account a report page's URL names, or None when it names none."""
    if not url:
        return None
    if source == "search_console":
        return _query(url, "resource_id")
    if source == "ga4":
        m = GA4_KEY.search(url)
        return str(int(m.group(1))) if m else None  # p0345 is p345
    if source == "google_ads":
        return _query(url, "ocid")
    return None


def stream_totals(source: str, calls: list[dict], key: str) -> dict | None:
    """The last totals read in the stream on a page of account `key`. A read counts alone, or joined with the
    read_page calls before it since the last page action (one page read in pieces: live agents read the header row
    and the totals row separately), and with the get_page_text right after it, as GA4's reader needs."""
    reader = extract.PAGE_TOTALS.get(source)
    if reader is None or key is None:
        return None
    found, run = None, []  # run: read_page texts since the last page action
    for i, c in enumerate(calls):
        if attest._is_action(c):
            run = []
        if c["name"] not in attest.READS or not c["text"]:
            continue
        texts = [c["text"]]
        if c["name"] == CHROME + "read_page":
            run.append(c["text"])
            texts.append("\n".join(run))
        nxt = calls[i + 1] if i + 1 < len(calls) else None
        if c["name"] == CHROME + "read_page" and nxt and nxt["name"] == CHROME + "get_page_text" and nxt["text"]:
            texts += [t + "\n--- page text ---\n" + nxt["text"] for t in texts]
        best = max((reader(t) for t in texts), key=len)
        if best and len(best) >= len(found or {}) and account_key(source, attest.page_url(calls, i)) == key:
            found = best  # the fullest read wins: a later fragment must not drop required metrics
    return found


def check(source: str, calls: list[dict], attested_url: str | None, export_totals: dict, tolerances: dict,
          within, *, expected: str | None = None) -> tuple[str | None, str | None]:
    """(None, None) when the export is tied to the attested account; else (reason, detail). With `expected`, a
    Search Console or GA4 key must also be the customer's account."""
    from mseo.capture.account import normalize

    key = account_key(source, attested_url)
    if expected is not None and source in KEYED and key is not None and normalize(key) != normalize(expected):
        return "other_account", REASONS["other_account"]
    page = stream_totals(source, calls, key)
    if not page:
        return "no_page_totals", REASONS["no_page_totals"]
    compared, off, missing = 0, [], []
    for metric, ours in export_totals.items():
        tol = tolerances.get(f"{source}.{metric}")
        if tol is None or ours is None:
            continue
        if page.get(metric) is None:
            if tol.get("page") != "optional":  # every metric the page shows must match
                missing.append(metric)
            continue
        compared += 1
        if not within(ours, page[metric], tol):
            off.append(f"{metric} export={ours} page={page[metric]}")
    if missing:
        return "no_page_totals", f"{REASONS['no_page_totals']} (the page totals lack {', '.join(missing)})"
    if not compared:
        return "no_page_totals", REASONS["no_page_totals"]
    if off:
        return "export_differs", f"{REASONS['export_differs']}: {', '.join(off)}"
    return None, None
