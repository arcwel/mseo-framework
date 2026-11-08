"""content/topics: `mseo content topics`, the query-gap shortlist for next week's article.

    mseo content topics --workspace S --week W [--top N] [--service WORDS]
    mseo content topics --workspace S --week W --pick N --slug X

One method: the latest ok Search Console queries.csv and Open-SEO rankings.csv for the week (data/runs records),
minus the brand's own terms (business.brand_terms), minus topics already covered (title word overlap >= 0.6 with a
content-log article from the last 60 days or any published post), ranked question-shaped queries first, service
searches next, bare business-type searches last. A query that names no place gets a local tie-in from the week's
research file (its first event or road line). Writes data/weeks/<W>.topics.json and prints the numbered shortlist;
`--pick N --slug X` logs the pick as a draft entry (title = the query as a question) and runs `content recipe`.

Exit 0 · 3 no ok record for the week, or the log, research file or a record cannot be read.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys

from mseo.content import dupes, recipes, variation
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.run import record as record_mod

SCHEMA_VERSION = "mseo.topics/1"
QUESTION_LEADS = ("how", "what", "when", "why", "can", "should", "which", "where", "do", "does", "is", "are")
COVERED_JACCARD = 0.6
COVERED_DAYS = 60
BARE_MAX_WORDS = 2
FILES = {"search_console": "queries.csv", "open_seo": "rankings.csv"}
DEFAULT_TOP = 10


class TopicsError(Exception):
    """Maps to exit 3."""


def today() -> dt.date:
    from mseo.doctor import today as doctor_today

    return doctor_today()


# ---------------------------------------------------------------- records

def ok_records(ws) -> list[dict]:
    folder = ws.path("data", "runs")
    out = []
    for path in sorted(folder.glob("*/record.json")) if folder.is_dir() else []:
        try:
            rec = record_mod.load(path)
        except (OSError, ValueError, record_mod.RecordInvalid):
            continue
        if rec.get("status") == record_mod.OK:
            out.append(rec)
    return out


def latest_for_week(ws, recs: list[dict], source: str, week: str):
    """(record, rows) for the newest ok record of `source` whose period is the ISO week and that holds the file."""
    period = record_mod.week_period(week, ws.config["timezone"])
    name = FILES[source]
    mine = sorted((r for r in recs if r["source"] == source and r.get("period")
                   and (r["period"]["start"], r["period"]["end"]) == (period["start"], period["end"])
                   and any(f["path"] == name for f in r["files"])), key=lambda r: r["started"])
    if not mine:
        return None, []
    rec = mine[-1]
    path = ws.path("data", "runs", rec["run_id"], name)
    with path.open(newline="", encoding="utf-8") as fh:
        return rec, list(csv.DictReader(fh))


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- the shortlist

def is_brand(query: str, brand_terms) -> bool:
    return any(re.search(t, query, re.I) for t in brand_terms)


def covered_titles(ws, on: dt.date) -> list[str]:
    log = variation.read_log(ws)
    titles = []
    for a in log.get("articles", []):
        if a.get("status") == "removed" or not a.get("title"):
            continue
        try:
            age = (on - dt.date.fromisoformat(a.get("date", ""))).days
        except ValueError:
            continue
        if -7 <= age <= COVERED_DAYS:
            titles.append(a["title"])
    try:
        posts = json.loads(ws.file("wp_posts").read_text(encoding="utf-8"))
        titles += [p["title"] for p in posts if isinstance(p, dict) and p.get("title")]
    except (OSError, ValueError, KeyError):
        pass
    return titles


def is_covered(query: str, titles) -> str | None:
    q = set(dupes.words(query))
    for t in titles:
        if dupes.jacc(q, set(dupes.words(t))) >= COVERED_JACCARD:
            return t
    return None


def classify(query: str, nouns) -> int:
    """0 question-shaped, 1 a service search, 2 a bare business-type search (one or two words, or nouns and a place)."""
    words = dupes.norm(query).split()
    if words and words[0] in QUESTION_LEADS:
        return 0
    if len(words) <= BARE_MAX_WORDS or all(w in nouns or w in ("near", "me") for w in words):
        return 2
    return 1


def tie_in_line(research_text: str) -> str | None:
    """The first event or road bullet in the research file (the Decision section does not count)."""
    skip = False
    for line in research_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            skip = "decision" in s.lower()
            continue
        if not skip and s.startswith(("-", "*")) and s.lstrip("-* ").strip():
            return s.lstrip("-* ").strip()
    return None


def as_question(query: str) -> str:
    q = query.strip().rstrip("?")
    text = q[:1].upper() + q[1:]
    if dupes.norm(q).split()[:1] and dupes.norm(q).split()[0] in QUESTION_LEADS:
        return text + "?"
    return f"What should you know about {q}?"


def shortlist(ws, week: str, *, on: dt.date, service: str = "") -> dict:
    recs = ok_records(ws)
    sc_rec, sc_rows = latest_for_week(ws, recs, "search_console", week)
    os_rec, os_rows = latest_for_week(ws, recs, "open_seo", week)
    if sc_rec is None and os_rec is None:
        raise TopicsError(f"no ok Search Console queries.csv or Open-SEO rankings.csv record for {week} under data/runs")
    merged: dict[str, dict] = {}
    for r in sc_rows:
        q = (r.get("query") or "").strip()
        if not q:
            continue
        key = dupes.norm(q)
        merged.setdefault(key, {"query": q, "clicks": 0, "impressions": 0, "position": None, "rank": None, "sources": []})
        merged[key]["clicks"] += int(_num(r.get("clicks")))
        merged[key]["impressions"] += int(_num(r.get("impressions")))
        merged[key]["position"] = _num(r.get("position")) or merged[key]["position"]
        merged[key]["sources"].append("search_console")
    for r in os_rows:
        q = (r.get("keyword") or "").strip()
        if not q:
            continue
        key = dupes.norm(q)
        merged.setdefault(key, {"query": q, "clicks": 0, "impressions": 0, "position": None, "rank": None, "sources": []})
        merged[key]["rank"] = int(_num(r.get("position"))) if r.get("position") not in (None, "") else None
        merged[key]["sources"].append("open_seo")
    business = ws.config["business"]
    nouns = set(ws.config["content"].get("business_nouns") or ()) | {dupes.norm(w) for w in ("near", "me")}
    titles = covered_titles(ws, on)
    research_rel = f"{(ws.config.get('local_research') or {}).get('research_dir') or 'research/local'}/{week}.md"
    research_path = ws.path(research_rel)
    research_text = research_path.read_text(encoding="utf-8") if research_path.is_file() else ""
    places = variation.research_places(research_text) | ({variation.city_of(ws)} - {""})
    tie = tie_in_line(research_text)
    dropped, kept = [], []
    for item in merged.values():
        q = item["query"]
        if is_brand(q, business.get("brand_terms") or ()):
            dropped.append({"query": q, "why": "brand term"})
            continue
        hit = is_covered(q, titles)
        if hit:
            dropped.append({"query": q, "why": f"covered by {hit!r}"})
            continue
        named = variation.mentions(q, places)
        kept.append({**item, "class": classify(q, nouns), "title": as_question(q), "place_named": named,
                     "tie_in": None if named else tie})
    kept.sort(key=lambda t: (t["class"], -t["impressions"], -t["clicks"], t["query"]))
    for n, t in enumerate(kept, 1):
        t["n"] = n
    return {"schema_version": SCHEMA_VERSION, "workspace": ws.slug, "week": week, "as_of": on.isoformat(),
            "records": {"search_console": sc_rec["run_id"] if sc_rec else None, "open_seo": os_rec["run_id"] if os_rec else None},
            "research_file": research_rel if research_text else None, "topics": kept, "dropped": dropped}


def write(ws, out: dict):
    path = ws.path("data", "weeks", f"{out['week']}.topics.json")
    write_json_atomic(path, out)
    return path


def pick(ws, out: dict, n: int, slug: str) -> dict:
    topics = out["topics"]
    if not 1 <= n <= len(topics):
        raise TopicsError(f"--pick {n} is not on the shortlist (1-{len(topics)})")
    topic = topics[n - 1]
    rec = recipes.recipe(ws.file("content_log"), "article")
    how = recipes.log_recipe(ws, rec, slug=slug, title=topic["title"], date=today().isoformat(), query=topic["query"],
                             status="draft", tie_in=topic.get("tie_in"))
    return {"topic": topic, "recipe": rec, "logged": how}


def _lines(out: dict, top: int) -> list[str]:
    kinds = {0: "question", 1: "service", 2: "bare"}
    lines = [f"{out['week']}: {len(out['topics'])} topic(s) after dropping {len(out['dropped'])} (brand or covered)"]
    for t in out["topics"][:top]:
        extra = f"; tie-in: {t['tie_in']}" if t.get("tie_in") else ""
        rank = f", rank {t['rank']}" if t.get("rank") is not None else ""
        lines.append(f"{t['n']:>3}. [{kinds[t['class']]}] {t['query']}  ({t['impressions']} impr, {t['clicks']} clicks{rank}){extra}")
    return lines


def cmd_topics(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    if args.pick and not args.slug:
        print("error: --pick needs --slug", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    try:
        out = shortlist(ws, args.week, on=today(), service=args.service or "")
        path = write(ws, out)
        picked = pick(ws, out, args.pick, args.slug) if args.pick else None
    except (TopicsError, recipes.RecipeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    except Exception as exc:  # noqa: BLE001 - unreadable records or log: could not run
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    messages = _lines(out, args.top) + [f"wrote {path}"]
    if picked:
        messages += [f"picked {picked['topic']['n']}: {picked['topic']['title']} -> {args.slug} ({picked['logged']})",
                     *recipes.describe(picked["recipe"])]
    ctx.emit({**out, "path": str(path), "picked": picked, "messages": messages})
    return EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("topics", parents=[parents.workspace, parents.json],
                        help="Query-gap shortlist for the next article from Search Console and Open-SEO")
    p.add_argument("--week", required=True, help="ISO week of the records, e.g. 2026-W45")
    p.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"Rows to print (default {DEFAULT_TOP})")
    p.add_argument("--service", help="Reserved: words that mark a service search")
    p.add_argument("--pick", type=int, help="Log shortlist item N as a draft article and run content recipe")
    p.add_argument("--slug", help="The slug for the picked topic (with --pick)")
    p.set_defaults(func=cmd_topics, command_id="content topics")
