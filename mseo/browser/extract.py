"""Named page extractors. Each reads the text an agent saved from one page and returns what Python needs, or a
reason it could not: an extractor never guesses. The agent's own words are never trusted for an account."""

from __future__ import annotations

import re

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Snapshots are the Chrome tool's get_page_text output: "Title: ...", "URL: ...", "Source element: ...", "---", text.
URL_LINE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
ACCOUNT_URL = re.compile(r"^https://myaccount\.google\.com(/|$)")
SIGN_IN_URL = re.compile(r"^https://accounts\.google\.com/")


def signed_in_email(text: str) -> tuple[str | None, str | None]:
    """(email, None) from the Google Account page, or (None, reason): signed_out, ambiguous, layout_changed."""
    url = URL_LINE.search(text)
    url = url.group(1) if url else ""
    if SIGN_IN_URL.match(url):
        return None, "signed_out"
    if not ACCOUNT_URL.match(url):
        return None, "layout_changed"
    body = text.split("\n---\n", 1)[-1]
    found = sorted({m.lower() for m in EMAIL.findall(body)})
    if len(found) == 1:
        return found[0], None
    return None, "ambiguous" if found else "layout_changed"


def page_header(text: str) -> str:
    """The snapshot's Title and URL lines (URL without its query), for an error message; never the page body."""
    head = text.split("\n---\n", 1)[0] if "\n---\n" in text else "\n".join(text.splitlines()[:3])
    title = re.search(r"^Title: (.{0,80})", head, re.MULTILINE)
    url = re.search(r"^URL: ([^?#\s]{0,200})", head, re.MULTILINE)
    parts = [f"Title: {title.group(1).strip()}" if title else "", f"URL: {url.group(1)}" if url else ""]
    return " | ".join(p for p in parts if p) or "no Title/URL lines"


# ---------------------------------------------------------------- account listers (identity check)
# Search Console and GA4 lists are read from the accessibility tree of the open picker (read_page); the Ads account
# chooser from its page text. Each returns (ids, None) or (None, reason).

OPTION = re.compile(r'^\s*option "([^"]+)"', re.MULTILINE)
QUOTED_DIGITS = re.compile(r'^\s*generic "(\d{6,})"', re.MULTILINE)
CUSTOMER_ID = re.compile(r"^\s*(\d{3}-\d{3}-\d{4})\s*$", re.MULTILINE)


def _url(text: str) -> str:
    m = URL_LINE.search(text)
    return m.group(1) if m else ""


def sc_properties(text: str) -> tuple[list[str] | None, str | None]:
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    if "listbox" not in text or "Add property" not in text:
        return None, "layout_changed"
    return [p for p in OPTION.findall(text) if p != "Add property"], None


def ga4_ids(text: str) -> tuple[dict | None, str | None]:
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    acc_at, prop_at = text.find('listbox "Analytics Accounts"'), text.find('listbox "Properties & Apps"')
    if acc_at < 0 or prop_at < 0 or prop_at < acc_at:
        return None, "layout_changed"
    return {"accounts": QUOTED_DIGITS.findall(text[acc_at:prop_at]),
            "properties": QUOTED_DIGITS.findall(text[prop_at:])}, None


def ads_customer_ids(text: str) -> tuple[list[str] | None, str | None]:
    url = _url(text)
    if SIGN_IN_URL.match(url):
        return None, "signed_out"
    if not url.startswith("https://ads.google.com/") or "Select a Google Ads account" not in text:
        return None, "layout_changed"
    return CUSTOMER_ID.findall(text.split("Select a Google Ads account", 1)[1]), None


# ---------------------------------------------------------------- the account a report page shows (pull snapshots)

COMBOBOX = re.compile(r'^\s*combobox "([^"]+)"', re.MULTILINE)
OPTION_BLOCK = re.compile(r"^\s*option \[[^\]]*\]\n((?:\s{2,}.*\n?)*)", re.MULTILINE)


