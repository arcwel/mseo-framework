"""Google Ads through the account's own Scripts Sheet.

The Ads Script in templates/ads/mseo-report-export.gs runs inside the account on a schedule and writes one tab per
report table to the Sheet whose ID the operator recorded in customer.json (`accounts.google_ads.scripts_sheet_id`).
The pull downloads tabs as CSV through `/spreadsheets/d/<id>/export?format=csv&gid=<gid>` (the gviz `tq` endpoint
infers column types and can blank the marker and header lines above a numeric column, so it is not used). It runs in
two phases: first the `_manifest` tab, which the script keeps on the spreadsheet's FIRST sheet (gid 0) and which lists
every table with its gid; then each table by the gid the manifest gave. Both phases land in data/inbox/.

Every tab starts with four header lines the script writes from AdsApp itself, so the account check does not read a
page: `# mseo.ads_sheet/1`, `customer_id,<id>`, `period,<start>,<end>`, `generated,<iso>`, then the GAQL column header
and the rows. A tab whose first line or columns are not these is `layout_changed`; another customer ID is
`wrong_account`; another period is an error. The download is attested by the harness's own record of each run: every
docs.google.com URL the agent navigated to must carry the configured sheet ID (`seen_via: sheet:<id>`); a URL to any
other sheet, or to a host outside Google, makes the run `unverified_account` and nothing is kept.

Beyond the ten report tables (TABLES) the script writes three non-data tabs, optional here: `_manifest` (table, rows,
gid), `_errors` (table, error: one row per GAQL that failed; the run record carries them under `checks` and is then
incomplete) and `applied_log` (applied_at, batch_id, n, type, target, result; what the script did with the pasted
approved_changes rows, read back by `mseo actions status`).

Auction insights is not available to Ads Scripts (no GAQL view, no AdsApp report), so no table carries it.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import time
import urllib.parse
from pathlib import Path

from mseo import identity
from mseo.adapters import export
from mseo.adapters.export import ExportError, LayoutChanged, write_table
from mseo.browser import attest
from mseo.browser import run as browser_run
from mseo.exits import EXIT_FAIL, EXIT_OK
from mseo.run import record as run_record

SOURCE = "google_ads"
PROMPT = "export_google_ads_sheet"
LAG_DAYS = 3  # conversions keep arriving for a few days
MARKER = "# mseo.ads_sheet/1"
SHEET_HOST = "docs.google.com"
EXIT_EXISTS = 2
MICROS = 1_000_000

# kinds: text (kept as written), micros (currency units, 2 decimals), num (number), flag (true/false text)
TABLES: dict[str, list[tuple[str, str, str]]] = {
    "campaigns": [("campaign.name", "campaign", "text"), ("campaign.advertising_channel_type", "campaign_type", "text"),
                  ("campaign.status", "status", "text"), ("campaign.bidding_strategy_type", "bidding_strategy", "text"),
                  ("campaign_budget.amount_micros", "budget", "micros"), ("metrics.cost_micros", "cost", "micros"),
                  ("metrics.clicks", "clicks", "num"), ("metrics.impressions", "impressions", "num"),
                  ("metrics.conversions", "conversions", "num"), ("metrics.conversions_value", "conversions_value", "num"),
                  ("metrics.phone_calls", "calls", "num"), ("metrics.search_impression_share", "impression_share", "num"),
                  ("metrics.search_budget_lost_impression_share", "lost_is_budget", "num"),
                  ("metrics.search_rank_lost_impression_share", "lost_is_rank", "num")],
    "ad_groups": [("campaign.name", "campaign", "text"), ("ad_group.name", "ad_group", "text"),
                  ("ad_group.status", "status", "text"), ("metrics.cost_micros", "cost", "micros"),
                  ("metrics.clicks", "clicks", "num"), ("metrics.impressions", "impressions", "num"),
                  ("metrics.conversions", "conversions", "num")],
    "keywords": [("campaign.name", "campaign", "text"), ("ad_group.name", "ad_group", "text"),
                 ("ad_group_criterion.keyword.text", "keyword", "text"),
                 ("ad_group_criterion.keyword.match_type", "match_type", "text"),
                 ("ad_group_criterion.status", "status", "text"),
                 ("ad_group_criterion.quality_info.quality_score", "quality_score", "num"),
                 ("metrics.cost_micros", "cost", "micros"), ("metrics.clicks", "clicks", "num"),
                 ("metrics.impressions", "impressions", "num"), ("metrics.conversions", "conversions", "num"),
                 ("metrics.ctr", "ctr", "num"), ("metrics.average_cpc", "average_cpc", "micros")],
    "search_terms": [("campaign.name", "campaign", "text"), ("ad_group.name", "ad_group", "text"),
                     ("search_term_view.search_term", "search_term", "text"), ("search_term_view.status", "status", "text"),
                     ("segments.search_term_match_type", "match_type", "text"), ("metrics.cost_micros", "cost", "micros"),
                     ("metrics.clicks", "clicks", "num"), ("metrics.impressions", "impressions", "num"),
                     ("metrics.conversions", "conversions", "num")],
    "assets": [("campaign.name", "campaign", "text"), ("ad_group.name", "ad_group", "text"),
               ("ad_group_ad_asset_view.field_type", "field_type", "text"), ("asset.text_asset.text", "text", "text"),
               ("ad_group_ad_asset_view.performance_label", "performance_label", "text"),
               ("ad_group_ad_asset_view.pinned_field", "pinned_field", "text"), ("metrics.impressions", "impressions", "num")],
    "locations": [("campaign.name", "campaign", "text"), ("geographic_view.country_criterion_id", "country", "text"),
                  ("geographic_view.location_type", "location_type", "text"), ("segments.geo_target_city", "location", "text"),
                  ("segments.geo_target_state", "state_resource", "text"), ("city_name", "city", "text"),
                  ("state_name", "state", "text"),
                  ("metrics.cost_micros", "cost", "micros"), ("metrics.clicks", "clicks", "num"),
                  ("metrics.impressions", "impressions", "num"), ("metrics.conversions", "conversions", "num")],
    "ad_schedule": [("campaign.name", "campaign", "text"), ("segments.day_of_week", "day_of_week", "text"),
                    ("segments.hour", "hour", "num"), ("metrics.cost_micros", "cost", "micros"),
                    ("metrics.clicks", "clicks", "num"), ("metrics.impressions", "impressions", "num"),
                    ("metrics.conversions", "conversions", "num")],
    "change_history": [("change_event.change_date_time", "changed_at", "text"), ("change_event.user_email", "user", "text"),
                       ("change_event.change_resource_type", "resource_type", "text"),
                       ("change_event.resource_change_operation", "operation", "text"),
                       ("change_event.client_type", "client_type", "text"), ("campaign.name", "campaign", "text")],
    "conversion_actions": [("conversion_action.name", "name", "text"), ("conversion_action.type", "type", "text"),
                           ("conversion_action.category", "category", "text"), ("conversion_action.status", "status", "text"),
                           ("conversion_action.primary_for_goal", "primary", "flag"),
                           ("conversion_action.counting_type", "counting_type", "text"),
                           ("conversion_action.origin", "origin", "text"),
                           ("conversion_action.include_in_conversions_metric", "in_conversions", "flag")],
    "budgets": [("campaign.name", "campaign", "text"), ("campaign_budget.amount_micros", "budget", "micros"),
                ("campaign_budget.delivery_method", "delivery_method", "text"),
                ("campaign_budget.explicitly_shared", "shared", "flag")],
}
# The script's non-data tabs: optional in a pull, never part of the week's metrics.
MANIFEST, ERRORS, APPLIED_LOG = "_manifest", "_errors", "applied_log"
MANIFEST_GID = 0  # the script keeps the manifest on the spreadsheet's first sheet
OPTIONAL_TABLES: dict[str, list[tuple[str, str, str]]] = {
    MANIFEST: [("table", "table", "text"), ("rows", "rows", "num"), ("gid", "gid", "num")],
    ERRORS: [("table", "table", "text"), ("error", "error", "text")],
    APPLIED_LOG: [("applied_at", "applied_at", "text"), ("batch_id", "batch_id", "text"), ("n", "n", "num"),
                  ("type", "type", "text"), ("target", "target", "text"), ("result", "result", "text")],
}
ALL_TABLES = {**TABLES, **OPTIONAL_TABLES}
TABLE_FILES = {name: f"{name}.csv" for name in TABLES}
OPTIONAL_FILES = {name: f"{name}.csv" for name in OPTIONAL_TABLES}
NUMERIC_KINDS = ("micros", "num")
SCRIPT_ERRORS_CHECK = "script_errors"
_SHEET_PATH = re.compile(r"^/spreadsheets/d/([A-Za-z0-9_-]+)(?:/|$)")


def configured(ws) -> str | None:
    """The Sheet ID the account's script writes, or None when the Report Editor path is in use."""
    acct = ws.config["accounts"].get(SOURCE) or {}
    return acct.get("scripts_sheet_id") or None


