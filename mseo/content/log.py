"""content/log: `mseo content log KIND`, record a post in the content log (ported from the earlier house script).

    mseo content log article --workspace S --slug SLUG --title T --date YYYY-MM-DD --shape NAME [--status ...]
    mseo content log gbp --workspace S --date YYYY-MM-DD --theme T --headline H [--claim C ...] [--status ...]

Re-logging a piece updates its entry (articles by slug, GBP posts by date + headline) and changes only the fields
given. Articles keep the recipe axes (shape, blocks, title_pattern, intro, evidence, faq_n, cta, word_band,
local_placement, closing; `list` stays for older readers) and, with --draft PATH, what the variation gate compares
later: the answer's opening words, the H2s and the word count. Business Profile entries keep gbp_type, opening,
length_band, closing and image. The log is the workspace's files.content_log, written temp-then-rename so a crash never leaves half a log.
`upsert_entry` is the in-process call `publish` uses. Exit 0 logged or updated, 3 could not run.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sys
import time

from mseo.content.shapes import INTROS, SHAPES
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import write_json_atomic

NEW_ARTICLE = {"status": "draft", "intro": INTROS[0], "list": "numbered", "faq_n": 4, "cta": "mid-and-end", "title_pattern": ""}
NEW_GBP = {"status": "scheduled", "claims": []}
ARTICLE_AXIS_FLAGS = ("intro", "list", "faq_n", "cta", "title_pattern", "evidence", "word_band", "local_placement", "closing")
GBP_AXIS_FLAGS = ("gbp_type", "opening", "length_band", "closing", "image")
ARTICLE_REQUIRED = ("title", "date", "shape")
GBP_REQUIRED = ("theme",)


class LogError(Exception):
    """The log cannot be read or the entry is incomplete. Maps to exit 3."""


LOCK_WAIT_S = 30.0


def lock_path(ws):
    """The lock file next to the content log, inside the workspace."""
    log = ws.file("content_log")
    return log.with_name("." + log.name + ".lock")


@contextlib.contextmanager
def locked(ws, wait_s: float = LOCK_WAIT_S):
    """Hold the content log's lock for a read-modify-write; two publishes or logs at once no longer lose an update."""
    path = lock_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    with open(path, "a+") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LogError(f"content log is locked by another run ({path}); try again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def given(**fields) -> dict:
    return {k: v for k, v in fields.items() if v is not None}


def upsert(items: list, entry: dict, key, required, defaults) -> str:
    """Re-logging a piece (draft -> published) updates its entry instead of adding a second one."""
    for i, old in enumerate(items):
        if key(old) == key(entry):
            items[i] = {**old, **entry}
            return "updated"
    missing = [f for f in required if f not in entry]
    if missing:
        raise LogError("a new entry needs " + " ".join("--" + m.replace("_", "-") for m in missing))
    items.append({**defaults, **entry})
    return "logged"


def article_key(e: dict):
    return e.get("slug")


def gbp_key(e: dict):
    return (e.get("date"), e.get("headline", "").lower())


def upsert_entry(ws, kind: str, entry: dict) -> str:
    """Upsert one entry into the workspace's content log. `entry` holds only the fields to set."""
    with locked(ws):
        return _upsert_entry(ws, kind, entry)


def _upsert_entry(ws, kind: str, entry: dict) -> str:
    path = ws.file("content_log")
    try:
        log = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LogError(f"content log {path} could not be read ({exc})") from exc
    if kind == "article":
        entry = {"type": "article", **entry}
        if entry.get("shape"):
            entry["blocks"] = SHAPES[entry["shape"]]
        how = upsert(log.setdefault("articles", []), entry, article_key, ARTICLE_REQUIRED, NEW_ARTICLE)
    else:
        entry = {**entry, "headline": entry["headline"].lower()}
        how = upsert(log.setdefault("gbp", []), entry, gbp_key, GBP_REQUIRED, NEW_GBP)
    write_json_atomic(path, log, indent=1)
    return how


def draft_fields(ws, kind: str, path) -> dict:
    """opening, h2s and words read from the draft (the variation gate compares later posts against them)."""
    from mseo.content import variation

    try:
        text = ws.contains(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LogError(f"draft {path} could not be read ({exc})") from exc
    return variation.draft_fields(text, kind, ws)


def entry_from_args(a, ws=None) -> dict:
    extra = given(check_sha=a.check_sha, url=a.url, wp_id=a.wp_id, wp_link=a.wp_link, approved=a.approved)
    if a.kind == "article":
        axes = given(**{k: getattr(a, k) for k in ARTICLE_AXIS_FLAGS})
        entry = {"slug": a.slug, **given(title=a.title, date=a.date, status=a.status, shape=a.shape, query=a.query), **axes, **extra}
    else:
        axes = given(**{k: getattr(a, k) for k in GBP_AXIS_FLAGS})
        entry = {"date": a.date, "headline": a.headline, **given(status=a.status, theme=a.theme, claims=a.claim), **axes, **extra}
    if getattr(a, "draft", None) and ws is not None:
        entry.update(draft_fields(ws, a.kind, a.draft))
    return entry


def cmd_log(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        how = upsert_entry(ws, args.kind, entry_from_args(args, ws))
    except LogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    ctx.emit({"status": how, "kind": args.kind, "messages": [f"{how} {args.kind}"]})
    return EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("log", help="Record a post in the content log")
    ksub = p.add_subparsers(dest="kind", required=True, parser_class=type(p))
    a = ksub.add_parser("article", parents=[parents.workspace, parents.json])
    a.add_argument("--slug", required=True)
    a.add_argument("--title")
    a.add_argument("--date")
    a.add_argument("--shape", choices=SHAPES)
    a.add_argument("--status")
    a.add_argument("--intro")
    a.add_argument("--list")
    a.add_argument("--faq-n", type=int)
    a.add_argument("--cta")
    a.add_argument("--title-pattern")
    a.add_argument("--evidence")
    a.add_argument("--word-band")
    a.add_argument("--local-placement")
    a.add_argument("--closing")
    a.add_argument("--query", help="The search query the article targets (measured by mseo loop measure)")
    g = ksub.add_parser("gbp", parents=[parents.workspace, parents.json])
    g.add_argument("--date", required=True)
    g.add_argument("--theme")
    g.add_argument("--headline", required=True)
    g.add_argument("--claim", action="append")
    g.add_argument("--status")
    g.add_argument("--gbp-type")
    g.add_argument("--opening")
    g.add_argument("--length-band")
    g.add_argument("--closing")
    g.add_argument("--image")
    for sp, cid in ((a, "content log article"), (g, "content log gbp")):
        sp.add_argument("--draft", help="Read the opening words, H2s and word count from this draft into the entry")
        sp.add_argument("--check-sha", help="SHA-256 of the checked draft (from its check.json)")
        sp.add_argument("--url", help="Live URL")
        sp.add_argument("--wp-id", type=int, help="WordPress post id")
        sp.add_argument("--wp-link", help="WordPress link")
        sp.add_argument("--approved", help='Who approved publishing and when, e.g. "NAME 2026-01-15"')
        sp.set_defaults(func=cmd_log, command_id=cid)
