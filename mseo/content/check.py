"""content/check: `mseo content check DRAFT`, every pre-publish gate on one draft (ported from the earlier house check script).

    mseo content check drafts/post.md --workspace S --kind article --slug my-post-slug [--json] [--gate NAME]
    mseo content check drafts/gbp_A.txt --workspace S --kind gbp

Runs all gates in a fixed order and never stops early. Each gate reports pass, warn, fail or error (the gate could
not run). The gates run in-process on the bytes read once; an earlier version ran house scripts as subprocesses.
A full run writes <draft>.check.json atomically, with the SHA-256 of the draft, of its input files (customer.json
among them) and of the mseo modules that judged it. `--gate` prints one gate and writes nothing.
The draft must be inside the workspace.

Exit codes: 0 pass (non-blocking warnings allowed) · 1 a blocking fail or warn · 3 a gate could not run
(also when there is both a failure and an error: the gate that did not run may hide more failures).

Sidecars a draft needs: <draft>.deslop.json (from `deslop gate DRAFT --seo --in-place --always --receipt
DRAFT.deslop.json`) and <draft>.research.json ({"file", "used", "event_dates"}).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from mseo.content import claims as claims_mod
from mseo.content import dupes as dupes_mod
from mseo.content import eeat as eeat_mod
from mseo.content import md as md_mod
from mseo.content import recipes as recipes_mod
from mseo.content import shapes, wording
from mseo.content import variation as variation_mod
from mseo.content.links import check_url, extract_urls
from mseo.content.lint_copy import content_rules, lint_text
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.fsutil import sha256_file

PASS, WARN, FAIL, ERROR = "pass", "warn", "fail", "error"
BOTH, ARTICLE = ("article", "gbp"), ("article",)
MARKER_RE = re.compile(r"^\s*<!--\s*block:\s*([\w-]+)\s*-->\s*$")
EXPIRES_RE = re.compile(r"^[^\w\n]{0,8}expires[^\w\n]{0,8}:\s*(\d{4}-\d{2}-\d{2})", re.M | re.I)
PACKAGE = Path(__file__).resolve().parents[1]
# The modules whose code decides a verdict: change one and every check.json is stale.
TOOL_FILES = ("content/check.py", "content/claims.py", "content/dupes.py", "content/eeat.py", "content/lint_copy.py",
              "content/links.py", "content/log.py", "content/md.py", "content/recipes.py", "content/shapes.py",
              "content/variation.py", "content/wording.py", "content/places.py", "cms/wordpress.py", "publish.py")


class CouldNotRun(Exception):
    """The check cannot start (no draft, draft outside the workspace)."""


def today() -> dt.date:
    from mseo.doctor import today as doctor_today

    return doctor_today()


# ---------------------------------------------------------------- inputs

@dataclass
class Draft:
    """The draft as read once: every gate, the result hash and `mseo publish` use these exact bytes, never a re-read."""
    path: Path
    kind: str
    text: str
    slug: str = ""
    query: str = ""
    theme: str = ""
    data: bytes = b""
    strict: bool = False  # publish: a missing section-order log entry blocks instead of warning

    @classmethod
    def load(cls, ws, path, kind, slug="", query="", theme="", strict=False):
        try:
            path = ws.contains(path)
        except Exception as exc:  # OutsideWorkspace: refuse before reading a byte
            raise CouldNotRun(str(exc)) from exc
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CouldNotRun(f"draft {path} could not be read ({exc})") from exc
        return cls(path, kind, text, slug or "", query or "", theme or "", data, strict)

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data or self.text.encode("utf-8")).hexdigest()

    def sidecar(self, suffix: str) -> Path:
        return sidecar(self.path, suffix)


def sidecar(path: Path, suffix: str) -> Path:
    """<draft><suffix>: gbp_A.txt -> gbp_A.txt.check.json, the same way deslop names DRAFT.deslop.json."""
    return path.with_name(path.name + suffix)


@dataclass
class GateResult:
    name: str
    status: str
    blocking: bool = True
    detail: str = ""
    line: int | None = None


def _line(text: str, needle: str) -> int | None:
    pos = text.lower().find(needle.lower())
    return text.count("\n", 0, pos) + 1 if pos >= 0 else None


def site_of(ws) -> str:
    return ws.config["business"]["site"]


# ---------------------------------------------------------------- gates

def gate_humanizer(draft: Draft, ws) -> GateResult:
    path = ws.contains(draft.sidecar(".deslop.json"))  # a symlink out of the workspace -> OutsideWorkspace -> error
    fix = f"run: deslop gate {draft.path.name} --seo --in-place --always --receipt {path.name}"
    if not path.exists():
        return GateResult("humanizer", FAIL, True, f"no humanizer receipt; {fix}")
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        r["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return GateResult("humanizer", ERROR, True, f"receipt {path.name} unreadable ({exc})")
    if r["sha256"] != draft.sha:
        return GateResult("humanizer", FAIL, True, f"draft changed after humanizing (hash differs from the receipt); {fix}")
    if r.get("rewritten") is not True:
        return GateResult("humanizer", FAIL, True, f"receipt records no rewrite (rewritten: {r.get('rewritten')}); add --always")
    if not isinstance(r.get("after"), (int, float)) or r["after"] < 5:
        return GateResult("humanizer", FAIL, True, f"score after humanizing is {r.get('after')}/5, needs 5/5")
    if r.get("humanizer_brief") != "live":
        return GateResult("humanizer", FAIL, True, f"humanizer brief was {r.get('humanizer_brief')!r} (fallback), not the live humanizer skill")
    return GateResult("humanizer", PASS, True, f"rewritten {r.get('before')}/5 -> {r['after']}/5 by {r.get('model')}")


def gate_wording(draft: Draft, ws) -> GateResult:
    found = wording.check(draft.text, wording.rules(ws))
    if not found:
        return GateResult("wording", PASS)
    line, hit, fix = found[0]
    return GateResult("wording", FAIL, True, f"'{hit}' -> use '{fix}'", line)


def gate_duplicates(draft: Draft, ws) -> GateResult:
    code, out = dupes_mod.run(ws, draft.kind, draft.text, today(), draft.query, draft.theme, draft.slug)
    if code == EXIT_COULD_NOT_RUN:
        return GateResult("duplicates", ERROR, True, out["error"])
    findings = out.get("findings", [])
    if out.get("status") == "pass":
        return GateResult("duplicates", PASS, True, f"window {out.get('window_days')} days, no overlap")
    first = findings[0] if findings else {}
    scores = ", ".join(f"{k} {v}" for k, v in first.items() if k not in ("level", "against"))
    detail = f"{len(findings)} finding(s); first: {first.get('level')} vs {first.get('against')} ({scores})"
    return GateResult("duplicates", FAIL if out.get("status") == "fail" else WARN, True, detail)


def gate_rules(draft: Draft, ws) -> GateResult:
    facts = claims_mod.load_facts(ws.file("facts"))  # FactsUnavailable -> error, via run()
    findings = lint_text(draft.text, gbp=draft.kind == "gbp", facts=facts, **content_rules(ws))
    errors = [msg for lv, msg in findings if lv == "ERROR"]
    if errors:
        return GateResult("rules", FAIL, True, f"{len(errors)} error(s); first: {errors[0]}")
    if findings:  # length and tone warnings never block; unverified claims are blocked by the claims gate
        return GateResult("rules", WARN, False, f"{len(findings)} warning(s); first: {findings[0][1]}")
    return GateResult("rules", PASS)


def denylisted(text: str, phrases) -> list:
    """[(line, phrase)] for each banned claim stated in `text`. A negated statement ("we do not offer X") is allowed."""
    out = []
    for phrase in phrases:
        for m in re.finditer(re.escape(phrase), text, re.I):
            sentence = re.split(r"[.!?\n]", text[:m.start()])[-1]
            if not claims_mod.NEGATION_RE.search(sentence):
                out.append((text.count("\n", 0, m.start()) + 1, phrase))
    return out


def gate_claims(draft: Draft, ws) -> GateResult:
    facts = claims_mod.load_facts(ws.file("facts"))
    rules = content_rules(ws)
    problems = [(_line(draft.text, c), f'unverified claim "{c}"')
                for c in claims_mod.unverified_claims(draft.text, facts, rules["claim_patterns"])]
    problems += [(line, f'banned claim "{p}" (RULES.md item 3)')
                 for line, p in denylisted(draft.text, ws.config["content"]["denylist_claims"])]
    try:
        facts_doc = json.loads(ws.file("facts").read_text(encoding="utf-8"))
        nouns = ws.config["content"].get("business_nouns") or claims_mod.DEFAULT_BUSINESS_NOUNS
        shop = claims_mod.business_mismatches(draft.text, facts_doc, ws.config["business"]["brand_terms"], nouns)
    except (OSError, ValueError, claims_mod.FactsUnavailable) as exc:
        also = f"; also {len(problems)} claim problem(s), first: {problems[0][1]}" if problems else ""
        return GateResult("claims", ERROR, True, f"{exc}{also}")
    problems += [(line, f'"{found}" does not match the business facts ({expected})') for line, found, expected in shop]
    if not problems:
        return GateResult("claims", PASS)
    problems.sort(key=lambda p: (p[0] is None, p[0] or 0))
    return GateResult("claims", FAIL, True, f"{len(problems)} problem(s); first: {problems[0][1]}", problems[0][0])


def site_links(text: str, site: str) -> list:
    """Absolute links on the workspace's own site plus root-relative Markdown links, resolved against the site."""
    host = urllib.parse.urlparse(site).hostname or ""
    urls = extract_urls(text, host) if host else []
    for rel in re.findall(r"\]\((/[^)\s]*)\)", text):
        url = urllib.parse.urljoin(site, rel)
        if url not in urls:
            urls.append(url)
    return urls


