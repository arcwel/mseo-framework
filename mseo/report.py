"""`mseo report monthly --workspace S --month YYYY-MM [--goal TEXT] [--skeleton] [--json]`: the customer's month report.

Reads only what the month's commands wrote, all of it tolerant of a missing file: the week files touching the month
(and the previous month's for the comparison), each week's check and Ads audit files, the action batches, the content
log, the latest site audit and the month's AI-answers file. Every figure comes from those files: a source with no
week is "not available this month", never 0, and a month-over-month delta appears only when both months have every
week complete for that source.

The text is written for the customer: it leads with what improved, each plan item names the metric it moves, no
internal or tool word appears, and the workspace's content.wording rules are applied (a `never` match becomes its
`always` phrase; one without a replacement is exit 1 naming the line). Written to data/reports/<YYYY-MM>.md.
--skeleton leaves `TODO:` markers where a human sentence belongs; without it a leftover `TODO:` is exit 1.
Exit 0 written, 1 wording or TODO left, 3 no week touches the month or the month is malformed.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from pathlib import Path

from mseo.content import wording
from mseo.fsutil import write_atomic
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
NOT_AVAILABLE = "not available this month"
NO_COMPARISON = "comparison not available"
TODO = "TODO:"
TOP_CHANNELS = 3
SITE_FAILS_SHOWN = 5
OPEN_ITEM_STATES = ("proposed", "approved")
DRAFT_STATES = ("draft", "checked")
PUBLISHED_STATES = ("published",)
ALL_CAMPAIGNS = "*"
SOURCES = ("search_console", "ga4", "google_ads")
GA4_SUMS = ("sessions", "engagedSessions", "keyEvents")
ADS_SUMS = ("cost", "clicks", "conversions", "calls")
ITEM_WORDS = {"negative_keyword": "Blocked the search term '{text}' in {where}",
              "pause_keyword": "Paused the keyword '{text}' in {where}",
              "pause_ad_group": "Paused the ad group {ad_group} in {campaign}",
              "budget": "Changed the daily budget of {campaign} from {before} to {after}",
              "ad_schedule": "Stopped showing ads during hour {hour}"}
PLAN_WORDS = {"negative_keyword": "Block the search term '{text}' in {where}",
              "pause_keyword": "Pause the keyword '{text}' in {where}",
              "pause_ad_group": "Pause the ad group {ad_group} in {campaign}",
              "budget": "Change the daily budget of {campaign} from {before} to {after}",
              "ad_schedule": "Stop showing ads during hour {hour}"}


class ReportError(Exception):
    """The month cannot be reported. Maps to exit 3."""


# ---------------------------------------------------------------- dates

def month_period(month: str) -> tuple[dt.date, dt.date]:
    m = MONTH_RE.match(month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ReportError(f"--month must be YYYY-MM, got {month!r}")
    year, num = int(m.group(1)), int(m.group(2))
    return dt.date(year, num, 1), dt.date(year, num, calendar.monthrange(year, num)[1])


def previous_month(month: str) -> str:
    first, _ = month_period(month)
    last_of_prev = first - dt.timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def weeks_of_month(month: str) -> list[str]:
    """Every ISO week with at least one day in the month, in order."""
    first, last = month_period(month)
    out: list[str] = []
    day = first
    while day <= last:
        y, w, _ = day.isocalendar()
        label = f"{y}-W{w:02d}"
        if label not in out:
            out.append(label)
        day += dt.timedelta(days=1)
    return out


def month_name(month: str) -> str:
    first, _ = month_period(month)
    return f"{calendar.month_name[first.month]} {first.year}"


# ---------------------------------------------------------------- readers (all tolerant)

def _json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_weeks(ws, month: str) -> dict[str, dict | None]:
    """{week: file or None} for every week touching the month."""
    folder = ws.path("data", "weeks")
    return {w: _json(folder / f"{w}.json") for w in weeks_of_month(month)}


def _entries(weeks: dict[str, dict | None], source: str) -> dict[str, dict | None]:
    out = {}
    for w, data in weeks.items():
        e = ((data or {}).get("sources") or {}).get(source)
        out[w] = e if isinstance(e, dict) and e.get("status") == "ok" and isinstance(e.get("metrics"), dict) else None
    return out


def _sum(entries: list[dict], key: str, *, nested: str = "metrics") -> float | int | None:
    vals = [e[nested].get(key) for e in entries if isinstance(e.get(nested), dict) and e[nested].get(key) is not None]
    if not vals:
        return None
    total = sum(vals)
    return int(total) if float(total).is_integer() else round(total, 6)


def _coverage(weeks: dict[str, dict | None], source: str) -> dict:
    entries = _entries(weeks, source)
    ok = [e for e in entries.values() if e]
    complete = [w for w, e in entries.items() if e and e.get("complete")]
    return {"weeks": len(weeks), "weeks_ok": len(ok), "weeks_complete": len(complete),
            "complete": bool(weeks) and len(complete) == len(weeks),
            "missing_weeks": [w for w, e in entries.items() if not e],
            "incomplete_weeks": [w for w, e in entries.items() if e and not e.get("complete")], "entries": ok}


def search_totals(weeks: dict[str, dict | None]) -> dict | None:
    cov = _coverage(weeks, "search_console")
    if not cov["entries"]:
        return None
    clicks, impressions = _sum(cov["entries"], "clicks"), _sum(cov["entries"], "impressions")
    weighted = sum((e["metrics"].get("position") or 0) * (e["metrics"].get("impressions") or 0) for e in cov["entries"])
    out = {"clicks": clicks, "impressions": impressions,
           "ctr": round(clicks / impressions, 6) if clicks is not None and impressions else None,
           "position": round(weighted / impressions, 2) if impressions else None}
    return {**out, **{k: v for k, v in cov.items() if k != "entries"}}


def visits_totals(weeks: dict[str, dict | None]) -> dict | None:
    cov = _coverage(weeks, "ga4")
    if not cov["entries"]:
        return None
    channels: dict[str, float] = {}
    for e in cov["entries"]:
        for name, m in (e.get("by_channel") or {}).items():
            channels[name] = channels.get(name, 0) + (m.get("sessions") or 0)
    top = sorted(channels.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_CHANNELS]
    out = {k: _sum(cov["entries"], k) for k in GA4_SUMS}
    out["top_channels"] = [{"channel": c, "sessions": int(s) if float(s).is_integer() else s} for c, s in top]
    return {**out, **{k: v for k, v in cov.items() if k != "entries"}}


def ads_totals(weeks: dict[str, dict | None]) -> dict | None:
    cov = _coverage(weeks, "google_ads")
    if not cov["entries"]:
        return None
    out = {k: _sum(cov["entries"], k) for k in ADS_SUMS}
    cost, conv = out.get("cost"), out.get("conversions")
    out["cost_per_conversion"] = round(cost / conv, 2) if cost is not None and conv else None
    return {**out, **{k: v for k, v in cov.items() if k != "entries"}}


def _in_month(date: str | None, month: str) -> bool:
    return bool(date) and str(date)[:7] == month


def batches(ws) -> list[dict]:
    folder = ws.path("data", "batches")
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        b = _json(path)
        if b and isinstance(b.get("items"), list):
            out.append(b)
    return out


def _batch_in_month(b: dict, month: str, weeks: list[str]) -> bool:
    return _in_month(b.get("created"), month) or b.get("week") in weeks


def applied_items(ws, month: str, weeks: list[str]) -> list[dict]:
    """Items applied in the month: by applied_at when the item carries it, else by the batch's created date or week."""
    out = []
    for b in batches(ws):
        for i in b["items"]:
            if not isinstance(i, dict) or i.get("status") != "applied":
                continue
            when = i.get("applied_at")
            if (_in_month(when, month) if when else _batch_in_month(b, month, weeks)):
                out.append(i)
    return out


