"""content/md: convert a workspace's AEO blog Markdown into WordPress-ready HTML (ported from the earlier house Markdown script).

Stdlib only. Handles exactly the subset the blog drafts use: ## / ### headings,
paragraphs, **bold**, *italic*, [links](url), bullet and numbered lists, pipe
tables, and --- rules.

Internal HTML comments are never served. The two that carry data are lifted
out first: the meta-description comment becomes the post excerpt, and the
FAQPage comment becomes a real <script type="application/ld+json"> block. Every
other comment is dropped — HTML comments are delivered to crawlers and LLMs, so
rationale must never ship in them.

With `options(ws)` the output also carries an `Article` block (author Person from
facts.json `author`, publisher Organization from customer.json `business`, the
dates) and a `BreadcrumbList` (site > blog > post), each in its own script tag.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field

# The body cannot cross another "<!--", so a text of repeated openers is scanned once, not quadratically (audit L3).
COMMENT_RE = re.compile(r"<!--((?:(?!<!--).)*?)-->", re.S)

# FAQ H2 text -> language code. Code defaults for English and Spanish; a customer adds or overrides a language's
# heading in customer.json business.languages[].faq_heading (see faq_headings()).
DEFAULT_FAQ_HEADINGS = {"frequently asked questions": "en", "preguntas frecuentes": "es"}
LANG_RE = re.compile(r"^\s*lang:\s*([a-z]{2})\s*$", re.I)


def faq_headings(ws) -> dict:
    """{heading (lower-cased): language code} for the workspace: the defaults plus every configured faq_heading."""
    out = dict(DEFAULT_FAQ_HEADINGS)
    for lang in ws.config["business"].get("languages") or ():
        if lang.get("faq_heading"):
            out[lang["faq_heading"].strip().lower()] = lang["code"]
    return out


def author_of(ws) -> dict | None:
    """facts.json `author` {name, role, url} when present and complete, else None (the eeat gate reports it)."""
    try:
        facts = json.loads(ws.file("facts").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    author = facts.get("author") if isinstance(facts, dict) else None
    if isinstance(author, dict) and author.get("name") and author.get("url"):
        return {"name": author["name"], "url": author["url"], **({"role": author["role"]} if author.get("role") else {})}
    return None


def options(ws, *, slug: str | None = None, date: str | None = None, modified: str | None = None,
            image: str | None = None, blog_path: str = "blog") -> dict:
    """Keyword arguments for convert() from the workspace config: the FAQ headings and the entity data the Article
    and BreadcrumbList schemas need. `date` defaults to today (MSEO_TODAY honoured); `modified` to `date`."""
    from mseo.doctor import today

    business = ws.config["business"]
    date = date or today().isoformat()
    return {"faq_headings": faq_headings(ws),
            "entity": {"site": business["site"].rstrip("/"), "name": business["name"], "author": author_of(ws),
                       "slug": slug, "date": date, "modified": modified or date, "image": image, "blog_path": blog_path}}


def article_schema(title: str, excerpt: str, entity: dict) -> dict:
    site = entity["site"]
    out = {"@context": "https://schema.org", "@type": "Article", "headline": plain(title),
           "publisher": {"@type": "Organization", "name": entity["name"], "url": site + "/"},
           "datePublished": entity["date"], "dateModified": entity.get("modified") or entity["date"]}
    if excerpt:
        out["description"] = excerpt
    if entity.get("author"):
        out["author"] = {"@type": "Person", "name": entity["author"]["name"], "url": entity["author"]["url"]}
    if entity.get("slug"):
        out["mainEntityOfPage"] = f"{site}/{entity['slug'].strip('/')}/"
    if entity.get("image"):
        out["image"] = entity["image"]
    return out


def breadcrumb_schema(title: str, entity: dict) -> dict:
    site = entity["site"]
    blog = entity.get("blog_path") or "blog"
    items = [{"@type": "ListItem", "position": 1, "name": entity["name"], "item": site + "/"},
             {"@type": "ListItem", "position": 2, "name": blog.strip("/").capitalize(), "item": f"{site}/{blog.strip('/')}/"},
             {"@type": "ListItem", "position": 3, "name": plain(title)}]
    if entity.get("slug"):
        items[2]["item"] = f"{site}/{entity['slug'].strip('/')}/"
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}


def _faq_sections(md: str, headings: dict | None):
    """[(language, question, answer, line)] for each ### question under a FAQ H2, answers up to the next heading or rule."""
    headings = headings or DEFAULT_FAQ_HEADINGS
    md = COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), md)
    out, lang = [], None
    for n, line in enumerate(md.splitlines(), 1):
        s = line.strip()
        if re.match(r"^#{1,2}\s", s) or re.fullmatch(r"-{3,}", s):
            lang = headings.get(s[3:].strip().lower()) if s.startswith("## ") else None
            continue
        if lang is None:
            continue
        if s.startswith("### "):
            out.append([lang, s[4:].strip(), [], n])
        elif s and out:
            out[-1][2].append(s)
    return [(lg, q, " ".join(a), n) for lg, q, a, n in out]


