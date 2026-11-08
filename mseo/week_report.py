"""`mseo week report --workspace S --week W`: the weekly markdown the operator reads.

Reads only what the week's commands wrote: data/weeks/<W>.json (source status first; a gap is "not available",
never zero), <W>.shadow.json, <W>.tracking.json, <W>.ads-audit.json, <W>.topics.json, data/batches/*.json,
the content log (drafts awaiting approval, measured pieces) and the latest data/audits/*-site.json. Every missing
file is one line saying so. Writes data/weeks/<W>.report.md and prints it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from mseo import week as week_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import write_atomic


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fmt(v) -> str:
    if v is None:
        return "not available"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def sources_section(week: dict) -> list[str]:
    out = ["## Sources", "", "| Source | Status | Complete | Metrics |", "|---|---|---|---|"]
    for name, e in week.get("sources", {}).items():
        if e.get("status") != "ok":
            out.append(f"| {name} | not available | | {e.get('reason', '')} |")
            continue
        metrics = ", ".join(f"{k} {_fmt(v)}" for k, v in (e.get("metrics") or {}).items())
        flag = "yes" if e.get("complete") else "no"
        shared = " (shared identity)" if e.get("identity_shared") else ""
        out.append(f"| {name} | ok{shared} | {flag} | {metrics} |")
    return out


def shadow_section(shadow: dict | None) -> list[str]:
    if not shadow:
        return ["## Shadow check", "", "not run (mseo shadow compare)"]
    rows = [r for r in shadow.get("rows", []) if not r.get("ok")]
    out = ["## Shadow check", "", f"result: **{shadow.get('status')}**"]
    out += [f"- {r.get('source')} {r.get('metric')}: ours {_fmt(r.get('ours'))}, page {_fmt(r.get('page'))} ({r.get('note', '')})"
            for r in rows[:10]]
    return out


def ads_section(tracking: dict | None, audit: dict | None) -> list[str]:
    out = ["## Google Ads", ""]
    if tracking:
        bad = [c for c in tracking.get("checks", []) if c.get("status") == "fail"]
        out.append(f"tracking gate: **{'fail' if bad else 'pass'}**" + (f" ({len(bad)} failing: " + ", ".join(c['name'] for c in bad) + ")" if bad else ""))
    else:
        out.append("tracking gate: not run (mseo ads tracking)")
    if audit:
        checks = audit.get("checks", [])
        counts = {s: sum(1 for c in checks if c.get("status") == s) for s in ("fail", "warn", "pass", "unchecked")}
        out.append(f"audit: {counts['fail']} fail, {counts['warn']} warn, {counts['pass']} pass, {counts['unchecked']} unchecked")
        out += [f"- {c['status']}: {c['name']}: {c.get('detail', '')}" for c in checks if c.get("status") in ("fail", "warn")]
    else:
        out.append("audit: not run (mseo ads audit)")
    return out


def batches_section(ws) -> list[str]:
    folder = ws.path("data", "batches")
    out = ["## Action batches", ""]
    found = False
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        b = _json(path)
        if not b or not isinstance(b.get("items"), list):
            continue
        found = True
        by = {}
        for it in b["items"]:
            by[it.get("status", "proposed")] = by.get(it.get("status", "proposed"), 0) + 1
        state = ", ".join(f"{n} {s}" for s, n in sorted(by.items()))
        out.append(f"- {b.get('batch_id')} (week {b.get('week')}): {state}")
        if by.get("proposed"):
            out.append(f"  approve with: mseo actions approve --workspace {ws.slug} --batch {b.get('batch_id')} --items N,M --by NAME")
    if not found:
        out.append("none (mseo actions propose)")
    return out


def topics_section(topics: dict | None) -> list[str]:
    if not topics:
        return ["## Topic shortlist", "", "not run (mseo content topics)"]
    items = topics.get("topics") or topics.get("shortlist") or []
    out = ["## Topic shortlist", ""]
    out += [f"{i}. {t.get('title') or t.get('query')}" + (f" (local: {t['local']})" if t.get("local") else "")
            for i, t in enumerate(items[:8], 1)] or ["no candidates"]
    return out


def content_section(ws, today: dt.date) -> list[str]:
    log = _json(ws.file("content_log")) or {}
    out = ["## Content", ""]
    drafts = []
    for a in log.get("articles", []):
        if a.get("status") in ("draft", "checked"):
            check = _json(ws.path("drafts", f"{a.get('slug')}.md.check.json"))
            result = (check or {}).get("result", "not checked")
            drafts.append(f"- article {a.get('slug')}: check {result}; approve with: mseo publish drafts/{a.get('slug')}.md --workspace {ws.slug} --kind article --slug {a.get('slug')} --approved \"NAME {today.isoformat()}\"")
    for g in log.get("gbp", []):
        if g.get("status") in ("draft", "scheduled"):
            drafts.append(f"- Business Profile post {g.get('date')}: {g.get('headline', '')[:60]} ({g.get('status')})")
    out += ["awaiting approval:"] + (drafts or ["- none"])
    measured = [a for a in log.get("articles", []) if isinstance(a.get("measured_28d"), dict)]
    recent = [a for a in measured if (a["measured_28d"].get("measured_on") or "") >= (today - dt.timedelta(days=7)).isoformat()]
    out += ["", "measured in the last 7 days:"] + (
        [f"- {a['slug']}: clicks {_fmt(a['measured_28d'].get('clicks'))}, impressions {_fmt(a['measured_28d'].get('impressions'))} over {a['measured_28d'].get('weeks_counted')} week(s)"
         for a in recent] or ["- none"])
    return out


def site_section(ws) -> list[str]:
    folder = ws.path("data", "audits")
    files = sorted(folder.glob("*-site.json")) if folder.is_dir() else []
    if not files:
        return ["## Site audit", "", "not run (mseo site audit)"]
    audit = _json(files[-1]) or {}
    fails = [c for c in audit.get("checks", []) if c.get("status") == "fail"]
    return ["## Site audit", "", f"{files[-1].name}: {len(fails)} failing check(s)"] + [f"- {c.get('page', '')} {c.get('name')}: {c.get('detail', '')}" for c in fails[:10]]


def report(ws, week: str, *, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    data = week_mod.load(ws, week)
    weeks = ws.path("data", "weeks")
    lines = [f"# {ws.config['business']['name']}: week {week}", "", f"period {data['period']['start']} to {data['period']['end']}, "
             f"assembled {data.get('assembled')}, report written {today.isoformat()}", ""]
    lines += sources_section(data) + [""]
    lines += shadow_section(_json(weeks / f"{week}.shadow.json")) + [""]
    lines += ads_section(_json(weeks / f"{week}.tracking.json"), _json(weeks / f"{week}.ads-audit.json")) + [""]
    lines += batches_section(ws) + [""]
    lines += topics_section(_json(weeks / f"{week}.topics.json")) + [""]
    lines += content_section(ws, today) + [""]
    lines += site_section(ws)
    return "\n".join(lines) + "\n"


def cmd_report(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        text = report(ws, args.week)
    except week_mod.WeekError as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    out = ws.path("data", "weeks", f"{args.week}.report.md")
    write_atomic(out, text)
    ctx.emit({"status": "ok", "path": str(out), "report": text, "messages": text.splitlines()})
    return EXIT_OK


def register(wsub, parents) -> None:
    r = wsub.add_parser("report", parents=[parents.workspace, parents.json],
                        help="Write data/weeks/<W>.report.md: sources, shadow, Ads, batches, topics, content, site audit")
    r.add_argument("--week", required=True, help="ISO week, e.g. 2026-W44")
    r.set_defaults(func=cmd_report, command_id="week report")