def _root(text: str) -> str:
    """The read element's own line: the first line that is not the `URL:` line. read_page puts the element it was
    given first, so a name found deeper (a search box, a nested child) is not the selector (Strix vuln-0001)."""
    return next((line for line in text.splitlines() if line.strip() and not line.startswith("URL:")), "")


def sc_selected_property(text: str) -> tuple[str | None, str | None]:
    """The property in Search Console's property selector (read_page of the combobox)."""
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    m = COMBOBOX.match(_root(text))
    return (m.group(1), None) if m else (None, "layout_changed")


def ga4_selected_property(text: str) -> tuple[str | None, str | None]:
    """The checked row's property number in GA4's open property picker (read_page of `Properties & Apps`)."""
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    at = text.find('listbox "Properties & Apps"')
    if at < 0 or not _root(text).lstrip().startswith('listbox "Properties & Apps"'):
        return None, "layout_changed"
    checked = [b for b in OPTION_BLOCK.findall(text[at:]) if 'img "check"' in b]
    if len(checked) != 1:
        return None, "layout_changed"
    ids = QUOTED_DIGITS.findall(checked[0])
    return (ids[0], None) if len(ids) == 1 else (None, "layout_changed")


ADS_HEADER = re.compile(r'^\s*generic "(\d{3}-\d{3}-\d{4})\b', re.MULTILINE)


def ads_header_customer_id(text: str) -> tuple[str | None, str | None]:
    """The customer ID in the Ads account header (read_page of the header text, e.g. `generic "<customer ID> <name>"`)."""
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    m = ADS_HEADER.match(_root(text))
    return (m.group(1), None) if m else (None, "layout_changed")


# ---------------------------------------------------------------- Business Profile locations (UNVERIFIED page shape)
# Built on synthetic fixtures until an identity has Business Profile access. Each location is a `row`
# block of read_page output whose link points at business.google.com/.../l/<location id>.

BP_ROW = re.compile(r"^(\s*)row\b.*$", re.MULTILINE)
BP_LINK = re.compile(r'link "([^"]*)" href="https://business\.google\.com/[^"]*?/l/(\d{10,})')
BP_GENERIC = re.compile(r'^\s*generic "([^"]+)"', re.MULTILINE)
BP_STATES = ("Verified", "Unverified", "Pending verification", "Needs reverification", "Suspended", "Disabled")


def bp_locations(text: str) -> tuple[list[dict] | None, str | None]:
    """[{id, name, address, verification}] from the locations table, or (None, reason)."""
    if SIGN_IN_URL.match(_url(text)):
        return None, "signed_out"
    starts = [m.start() for m in BP_ROW.finditer(text)]
    rows = []
    for i, at in enumerate(starts):
        block = text[at:starts[i + 1] if i + 1 < len(starts) else len(text)]
        link = BP_LINK.search(block)
        if not link:
            continue
        texts = BP_GENERIC.findall(block)
        state = next((t for t in texts if t in BP_STATES), None)
        address = next((t for t in texts if t != state and "," in t), None)
        rows.append({"id": link.group(2), "name": link.group(1), "address": address, "verification": state})
    return (rows, None) if rows else (None, "layout_changed")


def bp_location_ids(text: str) -> tuple[list[str] | None, str | None]:
    rows, reason = bp_locations(text)
    return ([r["id"] for r in rows], None) if rows else (None, reason)


# ------------------------------------------------------------------ page totals (the shadow baseline)

_QUOTED = re.compile(r'"([^"]*)"')
_NUMBER = re.compile(r"^\$?-?[\d,]+(\.\d+)?%?$")
SC_CARDS = {"Total clicks": "clicks", "Total impressions": "impressions", "Average CTR": "ctr",
            "Average position": "position"}