def internal_links(text: str, site: str) -> list[tuple[str, str]]:
    """[(anchor text, url)] for every Markdown link on the site's own host or root-relative, comments stripped."""
    host = (urllib.parse.urlparse(site).hostname or "").lower()
    out = []
    for m in variation_mod.LINK_RE.finditer(variation_mod.strip_comments(text)):
        anchor, url = m.group(1), m.group(2)
        link_host = (urllib.parse.urlparse(url).hostname or "").lower()
        if url.startswith("/") or (host and (link_host == host or link_host.endswith("." + host))):
            out.append((anchor, url))
    return out


def path_matches(path: str, prefixes) -> bool:
    """True when the URL path starts with one of the configured paths (case and trailing slash aside)."""
    p = path.lower().rstrip("/")
    return any(p.startswith(x.lower().rstrip("/")) for x in prefixes if x.strip("/"))


def internal_link_problem(text: str, site: str, link_paths: dict | None = None) -> str | None:
    """The README's internal-link rule: at least recipes.internal_links_min links on the site, one to a service page
    and one to a contact page (content.link_paths in customer.json, matched by path prefix), no anchor text twice."""
    from mseo.content.new import link_paths as paths_of

    paths_cfg = paths_of({"content": {"link_paths": link_paths or {}}})
    need = int(recipes_mod.article().get("internal_links_min", 3))
    links = internal_links(text, site)
    if len(links) < need:
        return f"{len(links)} internal link(s), the post needs {need} (service page, contact or estimate page, a related article)"
    paths = [urllib.parse.urlparse(u).path for _, u in links]
    if not any(path_matches(p, paths_cfg["service"]) for p in paths):
        return f"no internal link to a service page (content.link_paths.service: {', '.join(paths_cfg['service'])})"
    if not any(path_matches(p, paths_cfg["contact"]) for p in paths):
        return f"no internal link to a contact or estimate page (content.link_paths.contact: {', '.join(paths_cfg['contact'])})"
    seen = set()
    for anchor, _ in links:
        key = _norm(anchor).lower()
        if key in seen:
            return f"anchor text {anchor!r} is used twice; every internal link needs its own wording"
        seen.add(key)
    return None