def open_items(ws) -> list[dict]:
    return [i for b in batches(ws) for i in b["items"] if isinstance(i, dict) and i.get("status") in OPEN_ITEM_STATES]


def content_log(ws) -> dict:
    return _json(ws.file("content_log")) or {}


def published(log: dict, month: str) -> dict:
    arts = [a for a in log.get("articles", []) if a.get("status") in PUBLISHED_STATES and _in_month(a.get("date"), month)]
    posts = [g for g in log.get("gbp", []) if g.get("status") in PUBLISHED_STATES and _in_month(g.get("date"), month)]
    return {"articles": arts, "posts": posts}


def drafts(log: dict) -> dict:
    return {"articles": [a for a in log.get("articles", []) if a.get("status") in DRAFT_STATES],
            "posts": [g for g in log.get("gbp", []) if g.get("status") in ("draft", "scheduled")]}


def site_audit(ws) -> dict | None:
    folder = ws.path("data", "audits")
    files = sorted(folder.glob("*-site.json")) if folder.is_dir() else []
    audit = _json(files[-1]) if files else None
    if not audit:
        return None
    rows = audit.get("findings") if isinstance(audit.get("findings"), list) else audit.get("checks") or []
    fails = [r for r in rows if isinstance(r, dict) and (r.get("level") or r.get("status")) == "fail"]
    return {"date": audit.get("date") or files[-1].stem.replace("-site", ""), "fails": len(fails),
            "checks": [{"check": _plain(str(r.get("check") or r.get("name") or "check")), "url": r.get("url") or r.get("page") or "",
                        "detail": r.get("detail") or ""} for r in fails]}