def fields(name: str) -> list[str]:
    return [out for _, out, _ in ALL_TABLES[name]]


def tab_url(sheet_id: str, gid: int) -> str:
    """The CSV export of one tab by gid: cell text as displayed, no type inference."""
    q = urllib.parse.urlencode({"format": "csv", "gid": int(gid)})
    return f"https://{SHEET_HOST}/spreadsheets/d/{sheet_id}/export?{q}"


def values(ws, period: dict, gids: dict[str, int] | None = None) -> dict:
    """The prompt's values: identity, sheet and the URLs of one phase (no customer text lives in the prompt file).
    Without gids: phase one, the manifest alone (gid 0). With gids (table -> gid): phase two, every table listed."""
    sheet_id = configured(ws)
    if not sheet_id:
        raise ExportError(f"accounts.{SOURCE}.scripts_sheet_id is not set in {ws.slug}")
    names = list(gids) if gids else [MANIFEST]
    urls = "\n".join(f"{i}. {tab_url(sheet_id, gids[n] if gids else MANIFEST_GID)}" for i, n in enumerate(names, 1))
    return {"email": ws.identity_for(SOURCE)["email"], "sheet_id": sheet_id, "tab_urls": urls,
            "count": str(len(names)), "start": period["start"], "end": period["end"]}