def gate_links(draft: Draft, ws) -> GateResult:
    site = site_of(ws)
    results = [check_url(u) for u in site_links(draft.text, site)]
    unreachable = [r for r in results if r.get("status") is None]
    broken = [r for r in results if r.get("status") is not None and not r.get("ok")]
    if unreachable:
        return GateResult("links", ERROR, True, f"could not reach {unreachable[0]['url']} ({unreachable[0].get('error')})")
    if broken:
        b = broken[0]
        return GateResult("links", FAIL, True, f"{len(broken)} broken; first: {b['url']} returned {b['status']}",
                          _line(draft.text, urllib.parse.urlparse(b["url"]).path or b["url"]))
    if draft.kind == "article":
        problem = internal_link_problem(draft.text, site, ws.config["content"].get("link_paths"))
        if problem:
            return GateResult("links", FAIL, True, problem)
    return GateResult("links", PASS, True, f"{len(results)} link(s) on {site} resolve" if results else f"no links to {site}")


def _log_entry(ws, slug: str):
    log = json.loads(ws.file("content_log").read_text(encoding="utf-8"))
    return next((a for a in reversed(log.get("articles", [])) if slug and a.get("slug") == slug), None)


def gate_sections(draft: Draft, ws) -> GateResult:
    entry = _log_entry(ws, draft.slug)
    if not entry or not entry.get("blocks"):
        why = "no --slug given" if not draft.slug else f"no content log entry with blocks for slug {draft.slug!r}"
        if draft.strict:  # publish: an unverifiable order blocks
            return GateResult("sections", FAIL, True, f"{why}; section order cannot be verified before publishing")
        return GateResult("sections", WARN, False, f"{why}; section order not compared")
    known = {b for blocks in shapes.SHAPES.values() for b in blocks}
    found, lines, prev = ["answer"], [None], ""
    for n, line in enumerate(draft.text.splitlines(), 1):
        if line.startswith("## "):
            m = MARKER_RE.match(prev)
            if not m:
                return GateResult("sections", FAIL, True, f"missing <!-- block: NAME --> marker before H2 {line[3:].strip()!r}", n)
            if m.group(1) not in known:
                return GateResult("sections", FAIL, True, f"unknown block name {m.group(1)!r} (known: {', '.join(sorted(known))})", n - 1)
            found.append(m.group(1))
            lines.append(n - 1)
        if line.strip():
            prev = line
    expected = list(entry["blocks"])
    if found != expected:
        at = next((i for i, (a, b) in enumerate(zip(found, expected, strict=False)) if a != b), min(len(found), len(expected)))
        return GateResult("sections", FAIL, True, f"order {' > '.join(found)} differs from the logged shape "
                          f"{entry.get('shape', '?')}: {' > '.join(expected)}", lines[at] if at < len(lines) else None)
    return GateResult("sections", PASS, True, f"{entry.get('shape', 'logged')} order: {' > '.join(found)}")