def visible_faq(md: str, headings: dict | None = None) -> list:
    """[(question, answer, line)] for each ### question under the FAQ H2."""
    return [(q, a, n) for _, q, a, n in _faq_sections(md, headings)]


def faq_language(md: str, headings: dict | None = None) -> str | None:
    """The FAQ's language: an explicit <!-- lang: xx --> comment wins, else the language of the FAQ heading."""
    for body in COMMENT_RE.findall(md):
        m = LANG_RE.match(body)
        if m:
            return m.group(1).lower()
    langs = [lg for lg, *_ in _faq_sections(md, headings)]
    return langs[0] if langs else None


def plain(text: str) -> str:
    """Markdown inline text as plain text: links reduced to their text, emphasis and code marks removed."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", html.unescape(text))
    return " ".join(re.sub(r"[*`]", "", text).split())


def faq_schema(md: str, headings: dict | None = None) -> dict | None:
    """The FAQPage JSON-LD for the visible FAQ, or None when the post has none. Built from the page itself, so a
    rewrite of the visible answers can never leave the schema saying something else. inLanguage is set for any
    language but English."""
    faq = visible_faq(md, headings)
    if not faq:
        return None
    lang = faq_language(md, headings)
    return {"@context": "https://schema.org", "@type": "FAQPage", **({"inLanguage": lang} if lang and lang != "en" else {}),
            "mainEntity": [{"@type": "Question", "name": plain(q),
                            "acceptedAnswer": {"@type": "Answer", "text": plain(a)}} for q, a, _ in faq]}
META_RE = re.compile(r"^\s*Meta Description:\s*(.+?)\s*$", re.S | re.I)
PLACEHOLDER_MARKERS = ("TODO", "###WRITE###", "###VERIFY###")


class MarkdownError(ValueError):
    """The source cannot be converted safely."""


@dataclass(frozen=True)
class Converted:
    title: str
    excerpt: str
    html: str
    schema: dict | None = None          # the FAQPage block (None without a FAQ)
    placeholders: tuple = field(default_factory=tuple)
    schemas: tuple = field(default_factory=tuple)  # every JSON-LD block in output order


# ------------------------------------------------------------------ inline

SAFE_SCHEMES = ("http:", "https:", "mailto:", "tel:")


def _link(m: re.Match) -> str:
    """Render [text](url); unsafe schemes (javascript:, data:, ...) keep the text only."""
    label, url = m.group(1), m.group(2)
    raw_url = html.unescape(url)
    if not (raw_url.lower().startswith(SAFE_SCHEMES) or raw_url.startswith(("/", "#"))):
        return label
    return f'<a href="{html.escape(raw_url, quote=True)}">{label}</a>'


def _inline(text: str) -> str:
    """Escape, then apply link / bold / italic. Order matters: escape first."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    return out


# ------------------------------------------------------------------ blocks

