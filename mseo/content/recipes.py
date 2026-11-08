"""content/recipes: the variation axes from templates/posts/recipes.json and the recipe picker.

`recipe(log_path, kind)` picks a value on every axis for the next article or Business Profile post, following the
rules in templates/posts/README.md: the shape (block order) must not be one of the last `window` logged posts; an
axis with `avoid_last` N must not repeat any of the last N logged values; an axis with `max_run` N must not make
N+1 posts in a row share the value. Among the allowed values the least used inside the window wins; ties go to the
smallest value (by name), so the pick is deterministic given the log.

`mseo content recipe --workspace S --kind article|gbp [--slug X | --date D --headline H]` picks, prints and logs.
"""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

import mseo
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.workspace import WorkspaceError

FRAMEWORK_ROOT = Path(mseo.__file__).resolve().parents[1]
RECIPES_PATH = FRAMEWORK_ROOT / "templates" / "posts" / "recipes.json"
TEMPLATES = FRAMEWORK_ROOT / "templates" / "posts"
ARTICLE_AXES = ("title_pattern", "intro", "evidence", "faq_n", "cta", "word_band", "local_placement", "closing")
GBP_AXES = ("opening", "length_band", "closing", "image")
# Articles compared axis by axis (the variation gate's "shared with the previous post" rule).
COMPARED_ARTICLE_AXES = ("shape", *ARTICLE_AXES)
DEFAULT_LANG = "en"
# A piece's slug: the draft file name and the content-log key. Checked before any path is built from it.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")


class RecipeError(Exception):
    """recipes.json or the content log cannot be read. Maps to exit 3."""