def _norm(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", html.unescape(text))
    return " ".join(re.sub(r"[*`]", "", text).split())


def gate_faq(draft: Draft, ws) -> GateResult:
    visible = md_mod.visible_faq(draft.text, md_mod.faq_headings(ws))
    if not visible:
        return GateResult("faq", FAIL, True, "no '## Frequently asked questions' section with ### questions")
    comments = [c for c in md_mod.COMMENT_RE.findall(draft.text) if "FAQPage" in c]
    if not comments:  # md builds the schema from the visible FAQ, so there is nothing to drift
        return GateResult("faq", PASS, True, f"schema built from {len(visible)} visible question(s)")
    try:
        schema = md_mod._extract_schema(comments[0])
    except md_mod.MarkdownError as exc:
        return GateResult("faq", FAIL, True, str(exc), _line(draft.text, "FAQPage"))
    seen = {_norm(q): (_norm(a), n) for q, a, n in visible}
    marked = {_norm(i.get("name", "")): _norm((i.get("acceptedAnswer") or {}).get("text", ""))
              for i in schema.get("mainEntity", [])}
    hint = "(the schema is built from the visible FAQ: delete the FAQPage comment)"
    for q, answer in marked.items():
        if q not in seen:
            return GateResult("faq", FAIL, True, f"schema question {q!r} has no visible match {hint}", _line(draft.text, q))
        if seen[q][0] != answer:
            return GateResult("faq", FAIL, True, f"answer to {q!r} differs between schema and page {hint}", seen[q][1])
    for q, (_, n) in seen.items():
        if q not in marked:
            return GateResult("faq", FAIL, True, f"visible question {q!r} is missing from the schema {hint}", n)
    return GateResult("faq", PASS, True, f"{len(marked)} question(s) match")


def research_file(draft: Draft, ws):
    """(sidecar dict, research file path inside the workspace). Raises ValueError on an unreadable sidecar."""
    side = json.loads(ws.contains(draft.sidecar(".research.json")).read_text(encoding="utf-8"))
    if not isinstance(side, dict):
        raise ValueError("research sidecar must be a JSON object")
    return side, ws.path(str(side.get("file", "")))


def gate_research(draft: Draft, ws) -> GateResult:
    if not draft.sidecar(".research.json").exists():
        return GateResult("research", FAIL, True, f"no local research receipt {draft.sidecar('.research.json').name}")
    try:
        side, path = research_file(draft, ws)
    except (OSError, ValueError) as exc:
        return GateResult("research", ERROR, True, f"research sidecar unreadable ({exc})")
    if not side.get("file") or not path.is_file():
        return GateResult("research", FAIL, True, f"research file {side.get('file')!r} not found in workspace {ws.slug}")
    if not str(side.get("used", "")).strip():
        return GateResult("research", FAIL, True, "sidecar has no 'used' item (or 'none relevant' and why)")
    body = path.read_text(encoding="utf-8")
    m = EXPIRES_RE.search(body)
    if not m:
        return GateResult("research", FAIL, True, f"{side['file']} has no expires: date")
    if today() >= dt.date.fromisoformat(m.group(1)):
        return GateResult("research", FAIL, True, f"{side['file']} expired on {m.group(1)}; run this week's local research")
    missing = [d for d in side.get("event_dates", []) if str(d) not in body]
    if missing:
        return GateResult("research", FAIL, True, f"event date {missing[0]} is not in {side['file']}")
    return GateResult("research", PASS, True, f"{side['file']}: {side['used']}")


def gate_variation(draft: Draft, ws) -> GateResult:
    try:
        ok, detail = variation_mod.check_draft(draft, ws, today())
    except ValueError as exc:
        return GateResult("variation", ERROR, True, str(exc))
    return GateResult("variation", PASS if ok else FAIL, True, detail)


def gate_eeat(draft: Draft, ws) -> GateResult:
    try:
        ok, detail = eeat_mod.check_draft(draft, ws, today())
    except ValueError as exc:
        return GateResult("eeat", ERROR, True, str(exc))
    return GateResult("eeat", PASS if ok else FAIL, True, detail)


GATES = [
    ("humanizer", gate_humanizer, BOTH),
    ("wording", gate_wording, BOTH),
    ("duplicates", gate_duplicates, BOTH),
    ("rules", gate_rules, BOTH),
    ("claims", gate_claims, BOTH),
    ("links", gate_links, BOTH),
    ("sections", gate_sections, ARTICLE),
    ("faq", gate_faq, ARTICLE),
    ("research", gate_research, BOTH),
    ("variation", gate_variation, BOTH),
    ("eeat", gate_eeat, ARTICLE),
]
GATE_NAMES = [name for name, _, _ in GATES]


def gates_for(kind: str) -> list:
    return [name for name, _, kinds in GATES if kind in kinds]


def run(draft: Draft, ws, only: str | None = None) -> list:
    """Every gate that applies to the draft's kind, each in its own try: one broken gate never hides the others."""
    if only and draft.kind not in dict((n, k) for n, _, k in GATES).get(only, ()):
        return [GateResult(only, ERROR, True, f"gate {only!r} does not apply to a {draft.kind} draft")]
    out = []
    for name, gate, kinds in GATES:
        if draft.kind not in kinds or (only and name != only):
            continue
        try:
            out.append(gate(draft, ws))
        except Exception as exc:  # noqa: BLE001 - any crash is recorded as this gate's error, never a pass
            out.append(GateResult(name, ERROR, True, f"{type(exc).__name__}: {exc}"))
    return out


def result_of(gates) -> str:
    if any(g.status == ERROR for g in gates):
        return ERROR
    if any(g.blocking and g.status in (FAIL, WARN) for g in gates):
        return FAIL
    return PASS


EXIT_FOR = {PASS: EXIT_OK, FAIL: EXIT_FAIL, ERROR: EXIT_COULD_NOT_RUN}


# ---------------------------------------------------------------- result file

def _research_path(draft_path: Path, ws) -> Path | None:
    try:
        side = json.loads(ws.contains(sidecar(draft_path, ".research.json")).read_text(encoding="utf-8"))
        return ws.path(str(side["file"]))
    except Exception:  # noqa: BLE001 - unreadable or outside the workspace: no research input to hash
        return None


def _safe_sha256_sidecar(ws, draft_path: Path, suffix: str) -> str | None:
    try:
        return sha256_file(ws.contains(sidecar(draft_path, suffix)))
    except Exception:
        return None


def _log_entry_hash(ws, slug: str | None) -> str | None:
    """Hash of this article's own content-log entry. The rest of the log is left out on purpose: logging another
    piece must not make this result stale, and publish re-runs the duplicate check anyway."""
    if not slug:
        return None
    try:
        log = json.loads(ws.file("content_log").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = next((a for a in log.get("articles", []) if a.get("slug") == slug), None)
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest() if entry else None


def input_hashes(ws, draft_path: Path, slug: str | None = None) -> dict:
    """Every file a gate's verdict depends on, besides the draft and the tools: change one and the result is stale."""
    research = _research_path(draft_path, ws)
    return {"facts.json": sha256_file(ws.file("facts")), "RULES.md": sha256_file(ws.file("rules")),
            "customer.json": sha256_file(ws.path("customer.json")), "log_entry": _log_entry_hash(ws, slug),
            "research_sidecar": _safe_sha256_sidecar(ws, draft_path, ".research.json"),
            "research_file": sha256_file(research) if research else None,
            "deslop_receipt": _safe_sha256_sidecar(ws, draft_path, ".deslop.json")}


def tool_hashes() -> dict:
    return {name: sha256_file(PACKAGE / name) for name in TOOL_FILES}


def deslop_version() -> str | None:
    """Recorded for the audit trail only; the humanizer gate reads the receipt, so a missing deslop never fails."""
    try:
        proc = subprocess.run(["deslop", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def payload_for(draft: Draft, ws, gates) -> dict:
    return {"draft": str(draft.path), "kind": draft.kind, "workspace": ws.slug, "slug": draft.slug or None,
            "sha256": draft.sha, "inputs": input_hashes(ws, draft.path, draft.slug or None),
            "tools": {**tool_hashes(), "deslop": deslop_version()},
            "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "result": result_of(gates), "gates": [asdict(g) for g in gates]}


def write_result(path: Path, payload: dict) -> None:
    """Temp file, then os.replace: a crash leaves the previous result whole, never half a file."""
    write_json_atomic(path, payload)


def check_and_write(draft: Draft, ws) -> dict:
    """A full run on an already loaded draft: all gates, result file written. publish keeps the Draft so it
    publishes exactly the bytes that were checked."""
    payload = payload_for(draft, ws, run(draft, ws))
    write_result(ws.contains(sidecar(draft.path, ".check.json")), payload)
    return payload


# ---------------------------------------------------------------- CLI

def _text_lines(gates, result, wrote=None) -> list[str]:
    out = []
    for g in gates:
        tag = " (not blocking)" if g.status == WARN and not g.blocking else ""
        where = f" (line {g.line})" if g.line else ""
        out.append(f"{g.status:<5}  {g.name:<10}  {g.detail}{where}{tag}")
    out.append(f"result: {result}" + (f"  -> {wrote}" if wrote else ""))
    return out


def cmd_check(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        draft = Draft.load(ws, args.draft, args.kind, args.slug, args.query, args.theme)
    except CouldNotRun as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    gates = run(draft, ws, only=args.gate)
    if args.gate:
        result = result_of(gates)
        ctx.emit({"draft": str(draft.path), "gate_only": True, "result": result, "gates": [asdict(g) for g in gates],
                  "messages": _text_lines(gates, result)})
        return EXIT_FOR[result]
    payload = payload_for(draft, ws, gates)
    target = ws.contains(sidecar(draft.path, ".check.json"))
    write_result(target, payload)
    ctx.emit({**payload, "messages": _text_lines(gates, payload["result"], target)})
    return EXIT_FOR[payload["result"]]


def register(csub, parents) -> None:
    c = csub.add_parser("check", parents=[parents.workspace, parents.json],
                        help="Run every gate on one draft and write <draft>.check.json")
    c.add_argument("draft", help="Article (.md) or Business Profile post (.txt), inside the workspace")
    c.add_argument("--kind", required=True, choices=["article", "gbp"])
    c.add_argument("--slug", default="", help="The article's slug in the content log (section order, own-entry exclusion)")
    c.add_argument("--query", default="", help="Target question for the duplicate check (default: the H1)")
    c.add_argument("--theme", default="", help="Business Profile theme for the duplicate check")
    c.add_argument("--gate", choices=GATE_NAMES, help="Run one gate for debugging; writes no result file")
    c.set_defaults(func=cmd_check, command_id="content check")
