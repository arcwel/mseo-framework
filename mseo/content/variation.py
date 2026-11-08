"""content/variation: recompute a draft's variation axes from its text and judge them against the content log.

The recipe (content/recipes.py) is what the writer was asked to do; this module reads what was written. The axes
come back from heuristics on the Markdown (block markers, headings, lists, bold answer, links, places from the
week's research file) and the `variation` gate in content/check.py fails when the README's rules are broken:

- shape equals one of the last `window` logged articles
- more than `max_shared_axes_with_previous` axes equal the previous post's logged axes
- the first `opening_unique_words` words of the answer equal a logged post's `opening`
- a `##` question equals a `##` logged (`h2s`) within 60 days
- more than `h2_lead_max_share` of the H2s start with the same word
- fewer than `proof_items_min` of the proof items are present
- the word count is outside the band the recipe logged for this post

Business Profile posts: type not among the last 3, opening pattern not among the last 2, no three posts in a row in
one length band. Nothing here knows an industry: places come from the research file, numbers from facts.json.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from mseo.content import md as md_mod
from mseo.content import places, recipes
from mseo.content.links import URL_RE

MARKER_RE = re.compile(r"^\s*<!--\s*block:\s*([\w-]+)\s*-->\s*$")
# The body cannot cross another "<!--": a draft made of repeated openers is read in one linear pass (audit L3).
COMMENT_RE = re.compile(r"<!--(?!\s*block:)((?:(?!<!--).)*?)-->", re.S)
H2_REPEAT_DAYS = 60
STRUCTURAL_BLOCKS = ("faq", "author", "cta", "sources")
NUMBERED_RE = re.compile(r"^\s*\d+\.\s+\S")
BULLET_RE = re.compile(r"^\s*[-*]\s+\S")
CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+\S")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
LINK_RE = re.compile(r"\[([^\]\[]{1,500})\]\(([^)\s]{1,2000})\)")
IMAGE_WITH_ALT_RE = re.compile(r"!\[[^\]]*[^\]\s][^\]]*\]\([^)]+\)")
ILLUSTRATION_RE = re.compile(r"^\s*\*?\s*illustration\s*:", re.I)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NUMBER_RE = re.compile(r"\d+")
PROPER_RE = re.compile(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*")
# Lead words live in templates/posts/recipes.json `lexicon.<lang>` (recipes.lexicon); nothing here is English-only.


def language_of(text: str, faq_headings: dict | None = None) -> str:
    """The draft's language: its <!-- lang: xx --> comment, else its FAQ heading, else recipes.DEFAULT_LANG."""
    return md_mod.faq_language(text, faq_headings) or recipes.DEFAULT_LANG


def first_word(text: str) -> str:
    """The first word of visible text, lower-cased, leading punctuation such as "¿" skipped."""
    words = re.findall(r"[\w'’]+", plain(text).lower())
    return words[0] if words else ""


# ---------------------------------------------------------------- parsing

@dataclass
class Section:
    name: str | None          # block name from the marker, or None for an unmarked H2
    h2: str | None            # heading text, None for the answer and for a secondary (paragraph-only) block
    lines: list[str] = field(default_factory=list)
    line: int = 0             # 1-based line of the marker or heading

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def secondary(self) -> bool:
        return self.h2 is None and self.name != "answer"


@dataclass
class Parsed:
    title: str
    sections: list[Section]
    footer_lines: int = 0

    def named(self, name: str) -> list[Section]:
        return [s for s in self.sections if s.name == name]

    @property
    def answer(self) -> str:
        return next((s.text for s in self.sections if s.name == "answer"), "")

    @property
    def blocks(self) -> list[str]:
        """The block order the sections gate sees: `answer`, then the marker before each H2."""
        names = [s.name or "?" for s in self.sections if s.h2 is not None]
        return ["answer"] + [n for n in names if n != "answer"]

    @property
    def body_text(self) -> str:
        return "\n".join(s.text for s in self.sections)


