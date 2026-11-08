"""content/new: `mseo content new`, start a draft from the recipe's template with every {{token}} filled.

    mseo content new --workspace S --kind article|gbp --slug X --service "..." [--area "..."] [--recipe NAME]
                     [--used TEXT] [--service-url U] [--contact-url U] [--related-url U] [--title T] [--week W]

Picks the recipe (content/recipes.py), copies the template for its shape (article) or type (gbp) from
templates/posts/, fills the tokens from customer.json, facts.json, today and this week's research file, and writes
drafts/<slug>.md (or .txt) plus the .research.json sidecar skeleton. The ###WRITE### markers stay for the writer.
Only the tokens the chosen template contains are built, so a Business Profile post needs no author block and no
related post. `--area` defaults to the first location's city. The recipe's axes are written on the piece's
content-log entry (the sections and variation gates read them).

Exit 0 written · 2 the draft exists · 3 a bad slug, an unknown token or a missing fact (named in the message).
"""

from __future__ import annotations

import json
import re
import sys

from mseo.content import places, recipes
from mseo.fsutil import write_atomic, write_json_atomic
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_OK
from mseo.workspace import WorkspaceError

TOKEN_RE = re.compile(r"\{\{\s*([\w.\[\]]+)\s*\}\}")
MARKER_RE = re.compile(r"^\s*<!--\s*block:\s*([\w-]+)\s*-->\s*$", re.M)
FAQ_SLOT = "### ###WRITE###\n###WRITE###\n"
DEFAULT_RESEARCH_DIR = "research/local"


class NewError(Exception):
    """A token has no value or a fact is missing. Maps to exit 3."""


def today():
    from mseo.doctor import today as doctor_today

    return doctor_today()


def this_week() -> str:
    from mseo.doctor import period

    return period(today())


# ---------------------------------------------------------------- the inputs

