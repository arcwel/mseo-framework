"""content/dupes: `mseo content dupes KIND FILE`, the duplicate-content check (ported from the earlier house script).

Fails when a candidate repeats a topic or text inside the content log's window, or closely matches ANY older post.
The certification and subject-brand repetition warning now reads business.certifications and
content.subject_brands from customer.json. Paths go through the workspace, never the cwd.
Exit 0 pass, 1 fail, 2 warnings only (--strict fails on warnings), 3 the check could not run (missing or
unreadable data, or the posts list older than the newest published post in the log).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from collections import Counter

from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK

MIN_HEADLINE_CHARS = 20  # shorter logged headlines are too generic to identify a post
CERT_CLAIM = "certification list"
MIN_BRANDS = 4
STOP = set("a an and are as at be but by for from how i in is it of on or our so that the their they this to we what when where which who will with you your can do does not no if than then there these those into out up about after before".split())


class CouldNotRun(Exception):
    pass


def norm(t): return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", t.lower())).strip()
def words(t): return [w for w in norm(t).split() if w not in STOP]


def shingles(t, n=5):
    w = norm(t).split()
    return {" ".join(w[i:i + n]) for i in range(max(len(w) - n + 1, 0))}


def jacc(a, b): return len(a & b) / len(a | b) if a and b else 0.0


def cosine(a, b):
    ca, cb = Counter(words(a)), Counter(words(b))
    dot = sum(ca[k] * cb[k] for k in ca)
    na, nb = math.sqrt(sum(v * v for v in ca.values())), math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


def strip_md(t):
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return re.sub(r"[#*>|`]", " ", t)


def age(d, today): return (today - dt.date.fromisoformat(d)).days


def check_shape(log, name):
    if not isinstance(log, dict) or not isinstance(log.get("window_days"), int) \
            or not isinstance(log.get("articles"), list) or not isinstance(log.get("gbp"), list):
        raise CouldNotRun(f"{name} must hold window_days (int), articles (list) and gbp (list)")


def is_own_headline(entry, first_line):
    """Only an unpublished entry can be this draft: a published post that shares the opening is a repeat."""
    h = norm(entry.get("headline", ""))
    return entry.get("status") != "published" and len(h) >= MIN_HEADLINE_CHARS and first_line.startswith(h)


def own(entry, slug): return bool(slug) and entry.get("slug") == slug


def article(raw: str, log: dict, wp, today, query="", slug=""):
    text, res = strip_md(raw), []
    if not isinstance(wp, list) or not all(isinstance(p, dict) and isinstance(p.get("date"), str) for p in wp):
        raise CouldNotRun("the posts list must be a list of posts, each with a date")
    newest_wp = max((p["date"] for p in wp), default="")
    newest_pub = max((a["date"] for a in log.get("articles", []) if a.get("status") == "published"), default="")
    if newest_pub > newest_wp:
        raise CouldNotRun(f"the posts list is stale: newest post {newest_wp or 'none'}, but the log has a post published "
                          f"{newest_pub}. Refresh it (mseo wp posts-refresh) before checking.")
    wp = [p for p in wp if not own(p, slug)]
    lines = raw.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
    h2 = {norm(l[3:]) for l in lines if l.startswith("## ")}
    for p in wp:
        sh = jacc(shingles(text), shingles(p["text"]))
        cs = cosine(text, p["text"])
        tq = jacc(set(words(query or title)), set(words(p["title"])))
        a = age(p["date"], today)
        inwin = a <= log["window_days"]
        sev = None
        if sh >= .25 or cs >= .85:
            sev = "FAIL"  # near-duplicate text, any age
        elif tq >= .6 and inwin:
            sev = "FAIL"  # same topic inside the window
        elif tq >= .6 or cs >= .7:
            sev = "WARN"  # same topic older, or heavy vocabulary overlap
        elif sh >= .12 and inwin:
            sev = "WARN"
        if sev:
            res.append({"level": sev, "against": p["slug"], "age_days": a, "text_overlap": round(sh, 3),
                        "vocabulary_cosine": round(cs, 3), "topic_overlap": round(tq, 3)})
    seen = {p["slug"] for p in wp}
    for e in log.get("articles", []):  # logged articles not yet in the posts list: topic check only
        if e.get("slug") in seen or own(e, slug) or e.get("status") == "removed":
            continue
        tq = jacc(set(words(query or title)), set(words(e.get("title", ""))))
        a = age(e["date"], today)
        sev = "FAIL" if tq >= .6 and a <= log["window_days"] else ("WARN" if tq >= .6 else None)
        if sev:
            res.append({"level": sev, "against": f'{e["slug"]} (content log, {e.get("status")})', "age_days": a,
                        "topic_overlap": round(tq, 3)})
    return res, {"words": len(text.split()), "h2s": sorted(h2)}


def gbp(text: str, log: dict, today, theme="", slug="", certifications=(), brands=()):
    res = []
    headline = norm(next((l for l in text.splitlines() if l.strip()), ""))
    for g in log["gbp"]:
        a = age(g["date"], today)
        if a > log["window_days"] or g["status"] == "removed" or own(g, slug) or is_own_headline(g, headline):
            continue
        th = jacc(set(words(theme or "")), set(words(g["theme"])))
        shared = [c for c in g["claims"] if c and norm(c) in norm(text)]
        sev = "FAIL" if th >= .5 else ("WARN" if (shared or th >= .25) else None)
        if sev:
            res.append({"level": sev, "against": f'{g["date"]} {g["theme"]}', "age_days": a, "theme_overlap": round(th, 2),
                        "repeated_claims": shared})
    certs = [g for g in log["gbp"] if CERT_CLAIM in g["claims"] and age(g["date"], today) <= log["window_days"]]
    body = norm(text)
    names_cert = any(norm(c) and norm(c) in body for c in certifications)
    brand_re = "|".join(re.escape(norm(b)) for b in brands if norm(b))
    if names_cert or (brand_re and len(re.findall(brand_re, body)) >= MIN_BRANDS):
        res.append({"level": "WARN", "against": f"{len(certs)} posts in the window already list the certifications",
                    "repeated_claims": [CERT_CLAIM]})
    return res, {"chars": len(text)}


def run(ws, kind: str, text: str, today: dt.date, query="", theme="", slug="", strict=False) -> tuple[int, dict]:
    """(exit code, result dict). Raises nothing: a check that cannot run is exit 3 with an error."""
    try:
        log = json.loads(ws.file("content_log").read_text(encoding="utf-8"))
        check_shape(log, ws.config["files"]["content_log"])
        if kind == "article":
            wp = json.loads(ws.file("wp_posts").read_text(encoding="utf-8"))
            res, info = article(text, log, wp, today, query, slug)
        else:
            biz, content = ws.config["business"], ws.config["content"]
            res, info = gbp(text, log, today, theme, slug, biz["certifications"], content["subject_brands"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CouldNotRun) as exc:
        return EXIT_COULD_NOT_RUN, {"status": "error", "error": f"duplicate check could not run: {exc}"}
    fails = [r for r in res if r["level"] == "FAIL"]
    warns = [r for r in res if r["level"] == "WARN"]
    status = "fail" if fails or (strict and warns) else ("warn" if warns else "pass")
    out = {"status": status, "window_days": log["window_days"], "checked_on": today.isoformat(), "info": info,
           "findings": sorted(res, key=lambda r: r["level"])}
    return (EXIT_FAIL if status == "fail" else EXIT_OK), out  # warnings: exit 0, status "warn" (exit 2 means "exists")


def today_from(value: str | None) -> dt.date:
    from mseo.doctor import today

    return dt.date.fromisoformat(value) if value else today()


def cmd_dupes(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    path = ws.contains(args.file)
    code, out = run(ws, args.kind, path.read_text(encoding="utf-8"), today_from(args.date), args.query, args.theme,
                    args.slug, args.strict)
    if code == EXIT_COULD_NOT_RUN:
        print(f"error: {out['error']}", file=sys.stderr)
        return code
    fails = [r for r in out["findings"] if r["level"] == "FAIL"]
    warns = [r for r in out["findings"] if r["level"] == "WARN"]
    msgs = [f"{out['status'].upper()}: {len(fails)} fail, {len(warns)} warn (window {out['window_days']} days)"]
    msgs += [f"  {r['level']} vs {r['against']} " + str({k: v for k, v in r.items() if k not in ('level', 'against')})
             for r in out["findings"]]
    ctx.emit({**out, "messages": msgs})
    return code


def register(csub, parents) -> None:
    p = csub.add_parser("dupes", parents=[parents.workspace, parents.json], help="Duplicate-content check")
    p.add_argument("kind", choices=["article", "gbp"])
    p.add_argument("file")
    p.add_argument("--query", default="")
    p.add_argument("--theme", default="")
    p.add_argument("--date", default=None, help="Check as of this date (default: today, or MSEO_TODAY)")
    p.add_argument("--slug", default="", help="This draft's own slug in the log, so it is not compared with itself")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_dupes, command_id="content dupes")