GA4_COLUMNS = {"Sessions": "sessions", "Engaged sessions": "engagedSessions", "Key events": "keyEvents"}
ADS_COLUMNS = {"Conversions": "conversions", "Clicks": "clicks", "Impr.": "impressions", "Cost": "cost",
               "Phone calls": "calls"}


def _page_number(text: str) -> float | int | None:
    t = text.strip()
    if not _NUMBER.match(t):
        return None
    pct = t.endswith("%")
    v = float(t.strip("$%").replace(",", ""))
    if pct:
        return round(v / 100, 6)
    return int(v) if v.is_integer() and "." not in t else v


def sc_page_totals(text: str) -> dict:
    """The four summary cards: each label's first number before the next label (a tooltip repeats the label)."""
    out, label = {}, None
    for line in text.splitlines():
        m = _QUOTED.search(line)
        if not m:
            continue
        value = m.group(1)
        if value in SC_CARDS:
            label = SC_CARDS[value]
            continue
        if label and label not in out:
            num = _page_number(value)
            if num is not None:
                out[label] = num
            label = None if num is not None or "generic" in line else label
    return out


def _rows(text: str) -> list[list[list[str]]]:
    """read_page rows -> cells -> the quoted texts inside each cell (a cell is a line indented by one space)."""
    rows: list[list[list[str]]] = []
    for line in text.splitlines():
        if line.startswith("row "):
            rows.append([])
        elif rows and re.match(r"^ (columnheader|gridcell)\b", line):
            rows[-1].append([m for m in _QUOTED.findall(line.split("[ref")[0])])
        elif rows and rows[-1] and line.startswith("  "):
            rows[-1][-1].extend(_QUOTED.findall(line.split("[ref")[0]))
    return [r for r in rows if r]


def _table_totals(text: str, columns: dict) -> dict:
    rows = _rows(text)
    total = next((r for r in rows if any("Total" in t for c in r[:3] for t in c)), None)
    header = next((r for r in rows if r is not total), None)
    if not total or not header:
        return {}
    names = [c[0] if c else "" for c in header]
    out = {}
    for name, cell in zip(reversed(names), reversed(total), strict=False):  # the label cell may span columns: align from the right
        if name in columns:
            nums = [n for n in (_page_number(t) for t in cell) if n is not None]
            if nums:
                out[columns[name]] = nums[0]
    return out


def _ga4_text_totals(text: str, names: list[str]) -> dict:
    """GA4's read_page leaves out some totals cells; its page text has them in column order after the row label."""
    lines = [ln.strip() for ln in text.partition("--- page text ---")[2].splitlines()]
    try:
        start = lines.index("Empty Summary Totals cell for Index column")
    except ValueError:
        return {}
    values, stop_col = [], None
    for ln in lines[start + 2:]:  # skip the "Total" label
        m = re.match(r"^Empty Summary Totals cell for (.+) column$", ln)
        if m:
            stop_col = m.group(1)
            break
        if ln and ln != "100% of total" and not ln.startswith("Avg "):
            values.append(ln)
    if stop_col not in names:
        return {}
    cols = names[:names.index(stop_col)]
    out = {}
    for name, value in zip(reversed(cols), reversed(values), strict=False):
        num = _page_number(value)
        if name in GA4_COLUMNS and num is not None:
            out[GA4_COLUMNS[name]] = num
    return out


def ga4_page_totals(text: str) -> dict:
    table = text.partition("--- page text ---")[0]
    out = _table_totals(table, GA4_COLUMNS)
    rows = _rows(table)
    if rows and len(out) < len(GA4_COLUMNS):
        names = [c[0] if c else "" for c in rows[0]]
        out = {**_ga4_text_totals(text, names), **out}
    return out


def ads_page_totals(text: str) -> dict:
    return _table_totals(text, ADS_COLUMNS)


PAGE_TOTALS = {"search_console": sc_page_totals, "ga4": ga4_page_totals, "google_ads": ads_page_totals}