def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) < 2:
        raise MarkdownError("table needs a header row and a separator row")
    head, body = cells[0], cells[2:]
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table>\n<thead><tr>{th}</tr></thead>\n<tbody>{trs}</tbody>\n</table>"


def _list(lines: list[str], ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    strip = r"^\s*\d+\.\s+" if ordered else r"^\s*[-*]\s+"
    items = "".join(f"<li>{_inline(re.sub(strip, '', l))}</li>" for l in lines)
    return f"<{tag}>{items}</{tag}>"


def _blocks(md: str) -> list[str]:
    out, para = [], []
    lines = md.splitlines()
    i = 0

    def flush():
        if para:
            out.append("<p>" + "<br>\n".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush(); i += 1; continue
        if s.startswith("### "):
            flush(); out.append(f"<h3>{_inline(s[4:])}</h3>"); i += 1; continue
        if s.startswith("## "):
            flush(); out.append(f"<h2>{_inline(s[3:])}</h2>"); i += 1; continue
        if s.startswith("# "):
            flush(); i += 1; continue          # title is rendered by WordPress
        if re.fullmatch(r"-{3,}", s):
            flush(); out.append("<hr>"); i += 1; continue
        if s.startswith("|"):
            flush(); block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i]); i += 1
            out.append(_table(block)); continue
        for ordered, pat in ((False, r"^\s*[-*]\s+"), (True, r"^\s*\d+\.\s+")):
            if re.match(pat, line):
                flush(); block = []
                while i < len(lines) and re.match(pat, lines[i]):
                    block.append(lines[i]); i += 1
                out.append(_list(block, ordered)); break
        else:
            para.append(s); i += 1
    flush()
    return out


# ------------------------------------------------------------------ comments

def _extract_schema(comment: str) -> dict:
    start, end = comment.find("{"), comment.rfind("}")
    if start < 0 or end <= start:
        raise MarkdownError("FAQPage comment has no JSON object")
    try:
        data = json.loads(comment[start:end + 1])
    except json.JSONDecodeError as exc:
        raise MarkdownError(f"FAQPage schema is not valid JSON: {exc}") from None
    if data.get("@type") != "FAQPage" or not data.get("mainEntity"):
        raise MarkdownError("schema comment is not a populated FAQPage")
    return data


def jsonld_text(schema: dict, **dumps_kwargs) -> str:
    """JSON for a <script type="application/ld+json"> body: '<', '>' and '&' become \\u escapes (still JSON), so no
    value can close the script tag or start markup."""
    text = json.dumps(schema, ensure_ascii=False, **dumps_kwargs)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _script(schema: dict) -> str:
    return '<script type="application/ld+json">' + jsonld_text(schema, separators=(",", ":")) + "</script>"


def convert(md: str, *, faq_headings: dict | None = None, entity: dict | None = None) -> Converted:
    """`entity` (from options(ws)) adds the Article and BreadcrumbList blocks; without it only the FAQ ships."""
    excerpt, schema = "", None
    for body in COMMENT_RE.findall(md):
        meta = META_RE.match(body)
        if meta:
            excerpt = " ".join(meta.group(1).split())
        elif "FAQPage" in body:
            schema = _extract_schema(body)
    stripped = COMMENT_RE.sub("", md)

    title_m = re.search(r"^#\s+(.+)$", stripped, re.M)
    if not title_m:
        raise MarkdownError("no '# Title' line found")
    title = title_m.group(1).strip()

    schema = faq_schema(md, faq_headings) or schema  # the visible FAQ wins; an old schema comment is used only without one
    parts = _blocks(stripped)
    schemas = []
    if entity:
        schemas += [article_schema(title, excerpt, entity), breadcrumb_schema(title, entity)]
    if schema:
        schemas.append(schema)
    parts += [_script(s) for s in schemas]
    found = tuple(m for m in PLACEHOLDER_MARKERS if m in stripped)
    return Converted(title=title, excerpt=excerpt, html="\n".join(parts),
                     schema=schema, placeholders=found, schemas=tuple(schemas))