def strip_comments(text: str) -> str:
    """Every HTML comment but the block markers goes; newlines are kept so line numbers hold."""
    return COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def parse(text: str, footers=()) -> Parsed:
    footers = {" ".join(f.split()).lower() for f in footers if f}
    title, sections, cur, dropped = "", [], None, 0
    for n, raw in enumerate(strip_comments(text).splitlines(), 1):
        line = raw.rstrip()
        if " ".join(line.split()).lower().strip("* ") in footers:
            dropped += 1
            continue
        m = MARKER_RE.match(line)
        if m:
            cur = Section(m.group(1), None, [], n)
            sections.append(cur)
            continue
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if cur is not None and cur.h2 is None and cur.name not in (None, "answer") and not cur.text:
                cur.h2, cur.line = line[3:].strip(), n
            else:
                cur = Section(None, line[3:].strip(), [], n)
                sections.append(cur)
            continue
        if cur is None:
            if not line.strip():
                continue
            cur = Section("answer", None, [], n)
            sections.append(cur)
        cur.lines.append(line)
    return Parsed(title, sections, dropped)


def plain(text: str) -> str:
    return md_mod.plain(text)


def words_of(text: str) -> list[str]:
    text = LINK_RE.sub(r"\1", text)
    return re.findall(r"[\w'’-]+", re.sub(r"[*`|#>]", " ", text))


def word_count(parsed: Parsed) -> int:
    return len(words_of(parsed.body_text))


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w' ]+|_", " ", plain(text).lower())).strip()


# ---------------------------------------------------------------- places, numbers, sources

def city_of(ws) -> str:
    """The first location's city: its `city` when set, else read from its address (content/places)."""
    locations = ws.config.get("locations") or ()
    return places.location_city(locations[0]) if locations else ""


def research_places(research_text: str) -> set[str]:
    """Proper-noun phrases from the research file's bullet lines (events, roads, places) and their two-word windows,
    so "Spring Fair" is found when the file lists "Townsville Spring Fair". The Decision section is skipped."""
    out: set[str] = set()
    skip = False
    for line in research_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            skip = "decision" in s.lower()
            continue
        if skip or not s.startswith(("-", "*")):
            continue
        s = re.split(r"\bSource\b\s*:", s, maxsplit=1)[0]
        s = DATE_RE.sub(" ", URL_RE.sub(" ", s))
        for run in PROPER_RE.findall(s):
            toks = run.split()
            if len(toks) >= 2:
                out.add(run)
                out.update(" ".join(toks[i:i + 2]) for i in range(len(toks) - 1))
            elif len(run) >= 4:
                out.add(run)
    return out


def mentions(text: str, places) -> bool:
    low = plain(text).lower()
    return any(p and p.lower() in low for p in places)


def fact_numbers(facts: dict) -> set[str]:
    """Every number written in facts.json, dates aside."""
    found: set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            found.update(NUMBER_RE.findall(DATE_RE.sub(" ", str(v))))
    walk(facts)
    return found


def text_numbers(text: str) -> set[str]:
    return set(NUMBER_RE.findall(DATE_RE.sub(" ", URL_RE.sub(" ", LINK_RE.sub(r"\1", text)))))