def table_gids(manifest_rows: list[dict]) -> dict[str, int]:
    """table -> gid from the parsed manifest: every report table once, gids distinct; the optional tabs when listed."""
    gids: dict[str, int] = {}
    for r in manifest_rows:
        name, gid = (r.get("table") or "").strip(), r.get("gid")
        if name not in ALL_TABLES or name == MANIFEST:
            continue
        if gid is None or not isinstance(gid, (int, float)) or gid != gid or gid in (float("inf"), float("-inf")) \
                or int(gid) != gid or int(gid) < 0:
            raise LayoutChanged(f"the manifest gives table {name} no gid ({gid!r})")
        if name in gids:
            raise LayoutChanged(f"the manifest lists table {name} twice")
        gids[name] = int(gid)
    missing = [n for n in TABLES if n not in gids]
    if missing:
        raise ExportError(f"the manifest lists no gid for: {', '.join(missing)}")
    if len(set(gids.values())) != len(gids):
        raise LayoutChanged("two tables in the manifest share a gid")
    return gids


# ---------------------------------------------------------------- parsing

def _cells(row: list[str]) -> list[str]:
    out = [c.strip() for c in row]
    while out and out[-1] == "":
        out.pop()
    return out


def _value(text: str, kind: str):
    if kind in ("text", "flag"):
        return text
    if text == "":
        return None
    try:
        n = export.number(text)
    except ValueError:
        return None  # "--" and "< 10%" style placeholders carry no number
    return round(n / MICROS, 2) if kind == "micros" else n


