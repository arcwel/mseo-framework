"""`mseo ai-visibility ...`: the monthly prompt-set check and citation scoring.

    mseo ai-visibility prompts --workspace S              # the JSON prompt set an operator or agent runs on each engine
    mseo ai-visibility run --workspace S --answers FILE   # score the answers: who was cited, was the name mentioned
    mseo ai-visibility run --workspace S                  # with MSEO_AI_CHECK_CMD set: that command gets the prompt
                                                          # set on stdin and must print the answers JSON on stdout

The answers file is a JSON list of {prompt, engine, answer_text, cited_urls: [...]}. Scored per engine and overall:
cited the site (business.site host), cited the Business Profile (Google Maps / business.google.com / g.co / g.page),
cited a competitor (ai_visibility.competitors hosts), mentioned the business name. Written to
data/ai-visibility/<YYYY-MM>.json and .md with a trend over the earlier months. No network here.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.parse
from pathlib import Path

from mseo import doctor
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import write_atomic, write_json_atomic

MIN_PROMPTS = 10  # below this the set is topped up from the newest articles' headings
GENERATED_MAX = 5
PROFILE_HOSTS = ("business.google.com", "g.co", "g.page", "maps.app.goo.gl", "maps.google.com")
QUESTION_LEADS = ("how", "what", "when", "where", "why", "which", "who", "can", "should", "do", "does", "is", "are")
CMD_ENV = "MSEO_AI_CHECK_CMD"
CMD_TIMEOUT_S = 1800
SHARES = ("cited_site", "cited_profile", "cited_competitor", "mentioned_name")


class AnswersInvalid(Exception):
    """The answers are not the expected list of records. Maps to exit 3."""


# ---------------------------------------------------------------- prompt set

def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")


def generated_prompts(ws, limit: int = GENERATED_MAX) -> list[str]:
    """Questions from the newest articles' headings (the wp_posts titles), newest first."""
    try:
        posts = json.loads(ws.file("wp_posts").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    posts = sorted((p for p in posts if isinstance(p, dict) and p.get("title")), key=lambda p: str(p.get("date", "")), reverse=True)
    out = []
    for p in posts[:limit]:
        text = " ".join(str(p["title"]).split()).rstrip("?.! ")
        out.append(text + ("?" if text.split(" ", 1)[0].lower() in QUESTION_LEADS else ""))
    return out


def prompt_set(ws) -> dict:
    cfg = ws.config.get("ai_visibility") or {}
    prompts = [{"id": f"p{i:02d}", "text": t, "source": "config"} for i, t in enumerate(cfg.get("prompts") or [], 1)]
    if len(prompts) < MIN_PROMPTS:
        start = len(prompts) + 1
        prompts += [{"id": f"p{i:02d}", "text": t, "source": "article"}
                    for i, t in enumerate(generated_prompts(ws), start) if t not in {p["text"] for p in prompts}]
    return {"schema": "mseo.ai-visibility.prompts/1", "workspace": ws.slug, "engines": list(cfg.get("engines") or []),
            "prompts": prompts,
            "answer_format": [{"prompt": "<prompt text>", "engine": "<engine>", "answer_text": "<the answer>",
                               "cited_urls": ["<url>", "..."]}]}


# ---------------------------------------------------------------- scoring

def validate_answers(data) -> list[dict]:
    if not isinstance(data, list):
        raise AnswersInvalid("answers must be a JSON list")
    out = []
    for i, a in enumerate(data, 1):
        if not isinstance(a, dict) or not isinstance(a.get("prompt"), str) or not isinstance(a.get("engine"), str):
            raise AnswersInvalid(f"answer {i} needs string fields prompt and engine")
        urls = a.get("cited_urls") or []
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise AnswersInvalid(f"answer {i}: cited_urls must be a list of strings")
        out.append({"prompt": a["prompt"], "engine": a["engine"], "answer_text": str(a.get("answer_text") or ""),
                    "cited_urls": urls})
    return out


def _is_profile(url: str) -> bool:
    host, path = _host(url), urllib.parse.urlparse(url).path.lower()
    return host in PROFILE_HOSTS or (host.startswith("google.") and (path.startswith("/maps") or path.startswith("/local")))


def score_answer(answer: dict, site_host: str, competitor_hosts: set[str], name: str) -> dict:
    hosts = [_host(u) for u in answer["cited_urls"]]
    return {"prompt": answer["prompt"], "engine": answer["engine"],
            "cited_site": any(h == site_host for h in hosts),
            "cited_profile": any(_is_profile(u) for u in answer["cited_urls"]),
            "cited_competitor": any(h in competitor_hosts for h in hosts),
            "mentioned_name": name.lower() in answer["answer_text"].lower(),
            "cited_hosts": sorted(set(hosts) - {""})}


def _totals(rows: list[dict]) -> dict:
    n = len(rows)
    out = {"answers": n}
    for key in SHARES:
        count = sum(1 for r in rows if r[key])
        out[key] = count
        out[f"{key}_share"] = round(count / n, 3) if n else None
    return out


def score(ws, answers: list[dict]) -> dict:
    biz = ws.config["business"]
    site_host = _host(biz["site"])
    competitors = {_host(c) for c in (ws.config.get("ai_visibility") or {}).get("competitors") or ()}
    rows = [score_answer(a, site_host, competitors, biz["name"]) for a in answers]
    engines = {e: _totals([r for r in rows if r["engine"] == e]) for e in sorted({r["engine"] for r in rows})}
    cited = {}
    for r in rows:
        for h in r["cited_hosts"]:
            cited[h] = cited.get(h, 0) + 1
    return {"overall": _totals(rows), "engines": engines, "rows": rows,
            "cited_hosts": dict(sorted(cited.items(), key=lambda kv: (-kv[1], kv[0])))}


# ---------------------------------------------------------------- files, trend, markdown

def folder(ws) -> Path:
    return ws.path("data", "ai-visibility")


def month(today=None) -> str:
    return (today or doctor.today()).strftime("%Y-%m")


def trend(ws, current_month: str) -> list[dict]:
    out = []
    f = folder(ws)
    for path in sorted(f.glob("????-??.json")) if f.is_dir() else []:
        if path.stem >= current_month:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            overall = data["scores"]["overall"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append({"month": path.stem, "answers": overall.get("answers"),
                    **{f"{k}_share": overall.get(f"{k}_share") for k in SHARES}})
    return out


def markdown(report: dict) -> str:
    o = report["scores"]["overall"]
    pct = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    lines = [f"# AI visibility {report['month']}: {report['business']}", "",
             f"{o['answers']} answer(s) over {len(report['scores']['engines'])} engine(s). Cited the site {pct(o['cited_site_share'])}, "
             f"the Business Profile {pct(o['cited_profile_share'])}, a competitor {pct(o['cited_competitor_share'])}; "
             f"mentioned the name {pct(o['mentioned_name_share'])}.", "", "| engine | answers | site | profile | competitor | name |",
             "|---|---|---|---|---|---|"]
    for e, t in report["scores"]["engines"].items():
        lines.append(f"| {e} | {t['answers']} | {pct(t['cited_site_share'])} | {pct(t['cited_profile_share'])} | "
                     f"{pct(t['cited_competitor_share'])} | {pct(t['mentioned_name_share'])} |")
    if report["trend"]:
        lines += ["", "## Trend", "", "| month | answers | site | profile | competitor | name |", "|---|---|---|---|---|---|"]
        for t in report["trend"] + [{"month": report["month"], **{k: o.get(k) for k in o}}]:
            lines.append(f"| {t['month']} | {t.get('answers')} | {pct(t.get('cited_site_share'))} | {pct(t.get('cited_profile_share'))} | "
                         f"{pct(t.get('cited_competitor_share'))} | {pct(t.get('mentioned_name_share'))} |")
    top = list(report["scores"]["cited_hosts"].items())[:10]
    if top:
        lines += ["", "## Most cited hosts", ""] + [f"- {h}: {n}" for h, n in top]
    return "\n".join(lines) + "\n"


def run(ws, answers: list[dict], *, today=None) -> dict:
    m = month(today)
    report = {"schema": "mseo.ai-visibility/1", "workspace": ws.slug, "business": ws.config["business"]["name"], "month": m,
              "prompts": len(prompt_set(ws)["prompts"]), "scores": score(ws, answers), "trend": trend(ws, m)}
    write_json_atomic(folder(ws) / f"{m}.json", report)
    write_atomic(folder(ws) / f"{m}.md", markdown(report))
    report["path"] = str(folder(ws) / f"{m}.json")
    return report


# ---------------------------------------------------------------- the external check command

def subprocess_runner(argv: list[str], stdin_text: str) -> str:
    proc = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
    if proc.returncode != 0:
        raise AnswersInvalid(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def answers_from_command(ws, command: str, *, runner=subprocess_runner) -> list[dict]:
    argv = shlex.split(command)
    if not argv:
        raise AnswersInvalid(f"{CMD_ENV} is empty")
    try:
        out = runner(argv, json.dumps(prompt_set(ws)))
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnswersInvalid(f"{CMD_ENV} could not run: {exc}") from None
    try:
        return validate_answers(json.loads(out))
    except ValueError as exc:
        raise AnswersInvalid(f"{CMD_ENV} did not print JSON: {exc}") from None


# ---------------------------------------------------------------- CLI

def cmd_prompts(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    ps = prompt_set(ws)
    print(json.dumps(ps, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_run(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    command = os.environ.get(CMD_ENV)
    try:
        if args.answers:
            answers = validate_answers(json.loads(ws.contains(args.answers).read_text(encoding="utf-8")))
        elif command:
            answers = answers_from_command(ws, command)
        else:
            raise AnswersInvalid(f"give --answers FILE, or set {CMD_ENV} to a command that prints the answers JSON")
    except (OSError, ValueError, AnswersInvalid) as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    report = run(ws, answers)
    ctx.emit({**report, "messages": markdown(report).splitlines() + [f"written: {report['path']}"]})
    return EXIT_OK


def register(sub, parents) -> None:
    p = sub.add_parser("ai-visibility", help="Monthly check of what AI assistants cite for the customer's questions")
    asub = p.add_subparsers(dest="aivis_command", required=True, parser_class=type(p))
    pr = asub.add_parser("prompts", parents=[parents.workspace, parents.json], help="Print the prompt set as JSON")
    pr.set_defaults(func=cmd_prompts, command_id="ai-visibility prompts")
    r = asub.add_parser("run", parents=[parents.workspace, parents.json],
                        help="Score an answers file (or the output of MSEO_AI_CHECK_CMD) and write the month's report")
    r.add_argument("--answers", metavar="FILE", help="JSON list of {prompt, engine, answer_text, cited_urls}, inside the workspace")
    r.set_defaults(func=cmd_run, command_id="ai-visibility run")
