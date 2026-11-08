"""A stand-in for `claude -p` in browser runs. It never starts an agent or a browser: it writes the snapshot and export
files a real run would leave in <ws>/data/inbox/ and returns a stream-json transcript with an init and a result line.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


CHROME = "mcp__claude-in-chrome__"


def page_text(url: str, body: str, title: str = "Page") -> str:
    """The shape of a get_page_text result: header lines (the harness's own URL), then the page text."""
    return f"Title: {title}\nURL: {url}\nSource element: <body>\n---\n{body}"


def tab_list(url: str, tab: int = 7) -> dict:
    """A tabs_context_mcp call with the tool's own result shape: JSON first (the URL Python trusts), then plain text."""
    data = {"availableTabs": [{"tabId": tab, "title": "Page", "url": url}], "tabGroupId": 1}
    return {"name": CHROME + "tabs_context_mcp", "input": {},
            "text": json.dumps(data) + f'\n\nTab Context:\n- Available tabs:\n  - tabId {tab}: "Page" ({url})'}


EXPORT_SOURCES = ("search_console", "ga4", "google_ads")


def keyed(source: str, url: str) -> str:
    """The page URL with the account key a live report URL carries (resource_id, #/a<n>p<n>/, ocid)."""
    from urllib.parse import quote

    from tests.conftest import FX
    if source == "ga4":
        return url.split("#")[0] + f"#/a1p{FX['accounts']['ga4']['id']}/reports/explorer"
    name, value = (("resource_id", quote(FX["accounts"]["search_console"]["id"], safe=""))
                   if source == "search_console" else ("ocid", "1"))
    if f"{name}=" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"{name}={value}"


def totals_text(source: str, m: dict) -> str:
    """The report page's totals in the read_page shapes extract.PAGE_TOTALS reads (live layouts, 2026-11-07)."""
    def num(v):
        return f"{v:,}" if isinstance(v, int) else f"{v:.2f}"
    if source == "search_console":
        lines = ["main [ref_1]"]
        cards = [("Total clicks", num(m["clicks"])), ("Total impressions", num(m["impressions"])),
                 ("Average CTR", f"{m['ctr'] * 100:.1f}%"), ("Average position", f"{m['position']:.1f}")]
        for label, value in cards:
            lines += [" button [ref_a]", f'  generic "{label}" [ref_b]', f'  generic "{value}" [ref_d]']
        return "\n".join(lines)
    cols = ({"Sessions": "sessions", "Engaged sessions": "engagedSessions", "Key events": "keyEvents"}
            if source == "ga4" else {"Conversions": "conversions", "Impr.": "impressions", "Cost": "cost"})
    head = ["row [ref_1]"] + [f' columnheader "{n}" [ref_h]' for n in cols]
    total = ["row [ref_9]", " gridcell [ref_a]", '  generic "Total" [ref_b]']
    for metric in cols.values():
        v = m[metric]
        total += [" gridcell [ref_c]", f'  generic "{"$" if metric == "cost" else ""}{num(v)}" [ref_d]']
    return "\n".join([*head, "", *total])


def export_metrics(source: str, name: str, content) -> dict:
    """The export's week totals, the way Python sums them after the pull (the honest page shows the same)."""
    import tempfile

    from mseo import week
    from mseo.adapters import browser_pull

    adapter = browser_pull.SOURCES[source]["adapter"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        adapter.write(adapter.parse(path), Path(tmp))
        return week.METRICS[source](week._read(Path(tmp) / week.FILES[source]))["metrics"]


def honest_totals(files: dict) -> list[dict]:
    """A totals read on the report page, matching the export, unless the test gave its own totals file."""
    for source in EXPORT_SOURCES:
        snap = files.get(f"snapshot-{source}.txt")
        if not isinstance(snap, str) or f"totals-{source}.txt" in files:
            continue
        url = snap.partition("\n")[0].removeprefix("URL:").strip() if snap.startswith("URL:") else ""
        export = next(((n, c) for n, c in files.items() if not n.startswith(("snapshot-", "totals-"))), None)
        if not url or export is None:
            continue
        try:
            m = export_metrics(source, *export)
        except Exception:  # an export the adapter refuses: the pull fails on it before the tie
            continue
        return [{"name": CHROME + "read_page", "input": {}, "text": totals_text(source, m)},
                tab_list(keyed(source, url))]
    return []


def honest_reads(files: dict) -> list[dict]:
    out = honest_totals(files)
    for name, content in files.items():
        if isinstance(content, str) and name.startswith("totals-") and content.startswith("URL: https://"):
            first, _, rest = content.partition("\n")
            source = name.removeprefix("totals-").removesuffix(".txt")
            out += [{"name": CHROME + "read_page", "input": {}, "text": rest},
                    tab_list(keyed(source, first.removeprefix("URL:").strip()))]
            continue
        if not (isinstance(content, str) and name.startswith("snapshot-")):
            continue
        if content.startswith("Title:"):
            url = content.split("\n", 2)[1].removeprefix("URL:").strip()
            out += [{"name": CHROME + "get_page_text", "input": {}, "text": content}, tab_list(url)]
            continue
        first, _, rest = content.partition("\n")
        url = first.removeprefix("URL:").strip() if first.startswith("URL:") else "https://search.google.com/"
        source = name.removeprefix("snapshot-").removesuffix(".txt")
        url = keyed(source, url) if source in EXPORT_SOURCES else url
        out += [{"name": CHROME + "read_page", "input": {}, "text": rest if first.startswith("URL:") else content},
                tab_list(url)]
    return out


class FakeRunner:
    def __init__(self, files: dict[str, str | bytes] | None = None, *, exit_code: int = 0,
                 api_key_source: str = "none", usage: dict | None = None, turns: int = 4,
                 tools: list[str] | None = None, model: str = "claude-haiku-5-5", hang: bool = False,
                 reply: str = "done", calls: list[dict] | None = None, urls: list[str] | None = None):
        self.files = files or {}
        # The Chrome tool calls in the stream, as {name, input, text}. By default each snapshot the fake agent saves is
        # also what it read, as in an honest run (read_page, then get_page_text for the page's URL); a test passes
        # calls to make the file and the tool output differ.
        self.chrome = calls
        self.urls = urls or []
        self.reply = reply
        self.exit_code = exit_code
        self.api_key_source = api_key_source
        self.usage = usage or {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 40000,
                               "cache_creation_input_tokens": 5000}
        self.turns = turns
        self.tools = tools or ["Read", "Write", "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__get_page_text"]
        self.model = model
        self.hang = hang
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], *, cwd: Path, env: dict, timeout: float) -> subprocess.CompletedProcess:
        self.calls.append({"argv": argv, "cwd": Path(cwd), "env": env, "timeout": timeout})
        if self.hang:
            raise subprocess.TimeoutExpired(argv, timeout)
        inbox = Path(cwd) / "data" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for name, content in self.files.items():
            target = inbox / name
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        chrome = [{"name": CHROME + "navigate", "input": {"url": u}, "text": "Navigated"} for u in self.urls]
        chrome += self.chrome if self.chrome is not None else honest_reads(self.files)
        uses = [{"type": "tool_use", "id": f"c{i}", "name": c["name"], "input": c.get("input") or {}}
                for i, c in enumerate(chrome)]
        results = [{"type": "tool_result", "tool_use_id": f"c{i}", "content": [{"type": "text", "text": c["text"]}]}
                   for i, c in enumerate(chrome) if c.get("text") is not None]
        lines = [
            {"type": "system", "subtype": "init", "apiKeySource": self.api_key_source, "tools": self.tools,
             "model": self.model},
            {"type": "assistant", "message": {"content": uses}},
            {"type": "user", "message": {"content": results}},
            {"type": "result", "subtype": "success" if self.exit_code == 0 else "error", "is_error": self.exit_code != 0,
             "num_turns": self.turns, "duration_ms": 90_000, "result": self.reply, "permission_denials": [],
             "usage": self.usage, "modelUsage": {self.model: {}}},
        ]
        return subprocess.CompletedProcess(argv, self.exit_code, "\n".join(json.dumps(x) for x in lines) + "\n", "")