@lru_cache(maxsize=1)
def load(path: Path = RECIPES_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecipeError(f"{path} could not be read ({exc})") from exc
    for key in ("window", "max_shared_axes_with_previous", "blocks", "article", "gbp", "lexicon"):
        if key not in data:
            raise RecipeError(f"{path} has no {key!r}")
    if DEFAULT_LANG not in data["lexicon"]:
        raise RecipeError(f"{path} lexicon has no {DEFAULT_LANG!r} entry")
    return data


def article() -> dict:
    return load()["article"]


def lexicon(lang: str | None = None) -> dict:
    """The lead words for `lang` (ISO 639-1); a language recipes.json does not list falls back to English."""
    table = load()["lexicon"]
    return table.get((lang or DEFAULT_LANG).lower()) or table[DEFAULT_LANG]


def check_slug(slug: str) -> str:
    """`slug` when it is SLUG-like, else RecipeError (exit 3) before any path is built from it."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise RecipeError(f"bad slug {slug!r}: must match {SLUG_RE.pattern}")
    return slug


def gbp() -> dict:
    return load()["gbp"]


def shapes() -> dict:
    """{shape name: block order} for articles."""
    return {k: list(v) for k, v in article()["shapes"].items()}


def axis_values(kind: str, axis: str) -> list:
    section = article() if kind == "article" else gbp()
    return list(section["axes"][axis]["values"])


def template_path(kind: str, name: str) -> Path:
    """The template file for an article shape or a Business Profile post type."""
    if kind == "article":
        return TEMPLATES / "articles" / f"{name}.md"
    return TEMPLATES / "business-profile" / f"{name}.txt"


# ---------------------------------------------------------------- the picker

def distance(a, b) -> float:
    """Normalised edit distance between two block sequences (0 identical, 1 totally different)."""
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return d[n][m] / max(n, m, 1)


def read_log(log_path) -> dict:
    try:
        return json.loads(Path(log_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecipeError(f"content log {log_path} could not be read ({exc})") from exc


def _active(entries: list) -> list:
    return [e for e in entries if e.get("status") != "removed"]


def _sort_key(value):
    return (str(type(value).__name__), str(value))


def pick_axis(values: list, history: list, *, avoid_last: int = 0, max_run: int = 0) -> tuple:
    """(value, why) following the README rules. `history` is oldest-first, missing values dropped."""
    history = [h for h in history if h is not None]
    banned = set()
    if avoid_last:
        banned |= {h for h in history[-avoid_last:]}
    if max_run and len(history) >= max_run and len(set(history[-max_run:])) == 1:
        banned.add(history[-1])
    allowed = [v for v in values if v not in banned] or list(values)
    counts = {v: history.count(v) for v in allowed}
    best = sorted(allowed, key=lambda v: (counts[v], _sort_key(v)))[0]
    why = f"not in last {avoid_last}" if avoid_last else (f"no run of {max_run + 1}" if max_run else "least used")
    return best, why


def _pick_shape(recent: list, allowed=None) -> tuple:
    names = [s for s in shapes() if not allowed or s in allowed]
    if not names:
        raise RecipeError(f"no shape matches --recipe {allowed}; known: {', '.join(shapes())}")
    used = [tuple(a.get("blocks") or ()) for a in recent]
    recent_names = {a.get("shape") for a in recent}

    def score(name):
        return min([distance(shapes()[name], list(b)) for b in used] or [1.0])

    fresh = [n for n in names if n not in recent_names and tuple(shapes()[n]) not in used] or names
    ranked = sorted(fresh, key=lambda n: (-score(n), n))
    return ranked[0], score(ranked[0]), ranked


def recipe(log_path, kind: str = "article", *, allowed=None) -> dict:
    """The next piece's recipe. `allowed`: restrict the shape (article) or type (gbp) to these names."""
    data = load()
    log = read_log(log_path)
    window = int(data["window"])
    if kind == "article":
        recent = [a for a in _active(log.get("articles", []))][-window:]
        shape, score, ranked = _pick_shape([a for a in recent if a.get("blocks")], allowed)
        out = {"kind": "article", "shape": shape, "blocks": shapes()[shape],
               "why": f"shape most different from the last {len(recent)} logged articles (distance {score:.2f}); "
                      f"runners-up: " + ", ".join(ranked[1:3])}
        for axis, spec in data["article"]["axes"].items():
            history = [a.get(axis) for a in recent]
            out[axis], _ = pick_axis(list(spec["values"]), history, avoid_last=int(spec.get("avoid_last", 0)),
                                     max_run=int(spec.get("max_run", 0)))
        out["list"] = {"table": "table", "numbered-steps": "numbered", "checklist": "bullets"}.get(out["evidence"], "numbered")
        return out
    if kind == "gbp":
        recent = _active(log.get("gbp", []))[-window:]
        types = [t for t in data["gbp"]["types"] if not allowed or t in allowed]
        if not types:
            raise RecipeError(f"no Business Profile type matches --recipe {allowed}; known: {', '.join(data['gbp']['types'])}")
        gbp_type, _ = pick_axis(types, [g.get("gbp_type") for g in recent], avoid_last=int(data["gbp"].get("type_avoid_last", 0)))
        out = {"kind": "gbp", "gbp_type": gbp_type, "why": f"type not among the last {data['gbp'].get('type_avoid_last', 0)} logged posts"}
        for axis, spec in data["gbp"]["axes"].items():
            history = [g.get(axis) for g in recent]
            out[axis], _ = pick_axis(list(spec["values"]), history, avoid_last=int(spec.get("avoid_last", 0)),
                                     max_run=int(spec.get("max_run", 0)))
        return out
    raise RecipeError(f"unknown kind {kind!r}")


def logged_axes(rec: dict) -> dict:
    """The fields `content log` keeps for a recipe (the old names stay for older readers of the log)."""
    if rec["kind"] == "article":
        return {k: rec[k] for k in ("shape", "blocks", "list", *ARTICLE_AXES)}
    return {k: rec[k] for k in ("gbp_type", *GBP_AXES)}


def describe(rec: dict) -> list[str]:
    if rec["kind"] == "article":
        lines = [f"shape          {rec['shape']}: {' > '.join(rec['blocks'])}"]
        lines += [f"{axis:<15}{rec[axis]}" for axis in ARTICLE_AXES]
    else:
        lines = [f"type           {rec['gbp_type']}"] + [f"{axis:<15}{rec[axis]}" for axis in GBP_AXES]
    return lines + [f"why: {rec['why']}"]


# ---------------------------------------------------------------- CLI

def title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").strip().capitalize()


def log_recipe(ws, rec: dict, *, slug: str = "", date: str | None = None, headline: str | None = None,
               title: str | None = None, theme: str | None = None, **fields) -> str:
    """Write the recipe's axes on the piece's content-log entry (new entries get the required fields filled).
    Extra `fields` (a gbp entry's slug, an article's query) are written as given."""
    from mseo.content import log as log_mod
    from mseo.doctor import today

    if rec["kind"] == "article":
        if not slug:
            raise RecipeError("an article recipe is logged by --slug")
        entry = {"slug": slug, **logged_axes(rec), **fields}
        existing = _entry(ws, "article", slug=slug)
        if not existing:
            entry.update({"title": title or title_from_slug(slug), "date": date or today().isoformat()})
        return log_mod.upsert_entry(ws, "article", entry)
    if not headline:
        raise RecipeError("a Business Profile recipe is logged by --date and --headline")
    entry = {"date": date or today().isoformat(), "headline": headline, **logged_axes(rec), **fields}
    if slug:
        entry["slug"] = slug
    if theme:
        entry["theme"] = theme
    elif not _entry(ws, "gbp", date=entry["date"], headline=headline):
        entry["theme"] = rec["gbp_type"]
    return log_mod.upsert_entry(ws, "gbp", entry)


def _entry(ws, kind: str, *, slug: str = "", date: str = "", headline: str = ""):
    log = read_log(ws.file("content_log"))
    if kind == "article":
        return next((a for a in log.get("articles", []) if a.get("slug") == slug), None)
    return next((g for g in log.get("gbp", []) if g.get("date") == date and g.get("headline", "").lower() == headline.lower()), None)


def cmd_recipe(args, ctx) -> int:
    from mseo.content import log as log_mod

    ws = ctx.open(args.workspace)
    try:
        if args.slug:
            check_slug(args.slug)
        rec = recipe(ws.file("content_log"), args.kind, allowed=[args.recipe] if args.recipe else None)
        how = None
        if args.slug or args.headline:
            how = log_recipe(ws, rec, slug=args.slug, date=args.date, headline=args.headline, title=args.title)
    except (RecipeError, log_mod.LogError, WorkspaceError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    messages = describe(rec) + ([f"{how}: axes written to the content log"] if how else ["not logged (give --slug or --date/--headline)"])
    ctx.emit({**rec, "logged": how, "messages": messages})
    return EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("recipe", parents=[parents.workspace, parents.json],
                        help="Pick the next post's shape and variation axes, most different from the logged posts")
    p.add_argument("--kind", required=True, choices=["article", "gbp"])
    p.add_argument("--slug", default="", help="Article: log the axes on this slug's entry (created when new)")
    p.add_argument("--title", help="Article: title for a new entry (default: from the slug)")
    p.add_argument("--date", help="Date of the piece (default: today)")
    p.add_argument("--headline", help="Business Profile: log the axes on the entry for --date and this headline")
    p.add_argument("--recipe", help="Force this shape (article) or type (gbp); the other axes are still picked")
    p.set_defaults(func=cmd_recipe, command_id="content recipe")