def author_tokens(ws) -> dict:
    try:
        facts = json.loads(ws.file("facts").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NewError(f"facts.json could not be read ({exc})") from exc
    author = facts.get("author") if isinstance(facts, dict) else None
    if not isinstance(author, dict):
        raise NewError("missing fact author (facts.json needs an author block with name, role and url)")
    out = {}
    for key in ("name", "role", "url"):
        if not author.get(key):
            raise NewError(f"missing fact author.{key} in facts.json")
        out[f"author.{key}"] = str(author[key])
    return out


def research_file(ws, week: str) -> str:
    folder = (ws.config.get("local_research") or {}).get("research_dir") or DEFAULT_RESEARCH_DIR
    rel = f"{folder.strip('/')}/{week}.md"
    if not ws.path(rel).is_file():
        raise NewError(f"missing research.file {rel}: run this week's local research first")
    return rel


def decision_used(text: str, kind: str) -> str:
    """The research file's Decision line, the part for this kind ("Article: X. Business Profile post: Y.")."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower().startswith("## decision")), None)
    if start is None:
        return ""
    body = " ".join(l.strip().lstrip("-* ").strip() for l in lines[start + 1:] if l.strip() and not l.startswith("#"))
    parts = re.split(r"(?i)\b(article|business profile post)\s*:\s*", body)
    picked = {}
    for i in range(1, len(parts) - 1, 2):
        picked[parts[i].lower()] = parts[i + 1].strip().rstrip(".").strip()
    want = "article" if kind == "article" else "business profile post"
    return picked.get(want) or (body if not picked else "")


def newest_post_link(ws) -> str | None:
    try:
        posts = json.loads(ws.file("wp_posts").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    posts = [p for p in posts if isinstance(p, dict) and p.get("slug")] if isinstance(posts, list) else []
    if not posts:
        return None
    newest = max(posts, key=lambda p: str(p.get("date", "")))
    return newest.get("link") or f"{ws.config['business']['site'].rstrip('/')}/{newest['slug'].strip('/')}/"


def template_tokens(template: str) -> set[str]:
    return {m.group(1) for m in TOKEN_RE.finditer(template)}


def link_paths(config: dict) -> dict:
    """content.link_paths of a customer config with the schema defaults (the loader fills them; older configs get
    them here)."""
    paths = dict((config.get("content") or {}).get("link_paths") or {})
    paths.setdefault("service", ["/services/"])
    paths.setdefault("contact", ["/contact/", "/estimate/", "/book/", "/appointment/"])
    paths.setdefault("related_default", None)
    return paths


def tokens_for(ws, args, rec: dict, week: str, needed=None) -> dict:
    """The token values the template needs (`needed`: its token names; None builds every token). A fact is read only
    when a token asks for it, so a template without {{author.*}} needs no author block."""
    needed = set(needed) if needed is not None else None

    def wants(prefix: str) -> bool:
        return needed is None or any(n == prefix or n.startswith(prefix + ".") for n in needed)

    business = ws.config["business"]
    locations = ws.config.get("locations") or ()
    if wants("locations[0]") and (not locations or not locations[0].get("footer")):
        raise NewError("missing fact locations[0].footer in customer.json")
    rel = research_file(ws, week)
    used = (args.used or "").strip() or decision_used(ws.path(rel).read_text(encoding="utf-8"), args.kind)
    if not used:
        raise NewError(f"missing research.used: {rel} has no Decision line for this kind; pass --used")
    area = (args.area or "").strip() or (places.location_city(locations[0]) if locations else "")
    if wants("area") and not area:
        raise NewError("missing area: no --area and the first location has no city; pass --area or set locations[0].city")
    paths = link_paths(ws.config)
    tokens = {"business.name": business["name"], "business.site": business["site"].rstrip("/"),
              "service": args.service, "area": area, "today": today().isoformat(), "research.file": rel,
              "research.used": used, "links.service": args.service_url or paths["service"][0],
              "links.contact": args.contact_url or paths["contact"][0]}
    if locations and locations[0].get("footer"):
        tokens["locations[0].footer"] = locations[0]["footer"]
    if wants("author"):
        tokens.update(author_tokens(ws))
    if wants("links.related"):
        related = args.related_url or newest_post_link(ws) or paths.get("related_default")
        if not related:
            raise NewError("missing links.related: data/wp_posts.json lists no published post; pass --related-url "
                           "or set content.link_paths.related_default")
        tokens["links.related"] = related
    for axis in (*recipes.ARTICLE_AXES, *recipes.GBP_AXES):
        if axis in rec:
            tokens[axis] = str(rec[axis])
    return tokens


# ---------------------------------------------------------------- the text

def fill(template: str, tokens: dict) -> str:
    unknown = sorted({m.group(1) for m in TOKEN_RE.finditer(template) if m.group(1) not in tokens})
    if unknown:
        raise NewError("unknown token {{" + unknown[0] + "}}" + (f" (and {len(unknown) - 1} more)" if len(unknown) > 1 else ""))
    return TOKEN_RE.sub(lambda m: tokens[m.group(1)], template)


def expand_faq(text: str, faq_n: int) -> str:
    """The FAQ block gets exactly `faq_n` question slots."""
    m = re.search(r"(<!--\s*block:\s*faq\s*-->.*?)(?=^\s*<!--\s*block:|\Z)", text, re.S | re.M)
    if not m:
        return text
    block = m.group(1)
    slots = re.findall(re.escape(FAQ_SLOT) + r"\n?", block)
    if not slots:
        return text
    first = block.index(slots[0])
    last_end = block.rindex(slots[-1]) + len(slots[-1])
    rebuilt = block[:first] + (FAQ_SLOT + "\n") * faq_n + block[last_end:]
    return text[:m.start()] + rebuilt + text[m.end():]


def insert_secondary_cta(text: str, cta: str) -> str:
    """A second, paragraph-only CTA block after the answer or mid-article; the final CTA block stays."""
    if cta not in ("after-answer-and-end", "mid-and-end"):
        return text
    markers = list(MARKER_RE.finditer(text))
    names = [m.group(1) for m in markers]
    if cta == "after-answer-and-end":
        at = markers[1].start() if len(markers) > 1 and names[0] == "answer" else None
    else:
        middle = names.index("local") if "local" in names else len(names) // 2
        at = markers[middle].start() if middle < len(markers) else None
    if at is None:
        return text
    extra = ("<!-- block: cta -->\n<!-- Recipe cta: " + cta + ". One sentence with one internal link; the full CTA stays at the end. -->\n"
             "###WRITE### [###WRITE###]({{links.service}})\n\n")
    return text[:at] + extra + text[at:]


def build(template: str, rec: dict, tokens: dict) -> str:
    if rec["kind"] == "article":
        template = insert_secondary_cta(expand_faq(template, int(rec["faq_n"])), rec["cta"])
    return fill(template, tokens)


def sidecar_skeleton(tokens: dict) -> dict:
    return {"file": tokens["research.file"], "used": tokens["research.used"], "event_dates": []}


# ---------------------------------------------------------------- the command

def cmd_new(args, ctx) -> int:
    from mseo.content import log as log_mod

    ws = ctx.open(args.workspace)
    try:
        recipes.check_slug(args.slug)  # before any path is built from it
    except recipes.RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    ext = ".md" if args.kind == "article" else ".txt"
    target = ws.path("drafts", args.slug + ext)
    if target.exists():
        print(f"error: {target} exists; pick another --slug or remove it", file=sys.stderr)
        return EXIT_EXISTS
    try:
        rec = recipes.recipe(ws.file("content_log"), args.kind, allowed=[args.recipe] if args.recipe else None)
        week = args.week or this_week()
        name = rec["shape"] if args.kind == "article" else rec["gbp_type"]
        template_path = recipes.template_path(args.kind, name)
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NewError(f"template {template_path} could not be read ({exc})") from exc
        if args.kind == "article":
            template = insert_secondary_cta(expand_faq(template, int(rec["faq_n"])), rec["cta"])
        tokens = tokens_for(ws, args, rec, week, template_tokens(template))
        text = fill(template, tokens)
    except (recipes.RecipeError, NewError, WorkspaceError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(target, text)
    side = target.with_name(target.name + ".research.json")
    write_json_atomic(side, sidecar_skeleton(tokens), indent=1)
    try:
        if args.kind == "article":
            how = recipes.log_recipe(ws, rec, slug=args.slug, title=args.title or f"{args.service} in {tokens['area']}")
        else:
            how = recipes.log_recipe(ws, rec, headline=args.slug, theme=args.service, slug=args.slug)
    except (recipes.RecipeError, log_mod.LogError, WorkspaceError, OSError, ValueError, KeyError) as exc:
        # the draft is on disk; the log problem is the failure to report
        print(f"error: draft written to {target}, but the content log was not updated ({exc})", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    messages = [f"wrote {target}", f"sidecar {side.name}: {tokens['research.used']}", *recipes.describe(rec),
                f"{how}: recipe logged for {args.slug}", "fill every ###WRITE### marker, then: mseo content check"]
    ctx.emit({"draft": str(target), "sidecar": str(side), "recipe": rec, "tokens": tokens, "logged": how, "messages": messages})
    return EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("new", parents=[parents.workspace, parents.json],
                        help="Start a draft from the recipe's template with the tokens filled")
    p.add_argument("--kind", required=True, choices=["article", "gbp"])
    p.add_argument("--slug", required=True, help="drafts/<slug>.md (article) or .txt (gbp); also the content-log key")
    p.add_argument("--service", required=True, help="The service the piece is about ({{service}})")
    p.add_argument("--area", help="The town or area the piece speaks to ({{area}}; default: the first location's city)")
    p.add_argument("--recipe", help="Force this shape (article) or type (gbp)")
    p.add_argument("--title", help="Article: the content-log title (default: service in area)")
    p.add_argument("--used", help="The research item used ({{research.used}}; default: the research file's Decision line)")
    p.add_argument("--week", help="ISO week of the research file (default: this week)")
    p.add_argument("--service-url", help="{{links.service}} (default: content.link_paths.service[0])")
    p.add_argument("--contact-url", help="{{links.contact}} (default: content.link_paths.contact[0])")
    p.add_argument("--related-url", help="{{links.related}} (default: the newest post in data/wp_posts.json, "
                                         "else content.link_paths.related_default)")
    p.set_defaults(func=cmd_new, command_id="content new")