# The shape the Chrome tool's get_page_text returns (header lines, then the page text), with synthetic values.
ACCOUNT_PAGE = """Title: Google Account
URL: https://myaccount.google.com/
Source element: <div>
---
{name}
{email}
search

Only you can see your settings.
"""

SIGNED_OUT_PAGE = """Title: Sign in - Google Accounts
URL: https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fmyaccount.google.com%2F
Source element: <main>
---
Sign in
Use your Google Account
Email or phone
"""


# read_page output of the open pickers (accessibility tree), synthetic values, real shapes (D1 step 5 notes).
def sc_list(*props: str) -> str:
    rows = "".join(f' presentation [ref_{i}0]\n  option "{p}" [ref_{i}1]\n   image "favicon" [ref_{i}2]\n'
                   f'   generic "{p}" [ref_{i}3]\n' for i, p in enumerate(props, 1))
    return ("URL: https://search.google.com/search-console\nlistbox [ref_40]\n" + rows +
            ' presentation [ref_90]\n  separator [ref_91]\n option [ref_92]\n  generic "Add property" [ref_93]\n'
            '  generic "New options" [ref_94]\n')


def ga4_list(accounts: list[tuple[str, str]], props: list[tuple[str, str]]) -> str:
    acc = "".join(f' option [ref_a{i}]\n  button [ref_b{i}]\n   generic "{n}" [ref_c{i}]\n   generic "{x}" [ref_d{i}]\n'
                  f'   img "chevron_right" [ref_e{i}]\n' for i, (n, x) in enumerate(accounts))
    pro = "".join(f' option [ref_p{i}]\n  link [ref_q{i}] href="#/a1p{x}/reports/intelligenthome"\n'
                  f'   generic "{n}" [ref_r{i}]\n   generic "{x}" [ref_s{i}]\n' for i, (n, x) in enumerate(props))
    return ('URL: https://analytics.google.com/analytics/web/\nlistbox "Analytics Accounts" [ref_725]\n' + acc +
            'listbox "Properties & Apps" [ref_734]\n' + pro)


def ads_list(email: str, *accounts: tuple[str, str]) -> str:
    rows = "".join(f"{n}\n{cid}\n" for n, cid in accounts)
    return (f"Title: Google Ads\nURL: https://ads.google.com/nav/selectaccount\nSource element: <body>\n---\n{email}\n"
            f"Select a Google Ads account\nSearch for account name or CID\n{rows}New Google Ads Account\n"
            "Switch Google account\n")