def ai_answers(ws, month: str) -> dict | None:
    data = _json(ws.path("data", "ai-visibility", f"{month}.json"))
    overall = ((data or {}).get("scores") or {}).get("overall") if data else None
    if not isinstance(overall, dict):
        return None
    return {"answers": overall.get("answers"), "cited_site_share": overall.get("cited_site_share"),
            "cited_profile_share": overall.get("cited_profile_share"), "mentioned_name_share": overall.get("mentioned_name_share")}


def week_checks(ws, weeks: list[str]) -> dict:
    """How many of the month's weeks had their figures confirmed against Google's own pages."""
    folder = ws.path("data", "weeks")
    status = {w: ((_json(folder / f"{w}.shadow.json") or {}).get("status")) for w in weeks}
    return {"confirmed": [w for w, s in status.items() if s == "pass"], "differed": [w for w, s in status.items() if s == "fail"],
            "unchecked": [w for w, s in status.items() if s not in ("pass", "fail")]}


def ads_account_checks(ws, weeks: list[str]) -> list[str]:
    """Failing Ads account checks from the latest week that has an audit file, in plain words."""
    folder = ws.path("data", "weeks")
    for w in reversed(weeks):
        audit = _json(folder / f"{w}.ads-audit.json")
        if audit and isinstance(audit.get("checks"), list):
            return sorted({_plain(str(c.get("name") or "")) for c in audit["checks"] if isinstance(c, dict) and c.get("status") == "fail"})
    return []


# ---------------------------------------------------------------- wording