def parse_tab(path: Path) -> dict:
    """{table, customer_id, start, end, generated, rows} from one downloaded tab."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"{Path(path).name} could not be read ({exc})") from None
    try:
        rows = [_cells(r) for r in csv.reader(text.splitlines())]
    except csv.Error as exc:
        raise LayoutChanged(f"{Path(path).name} is not a CSV the mseo report script wrote ({exc})") from None
    if not rows or rows[0][:1] != [MARKER]:
        first = rows[0][0][:60] if rows and rows[0] else ""
        raise LayoutChanged(f"{Path(path).name} does not start with {MARKER!r} (first line {first!r}); not a tab the "
                            "mseo report script wrote")
    if len(rows) < 5 or rows[1][:1] != ["customer_id"] or rows[2][:1] != ["period"] or rows[3][:1] != ["generated"]:
        raise LayoutChanged(f"{Path(path).name} header lines are not customer_id, period, generated")
    if len(rows[1]) < 2 or len(rows[2]) < 3:
        raise LayoutChanged(f"{Path(path).name} header lines carry no customer ID or period")
    customer_id, start, end = rows[1][1], rows[2][1], rows[2][2]
    for day in (start, end):
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            raise ExportError(f"{Path(path).name} period {day!r} is not a date") from None
    header = rows[4]
    table = next((n for n, spec in ALL_TABLES.items() if [g for g, _, _ in spec] == header), None)
    if table is None:
        raise LayoutChanged(f"{Path(path).name}: no table has columns {header}")
    spec = ALL_TABLES[table]
    out = []
    for r in rows[5:]:
        if not r or all(c == "" for c in r):
            continue
        padded = r + [""] * (len(spec) - len(r))
        out.append({name: _value(padded[i], kind) for i, (_, name, kind) in enumerate(spec)})
    generated = rows[3][1] if len(rows[3]) > 1 else ""
    return {"table": table, "customer_id": customer_id, "start": start, "end": end, "generated": generated, "rows": out}


def parse_manifest(paths: list[Path]) -> dict:
    """The one manifest tab of phase one, parsed; anything else that arrived is an error."""
    if len(paths) != 1:
        raise ExportError(f"{len(paths)} file(s) arrived for the manifest ({', '.join(Path(p).name for p in paths)}); expected one")
    parsed = parse_tab(paths[0])
    if parsed["table"] != MANIFEST:
        raise ExportError(f"{Path(paths[0]).name} is the {parsed['table']} table, not the manifest (gid {MANIFEST_GID} is "
                          f"not the first sheet the script wrote)")
    parsed["file"] = Path(paths[0])
    return parsed


def parse_all(paths: list[Path]) -> dict[str, dict]:
    """Every report table once, from one customer and one period, keyed by table name; the optional tabs when they
    arrived."""
    out: dict[str, dict] = {}
    for p in paths:
        parsed = parse_tab(p)
        if parsed["table"] in out:
            raise ExportError(f"table {parsed['table']} arrived twice ({Path(p).name})")
        parsed["file"] = Path(p)
        out[parsed["table"]] = parsed
    missing = [n for n in TABLES if n not in out]
    if missing:
        raise ExportError(f"missing tables: {', '.join(missing)}")
    ids = sorted({p["customer_id"] for p in out.values()})
    if len(ids) != 1:
        raise ExportError(f"the tabs name {len(ids)} customer IDs ({', '.join(ids)}); one sheet holds one account")
    periods = sorted({(p["start"], p["end"]) for p in out.values()})
    if len(periods) != 1:
        raise ExportError(f"the tabs cover {len(periods)} different periods; the script writes them all at once")
    return out


def coverage(parsed: dict[str, dict]) -> tuple[str | None, str | None]:
    first = next(iter(parsed.values()), None)
    return (first["start"], first["end"]) if first else (None, None)


def customer_id(parsed: dict[str, dict]) -> str:
    return next(iter(parsed.values()))["customer_id"]


def write(parsed: dict[str, dict], run_dir: Path) -> list[dict]:
    names = list(TABLES) + [n for n in OPTIONAL_TABLES if n in parsed and n != MANIFEST]
    return [write_table(run_dir, f"{name}.csv", fields(name), parsed[name]["rows"]) for name in names]


def script_errors(parsed: dict[str, dict]) -> list[dict]:
    """The run record's `checks` entry for the script's `_errors` tab: one check, failing when any GAQL failed."""
    if ERRORS not in parsed:
        return []
    rows = [r for r in parsed[ERRORS]["rows"] if (r.get("table") or "").strip()]
    detail = (f"{len(rows)} table(s) failed in the Ads Script: " + ", ".join(sorted({r['table'] for r in rows}))
              if rows else "every table was written by the Ads Script")
    return [{"name": SCRIPT_ERRORS_CHECK, "status": "fail" if rows else "pass", "detail": detail, "evidence": rows}]


# ---------------------------------------------------------------- attestation

REASONS = {"no_navigation": "the Chrome tool output shows no navigation to the configured sheet",
           "other_sheet": "the run reached a docs.google.com URL that is not the configured sheet",
           "off_site": "the run reached a page outside the Google hosts it needs"}


def attest_sheet(calls: list[dict], sheet_id: str) -> str | None:
    """None when every URL the run reached is the configured sheet or an allowed Google page and at least one is the
    sheet; else the reason (REASONS, attest.REASONS)."""
    if attest._second_browser(calls):
        return "two_browsers"
    if attest._second_tab(calls):
        return "two_tabs"
    seen_sheet = False
    for url in attest.reached_urls(calls):
        if url in attest.NEUTRAL:
            continue
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return "off_site"
        if (parts.hostname or "") == SHEET_HOST and parts.scheme == "https":
            m = _SHEET_PATH.match(parts.path)
            if not m or m.group(1) != sheet_id or "@" in url or "\\" in url:
                return "other_sheet"
            seen_sheet = True
            continue
        if not attest.allowed_url(url):
            return "off_site"
    return None if seen_sheet else "no_navigation"


def _why(reason: str) -> str:
    text = REASONS.get(reason) or attest.REASONS[reason]
    return f"the download was not taken: {text} (Chrome tool output is the evidence)"


# ---------------------------------------------------------------- the pull

def _new_files(ws, since: float) -> list[Path]:
    folder = export.inbox(ws)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                  and not p.name.endswith(export.PARTIAL_SUFFIXES) and not p.name.startswith((".", "snapshot-", "totals-"))
                  and p.stat().st_mtime >= since)


def _discard(paths) -> None:
    for p in paths:
        if p is not None:
            Path(p).unlink(missing_ok=True)


def pull(ws, period: dict, *, runner=None, force: bool = False, job_id: str | None = None,
         now: dt.datetime | None = None, signed_in: bool = False, require_seal: bool = True):
    """(exit code, record or None, one line): the sheet path of `mseo pull --source google_ads`."""
    from mseo.capture.account import normalize

    sheet_id = configured(ws)
    runner = runner or browser_run.subprocess_runner
    started = now or dt.datetime.now(dt.timezone.utc)
    dup = export.existing(ws, SOURCE, period)
    if dup and not force:
        return EXIT_EXISTS, None, f"{SOURCE} {period['start']}..{period['end']} already recorded ({', '.join(dup)})"
    ident = ws.identity_for(SOURCE)
    run_id = run_record.new_run_id(SOURCE, now=started)
    run_dir = ws.path("data", "runs", run_id)
    expected = ws.account(SOURCE)
    base = dict(run_id=run_id, job_id=job_id, workspace=ws.slug, source=SOURCE, adapter="export",
                identity=ident["email"], identity_shared=bool(ident["allow_shared"]), period=period,
                account_id_expected=expected, seen_via=f"sheet:{sheet_id}", started=started)

    def finish(status, error, *, seen=None, files=(), usage=None, complete=None, checks=None):
        rec = run_record.build(**base, account_id_seen=seen, status=status, error=error, files=list(files),
                               usage=usage, complete=complete or {"value": False, "reason": status}, checks=checks,
                               finished=now or dt.datetime.now(dt.timezone.utc))
        run_record.write(run_dir, rec)
        code = EXIT_OK if status in ("ok", "empty") else EXIT_FAIL
        return code, rec, f"{status}: {SOURCE} {period['start']}..{period['end']} -> {run_dir}"

    if not signed_in:
        sign = identity.sign_in_check(ws, SOURCE, period=period, runner=runner, now=now, job_id=job_id,
                                      require_seal=require_seal)
        if sign["status"] != "ok":
            return finish(sign["status"], f"sign-in check: {sign['error']}")
    export.inbox(ws).mkdir(parents=True, exist_ok=True)
    usage = None

    def phase(gids):
        """One agent run: (result, arrived files) or a finished record on failure."""
        nonlocal usage
        since = time.time() - 1
        res = browser_run.run_agent(ws, browser_run.prompt(PROMPT, ws, source=SOURCE, **values(ws, period, gids)),
                                    runner=runner, require_seal=require_seal)
        usage = _add_usage(usage, res.usage)
        arrived = _new_files(ws, since)
        if not res.ok:
            _discard(arrived)
            return finish("auth_failed" if res.status == "auth_failed" else "error", res.error, usage=usage), None
        if res.reply.lower().startswith("failed"):
            _discard(arrived)
            gone = f"; discarded {len(arrived)} file(s)" if arrived else "; no download arrived"
            return finish("error", f"the agent replied: {res.reply[:300]}{gone}", usage=usage), None
        reason = attest_sheet(res.calls, sheet_id)
        if reason:
            _discard(arrived)
            return finish("unverified_account", _why(reason), usage=usage), None
        if not arrived:
            return finish("error", f"no download arrived in {export.inbox(ws)}", usage=usage), None
        return None, arrived

    # phase one: the manifest on gid 0, kept in the run folder before phase two so it never counts as a table
    done, arrived = phase(None)
    if done:
        return done
    try:
        manifest = parse_manifest(arrived)
        gids = table_gids(manifest["rows"])
        manifest_file = export.ingest(ws, manifest["file"], run_dir, name=f"export-{MANIFEST}.csv")
    except LayoutChanged as exc:
        _discard(arrived)
        return finish("layout_changed", str(exc), usage=usage)
    except ExportError as exc:
        _discard(arrived)
        return finish("error", str(exc), usage=usage)
    except Exception:
        _discard(arrived)
        raise
    # phase two: every table by gid
    done, arrived = phase(gids)
    if done:
        return done
    try:
        parsed = parse_all(arrived)
    except LayoutChanged as exc:
        _discard(arrived)
        return finish("layout_changed", str(exc), usage=usage)
    except ExportError as exc:
        _discard(arrived)
        return finish("error", str(exc), usage=usage)
    except Exception:
        _discard(arrived)
        raise
    if normalize(manifest["customer_id"]) != normalize(customer_id(parsed)) or \
            (manifest["start"], manifest["end"]) != coverage(parsed):
        _discard(arrived)
        return finish("error", "the manifest and the tables name different customers or periods; the script ran between "
                      "the two downloads", usage=usage)
    seen = customer_id(parsed)
    if normalize(seen) != normalize(expected):
        _discard(arrived)
        return finish("wrong_account", f"the tabs name customer {seen}, expected {expected}; nothing was saved",
                      seen=seen, usage=usage)
    first, last = coverage(parsed)
    if (first, last) != (period["start"], period["end"]):
        _discard(arrived)
        return finish("error", f"the tabs cover the period {first} to {last}, not {period['start']} to {period['end']}",
                      seen=seen, usage=usage)
    originals = [manifest_file] + [export.ingest(ws, parsed[name]["file"], run_dir, name=f"export-{name}.csv")
                                   for name in parsed]
    tables = write(parsed, run_dir)
    checks = script_errors(parsed)
    if any(c["status"] == "fail" for c in checks):
        complete = {"value": False, "reason": checks[0]["detail"]}
    elif export.within_lag(period, LAG_DAYS):
        complete = {"value": False, "reason": "within reporting lag"}
    else:
        complete = {"value": True, "reason": None}
    return finish("ok", None, seen=seen, files=[*originals, *tables], usage=usage, complete=complete, checks=checks)


def _add_usage(total: dict | None, more: dict | None) -> dict | None:
    if not more:
        return total
    if not total:
        return dict(more)
    out = dict(total)
    for k, v in more.items():
        out[k] = (out.get(k) or 0) + v if isinstance(v, (int, float)) else v
    return out