def source_urls(parsed: Parsed) -> list[str]:
    """The URLs in the sources block, and only there: a URL named elsewhere (the research sidecar, the body) is not a
    cited source (README item 10)."""
    urls = [u for s in parsed.named("sources") for u in URL_RE.findall(s.text)]
    urls += [m.group(2) for s in parsed.named("sources") for m in LINK_RE.finditer(s.text) if m.group(2).startswith("http")]
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;:!?*`)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def used_in_sources(parsed: Parsed, used: str) -> bool:
    """True when the research sidecar's `used` item appears in the sources block: by its URL when it names one, else by
    its words (at least half of its words of four letters or more are in the block)."""
    block = "\n".join(s.text for s in parsed.named("sources"))
    if not used.strip():
        return True
    urls = [u.rstrip(".,;:!?*`)") for u in URL_RE.findall(used)]
    if urls:
        return any(u in source_urls(parsed) for u in urls)
    words = {w for w in norm(URL_RE.sub(" ", used)).split() if len(w) >= 4}
    if not words:
        return norm(used) in norm(block)
    present = {w for w in words if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", norm(block))}
    return len(present) * 2 >= len(words)


def image_evidence_in(text: str) -> str | None:
    """"image" for image Markdown with alt text, "labelled illustration" for a caption line that starts with
    "Illustration:" (or "*Illustration"), else None."""
    if IMAGE_WITH_ALT_RE.search(text):
        return "image"
    if any(ILLUSTRATION_RE.match(l) for l in text.splitlines()):
        return "labelled illustration"
    return None


# ---------------------------------------------------------------- the axes

def title_pattern(title: str, places, lang: str = recipes.DEFAULT_LANG) -> str:
    words = recipes.lexicon(lang)
    t = plain(title).strip()
    low = t.lower()
    first = first_word(t)
    if first in words["question_leads"]:
        return "how-what-when-question"
    if re.match(r"\d", low):
        return "number-plus-noun"
    if any(marker in f" {low} " for marker in words["versus_markers"]):
        return "x-vs-y"
    if first in words["should_leads"]:
        return "should-you"
    if mentions(t, places):
        return "local-led"
    return "plain-statement"


def intro_style(answer: str) -> str | None:
    paras = [p for p in answer.splitlines() if p.strip()]
    if not paras:
        return None
    bolds = BOLD_RE.findall(answer)
    if not bolds:
        return None
    if not paras[0].lstrip().startswith("**"):
        return "scenario-then-bold-answer"
    bold = bolds[0]
    rest = BOLD_RE.sub("", answer, count=1).strip()
    if rest:
        return "bold-answer-then-why"
    sentences = len([s for s in re.split(r"(?<=[.!?])\s+", bold.strip()) if s.strip()])
    if sentences <= 1 and re.search(r"\d", bold):
        return "bold-one-line-plus-number"
    return "bold-two-sentences"


def evidence_element(parsed: Parsed) -> str | None:
    """The first evidence element in document order: table, numbered steps, checklist (- [ ]) or a scenario block."""
    found = []
    for s in parsed.sections:
        if s.name in ("author", "cta", "sources"):
            continue
        if s.name == "scenario" and s.h2 is not None:
            found.append((s.line, "worked-scenario"))
        for i, line in enumerate(s.lines):
            if TABLE_RE.match(line):
                found.append((s.line + i, "table"))
            elif CHECKBOX_RE.match(line):
                found.append((s.line + i, "checklist"))
            elif NUMBERED_RE.match(line):
                found.append((s.line + i, "numbered-steps"))
    return min(found)[1] if found else None


def cta_position(parsed: Parsed) -> str | None:
    ctas = parsed.named("cta")
    if not ctas:
        return None
    first_h2 = next((s.line for s in parsed.sections if s.h2 is not None), None)
    secondary = [s for s in ctas if s.secondary and s.text]
    if not secondary:
        return "end"
    return "after-answer-and-end" if first_h2 is None or secondary[0].line < first_h2 else "mid-and-end"


def band_of(count: int, bands) -> str | None:
    for band in bands:
        lo, hi = (int(x) for x in str(band).split("-"))
        if lo <= count <= hi:
            return band
    return None


def local_placement(parsed: Parsed, places, sources) -> str | None:
    """Where the first research-file place (the bare city aside) or research source is cited."""
    marks = [p for p in places] + list(sources)
    content = [s for s in parsed.sections if s.name not in ("author", "sources")]
    last = content[-1] if content else None
    for s in content:
        if not mentions(s.text, marks):
            continue
        if s.name == "answer":
            return "in-answer"
        if s.name == "faq":
            return "in-faq"
        if s.name == "cta" or s is last:
            return "in-close"
        return "own-section"
    return None


def closing_text(parsed: Parsed) -> str:
    """The last paragraph of the final CTA block, else of the last body section before author and sources."""
    finals = [s for s in parsed.named("cta") if not s.secondary] or \
             [s for s in parsed.sections if s.name not in ("author", "sources", "faq") and s.text]
    if not finals:
        return ""
    paras = [l for l in finals[-1].lines if l.strip() and not l.strip().startswith("#")]
    return paras[-1].strip() if paras else ""


def closing_style(parsed: Parsed, places, lang: str = recipes.DEFAULT_LANG) -> str | None:
    text = closing_text(parsed)
    if not text:
        return None
    visible = plain(text)
    if visible.rstrip().endswith("?"):
        return "question-back"
    if mentions(visible, places):
        return "local-fact-restated"
    if first_word(visible) in recipes.lexicon(lang)["next_step_leads"]:
        return "next-step"
    return "one-line-summary"


def h2_questions(parsed: Parsed, faq_headings: dict) -> list[str]:
    """The H2s that carry the argument: not the FAQ heading and not the author, CTA or sources headings."""
    return [s.h2 for s in parsed.sections if s.h2 is not None and s.name not in STRUCTURAL_BLOCKS
            and s.h2.strip().lower() not in faq_headings]


def lead_share(h2s: list[str]) -> tuple[str, float]:
    leads = [norm(h).split()[0] for h in h2s if norm(h)]
    if not leads:
        return "", 0.0
    word = max(set(leads), key=lambda w: (leads.count(w), w))
    return word, leads.count(word) / len(leads)


def opening_words(answer: str, n: int) -> str:
    return " ".join(norm(answer).split()[:n])


def proof_items(parsed: Parsed, *, facts: dict, places, sources) -> list[str]:
    body = "\n".join(s.text for s in parsed.sections if s.name not in ("author", "sources"))
    evidence_text = "\n".join(s.text for s in parsed.sections if s.name not in ("author", "sources", "cta"))
    out = []
    if text_numbers(body) & fact_numbers(facts):
        out.append("fact-number")
    if mentions(body, places):
        out.append("local-place")
    if sources:
        out.append("named-source")
    if any(TABLE_RE.match(l) or NUMBERED_RE.match(l) or BULLET_RE.match(l) for l in evidence_text.splitlines()):
        out.append("table-or-list")
    if parsed.named("scenario"):
        out.append("scenario")
    if image_evidence_in(body):
        out.append("photo-or-illustration")
    return out


def article_axes(text: str, *, facts: dict, places, city: str = "", used: str = "", footers=(), faq_headings=None) -> dict:
    """Every axis the variation gate judges, read from the draft. `places` may hold the city: the title may lead with
    it, but the local tie-in placement and a restated closing fact need a place from the research file."""
    rules = recipes.article()
    headings = faq_headings or md_mod.DEFAULT_FAQ_HEADINGS
    lang = language_of(text, headings)
    parsed = parse(text, footers)
    sources = source_urls(parsed)
    specific = {p for p in places if p and p.lower() != city.lower()}
    count = word_count(parsed)
    h2s = h2_questions(parsed, headings)
    lead, share = lead_share(h2s)
    blocks = parsed.blocks
    shape = next((n for n, b in recipes.shapes().items() if list(b) == blocks), None)
    return {"shape": shape or "custom:" + ">".join(blocks), "blocks": blocks, "title": parsed.title,
            "title_pattern": title_pattern(parsed.title, places, lang), "intro": intro_style(parsed.answer),
            "evidence": evidence_element(parsed), "faq_n": len(md_mod.visible_faq(text, headings)),
            "cta": cta_position(parsed), "words": count, "word_band": band_of(count, rules["axes"]["word_band"]["values"]),
            "local_placement": local_placement(parsed, specific, sources), "closing": closing_style(parsed, specific, lang),
            "opening": opening_words(parsed.answer, int(rules["opening_unique_words"])), "h2s": h2s,
            "h2_lead": lead, "h2_lead_share": round(share, 3),
            "proof_items": proof_items(parsed, facts=facts, places=places, sources=sources), "sources": sources,
            "lang": lang, "used_in_sources": used_in_sources(parsed, used or "")}


def gbp_axes(text: str, *, places, lang: str | None = None) -> dict:
    lang = lang or language_of(text)
    lines = [l.strip() for l in strip_comments(text).splitlines() if l.strip() and not l.lstrip().startswith("#")]
    first = plain(lines[0]) if lines else ""
    count = len(words_of("\n".join(lines)))
    if first.rstrip().endswith("?"):
        opening = "question"
    elif mentions(first, places):
        opening = "local-hook"
    elif first_word(first) in recipes.lexicon(lang)["scenario_leads"]:
        opening = "scenario"
    else:
        opening = "fact"
    return {"opening": opening, "words": count, "length_band": band_of(count, recipes.gbp()["axes"]["length_band"]["values"]),
            "first_line": first}


# ---------------------------------------------------------------- inputs from the workspace

def research_inputs(draft, ws) -> tuple[set[str], str]:
    """(places, used) from the draft's research sidecar and file; the city alone when there is none."""
    places, used = set(), ""
    try:
        side = json.loads(ws.contains(draft.sidecar(".research.json")).read_text(encoding="utf-8"))
        used = str(side.get("used", "")) if isinstance(side, dict) else ""
        path = ws.path(str(side.get("file", ""))) if isinstance(side, dict) and side.get("file") else None
        if path and path.is_file():
            places = research_places(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - the research gate reports a broken sidecar; here it only means fewer places
        pass
    city = city_of(ws)
    return (places | {city}) if city else places, used


def load_facts(ws) -> dict:
    try:
        data = json.loads(ws.file("facts").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"facts file could not be read ({exc})") from exc
    return data if isinstance(data, dict) else {}


def footers_of(ws) -> list[str]:
    return [l.get("footer", "") for l in ws.config.get("locations") or ()]


def read_log(ws) -> dict:
    try:
        return json.loads(ws.file("content_log").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"content log could not be read ({exc})") from exc


def draft_fields(text: str, kind: str, ws, *, places=None, used: str = "") -> dict:
    """What `content log --draft` records from the text: the opening words, the H2s and the word count."""
    places = places if places is not None else ({city_of(ws)} - {""})
    if kind == "article":
        axes = article_axes(text, facts={}, places=places, city=city_of(ws), used=used, footers=footers_of(ws),
                            faq_headings=md_mod.faq_headings(ws))
        return {"opening": axes["opening"], "h2s": axes["h2s"], "words": axes["words"]}
    axes = gbp_axes(text, places=places)
    return {"opening": axes["opening"], "words": axes["words"]}


# ---------------------------------------------------------------- the verdicts

def _other_articles(log: dict, slug: str) -> list[dict]:
    return [a for a in log.get("articles", []) if a.get("status") != "removed" and not (slug and a.get("slug") == slug)]


def _own_article(log: dict, slug: str):
    return next((a for a in reversed(log.get("articles", [])) if slug and a.get("slug") == slug), None)


def _days(a: str, b: dt.date) -> int | None:
    try:
        return abs((b - dt.date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return None


def judge_article(axes: dict, log: dict, slug: str, today: dt.date) -> tuple[bool, str]:
    """(passed, detail). The detail names the rule that failed, or sums up the axes when all pass."""
    data = recipes.load()
    rules = data["article"]
    others = _other_articles(log, slug)
    recent = others[-int(data["window"]):]
    for a in recent:
        if a.get("blocks") and list(a["blocks"]) == axes["blocks"]:
            return False, f"shape: block order equals {a.get('slug')} ({a.get('shape', '?')}), one of the last {len(recent)} posts"
    if others:
        prev = others[-1]
        shared = [k for k in recipes.COMPARED_ARTICLE_AXES if axes.get(k) is not None and prev.get(k) == axes.get(k)]
        if len(shared) > int(data["max_shared_axes_with_previous"]):
            return False, f"shared-axes: {len(shared)} axes equal the previous post {prev.get('slug')} ({', '.join(shared)}); at most {data['max_shared_axes_with_previous']}"
    n = int(rules["opening_unique_words"])
    for a in others:
        logged = opening_words(str(a.get("opening") or ""), n)
        if logged and len(logged.split()) >= n and logged == axes["opening"]:
            return False, f"opening: the first {n} words of the answer equal {a.get('slug')}'s opening"
    mine = {norm(h) for h in axes["h2s"]}
    for a in others:
        age = _days(a.get("date", ""), today)
        if age is None or age > H2_REPEAT_DAYS:
            continue
        hit = next((h for h in a.get("h2s") or [] if norm(h) in mine), None)
        if hit:
            return False, f"h2-repeat: {hit!r} is a ## in {a.get('slug')} ({a.get('date')}, within {H2_REPEAT_DAYS} days)"
    if axes["h2s"] and axes["h2_lead_share"] > float(rules["h2_lead_max_share"]):
        return False, f"h2-lead: {axes['h2_lead_share']:.0%} of the H2s start with {axes['h2_lead']!r}; at most {float(rules['h2_lead_max_share']):.0%}"
    need = int(rules["proof_items_min"])
    if len(axes["proof_items"]) < need:
        missing = [p for p in rules["proof_items"] if p not in axes["proof_items"]]
        return False, f"proof-items: {len(axes['proof_items'])} of {need} present ({', '.join(axes['proof_items']) or 'none'}); missing {', '.join(missing)}"
    own = _own_article(log, slug)
    band = (own or {}).get("word_band")
    if band and band_of(axes["words"], [band]) != band:
        return False, f"word-band: {axes['words']} words, the recipe asked for {band}"
    run = int(rules["axes"]["word_band"].get("max_run", 0))
    last = [a.get("word_band") for a in others[-run:]] if run else []
    if axes.get("word_band") and len(last) == run and set(last) == {axes["word_band"]}:
        return False, (f"word-band: {axes['words']} words would be the {run + 1}th post in a row in {axes['word_band']} "
                       f"(README: not {run + 1} in a row)")
    summary = ", ".join(f"{k} {axes[k]}" for k in ("shape", "title_pattern", "intro", "evidence", "faq_n", "cta", "word_band",
                                                     "local_placement", "closing") if axes.get(k) is not None)
    return True, f"{summary}; proof {len(axes['proof_items'])}/{len(rules['proof_items'])}"


def _own_gbp(log: dict, slug: str, first_line: str):
    first = norm(first_line)
    for g in reversed(log.get("gbp", [])):
        if slug and g.get("slug") == slug:
            return g
        head = norm(g.get("headline", ""))
        if g.get("status") != "published" and len(head) >= 20 and first.startswith(head):
            return g
    return None


def judge_gbp(axes: dict, log: dict, slug: str) -> tuple[bool, str]:
    data = recipes.gbp()
    own = _own_gbp(log, slug, axes["first_line"])
    others = [g for g in log.get("gbp", []) if g.get("status") != "removed" and g is not own]
    recent = others[-int(recipes.load()["window"]):]
    if own and own.get("gbp_type"):
        last = [g.get("gbp_type") for g in recent if g.get("gbp_type")][-int(data["type_avoid_last"]):]
        if own["gbp_type"] in last:
            return False, f"gbp-type: {own['gbp_type']} was used by one of the last {data['type_avoid_last']} posts"
    avoid = int(data["axes"]["opening"]["avoid_last"])
    last_openings = [g.get("opening") for g in recent if g.get("opening")][-avoid:]
    if axes["opening"] in last_openings:
        return False, f"gbp-opening: {axes['opening']} opening was used by one of the last {avoid} posts"
    run = int(data["axes"]["length_band"]["max_run"])
    bands = [g.get("length_band") for g in recent if g.get("length_band")][-run:]
    if axes["length_band"] and len(bands) == run and set(bands) == {axes["length_band"]}:
        return False, f"gbp-length-band: {axes['length_band']} words would be the {run + 1}th post in a row in that band"
    return True, f"opening {axes['opening']}, {axes['words']} words ({axes['length_band'] or 'outside every band'})"


def check_draft(draft, ws, today: dt.date) -> tuple[bool, str]:
    """The variation gate's verdict for an article or Business Profile draft. ValueError when it cannot run."""
    log = read_log(ws)
    places, used = research_inputs(draft, ws)
    if draft.kind == "gbp":
        return judge_gbp(gbp_axes(draft.text, places=places - {city_of(ws)}), log, draft.slug)
    axes = article_axes(draft.text, facts=load_facts(ws), places=places, city=city_of(ws), used=used,
                        footers=footers_of(ws), faq_headings=md_mod.faq_headings(ws))
    return judge_article(axes, log, draft.slug, today)