def _plain(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip()


def _n(v, decimals: int | None = None) -> str:
    if v is None:
        return "not available"
    if isinstance(v, bool):
        return str(v)
    if decimals is not None:
        return f"{v:,.{decimals}f}"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    return f"{int(v):,}"


def _pct(v) -> str:
    return "not available" if v is None else f"{v:.1%}"


def _delta(now, before, *, lower_is_better: bool = False, decimals: int | None = None) -> str:
    if now is None or before is None:
        return ""
    if before == 0:
        return f" (last month {_n(before, decimals)})"
    change = (now - before) / abs(before)
    if round(change, 2) == 0:
        return f" (last month {_n(before, decimals)}, unchanged)"
    better = change < 0 if lower_is_better else change > 0
    return f" (last month {_n(before, decimals)}, {change:+.0%}, {'better' if better else 'worse'})"


def _improvements(cur: dict | None, prev: dict | None, keys: list[tuple[str, str, bool]]) -> list[str]:
    """Plain sentences for every metric that got better, when both months are complete for the source."""
    if not cur or not prev or not cur.get("complete") or not prev.get("complete"):
        return []
    out = []
    for key, label, lower in keys:
        a, b = cur.get(key), prev.get(key)
        if a is None or b is None or a == b:
            continue
        if (a < b) if lower else (a > b):
            out.append(f"{label} {'fell' if a < b else 'rose'} from {_n(b)} to {_n(a)}")
    return out


def _where(t: dict) -> str:
    campaign = t.get("campaign")
    campaign = "all campaigns" if campaign == ALL_CAMPAIGNS else campaign
    return " / ".join(str(p) for p in (campaign, t.get("ad_group")) if p) or "the account"


def item_words(item: dict, *, plan: bool = False) -> str:
    t = item.get("target") or {}
    before, after = item.get("before") or {}, item.get("after") or {}
    template = (PLAN_WORDS if plan else ITEM_WORDS).get(item.get("type") or "")
    fields = {"text": t.get("text", ""), "where": _where(t), "campaign": _where({"campaign": t.get("campaign")}),
              "ad_group": t.get("ad_group", ""), "hour": t.get("hour", ""),
              "before": _n(before.get("budget")) if isinstance(before, dict) else "", "after": _n(after.get("budget")) if isinstance(after, dict) else ""}
    text = template.format(**fields) if template else f"{_plain(str(item.get('type') or 'change')).capitalize()} on {_where(t)}"
    metric = item.get("expected_metric")
    line = text + (f"; metric: {metric}" if metric else "")
    if not plan:
        line += measured_words(item.get("measured"))
    return line


def measured_words(m) -> str:
    """'; measured: cost 40.00 -> 31.50, conversions 3 -> 4 over 4 week(s) after' from an item's measured block."""
    if not isinstance(m, dict) or not isinstance(m.get("before"), dict) or not isinstance(m.get("after"), dict):
        return ""
    if not m.get("weeks_after"):
        return "; measured: not enough weeks after the change yet"
    money = lambda k, v: _n(v, 2) if k == "cost" and v is not None else _n(v)  # noqa: E731
    pairs = [f"{_plain(k)} {money(k, m['before'].get(k))} -> {money(k, v)}" for k, v in m["after"].items() if isinstance(v, (int, float))]
    tail = "" if m.get("status") == "complete" else " so far"
    return f"; measured: {', '.join(pairs)} over the {_n(m.get('weeks_after'))} week(s) after{tail}"


def apply_wording(text: str, rules: list[tuple[str, str]]) -> tuple[str, list[tuple[int, str, str]]]:
    """Replace every banned phrase that has a house phrase; return the text and what still remains."""
    for pat, fix in rules:
        if fix != wording.NO_REPLACEMENT:
            try:
                text = re.sub(pat, fix, text, flags=re.I)
            except re.error:
                continue
    return text, wording.check(text, rules)


# ---------------------------------------------------------------- sections

def _todo(line: str, skeleton: bool) -> str:
    return f"{TODO} {line}" if skeleton else line


def summary_section(r: dict, *, skeleton: bool) -> list[str]:
    improved = r["improved"]
    changed = []
    if r["ads_changes"]:
        changed.append(f"{len(r['ads_changes'])} change(s) to the ads account")
    if r["content"]["articles"]:
        changed.append(f"{len(r['content']['articles'])} article(s) published")
    if r["content"]["posts"]:
        changed.append(f"{len(r['content']['posts'])} Business Profile post(s) published")
    if improved:
        first = "What improved: " + "; ".join(improved[:3]) + "."
    elif r["comparable"]:
        first = "What improved: no figure beat last month; the plan below says what changes."
    else:
        first = "What improved: a comparison with last month is not available yet; this report sets the baseline."
    out = ["## Summary", ""]
    if r.get("goal"):
        out.append(f"Goal: {r['goal']}")
        out.append("")
    second = "What we changed: " + ("; ".join(changed) if changed else "no change was applied this month") + "."
    third = "What is next: " + (f"{len(r['plan'])} item(s) in the plan below" if r["plan"] else "nothing is waiting on a decision") + "."
    out += [f"- {_todo(first, skeleton)}", f"- {_todo(second, skeleton)}", f"- {_todo(third, skeleton)}"]
    return out


def search_section(cur: dict | None, prev: dict | None) -> list[str]:
    out = ["## Search", ""]
    if not cur:
        return out + [f"Google Search results: {NOT_AVAILABLE}."]
    cmp_ok = bool(prev) and cur["complete"] and prev["complete"]
    p = prev if cmp_ok else {}
    out += [f"- Clicks from Google Search: {_n(cur['clicks'])}{_delta(cur['clicks'], p.get('clicks'))}",
            f"- Times the site appeared in results: {_n(cur['impressions'])}{_delta(cur['impressions'], p.get('impressions'))}",
            f"- Click-through rate: {_pct(cur['ctr'])}" + (f" (last month {_pct(p['ctr'])})" if cmp_ok and p.get("ctr") is not None else ""),
            f"- Average position: {_n(cur['position'], 1)}{_delta(cur['position'], p.get('position'), lower_is_better=True, decimals=1)}"]
    if not cmp_ok:
        out.append(f"- Month over month: {NO_COMPARISON}")
    if not cur["complete"]:
        out.append(f"- Figures cover {cur['weeks_complete']} complete week(s) of {cur['weeks']}")
    return out


def visits_section(cur: dict | None, prev: dict | None) -> list[str]:
    out = ["## Website visits", ""]
    if not cur:
        return out + [f"Google Analytics: {NOT_AVAILABLE}."]
    cmp_ok = bool(prev) and cur["complete"] and prev["complete"]
    p = prev if cmp_ok else {}
    out += [f"- Visits: {_n(cur['sessions'])}{_delta(cur['sessions'], p.get('sessions'))}",
            f"- Engaged visits: {_n(cur['engagedSessions'])}{_delta(cur['engagedSessions'], p.get('engagedSessions'))}",
            f"- Key actions (calls, forms, directions): {_n(cur['keyEvents'])}{_delta(cur['keyEvents'], p.get('keyEvents'))}"]
    if cur["top_channels"]:
        out.append("- Where visits came from: " + ", ".join(f"{c['channel']} {_n(c['sessions'])}" for c in cur["top_channels"]))
    if not cmp_ok:
        out.append(f"- Month over month: {NO_COMPARISON}")
    if not cur["complete"]:
        out.append(f"- Figures cover {cur['weeks_complete']} complete week(s) of {cur['weeks']}")
    return out


def ads_section(cur: dict | None, prev: dict | None, changes: list[dict], account_checks: list[str]) -> list[str]:
    out = ["## Ads", ""]
    if not cur:
        out.append(f"Google Ads figures: {NOT_AVAILABLE}.")
    else:
        cmp_ok = bool(prev) and cur["complete"] and prev["complete"]
        p = prev if cmp_ok else {}
        out += [f"- Spend: {_n(cur['cost'], 2)}{_delta(cur['cost'], p.get('cost'), lower_is_better=True, decimals=2)}",
                f"- Clicks: {_n(cur['clicks'])}{_delta(cur['clicks'], p.get('clicks'))}",
                f"- Conversions: {_n(cur['conversions'])}{_delta(cur['conversions'], p.get('conversions'))}"]
        if cur.get("calls") is not None:
            out.append(f"- Calls: {_n(cur['calls'])}{_delta(cur['calls'], p.get('calls'))}")
        out.append(f"- Cost per conversion: {_n(cur['cost_per_conversion'], 2)}"
                   f"{_delta(cur['cost_per_conversion'], p.get('cost_per_conversion'), lower_is_better=True, decimals=2)}")
        if not cmp_ok:
            out.append(f"- Month over month: {NO_COMPARISON}")
        if not cur["complete"]:
            out.append(f"- Figures cover {cur['weeks_complete']} complete week(s) of {cur['weeks']}")
    out += ["", "Changes applied this month:"] + ([f"- {item_words(i)}" for i in changes] or ["- none"])
    if account_checks:
        out += ["", "Account checks that need attention: " + ", ".join(account_checks)]
    return out


def content_section(pub: dict) -> list[str]:
    out = ["## Content", ""]
    for a in pub["articles"]:
        m = a.get("measured_28d") if isinstance(a.get("measured_28d"), dict) else None
        measured = (f"; {_n(m.get('clicks'))} clicks from Google Search in the first 28 days"
                    + ("" if m.get("status") == "complete" else f" (so far, {_n(m.get('weeks_counted'))} week(s) counted)")) if m else ""
        out.append(f"- Article: {a.get('title') or a.get('slug')} ({a.get('date')}) {a.get('url') or 'live address not recorded'}{measured}")
    for g in pub["posts"]:
        out.append(f"- Business Profile post: {g.get('headline', '')} ({g.get('date')})" + (f" {g['url']}" if g.get("url") else ""))
    if not pub["articles"] and not pub["posts"]:
        out.append("No article or Business Profile post was published this month.")
    return out


def site_section(site: dict | None) -> list[str]:
    out = ["## Site health", ""]
    if site is None:
        return out + [f"Website check: {NOT_AVAILABLE}."]
    if not site["fails"]:
        return out + [f"Website check on {site['date']}: every check passed."]
    out.append(f"Website check on {site['date']}: {site['fails']} check(s) need a fix.")
    out += [f"- {c['check']}: {c['detail']}" + (f" ({c['url']})" if c["url"] else "") for c in site["checks"][:SITE_FAILS_SHOWN]]
    return out


def ai_section(ai: dict | None) -> list[str]:
    out = ["## AI answers", ""]
    if ai is None:
        return out + [f"Share of AI answers citing the website or the Business Profile: {NOT_AVAILABLE}."]
    return out + [f"Of {_n(ai['answers'])} AI answers checked, {_pct(ai['cited_site_share'])} cited the website, "
                  f"{_pct(ai['cited_profile_share'])} cited the Business Profile and {_pct(ai['mentioned_name_share'])} mentioned the business by name."]


def plan_items(open_ads: list[dict], draft: dict, site: dict | None) -> list[str]:
    items = [item_words(i, plan=True) for i in open_ads]
    items += [f"Approve the article '{a.get('title') or a.get('slug')}'; metric: clicks from Google Search to a new page" for a in draft["articles"]]
    items += [f"Approve the Business Profile post '{g.get('headline', '')}' for {g.get('date')}; metric: profile views and calls" for g in draft["posts"]]
    if site and site["fails"]:
        items += [f"Fix the website check '{c['check']}'" + (f" on {c['url']}" if c["url"] else "") + "; metric: pages Google can index and rank"
                  for c in site["checks"][:SITE_FAILS_SHOWN]]
    return items


def plan_section(items: list[str], goal: str | None, *, skeleton: bool) -> list[str]:
    out = ["## Next month plan", ""]
    if goal:
        out.append(f"Every item below works toward: {goal}.")
    if skeleton:
        out.append(f"{TODO} one paragraph on the month ahead")
    out += [f"{i}. {line}" for i, line in enumerate(items, 1)] or ["Nothing is waiting on a decision."]
    return out


def notes_section(notes: list[str]) -> list[str]:
    return ["## Notes", ""] + ([f"- {n}" for n in notes] or ["- Every source had a complete month."])


def _notes(cur: dict, prev: dict, prev_month: str, checks: dict, site, ai) -> list[str]:
    labels = {"search": "Google Search", "visits": "Google Analytics", "ads": "Google Ads"}
    out = []
    for key, label in labels.items():
        c = cur.get(key)
        if c is None:
            out.append(f"{label}: no figures for the month")
            continue
        if c["missing_weeks"] or c["incomplete_weeks"]:
            parts = []
            if c["missing_weeks"]:
                parts.append("no figures for week(s) " + ", ".join(c["missing_weeks"]))
            if c["incomplete_weeks"]:
                parts.append("week(s) " + ", ".join(c["incomplete_weeks"]) + " still within Google's reporting delay")
            out.append(f"{label}: " + "; ".join(parts))
        p = prev.get(key)
        if p is None or not p["complete"]:
            out.append(f"{label}: {month_name(prev_month)} is not complete, so no month-over-month comparison")
    if checks["confirmed"]:
        out.append(f"Figures for {len(checks['confirmed'])} week(s) were confirmed against Google's own pages")
    if checks["differed"]:
        out.append("Figures for week(s) " + ", ".join(checks["differed"]) + " differed from Google's pages and were reviewed by hand")
    if site is None:
        out.append("No website check ran this month")
    if ai is None:
        out.append("No AI-answers check ran this month")
    out.append("Monthly figures are totals of the weeks that touch the month, so a week shared with a neighbouring month counts in both")
    return out


# ---------------------------------------------------------------- build

def build(ws, month: str, *, goal: str | None = None, skeleton: bool = False) -> dict:
    """The structured numbers plus the markdown under "report". Raises ReportError when no week touches the month."""
    weeks = load_weeks(ws, month)
    if not any(weeks.values()):
        raise ReportError(f"no week file touches {month} under data/weeks; run `mseo week assemble` for its weeks first")
    prev_month = previous_month(month)
    prev_weeks = load_weeks(ws, prev_month)
    cur = {"search": search_totals(weeks), "visits": visits_totals(weeks), "ads": ads_totals(weeks)}
    prev = {"search": search_totals(prev_weeks), "visits": visits_totals(prev_weeks), "ads": ads_totals(prev_weeks)}
    week_list = list(weeks)
    log = content_log(ws)
    pub, draft = published(log, month), drafts(log)
    site, ai = site_audit(ws), ai_answers(ws, month)
    changes, open_ads = applied_items(ws, month, week_list), open_items(ws)
    improved = (_improvements(cur["search"], prev["search"], [("clicks", "Clicks from Google Search", False), ("position", "Average position", True)])
                + _improvements(cur["visits"], prev["visits"], [("sessions", "Website visits", False), ("keyEvents", "Key actions", False)])
                + _improvements(cur["ads"], prev["ads"], [("conversions", "Ads conversions", False), ("calls", "Calls from ads", False),
                                                         ("cost_per_conversion", "Cost per conversion", True)]))
    comparable = any(cur[k] and prev[k] and cur[k]["complete"] and prev[k]["complete"] for k in cur)
    plan = plan_items(open_ads, draft, site)
    checks = week_checks(ws, week_list)
    notes = _notes(cur, prev, prev_month, checks, site, ai)
    r = {"status": "ok", "month": month, "previous_month": prev_month, "goal": goal, "weeks": week_list,
         "search": _with_prev(cur["search"], prev["search"]), "visits": _with_prev(cur["visits"], prev["visits"]),
         "ads": _with_prev(cur["ads"], prev["ads"]), "ads_changes": changes, "content": pub, "drafts": draft, "site": site,
         "ai": ai, "improved": improved, "comparable": comparable, "plan": plan, "notes": notes}
    lines = [f"# {ws.config['business']['name']}: {month_name(month)}", ""]
    lines += summary_section(r, skeleton=skeleton) + [""]
    lines += search_section(cur["search"], prev["search"]) + [""]
    lines += visits_section(cur["visits"], prev["visits"]) + [""]
    lines += ads_section(cur["ads"], prev["ads"], changes, ads_account_checks(ws, week_list)) + [""]
    lines += content_section(pub) + [""]
    lines += site_section(site) + [""]
    lines += ai_section(ai) + [""]
    lines += plan_section(plan, goal, skeleton=skeleton) + [""]
    lines += notes_section(notes)
    text, remaining = apply_wording("\n".join(lines) + "\n", wording.rules(ws))
    r["report"], r["wording_problems"] = text, [{"line": l, "found": h, "use": f} for l, h, f in remaining]
    return r


def _with_prev(cur: dict | None, prev: dict | None) -> dict | None:
    if cur is None:
        return None
    return {**cur, "previous": prev}


def write(ws, month: str, text: str) -> Path:
    out = ws.path("data", "reports", f"{month}.md")
    write_atomic(out, text)
    return out


def cmd_monthly(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        r = build(ws, args.month, goal=args.goal, skeleton=args.skeleton)
    except ReportError as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    problems = [f"line {p['line']}: '{p['found']}' is banned wording; use {p['use']}" for p in r["wording_problems"]]
    if not args.skeleton:
        problems += [f"line {n}: fill the notes ({line.strip()})" for n, line in enumerate(r["report"].splitlines(), 1) if TODO in line]
    if problems:
        ctx.emit({**{k: v for k, v in r.items() if k != "report"}, "status": "fail", "problems": problems,
                  "messages": problems + [f"{len(problems)} problem(s); nothing written"]})
        return EXIT_FAIL
    path = write(ws, args.month, r["report"])
    ctx.emit({**r, "path": str(path), "messages": r["report"].splitlines() + [f"written: {path}"]})
    return EXIT_OK


def register(sub, parents) -> None:
    p = sub.add_parser("report", help="Reports for the customer")
    rsub = p.add_subparsers(dest="report_command", required=True, parser_class=type(p))
    m = rsub.add_parser("monthly", parents=[parents.workspace, parents.json],
                        help="Write data/reports/<YYYY-MM>.md: search, visits, ads, content, site health, AI answers, next month plan")
    m.add_argument("--month", required=True, help="Calendar month, e.g. 2026-10")
    m.add_argument("--goal", help="The customer's goal; the plan is written toward it")
    m.add_argument("--skeleton", action="store_true", help="Leave TODO: markers where a human sentence belongs")
    m.set_defaults(func=cmd_monthly, command_id="report monthly")
